#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Fait suivre à un standby PostgreSQL 17 le nouveau primaire après une
# promotion manuelle (lot #6868, EP8.3a ; geste réel d'EP8.3 #6860).
#
#   suivre-primaire.sh [--source <nœud>] [nom-du-noeud]
#
# Réécrit primary_conninfo et primary_slot_name dans postgresql.auto.conf,
# recharge (ou démarre) le standby, puis attend pg_stat_wal_receiver =
# streaming. Idempotent : rejouable si le flux est déjà sur la bonne source.
# Aucun mot de passe dans la configuration : le pgpass du compte postgres le
# porte.
set -euo pipefail
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
usage: suivre-primaire.sh [--source <nœud>] [nom-du-noeud]

  --source <nœud>   nouveau primaire (défaut : le primaire de noeuds.conf) ;
                    il doit être en service et primaire.
  nom-du-noeud      nœud de noeuds.conf ; par défaut le nom court de l'hôte.

Le nœud local doit être un standby (ou son répertoire de données doit exister
et il sera démarré en standby). Un nœud qui se croit primaire est refusé :
deux primaires ne se suivent pas.
EOF
}

source_nom=""
nom=""
while (( $# )); do
  case $1 in
    --source)
      shift
      (( $# )) || die "--source attend un nom de nœud"
      source_nom=$1
      ;;
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
source_nom=${source_nom:-$PRIMAIRE_NOM}
indice_de "$source_nom" || die "nœud source « $source_nom » absent de l'inventaire"
SOURCE_IDX=$IDX
SOURCE_NOM=${NOEUDS_NOMS[$SOURCE_IDX]}
SOURCE_HOTE=${NOEUDS_HOTES[$SOURCE_IDX]}
[[ $SOURCE_NOM != "$NOM_LOCAL" ]] || die "la source et le nœud local sont le même nœud ($NOM_LOCAL)"
[[ -d $DATA ]] || die "répertoire de données absent : $DATA (utiliser initialiser-standby.sh)"

installer_pgpass
verifier_pgpass

pg_isready -q -h "$SOURCE_HOTE" || die "source $SOURCE_NOM ($SOURCE_HOTE) injoignable"
if ! source_recovery=$(psql_rewind "$SOURCE_HOTE" -tAc 'SELECT pg_is_in_recovery()'); then
  die "source $SOURCE_NOM joignable mais rôle illisible (rôle rewind, pg_hba, pgpass)"
fi
[[ $source_recovery == f ]] || die "source $SOURCE_NOM est en récupération : un standby ne la suit pas"

if pg_isready -q 2>/dev/null; then
  if ! recovery=$(psql_local -tAc 'SELECT pg_is_in_recovery()'); then
    die "pg_is_in_recovery() illisible sur $NOM_LOCAL"
  fi
  [[ $recovery == t ]] || die "$NOM_LOCAL se croit primaire : refus de le faire suivre $SOURCE_NOM (deux primaires)"
  etait_en_marche=1
else
  etait_en_marche=0
fi

slot=$(slot_de "$NOM_LOCAL")
ecrire_reprise "$SOURCE_HOTE" "$NOM_LOCAL" "$slot"
poser_standby_signal
log "paramètres de reprise : $SOURCE_NOM ($SOURCE_HOTE), slot $slot"

if (( etait_en_marche )); then
  pg_ctlcluster "$PG_VERSION" main reload
  log "standby rechargé (pg_ctlcluster $PG_VERSION main reload)"
else
  pg_ctlcluster "$PG_VERSION" main start
  log "standby démarré"
fi
attendre_commande "démarrage de PostgreSQL" pg_isready -q
attendre_valeur "standby en récupération" 'SELECT pg_is_in_recovery()' t
attendre_valeur "flux entrant (pg_stat_wal_receiver)" 'SELECT status FROM pg_stat_wal_receiver' streaming
log "standby $NOM_LOCAL en flux depuis $SOURCE_NOM (slot $slot)"
