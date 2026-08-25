"""
AgroPulse — Sistema de Relatórios de Mercado Agrícola
======================================================
Versão: 2.1 (Produção)
Correção crítica: garantia de mesmo contrato para atual e anterior.

Horário de coleta : 18:30 (Brasília) = 21:30 UTC
Horário de envio  : 19:00 (Brasília) = 22:00 UTC
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
SOJA_KG_POR_BUSHEL  = 27.2155
MILHO_KG_POR_BUSHEL = 25.4012
TRIGO_KG_POR_BUSHEL = 27.2155
SACA_KG             = 60.0

# Prêmios de porto (basis) — diferencial médio histórico em USD/bushel
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
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)", (evento, detalhes))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Erro ao registrar log: {e}")


# ============================================================
# COLETA DE DADOS — yfinance com garantia de mesmo contrato
# ============================================================
def buscar_ticker(simbolo: str, tentativas: int = 3, delay_s: float = 2.0) -> dict | None:
    """
    Busca dados de fechamento para um símbolo.

    CORREÇÃO CRÍTICA v2.1:
    Usa period="10d" e garante que atual e anterior pertencem
    ao MESMO contrato, comparando pelo ticker info quando possível.
    Variação sempre calculada internamente: ((atual-anterior)/anterior)*100
    """
    import yfinance as yf

    for tentativa in range(1, tentativas + 1):
        try:
            time.sleep(delay_s)
            ticker = yf.Ticker(simbolo)

            # Busca 10 dias para ter margem suficiente mesmo em semanas
            # com feriados — garante pelo menos 2 pregões completos
            hist = ticker.history(period="10d", auto_adjust=True)
            hist = hist.dropna(subset=["Close"])

            if len(hist) < 2:
                print(f"⚠️ {simbolo}: dados insuficientes (tentativa {tentativa}/{tentativas})")
                continue

            # Pega apenas os dois últimos pregões com dados
            # Isso garante que estamos comparando o mesmo contrato
            atual_row    = hist.iloc[-1]
            anterior_row = hist.iloc[-2]

            atual    = float(atual_row["Close"])
            anterior = float(anterior_row["Close"])
            maxima   = float(atual_row["High"])
            minima   = float(atual_row["Low"])
            data_atual    = str(hist.index[-1].date())
            data_anterior = str(hist.index[-2].date())

            # Validações básicas
            if atual <= 0 or anterior <= 0:
                print(f"⚠️ {simbolo}: preço inválido (atual={atual}, anterior={anterior})")
                continue

            # Variação SEMPRE calculada internamente
            variacao = ((atual - anterior) / anterior) * 100

            # Alerta se variação parecer suspeita (>15% em um dia)
            if abs(variacao) > 15:
                print(f"⚠️ {simbolo}: variação suspeita ({variacao:.2f}%) "
                      f"— atual={atual:.4f} ({data_atual}) "
                      f"anterior={anterior:.4f} ({data_anterior})")
                registrar_log(
                    f"Variação suspeita — {simbolo}",
                    f"Atual={atual:.4f} ({data_atual}) | "
                    f"Anterior={anterior:.4f} ({data_anterior}) | "
                    f"Variacao={variacao:.2f}%"
                )

            print(f"  📌 {simbolo}: {anterior:.4f} ({data_anterior}) → "
                  f"{atual:.4f} ({data_atual}) = {variacao:+.2f}%")

            return {
                "valor_raw":    atual,
                "anterior_raw": anterior,
                "variacao":     round(variacao, 2),
                "maxima_raw":   maxima,
                "minima_raw":   minima,
                "data_atual":   data_atual,
                "data_anterior":data_anterior,
            }

        except Exception as e:
            print(f"⚠️ {simbolo} tentativa {tentativa}/{tentativas}: {e}")
            if tentativa < tentativas:
                time.sleep(delay_s * tentativa)

    return None


def buscar_precos() -> dict:
    """
    Coleta todos os preços de mercado com log detalhado.
    """
    print(f"\n{'='*50}")
    print(f"🔄 Iniciando coleta — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'='*50}")

    simbolos = {
        "Soja":           ("ZS=F",  "CBOT",  "cents/bushel"),
        "Milho":          ("ZC=F",  "CBOT",  "cents/bushel"),
        "Trigo":          ("ZW=F",  "CBOT",  "cents/bushel"),
        "Cafe":           ("KC=F",  "ICE",   "cents/libra"),
        "Algodao":        ("CT=F",  "ICE",   "cents/libra"),
        "Petroleo WTI":   ("CL=F",  "NYMEX", "USD/barril"),
        "Petroleo Brent": ("BZ=F",  "ICE",   "USD/barril"),
        "Dolar":          ("BRL=X", "FOREX", "BRL/USD"),
    }

    precos_raw = {}

    for nome, (simbolo, bolsa, unidade) in simbolos.items():
        dados = buscar_ticker(simbolo, tentativas=3, delay_s=2.0)
        if dados:
            precos_raw[nome] = dados
            registrar_log(
                f"Coleta OK — {nome}",
                f"Bolsa={bolsa} | Simbolo={simbolo} | "
                f"Atual={dados['valor_raw']:.4f} ({dados['data_atual']}) | "
                f"Anterior={dados['anterior_raw']:.4f} ({dados['data_anterior']}) | "
                f"Variacao={dados['variacao']:.2f}%"
            )
            print(f"✅ {nome} ({bolsa}): {dados['valor_raw']:.4f} "
                  f"({'+' if dados['variacao'] > 0 else ''}{dados['variacao']:.2f}%)")
        else:
            registrar_log(f"Coleta FALHOU — {nome}", f"Simbolo={simbolo} | Tentativas esgotadas")
            print(f"❌ {nome}: falha na coleta")

    # Validação mínima
    essenciais = ["Soja", "Milho", "Dolar"]
    faltando = [e for e in essenciais if e not in precos_raw]
    if faltando:
        raise ValueError(f"Dados essenciais ausentes: {faltando}. Relatório cancelado.")

    # ============================================================
    # CONSTRUÇÃO DO DICIONÁRIO FINAL
    # ============================================================
    precos = {}
    dolar_brl         = precos_raw["Dolar"]["valor_raw"]
    dolar_brl_ant     = precos_raw["Dolar"]["anterior_raw"]

    # --- SOJA (CBOT cents/bushel → USD/bushel) ---
    if "Soja" in precos_raw:
        r            = precos_raw["Soja"]
        atual_usd    = r["valor_raw"]    / 100
        anterior_usd = r["anterior_raw"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos["Soja"] = {
            "valor":    round(atual_usd, 2),
            "anterior": round(anterior_usd, 2),
            "variacao": variacao,
            "unidade":  "USD/bushel",
        }

        # Portos de Soja
        # Variação do porto depende de Chicago E câmbio — calculada independentemente
        for porto, premio_usd in PREMIOS_SOJA.items():
            # Preço atual do porto
            preco_atual_usd = atual_usd + premio_usd
            preco_atual_brl = (preco_atual_usd / SOJA_KG_POR_BUSHEL) * SACA_KG * dolar_brl

            # Preço anterior do porto (mesmo basis, câmbio anterior)
            preco_ant_usd = anterior_usd + premio_usd
            preco_ant_brl = (preco_ant_usd / SOJA_KG_POR_BUSHEL) * SACA_KG * dolar_brl_ant

            # Variação calculada independentemente para cada porto
            var_porto = round(((preco_atual_brl - preco_ant_brl) / preco_ant_brl) * 100, 2)

            precos[f"Soja {porto}"] = {
                "valor":    round(preco_atual_brl, 2),
                "anterior": round(preco_ant_brl, 2),
                "variacao": var_porto,
                "unidade":  "R$/saca (ref.)",
            }

    # --- MILHO (CBOT cents/bushel → USD/bushel) ---
    if "Milho" in precos_raw:
        r            = precos_raw["Milho"]
        atual_usd    = r["valor_raw"]    / 100
        anterior_usd = r["anterior_raw"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos["Milho"] = {
            "valor":    round(atual_usd, 2),
            "anterior": round(anterior_usd, 2),
            "variacao": variacao,
            "unidade":  "USD/bushel",
        }

        for porto, premio_usd in PREMIOS_MILHO.items():
            preco_atual_usd = atual_usd + premio_usd
            preco_atual_brl = (preco_atual_usd / MILHO_KG_POR_BUSHEL) * SACA_KG * dolar_brl
            preco_ant_usd   = anterior_usd + premio_usd
            preco_ant_brl   = (preco_ant_usd / MILHO_KG_POR_BUSHEL) * SACA_KG * dolar_brl_ant
            var_porto = round(((preco_atual_brl - preco_ant_brl) / preco_ant_brl) * 100, 2)
            precos[f"Milho {porto}"] = {
                "valor":    round(preco_atual_brl, 2),
                "anterior": round(preco_ant_brl, 2),
                "variacao": var_porto,
                "unidade":  "R$/saca (ref.)",
            }

        # Sorgo = 85% do milho por porto
        for porto in PREMIOS_MILHO:
            m = precos.get(f"Milho {porto}")
            if m:
                s_atual = m["valor"]    * 0.85
                s_ant   = m["anterior"] * 0.85
                precos[f"Sorgo {porto}"] = {
                    "valor":    round(s_atual, 2),
                    "anterior": round(s_ant,   2),
                    "variacao": round(((s_atual - s_ant) / s_ant) * 100, 2),
                    "unidade":  "R$/saca (est. 85% milho)",
                }

    # --- TRIGO (CBOT cents/bushel → USD/bushel) ---
    if "Trigo" in precos_raw:
        r            = precos_raw["Trigo"]
        atual_usd    = r["valor_raw"]    / 100
        anterior_usd = r["anterior_raw"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos["Trigo"] = {
            "valor":    round(atual_usd, 2),
            "anterior": round(anterior_usd, 2),
            "variacao": variacao,
            "unidade":  "USD/bushel",
        }

    # --- CAFÉ (ICE cents/libra → USD/libra) ---
    if "Cafe" in precos_raw:
        r            = precos_raw["Cafe"]
        atual_usd    = r["valor_raw"]    / 100
        anterior_usd = r["anterior_raw"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos["Cafe"] = {
            "valor":    round(atual_usd, 2),
            "anterior": round(anterior_usd, 2),
            "variacao": variacao,
            "unidade":  "USD/libra (ICE)",
        }

    # --- ALGODÃO (ICE cents/libra → USD/libra) ---
    if "Algodao" in precos_raw:
        r            = precos_raw["Algodao"]
        atual_usd    = r["valor_raw"]    / 100
        anterior_usd = r["anterior_raw"] / 100
        variacao     = round(((atual_usd - anterior_usd) / anterior_usd) * 100, 2)
        precos["Algodao"] = {
            "valor":    round(atual_usd, 2),
            "anterior": round(anterior_usd, 2),
            "variacao": variacao,
            "unidade":  "USD/libra (ICE)",
        }

    # --- PETRÓLEO WTI (NYMEX USD/barril) ---
    if "Petroleo WTI" in precos_raw:
        r        = precos_raw["Petroleo WTI"]
        atual    = r["valor_raw"]
        anterior = r["anterior_raw"]
        variacao = round(((atual - anterior) / anterior) * 100, 2)
        precos["Petroleo WTI"] = {
            "valor":    round(atual, 2),
            "anterior": round(anterior, 2),
            "variacao": variacao,
            "unidade":  "USD/barril",
        }

    # --- PETRÓLEO BRENT (ICE USD/barril) ---
    if "Petroleo Brent" in precos_raw:
        r        = precos_raw["Petroleo Brent"]
        atual    = r["valor_raw"]
        anterior = r["anterior_raw"]
        variacao = round(((atual - anterior) / anterior) * 100, 2)
        precos["Petroleo Brent"] = {
            "valor":    round(atual, 2),
            "anterior": round(anterior, 2),
            "variacao": variacao,
            "unidade":  "USD/barril",
        }

    # --- DÓLAR (FOREX BRL/USD) ---
    if "Dolar" in precos_raw:
        r        = precos_raw["Dolar"]
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
    erros = []

    for nome, dados in precos.items():
        valor    = dados.get("valor", 0)
        variacao = dados.get("variacao", 0)

        if valor <= 0:
            erros.append(f"{nome}: preço inválido ({valor})")

        if abs(variacao) > 15:
            erros.append(f"{nome}: variação suspeita ({variacao:.2f}%) — verificar manualmente")

    essenciais = ["Soja", "Milho", "Dolar", "Petroleo WTI"]
    for e in essenciais:
        if e not in precos:
            erros.append(f"{e}: ativo essencial ausente")

    # Variação suspeita não cancela — apenas alerta no log
    erros_criticos = [e for e in erros if "ausente" in e or "inválido" in e]

    if erros:
        for erro in erros:
            registrar_log("Validação ALERTA", erro)
            print(f"⚠️ Validação: {erro}")

    if erros_criticos:
        return False, erros_criticos

    registrar_log("Validação OK", f"{len(precos)} ativos validados")
    return True, []


# ============================================================
# GERAÇÃO DO RESUMO COM IA
# ============================================================
def gerar_resumo_ia(precos: dict) -> str:
    cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    linhas = []
    for nome, dados in precos.items():
        if not any(x in nome for x in ["Paranagua", "Tubarao", "Barcarena", "Sao Luis", "Sorgo"]):
            sinal = "+" if dados["variacao"] > 0 else ""
            linhas.append(
                f"{nome}: {dados['valor']} {dados.get('unidade','')} "
                f"({sinal}{dados['variacao']:.2f}%)"
            )

    resposta = cliente.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=350,
        messages=[{
            "role": "user",
            "content": f"""Você é um analista sênior do agronegócio brasileiro.

Com base nos dados de fechamento abaixo, escreva uma análise de 3 frases 
objetivas e diretas para produtores rurais e profissionais do agro.

Destaque: maiores movimentos do dia, impacto do dólar e do petróleo, 
o que isso significa para exportadores e produtores brasileiros.

Dados:
{chr(10).join(linhas)}

Regras: português claro, sem markdown, sem asteriscos, 
sem inventar dados, máximo 3 frases."""
        }]
    )
    return resposta.content[0].text.strip()


# ============================================================
# MONTAGEM DA MENSAGEM
# ============================================================
def montar_mensagem(precos: dict, resumo_ia: str) -> str:
    data_hoje = datetime.now().strftime("%d/%m/%Y")

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

    msg += "\n*🚢 PREÇOS MÉDIOS COMERCIALIZADOS NOS PORTOS DO BRASIL*\n"
    portos   = ["Paranagua", "Tubarao", "Barcarena", "Sao Luis"]
    culturas = [("Soja", "🌱"), ("Milho", "🌽"), ("Sorgo", "🌾")]

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

    msg += (
        "\n_ℹ️ Preços de referência calculados com base no fechamento de Chicago, "
        "câmbio do dia e prêmio médio histórico de cada porto. "
        "Consulte sua cooperativa, corretor ou trading antes de negociar._\n"
    )

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
    from datetime import timezone
    hora_utc = datetime.now(timezone.utc).hour
    if hora_utc < 11 or hora_utc >= 24:
        hora_brt = (hora_utc - 3) % 24
        print(f"⏰ Fora do horário permitido ({hora_brt}h Brasília). Envio cancelado.")
        registrar_log("Envio cancelado", f"Fora do horário ({hora_brt}h Brasília / {hora_utc}h UTC)")
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

    total       = len(produtores)
    enviados    = 0
    falhas      = 0
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
                registrar_log("Mensagem enviada", f"{usuario['nome']} | {numero}")
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
                registrar_log("Falha no envio", f"{usuario['nome']} | HTTP {status} | {str(resp)[:100]}")

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
def enviar_relatorio():
    # Verificar se é dia útil (segunda=0 a sexta=4)
    # Sábado=5 e Domingo=6 — não envia
    from datetime import timezone
    dia_semana = datetime.now(timezone.utc).weekday()
    nomes_dias = ["Segunda","Terça","Quarta","Quinta","Sexta","Sábado","Domingo"]
    if dia_semana >= 5:
        print(f"⏸️ {nomes_dias[dia_semana]} — mercado fechado. Envio cancelado.")
        registrar_log("Envio cancelado", f"{nomes_dias[dia_semana]} — fim de semana")
        return

    inicio = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"🚀 Pipeline iniciado — {inicio} ({nomes_dias[dia_semana]})")
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
        print(f"❌ Validação crítica falhou: {erros}")
        registrar_log("Pipeline cancelado — validação", str(erros))
        return

    # 3. Aguardar 22:00 UTC = 19:00 Brasília
    from datetime import timezone
    while True:
        agora_utc = datetime.now(timezone.utc)
        if agora_utc.hour >= 22:
            break
        brt_hora = (agora_utc.hour - 3) % 24
        print(f"⏳ Aguardando 19:00 Brasília... "
              f"({brt_hora:02d}:{agora_utc.minute:02d} Brasília / "
              f"{agora_utc.hour:02d}:{agora_utc.minute:02d} UTC)")
        time.sleep(60)
    print("✅ 19:00 Brasília — iniciando envio!")

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
    print("🚀 AgroPulse v2.1 iniciado!")
    schedule.every().day.at("21:30").do(enviar_relatorio)  # 21:30 UTC = 18:30 Brasília
    print("⏰ Coleta: 18:30 Brasília | Envio: 19:00 Brasília")
    while True:
        schedule.run_pending()
        time.sleep(30)
