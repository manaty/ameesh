# Paquet integration/paquet-h-20261102-13 — composition (§12), sur main `bb8eb1355a31`

Base : `main` **`bb8eb1355a31690656d3a077fcc3ac8cc8400fcf`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-13`, cible `main`.
Porteur : **#5507**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5497 | `c1bf72f79799` |
| #5474 | `77ad3f2f0ab4` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5497 | `c1bf72f79799` | `67286be659da` | composition |
| 2 | #5474 | `77ad3f2f0ab4` | `8d3a9d179c42` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 9 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 39.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
