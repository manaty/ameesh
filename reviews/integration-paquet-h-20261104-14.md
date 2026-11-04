# Paquet integration/paquet-h-20261104-14 — composition (§12), sur main `da02f821a9e6`

Base : `main` **`da02f821a9e6ec28b2af89b8ebeb50732444b669`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-14`, cible `main`.
Porteur : **#5760**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5756 | `b4247b5a118c` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5756 | `b4247b5a118c` | `b80fd40ace28` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 36.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
