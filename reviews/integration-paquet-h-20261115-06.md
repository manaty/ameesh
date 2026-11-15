# Paquet integration/paquet-h-20261115-06 — composition (§12), sur main `ec09ec054bb3`

Base : `main` **`ec09ec054bb3febc07f3d6ad7bedc8275aec248a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261115-06`, cible `main`.
Porteur : **#7469**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #7463 | `593da3130f0e` |
| #7432 | `a65e0bfebeb3` |
| #7445 | `8ab1379f4bc6` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7463 | `593da3130f0e` | `e449a29770e0` | composition |
| 2 | #7432 | `a65e0bfebeb3` | `12a6f33ae3d6` | composition |
| 3 | #7445 | `8ab1379f4bc6` | `d5a2d66ff405` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 51.6 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
