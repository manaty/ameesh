# Paquet integration/paquet-h-20261113-15 — composition (§12), sur main `e36cbd14dbb7`

Base : `main` **`e36cbd14dbb72a2f0b7430516cc84ef41d4f3081`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-15`, cible `main`.
Porteur : **#7264**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #7259 | `9bc9caa5669e` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7259 | `9bc9caa5669e` | `60ea1ec65db9` | composition |

## Écartés

| lot | motif |
|---|---|
| #7034 | tests ciblés rouges (tests.test_l790_outdated_restart.SelectedTurnRefusedTest.test_run_turn_remet_la_consigne_et_refuse, tests.test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue), situés au premier préfixe de l'empilement, retirés de la recomposition — préfixe base+u1…u1 (premier rouge à #7034) ; migration src/ameesh/migrations/0571_spawn_profiles.sql ; covered src/ameesh/adapters.py |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 50.5 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
