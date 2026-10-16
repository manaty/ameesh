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
#   migrer [--additif-seulement] [REF]
#                migrations de REF, lancées depuis son instantané AVANT de
#                l'installer : base du mesh (UNE fois), puis ancienne base ;
#                refusé si l'une n'est pas additive (voir MIGRER_APRES) ;
#                --additif-seulement garde ce refus même quand REF est déjà
#                installé (staging : des VM tournent encore le code précédent)
#   installer    instantané du code REF dans ~/.local/share/ameesh/src-<sha>,
#                installé dans le venv (dépendance psycopg comprise) ; refusé
#                tant qu'une base n'a pas les migrations de REF
#   role         rôle superviseur réappliqué, sur chaque base où il existe
#   redemarrer   exécuteurs du poste un par un, chacun hors tour, puis notify,
#                puis les exécuteurs de l'ancienne base
#   controler    doctor, budget, accounts list, projects, alerts
#   retour       réinstalle la version précédente et redémarre (sans toucher
#                aux bases : 0041 à 0047 sont additives)
#   publier      étiquette annotée v<version> sur REF (un commit de main, la
#                version de son pyproject.toml), poussée, puis release GitHub
#                (notes : docs/versions/<version>.md de main, sinon la PR qui a
#                fusionné ce commit) ;
#                idempotent, jamais une étiquette déplacée (L138)
#   rattraper    « publier » pour chaque version de l'historique de main
#                (premier parent) qui n'a pas encore d'étiquette ou de release ;
#                une étiquette déjà posée sur le distant fait foi (son commit est
#                publié, s'il porte cette version)
#
#   A_BLANC=1 (publier, rattraper) : affiche ce qui serait fait, sans rien
#   écrire, pas même un fetch (le distant est lu par ls-remote, main est
#   $DISTANT/main local). Ces deux étapes ne lisent pas $BASCULE_ENV.
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
case "${1:-}" in
  publier|rattraper) ;;   # le dépôt et GitHub seuls : ni serveur ni base
  *)
    [ -r "$BASCULE_ENV" ] || { echo "paramètres absents : $BASCULE_ENV" >&2; exit 2; }
    # shellcheck disable=SC1090
    . "$BASCULE_ENV"
    : "${VM:?VM manquant dans $BASCULE_ENV}"
    ;;
esac
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
case "${1:-}" in
  publier|rattraper) ;;
  *) mkdir -p "$TRAVAIL" "$SAUVEGARDES"; chmod 700 "$TRAVAIL" ;;
esac

etape() { printf '\n== %s\n' "$*"; }
ssh_vm() { ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$VM" "$@"; }
dsn_de() { python3 -c 'import json,sys; sys.stdout.write(json.load(open(sys.argv[1]))["dsn"])' "$1"; }
# #518: a DSN's password never goes on a command line, which every process of
# the host can read (ps, /proc/<pid>/cmdline), nor in a trace (bash -x) or a
# display. src/ameesh/dsn.py splits it as libpq reads it, the DSN on its
# standard input; it is the copy of the commit being deployed (the installed
# ameesh may be older). `dsn_part VAR CONFIG dsn|password`: VAR gets the DSN
# of CONFIG without its password, or that password, byte for byte (the "."
# keeps the trailing newlines that $(…) strips); it fails if the DSN is
# refused, and callers stop (|| return). `avec_mdp CONFIG cmd…`: cmd with that
# password in PGPASSWORD, which libpq reads, in a subshell without xtrace.
DSN_PY="$DEPOT/src/ameesh/dsn.py"
dsn_part() {
  local _sortie
  _sortie=$(dsn_de "$2" | python3 "$DSN_PY" "$3" && printf .) || return
  printf -v "$1" '%s' "${_sortie%.}"
}
avec_mdp() {
  ( { set +x; } 2>/dev/null
    dsn_part mdp "$1" password || exit
    shift
    if [ -n "$mdp" ]; then PGPASSWORD=$mdp exec "$@"; else exec "$@"; fi )
}
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
  local d
  etape "dépôt et version"
  echo "dépôt : $DEPOT @ $(git -C "$DEPOT" rev-parse --short HEAD)"
  echo "installé : $("$VENV/bin/pip" show ameesh 2>/dev/null | sed -n 's/^Version: //p') depuis $(readlink "$PARTAGE/src-current" || echo '?')"
  etape "bases visées (sans mot de passe)"
  dsn_part d "$CONFIG" dsn || return
  echo "mesh     : $d"
  if ancienne; then dsn_part d "$CONFIG_RESTE" dsn || return; echo "ancienne : $d"
  else echo "ancienne : aucune ($CONFIG_RESTE absent)"; fi
  etape "exécuteurs"
  echo "poste, base du mesh : $(unites_mesh | tr '\n' ' ')"
  echo "poste, ancienne base : ${UNITES_RESTE:-aucun}"
  etape "accès au serveur"
  ssh_vm 'hostname'
  ("$AMEESH" doctor || true) | tail -3
}

sauvegarder() {
  local jour dsn; jour=$(date +%F-%H%M)
  etape "base du mesh, vidée depuis le poste"
  dsn_part dsn "$CONFIG" dsn || return
  avec_mdp "$CONFIG" pg_dump -Fc -d "$dsn" -f "$SAUVEGARDES/mesh-$jour-avant-l60-l74.dump"
  ls -l "$SAUVEGARDES/mesh-$jour-avant-l60-l74.dump"
  etape "base du mesh, sauvegarde du serveur (copie gardée sur la VM)"
  ssh_vm 'systemctl start ameesh-sauvegarde.service && systemctl is-active ameesh-sauvegarde.service || true'
  if ancienne; then
    etape "ancienne base"
    dsn_part dsn "$CONFIG_RESTE" dsn || return
    avec_mdp "$CONFIG_RESTE" pg_dump -Fc -d "$dsn" -f "$SAUVEGARDES/ancienne-$jour-avant-l60-l74.dump"
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
  local ref=${1:-HEAD} cible d garde=(--additive-only) force=0
  if [ "${1:-}" = "--additif-seulement" ]; then
    force=1
    ref=${2:-HEAD}
  fi
  cible=$(instantane "$ref")
  # --additif-seulement : la garde tient meme quand l'instantane est deja
  # src-current (reprise de staging, avis R1 B2) : des VM peuvent tourner un
  # code precedent sur la base migree.
  if [ "$force" != 1 ] && installe "$cible"; then
    garde=()
    etape "$(basename "$cible") déjà installé : toutes ses migrations passent"
  else
    etape "migrations de $(basename "$cible"), avant son installation (additives seulement)"
  fi
  etape "base du mesh (une seule fois pour tout le mesh)"
  dsn_part d "$CONFIG" dsn || return
  echo "base : $d"
  ameesh_de "$cible" migrate --dry-run
  migrer_base "$cible" ${garde[@]+"${garde[@]}"}
  if ancienne; then
    etape "ancienne base (ses exécuteurs prennent le même code)"
    dsn_part d "$CONFIG_RESTE" dsn || return
    echo "base : $d"
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
  local exists dsn
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
    dsn_part dsn "$CONFIG_RESTE" dsn || return
    avec_mdp "$CONFIG_RESTE" "$DEPOT/deploy/mise-a-jour/role-executor.sh" "$dsn" "$EXECUTOR_ROLE"
  fi
}

role() {
  local existe dsn
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
    dsn_part dsn "$CONFIG_RESTE" dsn || return
    existe=$(avec_mdp "$CONFIG_RESTE" psql -X -At -d "$dsn" \
      -c "SELECT 1 FROM pg_roles WHERE rolname = '$ROLE'")
    if [ "$existe" = 1 ]; then
      avec_mdp "$CONFIG_RESTE" psql -X -q -d "$dsn" -v ON_ERROR_STOP=1 -v role="$ROLE" \
        -f "$DEPOT/deploy/sql/role-superviseur.sql"
      echo "rôle réappliqué"
    else
      echo "rôle absent de cette base : rien à mettre à jour"
    fi
  fi
}

lire_bail() {  # base persona → its registry row (`ameesh show --json`)
  if [ "$1" = ancienne ]; then ameesh_ancienne show "$2" --json
  else "$AMEESH" show "$2" --json; fi
}
# base … → the clock of that base's database, epoch seconds, read through
# ameesh's own connection, as `ameesh show` is: no DSN nor password in argv
heure_base() {
  local code='from ameesh import config, db
conn = db.connect(config.load())
try:
    print(conn.query("SELECT extract(epoch FROM clock_timestamp())::float8 AS t")[0]["t"])
finally:
    conn.close()'
  if [ "$1" = ancienne ]; then AMEESH_CONFIG="$CONFIG_RESTE" "$VENV/bin/python" -c "$code"
  else "$VENV/bin/python" -c "$code"; fi
}
proprietaire_de() {  # unit → the lease owner its runner takes: --runner-id, else :<MainPID>
  local sortie id
  sortie=$(systemctl --user show -p ExecStart --value "$1") && id=$(bail_runner_id <<<"$sortie") \
    || { echo "$1: ExecStart unreadable" >&2; return 1; }
  if [ -n "$id" ]; then echo "$id"
  else echo ":$(systemctl --user show -p MainPID --value "$1")"; fi
}

# Redémarre les unités $2… (base $1 : mesh | ancienne) une par une, chacune
# au premier instant où sa persona n'est pas en tour. Une persona longue en
# tour ne bloque pas les autres : on repasse sur la liste jusqu'au bout. Une
# persona arrêtée dans le registre (`stopped`, arrêt du propriétaire) n'est
# pas réactivée (#1131) ; son unité encore active est seulement rechargée sur
# le nouveau code (#1633), sans bail ni tour.
redemarrer_liste() {
  local base=$1; shift
  local restantes=("$@") suivantes debut=$SECONDS u p st avant ligne heure tenu attendu
  while [ ${#restantes[@]} -gt 0 ]; do
    suivantes=()
    for u in "${restantes[@]}"; do
      p=$(persona_de "$u")
      [ -n "$p" ] || { echo "$u : persona introuvable dans ExecStart" >&2; exit 1; }
      st=$(statut "$base" "$p")
      # #1131 : le propriétaire a arrêté cette persona ; un déploiement ne la
      # réactive pas (incident du 14/10 : unités dev1/dev2/dev4/dev7 restées
      # actives malgré le registre `stopped`).
      # #1633 : la laisser tourner sur l'ancien code faisait repartir une
      # reprise ultérieure sur cette version. Si son unité est encore active,
      # on la redémarre pour charger le nouveau code — sans exiger de bail :
      # la persona reste arrêtée, aucun tour. Une unité déjà arrêtée n'est pas
      # relancée (elle chargera le nouveau code à son démarrage).
      if [ "$st" = stopped ]; then
        if systemctl --user is-active --quiet "$u"; then
          systemctl --user restart "$u" \
            && systemctl --user is-active --quiet "$u" \
            || { echo "$u : unité d'une persona arrêtée, ne redémarre pas — journalctl --user -u $u -n 50" >&2; exit 1; }
          echo "$p : arrêté dans le registre — unité $u redémarrée pour charger le nouveau code, aucun tour"
        else
          echo "$p : arrêté dans le registre — unité $u déjà arrêtée, rien à recharger"
        fi
        continue
      fi
      if [ "$st" = running ]; then suivantes+=("$u"); continue; fi
      # #502: only a runner that held its lease must take it again (bail.sh)
      ligne=$(lire_bail "$base" "$p") && heure=$(heure_base "$base") \
        && attendu=$(proprietaire_de "$u") \
        || { echo "$u: registry, database clock or ExecStart unreadable — nothing restarted" >&2; exit 1; }
      avant=$(bail_epoque <<<"$ligne")
      tenu=0
      bail_valide -1 "$attendu" "$heure" <<<"$ligne" && tenu=1
      systemctl --user restart "$u"
      sleep 10
      if ! systemctl --user is-active --quiet "$u"; then
        echo "$u : ne redémarre pas — journalctl --user -u $u -n 50 ; puis « $0 retour »" >&2
        exit 1
      fi
      if [ "$tenu" = 0 ]; then
        echo "WARNING $p: not checked: no lease before the restart, no sign of life"
        non_verifiees+=("$p")
      elif ! attendu=$(proprietaire_de "$u") || ! attendre_bail "$avant" "$attendu" "$base" "$p"; then
        echo "$u: active, but $p did not take its lease again — journalctl --user -u $u -n 50; then « $0 retour »" >&2
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
  # shellcheck source=bail.sh
  . "$ICI/bail.sh"
  local unites
  non_verifiees=()
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
  if [ ${#non_verifiees[@]} -gt 0 ]; then
    etape "WARNING: ${#non_verifiees[@]} persona(s) not checked (no lease before the restart, no sign of life): ${non_verifiees[*]}"
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

# --- publication (L138) ----------------------------------------------------
# Une version est le commit de main où pyproject.toml prend son numéro. Son
# étiquette annotée v<version> est posée sur ce commit et poussée ; la release
# GitHub reprend les notes de docs/versions/<version>.md lues dans main (jamais
# dans l'arbre de travail), sinon la description de la PR qui a fusionné ce
# commit. Rien n'est jamais déplacé ni remplacé : une étiquette posée ailleurs
# arrête tout (ORCHESTRATEUR.md, livraison, 6). Les étiquettes qui font foi
# sont celles du distant (ls-remote). À blanc, rien n'est écrit, pas même dans
# le dépôt local : ni fetch, ni étiquette ; main est alors $DISTANT/main tel
# qu'il est ici.
DISTANT=${DISTANT:-origin}
a_blanc() { [ "${A_BLANC:-0}" = 1 ]; }
fait() {  # la commande, ou son affichage à blanc
  if a_blanc; then printf '  à blanc :'; printf ' %q' "$@"; echo; else "$@"; fi
}
version_de() {
  git -C "$DEPOT" show "$1:pyproject.toml" | sed -n 's/^version = "\(.*\)"$/\1/p' | head -1
}
depot_github() {
  git -C "$DEPOT" remote get-url "$DISTANT" \
    | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##'
}
# « <commit> <étiquette> » des étiquettes v* du distant (une étiquette annotée
# donne son commit, `^{}`)
etiquettes_distantes() {
  git -C "$DEPOT" ls-remote --tags "$DISTANT" 'refs/tags/v*' | awk '
    { sub("^refs/tags/", "", $2) }
    $2 ~ /\^\{\}$/ { sub(/\^\{\}$/, "", $2); pele[$2] = $1; next }
    { brut[$2] = $1 }
    END { for (t in brut) print ((t in pele) ? pele[t] : brut[t]), t }'
}
# le distant, lu une fois : main (fetch, sauf à blanc) et les étiquettes
# (ls-remote) ; aucune étiquette locale n'est touchée avant que les notes de
# la version publiée soient validées (#348 r2) : publier ne rapatrie que la
# sienne, ensuite
lire_distant() {
  [ -z "${DISTANTES+x}" ] || return 0
  a_blanc || git -C "$DEPOT" fetch -q --no-tags "$DISTANT" "+refs/heads/main:refs/remotes/$DISTANT/main"
  DISTANTES=$(etiquettes_distantes)
}
commit_distant() { awk -v t="$1" '$2 == t { print $1 }' <<<"$DISTANTES"; }
# la PR qui a fusionné le commit $1 : fusionnée, et son commit de fusion est
# $1 ; candidates : « (#N) » du sujet (squash), « Merge pull request #N »
# (fusion), puis l'API de GitHub
pr_verifiee() {
  [ "$(gh pr view "$1" -R "$(depot_github)" --json state,mergeCommit \
        --jq '.state + " " + (.mergeCommit.oid // "")' 2>/dev/null)" = "MERGED $2" ]
}
pr_de() {
  local sujet n
  sujet=$(git -C "$DEPOT" log -1 --format=%s "$1")
  for n in $(sed -nE 's/^Merge pull request #([0-9]+) .*/\1/p; s/.*\(#([0-9]+)\)$/\1/p' <<<"$sujet"); do
    if pr_verifiee "$n" "$1"; then echo "$n"; return 0; fi
  done
  for n in $(gh api "repos/$(depot_github)/commits/$1/pulls" --jq '.[].number' 2>/dev/null || true); do
    if pr_verifiee "$n" "$1"; then echo "$n"; return 0; fi
  done
  return 1
}
# la description d'une PR sans le pied de l'outil : la ligne exacte « 🤖
# Generated with [Claude Code](…) », les lignes d'attribution Co-Authored-By
# <noreply@anthropic.com>, puis les lignes vides et séparateurs (---) de fin ;
# une note qui parle d'autre chose reste
sans_pied_d_outil() {
  python3 -c '
import re, sys
outil = re.compile("^\\s*\U0001F916\uFE0F?\\s*Generated with \\[Claude Code\\]\\([^)]*\\)\\s*$")
credit = re.compile(r"^\s*Co-Authored-By:.*<noreply@anthropic\.com>\s*$", re.I)
fin = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})?\s*$")
lignes = [l for l in sys.stdin.read().replace("\r", "").split("\n")
          if not outil.match(l) and not credit.match(l)]
while lignes and fin.match(lignes[-1]):
    lignes.pop()
while lignes and not lignes[0].strip():
    lignes.pop(0)
sys.stdout.write("\n".join(lignes))
'
}
notes_de() {  # notes de la version $1 (commit $2), sur la sortie standard
  local main n corps
  main=$(git -C "$DEPOT" rev-parse --verify "$DISTANT/main^{commit}")
  if git -C "$DEPOT" cat-file -e "$main:docs/versions/$1.md" 2>/dev/null; then
    git -C "$DEPOT" show "$main:docs/versions/$1.md"
    return
  fi
  n=$(pr_de "$2") || { echo "notes introuvables pour $1 : ni docs/versions/$1.md dans $DISTANT/main, ni PR qui a fusionné ${2:0:7}" >&2; return 1; }
  corps=$(gh pr view "$n" -R "$(depot_github)" --json body --jq .body) \
    || { echo "description de la PR #$n illisible : pas de release $1" >&2; return 1; }
  # le pied de l'outil n'est pas une note de version ; il doit rester des
  # notes une fois retiré
  corps=$(sans_pied_d_outil <<<"$corps")
  [ -n "${corps//[[:space:]]/}" ] || { echo "PR #$n sans notes (description vide, ou la seule ligne de l'outil) : pas de release $1" >&2; return 1; }
  printf '%s\n\nPR : #%s\n' "$corps" "$n"
}
derniere_version() {  # la plus haute version étiquetée sur le distant (v1.2.3 → 1.2.3)
  awk '{ print $2 }' <<<"$DISTANTES" | sed -n 's/^v\([0-9].*\)$/\1/p' | sort -V | tail -1
}

# publier REF [VERSION] : VERSION, la version attendue (rattraper) ; un écart
# avec pyproject.toml de REF arrête tout
publier() {
  local ref=${1:-HEAD} attendue=${2:-} sha version tag distant ici notes plus_haute publiee
  lire_distant
  sha=$(git -C "$DEPOT" rev-parse --verify "$ref^{commit}")
  version=$(version_de "$sha")
  [ -n "$version" ] || { echo "pas de version dans pyproject.toml de ${sha:0:7}" >&2; exit 1; }
  if [ -n "$attendue" ] && [ "$version" != "$attendue" ]; then
    echo "${sha:0:7} porte la version $version, pas $attendue : rien n'est publié" >&2
    exit 1
  fi
  tag="v$version"
  etape "$tag : ${sha:0:7} $(git -C "$DEPOT" log -1 --format=%s "$sha")"
  git -C "$DEPOT" merge-base --is-ancestor "$sha" "$DISTANT/main" \
    || { echo "${sha:0:7} n'est pas sur $DISTANT/main : rien n'est publié" >&2; exit 1; }
  distant=$(commit_distant "$tag")
  publiee=0
  if gh release view "$tag" -R "$(depot_github)" >/dev/null 2>&1; then
    publiee=1
  else
    # les notes sont lues et validées avant tout effet de bord : pas
    # d'étiquette poussée sans release possible (#348 r1)
    notes=$(notes_de "$version" "$sha") || exit 1
  fi
  if [ -n "$distant" ]; then
    [ "$distant" = "$sha" ] || { echo "$tag est déjà posée sur ${distant:0:7}, pas ${sha:0:7} : une étiquette publiée ne se déplace pas (une nouvelle version passe par un nouveau numéro dans pyproject.toml)" >&2; exit 1; }
    # rapatriée ici seulement maintenant, les notes validées
    a_blanc || git -C "$DEPOT" fetch -q --no-tags "$DISTANT" "refs/tags/$tag:refs/tags/$tag" \
      || { echo "$tag est posée ici autrement que sur le distant : à examiner (git tag -d $tag si elle est fausse)" >&2; exit 1; }
    echo "étiquette : déjà posée"
  else
    if ici=$(git -C "$DEPOT" rev-parse -q --verify "refs/tags/$tag^{commit}"); then
      # posée ici par une publication dont la poussée a échoué : poussée maintenant
      [ "$ici" = "$sha" ] || { echo "$tag est posée ici sur ${ici:0:7}, pas ${sha:0:7}, et absente du distant : à examiner (git tag -d $tag si elle est fausse)" >&2; exit 1; }
      echo "étiquette : posée ici, absente du distant"
    else
      fait git -C "$DEPOT" tag -a "$tag" -m "ameesh $version" "$sha"
    fi
    fait git -C "$DEPOT" push "$DISTANT" "refs/tags/$tag"
  fi
  if [ "$publiee" = 1 ]; then
    echo "release : déjà publiée"
    return 0
  fi
  # « Latest » ne va qu'à la plus haute version : un rattrapage ne la vole pas,
  # ni ne la donne en passant à une version que la suite dépasse
  plus_haute=$( (derniere_version; echo "$version"; echo "${PREVUE:-}") | sort -V | tail -1)
  if a_blanc; then
    echo "release $tag (latest : $([ "$plus_haute" = "$version" ] && echo oui || echo non)), notes :"
    sed 's/^/  | /' <<<"$notes"
  else
    gh release create "$tag" -R "$(depot_github)" --verify-tag --title "ameesh $tag" \
      --notes "$notes" --latest="$([ "$plus_haute" = "$version" ] && echo true || echo false)"
  fi
}

# Les commits de main (premier parent) où pyproject.toml change de version,
# du plus ancien au plus récent : « <sha> <version> »
versions_de_main() {
  local sha avant=""
  git -C "$DEPOT" log --reverse --first-parent --format=%H -G'^version = ' \
      "$DISTANT/main" -- pyproject.toml \
    | while read -r sha; do
        v=$(version_de "$sha")
        [ -n "$v" ] && [ "$v" != "$avant" ] && echo "$sha $v"
        avant=$v
      done
}

rattraper() {
  local sha version distant versions
  lire_distant
  versions=$(versions_de_main)
  # la plus haute version de main, publiée à la fin de ce rattrapage
  PREVUE=$(awk '{ print $2 }' <<<"$versions" | sort -V | tail -1)
  while read -r sha version; do
    [ -n "$sha" ] || continue           # aucune version dans main
    # une étiquette déjà posée sur le distant fait foi, même sur un commit plus
    # tardif (v1.0.0 : un correctif fusionné après le numéro), si ce commit
    # porte bien cette version (publier le vérifie) ; elle n'est pas déplacée
    distant=$(commit_distant "v$version")
    [ -z "$distant" ] || sha=$distant
    ( publier "$sha" "$version" ) || exit 1
  done <<<"$versions"
}

case "${1:-}" in
  publier) publier "${2:-HEAD}" ;;
  rattraper) rattraper ;;
  verifier) verifier ;;
  sauvegarder) sauvegarder ;;
  installer) installer "${2:-HEAD}" ;;
  migrer) shift; migrer "$@" ;;
  role) role ;;
  redemarrer) redemarrer ;;
  controler) controler ;;
  retour) retour ;;
  *) sed -n '4,35p' "$0" >&2; exit 2 ;;
esac
