"""
Test script — Gold Bot nouvelles features
1. Graphique avec lignes de tendance automatiques → PNG local
2. Wiki push → wiki_knowledge Supabase
"""

import os, sys, asyncio, json
from datetime import datetime

# ── ENV VARS — copie depuis Railway si pas déjà setées ─────────────────────
required = ["GEMINI_API_KEY", "WIKI_SUPABASE_URL", "WIKI_SUPABASE_KEY",
            "TELEGRAM_TOKEN", "JOHN_ID"]
missing = [k for k in required if not os.environ.get(k)]
if missing:
    print(f"❌ Variables manquantes : {missing}")
    print("Lance d'abord dans PowerShell :")
    for k in missing:
        print(f'  $env:{k} = "valeur"')
    sys.exit(1)

import io
import pytz
import pandas as pd
import numpy as np
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

TZ = pytz.timezone("Europe/Brussels")

# ── 1. FETCH DONNÉES ────────────────────────────────────────────────────────
print("\n[1/4] Fetch XAUUSD...")
ticker = yf.Ticker("GC=F")
df = ticker.history(period="2d", interval="15m", auto_adjust=True)
if df is None or len(df) < 30:
    print("❌ Fetch échoué")
    sys.exit(1)
print(f"    ✅ {len(df)} bougies récupérées")

# ── 2. INDICATEURS ──────────────────────────────────────────────────────────
print("[2/4] Calcul indicateurs + détection pivots...")
c = df["Close"].squeeze()
h = df["High"].squeeze()
l = df["Low"].squeeze()

df["EMA9"]  = c.ewm(span=9,  adjust=False).mean()
df["EMA21"] = c.ewm(span=21, adjust=False).mean()
df["EMA50"] = c.ewm(span=50, adjust=False).mean()

ema12 = c.ewm(span=12, adjust=False).mean()
ema26 = c.ewm(span=26, adjust=False).mean()
df["MACD"]        = ema12 - ema26
df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
df["MACD_hist"]   = df["MACD"] - df["MACD_signal"]

delta = c.diff()
gain  = delta.clip(lower=0)
loss  = -delta.clip(upper=0)
df["RSI"] = 100 - 100 / (1 + gain.rolling(14).mean() / loss.rolling(14).mean())

bb_mid = c.rolling(20).mean()
bb_std = c.rolling(20).std()
df["BB_upper"] = bb_mid + 2 * bb_std
df["BB_lower"] = bb_mid - 2 * bb_std


def detect_pivots(df, n=5):
    h_vals = df["High"].squeeze().values
    l_vals = df["Low"].squeeze().values
    idx = df.index
    pivot_highs, pivot_lows = [], []
    for i in range(n, len(df) - n):
        if h_vals[i] == max(h_vals[i - n:i + n + 1]):
            pivot_highs.append((idx[i], h_vals[i]))
        if l_vals[i] == min(l_vals[i - n:i + n + 1]):
            pivot_lows.append((idx[i], l_vals[i]))
    return pivot_highs, pivot_lows


def draw_trendlines(ax, df, pivot_highs, pivot_lows):
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return None
    x_all = df.index

    def ts_float(ts):
        return ts.timestamp() if hasattr(ts, "timestamp") else float(ts.value)

    ph1, ph2 = pivot_highs[-2], pivot_highs[-1]
    x1h, y1h = ts_float(ph1[0]), ph1[1]
    x2h, y2h = ts_float(ph2[0]), ph2[1]
    slope_h  = (y2h - y1h) / (x2h - x1h) if x2h != x1h else 0

    pl1, pl2 = pivot_lows[-2], pivot_lows[-1]
    x1l, y1l = ts_float(pl1[0]), pl1[1]
    x2l, y2l = ts_float(pl2[0]), pl2[1]
    slope_l  = (y2l - y1l) / (x2l - x1l) if x2l != x1l else 0

    x_start = ts_float(x_all[0])
    x_end   = ts_float(x_all[-1])

    def project(x_ref, y_ref, slope, x):
        return y_ref + slope * (x - x_ref)

    y_h0 = project(x2h, y2h, slope_h, x_start)
    y_h1 = project(x2h, y2h, slope_h, x_end)
    y_l0 = project(x2l, y2l, slope_l, x_start)
    y_l1 = project(x2l, y2l, slope_l, x_end)

    ax.plot([x_all[0], x_all[-1]], [y_h0, y_h1],
            color="#ff4500", lw=1.5, ls="--", alpha=0.85, label="Résistance")
    ax.plot([x_all[0], x_all[-1]], [y_l0, y_l1],
            color="#00ff7f", lw=1.5, ls="--", alpha=0.85, label="Support")
    for ts, price in pivot_highs[-3:]:
        ax.scatter(ts, price, color="#ff4500", marker="v", s=60, zorder=6, alpha=0.7)
    for ts, price in pivot_lows[-3:]:
        ax.scatter(ts, price, color="#00ff7f", marker="^", s=60, zorder=6, alpha=0.7)
    ax.fill_between([x_all[0], x_all[-1]], [y_h0, y_h1], [y_l0, y_l1],
                    alpha=0.05, color="#ffffff")

    eps = abs(y2h - y1h) * 0.001
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
    else:
        d = "Haussier" if slope_h > 0 else "Baissier" if slope_h < 0 else "Neutre"
        return f"Canal {d} ↔"


pivot_highs, pivot_lows = detect_pivots(df, n=5)
print(f"    ✅ {len(pivot_highs)} pivots hauts, {len(pivot_lows)} pivots bas")

# ── 3. GRAPHIQUE ────────────────────────────────────────────────────────────
print("[3/4] Génération graphique PNG...")

fig = plt.figure(figsize=(14, 8), facecolor="#0a1428")
gs  = fig.add_gridspec(3, 1, hspace=0.08, height_ratios=[3, 1, 1])
ax1 = fig.add_subplot(gs[0])
ax2 = fig.add_subplot(gs[1], sharex=ax1)
ax3 = fig.add_subplot(gs[2], sharex=ax1)

for ax in (ax1, ax2, ax3):
    ax.set_facecolor("#0d1f3c")
    ax.tick_params(colors="#aaaaaa", labelsize=8)
    for sp in ax.spines.values():
        sp.set_color("#1e3a5f")

ax1.fill_between(df.index, df["BB_upper"].squeeze(), df["BB_lower"].squeeze(),
                 alpha=0.07, color="#4a90e2")
ax1.plot(df.index, df["BB_upper"].squeeze(), color="#4a90e2", lw=0.8, ls="--")
ax1.plot(df.index, df["BB_lower"].squeeze(), color="#4a90e2", lw=0.8, ls="--")
ax1.plot(df.index, c,                  color="#FFD700", lw=2,   label="XAU/USD")
ax1.plot(df.index, df["EMA9"].squeeze(),  color="#00bfff", lw=1.2, ls="--", label="EMA9")
ax1.plot(df.index, df["EMA21"].squeeze(), color="#ff6347", lw=1.2, ls="--", label="EMA21")
ax1.plot(df.index, df["EMA50"].squeeze(), color="#9b59b6", lw=1.0, ls="-.",  label="EMA50")

pattern = draw_trendlines(ax1, df, pivot_highs, pivot_lows)
pat_str = f" — {pattern}" if pattern else ""

ax1.set_title(f"TEST — XAU/USD — {datetime.now(TZ).strftime('%d/%m/%Y')}{pat_str}",
              color="white", fontsize=13, fontweight="bold")
ax1.legend(facecolor="#0d1f3c", labelcolor="white", fontsize=8, loc="upper left")

hist = df["MACD_hist"].squeeze()
colors_hist = ["#00ff7f" if v >= 0 else "#ff4500" for v in hist]
ax2.bar(df.index, hist, color=colors_hist, width=0.0005, alpha=0.8)
ax2.plot(df.index, df["MACD"].squeeze(),       color="#00bfff", lw=1.2, label="MACD")
ax2.plot(df.index, df["MACD_signal"].squeeze(), color="#ff6347", lw=1.0, label="Signal")
ax2.axhline(0, color="white", lw=0.5, alpha=0.4)
ax2.set_ylabel("MACD", color="#aaaaaa", fontsize=8)
ax2.legend(facecolor="#0d1f3c", labelcolor="white", fontsize=7)

rsi = df["RSI"].squeeze()
ax3.plot(df.index, rsi, color="#a78bfa", lw=1.5)
ax3.axhline(70, color="#ff6347", ls="--", alpha=0.7, lw=1)
ax3.axhline(30, color="#00ff7f", ls="--", alpha=0.7, lw=1)
ax3.set_ylim(0, 100)
ax3.set_ylabel("RSI", color="#aaaaaa", fontsize=8)

plt.setp(ax1.get_xticklabels(), visible=False)
plt.setp(ax2.get_xticklabels(), visible=False)

out_path = "C:/Users/jhoar/Desktop/Gold-bot/test_chart.png"
plt.savefig(out_path, format="png", dpi=150, bbox_inches="tight",
            facecolor=fig.get_facecolor())
plt.close()
print(f"    ✅ Graphique sauvé : {out_path}")
print(f"    Pattern détecté : {pattern or 'Aucun'}")

# ── 4. WIKI PUSH ────────────────────────────────────────────────────────────
print("[4/4] Test wiki push Supabase...")
try:
    from supabase import create_client
    wiki_client = create_client(
        os.environ["WIKI_SUPABASE_URL"],
        os.environ["WIKI_SUPABASE_KEY"]
    )
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    slug  = f"test-goldbot-{today}"
    res = wiki_client.table("wiki_knowledge").upsert({
        "slug":         slug,
        "title":        f"Test Gold Bot {today}",
        "type":         "journal",
        "summary":      f"Test push depuis script local — pattern détecté : {pattern or 'Aucun'}",
        "full_content": f"Test OK. Graphique généré. Pattern : {pattern or 'Aucun'}.",
        "created_at":   datetime.now(TZ).isoformat(),
    }, on_conflict="slug").execute()
    print(f"    ✅ Wiki push OK — slug: {slug}")
except Exception as e:
    print(f"    ❌ Wiki push échoué : {e}")

print("\n✅ TESTS TERMINÉS")
print(f"   Ouvre le PNG : {out_path}")
print("   Vérifie dans Supabase : table wiki_knowledge")
