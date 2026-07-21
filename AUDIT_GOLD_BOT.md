# Audit Gold Bot — 21 juillet 2026

> **MàJ 21/07 — TOUTES les corrections appliquées** (Critiques 1-2, Majeurs 3-5, Moyens 6-12) :
> fermeture réelle MT5 + `/modify` trailing + P&L réel, threshold adaptatif branché, TP ramené à ~2×SL avec timeout 6h, risque par défaut 1% (4 trades × 1% < daily loss 4.5%), fallback futures GC=F supprimé, caches `.total_seconds()`, reset journalier dans la boucle, `loss_streak` persisté, capital synchronisé sur la balance MT5 réelle, ticket MT5 sauvé dans Supabase (positions restaurables/fermables après restart), `backtest.py` réécrit (15m, timeout simulé, walk-forward train/test, sortie `params_optuna.json` lue par le bot — plus de patch regex).
>
> **Pour activer :** redémarre le bridge (start_bridge.bat), redéploie le bot (git add/commit/push). Optionnel : ajouter une colonne `mt5_ticket` (text) à la table `trade_history` dans Supabase — le code marche sans, mais avec, le bot peut fermer les positions réelles après un restart Railway.
> Les nouveaux paramètres SL/TP (1.5/3.0) sont des valeurs saines par défaut — lance `python backtest.py` pour les valider/affiner en walk-forward avant le compte réel.

Revue complète de `bot.py`, `mt5_bridge.py` et `backtest.py`. Classement par gravité.

---

## 🔴 CRITIQUE 1 — Le bot ne ferme JAMAIS les positions réelles dans MT5

`close_mt5_order()` existe dans le code mais **n'est appelé nulle part**. Conséquences :

- **Timeout 4h** (`MAX_POSITION_HOURS`) : le trade est fermé *localement* dans `check_exits()`, mais la position réelle reste ouverte dans MT5 jusqu'à son SL/TP d'origine.
- **Trailing stop** : le SL trailé n'est mis à jour que dans l'état local. Le bridge n'a même pas d'endpoint `/modify`. Si le prix touche le SL trailé, le bot enregistre une sortie gagnante… mais la position MT5 court toujours avec le SL initial.
- **Positions "stale" au redémarrage** : fermées dans Supabase avec pnl=0, jamais dans MT5 → positions orphelines réelles.

Ton capital suivi par le bot et ton compte RaiseMyFund divergent à chaque timeout ou trailing. Sur un compte prop avec limite de drawdown 10%, une position orpheline peut faire échouer le challenge.

**Correctif :**
1. Dans `check_exits()`, quand `timeout_hit` ou SL trailé touché → appeler `close_mt5_order(pos["mt5_ticket"])` et n'enregistrer la fermeture que si le bridge confirme (utiliser le profit réel renvoyé par MT5).
2. Ajouter un endpoint `/modify` au bridge (`mt5.TRADE_ACTION_SLTP`) et pousser chaque nouveau SL trailé vers MT5.
3. Au redémarrage, fermer les positions stale via le bridge avant de les marquer fermées.

---

## 🔴 CRITIQUE 2 — Le P&L local ne correspond pas au volume réellement exécuté

Le bot calcule `qty = capital × risk / sl_dist` (en onces), puis le bridge convertit en lots avec **deux distorsions silencieuses** :

- `lots = max(vol_min, …)` : si la qty calculée est trop petite, le bridge **gonfle** la position au minimum 0.01 lot (1 oz d'or) → risque réel supérieur au 1% prévu.
- `lots = min(lots, 0.03)` : si la qty est grande (capital 10k, risque 1–2%, qty ≈ 7–20 oz → 0.07–0.2 lots), le cap **réduit** la position à 0.03 lots.

Or le P&L local est calculé avec la qty théorique : `pnl = (exit − entry) × qty`. Avec un cap à 0.03 lots (3 oz) et une qty théorique de 10 oz, **le bot enregistre un P&L ~3× supérieur au réel**. Capital, drawdown, daily loss, adaptive params, ML : tout est faussé.

**Correctif :** le bridge renvoie déjà `volume` dans la réponse `/order` → stocker `pos["real_lots"] = volume` et calculer le P&L avec `real_lots × 100` oz (XAUUSD). Mieux : à la fermeture, toujours récupérer le profit réel via `/positions_status` (deals MT5, incluant swap + commission) au lieu de le calculer.

---

## 🟠 MAJEUR 3 — Le seuil adaptatif n'est jamais appliqué au signal

`adaptive_params()` retourne un `threshold` (ajusté par le mode et par Gemini), il est loggé (`"Mode adaptatif : … seuil X/7"`)… mais `compute_signal_score(df)` utilise un **seuil codé en dur `threshold = 5`** et ne reçoit jamais le paramètre. Tout le système d'adaptation du seuil (et les ajustements Gemini sur `threshold`) est sans effet.

**Correctif :** `compute_signal_score(df, threshold=params["threshold"])`.

---

## 🟠 MAJEUR 4 — Incohérence TP vs timeout 4h

Paramètres par défaut : `tp_mult = 5.343 × ATR` (voire 7.2 en mode récupération) avec un timeout de 4h. Sur du 15m, un TP à 5+ ATR est rarement atteint en 4h → la majorité des sorties se font par timeout au prix du moment, pas au TP. Ton ratio risque/rendement théorique (1 : 2+) n'existe pas en pratique.

**Correctif :** soit réduire `tp_mult` (~2.5–3× ATR, cohérent avec du scalping 4h), soit allonger le timeout, soit sortie partielle à 2×ATR + trailing sur le reste. À valider en backtest.

---

## 🟠 MAJEUR 5 — Le backtest ne teste pas la stratégie réelle

`backtest.py` optimise une version simplifiée (5 critères sur 11, sans Stoch/Williams/OTE/FVG/OB/IFVG, sans filtres DXY/session/1H/4H/TEMA/ML) sur du **GC=F 1h** (futures), alors que le bot vit sur du **XAU/USD spot 15m** avec tous ces filtres. Les paramètres "optimisés" ne sont pas transférables.

En plus, l'auto-application par regex est cassée : elle cherche `adx < 22` et `"tp_mult": 6.0` qui n'existent plus dans `bot.py` (valeurs actuelles : 20.7, 29.3, 5.343 — traces d'anciens patchs partiels). Résultat : application silencieusement partielle → dérive des paramètres.

**Correctif :** refactorer pour que `bot.py` et `backtest.py` partagent les mêmes fonctions (`compute_indicators`, `compute_signal_score`), backtester sur données 15m spot, et remplacer le patch regex par un fichier de config (`params.json`) lu par le bot. Ajouter du walk-forward (optimiser sur 4 mois, valider sur 2) pour éviter l'overfitting Optuna.

---

## 🟡 MOYEN

6. **Sources de prix mélangées** — Twelve Data (spot) → OANDA (spot) → yfinance `GC=F` (**futures**). Le fallback futures a un basis de plusieurs dollars vs spot : SL/TP calculés dessus sont décalés par rapport aux quotes RaiseGlobal exécutées. Supprimer le fallback GC=F pour le live, ou garder uniquement spot.

7. **Exits vérifiés toutes les 5 min sur le dernier close** — un pic intrabar qui touche le SL local n'est pas vu (MT5 le voit, lui → divergence, cf. Critique 1). Une fois le P&L réel MT5 utilisé partout, ce point devient bénin.

8. **`get_dxy_direction` / caches : `(now - fetched_at).seconds`** — `.seconds` ≠ `.total_seconds()` : après 24h, le cache repart à zéro artificiellement. Bug bénin ici, mais à corriger (`.total_seconds()`).

9. **Reset journalier dépendant du morning report** — `daily_pnl`/`daily_trades` ne se réinitialisent que dans `morning_report()`. Si le scheduler saute (crash, redeploy à la mauvaise heure), les limites journalières restent bloquées sur la veille. Déplacer le check de reset au début de `trading_loop`.

10. **`loss_streak` remis à 0 au restart** (`load_data_from_supabase`) — un redeploy Railway annule la pause après 3 pertes. Intentionnel d'après le commentaire, mais ça affaiblit la protection : persister le streak avec un timestamp plutôt.

11. **Capital sync avant trade** (`_sb_cap < data["capital"] * 0.9`) — sync uniquement à la baisse et seulement si écart >10% : asymétrique et arbitraire. Avec le correctif Critique 2, s'appuyer plutôt sur `fetch_mt5_account()` (balance réelle) comme source de vérité du capital.

12. **Commentaires/incohérences horaires** — `is_blackout_session()` dit "21h-minuit Paris" mais le log de la boucle dit "21h-00h UTC". Vérifier que les kill zones (2–9h, 9–12h, 15–18h Paris) correspondent bien aux sessions visées (NY PM 18–21h Paris est exclue — voulu ?).

13. **ADX à 29.3 / 20.7, RSI strong_uptrend dupliqué** — valeurs bizarres issues d'optimisations passées appliquées par regex. À re-valider proprement une fois le backtest refactoré (point 5).

---

## 🟢 CE QUI EST BIEN

- Pas de fallback OANDA fantôme quand le bridge MT5 est down (trade ignoré) — bonne décision.
- Filtres de confluence sérieux : EMA200 obligatoire, ADX, multi-timeframe 1H+4H, DXY, TEMA, macro blackout ForexFactory, kill zones, blacklist après 3 pertes.
- Gestion prop firm réfléchie : daily loss/gain cap, drawdown pause 48h, challenge pause, max 4 trades/jour, cap 0.03 lots.
- Sync des fermetures manuelles MT5 avec profit réel (deals + swap + commission) — c'est exactement ce qu'il faut généraliser à TOUTES les sorties.
- Hystérésis sur les modes adaptatifs, bornes sur les overrides Gemini, sanity checks prix.

---

## Plan d'action recommandé (dans l'ordre)

1. **Fermer/modifier les positions réelles** : appeler `close_mt5_order` sur timeout + ajouter `/modify` pour le trailing (Critique 1).
2. **P&L réel partout** : profit MT5 via deals comme source unique de vérité (Critique 2).
3. **Brancher le threshold adaptatif** dans `compute_signal_score` (Majeur 3).
4. **Cohérence TP/timeout** : à trancher par backtest (Majeur 4).
5. **Refactorer le backtest** sur la vraie stratégie 15m spot + config partagée + walk-forward (Majeur 5).
6. Puis les points moyens (8 → 13).

Les points 1–3 sont des corrections de bugs sans ambiguïté. Les points 4–5 changent le comportement de la stratégie → à valider en démo avant le compte réel.

*Rappel : je ne suis pas conseiller financier — cet audit porte sur le code et la cohérence technique, pas une garantie de rentabilité.*
