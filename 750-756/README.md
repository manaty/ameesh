# #750/#756 — candidat de rebase publié en atelier (aucun push de lot, aucune CI)

- Candidat : 54ef68d20e8b2f59f108c8b7a0462e82f91049e4
- Arbre    : 526b2018c1c01d0be6f43ed0e7a7ea8a528c9ac1
- Base     : 6eeecc8e914efb391c9e4be67058ae83519d8bd7 (= origin/main, Merge pull request #956)
- Parent   : af3e24baffde86fe18e33bc311285a4026c16a0c
- Chaîne   : 6eeecc8e914efb391c9e4be67058ae83519d8bd7 → ce73cc1f → d4b0317e → af3e24baffde86fe18e33bc311285a4026c16a0c → 54ef68d20e8b2f59f108c8b7a0462e82f91049e4 (4 commits rejoués)
- Gel (PR manaty/ameesh#633, tête inchangée) : aaad71177d523f9c7cb7797451529d5fc475cd15
- Ancienne base du gel : 5bc2c5de (Merge pull request #579)

## Refs publiées
- refs/atelier/750-756/candidat-54ef68d2 → 54ef68d20e8b2f59f108c8b7a0462e82f91049e4
- refs/atelier/750-756/preuves-54ef68d2 → ce commit (preuves)

La branche lot/750-756-release-perimeter reste à aaad71177d523f9c7cb7797451529d5fc475cd15 : aucun push de lot, aucune CI déclenchée.

## Vérifications reproduisibles
- `git merge-tree --write-tree --name-only origin/main aaad71177d523f9c7cb7797451529d5fc475cd15` → rc 0, arbre 526b2018c1c01d0be6f43ed0e7a7ea8a528c9ac1, égal à l'arbre du candidat (recoupement indépendant).
- 14 fichiers dans le diff du lot (5bc2c5de..aaad71177d523f9c7cb7797451529d5fc475cd15) ; 5 blobs modifiés par l'union, 9 blobs identiques au gel (voir blobs.txt).
- Delta borné du rebase sur les fichiers du lot : 5 fichiers, +206/−26 (delta-5-fichiers.stat.txt, delta-5-fichiers.patch).
- `git diff --check` propre ; grep des marqueurs de conflit : aucun.
