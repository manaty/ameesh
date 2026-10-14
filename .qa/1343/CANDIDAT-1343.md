# Candidat #1343 — la CI d'un lot part au gel (avis delta R1 cx2, binôme borné)

- **lot** : #1343, évolution, classe sensible sécurité/porte CI (canon §2/§8).
- **auteur** : mty-amh-impl-04 (DeepSeek, exécutant sur ameesh-agents-1).
- **mandat** : mesh-design 14/10 19:52 (mail) ; canon §3 (home 66, owner 14/10
  21:45) ; direction pilote P1-2 (mesh-design 14/10 21:43) ; précision pilote
  #42336 (reprise cx2 22:06) ; avis delta R1 cx2 du 14/10 22:24 (mail #42838).
- **base** : `main 2458ec053123d3f33644fef1e092fc84cc2773d9` (#1339 fusionné).
- **candidat** : `d1d875c89` (5 commits) — à examiner par le binôme borné.
  - ref atelier (à publier) : `refs/atelier/1343/candidat-d1d875c89`
  - pièces (à publier) : `refs/atelier/1343/pieces-d1d875c89`
  - branche locale : `lot/1343-ci-au-gel` (**non poussée pour ce candidat**),
    worktree `/home/ameesh/work/mty-amh-impl-04-1343`
- **tête déjà examinée, NO-GO delta R1 cx2** :
  `3ce572f7b6e5d8b83e6964243371b6fc95168727`
  - ref atelier déjà publiée : `refs/atelier/1343/candidat-3ce572f7b`
  - delta depuis ce point : `delta-apres-3ce572f7b.patch` (3 fichiers,
    +285/−20, 1 commit).
- **tête refusée par la R1 initiale** :
  `b60889b6151c3c01ebdf7ba79177197eb610aec2` (ref atelier publiée).

## Delta depuis le NO-GO delta R1 (ce que le binôme borne)

| constat cx2 sur 3ce572f7b | correctif |
|---|---|
| **P1 — relire le gel et les verdicts après acquisition du verrou** (`_request_once`) | `_revalidate(tx, …)` refait les gardes de `gel_plan` dans la transaction qui tient la ligne du lot, juste avant la pose : le jalon `frozen` est relu (un gel déplacé fait reculer), puis le dernier verdict de chaque acteur (un `blocked` commité pendant l'attente fait reculer). Les exceptions explicites (`--exception`, `--full`), gestes marqués et motivés, gardent leur cours. Les jalons s'écrivent sous le même verrou de ligne (`add_milestone` : `FOR NO KEY UPDATE`), donc la relecture est autoritaire. |
| **P2 — la reprise forcée du check rouge est neutralisée** | la phase critique réapplique la décision check/mode/`--force` du plan : un vert applicable + dernier check `tests` rouge + `--force` descend jusqu'à la pose ; sans `--force` la commande refuse ; un check en vol attend ; un check vert ne repose pas. |
| **P2 — une réservation déjà matérialisée bloque la reprise du rouge** | `_request_once` passe `after` = instant du plus récent run applicable visible : la marque `applied` antérieure à ce run est matérialisée par lui et ne tient plus la clé ; une marque postérieure au run (autre hôte, demande encore invisible) tient toujours. |
| **P2 — la réservation full ne couvre pas une demande lot** | `reservation_holder` compare les modes par la couverture des runs (`_covers`) : une marque `full` fraîche couvre une demande `lot` de la même composition, jamais l'inverse ; deux poses séquentielles `full` puis `lot` sont écartées. |
| **observation — chemin médié** | la garantie de sérialisation est bornée par écrit aux connexions directes (transaction + verrou de ligne) ; sur une connexion d'exécuteur médiée (`storage.is_remote`, L109), la phase critique reste sans transaction et la marque est une réservation de meilleur effort — aucune garantie inter-hôtes n'est annoncée. Docstring de `_request_once`, commentaire du chemin et section L161f (`docs/EXPLOITATION.md`) alignés ; un témoin vérifie qu'aucune transaction n'est prise sur ce chemin. |

## Porte — ce que le candidat garantit

1. **Une poussée de lot ne lance plus rien** : `tests.yml` ne se déclenche plus
   sur une PR ordinaire ; le parcours PR part de l'étiquette de gel. Les
   poussées `main`/`release/**`/`integration/**` gardent la suite complète.
2. **Demande unique sur la tête gelée** : jalon `frozen` obligatoire (un
   `--sha` seul ne le remplace pas), tête de branche == SHA gelé, PR ouverte
   dont la tête est le gel, `--avis N` verdicts `ok` sur ce SHA exact par des
   acteurs distincts hors assigné (`0` léger, `1` sensible, `2` SQL/sécurité/
   prod), workflow du SHA portant le déclencheur — et ces gardes (gel,
   verdicts, PR, composition) sont **relues sous le verrou**, juste avant la
   pose.
3. **Idempotence** : vert applicable (même composition, mode couvrant, check
   requis vert) ou run vivant → rien n'est posé ; rouge/annulé → `--force`
   explicite, qui répare aussi un check requis rouge sous un vert applicable ;
   une réservation fraîche d'un autre processus → rien n'est posé, dans la
   couverture des modes.
4. **Annulation limitée au gel** : `ci run`/`ci cancel` n'annulent que les runs
   ouverts qui ne portent pas la composition gelée (`--all` pour l'inclure) ;
   jamais le run de la composition courante.
5. **Aucun vert sans tests** : au gel, les voies version seule (L157) et
   documentation seule (L161a) sont éteintes (mode `lot` et `full`) ; l'agrégat
   requis refuse `version_only`/`docs_only`.
6. **Exceptions marquées** : `--exception incident|unblock --reason …`.
7. **`--full --reason …`** : suite complète ; imposée par les refs release.
8. **Erreur visible** : refus actionnable dit à l'appelant et noté au lot.

## Preuves ciblées (auteur, sur ce SHA — aucun push de branche, aucune CI)

- `tests.test_l161g_ci_gel` **82/82** (71 + 11 témoins du delta) ;
  `tests.test_l161f_plan` 121, `tests.test_l744_ci_entrypoint` 2,
  `tests.test_l161a_ci_sans_code` 6, `tests.test_hermeticity_lint` 17 →
  **146/146** ; `tests.test_l161b_flow` + `tests.test_l161c_failures_realtime`
  **34/34** (`preuves/tests.txt`).
- **4 mutations**, une par constat du delta (relecture sous verrou,
  check/mode/`--force`, run visible matérialisé, couverture des modes), font
  rougir leurs témoins respectifs, puis restauration vérifiée 82/82
  (`preuves/mutations.txt`).
- Lint d'herméticité : 0 constat sur 3 fichiers (`preuves/lint.txt`) ;
  `py_compile` OK (`preuves/py_compile.txt`).
- `tests.test_l161c_failures_realtime` (comportement existant de `ci.py`) :
  26/26 (`preuves/l161c.txt`).
- Aucun push de branche CI, aucun run, aucune CI : seules les refs
  `refs/atelier/1343/*` sont publiées.

## Points ouverts pour l'avis borné

- Le binôme porte sur le delta `3ce572f7b..d1d875c89` (les quatre constats et
  l'observation) ; le reste de la tête a déjà été examiné.
- Le chemin médié reste sans réservation atomique : la garantie est bornée, un
  chemin atomique serait un lot de suite si le pilote le juge nécessaire.
- Après avis delta GO et R2 : le pilote vérifie les déclencheurs sur ce SHA
  exact avant poussée de branche + PR ; la CI du lot se demande au gel par
  `bin/ameesh ci run 1343 --avis 2`, puis la preuve est le check requis `tests`
  de la tête dans `gh pr checks`.
