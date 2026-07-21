"""
Gold Bot — Optimisation Optuna avec validation walk-forward
- Données 15m (comme le bot live), 60 jours max (limite yfinance intraday)
- Simule le timeout MAX_POSITION_HOURS comme en live
- Optimise sur les premiers 70% (train), valide sur les 30% restants (test)
  → un paramètre n'est retenu que s'il tient sur des données jamais vues
- Écrit params_optuna.json (lu par bot.py au démarrage) — plus de patch regex de bot.py

Limites connues : stratégie simplifiée (pas de Stoch/Williams/OTE/FVG/OB, ni filtres
DXY/session/1H/4H/TEMA/ML). Les résultats sont indicatifs — valider en démo avant le réel.
"""

import warnings
warnings.filterwarnings("ignore")
import json
import numpy as np
import pandas as pd
import yfinance as yf
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)

TIMEOUT_BARS = 6 * 4   # MAX_POSITION_HOURS (6h) en bougies 15m — aligné sur bot.py
RISK         = 0.01    # 1% par trade — aligné sur bot.py


# ── DONNÉES ───────────────────────────────────────────────────────────────────
def fetch_data():
    print("Téléchargement XAU/USD 60 jours (15m)...")
    # GC=F : proxy le plus fiable en 15m sur yfinance. Basis vs spot ≈ constant
    # sur 60j → sans impact sur l'optimisation relative des paramètres.
    df = yf.download("GC=F", period="60d", interval="15m", progress=False)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.dropna()
    print(f"  {len(df)} bougies chargées")
    return df


# ── INDICATEURS ───────────────────────────────────────────────────────────────
def compute_indicators(df, p):
    c = df["Close"].squeeze()
    h = df["High"].squeeze()
    l = df["Low"].squeeze()

    df = df.copy()
    df["EMA_S"]  = c.ewm(span=p["ema_s"], adjust=False).mean()
    df["EMA_M"]  = c.ewm(span=p["ema_m"], adjust=False).mean()
    df["EMA200"] = c.ewm(span=200,        adjust=False).mean()

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    df["MACD_hist"] = macd - macd.ewm(span=9, adjust=False).mean()

    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(p["rsi_p"]).mean()
    loss  = (-delta.clip(upper=0)).rolling(p["rsi_p"]).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))

    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()

    plus_dm  = (h.diff()).clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    mask = plus_dm < minus_dm; plus_dm[mask] = 0
    mask2 = minus_dm <= plus_dm; minus_dm[mask2] = 0
    tr_s = tr.rolling(14).mean()
    plus_di  = 100 * plus_dm.rolling(14).mean() / tr_s
    minus_di = 100 * minus_dm.rolling(14).mean() / tr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
    df["ADX"] = dx.rolling(14).mean()

    return df.dropna()


# ── SCORING (aligné sur la partie commune de compute_signal_score de bot.py) ──
def score_bar(row, prev_row, p):
    c      = float(row["Close"])
    rsi    = float(row["RSI"])
    adx    = float(row["ADX"])
    macd_h = float(row["MACD_hist"])
    ema_s  = float(row["EMA_S"])
    ema_m  = float(row["EMA_M"])
    ema200 = float(row["EMA200"])

    if adx < p["adx_filter"]:
        return 0, "NONE"

    s_buy = s_sell = 0

    if c > ema200: s_buy += 1
    else:          s_sell += 1

    if float(prev_row["EMA_S"]) <= float(prev_row["EMA_M"]) and ema_s > ema_m: s_buy += 2
    elif float(prev_row["EMA_S"]) >= float(prev_row["EMA_M"]) and ema_s < ema_m: s_sell += 2
    elif ema_s > ema_m: s_buy += 1
    else: s_sell += 1

    if macd_h > 0: s_buy += 1
    else:          s_sell += 1

    if p["rsi_buy_lo"] <= rsi <= p["rsi_buy_hi"]:  s_buy += 1
    elif p["rsi_sell_lo"] <= rsi < p["rsi_buy_lo"]: s_sell += 1
    elif rsi > p["rsi_buy_hi"]:  s_sell += 1
    elif rsi < p["rsi_sell_lo"]: s_buy += 1

    if adx > p["adx_strong"]:
        if ema_s > ema_m: s_buy += 1
        else:              s_sell += 1

    if s_buy >= p["threshold"] and s_buy > s_sell and c > ema200:
        return min(s_buy, 7), "BUY"
    if s_sell >= p["threshold"] and s_sell > s_buy and c < ema200:
        return min(s_sell, 7), "SELL"
    return 0, "NONE"


# ── BACKTEST ──────────────────────────────────────────────────────────────────
def run_backtest(df, p, capital=10000.0):
    """Retourne dict de métriques. Simule SL/TP sur High/Low intrabar + timeout."""
    rows     = df.reset_index()
    trades   = []
    equity   = [capital]
    position = None

    for i in range(1, len(rows)):
        row   = rows.iloc[i]
        prev  = rows.iloc[i - 1]
        price = float(row["Close"])
        high  = float(row["High"])
        low   = float(row["Low"])
        atr   = float(row["ATR"])

        if position:
            position["bars"] += 1
            if position["dir"] == "BUY":
                hit_sl = low  <= position["sl"]
                hit_tp = high >= position["tp"]
            else:
                hit_sl = high >= position["sl"]
                hit_tp = low  <= position["tp"]
            timeout = position["bars"] >= TIMEOUT_BARS

            if hit_sl or hit_tp or timeout:
                # SL prioritaire si les deux touchés dans la même bougie (conservateur)
                if hit_sl:   exit_price = position["sl"]
                elif hit_tp: exit_price = position["tp"]
                else:        exit_price = price
                if position["dir"] == "BUY":
                    pnl = (exit_price - position["entry"]) * position["qty"]
                else:
                    pnl = (position["entry"] - exit_price) * position["qty"]
                capital += pnl
                trades.append(pnl)
                equity.append(capital)
                position = None
            continue

        score, direction = score_bar(row, prev, p)
        if direction != "NONE" and atr > 0:
            risk_amt = capital * RISK
            sl_dist  = p["sl_mult"] * atr
            qty      = risk_amt / sl_dist if sl_dist > 0 else 0
            if qty <= 0:
                continue
            if direction == "BUY":
                sl = price - sl_dist
                tp = price + p["tp_mult"] * atr
            else:
                sl = price + sl_dist
                tp = price - p["tp_mult"] * atr
            position = {"dir": direction, "entry": price, "sl": sl, "tp": tp,
                        "qty": qty, "bars": 0}

    if len(trades) < 5:
        return {"score": -999.0, "n": len(trades), "pnl": 0, "pf": 0, "maxdd": 0, "winrate": 0}

    arr    = np.array(trades)
    eq     = np.array(equity)
    peak   = np.maximum.accumulate(eq)
    maxdd  = float(((peak - eq) / peak).max())
    wins   = arr[arr > 0]
    losses = arr[arr <= 0]
    pf     = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else 99.0
    sharpe_t = float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0

    # Score composite : régularité (sharpe/trade) pénalisée par le drawdown
    score = sharpe_t * np.sqrt(len(arr)) - 2.0 * maxdd

    return {
        "score":   round(float(score), 4),
        "n":       len(trades),
        "pnl":     round(float(arr.sum()), 2),
        "pf":      round(pf, 3),
        "maxdd":   round(maxdd, 4),
        "winrate": round(float(len(wins) / len(arr)), 3),
    }


# ── OPTUNA ────────────────────────────────────────────────────────────────────
def objective(trial, df_train):
    p = {
        "ema_s":       trial.suggest_int("ema_s", 5, 15),
        "ema_m":       trial.suggest_int("ema_m", 16, 30),
        "rsi_p":       trial.suggest_int("rsi_p", 9, 21),
        "rsi_buy_lo":  trial.suggest_float("rsi_buy_lo", 40, 55),
        "rsi_buy_hi":  trial.suggest_float("rsi_buy_hi", 65, 80),
        "rsi_sell_lo": trial.suggest_float("rsi_sell_lo", 20, 35),
        "adx_filter":  trial.suggest_float("adx_filter", 18, 30),
        "adx_strong":  trial.suggest_float("adx_strong", 25, 40),
        "threshold":   trial.suggest_int("threshold", 4, 6),
        "sl_mult":     trial.suggest_float("sl_mult", 1.0, 2.5),
        "tp_mult":     trial.suggest_float("tp_mult", 2.0, 4.0),
    }
    try:
        df_ind = compute_indicators(df_train, p)
        return run_backtest(df_ind, p)["score"]
    except Exception:
        return -999.0


# ── PARAMÈTRES ACTUELS (baseline — alignés sur bot.py) ────────────────────────
BASELINE = {
    "ema_s": 9, "ema_m": 21, "rsi_p": 14,
    "rsi_buy_lo": 45, "rsi_buy_hi": 75,
    "rsi_sell_lo": 25,
    "adx_filter": 20.7, "adx_strong": 29.3,
    "threshold": 5,
    "sl_mult": 1.5, "tp_mult": 3.0,
}


def report(label, m):
    print(f"  {label:22s} score={m['score']:8.3f} | trades={m['n']:3d} | "
          f"P&L={m['pnl']:9.2f}$ | PF={m['pf']:.2f} | maxDD={m['maxdd']:.1%} | WR={m['winrate']:.0%}")


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    df_raw = fetch_data()

    # Walk-forward : optimise sur 70%, valide sur les 30% jamais vus
    split    = int(len(df_raw) * 0.7)
    df_train = df_raw.iloc[:split]
    df_test  = df_raw.iloc[split:]
    print(f"Train: {len(df_train)} bougies | Test: {len(df_test)} bougies\n")

    print("Baseline (paramètres actuels) :")
    base_train = run_backtest(compute_indicators(df_train, BASELINE), BASELINE)
    base_test  = run_backtest(compute_indicators(df_test,  BASELINE), BASELINE)
    report("train", base_train)
    report("test",  base_test)

    print("\nOptimisation Optuna (300 essais, sur train uniquement)...")
    study = optuna.create_study(direction="maximize")
    study.optimize(lambda t: objective(t, df_train), n_trials=300, n_jobs=-1, show_progress_bar=True)

    best = {**BASELINE, **study.best_params}
    print(f"\n{'='*60}\nRÉSULTATS\n{'='*60}")
    opt_train = run_backtest(compute_indicators(df_train, best), best)
    opt_test  = run_backtest(compute_indicators(df_test,  best), best)
    report("optimisé (train)", opt_train)
    report("optimisé (test)",  opt_test)

    print("\nMeilleurs paramètres :")
    for k, v in study.best_params.items():
        print(f"  {k:14s} : {BASELINE.get(k)} → {v:.3f}" if isinstance(v, float) else f"  {k:14s} : {BASELINE.get(k)} → {v}")

    # Critère de validation : les paramètres doivent AUSSI battre la baseline sur test
    validated = opt_test["score"] > base_test["score"] and opt_test["score"] > 0
    if not validated:
        print("\n❌ Non validé sur les données test (overfitting probable) — rien n'est appliqué.")
        raise SystemExit(0)

    print("\n✅ Validé sur les données test.")
    print("Écrire params_optuna.json (lu par bot.py au démarrage) ? (o/n) : ", end="")
    if input().strip().lower() == "o":
        # Seuls les paramètres que bot.py sait appliquer dynamiquement (bornés dans adaptive_params)
        out = {
            "threshold": int(best["threshold"]),
            "sl_mult":   round(float(best["sl_mult"]), 3),
            "tp_mult":   round(float(best["tp_mult"]), 3),
        }
        with open("params_optuna.json", "w") as f:
            json.dump(out, f, indent=2)
        print(f"✅ params_optuna.json écrit : {out}")
        print("Lance : git add params_optuna.json && git commit -m 'Params Optuna validés walk-forward' && git push")
    else:
        print("Aucune modification appliquée.")
