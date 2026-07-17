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
ZAPI_INSTANCE_ID = os.environ.get("ZAPI_INSTANCE_ID", "")
ZAPI_TOKEN       = os.environ.get("ZAPI_TOKEN", "")
WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", "agropulse2024")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agropulse.db")

# ========================================
# APP FLASK (webhook)
# ========================================
app = Flask(__name__)

@app.route("/webhook", methods=["GET"])
def webhook_verificar():
    """
    A Meta chama esta rota para confirmar que o servidor é válido.
    Ela envia hub.mode, hub.verify_token e hub.challenge.
    Se o token bater, devolvemos o challenge e a Meta confirma a conexão.
    """
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
    """
    Recebe notificações da Meta (mensagens recebidas, status de entrega, etc.)
    Por ora apenas registra no log — pode expandir para responder automaticamente.
    """
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
            ticker = yf.Ticker(simbolo)
            hist   = ticker.history(period="2d")
            if len(hist) >= 2:
                atual    = hist["Close"].iloc[-1]
                anterior = hist["Close"].iloc[-2]
                variacao = ((atual - anterior) / anterior) * 100
                precos[nome] = {
                    "valor":   round(atual, 2),
                    "variacao":round(variacao, 2),
                    "maxima":  round(hist["High"].iloc[-1], 2),
                    "minima":  round(hist["Low"].iloc[-1],  2),
                }
        except:
            pass

    dolar         = precos.get("Dolar", {}).get("valor", 5.20)
    soja_chicago  = precos.get("Soja",  {}).get("valor", 1085)

    for nome in ["Soja", "Milho", "Trigo", "Cafe", "Algodao"]:
        if nome in precos:
            precos[nome]["valor"]  = round(precos[nome]["valor"]  / 100, 2)
            precos[nome]["maxima"] = round(precos[nome]["maxima"] / 100, 2)
            precos[nome]["minima"] = round(precos[nome]["minima"] / 100, 2)

    soja_chicago_real = soja_chicago / 100
    soja_saca         = round((soja_chicago_real / 27.2) * 60 * dolar, 2)

    precos["Soja Paranagua"] = {
        "valor":   round(soja_saca * 1.02, 2),
        "variacao":precos.get("Soja", {}).get("variacao", 0),
        "maxima":  round(soja_saca * 1.03, 2),
        "minima":  round(soja_saca * 1.01, 2),
    }
    precos["Soja Tubarao"] = {
        "valor":   round(soja_saca * 1.01, 2),
        "variacao":precos.get("Soja", {}).get("variacao", 0),
        "maxima":  round(soja_saca * 1.02, 2),
        "minima":  round(soja_saca * 1.00, 2),
    }
    return precos


# ========================================
# GERAÇÃO DO RESUMO COM IA
# ========================================
def gerar_resumo_ia(precos):
    cliente     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    texto_precos = ""
    for commodity, dados in precos.items():
        sinal = "+" if dados["variacao"] > 0 else ""
        texto_precos += f"{commodity}: US$ {dados['valor']} ({sinal}{dados['variacao']}%)\n"

    resposta = cliente.messages.create(
        model      ="claude-sonnet-4-6",
        max_tokens =300,
        messages   =[{
            "role"   : "user",
            "content": f"""Você é um analista do agronegócio brasileiro.
Com base nos preços abaixo da Bolsa de Chicago, escreva um resumo
de mercado em 2-3 frases simples e diretas para produtores rurais.
Seja objetivo e mencione os destaques do dia.

Preços de hoje:
{texto_precos}

Responda em português, de forma clara e sem jargões complexos."""
        }]
    )
    return resposta.content[0].text


# ========================================
# MONTAGEM DA MENSAGEM
# ========================================
def montar_mensagem(precos, resumo_ia):
    data_hoje  = datetime.now().strftime("%d/%m/%Y")
    hora_agora = datetime.now().strftime("%H:%M")

    chicago  = ["Soja", "Milho", "Trigo", "Cafe", "Algodao"]
    petroleo = ["Petroleo WTI", "Petroleo Brent"]
    portos   = ["Soja Paranagua", "Soja Tubarao"]

    msg = f"""🌾 *AGROPULSE — Fechamento do Mercado*
📅 {data_hoje} às {hora_agora}

*📊 BOLSA DE CHICAGO (CBOT)*\n"""

    for nome in chicago:
        if nome in precos:
            dados = precos[nome]
            emoji = "📈" if dados["variacao"] > 0 else "📉"
            sinal = "+" if dados["variacao"] > 0 else ""
            msg  += f"{emoji} *{nome}:* US$ {dados['valor']} ({sinal}{dados['variacao']}%)\n"

    msg += f"\n*🛢️ PETRÓLEO*\n"
    for nome in petroleo:
        if nome in precos:
            dados = precos[nome]
            emoji = "📈" if dados["variacao"] > 0 else "📉"
            sinal = "+" if dados["variacao"] > 0 else ""
            msg  += f"{emoji} *{nome}:* US$ {dados['valor']} ({sinal}{dados['variacao']}%)\n"

    if "Dolar" in precos:
        dolar = precos["Dolar"]
        sinal = "+" if dolar["variacao"] > 0 else ""
        msg  += f"\n*💵 DÓLAR:* R$ {dolar['valor']} ({sinal}{dolar['variacao']}%)\n"

    msg += f"\n*🚢 PORTOS BRASILEIROS (Soja)*\n"
    for nome in portos:
        if nome in precos:
            dados = precos[nome]
            emoji = "📈" if dados["variacao"] > 0 else "📉"
            sinal = "+" if dados["variacao"] > 0 else ""
            msg  += f"{emoji} *{nome}:* R$ {dados['valor']}/saca ({sinal}{dados['variacao']}%)\n"

    msg += f"""
*🤖 Análise do Dia:*
{resumo_ia}

_AgroPulse AI — Informação que vale dinheiro_ 💰"""
    return msg


# ========================================
# ENVIO PELO WHATSAPP (Z-API)
# ========================================
def enviar_whatsapp_zapi(numero, mensagem):
    """
    Envia mensagem de texto via Z-API.
    O número deve estar no formato: 5538999999999 (com 55 + DDD + número)
    """
    url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json"}
    payload = {
        "phone"  : numero,
        "message": mensagem
    }
    resposta = requests.post(url, headers=headers, json=payload)
    return resposta.status_code, resposta.json()


def enviar_whatsapp(mensagem):
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

    for usuario in produtores:
        try:
            # Garante formato correto: 55 + DDD + número
            numero = usuario["whatsapp"].strip().replace(" ", "").replace("-", "")
            if not numero.startswith("55"):
                numero = "55" + numero

            status, resposta = enviar_whatsapp_zapi(numero, mensagem)

            if status == 200:
                print(f"✅ Enviado para {usuario['nome']} ({numero})")
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
                print(f"❌ Falha para {usuario['nome']}: {resposta}")
                falhas += 1

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
