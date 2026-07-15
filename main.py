import threading
import schedule
import time
import os
import json
from formulario import app as formulario_app
from painel import app as painel_app, init_db
from agropulse import enviar_relatorio
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from flask import Flask, request
import sqlite3

# ========================================
# WEBHOOK — adicionado direto no app principal
# ========================================
WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", "agropulse2024")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agropulse.db")

@formulario_app.route("/webhook", methods=["GET"])
def webhook_verificar():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    print(f"🔔 Webhook GET recebido — mode={mode}, token={token}, challenge={challenge}")

    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        print("✅ Webhook verificado com sucesso!")
        return challenge, 200

    print("❌ Token incorreto na verificação do webhook")
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
    app.run(debug=False, host="0.0.0.0", port=port)
