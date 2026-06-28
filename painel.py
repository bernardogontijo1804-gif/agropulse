from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import sqlite3
import os
import json

app = Flask(__name__)
app.secret_key = "agropulse2026secretkey"

# ========================================
# CONFIGURAÇÕES DE ACESSO
# ========================================
USUARIOS_ADMIN = {
    "bernardo": generate_password_hash("senha123"),
    "anderson": generate_password_hash("senha123"),
}

DB_PATH = "agropulse.db"

# ========================================
# BANCO DE DADOS
# ========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.commit()
    conn.close()

def get_produtores():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM produtores ORDER BY data_cadastro DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM produtores WHERE ativo=1")
    ativos = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM produtores")
    total = c.fetchone()[0]
    c.execute("SELECT SUM(mensagens_enviadas) FROM produtores")
    msgs = c.fetchone()[0] or 0
    c.execute("SELECT * FROM logs ORDER BY data DESC LIMIT 10")
    logs = c.fetchall()
    conn.close()
    return ativos, total, msgs, logs

# ========================================
# HTML DO PAINEL
# ========================================
PAINEL_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AgroPulse — Painel Admin</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Segoe UI',sans-serif; background:#f0f4f0; min-height:100vh; }
.topbar { background:#1a4d2e; padding:16px 32px; display:flex; justify-content:space-between; align-items:center; }
.topbar h1 { color:#fff; font-size:20px; font-weight:600; }
.topbar span { color:#81c784; font-size:13px; }
.topbar a { color:#c8a84b; font-size:13px; text-decoration:none; margin-left:16px; }
.container { max-width:1100px; margin:32px auto; padding:0 24px; }
.stats { display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin-bottom:28px; }
.stat-card { background:#fff; border-radius:12px; padding:24px; border-left:4px solid #1a4d2e; box-shadow:0 2px 8px rgba(0,0,0,0.06); }
.stat-num { font-size:36px; font-weight:700; color:#1a4d2e; }
.stat-label { font-size:13px; color:#666; margin-top:4px; }
.card { background:#fff; border-radius:12px; padding:24px; box-shadow:0 2px 8px rgba(0,0,0,0.06); margin-bottom:24px; }
.card-title { font-size:16px; font-weight:600; color:#1a4d2e; margin-bottom:16px; display:flex; justify-content:space-between; align-items:center; }
table { width:100%; border-collapse:collapse; }
th { text-align:left; padding:10px 12px; font-size:12px; color:#888; font-weight:500; border-bottom:1px solid #eee; text-transform:uppercase; }
td { padding:12px; font-size:14px; color:#333; border-bottom:1px solid #f5f5f5; vertical-align:middle; }
tr:hover td { background:#f9fdf9; }
.badge { display:inline-block; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:500; }
.badge-ativo { background:#e8f5e9; color:#1a4d2e; }
.badge-inativo { background:#fce4e4; color:#c62828; }
.btn { padding:7px 14px; border-radius:7px; border:none; cursor:pointer; font-size:13px; font-weight:500; }
.btn-danger { background:#fce4e4; color:#c62828; }
.btn-success { background:#e8f5e9; color:#1a4d2e; }
.btn-primary { background:#1a4d2e; color:#fff; }
.btn-gold { background:#c8a84b; color:#fff; }
.form-row { display:grid; grid-template-columns:1fr 1fr auto; gap:12px; align-items:end; }
.form-group { display:flex; flex-direction:column; gap:6px; }
label { font-size:13px; font-weight:500; color:#444; }
input { padding:10px 14px; border:1.5px solid #ddd; border-radius:8px; font-size:14px; outline:none; }
input:focus { border-color:#1a4d2e; }
.log-item { font-size:13px; color:#555; padding:8px 0; border-bottom:1px solid #f0f0f0; display:flex; gap:12px; }
.log-time { color:#aaa; min-width:140px; font-size:12px; }
.alert { padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:14px; }
.alert-success { background:#e8f5e9; color:#1a4d2e; border-left:4px solid #1a4d2e; }
.alert-error { background:#fce4e4; color:#c62828; border-left:4px solid #c62828; }
.dispatch-box { background:#f9fdf9; border:1.5px solid #c8e6c9; border-radius:10px; padding:20px; }
.dispatch-box p { font-size:13px; color:#555; margin-bottom:12px; }
</style>
</head>
<body>
<div class="topbar">
  <h1>🌾 AgroPulse — Painel Admin</h1>
  <div>
    <span>Olá, {{ usuario }}!</span>
    <a href="/logout">Sair</a>
  </div>
</div>

<div class="container">
  {% if msg %}
  <div class="alert alert-{{ msg_tipo }}">{{ msg }}</div>
  {% endif %}

  <div class="stats">
    <div class="stat-card">
      <div class="stat-num">{{ ativos }}</div>
      <div class="stat-label">Produtores ativos</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">{{ total }}</div>
      <div class="stat-label">Total cadastrados</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">{{ msgs }}</div>
      <div class="stat-label">Mensagens enviadas</div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">
      📤 Disparar relatório agora
    </div>
    <div class="dispatch-box">
      <p>Envia o relatório com as cotações atuais para todos os produtores ativos imediatamente.</p>
      <form method="POST" action="/disparar">
        <button type="submit" class="btn btn-gold">🚀 Enviar relatório agora</button>
      </form>
    </div>
  </div>

  <div class="card">
    <div class="card-title">
      ➕ Adicionar produtor manualmente
    </div>
    <form method="POST" action="/adicionar">
      <div class="form-row">
        <div class="form-group">
          <label>Nome completo</label>
          <input type="text" name="nome" placeholder="Ex: João Silva" required>
        </div>
        <div class="form-group">
          <label>WhatsApp (com DDD, sem +55)</label>
          <input type="text" name="whatsapp" placeholder="Ex: 38997344327" required>
        </div>
        <button type="submit" class="btn btn-primary" style="height:42px;">Adicionar</button>
      </div>
    </form>
  </div>

  <div class="card">
    <div class="card-title">
      👥 Produtores cadastrados
      <span style="font-size:13px; color:#888; font-weight:400;">{{ total }} no total</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Nome</th>
          <th>WhatsApp</th>
          <th>Cadastro</th>
          <th>Msgs enviadas</th>
          <th>Status</th>
          <th>Ações</th>
        </tr>
      </thead>
      <tbody>
        {% for p in produtores %}
        <tr>
          <td>{{ p[0] }}</td>
          <td><strong>{{ p[1] }}</strong></td>
          <td>{{ p[2] }}</td>
          <td>{{ p[5][:10] if p[5] else '-' }}</td>
          <td>{{ p[6] }}</td>
          <td>
            {% if p[3] == 1 %}
            <span class="badge badge-ativo">Ativo</span>
            {% else %}
            <span class="badge badge-inativo">Inativo</span>
            {% endif %}
          </td>
          <td style="display:flex; gap:8px;">
            <form method="POST" action="/toggle/{{ p[0] }}">
              <button class="btn {% if p[3] == 1 %}btn-danger{% else %}btn-success{% endif %}">
                {% if p[3] == 1 %}Pausar{% else %}Ativar{% endif %}
              </button>
            </form>
            <form method="POST" action="/remover/{{ p[0] }}" onsubmit="return confirm('Remover {{ p[1] }}?')">
              <button class="btn btn-danger">Remover</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <div class="card-title">📋 Últimos eventos</div>
    {% for log in logs %}
    <div class="log-item">
      <span class="log-time">{{ log[3][:16] if log[3] else '-' }}</span>
      <span>{{ log[1] }} — {{ log[2] }}</span>
    </div>
    {% endfor %}
    {% if not logs %}
    <p style="color:#aaa; font-size:13px;">Nenhum evento ainda.</p>
    {% endif %}
  </div>
</div>
</body>
</html>
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>AgroPulse — Login</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Segoe UI',sans-serif; background:linear-gradient(135deg,#1a4d2e,#2d7a47); min-height:100vh; display:flex; align-items:center; justify-content:center; }
.card { background:#fff; border-radius:16px; padding:40px; width:380px; box-shadow:0 20px 60px rgba(0,0,0,0.3); }
.logo { text-align:center; margin-bottom:28px; }
.logo h1 { color:#1a4d2e; font-size:26px; }
.logo p { color:#888; font-size:13px; margin-top:4px; }
.form-group { margin-bottom:16px; }
label { display:block; font-size:13px; font-weight:500; color:#444; margin-bottom:6px; }
input { width:100%; padding:12px 16px; border:1.5px solid #ddd; border-radius:8px; font-size:14px; outline:none; }
input:focus { border-color:#1a4d2e; }
button { width:100%; padding:13px; background:#1a4d2e; color:#fff; border:none; border-radius:8px; font-size:15px; font-weight:600; cursor:pointer; margin-top:8px; }
.erro { background:#fce4e4; color:#c62828; padding:10px 14px; border-radius:8px; font-size:13px; margin-bottom:16px; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <h1>🌾 AgroPulse</h1>
    <p>Painel Administrativo</p>
  </div>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form method="POST">
    <div class="form-group">
      <label>Usuário</label>
      <input type="text" name="usuario" required autofocus>
    </div>
    <div class="form-group">
      <label>Senha</label>
      <input type="password" name="senha" required>
    </div>
    <button type="submit">Entrar</button>
  </form>
</div>
</body>
</html>
"""

# ========================================
# ROTAS
# ========================================
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        usuario = request.form.get("usuario","").lower()
        senha = request.form.get("senha","")
        if usuario in USUARIOS_ADMIN and check_password_hash(USUARIOS_ADMIN[usuario], senha):
            session["usuario"] = usuario
            return redirect("/painel")
        return render_template_string(LOGIN_HTML, erro="Usuário ou senha incorretos.")
    return render_template_string(LOGIN_HTML, erro=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/painel")
def painel():
    if "usuario" not in session:
        return redirect("/login")
    produtores = get_produtores()
    ativos, total, msgs, logs = get_stats()
    msg = request.args.get("msg")
    msg_tipo = request.args.get("tipo", "success")
    return render_template_string(PAINEL_HTML,
        produtores=produtores, ativos=ativos, total=total,
        msgs=msgs, logs=logs, usuario=session["usuario"],
        msg=msg, msg_tipo=msg_tipo)

@app.route("/adicionar", methods=["POST"])
def adicionar():
    if "usuario" not in session:
        return redirect("/login")
    nome = request.form.get("nome","").strip()
    whatsapp = request.form.get("whatsapp","").strip().replace(" ","").replace("-","")
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO produtores (nome, whatsapp) VALUES (?,?)", (nome, whatsapp))
        c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)",
                  ("Novo cadastro", f"{nome} — {whatsapp}"))
        conn.commit()
        conn.close()
        return redirect("/painel?msg=Produtor+adicionado+com+sucesso!&tipo=success")
    except:
        return redirect("/painel?msg=Erro:+WhatsApp+já+cadastrado.&tipo=error")

@app.route("/toggle/<int:id>", methods=["POST"])
def toggle(id):
    if "usuario" not in session:
        return redirect("/login")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ativo, nome FROM produtores WHERE id=?", (id,))
    row = c.fetchone()
    novo = 0 if row[0] == 1 else 1
    c.execute("UPDATE produtores SET ativo=? WHERE id=?", (novo, id))
    status = "Ativado" if novo == 1 else "Pausado"
    c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)",
              (status, row[1]))
    conn.commit()
    conn.close()
    return redirect("/painel?msg=Status+atualizado!&tipo=success")

@app.route("/remover/<int:id>", methods=["POST"])
def remover(id):
    if "usuario" not in session:
        return redirect("/login")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT nome FROM produtores WHERE id=?", (id,))
    nome = c.fetchone()[0]
    c.execute("DELETE FROM produtores WHERE id=?", (id,))
    c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)",
              ("Removido", nome))
    conn.commit()
    conn.close()
    return redirect("/painel?msg=Produtor+removido.&tipo=success")

@app.route("/disparar", methods=["POST"])
def disparar():
    if "usuario" not in session:
        return redirect("/login")
    try:
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from agropulse import enviar_relatorio
        import threading
        t = threading.Thread(target=enviar_relatorio)
        t.start()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO logs (evento, detalhes) VALUES (?,?)",
                  ("Disparo manual", f"Disparado por {session['usuario']}"))
        conn.commit()
        conn.close()
        return redirect("/painel?msg=Relatório+enviado!&tipo=success")
    except Exception as e:
        return redirect(f"/painel?msg=Erro:+{str(e)}&tipo=error")
if __name__ == "__main__":
    init_db()
    print("🎛️ Painel rodando em: http://localhost:5001")
    app.run(debug=False, port=5001)