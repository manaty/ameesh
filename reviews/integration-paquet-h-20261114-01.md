# Paquet integration/paquet-h-20261114-01 — composition (§12), sur main `ed5b0c98009c`

Base : `main` **`ed5b0c98009c3fc5ea1b6702837f6569bbf9fd70`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-01`, cible `main`.
Porteur : **#7289**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7047 | `0c6e726a7e53` |
| #7186 | `6dbc64127431` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7047 | `0c6e726a7e53` | `95d1268228f0` | composition |
| 2 | #7186 | `6dbc64127431` | `670e5c064d6e` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 0 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 49.6 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
