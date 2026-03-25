import os
import time
import requests
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# ============ STATE ============
INIT_CAP = 100.0
capital = INIT_CAP
peak_capital = INIT_CAP
position = None
prices = []
trades = []
logs = []
daily_loss = 0.0
running = False
bot_thread = None

# ============ RISK PARAMS ============
SL_PCT = 1.5
TP_PCT = 3.0
POS_SIZE = 0.10
MAX_DAILY_LOSS = 2.0
REFRESH_SECONDS = 900  # 15 minutes

def add_log(msg, level="INFO"):
    now = datetime.now().strftime("%H:%M:%S")
    logs.append({"time": now, "msg": msg, "level": level})
    if len(logs) > 100:
        logs.pop(0)
    print(f"[{now}] [{level}] {msg}", flush=True)

def fetch_gold_price():
    try:
        res = requests.get("https://api.metals.live/v1/spot/gold", timeout=10)
        if res.status_code == 200:
            d = res.json()
            if isinstance(d, list) and len(d) > 0:
                return float(d[0].get("price", 0))
            if isinstance(d, dict):
                return float(d.get("price", d.get("gold", 0)))
        add_log(f"Erreur API: {res.status_code}", "WARN")
        return None
    except Exception as e:
        add_log(f"Erreur réseau: {str(e)}", "WARN")
        return None

def calc_ma(arr, n):
    if len(arr) < n:
        return None
    return sum(arr[-n:]) / n

def close_trade(price, reason):
    global capital, position, daily_loss, peak_capital
    if not position:
        return
    gain = (price - position["price"]) * position["qty"]
    capital += position["qty"] * price
    if gain < 0:
        daily_loss += gain
    if capital > peak_capital:
        peak_capital = capital
    trades.append({
        "entry": round(position["price"], 2),
        "exit": round(price, 2),
        "qty": round(position["qty"], 4),
        "pnl": round(gain, 2),
        "reason": reason,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    emoji = "✅" if reason == "TP" else "🛑" if reason == "SL" else "🔴"
    add_log(f"{emoji} SELL [{reason}] {position['qty']:.4f}oz @ {price:.2f}$ | P&L: {gain:+.2f}$", "TRADE")
    position = None

def bot_decision(price):
    global capital, position
    daily_loss_pct = abs(daily_loss / INIT_CAP * 100)
    if daily_loss_pct >= MAX_DAILY_LOSS and not position:
        add_log(f"⛔ Perte max journalière ({daily_loss_pct:.1f}%) — Pause", "WARN")
        return
    if position:
        sl_price = position["price"] * (1 - SL_PCT / 100)
        tp_price = position["price"] * (1 + TP_PCT / 100)
        if price <= sl_price:
            close_trade(sl_price, "SL")
            return
        if price >= tp_price:
            close_trade(tp_price, "TP")
            return
    if len(prices) < 21:
        add_log(f"Accumulation: {len(prices)}/21 points", "INFO")
        return
    m5 = calc_ma(prices, 5)
    m20 = calc_ma(prices, 20)
    pm5 = calc_ma(prices[:-1], 5)
    pm20 = calc_ma(prices[:-1], 20)
    if not all([m5, m20, pm5, pm20]):
        return
    if not position and pm5 < pm20 and m5 > m20:
        allocate = capital * POS_SIZE
        qty = round(allocate / price, 4)
        if qty <= 0:
            return
        capital -= qty * price
        position = {"price": price, "qty": qty}
        sl = price * (1 - SL_PCT / 100)
        tp = price * (1 + TP_PCT / 100)
        add_log(f"🟢 BUY {qty:.4f}oz @ {price:.2f}$ | SL:{sl:.2f} TP:{tp:.2f}", "TRADE")
    elif position and pm5 > pm20 and m5 < m20:
        close_trade(price, "Signal")

def bot_loop():
    global running
    add_log("🚀 Bot démarré — XAU/USD", "INFO")
    while running:
        add_log("📡 Récupération prix or...", "INFO")
        price = fetch_gold_price()
        if price:
            prices.append(price)
            add_log(f"💹 XAU/USD: {price:.2f}$ | Points: {len(prices)}", "INFO")
            bot_decision(price)
        time.sleep(REFRESH_SECONDS)
    add_log("⏹ Bot arrêté", "INFO")

# ============ HTML INTERFACE ============
HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>Gold Bot Pro</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;600&display=swap" rel="stylesheet">
<style>
:root{--gold:#D4A017;--gold-light:#F5C842;--bg:#0A0A0B;--bg2:#111114;--bg3:#18181C;--border:#2A2A30;--text:#E8E8EE;--muted:#5A5A6A;--green:#22C55E;--red:#EF4444;--blue:#3B82F6;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:12px;max-width:480px;margin:0 auto;}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid var(--border);}
.title{font-family:'Space Mono',monospace;font-size:1rem;color:var(--gold-light);letter-spacing:2px;}
.sub{font-size:0.65rem;color:var(--muted);margin-top:2px;}
.badge{padding:4px 10px;border-radius:20px;font-size:0.65rem;font-weight:600;font-family:'Space Mono',monospace;}
.badge.live{background:#0F2A0F;border:1px solid var(--green);color:var(--green);}
.badge.stopped{background:#1A0A0A;border:1px solid var(--red);color:var(--red);}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:12px;}
.card{background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:10px 12px;}
.label{font-size:0.62rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;}
.val{font-family:'Space Mono',monospace;font-size:1rem;font-weight:700;}
.green{color:var(--green);}.red{color:var(--red);}.gold{color:var(--gold-light);}.blue{color:var(--blue);}
.controls{display:flex;gap:7px;margin-bottom:12px;}
.btn{flex:1;padding:11px;border:none;border-radius:9px;font-family:'Space Mono',monospace;font-size:0.72rem;font-weight:700;cursor:pointer;text-decoration:none;text-align:center;}
.btn-start{background:#0D2B0D;border:1px solid var(--green);color:var(--green);}
.btn-stop{background:#2B0D0D;border:1px solid var(--red);color:var(--red);}
.btn-reset{background:#0D1A2B;border:1px solid var(--blue);color:var(--blue);}
.log-box{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:8px;height:200px;overflow-y:auto;font-family:'Space Mono',monospace;font-size:0.62rem;}
.log-entry{padding:2px 0;border-bottom:1px solid #1A1A20;}
.log-TRADE{color:var(--gold);}
.log-WARN{color:#F0C040;}
.log-INFO{color:var(--muted);}
.refresh{text-align:center;font-size:0.65rem;color:var(--muted);margin-bottom:10px;}
.trades{background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:10px;margin-bottom:12px;}
.trade-title{font-size:0.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;}
.trade-row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1A1A20;font-size:0.7rem;}
</style>
</head>
<body>
<div class="header">
  <div>
    <div class="title">GOLD BOT PRO</div>
    <div class="sub">XAU/USD · MA5/MA20 · Capital protégé</div>
  </div>
  <div class="badge {{ 'live' if running else 'stopped' }}">
    {{ '🟢 EN VIE' if running else '🔴 ARRÊTÉ' }}
  </div>
</div>

<div class="refresh">⏱ Page actualisée toutes les 30 secondes</div>

<div class="grid">
  <div class="card"><div class="label">💰 Capital</div><div class="val gold">{{ capital }} $</div></div>
  <div class="card"><div class="label">📈 P&L</div><div class="val {{ 'green' if pnl >= 0 else 'red' }}">{{ '+' if pnl >= 0 else '' }}{{ pnl }} $</div></div>
  <div class="card"><div class="label">🥇 Or (oz)</div><div class="val gold">{{ last_price }} $</div></div>
  <div class="card"><div class="label">🎯 Position</div><div class="val {{ 'green' if position else 'gold' }}">{{ position_text }}</div></div>
  <div class="card"><div class="label">🛑 Stop-Loss</div><div class="val red">{{ sl_price }}</div></div>
  <div class="card"><div class="label">✅ Take-Profit</div><div class="val green">{{ tp_price }}</div></div>
</div>

<div class="controls">
  <a href="/start" class="btn btn-start">▶ START</a>
  <a href="/stop" class="btn btn-stop">⏹ STOP</a>
  <a href="/reset" class="btn btn-reset">↺ RESET</a>
</div>

<div class="trades">
  <div class="trade-title">📊 Derniers Trades ({{ trade_count }} total | Win Rate: {{ win_rate }}%)</div>
  {% for t in recent_trades %}
  <div class="trade-row">
    <div>
      <div style="color:#E8E8EE">{{ t.date }} · {{ t.reason }}</div>
      <div style="color:#5A5A6A">{{ t.entry }} → {{ t.exit }} $</div>
    </div>
    <div class="val {{ 'green' if t.pnl >= 0 else 'red' }}">{{ '+' if t.pnl >= 0 else '' }}{{ t.pnl }} $</div>
  </div>
  {% endfor %}
  {% if not recent_trades %}<div style="color:#5A5A6A;text-align:center;padding:10px">Aucun trade fermé</div>{% endif %}
</div>

<div class="log-box">
  {% for l in logs_reversed %}
  <div class="log-entry log-{{ l.level }}">[{{ l.time }}] {{ l.msg }}</div>
  {% endfor %}
</div>
</body>
</html>"""

@app.route('/')
def index():
    last_price = f"{prices[-1]:.2f}" if prices else "--"
    unrealized = (prices[-1] - position["price"]) * position["qty"] if position and prices else 0
    pnl = round(capital - INIT_CAP + unrealized, 2)
    position_text = f"Long ({((prices[-1]-position['price'])/position['price']*100):+.2f}%)" if position and prices else "Aucune"
    sl_price = f"{position['price']*(1-SL_PCT/100):.2f} $" if position else "--"
    tp_price = f"{position['price']*(1+TP_PCT/100):.2f} $" if position else "--"
    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = round(wins/len(trades)*100) if trades else 0
    return render_template_string(HTML,
        running=running, capital=round(capital,2), pnl=pnl,
        last_price=last_price, position=position,
        position_text=position_text, sl_price=sl_price, tp_price=tp_price,
        trade_count=len(trades), win_rate=win_rate,
        recent_trades=list(reversed(trades[-5:])),
        logs_reversed=list(reversed(logs[-30:])))

@app.route('/start')
def start():
    global running, bot_thread
    if not running:
        running = True
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()
    return index()

@app.route('/stop')
def stop():
    global running
    running = False
    return index()

@app.route('/reset')
def reset():
    global capital, peak_capital, position, prices, trades, logs, daily_loss, running
    running = False
    capital = INIT_CAP
    peak_capital = INIT_CAP
    position = None
    prices = []
    trades = []
    logs = []
    daily_loss = 0.0
    add_log("↺ Bot réinitialisé — Capital: 100$", "INFO")
    return index()

@app.route('/api/status')
def status():
    last_price = prices[-1] if prices else 0
    unrealized = (last_price - position["price"]) * position["qty"] if position and prices else 0
    return jsonify({
        "running": running, "capital": round(capital,2),
        "pnl": round(capital - INIT_CAP + unrealized, 2),
        "price": last_price, "trades": len(trades),
        "position": position
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
