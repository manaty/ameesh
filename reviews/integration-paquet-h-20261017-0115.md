# Paquet integration/paquet-h-20261017-0115 — composition

Base : `main` après atterrissage de PR1541 (paquet h-2315) — merge
`700ca2f479f995c57790df3c1f459f66b5ef2923` ; branche
`integration/paquet-h-20261017-0115`. Exécuteur désigné : **mty-amh-impl-01**
(composition, CI et fusion), créneau confirmé par mesh-design 17/10 02:36
(« Créneau confirmé : h-20261017-0115 », liste retenue du même mail). Gels et avis
des lots conservés (contenus constants, §11bis/§12) ; aucune nouvelle relecture ;
aucune CI de lot ; h-2315/PR1541 non rouverte.

La colonne « commit de composition » porte la preuve d'inclusion : chaque lot a son
commit dans la branche (`git log --first-parent`).

## Admissions retenues à la coupure

Liste pilote (mail 00:36) : les 16 unités composées à `9e4024fbe`, plus #1914
`671da314b5fa` (GO indépendant Claude orchestrateur, N résiduel → backlog #2160),
#1944 `55d57565585a` (GO cx1 0B/0N sur cette tête) et #2136 `137059fc9d22`
(gel/GO/admission pilote du 02:06). #2034 reste hors tête (avis requis dus) ;
#1720 part au portage séparé (impl-03) ; #711 n'est pas ajouté — #2146 seul reste
`5ac900ae`.

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #508 | `7ffabc48bea5` (`refs/ameesh/resolutions/508/union-main-700ca2f4`) | `1f6f1a935` | Remise rafraîchie sur main `700ca2f4` par impl-07 ; porte l790 ; fusion propre |
| 2 | #1473 | `e577c85ace3f` (`refs/ameesh/resolutions/1473/main-fbb8eb58`) | `0b7fab583` | Remise FINALE ; union #1901 + #1473 conservée, migration 0457 additive, delta sémantique relu GO Codex natif |
| 3 | #1969 | `5e54f47beaf0` | `b33a3bd68` | Bascule atomique — raccord de composition fait ici (merge-tree propre × #1882), corrections alias Python + journal |
| 4 | #1882 | `a6bcbb011d6f` | `248ab26c0` | Doc seule (`docs/ORCHESTRATEUR.md` §L165), GO auditeur/Codex, merge-tree rc=0 sur main |
| 5 | #2107 + #2108 | `b50a71af0b2f` | `a97db92c4` | Runner/mémoire : libellés neutres — verdict Claude dev7 0B/0N sur ce gel, 34/34 `test_2096` + 56 `l498` verts |
| 6 | #1464 | `2585fb7904ea` | `d7b0e03af` | #793 suites : repère `process_commit` — re-gel auteur, R2 dev4 GO 0B/0N sur cette tête ; R1 cx2 borné à `f1aeb461e` |
| 7 | #1538 | `dc703c055e23` | `d1694caaa` | Une note de refus par (agent, élément) — GO natifs acquis (pilote 00:27) |
| 8 | #1508 | `5e84313df61d` | `e9bcc66ef` | Lot `#1508` (union #1508 × #1466 préparée par impl-04) — GO natifs acquis (pilote 00:27) |
| 9 | #2129 | `7491db3bf8` | `70557ded3` | Alerte ligne d'hôte malformée — GO Codex 0B/N, témoin rouge→vert 7 cas |
| 10 | #2146 | `5ac900ae2d16` | `9a076122e` | Radar migrations/remises — GO Codex natif + avis §11 bis ; #711 non inclus |
| 11 | #1161 | `b8ca19ca0a` | `ac872404e` | GO Codex cx1 reconduit ; union `cancel`/`ttl`, bases/attentes |
| 12 | #1516 | `af35fafe5b` | `f56448792` | GO Codex cx1 reconduit ; union classification `drop`/`skip` |
| 13 | doc C (#1915/#2130) | `fc5cd1b230f5` | `89da3939e` | Doc seule (`[skip ci]`), transport UNIQUE chez impl-01 (décision pilote 23:26Z) |
| 14 | #1592 | `47cb7720a8` | `abde372dd` | `dsh` codes de reprise — verdict Claude dev7 sur tête corrigée, admise |
| 15 | #1593 | `433e21c8302a` | `31994332b` | Remise mécanique (`remise/1593-main-fbb8eb581`), reçue auditeur 23:41Z |
| 16 | #1405 | `3ea21421c0f5` | `9e4024fbe` | Cache GitHub/ressources — route légère décidée (35009/35047), relecture qa01 APRÈS fusion |
| 17 | #1944 | `55d57565585a` | `3152af12b` | Liens d'affectation — GO Codex cx1 0B/0N ; correction test seule, zone `_PetitDepot` de #2151 préservée |
| 18 | #1914 | `671da314b5fa` | `72c71dbcd` | Verrou de session visible (sensible, concurrence) — GO indépendant Claude orchestrateur ; N résiduel → backlog #2160 |
| 19 | #2136 | `137059fc9d22` | `c597a2613` | `resolve` symétrique — gel/GO/admission pilote du 02:06, pas de nouvelle relecture |

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

## Contrôles de composition

- 19 fusions, **aucun conflit** ; `git diff --check` propre ; aucun marqueur de
  conflit ; **aucune migration** ajoutée par la composition elle-même.
- Compteurs du contrat cohérents sur l'arbre composé : interface **432** = 65
  admises + 367 refusées, aucune opération non classée.
- Delta total vs `700ca2f47` : **62 fichiers, +4075/−315**.
- Contrôle d'inclusion `scripts/paquet-inclusion.py --manifest … --head … --expected
  508,1473,1969,1882,2107,2108,1464,1538,1508,2129,2146,1161,1516,1592,1593,1405,1944,1914,2136`.

## Témoins ciblés sur l'arbre d'union (psql + psycopg)

`tests.test_l107_executor_contract` (22 OK ×2), `tests.test_l161e_map` (41 OK,
11 sauts ×2), puis `tests.test_l48_fast_failures` + `tests.test_l160d_resolutions` +
`tests.test_l107_executor_contract` (**75 OK ×2**). Sauts environnementaux
(« coverage requis »), sans rapport avec le delta. Aucune CI de lot ; une seule CI
complète sur la tête gelée du paquet (§12).

## Gel / CI / fusion

- Tête gelée et PR : `integration/paquet-h-20261017-0115` → `main` ; base `main`
  `700ca2f47`.
- Une seule CI : run PR sur la tête exacte ; feu pilote demandé sur cette tête ;
  fusion dès le vert (§11bis/§12), `gh pr merge --auto --match-head-commit` (aucun
  fichier `.github/workflows/**` au delta).
