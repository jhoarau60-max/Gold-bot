#!/usr/bin/env python3
"""
GOLD BOT — Système de trading automatique 7j/7
Project Inves'T — John H.

Stratégie multi-indicateurs inspirée des plus grands traders :
- Paul Tudor Jones   : analyse multi-timeframes, tendance principale
- Elder Alexander    : Triple Screen System (3 timeframes)
- Jesse Livermore    : suivre la tendance, pyramider dans les gagnants
- George Soros       : réflexivité du marché, macro analyse via IA
- Richard Dennis     : Turtle Trading — breakout + gestion rigoureuse du risque
- Stanley Druckenmiller : concentration sur les meilleures opportunités

Lundi-Vendredi : XAU/USD (Or) + XAG/USD (Argent)
Samedi-Dimanche : BTC/USD (Bitcoin)
"""

import asyncio
import os
import json
import logging
import io
from datetime import datetime

import pytz
import pandas as pd
import numpy as np
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import google.generativeai as genai
from supabase import create_client, Client

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Nettoyer les noms de variables (espaces parasites possibles dans Railway)
ENV = {k.strip(): v.strip() for k, v in os.environ.items()}

TELEGRAM_TOKEN  = ENV.get("TELEGRAM_TOKEN", "")
if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN manquant! Clés dispo: " + str([k for k in ENV if "TOKEN" in k.upper() or "TELEGRAM" in k.upper()]))
    raise SystemExit("TELEGRAM_TOKEN requis")
JOHN_ID         = int(ENV.get("JOHN_ID", "0"))
GEMINI_API_KEY  = ENV.get("GEMINI_API_KEY", "")
CAPITAL_INITIAL = float(ENV.get("CAPITAL", "50000"))

SUPABASE_URL     = ENV.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = ENV.get("SUPABASE_SERVICE_KEY", "")
sb_client: Client | None = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    try:
        sb_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        logger.info("Supabase connecté")
    except Exception as e:
        logger.error(f"Supabase connexion échouée: {e}")

TICKER_TO_BOT = {
    "XAUUSD=X": "gold",
    "XAGUSD=X": "silver",
    "BTC-USD":  "oracle",
}
RISK_PER_TRADE  = 0.01   # 1 % du capital par trade (Jesse Livermore : préserver le capital)
MAX_DAILY_LOSS  = 0.02   # 2 % de perte max par jour
TZ              = pytz.timezone("Europe/Brussels")
TRADES_FILE     = "trades.json"

WEEKDAY_INSTRUMENTS = {
    "XAUUSD=X": {"name": "Or (XAU/USD)",     "emoji": "🥇", "pip": 0.01},
    "XAGUSD=X": {"name": "Argent (XAG/USD)", "emoji": "🥈", "pip": 0.001},
}
WEEKEND_INSTRUMENTS = {
    "BTC-USD": {"name": "Bitcoin (BTC/USD)", "emoji": "₿", "pip": 1.0},
}

# ── DONNÉES PERSISTANTES ───────────────────────────────────────────────────────
def load_data() -> dict:
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            return json.load(f)
    return {
        "capital":        CAPITAL_INITIAL,
        "open_positions": [],
        "closed_trades":  [],
        "daily_pnl":      0.0,
        "daily_trades":   0,
        "total_pnl":      0.0,
        "last_reset":     datetime.now(TZ).strftime("%Y-%m-%d"),
        "start_date":     datetime.now(TZ).strftime("%Y-%m-%d"),
        "win_streak":     0,
        "loss_streak":    0,
    }

def save_data(data: dict):
    with open(TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

def get_instruments() -> dict:
    return WEEKEND_INSTRUMENTS if datetime.now(TZ).weekday() >= 5 else WEEKDAY_INSTRUMENTS


# ── FETCH DONNÉES ──────────────────────────────────────────────────────────────
def fetch(ticker: str, period: str = "5d", interval: str = "15m") -> pd.DataFrame | None:
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        return df if not df.empty else None
    except Exception as e:
        logger.error(f"Erreur fetch {ticker}: {e}")
        return None


# ── INDICATEURS TECHNIQUES (multi-stratégies) ──────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"].squeeze()
    h = df["High"].squeeze()
    l = df["Low"].squeeze()

    # ── TENDANCE (Elder Triple Screen — Screen 1 : timeframe supérieur) ──
    df["EMA9"]   = c.ewm(span=9,   adjust=False).mean()
    df["EMA21"]  = c.ewm(span=21,  adjust=False).mean()
    df["EMA50"]  = c.ewm(span=50,  adjust=False).mean()
    df["EMA200"] = c.ewm(span=200, adjust=False).mean()

    # ── MACD (Gerald Appel — momentum & convergence) ──
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["MACD"]        = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"]   = df["MACD"] - df["MACD_signal"]

    # ── RSI (Wilder — détection surachat/survente) ──
    delta = c.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    df["RSI"] = 100 - 100 / (1 + gain.rolling(14).mean() / loss.rolling(14).mean())

    # ── Stochastique (Lane — momentum de court terme) ──
    low14  = l.rolling(14).min()
    high14 = h.rolling(14).max()
    df["STOCH_K"] = 100 * (c - low14) / (high14 - low14)
    df["STOCH_D"] = df["STOCH_K"].rolling(3).mean()

    # ── Bandes de Bollinger (John Bollinger — volatilité & retour à la moyenne) ──
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    df["BB_upper"] = bb_mid + 2 * bb_std
    df["BB_lower"] = bb_mid - 2 * bb_std
    df["BB_mid"]   = bb_mid
    df["BB_width"] = (df["BB_upper"] - df["BB_lower"]) / bb_mid  # volatilité relative

    # ── ATR (Wilder — mesure de volatilité pour stop-loss adaptatif) ──
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()

    # ── Williams %R (Larry Williams — signaux rapides sur les extrêmes) ──
    df["WILLIAMS_R"] = -100 * (high14 - c) / (high14 - low14)

    # ── ADX (Wilder — force de la tendance) ──
    plus_dm  = (h.diff()).clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    mask = plus_dm < minus_dm
    plus_dm[mask] = 0
    mask2 = minus_dm <= plus_dm
    minus_dm[mask2] = 0
    tr_smooth    = tr.rolling(14).mean()
    plus_di      = 100 * plus_dm.rolling(14).mean() / tr_smooth
    minus_di     = 100 * minus_dm.rolling(14).mean() / tr_smooth
    dx           = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    df["ADX"]    = dx.rolling(14).mean()
    df["PLUS_DI"]  = plus_di
    df["MINUS_DI"] = minus_di

    return df


# ── PATTERNS CHANDELIERS (price action — Al Brooks, Steve Nison) ───────────────
def detect_candlestick_pattern(df: pd.DataFrame) -> str | None:
    if len(df) < 3:
        return None
    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    prev2 = df.iloc[-3]

    o1, c1, h1, l1 = float(last["Open"].squeeze() if "Open" in df.columns else last["Close"]), float(last["Close"].squeeze()), float(last["High"].squeeze()), float(last["Low"].squeeze())
    o2, c2 = float(prev["Open"].squeeze() if "Open" in df.columns else prev["Close"]), float(prev["Close"].squeeze())
    o3, c3 = float(prev2["Open"].squeeze() if "Open" in df.columns else prev2["Close"]), float(prev2["Close"].squeeze())

    body1 = abs(c1 - o1)
    body2 = abs(c2 - o2)
    range1 = h1 - l1 if (h1 - l1) > 0 else 1
    upper_shadow = h1 - max(o1, c1)
    lower_shadow = min(o1, c1) - l1

    # Hammer (signal haussier après baisse)
    if lower_shadow > 2 * body1 and upper_shadow < 0.3 * body1 and c2 < o2:
        return "HAMMER_BULL"

    # Shooting Star (signal baissier après hausse)
    if upper_shadow > 2 * body1 and lower_shadow < 0.3 * body1 and c2 > o2:
        return "SHOOTING_STAR_BEAR"

    # Engulfing haussier
    if c2 < o2 and c1 > o1 and c1 > o2 and o1 < c2:
        return "ENGULFING_BULL"

    # Engulfing baissier
    if c2 > o2 and c1 < o1 and c1 < o2 and o1 > c2:
        return "ENGULFING_BEAR"

    # Doji (indécision)
    if body1 < 0.1 * range1:
        return "DOJI"

    return None


# ── NIVEAUX FIBONACCI (outil clé de Paul Tudor Jones, Gann) ────────────────────
def fibonacci_levels(df: pd.DataFrame) -> dict:
    c = df["Close"].squeeze()
    period = min(50, len(df))
    recent = c.tail(period)
    high = float(recent.max())
    low  = float(recent.min())
    diff = high - low
    return {
        "high":  high,
        "low":   low,
        "fib_786": high - 0.786 * diff,
        "fib_618": high - 0.618 * diff,
        "fib_5":   high - 0.500 * diff,
        "fib_382": high - 0.382 * diff,
        "fib_236": high - 0.236 * diff,
    }


# ── SCORE DE SIGNAL (système de notation multi-critères) ───────────────────────
def compute_signal_score(df: pd.DataFrame) -> tuple[str | None, int, list[str]]:
    """
    Retourne (direction, score, raisons[])
    Score >= 4 sur 7 = signal valide
    Inspiré du système de validation multiple de Stan Druckenmiller
    """
    if len(df) < 50:
        return None, 0, []

    last = df.iloc[-1]
    prev = df.iloc[-2]

    score_buy  = 0
    score_sell = 0
    reasons    = []

    c = float(last["Close"].squeeze() if hasattr(last["Close"], "squeeze") else last["Close"])
    ema9   = float(last["EMA9"])
    ema21  = float(last["EMA21"])
    ema50  = float(last["EMA50"])
    ema200 = float(last["EMA200"])
    rsi    = float(last["RSI"])
    macd   = float(last["MACD"])
    macd_s = float(last["MACD_signal"])
    macd_h = float(last["MACD_hist"])
    stk    = float(last["STOCH_K"])
    std    = float(last["STOCH_D"])
    bb_up  = float(last["BB_upper"])
    bb_low = float(last["BB_lower"])
    adx    = float(last["ADX"])
    wr     = float(last["WILLIAMS_R"])

    prev_macd = float(prev["MACD"])
    prev_macd_s = float(prev["MACD_signal"])
    prev_ema9 = float(prev["EMA9"])
    prev_ema21 = float(prev["EMA21"])

    # 1. TENDANCE PRINCIPALE (EMA 50 & 200 — Paul Tudor Jones)
    if c > ema200 and ema50 > ema200:
        score_buy += 1
        reasons.append("✅ Tendance long terme haussière (EMA200)")
    elif c < ema200 and ema50 < ema200:
        score_sell += 1
        reasons.append("✅ Tendance long terme baissière (EMA200)")

    # 2. CROISEMENT EMA 9/21 (Elder Triple Screen — Screen 2)
    cross_up   = prev_ema9 <= prev_ema21 and ema9 > ema21
    cross_down = prev_ema9 >= prev_ema21 and ema9 < ema21
    if cross_up:
        score_buy += 2
        reasons.append("✅ Croisement EMA 9 × EMA 21 haussier")
    elif cross_down:
        score_sell += 2
        reasons.append("✅ Croisement EMA 9 × EMA 21 baissier")
    elif ema9 > ema21:
        score_buy += 1
        reasons.append("✅ EMA 9 au-dessus EMA 21")
    else:
        score_sell += 1
        reasons.append("✅ EMA 9 en-dessous EMA 21")

    # 3. MACD (momentum confirme la direction)
    macd_cross_up   = prev_macd <= prev_macd_s and macd > macd_s
    macd_cross_down = prev_macd >= prev_macd_s and macd < macd_s
    if macd_cross_up or (macd > macd_s and macd_h > 0):
        score_buy += 1
        reasons.append("✅ MACD haussier")
    elif macd_cross_down or (macd < macd_s and macd_h < 0):
        score_sell += 1
        reasons.append("✅ MACD baissier")

    # 4. RSI (éviter surachat/survente — Wilder)
    if 40 <= rsi <= 65:
        score_buy += 1
        reasons.append(f"✅ RSI favorable achat ({rsi:.1f})")
    elif 35 <= rsi <= 60:
        score_sell += 1
        reasons.append(f"✅ RSI favorable vente ({rsi:.1f})")
    elif rsi > 75:
        score_sell += 1
        reasons.append(f"⚠️ RSI en surachat ({rsi:.1f})")
    elif rsi < 25:
        score_buy += 1
        reasons.append(f"⚠️ RSI en survente ({rsi:.1f})")

    # 5. STOCHASTIQUE (Lane — entrée précise)
    if stk > std and stk < 80:
        score_buy += 1
        reasons.append(f"✅ Stochastique haussier ({stk:.1f})")
    elif stk < std and stk > 20:
        score_sell += 1
        reasons.append(f"✅ Stochastique baissier ({stk:.1f})")

    # 6. ADX — force de la tendance (Richard Dennis : trade uniquement si tendance forte)
    if adx > 25:
        reasons.append(f"✅ ADX fort ({adx:.1f}) — tendance confirmée")
        if ema9 > ema21:
            score_buy += 1
        else:
            score_sell += 1
    else:
        reasons.append(f"⚠️ ADX faible ({adx:.1f}) — marché en consolidation")

    # 7. WILLIAMS %R (Larry Williams — timing d'entrée)
    if -80 <= wr <= -20 and wr > float(prev["WILLIAMS_R"]):
        score_buy += 1
        reasons.append(f"✅ Williams %R en zone d'achat ({wr:.1f})")
    elif -80 <= wr <= -20 and wr < float(prev["WILLIAMS_R"]):
        score_sell += 1
        reasons.append(f"✅ Williams %R en zone de vente ({wr:.1f})")

    # Déterminer direction
    threshold = 4
    if score_buy >= threshold and score_buy > score_sell:
        return "BUY", score_buy, reasons
    elif score_sell >= threshold and score_sell > score_buy:
        return "SELL", score_sell, reasons
    return None, max(score_buy, score_sell), reasons


# ── GESTION DES POSITIONS ──────────────────────────────────────────────────────
def open_trade(data: dict, ticker: str, direction: str,
               price: float, atr: float, score: int) -> dict | None:
    if data["daily_pnl"] <= -(data["capital"] * MAX_DAILY_LOSS):
        logger.info("Limite perte journalière atteinte")
        return None
    for p in data["open_positions"]:
        if p["ticker"] == ticker:
            return None

    # Stop-loss adaptatif (1.5× ATR — méthode Turtle Trading)
    sl_dist = atr * 1.5
    tp_dist = atr * 3.0   # ratio risque/récompense 1:2

    sl = price - sl_dist if direction == "BUY" else price + sl_dist
    tp = price + tp_dist if direction == "BUY" else price - tp_dist
    qty = round((data["capital"] * RISK_PER_TRADE) / sl_dist, 6)
    if qty <= 0:
        return None

    pos = {
        "ticker":      ticker,
        "direction":   direction,
        "entry_price": round(price, 5),
        "sl":          round(sl, 5),
        "tp":          round(tp, 5),
        "qty":         qty,
        "score":       score,
        "entry_time":  datetime.now(TZ).isoformat(),
        "pnl":         0.0,
    }
    data["open_positions"].append(pos)
    data["daily_trades"] += 1
    save_data(data)

    if sb_client:
        try:
            res = sb_client.table("trade_history").insert({
                "bot":         TICKER_TO_BOT.get(ticker, "gold"),
                "symbol":      ticker,
                "direction":   direction,
                "price_entry": round(price, 5),
                "status":      "open",
                "opened_at":   datetime.now(TZ).isoformat(),
            }).execute()
            if res.data:
                pos["supabase_id"] = res.data[0]["id"]
                save_data(data)
        except Exception as e:
            logger.error(f"Supabase insert trade: {e}")

    return pos

def check_exits(data: dict, ticker: str, price: float) -> list[tuple]:
    closed, remaining = [], []
    for pos in data["open_positions"]:
        if pos["ticker"] != ticker:
            remaining.append(pos)
            continue

        if pos["direction"] == "BUY":
            pnl    = (price - pos["entry_price"]) * pos["qty"]
            hit_sl = price <= pos["sl"]
            hit_tp = price >= pos["tp"]
        else:
            pnl    = (pos["entry_price"] - price) * pos["qty"]
            hit_sl = price >= pos["sl"]
            hit_tp = price <= pos["tp"]

        pos["pnl"] = round(pnl, 2)

        if hit_sl or hit_tp:
            reason = "✅ Take Profit" if hit_tp else "🛑 Stop Loss"
            pos["exit_price"] = round(price, 5)
            pos["exit_time"]  = datetime.now(TZ).isoformat()
            pos["exit_reason"] = reason
            data["closed_trades"].append(pos)
            data["daily_pnl"] += pnl
            data["total_pnl"] += pnl
            data["capital"]   += pnl
            if pnl > 0:
                data["win_streak"]  = data.get("win_streak", 0) + 1
                data["loss_streak"] = 0
            else:
                data["loss_streak"] = data.get("loss_streak", 0) + 1
                data["win_streak"]  = 0
            if sb_client and "supabase_id" in pos:
                try:
                    sb_client.table("trade_history").update({
                        "price_exit": round(price, 5),
                        "pnl":        round(pnl, 2),
                        "status":     "closed",
                        "closed_at":  datetime.now(TZ).isoformat(),
                    }).eq("id", pos["supabase_id"]).execute()
                except Exception as e:
                    logger.error(f"Supabase update trade: {e}")

            closed.append((pos, reason))
        else:
            remaining.append(pos)

    data["open_positions"] = remaining
    save_data(data)
    return closed


# ── GRAPHIQUES PROFESSIONNELS ──────────────────────────────────────────────────
def chart_instrument(ticker: str, name: str, data: dict) -> io.BytesIO | None:
    df = fetch(ticker, period="2d", interval="15m")
    if df is None or len(df) < 30:
        return None
    df = compute_indicators(df)
    c  = df["Close"].squeeze()

    fig = plt.figure(figsize=(14, 10), facecolor="#0a1428")
    gs  = fig.add_gridspec(4, 1, hspace=0.08,
                            height_ratios=[3, 1, 1, 1])
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    ax4 = fig.add_subplot(gs[3], sharex=ax1)

    for ax in (ax1, ax2, ax3, ax4):
        ax.set_facecolor("#0d1f3c")
        ax.tick_params(colors="#aaaaaa", labelsize=8)
        for sp in ax.spines.values():
            sp.set_color("#1e3a5f")

    # Prix + EMAs + Bollinger
    ax1.fill_between(df.index, df["BB_upper"].squeeze(), df["BB_lower"].squeeze(),
                     alpha=0.07, color="#4a90e2")
    ax1.plot(df.index, df["BB_upper"].squeeze(), color="#4a90e2", lw=0.8, ls="--")
    ax1.plot(df.index, df["BB_lower"].squeeze(), color="#4a90e2", lw=0.8, ls="--")
    ax1.plot(df.index, df["BB_mid"].squeeze(),   color="#4a90e2", lw=0.6, ls=":")
    ax1.plot(df.index, c,                color="#FFD700", lw=2,   label=name)
    ax1.plot(df.index, df["EMA9"].squeeze(),  color="#00bfff", lw=1.2, ls="--", label="EMA9")
    ax1.plot(df.index, df["EMA21"].squeeze(), color="#ff6347", lw=1.2, ls="--", label="EMA21")
    ax1.plot(df.index, df["EMA50"].squeeze(), color="#9b59b6", lw=1.0, ls="-.",  label="EMA50")

    # Fibonacci
    fibs = fibonacci_levels(df)
    fib_colors = ["#ff9999","#ffcc99","#ffff99","#99ff99","#99ccff"]
    fib_keys   = ["fib_786","fib_618","fib_5","fib_382","fib_236"]
    for fk, fc in zip(fib_keys, fib_colors):
        ax1.axhline(fibs[fk], color=fc, lw=0.7, ls=":", alpha=0.6)

    # Points d'entrée/sortie
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    for trade in data.get("closed_trades", []):
        if trade["ticker"] != ticker or trade.get("entry_time","")[:10] != today:
            continue
        try:
            et = pd.Timestamp(trade["entry_time"]).tz_convert(TZ)
            color  = "#00ff7f" if trade["direction"] == "BUY" else "#ff4500"
            marker = "^" if trade["direction"] == "BUY" else "v"
            ax1.scatter(et, trade["entry_price"], color=color,   marker=marker, s=150, zorder=5)
            if "exit_price" in trade:
                xt = pd.Timestamp(trade["exit_time"]).tz_convert(TZ)
                ex_color = "#00ff7f" if trade.get("pnl",0) > 0 else "#ff4500"
                ax1.scatter(xt, trade["exit_price"], color=ex_color, marker="x", s=100, zorder=5)
                ax1.plot([et, xt],
                         [trade["entry_price"], trade["exit_price"]],
                         color=color, lw=0.8, ls=":", alpha=0.5)
        except Exception:
            pass

    ax1.set_title(f"GOLD BOT — {name} — {datetime.now(TZ).strftime('%d/%m/%Y')}",
                  color="white", fontsize=13, fontweight="bold", pad=8)
    ax1.legend(facecolor="#0d1f3c", labelcolor="white", fontsize=8, loc="upper left")
    ax1.yaxis.set_tick_params(labelcolor="white")

    # MACD
    hist = df["MACD_hist"].squeeze()
    colors_hist = ["#00ff7f" if v >= 0 else "#ff4500" for v in hist]
    ax2.bar(df.index, hist, color=colors_hist, width=0.0005, alpha=0.8)
    ax2.plot(df.index, df["MACD"].squeeze(),        color="#00bfff", lw=1.2, label="MACD")
    ax2.plot(df.index, df["MACD_signal"].squeeze(),  color="#ff6347", lw=1.0, label="Signal")
    ax2.axhline(0, color="white", lw=0.5, alpha=0.4)
    ax2.set_ylabel("MACD", color="#aaaaaa", fontsize=8)
    ax2.legend(facecolor="#0d1f3c", labelcolor="white", fontsize=7, loc="upper left")

    # RSI
    rsi = df["RSI"].squeeze()
    ax3.plot(df.index, rsi, color="#a78bfa", lw=1.5)
    ax3.axhline(70, color="#ff6347", ls="--", alpha=0.7, lw=1)
    ax3.axhline(50, color="white",   ls=":",  alpha=0.3, lw=0.8)
    ax3.axhline(30, color="#00ff7f", ls="--", alpha=0.7, lw=1)
    ax3.fill_between(df.index, rsi, 70, where=(rsi >= 70), alpha=0.2, color="#ff6347")
    ax3.fill_between(df.index, rsi, 30, where=(rsi <= 30), alpha=0.2, color="#00ff7f")
    ax3.set_ylim(0, 100)
    ax3.set_ylabel("RSI", color="#aaaaaa", fontsize=8)

    # Stochastique
    stk = df["STOCH_K"].squeeze()
    std = df["STOCH_D"].squeeze()
    ax4.plot(df.index, stk, color="#f39c12", lw=1.2, label="%K")
    ax4.plot(df.index, std, color="#e74c3c", lw=1.0, label="%D")
    ax4.axhline(80, color="#ff6347", ls="--", alpha=0.6, lw=0.8)
    ax4.axhline(20, color="#00ff7f", ls="--", alpha=0.6, lw=0.8)
    ax4.set_ylim(0, 100)
    ax4.set_ylabel("Stoch", color="#aaaaaa", fontsize=8)
    ax4.legend(facecolor="#0d1f3c", labelcolor="white", fontsize=7, loc="upper left")

    plt.setp(ax1.get_xticklabels(), visible=False)
    plt.setp(ax2.get_xticklabels(), visible=False)
    plt.setp(ax3.get_xticklabels(), visible=False)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close()
    buf.seek(0)
    return buf

def chart_capital(data: dict) -> io.BytesIO | None:
    trades = data.get("closed_trades", [])
    if len(trades) < 2:
        return None

    capital = CAPITAL_INITIAL
    caps, dates_idx, win_counts, trade_counts = [], [], [], []
    wins = 0
    for i, t in enumerate(trades):
        capital += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            wins += 1
        caps.append(capital)
        trade_counts.append(i + 1)
        win_counts.append(wins / (i + 1) * 100)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), facecolor="#0a1428",
                                    gridspec_kw={"height_ratios": [2, 1]})
    for ax in (ax1, ax2):
        ax.set_facecolor("#0d1f3c")
        ax.tick_params(colors="#aaaaaa")
        for sp in ax.spines.values():
            sp.set_color("#1e3a5f")

    color = "#00ff7f" if caps[-1] >= CAPITAL_INITIAL else "#ff4500"
    ax1.plot(trade_counts, caps, color=color, lw=2.5)
    ax1.fill_between(trade_counts, caps, CAPITAL_INITIAL,
                     where=[c >= CAPITAL_INITIAL for c in caps], alpha=0.15, color="#00ff7f")
    ax1.fill_between(trade_counts, caps, CAPITAL_INITIAL,
                     where=[c < CAPITAL_INITIAL for c in caps], alpha=0.15, color="#ff4500")
    ax1.axhline(CAPITAL_INITIAL, color="white", ls="--", alpha=0.4, lw=1)
    pct = (caps[-1] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100
    ax1.set_title(f"Évolution du Capital — Performance : {pct:+.2f}%",
                  color="white", fontsize=12, fontweight="bold")
    ax1.set_ylabel("Capital (EUR)", color="#aaaaaa")

    ax2.plot(trade_counts, win_counts, color="#f39c12", lw=2)
    ax2.axhline(50, color="white", ls="--", alpha=0.4, lw=1)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("Taux réussite (%)", color="#aaaaaa")
    ax2.set_xlabel("Nombre de trades", color="#aaaaaa")

    plt.tight_layout(pad=1.5)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close()
    buf.seek(0)
    return buf


# ── IA PRÉDICTIVE — PROMPT INSPIRÉ DES PLUS GRANDS TRADERS ────────────────────
async def ai_prediction(instruments: dict, data: dict) -> str:
    if not GEMINI_API_KEY:
        return "Analyse IA non configurée."
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")

        market_lines = ""
        for ticker, info in instruments.items():
            df = fetch(ticker, period="10d", interval="1h")
            if df is not None and not df.empty:
                df = compute_indicators(df)
                c    = df["Close"].squeeze()
                price = float(c.iloc[-1])
                chg24 = float((c.iloc[-1] / c.iloc[-24] - 1) * 100) if len(c) >= 24 else 0
                chg7d = float((c.iloc[-1] / c.iloc[-168] - 1) * 100) if len(c) >= 168 else 0
                rsi   = float(df["RSI"].iloc[-1])
                macd  = float(df["MACD"].iloc[-1])
                adx   = float(df["ADX"].iloc[-1])
                ema50 = float(df["EMA50"].iloc[-1])
                ema200 = float(df["EMA200"].iloc[-1])
                bb_w  = float(df["BB_width"].iloc[-1])
                fibs  = fibonacci_levels(df)
                market_lines += (
                    f"\n\n{info['name']}:"
                    f"\n  Prix: {price:.4f} | Var 24h: {chg24:+.2f}% | Var 7j: {chg7d:+.2f}%"
                    f"\n  RSI: {rsi:.1f} | MACD: {macd:.4f} | ADX: {adx:.1f}"
                    f"\n  EMA50: {ema50:.4f} | EMA200: {ema200:.4f}"
                    f"\n  Largeur BB: {bb_w:.3f} (volatilité)"
                    f"\n  Fibonacci: Support {fibs['fib_618']:.4f} | Résistance {fibs['fib_236']:.4f}"
                )

        closed_today = [t for t in data.get("closed_trades",[])
                        if t.get("entry_time","")[:10] == datetime.now(TZ).strftime("%Y-%m-%d")]
        wins_today = [t for t in closed_today if t.get("pnl",0) > 0]

        prompt = f"""Tu es un système d'intelligence artificielle de trading de niveau institutionnel.
Tu combines les philosophies et techniques des plus grands traders de l'histoire :

- PAUL TUDOR JONES : Analyse multi-timeframes, préservation du capital avant tout, "5:1 risk/reward"
- GEORGE SOROS : Réflexivité des marchés, identifier les déséquilibres macro, conviction forte
- JESSE LIVERMORE : "The market is never wrong, opinions often are" — suivre le prix, pas les opinions
- RICHARD DENNIS (Turtle Trading) : Breakout, trend-following rigoureux, couper les pertes rapidement
- STANLEY DRUCKENMILLER : Concentration sur les meilleures opportunités, ne pas sur-trader
- ELDER ALEXANDER : Triple Screen System — confirmer sur plusieurs timeframes avant d'entrer
- LARRY WILLIAMS : Cyclicité des marchés, timing précis des entrées
- AL BROOKS (Price Action) : Lire les chandeliers, structure du marché, momentum

DONNÉES DE MARCHÉ ACTUELLES :{market_lines}

ÉTAT DU PORTEFEUILLE :
- Capital: {data['capital']:.2f} EUR (initial: {CAPITAL_INITIAL:.2f} EUR)
- P&L total: {data['total_pnl']:+.2f} EUR
- Trades fermés: {len(data.get('closed_trades',[]))}
- Trades aujourd'hui: {len(closed_today)} (gagnants: {len(wins_today)})
- Série victoires: {data.get('win_streak',0)} | Série défaites: {data.get('loss_streak',0)}

ANALYSE REQUISE (en français, ton professionnel, maximum 5 lignes) :
1. Biais directionnel dominant (Haussier/Neutre/Baissier) avec % de confiance
2. Niveau de support/résistance clé à surveiller aujourd'hui
3. Risque principal du jour (macro, technique, ou sentiment)
4. Recommandation précise pour le bot (opportunité, prudence, ou pause)
5. Sentiment du marché selon la théorie de Soros (réflexivité — consensus vs réalité)

Sois précis, factuel, et pense comme un professionnel gérant des millions."""

        resp = model.generate_content(prompt)
        return resp.text
    except Exception as e:
        logger.error(f"Erreur IA: {e}")
        return f"Analyse IA indisponible ({str(e)[:80]})"


# ── RAPPORTS ───────────────────────────────────────────────────────────────────
async def morning_report(app: Application):
    data        = load_data()
    instruments = get_instruments()
    now         = datetime.now(TZ)
    today       = now.strftime("%Y-%m-%d")

    if data["last_reset"] != today:
        data["daily_pnl"]    = 0.0
        data["daily_trades"] = 0
        data["last_reset"]   = today
        save_data(data)

    prices_txt = ""
    for ticker, info in instruments.items():
        df = fetch(ticker)
        if df is not None and not df.empty:
            df2 = compute_indicators(df)
            price = float(df2["Close"].squeeze().iloc[-1])
            rsi   = float(df2["RSI"].iloc[-1])
            adx   = float(df2["ADX"].iloc[-1])
            prices_txt += (f"{info['emoji']} *{info['name']}* : `{price:.4f}` "
                           f"| RSI: `{rsi:.1f}` | ADX: `{adx:.1f}`\n")

    ia_txt = await ai_prediction(instruments, data)
    mode   = "Week-end — Mode CRYPTO 🪙" if now.weekday() >= 5 else "Semaine — Mode MÉTAUX 🥇🥈"
    pct    = (data["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100

    msg = (
        f"🌅 *GOLD BOT — Rapport du Matin*\n"
        f"📅 {now.strftime('%A %d %B %Y')} | {mode}\n\n"
        f"*Prix & Indicateurs :*\n{prices_txt}\n"
        f"*🤖 Analyse IA (niveau institutionnel) :*\n{ia_txt}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Capital : `{data['capital']:.2f} EUR`\n"
        f"📈 Performance : `{pct:+.2f}%`\n"
        f"🔄 Trades totaux : `{len(data['closed_trades'])}`"
    )
    try:
        await app.bot.send_message(JOHN_ID, msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erreur envoi rapport matin: {e}")
    logger.info("Rapport matin envoyé")

async def evening_report(app: Application):
    data        = load_data()
    instruments = get_instruments()
    now         = datetime.now(TZ)
    today       = now.strftime("%Y-%m-%d")

    today_trades = [t for t in data["closed_trades"] if t.get("entry_time","")[:10] == today]
    wins    = [t for t in today_trades if t.get("pnl", 0) > 0]
    losses  = [t for t in today_trades if t.get("pnl", 0) <= 0]
    wr      = (len(wins) / len(today_trades) * 100) if today_trades else 0
    best    = max(today_trades, key=lambda x: x.get("pnl", 0), default=None)
    worst   = min(today_trades, key=lambda x: x.get("pnl", 0), default=None)
    best_s  = f"`{best['pnl']:+.2f} EUR` ({best['ticker']})"   if best  else "Aucun"
    worst_s = f"`{worst['pnl']:+.2f} EUR` ({worst['ticker']})" if worst else "Aucun"
    pct     = (data["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100

    all_closed = data.get("closed_trades", [])
    all_wins   = [t for t in all_closed if t.get("pnl", 0) > 0]
    global_wr  = (len(all_wins) / len(all_closed) * 100) if all_closed else 0

    msg = (
        f"🌙 *GOLD BOT — Rapport du Soir*\n"
        f"📅 {now.strftime('%d/%m/%Y')}\n\n"
        f"*📊 Résultats du jour :*\n"
        f"• Trades exécutés : `{len(today_trades)}`\n"
        f"• Gagnants : `{len(wins)}` ✅ | Perdants : `{len(losses)}` ❌\n"
        f"• Taux de réussite du jour : `{wr:.1f}%`\n"
        f"• P&L du jour : `{data['daily_pnl']:+.2f} EUR`\n\n"
        f"🏆 Meilleur trade : {best_s}\n"
        f"💔 Pire trade : {worst_s}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Capital actuel : `{data['capital']:.2f} EUR`\n"
        f"📈 Performance totale : `{pct:+.2f}%`\n"
        f"🎯 Taux réussite global : `{global_wr:.1f}%`\n"
        f"🔄 Trades totaux : `{len(all_closed)}`\n"
        f"🏅 Série victoires : `{data.get('win_streak',0)}`"
    )
    try:
        await app.bot.send_message(JOHN_ID, msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erreur envoi rapport soir: {e}")

    for ticker, info in instruments.items():
        buf = chart_instrument(ticker, info["name"], data)
        if buf:
            try:
                await app.bot.send_photo(JOHN_ID, buf,
                    caption=f"{info['emoji']} {info['name']} — {today}")
                await asyncio.sleep(1)
            except Exception:
                pass

    cap_buf = chart_capital(data)
    if cap_buf:
        try:
            await app.bot.send_photo(JOHN_ID, cap_buf,
                caption="📈 Évolution du Capital & Taux de réussite")
        except Exception:
            pass
    logger.info("Rapport soir envoyé")


# ── BOUCLE DE TRADING ──────────────────────────────────────────────────────────
async def trading_loop(app: Application):
    logger.info("Boucle de trading démarrée — vérification toutes les 15 min")
    while True:
        try:
            data        = load_data()
            instruments = get_instruments()

            for ticker, info in instruments.items():
                df = fetch(ticker)
                if df is None or len(df) < 50:
                    continue

                df    = compute_indicators(df)
                price = float(df["Close"].squeeze().iloc[-1])
                atr   = float(df["ATR"].iloc[-1])

                if pd.isna(atr) or atr <= 0:
                    continue

                # Vérifier sorties
                exits = check_exits(data, ticker, price)
                data  = load_data()
                for pos, reason in exits:
                    pnl_e = pos.get("pnl", 0)
                    em = "✅" if pnl_e > 0 else "❌"
                    msg = (
                        f"{em} *Trade fermé — {info['name']}*\n"
                        f"Direction : {pos['direction']} (Score: {pos.get('score','?')}/7)\n"
                        f"Entrée : `{pos['entry_price']:.4f}` → Sortie : `{price:.4f}`\n"
                        f"Raison : {reason}\n"
                        f"P&L : `{pnl_e:+.2f} EUR` | Capital : `{data['capital']:.2f} EUR`"
                    )
                    try:
                        await app.bot.send_message(JOHN_ID, msg, parse_mode="Markdown")
                    except Exception:
                        pass

                # Pattern chandeliers
                pattern = detect_candlestick_pattern(df)

                # Signal multi-critères
                direction, score, reasons = compute_signal_score(df)

                if direction:
                    pos = open_trade(data, ticker, direction, price, atr, score)
                    data = load_data()
                    if pos:
                        em  = "📈" if direction == "BUY" else "📉"
                        pat = f"\n📊 Pattern : `{pattern}`" if pattern else ""
                        msg = (
                            f"{em} *Nouveau trade — {info['name']}*\n"
                            f"Direction : *{direction}* | Score : `{score}/7`\n"
                            f"Prix entrée : `{price:.4f}`\n"
                            f"Stop-Loss : `{pos['sl']:.4f}`\n"
                            f"Take-Profit : `{pos['tp']:.4f}`\n"
                            f"Quantité : `{pos['qty']}`{pat}\n\n"
                            f"*Confirmations :*\n" +
                            "\n".join(reasons[:4])
                        )
                        try:
                            await app.bot.send_message(JOHN_ID, msg, parse_mode="Markdown")
                        except Exception:
                            pass

        except Exception as e:
            logger.error(f"Erreur boucle trading: {e}")

        await asyncio.sleep(15 * 60)


# ── PLANIFICATEUR ──────────────────────────────────────────────────────────────
async def scheduler(app: Application):
    last_morning = ""
    last_evening = ""
    while True:
        now   = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        h, m  = now.hour, now.minute

        if h == 7 and m < 15 and last_morning != today:
            await morning_report(app)
            last_morning = today

        if h == 22 and m < 15 and last_evening != today:
            await evening_report(app)
            last_evening = today

        await asyncio.sleep(60)


# ── COMMANDES TELEGRAM ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"🤖 *GOLD BOT — Project Inves'T*\n\n"
        f"Système de trading 7j/7 avec IA avancée\n\n"
        f"Ton Chat ID : `{cid}`\n\n"
        f"*Commandes :*\n"
        f"/status — Positions en cours\n"
        f"/rapport — Rapport + graphiques immédiat\n"
        f"/capital — État du capital\n"
        f"/signal — Analyse du marché maintenant\n"
        f"/myid — Voir ton Chat ID",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    all_inst = {**WEEKDAY_INSTRUMENTS, **WEEKEND_INSTRUMENTS}
    if not data["open_positions"]:
        await update.message.reply_text("📊 Aucune position ouverte actuellement.")
        return
    msg = "📊 *Positions ouvertes :*\n\n"
    for pos in data["open_positions"]:
        info = all_inst.get(pos["ticker"], {"name": pos["ticker"], "emoji": "📊"})
        df   = fetch(pos["ticker"])
        cur  = float(df["Close"].squeeze().iloc[-1]) if df is not None and not df.empty else pos["entry_price"]
        pnl  = (cur - pos["entry_price"]) * pos["qty"] if pos["direction"] == "BUY" else (pos["entry_price"] - cur) * pos["qty"]
        em   = "✅" if pnl > 0 else "🔴"
        msg += f"{em} *{info['name']}* — {pos['direction']} (Score: {pos.get('score','?')}/7)\n"
        msg += f"Entrée : `{pos['entry_price']:.4f}` | Actuel : `{cur:.4f}`\n"
        msg += f"SL : `{pos['sl']:.4f}` | TP : `{pos['tp']:.4f}`\n"
        msg += f"P&L latent : `{pnl:+.2f} EUR`\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_rapport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Génération du rapport complet...")
    await evening_report(context.application)

async def cmd_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    pct  = (data["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100
    all_closed = data.get("closed_trades", [])
    wins = [t for t in all_closed if t.get("pnl", 0) > 0]
    wr   = (len(wins) / len(all_closed) * 100) if all_closed else 0
    msg  = (
        f"💰 *État du Capital — GOLD BOT*\n\n"
        f"Capital initial : `{CAPITAL_INITIAL:.2f} EUR`\n"
        f"Capital actuel : `{data['capital']:.2f} EUR`\n"
        f"Performance : `{pct:+.2f}%`\n"
        f"P&L total : `{data['total_pnl']:+.2f} EUR`\n\n"
        f"Trades fermés : `{len(all_closed)}`\n"
        f"Taux de réussite global : `{wr:.1f}%`\n"
        f"P&L aujourd'hui : `{data['daily_pnl']:+.2f} EUR`\n\n"
        f"🏅 Série victoires : `{data.get('win_streak',0)}`\n"
        f"📉 Série défaites : `{data.get('loss_streak',0)}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analyse du marché en cours...")
    data        = load_data()
    instruments = get_instruments()
    msg = "🔍 *Analyse du marché — GOLD BOT*\n\n"
    for ticker, info in instruments.items():
        df = fetch(ticker)
        if df is None or len(df) < 50:
            continue
        df    = compute_indicators(df)
        price = float(df["Close"].squeeze().iloc[-1])
        direction, score, reasons = compute_signal_score(df)
        pattern = detect_candlestick_pattern(df)
        fibs  = fibonacci_levels(df)
        rsi   = float(df["RSI"].iloc[-1])
        adx   = float(df["ADX"].iloc[-1])

        sig_txt = f"*{direction}* (Score: {score}/7)" if direction else f"Pas de signal ({score}/7 requis: 4)"
        msg += (
            f"{info['emoji']} *{info['name']}*\n"
            f"Prix : `{price:.4f}`\n"
            f"Signal : {sig_txt}\n"
            f"RSI : `{rsi:.1f}` | ADX : `{adx:.1f}`\n"
            f"Support Fib : `{fibs['fib_618']:.4f}`\n"
            f"Pattern : `{pattern or 'Aucun'}`\n"
            f"Confirmations : {len(reasons)}/7\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ton Chat ID : `{update.effective_chat.id}`",
        parse_mode="Markdown"
    )


# ── MAIN ───────────────────────────────────────────────────────────────────────
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("rapport", cmd_rapport))
    app.add_handler(CommandHandler("capital", cmd_capital))
    app.add_handler(CommandHandler("signal",  cmd_signal))
    app.add_handler(CommandHandler("myid",    cmd_myid))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    if JOHN_ID:
        try:
            await app.bot.send_message(
                JOHN_ID,
                "🟢 *GOLD BOT démarré !*\n\n"
                "🥇 Or + 🥈 Argent (lun-ven)\n"
                "₿ Bitcoin (sam-dim)\n\n"
                "7 indicateurs — 5 stratégies de légende\n"
                "Rapports automatiques 7h & 22h\n\n"
                "Envoie /start pour voir les commandes",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    logger.info("GOLD BOT démarré — 7j/7")
    await asyncio.gather(
        trading_loop(app),
        scheduler(app),
    )

if __name__ == "__main__":
    asyncio.run(main())
