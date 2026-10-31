# Paquet integration/paquet-h-20261031-15 — composition (§12), sur main `52624a358e18`

Base : `main` **`52624a358e1849b93367847ee05083b1fae53d0b`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-15`, cible `main`.
Porteur : **#5128**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5100 | `c0c69e7ade7d` |
| #5115 | `a22538582ee8` |
| #5117 | `50bd1ffa455a` |
| #5119 | `446d128c02b6` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5100 | `c0c69e7ade7d` | `afc2fda26f7a` | composition |
| 2 | #5115 | `a22538582ee8` | `1d4f70c00ff2` | composition |
| 3 | #5117 | `50bd1ffa455a` | `0f1494b87e8b` | composition |
| 4 | #5119 | `446d128c02b6` | `95b67950ef39` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 25.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
