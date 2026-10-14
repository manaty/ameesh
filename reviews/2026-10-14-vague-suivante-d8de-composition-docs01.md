# Composition de la vague suivante — contre-épreuve sur main d8de2a583 (14/10 02:4xZ)

mty-amh-docs-01 — lecture seule sur les branches ; composition dans un worktree
temporaire sous $TMPDIR, aucun push de lot, aucune PR. Complète
`wave-composition-1019-union.md` (qui portait sur la base gelée afa32932e).

## Base et têtes

- `origin/main` = `d8de2a583d8e109de0b520ef84dcf5382d2679dd`, arbre `e8ce189fa7a2`
  (contient `afa32932e` #1112 puis `e9ffe202b`/`d8de2a583` #1018).
- Les 9 têtes candidates sont **inchangées** (PR OPEN) : 672 `a2e3c60de181`,
  402 `79ebee0636f6`, 684 `2ec97643b8ca`, 619 `a02928cf6676`, 700 `c1d3271c82ba`,
  740 `e0d9b6553127`, 791 `9dfcb03e146d`, 806 `5bc72a02e9ad`, 808 `1e7475317f83`.
- Ordre repris de la composition validée : #287, #419, #748, #775, #857, #963,
  #1019, #1038, #1048 (#954 reste absorbé par #1112).

## Résultat

| étape | lot | merge sur la base courante | arbre |
|---|---|---|---|
| 1 | #287 | propre | `b8091c02167f` |
| 2 | #419 | propre | `4fdb6f744fdc` |
| 3 | #748 | propre | `e778c910efcc` |
| 4 | #775 | propre | `44f7cebed182` |
| 5 | #857 | propre | `e79dc0587539` |
| 6 | #963 | propre | `8b3ae8ec5abf` |
| 7 | #1019 | **conflit unique** `tests/parts/lecteurs-doc.txt`, résolu en union | `f4fecbc4b9b7` |
| 8 | #1038 | propre | `74fd582cc11d` |
| 9 | #1048 | propre | **`8d530d2bc782`** (commit local `f1d84e2c36d0`) |

Union `#775 × #1019` : les deux lignes, ordre alphabétique, insérées avant
`test_l159a_capacity_guard` ; blob du fichier **`f544a3a61123`**, identique à la
contre-épreuve réelle du 14/10 00:0xZ.

## Preuves sur l'arbre composé `8d530d2bc782`

- Contre-épreuve blob à blob : les **21 fichiers mono-candidats** retrouvent
  exactement les blobs attendus de la qualification (`next-wave-qualify.json`) ;
  aucun écart.
- Fichiers partagés (les deux contributions présentes) :
  - `docs/EXECUTEUR-MEDIEE.md` `a906548cf5d3` = compteurs #1019 (322/385) + #1038
    (`latest_by_path`, compte 22) ;
  - `docs/EXPLOITATION.md` `90185b150b8f` = seuil #963 (3 600 s) + #1048
    (`Environment=PATH=…`) ;
  - `tests/parts/lecteurs-doc.txt` `f544a3a61123` = union des deux lecteurs.
- Lecteurs de la doc + garde L161a sur l'arbre composé : **178 tests OK, 4 sautés**
  (Postgres injoignable, `AMEESH_REQUIRE_DB=0`).
- `scripts/doc-links.py` (#775) sur l'arbre composé : **114 fichiers, 0 lien cassé**,
  code retour 0.
- Worktree temporaire supprimé après mesure ; rien poussé, aucune branche de lot
  touchée.

## Validité

La composition n'est déterministe que pour cette base et ces têtes. Si `main` bouge
ou si une tête change, refaire : `WAVE_BASE=<rev> python3 simulate-composition.py`
(étapes 1→6), puis rejouer l'union #775/#1019 en worktree (le script s'arrête sur le
conflit) et les témoins ci-dessus. Les avis/relectures portent sur les têtes de lot,
pas sur la composition : la fusion de la vague reste soumise au ruling du pilote.
