#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Mise à jour d'ameesh vers L60–L74 sur le SERVEUR du mesh (profil L55),
# lancée depuis le poste, étape par étape. Procédure : docs/BASCULE.md,
# « Mise à jour vers L60–L74 ». AUCUNE migration ici : `poste.sh migrer` l'a
# faite pour toute la base du mesh, qui est celle du serveur.
#
#   deploy/mise-a-jour/vm.sh <étape> [REF]
#   deploy/mise-a-jour/vm.sh agents verifier|installer|redemarrer|controler|retour [REF]
#     AGENTS_SSH=root@cible, HOTE_AGENTS=nom du registre dans BASCULE_ENV —
#     un BASCULE_ENV par VM d'agents ; unités utilisateur sous ameesh, liste
#     du registre lue sur la VM. Voir docs/DEPLOIEMENT-HOTE-AGENTS.md.
#
#   verifier     contrôles, rien n'est modifié
#   installer    code REF dans /opt/ameesh/src (détaché), venv versionné
#                préparé à l'écart (dépendance psycopg comprise) puis bascule
#                atomique du venv (#1969) ; refuse tant que la base du mesh
#                n'a pas toutes les migrations de REF
#   redemarrer   ameesh-runner@<persona>, une par une, chacune hors tour,
#                en attendant la fin du tour au plus ATTENTE_MAX ; après
#                DRAIN_ATTENTE (300 s) une pause opérateur courte (drain) est
#                posée, puis levée dès l'agent redémarré hors tour (#2152) ;
#                geste d'opérateur : à lancer hors session liée à un agent
#                (le CLI local refuse bad_actor), sinon le drain est refusé et
#                le redémarrage de cet agent s'arrête sans tour interrompu ;
#                refuse tant que les migrations du code installé manquent
#   controler    unités actives, journal récent, ameesh doctor sur le serveur
#   retour       bascule en arrière vers le venv précédent conservé, puis
#                redémarre
#
#   En mode agents, installer et retour travaillent dans le layout du compte
#   ameesh (~/.local/share/ameesh : clone src, instantanés src-<sha7> jamais
#   écrasés, previous-sha, venv), avec les mêmes gardes de migrations ; le
#   redémarrage reste hors tour sous bail. L'installation ne redémarre
#   personne, le retour bascule en arrière vers le venv précédent conservé
#   puis redémarre et contrôle.
#   Avant toute mutation, l'identité effective du compte runner (son
#   EnvironmentFile) doit être celle de HOTE_AGENTS, et un journal de
#   récupération retient le code quitté : une installation inachevée ferme la
#   commande, « installer » refuse tant que ce journal existe (le résoudre par
#   « retour »), et « retour » ramène à ce code, même sans previous-sha.
#
# REF (installer) : commit, branche ou étiquette du dépôt qui contient ce
# script ; défaut : HEAD de ce dépôt. Si le serveur ne le trouve pas chez
# l'origine (branche non poussée), il le reçoit en paquet git par SSH.
#
# Paramètres lus dans $BASCULE_ENV (défaut ~/.config/ameesh/bascule.env, celui
# de la bascule L57) : VM (adresse WireGuard), PERSONAS_VM (personas servies
# par le serveur ; liste vide admise, un hôte sans exécutant). Aucun secret
# n'est lu ni affiché. ATTENTE_MAX (7200 s) et DRAIN_ATTENTE (300 s, 0 =
# jamais) se règlent par l'environnement (#2152).
set -euo pipefail
shopt -s inherit_errexit

unset AMEESH_DSN AGENT_MESH_DSN  # le statut des personas se lit sur la base du mesh

BASCULE_ENV=${BASCULE_ENV:-$HOME/.config/ameesh/bascule.env}
[ -r "$BASCULE_ENV" ] || { echo "paramètres absents : $BASCULE_ENV" >&2; exit 2; }
# shellcheck disable=SC1090
. "$BASCULE_ENV"
MODE=serveur
if [ "${1:-}" = agents ]; then MODE=agents; shift; fi
ACTION=${1:-}
if [ "$MODE" = agents ]; then
  case "$ACTION" in
    redemarrer|controler|verifier|installer|retour) ;;
    *) echo "mode agents : verifier, installer, redemarrer, controler ou retour attendu" >&2; exit 2 ;;
  esac
  : "${AGENTS_SSH:?AGENTS_SSH manquant dans $BASCULE_ENV}" "${HOTE_AGENTS:?HOTE_AGENTS manquant dans $BASCULE_ENV}"
  [[ $AGENTS_SSH =~ ^[a-zA-Z0-9_.@:-]+$ && $AGENTS_SSH != -* ]] || { echo "AGENTS_SSH invalide" >&2; exit 2; }
  [[ $HOTE_AGENTS =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || { echo "HOTE_AGENTS invalide" >&2; exit 2; }
else
  # #1928 : PERSONAS_VM défini mais vide est une configuration valide (hôte
  # sans exécutant, ce que staging.sh écrit) ; les boucles serveur savent être
  # vides. Seule une variable absente est refusée.
  : "${VM:?VM manquant dans $BASCULE_ENV}" "${PERSONAS_VM?PERSONAS_VM manquant dans $BASCULE_ENV}"
fi

ICI="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=bail.sh
. "$ICI/bail.sh"
DEPOT="$(git -C "$ICI" rev-parse --show-toplevel)"
AMEESH="$HOME/.local/share/ameesh/venv/bin/ameesh"   # celui du poste, pour lire les statuts
SRC=/opt/ameesh/src
VENV=/opt/ameesh/venv
PRECEDENT=/opt/ameesh/precedent
TRAVAIL="$HOME/.local/state/ameesh/mise-a-jour"
ATTENTE_MAX=${ATTENTE_MAX:-7200}
# #2152 : au-delà de cette attente d'un agent en tour, le redémarrage pose
# lui-même une pause opérateur courte (drain) au lieu d'attendre sans fin un
# agent qui enchaîne les tours de continuation (57 min constatées les 16-17/10).
# La pause prend effet à la fin du tour — aucun tour interrompu —, l'unité est
# redémarrée dès que l'agent est hors tour, puis la pause est levée. 0 = jamais
# (comportement d'avant).
DRAIN_ATTENTE=${DRAIN_ATTENTE:-300}
[[ $DRAIN_ATTENTE =~ ^[0-9]+$ ]] \
  || { echo "DRAIN_ATTENTE invalide : « $DRAIN_ATTENTE » (secondes attendues)" >&2; exit 2; }
mkdir -p "$TRAVAIL"; chmod 700 "$TRAVAIL"

etape() { printf '\n== %s\n' "$*"; }
ssh_vm() { ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$VM" "$@"; }
# #1969 : le helper de bascule atomique est copié depuis le poste, pour que
# l'installation et le retour emploient la même version quel que soit le
# commit déployé (l'ancien code n'a pas forcément le fichier).
# Exécuté en root : jamais dans /tmp (partagé), mais sous /root, privé.
BASCULE_VM=/root/.cache/ameesh/bascule-$$.py
envoyer_bascule_vm() {
  [ -r "$ICI/bascule.py" ] || { echo "bascule.py introuvable : $ICI/bascule.py" >&2; return 1; }
  ssh_vm "umask 077; mkdir -p ${BASCULE_VM%/*} && cat > $BASCULE_VM" < "$ICI/bascule.py"
}
BASCULE_AGENTS=/home/ameesh/.local/state/ameesh/mise-a-jour/bascule-$$.py
envoyer_bascule_agents() {
  local b64
  [ -r "$ICI/bascule.py" ] || { echo "bascule.py introuvable : $ICI/bascule.py" >&2; return 1; }
  b64=$(base64 -w0 "$ICI/bascule.py") || return 1
  utilisateur_agents bash -c "mkdir -p /home/ameesh/.local/state/ameesh/mise-a-jour
    printf %s '$b64' | base64 -d > $BASCULE_AGENTS
    chmod 600 $BASCULE_AGENTS"
}
# Versions de migration du commit $1 (clone du serveur) absentes de la base
# du mesh, une par ligne ; rien si toutes sont passées.
migrations_absentes() {
  local connues faites
  connues=$(ssh_vm "git -C $SRC ls-tree --name-only '$1' src/ameesh/migrations/" \
    | sed -nE 's#.*/0*([0-9]+)_[^/]*\.sql$#\1#p' | sort -n)
  [ -n "$connues" ] || { echo "aucune migration lue dans $1" >&2; exit 1; }
  faites=$(ssh_vm "runuser -u postgres -- psql -X -At -d ameesh -c 'SELECT version FROM schema_migrations'" | sort -n)
  comm -23 <(sort <<<"$connues") <(sort <<<"$faites") | sort -n
}
exiger_migrations() {  # $1 : commit, $2 : ce qu'on s'apprête à faire
  local absentes
  absentes=$(migrations_absentes "$1" | tr '\n' ' ') || exit 1
  [ -z "$absentes" ] && return 0
  echo "migrations ${absentes}absentes de la base du mesh : lancer d'abord « poste.sh migrer » ($2 refusé)" >&2
  exit 1
}
# HEAD du clone de la cible (agents ou serveur) : un commit dont la présence
# distante est prouvée, utilisable comme base du paquet. Vide si illisible.
tete_clone_distante() {
  if [ "$MODE" = agents ]; then
    utilisateur_agents bash -c 'git -C /home/ameesh/.local/share/ameesh/src rev-parse --verify "HEAD^{commit}" 2>/dev/null || true'
  else
    ssh_vm "git -C $SRC rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true"
  fi
}
# Envoie le commit $1 dans un fichier temporaire de la cible $2 (root@…) : paquet
# git du poste pour un clone qui n'atteint pas l'origine (le dépôt local doit
# contenir le commit). La seule base exclue est le HEAD de la cible, que la
# cible prouve présente avant l'envoi ; sans base lisible, ou si l'exclusion
# vide le paquet, le paquet est autonome — plus de « Refusing to create empty
# bundle » pour une cible déjà fusionnée dans origin/main (#1252 B4). Le nom
# distant vient de `mktemp` (créé en 0600 par le root distant : ni nom fixe
# pré-créable, ni symlink exploitable), puis la cible est rendue lisible par le
# compte ameesh (`chmod 644` sur la cible : le umask du root distant ne décide
# pas de la lecture). Le chemin créé est publié dans PAQUET_DISTANT ; l'appelant
# le retire après le fetch (#1252 R2 : lisibilité et nom non devinable).
paquet_git() {
  local sha=$1 hote=$2 base distant
  PAQUET_DISTANT=
  git -C "$DEPOT" cat-file -e "$sha^{commit}" 2>/dev/null \
    || { echo "commit $sha absent du dépôt local : paquet git impossible" >&2; return 1; }
  base=$(tete_clone_distante) || true
  [[ $base =~ ^[0-9a-fA-F]{40}$ ]] \
    && git -C "$DEPOT" cat-file -e "$base^{commit}" 2>/dev/null || base=
  # #1155 : la création du paquet est déléguée à `paquet-git.sh` (exclusions
  # calculées sur les objets réellement présents chez le destinataire, ref
  # temporaire retirée dans tous les cas) ; #1252 B4 : une base inconnue ou
  # égale au commit visé est ignorée, le paquet reste autonome.
  "$ICI/paquet-git.sh" "$DEPOT" refs/mise-a-jour/vm "$sha" \
    "$TRAVAIL/ameesh.bundle" "$base" \
    || { rm -f "$TRAVAIL/ameesh.bundle"; return 1; }
  distant=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$hote" \
              'umask 077; mktemp /tmp/ameesh.bundle.XXXXXX') \
    || { rm -f "$TRAVAIL/ameesh.bundle"
         echo "fichier temporaire distant impossible : paquet git non envoyé" >&2; return 1; }
  [[ $distant =~ ^/[A-Za-z0-9._/-]+$ ]] \
    || { ssh -o BatchMode=yes "$hote" "rm -f '$distant'" 2>/dev/null || true
         rm -f "$TRAVAIL/ameesh.bundle"
         echo "chemin distant inattendu : $distant" >&2; return 1; }
  chmod 644 "$TRAVAIL/ameesh.bundle"
  scp -q -o BatchMode=yes "$TRAVAIL/ameesh.bundle" "$hote:$distant" \
    || { ssh -o BatchMode=yes "$hote" "rm -f '$distant'" 2>/dev/null || true
         rm -f "$TRAVAIL/ameesh.bundle"; return 1; }
  ssh -o BatchMode=yes "$hote" "chmod 644 '$distant'" \
    || { ssh -o BatchMode=yes "$hote" "rm -f '$distant'" 2>/dev/null || true
         rm -f "$TRAVAIL/ameesh.bundle"; return 1; }
  rm -f "$TRAVAIL/ameesh.bundle"
  PAQUET_DISTANT=$distant
}
# Commandes sur la VM d'agents : utilisateur ameesh et son bus systemd,
# la base reste celle de la configuration du compte de l’unité.
# -n : le client consomme son entrée standard même quand la commande distante
# ne la lit pas. Sans lui, la boucle `while read` de controler_agents lui livre
# la liste des agents et n'en contrôle qu'un seul (#1455).
ssh_agents() { ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$AGENTS_SSH" "$@"; }
utilisateur_agents() {
  local commande; printf -v commande '%q ' "$@"
  ssh_agents "set -euo pipefail
    uid=\$(id -u ameesh)
    runuser -u ameesh -- env HOME=/home/ameesh XDG_RUNTIME_DIR=/run/user/\$uid \
      DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/\$uid/bus $commande"
}
commande_agents() {
  # Use the runner's systemd EnvironmentFile parser, including literal $/#.
  # `systemd-run -p` does not expand the `%h` specifier (a transient unit's
  # properties are not specifier-expanded): it would refuse
  # `EnvironmentFile=%h/...` as "not absolute" (#1133). Expand $HOME in the
  # shell, where it is the ameesh home set by `utilisateur_agents`.
  utilisateur_agents bash -c 'set -euo pipefail
    test -r "$HOME/.config/ameesh/env" \
      || { echo "runner environment file unreadable: $HOME/.config/ameesh/env" >&2; exit 1; }
    exec systemd-run --user --pipe --wait --quiet \
      -p EnvironmentFile="$HOME/.config/ameesh/env" -- "$@"' -- "$@"
}
ameesh_agents() { commande_agents /home/ameesh/.local/share/ameesh/venv/bin/ameesh "$@"; }
python_agents() { commande_agents /home/ameesh/.local/share/ameesh/venv/bin/python "$@"; }
lire_bail() {
  if [ "$MODE" = agents ]; then
    ameesh_agents show "$1" --json | python3 -c 'import json,sys
r=json.load(sys.stdin)
if r.get("host") != sys.argv[1]: sys.exit("placement modifié")
print(json.dumps(r))' "$HOTE_AGENTS"
  else "$AMEESH" show "$1" --json; fi
}
statut() { lire_bail "$1" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status") or "")'; }
heure_base() {
  if [ "$MODE" = agents ]; then
    python_agents -c 'from ameesh import config,db
conn = db.connect(config.load())
try: print(conn.query("SELECT extract(epoch FROM clock_timestamp()) AS ts")[0]["ts"])
finally: conn.close()'
  else
    ssh_vm "runuser -u postgres -- psql -X -At -d ameesh -c 'SELECT extract(epoch FROM clock_timestamp())'"
  fi
}
proprietaire_de() {
  local sortie id unite pid
  if [ "$MODE" = agents ]; then
    unite=$(unite_agents "$1")
    sortie=$(utilisateur_agents systemctl --user show -p ExecStart --value "$unite") || return 1
  else
    unite=ameesh-runner@$1
    sortie=$(ssh_vm "systemctl show -p ExecStart --value $unite") || return 1
  fi
  id=$(bail_runner_id <<<"$sortie") \
    || { echo "$unite: ExecStart unreadable" >&2; return 1; }
  if [ -n "$id" ]; then echo "$id"
  else
    if [ "$MODE" = agents ]; then
      pid=$(utilisateur_agents systemctl --user show -p MainPID --value "$unite") || return 1
    else pid=$(ssh_vm "systemctl show -p MainPID --value $unite") || return 1; fi
    echo ":$pid"
  fi
}
# Lecture sur l'hôte, jamais depuis PERSONAS_VM ni les unités déjà actives :
# une unité manquante doit être contrôlée elle aussi. Ignorer et nommer les
# noms invalides ; ne pas transformer un registre vide en succès.
# `avec-arretes` inclut les personas arrêtées du registre : le redémarrage de
# déploiement doit recharger leur unité si elle tourne encore (#1633), là où le
# contrôleur ne les attend pas (aucun bail d'une persona arrêtée).
agents_du_registre() {
  local arretes=${1:-} noms
  noms=$(ameesh_agents list --json | python3 -c 'import json,re,sys
rows = json.load(sys.stdin)
avec_arretes = sys.argv[2:] == ["avec-arretes"]
# Même règle que ameesh.config.NAME_RE (config.py:48), utilisée par
# registry._check_name : 1 à 64 caractères, lettres/chiffres puis . _ -.
names = set()
for r in rows:
    if r.get("host") != sys.argv[1] or (r.get("mode") or "execute") != "execute":
        continue
    if r.get("status") == "stopped" and not avec_arretes:
        continue
    name = r.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        print("agent " + repr(name) + " : nom invalide dans le registre, ignoré", file=sys.stderr)
        continue
    names.add(name)
names = sorted(names)
if not names: sys.exit("aucun agent à mener sur " + sys.argv[1])
print("\n".join(names))' "$HOTE_AGENTS" "$arretes") || return 1
  printf '%s\n' "$noms"
}

unite_agents() { printf 'ameesh-runner-agent@%s.service' "$1"; }
echec_agent() {
  echo "$1 : $2 — journalctl --user -u $(unite_agents "$1") -n 50 (utilisateur ameesh sur $AGENTS_SSH)" >&2
}

# --- #2152 : drain d'un agent qui enchaîne les tours -------------------------
# Attendre qu'un agent enchaînant les tours de continuation rende la main ne
# finit jamais (16-17/10 : 57 min, puis de nouveau). Après DRAIN_ATTENTE
# secondes, le redémarrage pose donc lui-même une pause opérateur courte
# (drain) : le runner finit son tour et n'en commence plus. Dès qu'il est hors
# tour, l'unité est redémarrée, puis la pause est levée — jamais un tour
# interrompu. Le mesh journalise la pose et la levée (operator_pause_events).
#
# Identité du drain (R1 B1/B2) : le motif exact de la pause — unique par pose
# (jeton) — est l'identité stable de l'opération. La levée passe par
# « resume --operator --expect-reason <motif> », conditionnel et atomique côté
# base : une pause étrangère, même posée après une levée externe et avant
# notre reprise, n'est jamais levée (aucune course lecture → écriture). Si la
# réponse de la pause est perdue ou illisible (SSH coupé, JSON tronqué), le
# script réconcilie par l'état réel : la pause en place est la nôtre (motif
# identique) et le drain continue, sinon rien n'est levé ni redémarré. Aucun
# drain ne survit à sa sortie (trap EXIT).
#
# Geste d'opérateur : pause et reprise passent par le CLI local, qui refuse
# une session liée à un agent (bad_actor). Le drain exige donc un chemin
# opérateur humain (vm.sh lancé hors session d'agent) ; sinon la pose est
# refusée, le script s'arrête sans redémarrer l'agent et le dit.
DRAINS=()  # entrées « persona<TAB>motif exact de la pause posée »
drain_lieu() {
  if [ "$MODE" = agents ]; then printf 'agents %s sur %s' "$ACTION" "$HOTE_AGENTS"
  else printf '%s sur %s' "$ACTION" "$VM"; fi
}
drain_jeton() { printf '%s-%s-%s' "$(hostname)" "$$" "$RANDOM"; }
drain_motif() {  # $1 jeton → raison exacte de la pause (identité du drain)
  printf 'drain de déploiement #2152 (vm.sh %s) [%s]' "$(drain_lieu)" "$1"
}
drain_reprise() {  # raison journalisée par la levée
  printf 'reprise après drain de déploiement #2152 (vm.sh %s) : redémarrage hors tour effectué' \
    "$(drain_lieu)"
}
drain_supporte() {  # le CLI de la cible connaît la reprise conditionnelle
  local aide
  if [ "$MODE" = agents ]; then aide=$(ameesh_agents resume --help 2>&1) || return 1
  else aide=$("$AMEESH" resume --help 2>&1) || return 1; fi
  grep -q -- '--expect-reason' <<<"$aide"
}
pause_operateur() {  # $1 persona, $2 motif ; --json : « changed » dit si la pause est nôtre
  if [ "$MODE" = agents ]; then
    ameesh_agents pause "$1" --reason "$2" --json
  else
    "$AMEESH" pause "$1" --reason "$2" --json
  fi
}
reprise_operateur() {  # $1 persona, $2 motif attendu ; lève cette pause-là, ou rien
  if [ "$MODE" = agents ]; then
    ameesh_agents resume "$1" --operator --reason "$(drain_reprise)" \
      --expect-reason "$2" --json
  else
    "$AMEESH" resume "$1" --operator --reason "$(drain_reprise)" \
      --expect-reason "$2" --json
  fi
}
json_champ() {  # $1 champ ; stdin : objet JSON → « 1 »/« 0 » pour un booléen, sinon la valeur
  python3 -c 'import json,sys
v = json.load(sys.stdin).get(sys.argv[1])
print("" if v is None else (1 if v is True else (0 if v is False else v)))' "$1"
}
pause_etat() {  # stdin : `show --json` → « <operator_paused>|<raison> »
  python3 -c 'import json,sys
r = json.loads(sys.stdin.read() or "{}")
print("%d|%s" % (1 if r.get("operator_paused") else 0, r.get("operator_pause_reason") or ""))'
}
etat_agent() {  # $1 persona → « statut|pause » (une seule lecture)
  if [ "$MODE" = agents ]; then
    ameesh_agents show "$1" --json | python3 -c 'import json,sys
r = json.load(sys.stdin)
if r.get("host") != sys.argv[1] or (r.get("mode") or "execute") != "execute":
    print("skip|0")
else:
    print("%s|%d" % (r.get("status") or "", 1 if r.get("operator_paused") else 0))' "$HOTE_AGENTS"
  else
    "$AMEESH" show "$1" --json | python3 -c 'import json,sys
r = json.load(sys.stdin)
print("%s|%d" % (r.get("status") or "", 1 if r.get("operator_paused") else 0))'
  fi
}
drain_motif_de() {  # $1 persona → motif exact si ce drain est le nôtre (rc 1 sinon)
  local e
  for e in ${DRAINS[@]+"${DRAINS[@]}"}; do
    if [ "${e%%$'\t'*}" = "$1" ]; then printf '%s' "${e#*$'\t'}"; return 0; fi
  done
  return 1
}
drain_retire() {  # $1 persona : notre drain n'est plus en place
  local e reste=()
  for e in ${DRAINS[@]+"${DRAINS[@]}"}; do
    [ "${e%%$'\t'*}" = "$1" ] || reste+=("$e")
  done
  DRAINS=(${reste[@]+"${reste[@]}"})
}
pause_reelle() {  # $1 persona → « <operator_paused>|<raison> » de l'état réel
  local ligne
  ligne=$(lire_bail "$1") || return 1
  pause_etat <<<"$ligne"
}
poser_drain() {  # $1 persona, $2 secondes déjà attendues
  local motif sortie change etat pose raison
  motif=$(drain_motif "$(drain_jeton)")
  # Track the intent before the call: both its reply and the reconciliation
  # read can fail after commit. EXIT still attempts the conditional cleanup.
  DRAINS+=("$1"$'\t'"$motif")
  echo "$1 : en tour depuis ${2}s — pose d'une pause opérateur (drain #2152)"
  if sortie=$(pause_operateur "$1" "$motif"); then
    if change=$(json_champ changed <<<"$sortie") && [ -n "$change" ]; then
      if [ "$change" = 1 ]; then
        echo "$1 : drain posé — le tour en cours se termine, aucun tour interrompu ; redémarrage dès qu'il est hors tour"
        return 0
      fi
      # Posée entre notre lecture et la nôtre : elle n'est pas de ce script,
      # il ne la lèvera pas (on ne lève que la pause qu'on a posée).
      drain_retire "$1"
      echo "$1 : pause opérateur déjà posée par un autre acteur — laissée en place"
      return 0
    fi
    echo "$1 : réponse de pause illisible — réconciliation par l'état réel" >&2
  else
    echo "$1 : pose du drain refusée (voir ci-dessus) — réconciliation par l'état réel" >&2
  fi
  # R1 B2 : la pause a peut-être abouti sans réponse lisible (SSH coupé, JSON
  # tronqué). L'état réel tranche : motif identique = c'est notre drain.
  if ! etat=$(pause_reelle "$1"); then
    echo "$1 : état illisible — aucun redémarrage de cet agent" >&2; return 1
  fi
  IFS='|' read -r pose raison <<<"$etat"
  if [ "$pose" = 1 ] && [ "$raison" = "$motif" ]; then
    echo "$1 : drain posé (confirmé par l'état réel) — le tour en cours se termine, aucun tour interrompu"
    return 0
  fi
  drain_retire "$1"
  echo "$1 : la pause en place n'est pas celle de ce drain — aucun redémarrage, aucune levée" >&2
  return 1
}
lever_drain() {  # $1 persona, $2 : ce qui a mis fin à l'attente
  local motif sortie change etat pose raison
  motif=$(drain_motif_de "$1") || return 0
  sortie=$(reprise_operateur "$1" "$motif") && change=$(json_champ changed <<<"$sortie") || change=
  if [ "$change" = 1 ]; then
    # Levée par nous ; l'état réel doit le confirmer.
    if ! etat=$(pause_reelle "$1"); then
      echo "$1 : drain levé, mais relecture de la pause impossible — vérifier « ameesh show $1 »" >&2
      return 1
    fi
    IFS='|' read -r pose raison <<<"$etat"
    [ "$pose" = 0 ] || {
      echo "$1 : pause toujours posée après reprise — le lever à la main : « ameesh resume $1 --operator --reason … »" >&2
      return 1; }
    drain_retire "$1"
    echo "$1 : drain levé ($2) — reprise prouvée (operator_paused=false)"
    return 0
  fi
  if [ "$change" = 0 ]; then
    # Rien levé : la pause en place n'est plus la nôtre (étrangère), ou elle
    # était déjà levée. Dans les deux cas notre drain n'est plus en place, et
    # aucune pause étrangère n'a été touchée (R1 B1).
    if ! etat=$(pause_reelle "$1"); then
      echo "$1 : reprise conditionnelle sans effet et état illisible — vérifier « ameesh show $1 »" >&2
      drain_retire "$1"; return 1
    fi
    IFS='|' read -r pose raison <<<"$etat"
    drain_retire "$1"
    if [ "$pose" = 1 ]; then
      echo "$1 : pause étrangère laissée en place (elle n'est pas celle de ce drain, $2)" >&2
    else
      echo "$1 : drain déjà levé par un autre acteur ($2)"
    fi
    return 0
  fi
  # Réponse illisible ou reprise refusée : réconcilier par l'état réel avant
  # de conclure — ne jamais lever une pause qui n'est pas la nôtre.
  if ! etat=$(pause_reelle "$1"); then
    echo "$1 : drain NON levé (reprise sans réponse lisible, état illisible) — le lever à la main : « ameesh resume $1 --operator --reason … »" >&2
    return 1
  fi
  IFS='|' read -r pose raison <<<"$etat"
  if [ "$pose" = 1 ] && [ "$raison" = "$motif" ]; then
    echo "$1 : drain NON levé (reprise refusée ou sans réponse) — l'agent reste en pause opérateur ; le lever à la main : « ameesh resume $1 --operator --reason … »" >&2
    return 1
  fi
  drain_retire "$1"
  if [ "$pose" = 1 ]; then
    echo "$1 : pause étrangère laissée en place (le drain n'était plus celui posé, $2)" >&2
  else
    echo "$1 : drain levé ($2) — état réel confirmé (operator_paused=false)"
  fi
  return 0
}
lever_drains() {  # sortie du script : aucun drain ne survit à la commande
  local e code=0
  for e in ${DRAINS[@]+"${DRAINS[@]}"}; do
    lever_drain "${e%%$'\t'*}" "sortie du script" || code=1
  done
  return "$code"
}
trap 'lever_drains || true' EXIT

redemarrer_agents() {
  local noms p st avant ligne heure attendu tenu restantes suivantes debut=$SECONDS
  local etat deja_pause ecoule
  local -A debut_tour=()
  local non_verifiees=()
  # #1633 : inclure les personas arrêtées pour recharger leur unité active.
  noms=$(agents_du_registre avec-arretes) || return 1
  mapfile -t restantes <<<"$noms"
  # R1 B2 : ne jamais poser une pause qu'on ne saurait pas lever. Un CLI de
  # cible antérieur à #2152 ne connaît pas --expect-reason : on attend comme
  # avant plutôt que de laisser un drain orphelin.
  if [ "$DRAIN_ATTENTE" -gt 0 ] && ! drain_supporte; then
    echo "drain #2152 indisponible : le CLI de la cible ne connaît pas --expect-reason (installer le code #2152 avant de redémarrer) — attente sans drain" >&2
    DRAIN_ATTENTE=0
  fi
  etape "exécuteurs de $HOTE_AGENTS (${#restantes[@]}), un par un hors tour"
  while [ "${#restantes[@]}" -gt 0 ]; do
    suivantes=()
    for p in "${restantes[@]}"; do
      # Le placement peut changer pendant l'attente d'un tour : relire avant
      # de toucher une unité que cet hôte ne doit plus mener.
      etat=$(etat_agent "$p") || return 1
      IFS='|' read -r st deja_pause <<<"$etat"
      if [ "$st" = skip ]; then
        lever_drain "$p" "placement modifié" || return 1
        echo "$p : placement modifié, ignoré"; continue
      fi
      if [ "$st" = running ]; then
        # #2152 : un agent qui enchaîne les tours de continuation ne rend
        # jamais la main ; après DRAIN_ATTENTE, le redémarrage pose lui-même
        # une pause opérateur courte (drain), qui prend effet à la fin du tour.
        # Une pause déjà posée par un autre acteur n'est ni doublée ni levée.
        if [ "$deja_pause" != 1 ] && [ "$DRAIN_ATTENTE" -gt 0 ]; then
          if [ -z "${debut_tour[$p]:-}" ]; then debut_tour[$p]=$SECONDS; fi
          ecoule=$((SECONDS - debut_tour[$p]))
          if [ "$ecoule" -ge "$DRAIN_ATTENTE" ]; then
            poser_drain "$p" "$ecoule" || return 1
          fi
        fi
        suivantes+=("$p"); continue
      fi
      # #1131 : persona arrêtée par le propriétaire — un déploiement ne la
      # réactive pas. #1633 : son unité encore active est seulement rechargée
      # sur le nouveau code, sans bail ni tour ; une unité déjà arrêtée n'est
      # pas relancée. #2152 : un drain posé par ce script est levé.
      if [ "$st" = stopped ]; then
        lever_drain "$p" "persona arrêtée" || return 1
        if utilisateur_agents systemctl --user is-active --quiet "$(unite_agents "$p")"; then
          utilisateur_agents systemctl --user restart "$(unite_agents "$p")" \
            && utilisateur_agents systemctl --user is-active --quiet "$(unite_agents "$p")" \
            || { echec_agent "$p" "unité d'une persona arrêtée : ne redémarre pas"; return 1; }
          echo "$p : arrêté dans le registre — unité redémarrée pour charger le nouveau code, aucun tour"
        else
          echo "$p : arrêté dans le registre — unité déjà arrêtée, rien à recharger"
        fi
        continue
      fi
      ligne=$(lire_bail "$p") && heure=$(heure_base) && attendu=$(proprietaire_de "$p") \
        || { echec_agent "$p" "registre, heure de base ou ExecStart illisible, aucun restart"; return 1; }
      avant=$(bail_epoque <<<"$ligne")
      tenu=0
      bail_valide -1 "$attendu" "$heure" <<<"$ligne" && tenu=1
      utilisateur_agents systemctl --user restart "$(unite_agents "$p")" \
        && utilisateur_agents systemctl --user is-active --quiet "$(unite_agents "$p")" \
        || { echec_agent "$p" "ne redémarre pas"; return 1; }
      # #2152 : l'agent est hors tour et l'unité tourne sur le nouveau code —
      # la pause du drain est levée ici, avant la preuve de reprise du bail.
      lever_drain "$p" "redémarrage hors tour" || return 1
      if [ "$tenu" = 0 ]; then
        echo "WARNING $p: not checked: no lease before the restart, no sign of life"
        non_verifiees+=("$p")
      elif ! attendu=$(proprietaire_de "$p") || ! attendre_bail "$avant" "$attendu" "$p"; then
        echec_agent "$p" "active, mais bail non repris"; return 1
      fi
      echo "$p : redémarré (était $st)"
    done
    restantes=("${suivantes[@]}")
    if [ "${#restantes[@]}" -gt 0 ]; then
      if [ $((SECONDS - debut)) -ge "$ATTENTE_MAX" ]; then
        echo "toujours en tour après ${ATTENTE_MAX}s : ${restantes[*]}" >&2; return 1
      fi
      sleep 15
    fi
  done
  if [ "${#non_verifiees[@]}" -gt 0 ]; then
    etape "WARNING: ${#non_verifiees[@]} persona(s) not checked (no lease before the restart, no sign of life): ${non_verifiees[*]}"
  fi
}

# --- mode agents : installer / retour (layout du compte ameesh) -------------
# Clone ~/.local/share/ameesh/src, venv ~/.local/share/ameesh/venv, code
# installé depuis des instantanés src-<sha7> jamais écrasés, sha du code
# d'avant dans previous-sha. Un instantané est un worktree détaché du commit
# (arbre exact, jamais copié d'un clone sale) ; un instantané préexistant n'est
# réutilisé que si son HEAD et son contenu correspondent au commit. L'identité
# effective du compte runner et le journal de récupération sont vérifiés avant
# toute mutation. Détail : docs/DEPLOIEMENT-HOTE-AGENTS.md.

installe_agents() {  # code réellement installé (direct_url.json du venv VIVANT)
  utilisateur_agents bash -c 'set -euo pipefail
    P=/home/ameesh/.local/share/ameesh; V=$P/venv; L=$V
    # #1969 : le venv stable porte les lanceurs ; les métadonnées du code servi
    # sont celles du venv versionné vivant (lien venv-courant), pas du lib
    # d’origine conservé pour les processus déjà lancés.
    if [ -L "$P/venv-courant" ]; then
      C=$(readlink "$P/venv-courant")
      case $C in /*) L=$C;; *) L=$P/$C;; esac
    fi
    for d in "$L"/lib/python*/site-packages/ameesh-*.dist-info; do
      [ -r "$d/direct_url.json" ] || continue
      python3 -c "import json,sys
u = json.load(open(sys.argv[1])).get(\"url\", \"\")
print(u.rsplit(\"src-\", 1)[-1] if \"src-\" in u else \"\")" "$d/direct_url.json"
      exit 0
    done'
}
clone_agents() {
  utilisateur_agents bash -c 'git -C /home/ameesh/.local/share/ameesh/src rev-parse HEAD 2>/dev/null || true'
}
previous_agents() {
  utilisateur_agents bash -c 'cat /home/ameesh/.local/share/ameesh/previous-sha 2>/dev/null || true'
}
# Identité effective du compte runner, lue dans l'environnement de l'unité
# (EnvironmentFile) : le paquet installé peut être cassé par l'installation
# inachevée que « retour » doit justement réparer, donc le repli reproduit
# config.load().host sans importer le paquet — AMEESH_HOST/AGENT_MESH_HOST,
# puis le champ `host` du JSON désigné par AMEESH_CONFIG/AGENT_MESH_CONFIG
# (repli sur la config héritée agent-mesh, comme config.load), puis le nom
# d'hôte. Un fichier présent mais illisible, ou un host non textuel ou vide,
# ferme la commande au lieu de retomber sur le nom machine (#1252 B3).
identite_agents() {
  local h
  if h=$(python_agents -c 'from ameesh import config
h = config.load().host
if not isinstance(h, str) or not h.strip():
    raise SystemExit("host unprovable")
print(h)' 2>/dev/null) && [ -n "${h//[$'\r\n ']/}" ]; then
    printf '%s\n' "${h//[$'\r\n ']/}"
    return 0
  fi
  commande_agents python3 -c '
import json, os, socket, sys

def expand(chemin):
    return os.path.abspath(os.path.expanduser(chemin))

def refus(message):
    print(message, file=sys.stderr)
    sys.exit(1)

legacy = expand(os.environ.get("AGENT_MESH_CONFIG") or "~/.config/agent-mesh/config.json")
chemin = expand(os.environ.get("AMEESH_CONFIG") or os.environ.get("AGENT_MESH_CONFIG")
                or "~/.config/ameesh/config.json")
if not os.path.exists(chemin) and os.path.exists(legacy):
    chemin = legacy  # une config agent-mesh existante reste lue (config.load)

absent = object()
hote = absent
if os.path.exists(chemin):
    try:
        with open(chemin, encoding="utf-8") as fh:
            brut = json.load(fh)
    except (OSError, ValueError) as exc:
        refus("unreadable config %s: %s" % (chemin, exc))
    if not isinstance(brut, dict):
        refus("unreadable config %s: JSON object expected" % chemin)
    if "host" in brut:
        hote = brut["host"]

env_hote = os.environ.get("AMEESH_HOST") or os.environ.get("AGENT_MESH_HOST")
if env_hote:
    valeur = env_hote
elif hote is not absent:
    valeur = hote
else:
    valeur = socket.gethostname().split(".")[0]
if not isinstance(valeur, str) or not valeur.strip():
    refus("host unprovable in the runner environment")
print(valeur.strip("\r\n "))
'
}
exiger_identite_agents() {
  local h
  h=$(identite_agents) || {
    echo "identité du compte runner illisible sur $AGENTS_SSH (EnvironmentFile) : aucune mutation" >&2
    exit 1; }
  h=${h//[$'\r\n']/}
  [ -n "$h" ] || {
    echo "identité du compte runner vide sur $AGENTS_SSH : aucune mutation" >&2
    exit 1; }
  [ "$h" = "$HOTE_AGENTS" ] || {
    echo "hôte effectif du compte runner « $h » ≠ HOTE_AGENTS « $HOTE_AGENTS » : $BASCULE_ENV ne vise pas cette VM, aucune mutation" >&2
    exit 1; }
}
# Journal de récupération (#1252 B1) : SHA du code quitté, celui que « retour »
# doit réinstaller si l'installation échoue après avoir remplacé le paquet.
# Écrit avant toute mutation, effacé seulement après une réinstallation
# réussie ; distinct de previous-sha, publié lui aussi seulement au succès.
# Une relance d'« installer » après un échec ne doit jamais écraser ce journal :
# le SHA quitté reste la cible de récupération jusqu'à résolution par « retour ».
etat_journal_agents() {  # sha40, « absent » ou « invalide »
  utilisateur_agents bash -c 'f=/home/ameesh/.local/share/ameesh/installation-en-cours
    [ -e "$f" ] || { echo absent; exit 0; }
    [ -r "$f" ] || { echo invalide; exit 0; }
    sha=$(sed -n "s/^sha=//p" "$f" | head -n1)
    if [ -z "$sha" ] || [ "${#sha}" -ne 40 ] || [ -n "${sha//[0-9a-fA-F]/}" ]; then
      echo invalide; exit 0
    fi
    echo "$sha"'
}
ecrire_journal_agents() {  # $1 : sha40 quitté, $2 : sha40 visé ; refuse d'écraser
  utilisateur_agents bash -c "set -euo pipefail
    umask 077
    P=/home/ameesh/.local/share/ameesh
    f=\$P/installation-en-cours
    test ! -e \"\$f\" || { echo 'journal de récupération déjà présent' >&2; exit 1; }
    t=\$(mktemp \"\$P/.installation-en-cours.XXXXXX\")
    printf 'sha=%s\ninstantane=src-%s\ncible=%s\n' '$1' '${1:0:7}' '$2' > \"\$t\"
    mv \"\$t\" \"\$f\""
}
effacer_journal_agents() {
  utilisateur_agents bash -c 'rm -f /home/ameesh/.local/share/ameesh/installation-en-cours'
}
# Vérifie le contenu du disque d'un instantané contre les objets du commit $1,
# sans passer par l'index : `assume-unchanged` et `skip-worktree` ne masquent
# rien, chaque fichier est haché et comparé au blob du commit, et un fichier
# modifié, absent, en trop, ou au mauvais mode est refusé. Affiche « ok » ou la
# raison du refus (#1252 B2).
verifier_contenu_agents() {  # $1 : sha40
  utilisateur_agents python3 -c '
import hashlib, os, stat, subprocess, sys

sha = sys.argv[1]
d = "/home/ameesh/.local/share/ameesh/src-" + sha[:7]

def git(*args):
    return subprocess.run(["git", "-C", d, *args], capture_output=True)

if not os.path.lexists(d):
    print("absent"); sys.exit(0)
if os.path.islink(d) or not os.path.isdir(d):
    print("chemin non répertoire"); sys.exit(0)
if not os.path.exists(os.path.join(d, ".git")):
    print("sans dépôt git : contenu non prouvable"); sys.exit(0)
res = git("rev-parse", "--verify", "HEAD^{commit}")
tete = res.stdout.decode().strip() if res.returncode == 0 else ""
if tete != sha:
    print("HEAD=" + (tete or "illisible")); sys.exit(0)
res = git("ls-tree", "-r", "-z", "--full-tree", sha)
if res.returncode != 0:
    print("arbre du commit illisible"); sys.exit(0)

attendus = {}
for entree in res.stdout.split(b"\0"):
    if not entree:
        continue
    meta, chemin = entree.split(b"\t", 1)
    mode, genre, objet = meta.decode().split()
    attendus[chemin.decode("utf-8", "surrogateescape")] = (mode, genre, objet)

vus = set()
for racine, repertoires, fichiers in os.walk(d, followlinks=False):
    # Un lien symbolique vers un répertoire est un élément du disque à vérifier,
    # pas un répertoire à parcourir.
    liens = [n for n in repertoires if os.path.islink(os.path.join(racine, n))]
    repertoires[:] = [n for n in repertoires if n not in liens]
    elements = fichiers + liens
    if racine == d:
        repertoires[:] = [n for n in repertoires if n != ".git"]
        elements = [n for n in elements if n != ".git"]
    for nom in elements:
        chemin = os.path.relpath(os.path.join(racine, nom), d)
        vus.add(chemin)
        attendu = attendus.get(chemin)
        if attendu is None:
            print("fichier en trop : " + chemin); sys.exit(0)
        mode, genre, objet = attendu
        complet = os.path.join(racine, nom)
        if genre == "commit":
            if os.path.islink(complet) or not os.path.isdir(complet):
                print("sous-module absent : " + chemin); sys.exit(0)
            continue
        try:
            st = os.lstat(complet)
        except OSError:
            print("fichier illisible : " + chemin); sys.exit(0)
        if mode == "120000":
            if not stat.S_ISLNK(st.st_mode):
                print("type différent du commit : " + chemin); sys.exit(0)
            try:
                donnees = os.readlink(complet).encode("utf-8", "surrogateescape")
            except OSError:
                print("lien illisible : " + chemin); sys.exit(0)
        else:
            if not stat.S_ISREG(st.st_mode):
                print("type différent du commit : " + chemin); sys.exit(0)
            try:
                with open(complet, "rb") as fh:
                    donnees = fh.read()
            except OSError:
                print("fichier illisible : " + chemin); sys.exit(0)
        if hashlib.sha1(b"blob %d\0" % len(donnees) + donnees).hexdigest() != objet:
            print("contenu modifié : " + chemin); sys.exit(0)
        if (mode == "100755") != bool(st.st_mode & stat.S_IXUSR):
            print("mode différent du commit : " + chemin); sys.exit(0)
manquants = sorted(set(attendus) - vus)
if manquants:
    print("fichier absent : " + manquants[0]); sys.exit(0)
print("ok")
' "$1"
}
# État d'un instantané src-<sha7> : « absent », « ok » (arbre exact du commit)
# ou la raison du refus. Un instantané venu d'un clone sale, modifié depuis sa
# création ou sans dépôt git n'est jamais installé (#1252 B2). Les fichiers
# ignorés comptent : un `build/` ajouté serait consommé par le build suivant.
etat_instantane_agents() {
  verifier_contenu_agents "$1"
}
# Retire les artefacts de construction ignorés (build/, *.egg-info, __pycache__)
# d'un instantané existant : le build suivant repart de l'arbre du commit.
nettoyer_instantane_agents() {
  utilisateur_agents bash -c 'd=/home/ameesh/.local/share/ameesh/src-'"${1:0:7}"'
    [ -e "$d/.git" ] && git -C "$d" clean -qfdX || true'
}
creer_instantane_agents() {  # $1 : sha40 ; refuse d'écraser un chemin existant
  # Le contenu créé est vérifié exactement par assurer_instantane_agents, qui
  # rappelle etat_instantane_agents juste après (objets du commit, pas l'index).
  utilisateur_agents bash -c 'set -euo pipefail
    P=/home/ameesh/.local/share/ameesh; d=$P/src-'"${1:0:7}"'
    test ! -e "$d" || { echo "instantané déjà présent : $d" >&2; exit 1; }
    git -C "$P/src" worktree add -q --detach "$d" '"$1"'
    test "$(git -C "$d" rev-parse HEAD)" = '"$1"''
}
assurer_instantane_agents() {  # $1 : sha40 ; instantané exact, créé ou vérifié
  local sha=$1 etat
  nettoyer_instantane_agents "$sha"
  etat=$(etat_instantane_agents "$sha") \
    || { echo "instantané src-${sha:0:7} illisible" >&2; return 1; }
  if [ "$etat" = absent ]; then
    creer_instantane_agents "$sha" \
      || { echo "création de l'instantané src-${sha:0:7} refusée" >&2; return 1; }
    etat=$(etat_instantane_agents "$sha") \
      || { echo "instantané src-${sha:0:7} illisible après création" >&2; return 1; }
  fi
  [ "$etat" = ok ] || {
    echo "instantané src-${sha:0:7} refusé ($etat) : le contenu ne correspond pas au commit ; le retirer à la main (aucun écrasement automatique)" >&2
    return 1; }
}
# Réinstallation depuis l'instantané $1 : venv versionné préparé à l'écart
# puis bascule atomique (#1969). $2 (facultatif) : version quittée, adoptée
# sans pip à la première conversion. Le retour, lui, ne passe pas par ici : il
# rebascule le lien vers le venv conservé (`retour_agents`).
reinstaller_agents() {
  local court=${1:0:7} precedent=${2:0:7}
  envoyer_bascule_agents || return 1
  utilisateur_agents bash -c "set -euo pipefail
    P=/home/ameesh/.local/share/ameesh; V=\$P/venv
    python3 $BASCULE_AGENTS preparer --venv \"\$V\" --src \"\$P/src-$court\" --version $court
    python3 $BASCULE_AGENTS activer --venv \"\$V\" --version $court ${precedent:+--precedent $precedent}
    \"\$V/bin/python\" -c 'import psycopg, ameesh; print(\"psycopg\", psycopg.__version__)'"
}
releve_agents() {  # identité et pièces du bon hôte, avant tout geste
  local installe clone previous
  installe=$(installe_agents) || return 1
  clone=$(clone_agents) || return 1
  previous=$(previous_agents) || return 1
  echo "code installé : ${installe:-inconnu} ; clone : ${clone:-?} ; previous-sha : ${previous:-absent}"
}
# Versions de migration du commit $1 (clone de la VM) absentes de la base
# commune du mesh : même garde que le mode serveur.
migrations_absentes_agents() {
  local connues faites
  connues=$({ utilisateur_agents bash -c "git -C /home/ameesh/.local/share/ameesh/src ls-tree --name-only '$1' src/ameesh/migrations/" || true; } \
    | sed -nE 's#.*/0*([0-9]+)_[^/]*\.sql$#\1#p' | sort -n)
  [ -n "$connues" ] || { echo "aucune migration lue dans $1" >&2; exit 1; }
  faites=$({ python_agents -c 'from ameesh import config,db
conn = db.connect(config.load())
try:
    for row in conn.query("SELECT version FROM schema_migrations"):
        print(row["version"])
finally:
    conn.close()' || true; } | sort -n)
  comm -23 <(sort <<<"$connues") <(sort <<<"$faites") | sort -n
}
exiger_migrations_agents() {
  local absentes
  absentes=$(migrations_absentes_agents "$1" | tr '\n' ' ') || exit 1
  [ -z "$absentes" ] && return 0
  echo "migrations ${absentes}absentes de la base du mesh : « poste.sh migrer » d'abord (installation refusée)" >&2
  exit 1
}

controler_agents() {
  local noms p ligne heure attendu echec=0
  etape "code installé sur $HOTE_AGENTS"
  releve_agents || { echo "relevé du code impossible" >&2; echec=1; }
  noms=$(agents_du_registre) || return 1
  while IFS= read -r p; do
    etape "$p"
    utilisateur_agents systemctl --user is-active --quiet "$(unite_agents "$p")" \
      && utilisateur_agents journalctl --user -u "$(unite_agents "$p")" -n 15 --no-pager \
      || { echec_agent "$p" "unité inactive ou journal inaccessible"; echec=1; }
    ligne=$(lire_bail "$p") && heure=$(heure_base) && attendu=$(proprietaire_de "$p") \
      && bail_valide 0 "$attendu" "$heure" <<<"$ligne" \
      || { echec_agent "$p" "bail absent, étranger ou expiré, ou preuve illisible"; echec=1; }
  done <<<"$noms"
  etape "ameesh doctor sur $HOTE_AGENTS"
  ameesh_agents doctor || echec=1
  [ "$echec" = 0 ] || { echo "contrôles en échec" >&2; return 1; }
  echo "contrôles verts"
}

installer_agents() {
  local ref=${1:-HEAD} sha court avant complet courant journal
  exiger_identite_agents
  # 0. une installation inachevée (journal de récupération présent) se résout
  # par « retour » : relancer « installer » écraserait la cible de récupération
  # par un SHA dont le premier pip a déjà publié le direct_url.json (#1252 B1).
  journal=$(etat_journal_agents) \
    || { echo "journal de récupération illisible sur $HOTE_AGENTS : installation refusée" >&2; exit 1; }
  case "$journal" in
    absent) ;;
    invalide)
      echo "journal de récupération invalide sur $HOTE_AGENTS : installation refusée (le corriger ou le retirer à la main, puis « $0 agents retour »)" >&2
      exit 1 ;;
    *)
      echo "installation inachevée (journal de récupération : ${journal:0:9}) : lancer d'abord « $0 agents retour », installation refusée" >&2
      exit 1 ;;
  esac
  sha=$(git -C "$DEPOT" rev-parse --verify "$ref^{commit}")
  court=${sha:0:7}
  etape "VM d'agents $HOTE_AGENTS ($AGENTS_SSH) : commit visé ${sha:0:9} ($ref)"
  ssh_agents hostname
  # 1. le commit doit être dans le clone de la VM : origine, sinon paquet du poste
  utilisateur_agents bash -c 'git -C /home/ameesh/.local/share/ameesh/src fetch -q origin' \
    || echo "origine injoignable : paquet du poste"
  if ! utilisateur_agents bash -c "git -C /home/ameesh/.local/share/ameesh/src cat-file -e '$sha^{commit}'" 2>/dev/null; then
    etape "la VM n'a pas ce commit : envoi d'un paquet git"
    paquet_git "$sha" "$AGENTS_SSH"
    utilisateur_agents bash -c "git -C /home/ameesh/.local/share/ameesh/src fetch -q '$PAQUET_DISTANT' refs/mise-a-jour/vm"
    ssh_agents "rm -f '$PAQUET_DISTANT'"
  fi
  # 2. migrations du commit passées sur la base commune du mesh
  etape "migrations de ${sha:0:9} passées sur la base du mesh"
  exiger_migrations_agents "$sha" installer
  # 3. relevé avant : sans sha d'avant, le retour serait impossible
  avant=$(installe_agents) || true
  [[ $avant =~ ^[0-9a-fA-F]{7,40}$ ]] || avant=
  complet=
  if [ -n "$avant" ]; then
    complet=$(utilisateur_agents bash -c "git -C /home/ameesh/.local/share/ameesh/src rev-parse --verify '$avant^{commit}'" 2>/dev/null || true)
  fi
  if [ -z "$complet" ]; then
    courant=$(clone_agents) || true
    [[ $courant =~ ^[0-9a-fA-F]{40}$ ]] && complet=$courant
  fi
  [ -n "$complet" ] || { echo "code d'avant introuvable (direct_url.json, clone) : installation refusée, retour impossible" >&2; exit 1; }
  echo "avant : ${complet:0:9} (installé ${avant:-inconnu}, clone $(clone_agents))"
  # 4. instantané exact du code quitté : le retour s'appuiera dessus
  assurer_instantane_agents "$complet" \
    || { echo "instantané du code quitté indisponible : installation refusée (retour impossible)" >&2; exit 1; }
  # 5. instantané exact de la révision visée : un instantané préexistant altéré
  # fait refuser l'installation, avant toute mutation et sans l'écraser
  assurer_instantane_agents "$sha" \
    || { echo "instantané de ${sha:0:9} indisponible : installation refusée avant toute mutation" >&2; exit 1; }
  # 6. sauvegarde privée 0600 de l'état (env et unités)
  utilisateur_agents bash -c "set -euo pipefail
    umask 077
    T=/home/ameesh/.local/state/ameesh/mise-a-jour
    mkdir -p \"\$T\"
    cp /home/ameesh/.config/ameesh/env \"\$T/env-$court\"
    cp /home/ameesh/.config/systemd/user/ameesh-runner-agent@*.service \"\$T/\" 2>/dev/null || true
    echo \"sauvegarde : \$T/env-$court, unités\""
  # 7. journal de récupération AVANT toute mutation : le SHA du code quitté,
  # même si un pip s'arrête au milieu et si previous-sha est plus ancien.
  ecrire_journal_agents "$complet" "$sha" \
    || { echo "journal de récupération non écrit : installation refusée" >&2; exit 1; }
  echo "journal : « $0 agents retour » ramènera à ${complet:0:9} si l'installation échoue"
  # 8. mutation : clone détaché sur la cible, puis installation depuis son
  # instantané (l'instantané est déjà vérifié ci-dessus).
  if ! utilisateur_agents bash -c "set -euo pipefail
      P=/home/ameesh/.local/share/ameesh
      git -C \"\$P/src\" checkout -q --detach '$sha'
      test \"\$(git -C \"\$P/src\" rev-parse HEAD)\" = '$sha'"; then
    echo "checkout de ${sha:0:9} inachevé : « $0 agents retour » revient à ${complet:0:9}" >&2
    exit 1
  fi
  etape "installation dans le venv (depuis src-$court)"
  if ! reinstaller_agents "$sha" "$complet"; then
    echo "installation inachevée : « $0 agents retour » revient à ${complet:0:9} (journal conservé)" >&2
    exit 1
  fi
  # 9. previous-sha seulement après une installation réussie, et seulement si
  # le code installé change : relancer « installer » sur le SHA déjà en place
  # ne doit pas écraser la cible de retour (sinon « retour » revient sur place,
  # un faux retour — #1252 R2). Sans previous-sha, on n'en invente pas un.
  if [ "$complet" != "$sha" ]; then
    utilisateur_agents bash -c "printf '%s\n' '$complet' > /home/ameesh/.local/share/ameesh/previous-sha"
  else
    echo "code ${sha:0:9} déjà installé : previous-sha inchangé (aucun faux retour)"
  fi
  effacer_journal_agents
  echo "installé depuis src-$court (avant $complet). Les exécuteurs tournent encore l'ancien code : « $0 agents redemarrer », puis « $0 agents controler »."
}

retour_agents() {
  local cible court etat complet
  exiger_identite_agents
  etat=$(etat_journal_agents) \
    || { echo "journal de récupération illisible sur $HOTE_AGENTS : retour refusé" >&2; exit 1; }
  case "$etat" in
    absent)
      cible=$(previous_agents) || true
      [ -n "$cible" ] || { echo "previous-sha absent sur $HOTE_AGENTS : aucun retour possible" >&2; exit 1; }
      [[ $cible =~ ^[0-9a-fA-F]{7,40}$ ]] || { echo "previous-sha invalide : $cible" >&2; exit 1; }
      ;;
    invalide)
      echo "journal de récupération invalide sur $HOTE_AGENTS : retour refusé (le corriger ou le retirer à la main)" >&2
      exit 1 ;;
    *)
      cible=$etat
      echo "installation inachevée : retour au code quitté ${cible:0:9} (journal de récupération)" ;;
  esac
  court=${cible:0:7}
  etape "VM d'agents $HOTE_AGENTS ($AGENTS_SSH) : retour au commit ${cible:0:9}"
  # la cible doit exister dans le clone, sinon paquet git du poste
  if ! utilisateur_agents bash -c "git -C /home/ameesh/.local/share/ameesh/src cat-file -e '$cible^{commit}'" 2>/dev/null; then
    git -C "$DEPOT" cat-file -e "$cible^{commit}" 2>/dev/null \
      || { echo "commit $cible absent du dépôt local : retour impossible d'ici" >&2; exit 1; }
    etape "la VM n'a pas ce commit : envoi d'un paquet git"
    paquet_git "$cible" "$AGENTS_SSH"
    utilisateur_agents bash -c "git -C /home/ameesh/.local/share/ameesh/src fetch -q '$PAQUET_DISTANT' refs/mise-a-jour/vm"
    ssh_agents "rm -f '$PAQUET_DISTANT'"
  fi
  complet=$(utilisateur_agents bash -c "git -C /home/ameesh/.local/share/ameesh/src rev-parse --verify '$cible^{commit}'" 2>/dev/null || true)
  [ -n "$complet" ] || { echo "commit $cible illisible dans le clone de la VM : retour refusé" >&2; exit 1; }
  # instantané exact de la cible : créé depuis les objets, ou vérifié s'il
  # existe, avant toute mutation ; un instantané altéré fait refuser le retour.
  assurer_instantane_agents "$complet" \
    || { echo "instantané de ${complet:0:9} indisponible : retour refusé avant toute mutation" >&2; exit 1; }
  if ! utilisateur_agents bash -c "set -euo pipefail
      P=/home/ameesh/.local/share/ameesh
      git -C \"\$P/src\" checkout -q --detach '$complet'
      test \"\$(git -C \"\$P/src\" rev-parse HEAD)\" = '$complet'"; then
    echo "checkout de ${complet:0:9} inachevé : relancer « $0 agents retour »" >&2
    exit 1
  fi
  etape "retour par la bascule, sans pip (venv versionné conservé)"
  if ! envoyer_bascule_agents \
     || ! utilisateur_agents bash -c "set -euo pipefail
        P=/home/ameesh/.local/share/ameesh; V=\$P/venv
        python3 $BASCULE_AGENTS retour --venv \"\$V\"
        \"\$V/bin/python\" -c 'import psycopg, ameesh; print(\"retour\", ameesh.__file__)'"; then
    echo "retour par la bascule inachevé : relancer « $0 agents retour »" >&2
    exit 1
  fi
  # succès : le code installé devient la cible idempotente du retour, et le
  # journal d'une installation inachevée a rempli son rôle.
  utilisateur_agents bash -c "printf '%s\n' '$complet' > /home/ameesh/.local/share/ameesh/previous-sha"
  effacer_journal_agents
  echo "code ${complet:0:9} réinstallé depuis src-$court ; previous-sha = ${complet:0:9} (retour idempotent)."
  redemarrer_agents
  controler_agents
}

verifier() {
  local absentes
  etape "serveur"
  ssh_vm "hostname; echo \"code : \$(git -C $SRC rev-parse --short HEAD) ; installé : \$($VENV/bin/pip show ameesh | sed -n 's/^Version: //p')\""
  etape "exécuteurs du serveur"
  for p in $PERSONAS_VM; do
    echo "$p : $(ssh_vm "systemctl is-active ameesh-runner@$p" || true), statut $(statut "$p")"
  done
  etape "migrations du code installé absentes de la base du mesh"
  absentes=$(migrations_absentes HEAD | tr '\n' ' ') || exit 1
  echo "${absentes:-aucune}"
}

installer() {
  local ref=${1:-HEAD} sha
  sha=$(git -C "$DEPOT" rev-parse --verify "$ref^{commit}")
  etape "commit visé : ${sha:0:9} ($ref)"
  ssh_vm "set -e; git -C $SRC rev-parse HEAD > $PRECEDENT; echo \"précédent : \$(cut -c1-9 $PRECEDENT)\"
    git -C $SRC fetch -q origin || echo 'origine injoignable : paquet du poste'"
  if ! ssh_vm "git -C $SRC cat-file -e '$sha^{commit}'" 2>/dev/null; then
    etape "le serveur n'a pas ce commit : envoi d'un paquet git"
    paquet_git "$sha" "root@$VM"
    ssh_vm "set -e; git -C $SRC fetch -q '$PAQUET_DISTANT' refs/mise-a-jour/vm; rm -f '$PAQUET_DISTANT'"
  fi
  # Le venv du serveur sert aussi les commandes des agents (mail send…) : le
  # nouveau code n'y entre qu'une fois la base migrée (10/10, L151)
  etape "migrations de ${sha:0:9} passées sur la base du mesh"
  exiger_migrations "$sha" installer
  etape "préparation isolée puis bascule dans $VENV (#1969)"
  envoyer_bascule_vm
  ssh_vm "set -e
    git -C $SRC checkout -q --detach '$sha'
    test \"\$(git -C $SRC rev-parse HEAD)\" = '$sha'
    python3 $BASCULE_VM preparer --venv $VENV --src $SRC --version ${sha:0:7}
    python3 $BASCULE_VM activer --venv $VENV --version ${sha:0:7} --precedent \$(cut -c1-7 $PRECEDENT)
    rm -f $BASCULE_VM
    $VENV/bin/python -c 'import psycopg, ameesh.budget, ameesh.projects; print(\"psycopg\", psycopg.__version__, \": modules L70 et L62 présents\")'"
  echo "installé. Les exécuteurs tournent encore l'ancien code : redemarrer, sans tarder."
}

redemarrer() {
  # shellcheck source=bail.sh
  . "$ICI/bail.sh"
  local restantes suivantes debut=$SECONDS st avant ligne heure tenu attendu
  local etat deja_pause ecoule
  local -A debut_tour=()
  non_verifiees=()
  # le code écrit ses nouvelles colonnes (relevé de l'hôte, tours) : sans
  # elles, l'exécuteur redémarré échouerait
  exiger_migrations "$(ssh_vm "git -C $SRC rev-parse HEAD")" redemarrer
  # shellcheck disable=SC2206
  restantes=($PERSONAS_VM)
  etape "exécuteurs du serveur (${#restantes[@]}), un par un hors tour"
  while [ ${#restantes[@]} -gt 0 ]; do
    suivantes=()
    for p in "${restantes[@]}"; do
      etat=$(etat_agent "$p") \
        || { echo "$p: registry unreadable — nothing restarted" >&2; exit 1; }
      IFS='|' read -r st deja_pause <<<"$etat"
      # #1131 : persona arrêtée par le propriétaire — un déploiement ne la
      # réactive pas.
      # #1633 : son unité encore active est seulement rechargée sur le nouveau
      # code, sans bail ni tour ; une unité déjà arrêtée n'est pas relancée.
      # #2152 : un drain posé par ce script est levé.
      if [ "$st" = stopped ]; then
        lever_drain "$p" "persona arrêtée" || exit 1
        if ssh_vm "systemctl is-active --quiet ameesh-runner@$p"; then
          ssh_vm "systemctl restart ameesh-runner@$p; sleep 10; systemctl is-active --quiet ameesh-runner@$p" \
            || { echo "$p : unité d'une persona arrêtée, ne redémarre pas — journalctl -u ameesh-runner@$p -n 50" >&2; exit 1; }
          echo "$p : arrêté dans le registre — unité redémarrée pour charger le nouveau code, aucun tour"
        else
          echo "$p : arrêté dans le registre — unité déjà arrêtée, rien à recharger"
        fi
        continue
      fi
      if [ "$st" = running ]; then
        # #2152 : après DRAIN_ATTENTE, pause opérateur courte (drain) au lieu
        # d'attendre sans fin un agent qui enchaîne les tours ; une pause déjà
        # posée par un autre acteur n'est ni doublée ni levée.
        if [ "$deja_pause" != 1 ] && [ "$DRAIN_ATTENTE" -gt 0 ]; then
          if [ -z "${debut_tour[$p]:-}" ]; then debut_tour[$p]=$SECONDS; fi
          ecoule=$((SECONDS - debut_tour[$p]))
          if [ "$ecoule" -ge "$DRAIN_ATTENTE" ]; then
            poser_drain "$p" "$ecoule" || exit 1
          fi
        fi
        suivantes+=("$p"); continue
      fi
      # #502: only a runner that held its lease must take it again (bail.sh)
      ligne=$(lire_bail "$p") && heure=$(heure_base) && attendu=$(proprietaire_de "$p") \
        || { echo "$p: registry, database clock or ExecStart unreadable — nothing restarted" >&2; exit 1; }
      avant=$(bail_epoque <<<"$ligne")
      tenu=0
      bail_valide -1 "$attendu" "$heure" <<<"$ligne" && tenu=1
      ssh_vm "systemctl restart ameesh-runner@$p; sleep 10; systemctl is-active --quiet ameesh-runner@$p" \
        || { echo "$p : ne redémarre pas — journalctl -u ameesh-runner@$p -n 50 ; puis « $0 retour »" >&2; exit 1; }
      # #2152 : hors tour et unité relancée — la pause du drain est levée ici.
      lever_drain "$p" "redémarrage hors tour" || exit 1
      if [ "$tenu" = 0 ]; then
        echo "WARNING $p: not checked: no lease before the restart, no sign of life"
        non_verifiees+=("$p")
      elif ! attendu=$(proprietaire_de "$p") || ! attendre_bail "$avant" "$attendu" "$p"; then
        echo "$p: active, but did not take its lease again — journalctl -u ameesh-runner@$p -n 50; then « $0 retour »" >&2
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
  if [ ${#non_verifiees[@]} -gt 0 ]; then
    etape "WARNING: ${#non_verifiees[@]} persona(s) not checked (no lease before the restart, no sign of life): ${non_verifiees[*]}"
  fi
}

controler() {
  local echec=0
  for p in $PERSONAS_VM; do
    etape "$p"
    ssh_vm "systemctl is-active ameesh-runner@$p && journalctl -u ameesh-runner@$p -n 15 --no-pager" || echec=1
    "$AMEESH" show "$p" | head -4 || echec=1
  done
  etape "ameesh doctor sur le serveur"
  ssh_vm "runuser -l ameesh -c 'ameesh doctor' | tail -8" || echec=1
  [ "$echec" = 0 ] || { echo "contrôles en échec" >&2; exit 1; }
  echo; echo "contrôles verts"
}

retour() {
  etape "retour au commit d'avant, par la bascule (sans pip)"
  envoyer_bascule_vm
  ssh_vm "set -e; test -s $PRECEDENT
    python3 $BASCULE_VM retour --venv $VENV
    rm -f $BASCULE_VM
    git -C $SRC checkout -q --detach \"\$(cat $PRECEDENT)\"
    echo \"code : \$(git -C $SRC rev-parse --short HEAD)\""
  echo "base inchangée : les migrations passées avant l'installation sont additives, l'ancien code les ignore"
  redemarrer
}

if [ "$MODE" = agents ]; then
  [[ $ATTENTE_MAX =~ ^[0-9]+$ ]] || { echo "ATTENTE_MAX invalide" >&2; exit 2; }
  case "$ACTION" in
    redemarrer) redemarrer_agents ;;
    verifier|controler) controler_agents ;;
    installer) installer_agents "${2:-HEAD}" ;;
    retour) retour_agents ;;
  esac
  exit
fi

case "${1:-}" in
  verifier) verifier ;;
  installer) installer "${2:-HEAD}" ;;
  redemarrer) redemarrer ;;
  controler) controler ;;
  retour) retour ;;
  *) sed -n '4,28p' "$0" >&2; exit 2 ;;
esac
