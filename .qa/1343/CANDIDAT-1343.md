# Candidat #1343 — la CI d'un lot part au gel (relecture R1/R2)

- **lot** : #1343, évolution, classe sensible sécurité/porte CI (canon §2/§8).
- **auteur** : mty-amh-impl-04 (DeepSeek, exécutant sur ameesh-agents-1).
- **mandat** : mesh-design 14/10 19:52 (mail) et source pilote 18885 ; canon §3
  (home 66, owner 14/10 21:45).
- **base** : `main 2458ec053123d3f33644fef1e092fc84cc2773d9` (#1339 fusionné).
- **candidat final** : `b60889b6151c3c01ebdf7ba79177197eb610aec2`
  - ref atelier publiée : `refs/atelier/1343/candidat-b60889b61`
  - branche locale : `lot/1343-ci-au-gel` (**non poussée**), worktree
    `/home/ameesh/work/mty-amh-impl-04-1343`
- **point antérieur nommé par la désignation R1** :
  `df7c99e8a4483ea57cb6d34c88a7b58b126a528c`
  - ref atelier : `refs/atelier/1343/superseded-df7c99e8a`
  - delta : `delta-apres-df7c99e8a.patch` (4 fichiers, +41/−14). Deux
    correctifs d'auto-revue, faits **avant toute publication et avant tout
    début de relecture** :
    1. `--sha` seul ne remplace plus le jalon de gel (un lot jamais gelé
       pouvait dispatcher sans trace de gel) ;
    2. au gel, `plan["why"]` dit le gel, plus « not a pull request ».
    Plus 2 témoins (31 au lieu de 29) et 1 mutation (5 au lieu de 4).
- **pièces** : `candidat-1343.patch` (format-patch des 2 commits),
  `SHA256SUMS`, `preuves/`.

## Contenu (7 fichiers, +1328/−44)

| fichier | rôle |
|---|---|
| `.github/workflows/tests.yml` | PR de lot sans run automatique ; dispatch `mode/sha/base/lot` ; garde `plan` (HEAD ≠ SHA gelé → check requis rouge, aucun test) ; agrégat refusant un vert sans tests ; marque d'exception ; `run-name`. |
| `scripts/test-plan.py` | `--mode auto\|lot\|full` ; au gel, jamais version seule ni documentation seule ; `--base` obligatoire ; repli de lot conservé ; règle `release/**`/`integration/**`. |
| `src/ameesh/ci.py` | `gel_plan` / `cancel_plan` / `cmd_ci_run` / `cmd_ci_cancel` : gel, avis indépendants, idempotence, annulation des runs périmés, exceptions. |
| `src/ameesh/mesh_cli.py` | `ameesh ci run <lot>` et `ameesh ci cancel <lot>`. |
| `tests/test_l161f_plan.py` | témoins du plan au gel (sélection, voies éteintes, release, repli). |
| `tests/test_l161g_ci_gel.py` | 31 témoins de la décision, sans base ni réseau. |
| `docs/EXPLOITATION.md` | protocole borné du déclencheur, section L161f. |

## Porte — ce que le candidat garantit

1. **Une poussée de lot ne lance plus rien** : `pull_request` de `tests.yml`
   n'est plus déclenché que pour une cible `release/**` ou `integration/**` ;
   les poussées `main`/`release/**`/`integration/**` gardent la suite complète.
2. **Dispatch unique sur la tête gelée** : jalon `frozen` obligatoire (un
   `--sha` seul ne le remplace pas), tête de branche == SHA gelé (sinon refus
   « the ref moved »), `--avis N` verdicts `ok` sur ce SHA exact par des
   acteurs distincts hors assigné (`0` léger, `1` sensible, `2` SQL/sécurité/
   prod), workflow du SHA portant le déclencheur (sinon « rebase on main »).
3. **Idempotence** : vert ou en cours sur le SHA → rien n'est dispatché ;
   rouge/annulé → `--force` explicite ; un événement rejoué ne redispatch pas.
4. **Annulation limitée au gel** : `ci run` et `ci cancel` n'annulent que les
   runs ouverts qui ne portent pas le SHA gelé (`--all` pour l'inclure) ;
   jamais un run vert du gel.
5. **Aucun vert sans tests** : au gel, les voies version seule (L157) et
   documentation seule (L161a) sont éteintes dans le plan, doute et repli
   compris ; l'agrégat requis refuse `version_only`/`docs_only` ; `plan`
   compare HEAD au SHA gelé et rougit sans exécuter de test si la ref a bougé.
6. **Exceptions marquées** : `--exception incident|unblock --reason …`
   (marque dans le `run-name`, note au lot, motif exigé) ; une exception après
   gel teste la tête du correctif, pas le gel périmé.
7. **`--full --reason …`** : suite complète (paquet, dispatch manuel) ;
   `main`, `release/**`, l'intégration et les PR de release gardent leur suite
   complète automatique.
8. **Erreur de dispatch visible** : dite à l'appelant et notée au lot.

## Preuves ciblées (auteur, sur ce SHA)

- `tests.test_l161g_ci_gel` **31/31** ; `tests.test_l161f_plan` 121,
  `tests.test_l744_ci_entrypoint` 2, `tests.test_l161a_ci_sans_code` 6,
  `tests.test_hermeticity_lint` 14 → **174/174** (`preuves/tests.txt`).
- **5 mutations** des gardes (tête bougée, gel obligatoire malgré `--sha`,
  avis de l'auteur, idempotence du vert, annulation hors gel) font rougir le
  témoin correspondant (`preuves/mutations.txt`).
- Lint d'herméticité : 0 constat (`preuves/lint.txt`) ; `py_compile` OK
  (`preuves/py_compile.txt`).
- `tests.test_l161c_failures_realtime` (comportement existant de `ci.py`) :
  `preuves/l161c.txt`.
- Aucun push de branche CI, aucune PR, aucun dispatch, aucun run : seules les
  deux refs `refs/atelier/1343/*` sont publiées ; `gh run list` après poussée
  ne montre aucun run sur ces refs.

## Points ouverts pour la relecture

- **Delta final du rebase** : conflit de docstring de `lot_selection` résolu
  en gardant le paramètre `lot` du dispatch (#1343) et la liste de doutes de
  #1339 ; à qualifier comme delta final.
- **Nombre d'avis** : paramètre explicite `--avis` (défaut 1) ; la classe
  n'est pas dérivée automatiquement du diff — choix à qualifier.
- **Après avis GO** : le pilote vérifie les quatre déclencheurs sur ce SHA
  exact avant branche+PR ; la CI du lot se dispatche au gel
  (`bin/ameesh ci run 1343 --avis 2`), l'outil étant utilisable depuis le
  worktree avant installation.
