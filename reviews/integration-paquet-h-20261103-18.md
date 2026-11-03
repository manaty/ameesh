# Paquet integration/paquet-h-20261103-18 — composition (§12), sur main `141868fb4f23`

Base : `main` **`141868fb4f2369c5f7df522340b70d15074745ed`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-18`, cible `main`.
Porteur : **#5674**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5665 | `16ccca030749` |
| #5666 | `9d4243e07c97` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5665 | `16ccca030749` | `d007b454d529` | composition |
| 2 | #5666 | `9d4243e07c97` | `fc47be616a3f` | composition |

## Écartés

| lot | motif |
|---|---|
| #5601 | FAIL réel en ci-full H18 (run 41688737841), reproduit en local : test_l100_dev_cli.WorkAliasTest (6 erreurs), TypeError handler() got an unexpected keyword argument 'cfg' ; cmd_work appelle plan_cli.run(db, args, cfg=cfg) depuis b0683d34 (#5601). Lot fautif retiré (§11 bis). — --exclude, recomposition sans lui |
| #5642 | Panier de #5601, empilé sur sa branche : il suit #5601. — --exclude, recomposition sans lui |
| #5673 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5633 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5653 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5661 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 6 lot(s) prêt(s), 2 en attente, 6 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 44.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
