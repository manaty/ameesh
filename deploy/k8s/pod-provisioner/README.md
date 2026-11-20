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
| `Namespace/ameesh` | porte l'annotation `ameesh.dev/pod-child-image`, paramètre cluster-scoped de la VAP (hors de portée du Role namespaced du pourvoyeur) |
| `ServiceAccount/ameesh-pod-provisioner` + `Role`/`RoleBinding` | pods et ConfigMap seulement, dans le namespace |
| `ServiceAccount/ameesh-child` | aucun droit, aucun jeton monté |
| `Deployment/ameesh-pod-admission` + `Service` + `ValidatingWebhookConfiguration` | l'autorité d'admission, sous une identité **distincte** : lit `pod_requests`, rend le pod attendu avec le template de son image et refuse tout écart (request-id, tenant, enfant, nom, Secret exact, env, volumes, ConfigMap) ; `failurePolicy: Fail` |
| `ValidatingAdmissionPolicy` + binding | seconde barrière statique : deux conteneurs nommés, image **exactement** celle du paramètre, commandes figées, aucun init/éphémère, aucun Secret hors `ameesh-db` et du Secret `ameesh-child-<label>` de l'enfant, monté dans le seul conteneur `child` |
| `ResourceQuota/ameesh-pod-children` | borne le nombre et les ressources des enfants |
| `template.yaml` | 2 conteneurs (`supervisor`, `child`), `emptyDir`, UID enfant 30000+, aucun PVC/hostPath |

Le template est **la** source du pod ; le pourvoyeur refuse tout placeholder
inconnu, tout `hostPath`/PVC, tout conteneur privilégié ou qui escalade, toute
image autre que `{{IMAGE}}`, et exige `AMEESH_POD_UID` depuis
`metadata.uid` (garde `pod-host-unknown`). Le nom du Secret d'API est dérivé
du label `ameesh.dev/child` (`ameesh-child-<label>`) ; l'image admise est
l'exacte valeur de l'annotation `ameesh.dev/pod-child-image` du Namespace
(`namespace.yaml`), que le Deployment doit suivre (le test du lot vérifie
l'égalité). Le paramètre vit dans le Namespace, cluster-scoped : le Role du
pourvoyeur ne peut ni le modifier ni le supprimer, contrairement à une
ConfigMap du namespace. Pour changer d'image, modifier `namespace.yaml` et
`deployment.yaml` dans le même geste.

### Pourquoi un webhook, et pas seulement la VAP

La VAP statique ne peut pas lire `pod_requests` : elle vérifie que le Secret
est `ameesh-child-<label>`, mais ce label est choisi par le même
ServiceAccount que celui qui crée le pod. Un SA compromis pourrait donc
prendre le label d'un autre enfant. Le webhook `ameesh-pod-admission` ferme
cette frontière :

- il tourne sous un **ServiceAccount distinct** (`ameesh-pod-admission`) et
  un Deployment séparé ; le Role du pourvoyeur ne peut ni le modifier, ni le
  supprimer, ni toucher son `caBundle` (ClusterRole limité au nom
  `ameesh-child-pods`, porté par le seul SA d'admission) ;
- il lit `pod_requests` en **lecture seule** (`get_by_id`) avec le rôle DB
  `ameesh_pod_admission`, qui n'a que `SELECT` : la lecture seule est une ACL
  PostgreSQL, pas seulement le drapeau `writer=False` ; le pourvoyeur, lui,
  n'a qu'un UPDATE borné aux colonnes d'exploitation
  (`role-pod-provisioner.sql`), jamais aux champs d'autorisation ; ses
  écritures sur `pod_hosts`/`host_tenancy`/`host_tenants` sont bornées par
  une RLS restrictive aux hôtes de ses propres demandes (`pod_requests.host`) :
  un hôte VM partagé reste hors de sa portée (#44568/#44569) ;
- il rend le pod attendu avec le template **copié dans son image**
  (`/etc/ameesh/pod-provisioner/template.yaml`), pas depuis une ConfigMap que
  le pourvoyeur pourrait remplacer ;
- il vérifie request-id, tenant, enfant, nom du pod et Secret exact, puis
  compare la forme complète (image, commandes, env, volumes, montages,
  contexte de sécurité, labels/annotations) et la ConfigMap passwd/group/
  config au plan `tenancy.child_plan` ;
- il n'admet que les objets présentés par l'identité réelle du pourvoyeur,
  `system:serviceaccount:<namespace>:ameesh-pod-provisioner` (nom dérivé du
  namespace, surchargeable par `--provisioner-user`) : un autre auteur est
  refusé, jamais admis par défaut ;
- `failurePolicy: Fail` : webhook indisponible = aucun pod enfant admis.

La VAP reste en place comme seconde barrière (défense en profondeur), mais
ce n'est pas elle qui porte la frontière inter-tenant.

## Déployer

1. **Déclarer le système non-agent** (une fois, dans la base du mesh) :

   ```sh
   agent-mesh work external-executor --declare system:ameesh-pod-provisioner \
       --responsible human:<pilote-ou-bureau> --reason "#7697 enfant pod"
   ```

2. **Rôles de base dédiés** (une fois, superutilisateur PostgreSQL) :

   Le pourvoyeur et le webhook ne se connectent plus avec le rôle `ameesh` :
   le premier n'a qu'un UPDATE d'exploitation sur `pod_requests`, le second
   n'a que SELECT (verdict #44561). Le script est idempotent et resserre un
   rôle préexistant :

   ```sh
   PGOPTIONS='-c search_path=<schéma mesh>' psql <DSN admin> -X \
       -v ON_ERROR_STOP=1 -f deploy/sql/role-pod-provisioner.sql
   # puis, dans psql, un mot de passe SCRAM par rôle (jamais en argument) :
   #   \password ameesh_pod_provisioner
   #   \password ameesh_pod_admission
   # audit après chaque migration additive :
   PGOPTIONS='-c search_path=<schéma mesh>' psql <DSN admin> -X \
       -v ON_ERROR_STOP=1 -f deploy/sql/_contrat-pod-provisioner.sql
   ```

3. **Secrets de la base** (jamais dans un dépôt) :

   ```sh
   kubectl -n ameesh create secret generic ameesh-db-pod-provisioner \
       --from-literal=password='…'       # fichier 0600
   kubectl -n ameesh create secret generic ameesh-db-pod-admission \
       --from-literal=password='…'
   ```

4. **Appliquer le namespace et les objets** (geste d'opérateur) :

   ```sh
   kubectl create namespace ameesh --dry-run=client -o yaml | kubectl apply -f -
   kubectl apply -k deploy/k8s/pod-provisioner
   kubectl -n ameesh rollout status deployment/ameesh-pod-provisioner
   kubectl -n ameesh rollout status deployment/ameesh-pod-admission
   ```

   Le conteneur d'init `tls-init` génère un certificat auto-signé dans un
   `emptyDir` et pose le `caBundle` du `ValidatingWebhookConfiguration`
   `ameesh-child-pods`. Le Deployment est en `Recreate` : une seule réplique
   écrit le CA (deux répliques se remplaceraient l'une l'autre ; un Secret
   TLS partagé est une tranche suivante).

5. **Vérifier le heartbeat** (le template est enregistré par le pourvoyeur à
   chaque passe ; sans heartbeat frais, `children: pod` refuse
   `spawn-pod-unsupported`) :

   ```sh
   kubectl -n ameesh logs deployment/ameesh-pod-provisioner --tail=20
   ameesh pod-provisioner --templates
   ```

6. **Créer le Secret d'API de l'enfant** avant le premier `spawn` (nom exact
   dans `placement.describe()`, ou `ameesh host tenant list` après démarrage) :

   ```sh
   kubectl -n ameesh create secret generic ameesh-child-<slug> \
       --from-literal=api-key='…'        # clé d'organisation, plafond fournisseur
   ```

7. **Prouver sur kind** (facultatif, geste de bureau) : le lot ne livre pas
   de script kind ; les contrôles sont ceux du bureau — appliquer
   `kubectl apply -k deploy/k8s/pod-provisioner`, vérifier
   `kubectl auth can-i --list` du SA enfant (aucun droit), puis soumettre un
   pod hors template (refus VAP/webhook) et un pod conforme (admis, quota
   respecté). Preuve de refus fail-closed : `kubectl -n ameesh scale
   deployment/ameesh-pod-admission --replicas=0`, puis un `spawn` d'enfant
   doit échouer sans pod admis (le webhook est `failurePolicy: Fail`) ;
   remettre `--replicas=1` ensuite.

8. **Preuve inter-tenant négative** (geste de bureau) : avec un SA
   pourvoyeur compromis simulé, un pod portant le `request-id`/nom de
   l'enfant A mais le label/Secret de l'enfant B doit être refusé par le
   webhook (`child pod shape`) ; la ConfigMap de B référencée par A doit être
   refusée aussi (`child ConfigMap data differs from the child plan`).

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
présent dans `pod_hosts`, une ligne `running` non expirée, un battement frais
(120 s, deux battements au rythme par défaut de 60 s) et un `pod_uid` égal à
celui du pod provisionné. Un pod recréé a un UID neuf : l'ancien ne réclame
plus (`pod-host-unknown`, fail closed) ; un runner muet non plus. Les hôtes
non pod n'ont aucune ligne `pod_hosts` et gardent la garde #7106 inchangée.

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
- **SA pourvoyeur entièrement compromis** : le webhook
  `ameesh-pod-admission` recoupe chaque pod et ConfigMap étiqueté à sa
  demande `pod_requests` (lecture seule) et refuse tout écart ; le label d'un
  autre enfant ne suffit donc plus : son rôle DB (`ameesh_pod_provisioner`)
  n'a qu'un UPDATE d'exploitation sur `pod_requests`, et le webhook lit avec
  `ameesh_pod_admission` en SELECT seul (`deploy/sql/role-pod-provisioner.sql`).
  Résidus : le reste du mesh garde le rôle partagé `ameesh` (limite L155) ;
  le SA du webhook peut mettre à jour son propre `caBundle` (`resourceNames`
  borné au nom `ameesh-child-pods`), une compromission du webhook peut donc le
  désarmer ; une ConfigMap sans label `managed-by` échappe à
  l'`objectSelector`, mais elle est recoupée quand le pod la monte.
- **Battement frais** : la garde de réclamation exige un battement de moins
  de 120 s ; un runner dont l'intervalle de ressources dépasse cette valeur
  doit l'abaisser (`AMEESH_RESOURCE_INTERVAL`).
