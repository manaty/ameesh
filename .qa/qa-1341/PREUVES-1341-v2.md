# #1341 — pièces du gel v2 (correctif R1)

Bundle destiné à la relecture delta (R1 ameesh-cx1, puis R2 Claude). Aucun
push de branche, aucun run CI, aucune fusion. Le gel v1 (`9f5bf357d`) et ses
preuves restent publiés sous `refs/atelier/1341/gel-candidat-9f5bf357d` et
`refs/atelier/1341/preuves`.

## Réfs

```
git fetch origin refs/atelier/1341/gel-candidat-v2-a8213ee0 \
                 refs/atelier/1341/union-v2-main-2458ec05 \
                 refs/atelier/1341/preuves-v2
```

- candidat v2 : `a8213ee03293d08b708cfebc312188d3ac76651e` (parent v1),
  arbre `523c57f3b2a5387a25e7d9c6290d0e85ed5560bf`, patch-id complet
  `3c87ed0c77c29926bdbc336f55932f4785eaa363`, delta `623fd6b6d8091b1f0d660be247599c9e5343c950`.
- union main `2458ec053123` : commit `7ca354a521ab6088e8aee5ddc552512d93ec61e6`,
  arbre `b45d4c598fffe72adbca9e8cfb64ceb626a224de` (`merge-tree` rc=0) ; patch
  `candidat-ABD-v2.patch` (sha256 `84eb8f06…`) rejoué sur `2458ec05` (index
  temporaire) = arbre de l'union.

## Contenu

- `CANDIDAT-ABD-v2.md` — manifeste du gel v2 (hash des pièces et faits).
- `candidat-ABD-v2.patch`, `delta-R1-v2.patch` (sha256 `84eb8f06…`, `bcadbff7…`).
- `CANDIDAT-ABD.md`, `RAPPORT-1341.md`, `PREUVES-1341.md` — v1, inchangés.
- `analysis-1341.py` / `.json` — qualification d'origine (reproductible).
- `analyse/simulate-1341b.*`, `preuve-AB.*` — A/B/C, inchangés sur v2
  (`simulate-1341b.json` `108a6511…`, `preuve-AB.json` `30bf2aea…`).
- `analyse/temoins-v2.py` / `.log` / `resultats-v2.json` — témoins v2 (D + R1).
- `analyse/temoins-D.py` / `.log` / `resultats.json` — témoins du **gel v1**
  (leur bloc de fusion est celui de la v1 : ne pas les rejouer sur le v2).
- `analyse/tests-cibles-v2.log` — 27 + 115 tests ciblés OK (1 skip).
- `analyse/flux-durations.py`, `c-psql-flux.json` — source de C.
- `artifacts/` — entrées minimales des scripts (`durees-0532faed4.json`,
  `plan/plan.json`, `carte-96a795fb2/carte.json.gz`, `durees.json` et
  `flux.jsonl` des 5 parts de mesure) + `SHA256SUMS` d'origine.
- `PREUVES-SHA256SUMS-v2` — empreintes de ce bundle.

## Rejeu (facultatif)

```
git worktree add wt-abd a8213ee03293d08b708cfebc312188d3ac76651e
cd analyse
python3 temoins-v2.py > temoins-v2.log                       # D + R1 (gel v2)
python3 simulate-1341b.py > simulate-1341b.log               # A/B/C, inchangé
python3 preuve-AB.py > preuve-AB.log                         # CLI == simulation
cd ../wt-abd
PYTHONPATH=src python3 -m unittest tests.test_carte_parts_completes -v          # 27 OK
PYTHONPATH=src python3 -m unittest tests.test_l161e_map tests.test_l116_parts tests.test_l161f_plan  # 115 OK, 1 skip
```

`temoins-v2.py` attend le candidat sous `wt-abd` et la carte de l'incident sous
`artefacts/../artifacts` (arborescence de ce bundle). Attendus : témoin K1/K2
(`b` omis, `a` doublé, garde D v1 muette, couverture qui refuse), plan ==
`split` sur les 6 parts, bloc réel du job `plan-mesure` (avec et sans carte),
et les scénarios de fusion (dérive K1/K2 refusée, plan unique publié).

## Portée

Le correctif déplace la lecture de la carte et le calcul du découpage dans un
job unique par run (`plan-mesure`) et ajoute le contrôle de couverture
effective à la fusion (`merge --plan`). Aucune suite complète, aucun run CI,
aucun rerun ; le push de branche et la CI de la tête gelée viennent après les
verdicts requis et la coordination pilote.
