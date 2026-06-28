import threading
import schedule
import time
from formulario import app
from agropulse import enviar_relatorio
import os

def rodar_agendamento():
    schedule.every().day.at("18:00").do(enviar_relatorio)
    print("⏰ Agendamento iniciado — envio todo dia às 18h")
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    # Roda o agendamento em segundo plano
    thread = threading.Thread(target=rodar_agendamento, daemon=True)
    thread.start()
    
    # Roda o formulário web
    port = int(os.environ.get("PORT", 5000))
    print(f"🌐 Formulário rodando na porta {port}")
    app.run(debug=False, host="0.0.0.0", port=port)