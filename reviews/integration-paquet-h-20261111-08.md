# Paquet integration/paquet-h-20261111-08 — composition (§12), sur main `561f023fe4e4`

Base : `main` **`561f023fe4e47faf612bceacd50f7fa5ee66ae9e`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-08`, cible `main`.
Porteur : **#6846**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6832 | `a34a07abb46c` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6832 | `a34a07abb46c` | `63b623223229` | composition |

## Écartés

| lot | motif |
|---|---|
| #6834 | tests ciblés rouges (tests.test_2552_revue_source.RevueSourceTest.test_les_deux_temoins_du_18_10_sont_refuses_a_la_meme_famille, tests.test_l165b_agents_libres.IdleCapacityTest.test_un_lot_de_session_d_un_autre_en_cours_occupe, tests.test_l165b_agents_libres.IdleCapacityTest.test_une_consigne_en_attente_occupe), situés au premier préfixe de l'empilement, retirés de la recomposition — préfixe base+u1…u2 (premier rouge à #6834) |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 45.6 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
