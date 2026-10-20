# Paquet integration/paquet-h-20261020-0300 — composition (§12)

Base : `main` **`797779c2d73a`** (fusion du paquet h-0000, PR #2125, 20/10
01:30Z). Branche `integration/paquet-h-20261020-0300`, cible `main`. Une seule
suite complète est demandée sur la tête finale.

Demandes : bureau 01:45Z — « compose le prochain paquet maintenant (h-0300) ;
je le fusionne au vert si l'auto-merge n'est pas armé », avec 12 lots
`waiting: merge` (#2766 en tête, #2812 #2800 #2799 #2702 #2815 #2809 #2625
#2726 #2755 #2609 #2786). Radar de conflit 01:38Z : #2799 × #2800 sur le
bundle généré `src/ameesh/ui/static/ameesh-ui.js` ; le composeur résout par
régénération (changement le plus petit côté #2799).

Liste calculée `scripts/paquet-candidats.py` à 01:49Z sur `797779c2d` :
**11 prêts, 6 en attente, 0 erreur** (`rc 0`, `--ready-out` écrit). Le paquet
porte **11 unités, 11 lots** ; #2786, prêt pour le bureau au sens de la file,
est écarté sur conflit (voir « Écartés »).

## Admissions (11 unités, 11 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2766 | `0d9a964b936a` (tête gelée) | sensitive | cx3 (codex) + auditeur (Claude) | porte de véhicule : avis par lot membre, pas de second avis sur le delta ; auteur docs-01 |
| #2812 | `793a2cd34b0a` (tête gelée) | sensitive | auditeur (Claude) + cx2 (codex) | désignation : ne pas inviter un correcteur exclu ni un auteur ; auteur docs-01 |
| #2726 | `f9213ff9ec7b` (tête gelée) | sensitive | cx3 (codex) + auditeur (Claude) | panier #2681 : porte des avis, auteurs et périmètre ; auteur dev3 |
| #2702 | `306134a9f100` (tête gelée) | sensitive | cx1 (codex) + mesh-design (Claude) | class_at_merge : classer un lot fusionné sur le delta du paquet ; auteur impl-06 |
| #2625 | `18831d0026b0` (tête gelée) | sensitive | auditeur (Claude) + dev5 (DeepSeek) | resume : avertir qu'aucun runner ne tient le bail ; auteur cx2 |
| #2815 | `93a07725080c` (tête gelée) | light | dev5 (DeepSeek) | activer packet_watch (#2601) : bloc de configuration et destinataires ; auteur mesh-design |
| #2809 | `954bf67950ca` (tête gelée) | light | auditeur (Claude) + dev5 (DeepSeek) | gel : lancer la garde mécanique avant d'enregistrer le jalon ; auteur cx1 |
| #2755 | `8190367f6628` (tête gelée) | light | dev5 (DeepSeek) | panier #2721 : famille d'auteur ambiguë, lever le doute ; auteur cx2 |
| #2609 | `c8d5cf678359` (tête gelée) | light | dev5 (DeepSeek) | #2584 : variantes « en relisant #N » et « pull-request #N » ; auteur cx3 |
| #2799 | `732a3fab5175` (tête gelée) | light | auditeur (Claude) | chat : l'agent arrêté exige une reprise, pas une attente en boîte ; auteur impl-01 |
| #2800 | `82635d27db8c` (tête gelée) | light | auditeur (Claude) | chat : un lot sélectionné lit aussi le fil du projet ; auteur dev5 |

## Composition (11 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2766 | `0d9a964b936a` | `ba2ddb509fbc` | sensitive ; 0 conflit |
| 2 | #2812 | `793a2cd34b0a` | `e01bd3f9f4f0` | sensitive ; 0 conflit |
| 3 | #2726 | `f9213ff9ec7b` | `32750b94d613` | sensitive ; 1 conflit résolu (voir Résolutions) |
| 4 | #2702 | `306134a9f100` | `65795c3bea74` | sensitive ; 0 conflit |
| 5 | #2625 | `18831d0026b0` | `3d8b07f43519` | sensitive ; 0 conflit |
| 6 | #2815 | `93a07725080c` | `1ea1759480eb` | light ; 0 conflit |
| 7 | #2809 | `954bf67950ca` | `580f19c2c7f7` | light ; 0 conflit |
| 8 | #2755 | `8190367f6628` | `8cdcaff45609` | light ; 0 conflit |
| 9 | #2609 | `c8d5cf678359` | `183256433ddc` | light ; 0 conflit |
| 10 | #2799 | `732a3fab5175` | `46c539827d0f` | light ; 0 conflit |
| 11 | #2800 | `82635d27db8c` | `8bdb50467bc4` | light ; 1 conflit résolu (bundle) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`
(`scripts/paquet-compose.py --units --base 797779c2d` : 11 unités de fusion,
11 lots portés).

## Résolutions de composition (2, écrites par le composeur)

| paire | fichier | nature | preuve |
|---|---|---|---|
| #2726 × #2766 | `src/ameesh/merge_queue.py` | **non mécanique** : la signature de `review_gate` garde les paramètres du véhicule (#2766 : `cfg`, `project`, `target`, `vehicle`) et ceux des drapeaux d'auteurs (#2726 : `exclude_author_family`, `distinct_families`) ; le dict de sortie porte `vehicle` et les deux drapeaux | compilation OK ; tests ciblés des deux lots + `test_2394_review_gate_verdicts`, `test_2739_review_correction`, `test_l160e_merge_queue` ; **avis pilote requis sur ce delta** (§2, §12) |
| #2800 × #2799 | `src/ameesh/ui/static/ameesh-ui.js` | **mécanique** : le merge des sources (`chat-screen.ts`, `chat.test.mjs`) est propre ; le bundle minifié est régénéré depuis l'arbre fusionné (`npm run build`), `npm run check` vérifie la reproductibilité octet pour octet | `npm run build` + `npm run check` verts ; `ui/test/chat.test.mjs` 10/10 ; aucun avis (§2, union mécanique) |

Le seul commit sans trailer de membre est l'unité 3 (résolution non
mécanique) : la porte du véhicule (#2766) demande un avis sur ce delta propre
et n'en demande pas pour une résolution mécanique ni pour les lots membres,
qui ont leurs avis sur leur tête gelée.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2786 | conflit avec `main` (`src/ameesh/plan_git.py`), aucune remise calculée sur `797779c2d` ; prêt au sens de la file mais non composable en l'état ; au paquet suivant après remise |
| #114 | `verdicts-missing` : un seul avis (deepseek) sur le gel, deux familles requises pour un lot sensible |
| #1515 | `verdicts-missing` : un seul avis (codex) sur le gel, deux familles requises pour un lot sensible |
| #2789 | `verdicts-missing` : un seul avis (deepseek) sur le gel, deux familles requises pour un lot sensible |
| #2614 | `no-verdict` : gel effectif refusé |
| #2771 | `no-verdict` : gel effectif refusé ; la lecture d'objet du premier calcul (`blob:none`) a été résolue au second calcul (0 erreur) |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py`, base `797779c2d` :
  **11 prêts, 6 en attente, 0 erreur** (`rc 0`) ; `--ready-out` écrit.
- Composition : **11 unités, 11 lots** ; 1 conflit non mécanique
  (`merge_queue.py`, #2726 × #2766) et 1 conflit mécanique de bundle
  (#2800 × #2799), tous deux résolus et documentés ci-dessus.
- Arbre : 36 fichiers, +2301/−248 hors manifeste ; aucun marqueur de conflit ;
  pas de migration. `git diff --check` ne relève que les lignes à blanc de fin
  de ligne du bundle minifié de #2799/#2800 (même défaut déjà présent sur la
  tête de #381, régénéré ici) : rien d'introduit par la composition.
- `scripts/garde-mecanique.py --json` : **0 constat**, 4 exemptions utilisées
  (#2642 et #2726), 0 inutilisée.
- `scripts/doc-links.py` : 172 fichiers, **0 lien cassé**.
- Contrôle d'inclusion `scripts/paquet-inclusion.py` (manifeste, tête
  `8bdb50467`, liste prête, 12 admissions bureau) : **aucun défaut détecté** —
  11 compositions, 0 retrait ; #2786 exclusion consignée et lue ; contenus
  partiels « à lire » attendus et documentés : #2766 (2 lignes du hunk de
  signature/dict de `review_gate` remplacées par la résolution #2726) et
  #2799 (ligne de bundle régénérée par #2800), cf. « Résolutions ».
- Tests ciblés : `scripts/test.sh` sur les 15 modules impactés des unités
  (merge/revue, désignation, plan, veille de paquet, gel, reprise, UI), passes
  psql et psycopg ; résultat ci-dessous.
