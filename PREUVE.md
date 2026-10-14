# #1342 — recomposition du paquet 1 sur le correctif #1379 (candidat local)

Date : 2026-10-14 ~22:45Z, agent mty-amh-impl-01 (exécutant du paquet).
Lecture seule sur la forge : aucun push, aucun run CI, aucun sondage.

## Faits

- `origin/main` = `2458ec053123d3f33644fef1e092fc84cc2773d9`, **ancêtre** de la tête du paquet
  `b689cb84db7a5fa2cf39088e21f070d364c3fc39` (merge-base = 2458ec053123).
- `git merge-tree --write-tree --messages b689cb84 7fc7eab9c` → **rc=0**, aucun conflit,
  arbre `adab91bc3dcafca5a22443453296633e16a5c316`
  (7fc7eab9c = correctif #1379, `lot/1379-test-fallback-refs`).
- Candidat local : commit `b558378fa8abe749dec8cea004398c07a32a54c6`,
  arbre `adab91bc`, parents `b689cb84` + `7fc7eab9c` (créé par `git commit-tree`, **non poussé**).
- Delta candidat vs gel `b689cb84` : `tests/test_l161f_plan.py` seul, 3+/1-
  (le correctif du test hermétique ; les dix apports du manifeste sont constants).
- Témoin décisif sur le candidat, avec l'environnement exact qui rendait le run du paquet rouge
  (`GITHUB_HEAD_REF=integration/paquet-1-20261014`, `GITHUB_BASE_REF=main`) :

      .venv/bin/python -m unittest tests.test_l161f_plan -q
      Ran 110 tests in 35.698s
      OK

  (avant correctif, `test_fallback_plan_lets_the_parts_split` échoue sous cet env — reproduit 22:28Z).

## Suite

Publication/actualisation de #1050 conduite par le bureau (mesh-design 22:41Z) ; feu paquet/staging
à renouveler sur la nouvelle tête. Candidat tenu prêt, aucun geste sur le gel `b689cb84` ni sur le run.
