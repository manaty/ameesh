#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Installe le serveur d'un mesh ameesh sur une VM Debian 12 (lot L55, 0033 §2).
# Lancé par cloud-init au premier démarrage, rejouable à la main (idempotent) :
#
#   ORGANISATION=manaty /opt/ameesh/src/deploy/serveur/installer-serveur.sh
#
# Ce qu'il met en place :
#   - réseau privé WireGuard 10.77.0.0/24 (serveur 10.77.0.1) pour les appareils ;
#   - Postgres en TLS, n'écoutant que sur localhost et le réseau WireGuard ;
#   - l'utilisateur Unix `ameesh`, ameesh dans /opt/ameesh/venv, sa configuration ;
#   - le gabarit systemd ameesh-runner@<persona> (un exécuteur par persona) ;
#   - la sauvegarde quotidienne de la base (locale, et hors de la VM si
#     /etc/ameesh/sauvegarde.env existe).
# Le résumé (clé publique WireGuard, suite) est écrit dans /root/ameesh-serveur.txt.
set -euo pipefail

ORGANISATION=${ORGANISATION:?ORGANISATION requis (identifiant court de l\'organisation)}
WIREGUARD_PORT=${WIREGUARD_PORT:-51820}
SRC=${SRC:-/opt/ameesh/src}
VENV=/opt/ameesh/venv
HOST_NAME=${HOST_NAME:-$ORGANISATION-mesh-1}
WG_NET=10.77.0
PG_VERSION=$(ls /etc/postgresql | sort -n | tail -1)
PG_CONF=/etc/postgresql/$PG_VERSION/main

log() { echo "== $*"; }

# --- utilisateur des exécuteurs ----------------------------------------------
log "utilisateur ameesh"
id ameesh >/dev/null 2>&1 || useradd --create-home --shell /bin/bash ameesh
usermod -aG docker ameesh
install -d -m 0750 -o root -g ameesh /etc/ameesh

# --- WireGuard -----------------------------------------------------------------
log "WireGuard (wg0, $WG_NET.1/24, port $WIREGUARD_PORT)"
install -d -m 0700 /etc/wireguard
if [[ ! -f /etc/wireguard/wg0.conf ]]; then
  umask 077
  wg genkey > /etc/wireguard/serveur.key
  wg pubkey < /etc/wireguard/serveur.key > /etc/wireguard/serveur.pub
  cat > /etc/wireguard/wg0.conf <<CONF
[Interface]
Address = $WG_NET.1/24
ListenPort = $WIREGUARD_PORT
PrivateKey = $(cat /etc/wireguard/serveur.key)
CONF
fi
systemctl enable --now wg-quick@wg0

# --- Postgres ------------------------------------------------------------------
log "Postgres $PG_VERSION en TLS, sur localhost et wg0"
install -d /etc/systemd/system/postgresql@.service.d
cat > /etc/systemd/system/postgresql@.service.d/wireguard.conf <<'CONF'
[Unit]
# Postgres écoute sur l'adresse de wg0 : l'interface doit exister avant lui.
After=wg-quick@wg0.service
Wants=wg-quick@wg0.service
CONF
cat > "$PG_CONF/conf.d/ameesh.conf" <<CONF
listen_addresses = 'localhost,$WG_NET.1'
ssl = on
password_encryption = scram-sha-256
CONF
HBA="$PG_CONF/pg_hba.conf"
grep -q "ameesh-mesh" "$HBA" || cat >> "$HBA" <<CONF
# ameesh-mesh : appareils du réseau WireGuard, TLS obligatoire
hostssl ameesh  all  $WG_NET.0/24  scram-sha-256
CONF
systemctl daemon-reload
systemctl restart postgresql
if [[ ! -f /etc/ameesh/db.env ]]; then
  PW=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ameesh') THEN
    CREATE ROLE ameesh LOGIN;
  END IF;
END \$\$;
ALTER ROLE ameesh PASSWORD '$PW';
SQL
  runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'ameesh'" | grep -q 1 \
    || runuser -u postgres -- createdb -O ameesh ameesh
  umask 077
  printf 'PGPASSWORD=%s\n' "$PW" > /etc/ameesh/db.env
  chgrp ameesh /etc/ameesh/db.env && chmod 0640 /etc/ameesh/db.env
  printf '127.0.0.1:5432:ameesh:ameesh:%s\n' "$PW" > /home/ameesh/.pgpass
  chown ameesh:ameesh /home/ameesh/.pgpass && chmod 0600 /home/ameesh/.pgpass
  unset PW
fi

# --- ameesh --------------------------------------------------------------------
log "ameesh ($(git -C "$SRC" rev-parse --short HEAD)) dans $VENV"
[[ -d $VENV ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --force-reinstall --no-deps "$SRC"
"$VENV/bin/pip" install -q "$SRC"
for b in ameesh agent-runner agent-mail; do ln -sfn "$VENV/bin/$b" "/usr/local/bin/$b"; done
CFG=/home/ameesh/.config/ameesh/config.json
if [[ ! -f $CFG ]]; then
  install -d -o ameesh -g ameesh /home/ameesh/.config/ameesh
  cat > "$CFG" <<CONF
{
  "dsn": "postgresql://ameesh@127.0.0.1:5432/ameesh",
  "host": "$HOST_NAME",
  "canons": []
}
CONF
  chown ameesh:ameesh "$CFG"
fi
runuser -l ameesh -c "ameesh migrate"

# --- exécuteurs ----------------------------------------------------------------
log "gabarit systemd ameesh-runner@<persona>"
install -m 0644 "$SRC/deploy/serveur/ameesh-runner@.service" /etc/systemd/system/
systemctl daemon-reload

# --- sauvegardes ---------------------------------------------------------------
log "sauvegarde quotidienne de la base"
install -m 0755 "$SRC/deploy/serveur/sauvegarder-base.sh" /usr/local/sbin/ameesh-sauvegarder-base
install -m 0644 "$SRC/deploy/serveur/ameesh-sauvegarde.service" "$SRC/deploy/serveur/ameesh-sauvegarde.timer" \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ameesh-sauvegarde.timer

# --- résumé --------------------------------------------------------------------
IP=$(curl -fsS --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
cat > /root/ameesh-serveur.txt <<TXT
Mesh ameesh de l'organisation « $ORGANISATION » — hôte $HOST_NAME
  ameesh      $(git -C "$SRC" rev-parse --short HEAD)
  WireGuard   $IP:$WIREGUARD_PORT, clé publique $(cat /etc/wireguard/serveur.pub)
  Postgres    $WG_NET.1:5432, base ameesh, rôle ameesh, TLS obligatoire depuis wg0
              (mot de passe : /etc/ameesh/db.env, jamais affiché)

Suite :
  1. déclarer l'hôte $HOST_NAME dans le canon de l'organisation (hotes/$HOST_NAME.md),
     puis renseigner "canons" dans $CFG et lancer : runuser -l ameesh -c "ameesh canon sync --fetch" ;
  2. ajouter chaque appareil : /opt/ameesh/src/deploy/serveur/ajouter-appareil.sh <nom> ;
  3. lancer une persona : systemctl enable --now ameesh-runner@<persona>.
TXT
log "terminé — voir /root/ameesh-serveur.txt"
