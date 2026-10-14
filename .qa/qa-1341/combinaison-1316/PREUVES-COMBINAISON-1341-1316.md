# #1341 × #1316 — preuves de combinaison sur main `2458ec05`

Préparé par `mty-amh-qa-01` (auteur #1341) le 2026-10-14 ~21:3xZ, en lecture
seule. Aucun push de branche, aucun run CI, aucune fusion, aucun élargissement
de gel. Ce bundle répond au point restant nommé par la R1 Codex
(`ameesh-cx1`, verdict du 21:20:09Z, jalon 4470) : « delta final de combinaison
1316 à relire ».

## Réfs

```
git fetch origin 'refs/atelier/1341/*:refs/atelier/1341/*' \
                 'refs/atelier/1316/*:refs/atelier/1316/*' \
                 'refs/atelier/combinaison-1341-1316/*:refs/atelier/combinaison-1341-1316/*'
```

- main de base : `2458ec053123d3f33644fef1e092fc84cc2773d9` (inchangée).
- union #1341 (gel v2 `a8213ee0`) : `7ca354a521ab6088e8aee5ddc552512d93ec61e6`,
  arbre `b45d4c59` (parents `a8213ee0` + main).
- union #1316 (gel `710f763dd`) : `0bcd2ae76c91b6b38d7c28b285d804ddf41e4491`,
  arbre `89e78315` (parents `710f763dd` + main).
- **combinaison** : `1cffaf46d091a0426e18aa215e9ac4b32cb67ba0`,
  arbre `0f336b9e223482b38fbc8398bbfb59c99973868d` (parents `7ca354a5` + `0bcd2ae7`),
  `merge-tree --write-tree` rc=0, zéro conflit, zéro résolution.

## Résultats (rejoués par `verif-combinaison.py`, verdict OK)

| Contrôle | Résultat |
|---|---|
| `merge-tree(union1341, union1316)` | rc=0, arbre unique `0f336b9e` |
| delta combinaison vs main | exactement les **6 fichiers** des deux lots, +773/−18, **0 migration** |
| delta combinaison vs `union1316` | == delta #1341 ; patch-ids **constants** std `3c87ed0c…` / -U0 `1dba6c87…` |
| delta combinaison vs `union1341` | == delta #1316 ; patch-ids **constants** std `54168a46…` / -U0 `307d2749…` |
| blobs propres à chaque lot | constants (seul `carte.yml` est fusionné) |
| rejeu séquentiel (index temporaire) : patch #1341 puis #1316 sur main | arbre `0f336b9e` == combinaison |

Les deux deltas traversent donc la combinaison **bit à bit** : les hunks des
deux lots dans `.github/workflows/carte.yml` sont disjoints (l'identité des
patch-ids en contexte fusionné le prouve), et aucun contenu de l'un n'est
écrasé par l'autre.

## Témoins ciblés sur l'arbre de combinaison (`wt-combo`)

| Commande | Résultat |
|---|---|
| `tests.test_carte_parts_completes` (#1341 v2) | **27 OK** (0,5 s) |
| `tests.test_l161e_map tests.test_l116_parts tests.test_l161f_plan` | **134 OK, 1 skip** (47,3 s) |
| `tests.test_1316_carte_dispatch tests.test_1314_carte_concurrency` (#1316) | **6 OK** (0,001 s) |

Note de lecture : les 134 tests (et non 115) viennent de la version de
`tests/test_l161f_plan.py` portée par main `2458ec05` (#1339 a ajouté 19 tests
à ce module) ; le gel v2 brut `a8213ee0` en compte 91. C'est la main courante
qui est testée ici, pas le gel seul — cohérent avec l'union.

Journaux : `tests-1341-combo.log`, `tests-1341b-combo.log`, `tests-1316-combo.log`.
Le worktree `wt-combo` (détaché sur `1cffaf46d`) est propre.

## Portée et limites

- Preuve **locale**, sur main `2458ec05` ; aucun push, aucun run, aucune CI.
  Le commit de combinaison et ce bundle sont publiés en `refs/atelier/**`
  (aucun workflow ne s'y déclenche).
- La composition finale d'un paquet §12 sera rejouée sur la main réelle au
  geste : cette preuve qualifie l'indépendance des deux deltas, elle ne
  remplace ni le gel du paquet, ni la R2 Claude de #1341, ni la R1/R2 propre
  de #1316, ni le feu du pilote.
- Aucun avis rendu ici : c'est une pièce à relire, pas un verdict.

## Rejeu

```
cd .qa/qa-1341
python3 combinaison-1316/verif-combinaison.py -v          # verdict OK attendu
cd wt-combo
PYTHONPATH=src python3 -m unittest tests.test_carte_parts_completes
PYTHONPATH=src python3 -m unittest tests.test_l161e_map tests.test_l116_parts tests.test_l161f_plan
PYTHONPATH=src python3 -m unittest tests.test_1316_carte_dispatch tests.test_1314_carte_concurrency
```
