# #1176 — paquet de revue du delta d'intégration (gel ce86f658)

Lot : #1176 « Comptes : plafond de rythme réglable par compte (ne plus perdre la
fin de fenêtre des forfaits) » — classe argent, sensible. Auteur : ameesh-dev3
(harnais DeepSeek).

- Tête gelée : `ce86f6581a7608cdc549d70cf86095b38eba95ac`
- Parents : `f9d7e43f` (lot, correctifs B1/B2 de la revue Codex) + `68a18f75`
  (main, #707/#898).
- PR #905 : reste sur `f9d7e43f` tant que la tête n'est pas poussée — hold CI
  avant l'activation de #1211 (bureau 29538 du 03:20:29Z, arbitrage
  30827/30828 du 04:59:41Z).

## Ce qui est déjà acquis

- R1 delta Codex (cx1) : **GO** le 14/10 04:18Z sur `f9d7e43f`, B1/B2 fermés,
  aucun nouveau B —
  https://github.com/manaty/ameesh/pull/905#pullrequestreview-5513007728

## Ce qui reste à relire (avis indépendant final, famille autre que l'auteur)

Le **delta d'intégration de main** : le commit de fusion `ce86f658`, et
uniquement les changements propres à la fusion (`git show --cc ce86f658`,
reproduit dans `delta-resolution.patch`).

- `git merge-tree --write-tree f9d7e43f 68a18f75` rend rc=1 : un seul fichier
  en conflit, `src/ameesh/accounts.py` (arbre automatique `a1d371ef`).
- Le conflit est un bloc de commentaire de `_release`/`until` : #898 (main) et
  #1176 (lot) ont réécrit les mêmes lignes. La résolution garde les deux
  formulations ; **aucun changement de code**. Blobs de `accounts.py` :
  `f9d7e43f` 5c27836b, `68a18f75` 6e459849, fusion 573649b8.
- `cost.py` s'auto-fusionne (aucune résolution manuelle).

## Preuves sur cette tête exacte (ce86f658)

Tests ciblés rejoués le 14/10 10:24Z (worktree dev3-1176, base Postgres locale
55432, harnais du dépôt) : **203 OK en 194 s** — modules `test_1176_pace_ceiling`,
`test_1075_pace_multiplier`, `test_898_pause_rythme`, `test_453_pause_pression`,
`test_l226b_auth_failure`, `test_cost`, `test_l30_accounts`. Aucune suite
complète en local (canon §3) ; la CI exacte reste à lancer à l'activation.

## Récupération

    git fetch origin 'refs/atelier/1176/*:refs/atelier/1176/*'
    git log -1 refs/atelier/1176/tete-ce86f658
    git show --cc refs/atelier/1176/tete-ce86f658

Le paquet (ce fichier, le patch de résolution, `SHA256SUMS`) est dans
`refs/atelier/1176/delta-revue`.

## Portée

Lecture seule : aucun run CI créé, aucune branche ni PR modifiée, aucun gel
déplacé, aucun avis dépensé. Le hold avant l'activation de #1211 reste tenu ;
le geste de poussée/PR/CI reste au plan convenu (au réveil de
`ameesh-dev3-wake-923-1211`).
