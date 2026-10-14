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
#   installer    code REF dans /opt/ameesh/src (détaché), installé dans
#                /opt/ameesh/venv (dépendance psycopg comprise) ; refuse tant
#                que la base du mesh n'a pas toutes les migrations de REF
#   redemarrer   ameesh-runner@<persona>, une par une, chacune hors tour ;
#                refuse tant que les migrations du code installé manquent
#   controler    unités actives, journal récent, ameesh doctor sur le serveur
#   retour       réinstalle le commit d'avant et redémarre
#
#   En mode agents, installer et retour travaillent dans le layout du compte
#   ameesh (~/.local/share/ameesh : clone src, instantanés src-<sha7> jamais
#   écrasés, previous-sha, venv), avec les mêmes gardes de migrations ; le
#   redémarrage reste hors tour sous bail. L'installation ne redémarre
#   personne, le retour réinstalle l'ancien code puis redémarre et contrôle.
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
    redemarrer|controler|verifier|installer|retour) ;;
    *) echo "mode agents : verifier, installer, redemarrer, controler ou retour attendu" >&2; exit 2 ;;
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
# Envoie le commit $1 dans /tmp/ameesh.bundle sur la cible $2 (root@…) : paquet
# git du poste pour un clone qui n'atteint pas l'origine (le dépôt local doit
# contenir le commit). Le fichier distant est retiré par l'appelant, après le
# fetch. Lisible par le compte ameesh des VM d'agents (chmod 644).
paquet_git() {
  local sha=$1 hote=$2
  git -C "$DEPOT" update-ref refs/mise-a-jour/vm "$sha"
  git -C "$DEPOT" bundle create -q "$TRAVAIL/ameesh.bundle" refs/mise-a-jour/vm \
    $(git -C "$DEPOT" for-each-ref --format='^%(objectname)' refs/remotes/origin/main)
  git -C "$DEPOT" update-ref -d refs/mise-a-jour/vm
  chmod 644 "$TRAVAIL/ameesh.bundle"
  scp -q -o BatchMode=yes "$TRAVAIL/ameesh.bundle" "$hote:/tmp/ameesh.bundle"
  rm -f "$TRAVAIL/ameesh.bundle"
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

# --- mode agents : installer / retour (layout du compte ameesh) -------------
# Clone ~/.local/share/ameesh/src, venv ~/.local/share/ameesh/venv, code
# installé depuis des instantanés src-<sha7> jamais écrasés, sha du code
# d'avant dans previous-sha. Détail : docs/DEPLOIEMENT-HOTE-AGENTS.md.

installe_agents() {  # code réellement installé (direct_url.json -> src-<sha7>)
  utilisateur_agents bash -c 'set -euo pipefail
    V=/home/ameesh/.local/share/ameesh/venv
    for d in "$V"/lib/python*/site-packages/ameesh-*.dist-info; do
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
  local ref=${1:-HEAD} sha court avant complet courant
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
    utilisateur_agents bash -c 'git -C /home/ameesh/.local/share/ameesh/src fetch -q /tmp/ameesh.bundle refs/mise-a-jour/vm'
    ssh_agents "rm -f /tmp/ameesh.bundle"
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
  # 4. sauvegarde privée 0600 de l'état, instantané du code quitté
  utilisateur_agents bash -c "set -euo pipefail
    umask 077
    P=/home/ameesh/.local/share/ameesh
    T=/home/ameesh/.local/state/ameesh/mise-a-jour
    mkdir -p \"\$T\"
    cp /home/ameesh/.config/ameesh/env \"\$T/env-$court\"
    cp /home/ameesh/.config/systemd/user/ameesh-runner-agent@*.service \"\$T/\" 2>/dev/null || true
    courant=\$(git -C \"\$P/src\" rev-parse HEAD)
    c=\${courant:0:7}
    test -d \"\$P/src-\$c\" || cp -a \"\$P/src\" \"\$P/src-\$c\"
    echo \"sauvegarde : \$T/env-$court, unités et src-\$c\""
  # 5. révision visée : checkout puis instantané immuable, jamais écrasé
  utilisateur_agents bash -c "set -euo pipefail
    P=/home/ameesh/.local/share/ameesh
    git -C \"\$P/src\" checkout -q --detach '$sha'
    test \"\$(git -C \"\$P/src\" rev-parse HEAD)\" = '$sha'
    test -d \"\$P/src-$court\" || cp -a \"\$P/src\" \"\$P/src-$court\"
    test \"\$(git -C \"\$P/src-$court\" rev-parse HEAD)\" = '$sha'"
  # 6. installation depuis l'instantané : code puis dépendances
  etape "installation dans le venv (depuis src-$court)"
  utilisateur_agents bash -c "set -euo pipefail
    P=/home/ameesh/.local/share/ameesh; V=\$P/venv
    \$V/bin/pip install -q --force-reinstall --no-deps \$P/src-$court
    \$V/bin/pip install -q \$P/src-$court
    \$V/bin/python -c 'import psycopg, ameesh; print(\"psycopg\", psycopg.__version__)'"
  # 7. previous-sha seulement après une installation réussie
  utilisateur_agents bash -c "printf '%s\n' '$complet' > /home/ameesh/.local/share/ameesh/previous-sha"
  echo "installé depuis src-$court (avant $complet). Les exécuteurs tournent encore l'ancien code : « $0 agents redemarrer », puis « $0 agents controler »."
}

retour_agents() {
  local precedent court
  precedent=$(previous_agents) || true
  [ -n "$precedent" ] || { echo "previous-sha absent sur $HOTE_AGENTS : aucun retour possible" >&2; exit 1; }
  [[ $precedent =~ ^[0-9a-fA-F]{7,40}$ ]] || { echo "previous-sha invalide : $precedent" >&2; exit 1; }
  court=${precedent:0:7}
  etape "VM d'agents $HOTE_AGENTS ($AGENTS_SSH) : retour au commit ${precedent:0:9}"
  # le commit d'avant doit exister dans le clone, sinon paquet git du poste
  if ! utilisateur_agents bash -c "git -C /home/ameesh/.local/share/ameesh/src cat-file -e '$precedent^{commit}'" 2>/dev/null; then
    git -C "$DEPOT" cat-file -e "$precedent^{commit}" 2>/dev/null \
      || { echo "commit $precedent absent du dépôt local : retour impossible d'ici" >&2; exit 1; }
    etape "la VM n'a pas ce commit : envoi d'un paquet git"
    paquet_git "$precedent" "$AGENTS_SSH"
    utilisateur_agents bash -c 'git -C /home/ameesh/.local/share/ameesh/src fetch -q /tmp/ameesh.bundle refs/mise-a-jour/vm'
    ssh_agents "rm -f /tmp/ameesh.bundle"
  fi
  # instantané du code quitté (jamais écrasé), puis checkout de l'ancien
  utilisateur_agents bash -c "set -euo pipefail
    P=/home/ameesh/.local/share/ameesh
    courant=\$(git -C \"\$P/src\" rev-parse HEAD)
    c=\${courant:0:7}
    test -d \"\$P/src-\$c\" || cp -a \"\$P/src\" \"\$P/src-\$c\"
    git -C \"\$P/src\" checkout -q --detach '$precedent'
    test \"\$(git -C \"\$P/src\" rev-parse HEAD)\" = '$precedent'
    test -d \"\$P/src-$court\" || cp -a \"\$P/src\" \"\$P/src-$court\"
    test \"\$(git -C \"\$P/src-$court\" rev-parse HEAD)\" = '$precedent'"
  etape "réinstallation dans le venv (depuis src-$court)"
  utilisateur_agents bash -c "set -euo pipefail
    P=/home/ameesh/.local/share/ameesh; V=\$P/venv
    \$V/bin/pip install -q --force-reinstall --no-deps \$P/src-$court
    \$V/bin/pip install -q \$P/src-$court
    \$V/bin/python -c 'import psycopg, ameesh; print(\"psycopg\", psycopg.__version__)'"
  echo "code d'avant réinstallé depuis src-$court ; previous-sha inchangé (retour idempotent)."
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
    ssh_vm "set -e; git -C $SRC fetch -q /tmp/ameesh.bundle refs/mise-a-jour/vm; rm -f /tmp/ameesh.bundle"
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
