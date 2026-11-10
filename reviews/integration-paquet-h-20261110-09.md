# Paquet integration/paquet-h-20261110-09 — composition (§12), sur main `669e6fe04140`

Base : `main` **`669e6fe04140fecf4ea4129dc3f8b93c31b4768e`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-09`, cible `main`.
Porteur : **#6723**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6677 | `ff1aa483eab0` |
| #6720 | `26e92b18a0cf` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6677 | `ff1aa483eab0` | `c7b8f77494d1` | composition |
| 2 | #6720 | `26e92b18a0cf` | `ac4696ec885c` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 73.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
