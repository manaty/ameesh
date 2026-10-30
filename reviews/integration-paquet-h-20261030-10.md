# Paquet integration/paquet-h-20261030-10 — composition (§12), sur main `3c1f11ef0f51`

Base : `main` **`3c1f11ef0f519067f64925d025b18e2054114d8a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-10`, cible `main`.
Porteur : **#4856**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (9 unités, 9 lots)

| lot | source composée |
|---|---|
| #4779 | `de082d6844a9` |
| #4791 | `fdd809f5a7f1` |
| #4793 | `44ea85a58c79` |
| #4819 | `83898bfac1c5` |
| #4829 | `6566734cb924` |
| #4831 | `c3ced059152f` |
| #4834 | `32dbff49954a` |
| #4786 | `d520ede94d04` |
| #4830 | `b69938e00791` |

## Composition (9 unités, 9 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4779 | `de082d6844a9` | `d75b11e08e04` | composition |
| 2 | #4791 | `fdd809f5a7f1` | `aece9e430853` | composition |
| 3 | #4793 | `44ea85a58c79` | `a282f70c840f` | composition |
| 4 | #4819 | `83898bfac1c5` | `9c1261f02f5f` | composition |
| 5 | #4829 | `6566734cb924` | `6394c2c00dd7` | composition |
| 6 | #4831 | `c3ced059152f` | `535dd956130e` | composition |
| 7 | #4834 | `32dbff49954a` | `1ee75624e425` | composition |
| 8 | #4786 | `d520ede94d04` | `f1ad52c8549e` | composition |
| 9 | #4830 | `b69938e00791` | `447161b3f3bb` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 9 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 24.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
