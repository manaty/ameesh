# Paquet integration/paquet-h-20261019-1600 — composition (§12)

Base : `main` **`f21e6c35c5c6`** (paquet h-1200 fusionné, PR #2055). Branche
`integration/paquet-h-20261019-1600`, cible `main`. Une seule suite complète est
demandée sur la tête finale.

Composition initiale à 15:0xZ avec 5 unités ; recomposée avant la CI avec
#2613, dont la remise sur `main f21e6c35` venait de recevoir ses deux avis
delta (14:47Z) et dont le merge-tree avec les 5 têtes est propre (vérifié par
l'auteur, puis composé sans conflit). #2451, devenu « prêt » sur un nouveau gel
à 14:55Z, est écarté : sa résolution `9654353a` porte sur une tête antérieure
(`4c97c889`) et conflit avec les unités du paquet (voir « Écartés »).

## Admissions (6 lots composés)

| lot | source | motif |
|---|---|---|
| #2727 | `507f9f9267d5` (tête gelée) | prêt mécanique, porte OK (cx2 Codex + dev1 Claude) ; sensible (train, porte de fusion) ; P1 en retard SLO review→merge, demandé par le bureau |
| #2681 | `3a142136d016` (tête gelée) | prêt mécanique, porte OK (cx2 Codex + dev1 Claude) ; sensible (train, packet_candidates, plan_git, review_*) ; P1 |
| #2698 | `915bdc85c1cf` (tête gelée) | prêt mécanique, porte OK (impl-06 DeepSeek + cx2 Codex ; hold levé 14:27) ; sensible (train_policy, garde) |
| #2733 | `bd4d6f4f8daa` (tête gelée), composé par la résolution `50b10bd3f5ec` | prêt mécanique, porte OK (dev1 Claude + cx3/cx1 Codex) ; sensible (train_shadow) ; conflit mécanique avec #2727 résolu par `50b10bd3` — une ligne `_WORK` identique ajoutée des deux côtés, gardée une fois, union `_commit_lots`/`_manual_sync`/`_sync_proof` ; aucun avis requis (§2) |
| #2511 | `0a5231d8ae98` (tête gelée) | prêt mécanique, porte OK (cx1 Codex + dev7 Claude) ; sensible (stagnation, exploitation) ; P3 |
| #2613 | remise `193f6f5a698f` (`refs/ameesh/resolutions/2613/main-f21e6c35c5c6`) | prêt mécanique : remise sur `main f21e6c35`, porte OK sur le gel `01834156` (cx1 Codex + dev7 Claude) et **deux avis delta sur la remise** (cx1 et dev7, 14:47Z) ; sensible (runner, storage, contrat médié) ; merge-tree rc 0 avec les 5 autres têtes |

## Composition (6 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2727 | `507f9f9267d5` | `d515dae35f52` | sensible (train, porte de fusion) ; merge-tree rc 0, 0 conflit |
| 2 | #2681 | `3a142136d016` | `79ffdd875ffa` | sensible (train, packet_candidates, plan_git, review_*) ; merge-tree rc 0, 0 conflit |
| 3 | #2698 | `915bdc85c1cf` | `841c85d2376b` | sensible (train_policy, garde) ; merge-tree rc 0, 0 conflit |
| 4 | #2733 | `50b10bd3f5ec` (résolution) | `13fe1d38b129` | sensible (train_shadow) ; résolution mécanique avec #2727, 0 conflit |
| 5 | #2511 | `0a5231d8ae98` | `1176e7ff9acf` | sensible (stagnation, exploitation) ; merge-tree rc 0, 0 conflit |
| 6 | #2613 | `193f6f5a698f` (remise) | `a0277cf31d30` | sensible (runner, storage, contrat médié 1.4) ; remise composée sans conflit, 0 résolution |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>` (relu par
`scripts/paquet-compose.py --units --base f21e6c35c5c6` : 6 unités, 6 lots
portés). Aucun conflit de composition ; les seules résolutions employées sont
celles de #2733 avec #2727 (`50b10bd3f5ec`, mécanique) et la remise de #2613
(`193f6f5a`, delta relu). Aucune migration.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2451 | prêt sur le gel `c8da2c60` (porte OK, cx3 ; merge-tree clean sur main), mais sa résolution avec #2698 (`9654353a`) porte sur une tête antérieure (`4c97c889`) et ne compose pas sur le gel : conflits `scripts/garde-mecanique.py`, `src/ameesh/train_history.py`, `src/ameesh/train_review.py`, `tests/test_1467_train_history.py`, `tests/test_1467_train_review.py` (avec #2698, #2681 et #2613). Il faut une résolution fraîche sur `c8da2c60` ; lot écarté (§12), à composer au prochain véhicule |
| #2539 | non prêt : verdicts manquants (2 familles requises) et conflit `docs/EXECUTEUR-MEDIEE.md` avec main |
| #2443 | non prêt : gel après verdict refusé et conflit `docs/EXECUTEUR-MEDIEE.md` |
| #2648 | non prêt : 1 verdict sur 2 |
| #286 | hors liste calculée (pause pilote, troisième famille Claude) |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` (base `f21e6c35c5c6`, lecture
  fraîche d'`origin/main`) : **29 candidats, 7 prêts, 22 en attente, 0 erreur**
  (les deux blobs illisibles de la mi-journée — #2681 `04b645d2`, #2619
  `b44c1630` — sont revenus après un `git fetch` explicite des têtes). Les
  7 prêts sont composés (6) ou écartés avec motif (#2451).
- Composition : **6 unités, 0 conflit** ; 46 fichiers, +2710/−810 ;
  `git diff --check` vert, aucun marqueur de conflit.
- `paquet-compose.py --units --base f21e6c35c5c6` : **6 unités de fusion,
  6 lots portés** (#2727 `507f9f9267d5`, #2681 `3a142136d016`, #2698
  `915bdc85c1cf`, #2733 `50b10bd3f5ec`, #2511 `0a5231d8ae98`, #2613
  `193f6f5a698f`).
- `paquet-inclusion` (tête finale, manifeste ajouté au-dessus, `--ready-file`
  recalculée) : **rc 0** — 6 admissions, 6 compositions, 1 prêt écarté avec
  motif, 0 omission, 0 défaut.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée,
  6 exemptions utilisées (toutes au registre).
- `scripts/doc-links.py` : 171 fichiers, **0 lien cassé**.
- Tests ciblés sur l'arbre composé (psql puis psycopg, `scripts/test.sh`) :
  **N_TESTS tests OK par pilote** sur les 14 modules des 5 premières unités
  (train, admissions, candidats, historique, revue, runner, runtime, paquet
  candidats, shadow, provenance des corrections, lecture des avis,
  train_policy compilé, alertes SLO, plan git) ; plus les modules de #2613
  (contrat médié `l107`, sessions `l165c`) sur l'arbre recomposé ; 0 échec.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
