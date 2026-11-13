# Paquet integration/paquet-h-20261113-09 — composition (§12), sur main `c7fd7a167c8a`

Base : `main` **`c7fd7a167c8afc4d6240998bdca112458eb9a7a2`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-09`, cible `main`.
Porteur : **#7213**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #7006 | `43fa3ffdfc21` |
| #7193 | `cef5e5d2bf89` |
| #7205 | `e9945c84afa6` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7006 | `43fa3ffdfc21` | `12380d3be163` | composition |
| 2 | #7193 | `cef5e5d2bf89` | `84b929b57619` | composition |
| 3 | #7205 | `e9945c84afa6` | `01ba0c03bc27` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 0 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 49.6 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
