#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# État de réplication du nœud local (lot #6868, EP8.3a) — lecture seule.
#
#   etat.sh [nom-du-noeud]
#
# Sur un primaire : rôles des standbys (pg_stat_replication), synchrones
# attendus, slots et leur état (actif, wal_status), paramètres de réplication.
# Sur un standby : flux entrant (pg_stat_wal_receiver), source, slot, retards.
# Les avertissements sont imprimés mais ne changent pas le code de sortie :
# 0 si le serveur répond, 1 s'il ne répond pas. Aucun secret n'est lu, aucune
# connexion distante n'est faite (le pgpass n'est pas nécessaire).
set -euo pipefail
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
usage: etat.sh [nom-du-noeud]

Lit l'état local (aucune connexion distante, aucun secret) :
  - rôle effectif et paramètres de réplication ;
  - primaire : standbys connectés, sync_state, slots, WAL retenu ;
  - standby : flux entrant, source, slot, retards.
Sortie 0 si le serveur répond, 1 sinon. Les avertissements ne bloquent pas.
EOF
}

nom=""
case ${1:-} in
  -h | --help) usage; exit 0 ;;
  '') ;;
  -*) die "option inconnue : $1 (voir --help)" ;;
  *) nom=$1 ;;
esac
(( $# <= 1 )) || die "au plus un nom de nœud"

charger_env
lire_noeuds
trouver_primaire
charger_noeud_local "$nom"

printf 'nœud              : %s (%s)\n' "$NOM_LOCAL" "$HOTE_LOCAL"
printf 'inventaire        : rôle %s, synchrone %s, nofailover %s, priorité %s\n' \
  "$ROLE_INVENTAIRE" "$SYNC_LOCAL" "$NOFAIL_LOCAL" "$PRIO_LOCAL"
printf 'synchrones voulus : %s\n' "$(liste_sync)"

if ! pg_isready -q 2>/dev/null; then
  printf 'état              : INJOIGNABLE (pg_isready)\n'
  exit 1
fi

if ! recovery=$(psql_local -tAc 'SELECT pg_is_in_recovery()'); then
  printf 'état              : JOIGNABLE mais rôle illisible (psql)\n'
  exit 1
fi
if [[ $recovery == t ]]; then
  role=standby
else
  role=primaire
fi
printf 'état              : joignable, %s\n' "$role"

wal_level=$(psql_local -tAc 'SHOW wal_level')
wal_log_hints=$(psql_local -tAc 'SHOW wal_log_hints')
sync_names=$(psql_local -tAc 'SHOW synchronous_standby_names')
max_senders=$(psql_local -tAc 'SHOW max_wal_senders')
max_slots=$(psql_local -tAc 'SHOW max_replication_slots')
printf 'wal_level         : %s (attendu replica)\n' "$wal_level"
printf 'wal_log_hints     : %s (attendu on pour pg_rewind)\n' "$wal_log_hints"
printf 'synchrones actifs : %s\n' "${sync_names:-aucun}"
printf 'max_wal_senders   : %s\n' "$max_senders"
printf 'max_replication_slots : %s\n' "$max_slots"

avertissements=0
avertir() {
  printf 'AVERTISSEMENT     : %s\n' "$*"
  avertissements=$((avertissements + 1))
}

[[ $wal_log_hints == on ]] || avertir "wal_log_hints=$wal_log_hints : pg_rewind de ce nœud impossible (activer, redémarrer)"
[[ $wal_level == replica ]] || avertir "wal_level=$wal_level : la réplication en flux est impossible"

# Un nœud nofailover ne doit jamais apparaître dans synchronous_standby_names.
for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
  [[ ${NOEUDS_NOFAIL[$i]} == oui ]] || continue
  if [[ $sync_names == *"${NOEUDS_NOMS[$i]}"* ]]; then
    avertir "${NOEUDS_NOMS[$i]} est nofailover mais apparaît dans synchronous_standby_names"
  fi
done

if [[ $role == primaire ]]; then
  echo
  echo 'standbys connectés (application_name|state|sync_state|sent_lsn|replay_lsn) :'
  lignes=$(psql_local -tAc 'SELECT application_name, state, sync_state, sent_lsn, replay_lsn FROM pg_stat_replication ORDER BY application_name')
  if [[ -z $lignes ]]; then
    echo '  (aucun)'
    avertir "aucun standby connecté : RPO non garanti"
  else
    synchrone=0
    while IFS='|' read -r app etat sync_state sent replay; do
      [[ -n $app ]] || continue
      printf '  %-20s %-10s %-8s sent=%s replay=%s\n' "$app" "$etat" "$sync_state" "${sent:-?}" "${replay:-?}"
      [[ $sync_state == sync ]] && synchrone=1
    done <<< "$lignes"
    if [[ -n $sync_names && $synchrone -eq 0 ]]; then
      avertir "aucun standby sync : synchronous_standby_names=$sync_names sans « sync » (RPO dégradé, synchronous_mode_strict=false)"
    fi
  fi

  echo
  echo 'slots physiques (slot_name|active|wal_status) :'
  slots=$(psql_local -tAc 'SELECT slot_name, active, wal_status FROM pg_replication_slots ORDER BY slot_name')
  if [[ -z $slots ]]; then
    echo '  (aucun)'
  else
    while IFS='|' read -r slot actif statut; do
      [[ -n $slot ]] || continue
      printf '  %-24s actif=%-5s wal_status=%s\n' "$slot" "${actif:-?}" "${statut:-?}"
      case $statut in
        lost) avertir "slot $slot perdu (WAL non retenu) : re-seed du standby concerné" ;;
        unreserved | extended) avertir "slot $slot en état $statut : WAL non réservé, surveiller le disque" ;;
      esac
      [[ $actif == t ]] || avertir "slot $slot inactif : il retient du WAL jusqu'à max_slot_wal_keep_size"
    done <<< "$slots"
  fi
else
  echo
  echo 'flux entrant (status|sender_host|slot_name|write_lag|flush_lag|replay_lag) :'
  lignes=$(psql_local -tAc "SELECT status, coalesce(sender_host, ''), coalesce(slot_name, ''), coalesce(write_lag::text, ''), coalesce(flush_lag::text, ''), coalesce(replay_lag::text, '') FROM pg_stat_wal_receiver")
  if [[ -z $lignes ]]; then
    echo '  (aucun)'
    avertir "aucun flux entrant : pg_stat_wal_receiver est vide"
  else
    while IFS='|' read -r statut source slot write_lag flush_lag replay_lag; do
      [[ -n $statut ]] || continue
      printf '  statut=%s source=%s slot=%s write_lag=%s flush_lag=%s replay_lag=%s\n' \
        "$statut" "${source:-?}" "${slot:-?}" "${write_lag:-?}" "${flush_lag:-?}" "${replay_lag:-?}"
      [[ $statut == streaming ]] || avertir "flux $statut (attendu streaming)"
    done <<< "$lignes"
  fi
fi

echo
if (( avertissements == 0 )); then
  echo 'bilan             : conforme aux attentes de ce lot'
else
  printf 'bilan             : %d avertissement(s), voir ci-dessus (non bloquant ici)\n' "$avertissements"
fi
