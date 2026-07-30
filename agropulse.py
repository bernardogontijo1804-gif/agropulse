import anthropic
import requests
import schedule
import time
import sqlite3
import os
import json
from datetime import datetime
from flask import Flask, request

# ========================================
# CONFIGURAÇÕES — todas por variável de ambiente
# ========================================
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ZAPI_INSTANCE_ID   = os.environ.get("ZAPI_INSTANCE_ID", "")
ZAPI_TOKEN         = os.environ.get("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN  = os.environ.get("ZAPI_CLIENT_TOKEN", "")
WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", "agropulse2024")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agropulse.db")

# ========================================
# APP FLASK (webhook)
# ========================================
app = Flask(__name__)

@app.route("/webhook", methods=["GET"])
def webhook_verificar():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        print("✅ Webhook verificado com sucesso pela Meta!")
        return challenge, 200
    print("❌ Falha na verificação do webhook — token incorreto.")
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook_receber():
    data = request.get_json(silent=True)
    if data:
        print(f"📩 Evento recebido da Meta: {json.dumps(data, indent=2)}")
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)",
                      ("Webhook recebido", json.dumps(data)[:200]))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Erro ao salvar log: {e}")
    return "OK", 200


# ========================================
# BUSCA DE PREÇOS
# ========================================
def buscar_precos():
    import yfinance as yf
    import time as time_module

    simbolos = {
        "Soja":          "ZS=F",
        "Milho":         "ZC=F",
        "Trigo":         "ZW=F",
        "Cafe":          "KC=F",
        "Algodao":       "CT=F",
        "Petroleo WTI":  "CL=F",
        "Petroleo Brent":"BZ=F",
        "Dolar":         "BRL=X",
    }
    precos = {}
    for nome, simbolo in simbolos.items():
        try:
            time_module.sleep(1)  # Delay de 1s entre requisições para evitar rate limit
            ticker = yf.Ticker(simbolo)
            hist   = ticker.history(period="5d")
            hist   = hist.dropna(subset=["Close"])
            if len(hist) >= 2:
                atual    = float(hist["Close"].iloc[-1])
                anterior = float(hist["Close"].iloc[-2])
                variacao = ((atual - anterior) / anterior) * 100
                precos[nome] = {
                    "valor":   round(atual, 4),
                    "variacao":round(variacao, 2),
                    "maxima":  round(float(hist["High"].iloc[-1]), 4),
                    "minima":  round(float(hist["Low"].iloc[-1]),  4),
                }
                print(f"✅ {nome}: {atual:.4f}")
            else:
                print(f"⚠️ {nome}: dados insuficientes")
        except Exception as e:
            print(f"⚠️ Erro ao buscar {nome}: {e}")

    # Dólar com 4 casas decimais
    dolar = precos.get("Dolar", {}).get("valor", 5.2000)

    # Guardar valores em cents ANTES de dividir por 100
    soja_chicago_cents  = precos.get("Soja",  {}).get("valor", 1085.0)
    milho_chicago_cents = precos.get("Milho", {}).get("valor", 475.0)

    # Converter commodities de cents/bushel para dólares/bushel
    for nome in ["Soja", "Milho", "Trigo", "Cafe", "Algodao"]:
        if nome in precos:
            precos[nome]["valor"]  = round(precos[nome]["valor"]  / 100, 2)
            precos[nome]["maxima"] = round(precos[nome]["maxima"] / 100, 2)
            precos[nome]["minima"] = round(precos[nome]["minima"] / 100, 2)

    # Calcular preço da soja em reais/saca (60kg)
    # cents/bushel → US$/bushel → R$/saca
    soja_chicago_dolar = soja_chicago_cents / 100
    soja_saca_reais = round((soja_chicago_dolar / 27.2) * 60 * dolar, 2)

    # Calcular preço do milho em reais/saca (60kg)
    milho_chicago_dolar = milho_chicago_cents / 100
    milho_saca_reais = round((milho_chicago_dolar / 25.4) * 60 * dolar, 2) if "Milho" in precos else 0

    variacao_soja = precos.get("Soja", {}).get("variacao", 0)
    variacao_milho = precos.get("Milho", {}).get("variacao", 0)

    # Portos brasileiros — Soja (R$/saca 60kg)
    precos["Soja Paranagua"] = {
        "valor":   round(soja_saca_reais * 1.025, 2),
        "variacao":variacao_soja,
    }
    precos["Soja Tubarao"] = {
        "valor":   round(soja_saca_reais * 1.015, 2),
        "variacao":variacao_soja,
    }
    precos["Soja Barcarena"] = {
        "valor":   round(soja_saca_reais * 1.010, 2),
        "variacao":variacao_soja,
    }
    precos["Soja Sao Luis"] = {
        "valor":   round(soja_saca_reais * 1.008, 2),
        "variacao":variacao_soja,
    }

    # Portos brasileiros — Milho (R$/saca 60kg)
    if milho_saca_reais > 0:
        precos["Milho Paranagua"] = {
            "valor":   round(milho_saca_reais * 1.020, 2),
            "variacao":variacao_milho,
        }
        precos["Milho Tubarao"] = {
            "valor":   round(milho_saca_reais * 1.010, 2),
            "variacao":variacao_milho,
        }
        precos["Milho Barcarena"] = {
            "valor":   round(milho_saca_reais * 1.005, 2),
            "variacao":variacao_milho,
        }
        precos["Milho Sao Luis"] = {
            "valor":   round(milho_saca_reais * 1.000, 2),
            "variacao":variacao_milho,
        }

    # Sorgo nos portos — estimado como 85% do milho (padrão de mercado)
    if milho_saca_reais > 0:
        sorgo_base = milho_saca_reais * 0.85
        variacao_sorgo = variacao_milho
        precos["Sorgo Paranagua"] = {"valor": round(sorgo_base * 1.020, 2), "variacao": variacao_sorgo}
        precos["Sorgo Tubarao"]   = {"valor": round(sorgo_base * 1.010, 2), "variacao": variacao_sorgo}
        precos["Sorgo Barcarena"] = {"valor": round(sorgo_base * 1.005, 2), "variacao": variacao_sorgo}
        precos["Sorgo Sao Luis"]  = {"valor": round(sorgo_base * 1.000, 2), "variacao": variacao_sorgo}

    return precos


# ========================================
# GERAÇÃO DO RESUMO COM IA
# ========================================
def gerar_resumo_ia(precos):
    cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    texto_precos = ""
    for commodity, dados in precos.items():
        sinal = "+" if dados["variacao"] > 0 else ""
        texto_precos += f"{commodity}: {dados['valor']} ({sinal}{dados['variacao']}%)\n"

    resposta = cliente.messages.create(
        model      ="claude-sonnet-4-6",
        max_tokens =300,
        messages   =[{
            "role"   : "user",
            "content": f"""Você é um analista do agronegócio brasileiro.
Com base nos preços abaixo, escreva um resumo de mercado em 2-3 frases 
simples e diretas para produtores rurais brasileiros.
Seja objetivo e mencione os destaques do dia.

Preços de hoje:
{texto_precos}

Responda em português, de forma clara e sem jargões complexos.
Não use markdown, asteriscos ou formatação especial."""
        }]
    )
    return resposta.content[0].text


# ========================================
# MONTAGEM DA MENSAGEM
# ========================================
def montar_mensagem(precos, resumo_ia):
    data_hoje  = datetime.now().strftime("%d/%m/%Y")

    chicago       = ["Soja", "Milho", "Trigo", "Cafe", "Algodao"]
    petroleo      = ["Petroleo WTI", "Petroleo Brent"]


    msg = f"""🌾 *AGROPULSE — Fechamento do Mercado*
📅 {data_hoje}

*📊 BOLSA DE CHICAGO (CBOT)*\n"""

    for nome in chicago:
        if nome in precos:
            dados = precos[nome]
            emoji = "📈" if dados["variacao"] > 0 else "📉"
            sinal = "+" if dados["variacao"] > 0 else ""
            msg  += f"{emoji} *{nome}:* US$ {dados['valor']:.2f} ({sinal}{dados['variacao']:.2f}%)\n"

    msg += f"\n*🛢️ PETRÓLEO*\n"
    for nome in petroleo:
        if nome in precos:
            dados = precos[nome]
            emoji = "📈" if dados["variacao"] > 0 else "📉"
            sinal = "+" if dados["variacao"] > 0 else ""
            msg  += f"{emoji} *{nome}:* US$ {dados['valor']:.2f} ({sinal}{dados['variacao']:.2f}%)\n"

    if "Dolar" in precos:
        dolar = precos["Dolar"]
        sinal = "+" if dolar["variacao"] > 0 else ""
        # Dólar com 4 casas decimais
        msg  += f"\n*💵 DÓLAR:* R$ {dolar['valor']:.4f} ({sinal}{dolar['variacao']:.2f}%)\n"

    portos_nomes = ["Paranagua", "Tubarao", "Barcarena", "Sao Luis"]
    culturas_porto = [("Soja", "🌱"), ("Milho", "🌽"), ("Sorgo", "🌾")]

    msg += f"\n*🚢 PORTOS BRASILEIROS (R$/saca)*\n"
    for porto in portos_nomes:
        linhas = []
        for cultura, emoji_cultura in culturas_porto:
            chave = f"{cultura} {porto}"
            if chave in precos:
                dados = precos[chave]
                emoji = "📈" if dados["variacao"] > 0 else "📉"
                sinal = "+" if dados["variacao"] > 0 else ""
                linhas.append(f"  {emoji} {cultura}: R$ {dados['valor']:.2f}/sc ({sinal}{dados['variacao']:.2f}%)")
        if linhas:
            msg += f"\n📍 *{porto}*\n" + "\n".join(linhas) + "\n"

    msg += f"""
*🤖 Análise do Dia:*
{resumo_ia}

_AgroPulse AI — Informação que vale dinheiro_ 💰"""
    return msg


# ========================================
# ENVIO PELO WHATSAPP (Z-API)
# ========================================
def enviar_whatsapp_zapi(numero, mensagem):
    url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {
        "Content-Type": "application/json",
        "Client-Token": ZAPI_CLIENT_TOKEN
    }
    payload = {
        "phone"  : numero,
        "message": mensagem
    }
    resposta = requests.post(url, headers=headers, json=payload)
    return resposta.status_code, resposta.json()


def enviar_whatsapp(mensagem):
    import random as random_module

    # Verificar horário permitido (8h às 20h)
    hora_atual = datetime.now().hour
    if hora_atual < 8 or hora_atual >= 20:
        print(f"⏰ Fora do horário permitido ({hora_atual}h). Envio cancelado.")
        print("⏰ Mensagens só são enviadas entre 8h e 20h para evitar banimento.")
        return

    enviados = 0
    falhas   = 0

    try:
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("SELECT nome, whatsapp FROM produtores WHERE ativo=1")
        produtores = [{"nome": row[0], "whatsapp": row[1]} for row in c.fetchall()]
        conn.close()
    except Exception as e:
        print(f"Erro ao buscar produtores: {e}")
        produtores = []

    total = len(produtores)
    print(f"📤 Iniciando envio para {total} produtores...")

    for i, usuario in enumerate(produtores):
        try:
            numero = usuario["whatsapp"].strip().replace(" ", "").replace("-", "")
            if not numero.startswith("55"):
                numero = "55" + numero

            status, resposta = enviar_whatsapp_zapi(numero, mensagem)

            if status == 200:
                print(f"✅ [{i+1}/{total}] Enviado para {usuario['nome']} ({numero})")
                try:
                    conn = sqlite3.connect(DB_PATH)
                    c    = conn.cursor()
                    c.execute("UPDATE produtores SET mensagens_enviadas = mensagens_enviadas + 1 WHERE whatsapp=?",
                              (usuario["whatsapp"],))
                    conn.commit()
                    conn.close()
                except:
                    pass
                enviados += 1
            else:
                print(f"❌ [{i+1}/{total}] Falha para {usuario['nome']}: {resposta}")
                falhas += 1

            # Delay aleatório entre envios para parecer mais humano
            # Entre 8 e 15 segundos entre cada mensagem
            if i < total - 1:
                delay = random_module.uniform(8, 15)
                print(f"⏳ Aguardando {delay:.1f}s antes do próximo envio...")
                time.sleep(delay)

        except Exception as e:
            print(f"❌ Erro ao enviar para {usuario['nome']}: {e}")
            falhas += 1

    print(f"\n📊 Envio concluído: {enviados} enviados, {falhas} falhas")


# ========================================
# FUNÇÃO PRINCIPAL
# ========================================
def enviar_relatorio():
    print(f"🔄 Gerando relatório às {datetime.now().strftime('%H:%M')}...")
    precos   = buscar_precos()
    resumo   = gerar_resumo_ia(precos)
    mensagem = montar_mensagem(precos, resumo)
    enviar_whatsapp(mensagem)


# ========================================
# AGENDAMENTO (uso direto)
# ========================================
if __name__ == "__main__":
    print("🚀 AgroPulse iniciado!")
    enviar_relatorio()
    schedule.every().day.at("18:00").do(enviar_relatorio)
    print("⏰ Agendado para enviar todo dia às 18h")
    print("✋ Pressione CTRL+C para parar")
    while True:
        schedule.run_pending()
        time.sleep(60)
