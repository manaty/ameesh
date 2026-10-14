# Atelier #1211 — résolution d'union `main 7305957e8` × `gel 39f51fd9d`

Atelier séparé, **aucune PR, aucun push `main`/`release`, aucun run, aucune
relance**. Le gel `39f51fd9d` et la branche `lot/1211-ci-impactes-contrat`
restent inchangés ; les runs `38513509371` (tests) et `38513509415` (carte)
ne sont ni annulés ni touchés.

## Objets

| objet | SHA |
|---|---|
| gel en vigueur (tête PR #923) | `39f51fd9d0105b21911adb1d56c7df67fc3876eb` |
| `main` contrôlée (#833 fusionné) | `7305957e88b7f511c2e853e64a6573f8e574832e` |
| **résolution d'union** | `b39908ae31c3f9e990a274ce003b6d8a0bbd3809` |
| arbre de la résolution | `ec9f6d3a140a7cc7310542029b9647adc908e4e0` |

`b39908ae3` est un commit de fusion à 2 parents (`39f51fd9d` + `7305957e8`),
jamais poussé sur une branche. Aucun amendement, aucun rebase : le gel
`39f51fd9d` n'est pas réécrit.

Blobs des deux fichiers croisés dans la résolution :

- `.github/workflows/tests.yml` : `de733120e50077697ffd7337a6f12cc2780e0475`
- `scripts/test-plan.py` : `097b5fe0a02313adeff0061814ea5cb6dab02eb3`

## Récupération

```sh
git fetch origin '+refs/atelier/1211/*:refs/atelier/1211/*'
git show refs/atelier/1211/union-b39908ae      # la résolution exacte
git ls-tree -r refs/atelier/1211/patchs        # les patchs ci-dessous
```

Refs poussées : `refs/atelier/1211/union-b39908ae` (→ `b39908ae3`) et
`refs/atelier/1211/patchs` (→ ce commit de patchs, sans parent).

## Patchs

1. `1211-resolution-combined.patch` — `git show --cc b39908ae` : diff combiné
   du commit de fusion, limité aux **deux fichiers réellement croisés**
   (`tests.yml`, `test-plan.py`). C'est la décision de résolution elle-même,
   lue face aux **deux** parents (249 lignes, 75 lignes changées).
2. `1211-union-vs-main.patch` — `git diff 7305957e8 b39908ae` : ce que le lot
   ajoute à `main` (10 fichiers, +763/−100). C'est le contenu final de la PR.
3. `1211-conflits-vs-main.patch` — idem, limité aux 2 fichiers croisés
   (565 lignes).
4. `1211-union-vs-gel-39f51fd9d.patch` — `git diff 39f51fd9d b39908ae` :
   delta complet depuis le gel (42 fichiers ; inclut l'avance de `main` de
   25 commits, donc plus bruyant).
5. `1211-conflits-vs-gel.patch` — idem, limité aux 2 fichiers croisés
   (216 lignes).

Sémantique conservée des deux côtés (vérifiée sur l'arbre d'union) :

- #833 (push doc) : job `AMEESH_CI_BASE` = `github.event.before`,
  `AMEESH_CI_REF` = `github.ref` ; `nature()` garde la voie *push main*
  (`pushed_range`) ;
- #1211 (sélection) : étape `BASE_REF`/`HEAD_REF`, arguments
  `--base-ref/--head-ref`, `release_pr`, `lot_selection`, repli complet
  (`base_fallback`) conservés.

Autres fichiers : auto-fusionnés, aucune adaptation (hors des 2 croisés).

## Preuves auteur (arbre d'union `b39908ae3`, pas de rejeu ici)

Déjà consignées au lot (note 06:16Z) : `tests/test_l161f_plan.py` **80 OK**
(`PushLaneTest` #833 + `LotBranchTest` #1211), **95** ciblés CI OK,
**27** flux/carte OK (1 skip), YAML du job `plan` valide. Aucune suite
complète locale (canon §3). Contre-vérifié ici à la lecture : YAML OK (8
jobs), `test-plan.py` parse OK, `git diff --check` propre des deux côtés,
les 4 arguments et les 2 blocs d'env présents dans l'arbre d'union.

## Statut

En attente de qualification (Codex en binôme pilote, R2 Claude réelle via
#1098 après compte autorisé frais). La poussée finale de `b39908ae3` sur
`lot/1211-ci-impactes-contrat` (nouveau gel + CI applicable) se fera au
terminal du passage CI courant, sous coordination, sans relance automatique.
