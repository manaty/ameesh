# Paquet integration/paquet-h-20261114-07 — composition (§12), sur main `10e6e907cf87`

Base : `main` **`10e6e907cf87519473ebf0c714fc6af4bf982c55`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-07`, cible `main`.
Porteur : **#7335**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #7121 | `0a776c3691c8` |
| #7318 | `bc5c71d9cbb2` |
| #7329 | `901000a81d95` |
| #7331 | `d7c3e172406c` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7121 | `0a776c3691c8` | `4308865afbce` | composition |
| 2 | #7318 | `bc5c71d9cbb2` | `71f6a5a94fa4` | composition |
| 3 | #7329 | `901000a81d95` | `34d2c4faf9b3` | composition |
| 4 | #7331 | `d7c3e172406c` | `e334c63730ec` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 66.8 s.
- Types clients : types clients : à jour (`gen-api --check`).
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
