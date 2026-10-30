# Paquet integration/paquet-h-20261030-14 — composition (§12), sur main `0382fd10e903`

Base : `main` **`0382fd10e90325a86a64dfe838341ad253e02e55`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-14`, cible `main`.
Porteur : **#4925**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (8 unités, 8 lots)

| lot | source composée |
|---|---|
| #4809 | `6c537a00770a` |
| #4899 | `754c6d8c4f87` |
| #4900 | `5d064e0e9ca2` |
| #4834 | `30ebce999f16` |
| #4868 | `2a511a2eb912` |
| #4870 | `ac922ac4260b` |
| #4871 | `c0c270f55766` |
| #4887 | `75ef1a500202` |

## Composition (8 unités, 8 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4809 | `6c537a00770a` | `f4b2136989c0` | composition |
| 2 | #4899 | `754c6d8c4f87` | `f9ec8411979e` | composition |
| 3 | #4900 | `5d064e0e9ca2` | `cb341e673957` | composition |
| 4 | #4834 | `30ebce999f16` | `47724816b5b1` | composition |
| 5 | #4868 | `2a511a2eb912` | `fbb297b7b25d` | composition |
| 6 | #4870 | `ac922ac4260b` | `7579190f3403` | composition |
| 7 | #4871 | `c0c270f55766` | `8453923f98ca` | composition |
| 8 | #4887 | `75ef1a500202` | `3832aad31c1c` | composition |

## Écartés

| lot | motif |
|---|---|
| #4888 | recouvre #4353 (exploitation.py) : après la passe 0518 (bureau #170135) — --exclude, recomposition sans lui |
| #4880 | recouvre #4353 (exploitation.py, notify.py) : après la passe 0518 (bureau #170135) — --exclude, recomposition sans lui |
| #4901 | recouvre #4353 (lot_sessions.py) : après la passe 0518 (bureau #170135) — --exclude, recomposition sans lui |
| #4791 | conflit avec #4913 (review_authors.py) : #4913 débloque le gel de la release Nexlink 11ag, #4791 passe après la passe 0518 — --exclude, recomposition sans lui |
| #4913 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 9 lot(s) prêt(s), 7 en attente, 5 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 20.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
