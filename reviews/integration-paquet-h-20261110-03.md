# Paquet integration/paquet-h-20261110-03 — composition (§12), sur main `779222317fac`

Base : `main` **`779222317fac5de17f67a0d4a1932e748a1a4bc3`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-03`, cible `main`.
Porteur : **#6633**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6605 | `fed1f83a9740` |
| #6607 | `12f2241ec705` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6605 | `fed1f83a9740` | `2c6936057381` | composition |
| 2 | #6607 | `12f2241ec705` | `476d8663b8cd` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 49.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
