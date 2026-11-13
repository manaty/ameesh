# Paquet integration/paquet-h-20261113-16 — composition (§12), sur main `6c769f8a18d1`

Base : `main` **`6c769f8a18d1a5900db930ac2a93727b327837e2`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-16`, cible `main`.
Porteur : **#7265**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7249 | `aa9897e4e0bf` |
| #7252 | `b563211245a7` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7249 | `aa9897e4e0bf` | `763acf80b9e1` | composition |
| 2 | #7252 | `b563211245a7` | `6934ac3eefc6` | composition |

## Écartés

| lot | motif |
|---|---|
| #7034 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |
| #7269 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 3 lot(s) prêt(s), 1 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 53.7 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
