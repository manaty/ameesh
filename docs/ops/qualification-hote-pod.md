# Qualification d'un hôte pod sur TPK et passation TPK/IMA (#7699)

Runbook de la tranche **P6** de #7633 (design
[`7633-hote-avatars-pod.md`](../design/7633-hote-avatars-pod.md) §10) : ce
qu'il faut obtenir de la plateforme, comment déployer le chart de référence,
comment qualifier, sauvegarder, mettre à jour et rendre la main. Il ne
déploie **rien** : la qualification réelle sur `tpk-stg` (Q-TPK) est un lot
TPK/IMA **hors série**, et l'application du chart reste un geste d'opérateur.

Sources et limites :

- le constat daté du 17/11 dans #7633 (cluster `tpk-stg`, namespace
  `issue-manager`, `ima-documents-tpk-staging` en codex/deepseek par clé
  d'API, 0 ligne `host_tenants`) est la **seule** source TPK utilisée ; tout
  le reste est à confirmer sur le cluster, sans hypothèse dans le chart ;
- le chart et le témoin `kind` sont livrés par #7694–#7699 ; le témoin
  `kind` prouve le mécanisme, pas la plateforme ;
- aucun secret n'est lu, écrit ni cité ici ; les valeurs réelles vivent dans
  des Secrets créés hors dépôt.

## 1. Périmètre

| Fait par #7699 | Fait par Q-TPK (hors série) |
|---|---|
| chart de référence (`deploy/k8s/pod-shared/`) : NetworkPolicy, quota, PDB, snapshots, digest, `preStop`/grâce, labels, mise à jour/retour arrière | choix du namespace et du StorageClass, RBAC réel, PSA, CNI, registre, base, canon, secrets |
| témoin `kind` (contrôle de référence inclus) et tests sans cluster | témoin rejoué sur `tpk-stg`, déplacement réel, connexion du siège |
| ce runbook de qualification et la passation | exécution du runbook, décisions D2/D4 (owner), recette |

**Depuis #7911** (décision owner #233251, option a, 27/11 00:18Z) : Q2 bis
ouvre `subscription` sur un hôte pod à volume par tenant, sous les conditions
de la carte Host (`allow_subscription`, `require_isolation`,
`account_isolation: per-tenant-volume`, `runtime.isolation_ref` citable). D2
reste une condition **humaine par personne** : le bureau vérifie les
conditions du fournisseur avec la personne avant toute connexion de siège et
consigne la preuve sur le lot ; D4 est accepté (l'admin du cluster est dans
la frontière de confiance). La connexion du siège passe par un pod dédié
([`connexion-siege-pod.md`](connexion-siege-pod.md)) ; D3 (namespace
réseau/IPC partagé pour les inactifs) et D5 (visibilité mémoire évaluée par
tenant) ont été tranchées le 20/11.

## 2. Décisions et replis portés par le chart

| # | Décision | État au départ de P6 | Ce que le chart fait |
|---|---|---|---|
| D1 | Q2 bis : abonnement sur un pod à volume par tenant | tranché oui, #233251 (27/11) | `subscription` admis si volume par tenant + `isolation_ref` citable ; `api-key` reste le repli |
| D2 | conditions du fournisseur sans humain devant l'écran | oui sous condition (27/11) | fenêtre `login` avec preuve citable (`--terms`), vérification par personne ; aucune clé de siège hors connexion |
| D3 | namespace réseau/IPC commun pour les inactifs | tranché oui (20/11) | pas de garde intra-pod ; limite §5.1 documentée |
| D4 | PVC monté par l'admin du cluster | oui (27/11) | volume par tenant ; l'admin du cluster est dans la frontière de confiance, limite assumée |
| D5 | visibilité mémoire sur un hôte partagé | tranché par tenant (20/11) | `isolation_ref` et témoin de compte ; pas de garde mémoire |
| D6 | enfant : relais ou clé d'API | repli clé d'API | P4, hors de ce chart |
| D7 | shared ↔ dedicated : PVC ré-attaché ou re-login | repli re-login | P5 ; `pod: reattach` est une condition, le bureau confirme le volume |
| D8 | UID stable par personne ou par hôte | repli UID par hôte | `pod_plan()` dérive un UID stable ; le battement vérifie `host_tenants.uid` |

Le chart ne suppose **aucune** de ces décisions au-delà de son repli. La
connexion du siège est un lot distinct (#7911) ; Q-TPK qualifie le RBAC réel
et la connexion d'une personne sur `tpk-stg`.

## 3. Ce que le chart livre

Dans `deploy/k8s/pod-shared/` :

| Objet | Qualification attendue |
|---|---|
| `StatefulSet` 1 réplique, `revisionHistoryLimit: 5`, `minReadySeconds: 10`, `RollingUpdate` partition 0 | rollout et `kubectl rollout undo` |
| `NetworkPolicy` `default-deny` (Ingress+Egress), DNS, une politique par sortie déclarée | objets acceptés ; **application** vérifiée par test de connexion sur le CNI réel |
| `ResourceQuota` 1 pod, N PVC, somme CPU/mémoire, 0 Service/Ingress | un second pod est refusé (`exceeded quota`) |
| `PodDisruptionBudget` `minAvailable: 1` | `expectedPods=1`, `disruptionsAllowed=0` |
| image `dépôt@sha256:…` dans tous les conteneurs | aucun tag mutable ; le digest est celui publié |
| `preStop` de drain + `terminationGracePeriodSeconds: 120` + `AMEESH_DRAIN_SECONDS: 80` | le pod rend ses baux avant le SIGKILL ; le tour en cours finit |
| labels `version`/`instance`/`host` hors sélecteur | un changement de digest ne bloque pas le rollout |
| `snapshot.py` + `snapshotclass.exemple.yaml` | snapshot et restauration réels, même UID après retour |
| `host.exemple.yaml`, `render.py --check`, `kustomization.yaml` | rendu déterministe, rien appliqué par un agent |

Le témoin `kind` et le workflow manuel sont dans
`deploy/k8s/pod-witness/` ; les tests sans cluster sont
`tests/test_7699_chart_reference.py` (et `test_7695_pod_partage.py`).

## 4. Prérequis à obtenir de TPK/IMA (avant tout geste)

Chaque ligne est **à confirmer et à consigner** dans le lot Q-TPK ; une
réponse absente n'est pas une hypothèse du chart.

| Élément | Question précise | Pourquoi |
|---|---|---|
| Cluster et namespace | `tpk-stg` et `issue-manager` sont-ils toujours les bons ? Un namespace par pod partagé est-il admis ? | les ConfigMap `ameesh-pod-*` ont des noms fixes : **un namespace par pod** |
| Version Kubernetes | version exacte, `policy/v1` PDB, `networking.k8s.io/v1` NetworkPolicy, `snapshot.storage.k8s.io/v1` | le chart n'émet que ces API |
| PSA | niveau `restricted` ou `baseline` ? le mode A (non-root, `fsGroup` 10000, aucun init root) est-il admis ? | le chart est en mode A ; `restricted` doit passer |
| Stockage | StorageClass `ReadWriteOnce`, `fsGroup` appliqué, `WaitForFirstConsumer`, ré-attachement d'un PVC | le modèle un PVC par tenant ; le témoin 1 juge les modes |
| Snapshots | pilote CSI, `VolumeSnapshotClass`, `deletionPolicy`, quota de snapshots, rétention | sauvegarde/restauration P6 |
| CNI | la NetworkPolicy est-elle **appliquée** ? `ipBlock.except` est-il supporté ? le DNS de `kube-system` est-il joignable ? | `default-deny` n'est une garde que si le CNI l'applique |
| Réseau | CIDR de la base, du canon/Git, des API de fournisseurs, DNS ; le service de métadonnées `169.254.169.254` est-il joignable ? | les règles `egress` sont des CIDR, pas des noms |
| Registre | digest publié de l'image, `imagePullSecret` si le registre est privé | le chart refuse un tag seul |
| Base | endpoint, base, schéma, rôles, mot de passe (Secret `ameesh-db`), version PostgreSQL | le superviseur et le runner écrivent l'état |
| Canon | URL Git, branche, `known_hosts`, clé de déploiement (Secret `ameesh-canon-deploy-key`) | clone init et `git fetch` |
| Identité | UID des personnes (plage 1000–60000, ni 10000 ni 10001), `human:<id>` du canon | le chart dérive l'UID du plan |
| Observabilité | journaux, événements, alertes de redémarrage, accès `kubectl logs` | incident et drainage |
| RBAC | qui applique le chart, qui crée les Secrets, qui lit les journaux | le chart ne donne aucun droit Kubernetes au pod |

## 5. Déploiement de qualification (Q-TPK)

Séquence d'opérateur ; les commandes sont des **modèles**, à adapter au
namespace et au cluster. Aucun agent ne les exécute.

1. **Consigner la version** : `git rev-parse HEAD`, digest de l'image,
   `kubectl version`. Le chart est rendu depuis ce commit.
2. **Créer le namespace** et y poser la PSA retenue :
   `kubectl create namespace <ns>` puis les labels
   `pod-security.kubernetes.io/enforce=restricted` (si TPK l'admet).
3. **Créer les Secrets hors dépôt** (0600) : `ameesh-db` (`password`),
   `ameesh-canon-deploy-key` (`ssh-privatekey`, `known_hosts`), un Secret
   `ameesh-harness-<slug>` par tenant si une clé d'API est utilisée. Vérifier
   les noms attendus dans `secrets.exemple.yaml` ; ne jamais les mettre dans
   `kustomization.yaml`.
4. **Renseigner `host.exemple.yaml`** : `image_digest`, `version`, CIDR
   réels de `network.egress`, `quota`, `pdb`, `dsn`, `canon`, tenants. Aucun
   CIDR d'exemple (TEST-NET) ne doit rester.
5. **Rendre et relire** : `./render.py --write`, `git diff`,
   `./render.py --check`, `kubectl kustomize .`. Vérifier que le sélecteur du
   `StatefulSet` ne change pas et que l'image porte `@sha256:`.
6. **Pré-vérifier côté API** : `kubectl apply --dry-run=server -k .` (le
   chart ne crée pas de Secret ; la simulation ne lit rien).
7. **Appliquer** : `kubectl apply -k .`. Lire `kubectl get quota,pdb,netpol`,
   `kubectl rollout status statefulset/ameesh-pod-<hôte>`.
8. **Enregistrer le tenant** avec l'UID du chart :
   `ameesh host tenant add human:<id> --host <hôte> --pod --register \
   --uid <uid-du-plan> --actor human:<admin>`. Attendre un battement frais et
   le profil publié (`ameesh host tenant list --host <hôte>`).
9. **Rejouer le témoin** sur la plateforme réelle : identité, modes,
   montage, superviseur, base, canon, contrôle de référence (section 6).
10. **Consigner** : versions, digest, sorties de `kubectl get`, trace du
    témoin et écarts dans le lot Q-TPK. Aucun journal de processus dans le
    dépôt produit.

## 6. Contrôles de qualification

| Contrôle | Commande / preuve | Attendu |
|---|---|---|
| Rendu déterministe | `render.py --check` | code 0, 6 fichiers |
| API | `kubectl apply --dry-run=server -k .` | accepté, aucun champ refusé |
| Quota | créer un second pod étiqueté comme l'hôte | refus `exceeded quota` |
| PDB | `kubectl get pdb` | `expectedPods=1`, `disruptionsAllowed=0` |
| NetworkPolicy | depuis un conteneur de tenant : DNS, base, canon, fournisseur ; puis une cible non autorisée | autorisés joignables, non autorisée refusée ; métadonnées refusées |
| Digest | `kubectl get pod -o jsonpath='{.spec.containers[*].image}'` | tous `@sha256:` |
| preStop/grâce | supprimer le pod pendant un tour (`kubectl delete pod`) | le runner draine, rend son bail, aucun tour perdu ; `terminationGracePeriodSeconds: 120` |
| Labels | `kubectl get sts -o yaml` | `version`/`instance`/`host` présents ; sélecteur sans version |
| Témoin comptes | `deploy/k8s/pod-witness/preuve-isolation-comptes.sh` sur un banc `kind` | sortie 0 (ou 77 si DSN absent : non-concluant, jamais vert) |
| Témoin réel | mêmes contrôles sur `tpk-stg` | identité, montage, permissions, base, canon, déplacement |
| Déplacement | runbook [`deplacement-hote-pod.md`](deplacement-hote-pod.md) | `pod.pvc: reattach` ou repli re-login décidé, conversation continue |
| Sauvegarde/restauration | section 7 | PVC restauré, même UID, témoin de compte relu |
| Mise à jour/retour arrière | section 8 | rollout vert, `rollout undo` revient au digest précédent |

Un test **négatif** (une garde qui ne mord pas) bloque la qualification. Un
test non concluant (CNI absent, snapshot non supporté par le pilote, appareil
indisponible) est **consigné** et traité par la plateforme ; il ne se
transforme jamais en « vert ».

## 7. Sauvegarde et restauration

Prérequis : `VolumeSnapshotClass` du CSI (voir
`deploy/k8s/pod-shared/snapshotclass.exemple.yaml`), `deletionPolicy:
Retain`, rétention décidée par TPK.

```sh
# 1. snapshot daté, un par PVC de tenant
./deploy/k8s/pod-shared/snapshot.py --class <classe> \
    --stamp "$(date -u +%Y%m%dt%H%M%SZ | tr A-Z a-z)" > /tmp/snapshots.yaml
kubectl -n <ns> apply -f /tmp/snapshots.yaml
kubectl -n <ns> get volumesnapshot

# 2. vérifier readyToUse=true et la taille réelle
kubectl -n <ns> get volumesnapshot -o wide

# 3. restauration : arrêter le pod, supprimer le PVC, recréer du même nom
kubectl -n <ns> scale statefulset/ameesh-pod-<hôte> --replicas=0
kubectl -n <ns> delete pvc pvc-<slug>          # geste destructif, après snapshot vérifié
./deploy/k8s/pod-shared/snapshot.py --restore --stamp <stamp> > /tmp/restore.yaml
kubectl -n <ns> apply -f /tmp/restore.yaml
kubectl -n <ns> scale statefulset/ameesh-pod-<hôte> --replicas=1
```

Après restauration : `pvc-<slug>` est `Bound`, le pod est `Ready`, le
témoin 1 est rejoué (même UID, dossier de comptes `0700`, fichiers `0600`),
`ameesh host tenant list` montre un battement frais. Ne jamais restaurer
**sous** le pod qui écrit : la séquence arrêt → copie/restauration → reprise
est la seule admise. Le PVC supprimé n'est pas recréé par `kubectl apply -k` :
c'est le manifeste `restore-pvcs` qui le recrée.

## 8. Mise à jour et retour arrière

1. **Préparer** : image construite, digest noté, `image_digest` et `version`
   mis à jour, `render.py --write`, diff relu, `--check` vert.
2. **Drainer** : `ameesh host drain <hôte> --for <durée>` ; attendre la fin
   des tours et la libération des baux. Le `preStop` protège l'arrêt, pas une
   migration de base.
3. **Appliquer** : `kubectl apply -k .`, `kubectl rollout status`.
   `RollingUpdate` à une réplique recrée **tous** les conteneurs (tous les
   tenants) : c'est une fenêtre de maintenance, pas un rolling par tenant.
4. **Vérifier** : pod `Ready`, superviseur détenteur du bail, tenants
   présents, profils publiés, témoin 1, journaux `preStop`/drainage.
5. **Retour arrière** : `kubectl rollout undo statefulset/ameesh-pod-<hôte>`
   revient au template précédent (digest précédent, historique 5). Une
   migration de base n'est **pas** annulée par Kubernetes : si le schéma a
   changé, appliquer une migration descendante compatible ou restaurer le
   snapshot (section 7). Ne pas faire de rollback applicatif sur un schéma
   incompatible.

Conserver l'image précédente dans le registre au moins jusqu'au prochain
palier ; le digest est la seule référence qui rend le retour arrière exact.

## 9. Passation TPK/IMA

| Qui | Ce qu'il reçoit / fait | Preuve de passation |
|---|---|---|
| **TPK** (plateforme) | namespace, RBAC, PSA, StorageClass et snapshots, CNI/NetworkPolicy, registre et digest, DNS, accès journaux | réponses de la section 4 consignées ; `VolumeSnapshotClass` créée ; test NetworkPolicy réel |
| **IMA** (ticket et application) | ticket de déploiement, Secrets créés hors dépôt, valeurs de `host.exemple.yaml` (CIDR, DSN, canon, tenants), fenêtre de maintenance | chart appliqué, `kubectl get quota,pdb,netpol`, témoin réel, trace |
| **ameesh** (équipe du code) | chart, `render.py`, `snapshot.py`, témoin, ce runbook, tests ; support pendant la qualification | version du commit, digest, `render.py --check`, CI du lot |
| **Owner** | D2 et D4 (et D1 si elle revient), décisions de publication | décision signée ; sans elle, `api-key` seulement |

**Artefacts remis** : ce document ; `deploy/k8s/pod-shared/` (chart, README,
`snapshot.py`, `snapshotclass.exemple.yaml`) ; `deploy/k8s/pod-witness/` ;
`tests/test_7699_chart_reference.py` ; le digest de l'image et le commit.

**Critères de sortie de Q-TPK** (tous, ou écart signé) :

- [ ] chart appliqué sur `tpk-stg`, namespace confirmé, rollout `Ready` ;
- [ ] quota refuse un second pod ; PDB `1/0` ;
- [ ] NetworkPolicy appliquée par le CNI : DNS/base/canon/fournisseurs
      joignables, cible non autorisée refusée, métadonnées refusées ;
- [ ] image par digest, aucun tag mutable ;
- [ ] témoin comptes rejoué sur la plateforme : identité, montage,
      permissions, superviseur, base, canon ;
- [ ] déplacement poste ↔ pod (runbook P5) : `pod.pvc` lu, conversation
      continue, témoin 1 après déplacement ;
- [ ] snapshot puis restauration : `readyToUse`, PVC `Bound`, même UID,
      témoin 1 relu ;
- [ ] mise à jour puis `rollout undo` : retour au digest précédent, schéma
      compatible ;
- [ ] Secrets hors dépôt, aucun secret dans le dépôt ni dans un journal ;
- [ ] décisions D2/D4 tracées **par personne** avec leur preuve sur le lot,
      et connexion du siège rejouée selon
      [`connexion-siege-pod.md`](connexion-siege-pod.md) (D1/Q2 bis : carte
      Host pod avec `isolation_ref` et `subscription`).

La passation est **faite** quand TPK/IMA peut rejouer seuls la séquence 5–8
sur un namespace neuf et que les critères ci-dessus sont consignés. Un
déploiement non qualifié n'est pas « fait » (canon §5) : le lot ameesh de la
série s'arrête au chart et à la preuve `kind` ; Q-TPK porte la plateforme.

## 10. Risques et limites

- **PSA** : si `restricted` refuse `fsGroup` ou l'UID du chart, le mode A est
  bloqué ; ne pas introduire d'init root sans décision (mode B du design
  §6.2). Consigner et revenir à TPK.
- **Stockage** : sans `ReadWriteOnce` + `fsGroup` + snapshot, le modèle un
  PVC par tenant ne tient pas ; le pod dédié est le repli, pas un
  contournement.
- **CNI** : une NetworkPolicy non appliquée n'est pas une garde. Le test de
  connexion réel est le seul verdict.
- **Admin du cluster** : il peut monter un PVC ; c'est la limite D4, non
  revendiquée.
- **Intra-pod** : les conteneurs partagent le namespace réseau/IPC ; pas de
  NetworkPolicy intra-pod. La parade est le pod dédié (design §5.1).
- **Abonnement** : D2/D4 sont tranchées (#233251), mais D2 reste une
  vérification **par personne** avant chaque connexion de siège ; ne pas
  déduire un accord des conditions générales. La fenêtre `login` exige une
  preuve citable et le pod de connexion dédié (design §7.3).
- **Métadonnées** : `0.0.0.0/0` reçoit `except: 169.254.0.0/16` ; un CNI qui
  ignore `except` doit être signalé (le test de connexion le voit).
- **Une seule réplique** : un drain de nœud attend le PDB ; l'opérateur
  draine les agents et supprime le pod explicitement. Un `kubectl drain
  --disable-eviction` contourne le PDB : geste conscient, tracé.
