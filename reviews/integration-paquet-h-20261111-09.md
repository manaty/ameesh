# Paquet integration/paquet-h-20261111-09 — composition (§12), sur main `561f023fe4e4`

Base : `main` **`561f023fe4e47faf612bceacd50f7fa5ee66ae9e`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-09`, cible `main`.
Porteur : **#6851**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6828 | `5c7407cad115` |
| #6839 | `32feb6b8a2ef` |
| #6849 | `dcbc1e2629aa` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6828 | `5c7407cad115` | `9b365e37f63e` | composition |
| 2 | #6839 | `32feb6b8a2ef` | `cc35dd9064d5` | composition |
| 3 | #6849 | `dcbc1e2629aa` | `834a2e73855c` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 52.8 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
