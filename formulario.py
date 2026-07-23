from flask import Flask, request, render_template_string, redirect
import json
import os
import sqlite3
import requests

app = Flask(__name__)

# Número da AgroPulse (sem + e sem espaços)
AGROPULSE_NUMERO = "5538998828784"

FORMULARIO_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AgroPulse — Cadastro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #1a3a1a, #2d5a27);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .card {
            background: white;
            border-radius: 16px;
            padding: 40px;
            max-width: 480px;
            width: 100%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }
        .logo {
            text-align: center;
            margin-bottom: 30px;
        }
        .logo h1 {
            color: #2d5a27;
            font-size: 28px;
            font-weight: 800;
        }
        .logo p {
            color: #666;
            font-size: 14px;
            margin-top: 5px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            font-weight: 600;
            color: #333;
            margin-bottom: 6px;
            font-size: 14px;
        }
        input, select {
            width: 100%;
            padding: 12px 16px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 15px;
            transition: border-color 0.3s;
            outline: none;
        }
        input:focus, select:focus {
            border-color: #2d5a27;
        }
        .commodities {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-top: 8px;
        }
        .commodity-item {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 10px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .commodity-item:hover {
            border-color: #2d5a27;
            background: #f0f7f0;
        }
        .commodity-item input[type=checkbox] {
            width: auto;
            margin: 0;
        }
        button {
            width: 100%;
            padding: 14px;
            background: #2d5a27;
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            margin-top: 10px;
            transition: background 0.3s;
        }
        button:hover { background: #1a3a1a; }
        .info {
            background: #f0f7f0;
            border-left: 4px solid #2d5a27;
            padding: 12px 16px;
            border-radius: 0 8px 8px 0;
            margin-bottom: 24px;
            font-size: 13px;
            color: #444;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">
            <h1>🌾 AgroPulse AI</h1>
            <p>Informação de mercado direto no seu WhatsApp</p>
        </div>

        <div class="info">
            📲 Após o cadastro, você receberá relatórios diários com cotações da Bolsa de Chicago, portos brasileiros e análise de IA — todo dia às 18h no seu WhatsApp.
        </div>

        <form method="POST" action="/cadastrar">
            <div class="form-group">
                <label>👤 Seu nome completo</label>
                <input type="text" name="nome" placeholder="Ex: João Silva" required>
            </div>

            <div class="form-group">
                <label>📱 WhatsApp (com DDD)</label>
                <input type="text" name="whatsapp" placeholder="Ex: 38999999999" required>
            </div>

            <div class="form-group">
                <label>🌾 Quais commodities você produz?</label>
                <div class="commodities">
                    <label class="commodity-item">
                        <input type="checkbox" name="commodities" value="Soja" checked> Soja
                    </label>
                    <label class="commodity-item">
                        <input type="checkbox" name="commodities" value="Milho"> Milho
                    </label>
                    <label class="commodity-item">
                        <input type="checkbox" name="commodities" value="Trigo"> Trigo
                    </label>
                    <label class="commodity-item">
                        <input type="checkbox" name="commodities" value="Cafe"> Café
                    </label>
                    <label class="commodity-item">
                        <input type="checkbox" name="commodities" value="Algodao"> Algodão
                    </label>
                </div>
            </div>

            <button type="submit">✅ Quero receber os relatórios!</button>
        </form>
    </div>
</body>
</html>
"""

SUCESSO_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AgroPulse — Cadastro realizado!</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #1a3a1a, #2d5a27);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .card {
            background: white;
            border-radius: 16px;
            padding: 40px;
            max-width: 480px;
            width: 100%;
            text-align: center;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }
        .icon { font-size: 60px; margin-bottom: 20px; }
        h1 { color: #2d5a27; margin-bottom: 10px; }
        p { color: #666; line-height: 1.6; margin-bottom: 20px; }
        .aviso {
            background: #f0f7f0;
            border-left: 4px solid #2d5a27;
            padding: 16px;
            border-radius: 0 8px 8px 0;
            font-size: 14px;
            color: #444;
            text-align: left;
            margin-bottom: 20px;
        }
        .btn-whatsapp {
            display: inline-block;
            background: #25D366;
            color: white;
            padding: 16px 32px;
            border-radius: 50px;
            font-size: 16px;
            font-weight: 700;
            text-decoration: none;
            margin-top: 8px;
            width: 100%;
            box-sizing: border-box;
        }
        .passo {
            background: #fff8e1;
            border-left: 4px solid #f9a825;
            padding: 12px 16px;
            border-radius: 0 8px 8px 0;
            font-size: 13px;
            color: #444;
            text-align: left;
            margin-top: 16px;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">✅</div>
        <h1>Cadastro realizado!</h1>
        <p>Obrigado, <strong>{{ nome }}</strong>! Seu cadastro foi confirmado.</p>

        <div class="aviso">
            📲 <strong>Último passo:</strong> Para ativar o recebimento dos relatórios, clique no botão abaixo e envie a mensagem para a AgroPulse no WhatsApp.
        </div>

        <a class="btn-whatsapp" href="https://wa.me/{{ agropulse_numero }}?text={{ mensagem_encoded }}" target="_blank">
            📱 Ativar recebimento no WhatsApp
        </a>

        <div class="passo">
            ⚠️ <strong>Importante:</strong> Você precisa enviar a mensagem para que os relatórios cheguem no seu WhatsApp. É só clicar no botão acima!
        </div>
    </div>
</body>
</html>
"""

def salvar_usuario(nome, whatsapp, commodities):
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agropulse.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS produtores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        whatsapp TEXT NOT NULL UNIQUE,
        ativo INTEGER DEFAULT 1,
        commodities TEXT DEFAULT '["Soja"]',
        data_cadastro TEXT DEFAULT CURRENT_TIMESTAMP,
        mensagens_enviadas INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evento TEXT,
        detalhes TEXT,
        data TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute("INSERT OR IGNORE INTO produtores (nome, whatsapp, commodities) VALUES (?,?,?)",
              (nome, whatsapp, json.dumps(commodities, ensure_ascii=False)))
    c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)",
              ("Novo cadastro via formulário", f"{nome} — {whatsapp}"))
    conn.commit()
    conn.close()
    print(f"✅ Usuário {nome} salvo no banco de dados!")


def enviar_boas_vindas(nome, whatsapp):
    """Envia mensagem de boas-vindas pelo Z-API assim que o produtor se cadastra"""
    zapi_instance = os.environ.get("ZAPI_INSTANCE_ID", "")
    zapi_token    = os.environ.get("ZAPI_TOKEN", "")

    if not zapi_instance or not zapi_token:
        print("⚠️ Z-API não configurado — boas-vindas não enviada")
        return

    numero = whatsapp.strip().replace(" ", "").replace("-", "")
    if not numero.startswith("55"):
        numero = "55" + numero

    mensagem = (
        f"🌾 Olá, {nome}! Bem-vindo à *AgroPulse*!\n\n"
        f"Seu cadastro foi confirmado com sucesso ✅\n\n"
        f"A partir de hoje você receberá todo dia às *18h* um relatório completo com:\n"
        f"📊 Cotações da Bolsa de Chicago\n"
        f"💵 Câmbio do dia\n"
        f"🚢 Preços nos portos brasileiros\n"
        f"🤖 Análise gerada por Inteligência Artificial\n\n"
        f"_AgroPulse AI — Informação que vale dinheiro_ 💰"
    )

    url = f"https://api.z-api.io/instances/{zapi_instance}/token/{zapi_token}/send-text"
    try:
        r = requests.post(url,
            headers={"Content-Type": "application/json"},
            json={"phone": numero, "message": mensagem},
            timeout=10)
        print(f"📩 Boas-vindas para {nome} ({numero}): HTTP {r.status_code}")
    except Exception as e:
        print(f"⚠️ Erro ao enviar boas-vindas: {e}")


@app.route("/")
def formulario():
    return render_template_string(FORMULARIO_HTML)


@app.route("/cadastrar", methods=["POST"])
def cadastrar():
    from urllib.parse import quote

    nome = request.form.get("nome", "").strip()
    whatsapp = request.form.get("whatsapp", "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    commodities = request.form.getlist("commodities")

    if not commodities:
        commodities = ["Soja"]

    salvar_usuario(nome, whatsapp, commodities)

    # Envia mensagem de boas-vindas automática
    enviar_boas_vindas(nome, whatsapp)

    print(f"✅ Novo cadastro: {nome} — {whatsapp}")

    # Mensagem pré-preenchida para o produtor enviar para a AgroPulse
    mensagem_wa = f"Olá! Sou {nome} e acabei de me cadastrar na AgroPulse. Quero receber os relatórios diários de mercado! 🌾"
    mensagem_encoded = quote(mensagem_wa)

    return render_template_string(SUCESSO_HTML,
        nome=nome,
        agropulse_numero=AGROPULSE_NUMERO,
        mensagem_encoded=mensagem_encoded)


if __name__ == "__main__":
    print("🌐 Formulário rodando em: http://localhost:5000")
    print("✋ Pressione CTRL+C para parar")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
