# Paquet integration/paquet-h-20261104-03 — composition (§12), sur main `49372f0259ce`

Base : `main` **`49372f0259cec6ce791dde33f52c000648a9eb03`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-03`, cible `main`.
Porteur : **#5712**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5698 | `f7c53e20eb4b` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5698 | `f7c53e20eb4b` | `bf81272647f5` | composition |

## Écartés

| lot | motif |
|---|---|
| #5695 | FAIL réel en ci-full H04-03 (run 41767590381) : test_l107_executor_contract.TableTest.test_l_interface_entiere_est_classee — la nouvelle opération de stockage fingerprints.failing (#5695) n'est pas classée dans src/ameesh/mediated_executor/contract.json (ni admise ni refusée). Lot fautif retiré (§11 bis). — --exclude, recomposition sans lui |
| #5692 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5693 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 3 lot(s) prêt(s), 4 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 46.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
