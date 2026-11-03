# Paquet integration/paquet-h-20261103-19 — composition (§12), sur main `e4228fecaa11`

Base : `main` **`e4228fecaa11a6c07aec9f6ccd648fa1bf330b91`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-19`, cible `main`.
Porteur : **#5677**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (8 unités, 8 lots)

| lot | source composée |
|---|---|
| #5673 | `aa9fd2d9e310` |
| #5676 | `9009d1e92966` |
| #5601 | `ab4b236fcca6` |
| #5633 | `3ae1af91f894` |
| #5642 | `6a9ac779f42e` |
| #5650 | `7084b47e9fdb` |
| #5661 | `9415e891d184` |
| #5669 | `0ba40693ff09` |

## Composition (8 unités, 8 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5673 | `aa9fd2d9e310` | `719bd3b26b87` | composition |
| 2 | #5676 | `9009d1e92966` | `918a09ffda9f` | composition |
| 3 | #5601 | `ab4b236fcca6` | `44c5cb24d898` | composition |
| 4 | #5633 | `3ae1af91f894` | `9f31233b7cd5` | composition |
| 5 | #5642 | `6a9ac779f42e` | `a257599f28f3` | composition |
| 6 | #5650 | `7084b47e9fdb` | `08ad4fa27da2` | composition |
| 7 | #5661 | `9415e891d184` | `53071f7fe450` | composition |
| 8 | #5669 | `0ba40693ff09` | `aba824159395` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 8 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 50.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
