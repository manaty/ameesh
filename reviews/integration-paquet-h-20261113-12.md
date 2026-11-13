# Paquet integration/paquet-h-20261113-12 — composition (§12), sur main `b63013906ddf`

Base : `main` **`b63013906ddf628fd60f1fc7830b38087144acf2`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-12`, cible `main`.
Porteur : **#7243**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (7 unités, 7 lots)

| lot | source composée |
|---|---|
| #7066 | `c25347a07c55` |
| #7108 | `0860cdcba792` |
| #7229 | `989dea4347bf` |
| #7239 | `696b110f8bba` |
| #7148 | `283206b9b50c` |
| #7200 | `326f1af7f6ed` |
| #7233 | `d73a973c49ab` |

## Composition (7 unités, 7 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7066 | `c25347a07c55` | `c7fa29a377f4` | composition |
| 2 | #7108 | `0860cdcba792` | `729462b0bf75` | composition |
| 3 | #7229 | `989dea4347bf` | `0ccf0f9b2d38` | composition |
| 4 | #7239 | `696b110f8bba` | `6df27cc2ed5d` | composition |
| 5 | #7148 | `283206b9b50c` | `a4f4d1555723` | composition |
| 6 | #7200 | `326f1af7f6ed` | `039cf1e7100d` | composition |
| 7 | #7233 | `d73a973c49ab` | `9cab38030647` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 7 lot(s) prêt(s), 0 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 56.3 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
