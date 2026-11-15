# Paquet integration/paquet-h-20261115-11 — composition (§12), sur main `10a258cebc95`

Base : `main` **`10a258cebc95d45bc684a1d64854076f52d2c592`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261115-11`, cible `main`.
Porteur : **#7496**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7490 | `6c044e84aa15` |
| #7494 | `eb9c652d8f34` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7490 | `6c044e84aa15` | `a729ad1ac91d` | composition |
| 2 | #7494 | `eb9c652d8f34` | `fe90641acfe6` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 55.7 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
