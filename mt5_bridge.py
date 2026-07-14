"""
MT5 Bridge — serveur local sur ton PC Windows
Reçoit les ordres de Gold Bot (Railway) et les exécute dans MT5.

Démarrage :
    pip install flask MetaTrader5
    set MT5_BRIDGE_TOKEN=ton_token_secret
    python mt5_bridge.py

Cloudflare Tunnel (pour exposer ce serveur sur internet) :
    cloudflared tunnel --url http://localhost:5678
    → copie l'URL https://xxxx.trycloudflare.com dans Railway : MT5_BRIDGE_URL
"""

import os
import logging
from flask import Flask, request, jsonify
import MetaTrader5 as mt5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mt5_bridge")

app = Flask(__name__)

SECRET_TOKEN = os.environ.get("MT5_BRIDGE_TOKEN", "CHANGE_ME_TOKEN")
MT5_MAGIC    = 20260714  # identifiant unique GoldBot

# Mapping ticker Gold Bot → symbole MT5
SYMBOL_MAP = {
    "XAUUSD=X": "XAUUSD",
    "XAGUSD=X": "XAGUSD",
}

# Conversion units OANDA → lots MT5
# XAUUSD : 1 lot MT5 = 100 oz → divise qty par 100
LOT_DIVISOR = {
    "XAUUSD": 100.0,
    "XAGUSD": 5000.0,
}


def ensure_mt5():
    """Initialise MT5 si pas encore fait."""
    if not mt5.initialize():
        logger.error(f"MT5 initialize() échoué: {mt5.last_error()}")
        return False
    return True


def convert_to_lots(symbol: str, qty: float) -> float:
    """Convertit qty (unités OANDA/oz) en lots MT5."""
    divisor = LOT_DIVISOR.get(symbol, 100.0)
    lots = qty / divisor
    info = mt5.symbol_info(symbol)
    vol_min  = info.volume_min  if info else 0.01
    vol_step = info.volume_step if info else 0.01
    # Arrondi au step, minimum vol_min
    lots = max(vol_min, round(round(lots / vol_step) * vol_step, 2))
    return lots


@app.route("/health", methods=["GET"])
def health():
    ok = mt5.initialize()
    info = mt5.account_info()
    return jsonify({
        "ok": ok,
        "account": info.login if info else None,
        "balance": info.balance if info else None,
        "server": info.server if info else None,
    })


@app.route("/order", methods=["POST"])
def place_order():
    # Auth
    if request.headers.get("X-Token") != SECRET_TOKEN:
        logger.warning("Requête non autorisée (mauvais token)")
        return jsonify({"error": "unauthorized"}), 401

    data = request.json or {}
    action  = data.get("action", "").upper()   # "BUY" ou "SELL"
    ticker  = data.get("ticker", "XAUUSD=X")   # format Gold Bot
    qty     = float(data.get("qty", 0))
    sl      = float(data.get("sl", 0))
    tp      = float(data.get("tp", 0))

    if action not in ("BUY", "SELL") or qty <= 0:
        return jsonify({"error": f"paramètres invalides action={action} qty={qty}"}), 400

    symbol = SYMBOL_MAP.get(ticker, "XAUUSD")

    if not ensure_mt5():
        return jsonify({"error": "MT5 non disponible"}), 500

    # Activer le symbole si besoin
    if not mt5.symbol_select(symbol, True):
        return jsonify({"error": f"Symbole {symbol} introuvable dans MT5"}), 400

    volume = convert_to_lots(symbol, qty)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return jsonify({"error": f"Pas de tick pour {symbol}"}), 500

    if action == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid

    digits = mt5.symbol_info(symbol).digits
    sl = round(sl, digits)
    tp = round(tp, digits)

    request_dict = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "symbol":      symbol,
        "volume":      volume,
        "type":        order_type,
        "price":       price,
        "sl":          sl,
        "tp":          tp,
        "deviation":   20,
        "magic":       MT5_MAGIC,
        "comment":     "GoldBot",
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    logger.info(f"Ordre MT5: {action} {volume} lots {symbol} @ {price} SL={sl} TP={tp}")
    result = mt5.order_send(request_dict)

    if result is None:
        err = mt5.last_error()
        logger.error(f"order_send None: {err}")
        return jsonify({"error": str(err)}), 500

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        logger.info(f"Ordre OK — ticket #{result.order}")
        return jsonify({"ok": True, "ticket": result.order, "volume": volume})

    logger.error(f"Ordre échoué: retcode={result.retcode} comment={result.comment}")
    return jsonify({"error": result.comment, "retcode": result.retcode}), 500


@app.route("/close", methods=["POST"])
def close_order():
    """Ferme une position MT5 par ticket."""
    if request.headers.get("X-Token") != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    data   = request.json or {}
    ticket = int(data.get("ticket", 0))
    if ticket <= 0:
        return jsonify({"error": "ticket invalide"}), 400

    if not ensure_mt5():
        return jsonify({"error": "MT5 non disponible"}), 500

    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return jsonify({"error": f"Position {ticket} introuvable"}), 404

    pos    = positions[0]
    symbol = pos.symbol
    volume = pos.volume
    tick   = mt5.symbol_info_tick(symbol)

    if pos.type == mt5.ORDER_TYPE_BUY:
        close_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        close_type = mt5.ORDER_TYPE_BUY
        price = tick.ask

    request_dict = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "symbol":      symbol,
        "volume":      volume,
        "type":        close_type,
        "position":    ticket,
        "price":       price,
        "deviation":   20,
        "magic":       MT5_MAGIC,
        "comment":     "GoldBot_close",
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request_dict)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        logger.info(f"Position {ticket} fermée OK")
        return jsonify({"ok": True})

    err = result.comment if result else str(mt5.last_error())
    logger.error(f"Fermeture {ticket} échouée: {err}")
    return jsonify({"error": err}), 500


if __name__ == "__main__":
    logger.info("Initialisation MT5...")
    if not mt5.initialize():
        logger.error(f"MT5 non disponible: {mt5.last_error()}")
        logger.error("Assure-toi que MT5 est ouvert et connecté à RaiseGlobalSA-live")
        exit(1)

    info = mt5.account_info()
    if info:
        logger.info(f"Connecté: compte {info.login} — solde {info.balance}$ — serveur {info.server}")
    else:
        logger.warning("MT5 initialisé mais pas de compte connecté")

    logger.info(f"Bridge démarré sur http://0.0.0.0:5678")
    logger.info(f"Token: {SECRET_TOKEN[:8]}...")
    app.run(host="0.0.0.0", port=5678, debug=False)
