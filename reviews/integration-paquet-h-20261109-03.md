# Paquet integration/paquet-h-20261109-03 — composition (§12), sur main `1c403005bb70`

Base : `main` **`1c403005bb709216da0d68f6e012069d5a84e483`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-03`, cible `main`.
Porteur : **#6432**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6404 | `fb1d91e4a2db` |
| #6423 | `fea5e81a81d9` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6404 | `fb1d91e4a2db` | `34c5e0ffd97c` | composition |
| 2 | #6423 | `fea5e81a81d9` | `dc50020aeb67` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 74.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
