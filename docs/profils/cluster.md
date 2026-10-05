# Profil d'hébergement « cluster » — un agent persistant par pod

Ce profil fait tourner **un** agent ameesh persistant dans un cluster
Kubernetes. C'est l'un des profils prévus par la décision
[0011](../design/decisions/0011-hebergement-configurable.md) (« un serveur ou
un cluster existant ») ; la base suit le profil
([0016](../design/decisions/0016-stockage.md)) : un Postgres durable,
sauvegardé hors de la machine.

C'est un profil **minimal** : l'exécuteur et son harnais, rien d'autre. Ni la
porte avec reçus humains, ni `ameesh-approve` ne sont exposés ; l'agent est
déclaré au canon avec les seules capacités `read` et `propose`.

Fichiers :

| Fichier | Rôle |
|---|---|
| [`Dockerfile`](../../Dockerfile), [`.dockerignore`](../../.dockerignore) | image de l'exécuteur, sans secret, sans harnais par défaut |
| [`deploy/k8s/`](../../deploy/k8s/) | manifestes d'exemple (kustomize) : StatefulSet, ConfigMap, Service sans port, NetworkPolicy, compte de service |
| [`deploy/k8s/secrets.exemple.yaml`](../../deploy/k8s/secrets.exemple.yaml) | forme des Secret attendus — jamais rempli, jamais appliqué tel quel |
| [`deploy/sql/role-superviseur.sql`](../../deploy/sql/role-superviseur.sql) | rôle Postgres en lecture seule pour un superviseur externe |

---

## 1. Architecture

```
                      dépôt du canon (git)                 Postgres durable
                      Host = nom du pod,                   (hors du pod : service
                      Agent, Placement                     géré ou StatefulSet dédié)
                             │ lecture seule (clé de              ▲
                             │ déploiement)                       │ AMEESH_DSN + PGPASSWORD
  ┌─ pod ameesh-agent-0 ─────┼────────────────────────────────────┼──────────────┐
  │  init migrate      ameesh migrate ───────────────────────────►│              │
  │  init canon        git clone ; ameesh canon sync --fetch ────►│              │
  │                          ▼                                    │              │
  │  canon-fetch  ──► emptyDir canon/ (git fetch périodique)      │              │
  │                          │ monté en LECTURE SEULE             │              │
  │  runner        agent-runner ── canon sync périodique ────────►│              │
  │   (tini, PID 1)   └─ harnais (claude | codex | dsh)           │              │
  │                        HOME = PVC : sessions, état, fils, dossiers de travail│
  └──────────────────────────────────────────────────────────────────────────────┘
        aucune entrée réseau (Service sans port, NetworkPolicy) ; sortie : base,
        git, API du fournisseur de modèles
```

* **Identité.** Le nom du pod (`ameesh-agent-0`) est l'hôte ameesh
  (`AMEESH_HOST`, par l'API descendante). Le canon déclare un `Host` de ce
  nom, avec son responsable et sa politique, et le `Placement` de l'agent sur
  cet hôte (spec §4.2). Sans eux, l'agent n'est pas réclamable (R14, C4 :
  fail closed). Une réplique, toujours : deux répliques seraient deux hôtes.
* **Canon.** Cloné par l'init `canon` avec une clé de déploiement **en
  lecture**, puis `ameesh canon sync --fetch`. Ensuite, le conteneur
  `canon-fetch` fait `git fetch` toutes les `fetch-interval` secondes, et
  l'exécuteur relit le canon toutes les `AMEESH_CANON_SYNC_INTERVAL` secondes
  (sans fetch : il n'a pas la clé). Le clone est monté **en lecture seule**
  dans le conteneur de l'exécuteur ; la clé n'y est jamais montée.
  `canon_ref: origin/main` dans `config.json` fixe la branche canonique de
  confiance de l'hôte (spec §8.2) : pas d'amorçage `--bootstrap-ref` à faire.
  Limite : un seul dépôt de canon (les membres d'une fédération présents
  localement, `workspace_path`, ne sont pas clonés par cet exemple).
* **État.** La base porte l'état (baux, boîte, registre, actions). Le volume
  persistant `home` porte ce qui vit sur l'hôte : sessions du harnais
  (`~/.claude`, `~/.codex`…), état local d'ameesh (`~/.local/state/ameesh`,
  dont les **fils lisibles**), dossiers de travail (le `cwd` du Placement, par
  exemple `/home/ameesh/travail/<projet>`).
* **Arrêt.** tini relaie SIGTERM à `agent-runner`, qui arrête le groupe du
  harnais (SIGTERM, 3 s, SIGKILL), joint ses tours (8 s au plus) et rend le
  bail. `terminationGracePeriodSeconds: 60` couvre aussi une requête en vol
  jusqu'au `statement_timeout` (30 s). Au-delà, Kubernetes tue le pod : le
  bail expire seul (`lease_ttl`) avant toute reprise.
* **Sonde.** `ameesh doctor --probe` en `readinessProbe` : base joignable,
  schéma présent, migrations à jour — en lecture seule, ni NOTIFY ni
  comptage. Pas de `livenessProbe` : relancer le pod ne répare pas une base
  injoignable, et l'exécuteur gère déjà cette panne ; s'il meurt, le conteneur
  sort et Kubernetes le relance.

## 2. Ce qui est garanti, et ce qui ne l'est pas

Le tableau de la spec ([§11](../design/specification.md)) vaut ici, avec ces
différences :

| Garanti dans ce profil | Pas garanti |
|---|---|
| **un agent par pod** : son système de fichiers, ses processus et ses sessions sont isolés des autres agents (autre pod, autre volume) — isolation que le profil local, où tous les agents partagent l'utilisateur Unix, n'a pas | isolation **dans la base** : l'exécuteur et son harnais partagent le rôle Postgres d'ameesh (`PGPASSWORD` est dans l'environnement du harnais, comme les hooks `agent-mail` l'exigent) ; l'agent peut lire et modifier l'état des autres agents du même schéma |
| le canon est en **lecture seule** pour l'agent ; la clé de déploiement ne lui est pas montée ; il ne peut pas réécrire `refs/remotes/…` du clone (limite §4.1 du profil local) | l'agent peut écrire dans la base (`canon_state`, registre) avec le rôle d'ameesh : la monotonie et le prédicat de réclamation restent les seuls garde-fous |
| racine du conteneur en lecture seule, aucune capacité Linux, utilisateur non root, aucun jeton d'API Kubernetes | sortie réseau ouverte dans l'exemple (base, git, fournisseur) : à restreindre selon le cluster ; pas de bac à sable réseau ni de proxy MCP (v1.x) |
| aucune entrée réseau : la **porte et `ameesh-approve` ne sont pas exposés** ; une action `irreversible` ou `costly` reste bloquée faute de reçu (`[receipt_required]`) — le comportement sûr | aucune approbation humaine possible depuis ce profil : il faut un `ameesh-approve` déployé **hors de portée des agents** (spec §9), ce que ce profil ne fournit pas |
| un agent sans responsable, mal placé, ou dont le canon est invalide n'est pas réclamé | l'exemple ne vérifie pas que les capacités de l'agent se limitent à `read` et `propose` : c'est la fiche `Agent` du canon (revue par PR) qui le dit |

Fiche d'agent attendue (extrait, spec §4.2) :

```yaml
type: Agent
title: veilleur
responsible: human:alice
capabilities: [read, propose]     # ni action irréversible ni approbation dans ce profil
harness: claude
provider: anthropic
credential_mode: api-key
```

## 3. Mise en place (actes de l'exploitant)

Rien de ceci n'est fait par un agent ni par le dépôt : création de registre,
de base, de Secret et application des manifestes sont des actes de
l'exploitant (et toute dépense une action `costly`, décision 0011).

1. **Image.** `docker build -t <registre>/ameesh-runner:<version> .`, avec
   `--build-arg HARNESS_NPM=<paquet@version>` pour un harnais distribué par
   npm, ou une image dérivée (`FROM` + `COPY` du binaire) pour un autre
   (en-tête du [`Dockerfile`](../../Dockerfile)). Poussée dans **votre**
   registre ; reporter le nom dans `images:` de `kustomization.yaml`.
2. **Base.** Un Postgres durable (service géré, ou instance dédiée avec
   volume et sauvegardes). Un rôle pour ameesh, propriétaire de son schéma
   (l'init `migrate` applique les migrations — risque et suite prévue en
   section 9). DSN **sans mot de passe** dans
   `config.json` ; le mot de passe dans le Secret `ameesh-db`.
3. **Canon.** Fiches `Host` (nom = nom du pod), `Agent` (capacités `read`,
   `propose`) et `Placement` (`cwd` sous `/home/ameesh`), fusionnées par PR.
   Clé de déploiement en lecture sur le dépôt du canon, `known_hosts` vérifié
   hors bande.
4. **Secrets.** Les trois Secret de
   [`secrets.exemple.yaml`](../../deploy/k8s/secrets.exemple.yaml), créés hors
   de tout dépôt (`kubectl create secret …` ou gestionnaire de secrets).
5. **Rendu puis application.** `kubectl kustomize deploy/k8s` (relire),
   puis `kubectl apply -k deploy/k8s`.
6. **Harnais.** Au premier démarrage, le HOME est vide : la configuration
   des hooks du harnais (`agent-mail hook <harnais>`, comme sur un poste) et
   le dépôt de travail (`cwd` du Placement) sont à mettre en place une fois,
   par `kubectl exec` ou par une image dérivée. Ils restent ensuite sur le
   volume.
7. **Contrôle.** `kubectl logs ameesh-agent-0 -c runner` (« bail acquis »),
   `kubectl exec ameesh-agent-0 -c runner -- ameesh canon check`, `ameesh
   list`, `ameesh doctor --notify-test`.

## 4. Superviseur externe en lecture seule

Le superviseur peut appartenir à une **autre organisation** que l'équipe des
agents. Par défaut, il voit donc l'**état** du mesh, pas le **travail**
lui-même : deux rôles distincts, tous deux `NOLOGIN`, en lecture seule.

* [`deploy/sql/role-superviseur.sql`](../../deploy/sql/role-superviseur.sql)
  — rôle de base `ameesh_superviseur` : registre et vue `agent_mesh_overview`
  (statut, bail, non-lus, budget, placement), métadonnées de la boîte
  (expéditeur, destinataire, type, statut, dates), lots (titre, état,
  assigné), index des fils (sans extrait), actions et tentatives (classe,
  état, cible, sans arguments ni notes), coûts, état du canon, migrations.
* [`deploy/sql/role-superviseur-contenus.sql`](../../deploy/sql/role-superviseur-contenus.sql)
  — rôle `ameesh_superviseur_contenus`, **accordé explicitement en plus** à
  un superviseur habilité à lire le travail : corps, charges et métadonnées
  des messages (`agent_mailbox.body`, `payload`, `meta`), consignes
  (`pending_prompt`, `current_prompt`), brief de redémarrage en attente
  (`restart_brief`, L26), extraits de fil
  (`thread_index.last_excerpt`), arguments et notes d'actions (`actions.args`,
  `last_note`, `action_events.note`), descriptions et notes de lots
  (`work_items.body`, `work_item_events.note`, `work_item_milestones.note`), métadonnées d'approbations
  (`mesh_approvals.meta`). Seul, il ne donne que ces colonnes.

Aucun des deux n'a de droit d'écriture, de séquence, ni d'accès aux colonnes
de signature, de reçu, de nonce, de défi, aux clés publiques ni aux
identifiants d'authentificateurs ; la liste exacte des exclusions et leur
raison sont en tête de `role-superviseur.sql`. Restent lisibles par le rôle de
base, comme état : titres de lots, cibles d'actions et diagnostics courts
(`status_text`, `last_error`, `placement_diagnostic`…), qui peuvent citer un
extrait d'erreur de harnais — à retirer du script si ce n'est pas acceptable.

```bash
export PGOPTIONS='-c search_path=public'
psql "<DSN administrateur>" -v ON_ERROR_STOP=1 -f deploy/sql/role-superviseur.sql
psql "<DSN administrateur>" -v ON_ERROR_STOP=1 -f deploy/sql/role-superviseur-contenus.sql
# puis un rôle de connexion par superviseur :
#   CREATE ROLE supervision_outil LOGIN IN ROLE ameesh_superviseur;
#   ALTER ROLE supervision_outil SET default_transaction_read_only = on;
# et, seulement pour un superviseur habilité aux contenus :
#   GRANT ameesh_superviseur_contenus TO supervision_outil;
```

Chaque script ouvre **sa propre transaction** et garantit son résultat, pas
seulement ses propres ordres. Il crée le rôle s'il manque, retire les droits
que ce rôle a reçus directement, et accorde la lecture colonne par colonne de
son contrat (liste explicite dans le script). Puis il **audite les droits
effectifs** du rôle (`has_column_privilege`, `has_table_privilege`…) : ceux
reçus directement, mais aussi de `PUBLIC`, des rôles dont il hérite et des
rôles prédéfinis (`pg_read_all_data`…). Il audite aussi les ACL par défaut du
schéma (`ALTER DEFAULT PRIVILEGES`), qui ouvriraient une future table ou
colonne à `PUBLIC` ou au rôle. Enfin, il vérifie l'absence d'écriture, de
séquence, de création dans le schéma, de fonction `SECURITY DEFINER`
exécutable et d'attribut privilégié hérité.

**Un rôle superviseur n'est membre d'aucun rôle.** Il ne peut appartenir ni à
un rôle parent, ni à un rôle prédéfini, ni à l'autre rôle superviseur. Même
sans héritage (`INHERIT FALSE`), une appartenance permettrait à un login
membre du superviseur de faire `SET ROLE` vers ce rôle et de lire ce que
l'audit des droits effectifs ne voit pas. C'est aussi le cas pour toute
appartenance avant PostgreSQL 16. Toute appartenance est donc refusée. Le
sens inverse est le seul prévu : les rôles de connexion sont membres des rôles
superviseurs. L'administrateur ne les rend membres d'aucun autre rôle ; ce
point relève de son exploitation, le script ne crée pas ces rôles et ne
les audite pas.

Si le contrat n'est pas tenu, le script **refuse** : il lève une erreur qui
liste chaque écart, et la transaction est annulée (aucun rôle créé, aucun
droit changé). Il ne tente jamais de réparer des droits accordés par
d'autres, à `PUBLIC` ou à un rôle parent : l'administrateur les retire à la
source, puis relance. Sous PostgreSQL ≤ 14, cela inclut le `CREATE` accordé à
`PUBLIC` sur le schéma `public`.

Les scripts sont idempotents : les relancer **après chaque mise à jour**
d'ameesh. Une colonne ajoutée par une migration n'est lisible qu'une fois
nommée dans un contrat, et une colonne du contrat absente de la base (base en
retard de migrations) fait échouer le script. Le test `tests/test_cluster.py`
les applique sur un Postgres réel. Il échoue si une colonne du schéma n'est ni
au contrat du rôle de base ni explicitement exclue, si le rôle de base lit un
contenu, ou si le rôle « contenus » lit autre chose que les contenus. Il
vérifie aussi le refus transactionnel dans trois cas : droits accordés à
`PUBLIC`, droit hérité d'un rôle parent, ACL par défaut — et deux cas
d'appartenance : rôle parent atteignable par `SET ROLE` sans héritage, rôle
prédéfini `pg_read_all_data`.

## 5. Sauvegardes

* **Base** : la sauvegarde du service géré, ou un `pg_dump` quotidien copié
  **hors du cluster** (0011 : sauvegardée hors de la machine dès qu'on
  quitte le profil local). Exercice de restauration obligatoire, comme à
  l'étape 1 de [`BASCULE.md`](../BASCULE.md) : restaurer dans une base
  jetable, `ameesh doctor --probe` dessus, supprimer.
* **Volume `home`** : instantanés de volume (VolumeSnapshot, si la classe de
  stockage le permet) ou copie périodique. Il porte les sessions du harnais
  et les **fils lisibles** ; leur index est en base, leur texte sur le volume.
  La PVC survit à la suppression du StatefulSet (politique par défaut de
  Kubernetes) : la supprimer est un acte explicite.
* **Avant toute mise à jour** : `pg_dump` (les migrations ne reviennent pas
  en arrière).

## 6. Identifiants des harnais

* **Clés d'API** : dans le Secret `ameesh-harness`, injectées dans
  l'environnement de l'exécuteur et héritées par le harnais. Elles sont
  facturées à l'usage : le plafond horaire d'ameesh
  (`AMEESH_BUDGET_USD_PER_HOUR`, décision 0019) et celui du fournisseur
  s'appliquent. Le harnais (donc l'agent) peut les lire.
* **Abonnements** (connexion interactive, `credential_mode: subscription`) :
  leur usage sur un serveur, sans humain devant, et partagé par un agent
  autonome, **est à vérifier contre les conditions de chaque fournisseur**
  avant tout déploiement. Ce profil ne tranche pas ; la politique de l'`Host`
  (`credential_modes`) peut simplement ne l'admettre pas.
* Aucun identifiant n'est dans l'image, le canon ni la base (spec §3).

## 7. Mise à jour

1. `pg_dump` de la base (section 5).
2. Nouvelle image, nouvelle étiquette (jamais réutilisée), `newTag` dans
   `kustomization.yaml`, `kubectl kustomize` relu, `kubectl apply -k`.
3. Le StatefulSet remplace le pod : SIGTERM, bail rendu ; le nouveau pod
   applique les migrations (init `migrate`, sous verrou consultatif), clone
   le canon et le synchronise, puis l'exécuteur reprend l'agent sur la **même
   session** (le HOME est conservé).
4. `ameesh doctor --probe` (sonde prête), `ameesh list`, puis relancer
   `role-superviseur.sql`.

## 8. Retour arrière

* **Image** : remettre l'ancienne étiquette et réappliquer (ou `kubectl
  rollout undo statefulset/ameesh-agent`). Si la nouvelle version a appliqué
  des migrations, l'ancien code tourne sur un schéma plus récent : la sonde
  reste prête et le signale (« base plus récente que ce code ») ; la
  compatibilité n'est **pas** garantie. En cas de doute : restaurer le
  `pg_dump` pris avant la mise à jour (l'état écrit depuis est perdu).
* **Mettre l'agent hors service** : `kubectl scale statefulset/ameesh-agent
  --replicas=0` (bail rendu, harnais arrêté avec son groupe) ; ou
  `ameesh run stop <agent>` — `canon sync` ne lève jamais cet arrêt.
* **Revenir au profil local** : arrêter le pod comme ci-dessus, vérifier que
  le bail est rendu (`ameesh show <agent>`), déplacer le `Placement` de
  l'agent vers l'hôte local par PR du canon, `ameesh canon sync` sur l'hôte
  local. Les sessions et fils restés sur le volume se récupèrent par
  `kubectl cp` si besoin ; une session de harnais n'est pas forcément
  portable d'une machine à l'autre.
* **Tout retirer** : `kubectl delete -k deploy/k8s` ; la PVC et les Secret
  restent jusqu'à leur suppression explicite.

## 9. Limites et questions ouvertes

* Rôle Postgres **par agent** (ou par pod) pour isoler l'état des agents
  entre eux : absent en v1 (toutes les écritures passent par le rôle
  d'ameesh).
* **Migrations par l'init `migrate`, avec le rôle de l'exécuteur** (choix
  provisoire). Risque : ce rôle doit avoir les droits DDL sur le schéma
  (propriétaire), et le harnais, qui hérite de `PGPASSWORD`, les a donc
  aussi — il peut modifier ou supprimer tables, déclencheurs et fonctions
  d'ameesh, y compris les gardes de la porte (`ameesh_actions_guard`…).
  **Suite prévue** : un rôle propriétaire du schéma, distinct, utilisé
  seulement par un Job de migration lancé par l'exploitant avant la mise à
  jour ; l'exécuteur reçoit un rôle sans DDL (DML sur ses tables), et l'init
  `migrate` disparaît au profit de la sonde (`doctor --probe` refuse un schéma
  en retard).
* `ameesh-approve` dans un cluster (autre espace de noms, autre compte de
  service, exposition par la page Nexlink, décision 0017) : hors de ce profil.
* Préparation du HOME (hooks du harnais, dépôt de travail) : à la main ou
  par image dérivée ; un init dédié pourrait le faire.
* Fédération de canons à plusieurs dépôts : non couverte par l'init `canon`.
* Pas de `livenessProbe` : un exécuteur bloqué sans mourir n'est pas détecté
  par Kubernetes (le bail, lui, expire et l'agent n'est plus servi).
