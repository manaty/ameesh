# Paquet integration/paquet-h-20261029-07 — composition (§12), sur main `10101e69dcf8`

Base : `main` **`10101e69dcf89a1900292035fc3ca580f8ac1b78`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-07`, cible `main`.
Porteur : **#4586**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #4543 | `7fd8dd2d5dea` |
| #4422 | `cf4537bae9d5` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4543 | `7fd8dd2d5dea` | `01fafafe0501` | composition |
| 2 | #4422 | `cf4537bae9d5` | `83d0243c13dd` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 11 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
