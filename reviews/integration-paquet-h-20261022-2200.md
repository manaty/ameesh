# Paquet integration/paquet-h-20261022-2200 — composition (§12)

Base : `main` **`e40ab362de43`** (fusion du paquet h-20261021-1000, PR
manaty/ameesh#2294, tête gelée `41112c2f9`, 22/10 01:34 UTC). Branche
`integration/paquet-h-20261022-2200`, cible `main`, lot porteur **#3009**.
Une seule suite complète sur la tête finale.

Composeur : mty-amh-impl-01, sur l'alerte de créneau **22:00** du 22/10
20:15Z (cible libre, aucun porteur `integration/paquet*` ouvert) ; canon
§11 bis et §12 ; liste calculée `scripts/paquet-candidats.py` du 22/10
20:2xZ sur la base `e40ab362`.

## Admissions (2 unités, 2 lots portés)

| lot | source | classe | avis (familles) | auteur | objet |
|---|---|---|---|---|---|
| #2962 | `71ada71d1396` (gel 15307) | sensitive | 2 avis ok (claude dev2 + codex cx2) | mty-amh-impl-07 | Gel et verdict : un SHA complet (40 hex) qui ne se résout dans aucun dépôt est refusé au gel comme au verdict |
| #2971 | `752c047ae1b1` (gel 15515) | sensitive | 2 avis ok (claude dev1 + codex cx2) | mty-amh-impl-06 | Retrait générique des marques machine `ameesh-freeze-*` de `--note` à l'entrée de milestone |

## Composition (2 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2962 | `71ada71d1396` | `2f97ca7272ef` | fusion de la tête gelée, 0 conflit |
| 2 | #2971 | `752c047ae1b1` | `a071b2163427` | fusion de la tête gelée, 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #2914 | `2df95bee4d8b` | **retiré** : la porte ne peut pas lire la provenance du gel antérieur fantôme `66a06cf09d5c` (« author provenance unknown for frozen commit(s) ») — c'est l'erreur unique de la liste calculée. Cause #2989 (P1) encore en qa, sans les deux avis requis sur `d2e057ac` ; le pilote a consigné (21/10 08:16) qu'il faut regeler #2914 après la fusion de #2989, puis le passer au paquet suivant. |
| #2614 | `b6b97f080099` | **retiré** : `verdicts-missing` — un seul avis (`ameesh-cx3`, codex) sur un lot sensible qui exige deux familles indépendantes ; et dépendance bloquante #2994 (qa, `blocked-verdict`) : la porte ne peut pas rendre les deux avis avant le correctif #2994 (constat mesh-design 21/10 08:46). |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #114 | verdicts-missing + conflit `src/ameesh/exploitation.py` |
| #390 | verdicts-missing |
| #480 | verdicts-missing |
| #2771 | verdicts-missing |
| #2913 | verdicts-missing |
| #2920 | verdicts-missing |
| #2940 | verdicts-missing |
| #2954 | verdicts-missing |
| #2955 | blocked-verdict |
| #2957 | verdicts-missing |
| #2965 | blocked-verdict |
| #2989 | verdicts-missing (tête `d2e057ac`, un seul avis codex) |
| #2994 | blocked-verdict |
| #3004 | verdicts-missing |
| #3007 | verdicts-missing |

## Correctif CI du 22/10 20:4xZ

La première exécution complète (run `39848754775`, tête `b942ab68241c`) a
rougi **13 fois sur 4 modules**, toutes de la même cause : #2962 refuse
désormais un verdict dont le SHA complet ne se résout dans aucun dépôt et ne
nomme pas la tête gelée ; des témoins antérieurs posaient un verdict avec un
SHA fictif sur un lot **jamais gelé**. Aucune unité n'est modifiée.

| module | témoin(s) | adaptation |
|---|---|---|
| `test_2971_freeze_note_marks` | 3 | #2962 écrit sa propre ligne `ameesh-freeze-unverified` ; le témoin vérifie la charge fournie par `--note`, pas le nom de la marque |
| `test_1677_prise_empilee` | `test_verdict_et_gel_ne_changent_plus_la_prise` | le dépôt qui porte le SHA est simulé (même modèle que `_verdict` de #2674) |
| `test_1465_provenance` | `test_milestone_outside_a_turn_stays_unknown` | idem |
| `test_789_slo_alerts` | `test_main_local_en_retard_ne_rajoute_pas_un_avis` | le dépôt du commit relu est nommé (`repo=`, comme `--repo`) |
| `test_705_reviewer_designated` | 2 | la tête gelée est posée au stockage (la porte d'écriture du gel a ses propres témoins, #2962) |

- Commit d'adaptation `f78f25720` (sans trailer `ameesh-work:`), aucune unité
  modifiée.
- Tests ciblés locaux sur la tête corrigée : **9 tests OK** (`2971`, `1677`,
  `1465`, `789`, `705`), banc PostgreSQL local.
- La CI complète est redemandée une seule fois sur la tête finale ; l'ancien
  run et son verdict sont remplacés.
- Conflit de fusion avec #2989 préparé hors de cette branche
  (`refs/ameesh/resolutions/3009/2989`, `eef75183a789` ; 61 témoins
  #2786/#2924 verts) : la branche du paquet ne change pas.

## Contrôles

- Liste calculée du 22/10 20:2xZ : **2 lots prêts** (#2962, #2971), 16 en
  attente, **1 erreur** (#2914, porte illisible). L'alerte de créneau annonçait
  quatre « prêts » ; le calcul de la porte ne retient que #2962 et #2971 :
  #2914 est l'erreur connue ci-dessus, #2614 n'a qu'un avis et dépend de #2994.
- Liste prête utilisée pour le contrôle d'inclusion : `2962,2971` (les deux
  lots prêts du calcul ; l'erreur #2914 ne masque aucun lot prêt).
- Contrôle d'inclusion (`scripts/paquet-inclusion.py --manifest … --head a071b216 --ready 2962,2971`) :
  **aucun défaut** — 2 entrées de composition, verdicts « présent » 2/2,
  2 lots attendus, 2 prêts, contenus à la tête 2/2 présents.
- Contrôles statiques du gel (`scripts/freeze-checks.py`) : **6/6 verts**
  (garde mécanique #2449, compilation, attribution #1632, lint L63, alert
  types #1060, doc readers L161a).
- Tests ciblés : la suite complète de la CI sur la tête gelée fait foi. Les
  deux témoins neufs (`tests.test_2962_freeze_unresolved`,
  `tests.test_2971_freeze_note_marks`) ont été lancés localement ; #2962
  résout un faux SHA jusque dans les dépôts distants et l'exécution locale a
  été interrompue par un `git fetch` sortant sans verdict local — la CI les
  rejoue sur la même tête.
