# Paquet integration/paquet-h-20261017-1600 — composition

Base : `main` **`1e035e02070c3134f07e1ecf13b5af48d61d87a0`** (#2244/PR1614 atterri à
14:12Z, paquet h-1200). La branche est composée directement sur cette base ; le diff de la PR ne
porte que les 15 unités ci-dessous, ce manifeste et le registre. Tête de composition :
**`1d03884f9`** (compteurs du contrat), puis la correction d'intégration `item_columns` (voir
« Corrections dédiées ») ; la tête de branche au gel, qui porte ce manifeste, est le SHA exact
communiqué au pilote et porté par l'étiquette `ci-full-<sha>`.

Demandes : **pilote mesh-design** 17/10 16:2xZ (mail) : composer le paquet h-1600 sur
`1e035e02070c`, même procédure que h-1200 (§12 : suite complète unique sur la tête du porteur, main
réservée pendant sa CI) ; **#2225 en priorité** (dev1, PR1618, gel `eaf45b6a`, verdict impl-02
DeepSeek ok, sensible hors prod, SLO review GO → merge dépassé) ; ajouter les autres lots ameesh en
`qa` dont les verdicts requis sont acquis sur la tête gelée ; **#2240 et #2249 exclus** tant que
leurs avis (porte de fusion) ne sont pas complets ; demander le feu du pilote sur la tête figée.
Décisions antérieures reprises : #2214 premier paquet après h-1200 (pilote 12:41Z), #2076 admis
(mail #98357, 12:48Z), #2197 transport confié au composeur (13:26Z), #2225 rangé dans le prochain
paquet (15:44), #2231 en attente du prochain véhicule, #2055/#2242/#1466 unions prêtes (14:0xZ).

Exécuteur : **mty-amh-impl-01** (composition, unique CI de paquet, fusion). Aucune CI de lot, aucun
push de branche de lot, aucune fusion parallèle.

## Admissions (16 lots attendus du véhicule)

- #2225
- #2224
- #2214
- #2231
- #2076
- #2197
- #2055
- #2242
- #2200
- #1739
- #2090
- #2084
- #800
- #1466
- #1664
- #1104

## Composition (15 unités, 16 lots)

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2225 | `eaf45b6a7f17` (`lot/2225-couverture-pont`, PR1618) | `6a345fca0` | verdict `ok` mty-amh-impl-02 (DeepSeek) sur `eaf45b6a` ; sensible hors prod (mesure CI), un avis indépendant de l'auteur Claude ; 2 f. +135/−0 |
| 2 | #2224 | `9a81bc30c2af` (`lot/2224-mesure-carte-part`, PR1611) | `47e0501d1` | verdict `ok` mty-amh-impl-03 (DeepSeek) sur `9a81bc30` ; `.github` limité à la CI (carte.yml, plafond des mesures 40 → 60 min, `--plafond-min`), sensible hors prod ; 3 f. +59/−8 |
| 3 | #2214 | `035560f7bf65` (`lot/2214-ci-full-avis-guard`) | `abe607d71` | **deux avis** `ok` sur `035560f7` : ameesh-dev4 (Claude) + ameesh-cx3 (Codex) ; 3 f. +88/−27 |
| 4 | #2231 | `47ff493b37de` (`lot/2231-slo-alerte-sans-reassignation`) | `485ae99ef` | **deux avis** `ok` sur `47ff493b` : ameesh-dev4 (Claude) + ameesh-cx1 (Codex) ; 3 f. +196/−5 |
| 5 | #2076 | tête `00d128e0ac76` (`lot/2076-multiple-reviewers`, PR1492) via résolution `98e434123d7c` (2231×2076) | `4860730ea` | verdicts `ok` deepseek3 (DeepSeek) + orchestrateur (Claude) sur `00d128e0` ; cx1 (Codex) non cité, même famille que les auteurs (note auditeur 17/10 16:40) ; admis au paquet suivant (pilote 12:48Z) ; union `alert_briefs.py` rejouée par la résolution ; 15 f. +453/−46 |
| 6 | #2197 | tête `69bd7e1b7d64` (`agent/deepseek2-2197-pgpass`) via résolution `d964a5628f6d` (2197×920) | `a32e9c6ef` | verdict `ok` ameesh-dev7 sur `69bd7e1b` ; transport confié au composeur (13:26Z) ; union `tests/test_migrations.py` ; 4 f. +43/−11 |
| 7 | #2055 | `3b594af35d30` (`lot/2055-runner-session-lot-fini`) | `d9cef0f30` | verdict `ok` ameesh-dev2 (Claude) sur `3b594af3` ; 4 f. +234/−20 |
| 8 | #2242 | tête `c434e6d95eed` (`agent/deepseek2-2242-brief-park`, issue #1619) via résolution `666c29945db8` (2242×2055) | `cd8ba413f` | verdict `ok` ameesh-cx2 (Codex) sur `c434e6d9` ; union `runner.py` avec #2055 ; 2 f. +52/−10 |
| 9 | #2200 | `0c3d15df581f` (`lot/2200-pause-operateur-disponibilite`) | `d29776432` | verdict `ok` mesh-design (Claude) sur `0c3d15df` ; 10 f. +241/−9 |
| 10 | #1739 | remise `2ecfbdd73f05` (source gelée `309d9203e5bc`, issue #1289) | `0bee87187` | verdicts `ok` ameesh-dev2 et ameesh-dev1 sur la remise `2ecfbdd7` ; 3 f. +52/−12 |
| 11 | #2090 | `0bf9b7324db6` (`lot/2090-tube-plein-deterministe`) | `aa9fc4ff4` | verdict `ok` ameesh-dev7 (Claude) sur `0bf9b732` ; 2 f. +288/−17 |
| 12 | #2084 **+ #800** | résolution de paire `ee089b807b53` (2084×800 ; têtes `4813dba027ff` et `4648e4e8cbd8`) | `76711dc6b` | #2084 : verdict `ok` orchestrateur sur `4813dba0` ; #800 : verdict `ok` bureau (Claude) sur `e8c057ad`, **porté sur la tête `4648e4e8`** par la qualification mécanique du rebase (pilote 15/10 14:36, §11bis), union `work.py`/`mesh_cli.py`/`PLAN-DE-TRAVAIL.md` ; 8 f. +346/−32 |
| 13 | #1466 | tête `fa155195567f` (`lot/1466-config-relecture`) via résolution `9bcde27f979d` (2242×1466) | `c8d3030af` | prod `exploitation.py` : avis `ok` mty-amh-qa-01 (DeepSeek) + ameesh-cx2 (Codex) sur `fa155195`, avis dev1 (Claude) + cx1 (Codex) sur `92693765` portés par la qualification mécanique du rebase (pilote 15/10 15:33) — **deux familles Claude+Codex** ; conflit `runner.py` résolu en union (`train_passages` #1467 + `cfg_overrides` #1466) ; 12 f. +872/−20 |
| 14 | #1664 | tête `0f981ff733b8` (`lot/1664-hold-sans-date`) via résolution `b8e54f39f35b` (1664×2200) | `cbc26890f` | verdict `ok` ameesh-cx2 (Codex) sur `0f981ff7` ; conflit `work.py` (`note`) résolu en union (`check_wait_note` + option `once`) ; 14 f. +760/−45 |
| 15 | #1104 | tête `12d477d72e45` (`lot/1104-relecture-hors-file`) via résolution `6716d38078e5` (1534×1104) | `90d50a41e` | verdicts `ok` ameesh-cx1 (Codex) et mesh-design (Claude) sur les têtes antérieures, `ok` ameesh-cx2 (Codex) sur `12d477d7` ; conflits `interface.py` (imports `re` + `datetime`) et `docs/EXECUTEUR-MEDIEE.md` (compteurs du contrat dérivés, corrigés en 380/445 par `1d03884f9`) ; 9 f. +1293/−8 |

Chaque commit de composition a le lot (ou les deux lots) en second parent, son sujet nomme le lot et
il porte le trailer `ameesh-work: #<lot>`. Aucun contenu nouveau : chaque unité est la tête, la
remise ou la résolution exacte listée ci-dessus ; les résolutions sont celles publiées par leurs
auteurs (réfs `refs/ameesh/resolutions/…`) et ne changent que les unions de conflits.

## Contrôle unitaire du pilote — points ouverts

- **#2225** : priorité du pilote ; verdict unique DeepSeek (auteur Claude) sur `eaf45b6a`, classe
  sensible hors prod (mesure CI) ; la CI complète du paquet couvre le lot.
- **#2224** : `.github` limité à la CI — plafond des parts de mesure `carte.yml` porté à 60 min et
  `--plafond-min` du plan ; aucune condition de check requis modifiée. Un avis indépendant
  (impl-03, DeepSeek, auteur Claude). À recontrôler au contrôle unitaire.
- **#2214** : les deux avis (Claude + Codex) portent sur `035560f7` ; le lot touche `ci.py` (outil),
  pas la porte de fusion elle-même.
- **#2076 / #2084** : review slots et journal des relecteurs ; avis orchestrateur (une famille
  autre que les auteurs DeepSeek), unions `alert_briefs.py` / `work.py` rejouées.
- **#800** : verdict bureau (Claude) sur `e8c057ad`, porté sur `4648e4e8` par la qualification
  mécanique du rebase (pilote 15/10 14:36, diff constant) ; la résolution de paire avec #2084
  `ee089b807` est celle de docs-01 (17/10 10:45). Aucun second avis requis (sensible hors
  SQL/sécurité/prod, une famille autre que l'auteur DeepSeek).
- **#1466** : prod `exploitation.py` ; les deux familles Claude+Codex sont acquises sur la source
  gelée `92693765` et maintenues à leur portée par la qualification mécanique du rebase vers
  `fa155195` (pilote 15/10 15:33) ; le conflit `runner.py` est l'union mécanique avec #1467
  (`train_passages`) et #1466 (`cfg_overrides`), sans contenu nouveau.
- **#1104** : conflit `docs/EXECUTEUR-MEDIEE.md` résolu sur les compteurs du contrat de la tête du
  paquet (65 opérations / **380 refusées / 445 opérations d'interface**), confirmés par
  `tests.test_l107_executor_contract` (22 tests OK, pilotes psql+psycopg) au commit `1d03884f9`.
- **#2240 / #2249** : **exclus** sur consigne du pilote (porte de fusion, deux avis incomplets).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2240 | porte de fusion : deux avis requis, un seul acquis à l'heure de la composition (pilote 16:2xZ) |
| #2249 | porte de fusion : aucun relecteur désigné, deux avis requis (pilote 16:2xZ) |
| #2218 | second avis indépendant DeepSeek et arbitrage de conception du pilote encore attendus (note 14:16Z) |
| #711 | SQL (réservations de migrations) : un seul verdict `ok` sur la tête `50fe8357` ; deux familles requises |
| #897 | SQL (`leases.state`/trigger) : un seul verdict `ok` sur `6e167dd2` ; la branche porte en outre le delta #423 non fusionné |
| #1349 | résolution `52928ddb` signalée par le pilote : préimages OpenAPI/types sans postimages, rejouer ne résout pas le mécanisme |
| #1359 | 12 fichiers en conflit sur la tête du paquet ; résolution fraîche requise |
| #1515 | hold : second avis sécurité Codex impossible avant le 22/10 08:13 |
| #2160 | hold : seconde famille Codex impossible avant le 22/10 |
| #1630 | hold : CI annulée par l'owner, attente de relance/consigne |
| #1768 | P1 porte de fusion : la remise contient un delta fonctionnel sans avis de sécurité |
| #1272 | verdict attendu (ameesh-dev1) sur la tête courante |
| #1738 | désignation de relecteur retirée le 17/10 14:06Z |
| #1907, #2122, #2182, #2207, #2211, #2238 | aucun verdict `ok` sur la tête gelée |
| #2184 | verdict `blocked` |
| #1945 | gel après verdict incomplet (`freeze-after-verdict`) |
| #2248, #2227, #2219, #1984, #661, #660, #598, #2232, #2223, #2220, #2228 | lots d'autres projets (Nexlink), hors paquet ameesh |

Les autres lots ameesh en `qa` non retenus le sont pour conflit sans résolution fraîche sur cette
tête ou pour gel non couvert par un verdict ; ils restent candidats aux paquets suivants.

## Corrections dédiées (hors unités)

- `1d03884f9` — compteurs du contrat dans `docs/EXECUTEUR-MEDIEE.md` : après les unions, le contrat
  composé compte 65 opérations / **380 refusées / 445 opérations d'interface** (et non 377/442) ;
  `tests.test_l107_executor_contract` **22 OK psql + 22 OK psycopg**. Documentation seule.
- Commit suivant — `src/ameesh/storage/postgres/work.py`, **2 lignes** : `item_columns` (#2084) nomme
  `work_items` pour l'expression `reviewers`, alors que deux lectures bornées aliasent la table
  (`items_missing_ref` de #1104 et la passe des lots promus de #753) ; sous l'alias la requête échoue
  (`column "work_items" does not exist`) et `catch_up` avalait l'erreur : aucune relecture rattrapée
  (12 échecs `tests.test_1104_review_lot_hors_file` sur `1d03884f9`, verts sur la résolution seule).
  Correction : passer l'alias `w` à `item_columns` aux **deux** appels. Aucun changement de
  comportement hors génération SQL ; adaptation d'un contenu relu, un avis sur le delta final est
  demandé au pilote (§12). Après correction, `tests.test_1104_review_lot_hors_file` +
  `tests.test_753_stop_returns_lots` + `tests.test_1425_stop_sweep_suites` = **52 OK psql +
  52 OK psycopg**.

## Preuves

- `git diff --check` propre, aucun marqueur de conflit.
- Chaque unité est un merge `--no-ff` de la tête/remise/résolution exacte listée ; seconds parents
  conformes ; aucun `.github/**` hors le delta CI de #2224 (`carte.yml`), aucun fichier de porte de
  fusion (#2240/#2249 exclus).
- Tests ciblés sur la tête composée : sur `1d03884f9`, 19 modules = **464 tests**, 12 échecs **tous
  dans `tests.test_1104_review_lot_hors_file`** (défaut `item_columns` ci-dessus), aucun autre échec.
  Après la correction 2 lignes : `tests.test_1104_review_lot_hors_file` +
  `tests.test_753_stop_returns_lots` + `tests.test_1425_stop_sweep_suites` = **52 OK psql +
  52 OK psycopg** ; `tests.test_l107_executor_contract` = **22 OK psql + 22 OK psycopg** (compteurs).
- `scripts/paquet-inclusion.py` (manifeste × arbre, admissions lues dans ce manifeste) :
  **aucun défaut** ; 14 lots `présent`, 2 contenus `partiel` à lire — #2076 (`mesh_cli.py`, `work.py`
  et `PLAN-DE-TRAVAIL.md` réécrits par l'union de paire #2084/#800) et #2055 (`runner.py` réécrit par
  les unions #2242 et #1466) — supersessions attendues, aucune omission.
- Conflits résolus : `runner.py` (#1466, union `train_passages`/`cfg_overrides`), `work.py` (#1664,
  union `check_wait_note`/`once`), `storage/interface.py` (#1104, imports `re` + `datetime`),
  `docs/EXECUTEUR-MEDIEE.md` (#1104, compteurs du contrat dérivés puis corrigés en 380/445).

## CI et fusion

- **Une seule** CI complète sur la tête exacte du paquet, après le contrôle unitaire du pilote ;
  étiquette `ci-full-<sha>` posée par l'exécuteur sur le SHA exact, aucune CI de lot.
- Feu du pilote sur le SHA exact (`gh pr merge --auto --match-head-commit <sha>` ou repli
  `ameesh work merge-pr` — la PR touche `.github/workflows/carte.yml`, `gh pr merge` peut être
  refusé faute de scope `workflow`), fusion automatique au vert ; ne pas rebaser. Toute nouvelle
  tête annule le feu.
- Après fusion : rattachement des unités au merge réel et aux runs, clôture des lots par le runner,
  confirmation au pilote et au bureau. Aucune opération hôte ici.
