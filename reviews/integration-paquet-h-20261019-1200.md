# Paquet integration/paquet-h-20261019-1200 — composition (§12)

Base : `main` **`b053ee263aa1`** (paquet h-1100 fusionné, PR #2053). Branche
`integration/paquet-h-20261019-1200`, cible `main`. Une seule suite complète est
demandée sur la tête finale.

Recomposition du 19/10 14:0xZ : la première composition (tête `50bbaccd4f13`,
PR #2055) était rouge — 8 échecs `test_811` et `test_1270`, dus au changement
`_pick` de #2466 (les doublures partielles ne simulaient pas
`_scheduled_wake_spec`). #2466 a une nouvelle tête `9b31c9cb` (adaptation de
`test_1270`, une ligne de mock) ; l'adaptation équivalente de `test_811` est
portée par le paquet (`8fa6c657f`, test seul, documentée ci-dessous). Le
véhicule est recomposé sur cette tête avec les autres unités prêtes au moment
de la composition.

## Admissions (6 lots composés)

| lot | source | motif |
|---|---|---|
| #2466 | `9b31c9cbfac4` (tête gelée) | admission pilote mesh-design 13:31, dispense 1979 reconduite sur ce gel ; avis cx3 (Codex) sur `9b31c9cb` et dev1 (Claude) sur `abc2fbf`, union qualifiée ; provenance `58818390` signalée |
| #2689 | `6ef894576b7a` (tête gelée) | prêt mécanique, porte OK (cx1 Codex + dev1 Claude sur la tête) ; sensible (runner, vue SQL 0468, deploy/sql) |
| #2574 | `c7dc8ff8e143` (tête gelée) | prêt mécanique, porte OK (cx2 Codex + dev1 Claude) ; sensible (packet_candidates, porte de fusion) |
| #2704 | `37b432e642ec` (tête gelée) | prêt mécanique, porte OK (cx3 Codex) ; léger |
| #2701 | `743d00d1229a` (tête gelée) | prêt mécanique, porte OK (cx3 Codex) ; léger |
| #2723 | `99fc79b7c318` (tête gelée) | prêt mécanique, porte OK (cx2 Codex) ; léger (revue après fusion 2556) |

## Composition (6 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2466 | `9b31c9cbfac4` | `32ce834108ab` | sensible (migration 0466 additive, runner, storage) ; merge-tree rc 0, 0 conflit |
| 2 | #2689 | `6ef894576b7a` | `7e92b1e857f4` | sensible (runner, vue SQL 0468 additive, deploy/sql) ; merge-tree rc 0, 0 conflit |
| 3 | #2574 | `c7dc8ff8e143` | `cee46193cefd` | sensible (packet_candidates, porte de fusion) ; merge-tree rc 0, 0 conflit |
| 4 | #2704 | `37b432e642ec` | `6d219196c051` | léger (housekeeping) ; merge-tree rc 0, 0 conflit |
| 5 | #2701 | `743d00d1229a` | `dfa9939c3c79` | léger (témoin 2582) ; merge-tree rc 0, 0 conflit |
| 6 | #2723 | `99fc79b7c318` | `08787e518cbe` | léger (revue après fusion #2556, témoin) ; merge-tree rc 0, 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>` (relu par
`scripts/paquet-compose.py --units --base b053ee263aa1` : 6 unités, 6 lots
portés). Aucun conflit de composition, aucune résolution ; migrations 0466 et
0468 additives (`add column if not exists`, index partiel, `create or replace
view`), appliquées avant les exécuteurs au déploiement staging (§14).

## Adaptation de test

`8fa6c657f` — `tests/test_811_lot_workdirs` : la doublure partielle simule
`_scheduled_wake_spec`, comme `test_1270` sur la tête `9b31c9cb`. Test seul,
aucun changement de production ; la tête et les avis de #2466 restent
`9b31c9cb`. Rouge avant (`AttributeError: 'object' object has no attribute
'query'`), vert après (39 tests psql + 39 psycopg).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2698 | prêt mécaniquement sur `915bdc85c1cf`, mais hold pilote mesh-design depuis 10:45 : le verdict codex d'ameesh-cx2 est attendu (op-paused ; distinct_families impose codex, auditeur 12:33) |
| #2613 | devenu prêt à 13:57 (verdict dev7 sur `01834156`) après la composition ; sa résolution avec #2466 (`c8ccabd2`) n'a pas d'avis sur le delta final — au prochain véhicule, #2466 étant alors sur `main` |
| #2727 | devenu prêt à 13:51 après la composition (p1, train/porte de fusion) — au prochain véhicule |
| #286 | pause pilote jusqu'à 16:00 (troisième famille Claude) ; non prêt |
| #2443 | nouvelle tête `47bf93cc` non couverte par le verdict dev4 ; non prêt |
| #2451 | laissé à ameesh-dev2, verdicts 1/2 ; non prêt |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` (base `b053ee263aa1`, lecture
  fraîche d'`origin/main`) : **34 candidats, 9 prêts, 24 en attente, 1 erreur
  de lecture** (#2681, blob `04b645d2` absent du clone de calcul ; lot en
  attente, hors véhicule). Les 9 prêts sont composés (6) ou écartés avec motif
  (#2698, #2613, #2727).
- Composition : **6 unités, 0 conflit** ; 27 fichiers, +1273/−42 (dont
  l'adaptation de test, 1 fichier, +2/−1) ; `git diff --check` vert, aucun
  marqueur de conflit, aucune résolution.
- `paquet-compose.py --units --base b053ee263aa1` : **6 unités de fusion,
  6 lots portés** (#2466 `9b31c9cb`, #2689 `6ef8945`, #2574 `c7dc8ff8`,
  #2704 `37b432e6`, #2701 `743d00d1`, #2723 `99fc79b7`).
- `paquet-inclusion` (tête finale, manifeste ajouté au-dessus, `--ready-file`
  recalculée) : **rc 0** — 6 admissions, 6 compositions, 3 prêts écartés avec
  motif, 0 omission, 0 défaut.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée,
  9 exemptions utilisées (toutes au registre).
- `scripts/doc-links.py` : 171 fichiers, **0 lien cassé**.
- Tests ciblés sur l'arbre composé (psql puis psycopg, `scripts/test.sh`) :
  **361 tests OK par pilote** sur les 10 modules des unités (6 ignorés en psql,
  dépendants de l'environnement) et **39 tests OK par pilote** sur `test_811`
  après l'adaptation ; 0 échec, 0 erreur.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
