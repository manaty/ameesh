# Hôte d'avatars en pod — squelette P1 (#7694, #7633)

Squelette **statique** d'un hôte pod ameesh : un conteneur de superviseur,
**un** conteneur de tenant, **un** PVC, la ConfigMap `passwd`/`group`, le
`fsGroup` neutre et les `securityContext` du design
[`docs/design/7633-hote-avatars-pod.md`](../../../docs/design/7633-hote-avatars-pod.md)
§2, §4 et §6. Il ne crée aucun Secret et n'est appliqué par aucun agent : le
rendu et l'application sont des gestes d'opérateur.

## Ce que le squelette contient

| Objet | Rôle |
|---|---|
| `StatefulSet/ameesh-pod-alice` (1 réplique) | `supervisor` (uid 10001, sans volume de tenant), `tenant-alice` (uid 2519), init `canon`, `canon-fetch` |
| `PersistentVolumeClaim/pvc-alice` | le PVC **du seul tenant**, monté dans son conteneur uniquement |
| `ConfigMap/ameesh-pod-passwd` | `/etc/passwd` et `/etc/group` projetés (subPath, lecture seule) |
| `ConfigMap/ameesh-pod-supervisor` | configuration du superviseur (hôte, canon, base, pas de secret) |
| `ConfigMap/ameesh-pod-tenant-alice` | configuration du tenant (hôte, tenant, runner, canon en lecture seule) |
| `ConfigMap/ameesh-pod-canon` | URL/branche du canon et intervalle de `git fetch` |
| `ServiceAccount/ameesh-pod` | aucun jeton monté, aucun Role vers ce pod |

**Aucun Service** : le pod n'expose aucun port (design §6.4). Aucun `Secret`,
`Role`, `RoleBinding`, `NetworkPolicy`, `ResourceQuota` ni `PDB` : ces objets
appartiennent aux tranches suivantes (P2 pour N tenants, P6 pour le chart de
référence).

## Invariants portés par ce chart

- **Un PVC par tenant, monté seulement dans son conteneur.** Pas de
  `ReadWriteMany`, pas de `hostPath`, pas de subPath partagé.
- **Un seul `fsGroup` neutre (10000) pour tout le pod**, jamais un `fsGroup`
  par tenant : Kubernetes n'en a qu'un par pod, et son passage récursif
  élargirait les modes `0700`/`0600` au groupe. Le GID neutre n'est pas la
  frontière : chaque PVC est isolé par le namespace de montage, puis par les
  modes (dossier de comptes `0700`, identifiants `0600`).
- **L'UID vient du chart**, pas d'un `useradd` : `tenancy.pod_plan()` le
  dérive de `human:<id>` (D8, UID stable par personne), `host_tenants.uid`
  l'enregistre, et le battement (`runner_presence`) refuse un processus dont
  l'UID réel diffère (`beat` vérifie `t.uid = os.getuid()`).
- **Aucun geste root, aucun jeton, aucun `hostPath`, aucun `privileged`** ;
  `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`,
  `readOnlyRootFilesystem: true`, `/tmp` en `emptyDir` par conteneur.
- **Le superviseur ne monte aucun volume de tenant** ; il publie
  `host_resources` avec la capacité **déclarée** du pod
  (`AMEESH_POD_MEMORY_BYTES`, `AMEESH_POD_CPU_CORES`) et jamais `load1`, qui
  est la charge du nœud (`resources.sample_pod`).
- **Le canon est alimenté par l'init/`canon-fetch` du superviseur** ; les
  conteneurs de tenant le lisent en lecture seule et `canon_sync_once` saute
  un runner de tenant.
- **Confiance Git bornée au dépôt du canon** (B1 de #44265, #44282/#44283) :
  le dépôt est cloné par l'init sous `10001` et lu par le tenant sous **son**
  UID ; sans `safe.directory`, Git refuse « detected dubious ownership », même
  en lecture seule. Le lecteur réel, `canon._git`, ajoute lui-même
  `-c safe.directory=<ce dépôt>` **seulement** dans un conteneur de tenant
  (`AMEESH_TENANT`) et pour le seul dépôt provisionné
  `/var/lib/ameesh/canon/repo` : tout autre dépôt, et tout hôte hors pod,
  gardent le refus de Git ; le filtrage des `GIT_CONFIG_*` par `_git_env`
  reste entier. Le conteneur du tenant porte en plus la paire
  `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=safe.directory`,
  `GIT_CONFIG_VALUE_0=/var/lib/ameesh/canon/repo` pour les git directs des
  outils du tenant — le chemin exact, jamais `*`, aucun fichier global. Le
  plan `host tenant add --pod` rend la même paire ;
  `deploy/k8s/pod-witness/temoin-canon.yaml` prouve la lecture sous un UID
  différent de celui du clone et le refus sans la paire.

## Instancier un tenant

1. Calculer le plan de la personne (aucun geste root, aucune écriture) :

   ```sh
   ameesh host tenant add human:alice --host pod-alice-exemple --pod
   ```

   Le plan rend le conteneur, le PVC, les ConfigMap `passwd`/`group` et la
   configuration du tenant, l'UID (`2519` pour `human:alice`), le `fsGroup`
   (`10000`) et la commande `--register` à lancer par le bureau. Le plan est
   un **document de liaison** ; le chart reste la source de vérité appliquée.

2. Recopier dans ce dossier les valeurs du plan — `human:alice` sert
   d'exemple : nom du conteneur (`tenant-alice`), PVC (`pvc-alice`), home
   (`/home/ameesh-alice`), UID, entrée `passwd`, nom de la ConfigMap de
   configuration. Le `StatefulSet`, les ConfigMap et le PVC doivent porter le
   **même** UID ; le témoin de la tranche P2 le vérifie, et le battement
   refuse un UID divergent.

3. Créer les Secret hors du dépôt (voir `secrets.exemple.yaml`), puis :

   ```sh
   kubectl kustomize deploy/k8s/pod-socle    # rendu relu, rien appliqué
   kubectl apply -k deploy/k8s/pod-socle     # geste de l'opérateur
   ```

4. Enregistrer la liaison **avec l'UID du chart** :

   ```sh
   ameesh host tenant add human:alice --host pod-alice-exemple --pod \
       --register --uid 2519 --actor human:<admin>
   ```

## Carte Host correspondante (canon)

La fiche décrit le runtime, pas un secret. `isolation_ref` cite le chart et
son témoin ; en P1 la carte reste en `api-key` (aucun siège : P3 rouvre
Q2 bis). Le nom `title` est celui de `AMEESH_HOST`, pas `metadata.name`.

```yaml
type: Host
title: pod-alice-exemple
responsible: human:<admin>
tags: [pod]
policy:
  harnesses: [claude, codex]
  providers: [anthropic, openai]
  credential_modes: [api-key]          # subscription : tranche P3 (Q2 bis)
  shared: true
  tenants: [human:alice]
  max_agents: 6
  lease_ttl: 120
  runtime:
    kind: pod
    namespace: ameesh
    workload: statefulset/ameesh-pod-alice
    account_isolation: per-tenant-volume
    isolation_ref: "manaty/ameesh#7694"   # chart + témoin relus
  resources:
    max_cgroup_memory_used: 80%
    max_cgroup_throttled: 20%
    min_disk_free: 2GiB
  workspaces:
    root: "~/work"
```

`canon check` refuse un hôte `runtime.kind: pod` qui n'est pas `shared`,
sans `tenants`, sans `isolation_ref`, ou dont `account_isolation` n'est pas
`per-tenant-volume` ; il avertit si `max_load` est déclaré (non mesuré sur un
pod). Un `runtime.kind: pod-template` exige `max_agents: 1`, `volatile: true`,
`account_isolation: per-pod-empty-dir` et aucun `subscription` : il n'est
jamais un hôte plaçable.

## Tranches livrées et où les trouver

Ce squelette P1 reste la référence pas à pas (un tenant, objets minimaux) ; il
n'est pas le chart d'exploitation.

- **P2** : [`../pod-shared/`](../pod-shared/README.md) — N conteneurs de
  tenant, N PVC, publication des profils, bail « un seul superviseur »,
  témoin complet ;
- **P3** : carte Host pod **sans abonnement** (décision owner du 20/11) :
  `credential_modes: [api-key]`, `host-pod-subscription-unproven` refuse
  `subscription` et `allow_subscription` ; connexion du siège par pod dédié,
  jamais `pods/exec` sur ce pod partagé ;
- **P4** : pourvoyeur d'enfants ([`../pod-provisioner/`](../pod-provisioner/README.md),
  `pod_hosts`, RBAC, admission, TTL) ;
- **P5** : déplacement poste ↔ pod partagé ↔ pod dédié, runbook
  [`docs/ops/deplacement-hote-pod.md`](../../../docs/ops/deplacement-hote-pod.md) ;
- **P6** : chart de référence complet (`../pod-shared/` : NetworkPolicy,
  quota, PDB, `snapshot.py`, digest d'image, `preStop` et grâce, labels) et
  runbook de qualification/passation TPK-IMA
  [`docs/ops/qualification-hote-pod.md`](../../../docs/ops/qualification-hote-pod.md).

Le témoin `kind` de la tranche P1 (identité, montage, permissions) est dans
[`deploy/k8s/pod-witness/`](../pod-witness/) ; il porte aussi, depuis #7695,
le superviseur, le canon et le contrôle base pour le chart `pod-shared`, et
depuis #7699 le contrôle du chart de référence (quota, PDB, digest, preStop).
