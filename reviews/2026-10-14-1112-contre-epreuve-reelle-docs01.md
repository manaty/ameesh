# Contre-épreuve réelle de la composition — 14/10 00:0xZ

mty-amh-docs-01 — lecture seule ; complète `2026-10-14-1112-composition-union-docs01.md`
(la base gelée est devenue `main`, aucune tête n'a bougé).

## Constat

- `origin/main` = `afa32932e72c`, arbre `5b39fda801d2` — identique à la base gelée de la
  qualification et de la composition : les preuves publiées s'appliquent telles quelles.
- Les 9 têtes candidates sont inchangées (PR ouverte, `headRefOid`) : 672 `a2e3c60de181`,
  402 `79ebee0636f6`, 684 `2ec97643b8ca`, 619 `a02928cf6676`, 700 `0d4c65dcabe7`,
  740 `e0d9b6553127`, 791 `9dfcb03e146d`, 806 `5bc72a02e9ad`, 808 `1e7475317f83`.
- Compteurs #1019 : `contract.json` de `main` = blob `d34eafe01478` = blob de la branche
  #1019 → 322 refusées / 385 ; témoin `tests.test_l107_executor_contract` 20/20 sur la
  branche.

## Union #775 × #1019 relue en direct (arbre réel, pas de simulation)

Worktree temporaire sur `afa32932e`, fusion de `9dfcb03e1` puis de `a02928cf6676` :
conflit unique `tests/parts/lecteurs-doc.txt`, résolu en union (deux lignes, ordre
alphabétique) ; arbre composé `434f09378875` (`36a659b42f35`), blob du fichier
`f544a3a61123`.

- `tests.test_l107_executor_contract` + `tests.test_l161a_ci_sans_code` : 26 OK.
- `tests.test_doc_links` (#775) : 12 OK.
- Worktree supprimé après mesure ; rien poussé, rien fusionné — cette branche de revue
  ne porte que ce document et les deux précédents.

## Suite

#1019 reste gelé sur `9dfcb03e146d` ; la vague suivante peut reprendre l'ordre publié.
La veille #1019 ignore désormais l'orphelin `tests (flux) psql 1/1` (#856 : conclusion
refusée après l'arrêt du runner) et laisse la vague composer d'abord ; rejeu solo borné
à partir de 02:01Z si #1019 n'est pas absorbé.
