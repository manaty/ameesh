# #1341 — pièces de relecture R1 (gel, aucune CI)

Bundle destiné à la relecture indépendante (ameesh-cx1, Codex). Aucun push de
branche, aucun run CI, aucune fusion. La branche auteur `qa/1341-abd` reste
locale à `9f5bf357d` ; les objets sont publiés sous `refs/atelier/1341/*`.

## Réfs à récupérer

```
git fetch origin refs/atelier/1341/gel-candidat-9f5bf357d:refs/atelier/1341/gel-candidat-9f5bf357d \
                 refs/atelier/1341/union-main-2458ec05:refs/atelier/1341/union-main-2458ec05
```

| | |
|---|---|
| candidat (gel) | `9f5bf357d10613296a4396c2bdadce8c38919797` |
| arbre candidat | `309aa1eea0a53b6a17d000c63c3edf89627adb20` |
| base | `0532faed4` (main au moment du candidat, ancêtre de la main courante) |
| patch-id | `07ae9cced4ecb3e438e42d4f02e19d1efc54c035` |
| union main | `30d996d4bfc6e02c9167d099b67014c420e2f5b8` (parents `9f5bf357d` + `2458ec053123`) |
| arbre union | `2bfe03d5cd717cf191f8ccd53aa6d961ba953046` (`git merge-tree --write-tree` rc=0) |
| main courante | `2458ec053123d3f33644fef1e092fc84cc2773d9` |

Faits vérifiés : `union vs main` = exactement les 3 fichiers du lot
(`.github/workflows/carte.yml`, `scripts/map.py`,
`tests/test_carte_parts_completes.py`), patch-id `07ae9cce` inchangé ;
`union vs candidat` = exactement les 3 fichiers du delta de main
(`docs/EXPLOITATION.md`, `scripts/test-plan.py`, `tests/test_l161f_plan.py`) ;
blobs des 3 fichiers identiques candidat/union ; `candidat-ABD.patch` appliqué
sur `2458ec053123` (index temporaire) redonne l'arbre `2bfe03d5cd`.

## Contenu du bundle

- `CANDIDAT-ABD.md` — manifeste du candidat A/B/D (sha256 `d5ca7019…`).
- `candidat-ABD.patch` — pièce transférable, 395 lignes (sha256 `ba39d094…`).
- `RAPPORT-1341.md` — qualification d'origine (sha256 `2262ad60…`).
- `analysis-1341.py` / `.json` — analyse reproductible de la qualification.
- `analyse/simulate-1341b.py` / `.json` / `.log` — simulations A/B/C.
- `analyse/preuve-AB.py` / `.json` / `.log` — A0/S0 : CLI == simulation.
- `analyse/temoins-D.py` / `.log` / `resultats.json` — témoins de la garde D.
- `analyse/tests-cibles.log` — tests ciblés auteur (12 nouveaux + 115 existants OK, 1 skip).
- `analyse/flux-durations.py` / `c-psql-flux.json` — source C (durées des flux).
- `artifacts/` — entrées minimales des scripts (`durees-0532faed4.json`,
  `plan/plan.json`, `carte-96a795fb2/carte.json.gz`, `durees.json` et
  `flux.jsonl` des 5 parts de mesure) + `SHA256SUMS` d'origine.

## Rejeu (facultatif, lecture d'abord)

```
git worktree add wt-abd 9f5bf357d            # le candidat gelé
cd analyse
python3 simulate-1341b.py > simulate-1341b.log   # A/B/C
python3 preuve-AB.py > preuve-AB.log             # A0/S0, CLI == simulation
python3 temoins-D.py > temoins-D.log             # garde D
cd ../wt-abd
PYTHONPATH=src python3 -m unittest tests.test_carte_parts_completes -v          # 12 OK
PYTHONPATH=src python3 -m unittest tests.test_l161e_map tests.test_l116_parts tests.test_l161f_plan  # 115 OK, 1 skip
```

Attendus : `A0_reproduit_les_parts=true`, `S0_reproduit_le_plan=true`,
`A0_cli_egale_simulation=true`, `A1_cli_egale_simulation=true`,
`B0_cli_egale_simulation=true`, `B1_cli_egale_simulation=true`, `A0≠A1`,
`B0≠B1`, carte lue 248 durées psycopg / 0 psql, témoin
`avant-correctif-sans-garde` = code 0 et carte publiée à 5 modules (défaut
d'origine reproduit), les 5 autres scénarios D refusés.

## Portée

A et B sont des découpages CI ; D touche la publication (artefact + cache) et
relève de la porte de sécurité. Aucune suite complète, aucun run CI, aucun
rerun ; le push de la branche et la CI de la tête gelée viennent après les
verdicts requis et coordination pilote.
