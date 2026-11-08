# #5495 — dossier C3 v1 : première activation de l'exécuteur élu L2

Bureau (BG), 2026-11-08, sur demande de mesh-design #200619. Branche `review/bureau-5495-c3`.
**Aucun geste n'a été fait.** Seuls des relevés en lecture ont été pris : stat, sha256sum,
`systemctl show`/`is-*`, `git rev-parse`, SELECT. Les scripts de pose et de retour tournent
à blanc par défaut et n'ont pas été lancés avec `--apply`.
Relecture demandée : deux familles hors claude.

## 0. Résumé

| Point | Constat (relevé 08/11 ~11:00Z) | Ce que propose ce dossier |
|---|---|---|
| 1. Canon d'AG2 | L2 d'AG2 est au niveau de la pose passive du 04/11 : canon `c336c53c`, pas de garde ni de drop-in, `AMEESH_OPERATEUR=human:smichea` | AG2 reste **hors C3** : minuterie désactivée, inchangée. La parité d'AG2 fera l'objet d'un dossier séparé, après une C3 réussie (règle #190310 §6, inversée) |
| 2. Écart ab4 → canon actuel | 980 d'AG1 lit `ab4ae5b1` (05/11) ; les agents sont à `b8c4ea12` (+26 commits, dont les fiches `hotes/`) | P1 : copie qualifiée du canon à la tête des agents, posée comme en phase 2A (archive + reçu, ancien canon conservé) |
| 3. Preuve cgroup #5967/0550 | 0550 est appliquée ; aucun drain n'a jamais été fait sous `ameesh:staging` | Preuve pendant C3 même : drains consignés sous `actor=ameesh:staging`, aucun drain `human:` dans la fenêtre (`sonde-c3.sh apres`) |
| 4. Minuterie 4f9 ≠ 5c1 | AG1 et AG2 ont `OnBootSec=60s` (5c1). 4f9 et 1.44.1 ont `OnActiveSec=60s` (#5853) | P2 : poser la minuterie 1.44.1 (`d59f9a50…`) avant l'activation |
| 5. N7 | Garde et drop-in en 980:0755/0644 : 980 peut les changer | Sonde en lecture (DropInPaths unique, empreintes du service, du drop-in, de la garde et de la minuterie) à T-15 et à chaque C3. Lot produit pour `ameesh doctor`. Le durcissement des droits n'est pas proposé (voir §5) |
| Version | Runtime L2 = kit `5c1e679b` (paquet ameesh 1.37.0) ; flotte = 1.44.1 (`5f5bc7f2a`) | Écart sans effet pour le premier C3, démontré au §6, sous une condition vérifiée à T-15. Réalignement : lot suivant, avant toute migration non additive |
| Prérequis nouveau | Détenteur `mesh-design` = **AG2** (epoch 389) ; l'exécuteur ne tourne que chez le détenteur | C3 seulement si AG1 détient `mesh-design` à T-15. Sinon NO-GO et BG porte la coupure |
| Prérequis nouveau | `arguments` de L2 = `["--sans-poste"]` seul ; la clé d'AG1 n'est pas sur manaty-mesh-1 | P3 : mêmes arguments que BG (`--absent manaty-mesh-1=…`, `--rapporteurs …`) |
| Prérequis nouveau | Config de soumission `~ameesh/.config/ameesh/staging-executor.json` absente des 3 VM ; aucune demande `mesh-staging` jamais consignée | Activation : cette config est posée sur AG1 seulement (copie de `/etc/ameesh/staging-submit.json`) |

Le relevé brut à blanc est dans `releve-prealables-20261108T1100Z.txt`. `sonde-c3.sh` y
compte 3 KO, ceux de la dernière colonne : arguments, canon, détenteur.

## 1. État courant relevé (lecture)

### AG1, compte `ameesh-staging` (uid 980), pose passive des 06/11 et 08/11

- **Unité.** `~/.config/systemd/user/ameesh-staging.service` `f8add4eb…` = blob `411fb16d`.
  Ce blob est identique en 5c1, 4f9 et 1.44.1 : `git rev-parse <c>:deploy/systemd/ameesh-staging.service`.
- **Drop-in.** `ameesh-staging.service.d/10-native-actor.conf` `ce3ed09c…` est le seul de `DropInPaths`.
- **Garde.** `~/.local/libexec/staging-startup-guard.py` `4bde802e…`.
- **Minuterie.** `ameesh-staging.timer` `f6460989…` (OnBootSec). `disabled`/`inactive` ;
  service `inactive`, MainPID 0, UMask 0022. Linger yes.
- **Droits.** `~/.config`, `~/.config/systemd/user`, `…service.d`, `~/.local/libexec` et la
  garde sont en `755 ameesh-staging`. Le home est en 0700 et `.config/ameesh` en 0700
  (`stat -c '%a %U:%G'`).
- **Configuration.**
  - `staging-executor.json` `c420d281…` = `/etc/ameesh/staging-submit.json` (root 0644).
  - `role=mesh-design`, `candidates=[ameesh-agents-1, ameesh-agents-2]`, `arguments=["--sans-poste"]`,
    `repo=~/staging/ameesh`, `target=origin/main`.
  - `expected_sha256` : `quiescence-staging.sh` f988ee7a, `bin/python` e50d468e, paquet 0d38f1e7.
- **`bascule.env`** (980:0600).
  - Clés : `VM PERSONAS_VM HOTES_STAGING AMEESH_OPERATEUR`.
  - `AMEESH_OPERATEUR=ameesh:staging`.
  - `HOTES_STAGING` nomme les 3 VM et `manaty-mesh-1=root@10.77.0.1`.
- **`env`** : clés `AMEESH_DSN AMEESH_SCHEMA`. Les valeurs n'ont pas été lues.
- **Runtime.** `venv-courant → venv-5c1e679`, paquet `ameesh` 1.37.0 (METADATA). Le clone dédié
  `~/staging/ameesh` est à `5c1e679b`.
- **Canon.** `~/staging/canon` HEAD `ab4ae5b1` (05/11 19:11+01), `canon_ref=ab4ae5b1`.
  `canon-c336-retained-before-ab4` est conservé.
- **Accès 2B.** La clé `SHA256:ZkiBds0z…` donne root sur AG1, AG2 et AG3 (attestation #200612).
  Elle n'est pas sur manaty-mesh-1 : `grep` d'`authorized_keys` de mesh-1 via la route
  AG2→mesh-1 = une seule ligne L2, celle d'AG2.

### AG2, compte `ameesh-staging` (uid 980), pose passive du 04/11 (#190531)

- **Unité et minuterie.** `f8add4eb` et `f6460989`, identiques à AG1 ; **aucun** dossier
  `ameesh-staging.service.d`, **aucune** garde. Minuterie `disabled`/`inactive`.
- **Canon.** `~/staging/canon` HEAD `c336c53c` (03/11), `canon_ref=c336c53c`.
- **`bascule.env`** : `AMEESH_OPERATEUR=human:smichea`.
- **Fichiers.** `env` contient `AMEESH_CONFIG`, et un `.pgpass` est dans le home (modèle d'avant la phase 2A).
- **Runtime.** `venv-courant → venv-5c1e679` ; `venv-6add86b` est conservé.

### Flotte et base

- **Version.** Agents en 1.44.1 (`5f5bc7f2a`, release de 11:00Z).
- **Canon des agents.** `canons=[{path: /home/ameesh/canon, ref: origin/main}]`, HEAD
  `b8c4ea12` sur AG1, AG2 et AG3.
- **Base.** `max(schema_migrations.version)=555`.
  `select count(*) from schema_non_additive_migrations where version>544` = **0**.
- **Élection.** `ameesh role status mesh-design` à 11:0xZ : détenteur `ameesh-agents-2`
  (epoch 389), AG2 rang 0 (score 100.6), AG1 rang 1 (100.5).
- **Configuration de release.** `release.projects.ameesh.staging_executor` vaut
  `~/.config/ameesh/staging-executor.json`. Ce fichier est **absent** sur AG1, AG2 et AG3.
  `staging_executor.latest(db, "mesh-staging")` = aucun enregistrement.

## 2. Points 1 à 5 de #200619

### Point 1 — canon d'AG2

- **Constat.** AG2 lit `c336c53c`. Il n'a pas reçu la phase 2A V6 : ni garde, ni drop-in, ni
  acteur machine. Son `AMEESH_OPERATEUR=human:smichea` est un humain fabriqué pour un démon,
  ce que #5967 exclut (« Fabriquer human:smichea … pour un daemon est exclu »).
- **Écart.** Activé tel quel, AG2 drainerait au nom de l'owner : `vm.sh operateur_drain`
  prend la branche `human:`.
- **Avant C3.** AG2 n'est **pas** activé : sa minuterie reste `disabled`, et c'est une case de
  `sonde-c3.sh`. Il reste dans `candidates` : sans minuterie, il ne tique jamais.
- **Conséquence.** Une C3 n'a lieu que si AG1 détient `mesh-design` (voir §3, préalable P0).
  La parité d'AG2 (phase 2A transposée, plus canon) fera l'objet d'un dossier séparé, après
  une C3 réussie sur AG1.
- **Preuve.** `sonde-c3.sh` : `AG2 timer L2 = disabled`.

### Point 2 — écart ab4ae5b1 → canon actuel

- **Constat.** 26 commits en première ligne entre `ab4ae5b1` et `b8c4ea12`
  (`git rev-list --count`). Ils touchent 11 fichiers, dont `hotes/ameesh-agents-{1,2,3}.md`,
  `hotes/manaty-mesh-1.md`, `federation.yaml` et `agents/bureau.md`.
  Exemple : `capacity: baseline` pour AG1 et AG2 (#5926), et les fiches pgclient17 (#6291).
- **Écart.** Le contrôle C3 n°4 de #190310 exige `canon_ref` égal au canon des agents.
  De plus, l'alerte `staging_canon_stale` (#5727, `staging_watch.CANON_DRIFT_S`=1 j) se
  déclencherait dès le premier enregistrement.
- **Avant C3 (P1).**
  - `preparer-canon-c3.sh <tête des agents>` sur BG : clone complet, hooks vidés,
    `origin=https://github.com/manaty/home.git`, archive déterministe et reçu (arbre,
    `.git/config`, sha de chaque fichier suivi).
  - Puis `pose-c3-AG1.sh --apply prealables` :
    - archive root 0600 et contrôle des membres ;
    - extraction **sous 980** ;
    - vérification HEAD, arbre, propreté, absence de hook et fichiers suivis ;
    - renommage (`canon-ab4ae5b1-retained-before-<new>` conservé) ;
    - `canon_ref` réécrit atomiquement en 0600.
  - Même forme que `prepare-passive-configuration-v6.py` (canon-ab4 et reçu). Essai à blanc
    sur BG avec `b8c4ea12` : arbre `219caba4…`, 172 fichiers suivis conformes.
- **Preuve.** `sonde-c3.sh` : `canon 980 = canon_ref = canon des agents`, et canon 980 propre.
  Le canon pouvant avancer pendant la journée, l'archive est refaite à la tête du moment,
  au plus tard à T-60. `pose-c3-AG1.sh` refuse un reçu différent de la tête des agents.

### Point 3 — preuve cgroup #5967/0550

- **Constat.** `vm.sh` (`5f5bc7f2a`, l. 719-745) :
  - n'émet `AMEESH_OPERATEUR=ameesh:staging` que si `preuve_unite_staging /proc/self/cgroup`
    trouve `0::*/ameesh-staging.service` hors tour d'agent ;
  - sinon, `operateur_drain` rend 1, et il n'y a pas de drain au nom du service.
  - Migration `0550_operator_pause_staging_actor.sql` appliquée (max 555).
  - `operator_pause_events` sur 3 jours : seulement `ameesh:file-decoupage` (7319 lignes),
    aucun `ameesh:staging`.
- **Écart.** Jamais exercée en vrai. La séquence de BG ne passe pas par l'unité.
- **Pendant C3.** Aucun geste supplémentaire : la preuve **est** la première séquence de l'unité.
- **Critère.** `sonde-c3.sh apres --depuis <TS>` doit montrer :
  - au moins un drain `actor=ameesh:staging`, principal = owner de la cible (serve.owner) ;
  - **0** drain `human:%` au motif `drain de déploiement%` dans la fenêtre ;
  - les levées par `expect_reason`.
- **Limite**, reprise de #194536 : le cgroup n'est pas une barrière contre l'appelant de
  `vm.sh`. Il empêche seulement d'attribuer au service un drain lancé ailleurs.

### Point 4 — minuterie 4f9 ≠ 5c1

- **Constat.** `git diff 5c1e679b 5f5bc7f2a -- deploy/systemd/ameesh-staging.timer` change une
  seule ligne : `OnBootSec=60s` → `OnActiveSec=60s`. C'est le commit `b16ee5e5f` (#5853) :
  après un redémarrage de `user@`, `OnBootSec` est déjà passé. La minuterie reste alors
  « active (elapsed) » et ne tique plus. Le blob 4f9 = celui de 1.44.1 (`c710a0d2`,
  sha256 `d59f9a50…`).
- **Écart.** Avec la minuterie 5c1, un redémarrage de `user@980` (vm.sh, B2 du 04/11) arrête
  L2 en silence, et la demande reste pending.
- **Avant C3 (P2).** Poser la minuterie de 1.44.1, octets tirés de
  `git show 5f5bc7f2a:deploy/systemd/ameesh-staging.timer`, sha vérifié avant envoi. Puis
  `daemon-reload` du gestionnaire 980. La minuterie reste désactivée.
- **Preuve.** `sonde-c3.sh` : `AG1 timer sha256 = d59f9a50…`. Après l'activation :
  `NextElapseUSecMonotonic` non nul.

### Point 5 — N7 : garde et drop-in modifiables par 980

- **Constat.** Droits : voir §1. 980 possède `~/.config/systemd/user` et ses parents.
  - Changer le propriétaire de la garde ou du drop-in pour root ne protège rien : 980 peut
    renommer ou remplacer toute entrée d'un dossier qu'il possède.
  - Un drop-in utilisateur plus loin dans l'ordre peut aussi vider `ExecStartPre=`.
  - Une protection réelle exigerait une unité **système** (`User=ameesh-staging`) ou
    `chattr +i` sur toute la chaîne de dossiers. C'est un changement de conception,
    hors C3, et l'avis de l'auditeur le classe non bloquant.
- **Proposition.**
  - (a) **Sonde opérateur maintenant**, dans `sonde-c3.sh`, à T-15 et après chaque C3 :
    - `DropInPaths` = exactement `…/10-native-actor.conf`, et ce dossier ne contient que ce fichier ;
    - `FragmentPath` attendu ;
    - sha256 du service `f8add4eb`, du drop-in `ce3ed09c`, de la garde `4bde802e` et de
      la minuterie ;
    - aucune autre unité `ameesh-staging*` dans `~/.config/systemd/user`.
  - (b) **Lot produit** à ouvrir, que le bureau n'écrit pas : `ameesh doctor` lit la même
    chose pour l'unité de l'exécuteur élu de l'hôte.
    - Empreintes attendues prises dans `staging-executor.json` (`expected_sha256`, déjà lu
      par l'exécuteur), ou dans un champ `unit_sha256` nouveau.
    - Verdict `warning` si l'écart est hors séquence.
- **Preuve.** `sonde-c3.sh prealables` OK, et le même relevé après C3.

## 3. Prérequis apparus pendant le relevé

- **P0 — détenteur.** `staging_executor.tick` (l. 638-645) :
  `if not elected["held"] or elected.get("holder") != host: standby`. Il revérifie après
  avoir pris le bail (l. 673-677). AG2 n'étant pas activé, C3 exige qu'AG1 détienne
  `mesh-design` **au moment de la demande**. Aucune relève n'est forcée (#190531 : « sans
  forçage SQL ni interruption improvisée du pilote »).
- **Une fois lancée,** la séquence ne dépend plus du détenteur. Le bail d'exécution
  (`_AlertsLease`, clé `merge_locks`) est renouvelé par CAS sur son epoch, indépendamment
  du rôle.
- **P3 — arguments.**
  - `staging-executor.json` porte `["--sans-poste"]`. Or `staging.sh` lit `HOTES_STAGING`,
    qui inclut `manaty-mesh-1`, et la clé d'AG1 n'y est pas. La séquence L2 échouerait donc
    sur mesh-1.
  - BG lance `--sans-poste --absent "manaty-mesh-1=central runtime deferred; follow up separately" --rapporteurs "bureau mesh-design auditeur"`
    (`staging-cut-20261108T1000/worker.py` l. 10).
  - P3 aligne les deux copies (980 0600 et `/etc/ameesh/staging-submit.json`) sur ces
    arguments, avec les mêmes octets.
- **P4 — soumission.**
  - `release_cutoff.run_staging` (l. 2637-2654) soumet à l'exécuteur élu par
    `release.projects.ameesh.staging_executor`. Ce fichier est absent, donc aucune vraie
    demande ne peut être consignée aujourd'hui.
  - À l'activation, il est posé sur **AG1 seulement** : copie de `/etc/ameesh/staging-submit.json`,
    ameesh 0600.
  - Le comportement actuel de la release quand ce fichier manque est à confirmer par le
    pilote (Q1).

## 4. Alignement de version (kit 5c1e679b ↔ 1.44.1)

- **Ce qui tourne sous quel code.**
  - L'**exécuteur** tourne sur le venv de 980 (5c1, 1.37.0).
  - La **séquence** (`staging.sh`, `vm.sh`, `quiescence`) tourne **depuis l'arbre du commit
    demandé** (`staging_executor.py` l. 833-850 ; `docs/DEPLOIEMENT-HOTE-AGENTS.md` l. 980-990).
  - Les outils Python de la séquence tournent avec `$HOME/.local/share/ameesh/venv`, donc
    celui de 980 :
    - `sante-courrier.py`, `actif.py` ;
    - `staging_watch` via `PYTHONPATH=$DEPOT/src`.
    - Références : `staging.sh` l. 172-175 et 819-826.
- **Code de l'exécuteur, de 5c1 à 1.44.1** (`git diff --quiet`) :
  - **identiques** : `staging_executor.py`, `role_run.py`, `staging_watch.py`,
    `staging_pending.py`, `storage/postgres/events.py` (dont `land_lock`),
    `deploy/systemd/ameesh-staging.service`, `deploy/mise-a-jour/quiescence-staging.sh` ;
  - **modifiés** :
    - `db.py` : `application_name` (#5976), reconnexion (#5986), admission écrivaine
      partagée 5 s (#5980) ;
    - `config.py` et `canon.py` (`runs_interface`) ;
    - `release_cutoff.py` : chemin Nexlink (#6020, #6039, #6152, #6243).
  - Effet pour C3 : les sessions de L2 n'ont pas de nom dans `pg_stat_activity`. C'est de
    l'observabilité ; aucune fonction n'est touchée.
- **Admission des écritures.** En 5c1, `require_compatible_writer` refuse si la base porte
  une migration **non additive** inconnue du code. Aujourd'hui : 0 au-delà de 0544. Le
  staging n'applique que `migrate --additive-only` (`staging.sh` l. 748-749).
- **Outils de la séquence 1.44.1 sur le paquet 1.37.0.**
  - `analyse/attrs.py` lit l'AST de l'outil cible et cherche chaque `ameesh.<module>.<nom>`
    dans la source 5c1.
  - Résultat : `sante-courrier.py`, 22 attributs, aucun absent ; `actif.py`, 8 attributs,
    aucun absent.
  - Limite : c'est un contrôle des noms, pas de la sémantique. `account_window.py`
    (+208/-…) et `work.py` ont changé.
  - Une sonde de courrier en échec est « non concluante » (canon §3, `staging.sh` l. 49-53) :
    elle ne bloque pas. Un `actif.py` en échec vaut échec de C3, puis retour à BG.
- **Conclusion.** L'écart est sans effet sur le **premier** C3 si **0 migration non additive
  > 0544** à T-15 (case de `sonde-c3.sh`).
  - Si une telle migration apparaît : NO-GO, BG porte la coupure.
  - Réalignement à ouvrir en lot, avant la première migration non additive : kit L2
    reconstruit sur la release active, requalifié par l'auditeur, puis `bascule.py preparer`
    et `activer` sous 980.

## 5. Séquence C3 proposée (coupure de 22:00Z ou suivante)

Ordre, avec STOP au premier refus nommé :

1. **Avant 20:00Z** : avis des deux familles sur ce dossier et feu du pilote.
2. **T-60 (21:00Z)** :
   - `preparer-canon-c3.sh <HEAD des agents>` ;
   - puis `pose-c3-AG1.sh` à blanc, et `--apply prealables` si le blanc est OK.
   - La sonde finale doit être OK, sauf la case `détenteur` (P0), relevée à T-15.
3. **T-15 (21:45Z)** : `sonde-c3.sh prealables`.
   - Toutes les cases OK, `détenteur mesh-design = ameesh-agents-1` compris → GO.
   - Sinon **NO-GO** : BG porte 22:00Z, rien n'est activé, et les préalables P1 à P3
     restent posés (inertes, minuterie désactivée).
4. **GO** : `pose-c3-AG1.sh --apply activer`.
   - Il relance la sonde, pose la config de soumission AG1, puis fait
     `enable --now ameesh-staging.timer`.
   - BG ne lance pas de coupure, et la case « aucune séquence BG active » le prouve.
5. **22:00Z** : la release du détenteur AG1 soumet la vraie demande. Aucune demande n'est
   fabriquée (#190310 §2). La minuterie la prend dans les 60 s.
6. **Après la séquence** : `sonde-c3.sh apres --depuis <TS d'activation>`.
   - L'enregistrement porte `state`, `host=ameesh-agents-1`, `canon_ref` = canon des agents
     et les empreintes.
   - Drains `ameesh:staging` (point 3), 0 drain humain.
   - Journal de l'unité.
   - Plus l'attestation `actif.py` de la séquence.
7. Rapport en note de #5495. L'auditeur atteste C3. La minuterie reste active seulement sur
   feu du pilote. À défaut, `retour-c3-AG1.sh --apply activation`.

**Retour à BG** (repli #190310 §5) — si L2 refuse (`blocked`) ou échoue :

- `retour-c3-AG1.sh --apply activation` :
  - minuterie désactivée ;
  - si l'unité est encore en séquence : **arrêt du script (rc 3), sans tuer**, car le piège
    TERM fait le retour d'hôte ;
  - config de soumission retirée (gardée sous `/root`) ;
  - lecture du dernier enregistrement.
- Puis BG relance la coupure, comme le 08/11 à 10:00Z, dès que `staging_watch in_flight` est vide.
- Aucune réparation sur place. `retour-c3-AG1.sh --apply prealables <TS>` remet, depuis les
  reçus : canon ab4, `canon_ref`, minuterie 5c1 et arguments d'avant.

## 6. Pièces

| Fichier | Rôle |
|---|---|
| `sonde-c3.sh` | Lecture seule : préflight T-15 et sonde N7 (`prealables`) ; preuves C3 et cgroup (`apres`) |
| `preparer-canon-c3.sh` | Sur BG seulement : archive et reçu du canon qualifié |
| `pose-c3-AG1.sh` | À blanc par défaut ; `--apply prealables` (P1-P3), `--apply activer` (au feu) |
| `retour-c3-AG1.sh` | À blanc par défaut ; `--apply activation`, `--apply prealables <TS>` |
| `analyse/attrs.py` | Contrôle statique des noms `ameesh.*` utilisés par les outils de séquence |
| `releve-prealables-20261108T1100Z.txt` | Sortie de `sonde-c3.sh` à 11:00Z (minuterie attendue 5c1) |
| `MANIFEST.json` | sha256 de chaque pièce |

## 7. Questions ouvertes

- **Q1 (pilote).** Quand `staging_executor` manque, que fait aujourd'hui la release ameesh
  au moment du staging ? Et la config de soumission sur AG1 seul suffit-elle (release lancée
  par le détenteur) ?
  Si `mesh-design` passe sur AG2 entre T-15 et la coupure, la release d'AG2 ne soumet rien,
  et BG relance après constat (≥ 22:10Z). Acceptable ?
- **Q2 (pilote).** Parité d'AG2 : faire un dossier séparé après une C3 réussie (proposé), ou
  attendre cette parité avant toute C3 ? Avec l'élection actuelle (AG2 rang 0), une C3 sur
  AG1 seul n'aura lieu qu'au hasard du détenteur.
- **Q3 (relecteurs).** N7 : sonde de détection plus lot doctor seulement (proposé), ou exiger
  l'unité système ou `chattr +i` avant C3 ?
- **Q4 (relecteurs).** Version : acceptez-vous la démonstration du §4 avec sa condition
  (0 migration non additive > 0544), ou exigez-vous le réalignement du kit avant le premier C3 ?
- **Q5 (pilote).** Après une C3 réussie, la minuterie reste-t-elle active (porteur
  permanent), ou repasse-t-elle désactivée jusqu'à la parité d'AG2 ?
