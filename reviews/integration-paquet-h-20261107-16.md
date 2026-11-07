# Paquet integration/paquet-h-20261107-16 — composition (§12), sur main `e4c2576d1e39`

Base : `main` **`e4c2576d1e393423b5f742b6760b0fba6457c711`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-16`, cible `main`.
Porteur : **#6170**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6152 | `77015c9a2f8a` |
| #6161 | `02f95af2fcce` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6152 | `77015c9a2f8a` | `fa1c44cbbb11` | composition |
| 2 | #6161 | `02f95af2fcce` | `897157017a11` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 63.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
