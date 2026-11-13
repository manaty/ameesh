# Paquet integration/paquet-h-20261113-04 — composition (§12), sur main `8fe2a4868e4f`

Base : `main` **`8fe2a4868e4ff888e73ea4a212f3f274e3d6991d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-04`, cible `main`.
Porteur : **#7179**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6961 | `d83f1fa5aa8d` |
| #7164 | `61e7a13863fa` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6961 | `d83f1fa5aa8d` | `fb3fa8875181` | composition |
| 2 | #7164 | `61e7a13863fa` | `dbe9be246f21` | composition |

## Écartés

| lot | motif |
|---|---|
| #7149 | test_l61_allers_retours.test_mail_inbox vert sur main 8fe2a4868e4f et après #6961, rouge juste après l'unité #7149 (refus inbox d'une boîte execute) ; seul rouge du run 43361453005 — --exclude, recomposition sans lui |
| #7174 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6863 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7052 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7090 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7177 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 7 lot(s) prêt(s), 4 en attente, 6 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 54.2 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
