from flask import Flask, request, render_template_string, redirect
import json
import os

app = Flask(__name__)

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
            background: #fff8e1;
            border-left: 4px solid #f9a825;
            padding: 12px 16px;
            border-radius: 0 8px 8px 0;
            font-size: 13px;
            color: #444;
            text-align: left;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">✅</div>
        <h1>Cadastro realizado!</h1>
        <p>Obrigado, <strong>{{ nome }}</strong>! Você receberá os relatórios diários do AgroPulse todo dia às 18h no WhatsApp.</p>
        <div class="aviso">
            📲 <strong>Passo importante:</strong> Para ativar o recebimento, envie a mensagem abaixo para o número <strong>+1 415 523 8886</strong> no WhatsApp:<br><br>
            <strong>join air-appropriate</strong>
        </div>
    </div>
</body>
</html>
"""

def salvar_usuario(nome, whatsapp, commodities):
    """Salva o novo usuário no arquivo usuarios.py"""
    arquivo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usuarios.py")
    
    with open(arquivo, "r", encoding="utf-8") as f:
        conteudo = f.read()
    
    novo_usuario = f"""    {{
        "nome": "{nome}",
        "whatsapp": "+55{whatsapp}",
        "ativo": True,
        "commodities": {json.dumps(commodities, ensure_ascii=False)}
    }},\n"""
    
    if "# Adicione mais produtores aqui seguindo o mesmo modelo:" in conteudo:
        conteudo = conteudo.replace(
            "    # Adicione mais produtores aqui seguindo o mesmo modelo:", 
            novo_usuario + "    # Adicione mais produtores aqui seguindo o mesmo modelo:"
        )
    else:
        conteudo = conteudo.replace(
            "]",
            novo_usuario + "]",
            1
        )
    
    with open(arquivo, "w", encoding="utf-8") as f:
        f.write(conteudo)
    
    print(f"✅ Usuário {nome} salvo no arquivo!")

@app.route("/")
def formulario():
    return render_template_string(FORMULARIO_HTML)

@app.route("/cadastrar", methods=["POST"])
def cadastrar():
    nome = request.form.get("nome", "").strip()
    whatsapp = request.form.get("whatsapp", "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    commodities = request.form.getlist("commodities")
    
    if not commodities:
        commodities = ["Soja"]
    
    salvar_usuario(nome, whatsapp, commodities)
    
    print(f"✅ Novo cadastro: {nome} — {whatsapp}")
    
    return render_template_string(SUCESSO_HTML, nome=nome)

if __name__ == "__main__":
    print("🌐 Formulário rodando em: http://localhost:5000")
    print("✋ Pressione CTRL+C para parar")
    app.run(debug=False, port=5000)