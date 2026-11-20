# Témoin d'isolation et de référence d'un hôte pod (#7694, #7695, #7699)

Preuve `kind` des cinq contrôles de la tranche P2 du design
[`7633-hote-avatars-pod.md`](../../../docs/design/7633-hote-avatars-pod.md)
§5.3 : **identité**, **montage**, **permissions**, **superviseur**, **base**.
Le canon (#7694, B1 de #44265) est rejoué en plus, comme contrôle
complémentaire ; #7698 ajoute le déplacement partagé ↔ dédié, #7699 le
**contrôle du chart de référence** (NetworkPolicy, quota, PDB, digest,
`preStop`, labels). Le témoin est un geste de bureau/opérateur, pas de l'agent
intéressé : il crée un cluster jetable, applique les gabarits, lit les
résultats depuis les conteneurs, imprime une trace, puis supprime le cluster.
Aucun secret n'est lu ni affiché.

## Lancer

```sh
./preuve-isolation-comptes.sh                 # kind + contrôles + nettoyage
./preuve-isolation-comptes.sh --render-only   # rendu des manifestes, sans cluster
./preuve-isolation-comptes.sh --keep          # garde le cluster pour inspection
```

Options : `--uid-a`, `--uid-b`, `--fs-group`, `--image`, `--git-image`,
`--cluster`, `--namespace`. Défauts : UID `2519`/`2520`, superviseur `10001`,
`fsGroup` `10000`, image `busybox:1.36` (épingler un digest en CI :
`--image busybox@sha256:…`), image Git `alpine/git:2.45.2` pour le canon.

Le contrôle **base** exige un PostgreSQL migré, passé par
`AMEESH_TEST_DSN` (le banc de `tests/support.py` crée son schéma et applique
les migrations, 0599 comprise) :

```sh
AMEESH_TEST_DSN=postgresql://ameesh@127.0.0.1:5432/ameesh \
    ./preuve-isolation-comptes.sh
```

Codes de sortie : `0` = tous les témoins passent ; `1` = un contrôle échoue
(le script imprime le diagnostic) ; `77` = `kind`, `kubectl`, `docker` ou
PyYAML absent, **ou** `AMEESH_TEST_DSN` absent (contrôle base non exécuté) —
le témoin est alors **non exécuté / non-concluant**, jamais « vert ».

Le script crée **le consommateur avant d'attendre le binding** : kind installe
une StorageClass `WaitForFirstConsumer`, donc un PVC seul reste `Pending`
(B2 de #44265).

## Ce que chaque contrôle prouve

1. **Identité** : dans le conteneur de A, `id -u` = UID du chart = entrée
   `passwd` (`ameesh-a`), le home a le `fsGroup` neutre, le dossier de comptes
   est `0700` au tenant, le fichier témoin `0600`. Le contrôle est rejoué
   **après suppression et recréation du pod** (déplacement du PVC) : l'UID,
   les modes et le contenu du volume survivent, et l'UID du pod change. Il est
   rejoué une seconde fois **après le passage du PVC dans le pod sans
   `fsGroup`** (contrôle permissions) : ce montage ne doit pas élargir les
   modes.
2. **Montage** : dans A, `/home/ameesh-b` n'existe pas, le PVC de B n'est pas
   dans `/proc/self/mountinfo`, le témoin de A est lisible (contrôle positif),
   et aucun jeton de compte de service n'est monté.
3. **Permissions** : un second pod, sous l'UID de A, monte les **dossiers de
   comptes** des deux PVC en lecture seule par `subPath`, **sans `fsGroup`**
   (exigence N de #7659 : on teste les modes posés par le tenant, pas un
   `chown` de kubelet). A lit son `.temoin-a`, mais `cat`/`ls` sur le dossier
   `0700` de B échoue (`EACCES`).
4. **Superviseur** (#7695) : le conteneur `superviseur` tourne sous
   `SUPERVISOR_UID` (`10001`, entrée `ameesh` de `passwd`), n'a **aucun** home
   ni PVC de tenant (`/home/ameesh-a` et `/home/ameesh-b` absents, absents de
   `mountinfo`), ne reçoit pas de jeton de compte de service, et son `/tmp`
   propre est inscriptible et lisible (contrôle positif : un conteneur sans
   volume tenant n'est pas un conteneur mort).
5. **Base** (#7695) : contre un PostgreSQL réel migré, `WitnessBaseTest`
   vérifie que `ameesh_tenant_claim_ok` refuse le runner d'un autre tenant et
   un intrus, qu'une session `attach:ameesh-<id>@<hôte>` ne s'ouvre qu'à
   l'utilisateur système du tenant, que l'isolation par UID refuse un pair du
   même tenant, et qu'un seul superviseur prend le bail (0599) puis le
   rend. Sans DSN, ce contrôle est **sauté** (sortie `77`).

**Canon (#7694, en plus)** : un pod dédié clone un dépôt sous
`SUPERVISOR_UID` (`10001`) dans un `emptyDir`, puis le lit sous l'UID du
tenant, montage en lecture seule, avec la **paire du plan**
(`GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=safe.directory`,
`GIT_CONFIG_VALUE_0=/canon/repo`). Le contrôle positif exige la lecture sous
un UID différent du clone ; le contrôle négatif, sans la paire, exige le
refus de Git (`dubious ownership`). La confiance reste bornée au dépôt :
jamais `*`.

**Chart de référence (#7699, en plus)** : dans un namespace jetable, le
script rend les objets P6 de `../pod-shared/` (`render.py --check`, puis
`--stdout --kind NetworkPolicy --kind ResourceQuota --kind
PodDisruptionBudget`) et vérifie que l'API les accepte. Il prouve que la
`ResourceQuota` **mord** (un second pod est refusé avec `exceeded quota`),
que le `PodDisruptionBudget` compte **1 pod et 0 interruption**, et que le
rendu porte `image@sha256:…`, `preStop`, `terminationGracePeriodSeconds:
120` et le label de version. Le `kubectl kustomize` complet est aussi
construit. L'**application** de la NetworkPolicy par le CNI n'est pas
prouvée par kind (kindnet) : le runbook de qualification la porte à Q-TPK.

Le pod témoin (trois conteneurs : A, B et le superviseur) a **un seul
`fsGroup` neutre** (`10000`) pour les deux PVC : jamais un `fsGroup` par
tenant, que Kubernetes ne permet pas au niveau d'un pod et dont le passage
récursif élargirait les modes au groupe. Le pod de permissions n'a aucun
`fsGroup` : c'est le point du contrôle 3.

## Ce que le témoin ne prouve pas

L'admin du cluster (il peut monter un PVC), le noyau, les conditions du
fournisseur, l'isolation réseau/IPC entre conteneurs d'un même pod, ni
l'absence de faille dans un harnais. Ces limites sont celles du design §5.1.
La qualification sur `tpk-stg` (stockage, PSA, RBAC réels) reste la tranche
Q-TPK, hors série : ce témoin `kind` prouve le mécanisme, pas la plateforme.

## Trace à joindre au lot

Le script imprime `trace:` (date UTC, version Kubernetes, image et `imageId`,
UID du pod avant/après recréation, UID/GID, modes). Copier cette trace dans
la note du lot ou la PR, avec le lien du run : aucune valeur secrète n'y
figure.

## CI

[`.github/workflows/pod-kind.yml`](../../../.github/workflows/pod-kind.yml)
est **manuel** (`workflow_dispatch`) : il installe kind/kubectl épinglés avec
leur empreinte, démarre PostgreSQL 17 et exécute le témoin complet. Il ne
déclenche pas de CI sur une branche de lot (canon §3/§11 bis).

## Fichiers

- `temoin-pvc.yaml` : deux PVC `ReadWriteOnce`, 128 Mi ;
- `temoin-deux.yaml` : pod à trois conteneurs/UID/PVC + superviseur sans
  volume tenant (`__UID_A__`, `__UID_B__`, `__SUPERVISOR_UID__`,
  `__FS_GROUP__`, `__IMAGE__` remplacés par le script) ;
- `temoin-lecture.yaml` : pod de permissions, `subPath` des dossiers de
  comptes, **sans `fsGroup`** ;
- `temoin-canon.yaml` : pod du contrôle canon (clone sous `10001`, lecture
  sous `__UID_A__` avec la paire `safe.directory` bornée au dépôt et un
  contrôle négatif sans elle ; `__GIT_IMAGE__`, `__SUPERVISOR_UID__`
  remplacés) ;
- `preuve-isolation-comptes.sh` : le script, seul point d'entrée.
