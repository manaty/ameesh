# Atelier #1110 — union main 6eeecc8e9 × gel c927fec5a

Préparé par ameesh-dev5 (auteur du lot), le 2026-10-14, à la demande de mesh-design
(courrier #34628). **Aucun push de branche lot, aucune PR, aucun run** : la branche
`lot/1110-ci-nexlink-wake` et la PR #845 restent sur la tête gelée ; seules les refs
d'atelier ci-dessous portent l'union, pour la relecture du delta avant feu.

## Objets

| objet | SHA | rôle |
|---|---|---|
| gel (tête PR #845, inchangée) | `c927fec5af65fa1ea6d8bdf46c876b3ae662cac0` | branche `lot/1110-ci-nexlink-wake` |
| main courante | `6eeecc8e914efb391c9e4be67058ae83519d8bd7` | fusion #956 |
| base du gel | `afa32932e72c459bf1d23314912647b23b306b73` | main au gel |
| **union d'atelier** | `ff4b5d4041958b34d2f7d9fac24d0bb8e74b044b` | parents `c927fec5a` + `6eeecc8e9`, arbre `4976b8f07429d85d61e0b218386f878f3d67bb94` |
| paquet (cette ref) | voir `refs/atelier/1110/patchs` | lecture seule |

`git merge-tree --write-tree c927fec5a 6eeecc8e9` rend `4976b8f07429d85d61e0b218386f878f3d67bb94`,
rc 0 : l'arbre de l'union est l'auto-fusion exacte des deux parents, aucun conflit.

## Compte des blobs (12 fichiers du lot)

9 des 12 blobs du lot sont **constants** dans l'union (identiques à la tête gelée) ;
3 sont **combinés** avec la main, chacun sur des hunks disjoints :

| fichier | blob main | blob gel | blob union | état |
|---|---|---|---|---|
| `deploy/sql/role-superviseur.sql` | `9b4c91ee5957` | `32ad121f3878` | `d39d8c1e7380` | combiné |
| `docs/EXPLOITATION.md` | `ee349bdcca93` | `b72b0de91032` | `66c2f5be4db7` | combiné |
| `tests/parts/pilote-psql.txt` | `b977d1e0bbf0` | `594246375390` | `47c19faed29e` | combiné |
| 9 autres (ci.py, exploitation.py, contract.json, migration 0433, plan_github.py, storage/interface.py, storage/postgres/ci.py, test_l126_issues.py, test_l161c_failures_realtime.py) | — | — | = blob du gel | constants |

Détail complet : `1110-blob-accounting.txt`.

Les combinaisons sont additives et sans recouvrement :

- `role-superviseur.sql` : le lot ajoute `workflow` au contrat `ci_runs` (hunk ~481) ;
  la main ajoute `roles` au contrat `agents` (hunk ~207). Les deux lignes sont
  présentes dans l'union.
- `tests/parts/pilote-psql.txt` : le lot ajoute `test_l161c_failures_realtime` en
  fin de liste (ligne 38) ; la main ajoute `test_1037_bascule_harnais` en tête
  (ligne 17). Les deux lignes sont présentes, chacune une fois.
- `docs/EXPLOITATION.md` : le delta du lot (workflows suivis, rattrapage borné,
  verdict par workflow, base du rouge hérité par workflow, migration 0433) est
  intégré dans la doc de la main ; la main apporte en plus « Plafond du job
  (#899) » dans la même section. `1110-combined-cc.txt` montre le seul passage
  entrelacé.

## Pièces

| pièce | contenu |
|---|---|
| `1110-union-vs-main.patch` | delta complet du lot rapporté sur la main courante : 12 fichiers, +781/−94 (ce que la fusion apporterait à main). |
| `1110-combines-vs-main.patch` | diff borné aux 3 blobs combinés, depuis la main : la contribution du lot telle qu'elle survit dans l'union (SQL `workflow`, doc #1110, ligne pilote-psql). |
| `1110-combines-vs-gel.patch` | diff borné aux 3 blobs combinés, depuis le gel : la contribution de la main dans ces fichiers (SQL `roles`, doc #956/#899, ligne `test_1037_bascule_harnais`). |
| `1110-combined-cc.txt` | `git show --cc` de l'union (`--format=fuller`) : le seul hunk entrelacé (docs/EXPLOITATION.md). |
| `1110-blob-accounting.txt` | table des 12 blobs main / gel / union avec l'état de chacun. |

## Lecture demandée

- **R1 delta (auditeur)** : qualifier `1110-combines-vs-main.patch` et
  `1110-combines-vs-gel.patch` — les 3 combinaisons, disjointes et additives,
  préservent-elles le correctif du lot et les apports de la main ? Le reste des
  12 fichiers est constant (blob du gel), donc déjà couvert par la R1 Codex
  conservée sur `c927fec5a` (aucun avis implicite élargi).
- **R2 Claude (#1098, session réelle)** : même contexte ; la référence reste la
  tête gelée `c927fec5a` sur la PR #845. L'union n'est ni poussée ni exécutée.

## Portée

- Aucun rejeu CI : la part CI acquise (run `38489198233` attempt 2, tête
  `c927fec5a`) porte sur la tête gelée, inchangée ; l'union d'atelier n'est pas
  un arbre validé, elle sert à qualifier le delta avant feu.
- La migration 0433 et le contrat `role-superviseur.sql` restent ceux du lot ;
  l'union ajoute seulement le `roles` de la main au même fichier de contrat.
- Aucun test n'a été relancé pour cette union (préparation sur demande, sans
  push/run) ; les preuves ciblées du lot restent celles du gel.

## Reproductibilité

```sh
git fetch origin \
  refs/atelier/1110/union-c927fec5a:refs/atelier/1110/union-c927fec5a \
  refs/atelier/1110/patchs:refs/atelier/1110/patchs
git log -1 --format='%H %T %P' refs/atelier/1110/union-c927fec5a
# ff4b5d4041958b34d2f7d9fac24d0bb8e74b044b 4976b8f07429d85d61e0b218386f878f3d67bb94
#   c927fec5af65fa1ea6d8bdf46c876b3ae662cac0 6eeecc8e914efb391c9e4be67058ae83519d8bd7
git merge-tree --write-tree c927fec5af65fa1ea6d8bdf46c876b3ae662cac0 6eeecc8e914efb391c9e4be67058ae83519d8bd7
git show --cc refs/atelier/1110/union-c927fec5a
git diff 6eeecc8e914efb391c9e4be67058ae83519d8bd7 refs/atelier/1110/union-c927fec5a
```
