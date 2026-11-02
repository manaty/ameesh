---
type: Study
title: "L5502 — Docker rootless pour le compte ameesh sur agents-1 et agents-2 (staging élu, L0)"
description: "Inventaire des usages Docker du compte ameesh, essai rootless sur ameesh-agents-3 (non candidat), et plan de bascule par hôte jusqu'au retrait du groupe docker."
status: draft
tags: [staging, droits, docker, rootless, hote]
generated: { by: "mty-amh-impl-13", at: "2026-11-02" }
---

# Recommandation

- **Docker rootless pour ameesh** sur agents-1 (AG1) et agents-2 (AG2), puis
  retrait du groupe `docker`. C'est la cible du pilote, et rien de ce qui a été
  essayé ne casse sans contournement simple.
- **Avant la première bascule, un essai de confirmation (15 min) sur
  ameesh-agents-3 (AG3).** L'essai du 02/11 a tourné sous Podman rootless, déjà
  installé sur AG3 et utilisable sans geste root. Les constats propres aux
  espaces de noms utilisateur (uid, droits, ports, cgroups, isolation) valent
  pour les deux moteurs. Les points propres à Docker (rootlesskit, healthcheck
  dans le démon, redémarrage du service) restent à confirmer. Pour cela, le
  bureau doit d'abord poser les prérequis (étape 0). L'essai est isolé :
  `DOCKER_CONFIG` jetable, puis nettoyage complet.
- **Repli sans dépense** si Docker rootless ne passe pas : Podman rootless
  derrière la même CLI `docker` (`DOCKER_HOST` vers le socket de l'API
  Podman), avec trois réglages (§3.2). Le repli décrit dans le lot, des hôtes
  candidats dédiés sans agents, n'est plus nécessaire selon cet essai.
- **Le retrait du groupe ne vaut qu'après redémarrage de `user@1001.service`.**
  Les processus vivants gardent le groupe 112 dans leurs credentials. Le
  redémarrage arrête tous les processus ameesh de l'hôte, d'où le drainage
  (§4).

# 1. Inventaire des usages

Sources : lecture du dépôt ameesh (`origin/main` 71b70dd5d), de Nexlink
(`origin/develop`), d'AG3 en lecture, du constat du pilote sur AG2 (corps du
lot) et de `ameesh hosts` (02/11 14:2xZ). AG1 et AG2 ne sont pas joignables
en ssh depuis AG3. L'inventaire par hôte en dessous vient donc de la mesure
publiée par chaque hôte, plus le constat du pilote.

## 1.1 Par hôte (conteneurs et volumes à longue vie)

| Hôte | Conteneurs | Volumes utiles | Ports |
|---|---|---|---|
| AG1 | `ameesh-pg` (postgres:17), `pgss-probe-1624094` (lot #5263, `waiting_human`) | `ameesh-pg-data` | 127.0.0.1:55432 |
| AG2 | `ameesh-pg` (postgres:17), deux postgres17 de tests (`b7eade71…`, `7894f8a2…`, 2 semaines) | `ameesh-pg-data` | 127.0.0.1:55432 |
| AG3 (hors lot) | `ameesh-pg`, `ameesh-pg-dev1/2/4`, `nexlink-fgs-5112` (lot #5112) | `ameesh-pg-*-data` | 127.0.0.1:55432, 55441-55443 |

Images sur AG2 (constat du pilote) : `nxl-chromium-node`, `nxl-chromium-155`,
`nexlink-lab-sway`, `nexlink-rust-2730`, `linuxserver/ffmpeg`, `rust`, `node`,
`python`, `postgres` 16/17, `caddy`. Celles qui commencent par `nxl-` ou
`nexlink-` ont été construites par des tours d'agents ; aucune n'est
référencée dans un dépôt, sauf `nexlink-lab-sway` (construite par
`lab/linux/providers/local.sh`). Toutes se reconstruisent ou se retirent du
registre : aucune n'est une donnée à migrer.

La base partagée du mesh est sur manaty-mesh-1, hors Docker de ces VM : la
bascule ne la touche pas.

## 1.2 Qui lance Docker sous le compte ameesh

| Usage | Lancé par | Drapeaux | Long/éphémère |
|---|---|---|---|
| `scripts/pg-up.sh` | `scripts/test.sh` (suites SQL), `make pg-up` | `--init`, `-p 127.0.0.1:PORT:5432`, volume nommé, `--health-cmd` | long (`ameesh-pg`) |
| `scripts/test-parallele.sh` | `make test-parallele` | `--rm`, `-p 127.0.0.1::5432`, `--tmpfs` | éphémère |
| `scripts/path-b-trial/trial.py` | essai manuel L115 | build, 4 montages, `chown 10001`, écoute sur `172.17.0.1` | éphémère |
| `src/ameesh/containers.py` (shim L73, règlement des tours) | chaque tour | `ps/rm/volume/image` (étiquettes) | — |
| `src/ameesh/housekeeping.py`, `docker_storage.py` | `ameesh menage` | `volume ls/rm`, `image rm`, `buildx du` | — |
| `src/ameesh/resources.py` | garde de ressources | `docker info {{.DockerRootDir}}` | — |
| `src/ameesh/netcheck.py` | `ameesh menage`, `ameesh doctor` | `network ls/inspect` + routes de l'hôte | — |
| Nexlink : `scripts/sql-tests.sh`, `node-sql-tests.sh`, `perf-trust-bench.sh`, `test-release-10*-runner.sh`, `mp12-*.sh` | tours Nexlink | postgres, `-p 127.0.0.1:PORT:5432` | éphémère |
| Nexlink : `lab/linux/providers/local.sh` (sway, gnome, kde) | tours Nexlink | `--network none --cap-drop ALL --cap-add SETUID,SETGID --security-opt no-new-privileges`, `docker build` | éphémère |
| Nexlink : `infra/federation-bench/*.sh` | à la main | `docker compose`, `exec`, builds | éphémère |
| Nexlink : `infra/compute-vm/*-test.py`, `infra/gcp/test-relay-*.sh` | à la main | `--cap-add NET_ADMIN`, `--network host` | éphémère |
| Builds Rust, chromium (images `nxl-*`) | tours Nexlink, ad hoc | inconnus (non versionnés) | éphémère |

Aucun appel `docker` d'ameesh ne passe `--privileged`, `--device`,
`--cgroup-parent`, `--cpus` ou `--memory`. Les CI GitHub (`ameesh-ci`, VM
dédiées) tournent sous `runner` ou `ameesh-guest` et ne sont pas concernées.
Les invités (`ameesh-guest`) sont déjà sous Podman rootless et ne peuvent pas
atteindre `/run/docker.sock` (`deploy/guest/ci-runners.sh`).

Seule ligne qui ajoute ameesh au groupe : `deploy/serveur/installer-serveur.sh:34`
(`usermod -aG docker ameesh`). Le vérificateur de migration du poste
(`deploy/poste/migration-runners-ameesh.py`, `groupes_interdits`) refuse déjà
ce groupe et cite Docker rootless comme sortie.

# 2. Essai du 02/11 sur AG3

Annoncé à mesh-design (copie bureau) avant de commencer. Aucun geste root,
démon rootful intact. Moteur : Podman 4.9.3 rootless (runc, overlay natif,
cgroup v2, gestionnaire systemd), stockage isolé sous `$TMPDIR`, API
compatible Docker dans une unité `systemd --user` transitoire, client
`/usr/bin/docker` 29.1.3 via `DOCKER_HOST`. Tout a été retiré à la fin
(conteneurs, volumes, images, stockage, unité).

Prérequis constatés sur AG3 : `uidmap` (newuidmap setuid), `ameesh:165536:65536`
dans `/etc/subuid` et `/etc/subgid`, `slirp4netns`, `fuse-overlayfs`, linger
actif, noyau 6.8. `kernel.apparmor_restrict_unprivileged_userns=1` : un
`unshare -U` direct est refusé. Seuls les binaires qui ont un profil AppArmor
(`podman`, `crun`, `runc`, `rootlesskit`, `chrome`, …) obtiennent un espace
de noms utilisateur. Délégation cgroup de `user@1001.service` : `cpu memory pids`
(pas `io` ni `cpuset`).

| Cas | Résultat | Détail |
|---|---|---|
| postgres:17, `-p 127.0.0.1:P:5432`, volume nommé, `--memory 1g --cpus 2` | **passe** | `memory.max=1073741824`, `cpu.max=200000 100000` appliqués ; scope sous `user@1001.service/user.slice/libpod-*.scope` |
| `scripts/pg-up.sh` tel quel (`AMEESH_PG_NAME/PORT/PASSWORD/DISPOSABLE`) | **passe après réglage** | `--init` exige `catatonit`, absent → `init_path = "/usr/bin/docker-init"` dans `containers.conf` (sans root) |
| healthcheck (`--health-cmd`) | **ne tourne pas** (Podman, AG3) | Podman passe par des minuteries systemd ; `/run/user/1001/systemd/private` refuse les connexions depuis le 01/11 02:05 (socket périmé du gestionnaire utilisateur). `podman healthcheck run` à la main → `healthy`. Docker rootless exécute le healthcheck dans le démon : non concerné |
| Débit SQL via le port publié (pgbench -S, 4 clients, 10 s) | **pas de perte** | rootless 18 268 tps (0,219 ms) ; rootful même machine 16 545 tps (0,242 ms) ; dans le conteneur 18 530 tps |
| Labo sway Nexlink : build de `lab/linux/images/sway` (Arch, pacman) | **passe** | 78 s, constructeur classique (pas de buildx) |
| Labo sway : `docker run` avec les drapeaux exacts de `local.sh` | **passe** | bureau prêt au 2e sondage, `grim` (capture), `probe.sh` : PipeWire, portail ScreenCast, sortie wlr |
| Chromium (Playwright 1.59.1 noble) `--headless=new --no-sandbox` | **passe** | |
| Chromium avec bac à sable (pwuser, drapeaux par défaut, `--ipc=host`) | **passe** | espace de noms imbriqué accordé |
| Chromium avec bac à sable sous `--cap-drop ALL --security-opt no-new-privileges` | **casse** | `FATAL zygote_host_impl_linux.cc:221`. Attendu à l'identique en rootful : pas de CAP_SYS_ADMIN ni de helper setuid. Non mesuré en rootful. Remède : `--no-sandbox`, ce que Playwright fait par défaut |
| Rust (`rust:1-slim`, `cargo build`) sur un montage du worktree, en root du conteneur | **passe** | fichiers créés possédés par ameesh (1001) |
| Même chose avec `--user "$(id -u):$(id -g)"` | **casse** | uid 1001 du conteneur = 166536 sur l'hôte : pas d'écriture dans un dossier d'ameesh. `--userns=keep-id` refusé par la CLI docker |
| Fichier écrit par un uid non root du conteneur sur un montage | **à savoir** | possédé par 165536+uid (ex. 166535) : ameesh ne peut pas le modifier sans `podman unshare` / `rootlesskit` |
| `--cap-add NET_ADMIN` | **passe** | capacité dans l'espace de noms seulement |
| `--network host` | **passe** | réseau de l'hôte (Podman) ; sous Docker rootless, c'est l'espace réseau de rootlesskit, pas celui de l'hôte |
| Isolation : `-v /etc/shadow`, `-v /root` | **refusé** | `Permission denied` : le conteneur n'a que les droits d'ameesh |
| Redémarrage de l'unité de l'API | **casse en silence** (Podman) | `rootlessport` des conteneurs créés avant est tué avec l'unité ; le conteneur reste `Up`, port affiché, connexion refusée. Remède : `KillMode=process` (unité `podman.service` amont). Docker rootless : redémarrer `docker.service` arrête tous les conteneurs (pas de live-restore), ce qui casse visiblement |

## 2.1 Ce qui casse, précisément

1. **`--user $(id -u):$(id -g)` sur un montage d'ameesh.** C'est le seul
   motif qui casse partout en rootless. Remède : lancer en root du conteneur
   (= ameesh sur l'hôte). Aucun script d'ameesh ne le fait. Nexlink le fait
   dans `desktop-build.yml:812`, qui tourne sur la CI et non sous ameesh.
   À signaler à Nexlink si un tour le rejoue en local.
2. **`trial.py` (L115)** : écoute sur `172.17.0.1` (`docker0` n'existe plus sur
   l'hôte), `chown 10001` (exige root, et 10001 du conteneur ≠ 10001 de
   l'hôte). Essai manuel ancien : il faudra l'adapter s'il resert, sans
   retenir la bascule.
3. **Chromium avec bac à sable sous `--cap-drop ALL`** : même limite qu'en
   rootful, `--no-sandbox`.
4. **Ports < 1024** (image `caddy`) : refusés en rootless tant que
   `net.ipv4.ip_unprivileged_port_start=1024`. Aucun script ne publie un
   port bas. La `caddy` de `federation-bench` écoute dans le réseau compose,
   donc un port publié ≥ 1024 suffit.
5. **ameesh lui-même** (constats de lecture du code, pas de l'essai) :
   - `DOCKER_HOST` doit être dans l'environnement de toutes les unités ; sinon
     la CLI vise `/var/run/docker.sock`, refusé après le retrait.
     `DOCKER_HOST` prime sur tout contexte, `--context` compris (mesuré par
     impl-08) : avec lui, `default` suit le démon rootless. Le piège est le
     **`docker context use rootless` que `dockerd-rootless-setuptool.sh
     install` exécute lui-même** (contrib moby, `cli_ctx_use`). Il écrit
     `~/.docker/config.json`, si bien que tout `docker` non qualifié change de
     démon dès l'installation. Un processus sans `DOCKER_HOST` après le
     retrait voit `default` (socket rootful) muet. `all_runtimes()`
     (`containers.py:537-570`) le classe alors en moteur muet, et le
     règlement des tours morts reste suspendu tant qu'il le reste
     (`runner.py:14289-14302`, « container engine … not answering »).
     L'installation se fait donc avec un `DOCKER_CONFIG` jetable (§4,
     étape 2), et le contexte courant reste `default`.
   - `netcheck.py` lit les ponts Docker comme s'ils étaient dans l'espace
     réseau de l'hôte : en rootless, faux `host_network_overlap` possible, et
     le remède cité (`/etc/docker/daemon.json`) devient
     `~/.config/docker/daemon.json`.
   - `resources.py` : `DockerRootDir` devient `~/.local/share/docker`, sur le
     disque de `/home`. Ça fonctionne ; les volumes rootful ne sont plus vus
     par le ménage, ce qui est voulu.

# 3. Moteur

## 3.1 Docker rootless (retenu)

- Prérequis root (bureau, une fois par hôte) :
  `apt-get install rootlesskit uidmap slirp4netns fuse-overlayfs`
  (rootlesskit 1.1.1-1ubuntu0.24.04.3 ; profil AppArmor
  `/etc/apparmor.d/rootlesskit` fourni pour `/usr/bin/rootlesskit`, à
  vérifier chargé puisque `kernel.apparmor_restrict_unprivileged_userns=1`).
  Sur AG2, impl-08 a relevé l'absence des quatre paquets à 14:40Z.
  `rootlesskit` tire `uidmap` et recommande `slirp4netns`, mais pas
  `fuse-overlayfs`. Le noyau 6.8 permet l'overlay natif en rootless (essai
  AG3) ; `fuse-overlayfs` reste le repli, pour ne pas tomber sur `vfs`, qui
  multiplie l'espace disque ; `dockerd-rootless.sh` et
  `dockerd-rootless-setuptool.sh`, qui ne sont pas fournis par le paquet
  `docker.io` d'Ubuntu, posés dans `/usr/local/bin` depuis
  `moby/moby@docker-v29.1.3:contrib/` (même version que le démon, empreinte
  sha256 consignée). Facultatif : `docker-buildx` (sinon `buildx du`
  signale `build_cache_unsupported`, sans effet).
- Ensuite, sans root, sous ameesh :
  `DOCKER_CONFIG=$(mktemp -d) dockerd-rootless-setuptool.sh install
  --force --skip-iptables`. `--force` est requis : le démon rootful et son
  socket restent actifs pendant toute la bascule, et le script refuse
  l'installation dans ce cas sans ce paramètre
  (https://docs.docker.com/engine/security/rootless/, section Install).
  Le `DOCKER_CONFIG` jetable reçoit le `context use` du
  script, et `~/.docker/config.json` n'est pas touché. Le script crée
  `~/.config/systemd/user/docker.service`, et
  `~/.config/docker/daemon.json` reçoit `default-address-pools` (hors
  10.77.0.0/16).
- Le healthcheck tourne dans le démon : le socket systemd périmé d'AG3 n'a
  pas d'effet.

## 3.2 Podman rootless (repli, déjà essayé)

Rien à installer sur AG3. Sur AG1 et AG2, le bureau vérifie la présence de
`podman` ; l'installeur des invités le pose (`deploy/guest/install.sh`).
Trois réglages sont nécessaires :
1. `~/.config/containers/containers.conf` : `init_path = "/usr/bin/docker-init"`,
   ou `apt-get install catatonit` (root).
2. API (`podman system service`) en unité utilisateur permanente avec
   `KillMode=process`, sinon le port forwarding meurt en silence au
   redémarrage.
3. Healthchecks : ils dépendent du socket privé du gestionnaire systemd
   utilisateur. Sur AG3, il est périmé (suivi séparé : #5505).

# 4. Plan de bascule par hôte

Ordre : **AG2 d'abord** (moins de conteneurs à longue vie, pas de lot humain
en attente sur un conteneur), puis **AG1**, une fois `pgss-probe-1624094`
réglé avec le porteur de #5263. Un seul hôte à la fois : l'autre garde la
capacité. Durée estimée : 1 h par hôte, entièrement sous drainage (étapes 1 à 9).

Notation : `RF=unix:///var/run/docker.sock` (démon rootful) et
`RL=unix:///run/user/1001/docker.sock` (démon rootless). De l'étape 2 à
l'étape 6, **toute commande `docker` du plan porte `DOCKER_HOST=$RF` ou
`DOCKER_HOST=$RL`** ; aucune ne dépend du contexte courant.

| # | Geste | Qui | Preuve |
|---|---|---|---|
| 0 | **Essai de confirmation sur AG3**, après les avis et après la relance du staging du soir (pilote, 14:33Z). Prérequis §3.1 (bureau), puis installation `dockerd-rootless-setuptool.sh install --force --skip-iptables` (démon rootful conservé) avec `DOCKER_CONFIG` jetable, `data-root` sous `$TMPDIR`, unité `docker.service` utilisateur. Même grille qu'au §2, toutes les commandes sous `DOCKER_HOST=$RL`. **Nettoyage** : `systemctl --user disable --now docker`, `dockerd-rootless-setuptool.sh uninstall` (même `DOCKER_CONFIG`), retrait de l'unité, du `DOCKER_CONFIG` jetable, du `~/.config/docker/daemon.json` (qui porte le `data-root` sous `$TMPDIR`) et du `data-root` (`rootlesskit rm -rf`) | bureau (root, prérequis), puis mty-amh-impl-13 | grille verte consignée sur le lot ; après nettoyage : `docker context ls` = `default *` seul, `docker info` non qualifié = rootful, `systemctl --user status docker` absent, `~/.config/docker/daemon.json` absent, aucun processus `rootlesskit`/`dockerd` d'ameesh |
| 1 | **Drainer l'hôte** et le garder drainé jusqu'à l'étape 9 : plus de nouveau tour, tours en cours finis ou rendus (§4 du processus) | pilote / bureau | aucun tour actif (`ameesh hosts`) |
| 2 | **Marge disque** : `df -h /` et `DOCKER_HOST=$RF docker system df`. Seuil : espace libre ≥ taille des images à recharger (étape 5) + taille du volume `ameesh-pg-data` + 10 GiB. Sinon, purge d'abord des images rootful inutiles (tout sauf `postgres:17`) et des conteneurs de test arrêtés, par le bureau. Repère : AG2 à 14:40Z, 19,4 GiB libres sur 135 G, images rootful 13,74 GB, volumes 3,87 GB (impl-08) | agent (mesure), bureau (purge) | sortie de `df` et `system df` collée sur le lot |
| 3 | Prérequis §3.1 ; vérifier `newuidmap`/`newgidmap` setuid, `slirp4netns`, `fuse-overlayfs`, `/usr/bin/docker-init`, `ameesh` dans `/etc/subuid`/`/etc/subgid` (65536), linger, profil `rootlesskit` chargé (`aa-status`) | bureau (root) | sortie des commandes |
| 4 | Sous ameesh : `DOCKER_CONFIG=$(mktemp -d) dockerd-rootless-setuptool.sh install --force --skip-iptables` (`--force` : démon rootful conservé), `~/.config/docker/daemon.json`, `systemctl --user enable --now docker` | agent | `DOCKER_HOST=$RL docker info` : `rootless` dans `SecurityOptions`, pilote de stockage `overlay2` ou `fuse-overlayfs` (pas `vfs`) ; **`docker context ls` inchangé (`default *`) et `docker info` non qualifié = rootful** |
| 5 | Images utiles : `DOCKER_HOST=$RF docker save postgres:17 … \| DOCKER_HOST=$RL docker load`, plus les images d'un lot en cours ; le reste est reconstruit à la demande | agent | `DOCKER_HOST=$RL docker images` |
| 6 | Migrer `ameesh-pg-data` : `DOCKER_HOST=$RF docker stop ameesh-pg`, copie du volume par `tar` en flux d'un conteneur `$RF` vers un conteneur `$RL` (uid numériques préservés, PG 17 → 17), puis `DOCKER_HOST=$RL scripts/pg-up.sh` avec le même nom, le même port et le même mot de passe | agent (encore membre du groupe à ce moment) | `psql -p 55432` : nombre de bases et de lignes de `schema_migrations` identique avant/après |
| 7 | `~/.config/environment.d/50-docker.conf` : `DOCKER_HOST=unix:///run/user/1001/docker.sock` (chemin littéral : uid d'ameesh sur l'hôte, vérifié par `id -u`). Couvre toutes les unités utilisateur, dont `ameesh-runner-agent@`, `ameesh-serve` et les minuteries ; aussi dans `~/.config/ameesh/env` | agent | `systemctl --user daemon-reload`, puis `systemctl --user show-environment` |
| 8 | `gpasswd -d ameesh docker` puis `systemctl restart user@1001.service` | bureau (root) | voir preuve finale |
| 9 | Relancer les runners, lever le drainage ; un tour réel de bout en bout (`scripts/test.sh` SQL ciblé, `ameesh menage`, `ameesh doctor`) | agent / pilote | tour réglé sans « container engine not answering » |
| 10 | Ancien `ameesh-pg` rootful et conteneurs de test : arrêtés ; images rootful purgées sauf `postgres:17` ; **volume rootful et `postgres:17` gardés 7 jours** pour le retour arrière, puis supprimés | bureau (root) | `docker ps -a`, `docker system df` (root) |

**Preuve finale (par hôte)**, relevée par le bureau et collée sur le lot :
- `id ameesh` sans `docker` ; aucun groupe parmi `sudo wheel admin docker adm lxd libvirt systemd-journal` ;
- `sudo -l -U ameesh` vide (« not allowed ») ;
- aucun processus d'uid 1001 avec 112 dans `Groups:` de `/proc/<pid>/status` ;
- sous ameesh : `test -w /var/run/docker.sock` faux ;
  `DOCKER_HOST=$RL docker run -v /etc/shadow:/x:ro … cat /x` → `Permission denied` ;
- `docker info` (via `DOCKER_HOST`) : `rootless`.

**Retour arrière** (par hôte, 10 min) : `usermod -aG docker ameesh` puis
redémarrage de `user@1001` (bureau) ; retrait de `50-docker.conf` et de la
ligne `DOCKER_HOST` ; `systemctl --user disable --now docker` (rootless) ; `DOCKER_HOST=$RF docker start ameesh-pg` (rootful, volume gardé ; sous ameesh, un `DOCKER_HOST=$RL` encore exporté viserait le démon rootless arrêté).
Les écritures de la base de test faites depuis la bascule sont perdues, sauf
copie inverse par `tar` comme à l'étape 6. Cette base sert aux tests et n'a pas de
valeur propre. La pose L2 reste bloquée tant que le retour arrière est actif.

# 5. Suites hors de ce lot (à créer après relecture du plan)

- ameesh, léger : `netcheck.py` qui reconnaît un démon rootless (pas de faux
  `host_network_overlap`, remède `~/.config/docker/daemon.json`) ;
  `containers.py` qui documente que `docker context use` est proscrit.
- ameesh, sensible : `deploy/serveur/installer-serveur.sh:34` ne doit plus
  ajouter ameesh au groupe `docker` (installer Docker rootless à la place).
- Hors candidats : AG3 et manaty-mesh-1 ont la même exposition (ameesh dans
  `docker`). À traiter avec ce plan une fois AG1/AG2 faits, sur décision du
  pilote.
- AG3 : socket `systemd/private` du gestionnaire utilisateur périmé depuis le
  01/11 02:05 (`connection refused`) : lot #5505, suivi séparé, sans lien
  d'ordre avec ce plan.
