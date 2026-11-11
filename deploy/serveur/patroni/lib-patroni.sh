#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Fonctions communes d'adopter.sh et de retour-arriere.sh (lot #6861, EP8.4).
# Sourcé après deploy/serveur/replication/lib.sh, jamais exécuté.
#
# Chemins surchargeables pour les témoins (faux binaires, aucun hôte réel) :
#   AMEESH_PATRONI_CONF         (/etc/patroni/patroni.yml)
#   AMEESH_PATRONI_ENV          (/etc/ameesh/patroni.env)
#   AMEESH_PATRONI_TOPOLOGIE    (topologie.json à côté de ce fichier)
#   AMEESH_PATRONI_ETCD         (/usr/local/bin/etcd)
#   AMEESH_PATRONI_ETCDCTL      (/usr/local/bin/etcdctl)
#   AMEESH_PATRONI_BIN          (/opt/patroni/venv/bin/patroni)
#   AMEESH_PATRONI_SAUVEGARDES  (/var/lib/ameesh-db/avant-patroni)
#   AMEESH_PATRONI_WATCHDOG     (/dev/watchdog)
#   AMEESH_PATRONI_SUIVRE       (replication/suivre-primaire.sh)
#   AMEESH_ADOPTION_LECTURE     (document /cluster enregistré, lu par adoption.py)

PATRONI_DIR=${PATRONI_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
PATRONI_CONF=${AMEESH_PATRONI_CONF:-/etc/patroni/patroni.yml}
PATRONI_ENV=${AMEESH_PATRONI_ENV:-/etc/ameesh/patroni.env}
TOPOLOGIE=${AMEESH_PATRONI_TOPOLOGIE:-$PATRONI_DIR/topologie.json}
ETCD_BIN=${AMEESH_PATRONI_ETCD:-/usr/local/bin/etcd}
ETCDCTL=${AMEESH_PATRONI_ETCDCTL:-/usr/local/bin/etcdctl}
PATRONI_BIN=${AMEESH_PATRONI_BIN:-/opt/patroni/venv/bin/patroni}
SAUVEGARDES=${AMEESH_PATRONI_SAUVEGARDES:-/var/lib/ameesh-db/avant-patroni}
WATCHDOG=${AMEESH_PATRONI_WATCHDOG:-/dev/watchdog}
START_CONF="$PGCONF/start.conf"
UNITE_PG="postgresql@$PG_VERSION-main"
REEL=${REEL:-0}
ACCEPTER_HBA=${ACCEPTER_HBA:-0}

# Paramètres que Patroni pose lui-même : dans postgresql.auto.conf (lu en
# dernier), ils l'emporteraient sur les siens et figeraient la grappe.
PARAMETRES_GERES='synchronous_standby_names|hot_standby|wal_level|max_connections|max_wal_senders|max_replication_slots|max_worker_processes|max_locks_per_transaction|max_prepared_transactions|track_commit_timestamp|wal_log_hints|wal_keep_size|listen_addresses|port|cluster_name|hba_file|ident_file|recovery_target[a-z_]*'

aide() {
  python3 -I "$PATRONI_DIR/adoption.py" "$@"
}

# Un geste : exécuté avec --geste-reel, affiché sinon.
geste() {
  if (( REEL )); then
    info "geste : $*"
    "$@"
  else
    info "[simulation] $*"
  fi
}

# Valeur d'une clé de patroni.env, sans exécuter le fichier.
valeur_env() {
  sed -n "s/^$1=//p" "$PATRONI_ENV" | tail -n1
}

topologie_valeur() {
  python3 -I -c 'import json, sys
t = json.load(open(sys.argv[1], encoding="utf-8"))
for cle in sys.argv[2].split("."):
    t = t[cle]
print(t)' "$TOPOLOGIE" "$1"
}

url_rest() {
  printf 'https://%s:%s' "$1" "$(topologie_valeur ports.patroni_rest)"
}

patroni_actif() {
  systemctl is-active --quiet patroni.service
}

etcdctl_grappe() {
  "$ETCDCTL" --endpoints "$(valeur_env AMEESH_DB_ETCD_ENDPOINTS)" \
    --cacert "$(valeur_env AMEESH_DB_CA)" \
    --cert "$(valeur_env AMEESH_DB_ETCD_CERT)" \
    --key "$(valeur_env AMEESH_DB_ETCD_KEY)" \
    --command-timeout=5s "$@"
}

# Au moins deux membres sains sur trois.
etcd_quorum() {
  local sains
  sains=$(etcdctl_grappe endpoint health --cluster 2>&1 | grep -c ' is healthy: ' || true)
  (( sains >= 2 ))
}

# Clé Patroni du DCS (leader, initialize) ; vide si absente. Une lecture
# impossible arrête : « absente » et « illisible » ne se confondent pas.
cle_dcs() {
  local cle
  cle="$(topologie_valeur namespace)$(topologie_valeur grappe)/$1"
  etcdctl_grappe get --print-value-only "$cle" \
    || die "DCS illisible ($cle) : etcd injoignable ou certificats refusés"
}

identifiant_systeme() {
  LC_ALL=C comme_postgres "$PG_BIN/pg_controldata" -D "$DATA" \
    | sed -n 's/^Database system identifier:[[:space:]]*//p'
}

# Paramètres gérés par Patroni présents dans postgresql.auto.conf, hors du
# bloc de reprise d'EP8.3 (retiré au passage) et hors primary_conninfo /
# primary_slot_name (laissés par pg_basebackup -R, retirés aussi).
parametres_geres_auto_conf() {
  local fichier="$DATA/postgresql.auto.conf"
  [[ -r $fichier ]] || return 0
  awk -v debut="$REPRISE_DEBUT" -v fin="$HBA_FIN" -v geres="^($PARAMETRES_GERES)\$" '
    $0 == debut { dedans = 1; next }
    $0 == fin   { dedans = 0; next }
    dedans      { next }
    /^[[:space:]]*[a-z_]+[[:space:]]*=/ {
      nom = $0; sub(/^[[:space:]]*/, "", nom); sub(/[[:space:]]*=.*/, "", nom)
      if (nom ~ geres) printf "%s ", nom
    }
  ' "$fichier"
}

# Retire le bloc de reprise d'EP8.3 et les lignes primary_conninfo /
# primary_slot_name : sous Patroni, elles fixeraient la source du flux sur
# l'ancien primaire après une bascule. Le reste de auto.conf est conservé.
retirer_reprise_auto_conf() {
  local fichier="$DATA/postgresql.auto.conf" tmp
  [[ -f $fichier ]] || return 0
  tmp=$(mktemp "$DATA/.auto.XXXXXX") || die "temporaire impossible dans $DATA"
  awk -v debut="$REPRISE_DEBUT" -v fin="$HBA_FIN" '
    $0 == debut { dedans = 1; next }
    $0 == fin   { dedans = 0; next }
    dedans      { next }
    !/^[[:space:]]*(primary_conninfo|primary_slot_name)[[:space:]]*=/ { print }
  ' "$fichier" > "$tmp"
  chmod --reference="$fichier" -- "$tmp"
  if (( EUID == 0 )); then
    chown --reference="$fichier" -- "$tmp" || die "chown impossible sur $tmp"
  fi
  mv -f -- "$tmp" "$fichier"
}

# start.conf de l'unité Debian : « manual » sous Patroni, « auto » au retour.
ecrire_start_conf() {
  local valeur=$1 tmp
  tmp=$(mktemp "$PGCONF/.start.XXXXXX") || die "temporaire impossible dans $PGCONF"
  printf '# Géré par deploy/serveur/patroni (lot #6861) : %s\n%s\n' \
    "$([[ $valeur == manual ]] && echo 'Patroni démarre la base' || echo 'unité Debian')" "$valeur" > "$tmp"
  if [[ -f $START_CONF ]]; then
    chmod --reference="$START_CONF" -- "$tmp"
    if (( EUID == 0 )); then chown --reference="$START_CONF" -- "$tmp"; fi
  else
    chmod 0644 -- "$tmp"
  fi
  mv -f -- "$tmp" "$START_CONF"
}

# Copie la configuration Debian du nœud avant que Patroni la réécrive
# (postgresql.conf renommé en postgresql.base.conf, pg_hba.conf et pgpass
# réécrits). retour-arriere.sh relit la sauvegarde « dernier ».
sauvegarder_configuration() {
  [[ ! -e $PGCONF/postgresql.base.conf ]] \
    || die "$PGCONF/postgresql.base.conf existe : Patroni est déjà passé ici, la configuration d'origine n'est plus dans postgresql.conf (retour-arriere.sh d'abord)"
  local horodatage dossier
  horodatage=$(date -u +%Y%m%dT%H%M%SZ)
  dossier="$SAUVEGARDES/$horodatage-$NOM_LOCAL"
  if (( ! REEL )); then
    info "[simulation] sauvegarde de postgresql.conf, pg_hba.conf, pg_ident.conf, start.conf, conf.d, postgresql.auto.conf et pgpass dans $dossier"
    return 0
  fi
  install -d -m 0700 -- "$SAUVEGARDES" "$dossier"
  local f
  for f in postgresql.conf pg_hba.conf pg_ident.conf start.conf; do
    [[ -e $PGCONF/$f ]] && cp -a -- "$PGCONF/$f" "$dossier/$f"
  done
  [[ -d $PGCONF/conf.d ]] && cp -a -- "$PGCONF/conf.d" "$dossier/conf.d"
  [[ -e $DATA/postgresql.auto.conf ]] && cp -a -- "$DATA/postgresql.auto.conf" "$dossier/postgresql.auto.conf"
  [[ -e $PGPASS ]] && cp -a -- "$PGPASS" "$dossier/pgpass"
  printf '%s\n' "${ROLE:-inconnu}" > "$dossier/role"
  ln -sfn -- "$dossier" "$SAUVEGARDES/dernier"
  log "configuration Debian sauvegardée : $dossier"
}

# Aucun autre nœud joignable ne répond comme primaire (même contrôle que
# replication/promouvoir.sh, par le rôle rewind d'EP8.3 et son pgpass).
aucun_autre_primaire() {
  local i autre hote etat
  for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
    (( i == IDX_LOCAL )) && continue
    autre=${NOEUDS_NOMS[$i]}
    hote=${NOEUDS_HOTES[$i]}
    pg_isready -q -h "$hote" 2>/dev/null || continue
    etat=$(psql_rewind "$hote" -tAc 'SELECT pg_is_in_recovery()') \
      || die "$autre ($hote) joignable mais son rôle est illisible : on n'adopte pas à l'aveugle"
    [[ $etat != f ]] || die "$autre ($hote) répond aussi comme primaire : split brain, adoption refusée"
  done
}

# Mots de passe de patroni.env posés sur le leader : la réplication des
# standbys adoptés et pg_rewind (superutilisateur en TCP) s'en servent. Sur
# l'entrée standard de psql, jamais en argument ; la journalisation des
# instructions est coupée pour la session.
poser_mots_de_passe_patroni() {
  local replication superuser
  replication=$(valeur_env PATRONI_REPLICATION_PASSWORD)
  superuser=$(valeur_env PATRONI_SUPERUSER_PASSWORD)
  [[ ${#replication} -ge 24 && ${#superuser} -ge 24 ]] \
    || die "mots de passe de $PATRONI_ENV trop courts (24 caractères au moins)"
  {
    printf "SET log_statement = 'none';\nSET log_min_duration_statement = -1;\n"
    printf 'ALTER ROLE postgres PASSWORD %s;\n' "$(litteral_sql "$superuser")"
    printf 'ALTER ROLE replicator PASSWORD %s;\n' "$(litteral_sql "$replication")"
  } | psql_local
  log "mots de passe postgres et replicator de $PATRONI_ENV posés sur le leader"
}
