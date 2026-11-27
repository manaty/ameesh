# Pod de connexion du siège (#7911, design 7633 §7.3)

Connexion **humaine et interactive** d'un abonnement personnel (Claude, Codex)
sur un hôte pod. Décision owner [#233251](https://github.com/manaty/ameesh/issues/233251)
option a : D2 oui, sous condition que le bureau vérifie les conditions du
fournisseur **avec chaque personne** avant toute connexion de siège, preuve
sur le lot ; D4 oui (l'admin du cluster est dans la frontière de confiance).

Ce dossier ne contient **aucun secret** et n'en lit aucun. La personne saisit
elle-même ses identifiants dans le pod de connexion ; le bureau ne les voit
jamais.

## Ce que ce chart rend

`render.py` produit, pour une personne et une session :

- un **Pod d'un seul conteneur** qui monte **seulement** le PVC de la
  personne, sous son UID/GID, sans superviseur, sans jeton de compte de
  service, `readOnlyRootFilesystem: true`, `/tmp` en `emptyDir`, TTL ≤ 30 min
  (`activeDeadlineSeconds`, filet de sécurité du manifeste), épinglé au
  **même nœud** que le pod partagé ;
- un **processus principal** qui tient un verrou et écrit un **battement**
  toutes les secondes dans `.ameesh/seat-login/` sur le PVC, et qui **sort de
  lui-même** à `expires_at - 30 s` : cette borne est absolue (horloge de la
  fenêtre), donc un pod créé tard après le rendu ne survit pas à l'échéance.
  Le runner du tenant, qui monte le même volume, n'enregistre la fin du pod
  qu'après verrou libre et 30 s de silence. C'est la **fin observée** qui
  rouvre la réclamation, jamais la seule échéance SQL (B/P2 #46731) ;
- un **Role** `get pods` + `create pods/exec` borné par `resourceNames` à
  **ce seul pod** (`kubectl exec` lit d'abord le pod) ;
- si `--subject` est donné, le **RoleBinding** de la personne
  (`User:<nom>` ou `Group:<nom>`) ; sans sujet, le bureau crée le binding
  avec l'identité Kubernetes réelle de la personne ;
- un ServiceAccount `ameesh-login` sans jeton et sans aucun binding.

## Règle dure : jamais `pods/exec` sur le pod partagé

`kubectl exec` passe le **conteneur** en paramètre de requête, hors de l'objet
RBAC. Un `resourceNames` sur le pod partagé ouvrirait donc **tous** ses
conteneurs, ceux des autres tenants compris. Le témoin
`deploy/k8s/pod-witness/preuve-isolation-comptes.sh` vérifie qu'aucun Role ne
donne `pods/exec` sur le pod partagé, qu'un `SubjectAccessReview` nommant ce
pod est refusé, et que le Role du pod de connexion n'ouvre que son pod.

## Procédure

### 1. Vérifier D2 et ouvrir la fenêtre

Le bureau a vérifié les conditions du fournisseur **avec la personne** (D2)
et consigné la preuve dans la note du lot. Il ouvre la fenêtre en citant cette
preuve : `--terms` accepte une référence citable (`#233251`,
`owner/repo#123`, `mail:234666`, `card@commit`) ; le récit va dans la note du
lot, jamais dans la colonne.

```sh
ameesh host tenant login human:alice --host pod-alice \
    --terms '#233251' --ttl 1800
```

Le geste est humain ou avatar (`operator`), **jamais un agent** ni le runner
du tenant. Il exige `policy.runtime.kind: pod` dans le canon et une liaison
`host_tenants` existante. La sortie JSON donne `pod_name` et `expires_ts` :
ce sont eux qui pilotent le rendu (étape 4), pas un nom ou un TTL inventés.

### 2. Attendre que le runner ait rendu ses baux

```sh
ameesh host tenant list --host pod-alice
```

La ligne doit porter `login until … terms=…` **et** `state=login` : c'est
l'acquittement du runner (`runner_presence.state`), qui a arrêté ses tours,
rendu ses baux et cesse de réclamer (`ameesh_tenant_claim_ok` refuse aussi
tout autre runner pendant la fenêtre). L'acquittement n'est publié qu'au
battement suivant la libération (au plus l'intervalle de ressources, 60 s par
défaut). Ne pas créer le pod avant `state=login` : le harnais pourrait encore
écrire les identifiants.

### 3. Lire le nœud du pod partagé

```sh
NODE=$(kubectl -n ameesh get pod ameesh-pod-alice \
        -o jsonpath='{.spec.nodeName}')
```

Le PVC est `ReadWriteOnce` : le pod de connexion doit être sur le **même
nœud** pour que les deux pods le montent sans détachement.

### 4. Rendre et appliquer le pod de connexion

`IMAGE` est l'image du harnais, **épinglée par digest** et contenant
`python3` (le processus principal du pod est le gardien du battement). `UID`
est celui du chart (`ameesh host tenant list` le montre ; le battement le
vérifie). `POD` et `EXPIRES` sont les valeurs rendues par l'étape 1 :

```sh
LOGIN=$(ameesh host tenant login human:alice --host pod-alice \
        --terms '#233251' --ttl 1800)
POD=$(printf '%s' "$LOGIN" | python3 -c \
      'import json,sys; print(json.load(sys.stdin)["pod_name"])')
EXPIRES=$(printf '%s' "$LOGIN" | python3 -c \
      'import json,sys; print(int(json.load(sys.stdin)["expires_ts"]))')
```

```sh
python3 deploy/k8s/pod-login/render.py \
    --namespace ameesh --host pod-alice --tenant human:alice \
    --uid "$UID" --pvc pvc-alice --pod "$POD" \
    --node "$NODE" \
    --image "$IMAGE" \
    --expires-at "$EXPIRES" \
    --subject User:alice \
  | kubectl apply -f -

kubectl -n ameesh wait --for=condition=Ready \
    "pod/$POD" --timeout=120s
```

### 5. La personne se connecte

Avec **son** identité Kubernetes (celle du RoleBinding) :

```sh
kubectl -n ameesh exec -it "$POD" -- sh
# dans le shell, sous l'UID du tenant :
claude      # puis /login ; l'URL s'ouvre sur le poste de la personne
# ou
codex login # flux appareil
```

Le harnais écrit ses identifiants en `0600` dans le dossier de comptes
(`0700`) du PVC. Aucun agent ne saisit d'identifiant ; le bureau ne lit ni
n'affiche le contenu.

### 6. Fermer

Supprimer le pod **d'abord**, puis le Role et le RoleBinding, puis fermer la
fenêtre :

```sh
kubectl -n ameesh delete pod "$POD" --wait=true
kubectl -n ameesh delete role "$POD"
kubectl -n ameesh delete rolebinding "$POD"
ameesh host tenant logout human:alice --host pod-alice
```

`kubectl delete --wait=true` attend la disparition de l'objet : c'est la
suppression **constatée**. `logout` est idempotent (`closed: false` s'il n'y
a rien à fermer). Le battement suivant republie `state=running` et les profils
de compte ; le runner reprend les tours.

## Fin observée du pod (B/P2 #46731)

La fenêtre SQL et le `activeDeadlineSeconds` du pod ne suffisent pas : le pod
peut démarrer après l'ouverture, survivre à l'échéance ou être détruit plus
tard que prévu. Le pod de connexion écrit donc un **battement** et tient un
**verrou** sur le volume de la personne ; le runner du tenant, seul autre
processus qui monte ce volume, applique cette règle :

- verrou tenu par le pod, ou battement frais : la fin n'est pas observée, la
  fenêtre reste fermée, le runner ne réclame pas ;
- verrou libre **et** battement silencieux depuis 30 s (plus que
  `terminationGracePeriodSeconds: 5`) : le runner enregistre `pod_ended_at`,
  la garde `ameesh_tenant_login_active` s'ouvre et les prises reprennent ;
- pod jamais démarré (aucun battement) : la fenêtre reste fermée après
  l'échéance. Le bureau supprime le pod, constate sa disparition, puis
  `logout` — jamais l'inverse, et jamais de réouverture par simple attente.

Une nouvelle fenêtre pour la même personne prend un **nouveau** `pod_name` :
le battement d'une session précédente ne peut pas libérer la suivante.

### 7. Vérifier

```sh
ameesh host tenant list --host pod-alice
ameesh agent observe alice-agent
```

`state=running` et un battement frais ; les profils `claude/…` ou `codex/…`
sont de nouveau publiés.

## Si le PVC ne peut pas être monté à côté du pod partagé

Stockage qui refuse deux montages, nœud imposé, pod de connexion interdit :
la personne est servie par un **pod dédié** (design §2.2, D3) ; le pod de
travail est arrêté, le pod de connexion monte seul le PVC, la personne se
connecte, le pod de travail redémarre sur le même PVC (montages séquentiels,
jamais simultanés). On ne retombe **jamais** sur `pods/exec` dans le pod
partagé.

## En cas de perte du PVC ou de déplacement

La personne se reconnecte sur la cible ; **aucun fichier d'identifiants n'est
copié** (design §9, D7).

## Témoin

Le contrôle 6 du témoin `kind` est un geste de bureau/opérateur :

```sh
AMEESH_TEST_DSN=postgresql://… ./deploy/k8s/pod-witness/preuve-isolation-comptes.sh
```

Sans `kind`/`kubectl`/`docker` ou sans DSN, il rend `77` : **non-concluant**,
jamais vert. La création réelle du pod et la connexion du siège sont un geste
Q-TPK hors série (voir `docs/ops/qualification-hote-pod.md`).

## Ce que ce chart ne fait pas

- Il ne choisit pas le nœud : le bureau lit `spec.nodeName` du pod partagé.
- Il ne crée pas la fenêtre `login` : c'est `ameesh host tenant login`, qui
  exige la preuve D2.
- Il ne lit, ne copie et n'affiche aucun identifiant.
- Il n'ouvre aucun droit sur le pod partagé, ni sur un autre tenant.
- Il ne remplace pas la qualification TPK/IMA (Q-TPK, hors série).
