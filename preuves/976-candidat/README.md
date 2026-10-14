# #976 — candidat de réintégration sur main après #1003 (préparation, lecture seule)

Objet : donner au lecteur (relecteur indépendant / pilote) une **référence lecture seule du
candidat exact** et ses preuves de constance, sans toucher la PR #777, sans pousser la branche
de lot et sans lancer de CI.

## Candidat exact

| | |
|---|---|
| commit | `57fa1824a378423d6fcf92d7ced90231d5ab203f` |
| arbre | `df596d3bd277d778b46d149436569e0dfe1cf61d` |
| parents | `04a6281d909d2fb56c742067b32cd1cadd529bb7` (tête gelée du lot = `origin/lot/976-mail-undeliverable-expiry` = `refs/pull/777/head`) + `dc40ccbb023e9f517687a81cf5854ed26e713ac6` (`origin/main`, #1003 fusionné) |
| base d'union | `fcff03db4e77aeee6ccaa3bb09debde3e5ad089c` (main d'avant #1003, second parent de la tête gelée) |
| auteur | mty-amh-impl-01 |

Réfs d'atelier publiées (lecture seule, sans CI — `refs/atelier/**` ne déclenche rien) :

- `refs/atelier/976/candidat-57fa1824` → `57fa1824a378423d6fcf92d7ced90231d5ab203f` (le candidat exact) ;
- `refs/atelier/976/preuves-57fa1824` → le présent dossier (patchs, remerge-diff, range-diff,
  constance, SHA256SUMS), commit dont le parent est le candidat.

Récupération :

```sh
git fetch origin '+refs/atelier/976/*:refs/atelier/976/*'
git log -1 --stat refs/atelier/976/candidat-57fa1824
git show refs/atelier/976/preuves-57fa1824:preuves/976-candidat/constance.txt
```

## Conflit et résolution

Un seul conflit : `docs/EXPLOITATION.md`. #1003 insère la section
« Fusion hors API d'une PR touchant `.github/workflows/**` — `ameesh work merge-pr` » ;
#976 renomme « Courrier en souffrance — … `ameesh mail forward` » en ajoutant « , balayage ».

Résolution : **les deux sections sont conservées**, la section #1003 d'abord (ordre du texte de
main), le titre #976 conservé avec « , balayage ». Rien d'autre n'est modifié à la main :
`git log -1 --remerge-diff` ne montre que ces hunks de résolution (`remerge-diff.txt`, 29 lignes).

## Preuves de constance (détail et sorties dans `constance.txt`)

1. **Même ensemble de fichiers** avant/après : 14 fichiers, identiques dans `fcff03db4..04a6281d9`
   et `dc40ccbb..57fa1824`.
2. **13 des 14 fichiers : diff textuellement identique** (octet à octet) entre `delta lot avant`
   et `delta lot après` ; le 14e (`docs/EXPLOITATION.md`) a un **ensemble de lignes +/- identique**
   (38 lignes, `cmp` du tri).
3. **13/13 blobs du lot non touchés par main : blob identique** à la tête gelée.
4. **Les 3 fichiers apportés par main : blob identique** au blob de `origin/main`
   (`src/ameesh/merge_pr.py`, `src/ameesh/mesh_cli.py`, `tests/test_1003_merge_pr.py`).
5. **Auto-fusion trois points** `git merge-tree --write-tree 04a6281d9 dc40ccbb` : rc=1, conflit
   unique `docs/EXPLOITATION.md` ; blobs de base stables (base `97b6d1c3`, lot `4d7cd67d`,
   main `ee349bdcc`) ; l'arbre du candidat n'en diffère **que** par ce fichier. (L'OID de l'arbre
   d'auto-fusion, lui, dépend de l'orthographe des SHA dans les étiquettes de conflit : il n'est
   pas revendiqué comme preuve.)
6. **Union effective sur le document** : 0 ligne ajoutée par le lot absente du candidat,
   0 ligne ajoutée par main absente du candidat ; les deux titres présents une fois.
7. `git diff --check` propre dans les deux sens (vs main et vs tête gelée).
8. `range-diff` des deux témoins de delta (`31729f4e0` base `fcff03db4` vs `5ae19eb8d` base
   `dc40ccbb`) : un seul écart, le contexte du hunk de titre de `docs/EXPLOITATION.md`.

## Portée et état

- Aucun push sur `lot/976-mail-undeliverable-expiry` (toujours `04a6281d9` = PR #777), aucune PR
  modifiée, aucun run CI créé. Le candidat vit uniquement sous `refs/atelier/976/*`.
- Le candidat est une **préparation sur `main` = `dc40ccbb`** ; il sera **refait sur la main
  fraîche au geste #1211** (mesh-design, 14/10 09:52Z).
- L'ancien feu (02:20Z) ne couvre pas cette résolution : qualification de la résolution + feu
  adapté nommant la nouvelle tête restent à donner par le pilote.
- Veilleur de constat arrêté (aucun réarmement sur l'ancien head).

## Contenu du dossier

- `README.md` — ce fichier ;
- `constance.txt` — commandes et sorties des preuves ci-dessus ;
- `remerge-diff.txt` — `git log -1 --remerge-diff` du candidat (résolution seule) ;
- `range-diff.txt` — `git range-diff` des deux témoins de delta ;
- `diffs/` — par fichier, `lot-avant--*.diff` (`fcff03db4..04a6281d9`) et
  `lot-apres--*.diff` (`dc40ccbb..57fa1824`) ;
- `candidat-vs-main.patch` — `git diff dc40ccbb 57fa1824` (delta #976 sur main courante) ;
- `delta-lot-gele.patch` — `git diff fcff03db4 04a6281d9` (delta #976 d'origine) ;
- `merge-tree.txt` — auto-fusion trois points et conflit ;
- `SHA256SUMS` — empreintes des fichiers ci-dessus.
