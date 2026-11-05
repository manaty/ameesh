# Paquet integration/paquet-h-20261105-27 — composition (§12), sur main `45def427e487`

Base : `main` **`45def427e487c9e755aff367b33605bbc8aaaa0a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-27`, cible `main`.
Porteur : **#5960**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5952 | `257c083d3a11` |
| #5955 | `e516c32aae00` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5952 | `257c083d3a11` | `0f87cf08f906` | composition |
| 2 | #5955 | `e516c32aae00` | `b8b30a315265` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
