import threading
import schedule
import time
import os
from formulario import app as formulario_app
from painel import app as painel_app, init_db
from agropulse import enviar_relatorio
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from flask import Flask

# App principal
app = Flask(__name__)

# Monta formulário na raiz e painel em /admin
app.wsgi_app = DispatcherMiddleware(formulario_app, {
    '/admin': painel_app
})

def rodar_agendamento():
    schedule.every().day.at("18:00").do(enviar_relatorio)
    print("⏰ Agendamento iniciado — envio todo dia às 18h")
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    init_db()
    
    # Inicia agendamento em segundo plano
    thread = threading.Thread(target=rodar_agendamento, daemon=True)
    thread.start()
    
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 AgroPulse rodando na porta {port}")
    print(f"🌐 Formulário: http://localhost:{port}")
    print(f"🎛️ Painel admin: http://localhost:{port}/admin")
    app.run(debug=False, host="0.0.0.0", port=port)