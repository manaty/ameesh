# Paquet integration/paquet-h-20261016-1900 — composition r2 (mty-amh-impl-01, 16/10/2026 20:4xZ)

Recomposition après la CI rouge du run `38955155239` (psycopg 1/4, job
`117156289446`) sur l'ancienne tête `916b0c28428c0c2c63795a7f74054cdd4ccfc587` :
retrait des lots fautifs sur attribution de première main du composeur
(courrier bureau 19:46Z, arbitrage pilote mesh-design 19:48Z/19:50Z/19:51Z).
Base : `origin/main` `edf2bf3d`, arbre identique à la tête r2
`d256b6823da0c118deb13c5711b6d9f3a7d7af4a` (PR1500 fusionnée). Branche :
`integration/paquet-h-20261016-1900`. Tête de composition (avant ce manifeste) :
`3ab4576c8`. Exécuteur désigné : **mty-amh-impl-01** (composeur, CI et fusion).
Unités incluses : 12. Gels et avis des lots conservés (contenus constants,
§11bis/12) ; aucune nouvelle relecture.

La colonne « commit de composition » porte la preuve d'inclusion : chaque lot a
son commit dans la branche (`git log edf2bf3d..3ab4576c8`). Les résolutions d'union sont
signées dans le message du commit correspondant.

## Admissions retenues à la coupure

#2096, #1906, #974, #2110, #1625, #971, #1286, #1641, #2023, #910, #2116, #1632

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2096 | `4b8ca5faf93e0f0a7af91e22172c99cce96f837e` | `52f37c933` | Runner : ~800 Mo de mémoire par processus dès le démarrage (15 runners ≈ 11 Go sur pc-smi… — mty-amh-qa-01, verdict ok de ameesh-dev4 et mesh-design |
| 2 | #1906 | `39fabfe6b46b` | `2d1ae508c` | #1878 N (R dev4) : test-plan — module sys/os nommé par walrus ((s := sys).exit(), getattr… — mty-amh-qa-01, verdict ok de ameesh-dev4 |
| 3 | #974 | `7c0b34b1` | `928408795` | #364 suite : un inventaire anterieur sans source_bytes ne se restaure plus (N bureau R2) — mty-amh-impl-03, verdict ok de ameesh-dev7 |
| 4 | #2110 | `52b771822` | `026c4842c` | Runner : borner les lectures de events.jsonl par CostBook (621 Mo de pic sur un journal d… — mty-amh-impl-05, verdict ok de mesh-design |
| 5 | #1625 | `bb821753` | `28fc91bf2` | Prise automatique : un lot sans assigne qu'une attente retient bloque encore la prise d'a… — ameesh-dev2, verdict ok de mesh-design |
| 6 | #971 | `ad52f872` | `23dc486cc` | TEST-FLAKE : test_panne_de_base_definitive_tue_avant_tout_journal — un log asynchrone en… — mty-amh-impl-06, verdict ok de ameesh-cx1 |
| 7 | #1286 | `f0ce1760` | `6a03e6674` | work merged : le SHA declare n'est rattache a aucun lot — un numero de PR homonyme ferme… — mty-amh-impl-06, verdict ok de mty-amh-impl-02 |
| 8 | #1641 | `8d539655` | `e3a226342` | #498 : dossier cgroup etranger vide laisse si l'ecriture de cgroup.procs echoue — mty-amh-impl-06, verdict ok de ameesh-dev4 |
| 9 | #2023 | `39b5dea5` | `96a0e7018` | L232b : audit du rôle contrôleur sans séquences ; K7 refuse des cibles libpq équivalentes — deepseek4, verdict ok de ameesh-cx3 et ameesh-dev2 |
| 10 | #910 | `39b5dea5` | `96a0e7018` | L232b suite : entree operateur d'installation du controleur et enrolement turn.json — mty-amh-impl-03, verdict ok de codex3 et ameesh-cx3 |
| 11 | #2116 | `4f5fc757` | `1ce07e389` | CI : suspendre les mesures carte automatiques pendant l’incident de quota artefacts — ameesh-cx1, verdict ok de ameesh-dev4 |
| 12 | #1632 | `f6fbd083` | `f8eac87cb` | #1341 suite : couverture stricte de la carte — un module qui importe un TestCase d'un aut… — mty-amh-impl-08, verdict ok de ameesh-dev4 |

## Retirés de ce véhicule (diagnostic reporté au suivant)

Cause réelle des quatre échecs psycopg 1/4, nommée distinctement (aucune
attribution par association) : `args.json` → #1467 ; `agents.resolve_name` avant
`work.get` → #423 ; clone nexlink du témoin l122 → #1286 (garde de preuve
repo/clone, adaptation de fixture manquée). Aucun raccord #2022/#1343/#1484 ne
figure dans ces quatre échecs ; leur retrait suit la route bureau (déjà retirés
de la r2 #1500), il est prudent pour la fenêtre et ne leur invente pas de cause.

- #1467 `0234aafa` — `test_1467_train.TrainGitTest.test_missing_radar_profile_is_diagnosed_without_implicit_clone`
  en ERROR (`args.json` sur un SimpleNamespace, `merge_queue.py:1125`, appel
  `test:474`) : cause du rouge psycopg 1/4 (relevé auditeur 19:45Z). Déjà retiré
  de la r2 pour 25 occurrences (train_candidates/admissions, test_1680,
  test_cluster) ; diagnostic non rouvert dans ce véhicule.
- #423 `12823573` — rouge l109 : `work_move`/`work_close` « lot pinné refusé sans
  SQL » observent `['agents.resolve_name','work.get']` là où main attend
  `['work.get']` ; le delta #423 introduit l'opération médiée
  `agents.resolve_name`. Déjà retiré de la r2 (test_705_reviewer_designated,
  test_l242c).
- #1465 `c9885d1b` — `test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue` :
  les 8 colonnes de `provenance_receipts` ne sont ni accordées ni exclues au
  contrat des rôles superviseur (rejoué sur l'arbre réduit : les 20 colonnes du
  train sont parties avec #1467, restent les 8 de #1465 — lecture r2
  confirmée). Contrat de rôle non réglé : retrait plutôt qu'un correctif de
  classement hors fenêtre (arbitrage pilote : retirer l'unité réellement
  responsable plutôt que masquer ses tests). Le lot avait déjà été retiré de la
  r2 pour ce motif.
- #1343 `17976b9f4` — aucun des quatre échecs ne le cite ; retrait sur la route
  bureau (déjà retiré de la r2 : union plan/CI #1343×#1476×#2061/#1683
  incohérente, test_l161f_plan).
- #2022 `d95f227b` — aucun des quatre échecs ; retrait sur la route bureau (déjà
  retiré de la r2 : test_l159a_capacity_guard, `max_zombies` non validé).
- #1484 `dca1642a` — aucun des quatre échecs ; retrait sur la route bureau (déjà
  retiré de la r2 : test_expected_output, ligne 402 de
  `tests/test_l56_role_executor.py`).

## Réparations de composition portées par la branche

- **#1286 × l122** (`3ab4576c8`) : le témoin
  `test_le_sweep_concurrent_avec_numero_deja_tenu_le_corrige` appelait encore
  `work.merged(sha='a'*40)` sans dépôt ; la garde de preuve de #1286 refuse
  alors le clone ameesh au lieu du dépôt du lot (`manaty/nexlink`). Le témoin
  passe par le helper `_merged` (preuve Git réelle commitée dans le clone
  nexlink), course et assertions conservées ; seule l'adaptation de fixture
  manquée est portée, sans importer #2120 ni modifier le code.
- `contract.json` et compteurs médiés **inchangés** (identiques à la base r2 :
  64 opérations admises, 364 refusées, 428 à l'interface) ;
  `docs/EXECUTEUR-MEDIEE.md` non retouché ; `test_l107` rejoué vert.

## Contrôles de composition

- Rejeu first-parent `git cherry-pick -m 1` (résolutions d'union préservées) ;
  aucun marqueur de conflit restant ; `git diff --check` propre.
- Preuves des cas rouges sur l'arbre final (tête de composition `3ab4576c8`) :
  `test_1467_train` **absent** (retiré avec #1467) ;
  `test_l109_mediated_mode.SessionTest.test_work_close_lot_pinne_refuse_sans_sql`
  et `test_work_move_lot_pinne_refuse_sans_sql` **OK psql + psycopg** ;
  `test_l122_numeros_de_migration.NumeroAtterriTest.test_le_sweep_concurrent_avec_numero_deja_tenu_le_corrige`
  **OK psql + psycopg** ;
  `test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue`
  **OK psql + psycopg** après retrait de #1465 (rouge avec lui).
- Modules voisins rejoués sur l'arbre réduit : l107, l122, l109, cluster,
  1286_declared_merge_proof — résultats consignés au fil du lot.
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur ce manifeste.
- **Une seule CI complète** sur cette tête gelée (§12) ; aucune CI de lot ; le
  feu du pilote sur l'ancienne tête `916b0c28` ne couvre pas cette tête — un
  nouveau feu sur la tête exacte est demandé avant tout réarmement
  d'auto-fusion.
