#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Prépare un nœud PostgreSQL 17 pour la réplication en flux du mesh
# (lot #6868, EP8.3a, étude docs/design/etudes/replique-base-mesh.md §2/§3).
#
#   configurer-noeud.sh [--redemarrer] [nom-du-noeud]
#
# Idempotent, sans geste réseau ni redémarrage implicite :
#   - écrit deploy/serveur/replication/60-ameesh-replication.conf dans conf.d
#     (wal_level, wal_log_hints, hot_standby, slots, synchrone ANY 1) ;
#   - remplace le bloc marqué de pg_hba.conf (replicator et rewind, hostssl) ;
#   - recharge le serveur s'il tourne (pg_hba et paramètres rechargeables) ;
#   - signale les paramètres qui exigent un redémarrage (wal_log_hints, …) ;
#     --redemarrer le fait, sinon la commande est imprimée ;
#   - sur le primaire effectif (pg_is_in_recovery = f), pose les rôles
#     replicator et rewind et un slot physique par autre nœud.
#
# Le hub est encore primaire au moment de la préparation : le rôle effectif
# est lu au serveur, pas seulement dans l'inventaire.
#
# Aucun secret en argument : les mots de passe viennent de
# /etc/ameesh/replication.env (0600) et passent par l'entrée standard de psql.
set -euo pipefail
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
usage: configurer-noeud.sh [--redemarrer] [nom-du-noeud]

  --redemarrer   redémarre PostgreSQL si des paramètres (wal_log_hints,
                 wal_level, max_wal_senders, max_replication_slots) ne
                 s'appliquent qu'au redémarrage ; sans l'option, la commande
                 est seulement imprimée.
  nom-du-noeud   nœud de noeuds.conf ; par défaut le nom court de l'hôte.

Le script est idempotent. Il n'ouvre aucun port, ne touche ni au réseau
(#6859) ni à l'archivage (#6858), et ne démarre pas un serveur arrêté.
EOF
}

redemarrer=0
nom=""
while (( $# )); do
  case $1 in
    --redemarrer) redemarrer=1 ;;
    -h | --help) usage; exit 0 ;;
    --) shift; break ;;
    -*) die "option inconnue : $1 (voir --help)" ;;
    *)
      [[ -z $nom ]] || die "un seul nom de nœud attendu"
      nom=$1
      ;;
  esac
  shift
done
while (( $# )); do
  [[ -z $nom ]] || die "un seul nom de nœud attendu"
  nom=$1
  shift
done

charger_env
lire_noeuds
trouver_primaire
charger_noeud_local "$nom"

log "nœud $NOM_LOCAL ($HOTE_LOCAL) — inventaire : $ROLE_INVENTAIRE, synchrone=$SYNC_LOCAL, nofailover=$NOFAIL_LOCAL"
log "synchrones de l'inventaire : $(liste_sync)"

# 1. Configuration commune : fragment conf.d, bloc pg_hba, contrôle de lecture.
ecrire_fichier "$CONF_FRAGMENT" 0644 "$(fragment_conf)"
remplacer_bloc_hba "$(bloc_hba)"
log "configuration écrite : $CONF_FRAGMENT et bloc de $HBA"
controler_configuration

# 2. Rechargement si le serveur tourne ; redémarrage seulement sur demande.
if pg_isready -q 2>/dev/null; then
  pg_ctlcluster "$PG_VERSION" main reload
  log "configuration rechargée (pg_ctlcluster $PG_VERSION main reload)"
  avertir_prerequis
  if ! parametres_hors_rechargement; then
    if (( redemarrer )); then
      log "redémarrage demandé (--redemarrer)"
      pg_ctlcluster "$PG_VERSION" main restart
      attendre_commande "redémarrage de PostgreSQL" pg_isready -q
      parametres_hors_rechargement \
        || die "paramètres toujours hors cible après redémarrage (voir ci-dessus)"
      log "redémarrage terminé"
    else
      info "un REDÉMARRAGE est requis pour appliquer les paramètres ci-dessus :"
      info "  pg_ctlcluster $PG_VERSION main restart"
      info "  (ou rejouer ce script avec --redemarrer)"
    fi
  fi
else
  info "serveur arrêté : configuration posée ; rôles et slots seront posés au premier passage d'un serveur en service"
fi

# 3. SQL : uniquement sur le primaire effectif, serveur en service.
role=$(role_effectif)
if [[ $role == primaire ]] && pg_isready -q 2>/dev/null; then
  poser_roles
  poser_slots
  log "nœud primaire effectif : rôles et slots assurés"
elif [[ $role == primaire ]]; then
  info "primaire d'inventaire mais serveur arrêté : rôles et slots non posés (à rejouer serveur en service)"
else
  info "nœud standby effectif : les rôles se répliquent depuis le primaire ; le flux se pose avec initialiser-standby.sh ou reintegrer.sh"
fi

log "terminé — $NOM_LOCAL configuré ; aucun geste réseau, aucun redémarrage implicite"
