# Paquet integration/paquet-h-20261027-36 — composition (§12), sur main `b875952cf208`

Base : `main` **`b875952cf208aca112526dfd786ef4d4a35a9f79`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-36`, cible `main`.
Porteur : **#3929**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #3907 | `d2b8cd2b7d3bb9a786438404a73ae726048e2086` |
| #3905 | `3b80c375b702bdcd5cdcd3b136699dfd1737f21b` |
| #3910 | `afa2b121090d9fbb5ffa2ab28113262b7fe87ebd` |
| #3881 | `797d476b9a2f79881f0357a8329b4d86cdfc238b` |
| #3904 | `b41475191c2a7ac38971a653160593882dc00a41` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3907 | `d2b8cd2b7d3b` | `4e1c4ae07347` | composition |
| 2 | #3905 | `3b80c375b702` | `f53390ef20f0` | composition |
| 3 | #3910 | `afa2b121090d` | `70cc45d2a1c7` | composition |
| 4 | #3881 | `797d476b9a2f` | `f8359a71c1b8` | composition |
| 5 | #3904 | `b41475191c2a` | `8873525199bf` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 10 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 45.9 s.
- Tests locaux : 6 séries, 17 modules, 435 tests exécutés, 0 échec(s), 0 non concluant(s), 10 sauté(s) : `tests.test_2340_auditeur_role`, `tests.test_2443_packet_members`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_339_orphan_settle`, `tests.test_3752_gabarit`, `tests.test_3904_remote_tip_batch`, `tests.test_3907_stop_sweep_light_read`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l112_host_gate`, `tests.test_l161f_plan`, `tests.test_l31_orphans`, `tests.test_l50_group_orphan_work_items`, `tests.test_l72_panne_de_base`, `tests.test_migrations`, `tests.test_runner`.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
