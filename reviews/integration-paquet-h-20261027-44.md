# Paquet integration/paquet-h-20261027-44 — composition (§12), sur main `57718a568f21`

Base : `main` **`57718a568f21252c7d9e17e7285a3e1faf1393f4`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-44`, cible `main`.
Porteur : **#4052**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (10 unités, 10 lots)

| lot | source composée |
|---|---|
| #3961 | `56832beaa7352ffb5d204746fed675d469f13027` |
| #3892 | `31c8a35d7f532260ab77cce9a1c95c8167efe036` |
| #4007 | `96fd0ad802abaebb6c0562524f655be45be29889` |
| #4025 | `8a1b90369b0635e39e1a40bf2f728aa8ff5d3853` |
| #4036 | `12c0bfd1061842ac1fabf7758cdc95dbcd444e7b` |
| #4039 | `7037aadf760ed5454fa2d2668d05683ed1fcc601` |
| #4043 | `81689efea8d0278b1ed3347a093c178a69e03e30` |
| #4044 | `6be1bb2b4328feff62917f267534963d8aa186b7` |
| #3993 | `5b64add106485ccf5f19b0900f865d458448352f` |
| #4037 | `47dd9c77d995388f10cb939efa4c9058fd1bde50` |

## Composition (10 unités, 10 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3961 | `56832beaa735` | `418bf356e350` | composition |
| 2 | #3892 | `31c8a35d7f53` | `ea1ec38a3a6a` | composition |
| 3 | #4007 | `96fd0ad802ab` | `e34b10fa8598` | composition |
| 4 | #4025 | `8a1b90369b06` | `a4d91ecb74a6` | composition |
| 5 | #4036 | `12c0bfd10618` | `aacce934694f` | composition |
| 6 | #4039 | `7037aadf760e` | `705c4e8fb32f` | composition |
| 7 | #4043 | `81689efea8d0` | `f081d2f24223` | composition |
| 8 | #4044 | `6be1bb2b4328` | `487e81ffdf45` | composition |
| 9 | #3993 | `5b64add10648` | `cd053668fb03` | composition |
| 10 | #4037 | `47dd9c77d995` | `1d7f0fa3647d` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 10 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 15.7 s.
- Tests locaux : 11 séries, 32 modules, 997 tests exécutés, 0 échec(s), 0 non concluant(s), 16 sauté(s) : `tests.test_1425_stop_sweep_suites`, `tests.test_1770_prevalidation`, `tests.test_2340_auditeur_role`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_339_orphan_settle`, `tests.test_3752_gabarit`, `tests.test_3845_paquet_horaire`, `tests.test_3961_pause_echeance`, `tests.test_4007_revision_transfer`, `tests.test_4025_paquet_voisins`, `tests.test_4036_required_client_types`, `tests.test_4037_reviewed_requalification`, `tests.test_4044_readiness_vehicle_class`, `tests.test_789_slo_alerts`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l118c_mail_projects`, `tests.test_l124_decisions`, `tests.test_l160e2_land`, `tests.test_l161f_plan`, `tests.test_l31_orphans`, `tests.test_l50_group_orphan_work_items`, `tests.test_l72_panne_de_base`, `tests.test_l790_outdated_restart`, `tests.test_l79_chat_terminal`, `tests.test_l84_serve`, `tests.test_mail`, `tests.test_migrations`, `tests.test_process_registry`, `tests.test_process_verifiers_seam`, `tests.test_work`.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
