# Paquet integration/paquet-h-20261104-16 — composition (§12), sur main `1b1eedb799da`

Base : `main` **`1b1eedb799da26105718a8f31b65b9b3467851d1`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-16`, cible `main`.
Porteur : **#5767**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5751 | `bf6d18e924be` |
| #5759 | `ab12102779e3` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5751 | `bf6d18e924be` | `6199ec7c4e1c` | composition |
| 2 | #5759 | `ab12102779e3` | `75a89411f1d2` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
