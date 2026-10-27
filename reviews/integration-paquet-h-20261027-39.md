# Paquet integration/paquet-h-20261027-39 — composition (§12), sur main `f4327171a91b`

Base : `main` **`f4327171a91b4673f37df641f9203b25495128e5`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-39`, cible `main`.
Porteur : **#3975**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #3925 | `a4ac290b145fd9366f7e3c4bbe40f3b8935eb025` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3925 | `a4ac290b145f` | `e52fd84d0dcc` | composition |

## Écartés

| lot | motif |
|---|---|
| #3963 | tests ciblés rouges (tests.test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue), attribués à son unité (#3801), retirés de la recomposition — migration src/ameesh/migrations/0498_canon_sync_claim.sql ; covered src/ameesh/canon_sync.py ; covered src/ameesh/runner.py |

Liste calculée : 2 lot(s) prêt(s), 16 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 35.7 s.
- Tests locaux : 2 séries, 13 modules, 342 tests exécutés, 0 échec(s), 0 non concluant(s), 10 sauté(s) : `tests.test_2340_auditeur_role`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_339_orphan_settle`, `tests.test_3752_gabarit`, `tests.test_3925_admission_dynamique`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l161f_plan`, `tests.test_l31_orphans`, `tests.test_l50_group_orphan_work_items`, `tests.test_l72_panne_de_base`, `tests.test_migrations`.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
