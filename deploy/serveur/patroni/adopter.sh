#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Passage de la réplication manuelle d'EP8.3 (unité Debian postgresql@17-main,
# deploy/serveur/replication) à etcd + Patroni sur la base existante (lot
# #6861, EP8.4 ; étude docs/design/etudes/replique-base-mesh.md §4).
#
#   adopter.sh [--geste-reel] [--accepter-hba] verifier|etcd|primaire|standby|grappe [nœud]
#
# Une phase par appel, sur le nœud concerné, dans cet ordre :
#   1. verifier   sur les trois nœuds : lecture seule ;
#   2. etcd       sur les trois nœuds, dans la même fenêtre ;
#   3. primaire   sur le primaire EP8.3 : Patroni reprend son data_dir ;
#   4. standby    sur chaque standby, un par un : Patroni le prend en
#                 réplique, sans pg_basebackup (même identifiant système) ;
#   5. grappe     n'importe où : la cible est-elle atteinte ?
#
# Sans --geste-reel, rien ne change : les contrôles tournent et chaque geste
# est affiché « [simulation] ». Avec --geste-reel, chaque phase sauvegarde
# d'abord la configuration Debian du nœud (retour-arriere.sh la relit).
#
# Gardes, en plus de celles d'EP8.3 :
#   - topologie.json (Patroni) et noeuds.conf (EP8.3) décrivent la même
#     grappe ;
#   - les versions validées : etcd 3.5.x, Patroni 4.x ;
#   - aucun paramètre géré par Patroni dans postgresql.auto.conf (il y
#     serait prioritaire) ; le bloc de reprise d'EP8.3 est retiré au passage ;
#   - pg_hba : Patroni réécrit le fichier ; une règle perdue ou un client
#     connecté non couvert arrête, sauf --accepter-hba (décision d'exploitant) ;
#   - slots : Patroni supprime un slot physique qu'il ne connaît pas ;
#   - primaire : pas le hub nofailover (Patroni ne le garderait pas leader),
#     aucun leader dans le DCS, aucun autre primaire joignable ;
#   - standby : un leader dans le DCS, membre de la grappe, avec le même
#     identifiant système que le data_dir local ;
#   - start.conf passe à « manual » : l'unité Debian ne redémarre plus la
#     base au boot à côté de Patroni (deux gestionnaires du même data_dir).
#
# Les mots de passe ne passent jamais en argument : ceux de patroni.env sont
# lus dans le fichier et écrits sur l'entrée standard de psql.
set -euo pipefail
PATRONI_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
. "$PATRONI_DIR/../replication/lib.sh"
. "$PATRONI_DIR/lib-patroni.sh"

usage() {
  cat <<'EOF'
usage: adopter.sh [--geste-reel] [--accepter-hba] <phase> [nom-du-noeud]

Phases, dans l'ordre : verifier (3 nœuds), etcd (3 nœuds), primaire (le
primaire EP8.3), standby (chaque standby, un par un), grappe.

  --geste-reel    exécute les gestes ; sans elle, simulation (contrôles
                  réels, gestes affichés).
  --accepter-hba  poursuit malgré une règle pg_hba perdue ou un client non
                  couvert par le pg_hba de Patroni (décision d'exploitant).
  nom-du-noeud    nœud de noeuds.conf ; par défaut le nom court de l'hôte.
EOF
}

phase=""
nom=""
while (( $# )); do
  case $1 in
    --geste-reel) REEL=1 ;;
    --accepter-hba) ACCEPTER_HBA=1 ;;
    -h | --help) usage; exit 0 ;;
    -*) die "option inconnue : $1 (voir --help)" ;;
    *)
      if [[ -z $phase ]]; then phase=$1
      elif [[ -z $nom ]]; then nom=$1
      else die "trop d'arguments"
      fi
      ;;
  esac
  shift
done
[[ -n $phase ]] || { usage >&2; exit 2; }

lire_noeuds
charger_noeud_local "$nom"
(( REEL )) || log "simulation : aucun geste (--geste-reel pour agir)"

# --- contrôles ----------------------------------------------------------------

verifier_versions() {
  local v
  v=$("$ETCD_BIN" --version 2>/dev/null | sed -n 's/^etcd Version: //p' | head -n1) || true
  [[ $v =~ ^3\.5\. ]] || die "etcd 3.5.x attendu dans $ETCD_BIN, trouvé « ${v:-absent} »"
  info "etcd $v"
  v=$("$PATRONI_BIN" --version 2>/dev/null | awk '{print $2}' | head -n1) || true
  [[ $v =~ ^4\. ]] || die "Patroni 4.x attendu dans $PATRONI_BIN, trouvé « ${v:-absent} »"
  info "Patroni $v"
}

verifier_fichiers() {
  [[ -r $PATRONI_CONF ]] || die "configuration Patroni absente : $PATRONI_CONF (README, Installation, étape 2)"
  [[ -r $PATRONI_ENV ]] || die "environnement Patroni absent : $PATRONI_ENV"
  local mode
  mode=$(stat -c '%a' -- "$PATRONI_ENV")
  [[ $mode == 640 || $mode == 600 ]] || die "$PATRONI_ENV en $mode : 0640 root:postgres attendu (secrets)"
  grep -q "^name: $NOM_LOCAL\$" "$PATRONI_CONF" \
    || die "$PATRONI_CONF n'est pas celui de $NOM_LOCAL (name:)"
  local cle
  for cle in PATRONI_REPLICATION_PASSWORD PATRONI_SUPERUSER_PASSWORD AMEESH_DB_ETCD_ENDPOINTS AMEESH_DB_REST_URL; do
    [[ -n $(valeur_env "$cle") ]] || die "$cle absent de $PATRONI_ENV"
  done
}

verifier_auto_conf() {
  local geres
  geres=$(parametres_geres_auto_conf) || true
  [[ -z $geres ]] || die "postgresql.auto.conf porte des paramètres gérés par Patroni, prioritaires sur les siens : $geres (ALTER SYSTEM RESET d'abord)"
}

verifier_hba() {
  local clients sortie
  clients=$(mktemp "${TMPDIR:-/tmp}/adopter-clients.XXXXXX")
  psql_local -tAc "SELECT DISTINCT host(client_addr) FROM pg_stat_activity WHERE client_addr IS NOT NULL" > "$clients" 2>/dev/null || :
  if sortie=$(aide hba --patroni-yml "$PATRONI_CONF" --hba "$HBA" --clients "$clients"); then
    info "$sortie"
  else
    rm -f -- "$clients"
    printf '%s\n' "$sortie" | sed 's/^/   /'
    (( ACCEPTER_HBA )) || die "pg_hba : Patroni réécrira le fichier sans ces règles (--accepter-hba pour poursuivre, décision d'exploitant)"
    info "ATTENTION : --accepter-hba accordé"
    return 0
  fi
  rm -f -- "$clients"
}

verifier_slots() {
  local sortie
  sortie=$(psql_local -tAc "SELECT slot_name FROM pg_replication_slots WHERE slot_type = 'physical' ORDER BY 1" \
    | aide slots --topologie "$TOPOLOGIE" --local "$NOM_LOCAL") \
    || die "slots : $sortie"
  info "$sortie"
}

verifier_local() {
  log "contrôles de $NOM_LOCAL"
  local sortie
  sortie=$(aide topologie --topologie "$TOPOLOGIE" --noeuds "$NOEUDS") || die "topologie : $sortie"
  info "$sortie"
  verifier_versions
  verifier_fichiers
  if [[ -c $WATCHDOG ]]; then
    info "chien de garde présent ($WATCHDOG) : laissé à « off » tant qu'il n'est pas testé (étude §4)"
  else
    info "aucun chien de garde ($WATCHDOG absent) : la clôture repose sur le bail etcd et le verrou Patroni"
  fi
  if patroni_actif; then
    info "Patroni tourne déjà sur $NOM_LOCAL"
    return 0
  fi
  pg_isready -q 2>/dev/null || die "PostgreSQL ne répond pas sur $NOM_LOCAL : l'adoption reprend une base en service"
  ROLE=$(role_effectif)
  info "rôle effectif : $ROLE ; start.conf : $(cat -- "$START_CONF" 2>/dev/null | grep -v '^#' | tr -d '[:space:]' || echo absent)"
  [[ $(psql_local -tAc 'SHOW wal_log_hints') == on ]] \
    || die "wal_log_hints=off : pg_rewind serait impossible après une bascule (EP8.3 configurer-noeud.sh, puis redémarrage)"
  verifier_auto_conf
  verifier_hba
  [[ $ROLE == primaire ]] && verifier_slots
  return 0
}

# --- gestes -------------------------------------------------------------------

# Prend l'unité Debian hors service et démarre Patroni sur le même data_dir.
passer_sous_patroni() {
  sauvegarder_configuration
  geste retirer_reprise_auto_conf
  geste ecrire_start_conf manual
  geste systemctl disable --now "$UNITE_PG"
  geste systemctl enable --now patroni.service
}

phase_etcd() {
  verifier_versions
  verifier_fichiers
  geste systemctl enable --now etcd.service
  (( REEL )) || return 0
  attendre_commande "quorum etcd (au moins deux membres sains)" etcd_quorum
  log "etcd : quorum atteint"
}

phase_primaire() {
  verifier_local
  if patroni_actif; then
    aide grappe --env "$PATRONI_ENV" --membre "$NOM_LOCAL" --etat leader >/dev/null \
      || die "Patroni tourne sur $NOM_LOCAL mais il n'est pas leader : la phase primaire ne se rejoue pas ici"
    log "$NOM_LOCAL déjà leader sous Patroni : rien à faire"
    return 0
  fi
  [[ $ROLE == primaire ]] || die "$NOM_LOCAL est $ROLE : la phase primaire se joue sur le primaire EP8.3"
  [[ $NOFAIL_LOCAL == non ]] \
    || die "$NOM_LOCAL est nofailover : Patroni ne le garderait pas leader ; promouvoir d'abord ameesh-agents-1 par EP8.3 (replication/promouvoir.sh), puis adopter depuis lui"
  etcd_quorum || die "etcd sans quorum : phase etcd d'abord, sur les trois nœuds"
  local leader
  leader=$(cle_dcs leader)
  [[ -z $leader ]] || die "le DCS a déjà un leader ($leader) : une seule grappe, pas de second amorçage"
  [[ -z $(cle_dcs initialize) ]] || die "le DCS porte déjà une clé initialize sans leader : grappe à examiner (patronictl list), pas de réamorçage aveugle"
  aucun_autre_primaire
  [[ $NOM_LOCAL == "$(topologie_valeur primaire_prefere)" ]] \
    || info "ATTENTION : $NOM_LOCAL n'est pas le primaire préféré ; patronictl switchover après l'adoption"
  log "adoption de $NOM_LOCAL par Patroni (redémarrage bref de la base : les clients se reconnectent par le DSN)"
  passer_sous_patroni
  if (( REEL )); then
    attendre_commande "$NOM_LOCAL leader sous Patroni" aide grappe --env "$PATRONI_ENV" --membre "$NOM_LOCAL" --etat leader
    poser_mots_de_passe_patroni
    log "$NOM_LOCAL leader ; standbys ensuite, un par un : adopter.sh --geste-reel standby"
  fi
}

phase_standby() {
  verifier_local
  if patroni_actif; then
    aide grappe --env "$PATRONI_ENV" --membre "$NOM_LOCAL" --etat flux >/dev/null \
      || die "Patroni tourne sur $NOM_LOCAL sans être en flux : patronictl list"
    log "$NOM_LOCAL déjà réplique sous Patroni : rien à faire"
    return 0
  fi
  [[ $ROLE == standby ]] || die "$NOM_LOCAL est $ROLE : la phase standby se joue sur un standby (deux primaires ne se suivent pas)"
  local leader idx sysid_local sysid_dcs
  leader=$(cle_dcs leader)
  [[ -n $leader ]] || die "aucun leader dans le DCS : phase primaire d'abord"
  indice_de "$leader" || die "leader du DCS « $leader » absent de l'inventaire"
  idx=$IDX
  [[ $leader != "$NOM_LOCAL" ]] || die "le DCS désigne $NOM_LOCAL comme leader alors qu'il est standby"
  aide grappe --env "$PATRONI_ENV" --url "$(url_rest "${NOEUDS_HOTES[$idx]}")" --membre "$leader" --etat leader >/dev/null \
    || die "$leader n'est pas leader « running » dans son API Patroni"
  sysid_dcs=$(cle_dcs initialize)
  sysid_local=$(identifiant_systeme)
  [[ -n $sysid_local && $sysid_local == "$sysid_dcs" ]] \
    || die "identifiant système local « $sysid_local » ≠ grappe « $sysid_dcs » : Patroni réinitialiserait ce data_dir, refus"
  log "adoption de $NOM_LOCAL en réplique de $leader"
  passer_sous_patroni
  if (( REEL )); then
    attendre_commande "$NOM_LOCAL en flux sous Patroni" aide grappe --env "$PATRONI_ENV" --membre "$NOM_LOCAL" --etat flux
    log "$NOM_LOCAL en flux ; grappe : adopter.sh grappe"
  fi
}

phase_grappe() {
  local sortie code=0
  sortie=$(aide grappe --env "$PATRONI_ENV" --topologie "$TOPOLOGIE") || code=$?
  printf '%s\n' "$sortie"
  (( code == 0 )) || exit 1
  log "grappe conforme"
}

case $phase in
  verifier) verifier_local; log "contrôles passés sur $NOM_LOCAL" ;;
  etcd) phase_etcd ;;
  primaire) phase_primaire ;;
  standby) phase_standby ;;
  grappe) phase_grappe ;;
  *) die "phase inconnue : $phase (verifier|etcd|primaire|standby|grappe)" ;;
esac
