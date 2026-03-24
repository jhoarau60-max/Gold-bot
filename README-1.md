# Gold Bot Pro 🥇

Bot de trading automatique sur l'or (XAU/USD) — hébergé sur Railway.

## Variables d'environnement (à configurer sur Railway)

| Variable | Description | Défaut |
|---|---|---|
| GOLD_API_KEY | Votre clé API goldapi.io | obligatoire |
| INITIAL_CAPITAL | Capital de départ en $ | 10000 |
| SL_PCT | Stop-Loss en % | 1.5 |
| TP_PCT | Take-Profit en % | 3.0 |
| POS_SIZE | Taille position (0.10 = 10%) | 0.10 |
| MAX_DAILY_LOSS | Perte max journalière en % | 2.0 |
| REFRESH_SECONDS | Intervalle de mise à jour | 30 |

## Stratégie
Croisement de Moyennes Mobiles MA5 / MA20 sur XAU/USD.
