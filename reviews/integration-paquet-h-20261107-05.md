# Paquet integration/paquet-h-20261107-05 — composition (§12), sur main `6f9ece43ff23`

Base : `main` **`6f9ece43ff233c0d5b5e301002bb429b0e810a21`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-05`, cible `main`.
Porteur : **#6097**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6043 | `bbaf57d5425d` |
| #6089 | `eeccbd19fbc9` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6043 | `bbaf57d5425d` | `7055f6569b1a` | composition |
| 2 | #6089 | `eeccbd19fbc9` | `e314cb19338d` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
