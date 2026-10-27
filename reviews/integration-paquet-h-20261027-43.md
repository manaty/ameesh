# Paquet integration/paquet-h-20261027-43 — composition (§12), sur main `bbcc474bf2e8`

Base : `main` **`bbcc474bf2e8c8806aaa9eb197656b334f13bcd9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-43`, cible `main`.
Porteur : **#4047**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #4028 | `2d80443a15e0aa702f26f621202348d4c6288f0e` |
| #4032 | `7d1f19a866e51638d8e4eb27cd0a2c4b53297dd1` |
| #4041 | `8b2758f645dbe4f9a0acb0781b91a3b45c47ccbd` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4028 | `2d80443a15e0` | `5ff51d2c7bd3` | composition |
| 2 | #4032 | `7d1f19a866e5` | `95e094e3e1ec` | composition |
| 3 | #4041 | `8b2758f645db` | `0477b82aab29` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 10 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 17.6 s.
- Tests locaux : 4 séries, 16 modules, 463 tests exécutés, 0 échec(s), 0 non concluant(s), 10 sauté(s) : `tests.test_2340_auditeur_role`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_339_orphan_settle`, `tests.test_3752_gabarit`, `tests.test_3845_paquet_horaire`, `tests.test_4041_vehicle_class_trace`, `tests.test_789_slo_alerts`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l161f_plan`, `tests.test_l31_orphans`, `tests.test_l3889_approve_js`, `tests.test_l50_group_orphan_work_items`, `tests.test_l72_panne_de_base`, `tests.test_migrations`.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
