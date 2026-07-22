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

# Mapping ticker Gold Bot → candidats de symbole MT5 (varie selon le broker :
# RaiseGlobalSA utilise "Gold"/"Silver", d'autres "XAUUSD", "GOLD.r", etc.)
# resolve_symbol() teste ces noms puis, en dernier recours, cherche dans la liste
# complète des symboles du broker un nom contenant gold/xau (ou silver/xag).
SYMBOL_CANDIDATES = {
    "XAUUSD=X": ["Gold", "gold", "XAUUSD", "GOLD", "XAUUSD.r", "XAUUSD.raw", "GOLD.r", "GOLDUSD"],
    "XAGUSD=X": ["Silver", "silver", "XAGUSD", "SILVER", "XAGUSD.r", "XAGUSD.raw", "SILVERUSD"],
}

_symbol_cache = {}

def resolve_symbol(ticker: str) -> str | None:
    """Trouve le vrai nom du symbole chez CE broker. Résultat mis en cache."""
    if ticker in _symbol_cache:
        return _symbol_cache[ticker]
    # 1) Essaie les noms connus
    for cand in SYMBOL_CANDIDATES.get(ticker, [ticker]):
        if mt5.symbol_info(cand) is not None:
            _symbol_cache[ticker] = cand
            logger.info(f"Symbole résolu {ticker} → {cand}")
            return cand
    # 2) Recherche par mot-clé dans tous les symboles du broker
    keyword = "xau" if "XAU" in ticker else ("xag" if "XAG" in ticker else "")
    alt     = "gold" if keyword == "xau" else ("silver" if keyword == "xag" else "")
    for s in (mt5.symbols_get() or []):
        name = s.name.lower()
        if keyword and (keyword in name or alt in name):
            _symbol_cache[ticker] = s.name
            logger.info(f"Symbole résolu par recherche {ticker} → {s.name}")
            return s.name
    logger.error(f"Aucun symbole trouvé pour {ticker} chez ce broker")
    return None

MAX_LOT_SIZE = 0.03  # cap prop firm RaiseMyFund — jamais plus de 0.03 lots


MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

def ensure_mt5():
    """Initialise MT5 si pas encore fait — pointe vers RaiseGlobalSA (pas OANDA)."""
    if not mt5.initialize(path=MT5_PATH):
        logger.error(f"MT5 initialize() échoué: {mt5.last_error()}")
        return False
    return True


def convert_to_lots(symbol: str, qty: float) -> float:
    """Convertit qty (onces) en lots MT5 en utilisant la vraie taille de contrat du broker."""
    info = mt5.symbol_info(symbol)
    # trade_contract_size = onces par lot (100 pour l'or chez la plupart des brokers)
    contract = info.trade_contract_size if info and info.trade_contract_size else 100.0
    lots = qty / contract
    vol_min  = info.volume_min  if info else 0.01
    vol_step = info.volume_step if info else 0.01
    # Arrondi au step, minimum vol_min, maximum MAX_LOT_SIZE
    lots = max(vol_min, round(round(lots / vol_step) * vol_step, 2))
    lots = min(lots, MAX_LOT_SIZE)
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


@app.route("/account", methods=["GET"])
def account():
    """Chiffres réels du compte MT5 — balance, equity, nombre de trades réellement exécutés."""
    if request.headers.get("X-Token") != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    if not ensure_mt5():
        return jsonify({"error": "MT5 non disponible"}), 500

    info = mt5.account_info()
    if info is None:
        return jsonify({"error": "Pas de compte connecté"}), 500

    from datetime import datetime as _dt
    deals = mt5.history_deals_get(_dt(2020, 1, 1), _dt.now()) or []
    # DEAL_ENTRY_OUT = fermeture de position → 1 deal = 1 trade réellement clôturé
    trades_count = len([d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT and d.magic == MT5_MAGIC])

    return jsonify({
        "ok": True,
        "login": info.login,
        "server": info.server,
        "balance": info.balance,
        "equity": info.equity,
        "profit": info.profit,
        "trades_count": trades_count,
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

    if not ensure_mt5():
        return jsonify({"error": "MT5 non disponible"}), 500

    symbol = resolve_symbol(ticker)
    if not symbol:
        return jsonify({"error": f"Aucun symbole pour {ticker} chez ce broker"}), 400

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


@app.route("/modify", methods=["POST"])
def modify_position():
    """Modifie le SL/TP d'une position ouverte (trailing stop du bot)."""
    if request.headers.get("X-Token") != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    data   = request.json or {}
    ticket = int(data.get("ticket", 0))
    sl     = float(data.get("sl", 0) or 0)
    tp_raw = data.get("tp")

    if ticket <= 0 or sl <= 0:
        return jsonify({"error": f"paramètres invalides ticket={ticket} sl={sl}"}), 400

    if not ensure_mt5():
        return jsonify({"error": "MT5 non disponible"}), 500

    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return jsonify({"error": f"Position {ticket} introuvable"}), 404

    pos    = positions[0]
    digits = mt5.symbol_info(pos.symbol).digits

    request_dict = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   pos.symbol,
        "position": ticket,
        "sl":       round(sl, digits),
        "tp":       round(float(tp_raw), digits) if tp_raw else pos.tp,
    }

    result = mt5.order_send(request_dict)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        logger.info(f"Position {ticket} modifiée: SL={sl} TP={tp_raw or pos.tp}")
        return jsonify({"ok": True})

    err = result.comment if result else str(mt5.last_error())
    logger.error(f"Modification {ticket} échouée: {err}")
    return jsonify({"error": err, "retcode": result.retcode if result else None}), 500


@app.route("/positions_status", methods=["POST"])
def positions_status():
    """Vérifie si des tickets (positions ouvertes côté bot) sont toujours ouverts dans MT5,
    ou ont été fermés (manuellement ou par SL/TP MT5 natif) — renvoie le profit réel si fermé."""
    if request.headers.get("X-Token") != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    if not ensure_mt5():
        return jsonify({"error": "MT5 non disponible"}), 500

    body    = request.json or {}
    tickets = body.get("tickets", [])
    result  = {}

    for raw_ticket in tickets:
        try:
            ticket = int(raw_ticket)
        except (TypeError, ValueError):
            continue
        pos = mt5.positions_get(ticket=ticket)
        if pos:
            result[str(ticket)] = {"open": True}
        else:
            deals  = mt5.history_deals_get(position=ticket) or []
            profit = sum(d.profit + d.swap + d.commission for d in deals)
            result[str(ticket)] = {"open": False, "profit": round(profit, 2)}

    return jsonify(result)


if __name__ == "__main__":
    logger.info("Initialisation MT5...")
    if not mt5.initialize(path=MT5_PATH):
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
