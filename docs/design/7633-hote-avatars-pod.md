---
type: Design
title: "Hôte d'avatars en pod : superviseur sans systemd, un tenant par conteneur, isolation des comptes prouvée"
description: "Tranche 1 de #7633 : note de conception de l'hôte partagé d'avatars dans un pod Kubernetes. Un conteneur de superviseur pour l'état machine, un conteneur et un volume par personne, un utilisateur système par personne sans useradd/linger/systemd, refus de lecture croisée et témoin, droits du pod justifiés, clone dans son propre pod, déplacement poste ↔ pod partagé ↔ pod dédié, carte Host d'un pod (claude et codex en abonnement), et découpage des étapes 2 à 6 en tranches d'au plus une journée."
status: draft
tags: [avatar, pod, kubernetes, hote-partage, isolation, comptes, abonnement, spawn, placement, securite]
generated: { by: "mty-amh-impl-04/deepseek", at: "2026-11-17T04:55:00Z" }
sources:
  - { resource: "manaty/ameesh#7633", title: "Hôte partagé d'avatars en pod Kubernetes, et clone dans son pod" }
  - { resource: "manaty/ameesh#7659", title: "Tranche 1 : cette note de conception" }
  - { resource: "manaty/ameesh#7627", title: "Spec owner du 17/11, décision 11 (pod partagé) et décision 12 (clones des agents personnels)" }
  - { resource: "manaty/ameesh#7106", title: "Hôte partagé multi-tenant sur VM : policy.shared/tenants, runner par utilisateur, runner_presence, garde de claim" }
  - { resource: "manaty/ameesh#7107", title: "Placement des enfants sur un hôte propre ; pod refusé (spawn-pod-unsupported)" }
  - { resource: "manaty/ameesh#213954", title: "Décision Q2 : clés d'organisation seulement sur un hôte partagé" }
  - { resource: "manaty/ameesh#213955", title: "Décision Q6 : l'enfant est payé par le budget/compte du parent" }
  - { resource: "docs/design/5322-avatar-humain.md", title: "Conception avatar §3.4 à §3.6, Q2 et Q6" }
  - { resource: "docs/design/decisions/0033-isolated-meshes-hosting-and-identity.md", title: "0033 §3 : les abonnements humains restent sur les appareils" }
  - { resource: "docs/HOST-HANDOFFS.md", title: "Hôte partagé multi-tenant et relève à préférence" }
  - { resource: "docs/profils/cluster.md", title: "Profil cluster : volume, sauvegardes, identifiants et abonnements" }
  - { resource: "docs/COMPTES.md", title: "Dossiers de comptes de harnais, 0700/0600, profils de compte" }
  - { resource: "docs/design/etudes/2202-external-sandbox-turns.md", title: "Job Kubernetes borné, relais de modèle, aucune clé au Job" }
  - { resource: "docs/design/3167-placement-liberation.md", title: "Hôtes burst TPK ; ameesh ne programme pas de pods" }
  - { resource: "src/ameesh/tenancy.py", title: "useradd, enable-linger, unité de tenant, register, battement" }
  - { resource: "src/ameesh/avatar_children.py", title: "children: own-host|none|pod, spawn-pod-unsupported" }
  - { resource: "src/ameesh/relocation.py", title: "agent move : préflight, tenant, compte, conversation" }
  - { resource: "src/ameesh/runner.py", title: "resource_once, canon_sync_once, SIGTERM, bail" }
  - { resource: "src/ameesh/election.py", title: "candidate_state : relevé, porte, canon, tenant" }
  - { resource: "src/ameesh/migrations/0575_shared_host_tenants.sql", title: "host_tenancy, host_tenants, runner_presence, ameesh_tenant_claim_ok" }
  - { resource: "src/ameesh/migrations/0576_tenant_account_profiles.sql", title: "profils de compte publiés au battement" }
  - { resource: "deploy/k8s/statefulset.yaml", title: "Profil cluster existant : securityContext, fsGroup, PVC, probes" }
  - { resource: "deploy/sql/role-superviseur.sql", title: "Contrat du rôle superviseur ; toute colonne nouvelle s'y tranche" }
---

# Hôte d'avatars en pod (#7633, tranche 1)

**Lot #7659**, note de conception demandée par mesh-design le 2026-11-17.
Lecture du code à `aeca7e2eb` (`main` du 17/11). **Cette note ne contient aucun
code, aucune migration, aucun déploiement et aucun secret.** Elle fixe le
modèle, les gardes et le découpage ; les lots d'implémentation sont créés par
le pilote à sa fusion.

## 0. Recommandation

1. **Un pod, N conteneurs de tenant, un conteneur de superviseur.** Le pod
   partagé est un vrai pod Kubernetes : le superviseur (sans `tenant`) tient
   l'état machine ; chaque personne a **son conteneur**, **son UID système**,
   **son PVC** et **son runner**. Le superviseur des processus est **kubelet**
   (redémarrage, journaux, SIGTERM, sondes) ; ameesh ne réimplémente ni
   systemd, ni `useradd`, ni `enable-linger`.
2. **Le volume est la frontière.** Chaque PVC n'est monté que dans le
   conteneur de son tenant, jamais dans le superviseur ni dans un autre
   conteneur. Le dossier de comptes et l'état du harnais sont en 0700/0600 sur
   ce volume. La lecture croisée est refusée par le **namespace de montage**,
   puis par les **modes**, et un **témoin** à contrôle positif le prouve.
3. **Une carte Host de pod** (`policy.runtime.kind: pod`) décrit l'hôte :
   `shared: true`, `tenants`, `credential_modes`, `runtime` et une référence
   d'isolation relue par le bureau. Le pod partagé sert aux avatars inactifs ;
   un **pod dédié** est le même modèle avec un seul tenant ; un **pod d'enfant**
   est un pod dédié éphémère, créé par un pourvoyeur, jamais par l'agent.
4. **Abonnement : décision à rouvrir, pas un détail.** La carte Host demandée
   admet `claude` et `codex` en `subscription`. Or le canon refuse aujourd'hui
   `subscription` sur un hôte `shared` (Q2, #213954), et 0033 §3 dit que les
   abonnements humains restent sur les appareils. La note propose une
   **exception étroite aux hôtes pod à volume par tenant**, avec référence
   d'isolation revue, témoin vert et vérification des conditions du
   fournisseur. Sans cette décision, la carte reste en `api-key` et la partie
   « abonnement » du lot n'est pas livrée.
5. **Découpage** : six tranches d'au plus une journée, environ **5,5 j** au
   total, plus une qualification TPK d'environ 1 j hors série. L'estimation
   actuelle de #7633 (3 j) est trop basse : le pod ajoute le chart, le
   pourvoyeur, le témoin et la connexion du siège.

## 1. Périmètre, sources et ce qui n'est pas vérifié

### 1.1 Dans le périmètre

- Le modèle de l'hôte partagé **dans un pod** : superviseur, conteneurs de
  tenant, UID/GID, PVC, état persistant.
- Le remplacement de `useradd`, `loginctl enable-linger` et
  `ameesh-runner-tenant@<id>.service` de `tenancy.py` (#7106).
- Le refus de lecture croisée et son **témoin**.
- Les droits du pod (`securityContext`, `capabilities`, `fsGroup`), chacun
  justifié.
- Le clone en pod (`avatar.children: pod`, aujourd'hui
  `spawn-pod-unsupported`).
- Le déplacement poste ↔ pod partagé ↔ pod dédié, conversation continue.
- La carte Host d'un pod, y compris `claude` et `codex` en `subscription`, et
  la procédure de connexion du siège par la personne.
- Le découpage des étapes 2 à 6 de #7633 en tranches d'au plus une journée.

### 1.2 Hors périmètre

- Le code, les migrations, les tests (ils appartiennent aux tranches 2 à 6).
- Le déploiement sur le cluster TPK et les sièges TPK : le chart est livré,
  le déploiement et la qualification réelle sont un lot TPK/IMA distinct.
- L'isolation SQL par tenant (L155, 0033) : la note reprend la limite honnête
  de #7106, elle ne la revendique pas.
- La mémoire de persona, Memoria, la porte d'actions, les connecteurs : autres
  lots de la série #7627-#7638.

### 1.3 Ce qui est su, et ce qui reste à vérifier

Le lot #7633 donne un constat daté du 17/11 ; il est la seule source des faits
TPK ci-dessous. **Aucune autre hypothèse sur `tpk-stg` n'est utilisée.**

| Fait | Source | Usage dans cette note |
|---|---|---|
| Cluster `tpk-stg`, namespace `issue-manager` | constat #7633, 17/11 | exemple de carte Host ; à confirmer sur le cluster |
| Aucun hôte n'y admet `claude` en `subscription` ; `ima-documents-tpk-staging` est codex/deepseek en clé d'API | constat #7633, 17/11 | la marche à couvrir par la tranche 5 |
| 0 ligne dans `host_tenants` des deux bases | constat #7633, 17/11 | l'hôte partagé VM n'a jamais servi ; rien à migrer |
| Pools `baseline` 2 × DEV1-L, `burst` DEV1-L max 8, Terraform | `3167` §4, décision owner du 28/10 (contexte plus ancien) | capacité probable, **à revérifier** ; non utilisée comme garantie |
| Le profil cluster existant est un pod par agent, `fsGroup: 10001`, PVC 20 Gi | `deploy/k8s/statefulset.yaml`, `docs/profils/cluster.md` | point de départ du chart, pas la cible multi-tenant |

À vérifier **avant** les tranches 2, 3 et 6, et à consigner dans le lot de
qualification (pas d'hypothèse dans le chart) :

- version Kubernetes et niveau **Pod Security Admission** (les conteneurs
  d'init root sont-ils admis ? `restricted` ou `baseline` ?) ;
- classe de stockage : `ReadWriteOnce`, snapshots, `fsGroup`, options
  `uid`/`gid` au montage, ré-attachement d'un PVC d'un pod à l'autre ;
- politique d'UID du namespace (plage autorisée, `runAsUser` imposé, PSP/PSA,
  OpenShift ou non), `hostUsers` disponible ou non ;
- CNI et `NetworkPolicy` (le trafic intra-pod n'est jamais filtré par une
  NetworkPolicy), egress vers la base, le canon, le relais et les
  fournisseurs ; blocage du service de métadonnées `169.254.169.254` ;
- registre d'images, digest épinglé, `imagePullSecret` ;
- RBAC du pourvoyeur d'enfants (création de pods, pas de lecture de secrets,
  pas d'`exec`), quotas et `LimitRange` du namespace ;
- cgroup v2 et **lisibilité réelle** du cgroup du pod depuis un conteneur
  (namespace cgroup privé ou non) : `memory.max`, `cpu.max`, `cpu.stat` ;
  `shareProcessNamespace` par défaut ;
- conditions d'utilisation des fournisseurs pour un abonnement dans un
  conteneur sans humain devant l'écran (`docs/profils/cluster.md` §6) ;
- politique d'accès des personnes à la connexion de leur siège (pod de
  connexion dédié, Role `pods/exec` nommé sur ce seul pod) et à aucun autre
  conteneur.

**Règle de lecture** : la note décrit ce que le chart **exige** ; si le
cluster ne le permet pas, la tranche est bloquée ou prend le repli indiqué,
jamais une hypothèse silencieuse.

## 2. Modèle retenu

### 2.1 Un pod, un superviseur, un conteneur par personne

```
Pod <hôte logique> (namespace <ns>)
┌──────────────────────────────────────────────────────────────────────┐
│ superviseur  (uid 10001, sans tenant)                                │
│   ameesh run : host_resources, host_lifecycle, porte, canon sync     │
│   aucun volume de tenant, aucun jeton Kubernetes                     │
│                                                                      │
│ tenant alice (uid 2001)            tenant bob (uid 2002)             │
│   ameesh run, AMEESH_TENANT=…        ameesh run, AMEESH_TENANT=…     │
│   /home/ameesh-alice ← pvc-alice     /home/ameesh-bob ← pvc-bob     │
│   /tmp ← emptyDir-alice              /tmp ← emptyDir-bob            │
│   canon en lecture seule             canon en lecture seule         │
└──────────────────────────────────────────────────────────────────────┘
        kubelet : restart, journaux, SIGTERM, sondes, limites
```

- **Un seul pod** pour les avatars inactifs : c'est la décision owner
  (décision 11 de la spec du 17/11). Le coût est celui d'un pod, pas d'un pod
  par personne.
- **Un conteneur par personne** : chaque conteneur a son `runAsUser`, son
  `runAsGroup`, son namespace de montage, son PVC et son `/tmp`. C'est ce qui
  permet de refuser la lecture croisée sans un superviseur root qui porterait
  `SETUID`/`SETGID`.
- **Un conteneur de superviseur** : c'est le runner `ameesh` **sans tenant**
  de #7106, pas un nouveau processus maison. Il publie `host_resources`, tient
  `host_lifecycle` et la porte, fait le `canon sync` de l'hôte, et ne réclame
  aucun agent. Il ne monte aucun volume de tenant.
- **kubelet est le superviseur de processus** : redémarrage du conteneur,
  journaux sur stdout, SIGTERM relayé par `tini` (déjà l'ENTRYPOINT de
  l'image), sondes de readiness. Il n'y a **ni systemd, ni `useradd`, ni
  `loginctl`, ni unité `ameesh-runner-tenant@`**.
- **Identité de l'hôte** : `AMEESH_HOST` est le **nom logique stable** de la
  carte Host, pas `metadata.name`. Un `Deployment` recréerait un nom de pod
  différent et casserait le canon, les relevés et le bail. Le chart utilise un
  `StatefulSet` à une réplique (nom stable) ou fixe `AMEESH_HOST` par
  configuration ; le pod est retrouvé par étiquettes, pas par son nom.

### 2.2 Alternatives écartées

| Alternative | Pourquoi elle est écartée |
|---|---|
| **Un seul conteneur root, superviseur maison qui `setuid` et lance les runners** | Il faut `SETUID`, `SETGID`, souvent `DAC_OVERRIDE`, et tous les tenants partagent le même namespace de montage : le PVC de B est visible de A, seule la permission 0700 le protège. Il réimplémente kubelet (redémarrage, journaux, signaux) et le témoin d'isolation devient plus faible. Rejeté pour la frontière ; gardé seulement comme garde d'erreur. |
| **systemd dans le pod** | systemd comme PID 1 demande souvent des droits root, un `hostPath` ou des capacités, et le profil cluster de `docs/profils/cluster.md` ne l'utilise pas. Rejeté. |
| **Un pod par avatar inactif** | C'est l'isolation la plus forte (namespaces réseau, IPC et PID séparés), mais c'est le coût que la décision 11 refuse pour les inactifs. Gardé comme **variante pod dédié** pour un avatar qui a du travail ou qui exige l'isolation réseau. |
| **Réutiliser `policy.sandbox`** | `sandbox` est un **Job d'un tour** de relecteur : pas de runner durable, pas de tenant, pas de PVC, pas de `host_tenants`. Un hôte pod a un runner multi-tours et un état persistant. Mélanger les deux enveloppes rendrait les gardes illisibles. Rejeté ; `sandbox` reste pour les Jobs bornés. |
| **Un runner de tenant dans un pod sans superviseur** | Sur un hôte `shared`, la présence et la porte viennent du superviseur (`host_resources`, `host_lifecycle`) ; un tenant seul ne les publie pas (`runner.resource_once` rend `None`). Le pod d'enfant garde donc un petit superviseur (voir §8). |
| **Le PVC du parent monté dans le pod de l'enfant** | L'enfant lit alors les identifiants du parent ; c'est exactement ce que 5322 §3.6 interdit. Rejeté. |
| **Copier le dossier d'abonnement du parent vers l'enfant** | Un siège n'est pas partageable entre deux UID et les conditions du fournisseur ne le permettent pas par défaut. Rejeté ; l'enfant utilise un relais ou une clé d'API (voir §8). |

### 2.3 Conséquences opérationnelles

- **Ajouter ou retirer un tenant** = modifier le chart (PR relue par le
  bureau) et faire un rollout du pod. Ce n'est pas un `useradd` à chaud ; le
  rollout draine les tours en cours (`terminationGracePeriodSeconds`) et les
  autres tenants redémarrent leur conteneur. Pour une petite équipe, ce geste
  rare est acceptable ; il est **explicitement** le prix de l'isolation par
  conteneur.
- **Un seul superviseur par hôte** : le chart fixe `replicas: 1` et une
  stratégie `Recreate` (ou `OrderedReady`), et la base doit refuser un second
  écrivain. `host_resources` n'a aujourd'hui pas de bail ; la tranche 2 ajoute
  une garde (bail de superviseur ou `host_lifecycle` avec génération) pour
  qu'un rollout qui chevauche ne fasse pas deux écrivains. C'est un point de
  conception, pas un détail de chart.
- **Réseau et IPC partagés** : les conteneurs d'un pod partagent le namespace
  réseau et IPC. Un runner de tenant peut donc, en principe, joindre un port
  local d'un autre tenant ou une mémoire partagée. Les runners n'écoutent
  aucun port aujourd'hui (`docs/profils/cluster.md` : service sans port), et
  le témoin vérifie l'absence d'écoute ; c'est une **limite honnête**, pas une
  isolation revendiquée. Si l'owner exige l'isolation réseau, le pod dédié
  (un tenant par pod) est le repli, au coût d'un pod par avatar.
- **Ajout d'un tenant et visibilité mémoire** : la règle de visibilité de
  `specification.md` fait dépendre le placement de l'accès du responsable,
  des `admins` et des `occupants` de l'hôte au dépôt de mémoire de la
  persona. Sur un hôte partagé, les `admins` de l'hôte ne doivent pas lire la
  mémoire de chaque personne. Il faut trancher (voir §11, décision D5) :
  évaluer la visibilité par **tenant** et non par les admins de l'hôte, ou
  n'admettre que des personnes dont la mémoire est lisible par les admins.
  Sans cette décision, une carte Host partagée peut être refusée par
  `canon sync` pour raison de visibilité.

## 3. Ce qui remplace `useradd`, `enable-linger` et l'unité systemd

Le tableau ci-dessous est le cœur de la tranche 1 : chaque mécanisme de
`tenancy.py` (#7106) a un remplacement nommé, et aucun ne demande root sur
l'hôte.

| Mécanisme VM (#7106) | Remplacement dans le pod | Où |
|---|---|---|
| `useradd --create-home --user-group`, `chmod 0700`, `install -d` des dossiers de comptes et de l'unité | **UID/GID statiques** déclarés dans le chart ; entrée `passwd`/`group` projetée en ConfigMap ; PVC ou `emptyDir` déjà monté ; dossier de comptes 0700 créé par le tenant lui-même ou par l'init (voir §6) ; **aucun `useradd`** | chart + `tenancy.pod_plan()` (tranche 2) |
| `loginctl enable-linger <user>` | **kubelet** : le conteneur vit tant que le pod vit (`restartPolicy: Always`) ; il n'y a pas de session de connexion à maintenir ; `terminationGracePeriodSeconds` et `preStop` pour le drain | chart |
| `ameesh-runner-tenant@<id>.service` | **un conteneur par tenant** : `ameesh run`, `AMEESH_TENANT=human:<id>`, `AMEESH_RUNNER_ID=<hôte>/<id>`, `AMEESH_CONFIG=<config du tenant>` ; redémarrage par kubelet ; readiness `ameesh doctor --probe` ; SIGTERM relayé par `tini` | chart |
| `Environment=...`, `UMask=0077` de l'unité | variables d'environnement du conteneur (`HOME`, `USER`, `LOGNAME`, `AMEESH_*`) ; `umask 077` dans l'entrypoint ou un petit `sh -c 'umask 077; exec ameesh run'` ; les fichiers de compte sont explicitement 0600 | image/chart |
| `AMEESH_CONFIG=%h/.config/ameesh/config.json` | ConfigMap **par tenant** (non secret : hôte, tenant, runner, comptes sans clé) montée en lecture seule ; le mot de passe base vient d'un Secret ; `AMEESH_CONFIG` pointe le fichier du tenant | chart |
| `register` lit l'UID dans le `passwd` de l'hôte cible | l'UID vient du **chart** (source de vérité) ; `host_tenants.uid` est écrit par le bureau ; le **battement** (`runner_presence`) n'accepte que le `uid` du processus égal à `host_tenants.uid` (garde SQL, tranche 2) ; le témoin vérifie `id -u` = chart = base | `tenancy.beat` + SQL |
| `host tenant add` imprime `useradd`/`linger` | `ameesh host tenant add --pod` imprime le **fragment de chart** (conteneur, PVC, UID, ConfigMap) et les valeurs de liaison ; le bureau applique le chart puis `--register` avec l'UID du chart ; aucun geste root | `tenancy.pod_plan()` |
| `host_resources` publié par le runner superviseur | le **conteneur superviseur** publie la capacité du pod (limites cgroup, volumes rapportés par les tenants) ; les runners de tenant ne publient pas de relevé machine, ils battent et rapportent leur propre consommation (voir §3.2) | `runner.resource_once` (mode pod) |
| `host_lifecycle`, porte, `host_hold` | le **superviseur** reste le seul écrivain ; les tenants ne publient ni relevé ni cycle de vie | inchangé |
| `canon sync` par l'hôte | le **superviseur** fait le `canon sync` (init `canon` + `canon-fetch` comme le profil cluster) ; les runners de tenant lisent le canon monté en lecture seule et ont `canon_sync_interval: 0` ; `canon_sync_once` saute les runners de tenant (tranche 2) | `runner.canon_sync_once` |
| journaux système (`journald`) | stdout/stderr du conteneur, lus par `kubectl logs` / le plateforme ; aucun journal local | plateforme |
| `ameesh doctor`, `attach` | readiness `ameesh doctor --probe` ; connexion du siège par un **pod de connexion dédié** (§7.3), jamais par `pods/exec` sur le pod partagé ; le runner reste le seul processus qui sert les tours | runbook |
| ménage (`housekeeping`) | par tenant sur son PVC ; `/tmp` par tenant en `emptyDir` avec `sizeLimit` ; aucun `/tmp` partagé | chart + `resources` |

### 3.1 `tenancy.py` : ce qui reste, ce qui change

- `TENANT_RE`, `unix_user`, `tenant_home`, `runner_id`, `check_tenant` restent
  la grammaire commune ; le chart **dérive** ses noms avec les mêmes
  fonctions (ou un `ameesh host tenant add --pod` qui les imprime), pour que
  `host_tenants.unix_user` et l'entrée `passwd` ne divergent jamais.
- `add_plan`/`shell_plan`/`unit_text` deviennent **spécifiques au runtime** :
  pour un hôte `runtime.kind: vm`, le plan actuel reste ; pour un hôte pod,
  un `pod_plan()` rend un fragment YAML/JSON (conteneur, volume, UID, ConfigMap,
  variables) et **aucune commande système**. Le plan pod n'exécute rien ; il
  est appliqué par le bureau via le chart.
- `register` : sur un pod, l'UID ne peut pas être lu dans un `passwd` local
  (il n'y a pas de `useradd`). Le bureau fournit l'UID du chart ; `register`
  le stocke ; la **preuve** que le processus tourne bien sous cet UID est le
  battement (`os.getuid()`) comparé en SQL. Un UID saisi sans témoin n'est
  jamais « vérifié » : c'est le battement qui le vérifie, pas la déclaration.
- `beat` : ajouter `AND t.uid = %s` (et, si utile, `t.unix_user = %s`) à
  l'INSERT `runner_presence` ; un runner dont l'UID réel diffère de la liaison
  ne publie pas de présence, donc ne peut pas être pris pour le tenant. C'est
  le pendant pod de « l'UID lu sur le passwd de l'hôte cible ».
- `isolation_problem` reste : il compare l'UID du processus à `host_tenants`
  et refuse deux personas du même tenant. Sur un pod, le pair d'un autre
  tenant a un UID distinct ; le même tenant (deux conteneurs) reste refusé.
- `operator` reste : la liaison de tenant est un geste **humain ou bureau**,
  jamais un agent. Le chart est relu par le bureau ; le `--register` est lancé
  par le bureau (ou un Job signé par lui), pas par le superviseur.

### 3.2 Relevés et capacité dans un pod

Le profil VM publie les mesures de l'hôte. Dans un pod, `/proc/meminfo`,
`/proc/loadavg` et `os.cpu_count()` reflètent le **nœud**, pas le pod : les
publier tels quels ferait juger la capacité du nœud à chaque hôte pod et
fausserait l'admission.

Le contrat proposé (tranche 2) :

- **Superviseur** : publie `host_resources` pour l'hôte pod avec
  - `mem_available_bytes` = capacité **déclarée par le chart** (limite du pod)
    moins la somme des consommations rapportées ; si, et seulement si, le
    cgroup du **pod** est réellement lisible (namespace cgroup non privé ou
    montage explicite), la limite lue prévaut sur la valeur déclarée. Un
    namespace cgroup privé (le défaut) ne montre au conteneur superviseur que
    **son propre** cgroup : lire `memory.max` là et l'appeler « limite du pod »
    serait faux, donc on ne le fait pas ;
  - `cpu_count` = quota CPU **déclaré** du pod converti en cœurs, au moins 1 ;
    même réserve : `cpu.max` n'est lu que s'il porte sur le cgroup du pod, pas
    sur celui du conteneur superviseur ;
  - `load1` = **non mesuré** (`None`) : la charge 1 min est celle du nœud, pas
    du pod ; la carte Host pod utilise `max_cgroup_throttled` et
    `max_cgroup_memory_used` (déjà supportés) au lieu de `max_load` ;
  - `disk_free_bytes` = pire espace libre rapporté par les PVC des tenants,
    `None` si aucun rapport (mesure absente, pas une valeur inventée) ;
  - `volumes` par tenant si le schéma le permet ; sinon le pire volume.
- **Runner de tenant** : joint à son battement un petit document de
  consommation de **son** cgroup (`memory.current`, `memory.max`, `cpu.stat`,
  `cpu.max`, espace libre de son PVC) ; noms et nombres, aucun chemin de
  secret. La colonne peut être `runner_presence.resources jsonb` (migration
  additive de la tranche 2), à classer dans le contrat du rôle superviseur :
  c'est de l'état, pas un contenu de compte.
- **Admission** : tant qu'un tenant n'a pas de rapport frais, le superviseur
  ne retire pas sa part de la capacité ; il utilise la **limite déclarée** du
  conteneur, ce qui est conservateur. Un tenant qui ment sur sa consommation
  ne peut pas dépasser sa limite cgroup : le noyau l'arrête ; le relevé sert
  au classement, pas à la garde.
- **La garde de capacité reste celle de Kubernetes** (requests/limits,
  `ResourceQuota`) ; `host_resources` sert à l'admission ameesh **à
  l'intérieur** du pod, pas à programmer des pods.

### 3.3 Un seul écrivain par hôte

Le superviseur est le seul à écrire `host_resources`, `host_lifecycle` et
`canon_state` de l'hôte. Deux superviseurs (rollout qui chevauche, pod
dupliqué) écriraient les mêmes lignes et pourraient ouvrir la porte à tort.
La tranche 2 ajoute une garde nommée : bail de superviseur (ligne
`host_lifecycle` avec génération/époque, ou table dédiée), refus du second
écrivain, et `replicas: 1` + `Recreate` dans le chart. C'est la même
discipline que le bail d'agent : la base est l'arbitre, le chart est une
politesse.

## 4. Un volume, un état persistant par personne

### 4.1 Le PVC est l'unité

- **Un PVC par tenant**, `ReadWriteOnce`, monté à `/home/ameesh-<id>` dans
  **son** conteneur uniquement. Pas de `ReadWriteMany`, pas de `subPath`
  partagé, pas de `hostPath` : ce sont les trois façons de rendre un volume
  visible à deux tenants.
- Le PVC survit à la suppression du pod (politique Kubernetes par défaut) ;
  le supprimer est un geste explicite du bureau.
- Le PVC contient **tout** l'état de la personne : dossiers de comptes de
  harnais, sessions natives, état d'ameesh, worktrees. La base garde le
  journal, les baux et les fils ; le PVC est le cache et l'état local, comme
  dans `docs/profils/cluster.md` §5.

```
/home/ameesh-<id>/                 (racine du PVC, point de montage)
  .config/ameesh/config.json       (non secret, 0600)
  .config/ameesh/accounts/         (0700 ; profils de compte)
  .claude/ .codex/ .dsh/           (dossiers de harnais, 0700/0600)
  work/                            (worktrees, 0700)
  .local/state/ameesh/             (état, 0700)
```

Le mode de la **racine du montage** dépend du mode de provisionnement (voir
§6) : dans le mode `fsGroup` sans init root, elle peut être 0770
(`root:<fsGroup>`) ; ce n'est pas la frontière, car le volume n'est monté que
dans le conteneur du tenant. Les dossiers **`.config/ameesh/accounts`,
`.config/ameesh`, `.local/state/ameesh` et `work`** sont 0700, et les
identifiants 0600. Si la classe de stockage permet `uid`/`gid` au montage, ou
si le cluster autorise un init root, la racine est en plus 0700 au tenant
(voir §6.2).

### 4.2 Ce qui ne va jamais sur le volume partagé

- Aucun **Secret** de fournisseur dans le chart, la ConfigMap ou l'image : les
  identifiants d'abonnement sont écrits par la personne dans **son** PVC
  (`.credentials.json` Claude, `auth.json` Codex) ; la clé d'API est dans un
  Secret par tenant ou une variable d'environnement du conteneur, jamais dans
  le canon.
- Aucun dossier de compte **hors du PVC** : un profil `config_dir` dont le
  chemin sort du home du tenant est refusé par la validation des comptes en
  mode pod (tranche 2). Le défaut `~/.claude`/`~/.codex`/`~/.dsh` est sous le
  PVC.
- Aucun `/tmp` partagé : chaque conteneur de tenant a son `emptyDir` à `/tmp`
  avec `sizeLimit` ; le superviseur a le sien. Un fichier temporaire de A ne
  doit jamais être visible de B.
- Aucun **canon** inscriptible : le canon est un `emptyDir`/volume en lecture
  seule, alimenté par l'init `canon` du superviseur ; les tenants le lisent,
  ne l'écrivent pas.
- Aucune **clé de déploiement du canon** dans le conteneur d'un tenant : elle
  reste dans l'init `canon`/`canon-fetch` du superviseur, comme dans le profil
  cluster.

### 4.3 Comptes, profils et responsabilité

- La configuration `accounts` du tenant est **locale à son PVC** ; elle
  déclare ses profils (`claude/primaire`, `codex/primaire`, etc.). Le runner
  publie seulement les **noms** `harnais/compte` dans
  `runner_presence.profiles` (#7108) ; jamais un chemin ni un secret.
- Un profil `config_dir` est validé comme aujourd'hui : dossier à
  l'utilisateur, 0700, identifiants présents (présence seulement) ; un profil
  invalide n'est jamais choisi (`docs/COMPTES.md` §2).
- Deux tenants ne doivent pas publier le même profil de compte **de
  subscription** ; le superviseur peut signaler un doublon de noms (pas de
  lecture de secret) et le bureau le corrige. Un même identifiant
  d'organisation partagé entre deux tenants est un choix explicite de la
  politique (`api-key`), pas un défaut.
- **Connexion du siège** : la personne ouvre un shell dans un **pod de
  connexion dédié** qui ne monte que son PVC et ne contient que son conteneur,
  sous son UID (`kubectl exec -it <pod-de-connexion> -- sh`) ; le shell
  démarre avec son `HOME` sur son PVC ; elle lance `claude` puis `/login`, ou
  `codex login`. Le flux ouvre une URL sur son poste ; aucun agent ne saisit
  ses identifiants. Le runner relit la configuration au tour suivant. Voir
  §7.3 pour le RBAC et les conditions.

## 5. Refus de lecture croisée : mécanisme et témoin

### 5.1 Ce que l'on protège, et contre qui

L'acceptation de #7633 est : **deux personnes dans le pod partagé, le runner
de l'une ne peut pas lire le dossier de comptes de l'autre**. Le tableau
distingue ce qui est refusé, ce qui est une limite assumée, et ce qui est hors
frontière.

| Menace | Refus / limite | Preuve |
|---|---|---|
| Le runner de A lit le dossier de comptes de B | **Refusé** : le PVC de B n'est pas monté dans le conteneur de A ; même s'il l'était, B est 0700 et A n'a ni l'UID ni le GID de B | témoin négatif + témoin de permissions |
| A modifie son `agents_filter` ou sa configuration pour réclamer un agent de B | **Refusé** : `ameesh_tenant_claim_ok` (SQL) ne donne le bail qu'au runner lié au `responsible` ; le filtre n'est qu'une optimisation | test SQL, comme #7106 |
| A lit la mémoire ou les fichiers d'un processus de B | **Refusé** : `shareProcessNamespace: false` (défaut) ; chaque conteneur a son namespace PID ; pas de `hostPID` | chart + `kubectl exec` (pas de `/proc/<pid>` de B) |
| A joint un port local de B | **Limite** : le namespace réseau est partagé par le pod ; les runners n'écoutent aucun port ; une NetworkPolicy ne filtre pas l'intra-pod | témoin `ss -ltn` ; repli pod dédié si l'isolation réseau est exigée |
| A lit une mémoire partagée IPC de B | **Limite** : le namespace IPC est partagé par le pod ; aucun composant n'utilise IPC ; repli pod dédié | chart ; pas d'objet IPC |
| A ouvre un shell dans le conteneur de B (`pods/exec`) | **Refusé** : aucun droit `pods/exec` n'est délivré sur le pod partagé, même nommé par pod — le conteneur n'est pas dans l'objet RBAC ; la connexion passe par un pod dédié d'un seul conteneur (§7.3) | contrôle 6 : RBAC + `SelfSubjectAccessReview` |
| A lit le PVC de B via le superviseur | **Refusé** : le superviseur ne monte aucun volume de tenant | témoin `ls /home` dans le superviseur |
| L'admin du cluster lit tous les PVC | **Limite assumée** : un admin de cluster peut monter un PVC ; c'est la frontière de confiance du cluster. Elle est incompatible avec un abonnement personnel si l'owner ne l'accepte pas | décision D4 |
| Une faille noyau ou un conteneur privilégié | **Hors frontière** : pas de `privileged`, pas de `hostPath`, `seccompProfile: RuntimeDefault`, `capabilities.drop: ALL`, PSA | chart + revue |
| A lit les lignes de B dans la base | **Limite honnête de #7106** : le runner a la chaîne de connexion du mesh ; l'isolation SQL par rôle est L155, hors de cette note | §9 de 5322 |

### 5.2 Les couches, dans l'ordre

1. **Namespace de montage** : le PVC de B n'est pas dans le conteneur de A.
   Le chemin `/home/ameesh-B` n'existe pas ; il n'y a pas de lien symbolique,
   pas de `subPath`, pas de volume partagé.
2. **UID/GID et modes** : le dossier de comptes de B est 0700, ses
   identifiants 0600, propriétaire `ameesh-B:<gid>`. Si un volume était monté
   par erreur, A (autre UID) ne pourrait pas le traverser ; le `fsGroup`
   partagé ne donne pas le groupe du dossier 0700.
3. **Pas de volume inscriptible commun** : seuls le canon (lecture seule) et
   les ConfigMap (lecture seule) sont partagés ; les `emptyDir` `/tmp` sont
   par conteneur.
4. **Pas de jeton ni de droit Kubernetes dans les conteneurs de tenant** :
   `automountServiceAccountToken: false`, aucun `hostPath`, aucun
   `privileged`, aucun `hostNetwork`/`hostPID`/`hostIPC`,
   `shareProcessNamespace: false`.
5. **Garde en base** : `ameesh_tenant_claim_ok` et le battement vérifié par
   UID ; le runner de A ne peut pas prendre le bail de B.

### 5.3 Le témoin

Le témoin est un **script exécuté par le bureau/opérateur**, pas par l'agent
intéressé : un agent pourrait mentir sur sa propre sortie. Il est rejouable et
produit une trace attachée à la PR ou au lot de qualification (lien de run,
digest d'image, UID du pod, version Kubernetes, date) ; **aucun secret n'est
lu ni affiché**.

Emplacement proposé (tranche 2) : `deploy/k8s/pod-witness/` avec un script
`preuve-isolation-comptes.sh` et deux manifests de test (un pod témoin à deux
PVC, un Job de contrôle). Le script a six contrôles, chacun avec un
**contrôle positif** pour prouver qu'il n'est pas vide :

1. **Identité** : dans le conteneur de A, `id -u` = UID du chart =
   `host_tenants.uid` = `runner_presence.uid` ; `stat -c '%u:%g %a'` du home
   et du dossier de comptes ; le home est au moins 0700 sur le dossier de
   comptes. Contrôle positif : le même `stat` sur A réussit.
2. **Montage** : dans A, `cat /proc/self/mountinfo` ne contient pas le PVC de
   B ; `ls -ld /home/ameesh-B` échoue (`ENOENT`) ; `cat
   /home/ameesh-B/.config/ameesh/accounts/.temoin-B` échoue. Contrôle
   positif : A lit son propre `.temoin-A`.
3. **Permissions** (deuxième couche, pod témoin qui monte **les deux** PVC en
   lecture seule et tourne sous l'UID de A) : `cat
   /mnt/b/.config/ameesh/accounts/.temoin-B` échoue (`EACCES`), `cat
   /mnt/a/.../.temoin-A` réussit, `stat` de B montre `B:B 700`. Ce pod est un
   artefact de test, supprimé après ; il ne fait pas partie du pod partagé.
4. **Superviseur** : dans le conteneur superviseur, `ls /home` ne montre aucun
   home de tenant et `cat /home/ameesh-A/...` échoue ; le superviseur n'a que
   son propre `/tmp` et le canon.
5. **Base** : un runner de A ne prend pas le bail d'un agent de B et prend le
   sien (test SQL, réutilise `tests/test_7106_hote_partage.py`) ; le
   `runner_presence.uid` de A correspond à la liaison, celui d'un intrus ne
   bat pas.
6. **Connexion du siège** : aucun RoleBinding `pods/exec` ne vise le pod
   partagé ; un `SelfSubjectAccessReview` (`create`, `pods`, sous-ressource
   `exec`, nom du pod partagé) rend `allowed: false` ; le Role du pod de
   connexion porte `resourceNames` sur ce pod, qui n'a qu'un conteneur ;
   le même contrôle pour le compte de A sur le pod de connexion de B rend
   `false`. Contrôle positif : le compte de A peut `exec` dans **son** pod de
   connexion quand il existe.

Le témoin vérifie aussi, dans le même run, que **aucun port n'écoute** dans
les conteneurs de tenant (`ss -ltn`), et que le conteneur de A ne voit pas le
jeton de compte de service (`/var/run/secrets/kubernetes.io/serviceaccount`
absent).

**Ce que le témoin ne prouve pas** : l'admin du cluster, le noyau, les
conditions du fournisseur, l'isolation réseau/IPC entre conteneurs, et
l'absence de faille dans un harnais. Ces limites sont dans le tableau §5.1 ;
elles ne sont pas « compensées » par le témoin.

**Où le témoin tourne** :

- en CI sur un cluster `kind` (mécanisme : namespaces, UID, modes, PVC) ;
- sur `tpk-stg` à la qualification (stockage, PSA, RBAC réels), lot TPK/IMA
  hors série. Tant que le second n'est pas vert, la carte Host pod n'est pas
  « faite » au sens de #7633.

## 6. Droits du pod, chacun justifié

### 6.1 Tableau des droits

| Conteneur | Droit | Justification | Refusé |
|---|---|---|---|
| **superviseur** | `runAsUser: 10001` (non root), `runAsGroup: 10001`, `runAsNonRoot: true` | même utilisateur que le profil cluster ; il ne monte aucun PVC de tenant | root, `SETUID` |
| | `allowPrivilegeEscalation: false`, `privileged: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault` | il ne fait que du réseau/DB, du canon en lecture et des écritures d'état | `SYS_ADMIN`, `NET_ADMIN`, `DAC_OVERRIDE` |
| | `readOnlyRootFilesystem: true`, `/tmp` sur `emptyDir` | l'image n'est pas modifiée ; l'état est en base | écriture dans l'image |
| | `automountServiceAccountToken: false` | il n'a pas besoin de l'API Kubernetes ; le canon est un volume | jeton de compte de service |
| | `resources` requests/limits | capacité mesurable ; l'hôte pod a une limite | — |
| **tenant** | `runAsUser: <uid du chart>`, `runAsGroup: <gid>` | un utilisateur système par personne ; l'UID est la frontière | root, UID partagé |
| | `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `privileged: false` | aucun besoin d'élévation ; un harnais n'est pas root | `privileged` |
| | `capabilities.drop: [ALL]` | aucun besoin (`ping`, ports < 1024, montages) | `NET_RAW`, `CHOWN`, `SETUID` |
| | `readOnlyRootFilesystem: true`, `/tmp` sur `emptyDir` `sizeLimit`, PVC sur `$HOME` | seul le home est inscriptible ; le `/tmp` est privé | racine inscriptible, `/tmp` partagé |
| | `automountServiceAccountToken: false` | le runner n'a aucun geste Kubernetes ; la création d'enfants passe par le pourvoyeur | jeton, `kubectl` dans le pod |
| | `seccompProfile: RuntimeDefault` | réduit la surface noyau | `Unconfined` |
| | `resources` requests/limits | un tenant ne peut pas affamer les autres ; la somme est la capacité du pod | pas de limite |
| | `shareProcessNamespace: false`, `hostNetwork/PID/IPC: false` | pas de visibilité des processus/ports de l'autre tenant | partage de namespaces |
| **init de provisionnement** (alternative root, §6.2) | `runAsUser: 0`, `runAsNonRoot: false`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL], add: [CHOWN]` (+ `FOWNER` si `chmod` après `chown`) | il donne le PVC à l'UID du tenant et pose 0700 ; il monte **un seul** PVC, n'a pas de réseau utile, pas de jeton, et sort | `DAC_OVERRIDE`, `SETUID`, `SYS_ADMIN`, shell root |
| **pod** | `fsGroup` / `fsGroupChangePolicy` | voir §6.2 : provisionnement du volume, pas frontière | `fsGroup` par tenant (impossible au niveau pod) |
| **pod** | `enableServiceLinks: false` | pas de variables de service inutiles | — |
| **pod** | `terminationGracePeriodSeconds` (≥ 120 s), `preStop` de drain | finir ou rendre le bail avant le SIGKILL ; le runner libère son bail sur SIGTERM | arrêt brutal |

**Droits explicitement refusés** : `privileged`, `hostPath`, `hostNetwork`,
`hostPID`, `hostIPC`, `shareProcessNamespace: true`, `SYS_ADMIN`,
`NET_ADMIN`, `DAC_OVERRIDE` (sauf le `chown` ciblé de l'init alternatif),
`SETUID`/`SETGID` (pas de superviseur root), jeton de compte de service dans
un conteneur de tenant, `pods/exec` délivré sur le pod partagé (§7.3),
`hostUsers: true` (le défaut est le namespace d'UID de
l'hôte ; `hostUsers: false` est un durcissement optionnel **à vérifier**, pas
une hypothèse).

### 6.2 `fsGroup` : le point qui décide du provisioning

`fsGroup` est un réglage **du pod**, pas d'un conteneur : on ne peut pas
donner un `fsGroup` différent à chaque tenant dans le même pod. Deux modes
sont proposés ; le chart choisit selon ce que le cluster autorise.

**Mode A — `fsGroup` partagé, sans init root (défaut, compatible PSA
`restricted`).**

- Pod : `fsGroup: <gid neutre>` (par exemple 10000), `fsGroupChangePolicy:
  OnRootMismatch`.
- Conteneurs de tenant : `runAsGroup: <gid neutre>` ; le volume est
  group-écrivable à sa racine, ce qui permet au tenant de créer ses dossiers
  sans `chown` root.
- Le `gid` est **le même** pour tous les tenants ; ce n'est pas une frontière :
  aucun volume inscriptible n'est partagé, chaque PVC n'est monté que dans son
  conteneur, et le dossier de comptes est 0700 (le groupe n'y a aucun droit).
- La racine du PVC peut être `0770` (`root:<gid>`, après le passage de
  `fsGroup`/`fsGroupChangePolicy`) ou `0775` selon la classe ; **ce n'est pas
  la frontière** : le volume n'est monté que dans le conteneur de son tenant,
  et les dossiers `accounts`, `.local/state/ameesh` et `work` sont créés 0700
  (par le tenant lui-même ou par l'init) : le groupe `fsGroup` ne peut ni les
  lire ni les traverser. Le témoin vérifie ces dossiers et les fichiers 0600,
  pas le mode de la racine.
- Aucun conteneur root ; PSA `restricted` peut l'admettre (sous réserve de la
  politique du cluster).

**Mode B — init root de `chown`, sans `fsGroup` partagé (si le cluster
l'autorise).**

- Un init par tenant, qui monte **seulement** le PVC de ce tenant, tourne en
  root avec `CHOWN` (et `FOWNER` si nécessaire), fait `chown <uid>:<gid>` et
  `chmod 0700` de la racine, puis sort. Le conteneur du tenant n'a plus besoin
  du `fsGroup` partagé ; la racine est 0700 au tenant.
- Justification : c'est le seul droit root, il est court, sans réseau utile,
  sans jeton, sans autre volume ; il remplace `useradd`/`chown` de la VM.
- Refusé si PSA `restricted` ou si le namespace interdit root : on reste en
  mode A.

**Pod dédié (un seul tenant)** : `fsGroup: <gid du tenant>` (ou le `gid`
neutre) et `runAsGroup` du tenant ; pas besoin d'init root. C'est le mode le
plus simple, et c'est aussi le repli si l'isolation réseau est exigée.

**Pourquoi pas un `fsGroup` par tenant dans le pod partagé** : Kubernetes ne
le permet pas ; il faudrait un pod par tenant, c'est-à-dire le pod dédié.

### 6.3 `passwd`/`group`, `HOME` et l'absence d'identifiants ambiants

- Le conteneur du tenant tourne sous un UID qui n'existe pas dans
  `/etc/passwd` de l'image (qui ne connaît que `ameesh`, uid 10001). Les
  harnais Node/Python appellent `getpwuid` : sans entrée, certains échouent au
  démarrage.
- Le chart projette une ConfigMap **non secrète** `ameesh-pod-passwd` et
  `ameesh-pod-group` (root, nobody, `ameesh` 10001, et les tenants), montée
  en lecture seule sur `/etc/passwd` et `/etc/group` dans les conteneurs de
  tenant. Elle est dérivée du chart, donc de la même source que
  `host_tenants` ; le témoin compare `id -u` et `getent passwd`.
- Variables du conteneur : `HOME=/home/ameesh-<id>`, `USER=ameesh-<id>`,
  `LOGNAME=ameesh-<id>`, `TMPDIR=/tmp`, `umask 077`. Les variables
  d'identifiants ambiantes (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `DSH_HOME`) ne sont **pas** posées par le
  pod ; le runner les retire et ne pose que celles du compte choisi
  (`docs/COMPTES.md` §2). Le mot de passe de la base est le seul secret
  ambiant, et il est nécessaire au runner comme sur la VM.
- Les binaires de harnais sont dans l'image, épinglée par digest ; un tenant
  ne peut pas installer un binaire dans la racine (lecture seule) ; il peut
  installer dans son PVC, ce qui est son état, pas celui d'un autre.

### 6.4 Réseau

- Le pod n'expose **aucun service** : pas de `Service` avec port, pas
  d'`Ingress`. Le profil cluster le fait déjà (`deploy/k8s/service.yaml`).
- `NetworkPolicy` : entrée refusée ; sortie par défaut refusée puis
  autorisée pour la base, le canon (si `git fetch`), le relais, les
  fournisseurs de modèles et le DNS. Une NetworkPolicy standard ne filtre pas
  l'intra-pod (les conteneurs partagent le namespace réseau) : c'est la limite
  §5.1, pas une garde.
- Bloquer explicitement le service de métadonnées `169.254.169.254` si le
  CNI le permet (sinon le documenter) ; aucun `hostNetwork`.
- Le superviseur n'a pas besoin d'un port ; `ameesh serve`/`attach` restent
  hors du pod ou passent par le relais existant ; la note ne les expose pas.

### 6.5 Pourvoyeur d'enfants (hors conteneurs de tenant)

Le pourvoyeur qui crée les pods d'enfants (§8) est un composant du mesh, pas
un agent. Il a :

- un `ServiceAccount` dédié, `automountServiceAccountToken: true` **pour lui
  seul** (jamais dans les conteneurs de tenant) ;
- un `Role` limité au namespace : `create`/`delete`/`get`/`list` des pods et
  `get` des logs, **pas** `get secrets`, **pas** `pods/exec`, **pas** de
  namespaces autres ;
- une `ValidatingAdmissionPolicy` **et** un webhook d'admission sous une
  identité distincte (`ameesh-pod-admission`) qui recoupe la demande
  `pod_requests` (lecture seule) et n'accepte que le template : image exacte,
  `runAsNonRoot`, `drop: ALL`, pas de `hostPath`/`privileged`, labels
  obligatoires, `activeDeadlineSeconds` borné, pas de jeton monté dans les
  conteneurs de tenant, Secret exact de l'enfant ; `failurePolicy: Fail` ;
- `ResourceQuota` et `LimitRange` pour borner le nombre et la taille des pods
  d'enfants ;
- un journal des créations/suppressions (table `pod_requests`/`pod_hosts`),
  pas un journal de processus dans le dépôt (canon §6).

Si le cluster ne permet pas ce RBAC, la tranche 3 (clone en pod) est bloquée
et le clone reste sur `own-host` ; on ne donne pas au runner d'un agent un
`kubeconfig` pour contourner.

## 7. Carte Host d'un pod, abonnement et connexion du siège

### 7.1 Le bloc `policy.runtime`

Une carte Host de pod reste une carte Host : `responsible`, `policy`,
`tags`. Elle ajoute un bloc **données** `runtime` (nouveau, tranche 2) :

```yaml
type: Host
title: <hote-pod>                 # nom logique stable, pas le nom du pod
responsible: human:<admin>
tags: [pod]
policy:
  harnesses: [claude, codex]
  providers: [anthropic, openai]
  credential_modes: [subscription, api-key]   # subscription : voir 7.2
  shared: true
  tenants: [human:<alice>, human:<bob>]
  max_agents: 6
  lease_ttl: 120
  runtime:
    kind: pod                     # pod | pod-template
    namespace: <ns>               # DNS-1123, pas de secret
    workload: statefulset/<nom>   # ou deployment/<nom>, pour le runbook
    account_isolation: per-tenant-volume
    isolation_ref: "<org>/<depot>#<PR>"   # chart + témoin relus par le bureau
  resources:
    max_cgroup_memory_used: 80%
    max_cgroup_throttled: 20%
    min_disk_free: 2GiB
  workspaces:
    root: "~/work"                # ~ = home du tenant
```

Règles de `canon check` proposées (tranche 2, complétées en 5) :

- `runtime.kind` ∈ {`pod`, `pod-template`} ; `namespace` DNS-1123 ;
  `workload` non vide ; `account_isolation` ∈ {`per-tenant-volume`,
  `per-pod-empty-dir`} selon le type.
- `shared: true` et `tenants` non vide (comme aujourd'hui) ; `tenants` résolus
  en `Member` humains ; `~` dans les `workspaces` (pas de chemin absolu
  commun).
- `subscription` dans `credential_modes` **seulement si** `runtime.kind: pod`
  **et** `runtime.account_isolation: per-tenant-volume` **et**
  `runtime.isolation_ref` est une référence citable ; sinon erreur
  `host-pod-subscription-unproven`. C'est la traduction mécanique de
  l'exception 7.2, pas une déclaration libre.
- `runtime.kind: pod-template` : `shared: true`, `tenants` non vide,
  `max_agents: 1`, `volatile: true`, `credential_modes` sans `subscription`
  (un enfant ne reçoit pas de siège) ; le template ne se place pas comme un
  hôte normal.
- `resources` : les clés `max_load` sont acceptées mais **non évaluées** en
  mode pod (`load1` non mesuré) ; un avertissement le dit.
- `sandbox` et `runtime` peuvent coexister (un pod peut aussi lancer des Jobs
  de relecture), mais le chart doit alors inclure le superviseur de sandbox ;
  par défaut, un hôte pod ne déclare pas `sandbox`.

### 7.2 L'abonnement : décision à rouvrir (Q2 / 0033 §3)

**Le conflit est réel, et il doit être vu avant d'écrire le chart.**

- `src/ameesh/canon.py` : `SHARED_CREDENTIAL_MODES = ("api-key",)` et
  `host-shared-credential-mode` refusent `subscription` sur un hôte
  `shared: true` ; la décision Q2 (#213954, 12/11) dit « clés d'organisation
  seulement ; l'abonnement personnel reste sur le poste ».
- `0033` §3 : « les personas serveur utilisent les clés d'API de
  l'organisation ; les abonnements humains restent sur leurs appareils ».
- Le lot #7633 demande pourtant une carte Host de pod admettant `claude` et
  `codex` en `subscription` (sièges TPK). C'est une **nouvelle décision**, pas
  une conséquence de la décision 11.

**Exception proposée (Q2 bis), étroite :**

> Sur un **hôte pod** dont chaque tenant a son conteneur et son volume propre,
> un abonnement personnel peut être déclaré dans le dossier de comptes du
> tenant (0700, identifiants 0600) si, et seulement si :
> (a) le PVC n'est monté que dans le conteneur du tenant ;
> (b) le superviseur et les autres conteneurs ne le montent pas ;
> (c) le témoin d'isolation est vert sur ce pod et cette image ;
> (d) les conditions du fournisseur autorisent l'usage du siège dans un
> conteneur sans humain devant l'écran, pour cette personne et cet usage ;
> (e) la carte Host cite `runtime.isolation_ref` et
> `runtime.account_isolation: per-tenant-volume`.
> Sur un hôte partagé **VM**, Q2 et 0033 §3 restent inchangés : clés
> d'organisation seulement, abonnement sur le poste.

**Ce que l'exception ne prétend pas** : elle ne protège pas contre l'admin du
cluster (il peut monter le PVC) ; elle ne transforme pas un siège en compte
partageable ; elle ne vaut pas pour un enfant (§8). Si l'owner ou le bureau
refuse, le repli est `credential_modes: [api-key]`, et la partie
« abonnement » de #7633 n'est pas livrée : c'est une décision, pas un échec de
la note.

**Conditions du fournisseur** : `docs/profils/cluster.md` §6 demande de les
vérifier avant tout déploiement d'un abonnement sur un serveur. La note ne
les tranche pas ; la vérification est une condition de la tranche 5, avec la
personne et le bureau, pas une hypothèse de l'agent.

### 7.3 Connexion du siège par la personne

La connexion est **humaine et interactive** ; aucun agent ne saisit
d'identifiant, aucune clé ne transite par le canon, la base ou un courrier.

**Règle dure : aucun droit `pods/exec` sur le pod partagé.** Le RBAC
Kubernetes peut nommer un **pod** (`resourceNames`, y compris sur la
sous-ressource `pods/exec` ; voir [RBAC, « Referring to
resources »](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#referring-to-resources))
mais **pas un conteneur** : `kubectl exec` passe le conteneur en paramètre de
requête, hors de l'objet autorisé. Accorder `pods/exec` sur le pod partagé,
même « pour son conteneur », donnerait accès à **tous** les conteneurs du pod,
donc au compte d'une autre personne. C'est refusé ; le témoin le vérifie
(§5.3, contrôle 6).

**Pod de connexion dédié (chemin retenu).** La connexion se fait dans un pod
d'un seul conteneur, créé pour la session :

1. Le bureau marque le tenant `login` en base (geste signé, pas un agent) ; le
   runner de ce tenant rend son bail et cesse de claimer au battement suivant ;
   `runner_presence` montre l'état `login`. La garde `ameesh_tenant_claim_ok`
   empêche un autre runner de prendre le tenant.
2. Le bureau crée le pod de connexion : il monte **seulement** le PVC de la
   personne, tourne sous son UID/GID, sans superviseur, sans jeton de compte
   de service, sans autre volume, `readOnlyRootFilesystem: true`, `/tmp` en
   `emptyDir`, TTL ≤ 30 min (`activeDeadlineSeconds`). Il est épinglé au
   **même nœud** que le pod partagé (le bureau lit `spec.nodeName`), pour que
   le PVC `ReadWriteOnce` soit monté par les deux pods sans détachement.
3. Le droit est un Role `pods/exec` du namespace lié par
   `resourceNames: ["<pod-de-connexion>"]` — le pod n'a qu'un conteneur, donc
   la limite par pod suffit —, pour la durée de la session ; il est révoqué et
   le pod supprimé à la fin.
4. `kubectl exec -it <pod-de-connexion> -- sh` démarre sous l'UID du tenant,
   `HOME` sur son PVC, sans jeton Kubernetes.
5. Claude Code : `claude` puis `/login` ; l'URL s'ouvre sur le poste de la
   personne. Codex : `codex login` (flux appareil). Le harnais écrit
   `.credentials.json` / `auth.json` en 0600 sur le PVC.
6. Le bureau supprime le pod, efface l'état `login` ; le runner reprend au
   tour suivant et `runner_presence` publie `claude/primaire` (nom seulement).
   Aucun redémarrage du harnais n'est nécessaire.
7. **Si le PVC ne peut pas être monté à côté du pod partagé** (stockage qui
   refuse deux montages sur le même nœud, nœud imposé, pod de connexion
   interdit par la politique) : la personne est servie par un **pod dédié**
   (§2.2, D3) ; le pod de travail est arrêté, le pod de connexion monte seul le
   PVC, la personne se connecte, le pod de travail redémarre sur le même PVC
   (montages séquentiels, jamais simultanés). On ne retombe **jamais** sur
   `pods/exec` dans le pod partagé. Un **relais d'attache** (PTY exposé par le
   superviseur) n'est pas retenu : dans un pod, les conteneurs partagent le
   namespace réseau, donc un tenant peut écouter le port du relais ou un port
   voisin ; un relais sûr demanderait un namespace réseau par tenant,
   c'est-à-dire le pod dédié.

**Witness (contrôle 6, §5.3)** : aucun RoleBinding `pods/exec` ne vise le pod
partagé ; un `SelfSubjectAccessReview` `create pods/exec` nommant le pod
partagé rend `allowed: false` ; le Role du pod de connexion porte
`resourceNames` sur ce seul pod, qui n'a qu'un conteneur ; le compte de A ne
peut pas `exec` dans le pod de B ; les identifiants sont en 0600 et le dossier
de comptes en 0700.

**Sièges et conditions** : `claude` et `codex` en `subscription` sont deux
profils distincts, deux dossiers distincts, deux entrées `passwd` possibles
(même UID, plusieurs profils). Le mode `subscription` n'est pas un mode
`api-key` déguisé : le runner ne peut pas injecter une clé à la place ; il
choisit le profil `config_dir` et laisse le harnais utiliser le jeton OAuth
du dossier.

**En cas de perte du PVC ou de déplacement vers un pod sans le même volume**,
la personne se reconnecte sur la cible ; **aucun fichier d'identifiants n'est
copié** (voir §9).

## 8. Clone en pod (`avatar.children: pod`)

### 8.1 Ce qui existe, et ce qui manque

`src/ameesh/avatar_children.py` refuse aujourd'hui `children: pod` avec
`spawn-pod-unsupported` (B5b, hors du lot #7107) ; `children: own-host` place
l'enfant sur un hôte partagé **autre** que la lignée, avec un UID, un runner
et un worktree propres. Le pod doit apporter la même chose, avec un pod par
enfant et un compte rendu au parent.

### 8.2 Le pourvoyeur

Le parent (l'avatar) **ne parle jamais à l'API Kubernetes**. Il écrit une
demande dans une table (`pod_requests` : parent, enfant, harnais, profil,
TTL, outils, contexte, empreinte) ; un **pourvoyeur** du mesh, déclaré comme
système non-agent (`system:ameesh-pod-provisioner`, #6091), lit la demande et
crée le pod à partir d'un **template figé**.

- Le pourvoyeur est le seul composant avec le `ServiceAccount` de création
  (§6.5). Il ne lit aucun secret de tenant, n'`exec` pas, ne crée que des pods
  du template, dans le namespace déclaré.
- Il écrit, dans une transaction : `pod_hosts` (hôte enfant, template,
  parent, tenant, UID, runner, UID du pod, dates, état), `host_tenancy`
  (`shared: true`, `tenants: [tenant]`, `canon: "pod:<template>"`),
  `host_tenants` (liaison enfant) ; il met à jour `agent_registry.host` et
  `admitted_hosts` de l'enfant, ou les fait écrire par le `spawn` avant.
- Il supprime le pod et les lignes à l'échéance (TTL) ; il ne supprime jamais
  un pod d'un autre parent.
- Le `spawn` attend un **démarrage borné** (par exemple 120 s) : le premier
  battement de l'enfant ou un état d'échec ; au-delà, il annule la demande et
  nettoie. Le parent ne bloque pas sur le travail de l'enfant : il reçoit un
  accusé « enfant démarré » et les rapports par le canal existant.

### 8.3 L'hôte enfant

- L'hôte enfant est un **pod dédié** (un seul tenant) : un conteneur de
  superviseur (état machine, `host_resources`, `host_lifecycle`, porte) et un
  conteneur de runner d'enfant. Il ne peut pas être un simple runner de
  tenant sans superviseur (§2.2).
- Son `host` est unique et stable pour la vie du pod (par exemple
  `pod-<parent>-<enfant>-<8 hex>`), pas le nom du pod ; `host_policy` vient du
  template `pod-template`, pas d'une carte Host dans le canon ; le
  `canon_sync` est désactivé (`canon_sync_interval: 0`) et `host_tenancy` est
  écrit par le pourvoyeur. Un `pod_hosts` absent, expiré ou dont le pod UID ne
  correspond pas ferme la réclamation (`pod-host-unknown`, fail closed).
- `host_resources` du superviseur enfant : capacité du pod enfant (cgroup),
  pas de `hostPath` ; `lease_ttl` court (90 s), `volatile: true`,
  `max_agents: 1`.
- Le claim de l'enfant passe par `ameesh_tenant_claim_ok` (l'hôte enfant est
  `shared` avec un tenant) ; le runner d'enfant est lié à `host_tenants` ; le
  `beat` vérifie l'UID comme pour un tenant.

### 8.4 Le conteneur de l'enfant

- **UID neuf** dans une plage d'enfants (par exemple 30000+), **jamais** l'UID
  du parent ; entrée `passwd` propre ; `runAsUser` de l'enfant.
- **Système de fichiers neuf** : `emptyDir` avec `sizeLimit` pour le home et
  le worktree, `/tmp` séparé ; **aucun PVC du parent**, aucun volume du
  parent. L'enfant est éphémère : son travail durable est le rapport et les
  artefacts qu'il remonte, pas son disque.
- **Outils/MCP** : le profil `spawn_profiles` existant (#7034) est appliqué
  par le runner de l'enfant ; le template ne donne pas plus de droits ; un
  harnais qui ne prouve pas l'application refuse le démarrage
  (`spawn-tools-unsupported`), comme pour `own-host`.
- **Contexte** : `spawn_contexts` borné, filtré, chargé au démarrage ; le
  rapport enfant → parent (`child report`, `spawn_reports`, courrier) est
  réutilisé ; aucun nouveau canal.
- **Identifiants** : pas de copie du dossier d'abonnement du parent. Cible :
  un **relais de modèle** (le runner enfant obtient un jeton court lié à son
  bail/époque ; le relais ajoute la clé côté serveur et impute au compte du
  parent, Q6 #213955). Repli : une clé d'API d'organisation dans un Secret
  par enfant, que l'enfant peut lire (même modèle qu'un runner normal), avec
  un plafond de dépense. Un enfant en `subscription` est refusé par défaut :
  un siège n'est pas partageable entre deux UID, et le copier serait une
  fuite de credentials.
- **Cycle de vie** : `activeDeadlineSeconds` ≥ TTL + grâce ;
  `ttlSecondsAfterFinished` pour l'adoption ; SIGTERM → rapport final `expired`/
  `failed` ; le pourvoyeur supprime le pod et les lignes ; un enfant orphelin
  expire par son TTL même si le parent meurt ; un enfant ne survit pas à son
  TTL.
- **Coût** : le budget/compte du parent (Q6) ; le relais écrit
  `turn_costs` avec l'agent enfant et le compte de facturation du parent ;
  le plafond du parent s'applique. Le `spawn` refuse si le profil n'a pas de
  plafond/reserve.

### 8.5 `spawn-pod-unsupported` disparaît quand

- le pourvoyeur et le template sont déployés (chart) ;
- `pod_hosts`/`host_tenancy`/`host_tenants` de l'enfant sont écrits et
  gardés ;
- le runner enfant bat sous son UID, claim l'enfant, rapporte au parent ;
- le témoin enfant↔parent est vert (l'enfant ne lit ni le PVC ni les
  identifiants du parent) ;
- le nettoyage TTL est prouvé.

Tant qu'un de ces points manque, `children: pod` **refuse** et ne replie
jamais sur l'UID ou le PVC du parent (la règle de 5322 §3.6 reste).

## 9. Déplacement poste ↔ pod partagé ↔ pod dédié

### 9.1 Le pod est un hôte normal pour `releve: preference`

- La carte `Placement` de l'avatar ordonne : poste (`pc-…`) puis pod partagé
  puis, si elle existe, pod dédié. L'ordre est une **préférence** ; le poste
  reste rang 0, le pod attend `PREFERENCE_DWELL_S`/`RANK_DELAY_S`.
- La **présence** d'un pod exige : `host_resources` frais du superviseur,
  `host_lifecycle` `available`, porte ouverte, `canon_state` `ok`, verdict de
  placement `ok`, et **battement frais du runner du tenant**
  (`runner_presence`, §3.2 point 5 de 5322). Sur un hôte pod, c'est le
  couple superviseur + runner de tenant qui rend l'hôte présent.
- Le **claim** reste le bail CAS ; la garde
  `ameesh_tenant_claim_ok` s'applique (l'hôte pod est `shared`) ; le runner
  de tenant ne peut prendre que les agents de son `responsible`.
- `agent move` (#7108) reste le chemin immédiat : cible présente, tenant lié,
  profil de compte publié pour le harnais, pas de tour, pas de handoff, pas de
  `host_hold`, pas de bail source vivant ; écriture `agent.moved` +
  `conversation_moves` en une transaction ; la session native ne traverse
  pas ; le premier tour de la cible reconstruit le segment depuis le journal.
  Un `move-account-missing` est refusé **avant** tout effet.

### 9.2 Ce que le pod ajoute au préflight

- `runtime.kind: pod` et `account_isolation` connus ; le pod cible n'est pas
  un hôte enfant (`pod_hosts` interdit comme cible de `agent move`) ;
- le conteneur du tenant est **prêt** : PVC monté, `passwd`/UID conformes,
  `id -u` = `host_tenants.uid` (le battement le prouve), profil publié ;
- le superviseur est frais et la porte ouverte ; le `canon_state` de l'hôte
  pod est `ok` ;
- la carte Host du pod existe dans le canon (hôte statique) ; un hôte enfant
  n'a pas de carte Host et ne peut pas être cible.

### 9.3 Pod partagé ↔ pod dédié (même personne)

- **Même UID de personne** sur les deux hôtes : l'UID est une propriété de la
  personne (alloué dans le chart), pas de l'hôte ; `host_tenants` le réutilise
  sur chaque hôte. C'est ce qui permet de ré-attacher un PVC sans changer les
  propriétaires.
- **Ré-attachement du PVC** : si la classe de stockage le permet, le bureau
  détache le PVC du pod partagé (le conteneur du tenant est drainé/arrêté) et
  le monte dans le pod dédié ; ce n'est **pas une copie** : mêmes fichiers,
  même UID, même compte. Le `conversation_moves` reste le chemin de la
  conversation ; la session native peut être reprise car le stockage est le
  même (bonus, pas une garantie).
- **Si le PVC ne peut pas être ré-attaché** : le pod dédié reçoit un PVC
  neuf ; la personne se reconnecte (aucun fichier d'identifiants copié) ; le
  profil de compte est republié ; le `agent move` attend
  `move-account-missing` levé. Le déplacement est refusé tant que le compte
  n'est pas prêt, jamais replié.
- **Ordre du runbook** : drainer le tour source (frontière), libérer le bail,
  détacher le PVC, créer/attacher la cible, vérifier UID/PVC/compte,
  `agent move`, claim de la cible, premier tour avec segment, vérifier le
  journal. Le retour est le même dans l'autre sens. Le runbook est un
  document d'exploitation de la tranche 5/6, pas un geste d'agent.

### 9.4 Poste ↔ pod

- Le poste est `intermittent: true` ; à son extinction, son bail cesse d'être
  renouvelé et expire (TTL court de l'avatar, 90–120 s) ; le pod partagé
  claim au rang suivant.
- Au retour du poste, il attend `PREFERENCE_DWELL_S` (120 s) sans trou, le pod
  finit son tour et libère à la frontière ; le poste reprend ; l'ancien
  détenteur attend le holdoff avant de reprendre si le poste ne claim pas.
  C'est B2 (#7052), inchangé ; le pod n'ajoute que la présence du runner de
  tenant et le `host_resources` du superviseur.
- La conversation continue par le segment ; le cache natif reste sur chaque
  hôte ; aucun credential ne traverse.

### 9.5 Tests de la tranche

- Un test avec deux exécuteurs simulés (poste, pod) et la base réelle :
  préférence, dwell/holdoff, bail, `conversation_moves`,
  `move-account-missing`, pas de copie de session/credential (réutilise les doublures de
  `test_7108_avatar_b4.py` et `test_7106_hote_partage.py`).
- Un test `kind` pour la présence du pod (superviseur + battement) et le
  ré-attachement PVC si le stockage de test le permet ; sinon, documenter le
  repli « compte re-login ».
- La qualification TPK réelle est hors série ; elle rejoue le même scénario
  sur `tpk-stg`.

## 10. Découpage des étapes 2 à 6 de #7633

Chaque tranche est une unité d'au plus une journée-agent. Les estimations
sont des **propositions à réestimer par le bureau** ; la classe est celle du
canon §1 et de `ameesh review-class` sur les chemins réels. Les tranches 1 à 3
touchent les comptes/ secrets : **deux familles** de relecture ; les tranches
4 à 6 suivent la classe de leur code (au minimum une autre famille).

| Tranche | Étape #7633 | Contenu | Dépend de | Est. | Classe |
|---|---|---|---|---|---|
| **P1 — socle pod** | 2a, 6a | `policy.runtime` + `canon check` (pod, pod-template, `isolation_ref`) ; `tenancy.pod_plan()` et `host tenant add --pod` (plan, aucun geste root) ; `beat` vérifié par UID ; `canon_sync_once` saute les tenants ; chart squelette : superviseur, un tenant, PVC, ConfigMap passwd/group, `fsGroup` mode A, `securityContext` §6 ; `host_resources` mode pod (cgroup, pas de `load1`) ; `pod_hosts` minimal ; témoin `kind` (identité, montage, permissions) | cette note | 1 j | sensible (droits, comptes) — 2 familles |
| **P2 — pod partagé N tenants** | 2b, 6b | N conteneurs de tenant, N PVC, superviseur sans volume de tenant ; publication des profils ; `require_isolation`/`ameesh_tenant_claim_ok` sur pod ; garde « un seul superviseur » ; témoin complet (5 contrôles) en CI `kind` ; doc d'exploitation | P1 | 1 j | sensible (droits, comptes) — 2 familles |
| **P3 — carte Host pod + siège** | 5 | exception Q2 bis et validation `subscription` + `isolation_ref` ; `claude`/`codex` en `subscription` dans la carte ; procédure de connexion du siège par pod dédié d'un conteneur (`resourceNames`, jamais `pods/exec` sur le pod partagé) + état `login` du tenant ; témoin A↛B sur le compte et contrôle 6 ; vérification des conditions du fournisseur (décision owner) | P2 | 0,5–1 j | sensible (comptes, secrets) — 2 familles |
| **P4 — clone en pod** | 3 | pourvoyeur `system:ameesh-pod-provisioner` + `ServiceAccount`/RBAC/`ValidatingAdmissionPolicy`/quotas ; `pod_requests`, `pod_hosts`, `host_tenancy`/`host_tenants` enfant ; `children: pod` dans `avatar_children` ; template d'image/harnais ; relais ou clé d'API enfant (pas de siège) ; `spawn_profiles`/rapport ; TTL/nettoyage ; tests `kind` | P1, P2 | 1 j | sensible (droits, comptes, infra) — 2 familles |
| **P5 — déplacement** | 4 | présence pod dans `candidate_state` ; préflight `agent move` pod ; shared ↔ dedicated (PVC ré-attaché ou re-login) ; poste ↔ pod ; runbook ; tests base + `kind` | P2, P4 | 0,75–1 j | sensible (concurrence, comptes) — 2 familles |
| **P6 — chart de référence** | 6 | chart complet : NetworkPolicy, quotas, PDB, sauvegardes/snapshots, upgrade/rollback, `preStop`/grâce, labels, digest d'image, runbook de qualification ; témoin et documentation ; passation TPK/IMA | P2–P5 | 1 j | sensible (infra) — 1 famille + revue infra |
| **Q-TPK — qualification** | — | déploiement sur `tpk-stg`, RBAC, stockage, PSA, conditions des fournisseurs, témoin réel, connexion des sièges ; **hors série** (TPK/IMA) | P6 | ~1 j | TPK |

**Total série** : 5,25 à 6 j (P1–P6), plus ~1 j de qualification TPK. L'écart
avec les 3 j de #7633 vient du chart, du pourvoyeur, du témoin et de la
connexion du siège ; c'est le même type d'écart que 5322 §6 avait constaté
pour l'hôte partagé VM. Le pilote peut réduire le périmètre (par exemple
P3 sans abonnement, P5 sans pod dédié) s'il veut tenir 3 j ; il ne peut pas
tenir 3 j en gardant tout.

**Ordre recommandé** : P1 → P2 → P3 (peut être bloquée par la décision
d'abonnement) → P4 → P5 → P6 → Q-TPK. P3 et P4 peuvent se chevaucher sur des
agents différents ; P5 dépend des deux.

## 11. Risques, limites et décisions à prendre

### 11.1 Décisions

| # | Décision | Qui | Si non |
|---|---|---|---|
| **D1** | **Q2 bis** : abonnement personnel autorisé sur un hôte pod à volume par tenant, aux conditions §7.2 ; Q2/0033 §3 inchangés pour la VM | owner/bureau, après deux familles | `credential_modes: [api-key]` ; la partie abonnement de #7633 n'est pas livrée |
| **D2** | Conditions du fournisseur pour un abonnement dans un conteneur sans humain devant l'écran | owner/bureau + personne | pas d'abonnement dans le pod ; le siège reste sur le poste |
| **D3** | Le pod partagé accepte le namespace réseau/IPC commun pour les inactifs, avec runners sans port ; ou l'owner exige un pod par tenant (coût) | owner/pilote | pod dédié par avatar ; le coût et le nombre de pods changent |
| **D4** | L'admin du cluster peut monter un PVC : accepté pour un abonnement personnel ? | owner/sécurité | pas d'abonnement ; clés d'organisation seulement |
| **D5** | Visibilité mémoire sur un hôte partagé : évaluer par tenant, ou n'admettre que les personnes dont la mémoire est lisible par les admins | owner/canon | une carte Host partagée peut être refusée par `canon sync` ; le pod partagé est bloqué |
| **D6** | Enfant : relais de modèle (cible) ou clé d'API par enfant (repli) ; le coût est celui du parent (Q6 acquis) | pilote/bureau | repli clé d'API ; le relais est une tranche suivante |
| **D7** | Shared ↔ dedicated : PVC ré-attaché (même volume) ou re-login (PVC neuf, aucun fichier copié) | bureau/plateforme | re-login ; le déplacement attend le compte |
| **D8** | UID stable par personne entre les hôtes pod (recommandé) ou UID par hôte | bureau | UID par hôte ; le PVC n'est pas ré-attachable sans re-login |

### 11.2 Risques

- **PSA `restricted`** : le mode B (init root) est refusé ; le mode A
  (`fsGroup` partagé, pas de root) reste. Si le cluster refuse aussi
  `fsGroup`, la tranche 2 est bloquée ; il faut une classe de stockage qui
  pose l'UID au montage.
- **Stockage** : pas de `ReadWriteOnce`/snapshot/ré-attachement, ou pas de
  `fsGroup` ; le chart doit être testé sur `kind` et sur la classe TPK.
- **UID imposé par le namespace** (OpenShift, PSP) : les UID du chart doivent
  être dans la plage autorisée ; le `passwd` projeté doit suivre.
- **Un seul superviseur** : sans garde en base, un rollout qui chevauche peut
  ouvrir la porte à tort ; la tranche 2 doit la poser avant le multi-tenant.
- **Ajout d'un tenant = rollout** : un pod partagé redémarre tous les
  conteneurs ; le drain doit être prouvé (aucun tour perdu, baux rendus).
- **Réseau/IPC partagés** : limite assumée ; si un harnais écoute un port, le
  témoin le signale ; la parade est le pod dédié, pas une NetworkPolicy
  intra-pod.
- **Abonnement** : conditions du fournisseur, admin du cluster, Q2/0033 ;
  trois décisions distinctes, aucune ne se déduit du lot.
- **Pourvoyeur** : un RBAC trop large (secrets, exec) recréerait la
  compromission du cluster ; le template et l'admission sont la garde.
- **Secret d'API par enfant** : le nom est dérivé du label
  `ameesh.dev/child` (`ameesh-child-<label>`) et ne dépend pas du tenant.
  Un SA pourvoyeur compromis peut choisir ce label ; la VAP statique ne peut
  pas recouper `pod_requests`. La frontière est donc un webhook d'admission
  (P4, #7697) exécuté sous une identité distincte
  (`ameesh-pod-admission`), avec son template dans son image et un accès en
  lecture seule à `pod_requests` : il vérifie request-id, tenant, enfant,
  nom du pod et Secret exact, et refuse absence, discordance ou demande
  périmée ; il n'admet que l'identité réelle du ServiceAccount pourvoyeur
  (`system:serviceaccount:<namespace>:ameesh-pod-provisioner`) et refuse tout
  autre auteur ; `failurePolicy: Fail`. La VAP reste la seconde barrière. Le
  webhook se connecte avec `ameesh_pod_admission` (SELECT seul) et le
  pourvoyeur avec `ameesh_pod_provisioner` (UPDATE borné aux colonnes
  d'exploitation de `pod_requests`) : `deploy/sql/role-pod-provisioner.sql`
  et son contrat font de la source d'autorisation une ACL PostgreSQL, pas un
  drapeau applicatif. Le reste du mesh garde le rôle partagé `ameesh`
  (limite L155).
- **Estimation** : 3 j pour #7633 est sous-évalué ; le pilote doit
  réestimer ou réduire le périmètre.

### 11.3 Ce qui invaliderait la note

- Le cluster interdit un UID par conteneur ou un PVC par tenant (obligation
  d'un volume partagé `ReadWriteMany`) : le modèle §2 ne tient pas, il faut
  un pod par tenant ou un autre runtime.
- Le cluster interdit tout conteneur non-root avec un UID du chart (UID
  imposé) : le `passwd`/UID mapping et le PVC doivent être repris.
- Le cluster interdit au pourvoyeur de créer des pods : `children: pod`
  reste `spawn-pod-unsupported`.
- L'owner refuse D1/D4 : la carte Host reste `api-key` ; la partie
  abonnement est retirée du périmètre, le reste de la note tient.
- Le témoin de permissions échoue sur le stockage réel (mode 0777 imposé,
  `fsGroup` qui écrase les modes) : le mode de provisionnement doit changer
  ou le pod partagé est abandonné au profit du pod dédié.

## 12. Ce que la note ne fait pas

- Elle n'écrit aucun code, aucune migration, aucun manifeste appliqué.
- Elle ne déploie rien sur `tpk-stg` ; elle ne lit aucun secret.
- Elle ne tranche pas Q2/0033, les conditions du fournisseur, la visibilité
  mémoire ni le coût d'un pod par avatar : elle les nomme (D1–D8).
- Elle ne remplace pas les notes de #7106 (hôte partagé VM), #7107 (enfants
  `own-host`), #2202 (Jobs de relecture) ; elle s'appuie dessus.
- Elle ne promet pas l'isolation SQL par tenant (L155) ni l'isolation réseau
  entre conteneurs d'un même pod.

## 13. Références internes

- `docs/design/5322-avatar-humain.md` §3.4 (hôte partagé), §3.5
  (déplacement), §3.6 (enfants), §6 (découpage), Q2/Q6.
- `docs/HOST-HANDOFFS.md`, « Shared multi-tenant host (#7106, avatar B3) ».
- `docs/profils/cluster.md` §1 (modèle pod), §5 (volume), §6 (identifiants,
  abonnements), §7 (mise à jour), §8 (retour arrière).
- `docs/COMPTES.md` §2 (dossiers de comptes, 0700/0600, profils).
- `docs/design/etudes/2202-external-sandbox-turns.md` (Job borné, relais,
  aucune clé au Job).
- `docs/design/3167-placement-liberation.md` §4 (TPK) et §5 (ameesh ne
  programme pas de pods).
- `src/ameesh/tenancy.py`, `src/ameesh/avatar_children.py`,
  `src/ameesh/relocation.py`, `src/ameesh/election.py`,
  `src/ameesh/runner.py` (`resource_once`, `canon_sync_once`).
- `src/ameesh/migrations/0575_shared_host_tenants.sql`,
  `0576_tenant_account_profiles.sql`, `deploy/sql/role-superviseur.sql`.
- `deploy/k8s/statefulset.yaml` (profil cluster existant).
