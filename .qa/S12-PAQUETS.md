# Canon §12 — intégration par paquets : ce qui est supersédé pour mty-amh-impl-03

Fichier privé (`.qa/` non versionné), pour les reprises après rotation de session.
Écrit le 2026-10-14 ~19:4xZ.

## Source (à citer, pas à paraphraser)

- Canon manaty `processus/lots.md` §12, ligne ajoutée par la PR **manaty/home#65**,
  fusionnée **2026-10-14 19:25:36Z** (commit `2c4f18b85bfa3aa14ce35eaf6b6e4d53482aec04`,
  owner, session bureau Claude `17076001-2b29-414c-a288-06791a5d9719`, 14/10 vers 21:25) :
  > « **Par défaut, les lots ameesh relus s'intègrent par paquets, pas un à un.** Un lot
  > dont les verdicts sont complets ne passe que ses tests ciblés et n'est pas fusionné
  > seul sur `main` : le pilote le range dans le prochain paquet (une dizaine de lots).
  > Le paquet passe une seule CI complète, puis une seule fusion sur `main`. En cas
  > d'échec, le pilote isole le ou les lots fautifs et les sort du paquet, sans relancer
  > la CI des autres lots. Un lot n'est fusionné seul que s'il débloque la CI ou corrige
  > un incident. »
  > « La preuve CI du paquet remplace les CI individuelles pour les seuls lots
  > explicitement couverts : contenu exact intégré, dépendances satisfaites et avis
  > valides. Le manifeste relie chaque lot au commit et au run du paquet. Les anciennes
  > CI rouges étrangères au diff ne sont pas relancées individuellement pour ces lots. »
- Relais pilote : mesh-design, messages **#40169** (broadcast, 19:29:49Z) et **#40183**
  (19:30:09Z) : « Mon ancienne condition close/reopen 835/789 n'est plus un plan de
  reprise automatique ; conserve gels/tests/avis, aucun run individuel ordinaire à
  déclencher. Prépare R2 native 835/1252 comme #1098 routé, aucune clôture prématurée ;
  manifeste/CI du paquet couvrir a les seuls contenus exacts avec avis satisfaits.
  Paquet 1 en préparation main 0532. »
- Exceptions solitaires : débloquer la CI ou corriger un incident (**#1339** sous son P1
  résiduel). Aucune CI/fusion ordinaire à l'initiative d'un second opérateur.

## Conséquences pour mes lots

1. **Aucun close/reopen, aucune relance CI individuelle à mon initiative.** Les plans
   « close/reopen à tête constante + veilleur » écrits avant le 14/10 19:25Z pour
   #789 t1 (#603), #1035 t2 (#817), #835 (#631), #1252 (#952), #1020 (#794),
   #896 (#701), #705 (#575), #814 (#621) ne sont **plus** des plans de reprise.
2. **Les têtes gelées restent gelées** ; les avis restent acquis à leur portée exacte.
   Un changement de tête exige un nouveau contenu et une nouvelle relecture.
3. **L'intégration passe par le(s) paquet(s) du pilote** (mesh-design), qui décide du
   rangement, passe la CI complète du paquet et fusionne une fois. Le manifeste du
   paquet doit couvrir le contenu exact de chaque lot (fiches ci-dessous).
4. **Aucune veille locale, aucun sondage** (canon §3, #1324). Le réveil est le courrier
   du runner (verdict) ou du pilote. Le seul cas connu sans courrier : un run annulé
   (`ci._conclude` -> `to=None`, #901/#1110) — ne jamais compter sur un verdict pour un
   run annulé, vérifier l'état une fois au premier réveil utile.
5. **#1107 n2** (branche sans PR) n'est pas ouverte seule : sa PR n'a de sens que sur la
   main qui contient #1035 ; sous §12, c'est le paquet qui l'emporte, classée sensible
   prod (2 avis + CI exacte + feu pilote).

## Automatisations locales à ne jamais réarmer

| fichier / unité | geste | état au 14/10 19:4xZ |
|---|---|---|
| `.qa/qa-1035/arm-after-789.py` | retarget #817 + close/reopen (CI neuve) | **garde §12 posée** : refuse sans `S12_AUTORISATION_PILOTE=1` (sortie 5) ; trace conservée |
| `.qa/qa-789/merge-789-on-green.v6.3.py` + unité `ameesh-impl03-merge-789` | boucle sondage CI/verdict + fusion auto | unité arrêtée/supprimée 19:1xZ ; **NE PAS RÉARMER** (`.qa/qa-789/NE-PAS-REARMER.md`) |
| `.qa/merge-on-green.py` (branches 1020, 835, 896, 705, 814, 1003, 420) | veilleur fusion/après-fusion | aucun processus, aucune unité chargée ; ne pas réarmer pour un lot « relu » (§12) |
| `.qa/qa-1252/watch-1252-ci.py` + unité `ameesh-impl03-watch-1252-ci` | boucle sondage CI | unité **disabled/inactive** ; ne pas réarmer (§3, #1324) |
| unités `ameesh-impl03-watch-1211`, `ameesh-impl03-watch-1236-ci2` | réveils | disabled/inactive (#1211 est fusionné, #1236 volet ameesh chez qa-01) |
| configs `.qa/qa-1020/watcher.json`, `.qa/qa-835/watcher.json`, `.qa/qa-896`, `.qa/qa-705/watcher.json`, `.qa/qa-814/watcher.json`, `.qa/qa-1003/watcher-776.json` | cibles gelées des veilleurs | inertes ; toute réactivation exige un feu pilote nommant la tête et hors §12 |

Contrôle : `systemctl --user list-units --all | grep impl03` = seul
`ameesh-runner-agent@mty-amh-impl-03.service` actif ; `ps` = aucun
`merge-on-green|watch-1252|merge-789|merge-835|merge-1035|arm-after`.

## Fiches manifeste — contenu exact au 14/10 19:4xZ (main `0532faed4bba6a683073b9c7f5b9deca64abb2bd`)

Toutes ces valeurs ont été lues à la source en une passe (pas de sondage) :
`git rev-parse`, `git diff`, `git merge-tree`, `gh pr view`.

### #835 — PR #631, branche `lot/835-migration-lock-timeout`

- tête : `8ab9134b7beb7cb8a7790fafe56869cdb51bd1da` ; base PR : `main` ; OPEN, MERGEABLE/BLOCKED, `mergedAt=null`.
- delta du lot (3 fichiers, +363/-2), blobs :
  - `src/ameesh/migrations.py` = `eff221ca2fbbd2755ae22e161ce4631b45569b29`
  - `tests/test_migrations.py` = `2f78fc7406b1aeaffddbc025f5ac7085b47d77ec`
  - `docs/V1-MAILBOX-RUNNER.md` = `d211f444a205d53e4e1c511920d4c061db56c5c5`
- classe : sensible **SQL/prod** (mécanisme de migrations) -> 2 avis hors auteur.
  - Codex : GO delta codex3 0 B / 0 N du 13/10 19:29 (`docs/reviews/2026-10-13-835-delta-codex3.md`), conservé.
  - Claude : GO 16:22 **retiré** par sa source (17:22, 19:02/19:26) -> **R2 native due** sur `8ab9134b7`.
- CI individuelle : run `38492433852` terminal, `psycopg 4/4` + agrégat `tests` rouges
  par **exit 127 avant tests** (`.venv-ci/bin/python` absent, étape dépendances sautée) —
  panne d'environnement, jamais rejouée. Sous §12 : la CI du paquet couvre le lot.
- composition vs main `0532faed4` : merge-base `afa32932e` (128 commits de retard) ;
  `git merge-tree --write-tree` rc=0 -> arbre `638a3e4d57f69aee78b4590d0c83ee9d948ddc62`.
  Même fichier touché que main (`docs/V1-MAILBOX-RUNNER.md`, sections distinctes ~203 vs ~395) :
  pas de conflit textuel, mais chevauchement de fichier à consigner dans le manifeste.
  Main a ajouté la migration `0427_qa_role_guard.sql` + rôles `deploy/sql/` : à regarder
  avec le mécanisme `lock_timeout` modifié (2 avis).
- preuves ciblées auteur : `.qa/qa-835/proof-migration.log` (14 OK psycopg),
  `r2-mark-psql.log` (15 OK), `r2-mark-psycopg.log` (15 OK).

### #1252 — PR #952, branche `lot/1252-vm-agents-installer`

> **Candidat correctif local (14/10 ~20:0xZ)** : branche `prep/1252-r2-points`
> @ `d402f8809a51a036b2877f9ab15a9d43922b657f` (arbre `0e20118b28`) — points R2
> traités (previous-sha sur relance, paquet git lisible) ; delta 3 fichiers
> +106/−22 vs gel, composition main rc=0 arbre `7d876a63` ; 46/46 témoins,
> contre-exécution 3/3 rouges. Détail `.qa/qa-1252/R2-points-traitement-20261014.md` ;
> décision pilote (pousser ou lot de suite) en attente. **Aucun push.**

- tête : `4ac67f8f4329bc317edce314c2533263860cb5f4` ; base PR : `main` ; OPEN, MERGEABLE/CLEAN, `mergedAt=null`.
- delta du lot (4 fichiers, +1459/-22), blobs :
  - `deploy/mise-a-jour/vm.sh` = `d0f4982383ca92e0700b89b5ac31eeab212817df`
  - `docs/DEPLOIEMENT-HOTE-AGENTS.md` = `c415b1d77bfe2dd8de67a03c41d12540e0d49ba0`
  - `tests/test_1252_vm_agents_install.py` = `e35c03f13d0a6e20d90900972086ec7898ae6455`
  - `tests/test_503_agent_host.py` = `63302724cb7cd8c7de63ca607c1486bd8094bb16`
- classe : sensible **prod/exploitation** (`deploy/mise-a-jour/vm.sh`) -> 2 avis hors auteur.
  - R1 cx3 : revue `3991` sur le gel `09e0622de` ; le delta `09e0622de..4ac67f8f4` est
    **tests seuls** (+43/-40) et a été recoupé par cx3 le 14/10 10:29Z sans rejeu.
  - Claude : **R2 native due** sur `4ac67f8f4` (routée #1098).
- CI individuelle : run `38547022010` terminal **vert**, qualifié **CONFORME** par le
  pilote le 14/10 10:32Z (union PR testée `93932d982e5a`, parents
  `[main 6eeecc8e914e, tête 4ac67f8f4329]`, arbre `cc0e518e1b35` = merge-tree rc0 ;
  les 4 fichiers du lot sont ceux du gel, aucun n'est altéré par l'union). Conservé.
- composition vs main `0532faed4` : merge-base `7305957e` ; **aucun** des 4 fichiers du lot
  n'est touché par le delta de main (86 commits), aucune migration ;
  `git merge-tree --write-tree` rc=0 -> arbre `d9df15391f0a09ff8753471c22066b6c6d8eb1bd`.
- dû après fusion : témoin réel staging (`.qa/qa-1252/procedure-temoin-staging.md`),
  après #1236 complet et sous coordination bureau ; aucune opération prod Nexlink.

### #789 t1 — PR #603, branche `lot/789-slo-escalades`

- tête : `32930dd49f7ee16da9d466e723928ab0ed8cc73d` ; base PR : `main` ; OPEN, MERGEABLE/BLOCKED.
- delta du lot (2 fichiers, +452) : `src/ameesh/slo.py` = `5221638a1bb86a1ab6cb5267a1a90ba0e9a334db`,
  `tests/test_789_slo.py` = `97825750bc5cd6fafa8152fd45a19532a63e69d9`.
- classe : légère (canon §1).
- CI individuelle : run `38576269139` attempt 2 **annulé** par la vague coordonnée du
  14/10 19:20Z, sans courrier de verdict (#901/#1110) -> la CI du paquet doit couvrir t1.
- composition vs main `0532faed4` : merge-tree rc=0 -> arbre `6862bb14d980611ea0defa71b54598820dd5f315`.

### #1035 t2 — PR #817, branche `lot/1035-slo-escalades-t2` (empilée sur t1)

- tête : `5b018976942afe33aedf1de15b0f59d96c7b3cc1` ; base PR encore `lot/789-slo-escalades`.
- delta t2 seul (7 fichiers, +403/−9, base enregistrée `8a25bec5`) : `src/ameesh/alert_briefs.py`,
  `src/ameesh/exploitation.py`, `src/ameesh/notify.py`, `src/ameesh/storage/interface.py`,
  `src/ameesh/storage/postgres/operations.py`, `tests/parts/pilote-psql.txt`,
  `tests/test_789_slo_alerts.py` (blobs au commit t2 `5b0189769`).
- classe : sensible **prod** (exploitation.py, alert_briefs) -> 2 avis :
  auditeur/Codex GO 0 B/0 N (`docs/review/2026-10-13-1035-r2-auditeur-codex.md`) +
  orchestrateur/Claude GO sur cette tête (provenance vérifiée à la source) ; feu pilote
  conditionnel mesh-design 13/10 22:34. **Acquis à leur portée** tant que la tête ne bouge pas.
- composition : la branche t1 a bougé (`32930dd4`) et n'est **pas** ancêtre de t2 ;
  l'ordre du paquet est t1 puis t2 (même fichiers `slo.py`/`test_789_slo.py`).

### #1107 n2 — branche `lot/1107-slo-per-lot-guard` (aucune PR)

- tête conservée : `83f1055df39edf2b8618fdb31378e09268e8bf97` (base `5b0189769`).
- delta n2 seul (3 fichiers, +72/−6) : `src/ameesh/exploitation.py`, `src/ameesh/notify.py`,
  `tests/test_789_slo_alerts.py` (garde par lot + alerte `slo_unreadable` + témoin mixte).
- classe : sensible **prod** -> 2 avis + CI exacte + feu pilote. PR à ouvrir seulement
  quand sa base existe dans main ; sous §12, passer par le paquet.

## Portefeuille paquet(s) suivant(s) — relevé unique 14/10 19:46Z (main `0532faed4`)

Transmis à mesh-design (courrier 19:46Z, cc bureau) ; notes posées sur #896/#705/#814/#1035.
Aucun geste CI/fusion/push, aucune veille.

| lot | tête | PR | classe | avis | merge-tree vs main |
|---|---|---|---|---|---|
| #896 | `1cb46f06c7b0e3e3de29479c862496013a9e0bd8` | #701 | sensible identité/droits | cx3/Codex GO 0B/0P2 (15:32Z) + feu conditionnel pilote 16:05 nommant cette tête ; run 38589721090 annulé -> CI paquet | rc=0 arbre `51a1453e019e151b5511747e7a2432320d4c0ead` ; delta main depuis `046a106a5` disjoint ; 0 migration |
| #789 t1 | `32930dd49f7ee16da9d466e723928ab0ed8cc73d` | #603 | légère | CI 38576269139 annulée -> CI paquet | rc=0 arbre `6862bb14d980611ea0defa71b54598820dd5f315` |
| #1035 t2 | `5b018976942afe33aedf1de15b0f59d96c7b3cc1` | #817 | sensible prod | 2 GO (auditeur/Codex + orchestrateur/Claude) + feu conditionnel 13/10 22:34 | rc=0 arbre `4317247db7a6f0b1a9468eb99fdf4874b5f8b7b6` ; ordre t1 puis t2 |
| #1020 | `2241af9aaf2378401124f6f355b1cb7d761319f3` | #794 | — | — | **déjà dans le paquet 1 #1342** (candidat `b83d3fbf`) |

Bloqués par avis (R2 Claude native #1098) : #835 `8ab9134b7beb` (dossier publié
`refs/atelier/835/dossier-r2`), #1252 `4ac67f8f4329` (idem 1252), #814 `d3390df232a9`
(5/5 fichiers chevauchés par main -> avis delta final §12 en plus), #705 `4640bbb88ca2`.

### #705 — non intégrable en l'état (nouveau, 14/10 19:46Z)

- `git merge-tree --write-tree main(0532faed4) 4640bbb88` -> **rc=1**, conflit de contenu
  `src/ameesh/storage/postgres/work.py` : main `73b5b54ce` (#1193) contre les 5 commits
  #705 sur ce fichier (`27f28dfa6`, `45033f449`, `e457200ed`, `bd5daf341`, `2f0ce8dc3`).
- La qualification pilote du 09:32 (rc=0, arbre `795bd8ca`) portait sur main `dc40ccbb` ;
  main a avancé depuis. Réintégration -> nouvelle tête -> avis delta final §12, en plus du
  R2 Claude dû. Question posée à mesh-design : résolution locale sans push, ou branche de
  paquet. **Aucune résolution ni poussée faite ici.**

## #705 — résolution locale du conflit avec main 0532 (14/10 ~19:5xZ)

Préparée sur demande du pilote (courrier 19:49Z) : « prépare la résolution locale
fresh 1193/0532 dans storage/postgres/work.py, nouvelle tête/arbre et delta borné,
préserve les 5 commits 705 et parent_lot 1193/contrats. Aucun push lot/CI ».

- branche locale : `prep/705-fresh-0532` (worktree `mty-amh-impl-03-l705r`) ;
  **la tête gelée `4640bbb88ca2` et `origin/lot/705-designate-reviewer` restent intactes**.
- nouvelle tête : `1671ab6d829c787d9bdc44aa6f2715a80c49b5ae` ; arbre
  `451b5a77a711a7d3bcd4ac55089e04a0d460f698` ; parents
  `[4640bbb88ca2, main 0532faed4]` (main est ancêtre de la tête).
- seul conflit : `src/ameesh/storage/postgres/work.py` (`unassigned_lots`, #1193 x
  #705) ; résolution = SQL de #1193 (filtre de dépendances) + `item_columns(self.db)`
  du lot. Preuve de bornage : `git diff 4640bbb88 HEAD -- work.py` = **exactement le
  delta de #1193** (39+/5−) ; aucun contenu de #705 perdu, aucune ligne de #1193 perdue
  (`parent_lot` présent l. 235-312, filtre l. 1221).
- delta final du lot vs main : 20 fichiers, +1047/−102 (work.py : 168+/48− inchangé).
- témoin ciblé auteur (adaptation) : `tests.test_1193_suite_dependency`,
  `tests.test_705_reviewer_designated`, `tests.test_work`,
  `tests.test_l107_executor_contract` → **72/72 psql + 72/72 psycopg**,
  log `.qa/qa-705/resol-0532-tests.log`.
- à faire par le pilote : pousser la branche (ou non) comme nouvelle tête du lot,
  puis qualification Code+Claude SQL/prod sur le delta final ; R2 historique
  manquante non levée. Aucun push, aucun run.

## Composition #789 t1 → #1035 t2 (contrôle §12, 14/10 19:5xZ)

Ordre confirmé par le pilote (19:49Z). Preuve locale, aucun geste :
`main 0532faed4` + `t1 32930dd4` = arbre `6862bb14d9` (rc=0) ; puis + `t2 5b0189769`
= arbre `4317247db7` (rc=0), **identique au merge direct main×t2** — t2 porte déjà
les blobs finaux de t1 (`slo.py 5221638a`, `test_789_slo.py 97825750`), donc aucun
contenu de t1 n'est perdu dans l'ordre t1→t2. Fiches ci-dessus valables.

## Contrôle unique 14/10 20:2x–20:3xZ — canon #40902 reçu, main avancée (#1339)

- Rediffusion du canon « Processus de lot » reçue en courrier **#40902** (ameesh, 20:24:14Z) :
  §1–§14, dont §12 paquets (home#65) et §3 relecture avant CI. **Aucune règle nouvelle pour
  mes lots ; rien à appliquer, rien à répondre.**
- `main 0532faed4` → **`2458ec053123d3f33644fef1e092fc84cc2773d9`** (PR #1024, lot #1339,
  fusionnée 20:23:12Z ; delta 3 fichiers : `docs/EXPLOITATION.md`, `scripts/test-plan.py`,
  `tests/test_l161f_plan.py`) — **disjoint** des deltas #789 t1, #1035 t2 et #705.
- Composition §12 rafraîchie (rc=0, aucune tête ni blob touché) :
  - #789 t1 `32930dd4` : arbre **`64a66657394ff614f1c933836007865dfd8fa879`**.
  - #1035 t2 `5b0189769` (ordre t1 puis t2) : arbre **`55ed6e82c4ba947ce2ddbfde385000fbc0902934`**.
  - #705 `prep/705-fresh-0532` `1671ab6d` : arbre **`f02b2780f70a49d25773d6a648755f95522fe99d`** ;
    chevauchement **fichier** `docs/EXPLOITATION.md` avec #1339 (+29/−4 côté main, sections
    distinctes, rc=0, aucune adaptation) — à consigner au manifeste ; décision pilote (push de
    la nouvelle tête ou branche de paquet) **toujours en attente**.
- PR : #603 OPEN tête inchangée ; #817 OPEN tête inchangée, base encore `lot/789-slo-escalades`.
- Unités/veilles : aucune unité propre (`systemctl --user list-units --all | grep impl03`),
  aucun processus `merge-789|watch-1252|arm-after|merge-1035|merge-on-green` — §3 respecté.
  (`ameesh fil tail` lancé par erreur ce tour = boucle de suivi, tué aussitôt ; plus de job.)
- Courrier 20:26Z (ameesh-dev5, #1294) : confirme la fusion #1339 ; aucune action pour moi.
- Courrier envoyé au pilote (20:3xZ, cc bureau) : composition fraîche ci-dessus + chevauchement #705.

## Contrôle unique 14/10 20:4xZ — paquet 1 v3, briefs stale hors rôle

- Paquet 1 #1342 : mesh-design a recomposé **v3** sur `main 2458ec05` : candidat local
  `b689cb84db7a5fa2cf39088e21f070d364c3fc39`, arbre
  `3e01f21a35e23ae53ada67574a8fbceda399bb59` (10 apports 490/513/706/859/1020/1028/
  1145/1279/1293/1196 ; #1294 nommé couvert par 1293). Avis indépendant cx2/Codex **GO
  borné transport/composition, 0 nouveau B/P2** (20:36Z : 10 merges/patch-ids reproduits,
  43/44 blobs membres constants, 0 migration). **Ni gel, ni CI, ni fusion** : admission
  matrice, coordination bureau 38076/1341, suite complète exacte, fraîcheur et feu
  restent dus. Mon #1020 (tête `2241af9a`) est membre de ce paquet sous réserve
  d'admission (repli historique 9099/9205) ; note posée au lot (plan individuel de
  17:06Z supersédé par §12).
- Deux briefs stale_lot reçus **hors rôle** à 20:40Z (fan-out #1026) : #1260 (cx2) et
  #794 (impl-04). Vérification source unique : #1260 = lot de préparation sans
  branche/PR, porte = qualification/designation pilote (dépendance #1211 satisfaite
  depuis 13:15Z) ; #794 = PR #628 OPEN, tête `e915f0fcb555` inchangée, `mergedAt=null`,
  porte = R2 Claude finale (#1098) + feu nominatif. Déjà triés par 3 agents chacun
  avant moi (dev3/impl-01/auditeur ; dev3/impl-01/qa-01), pilote copié. **Abstention** :
  ni nudge (doublon), ni réassignation, ni clôture, aucun geste CI ; occurrence versée
  à #1026 pour la vérification post-déploiement du correctif.
- Aucun courrier non lu (boîte vide) ; aucune unité ni veille impl03 active.

## Reprise

- Contrôle unique 14/10 20:4xZ (aucun geste) : voir sections ci-dessus — main `2458ec05`
  (#1339), têtes #603 `32930dd4` / #817 `5b0189769` inchangées ; paquet 1 #1342 en
  préparation v3 (`b689cb84`, avis cx2 borné) ; #1020 membre sous réserve d'admission.
- Sous §12 : attendre le manifeste du pilote (paquet 1 = #1342, dix lots dont mon #1020 ;
  #896/#789 t1/#1035 t2 proposés pour le suivant). Ne rien pousser/relancer soi-même.
- Réponse attendue de mesh-design sur #705 (résolution locale ou branche de paquet).
- Réveil normal : courrier du runner (verdict de la CI du paquet) ou du pilote.
- Aucune action de ma part tant que le pilote n'a pas publié le contenu du paquet et
  nommé les lots couverts ; alors : vérifier le contenu exact (blobs ci-dessus), les avis
  à leur portée, le run du paquet (toutes parts réellement exécutées, aucune annulée
  #940), puis le geste est celui du pilote/auteur désigné.
