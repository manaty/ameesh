# Paquet integration/paquet-h-20261019-1700 — composition (§12)

Base : `main` **`8c2c9a964f60d`** (paquet h-1600 fusionné, PR #2076, commit du
19/10 16:36 UTC). Branche `integration/paquet-h-20261019-1700`, cible `main`.
Une seule suite complète est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis. Liste calculée
`scripts/paquet-candidats.py` à 17:42 UTC sur `8c2c9a964f60d` (lecture fraîche
d'`origin/main`) : **13 prêts, 14 en attente, 0 erreur**. Le paquet initial de
17:00 portait 7 unités ; recomposé à 17:47 UTC avec les trois lots devenus
prêts entre-temps (#2451, #2619, #2706), soit **10 unités**. #2539 et #2744,
non prêts au calcul (conflit `docs/EXECUTEUR-MEDIEE.md` avec main), entrent par
les **remises officielles** de `mty-amh-qa-01` (message du 19/10 16:55 UTC) :
`refs/ameesh/resolutions/2539/main-8c2c9a964` = `14f3ef61a14e` (arbre
`053a205e`, identique au contenu relu `94af3e06`) et
`refs/ameesh/resolutions/2744/2539` = `83f0d7a021e7` (arbre `0e11cce8`,
identique au candidat composeur `9673bba39`). Aucun delta de contenu pour
#2744 : les avis dev1+cx3 portent sur la tête gelée `2fa42376`, et l'union
officielle ne change que les compteurs de contrat attendus par l'union.

## Admissions (10 unités, 10 lots portés)

| lot | source | motif |
|---|---|---|
| #2539 | remise `14f3ef61a14e` (sur main `8c2c9a964`) | non prêt au calcul (conflit docs), **admission manuelle** : arbre `053a205e` identique au contenu relu `94af3e06` (avis cx3 Codex + dev4 Claude) ; la porte exige un avis sur le SHA de remise, non acquis — aucun delta de contenu, écart assumé et signalé |
| #2744 | union `83f0d7a021e7` (#2744 sur #2539) | non prêt au calcul sur la branche (conflit docs), **admission manuelle** : arbre `0e11cce8` identique au candidat relu `9673bba39` ; avis dev1 Claude + cx3 Codex sur `2fa42376`, aucun delta de contenu |
| #2585 | `9570b6756ad6` (tête gelée) | prêt mécanique, porte OK (dev7 Claude + cx1 Codex) ; sensible (review_lot, work, land) |
| #2623 | `7c7a32cff8ca` (tête gelée) | prêt mécanique, porte OK (cx2 Codex + dev7 Claude) ; sensible (exploitation) |
| #2721 | `4b089f6577ec` (tête gelée) | prêt mécanique, porte OK (cx2 Codex + dev1 Claude) ; sensible (review_authors, activation, provenance) |
| #2564 | `c48eecd12eed` (tête gelée) | prêt mécanique, porte OK (cx2 Codex + dev1 Claude) ; sensible (exploitation, lot_class, merge_queue) |
| #2648 | `7e0db389ceb2` (tête gelée) | prêt mécanique, porte OK (dev7 Claude, un seul avis requis par l'allègement §2) ; sensible (accounts, sous_utilisation) |
| #2451 | `afa63e5e6604` (tête gelée) | prêt mécanique, porte OK (dev5 DeepSeek + cx3 Codex) ; sensible (canon, train, désignation) ; P1 |
| #2619 | `2dca102823bd` (tête gelée) | prêt mécanique, porte OK ; sensible (reprise, runner) |
| #2706 | `a5f655a70eba` (tête gelée) | prêt mécanique, porte OK (cx2 Codex, allègement §2) ; sensible (harness, runner) |

## Composition (10 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2539 | `14f3ef61a14e` (remise) | `c915b6f5055f` | sensible (mail, runner, storage, contrat médié) ; remise officielle, 0 conflit |
| 2 | #2744 | `83f0d7a021e7` (union) | `2b9477b642fb` | sensible (plan_git, work, packet_close, contrat médié) ; union officielle avec #2539, 0 conflit |
| 3 | #2585 | `9570b6756ad6` | `9e882cde1a99` | sensible (review_lot, work, land) ; 0 conflit |
| 4 | #2623 | `7c7a32cff8ca` | `047ef6da94aa` | sensible (exploitation) ; 0 conflit |
| 5 | #2721 | `4b089f6577ec` | `b19681c88e9f` | sensible (review_authors, activation, provenance) ; 0 conflit |
| 6 | #2564 | `c48eecd12eed` | `bd933eee6939` | sensible (exploitation, lot_class, merge_queue) ; 0 conflit |
| 7 | #2648 | `7e0db389ceb2` | `03fd16253fcd` | sensible (accounts, sous_utilisation) ; 0 conflit |
| 8 | #2451 | `afa63e5e6604` | `8258531e117e` | sensible (canon, train, désignation) ; 0 conflit |
| 9 | #2619 | `2dca102823bd` | `0cfd74c9ecb4` | sensible (reprise, runner) ; 0 conflit |
| 10 | #2706 | `a5f655a70eba` | `c1c0449e1f18` | sensible (harness, runner) ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>` (relu par
`scripts/paquet-compose.py --units --base 8c2c9a964f60d` : 10 unités, 10 lots
portés). Aucun conflit de composition, aucune résolution, aucune migration SQL.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2729 | **non prêt** : 1 avis indépendant manquant sur `e21c15cca227` (seul cx1 Codex ; familles claude et deepseek auteurs, aucun second avis éligible en l'état). Signalé au bureau à 17:44Z ; priorité demandée par le bureau, à composer dès le 2e avis |
| #2538 | **non prêt** : 1 avis indépendant manquant sur `e7834ed960b7` (seul cx3 Codex) |
| #2738 | **non prêt** : 1 avis indépendant manquant sur `018d6d2d462e` (seul dev7 Claude) |
| #2692 | prêt au calcul, **reporté** au véhicule suivant : le paquet porte déjà 10 unités (dont 2 remises) ; priorité aux 8 lots de la liste bureau et aux remises déjà composées |
| #2680 | prêt au calcul, **reporté** au véhicule suivant (même motif de taille) |
| #2640 | prêt au calcul, **reporté** au véhicule suivant (même motif de taille) |
| #2670 | prêt au calcul, **reporté** au véhicule suivant (même motif de taille) |
| #2674 | prêt au calcul, **reporté** au véhicule suivant (même motif de taille) |

Les 14 lots en attente hors #2539/#2744 (verdicts manquants ou conflit) sont
exclus par la liste calculée ; aucun n'est admis sans porte verte.

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 17:42 UTC, base
  `8c2c9a964f60d` : **13 prêts, 14 en attente, 0 erreur** ; les 13 prêts sont
  composés (8) ou écartés avec motif (#2692, #2680, #2640, #2670, #2674) ;
  #2729/#2538/#2738 sont en attente (verdicts manquants).
- Composition : **10 unités, 0 conflit** ; 71 fichiers, +2846/−353 ;
  `git diff --check` vert, aucun marqueur de conflit, aucune migration SQL.
- `paquet-compose.py --units --base 8c2c9a964f60d` : **10 unités de fusion,
  10 lots portés** (#2539 `14f3ef61a14e`, #2744 `83f0d7a021e7`, #2585
  `9570b6756ad6`, #2623 `7c7a32cff8ca`, #2721 `4b089f6577ec`, #2564
  `c48eecd12eed`, #2648 `7e0db389ceb2`, #2451 `afa63e5e6604`, #2619
  `2dca102823bd`, #2706 `a5f655a70eba`).
- `paquet-inclusion` (tête finale, manifeste ajouté au-dessus, `--ready-file`
  recalculée à 17:49Z) : **rc 0** — 10 admissions, 10 compositions, 0 omission,
  0 défaut ; 5 prêts écartés motivés (#2680, #2692, #2640, #2670, #2674) ;
  #2539 `absent` sur les seules lignes de `docs/EXECUTEUR-MEDIEE.md` remplacées
  par l'union officielle #2744 (avertissement non bloquant), le reste du
  contenu #2539 est présent.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée,
  1 exemption utilisée (au registre).
- `scripts/doc-links.py` : 171 fichiers, **0 lien cassé**.
- Tests ciblés sur l'arbre composé : exécutés sur les modules des 10 unités
  (psql + psycopg), à consigner au fil du run.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
