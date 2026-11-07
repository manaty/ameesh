# Paquet integration/paquet-h-20261107-28 — composition (§12), sur main `ec064b0e5ed2`

Base : `main` **`ec064b0e5ed2eaca7e747c45620d712ef1c4a1be`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-28`, cible `main`.
Porteur : **#6230**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6183 | `749da3b76a4e` |
| #6228 | `ca651174be89` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6183 | `749da3b76a4e` | `ccc8c59496fa` | composition |
| 2 | #6228 | `ca651174be89` | `db3b1f30ffa5` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 50.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
