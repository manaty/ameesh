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
#   - Docker rootless pour `ameesh` (docker-rootless.sh, #5784), sans le groupe docker ;
#   - le gabarit systemd ameesh-runner@<persona> (un exécuteur par persona) ;
#   - la sauvegarde quotidienne de la base (locale, et hors de la VM si
#     /etc/ameesh/sauvegarde.env existe) ;
#   - le relevé de l'hôte toutes les 5 minutes (#5559 : disque de la base).
# Le résumé (clé publique WireGuard, suite) est écrit dans /root/ameesh-serveur.txt.
set -euo pipefail

ORGANISATION=${ORGANISATION:?ORGANISATION requis (identifiant court de l\'organisation)}
WIREGUARD_PORT=${WIREGUARD_PORT:-51820}
SRC=${SRC:-/opt/ameesh/src}
VENV=/opt/ameesh/venv
HOST_NAME=${HOST_NAME:-$ORGANISATION-mesh-1}
WG_NET=10.77.0
PG_VERSION=${PG_VERSION:-17}
PG_CONF=/etc/postgresql/$PG_VERSION/main
VERSION=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo "?")

log() { echo "== $*"; }
# #5784 (revue DeepSeek) : le home d'ameesh n'est écrit qu'avec ses droits ; en
# root, un lien qu'il y aurait posé (~/.config/ameesh vers /etc/systemd/system)
# serait suivi à la reprise.
as_ameesh() { runuser -u ameesh -- "$@"; }

# --- utilisateur des exécuteurs ----------------------------------------------
log "utilisateur ameesh"
id ameesh >/dev/null 2>&1 || useradd --create-home --shell /bin/bash ameesh
install -d -m 0750 -o root -g ameesh /etc/ameesh
# #5784 : Docker rootless, jamais le groupe docker (démon rootful = root) ; un
# compte encore membre du groupe arrête l'installation avant tout autre geste.
log "Docker rootless pour ameesh"
"$SRC/deploy/serveur/docker-rootless.sh" ameesh

# --- WireGuard -----------------------------------------------------------------
log "WireGuard (wg0, $WG_NET.1/24, port $WIREGUARD_PORT)"
install -d -m 0700 /etc/wireguard
if [[ ! -f /etc/wireguard/wg0.conf ]]; then
  (  # umask restreint aux seules clés : il ne doit pas fuir sur la suite
    umask 077
    wg genkey > /etc/wireguard/serveur.key
    wg pubkey < /etc/wireguard/serveur.key > /etc/wireguard/serveur.pub
    cat > /etc/wireguard/wg0.conf <<CONF
[Interface]
Address = $WG_NET.1/24
ListenPort = $WIREGUARD_PORT
PrivateKey = $(cat /etc/wireguard/serveur.key)
CONF
  )
fi
systemctl enable --now wg-quick@wg0

# --- Postgres ------------------------------------------------------------------
# Version de la communauté PostgreSQL (dépôt PGDG), la même que celle des
# autres meshes : une sauvegarde se restaure d'un hôte à l'autre.
if ! dpkg -s "postgresql-$PG_VERSION" >/dev/null 2>&1; then
  log "Postgres $PG_VERSION depuis apt.postgresql.org"
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSo /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release; echo "$VERSION_CODENAME")-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "postgresql-$PG_VERSION" "postgresql-contrib-$PG_VERSION" >/dev/null 2>&1 \
    || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "postgresql-$PG_VERSION"
fi
# Un cluster d'une autre version (paquet `postgresql` de la distribution, au
# premier démarrage) est retiré : la base du mesh n'y a encore rien écrit.
for v in $(ls /etc/postgresql); do
  [[ $v == "$PG_VERSION" ]] && continue
  pg_dropcluster --stop "$v" main || true
done
[[ -d $PG_CONF ]] || pg_createcluster "$PG_VERSION" main --port 5432
log "Postgres $PG_VERSION en TLS, sur localhost et wg0"
install -d /etc/systemd/system/postgresql@.service.d
cat > /etc/systemd/system/postgresql@.service.d/wireguard.conf <<'CONF'
[Unit]
# Postgres écoute sur l'adresse de wg0 : l'interface doit exister avant lui.
After=wg-quick@wg0.service
Wants=wg-quick@wg0.service
CONF
install -m 0644 -o postgres -g postgres /dev/stdin "$PG_CONF/conf.d/ameesh.conf" <<CONF
listen_addresses = 'localhost,$WG_NET.1'
port = 5432
ssl = on
password_encryption = scram-sha-256
CONF
HBA="$PG_CONF/pg_hba.conf"
grep -q "ameesh-mesh" "$HBA" || cat >> "$HBA" <<CONF
# ameesh-mesh : appareils du réseau WireGuard, TLS obligatoire
hostssl ameesh  all  $WG_NET.0/24  scram-sha-256
CONF
systemctl daemon-reload
systemctl restart "postgresql@$PG_VERSION-main"
for _ in $(seq 30); do pg_isready -q -h localhost && break; sleep 1; done
pg_isready -h localhost
if [[ ! -f /etc/ameesh/db.env ]]; then
  PW=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  printf 'PGPASSWORD=%s\n' "$PW" | install -m 0640 -o root -g ameesh /dev/stdin /etc/ameesh/db.env
  printf '127.0.0.1:5432:ameesh:ameesh:%s\n' "$PW" \
    | as_ameesh sh -c 'umask 077 && cat > "$1" && chmod 0600 "$1"' sh /home/ameesh/.pgpass
fi
# rôle et base assurés à chaque passage (rejouable, et après un changement de version)
PW=$(sed -n 's/^PGPASSWORD=//p' /etc/ameesh/db.env)
cd /tmp
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ameesh') THEN
    CREATE ROLE ameesh LOGIN;
  END IF;
END \$\$;
ALTER ROLE ameesh PASSWORD '$PW';
-- #3503 : le JIT de Postgres compilait des plans à coût estimé élevé sur de
-- très petites tables (_unreachable_lots : 42 s contre 0,2 s). Le client le
-- coupe déjà dans chaque session ; ce réglage couvre les sessions qui ne
-- passent pas par ameesh (psql, script de déploiement, nouveau rôle).
ALTER ROLE ameesh SET jit = off;
-- #3836 : une session laissée `idle in transaction` tenait les verrous du
-- mesh (sept minutes lors du gel #3700). La garde serveur la coupe à 3 min
-- (180 s) ; le code du gel ne va plus au réseau dans sa transaction, et cette
-- valeur couvre le travail local légitime qui y reste.
ALTER ROLE ameesh SET idle_in_transaction_session_timeout = '180s';
SQL
runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'ameesh'" | grep -q 1 \
  || runuser -u postgres -- createdb -O ameesh ameesh
# même réglage pour la base : un NOUVEAU rôle qui s'y connecte en hérite.
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q -d ameesh \
  -c "ALTER DATABASE ameesh SET jit = off" \
  -c "ALTER DATABASE ameesh SET idle_in_transaction_session_timeout = '180s'"
unset PW

# --- ameesh --------------------------------------------------------------------
log "ameesh ($(echo "$VERSION")) dans $VENV"
[[ -d $VENV ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --force-reinstall --no-deps "$SRC"
# #3619 : crypto = cryptography>=41, le canal d'alertes nexlink ; posé ici
# comme sur les autres hôtes, contrôlé avant de servir le code.
"$VENV/bin/pip" install -q "$SRC[psycopg,crypto]"
"$VENV/bin/python" -c 'import psycopg, cryptography, ameesh'
for b in ameesh agent-runner agent-mail; do ln -sfn "$VENV/bin/$b" "/usr/local/bin/$b"; done
CFG=/home/ameesh/.config/ameesh/config.json
if [[ ! -f $CFG ]]; then
  as_ameesh mkdir -p /home/ameesh/.config/ameesh
  as_ameesh sh -c 'cat > "$1"' sh "$CFG" <<CONF
{
  "dsn": "postgresql://ameesh@127.0.0.1:5432/ameesh",
  "host": "$HOST_NAME",
  "canons": []
}
CONF
fi
runuser -l ameesh -c "ameesh migrate"

# --- exécuteurs ----------------------------------------------------------------
log "gabarit systemd ameesh-runner@<persona>"
install -m 0644 "$SRC/deploy/serveur/ameesh-runner@.service" /etc/systemd/system/
systemctl daemon-reload

# --- sauvegardes ---------------------------------------------------------------
log "sauvegarde quotidienne de la base"
install -m 0755 "$SRC/deploy/serveur/sauvegarder-base.sh" /usr/local/sbin/ameesh-sauvegarder-base
install -m 0755 "$SRC/deploy/mise-a-jour/sauvegarder-prive.py" /usr/local/sbin/ameesh-sauvegarder-prive.py
install -m 0755 "$SRC/deploy/mise-a-jour/retention-privee.py" /usr/local/sbin/ameesh-retention-privee.py
# The version-retention provider is loaded by the helper under this fixed
# sibling name; absent, a set with a remote member stays protected.
install -m 0755 "$SRC/deploy/mise-a-jour/retention-scaleway.py" /usr/local/sbin/retention-scaleway.py
install -m 0644 "$SRC/deploy/serveur/ameesh-sauvegarde.service" "$SRC/deploy/serveur/ameesh-sauvegarde.timer" \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ameesh-sauvegarde.timer

# --- relevé de l'hôte (#5559) ----------------------------------------------------
log "relevé de l'hôte toutes les 5 minutes (disque de la base)"
install -m 0644 "$SRC/deploy/serveur/ameesh-releve-hote.service" "$SRC/deploy/serveur/ameesh-releve-hote.timer" \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ameesh-releve-hote.timer

# --- résumé --------------------------------------------------------------------
IP=$(curl -fsS --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
cat > /root/ameesh-serveur.txt <<TXT
Mesh ameesh de l'organisation « $ORGANISATION » — hôte $HOST_NAME
  ameesh      $(echo "$VERSION")
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
