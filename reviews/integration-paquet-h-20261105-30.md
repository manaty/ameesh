# Paquet integration/paquet-h-20261105-30 — composition (§12), sur main `af552e6be32c`

Base : `main` **`af552e6be32cda5afe617d320382ea8f95e63119`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-30`, cible `main`.
Porteur : **#5975**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5954 | `825735b52b1b` |
| #5965 | `97858efc34ab` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5954 | `825735b52b1b` | `326876479df4` | composition |
| 2 | #5965 | `97858efc34ab` | `ea800aaeb88a` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
