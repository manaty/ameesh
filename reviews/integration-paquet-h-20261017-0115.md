# Paquet integration/paquet-h-20261017-0115 — composition

Base : `main` après atterrissage de PR1541 (paquet h-2315) — merge
`700ca2f479f995c57790df3c1f459f66b5ef2923` ; branche
`integration/paquet-h-20261017-0115`. Exécuteur désigné : **mty-amh-impl-01**
(composition, CI et fusion), créneau confirmé par mesh-design 17/10 00:36Z
(« Créneau confirmé : h-20261017-0115 », liste retenue du même mail). Gels et avis
des lots conservés (contenus constants, §11bis/§12) ; aucune nouvelle relecture ;
aucune CI de lot ; h-2315/PR1541 non rouverte.

**Recomposition 2** (17/10 01:2xZ) : l'unité 1 (#508) est remplacée par la remise
corrigée `ffd910f7` après le rouge cluster de la première CI (section dédiée) ; les
18 autres unités sont inchangées, le reste du tableau est identique, seuls les
commits de composition sont neufs.

La colonne « commit de composition » porte la preuve d'inclusion : chaque lot a son
commit dans la branche (`git log --first-parent`).

## Admissions retenues à la coupure

Liste pilote (mail 00:36) : les 16 unités composées à `9e4024fbe`, plus #1914
`671da314b5fa` (GO indépendant Claude orchestrateur, N résiduel → backlog #2160),
#1944 `55d57565585a` (GO cx1 0B/0N sur cette tête) et #2136 `137059fc9d22`
(gel/GO/admission pilote du 02:06). #2034 reste hors tête (avis requis dus) ;
#1720 part au portage séparé (impl-03) ; #711 n'est pas ajouté — #2146 seul reste
`5ac900ae`. #2086 (`8dbc6c80`, admis 00:37Z) et #1612 (`70c9afa3`, admis le 16/10)
vont au véhicule suivant : le premier gel était posé avant ces courriers.

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #508 | `ffd910f7` (`refs/ameesh/resolutions/508/union-main-700ca2f4-cluster`) | `be094cff1` | Remise rafraîchie sur main `700ca2f4` par impl-07 ; **correctif cluster** (3 colonnes de la vue `agent_mesh_overview` accordées : `runner_seen_{at,host,owner}`), +5/−1 sans migration, `test_cluster` 34 OK ×2 ; porte l790 ; fusion propre |
| 2 | #1473 | `e577c85ace3f` (`refs/ameesh/resolutions/1473/main-fbb8eb58`) | `4758ffc35` | Remise FINALE ; union #1901 + #1473 conservée, migration 0457 additive, delta sémantique relu GO Codex natif |
| 3 | #1969 | `5e54f47beaf0` | `4c8e1959d` | Bascule atomique — raccord de composition fait ici (merge-tree propre × #1882), corrections alias Python + journal |
| 4 | #1882 | `a6bcbb011d6f` | `d3431d250` | Doc seule (`docs/ORCHESTRATEUR.md` §L165), GO auditeur/Codex, merge-tree rc=0 sur main |
| 5 | #2107 + #2108 | `b50a71af0b2f` | `d0aca029d` | Runner/mémoire : libellés neutres — verdict Claude dev7 0B/0N sur ce gel, 34/34 `test_2096` + 56 `l498` verts |
| 6 | #1464 | `2585fb7904ea` | `79c4457d2` | #793 suites : repère `process_commit` — re-gel auteur, R2 dev4 GO 0B/0N sur cette tête ; R1 cx2 borné à `f1aeb461e` |
| 7 | #1538 | `dc703c055e23` | `84490639e` | Une note de refus par (agent, élément) — GO natifs acquis (pilote 00:27) |
| 8 | #1508 | `5e84313df61d` | `fa59b5d5e` | Lot `#1508` (union #1508 × #1466 préparée par impl-04) — GO natifs acquis (pilote 00:27) |
| 9 | #2129 | `7491db3bf8` | `0d8eb22ca` | Alerte ligne d'hôte malformée — GO Codex 0B/N, témoin rouge→vert 7 cas |
| 10 | #2146 | `5ac900ae2d16` | `c14091a8c` | Radar migrations/remises — GO Codex natif + avis §11 bis ; #711 non inclus |
| 11 | #1161 | `b8ca19ca0a` | `2731d77cf` | GO Codex cx1 reconduit ; union `cancel`/`ttl`, bases/attentes |
| 12 | #1516 | `af35fafe5b` | `6b65e87f4` | GO Codex cx1 reconduit ; union classification `drop`/`skip` |
| 13 | doc C (#1915/#2130) | `fc5cd1b230f5` | `83b815669` | Doc seule (`[skip ci]`), transport UNIQUE chez impl-01 (décision pilote 23:26Z) |
| 14 | #1592 | `47cb7720a8` | `209a9be9a` | `dsh` codes de reprise — verdict Claude dev7 sur tête corrigée, admise |
| 15 | #1593 | `433e21c8302a` | `35815a6f7` | Remise mécanique (`remise/1593-main-fbb8eb581`), reçue auditeur 23:41Z |
| 16 | #1405 | `3ea21421c0f5` | `f2bb7f79a` | Cache GitHub/ressources — route légère décidée (35009/35047), relecture qa01 APRÈS fusion |
| 17 | #1944 | `55d57565585a` | `8f16fe222` | Liens d'affectation — GO Codex cx1 0B/0N ; correction test seule, zone `_PetitDepot` de #2151 préservée |
| 18 | #1914 | `671da314b5fa` | `e4aecf2ae` | Verrou de session visible (sensible, concurrence) — GO indépendant Claude orchestrateur ; N résiduel → backlog #2160 |
| 19 | #2136 | `137059fc9d22` | `b03ad3084` | `resolve` symétrique — gel/GO/admission pilote du 02:06, pas de nouvelle relecture |

## Recomposition 2 — rouge cluster de la première CI

Première CI du paquet (tête `5fd320938`, run PR `tests` 38987500146 ; le portier
`paquet` 38987474341, lancé par la poussée avant l'ouverture de la PR, a doublé la
suite puis a été annulé) : rouge sur `psql 5/9` et `psycopg 3/4` —
`test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue` : trois
colonnes `agent_mesh_overview.runner_seen_{at,host,owner}` ni accordées ni exclues
(grant posé sur `agent_registry`, oublié sur la vue `r.*`).

Correctif `ffd910f7` de mty-amh-impl-07 (mail urgent 01:23Z), reproduit de première
main sur la remise `7ffabc48` : `deploy/sql/role-superviseur.sql`, 2 lignes +
commentaire ; preuves auteur `tests.test_cluster` 34 OK ×2 et `merge-tree` × main
`700ca2f4` propre. L'unité 1 est recomposée sur cette remise ; le delta de la
recomposition contre l'ancienne composition `c597a2613` est ce seul fichier
(+5/−1), aucun autre conflit, aucune migration. La reconduction des GO dev4/cx3 est
demandée au pilote (SQL/sensible) ; la ref `union-main-700ca2f4` reste sur
`7ffabc48` pour la provenance.

## Retiré de ce véhicule

- **#800** (`4648e4e8cbd8`) — union **non mécanique** : trois conflits sémantiques
  avec l'état courant — `docs/PLAN-DE-TRAVAIL.md` (paragraphe réécrit par #1979),
  `src/ameesh/mesh_cli.py` (branche `--remove`/`--no-verdict` de #1979) et
  `src/ameesh/work.py` (`_review_item`). Ce n'est pas une union additive : la
  résolution demande une conception, pas un recollement. Exclusion nommée conforme à
  la consigne pilote 00:36 ; prochain véhicule compatible.
- **#1465** (remise `4433e5f9`, reçue 00:37 après la liste retenue) — non inclus,
  aucun élargissement après la liste de coupure ; candidat au prochain compatible
  (réserve connue : `test_cluster` 8 colonnes `provenance_receipts`, delta test-only
  cx3 dû).
- **#2034** (avis dus) et **#1900** (verdict Codex BLOQUÉ sur `33e40203`, correction
  de conception rendue à impl-03) — hors tête.
- **#2086** `8dbc6c80` et **#1612** `70c9afa3` — admis, mais après le premier gel :
  véhicule suivant, sans élargir ce paquet.

## Contrôles de composition

- 19 fusions, **aucun conflit** ; `git diff --check` propre ; aucun marqueur de
  conflit ; **aucune migration** ajoutée par la composition elle-même.
- Compteurs du contrat cohérents sur l'arbre composé : interface **432** = 65
  admises + 367 refusées, aucune opération non classée.
- Delta total vs `700ca2f47` : **62 fichiers, +4080/−316** (recomposition 2).
- Contrôle d'inclusion `scripts/paquet-inclusion.py --manifest … --head … --expected
  508,1473,1969,1882,2107,2108,1464,1538,1508,2129,2146,1161,1516,1592,1593,1405,1944,1914,2136`.

## Témoins ciblés sur l'arbre d'union (psql + psycopg)

`tests.test_l107_executor_contract` (22 OK ×2), `tests.test_l161e_map` (41 OK,
11 sauts ×2), puis `tests.test_l48_fast_failures` + `tests.test_l160d_resolutions` +
`tests.test_l107_executor_contract` (**75 OK ×2**) sur la composition 1 ; pour la
recomposition 2, `tests.test_cluster` (34 OK ×2, preuve mty-amh-impl-07, 3 colonnes
désormais accordées). Sauts environnementaux (« coverage requis »), sans rapport avec
le delta. Aucune CI de lot ; une seule CI complète sur la tête gelée du paquet (§12).

## Gel / CI / fusion

- Tête gelée et PR : `integration/paquet-h-20261017-0115` → `main` (PR #1550) ; base
  `main` `700ca2f47`.
- Une seule CI : run PR sur la tête exacte, après reconduction des GO par le pilote ;
  feu pilote sur cette tête ; fusion dès le vert (§11bis/§12),
  `gh pr merge --auto --match-head-commit` (aucun fichier `.github/workflows/**` au
  delta).
