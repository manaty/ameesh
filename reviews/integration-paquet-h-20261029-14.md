# Paquet integration/paquet-h-20261029-14 — composition (§12), sur main `69e13a33d272`

Base : `main` **`69e13a33d272b67f0b8465604f2d323a13747964`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-14`, cible `main`.
Porteur : **#4721**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (14 unités, 14 lots)

| lot | source composée |
|---|---|
| #4679 | `b654c18c13e7` |
| #4719 | `8b9ed214d6d4` |
| #4392 | `143b67bbc75a` |
| #4393 | `0677cb3709e9` |
| #4394 | `d92896437ca2` |
| #4521 | `f9fcb1559d2e` |
| #4618 | `76225be8c981` |
| #4699 | `b77fc848b1c9` |
| #4704 | `c9bfc3dfc067` |
| #4724 | `4dc7fe6982e9` |
| #4728 | `f6ad2a2aed96` |
| #4733 | `b14d0e89632d` |
| #4619 | `618cf2497846` |
| #4625 | `803bf5343bc9` |

## Composition (14 unités, 14 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4679 | `b654c18c13e7` | `42f6e0e28557` | composition |
| 2 | #4719 | `8b9ed214d6d4` | `690b2baa9e44` | composition |
| 3 | #4392 | `143b67bbc75a` | `894f2081f0ab` | composition |
| 4 | #4393 | `0677cb3709e9` | `e82a4a15c4ca` | composition |
| 5 | #4394 | `d92896437ca2` | `c299e960f119` | composition |
| 6 | #4521 | `f9fcb1559d2e` | `1afd5393d087` | composition |
| 7 | #4618 | `76225be8c981` | `1fd49d4346df` | composition |
| 8 | #4699 | `b77fc848b1c9` | `e47bc85c8ead` | composition |
| 9 | #4704 | `c9bfc3dfc067` | `9637f2688840` | composition |
| 10 | #4724 | `4dc7fe6982e9` | `461a31d5a0c3` | composition |
| 11 | #4728 | `f6ad2a2aed96` | `561bd28ac18f` | composition |
| 12 | #4733 | `b14d0e89632d` | `4f940aba799f` | composition |
| 13 | #4619 | `618cf2497846` | `321bc6b7affc` | composition |
| 14 | #4625 | `803bf5343bc9` | `c554b59e09b8` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 14 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 21.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
