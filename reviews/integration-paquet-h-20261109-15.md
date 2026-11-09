# Paquet integration/paquet-h-20261109-15 — composition (§12), sur main `d92f3465ec42`

Base : `main` **`d92f3465ec42244957c708ba3dc5be0b5cb9a5cd`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-15`, cible `main`.
Porteur : **#6545**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #6520 | `6b6e6b4639d1` |
| #6525 | `df8bfe2d06a4` |
| #6528 | `0422fb8e2c9a` |
| #6484 | `a852c9356229` |
| #6538 | `80e1f9a15284` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6520 | `6b6e6b4639d1` | `8dc9bf70ebea` | composition |
| 2 | #6525 | `df8bfe2d06a4` | `2017d740dbea` | composition |
| 3 | #6528 | `0422fb8e2c9a` | `f16c881cf8e3` | composition |
| 4 | #6484 | `a852c9356229` | `caa740f5de0e` | composition |
| 5 | #6538 | `80e1f9a15284` | `16bc69d8fa47` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 83.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
