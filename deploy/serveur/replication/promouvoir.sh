#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Promotion manuelle d'un standby PostgreSQL 17 (lot #6868, EP8.3a ; geste
# réel d'EP8.3 #6860, jamais joué ici). Le code ne décide pas : c'est un
# geste d'exploitant, dans la fenêtre de bascule, en attendant Patroni (EP8.4).
#
#   promouvoir.sh [--force] [nom-du-noeud]
#
# Gardes contre le split brain, dans cet ordre :
#   - un nœud nofailover (le hub) n'est jamais promu sans --force ;
#   - le serveur local doit tourner et être en récupération ;
#   - aucun autre nœud joignable ne doit répondre comme primaire ; un nœud
#     joignable dont le rôle est illisible arrête aussi (on ne parie pas) ;
#   - --force ne sert qu'à une décision humaine de désastre, et le dit.
#
# Après promotion : synchrones réécrits (ANY 1 sur la paire d'agents), rechargement,
# slot physique créé pour chaque autre nœud. Le retour de l'ancien primaire se
# fait par reintegrer.sh ; les autres standbys par suivre-primaire.sh.
set -euo pipefail
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
usage: promouvoir.sh [--force] [nom-du-noeud]

  --force   passe outre le nofailover et le contrôle d'absence d'autre
            primaire ; réservé à une décision humaine en situation de
            désastre (double panne), jamais à une bascule planifiée.
  nom-du-noeud   nœud de noeuds.conf ; par défaut le nom court de l'hôte.

La bascule planifiée arrête d'abord l'ancien primaire, puis promeut un
standby ; ce script ne fait pas le premier geste.
EOF
}

force=0
nom=""
while (( $# )); do
  case $1 in
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

log "nœud $NOM_LOCAL ($HOTE_LOCAL) — inventaire : $ROLE_INVENTAIRE, synchrone=$SYNC_LOCAL, nofailover=$NOFAIL_LOCAL"
if [[ $NOFAIL_LOCAL == oui ]] && (( !force )); then
  die "$NOM_LOCAL est nofailover : jamais promu (4 vCPU sans swap) ; --force seulement pour une décision humaine de désastre"
fi
pg_isready -q 2>/dev/null || die "le serveur local ne tourne pas : rien à promouvoir"
if ! recovery=$(psql_local -tAc 'SELECT pg_is_in_recovery()'); then
  die "pg_is_in_recovery() illisible sur $NOM_LOCAL"
fi
[[ $recovery == t ]] || die "$NOM_LOCAL n'est pas en récupération : il se croit déjà primaire (rien à faire)"

verifier_pgpass
hints=$(psql_local -tAc 'SHOW wal_log_hints' || true)
[[ $hints == on ]] \
  || info "ATTENTION : wal_log_hints=$hints sur $NOM_LOCAL ; ce nœud ne pourra pas être rewound plus tard (le paramètre s'applique au redémarrage)"

# Aucun autre primaire joignable : le contrôle porte sur le rôle annoncé, pas
# seulement sur le port ouvert. Un nœud joignable mais illisible arrête : on
# ne promeut pas à l'aveugle.
for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
  (( i == IDX_LOCAL )) && continue
  autre=${NOEUDS_NOMS[$i]}
  autre_hote=${NOEUDS_HOTES[$i]}
  if pg_isready -q -h "$autre_hote" 2>/dev/null; then
    if ! etat=$(psql_rewind "$autre_hote" -tAc 'SELECT pg_is_in_recovery()'); then
      (( force )) || die "$autre ($autre_hote) est joignable mais son rôle est illisible : refus de promouvoir (--force pour une décision humaine)"
      info "ATTENTION : $autre joignable, rôle illisible, --force accordé"
    elif [[ $etat == f ]]; then
      (( force )) || die "un primaire répond déjà : $autre ($autre_hote) — split brain interdit, arrêtez-le d'abord"
      info "ATTENTION : $autre répond comme primaire, promotion forcée"
    fi
  fi
done

log "promotion de $NOM_LOCAL (aucun autre primaire joignable)"
pg_ctlcluster "$PG_VERSION" main promote
attendre_valeur "promotion (pg_is_in_recovery)" 'SELECT pg_is_in_recovery()' f
log "promotion confirmée : $NOM_LOCAL est primaire"

# La configuration de synchrones est commune aux deux agents ; on la réécrit
# et on recharge pour que le nouveau primaire attende un synchrone.
ecrire_fichier "$CONF_FRAGMENT" 0644 "$(fragment_conf)"
remplacer_bloc_hba "$(bloc_hba)"
pg_ctlcluster "$PG_VERSION" main reload
log "synchrones rechargés : $(liste_sync)"

poser_slots
log "terminé — ancien primaire à réintégrer avec reintegrer.sh --source $NOM_LOCAL ; autres standbys avec suivre-primaire.sh --source $NOM_LOCAL"
