# Atelier #1211 — candidat C2 `ffbde584` : union #1211 × #932 × `main 6eeecc8e9`

Atelier séparé, **aucune PR, aucune branche, aucun push `main`/`release`/lot,
aucun run, aucune relance**. La branche `lot/1211-ci-impactes-contrat` reste à
`39f51fd9d`, la PR #923 est inchangée ; les runs existants ne sont ni annulés
ni touchés.

Demande mesh-design 14/10 10:22Z : rendre C2 lisible hors VM (artéfact exact,
bundle/diff borné) sans push de branche ni CI, sans refaire les cibles.

## Objets

| objet | SHA |
|---|---|
| gel en vigueur (tête PR #923) | `39f51fd9d0105b21911adb1d56c7df67fc3876eb` |
| résolution #833 : `39f51fd9d` × `main 7305957e8` | `b39908ae31c3f9e990a274ce003b6d8a0bbd3809` (arbre `ec9f6d3a140a7cc7…`) |
| composite #1211 × #932 : `b39908ae3` × `91a53bdb1` | `e54429a4e2bbf5278ffb095f7043aca522bffc3c` (arbre `ec85e93e92c8c046…`) |
| `main` fraîche (contrôlée à 10:27Z) | `6eeecc8e914efb391c9e4be67058ae83519d8bd7` |
| **candidat C2** | `ffbde584520e643b44e33513914dc3bbe7b1bddc` |
| arbre C2 | `35a1bac5451f8fdc2a4c092d5f1311d9648fee56` |

`ffbde584` est un commit de fusion à 2 parents (`e54429a4e` + `6eeecc8e9`),
jamais poussé sur une branche. `git merge-tree --write-tree e54429a4e
6eeecc8e9` rend l'arbre `35a1bac…`, rc=0 : C2 est l'auto-fusion exacte,
aucun conflit ; `git diff --check` propre des deux côtés.

Blobs des constantes de l'arbre C2 (identiques au composite `00664c00b`) :

- `.github/workflows/tests.yml` : `517f0ac5abea47518398079aa66756920f04f67b`
- `scripts/test-plan.py` : `097b5fe0a02313adeff0061814ea5cb6dab02eb3`
- témoin #932 `tests/test_l932_ci_venv_cache.py` : `4bb84e4d46691c2225fad0059871eccdb506eedd`

## Provenance (déjà contrôlée, non rejouée ici)

Reconstruction depuis les seules refs publiques : `refs/atelier/1211/union-b39908ae`
= `b39908ae3`, `origin/lot/932-ci-cache-venv` = `91a53bdb1`, `origin/main` =
`6eeecc8e9`. Les deux ordres de fusion donnent l'arbre `35a1bac` :
`(b39908ae3 × 932) × main` et `(b39908ae3 × main) × 932` — contrôles
indépendants mesh-design 10:12Z et auteur 10:21Z (registre #1211).

## Delta de fraîcheur

`git diff 00664c00b 35a1bac` = **6 fichiers, +1661/−21**, tous du seul delta
`main` #1003+#1044 :

| fichier | lignes |
|---|---|
| `docs/EXPLOITATION.md` | +118 |
| `scripts/ci-flow.py` | +57/−21 env. |
| `src/ameesh/merge_pr.py` | +788 |
| `src/ameesh/mesh_cli.py` | +5 |
| `tests/test_1003_merge_pr.py` | +646 |
| `tests/test_899_alerte_plafond.py` | +68 |

Rien du lot ni de #932 n'y est touché : les trois constantes ci-dessus sont
byte-identiques au composite.

## Récupération

```sh
git fetch origin '+refs/atelier/1211/*:refs/atelier/1211/*'
git show refs/atelier/1211/union-c2-ffbde584          # C2 exact
git ls-tree -r refs/atelier/1211/patchs-c2-ffbde584   # les patchs ci-dessous
```

Refs poussées : `refs/atelier/1211/union-c2-ffbde584` (→ `ffbde584`) et
`refs/atelier/1211/patchs-c2-ffbde584` (→ ce commit de patchs, sans parent).
Aucun run créé par ces poussées (refs hors `refs/heads/**` et `refs/tags/**`).

## Patchs bornés

1. `1211-c2-vs-main-6eeecc8e9.patch` — `git diff 6eeecc8e9 ffbde584` : contenu
   final du lot sur la main courante, 11 fichiers, +831/−106. C'est la
   contribution à relire (sélecteur #1211, contrat, #932, résolution #833).
2. `1211-c2-vs-composite-00664c00b.patch` — `git diff 00664c00b 35a1bac` :
   delta de fraîcheur seul, 6 fichiers, +1661/−21 (main #1003+#1044).
3. `1211-932-vs-b39908ae3.patch` — `git diff b39908ae3 e54429a4e` : apport
   #932, 2 fichiers, +68/−6 (workflow cache venv par runner + témoin).

La résolution #833 elle-même (2 fichiers croisés, blobs `de733120e…` /
`097b5fe0a…`) reste dans `refs/atelier/1211/patchs` = `b8b6005b3a0b5fd80fe999054570a1f665e00330`.

## Preuves (déjà acquises, non rejouées ici)

Témoins ciblés rejoués sur l'arbre exact `35a1bac` en worktree détaché (auteur
10:21Z, worktree retiré) : **196 OK psql + 196 OK psycopg** (1 skip
conditionnel), modules `l161f_plan`, `l932_ci_venv_cache`, `l161a_ci_sans_code`,
`940_agregat_bash`, `l116_parts`, `l161b_flow`, `l161c_failures_realtime`,
`l161e_map`, `l148_stage2b_scripts`, `l148_stage6_json_keys`,
`l157_estimations`. Aucune suite complète locale (canon §3). Aucune cible
refaite pour ce paquet.

## Statut

Candidat **non gelé, non poussé, non fusionné**. Portes inchangées : avis
Claude applicable sur `b399`/composite (bureau), gel coordonné, UN passage
complet commun 1211+932 §12 sur main courante — aucun feu déduit d'un vert
`39f`. Ordre de coordination bureau 10:18Z : validation #1236 strict 12408
d'abord, puis passage commun 1211+932 après le Claude final.
