# Paquet integration/paquet-h-20261030-15 — composition (§12), sur main `0382fd10e903`

Base : `main` **`0382fd10e90325a86a64dfe838341ad253e02e55`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-15`, cible `main`.
Porteur : **#4943**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (7 unités, 7 lots)

| lot | source composée |
|---|---|
| #4809 | `6c537a00770a` |
| #4899 | `754c6d8c4f87` |
| #4900 | `5d064e0e9ca2` |
| #4834 | `30ebce999f16` |
| #4871 | `c0c270f55766` |
| #4887 | `75ef1a500202` |
| #4913 | `5190ac2d4dbe` |

## Composition (7 unités, 7 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4809 | `6c537a00770a` | `f9ad656688cc` | composition |
| 2 | #4899 | `754c6d8c4f87` | `f7025abd09f7` | composition |
| 3 | #4900 | `5d064e0e9ca2` | `86cd4109fadc` | composition |
| 4 | #4834 | `30ebce999f16` | `bcad676c6eff` | composition |
| 5 | #4871 | `c0c270f55766` | `2d7280dc48d4` | composition |
| 6 | #4887 | `75ef1a500202` | `288148f1b9e3` | composition |
| 7 | #4913 | `5190ac2d4dbe` | `e2eccf872327` | composition |

## Écartés

| lot | motif |
|---|---|
| #4888 | recouvre #4353 (exploitation.py) : après la passe 0518 (bureau #170135) — --exclude, recomposition sans lui |
| #4880 | recouvre #4353 (exploitation.py, notify.py) : après la passe 0518 (bureau #170135) — --exclude, recomposition sans lui |
| #4901 | recouvre #4353 (lot_sessions.py) : après la passe 0518 (bureau #170135) — --exclude, recomposition sans lui |
| #4791 | conflit avec #4913 (review_authors.py) : #4913 passe d abord (release Nexlink 11ag) — --exclude, recomposition sans lui |
| #4868 | rouge test_4259 au paquet #4925 : littéral client IMA dans canon.py, regelé depuis — --exclude, recomposition sans lui |
| #4870 | rouge test_l107_executor_contract au paquet #4943 : turn_costs.session_reread non classée dans le contrat de l exécuteur — --exclude, recomposition sans lui |
| #4353 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 8 lot(s) prêt(s), 6 en attente, 7 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
