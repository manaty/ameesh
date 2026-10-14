# #750/#756 — candidat de rebase publié en atelier (aucun push de lot, aucune CI)

- Candidat : 7e92f805265393f4e8071425179919e131bb1637
- Arbre    : adc353cc59b929acc3f5c853fe447ec37c438f69
- Base     : 537f4b964a9fcbc41814cad4e88ca918ce78549d (= origin/main, Merge pull request #983 — lot #1295)
- Chaîne   : 537f4b964a9f → 7a97be62 → 28f6adef → a7522407 → 7e92f805 (4 commits rejoués)
- Gel (PR manaty/ameesh#633, tête inchangée) : aaad71177d523f9c7cb7797451529d5fc475cd15
- Base du gel : 5bc2c5de (Merge pull request #579)
- Candidat précédent, déjà révisé par cx2 (14/10 12:58Z) : 54ef68d20e8b2f59f108c8b7a0462e82f91049e4, base 6eeecc8e

## Refs publiées (origin)

- refs/atelier/750-756/candidat-7e92f805 → 7e92f805265393f4e8071425179919e131bb1637
- refs/atelier/750-756/preuves-7e92f805 → ce commit (preuves)

La branche lot/750-756-release-perimeter reste à aaad71177d523f9c7cb7797451529d5fc475cd15 : aucun push de lot, aucune CI déclenchée.

## Vérifications reproduisibles

- `git merge-tree --write-tree --name-only origin/main aaad71177d523f9c7cb7797451529d5fc475cd15` → rc 0, arbre adc353cc59b929acc3f5c853fe447ec37c438f69, égal à l'arbre du candidat (recoupement indépendant).
- 14 fichiers dans le diff du lot (5bc2c5de..aaad7117) ; 6 blobs modifiés par l'union, 8 identiques au gel (voir blobs.txt).
- Delta borné du rebase sur les fichiers du lot : 6 fichiers, +241/−27 (delta-6-fichiers.stat.txt, delta-6-fichiers.patch).
- Nouveau depuis la révision cx2 du candidat 54ef68d2 : 2 fichiers du lot, +35/−1 — docs/EXPLOITATION.md (+29, hunks de main arrivés avec 793288b16) et docs/ORCHESTRATEUR.md (+7/−1, classé constant par cx2, devenu combiné) ; les 4 autres blobs combinés sont inchangés (delta-nouveau-2-fichiers.stat.txt, delta-nouveau-2-fichiers.patch).
- `git diff --check` propre sur les 14 fichiers ; aucun marqueur de conflit dans les 6 blobs du candidat.
- Tests ciblés de l'auteur (sous `ameesh heavy`, sans CI) : `python3 -m unittest tests.test_l750_release tests.test_l756_full_suite tests.test_l160e_merge_queue tests.test_l160b_radar tests.test_l160g_nexlink` → Ran 145 tests, OK, 67,2 s.
- Aucun run CI sur 7e92f805 (la tête de lot et la PR #633 restent aaad7117).

## Reste dû (hors atelier)

- Extension de l'avis delta cx2 aux 2 fichiers nouveaux (EXPLOITATION.md, ORCHESTRATEUR.md).
- Après activation de #1211 (PR #923) : rebase frais sur la main post-1211 (ses 3 fichiers .github/** sont disjoints des 14 du lot), CI applicable sur la tête exacte, feu nominatif, puis fusion auteur --match-head-commit.
