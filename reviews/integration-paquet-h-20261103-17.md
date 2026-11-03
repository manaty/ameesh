# Paquet integration/paquet-h-20261103-17 — composition (§12), sur main `3046d0e17897`

Base : `main` **`3046d0e17897ad669a2990c17bfab3194a169814`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-17`, cible `main`.
Porteur : **#5668**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5641 | `3616a9ff7aa2` |
| #5655 | `a6191f88a800` |
| #5663 | `421f6956cde1` |
| #5634 | `e5deccd3106b` |
| #5643 | `08ac06e69a81` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5641 | `3616a9ff7aa2` | `8044bce19e27` | composition |
| 2 | #5655 | `a6191f88a800` | `3d3875074806` | composition |
| 3 | #5663 | `421f6956cde1` | `34053dbffa98` | composition |
| 4 | #5634 | `e5deccd3106b` | `aba295816824` | composition |
| 5 | #5643 | `08ac06e69a81` | `bf12030be0ee` | composition |

## Écartés

| lot | motif |
|---|---|
| #5601 | FAIL réel en ci-full H17 (run 41676026343) : test_2339_autotake_migration, TypeError Mock non itérable sur runner.sweep (appel _admission_order de #5601). Lot fautif retiré (§11 bis). — --exclude, recomposition sans lui |
| #5642 | Panier de #5601 (réservations de slots, runner.py), il suit #5601. — --exclude, recomposition sans lui |
| #5665 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 6 lot(s) prêt(s), 4 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 39.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
