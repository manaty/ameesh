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
#   installer    instantané du code REF dans ~/.local/share/ameesh/src-<sha>,
#                installé dans le venv (dépendance psycopg comprise)
#   migrer       ameesh migrate : base du mesh (UNE fois, pour tout le mesh),
#                puis ancienne base
#   role         rôle superviseur réappliqué, sur chaque base où il existe
#   redemarrer   exécuteurs du poste un par un, chacun hors tour, puis notify,
#                puis les exécuteurs de l'ancienne base
#   controler    doctor, budget, accounts list, projects, alerts
#   retour       réinstalle la version précédente et redémarre (sans toucher
#                aux bases : 0041 et 0042 sont additives)
#
# REF (installer) : commit, branche ou étiquette du dépôt qui contient ce
# script ; défaut : HEAD de ce dépôt.
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

installer() {
  local ref=${1:-HEAD} sha court cible
  sha=$(git -C "$DEPOT" rev-parse --verify "$ref^{commit}")
  court=${sha:0:7}
  cible="$PARTAGE/src-$court"
  etape "instantané $ref ($court) dans $cible"
  [ -L "$PARTAGE/src-current" ] && readlink "$PARTAGE/src-current" > "$TRAVAIL/precedent"
  echo "version précédente : $(cat "$TRAVAIL/precedent" 2>/dev/null || echo '?')"
  if [ ! -d "$cible" ]; then
    mkdir "$cible"
    git -C "$DEPOT" archive "$sha" | tar -x -C "$cible"
  fi
  etape "installation dans $VENV (code puis dépendances, dont psycopg)"
  "$VENV/bin/pip" install -q --force-reinstall --no-deps "$cible"
  "$VENV/bin/pip" install -q "$cible"
  ln -sfn "src-$court" "$PARTAGE/src-current"
  "$VENV/bin/python" -c 'import psycopg, ameesh.budget, ameesh.projects; print("psycopg", psycopg.__version__, ": modules L70 et L62 présents")'
  echo "installé : src-$court. Les exécuteurs tournent encore l'ancien code : migrer, puis redemarrer, sans tarder."
}

migrer() {
  etape "ameesh migrate, base du mesh (une seule fois pour tout le mesh)"
  echo "base : $(dsn_de "$CONFIG" | sans_secret)"
  "$AMEESH" migrate
  "$AMEESH" doctor | grep -i 'migration' || true
  if ancienne; then
    etape "ameesh migrate, ancienne base (ses exécuteurs prennent le même code)"
    echo "base : $(dsn_de "$CONFIG_RESTE" | sans_secret)"
    ameesh_ancienne migrate
    ameesh_ancienne doctor | grep -i 'migration' || true
  fi
}

role() {
  local existe
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
  echo "bases inchangées : 0041 et 0042 n'ajoutent que des colonnes et des tables, que l'ancien code ignore"
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
  *) sed -n '4,22p' "$0" >&2; exit 2 ;;
esac
