# Paquet integration/paquet-h-20261029-05 — composition (§12), sur main `a565151532c1`

Base : `main` **`a565151532c117eda97a55e2c043d4894c1c621b`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-05`, cible `main`.
Porteur : **#4523**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (7 unités, 7 lots)

| lot | source composée |
|---|---|
| #4382 | `435312135a497fcb93cf97056708882eeee395d2` |
| #4423 | `03b0707cc7cc8d7d6a71e61349788be4034188d8` |
| #4488 | `482bc497854c55e229243bff1590e0d886d5c822` |
| #4319 | `e0aaa924461c23af7424d173e6fe7491b72e4c72` |
| #4465 | `1cf8645d444d5f9455717a6cbb7187308b80877e` |
| #4482 | `d6041a0690fd8bf62d911e2ef5c844494a3fb262` |
| #4486 | `150c5cc3957c82f7fdfd518e610962d1db392e06` |

## Composition (7 unités, 7 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4382 | `435312135a49` | `398f91b9bb26` | composition |
| 2 | #4423 | `03b0707cc7cc` | `43077067201b` | composition |
| 3 | #4488 | `482bc497854c` | `a21d5b3a3de9` | composition |
| 4 | #4319 | `e0aaa924461c` | `9f241f1fb75e` | composition |
| 5 | #4465 | `1cf8645d444d` | `7ee9ae76ef30` | composition |
| 6 | #4482 | `d6041a0690fd` | `ce82758b37c4` | composition |
| 7 | #4486 | `150c5cc3957c` | `5d5bb95d9348` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 7 lot(s) prêt(s), 13 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 34.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
