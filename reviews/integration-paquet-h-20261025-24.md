# Paquet integration/paquet-h-20261025-24 — composition (§12), sur main issu de h-23

Base : `main` **`336a64b006e7ad54e36ef2526623c85df1ff7bcc`** (fusion du paquet
h-23 v4, PR #2794, 25/10 21:31Z). Branche
`integration/paquet-h-20261025-24`, cible `main`. Porteur : **#3612**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : consigne mesh-design du 25/10 22:03Z — composer h-24 sans
attendre l'alerte de créneau, `main` rouge sous psql sur `test_3536`
(run 40235900946) et #3609 le corrige. Composition sur `336a64b0` : #3609 en
tête, puis #3601. #3608 n'entre pas : son gel `de07dd1e` n'a pas de verdict
(dev7 mort, remplacé par dev2 arrêté) ; il est reporté à h-25 sur la même
consigne. Liste calculée `paquet-candidats` sur `336a64b0` : 3 prêts
(#3581, #3601, #3609) ; #3581 est écarté par consigne pilote (siège codex de
07:27Z), #3601 et #3609 sont composés.

## Admissions (2 lots, 2 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3609 | `3e47d1b2644e` | 1/1 (auditeur claude) | test_3536 rouge sous psql : cryptography absent — sauter les cas de crypto, forcer la config |
| #3601 | `591d81a37fbc` | 1/1 (auditeur claude) | tests : PgTestCase échoue dans la classe qui laisse un global sensible patché |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3609 | 19436 | 19449 auditeur (claude) ok |
| #3601 | 19412 | 19416 auditeur (claude) ok |

## Composition (2 unités, 2 lots)

| unité | lot | source | commit |
|---|---|---|---|
| 1 | #3609 | `3e47d1b2644e` | `9088a38a616a` |
| 2 | #3601 | `591d81a37fbc` | `2704735b8540` |

Aucun conflit : chaque unité fusionne proprement sur `336a64b0` (`git
merge-tree`), puis l'une après l'autre. Contrôle d'inclusion
`scripts/paquet-inclusion.py` sur la tête du manifeste : voir la note de gel
du porteur.

## Écartés de h-24

| lot | motif |
|---|---|
| #3581 | hors composition par consigne mesh-design du 21:33Z : attend son siège codex de 07:27Z |
| #3608 | gel `de07dd1e` sans verdict ok (dev7 mort, dev2 arrêté) — reporté h-25 sur consigne pilote |
| #3575 | 0 avis sur le gel `ac6688bd5c56` (léger) |
| #3593 | 1 avis claude sur 2 requis (sensible) |
| #3576 | 1 avis claude sur 2 requis (sensible) |
| #3550 | 1 avis claude sur 2 requis (sensible) |
| #3606 | 1 avis claude sur 2 requis (sensible) |
| #3607 | 1 avis claude sur 2 requis (sensible) |
| #3592 | 1 avis claude sur 2 requis (sensible) |
| #3526 | 0 avis sur le gel `30a916ad44b0` (sensible) et conflit handoff (`handoff_factory.py`, `runner.py`, `test_3472_handoff_runner.py`) |
| #3572 | 1 avis deepseek sur 2 requis (indépendance non prouvée) |
| #3604 | 1 avis claude sur 2 requis (sensible) |
| #3584 | 1 avis claude sur 2 requis (sensible) et conflit `review_designation.py` |
| #3562 | 1 avis claude sur 2 requis (sensible) |
| #3544 | 1 avis deepseek sur 2 requis (indépendance non prouvée) |
| #3596 | 1 avis claude sur 2 requis (sensible) et conflit `review_designation_activation.py` |
| #3602 | 1 avis claude sur 2 requis (sensible) et conflit `review_designation_activation.py` |
| #3598 | 1 avis claude sur 2 requis (sensible) |
| #3589 | 1 avis claude sur 2 requis (sensible) |
| #3457 | hold pilote depuis 07:54Z et branche non publiée |
| #3474 | lecture de la porte incomplète (provenance d'auteur inconnue) — chaîne handoff |
| #3509 | lecture de la porte incomplète (provenance d'auteur inconnue) — chaîne handoff |
| #3473 | lecture de la porte incomplète (famille d'auteur ambiguë pour ameesh-cx2) — chaîne handoff |
| #3506 | lecture de la porte incomplète (provenance d'auteur inconnue) — chaîne handoff |
| #3519 | lecture de la porte incomplète (provenance d'auteur inconnue) — chaîne handoff |

Les cinq erreurs de lecture (#3473, #3474, #3506, #3509, #3519) sont la chaîne
handoff, hors h-24 par consigne pilote (déjà hors h-22/h-23 pour le même
motif) : elles ne rendent pas la liste des lots prêts incomplète pour ce
paquet.

## Contrôles

- Composition : 2/2 sans conflit sur `336a64b006e7ad54e36ef2526623c85df1ff7bcc` ; écart #3581 motivé ci-dessus, sans relire les autres unités (§11 bis).
- Liste calculée `paquet-candidats` sur `336a64b0` : **3 prêts** ; #3601 et
  #3609 composés, #3581 écarté avec motif ci-dessus.
- Une seule suite complète sur la tête gelée, après le gel (`ameesh ci run 3612 --full --reason "paquet h-24"`).
- Après fusion : `main` retrouve `test_3536` vert sous psql (#3609) et la
  garde des globaux sensibles (#3601) ; staging hors créneau si 3 lots ou plus
  attendent un correctif déjà fusionné (§11 bis).
