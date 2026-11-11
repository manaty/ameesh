# Paquet integration/paquet-h-20261111-18 — composition (§12), sur main `ae04c08cf267`

Base : `main` **`ae04c08cf267eb0ccbd135f60cec6e6df3a1d7f4`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-18`, cible `main`.
Porteur : **#6963**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6958 | `002e19dd400f` |
| #6938 | `f13be2a1718d` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6958 | `002e19dd400f` | `61bb7e368015` | composition |
| 2 | #6938 | `f13be2a1718d` | `122e67ae7c2c` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 10 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 44.5 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
