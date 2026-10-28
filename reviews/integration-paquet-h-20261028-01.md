# Paquet integration/paquet-h-20261028-01 — composition (§12), sur main `904cefc74acf`

Base : `main` **`904cefc74acf6b18c2e5133a776e6e18719bc5ac`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261028-01`, cible `main`.
Porteur : **#4099**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #4089 | `308d5e12761e6c3acca088189bb6ee140a6a9b44` |
| #4087 | `20f720da429f9103c5e695e4cc3c57a615865a2b` |
| #4094 | `8b911e66a7484d06c64409ef5dcdcb9d66142568` |
| #4090 | `303c817853de02de1159a92f9a7d5f15d699ffae` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4089 | `308d5e12761e` | `8bcaf7a0823c` | composition |
| 2 | #4087 | `20f720da429f` | `1c804d058956` | composition |
| 3 | #4094 | `8b911e66a748` | `a3f270ca3507` | composition |
| 4 | #4090 | `303c817853de` | `c5e6b1fd0032` | composition |

## Écartés

| lot | motif |
|---|---|
| #4086 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |

Liste calculée : 4 lot(s) prêt(s), 7 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 18.7 s.
- Tests locaux : 4 séries, 15 modules, 504 tests exécutés, 0 échec(s), 0 run(s) rouge(s) sans effet, 0 non concluant(s), 10 sauté(s) : `tests.test_1155_staging`, `tests.test_2340_auditeur_role`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_339_orphan_settle`, `tests.test_3752_gabarit`, `tests.test_3845_paquet_horaire`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l107_executor_contract`, `tests.test_l161f_plan`, `tests.test_l31_orphans`, `tests.test_l50_group_orphan_work_items`, `tests.test_l72_panne_de_base`, `tests.test_migrations`.

Modules voisins joués (support importé, carte d'impact) : carte d'impact `6d67072c3787` (lue, antérieure à la base) ; budget de carte 180 s par unité, 0 s pris.

Aucun module voisin ajouté.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
