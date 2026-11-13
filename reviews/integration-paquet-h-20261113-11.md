# Paquet integration/paquet-h-20261113-11 — composition (§12), sur main `37ddc5e0164c`

Base : `main` **`37ddc5e0164cf751f357c11858ad6dfd8fc61d6d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-11`, cible `main`.
Porteur : **#7235**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7183 | `a43365031c48` |
| #7211 | `5e4e12d59e3a` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7183 | `a43365031c48` | `8fca35b3e1a6` | composition |
| 2 | #7211 | `5e4e12d59e3a` | `56625eee2684` | composition |

## Écartés

| lot | motif |
|---|---|
| #7229 | tests ciblés rouges (tests.test_7120_attach_relay.TlsTest.test_session_tls), attribués à son unité (#3801), retirés de la recomposition — test tests/test_7120_attach_relay.py |
| #7066 | tests ciblés rouges (tests.test_cluster.ContratDesColonnesTest.test_toute_colonne_ajoutee_est_accordee_ou_exclue), attribués à son unité (#3801), retirés de la recomposition — test tests/test_cluster.py ; migration src/ameesh/migrations/0577_account_turn_exceptions.sql ; covered src/ameesh/account_turn.py |
| #7108 | coupure suivante : tête regelée — composé 586ee75e3ffd, tête actuelle 0860cdcba792 : pas de ci de paquet sur la nouvelle tête |
| #7200 | coupure suivante : tête regelée — composé 16454e995e60, tête actuelle 326f1af7f6ed : pas de ci de paquet sur la nouvelle tête |

Liste calculée : 6 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 45.1 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
