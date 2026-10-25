# Paquet integration/paquet-h-20261025-17 — composition (§12)

Base : `main` **`7d3e776ec02bc7f4a50cb59d1965292abe32d799`** (fusion du paquet
h-16, PR #2673, 25/10 06:55:40Z, auto-merge sur CI verte).
Branche `integration/paquet-h-20261025-17`, cible `main`. Porteur : **#3499**
(créé par mesh-design, 25/10 07:00Z).
Composeur : mty-amh-impl-01. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, 25/10 06:51Z : cas 2 — le drill #3439 glisse (h-16 non fusionné
  avant 06:50Z) ; h-17 = #3468, #3494, #3495, #3456 ; #3496 exclu (témoin du
  drill, reporté à 13:00Z).
- mesh-design, 25/10 06:58Z : ajouter H2 #3469 si le recalcul sur la tête
  fusionnée le donne prêt, en unité juste après H1 #3468 (§4, lots consécutifs
  du même chantier) ; sinon il passe au paquet suivant sans retenir les autres.
- mesh-design, 25/10 07:00Z : véhicule #3499 créé, branche
  `integration/paquet-h-20261025-17`, cible `main 7d3e776e`.
- mesh-design, 25/10 07:31Z (courrier #136983) : la branche n'est pas encore
  poussée (rappel §6) ; #3469 est revenu prêt (`f81a41ce`, gel #18629, OK cx2 et
  mesh-design) à mettre en unité juste après #3468 ; **#3460** (`fb82948b`) a sa
  dispense et l'OK de dev3 — l'ajouter si l'inclusion reste propre, sinon il
  passe en h-18 ; ne pas retarder la poussée pour eux.
- Premier recalcul `packet_candidates.inventory` du 25/10 07:2xZ sur la base
  ci-dessus (projet `ameesh`/`main` explicites, l'écart d'hôte `radar.projects`
  restant) : **6 lots prêts**, 4 en attente, 0 erreur. Prêts : #3468
  (`6bc7211e7a27`), #3469 (`f81a41ce4295`), #3489 (`6e46a4a543e5`), #3494
  (`52f5746dc196`), #3495 (`6cfa3430575c`), #3456 (`3581d8f17455`).
- #3469 : au premier recalcul il était en attente (`freeze-after-verdict`,
  tête `32f9de8d` postérieure aux verdicts de `f81a41ce`) ; il est revenu prêt
  à 07:12Z (gel #18629 sur `f81a41ce`, branche ramenée à cette tête, verdicts
  de cx2 06:52Z et mesh-design 06:58Z sur cette même tête) et entre au paquet
  comme demandé.
- #3460 : au premier recalcul il était en attente (`verdicts-missing`, un seul
  avis) ; gel #18639 sur `fb82948b` à 07:18Z, **dispense pilote** mesh-design
  (#1979, 07:22Z, note `verdict-not-required`) et **OK ameesh-dev3** (verdict
  18642, 07:23Z) sur cette tête ; il entre au paquet sur la consigne du pilote.
- Recalcul de contrôle `packet_candidates` du 25/10 07:41Z sur la même base :
  **10 lots prêts**, 2 en attente, 0 erreur. Prêts : #3460 (`fb82948b`), #3468
  (`6bc7211e`), #3469 (`f81a41ce`), #3488 (`00b550ef`), #3489 (`6e46a4a5`),
  #3494 (`52f5746d`), #3495 (`6cfa3430`), #3497 (`927b614d`), #3498
  (`f0d14847`), #3456 (`3581d8f1`). En attente : #3457
  (`branch-not-published`), #3471 (`verdicts-missing`, indépendance non
  prouvée).
- Lecture du recalcul : lancée depuis l'arbre de main (h-16) parce que le `src`
  du paquet lit `agent_registry.retired_by` (#3460, migration 0484) que la base
  de l'hôte n'a pas encore — la migration additive s'applique en staging
  **avant** de relancer les exécuteurs (§14) ; même module, même base, cible et
  projet explicites, aucune écriture. Écart d'hôte signalé :
  `scripts/paquet-candidats.py --project ameesh` refuse sur cet hôte (`project
  'ameesh' is not declared in radar.projects`) ; la liste est produite par le
  même module sans écriture (même écart que les manifestes h-16 et 23/10).

## Admissions (6 lots, 6 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3468 | `6bc7211e7a27` | sensitive | 2/2 : ameesh-cx2 (codex) 05:46Z ; auditeur (claude) 05:48Z sur `6bc7211e` | Handoff réel H1 : contrat et fabrique de handoff enrôlée (source épinglée, requirements_for) |
| #3469 | `f81a41ce4295` | sensitive | 2/2 : ameesh-cx2 (codex) 06:52Z ; mesh-design (claude) 06:58Z sur `f81a41ce` | Handoff réel H2 : effets de préparation et Git (inventory … session), sans démarrage cible |
| #3494 | `52f5746dc196` | sensitive | 2/2 : mesh-design (claude) 05:45Z ; ameesh-cx1 (codex) 05:47Z sur `52f5746d` | Paquet : ci-full n'annule plus le run paquet du push (check requis en FAILURE) |
| #3495 | `6cfa3430575c` | sensitive | 2/2 : mty-amh-qa-01 (deepseek) 05:59Z ; mesh-design (claude) 06:16Z sur `6cfa3430` | Panier « courrier-mort » du 2026-10-25 : défauts non bloquants (N, P2, P3) |
| #3456 | `3581d8f17455` | sensitive | 2/2 : ameesh-dev1 (claude) 04:48Z ; ameesh-dev3 (deepseek) 05:26Z sur `3581d8f1` | Panier « #3444 » du 2026-10-25 : défauts non bloquants (N, P2, P3) |
| #3460 | `fb82948be5f1` | sensitive | dispense pilote (gel 07:22Z, `verdict-not-required`) ; OK ameesh-dev3 (deepseek, verdict 18642) 07:23Z sur `fb82948b` | Agent veille sans responsable humain (66 non lus depuis 14 j) : retrait, docs-01 tient la veille GitHub |

## Composition (6 unités, 6 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3468 | `6bc7211e7a27` | `f4a907459` | 0 conflit |
| 2 | #3469 | `f81a41ce4295` | `1a8a09f7f` | 0 conflit (H2, empilé sur H1) |
| 3 | #3494 | `52f5746dc196` | `d3a767a5c` | 0 conflit |
| 4 | #3495 | `6cfa3430575c` | `e3d1edfb1` | 0 conflit |
| 5 | #3456 | `3581d8f17455` | `b807feb5e` | 0 conflit |
| 6 | #3460 | `fb82948be5f1` | `ecf7601ee` | 0 conflit (tête de lot déjà fusionnée avec h-16) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête (`vehicule 1700 : #<lot> <tête>`) et il porte un trailer
`ameesh-work:`. La base n'a pas bougé pendant la composition.

## Conflits

Aucun : les six unités se sont fusionnées sans intervention, `git diff
--check` vert.

Le contrôle d'inclusion signale un contenu **partiel** pour l'unité 1
(#3468) : l'unité 2 (#3469, H2, empilée sur H1) réécrit des docstrings et des
tests ajoutés par H1 — c'est l'effet attendu de l'empilement, pas une omission,
et chaque unité porte sa propre tête et son propre commit. Signal lu avant le
feu, aucune action.

## Reprise — classement des colonnes de retrait (0484, #3460)

Le contrat des rôles superviseur refuse toute colonne ni accordée ni exclue :
`tests.test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue`
rougissait sur la tête composée (`fb82948b` et intégrée) avec **3 écarts** —
`agent_registry.retired_at`, `.retired_by`, `.retired_reason`, ajoutées par la
migration 0484 de #3460.

Correctif sur le véhicule, commit **`dea94f3b7`**
(`deploy/sql/role-superviseur.sql`) : les trois colonnes entrent au contrat du
rôle de base (un instant, le nom de qui a retiré l'agent et la raison de cet
état — même nature que `stop_reason` et `status_text`, pas de contenu). Aucun
GRANT retiré, aucune autre colonne ; classe du delta propre : **sensible
(SQL)** → deux avis de familles différentes sur la tête finale, puis suite
complète. Témoin local : la classe `SuperviseurRoleTest` verte sur le banc
55432.

## Contrôles

- **Liste calculée** : 10 prêts, **6/6 composés**, 4 écartés avec motif
  (#3488, #3489, #3497, #3498 — prêts après la liste du pilote et la
  composition, ils passent en h-18), #3496 hors paquet (témoin du drill), 0
  erreur.
- **Inclusion** : `scripts/paquet-inclusion.py` à lancer sur la tête finale
  (attendus = les 6 admis ; prêts = les 10 du recalcul, 4 écartés motivés,
  #3496 hors paquet).
- **Contrôles statiques du gel** (`scripts/freeze-checks.py`) : **6/6 verts**
  (garde mécanique #2449, compilation, attribution #1632, lint L63, alert
  types #1060, doc readers L161a) sur la tête finale.
- **Tests ciblés de l'arbre intégré** : 435 tests sur les 11 modules
  impactés. Verts : `test_3468_handoff_factory` **34/34**,
  `SuperviseurRoleTest` **16/16**, `test_l107_executor_contract` **23/23**.
  Premier passage interrompu par un test serveur de
  `test_l108_executor_server` bloqué sur un banc saturé (banc 55432 partagé,
  charge 6) : 31 rouges sur les 345 premiers tests, dont 20 reproduits et
  expliqués par les modes du worktree (ci-dessous) ; second passage verbose
  après normalisation en cours au gel — **non concluant** pour le reste (infra,
  §3), la suite complète de la CI tranche sur l'arbre entier.
- **Défaut d'environnement consigné (non bloquant, à corriger à la source)** :
  le worktree était en umask 0002 (fichiers group-writable) ; `_trusted_source`
  de la fabrique de handoff refuse à raison un compagnon writable, d'où 20
  rouges de #3468, tous verts après `chmod g-w` sur `pont-handoff.py` et
  `hote-handoff.py`. Le test suppose un checkout 0022 : défaut du test, à
  porter au panier du chantier, sans retenir le paquet (§3).
- **Diff** : 46 fichiers, +6666/−98 hors manifeste ; classe sensible (SQL
  migration 0484, `deploy/`, handoffs, `plan_git`, `radar`, `undeliverable`,
  `registry`, `storage/`, `cli`).
- **Migrations (staging)** : une migration additive entre,
  `src/ameesh/migrations/0484_agent_retire.sql` (#3460, trois colonnes NULL sur
  `agent_registry`, aucun changement de sens des lignes existantes). Elle
  s'applique en staging **avant** de relancer les exécuteurs (§11 bis, §14).
- **Fusion** : sous garde de tête, dès le vert et les avis du porteur ;
  landings `ameesh work landing <unité> --vehicle 3499` déclarés au gel.

## En attente (recalcul, hors paquet)

| lot | motif |
|---|---|
| #3457 | `branch-not-published` : `auditeur/session-taille-pilotes` absente d'origin |
| #3471 | `verdicts-missing` : un avis (mesh-design) sur `8564951b`, indépendance non prouvée |

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3496 | témoin du drill #3439 (reporté à 13:00Z) : exclu de tout paquet normal (consigne mesh-design 06:35Z) |
| #3488 | prêt au recalcul de 07:41Z seulement (`00b550ef`), après la liste du pilote (06:51Z) et la composition (07:02Z) ; passe au paquet suivant (consigne mesh-design 07:31Z : ne pas retarder la poussée) |
| #3489 | prêt à 07:17Z (`6e46a4a5`), après la liste du pilote et le début de composition ; passe au paquet suivant |
| #3497 | prêt au recalcul de 07:41Z seulement (`927b614d`), après la liste du pilote et la composition ; passe au paquet suivant |
| #3498 | prêt au recalcul de 07:41Z seulement (`f0d14847`), après la liste du pilote et la composition ; passe au paquet suivant |

## Têtes précédentes

`b807feb5e` : première poussée, 25/10 07:35Z, 5 unités (#3468, #3469, #3494,
#3495, #3456). #3460 (unité 6) et le correctif du contrat superviseur
(`dea94f3b7`) sont ajoutés ensuite pour la tête finale. Base `7d3e776e`
inchangée.

## CI

Suite complète unique portée par le run `pull_request` de la PR sur la tête
finale qui porte ce manifeste, étiquette `ci-full-<tête>` ; le portier
`paquet.yml` ne lance rien pour une tête couverte par la PR. Un rouge après
fusion se corrige en avant ; un lot fautif est retiré sans relire le reste
(§11 bis).
