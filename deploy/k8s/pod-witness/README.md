# Témoin d'isolation des comptes d'un hôte pod (#7694)

Preuve `kind` des quatre contrôles de la tranche P1 du design
[`7633-hote-avatars-pod.md`](../../../docs/design/7633-hote-avatars-pod.md)
§5.3 : **identité**, **montage**, **permissions**, **canon**. Le témoin est un
geste de bureau/opérateur, pas de l'agent intéressé : il crée un cluster
jetable, applique les gabarits, lit les résultats depuis les conteneurs,
imprime une trace, puis supprime le cluster. Aucun secret n'est lu ni affiché.

## Lancer

```sh
./preuve-isolation-comptes.sh                 # kind + contrôles + nettoyage
./preuve-isolation-comptes.sh --render-only   # rendu des manifestes, sans cluster
./preuve-isolation-comptes.sh --keep          # garde le cluster pour inspection
```

Options : `--uid-a`, `--uid-b`, `--fs-group`, `--image`, `--git-image`,
`--cluster`, `--namespace`. Défauts : UID `2519`/`2520`, `fsGroup` `10000`,
image `busybox:1.36` (épingler un digest en CI : `--image busybox@sha256:…`),
image Git `alpine/git:2.45.2` pour le contrôle 4.

Codes de sortie : `0` = tous les témoins passent ; `1` = un contrôle échoue
(le script imprime le diagnostic) ; `77` = `kind`, `kubectl` ou `docker`
absent — le témoin est **non exécuté**, jamais « vert ».

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
   `fsGroup`** (contrôle 3) : ce montage ne doit pas élargir les modes.
2. **Montage** : dans A, `/home/ameesh-b` n'existe pas, le PVC de B n'est pas
   dans `/proc/self/mountinfo`, le témoin de A est lisible (contrôle positif),
   et aucun jeton de compte de service n'est monté.
3. **Permissions** : un second pod, sous l'UID de A, monte les **dossiers de
   comptes** des deux PVC en lecture seule par `subPath`, **sans `fsGroup`**
   (exigence N de #7659 : on teste les modes posés par le tenant, pas un
   `chown` de kubelet). A lit son `.temoin-a`, mais `cat`/`ls` sur le dossier
   `0700` de B échoue (`EACCES`).
4. **Canon** (`B1 de #44265`) : un pod dédié clone un dépôt sous
   `SUPERVISOR_UID` (`10001`) dans un `emptyDir`, puis le lit sous l'UID du
   tenant, montage en lecture seule, avec la **paire du plan**
   (`GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=safe.directory`,
   `GIT_CONFIG_VALUE_0=/canon/repo`). Le contrôle positif exige la lecture
   sous un UID différent du clone ; le contrôle négatif, sans la paire,
   exige le refus de Git (`dubious ownership`). La confiance reste bornée au
   dépôt : jamais `*`.

Le pod à deux conteneurs a **un seul `fsGroup` neutre** (`10000`) pour les
deux PVC : jamais un `fsGroup` par tenant, que Kubernetes ne permet pas au
niveau d'un pod et dont le passage récursif élargirait les modes au groupe.
Le pod 3 n'a aucun `fsGroup` : c'est le point du contrôle 3.

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

## Fichiers

- `temoin-pvc.yaml` : deux PVC `ReadWriteOnce`, 128 Mi ;
- `temoin-deux.yaml` : pod à deux conteneurs/UID/PVC (`__UID_A__`,
  `__UID_B__`, `__FS_GROUP__`, `__IMAGE__` remplacés par le script) ;
- `temoin-lecture.yaml` : pod de permissions, `subPath` des dossiers de
  comptes, **sans `fsGroup`** ;
- `temoin-canon.yaml` : pod du contrôle 4 (clone sous `10001`, lecture sous
  `__UID_A__` avec la paire `safe.directory` bornée au dépôt et un contrôle
  négatif sans elle ; `__GIT_IMAGE__`, `__SUPERVISOR_UID__` remplacés) ;
- `preuve-isolation-comptes.sh` : le script, seul point d'entrée.
