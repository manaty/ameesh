#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Docker rootless pour l'utilisateur des exécuteurs (#5784, étude
# docs/design/etudes/l5502-docker-rootless.md). Appelé par installer-serveur.sh,
# rejouable seul, en root (idempotent) :
#
#   /opt/ameesh/src/deploy/serveur/docker-rootless.sh [utilisateur]
#
# Converge vers : dockerd sous rootlesskit dans l'unité systemd UTILISATEUR
# docker.service, socket /run/user/<uid>/docker.sock, DOCKER_HOST posé pour les
# unités utilisateur (environment.d, ~/.config/ameesh/env s'il existe) et pour
# le gabarit système ameesh-runner@.
#
# L'utilisateur n'est jamais ajouté au groupe `docker` : le démon rootful est un
# équivalent root. S'il en est encore membre, le script s'arrête avant tout
# geste : la bascule (étude L5502 §4 : drainage, migration du volume, retrait du
# groupe) se fait d'abord. Un prérequis manquant est une erreur explicite,
# jamais un repli sur le démon rootful.
#
# Pas de dockerd-rootless-setuptool.sh : il exécute `docker context use
# rootless` (étude §2.1), et l'unité qu'il écrirait est posée ici.
set -euo pipefail

U=${1:-ameesh}
# Préfixe des chemins système, pour les essais seulement (tests/test_5784_*).
RACINE=${RACINE:-}
PAQUETS=(rootlesskit uidmap slirp4netns fuse-overlayfs dbus-user-session)
# dockerd-rootless.sh n'est fourni ni par docker.io (Debian, Ubuntu) ni par
# rootlesskit : posé depuis moby, commit épinglé (tag docker-v29.1.3), empreinte
# vérifiée AVANT installation ; une empreinte différente arrête le script.
MOBY_COMMIT=fbf3ed25f893e6ce21336f1101590e40a13934f4
WRAPPER_SHA256=8eb00d61b5e78ffc0dcd628f056389b5859cc61d6fe3ce17b4a3a4b08143c8a5
WRAPPER_URL=https://raw.githubusercontent.com/moby/moby/$MOBY_COMMIT/contrib/dockerd-rootless.sh
WRAPPER=$RACINE/usr/local/bin/dockerd-rootless.sh
SUB_COUNT=65536

log() { echo "== $*"; }
die() { echo "docker rootless ($U) : $*" >&2; exit 1; }

UID_U=$(id -u "$U") || die "utilisateur absent"
HOME_U=$(getent passwd "$U" | cut -d: -f6)
[[ -n $HOME_U ]] || die "répertoire personnel introuvable"
SOCK=unix:///run/user/$UID_U/docker.sock

# --- jamais le groupe docker -------------------------------------------------
if id -nG "$U" | tr ' ' '\n' | grep -qx docker; then
  die "membre du groupe docker (démon rootful = root). Basculer d'abord \
(étude L5502 §4), puis : gpasswd -d $U docker && systemctl restart user@$UID_U.service"
fi

# --- paquets -------------------------------------------------------------------
command -v dockerd >/dev/null || die "dockerd absent (paquet docker.io)"
MANQUANTS=()
for p in "${PAQUETS[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || MANQUANTS+=("$p")
done
if (( ${#MANQUANTS[@]} )); then
  log "paquets rootless : ${MANQUANTS[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MANQUANTS[@]}" >/dev/null \
    || die "installation impossible : ${MANQUANTS[*]}"
fi
for b in newuidmap newgidmap; do
  [[ -u $RACINE/usr/bin/$b ]] || die "/usr/bin/$b absent ou non setuid (paquet uidmap)"
done

# --- plages subordonnées ---------------------------------------------------------
# useradd les pose d'ordinaire ; sinon, la première plage libre après les autres.
for f in subuid subgid; do
  F=$RACINE/etc/$f
  touch "$F"
  n=$(awk -F: -v u="$U" '$1 == u { s += $3 } END { print s + 0 }' "$F")
  if (( n == 0 )); then
    debut=$(awk -F: 'BEGIN { m = 100000 } $3 > 0 && $2 + $3 > m { m = $2 + $3 } END { print m }' "$F")
    echo "$U:$debut:$SUB_COUNT" >> "$F"
    log "/etc/$f : $U:$debut:$SUB_COUNT"
  elif (( n < SUB_COUNT )); then
    die "/etc/$f : $n identifiants subordonnés, $SUB_COUNT requis"
  fi
done

# --- AppArmor (Ubuntu 24.04 : espaces de noms utilisateur restreints) -------------
RESTRICT=$RACINE/proc/sys/kernel/apparmor_restrict_unprivileged_userns
if [[ -r $RESTRICT && $(cat "$RESTRICT") == 1 ]]; then
  PROFILS=$RACINE/sys/kernel/security/apparmor/profiles
  if ! grep -q '^rootlesskit ' "$PROFILS" 2>/dev/null; then
    [[ -f $RACINE/etc/apparmor.d/rootlesskit ]] \
      || die "espaces de noms restreints et aucun profil AppArmor rootlesskit"
    apparmor_parser -r "$RACINE/etc/apparmor.d/rootlesskit" \
      || die "profil AppArmor rootlesskit non chargé"
  fi
fi

# --- dockerd-rootless.sh épinglé ---------------------------------------------------
if [[ $(sha256sum "$WRAPPER" 2>/dev/null | cut -d' ' -f1) != "$WRAPPER_SHA256" ]]; then
  log "dockerd-rootless.sh (moby $MOBY_COMMIT)"
  T=$(mktemp)
  trap 'rm -f "$T"' EXIT
  curl -fsSL --max-time 60 -o "$T" "$WRAPPER_URL" || die "téléchargement impossible : $WRAPPER_URL"
  [[ $(sha256sum "$T" | cut -d' ' -f1) == "$WRAPPER_SHA256" ]] \
    || die "empreinte inattendue pour $WRAPPER_URL (attendu $WRAPPER_SHA256)"
  install -D -m 0755 "$T" "$WRAPPER"
fi

# --- unité utilisateur et DOCKER_HOST ------------------------------------------------
loginctl enable-linger "$U"
# Le home appartient à l'utilisateur : toute écriture y est faite avec ses seuls
# droits (revue cx1 #5784). En root, un lien qu'il aurait posé (par exemple
# ~/.config/systemd/user vers /etc/systemd/system) serait suivi.
en_u() { runuser -u "$U" -- "$@"; }
pose() {  # pose <chemin> : stdin vers un fichier de l'utilisateur, 0644
  en_u mkdir -p "$(dirname "$1")"
  en_u sh -c 'cat > "$1" && chmod 0644 "$1"' sh "$1"
}
pose "$HOME_U/.config/systemd/user/docker.service" <<'UNIT'
# Posée par deploy/serveur/docker-rootless.sh (#5784).
[Unit]
Description=Docker Application Container Engine (Rootless)
Documentation=https://docs.docker.com/go/rootless/

[Service]
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
ExecStart=/usr/local/bin/dockerd-rootless.sh
ExecReload=/bin/kill -s HUP $MAINPID
TimeoutSec=0
RestartSec=2
Restart=always
StartLimitBurst=3
StartLimitInterval=60s
LimitNOFILE=infinity
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
Delegate=yes
Type=notify
NotifyAccess=all
KillMode=mixed

[Install]
WantedBy=default.target
UNIT
pose "$HOME_U/.config/environment.d/50-docker.conf" <<CONF
DOCKER_HOST=$SOCK
CONF
ENV_AMEESH=$HOME_U/.config/ameesh/env
en_u sh -c '[ ! -f "$1" ] || grep -qx "DOCKER_HOST=$2" "$1" || {
  sed -i "/^DOCKER_HOST=/d" "$1" && echo "DOCKER_HOST=$2" >> "$1"; }' sh "$ENV_AMEESH" "$SOCK"
# Les exécuteurs du serveur sont des unités SYSTÈME (User=) : environment.d ne
# les atteint pas.
install -d "$RACINE/etc/systemd/system/ameesh-runner@.service.d"
cat > "$RACINE/etc/systemd/system/ameesh-runner@.service.d/docker-rootless.conf" <<CONF
# Posé par deploy/serveur/docker-rootless.sh (#5784).
[Unit]
Wants=user@$UID_U.service
After=user@$UID_U.service

[Service]
Environment=DOCKER_HOST=$SOCK
CONF
systemctl daemon-reload
systemctl start "user@$UID_U.service"
systemctl --user -M "$U@" daemon-reload
systemctl --user -M "$U@" enable --now docker.service

# --- vérification -----------------------------------------------------------------------
for _ in $(seq 30); do
  [[ -S $RACINE/run/user/$UID_U/docker.sock ]] && break
  sleep 1
done
INFO=$(runuser -u "$U" -- env DOCKER_HOST="$SOCK" docker info \
  --format '{{.SecurityOptions}} {{.Driver}}') || die "le démon rootless ne répond pas sur $SOCK"
[[ $INFO == *rootless* ]] || die "$SOCK ne sert pas un démon rootless ($INFO)"
[[ $INFO != *" vfs" ]] || die "pilote de stockage vfs (fuse-overlayfs attendu au moins)"
log "Docker rootless pour $U : $SOCK ($INFO)"
