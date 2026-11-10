# Paquet integration/paquet-h-20261110-14 — composition (§12), sur main `58bd9e34df8f`

Base : `main` **`58bd9e34df8fdfe1f995fcc768290b5a688d4bd7`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-14`, cible `main`.
Porteur : **#6773**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6667 | `45278f32cb91` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6667 | `45278f32cb91` | `d7fbf1a10a6e` | composition |

## Écartés

| lot | motif |
|---|---|
| #6765 | CI rouge : tests.test_3532_fire_requeue_takeout (setUp appelle fire.give sans repo ; nouveau refus « no repository » de #6765) ; adaptation du témoin à faire par l'auteur — --exclude, recomposition sans lui |
| #6769 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6777 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6776 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6671 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 5 lot(s) prêt(s), 4 en attente, 5 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 50.9 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
