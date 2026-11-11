#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# #6881 / manaty/ameesh#5236 : CLI ameesh du BUREAU sur bureau-goal, avec la
# connexion PostgreSQL limitée (deploy/sql/role-bureau.sql), sur le modèle du
# poste (venv versionné, bascule atomique). Procédure complète, côté hub et
# côté bureau : docs/ops/bureau-goal-cli.md. Lancé PAR LE COMPTE UNIX DU
# BUREAU sur bureau-goal (jamais root), étape par étape :
#
#   installer-bureau-goal.sh verifier
#   installer-bureau-goal.sh installer <REF>    tag vX.Y.Z ou SHA de main déployé en staging
#   installer-bureau-goal.sh connexion          secret du rôle lu sur l'entrée standard
#   installer-bureau-goal.sh hooks <clone>...   hooks de provenance dans les clones du bureau
#   installer-bureau-goal.sh controler <lot>    lot de contrôle réservé au bureau (external-executor)
#   installer-bureau-goal.sh retour             venv précédent, sans réseau
#
# Variables (défauts) : DB_HOST (10.77.0.1, la base par le pair WireGuard),
# DB_PORT (5432), DB_NAME (ameesh), DB_USER (bureau_goal), AGENT (bureau),
# HOST_NAME (bureau-goal), HARNESS (claude), SRC_GIT (dépôt manaty/ameesh),
# BASE (~/.local/share/ameesh), CANON (~/canon : clone du canon, lu pour les
# familles des reçus de provenance ; jamais synchronisé vers la base d'ici).
#
# Aucun secret en argument ni dans le dépôt : le mot de passe du rôle n'entre
# que par l'entrée standard de `connexion` et ne vit que dans ~/.pgpass 0600 ;
# la configuration ne contient pas de mot de passe. Aucun DSN d'agent.
set -euo pipefail
umask 077

DB_HOST=${DB_HOST:-10.77.0.1}
DB_PORT=${DB_PORT:-5432}
DB_NAME=${DB_NAME:-ameesh}
DB_USER=${DB_USER:-bureau_goal}
AGENT=${AGENT:-bureau}
HOST_NAME=${HOST_NAME:-bureau-goal}
HARNESS=${HARNESS:-claude}
SRC_GIT=${SRC_GIT:-https://github.com/manaty/ameesh.git}
BASE=${BASE:-$HOME/.local/share/ameesh}
CANON=${CANON:-$HOME/canon}
CONF_DIR=${AMEESH_CONFIG_DIR:-$HOME/.config/ameesh}
BIN=$HOME/.local/bin
AMEESH=$BASE/venv/bin/ameesh

log() { echo "== $*"; }
die() { echo "refusé : $*" >&2; exit 1; }

[[ $(id -u) -ne 0 ]] || die "à lancer par le compte du bureau, pas root"

dsn() {
  # sans mot de passe : libpq le lit dans ~/.pgpass (0600)
  printf 'host=%s port=%s dbname=%s user=%s sslmode=require application_name=ameesh-bureau' \
    "$DB_HOST" "$DB_PORT" "$DB_NAME" "$DB_USER"
}

verifier() {
  local ok=0
  for cmd in git python3 psql; do
    command -v "$cmd" >/dev/null || { echo "manque : $cmd" >&2; ok=1; }
  done
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || { echo "python3 >= 3.11 requis" >&2; ok=1; }
  [[ $(hostname -s) == "$HOST_NAME" ]] \
    || echo "attention : hostname $(hostname -s) ≠ $HOST_NAME (AMEESH_HOST le fixe)" >&2
  if [[ -x $AMEESH ]]; then
    echo "installé : $(readlink -f "$BASE/venv") ($("$AMEESH" --version 2>/dev/null || echo '?'))"
  else
    echo "installé : rien"
  fi
  if [[ -f $HOME/.pgpass ]]; then
    [[ $(stat -c %a "$HOME/.pgpass") == 600 ]] || { echo "~/.pgpass n'est pas 0600" >&2; ok=1; }
    psql "$(dsn)" -X -Atc "select current_user" >/dev/null 2>&1 \
      && echo "base : connexion $DB_USER@$DB_HOST ok" \
      || { echo "base : connexion $DB_USER@$DB_HOST impossible (pair WireGuard, pg_hba, secret ?)" >&2; ok=1; }
  else
    echo "base : ~/.pgpass absent (étape connexion)"
  fi
  return $ok
}

installer() {
  local ref=${1:?REF requis : tag vX.Y.Z ou SHA de main déployé en staging}
  mkdir -p "$BASE" "$BIN"
  [[ -d $BASE/src/.git ]] || git clone --quiet "$SRC_GIT" "$BASE/src"
  git -C "$BASE/src" fetch --quiet --tags origin
  local sha
  sha=$(git -C "$BASE/src" rev-parse --verify --end-of-options "$ref^{commit}") \
    || die "référence inconnue : $ref"
  # une version de staging est un commit de main (§14) ; jamais une branche de lot
  git -C "$BASE/src" merge-base --is-ancestor "$sha" origin/main \
    || die "$ref ($sha) n'est pas dans origin/main"
  local short=${sha:0:12} snap venv
  snap=$BASE/src-$short
  venv=$BASE/venv-$short
  if [[ ! -f $snap/.complet ]]; then
    rm -rf "$snap"; mkdir -p "$snap"
    git -C "$BASE/src" archive "$sha" | tar -x -C "$snap"
    echo "$sha" > "$snap/.complet"
  fi
  if [[ ! -f $venv/.complet ]]; then
    rm -rf "$venv"
    log "venv $venv (dépendances épinglées, #4778)"
    python3 -m venv "$venv"
    "$venv/bin/pip" install --quiet --require-hashes --only-binary=:all: \
      -r "$snap/deploy/mise-a-jour/runtime-pins.txt"
    "$venv/bin/pip" install --quiet --no-deps "$snap"
    "$venv/bin/ameesh" --version >/dev/null
    echo "$sha" > "$venv/.complet"
  fi
  # bascule atomique ; le précédent est gardé pour `retour`
  if [[ -L $BASE/venv && $(readlink "$BASE/venv") != "$venv" ]]; then
    ln -sfn "$(readlink "$BASE/venv")" "$BASE/venv.precedent"
  fi
  ln -sfn "$venv" "$BASE/venv.tmp" && mv -Tf "$BASE/venv.tmp" "$BASE/venv"
  ln -sfn "$BASE/venv/bin/ameesh" "$BIN/ameesh"
  ln -sfn "$BASE/venv/bin/agent-mail" "$BIN/agent-mail"
  log "ameesh $sha installé ($BASE/venv → $venv)"
}

retour() {
  [[ -L $BASE/venv.precedent ]] || die "aucun venv précédent"
  local prev; prev=$(readlink "$BASE/venv.precedent")
  ln -sfn "$prev" "$BASE/venv.tmp" && mv -Tf "$BASE/venv.tmp" "$BASE/venv"
  log "retour : $BASE/venv → $prev"
}

connexion() {
  [[ ! -t 0 ]] || die "le secret du rôle $DB_USER se lit sur l'entrée standard (jamais en argument)"
  local secret; IFS= read -r secret || true
  [[ -n $secret ]] || die "secret vide"
  [[ $secret != *:* && $secret != *\\* ]] || die "secret avec ':' ou '\\' : à échapper pour .pgpass, à régénérer"
  mkdir -p "$CONF_DIR"; chmod 0700 "$CONF_DIR"
  local pgpass=$HOME/.pgpass tmp line
  line="$DB_HOST:$DB_PORT:$DB_NAME:$DB_USER:$secret"
  tmp=$(mktemp "$HOME/.pgpass.XXXXXX")
  { [[ -f $pgpass ]] && grep -v "^$DB_HOST:$DB_PORT:$DB_NAME:$DB_USER:" "$pgpass" || true
    printf '%s\n' "$line"; } > "$tmp"
  chmod 0600 "$tmp"; mv -f "$tmp" "$pgpass"
  unset secret line
  git -C "$CANON" rev-parse --verify --quiet origin/main >/dev/null \
    || die "canon introuvable en $CANON (clone requis : familles des reçus)"
  python3 - "$CONF_DIR/config.json" "$(dsn)" "$HOST_NAME" "$CANON" <<'PY'
import json, os, sys
path, dsn, host, canon = sys.argv[1:5]
data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
data.update(dsn=dsn, host=host, canons=[{"path": canon, "ref": "origin/main"}])
tmp = path + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
os.replace(tmp, path)
PY
  psql "$(dsn)" -X -Atc "select current_user" | grep -qx "$DB_USER" \
    || die "connexion $DB_USER@$DB_HOST refusée"
  log "connexion $DB_USER@$DB_HOST:$DB_PORT/$DB_NAME ; ~/.pgpass 0600, $CONF_DIR/config.json sans secret"
}

hooks() {
  [[ $# -gt 0 ]] || die "au moins un clone du bureau"
  local clone
  for clone in "$@"; do
    git -C "$clone" rev-parse --git-dir >/dev/null 2>&1 || die "$clone n'est pas un clone git"
    (cd "$clone" && "$AMEESH" provenance install)
  done
}

controler() {
  local lot=${1:?lot de contrôle requis (réservé au bureau : work external-executor <lot> $AGENT)}
  [[ $lot =~ ^[0-9]+$ ]] || die "lot invalide : $lot"
  export AMEESH_HOST=$HOST_NAME
  local run; run=$(date -u +%Y%m%dT%H%M%SZ)
  local a=controle-a-$run b=controle-b-$run
  log "1. liaison avec PID vérifié (session $a, pid $$)"
  "$AMEESH" mail bind "$AGENT" --session "$a" --harness "$HARNESS" --pid $$
  log "2. prise par $a, puis refus d'une seconde session ($b)"
  "$AMEESH" work claim "$lot" --session "$a"
  # la seconde session est un processus fils lié à son propre PID : il est
  # l'ancêtre le plus proche de sa commande, sa liaison fait foi pour elle
  if AMEESH="$AMEESH" AGENT="$AGENT" HARNESS="$HARNESS" LOT="$lot" B="$b" bash -c '
      "$AMEESH" mail bind "$AGENT" --session "$B" --harness "$HARNESS" --pid $$ >/dev/null
      out=$("$AMEESH" work claim "$LOT" --session "$B" 2>&1); rc=$?
      "$AMEESH" mail unbind --session "$B" --harness "$HARNESS" >/dev/null
      echo "$out"; exit $rc'; then
    "$AMEESH" work claim "$lot" --session "$a" --release || true
    die "la seconde session a pris le lot $lot"
  fi
  echo "   refus attendu témoigné"
  "$AMEESH" work claim "$lot" --session "$a" --release
  log "3. commit de contrôle avec reçu (hooks installés)"
  local repo; repo=$(mktemp -d)
  git init --quiet "$repo"
  (cd "$repo" && "$AMEESH" provenance install >/dev/null)
  echo "controle $run" > "$repo/controle.txt"
  git -C "$repo" add controle.txt
  # le reçu exige le bureau comme auteur ET committer : l'environnement prime
  # sur `git -c user.*`, il est donc fixé ici
  PATH=$BIN:$PATH GIT_AUTHOR_NAME=$AGENT GIT_AUTHOR_EMAIL=$AGENT@ameesh.invalid \
    GIT_COMMITTER_NAME=$AGENT GIT_COMMITTER_EMAIL=$AGENT@ameesh.invalid \
    git -C "$repo" commit --quiet -m "contrôle bureau-goal $run"
  local head; head=$(git -C "$repo" rev-parse HEAD)
  if ! (cd "$repo" && "$AMEESH" provenance show --commit "$head") | grep -F "$a"; then
    # les hooks se taisent (ils ne bloquent jamais un commit) : la cause se lit ici
    (cd "$repo" && PATH=$BIN:$PATH "$AMEESH" provenance record --commit "$head") || true
    die "aucun reçu pour le commit de contrôle $head"
  fi
  rm -rf "$repo"
  "$AMEESH" mail unbind --session "$a" --harness "$HARNESS" >/dev/null
  log "contrôle complet : liaison $a, prise exclusive, reçu de $head"
}

etape=${1:-}; shift || true
case $etape in
  verifier)  verifier ;;
  installer) installer "$@" ;;
  connexion) connexion ;;
  hooks)     hooks "$@" ;;
  controler) controler "$@" ;;
  retour)    retour ;;
  *) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
