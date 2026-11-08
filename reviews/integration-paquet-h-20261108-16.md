# Paquet integration/paquet-h-20261108-16 — composition (§12), sur main `10c19004923a`

Base : `main` **`10c19004923abbf9667fe742745393b5b655dceb`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-16`, cible `main`.
Porteur : **#6338**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #6319 | `2fee92a32f1f` |
| #6320 | `2e769c7beb73` |
| #6323 | `ed4e9e05f870` |
| #6311 | `54b7da517c34` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6319 | `2fee92a32f1f` | `6f3a5b792f88` | composition |
| 2 | #6320 | `2e769c7beb73` | `9b797880769a` | composition |
| 3 | #6323 | `ed4e9e05f870` | `1245fe328ad2` | composition |
| 4 | #6311 | `54b7da517c34` | `518705037a38` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 49.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
