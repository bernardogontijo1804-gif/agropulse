import anthropic
from twilio.rest import Client
import schedule
import time
import sqlite3
import os
from datetime import datetime

# ========================================
# CONFIGURAÇÕES
# ========================================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "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")
TWILIO_SID = os.environ.get("TWILIO_SID", "ACa149c70f4cf4ef201d5f7bce2d8cf14b")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "58180ce99b83135e07ca738ae7b7fa1a")
TWILIO_WHATSAPP = os.environ.get("TWILIO_WHATSAPP", "whatsapp:+14155238886")

# ========================================
# BUSCA DE PREÇOS
# ========================================
def buscar_precos():
    import yfinance as yf
    simbolos = {
        "Soja": "ZS=F",
        "Milho": "ZC=F",
        "Trigo": "ZW=F",
        "Cafe": "KC=F",
        "Algodao": "CT=F",
        "Petroleo WTI": "CL=F",
        "Petroleo Brent": "BZ=F",
        "Dolar": "BRL=X",
    }
    precos = {}
    for nome, simbolo in simbolos.items():
        try:
            ticker = yf.Ticker(simbolo)
            hist = ticker.history(period="2d")
            if len(hist) >= 2:
                atual = hist["Close"].iloc[-1]
                anterior = hist["Close"].iloc[-2]
                variacao = ((atual - anterior) / anterior) * 100
                precos[nome] = {
                    "valor": round(atual, 2),
                    "variacao": round(variacao, 2),
                    "maxima": round(hist["High"].iloc[-1], 2),
                    "minima": round(hist["Low"].iloc[-1], 2),
                }
        except:
            pass

    dolar = precos.get("Dolar", {}).get("valor", 5.20)
    soja_chicago = precos.get("Soja", {}).get("valor", 1085)

    for nome in ["Soja", "Milho", "Trigo", "Cafe", "Algodao"]:
        if nome in precos:
            precos[nome]["valor"] = round(precos[nome]["valor"] / 100, 2)
            precos[nome]["maxima"] = round(precos[nome]["maxima"] / 100, 2)
            precos[nome]["minima"] = round(precos[nome]["minima"] / 100, 2)

    soja_chicago_real = soja_chicago / 100
    soja_saca = round((soja_chicago_real / 27.2) * 60 * dolar, 2)

    precos["Soja Paranagua"] = {
        "valor": round(soja_saca * 1.02, 2),
        "variacao": precos.get("Soja", {}).get("variacao", 0),
        "maxima": round(soja_saca * 1.03, 2),
        "minima": round(soja_saca * 1.01, 2),
    }
    precos["Soja Tubarao"] = {
        "valor": round(soja_saca * 1.01, 2),
        "variacao": precos.get("Soja", {}).get("variacao", 0),
        "maxima": round(soja_saca * 1.02, 2),
        "minima": round(soja_saca * 1.00, 2),
    }
    return precos

# ========================================
# GERAÇÃO DO RESUMO COM IA
# ========================================
def gerar_resumo_ia(precos):
    cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    texto_precos = ""
    for commodity, dados in precos.items():
        sinal = "+" if dados["variacao"] > 0 else ""
        texto_precos += f"{commodity}: US$ {dados['valor']} ({sinal}{dados['variacao']}%)\n"

    resposta = cliente.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
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
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    hora_agora = datetime.now().strftime("%H:%M")

    chicago = ["Soja", "Milho", "Trigo", "Cafe", "Algodao"]
    petroleo = ["Petroleo WTI", "Petroleo Brent"]
    portos = ["Soja Paranagua", "Soja Tubarao"]

    msg = f"""🌾 *AGROPULSE — Fechamento do Mercado*
📅 {data_hoje} às {hora_agora}

*📊 BOLSA DE CHICAGO (CBOT)*\n"""

    for nome in chicago:
        if nome in precos:
            dados = precos[nome]
            emoji = "📈" if dados["variacao"] > 0 else "📉"
            sinal = "+" if dados["variacao"] > 0 else ""
            msg += f"{emoji} *{nome}:* US$ {dados['valor']} ({sinal}{dados['variacao']}%)\n"

    msg += f"\n*🛢️ PETRÓLEO*\n"
    for nome in petroleo:
        if nome in precos:
            dados = precos[nome]
            emoji = "📈" if dados["variacao"] > 0 else "📉"
            sinal = "+" if dados["variacao"] > 0 else ""
            msg += f"{emoji} *{nome}:* US$ {dados['valor']} ({sinal}{dados['variacao']}%)\n"

    if "Dolar" in precos:
        dolar = precos["Dolar"]
        sinal = "+" if dolar["variacao"] > 0 else ""
        msg += f"\n*💵 DÓLAR:* R$ {dolar['valor']} ({sinal}{dolar['variacao']}%)\n"

    msg += f"\n*🚢 PORTOS BRASILEIROS (Soja)*\n"
    for nome in portos:
        if nome in precos:
            dados = precos[nome]
            emoji = "📈" if dados["variacao"] > 0 else "📉"
            sinal = "+" if dados["variacao"] > 0 else ""
            msg += f"{emoji} *{nome}:* R$ {dados['valor']}/saca ({sinal}{dados['variacao']}%)\n"

    msg += f"""
*🤖 Análise do Dia:*
{resumo_ia}

_AgroPulse AI — Informação que vale dinheiro_ 💰"""
    return msg

# ========================================
# ENVIO PELO WHATSAPP
# ========================================
def enviar_whatsapp(mensagem):
    cliente = Client(TWILIO_SID, TWILIO_TOKEN)
    enviados = 0
    falhas = 0

    try:
        conn = sqlite3.connect("agropulse.db")
        c = conn.cursor()
        c.execute("SELECT nome, whatsapp FROM produtores WHERE ativo=1")
        produtores = [{"nome": row[0], "whatsapp": row[1]} for row in c.fetchall()]
        conn.close()
    except:
        from usuarios import USUARIOS
        produtores = USUARIOS

    for usuario in produtores:
        try:
            numero = f"whatsapp:+55{usuario['whatsapp']}"
            cliente.messages.create(
                from_=TWILIO_WHATSAPP,
                to=numero,
                body=mensagem
            )
            print(f"✅ Enviado para {usuario['nome']} ({usuario['whatsapp']})")
            try:
                conn = sqlite3.connect("agropulse.db")
                c = conn.cursor()
                c.execute("UPDATE produtores SET mensagens_enviadas = mensagens_enviadas + 1 WHERE whatsapp=?",
                         (usuario['whatsapp'],))
                conn.commit()
                conn.close()
            except:
                pass
            enviados += 1
        except Exception as e:
            print(f"❌ Falha ao enviar para {usuario['nome']}: {e}")
            falhas += 1

    print(f"\n📊 Relatório de envio: {enviados} enviados, {falhas} falhas")

# ========================================
# FUNÇÃO PRINCIPAL
# ========================================
def enviar_relatorio():
    print(f"🔄 Gerando relatório às {datetime.now().strftime('%H:%M')}...")
    precos = buscar_precos()
    resumo = gerar_resumo_ia(precos)
    mensagem = montar_mensagem(precos, resumo)
    enviar_whatsapp(mensagem)

# ========================================
# AGENDAMENTO
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