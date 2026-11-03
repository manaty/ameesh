# Paquet integration/paquet-h-20261103-11 — composition (§12), sur main `b9ac244282a8`

Base : `main` **`b9ac244282a8d844a88255c37e1ca0233d9aa6e2`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-11`, cible `main`.
Porteur : **#5596**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5599 | `4f9709045c2a` |
| #5582 | `494dcf84b669` |
| #5586 | `101d91cd3384` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5599 | `4f9709045c2a` | `d884bf5146c2` | composition |
| 2 | #5582 | `494dcf84b669` | `468cd490997f` | composition |
| 3 | #5586 | `101d91cd3384` | `f81644282939` | composition |

## Écartés

| lot | motif |
|---|---|
| #5585 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #5595 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |

Liste calculée : 5 lot(s) prêt(s), 4 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 39.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
