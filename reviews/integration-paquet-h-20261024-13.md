# Paquet integration/paquet-h-20261024-13 — composition (§12)

Base : `main` **`84127f59aee871b268d15d71e3e03fc317a0f1e8`** (fusion du paquet
h-12, PR #2597, 24/10 23:45Z).
Branche `integration/paquet-h-20261024-13`, cible `main`. Porteur : **#3420**
(créé par mesh-design, fil du lot, 24/10 23:27Z).
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message **#135162** (24/10 23:27Z) : h-13 sur la nouvelle tête
  de `main` après la fusion de h-12 (#3414, PR #2597) ; « Déclencheur : la
  fusion de h-12 […] Main est réservée pendant cette CI (§12) : ne compose rien
  avant la fusion. Ensuite, paquet-candidats.py sur la nouvelle tête de main,
  puis integration/paquet-h-20261024-13 avec tout ce qui est prêt. Déjà
  annoncés : #3186 (`74260570937a`, 2 avis), #3399 (`3ca0daad86e1`, 2 avis),
  #3413 (`02e77e1d`, 2 avis). #3400 (impl-01, rebase prévu à 00:10Z) entre s'il
  est gelé et relu à ta composition. Pas de migration annoncée. Les fusions
  doivent être identiques au merge-tree, avec le manifeste seul en propre.
  Annonce-moi le gel : je pose « verdict not required ». N'oublie pas git fetch
  du clone radar /home/ameesh/work/ameesh-packets après ta poussée. »
- mesh-design, messages des 24/10 23:36–23:41Z : accord pour #3387 (P1 de la
  feuille de route, 2 avis, aucun hold) ; puis **#3223** (témoin EP7, doc
  seule, tête `62fb018bda47`, avis OK de dev2) « doit être dans h-13, premier
  paquet après le T0 de 23:30Z » ; « paquet-candidats sur ef32b515c le donne
  prêt ; s'il tombe en « écarté », écris-moi le motif avant de pousser le
  manifeste ». Composition par délégation du pilote élu ; mention au manifeste
  « témoin EP7 #3015 ».
- auditeur, message du 24/10 23:43Z : #3223 en `qa` sans assigné depuis
  23:38Z, tête `62fb018b`, avis ok dev2 #16900 (1/1, aucun avis dû),
  merge-tree propre sur `main ef32b515c` et sur h-12 `50a629aad` (vérifié
  23:4xZ) ; témoin du premier paquet post-T0 (#3015).
- mesh-design, message du 24/10 23:48Z : « deuxième ajout : #3015 (EP7, doc
  HOST-HANDOFFS, tête `170deada0bb8`, avis ok de cx1, merge-tree propre sur
  ef32b515 selon l'auditeur) est en qa depuis 23:5xZ et attend la fusion. Avec
  #3223, il entre dans h-13 si paquet-candidats le donne prêt ; sinon, dis-moi
  le motif. » ; auditeur, 23:50Z : #3015 passé en `qa` à 23:48Z pour h-13,
  tête `170deada`, doc seule (`docs/HOST-HANDOFFS.md`), avis ok cx1,
  merge-tree propre sur `main ef32b515c`.
- Calcul `scripts/paquet-candidats.py --repo . --project ameesh --target main`
  sur la base ci-dessus (clone radar `ameesh-packets`, 25/10 00:0xZ) :
  **8 lots prêts**, 3 en attente (hold pilote pour #3385 et #3388 ; véhicule
  h-12 désormais dans `main`), 0 erreur. Prêts : #3015, #3186, #3223, #3387,
  #3399, #3407, #3415, #3413.
- **Recomposition** (25/10 00:0xZ) : le premier calcul (24/10 23:48Z) donnait
  5 prêts, et la première tête `9997633aa0ea` (5 unités) a été gelée à 23:55Z.
  Le recalcul sur la même base donne **8 prêts** : #3015 est entré en `qa` à
  23:48Z (gel 18137, avis cx1 #18139), #3407 a reçu son avis cx1 #18231 à
  23:50Z (gel 18225), #3415 ses avis cx1 #18236 et auditeur #18237 à
  23:53–23:54Z (gel 18233) — tous acquis autour du premier gel.
  Conformément à #2404 et à la demande du pilote (#3015), la tête est
  recomposée avec les 8 ; la tête `9997633aa0ea` est remplacée et son run
  `40144843565` est annulé.
- **#3387 n'est pas cité par le courrier initial du pilote** : le calcul le
  rend prêt (gel 18184, avis auditeur #18189 et ameesh-dev5 #18200, fusion
  propre sur la base) ; la règle #2404 veut qu'un lot prêt soit composé ou
  écarté avec motif. Il est donc **composé**, accord du pilote le 24/10
  23:36Z.
- **#3223** est entré en `qa` à 23:38Z, après la prévision du composeur : le
  recalcul le rend prêt (gel 16896, avis ameesh-dev2 #16900 et auditeur
  #18095 sur la tête `62fb018b`, fusion propre) ; il est composé comme
  **témoin EP7 (#3015), premier paquet après le T0**.
- #3400 est encore en `intake` au calcul (réveil 00:10Z, rebase par impl-01) :
  pas de gel = pas candidat. S'il est gelé et relu avant le gel du paquet, il
  sera recomposé et ajouté comme #3412 le fut à h-12.

## Admissions (8 lots, 8 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3015 | `170deada0bb8` | light | 1/1 : ameesh-cx1 (codex), verdict 18139 | **EP7 (#3015)** : exercice en staging — arrêt planifié du poste puis coupure réseau : nouveau pilote < 5 min, jamais deux détenteurs, retour du poste en attente ; doc seule, 234 lignes à `docs/HOST-HANDOFFS.md` |
| #3186 | `74260570937a` | sensitive | 2/2 : ameesh-cx2 (codex), verdict 18197 ; mty-amh-impl-04 (deepseek), verdict 18201 | Release : un run jamais conclu (`CANCELLED`/`TIMED_OUT`/`STARTUP_FAILURE`) ne passe plus pour vert ; un requis rouge d'une suite homonyme n'est plus reclassé (B1/B2/B3 de la relecture) |
| #3223 | `62fb018bda47` | light | 2/2 : ameesh-dev2 (deepseek), verdict 16900 ; auditeur (claude), verdict 18095 | **Témoin EP7 #3015** : documentation du standby d'un rôle élu sur un hôte candidat (unité systemd, vérification `is-active`/journal, interdiction d'`agent-runner register` sur un candidat) ; doc seule, 22 lignes à `docs/EXPLOITATION.md` |
| #3387 | `2eb8afcfc3db` | sensitive | 2/2 : auditeur (claude), verdict 18189 ; ameesh-dev5 (deepseek), verdict 18200 | `macos.yml` : isolation des témoins Linux et des fixtures sous-processus, suite complète macOS verte sur main ; e2e timeout non reproduit sous Linux, diagnostic enrichi |
| #3399 | `3ca0daad86e1` | sensitive | 2/2 : auditeur (claude), verdict 18207 ; ameesh-cx1 (codex), verdict 18210 | `freeze_gate`/`plan_git` : un gel antérieur hors de toute ref est remplaçable, les auteurs inconnus d'un gel remplacé ne bloquent plus la désignation ; ascendance des refs distantes sur toute l'annonce |
| #3407 | `1a52c786987c` | light | 1/1 : ameesh-cx1 (codex), verdict 18231 | `packet-watch` : borner le titre seul au créneau et replier sur `target` (suites du panier #3406) |
| #3415 | `1656510cc608` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 18236 ; auditeur (claude), verdict 18237 | Désignation des relecteurs : un agent `dead` ou sous pause opérateur n'est plus désigné (`is_available` lit les deux états) |
| #3413 | `02e77e1d2a3a` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 18187 ; auditeur (claude), verdict 18194 | `work merged --repo` relance la classification d'un lot déjà fusionné ; la CLI dit relecture ouverte / déjà ouverte / motif |

Les têtes composées sont les têtes **publiées** lues par le calcul (gel effectif
= tête publiée, verdicts acquis sur cette tête). Aucune migration : les 8 lots
sont du code, des tests et de la documentation (vérifié : aucun `*.sql` ni
`deploy/` dans le diff).

## Composition (8 unités, 8 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3015 | `170deada0bb8` | `b5292dd3106e` | 0 conflit |
| 2 | #3186 | `74260570937a` | `b8255d70d8df` | 0 conflit |
| 3 | #3223 | `62fb018bda47` | `d5ac6f79559a` | 0 conflit |
| 4 | #3387 | `2eb8afcfc3db` | `ad001b75b1eb` | 0 conflit |
| 5 | #3399 | `3ca0daad86e1` | `abe2320f102d` | 0 conflit |
| 6 | #3407 | `1a52c786987c` | `4ff2eb78bc12` | 0 conflit |
| 7 | #3415 | `1656510cc608` | `fa38c692ac38` | 0 conflit |
| 8 | #3413 | `02e77e1d2a3a` | `b6ca263615de` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête (`vehicule 1300 : #<lot> <tête>`), et il porte un trailer
`ameesh-work:`. La base n'a pas bougé pendant la composition.

## Contrôles

- **Liste calculée** : 8 prêts, **8/8 composés**, 0 écarté, 0 erreur.
- **Conflits** : 8/8 unités fusionnées sans intervention ; chaque arbre de
  composition est identique à `git merge-tree --write-tree` de ses parents
  (vérifié commit par commit) ; aucun marqueur de conflit, aucun arbitrage de
  code ; `git diff --check` vert.
- **Tests ciblés de l'arbre intégré** : **704/704 verts en 518 s** (26 modules
  impactés : #3186 `test_3186_unconcluded_aggregate`, `test_3085_nonconclusive_gates`,
  `test_2876_packet_replacement`, `test_3244_release_optional_jobs` ; #3387
  `test_1252_vm_agents_install`, `test_3311_preuve_venv`, `test_2950_poste_processus`,
  `test_476_cleanup_limits`, `test_l30_accounts`, `test_l76_resident_runner`,
  `test_l226b_auth_failure` ; #3399 `test_3399_dangling_supersedes`,
  `test_2924_history_rewrite_provenance`, `test_2873_freeze_gate`,
  `test_2786_freeze_head`, `test_2971_freeze_note_marks`,
  `test_3147_prior_freeze_forge`, `test_3301_prior_freeze_unknown` ; #3413
  `test_1104_review_lot_hors_file`, `test_l539_review_lot`,
  `test_2394_review_gate_verdicts` ; #3407 `test_2601_packet_watch` ; #3415
  `test_3415_designation_dead_pause`, `test_3411_ephemeres_hors_designation`,
  `test_2447_review_designation_activation`, `test_1617_review_designation` ;
  #3015 et #3223 doc seule). Tests passés sur l'arbre de la tête de
  composition, identique à l'arbre final (le manifeste n'ajoute aucun code).
- **Compilation** : `compileall src tests` verte ; `bash -n scripts/pg-up.sh`
  vert.
- **Contrôles statiques** : 6/6 verts (`scripts/freeze-checks.py`).
- **Prévalidation** : indisponible (`card-stale`, carte de 175 h) —
  non bloquante (§3), motif journalisé (`prevalidation-unavailable`) ; citée
  ici comme demandé par la demande de CI.
- **Diff** : 36 fichiers, +2740/−139 (manifeste non compris).
- **Inclusion** : `scripts/paquet-inclusion.py` rc 0 sur la tête (attendus =
  les annoncés #3015/#3223/#3186/#3387/#3399/#3413 ; prêts = les 8 du
  calcul ; #3407 et #3415 composés).
- **Suite complète** : une seule `ci-full` demandée sur la tête finale, en
  mode paquet ; les verdicts unitaires couvrent les contenus, le paquet est
  relu sur sa tête par les familles du porteur (§12).
- **Fusion** : sous garde de tête (`--match-head-commit`), dès le vert et les
  avis du porteur ; landings `ameesh work landing <unité> --vehicle 3420`
  déclarés au gel.

## En attente (calcul, hors paquet)

| lot | motif |
|---|---|
| #3385 | hold pilote (lot canon) ; branche `canon/3385-roadmap-24-10` non publiée sur origin |
| #3388 | hold pilote (lot canon) ; branche `lot/3388-renommage` non publiée sur origin |
| #3400 | `intake` : réveil 00:10Z, gel et relecture par impl-01 ; entrera à une recomposition s'il est prêt avant le gel |

## Écartés (ce véhicule)

Aucun lot prêt du calcul n'est écarté : les 8 prêts sont composés.
