#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Initialise un standby PostgreSQL 17 par base backup physique depuis le
# primaire (lot #6868, EP8.3a ; geste réel d'EP8.3 #6860, jamais joué ici).
#
#   initialiser-standby.sh [--source <nœud>] [--reinitialiser] [nom-du-noeud]
#
# Le nœud doit être arrêté. Le répertoire de données existant n'est déplacé
# (jamais supprimé) qu'avec --reinitialiser, qui sert aussi de re-seed après
# un pg_rewind impossible. Le slot physique du nœud doit exister sur le
# primaire : configurer-noeud.sh le pose.
#
#   pg_basebackup -X stream -S <slot> -R -c fast
#
# puis primary_conninfo/primary_slot_name réécrits (passfile du compte
# postgres, aucun mot de passe dans la configuration), standby.signal reposé,
# démarrage et contrôle du flux (pg_stat_wal_receiver = streaming).
set -euo pipefail
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
usage: initialiser-standby.sh [--source <nœud>] [--reinitialiser] [nom-du-noeud]

  --source <nœud>   primaire d'où copier les données (défaut : le primaire de
                    noeuds.conf) ; il doit être en service et primaire.
  --reinitialiser   déplace le répertoire de données existant en
                    <données>.remplace-<horodatage> avant la base backup
                    (re-seed) ; sans l'option, un répertoire non vide arrête.
  nom-du-noeud      nœud de noeuds.conf ; par défaut le nom court de l'hôte.

Le serveur local doit être arrêté : ce script ne l'arrête pas lui-même.
Aucun geste réseau n'est fait ici (le réseau privé est #6859).
EOF
}

source_nom=""
reinitialiser=0
nom=""
while (( $# )); do
  case $1 in
    --source)
      shift
      (( $# )) || die "--source attend un nom de nœud"
      source_nom=$1
      ;;
    --reinitialiser) reinitialiser=1 ;;
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
SOURCE_ROLE=${NOEUDS_ROLES[$SOURCE_IDX]}
[[ $SOURCE_NOM != "$NOM_LOCAL" ]] || die "la source et le nœud local sont le même nœud ($NOM_LOCAL)"
[[ $SOURCE_ROLE == primaire ]] \
  || info "ATTENTION : $SOURCE_NOM est standby dans l'inventaire (la cible d'EP8.3) ; son rôle effectif est contrôlé ci-dessous"

log "nœud $NOM_LOCAL ($HOTE_LOCAL), source $SOURCE_NOM ($SOURCE_HOTE)"

[[ -f $PGCONF/pg_hba.conf ]] \
  || die "cluster PostgreSQL $PG_VERSION absent ($PGCONF) : le créer d'abord (pg_createcluster $PG_VERSION main), installation d'EP8.3"
[[ -L $DATA ]] && die "$DATA est un lien symbolique : le répertoire de données doit être réel"

# 1. Le serveur local doit être arrêté ; le répertoire de données n'est
#    déplacé que sur --reinitialiser (jamais supprimé). Le hub, encore
#    primaire au moment de la préparation, se réintègre par reintegrer.sh.
if pg_isready -q 2>/dev/null; then
  die "le serveur local tourne : arrêtez-le d'abord (pg_ctlcluster $PG_VERSION main stop) — le script ne l'arrête pas lui-même"
fi

# 2. pgpass : les entrées replicator/rewind, avant toute connexion distante.
installer_pgpass
verifier_pgpass

# 3. La source doit être en service, primaire, et son slot doit exister.
pg_isready -q -h "$SOURCE_HOTE" \
  || die "source $SOURCE_NOM ($SOURCE_HOTE) injoignable sur le port 5432"
if ! source_recovery=$(psql_rewind "$SOURCE_HOTE" -tAc 'SELECT pg_is_in_recovery()'); then
  die "source $SOURCE_NOM joignable mais son rôle est illisible (rôle rewind, pg_hba, pgpass)"
fi
[[ $source_recovery == f ]] \
  || die "source $SOURCE_NOM est en récupération : une base backup se prend sur un primaire"
slot=$(slot_de "$NOM_LOCAL")
if ! slot_present=$(psql_rewind "$SOURCE_HOTE" -tAc "SELECT 1 FROM pg_replication_slots WHERE slot_name = '$slot'"); then
  die "source $SOURCE_NOM joignable mais la lecture de ses slots a échoué (rôle rewind, pg_hba, pgpass)"
fi
[[ $slot_present == 1 ]] \
  || die "slot $slot absent sur $SOURCE_NOM : rejouer configurer-noeud.sh sur le primaire $SOURCE_NOM"

# 4. Répertoire de données : vide, absent, ou déplacé avec --reinitialiser.
sauvegarde=""
if [[ -d $DATA ]] && [[ -n $(ls -A -- "$DATA" 2>/dev/null || true) ]]; then
  (( reinitialiser )) \
    || die "$DATA n'est pas vide : --reinitialiser le déplace (jamais le supprime) avant la base backup"
  sauvegarde="${DATA}.remplace-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  [[ ! -e $sauvegarde ]] || die "répertoire de sauvegarde déjà présent : $sauvegarde"
  mv -- "$DATA" "$sauvegarde"
  log "ancien répertoire conservé : $sauvegarde"
fi
if [[ ! -d $DATA ]]; then
  # Les parents appartiennent déjà à postgres sur Debian ; on ne change que
  # le répertoire de données, pas leurs droits.
  mkdir -p -- "$(dirname -- "$DATA")"
  install -d -m 0700 -- "$DATA"
  if (( EUID == 0 )); then
    chown postgres:postgres -- "$(dirname -- "$DATA")" || die "chown postgres impossible sur $(dirname -- "$DATA")"
    chown postgres:postgres -- "$DATA" || die "chown postgres impossible sur $DATA"
  fi
elif (( EUID == 0 )); then
  # Un répertoire vide peut avoir été créé par root : postgres doit y écrire.
  chown postgres:postgres -- "$DATA" || die "chown postgres impossible sur $DATA"
fi

# 5. Base backup physique, WAL en flux, slot déjà réservé par le primaire.
#    sslmode=require : TLS imposé par le pg_hba hostssl, sans repli en clair
#    (la vérification du certificat attend l'AC du mesh, #6859).
log "base backup de $SOURCE_NOM ($SOURCE_HOTE) vers $DATA (slot $slot)"
if ! PGSSLMODE=require comme_postgres "$PG_BIN/pg_basebackup" -w -h "$SOURCE_HOTE" -p 5432 -U replicator \
    -D "$DATA" -X stream -S "$slot" -R -c fast -P; then
  die "pg_basebackup a échoué ; $DATA peut être partiel — le déplacer avant de rejouer avec --reinitialiser (ancien répertoire : ${sauvegarde:-aucun})"
fi

# 6. Paramètres de reprise et signal : -R écrit une chaîne que l'on réécrit
#    pour l'application_name, le slot et le passfile (aucun mot de passe).
ecrire_reprise "$SOURCE_HOTE" "$NOM_LOCAL" "$slot"
poser_standby_signal
controler_configuration

# 7. Démarrage et preuve du flux.
log "démarrage du standby $NOM_LOCAL"
pg_ctlcluster "$PG_VERSION" main start
attendre_commande "démarrage de PostgreSQL" pg_isready -q
attendre_valeur "standby en récupération" 'SELECT pg_is_in_recovery()' t
attendre_valeur "flux entrant (pg_stat_wal_receiver)" 'SELECT status FROM pg_stat_wal_receiver' streaming
log "standby $NOM_LOCAL en flux depuis $SOURCE_NOM (slot $slot) — vérifier avec etat.sh sur le primaire"
