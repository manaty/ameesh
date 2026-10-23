# Paquet integration/paquet-h-20261020-1300 — composition (§12)

Base : `main` **`1e35394de01e`** (fusion de #2874 hors paquet, exception
incident, PR #2191, 20/10 ~14:20). Branche `integration/paquet-h-20261020-1300`,
cible `main`. Une seule suite complète est demandée sur la tête finale.

Composeur : mesh-design (pilote, §12 : « le pilote garde la capacité et
compose lui-même quand le composeur ne le fait pas » ; mty-amh-impl-01 sur
#2578, prévenu à 13:0x et à 14:30). Demandes : auditeur 12:42 et 13:05 —
paniers #2823, #2776, #2854 en SLO niveau 3, à composer dès la fusion de h-0900.

**Recomposition (14:3x).** La première composition (7 unités sur `aef74e256089`,
tête `bfa4ca16a835`) est rouge au run 39414426281 : deux conflits sémantiques
avec `main`, plus `test_423` (environnement du runner, #1801). Entre-temps
`main` a reçu #2874. Le paquet est réaligné par fusion de `main` (`3960dd76c`,
propre, contenu relu inchangé), puis recomposé depuis la liste calculée sur
`1e35394de01e` : `scripts/paquet-candidats.py` rend **14 prêts, 5 en attente
(dont ce véhicule), 0 erreur** ; les 14 prêts sont composés.

## Admissions (14 unités, 14 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2814 | `e8eb7ee61a63` (tête gelée, gel 14682) | sensitive | auditeur (Claude) + cx3 (Codex) | admission consciente du conteneur et capacité élastique ; recomposé à sa nouvelle tête qui porte la garde `cgroup` de `_reading` (lot 2851) ; auteur impl-05 |
| #2864 | `b1cc5962a548` (tête gelée) | sensitive | auditeur (Claude) + cx3 (Codex) | proposition de relecture : la famille du relecteur lit les auteurs de la plage gelée ; auteur impl-05 |
| #2594 | `05aa71905022` (tête gelée) | sensitive | impl-08 (DeepSeek) + auditeur (Claude) | documentation d'alerte partagée ; auteur cx1 |
| #2637 | `aa7f086d6217` (tête gelée) | sensitive | auditeur (Claude) + cx3 (Codex) | désignation : un résultat manuel ne prétend plus couvrir les avis quand `prepare` échoue ; auteur deepseek2 |
| #2776 | `8c9711c0d6ae` (tête gelée) | sensitive | cx3 (Codex) + auditeur (Claude) | panier du lot 2739 : désignations lues dans tout le journal ; auteur impl-04 |
| #2823 | `f02d54978388` (tête gelée) | sensitive | auditeur (Claude) + impl-08 (DeepSeek) | panier du lot 2702 : gel non prouvé ; auteur cx3 |
| #2854 | `efb987ea23ca` (tête gelée) | light | cx3 (Codex) | panier du lot 2844 : reprise des reçus d'étape ; auteur impl-06 |
| #2845 | `eb919bc16296` (tête gelée) | sensitive | auditeur (Claude) + impl-01 (DeepSeek) | demandes GitHub : agent de veille des tickets et commentaires (migration 0470) ; auteur cx2 |
| #2853 | `92824f226274` (tête gelée) | sensitive | auditeur (Claude) + cx3 (Codex) | garde auto-merge GitHub : pas de fusion avant le vert de la porte ; auteur deepseek4 |
| #2873 | `85cafb212890` (tête gelée) | sensitive | auditeur (Claude) + cx3 (Codex) | porte de fusion étape 2 : gel/verdict extraits dans `freeze_gate.py` (PR canon manaty/home 143 fusionnée) ; auteur impl-05 |
| #2876 | `49c8233bab9b` (tête gelée) | sensitive | auditeur (Claude) + dev3 (DeepSeek) | une seule suite complète par tête : portier paquet annulé, preuve du run PR ; auteur cx2 |
| #2578 | `b3d990a29386` (tête gelée) | sensitive | auditeur (Claude) + cx3 (Codex) | `vm.sh appel_drain` : mourir du signal reçu ; auteur impl-01 |
| #2669 | `ee1449ebd36d` (tête gelée) | light | cx3 (Codex) | migrations : commentaire de 0467 et référence 0420 ; auteur impl-08 |
| #2869 | `1b95c311f00e` (tête gelée) | light | cx3 (Codex) | panier ameesh du 20/10 : sonde psql hermétique ; auteur qa-01 |

## Composition (14 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2814 | `e8eb7ee61a63` | `9700db21b` | sensitive ; 0 conflit ; recomposé (première unité `379c85af1` sur `31739fe46624`, supersédée) |
| 2 | #2864 | `b1cc5962a548` | `f170a26c3` | sensitive ; 0 conflit |
| 3 | #2594 | `05aa71905022` | `8ac2518ca` | sensitive ; 0 conflit |
| 4 | #2637 | `aa7f086d6217` | `0402ee99a` | sensitive ; 0 conflit |
| 5 | #2776 | `8c9711c0d6ae` | `26a4a26a7` | sensitive ; 0 conflit |
| 6 | #2823 | `f02d54978388` | `eadc3d5f3` | sensitive ; 0 conflit |
| 7 | #2854 | `efb987ea23ca` | `a9706e468` | light ; 0 conflit |
| 8 | #2845 | `eb919bc16296` | `e611a9dd1` | sensitive ; 0 conflit |
| 9 | #2853 | `92824f226274` | `1dd419ae5` | sensitive ; 0 conflit |
| 10 | #2873 | `85cafb212890` | `3d0397b06` | sensitive ; 0 conflit |
| 11 | #2876 | `49c8233bab9b` | `98eabc40a` | sensitive ; 0 conflit |
| 12 | #2578 | `b3d990a29386` | `e6c031df3` | sensitive ; 0 conflit |
| 13 | #2669 | `ee1449ebd36d` | `6fcd6b0e6` | light ; 0 conflit |
| 14 | #2869 | `1b95c311f00e` | `8e4d4ba5b` | light ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`. Le réalignement
`3960dd76c` (fusion de `main` `1e35394de01e`) est propre : aucun conflit, pas
d'unité.

## Résolutions de composition

Aucune : les quatorze fusions et le réalignement sont propres (aucun conflit,
aucun marqueur).

## Adaptations après CI (run 39414426281 sur `bfa4ca16a835`)

| commit | conflit | nature | avis |
|---|---|---|---|
| `a5ead71d3` | #2854 × #2861 (main) : l'étape gelée a la clé `frozen`, un miroir existant s'inscrit depuis son étape courante, l'entrée porte `baseline` | adaptation de test seule (`test_2854_stage_receipts_recovery`), contenu de #2854 inchangé | aucun (§11 bis) |

Le second conflit, #2814 × #2851 (`_reading` ajoutait `cgroup: {}` à la
lecture étroite `disk_history`), est corrigé dans le lot lui-même (tête
`e8eb7ee61a63`, deux avis) : le véhicule n'a **aucun delta propre** hors
adaptation de test et manifeste. `test_423_agent_aliases` (psql 14/16) est une
erreur d'environnement du runner (gpg-agent, #1801), hors composition.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` : deux avis de familles distinctes requis sur `89bdc6b50e93` |
| #2614 | `no-verdict` : gel effectif refusé |
| #2771 | `no-verdict` : gel effectif refusé |
| #2848 | `freeze-not-head` : gel `8eb533de29ef` ≠ tête publiée `1d5cdc4a5a3b` ; deux avis requis |
| #2863 | hors liste : tête publiée `5fe81731646f` après le gel, verdict attendu (orchestrateur) |
| #2871 | lot canon (manaty/home), hors paquet ameesh |
| #2872 | déjà fusionné |
| #2874 | fusionné hors paquet (incident, PR #2191) : c'est la base |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py`, base `1e35394de01e` :
  **14 prêts, 5 en attente, 0 erreur** ; les 14 prêts sont composés, aucun
  écarté.
- Composition : **14 unités, 14 lots** ; aucun conflit, aucune résolution.
- 62 fichiers, +5822/−609 hors manifeste ; `git diff --check` propre.
- Migrations ajoutées : `0469_host_cgroup.sql` (#2814), `0470_github_watch.sql`
  (#2845) ; aucune collision de numéro.
