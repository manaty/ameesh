# Paquet integration/paquet-h-20261018-1400 — composition

Base : `main` **`b50b2be2b586`** (fusion de la PR #1832, paquet h-1300,
18/10 13:58Z ; CI complète `39130743714` verte sur `5fb2bc96d`). Branche
`integration/paquet-h-20261018-1400`, cible `main`, PR à ouvrir. Une seule
suite complète est demandée sur la tête de ce manifeste.

Demandes : liste calculée des lots prêts (#2404) sur la base `b50b2be2b`
(**8 prêts, 30 en attente, 0 erreur**) ; admissions du pilote 18/10 13:40Z
(#2384, #2449, #2359 ; #2452 hors paquet ; #1349 conditionnel) ; têtes
doc-links de mty-amh-docs-01 14:03Z (#836, #1406).

## Admissions

- Pilote 13:40Z : **#2384, #2449, #2359** ; #2452 (`manaty/home#111`, hors
  paquet ameesh) ; #1349 seulement si le verdict dev7 était rendu.
- mty-amh-docs-01 14:03Z : **#836, #1406**.

## Composition (8 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #836 | `c2104bedc2af` (tête gelée) | `9d263f78e70c` | DOC-LINKS-TEST-TMP : nettoyer les arbres temporaires du vérificateur ; léger, relecture après fusion (avis cx1 en trace) ; 1 f. +3/−1 |
| 2 | #1406 | `79f876afbf7a` (tête gelée, rebasée sur `b50b2be2b`) | `7c38e8ed2c70` | liens de documentation : mieux analyser les blocs et les ancres ; léger, 0 avis requis, relecture après fusion ; 2 f. +176/−56 |
| 3 | #2326 | `fb7261817334` (tête gelée) | `ec19311fa96e` | ménage tmp : l'exécuteur ima supprime les worktrees manaty créés à la racine partagée ; léger, avis dev2 ; 3 f. +81/−4 |
| 4 | #2384 | `e5571517ec89` (tête gelée) | `0d771a616e7f` | relecture après fusion : expliquer le cas auteur sans famille tracée ; sensible (porte de fusion), avis dev1+cx1 ; 3 f. +79/−7 |
| 5 | #2444 | `ea1233741e1a` (tête gelée) | `da2a83b399d9` | sync-merges, gel d'origine verdict : trois gardes ; sensible (SQL/stockage), avis cx2+impl-05 ; 5 f. +84/−15 |
| 6 | #2447 | `eb04aa8a9d50` (tête gelée) | `586ecae3d916` | désignation automatique des relecteurs au jalon frozen ; sensible (porte de fusion), avis dev7+cx2 ; 6 f. +1674/−31 |
| 7 | #2448 | `030d909f706f` (tête gelée) | `85e1560e643f` | release 11:00/23:00 sans agent : tag de la tête verte de main ; sensible (déploiement), avis dev1+cx3 ; 9 f. +2620/−3 |
| 8 | #2453 | `7309a364c821` (tête gelée) | `1d3f8516ce4c` | #2421 N : `write_usage_event`, une ligne partielle ne rend plus illisible `resident.turn` ; léger, avis cx1 ; 2 f. +58/−5 |

Chaque commit de composition a le lot en second parent, son sujet nomme le
lot et il porte un trailer `ameesh-work: <lot>` (8 trailers de composition).
Aucun conflit : les deux fichiers touchés par deux unités
(`src/ameesh/work.py` par #2444 et #2447, `tests/test_doc_links.py` par #836
et #1406) ont été auto-fusionnés et sont couverts par les témoins ci-dessous.

## Écartés

| lot | motif |
|---|---|
| #2449 | admission pilote 13:40Z ; calcul frais : **1/2 avis** (dev7/Claude), classe sensible (garde CI) ; aucun conflit — à reprendre après le second avis |
| #2359 | admission pilote 13:40Z ; calcul frais : **1/2 avis** (cx3/Codex) et conflit `deploy/mise-a-jour/poste.sh` sur la tête de branche `6ab07fac` ; remise `a4b17607` mergeable sur h-1300 mais second avis manquant |
| #1349 | verdict dev7 toujours absent au calcul (`e6c11182`) et conflits `docs/EXECUTEUR-MEDIEE.md`, `tests/test_l107_executor_contract.py` |
| #2452 | PR `manaty/home#111` (dépôt canon), fusion par le bureau — pas une unité du paquet ameesh |

Les 30 autres lots en attente au calcul ne sont ni composés ni dans cette
table : leur motif calculé (`verdicts-missing`, `conflict`, `freeze-*`,
`blocked-verdict`, `branch-not-published`) les rend non composables.

## Preuves

- `scripts/paquet-candidats.py --target main` sur la base `b50b2be2b` :
  **8 prêts, 30 en attente, 0 erreur**, rc 0 (clone complet dédié, jamais
  l'arbre des agents).
- `scripts/paquet-inclusion.py --manifest … --head HEAD --ready-file …` :
  **rc 0**, 8 unités composées, aucune omission ni contenu absent.
- `git diff --check b50b2be2b..HEAD` : rc 0, aucun marqueur de conflit.
- Aucune migration dans le paquet : `git diff --name-only b50b2be2b..HEAD`
  ne contient ni `*.sql` ni `src/ameesh/migrations/**`.
- Témoins sur l'arbre composé (`PYTHONPATH=src python -m unittest`, `TMPDIR`
  dédié) : **363 tests OK** —
  `tests.test_doc_links` 21 ; `tests.test_2447_review_designation_activation`
  + `tests.test_plan` + `tests.test_1104_review_lot_hors_file` +
  `tests.test_l539_review_lot` 143 ; `tests.test_l219_housekeeping_tmp` +
  `tests.test_l73_housekeeping` + `tests.test_1155_staging` +
  `tests.test_2448_release_cutoff` 152 ; `tests.test_l76_resident_runner` 47.
  `scripts/doc-links.py --root docs` : **169 fichiers, 0 lien cassé**.
- Staging : le paquet n'ajoute aucune migration ; les migrations `0418` et
  `0464` de h-1300 restent à appliquer par le bureau avant relance des
  exécuteurs. Aucun geste d'hôte de ce lot.

## CI

Suite complète unique (`ci-full`, mode full, avis 0 — composition mécanique)
demandée sur la tête de ce manifeste ; auto-merge `MERGE` armé sur cette tête.
