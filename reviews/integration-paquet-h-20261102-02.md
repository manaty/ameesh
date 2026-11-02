# Paquet integration/paquet-h-20261102-02 — composition (§12), sur main `433c7ceed1f1`

Base : `main` **`433c7ceed1f10e309c74926f02fce2692f04da0e`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-02`, cible `main`.
Porteur : **#5421**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5412 | `0ef1d9763ac5` |
| #5419 | `c18ef1c439af` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5412 | `0ef1d9763ac5` | `7f6d59638bc9` | composition |
| 2 | #5419 | `c18ef1c439af` | `88b79da91323` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 25.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
