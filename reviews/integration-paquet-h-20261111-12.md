# Paquet integration/paquet-h-20261111-12 — composition (§12), sur main `7e4da28718b8`

Base : `main` **`7e4da28718b886a8ef3139d0449321836aa3235d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-12`, cible `main`.
Porteur : **#6886**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #6858 | `a81186a88663` |
| #6859 | `e339ffdb6b2b` |
| #6869 | `5b65c51511eb` |
| #6870 | `a1b357504db3` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6858 | `a81186a88663` | `dd69245e7ebb` | composition |
| 2 | #6859 | `e339ffdb6b2b` | `c2f19f3ebcac` | composition |
| 3 | #6869 | `5b65c51511eb` | `dc391933d03a` | composition |
| 4 | #6870 | `a1b357504db3` | `2f3c5313b0cc` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 54.9 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
