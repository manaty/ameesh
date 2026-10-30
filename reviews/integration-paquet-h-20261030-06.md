# Paquet integration/paquet-h-20261030-06 — composition (§12), sur main `ede3fc612564`

Base : `main` **`ede3fc612564de899d48290d18f5b51e530abf98`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-06`, cible `main`.
Porteur : **#4789**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #4767 | `8c1069569920` |
| #4778 | `faf5e1b51375` |
| #4755 | `13355bdab1c6` |
| #4747 | `32d485850aa6` |
| #4769 | `8edff887498e` |
| #4782 | `12ebd6d1a824` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4767 | `8c1069569920` | `e21152c64c88` | composition |
| 2 | #4778 | `faf5e1b51375` | `3b6e1d403837` | composition |
| 3 | #4755 | `13355bdab1c6` | `31e46dc0d137` | composition |
| 4 | #4747 | `32d485850aa6` | `e9db8f31929a` | composition |
| 5 | #4769 | `8edff887498e` | `80541c012385` | composition |
| 6 | #4782 | `12ebd6d1a824` | `3e0f419cd868` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 6 lot(s) prêt(s), 7 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 24.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
