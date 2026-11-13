# Paquet integration/paquet-h-20261113-07 — composition (§12), sur main `ca102ca66a50`

Base : `main` **`ca102ca66a5001af8a16a3682194bd475075a434`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-07`, cible `main`.
Porteur : **#7204**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #7023 | `cca5f5000349` |
| #7052 | `9fa856dacb69` |
| #7176 | `ca5a2cd9dda4` |
| #7175 | `a3197c54e898` |
| #7185 | `c5eac9112ee2` |
| #7189 | `8cfa07744156` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7023 | `cca5f5000349` | `4c6e1363b8ca` | composition |
| 2 | #7052 | `9fa856dacb69` | `b52182b04c08` | composition |
| 3 | #7176 | `ca5a2cd9dda4` | `1b09d071462b` | composition |
| 4 | #7175 | `a3197c54e898` | `deaac1e0799d` | composition |
| 5 | #7185 | `c5eac9112ee2` | `39d34b978550` | composition |
| 6 | #7189 | `8cfa07744156` | `98369bded1c5` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 6 lot(s) prêt(s), 6 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 59.1 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
