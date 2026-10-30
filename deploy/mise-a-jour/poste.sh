#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Mise à jour d'ameesh vers L60–L74, côté POSTE, étape par étape. Procédure
# complète et raisons : docs/BASCULE.md, « Mise à jour vers L60–L74 ».
#
#   deploy/mise-a-jour/poste.sh <étape> [REF]
#
#   verifier     contrôles, rien n'est modifié ; affiche les bases visées
#   sauvegarder  dump de la base du mesh sur la VM (copie gardée), rapatrié
#                compressé, puis pg_dump de l'ancienne base
#   migrer [--additif-seulement] [REF]
#                migrations de REF (défaut HEAD, comme installer), lancées
#                depuis son instantané AVANT de l'installer : base du mesh
#                (UNE fois), puis ancienne
#                base ; refusé si l'une n'est pas additive — garde inconditionnelle,
#                MIGRER_APRES ne la désactive jamais (docs/BASCULE.md, L232b) ;
#                --additif-seulement garde ce refus même quand REF est déjà
#                installé (staging : des VM tournent encore le code précédent) ;
#                si la sonde d'auto-prise du code installé échoue après la
#                migration, installe REF et redémarre aussitôt (#2357) ;
#                MIGRER_SANS_REDEMARRAGE=1 (staging) laisse la séquence par
#                hôte faire cette installation/redémarrage, VM d'abord
#   installer    instantané du code REF dans ~/.local/share/ameesh/src-<sha>,
#                venv versionné préparé à l'écart (dépendance psycopg comprise)
#                puis bascule atomique du venv (#1969) ; refusé tant qu'une
#                base n'a pas les migrations de REF
#   role         rôle superviseur réappliqué, sur chaque base où il existe ;
#                JIT de Postgres coupé par défaut sur la base du mesh (#3503)
#   redemarrer   exécuteurs du poste un par un (arrêt prouvé avant chaque
#                démarrage), puis notify, puis les exécuteurs de l'ancienne base
#                refusé depuis une unité ameesh-* (risque de tuer le script)
#   timers       pose les minuteries mécaniques EP5 (veille GitHub, CI,
#                projection) sur cet hôte s'il est candidat du rôle élu ;
#                refuse explicitement les passes dont la clé `github` manque
#                (#3327)
#   controler    doctor, budget, accounts list, projects, alerts ; vérifie aussi
#                le venv réellement exécuté par le MainPID de notify et de
#                chaque exécuteur (#2950, #3311), et les minuteries de rôle
#                (EP5) vérifiées par `role timers --check`
#   retour       bascule en arrière vers le venv précédent conservé (sans pip),
#                puis redémarre (sans toucher aux bases : 0041 à 0047 sont
#                additives)
#   publier      étiquette annotée v<version> sur REF (un commit de main, la
#                version de son pyproject.toml), poussée, puis release GitHub
#                (notes : docs/versions/<version>.md de main, sinon la PR qui a
#                fusionné ce commit) ; actifs : la roue, runtime-pins.txt,
#                runtime-build-pins.txt et SHA256SUMS du commit, construits
#                avant l'étiquette (#4786 ; aucun avant runtime-pins.txt) ;
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
# REF : commit, branche ou étiquette du dépôt qui contient ce script.
# Défaut HEAD pour migrer et installer (#1245) ; le contrôleur de release L232b,
# lui, migre toujours l'instantané de son commit épinglé. MIGRER_APRES=1
# ne désactive jamais la garde additive de migrer (docs/BASCULE.md, L232b).
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
#: dumps produits sur la VM par ameesh-sauvegarde.service (0700, 0600)
SAUVEGARDES_VM=${SAUVEGARDES_VM:-/home/ameesh/sauvegardes}
SAUVEGARDES_VM=${SAUVEGARDES_VM%/}
#: borne du rapatriement du dump serveur (secondes) ; la progression et les
#: octets déjà écrits sont journalisés par le récepteur en cas d'échec
RECUPERATION_TIMEOUT=${RECUPERATION_TIMEOUT:-3600}
case "${1:-}" in
  publier|rattraper) ;;
  *) mkdir -p "$TRAVAIL" "$SAUVEGARDES"; chmod 700 "$TRAVAIL" ;;
esac

etape() { printf '\n== %s\n' "$*"; }
ssh_vm() { ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$VM" "$@"; }
#: même canal, borné par un délai : la récupération d'un dump ne peut pas
#: retenir le staging indéfiniment si le lien WireGuard se bloque.
ssh_vm_borne() {
  local delai=$1; shift
  timeout --foreground "$delai" ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$VM" "$@"
}
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
# #4437 : c'est le périmètre commun de `redemarrer` (redemarrer_liste) et de
# `controler` (controler_executeurs). Le registre ne décide que du statut :
# une persona arrêtée n'est pas réactivée, mais son unité encore active est
# rechargée (#1633) puis contrôlée sur le venv, comme toute unité active.
unites_mesh() {
  systemctl --user list-units 'ameesh-runner-agent@*.service' --state=active \
    --plain --no-legend | awk '{print $1}'
}
# persona d'une unité : son ExecStart porte « --agents <p> »
persona_de() {
  systemctl --user show -p ExecStart --value "$1" \
    | grep -o -- '--agents [^ ;]*' | head -1 | awk '{print $2}'
}
# #4358 : une lecture vide ou illisible (relève d'un rôle élu, timeout) est
# relue 3 fois, puis rendue « illisible » : un non-concluant, jamais une
# JSONDecodeError qui arrête le script sous `set -e`.
statut() {  # statut de la persona $2 vue par la configuration $1 (« mesh » ou « ancienne »)
  local sortie essai
  for essai in 1 2 3; do
    if [ "$1" = ancienne ]; then sortie=$(ameesh_ancienne show "$2" --json) || sortie=
    else sortie=$("$AMEESH" show "$2" --json) || sortie=; fi
    python3 -c 'import json,sys
r = json.loads(sys.stdin.read())
if not isinstance(r, dict): sys.exit(1)
print(r.get("status") or "")' <<<"$sortie" 2>/dev/null && return 0
    [ "$essai" = 3 ] || sleep 3
  done
  echo illisible
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

# Démarre la sauvegarde serveur (copie gardée sur la VM) et atteste le dump
# qu'elle vient de produire : chemin, taille et SHA-256, sur trois lignes.
# Le service publie son fichier par lien dur : un dump visible est complet.
# Un script serveur antérieur réécrit le dump du jour sous le même nom : un
# dump est donc reconnu nouveau à son nom, son inode, sa taille ou sa date (#2424).
# Aucun DSN ni secret ne traverse cette commande distante.
sauvegarde_serveur() {
  printf '%s\n' "$SAUVEGARDES_VM" | ssh_vm 'set -eu
    IFS= read -r d
    test -d "$d"
    before=$(mktemp "$d/.liste-avant.XXXXXX")
    after=$(mktemp "$d/.liste-apres.XXXXXX")
    trap "rm -f -- \"$before\" \"$after\"" EXIT HUP INT TERM
    find "$d" -maxdepth 1 -type f -name "ameesh-*.dump" -printf "%f %i %s %T@\n" | sort > "$before"
    systemctl start ameesh-sauvegarde.service
    test "$(systemctl show ameesh-sauvegarde.service -p Result --value)" = success
    test "$(systemctl show ameesh-sauvegarde.service -p ExecMainStatus --value)" = 0
    find "$d" -maxdepth 1 -type f -name "ameesh-*.dump" -printf "%f %i %s %T@\n" | sort > "$after"
    new=$(comm -13 "$before" "$after" | tail -1 | cut -d" " -f1)
    if [ -z "$new" ]; then
      echo "ameesh-sauvegarde : aucun nouveau dump dans $d" >&2
      exit 1
    fi
    f="$d/$new"
    test -f "$f"
    printf "%s\n" "$f"
    stat -c %s -- "$f"
    sha256sum -- "$f" | cut -d" " -f1
  '
}

sauvegarder() {
  local jour sortie meta statut distant taille sha statuts dsn
  jour=$(date +%F-%H%M)
  sortie="$SAUVEGARDES/mesh-$jour-avant-l60-l74.dump"
  case "$RECUPERATION_TIMEOUT" in
    ''|*[!0-9]*|0) echo "RECUPERATION_TIMEOUT doit être un entier positif" >&2; return 2 ;;
  esac
  # #518 : le DSN ambigu est refusé (garde fail-closed du code déployé) avant
  # toute sauvegarde, comme pour « migrer ».
  dsn_part dsn "$CONFIG" dsn || return
  [ -n "$dsn" ] || { echo "DSN de la base du mesh illisible" >&2; return 1; }
  etape "base du mesh, sauvegarde du serveur (copie gardée sur la VM)"
  # Une oneshot réussie est inactive : vérifier le résultat, pas is-active.
  # Un échec de backup/upload doit arrêter cette étape avant les migrations ;
  # son code de sortie est propagé tel quel (banc : BACKUP_START_STATUS).
  if meta=$(sauvegarde_serveur); then :; else
    statut=$?
    echo "sauvegarde serveur en échec (statut $statut) : rien n'a été modifié" >&2
    return "$statut"
  fi
  distant=$(sed -n '1p' <<<"$meta")
  taille=$(sed -n '2p' <<<"$meta")
  sha=$(sed -n '3p' <<<"$meta")
  case "$distant" in
    "$SAUVEGARDES_VM"/ameesh-*.dump) ;;
    *) echo "sauvegarde serveur : chemin de dump inattendu" >&2; return 1 ;;
  esac
  case "$taille" in ''|*[!0-9]*|0) echo "sauvegarde serveur : taille invalide" >&2; return 1 ;; esac
  case "$sha" in
    ''|*[!0-9a-f]*) echo "sauvegarde serveur : empreinte invalide" >&2; return 1 ;;
  esac
  [ "${#sha}" -eq 64 ] || { echo "sauvegarde serveur : empreinte invalide" >&2; return 1; }
  etape "rapatriement compressé du dump serveur ($taille octets)"
  # Le dump est déjà sur la VM ; on ne fait plus traverser WireGuard à pg_dump.
  # gzip borne le débit utile ; le récepteur vérifie taille et SHA-256 avant
  # de publier le fichier par lien dur, et journalise les octets écrits.
  set +e
  printf '%s\n' "$distant" | ssh_vm_borne "$RECUPERATION_TIMEOUT" \
      'IFS= read -r f; gzip -1 -c -- "$f"' \
    | "$VENV/bin/python" "$ICI/sauvegarder-prive.py" pull --output "$sortie" \
        --sha256 "$sha" --bytes "$taille" --timeout "$RECUPERATION_TIMEOUT"
  statuts=("${PIPESTATUS[@]}")
  set -e
  # trois maillons : l'écriture du chemin, la source ssh bornée, le récepteur.
  if [ "${statuts[2]}" != 0 ]; then
    echo "rapatriement en échec : récepteur ${statuts[2]}, source ${statuts[1]}" >&2
    return 1
  fi
  if [ "${statuts[1]}" != 0 ]; then
    if [ "${statuts[1]}" = 124 ]; then
      echo "rapatriement en échec : délai de ${RECUPERATION_TIMEOUT} s dépassé" >&2
    else
      echo "rapatriement en échec : source ${statuts[1]}" >&2
    fi
    return 1
  fi
  if [ "${statuts[0]}" != 0 ]; then
    echo "rapatriement en échec : écriture du chemin ${statuts[0]}" >&2
    return 1
  fi
  ls -l "$sortie"
  if ancienne; then
    etape "ancienne base"
    dsn_part dsn "$CONFIG_RESTE" dsn || return
    "$VENV/bin/python" "$ICI/sauvegarder-prive.py" dump --config "$CONFIG_RESTE" \
      --output "$SAUVEGARDES/ancienne-$jour-avant-l60-l74.dump"
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
  local ref=${1:-HEAD} cible court precedent_court=
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
  if [ -s "$TRAVAIL/precedent" ]; then
    precedent_court=$(basename "$(cat "$TRAVAIL/precedent")"); precedent_court=${precedent_court#src-}
  fi
  # #1969 : jamais de pip dans le venv vivant. Le venv de $court est préparé à
  # l'écart (pip + contrôles d'imports, de point d'entrée et de shebang), puis
  # la bascule n'est qu'un rename : un appel concurrent des agents voit
  # l'ancien ou le nouveau code entier, jamais un état incomplet. La première
  # conversion adopte le venv réel comme version précédente, sans pip.
  etape "préparation isolée du venv de $court (code puis dépendances, dont psycopg et cryptography, #3619)"
  python3 "$ICI/bascule.py" preparer --venv "$VENV" --src "$cible" --version "$court"
  etape "bascule atomique du venv et du code courant"
  python3 "$ICI/bascule.py" activer --venv "$VENV" --version "$court" \
    ${precedent_court:+--precedent "$precedent_court"}
  ln -sfn "src-$court" "$PARTAGE/src-current"
  "$VENV/bin/python" -c 'import psycopg, cryptography, ameesh.budget, ameesh.projects; print("psycopg", psycopg.__version__, "crypto", cryptography.__version__, ": modules L70 et L62 présents")'
  echo "installé : src-$court. Les exécuteurs tournent encore l'ancien code : redemarrer, sans tarder."
}

# #2357: emergency restart after a failed compatibility probe. Launched from an
# agent's own runner, a direct « redemarrer » would stop that runner before the
# following units (docs/AUDITEUR.md): throw the restart into a transient unit
# when the user manager accepts it, and only fall back to the direct call in a
# plain shell or where systemd-run is unavailable (tests, CI).
redemarrer_urgence() {
  local unite="poste-maj-urgence-$$" env_args=()
  [ -n "${BASCULE_ENV:-}" ] && env_args+=(--setenv=BASCULE_ENV="$BASCULE_ENV")
  [ -n "${AMEESH_CONFIG:-}" ] && env_args+=(--setenv=AMEESH_CONFIG="$AMEESH_CONFIG")
  if command -v systemd-run >/dev/null 2>&1 \
     && systemd-run --user --collect --wait --unit "$unite" \
          ${env_args[@]+"${env_args[@]}"} \
          /bin/sh -c '"$0" redemarrer' "$ICI/poste.sh"; then
    echo "redémarrage terminé dans l'unité transitoire $unite ; journalctl --user -u $unite" >&2
    return 0
  fi
  echo "unité transitoire indisponible ou en échec : tentative directe, refusée depuis une unité ameesh-*." >&2
  # A fresh dispatcher preserves errexit even when this function is tested
  # by `if ! redemarrer_urgence` (Bash disables it throughout that call).
  "$ICI/poste.sh" redemarrer
}

# Migrations du commit REF (défaut HEAD, comme installer) : garde additive même
# si ce code est déjà installé. Le lien src-current n'accorde aucune autorité de
# migration. --additif-seulement reste accepté pour les appels de staging (#1155).
migrer() {
  local ref cible d garde=(--additive-only) sha incompatible=0
  if [ "${1:-}" = "--additif-seulement" ]; then
    shift
  fi
  ref=${1:-HEAD}
  sha=$(git -C "$DEPOT" rev-parse --verify "$ref^{commit}")
  cible=$(instantane "$sha")
  etape "migrations du commit $sha, additives seulement"
  etape "base du mesh (une seule fois pour tout le mesh)"
  dsn_part d "$CONFIG" dsn || return
  echo "base : $d"
  ameesh_de "$cible" migrate --dry-run --additive-only
  migrer_base "$cible" ${garde[@]+"${garde[@]}"} || incompatible=1
  if ancienne; then
    etape "ancienne base (ses exécuteurs prennent le même code)"
    dsn_part d "$CONFIG_RESTE" dsn || return
    echo "base : $d"
    AMEESH_CONFIG="$CONFIG_RESTE" migrer_base "$cible" ${garde[@]+"${garde[@]}"} || incompatible=1
  fi
  # #2357: after a failed compatibility probe the OLD code can no longer serve
  # the migrated schema. The target snapshot already passed doctor --probe,
  # so install it and restart at once instead of stopping and leaving the
  # incident window open (72 min of DbError auto-take on 18/10).
  if [ "$incompatible" = 1 ]; then
    if [ "${MIGRER_SANS_REDEMARRAGE:-0}" = 1 ]; then
      # staging.sh runs this step before its per-host sequence (VMs first,
      # poste last, canon §14): it continues at once and does the installation
      # and restart itself, in that order, instead of being stopped here.
      echo "sonde d'auto-prise en échec : la séquence staging poursuit sans s'arrêter (installation/redémarrage des hôtes, VM d'abord, poste en dernier)." >&2
      return 0
    fi
    etape "sonde d'auto-prise en échec : installation et redémarrage immédiats (base déjà migrée)"
    if installe "$cible"; then
      # The venv already holds REF (re-run, or MIGRER_APRES=1): do not touch
      # the recorded previous version, only load REF into the runners.
      echo "code de $cible déjà installé : redémarrage immédiat sans réinstallation." >&2
    # Keep installer's fail-fast shell: a conditional function call disables
    # errexit, allowing a failed venv preparation to reach activation.
    elif ! "$ICI/poste.sh" installer "$sha"; then
      echo "ÉCHEC de l'installation immédiate : l'ancien code sert encore le schéma migré." >&2
      echo "Traiter tout de suite (installer/redémarrer à la main, correction compatible)." >&2
      return 1
    fi
    if ! redemarrer_urgence; then
      echo "ÉCHEC du redémarrage immédiat : l'ancien code tourne encore sur le schéma migré." >&2
      echo "Redémarrer à la main sans délai, puis vérifier l'auto-prise." >&2
      return 1
    fi
    echo "Code ${cible##*/src-} installé et exécuteurs du poste redémarrés sans attendre." >&2
    echo "La base est migrée pour tout le mesh : installer et redémarrer les autres hôtes sans délai (vm.sh, ou la séquence staging)." >&2
  fi
}
migrer_base() {
  local cible=$1 court code=0; shift
  court=${cible##*/src-}
  ameesh_de "$cible" migrate "$@" || code=$?
  if [ "$code" = 3 ]; then
    echo "migration non additive : le code en place casserait sur la base migrée." >&2
    echo "Plan non additif séparé à faire approuver ; migrer conserve toujours la garde additive." >&2
  fi
  [ "$code" = 0 ] || exit "$code"
  ameesh_de "$cible" doctor --probe || exit "$?"
  # #2339: run the INSTALLED code, not the migration snapshot, read-only.
  # #2357: a failure no longer stops the update; migrer installs the target
  # code and restarts immediately (the database is already migrated).
  etape "auto-prise du code installé sur la base migrée (lecture seule)"
  # #2359: exit 2 = the probe could not set up the installed runner. Unknown
  # compatibility is handled like a failure: installing REF is the safe move.
  local sonde=0
  "$VENV/bin/python" -I "$ICI/auto-take-probe.py" || sonde=$?
  case "$sonde" in
    0) return 0 ;;
    2) echo "compatibilité de l'ancien code non vérifiée (sonde inutilisable) : traitée comme un échec." >&2 ;;
    *) echo "ancien code incompatible avec le schéma migré (sonde en échec)." >&2 ;;
  esac
  echo "La base est DÉJÀ migrée : la mise à jour doit enchaîner tout de suite sur l'installation et le redémarrage du code $court." >&2
  return 1
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

reapply_jit_off() {
  # #3503 : le JIT de Postgres compilait des plans à coût estimé élevé sur de
  # très petites tables (_unreachable_lots : 42 s contre 0,2 s, et toutes les
  # passes de réassignation des relecteurs expiraient). ameesh coupe le JIT
  # dans chaque session ; ce réglage de la base et du rôle de connexion couvre
  # les sessions qui ne passent pas par ameesh (psql, script de déploiement,
  # nouveau rôle).
  local existe
  existe=$(ssh_vm "runuser -u postgres -- psql -X -At -d ameesh -c \"SELECT 1 FROM pg_roles WHERE rolname = 'ameesh'\"")
  if [ "$existe" = 1 ]; then
    ssh_vm "runuser -u postgres -- psql -X -q -d ameesh -v ON_ERROR_STOP=1 -c 'ALTER ROLE ameesh SET jit = off'"
  fi
  ssh_vm "runuser -u postgres -- psql -X -q -d ameesh -v ON_ERROR_STOP=1 -c 'ALTER DATABASE ameesh SET jit = off'"
  echo "JIT de Postgres coupé par défaut (rôle ameesh, base du mesh)"
}

reapply_idle_in_transaction_guard() {
  # #3836 : un client resté `idle in transaction` sept minutes (gel #3700,
  # 27/10) tenait le verrou du dépôt de migration et le verrou consultatif du
  # projet ; toutes les écritures du mesh ont attendu. La garde serveur coupe
  # la session après 3 min : bien au-delà du travail local que fait un gel
  # depuis que la lecture de provenance ne va plus au réseau dans sa
  # transaction, et bien en deçà de l'incident. Elle couvre aussi les clients
  # antérieurs au correctif (psql, script, runner non redémarré).
  local valeur=180s
  local existe
  existe=$(ssh_vm "runuser -u postgres -- psql -X -At -d ameesh -c \"SELECT 1 FROM pg_roles WHERE rolname = 'ameesh'\"")
  if [ "$existe" = 1 ]; then
    ssh_vm "runuser -u postgres -- psql -X -q -d ameesh -v ON_ERROR_STOP=1 -c \"ALTER ROLE ameesh SET idle_in_transaction_session_timeout = '$valeur'\""
  fi
  ssh_vm "runuser -u postgres -- psql -X -q -d ameesh -v ON_ERROR_STOP=1 -c \"ALTER DATABASE ameesh SET idle_in_transaction_session_timeout = '$valeur'\""
  echo "garde idle-in-transaction posée par défaut ($valeur, rôle ameesh et base du mesh)"
}

role() {
  local existe dsn
  reapply_executor_role
  etape "réglage JIT de la base du mesh (#3503)"
  reapply_jit_off
  etape "garde idle-in-transaction de la base du mesh (#3836)"
  reapply_idle_in_transaction_guard
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
  local sortie essai
  for essai in 1 2 3; do
    if [ "$1" = ancienne ]; then sortie=$(ameesh_ancienne show "$2" --json) || sortie=
    else sortie=$("$AMEESH" show "$2" --json) || sortie=; fi
    python3 -c 'import json,sys
r = json.loads(sys.stdin.read())
if not isinstance(r, dict): sys.exit(1)
print(json.dumps(r))' <<<"$sortie" 2>/dev/null && return 0
    [ "$essai" = 3 ] || sleep 3
  done
  return 3  # #4358 : lecture non concluante, aucun redémarrage
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
# #1152 : arrêt PROUVÉ avant tout démarrage. Le SIGTERM de `stop` ferme
# l'admission : le tour en cours se termine (drainage borné par
# `drain_seconds`, sous `TimeoutStopSec`), plus aucun nouveau tour ne part. On
# ne décide pas d'arrêter sur une lecture de statut — un tour peut partir juste
# après elle — et on ne relance jamais au-dessus d'un processus vivant.
# Only inactive with MainPID=0 proves the safe point. Clear a preexisting
# failure BEFORE stop (#1907), so a new failure during drainage stays visible.
redemarrer_hors_tour() {  # unité — 0 quand l'arrêt est prouvé puis l'unité démarrée
  local u=$1 etat pid
  etat=$(systemctl --user show -p ActiveState --value "$u") \
    || { echo "$u : lecture d'ActiveState impossible — aucun démarrage" >&2; return 1; }
  if [ "$etat" = failed ]; then
    systemctl --user reset-failed "$u" \
      || { echo "$u : reset-failed échoue — aucun démarrage" >&2; return 1; }
  fi
  systemctl --user stop "$u" \
    || { echo "$u : l'arrêt échoue — journalctl --user -u $u -n 50 ; puis « $0 retour »" >&2; return 1; }
  etat=$(systemctl --user show -p ActiveState --value "$u") || return 1
  pid=$(systemctl --user show -p MainPID --value "$u") || return 1
  if [ "$etat" != inactive ] || [ "$pid" != 0 ]; then
    echo "$u : arrêt non prouvé (ActiveState=$etat, MainPID=$pid) — aucun démarrage ; journalctl --user -u $u -n 50 ; puis « $0 retour »" >&2
    return 1
  fi
  systemctl --user start "$u" \
    || { echo "$u : ne redémarre pas — journalctl --user -u $u -n 50 ; puis « $0 retour »" >&2; return 1; }
}

# Redémarre les unités $2… (base $1 : mesh | ancienne) une par une. L'admission
# de chaque persona est fermée par `stop` (SIGTERM) AVANT tout démarrage : le
# tour en cours draine, borné par TimeoutStopSec, et aucun nouveau tour ne
# part. On n'attend plus un créneau « idle » : un statut `running` à chaque
# lecture faisait expirer la mise à jour sans même arrêter l'unité (starvation
# de l'incident impl07, #1152/R1). Le statut sert encore à la garde `stopped`
# (#1131) : une persona arrêtée par le propriétaire n'est pas relancée ; son
# unité encore active est seulement rechargée sur le nouveau code (#1633),
# sans bail ni tour.
redemarrer_liste() {
  local base=$1; shift
  local u p st avant ligne heure tenu attendu rc
  for u in "$@"; do
    p=$(persona_de "$u")
    [ -n "$p" ] || { echo "$u : persona introuvable dans ExecStart" >&2; exit 1; }
    st=$(statut "$base" "$p")
    if [ "$st" = illisible ]; then
      echo "WARNING $p: registre illisible (lecture vide) — unité $u laissée en l'état (non concluant, sans retour)"
      continue
    fi
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
    # #502: only a runner that held its lease must take it again (bail.sh)
    rc=0; ligne=$(lire_bail "$base" "$p") || rc=$?
    if [ "$rc" = 3 ]; then
      echo "WARNING $p: bail illisible — unité $u laissée en l'état (non concluant, sans retour)"
      continue
    fi
    [ "$rc" = 0 ] && heure=$(heure_base "$base") \
      && attendu=$(proprietaire_de "$u") \
      || { echo "$u: registry, database clock or ExecStart unreadable — nothing restarted" >&2; exit 1; }
    avant=$(bail_epoque <<<"$ligne")
    tenu=0
    bail_valide -1 "$attendu" "$heure" <<<"$ligne" && tenu=1
    # #1152 : arrêt PROUVÉ (SIGTERM, drainage) avant le démarrage ; la course
    # entre la lecture de statut et le lancement réel est fermée par le
    # drainage, jamais par cette lecture.
    redemarrer_hors_tour "$u" || exit 1
    # Catch an immediate crash after start; the stop proof does not cover it.
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
}

# #2950 : ne jamais arrêter le cgroup qui héberge le script de redémarrage.
# Le nom de l'unité transitoire d'urgence est hors de cet espace ; un appel
# direct depuis un agent est refusé AVANT tout arrêt (y compris le repli #2357).
verifier_hors_agent() {
  local groupes
  groupes=$(cat "/proc/$$/cgroup") \
    || { echo "cgroup illisible : redémarrage refusé, aucun arrêt effectué" >&2; return 1; }
  [ -n "$groupes" ] \
    || { echo "cgroup vide : redémarrage refusé, aucun arrêt effectué" >&2; return 1; }
  if [[ "$groupes" =~ /ameesh-[^/[:space:]]*\.(service|scope)(/|$|[[:space:]]) ]]; then
    echo "redémarrage refusé depuis une unité ameesh-* : le script serait tué avec son exécuteur." >&2
    echo "Relancer depuis un terminal indépendant ou une unité transitoire poste-maj-* (systemd-run --user --collect --unit poste-maj-<identifiant>), puis terminer le tour ; suivre son journal." >&2
    return 1
  fi
}

redemarrer() {
  verifier_hors_agent || return 1
  # shellcheck source=bail.sh
  . "$ICI/bail.sh"
  local unites
  non_verifiees=()
  mapfile -t unites < <(unites_mesh)
  etape "exécuteurs du poste, base du mesh (${#unites[@]}), arrêt prouvé avant démarrage"
  [ ${#unites[@]} -gt 0 ] && redemarrer_liste mesh "${unites[@]}"
  etape "ameesh-notify"
  systemctl --user restart ameesh-notify.service
  sleep 5; systemctl --user is-active ameesh-notify.service
  if [ -n "$UNITES_RESTE" ]; then
    # shellcheck disable=SC2206
    unites=($UNITES_RESTE)
    etape "exécuteurs de l'ancienne base (${#unites[@]}), arrêt prouvé avant démarrage"
    ancienne || { echo "$CONFIG_RESTE absent : ces unités ne savent pas quelle base lire" >&2; exit 1; }
    redemarrer_liste ancienne "${unites[@]}"
  fi
  if [ ${#non_verifiees[@]} -gt 0 ]; then
    etape "WARNING: ${#non_verifiees[@]} persona(s) not checked (no lease before the restart, no sign of life): ${non_verifiees[*]}"
  fi
}

# L'ExecStart peut pointer vers le lanceur stable alors que le processus
# vivant garde l'ancien venv. Ni ExecStart ni /proc/PID/exe seuls ne le prouvent.
controler_notify() {
  local pid apres
  systemctl --user is-active --quiet ameesh-notify.service || return 1
  pid=$(systemctl --user show -p MainPID --value ameesh-notify.service) || return 1
  python3 "$ICI/venv-processus.py" "$pid" "$VENV" ameesh-notify || return 1
  apres=$(systemctl --user show -p MainPID --value ameesh-notify.service) || return 1
  [ "$pid" = "$apres" ] \
    || { echo "ameesh-notify : MainPID changé pendant le contrôle, relancer controler" >&2; return 1; }
}

# #3311 : le lanceur stable (#1969) ne prouve rien pour un exécuteur déjà
# lancé — après une bascule, il peut rester sur l'ancien venv et l'hôte
# paraître sain. Chaque MainPID vivant doit exécuter le venv courant.
controler_executeurs() {
  local u pid apres echec=0
  while IFS= read -r u; do
    [ -n "$u" ] || continue
    pid=$(systemctl --user show -p MainPID --value "$u") \
      || { echo "$u : MainPID illisible — venv non prouvé" >&2; echec=1; continue; }
    python3 "$ICI/venv-processus.py" "$pid" "$VENV" "$u" \
      || { echec=1; continue; }
    apres=$(systemctl --user show -p MainPID --value "$u") \
      || { echo "$u : MainPID illisible après la preuve, relancer controler" >&2; echec=1; continue; }
    [ "$pid" = "$apres" ] \
      || { echo "$u : MainPID changé pendant le contrôle, relancer controler" >&2; echec=1; }
  done < <(unites_mesh)
  [ "$echec" = 0 ]
}

timers() {
  # #3327 : le poste est candidat du rôle élu ; ses minuteries mécaniques
  # remplacent les passes GitHub du service complet, limité aux alertes.
  etape "minuteries de rôle (EP5)"
  "$AMEESH" role timers --install
}

controler() {
  local echecs=()
  verif() { etape "$*"; "$@" || echecs+=("$*"); }
  # #3345 : les profils des runners de ce poste (orchestrateur, mesh-design,
  # auditeur) doivent s'accorder avec config.json sur train, github, release ;
  # un profil resté en shadow ou sans github faisait tourner ces runners à
  # côté du reste de l'hôte. Contrôle sans base, avant le doctor complet.
  verif "$AMEESH" doctor --profiles
  verif "$AMEESH" doctor
  verif "$AMEESH" budget
  verif "$AMEESH" accounts list
  verif "$AMEESH" projects
  verif "$AMEESH" alerts
  verif controler_notify
  verif controler_executeurs
  verif "$AMEESH" role timers --check
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
  verifier_hors_agent || return 1
  local precedent
  precedent=$(cat "$TRAVAIL/precedent" 2>/dev/null || true)
  [ -n "$precedent" ] && [ -d "$PARTAGE/$precedent" ] \
    || { echo "version précédente inconnue ($TRAVAIL/precedent)" >&2; exit 1; }
  etape "retour par la bascule, sans pip : $precedent"
  # #1969 : le venv précédent est conservé entier sur disque ; le retour est la
  # même bascule en arrière, pas une réinstallation dans le venv vivant.
  python3 "$ICI/bascule.py" retour --venv "$VENV"
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

# actifs de la release du commit $1 (#4786) dans le répertoire $2 (neuf) : la
# roue ameesh construite depuis ce commit, runtime-pins.txt,
# runtime-build-pins.txt et SHA256SUMS, provenance publiée du runtime root du
# handoff (construction-runtime.py --assets, même roue et mêmes épinglages)
construire_actifs() {
  python3 "$ICI/construction-runtime.py" --assets --clone "$DEPOT" --sha "$1" --output "$2" \
    || { echo "actifs de ${1:0:7} non construits : pas de release" >&2; return 1; }
  ( cd "$2" && sha256sum --quiet --strict -c SHA256SUMS ) \
    || { echo "SHA256SUMS de ${1:0:7} ne vérifie pas ses actifs : pas de release" >&2; return 1; }
}

# publier REF [VERSION] : VERSION, la version attendue (rattraper) ; un écart
# avec pyproject.toml de REF arrête tout
publier() {
  local ref=${1:-HEAD} attendue=${2:-} sha version tag distant ici notes plus_haute publiee
  local temp actifs=()
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
    # les actifs aussi, avant l'étiquette ; une version antérieure à #4778
    # (sans runtime-pins.txt) n'en a pas
    if git -C "$DEPOT" cat-file -e "$sha:deploy/mise-a-jour/runtime-pins.txt" 2>/dev/null; then
      if a_blanc; then
        echo "actifs : roue ameesh, runtime-pins.txt, runtime-build-pins.txt, SHA256SUMS (construits à la publication)"
      else
        # directement sous le HOME : construction-runtime.py refuse un
        # ancêtre ouvert en écriture au groupe ou à tous (/tmp, un ~/.cache
        # 0775 sous umask 002)
        temp=$(mktemp -d "$HOME/.ameesh-actifs.XXXXXX")
        # shellcheck disable=SC2064
        trap "rm -rf -- '$temp'" EXIT
        construire_actifs "$sha" "$temp/actifs" || exit 1
        actifs=("$temp"/actifs/*)
      fi
    fi
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
      --notes "$notes" --latest="$([ "$plus_haute" = "$version" ] && echo true || echo false)" \
      "${actifs[@]}"
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
  timers) timers ;;
  redemarrer) redemarrer ;;
  controler) controler ;;
  retour) retour ;;
  *) sed -n '4,35p' "$0" >&2; exit 2 ;;
esac
