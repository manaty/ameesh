# Paquet integration/paquet-h-20261109-09 — composition (§12), sur main `121c991bf3a7`

Base : `main` **`121c991bf3a7f37f8ea6577daff67914d98a3122`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-09`, cible `main`.
Porteur : **#6472**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6453 | `5aa5f6d56675` |
| #6466 | `d01dad32c7d3` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6453 | `5aa5f6d56675` | `3b114ded561f` | composition |
| 2 | #6466 | `d01dad32c7d3` | `383942ec595f` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 67.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
