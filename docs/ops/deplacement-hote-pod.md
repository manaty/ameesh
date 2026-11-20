# Déplacement d'un avatar : poste ↔ pod partagé ↔ pod dédié (#7698)

Runbook d'exploitation de la tranche P5 de #7633
([design 7633 §9](../design/7633-hote-avatars-pod.md#9-déplacement-poste--pod-partagé--pod-dédié)).
Il décrit les gestes du **bureau** autour de `ameesh agent move` ; il ne crée
ni pod ni PVC par lui-même et ne copie **jamais** un fichier de session ou
d'identifiant. Le code de `agent move` et le présent document sont deux
choses : le code refuse ce qui est faux, le runbook ordonne les gestes
d'infrastructure.

## Ce que le code vérifie (et ce qu'il ne peut pas vérifier)

`ameesh agent move <avatar> --to <hôte>` reste le chemin immédiat d'un avatar
`releve: preference` (design 5322 §3.5). Vers un hôte pod, `pod_move.preflight`
ajoute, **avant tout effet** :

| Code de refus | Ce qu'il prouve |
|---|---|
| `move-pod-child-target` | la cible est un hôte enfant éphémère (`pod_hosts`) : jamais une cible |
| `move-pod-template` | la carte est `runtime.kind: pod-template`, pas un hôte pod |
| `move-pod-runtime` | la carte manque (alors que l'hôte a déjà tenu un bail de superviseur) ou son runtime est illisible |
| `move-pod-isolation` | `runtime.account_isolation` inconnu : le modèle de compte n'est pas prouvé |
| `move-tenant-missing` | le tenant de la personne n'est pas déclaré/lié sur l'hôte pod |
| `move-pod-passwd` | le `unix_user` lié ne suit pas le plan (`ameesh-<id>`) |
| `move-pod-uid` | UID hors plage pod, ou battement sous un UID différent de la liaison |
| `move-tenant-uid` | la présence du runner contredit l'UID de `host_tenants` (via `candidate_state`) |
| `move-presence`, `move-account-missing` | superviseur vivant, relevé frais, porte ouverte (`host_lifecycle`, `gate_state`), `canon_state` ok, battement frais ; profil de compte publié pour le harnais (gardes #7108) |

Ces deux dernières gardes valent pour **toute** cible pod, y compris un agent
`releve: hote` qui n'a pas de tentative de conversation : le chemin hôte les
rejoue sous le verrou avant l'écriture (B #44740, complété par B #44767 pour
la porte et le canon).

Le préflight est revalidé **sous le verrou** de l'écriture : un changement de
liaison, d'UID ou de mode PVC entre la lecture et l'écriture donne
`move-pod-changed` et rien n'est écrit. Le résultat JSON (`--json`) et
l'événement `agent.moved` portent le plan :

```json
"pod": {
  "kind": "pod", "tenant": "human:alice", "uid": 2519,
  "account_isolation": "per-tenant-volume", "isolation_ref": "manaty/ameesh#7694",
  "source_kind": "pod", "source_uid": 2519, "pvc": "reattach"
}
```

`pvc` vaut `reattach` (même personne, **même UID** sur les deux hôtes pod : le
PVC peut être ré-attaché), `relogin` (UID différent ou source pod absente du
plan : PVC neuf, la personne se reconnecte) ou `none` (la source n'est pas un
pod : aucun volume de pod à déplacer). Le code ne voit **pas** la StorageClass
ni le namespace : `reattach` est une condition nécessaire, le bureau confirme
que le volume est réellement détachable/ré-attachable.

## Décisions et replis

- **D8 (UID stable par personne)** n'est pas tranché : le repli est l'UID par
  hôte. Un UID différent entre deux hôtes pod rend le PVC de l'ancien hôte
  inutilisable pour le nouveau : `pvc: relogin`, PVC neuf, compte refait par
  la personne (procédure de connexion du siège, design §7.3).
- **D7 (PVC ré-attaché ou re-login)** n'est pas tranché : le repli est le
  re-login. Si la classe de stockage, le namespace ou `ReadWriteOnce`
  interdisent le ré-attachement, ne pas forcer : créer un PVC neuf et laisser
  la personne se reconnecter. Le déplacement est refusé tant que le profil de
  compte n'est pas publié (`move-account-missing`), jamais replié.
- Le **même fsGroup neutre** (`10000`) et le **même UID de personne** sont la
  condition du ré-attachement (N de #7659). Le témoin 1 (identité, passwd,
  modes 0700/0600, marqueur du compte) est rejoué **après chaque déplacement**.

## Avant tout geste : préflight

1. Lire la cible : `ameesh host show <hôte-pod> --json` (runtime, tenants,
   porte) et `ameesh host tenant list --host <hôte-pod>` (liaison, UID,
   présence, profils publiés).
2. Vérifier la carte Host dans le canon : `runtime.kind: pod`,
   `account_isolation: per-tenant-volume`, `isolation_ref` citable ; les deux
   hôtes pod du déplacement doivent être déclarés et admis dans la `Placement`
   de l'avatar.
3. Vérifier que le tour source est fini (`ameesh agent peek <avatar>`) et
   qu'aucun handoff n'est ouvert. Un tour vivant n'est jamais déplacé.
4. Pour un PVC existant, vérifier que le pod cible et le pod source sont dans
   le **même namespace** et que le chart cible référence le PVC existant
   (`pvc-<slug>`), pas un PVC neuf.

## A. Pod partagé → pod dédié, PVC ré-attaché (même UID)

Ordre (le retour est le même dans l'autre sens) :

1. **Frontière du tour** : attendre la fin du tour de l'avatar ; au besoin
   `ameesh host drain <hôte-partagé> --for <durée>` pour ne plus admettre de
   tour pendant l'opération, puis `ameesh host undrain` à la fin.
2. **Libérer le PVC** : retirer le conteneur du tenant du pod partagé
   (modifier `host.exemple.yaml` du chart `deploy/k8s/pod-shared`, relire le
   diff, `./render.py --write`, `kubectl apply -k .`) ou supprimer le pod
   partagé si tous ses tenants sont drainés. Attendre que le pod soit arrêté
   (`kubectl rollout status statefulset/…`). Le PVC `pvc-<slug>` **n'est pas
   supprimé** (le chart ne pose pas de `volumeClaimTemplates`).
3. **Monter la cible** : appliquer le pod dédié (`deploy/k8s/pod-socle`) dans
   le **même namespace**, en gardant `claimName: pvc-<slug>`, le même UID et
   le même `fsGroup`. Attendre `Ready`.
4. **Enregistrer le tenant** avec l'UID du chart :
   `ameesh host tenant add human:<id> --host <hôte-dédié> --pod --register \
   --uid <uid-du-chart> --actor human:<admin>`.
5. **Attendre la présence** : `ameesh host tenant list --host <hôte-dédié>`
   jusqu'à un battement frais et le profil du harnais publié
   (`profiles=codex/…`). Un profil absent est un refus, pas un raccourci.
6. **Déplacer** : `ameesh agent move <avatar> --to <hôte-dédié> --actor \
   human:<admin> --json`. Lire `pod.pvc` : `reattach`. Le bail source est
   révoqué dans la même transaction, la conversation passe par
   `conversation_moves` (la session native reste provisoire jusqu'au premier
   tour de la cible).
7. **Vérifier** : la cible réclame (`ameesh agent peek`), le premier tour
   reconstruit le segment, puis rejouer le témoin 1 (voir plus bas). Le
   volume contient toujours les fichiers de la personne, aux mêmes UID/modes,
   et **aucun** fichier d'identifiant n'a été copié.

Si le bureau ne peut pas ré-attacher (StorageClass, namespace, `ReadWriteOnce`) :
suivre **B**.

## B. Pod partagé → pod dédié, re-login (UID différent ou pas de ré-attachement)

1. Mêmes étapes 1–2 que A, mais **ne pas** monter l'ancien PVC dans le pod
   dédié. Créer un PVC neuf (`pvc-<slug>` du chart dédié) et appliquer le pod.
2. La personne se connecte elle-même dans son pod dédié (procédure de
   connexion du siège, design §7.3 : pod de connexion dédié, jamais
   `pods/exec` sur le pod partagé). Aucun agent ne saisit d'identifiant.
3. Attendre que le runner du tenant publie son profil
   (`ameesh host tenant list --host <hôte-dédié>`), puis `ameesh agent move`
   comme en A. Le plan porte `pvc: relogin` ; il est refusé
   `move-account-missing` tant que le compte n'est pas prêt.

Ne jamais copier l'ancien `config_dir`, une session ou un fichier
d'identifiant d'un volume à l'autre : c'est exactement ce que le modèle
interdit (design 5322 §3.5, 7633 §4.2).

## C. Poste ↔ pod

- **Poste → pod** : le poste garde ses identifiants et sa session native ; le
  pod doit avoir son propre compte prêt (battement + profil publié). Après le
  déplacement, `pod.pvc` vaut `none` (aucun volume de pod déplacé) ; la
  conversation suit par le journal. Le poste est `intermittent: true` : à son
  extinction, son bail expire ; au retour, il attend `PREFERENCE_DWELL_S`
  (120 s) et le holdoff de la cible (B2, inchangé).
- **Pod → poste** : aucun volume ne traverse ; le poste reconstruit le
  segment au premier tour. Le pod peut rester allumé : son runner de tenant
  ne réclame que les agents dont il est le `responsible`, et la garde
  `ameesh_tenant_claim_ok` refuse un bail d'un autre tenant.

## Témoin 1 après chaque déplacement

Le geste de qualification est un contrôle d'identité, pas une confiance au
`.status` du pod :

1. Dans le conteneur du tenant : `id -u` = UID du chart, entrée `passwd`
   `ameesh-<id>:x:<uid>:<fsGroup>`, home 0700 (ou 0770/2770 avec le bit
   `fsGroup`), dossier de comptes 0700, fichiers 0600.
2. Le marqueur du compte (fichier de test propre au tenant) est toujours
   lisible sous le même UID ; le home d'un autre tenant n'est **pas** monté.
3. En kind, le témoin complet rejoue ce contrôle après le déplacement :
   `deploy/k8s/pod-witness/preuve-isolation-comptes.sh` (sortie 77 =
   non-concluant si `kind`/`kubectl`/`docker` ou `AMEESH_TEST_DSN` manque ;
   jamais vert).

## Pannes et retour arrière

- **La cible ne réclame pas** : lire `ameesh host tenant list`, le bail
  superviseur (`pod_supervisor_leases`) et les journaux
  `kubectl logs <pod> -c tenant-<slug>` ; un UID divergent refuse la présence,
  corriger le chart et régénérer, jamais le passer à la main.
- **Le PVC ne se ré-attache pas** : ne pas insister en root ; revenir en B
  (PVC neuf + re-login).
- **Retour** : `ameesh agent move <avatar> --to <hôte-source>` dès que
  l'hôte source est de nouveau présent (même procédure dans l'autre sens).
  Le PVC re-attaché reste sur l'hôte qui le monte : le retour exige le même
  UID, sinon re-login.
- **Incident** : le lot de cause racine suit le chemin normal ; ne jamais
  copier un dossier de comptes ni un état de session pour « réparer ».

## Limites

- La qualification TPK réelle (cluster, StorageClass, PSA, connexion des
  sièges) est la tranche Q-TPK, hors série.
- Le code ne déplace aucun fichier : il vérifie et journalise. Le bureau
  déplace des pods et des PVC ; la personne se connecte elle-même.
- Une StorageClass `ReadWriteOnce` ne permet qu'un pod consommateur à la
  fois : supprimer/drainer le pod source est un préalable, pas une option.
