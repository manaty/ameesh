# Hôte pod partagé à N tenants — chart de référence (#7695 P2, #7699 P6)

Chart d'un hôte pod ameesh **partagé** : un superviseur et N conteneurs de
tenant dans le même pod, **un PVC par tenant**, un seul `fsGroup` neutre, et
les objets d'exploitation de la tranche P6 (réseau, quota, PDB, sauvegarde,
mise à jour, préStop, labels, image par digest). Il complète le squelette P1
([`../pod-socle/`](../pod-socle/README.md)) pour les tranches P2 et P6 du
design [`7633-hote-avatars-pod.md`](../../../docs/design/7633-hote-avatars-pod.md)
§2–§6 et §10. Le rendu et l'application sont des gestes d'**opérateur** :
aucun agent n'applique ce chart, et aucun secret n'y figure.

Le runbook de qualification, la mise à jour/retour arrière et la passation
TPK/IMA sont dans
[`docs/ops/qualification-hote-pod.md`](../../../docs/ops/qualification-hote-pod.md).
La qualification réelle sur `tpk-stg` (Q-TPK) est **hors série** : le chart
livre les manifestes et la procédure, pas le déploiement.

## Objets rendus

| Objet | Rôle |
|---|---|
| `StatefulSet/ameesh-pod-<hôte>` (1 réplique) | `supervisor` (uid 10001, sans volume de tenant), un conteneur `tenant-<slug>` par tenant, init `canon`, sidecar `canon-fetch` ; `preStop` de drain, grâce bornée |
| `PersistentVolumeClaim/pvc-<slug>` | le PVC **du seul tenant**, monté uniquement dans son conteneur |
| `ConfigMap/ameesh-pod-passwd` | `/etc/passwd` et `/etc/group` projetés (subPath, lecture seule), dérivés des plans |
| `ConfigMap/ameesh-pod-supervisor` | configuration du superviseur (hôte, canon, base, capacité déclarée) |
| `ConfigMap/ameesh-pod-tenant-<slug>` | configuration du tenant ; `accounts` ne porte que des **noms** de profil |
| `ConfigMap/ameesh-pod-canon` | URL, branche et intervalle de `git fetch` du canon |
| `NetworkPolicy/…-default-deny` | entrée refusée, sortie refusée par défaut (design §6.4) |
| `NetworkPolicy/…-egress-dns` | DNS vers le sélecteur DNS du cluster (53 UDP/TCP) |
| `NetworkPolicy/…-egress-<règle>` | une politique additive par sortie déclarée (base, canon, fournisseurs…) |
| `ResourceQuota/…-quota` | 1 pod, N PVC, somme des ressources du chart, 0 Service/Ingress |
| `PodDisruptionBudget/…-pdb` | `minAvailable: 1` : pas d'éviction volontaire sans décision de l'opérateur |
| `ServiceAccount/ameesh-pod` | aucun jeton monté, aucun Role vers ce pod |

**Aucun Service** : le pod n'expose aucun port (design §6.4). Les `Secret`
sont créés à part, jamais dans `kustomization.yaml` (voir
[`secrets.exemple.yaml`](secrets.exemple.yaml)).

## Invariants

- **Un PVC par tenant, monté seulement dans son conteneur** ; un
  `ReadWriteOnce` par personne, pas de `ReadWriteMany` ni de `hostPath`.
- **Un seul `fsGroup` neutre (`10000`) pour tout le pod**, jamais un
  `fsGroup` par tenant : Kubernetes n'en a qu'un par pod et son passage
  récursif élargirait les modes `0700`/`0600` au groupe. Le GID neutre n'est
  pas la frontière : chaque PVC est isolé par le namespace de montage, puis
  par les modes.
- **L'UID vient du chart**, de `tenancy.pod_plan()` (`human:<id>` → UID
  stable) ; `host_tenants.uid` l'enregistre et le battement refuse un
  processus dont l'UID réel diffère.
- **Le superviseur ne monte aucun volume de tenant** ; il publie la capacité
  **déclarée** du pod (`AMEESH_POD_MEMORY_BYTES`, `AMEESH_POD_CPU_CORES`) et
  jamais `load1`, qui est la charge du nœud.
- **Un seul superviseur vivant par hôte pod** : la migration
  `0599_pod_supervisor_lease.sql` crée `pod_supervisor_leases` (bail, epoch,
  échéance) ; le détenteur seul peut écrire `host_resources` (déclencheur
  `BEFORE INSERT`). `replicas: 1` est la politesse du chart, le bail SQL est
  l'arbitre ; un second superviseur reste en veille (pas de relevé de
  ressources, pas de synchronisation du canon, pas de scan de libération).
- **Profils de compte publiés par le battement** : noms seulement
  (`harnais/compte`), et seulement si le profil est dans le home du tenant
  (`tenancy.pod_profile_problem`) ; `ameesh host tenant list` les affiche
  (`profiles=…`). Les identifiants vivent sur le PVC du tenant.
- **Aucun geste root, aucun jeton, aucun `hostPath`, aucun `privileged`** ;
  `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`,
  `readOnlyRootFilesystem: true`, `/tmp` en `emptyDir` par conteneur.

## Image épinglée par digest (P6)

`host.exemple.yaml` porte `image` (dépôt) et `image_digest`
(`sha256:<64 hex>`) ; le rendu écrit `dépôt@sha256:…` dans **tous** les
conteneurs, init et sidecar. `render.py` refuse :

- un digest absent quand `require_digest` est vrai (défaut) ;
- un digest mal formé ;
- un tag dans `image` quand un digest est posé (deux références brouillent le
  retour arrière).

`require_digest: false` n'est admis que sur un banc local ; le runbook de
qualification impose le digest. `kustomization.yaml` n'a **pas** de bloc
`images:` : il écraserait le digest.

## Labels et sélecteur immuable

Le sélecteur du `StatefulSet` ne porte que `app.kubernetes.io/name` et
`app.kubernetes.io/instance`. Les labels de métadonnées ajoutent
`component`, `part-of`, `managed-by`, `version` et `ameesh.dev/host` : un
changement de digest (donc de version) change les métadonnées, **jamais** le
sélecteur, qui est immuable dans un `StatefulSet`. Les PVC portent en plus
`ameesh.kubernetes.io/tenant`.

## Réseau : entrée refusée, sortie déclarée

Le pod n'écoute rien ; la `NetworkPolicy` `…-default-deny` refuse **entrée et
sortie**, puis des politiques additives ouvrent le DNS et les sorties
déclarées dans `host.exemple.yaml` :

```yaml
network:
  dns:
    namespace: kube-system
    pod_labels: {k8s-app: kube-dns}
  egress:
    - name: base
      to:
        - ipBlock: {cidr: 10.20.0.0/24}   # CIDR réel de la base
      ports:
        - {protocol: TCP, port: 5432}
    - name: fournisseurs
      to:
        - ipBlock: {cidr: 0.0.0.0/0}      # 443 seulement, métadonnées exclues
      ports:
        - {protocol: TCP, port: 443}
```

- Une règle `0.0.0.0/0` reçoit automatiquement `except: [169.254.0.0/16]`
  (métadonnées d'instance) ; une règle plus étroite n'est pas élargie.
- Une NetworkPolicy standard **ne filtre pas les domaines** : les
  fournisseurs se déclarent par CIDR ou par le pair que le CNI expose.
- Elle **ne filtre pas l'intra-pod** (les conteneurs partagent le namespace
  réseau) : c'est la limite assumée du design §5.1, pas une garde.
- L'**application** dépend du CNI. `kind` accepte les objets mais ne prouve
  pas le filtrage ; Q-TPK le qualifie (voir le runbook).

## Quota et PDB

Le chart suppose **un namespace par pod partagé** (les ConfigMap
`ameesh-pod-*` y ont des noms fixes). La `ResourceQuota` calcule depuis le
chart : `pods: 1`, `persistentvolumeclaims: N`, `requests.storage` (somme des
PVC), `requests.cpu/memory` et `limits.cpu/memory` (superviseur, init,
sidecar, tenants), `services: 0`, `count/ingresses.networking.k8s.io: 0`,
`count/statefulsets.apps: 1`. `quota.hard` ajoute des clés explicites (par
exemple `secrets: "20"`). Un pod de débogage oublié est refusé : c'est voulu.

Le `PodDisruptionBudget` vaut `minAvailable: 1` sur une seule réplique :
`disruptionsAllowed` est donc `0`. Un drain de nœud attend ; l'opérateur
draine d'abord les agents (`ameesh host drain`), supprime le pod
explicitement, puis agit sur le nœud. `pdb.max_unavailable` est accepté au
lieu de `min_available`, jamais les deux.

## Sauvegardes et snapshots (P6)

Le chart ne porte pas de `VolumeSnapshot` statique : un snapshot est daté et
créé à la demande, sinon chaque `kubectl apply` en créerait un. L'outil
[`snapshot.py`](snapshot.py) rend, depuis le même `host.exemple.yaml` :

```sh
# un VolumeSnapshot par PVC de tenant, horodaté (UTC)
./deploy/k8s/pod-shared/snapshot.py \
    --class ameesh-pod-snapshots --stamp 20261120t190000z > snapshots.yaml
kubectl -n <namespace> apply -f snapshots.yaml

# plus tard, après arrêt du pod et suppression du PVC : PVC du même nom
./deploy/k8s/pod-shared/snapshot.py --restore \
    --stamp 20261120t190000z > restore-pvcs.yaml
kubectl -n <namespace> apply -f restore-pvcs.yaml
```

La `VolumeSnapshotClass` est **cluster-scoped** et dépend du pilote CSI :
voir [`snapshotclass.exemple.yaml`](snapshotclass.exemple.yaml) (hors
kustomization, geste d'opérateur, `deletionPolicy: Retain`). Sans
`--class`, le script omet la classe et le cluster applique sa classe par
défaut. La procédure complète, la rétention et la vérification après
restauration (témoin 1 rejoué, même UID) sont dans le runbook.

## Mettre à jour et revenir en arrière (P6)

1. Construire/pousser l'image, noter son digest, mettre `image_digest` et
   `version` dans `host.exemple.yaml`.
2. `./render.py --write`, relire le diff (le sélecteur ne doit **pas**
   changer), `./render.py --check`, `kubectl kustomize`.
3. Drainer les agents (`ameesh host drain <hôte> --for <durée>`), puis
   `kubectl apply -k .` et `kubectl rollout status statefulset/…`.
4. Vérifier : `ameesh host tenant list`, le profil publié, le témoin 1, le
   `preStop` dans les journaux, puis `undrain`.

Le `StatefulSet` garde `revisionHistoryLimit: 5` et
`updateStrategy.rollingUpdate.partition: 0` : `kubectl rollout undo
statefulset/<nom>` revient au template précédent (digest précédent), à
condition de garder l'image et la compatibilité de schéma. Une migration de
base n'est pas annulée par un rollback Kubernetes : le runbook impose la
compatibilité descendante ou une restauration.

## Arrêt : `preStop` et grâce

`terminationGracePeriodSeconds` vaut `grace_period` (≥ 120 s, défaut 120).
Chaque conteneur `supervisor`/`tenant-<slug>` porte un `preStop` qui envoie
`SIGTERM` à PID 1 (`tini`, qui le relaie au runner) puis attend que le
processus sorte, au plus `prestop_wait` (défaut 90 s). Le runner draine ses
tours, rend ses baux et sort ; `AMEESH_DRAIN_SECONDS` (défaut 80 s) est plus
court que l'attente pour que le drainage se termine avant la fin de la grâce.
Si l'attente expire, le `SIGTERM` du kubelet borne la fin et le `SIGKILL` de
la grâce reste le dernier recours. Un `prestop_wait: 0` désactive le hook
(banc local seulement).

## Rendre et appliquer

`host.exemple.yaml` est la **source** ; `rendered/` est généré et versionné.
Un namespace par pod partagé (les ConfigMap `ameesh-pod-*` ont des noms
fixes) : décommenter `namespace:` dans `kustomization.yaml` et le poser.

```sh
cd deploy/k8s/pod-shared
./render.py --check                 # rendu à jour ? (code 1 sinon)
./render.py --write                 # régénère rendered/, relire le diff
kubectl kustomize .                 # rendu complet, rien appliqué
kubectl apply -k .                  # geste de l'opérateur
```

`--check` est aussi la commande déclarée dans `.ameesh/generated-files.json` :
une modification de `render.py`, de `host.exemple.yaml` ou d'un plan sans
régénération est refusée. Ne pas éditer `rendered/` à la main.

Pour un contrôle ciblé sans cluster : `render.py --stdout --kind
NetworkPolicy` (répétable) rend uniquement ces objets ; `--stdout` seul rend
un flux YAML multi-documents applicable.

## Ajouter un tenant

1. Obtenir le plan (aucun geste root, aucune écriture) :

   ```sh
   ameesh host tenant add human:carol --host pod-alice-exemple --pod
   ```

   Le plan donne l'UID (dérivé de `human:carol`), le conteneur
   (`tenant-carol`), le PVC (`pvc-carol`), le home, l'entrée `passwd` et la
   commande `--register`.

2. Ajouter le tenant dans `host.exemple.yaml` (un `uid:` explicite seulement
   si l'on veut forcer un UID hors plan) :

   ```yaml
   tenants:
     - tenant: human:alice
     - tenant: human:bruno
     - tenant: human:carol
   ```

3. Régénérer et relire le diff, puis vérifier :

   ```sh
   ./render.py --write
   git diff -- deploy/k8s/pod-shared
   ./render.py --check
   ```

4. Créer le Secret du tenant **hors du dépôt** (un Secret par tenant, monté
   seulement dans son conteneur) :

   ```sh
   kubectl -n <namespace> create secret generic ameesh-harness-carol \
       --from-env-file=carol.env        # fichier 0600
   ```

5. Appliquer, attendre le déploiement, puis enregistrer la liaison **avec
   l'UID du chart** :

   ```sh
   kubectl apply -k .
   kubectl -n <namespace> rollout status statefulset/ameesh-pod-alice
   ameesh host tenant add human:carol --host pod-alice-exemple --pod \
       --register --uid <uid-du-plan> --actor human:<admin>
   ameesh host tenant list --host pod-alice-exemple
   ```

   `--register` exige que le canon déclare déjà l'hôte `shared` avec ce
   tenant (`ameesh canon sync`), et le battement vérifie ensuite l'UID. Un
   profil publié n'apparaît qu'une fois le runner du tenant passé.

## Retirer un tenant

1. Arrêter/drainer ses agents (`ameesh host drain`, ou déplacement) et
   attendre qu'il ne reste plus de bail ni de tour : `host tenant remove` est
   refusé tant qu'un agent du tenant est encore sur l'hôte.
2. Retirer l'entrée de `host.exemple.yaml`, régénérer (`--write`), relire,
   appliquer.
3. Retirer la liaison : `ameesh host tenant remove human:carol --host …`
   (avec `--actor` sur un terminal non lié).
4. Le PVC `pvc-carol` n'est **pas** supprimé par `kubectl apply` : sa
   suppression est une décision d'opérateur, destructive, à faire après
   sauvegarde. Le chart ne pose pas de `volumeClaimTemplates`, donc un
   redéploiement ne recrée ni ne détruit les PVC.

## Cycle de vie, drainage, incident

- Un changement de `host.exemple.yaml` change le template du `StatefulSet` :
  le `RollingUpdate` à **une** réplique redémarre **tous** les conteneurs du
  pod (tous les tenants) en même temps. À planifier comme une fenêtre de
  maintenance : `ameesh host drain <hôte> --for <durée>` d'abord, puis
  appliquer, puis `undrain`.
- Le bail superviseur a une échéance de trois passes de ressources au moins
  (plancher 180 s ; `max(180, 3 × resource_interval)`) : un pod recréé
  reprend la main à l'expiration, avec une **époque neuve**. Un ancien
  détenteur ne peut plus renouveler ni publier.
- `kubectl rollout status statefulset/…` et les événements du namespace sont
  la première lecture ; `ameesh host tenant list` montre la présence et les
  profils. Les journaux par conteneur :
  `kubectl logs <pod> -c supervisor|tenant-<slug>|canon-fetch`.
- **Relevé refusé** (`host_resources` en erreur) : le `runner_id` n'est pas
  le détenteur vivant du bail. Lire `pod_supervisor_leases` (détenteur,
  époque, échéance) ; ne pas contourner le déclencheur, corriger le
  superviseur ou attendre l'expiration.
- **UID divergent** : le processus du conteneur ne tourne pas sous l'UID du
  plan (`host_tenants.uid`) ; le battement refuse la présence. Vérifier le
  `securityContext` rendu et le plan, régénérer plutôt que corriger à la main.
- **Profils absents** : `pod_profile_problem()` refuse un `config_dir` ou un
  `key_file` hors du home du tenant sur un pod ; un profil sans identifiant
  sur le PVC n'est jamais choisi. Les noms publiés ne sont pas des secrets.
- **PVC `Pending`** : la StorageClass est probablement
  `WaitForFirstConsumer` ; le pod consommateur doit être planifié avant que
  le PVC passe `Bound` (le témoin kind crée le consommateur d'abord).

## Témoin et CI

Le témoin complet est
[`../pod-witness/preuve-isolation-comptes.sh`](../pod-witness/preuve-isolation-comptes.sh) :
identité, montage, permissions, superviseur, puis **base** (PostgreSQL
migré, 0599 comprise) ; le canon (#7694) est rejoué en plus. Depuis #7699, le
**contrôle de référence** applique les objets P6 du chart dans un namespace
jetable, prouve que la quota refuse un second pod, que le PDB compte 1 pod et
0 interruption, et vérifie le digest, le `preStop` et les labels dans le
rendu. Il rend `77` si `kind`/`kubectl`/`docker`/PyYAML manque ou si
`AMEESH_TEST_DSN` n'est pas posé : **non-concluant, jamais vert**. L'effet
réel de la NetworkPolicy n'est pas prouvé par kind (CNI) : Q-TPK le qualifie.

```sh
AMEESH_TEST_DSN=postgresql://… ./deploy/k8s/pod-witness/preuve-isolation-comptes.sh
```

Le workflow **manuel** [`.github/workflows/pod-kind.yml`](../../../.github/workflows/pod-kind.yml)
installe kind/kubectl épinglés, démarre PostgreSQL 17 et exécute le témoin
complet. Il n'a pas de déclencheur `push`/`pull_request` : la CI d'un lot
tourne sur la tête gelée (canon §3/§11 bis), le pilote le lance à la main.

## Ce que ce chart ne fait pas

- Pas de connexion de siège ni de `pods/exec` : la connexion P3 passe par un
  pod dédié (design §7.3).
- Pas d'isolation réseau/IPC entre conteneurs d'un même pod, ni de protection
  contre l'admin du cluster : limites du design §5.1.
- Pas de fournisseur de pods enfants (`pod_hosts`, RBAC, admission, TTL) :
  tranche P4, dans `deploy/k8s/pod-provisioner/`.
- Pas de déploiement ni de qualification sur TPK : le runbook et la passation
  sont prêts, Q-TPK est un lot TPK/IMA hors série.

## Fichiers

- `host.exemple.yaml` : source du chart (hôte, image et digest, réseau,
  quota, PDB, canon, tenants) ;
- `render.py` : rendu déterministe, `--check`/`--write`/`--stdout`/`--kind` ;
- `rendered/` : `configmaps.yaml`, `pvcs.yaml`, `statefulset.yaml`,
  `networkpolicy.yaml`, `resourcequota.yaml`, `pdb.yaml` générés ;
- `snapshot.py` : snapshots et PVC de restauration (opérateur) ;
- `snapshotclass.exemple.yaml` : forme de la classe CSI, hors kustomization ;
- `kustomization.yaml` : assemblage (pas de bloc `images:`) ;
- `serviceaccount.yaml` : compte de service sans jeton ni droit ;
- `secrets.exemple.yaml` : **forme** des Secret attendus, jamais remplie ;
- `README.md` : ce document.
