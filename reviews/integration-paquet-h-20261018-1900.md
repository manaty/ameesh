# Paquet integration/paquet-h-20261018-1900 — composition

Base : `main` **`d9a6343fd9b6`** (fusion de la PR #1864, paquet h-1500,
18/10 17:09Z, CI complète `39144895213` verte sur `7d4467ec7`). Branche
`integration/paquet-h-20261018-1900`, cible `main`, **PR #1884**. Une seule
suite complète est demandée sur la tête de ce manifeste.

Demandes : pilote **mesh-design** 18/10 17:15Z (paquet suivant depuis
`d9a6343f`) et 17:37Z (urgent, h-1900) ; orchestrateur 17:15Z (SLO « review
GO → merge » : #2349, #377, #2389). Liste calculée `scripts/paquet-candidats.py`
sur la base `d9a6343f` : 5 lots prêts (`#2497`, `#2396`, `#2461`, `#2486`,
`#2502`) mais **4 erreurs de lecture de blobs** (`#2466`, `#2493`, `#2318`,
`#2395`, dépôt partiel) : elle n'a pas servi de base unique ; chaque tête
publiée, chaque gel et chaque verdict ont été relus sur les jalons du lot et
sur `origin` avant composition.

## Admissions (11 unités, 11 lots portés)

- Liste pilote 17:37Z : #2452, #1693, #2461, #2486, #2396, #2426, #2459,
  #1955, #2318, #2478, #2349, #377, #2389.
- **11 composés** : #2426, #2396, #2318, #1693, #2349, #2389, #377, #2478,
  #2486, #2461, #1955.
- #2452 et #2459 sont des lots du **canon** (`manaty/home`) : leurs branches
  `canon/2452-mecanique-train-reviews` et `canon/2459-ci-full-source` ne sont
  pas sur `origin` d'ameesh ; voir « Écartés ».

## Composition (11 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2426 | `8aa65fa94cc1` (tête gelée, re-gel après main d9a6343f) | `8adc8dcdf3e7` | Fusion de paquet : fermer ses lots unitaires (`package_id` nul, composition) ; 7 f. +720/−3 ; `runner.py` remis en union sur main d9a6343f ; verdict dev7 (Claude) ok ; sensible (porte de fusion) |
| 2 | #2396 | `fa34a257a02a` (tête gelée, re-gel après main d9a6343f) | `b627cdc49f55` | Radar migrations : préférer comme survivant le détenteur de la réservation ; 2 f. +119/−21 ; verdict cx3 (Codex) ok ; léger |
| 3 | #2318 | `4c27418aca06` (gel 5 sur main 4c73962e) | `1657e7129216` | Admission : un aller-retour réseau de moins, `fetch` autorisé une fois ; 3 f. +109/−17 ; verdicts cx2 (Codex) + dev2 (Claude) ok ; sensible |
| 4 | #1693 | `cd68cf6d62d4` (remise = gel `09d90d3c` × main `4c73962e`) | `e6fd751b06db` | Synchronisation GitHub : fermeture multi-étapes ; 7 f. +441/−31, migration **`0449_work_milestones_correction.sql` additive** (index unique restreint à `requested`, jalon `merged` effectif ; aucune ligne supprimée ni réécrite) ; verdicts dev2 (Claude) + cx3 (Codex) sur `09d90d3c` ; sensible (SQL) |
| 5 | #2349 | `4eeed0ef927f` (remise = gel `2d966185` × main `4c73962e`) | `b78586d12125` | `work fire` : comparaison de SHA par préfixe, `--repo` dossier ou `owner/name` ; 6 f. +127/−9 ; verdicts dev4 (Claude) + cx3 (Codex) sur `2d966185` ; sensible |
| 6 | #2389 | `cad4287cc3f6` (tête gelée) | `b15f53ca7eb6` | Le `no-land` du feu ameesh ne répète plus une ligne LOUD à chaque passage : dédup par `(lot, fire_sha)` ; 2 f. +102/−3 ; verdict dev4 (Claude) ok ; sensible |
| 7 | #377 | `b00875ee4d35` (tête gelée) | `6988f029cc45` | L86 : identité OIDC et autorisations ; 6 f. +1025 ; verdicts auditeur (Claude sur ce delta), dev4 (Claude) et cx2 (Codex) ok sur `b00875ee` — deux familles ; sensible |
| 8 | #2478 | `cd855884c407` (tête gelée) | `78764e60a8ff` | Désignation de relecteur : le relecteur désigné est réveillé (désignation manuelle et activation) ; 7 f. +224/−11 ; verdicts cx1 (Codex) + dev1 (Claude) ok ; sensible |
| 9 | #2486 | `2f6e7c7fca34` (tête gelée) | `e2861958161d` | Un rebase qui laisse de côté un commit déjà dans la cible ne perd pas le verdict ; 2 f. +163/−13 ; verdict cx1 (Codex) ok ; léger |
| 10 | #2461 | `b37163a475d5` (tête gelée) | `a081c9b1481d` | Release #2448 : bords N4-N6 (release vide, aucun vert, `--version` sous un tag) ; 3 f. +110/−16 ; verdict cx3 (Codex) ok ; léger |
| 11 | #1955 | `5733709f67c3` (tête gelée) | `adf47fc1814a` | N de la relecture de #423 : contrat médié 1.2.0 → 1.3.0 (`agents.resolve_name`), `exploitation.stop` rend le nom primaire, dorés conformes au générateur (comparaison octet pour octet) ; 10 f. +64/−18 ; verdicts dev2 (Claude) + cx3 (Codex) ok ; sensible |

Chaque commit de composition a le lot en second parent, son sujet nomme le
lot et son SHA, et il porte un trailer `ameesh-work: <lot>` (11 trailers de
composition).

## Conflits et résolutions

- **Aucune résolution** : les 11 sources se composent proprement dans l'ordre
  ci-dessus ; aucun `git merge` n'a laissé de conflit, aucun marqueur de
  conflit, `git diff --check` rc 0.
- Les deux remises (#1693 `cd68cf6d`, #2349 `4eeed0ef`) ont pour parents leur
  gel et main `4c73962e` ; leur delta net contre la base du paquet est celui du
  lot (441/7 f. et 127/6 f.), le reste étant le retard de main déjà porté par
  `d9a6343f`.
- #2426 et #2396 avaient déjà intégré main `d9a6343f` dans leur re-gel : leur
  merge-tree contre la base est direct.

## Écartés

| lot | motif |
|---|---|
| #2452 | Lot du **canon** (`manaty/home#111`, branche `canon/2452-mecanique-train-reviews` sur `manaty/home`) : absent d'ameesh, sa fusion est celle du canon, pas ce paquet |
| #2459 | Lot du **canon** (`manaty/home#114`, branche `canon/2459-ci-full-source`, cible `canon/2452-…`) : même motif, et empilé sur #2452 |
| #1349 | Pilote 17:37Z : avis manquants sur `2c66b493` |
| #2505 | Pilote 17:37Z : relecture en cours |
| #2497 | Pilote 17:37Z : incident (main rouge), voie à part avec sa CI de lot (PR #1870) |
| #2502 | Prêt au calcul 17:23Z mais dépend de #2497 (empilé sur `3697b3ff`, PR #1879) : à remettre sur main après la fusion de #1870, hors de ce paquet |

Les erreurs de lecture du calcul (#2466, #2493, #2395 ; #2318 depuis vérifié
et composé) ne sont pas des admissions : aucune tête n'a été composée sans
gel, verdict et merge-tree relus à la main.

## Preuves

- `scripts/paquet-compose.py --units --base d9a6343f` : **11 unités de fusion,
  11 lots portés** — #2426 `8aa65fa94cc1`, #2396 `fa34a257a02a`, #2318
  `4c27418aca06`, #1693 `cd68cf6d62d4`, #2349 `4eeed0ef927f`, #2389
  `cad4287cc3f6`, #377 `b00875ee4d35`, #2478 `cd855884c407`, #2486
  `2f6e7c7fca34`, #2461 `b37163a475d5`, #1955 `5733709f67c3`.
- Delta unités contre la base, manifeste exclu : **54 fichiers, +3204/−142**
  (`git diff --shortstat d9a6343f..HEAD`) ; `git diff --check` rc 0.
- Migrations : une seule, `0449_work_milestones_correction.sql` (#1693),
  additive et absente de main ; aucune autre `*.sql`.
- Liens de la documentation : `scripts/doc-links.py` — **170 fichiers,
  0 lien cassé**.
- Témoins ciblés sur l'arbre composé, deux pilotes (`scripts/test.sh`) :
  `test_2426_packet_close`, `test_2442_verdict_carry`,
  `test_l160e_merge_queue`, `test_2389_fire_no_land_once`, `test_1374_fire`,
  `test_705_reviewer_designated`, `test_1617_review_designation_pg`,
  `test_plan`, `test_progress`, `test_merged_work_item_tracking`, `test_oidc`,
  `test_423_agent_aliases`, `test_l107_executor_contract`,
  `test_2448_release_cutoff` — verdict au fil du lot.
- Suite complète : PR #1884, une seule exécution sur la tête finale de ce
  manifeste ; auto-merge armé `--match-head-commit`.

## Staging

Après fusion, le déploiement staging doit appliquer les migrations en attente
**avant** de relancer les exécuteurs : `0418_agent_aliases` et
`0464_spend_pending_resident_usage` (h-1300, toujours non appliquées) puis
`0449_work_milestones_correction` (ce paquet). Aucune migration destructive.
