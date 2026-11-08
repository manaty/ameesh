# #6321 — AG3 : Docker rootless pour ameesh, retrait du groupe docker — dossier v1

Bureau (BG), 2026-11-08 ~13:45Z. Branche `review/bureau-6321-ag3-rootless` (manaty/ameesh).

**AUCUN GESTE** de remédiation : relevés en lecture seule et essais à blanc uniquement. Une
exception, nommée au §1.4 : un essai à blanc a créé un processus `podman pause`.

Ce dossier reprend la remédiation de **#5781 sur AG1**, exécutée les 04 et 05/11 :
- les phases A, B et C2 ;
- la qualification par un vrai tour et une suite SQL ;
- les leçons de ses incidents (§3).

Il vise aussi les préalables de l'activation de l'adaptateur VM (#6002).

Relecture demandée : deux familles hors claude (corps du lot).

## 1. Inventaire en lecture seule d'AG3 (08/11, 13:36-13:42Z)

### 1.1 Comptes, groupe et processus

- `ameesh` : uid 1001, gid 1001, groupes `1001(ameesh),112(docker)`. `docker:x:112:ameesh` : ameesh en est le seul membre.
- Processus de l'uid 1001 : de 12 à 15 selon l'activité. Ceux qui ont le groupe 112 : tous, sauf un.
  Tous tournent **dans `user@1001.service`** :
  - les 8 runners `ameesh-runner-agent@{ameesh-cx3, ameesh-dev1, ameesh-dev2, ameesh-dev4, ameesh-dev7, claude1, claude2, codex2}` ;
  - `ameesh-pgbouncer.service` ;
  - `dbus` ;
  - `init.scope`.

  **Aucun processus uid 1001 hors du gestionnaire**, contrairement à AG1, qui avait un pause
  Podman dans une session root.
- Le `podman pause` existant au relevé de 13:37Z appartient à **ameesh-job** (uid 1002, groupe
  `ameesh-jobs` 987, invités de l'hôte prêteur). Il n'a pas le groupe 112 et sort du périmètre.
- Pas de sudo (« not allowed »), pas de crontab, pas de spool at.
- `~/.ssh` contient `config`, `known_hosts` et la clé `offload-agents-3_ed25519` (contenus non lus).
- Linger : yes.
- **Aucun rôle élu sur AG3.** Les candidats d'`auditeur` et de `mesh-design` sont AG1 et AG2. Il n'y
  a pas de minuterie de composeur (`ameesh-paquet-horaire`) à masquer, contrairement à AG1
  (N7 de #5781).

### 1.2 Docker rootful (29.1.3) : conteneurs, volumes, images

| Conteneur | Image | Port (127.0.0.1) | Volume | Taille | Redémarrage |
|---|---|---|---|---|---|
| `ameesh-pg` | postgres:17 `b8faeff7` | 55432 | `ameesh-pg-data` | 1,8 Go | no |
| `ameesh-pg-dev1` | idem | 55441 | `ameesh-pg-dev1-data` | 655 Mo | no |
| `ameesh-pg-dev2` | idem | 55443 | `ameesh-pg-dev2-data` | 747 Mo | no |
| `ameesh-pg-dev4` | idem | 55442 | `ameesh-pg-dev4-data` | 277 Mo | no |

Caractéristiques communes aux quatre conteneurs :
- sains, en marche depuis 12 jours ;
- `POSTGRES_USER=agent_mesh` ;
- un seul montage, entrypoint `docker-entrypoint.sh`, commande `-c autovacuum_*`.

Autres éléments du démon rootful :
- **Volume anonyme** `b8cbe748…` (39 Mo, du 02/11) : aucun conteneur ne l'utilise. Il n'est pas touché.
- **Images** :
  - postgres:17, postgres:16, node:22-bookworm-slim et ubuntu:24.04 (identifiants exacts dans le script) ;
  - images construites par des tours d'agents, toutes reconstructibles ou retirables (l5502 §1.1) :
    `nexlink-web:lot-6193` (22 h), `ima-private-headroom-pg:1efe6e6`, `caddy:2.10.2` et `keycloak:26.4.2`.
- **Journal du démon sur 72 h** : 84 `exec` dans postgres:17, aucune création de conteneur.

Consommateurs :
- `ameesh-pg` sert aux suites SQL (pg-up/test.sh).
- Les ports 55441 à 55443 servent aux worktrees des agents dev.
- La garde d'invités de l'hôte prêteur refuse ces ports aux invités (`--deny-port 55432,55441,55442,55443,34949`,
  `deploy/guest/PILOT-ameesh-agents-3.md`).

### 1.3 Prérequis rootless : déjà présents

Ils ont été posés le 02/11 vers 22:31Z, pour l'essai l5502 sur AG3 ; leur origine est relevée dans `dpkg.log`.

- **Paquets** : `rootlesskit 1.1.1-1ubuntu0.24.04.3`, `uidmap`, `slirp4netns`, `fuse-overlayfs`,
  `dbus-user-session` et `podman 4.9.3`. `dpkg --verify` est OK sur 7 paquets.
- **Programmes identiques au kit relu `805890af`** : `docker`, `dockerd`, `runc`, `rootlesskit`,
  `newuidmap` et `newgidmap` (préfixes sha256 vérifiés).
- **Écart** : `containerd` est en 2.2.1-0ubuntu1~**24.04.4**, alors que le kit porte la version
  précédente. Seuls `dpkg --verify` et l'origine `noble-updates` l'authentifient (point ouvert O2).
- **Scripts Moby** : `/usr/local/bin/dockerd-rootless-setuptool.sh` (`e68ef655…`) et
  `dockerd-rootless.sh` (`8eb00d61…`), en root 0755, identiques au kit relu.
- **Lien** `/usr/local/bin/docker → /usr/bin/docker`, présent depuis le 02/11. C'est justement ce
  qui manquait sur AG1 (setuptool l.377).
- `subuid`/`subgid` : `ameesh:165536:65536`. Profil AppArmor `rootlesskit` chargé, alors que
  `apparmor_restrict_unprivileged_userns=1`.
- **Jamais lancé ici** : aucun moteur rootless, ni `~/.local/share/docker`, ni `~/.config/docker`.
  Contexte docker : `default`. Pas d'`environment.d`, et pas de `DOCKER_HOST`.
- **Réseau** : `docker0` en 172.17.0.0/16, WireGuard en 10.77.0.5/32. Le pool rootless proposé,
  172.30.0.0/16, est libre.
- **Espace** : 80,2 Gio libres. Besoin : environ 3,8 Gio (4 images et 4 volumes), plus la marge de 10 Gio.

### 1.4 Incident pendant l'inventaire (dit, non corrigé sur l'hôte)

- Deux lectures ont lancé `podman ps` sous ameesh par `runuser`, depuis root : l'une à 13:37Z,
  l'autre pendant l'essai à blanc de la phase B, à 13:42:03Z.
- La seconde a laissé un `podman pause` (PID 1704662) qui garde le groupe 112. Il est **dans**
  `user@1001.service` (scope `podman-pause-c759da9a`), donc il disparaîtra au redémarrage du
  gestionnaire, en phase B.
- C'est exactement le piège N8 de #5781. Le script ne lance plus aucune commande podman : la phase B
  lit le registre de conteneurs Podman en fichier, et exige que tout pause soit dans le gestionnaire.
- Ce processus n'a pas été tué (aucun geste).

## 2. Plan : trois phases et un retour, un contrôleur unique

Le contrôleur unique est `ag3-rootless-6321.py`, exécuté **sur AG3** en root. Il dérive des
contrôleurs exécutés sur AG1 : `phase-A-engine-and-PG.py` `d5b50510…`, `phase-B-client-and-groups.py`,
`phase-C-local-verification.py`, `phase-C-correct-module-list.py` et `pre-key-rollback.py`.

Règles communes :
- **À blanc par défaut** : contrôles en lecture, plan imprimé, aucun fichier créé (pas même le
  dossier de reçus). Les 5 sous-commandes ont été essayées à blanc sur AG3 (relevé joint).
- **`--apply`** exige une autorité fraîche `/root/ameesh-operator-6321-AG3/<phase>-authority.json`
  (root 0600, moins de 600 s, feu du pilote, avis codex et deepseek), ainsi que le drainage natif
  d'AG3 et l'absence de tour, de staging et de session personnelle.
- Refus entre 09:40Z et 11:30Z, et entre 21:40Z et 23:30Z (coupures et staging sur AG3).
- Aucun rejeu après un échec : le reçu de progression interdit de relancer, il faut réconcilier.

| Phase | Gestes | Preuves exigées dans la phase |
|---|---|---|
| **A** moteur et PG | Instantanés privés (configuration du compte, unités utilisateur, contexte). `daemon.json` rootless (pool 172.30/16). `dockerd-rootless-setuptool.sh install --force --skip-iptables` sous l'uid 1001, avec un `DOCKER_CONFIG` jetable **retiré ensuite**. Chargement des 4 images **par identifiant**, sans pull. Pour chaque PG, l'un après l'autre : volume cible créé, source **arrêtée et conservée** (redémarrage `no` inchangé), copie tar `--numeric-owner`, conteneur rootless même image, même port, même commande et même `Env` (fichier 0600 détruit), `--restart=unless-stopped` | Programmes et scripts conformes, `docker.service` utilisateur `enabled`, `SecurityOptions` rootless, pilote ≠ vfs, `DockerRootDir` sous home. Pour chaque PG : manifeste octets et propriétaires identiques, santé, bases et `schema_migrations` identiques, même image. Contexte ordinaire inchangé |
| **B** client et groupe | `DOCKER_HOST` rootless dans `~/.config/ameesh/env` et `environment.d/90-ameesh-docker-rootless.conf` (écrit sous l'uid 1001), `gpasswd --delete ameesh docker`, `systemctl restart user@1001.service`, puis relance des unités natives listées dans l'autorité (8 runners et pgbouncer) | Aucun processus uid 1001 hors du gestionnaire, aucun avec le groupe 112. Groupes interdits absents (sudo, wheel, admin, adm, docker, lxd, libvirt, systemd-journal). Socket rootful refusée (EACCES) sous l'uid 1001. 4 PG rootless sains, contexte inchangé |
| **C** qualification locale | Aucune mutation, à part un fichier témoin root 0600 et un dossier de travail du compte | `sudo -l` refusé ; le montage d'un fichier root 0600 est illisible depuis un conteneur rootless ; **module SQL réel** `test_work` (liste « test_work ») par `test-parallele.sh -n 1 -p psql`, dans une **unité utilisateur** (environment.d effectif) ; `ameesh doctor` ; `ameesh menage --dry-run --json` |
| **Retour** (avant toute clé) | PG rootless arrêtés, `docker.service` utilisateur `disable --now`, configuration restaurée octet pour octet depuis l'instantané, groupe docker rétabli et gestionnaire redémarré s'il avait été retiré, PG rootful relancés | Santé et bases rootful = référence. Volumes rootless **conservés** : pas de copie inverse, écritures réconciliées d'abord (case d'autorité `target_writes_reconciled`) |

**Ensuite, gestes natifs** (pas dans le script) :
1. Levée du drainage d'AG3.
2. Porte d'hôte ouverte et 8 runners revenus, en lecture (`ameesh hosts`, `role status`).
3. **Un vrai tour d'agent sur AG3**, prouvé en base : ligne du registre des tours et bail
   `runner-…@ameesh-agents-3`.
4. Attestation indépendante (auditeur). Les sources rootful sont gardées au moins 7 jours.

## 3. Leçons de #5781 reprises

| Incident ou avis sur AG1 | Ici |
|---|---|
| umask 077 : scripts installés en 0700, rc 126 (05/11 01:44Z) | Rien n'est installé : les prérequis sont présents et vérifiés (mode 0755 exigé) |
| setuptool l.377 `$BIN/docker` absent, rc 127 (01:53Z) | Le lien `/usr/local/bin/docker` existe sur AG3 (02/11) ; il est vérifié, jamais créé ni retiré par ce lot |
| Répertoires de contexte orphelins (N2) | Le `DOCKER_CONFIG` jetable est retiré en fin d'installation et consigné |
| Liste de modules « tests.test_work » refusée | La phase C écrit « test_work » |
| Pause Podman hors gestionnaire et N8 | Aucun pause hors gestionnaire sur AG3. Aucune commande podman dans le contrôleur (§1.4) |
| N1 : images de test (pg-up, test-parallele, Nexlink) | postgres:17, postgres:16, node:22-bookworm-slim et ubuntu:24.04 chargés par identifiant ; les autres images restent dans le rootful (O1) |
| N7 : composeur et timers ; drain par défaut 8 h | Pas de composeur ni de rôle sur AG3. Drainage natif `--for 4h`, borné par la fenêtre |
| N9 : ensemble des unités | Instantanés `list-unit-files` et `list-units` avant ; unités à relancer listées dans l'autorité, contrôlées après |
| Porte d'hôte « snapshot mismatch » (#5848) et runners « lost » (#5869) | La qualification n'est annoncée qu'avec la porte ouverte et les 8 runners revenus, en lecture native. Sinon le drainage est maintenu, sans réparation sur place |
| Politique de redémarrage `no` → `unless-stopped` | Changement voulu et dit (précédent AG2 B2 et AG1) ; la source rootful reste en `no` |
| Résiduel `installer-serveur.sh` qui ajoute le groupe docker (l5502 §5, suite #5784) | À vérifier avant la clôture (O6) |

## 4. Fenêtre proposée

- **09/11, de 13:00Z à 15:30Z (21:00-23:30 à Manille).** Ce créneau tombe après la coupure de 10:00Z
  et le C3 éventuel, dont le staging d'AG3 se termine vers 11:30Z. Il reste à plus de 20 min de 22:00Z.
- **Alternative : cette nuit, de 23:45Z à 02:00Z**, si le staging de 22:00Z est attesté avant 23:30Z.
- Durées estimées : A de 20 à 30 min (4 copies, 3,5 Go) ; B 5 min ; C de 10 à 15 min ; plus le tour réel.
- Drainage natif posé avant A et levé seulement après C, avec la porte ouverte.
- Retour commencé au plus tard 60 min avant la coupure suivante.

## 5. Critères de qualification (clôture)

Tous sont exigés :
1. aucun processus uid 1001 n'a le groupe 112 ;
2. `id ameesh` ne montre pas docker ;
3. socket rootful refusée en EACCES ;
4. groupes interdits absents ;
5. sudo refusé ;
6. lecture d'un fichier protégé refusée ;
7. 4 PG rootless sains, avec bases et `schema_migrations` identiques à la référence ;
8. module SQL réel OK ;
9. doctor et ménage OK ;
10. porte ouverte et 8 runners actifs ;
11. **vrai tour d'agent sur AG3** prouvé en base ;
12. sources rootful conservées au moins 7 jours ;
13. attestation de l'auditeur.

Sinon, la remédiation n'est pas faite.

## 6. Points ouverts pour mesh-design

- **O1. Images.** Les images construites par les agents (`nexlink-web:lot-6193`,
  `ima-private-headroom-pg`, `caddy`, `keycloak`) ne sont pas migrées : les tours les reconstruiront
  ou les tireront en rootless (l5502 §1.1). Elles restent dans le rootful, désormais inaccessible.
  Est-ce acceptable ? Sinon, quelle liste charger ?
- **O2. containerd 24.04.4** n'est pas dans le kit relu `805890af`. Faut-il accepter
  `dpkg --verify` et l'origine `noble-updates`, ou exiger une vérification signée hors ligne comme pour le kit ?
- **O3.** Migrer les **4** PG, y compris ceux des agents dev (proposé), ou seulement `ameesh-pg`,
  en laissant les dev à reconstruire ? Leur arrêt coupe les worktrees `nexlink-dev*`/`ameesh-dev*`
  pendant la fenêtre.
- **O4. Incident §1.4** : un `podman pause` uid 1001 avec le groupe 112, créé par mon essai à blanc
  de 13:42Z, reste dans `user@1001` jusqu'à la phase B. Faut-il le laisser (proposé) ou prévoir un
  geste séparé ?
- **O5. Fenêtre** : 09/11 13:00Z (proposé), ou cette nuit 23:45Z.
- **O6.** Lot de suite `installer-serveur.sh` (#5784, groupe docker) : est-il livré ? Sinon, un
  nouvel hôte pourrait rouvrir la voie.
- **O7.** Hôte prêteur : les ports restent identiques (redirection rootlesskit sur 127.0.0.1) et la
  garde d'invités les refuse toujours. À vérifier en phase C par une lecture de la garde ; aucun
  changement prévu.

## 7. Pièces

| Fichier | Rôle |
|---|---|
| `DOSSIER-6321-AG3-rootless-v1.md` | Ce dossier |
| `ag3-rootless-6321.py` | Contrôleur ; à blanc par défaut, `--apply` exigé |
| `inventaire-AG3-6321-20261108.json` | Relevé public (processus, conteneurs, prérequis), sans secret |
| `releve-6321-blancs-*.txt` | Les 5 sous-commandes à blanc sur AG3, plus la phase B relancée sans podman |
| `ref-5781/` | Contrôleurs et plan relus d'AG1 cités (copies publiques) |
| `SHA256SUMS` | Empreintes |
