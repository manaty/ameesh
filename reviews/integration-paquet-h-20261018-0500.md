# Paquet integration/paquet-h-20261018-0500 — composition

Base : `main` **`44cb7dc51840`** (fusion de la PR #1697, paquet h-2300, 02:06Z). Aucun paquet n'était ouvert ; `main` n'a pas bougé depuis cette fusion.

Demandes : **bureau** — mail du 18/10 02:48Z : main libre, aucun paquet ouvert, composer le suivant (§11 bis) ; priorité #2296 (P1, alerte SLO), prêts d'après leurs auteurs #2285 (`352ba651`, résolution `1c3a69d9`), #2340 (`e5b037d9`), #1844 (`9ff7c4832`), #2333 (`511c43346`, avis dev1 en attente). **Orchestrateur** — mail du 18/10 02:44Z : #2289 prêt (`84045d1a`, avis codex3 + mty-nxl-rel-01), avec #2341 (`466609e44`), #2342 (`c40fde773`), #2336 (`e9ee6759e`) ; #1515 à ne pas inclure. Le composeur ajoute #2306 (`809823bf`, deux avis) : la résolution `1c3a69d9` lie la paire #2306 × #2285 et le pilote confirme que le paquet prend `352ba651` (fil #2285, 02:20Z).

## Admissions (8 lots)

- #2296
- #2285
- #2306
- #2340
- #2289
- #2341
- #2342
- #2336

## Composition (8 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2296 | `12b959de45b3c630c554ccb9d393065bf1b712e7` (`lot/2296-admission-pression-memoire`, PR #1689) | `2ba41f91b05f` | deux avis sur la tête : Claude `mesh-design` + Codex `ameesh-cx1` ; classe prod sensible (`src/ameesh/host_*`, `resources.py`, `canon.py`) ; composition propre |
| 2 | #2341 | `466609e44b4a11e2fc0e4a7b552f41dfd9f1da7c` (`auditeur/host-unsampled`) | `5e77d7dce95d` | avis Codex `codex1` sur la tête (auteur Claude : autre famille) ; classe prod sensible (`exploitation.py`, `notify.py`) ; composition propre |
| 3 | #2342 | `c40fde773f01e2c90bd76176e0306e81acc56014` (`auditeur/engagement-overdue-p80`) | `bd3dbcebd03d` | avis Codex `ameesh-cx1` sur la tête (auteur Claude) ; alerte/roadmap (`roadmap.py`, `roadmap_dev.py`) ; composition propre |
| 4 | #2340 | `e5b037d9b3468dd980987b12bf7d64377d626326` (`auditeur/consigne-auditeur-dsn-journal`, PR #1736) | `b791534bcd56` | deux avis sur la tête : Claude `ameesh-dev4` + Codex `ameesh-cx1` ; SQL/droits (`deploy/sql/role-auditeur.sql`), contrat `tests/parts/toujours.txt` ; composition propre |
| 5 | #2289 | `84045d1a354371e975cc584e8cce60dbe6924219` (`lot/2289-required-refusal-slug`, PR #1705) | `3561de311c93` | deux avis sur la tête : Codex `codex3` + DeepSeek `mty-nxl-rel-01` ; porte de fusion (`merge_pr.py`) ; composition propre |
| 6 | #2336 | `e9ee6759e5de96e3f40adfa8bc82169304b79f1f` (`auditeur/plafond-contexte`, PR #1737) | `37298ecc7d16` | avis Codex `codex1` sur la tête (auteur DeepSeek : autre famille) ; §8 exécuteur (`runner.py`, `adapters.py`) ; composition propre |
| 7 | #2306 | `809823bf3765e043667fd7b59942dea012f6726a` (`lot/2306-check-read-retry`) | `18474b8a8d01` | deux avis sur la tête : Codex `ameesh-cx1` + Claude `auditeur` ; porte de fusion (`fire.py`, `land.py`) ; composition propre |
| 8 | #2285 | `352ba651d71354dcfc331a11d7130ccd0ff0fe17` (`lot/2285-feu-checks-requis`, PR #1680), via la résolution `1c3a69d9a000792c67021fbf8a91ff120cc80df1` (#2306 × #2285) | `d97951ecb72f` | deux avis sur la tête : Claude `ameesh-dev4` + Codex `ameesh-cx1` ; porte de fusion (`fire.py`) ; résolution refaite sur `352ba651`, `fire.py` résolu à l'identique (même sha256), adaptation `test_2273` portée par #2285, 314 tests ciblés verts côté dev3 ; composition propre |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte le trailer `ameesh-work: #<lot>`. Aucun contenu nouveau : les 8 unités ajoutent **39 fichiers, +2210/−234** vs `44cb7dc51`, dont la SQL de rôle `deploy/sql/role-auditeur.sql` (unité 4, deux familles), aucun `.github/**`, aucune migration.

## Conflits et résolution

Aucun conflit de composition : les 8 merges `--no-ff` passent rc=0 sur `44cb7dc51`. Seul chevauchement entre unités : #2306 × #2285 sur `src/ameesh/fire.py`, couvert par la résolution `1c3a69d9a000` (parents `809823bf` × `352ba651`), utilisée comme source de l'unité 8 ; `git diff 1c3a69d9 d97951ec -- src/ameesh/fire.py` est vide, donc le contenu de la résolution est bien celui de la tête. Le contrôle d'inclusion signale #2306 « partiel » sur `src/ameesh/fire.py` (60/70 lignes) : les trois lignes absentes sont des **commentaires** de la version #2306, remplacés par la rédaction relue de #2285 (bloc `_required_gap`, « #2306 : un échec passager lève `land.TransientLandError` ») ; le code de reprise de #2306 (`_transient_read`, codes 408/429/5xx, `land.TransientLandError`) est présent, et `git diff 352ba651 1c3a69d9 -- src/ameesh/fire.py` n'ajoute que ces hunks #2306 déjà relus. Aucune adaptation de contenu relu par le composeur, aucun commit de résolution nouveau.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #1844 | lot **nexlink** (Pages staging), pas du dépôt `manaty/ameesh` ; sa branche et son commit `9ff7c4832` sont absents de la forge ameesh (commit inconnu de l'API GitHub et du clone) |
| #2333 | second avis dû (dev1) sur la tête `511c43346` |
| #2337 | second avis dû (dev2) : classe critique, deux avis requis |
| #2349 | verdict dû (dev4) |
| #1515 | consigne du pilote (02:44Z) : ne pas inclure |
| #2212, #897, #1280, #2265, #2263 | verdicts incomplets, tenus par le pilote ou hors admission |
| autres lots qa | verdicts incomplets ou hors admission du pilote |

## Preuves

- `git diff --check 44cb7dc51..HEAD` : rc 0, aucun marqueur de conflit.
- Seconds parents et trailers `ameesh-work` : 8/8 vérifiés sur la première ligne de l'historique.
- `git diff 1c3a69d9 d97951ec -- src/ameesh/fire.py` : vide (résolution à l'identique).
- Chaque tête source est la tête distante gelée (`ls-remote` = sources listées) ; aucun rebase de lot, aucune CI de lot.
- `scripts/paquet-inclusion.py --manifest <ce fichier> --head d97951ecb72f --expected 2296,2285,2306,2340,2289,2341,2342,2336` : verdict « aucun défaut détecté » ; 7 lots présents, #2306 partiel sur trois lignes de commentaire de `src/ameesh/fire.py` (lues ci-dessus, comportement de reprise présent) ; à rejouer sur la tête finale avant le gel, résultat consigné au fil du lot.

## CI et fusion

- **Une seule** suite complète sur la tête exacte du paquet : la branche `integration/paquet-h-20261018-0500` est poussée et la PR ouverte immédiatement pour que le portier `paquet.yml` constate la couverture ; s'il la constate, l'étiquette `ci-full-<sha>` déclenche l'unique suite `tests.yml`. Aucune CI de lot, aucun rebase.
- Le check requis `tests` doit être `success` avec des tests réellement exécutés (pas `skipped`/absent, #2271).
- `main` est réservée pendant la CI (§12) : aucune autre fusion n'y entre. Si `main` bouge sans conflit, la branche est remise à jour, le manifeste réécrit et la tête gelée de nouveau ; l'ancien run est annulé, une seule suite effective sur la tête finale.
- Fusion dès le vert par le composeur (`ameesh work merge-pr` sur la tête gelée, auto-merge GitHub désarmé), puis rattachement des unités au merge réel et clôture. Aucune opération hôte ici.
