# Paquet integration/paquet-h-20261027-37 — composition (§12), sur main `ad042a0cd858`

Base : `main` **`ad042a0cd8582dc92d25b57e25677c0cbeef1e9e`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-37`, cible `main`.
Porteur : **#3940**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (7 unités, 7 lots)

| lot | source composée |
|---|---|
| #3920 | `e6349766bab2516569d5fbc56eec44fb675095e9` |
| #3923 | `76b8d00479ed0c92c5bc577720cdbd6205defa9a` |
| #3931 | `79331343591338f6d61079e5f027f6e25bb71b08` |
| #3933 | `5024637cec55f032edba317ea045d9a8141e42e8` |
| #3934 | `56864fd2f98cc095588276224eacf36b014692f8` |
| #3936 | `e13e7f8f6d575bc59d394e60980c116dc1de252c` |
| #3944 | `f76ba0b6ecfdf2fc983a096885494efea22d25a3` |

## Composition (7 unités, 7 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3920 | `e6349766bab2` | `a2bb930b801b` | composition |
| 2 | #3923 | `76b8d00479ed` | `f51e5b2b09f3` | composition |
| 3 | #3931 | `793313435913` | `ee2a6f2c8802` | composition |
| 4 | #3933 | `5024637cec55` | `80067cfc9938` | composition |
| 5 | #3934 | `56864fd2f98c` | `2953253e8b44` | composition |
| 6 | #3936 | `e13e7f8f6d57` | `b8abaaf1dae1` | composition |
| 7 | #3944 | `f76ba0b6ecfd` | `f35745b79fb0` | composition |

## Écartés

| lot | motif |
|---|---|
| #3925 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |
| #3916 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |
| #3885 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |

Liste calculée : 7 lot(s) prêt(s), 16 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 16.5 s.
- Tests locaux : 7 séries, 19 modules, 416 tests exécutés, 0 échec(s), 0 non concluant(s), 10 sauté(s) : `tests.test_1467_train_events`, `tests.test_2340_auditeur_role`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_339_orphan_settle`, `tests.test_3752_gabarit`, `tests.test_3792_menage_venvs`, `tests.test_3807_paquet_auto`, `tests.test_3923_listing_soldes`, `tests.test_3931_paquet_rouge_cible`, `tests.test_3936_release_no_freeze`, `tests.test_3944_grace_strays`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l161f_plan`, `tests.test_l31_orphans`, `tests.test_l50_group_orphan_work_items`, `tests.test_l72_panne_de_base`, `tests.test_migrations`.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
