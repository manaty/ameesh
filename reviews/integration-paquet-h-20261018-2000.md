# Paquet integration/paquet-h-20261018-2000 — composition (recomposition sur main après #2497)

Base : `main` **`7e6e3ed7aefe`** (fusion de la PR #1870, #2497, exception
incident pilote, 18/10 19:26Z). Branche
`integration/paquet-h-20261018-2000`, cible `main`, **PR #1902**. Une seule
suite complète est demandée sur la tête de ce manifeste.

Demandes : pilote 19:07Z — recomposer sur la nouvelle tête de `main` après
l'atterrissage de #2497, avec les 5 unités de h-2000, #2514, #2524, #2526,
#1349 si ses avis sont complets, et #2493 seulement si dev7 a rendu son avis
sur le delta cx2 `b7082bb8`. Bureau 19:26Z : #2497 est atterri sur `main`
`7e6e3ed7a` (PR #1870, tête `3697b3ff`, exception incident) — recomposer
**sans #2497**. Pilote 19:32Z : ajouter **#2502** (gel `25f5dfbb`, avis dev4,
dépendance #2497 satisfaite), oublié de la liste de 19:07Z ; #2493 reste
dehors ; #1349 reste dedans. L'ancienne tête `34ba6e5dc` (recomposition locale
avec #2497 comme unité) et son run sont abandonnés. #2493 est écarté
(condition non remplie).

## Admissions (10 unités, 10 lots portés)

- #2184, #2449, #2470, #2495, #2503 (les 5 unités de h-2000) ;
- #2514 (gel `e325e91d15e1`, verdict dev1), #2524 (gel `1cf918c41946`,
  verdicts dev2+impl-04), #2526 (gel `22bd1bbf7291`, verdicts cx1+dev4) ;
- #1349 (gel `167b4ffa6765`, verdicts dev4+impl-08 ; remise `dc7d3c45d8d1`) ;
- #2502 (gel `25f5dfbb2389`, verdict dev4 ; empilé sur #2497, dépendance
  satisfaite dans la base).
- #2497 est dans la base (`main` `7e6e3ed7a`), pas une unité du paquet.

#2493 est écarté avec son motif (voir « Écartés »).

## Composition (10 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2184 | `c06831f76398` (tête gelée) | `bcb834d80067` | Drain `vm.sh` hors tour robuste : N1 (agent déplacé ou mode changé pendant l'attente), N2 (pause refusée ne coupe plus le balayage), N3 (appels pause/levée/lecture bornés), N6 (acteur d'audit = opérateur) ; migration **`0459_operator_resume_if_non_execute.sql`** additive (échéance lue sous verrou, `SECURITY INVOKER`, `REVOKE PUBLIC`) et reprise conditionnelle. 11 f. +874/−85. Verdicts **cx3** (Codex) + **dev2** (Claude) ok sur `c06831f7` ; sensible (deploy/** + droits SQL) |
| 2 | #2449 | `32d6fab49322` (tête gelée) | `5b36aed4c4d3` | Garde CI : refuser un paramètre mécanique codé en dur hors du canon compilé (`scripts/garde-mecanique.py`, 22 clés, 10 exemptions nommées et datées) ; `ui.yml` ne tourne plus sur les PR de lot. 8 f. +1221/−17. Verdicts **dev7** (Claude) + **cx1** (Codex) ok sur `32d6fab4` ; sensible (deploy/**) |
| 3 | #2470 | `1fd3a967b313` (tête gelée) | `dcd6ec075f58` | `version-level.py` : `ON COMMIT DROP` d'une table temporaire n'est pas une rupture ; v1.11.6 se lit « minor needed ». 2 f. +14/−4. Verdict **mty-amh-qa-01** (DeepSeek) ok sur `1fd3a967` ; light |
| 4 | #2495 | `a7cc15b8b773` (tête gelée) | `4c6f560e383f` | Désignation de relecteur : un seul request par `(lot, relecteur, tête)`, un request ordinaire ne couvre pas une désignation ; message manuel sans promesse de nouveau gel. 6 f. +192/−8. Verdicts **cx2** (Codex) + **cx1** (Codex) ok sur `a7cc15b8` ; sensible (chemins review*) |
| 5 | #2503 | `1e9b7687d0de` (tête gelée) | `43f98c0ba858` | Contrat de CI de paquet Nexlink via lot-plan (mode ombre) : parts dues par la base et la tête, exceptions de plomberie exactes, job toléré rouge isolable. 7 f. +1199/−61. Verdicts **codex3** (Codex) R2 ok sur `4010c60d` + R3 delta ok sur `1e9b7687` ; light |
| 6 | #2514 | `e325e91d15e1` (tête gelée) | `df6dc567664e` | Incident CI main : les deux tests rouges (sonde LISTEN réémise, plafond mural 2,5 s) ne mesurent plus l'environnement ; `tests/test_actions.py` + `tests/test_l78_conversations.py`. 2 f. +17/−23. Verdict **dev1** (Claude) ok sur `e325e91d` ; light |
| 7 | #2524 | `1cf918c41946` (tête gelée) | `934331bb91ad` | Porte de fusion : `ci_green` lit les checks les plus récents de la tête (un run plus ancien rouge ne fige plus la PR), date d'un `StatusContext` lue là où `gh` la met ; `src/ameesh/radar.py` + `tests/test_2524_latest_ci_rollup.py`. 4 f. +157/−6. Verdicts **dev2** (Claude) + **impl-04** (DeepSeek) ok sur `1cf918c4` ; light |
| 8 | #2526 | `22bd1bbf7291` (tête gelée) | `239acce66a53` | `_orphan_lots` ne signale plus un lot `intake`/`build` retenu par une dépendance bloquante ouverte (même vérité que `lot_sessions.actionable`) ; `src/ameesh/exploitation.py` + `tests/test_2526_orphan_dependance.py`. 2 f. +100/−2. Verdicts **cx1** (Codex) + **dev4** (Claude) ok sur `22bd1bbf` ; sensible (exploitation.py) |
| 9 | #1349 | `dc7d3c45d8d1` (remise du gel `167b4ffa` sur `main` `7e6e3ed7a`) | `a9a217b8adab` | `ameesh agent peek <nom>` : résumé lisible du travail d'un agent, avertissement une fois par série ; migration **`0436_agent_peek.sql`** additive, rôle superviseur-contenus, `operations.json` doré. 22 f. +1070/−7. Verdicts **dev4** (Claude) + **impl-08** (DeepSeek) ok sur le gel `167b4ffa` ; sensible (droits SQL + serve) ; la remise ne change que la résolution des conflits avec main (`docs/EXPLOITATION.md`, `tests/test_l107_executor_contract.py`), mécanique ; correctifs d'attribution CI +4/−2 et d'ordre de l'aide +1/−1 (voir « Correctif de composition ») |
| 10 | #2502 | `25f5dfbb2389` (tête gelée, empilée sur #2497) | `2d1b948f7f8e` | N de la relecture de #2497 : `ARRETS_DE_BAIL` resserré (« database unreachable » ne couvre plus le diagnostic non définitif de `renew`) ; `tests/test_runner.py`. 1 f. +43/−2 après #2497. Verdict **dev4** (Claude) ok sur `25f5dfbb` ; light ; dépendance #2497 satisfaite dans la base |

Chaque commit de composition a le lot en second parent, son sujet nomme le
lot et son SHA, et il porte un trailer `ameesh-work: <lot>` (10 trailers de
composition).

## Correctif de composition

- **Attribution #1632** (échec `test_l161e_map.AttributionContractTest` du
  run 39159851465) : `tests/test_1349_agent_peek.py` importait
  `ServeurExecTest`, classe concrète de `test_l108_executor_server`, au niveau
  module ; le contrat d'attribution refuse ce lien. Le fichier lie désormais le
  module (`from tests import test_l108_executor_server as _exec_server`) et
  prend `_exec_server.ServeurExecTest` comme base ; `OWNER` reste un alias de
  constante. **+4/−2**, adaptation de test mécanique, aucun avis (§2/§11 bis).
  Rejoué : `test_l161e_map.AttributionContractTest...` + `test_1349_agent_peek`,
  **19 tests OK psql+psycopg**.
- **Ordre de l'aide** (échec
  `test_2179_roadmap_cli.RoadmapHelpTest.test_root_help_is_alphabetical_and_keeps_routes_visible`
  du run 39161059742, psycopg 5/8 et psql 6/16) : `agent peek` (#1349, commit
  `e6c1118228`, `[skip ci]`) avait été inséré après `agent spawn` dans la liste
  alphabétique de `src/ameesh/main.py`. Les deux lignes sont remises dans
  l'ordre (`peek` avant `spawn`). **+1/−1**, correctif mécanique, aucun avis
  (§2/§11 bis). Rejoué : `tests.test_2179_roadmap_cli.RoadmapHelpTest`,
  **3 tests OK**.

## Conflits et résolutions

- **Aucune résolution dans le paquet** : les 9 sources se composent proprement
  sur `7e6e3ed7a`, aucun `git merge` n'a laissé de conflit, aucun marqueur.
- #2497 est dans la base (`main` `7e6e3ed7a`, PR #1870) et n'est pas une
  unité ; #2502 est empilé sur #2497, composé après #1349 : son merge-base est
  `3697b3ff`, la fusion ne reprend que son delta d'un fichier.
- #1349 est une remise qui a déjà résolu ses conflits avec `main`
  (`docs/EXPLOITATION.md`, `tests/test_l107_executor_contract.py`) au commit
  `30c689175` ; sa fusion dans le paquet est propre.
- #2184 et #2449 touchent tous deux `docs/EXPLOITATION.md` : fusion
  automatique de git, sans intervention ; `git diff --check` rc 0.
- #2514, #2524 et #2526 touchent des fichiers disjoints des cinq premières
  unités (`tests/test_actions.py`, `tests/test_l78_conversations.py`,
  `src/ameesh/radar.py`, `src/ameesh/exploitation.py`) : aucune résolution.
- #2495 est empilé sur #2478 (`cd855884`), déjà dans `main` par h-1900 : son
  merge-base est `cd855884`, la fusion ne reprend que son delta de 6 fichiers.
- #2503 et #2470 ont pour merge-base `d9a6343f` (h-1500) : `main` a avancé
  depuis (h-1900, #2497), la fusion ne reprend que leur delta propre.

## Écartés

| lot | motif |
|---|---|
| #2493 | Condition du pilote non remplie : **dev7 n'a pas rendu d'avis sur le delta cx2 `b7082bb8`** (lot en attente de verdict dev7 ; tête publiée `b7082bb8`, gel `d010ea5d`). Le lot revient au prochain paquet dès cet avis. |
| #2443 | Gel refusé (`freeze-after-verdict`) et conflit `docs/ORCHESTRATEUR.md`, `src/ameesh/plan_cli.py` : à regeler/résoudre. |
| #2505 | 1 avis sur 2 requis sur `7e058770` (classe sensible critique). |
| #2512 | 1 avis sur 2 requis sur `32257e4b` (classe sensible). |
| #2520 | 1 avis sur 2 requis sur `4f2519bf` (classe sensible) ; cx3 ok, 2e famille attendue. |
| #2466 | 1 avis sur 2 requis sur `abc2fbf7` (classe sensible) ; prépare sa résolution avec ce paquet (`docs/EXECUTEUR-MEDIEE.md`). |
| #1398 | 1 avis sur 2 requis + conflits (openapi, `main.py`, `runner.py`, `serve.py`, …) ; conflit `docs/EXPLOITATION.md` hérité de #1349, pas du delta de ce paquet. |
| #2452 / #2459 / #2455 / #2458 | Lots du **canon** (`manaty/home`) : branches absentes d'ameesh, cible `canon/…` — hors de ce paquet. |
| #2481 / #2488 / #2395 / #2473 / #2317 / #2457 | Avis requis manquants sur la tête gelée (classe sensible critique) : en attente de relecture. |
| #2496 / #879 / #2487 / #1484 / #1022 | Gel refusé (verdict bloquant courant ou `freeze-after-verdict`) et/ou conflit avec `main` : correctifs ou résolutions dus. |

Tous les lots **prêts** de la liste calculée sont composés ; #2493, demandé
sous condition par le pilote, est le seul lot attendu écarté.

## Preuves

- `scripts/paquet-compose.py --units --base 7e6e3ed7a --head HEAD` : **10
  unités de fusion, 10 lots portés** — #2184 `c06831f76398`, #2449
  `32d6fab49322`, #2470 `1fd3a967b313`, #2495 `a7cc15b8b773`, #2503
  `1e9b7687d0de`, #2514 `e325e91d15e1`, #2524 `1cf918c41946`, #2526
  `22bd1bbf7291`, #1349 `dc7d3c45d8d1`, #2502 `25f5dfbb2389`.
- Delta composition + correctif contre la base, manifeste exclu : **62 fichiers, +4889/−215**
  (`git diff --shortstat 7e6e3ed7a..HEAD`) ; `git diff --check` rc 0.
- Migrations : deux ajoutées par le paquet, toutes deux **additives** —
  `0459_operator_resume_if_non_execute.sql` (#2184) et `0436_agent_peek.sql`
  (#1349). `ameesh migrate --dry-run --additive-only` les lit « additive ».
- Liens de la documentation : `scripts/doc-links.py` — **170 fichiers,
  0 lien cassé**.
- Témoins ciblés sur l'arbre composé (`scripts/test.sh`, pilotes psql et
  psycopg) : bail perdu (`RaisonsDArretDeBailTest`, `test_bail_perdu_interrompt_le_tour`,
  `test_branche_bail_perdu_ne_perd_pas_la_consigne`), `test_actions.ActionsCycleTest`,
  `test_l78_conversations.StoreTest`, `test_2524_latest_ci_rollup`,
  `test_2526_orphan_dependance`, `test_1349_agent_peek`,
  `test_l107_executor_contract`, `test_l84_serve`, `test_2449_garde_mecanique`,
  `test_2503_train_contract_nexlink`, `test_l158_version_level`,
  `test_2404_paquet_candidats`, `test_1644_paquet_inclusion` — **verts sur
  les deux pilotes** (psql et psycopg), aucun échec.
- **Couverture du plan** : le job `plan` du run 39157311081 est **vert**
  (19:08:48Z), malgré des notices sur `Runner.radar_once` / `Runner.fire_once`.
  La carte `996f718050e6` date du 17/10 16:47 (26,3 h) ; ces deux fonctions
  ont été changées sur `main` après la carte (#2212 `fresh_radar`, #2389
  `no-land`), pas par ce paquet : le diff `runner.py` de #1349 ne touche que
  `_close_peek` / `ensure_peek`, et `test_2212_fire_fresh_radar` +
  `test_2389_fire_no_land_once` les exécutent. Le pilote (19:32Z) confirme :
  `radar_once`/`fire_once`/`branch_sweep_once` ont la même empreinte à
  `e94d2b6e` et `34ba6e5d`, la liste vient de la carte `996f7180` (backlog
  #2538) ; #1349 reste dans le paquet. Le plan émet des notices, il n'échoue
  pas.
- Contrôle d'inclusion : `scripts/paquet-inclusion.py --manifest
  reviews/integration-paquet-h-20261018-2000.md --head HEAD
  --ready-file "$TMPDIR/prets-2000-r3b.txt"
  --expected 2184,2449,2470,2495,2503,2514,2524,2526,1349,2502,2493` — 10
  compositions, 0 retrait, 10 lots attendus (9 contenus « présent », #1349
  « partiel » attendu : correctifs de composition +5/−3 (attribution +4/−2,
  ordre de l'aide +1/−1)), 4 lots prêts lus
  (#2514, #2470, #2503, #2502), 1 exclusion consignée (#2493) ; **verdict :
  aucun défaut**.

## Staging

Le déploiement staging doit appliquer les migrations en attente **avant** de
relancer les exécuteurs : **0418, 0419, 0435, 0436 (ce paquet), 0449, 0450,
0459 (ce paquet), 0464**. `ameesh migrate --dry-run --additive-only` les lit
toutes « additive » ; aucune migration destructive.
