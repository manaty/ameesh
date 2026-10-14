# #750/#756 — atelier de l'union avec le paquet #1211 (préparation ; aucun push de lot, aucune CI)

- Candidat d'union : d9cd3373768c1e5f4371402449d7bd92fbb988e5
- Arbre            : 2586ba05211d0a811f19f8f9e4253ac09ba81a1b
- Base anticipée   : a3e245cfc10e5fda7000fe7b9c61a7540a51e778 (= refs/pull/923/merge de #1211 ; parents main 537f4b964a9fcbc41814cad4e88ca918ce78549d + tête 3668d4b6b75f833d15418d45157096204d71e4f0 ; arbre 57a3b14ab82f668a23b5539d2426d7e76dee3fc3)
- Chaîne           : a3e245cfc → ee383e92 → a1d555c4 → ec03dbbf → d9cd3373 (les 4 commits du lot rejoués)
- Candidat antérieur, révisé cx2 (14/10 12:35Z) : 7e92f805265393f4e8071425179919e131bb1637, arbre adc353cc59b929acc3f5c853fe447ec37c438f69, base main 537f4b96 — conservé tel quel
- Gel (PR manaty/ameesh#633, tête inchangée) : aaad71177d523f9c7cb7797451529d5fc475cd15 ; base du gel 5bc2c5de35994b1b7e5bff435b30bd0a4df731d8

## Refs publiées (origin)

- refs/atelier/750-756/candidat-union1211-d9cd3373 → d9cd3373768c1e5f4371402449d7bd92fbb988e5
- refs/atelier/750-756/preuves-d9cd3373 → ce commit (preuves)
- refs/atelier/750-756/candidat-7e92f805 et refs/atelier/750-756/preuves-7e92f805 : conservées, inchangées

La branche lot/750-756-release-perimeter reste à aaad71177d523f9c7cb7797451529d5fc475cd15 et la PR #633 à la même tête : aucun push de lot, aucune CI déclenchée.

## Vérifications reproduisibles

- Recoupement indépendant : `git merge-tree --write-tree a3e245cfc aaad7117` → rc 0, arbre 2586ba05211d0a811f19f8f9e4253ac09ba81a1b = arbre du candidat d'union ; idem avec le candidat 7e92f805 (merge-tree.txt).
- Rejeu du contenu par `git merge-file` (base 5bc2c5de, ours 7e92f805, theirs a3e245cfc) : rc 0, hash b6a8ece55c9e9b9f181e5bd24c369722d4d43f31 = blob docs/EXPLOITATION.md du candidat d'union (merge-file-check.txt).
- Intersection #1211 × lot : delta propre de #1211 (merge-base ec0b4a70e..3668d4b6b) = 11 fichiers ; intersection avec les 14 fichiers du lot = docs/EXPLOITATION.md SEUL (intersection-1211-lot.txt). La disjonction annoncée avant 12:38 (« ses 3 fichiers .github/** sont disjoints ») ne couvrait pas ces 11 fichiers du paquet.
- 14 fichiers du lot : 13 blobs identiques entre le candidat 7e92f805 et l'union, 1 seul nouveau blob — docs/EXPLOITATION.md, a4f738b9e7 → b6a8ece55c, +47/−10, delta dû à #1211 (blobs.txt, delta-nouveau-1-fichier.stat.txt/patch).
- Delta total union vs candidat 7e92f805 : 11 fichiers, +831/−106 = le delta propre de #1211, dont les 10 autres fichiers ne font pas partie du lot et restent sous la revue de #1211 (delta-1211-11-fichiers.stat.txt/patch).
- `git range-diff 537f4b96..7e92f805 a3e245cfc..d9cd3373` : 4 commits deux à deux identiques (=), aucune retouche du lot (range-diff.txt).
- `git diff --check` propre ; aucun marqueur de conflit dans les 14 fichiers.
- Aucun run CI sur d9cd3373 ; tête de lot et PR #633 inchangées à aaad7117.

## Portée

Préparation seule : cet atelier sert à l'extension bornée de l'avis delta cx2 au seul blob nouveau, docs/EXPLOITATION.md. Le GO cx2 du 12:35 sur 7e92f805 ne couvre pas ce blob par lui-même. Après fusion réelle de #1211, l'identité du contexte doit être requalifiée avec la vraie main (le merge-ref a3e245cfc n'est qu'anticipé) : rebase frais, CI applicable sur la tête exacte, feu nominatif, puis fusion auteur --match-head-commit.
