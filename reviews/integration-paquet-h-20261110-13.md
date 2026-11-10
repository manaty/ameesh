# Paquet integration/paquet-h-20261110-13 — composition (§12), sur main `bab0d9f0c5b8`

Base : `main` **`bab0d9f0c5b839dda2383d099dee9e3690aecbf5`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-13`, cible `main`.
Porteur : **#6762**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6674 | `0d5c5390fd6f` |
| #6735 | `c5503c70c9a1` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6674 | `0d5c5390fd6f` | `5efa40503723` | composition |
| 2 | #6735 | `c5503c70c9a1` | `67e6f18b2060` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 63.9 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
