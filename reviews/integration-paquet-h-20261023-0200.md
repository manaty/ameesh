# Paquet integration/paquet-h-20261023-0200 — composition (§12)

Base : `main` **`b7092b41c28f`** (fusion du paquet h-20261022-2200, PR
manaty/ameesh#2307, tête `f0fd8c918`). Branche
`integration/paquet-h-20261023-0200`, cible `main`, lot porteur **#3030**.
Une seule suite complète sur la tête finale.

Composeur : mty-amh-impl-01, sur l'alerte de créneau **02:00** du 23/10
00:15Z (cible libre, aucun porteur `integration/paquet*` ouvert) et le
courrier mesh-design de 00:38Z ajoutant #2965 ; canon §11 bis et §12 ; liste
calculée `scripts/paquet-candidats.py` du 23/10 00:44Z sur la base
`b7092b41`.

## Admissions (5 unités, 5 lots portés)

| lot | source | classe | avis (familles) | auteur | objet |
|---|---|---|---|---|---|
| #393 | `627808b156ce` (gel 15612) | sensitive | 2 avis ok (claude dev1 + codex cx2) | ameesh-dev5 | L68 : suite complète macOS (PostgreSQL dans une étape), tests /proc via la couche plateforme |
| #480 | `c68c37851ecb` (gel 15614) | sensitive | 1 avis ok (claude dev4) | mty-amh-docs-01 | CHAT-VEILLE : l'agent ameesh chat surveille seul les décisions et le courrier entre deux messages |
| #2957 | `eb181d12b7be` (gel 15615) | sensitive | 2 avis ok (claude dev4 + codex cx1) | mty-amh-impl-08 | Panier #2951 : borner les dépôts historiques de `ci_queue_state` |
| #2965 | `e5fd68ec7358` (gel 15617) | sensitive | 2 avis ok (claude dev7 + codex cx1) | mty-amh-impl-02 | Train actif : un lot enrôlé au créneau ou après n'est plus classé late |
| #2913 | `62d5b6427470` (gel 15544) | light | 2 avis ok (claude dev4 + claude dev1) | mty-amh-impl-05 | Panier #2891 : corriger le SHA de l'adaptation DoctorHooksTest dans le manifeste h-2200 |

## Composition (5 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #393 | `627808b156ce` | `fa002f5dc19d` | fusion de la tête gelée, 0 conflit |
| 2 | #480 | `c68c37851ecb` | `747ca8baf760` | fusion de la tête gelée, 0 conflit |
| 3 | #2957 | `eb181d12b7be` | `ca307aa11fed` | fusion de la tête gelée, 0 conflit |
| 4 | #2965 | `e5fd68ec7358` | `8685e065a005` | fusion de la tête gelée, 0 conflit |
| 5 | #2913 | `62d5b6427470` | `b0a19a22889c` | fusion de la tête gelée, 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #2914 | `2df95bee4d8b` | **retiré** : la porte ne peut pas lire la provenance du gel antérieur fantôme `66a06cf09d5c` (« author provenance unknown for frozen commit(s) ») — erreur unique de la liste calculée. Cause #2989 (P1) encore en qa ; à regeler après sa fusion, puis au paquet suivant. |
| #2951 | — | **panier parent** cité par le titre de #2957, déjà fusionné : référence de contenu, pas une unité de ce véhicule. |
| #2891 | — | **panier parent** cité par le titre de #2913, déjà fusionné : référence de contenu, pas une unité de ce véhicule. |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #114 | verdicts-missing + conflit `src/ameesh/exploitation.py` |
| #390 | verdicts-missing |
| #2614 | verdicts-missing (un seul avis codex, dépend de #2994) |
| #2771 | verdicts-missing |
| #2920 | verdicts-missing |
| #2940 | verdicts-missing |
| #2954 | verdicts-missing |
| #2955 | blocked-verdict |
| #2987 | branch-not-published |
| #2989 | verdicts-missing (un seul avis codex) |
| #2993 | verdicts-missing |
| #2994 | blocked-verdict |
| #3001 | verdicts-missing (un avis deepseek sur le gel e831c772, second attendu) |
| #3004 | verdicts-missing |
| #3007 | blocked-verdict |
| #3008 | verdicts-missing (un avis codex sur le gel 2af62cfa, second attendu) |
| #3010 | blocked-verdict |
| #3016 | verdicts-missing |

## Contrôles

- Liste calculée du 23/10 00:44Z : **5 lots prêts** (#393, #480, #2957,
  #2965, #2913), 18 en attente, **1 erreur** (#2914, porte illisible sur le
  gel fantôme `66a06cf0`). L'erreur ne masque aucun lot prêt ; elle est
  écartée avec motif ci-dessus.
- Liste prête utilisée pour le contrôle d'inclusion :
  `393,480,2957,2965,2913`.
- Contrôle d'inclusion (`scripts/paquet-inclusion.py --manifest … --head HEAD --ready-file prets-0200.txt --no-expected`) :
  **aucun défaut** — 5 entrées de composition, verdicts « présent » 5/5,
  5 lots prêts, 5/5 contenus à la tête ; exclusions #2914, #2951, #2891
  consignées.
- Contrôles statiques du gel (`scripts/freeze-checks.py`) : rejoués par le
  gel de la tête finale ; le gate les exige.
- Tests ciblés : la suite complète de la CI sur la tête gelée fait foi. Aucun
  test local n'a été relancé pour ce paquet.
