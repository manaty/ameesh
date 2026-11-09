# Paquet integration/paquet-h-20261109-16 — composition (§12), sur main `65c82625b113`

Base : `main` **`65c82625b113a03641bae38e4b687e827d42295a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-16`, cible `main`.
Porteur : **#6554**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6372 | `23fe3a8e8ad5` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6372 | `23fe3a8e8ad5` | `1c8b44702101` | composition |

## Écartés

| lot | motif |
|---|---|
| #6487 | test_l790 rouge déterministe : run_turn appelle _settle_runner_retry absent du faux worker (SimpleNamespace), reproduit en local sur 40044ed04be6 — --exclude, recomposition sans lui |
| #6534 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6544 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6537 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6556 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 5 lot(s) prêt(s), 2 en attente, 5 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 65.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
