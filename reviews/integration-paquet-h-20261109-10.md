# Paquet integration/paquet-h-20261109-10 — composition (§12), sur main `77849f0e82bc`

Base : `main` **`77849f0e82bc3ad3a939f60d4d6304637a09ea74`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-10`, cible `main`.
Porteur : **#6515**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6473 | `66bdff21de72` |
| #6477 | `fae100132c6b` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6473 | `66bdff21de72` | `a85975bedaf8` | composition |
| 2 | #6477 | `fae100132c6b` | `6dd22aa94fa0` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 69.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
