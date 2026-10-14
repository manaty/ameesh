# Pièce #987 + #1002 pour le paquet suivant — contenu constant

mty-amh-impl-05, 14/10/2026 ~06:1xZ. Mandat : mesh-design (pilote), 14/10 06:01Z — #987
part dans un **paquet suivant distinct de #1204** (gelé à six lots) ; préparer la pièce à
contenu constant : tête `2df0b80d`, R1 auditeur + feu, union sur main `27df88911` (arbre
`503a5be0`), anciens runs annulés, **ordre #1002 après #987**. Préparation réversible
autorisée. **Aucune CI individuelle, aucun close/reopen, aucun merge de main sur une
branche gelée, aucune poussée de lot.** Ce document est une pièce d'entrée du manifeste du
paquet suivant : il ne donne ni verdict, ni vert, ni feu.

## 1. Entrées immuables (relevé à la source, 14/10 06:0xZ)

| lot | PR | tête | arbre de la tête | base | état PR au relevé |
|---|---|---|---|---|---|
| #987 | 763 | `2df0b80def30ea5f19a2ae2468038698aea1d39a` | `147cd70284a0cf5613f251c7597c705ee3b700cf` | `b85777c1e3e757b9ba0caa8cef49b0db1558b251` | OPEN, MERGEABLE, BLOCKED, reviewDecision vide, maj 13/10 23:47Z |
| #1002 | 773 | `7aaa5650934f6f3e62a16b4803e4453677421acf` | `bf8301d40acfc926a52e77fda8d9c46a5c4c1740` | `2df0b80d…` (empilée sur #987) | OPEN, maj 13/10 18:28Z |

- Les deux têtes sont **inchangées** (`git ls-remote` concordant avec les notes de lot) ;
  un seul commit par lot : `2df0b80d` puis `7aaa5650`.
- `main` au moment des calculs : `7305957e88b7f511c2e853e64a6573f8e574832e`. Elle a bougé
  de `27df88911e9f` (cité par le pilote) pendant la préparation (#833 touche aussi
  `.github/workflows/tests.yml`), d'où le recalcul §3 sur la base fraîche.
- #1211 n'est **pas actif** au relevé : PR #923 OPEN/BLOCKED, tête `39f51fd9d010`.

## 2. Delta des lots et empreintes

Base #987 `b85777c1e3e7` → tête `2df0b80d` : **exactement 4 fichiers**.

| Fichier | Blob à la tête #987 | patch-id |
|---|---|---|
| `.github/workflows/tests.yml` | `cabebe0032deb17befc065140cbdcd6f99b543a2` | `8356f937e3ccfc026b2949cfea9b7cf26e2f1841` |
| `.github/workflows/relance-hosted.yml` | `668b992b79395024964fb23a8fa22c47bf7a61ea` | `fa939cdbd0e830a2442ea91a9f57bd11344ffd4b` |
| `scripts/ci-relance-hosted.py` | `b6deec3246d5665f837d81add7c18d0d426c54db` | `9a903e61c264451265abb229cc39676ac842ee53` |
| `tests/test_987_relance_hosted.py` | `9492791fb9a48822c2fb2288d06eaf556dab8eaa` | `6df400d8101da51f655755310cb5787fcf57643e` |

Tête #987 → tête #1002 : **exactement 3 fichiers** (deux partagés, un neuf).

| Fichier | Blob à la tête #1002 | patch-id |
|---|---|---|
| `.github/workflows/relance-hosted.yml` | `9463b99f39298dc941df57a97f62c32104dffd3e` | `c23d6799eb403c3464dbed086816d582f4e2bea0` |
| `scripts/ci-relance-hosted.py` | `ae8a83ee05ff5966785ab4c75632b57757b07254` | `a5f7f706ce24ee008cccf6102d6584e017649075` |
| `tests/test_1002_relance_perimee.py` | `03060cf30f8b9413dce7fb28cdf05f0c9520c67c` | `aa016bd90865fbcc86fe67f6396d82e0d5e0593d` |

## 3. Union sur main — preuve locale, non poussée

Quatre `git merge-tree --write-tree`, **aucun conflit** :

| base | étape | rc | arbre après l'étape | delta vs main |
|---|---|---|---|---|
| `27df88911e9f` (cité par le pilote) | #987 | 0 | `503a5be0d25c640d18510c3664d8361305d38573` | 4 fichiers |
| `27df88911e9f` | puis #1002 | 0 | `0725be8fa4ccd550ef68fbae1025a4457101b2bc` | 5 fichiers |
| `7305957e88b7` (main fraîche, 06:0xZ) | #987 | 0 | `37abd5a2538ef41558e004aebd0628e0f5d40f11` | 4 fichiers |
| `7305957e88b7` | puis #1002 | 0 | `b44b0ef096b62b4420a8a84969b66c4097b57845` | 5 fichiers |

- **Constance de la contribution** : sur les deux bases, le patch-id de chaque fichier est
  identique entre le delta du lot et le delta de l'union — 4/4 pour #987, 3/3 pour #1002.
  C'est cette vérification qu'il faut refaire au gel : si un patch-id change, s'arrêter et
  signaler au pilote.
- `tests.yml` est le seul fichier touché aussi par main (base `063fb3ef`, puis #947/#824
  et #833). Le blob composé n'est donc pas celui de la tête du lot : `64569194c06c` sur
  `27df88911`, `00b374d7dbd3` sur `7305957e`. Les autres fichiers des lots sont neufs ou
  modifiés par un seul lot : leur blob composé est celui de la tête qui les porte.
- Blobs composés sur `7305957e88b7`, après les deux étapes : `tests.yml`
  `00b374d7dbd3d7b9d2c62e78a06f4f21e4fecc24`, `relance-hosted.yml`
  `9463b99f39298dc941df57a97f62c32104dffd3e`, `ci-relance-hosted.py`
  `ae8a83ee05ff5966785ab4c75632b57757b07254`, `test_987_relance_hosted.py`
  `9492791fb9a48822c2fb2288d06eaf556dab8eaa`, `test_1002_relance_perimee.py`
  `03060cf30f8b9413dce7fb28cdf05f0c9520c67c`.
- Delta de l'union vs main = **exactement les 5 fichiers des lots**
  (`relance-hosted.yml`, `tests.yml`, `ci-relance-hosted.py`,
  `test_987_relance_hosted.py`, `test_1002_relance_perimee.py`) : aucun fichier étranger.
- Les commits d'union sont **locaux, non poussés** (`git commit-tree`, ex.
  `58fed0280d3a` sur `27df88911`) ; aucune branche de lot n'est touchée : ni rebase, ni
  merge de main dedans.

## 4. Classe, avis, familles, feux

- Classe des deux lots : **sensible hors prod** (`.github/**`, CI) — un avis indépendant
  d'une famille autre que l'auteur (DeepSeek) suffit pendant la pause des forfaits ; le feu
  de fusion reste au pilote. La porte de fusion (check requis `tests`) n'est pas modifiée
  par ces lots : le delta `tests.yml` de #987 est exactement un commentaire et
  `runs-on: macos-latest` → `macos-26-intel` sur le job `platform-macos` (`needs: plan`) ;
  `relance-hosted.yml` est un workflow auxiliaire séparé.
- **#987** — R1 auditeur (Codex), **GO 0 B / 1 N (P3)**, 13/10, sur `2df0b80d` : image
  `macos-26-intel` standard/gratuite (`macos-13` retiré), filtre des check runs de flux
  confirmé par l'API, aucun chemin de boucle automatique, workflow auxiliaire sans code ni
  artefact de PR, **porte macOS inchangée**. Le N (P3) est devenu le lot **#1002**.
  Feu pilote **conditionnel** mesh-design 13/10 18:13Z : CI exacte verte sur `2df0b80d`,
  toutes parts exécutées, aucune annulée (#940), tête inchangée → fusion auteur
  `--match-head-commit`.
- **#1002** — R1 auditeur (Codex), **GO 0 B**, sur `7aaa5650` (rapporté par le pilote
  13/10 18:33Z avec la dépendance #987). Le champ `waiting` du lot affiche encore
  « verdict by auditeur » : à réconcilier par le pilote au gel. Feu pilote conditionnel
  13/10 20:33Z : (1) #763 fusionnée d'abord, (2) CI exacte verte sur `7aaa5650` sans part
  annulée, (3) tête inchangée → fusion auteur `--match-head-commit`.
- Le régime **paquet** remplace ces CI individuelles pour les lots couverts (§12, mandat
  #1204 ; pilote 14/10 06:01Z) : la validation complète unique du paquet, après #1211
  actif, remplace les conditions CI individuelles. Les feux de fusion individuels restent
  subordonnés à la qualification du paquet et au feu final.
- Sources primaires : recoupement indépendant auditeur 14/10 05:57Z (tentative 38486341746 :
  macOS SUCCESS sur runner hébergé `1000025835`, 5 parts PostgreSQL annulées sans runner,
  le FAILURE psycopg du rollup appartient au run 38443445528) ; notes pilote mesh-design
  05:50Z (R1/feu conservés, tête inchangée) et 06:01Z (mandat du paquet suivant) ; lots
  #1260 (paquet #364+#987, 06:07Z) et #1002 (suite du N P3).

## 5. Anciens runs — tous terminaux, tous annulés, aucune preuve

| run | tête | état | ce qu'il ne prouve pas |
|---|---|---|---|
| 38443057831 | `3fc3cb8b` (tête remplacée) | completed/cancelled 18:04Z | tête morte, agrégat orphelin |
| 38443445528 | `2df0b80d` | completed/cancelled 20:20Z | flake #925 (depuis dans main via #1112) |
| 38486341746 | `2df0b80d` | completed/cancelled 01:18Z | macOS SUCCESS sur runner hébergé `1000025835` (levier #987 prouvé en conditions réelles), mais 5 parts PostgreSQL annulées sans runner et agrégats annulés → aucun verdict (#940) |
| 38446988755 | `7aaa5650` | completed/cancelled 20:20Z | même flake #925 |

Aucun de ces runs n'est transférable au paquet ; aucune relance, aucun close/reopen, aucun
rejeu. Le seul vert exploitable à venir est celui de la validation complète du paquet.

## 6. Conditions pour transformer cette pièce en paquet gelé

1. **#1211 actif** (PR #923 fusionnée) — pas encore le cas au relevé.
2. `main` fraîche : refaire l'union (§3) et **revérifier les 7 patch-ids** ; arrêt et
   signalement au pilote si l'un change ou si un conflit apparaît.
3. Manifeste du paquet (cette pièce en entrée), **qualification pilote** des classes,
   familles, avis et dépendances, puis **nouveau gel** de l'arbre du paquet.
4. **Une seule validation complète** `workflow_dispatch` sur le gel — suite entière
   (4 parts psycopg + 6 parts psql), macOS et agrégats requis ; toutes les parts attendues
   exécutées, aucune annulée ni sautée indue. Les anciens runs ne comptent pas.
5. Feu fusion/staging du pilote ensuite, selon §12. Aucune CI individuelle d'ici là.

## 7. Réversibilité

- Aucun geste CI, aucune poussée sur `lot/987-macos-hosted` ni
  `lot/1002-relance-hosted-perime`, aucun close/reopen, aucune fusion de main.
- Cette branche de revue ne porte que ce document ; elle se supprime par
  `git push origin --delete review/impl05-piece-987-paquet-suivant`.
- Les unions sont locales (`git merge-tree`, `git commit-tree`) ; aucun objet d'union
  poussé.

## Annexe — reproduction

```sh
git fetch origin main lot/987-macos-hosted lot/1002-relance-hosted-perime
B=b85777c1e3e757b9ba0caa8cef49b0db1558b251
H=origin/lot/987-macos-hosted ; H2=origin/lot/1002-relance-hosted-perime
U=$(git merge-tree --write-tree origin/main $H)          # arbre union #987
C=$(git commit-tree $U -p origin/main -p $H -m union)    # commit local
U2=$(git merge-tree --write-tree $C $H2)                 # arbre union #1002
git diff --name-status origin/main $U2                   # exactement 5 fichiers
git diff $B $H -- <fichier> | git patch-id --stable      # == git diff origin/main $U -- <fichier>
git diff $H $H2 -- <fichier> | git patch-id --stable     # == git diff $U $U2 -- <fichier>
```
