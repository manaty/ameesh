# Paquet integration/paquet-h-20261018-2300 — composition (§12)

Base : `main` **`9a0b4f1ce89f`** (release 1.12.0 + note #2076, sur la tête
de h-2000 `e5aac6a05`). Branche
`integration/paquet-h-20261018-2300`, cible `main`, **PR #1929**. Une seule
suite complète est demandée sur la tête de ce manifeste.

Demandes : pilote mesh-design 23:2x et 23:3x — paquet suivant sur
`main 9a0b4f1ce`, en tête la paire **#2544 + #2537** (incident main rouge),
puis les lots prêts #2487+#2541 ensemble, #2496 si l'union est propre sur
`9a0b4f1ce`, #2473, #2493, #2521, #2550. Pilote 23:4x — **écarter** #2520,
#2443, #2511, #1398, #2538 et #2539 (la porte des avis exige deux avis sur
ces chemins et un pilote ne la contourne pas par jugement à la composition ;
l'écart canon/code est à signaler, pas à trancher ici) ; embarquer #2457,
#2542, #2543 et #2317 s'ils passent `paquet-candidats` et `merge-tree`, #2457
seulement si sa dépendance #2449 est en base, #2317 avec sa migration 0463
inscrite au manifeste et appliquée avant la relance des exécuteurs ; #2452
reste hors paquet (PR canon `manaty/home#111`, fusion bureau) ; #2546 reste
hors paquet (verdict dev2 encore attendu, prochain paquet).

## Admissions (13 unités, 13 lots portés)

- #2544, #2537 (paire : l'incident main et son témoin) ;
- #2487, #2541, #2496, #2473, #2493, #2521, #2550 ;
- #2457, #2542, #2543, #2317 (prêts calculés, conditions du pilote remplies).

## Composition (13 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2544 | `4d488ff34441` (tête gelée) | `34a66f9875a7` | Incident CI main rouge : `test_bail_perdu_interrompt_le_tour` (le tour ne démarre pas, battement tardif) et `test_branche_bail_perdu_ne_perd_pas_la_consigne` ; `runner.py` + témoin `test_runner.py`, 3 f. +62/−6. Verdict **ameesh-dev4** (Claude) ok sur `4d488ff3` ; sensible, 1 avis |
| 2 | #2537 | `cebffb0e88d6` (tête gelée) | `114c5c720a2a` | Témoin du bail perdu : la restauration est lue après la sortie de `--once`, sans course avec la reprise ; `test_runner.py`, 1 f. +20/−7. Verdict **ameesh-cx2** (Codex) ok sur `cebffb0e` ; light |
| 3 | #2487 | `dfda31e15a1f` (tête gelée) | `e4c7a6372266` | Désignation au gel : un gel réaligné dont les verdicts sont repris garde les relecteurs du gel précédent ; désignation et gel, 7 f. +1321/−93. Verdicts **ameesh-cx1** (Codex) + **ameesh-dev1** (Claude) ok sur `dfda31e1` ; sensible |
| 4 | #2541 | `3a3f42957bf8` (tête gelée, empilé sur #2487) | `3ac76939e2b3` | Rejeu force du même gel : les courriels de sélection/attente ne sont plus doublés (filtre boîte lot+gel) ; 3 f. +173/−6. Verdicts **ameesh-cx1** + **ameesh-dev1** ok sur `3a3f4295` ; sensible |
| 5 | #2496 | `4a9275dcbd36` (remise union sur `9a0b4f1ce`) | `a2654be0a8da` | Remise d'une union mécanique `merge_queue.py` (ajouts adjacents `CRITICAL_SCOPE`) recalculée contre `9a0b4f1ce` : `merge-tree` rc 0, ciblés 2404/2394/2265 67 OK au gel. Verdicts **ameesh-cx2** + **mty-amh-impl-06** ok sur le gel `9cc95551` ; sensible |
| 6 | #2473 | `04f5826cb1d5` (tête gelée) | `7e2128f08c1a` | `work fire` readiness : checkout du lot validé, origine comparée, repli `radar.projects`, branche poussée depuis un worktree supprimé ; `fire.py` + témoin, 2 f. +227/−5. Verdicts **ameesh-cx1**, **ameesh-cx3**, **ameesh-dev2** ok sur `04f5826c` ; sensible |
| 7 | #2493 | `b7082bb87f15` (tête gelée) | `af40c439955f` | Ménage tmp : constat des orphelins dédupliqué et reprise sûre des copies Git (sans suppression automatique) ; `housekeeping`/`exploitation` + témoins, 7 f. +499/−29. Verdicts **ameesh-cx2** + **ameesh-dev7** ok sur `b7082bb8` ; sensible |
| 8 | #2521 | `ecdfc971c264` (tête gelée) | `eda977de64b3` | `paquet-candidats` : une lecture du registre impossible est une erreur de lecture (`exit 1`, `--ready-out` supprimé), plus jamais `verdicts-missing` ; 6 f. +150/−11. Verdicts **ameesh-cx3** + **ameesh-dev4** ok sur `ecdfc971` ; sensible |
| 9 | #2550 | `a6df9346eb4d` (tête gelée) | `92fa628025ee` | Revue après fusion #2503 : correctif N du contrat de CI de paquet Nexlink, témoin ajouté ; 2 f. +24/−7. Verdicts **ameesh-cx3** + **ameesh-cx1** ok sur `a6df9346` ; sensible |
| 10 | #2457 | `85b5becfd878` (tête gelée) | `4e6bc8a360f0` | Garde mécanique étendue aux runners non-Python (shell, unités systemd `OnCalendar`, cron de workflow) ; `scripts/garde-mecanique.py` + témoins, 3 f. +282/−9. Verdict **ameesh-dev1** (Claude) ok sur `85b5becf` ; dépendance #2449 satisfaite dans la base h-2000 ; light/sensible selon chemins, 1 avis |
| 11 | #2542 | `36f0084c7d64` (tête gelée) | `69553fec9ed7` | Gel de lot : les contrôles statiques #1632 passent avant le gel (`scripts/freeze-checks.py`, `freeze_checks.py`, CLI + témoin), 6 f. +570/−2. Pas de verdict exigé (lot léger, 0 avis) ; **conflit `src/ameesh/mesh_cli.py` résolu** (voir « Correctif de composition ») |
| 12 | #2543 | `830d5537dfdb` (tête gelée) | `9ca693d5a2d4` | Balayage des branches : une pointe ancêtre hors chaîne première-parente ne suffit plus sans preuve de travail propre ; `plan_git.py` + témoins, 3 f. +84/−10. Verdict **ameesh-cx2** (Codex) ok sur `830d5537` ; sensible, 1 avis |
| 13 | #2317 | `ea9994e72ecc` (tête gelée) | `46a87dc8d616` | Jalon `merged` effectif : N1-N7 (horloge, note libre, scan du journal, lecteurs restants) ; migration additive **`0463_work_milestones_effective_merge.sql`**, 10 f. +350/−68. Verdicts **codex3** (Codex) + **ameesh-dev2** (Claude) ok sur `ea9994e7` ; SQL, 2 avis |

Chaque commit de composition a le lot en second parent, son sujet nomme le
lot et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Correctif de composition

- **#2542 / `src/ameesh/mesh_cli.py`** : la branche #2542 (base antérieure)
  réécrivait le bloc `work milestone` sans le paramètre `outcome` de #2487.
  Résolution : on garde `work.milestone(..., outcome=outcome)` de la tête du
  paquet (la désignation au gel reste portée par #2487, avec son repli
  `verdict_carry`) et on ajoute le contrôle statique `freeze_checks.before_freeze`
  de #2542, avec le refus `--repo`/`--no-static-checks` hors gel. Adaptation
  mécanique de composition, aucun avis nouveau (§2/§11 bis) ; les témoins
  #2542 et #2487 sont dans les tests ciblés.
- **#2496** : la remise d'union `4a9275dc` (parents gel `9cc95551` +
  `7e6e3ed7`) est recalculée par `git merge-tree` contre `9a0b4f1ce` :
  **rc 0**, aucune résolution manuelle nouvelle ; la remise est la source de
  l'unité.
- Aucun autre conflit : les 12 autres unités se composent proprement.

## Contenu et contrôles

- 52 fichiers, **+4337/−256** hors manifeste, sur `main 9a0b4f1ce` (après les
  correctifs CI ci-dessous).
- Une migration additive : `0463_work_milestones_effective_merge.sql`
  (#2317, table interne, déclencheur, aucune rupture) — à appliquer en staging
  **avant** de relancer les exécuteurs (`deploy/mise-a-jour/`, §11 bis). #2443
  (0465) est écarté de ce paquet.
- `paquet-inclusion` : 13 compositions, 13 lots attendus, 13 contenus
  présents ou à lire ; #2487 est signalé « partiel » sur
  `docs/design/etudes/1617-designation-relecteurs.md` (25/29 lignes) et
  `src/ameesh/review_designation_pg.py` (12/13 lignes) parce que #2541
  réécrit les lignes adjacentes du même chantier — adaptation de composition
  voulue, couverte par les témoins #2487/#2541 ; aucun défaut bloquant.
- Tests ciblés de composition (psql+psycopg) : consignés dans la note du
  véhicule #2558 avant la demande de suite complète. La suite complète du
  paquet reste la qualification de l'arbre combiné.
- #2452 est une PR du canon `manaty/home#111` (branche
  `canon/2452-mecanique-train-reviews`), hors de `main` ameesh : elle reste
  hors paquet, fusion bureau.

## Correctifs CI (suite complète)

- **`work.corrections` (#2317)** : l'unité ajoute la lecture
  `WorkItems.corrections` (N2/N4) sans la classer dans
  `src/ameesh/mediated_executor/contract.json` ;
  `tests.test_l107_executor_contract.TableTest.test_l_interface_entiere_est_classee`
  échoue sur les deux familles (psycopg 8/8 et psql 7/16) : `'work.corrections'`
  manque à `admises | refusées`. Correctif de composition : la lecture rejoint
  les refusées du domaine `work` (hors du sous-ensemble de l'exécuteur médié,
  comme `work.milestones`), et les deux compteurs du doc suivent (395 refusées,
  interface 462). Aucune opération admise ni schéma modifié : adaptation
  mécanique, sans avis nouveau (§2/§11 bis). `tests.test_l107_executor_contract`
  : 22 tests OK en local.
- **`test_2457_garde_non_python` (#2457)** : l'unité ajoute le module à
  `tests/parts/toujours.txt` sans l'ajouter à la liste attendue de
  `tests.test_l161f_plan.MapTest.test_always_list_sorted_and_known` ; la suite
  complète échoue sur les deux familles (psycopg 5/8 et psql 13/16). Correctif
  de composition : le module rejoint la liste attendue, à sa place triée.
  `tests.test_l161f_plan` : 160 tests OK (2 sautés) en local, dont le témoin
  #2457.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2452 | PR `manaty/home#111` (canon home), pas une unité de `main` ameesh ; fusion par le bureau |
| #2520 | écarté par le pilote 23:4x : la porte des avis exige 2 avis sur `.github/**`, un seul acquis ; second avis à désigner, prochain paquet |
| #2443 | écarté par le pilote 23:4x : 2 avis exigés sur migration `0465`/`storage/**`, un seul acquis ; prochain paquet |
| #2511 | écarté par le pilote 23:4x : 2 avis exigés sur `exploitation.py`, un seul acquis ; prochain paquet |
| #1398 | écarté par le pilote 23:4x : 2 avis exigés sur `storage/**`, un seul acquis (dev4) ; prochain paquet |
| #2538 | écarté par le pilote 23:4x : 2 avis exigés sur `scripts/test*`, un seul acquis ; prochain paquet |
| #2539 | écarté par le pilote 23:4x : une seule famille d'avis + conflit `docs/EXECUTEUR-MEDIEE.md` ; prochain paquet |
| #2546 | prêt calculé mais renvoyé au paquet suivant par le pilote (verdict dev2 attendu) |
| #2502 | déjà dans la base (`main` h-2000), pas une unité |

Les lots prêts calculés absents de la composition sont donc tous nommés
ci-dessus avec un motif ; aucun n'est omis en silence (#2404).
