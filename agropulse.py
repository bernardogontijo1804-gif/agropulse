"""
AgroPulse — Sistema de Relatórios de Mercado Agrícola
======================================================
Versão: 2.5 (Produção)

MUDANÇAS v2.4 → v2.5:
1. ROLLOVER — Segunda consulta para verificação de contractSymbol agora
   ocorre em TODOS os futuros, inclusive quando a variação está na faixa
   normal (≤5%). A segunda consulta é reutilizada como verificação de
   consistência quando há variação elevada, evitando consultas extras.

2. RETRY CONTROLADO — Status 'cancelado' no SQLite distingue se o pipeline
   foi cancelado antes ou depois de iniciar o envio. Cancelamento antes do
   envio permite uma nova tentativa automática. Cancelamento depois do envio
   iniciado bloqueia nova execução automática e exige intervenção manual.
   Nova coluna `envio_iniciado` na tabela `execucoes_diarias`.

3. LOCK EXCLUSIVO SQLite — Proteção contra dois processos simultâneos usando
   transação exclusiva ao gravar 'iniciado'. A leitura de `execucao_ja_realizada`
   e a gravação de 'iniciado' são atômicas, eliminando a race condition anterior.

4. ENVIO APÓS 21H DOCUMENTADO — A janela 08:00–21:00 BRT impede novos inícios
   de envio, mas não interrompe uma fila já em andamento. Fila iniciada às
   19:00 completa normalmente mesmo que ultrapasse 21:00.

5. LOG DE AUDITORIA COMPLETO — Cada ativo futuro registra dados das duas
   consultas separadamente: preço, data, contractSymbol e horário de cada uma.

MUDANÇAS v2.3 → v2.4:
1. AGENDAMENTO — `data_ultima_execucao` agora é persistida no SQLite
   (tabela `execucoes_diarias`). Reinício do servidor após 18:30 não
   dispara o pipeline se já houver registro de execução bem-sucedida
   para o dia. Se não houver registro e o horário for >= 18:30, executa
   uma única vez (recuperação após reinício).

2. ROLLOVER — Separação explícita de conceitos:
   - `rollover_confirmado`: divergência de contractSymbol entre duas
     consultas independentes ao mesmo ticker. Indica rollover em andamento
     no momento da coleta.
   - `rollover_suspeito_por_preco`: salto > LIMIAR_ROLLOVER_PRECO (8%)
     entre os dois últimos pregões do histórico. É um sinal indireto;
     NÃO confirma rollover — apenas bloqueia por segurança.
   O código nunca fabrica ou infere contractSymbol histórico por linha.

MUDANÇAS v2.2 → v2.3:
1. CONTRATO DOS FUTUROS — Validação real de continuidade via detecção de
   rollover por salto de preço e comparação de contractSymbol entre os dois
   últimos pregões. Se rollover for detectado ou contrato não puder ser
   verificado, o ativo é BLOQUEADO (não publicado).

2. DATA DO PREGÃO — Defasagem excessiva agora é bloqueio real, não apenas
   aviso. Tolerância de 2 dias úteis (não corridos). Ativo essencial com
   data inválida cancela o relatório inteiro.

3. AGENDAMENTO — Removido `schedule`. Loop baseado em datetime.now(TZ_BRASIL).
   Funciona corretamente mesmo com servidor em UTC.

4. CONFIRMAÇÃO DE VALORES EXTREMOS — Acima de 20% bloqueia mesmo com
   confirmação. Confirmação valida data, contrato e tolerância de preço.
   Documentado claramente que segunda consulta ao Yahoo ≠ confirmação oficial.

MANTIDO da v2.2:
- Remoção completa de portos, prêmios, sorgo
- auto_adjust=False
- ZoneInfo("America/Sao_Paulo") em todo código interno
- USDBRL=X + fallback BRL=X invertido
- Logs detalhados
- Flask/webhook, SQLite, Z-API, Anthropic
- Delays anti-ban (45–75s entre envios)
- Constantes de conversão auditadas
- Estrutura da mensagem WhatsApp

LIMITAÇÕES DOCUMENTADAS (Yahoo Finance):
- Close ≠ settlement oficial CME/ICE. É o último preço negociado retornado
  pelo Yahoo. Para settlement oficial seria necessário CME DataMine ou Quandl.
- contractSymbol em ticker.info reflete o contrato atual no momento da
  consulta, não o contrato de cada linha histórica individual.
- A validação de rollover é uma proteção probabilística, não uma garantia
  absoluta. Um rollover com preço muito similar ao contrato expirado pode
  passar sem detecção.

Horário de coleta : 18:30 BRT (America/Sao_Paulo)
Horário de envio  : 19:00 BRT (America/Sao_Paulo)
"""

import anthropic
import requests
import time
import random
import sqlite3
import os
import json
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request as flask_request

# ============================================================
# CONFIGURAÇÕES
# ============================================================
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY",    "")
ZAPI_INSTANCE_ID     = os.environ.get("ZAPI_INSTANCE_ID",     "")
ZAPI_TOKEN           = os.environ.get("ZAPI_TOKEN",           "")
ZAPI_CLIENT_TOKEN    = os.environ.get("ZAPI_CLIENT_TOKEN",    "")
WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", "agropulse2024")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agropulse.db")

TZ_BRASIL = ZoneInfo("America/Sao_Paulo")

# ============================================================
# CONSTANTES DE CONVERSÃO (mantidas — auditadas v2.1)
# ============================================================
SOJA_KG_POR_BUSHEL  = 27.2155
MILHO_KG_POR_BUSHEL = 25.4012
TRIGO_KG_POR_BUSHEL = 27.2155
SACA_KG             = 60.0

# ============================================================
# FAIXAS DE VALIDAÇÃO DE VARIAÇÃO
# ============================================================
VARIACAO_NORMAL      = 5.0   # ≤ 5%  → aceita normalmente
VARIACAO_ALERTA      = 8.0   # 5–8%  → alerta no log, aceita
VARIACAO_CONFIRMACAO = 12.0  # 8–12% → segunda consulta obrigatória
VARIACAO_EXTREMA     = 20.0  # > 20% → BLOQUEIO mesmo com confirmação

# Tolerância máxima de dias úteis de defasagem permitida na data do pregão
MAX_DEFASAGEM_DIAS_UTEIS = 2

# Limiar de variação de preço que indica provável rollover de contrato (%)
# Contratos futuros tipicamente diferem 1–3% entre vencimentos consecutivos;
# um salto > 8% sem notícia relevante quase sempre indica troca de contrato.
LIMIAR_ROLLOVER_PRECO = 8.0

# ============================================================
# APP FLASK
# ============================================================
app = Flask(__name__)

@app.route("/webhook", methods=["GET"])
def webhook_verificar():
    mode      = flask_request.args.get("hub.mode")
    token     = flask_request.args.get("hub.verify_token")
    challenge = flask_request.args.get("hub.challenge")
    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        print("✅ Webhook Meta verificado.")
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook_receber():
    data = flask_request.get_json(silent=True)
    if data:
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)",
                      ("Webhook Meta", json.dumps(data)[:300]))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Erro ao salvar log webhook: {e}")
    return "OK", 200


# ============================================================
# BANCO DE DADOS — LOG ESTRUTURADO
# ============================================================
def registrar_log(evento: str, detalhes: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)", (evento, detalhes))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Erro ao registrar log: {e}")


# ============================================================
# PERSISTÊNCIA DO ESTADO DE EXECUÇÃO DIÁRIA (SQLite)
# ============================================================
def _garantir_tabela_execucoes():
    """
    Cria a tabela `execucoes_diarias` se não existir.
    Chamada uma vez na inicialização do loop.

    Schema:
        data_execucao  TEXT PRIMARY KEY — formato ISO 'YYYY-MM-DD' (data BRT)
        status         TEXT             — 'iniciado' | 'concluido' | 'cancelado'
        envio_iniciado INTEGER          — 0 = envio não começou | 1 = envio iniciado
        timestamp_brt  TEXT             — datetime completo para auditoria

    A coluna `envio_iniciado` distingue dois tipos de cancelamento:
    - cancelado com envio_iniciado=0 → retry automático permitido
    - cancelado com envio_iniciado=1 → bloqueia retry, exige intervenção manual
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS execucoes_diarias (
                data_execucao  TEXT    PRIMARY KEY,
                status         TEXT    NOT NULL,
                envio_iniciado INTEGER NOT NULL DEFAULT 0,
                timestamp_brt  TEXT    NOT NULL
            )
        """)
        # Migração suave: adiciona coluna se a tabela já existir sem ela
        try:
            conn.execute("ALTER TABLE execucoes_diarias ADD COLUMN envio_iniciado INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # coluna já existe — ignorar
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Erro ao criar tabela execucoes_diarias: {e}")


def registrar_execucao(data_brt: date, status: str, envio_iniciado: bool = False):
    """
    Insere ou atualiza o registro de execução do dia.

    `status`:
        'iniciado'  — pipeline começou (gravado atomicamente antes da coleta)
        'concluido' — pipeline terminou normalmente
        'cancelado' — pipeline abortado

    `envio_iniciado`:
        False (padrão) — cancelamento antes do envio WhatsApp
        True           — cancelamento depois de começar o envio WhatsApp

    IMPORTANTE: ao gravar 'concluido' ou 'cancelado', nunca regride
    `envio_iniciado` de 1 para 0.
    """
    ts = datetime.now(TZ_BRASIL).strftime("%Y-%m-%d %H:%M:%S BRT")
    ei = 1 if envio_iniciado else 0
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO execucoes_diarias (data_execucao, status, envio_iniciado, timestamp_brt)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(data_execucao) DO UPDATE SET
                status         = excluded.status,
                envio_iniciado = MAX(execucoes_diarias.envio_iniciado, excluded.envio_iniciado),
                timestamp_brt  = excluded.timestamp_brt
        """, (data_brt.isoformat(), status, ei, ts))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Erro ao registrar execução: {e}")


def tentar_iniciar_execucao(data_brt: date) -> bool:
    """
    Tenta registrar atomicamente 'iniciado' para o dia usando transação
    exclusiva SQLite, eliminando a race condition entre dois processos
    que consultem o banco simultaneamente.

    Retorna True se esta instância conseguiu o lock e registrou 'iniciado'.
    Retorna False se outro processo já registrou um status incompatível.

    Lógica de retry por status:
        Ausente            → registra 'iniciado', retorna True
        'iniciado'         → outro processo está rodando (ou queda) — retorna False
        'concluido'        → nunca repetir — retorna False
        'cancelado' + envio_iniciado=0 → cancelamento pré-envio → registra 'iniciado', retorna True
        'cancelado' + envio_iniciado=1 → envio já começou → bloqueia, retorna False
    """
    ts = datetime.now(TZ_BRASIL).strftime("%Y-%m-%d %H:%M:%S BRT")
    data_str = data_brt.isoformat()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        # BEGIN EXCLUSIVE: apenas uma conexão por vez pode entrar neste bloco
        conn.execute("BEGIN EXCLUSIVE")
        cur = conn.execute(
            "SELECT status, envio_iniciado FROM execucoes_diarias WHERE data_execucao = ?",
            (data_str,)
        )
        row = cur.fetchone()

        if row is None:
            # Nenhum registro → inserir 'iniciado'
            conn.execute("""
                INSERT INTO execucoes_diarias (data_execucao, status, envio_iniciado, timestamp_brt)
                VALUES (?, 'iniciado', 0, ?)
            """, (data_str, ts))
            conn.commit()
            conn.close()
            registrar_log("Execução registrada", f"Data={data_brt} | Status=iniciado (novo registro)")
            return True

        status_atual, envio_ini = row[0], row[1]

        if status_atual == "concluido":
            conn.rollback()
            conn.close()
            print(f"  📋 {data_brt}: status=concluido — não repetir.")
            registrar_log("Agendamento bloqueado", f"Data={data_brt} | Status=concluido")
            return False

        if status_atual == "iniciado":
            conn.rollback()
            conn.close()
            print(f"  📋 {data_brt}: status=iniciado — outro processo ativo ou queda. Aguardando.")
            registrar_log("Agendamento bloqueado", f"Data={data_brt} | Status=iniciado (possível processo ativo)")
            return False

        if status_atual == "cancelado":
            if envio_ini == 1:
                # Envio já começou — não repetir automaticamente
                conn.rollback()
                conn.close()
                print(f"  📋 {data_brt}: cancelado APÓS envio iniciado — requer intervenção manual.")
                registrar_log(
                    "Agendamento bloqueado",
                    f"Data={data_brt} | Status=cancelado | envio_iniciado=1 — intervenção manual necessária"
                )
                return False
            else:
                # Cancelado ANTES do envio — retry permitido
                conn.execute("""
                    UPDATE execucoes_diarias
                    SET status='iniciado', envio_iniciado=0, timestamp_brt=?
                    WHERE data_execucao=?
                """, (ts, data_str))
                conn.commit()
                conn.close()
                registrar_log(
                    "Retry autorizado",
                    f"Data={data_brt} | Status anterior=cancelado | envio_iniciado=0 → reiniciando"
                )
                print(f"  🔄 {data_brt}: retry autorizado (cancelado antes do envio).")
                return True

        # Status desconhecido — conservador: não executar
        conn.rollback()
        conn.close()
        registrar_log("Agendamento bloqueado", f"Data={data_brt} | Status desconhecido={status_atual}")
        return False

    except Exception as e:
        print(f"⚠️ Erro ao tentar iniciar execução: {e} — abortando por segurança")
        registrar_log("Erro lock execução", str(e))
        return False


# ============================================================
# UTILITÁRIOS DE DATA / PREGÃO
# ============================================================
def dias_uteis_entre(d1: date, d2: date) -> int:
    """
    Conta dias úteis (seg–sex) entre duas datas.
    Retorna valor positivo se d1 > d2 (defasagem), negativo se d1 < d2.
    Não considera feriados (Yahoo Finance também não os conhece).
    """
    if d1 == d2:
        return 0
    delta_sinal = 1 if d1 > d2 else -1
    inicio = min(d1, d2)
    fim    = max(d1, d2)
    total  = 0
    atual  = inicio + timedelta(days=1)
    while atual <= fim:
        if atual.weekday() < 5:
            total += 1
        atual += timedelta(days=1)
    return total * delta_sinal


def data_pregao_esperada() -> date:
    """
    Retorna a data do último pregão esperado nas bolsas americanas,
    calculada a partir do horário de Brasília.

    Lógica:
    - O pipeline é disparado às 18:30 BRT. Nesse horário, Chicago e Nova York
      já fecharam (mercados fecham ~15:15–15:30 Chicago / ~14:30–15:00 NY).
    - Se for dia útil, a data esperada é hoje.
    - Se for sábado ou domingo, a data esperada é sexta-feira.

    NÃO inventa feriados americanos — a verificação de feriados é feita
    implicitamente: se a bolsa não abriu, o Yahoo não terá dado para hoje
    e a coleta falhará na verificação de defasagem.
    """
    agora_br = datetime.now(TZ_BRASIL)
    d = agora_br.date()
    # Voltar ao último dia útil (seg=0 … sex=4)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d


def verificar_data_pregao(nome: str, data_coletada_str: str, essencial: bool) -> bool:
    """
    Verifica se a data coletada está dentro da tolerância aceitável.

    Retorna True se aceitável, False se deve rejeitar.

    Critérios:
    - Defasagem em dias úteis <= MAX_DEFASAGEM_DIAS_UTEIS → aceita
    - Defasagem > MAX_DEFASAGEM_DIAS_UTEIS → BLOQUEIA o ativo
    - Dado mais recente do que o esperado (data futura): também bloqueia
      (indica problema na fonte).
    """
    data_esperada = data_pregao_esperada()
    data_coletada = date.fromisoformat(data_coletada_str)

    # Dado do futuro: impossível — rejeitar
    if data_coletada > data_esperada:
        motivo = (
            f"Data coletada ({data_coletada}) é POSTERIOR à data esperada ({data_esperada}). "
            f"Possível erro na fonte."
        )
        registrar_log(f"Data INVÁLIDA — {nome}", motivo)
        print(f"  ❌ {nome}: {motivo}")
        return False

    defasagem_uteis = dias_uteis_entre(data_esperada, data_coletada)

    if defasagem_uteis <= MAX_DEFASAGEM_DIAS_UTEIS:
        if defasagem_uteis > 0:
            registrar_log(
                f"Data OK (com defasagem) — {nome}",
                f"Esperada={data_esperada} | Coletada={data_coletada} | "
                f"Defasagem={defasagem_uteis} dia(s) útil(eis) — dentro da tolerância"
            )
        return True

    # Defasagem excede tolerância → BLOQUEIO
    motivo = (
        f"Esperada={data_esperada} | Coletada={data_coletada} | "
        f"Defasagem={defasagem_uteis} dia(s) útil(eis) > máximo permitido "
        f"({MAX_DEFASAGEM_DIAS_UTEIS}). "
        f"Ativo={'ESSENCIAL — relatório cancelado' if essencial else 'não essencial — omitido'}."
    )
    registrar_log(f"Data BLOQUEADA — {nome}", motivo)
    print(f"  ❌ {nome}: data bloqueada. {motivo}")
    return False


# ============================================================
# VALIDAÇÃO DE ROLLOVER DE CONTRATO — DOIS CONCEITOS DISTINTOS
# ============================================================
#
# LIMITAÇÃO DOCUMENTADA (Yahoo Finance):
#
# O yfinance NÃO disponibiliza o contractSymbol de cada linha individual
# do histórico. ticker.info["contractSymbol"] reflete o contrato ativo
# NO MOMENTO DA CONSULTA, não o contrato associado a cada pregão histórico.
#
# Consequência: é impossível, com yfinance, afirmar com certeza que
# hist.iloc[-1] e hist.iloc[-2] pertencem ao mesmo contrato futuro.
#
# O código implementa duas proteções independentes:
#
#   rollover_confirmado:
#       Divergência de contractSymbol entre DUAS consultas independentes
#       feitas com intervalo de tempo ao mesmo ticker. Se entre a primeira
#       e a segunda consulta o contractSymbol mudar, o rollover está
#       ocorrendo no momento da coleta. Isso é verificável e confiável
#       dentro das limitações do yfinance.
#       → Bloqueia o ativo.
#
#   rollover_suspeito_por_preco:
#       Salto de preço > LIMIAR_ROLLOVER_PRECO (8%) entre os dois últimos
#       pregões do histórico. Contratos futuros consecutivos de commodities
#       tipicamente diferem 1–3%. Um salto maior é um SINAL INDIRETO de
#       possível rollover — não uma confirmação. Pode também ser causado
#       por evento de mercado extremo.
#       O código NÃO afirma que é rollover; afirma apenas que o dado é
#       suspeito o suficiente para bloquear a publicação por segurança.
#       → Bloqueia o ativo por precaução.
#
# Em nenhum caso o código fabrica, infere ou atribui contractSymbol
# a linhas individuais do histórico.
#
# ============================================================

def _rollover_confirmado(
    nome: str,
    contrato_consulta_1: str,
    contrato_consulta_2: str,
) -> tuple[bool, str]:
    """
    Verifica rollover confirmado: divergência de contractSymbol entre
    duas consultas independentes feitas em momentos distintos.

    Condição de aplicação: ambos os contractSymbol devem ser identificados
    (diferentes de 'desconhecido'). Se um ou ambos forem desconhecidos,
    esta verificação não pode ser realizada e retorna (False, '').

    Retorna (True, motivo) se rollover confirmado, (False, '') caso contrário.
    """
    if contrato_consulta_1 == "desconhecido" or contrato_consulta_2 == "desconhecido":
        # Não é possível verificar — não afirmar nem negar
        return False, ""

    if contrato_consulta_1 != contrato_consulta_2:
        motivo = (
            f"ROLLOVER CONFIRMADO: contractSymbol divergiu entre consultas independentes. "
            f"Consulta 1={contrato_consulta_1} | Consulta 2={contrato_consulta_2}. "
            f"Rollover em andamento no momento da coleta. Ativo bloqueado."
        )
        registrar_log(f"Rollover CONFIRMADO — {nome}", motivo)
        print(f"  🚨 {nome}: {motivo}")
        return True, motivo

    return False, ""


def _rollover_suspeito_por_preco(
    nome: str,
    preco_atual: float,
    preco_anterior: float,
) -> tuple[bool, str]:
    """
    Sinal indireto de possível rollover: salto de preço > LIMIAR_ROLLOVER_PRECO
    entre os dois últimos pregões do histórico.

    IMPORTANTE: Este método NÃO confirma rollover. Detecta um padrão de preço
    anormal que pode ser causado por rollover de contrato OU por evento de
    mercado extremo. Em ambos os casos, a publicação é bloqueada por segurança.

    O código NÃO infere nem atribui contractSymbol histórico por linha.
    Apenas constata que a variação de preço é suspeita o suficiente para
    impedir a publicação automática.

    Retorna (True, motivo) se suspeita detectada, (False, '') caso contrário.
    """
    if preco_anterior <= 0:
        return False, ""

    variacao_abs = abs((preco_atual - preco_anterior) / preco_anterior * 100)

    if variacao_abs > LIMIAR_ROLLOVER_PRECO:
        motivo = (
            f"ROLLOVER SUSPEITO POR PREÇO: salto de {variacao_abs:.2f}% entre os dois "
            f"últimos pregões excede o limiar ({LIMIAR_ROLLOVER_PRECO}%). "
            f"Atual={preco_atual:.4f} | Anterior={preco_anterior:.4f}. "
            f"Causa desconhecida (rollover ou evento extremo). "
            f"Ativo bloqueado por precaução. NÃO é confirmação de rollover."
        )
        registrar_log(f"Rollover SUSPEITO POR PREÇO — {nome}", motivo)
        print(f"  🚨 {nome}: {motivo}")
        return True, motivo

    return False, ""


def detectar_rollover(
    nome: str,
    contrato_consulta_1: str,
    contrato_consulta_2: str,
    preco_atual: float,
    preco_anterior: float,
) -> tuple[bool, str]:
    """
    Ponto de entrada unificado para detecção de rollover.
    Executa as duas verificações independentes em ordem.

    1. rollover_confirmado   → divergência de contractSymbol entre consultas
    2. rollover_suspeito_por_preco → salto de preço anormal

    Retorna (bloqueado: bool, motivo: str).
    Retorna True (bloquear) na primeira verificação positiva.
    """
    confirmado, motivo = _rollover_confirmado(
        nome, contrato_consulta_1, contrato_consulta_2
    )
    if confirmado:
        return True, motivo

    suspeito, motivo = _rollover_suspeito_por_preco(
        nome, preco_atual, preco_anterior
    )
    if suspeito:
        return True, motivo

    return False, ""


# ============================================================
# COLETA PRINCIPAL — yfinance
# ============================================================
#
# ARQUITETURA DE CONSULTAS (v2.5):
#
# Para ativos futuros (ZS=F, ZC=F, ZW=F, KC=F, CT=F, CL=F, BZ=F):
#   Consulta 1: preço + data + contractSymbol (sempre)
#   Consulta 2: preço + data + contractSymbol (sempre, para verificar rollover)
#   → A Consulta 2 é reutilizada como verificação de consistência quando
#     a variação for elevada (>8%), eliminando uma terceira chamada.
#
# Para ativos não-futuros (USDBRL=X, BRL=X):
#   Consulta única — sem contractSymbol relevante para rollover.
#   Segunda consulta apenas se variação for alta (comportamento anterior).
#
# LIMITAÇÕES DOCUMENTADAS (Yahoo Finance):
#   - Close = último preço negociado. NÃO é settlement oficial CME/ICE.
#   - contractSymbol de ticker.info reflete o contrato ATUAL no momento da
#     consulta, NÃO o contrato de linhas históricas individuais.
#   - Duas consultas ao Yahoo confirmam consistência da fonte, não veracidade
#     do preço perante as bolsas oficiais.
#
# ============================================================

def _buscar_ticker_raw(simbolo: str, tentativa_num: int = 1) -> dict | None:
    """
    Consulta yfinance para um símbolo.
    Retorna dicionário com dados brutos ou None em caso de falha.

    Campos retornados:
        atual, anterior, maxima, minima — preços em unidade bruta da fonte
        data_atual, data_anterior       — datas ISO dos dois últimos pregões
        contrato                        — contractSymbol no momento da consulta
                                          ('desconhecido' se ticker.info falhar)
        vencimento                      — mês/ano do vencimento ('desconhecido' se ausente)
        hora_consulta_brt               — timestamp BRT da consulta (para auditoria)
        fonte, campo_preco              — metadados de rastreabilidade
    """
    import yfinance as yf
    time.sleep(2.0 * tentativa_num)

    hora_consulta = datetime.now(TZ_BRASIL).strftime("%Y-%m-%d %H:%M:%S BRT")
    ticker = yf.Ticker(simbolo)

    # contractSymbol do momento da consulta — NÃO é o contrato por linha histórica
    contrato_info   = "desconhecido"
    vencimento_info = "desconhecido"
    try:
        info = ticker.info
        contrato_info = info.get("contractSymbol", info.get("symbol", simbolo))
        expire_ts = info.get("expireDate", None)
        if expire_ts:
            vencimento_info = datetime.fromtimestamp(expire_ts).strftime("%Y-%m")
    except Exception:
        pass  # info pode falhar — não bloqueia a coleta do histórico

    # auto_adjust=False: preço bruto do contrato, sem ajustes
    hist = ticker.history(period="10d", auto_adjust=False)

    if "Close" not in hist.columns or hist.empty:
        return None

    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        return None

    atual_row    = hist.iloc[-1]
    anterior_row = hist.iloc[-2]

    return {
        "atual":            float(atual_row["Close"]),
        "anterior":         float(anterior_row["Close"]),
        "maxima":           float(atual_row.get("High", atual_row["Close"])),
        "minima":           float(atual_row.get("Low",  atual_row["Close"])),
        "data_atual":       str(hist.index[-1].date()),
        "data_anterior":    str(hist.index[-2].date()),
        "contrato":         contrato_info,
        "vencimento":       vencimento_info,
        "hora_consulta_brt":hora_consulta,
        "fonte":            "yfinance / Yahoo Finance",
        "campo_preco":      "Close (último negociado — NÃO é settlement oficial da bolsa)",
        "tentativa":        tentativa_num,
    }


def _segunda_consulta(
    simbolo: str,
    dados1: dict,
    variacao_abs: float,
    exigir_consistencia_preco: bool,
) -> dict | None:
    """
    Realiza a segunda consulta ao Yahoo Finance.

    Função unificada usada para:
    (a) Verificação de contractSymbol em todos os futuros (variacao_abs ≤ limiar)
    (b) Verificação de consistência de preço quando variação é elevada

    PARÂMETROS:
        dados1                    — resultado da primeira consulta
        variacao_abs              — variação absoluta calculada da 1ª consulta
        exigir_consistencia_preco — se True, verifica divergência de preço ≤ 0.5%

    RETORNA:
        dict com dados da 2ª consulta se tudo OK
        None se qualquer validação falhar

    CRITÉRIOS DE REJEIÇÃO:
        1. Variação > VARIACAO_EXTREMA (20%) → bloqueio incondicional
        2. Data diverge entre consultas
        3. contractSymbol diverge (rollover confirmado) → bloqueia
        4. Preço diverge > 0.5% (apenas se exigir_consistencia_preco=True)

    NOTA: Duas consultas ao Yahoo confirmam consistência da coleta, NÃO
    settlement oficial da bolsa.
    """
    # Bloqueio incondicional acima de 20%
    if variacao_abs > VARIACAO_EXTREMA:
        motivo = (
            f"Variação de {variacao_abs:.2f}% excede limite absoluto ({VARIACAO_EXTREMA}%). "
            f"Publicação bloqueada. Requer verificação manual."
        )
        registrar_log(f"Bloqueio EXTREMO — {simbolo}", motivo)
        print(f"  🚫 {simbolo}: {motivo}")
        return None

    print(f"  🔍 Segunda consulta para {simbolo}...")
    dados2 = _buscar_ticker_raw(simbolo, tentativa_num=2)

    if dados2 is None:
        registrar_log(f"Segunda consulta FALHOU — {simbolo}", "Retornou vazio")
        print(f"  ❌ {simbolo}: segunda consulta retornou vazio")
        return None

    # 1. Verificar data
    if dados2["data_atual"] != dados1["data_atual"]:
        registrar_log(
            f"Segunda consulta FALHOU (data) — {simbolo}",
            f"Consulta 1={dados1['data_atual']} ({dados1['hora_consulta_brt']}) | "
            f"Consulta 2={dados2['data_atual']} ({dados2['hora_consulta_brt']})"
        )
        print(f"  ❌ {simbolo}: datas divergem entre consultas — ativo rejeitado")
        return None

    # 2. Verificar contractSymbol — rollover confirmado se divergirem
    c1, c2 = dados1["contrato"], dados2["contrato"]
    rollover_conf, motivo_conf = _rollover_confirmado(simbolo, c1, c2)
    if rollover_conf:
        # Log auditável com dados completos das duas consultas
        registrar_log(
            f"Rollover CONFIRMADO — {simbolo}",
            f"Consulta1: preço={dados1['atual']:.4f} | data={dados1['data_atual']} | "
            f"contrato={c1} | hora={dados1['hora_consulta_brt']} || "
            f"Consulta2: preço={dados2['atual']:.4f} | data={dados2['data_atual']} | "
            f"contrato={c2} | hora={dados2['hora_consulta_brt']}"
        )
        return None

    # 3. Verificar consistência de preço (somente quando solicitado)
    if exigir_consistencia_preco:
        preco1      = dados1["atual"]
        preco2      = dados2["atual"]
        divergencia = abs(preco1 - preco2) / preco1 * 100

        if divergencia > 0.5:
            registrar_log(
                f"Consistência FALHOU (preço) — {simbolo}",
                f"Consulta1={preco1:.4f} ({dados1['hora_consulta_brt']}) | "
                f"Consulta2={preco2:.4f} ({dados2['hora_consulta_brt']}) | "
                f"Divergência={divergencia:.2f}%"
            )
            print(f"  ❌ {simbolo}: preços divergem ({divergencia:.2f}%) — ativo rejeitado")
            return None

        registrar_log(
            f"Consistência OK — {simbolo}",
            f"Consulta1={preco1:.4f} | Consulta2={preco2:.4f} | "
            f"Divergência={divergencia:.2f}% | contractSymbol={c1} | "
            f"NOTA: consistência de coleta, não settlement oficial"
        )
        print(f"  ✅ {simbolo}: consistência OK (divergência={divergencia:.2f}%)")
    else:
        # Consulta de contractSymbol apenas — log resumido
        registrar_log(
            f"Segunda consulta OK (contractSymbol) — {simbolo}",
            f"Contrato1={c1} ({dados1['hora_consulta_brt']}) | "
            f"Contrato2={c2} ({dados2['hora_consulta_brt']}) | "
            f"Sem divergência de rollover"
        )
        print(f"  ✅ {simbolo}: contractSymbol consistente ({c1})")

    return dados2


def buscar_ticker(
    simbolo: str,
    nome: str,
    bolsa: str,
    essencial: bool = False,
    eh_futuro: bool = True,
) -> dict | None:
    """
    Coleta dados de um ativo com validação completa.

    FLUXO v2.5:
    1. Consulta 1: preço + data + contractSymbol (até 3 tentativas)
    2. Validação de preço básico (> 0)
    3. Verificação de data do pregão com bloqueio real (dias úteis)
    4. Cálculo de variação e verificação de rollover_suspeito_por_preco
    5. Segunda consulta:
       - Para futuros (eh_futuro=True): SEMPRE, para verificar contractSymbol
       - Para não-futuros: somente se variação > VARIACAO_ALERTA (8%)
       - Para variação > VARIACAO_ALERTA: segunda consulta com verificação
         de consistência de preço (tolerance 0.5%), reutilizando a mesma
         chamada da verificação de contractSymbol — sem consulta extra
    6. Log auditável com dados das duas consultas

    PARÂMETRO eh_futuro:
        True  — ativo tem contractSymbol relevante; segunda consulta sempre obrigatória
        False — ativo forex/spot; segunda consulta apenas se variação for alta

    Retorna None em qualquer falha.
    """
    # --- Consulta 1 (até 3 tentativas) ---
    dados1 = None
    for tentativa in range(1, 4):
        try:
            dados1 = _buscar_ticker_raw(simbolo, tentativa_num=tentativa)
            if dados1:
                break
        except Exception as e:
            print(f"  ⚠️ {simbolo} tentativa {tentativa}/3: {e}")
            if tentativa < 3:
                time.sleep(3.0 * tentativa)

    if dados1 is None:
        registrar_log(f"Coleta FALHOU — {nome}", f"Símbolo={simbolo} | 3 tentativas esgotadas")
        print(f"  ❌ {nome}: falha na coleta após 3 tentativas")
        return None

    # --- Validação de preço básico ---
    if dados1["atual"] <= 0 or dados1["anterior"] <= 0:
        registrar_log(
            f"Coleta INVÁLIDA — {nome}",
            f"Preço zero ou negativo: atual={dados1['atual']} | anterior={dados1['anterior']}"
        )
        print(f"  ❌ {nome}: preço inválido ({dados1['atual']})")
        return None

    # --- Verificação de data do pregão ---
    if not verificar_data_pregao(nome, dados1["data_atual"], essencial):
        return None

    # --- Cálculo de variação ---
    variacao_bruta = ((dados1["atual"] - dados1["anterior"]) / dados1["anterior"]) * 100
    abs_variacao   = abs(variacao_bruta)

    # --- Determinação da segunda consulta ---
    # Para futuros: sempre (verificar contractSymbol)
    # Para não-futuros: somente se variação > VARIACAO_ALERTA
    # A segunda consulta inclui verificação de consistência de preço se
    # a variação estiver acima de VARIACAO_ALERTA (8%).
    precisar_segunda = eh_futuro or abs_variacao > VARIACAO_ALERTA

    if not precisar_segunda:
        # Ativo não-futuro com variação normal — aceita com uma consulta.
        # Verificação de rollover_suspeito_por_preco ainda se aplica.
        suspeito, motivo_suspeito = _rollover_suspeito_por_preco(
            nome, dados1["atual"], dados1["anterior"]
        )
        if suspeito:
            registrar_log(f"Ativo BLOQUEADO (suspeita rollover) — {nome}", motivo_suspeito)
            return None
        dados_final = dados1
    else:
        # Segunda consulta obrigatória.
        #
        # IMPORTANTE — ordem das verificações para futuros com salto >8%:
        #
        # O _rollover_suspeito_por_preco é verificado APÓS a segunda consulta,
        # porque o salto >8% pode ser um evento real de mercado e não rollover.
        # A segunda consulta permite distinguir:
        #   - contractSymbol inalterado + preço consistente → evento de mercado real
        #     → aceitar (com log de variação alta)
        #   - contractSymbol diverge → rollover_confirmado → bloquear
        #   - Preço inconsistente entre consultas → bloquear
        #
        # Para ativos não-futuros (eh_futuro=False) com variação alta,
        # o _rollover_suspeito_por_preco não é aplicável (sem contractSymbol),
        # então a segunda consulta verifica somente consistência de preço.
        #
        # Para futuros com variação ≤ LIMIAR_ROLLOVER_PRECO (8%),
        # a segunda consulta verifica apenas contractSymbol (sem rollover suspeito).

        exigir_preco = abs_variacao > VARIACAO_ALERTA

        if abs_variacao > VARIACAO_ALERTA:
            nivel = "EXTREMA" if abs_variacao > VARIACAO_EXTREMA else (
                    "MUITO ALTA" if abs_variacao > VARIACAO_CONFIRMACAO else "ALTA")
            print(f"  ⚠️ {nome}: variação {nivel} ({variacao_bruta:+.2f}%) — segunda consulta obrigatória")
            registrar_log(
                f"Variação {nivel} — {nome}",
                f"Variação={variacao_bruta:.2f}% | Consulta 1: preço={dados1['atual']:.4f} | "
                f"data={dados1['data_atual']} | contrato={dados1['contrato']} | "
                f"hora={dados1['hora_consulta_brt']}"
            )

        dados2 = _segunda_consulta(
            simbolo=simbolo,
            dados1=dados1,
            variacao_abs=abs_variacao,
            exigir_consistencia_preco=exigir_preco,
        )

        if dados2 is None:
            print(f"  ❌ {nome}: segunda consulta falhou — ativo bloqueado")
            return None

        # Segunda consulta passou (contractSymbol OK, preço consistente se exigido).
        # Agora verificar rollover_suspeito_por_preco apenas para futuros
        # e somente se o contractSymbol não confirmou rollover.
        # Se eh_futuro e a variação histórica for suspeita MAS o contractSymbol
        # confirmou ser o mesmo contrato, ainda assim bloqueamos — a causa pode
        # ser evento extremo de mercado, o que também requer verificação manual.
        if eh_futuro and abs_variacao > LIMIAR_ROLLOVER_PRECO:
            suspeito, motivo_suspeito = _rollover_suspeito_por_preco(
                nome, dados2["atual"], dados2["anterior"]
            )
            if suspeito:
                # contractSymbol é o mesmo mas salto é suspeito — pode ser evento
                # extremo de mercado; exige verificação manual acima do limiar
                registrar_log(
                    f"Ativo BLOQUEADO (salto suspeito — mesmo contrato) — {nome}",
                    f"{motivo_suspeito} | contractSymbol={dados2['contrato']} (consistente)"
                )
                print(f"  ❌ {nome}: salto suspeito mesmo com contrato consistente — bloqueado")
                return None

        dados_final = dados2
        # Recalcular variação com dados da segunda consulta (podem diferir em ≤0.5%)
        variacao_bruta = ((dados_final["atual"] - dados_final["anterior"]) / dados_final["anterior"]) * 100
        abs_variacao   = abs(variacao_bruta)

        if abs_variacao > VARIACAO_CONFIRMACAO and abs_variacao <= VARIACAO_EXTREMA:
            registrar_log(
                f"Variação ALTA confirmada — {nome}",
                f"Variação confirmada={variacao_bruta:.2f}% | NOTA: consistência de coleta apenas"
            )

    # --- Alerta de log para faixa 5–8% ---
    if VARIACAO_NORMAL < abs_variacao <= VARIACAO_ALERTA:
        registrar_log(
            f"Variação ALERTA — {nome}",
            f"Variação={variacao_bruta:.2f}% | Atual={dados_final['atual']:.4f} | "
            f"Anterior={dados_final['anterior']:.4f} | Data={dados_final['data_atual']}"
        )
        print(f"  ⚠️ {nome}: variação elevada ({variacao_bruta:.2f}%) — alerta registrado")

    dados_final["variacao"] = round(variacao_bruta, 2)

    # --- Log auditável final ---
    c1_str = dados1["contrato"]
    c2_str = dados_final["contrato"] if dados_final is not dados1 else "N/A (única consulta)"
    log_final = (
        f"Ativo={nome} | Símbolo={simbolo} | Bolsa={bolsa} | "
        f"Consulta1: preço={dados1['atual']:.4f} | data={dados1['data_atual']} | "
        f"contrato={c1_str} | hora={dados1['hora_consulta_brt']} || "
        f"Consulta2: preço={dados_final['atual']:.4f} | data={dados_final['data_atual']} | "
        f"contrato={c2_str} | hora={dados_final['hora_consulta_brt']} || "
        f"Variação={dados_final['variacao']:.2f}% | Vencimento={dados_final['vencimento']} | "
        f"Resultado=OK | Fonte={dados_final['fonte']} | Campo={dados_final['campo_preco']}"
    )
    registrar_log(f"Coleta OK — {nome}", log_final)

    print(
        f"  ✅ {nome} ({bolsa}): {dados_final['anterior']:.4f} ({dados_final['data_anterior']}) → "
        f"{dados_final['atual']:.4f} ({dados_final['data_atual']}) = {variacao_bruta:+.2f}% "
        f"[contrato: {dados_final['contrato']}]"
    )

    return dados_final


# ============================================================
# COLETA COMPLETA
# ============================================================
def buscar_precos() -> dict:
    """
    Coleta todos os preços de mercado.

    FONTES DOCUMENTADAS:
    Soja      ZS=F      CBOT   yfinance — Close (não é settlement oficial CME)
    Milho     ZC=F      CBOT   yfinance — Close
    Trigo     ZW=F      CBOT   yfinance — Close
    Café      KC=F      ICE    yfinance — Close
    Algodão   CT=F      ICE    yfinance — Close
    WTI       CL=F      NYMEX  yfinance — Close
    Brent     BZ=F      ICE    yfinance — Close
    Dólar     USDBRL=X  FOREX  yfinance — BRL por 1 USD (dólar comercial referência)
              Fallback: BRL=X invertido (USD por BRL → invertido para BRL/USD)
    """
    print(f"\n{'='*55}")
    print(f"🔄 Coleta iniciada — {datetime.now(TZ_BRASIL).strftime('%d/%m/%Y %H:%M:%S BRT')}")
    print(f"{'='*55}")

    # Campos: (símbolo, bolsa, essencial, eh_futuro)
    # eh_futuro=True  → contrato futuro com contractSymbol → 2ª consulta sempre
    # eh_futuro=False → forex/spot sem contractSymbol relevante → 2ª consulta só se variação alta
    simbolos = {
        "Soja":           ("ZS=F",    "CBOT",  True,  True),
        "Milho":          ("ZC=F",    "CBOT",  True,  True),
        "Trigo":          ("ZW=F",    "CBOT",  False, True),
        "Cafe":           ("KC=F",    "ICE",   False, True),
        "Algodao":        ("CT=F",    "ICE",   False, True),
        "Petroleo WTI":   ("CL=F",    "NYMEX", False, True),
        "Petroleo Brent": ("BZ=F",    "ICE",   False, True),
        "Dolar":          ("USDBRL=X","FOREX", True,  False),
    }

    dados_raw = {}

    for nome, (simbolo, bolsa, essencial, eh_futuro) in simbolos.items():
        print(f"\n📡 Coletando {nome} ({simbolo})...")
        d = buscar_ticker(simbolo, nome, bolsa, essencial=essencial, eh_futuro=eh_futuro)
        if d:
            dados_raw[nome] = d

    # --- Fallback do dólar ---
    if "Dolar" not in dados_raw:
        print("\n📡 Fallback dólar: tentando BRL=X (invertido)...")
        d = buscar_ticker("BRL=X", "Dolar", "FOREX", essencial=True, eh_futuro=False)
        if d and d["atual"] > 0:
            # BRL=X = USD por BRL → inverter para BRL por USD
            anterior_orig = d["anterior"]
            atual_orig    = d["atual"]
            d["atual"]    = round(1.0 / atual_orig,    4)
            d["anterior"] = round(1.0 / anterior_orig, 4)
            d["variacao"] = round(((d["atual"] - d["anterior"]) / d["anterior"]) * 100, 2)
            d["campo_preco"] += " (BRL=X invertido → R$/USD)"
            dados_raw["Dolar"] = d
            registrar_log("Dólar via fallback BRL=X (invertido)", f"Valor={d['atual']:.4f}")

    # --- Validação de essenciais ---
    essenciais_obrigatorios = ["Soja", "Milho", "Dolar"]
    faltando = [e for e in essenciais_obrigatorios if e not in dados_raw]
    if faltando:
        raise ValueError(f"Dados essenciais ausentes: {faltando}. Relatório cancelado.")

    # ============================================================
    # CONSTRUÇÃO DO DICIONÁRIO FINAL
    # ============================================================
    precos = {}

    def processar_cents_para_usd(nome_ativo: str, unidade_final: str) -> None:
        """Converte cents/bushel ou cents/libra → USD."""
        if nome_ativo not in dados_raw:
            return
        r            = dados_raw[nome_ativo]
        atual_usd    = r["atual"]    / 100
        anterior_usd = r["anterior"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos[nome_ativo] = {
            "valor":      round(atual_usd, 4),
            "anterior":   round(anterior_usd, 4),
            "variacao":   variacao,
            "unidade":    unidade_final,
            "contrato":   r["contrato"],
            "vencimento": r["vencimento"],
            "data":       r["data_atual"],
            "fonte":      r["fonte"],
        }

    def processar_usd_direto(nome_ativo: str, unidade_final: str, casas: int = 2) -> None:
        """Usa preço direto em USD sem conversão."""
        if nome_ativo not in dados_raw:
            return
        r        = dados_raw[nome_ativo]
        atual    = r["atual"]
        anterior = r["anterior"]
        variacao = round(((atual - anterior) / anterior) * 100, 2)
        precos[nome_ativo] = {
            "valor":      round(atual, casas),
            "anterior":   round(anterior, casas),
            "variacao":   variacao,
            "unidade":    unidade_final,
            "contrato":   r["contrato"],
            "vencimento": r["vencimento"],
            "data":       r["data_atual"],
            "fonte":      r["fonte"],
        }

    processar_cents_para_usd("Soja",    "USD/bushel")
    processar_cents_para_usd("Milho",   "USD/bushel")
    processar_cents_para_usd("Trigo",   "USD/bushel")
    processar_cents_para_usd("Cafe",    "USD/libra (ICE)")
    processar_cents_para_usd("Algodao", "USD/libra (ICE)")

    processar_usd_direto("Petroleo WTI",   "USD/barril", casas=2)
    processar_usd_direto("Petroleo Brent", "USD/barril", casas=2)

    # Dólar: USDBRL=X já está em BRL/USD
    if "Dolar" in dados_raw:
        r        = dados_raw["Dolar"]
        atual    = r["atual"]
        anterior = r["anterior"]
        variacao = round(((atual - anterior) / anterior) * 100, 2)
        precos["Dolar"] = {
            "valor":      round(atual,    4),
            "anterior":   round(anterior, 4),
            "variacao":   variacao,
            "unidade":    "R$/USD (USDBRL=X — Dólar Comercial referência)",
            "contrato":   r["contrato"],
            "vencimento": r["vencimento"],
            "data":       r["data_atual"],
            "fonte":      r["fonte"],
        }

    print(f"\n✅ Coleta concluída — {len(precos)} ativos processados")
    return precos


# ============================================================
# VALIDAÇÃO DOS DADOS
# ============================================================
def validar_precos(precos: dict) -> tuple[bool, list]:
    erros_criticos = []

    for nome, dados in precos.items():
        if dados.get("valor", 0) <= 0:
            erros_criticos.append(f"{nome}: preço inválido ({dados.get('valor')})")

    essenciais = ["Soja", "Milho", "Dolar", "Petroleo WTI"]
    for e in essenciais:
        if e not in precos:
            erros_criticos.append(f"{e}: ativo essencial ausente")

    if erros_criticos:
        for err in erros_criticos:
            registrar_log("Validação CRÍTICA", err)
            print(f"❌ Validação crítica: {err}")
        return False, erros_criticos

    registrar_log("Validação OK", f"{len(precos)} ativos validados")
    return True, []


# ============================================================
# GERAÇÃO DO RESUMO COM IA
# ============================================================
def gerar_resumo_ia(precos: dict) -> str:
    """
    Passa somente os dados realmente coletados para a IA.
    Prompt instrui a não inventar causas, dados ou contextos externos.
    """
    cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    linhas = []
    for nome, dados in precos.items():
        sinal = "+" if dados["variacao"] > 0 else ""
        linhas.append(
            f"{nome}: {dados['valor']} {dados.get('unidade', '')} "
            f"({sinal}{dados['variacao']:.2f}%)"
        )

    resposta = cliente.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=350,
        messages=[{
            "role": "user",
            "content": (
                "Você é um analista sênior do agronegócio brasileiro.\n\n"
                "Com base EXCLUSIVAMENTE nos dados de fechamento abaixo, escreva uma análise "
                "de 3 frases objetivas e diretas para produtores rurais e profissionais do agro.\n\n"
                "Destaque: maiores movimentos do dia, impacto do dólar e do petróleo, "
                "o que isso significa para exportadores e produtores brasileiros.\n\n"
                "REGRAS OBRIGATÓRIAS:\n"
                "- Use apenas os dados fornecidos. Não invente causas, fatores ou contextos externos.\n"
                "- Se houver forte valorização/queda, pode mencioná-la, mas NÃO invente o motivo.\n"
                "- Português claro, sem markdown, sem asteriscos, sem emojis.\n"
                "- Máximo 3 frases.\n"
                "- Não mencione estimativas, preços de portos ou prêmios.\n\n"
                f"Dados do pregão:\n{chr(10).join(linhas)}"
            ),
        }]
    )
    return resposta.content[0].text.strip()


# ============================================================
# MONTAGEM DA MENSAGEM
# ============================================================
def montar_mensagem(precos: dict, resumo_ia: str) -> str:
    agora_br  = datetime.now(TZ_BRASIL)
    data_hoje = agora_br.strftime("%d/%m/%Y")

    def linha_ativo(nome, prefixo="US$", casas=2):
        if nome not in precos:
            return ""
        d     = precos[nome]
        emoji = "📈" if d["variacao"] > 0 else "📉"
        sinal = "+" if d["variacao"] > 0 else ""
        return f"{emoji} *{nome}:* {prefixo} {d['valor']:.{casas}f} ({sinal}{d['variacao']:.2f}%)\n"

    msg = f"🌾 *AGROPULSE — Fechamento do Mercado*\n📅 {data_hoje}\n"

    msg += "\n*📊 BOLSA DE CHICAGO (CBOT)*\n"
    for nome in ["Soja", "Milho", "Trigo"]:
        msg += linha_ativo(nome, "US$", 2)

    msg += "\n*🧋 ICE (Nova York)*\n"
    for nome in ["Cafe", "Algodao"]:
        msg += linha_ativo(nome, "US$", 2)

    msg += "\n*🛢️ PETRÓLEO*\n"
    for nome in ["Petroleo WTI", "Petroleo Brent"]:
        msg += linha_ativo(nome, "US$", 2)

    if "Dolar" in precos:
        d     = precos["Dolar"]
        emoji = "📈" if d["variacao"] > 0 else "📉"
        sinal = "+" if d["variacao"] > 0 else ""
        msg  += f"\n*💵 DÓLAR:* R$ {d['valor']:.4f} ({sinal}{d['variacao']:.2f}%)\n"
        msg  += "_Ref.: Dólar Comercial (USDBRL=X)_\n"

    msg += f"\n*🤖 Análise do Dia:*\n{resumo_ia}\n"

    msg += (
        "\n_ℹ️ Cotações via Yahoo Finance (último preço negociado). "
        "Para negociação, consulte sua cooperativa ou corretor._\n"
    )
    msg += "\n_AgroPulse AI — Informação que vale dinheiro_ 💰"

    return msg


# ============================================================
# ENVIO WHATSAPP (Z-API)
# ============================================================
def enviar_whatsapp_zapi(numero: str, mensagem: str) -> tuple[int, dict]:
    url     = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {
        "Content-Type": "application/json",
        "Client-Token": ZAPI_CLIENT_TOKEN,
    }
    payload = {"phone": numero, "message": mensagem}
    resp    = requests.post(url, headers=headers, json=payload, timeout=15)
    return resp.status_code, resp.json()


def enviar_whatsapp(mensagem: str):
    """
    Envia mensagem para todos os produtores ativos.

    JANELA DE HORÁRIO: 08:00–21:00 BRT.
    A verificação de janela acontece APENAS no início da função.
    Se o envio começar dentro da janela (ex: 19:00) e a fila ainda
    estiver em andamento após 21:00, ela continua normalmente até
    o último produtor. NÃO interromper uma fila já iniciada porque
    isso deixaria produtores sem o relatório do dia.

    DELAY: 45–75 segundos aleatórios entre mensagens — obrigatório
    para reduzir risco de bloqueio/banimento na API de WhatsApp.
    NÃO reduzir este intervalo para tentar caber antes das 21:00.
    """
    agora_br = datetime.now(TZ_BRASIL)
    hora_brt = agora_br.hour

    if hora_brt < 8 or hora_brt >= 21:
        print(f"⏰ Fora do horário permitido ({hora_brt}h BRT). Envio cancelado.")
        registrar_log("Envio cancelado", f"Fora do horário ({hora_brt}h BRT)")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("SELECT nome, whatsapp FROM produtores WHERE ativo=1")
        produtores = [{"nome": r[0], "whatsapp": r[1]} for r in c.fetchall()]
        conn.close()
    except Exception as e:
        print(f"❌ Erro ao buscar produtores: {e}")
        registrar_log("Erro busca produtores", str(e))
        return

    total    = len(produtores)
    enviados = 0
    falhas   = 0

    print(f"\n📤 Iniciando envio para {total} produtores — {agora_br.strftime('%H:%M:%S BRT')}")

    for i, usuario in enumerate(produtores):
        try:
            numero = (
                usuario["whatsapp"].strip()
                .replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
            )
            if not numero.startswith("55"):
                numero = "55" + numero

            status, resp = enviar_whatsapp_zapi(numero, mensagem)

            if status == 200:
                enviados += 1
                print(f"✅ [{i+1}/{total}] {usuario['nome']} ({numero})")
                registrar_log("Mensagem enviada", f"{usuario['nome']} | {numero}")
                try:
                    conn = sqlite3.connect(DB_PATH)
                    c    = conn.cursor()
                    c.execute(
                        "UPDATE produtores SET mensagens_enviadas = mensagens_enviadas + 1 "
                        "WHERE whatsapp=?",
                        (usuario["whatsapp"],)
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            else:
                falhas += 1
                print(f"❌ [{i+1}/{total}] {usuario['nome']}: {resp}")
                registrar_log(
                    "Falha no envio",
                    f"{usuario['nome']} | HTTP {status} | {str(resp)[:100]}"
                )

            if i < total - 1:
                delay = random.uniform(45, 75)
                print(f"⏳ Aguardando {delay:.0f}s...")
                time.sleep(delay)

        except Exception as e:
            falhas += 1
            print(f"❌ Erro: {usuario['nome']}: {e}")
            registrar_log("Erro no envio", f"{usuario['nome']} | {str(e)}")

    registrar_log(
        "Envio concluído",
        f"Total={total} | Enviados={enviados} | Falhas={falhas}"
    )
    print(f"\n📊 Concluído: {enviados} enviados, {falhas} falhas")


# ============================================================
# PIPELINE PRINCIPAL
# ============================================================
def executar_pipeline():
    """
    Executa o pipeline completo de coleta, validação e envio.
    Chamado pelo loop de agendamento quando as condições BRT são satisfeitas.
    """
    agora_br   = datetime.now(TZ_BRASIL)
    nomes_dias = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    dia_nome   = nomes_dias[agora_br.weekday()]

    inicio_str = agora_br.strftime("%d/%m/%Y %H:%M:%S BRT")
    hoje_brt   = agora_br.date()

    print(f"\n{'='*55}")
    print(f"🚀 Pipeline iniciado — {inicio_str} ({dia_nome})")
    print(f"{'='*55}")
    registrar_log("Pipeline iniciado", f"{inicio_str} ({dia_nome})")

    # 1. Coleta
    try:
        precos = buscar_precos()
    except ValueError as e:
        print(f"❌ Coleta falhou: {e}")
        registrar_log("Pipeline cancelado — coleta", str(e))
        # envio_iniciado=False → cancelamento pré-envio → retry permitido
        registrar_execucao(hoje_brt, "cancelado", envio_iniciado=False)
        return

    # 2. Validação
    ok, erros = validar_precos(precos)
    if not ok:
        print(f"❌ Validação crítica falhou: {erros}")
        registrar_log("Pipeline cancelado — validação", str(erros))
        # envio_iniciado=False → cancelamento pré-envio → retry permitido
        registrar_execucao(hoje_brt, "cancelado", envio_iniciado=False)
        return

    # 3. Aguardar 19:00 BRT para envio
    while True:
        agora_br = datetime.now(TZ_BRASIL)
        if agora_br.hour >= 19:
            break
        print(f"⏳ Aguardando 19:00 BRT... ({agora_br.strftime('%H:%M BRT')})")
        time.sleep(60)

    hora_envio = datetime.now(TZ_BRASIL).strftime("%H:%M:%S BRT")
    print(f"✅ Horário de envio atingido — {hora_envio}")

    # Gravar envio_iniciado=True ANTES de chamar enviar_whatsapp.
    # A partir deste ponto, qualquer falha ou reinício NÃO deve
    # repetir o envio automaticamente — exige intervenção manual.
    registrar_execucao(hoje_brt, "iniciado", envio_iniciado=True)
    registrar_log("Envio iniciado", hora_envio)

    # 4. Análise IA
    try:
        resumo = gerar_resumo_ia(precos)
    except Exception as e:
        resumo = "Análise indisponível no momento."
        registrar_log("Erro análise IA", str(e))

    # 5. Montar e enviar
    mensagem = montar_mensagem(precos, resumo)
    registrar_log("Mensagem montada", f"{len(mensagem)} caracteres")
    enviar_whatsapp(mensagem)

    registrar_execucao(hoje_brt, "concluido", envio_iniciado=True)
    registrar_log(
        "Pipeline finalizado",
        datetime.now(TZ_BRASIL).strftime("%d/%m/%Y %H:%M:%S BRT")
    )


# ============================================================
# AGENDAMENTO BASEADO EM TZ_BRASIL COM PERSISTÊNCIA SQLite
# ============================================================
def loop_agendamento():
    """
    Loop principal de agendamento. Independente do timezone do servidor.

    Critérios de disparo (todos devem ser verdadeiros):
    - Dia da semana: segunda a sexta (0–4)
    - Hora BRT >= 18:30
    - `tentar_iniciar_execucao()` retorna True (lock exclusivo obtido)

    Comportamento após reinício do servidor:
    - Antes das 18:30 → aguarda normalmente.
    - Após 18:30, sem registro do dia → executa (recuperação).
    - Após 18:30, status='concluido' → não executa.
    - Após 18:30, status='iniciado' → não executa (pode haver processo ativo).
    - Após 18:30, status='cancelado' + envio_iniciado=0 → retry autorizado.
    - Após 18:30, status='cancelado' + envio_iniciado=1 → bloqueia (exige manual).

    Proteção anti-duplicidade:
    - `tentar_iniciar_execucao()` usa BEGIN EXCLUSIVE — atômico.
    - Dois processos simultâneos: apenas o primeiro obtém o lock.
    """
    _garantir_tabela_execucoes()

    agora_inicio = datetime.now(TZ_BRASIL).strftime("%d/%m/%Y %H:%M:%S BRT")
    print(f"⏳ Loop de agendamento iniciado — {agora_inicio}")
    print("📅 Disparo: 18:30 BRT | Envio: 19:00 BRT | Dias: segunda a sexta")
    print("💾 Controle de execução única persistido em SQLite (tabela execucoes_diarias)")

    while True:
        try:
            agora_br   = datetime.now(TZ_BRASIL)
            hoje       = agora_br.date()
            dia_semana = agora_br.weekday()  # 0=seg … 6=dom

            # Ignorar fins de semana
            if dia_semana >= 5:
                time.sleep(30)
                continue

            # Verificar se chegou às 18:30 BRT
            hora    = agora_br.hour
            minuto  = agora_br.minute
            apos_18h30 = (hora == 18 and minuto >= 30) or hora > 18

            if not apos_18h30:
                time.sleep(30)
                continue

            # Tentar registrar 'iniciado' atomicamente.
            # tentar_iniciar_execucao() usa BEGIN EXCLUSIVE para eliminar
            # race condition entre dois processos. Retorna True somente se
            # esta instância conseguiu o lock e o status permite execução.
            if not tentar_iniciar_execucao(hoje):
                time.sleep(30)
                continue

            registrar_log(
                "Agendamento disparado",
                f"{agora_br.strftime('%d/%m/%Y %H:%M:%S BRT')} — "
                f"TZ explícito America/Sao_Paulo | Lock SQLite exclusivo obtido"
            )
            executar_pipeline()

        except Exception as e:
            print(f"⚠️ Erro no loop de agendamento: {e}")
            registrar_log("Erro loop agendamento", str(e))

        time.sleep(30)


# ============================================================
# ENTRADA PRINCIPAL
# ============================================================
if __name__ == "__main__":
    print("🚀 AgroPulse v2.5 iniciado!")
    print(f"🕐 Horário de referência: {datetime.now(TZ_BRASIL).strftime('%d/%m/%Y %H:%M:%S BRT')}")
    print("ℹ️  Agendamento via loop BRT — independente do timezone do servidor")
    loop_agendamento()
