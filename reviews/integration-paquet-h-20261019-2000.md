# Paquet integration/paquet-h-20261019-2000 — composition (§12)

Base : `main` **`52d1e28cda65`** (release 1.14.0, coupure de 23:00 du
19/10/2026, `[skip ci]`). Branche `integration/paquet-h-20261019-2000`, cible
`main`. Une seule suite complète est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis. Recompose après **retrait de
l'unité #2766** : son écart P2 sensible de dev1 (trois lectures git du véhicule
qui échouent en mode ouvert, `src/ameesh/packet_reviews.py`) est bloquant sur
un lot sensible ameesh (§2). Le pilote l'a retiré de tout paquet (note #2766,
19/10 21:57) et docs-01 a gelé une nouvelle tête `c2a9b7150e61` (gel #14002,
correctif `VehicleError`) le 19/10 22:08 ; ses deux avis (dev1 + cx3) sont dus
sur cette tête, qui ira au paquet suivant. `6fe2f870d06a` n'entre dans aucun
paquet.

Liste calculée `scripts/paquet-candidats.py` à 22:12 UTC sur `52d1e28c`
(lecture fraîche d'`origin/main`) : **8 prêts, 9 en attente, 0 erreur**. Le
paquet porte **7 unités, 7 lots** : les 8 prêts moins #2743, devenu prêt à
22:11 seulement (hors admission du 21:30) et dont `work show` dit « waiting
merge (executor not designated) » ; il reste au paquet suivant, sans élargir
cette recomposition. #2766 est en attente (verdict dev1 dû sur `c2a9b715`).

## Admissions (7 unités, 7 lots portés)

| lot | source | motif |
|---|---|---|
| #2739 | `b809763a3925` (tête gelée, gel 13954) | dispense du pilote : delta de tests seuls sur `8b5c419c`, aucun contenu nouveau ; sensible (review_*, fire, merge_queue, lot_class) |
| #2696 | `415ed5c66c06` (tête gelée) | 2 avis OK (cx3 codex + dev4 claude) ; sensible (lot_class, merge_queue) |
| #2738 | `43bc7d22ce49` (tête gelée) | 2 avis OK (dev7 claude + cx2 codex) ; sensible (merge_queue, packet_candidates) |
| #2752 | `63e03bae6f42` (tête gelée) | 2 avis OK (cx1 codex + dev5 deepseek) ; sensible (mailbox PostgreSQL) |
| #1022 | `c0d5a418064c` (tête gelée) | 1 avis OK (cx3 codex) ; léger (témoins de la migration 0420) |
| #2660 | `bdbfab85e4ff` (tête gelée) | 2 avis OK (auditeur claude + cx1 codex) ; sensible (cible de migration) |
| #2763 | `cf57f3de4a7d` (tête gelée, gel 13861) | 2 avis OK (cx3 codex + impl-06 deepseek) ; sensible (déploiement, consignes) |

## Composition (7 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2739 | `b809763a3925` | `dddfc55a6f8f` | sensible ; 0 conflit |
| 2 | #2696 | `415ed5c66c06` | `b683eb831792` | sensible ; 1 conflit résolu dans `lot_class.py` (union `base_sha` + `project`) |
| 3 | #2738 | `43bc7d22ce49` | `4369ada4833a` | sensible ; 0 conflit |
| 4 | #2752 | `63e03bae6f42` | `76cd8024213c` | sensible ; 0 conflit |
| 5 | #1022 | `c0d5a418064c` | `f7ac1227bcee` | léger ; 0 conflit |
| 6 | #2660 | `bdbfab85e4ff` | `2eb8fa241eef` | sensible ; 0 conflit |
| 7 | #2763 | `cf57f3de4a7d` | `c6cf2063b003` | sensible (P1) ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le
lot et son SHA, et il porte un trailer `ameesh-work: <lot>`
(`scripts/paquet-compose.py --units --base 52d1e28c` : 7 unités de fusion,
7 lots portés).

## Résolutions de composition (1, mécanique)

| paire | fichier | nature | preuve |
|---|---|---|---|
| #2739 × #2696 | `src/ameesh/lot_class.py` | union de deux clés ajoutées au dict initial de `of_lot` : `base_sha` (#2739) et `project` (#2696) | blob identique à celui déjà vérifié dans la composition précédente (`bbc03dbb1`, qui portait #2739 + #2766 + #2696 ; #2766 ne touche pas `lot_class.py`) ; le diff aux deux parents ne montre que l'union, `project` étant réaffecté juste après depuis la ligne de lot |

La résolution préparée `1b9d297d4898` (#2739 × #2766, `merge_queue.py`) est
**obsolète et non reprise** : #2766 est retiré du paquet, son union `vehicle`
n'a plus lieu d'être. Aucune migration SQL n'est ajoutée ni modifiée par la
composition (le seul fichier `.sql` du diff est un nom de test).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2766 | **retiré sur bloquant** : P2 sensible de dev1 (lectures git fail-open) = bloquant §2 ; pilote 19/10 21:57, docs-01 22:08 ; nouvelle tête `c2a9b7150e61` (gel #14002) en attente de dev1 + cx3, paquet suivant. `6fe2f870d06a` n'entre dans aucun paquet |
| #2743 | prêt à 22:11 seulement (1 avis dev2 sur `7940042b`), hors admission du 21:30 ; « waiting merge (executor not designated) » ; au paquet suivant, la recomposition demandée ne l'élargit pas |
| #114, #1515, #2443, #2538, #2562, #2702, #2729, #2757 | en attente dans la liste calculée (avis indépendant manquant ou conflit avec `main`) ; aucun n'est admis sans porte verte |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 22:12 UTC, base
  `52d1e28cda65` : **8 prêts, 9 en attente, 0 erreur** ; les 7 prêts admis sont
  composés ; #2743 (prêt) et #2766 (en attente) sont écartés pour les motifs
  ci-dessus.
- Composition : **7 unités, 0 conflit non résolu** ; 37 fichiers (hors
  manifeste), +2247/−138 ; `git diff --check` vert, aucun marqueur de
  conflit.
- `paquet-compose.py --units --base 52d1e28c` : **7 unités de fusion,
  7 lots portés** (#2739 `b809763a3925`, #2696 `415ed5c66c06`,
  #2738 `43bc7d22ce49`, #2752 `63e03bae6f42`, #1022 `c0d5a418064c`,
  #2660 `bdbfab85e4ff`, #2763 `cf57f3de4a7d`).
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée
  (1 exemption utilisée, #2642).
- `scripts/doc-links.py` : 172 fichiers, **0 lien cassé**.
- `paquet-inclusion` (liste prête recalculée à 22:12, manifeste, `#2743`
  excusé) : **rc 0** — 7 admissions, 7 compositions, 6 contenus présents,
  1 partiel attendu (#2739 : la ligne `base_sha` du dict de `of_lot` est
  réécrite par l'union #2696 ; plus de réécriture `review_gate` puisque #2766
  est retiré).
- Tests ciblés sur l'arbre composé, **psql + psycopg** : **425 tests par
  pilote, OK, 0 échec** (psycopg 361 s ; psql vert avant lui), modules des
  7 unités et régressions de la porte (`test_2739_review_correction`,
  `test_2696_gate_train_reviews`, `test_2738_clone_objects`,
  `test_l165c_sessions_par_lot`, `test_1022_creneau_landed`,
  `test_l122_numeros_de_migration`, `test_2763_venv_guard`,
  `test_l160e_merge_queue`, `test_1003_merge_pr`,
  `test_2404_paquet_candidats`, `test_789_slo_alerts`).
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
