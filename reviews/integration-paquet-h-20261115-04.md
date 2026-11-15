# Paquet integration/paquet-h-20261115-04 — composition (§12), sur main `4c27900e42a6`

Base : `main` **`4c27900e42a674d7300b6906bbcc41ca9d225a2a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261115-04`, cible `main`.
Porteur : **#7459**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7442 | `b3bc9b6bc3e9` |
| #7438 | `b3bd86a93b7c` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7442 | `b3bc9b6bc3e9` | `f8c608fb30c6` | composition |
| 2 | #7438 | `b3bd86a93b7c` | `efaf9e6d5115` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 46.5 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
