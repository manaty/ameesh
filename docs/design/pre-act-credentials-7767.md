---
type: Design
title: "Voie imposée des identifiants KMS/IAM et GitHub prod (tranche 2 du pré-geste)"
description: "Étude et mécanisme #7767 : le courtier d'identifiants à compte Unix dédié, le contrôle pre-act exécuté par le courtier, la liste de commandes admises, le reçu injecté au dispatch et la preuve pre-act/2 ; migration, droits et décision owner."
status: draft
tags: [securite, kms, iam, github, production, pre-geste, courtier, identifiants, droits, auditeur]
generated: { by: "mty-amh-impl-02/deepseek", at: "2026-11-18T15:30:00Z" }
sources:
  - { resource: "manaty/ameesh#5978", title: "Voie imposée aux identifiants KMS et production (lot #7767)" }
  - { resource: "manaty/ameesh#5971", title: "Incident MP-2 du 18/11 : STOP owner franchi par un acte KMS temporisé (lot #7761)" }
  - { resource: "manaty/nexlink#3455", title: "MP-2 prod, acte opérateur réel (lot #5772)" }
  - { resource: "manaty/ameesh#4765", title: "Workflows prod Nexlink : entrée pregeste (lot #7768)" }
  - { resource: "docs/design/pre-act-7761.md", title: "Pré-geste, tranche 1 : le reçu et son audit" }
  - { resource: "docs/runbooks/mp-2-at-rest.md", title: "Clé KMS nexlink-messages, import, IAM runtime (dépôt Nexlink)" }
  - { resource: "docs/COMPTES.md", title: "Magasins gcloud/gh, empreintes et sentinelle" }
  - { resource: "docs/ops/bureau-goal-cli.md", title: "Connexion PostgreSQL limitée du bureau (#6881)" }
  - { resource: "deploy/sql/role-bureau.sql", title: "Droits réels du rôle bureau_goal" }
---

# Voie imposée des identifiants KMS/IAM et GitHub prod (#7767)

**Lot #7767**, seconde tranche du pré-geste #7761, décidée par l'orchestrateur
le 18/11 (#225799) et créée par `ameesh-cx1`. Cette note qualifie les accès,
les chemins directs, les risques et la migration. Le mécanisme est livré dans
`src/ameesh/credential_gate.py`, `deploy/gate/` et ses tests ; **aucun droit
n'est modifié et aucun identifiant n'est déplacé par ce lot**. L'application
sur `bureau-goal` est une décision owner (section 7), hors de ce lot.

## 0. Recommandation

1. **Un compte Unix dédié** (`ameesh-gate`) détient seul les magasins
   `gcloud` et `gh` de production ; le compte du bureau n'y a plus accès.
2. **Un courtier** (`ameesh gate broker serve`) tourne sous ce compte et
   n'expose qu'une socket Unix au groupe `ameesh-gate-client` (le bureau).
   Chaque demande est vérifiée (`SO_PEERCRED`, liste de commandes admises,
   cible et branche lues au canon), puis le courtier exécute lui-même le
   pré-geste (`pre_act.inspect`) et la commande, avec son propre environnement.
   Aucun identifiant ne traverse la socket.
3. **Le dispatch prod** est un `gh workflow run` dont le courtier impose
   `--repo`, `--ref` (branche de production du canon) et le workflow déclaré ;
   il ajoute lui-même `-f pregeste=<reçu>`, que l'appelant ne peut pas fournir.
   #7768 fournit l'entrée et le `run-name` côté workflows.
4. **La preuve est sur le lot** : le reçu `pre-act/1` existant, puis deux
   événements `pre-act/2` (début, fin) avec l'empreinte de la commande, le
   résultat et la durée, jamais un secret. `ameesh gate audit` signale un
   début sans fin, une fin sans début ou un doublon.
5. **Décision owner avant toute application** : créer l'identité de service
   KMS et l'identité GitHub du courtier, déplacer les identifiants, retirer
   les droits KMS du compte du bureau et le scope `workflow` de ses jetons.
   Sans cette décision, le mécanisme reste inerte : les identifiants actuels
   permettent toujours un appel direct.

## 1. Périmètre et méthode

### 1.1 Dans le périmètre

- Les identifiants qui permettent un acte **KMS/IAM** (création d'import job,
  clé, version, `set-primary`, lecture IAM) et un acte **GitHub de
  production** (dispatch d'un workflow prod, écriture de secrets ou
  d'environnements, modification de workflow).
- Le chemin qui va de l'agent qui demande l'acte jusqu'à l'effet, y compris
  une attente programmée (le cas de l'incident : `sleep 02:00 ; p2a.sh`).
- Les preuves et l'audit côté mesh, et les contrôles IAM/GitHub qui rendent le
  contournement impossible.

### 1.2 Hors périmètre

- L'application des droits (création d'identités, révocation, copie de
  magasins) : décision owner, lot d'application séparé.
- Les autres secrets (DSN, clés d'API des harnais, secrets applicatifs) : ils
  ont leurs propres voies (#6881, L232c).
- La production réelle Nexlink : aucun dispatch, aucune création de clé,
  aucune migration n'est exécutée par ce lot.
- Le cas d'un agent root ou d'une compromission du noyau : hors modèle.

### 1.3 Ce qui est vérifié, et ce qui reste à vérifier sur `bureau-goal`

Vérifié dans le dépôt, le canon ou l'hôte `ameesh-agents-1` :

- `gh` y est connecté au compte **`smichea`** avec les scopes
  `gist, read:org, repo, workflow` (`gh auth status`, 18/11). Le jeton est
  dans `~/.config/gh/hosts.yml` (0600) ; c'est un identifiant **owner** avec
  le droit de dispatcher un workflow et de modifier un workflow ou un secret.
- `gcloud` n'est pas installé sur `ameesh-agents-1` ; il l'est sur
  `bureau-goal` (sentinelle L226a, `docs/COMPTES.md` §6). Le magasin est
  `~/.config/gcloud` (0700), `credentials.db` et l'ADC en 0600, sous le
  compte Unix du bureau.
- Le canon §10 réserve au bureau les gestes de production ; le canon §14/§15
  fixe la cible prod Nexlink (`main`, `environment: production`, verrou
  `prod-deploy`, acteurs déclarés).
- Le runbook MP-2 (`docs/runbooks/mp-2-at-rest.md`) nomme la clé
  `nexlink-messages`, l'import job et l'IAM runtime :
  `roles/cloudkms.cryptoKeyEncrypterDecrypter` sur la clé seulement.
- `#7761` ne corrèle que les runs GitHub dont le titre porte
  `pregeste=<UUID>` ; un acte `gcloud` direct n'a aucune trace dans le mesh.
- La sentinelle ne lit que des noms, chemins et dates ; elle ne donne pas
  l'identité IAM effective ni les scopes du jeton.

À vérifier au lot d'application, en lecture seule, sur `bureau-goal` :

- l'identité `gcloud` effective (`gcloud auth list`), ses rôles IAM sur le
  projet et la keyring, et les fichiers réellement lus (`~/.config/gcloud`,
  `GOOGLE_APPLICATION_CREDENTIALS`, `CLOUDSDK_AUTH_ACCESS_TOKEN`) ;
- l'identité `gh`, ses scopes et la variable `PAGE_SERVER_PROD_ACTORS` des
  workflows prod ;
- les scripts réels (`p2a.sh`, `scripts/migrate.sh`) et leurs appels `gcloud`,
  `gh`, `psql` ;
- la présence éventuelle de `sudo`/root sur `bureau-goal` : si le compte du
  bureau peut devenir root, il peut lire le magasin du compte `ameesh-gate` ;
  la séparation ne vaut que si le bureau n'a pas root et si le magasin est
  0600 `ameesh-gate`. C'est une condition de la décision, pas un constat.

## 2. Accès actuels et chemins directs

### 2.1 Table des accès

| Identifiant | Où il vit | Qui peut l'utiliser | Ce qu'il permet | Garde actuelle |
|---|---|---|---|---|
| Jeton GitHub `smichea` (`gh`) | `~/.config/gh/hosts.yml` 0600 (ou `GH_TOKEN`) | tout processus du compte Unix de l'agent, et son shell | `repo` + `workflow` : dispatch prod, lecture/écriture de dépôt, secrets si l'API l'admet | aucune côté mesh ; `environment: production`, `PAGE_SERVER_PROD_ACTORS` et le verrou `prod-deploy` côté GitHub |
| Login `gcloud` (compte utilisateur ou ADC) | `~/.config/gcloud` 0700, `credentials.db`/ADC 0600 | tout processus du compte du bureau | selon IAM : KMS admin sur la keyring, lecture IAM, éventuellement d'autres projets | aucune côté mesh ; Cloud Audit Logs si activés |
| Clé de service / identité runtime Nexlink | secrets de déploiement, SA `nexlink-runtime` | services Cloud Run, workflows CI | `cryptoKeyEncrypterDecrypter` sur la clé | IAM ; pas de dispatch manuel |
| Identité owner (Google/GitHub) | appareils de l'owner, navigateur | l'owner | tout | 2FA/navigateur ; hors agents en principe |
| Rôle PostgreSQL `bureau_goal` | `.pgpass` 0600 sur `bureau-goal` | compte du bureau | lecture des lots, prise de geste, courrier, reçu de pré-geste | garde 0567, liste de colonnes #6881 |

### 2.2 Chemins directs constatés

1. **`gcloud` direct.** Un shell du bureau peut appeler `gcloud kms …` sans
   passer par `ameesh gate pre-act`. C'est exactement l'incident du 18/11 :
   la commande temporisée a été lancée à 02:00:14Z, après le STOP owner de
   01:39Z, sans relecture ni reçu. Le reçu #7761 n'aurait pas suffi : le
   chemin `gcloud` restait ouvert.
2. **`gh` direct.** Le jeton `smichea` porte le scope `workflow` ; un agent
   peut dispatcher un workflow prod ou modifier un workflow, sans reçu. Les
   gardes GitHub (environnement, acteurs, concurrence) réduisent l'effet mais
   ne sont pas la garde de pré-geste.
3. **Écriture de secrets / workflow.** Le même jeton `repo` peut modifier un
   fichier de workflow sur une branche, pousser, ou écrire un secret si
   l'API l'admet. Un workflow modifié sur une branche ne devrait pas atteindre
   l'environnement `production` sans revue, mais le chemin n'est pas fermé par
   le mesh.
4. **Attente programmée.** Une commande unique qui attend puis agit ne relit
   ni fil ni courrier avant l'effet. `ameesh gate pre-act --wait-until` corrige
   la lecture, mais seulement si l'acte passe par la garde.
5. **Audit.** Le collecteur #7761 ne voit que les runs GitHub marqués ; un
   acte `gcloud` direct ne laisse aucune preuve dans le mesh. Les Cloud Audit
   Logs sont la seule trace, et ils ne sont pas corrélés à un lot.

### 2.3 Risques

- **Franchissement d'un STOP** : un acte KMS/IAM ou prod peut partir après un
  STOP owner, un hold ou un courrier non lu (incident #5772).
- **Identifiant owner sur les hôtes** : un jeton `smichea` (`workflow`,
  `repo`) sur un hôte d'agent donne à tout processus de cet hôte les droits de
  l'owner. Une fuite de `hosts.yml` ou une commande malveillante les emporte.
- **Pas de preuve d'acte** : impossibilité de dire, depuis le mesh, quel lot a
  créé un import job, une version de clé ou dispatché un workflow.
- **Élargissement silencieux** : une commande `gcloud` peut agir sur un autre
  projet ou une autre keyring si l'identité IAM le permet ; rien ne borne la
  cible.
- **Jetons de longue durée** : `gh` OAuth et `gcloud` utilisateur sont des
  identifiants durables ; une révocation est un geste owner.

## 3. Voie imposée

### 3.1 Principe

Le contrôle n'est plus une commande que l'agent peut oublier : il est dans le
seul processus qui a le droit d'agir. Le compte `ameesh-gate` détient les
identifiants ; le compte du bureau n'a qu'une socket.

```
bureau (ou un script du bureau)
  └─ ameesh gate act --lot N --kind kms|key|prod [--wait-until T] -- gcloud … | gh …
       └─ socket Unix /run/ameesh-gate/broker.sock (groupe ameesh-gate-client)
            └─ ameesh gate broker serve (compte ameesh-gate)
                 ├─ SO_PEERCRED : uid/gid appelant
                 ├─ règle admise : exe + préfixe d'argv, flags fixes, valeurs admises
                 ├─ cible et branche lues au canon appliqué (prod)
                 ├─ pre_act.inspect : fil, courrier, holds, décisions retirées
                 ├─ pre-act/2 début (empreinte de la commande, cible)
                 ├─ exécution (env du compte gate, sans shell)
                 └─ pre-act/2 fin (code, durée, empreintes de sortie)
```

### 3.2 Ce que le courtier refuse

- Un appelant hors des uid/groupes déclarés (`SO_PEERCRED`).
- Un schéma, un lot, un type ou un `request_id` invalide.
- Une commande qui n'est pas exactement `exe` + préfixe d'une règle.
- Un flag qui imprime ou détourne un identifiant (`--log-http`,
  `--verbosity=debug`, `--access-token-file`, `--impersonate-service-account`,
  `--credential-file-override`), et `--flags-file` partout : gcloud lit ce
  fichier après le courtier et y trouverait les flags interdits ou les flags
  fixes (revue B1 du 18/11).
- Tout flag non déclaré par la règle : seuls `fixed`, `allow`, `required` et
  la liste explicite `free` sont admis ; un flag inconnu, une abréviation
  (`--impersonate-service`) ou un flag court (`-q`) est refusé. C'est le seul
  modèle sûr face aux flags globaux de gcloud (`--configuration`, `--account`,
  `--billing-project`).
- Un flag fixe avec une autre valeur (projet, région), une valeur hors liste
  (keyring, clé), un flag requis absent, une cible positionnelle hors liste
  (`targets` : le nom de keyring ou de clé est alors exact).
- Pour `prod` : un dépôt, une branche ou un workflow non déclarés au canon,
  un flag inconnu, une entrée `pregeste` fournie par l'appelant, une entrée
  non admise par la règle, une cible qui ne correspond pas au dispatch.
- Un reçu créé il y a plus de 30 s, un lot `blocked`/`waiting_human`, un STOP
  ou un hold actif, un courrier non lu, une décision retirée.
- Une attente plus longue que `max_wait_seconds`, une base non
  transactionnelle, un canon illisible.

### 3.3 Ce que le courtier n'expose jamais

- Aucun secret ne traverse la socket : le courtier ne lit pas la valeur des
  identifiants, il pointe `gcloud`/`gh` vers les répertoires 0700 du compte
  gate. Seule la sortie de la commande revient, bornée et sans secret connu.
- L'empreinte de la commande et ses quatre premiers arguments seulement sont
  consignés (`argv_sha256`, `argv_head`) : le journal ne reçoit pas la ligne
  complète, qui peut nommer un fichier.
- Les messages d'erreur sont des catégories stables, jamais une valeur.

### 3.4 Preuves et audit

Le lot reçoit, dans l'ordre :

1. `pre-act/1` : le reçu du contrôle (type, lot, acteur, cible, `pregeste_id`,
   `request_id`), écrit dans la même transaction que la lecture du fil ;
2. `pre-act/2 phase=start` : la commande admise, l'empreinte, la cible, le
   début ;
3. `pre-act/2 phase=end` : le code, la durée, les empreintes de sortie.

`ameesh gate audit --since <ISO>` relit ces événements et signale les débuts
sans fin après le délai, les fins sans début et les doublons. Un début sans
fin est un acte interrompu ou non consigné : l'auditeur peut le rapprocher des
Cloud Audit Logs (KMS/IAM) et des runs GitHub. Le contrôle est **détectif**
côté mesh ; la barrière **préventive** est l'IAM et le jeton GitHub, qui ne
sont plus dans les mains de l'agent.

## 4. Migration

Chaque étape est séparée, réversible et laisse une preuve publique (sans
secret) au lot d'application.

### 4.1 Étape 0 — code (ce lot)

Fusionner `src/ameesh/credential_gate.py`, la commande `ameesh gate act`,
`ameesh gate broker`, `ameesh gate audit`, `deploy/gate/` et les tests.
Aucune modification de droit, aucun déploiement. Les tests ciblés couvrent le
refus d'un STOP pendant l'attente, l'injection du reçu, la cible prod, les
flags interdits et l'audit.

### 4.2 Étape 1 — compte et service sur la cible

Sur l'hôte qui exécute les actes (aujourd'hui `bureau-goal`, à confirmer) :

1. créer `ameesh-gate` (compte système, sans login) et le groupe
   `ameesh-gate-client` ; y ajouter le compte du bureau seulement ;
2. lancer `deploy/gate/install.sh --client bureau --pgpass …` : il crée le
   compte, le venv/snapshot `ameesh`, les répertoires privés et l'unité
   `deploy/gate/ameesh-gate.service` ;
3. écrire `/etc/ameesh-gate/config.json` (DSN sans mot de passe, `.pgpass`
   gate 0600, canon épinglé) et `/etc/ameesh-gate/broker.json` (règles,
   chemins) en root:ameesh-gate 0640 ;
4. `deploy/gate/install.sh --check --live` (ou `ameesh gate broker check`)
   doit lister les règles, le commit canon et la base ; il refuse un
   exécutable absent, un répertoire d'identifiants lisible par le groupe, un
   canon illisible ou une base distante.

### 4.3 Étape 2 — créer les identités du courtier (owner)

1. **KMS/IAM** : créer ou dédier un compte de service `ameesh-gate-kms`,
   lui donner KMS admin **sur la seule keyring** `nexlink-messages` (et, si
   nécessaire, la lecture IAM du projet), pas l'administration IAM globale.
   Activer les Data Access audit logs sur la keyring. Déposer la clé 0600
   dans le magasin du compte gate et l'activer (`gcloud auth
   activate-service-account --key-file …`). Ne jamais copier la clé dans le
   dépôt ni dans un courrier.
2. **GitHub prod** : créer une identité machine dédiée (jeton fin grained
   limité au dépôt `manaty/nexlink`, permission `Actions: write` et
   `Contents: read`, ou GitHub App équivalente), l'installer dans le magasin
   `gh` du compte gate, et l'ajouter à `PAGE_SERVER_PROD_ACTORS` (ou à la
   liste d'acteurs que les workflows prod lisent). L'identité du bureau et
   `smichea` n'ont plus à y figurer pour le dispatch.
3. Le compte gate ne doit avoir ni `sudo`, ni accès root, ni accès aux
   répertoires des agents ; le compte du bureau ne doit pas pouvoir lire
   `/var/lib/ameesh-gate` (0700) ni `/etc/ameesh-gate` (0750).

### 4.4 Étape 3 — vérifier la voie

1. Choisir un lot de contrôle réservé au bureau (`external-executor bureau`).
2. Un acte **lecture seule** admis (`gcloud kms keyrings describe` ou
   `gcloud kms keys list`, règle dédiée) : le courtier écrit le reçu et les
   deux preuves, la sortie arrive au bureau.
3. Un acte **sans** `pre-act` propre (lot bloqué, STOP simulé, courrier non
   lu) : refus, aucune exécution, aucune preuve `start`.
4. Un dispatch prod de contrôle sur un workflow déclaré, avec un `pregeste`
   injecté : le run porte le bon identifiant dans son titre ; le collecteur
   #7761 le corrèle ; un second dispatch avec le même reçu est refusé.
5. `ameesh gate audit --since …` : aucune anomalie sur ces actes.

### 4.5 Étape 4 — retirer les chemins directs

1. Sur `bureau-goal`, après escrow 0600 hors ligne : `gcloud auth revoke` et
   retrait du magasin `~/.config/gcloud` du compte bureau ; vérifier qu'un
   `gcloud kms …` direct échoue.
2. Retirer au compte du bureau le scope `workflow` (et, si possible, passer
   les agents à un jeton d'application sans `actions:write`). Vérifier qu'un
   `gh workflow run` direct est refusé par l'acteur ou l'environnement.
3. Côté IAM, retirer les rôles KMS du compte utilisateur du bureau ; garder
   l'owner. Côté GitHub, retirer les acteurs non courtier de la variable prod
   si la décision le prévoit.
4. Rejouer les contrôles de l'étape 3 et un acte réel sur décision owner.

### 4.6 Retour arrière

Conserver, hors ligne et 0600, une copie des magasins et des politiques IAM
avant révocation, avec une date. Pour revenir : arrêter le courtier, restaurer
le magasin du bureau, redonner les rôles IAM (owner), remettre les acteurs
GitHub, vérifier par un acte de contrôle. Un retour arrière est un geste
owner ; il ne se fait pas sous un STOP.

## 5. Limites et risques résiduels

- **Le compte gate est le nouveau point de rupture.** Il n'est pas root, mais
  une compromission de ce compte donne tous les actes admis. Le garder sans
  login, sans shell, avec un magasin 0700 et une unité systemd durcie.
- **La politique est une frontière de sécurité.** Une règle trop large
  (`gcloud kms` entier, `gh api`) rouvre le contournement. Tout flag non
  déclaré est refusé, et `--flags-file` est interdit globalement : une liste
  blanche de flags est le seul modèle sûr (B1 de la revue). Un sous-commandement
  à argument positionnel sans `targets` laisse libre le nom de la keyring ou
  de la clé : la politique d'exemple n'en contient aucun. La revue de la
  politique est aussi importante que celle du code ; les règles du dépôt sont
  un exemple, la politique réelle est root-owned et datée.
- **Le lot cité n'est pas lié à la cible (N1 de la revue).** L'appelant choisit
  le lot ; le courtier relit les holds et STOP de ce lot, pas ceux d'un autre.
  Citer un lot propre pour un acte destiné à un autre lot passe la garde et
  n'est vu qu'après coup par l'audit. Correctif de conception à décider : lier
  la règle ou la cible à un lot autorisé (décision portée par le lot, par
  exemple). En attendant, la revue de l'audit rapproche les preuves
  `pre-act/2` des actes réels.
- **Attente longue et threads (N3 de la revue).** `max_wait_seconds` vaut
  86400 et chaque demande en attente tient un thread, sans plafond par
  appelant. Un plafond de demandes simultanées est à ajouter avant un usage
  large ; aujourd'hui le seul client est le bureau.
- **Pas de limite de débit par lot** : plusieurs actes admis restent possibles
  après un contrôle propre. Le frein est la rareté des lots de production, pas
  le courtier.
- **Installeur et source root (N2 de la revue).** Sans `--wheel`, l'installeur
  refuse un arbre source non root-owned ou writable par le groupe, car pip
  exécute son backend en root. La roue est la voie recommandée.
- **Le courtier n'est pas HA** : s'il tombe, les actes KMS/prod sont bloqués
  (fail-closed). Le retour à la main de l'owner est une décision, pas un
  automatisme.
- **Les journaux cloud restent nécessaires** : le mesh voit les actes du
  courtier, pas une utilisation directe d'un identifiant qui aurait échappé à
  la migration. Les Data Access audit logs et la revue IAM sont la barrière
  détective complémentaire.
- **GitHub** : un jeton owner reste un contournement tant que l'étape 4.5
  n'est pas faite. Les protections d'environnement et la revue de branche sont
  la seule barrière avant.
- **Le premier jet n'inclut pas la création de jetons GitHub App** : le
  magasin `gh` du compte gate peut porter un jeton fin grained ; l'App est une
  amélioration (rotation automatique) qui ne change pas le contrat du courtier.
- **Pas de signature des reçus** : le reçu est en base, écrit par le rôle
  limité ; un acteur qui aurait un DSN complet pourrait écrire un faux reçu.
  La décision doit vérifier que le courtier n'utilise que `bureau_goal`.

## 6. Vérifications livrées par ce lot

- `tests/test_7767_credential_gate.py` : politique stricte, commandes
  admises/refusées, flags interdits dont `--flags-file`, refus des flags
  inconnus/courts/abrégés, fixed/allow/required/free, cibles positionnelles,
  dispatch prod (dépôt, branche, workflow, entrées, `pregeste`), courtier
  (STOP pendant l'attente, reçu, preuves, doublon, base distante, échec
  d'exécution), audit (début sans fin, fin sans début, doublon), socket
  aller-retour.
- `tests/test_7761_pre_act.py`, `tests/test_7761_pre_act_audit.py` et les
  contrats existants restent verts (le reçu `pre-act/1` gagne seulement le
  champ optionnel `request_id`).
- `deploy/gate/install.sh --check` : modes, propriétaires, répertoires
  d'identifiants privés, JSON, chargement de la politique par le compte gate ;
  `--live` ajoute `ameesh gate broker check` (canon, base). Sans `--wheel`,
  l'installeur refuse un arbre source non root-owned ou writable par le groupe
  (revue N2), car pip exécute son backend en root.
- `bash -n deploy/gate/install.sh` : l'installeur n'est pas exécuté par les
  tests (il crée un compte système) ; son exécution appartient à l'étape 1 de
  la migration, après décision owner.
- Aucun test ne touche un vrai `gcloud`/`gh`, un vrai secret ou un vrai
  identifiant ; le compte gate n'est pas créé par les tests.

## 7. Décision owner demandée

Une demande `ameesh decide` est déposée sur le lot d'application, avec la
question suivante (options) :

**« Appliquer la voie imposée des identifiants KMS/IAM et GitHub prod sur
l'hôte des actes (bureau-goal) ? »**

- **A (recommandée)** — créer le compte `ameesh-gate` et le courtier, déplacer
  le login `gcloud` vers un compte de service KMS dédié (keyring
  `nexlink-messages` seulement), créer l'identité GitHub machine
  (`Actions: write` sur `manaty/nexlink` seulement), vérifier les actes de
  contrôle, puis retirer le login `gcloud` du bureau et le scope `workflow`
  de ses jetons. Retour arrière par escrow.
- **B** — courtier pour KMS seulement ; GitHub prod reste sous les gardes
  GitHub actuelles. Plus court, mais le jeton owner `workflow` reste un
  contournement.
- **C** — ne rien appliquer ; garder `ameesh gate pre-act` comme garde
  volontaire. L'incident #5772 reste reproductible par un appel direct.

La décision doit nommer : l'hôte, le compte de service KMS, la keyring, le
dépôt GitHub, l'identité machine, la fenêtre, le responsable de l'installation
et la destination de l'escrow. Aucune de ces valeurs n'est dans le code.

## 8. Références

- `src/ameesh/credential_gate.py` — courtier, client, politique, audit.
- `src/ameesh/pre_act.py` — `inspect` (`pre-act/1`) et preuves `pre-act/2`.
- `deploy/gate/` — unité systemd, politique et configuration d'exemple,
  installeur root.
- `docs/design/pre-act-7761.md` — tranche 1 et limites assumées.
- `docs/runbooks/mp-2-at-rest.md` (Nexlink) — keyring, import, IAM runtime.
- `docs/COMPTES.md` §6 — magasins `gcloud`/`gh`, empreintes, sentinelle.
- `docs/ops/bureau-goal-cli.md` — connexion limitée `bureau_goal`.
