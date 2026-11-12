# Paquet integration/paquet-h-20261112-02 — composition (§12), sur main `c731cd47d3fa`

Base : `main` **`c731cd47d3fa68a77657440c9b4a9faa196b4f89`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261112-02`, cible `main`.
Porteur : **#7014**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7008 | `e7a6b009587c` |
| #7009 | `e9758c86592d` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7008 | `e7a6b009587c` | `8f59af76445e` | composition |
| 2 | #7009 | `e9758c86592d` | `7f09c01e2dcc` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 19 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 55.0 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
