# Paquet integration/paquet-h-20261115-03 — composition (§12), sur main `9bf362424a1d`

Base : `main` **`9bf362424a1dccb61de9a07ef9417b07bd225e5e`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261115-03`, cible `main`.
Porteur : **#7453**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #7430 | `c62f2a32ac32` |
| #7439 | `93d3bf3fccc2` |
| #7443 | `958c4e980e7e` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7430 | `c62f2a32ac32` | `50aaf0cd0541` | composition |
| 2 | #7439 | `93d3bf3fccc2` | `7c95a7688950` | composition |
| 3 | #7443 | `958c4e980e7e` | `4b264167d9e0` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 66.8 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
