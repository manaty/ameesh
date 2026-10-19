# Paquet integration/paquet-h-20261019-0700 — composition (§12)

Base : `main` **`64e85ed9bd`** (paquet h-0600, PR #2010, fusionné en
`64e85ed9b` le 19/10 05:05:50 UTC). Branche `integration/paquet-h-20261019-0700`,
cible `main`. Une seule suite complète est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis — `main` a bougé à `64e85ed9bd`
(h-0600 fusionné) ; liste calculée `paquet-candidats.py` (4 prêts). Entrée :
message `mty-amh-impl-06` #112641 (#879 prêt pour le paquet suivant, verdicts
`ameesh-cx1` #13051 et `ameesh-dev1` #13053 sur le gel `701006dd`).

## Admissions (3 unités, 3 lots portés)

- #2601 — paquets horaires : alerte « créneau manqué » au composeur et au pilote
- #2642 — porte des avis : le plancher d'avis d'un lot léger vient du canon
- #879 — reprise 40P01, cible d'allocation canonique, landed non ressuscité

## Composition (3 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #879 | `701006ddd5cd` (tête gelée) | `e2f9a982c583` | sensible (SQL 0432) — 2 avis (cx1 Codex #13051, dev1 Claude #13053) |
| 2 | #2601 | `b7bf32708dba` (tête gelée) | `3a2a063b4cbd` | sensible — alerte créneau manqué ; 2 avis (cx1 Codex #13033, dev5 DeepSeek #13041) |
| 3 | #2642 | `5077f8678876` (tête gelée) | `2b535db560f7` | sensible — plancher d'avis léger ; 2 avis (cx3 Codex #13018, dev2 Claude #13038) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>` (relu par
`scripts/paquet-compose.py --units` : 3 unités, 3 lots portés). Aucun conflit
de composition, aucune résolution, aucune adaptation de test. Une migration
SQL additive : 0432 (#879, numéro réservé L122).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2644 | avis pilote manquants : doute → sensible (§8), deux familles désignées à la main par le pilote (dev1, cx1) sur la tête `b40f10da` ; le calcul automatique le donne light/0 avis par le trou de chemins décrit au lot #2653. Reporté jusqu'aux deux verdicts ou à #2653. |
| #2466 | `conflict` dans le calcul ; la remise publiée `8165e591` (merge-tree rc 0 sur `64e85ed9bd`) n'est pas reconnue par `paquet-candidats` et son avis reste dû — paquet suivant. |
| #2443, #2603, #114 | `blocked-verdict` : verdict bloquant sur le gel effectif. |
| #2451 | `conflict` + `verdicts-missing` : aucun avis et conflit sur `review_designation*.py`. |
| #2613, #2538, #2539, #2562, #2585, #2511, #2582, #2520, #2488, #2623, #2620, #2656 | `verdicts-missing` / `freeze-after-verdict` / `freeze-not-head` : avis dus sur la tête gelée. |
| #2653, #2629 | `missing-branch` : lot de canon (home), sans branche ameesh à composer. |
| #2455, #2459 | `branch-not-published` / `target-mismatch` : branches canon hors de la cible. |
| #286 | `no-verdict` : relecteur non désigné. |
| #1022 | `freeze-unresolved` + conflit : gel absent du clone de calcul. |
| #1484 | `freeze-after-verdict` + conflit. |

Les 24 lots non prêts restent en attente ; aucun autre n'est prêt à cette
coupure (calcul `paquet-candidats.py` à 05:08 UTC, base `64e85ed9bd`).

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 05:08 UTC, base
  `64e85ed9bd` : **4 lots prêts, 24 en attente, 0 erreur** (`--ready-out`).
  3 prêts composés, #2644 écarté avec motif ci-dessus.
- Composition : **3 unités, 0 conflit** ; 27 fichiers, +1323/−113 hors
  manifeste ; `git diff --check` vert, aucun marqueur de conflit.
- `paquet-inclusion` (tête de ce manifeste, `--ready-file` + admissions du
  manifeste) : **rc 0** — 3 admissions, 3 compositions, 0 défaut ; #2644
  écarté motivé.
- Migration : une seule, **0432** (#879), additive ; numéro réservé (L122),
  aucune autre migration au diff.
- Tests ciblés sur l'arbre composé : **209 tests OK**, deux pilotes
  (psql 240,5 s ; psycopg 119,8 s), 0 échec — #879
  (`test_711_migration_reservations`, `test_l122_numeros_de_migration`,
  `test_l160g_migration_reservations`), #2601 (`test_2601_packet_watch`),
  #2642 (`test_2404_paquet_candidats`, `test_2394_review_gate_verdicts`,
  `test_l1616_mechanics`).
- Prévalidation L161f : **indisponible** — plan du paquet `full / unsure`
  (aucune carte de tests) puis `--host ameesh-agents-1` → `no-vm`, les deux
  refus nommés ; écart noté, suite complète lancée par le fallback (§3/§12,
  consigne pilote du 19/10 03:37).
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
