# Paquet integration/paquet-h-20261108-09 — composition (§12), sur main `ce34e062ae04`

Base : `main` **`ce34e062ae04a8468be05e18574660b28ece1e70`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-09`, cible `main`.
Porteur : **#6290**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6274 | `d4668e55ea81` |
| #6287 | `1157dc070460` |
| #6270 | `d7b3d71cb38a` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6274 | `d4668e55ea81` | `7375124678c1` | composition |
| 2 | #6287 | `1157dc070460` | `fd14a811101a` | composition |
| 3 | #6270 | `d7b3d71cb38a` | `555d5685455d` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 48.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
