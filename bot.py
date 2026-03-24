import os
import time
import requests
from datetime import datetime
import json

# ============ CONFIG ============
API_KEY = os.environ.get("GOLD_API_KEY", "")
INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", "10000"))
SL_PCT = float(os.environ.get("SL_PCT", "1.5"))
TP_PCT = float(os.environ.get("TP_PCT", "3.0"))
POS_SIZE = float(os.environ.get("POS_SIZE", "0.10"))
MAX_DAILY_LOSS = float(os.environ.get("MAX_DAILY_LOSS", "2.0"))
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "30"))

# ============ STATE ============
capital = INITIAL_CAPITAL
position = None
prices = []
trades = []
daily_loss = 0.0
last_date = datetime.now().date()

def log(msg, level="INFO"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [{level}] {msg}", flush=True)

def fetch_gold_price():
    try:
        res = requests.get(
            "https://www.goldapi.io/api/XAU/USD",
            headers={"x-access-token": API_KEY},
            timeout=10
        )
        if res.status_code == 200:
            data = res.json()
            return data.get("price") or data.get("ask")
        else:
            log(f"Erreur API: {res.status_code}", "WARN")
            return None
    except Exception as e:
        log(f"Erreur réseau: {e}", "WARN")
        return None

def calc_ma(arr, n):
    if len(arr) < n:
        return None
    return sum(arr[-n:]) / n

def check_day_reset():
    global daily_loss, last_date
    today = datetime.now().date()
    if today != last_date:
        daily_loss = 0.0
        last_date = today
        log("Nouveau jour — remise à zéro perte journalière", "INFO")

def close_trade(price, reason):
    global capital, position, daily_loss
    if not position:
        return
    gain = (price - position["price"]) * position["qty"]
    capital += position["qty"] * price
    if gain < 0:
        daily_loss += gain

    trades.append({
        "entry": position["price"],
        "exit": price,
        "qty": position["qty"],
        "pnl": round(gain, 2),
        "reason": reason,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M")
    })

    emoji = "✅" if reason == "TP" else "🛑" if reason == "SL" else "🔴"
    log(f"{emoji} SELL [{reason}] {position['qty']:.4f}oz @ {price:.2f}$ | P&L: {gain:+.2f}$", "TRADE")
    position = None

def bot_decision(price):
    global capital, position

    check_day_reset()

    # Vérif perte journalière max
    daily_loss_pct = abs(daily_loss / INITIAL_CAPITAL * 100)
    if daily_loss_pct >= MAX_DAILY_LOSS and not position:
        log(f"⛔ Perte max journalière atteinte ({daily_loss_pct:.1f}%) — Pause trading", "WARN")
        return

    # Stop-Loss / Take-Profit
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
        log(f"Accumulation données: {len(prices)}/21 points", "INFO")
        return

    m5  = calc_ma(prices, 5)
    m20 = calc_ma(prices, 20)
    pm5  = calc_ma(prices[:-1], 5)
    pm20 = calc_ma(prices[:-1], 20)

    if not all([m5, m20, pm5, pm20]):
        return

    # Signal BUY
    if not position and pm5 < pm20 and m5 > m20:
        allocate = capital * POS_SIZE
        qty = round(allocate / price, 4)
        if qty <= 0:
            log("Capital insuffisant", "WARN")
            return
        capital -= qty * price
        position = {"price": price, "qty": qty}
        sl = price * (1 - SL_PCT / 100)
        tp = price * (1 + TP_PCT / 100)
        log(f"🟢 BUY {qty:.4f}oz XAU @ {price:.2f}$ | SL:{sl:.2f} TP:{tp:.2f}", "TRADE")

    # Signal SELL (croisement baissier)
    elif position and pm5 > pm20 and m5 < m20:
        close_trade(price, "Signal")

def print_summary():
    pnl = capital - INITIAL_CAPITAL
    total_trades = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = round(wins / total_trades * 100) if total_trades > 0 else 0
    log(f"📊 Capital: {capital:.2f}$ | P&L: {pnl:+.2f}$ | Trades: {total_trades} | Win Rate: {win_rate}%", "SUMMARY")

def main():
    log("🚀 Gold Bot Pro démarré", "INFO")
    log(f"💰 Capital initial: {INITIAL_CAPITAL}$ | SL: {SL_PCT}% | TP: {TP_PCT}% | Position: {POS_SIZE*100:.0f}%", "INFO")

    if not API_KEY:
        log("❌ GOLD_API_KEY manquante ! Ajoutez-la dans les variables Railway.", "ERROR")
        return

    tick = 0
    while True:
        price = fetch_gold_price()
        if price:
            prices.append(price)
            log(f"💹 XAU/USD: {price:.2f}$ | Points: {len(prices)}", "INFO")
            bot_decision(price)
            tick += 1
            if tick % 10 == 0:
                print_summary()
        time.sleep(REFRESH_SECONDS)

if __name__ == "__main__":
    main()
