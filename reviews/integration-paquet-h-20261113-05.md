# Paquet integration/paquet-h-20261113-05 — composition (§12), sur main `a8ef950b24bf`

Base : `main` **`a8ef950b24bfc2cbad2d36f9fa0d0bec1b5108e1`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-05`, cible `main`.
Porteur : **#7191**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #7174 | `652d835bfb1c` |
| #6863 | `b02c4058dbee` |
| #7052 | `c48117fe6dcf` |
| #7149 | `1009d51434ad` |
| #7090 | `a91c378c5403` |
| #7177 | `c49911ea6afc` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7174 | `652d835bfb1c` | `0c6a5d489541` | composition |
| 2 | #6863 | `b02c4058dbee` | `f11b2f4f1f20` | composition |
| 3 | #7052 | `c48117fe6dcf` | `629a3f0c2ecd` | composition |
| 4 | #7149 | `1009d51434ad` | `3e5fa2b5c007` | composition |
| 5 | #7090 | `a91c378c5403` | `bdbb8bb9f3f8` | composition |
| 6 | #7177 | `c49911ea6afc` | `1a0e9e4c7828` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 6 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 52.2 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
