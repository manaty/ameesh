# Candidat #1343 — la CI d'un lot part au gel (P2-3 cx2, binôme borné)

- **lot** : #1343, évolution, classe sensible sécurité/porte CI (canon §2/§8).
- **auteur** : mty-amh-impl-04 (DeepSeek, exécutant sur ameesh-agents-1).
- **mandat** : mesh-design 14/10 19:52 (mail) ; canon §3 (home 66, owner 14/10
  21:45) ; direction pilote P1-2 (mesh-design 14/10 21:43) ; avis delta R1 cx2
  du 14/10 22:24 (#42838) ; constat P2-3 borné cx2 du 14/10 22:46 (#43144) ;
  **NO-GO 4847 (cx2) : le mode du run est perdu dans `_materialized`** ;
  reprise demandée par le pilote mesh-design 15/10 07:01.
- **base** : `main 2458ec053123d3f33644fef1e092fc84cc2773d9` (#1339 fusionné).
- **candidat** : `3ffddb0418c487d61f0f517336360b23404bc515` (7 commits).
  - ref atelier : `refs/atelier/1343/candidat-3ffddb041`
  - pièces : `refs/atelier/1343/pieces-3ffddb041`
  - branche locale : `lot/1343-ci-au-gel` (**non poussée pour ce candidat**),
    worktree `/home/ameesh/work/mty-amh-impl-04-1343`
- **tête examinée par cx2, constat restant** :
  `866764dc5d85d2b8009610fa492126703b966b5f`
  - P1 (relecture sous verrou), P2-2 (`--force`/check), P2-4 (couverture
    `full`/`lot`) corrigés et validés à la lecture par cx2 ; pièces
    `refs/atelier/1343/pieces-d1d875c89` reprises, `SHA256SUMS` OK.
  - P2-3 corrigé une première fois (corrélation par identifiant, `seen=`).
  - **NO-GO 4847 sur 866764dc5** : `_materialized` ne compare que les
    identifiants et `_request_once` ne transmet pas le mode — un run de lot,
    même rouge, libère une réservation `full` encore invisible (sonde AST :
    marque `{mode: full, seen: 0}` + run lot 12 → `True`).
- **têtes antérieures** : `3ce572f7b6e5d8b83e6964243371b6fc95168727` (NO-GO
  delta R1), `b60889b6151c3c01ebdf7ba79177197eb610aec2` (NO-GO R1).

## Delta depuis 866764dc5 (constat 4847 — ce que le binôme borne)

| constat cx2 sur 866764dc5 | correctif |
|---|---|
| **P2-3 (mode) — un run de lot matérialise une marque `full`** | `runs` porte désormais des couples `(identifiant, mode)` : `_request_once` transmet `(_run_id(r["row"]), r["mode"])` et `_materialized` n'accepte une exécution que si `_covers(run.mode, mark.mode)` — seul un run `full` matérialise une marque `full` ; un run de lot matérialise un lot, et un `full` couvre un `lot`. |
| **une marque `full` invisible ne doit pas être libérée par un run de lot rouge** | Témoin de bout en bout `test_un_run_de_lot_rouge_ne_libere_pas_une_reservation_full` : marque `full` d'un autre porteur + run lot rouge visible + `--force` → « already holds », aucune seconde étiquette. |
| **la borne `seen=` reste la garde d'antériorité** | Inchangée : `ident > seen` demeure requis ; la mutation M3 (borne retirée) fait rougir 4 témoins. |

La corrélation reste par identifiant et par mode, jamais par comparaison
d'horloges (`_run_ts` supprimé au delta précédent ; `REQUEST_RE` garde le champ
optionnel `seen=`, les marques antérieures restent lisibles).

## Porte — ce que le candidat garantit (inchangé depuis d1d875c89)

1. **Une poussée de lot ne lance plus rien** : `tests.yml` ne se déclenche plus
   sur une PR ordinaire ; le parcours PR part de l'étiquette de gel. Les
   poussées `main`/`release/**`/`integration/**` gardent la suite complète.
2. **Demande unique sur la tête gelée** : jalon `frozen` obligatoire, tête de
   branche == SHA gelé, PR ouverte dont la tête est le gel, `--avis N` verdicts
   `ok` sur ce SHA par des acteurs distincts hors assigné (`0` léger, `1`
   sensible, `2` SQL/sécurité/prod), workflow du SHA portant le déclencheur —
   gardes relues sous le verrou, juste avant la pose.
3. **Idempotence** : vert applicable (même composition, mode couvrant, check
   requis vert) ou run vivant → rien n'est posé ; rouge/annulé → `--force`
   explicite ; réservation fraîche d'un autre processus → rien n'est posé,
   libérée par corrélation (identifiant, mode).
4. **Annulation limitée au gel** : `ci run`/`ci cancel` n'annulent que les runs
   ouverts qui ne portent pas la composition gelée (`--all` pour l'inclure).
5. **Aucun vert sans tests** : au gel, les voies version seule (L157) et
   documentation seule (L161a) sont éteintes (mode `lot` et `full`).
6. **Exceptions marquées** : `--exception incident|unblock --reason …`.
7. **`--full --reason …`** : suite complète ; imposée par les refs release.
8. **Erreur visible** : refus actionnable dit à l'appelant et noté au lot.

## Preuves ciblées (auteur, sur ce SHA — aucun push de branche, aucune CI)

- `tests.test_l161g_ci_gel` **88/88** (86 + 2 témoins mode/run) ;
  `tests.test_l161f_plan` + `test_l744_ci_entrypoint` +
  `test_l161a_ci_sans_code` + `test_l161e_map` + `test_l932_ci_venv_cache` +
  `test_hermeticity_lint` **168/168** (1 skip) ;
  `tests.test_l161b_flow` + `tests.test_l161c_failures_realtime` **34/34**
  (`preuves/tests.txt`).
- **Reproduction 4847** : sonde directe (`full/seen0` + run lot 12 → `False`,
  run full 12 → `True`) et les deux témoins nommés, verts
  (`preuves/reproduction.txt`).
- **3 mutations du delta**, une par garde : couverture des modes retirée
  (2 rouges), matérialisation neutralisée (5 rouges), borne `seen=` retirée
  (4 rouges) ; restauration vérifiée 88/88 (`preuves/mutations.txt`). Les
  mutations P1/P2-2/P2-4 de la tête d1d875c89 restent valides : ce delta ne
  touche ni le verrou, ni `--force`, ni `_covers`.
- Lint d'herméticité : 0 constat sur 3 fichiers (`preuves/lint.txt`) ;
  `py_compile` OK (`preuves/py_compile.txt`) ; `diffstat`
  (`preuves/diffstat.txt`).
- Aucun push de branche CI, aucun run, aucune CI : seules les refs
  `refs/atelier/1343/*` sont publiées.

## Points ouverts pour l'avis borné

- Le binôme porte sur le delta `866764dc5..3ffddb041` (mode/run, seul objet du
  NO-GO 4847) ; le reste de la tête a déjà été examiné par cx2.
- Une marque sans `seen=` (écrite par une version antérieure à ce candidat)
  tient la clé jusqu'au `ttl` : fenêtre transitoire bornée à 15 min après
  déploiement, sans comparaison d'horloges. Alternative impossible sans changer
  l'étiquette (plafond GitHub de 50 caractères : `ci-full-<40hex>` en fait 48).
- Le chemin médié reste sans réservation atomique : la garantie est bornée aux
  connexions directes (observation R1, inchangée).
- Après avis delta GO (cx2) et R2 indépendante : le pilote vérifie les
  déclencheurs sur ce SHA exact avant poussée de branche + PR ; la CI du lot se
  demande au gel par `bin/ameesh ci run 1343 --avis 2`, puis la preuve est le
  check requis `tests` de la tête dans `gh pr checks`.
