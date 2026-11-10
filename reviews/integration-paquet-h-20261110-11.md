# Paquet integration/paquet-h-20261110-11 — composition (§12), sur main `9ec4061a94ba`

Base : `main` **`9ec4061a94baa952330e0998d91ed8f8ecdfd0c5`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-11`, cible `main`.
Porteur : **#6731**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6665 | `0fddeeeb1d81` |
| #6727 | `31abbff14c41` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6665 | `0fddeeeb1d81` | `d18eb39f7365` | composition |
| 2 | #6727 | `31abbff14c41` | `70abd16b0aca` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 70.1 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
