# Paquet integration/paquet-h-20261113-10 — composition (§12), sur main `a8b7d547a2f7`

Base : `main` **`a8b7d547a2f733cfd54c3231737bda30cea4a4f7`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-10`, cible `main`.
Porteur : **#7220**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #7212 | `65b86cf6bcec` |
| #6999 | `c7278c3ec2ad` |
| #7107 | `6b7f85d71675` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7212 | `65b86cf6bcec` | `5a2f2250ecdd` | composition |
| 2 | #6999 | `c7278c3ec2ad` | `4d540e7c89b5` | composition |
| 3 | #7107 | `6b7f85d71675` | `4effde30ce4a` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 0 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 64.1 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
