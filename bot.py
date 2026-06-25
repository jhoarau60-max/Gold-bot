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

7j/7 : XAU/USD (Or) + XAG/USD (Argent)
"""

import asyncio
import os
import json
import logging
import io
import re
from datetime import datetime, timedelta

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
JOETRADE_GROUP_ID = int(ENV.get("JOETRADE_GROUP_ID", "-1003942074689"))
JOETRADE_THREAD_GOLD = 40
GEMINI_API_KEY  = ENV.get("GEMINI_API_KEY", "")
CAPITAL_INITIAL = float(ENV.get("CAPITAL", "100"))

OANDA_ACCOUNT_ID = ENV.get("OANDA_ACCOUNT_ID", "")
OANDA_TOKEN      = ENV.get("OANDA_TOKEN", "")
OANDA_PRACTICE   = ENV.get("OANDA_PRACTICE", "true").lower() != "false"
OANDA_BASE_URL   = "https://api-fxpractice.oanda.com" if OANDA_PRACTICE else "https://api-fxtrade.oanda.com"
OANDA_HEADERS    = {"Authorization": f"Bearer {OANDA_TOKEN}", "Content-Type": "application/json"}
OANDA_INST_MAP   = {"XAUUSD=X": "XAU_USD", "XAGUSD=X": "XAG_USD"}
OANDA_GRAN_MAP   = {"15m": "M15", "1h": "H1"}
OANDA_COUNT_MAP  = {("5d","15m"): 480, ("2d","15m"): 192, ("10d","1h"): 240, ("5d","1h"): 120}
logger.info(f"OANDA configuré: account={bool(OANDA_ACCOUNT_ID)} token={bool(OANDA_TOKEN)} practice={OANDA_PRACTICE}")

TWELVEDATA_KEY   = ENV.get("TWELVEDATA_KEY", "")
TD_INST_MAP      = {"XAUUSD=X": "XAU/USD", "XAGUSD=X": "XAG/USD"}
TD_INTERVAL_MAP  = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1day"}
TD_COUNT_MAP     = {("5d","5m"): 480, ("5d","15m"): 480, ("2d","15m"): 192, ("10d","1h"): 240, ("5d","1h"): 120}
logger.info(f"Twelve Data configuré: key={bool(TWELVEDATA_KEY)}")

SUPABASE_URL     = ENV.get("SUPABASE_URL", "") or ENV.get("WIKI_SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = ENV.get("SUPABASE_SERVICE_KEY", "") or ENV.get("WIKI_SUPABASE_KEY", "")
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
}
RISK_PER_TRADE      = 0.01   # 1 % du capital par trade (Jesse Livermore : préserver le capital)
MAX_DAILY_LOSS      = 0.05   # 5 % de perte max par jour (phase test)
MAX_POSITION_HOURS  = 4      # timeout auto-close : scalping max 4h
MAX_DAILY_TRADES    = 4      # GOLD-E : max 4 trades/jour (évite sur-trading)
DRAWDOWN_ALERT      = 0.12   # 12% drawdown → risk réduit à 0.5%
DRAWDOWN_PAUSE      = 0.20   # 20% drawdown → pause 48h obligatoire
ML_MIN_TRADES       = 50     # XGBoost activé après 50 trades labelisés
UTC                 = pytz.utc

# Plages de prix valides — protection contre données aberrantes yfinance
PRICE_BOUNDS = {
    "XAUUSD=X": (1200, 8000),
    "XAGUSD=X": (8,    120),
    "BTC-USD":  (5000, 500000),
    "ETH-USD":  (500,  50000),
}
TZ              = pytz.timezone("Europe/Brussels")
TRADES_FILE     = "trades.json"

WEEKDAY_INSTRUMENTS = {
    "XAUUSD=X": {"name": "Or (XAU/USD)", "emoji": "🥇", "pip": 0.01},
    # XAG/USD retiré — Twelve Data requiert plan Grow/Venture pour l'argent
}
# ── DONNÉES PERSISTANTES ───────────────────────────────────────────────────────
def _default_state() -> dict:
    return {
        "capital":              CAPITAL_INITIAL,
        "peak_capital":         CAPITAL_INITIAL,
        "open_positions":       [],
        "closed_trades":        [],
        "daily_pnl":            0.0,
        "daily_trades":         0,
        "total_pnl":            0.0,
        "last_reset":           datetime.now(TZ).strftime("%Y-%m-%d"),
        "start_date":           datetime.now(TZ).strftime("%Y-%m-%d"),
        "win_streak":           0,
        "loss_streak":          0,
        "instrument_losses":    {},
        "instrument_blacklist": {},
        "learned_params":       {},
        "drawdown_pause_until": None,
        "ml_auc":               0.0,
        "ml_active":            False,
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
            base["loss_streak"]  = 0  # Reset au démarrage — pertes d'avant redeploy ne bloquent pas
            base["last_reset"]   = str(s.get("last_reset") or base["last_reset"])[:10]

        # Positions ouvertes — ferme automatiquement les stale (> MAX_POSITION_HOURS)
        o_res = sb_client.table("trade_history").select("*").eq("status", "open").execute()
        now_utc = datetime.now(pytz.utc)
        stale_cutoff = (now_utc - timedelta(hours=MAX_POSITION_HOURS)).isoformat()
        for row in (o_res.data or []):
            opened_at = row.get("opened_at") or ""
            # Position trop ancienne : fermer dans Supabase, ne pas réimporter
            if opened_at and opened_at < stale_cutoff:
                try:
                    sb_client.table("trade_history").update({
                        "status":     "closed",
                        "pnl":        0.0,
                        "price_exit": float(row.get("price_entry") or 0),
                        "closed_at":  now_utc.isoformat(),
                    }).eq("id", row["id"]).execute()
                    logger.info(f"Position stale fermée au démarrage: {row['symbol']} (ouverte {opened_at})")
                except Exception as e:
                    logger.error(f"Fermeture stale échouée: {e}")
                continue
            if row.get("sl") and row.get("tp") and row.get("qty"):
                base["open_positions"].append({
                    "ticker":      row["symbol"],
                    "direction":   row["direction"],
                    "entry_price": float(row["price_entry"]),
                    "sl":          float(row["sl"]),
                    "tp":          float(row["tp"]),
                    "qty":         float(row["qty"]),
                    "score":       min(int(row.get("score") or 0), 7),
                    "entry_time":  str(row.get("opened_at") or ""),
                    "pnl":         0.0,
                    "supabase_id": row["id"],
                })

        # Historique trades fermés — nourrit adaptive_params + XGBoost au redémarrage
        try:
            h_res = sb_client.table("trade_history").select(
                "symbol,direction,price_entry,price_exit,pnl,score,opened_at,closed_at"
            ).eq("status", "closed").eq("bot", "gold").order(
                "closed_at", desc=True
            ).limit(100).execute()
            # Inverse : la requête renvoie le plus récent en premier,
            # mais closed_trades doit être chronologique (le plus récent en dernier)
            # comme les appends en cours d'exécution, sinon closed_trades[-1] pointe
            # vers le trade le plus ANCIEN du lot juste après un restart Railway.
            h_res.data = list(reversed(h_res.data or []))
            for row in (h_res.data or []):
                if row.get("pnl") is not None:
                    base["closed_trades"].append({
                        "ticker":      row["symbol"],
                        "direction":   row["direction"],
                        "entry_price": float(row["price_entry"] or 0),
                        "exit_price":  float(row.get("price_exit") or 0),
                        "pnl":         float(row["pnl"]),
                        "score":       int(row.get("score") or 0),
                        "entry_time":  str(row.get("opened_at") or ""),
                        "exit_time":   str(row.get("closed_at") or ""),
                    })
            logger.info(f"{len(base['closed_trades'])} trades fermés restaurés depuis Supabase")
        except Exception as e:
            logger.warning(f"Restauration historique trades: {e}")

        logger.info(
            f"State Supabase chargé — capital: {base['capital']:.2f} EUR, "
            f"{len(base['open_positions'])} positions ouvertes, "
            f"{len(base['closed_trades'])} trades historique"
        )
    except Exception as e:
        logger.error(f"load_data_from_supabase: {e}")
    return base

def load_data() -> dict:
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            data = json.load(f)
        # Corrige peak_capital si aberrant (vieux capital avant reset)
        cap = data.get("capital", CAPITAL_INITIAL)
        peak = data.get("peak_capital", cap)
        if peak > cap * 1.5:
            logger.info(f"peak_capital aberrant ({peak:.2f}) corrigé → {cap:.2f}")
            data["peak_capital"] = cap
            save_data(data)
        return data
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
    return WEEKDAY_INSTRUMENTS

def is_trading_session() -> bool:
    """ICT Kill Zones — London Open (9h-12h) + NY Open (15h-18h) Paris."""
    h = datetime.now(TZ).hour
    return (9 <= h < 12) or (15 <= h < 18)

def is_blackout_session() -> bool:
    """Blackout 21h-06h UTC — session asiatique, spreads larges, pas de nouveaux trades."""
    h_utc = datetime.now(UTC).hour
    return h_utc >= 21 or h_utc < 6

def get_current_session() -> str:
    """Session active UTC pour logs et features ML."""
    h = datetime.now(UTC).hour
    if 0 <= h < 8:
        return "Tokyo"
    elif 8 <= h < 13:
        return "London"
    elif 13 <= h < 16:
        return "London/NY"
    elif 16 <= h < 21:
        return "New York"
    return "Blackout"

def get_drawdown(data: dict) -> float:
    """Drawdown courant = (peak - capital) / peak. Met à jour peak si nouveau sommet."""
    peak    = data.get("peak_capital", CAPITAL_INITIAL)
    capital = data["capital"]
    if capital > peak:
        data["peak_capital"] = capital
        return 0.0
    if peak <= 0:
        return 0.0
    return (peak - capital) / peak

# ── DXY (Dollar Index — corrélation inverse XAU) ──────────────────────────────
_dxy_cache: dict = {"direction": "FLAT", "fetched_at": None}

def get_dxy_direction() -> str:
    """UP si DXY monte (bearish XAU), DOWN si DXY baisse (bullish XAU). Cache 30min."""
    global _dxy_cache
    now = datetime.now(UTC)
    if _dxy_cache["fetched_at"] and (now - _dxy_cache["fetched_at"]).seconds < 1800:
        return _dxy_cache["direction"]
    try:
        df = yf.download("DX-Y.NYB", period="2d", interval="1h", progress=False, auto_adjust=True)
        if df is None or len(df) < 4:
            return "FLAT"
        c   = df["Close"].squeeze()
        pct = (float(c.iloc[-1]) - float(c.iloc[-4])) / float(c.iloc[-4])
        if pct > 0.001:
            direction = "UP"
        elif pct < -0.001:
            direction = "DOWN"
        else:
            direction = "FLAT"
        _dxy_cache = {"direction": direction, "fetched_at": now}
        return direction
    except Exception as e:
        logger.warning(f"DXY fetch: {e}")
        return "FLAT"

# ── MACRO CALENDAR (ForexFactory) ─────────────────────────────────────────────
_macro_cache: dict = {"events": [], "fetched_at": None}

def fetch_macro_calendar() -> list:
    """ForexFactory calendar — high-impact events uniquement. Cache 4h."""
    global _macro_cache
    now = datetime.now(UTC)
    if _macro_cache["fetched_at"] and (now - _macro_cache["fetched_at"]).seconds < 14400:
        return _macro_cache["events"]
    try:
        import httpx
        r = httpx.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=8)
        if r.status_code == 200:
            events = [e for e in r.json() if e.get("impact") == "High"]
            _macro_cache = {"events": events, "fetched_at": now}
            logger.info(f"Macro calendar: {len(events)} événements haute-impact")
            return events
    except Exception as e:
        logger.warning(f"Macro calendar: {e}")
    return _macro_cache.get("events", [])

def is_macro_blackout() -> bool:
    """True si annonce macro haute-impact dans les 30 prochaines minutes."""
    try:
        for e in fetch_macro_calendar():
            dt_str = e.get("date", "")
            if not dt_str:
                continue
            try:
                ev_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                diff  = (ev_dt.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds()
                if -300 <= diff <= 1800:
                    logger.info(f"Macro blackout: {e.get('title','?')} dans {diff:.0f}s")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False

# ── ML INFRASTRUCTURE (XGBoost) ───────────────────────────────────────────────
_ml_model = None
_ml_auc   = 0.0

FEATURE_COLS = [
    "adx", "atr_norm", "rsi", "macd_hist", "ema9_ema21_gap",
    "ema50_ema200_gap", "stoch_k", "williams_r", "dxy_direction",
    "score", "direction_int", "win_rate_20", "loss_streak", "sl_mult", "tp_mult",
]

async def init_ml_db(app=None):
    """Vérifie que gold_ml_features est accessible dans Supabase."""
    if not sb_client:
        logger.warning("ML: sb_client non dispo — ML désactivé")
        return
    try:
        sb_client.table("gold_ml_features").select("id").limit(1).execute()
        logger.info("ML Supabase: table gold_ml_features OK")
    except Exception as e:
        logger.error(f"ML: gold_ml_features inaccessible — {e}")

def log_trade_features(features: dict, supabase_id: str = ""):
    if not sb_client:
        return
    try:
        sb_client.table("gold_ml_features").insert({
            "supabase_id":      supabase_id,
            "trade_time":       features.get("trade_time"),
            "session":          features.get("session", ""),
            "adx":              features.get("adx", 0),
            "atr_norm":         features.get("atr_norm", 1),
            "rsi":              features.get("rsi", 50),
            "macd_hist":        features.get("macd_hist", 0),
            "ema9_ema21_gap":   features.get("ema9_ema21_gap", 0),
            "ema50_ema200_gap": features.get("ema50_ema200_gap", 0),
            "stoch_k":          features.get("stoch_k", 50),
            "williams_r":       features.get("williams_r", -50),
            "dxy_direction":    features.get("dxy_direction", 0),
            "score":            features.get("score", 0),
            "direction_int":    features.get("direction_int", 0),
            "win_rate_20":      features.get("win_rate_20", 50),
            "loss_streak":      features.get("loss_streak", 0),
            "sl_mult":          features.get("sl_mult", 1.5),
            "tp_mult":          features.get("tp_mult", 3.75),
        }).execute()
    except Exception as e:
        logger.error(f"log_trade_features: {e}")

def update_trade_outcome(supabase_id: str, outcome: int, pnl: float):
    if not sb_client or not supabase_id:
        return
    try:
        sb_client.table("gold_ml_features").update({
            "outcome": outcome, "pnl": pnl
        }).eq("supabase_id", supabase_id).is_("outcome", "null").execute()
    except Exception as e:
        logger.error(f"update_trade_outcome: {e}")

def collect_features(df: pd.DataFrame, data: dict, direction: str, dxy_dir: str) -> dict:
    try:
        last    = df.iloc[-1]
        recent  = data.get("closed_trades", [])[-20:]
        wr_20   = (sum(1 for t in recent if t.get("pnl", 0) > 0) / len(recent) * 100) if recent else 50.0
        atr_avg = float(df["ATR"].tail(20).mean())
        atr_now = float(last["ATR"])
        atr_norm = atr_now / atr_avg if atr_avg > 0 else 1.0
        dxy_map = {"UP": 1, "FLAT": 0, "DOWN": -1}
        lp = data.get("learned_params", {})
        return {
            "trade_time":       datetime.now(TZ).isoformat(),
            "session":          get_current_session(),
            "adx":              float(last["ADX"]),
            "atr_norm":         atr_norm,
            "rsi":              float(last["RSI"]),
            "macd_hist":        float(last["MACD_hist"]),
            "ema9_ema21_gap":   (float(last["EMA9"]) - float(last["EMA21"])) / max(float(last["EMA21"]), 1) * 100,
            "ema50_ema200_gap": (float(last["EMA50"]) - float(last["EMA200"])) / max(float(last["EMA200"]), 1) * 100,
            "stoch_k":          float(last["STOCH_K"]),
            "williams_r":       float(last["WILLIAMS_R"]),
            "dxy_direction":    dxy_map.get(dxy_dir, 0),
            "score":            0,
            "direction_int":    1 if direction == "BUY" else -1,
            "win_rate_20":      wr_20,
            "loss_streak":      data.get("loss_streak", 0),
            "sl_mult":          lp.get("sl_mult", 1.5),
            "tp_mult":          lp.get("tp_mult", 3.75),
        }
    except Exception as e:
        logger.error(f"collect_features: {e}")
        return {}

def train_and_save_ml() -> tuple:
    global _ml_model, _ml_auc
    if not sb_client:
        return None, 0.0
    try:
        import xgboost as xgb
        from sklearn.metrics import roc_auc_score
        resp = sb_client.table("gold_ml_features").select("*").not_.is_("outcome", "null").execute()
        if not resp.data:
            logger.info(f"ML: 0/{ML_MIN_TRADES} trades — entraînement reporté")
            return None, 0.0
        df_ml = pd.DataFrame(resp.data)
        if len(df_ml) < ML_MIN_TRADES:
            logger.info(f"ML: {len(df_ml)}/{ML_MIN_TRADES} trades — entraînement reporté")
            return None, 0.0
        X = df_ml[FEATURE_COLS].values.astype(float)
        y = df_ml["outcome"].values.astype(int)
        split = int(len(X) * 0.8)
        X_tr, X_val = X[:split], X[split:]
        y_tr, y_val = y[:split], y[split:]
        if len(set(y_val)) < 2:
            return None, 0.0
        model = xgb.XGBClassifier(
            n_estimators=100, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, eval_metric="logloss", verbosity=0
        )
        model.fit(X_tr, y_tr)
        auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
        _ml_model = model
        _ml_auc   = auc
        logger.info(f"ML entraîné — AUC {auc:.3f} sur {len(df_ml)} trades (Supabase)")
        return model, auc
    except ImportError:
        logger.warning("XGBoost non installé — ML désactivé")
        return None, 0.0
    except Exception as e:
        logger.error(f"train_and_save_ml: {e}")
        return None, 0.0

def predict_ml_proba(features: dict) -> float:
    """Probabilité de succès ML. Retourne -1 si modèle non actif."""
    global _ml_model, _ml_auc
    if _ml_model is None or _ml_auc < 0.55:
        return -1.0
    try:
        X = [[features.get(c, 0) for c in FEATURE_COLS]]
        return float(_ml_model.predict_proba(X)[0][1])
    except Exception:
        return -1.0

def save_learned_params(params: dict):
    """Persiste les params Gemini dans wiki_knowledge (survie redéploiement)."""
    if not wiki_sb_client:
        return
    try:
        wiki_sb_client.table("wiki_knowledge").upsert({
            "slug":         "goldbot-learned-params",
            "title":        "Gold Bot — Paramètres appris",
            "type":         "params",
            "summary":      f"Mis à jour {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}",
            "full_content": json.dumps(params),
            "created_at":   datetime.now(TZ).isoformat(),
        }, on_conflict="slug").execute()
    except Exception as e:
        logger.error(f"save_learned_params: {e}")

def load_learned_params() -> dict:
    """Charge les params Gemini depuis Supabase au démarrage."""
    if not wiki_sb_client:
        return {}
    try:
        res = wiki_sb_client.table("wiki_knowledge").select("full_content").eq("slug", "goldbot-learned-params").execute()
        if res.data:
            return json.loads(res.data[0]["full_content"])
    except Exception as e:
        logger.error(f"load_learned_params: {e}")
    return {}

def adaptive_params(data: dict) -> dict:
    """Niveau 1 : ajuste risque/seuil selon win rate glissant.
    Niveau 2 : applique les overrides Gemini (bornés)."""
    recent  = data.get("closed_trades", [])[-20:]
    learned = data.get("learned_params", {})

    if len(recent) < 5:
        base = {"threshold": 5, "risk_per_trade": 0.02, "sl_mult": 2.632, "tp_mult": 5.343, "mode": "démarrage"}
    else:
        wr = sum(1 for t in recent if t.get("pnl", 0) > 0) / len(recent)
        if wr < 0.35:
            base = {"threshold": 5, "risk_per_trade": 0.015, "sl_mult": 1.8, "tp_mult": 7.2, "mode": "récupération"}
        elif wr > 0.65:
            base = {"threshold": 5, "risk_per_trade": 0.025, "sl_mult": 1.3, "tp_mult": 5.2, "mode": "sélectif"}
        else:
            base = {"threshold": 5, "risk_per_trade": 0.02, "sl_mult": 2.632, "tp_mult": 5.343, "mode": "normal"}

    if learned:
        if "threshold"      in learned: base["threshold"]      = max(3, min(6,   int(learned["threshold"])))
        if "risk_per_trade" in learned: base["risk_per_trade"] = max(0.003, min(0.02, float(learned["risk_per_trade"])))
        if "sl_mult"        in learned: base["sl_mult"]        = max(1.0, min(3.0, float(learned["sl_mult"])))
        if "tp_mult"        in learned: base["tp_mult"]        = max(2.0, min(6.0, float(learned["tp_mult"])))
    return base


# ── FETCH DONNÉES ──────────────────────────────────────────────────────────────

def fetch_oanda_candles(ticker: str, count: int = 300, granularity: str = "M15") -> pd.DataFrame | None:
    """Données temps réel OANDA → DataFrame compatible compute_indicators."""
    if not OANDA_TOKEN:
        return None
    oanda_inst = OANDA_INST_MAP.get(ticker)
    if not oanda_inst:
        return None
    try:
        import httpx as _httpx
        r = _httpx.get(
            f"{OANDA_BASE_URL}/v3/instruments/{oanda_inst}/candles",
            headers=OANDA_HEADERS,
            params={"count": count, "granularity": granularity, "price": "M"},
            timeout=15
        )
        if r.status_code != 200:
            logger.error(f"OANDA candles {ticker}: {r.status_code} {r.text[:100]}")
            return None
        candles = [c for c in r.json().get("candles", []) if c.get("complete", True)]
        if len(candles) < 10:
            return None
        rows = [{"Open": float(c["mid"]["o"]), "High": float(c["mid"]["h"]),
                 "Low": float(c["mid"]["l"]), "Close": float(c["mid"]["c"]),
                 "Volume": int(c.get("volume", 0))} for c in candles]
        idx = pd.to_datetime([c["time"] for c in candles])
        df = pd.DataFrame(rows, index=idx)
        logger.info(f"OANDA fetch OK: {ticker} — {len(df)} bougies temps réel")
        return df
    except Exception as e:
        logger.error(f"fetch_oanda_candles {ticker}: {e}")
        return None


def place_oanda_order(ticker: str, direction: str, units: float, sl: float, tp: float) -> str | None:
    """Passe un ordre marché OANDA. Retourne trade_id ou None."""
    if not OANDA_TOKEN or not OANDA_ACCOUNT_ID:
        return None
    oanda_inst = OANDA_INST_MAP.get(ticker)
    if not oanda_inst:
        return None
    try:
        import httpx as _httpx
        oanda_units = str(int(abs(units))) if direction == "BUY" else str(-int(abs(units)))
        if oanda_units in ("0", "-0"):
            return None
        payload = {"order": {
            "type": "MARKET",
            "instrument": oanda_inst,
            "units": oanda_units,
            "stopLossOnFill":   {"price": f"{sl:.5f}"},
            "takeProfitOnFill": {"price": f"{tp:.5f}"},
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
        }}
        r = _httpx.post(
            f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders",
            headers=OANDA_HEADERS, json=payload, timeout=15
        )
        if r.status_code in (200, 201):
            trade_id = r.json().get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID")
            logger.info(f"OANDA ordre OK: {oanda_inst} {direction} {oanda_units} → trade {trade_id}")
            return trade_id
        logger.error(f"OANDA ordre échoué: {r.status_code} {r.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"place_oanda_order {ticker}: {e}")
        return None


def close_oanda_trade(trade_id: str) -> bool:
    """Ferme un trade OANDA par son ID."""
    if not OANDA_TOKEN or not OANDA_ACCOUNT_ID or not trade_id:
        return False
    try:
        import httpx as _httpx
        r = _httpx.put(
            f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close",
            headers=OANDA_HEADERS, timeout=10
        )
        ok = r.status_code in (200, 201)
        if ok:
            logger.info(f"OANDA trade {trade_id} fermé")
        else:
            logger.error(f"OANDA close trade {trade_id}: {r.status_code} {r.text[:100]}")
        return ok
    except Exception as e:
        logger.error(f"close_oanda_trade {trade_id}: {e}")
        return False


def fetch_twelvedata_candles(ticker: str, count: int = 300, interval: str = "15min") -> pd.DataFrame | None:
    """Données temps réel Twelve Data → DataFrame compatible compute_indicators."""
    if not TWELVEDATA_KEY:
        return None
    symbol = TD_INST_MAP.get(ticker)
    if not symbol:
        return None
    try:
        import httpx as _httpx
        r = _httpx.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol":     symbol,
                "interval":   interval,
                "outputsize": count,
                "apikey":     TWELVEDATA_KEY,
                "format":     "JSON",
            },
            timeout=15
        )
        if r.status_code != 200:
            logger.error(f"Twelve Data {ticker}: HTTP {r.status_code}")
            return None
        data = r.json()
        if data.get("status") == "error":
            logger.error(f"Twelve Data {ticker}: {data.get('message')}")
            return None
        values = data.get("values", [])
        if len(values) < 10:
            return None
        # TD retourne newest first → reverse
        values = list(reversed(values))
        rows = [{
            "Open":   float(v["open"]),
            "High":   float(v["high"]),
            "Low":    float(v["low"]),
            "Close":  float(v["close"]),
            "Volume": float(v.get("volume", 0) or 0),
        } for v in values]
        idx = pd.to_datetime([v["datetime"] for v in values])
        df = pd.DataFrame(rows, index=idx)
        logger.info(f"Twelve Data fetch OK: {ticker} — {len(df)} bougies temps réel")
        return df
    except Exception as e:
        logger.error(f"fetch_twelvedata_candles {ticker}: {e}")
        return None


TICKER_FALLBACKS = {
    "XAUUSD=X": ["GC=F", "XAUUSD=X"],
    "XAGUSD=X": ["SI=F", "XAGUSD=X"],
}

def _is_rate_limit(e: Exception) -> bool:
    s = str(e).lower()
    return "too many requests" in s or "rate limit" in s or "ratelimit" in s

async def fetch_async(ticker: str, period: str = "5d", interval: str = "5m"):
    """Wrapper non-bloquant — exécute fetch() dans thread pool."""
    return await asyncio.to_thread(fetch, ticker, period, interval)

def resample_to_1h(df_5m: pd.DataFrame) -> pd.DataFrame | None:
    """Convertit 5min/15min → 1H par resampling (0 crédit API supplémentaire)."""
    try:
        df = df_5m.resample("1h").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna()
        return compute_indicators(df) if len(df) >= 10 else None
    except Exception:
        return None

def get_1h_trend(df_base: pd.DataFrame) -> str:
    """Tendance macro 1H depuis données de base resampleées. Retourne UP/DOWN/NEUTRAL."""
    df1h = resample_to_1h(df_base)
    if df1h is None:
        return "NEUTRAL"
    close = float(df1h["Close"].squeeze().iloc[-1])
    ema21 = float(df1h["EMA21"].iloc[-1])
    ema50 = float(df1h["EMA50"].iloc[-1])
    if close > ema50 and ema21 > ema50:
        return "UP"
    elif close < ema50 and ema21 < ema50:
        return "DOWN"
    return "NEUTRAL"

_4h_cache_gold: dict = {}

def get_4h_trend(ticker: str) -> str:
    """Tendance 4H Twelve Data (cache 4H). Filtre macro fiable — évite contre-tendance multi-jours."""
    import time as _time
    now = _time.time()
    cached = _4h_cache_gold.get(ticker, {})
    if now - cached.get("ts", 0) < 14400:
        return cached.get("trend", "NEUTRAL")
    df = fetch_twelvedata_candles(ticker, count=100, interval="4h")
    if df is None or len(df) < 30:
        _4h_cache_gold[ticker] = {"trend": "NEUTRAL", "ts": now}
        return "NEUTRAL"
    df = compute_indicators(df)
    close = float(df["Close"].squeeze().iloc[-1])
    ema21 = float(df["EMA21"].iloc[-1])
    ema50 = float(df["EMA50"].iloc[-1])
    if close > ema50 and ema21 > ema50:
        trend = "UP"
    elif close < ema50 and ema21 < ema50:
        trend = "DOWN"
    else:
        trend = "NEUTRAL"
    _4h_cache_gold[ticker] = {"trend": trend, "ts": now}
    logger.info(f"Tendance 4H {ticker}: {trend} (close={close:.2f} EMA21={ema21:.2f} EMA50={ema50:.2f})")
    return trend

def fetch(ticker: str, period: str = "5d", interval: str = "5m") -> pd.DataFrame | None:
    # Twelve Data — priorité maximale (temps réel, pas de rate limit agressif)
    if ticker in TD_INST_MAP and TWELVEDATA_KEY:
        td_interval = TD_INTERVAL_MAP.get(interval, "5min")
        count = TD_COUNT_MAP.get((period, interval), 300)
        df = fetch_twelvedata_candles(ticker, count=count, interval=td_interval)
        if df is not None and len(df) >= 10:
            return df
        logger.warning(f"Twelve Data fetch raté pour {ticker} — fallback OANDA")

    # OANDA fallback pour XAU/XAG
    if ticker in OANDA_INST_MAP and OANDA_TOKEN:
        gran  = OANDA_GRAN_MAP.get(interval, "M15")
        count = OANDA_COUNT_MAP.get((period, interval), 300)
        df = fetch_oanda_candles(ticker, count=count, granularity=gran)
        if df is not None and len(df) >= 10:
            return df
        logger.warning(f"OANDA fetch raté pour {ticker} — fallback yfinance")

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
    df["EMA21"]  = c.ewm(span=20,  adjust=False).mean()
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
    df["RSI"] = 100 - 100 / (1 + gain.rolling(13).mean() / loss.rolling(14).mean())

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

    # ── TEMA (Triple EMA, période 9 — confirmation momentum Freqtrade) ──
    _t1 = c.ewm(span=9, adjust=False).mean()
    _t2 = _t1.ewm(span=9, adjust=False).mean()
    _t3 = _t2.ewm(span=9, adjust=False).mean()
    df["TEMA"] = 3 * _t1 - 3 * _t2 + _t3

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
    period = min(50, len(df))
    hi_series = df["High"].squeeze().tail(period)
    lo_series = df["Low"].squeeze().tail(period)
    high = float(hi_series.max())
    low  = float(lo_series.min())
    diff = high - low
    # Timestamp des bougies swing (pour positionnement exact sur le graphique)
    hi_pos = int(hi_series.values.argmax())
    lo_pos = int(lo_series.values.argmin())
    try:
        hi_time = str(hi_series.index[hi_pos])
        lo_time = str(lo_series.index[lo_pos])
    except Exception:
        hi_time = None
        lo_time = None
    return {
        "high":         high,
        "low":          low,
        "fib_high_time": hi_time,
        "fib_low_time":  lo_time,
        "fib_786": high - 0.786 * diff,
        "fib_618": high - 0.618 * diff,
        "fib_5":   high - 0.500 * diff,
        "fib_382": high - 0.382 * diff,
        "fib_236": high - 0.236 * diff,
    }


# ── ICT : OTE / FVG / OB ──────────────────────────────────────────────────────
def _find_swing(df: pd.DataFrame, lookback: int = 60) -> tuple[float, float]:
    recent = df.tail(lookback)
    return float(recent["High"].max()), float(recent["Low"].min())

def _fib_levels(sh: float, sl: float, trend: str) -> dict:
    diff = sh - sl
    lvls = {}
    retr = [0.236, 0.382, 0.5, 0.618, 0.786]
    if trend == "UP":
        for r in retr: lvls[r] = sh - diff * r
    else:
        for r in retr: lvls[r] = sl + diff * r
    return lvls

def _in_ote(price: float, lvls: dict, atr: float, trend: str) -> tuple[bool, str]:
    """Prix dans zone OTE 0.618–0.786. Score 2 si exact sur niveau, 1 si dans la zone."""
    lvl618 = lvls.get(0.618, 0)
    lvl786 = lvls.get(0.786, 0)
    lo, hi = (min(lvl618, lvl786), max(lvl618, lvl786))
    for key_lvl in [0.618, 0.786]:
        target = lvls.get(key_lvl, 0)
        if abs(price - target) <= atr * 0.6:
            return True, f"✅✅ OTE exact {key_lvl*100:.1f}% — entrée précise"
    if lo <= price <= hi:
        return True, "✅ Prix en zone OTE (61.8%–78.6%)"
    return False, ""

def _detect_fvg(df: pd.DataFrame, lookback: int = 30) -> tuple[bool, bool, str]:
    recent = df.tail(lookback).reset_index(drop=True)
    price = float(recent["Close"].iloc[-1])
    for i in range(2, len(recent)):
        h0 = float(recent["High"].iloc[i - 2])
        l0 = float(recent["Low"].iloc[i - 2])
        h2 = float(recent["High"].iloc[i])
        l2 = float(recent["Low"].iloc[i])
        if h0 < l2 and h0 <= price <= l2:
            return True, False, f"✅ FVG haussier [{h0:.2f}–{l2:.2f}]"
        if l0 > h2 and h2 <= price <= l0:
            return False, True, f"✅ FVG baissier [{h2:.2f}–{l0:.2f}]"
    return False, False, ""

def _detect_ob(df: pd.DataFrame, lookback: int = 30, atr_mult: float = 1.5) -> tuple[bool, bool, str]:
    recent = df.tail(lookback).reset_index(drop=True)
    price = float(recent["Close"].iloc[-1])
    atr   = float(recent["ATR"].iloc[-1])
    threshold = atr * atr_mult
    for i in range(1, len(recent) - 1):
        o  = float(recent["Open"].iloc[i])
        c  = float(recent["Close"].iloc[i])
        h  = float(recent["High"].iloc[i])
        l  = float(recent["Low"].iloc[i])
        cn = float(recent["Close"].iloc[i + 1])
        on = float(recent["Open"].iloc[i + 1])
        if c < o and (cn - on) > threshold and l <= price <= h:
            return True, False, f"✅ OB haussier [{l:.2f}–{h:.2f}]"
        if c > o and (on - cn) > threshold and l <= price <= h:
            return False, True, f"✅ OB baissier [{l:.2f}–{h:.2f}]"
    return False, False, ""

def _detect_ifvg(df: pd.DataFrame, lookback: int = 50) -> tuple[bool, bool, str]:
    """Inverse Fair Value Gap — ancien FVG comblé qui s'est inversé en zone S/R opposée."""
    recent = df.tail(lookback).reset_index(drop=True)
    n = len(recent)
    price = float(recent["Close"].iloc[-1])
    for i in range(2, n - 3):
        h0 = float(recent["High"].iloc[i - 2])
        l0 = float(recent["Low"].iloc[i - 2])
        h2 = float(recent["High"].iloc[i])
        l2 = float(recent["Low"].iloc[i])
        if h0 < l2:
            gap_lo, gap_hi = h0, l2
            filled = any(
                float(recent["Low"].iloc[j]) <= gap_hi and float(recent["High"].iloc[j]) >= gap_lo
                for j in range(i + 1, n - 1)
            )
            if filled and gap_lo <= price <= gap_hi:
                return False, True, f"✅ IFVG baissier [{gap_lo:.2f}–{gap_hi:.2f}]"
        if l0 > h2:
            gap_lo, gap_hi = h2, l0
            filled = any(
                float(recent["Low"].iloc[j]) <= gap_hi and float(recent["High"].iloc[j]) >= gap_lo
                for j in range(i + 1, n - 1)
            )
            if filled and gap_lo <= price <= gap_hi:
                return True, False, f"✅ IFVG haussier [{gap_lo:.2f}–{gap_hi:.2f}]"
    return False, False, ""

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

    # 4. RSI (Wilder) — zone élargie en tendance forte
    strong_uptrend = adx > 29.3 and ema9 > ema21
    if 45 <= rsi <= 75:
        score_buy += 1
        reasons_buy.append(f"✅ RSI favorable achat ({rsi:.1f})")
    elif 25 <= rsi < 45:
        score_sell += 1
        reasons_sell.append(f"✅ RSI momentum baissier ({rsi:.1f})")
    elif rsi > 75:
        if not strong_uptrend:
            score_sell += 1
            reasons_sell.append(f"⚠️ RSI en surachat ({rsi:.1f})")
        else:
            reasons_buy.append(f"ℹ️ RSI élevé ({rsi:.1f}) — tendance forte, momentum maintenu")
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
    if adx > 29.3:
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

    # 8. OTE Fibonacci 0.618–0.786
    atr = float(last["ATR"]) if not pd.isna(last["ATR"]) else 0
    if atr > 0:
        trend_dir = "UP" if c > ema200 else "DOWN"
        sh, sl_ = _find_swing(df)
        lvls = _fib_levels(sh, sl_, trend_dir)
        in_ote, ote_desc = _in_ote(c, lvls, atr, trend_dir)
        if in_ote:
            if trend_dir == "UP":
                score_buy += 2 if "exact" in ote_desc else 1
                reasons_buy.append(ote_desc)
            else:
                score_sell += 2 if "exact" in ote_desc else 1
                reasons_sell.append(ote_desc)

    # 9. FVG (Fair Value Gap)
    fvg_bull, fvg_bear, fvg_desc = _detect_fvg(df)
    if fvg_bull: score_buy  += 1; reasons_buy.append(fvg_desc)
    if fvg_bear: score_sell += 1; reasons_sell.append(fvg_desc)

    # 10. OB (Order Block)
    ob_bull, ob_bear, ob_desc = _detect_ob(df)
    if ob_bull: score_buy  += 1; reasons_buy.append(ob_desc)
    if ob_bear: score_sell += 1; reasons_sell.append(ob_desc)

    # 11. IFVG (Inverse Fair Value Gap)
    ifvg_bull, ifvg_bear, ifvg_desc = _detect_ifvg(df)
    if ifvg_bull: score_buy  += 2; reasons_buy.append(ifvg_desc)
    if ifvg_bear: score_sell += 2; reasons_sell.append(ifvg_desc)

    threshold = 5  # ICT confluence : OTE + FVG/OB + confirmations techniques

    # Filtre ADX obligatoire — pas de trade en consolidation (ADX < 22)
    if adx < 20.7:
        logger.info(f"Signal bloqué — ADX trop faible ({adx:.1f}) : marché en range, pas de trade")
        return None, max(score_buy, score_sell), []

    # Filtre EMA200 obligatoire — trade UNIQUEMENT dans le sens de la tendance principale
    if score_buy >= threshold and score_buy > score_sell:
        if c < ema200:
            logger.info(f"BUY bloqué — prix ({c:.2f}) sous EMA200 ({ema200:.2f}) : contre-tendance")
            return None, score_buy, []
        return "BUY", min(score_buy, 7), reasons_buy
    elif score_sell >= threshold and score_sell > score_buy:
        if c > ema200:
            logger.info(f"SELL bloqué — prix ({c:.2f}) au-dessus EMA200 ({ema200:.2f}) : contre-tendance")
            return None, score_sell, []
        return "SELL", min(score_sell, 7), reasons_sell

    logger.info(f"Signal XAU — BUY:{score_buy}/7 SELL:{score_sell}/7 (seuil:{threshold}) — pas assez fort")
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
               price: float, atr: float, score: int, params: dict = None) -> dict | None:
    # Sanity check prix — rejette données yfinance aberrantes
    if ticker in PRICE_BOUNDS:
        lo, hi = PRICE_BOUNDS[ticker]
        if not (lo <= price <= hi):
            logger.error(f"Prix aberrant {ticker}: {price:.2f} (attendu {lo}–{hi}) — trade annulé")
            return None

    if data["daily_pnl"] <= -(data["capital"] * MAX_DAILY_LOSS):
        logger.info("Limite perte journalière atteinte")
        return None

    # GOLD-E : max 4 trades/jour
    if data.get("daily_trades", 0) >= MAX_DAILY_TRADES:
        logger.info(f"Refus {ticker} — max {MAX_DAILY_TRADES} trades/jour atteint ({data['daily_trades']})")
        return None

    # GOLD-E : drawdown control
    dd = get_drawdown(data)
    if dd >= DRAWDOWN_PAUSE:
        logger.warning(f"Drawdown {dd:.1%} >= {DRAWDOWN_PAUSE:.0%} — pause obligatoire")
        return None

    for p in data["open_positions"]:
        if p["ticker"] == ticker:
            return None

    sl_mult = params["sl_mult"]        if params else 1.5
    tp_mult = params["tp_mult"]        if params else 3.75
    risk    = params["risk_per_trade"] if params else RISK_PER_TRADE

    # GOLD-E : drawdown 12% → sizing réduit à 0.5%
    if dd >= DRAWDOWN_ALERT:
        risk = min(risk, 0.005)
        logger.info(f"Drawdown {dd:.1%} — risk réduit à {risk:.1%}")
    sl_dist = atr * sl_mult
    tp_dist = atr * tp_mult

    sl = price - sl_dist if direction == "BUY" else price + sl_dist
    tp = price + tp_dist if direction == "BUY" else price - tp_dist
    qty = round((data["capital"] * risk) / sl_dist, 6)
    if qty <= 0:
        return None

    pos = {
        "ticker":           ticker,
        "direction":        direction,
        "entry_price":      round(price, 5),
        "sl":               round(sl, 5),
        "tp":               round(tp, 5),
        "qty":              qty,
        "score":            score,
        "entry_time":       datetime.now(TZ).isoformat(),
        "pnl":              0.0,
        "oanda_id":         None,
        "atr_entry":        atr,
        "sl_mult":          sl_mult,
        "trail_peak":       price,
        "trailing_active":  False,
    }

    # Ordre réel OANDA (XAU/XAG uniquement)
    if ticker in OANDA_INST_MAP and OANDA_TOKEN:
        oanda_id = place_oanda_order(ticker, direction, qty, round(sl, 5), round(tp, 5))
        if oanda_id:
            pos["oanda_id"] = oanda_id
        else:
            logger.warning(f"Ordre OANDA échoué pour {ticker} — trade enregistré localement uniquement")

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
            hit_sl = price <= pos["sl"]
            hit_tp = price >= pos["tp"]
            # Fermeture au prix SL/TP réel (évite slippage gap)
            exit_price = pos["sl"] if hit_sl else (pos["tp"] if hit_tp else price)
            pnl = (exit_price - pos["entry_price"]) * pos["qty"]
        else:
            hit_sl = price >= pos["sl"]
            hit_tp = price <= pos["tp"]
            exit_price = pos["sl"] if hit_sl else (pos["tp"] if hit_tp else price)
            pnl = (pos["entry_price"] - exit_price) * pos["qty"]

        pos["pnl"] = round(pnl, 2)

        # Timeout auto-close : scalping max MAX_POSITION_HOURS
        entry_dt = pos.get("entry_time", "")
        try:
            entry_dt = datetime.fromisoformat(entry_dt)
            if entry_dt.tzinfo is None:
                entry_dt = TZ.localize(entry_dt)
            age_h = (datetime.now(TZ) - entry_dt).total_seconds() / 3600
        except Exception:
            age_h = 0
        timeout_hit = age_h > MAX_POSITION_HOURS

        if hit_sl or hit_tp or timeout_hit:
            reason = "✅ Take Profit" if hit_tp else ("⏰ Timeout" if timeout_hit else "🛑 Stop Loss")
            pos["exit_price"]  = round(exit_price if (hit_sl or hit_tp) else price, 5)
            pos["exit_time"]   = datetime.now(TZ).isoformat()
            pos["exit_reason"] = reason
            data["closed_trades"].append(pos)
            data["daily_pnl"] += pnl
            data["total_pnl"] += pnl
            data["capital"]   += pnl
            if pnl > 0:
                data["win_streak"]  = data.get("win_streak", 0) + 1
                data["loss_streak"] = 0
                data.setdefault("instrument_losses", {})[ticker] = 0
            else:
                data["loss_streak"] = data.get("loss_streak", 0) + 1
                data["win_streak"]  = 0
                losses = data.setdefault("instrument_losses", {})
                losses[ticker] = losses.get(ticker, 0) + 1
                if losses[ticker] >= 3:
                    data.setdefault("instrument_blacklist", {})[ticker] = datetime.now(TZ).timestamp() + 86400
                    logger.warning(f"Blacklist 24h {ticker} — 3 pertes consécutives")
            if sb_client and "supabase_id" in pos:
                try:
                    sb_client.table("trade_history").update({
                        "price_exit": round(pos.get("exit_price", price), 5),
                        "pnl":        round(pnl, 2),
                        "status":     "closed",
                        "closed_at":  datetime.now(TZ).isoformat(),
                    }).eq("id", pos["supabase_id"]).execute()
                except Exception as e:
                    logger.error(f"Supabase update trade: {e}")

            update_investor_profiles(pnl)
            closed.append((pos, reason))
        else:
            # Trailing stop : suit le prix favorable
            atr_e      = pos.get("atr_entry", 0)
            sl_m       = pos.get("sl_mult", 1.5)
            trail_dist = atr_e * sl_m
            d_pos      = pos["direction"]
            if atr_e > 0:
                if d_pos == "BUY":
                    if price > pos.get("trail_peak", price):
                        pos["trail_peak"] = price
                    if not pos.get("trailing_active") and price >= pos["entry_price"] + atr_e:
                        pos["trailing_active"] = True
                        logger.info(f"Trailing activé {ticker} BUY")
                    if pos.get("trailing_active"):
                        new_sl = pos["trail_peak"] - trail_dist
                        if new_sl > pos["sl"]:
                            pos["sl"] = round(new_sl, 5)
                else:
                    if price < pos.get("trail_peak", price):
                        pos["trail_peak"] = price
                    if not pos.get("trailing_active") and price <= pos["entry_price"] - atr_e:
                        pos["trailing_active"] = True
                        logger.info(f"Trailing activé {ticker} SELL")
                    if pos.get("trailing_active"):
                        new_sl = pos["trail_peak"] + trail_dist
                        if new_sl < pos["sl"]:
                            pos["sl"] = round(new_sl, 5)
            if sb_client and "supabase_id" in pos:
                try:
                    sb_client.table("trade_history").update({
                        "pnl": round(pnl, 2),
                        "sl":  round(pos["sl"], 5),
                    }).eq("id", pos["supabase_id"]).execute()
                except Exception as e:
                    logger.error(f"Supabase update open pnl: {e}")
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


# ── RÈGLES TRADING — LIVRES PDF VIA SUPABASE WIKI ─────────────────────────────
_pdf_rules_cache: str = ""
_pdf_rules_ts: float = 0.0

def load_pdf_trading_rules() -> str:
    global _pdf_rules_cache, _pdf_rules_ts
    import time as _time
    if _time.time() - _pdf_rules_ts < 21600 and _pdf_rules_cache:
        return _pdf_rules_cache
    try:
        import httpx as _httpx
        _key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImN2Z2d4a3R5YnpicnRza2N3bHhwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQyODM2MjcsImV4cCI6MjA4OTg1OTYyN30.cfHsAvmgcXYvedCz1fZCHlxOApupKCxnt8t9e8KzNBs"
        r = _httpx.get(
            "https://cvggxktybzbrtskcwlxp.supabase.co/rest/v1/john_memory"
            "?content=like.%5BPDF_TRADING%25&select=content&order=created_at&limit=8",
            headers={"apikey": _key, "Authorization": f"Bearer {_key}"},
            timeout=10
        )
        if r.is_success:
            excerpts = []
            for row in r.json():
                c = row["content"]
                idx = c.lower().find("citations")
                excerpts.append(c[idx:idx+500].strip() if idx > 0 else c[-400:].strip())
            _pdf_rules_cache = "\n---\n".join(excerpts[:4])
            _pdf_rules_ts = _time.time()
            return _pdf_rules_cache
    except Exception as e:
        logger.error(f"load_pdf_trading_rules: {e}")
    return ""


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

        _pdf_rules = load_pdf_trading_rules()
        _pdf_section = f"\n\nPRINCIPES EXPERTS (livres trading spécialisés) :\n{_pdf_rules[:700]}\n" if _pdf_rules else ""

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
{_pdf_section}
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


# ── POLARIS ORACLE — MULTI-MARCHÉ RSS + IA ────────────────────────────────────

ORACLE_DOMAINS = {
    "metals": {
        "name": "Or & Argent",
        "emoji": "🥇",
        "rss": [
            "https://www.kitco.com/rss/kitcogoldnews.xml",
            "https://feeds.marketwatch.com/marketwatch/marketpulse/",
            "https://finance.yahoo.com/news/rssindex",
        ],
        "keywords": [
            "gold", "silver", "xau", "xag", "precious metals", "inflation", "fed",
            "dollar", "interest rate", "commodities", "bullion", "safe haven",
            "central bank", "rate", "treasury", "bond yield", "hedge",
        ],
        "ticker": "XAUUSD=X",
        "trade": True,
    },
    "energy": {
        "name": "Pétrole & Énergie",
        "emoji": "🛢️",
        "rss": [
            "https://oilprice.com/rss/main",
            "https://feeds.reuters.com/reuters/businessNews",
            "https://finance.yahoo.com/news/rssindex",
        ],
        "keywords": [
            "oil", "crude", "opec", "petroleum", "brent", "wti", "energy",
            "gas", "natural gas", "pipeline", "refinery", "barrel", "production",
            "supply", "demand", "geopolitical", "iran", "saudi", "russia",
        ],
        "ticker": "CL=F",
        "trade": False,
    },
    "bourse": {
        "name": "Bourse & Indices",
        "emoji": "📊",
        "rss": [
            "https://feeds.marketwatch.com/marketwatch/topstories/",
            "https://finance.yahoo.com/news/rssindex",
            "https://feeds.reuters.com/reuters/businessNews",
        ],
        "keywords": [
            "stock", "market", "s&p", "nasdaq", "dow", "earnings", "fed",
            "interest rate", "inflation", "recession", "gdp", "unemployment",
            "rally", "correction", "bull", "bear", "ipo", "merger",
        ],
        "ticker": "^GSPC",
        "trade": False,
    },
    "football": {
        "name": "Football",
        "emoji": "⚽",
        "rss": [
            "https://feeds.bbci.co.uk/sport/football/rss.xml",
            "https://www.lequipe.fr/rss/actu_rss_Football.xml",
            "https://www.eurosport.fr/football/rss.xml",
        ],
        "keywords": [
            "football", "soccer", "match", "goal", "league", "champions",
            "premier league", "ligue 1", "serie a", "bundesliga", "la liga",
            "injury", "transfer", "win", "loss", "draw", "world cup", "euro",
            "copa", "ucl", "form", "squad",
        ],
        "ticker": None,
        "trade": False,
    },
    "tennis": {
        "name": "Tennis",
        "emoji": "🎾",
        "rss": [
            "https://feeds.bbci.co.uk/sport/tennis/rss.xml",
            "https://www.eurosport.fr/tennis/rss.xml",
        ],
        "keywords": [
            "tennis", "atp", "wta", "grand slam", "wimbledon", "roland garros",
            "us open", "australian open", "djokovic", "alcaraz", "sinner",
            "swiatek", "final", "semifinal", "injury", "ranking", "tournament",
        ],
        "ticker": None,
        "trade": False,
    },
    "politique": {
        "name": "Politique & Géopolitique",
        "emoji": "🏛️",
        "rss": [
            "https://feeds.bbci.co.uk/news/politics/rss.xml",
            "https://feeds.reuters.com/reuters/politicsNews",
            "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        ],
        "keywords": [
            "election", "vote", "president", "prime minister", "government",
            "war", "peace", "sanctions", "trade", "tariff", "trump", "macron",
            "geopolitical", "conflict", "agreement", "treaty", "summit", "nato",
            "congress", "senate", "parliament", "policy",
        ],
        "ticker": None,
        "trade": False,
    },
}


def fetch_oracle_news(rss_feeds: list, keywords: list, hours_back: int = 4) -> list[dict]:
    from email.utils import parsedate_to_datetime
    articles = []
    cutoff = datetime.now(pytz.utc) - pd.Timedelta(hours=hours_back)

    for url in rss_feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:25]:
                title      = entry.get("title", "")
                summary    = entry.get("summary", "")[:300]
                text_lower = (title + " " + summary).lower()
                if not any(kw in text_lower for kw in keywords):
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


async def oracle_ai_signal(articles: list[dict], domain_key: str, domain: dict,
                           market_df: pd.DataFrame | None = None) -> dict:
    if not GEMINI_API_KEY:
        return {"direction": None, "confidence": 0, "summary": "Clé Gemini manquante"}
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")

        tech_section = ""
        if market_df is not None and len(market_df) >= 50:
            mdf    = compute_indicators(market_df)
            mc     = mdf["Close"].squeeze()
            mprice = float(mc.iloc[-1])
            mrsi   = float(mdf["RSI"].iloc[-1])
            madx   = float(mdf["ADX"].iloc[-1])
            mema200 = float(mdf["EMA200"].iloc[-1])
            mchg24  = float((mc.iloc[-1] / mc.iloc[-24] - 1) * 100) if len(mc) >= 24 else 0
            tech_section = (
                f"\nDONNÉES TECHNIQUES {domain['name'].upper()} :\n"
                f"- Prix : {mprice:.4f} | Variation 24h : {mchg24:+.2f}%\n"
                f"- RSI : {mrsi:.1f} | ADX : {madx:.1f}\n"
                f"- EMA200 : {mema200:.4f} | Tendance : {'HAUSSIÈRE' if mprice > mema200 else 'BAISSIÈRE'}\n"
            )

        news_text = "\n".join([
            f"- [{a['source']}] {a['title']} — {a['summary'][:150]}"
            for a in articles
        ])

        prompt = f"""Tu es Polaris Oracle — IA prédictive niveau institutionnel. Domaine : {domain['name']}.

ACTUALITÉS RÉCENTES ({len(articles)} articles) :
{news_text}
{tech_section}
Réponds UNIQUEMENT en JSON valide :
{{
  "direction": "BUY" ou "SELL" ou "NEUTRAL",
  "confidence": <0-100>,
  "timeframe": "4h" ou "12h" ou "24h" ou "48h",
  "catalysts": ["raison 1", "raison 2"],
  "risk": "LOW" ou "MEDIUM" ou "HIGH",
  "summary": "<1 phrase claire en français>"
}}

Pour {domain['name']} : BUY = signal favorable/hausse/issue positive attendue. SELL = signal défavorable/baisse/issue négative. NEUTRAL = incertain.
Si pas d'info claire → NEUTRAL confidence < 50."""

        resp       = model.generate_content(prompt)
        json_match = re.search(r'\{.*\}', resp.text.strip(), re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return {"direction": None, "confidence": 0, "summary": "Réponse non parsable"}
    except Exception as e:
        logger.error(f"Oracle AI {domain_key}: {e}")
        return {"direction": None, "confidence": 0, "summary": str(e)[:80]}


async def oracle_loop(app: Application):
    logger.info("Polaris Oracle démarré — multi-marché 7 domaines, analyse toutes les 2h")
    last_run: dict[str, str] = {}

    while True:
        try:
            now      = datetime.now(TZ)
            if now.weekday() >= 5:  # Samedi/dimanche — or ne trade pas
                await asyncio.sleep(30 * 60)
                continue
            # Slot change toutes les 2h — chaque domaine tourne une fois par slot
            time_slot = f"{now.strftime('%Y-%m-%d')}-{now.hour // 2}"

            for domain_key, domain in ORACLE_DOMAINS.items():
                slot_key = f"{domain_key}-{time_slot}"
                if last_run.get(domain_key) == slot_key:
                    continue

                articles = fetch_oracle_news(domain["rss"], domain["keywords"], hours_back=4)
                last_run[domain_key] = slot_key

                if not articles:
                    logger.info(f"Oracle {domain_key} — pas de news pertinentes")
                    continue

                market_df = None
                if domain.get("ticker"):
                    market_df = await fetch_async(domain["ticker"], period="10d", interval="1h")

                signal     = await oracle_ai_signal(articles, domain_key, domain, market_df)
                direction  = signal.get("direction")
                confidence = int(signal.get("confidence", 0))
                risk       = signal.get("risk", "MEDIUM")
                risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(risk, "🟡")
                dir_emoji  = "📈" if direction == "BUY" else "📉" if direction == "SELL" else "⚖️"
                catalysts  = "\n".join([f"• {cat}" for cat in signal.get("catalysts", [])])

                msg = (
                    f"{domain['emoji']} *POLARIS ORACLE — {domain['name']}*\n"
                    f"🕐 {now.strftime('%d/%m %H:%M')}\n\n"
                    f"{dir_emoji} Signal : *{direction or 'NEUTRAL'}* | Confiance : `{confidence}%`\n"
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
                        logger.error(f"Oracle Telegram {domain_key}: {e}")

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
                            "domain":         domain_key,
                            "created_at":     now.isoformat(),
                        }).execute()
                        logger.info(f"Oracle {domain_key} — {direction} {confidence}% → Supabase OK")
                    except Exception as e:
                        logger.error(f"Oracle Supabase {domain_key}: {e}")

                # Paper trade uniquement pour domaines financiers avec ticker
                if domain.get("trade") and direction in ("BUY", "SELL") and confidence >= 75:
                    data     = load_data()
                    price_df = await fetch_async(domain["ticker"], period="5d", interval="15m")
                    if price_df is not None and len(price_df) >= 50:
                        price_df = compute_indicators(price_df)
                        trade_price = float(price_df["Close"].squeeze().iloc[-1])
                        trade_atr   = float(price_df["ATR"].iloc[-1])
                        if not pd.isna(trade_atr) and trade_atr > 0:
                            pos = open_trade(data, domain["ticker"], direction, trade_price, trade_atr,
                                             score=int(confidence / 10))
                            if pos and JOHN_ID:
                                try:
                                    await app.bot.send_message(
                                        JOHN_ID,
                                        f"🔮 *Oracle → Trade {domain['name']} ouvert*\n"
                                        f"Confiance `{confidence}%` ≥ 75% → position prise\n"
                                        f"Prix : `{trade_price:.4f}` | *{direction}*\n"
                                        f"SL : `{pos['sl']:.4f}` | TP : `{pos['tp']:.4f}`",
                                        parse_mode="Markdown"
                                    )
                                except Exception:
                                    pass

                await asyncio.sleep(5)  # délai entre domaines — évite rate limit Gemini

        except Exception as e:
            logger.error(f"Oracle loop: {e}")

        await asyncio.sleep(5 * 60)


# ── WATCHDOG SILENCE ──────────────────────────────────────────────────────────
async def no_trade_alert(app: Application, data: dict):
    """Alerte Telegram si aucun trade depuis 48h un jour de semaine."""
    if datetime.now(TZ).weekday() >= 5:
        return
    closed = data.get("closed_trades", [])
    last_dt = None
    if closed:
        try:
            raw = closed[-1].get("exit_time") or closed[-1].get("entry_time", "")
            last_dt = datetime.fromisoformat(raw)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=TZ)
        except Exception:
            pass
    hours_silent = (datetime.now(TZ) - last_dt).total_seconds() / 3600 if last_dt else 999
    if hours_silent >= 24:
        hours_txt = f"{int(hours_silent)}h" if hours_silent < 48 else f"{int(hours_silent // 24)} jours"
        try:
            await app.bot.send_message(
                JOHN_ID,
                f"⚠️ *GOLD BOT — ALERTE SILENCE*\n\n"
                f"Aucun trade depuis *{hours_txt}* !\n"
                f"Capital : `{data.get('capital', 0):.2f} $`\n\n"
                f"_Vérifie : filtres trop stricts, API données, logs Railway._",
                parse_mode="Markdown"
            )
            logger.warning(f"Alerte silence Gold — {hours_silent:.0f}h sans trade")
        except Exception as e:
            logger.error(f"no_trade_alert Gold: {e}")

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

        prompt = f"""Tu es GOLD-E, système de trading XAU/USD auto-évolutif. Analyse ce trade perdant selon 6 axes précis.

TRADE :
- Direction : {pos.get('direction')} | Entrée : {pos.get('entry_price')} → Sortie : {pos.get('exit_price', '?')}
- SL : {pos.get('sl')} | TP : {pos.get('tp')} | Score : {pos.get('score', '?')}/7
- P&L : {pnl:+.2f} EUR | Session : {pos.get('session', '?')} | Raison : {pos.get('exit_reason', 'SL')}

POST-MORTEM (Markdown, 1 ligne par axe, concis et factuel) :

1. **Erreur de setup** : Pattern valide ? Volume suffisant ? Signal trop faible ?
2. **Erreur de timing** : Entrée trop tôt/tard ? Quel indice manquait ?
3. **Erreur macro** : DXY en sens inverse ? Annonce économique ignorée ?
4. **Erreur ML/signal** : Indicateur dominant qui aurait dû bloquer ce trade ?
5. **Erreur risk management** : SL trop serré ? Sizing trop agressif ? Drawdown ignoré ?
6. **Classification** : SYSTÉMATIQUE (va se reproduire) ou CONJONCTUREL (one-off) ?

Termine par : **Leçon GOLD-E** : "La prochaine fois, [action corrective concrète en 1 phrase]." """

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


async def gemini_param_adjustment(app: Application, data: dict):
    """Niveau 2 — Gemini analyse les trades et ajuste réellement les paramètres."""
    if not GEMINI_API_KEY:
        return
    recent = data.get("closed_trades", [])[-30:]
    if len(recent) < 5:
        return

    by_inst: dict = {}
    for t in recent:
        tk = t.get("ticker", "?")
        if tk not in by_inst:
            by_inst[tk] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if t.get("pnl", 0) > 0: by_inst[tk]["wins"]   += 1
        else:                    by_inst[tk]["losses"] += 1
        by_inst[tk]["pnl"] += t.get("pnl", 0)

    inst_summary = "\n".join(f"- {k}: {v['wins']}G/{v['losses']}P | P&L {v['pnl']:+.2f}€" for k, v in by_inst.items())
    wr = sum(1 for t in recent if t.get("pnl", 0) > 0) / len(recent) * 100
    current = data.get("learned_params", {})
    _default_params = {"threshold": 4, "risk_per_trade": 0.01, "sl_mult": 1.5, "tp_mult": 3.75}

    prompt = f"""Bot de trading Or (XAU/USD). Analyse et recommande des ajustements JSON.

PERFORMANCE ({len(recent)} trades) :
- Win rate : {wr:.1f}%
- P&L : {sum(t.get('pnl',0) for t in recent):+.2f}€

PAR INSTRUMENT :
{inst_summary}

PARAMS ACTUELS : {json.dumps(current if current else _default_params)}

RÈGLES : Si instrument 0G/2+P → blacklist 7j. Si WR<40% → réduire risque, augmenter seuil.

JSON UNIQUEMENT (pas de markdown) :
{{"threshold":4,"risk_per_trade":0.01,"sl_mult":1.5,"tp_mult":3.75,"blacklist":{{"TICKER":86400}},"rationale":"explication"}}
"""
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model  = genai.GenerativeModel("gemini-2.5-flash")
        resp   = model.generate_content(prompt)
        raw    = resp.text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json\n"):
                raw = raw[5:]
        params_new = json.loads(raw)

        learned = {
            "threshold":      max(3, min(6,   int(params_new.get("threshold",      4)))),
            "risk_per_trade": max(0.003, min(0.02, float(params_new.get("risk_per_trade", 0.01)))),
            "sl_mult":        max(1.0, min(3.0,    float(params_new.get("sl_mult",        1.5)))),
            "tp_mult":        max(2.0, min(6.0,    float(params_new.get("tp_mult",        3.75)))),
            "rationale":      str(params_new.get("rationale", "")),
            "updated_at":     datetime.now(TZ).isoformat(),
        }
        for tk, secs in params_new.get("blacklist", {}).items():
            data.setdefault("instrument_blacklist", {})[tk] = datetime.now(TZ).timestamp() + int(secs)

        data["learned_params"] = learned
        save_data(data)
        save_learned_params(learned)

        try:
            await app.bot.send_message(
                JOHN_ID,
                f"🤖 *Gold Bot — Paramètres auto-ajustés*\n\n"
                f"Seuil : `{learned['threshold']}/7` | Risque : `{learned['risk_per_trade']*100:.1f}%`\n"
                f"SL : `{learned['sl_mult']}×ATR` | TP : `{learned['tp_mult']}×ATR`\n\n"
                f"💬 _{learned['rationale']}_",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        logger.info(f"Params Gemini Gold Bot appliqués: {learned}")
    except json.JSONDecodeError as e:
        logger.error(f"Gemini params JSON invalide Gold Bot: {e}")
    except Exception as e:
        logger.error(f"gemini_param_adjustment Gold Bot: {e}")


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
    peak      = data.get("peak_capital", CAPITAL_INITIAL)
    dd_pct    = (peak - data["capital"]) / peak * 100 if peak > 0 else 0.0
    dxy_dir   = get_dxy_direction()

    analysis = "Analyse indisponible."
    if GEMINI_API_KEY:
        try:
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel("gemini-2.5-flash")
            trades_summary = "\n".join([
                f"- {t.get('ticker')} {t.get('direction')} Score:{t.get('score','?')}/7 P&L:{t.get('pnl',0):+.2f}€ ({t.get('exit_reason','?')})"
                for t in week_trades[-20:]
            ]) or "Aucun trade."

            prompt = f"""Analyse la semaine de trading GOLD-E (Markdown, 5 points concis).

STATS : {len(week_trades)} trades | {wr:.1f}% WR | P&L semaine {total_pnl:+.2f} EUR | Capital {data['capital']:.2f} EUR ({pct:+.2f}%)
Drawdown max semaine : {dd_pct:.1f}% | DXY fin de semaine : {dxy_dir}
Meilleur : {f"{best['ticker']} {best['direction']} +{best['pnl']:.2f}€" if best else "N/A"}
Pire : {f"{worst['ticker']} {worst['direction']} {worst['pnl']:+.2f}€" if worst else "N/A"}

TRADES :
{trades_summary}

1. **Performance globale** : Bonne/mauvaise semaine ?
2. **Patterns d'erreurs** : Quelles erreurs reviennent ?
3. **Impact DXY** : Le Dollar a-t-il influencé les résultats ? (DXY {dxy_dir})
4. **Forces identifiées** : Ce qui fonctionne
5. **Actions semaine prochaine** : 2-3 ajustements concrets"""

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
                f"📊 {len(week_trades)} trades | {wr:.1f}% WR | `{total_pnl:+.2f} EUR`\n"
                f"📉 Drawdown semaine : `{dd_pct:.1f}%` | DXY : `{dxy_dir}`\n\n"
                f"{analysis[:700]}\n\n💾 _Sauvegardé dans le wiki_",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    logger.info(f"Audit hebdomadaire envoyé: {slug}")
    await gemini_param_adjustment(app, data)


# ── BOUCLE DE TRADING ──────────────────────────────────────────────────────────
async def trading_loop(app: Application):
    logger.info("Boucle de trading démarrée — vérification toutes les 5 min")
    await init_ml_db(app)
    global _ml_model, _ml_auc
    _ml_model = None  # ML entraîné depuis Supabase après 50 trades
    cycle         = 0
    last_ml_train = 0  # cycle du dernier entraînement ML

    while True:
        try:
            data        = load_data()
            instruments = get_instruments()
            cycle += 1
            hourly_lines = []

            # Exits toujours surveillés — même hors session (évite positions bloquées overnight)
            for ticker, info in instruments.items():
                df_exit = await fetch_async(ticker)
                if df_exit is None or len(df_exit) < 10:
                    continue
                price_exit = float(df_exit["Close"].squeeze().iloc[-1])
                exits = check_exits(data, ticker, price_exit)
                data  = load_data()
                for pos, reason in exits:
                    pnl_e   = pos.get("pnl", 0)
                    outcome = 1 if pnl_e > 0 else 0
                    update_trade_outcome(pos.get("supabase_id", ""), outcome, pnl_e)
                    em  = "✅" if pnl_e > 0 else "❌"
                    rst = len(data.get("open_positions", []))
                    rst_txt = "Aucun trade en cours" if rst == 0 else f"{rst} trade(s) en cours"
                    msg = (
                        f"{em} *Trade fermé — {info['name']}*\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Direction : {pos['direction']} | Score : `{pos.get('score','?')}/7`\n"
                        f"Entrée : `{pos['entry_price']:.4f}` → Sortie : `{price_exit:.4f}`\n"
                        f"Raison : {reason}\n"
                        f"P&L : `{pnl_e:+.2f} €` | Capital : `{data['capital']:.2f} €`\n"
                        f"📊 {rst_txt}"
                    )
                    try:
                        await app.bot.send_message(JOHN_ID, msg, parse_mode="Markdown")
                    except Exception:
                        pass
                    if JOETRADE_GROUP_ID:
                        try:
                            import os as _os
                            _img = "trade_gagnant.jpg" if pnl_e > 0 else "trade_perdant.jpg"
                            if _os.path.exists(_img):
                                with open(_img, "rb") as _f:
                                    await app.bot.send_photo(JOETRADE_GROUP_ID, photo=_f, caption=msg, parse_mode="Markdown", message_thread_id=JOETRADE_THREAD_GOLD)
                            else:
                                await app.bot.send_message(JOETRADE_GROUP_ID, msg, parse_mode="Markdown", message_thread_id=JOETRADE_THREAD_GOLD)
                        except Exception:
                            pass
                    if pnl_e < 0:
                        asyncio.create_task(post_mortem_analysis(app, pos))

            # Weekend — or ne trade pas, aucune analyse ni notification
            if datetime.now(TZ).weekday() >= 5:
                await asyncio.sleep(30 * 60)
                continue

            # Filtre session : pas de NOUVEAUX trades hors London/NY
            if not is_trading_session():
                logger.info("Hors session — exits surveillés, pas de nouveaux trades")
                await asyncio.sleep(30 * 60)
                continue

            # Blackout 21h-00h UTC — gap asiatique
            if is_blackout_session():
                logger.info("Blackout 21h-00h UTC — aucun nouveau trade")
                await asyncio.sleep(30 * 60)
                continue

            # Drawdown pause
            dd = get_drawdown(data)
            pause_until = data.get("drawdown_pause_until")
            if pause_until and datetime.now(TZ).isoformat() < pause_until:
                logger.warning(f"Drawdown pause active jusqu'à {pause_until}")
                await asyncio.sleep(30 * 60)
                continue
            if dd >= DRAWDOWN_PAUSE:
                pause_dt = (datetime.now(TZ) + timedelta(hours=48)).isoformat()
                data["drawdown_pause_until"] = pause_dt
                save_data(data)
                try:
                    await app.bot.send_message(
                        JOHN_ID,
                        f"🛑 *GOLD-E — Pause 48h*\n\nDrawdown : `{dd:.1%}` ≥ seuil `{DRAWDOWN_PAUSE:.0%}`\n"
                        f"Reprise : `{pause_dt[:16]}`\nCapital : `{data['capital']:.2f} €`",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                await asyncio.sleep(30 * 60)
                continue

            # Macro blackout
            if is_macro_blackout():
                logger.info("Macro blackout — annonce haute-impact imminente")
                await asyncio.sleep(10 * 60)
                continue

            # DXY direction (cache 30min)
            dxy_dir = get_dxy_direction()

            # Réentraîner ML tous les 50 cycles (~4h) si nouveau modèle possible
            if cycle - last_ml_train >= 50:
                _ml_model, _ml_auc = train_and_save_ml()
                last_ml_train = cycle
                if _ml_auc >= 0.55:
                    data["ml_active"] = True
                    data["ml_auc"]    = _ml_auc
                    save_data(data)
                    logger.info(f"ML activé — AUC {_ml_auc:.3f}")

            # Nettoyer blacklist expirée
            now_ts = datetime.now(TZ).timestamp()
            bl = data.get("instrument_blacklist", {})
            expired = [k for k, exp in bl.items() if now_ts > exp]
            for k in expired:
                del bl[k]
                data.setdefault("instrument_losses", {})[k] = 0
            if expired:
                save_data(data)

            # Pause après 3 pertes consécutives (Druckenmiller — préserver le capital)
            if data.get("loss_streak", 0) >= 3:
                logger.info(f"Pause trading — {data['loss_streak']} pertes consécutives")
                await asyncio.sleep(2 * 60 * 60)
                data["loss_streak"] = 0  # Reset après pause — évite boucle infinie
                save_data(data)
                continue

            params = adaptive_params(data)
            logger.info(f"Mode adaptatif : {params['mode']} — seuil {params['threshold']}/7")

            for ticker, info in instruments.items():
                if ticker in data.get("instrument_blacklist", {}):
                    logger.info(f"Skip {ticker} — blacklisté")
                    continue

                df = await fetch_async(ticker)
                if df is None or len(df) < 50:
                    continue

                df    = compute_indicators(df)
                price = float(df["Close"].squeeze().iloc[-1])
                atr   = float(df["ATR"].iloc[-1])
                if pd.isna(atr) or atr <= 0:
                    continue

                pattern              = detect_candlestick_pattern(df)
                direction, score, reasons = compute_signal_score(df)

                rsi_val = float(df["RSI"].iloc[-1])
                adx_val = float(df["ADX"].iloc[-1])
                arrow   = "📈" if direction == "BUY" else ("📉" if direction == "SELL" else "⏸")

                # DXY confirmation — XAU/USD corrélation inverse
                dxy_label = f"DXY {dxy_dir}"
                if direction == "BUY" and dxy_dir == "UP":
                    logger.info(f"Skip {ticker} BUY — DXY hausse (bearish XAU)")
                    direction = None
                elif direction == "SELL" and dxy_dir == "DOWN":
                    logger.info(f"Skip {ticker} SELL — DXY baisse (bullish XAU)")
                    direction = None

                if direction:
                    # 1H + 4H multi-timeframe : signal aligné avec les deux tendances macro
                    trend_1h = get_1h_trend(df)
                    trend_4h = get_4h_trend(ticker)
                    dir_map  = {"BUY": "UP", "SELL": "DOWN"}
                    if trend_1h != "NEUTRAL" and trend_1h != dir_map.get(direction):
                        logger.info(f"Skip {ticker} — signal {direction} contre tendance 1H ({trend_1h})")
                        direction = None
                    if direction and trend_4h != "NEUTRAL" and trend_4h != dir_map.get(direction):
                        if score >= 6:
                            logger.info(f"Override 4H {ticker} — score {score}/7 suffisant malgré tendance 4H ({trend_4h})")
                        else:
                            logger.info(f"Skip {ticker} — signal {direction} contre tendance 4H ({trend_4h})")
                            direction = None

                if direction:
                    # TEMA momentum guard — momentum TEMA aligné avec direction
                    tema      = float(df["TEMA"].iloc[-1])
                    tema_prev = float(df["TEMA"].iloc[-2])
                    if direction == "BUY" and tema < tema_prev:
                        logger.info(f"Skip {ticker} BUY — TEMA baissier ({tema:.2f} < {tema_prev:.2f})")
                        direction = None
                    elif direction == "SELL" and tema > tema_prev:
                        logger.info(f"Skip {ticker} SELL — TEMA haussier ({tema:.2f} > {tema_prev:.2f})")
                        direction = None

                # ML prediction (si modèle actif — AUC >= 0.55)
                ml_proba = -1.0
                ml_label = ""
                if direction:
                    feats = collect_features(df, data, direction, dxy_dir)
                    feats["score"] = score
                    ml_proba = predict_ml_proba(feats)
                    if ml_proba >= 0:
                        ml_label = f" | ML `{ml_proba:.0%}`"
                        if ml_proba < 0.50:
                            logger.info(f"Skip {ticker} — ML proba {ml_proba:.2f} < 0.50")
                            direction = None
                        elif ml_proba < 0.55:
                            # Sizing réduit géré dans open_trade via risk override
                            logger.info(f"{ticker} ML proba {ml_proba:.2f} → sizing 0.5%")
                            if params:
                                params = dict(params)
                                params["risk_per_trade"] = min(params.get("risk_per_trade", 0.01), 0.005)

                hourly_lines.append(
                    f"{arrow} *{info['name']}* — Score `{score}/7` | `{price:.2f}` | RSI `{rsi_val:.1f}` | ADX `{adx_val:.1f}` | {dxy_label}{ml_label}"
                )

                # ── Écriture analyse en temps réel → Supabase bot_analysis ──
                if sb_client and ticker == "XAUUSD=X":
                    try:
                        fibs = fibonacci_levels(df)
                        trend_now = get_1h_trend(df)
                        sl_m = (params or {}).get("sl_mult", 1.5)
                        tp_m = (params or {}).get("tp_mult", 2.5)
                        p_entry = round(price, 2) if direction else None
                        p_sl    = round(price - atr*sl_m if direction=="BUY" else price + atr*sl_m, 2) if direction else None
                        p_tp    = round(price + atr*tp_m if direction=="BUY" else price - atr*tp_m, 2) if direction else None
                        sb_client.table("bot_analysis").upsert({
                            "bot":          "gold",
                            "ticker":       ticker,
                            "timestamp":    datetime.now(TZ).isoformat(),
                            "price":        round(price, 2),
                            "direction":    direction,
                            "score":        score,
                            "rsi":          round(rsi_val, 2),
                            "adx":          round(adx_val, 2),
                            "atr":          round(atr, 4),
                            "trend_1h":     trend_now,
                            "dxy_dir":      dxy_dir,
                            "fib_high":      round(fibs["high"], 2),
                            "fib_low":       round(fibs["low"], 2),
                            "fib_high_time": fibs.get("fib_high_time"),
                            "fib_low_time":  fibs.get("fib_low_time"),
                            "fib_786":      round(fibs["fib_786"], 2),
                            "fib_618":      round(fibs["fib_618"], 2),
                            "fib_5":        round(fibs["fib_5"], 2),
                            "fib_382":      round(fibs["fib_382"], 2),
                            "fib_236":      round(fibs["fib_236"], 2),
                            "planned_entry": p_entry,
                            "planned_sl":    p_sl,
                            "planned_tp":    p_tp,
                        }, on_conflict="bot").execute()
                        logger.info("bot_analysis mis à jour Supabase")
                    except Exception as _e:
                        logger.warning(f"bot_analysis write: {_e}")

                if direction:
                    feats_final = collect_features(df, data, direction, dxy_dir)
                    feats_final["score"] = score
                    # Sync capital depuis Supabase avant ouverture trade
                    if sb_client:
                        try:
                            _sr = sb_client.table("bot_state").select("capital").eq("id", 1).execute()
                            if _sr.data:
                                _sb_cap = float(_sr.data[0].get("capital") or 0)
                                if _sb_cap > 0 and _sb_cap < data["capital"] * 0.9:
                                    logger.info(f"Capital sync avant trade: {data['capital']:.2f} → {_sb_cap:.2f}")
                                    data["capital"] = _sb_cap
                                    data["peak_capital"] = _sb_cap
                        except Exception as _ce:
                            logger.warning(f"Capital sync avant trade: {_ce}")
                    pos = open_trade(data, ticker, direction, price, atr, score, params=params)
                    data = load_data()
                    if pos:
                        log_trade_features(feats_final, pos.get("supabase_id", ""))
                        pos["session"] = get_current_session()
                        em  = "📈" if direction == "BUY" else "📉"
                        pat = f"\n📊 Pattern : `{pattern}`" if pattern else ""
                        session_str = get_current_session()
                        ml_str = f"\n🤖 ML : `{ml_proba:.0%}`" if ml_proba >= 0 else ""
                        msg = (
                            f"{em} *TRADE EN COURS — {info['name']}*\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"Session : `{session_str}` | DXY : `{dxy_dir}`\n"
                            f"Direction : *{direction}* | Score : `{score}/7`\n"
                            f"Prix d'entrée : `{price:.4f}`\n"
                            f"Stop-Loss : `{pos['sl']:.4f}`\n"
                            f"Take-Profit : `{pos['tp']:.4f}`{pat}{ml_str}\n\n"
                            f"*Signaux :*\n" + "\n".join(reasons[:4])
                        )
                        try:
                            await app.bot.send_message(JOHN_ID, msg, parse_mode="Markdown")
                        except Exception:
                            pass
                        if JOETRADE_GROUP_ID:
                            try:
                                await app.bot.send_message(JOETRADE_GROUP_ID, msg, parse_mode="Markdown", message_thread_id=JOETRADE_THREAD_GOLD)
                            except Exception:
                                pass

            # Résumé toutes les heures (cycle 12 = 12×5min)
            if cycle % 12 == 0 and hourly_lines:
                now_str  = datetime.now(TZ).strftime("%H:%M")
                sess_str = get_current_session()
                dd_str   = f"{get_drawdown(data):.1%}"
                summary  = f"🕐 *Surveillance {now_str} — {sess_str} | DD: {dd_str}*\n\n" + "\n".join(hourly_lines)
                try:
                    await app.bot.send_message(JOHN_ID, summary, parse_mode="Markdown")
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Erreur boucle trading: {e}")
            try:
                await app.bot.send_message(JOHN_ID, f"⚠️ Gold Bot — erreur boucle trading: {e}")
            except Exception:
                pass

        await asyncio.sleep(5 * 60)


# ── SYNC CAPITAL RESET ─────────────────────────────────────────────────────────
async def check_capital_reset_gold(data: dict, app):
    """Détecte reset externe capital via bot_state Supabase (Sofia reset mensuel)."""
    if not sb_client:
        return
    try:
        res = sb_client.table("bot_state").select("capital, total_pnl").eq("id", 1).execute()
        if not res.data:
            return
        sb_cap = float(res.data[0].get("capital") or 0)
        if sb_cap > 0 and sb_cap < data["capital"] * 0.5 and abs(sb_cap - data["capital"]) > 5.0:
            old_cap = data["capital"]
            data["capital"]       = sb_cap
            data["peak_capital"]  = sb_cap
            data["daily_pnl"]     = 0.0
            data["total_pnl"]     = 0.0
            data["closed_trades"] = []
            save_data(data)
            logger.info(f"Reset capital Gold appliqué: {old_cap:.2f} → {sb_cap:.2f}")
            await app.bot.send_message(JOHN_ID, f"♻️ Reset capital Gold Bot: {old_cap:.2f}→{sb_cap:.2f} $")
    except Exception as e:
        logger.warning(f"check_capital_reset_gold: {e}")


# ── PLANIFICATEUR ──────────────────────────────────────────────────────────────
async def scheduler(app: Application):
    last_morning = ""
    last_evening = ""
    last_audit   = ""
    last_cap_check = ""
    while True:
        try:
            now   = datetime.now(TZ)
            today = now.strftime("%Y-%m-%d")
            h, m  = now.hour, now.minute

            if m < 5 and last_cap_check != f"{today}-{h}":
                data = load_data()
                await check_capital_reset_gold(data, app)
                last_cap_check = f"{today}-{h}"

            if h == 7 and m < 15 and last_morning != today and now.weekday() < 5:
                await morning_report(app)
                data_w = load_data()
                await no_trade_alert(app, data_w)
                last_morning = today

            if h == 13 and m < 5 and last_cap_check != f"{today}-13" and now.weekday() < 5:
                data_w = load_data()
                await no_trade_alert(app, data_w)

            if h == 22 and m < 15 and last_evening != today and now.weekday() < 5:
                await evening_report(app)
                await _push_gold_wiki()
                last_evening = today

            if h == 8 and m < 15 and now.weekday() == 6 and last_audit != today:
                data = load_data()
                await weekly_audit(app, data)
                last_audit = today
        except Exception as e:
            logger.error(f"scheduler: {e}")
            try:
                await app.bot.send_message(JOHN_ID, f"⚠️ Erreur dans le planificateur Gold Bot: {e}")
            except Exception:
                pass

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
    all_inst = WEEKDAY_INSTRUMENTS
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

        sig_txt = f"*{direction}* (Score: {score}/7)" if direction else f"Pas de signal ({score}/7 requis: 5)"
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
                "🥇 Or (XAU/USD) + 🥈 Argent (XAG/USD)\n\n"
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
