#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Retour arrière de la préparation de réplication d'un nœud (lot #6868,
# EP8.3a) : retire la configuration posée par configurer-noeud.sh, et sur
# demande les slots et les rôles. Idempotent ; ne supprime aucune donnée, ne
# touche ni au standby.signal ni au répertoire de données, n'arrête pas le
# serveur.
#
#   retour-arriere.sh [--tout | --slots] [--roles] [--force] [nom-du-noeud]
#
#   --slots   supprime les slots physiques des autres nœuds (un slot actif
#             arrête sans --force) ;
#   --roles   supprime les rôles replicator et rewind (après les slots) ;
#   --tout    --slots et --roles ;
#   --force   termine un flux actif avant de supprimer son slot.
#
# Les paramètres qui n'ont été posés qu'au redémarrage (wal_log_hints) ne
# reviennent à leur valeur par défaut qu'au redémarrage suivant, qui n'est pas
# fait ici : la commande est imprimée.
set -euo pipefail
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
usage: retour-arriere.sh [--tout | --slots] [--roles] [--force] [nom-du-noeud]

Retire le fragment conf.d et le bloc pg_hba du lot #6868 (toujours), puis :
  --slots   les slots physiques des autres nœuds (serveur en service requis) ;
  --roles   les rôles replicator et rewind (après les slots) ;
  --tout    les deux ;
  --force   termine un flux actif avant de supprimer son slot.

Aucune donnée n'est supprimée ; un standby reste un standby (standby.signal
intact). Le redémarrage qui oublie wal_log_hints n'est pas fait ici.
EOF
}

supprimer_slots=0
supprimer_roles=0
force=0
nom=""
while (( $# )); do
  case $1 in
    --slots) supprimer_slots=1 ;;
    --roles) supprimer_roles=1 ;;
    --tout) supprimer_slots=1; supprimer_roles=1 ;;
    --force) force=1 ;;
    -h | --help) usage; exit 0 ;;
    -*) die "option inconnue : $1 (voir --help)" ;;
    *)
      [[ -z $nom ]] || die "un seul nom de nœud attendu"
      nom=$1
      ;;
  esac
  shift
done

charger_env
lire_noeuds
trouver_primaire
charger_noeud_local "$nom"

log "retour arrière de la réplication sur $NOM_LOCAL ($HOTE_LOCAL)"

# 1. Configuration : le fragment du lot et son bloc pg_hba. Le reste de
#    pg_hba.conf et de conf.d est intact (bloc marqué, fichier dédié).
if [[ -f $CONF_FRAGMENT ]]; then
  rm -f -- "$CONF_FRAGMENT"
  log "fragment retiré : $CONF_FRAGMENT"
else
  info "fragment déjà absent : $CONF_FRAGMENT"
fi
retirer_bloc_hba
log "bloc pg_hba retiré (s'il était présent)"

if pg_isready -q 2>/dev/null; then
  pg_ctlcluster "$PG_VERSION" main reload
  log "configuration rechargée (pg_ctlcluster $PG_VERSION main reload)"
  info "wal_log_hints, wal_level et max_* reprennent leur valeur par défaut au prochain REDÉMARRAGE :"
  info "  pg_ctlcluster $PG_VERSION main restart"
else
  info "serveur arrêté : la configuration retirée s'appliquera au prochain démarrage"
fi

# 2. Slots : uniquement sur un primaire en service (un standby n'en a pas).
if (( supprimer_slots )); then
  if ! pg_isready -q 2>/dev/null; then
    info "serveur arrêté : aucun slot à supprimer ici"
  elif [[ $(role_effectif) != primaire ]]; then
    info "nœud standby : aucun slot local à supprimer"
  else
    for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
      (( i == IDX_LOCAL )) && continue
      autre=${NOEUDS_NOMS[$i]}
      slot=$(slot_de "$autre")
      if ! active=$(psql_local -tAc "SELECT active FROM pg_replication_slots WHERE slot_name = '$slot'"); then
        die "lecture du slot $slot impossible"
      fi
      [[ -n $active ]] || { info "slot $slot ($autre) déjà absent"; continue; }
      if [[ $active == t ]]; then
        if (( force )); then
          psql_local -tAc "SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots WHERE slot_name = '$slot' AND active" >/dev/null
          sleep 1
          info "flux de $autre terminé (--force)"
        else
          die "slot $slot ($autre) actif : arrêtez le flux ou utilisez --force"
        fi
      fi
      psql_local -tAc "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = '$slot' AND NOT active" >/dev/null
      if ! reste=$(psql_local -tAc "SELECT 1 FROM pg_replication_slots WHERE slot_name = '$slot'"); then
        die "vérification du slot $slot impossible"
      fi
      [[ -z $reste ]] || die "slot $slot encore présent après suppression (flux réapparu ?)"
      log "slot $slot ($autre) supprimé"
    done
  fi
fi

# 3. Rôles : après les slots, pour ne pas laisser un slot rattaché à un rôle
#    qui n'existe plus. B3 de revue #6868 : DROP ROLE rewind échoue tant que
#    les quatre GRANT EXECUTE de sql/roles.sql dépendent de lui ; on les
#    révoque d'abord, précisément (aucun DROP OWNED général ni destructif).
if (( supprimer_roles )); then
  if pg_isready -q 2>/dev/null; then
    psql_local <<'SQL'
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rewind') THEN
    REVOKE EXECUTE ON FUNCTION pg_catalog.pg_ls_dir(text, boolean, boolean) FROM rewind;
    REVOKE EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text, boolean) FROM rewind;
    REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) FROM rewind;
    REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text, bigint, bigint, boolean) FROM rewind;
  END IF;
END
$$;
DROP ROLE IF EXISTS rewind;
DROP ROLE IF EXISTS replicator;
SQL
    log "rôles rewind et replicator supprimés (s'ils existaient ; grants pg_rewind révoqués)"
  else
    info "serveur arrêté : rôles non supprimés (rejouer avec le serveur en service)"
  fi
fi

log "terminé — configuration de réplication retirée ; aucune donnée touchée"
