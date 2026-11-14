# Paquet integration/paquet-h-20261114-06 — composition (§12), sur main `182f92d25190`

Base : `main` **`182f92d25190aaaca0b32436edf8e359a09f3e4d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-06`, cible `main`.
Porteur : **#7332**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #7324 | `031a09f23d16` |
| #7325 | `0fbd5628fff3` |
| #7327 | `74ce9ae81f11` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7324 | `031a09f23d16` | `77ce841835b5` | composition |
| 2 | #7325 | `0fbd5628fff3` | `2defee692b30` | composition |
| 3 | #7327 | `74ce9ae81f11` | `e148c1bf22f9` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 45.9 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
