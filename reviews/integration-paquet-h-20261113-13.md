# Paquet integration/paquet-h-20261113-13 — composition (§12), sur main `8d34b24307b1`

Base : `main` **`8d34b24307b1c9e2697bddca49fd0cf4cb324b28`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-13`, cible `main`.
Porteur : **#7250**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7246 | `d47f12317ad8` |
| #7245 | `500b0a8d2ddd` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7246 | `d47f12317ad8` | `74d8e666419c` | composition |
| 2 | #7245 | `500b0a8d2ddd` | `1d723c7a055a` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 52.0 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
