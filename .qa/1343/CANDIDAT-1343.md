# Candidat #1343 — la CI d'un lot part au gel (P2-3 cx2, binôme borné)

- **lot** : #1343, évolution, classe sensible sécurité/porte CI (canon §2/§8).
- **auteur** : mty-amh-impl-04 (DeepSeek, exécutant sur ameesh-agents-1).
- **mandat** : mesh-design 14/10 19:52 (mail) ; canon §3 (home 66, owner 14/10
  21:45) ; direction pilote P1-2 (mesh-design 14/10 21:43) ; avis delta R1 cx2
  du 14/10 22:24 (#42838) ; **constat P2-3 borné cx2 du 14/10 22:46 (#43144)**.
- **base** : `main 2458ec053123d3f33644fef1e092fc84cc2773d9` (#1339 fusionné).
- **candidat** : `866764dc5d85d2b8009610fa492126703b966b5f` (6 commits).
  - ref atelier (à publier) : `refs/atelier/1343/candidat-866764dc5`
  - pièces (à publier) : `refs/atelier/1343/pieces-866764dc5`
  - branche locale : `lot/1343-ci-au-gel` (**non poussée pour ce candidat**),
    worktree `/home/ameesh/work/mty-amh-impl-04-1343`
- **tête examinée, un seul constat restant** :
  `d1d875c89168fe1fb92059cae5b331ccbe3ef750`
  - P1 (relecture sous verrou), P2-2 (`--force`/check), P2-4 (couverture
    `full`/`lot`) corrigés et validés à la lecture par cx2 ; pièces
    `refs/atelier/1343/pieces-d1d875c89` reprises, `SHA256SUMS` OK.
  - **P2-3** seul ouvert : demande créée dans la même seconde que son run.
  - delta depuis cette tête : `delta-apres-d1d875c89.patch` (2 fichiers,
    +175/−61, 1 commit).
- **têtes antérieures** : `3ce572f7b6e5d8b83e6964243371b6fc95168727` (NO-GO
  delta R1), `b60889b6151c3c01ebdf7ba79177197eb610aec2` (NO-GO R1).

## Delta depuis d1d875c89 (ce que le binôme borne)

| constat cx2 sur d1d875c89 | correctif |
|---|---|
| **P2-3 — la marque `applied` et le run de la même seconde : la reprise `--force` restait bloquée quinze minutes** | La marque ne se corrèle plus au run par les horloges (base, fractionnaire ; GitHub, à la seconde) mais par **identifiant d'exécution**. `_request_once` lit les runs une fois avant la pose, en retient le plus grand identifiant connu (`watermark`) et l'écrit dans la marque : `… token=<16> seen=<id>`. `reservation_holder(…, runs=[…])` libère une marque dès qu'une exécution visible de la composition porte un identifiant supérieur à son `seen=` (`_materialized`) ; plus aucune comparaison `last.ts < after`. La lecture du watermark est **avant la pose** : le run de la demande, créé ensuite, a forcément un identifiant supérieur ; celui d'une demande postérieure à un run connu ne le libère pas. |
| **ne pas arrondir ni tolérer une seconde** | Aucun arrondi, aucune tolérance : le témoin `test_la_meme_seconde_ne_decide_pas` fixe la même seconde (`ts = t + 0.125`, run `created_at = t`) et n'attend que l'identifiant. |
| **une demande encore invisible créée après un ancien run ne doit pas être libérée** | Témoin de bout en bout `test_une_reprise_invisible_apres_un_run_rouge_ne_repose_pas` : premier `--force` sur le run rouge 12 (marque `seen=12`), second `--force` avant que le nouveau run soit visible → recule (« already holds »), une seule pose. |
| **marque écrite avant #1343 (sans `seen=`)** | Non corrélable : elle tient la clé jusqu'au `ttl` (15 min), jamais libérée par une horloge — choix borné et documenté, plutôt que de risquer la libération d'une demande encore invisible. Témoins `test_une_marque_sans_watermark_tient_jusqu_au_ttl` et `test_une_marque_sans_watermark_tient_la_cle`. |

`_run_ts` (comparaison d'horloges) est supprimé ; `REQUEST_RE` gagne le champ
optionnel `seen=` (les marques déjà journalisées restent lisibles).

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
   désormais libérée par corrélation d'identifiant (P2-3).
4. **Annulation limitée au gel** : `ci run`/`ci cancel` n'annulent que les runs
   ouverts qui ne portent pas la composition gelée (`--all` pour l'inclure).
5. **Aucun vert sans tests** : au gel, les voies version seule (L157) et
   documentation seule (L161a) sont éteintes (mode `lot` et `full`).
6. **Exceptions marquées** : `--exception incident|unblock --reason …`.
7. **`--full --reason …`** : suite complète ; imposée par les refs release.
8. **Erreur visible** : refus actionnable dit à l'appelant et noté au lot.

## Preuves ciblées (auteur, sur ce SHA — aucun push de branche, aucune CI)

- `tests.test_l161g_ci_gel` **86/86** (82 + 4 témoins P2-3) ;
  `tests.test_l161f_plan` 121, `tests.test_l744_ci_entrypoint` 2,
  `tests.test_l161a_ci_sans_code` 6, `tests.test_hermeticity_lint` 17 →
  **146/146** ; `tests.test_l161b_flow` + `tests.test_l161c_failures_realtime`
  **34/34** (`preuves/tests.txt`).
- **Reproduction P2-3** (scénario de la revue) : témoin unique nommé, vert
  (`preuves/reproduction.txt`).
- **4 mutations**, une par garde P2-3 (matérialisation neutralisée, libération
  par toute exécution, `seen=` non enregistré, watermark toujours nul) : chacune
  fait rougir ses témoins, restauration vérifiée 86/86
  (`preuves/mutations.txt`).
- Lint d'herméticité : 0 constat sur 3 fichiers (`preuves/lint.txt`) ;
  `py_compile` OK (`preuves/py_compile.txt`) ; `diffstat`
  (`preuves/diffstat.txt`).
- Aucun push de branche CI, aucun run, aucune CI : seules les refs
  `refs/atelier/1343/*` sont publiées.

## Points ouverts pour l'avis borné

- Le binôme porte sur le delta `d1d875c89..866764dc5` (P2-3 seul) ; le reste de
  la tête a déjà été examiné.
- Une marque sans `seen=` (écrite par une version antérieure à ce candidat)
  tient la clé jusqu'au `ttl` : fenêtre transitoire bornée à 15 min après
  déploiement, sans comparaison d'horloges. Si le binôme juge ce repli
  inacceptable, la seule alternative serait de propager le jeton de demande
  dans le run — impossible sans changer l'étiquette (plafond GitHub de 50
  caractères : `ci-full-<40hex>` en fait déjà 48).
- Le chemin médié reste sans réservation atomique : la garantie est bornée aux
  connexions directes (observation R1, inchangée).
- Après avis delta GO et R2 : le pilote vérifie les déclencheurs sur ce SHA
  exact avant poussée de branche + PR ; la CI du lot se demande au gel par
  `bin/ameesh ci run 1343 --avis 2`, puis la preuve est le check requis `tests`
  de la tête dans `gh pr checks`.
