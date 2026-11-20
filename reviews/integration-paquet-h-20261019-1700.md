# Paquet integration/paquet-h-20261019-1700 — composition (§12)

Base : `main` **`8c2c9a964f60d`** (paquet h-1600 fusionné, PR #2076, commit du
19/10 16:36 UTC). Branche `integration/paquet-h-20261019-1700`, cible `main`.
Une seule suite complète est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis. Liste calculée
`scripts/paquet-candidats.py` à 16:59 UTC sur `8c2c9a964f60d` : **27
candidats, 5 prêts (#2585, #2623, #2721, #2564, #2648), 22 en attente,
0 erreur**. #2539 et #2744, non prêts au calcul (conflit
`docs/EXECUTEUR-MEDIEE.md` avec main), sont composés par les **remises
officielles** de `mty-amh-qa-01` (message du 19/10 16:55 UTC) :
`refs/ameesh/resolutions/2539/main-8c2c9a964` = `14f3ef61a14e` (arbre
`053a205e`, identique au candidat composeur `67faedc51`) et
`refs/ameesh/resolutions/2744/2539` = `83f0d7a021e7` (arbre `0e11cce8`,
identique au candidat composeur `9673bba39`). Aucun delta de code : les avis
cx3+dev4 de #2539 portent sur la remise `94af3e06` (même code, compteurs de
docs alignés sur l'ancienne base) et dev1+cx3 de #2744 sur la tête gelée
`2fa42376` ; les remises officielles ne changent que les compteurs de
`docs/EXECUTEUR-MEDIEE.md` pour la nouvelle base.

## Admissions (7 lots, 6 unités)

| lot | source | motif |
|---|---|---|
| #2539 | remise `14f3ef61a14e` (sur main `8c2c9a964`) | non prêt au calcul (conflit docs), **admission manuelle** : arbre `053a205e` identique au candidat relu `67faedc51` ; avis cx3 Codex + dev4 Claude sur `94af3e06`, seuls les compteurs de docs sont réalignés sur la nouvelle base, aucun delta de code |
| #2744 | union `83f0d7a021e7` (#2744 sur #2539) | non prêt au calcul (conflit docs), **admission manuelle** : arbre `0e11cce8` identique au candidat relu `9673bba39` ; avis dev1 Claude + cx3 Codex sur `2fa42376` (code #2744), union avec #2539 et compteurs de docs réalignés, aucun delta de code |
| #2585 | `9570b6756ad6` (tête gelée) | prêt mécanique, porte OK (dev7 Claude + cx1 Codex) ; sensible (review_lot, work, land) |
| #2623 | `7c7a32cff8ca` (tête gelée) | prêt mécanique, porte OK (cx2 Codex + dev7 Claude) ; sensible (exploitation) |
| #2721 | `4b089f6577ec` (tête gelée) | prêt mécanique, porte OK (cx2 Codex + dev1 Claude) ; sensible (review_authors, activation, provenance) |
| #2564 | `c48eecd12eed` (tête gelée) | prêt mécanique, porte OK (cx2 Codex + dev1 Claude) ; sensible (exploitation, lot_class, merge_queue) |
| #2648 | `7e0db389ceb2` (tête gelée) | prêt mécanique, porte OK (dev7 Claude) ; sensible (accounts, sous_utilisation) |

## Composition (6 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2539, #2744 | `83f0d7a021e7` (union officielle) | `2d8c24342586` | sensible (mail, runner, storage, plan_git, work, contrat médié) ; union officielle, 0 conflit |
| 2 | #2585 | `9570b6756ad6` | `e91619c8f9de` | sensible (review_lot, work, land) ; 0 conflit |
| 3 | #2623 | `7c7a32cff8ca` | `f4a3d8a83c2f` | sensible (exploitation) ; 0 conflit |
| 4 | #2721 | `4b089f6577ec` | `65b15fd2bd1a` | sensible (review_authors, activation, provenance) ; 0 conflit |
| 5 | #2564 | `c48eecd12eed` | `51f145a818de` | sensible (exploitation, lot_class, merge_queue) ; 0 conflit |
| 6 | #2648 | `7e0db389ceb2` | `c6577684cf66` | sensible (accounts, sous_utilisation) ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>` (relu par
`scripts/paquet-compose.py --units --base 8c2c9a964f60d` : 6 unités, 7 lots
portés). Aucun conflit de composition, aucune résolution ; aucune migration SQL.

## Écartés (ce véhicule)

Aucun lot prêt écarté : les 5 lots prêts de la liste calculée sont composés.

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 16:59 UTC, base
  `8c2c9a964f60d` : **27 candidats, 5 prêts, 22 en attente, 0 erreur**
  (`--ready-out` dans `$TMPDIR`). #2539 et #2744 restent en attente
  (`conflict` sur `docs/EXECUTEUR-MEDIEE.md`) ; ils entrent par les remises
  officielles, dont les arbres sont identiques aux candidats relus.
- Composition : **6 unités, 0 conflit** ; 45 fichiers, +2244/−153 ;
  `git diff --check` vert, aucun marqueur de conflit, aucune migration SQL.
- `paquet-compose.py --units --base 8c2c9a964f60d` : **6 unités de fusion,
  7 lots portés** (#2539 `83f0d7a021e7`, #2744 `83f0d7a021e7`, #2585
  `9570b6756ad6`, #2623 `7c7a32cff8ca`, #2721 `4b089f6577ec`, #2564
  `c48eecd12eed`, #2648 `7e0db389ceb2`).
- `paquet-inclusion` (tête finale, manifeste ajouté au-dessus, `--ready-file`
  recalculée) : **rc 0** — 7 admissions, 6 compositions, 0 omission,
  0 défaut ; 7 contenus `présent` (l'unité union #2539/#2744 couvre les deux
  lots).
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée,
  6 exemptions utilisées (toutes au registre).
- `scripts/doc-links.py` : 171 fichiers, **0 lien cassé**.
- Tests ciblés sur l'arbre composé : à compléter (psql + psycopg).
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
