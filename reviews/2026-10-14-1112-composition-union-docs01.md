# Composition de la vague suivante — union #775/#1019 verifiee (base gelee afa32932e)

mty-amh-docs-01, 2026-10-13T23:3xZ — lecture seule (merge-tree/commit-tree, aucun push,
aucune branche). Preparation, pas fusion : a refaire si la base ou une tete bouge.

## Methode

`simulate-composition.py` (ce repertoire) : integration sequentielle des 9 candidats
sur la base gelee `afa32932e72c` (arbre `5b39fda801d2`), par `git merge-tree --write-tree`
puis `git commit-tree` (objets pendants uniquement). Ordre retenu :

| etape | lot | PR | tete | resultat |
|---|---|---|---|---|
| 1 | #287 | 672 | a2e3c60de181 | propre → arbre 9242e0fd58da |
| 2 | #419 | 402 | 79ebee0636f6 | propre → arbre 1103cd3c3a15 |
| 3 | #748 | 684 | 2ec97643b8ca | propre → arbre fa91f4b5dc0a |
| 4 | #775 | 619 | a02928cf6676 | propre → arbre e09ec1889cce |
| 5 | #857 | 700 | 0d4c65dcabe7 | propre → arbre a7cd31f5d5c3 |
| 6 | #963 | 740 | e0d9b6553127 | propre → arbre 72a360861674, commit 1cd4761b7381 |
| 7 | #1019 | 791 | 9dfcb03e146d | **conflit unique** sur `tests/parts/lecteurs-doc.txt`, resolu en union |
| 8 | #1038 | 806 | 5bc72a02e9ad | propre → commit 82087e7257bc |
| 9 | #1048 | 808 | 1e7475317f83 | propre → commit final 46931228c5de (arbre 0aef140333e3) |

#954 est absent : absorbe par la base gelee (`merge-tree(afa32932e, 0722ff4ef931)` = arbre
de la base, verifie 23:2xZ).

## Union #775 x #1019 (seul conflit entre candidats)

`tests/parts/lecteurs-doc.txt` : les deux lots inserent une ligne a la meme position
(avant `test_l159a_capacity_guard`) — #775 `test_doc_links`, #1019
`test_l107_executor_contract`. Union resolue, ordre alphabetique respecte :

```
test_doc_links  # docs/ (liens internes et ancres, vérificateur scripts/doc-links.py, #775)
test_l107_executor_contract  # docs/EXECUTEUR-MEDIEE.md (compteurs du contrat de l'exécuteur, L107)
```

Blob resolu (arbre 404e75269a72) : temoin rejoue sur l'arbre resolu, puis sur l'arbre
final compose.

## Preuves (23:3xZ, arbre final 0aef140333e3, worktree temporaire $TMPDIR)

- lecteurs declares + garde L161a : `tests.test_doc_links`, `tests.test_l107_executor_contract`,
  `tests.test_l159a_capacity_guard`, `tests.test_l162_tmpfs`, `tests.test_l233_decide_only`,
  `tests.test_l33_chatbot`, `tests.test_l84_serve`, `tests.test_site_art`,
  `tests.test_l161a_ci_sans_code` → **178 tests OK, 4 sautes** (Postgres injoignable,
  `AMEESH_REQUIRE_DB=0` ; les lecteurs sont declares sans base).
- Verificateur de liens #775 sur l'arbre compose : `scripts/doc-links.py` →
  « 114 fichiers, 0 lien(s) cassé(s) », rc=0.
- Contre-epreuve hash a hash sur l'arbre final : les 21 blobs mono-lot retrouvent
  exactement les blobs attendus de la qualification. Les 3 fichiers partages sont bien
  fusionnes : `tests/parts/lecteurs-doc.txt` (union), `docs/EXPLOITATION.md` (#963 seuil
  3 600 s + #1048 PATH/`Environment=PATH`), `docs/EXECUTEUR-MEDIEE.md` (#1019 compteurs
  322/385 + #1038 `latest_by_path`).
- Aucun push, aucune branche, aucune PR ; worktree temporaire supprime apres mesure.

## Contre-epreuve a la composition reelle

Quand #1112 est fusionne (main = arbre afa32932e) : reprendre l'ordre ci-dessus, verifier
que chaque tete est inchangee, resoudre l'union avec le contenu ci-dessus, puis rejouer
les lecteurs sur l'arbre compose. Toute tete ou base differente → refaire la qualification
(`next-wave-qualify.py`) et cette composition.
