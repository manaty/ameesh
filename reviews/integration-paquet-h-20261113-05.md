# Paquet integration/paquet-h-20261113-05 — composition (§12), sur main `a8ef950b24bf`

Base : `main` **`a8ef950b24bfc2cbad2d36f9fa0d0bec1b5108e1`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-05`, cible `main`.
Porteur : **#7191**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #7174 | `652d835bfb1c` |
| #6863 | `b02c4058dbee` |
| #7149 | `1009d51434ad` |
| #7090 | `a91c378c5403` |
| #7177 | `c49911ea6afc` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7174 | `652d835bfb1c` | `02fd0078fe0f` | composition |
| 2 | #6863 | `b02c4058dbee` | `1305d88d5936` | composition |
| 3 | #7149 | `1009d51434ad` | `18b26af46134` | composition |
| 4 | #7090 | `a91c378c5403` | `8428e58429bf` | composition |
| 5 | #7177 | `c49911ea6afc` | `ac09f4dd9bf1` | composition |

## Écartés

| lot | motif |
|---|---|
| #7052 | rouge CI 43373367117 : test_1270 CadenceTest et test_5976 (lease_ttl absent des doublures), reproduit sur c48117fe6 seul ; regelé 9fa856dacb69 par dev5, coupure suivante — --exclude, recomposition sans lui |
| #7023 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7196 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7175 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7185 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7189 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 10 lot(s) prêt(s), 4 en attente, 6 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 49.5 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
