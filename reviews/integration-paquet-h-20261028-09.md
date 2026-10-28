# Paquet integration/paquet-h-20261028-09 — composition (§12), sur main `cf660e540741`

Base : `main` **`cf660e5407419030c3c73f6461fecae29827f972`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261028-09`, cible `main`.
Porteur : **#4400**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (13 unités, 13 lots)

| lot | source composée |
|---|---|
| #4164 | `5844df4aa44e0e224624a952b7237f7bde5cbbec` |
| #4327 | `8a4d4ab712aedaed800e5f651dd6adf3bd297e15` |
| #4151 | `362e1d66287a076025f191c711a9a6e49f4039b2` |
| #4247 | `7d510f7db0642bd6a828949efb193fab8e901dd9` |
| #4275 | `760b20e0d6136fa864e0339686f0c415e9c1eb35` |
| #4292 | `8dcd48b21784c8d3c6fba611fd5bcdb15cbd9c64` |
| #4350 | `4fe276b8398d492268ad9cfcbbccc97b44c08da2` |
| #4354 | `cd317ae25302ad5230a7bbc32a85a50fa284c10e` |
| #4369 | `2bfd982a5d37b82dea43f966338e9624beb45ae6` |
| #4373 | `2dacb69be623177a8c1641a123f3d032b29aeb83` |
| #4387 | `25c32056d777469ad0814803a00473f8ca66a35a` |
| #4262 | `913e20424b6efe187ad18b5873125f23c05111f7` |
| #4340 | `ad7c9d64bb36e2609662b5e5d573bf34d0b89352` |

## Composition (13 unités, 13 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4164 | `5844df4aa44e` | `5cc82bd822a6` | composition |
| 2 | #4327 | `8a4d4ab712ae` | `8e9e2cd6d2b7` | composition |
| 3 | #4151 | `362e1d66287a` | `6ee1e18f9ad8` | composition |
| 4 | #4247 | `7d510f7db064` | `b5d79c531f00` | composition |
| 5 | #4275 | `760b20e0d613` | `baa2fbf0c25e` | composition |
| 6 | #4292 | `8dcd48b21784` | `86773e1c3c9c` | composition |
| 7 | #4350 | `4fe276b8398d` | `dee95079157a` | composition |
| 8 | #4354 | `cd317ae25302` | `6c52c00e0b13` | composition |
| 9 | #4369 | `2bfd982a5d37` | `09e30a3e86ab` | composition |
| 10 | #4373 | `2dacb69be623` | `ce965cbbd320` | composition |
| 11 | #4387 | `25c32056d777` | `2f6c1e42be0d` | composition |
| 12 | #4262 | `913e20424b6e` | `fff632251d36` | composition |
| 13 | #4340 | `ad7c9d64bb36` | `704486191dbd` | composition |

## Écartés

| lot | motif |
|---|---|
| #4291 | conflit, coupure suivante — fichiers : src/ameesh/mediated_executor/contract.json, tests/test_cluster.py |
| #4293 | conflit, coupure suivante — fichiers : tests/test_cluster.py |
| #4329 | conflit, coupure suivante — fichiers : scripts/paquet-horaire.py |
| #4377 | conflit, coupure suivante — fichiers : src/ameesh/host_idle.py |
| #4406 | conflit, coupure suivante — fichiers : src/ameesh/modules/registry.py |
| #4339 | conflit, coupure suivante — fichiers : deploy/mise-a-jour/backend-administrateur.py, deploy/mise-a-jour/compte-release.py |

Liste calculée : 19 lot(s) prêt(s), 7 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 19.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
