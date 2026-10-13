#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Mise à jour d'ameesh vers L60–L74 sur le SERVEUR du mesh (profil L55),
# lancée depuis le poste, étape par étape. Procédure : docs/BASCULE.md,
# « Mise à jour vers L60–L74 ». AUCUNE migration ici : `poste.sh migrer` l'a
# faite pour toute la base du mesh, qui est celle du serveur.
#
#   deploy/mise-a-jour/vm.sh <étape> [REF]
#   deploy/mise-a-jour/vm.sh agents verifier|redemarrer|controler
#     AGENTS_SSH=root@cible, HOTE_AGENTS=nom du registre dans BASCULE_ENV ;
#     unités utilisateur sous ameesh, liste du registre lue sur la VM.
#     Voir docs/DEPLOIEMENT-HOTE-AGENTS.md.
#
#   verifier     contrôles, rien n'est modifié
#   installer    code REF dans /opt/ameesh/src (détaché), installé dans
#                /opt/ameesh/venv (dépendance psycopg comprise) ; refuse tant
#                que la base du mesh n'a pas toutes les migrations de REF
#   redemarrer   ameesh-runner@<persona>, une par une, chacune hors tour ;
#                refuse tant que les migrations du code installé manquent
#   controler    unités actives, journal récent, ameesh doctor sur le serveur
#   retour       réinstalle le commit d'avant et redémarre
#
# REF (installer) : commit, branche ou étiquette du dépôt qui contient ce
# script ; défaut : HEAD de ce dépôt. Si le serveur ne le trouve pas chez
# l'origine (branche non poussée), il le reçoit en paquet git par SSH.
#
# Paramètres lus dans $BASCULE_ENV (défaut ~/.config/ameesh/bascule.env, celui
# de la bascule L57) : VM (adresse WireGuard), PERSONAS_VM (personas servies
# par le serveur). Aucun secret n'est lu ni affiché.
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
    redemarrer|controler|verifier) ;;
    *) echo "mode agents : verifier, redemarrer ou controler attendu" >&2; exit 2 ;;
  esac
  : "${AGENTS_SSH:?AGENTS_SSH manquant dans $BASCULE_ENV}" "${HOTE_AGENTS:?HOTE_AGENTS manquant dans $BASCULE_ENV}"
  [[ $AGENTS_SSH =~ ^[a-zA-Z0-9_.@:-]+$ && $AGENTS_SSH != -* ]] || { echo "AGENTS_SSH invalide" >&2; exit 2; }
  [[ $HOTE_AGENTS =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || { echo "HOTE_AGENTS invalide" >&2; exit 2; }
else
  : "${VM:?VM manquant dans $BASCULE_ENV}" "${PERSONAS_VM:?PERSONAS_VM manquant dans $BASCULE_ENV}"
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
mkdir -p "$TRAVAIL"; chmod 700 "$TRAVAIL"

etape() { printf '\n== %s\n' "$*"; }
ssh_vm() { ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$VM" "$@"; }
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
# Commandes sur la VM d'agents : utilisateur ameesh et son bus systemd,
# la base reste celle de la configuration du compte de l’unité.
ssh_agents() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$AGENTS_SSH" "$@"; }
utilisateur_agents() {
  local commande; printf -v commande '%q ' "$@"
  ssh_agents "set -euo pipefail
    uid=\$(id -u ameesh)
    runuser -u ameesh -- env HOME=/home/ameesh XDG_RUNTIME_DIR=/run/user/\$uid \
      DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/\$uid/bus $commande"
}
commande_agents() {
  # Use the runner's systemd EnvironmentFile parser, including literal $/#.
  utilisateur_agents bash -c 'set -euo pipefail
    test -r "$HOME/.config/ameesh/env" \
      || { echo "runner environment file unreadable: $HOME/.config/ameesh/env" >&2; exit 1; }
    exec systemd-run --user --pipe --wait --quiet \
      -p EnvironmentFile=%h/.config/ameesh/env -- "$@"' -- "$@"
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
agents_du_registre() {
  local noms
  noms=$(ameesh_agents list --json | python3 -c 'import json,re,sys
rows = json.load(sys.stdin)
# Même règle que ameesh.config.NAME_RE (config.py:48), utilisée par
# registry._check_name : 1 à 64 caractères, lettres/chiffres puis . _ -.
names = set()
for r in rows:
    if r.get("host") != sys.argv[1] or (r.get("mode") or "execute") != "execute" or r.get("status") == "stopped":
        continue
    name = r.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        print("agent " + repr(name) + " : nom invalide dans le registre, ignoré", file=sys.stderr)
        continue
    names.add(name)
names = sorted(names)
if not names: sys.exit("aucun agent à mener sur " + sys.argv[1])
print("\n".join(names))' "$HOTE_AGENTS") || return 1
  printf '%s\n' "$noms"
}

unite_agents() { printf 'ameesh-runner-agent@%s.service' "$1"; }
echec_agent() {
  echo "$1 : $2 — journalctl --user -u $(unite_agents "$1") -n 50 (utilisateur ameesh sur $AGENTS_SSH)" >&2
}
redemarrer_agents() {
  local noms p st avant ligne heure attendu tenu restantes suivantes debut=$SECONDS
  local non_verifiees=()
  noms=$(agents_du_registre) || return 1
  mapfile -t restantes <<<"$noms"
  etape "exécuteurs de $HOTE_AGENTS (${#restantes[@]}), un par un hors tour"
  while [ "${#restantes[@]}" -gt 0 ]; do
    suivantes=()
    for p in "${restantes[@]}"; do
      # Le placement peut changer pendant l'attente d'un tour : relire avant
      # de toucher une unité que cet hôte ne doit plus mener.
      st=$(ameesh_agents show "$p" --json | python3 -c 'import json,sys
r=json.load(sys.stdin)
if r.get("host") != sys.argv[1] or (r.get("mode") or "execute") != "execute" or r.get("status") == "stopped":
    print("skip")
else: print(r.get("status") or "")' "$HOTE_AGENTS") || return 1
      if [ "$st" = skip ]; then echo "$p : placement modifié ou arrêté, ignoré"; continue; fi
      if [ "$st" = running ]; then suivantes+=("$p"); continue; fi
      ligne=$(lire_bail "$p") && heure=$(heure_base) && attendu=$(proprietaire_de "$p") \
        || { echec_agent "$p" "registre, heure de base ou ExecStart illisible, aucun restart"; return 1; }
      avant=$(bail_epoque <<<"$ligne")
      tenu=0
      bail_valide -1 "$attendu" "$heure" <<<"$ligne" && tenu=1
      utilisateur_agents systemctl --user restart "$(unite_agents "$p")" \
        && utilisateur_agents systemctl --user is-active --quiet "$(unite_agents "$p")" \
        || { echec_agent "$p" "ne redémarre pas"; return 1; }
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

controler_agents() {
  local noms p ligne heure attendu echec=0
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
lire_bail() { "$AMEESH" show "$1" --json; }  # persona → its registry row
heure_base() {  # the clock of the mesh's database (the server's), epoch seconds
  ssh_vm "runuser -u postgres -- psql -X -At -d ameesh -c 'SELECT extract(epoch FROM clock_timestamp())'"
}
proprietaire_de() {  # persona → the lease owner of ameesh-runner@<p>: --runner-id, else :<MainPID>
  local sortie id
  sortie=$(ssh_vm "systemctl show -p ExecStart --value ameesh-runner@$1") \
    && id=$(bail_runner_id <<<"$sortie") \
    || { echo "ameesh-runner@$1: ExecStart unreadable" >&2; return 1; }
  if [ -n "$id" ]; then echo "$id"
  else echo ":$(ssh_vm "systemctl show -p MainPID --value ameesh-runner@$1")"; fi
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
    git -C "$DEPOT" update-ref refs/mise-a-jour/vm "$sha"
    git -C "$DEPOT" bundle create -q "$TRAVAIL/ameesh.bundle" refs/mise-a-jour/vm \
      $(git -C "$DEPOT" for-each-ref --format='^%(objectname)' refs/remotes/origin/main)
    git -C "$DEPOT" update-ref -d refs/mise-a-jour/vm
    scp -q -o BatchMode=yes "$TRAVAIL/ameesh.bundle" "root@$VM:/tmp/ameesh.bundle"
    ssh_vm "set -e; git -C $SRC fetch -q /tmp/ameesh.bundle refs/mise-a-jour/vm; rm -f /tmp/ameesh.bundle"
    rm -f "$TRAVAIL/ameesh.bundle"
  fi
  # Le venv du serveur sert aussi les commandes des agents (mail send…) : le
  # nouveau code n'y entre qu'une fois la base migrée (10/10, L151)
  etape "migrations de ${sha:0:9} passées sur la base du mesh"
  exiger_migrations "$sha" installer
  etape "extraction et installation dans $VENV"
  ssh_vm "set -e
    git -C $SRC checkout -q --detach '$sha'
    test \"\$(git -C $SRC rev-parse HEAD)\" = '$sha'
    $VENV/bin/pip install -q --force-reinstall --no-deps $SRC
    $VENV/bin/pip install -q $SRC
    $VENV/bin/python -c 'import psycopg, ameesh.budget, ameesh.projects; print(\"psycopg\", psycopg.__version__, \": modules L70 et L62 présents\")'"
  echo "installé. Les exécuteurs tournent encore l'ancien code : redemarrer, sans tarder."
}

redemarrer() {
  # shellcheck source=bail.sh
  . "$ICI/bail.sh"
  local restantes suivantes debut=$SECONDS st avant ligne heure tenu attendu
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
      st=$(statut "$p")
      if [ "$st" = running ]; then suivantes+=("$p"); continue; fi
      # #502: only a runner that held its lease must take it again (bail.sh)
      ligne=$(lire_bail "$p") && heure=$(heure_base) && attendu=$(proprietaire_de "$p") \
        || { echo "$p: registry, database clock or ExecStart unreadable — nothing restarted" >&2; exit 1; }
      avant=$(bail_epoque <<<"$ligne")
      tenu=0
      bail_valide -1 "$attendu" "$heure" <<<"$ligne" && tenu=1
      ssh_vm "systemctl restart ameesh-runner@$p; sleep 10; systemctl is-active --quiet ameesh-runner@$p" \
        || { echo "$p : ne redémarre pas — journalctl -u ameesh-runner@$p -n 50 ; puis « $0 retour »" >&2; exit 1; }
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
  etape "retour au commit d'avant"
  ssh_vm "set -e; test -s $PRECEDENT
    git -C $SRC checkout -q --detach \"\$(cat $PRECEDENT)\"
    $VENV/bin/pip install -q --force-reinstall --no-deps $SRC
    echo \"code : \$(git -C $SRC rev-parse --short HEAD)\""
  echo "base inchangée : les migrations passées avant l'installation sont additives, l'ancien code les ignore"
  redemarrer
}

if [ "$MODE" = agents ]; then
  [[ $ATTENTE_MAX =~ ^[0-9]+$ ]] || { echo "ATTENTE_MAX invalide" >&2; exit 2; }
  case "$ACTION" in
    redemarrer) redemarrer_agents ;;
    verifier|controler) controler_agents ;;
  esac
  exit
fi

case "${1:-}" in
  verifier) verifier ;;
  installer) installer "${2:-HEAD}" ;;
  redemarrer) redemarrer ;;
  controler) controler ;;
  retour) retour ;;
  *) sed -n '4,22p' "$0" >&2; exit 2 ;;
esac
