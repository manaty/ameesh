#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Archivage WAL hors hôte et restauration à un instant (EP8.1, lot #6858,
# étude docs/design/etudes/replique-base-mesh.md §6) : pgBackRest vers un
# stockage d'objets S3. Le dump quotidien (ameesh-sauvegarde.timer) reste en
# place, format indépendant et chemin de restauration lisible.
#
#   configurer           écrit /etc/pgbackrest/pgbackrest.conf depuis
#                        /etc/ameesh/pgbackrest.env ; ne touche pas Postgres
#   activer [--redemarrer]
#                        crée la stanza, pose archive_mode/wal_log_hints ;
#                        avec --redemarrer (fenêtre planifiée par le pilote
#                        ou le bureau, owner informé) : redémarrage court,
#                        `pgbackrest check`, première sauvegarde complète,
#                        minuteries ; sans : rien n'est redémarré
#   desactiver [--redemarrer]
#                        retour arrière : retire le réglage et les minuteries
#   sauvegarder full|diff
#                        une sauvegarde (minuteries ameesh-pgbackrest-*)
#   etat                 `pgbackrest info`
#   restaurer --cible 'AAAA-MM-JJ HH:MM:SS+00' --dossier D [--port P] [--garder]
#                        exercice : restaure à l'instant choisi dans un
#                        dossier vide, démarre une instance jetable (socket
#                        dans D, aucune écoute réseau, archive-mode=off pour
#                        ne jamais pousser dans le dépôt), passe
#                        `ameesh doctor --probe`, mesure le RTO, s'arrête
#   exercice-dump --fichier F --dossier D [--port P] [--garder]
#                        même exercice sur un dump `pg_dump -Fc` (restaurer
#                        le dump S3 actuel avant tout changement)
#
# /etc/ameesh/pgbackrest.env (root, 0600 ; jamais dans le dépôt) :
#   PGBR_S3_BUCKET, PGBR_S3_REGION, PGBR_S3_KEY, PGBR_S3_KEY_SECRET,
#   PGBR_CIPHER_PASS (chiffrement du dépôt : une copie DOIT vivre hors de
#   l'hôte, chez le bureau, sinon une perte de l'hôte rend le dépôt illisible)
#   facultatifs : PGBR_S3_ENDPOINT (défaut s3.<région>.scw.cloud),
#   PGBR_REPO_PATH (défaut /pgbackrest/<hôte>)
# La stanza est toujours `ameesh` : les minuteries (sous postgres) et les
# autres commandes ne lisent pas ce fichier, une stanza propre à l'hôte n'y
# serait vue que par `configurer` ; PGBR_STANZA est donc refusé.
#
# Les exercices se lancent en root (ou sous postgres) : initdb, pg_ctl,
# pgbackrest restore, psql et pg_restore passent sous le compte de Postgres,
# qui refuse root ; le dossier lui appartient, le dump lui est lu sur l'entrée.
#
# La rétention (14 jours de restauration à un instant) est tenue par
# pgBackRest (repo1-retention-full-type=time) : une règle de cycle de vie du
# bucket à 14 jours effacerait une sauvegarde complète encore nécessaire.
# Les chemins système se redirigent par AMEESH_PGBR_* pour les tests.
set -euo pipefail
umask 077

PG_VERSION=${AMEESH_PG_VERSION:-17}
PG_BIN=${AMEESH_PG_BIN:-/usr/lib/postgresql/$PG_VERSION/bin}
PG_CONF=${AMEESH_PGBR_PG_CONF:-/etc/postgresql/$PG_VERSION/main}
PG_DATA=${AMEESH_PGBR_PG_DATA:-/var/lib/postgresql/$PG_VERSION/main}
PG_UNIT=${AMEESH_PGBR_PG_UNIT:-postgresql@$PG_VERSION-main}
PG_USER=${AMEESH_PGBR_PG_USER:-postgres}
PG_GROUP=$(id -gn "$PG_USER" 2>/dev/null || echo "$PG_USER")
ENV_FILE=${AMEESH_PGBR_ENV:-/etc/ameesh/pgbackrest.env}
ETC=${AMEESH_PGBR_ETC:-/etc/pgbackrest}
SPOOL=${AMEESH_PGBR_SPOOL:-/var/spool/pgbackrest}
LOGS=${AMEESH_PGBR_LOGS:-/var/log/pgbackrest}
ARCHIVAGE="$PG_CONF/conf.d/ameesh-archivage.conf"
TIMERS="ameesh-pgbackrest-full.timer ameesh-pgbackrest-diff.timer"

log() { printf 'pgbackrest-ameesh : %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }

# Commande lancée sous le compte de Postgres ; sans changement si on l'est déjà.
as_pg() {
  if [[ $(id -un) == "$PG_USER" ]]; then "$@"; else runuser -u "$PG_USER" -- "$@"; fi
}

stanza() { echo ameesh; }

# Fichier ou dossier rendu à Postgres ; hors root (tests), rien à céder.
ceder() { if [[ $EUID == 0 ]]; then chown "$PG_USER:$PG_GROUP" "$@"; fi; }

charger_env() {
  [[ -r $ENV_FILE ]] || die "$ENV_FILE absent : le bureau confirme le bucket et ses accès d'abord"
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  local v
  for v in PGBR_S3_BUCKET PGBR_S3_REGION PGBR_S3_KEY PGBR_S3_KEY_SECRET PGBR_CIPHER_PASS; do
    [[ -n ${!v:-} ]] || die "$v manquant dans $ENV_FILE"
  done
  [[ -z ${PGBR_STANZA:-} ]] || die "PGBR_STANZA refusé : la stanza est toujours ameesh"
  for v in PGBR_S3_BUCKET PGBR_S3_REGION PGBR_S3_ENDPOINT PGBR_S3_KEY PGBR_S3_KEY_SECRET \
           PGBR_CIPHER_PASS PGBR_REPO_PATH; do
    # une valeur du fichier ini va jusqu'à la fin de la ligne
    [[ ${!v:-} != *$'\n'* && ${!v:-} != *$'\r'* ]] || die "$v : retour à la ligne refusé"
  done
  [[ $PGBR_S3_BUCKET =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || die "PGBR_S3_BUCKET invalide"
  [[ $PGBR_S3_REGION =~ ^[a-z]{2}-[a-z]{3}$ ]] || die "PGBR_S3_REGION invalide (ex. nl-ams)"
  if [[ $PGBR_S3_REGION == fr-par ]]; then
    log "bucket en fr-par, la région des VM : pas de copie hors région (décision 6)"
  fi
}

configurer() {
  charger_env
  local endpoint=${PGBR_S3_ENDPOINT:-s3.$PGBR_S3_REGION.scw.cloud}
  local path=${PGBR_REPO_PATH:-/pgbackrest/$(hostname)}
  # pgBackRest lit sa configuration sous postgres : son groupe doit traverser
  # le dossier (relecture ameesh-dev3, 292eb8991d42)
  install -d -m 0750 "$ETC"
  chgrp "$PG_GROUP" "$ETC" 2>/dev/null || true
  install -d -m 0750 -o "$PG_USER" -g "$PG_USER" "$SPOOL" "$LOGS" 2>/dev/null \
    || install -d -m 0750 "$SPOOL" "$LOGS"
  local tmp
  tmp=$(mktemp "$ETC/.pgbackrest.conf.XXXXXX")
  cat > "$tmp" <<CONF
# Écrit par pgbackrest-ameesh configurer (lot #6858) ; secrets de $ENV_FILE.
[global]
repo1-type=s3
repo1-s3-bucket=$PGBR_S3_BUCKET
repo1-s3-endpoint=$endpoint
repo1-s3-region=$PGBR_S3_REGION
repo1-s3-key=$PGBR_S3_KEY
repo1-s3-key-secret=$PGBR_S3_KEY_SECRET
repo1-path=$path
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=$PGBR_CIPHER_PASS
repo1-retention-full-type=time
repo1-retention-full=14
compress-type=zst
process-max=2
start-fast=y
archive-async=y
spool-path=$SPOOL
# S3 injoignable : au-delà de 4 Gio en attente, les WAL sont abandonnés (la
# restauration à un instant a un trou) plutôt que de remplir pg_wal.
archive-push-queue-max=4GiB
log-path=$LOGS
log-level-console=info
log-level-file=detail

[$(stanza)]
pg1-path=$PG_DATA
pg1-port=5432
pg1-socket-path=/var/run/postgresql
CONF
  chmod 0640 "$tmp"
  chgrp "$PG_GROUP" "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$ETC/pgbackrest.conf"
  log "$ETC/pgbackrest.conf écrit (dépôt s3://$PGBR_S3_BUCKET$path, $endpoint)"
}

attendre_postgres() {
  local i
  for i in $(seq 120); do
    as_pg "$PG_BIN/pg_isready" -q -h /var/run/postgresql && return 0
    sleep 0.5
  done
  return 1
}

redemarrer_postgres() {
  local t0 t1
  t0=$(date +%s)
  systemctl restart "$PG_UNIT"
  attendre_postgres || die "Postgres ne répond pas 60 s après le redémarrage"
  t1=$(date +%s)
  log "redémarrage de $PG_UNIT : $((t1 - t0)) s"
}

activer() {
  local redemarrer=0
  [[ ${1:-} == --redemarrer ]] && redemarrer=1
  [[ -r $ETC/pgbackrest.conf ]] || die "$ETC/pgbackrest.conf absent : lancer configurer"
  as_pg pgbackrest --stanza="$(stanza)" stanza-create
  install -d -m 0755 "$PG_CONF/conf.d"
  local tmp
  tmp=$(mktemp "$PG_CONF/conf.d/.ameesh-archivage.XXXXXX")
  cat > "$tmp" <<CONF
# EP8.1 (lot #6858) : archivage WAL continu vers pgBackRest ; retour arrière :
# pgbackrest-ameesh desactiver --redemarrer
archive_mode = on
archive_command = 'pgbackrest --stanza=$(stanza) archive-push %p'
archive_timeout = 60
wal_log_hints = on
CONF
  chmod 0644 "$tmp"
  chown "$PG_USER:$PG_USER" "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$ARCHIVAGE"
  if [[ $redemarrer == 0 ]]; then
    log "$ARCHIVAGE posé ; archive_mode et wal_log_hints attendent un redémarrage (activer --redemarrer, dans la fenêtre)"
    return 0
  fi
  redemarrer_postgres
  as_pg pgbackrest --stanza="$(stanza)" check
  as_pg pgbackrest --stanza="$(stanza)" --type=full backup
  systemctl daemon-reload
  # shellcheck disable=SC2086
  systemctl enable --now $TIMERS
  log "archivage actif, première sauvegarde complète faite, minuteries en marche"
}

desactiver() {
  local redemarrer=0
  [[ ${1:-} == --redemarrer ]] && redemarrer=1
  # shellcheck disable=SC2086
  systemctl disable --now $TIMERS 2>/dev/null || true
  rm -f "$ARCHIVAGE"
  if [[ $redemarrer == 1 ]]; then
    redemarrer_postgres
  else
    log "$ARCHIVAGE retiré ; l'archivage reste actif jusqu'au prochain redémarrage"
  fi
}

sauvegarder() {
  case ${1:-} in full|diff) ;; *) die "sauvegarder full|diff" ;; esac
  as_pg pgbackrest --stanza="$(stanza)" --type="$1" backup
}

# --- exercices de restauration -------------------------------------------------
CIBLE='' DOSSIER='' FICHIER='' PORT=55432 GARDER=0
lire_options() {
  while [[ $# -gt 0 ]]; do
    case $1 in
      --cible) CIBLE=${2:?}; shift 2 ;;
      --dossier) DOSSIER=${2:?}; shift 2 ;;
      --fichier) FICHIER=${2:?}; shift 2 ;;
      --port) PORT=${2:?}; shift 2 ;;
      --garder) GARDER=1; shift ;;
      *) die "option inconnue : $1" ;;
    esac
  done
  [[ -n $DOSSIER ]] || die "--dossier requis"
  [[ $PORT =~ ^[0-9]+$ && $PORT -ge 1024 && $PORT -le 65535 && $PORT != 5432 ]] \
    || die "--port : 1024 à 65535, jamais 5432"
  DOSSIER=$(realpath -m "$DOSSIER")
  local vive
  vive=$(realpath -m "$PG_DATA")
  [[ $DOSSIER != "$vive" && $DOSSIER != "$vive"/* && $vive != "$DOSSIER"/* ]] \
    || die "--dossier recouvre la base vive $PG_DATA : refusé"
  if [[ -e $DOSSIER ]] && [[ -n $(ls -A "$DOSSIER") ]]; then
    die "--dossier $DOSSIER n'est pas vide"
  fi
  install -d -m 0700 "$DOSSIER"
  ceder "$DOSSIER"
}

# Instance jetable : socket dans le dossier, aucune écoute réseau, aucun
# archivage ; l'utilisateur courant en est le seul accès (trust local).
configurer_instance() {
  cat > "$DOSSIER/postgresql.conf" <<CONF
port = $PORT
listen_addresses = ''
unix_socket_directories = '$DOSSIER'
archive_mode = off
ssl = off
# sans lecture à chaud, la récupération ne compare pas max_connections (300
# sur la base vive) à celui de l'instance jetable
hot_standby = off
CONF
  printf 'local all all trust\n' > "$DOSSIER/pg_hba.conf"
  : > "$DOSSIER/pg_ident.conf"
  ceder "$DOSSIER/postgresql.conf" "$DOSSIER/pg_hba.conf" "$DOSSIER/pg_ident.conf"
}

demarrer_instance() {
  as_pg "$PG_BIN/pg_ctl" -D "$DOSSIER" -l "$DOSSIER/exercice.log" -w -t 3600 start
}

arreter_instance() {
  as_pg "$PG_BIN/pg_ctl" -D "$DOSSIER" -m fast -w stop >/dev/null 2>&1 || true
  if [[ $GARDER == 0 ]]; then rm -rf "$DOSSIER"; fi
}

sonder_instance() {
  local cfg="$DOSSIER/ameesh-exercice.json"
  printf '{"dsn": "postgresql://ameesh@/ameesh?host=%s&port=%s", "host": "exercice-restauration", "canons": []}\n' \
    "$DOSSIER" "$PORT" > "$cfg"
  as_pg "$PG_BIN/psql" -h "$DOSSIER" -p "$PORT" -U ameesh -d ameesh -XAtc \
    "SELECT 'instant atteint : ' || coalesce(max(created_at)::text, '?') FROM agent_mailbox" >&2 || true
  AMEESH_CONFIG=$cfg ameesh doctor --probe
}

restaurer() {
  lire_options "$@"
  [[ -n $CIBLE ]] || die "--cible requis (ex. '2026-11-11 08:00:00+00')"
  local t0 t1
  t0=$(date +%s)
  trap arreter_instance EXIT
  as_pg pgbackrest --stanza="$(stanza)" --pg1-path="$DOSSIER" --type=time --target="$CIBLE" \
    --target-action=promote --archive-mode=off --delta=n restore
  configurer_instance
  demarrer_instance
  sonder_instance
  t1=$(date +%s)
  log "exercice réussi : restauration à « $CIBLE », doctor vert, RTO mesuré $((t1 - t0)) s"
}

exercice_dump() {
  lire_options "$@"
  [[ -r $FICHIER ]] || die "--fichier illisible : $FICHIER"
  local t0 t1
  t0=$(date +%s)
  trap arreter_instance EXIT
  as_pg "$PG_BIN/initdb" -D "$DOSSIER" -U postgres -A trust >/dev/null
  configurer_instance
  demarrer_instance
  as_pg "$PG_BIN/psql" -h "$DOSSIER" -p "$PORT" -U postgres -d postgres -Xqc "CREATE ROLE ameesh LOGIN" \
    -c "CREATE DATABASE ameesh OWNER ameesh"
  # le dump (root, 0600) est ouvert ici et lu par pg_restore sur son entrée
  as_pg "$PG_BIN/pg_restore" -h "$DOSSIER" -p "$PORT" -U postgres -d ameesh --no-acl \
    --exit-on-error < "$FICHIER"
  sonder_instance
  t1=$(date +%s)
  log "exercice réussi : dump $(basename "$FICHIER") restauré, doctor vert, RTO mesuré $((t1 - t0)) s"
}

cmd=${1:-}
[[ $# -gt 0 ]] && shift
case $cmd in
  configurer) configurer ;;
  activer) activer "$@" ;;
  desactiver) desactiver "$@" ;;
  sauvegarder) sauvegarder "$@" ;;
  etat) as_pg pgbackrest --stanza="$(stanza)" info ;;
  restaurer) restaurer "$@" ;;
  exercice-dump) exercice_dump "$@" ;;
  *) sed -n '4,44p' "$0" >&2; exit 2 ;;
esac
