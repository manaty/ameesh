---
type: Study
title: "Contexte minimal par type de tâche — mesure, profil et contrat proposé (TPK #5549, lot #7355)"
description: "Étude du lot #7355 (ticket manaty/ameesh#5549) : mesure du contexte par rôle et par type de tâche sur 10 623 tours de la mesh (7 jours) et sur les traces IMA du demandeur ; décomposition de ce qui est réellement renvoyé à chaque appel ; profil chiffré pour verifier/bug_staging, fixer/qa_pr_merge_conflict, builder/implementation et concierge/chat ; proposition du contrat `ameesh-task-context/1` (fourni d'emblée / à la demande, plafonds de sortie d'outil avec débordement fichier, budget d'étapes, sous-tours, budgets de relecture) ; instrumentation et rétention opt-in caviardée ; découpage des lots pilotes. Aucun code d'exécution, aucune valeur activée, aucune action de production."
status: draft
tags: [contexte, budget, tache, ima, tpk, mesure, contrat, etapes, vie-privee]
generated: { by: "mty-amh-impl-03/deepseek", at: "2026-11-14T10:36:00Z" }
stale_after: 2027-02-14
sources:
  - { resource: "https://github.com/manaty/ameesh/issues/5549", title: "TPK: le contexte minimal par type de tâche (Seb, 14/11/2026), ses deux commentaires de traces (08:29Z et 10:18Z) et l'acceptation du 09:58Z" }
  - { resource: "https://github.com/thepackengers/issue-manager-agent/issues/1792", title: "budget de vérification staging bloqué sans rejeu ; les 40 vérifications bloquées" }
  - { resource: "https://github.com/thepackengers/issue-manager-agent/pull/1793", title: "hausse du budget verifier 2,5 M → 5 M, en attente de cette analyse" }
  - { resource: "../ctx-01-compteurs-contexte.md", title: "contrat `ameesh-context/1` : compteurs, profils, décisions, corpus et rejeu" }
  - { resource: "../etudes/l2894-turn-context-api.md", title: "`ameesh-turn-context/1` : le document de tour remis au harnais et son API" }
  - { resource: "../../../src/ameesh/context_metrics.py", title: "normalisation des mesures de contexte par appel et par tour" }
  - { resource: "../../../src/ameesh/context_corpus.py", title: "corpus `ameesh-context-corpus/1` (export en lecture seule, liste blanche)" }
  - { resource: "../../../src/ameesh/runner.py", title: "budgets de relecture, clôture au point sûr, sessions, `turn.context`" }
  - { resource: "../../../src/ameesh/adapters.py", title: "flux Claude, Codex et dsh : usage par appel, résultats d'outil, points sûrs" }
  - { resource: "../../../docs/EXPLOITATION.md", title: "réglages d'exploitation (`turn_reread_budget`, `session_reread_budget`, `compact_tokens`)" }
  - { resource: "6427-deepseek-premier-tour.md", title: "tours longs contre dumps d'outil : croissance par pas constante, plafond de contexte par harnais" }
  - { resource: "#6677", title: "plafond de contexte par harnais (`context_max_tokens_by_harness`, DeepSeek 250 k)" }
  - { resource: "#6433", title: "série de contexte par pas dans `turn.context` (`steps`, `C_first`, `dC_max`)" }
---

# Contexte minimal par type de tâche

## 1. Décision en une page

**Donner à chaque type de tâche le plus petit contexte qui réussit, et le
prouver par la mesure.** Le contexte est renvoyé à chaque appel du modèle :
son coût total est le produit du nombre d'appels par la taille moyenne du
contexte, et il croît quadratiquement quand le contexte croît à chaque étape.
Les chiffres du demandeur (IMA sur `tpk-stg`) et ceux de notre mesh disent la
même chose : le levier principal n'est ni le prompt système ni la fenêtre du
modèle, mais **le nombre d'appels et les sorties d'outil accumulées**.

Ce que cette étude établit :

1. **La mesure est possible avec l'existant, sans journal de rollout.** Sur
   les 7 jours du 2026-11-07 au 2026-11-14, le grand livre et `turn.context`
   couvrent **10 623 tours, 2 330 sessions, 6 233 tours avec une série
   d'appels**. Total relu : **24,6 milliards de jetons d'entrée**, dont 64,9 %
   pour le rôle `build`. Le tour médian relit 0,87 M de jetons ; le p90 est à
   6,0 M, le p99 à 27,1 M, le maximum à **106,5 M** (367 appels dans un seul
   tour).
2. **Le préambule fixe n'est pas le coût dominant, mais il pèse sur les
   tâches courtes.** Au premier tour d'une session, le contexte d'entrée est
   de **11,8–16,9 k jetons** pour DeepSeek, **25,4–37,0 k** pour Claude, et
   d'environ **19 k** pour le vérificateur Codex d'IMA. Sur une vérification
   de 25 appels, ce préambule est présent dans chaque appel : il compte, mais
   les 24 autres appels et leurs sorties d'outil comptent davantage.
3. **La croissance dans le tour vient surtout des outils et du raisonnement
   reporté, et l'extrême vient du nombre d'étapes.** La différence
   `C_last - C_first` est de 36 k jetons médians pour `build`/DeepSeek (22
   appels médians) et de 59 k pour `review`/DeepSeek (24 appels). La part
   « sorties d'outil » de cette croissance est estimée à 50 % environ pour
   DeepSeek et à 60–67 % pour Claude (le reste est la sortie du modèle, lue au
   grand livre). Les plus gros pas observés atteignent 34 k jetons (`dC_max`),
   et le demandeur a mesuré des dumps uniques de 51 k à 178 k caractères chez
   IMA. Mais à croissance par pas constante, ce sont les tours de 200–370
   appels qui font les centaines de millions de jetons relus (#6427, §4.3) :
   le plafond d'outil seul ne les ferme pas.
4. **Le cache masque le coût, pas la latence ni le budget.** 97 à 99 % des
   jetons relus sont des lectures de cache ; c'est précisément pour cela que
   les budgets IMA comptent 96–98 % de cache et coupent quand même. Un jeton
   de cache reste un jeton relu, facturé à un tarif réduit, et surtout un
   jeton qui traverse le réseau et le harnais à chaque appel.
5. **Le mécanisme de budget existe déjà, mais il est éteint par défaut.**
   `turn_reread_budget` vaut 0 (désactivé) : sur les **2 041 tours au-dessus
   de 2,5 M** de la semaine, **1 586 (77,7 %) sont « unbounded »**, sans
   politique ; 231 sont clos par rotation, 223 par durée, 1 par compaction.
   Le levier n'est donc pas à inventer, il est à configurer par type de tâche
   et à compléter par les plafonds d'outil et le budget d'étapes qui manquent.
6. **Le contrat proposé s'appelle `ameesh-task-context/1`.** Le demandeur
   (IMA, puis tout intégrateur) déclare un **type de tâche opaque** et un
   **profil** dans le message qui ouvre le tour ; ameesh valide, applique ce
   qu'il sait appliquer (budgets de relecture, budget d'étapes, politique de
   session, seuil de compaction, options de préambule), publie le profil
   résolu dans `turn.context` et dans `ameesh-turn-context/1`, et laisse à
   l'intégrateur ce qui lui appartient (distillation de la consigne, plafond
   et débordement des sorties d'outil, découpage en phases). **Aucune valeur
   propre à un client n'entre dans le cœur d'ameesh** : le schéma et les
   bornes sont à ameesh, les valeurs sont au propriétaire de la tâche.
7. **Les profils proposés, chiffrés et à valider par les pilotes :**
   - `verifier/bug_staging` : 3 k jetons fournis d'emblée, sortie d'outil
     plafonnée à 4 k caractères (débordement fichier), 15 étapes, budget de
     tour 600 k, session par vérification, pas de repo ni de skills ;
   - `fixer/qa_pr_merge_conflict` : 6 k fournis, 4 k/60 k d'outils, 20 étapes,
     budget 1 M, session par PR, sous-tour neuf après l'édition et après les
     tests ;
   - `builder/implementation` : 10 k fournis, 4 k/100 k d'outils, 30 étapes,
     budget 2 M, session par lot, sous-tour aux frontières de phase ;
   - `concierge/chat` : 2 k fournis, 2 k/8 k d'outils, 5 étapes, budget 200 k.
8. **Effet attendu, à mesurer et non à croire.** Le demandeur estime le
   vérificateur à ~0,3 M par vérification (pire cas ~0,5 M) contre 0,7–2,5 M
   aujourd'hui ; notre profil fixe la cible à **≤ 40 % des jetons par vérification
   réussie par rapport à la référence** et **aucune baisse du taux de succès**. Pour le
   fixer, la cible est **≤ 50 % des 3,1 M actuels** ; pour le builder, la
   cible sera fixée après les deux premiers pilotes, pas avant.
9. **Sur la hausse #1793 (2,5 M → 5 M) :** elle débloque les 40 vérifications
   coincées et permet de mesurer la nouvelle politique sur des cas réels ;
   elle ne corrige pas la croissance quadratique. Recommandation : la traiter
   comme **mesure d'attente explicite** (propriétaire), mesurer les deux
   régimes, et redescendre après le pilote si les chiffres le permettent. Ce
   n'est pas une décision d'agent.
10. **Ce lot ne livre que les étapes 1–3** : la mesure, le profil écrit et le
    contrat proposé. Aucun code, aucune migration, aucune valeur posée sur un
    agent, aucun déploiement, aucune action de production. Les pilotes 4–5 et
    le builder sont découpés en lots distincts (§8).

---

## 2. Ce que ce lot livre, et ce qu'il ne livre pas

### 2.1 Livré

- une **mesure reproductible** du contexte sur notre mesh (7 jours), avec la
  fenêtre, les sources, les conventions et les limites (§3) ;
- une **décomposition** de ce qui est réellement renvoyé à chaque appel, dans
  la mesure où les compteurs existants le permettent, et la liste nommée de
  ce qu'ils ne permettent pas encore (§4) ;
- un **profil de contexte par type de tâche**, chiffré et daté, pour les deux
  tâches pilotes et pour le builder (§5) ;
- le **contrat `ameesh-task-context/1`** : schéma, provenance, matrice
  d'application, bornes dures, publication, cycle de version, modes d'échec
  (§6) ;
- le **plan de mesure continue et de rétention opt-in caviardée** (§7), et
  le **découpage des pilotes** avec leurs critères de succès (§8).

### 2.2 Non livré (et pourquoi)

- **Aucun code d'exécution.** Le schéma est une proposition ; le premier lot
  d'implémentation le fera vivre. Une étude qui modifierait le runner et les
  budgets dans le même geste mélangerait conception et activation.
- **Aucune migration.** Le profil voyage dans le `payload` JSONB du message
  (additif, sans schéma de base) et dans le document de tour ; si un champ
  doit être indexé plus tard, ce sera une décision du lot d'implémentation.
- **Aucune activation** : `turn_reread_budget`, `session_reread_budget`,
  `compact_tokens`, `context_max_tokens`, `session_max_tokens` restent aux
  valeurs actuelles. Aucun agent n'est reconfiguré.
- **Aucune rétention de contenu** : la rétention est proposée, pas ouverte.
  Le volet contenu est **sensible** (vie privée, données de staging) et suit
  le chemin de revue sensible s'il est découpé ; les compteurs seuls, eux,
  ne conservent aucun contenu.
- **Aucune action de production, aucun merge, aucun commentaire de release.**
  Le ticket miroir #5549 est commenté à chaque étape par la veille ; cette
  étude ne fusionne rien et ne déploie rien.

### 2.3 Ce qui existait déjà et n'est pas refait

| Lot | Apport | Ce que cette étude en réutilise |
|---|---|---|
| #4517 | contrat `ameesh-context/1` : `C_last`, `C_next`, `R_turn`, profils, décisions | toutes les définitions et conventions de mesure |
| #6040 | seuil de compaction native par agent (`compact_tokens`), Codex en cours de tour | le levier « compaction » du profil, sans changer le seuil |
| #6046 | budgets de contexte par classe de rôle (`session.by_role`) | la précédence agent → rôle → hôte → défaut, réutilisée par le profil de tâche |
| #5250 | pilote de politique de contexte réversible (apply/revert/status/check) | le mécanisme de mesure avant/après et de retour arrière |
| #6044/#6433 | compaction observée, pic `C_peak`, série par étape (`steps`, `C_first`, `dC_max`) | la mesure du nombre d'appels et de la croissance dans le tour |
| #6427/#6677 | plafond de contexte par harnais (DeepSeek 250 k) et mesure des tours longs | la garde par appel, laissée telle quelle ; le profil y ajoute la borne cumulative |
| #5134/#5135 | corpus de rejeu `ameesh-context-corpus/1` et rejeu contrefactuel | la source de mesure reproductible et la méthode de comparaison |
| #2894 | document de tour `ameesh-turn-context/1` et `ameesh turn current` | le canal de publication du profil vers le harnais |

---

## 3. Mesure

### 3.1 Sources et fenêtre

| Source | Contenu | Fenêtre | Volume |
|---|---|---|---|
| `ameesh context corpus export` | une ligne par tour : mesures `ameesh-context/1`, profil, décision observée, politique, série d'appels ; **aucun prompt, aucune sortie d'outil, aucun secret** | 2026-11-07T10:29Z → 2026-11-14T10:29Z | 10 623 tours, 2 330 sessions, 6 233 tours avec série d'appels |
| `ameesh cost turns --json` | grand livre : `input_tokens`, `cached_input_tokens`, `output_tokens`, modèle, session, lot, nature du tour | même fenêtre | 10 623 lignes |
| `ameesh cost turns --over 2500000` | tours au-dessus du seuil IMA, avec `R_turn`, `C_last`, `C_peak`, politique et motif | même fenêtre | 2 041 tours |
| traces IMA #5549 | agrégats 7 jours par rôle/type et 8 sessions de vérification détaillées | 2026-11-07 → 2026-11-14 | 1 396 tours, 8 sessions |

Les chiffres IMA sont ceux du demandeur ; ils ne sont pas re-mesurés ici. Les
chiffres de notre mesh sont mesurés sur la base d'ameesh. La transposition est
explicite : mêmes harnais (Claude, Codex, DeepSeek), même runner, rôles
comparables, mais **ce ne sont pas les mêmes tâches** ; c'est une validation
d'ordre de grandeur, pas une copie.

### 3.2 Conventions de lecture

- **Un jeton est un jeton de flux**, pas un caractère. Pour convertir les
  tailles en caractères du demandeur, on utilise **4 caractères ≈ 1 jeton**
  (ordre de grandeur du code et de l'anglais), comme dans #5549 ; la
  conversion est signalée à chaque fois.
- **`C_first`** : entrée du premier appel d'un tour. Au **premier tour d'une
  session**, c'est le préambule fixe plus la première consigne ; c'est donc
  une **borne supérieure** du préambule, et le minimum observé en est la
  meilleure estimation pour une consigne courte.
- **`C_last`** : entrée du dernier appel du tour. **`dC_max`** : plus grande
  croissance entre deux appels du tour, sortie du modèle comprise ; c'est une
  borne supérieure de la plus grosse sortie d'outil.
- **`R_turn`** : somme des entrées relues par les appels du tour, cache lu
  compris, cache écrit exclu (contrat `ameesh-context/1` §2.2). C'est la
  grandeur qui compte pour un budget de relecture.
- **`pas`** : nombre d'appels au modèle dans le tour, publié par la série
  d'appels (`series.steps`). Codex non résident ne publie pas d'usage par
  appel : ses pas sont inconnus, pas nuls.
- **Contexte moyen par appel** : `R_turn / pas`, calculé seulement quand les
  deux mesures existent. C'est la taille moyenne réellement renvoyée.
- **Cache** : Claude = cache lu / (entrée + cache lu + cache écrit) ; Codex =
  cache inclus dans l'entrée / entrée ; DeepSeek = cache lu / (entrée + cache
  lu). Aucune comparaison brute entre harnais (contrat §2.2).
- **Absence** : une mesure absente reste `—`, jamais `0` ; les moyennes ne
  sont calculées que sur les valeurs présentes, et le nombre de valeurs est
  donné quand il diffère du nombre de tours.

### 3.3 Notre mesh, par rôle et par harnais

Rôles du contrat `ameesh-context/1` (§3.6), 7 jours, valeurs en jetons.
`pas` = appels par tour ; `C_first session` = premier tour d'une session
(préambule + première consigne) ; `entrée 7 j` = somme des entrées relues
(lectures de cache comprises).

| rôle | harnais | tours | sessions | tours/session | pas/tour méd. (p90) | C_first session méd. [p10–p90] | C_last méd. (p90) | croissance méd. | dC_max méd. (p90) | R_turn moyen | R_turn méd. (p90) | sortie méd. (grand livre) | durée méd. | entrée 7 j |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| audit | claude | 876 | 33 | 26,5 | 5,0 (13) | 26 371 [25 472–35 244] | 219 092 (374 548) | 4 898 | 2 876 (7 663) | 1,20 M | 0,85 M (2,53 M) | 2 181 | 42,7 s | 1,05 G |
| audit | codex | 29 | 6 | 4,8 | — | — | 75 002 (100 159) | — | — | — | — | 51 | 9,4 s | 0,01 G |
| build | claude | 1 414 | 503 | 2,8 | 7,0 (32) | 30 994 [25 351–37 013] | 96 108 (238 769) | 9 621 | 4 709 (12 194) | 1,28 M | 0,59 M (2,82 M) | 3 012 | 53,4 s | 1,81 G |
| build | codex | 753 | 150 | 5,0 | — | — | 81 831 (144 851) | — | — | — | — | 1 155 | 46,8 s | 0,81 G |
| build | deepseek | 2 306 | 1 014 | 2,3 | 22,0 (114) | 14 691 [11 729–16 876] | 174 952 (295 362) | 36 440 | 6 208 (15 051) | 5,79 M | 2,10 M (17,33 M) | 16 400 | 233,0 s | 13,35 G |
| docs | deepseek | 499 | 174 | 2,9 | 12,0 (72) | 15 423 [12 057–17 086] | 136 193 (251 951) | 20 394 | 4 975 (13 428) | 3,23 M | 1,12 M (9,02 M) | 9 410 | 128,5 s | 1,61 G |
| other | deepseek | 165 | 72 | 2,3 | 23,0 (83) | 15 672 [14 921–16 986] | 192 073 (344 543) | 45 479 | 7 268 (18 697) | 5,26 M | 2,60 M (12,91 M) | 21 699 | 282,5 s | 0,87 G |
| pilot | claude | 3 248 | 80 | 40,6 | 4,0 (13) | 27 593 [26 673–30 944] | 225 483 (350 136) | 2 839 | 1 392 (4 689) | 1,09 M | 0,75 M (2,28 M) | 1 976 | 41,6 s | 3,53 G |
| pilot | codex | 53 | 13 | 4,1 | — | — | 61 299 (101 856) | — | — | — | — | 894 | 41,6 s | 0,02 G |
| review | codex | 1 160 | 224 | 5,2 | — | — | 87 585 (145 139) | — | — | — | — | 1 166 | 46,3 s | 0,99 G |
| review | deepseek | 120 | 61 | 2,0 | 24,0 (91) | 12 357 [11 451–13 303] | 182 201 (250 608) | 58 736 | 8 992 (16 376) | 4,64 M | 2,55 M (11,01 M) | 28 660 | 329,6 s | 0,56 G |

Lecture :

- **`build`/DeepSeek est le centre de coût** : 13,35 G sur 24,6 G, 22 appels
  par tour, 131 k jetons de contexte moyen par appel, 2,1 M médians par tour,
  17,3 M au p90. C'est le transposé le plus proche du `builder/implementation`
  d'IMA (7,1 M de moyenne, 14,8 M au p90) : **notre moyenne de 5,79 M est du
  même ordre**.
- **`review`/DeepSeek** (24 appels, 118 k par appel, 2,55 M médians) est le
  transposé le plus proche du `fixer/qa_pr_merge_conflict` (3,1 M de moyenne) :
  **notre moyenne de 4,64 M est du même ordre**, avec un p90 de 11 M.
- **`pilot`/Claude** a peu d'appels par tour (4) mais des sessions très
  longues (40,6 tours, 80 sessions) et un contexte moyen par appel de 213 k :
  la session accumulée pèse plus que le préambule (27,6 k). `audit`/Claude
  est dans la même famille (26,5 tours par session, 202 k par appel).
- **Codex ne publie pas d'usage par appel** : les colonnes `pas`, `C_first`,
  `dC_max` et `R_turn` restent inconnues, et ne doivent pas être lues comme
  zéro. C'est une limite de mesure, pas un résultat.

### 3.4 Distribution et tours au-dessus du seuil

Sur les 8 624 tours dont `R_turn` est connu :

| seuil de `R_turn` | tours au-dessus | part |
|---|---:|---:|
| > 1 M | 3 963 | 46,0 % |
| > 2,5 M | 1 893 | 22,0 % |
| > 5 M | 1 055 | 12,2 % |
| > 10 M | 531 | 6,2 % |

Sur les 6 233 tours dont le nombre d'appels est connu :

| seuil de pas | tours au-dessus | part |
|---|---:|---:|
| > 10 | 2 182 | 35,0 % |
| > 15 | 1 673 | 26,8 % |
| > 20 | 1 408 | 22,6 % |
| > 30 | 1 133 | 18,2 % |
| > 50 | 773 | 12,4 % |
| > 100 | 251 | 4,0 % |

Médiane des pas : 6 ; p90 : 60 ; p99 : 160 ; maximum : **367**. Un seul tour
a relu 106,5 M de jetons (367 appels, `C_last` 410 k, `build`/DeepSeek).

Sur les **2 041 tours au-dessus de 2,5 M** (commande `--over`, tous harnais) :

| motif observé | tours | part |
|---|---:|---:|
| `unbounded` (aucune politique de budget) | 1 586 | 77,7 % |
| rotation (`context_cap` ou `session_size`) | 231 | 11,3 % |
| clôture par durée | 223 | 10,9 % |
| compaction | 1 | 0,0 % |

La politique est absente (`turn_reread_budget` et `compact_tokens` à `null`)
sur **tous** ces tours. Le mécanisme de budget existe (`ameesh set
turn_reread_budget=…`, #5250, et les valeurs par rôle de #6046) ; il n'est pas
configuré ici, ni par rôle ni par type de tâche. C'est le premier levier du contrat, et le moins coûteux à mettre en
œuvre.

### 3.5 IMA, les cinq types de tâche du demandeur

Chiffres du commentaire #5549 du 2026-11-14, `issue_manager.ameesh_role_turns`,
7 jours, `tpk-stg`. « Entrée » = jetons d'entrée moyens par tour, cache
compris.

| rôle / type | tours | entrée moyenne | p90 | cache | sortie moyenne | total entrée |
|---|---:|---:|---:|---:|---:|---:|
| builder / implementation | 164 | 7,1 M | 14,8 M | 98 % | 24 k | 1 158 M |
| verifier / bug_staging | 989 | 455 k | 901 k | 92 % | 5 k | 450 M |
| fixer / qa_pr_merge_conflict | 130 | 3,1 M | 8,1 M | 98 % | 23 k | 403 M |
| fixer / qa_pr_fix_worker | 20 | 2,0 M | 3,4 M | 97 % | 15 k | 41 M |
| concierge / chat | 93 | 201 k | 399 k | 79 % | 2 k | 19 M |

### 3.6 Les huit sessions de vérification staging

Six sessions détaillées dans l'addendum du 10:18Z, plus les deux premières
du 08:29Z. Les deux premières ont servi à l'analyse des appels de polling et
des sorties d'outil ; les six autres confirment la forme.

| mesure | minimum | médiane | moyenne | maximum |
|---|---:|---:|---:|---:|
| appels par vérification | 14 | 25 | 25,8 | 41 |
| entrée du premier appel | 14,8 k | 19,2 k | 18,5 k | 19,3 k |
| total d'entrée | 0,38 M | 0,915 M | 1,02 M | 2,12 M |
| part de cache | 88 % | 93 % | 92,7 % | 95 % |
| plus grosse sortie d'outil | 51 k car. | 63,5 k car. | 94,7 k car. | 178 k car. |

Faits qualitatifs des deux premières sessions, non généralisables :

- **8 et 7 appels sur 24 et 26** ne font que sonder un script en cours
  (`write_stdin`, entrée vide, attente jusqu'à 10 s), soit ~30 % des appels ;
- **144 k et 217 k caractères** de sorties d'outil restent dans le contexte
  jusqu'à la fin ; la plus grosse sortie unique est de 50,9 k (dump de
  configuration tronqué) et **156,6 k** caractères (page ou JSON) ;
- le premier appel part à ~19,3 k jetons avant toute lecture de la tâche :
  instructions de base Codex (~22 k car., ~5,5 k jetons), skills (~3,4 k car.,
  ~0,85 k), rôle multi-agent (~2,5 k car., ~0,6 k), JSON issue+PR (~29,5 k
  car., ~7,4 k) — soit ~14,4 k expliqués, le reste (~4,9 k) étant les schémas
  d'outils/MCP et la consigne.

Le demandeur estime le contrat vérificateur à **~0,3 M par vérification**
(0,5 M dans le pire cas) contre 0,7–2,5 M aujourd'hui. Cette estimation est
reprise comme **cible de pilote**, pas comme résultat.

### 3.7 Ce que ces chiffres ne disent pas

- **Pas de contenu** : le corpus ne conserve ni prompt, ni sortie d'outil, ni
  texte de courrier. On ne peut donc pas dire *quelle* sortie d'outil a été
  utilisée par la réponse, ni quelle partie du prompt système a servi. C'est
  l'objet de §4.4 et §7.
- **Pas de causalité** : une corrélation entre 30 appels et un échec ne prouve
  pas que le nombre d'appels est la cause. Les pilotes sont des expériences
  contrôlées, pas des corrélations.
- **Pas de comparaison brute entre harnais** : les conventions de cache et de
  fenêtre diffèrent (contrat §2.2). Un jeton Claude et un jeton Codex ne sont
  pas la même grandeur.
- **Pas de succès par tour** : le grand livre ne porte pas le résultat de la
  tâche. Les taux de succès des pilotes viennent des reçus d'IMA et de ses
  propres compteurs, pas de `turn_costs`.
- **Pas de mesure Codex par appel** : `codex exec --json` ne publie qu'un
  cumul de fin de tour ; `R_turn`, `C_first` et les pas restent inconnus pour
  ce harnais. `C_last` est lu au journal de session (`last_token_usage`), seul,
  sans cumul. La mesure IMA, elle, lit son propre journal de session ; c'est
  une source différente, à ne pas confondre.
- **Fenêtre courte** : 7 jours, une mesh, une période de paquets et de
  releases. Les chiffres sont datés et reproductibles, pas un catalogue.

---

## 4. Décomposition du contexte

### 4.1 Ce que les compteurs existants séparent

À chaque appel, le contexte contient :

| partie | mesurable aujourd'hui ? | où |
|---|---|---|
| consigne système et rôle du harnais | non directement ; incluse dans `C_first` au premier tour d'une session | `C_first` (borne supérieure), `turn.context.profile` |
| outils et schémas MCP | non ; dans le préambule du harnais | `C_first`, `tools` du descripteur (noms seulement) |
| `AGENTS.md`, skills, instructions de dépôt | non ; dans le préambule | `C_first` |
| données de la tâche (issue, PR, courrier, brief) | partiellement : les messages remis sont connus par leur taille, la consigne est dans le texte du tour | `agent_mailbox`, `turn.context` |
| sorties d'outil accumulées | non ; seulement leur effet sur la croissance | `C_last - C_first`, `dC_max`, `R_turn` |
| raisonnement reporté (sorties du modèle) | oui, en cumul : sortie du tour au grand livre (`O` du contexte quand il est complet) | `turn.context.measures.O`, grand livre |
| historique de session accumulé | oui : `C_first` d'un tour après le premier | `C_first`, `R_session` |

### 4.2 Le préambule fixe, chiffré

| harnais | estimation du préambule | source | qualité |
|---|---:|---|---|
| DeepSeek/dsh | 11,8–16,9 k jetons | p10–p90 des `C_first` au premier tour d'une session, tous rôles | mesuré, consigne courte incluse |
| Claude | 25,4–37,0 k jetons | idem | mesuré, consigne courte incluse |
| Codex (IMA) | ~19,3 k jetons au premier appel | traces #5549 | mesuré par le demandeur |
| Codex (notre mesh) | inconnu | pas d'usage par appel | — |

La décomposition IMA du préambule (en caractères, convertie à 4 car./jeton) :

| partie | caractères | ≈ jetons | part du premier appel (19,3 k) |
|---|---:|---:|---:|
| instructions de base Codex | 22 000 | 5 500 | 28 % |
| skills | 3 400 | 850 | 4 % |
| rôle multi-agent (alors que le spawn est désactivé) | 2 500 | 625 | 3 % |
| JSON issue + PR (commentaires compris) | 29 500 | 7 375 | 38 % |
| schémas d'outils/MCP et consigne restante | ~19 600 | ~4 900 | 25 % |

Deux conséquences :

1. **Pour une vérification**, le préambule et les données de tâche sont plus
   de 60 % du premier appel ; les supprimer ou les distiller est rentable.
2. **Pour un builder**, le préambule est noyé dans des contextes de 80–200 k :
   le gain principal vient des appels et des sorties d'outil, pas des 20 k du
   début. C'est pourquoi le contrat n'a pas la même valeur au même endroit.

### 4.3 La croissance dans le tour

`C_last - C_first` mesure ce qui s'ajoute pendant le tour : sorties d'outil
plus sorties du modèle (raisonnement et texte). En retirant la sortie du tour
lue au **grand livre** (et non le `O` du contexte, qui sous-déclare la sortie
de Claude quand le flux omet l'usage d'un appel), on obtient une **estimation
haute** de la part des outils. Le tableau ne garde que les tours à croissance
positive et à sortie connue :

| rôle / harnais | tours retenus | croissance médiane | sortie médiane (grand livre) | part outils estimée | `dC_max` p90 |
|---|---:|---:|---:|---:|---:|
| build / DeepSeek | 1 380 | 57 838 | 26 405 | 53 % | 15 051 |
| review / DeepSeek | 96 | 89 108 | 38 374 | 53 % | 16 376 |
| docs / DeepSeek | 319 | 27 903 | 13 185 | 50 % | 13 428 |
| other / DeepSeek | 132 | 62 878 | 31 404 | 48 % | 18 697 |
| build / Claude | 897 | 11 420 | 3 645 | 67 % | 12 194 |
| audit / Claude | 540 | 6 158 | 2 431 | 60 % | 7 663 |
| pilot / Claude | 2 082 | 3 456 | 2 243 | 22 % | 4 689 |

Pour DeepSeek, la moitié de la croissance est donc du raisonnement reporté,
l'autre moitié des sorties d'outil. Pour Claude, les outils comptent pour
60–67 % dans `audit` et `build`, mais seulement 22 % dans `pilot`, où la
conversation domine. Un plafond d'outil seul ne suffit pas pour DeepSeek :
le budget d'étapes et le sous-tour neuf sont aussi nécessaires ; pour un
`pilot`, c'est la session accumulée qu'il faut borner.

**Confronté à l'étude voisine #6427** (premiers tours DeepSeek ≥ 300 k,
fenêtre du 8 novembre) : sur 127 premiers tours frais, la croissance par pas
est la même pour les gros et les petits tours (≈1,3 k contre ≈1,4 k) ; seuls
le nombre de pas et la durée changent. Environ 40–55 % de cette croissance
est la sortie du modèle, le reste (≈700 jetons par pas) est de l'entrée et
des sorties d'outil. Six des sept premiers tours ≥ 300 k sont donc des tours
longs (≈210–300 pas, 24–30 min), **pas des dumps d'outil non tronqués** ;
seul 587218 (≈5 k par pas sur ≈59 pas) laisse penser à de grosses sorties,
que le corpus ne sait pas distinguer sans série par pas (#6433). Les deux
mesures ne se contredisent pas : ici, 53 % de la croissance médiane d'un tour
`build` est de l'outil, mais un plafond d'outil réduit la croissance **par
pas**, il ne borne ni le nombre de pas ni la relecture cumulée. C'est
pourquoi le profil (§6) combine plafond d'outil, budget d'étapes, sous-tour
neuf et budget de relecture, au lieu du seul plafond d'outil.

Le plafond de contexte par harnais (#6427 → #6677 : DeepSeek 250 k depuis le
10/11, `config.py`) ferme le tour au point sûr quand **un appel** atteint
250 k. Il ne borne pas la somme des relectures : 367 appels sous ce plafond
suffisent à dépasser 90 M, et le tour maximal de notre fenêtre relit 106,5 M.
Les deux gardes sont complémentaires ; aucune ne remplace le budget de
relecture cumulée (§6.5).

### 4.4 Quelles parties sont réellement utilisées ?

**Non mesuré aujourd'hui.** Le contrat de compteurs ne conserve pas le
contenu. Trois méthodes sont possibles, par ordre de coût :

1. **Citation** : sur un tour échantillonné, marquer chaque bloc (consigne,
   outil, fichier, issue) et vérifier si un fragment apparaît dans une sortie
   ultérieure du modèle ou dans les arguments d'un appel d'outil. Cela demande
   le contenu, donc la rétention opt-in (§7).
2. **Ablation** : rejouer la même tâche en retirant une partie (le fil GitHub
   complet, les skills, les 100 dernières lignes d'un log) et comparer le
   succès et les jetons. C'est la seule preuve causale ; c'est aussi la plus
   chère, à réserver à un échantillon (par exemple 20 vérifications).
3. **Jamais lu** : un bloc dont aucun fragment n'est cité dans les N tours
   suivants est un candidat à la suppression, pas une preuve. La citation ne
   prouve pas l'usage (le modèle peut lire sans citer), mais l'absence de
   citation sur un grand échantillon est un signal.

Le demandeur a déjà une preuve qualitative : sur TM#930, le premier appel part
sur une découverte de dépôt (`rg --files -g AGENTS.md`) alors que le
vérificateur n'a pas besoin du dépôt. Ce n'est pas une mesure d'usage, c'est
un défaut de conception du brief : le contrat doit fournir d'emblée ce qui est
nécessaire et laisser le reste à la demande.

### 4.5 Ce qu'il faut instrumenter

Sans conserver de contenu, le runner peut publier à chaque tour :

- **sorties d'outil** : nombre, caractères, maximum, total, par nom d'outil
  (`exec`, `write_stdin`, `read_file`, MCP…) ; le flux Claude expose déjà les
  `tool_result`, le flux Codex les `item.completed` avec leur sortie, dsh ses
  étapes ;
- **entrées par catégorie** : au premier appel, taille du préambule (connue
  du descripteur ou mesurée), taille de la consigne et des messages remis ;
- **appels** : déjà comptés (`counts.calls`, `series.steps`) ;
- **sortie du modèle** : déjà mesurée (`O`, `O_last`) ;
- **sous-agents** : déjà mesurés à part.

Ces compteurs suffisent à mesurer la politique et à détecter les dumps
anormaux, sans jamais conserver une ligne de contenu. C'est le premier lot
d'implémentation proposé (§8.2).

---

## 5. Profil de contexte par type de tâche

Les valeurs ci-dessous sont des **propositions**, pas des réglages actifs.
Elles sont dérivées des mesures §3 et des cibles du demandeur ; chaque pilote
les validera ou les corrigera. Les profils sont **génériques** : le type est
un label opaque, les valeurs appartiennent au propriétaire de la tâche, et le
cœur d'ameesh ne connaît aucun client (§7).

### 5.1 `verifier/bug_staging`

| champ | proposition | justification |
|---|---|---|
| fourni d'emblée | brief de reproduction + résultat attendu + critères d'acceptation + origine de staging + règles de capacité, **≤ 3 k jetons** | le vérificateur n'a pas besoin du fil complet ni du dépôt ; le premier appel IMA part à 19,3 k |
| à la demande | fil de l'issue, diff de PR, logs de staging, fichiers du dépôt, capture de courrier | seulement si la vérification en a besoin ; chaque récupération est plafonnée |
| outils | `exec`, `write_stdin`, lecture/grep de fichiers ; **pas de spawn, pas de multi-agent** | le spawn est déjà désactivé ; le préambule multi-agent est du poids mort |
| skills / repo | aucun skill, aucun `AGENTS.md`, pas de découverte de dépôt | le vérificateur ne modifie pas le dépôt |
| plafond de sortie | **4 000 caractères par résultat**, **40 000 caractères par tour**, débordement dans un fichier `$TMPDIR` que l'agent peut grep, marqueur de troncature explicite | les dumps de 51–178 k caractères sont le premier facteur de croissance |
| attente active | `exec` bloquant avec un rendu de **30 s** ; `write_stdin` vide interdit comme attente | ~30 % des appels des deux sessions observées ne font que sonder |
| budget d'étapes | **15 appels** par vérification | 14–41 observés, médiane 25 ; 15 divise le pire cas par ~2,7 |
| budget de tour | **600 000 jetons** relus (`turn_reread_budget`) | ~0,3 M visés, marge de 2× pour les cas difficiles |
| session | **une session par vérification**, pas de réutilisation entre bugs | évite l'historique accumulé |
| compaction | aucune (trop courte) | — |
| sous-tours | phases `setup` (≤3), `check` (≤8), `report` (≤4), avec une note de passage de ≤1 200 caractères entre phases ; nouveau tour à chaque frontière | isole les étapes indépendantes, empêche le contexte de traverser tout le check |
| cible de pilote | **≤ 40 % des jetons par vérification réussie** ; aucun échec supplémentaire ; durée ≤ celle d'aujourd'hui | à mesurer sur 30 vérifications au moins |

Effet attendu : 15 appels × un contexte moyen de ~20–30 k = **0,3–0,45 M**,
contre 0,7–2,5 M. C'est l'ordre de grandeur du demandeur, pas une garantie.

### 5.2 `fixer/qa_pr_merge_conflict`

| champ | proposition | justification |
|---|---|---|
| fourni d'emblée | les fichiers en conflit, les hunks des deux côtés, la base et la tête, les tests concernés, la politique de merge, **≤ 6 k jetons** | un fixer n'a pas besoin du fil GitHub complet ni de tout le dépôt |
| à la demande | fichiers voisins, historique, CI, logs de tests | à la demande, plafonné |
| outils | édition, `exec`, tests, `grep` ; pas de spawn | — |
| plafond de sortie | **4 000 caractères par résultat**, **60 000 par tour**, débordement fichier | les logs de tests et les diffs sont les plus gros |
| budget d'étapes | **20 appels** | 3,1 M de moyenne aujourd'hui ; objectif ≤ 50 % |
| budget de tour | **1 000 000 jetons** | laisse de la place à un conflit réel |
| session | **une session par PR** | — |
| compaction | `compact_tokens` seulement si le harnais le supporte et si le contexte dépasse 200 k ; sinon sous-tour neuf | la compaction perd le cache ; le sous-tour est plus sûr |
| sous-tours | `analyze` (≤6), `edit` (≤8), `test` (≤6), avec note de passage ; nouveau tour après l'édition et après les tests | sépare les étapes indépendantes |
| cible de pilote | **≤ 50 % des jetons par conflit résolu**, taux de résolution inchangé | à mesurer sur 20 conflits |

Effet attendu : 20 appels × ~20–40 k = **0,4–0,8 M**, contre 3,1 M.

### 5.3 `builder/implementation`

| champ | proposition | justification |
|---|---|---|
| fourni d'emblée | lot ou issue distillé, contraintes de conception, `AGENTS.md` du dépôt **seulement s'il est utile**, tests à lancer, **≤ 10 k jetons** | le builder a besoin du dépôt, pas de tout le fil |
| à la demande | fichiers, tests, CI, revue, issue complète | à la demande |
| outils | édition, `exec`, tests, `grep`, CI ; spawn interdit sauf besoin explicite | — |
| plafond de sortie | **4 000 caractères par résultat**, **100 000 par tour**, débordement fichier | les suites de tests et les builds sont les plus gros |
| budget d'étapes | **30 appels** par tour | 22 médians, 114 au p90 ; 30 couvre la médiane et laisse le p90 au sous-tour |
| budget de tour | **2 000 000 jetons** | 2,1 M médians aujourd'hui ; l'objectif est de baisser, pas de couper |
| session | **une session par lot**, rotation à la fusion | décision 0025 |
| compaction | Codex : `compact_tokens` à 250 k si le harnais le supporte ; sinon sous-tour | — |
| sous-tours | `understand` (≤8), `implement` (≤12), `test` (≤10), avec note de passage | isole les phases |
| cible de pilote | après les deux premiers pilotes ; viser **≤ 50 % des jetons par lot livré** | 7,1 M de moyenne IMA, 5,8 M chez nous |

Effet attendu, à titre indicatif seulement : 30 appels × ~40–60 k =
**1,2–1,8 M**, contre 7,1 M. Le builder est le plus difficile : il peut avoir
besoin de plus d'étapes et de plus de contexte ; le pilote décidera.

### 5.4 Les autres types

| type | fourni | plafond | étapes | budget | remarque |
|---|---|---|---|---|---|
| `fixer/qa_pr_fix_worker` | 5 k | 4 k/40 k | 15 | 600 k | proche du fixer, périmètre plus étroit |
| `concierge/chat` | 2 k | 2 k/8 k | 5 | 200 k | 201 k de moyenne IMA ; le gain est surtout le préambule et l'absence d'outils |
| rôles `audit` / `pilot` | selon la mission | 4 k/80 k | 20 | 2 M | ce sont des rôles, pas des types de tâche ; sessions longues : la rotation et le sous-tour comptent plus que le plafond d'outil |

### 5.5 Ce qui est commun, ce qui varie

**Commun à tous les profils :**

- un préambule stable (même ordre, même texte) pour ne pas casser le cache ;
- une consigne distillée, avec le reste **à la demande** ;
- un plafond de sortie d'outil **par résultat** et **par tour**, avec
  débordement fichier et marqueur ;
- un budget d'étapes et un budget de relecture, tous deux **souples** :
  clôture au point sûr, puis continuation, jamais un échec sec ;
- une session liée à la tâche, pas au hasard de l'historique ;
- une note de passage courte entre deux phases.

**Ce qui varie :** la taille du brief, le plafond par tour, le nombre
d'étapes, la politique de session et le besoin de compaction. C'est pourquoi
le profil est **par type de tâche**, pas par agent ni par rôle : deux tâches
du même rôle n'ont pas le même contexte minimal.

---

## 6. Contrat `ameesh-task-context/1`

### 6.1 Principes

1. **Le schéma et les bornes sont à ameesh ; les valeurs sont au propriétaire
   de la tâche.** Aucun nom de client, aucune valeur métier dans le cœur.
2. **Le type de tâche est opaque** : `verifier/bug_staging` est une chaîne
   déclarée par l'intégrateur, jamais un enum du code.
3. **Le profil est déclaré avant le tour**, dans le message qui ouvre le
   tour (payload JSONB additif), et **publié** dans `turn.context` et dans le
   document `ameesh-turn-context/1` : ce qui a été appliqué est lisible.
4. **Une valeur absente reste absente**, jamais un défaut supposé ; un profil
   invalide est refusé avec une raison et le tour part en **legacy**, sans
   bloquer la tâche.
5. **Les budgets sont des clôtures, pas des échecs** : au point sûr, le tour
   se ferme avec une continuation ; la tâche n'est pas déclarée terminée.
6. **Les plafonds d'outil sont appliqués par l'intégrateur**, qui voit les
   appels d'outil ; ameesh les publie et les **mesure**, il ne réécrit pas le
   flux d'un harnais qu'il ne médie pas.
7. **Le profil n'est pas une autorisation** : il ne donne aucun droit, ne
   remplace pas le bail, le budget de compte ou les gardes. Une valeur hors
   bornes est refusée, jamais appliquée silencieusement.

### 6.2 Déclaration dans le message

Le message qui ouvre le tour peut porter :

```json
{
  "ima": {"...": "..."},
  "task": {
    "kind": "verifier/bug_staging",
    "schema": "ameesh-task-context/1",
    "version": 3,
    "source": {"by": "ima", "at": "2026-11-14", "ticket": "#1792"},
    "up_front": {"max_tokens": 3000, "parts": ["brief", "staging_origin", "capabilities"]},
    "on_demand": {"parts": ["issue_thread", "pr_diff", "staging_logs", "repo_files"], "max_tokens_per_fetch": 4000},
    "output": {"chars_per_call": 4000, "chars_per_turn": 40000, "spill": "file", "marker": true},
    "steps": {"budget": 15, "yield_seconds": 30, "close_at_safe_point": true},
    "session": {"policy": "par-tache", "turn_reread_budget": 600000, "session_reread_budget": 2000000, "compact_tokens": null},
    "phases": ["setup", "check", "report"]
  }
}
```

Un profil absent ou vide signifie **legacy** : aucun budget de tâche, aucun
plafond d'outil, aucun budget d'étapes. C'est le comportement d'aujourd'hui,
et il reste le défaut tant qu'aucune valeur n'a été posée.

### 6.3 Schéma

`ameesh-task-context/1`, objet JSON, champs optionnels sauf `kind` :

| champ | type | sens | bornes proposées |
|---|---|---|---|
| `kind` | string 1–120 | label opaque du type de tâche | non vide, pas de chemin |
| `version` | entier ≥1 | version du profil côté propriétaire | — |
| `source` | objet `{by, at, ticket}` | qui a écrit le profil, quand, pour quel ticket | `at` ISO 8601, `by` non vide |
| `up_front.max_tokens` | entier | taille maximale de ce qui est fourni avant le premier appel | 500–50 000 |
| `up_front.parts` | liste de strings | classes fournies d'emblée | ≤ 12, labels opaques |
| `on_demand.parts` | liste de strings | classes récupérables à la demande | ≤ 20 |
| `on_demand.max_tokens_per_fetch` | entier | plafond d'une récupération | 500–20 000 |
| `output.chars_per_call` | entier | plafond d'une sortie d'outil | 1 000–16 000 |
| `output.chars_per_turn` | entier | plafond cumulé par tour | 4 000–200 000 |
| `output.spill` | `"file"` ou `"none"` | débordement de la sortie complète | défaut `file` |
| `output.marker` | booléen | marqueur de troncature explicite | défaut `true` |
| `steps.budget` | entier | nombre d'appels avant clôture au point sûr | 1–100 ; `0` = legacy |
| `steps.yield_seconds` | entier | attente bloquante maximale d'un outil | 10–120 |
| `steps.close_at_safe_point` | booléen | clôture souple, continuation | défaut `true` |
| `session.policy` | `par-tache`, `par-lot`, `taille`, `jamais`, `heritee` | portée de la session ; `par-tache` est nouveau, les autres sont les valeurs existantes | défaut `heritee` (legacy) |
| `session.turn_reread_budget` | entier | budget de relecture du tour | `0` ou 100 000–10 000 000 |
| `session.session_reread_budget` | entier | budget de relecture de session | `0` ou 1 000 000–100 000 000 |
| `session.compact_tokens` | entier ou null | seuil de compaction native | `null` ou ≥ 10 000 (plancher #6040) |
| `phases` | liste de strings | frontières de phase | ≤ 10, labels opaques |
| `preamble` | objet | options de préambule (skills, multi-agent, repo) | voir ci-dessous |
| `handoff.max_chars` | entier | taille de la note de passage | 200–4 000 |

`preamble` peut porter `skills` (`on`/`off`), `multi_agent` (`on`/`off`),
`repo_instructions` (`none`/`listed`), `stable_prefix` (booléen). Ces options
sont **indicatives** : ameesh les applique s'il sait le faire avec le
descripteur du harnais, sinon il les publie et l'intégrateur les applique.

### 6.4 Matrice d'application

| champ | ameesh (runner) | intégrateur (IMA) | harnais |
|---|---|---|---|
| `up_front` | valide la taille, publie | compose la consigne distillée | — |
| `on_demand` | publie | fournit l'outil de récupération et son plafond | — |
| `output.chars_per_call/turn` | publie, **mesure** | tronque, écrit le fichier de débordement, pose le marqueur | peut avoir son propre plafond |
| `steps.budget` | **applique** (nouveau), clôt au point sûr, publie | découpe en phases | — |
| `session.policy` | **applique** (`par-lot`/`taille`/`jamais` existants ; `par-tache` nouveau) | choisit la portée | — |
| `session.turn_reread_budget` | **applique** (existant) | — | — |
| `session.session_reread_budget` | **applique** (existant) | — | — |
| `session.compact_tokens` | **applique** (existant, si le descripteur le permet) | — | compaction native |
| `phases` | publie, peut clore à la frontière | découpe, envoie le tour suivant | — |
| `preamble` | applique si le descripteur le permet | sinon configure son profil de harnais | lit `AGENTS.md`/skills |
| `handoff` | borne la note | l'écrit / la lit | — |

**Règle de partage** : tout ce qui est un **compteur** (budget, étapes,
session) est appliqué par ameesh, car lui seul voit le tour complet et le
point sûr. Tout ce qui est un **contenu** (brief, récupération, troncature)
est appliqué par l'intégrateur, car lui seul connaît la tâche et ses outils.
Le harnais garde son préambule et sa compaction native.

### 6.5 Bornes dures et valeurs par défaut

- **Aucune valeur de profil ne peut dépasser les bornes du tableau.** Un
  dépassement est refusé avant le premier appel, avec une raison ; le tour
  n'est pas perdu pour autant : il retombe sur le legacy et l'incident est
  publié (`task_context.invalid`). Un profil invalide ne doit jamais bloquer
  une tâche en production.
- **`0` signifie legacy** pour `steps.budget` et les budgets de relecture ;
  ce n'est pas « zéro étape » ni « zéro jeton ».
- **`compact_tokens`** garde le plancher de 10 000 de #6040 ; `null` signifie
  « ne rien passer », pas « seuil par défaut d'ameesh ».
- **Les plafonds d'outil ne peuvent pas être `0`** : un outil muet rendrait
  la tâche impossible. Le minimum est 1 000 caractères par appel.
- **Le profil ne fixe ni le modèle ni le harnais** : ce sont des choix
  d'agent/placement, pas de contexte.

### 6.6 Publication et télémétrie

Ce qui est publié, sans contenu :

- `turn.context.task` : `kind`, `version`, `source`, profil résolu, profil
  demandé, différences (`requested` vs `applied`), raisons ;
- `turn.context.policy` : les valeurs effectives (`turn_reread_budget`,
  `session_reread_budget`, `compact_tokens`, `context_max_tokens`,
  `session_max_tokens`, `steps_budget`) avec leur **source** (agent, rôle,
  tâche, hôte, défaut) — le mécanisme de source de #6046 est réutilisé ;
- `turn.context.counts` : `calls`, `steps`, `tool_calls`,
  `tool_output_chars`, `tool_output_max_chars`, `assistant_chars` ;
- `turn.context.decision` : motif de clôture (`step_budget`, `reread_budget`,
  `duration`, `rotation`, `compaction`) et continuation ;
- `ameesh-turn-context/1` : une section `task` additive (facultative) avec le
  même profil, pour que le harnais et l'intégrateur lisent le contrat du tour
  sans ouvrir la base.

Le document `ameesh-turn-context/1` est régi par ses règles de version
(ajout de champs optionnels seulement dans `/1`, §6.7 de l'étude #2894) ; la
section `task` est donc **additive**, jamais un remplacement.

### 6.7 Cycle de version

- Le **schéma** est `ameesh-task-context/1` : tout ajout est optionnel, aucun
  champ n'est retiré, aucun sens n'est changé. Un changement incompatible est
  un `/2`, refusé par les lecteurs `/1`.
- Le **profil** porte sa propre `version` et sa `source` ; deux profils de
  même `kind` et de versions différentes sont deux objets distincts, jamais
  une mise à jour silencieuse.
- **Aucune valeur par défaut n'est inventée** : un champ absent est absent,
  et le comportement est le legacy. Un profil partiel est valide ; il
  n'applique que ce qu'il déclare.
- **Les valeurs sont datées** : un profil sans `source.at` est refusé (sa
  raison est publiée), pour que le rejeu sache ce qui a été utilisé.
- **Le profil est réversible** : le retirer (ou vider `task`) ramène le
  legacy au tour suivant ; aucun état n'est collé à l'agent.

### 6.8 Modes d'échec

| cas | comportement attendu |
|---|---|
| `task` absent | legacy, aucune erreur |
| `kind` absent ou vide | profil refusé, legacy, raison `task_kind_missing` |
| `kind` inconnu d'ameesh | accepté (label opaque), publié ; ameesh n'a pas à le connaître |
| champ inconnu | profil refusé (`additionalProperties: false`), legacy, raison `task_profile_unknown_field` |
| valeur hors bornes | profil refusé, legacy, raison `task_profile_out_of_bounds`, incident publié |
| `source` absente | profil refusé, legacy, raison `task_profile_unsourced` |
| `session.policy` inconnue | profil refusé, legacy, raison `task_profile_bad_session` |
| budget de relecture atteint | clôture au point sûr, continuation, pas d'échec |
| budget d'étapes atteint | idem, avec `decision.close.cause = "step_budget"` |
| plafond d'outil atteint | l'intégrateur tronque et écrit le fichier ; ameesh le mesure |
| harnais sans l'option `compact_tokens` | la valeur n'est pas passée, elle est publiée `not_supported` |

### 6.9 Vie privée et sûreté

- Le profil ne contient **aucun secret, aucun contenu de tâche, aucune URL
  privée** : seulement des classes, des nombres et une provenance.
- Il n'est **pas** une autorisation : le bail, le budget de compte, les
  gardes de sécurité et les permissions d'outil restent la seule autorité.
- Les valeurs proposées ici pour IMA sont des **exemples** dans une étude,
  pas des valeurs du cœur ; le lot d'implémentation doit refuser tout nom de
  client en dur (garde de mécanique, §7 du canon).
- La rétention de contenu (§7) est **opt-in**, caviardée et à durée bornée ;
  elle suit le chemin sensible.

---

## 7. Mesure continue et rétention opt-in

### 7.1 Ce qu'on mesure en continu, sans contenu

Le premier lot d'implémentation ajoute au `turn.context` les compteurs de
§4.5. Ils sont **toujours actifs**, sans rétention de contenu, et coûtent
quelques dizaines de lignes dans l'analyse du flux :

- `counts.tool_calls`, `counts.tool_output_chars`,
  `counts.tool_output_max_chars`, `counts.tool_output_chars_by_tool` ;
- `counts.assistant_chars` (sortie du modèle en caractères, en plus de `O` en jetons) ;
- `counts.steps` (déjà connu pour Claude/dsh ; à compter pour Codex par les
  `item.completed`) ;
- `counts.context_first_chars` (consigne + messages remis), quand le runner
  compose le prompt.

Avec ces compteurs, `ameesh cost turns --over` et un futur
`ameesh task context report` peuvent répondre à « combien de tours, de pas,
de caractères d'outil, pour quel type de tâche ? » sans jamais lire un
prompt. C'est la mesure qui pilote le contrat.

### 7.2 Rétention opt-in caviardée

Pour la décomposition fine (sections du prompt, citations, ablation), il faut
le contenu. La rétention proposée :

- **Opt-in** par agent et par hôte, désactivée par défaut ; jamais activée
  par un lot de code sans décision explicite.
- **Échantillonnage déterministe** : `hash(turn_id) % N == 0`, par exemple
  1 tour sur 20, plafonné à 50 tours par jour et par hôte ; le taux et la
  graine sont publiés.
- **Caviardage** avant écriture : secrets (valeurs d'environnement, jetons,
  mots de passe, clés), URLs avec identifiants, adresses de courrier,
  chemins privés ; remplacement par `<redacted:type>` et un hachage court
  pour la corrélation. Le caviardage est versionné et testé.
- **Stockage** : hors dépôt Git, sous `<state>/context-samples/`, mode `0600`
  dans un dossier `0700`, un fichier par tour, schéma
  `ameesh-context-sample/1` (métadonnées + sections, contenu optionnel).
- **Durée** : 7 jours par défaut, effacement automatique ; commande
  d'effacement manuel ; aucune copie dans un ticket, un courrier ou un
  commentaire.
- **Accès** : propriétaire de l'hôte et auditeur ; journalisé ; aucune
  lecture par un autre agent.
- **Contenu par section** : le format minimal ne conserve que les tailles et
  les types ; le contenu n'est conservé que si l'opt-in le demande
  explicitement, et jamais pour une source de staging sans accord.

### 7.3 Mesurer « ce qui est utilisé »

Sur les tours échantillonnés, deux mesures :

1. **citation** : chaque bloc reçoit un identifiant ; on cherche ses
   fragments (chemins, noms de tests, extraits de log) dans les sorties
   ultérieures et les arguments d'outil ; on publie le taux de citation par
   classe, jamais le contenu ;
2. **ablation** : sur un petit échantillon (20 vérifications, 20 conflits),
   on rejoue la même tâche sans une classe (fil complet, skills, repo, dump
   d'outil) et on compare succès, jetons et durée. C'est la seule preuve
   causale ; elle est chère et se fait hors production.

### 7.4 Ce qu'on ne fait pas

- Pas de rétention par défaut, pas de rétention « pour la sécurité » sans
  décision, pas de contenu de staging dans le dépôt.
- Pas de profil par client dans le code ; pas de valeur métier dans le
  schéma.
- Pas de mesure qui ralentit le tour : les compteurs sont calculés sur le
  flux déjà lu, sans appel supplémentaire.

---

## 8. Pilotes et découpage

### 8.1 Séquence recommandée

| # | lot | dépend de | classe probable | livrable |
|---|---|---|---|---|
| A | compteurs de sections et d'outils dans `turn.context` | — | légère (compteurs, aucun contenu) | mesure continue |
| B | schéma `ameesh-task-context/1`, validation, publication | A | légère | contrat exposé |
| C | application par l'exécuteur : étapes, budgets de tâche, sessions, préambule | B | sensible (runner, budgets) | contrat appliqué |
| D | pilote `verifier/bug_staging` (IMA) | C | sensible (staging réel) | avant/après |
| E | pilote `fixer/qa_pr_merge_conflict` (IMA) | C, D | sensible | avant/après |
| F | pilote `builder/implementation` | D, E | sensible | avant/après |
| G | rétention opt-in caviardée | B | **sensible** (vie privée) | mesure de contenu |

A et B peuvent se faire en parallèle ; C attend B ; D attend C. G est
indépendant de C et peut être découpé plus tard, une fois le besoin de
contenu confirmé. Le découpage exact et l'ordre sont à mesh-design.

### 8.2 Lot A — compteurs

- Ajouter les compteurs de §7.1 au normalisateur et à `turn.context`.
- Compter les sorties d'outil dans les flux Claude (tool_result), Codex
  (`item.completed` avec sortie) et dsh (étape) sans conserver le contenu.
- Étendre `ameesh cost turns --over` et le corpus avec les nouveaux champs,
  en gardant la liste blanche.
- Tests ciblés : un flux par harnais, un dump de 200 k caractères, un tour
  sans sortie d'outil, un flux interrompu.
- **Aucune valeur activée, aucun contenu conservé.**

### 8.3 Lot B — contrat

- Module `task_context.py` : schéma, validation stricte, bornes, refus,
  legacy.
- Lecture du `payload.task` dans le runner, publication dans
  `turn.context.task` et dans `ameesh-turn-context/1` (section additive).
- Commande `ameesh task context check FILE` et `ameesh task context show
  --turn <id>` (lecture seule).
- Tests : profil valide, absent, hors bornes, sans source, type inconnu,
  profil partiel, rétrocompatibilité legacy.

### 8.4 Lot C — application

- Budget d'étapes : compteur de pas, clôture au point sûr, continuation,
  `decision.close.cause = "step_budget"`.
- Budgets de tâche : `turn_reread_budget` et `session_reread_budget`
  prioritaires sur l'agent et le rôle, avec source publiée.
- Session `par-tache` : le runner ouvre/ferme la session selon la politique
  (les valeurs `par-lot`, `taille` et `jamais` restent inchangées).
- Préambule : appliquer `preamble` quand le descripteur le permet
  (skills/multi-agent/repo), publier sinon ; `compact_tokens` suit le chemin
  #6040 déjà en place.
- Tests : clôture à 15 pas avec continuation, sans perte de travail ; budget
  de tour ; session ; préambule non supporté.

### 8.5 Lots D–F — pilotes

Chaque pilote :

1. **pré-enregistre** la fenêtre de référence (mêmes types de tâche, même
   mélange de difficulté, même version de harnais), la métrique principale
   (jetons par tâche réussie), les métriques secondaires (taux de succès,
   durée, coût) et la taille de l'échantillon ;
2. **applique** le profil à un sous-ensemble tiré au sort, l'autre moitié
   restant en legacy (ou une fenêtre avant/après, si le tirage n'est pas
   possible) ;
3. **mesure** avec les compteurs de A et les reçus d'IMA ;
4. **décide** : garder, corriger ou revenir en arrière (#5250 fournit le
   mécanisme réversible) ;
5. **publie** un commentaire sur #5549 et un bilan dans le lot.

Critères de succès proposés :

| type | taille | métrique principale | garde-fou |
|---|---:|---|---|
| `verifier/bug_staging` | ≥ 30 vérifications | jetons par vérification réussie ≤ 40 % de la référence | taux de succès ≥ référence − 2 points ; durée ≤ référence |
| `fixer/qa_pr_merge_conflict` | ≥ 20 conflits | jetons par conflit résolu ≤ 50 % | taux de résolution ≥ référence ; tests verts |
| `builder/implementation` | ≥ 15 lots | jetons par lot livré ≤ 50 % | pas de régression de CI ni de délai |

Les échecs comptent : une vérification qui échoue ou est bloquée reste dans
le dénominateur (intention de traiter), sinon le gain est illusoire.

### 8.6 Garde-fous communs

- **Aucune production** dans ce lot ; les pilotes D–F sont des lots séparés,
  avec leurs propres autorisations.
- **Aucune baisse de succès** n'est acceptable pour un gain de jetons.
- **Le même mélange de tâches** avant/après ; pas de tri des cas faciles.
- **Le retour arrière** est prévu dès le départ (#5250) et testé.
- **Les valeurs IMA** restent dans IMA ; le cœur d'ameesh ne les connaît pas.
- **La rétention** (G) est opt-in, caviardée, sensible, et n'est pas un
  prérequis des pilotes D–F.

---

## 9. Risques, questions ouvertes

### 9.1 Risques

| id | risque | mitigation |
|---|---|---|
| R1 | le budget d'étapes coupe une vérification au milieu d'une preuve | clôture au point sûr + continuation + phases ; le budget est un plafond, pas un verdict |
| R2 | le plafond d'outil cache la ligne qui prouve le bug | débordement fichier + grep + tête/queue explicitement marqués ; le plafond par tour peut être relevé pour un cas |
| R3 | la distillation retire une information nécessaire | fourni d'emblée + à la demande ; ablation sur un échantillon avant généralisation |
| R4 | la compaction casse le cache et coûte plus qu'elle ne rapporte | préférer le sous-tour ; `compact_tokens` seulement si le harnais le supporte et si le contexte dépasse un seuil |
| R5 | un profil par tâche devient un nouveau mur | budgets souples, clôture + continuation, retour arrière ; jamais un échec sec |
| R6 | la rétention de contenu expose des données de staging | opt-in, caviardage, 0600, 7 jours, accès auditeur, chemin sensible ; les compteurs seuls suffisent pour la première boucle |
| R7 | le gain est confondu avec un changement de mélange de tâches | pré-enregistrement, tirage aléatoire ou fenêtre avant/après, échecs inclus |
| R8 | le profil est copié dans le cœur avec des valeurs client | schéma générique, garde de mécanique, revue §7 du canon |

### 9.2 Questions ouvertes

1. **Unité du budget d'étapes** : appels au modèle ou actions d'outil ? Le
   contrat propose les appels (c'est ce qui coûte), mais le runner doit les
   compter pour Codex, qui ne publie pas d'usage par appel ; les
   `item.completed` donnent une approximation. À trancher au lot C.
2. **Qui possède les profils** : IMA les écrit dans son dépôt et les envoie
   dans le payload, ou ameesh les lit dans un fichier de canon ? Le contrat
   propose le payload (propriétaire = intégrateur), avec un registre
   optionnel plus tard.
3. **Où vit le fichier de débordement** : `$TMPDIR` de l'agent (déjà la règle
   L162) ou un dossier de tour, nettoyé à la fin ? Le premier est déjà en
   place ; le second est plus sûr pour la reprise.
4. **Combien de temps garder les échantillons** : 7 jours est un défaut ; la
   durée exacte est une décision de vie privée.
5. **Faut-il relever #1793 à 5 M ?** Recommandation : oui comme mesure
   d'attente explicite, non comme solution ; décision du propriétaire.
6. **Le builder doit-il avoir un profil distinct par langage/écosystème ?**
   Probablement pas au niveau du contrat ; les tests et les outils changent,
   pas la structure du contexte.
7. **Comment mesurer le « raisonnement reporté »** : `O` est un cumul, pas
   une ventilation ; seule une rétention de contenu ou une ablation peut
   séparer le raisonnement utile du raisonnement répété.

---

## 10. Annexes

### A. Commandes de reproduction

```bash
# 1. Corpus de contexte (écrit hors dépôt, 0600, liste blanche)
ameesh context corpus export --out "$TMPDIR/corpus7d.jsonl" --days 7

# 2. Grand livre des tours
ameesh cost turns --since 7d --limit 30000 --json > "$TMPDIR/costs7d.json"

# 3. Tours au-dessus du seuil IMA, avec politique et motif
ameesh cost turns --since 7d --over 2500000 --limit 3000 --json \
  > "$TMPDIR/over25.json"

# 4. Vérification du corpus
ameesh context corpus check "$TMPDIR/corpus7d.jsonl"
```

Le script d'agrégation de cette étude est volontairement court et sans
dépendance :

```python
import json
rows = [json.loads(line) for line in open("corpus7d.jsonl")]
costs = {c["turn_id"]: c for c in json.load(open("costs7d.json"))["turns"]}

def value(row, key):
    if key in ("C_first", "dC_max"):
        return (row.get("series") or {}).get(key, {}).get("value")
    if key == "steps":
        return (row.get("series") or {}).get("steps")
    raw = row.get(key)
    return raw.get("value") if isinstance(raw, dict) else raw

def pct(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = (len(values) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)

for role in sorted({r["role"] for r in rows}):
    for harness in sorted({r["harness"] for r in rows if r["role"] == role}):
        g = [r for r in rows if r["role"] == role and r["harness"] == harness]
        rt = [value(r, "R_turn") for r in g]
        steps = [value(r, "steps") for r in g]
        print(role, harness, len(g), pct(steps, 50), pct(rt, 50), pct(rt, 90))
```

### B. Tables de référence

**Tours les plus gros de la fenêtre** (`R_turn` connu) :

| `R_turn` | rôle | harnais | agent | pas | `C_last` | lot |
|---:|---|---|---|---:|---:|---:|
| 106 522 948 | build | deepseek | deepseek4 | 367 | 409 751 | 6708 |
| 94 191 737 | docs | deepseek | mty-nxl-docs-01 | 363 | 400 579 | 6512 |
| 72 752 893 | build | deepseek | mty-nxl-impl-04 | 330 | 357 386 | 6748 |
| 69 794 197 | build | deepseek | deepseek7 | 272 | 397 158 | 6701 |
| 61 445 195 | build | deepseek | deepseek2 | — | 386 544 | 6368 |

**Sessions les plus grosses** (entrée cumulée) : une session de 4 tours de
`docs`/DeepSeek à 140 M, une de 4 tours de `build`/DeepSeek à 130 M, une de
15 tours de `build`/Claude à 120 M, une session `pilot`/Claude de 93 tours à
90 M.

### C. Glossaire

- **appel** : une requête au modèle, identifiée par un `call_id` quand le
  harnais en publie un.
- **pas** : un appel dans un tour ; `steps` dans `turn.context`.
- **contexte** : ce qui est renvoyé au modèle à un appel ; mesuré par
  `C_last` (dernier appel) et `C_first` (premier appel).
- **relecture** : jetons d'entrée relus par un appel ou un tour, cache lu
  compris ; mesurée par `R_turn`, `R_session`.
- **cache lu** : jetons d'entrée servis par le cache du fournisseur ; ils
  restent des jetons relus.
- **S_hist** : garde historique de taille de session (`session_max_tokens`),
  distincte du contexte d'un appel.
- **préambule** : ce qui est présent avant la première consigne : système,
  rôle, outils, skills, instructions de dépôt.
- **type de tâche** : label opaque déclaré par l'intégrateur, par exemple
  `verifier/bug_staging`.
- **profil** : objet `ameesh-task-context/1` qui porte les valeurs du type de
  tâche.
- **sous-tour** : un nouveau tour, éventuellement une nouvelle session, entre
  deux phases indépendantes ; il ne transporte que la note de passage.
- **débordement** : écriture de la sortie complète dans un fichier et
  troncature de ce qui reste dans le contexte.

### D. Carte du code et des documents

| sujet | fichier |
|---|---|
| compteurs et contrat | `src/ameesh/context_metrics.py`, `docs/design/ctx-01-compteurs-contexte.md` |
| corpus et rejeu | `src/ameesh/context_corpus.py`, `src/ameesh/context_replay.py` |
| profil de tour | `src/ameesh/context_profile.py` |
| budgets et politiques | `src/ameesh/exploitation.py`, `src/ameesh/mechanics.py`, `docs/EXPLOITATION.md` |
| runner, clôture, sessions | `src/ameesh/runner.py` |
| flux des harnais | `src/ameesh/adapters.py` |
| document de tour | `src/ameesh/turn_context.py`, `docs/design/etudes/l2894-turn-context-api.md` |
| décision de session | `docs/design/decisions/0025-une-session-par-lot.md` |

---

## 11. Journal des versions

| version | date | changement |
|---|---|---|
| r1 | 2026-11-14 | création : mesure 7 jours, profil par type de tâche, contrat `ameesh-task-context/1`, plan de mesure et de rétention, découpage des pilotes |
| r2 | 2026-11-14 | confrontation à l'étude #6427 (tours longs, croissance par pas constante), plafond par harnais #6677 et série par pas #6433 ; aucune valeur ni conclusion de mesure modifiée |
