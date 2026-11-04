# Paquet integration/paquet-h-20261104-02 — composition (§12), sur main `db87cd6a820c`

Base : `main` **`db87cd6a820c2cd33ffd76f04db47a837f758fe9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-02`, cible `main`.
Porteur : **#5708**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5691 | `dc70deacedcf` |
| #5694 | `31b13563c107` |
| #5696 | `9e10efcd2b6d` |
| #5699 | `7d680f28be8f` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5691 | `dc70deacedcf` | `2a33b1c377a3` | composition |
| 2 | #5694 | `31b13563c107` | `1b95862c5f25` | composition |
| 3 | #5696 | `9e10efcd2b6d` | `a7df5384fc6d` | composition |
| 4 | #5699 | `7d680f28be8f` | `24021bfad662` | composition |

## Écartés

| lot | motif |
|---|---|
| #5693 | conflit, coupure suivante — fichiers : src/ameesh/staging_watch.py |

Liste calculée : 5 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 53.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
