# Paquet integration/paquet-h-20261109-13 — composition (§12), sur main `d92f3465ec42`

Base : `main` **`d92f3465ec42244957c708ba3dc5be0b5cb9a5cd`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-13`, cible `main`.
Porteur : **#6531**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6520 | `2d78dbceccc2` |
| #6525 | `df8bfe2d06a4` |
| #6484 | `a852c9356229` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6520 | `2d78dbceccc2` | `ca90badfe276` | composition |
| 2 | #6525 | `df8bfe2d06a4` | `6015b61f2e7d` | composition |
| 3 | #6484 | `a852c9356229` | `13ef9ef37a57` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 66.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
