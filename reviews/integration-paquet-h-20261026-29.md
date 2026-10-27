# Paquet integration/paquet-h-20261026-29 — composition (§12), sur main issu de h-28

Base : `main` **`38736fdad914fd5cb7b7dbbdad153776b7660263`** (fusion du paquet
h-28, PR #2865, 26/10 13:17Z). Branche `integration/paquet-h-20261026-29`,
cible `main`. Porteur : **#3748**. Composeur : mty-amh-impl-07 (deepseek,
agents-2).

Déclencheur : fusion de h-28 (`main` 38736fdad, 13:17Z) et consigne mesh-design
du 26/10 12:38Z (lot #3748 : candidats et motifs des écartés), rappel du 13:20Z
et candidats de plus du 13:22Z. Liste calculée `paquet-candidats` sur
`38736fdad` à 13:25Z : **8 prêts**, 8 en attente, 2 erreurs de provenance
(chaîne handoff, hors paquet par consigne pilote). Sept prêts sont composés :
#3745 (prioritaire, il débloque le staging de pc-smichea), #3687, #3733,
#3741, #3746, #3729 et #3744. **#3742 est retiré sur échec de la première
ci-full** (voir « Retrait de #3742 »). Le calcul sort en code 1 à cause des deux
erreurs connues de #3506/#3509 : la liste des prêts reste utilisable par
consigne pilote (même traitement en h-22 à h-28), et les 8 ids prêts ont été
recopiés de la sortie calculée pour le contrôle d'inclusion.

## Motif et correction de la base

Correction du pilote (mesh-design, 26/10 13:50Z) : **h-28 a été fusionné en
admin** par le pilote, avec un agrégat de tests rouge **non concluant** (part
`psql 12/16` coupée à 60 min, 0 test en échec) ; il n'est pas passé par la porte
avec des tests verts. Sa seule adaptation, le renommage mécanique
`_removed_seats` → `_removed_seats_from_mark` de #3685 contre le helper de
#3584, a été couverte par la dispense « verdict non requis » du pilote sur
`15c7296`. La correction est notée au lot #3748 ; si une part psql de h-29 est
coupée à 60 min sans test en échec, elle sera signalée au pilote, pas relancée.

## Admissions (7 lots, 7 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3745 | `5114442723cf` | 1/1 (docs-01 deepseek) | pgbouncer : jit suivi par le pool, doctor nomme un réglage de session refusé (débloque le staging de pc-smichea) |
| #3687 | `5d2fb2986481` | 2/2 (cx3 codex, dev3 deepseek) | gel : lancer les tests de contrat de la porte de fusion quand le diff touche la porte |
| #3733 | `ef9940e129a9` | 1/1 (dev3 deepseek) | runner : un tour codex refusé (429) finit à 0 jeton sans erreur ni alerte — débit chronique et alerte |
| #3741 | `1efec16cead0` | 2/2 (dev3 deepseek, cx2 codex) | base commune : délais de requête en rafale, lectures à la source, migration additive 0489 (index du courrier passif) |
| #3746 | `ddfd8426e2e6` | 1/1 (dev3 deepseek) | deps : une entrée `node_modules` sans lien est purgée après 24 h au lieu de 3 jours |
| #3729 | `4d6b0b59660d` | 2/2 (cx2 codex, dev2 claude) | show/list : la dernière écriture d'une session externe est son activité, l'hôte réel du dernier dépôt est nommé |
| #3744 | `0dd5942e8e0a` | 1/1 (dev3 deepseek) | `work add --body -` lit l'entrée standard au lieu d'enregistrer « - » |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3745 | 20274 | #20279 docs-01 (deepseek) ok |
| #3687 | 19945 | #19954 cx3 (codex) ok, #20018 dev3 (deepseek) ok |
| #3733 | 20225 | #20241 dev3 (deepseek) ok |
| #3741 | 20270 | #20277 dev3 (deepseek) ok, #20282 cx2 (codex) ok |
| #3746 | 20281 | #20285 dev3 (deepseek) ok |
| #3729 | 20237 | #20239 cx2 (codex) ok, #20243 dev2 (claude) ok |
| #3744 | 20244 | #20253 dev3 (deepseek) ok |

## Composition (7 unités, 7 lots)

| unité | lot | source | commit |
|---|---|---|---|
| 1 | #3745 | `5114442723cf` | `b3fee000978c` |
| 2 | #3687 | `5d2fb2986481` | `d82a947f9299` |
| 3 | #3733 | `ef9940e129a9` | `1a2672188e43` |
| 4 | #3741 | `1efec16cead0` | `22064f42a53f` |
| 5 | #3746 | `ddfd8426e2e6` | `cda20233244a` |
| 6 | #3729 | `4d6b0b59660d` | `e5892e081e78` |
| 7 | #3744 | `0dd5942e8e0a` | `b206b38028d2` |

Les sept fusions sont propres : aucun conflit textuel, aucune résolution
d'union, aucune adaptation. Le contrôle des définitions dupliquées sur
`src/ameesh` ne trouve aucun doublon introduit par le paquet (seul le doublon
préexistant de `reprise.recently_modified`, hors paquet). Après retrait de
#3742, le diff du paquet est de 34 fichiers (+1961/−57), manifeste compris.

## Retrait de #3742

La première ci-full sur la tête `5fbc0864` (run 40334122332) rend un rouge
négatif : `test_2864_proposition_familles` (5 témoins) échoue parce que le
nouveau `freeze_families.enforce` refuse leurs gels — 2 avis requis, 1 seule
famille éligible — et `test_2914_train_canon_drift` (3 témoins) casse sur
`_policy` qui lit `loaded.exclude_author_family` absent du mock
(`AttributeError`). Les deux régressions sont reproduites localement sur
l'arbre du paquet, puis disparaissent après retrait.

Retrait : **recomposition sans #3742** sur `38736fdad` — les sept autres
fusions sont reprises telles quelles ; le merge #3742 (`c3afb8ba1bdc`) et le
revert intermédiaire du premier passage (`2f3ce4e24`) ne sont pas repris.
Après retrait, les deux modules entiers sont rejoués verts (30 tests × 2
pilotes), dont les trois témoins en échec. Le lot #3742 retourne à son auteur
(mty-amh-impl-08) : les fixtures `test_2864` (auteurs multi-familles) et le mock
`test_2914` doivent être adaptés à la règle du §2, ce n'est pas une adaptation
mécanique de composition. Rien d'autre n'est relu ni modifié.

## Écartés de h-29

| lot | motif |
|---|---|
| #3742 | retiré du paquet sur échec CI (run 40334122332) : régressions `test_2864` (freeze refusé par `freeze_families`) et `test_2914` (`AttributeError` sur le mock de politique) ; recomposition sans #3742, retour à l'auteur pour adaptation |
| #3519 | hold pilote (mesh-design, 26/10 12:32Z) : chaîne handoff, empilé sur H8 #3509 (dépendance déclarée) ; rebase sur main, regel puis désignation après #3509 (`freeze-after-verdict`) |
| #3526 | aucun avis sur le gel `30a916ad` (sensible) et conflit avec main (`docs/ops/handoff-staging-enrollment.md`, `pyproject.toml`) |
| #3656 | aucun avis sur le gel `e0fc8047` (1 requis, léger) |
| #3735 | verdict bloquant (B/P1 cx2) sur la tête publiée `b32b7e39` ; nouveaux avis requis après correction |
| #3747 | verdict bloquant sur son gel ; toujours en construction (build), pas prêt |
| #3693 | déjà dans la cible : tête `0b10ea4f` ancêtre de `main` (h-28) ; `sync-merges` doit clore |
| #3728 | déjà dans la cible : tête `b2bad784` ancêtre de `main` (h-28) ; `sync-merges` doit clore |
| #3737 | véhicule de paquet (h-28), jamais une unité |
| #3506 | erreur de lecture de la porte (provenance d'auteur inconnue sur les commits gelés) ; hors paquet par consigne pilote (chaîne handoff) |
| #3509 | erreur de lecture de la porte (provenance d'auteur inconnue sur les commits gelés) ; hors paquet par consigne pilote (chaîne handoff) |

Les erreurs de provenance de la chaîne handoff (#3506, #3509) restent hors
paquet par consigne pilote (même motif en h-22 à h-28) : elles ne rendent pas la
liste des lots prêts incomplète pour ce paquet. Le calcul de 13:25Z rend 8
prêts : 7 composés, #3742 retiré avec motif, et les 8 en attente et les 2
erreurs sont écartés ci-dessus.

## Contrôles

- Liste calculée `paquet-candidats` sur `38736fdad` à 13:25Z : 8 lots prêts,
  8 en attente, 2 erreurs de provenance ; code 1 dû aux deux erreurs connues de
  la chaîne handoff. Sept prêts composés ; #3742 retiré sur échec CI ; les 8 en
  attente et les 2 erreurs sont écartés avec motif ci-dessus.
- Composition : 7 unités sur `38736fdad914` ; 7 fusions propres, aucun conflit
  textuel, aucune adaptation. Aucune collision sémantique de définition
  détectée (`compileall` vert, contrôle des doublons sur `src/ameesh`).
- Tests ciblés avant la première ci-full : témoins des 8 lots et modules
  voisins touchant les mêmes fichiers (désignation, gel, porte, runner, ménage,
  dépendances, show/work) ; pilote psql : 253 tests, 0 échec (17 sautés
  « requires psycopg »), 1072 s. Après retrait de #3742 : `test_2864` et
  `test_2914` entiers rejoués verts (30 tests × 2 pilotes), dont les trois
  témoins qui avaient rougi.
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur la tête du manifeste :
  vert — 7 compositions, 7 contenus présents à la tête, #3742
  écarté, aucun défaut détecté.
- Une seule suite complète sur la nouvelle tête gelée (`ameesh ci run 3748
  --full --avis 0`, marque « verdict non requis » du pilote), pas de CI par lot
  (§11 bis). Consigne h-28 : une part coupée sans test en échec se signale au
  pilote, elle ne se relance pas.
- Migration additive **0489** (`courrier_passif_index`, #3741) : aucune
  migration destructive ; à appliquer en staging avant la relance des
  exécuteurs (§14).
