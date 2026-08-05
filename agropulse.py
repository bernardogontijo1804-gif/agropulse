"""
AgroPulse — Sistema de Relatórios de Mercado Agrícola
======================================================
Versão: 2.0 (Produção)
Auditoria completa conforme especificação técnica.

Horário de coleta : 18:30 (Brasília)
Horário de envio  : 19:00 (Brasília)
"""

import anthropic
import requests
import schedule
import time
import random
import sqlite3
import os
import json
from datetime import datetime
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

# ============================================================
# CONSTANTES DE CONVERSÃO (auditadas)
# ============================================================
# 1 bushel de soja  = 27.2155 kg  →  saca 60 kg = 60 / 27.2155 bushels
# 1 bushel de milho = 25.4012 kg  →  saca 60 kg = 60 / 25.4012 bushels
# 1 bushel de trigo = 27.2155 kg  →  saca 60 kg = 60 / 27.2155 bushels
# Soja, Milho, Trigo cotados em cents/bushel na CBOT → dividir por 100 para USD/bushel
# Café cotado em cents/libra na ICE → 1 libra = 0.453592 kg → saca 60 kg = 60/0.453592 libras / 100 cents
# Algodão cotado em cents/libra na ICE

SOJA_KG_POR_BUSHEL   = 27.2155
MILHO_KG_POR_BUSHEL  = 25.4012
TRIGO_KG_POR_BUSHEL  = 27.2155
CAFE_KG_POR_LIBRA    = 0.453592
SACA_KG              = 60.0

# Prêmios de porto (basis) — diferencial médio histórico em USD/bushel
# Paranaguá ≈ +30 cents, Tubarão ≈ +20 cents, Barcarena ≈ +15 cents, São Luís ≈ +12 cents
PREMIOS_SOJA = {
    "Paranagua": 0.30,
    "Tubarao":   0.20,
    "Barcarena": 0.15,
    "Sao Luis":  0.12,
}
PREMIOS_MILHO = {
    "Paranagua": 0.25,
    "Tubarao":   0.18,
    "Barcarena": 0.12,
    "Sao Luis":  0.10,
}

# ============================================================
# APP FLASK (webhook Meta)
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
    """Registra evento no banco com timestamp."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)", (evento, detalhes))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Erro ao registrar log: {e}")


# ============================================================
# COLETA DE DADOS — yfinance com retry
# ============================================================
def buscar_ticker(simbolo: str, tentativas: int = 3, delay_s: float = 2.0) -> dict | None:
    """
    Busca dados de fechamento para um símbolo.
    Retorna dict com valor_atual, valor_anterior, variacao, maxima, minima
    ou None se falhar após todas as tentativas.
    """
    import yfinance as yf

    for tentativa in range(1, tentativas + 1):
        try:
            time.sleep(delay_s)
            ticker = yf.Ticker(simbolo)
            hist = ticker.history(period="5d")
            hist = hist.dropna(subset=["Close"])

            if len(hist) < 2:
                print(f"⚠️ {simbolo}: dados insuficientes (tentativa {tentativa}/{tentativas})")
                continue

            atual    = float(hist["Close"].iloc[-1])
            anterior = float(hist["Close"].iloc[-2])
            maxima   = float(hist["High"].iloc[-1])
            minima   = float(hist["Low"].iloc[-1])

            # Validações
            if atual <= 0 or anterior <= 0:
                print(f"⚠️ {simbolo}: preço inválido (atual={atual}, anterior={anterior})")
                continue

            # Variação calculada internamente — nunca confiamos na API
            variacao = ((atual - anterior) / anterior) * 100

            # Sanidade: variação acima de 20% em um dia é suspeita
            if abs(variacao) > 20:
                print(f"⚠️ {simbolo}: variação suspeita ({variacao:.2f}%) — verificando...")
                # Não bloqueamos, apenas alertamos

            return {
                "valor_raw":   atual,
                "anterior_raw":anterior,
                "variacao":    round(variacao, 2),
                "maxima_raw":  maxima,
                "minima_raw":  minima,
            }

        except Exception as e:
            print(f"⚠️ {simbolo} tentativa {tentativa}/{tentativas}: {e}")
            if tentativa < tentativas:
                time.sleep(delay_s * tentativa)

    return None


def buscar_precos() -> dict:
    """
    Coleta todos os preços de mercado.
    Retorna dicionário com todos os ativos validados.
    Registra log detalhado de cada coleta.
    """
    print(f"\n{'='*50}")
    print(f"🔄 Iniciando coleta — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'='*50}")

    # ----------------------------------------------------------
    # CBOT: Soja, Milho, Trigo (cents/bushel)
    # ICE:  Café (cents/libra), Algodão (cents/libra)
    # NYMEX/ICE: Petróleo WTI (USD/barril), Brent (USD/barril)
    # FOREX: Dólar (BRL/USD)
    # ----------------------------------------------------------
    simbolos = {
        "Soja":          ("ZS=F",  "CBOT", "cents/bushel"),
        "Milho":         ("ZC=F",  "CBOT", "cents/bushel"),
        "Trigo":         ("ZW=F",  "CBOT", "cents/bushel"),
        "Cafe":          ("KC=F",  "ICE",  "cents/libra"),
        "Algodao":       ("CT=F",  "ICE",  "cents/libra"),
        "Petroleo WTI":  ("CL=F",  "NYMEX","USD/barril"),
        "Petroleo Brent":("BZ=F",  "ICE",  "USD/barril"),
        "Dolar":         ("BRL=X", "FOREX","BRL/USD"),
    }

    precos_raw = {}

    for nome, (simbolo, bolsa, unidade) in simbolos.items():
        dados = buscar_ticker(simbolo, tentativas=3, delay_s=2.0)
        if dados:
            precos_raw[nome] = dados
            registrar_log(
                f"Coleta OK — {nome}",
                f"Bolsa={bolsa} | Simbolo={simbolo} | "
                f"Atual={dados['valor_raw']:.4f} | "
                f"Anterior={dados['anterior_raw']:.4f} | "
                f"Variacao={dados['variacao']:.2f}%"
            )
            print(f"✅ {nome} ({bolsa}): {dados['valor_raw']:.4f} "
                  f"({'+' if dados['variacao'] > 0 else ''}{dados['variacao']:.2f}%)")
        else:
            registrar_log(f"Coleta FALHOU — {nome}", f"Simbolo={simbolo} | Tentativas esgotadas")
            print(f"❌ {nome}: falha na coleta")

    # ----------------------------------------------------------
    # VALIDAÇÃO MÍNIMA
    # Precisamos de pelo menos Soja, Milho e Dólar para gerar portos
    # ----------------------------------------------------------
    essenciais = ["Soja", "Milho", "Dolar"]
    faltando = [e for e in essenciais if e not in precos_raw]
    if faltando:
        raise ValueError(f"Dados essenciais ausentes: {faltando}. Relatório cancelado.")

    # ----------------------------------------------------------
    # CONSTRUÇÃO DO DICIONÁRIO FINAL
    # ----------------------------------------------------------
    precos = {}

    dolar_brl = precos_raw["Dolar"]["valor_raw"]   # BRL por USD

    # --- SOJA (CBOT, cents/bushel → USD/bushel → R$/saca 60kg) ---
    if "Soja" in precos_raw:
        r = precos_raw["Soja"]
        atual_usd    = r["valor_raw"]    / 100   # USD/bushel
        anterior_usd = r["anterior_raw"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos["Soja"] = {
            "valor":    round(atual_usd, 4),
            "anterior": round(anterior_usd, 4),
            "variacao": variacao,
            "unidade":  "USD/bushel",
        }
        # Portos: (Chicago + prêmio) × (60 / kg_por_bushel) × dólar
        for porto, premio_usd in PREMIOS_SOJA.items():
            preco_porto_usd  = atual_usd + premio_usd
            preco_porto_brl  = (preco_porto_usd / SOJA_KG_POR_BUSHEL) * SACA_KG * dolar_brl
            anterior_porto_usd = anterior_usd + premio_usd
            anterior_porto_brl = (anterior_porto_usd / SOJA_KG_POR_BUSHEL) * SACA_KG * dolar_brl
            var_porto = round(((preco_porto_brl - anterior_porto_brl) / anterior_porto_brl) * 100, 2)
            precos[f"Soja {porto}"] = {
                "valor":    round(preco_porto_brl, 2),
                "anterior": round(anterior_porto_brl, 2),
                "variacao": var_porto,
                "unidade":  "R$/saca",
            }

    # --- MILHO (CBOT, cents/bushel → USD/bushel → R$/saca 60kg) ---
    if "Milho" in precos_raw:
        r = precos_raw["Milho"]
        atual_usd    = r["valor_raw"]    / 100
        anterior_usd = r["anterior_raw"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos["Milho"] = {
            "valor":    round(atual_usd, 4),
            "anterior": round(anterior_usd, 4),
            "variacao": variacao,
            "unidade":  "USD/bushel",
        }
        for porto, premio_usd in PREMIOS_MILHO.items():
            preco_porto_usd  = atual_usd + premio_usd
            preco_porto_brl  = (preco_porto_usd / MILHO_KG_POR_BUSHEL) * SACA_KG * dolar_brl
            anterior_porto_usd = anterior_usd + premio_usd
            anterior_porto_brl = (anterior_porto_usd / MILHO_KG_POR_BUSHEL) * SACA_KG * dolar_brl
            var_porto = round(((preco_porto_brl - anterior_porto_brl) / anterior_porto_brl) * 100, 2)
            precos[f"Milho {porto}"] = {
                "valor":    round(preco_porto_brl, 2),
                "anterior": round(anterior_porto_brl, 2),
                "variacao": var_porto,
                "unidade":  "R$/saca",
            }
        # Sorgo = 85% do milho (estimativa de mercado — sem contrato próprio no CBOT)
        for porto in PREMIOS_MILHO:
            milho_porto = precos.get(f"Milho {porto}")
            if milho_porto:
                sorgo_atual    = milho_porto["valor"]    * 0.85
                sorgo_anterior = milho_porto["anterior"] * 0.85
                var_sorgo = round(((sorgo_atual - sorgo_anterior) / sorgo_anterior) * 100, 2)
                precos[f"Sorgo {porto}"] = {
                    "valor":    round(sorgo_atual, 2),
                    "anterior": round(sorgo_anterior, 2),
                    "variacao": var_sorgo,
                    "unidade":  "R$/saca (est.)",
                }

    # --- TRIGO (CBOT, cents/bushel → USD/bushel) ---
    if "Trigo" in precos_raw:
        r = precos_raw["Trigo"]
        atual_usd    = r["valor_raw"]    / 100
        anterior_usd = r["anterior_raw"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos["Trigo"] = {
            "valor":    round(atual_usd, 4),
            "anterior": round(anterior_usd, 4),
            "variacao": variacao,
            "unidade":  "USD/bushel",
        }

    # --- CAFÉ (ICE, cents/libra → USD/libra) ---
    if "Cafe" in precos_raw:
        r = precos_raw["Cafe"]
        atual_usd    = r["valor_raw"]    / 100
        anterior_usd = r["anterior_raw"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos["Cafe"] = {
            "valor":    round(atual_usd, 4),
            "anterior": round(anterior_usd, 4),
            "variacao": variacao,
            "unidade":  "USD/libra (ICE)",
        }

    # --- ALGODÃO (ICE, cents/libra → USD/libra) ---
    if "Algodao" in precos_raw:
        r = precos_raw["Algodao"]
        atual_usd    = r["valor_raw"]    / 100
        anterior_usd = r["anterior_raw"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos["Algodao"] = {
            "valor":    round(atual_usd, 4),
            "anterior": round(anterior_usd, 4),
            "variacao": variacao,
            "unidade":  "USD/libra (ICE)",
        }

    # --- PETRÓLEO WTI (NYMEX, USD/barril) ---
    if "Petroleo WTI" in precos_raw:
        r = precos_raw["Petroleo WTI"]
        atual    = r["valor_raw"]
        anterior = r["anterior_raw"]
        variacao = round(((atual - anterior) / anterior) * 100, 2)
        precos["Petroleo WTI"] = {
            "valor":    round(atual, 2),
            "anterior": round(anterior, 2),
            "variacao": variacao,
            "unidade":  "USD/barril",
        }

    # --- PETRÓLEO BRENT (ICE, USD/barril) ---
    if "Petroleo Brent" in precos_raw:
        r = precos_raw["Petroleo Brent"]
        atual    = r["valor_raw"]
        anterior = r["anterior_raw"]
        variacao = round(((atual - anterior) / anterior) * 100, 2)
        precos["Petroleo Brent"] = {
            "valor":    round(atual, 2),
            "anterior": round(anterior, 2),
            "variacao": variacao,
            "unidade":  "USD/barril",
        }

    # --- DÓLAR (FOREX, BRL/USD) ---
    if "Dolar" in precos_raw:
        r = precos_raw["Dolar"]
        atual    = r["valor_raw"]
        anterior = r["anterior_raw"]
        variacao = round(((atual - anterior) / anterior) * 100, 2)
        precos["Dolar"] = {
            "valor":    round(atual, 4),
            "anterior": round(anterior, 4),
            "variacao": variacao,
            "unidade":  "BRL/USD",
        }

    print(f"\n✅ Coleta concluída — {len(precos)} ativos processados")
    return precos


# ============================================================
# VALIDAÇÃO DOS DADOS
# ============================================================
def validar_precos(precos: dict) -> tuple[bool, list]:
    """
    Valida consistência dos dados antes do envio.
    Retorna (True, []) se OK, ou (False, [erros]) se falhar.
    """
    erros = []

    for nome, dados in precos.items():
        valor    = dados.get("valor", 0)
        variacao = dados.get("variacao", 0)

        if valor <= 0:
            erros.append(f"{nome}: preço inválido ({valor})")

        if abs(variacao) > 25:
            erros.append(f"{nome}: variação suspeita ({variacao:.2f}%)")

    essenciais = ["Soja", "Milho", "Dolar", "Petroleo WTI"]
    for e in essenciais:
        if e not in precos:
            erros.append(f"{e}: ativo essencial ausente")

    if erros:
        for erro in erros:
            registrar_log("Validação FALHOU", erro)
            print(f"❌ Validação: {erro}")
        return False, erros

    registrar_log("Validação OK", f"{len(precos)} ativos validados")
    return True, []


# ============================================================
# GERAÇÃO DO RESUMO COM IA
# ============================================================
def gerar_resumo_ia(precos: dict) -> str:
    """Gera análise de mercado usando Claude."""
    cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    linhas = []
    for nome, dados in precos.items():
        if "Porto" not in nome and "Sorgo" not in nome:
            sinal = "+" if dados["variacao"] > 0 else ""
            linhas.append(
                f"{nome}: {dados['valor']} {dados.get('unidade','')} "
                f"({sinal}{dados['variacao']:.2f}%)"
            )

    texto_precos = "\n".join(linhas)

    resposta = cliente.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=350,
        messages=[{
            "role": "user",
            "content": f"""Você é um analista sênior do agronegócio brasileiro.

Com base nos dados de fechamento de mercado abaixo, escreva uma análise
de 3 frases objetivas e diretas para produtores rurais e profissionais do agro.

A análise deve:
1. Destacar os maiores movimentos do dia (altas e baixas)
2. Explicar o impacto do dólar e do petróleo para o produtor
3. Indicar o que isso significa para exportadores e produtores

Dados de fechamento:
{texto_precos}

Regras:
- Escreva em português claro e acessível
- Não use markdown, asteriscos ou formatação especial
- Não invente dados que não estejam acima
- Seja preciso e objetivo"""
        }]
    )
    return resposta.content[0].text.strip()


# ============================================================
# MONTAGEM DA MENSAGEM
# ============================================================
def montar_mensagem(precos: dict, resumo_ia: str) -> str:
    """Monta o relatório final para WhatsApp."""
    data_hoje = datetime.now().strftime("%d/%m/%Y")

    def linha(nome, prefixo="US$", casas=2):
        if nome not in precos:
            return ""
        d     = precos[nome]
        emoji = "📈" if d["variacao"] > 0 else "📉"
        sinal = "+" if d["variacao"] > 0 else ""
        fmt   = f".{casas}f"
        return f"{emoji} *{nome}:* {prefixo} {d['valor']:{fmt}} ({sinal}{d['variacao']:.2f}%)\n"

    msg = f"🌾 *AGROPULSE — Fechamento do Mercado*\n📅 {data_hoje}\n"

    # CBOT
    msg += "\n*📊 BOLSA DE CHICAGO (CBOT)*\n"
    for nome in ["Soja", "Milho", "Trigo"]:
        msg += linha(nome, "US$", 2)

    # ICE
    msg += "\n*🧋 ICE (Nova York)*\n"
    for nome in ["Cafe", "Algodao"]:
        msg += linha(nome, "US$", 2)

    # Petróleo
    msg += "\n*🛢️ PETRÓLEO*\n"
    for nome in ["Petroleo WTI", "Petroleo Brent"]:
        msg += linha(nome, "US$", 2)

    # Dólar
    if "Dolar" in precos:
        d     = precos["Dolar"]
        emoji = "📈" if d["variacao"] > 0 else "📉"
        sinal = "+" if d["variacao"] > 0 else ""
        msg  += f"\n*💵 DÓLAR:* R$ {d['valor']:.4f} ({sinal}{d['variacao']:.2f}%)\n"

    # Portos — agrupados por cidade
    msg += "\n*🚢 PORTOS BRASILEIROS (R$/saca)*\n"
    portos    = ["Paranagua", "Tubarao", "Barcarena", "Sao Luis"]
    culturas  = [("Soja", "🌱"), ("Milho", "🌽"), ("Sorgo", "🌾")]

    for porto in portos:
        linhas_porto = []
        for cultura, icone in culturas:
            chave = f"{cultura} {porto}"
            if chave in precos:
                d     = precos[chave]
                emoji = "📈" if d["variacao"] > 0 else "📉"
                sinal = "+" if d["variacao"] > 0 else ""
                linhas_porto.append(
                    f"  {emoji} {icone} {cultura}: R$ {d['valor']:.2f}/sc ({sinal}{d['variacao']:.2f}%)"
                )
        if linhas_porto:
            msg += f"\n📍 *{porto}*\n" + "\n".join(linhas_porto) + "\n"

    # Análise
    msg += f"\n*🤖 Análise do Dia:*\n{resumo_ia}\n"
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
    """Envia relatório para todos os produtores ativos com delay anti-ban."""

    # Horário permitido: 8h–20h (proteção anti-ban)
    hora = datetime.now().hour
    if hora < 8 or hora >= 20:
        print(f"⏰ Fora do horário permitido ({hora}h). Envio cancelado.")
        registrar_log("Envio cancelado", f"Fora do horário ({hora}h)")
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
    hora_inicio = datetime.now().strftime("%H:%M:%S")

    print(f"\n📤 Iniciando envio para {total} produtores — {hora_inicio}")

    for i, usuario in enumerate(produtores):
        try:
            numero = usuario["whatsapp"].strip().replace(" ","").replace("-","").replace("(","").replace(")","")
            if not numero.startswith("55"):
                numero = "55" + numero

            status, resp = enviar_whatsapp_zapi(numero, mensagem)

            if status == 200:
                enviados += 1
                print(f"✅ [{i+1}/{total}] {usuario['nome']} ({numero})")
                registrar_log("Mensagem enviada", f"{usuario['nome']} | {numero} | {hora_inicio}")
                # Atualiza contador
                try:
                    conn = sqlite3.connect(DB_PATH)
                    c    = conn.cursor()
                    c.execute("UPDATE produtores SET mensagens_enviadas = mensagens_enviadas + 1 WHERE whatsapp=?",
                              (usuario["whatsapp"],))
                    conn.commit()
                    conn.close()
                except:
                    pass
            else:
                falhas += 1
                print(f"❌ [{i+1}/{total}] {usuario['nome']}: {resp}")
                registrar_log("Falha no envio", f"{usuario['nome']} | {numero} | HTTP {status} | {str(resp)[:100]}")

            # Delay aleatório anti-ban entre envios
            if i < total - 1:
                delay = random.uniform(10, 18)
                print(f"⏳ Aguardando {delay:.1f}s...")
                time.sleep(delay)

        except Exception as e:
            falhas += 1
            print(f"❌ Erro ao enviar para {usuario['nome']}: {e}")
            registrar_log("Erro no envio", f"{usuario['nome']} | {str(e)}")

    registrar_log(
        "Envio concluído",
        f"Total={total} | Enviados={enviados} | Falhas={falhas} | Início={hora_inicio}"
    )
    print(f"\n📊 Concluído: {enviados} enviados, {falhas} falhas")


# ============================================================
# FUNÇÃO PRINCIPAL — com retry e validação
# ============================================================
def enviar_relatorio():
    """
    Pipeline completo:
    1. Coleta dados (com retry)
    2. Valida dados
    3. Gera análise IA
    4. Monta mensagem
    5. Envia após 19:00
    """
    inicio = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"🚀 Pipeline iniciado — {inicio}")
    print(f"{'='*50}")

    registrar_log("Pipeline iniciado", inicio)

    # 1. Coleta
    try:
        precos = buscar_precos()
    except ValueError as e:
        print(f"❌ Coleta falhou: {e}")
        registrar_log("Pipeline cancelado", str(e))
        return

    # 2. Validação
    ok, erros = validar_precos(precos)
    if not ok:
        print(f"❌ Validação falhou: {erros}")
        registrar_log("Pipeline cancelado — validação", str(erros))
        return

    # 3. Aguardar 19:00 se ainda for cedo
    # Aguarda 22h UTC = 19h Brasília
    while datetime.now().hour < 22:
        print(f"⏳ Aguardando 19:00 para envio... (agora: {datetime.now().strftime('%H:%M')})")
        time.sleep(60)

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

    registrar_log("Pipeline finalizado", datetime.now().strftime("%d/%m/%Y %H:%M:%S"))


# ============================================================
# AGENDAMENTO
# ============================================================
if __name__ == "__main__":
    print("🚀 AgroPulse v2.0 iniciado!")
    # Coleta às 18:30, envio após 19:00
    schedule.every().day.at("21:30").do(enviar_relatorio)  # 21:30 UTC = 18:30 Brasília
    print("⏰ Agendado: coleta às 18:30 Brasília (21:30 UTC), envio após 19:00 Brasília (22:00 UTC)")
    print("✋ Pressione CTRL+C para parar")
    while True:
        schedule.run_pending()
        time.sleep(30)
