#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Fonctions communes des scripts de réplication PostgreSQL 17 du mesh
# (lot #6868, EP8.3a, étude docs/design/etudes/replique-base-mesh.md §2/§3).
#
# Ce fichier est sourcé, jamais exécuté. Il ne fait aucun geste : il lit
# l'inventaire, vérifie les secrets et écrit la configuration.
#
# Les chemins et les délais sont surchargeables par l'environnement pour les
# témoins (tests unitaires sur des faux, aucun hôte réel, aucun réseau) :
#   AMEESH_REPLICATION_ETC      (/etc/ameesh)
#   AMEESH_REPLICATION_ENV      ($ETC/replication.env)
#   AMEESH_REPLICATION_NOEUDS   (noeuds.conf à côté de ce fichier)
#   AMEESH_REPLICATION_PGCONF   (/etc/postgresql/$PG_VERSION/main)
#   AMEESH_REPLICATION_DATA     (/var/lib/postgresql/$PG_VERSION/main)
#   AMEESH_REPLICATION_PGBIN    (/usr/lib/postgresql/$PG_VERSION/bin)
#   AMEESH_REPLICATION_PGPASS   (/var/lib/postgresql/.pgpass)
#   AMEESH_REPLICATION_NOM      (nom court de l'hôte)
#   AMEESH_REPLICATION_ATTENTE  (secondes, 60)
#   AMEESH_REPLICATION_RUNUSER  (auto : oui si root ; « oui » force le témoin)
#
# Aucun secret n'est passé en argument : le mot de passe des rôles est écrit
# sur l'entrée standard de psql, et les connexions de réplication passent par
# le pgpass du compte postgres (0600), jamais par PGPASSWORD ni par une
# chaîne de connexion visible dans « ps ».

REPLICATION_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PG_VERSION=${PG_VERSION:-17}
AMEESH_REPLICATION_ETC=${AMEESH_REPLICATION_ETC:-/etc/ameesh}
ENV_FILE=${AMEESH_REPLICATION_ENV:-$AMEESH_REPLICATION_ETC/replication.env}
NOEUDS=${AMEESH_REPLICATION_NOEUDS:-$REPLICATION_DIR/noeuds.conf}
PGCONF=${AMEESH_REPLICATION_PGCONF:-/etc/postgresql/$PG_VERSION/main}
DATA=${AMEESH_REPLICATION_DATA:-/var/lib/postgresql/$PG_VERSION/main}
PG_BIN=${AMEESH_REPLICATION_PGBIN:-/usr/lib/postgresql/$PG_VERSION/bin}
PGPASS=${AMEESH_REPLICATION_PGPASS:-/var/lib/postgresql/.pgpass}
ATTENTE=${AMEESH_REPLICATION_ATTENTE:-60}

CONF_FRAGMENT="$PGCONF/conf.d/60-ameesh-replication.conf"
HBA="$PGCONF/pg_hba.conf"
HBA_DEBUT='# BEGIN ameesh-replication (lot #6868)'
HBA_FIN='# END ameesh-replication (lot #6868)'
REPRISE_DEBUT='# BEGIN ameesh-replication (lot #6868)'

# Le pgpass du compte postgres est le seul porteur du mot de passe des rôles
# de réplication sur un nœud ; PGPASSFILE le rend indépendant du HOME que
# runuser laisse à root.
export PGPASSFILE=$PGPASS
export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-10}

log() { printf '== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die() { printf 'ERREUR : %s\n' "$*" >&2; exit 1; }

# --- secrets et inventaire ----------------------------------------------------

# Refuse un fichier de secrets lisible ou modifiable par un autre compte que
# son propriétaire (0600 ou 0400), et un lien symbolique.
verifier_secret() {
  local fichier=$1 mode
  [[ -f $fichier ]] || die "$fichier est absent (voir le README du répertoire)"
  [[ -L $fichier ]] && die "$fichier ne doit pas être un lien symbolique"
  mode=$(stat -c '%a' -- "$fichier") || die "mode de $fichier illisible"
  if (( (8#$mode & 8#077) != 0 )); then
    die "$fichier doit être en 0600 (il est en 0$mode) : chmod 0600 $fichier"
  fi
}

# Les mots de passe sont générés en base64 sans « / » ni « + » par le README ;
# le jeu de caractères exclut « : » et « \ » pour que le pgpass n'ait rien à
# échapper, et tout blanc pour qu'aucune ligne de commande ne les coupe.
verifier_mot_de_passe() {
  local nom=$1 valeur=${!1:-}
  [[ ${#valeur} -ge 24 ]] || die "$nom : 24 caractères au minimum (mot de passe généré, jamais choisi)"
  [[ $valeur =~ ^[A-Za-z0-9+/=._~!@%+-]+$ ]] \
    || die "$nom : caractères autorisés A-Z a-z 0-9 + / = . _ ~ ! @ % + - (ni espace, ni deux-points, ni guillemet)"
}

# Un CIDR invalide dans pg_hba empêche le serveur de redémarrer : on le
# valide avant d'écrire. python3 est présent sur les hôtes du mesh (il sert
# au paquet ameesh) ; sans lui, la forme est contrôlée par expression.
verifier_cidr() {
  local cidr=$1
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$cidr" <<'PY' || die "REPLICATION_SUBNET doit être un CIDR valide : « $cidr »"
import ipaddress
import sys
try:
    ipaddress.ip_network(sys.argv[1], strict=False)
except ValueError:
    sys.exit(1)
PY
    return 0
  fi
  [[ $cidr =~ ^[0-9A-Fa-f:.]+/[0-9]{1,3}$ ]] \
    || die "REPLICATION_SUBNET doit être un CIDR (ex. 10.77.0.0/24), pas « $cidr »"
}

charger_env() {
  verifier_secret "$ENV_FILE"
  # Le fichier est root:root 0600 : il peut être sourcé comme un fichier de
  # configuration du serveur (même pratique que /etc/ameesh/db.env).
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  [[ -n ${REPLICATION_SUBNET:-} ]] \
    || die "REPLICATION_SUBNET manquant dans $ENV_FILE (réseau privé, #6859)"
  verifier_cidr "$REPLICATION_SUBNET"
  verifier_mot_de_passe REPLICATOR_PASSWORD
  verifier_mot_de_passe REWIND_PASSWORD
  WAL_KEEP_SIZE=${WAL_KEEP_SIZE:-1GB}
  MAX_SLOT_WAL_KEEP_SIZE=${MAX_SLOT_WAL_KEEP_SIZE:-4GB}
  [[ $WAL_KEEP_SIZE =~ ^[0-9]+(kB|MB|GB|TB)$ ]] \
    || die "WAL_KEEP_SIZE invalide : « $WAL_KEEP_SIZE » (ex. 1GB)"
  [[ $MAX_SLOT_WAL_KEEP_SIZE =~ ^[0-9]+(kB|MB|GB|TB)$ ]] \
    || die "MAX_SLOT_WAL_KEEP_SIZE invalide : « $MAX_SLOT_WAL_KEEP_SIZE » (ex. 4GB)"
}

# Inventaire : une ligne par nœud, six champs, « # » commente.
#   nom  hote  role  synchrone  nofailover  priorite
# Le rôle de l'inventaire est la cible ; à l'exécution, le rôle effectif est
# celui que le serveur local annonce (pg_is_in_recovery), pour couvrir la
# bascule du hub (encore primaire au moment de la préparation).
lire_noeuds() {
  NOEUDS_NOMS=()
  NOEUDS_HOTES=()
  NOEUDS_ROLES=()
  NOEUDS_SYNC=()
  NOEUDS_NOFAIL=()
  NOEUDS_PRIOS=()
  [[ -r $NOEUDS ]] || die "inventaire illisible : $NOEUDS"
  local ligne nom hote role sync nofail prio extra i primaires=0 syncs=0
  while IFS= read -r ligne || [[ -n $ligne ]]; do
    ligne=${ligne%%#*}
    [[ -z ${ligne//[[:space:]]/} ]] && continue
    nom="" hote="" role="" sync="" nofail="" prio="" extra=""
    read -r nom hote role sync nofail prio extra <<< "$ligne"
    [[ -n $nom && -n $hote && -n $role && -n $sync && -n $nofail && -n $prio ]] \
      || die "inventaire : ligne incomplète « $ligne »"
    [[ -z $extra ]] || die "inventaire : trop de champs sur « $ligne »"
    [[ $nom =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || die "inventaire : nom invalide « $nom »"
    [[ $hote =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ ]] || die "inventaire : hôte invalide « $hote »"
    [[ $role == primaire || $role == standby ]] \
      || die "inventaire : rôle de $nom inconnu « $role » (primaire|standby)"
    [[ $sync == oui || $sync == non ]] \
      || die "inventaire : synchrone de $nom inconnu « $sync » (oui|non)"
    [[ $nofail == oui || $nofail == non ]] \
      || die "inventaire : nofailover de $nom inconnu « $nofail » (oui|non)"
    [[ $prio =~ ^[0-9]{1,4}$ ]] || die "inventaire : priorité de $nom invalide « $prio »"
    [[ $nofail != oui || $sync != oui ]] \
      || die "inventaire : $nom est nofailover et synchrone — un nœud nofailover n'entre jamais dans synchronous_standby_names"
    for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
      [[ ${NOEUDS_NOMS[$i]} != "$nom" ]] || die "inventaire : nœud $nom en double"
    done
    [[ $role == primaire ]] && primaires=$((primaires + 1))
    [[ $sync == oui ]] && syncs=$((syncs + 1))
    NOEUDS_NOMS+=("$nom")
    NOEUDS_HOTES+=("$hote")
    NOEUDS_ROLES+=("$role")
    NOEUDS_SYNC+=("$sync")
    NOEUDS_NOFAIL+=("$nofail")
    NOEUDS_PRIOS+=("$prio")
  done < "$NOEUDS"
  (( ${#NOEUDS_NOMS[@]} >= 2 )) || die "inventaire : au moins deux nœuds"
  (( primaires == 1 )) || die "inventaire : un seul primaire attendu, $primaires trouvé(s)"
  (( syncs >= 2 )) || die "inventaire : au moins deux candidats synchrones (la paire d'agents)"
  for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
    [[ ${NOEUDS_ROLES[$i]} != primaire || ${NOEUDS_SYNC[$i]} == oui ]] \
      || die "inventaire : le primaire ${NOEUDS_NOMS[$i]} doit être un candidat synchrone"
  done
}

# Indice d'un nœud dans les tableaux, sans toucher aux globales du nœud local.
indice_de() {
  local recherche=$1 i
  for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
    if [[ ${NOEUDS_NOMS[$i]} == "$recherche" ]]; then
      IDX=$i
      return 0
    fi
  done
  return 1
}

# Renseigne les globales du nœud cherché ; échoue sans quitter.
trouver_noeud() {
  indice_de "$1" || return 1
  IDX_LOCAL=$IDX
  NOM_LOCAL=${NOEUDS_NOMS[$IDX]}
  HOTE_LOCAL=${NOEUDS_HOTES[$IDX]}
  ROLE_INVENTAIRE=${NOEUDS_ROLES[$IDX]}
  SYNC_LOCAL=${NOEUDS_SYNC[$IDX]}
  NOFAIL_LOCAL=${NOEUDS_NOFAIL[$IDX]}
  PRIO_LOCAL=${NOEUDS_PRIOS[$IDX]}
  return 0
}

# Nom local : l'argument du script s'il est donné, sinon le nom court de
# l'hôte. Il doit figurer dans l'inventaire, sans quoi les slots et le
# primary_conninfo seraient faux.
charger_noeud_local() {
  local demande=${1:-}
  local nom=${demande:-${AMEESH_REPLICATION_NOM:-$(hostname -s)}}
  if ! trouver_noeud "$nom"; then
    die "nœud « $nom » absent de l'inventaire $NOEUDS (noms : ${NOEUDS_NOMS[*]})"
  fi
}

# Renseigne PRIMAIRE_IDX/PRIMAIRE_NOM/PRIMAIRE_HOTE (le primaire de
# l'inventaire, c'est-à-dire la cible, pas nécessairement le primaire actuel).
trouver_primaire() {
  local i
  for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
    if [[ ${NOEUDS_ROLES[$i]} == primaire ]]; then
      PRIMAIRE_IDX=$i
      PRIMAIRE_NOM=${NOEUDS_NOMS[$i]}
      PRIMAIRE_HOTE=${NOEUDS_HOTES[$i]}
      return 0
    fi
  done
  die "inventaire : aucun primaire"
}

# Liste des noms synchrones, au format de synchronous_standby_names.
liste_sync() {
  local noms=() i joint
  for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
    [[ ${NOEUDS_SYNC[$i]} == oui ]] && noms+=("\"${NOEUDS_NOMS[$i]}\"")
  done
  (( ${#noms[@]} >= 2 )) || die "inventaire : moins de deux candidats synchrones"
  printf -v joint '%s, ' "${noms[@]}"
  printf 'ANY 1 (%s)' "${joint%, }"
}

# Candidats synchrones de l'inventaire réellement en flux, vus du primaire
# local. Vide si le serveur ne répond pas, n'est pas primaire, ou n'a aucun
# candidat en streaming : c'est l'état d'amorçage. La liste synchrone ne doit
# être posée que si ce résultat est non vide (B1 de revue #6868) : avec
# PostgreSQL seul, une liste non vide sans standby connecté fait attendre
# chaque COMMIT indéfiniment (synchronous_mode_strict=false est une propriété
# de Patroni, pas du serveur).
sync_candidats_en_flux() {
  local rec valeurs="" nom i liste
  pg_isready -q 2>/dev/null || return 0
  rec=$(psql_local -tAc 'SELECT pg_is_in_recovery()' 2>/dev/null) || return 0
  [[ $rec == f ]] || return 0
  for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
    [[ ${NOEUDS_SYNC[$i]} == oui ]] || continue
    nom=${NOEUDS_NOMS[$i]}
    valeurs+="${valeurs:+,}$(litteral_sql "$nom")"
  done
  [[ -n $valeurs ]] || return 0
  liste=$(psql_local -tAc "SELECT application_name FROM pg_stat_replication WHERE state = 'streaming' AND application_name IN ($valeurs) ORDER BY application_name" 2>/dev/null) || return 0
  printf '%s' "$liste"
}

# Vrai si la liste synchrone doit être posée : un candidat est en flux et
# l'exploitant n'a pas demandé la dégradation (--sans-synchrone).
sync_actif() {
  (( ${FORCER_SANS_SYNCHRONE:-0} )) && return 1
  [[ -n $(sync_candidats_en_flux) ]]
}

# Nom de slot physique d'un nœud : stable, sans tiret (les tirets sont
# autorisés dans un nom de slot, mais un identifiant SQL simple évite tout
# échappement dans les requêtes générées).
slot_de() {
  local nom=$1
  printf '%s' "${nom//[-.]/_}"
}

# --- exécution ----------------------------------------------------------------

# Le serveur doit tourner sous postgres ; les scripts tournent en root (comme
# installer-serveur.sh) et passent par runuser. Le témoin force le chemin
# production avec AMEESH_REPLICATION_RUNUSER=oui et une doublure de runuser.
comme_postgres() {
  local mode=${AMEESH_REPLICATION_RUNUSER:-auto}
  if [[ $mode == oui ]] || { [[ $mode == auto ]] && (( EUID == 0 )); }; then
    runuser -u postgres -- "$@"
  else
    "$@"
  fi
}

psql_local() {
  comme_postgres psql -X -w -v ON_ERROR_STOP=1 -q -d postgres "$@"
}

# Connexion normale (pas de réplication) au nom de rewind, pour pg_rewind et
# le contrôle d'absence de second primaire ; le mot de passe vient du pgpass.
# sslmode=require : le pg_hba hostssl impose déjà TLS, on interdit tout
# repli en clair ; la vérification du certificat (verify-full) attend l'AC du
# mesh (#6859, étude §8) et reste une limite connue, non bloquante.
psql_rewind() {
  local hote=$1
  shift
  PGSSLMODE=require comme_postgres psql -X -w -v ON_ERROR_STOP=1 -q -h "$hote" -p 5432 -U rewind -d postgres "$@"
}

# Rôle effectif du serveur local, ou rôle de l'inventaire s'il ne répond pas
# (nœud neuf, préparation avant installation).
role_effectif() {
  local rec
  if pg_isready -q 2>/dev/null; then
    if ! rec=$(psql_local -tAc 'SELECT pg_is_in_recovery()'); then
      die "serveur joignable mais pg_is_in_recovery() illisible"
    fi
    if [[ $rec == t ]]; then printf 'standby'; else printf 'primaire'; fi
    return 0
  fi
  printf '%s' "$ROLE_INVENTAIRE"
}

# --- écriture de configuration ------------------------------------------------

# Écrit un fichier de configuration atomiquement (temporaire dans le même
# répertoire, puis renommage). Le contenu est passé en troisième argument.
ecrire_fichier() {
  local chemin=$1 mode=$2 contenu=$3 dossier tmp
  dossier=$(dirname -- "$chemin")
  [[ -d $dossier ]] || install -d -m 0755 -- "$dossier"
  tmp=$(mktemp "$dossier/.ameesh-replication.XXXXXX") || die "temporaire impossible dans $dossier"
  printf '%s\n' "$contenu" > "$tmp"
  chmod "$mode" -- "$tmp"
  if (( EUID == 0 )); then
    chown postgres:postgres -- "$tmp" || die "chown postgres impossible sur $tmp"
  fi
  mv -f -- "$tmp" "$chemin"
}

fragment_conf() {
  local sync=""
  # B1 de revue #6868 : la liste n'est posée que si un candidat est déjà en
  # flux. À l'amorçage (aucun standby), elle reste vide pour que les COMMIT
  # (dont les CREATE/ALTER ROLE et les slots) ne s'attendent pas.
  if sync_actif; then
    sync=$(liste_sync)
  fi
  cat <<CONF
# BEGIN ameesh-replication (lot #6868) — géré par deploy/serveur/replication/configurer-noeud.sh
# Réplication en flux PostgreSQL 17 (EP8.3) ; ne pas éditer à la main.
wal_level = replica
max_wal_senders = 10
max_replication_slots = 10
wal_log_hints = on
full_page_writes = on
hot_standby = on
wal_keep_size = '$WAL_KEEP_SIZE'
max_slot_wal_keep_size = '$MAX_SLOT_WAL_KEEP_SIZE'
synchronous_commit = on
synchronous_standby_names = '$sync'
password_encryption = 'scram-sha-256'
# END ameesh-replication (lot #6868)
CONF
}

bloc_hba() {
  cat <<CONF
$HBA_DEBUT
# Géré par deploy/serveur/replication/configurer-noeud.sh (lot #6868).
# Réplication physique (replicator) et pg_rewind (rewind) sur le réseau privé,
# TLS obligatoire et scram-sha-256 ; l'accès client « ameesh » reste posé par
# installer-serveur.sh (bloc ameesh-mesh).
hostssl replication replicator $REPLICATION_SUBNET scram-sha-256
hostssl postgres    rewind     $REPLICATION_SUBNET scram-sha-256
$HBA_FIN
CONF
}

# Remplace le bloc marqué de pg_hba.conf, en conserve les droits et le
# propriétaire, et n'ajoute qu'un bloc (rejouable).
remplacer_bloc_hba() {
  local fichier=$HBA contenu=$1 tmp
  [[ -f $fichier ]] || die "pg_hba.conf introuvable : $fichier"
  tmp=$(mktemp "$(dirname -- "$fichier")/.hba.XXXXXX") || die "temporaire impossible pour $fichier"
  awk -v debut="$HBA_DEBUT" -v fin="$HBA_FIN" '
    $0 == debut { dedans = 1; next }
    $0 == fin   { dedans = 0; next }
    !dedans     { print }
  ' "$fichier" > "$tmp"
  printf '%s\n' "$contenu" >> "$tmp"
  chmod --reference="$fichier" -- "$tmp"
  if (( EUID == 0 )); then
    chown --reference="$fichier" -- "$tmp" || die "chown impossible sur $tmp"
  fi
  mv -f -- "$tmp" "$fichier"
}

# Retire le bloc marqué ; ne réécrit rien s'il est absent (rejouable).
retirer_bloc_hba() {
  local fichier=$HBA tmp
  [[ -f $fichier ]] || return 0
  grep -qxF -- "$HBA_DEBUT" "$fichier" || return 0
  tmp=$(mktemp "$(dirname -- "$fichier")/.hba.XXXXXX") || die "temporaire impossible pour $fichier"
  awk -v debut="$HBA_DEBUT" -v fin="$HBA_FIN" '
    $0 == debut { dedans = 1; next }
    $0 == fin   { dedans = 0; next }
    !dedans     { print }
  ' "$fichier" > "$tmp"
  chmod --reference="$fichier" -- "$tmp"
  if (( EUID == 0 )); then
    chown --reference="$fichier" -- "$tmp" || die "chown impossible sur $tmp"
  fi
  mv -f -- "$tmp" "$fichier"
}

# Écrit les paramètres de reprise (primary_conninfo, primary_slot_name) dans
# postgresql.auto.conf, qui est lu après conf.d : un paramètre laissé par
# pg_basebackup -R ou copié par pg_rewind serait prioritaire. Les lignes
# existantes de ces deux paramètres sont retirées, puis les nôtres ajoutées.
# Aucun mot de passe : le pgpass de postgres porte le secret.
ecrire_reprise() {
  local hote=$1 nom=$2 slot=$3 fichier="$DATA/postgresql.auto.conf" tmp
  local passfile
  passfile=$(printf '%s' "$PGPASS" | sed 's/\\/\\\\/g; s/'"'"'/\\'"'"'/g')
  [[ -d $DATA ]] || die "répertoire de données absent : $DATA"
  [[ -f $fichier ]] || : > "$fichier"
  tmp=$(mktemp "$DATA/.auto.XXXXXX") || die "temporaire impossible dans $DATA"
  # Retire le bloc de reprise précédent (marqueurs compris) et toute ligne
  # primary_conninfo / primary_slot_name laissée par -R ou par la source du
  # pg_rewind ; le reste de auto.conf (ALTER SYSTEM) est conservé.
  awk -v debut="$REPRISE_DEBUT" -v fin="$HBA_FIN" '
    $0 == debut { dedans = 1; next }
    $0 == fin   { dedans = 0; next }
    dedans      { next }
    !/^[[:space:]]*(primary_conninfo|primary_slot_name)[[:space:]]*=/ { print }
  ' "$fichier" > "$tmp"
  cat >> "$tmp" <<CONF
$REPRISE_DEBUT
# Paramètres de reprise gérés par deploy/serveur/replication (lot #6868).
primary_conninfo = 'host=$hote port=5432 user=replicator application_name=$nom connect_timeout=10 passfile=$passfile sslmode=require keepalives=1 keepalives_idle=30 keepalives_interval=10 keepalives_count=3'
primary_slot_name = '$slot'
$HBA_FIN
CONF
  chmod 0600 -- "$tmp"
  if (( EUID == 0 )); then
    chown postgres:postgres -- "$tmp" || die "chown postgres impossible sur $tmp"
  fi
  mv -f -- "$tmp" "$fichier"
}

# Le signal de standby est un fichier vide ; pg_rewind copie celui de la
# source (un primaire n'en a pas), donc on le repose après.
poser_standby_signal() {
  : > "$DATA/standby.signal"
  chmod 0600 -- "$DATA/standby.signal"
  if (( EUID == 0 )); then
    chown postgres:postgres -- "$DATA/standby.signal" || die "chown postgres impossible sur standby.signal"
  fi
}

# Écrit les deux entrées du pgpass du compte postgres. Les autres lignes sont
# conservées ; les précédentes entrées de ces deux rôles sont remplacées, pour
# qu'une rotation de mot de passe soit rejouable.
installer_pgpass() {
  local dossier tmp
  dossier=$(dirname -- "$PGPASS")
  [[ -d $dossier ]] || install -d -m 0700 -- "$dossier"
  tmp=$(mktemp "$dossier/.pgpass.XXXXXX") || die "temporaire impossible dans $dossier"
  if [[ -f $PGPASS ]]; then
    grep -v -E '^[^:]*:5432:(replication:replicator|postgres:rewind):' "$PGPASS" > "$tmp" || true
  fi
  printf '*:5432:replication:replicator:%s\n' "$REPLICATOR_PASSWORD" >> "$tmp"
  printf '*:5432:postgres:rewind:%s\n' "$REWIND_PASSWORD" >> "$tmp"
  chmod 0600 -- "$tmp"
  if (( EUID == 0 )); then
    chown postgres:postgres -- "$tmp" || die "chown postgres impossible sur $tmp"
  fi
  mv -f -- "$tmp" "$PGPASS"
  log "pgpass : entrées replicator et rewind posées dans $PGPASS (0600)"
}

verifier_pgpass() {
  verifier_secret "$PGPASS"
  grep -q ':replication:replicator:' "$PGPASS" || die "$PGPASS ne porte pas l'entrée replication:replicator"
  grep -q ':postgres:rewind:' "$PGPASS" || die "$PGPASS ne porte pas l'entrée postgres:rewind"
  if (( EUID == 0 )); then
    [[ $(stat -c '%U' -- "$PGPASS") == postgres ]] \
      || die "$PGPASS doit appartenir à postgres (le serveur le lit)"
  fi
}

# --- attentes bornées ---------------------------------------------------------

# Attend qu'une commande réussisse (pg_isready) ; au plus ATTENTE secondes.
attendre_commande() {
  local description=$1 commande=$2 essais i
  shift 2
  essais=$(( ATTENTE / 2 ))
  (( essais > 0 )) || essais=1
  for ((i = 0; i < essais; i++)); do
    if "$commande" "$@" >/dev/null 2>&1; then
      return 0
    fi
    (( i + 1 < essais )) && sleep 2
  done
  die "$description : toujours faux après ${ATTENTE}s"
}

# Attend qu'une requête psql rende la valeur attendue ; au plus ATTENTE s.
attendre_valeur() {
  local description=$1 requete=$2 attendu=$3 valeur essais i
  essais=$(( ATTENTE / 2 ))
  (( essais > 0 )) || essais=1
  for ((i = 0; i < essais; i++)); do
    if valeur=$(psql_local -tAc "$requete" 2>/dev/null) && [[ $valeur == "$attendu" ]]; then
      return 0
    fi
    (( i + 1 < essais )) && sleep 2
  done
  die "$description : « ${valeur:-vide} » au lieu de « $attendu » après ${ATTENTE}s"
}

# --- gestes SQL ---------------------------------------------------------------

litteral_sql() {
  local valeur=$1
  printf "'%s'" "${valeur//\'/\'\'}"
}

# Rôles replicator et rewind : moindre privilège, mots de passe posés sur
# l'entrée standard de psql (jamais en argument), grants exigés par pg_rewind.
poser_roles() {
  log "rôles replicator et rewind (moindre privilège, pg_rewind)"
  {
    cat "$REPLICATION_DIR/sql/roles.sql"
    printf 'ALTER ROLE replicator PASSWORD %s;\n' "$(litteral_sql "$REPLICATOR_PASSWORD")"
    printf 'ALTER ROLE rewind PASSWORD %s;\n' "$(litteral_sql "$REWIND_PASSWORD")"
  } | psql_local
}

# Crée sur le primaire le slot de chaque autre nœud, réservé immédiatement
# pour couvrir la fenêtre entre la base backup et le premier flux. Rejouable.
poser_slots() {
  local i nom slot
  for ((i = 0; i < ${#NOEUDS_NOMS[@]}; i++)); do
    (( i == IDX_LOCAL )) && continue
    nom=${NOEUDS_NOMS[$i]}
    slot=$(slot_de "$nom")
    psql_local -tAc "SELECT pg_create_physical_replication_slot('$slot', true) WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = '$slot');" >/dev/null
    log "slot $slot pour $nom (réservé ; un slot inactif retient du WAL jusqu'à max_slot_wal_keep_size)"
  done
}

# Signale les paramètres qui n'entrent en vigueur qu'au redémarrage.
parametres_hors_rechargement() {
  local couple param attendu valeur manquants=0
  for couple in 'wal_level:replica' 'wal_log_hints:on' 'max_wal_senders:10' 'max_replication_slots:10' 'full_page_writes:on'; do
    param=${couple%%:*}
    attendu=${couple#*:}
    if ! valeur=$(psql_local -tAc "SHOW $param"); then
      die "SHOW $param illisible"
    fi
    if [[ $valeur != "$attendu" ]]; then
      info "$param = ${valeur:-vide} (attendu $attendu)"
      manquants=1
    fi
  done
  return "$manquants"
}

# Contrôle que le fichier de configuration généré est lisible par postgres,
# sans démarrer le serveur. Ignoré sur un nœud sans données ou sans binaire.
controler_configuration() {
  [[ -x $PG_BIN/postgres && -f $DATA/PG_VERSION ]] || return 0
  comme_postgres "$PG_BIN/postgres" -D "$DATA" -c config_file="$PGCONF/postgresql.conf" -C wal_level >/dev/null \
    || die "postgresql.conf invalide (voir ci-dessus) — la configuration n'a pas été appliquée"
}

# Avertissements de préparation, jamais bloquants : le réseau (#6859) et TLS
# sont des prérequis d'un autre lot.
avertir_prerequis() {
  local ssl ecoute
  if pg_isready -q 2>/dev/null; then
    ssl=$(psql_local -tAc 'SHOW ssl' || true)
    [[ $ssl == on ]] || info "ATTENTION : ssl=$ssl ; les lignes hostssl de pg_hba ne serviront pas (installer-serveur.sh pose ssl=on)"
    ecoute=$(psql_local -tAc 'SHOW listen_addresses' || true)
    if [[ $ecoute != '*' ]] && [[ ",$ecoute," != *",$HOTE_LOCAL,"* ]]; then
      info "ATTENTION : listen_addresses=$ecoute ne contient pas $HOTE_LOCAL ; la réplication entrante échouera tant que le réseau (#6859) ne l'aura pas posé"
    fi
  fi
}
