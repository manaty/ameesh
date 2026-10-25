# Paquet integration/paquet-h-20261025-23 — composition (§12), sur main issu de h-22

Base : `main` **`636cc71c712cf1a04a412a1c08af5859ca92d4be`** (fusion du paquet
h-22, PR #2770, 25/10 18:51Z). Branche
`integration/paquet-h-20261025-23`, cible `main`. Porteur : **#3599**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : alerte ameesh du 19:19Z (créneau de paquet 20:00 manqué, cible
`main` libre, 9 lots prêts) ; liste de l'auditeur du 18:34/18:35Z complétée le
19:02Z (#3564) ; canon §11 bis/§12. Liste calculée `paquet-candidats` sur
`636cc71c712c` : **9 prêts**, tous composés ci-dessous ; #3594 est entré comme
prêt calculé, non cité par les courriers. #3551 est composé avant #3558,
comme demandé par l'auditeur.

## Admissions (9 lots, 9 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3551 | `4fb9db607821` | 1/1 (dev2 claude) | doc : portée réelle de GIT_CONFIG_SYSTEM et priorité de GIT_SSH_COMMAND |
| #3558 | `d7f196389be0` | 2/2 (dev1 claude, cx3 codex) | Fusion auto : un rouge seulement hérité de la cible ne retient pas la fusion |
| #3557 | `4a6c843d448e` | 3/3 (cx3 codex, auditeur claude, dev2 claude) | Plancher d'avis posé après le gel : compléter la désignation |
| #3586 | `dbc0f69489df` | 1/1 (dev2 claude) | Comptes : une lecture antérieure au reset de fenêtre n'écarte plus le compte |
| #3565 | `07b07f7c04a3` | 1/1 (dev2 claude) | Panier #3556 : course hold/assign et proposition filtrée |
| #3581 | `3a5bc588faac` | 2/2 (dev2 claude, cx3 codex) | CI : node 22 provisionné et vérifié dans chaque part de la suite |
| #3564 | `a10fa46151cf` | 2/2 (dev3 deepseek, cx3 codex) | Panier #3546 : ménage des mesures et preuve d'annulation |
| #3594 | `445198108f5e` | 1/1 (mesh-design claude) | Test instable : marge nommée de 10 s avant l'échéance |
| #3597 | `2ec59f5cca74` | 1/1 (auditeur claude) | Test instable : tolérance nommée de 50 ms sur les attentes l48 |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3551 | 19313 | 19324 dev2 (claude) ok |
| #3558 | 19318 | 19331 dev1 (claude) ok, 19328 cx3 (codex) ok |
| #3557 | 19211 | 19221 cx3 (codex) ok, 19308 auditeur (claude) ok, 19320 dev2 (claude) ok |
| #3586 | 19297 | 19323 dev2 (claude) ok |
| #3565 | 19293 | 19310 dev2 (claude) ok |
| #3581 | 19256 | 19265 cx3 (codex) ok, 19295 dev2 (claude) ok |
| #3564 | 19322 | 19333 cx3 (codex) ok, 19334 dev3 (deepseek) ok |
| #3594 | 19350 | 19351 mesh-design (claude) ok |
| #3597 | 19361 | 19362 auditeur (claude) ok |

## Composition (9 unités, 9 lots)

| unité | lot | source | commit |
|---|---|---|---|
| 1 | #3551 | `4fb9db607821` | `2cef290c7` |
| 2 | #3558 | `d7f196389be0` | `de4b3ab31` |
| 3 | #3557 | `4a6c843d448e` | `d0f481eb5` |
| 4 | #3586 | `dbc0f69489df` | `ad8b8ac41` |
| 5 | #3565 | `07b07f7c04a3` | `f40b433ec` |
| 6 | #3581 | `3a5bc588faac` | `25c400073` |
| 7 | #3564 | `a10fa46151cf` | `2dbf247db` |
| 8 | #3594 | `445198108f5e` | `841cac349` |
| 9 | #3597 | `2ec59f5cca74` | `7836511ff` |

Aucun conflit : 9/9 fusionnées proprement sur `636cc71c712c`, sans résolution
ni union. Contrôle d'inclusion `scripts/paquet-inclusion.py` sur la tête du
manifeste : voir la note de gel du porteur.

## Écartés de h-23

| lot | motif |
|---|---|
| #2309 | app `liquichain` : atterrit sur le véhicule #3514 (`integration/liquichain-20261025-0930`, unité `b8b0f32d95f3`), pas un candidat de `main` ameesh |
| #3519 | lecture de la porte incomplète (provenance d'auteur inconnue sur les commits gelés) — chaîne handoff hors h-23 |
| #3536 | conflit avec `main` sur `src/ameesh/role_timers.py` (résolutions #3568 et #3590 disponibles, non appliquées) — verdicts acquis mais composition impossible sans résolution relue |
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
elles ne rendent pas la liste des 9 prêts incomplète pour ce paquet.

## Contrôles

- Composition : 9/9 sans conflit sur `636cc71c712cf1a04a412a1c08af5859ca92d4be`.
- Liste calculée `paquet-candidats` sur `636cc71c712c` : 9 prêts, tous composés ;
  les 16 autres lots ouverts sont écartés avec motif ci-dessus.
- Une seule suite complète sur la tête gelée, après le gel.
- Après fusion : staging hors créneau (§11 bis) pour #3564 (workflows
  `.github/workflows/**` et `scripts/purge-artefacts.py`), #3581 (workflows
  `tests.yml`), #3586 (comptes/jauge), #3557 (porte d'avis) ; les tests seuls
  (#3594, #3597) n'exigent pas de geste de déploiement.
