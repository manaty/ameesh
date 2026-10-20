# Paquet integration/paquet-h-20261020-1300 — composition (§12)

Base : `main` **`aef74e256089`** (fusion du paquet h-0900, PR #2178, 20/10
~12:55). Branche `integration/paquet-h-20261020-1300`, cible `main`. Une
seule suite complète est demandée sur la tête finale.

Composeur : mesh-design (pilote, §12 : « le pilote garde la capacité et
compose lui-même quand le composeur ne le fait pas » ; mty-amh-impl-01 sur
#2578, prévenu à 13:0x). Demandes : auditeur 12:42 et 13:05 — paniers #2823,
#2776, #2854 en SLO niveau 3, à composer dès la fusion de h-0900. La liste
composée est exactement celle de `scripts/paquet-candidats.py` sur
`aef74e256089` : **7 prêts, 11 en attente, 0 erreur** (`--ready-out` écrit).

## Admissions (7 unités, 7 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2814 | `31739fe46624` (tête gelée) | sensitive | auditeur (Claude) + cx2 (Codex) | admission consciente du conteneur et capacité élastique (`pressure_scope: own_cgroup`) ; deux avis (SQL) ; auteur impl-05 |
| #2864 | `b1cc5962a548` (tête gelée) | sensitive | auditeur (Claude) + cx3 (Codex) | proposition de relecture : la famille du relecteur lit les auteurs de la plage gelée ; deux avis (porte de fusion) ; auteur impl-05 |
| #2594 | `05aa71905022` (tête gelée) | sensitive | impl-08 (DeepSeek) + auditeur (Claude) | documentation d'alerte partagée ; deux avis ; auteur cx1 |
| #2637 | `aa7f086d6217` (tête gelée) | sensitive | auditeur (Claude) + cx3 (Codex) | désignation : un résultat manuel ne prétend plus couvrir les avis quand `prepare` échoue ; deux avis (porte de fusion) ; auteur deepseek2 |
| #2776 | `8c9711c0d6ae` (tête gelée) | sensitive | cx3 (Codex) + auditeur (Claude) | panier du lot 2739 : désignations lues dans tout le journal ; deux avis ; auteur impl-04 |
| #2823 | `f02d54978388` (tête gelée) | sensitive | auditeur (Claude) + impl-08 (DeepSeek) | panier du lot 2702 : gel non prouvé ; deux avis ; auteur cx3 |
| #2854 | `efb987ea23ca` (tête gelée) | light | cx3 (Codex) | panier du lot 2844 : reprise des reçus d'étape ; un avis ; auteur impl-06 |

## Composition (7 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2814 | `31739fe46624` | `379c85af1` | sensitive ; 0 conflit |
| 2 | #2864 | `b1cc5962a548` | `f170a26c3` | sensitive ; 0 conflit |
| 3 | #2594 | `05aa71905022` | `8ac2518ca` | sensitive ; 0 conflit |
| 4 | #2637 | `aa7f086d6217` | `0402ee99a` | sensitive ; 0 conflit |
| 5 | #2776 | `8c9711c0d6ae` | `26a4a26a7` | sensitive ; 0 conflit |
| 6 | #2823 | `f02d54978388` | `eadc3d5f3` | sensitive ; 0 conflit |
| 7 | #2854 | `efb987ea23ca` | `a9706e468` | light ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Résolutions de composition

Aucune : les sept fusions sont propres (aucun conflit, aucun marqueur). Le seul
contenu hors fusions est ce manifeste.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` : un seul avis (impl-07, DeepSeek) sur `89bdc6b50e93` ; siège Claude (dev1) en pause budget jusqu'au 21/10 08:00 |
| #2578 | `verdicts-missing` : deux avis requis sur `b3d990a29386` |
| #2614 | `no-verdict` : gel effectif refusé |
| #2771 | `no-verdict` : gel effectif refusé |
| #2845 | `blocked-verdict` : gel effectif refusé |
| #2848 | `verdicts-missing` : deux avis requis sur `8eb533de29ef` |
| #2853 | pas de tête composable dans ce dépôt à la liste |
| #2863 | en attente à la liste (non prêt) |
| #2871 | lot canon (manaty/home), hors paquet ameesh |
| #2872 | lot canon (manaty/home), hors paquet ameesh |
| #2874 | `verdicts-missing` : un avis requis sur `290477241542` (léger) |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py`, base `aef74e256089` :
  **7 prêts, 11 en attente, 0 erreur** ; les 7 prêts sont composés, aucun
  écarté.
- Composition : **7 unités, 7 lots** ; aucun conflit, aucune résolution.
- 32 fichiers, +2918/−104 hors manifeste ; `git diff --check` propre.
- `scripts/paquet-inclusion.py` (manifeste, tête de composition, prêts) :
  **aucun défaut** — 7 compositions, 0 exclusion, 7 contenus présents à la
  tête.
