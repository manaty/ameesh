# Paquet integration/paquet-h-20261115-08 — composition (§12), sur main `1685f91ab1cc`

Base : `main` **`1685f91ab1cc06759a4dffe317d7fde8fe4df797`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261115-08`, cible `main`.
Porteur : **#7479**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #7468 | `2b0abd33e630` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7468 | `2b0abd33e630` | `6fb4065cd0a5` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 60.1 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
