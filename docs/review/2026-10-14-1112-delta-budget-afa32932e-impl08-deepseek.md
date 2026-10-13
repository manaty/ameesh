# #1112 — delta budget afa32932e : avis indépendant DeepSeek (impl-08)

Tête : `afa32932e72c459bf1d23314912647b23b306b73` (`origin/integration/bureau-1112-ci-budgets`), lue telle quelle.
Blobs vérifiés par `git rev-parse` : `src/ameesh/accounts.py` `9ae896b3b79263466ef193c2877563876793e2f4`,
`src/ameesh/cost.py` `09c528c5cb570f202cb4aaf044ab0302000781c1`, `tests/test_l242b2_budget.py` `1161d799db6088eb59b99a47e3d55a145effbddf`,
`tests/test_1075_pace_multiplier.py` `9fe762c1ec665e455cc543ca2d6770c5ac55fadd`, `.github/workflows/tests.yml` `fbf1e8d0cd7dec6d945c19e23a92b35c88d3dd35`.
Auteur de la résolution : bureau (Codex). Avis indépendant : impl-08 (DeepSeek). Aucune suite rejouée (canon §3).

## Verdict

**GO contenu : 0 B / 0 P2 / 1 N P3.** Le couplage #777 x #1075 est résolu sans perte et l'invariant de libération est exact.

## Vérifications

1. **Aucun hunk perdu** : diff de l'arbre fusionné contre chaque tête d'entrée (#777 `ef1b22b9`, #1075 `2fb4913a`) sur `accounts.py` et `cost.py` ; les deux apports sont présents. Dans `limits`, la ligne de #1075 (`max(0, pace_cap - cut)`) est remplacée par `held_cap(now, cut, multiplier)` (#777), et `_release`/`free_at` reçoivent le multiplicateur du profil.
2. **Invariant `free_at`** (grille m ∈ {0,5 ; 1 ; 2} x réserve ∈ {0 ; 0,1} x used 0..1 x écoulé 0..1 au pas de 0,01, fenêtre 7 j) : l'heure rendue est le plus petit t où `used_at(t) < held_cap(t)` ; formule exacte `e* = min(1, used/m)` (avec réserve) ou `e* = min(1, max(0, (used - marge)/m))` (sans) ; 0 écart > 1 s ; tenue à t-1 s, libre à t+1 s ; jamais hors `[now, resets_at]`.
3. **Cohérence `_release` / `limits`** (mêmes grilles, fenêtre 5 h, `short_window_cap` absent et 0,75, plafonds fixes) : l'heure annoncée est tenue à t-1 s et libérée à t+1 s ; un plafond fixe ou un plafond court bloquant annoncent la remise à zéro.
4. **Plancher (#1028 N2)** : sur 16 462 cas bloqués à réserve 0,1 de la grille, `max(share, PACE_MARGIN)` n'est jamais actif (une fenêtre bloquée a `used >= held_cap >= marge`) ; placé avant la division, il ne crée aucun retard m=2. Sans effet comportemental, confirmé ; le lot #1028 (P3) reste le suivi.
5. **Chemins de rythme** : `evaluate` / `limits` / `_release` / `rank` / `explain` passent le multiplicateur (directement ou via `result.limits`) ; `pace_exceeded`, `over(pace=True)` et le compte par défaut restent nominaux et ne sont appelés que sans comptes déclarés (runner, mesh_cli, account_turn). Les témoins de #1075 (`test_reserve_floor_survives_multiplier`, bisection `exceeded(free ± 1)`) et les 4 ajouts de `test_l242b2_budget.py` couvrent le couplage ; lus, non rejoués.

## N P3 (non bloquant, lot de suite possible)

`CostBook.report`, `progress` (plans) et `sous_utilisation` affichent ou comparent encore `pace_cap(now)` nominal pour un compte à multiplicateur ×2 ou ×0,5 ; aucun effet sur une pause ni sur un choix de compte (les décisions passent par `accounts`). Observabilité seulement.

## Limites

Avis limité au delta budget sur les blobs ci-dessus. La QA complète §12 (4 psycopg / 6 psql) est lancée par le bureau sur la même tête ; tout déplacement de la tête invalide cet avis.
