#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Mise à jour d'ameesh vers L60–L74, côté POSTE, étape par étape. Procédure
# complète et raisons : docs/BASCULE.md, « Mise à jour vers L60–L74 ».
#
#   deploy/mise-a-jour/poste.sh <étape> [REF]
#
#   verifier     contrôles, rien n'est modifié ; affiche les bases visées
#   sauvegarder  pg_dump de la base du mesh (sur la VM) et de l'ancienne base
#   migrer       migrations de REF, lancées depuis son instantané AVANT de
#                l'installer : base du mesh (UNE fois), puis ancienne base ;
#                refusé si l'une n'est pas additive (voir MIGRER_APRES)
#   installer    instantané du code REF dans ~/.local/share/ameesh/src-<sha>,
#                installé dans le venv (dépendance psycopg comprise) ; refusé
#                tant qu'une base n'a pas les migrations de REF
#   role         rôle superviseur réappliqué, sur chaque base où il existe
#   redemarrer   exécuteurs du poste un par un, chacun hors tour, puis notify,
#                puis les exécuteurs de l'ancienne base
#   controler    doctor, budget, accounts list, projects, alerts
#   retour       réinstalle la version précédente et redémarre (sans toucher
#                aux bases : 0041 à 0047 sont additives)
#
# REF (migrer, installer) : commit, branche ou étiquette du dépôt qui contient
# ce script ; défaut : HEAD de ce dépôt. MIGRER_APRES=1 (installer) : ancien
# ordre, pour une migration non additive (docs/BASCULE.md, L151).
#
# Paramètres lus dans $BASCULE_ENV (défaut ~/.config/ameesh/bascule.env, celui
# de la bascule L57, jamais dans le dépôt) : VM (adresse WireGuard du serveur),
# UNITES_RESTE (exécuteurs du poste qui servent l'ancienne base).
#
# Aucun secret n'est affiché ni écrit : les mots de passe viennent de
# ~/.pgpass, et les DSN sont affichés sans eux.
set -euo pipefail

# Jamais la base d'un environnement hérité : une session lancée par
# `ameesh attach` garde l'ancien AMEESH_DSN (piège noté lors de L57).
unset AMEESH_DSN AGENT_MESH_DSN

BASCULE_ENV=${BASCULE_ENV:-$HOME/.config/ameesh/bascule.env}
[ -r "$BASCULE_ENV" ] || { echo "paramètres absents : $BASCULE_ENV" >&2; exit 2; }
# shellcheck disable=SC1090
. "$BASCULE_ENV"
: "${VM:?VM manquant dans $BASCULE_ENV}"
UNITES_RESTE=${UNITES_RESTE:-}

ICI="$(cd "$(dirname "$0")" && pwd)"
DEPOT="$(git -C "$ICI" rev-parse --show-toplevel)"
PARTAGE="$HOME/.local/share/ameesh"
VENV="$PARTAGE/venv"
AMEESH="$VENV/bin/ameesh"
CONF_DIR="$HOME/.config/ameesh"
CONFIG="$CONF_DIR/config.json"
CONFIG_RESTE="$CONF_DIR/config-ancienne-base.json"
ROLE=${ROLE_SUPERVISEUR:-ameesh_superviseur}
EXECUTOR_ROLE=${ROLE_EXECUTOR:-ameesh_executor}
TRAVAIL="$HOME/.local/state/ameesh/mise-a-jour"
SAUVEGARDES="$HOME/backups/ameesh"
#: une persona reste en tour au plus ce temps avant qu'on renonce (secondes)
ATTENTE_MAX=${ATTENTE_MAX:-7200}
mkdir -p "$TRAVAIL" "$SAUVEGARDES"; chmod 700 "$TRAVAIL"

etape() { printf '\n== %s\n' "$*"; }
ssh_vm() { ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$VM" "$@"; }
dsn_de() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dsn"])' "$1"; }
# DSN sans mot de passe éventuel (postgresql://u:secret@h → postgresql://u@h)
sans_secret() { sed -E 's#(://[^:/@]+):[^@]*@#\1@#'; }
ancienne() { [ -f "$CONFIG_RESTE" ]; }
ameesh_ancienne() { AMEESH_CONFIG="$CONFIG_RESTE" "$AMEESH" "$@"; }

# exécuteurs du poste sur la base du mesh : ameesh-runner-agent@<p>, actifs
unites_mesh() {
  systemctl --user list-units 'ameesh-runner-agent@*.service' --state=active \
    --plain --no-legend | awk '{print $1}'
}
# persona d'une unité : son ExecStart porte « --agents <p> »
persona_de() {
  systemctl --user show -p ExecStart --value "$1" \
    | grep -o -- '--agents [^ ;]*' | head -1 | awk '{print $2}'
}
statut() {  # statut de la persona $2 vue par la configuration $1 (« mesh » ou « ancienne »)
  local sortie
  if [ "$1" = ancienne ]; then sortie=$(ameesh_ancienne show "$2" --json)
  else sortie=$("$AMEESH" show "$2" --json); fi
  python3 -c 'import json,sys; print(json.load(sys.stdin).get("status") or "")' <<<"$sortie"
}

verifier() {
  etape "dépôt et version"
  echo "dépôt : $DEPOT @ $(git -C "$DEPOT" rev-parse --short HEAD)"
  echo "installé : $("$VENV/bin/pip" show ameesh 2>/dev/null | sed -n 's/^Version: //p') depuis $(readlink "$PARTAGE/src-current" || echo '?')"
  etape "bases visées (sans mot de passe)"
  echo "mesh     : $(dsn_de "$CONFIG" | sans_secret)"
  if ancienne; then echo "ancienne : $(dsn_de "$CONFIG_RESTE" | sans_secret)"
  else echo "ancienne : aucune ($CONFIG_RESTE absent)"; fi
  etape "exécuteurs"
  echo "poste, base du mesh : $(unites_mesh | tr '\n' ' ')"
  echo "poste, ancienne base : ${UNITES_RESTE:-aucun}"
  etape "accès au serveur"
  ssh_vm 'hostname'
  ("$AMEESH" doctor || true) | tail -3
}

sauvegarder() {
  local jour; jour=$(date +%F-%H%M)
  etape "base du mesh, vidée depuis le poste"
  pg_dump -Fc "$(dsn_de "$CONFIG")" -f "$SAUVEGARDES/mesh-$jour-avant-l60-l74.dump"
  ls -l "$SAUVEGARDES/mesh-$jour-avant-l60-l74.dump"
  etape "base du mesh, sauvegarde du serveur (copie gardée sur la VM)"
  ssh_vm 'systemctl start ameesh-sauvegarde.service && systemctl is-active ameesh-sauvegarde.service || true'
  if ancienne; then
    etape "ancienne base"
    pg_dump -Fc "$(dsn_de "$CONFIG_RESTE")" -f "$SAUVEGARDES/ancienne-$jour-avant-l60-l74.dump"
    ls -l "$SAUVEGARDES/ancienne-$jour-avant-l60-l74.dump"
  fi
}

# Instantané du commit REF dans $PARTAGE/src-<court> ; affiche son chemin.
instantane() {
  local sha cible
  sha=$(git -C "$DEPOT" rev-parse --verify "${1:-HEAD}^{commit}")
  cible="$PARTAGE/src-${sha:0:7}"
  if [ ! -d "$cible" ]; then
    mkdir "$cible.tmp-$$"
    git -C "$DEPOT" archive "$sha" | tar -x -C "$cible.tmp-$$"
    mv "$cible.tmp-$$" "$cible"
  fi
  echo "$cible"
}
# ameesh du code de l'instantané $1, sans l'installer : ses migrations
# passent avant que le venv ne serve le nouveau code (L151)
ameesh_de() {
  local cible=$1; shift
  PYTHONPATH="$cible/src" "$VENV/bin/python" -m ameesh "$@"
}
installe() { [ "$(readlink "$PARTAGE/src-current" 2>/dev/null)" = "$(basename "$1")" ]; }

installer() {
  local ref=${1:-HEAD} cible court
  cible=$(instantane "$ref")
  court=${cible##*/src-}
  etape "instantané $ref ($court) dans $cible"
  if [ "${MIGRER_APRES:-0}" = 1 ]; then
    echo "MIGRER_APRES=1 : migrations après l'installation (ancien ordre) ; « migrer » sans tarder"
  else
    # Le venv sert aussi chaque commande lancée par les agents (mail send…) :
    # le nouveau code y trouverait des colonnes absentes (10/10, L151).
    etape "migrations de $court déjà passées (sonde en lecture seule)"
    ameesh_de "$cible" doctor --probe \
      || { echo "base du mesh pas prête pour $court (ligne ci-dessus) : « $0 migrer $ref » d'abord" >&2; exit 1; }
    if ancienne; then
      AMEESH_CONFIG="$CONFIG_RESTE" ameesh_de "$cible" doctor --probe \
        || { echo "ancienne base pas prête pour $court (ligne ci-dessus) : « $0 migrer $ref » d'abord" >&2; exit 1; }
    fi
  fi
  [ -L "$PARTAGE/src-current" ] && readlink "$PARTAGE/src-current" > "$TRAVAIL/precedent"
  echo "version précédente : $(cat "$TRAVAIL/precedent" 2>/dev/null || echo '?')"
  etape "installation dans $VENV (code puis dépendances, dont psycopg)"
  "$VENV/bin/pip" install -q --force-reinstall --no-deps "$cible"
  "$VENV/bin/pip" install -q "$cible"
  ln -sfn "src-$court" "$PARTAGE/src-current"
  "$VENV/bin/python" -c 'import psycopg, ameesh.budget, ameesh.projects; print("psycopg", psycopg.__version__, ": modules L70 et L62 présents")'
  echo "installé : src-$court. Les exécuteurs tournent encore l'ancien code : redemarrer, sans tarder."
}

# Migrations du commit REF, lancées depuis son instantané. Avant installation,
# seules les migrations additives passent (--additive-only) : le code en
# place, exécuteurs et commandes des agents, tourne sur la base migrée. Une
# fois REF installé (ancien ordre, MIGRER_APRES=1), tout passe.
migrer() {
  local ref=${1:-HEAD} cible garde=(--additive-only)
  cible=$(instantane "$ref")
  if installe "$cible"; then
    garde=()
    etape "$(basename "$cible") déjà installé : toutes ses migrations passent"
  else
    etape "migrations de $(basename "$cible"), avant son installation (additives seulement)"
  fi
  etape "base du mesh (une seule fois pour tout le mesh)"
  echo "base : $(dsn_de "$CONFIG" | sans_secret)"
  ameesh_de "$cible" migrate --dry-run
  migrer_base "$cible" ${garde[@]+"${garde[@]}"}
  if ancienne; then
    etape "ancienne base (ses exécuteurs prennent le même code)"
    echo "base : $(dsn_de "$CONFIG_RESTE" | sans_secret)"
    AMEESH_CONFIG="$CONFIG_RESTE" migrer_base "$cible" ${garde[@]+"${garde[@]}"}
  fi
}
migrer_base() {
  local cible=$1 code=0; shift
  ameesh_de "$cible" migrate "$@" || code=$?
  if [ "$code" = 3 ]; then
    echo "migration non additive : le code en place casserait sur la base migrée." >&2
    echo "Procédure à part (docs/BASCULE.md, L151) : MIGRER_APRES=1 $0 installer, puis $0 migrer, puis redemarrer, d'affilée." >&2
  fi
  [ "$code" = 0 ] || exit "$code"
  ameesh_de "$cible" doctor --probe
}

# The remote administrator switches to the actual migration owner so future
# migration objects receive the owner's default privileges, not postgres's.
reapply_executor_role() {
  local exists
  [[ "$EXECUTOR_ROLE" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "Invalid executor role name" >&2; return 2; }
  exists=$(ssh_vm "runuser -u postgres -- psql -X -At -d ameesh -v ON_ERROR_STOP=1 -v role=$EXECUTOR_ROLE" <<'SQL'
SELECT 1 FROM pg_roles WHERE rolname = :'role';
SQL
)
  if [ "$exists" = 1 ]; then
    {
      cat <<'SQL'
\set ON_ERROR_STOP on
SELECT format('SET ROLE %I', pg_get_userbyid(c.relowner))
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = current_schema() AND c.relname = 'agent_registry' \gexec
SQL
      cat "$DEPOT/deploy/sql/role-executor.sql"
    } | ssh_vm "runuser -u postgres -- psql -X -q -d ameesh -v ON_ERROR_STOP=1 -v role=$EXECUTOR_ROLE -f -"
    echo "Executor role reapplied on mesh database."
  else
    echo "Executor role absent: initial provisioning requires a separate owner decision."
  fi
  if ancienne; then
    "$DEPOT/deploy/mise-a-jour/role-executor.sh" "$(dsn_de "$CONFIG_RESTE")" "$EXECUTOR_ROLE"
  fi
}

role() {
  local existe
  reapply_executor_role
  etape "rôle $ROLE, base du mesh (sur le serveur, en superutilisateur)"
  existe=$(ssh_vm "runuser -u postgres -- psql -X -At -d ameesh -c \"SELECT 1 FROM pg_roles WHERE rolname = '$ROLE'\"")
  if [ "$existe" = 1 ]; then
    ssh_vm "runuser -u postgres -- psql -X -q -d ameesh -v ON_ERROR_STOP=1 -v role=$ROLE -f -" \
      < "$DEPOT/deploy/sql/role-superviseur.sql"
    echo "rôle réappliqué"
  else
    echo "rôle absent de cette base : rien à mettre à jour (le créer est une décision à part)"
  fi
  if ancienne; then
    etape "rôle $ROLE, ancienne base"
    existe=$(psql -X -At "$(dsn_de "$CONFIG_RESTE")" -c "SELECT 1 FROM pg_roles WHERE rolname = '$ROLE'")
    if [ "$existe" = 1 ]; then
      psql -X -q "$(dsn_de "$CONFIG_RESTE")" -v ON_ERROR_STOP=1 -v role="$ROLE" \
        -f "$DEPOT/deploy/sql/role-superviseur.sql"
      echo "rôle réappliqué"
    else
      echo "rôle absent de cette base : rien à mettre à jour"
    fi
  fi
}

# Redémarre les unités $2… (base $1 : mesh | ancienne) une par une, chacune
# au premier instant où sa persona n'est pas en tour. Une persona longue en
# tour ne bloque pas les autres : on repasse sur la liste jusqu'au bout.
redemarrer_liste() {
  local base=$1; shift
  local restantes=("$@") suivantes debut=$SECONDS u p st
  while [ ${#restantes[@]} -gt 0 ]; do
    suivantes=()
    for u in "${restantes[@]}"; do
      p=$(persona_de "$u")
      [ -n "$p" ] || { echo "$u : persona introuvable dans ExecStart" >&2; exit 1; }
      st=$(statut "$base" "$p")
      if [ "$st" = running ]; then suivantes+=("$u"); continue; fi
      systemctl --user restart "$u"
      sleep 10
      if ! systemctl --user is-active --quiet "$u"; then
        echo "$u : ne redémarre pas — journalctl --user -u $u -n 50 ; puis « $0 retour »" >&2
        exit 1
      fi
      echo "$p : redémarré (était $st)"
    done
    restantes=("${suivantes[@]+"${suivantes[@]}"}")
    if [ ${#restantes[@]} -gt 0 ]; then
      if [ $((SECONDS - debut)) -gt "$ATTENTE_MAX" ]; then
        echo "toujours en tour après ${ATTENTE_MAX}s : ${restantes[*]} — à redémarrer à la main" >&2
        exit 1
      fi
      sleep 15
    fi
  done
}

redemarrer() {
  local unites
  mapfile -t unites < <(unites_mesh)
  etape "exécuteurs du poste, base du mesh (${#unites[@]}), un par un hors tour"
  [ ${#unites[@]} -gt 0 ] && redemarrer_liste mesh "${unites[@]}"
  etape "ameesh-notify"
  systemctl --user restart ameesh-notify.service
  sleep 5; systemctl --user is-active ameesh-notify.service
  if [ -n "$UNITES_RESTE" ]; then
    # shellcheck disable=SC2206
    unites=($UNITES_RESTE)
    etape "exécuteurs de l'ancienne base (${#unites[@]}), un par un hors tour"
    ancienne || { echo "$CONFIG_RESTE absent : ces unités ne savent pas quelle base lire" >&2; exit 1; }
    redemarrer_liste ancienne "${unites[@]}"
  fi
}

controler() {
  local echecs=()
  verif() { etape "$*"; "$@" || echecs+=("$*"); }
  verif "$AMEESH" doctor
  verif "$AMEESH" budget
  verif "$AMEESH" accounts list
  verif "$AMEESH" projects
  verif "$AMEESH" alerts
  etape "exécuteurs en échec"
  systemctl --user list-units 'ameesh-*' --state=failed --plain --no-legend || true
  if ancienne; then
    verif env AMEESH_CONFIG="$CONFIG_RESTE" "$AMEESH" doctor
  fi
  if [ ${#echecs[@]} -gt 0 ]; then
    printf '\nen échec :\n' >&2; printf '  %s\n' "${echecs[@]}" >&2
    exit 1
  fi
  echo; echo "contrôles verts"
}

retour() {
  local precedent
  precedent=$(cat "$TRAVAIL/precedent" 2>/dev/null || true)
  [ -n "$precedent" ] && [ -d "$PARTAGE/$precedent" ] \
    || { echo "version précédente inconnue ($TRAVAIL/precedent)" >&2; exit 1; }
  etape "réinstallation de $precedent"
  "$VENV/bin/pip" install -q --force-reinstall --no-deps "$PARTAGE/$precedent"
  ln -sfn "$precedent" "$PARTAGE/src-current"
  echo "bases inchangées : 0041 à 0047 n'ajoutent que des colonnes et des tables, que l'ancien code ignore"
  redemarrer
}

case "${1:-}" in
  verifier) verifier ;;
  sauvegarder) sauvegarder ;;
  installer) installer "${2:-HEAD}" ;;
  migrer) migrer ;;
  role) role ;;
  redemarrer) redemarrer ;;
  controler) controler ;;
  retour) retour ;;
  *) sed -n '4,26p' "$0" >&2; exit 2 ;;
esac
