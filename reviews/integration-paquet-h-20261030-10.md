# Paquet integration/paquet-h-20261030-10 — composition (§12), sur main `3c1f11ef0f51`

Base : `main` **`3c1f11ef0f519067f64925d025b18e2054114d8a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-10`, cible `main`.
Porteur : **#4856**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #4779 | `de082d6844a9` |
| #4793 | `44ea85a58c79` |
| #4819 | `83898bfac1c5` |
| #4829 | `6566734cb924` |
| #4786 | `d520ede94d04` |
| #4830 | `b69938e00791` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4779 | `de082d6844a9` | `b4db35e6c2ff` | composition |
| 2 | #4793 | `44ea85a58c79` | `ed4d5b0397c5` | composition |
| 3 | #4819 | `83898bfac1c5` | `0d3808a4a21e` | composition |
| 4 | #4829 | `6566734cb924` | `a80d56cc3e7c` | composition |
| 5 | #4786 | `d520ede94d04` | `969ac84c8b43` | composition |
| 6 | #4830 | `b69938e00791` | `6d7c42003f12` | composition |

## Écartés

| lot | motif |
|---|---|
| #4791 | rouges test_3805 et test_3762 dès son préfixe 6c8da7ff7, verts sur main + #4779 (mesh-design) — --exclude, recomposition sans lui |
| #4822 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4831 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4851 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 9 lot(s) prêt(s), 9 en attente, 4 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 28.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
