# Paquet integration/paquet-h-20261105-31 — composition (§12), sur main `946b81a283c6`

Base : `main` **`946b81a283c6693f772c8dab5b3313db6dc347eb`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-31`, cible `main`.
Porteur : **#5984**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5970 | `9b432fa27687` |
| #5974 | `c456e4f36044` |
| #5968 | `b4e31a1d5b5c` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5970 | `9b432fa27687` | `514a26d2252c` | composition |
| 2 | #5974 | `c456e4f36044` | `09b00b89a074` | composition |
| 3 | #5968 | `b4e31a1d5b5c` | `5b8cc8ed8169` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 43.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
