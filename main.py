import threading
import schedule
import time
import os
import json
import sqlite3
from formulario import app as formulario_app
from painel import app as painel_app, init_db
from agropulse import enviar_relatorio
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from flask import Flask, request, redirect

# ========================================
# WEBHOOK + REDIRECIONAMENTO ADMIN
# ========================================
WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", "agropulse2024")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agropulse.db")

@formulario_app.route("/webhook", methods=["GET"])
def webhook_verificar():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    print(f"🔔 Webhook GET recebido — mode={mode}, token={token}")
    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        print("✅ Webhook verificado com sucesso!")
        return challenge, 200
    return "Forbidden", 403

@formulario_app.route("/webhook", methods=["POST"])
def webhook_receber():
    data = request.get_json(silent=True)
    if data:
        print(f"📩 Mensagem recebida: {json.dumps(data)[:200]}")
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

@formulario_app.route("/admin")
@formulario_app.route("/admin/")
def redirecionar_admin():
    return redirect("/admin/login")

@formulario_app.route("/zapi-webhook", methods=["POST"])
def zapi_webhook():
    """
    Recebe eventos do Z-API quando alguém manda mensagem para a AgroPulse.
    Se for mensagem de ativação de cadastro, responde com boas-vindas.
    """
    import requests as req
    data = request.get_json(silent=True)
    if not data:
        return "OK", 200

    print(f"📩 Z-API Webhook: {json.dumps(data)[:300]}")

    try:
        # Só processa mensagens recebidas (não as enviadas por nós)
        if data.get("fromMe") == True:
            return "OK", 200

        # Pega o número e texto da mensagem
        phone = data.get("phone", "")
        texto = data.get("text", {}).get("message", "") if isinstance(data.get("text"), dict) else ""

        if not phone or not texto:
            return "OK", 200

        # Normaliza o número (remove @s.whatsapp.net se vier assim)
        numero = phone.replace("@s.whatsapp.net", "").replace("+", "").strip()

        # Verifica se é mensagem de ativação de cadastro
        palavras_ativacao = ["cadastrar", "relatório", "relatorio", "cotações", "cotacoes", "agropulse"]
        eh_ativacao = any(p in texto.lower() for p in palavras_ativacao)

        if not eh_ativacao:
            return "OK", 200

        # Busca o produtor no banco pelo número
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        numero_sem55 = numero[2:] if numero.startswith("55") else numero
        c.execute("SELECT nome FROM produtores WHERE whatsapp LIKE ?", (f"%{numero_sem55}%",))
        row = c.fetchone()
        conn.close()

        nome = row[0] if row else "Produtor"

        # Monta e envia mensagem de boas-vindas
        zapi_instance    = os.environ.get("ZAPI_INSTANCE_ID", "")
        zapi_token       = os.environ.get("ZAPI_TOKEN", "")
        zapi_client_token = os.environ.get("ZAPI_CLIENT_TOKEN", "")

        mensagem_bv = (
            f"🌾 Olá, *{nome}*! Seja bem-vindo à *AgroPulse*! ✅\n\n"
            f"Seu cadastro está *ativo* e você já vai receber os relatórios diários!\n\n"
            f"Todo dia às *18h* você receberá:\n"
            f"📊 Cotações da Bolsa de Chicago\n"
            f"💵 Câmbio do dia\n"
            f"🚢 Preços nos portos brasileiros\n"
            f"🤖 Análise gerada por Inteligência Artificial\n\n"
            f"_AgroPulse AI — Informação que vale dinheiro_ 💰"
        )

        url = f"https://api.z-api.io/instances/{zapi_instance}/token/{zapi_token}/send-text"
        req.post(url,
            headers={"Content-Type": "application/json", "Client-Token": zapi_client_token},
            json={"phone": numero, "message": mensagem_bv},
            timeout=10)

        # Registra no log
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)",
                  ("Boas-vindas enviada", f"{nome} — {numero}"))
        conn.commit()
        conn.close()

        print(f"✅ Boas-vindas enviada para {nome} ({numero})")

    except Exception as e:
        print(f"❌ Erro no zapi-webhook: {e}")

    return "OK", 200

@formulario_app.route("/teste-envio")
def teste_envio():
    import agropulse as ag
    resultado = []
    try:
        precos = ag.buscar_precos()
        resultado.append(f"OK Precos buscados: {len(precos)} commodities")
        resumo = ag.gerar_resumo_ia(precos)
        resultado.append(f"OK Resumo IA: {resumo[:60]}...")
        mensagem = ag.montar_mensagem(precos, resumo)
        resultado.append(f"OK Mensagem: {len(mensagem)} chars")
        import sqlite3
        conn = sqlite3.connect(ag.DB_PATH)
        c = conn.cursor()
        c.execute("SELECT nome, whatsapp FROM produtores WHERE ativo=1")
        produtores = c.fetchall()
        conn.close()
        resultado.append(f"OK Produtores ativos: {len(produtores)}")
        for nome, whatsapp in produtores:
            numero = whatsapp.strip().replace(" ", "").replace("-", "")
            if not numero.startswith("55"):
                numero = "55" + numero
            status, resp = ag.enviar_whatsapp_zapi(numero, mensagem)
            icone = "OK" if status == 200 else "ERRO"
            resultado.append(f"{icone} {nome} ({numero}): HTTP {status} -- {str(resp)[:100]}")
    except Exception as e:
        resultado.append(f"ERRO: {str(e)}")
    return "<pre style='padding:20px;font-size:13px'>" + "\n".join(resultado) + "</pre>", 200

@formulario_app.route("/diagnostico")
def diagnostico():
    import requests as req
    import sqlite3

    resultado = []

    # 1. Checar variáveis de ambiente
    zapi_instance = os.environ.get("ZAPI_INSTANCE_ID", "")
    zapi_token    = os.environ.get("ZAPI_TOKEN", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    resultado.append(f"ZAPI_INSTANCE_ID: {'✅ preenchido' if zapi_instance else '❌ VAZIO'}")
    resultado.append(f"ZAPI_TOKEN: {'✅ preenchido' if zapi_token else '❌ VAZIO'}")
    resultado.append(f"ANTHROPIC_API_KEY: {'✅ preenchido' if anthropic_key else '❌ VAZIO'}")

    # 2. Buscar produtores
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT nome, whatsapp, ativo FROM produtores")
        produtores = c.fetchall()
        conn.close()
        resultado.append(f"\nProdutores no banco: {len(produtores)}")
        for p in produtores:
            resultado.append(f"  - {p[0]} | {p[1]} | ativo={p[2]}")
    except Exception as e:
        resultado.append(f"Erro ao buscar produtores: {e}")

    # 3. Testar Z-API com primeiro produtor
    if produtores and zapi_instance and zapi_token:
        numero = produtores[0][1].strip().replace(" ", "").replace("-", "")
        if not numero.startswith("55"):
            numero = "55" + numero
        url = f"https://api.z-api.io/instances/{zapi_instance}/token/{zapi_token}/send-text"
        try:
            r = req.post(url,
                headers={"Content-Type": "application/json"},
                json={"phone": numero, "message": "🌾 Teste AgroPulse — diagnóstico do sistema"},
                timeout=10)
            resultado.append(f"\nTeste Z-API para {numero}:")
            resultado.append(f"  Status HTTP: {r.status_code}")
            resultado.append(f"  Resposta: {r.text[:300]}")
        except Exception as e:
            resultado.append(f"\nErro ao chamar Z-API: {e}")

    return "<pre style='font-family:monospace;font-size:14px;padding:20px'>" + "\n".join(resultado) + "</pre>", 200

# ========================================
# APP PRINCIPAL
# ========================================
app = Flask(__name__)

app.wsgi_app = DispatcherMiddleware(formulario_app, {
    '/admin': painel_app,
})

def rodar_agendamento():
    schedule.every().day.at("18:00").do(enviar_relatorio)
    print("⏰ Agendamento iniciado — envio todo dia às 18h")
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    init_db()

    thread = threading.Thread(target=rodar_agendamento, daemon=True)
    thread.start()

    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 AgroPulse rodando na porta {port}")
    print(f"🌐 Formulário:  http://localhost:{port}")
    print(f"🎛️  Painel admin: http://localhost:{port}/admin")
    print(f"🔗 Webhook:     http://localhost:{port}/webhook")
    print(f"🧪 Teste envio: http://localhost:{port}/teste-envio")
    app.run(debug=False, host="0.0.0.0", port=port)
