# Candidat #1343 — la CI d'un lot part au gel (relecture R1 delta/R2)

- **lot** : #1343, évolution, classe sensible sécurité/porte CI (canon §2/§8).
- **auteur** : mty-amh-impl-04 (DeepSeek, exécutant sur ameesh-agents-1).
- **mandat** : mesh-design 14/10 19:52 (mail) ; canon §3 (home 66, owner 14/10
  21:45) ; direction pilote P1-2 (mesh-design 14/10 21:43) et précision pilote
  #42336 (reprise cx2 22:06).
- **base** : `main 2458ec053123d3f33644fef1e092fc84cc2773d9` (#1339 fusionné).
- **candidat final** : `3ce572f7b6e5d8b83e6964243371b6fc95168727` (4 commits)
  - ref atelier (à publier) : `refs/atelier/1343/candidat-3ce572f7b`
  - pièces (à publier) : `refs/atelier/1343/pieces-3ce572f7b`
  - branche locale : `lot/1343-ci-au-gel` (**non poussée pour ce candidat**),
    worktree `/home/ameesh/work/mty-amh-impl-04-1343`
- **tête refusée par R1 cx2 (NO-GO 3 P1, 2 P2)** :
  `b60889b6151c3c01ebdf7ba79177197eb610aec2`
  - ref atelier déjà publiée : `refs/atelier/1343/candidat-b60889b61`
  - delta depuis ce point : `delta-apres-b60889b61.patch` (8 fichiers,
    +1618/−525, 2 commits).

## Delta depuis le NO-GO R1 (ce que la relecture delta doit couvrir)

| point R1 | correctif |
|---|---|
| **P1-1** dernier verdict par acteur | `latest_verdicts` ne garde que le dernier verdict de chaque acteur (hors assigné) : un `blocked` récent annule un `ok` ancien du même relecteur ; un `blocked` d'un autre relecteur bloque la tête. |
| **P1-2** un `workflow_dispatch` ne satisfait pas un check requis de PR | le déclencheur devient l'étiquette de PR `ci-lot-<40hex>` / `ci-full-<40hex>` (`pull_request` `types: [labeled]`), posée par `ameesh ci run` après ses gardes. Le job `plan` vérifie, sans exécuter de test : motif exact de l'étiquette, égalité avec `pull_request.head.sha`, mode imposé par les refs (`release/**`/`integration/**` → `full`), et composition du merge-ref (parents = base du payload + tête). Toute demande invalide fait rougir le check requis. La concurrence d'une étiquette étrangère est distincte de celle d'une demande de gel. |
| **P1-3** le mode du run compte | l'applicabilité d'un run exige la même composition (PR, tête, base du payload) et un mode qui couvre la demande (`full` couvre `lot`, l'inverse non) ; un run de `push` ou un ancien dispatch ne verdit pas le check de PR. |
| **P2-4** `--force` | reprise explicite d'une composition déjà courue mais non verte, ou d'un check requis rouge malgré un run vert ; jamais un rejeu automatique d'un vert ou d'un run vivant. |
| **P2-5** réservation sous verrou | les marques `ci-label reserve/applied/released` (PR, étiquette, SHA, base, mode, jeton) sont écrites dans la transaction de la ligne du lot ; une réservation fraîche (TTL 15 min) empêche une double pose, une marque `applied` la referme, `released` sur refus. |
| précision pilote #42336 | étiquettes `ci-lot-<40hex>`/`ci-full-<40hex>` ; `full` obligatoire si source ou cible `release/**`/`integration/**` ; retrait/repose détenu par la commande sous réservation ; aucun rejeu d'un vert ou d'un run vivant. |

## Porte — ce que le candidat garantit

1. **Une poussée de lot ne lance plus rien** : `tests.yml` ne se déclenche plus
   sur une PR ordinaire ; le parcours PR part de l'étiquette de gel. Les
   poussées `main`/`release/**`/`integration/**` gardent la suite complète.
2. **Demande unique sur la tête gelée** : jalon `frozen` obligatoire (un
   `--sha` seul ne le remplace pas), tête de branche == SHA gelé, PR ouverte
   dont la tête est le gel, `--avis N` verdicts `ok` sur ce SHA exact par des
   acteurs distincts hors assigné (`0` léger, `1` sensible, `2` SQL/sécurité/
   prod), workflow du SHA portant le déclencheur.
3. **Idempotence** : vert applicable (même composition, mode couvrant, check
   requis vert) ou run vivant → rien n'est posé ; rouge/annulé → `--force`
   explicite ; une réservation fraîche d'un autre processus → rien n'est posé.
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

- `tests.test_l161g_ci_gel` **71/71** ; `tests.test_l161f_plan` 121,
  `tests.test_l744_ci_entrypoint` 2, `tests.test_l161a_ci_sans_code` 6,
  `tests.test_hermeticity_lint` 17 → **146/146** ; `tests.test_l161b_flow` +
  `tests.test_l161c_failures_realtime` **34/34** (`preuves/tests.txt`).
- **5 mutations** des gardes (tête bougée, gel obligatoire malgré `--sha`,
  avis de l'auteur, idempotence du vert, annulation hors composition gelée)
  font rougir le témoin correspondant, puis restauration vérifiée 71/71
  (`preuves/mutations.txt`).
- Lint d'herméticité : 0 constat sur 3 fichiers (`preuves/lint.txt`) ;
  `py_compile` OK (`preuves/py_compile.txt`).
- `tests.test_l161c_failures_realtime` (comportement existant de `ci.py`) :
  26/26 (`preuves/l161c.txt`).
- Aucun push de branche CI, aucun run, aucune CI : seules les refs
  `refs/atelier/1343/*` sont publiées.

## Points ouverts pour la relecture

- **Nombre d'avis** : paramètre explicite `--avis` (défaut 1) ; la classe n'est
  pas dérivée automatiquement du diff — choix à qualifier.
- **Après avis delta GO et R2** : le pilote vérifie les déclencheurs sur ce SHA
  exact avant poussée de branche + PR ; la CI du lot se demande au gel par
  `bin/ameesh ci run 1343 --avis 2`, puis la preuve est le check requis `tests`
  de la tête dans `gh pr checks`.
