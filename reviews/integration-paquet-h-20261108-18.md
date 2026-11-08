# Paquet integration/paquet-h-20261108-18 — composition (§12), sur main `242ced700d6e`

Base : `main` **`242ced700d6ec1a63fc9d72643bc9faad175f72f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-18`, cible `main`.
Porteur : **#6369**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6347 | `f5ed9138e63d` |
| #6332 | `504416318b88` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6347 | `f5ed9138e63d` | `7f462da70b71` | composition |
| 2 | #6332 | `504416318b88` | `457270769367` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 76.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
