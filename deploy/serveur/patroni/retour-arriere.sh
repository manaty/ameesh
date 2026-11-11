#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Retour de Patroni vers l'unité Debian et la réplication manuelle d'EP8.3
# (lot #6861, EP8.4) : l'inverse d'adopter.sh, sans perte de données.
#
#   retour-arriere.sh [--geste-reel] arreter|primaire|standby|etcd [--source <nœud>] [nœud]
#
# Ordre, une phase par appel :
#   1. arreter    sur chaque membre qui n'est pas leader : Patroni s'arrête
#                 et arrête sa base ; le leader continue seul (synchronous_mode
#                 non strict : les COMMIT n'attendent pas) ;
#   2. primaire   sur le leader, en dernier : Patroni s'arrête, la
#                 configuration Debian sauvegardée par adopter.sh revient,
#                 sans liste synchrone (aucun standby n'est encore là), les
#                 rôles et le pgpass d'EP8.3 sont reposés, puis l'unité Debian
#                 redémarre la base en primaire ;
#   3. standby    sur chaque autre nœud : configuration restaurée, puis
#                 replication/suivre-primaire.sh --source <primaire> ;
#   4. etcd       sur les trois nœuds, quand plus aucun Patroni ne tourne.
#
# Ensuite, sur le primaire : replication/configurer-noeud.sh rétablit la
# liste synchrone (ANY 1) dès qu'un candidat est en flux. Les clients gardent
# le DSN multi-hôtes (target_session_attrs=read-write trouve le primaire) ;
# le repasser en mono-hôte est un choix, pas une nécessité.
#
# Sans --geste-reel, simulation : contrôles réels, gestes affichés.
set -euo pipefail
PATRONI_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
. "$PATRONI_DIR/../replication/lib.sh"
. "$PATRONI_DIR/lib-patroni.sh"

usage() {
  cat <<'EOF'
usage: retour-arriere.sh [--geste-reel] <phase> [--source <nœud>] [nom-du-noeud]

Phases, dans l'ordre : arreter (membres non leaders), primaire (le leader),
standby --source <primaire> (les autres), etcd (les trois).

  --geste-reel      exécute les gestes ; sans elle, simulation.
  --source <nœud>   phase standby : le primaire rendu à l'unité Debian.
  nom-du-noeud      nœud de noeuds.conf ; par défaut le nom court de l'hôte.
EOF
}

phase=""
nom=""
source_nom=""
while (( $# )); do
  case $1 in
    --geste-reel) REEL=1 ;;
    --source)
      shift
      (( $# )) || die "--source attend un nom de nœud"
      source_nom=$1
      ;;
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

SAUVEGARDE="$SAUVEGARDES/dernier"
SUIVRE=${AMEESH_PATRONI_SUIVRE:-$PATRONI_DIR/../replication/suivre-primaire.sh}

verifier_sauvegarde() {
  [[ -f $SAUVEGARDE/postgresql.conf && -f $SAUVEGARDE/pg_hba.conf ]] \
    || die "sauvegarde d'adopter.sh absente ou incomplète : $SAUVEGARDE"
  info "sauvegarde : $(readlink -f -- "$SAUVEGARDE")"
}

# Remet la configuration Debian d'avant Patroni. postgresql.auto.conf n'est
# pas restauré : son bloc de reprise visait l'ancien primaire, la phase
# standby le réécrit vers le bon.
restaurer_configuration() {
  local f
  for f in postgresql.conf pg_hba.conf pg_ident.conf; do
    [[ -e $SAUVEGARDE/$f ]] && cp -a -- "$SAUVEGARDE/$f" "$PGCONF/$f"
  done
  if [[ -d $SAUVEGARDE/conf.d ]]; then
    cp -a -- "$SAUVEGARDE/conf.d/." "$PGCONF/conf.d/"
  fi
  rm -f -- "$PGCONF/postgresql.base.conf"
  # B1 de revue #6861 : Patroni a réécrit le pgpass du compte postgres
  # (« <ip>:5432:*:replicator:<mot de passe Patroni> »). Laissée en tête, sa
  # ligne passerait avant celles d'EP8.3 et suivre-primaire.sh s'authentifierait
  # avec le mauvais mot de passe. On remet la copie d'avant Patroni ; sans
  # copie, on retire les lignes génériques de replicator et de postgres.
  if [[ -e $SAUVEGARDE/pgpass ]]; then
    cp -a -- "$SAUVEGARDE/pgpass" "$PGPASS"
  elif [[ -f $PGPASS ]]; then
    local tmp
    tmp=$(mktemp "$(dirname -- "$PGPASS")/.pgpass.XXXXXX") || die "temporaire impossible pour $PGPASS"
    grep -v -E '^[^:]*:[^:]*:\*:(replicator|postgres):' "$PGPASS" > "$tmp" || true
    chmod 0600 -- "$tmp"
    if (( EUID == 0 )); then chown postgres:postgres -- "$tmp"; fi
    mv -f -- "$tmp" "$PGPASS"
  fi
  retirer_reprise_auto_conf
  log "configuration Debian restaurée depuis $(readlink -f -- "$SAUVEGARDE")"
}

# Fragment EP8.3 sans liste synchrone : aucun standby n'est encore en flux,
# une liste non vide ferait attendre chaque COMMIT (B1 de revue #6868).
fragment_sans_synchrone() {
  FORCER_SANS_SYNCHRONE=1 ecrire_fichier "$CONF_FRAGMENT" 0644 "$(FORCER_SANS_SYNCHRONE=1 fragment_conf)"
}

# État d'un membre dans l'API Patroni : 0 atteint, 3 pas atteint. Une API
# illisible (code 2) arrête : « pas leader » et « inconnu » ne se confondent
# pas (B3 de revue #6861).
etat_atteint() {
  local code=0
  aide grappe --env "$PATRONI_ENV" --membre "$1" --etat "$2" >/dev/null || code=$?
  case $code in
    0) return 0 ;;
    3) return 1 ;;
    *) die "API Patroni illisible (code $code) : état de $1 inconnu, on n'agit pas à l'aveugle" ;;
  esac
}

leader_local() {
  etat_atteint "$NOM_LOCAL" leader
}

# Avant de rendre la base à l'unité Debian : le DCS ne désigne pas un autre
# leader, et aucun autre nœud ne sert de base (phase arreter faite partout).
# Sinon, après une bascule, on démarrerait un second primaire (B3 de revue
# #6861).
aucun_autre_serveur() {
  local leader i
  leader=$(cle_dcs leader)
  [[ -z $leader || $leader == "$NOM_LOCAL" ]] \
    || die "le DCS désigne $leader comme leader : la phase primaire se joue sur lui, pas sur $NOM_LOCAL"
  for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
    (( i == IDX_LOCAL )) && continue
    if pg_isready -q -h "${NOEUDS_HOTES[$i]}" 2>/dev/null; then
      die "${NOEUDS_NOMS[$i]} (${NOEUDS_HOTES[$i]}) sert encore une base : retour-arriere.sh arreter sur lui d'abord"
    fi
  done
}

phase_arreter() {
  if ! patroni_actif; then
    log "Patroni déjà arrêté sur $NOM_LOCAL"
    return 0
  fi
  ! leader_local || die "$NOM_LOCAL est leader : il s'arrête en dernier (phase primaire), après les autres membres"
  geste systemctl disable --now patroni.service
}

phase_primaire() {
  charger_env
  verifier_sauvegarde
  aucun_autre_serveur
  if patroni_actif; then
    leader_local || die "$NOM_LOCAL n'est pas leader : phase arreter ici, primaire sur le leader"
    local i membre
    for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
      (( i == IDX_LOCAL )) && continue
      membre=${NOEUDS_NOMS[$i]}
      if etat_atteint "$membre" flux; then
        die "$membre tourne encore sous Patroni : retour-arriere.sh arreter sur lui d'abord (le leader s'arrête en dernier)"
      fi
    done
    geste systemctl disable --now patroni.service
  else
    [[ ! -f $DATA/standby.signal ]] \
      || die "$NOM_LOCAL porte standby.signal : ce n'était pas le leader, phase standby"
  fi
  geste restaurer_configuration
  geste fragment_sans_synchrone
  geste ecrire_start_conf auto
  geste systemctl enable --now "$UNITE_PG"
  (( REEL )) || return 0
  attendre_commande "démarrage de PostgreSQL" pg_isready -q
  attendre_valeur "primaire hors récupération" 'SELECT pg_is_in_recovery()' f
  installer_pgpass
  poser_roles
  log "$NOM_LOCAL primaire sous l'unité Debian ; standbys : retour-arriere.sh --geste-reel standby --source $NOM_LOCAL"
}

phase_standby() {
  [[ -n $source_nom ]] || die "phase standby : --source <primaire> exigé"
  ! patroni_actif || die "Patroni tourne encore sur $NOM_LOCAL : phase arreter d'abord"
  verifier_sauvegarde
  geste restaurer_configuration
  geste poser_standby_signal
  geste ecrire_start_conf auto
  geste systemctl enable "$UNITE_PG"
  geste "$SUIVRE" --source "$source_nom" "$NOM_LOCAL"
}

phase_etcd() {
  ! patroni_actif || die "Patroni tourne encore sur $NOM_LOCAL : etcd s'arrête en dernier"
  geste systemctl disable --now etcd.service
}

case $phase in
  arreter) phase_arreter ;;
  primaire) phase_primaire ;;
  standby) phase_standby ;;
  etcd) phase_etcd ;;
  *) die "phase inconnue : $phase (arreter|primaire|standby|etcd)" ;;
esac
