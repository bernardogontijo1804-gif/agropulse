import anthropic
from usuarios import USUARIOS
from twilio.rest import Client
import schedule
import time
import requests
from datetime import datetime

# ========================================
# SUAS CONFIGURAÇÕES — PREENCHA AQUI
# ========================================

ANTHROPIC_API_KEY = "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"

TWILIO_SID = "ACa149c70f4cf4ef201d5f7bce2d8cf14b"
TWILIO_TOKEN = "58180ce99b83135e07ca738ae7b7fa1a"
TWILIO_WHATSAPP = "whatsapp:+14155238886"

# Coloque seu número com código do país (sem espaços)
# Exemplo: whatsapp:+5538999999999
SEU_WHATSAPP = "whatsapp:+553897344327"

# ========================================
# BUSCA DE PREÇOS
# ========================================

def buscar_precos():
    """Busca preços reais via Yahoo Finance"""
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
    
    # Cotações dos portos brasileiros (Cepea - atualizadas manualmente por ora)
    dolar = precos.get("Dolar", {}).get("valor", 5.20)
    soja_chicago = precos.get("Soja", {}).get("valor", 10.85)
    
# Soja no Yahoo Finance vem em centavos, divide por 100
    soja_chicago_real = soja_chicago / 100
    
    # Conversão: 1 bushel = 27,2 kg, 1 saca = 60 kg
    soja_saca = round((soja_chicago_real / 27.2) * 60 * dolar, 2)
    
   # Soja no Yahoo Finance vem em centavos, divide por 100
    soja_chicago_real = soja_chicago / 100
    
    # Corrigir exibição dos preços em centavos
    for nome in ["Soja", "Milho", "Trigo", "Cafe", "Algodao"]:
        if nome in precos:
            precos[nome]["valor"] = round(precos[nome]["valor"] / 100, 2)
            precos[nome]["maxima"] = round(precos[nome]["maxima"] / 100, 2)
            precos[nome]["minima"] = round(precos[nome]["minima"] / 100, 2)
    
    # Conversão: 1 bushel = 27,2 kg, 1 saca = 60 kg
    soja_saca = round((soja_chicago_real / 27.2) * 60 * dolar, 2)
    
    precos["Soja Paranagua"] = {
        "valor": round(soja_saca * 1.02, 2),
        "variacao": precos.get("Soja", {}).get("variacao", 0),
        "maxima": round(soja_saca * 1.03, 2),
        "minima": round(soja_saca * 1.01, 2),
        "moeda": "R$/saca"
    }
    
    precos["Soja Tubarao"] = {
        "valor": round(soja_saca * 1.01, 2),
        "variacao": precos.get("Soja", {}).get("variacao", 0),
        "maxima": round(soja_saca * 1.02, 2),
        "minima": round(soja_saca * 1.00, 2),
        "moeda": "R$/saca"
    }
    
    return precos

# ========================================
# GERAÇÃO DO RESUMO COM IA
# ========================================

def gerar_resumo_ia(precos):
    """Usa a API da Anthropic para gerar análise do mercado"""
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
    """Monta a mensagem formatada para WhatsApp"""
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    hora_agora = datetime.now().strftime("%H:%M")
    
    # Separar grupos
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
    """Envia a mensagem para todos os produtores cadastrados"""
    cliente = Client(TWILIO_SID, TWILIO_TOKEN)
    
    enviados = 0
    falhas = 0
    
    for usuario in USUARIOS:
        if not usuario["ativo"]:
            continue
        try:
            numero = f"whatsapp:{usuario['whatsapp']}"
            cliente.messages.create(
                from_=TWILIO_WHATSAPP,
                to=numero,
                body=mensagem
            )
            print(f"✅ Enviado para {usuario['nome']} ({usuario['whatsapp']})")
            enviados += 1
        except Exception as e:
            print(f"❌ Falha ao enviar para {usuario['nome']}: {e}")
            falhas += 1
    
    print(f"\n📊 Relatório de envio: {enviados} enviados, {falhas} falhas")
# ========================================
# FUNÇÃO PRINCIPAL
# ========================================

def enviar_relatorio():
    """Executa todo o fluxo do relatório"""
    print(f"🔄 Gerando relatório às {datetime.now().strftime('%H:%M')}...")
    
    precos = buscar_precos()
    resumo = gerar_resumo_ia(precos)
    mensagem = montar_mensagem(precos, resumo)
    enviar_whatsapp(mensagem)

# ========================================
# TESTE IMEDIATO + AGENDAMENTO
# ========================================

print("🚀 AgroPulse iniciado!")
print("📲 Enviando mensagem de teste agora...")

# Envia uma mensagem agora para testar
enviar_relatorio()

# Agenda envio automático todo dia às 18h
schedule.every().day.at("18:00").do(enviar_relatorio)

print("⏰ Agendado para enviar todo dia às 18h")
print("✋ Pressione CTRL+C para parar")

while True:
    schedule.run_pending()
    time.sleep(60)