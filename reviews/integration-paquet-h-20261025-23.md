# Paquet integration/paquet-h-20261025-23-v4 — composition (§12), sur main issu de h-22

Base : `main` **`636cc71c712cf1a04a412a1c08af5859ca92d4be`** (fusion du paquet
h-22, PR #2770, 25/10 18:51Z). Branche
`integration/paquet-h-20261025-23-v4`, cible `main`. Porteur : **#3599**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : alerte ameesh du 19:19Z (créneau de paquet 20:00 manqué, cible
`main` libre, 9 lots prêts) ; liste de l'auditeur du 18:34/18:35Z complétée le
19:02Z (#3564) puis corrigée le 19:37Z (#3536 à inclure, résolution mécanique
`27b08fc4a5c2`) ; consigne mesh-design du 19:29Z : le lot d'incident #3600
(main rouge, `test_3568` laisse `ameesh.db.connect` patché) entre **en tête**
de cette composition dès son verdict OK, pour que la CI du paquet ne retombe
pas sur le même rouge hérité. Liste calculée `paquet-candidats` sur
`636cc71c712c` : **9 prêts**, tous composés ; #3600 s'y ajoute comme incident
P1, #3536 par sa résolution mécanique (conflit `role_timers.py`, avis 2/2
acquis sur `79dea9c4`). #3594 est entré comme prêt calculé, non cité par les
courriers. #3551 est composé avant #3558, comme demandé par l'auditeur.

Recomposition v4 (consigne mesh-design du 20:09Z, §11 bis) : la suite complète
du paquet v3 (`606ba639`) est rouge sur un test négatif déterministe — psycopg
5/8, `tests.test_2446_train_contract`
`test_packet_gate_and_suite_blobs_are_the_current_ones` : le blob de
`.github/workflows/tests.yml` est `76a0b227`, l'épingle du contrat est
`2fc4381f`. L'unité #3581 modifie `tests.yml` sans mettre à jour l'épingle de
`src/ameesh/train_contracts.py` ; aucune autre source du contrat ne change
entre `636cc71c` et `606ba639`. #3581 est donc retiré sans relire le reste :
**v4 = v3 − #3581**, même base `636cc71c`, même ordre, #3600 en tête, #3536
avec sa résolution mécanique `27b08fc4a5c2` telle quelle. Les 10 autres
unités sont inchangées.

## Admissions (10 lots, 10 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3600 | `e8c58fd9184d` | 1/1 (docs-01 deepseek) | INCIDENT main rouge : `test_3568` ne laisse plus `ameesh.db.connect` patché |
| #3551 | `4fb9db607821` | 1/1 (dev2 claude) | doc : portée réelle de GIT_CONFIG_SYSTEM et priorité de GIT_SSH_COMMAND |
| #3558 | `d7f196389be0` | 2/2 (dev1 claude, cx3 codex) | Fusion auto : un rouge seulement hérité de la cible ne retient pas la fusion |
| #3557 | `4a6c843d448e` | 3/3 (cx3 codex, auditeur claude, dev2 claude) | Plancher d'avis posé après le gel : compléter la désignation |
| #3586 | `dbc0f69489df` | 1/1 (dev2 claude) | Comptes : une lecture antérieure au reset de fenêtre n'écarte plus le compte |
| #3565 | `07b07f7c04a3` | 1/1 (dev2 claude) | Panier #3556 : course hold/assign et proposition filtrée |
| #3564 | `a10fa46151cf` | 2/2 (dev3 deepseek, cx3 codex) | Panier #3546 : ménage des mesures et preuve d'annulation |
| #3594 | `445198108f5e` | 1/1 (mesh-design claude) | Test instable : marge nommée de 10 s avant l'échéance |
| #3597 | `2ec59f5cca74` | 1/1 (auditeur claude) | Test instable : tolérance nommée de 50 ms sur les attentes l48 |
| #3536 | `27b08fc4a5c2` (résolution mécanique de `79dea9c4`) | 2/2 (cx1 codex, dev4 claude) | notify : canal nexlink — résolution `role_timers.py` avec h-22, purement mécanique |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3600 | 19388 | 19392 docs-01 (deepseek) ok |
| #3551 | 19313 | 19324 dev2 (claude) ok |
| #3558 | 19318 | 19331 dev1 (claude) ok, 19328 cx3 (codex) ok |
| #3557 | 19211 | 19221 cx3 (codex) ok, 19308 auditeur (claude) ok, 19320 dev2 (claude) ok |
| #3586 | 19297 | 19323 dev2 (claude) ok |
| #3565 | 19293 | 19310 dev2 (claude) ok |
| #3564 | 19322 | 19333 cx3 (codex) ok, 19334 dev3 (deepseek) ok |
| #3594 | 19350 | 19351 mesh-design (claude) ok |
| #3597 | 19361 | 19362 auditeur (claude) ok |
| #3536 | 19118 | 19127 cx1 (codex) ok, 19168 dev4 (claude) ok ; résolution mécanique `27b08fc4` |

## Composition (10 unités, 10 lots)

| unité | lot | source | commit |
|---|---|---|---|
| 1 | #3600 | `e8c58fd9184d` | `77dfb01291e7` |
| 2 | #3551 | `4fb9db607821` | `e5b1b3a59fee` |
| 3 | #3558 | `d7f196389be0` | `1e2ba6d850f9` |
| 4 | #3557 | `4a6c843d448e` | `70c65bc4eedf` |
| 5 | #3586 | `dbc0f69489df` | `1fa047c79fef` |
| 6 | #3565 | `07b07f7c04a3` | `9517d515c962` |
| 7 | #3564 | `a10fa46151cf` | `6024bee1b40b` |
| 8 | #3594 | `445198108f5e` | `bb02dab2b648` |
| 9 | #3597 | `2ec59f5cca74` | `428133ea40b7` |
| 10 | #3536 | `27b08fc4a5c2` (résolution) | `1434e95b3ba7` |

Aucun conflit : 9/9 unités directes fusionnées proprement sur `636cc71c712c`,
puis #3536 par sa résolution mécanique `27b08fc4a5c2` (parents `79dea9c4` +
h-22 `d80e7201`, déjà dans `main`), sans relance de relecture (canon §2 :
conflit mécanique). Contrôle d'inclusion `scripts/paquet-inclusion.py` sur la
tête du manifeste : voir la note de gel du porteur.

## Écartés de h-23

| lot | motif |
|---|---|
| #3581 | retiré de la v3 : `tests.yml` modifié sans mise à jour de l'épingle de `src/ameesh/train_contracts.py` — rouge négatif déterministe `test_2446_train_contract` (psycopg 5/8) ; à recomposer hors v4 |
| #2309 | app `liquichain` : atterrit sur le véhicule #3514 (`integration/liquichain-20261025-0930`, unité `b8b0f32d95f3`), pas un candidat de `main` ameesh |
| #3519 | lecture de la porte incomplète (provenance d'auteur inconnue sur les commits gelés) — chaîne handoff hors h-23 |
| #3572 | gel `38020880b51e` dépassé par la tête publiée `b322f6646924` ; 1 avis codex sur 2 requis |
| #3544 | 1 avis deepseek sur 2 requis (indépendance non prouvée) |
| #3526 | 0 avis sur le gel `30a916ad44b0` (sensible) et conflit handoff (`handoff_factory.py`, `runner.py`, `test_3472_handoff_runner.py`) |
| #3584 | 1 avis claude sur 2 requis (indépendance non prouvée) et conflit `review_designation.py` |
| #3576 | 1 avis claude sur 2 requis et conflit `docs/EXPLOITATION.md` |
| #3592 | 1 avis claude sur 2 requis |
| #3589 | 1 avis claude sur 2 requis |
| #3550 | 1 avis claude sur 2 requis |
| #3457 | hold pilote depuis 07:54Z et branche non publiée |
| #3474 | lecture de la porte incomplète (provenance d'auteur inconnue) |
| #3509 | lecture de la porte incomplète (provenance d'auteur inconnue) |
| #3473 | lecture de la porte incomplète (famille d'auteur ambiguë pour ameesh-cx2) |
| #3506 | lecture de la porte incomplète (provenance d'auteur inconnue) |

Les cinq erreurs de lecture (#3473, #3474, #3506, #3509, #3519) sont la chaîne
handoff, hors h-23 par consigne pilote (déjà hors h-22 pour le même motif) :
elles ne rendent pas la liste des lots prêts incomplète pour ce paquet.

## Contrôles

- Composition : 10/10 sans conflit sur `636cc71c712cf1a04a412a1c08af5859ca92d4be` (#3536 par résolution mécanique) ; écart #3581 motivé ci-dessus, sans relire les autres unités (§11 bis).
- Liste calculée `paquet-candidats` sur `636cc71c712c` : **9 prêts** ; 8 sont
  composés et #3581 est écarté avec motif ci-dessus ; #3600 ajouté comme
  incident P1 sur consigne mesh-design, #3536 sur correction de l'auditeur
  (résolution mécanique) ; les autres lots ouverts sont écartés avec motif
  ci-dessus.
- Une seule suite complète sur la tête gelée, après le gel.
- Après fusion : staging hors créneau (§11 bis) pour #3600 (correctif de test),
  #3564 (workflows `.github/workflows/**` et `scripts/purge-artefacts.py`),
  #3586 (comptes/jauge), #3557 (porte d'avis), #3536 (canal notify nexlink).
