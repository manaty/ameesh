# Paquet integration/paquet-h-20261019-0900 — composition (§12)

Base : `main` **`2be10c08d75c`** (paquet h-0700, PR #2019, fusionné en
`2be10c08d` le 19/10 06:08:45 UTC). Branche
`integration/paquet-h-20261019-0900`, cible `main`. Une seule suite complète
est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis — `main` a bougé à `2be10c08d`
(h-0700 fusionné) ; liste calculée `paquet-candidats.py` (32 candidats, 10
prêts, 0 erreur). Entrées : message `mesh-design` #113058 (composer le paquet
h-0900, #2644 en tête) et sa correction #113060 (#1484 prêt, gel
`6ebb18539e14`, deux avis sur cette tête) ; porte des avis de #2644 acquise
sur `b40f10dabf76` (dev1 04:38:43Z, cx1 04:38:08Z).

## Admissions (10 unités, 10 lots portés)

- #2644 — prévalidation #1770 appelée par l'outillage (en tête)
- #2620 — réservation de migration à numéro exact, scan à portée de la cible
- #2658 — garde `landed` étendue à `released` (migration 0467)
- #1484 — L232b : sept réserves mineures du complément Claude
- #2603 — PR empilée : base déjà fusionnée refusée, PR hors dépôt déclarée
- #2617 — bilan 24 h : plus de double compte des entrées `/tmp` non attribuées
- #2664 — familles de modèle lues au canon compilé (review_lot, porte, provenance)
- #2654 — `plan_git.branch_probe` lit la forge avant de conclure
- #2656 — alerte « créneau manqué » : un créneau déjà servi n'alerte plus
- #2572 — `ameesh accounts use` refuse un compte refusé en strict

## Composition (10 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2644 | `b40f10dabf76` (tête gelée) | `7803af70592c` | reçu par le pilote en tête ; classe light par trou de chemins (#2653), admis à la main avec les 2 avis dev1 (Claude) + cx1 (Codex) sur cette tête |
| 2 | #2620 | `dc7148b339c7` (tête gelée) | `58b4882c63bb` | sensible — 2 avis (dev1 Claude, cx1 Codex) |
| 3 | #2658 | `ce40f3970525` (tête gelée) | `36d0c4d1a263` | sensible (SQL 0467) — 2 avis (dev1 Claude, cx1 Codex) |
| 4 | #1484 | `6ebb18539e14` (tête gelée) | `52e1e1be5377` | sensible (SQL rôle release) — 2 avis (dev1 Claude, cx1 Codex) |
| 5 | #2603 | `b933e3f02a55` (tête gelée) | `404fd48712fc` | sensible — 2 avis (dev7 Claude, cx2 Codex) |
| 6 | #2617 | `20b83ad843e6` (tête gelée) | `989a41da462b` | sensible — 2 avis (dev1 Claude, cx1 Codex) |
| 7 | #2664 | `370c750e4fcc` (tête gelée) | `acc364138a63` | sensible — 2 avis (dev1 Claude, cx1 Codex) |
| 8 | #2654 | `c90dd211196e` (tête gelée) | `3ea582608614` | léger — 2 avis (cx3 Codex, dev5 DeepSeek) |
| 9 | #2656 | `5a4508d2c83c` (tête gelée) | `223d3db87d80` | léger — 2 avis (dev7 Claude, dev5 DeepSeek) |
| 10 | #2572 | `06ebd30f546d` (tête gelée) | `9694703dfb6e` | léger — 2 avis (cx3 Codex, cx2 Codex) ; un avis hors auteur suffit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>` (relu par
`scripts/paquet-compose.py --units` : 10 unités, 10 lots portés). Aucun conflit
de composition, aucune résolution, aucune adaptation de test. Une seule
migration SQL additive : **0467** (#2658), numéro réservé L122 ; #1484 modifie
`deploy/sql/role-release-controller.sql` sans nouveau numéro.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #114 | `blocked-verdict` : verdict bloquant sur le gel effectif |
| #286 | `blocked-verdict` ; n'entre avec home#124 que lorsque ses deux avis sont acquis (consigne pilote) — ce n'est pas le cas |
| #1022 | `freeze-unresolved` + conflit : gel absent du clone de calcul |
| #2443 | `verdicts-missing` + conflit sur `src/ameesh/storage/postgres/work.py` |
| #2451 | `conflict` avec `main` : `scripts/garde-mecanique.py`, `src/ameesh/mechanics.py`, `src/ameesh/review_designation*.py` |
| #2455, #2459 | `target-mismatch` + `branch-not-published` : branches canon hors de la cible |
| #2466 | `conflict` avec `main` : `docs/EXECUTEUR-MEDIEE.md`, `src/ameesh/runner.py`, `src/ameesh/storage/postgres/work.py` |
| #2488 | `verdicts-missing` sur `3c7c54912a65` (seul dev4, hors relecteur désigné dev1) |
| #2511 | `verdicts-missing` sur `0a5231d8ae98` (cx1 seul) |
| #2520 | `verdicts-missing` sur `4f2519bf3ae4` (cx3 seul) |
| #2538 | `verdicts-missing` sur `e7834ed960b7` (cx3 seul) |
| #2539 | `verdicts-missing` sur `701cba88e60b` (cx3 seul) |
| #2562 | `freeze-after-verdict` |
| #2582 | `verdicts-missing` sur `a200d5e6b2d0` (aucun) |
| #2585 | `verdicts-missing` sur `496fc0379960` (cx1 seul) |
| #2613 | `verdicts-missing` sur `0183415640a7` (cx1 seul) |
| #2623 | `verdicts-missing` sur `7c7a32cff8ca` (cx2 seul) |
| #2629, #2653 | `missing-branch` : lot de canon (home), sans branche ameesh à composer |
| #2648 | `verdicts-missing` sur `7e0db389ceb2` (aucun) |
| #2668 | `blocked-verdict` sur `60cbca35fb7b` |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 07:14 UTC, base
  `2be10c08d` : **32 candidats, 10 prêts, 22 en attente, 0 erreur**
  (`--ready-out`). Les 10 prêts sont composés ; aucun prêt écarté.
- Composition : **10 unités, 0 conflit** ; 73 fichiers, +3862/−338 hors
  manifeste ; `git diff --check` vert, aucun marqueur de conflit.
- `paquet-inclusion` (tête de ce manifeste, `--ready-file` + admissions) :
  **rc 0** — 10 admissions, 10 compositions, 0 défaut.
- Migration : une seule, **0467** (#2658), additive ; numéro réservé (L122),
  aucune autre migration au diff.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée ;
  `scripts/doc-links.py` : 170 fichiers, **0 lien cassé**.
- Prévalidation L161f : **indisponible**, deux refus nommés. (1) Commande
  `ameesh work prevalidate` sur `9694703dfb6e` avec le plan du paquet
  (`full/unsure`, sans carte) → `no-vm` (l'hôte local `ameesh-agents-1` est
  refusé par le code installé, avant la règle d'hôte de #2644). (2) Même plan
  lu par le code du porteur (#2644) → `card-missing` : « carte absente ; main,
  release/** and the integration branch run the full suite ; sélection non
  sûre: no map ; phase 2 non sûre: no map ». Écart noté ; suite complète
  lancée par le fallback (§3/§12, consigne pilote du 19/10 03:37).
- Tests ciblés sur l'arbre composé : **248 tests OK par pilote**, 0 échec —
  psql 336 s, psycopg 160 s (`scripts/test.sh` avec
  `AMEESH_TEST_VENV`). Modules : `test_711_migration_reservations`,
  `test_l122_numeros_de_migration`, `test_2644_prevalidation_calls`,
  `test_2664_familles_canon`, `test_2601_packet_watch`,
  `test_2404_paquet_candidats`, `test_l232b_bootstrap_installer`,
  `test_plan_git`.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
