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
import re
from datetime import datetime

import pytz
import feedparser
import pandas as pd
import numpy as np
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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
logger.info(f"Supabase URL présente: {bool(SUPABASE_URL)} | Service key présente: {bool(SUPABASE_SERVICE_KEY)}")
sb_client: Client | None = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    try:
        sb_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        logger.info("Supabase connecté")
    except Exception as e:
        logger.error(f"Supabase connexion échouée: {e}")
else:
    logger.warning("Supabase désactivé — variables manquantes")

# Client séparé pour wiki_knowledge (projet Sofia/Elise — email différent)
WIKI_SUPABASE_URL = ENV.get("WIKI_SUPABASE_URL", "")
WIKI_SUPABASE_KEY = ENV.get("WIKI_SUPABASE_KEY", "")
wiki_sb_client: Client | None = None
if WIKI_SUPABASE_URL and WIKI_SUPABASE_KEY:
    try:
        wiki_sb_client = create_client(WIKI_SUPABASE_URL, WIKI_SUPABASE_KEY)
        logger.info("Wiki Supabase connecté")
    except Exception as e:
        logger.error(f"Wiki Supabase connexion échouée: {e}")
else:
    logger.warning("Wiki Supabase désactivé — WIKI_SUPABASE_URL / WIKI_SUPABASE_KEY manquants")

TICKER_TO_BOT = {
    "XAUUSD=X": "gold",
    "XAGUSD=X": "silver",
    "BTC-USD":  "oracle",
}
RISK_PER_TRADE  = 0.01   # 1 % du capital par trade (Jesse Livermore : préserver le capital)
MAX_DAILY_LOSS  = 0.05   # 5 % de perte max par jour (phase test)
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
def _default_state() -> dict:
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

def load_data_from_supabase() -> dict:
    """Reconstruit l'état depuis Supabase après un restart Railway."""
    base = _default_state()
    if not sb_client:
        return base
    try:
        # Capital + état journalier
        s_res = sb_client.table("bot_state").select("*").eq("id", 1).execute()
        if s_res.data:
            s = s_res.data[0]
            base["capital"]      = float(s.get("capital") or CAPITAL_INITIAL)
            base["total_pnl"]    = float(s.get("total_pnl") or 0)
            base["daily_pnl"]    = float(s.get("daily_pnl") or 0)
            base["daily_trades"] = int(s.get("daily_trades") or 0)
            base["win_streak"]   = int(s.get("win_streak") or 0)
            base["loss_streak"]  = int(s.get("loss_streak") or 0)
            base["last_reset"]   = str(s.get("last_reset") or base["last_reset"])[:10]

        # Positions ouvertes (avec SL/TP pour gérer les sorties)
        o_res = sb_client.table("trade_history").select("*").eq("status", "open").execute()
        for row in (o_res.data or []):
            if row.get("sl") and row.get("tp") and row.get("qty"):
                base["open_positions"].append({
                    "ticker":      row["symbol"],
                    "direction":   row["direction"],
                    "entry_price": float(row["price_entry"]),
                    "sl":          float(row["sl"]),
                    "tp":          float(row["tp"]),
                    "qty":         float(row["qty"]),
                    "score":       int(row.get("score") or 0),
                    "entry_time":  str(row.get("opened_at") or ""),
                    "pnl":         0.0,
                    "supabase_id": row["id"],
                })

        logger.info(
            f"State Supabase chargé — capital: {base['capital']:.2f} EUR, "
            f"{len(base['open_positions'])} positions ouvertes récupérées"
        )
    except Exception as e:
        logger.error(f"load_data_from_supabase: {e}")
    return base

def load_data() -> dict:
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            return json.load(f)
    # trades.json absent (restart Railway) → reconstruire depuis Supabase
    logger.warning("trades.json absent — reconstruction depuis Supabase")
    data = load_data_from_supabase()
    save_data(data)
    return data

def save_data(data: dict):
    with open(TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)
    # Sync état capital vers Supabase (persistance cross-restart)
    if sb_client:
        try:
            sb_client.table("bot_state").upsert({
                "id":           1,
                "capital":      round(data["capital"], 2),
                "total_pnl":    round(data["total_pnl"], 2),
                "daily_pnl":    round(data["daily_pnl"], 2),
                "daily_trades": data["daily_trades"],
                "win_streak":   data.get("win_streak", 0),
                "loss_streak":  data.get("loss_streak", 0),
                "last_reset":   data.get("last_reset"),
                "updated_at":   datetime.now(TZ).isoformat(),
            }).execute()
        except Exception as e:
            logger.error(f"save_data Supabase sync: {e}")

def get_instruments() -> dict:
    return WEEKEND_INSTRUMENTS if datetime.now(TZ).weekday() >= 5 else WEEKDAY_INSTRUMENTS


# ── FETCH DONNÉES ──────────────────────────────────────────────────────────────
TICKER_FALLBACKS = {
    "XAUUSD=X": ["GC=F", "XAUUSD=X"],
    "XAGUSD=X": ["SI=F", "XAGUSD=X"],
    "BTC-USD":  ["BTC-USD"],
}

def _is_rate_limit(e: Exception) -> bool:
    s = str(e).lower()
    return "too many requests" in s or "rate limit" in s or "ratelimit" in s

async def fetch_async(ticker: str, period: str = "5d", interval: str = "15m"):
    """Wrapper non-bloquant — exécute fetch() dans thread pool."""
    return await asyncio.to_thread(fetch, ticker, period, interval)

def fetch(ticker: str, period: str = "5d", interval: str = "15m") -> pd.DataFrame | None:
    tickers_to_try = TICKER_FALLBACKS.get(ticker, [ticker])
    for t in tickers_to_try:
        # Ticker.history — tentative unique, pas de sleep (appelé depuis async)
        try:
            df = yf.Ticker(t).history(period=period, interval=interval, auto_adjust=True)
            if df is not None and not df.empty and len(df) >= 10:
                logger.info(f"Fetch OK: {t} — {len(df)} bougies")
                return df
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning(f"Rate limit {t} — skip")
                return None
            logger.error(f"Erreur fetch {t}: {e}")
        # Fallback: yf.download — tentative unique
        try:
            df = yf.download(t, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                if len(df) >= 10:
                    logger.info(f"Fetch OK (download): {t} — {len(df)} bougies")
                    return df
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning(f"Rate limit download {t} — skip")
                return None
            logger.error(f"Erreur download {t}: {e}")
    logger.error(f"Fetch échoué pour {ticker}")
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
    reasons_buy  = []
    reasons_sell = []

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
    adx    = float(last["ADX"])
    wr     = float(last["WILLIAMS_R"])

    prev_macd   = float(prev["MACD"])
    prev_macd_s = float(prev["MACD_signal"])
    prev_ema9   = float(prev["EMA9"])
    prev_ema21  = float(prev["EMA21"])

    # 1. TENDANCE PRINCIPALE (EMA 50 & 200 — Paul Tudor Jones)
    if c > ema200 and ema50 > ema200:
        score_buy += 1
        reasons_buy.append("✅ Tendance long terme haussière (EMA200)")
    elif c < ema200 and ema50 < ema200:
        score_sell += 1
        reasons_sell.append("✅ Tendance long terme baissière (EMA200)")

    # 2. CROISEMENT EMA 9/21 (Elder Triple Screen — Screen 2)
    cross_up   = prev_ema9 <= prev_ema21 and ema9 > ema21
    cross_down = prev_ema9 >= prev_ema21 and ema9 < ema21
    if cross_up:
        score_buy += 2
        reasons_buy.append("✅ Croisement EMA 9 × EMA 21 haussier")
    elif cross_down:
        score_sell += 2
        reasons_sell.append("✅ Croisement EMA 9 × EMA 21 baissier")
    elif ema9 > ema21:
        score_buy += 1
        reasons_buy.append("✅ EMA 9 au-dessus EMA 21")
    else:
        score_sell += 1
        reasons_sell.append("✅ EMA 9 en-dessous EMA 21")

    # 3. MACD (momentum confirme la direction)
    macd_cross_up   = prev_macd <= prev_macd_s and macd > macd_s
    macd_cross_down = prev_macd >= prev_macd_s and macd < macd_s
    if macd_cross_up or (macd > macd_s and macd_h > 0):
        score_buy += 1
        reasons_buy.append("✅ MACD haussier")
    elif macd_cross_down or (macd < macd_s and macd_h < 0):
        score_sell += 1
        reasons_sell.append("✅ MACD baissier")

    # 4. RSI (zones resserrées — Wilder)
    if 48 <= rsi <= 62:
        score_buy += 1
        reasons_buy.append(f"✅ RSI favorable achat ({rsi:.1f})")
    elif 38 <= rsi <= 52:
        score_sell += 1
        reasons_sell.append(f"✅ RSI favorable vente ({rsi:.1f})")
    elif rsi > 75:
        score_sell += 1
        reasons_sell.append(f"⚠️ RSI en surachat ({rsi:.1f})")
    elif rsi < 25:
        score_buy += 1
        reasons_buy.append(f"⚠️ RSI en survente ({rsi:.1f})")

    # 5. STOCHASTIQUE (Lane — entrée précise)
    if stk > std and stk < 80:
        score_buy += 1
        reasons_buy.append(f"✅ Stochastique haussier ({stk:.1f})")
    elif stk < std and stk > 20:
        score_sell += 1
        reasons_sell.append(f"✅ Stochastique baissier ({stk:.1f})")

    # 6. ADX — force de la tendance (Richard Dennis) — minimum 30
    if adx > 30:
        if ema9 > ema21:
            score_buy += 1
            reasons_buy.append(f"✅ ADX fort ({adx:.1f}) — tendance haussière confirmée")
        else:
            score_sell += 1
            reasons_sell.append(f"✅ ADX fort ({adx:.1f}) — tendance baissière confirmée")
    else:
        reasons_buy.append(f"⚠️ ADX faible ({adx:.1f}) — marché en consolidation")
        reasons_sell.append(f"⚠️ ADX faible ({adx:.1f}) — marché en consolidation")

    # 7. WILLIAMS %R (Larry Williams — timing d'entrée)
    if -80 <= wr <= -20 and wr > float(prev["WILLIAMS_R"]):
        score_buy += 1
        reasons_buy.append(f"✅ Williams %R en zone d'achat ({wr:.1f})")
    elif -80 <= wr <= -20 and wr < float(prev["WILLIAMS_R"]):
        score_sell += 1
        reasons_sell.append(f"✅ Williams %R en zone de vente ({wr:.1f})")

    threshold = 5
    if score_buy >= threshold and score_buy > score_sell:
        return "BUY", score_buy, reasons_buy
    elif score_sell >= threshold and score_sell > score_buy:
        return "SELL", score_sell, reasons_sell
    return None, max(score_buy, score_sell), []


# ── MISE À JOUR PROFILES INVESTISSEURS ────────────────────────────────────────
def update_investor_profiles(pnl: float):
    """Distribue 70% du P&L du trade à tous les investisseurs proportionnellement."""
    if not sb_client or pnl == 0:
        return
    try:
        res = sb_client.table("profiles").select("id, capital_initial, capital_current, pnl_total").execute()
        profiles_data = res.data or []
        if not profiles_data:
            return

        total_capital = sum(float(p.get("capital_initial") or 0) for p in profiles_data)
        if total_capital <= 0:
            return

        investor_share = pnl * 0.70

        for p in profiles_data:
            cap_init = float(p.get("capital_initial") or 0)
            if cap_init <= 0:
                continue
            weight      = cap_init / total_capital
            gain        = investor_share * weight
            new_capital = float(p.get("capital_current") or cap_init) + gain
            new_pnl     = float(p.get("pnl_total") or 0) + gain
            sb_client.table("profiles").update({
                "capital_current": round(new_capital, 2),
                "pnl_total":       round(new_pnl, 2),
            }).eq("id", p["id"]).execute()

        logger.info(f"Profiles mis à jour — P&L: {pnl:+.2f} EUR distribué à {len(profiles_data)} investisseurs")
    except Exception as e:
        logger.error(f"Supabase update profiles: {e}")


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
                "sl":          round(sl, 5),
                "tp":          round(tp, 5),
                "qty":         round(qty, 6),
                "score":       score,
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

            update_investor_profiles(pnl)

            closed.append((pos, reason))
        else:
            remaining.append(pos)

    data["open_positions"] = remaining
    save_data(data)
    return closed


# ── LIGNES DE TENDANCE AUTOMATIQUES ───────────────────────────────────────────

def detect_pivots(df: pd.DataFrame, n: int = 5) -> tuple[list, list]:
    """Détecte pivots hauts et bas (n bougies de chaque côté)."""
    h = df["High"].squeeze().values
    l = df["Low"].squeeze().values
    idx = df.index

    pivot_highs, pivot_lows = [], []
    for i in range(n, len(df) - n):
        if h[i] == max(h[i - n:i + n + 1]):
            pivot_highs.append((idx[i], h[i]))
        if l[i] == min(l[i - n:i + n + 1]):
            pivot_lows.append((idx[i], l[i]))

    return pivot_highs, pivot_lows


def draw_trendlines(ax, df: pd.DataFrame,
                    pivot_highs: list, pivot_lows: list) -> str | None:
    """
    Trace lignes de tendance + détecte pattern (triangle/canal/wedge).
    Retourne nom du pattern ou None.
    """
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return None

    x_all = df.index
    # Convertir timestamps en float pour la pente
    def ts_float(ts):
        return ts.timestamp() if hasattr(ts, "timestamp") else float(ts.value)

    # Derniers 2 pivots hauts
    ph1, ph2 = pivot_highs[-2], pivot_highs[-1]
    x1h, y1h = ts_float(ph1[0]), ph1[1]
    x2h, y2h = ts_float(ph2[0]), ph2[1]
    slope_h  = (y2h - y1h) / (x2h - x1h) if x2h != x1h else 0

    # Derniers 2 pivots bas
    pl1, pl2 = pivot_lows[-2], pivot_lows[-1]
    x1l, y1l = ts_float(pl1[0]), pl1[1]
    x2l, y2l = ts_float(pl2[0]), pl2[1]
    slope_l  = (y2l - y1l) / (x2l - x1l) if x2l != x1l else 0

    # Projection sur toute la plage x
    x_start = ts_float(x_all[0])
    x_end   = ts_float(x_all[-1])

    def project(x_ref, y_ref, slope, x):
        return y_ref + slope * (x - x_ref)

    y_h_start = project(x2h, y2h, slope_h, x_start)
    y_h_end   = project(x2h, y2h, slope_h, x_end)
    y_l_start = project(x2l, y2l, slope_l, x_start)
    y_l_end   = project(x2l, y2l, slope_l, x_end)

    # Tracer résistance (rouge) et support (vert)
    ax.plot([x_all[0], x_all[-1]], [y_h_start, y_h_end],
            color="#ff4500", lw=1.5, ls="--", alpha=0.85, label="Résistance")
    ax.plot([x_all[0], x_all[-1]], [y_l_start, y_l_end],
            color="#00ff7f", lw=1.5, ls="--", alpha=0.85, label="Support")

    # Marqueurs pivots
    for ts, price in pivot_highs[-3:]:
        ax.scatter(ts, price, color="#ff4500", marker="v", s=60, zorder=6, alpha=0.7)
    for ts, price in pivot_lows[-3:]:
        ax.scatter(ts, price, color="#00ff7f", marker="^", s=60, zorder=6, alpha=0.7)

    # Remplissage canal
    ax.fill_between(
        [x_all[0], x_all[-1]],
        [y_h_start, y_h_end],
        [y_l_start, y_l_end],
        alpha=0.05, color="#ffffff"
    )

    # Détection pattern
    eps = abs(y2h - y1h) * 0.001  # tolérance pente nulle
    if slope_h < -eps and slope_l > eps:
        return "Triangle Symétrique ▲"
    elif slope_h < -eps and abs(slope_l) <= eps:
        return "Triangle Descendant ▽"
    elif abs(slope_h) <= eps and slope_l > eps:
        return "Triangle Ascendant △"
    elif slope_h < -eps and slope_l < -eps:
        return "Wedge Baissier ↘"
    elif slope_h > eps and slope_l > eps:
        return "Wedge Haussier ↗"
    elif abs(slope_h - slope_l) < eps * 5:
        dir_txt = "Haussier" if slope_h > 0 else "Baissier" if slope_h < 0 else "Neutre"
        return f"Canal {dir_txt} ↔"
    return None


# ── GRAPHIQUES PROFESSIONNELS ──────────────────────────────────────────────────
async def chart_instrument(ticker: str, name: str, data: dict) -> io.BytesIO | None:
    df = await fetch_async(ticker, period="2d", interval="15m")
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

    # Lignes de tendance automatiques
    pivot_highs, pivot_lows = detect_pivots(df, n=5)
    pattern_label = draw_trendlines(ax1, df, pivot_highs, pivot_lows)

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

    pat_str = f" — {pattern_label}" if pattern_label else ""
    ax1.set_title(f"GOLD BOT — {name} — {datetime.now(TZ).strftime('%d/%m/%Y')}{pat_str}",
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
            df = await fetch_async(ticker, period="10d", interval="1h")
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


# ── POLARIS ORACLE — RSS + IA PRÉDICTIVE ──────────────────────────────────────
ORACLE_RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://cryptopanic.com/news/rss/",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cryptoslate.com/feed/",
    "https://www.theblock.co/rss.xml",
]

ORACLE_KEYWORDS = [
    "bitcoin", "btc", "crypto", "fed", "inflation", "interest rate", "etf",
    "sec", "regulation", "whale", "halving", "macro", "recession", "dollar",
    "rate hike", "monetary", "blackrock", "microstrategy", "coinbase", "fomc",
    "cpi", "gdp", "tariff", "sanctions", "war", "geopolit",
]


def fetch_oracle_news(hours_back: int = 2) -> list[dict]:
    from email.utils import parsedate_to_datetime
    articles = []
    cutoff = datetime.now(pytz.utc) - pd.Timedelta(hours=hours_back)

    for url in ORACLE_RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:25]:
                title   = entry.get("title", "")
                summary = entry.get("summary", "")[:300]
                text_lower = (title + " " + summary).lower()
                if not any(kw in text_lower for kw in ORACLE_KEYWORDS):
                    continue
                try:
                    pub_dt = parsedate_to_datetime(entry.get("published", ""))
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=pytz.utc)
                    if pub_dt < cutoff:
                        continue
                except Exception:
                    pass
                articles.append({
                    "title":   title,
                    "summary": summary,
                    "source":  feed.feed.get("title", url),
                })
        except Exception as e:
            logger.warning(f"Oracle RSS {url}: {e}")

    seen, unique = set(), []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)
    return unique[:15]


async def oracle_ai_signal(articles: list[dict], btc_df: pd.DataFrame) -> dict:
    if not GEMINI_API_KEY:
        return {"direction": None, "confidence": 0, "summary": "Clé Gemini manquante"}
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model  = genai.GenerativeModel("gemini-2.5-flash")
        btc_df = compute_indicators(btc_df)
        c      = btc_df["Close"].squeeze()
        price  = float(c.iloc[-1])
        rsi    = float(btc_df["RSI"].iloc[-1])
        adx    = float(btc_df["ADX"].iloc[-1])
        ema200 = float(btc_df["EMA200"].iloc[-1])
        chg24  = float((c.iloc[-1] / c.iloc[-24] - 1) * 100) if len(c) >= 24 else 0

        news_text = "\n".join([
            f"- [{a['source']}] {a['title']} — {a['summary'][:150]}"
            for a in articles
        ])

        prompt = f"""Tu es Polaris Oracle — IA prédictive Bitcoin niveau institutionnel.
Tu analyses actualités macro + crypto pour prédire la direction à court terme.

ACTUALITÉS RÉCENTES (dernières {len(articles)} heures) :
{news_text}

DONNÉES TECHNIQUES BTC/USD :
- Prix : {price:.2f}$ | Variation 24h : {chg24:+.2f}%
- RSI : {rsi:.1f} | ADX : {adx:.1f}
- EMA200 : {ema200:.2f} | Tendance : {"HAUSSIÈRE" if price > ema200 else "BAISSIÈRE"}

Réponds UNIQUEMENT en JSON valide, exactement ce format :
{{
  "direction": "BUY" ou "SELL" ou "NEUTRAL",
  "confidence": <0-100>,
  "timeframe": "4h" ou "12h" ou "24h",
  "catalysts": ["raison 1", "raison 2"],
  "risk": "LOW" ou "MEDIUM" ou "HIGH",
  "summary": "<1 phrase>"
}}

Si pas d'info claire → NEUTRAL confidence < 50."""

        resp      = model.generate_content(prompt)
        json_match = re.search(r'\{.*\}', resp.text.strip(), re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return {"direction": None, "confidence": 0, "summary": "Réponse non parsable"}
    except Exception as e:
        logger.error(f"Oracle AI: {e}")
        return {"direction": None, "confidence": 0, "summary": str(e)[:80]}


async def oracle_loop(app: Application):
    logger.info("Polaris Oracle démarré — analyse RSS toutes les heures, 24h/24")
    last_hour = ""
    while True:
        try:
            now      = datetime.now(TZ)
            now_hour = now.strftime("%Y-%m-%d-%H")

            if now_hour != last_hour:
                last_hour = now_hour
                articles  = fetch_oracle_news(hours_back=2)

                if not articles:
                    logger.info("Oracle — pas de news pertinentes")
                    await asyncio.sleep(60 * 60)
                    continue

                btc_df = await fetch_async("BTC-USD", period="10d", interval="1h")
                if btc_df is None or len(btc_df) < 50:
                    await asyncio.sleep(60 * 60)
                    continue

                signal     = await oracle_ai_signal(articles, btc_df)
                direction  = signal.get("direction")
                confidence = int(signal.get("confidence", 0))
                risk       = signal.get("risk", "MEDIUM")
                risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(risk, "🟡")
                dir_emoji  = "📈" if direction == "BUY" else "📉" if direction == "SELL" else "⚖️"
                catalysts  = "\n".join([f"• {c}" for c in signal.get("catalysts", [])])

                msg = (
                    f"🔮 *POLARIS ORACLE — {now.strftime('%H:%M')}*\n\n"
                    f"{dir_emoji} Direction : *{direction or 'NEUTRAL'}* | Confiance : `{confidence}%`\n"
                    f"⏱ Timeframe : `{signal.get('timeframe', '?')}`\n"
                    f"{risk_emoji} Risque : `{risk}`\n\n"
                    f"*Catalyseurs :*\n{catalysts}\n\n"
                    f"📌 {signal.get('summary', '')}\n"
                    f"📰 `{len(articles)} articles analysés`"
                )
                if JOHN_ID:
                    try:
                        await app.bot.send_message(JOHN_ID, msg, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Oracle Telegram: {e}")

                if sb_client:
                    try:
                        sb_client.table("oracle_signals").insert({
                            "direction":      direction or "NEUTRAL",
                            "confidence":     confidence,
                            "timeframe":      signal.get("timeframe"),
                            "risk":           risk,
                            "catalysts":      json.dumps(signal.get("catalysts", []), ensure_ascii=False),
                            "summary":        signal.get("summary", ""),
                            "articles_count": len(articles),
                            "created_at":     now.isoformat(),
                        }).execute()
                        logger.info(f"Oracle signal écrit Supabase — {direction} {confidence}%")
                    except Exception as e:
                        logger.error(f"Oracle Supabase insert: {e}")

                if direction in ("BUY", "SELL") and confidence >= 75:
                    data    = load_data()
                    btc_15m = await fetch_async("BTC-USD", period="5d", interval="15m")
                    if btc_15m is not None and len(btc_15m) >= 50:
                        btc_15m = compute_indicators(btc_15m)
                        price   = float(btc_15m["Close"].squeeze().iloc[-1])
                        atr     = float(btc_15m["ATR"].iloc[-1])
                        if not pd.isna(atr) and atr > 0:
                            pos = open_trade(data, "BTC-USD", direction, price, atr, score=int(confidence / 10))
                            if pos and JOHN_ID:
                                try:
                                    await app.bot.send_message(
                                        JOHN_ID,
                                        f"🔮 *Oracle → Trade BTC ouvert*\n"
                                        f"Confiance `{confidence}%` ≥ 75% → position prise\n"
                                        f"Prix : `{price:.2f}$` | *{direction}*\n"
                                        f"SL : `{pos['sl']:.2f}$` | TP : `{pos['tp']:.2f}$`",
                                        parse_mode="Markdown"
                                    )
                                except Exception:
                                    pass

        except Exception as e:
            logger.error(f"Oracle loop: {e}")

        await asyncio.sleep(15 * 60)


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
        df = await fetch_async(ticker)
        if df is not None and not df.empty:
            df2 = compute_indicators(df)
            price = float(df2["Close"].squeeze().iloc[-1])
            rsi   = float(df2["RSI"].iloc[-1])
            adx   = float(df2["ADX"].iloc[-1])
            prices_txt += (f"{info['emoji']} *{info['name']}* : `{price:.4f}` "
                           f"| RSI: `{rsi:.1f}` | ADX: `{adx:.1f}`\n")
        else:
            prices_txt += f"{info['emoji']} *{info['name']}* : ⚠️ données indisponibles\n"
            logger.error(f"morning_report: fetch échoué pour {ticker}")

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
        buf = await chart_instrument(ticker, info["name"], data)
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
    await generate_daily_journal(app, data)


# ── AUTO-APPRENTISSAGE — JOURNAL + POST-MORTEM + AUDIT ─────────────────────────

def push_wiki_knowledge(slug: str, title: str, type_: str, summary: str, full_content: str):
    if not wiki_sb_client:
        logger.warning("Wiki push ignoré — WIKI_SUPABASE_URL/KEY non configurés")
        return
    try:
        wiki_sb_client.table("wiki_knowledge").upsert({
            "slug":         slug,
            "title":        title,
            "type":         type_,
            "summary":      summary[:1000],
            "full_content": full_content[:5000],
            "created_at":   datetime.now(TZ).isoformat(),
        }, on_conflict="slug").execute()
        logger.info(f"Wiki push OK — {slug}")
    except Exception as e:
        logger.error(f"Wiki push failed: {e}")


async def post_mortem_analysis(app: Application, pos: dict):
    """Analyse Gemini sur trade perdant → wiki_knowledge."""
    if not GEMINI_API_KEY:
        return
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
        pnl = pos.get("pnl", 0)

        prompt = f"""Tu es un coach de trading expert. Analyse ce trade perdant en 3 points concis (Markdown).

TRADE :
- Instrument : {pos.get('ticker')} | Direction : {pos.get('direction')}
- Entrée : {pos.get('entry_price')} → Sortie : {pos.get('exit_price', '?')}
- SL : {pos.get('sl')} | TP : {pos.get('tp')}
- P&L : {pnl:+.2f} EUR | Score signal : {pos.get('score', '?')}/7
- Raison sortie : {pos.get('exit_reason', 'Stop Loss')}

1. **Erreur principale** : Qu'est-ce qui a mal tourné ?
2. **Signal d'alerte manqué** : Quel indicateur aurait dû alerter ?
3. **Leçon** : Que faire différemment ?"""

        resp   = model.generate_content(prompt)
        lesson = resp.text.strip()
        today  = datetime.now(TZ).strftime("%Y-%m-%d")
        t_slug = pos.get('ticker','').replace('=','').replace('-','').lower()
        h_slug = (pos.get('entry_time','')[11:16] or "0000").replace(':','')
        slug   = f"postmortem-{t_slug}-{today}-{h_slug}"

        full = f"""---
title: "Post-mortem {pos.get('ticker')} {today}"
type: postmortem
created: {today}
---

## Trade
- {pos.get('ticker')} {pos.get('direction')} | Score {pos.get('score','?')}/7
- Entrée {pos.get('entry_price')} → Sortie {pos.get('exit_price','?')} | P&L {pnl:+.2f} EUR

## Analyse
{lesson}
"""
        push_wiki_knowledge(slug, f"Post-mortem {pos.get('ticker')} {today}", "postmortem", lesson[:500], full)

        if JOHN_ID:
            try:
                await app.bot.send_message(
                    JOHN_ID,
                    f"🧠 *Post-mortem — {pos.get('ticker')}*\n\nP&L : `{pnl:+.2f} EUR`\n\n{lesson[:600]}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Post-mortem: {e}")


async def generate_daily_journal(app: Application, data: dict):
    """Journal journalier Markdown → wiki_knowledge."""
    now   = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    today_trades = [t for t in data.get("closed_trades", []) if t.get("entry_time","")[:10] == today]
    wins   = [t for t in today_trades if t.get("pnl", 0) > 0]
    losses = [t for t in today_trades if t.get("pnl", 0) <= 0]
    wr     = (len(wins) / len(today_trades) * 100) if today_trades else 0
    pct    = (data["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100

    trades_md = "\n".join([
        f"- {'✅' if t.get('pnl',0)>0 else '❌'} {t.get('ticker')} {t.get('direction')} "
        f"Score {t.get('score','?')}/7 | P&L {t.get('pnl',0):+.2f} EUR | {t.get('exit_reason','?')}"
        for t in today_trades
    ]) or "- Aucun trade clôturé aujourd'hui."

    full = f"""---
title: "Journal Gold Bot {today}"
type: journal
created: {today}
---

## Summary
Journée {today} : {len(today_trades)} trades, taux réussite {wr:.1f}%, P&L {data['daily_pnl']:+.2f} EUR.

## Résultats
- Trades : {len(today_trades)} | Gagnants : {len(wins)} | Perdants : {len(losses)}
- Taux de réussite : {wr:.1f}%
- P&L du jour : {data['daily_pnl']:+.2f} EUR
- Capital : {data['capital']:.2f} EUR ({pct:+.2f}% total)
- Série victoires : {data.get('win_streak',0)} | Série défaites : {data.get('loss_streak',0)}

## Trades
{trades_md}
"""
    slug    = f"journal-goldbot-{today}"
    summary = f"Gold Bot {today} : {len(today_trades)} trades, {wr:.1f}% WR, P&L {data['daily_pnl']:+.2f} EUR"
    push_wiki_knowledge(slug, f"Journal Gold Bot {today}", "journal", summary, full)
    logger.info(f"Journal journalier poussé: {slug}")


async def weekly_audit(app: Application, data: dict):
    """Audit hebdomadaire dimanche — Gemini analyse + wiki push."""
    now     = datetime.now(TZ)
    today   = now.strftime("%Y-%m-%d")
    cutoff  = (now - pd.Timedelta(days=7)).strftime("%Y-%m-%d")

    week_trades = [t for t in data.get("closed_trades", []) if t.get("entry_time","")[:10] >= cutoff]
    wins      = [t for t in week_trades if t.get("pnl", 0) > 0]
    losses    = [t for t in week_trades if t.get("pnl", 0) <= 0]
    total_pnl = sum(t.get("pnl", 0) for t in week_trades)
    wr        = (len(wins) / len(week_trades) * 100) if week_trades else 0
    pct       = (data["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100
    best      = max(week_trades, key=lambda x: x.get("pnl", 0), default=None)
    worst     = min(week_trades, key=lambda x: x.get("pnl", 0), default=None)

    analysis = "Analyse indisponible."
    if GEMINI_API_KEY:
        try:
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel("gemini-2.5-flash")
            trades_summary = "\n".join([
                f"- {t.get('ticker')} {t.get('direction')} Score:{t.get('score','?')}/7 P&L:{t.get('pnl',0):+.2f}€ ({t.get('exit_reason','?')})"
                for t in week_trades[-20:]
            ]) or "Aucun trade."

            prompt = f"""Analyse la semaine de trading (Markdown, 4 points concis).

STATS : {len(week_trades)} trades | {wr:.1f}% WR | P&L semaine {total_pnl:+.2f} EUR | Capital {data['capital']:.2f} EUR ({pct:+.2f}%)
Meilleur : {f"{best['ticker']} {best['direction']} +{best['pnl']:.2f}€" if best else "N/A"}
Pire : {f"{worst['ticker']} {worst['direction']} {worst['pnl']:+.2f}€" if worst else "N/A"}

TRADES :
{trades_summary}

1. **Performance globale** : Bonne/mauvaise semaine ?
2. **Patterns d'erreurs** : Quelles erreurs reviennent ?
3. **Forces identifiées** : Ce qui fonctionne
4. **Actions semaine prochaine** : 2-3 ajustements concrets"""

            resp     = model.generate_content(prompt)
            analysis = resp.text.strip()
        except Exception as e:
            analysis = f"Analyse indisponible: {e}"

    full = f"""---
title: "Audit Hebdomadaire Gold Bot {today}"
type: audit
created: {today}
---

## Summary
Audit semaine {cutoff} → {today} : {len(week_trades)} trades, {wr:.1f}% WR, {total_pnl:+.2f} EUR.

## Statistiques
- Trades : {len(week_trades)} | Gagnants : {len(wins)} | Perdants : {len(losses)}
- Taux de réussite : {wr:.1f}% | P&L semaine : {total_pnl:+.2f} EUR
- Capital : {data['capital']:.2f} EUR ({pct:+.2f}% total)

## Analyse Gemini
{analysis}
"""
    slug    = f"audit-goldbot-{today}"
    summary = f"Audit semaine {cutoff}/{today}: {len(week_trades)} trades, {wr:.1f}% WR, {total_pnl:+.2f} EUR"
    push_wiki_knowledge(slug, f"Audit Gold Bot semaine {today}", "audit", summary, full)

    if JOHN_ID:
        try:
            await app.bot.send_message(
                JOHN_ID,
                f"📋 *Audit Hebdomadaire — Gold Bot*\n📅 {cutoff} → {today}\n\n"
                f"📊 {len(week_trades)} trades | {wr:.1f}% WR | `{total_pnl:+.2f} EUR`\n\n"
                f"{analysis[:700]}\n\n💾 _Sauvegardé dans le wiki_",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    logger.info(f"Audit hebdomadaire envoyé: {slug}")


# ── BOUCLE DE TRADING ──────────────────────────────────────────────────────────
async def trading_loop(app: Application):
    logger.info("Boucle de trading démarrée — vérification toutes les 15 min")
    while True:
        try:
            data        = load_data()
            instruments = get_instruments()

            # Pause après 3 pertes consécutives (Druckenmiller — préserver le capital)
            if data.get("loss_streak", 0) >= 3:
                logger.info(f"Pause trading — {data['loss_streak']} pertes consécutives")
                await asyncio.sleep(2 * 60 * 60)
                continue

            for ticker, info in instruments.items():
                df = await fetch_async(ticker)
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
                    if pnl_e < 0:
                        asyncio.create_task(post_mortem_analysis(app, pos))

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
    last_audit   = ""
    while True:
        now   = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        h, m  = now.hour, now.minute

        if h == 7 and m < 15 and last_morning != today:
            await morning_report(app)
            last_morning = today

        if h == 22 and m < 15 and last_evening != today:
            await evening_report(app)
            await _push_gold_wiki()
            last_evening = today

        if h == 8 and m < 15 and now.weekday() == 6 and last_audit != today:
            data = load_data()
            await weekly_audit(app, data)
            last_audit = today

        await asyncio.sleep(60)


# ── WIKI MANUEL — buffer + push 22h ───────────────────────────────────────────
gold_wiki_buffer: list[dict] = []

async def cmd_wiki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != JOHN_ID:
        return
    now     = datetime.now(TZ).strftime("%H:%M")
    content = ""
    photo_bytes = None

    if update.message.photo:
        photo = update.message.photo[-1]
        file  = await context.bot.get_file(photo.file_id)
        photo_bytes = bytes(await file.download_as_bytearray())
        content = (" ".join(context.args) if context.args else update.message.caption) or "Image"
    elif update.message.video or update.message.video_note:
        vid = update.message.video or update.message.video_note
        caption = (" ".join(context.args) if context.args else update.message.caption) or "Vidéo"
        placeholder = {"content": f"[VIDÉO en cours] {caption}", "time": now, "photo_bytes": None}
        gold_wiki_buffer.append(placeholder)
        count = len(gold_wiki_buffer)
        await update.message.reply_text(f"✅ Noté ({count} élément{'s' if count > 1 else ''} en attente — rapport à 22h)\n⏳ Transcription vidéo en arrière-plan...")
        async def _transcribe():
            try:
                file = await context.bot.get_file(vid.file_id)
                vbytes = bytes(await file.download_as_bytearray())
                genai.configure(api_key=GEMINI_API_KEY)
                model = genai.GenerativeModel("gemini-2.5-flash")
                resp = model.generate_content([
                    {"mime_type": "video/mp4", "data": vbytes},
                    "Transcris et résume cette vidéo en français."
                ])
                placeholder["content"] = f"[VIDÉO] {caption}\n{resp.text.strip()}"
                await update.message.reply_text("✅ Transcription vidéo terminée !")
            except Exception as e:
                placeholder["content"] = f"[VIDÉO] {caption} (analyse échouée: {e})"
        asyncio.create_task(_transcribe())
        return
    elif context.args:
        content = " ".join(context.args)
    elif update.message.text:
        content = update.message.text
    elif update.message.reply_to_message:
        content = update.message.reply_to_message.text or ""
    else:
        await update.message.reply_text("Usage: `/wiki <texte>` ou envoie une image/vidéo avec `/wiki` en légende.", parse_mode="Markdown")
        return

    gold_wiki_buffer.append({"content": content, "time": now, "photo_bytes": photo_bytes})
    count = len(gold_wiki_buffer)
    await update.message.reply_text(f"✅ Noté ({count} élément{'s' if count > 1 else ''} en attente — push à 22h)")


async def cmd_wikisend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != JOHN_ID:
        return
    await update.message.reply_text("⏳ Push wiki Gold Bot en cours...")
    await _push_gold_wiki()


async def _push_gold_wiki():
    if not gold_wiki_buffer:
        return
    if not GEMINI_API_KEY:
        return
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
        items_text = "\n".join([f"[{i+1}] ({it['time']}) {it['content']}"
                                 for i, it in enumerate(gold_wiki_buffer)])
        prompt = f"""Compile ces notes en une page wiki Markdown (format veille IA, date {today}).
Frontmatter obligatoire :
---
title: "Notes Gold Bot {today}"
type: source
created: {today}
---
## Summary
## Key Facts
## Concepts Mentioned

NOTES :
{items_text}"""
        resp = model.generate_content(prompt)
        md   = resp.text.strip()
        slug = f"notes-goldbot-{today}"
        push_wiki_knowledge(slug, f"Notes Gold Bot {today}", "source",
                            md[:500], md[:5000])
        gold_wiki_buffer.clear()
        logger.info(f"Gold wiki push OK: {slug}")
    except Exception as e:
        logger.error(f"Gold wiki push: {e}")


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
        df   = await fetch_async(pos["ticker"])
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
        df = await fetch_async(ticker)
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
    app.add_handler(CommandHandler("myid",      cmd_myid))
    app.add_handler(CommandHandler("wiki",      cmd_wiki))
    app.add_handler(CommandHandler("wikisend",  cmd_wikisend))
    app.add_handler(MessageHandler(filters.Regex(r'https?://\S+') & filters.ChatType.PRIVATE, cmd_wiki))
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE) & filters.ChatType.PRIVATE & filters.CaptionRegex(r'^/wiki'), cmd_wiki))

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
        oracle_loop(app),
    )

if __name__ == "__main__":
    asyncio.run(main())
