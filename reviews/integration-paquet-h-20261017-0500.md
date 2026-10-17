# Paquet integration/paquet-h-20261017-0500 — composition et réparation #2199

Base : `main` après atterrissage de PR1577 (`integration/paquet-h-20261017-0400`),
merge réel **`18859e0664c291f199bc88b8c98abd7e68b1222f`** (04:49:02Z, arbre identique à la tête
`f2be9d0f4fca990afa54f8ed03084a459b699352`). La branche est construite sur
`f2be9d0f4`, ancêtre de `main` après cette fusion : le diff de la PR ne porte
que les unités ci-dessous.

Exécuteur : **mty-amh-impl-01** (composition, CI et fusion). Admissions du
pilote : #2190 (04:15, GO final auditeur sur `62b37ebd7`), #1677 (admission
maintenue au prochain paquet compatible, remise `91df05dac` sur la tête 0400),
#2189 (04:26, R1 Codex favorable sur `e583d234c`), #2177 (04:29, GO Codex sur
`4ff178510`), #2157 (04:29, R1 Codex favorable sur `1ee9af5c`, « admis
prochain paquet compatible »). Transports : #2190 confié par mesh-design
(#94841/#94861) ; #1677 remise poussée par mty-amh-impl-07 ; #1152/#1343
signalés prêts par mty-amh-impl-04 mais différés (voir Hors périmètre).
Réparation d'incident CI : #2199 — GO Codex mesh-design **enregistré** sur
`96da5b62cee30c8f10e87aa06922bedce9026536` (**0 B / 0 N**, désignation native),
diff test-only relu, transport exclusif impl-01 ; ajoutée après l'échec du run
`39006173443` sans relance identique de `c8d960af`. Aucune CI de lot.

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2190 | `62b37ebd701d082368384e10d6c6d7945388dc14` (`lot/2190-sonde-libre-rapport-ko`) | `28eba5c91` | priorité 1, staging ; gel 04:05, GO auditeur 04:14 sur la même tête ; fusion propre. 4 fichiers, zéro migration |
| 2 | #2189 | `e583d234ca1d38acd2fd66f6d4145d1d5c400f55` (`lot/2189-rejeu-attributs-cible`) | `6687ee360` | R1 Codex (mesh-design) 04:26, correction sur la branche ; fusion propre (`resolutions.py`, `test_l160d_resolutions.py`) |
| 3 | #2177 | `4ff178510492b742259218892432fa1a0358fbbb` (`lot/2177-carte-fichier-vide`) | `497cd68f6` | GO Codex (mesh-design) 04:29 ; fusion propre (`tests/fake_acp_agent.py`, nouveau témoin SIGTERM) |
| 4 | #2157 | `1ee9af5c10e662176a43067e76324bd4fb5ebfed` (`lot/2157-composition-ameesh-work`) | `e26883b43` | R1 Codex favorable 04:29, léger ; outil de composition + convention `ameesh-work` ; fusion propre |
| 5 | #1677 | `5b208e277d16` (tête gelée ; remise `91df05dac`, `refs/ameesh/resolutions/1677/paquet-h-20261017-0400`) | `fa961a70a` | priorité 1, prise automatique ; GO dev7 7023 conservé, remise fraîche sur la tête 0400 ; fusion propre, 16 fichiers |
| 6 | #2199 | `96da5b62cee3` (`lot/2199-isolation-threads-attach`) | `96da5b62` (reprise directe, ff) | réparation d'incident CI du run `39006173443` : isolation du fil `attach`, test-only (`tests/test_events_attach.py`, +97/−21), base `c8d960af` ; GO Codex mesh-design 0 B / 0 N sur `96da5b62` (désignation native), diff relu ; 51 ciblés psql + 51 psycopg (auteur et transporteur) |

Chaque commit de composition porte le trailer `ameesh-work: <lot>` (convention
#2157) ; les commits de fusion portent le lot comme second parent. L'unité 6
est une reprise directe (fast-forward) du correctif auteur : son lien est le
manifeste et son sujet `#2199`, aucun delta sémantique n'est introduit.

## Résolutions de composition (preuve)

- Aucun conflit : les cinq unités initiales touchent des fichiers disjoints
  sur la base `f2be9d0f4` (deploy staging, `resolutions.py`, témoins de
  mesure, outillage de composition, prise automatique) ; #2199 ne touche
  qu'un fichier de test et entre en fast-forward sans conflit. Aucune
  résolution manuelle, aucun delta sémantique.
- #2190 est la première unité (priorité 1) ; aucun code relu de #2190 n'est
  modifié par la composition : ses trois fichiers propres sont identiques à
  `62b37ebd7`, et `docs/DEPLOIEMENT-HOTE-AGENTS.md` ne reçoit en plus que la
  section #2152 apportée par la base 0400 (hunks disjoints, passages #2190
  intacts).
- #1677 : la remise `91df05dac` (parents `f2be9d0f4` + `2b5ef81f`) porte le
  delta #1677 seul (16 fichiers, +955/−61) ; sa fusion sur le paquet est
  propre et ne réintroduit aucun contenu du 0400.
- Totaux hors manifeste : cinq unités initiales = 28 fichiers, +2334/−182 ;
  correctif L804 = 1 fichier, +8/−3 ; #2199 = 1 fichier, +97/−21 ; soit
  **30 fichiers, +2439/−206** au total. `git diff c8d960af..96da5b62` =
  `tests/test_events_attach.py` seul (+97/−21) ; `git diff --check` propre.

## Preuves locales (arbre composé)

- `tests.test_1155_staging` + `tests.test_1323_actif` (cibles #2190) :
  **113 OK**.
- `tests.test_l160d_resolutions` (cible #2189) : **53 OK**.
- `tests.test_2177_couverture_sigterm` (cible #2177) : **2 OK**.
- `tests.test_2157_paquet_compose` (cible #2157) : **5 OK**.
- `tests.test_1677_prise_empilee` + `tests.test_l119_backlog` +
  `tests.test_l234_auditor` (cible #1677, banc psql 55432) : **63 OK**.
- `tests.test_events_attach` + `tests.test_l109_mediated_mode` (cible #2199,
  arbre de réparation) : **51 OK psql + 51 OK psycopg** (transporteur),
  après les 51 + 51 de l'auteur ; log local du transporteur.
- Total **236 tests ciblés OK** sur les cinq unités initiales, plus la
  réparation #2199 ; `bash -n` sur `staging.sh`, `py_compile` sur
  `sante-courrier.py` et `paquet-compose.py`, `diff --check` propre.
- Aucune CI de lot, aucune suite complète locale : une seule CI complète
  tourne sur la tête de la PR du paquet (§12).

## Correctif de composition après la CI du 17/10 (tête `2fafa5755`)

La CI psycopg 1/4 et 2/4 (runs `39004127666`, flux `117313147491` /
`117315463228`) a rendu deux échecs :

- `tests.test_l804_prise_executeur.PriseDeLExecuteurTest.test_garde_fous_conserves`
  (psycopg 2/4) : déterministe. Le test attendait l'ancien garde-fou de repos
  (dernier tour < `idle_s`), que #1677 (b) a explicitement remplacé par la
  lecture des lots actionnables (`actionable_lots`) ; #1677 avait adapté
  `test_l119_backlog` et `test_l165_prise_lots` mais pas ce témoin L804.
  **Correction test-only** : le témoin vérifie désormais la prise d'un agent
  sans lot actionnable, garde le plafond de budget, la pause d'hôte et
  l'interrupteur `idle_s=0`. Aucun code de production modifié ; après
  correction, `tests.test_l804_prise_executeur` = **14 OK**.
- `tests.test_l109_mediated_mode.SessionTest.test_foreign_harness_hook_leaves_mediated_mail_pending`
  (psycopg 1/4) : **confirmé en CI** sur `c8d960af` (run `39006173443`, job
  `117319211444` : 1 échec sur 2251, les 9 parts psql et les 3 autres parts
  psycopg vertes). Diagnostic #2199 : la fixture
  `test_attach_coupe_sans_journal_sur_le_chemin_d_arret` patchait le `log`
  global avec un `sleep(30)` ; le fil démon `journal` restait bloqué dans ce
  puits après le test et écrivait au réveil dans le `redirect_stdout` de
  l109. **Correctif test-only `96da5b62`** : puits confiné au test, sentinelle
  FIFO prouvant la vidange de la file, contrat d'arrêt borné d'`attach` et
  assertion stdout vide de l109 conservés. Ciblés : 51 OK psql + 51 OK
  psycopg (auteur), 51 OK psql + 51 OK psycopg (transporteur).

La réparation change la tête de la branche (`c8d960af` → nouvelle tête) :
l'auto-fusion a été **désarmée** avant la poussée ; elle ne sera réarmée que
sur la nouvelle tête exacte, après le nouveau GO du pilote (§2, tête changée).

## Hors périmètre (différé, non retenu à ce cutoff)

- **#1152** : la résolution publiée `refs/ameesh/resolutions/1152/2190` =
  `8fd29d790797` (parents `a74d8fd64` + `62b37ebd`, delta vs `62b37ebd` =
  delta #1152 exact 10 fichiers +550/−233, 156 ciblés OK) est prête, mais sa
  composition sur ce paquet rouvre un conflit `deploy/mise-a-jour/vm.sh` avec
  #2152 (drain hors tour) et un delta de composition à signaler. Elle passe
  **après** #2190, selon la consigne de mty-amh-impl-04 (« la résolution est
  prête pour quand #2190 aura atterri ») ; elle sera reprise au véhicule
  suivant, sur la base à jour.
- **#1343** : admission/transport confirmés, tête `08f384f028` inchangée ;
  la remise disponible (`662647761`, base `ac7bfdb23` du 16/10) demande le
  rejeu des résolutions #379/#1359/#1466/#2152 et du correctif de parts. Elle
  demande un véhicule dédié avec mty-amh-impl-04, pas ce cutoff prioritaire.
- **#2186** : admission conditionnée à #2043, absent de `main`.
- **#1114** : précondition de configuration staging toujours ouverte.
- **#1465/#1612** : prêts (gel/verdict ok, remises `fb9a2575`/`70c9afa3`) mais reportés au véhicule suivant ; composition de banc locale `wip/paquet-0400-7units`, non poussée.

## CI et fusion

- Une seule CI complète sur la tête gelée de la branche (PR, suite
  psql+psycopg) ; aucune CI de lot, aucune relance de `c8d960af`.
- Fusion automatique **désarmée** sur `c8d960af` ; réarmement seulement sur la
  nouvelle tête exacte (`--match-head-commit`) après le GO du pilote ; ne pas
  rebaser.
- #2190 et #1677 sont reliés à ce manifeste et au run de la PR ; après
  fusion, rattachement des cinq lots aux commits réels et confirmation à
  mesh-design. L'activation staging du correctif #2190 reste coordonnée par
  bureau après intégration ; aucune opération hôte n'est faite ici.
