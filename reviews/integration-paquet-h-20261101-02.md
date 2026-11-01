# Paquet integration/paquet-h-20261101-02 — composition (§12), sur main `a16f6910e2d1`

Base : `main` **`a16f6910e2d1f49358043acbdfb3ef20433ee81d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-02`, cible `main`.
Porteur : **#5210**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (11 unités, 11 lots)

| lot | source composée |
|---|---|
| #5179 | `cfe4928c34c0` |
| #5184 | `feb67c4337e7` |
| #5197 | `468f115a6bd7` |
| #5147 | `3e551bd36ecb` |
| #5149 | `ee1ff8bb9fab` |
| #5169 | `62bf8818c702` |
| #5198 | `72ea9c0fde67` |
| #5202 | `9d04f18882d3` |
| #4902 | `3228d6f8199c` |
| #4976 | `c6569fdf4360` |
| #5170 | `5ec6bb076c7b` |

## Composition (11 unités, 11 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5179 | `cfe4928c34c0` | `622dbc8de857` | composition |
| 2 | #5184 | `feb67c4337e7` | `1b79610d534b` | composition |
| 3 | #5197 | `468f115a6bd7` | `9ddeeda3d125` | composition |
| 4 | #5147 | `3e551bd36ecb` | `de7bd857acef` | composition |
| 5 | #5149 | `ee1ff8bb9fab` | `d1dc8fc6dc10` | composition |
| 6 | #5169 | `62bf8818c702` | `a0f476eec83d` | composition |
| 7 | #5198 | `72ea9c0fde67` | `2da653d1f75b` | composition |
| 8 | #5202 | `9d04f18882d3` | `73fd1736492f` | composition |
| 9 | #4902 | `3228d6f8199c` | `b67de6c1415a` | composition |
| 10 | #4976 | `c6569fdf4360` | `3b8fcbe70526` | composition |
| 11 | #5170 | `5ec6bb076c7b` | `b05b484f34ec` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 11 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 29.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
