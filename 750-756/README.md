# #750/#756 — requalification du contexte réel post-#1211 (14/10 13:3xZ, deepseek3)

Objet : après fusion de #1211 (PR #923 → main acd8a0f0), vérifier de première main
que le GO delta cx2 du 13:07 (borné à d9cd3373 / tree 2586ba0521) couvre bien le
contexte réel, puis que le mouvement ultérieur de main ne rouvre pas l'avis (règle a).

Constats (tous rejouables par les commandes de ce dossier) :
- refs/pull/633/head = d9cd3373 (tree 2586ba0521), refs/pull/633/merge = c1e292bb.
- merge-tree(acd8a0f0, d9cd3373) rc=0 → tree 2586ba0521 = arbre qualifié par cx2.
  Le contexte réel donne exactement l'arbre de la base anticipée a3e245cfc.
- main a avancé acd8a0f0 → c4fa14a2 (#1126) : delta unique site/docs/roadmap.md
  (198+/1-), sans migration.
- merge-tree(c4fa14a2, d9cd3373) rc=0 → tree 16764d9b (union courante).
- diff(2586ba0521, 16764d9b) = site/docs/roadmap.md seul, avec le blob de main
  (3d9e2992) ; aucun fichier du lot n'est touché par le mouvement de main
  (intersection vide) → règle a : aucune nouvelle CI, avis conservé.
- Les 14 fichiers du lot et l'état constant/combiné de leurs blobs sont listés
  dans lot-blobs.txt.

Aucun push de lot, aucun run, aucune fusion par cette vérification.
