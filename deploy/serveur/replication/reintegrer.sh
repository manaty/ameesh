#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Réintègre l'ancien primaire comme standby du nouveau primaire par pg_rewind
# (lot #6868, EP8.3a ; geste réel d'EP8.3 #6860, jamais joué ici).
#
#   reintegrer.sh --source <nouveau-primaire> [nom-du-noeud]
#
# Le serveur local doit être proprement arrêté ; le script ne l'arrête pas.
# pg_rewind utilise le rôle rewind (quatre fonctions, pas superuser) et le
# pgpass du compte postgres. wal_log_hints=on est requis sur l'ancien primaire
# (configurer-noeud.sh le pose ; il faut le redémarrage d'EP8.1/EP8.3).
#
# pg_rewind recopie postgresql.auto.conf (et tout fichier de configuration
# présent dans le répertoire de données) : le fragment, pg_hba,
# primary_conninfo/primary_slot_name et standby.signal sont réécrits ici
# après le rewind. Un échec n'est pas masqué : le répertoire n'est pas
# redémarré en primaire, et le re-seed explicite est imprimé.
set -euo pipefail
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
usage: reintegrer.sh --source <nouveau-primaire> [nom-du-noeud]

  --source <nœud>   nouveau primaire, en service ; obligatoire (le défaut de
                    l'inventaire peut être le nœud local après une bascule).
  nom-du-noeud      nœud de noeuds.conf ; par défaut le nom court de l'hôte.

Le serveur local doit être arrêté : ce script ne l'arrête pas lui-même. En
cas d'échec de pg_rewind, le re-seed explicite est :
  initialiser-standby.sh --reinitialiser --source <nœud> [nom-du-noeud]
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
[[ -n $source_nom ]] \
  || die "--source requis : après une bascule, le primaire de l'inventaire peut être le nœud local — indiquer le NOUVEAU primaire"
indice_de "$source_nom" || die "nœud source « $source_nom » absent de l'inventaire"
SOURCE_IDX=$IDX
SOURCE_NOM=${NOEUDS_NOMS[$SOURCE_IDX]}
SOURCE_HOTE=${NOEUDS_HOTES[$SOURCE_IDX]}
[[ $SOURCE_NOM != "$NOM_LOCAL" ]] || die "la source et le nœud local sont le même nœud ($NOM_LOCAL)"
[[ -d $DATA && -f $DATA/PG_VERSION ]] \
  || die "répertoire de données absent ou non initialisé : $DATA (utiliser initialiser-standby.sh)"
[[ -L $DATA ]] && die "$DATA est un lien symbolique : le répertoire de données doit être réel"

log "nœud $NOM_LOCAL ($HOTE_LOCAL) — réintégration derrière $SOURCE_NOM ($SOURCE_HOTE)"

if pg_isready -q 2>/dev/null; then
  die "le serveur local tourne : arrêtez-le d'abord (pg_ctlcluster $PG_VERSION main stop) — pg_rewind exige un répertoire arrêté proprement"
fi

installer_pgpass
verifier_pgpass

pg_isready -q -h "$SOURCE_HOTE" || die "source $SOURCE_NOM ($SOURCE_HOTE) injoignable"
if ! source_recovery=$(psql_rewind "$SOURCE_HOTE" -tAc 'SELECT pg_is_in_recovery()'); then
  die "source $SOURCE_NOM joignable mais rôle illisible (rôle rewind, pg_hba, pgpass)"
fi
[[ $source_recovery == f ]] || die "source $SOURCE_NOM est en récupération : pg_rewind se fait vers un primaire en service"
hints=$(psql_rewind "$SOURCE_HOTE" -tAc 'SHOW wal_log_hints' || true)
[[ $hints == on ]] || info "ATTENTION : wal_log_hints=$hints sur $SOURCE_NOM ; à corriger (redémarrage) avant qu'il ne doive être rewound à son tour"

log "pg_rewind de $DATA vers $SOURCE_NOM ($SOURCE_HOTE), rôle rewind"
if ! comme_postgres "$PG_BIN/pg_rewind" \
    --target-pgdata="$DATA" \
    --source-server="host=$SOURCE_HOTE port=5432 user=rewind dbname=postgres connect_timeout=10 passfile=$PGPASS" \
    --progress; then
  die "pg_rewind a échoué : le répertoire $DATA n'est pas garanti réutilisable ; NE PAS le redémarrer en primaire. Re-seed explicite : deploy/serveur/replication/initialiser-standby.sh --reinitialiser --source $SOURCE_NOM $NOM_LOCAL"
fi
log "pg_rewind terminé"

# pg_rewind recopie postgresql.auto.conf (et les fichiers de configuration du
# répertoire de données) : on repose celle du nœud (fragment, pg_hba,
# paramètres de reprise), sinon le standby pourrait suivre une autre source ou
# démarrer avec la configuration d'un primaire.
ecrire_fichier "$CONF_FRAGMENT" 0644 "$(fragment_conf)"
remplacer_bloc_hba "$(bloc_hba)"
ecrire_reprise "$SOURCE_HOTE" "$NOM_LOCAL" "$(slot_de "$NOM_LOCAL")"
poser_standby_signal
controler_configuration

log "démarrage du standby $NOM_LOCAL"
pg_ctlcluster "$PG_VERSION" main start
attendre_commande "démarrage de PostgreSQL" pg_isready -q
attendre_valeur "standby en récupération" 'SELECT pg_is_in_recovery()' t
attendre_valeur "flux entrant (pg_stat_wal_receiver)" 'SELECT status FROM pg_stat_wal_receiver' streaming
log "ancien primaire $NOM_LOCAL réintégré en standby de $SOURCE_NOM — vérifier avec etat.sh (aucun second primaire)"
