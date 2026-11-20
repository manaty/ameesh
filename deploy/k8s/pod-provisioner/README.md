# Pourvoyeur d'enfants pod (#7697, P4 de #7633)

Un avatar `avatar.children: pod` ne parle jamais à l'API Kubernetes. Il écrit
une **demande** (`pod_requests`) ; un composant du mesh,
`system:ameesh-pod-provisioner` (#6091), la lit, crée un pod à partir du
template figé [`template.yaml`](template.yaml), puis écrit l'identité de
l'hôte enfant dans **une transaction** : `pod_hosts`, `host_tenancy`,
`host_tenants`. À l'échéance, il supprime le pod et les liaisons.

Design : [`docs/design/7633-hote-avatars-pod.md`](../../../docs/design/7633-hote-avatars-pod.md)
§8. Décision D6 (pilote, 20/11) : **repli clé d'API d'organisation par
enfant** ; le relais de modèle viendra dans une tranche suivante.

## Ce que le pourvoyeur ne fait pas

- Il ne lit **aucun Secret** : il monte le Secret de l'enfant par son nom
  (`ameesh-child-<slug>`, déterministe) ; l'enfant est le seul à le lire.
- Il n'`exec` pas, ne lit pas les logs, ne crée ni PVC ni Deployment, ne
  touche pas aux pods d'un autre parent : avant de supprimer, il vérifie les
  labels `ameesh.dev/host` et `ameesh.dev/parent`, et refuse
  (`pod-owner-mismatch`) au moindre écart.
- Il ne copie jamais le dossier d'abonnement du parent. Un parent en
  `subscription` est refusé (`spawn-pod-credential`) ; seul `api-key` est admis
  pour un enfant pod (design §8.4).
- Il ne crée pas de `Job` : le pod enfant est un `Pod` nu. `ttlSecondsAfterFinished`
  est un champ de `Job`, pas de `Pod` ; le TTL est tenu par
  `activeDeadlineSeconds` (arrêt par le kubelet) **et** par le nettoyage du
  pourvoyeur (suppression des lignes et du pod). Un pod orphelin expire par
  son `activeDeadlineSeconds` même si le parent meurt.

## Objets

| Objet | Rôle |
|---|---|
| `Deployment/ameesh-pod-provisioner` | le processus `ameesh pod-provisioner` (1 réplique) |
| `ConfigMap/ameesh-pod-provisioner-template` | le template figé, généré par kustomize |
| `ServiceAccount/ameesh-pod-provisioner` + `Role`/`RoleBinding` | pods et ConfigMap seulement, dans le namespace |
| `ServiceAccount/ameesh-child` | aucun droit, aucun jeton monté |
| `ValidatingAdmissionPolicy` + binding | refuse un pod hors template même si le SA est compromis : deux conteneurs nommés, commandes figées, aucun init/éphémère, aucun Secret hors `ameesh-db` et du Secret `ameesh-child-*` de l'enfant |
| `ResourceQuota/ameesh-pod-children` | borne le nombre et les ressources des enfants |
| `template.yaml` | 2 conteneurs (`supervisor`, `child`), `emptyDir`, UID enfant 30000+, aucun PVC/hostPath |

Le template est **la** source du pod ; le pourvoyeur refuse tout placeholder
inconnu, tout `hostPath`/PVC, tout conteneur privilégié ou qui escalade, toute
image autre que `{{IMAGE}}`, et exige `AMEESH_POD_UID` depuis
`metadata.uid` (garde `pod-host-unknown`).

## Déployer

1. **Déclarer le système non-agent** (une fois, dans la base du mesh) :

   ```sh
   agent-mesh work external-executor --declare system:ameesh-pod-provisioner \
       --responsible human:<pilote-ou-bureau> --reason "#7697 enfant pod"
   ```

2. **Secret de la base** (jamais dans un dépôt) :

   ```sh
   kubectl -n ameesh create secret generic ameesh-db \
       --from-literal=password='…'       # fichier 0600
   ```

3. **Appliquer le namespace et les objets** (geste d'opérateur) :

   ```sh
   kubectl create namespace ameesh --dry-run=client -o yaml | kubectl apply -f -
   kubectl apply -k deploy/k8s/pod-provisioner
   kubectl -n ameesh rollout status deployment/ameesh-pod-provisioner
   ```

4. **Vérifier le heartbeat** (le template est enregistré par le pourvoyeur à
   chaque passe ; sans heartbeat frais, `children: pod` refuse
   `spawn-pod-unsupported`) :

   ```sh
   kubectl -n ameesh logs deployment/ameesh-pod-provisioner --tail=20
   ameesh pod-provisioner --templates
   ```

5. **Créer le Secret d'API de l'enfant** avant le premier `spawn` (nom exact
   dans `placement.describe()`, ou `ameesh host tenant list` après démarrage) :

   ```sh
   kubectl -n ameesh create secret generic ameesh-child-<slug> \
       --from-literal=api-key='…'        # clé d'organisation, plafond fournisseur
   ```

6. **Prouver sur kind** (facultatif, geste de bureau) : le lot ne livre pas
   de script kind ; les contrôles sont ceux du bureau — appliquer
   `kubectl apply -k deploy/k8s/pod-provisioner`, vérifier
   `kubectl auth can-i --list` du SA enfant (aucun droit), puis soumettre un
   pod hors template (refus VAP) et un pod conforme (admis, quota respecté).

## Cycle de vie

- `spawn` écrit l'enfant **et** la demande dans la même transaction, puis
  attend 120 s (premier battement ou état d'échec). Au-delà : demande
  `cancelled`, ligne enfant nettoyée, `spawn-pod-start-timeout`.
- Le pourvoyeur réclame une demande (`requested → claimed`), crée ConfigMap
  puis Pod, lit `metadata.uid`, et écrit `pod_hosts`/`host_tenancy`/
  `host_tenants` + `running` en une transaction. Si l'enfant a disparu
  entre-temps, il supprime le pod et échoue la demande.
- Un `claimed` plus vieux que 5 min est remis en file (`reclaim_stale`) : un
  pourvoyeur mort ne bloque pas la demande.
- À l'échéance (`pod_requests.expires_at`, `pod_hosts.expires_at`), le
  nettoyage supprime le pod et la ConfigMap, retire `host_tenants` et
  `host_tenancy`, et laisse `pod_hosts` en `deleted` (tombeau : un ancien pod
  ne peut pas se réclamer d'un hôte supprimé).

## Garde de réclamation

Le runner de l'enfant publie `runner_presence.pod_uid`
(`AMEESH_POD_UID`, Downward API). `ameesh_tenant_claim_ok` exige, pour un hôte
présent dans `pod_hosts`, une ligne `running` non expirée **et** un battement
dont le `pod_uid` est celui du pod provisionné. Un pod recréé a un UID neuf :
l'ancien ne réclame plus (`pod-host-unknown`, fail closed). Les hôtes non pod
n'ont aucune ligne `pod_hosts` et gardent la garde #7106 inchangée.

## Limites connues (tranches suivantes)

- **Coût** : la clé d'organisation est facturée au fournisseur, avec le
  plafond de la clé ; l'imputation au compte du parent et le plafond du parent
  viendront avec le **relais de modèle** (D6, tranche suivante). Le profil du
  parent (`budget_usd`) n'est pas encore appliqué au pod enfant.
- **Réseau/IPC** : pas de NetworkPolicy intra-pod (P6) ; le pod enfant est
  dédié (un seul tenant), donc la limite est plus faible que sur un pod
  partagé, mais elle reste à traiter au chart de référence.
- **Stockage** : `emptyDir` seulement ; aucun PVC, donc aucun travail durable
  hors rapports/artefacts remontés (design §8.4).
- **Kind** : la preuve RBAC/template/quota/cycle de vie est un geste de
  bureau (pas de script livré dans ce lot) ; le témoin complet des identités
  et du canon reste [`../pod-witness/`](../pod-witness/).
