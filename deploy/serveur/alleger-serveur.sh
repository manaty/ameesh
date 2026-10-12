#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Allège le serveur du mesh (manaty-mesh-1 : base du mesh ET exécuteurs des
# personas) : plafond CPU sur la slice des agents, et déménagement de
# personas vers une VM d'agents (ameesh-agents-1). Demande du bureau,
# 2026-10-12 16:39 (charge 24 pour 4 vCPU, 561 Mo libres).
#
# Lancé par le PROPRIÉTAIRE des VM, depuis son poste (comme vm.sh) : SSH en
# root, ou en compte sudo (sudo -n), sur les deux VM. Idempotent : chaque
# étape regarde l'état avant d'agir, et se relance sans dommage. Tout script
# envoyé à une VM s'arrête à la première erreur (set -euo pipefail).
#
#   deploy/serveur/alleger-serveur.sh verifier
#   deploy/serveur/alleger-serveur.sh appliquer [cpu|deplacer|tout]
#   deploy/serveur/alleger-serveur.sh retour    [cpu|deplacer|tout]
#
#   verifier   rien n'est modifié : charge et mémoire du serveur, plafond CPU
#              effectif de la slice, unités des personas sur les deux VM,
#              statut et lots en build de chaque persona, état de ses
#              dossiers de travail (modifications, commits non poussés)
#   appliquer  cpu : complément /etc/systemd/system/ameesh-agents.slice.d/
#              20-cpu.conf (CPUQuota), pris en compte au daemon-reload, sans
#              redémarrer personne (les tours en cours sont ralentis, jamais
#              tués) ; le complément d'avant (ou son absence) est gardé.
#              deplacer : pour chaque persona, une fois le canon fusionné et
#              synchronisé, hors tour, dossiers des deux côtés propres et
#              poussés : état des deux unités gardé, exécuteur du serveur
#              arrêté et désactivé, dossier de travail (worktree) créé sur la
#              VM d'agents, unité utilisateur activée
#   retour     l'inverse : personas ramenées (après le revert du canon), hors
#              tour, dossiers propres, chaque unité remise dans l'état gardé ;
#              complément d'avant remis (ou retiré s'il n'y en avait pas)
#
# Ordre : verifier → fusion de la PR du canon → appliquer → verifier. Le
# canon d'abord : une fois le placement synchronisé, l'exécuteur du serveur
# finit son tour et rend le bail (L147), il ne prend plus de tour de travail ;
# celui de la VM d'agents refuse une persona que le registre place ailleurs.
#
# L'état d'avant est gardé sur chaque VM (root), dans $ETAT : le retour ne
# remet que ce qui y est écrit, et refuse sans lui.
#
# Paramètres (environnement) :
#   MESH_SSH      cible SSH du serveur (défaut : root@$VM de $BASCULE_ENV)
#   AGENTS_SSH    cible SSH de la VM d'agents (obligatoire pour deplacer)
#   PERSONAS      personas déplacées (défaut : deepseek5 deepseek7)
#   CPU_QUOTA     plafond de la slice des agents du serveur, 1 à 4 chiffres
#                 suivis de % (défaut : 250%)
#   HOTE_AGENTS   nom canon de la VM d'agents (défaut : ameesh-agents-1)
#   HOTE_MESH     nom canon du serveur (défaut : manaty-mesh-1)
#   ATTENTE_MAX   attente maximale de la fin d'un tour, en s (défaut : 7200)
#   FORCER_DOSSIER=1  déplacer même si le dossier de travail QUITTÉ a des
#                 fichiers modifiés ou des commits non poussés (ils restent
#                 sur l'ancien hôte, jamais effacés ; l'agent repart sans
#                 eux). Le dossier d'arrivée doit toujours être propre.
#   AMEESH        CLI ameesh du poste, pour lire registre et lots
# Aucun secret n'est lu ni affiché.
set -euo pipefail
shopt -s inherit_errexit

usage() { sed -n '/^# Allège/,/^# Aucun secret/p' "$0" >&2; exit 2; }

BASCULE_ENV=${BASCULE_ENV:-$HOME/.config/ameesh/bascule.env}
if [ -z "${MESH_SSH:-}" ] && [ -r "$BASCULE_ENV" ]; then
  # shellcheck disable=SC1090
  . "$BASCULE_ENV"
  [ -z "${VM:-}" ] || MESH_SSH="root@$VM"
fi
: "${MESH_SSH:?MESH_SSH manquant (ou VM dans $BASCULE_ENV)}"
AGENTS_SSH=${AGENTS_SSH:-}
PERSONAS=${PERSONAS:-deepseek5 deepseek7}
CPU_QUOTA=${CPU_QUOTA:-250%}
HOTE_AGENTS=${HOTE_AGENTS:-ameesh-agents-1}
HOTE_MESH=${HOTE_MESH:-manaty-mesh-1}
ATTENTE_MAX=${ATTENTE_MAX:-7200}
if [ -z "${AMEESH:-}" ]; then
  AMEESH=$(command -v ameesh || echo "$HOME/.local/share/ameesh/venv/bin/ameesh")
fi
unset AMEESH_DSN AGENT_MESH_DSN  # registre et lots se lisent sur la base du mesh

# Tout ce qui entre dans un script root est validé ici, avant le premier SSH.
[[ $CPU_QUOTA =~ ^[0-9]{1,4}%$ ]] || { echo "CPU_QUOTA invalide : « $CPU_QUOTA » (attendu : 1 à 4 chiffres suivis de %)" >&2; exit 2; }
[ "${CPU_QUOTA%\%}" -gt 0 ] || { echo "CPU_QUOTA nul" >&2; exit 2; }
[[ $ATTENTE_MAX =~ ^[0-9]+$ ]] || { echo "ATTENTE_MAX invalide : « $ATTENTE_MAX »" >&2; exit 2; }
for p in $PERSONAS; do
  [[ $p =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { echo "persona invalide : « $p »" >&2; exit 2; }
done
[ -n "${PERSONAS// /}" ] || { echo "PERSONAS vide" >&2; exit 2; }

SLICE=ameesh-agents.slice
COMPLEMENT=/etc/systemd/system/$SLICE.d/20-cpu.conf
ETAT=/var/lib/ameesh/alleger-serveur  # état d'avant, sur chaque VM (root)
DOSSIER_MESH='/home/ameesh/nexlink-%s'        # policy.work_roots du serveur
DOSSIER_AGENTS='/home/ameesh/work/nexlink-%s' # policy.work_roots de la VM d'agents
# dépôt nexlink de la VM d'agents : chaque agent y a son worktree, comme
# deepseek2 à deepseek4
DEPOT_AGENTS=/home/ameesh/work/nexlink

etape() { printf '\n== %s\n' "$*"; }
ok() { printf 'ok         %s\n' "$*"; }
attention() { printf 'attention  %s\n' "$*"; ATTENTION=1; }
ATTENTION=0

# Préambule de TOUT script envoyé à une VM : arrêt à la première erreur, y
# compris dans un pipeline ou une substitution ; `utilisateur …` lance une
# commande sous l'utilisateur ameesh, avec son systemd utilisateur ;
# `quota_effectif` lit le plafond CPU appliqué à la slice, en %.
PREAMBULE='set -euo pipefail
shopt -s inherit_errexit
utilisateur() { local u; u=$(id -u ameesh)
  runuser -u ameesh -- env HOME=/home/ameesh XDG_RUNTIME_DIR=/run/user/$u \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$u/bus "$@"; }
quota_effectif() {
  systemctl show '"$SLICE"' -p CPUQuotaPerSecUSec --value | awk '\''{
    if ($0 == "infinity") { print "aucun"; exit }
    t = 0
    for (i = 1; i <= NF; i++) { f = $i
      if (f ~ /ms$/) { sub(/ms$/, "", f); t += f / 1000 }
      else if (f ~ /us$/) { sub(/us$/, "", f); t += f / 1000000 }
      else if (f ~ /min$/) { sub(/min$/, "", f); t += f * 60 }
      else { sub(/s$/, "", f); t += f } }
    printf "%d%%\n", t * 100 + 0.5 }'\''; }'

# Script root sur une VM (stdin, précédé du préambule), en root ou par sudo -n.
root_sur() {
  { printf '%s\n' "$PREAMBULE"; cat; } | ssh -o BatchMode=yes -o ConnectTimeout=10 "$1" \
    'if [ "$(id -u)" = 0 ]; then bash -s; else sudo -n bash -s; fi'
}
mesh() { root_sur "$MESH_SSH"; }
agents() {
  [ -n "$AGENTS_SSH" ] || { echo "AGENTS_SSH manquant" >&2; exit 2; }
  root_sur "$AGENTS_SSH"
}

champ() {  # $1 persona, $2 champ du registre ; échoue si la lecture échoue
  "$AMEESH" show "$1" --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]) or "")' "$2"
}
lots_en_build() {  # $1 persona : identifiants, séparés par des espaces
  "$AMEESH" work list --assignee "$1" --state build --json \
    | python3 -c 'import json,sys
d=json.load(sys.stdin); d=d.get("items", d) if isinstance(d, dict) else d
print(" ".join(str(r["id"]) for r in d))'
}
# État du dossier de travail ($2 : chemin, clone ou worktree) de la persona
# $3 sur une VM ($1 : mesh|agents) : « absent », « propre », ou ce qui n'est
# pas en sécurité chez l'origine. Le dépôt peut être partagé entre agents
# (worktrees) : seuls comptent HEAD et les branches agent/<persona>-….
# Une lecture git qui échoue fait échouer la fonction (jamais « propre »).
etat_dossier() {
  "$1" <<EOF
d='$2'
[ -e "\$d/.git" ] || { echo absent; exit 0; }
g() { runuser -u ameesh -- env HOME=/home/ameesh git -C "\$d" "\$@"; }
sale=\$(g status --porcelain --untracked-files=normal | wc -l)
refs=\$(g for-each-ref --format='%(refname)' 'refs/heads/agent/$3-*' 'refs/heads/agent/$3/*')
# shellcheck disable=SC2086
local_seul=\$(g log --oneline HEAD \$refs --not --remotes | wc -l)
branche=\$(g rev-parse --abbrev-ref HEAD)
if [ "\$sale" = 0 ] && [ "\$local_seul" = 0 ]; then echo propre
else echo "\$sale fichier(s) modifié(s) ou non suivi(s), \$local_seul commit(s) non poussé(s) (branche \$branche)"
fi
EOF
}

# --- verifier -----------------------------------------------------------------

verifier() {
  local p hote statut lots dm da
  etape "serveur ($MESH_SSH)"
  mesh <<EOF
echo "hôte : \$(hostname) ; charge : \$(cut -d' ' -f1-3 /proc/loadavg) ; \$(nproc) vCPU ; mémoire disponible : \$(awk '/MemAvailable/ {printf "%d Mo", \$2/1024}' /proc/meminfo)"
echo "$SLICE : plafond CPU effectif \$(quota_effectif), MemoryMax \$(systemctl show $SLICE -p MemoryMax --value)"
if [ -f $COMPLEMENT ]; then echo "complément : \$(grep -h '^CPUQuota' $COMPLEMENT || echo 'sans CPUQuota')"; else echo "complément : absent"; fi
if [ -d $ETAT ]; then echo "état d'avant gardé : \$(ls $ETAT | paste -sd' ')"; fi
for u in \$(systemctl list-units 'ameesh-runner@*' --all --plain --no-legend | awk '{print \$1}'); do
  echo "\$u : \$(systemctl is-active \$u || true), \$(systemctl is-enabled \$u 2>/dev/null || true), slice \$(systemctl show \$u -p Slice --value)"
done
EOF
  if [ -n "$AGENTS_SSH" ]; then
    etape "VM d'agents ($AGENTS_SSH)"
    agents <<EOF
echo "hôte : \$(hostname) ; charge : \$(cut -d' ' -f1-3 /proc/loadavg) ; \$(nproc) vCPU ; mémoire disponible : \$(awk '/MemAvailable/ {printf "%d Mo", \$2/1024}' /proc/meminfo)"
if [ -d $ETAT ]; then echo "état d'avant gardé : \$(ls $ETAT | paste -sd' ')"; fi
utilisateur systemctl --user list-units 'ameesh-runner-agent@*' --all --plain --no-legend | awk '{print \$1" : "\$3}'
EOF
  fi
  for p in $PERSONAS; do
    etape "$p"
    hote=$(champ "$p" host); statut=$(champ "$p" status); lots=$(lots_en_build "$p")
    echo "registre : hôte $hote, statut $statut, bail $(champ "$p" lease_owner)"
    if [ -n "$lots" ]; then attention "$p a des lots en build : $lots (déménager entre deux lots)"
    else ok "$p n'a aucun lot en build"; fi
    # shellcheck disable=SC2059
    dm=$(etat_dossier mesh "$(printf "$DOSSIER_MESH" "$p")" "$p") || dm="illisible (erreur ssh ou git)"
    case "$dm" in propre|absent) ok "dossier du serveur : $dm" ;; *) attention "dossier du serveur : $dm" ;; esac
    if [ -n "$AGENTS_SSH" ]; then
      # shellcheck disable=SC2059
      da=$(etat_dossier agents "$(printf "$DOSSIER_AGENTS" "$p")" "$p") || da="illisible (erreur ssh ou git)"
      case "$da" in propre|absent) ok "dossier de la VM d'agents : $da" ;; *) attention "dossier de la VM d'agents : $da" ;; esac
    fi
    case "$hote" in
      "$HOTE_AGENTS") ok "placé sur $HOTE_AGENTS (canon synchronisé)" ;;
      "$HOTE_MESH") echo "placé sur $HOTE_MESH : la PR du canon n'est pas encore fusionnée et synchronisée" ;;
      *) attention "placé sur « $hote »" ;;
    esac
  done
  echo
  if [ "$ATTENTION" = 0 ]; then echo "aucune attention"; else echo "des points d'attention (ci-dessus)"; fi
}

# --- plafond CPU ----------------------------------------------------------------

appliquer_cpu() {
  etape "plafond CPU de $SLICE sur le serveur : $CPU_QUOTA"
  mesh <<EOF
systemctl cat $SLICE >/dev/null 2>&1 || { echo "$SLICE absente : l'installer d'abord (docs/EXPLOITATION.md, « Plafonds mémoire des agents »)" >&2; exit 1; }
install -d -m 0700 $ETAT
# l'état d'avant n'est gardé qu'une fois : une relance ne l'écrase pas
if [ ! -e $ETAT/cpu.avant ] && [ ! -e $ETAT/cpu.absent ]; then
  rm -f $ETAT/cpu.remis
  if [ -e $COMPLEMENT ]; then cp -p $COMPLEMENT $ETAT/cpu.avant; echo "complément d'avant gardé"
  else : > $ETAT/cpu.absent; echo "pas de complément avant : noté"; fi
fi
voulu='# alleger-serveur.sh (bureau, 2026-10-12) : la base du mesh garde du CPU
[Slice]
CPUQuota=$CPU_QUOTA'
if [ "\$(cat $COMPLEMENT 2>/dev/null || true)" = "\$voulu" ]; then
  echo "complément déjà en place"
else
  install -d -m 0755 "\$(dirname $COMPLEMENT)"
  printf '%s\n' "\$voulu" > $COMPLEMENT.tmp
  chmod 0644 $COMPLEMENT.tmp
  mv $COMPLEMENT.tmp $COMPLEMENT
  echo "complément écrit"
fi
# daemon-reload applique le plafond au cgroup de la slice, sans redémarrer
# d'unité ; pas de set-property --runtime, qui primerait sur /etc au retour
systemctl daemon-reload
effectif=\$(quota_effectif)
if [ "\$effectif" != "$CPU_QUOTA" ]; then
  echo "plafond effectif \$effectif, pas $CPU_QUOTA : un autre complément l'emporte (systemctl cat $SLICE ; /run/systemd/system.control, /etc/systemd/system.control)" >&2
  exit 1
fi
echo "$SLICE : plafond CPU effectif \$effectif"
hors=\$(for u in \$(systemctl list-units 'ameesh-runner@*' --state=active --plain --no-legend | awk '{print \$1}'); do
  [ "\$(systemctl show \$u -p Slice --value)" = $SLICE ] || echo \$u; done)
[ -z "\$hors" ] || echo "attention : hors de $SLICE, donc hors plafond : \$hors (complément 10-plafonds.conf, puis redémarrage hors tour : vm.sh redemarrer)"
EOF
}

retour_cpu() {
  etape "plafond CPU de $SLICE : complément d'avant remis sur le serveur"
  mesh <<EOF
if [ -e $ETAT/cpu.avant ]; then
  cp -p $ETAT/cpu.avant $COMPLEMENT.tmp
  mv $COMPLEMENT.tmp $COMPLEMENT
  echo "complément d'avant remis"
elif [ -e $ETAT/cpu.absent ]; then
  rm -f $COMPLEMENT
  echo "complément retiré (il n'y en avait pas avant)"
elif [ -e $ETAT/cpu.remis ]; then
  echo "complément d'avant déjà remis : rien à faire"
  exit 0
else
  echo "aucun état d'avant gardé dans $ETAT : rien n'est touché (à remettre à la main)" >&2
  exit 1
fi
systemctl daemon-reload
rm -f $ETAT/cpu.avant $ETAT/cpu.absent
: > $ETAT/cpu.remis  # une relance du retour ne refuse pas
echo "$SLICE : plafond CPU effectif \$(quota_effectif)"
EOF
}

# --- déménagement ---------------------------------------------------------------

# Attend que la persona soit hors tour. Un statut illisible ou vide arrête
# tout : jamais un arrêt sur la foi d'une lecture ratée.
attendre_hors_tour() {  # $1 persona
  local debut=$SECONDS st lots
  while :; do
    st=$(champ "$1" status) || { echo "$1 : statut illisible (base du mesh ?) : rien n'est déplacé" >&2; return 1; }
    [ -n "$st" ] || { echo "$1 : statut vide dans le registre : rien n'est déplacé" >&2; return 1; }
    [ "$st" = running ] || break
    [ $((SECONDS - debut)) -lt "$ATTENTE_MAX" ] \
      || { echo "$1 toujours en tour après ${ATTENTE_MAX}s : relancer plus tard" >&2; return 1; }
    echo "$1 en tour, attente…"; sleep 30
  done
  lots=$(lots_en_build "$1") || { echo "$1 : lots illisibles : rien n'est déplacé" >&2; return 1; }
  [ -z "$lots" ] || echo "attention : $1 a des lots en build ($lots) : il les reprendra sur son nouvel hôte, depuis leur branche poussée"
}

# $1 mesh|agents, $2 persona, $3 modèle du chemin, $4 depart|arrivee : le
# dossier quitté peut être forcé (FORCER_DOSSIER=1), celui d'arrivée jamais.
exiger_dossier_propre() {
  local etat
  # shellcheck disable=SC2059
  etat=$(etat_dossier "$1" "$(printf "$3" "$2")" "$2") \
    || { echo "$2 : dossier de travail illisible ($1) : rien n'est déplacé" >&2; return 1; }
  case "$etat" in
    propre|absent) return 0 ;;
  esac
  if [ "$4" = depart ] && [ "${FORCER_DOSSIER:-0}" = 1 ]; then
    echo "attention : $2 : dossier quitté pas en sécurité chez l'origine ($etat) ; déplacé quand même (FORCER_DOSSIER=1), le dossier reste sur l'ancien hôte"
    return 0
  fi
  if [ "$4" = depart ]; then
    echo "$2 : le dossier quitté ($1) n'est pas en sécurité chez l'origine ($etat) ; rien n'est déplacé. L'agent pousse son travail (ou le propriétaire), puis relancer ; ou FORCER_DOSSIER=1." >&2
  else
    echo "$2 : le dossier d'arrivée ($1) a du travail d'un séjour précédent ($etat) ; rien n'est déplacé. Le pousser ou le ranger à la main, puis relancer." >&2
  fi
  return 1
}

deplacer() {  # $1 persona
  local p=$1 hote dossier
  etape "$p : $HOTE_MESH → $HOTE_AGENTS"
  hote=$(champ "$p" host) || { echo "$p : registre illisible (base du mesh ?) : rien n'est déplacé" >&2; return 1; }
  if [ "$hote" != "$HOTE_AGENTS" ]; then
    echo "$p est placé sur « $hote » : fusionner la PR du canon, puis « ameesh canon sync » (ou attendre la passe d'un exécuteur), et relancer" >&2
    return 1
  fi
  attendre_hors_tour "$p"
  exiger_dossier_propre mesh "$p" "$DOSSIER_MESH" depart
  exiger_dossier_propre agents "$p" "$DOSSIER_AGENTS" arrivee
  # Hors tour, le placement synchronisé : l'arrêt drainé est immédiat ou attend
  # la fin du tour (TimeoutStopSec 35 min), jamais un tour tué. L'état de
  # l'unité est gardé avant, une seule fois.
  mesh <<EOF
u=ameesh-runner@$p
install -d -m 0700 $ETAT
if [ ! -e $ETAT/$p.unite ]; then
  rm -f $ETAT/$p.remise
  printf '%s %s\n' "\$(systemctl is-enabled \$u 2>/dev/null || true)" "\$(systemctl is-active \$u || true)" > $ETAT/$p.unite
  echo "état d'avant de \$u gardé : \$(cat $ETAT/$p.unite)"
fi
if systemctl is-active --quiet \$u || systemctl is-enabled --quiet \$u 2>/dev/null; then
  systemctl disable --now \$u; echo "\$u arrêté et désactivé"
else echo "\$u déjà arrêté et désactivé"; fi
EOF
  # shellcheck disable=SC2059
  dossier=$(printf "$DOSSIER_AGENTS" "$p")
  agents <<EOF
u=ameesh-runner-agent@$p
install -d -m 0700 $ETAT
if [ ! -e $ETAT/$p.unite ]; then
  rm -f $ETAT/$p.remise
  printf '%s %s\n' "\$(utilisateur systemctl --user is-enabled \$u 2>/dev/null || true)" "\$(utilisateur systemctl --user is-active \$u || true)" > $ETAT/$p.unite
  echo "état d'avant de \$u gardé : \$(cat $ETAT/$p.unite)"
fi
if [ -e '$dossier/.git' ]; then echo "dossier de travail déjà présent, propre : $dossier"
else
  utilisateur git -C '$DEPOT_AGENTS' fetch -q origin
  utilisateur git -C '$DEPOT_AGENTS' worktree add -q --detach '$dossier' origin/main
  echo "worktree créé : $dossier (détaché sur origin/main ; l'agent prend la branche de son lot)"
fi
utilisateur systemctl --user enable --now \$u
sleep 10
utilisateur systemctl --user is-active --quiet \$u || { echo "\$u ne démarre pas : journalctl --user -u \$u -n 50 (sous ameesh)" >&2; exit 1; }
echo "\$u actif"
EOF
  sleep 20
  echo "registre : hôte $(champ "$p" host), statut $(champ "$p" status), bail $(champ "$p" lease_owner)"
}

# Remet une unité dans l'état gardé ($ETAT/<persona>.unite : « activation
# activité »), sur la VM dont le script reçoit ce bloc. $1 : préfixe de
# commande (vide, ou « utilisateur ») ; $2 : options de systemctl ; $3 :
# unité ; $4 : persona.
remettre_unite() {
  cat <<EOF
f=$ETAT/$4.unite
if [ ! -e \$f ] && [ -e $ETAT/$4.remise ]; then echo "$3 déjà remise dans son état d'avant : rien à faire"; exit 0; fi
[ -s \$f ] || { echo "état d'avant de $3 inconnu (\$f) : rien n'est touché, à remettre à la main" >&2; exit 1; }
read -r activation activite < \$f
case "\$activation" in
  enabled) $1 systemctl $2 enable $3 ;;
  *) $1 systemctl $2 disable $3 2>/dev/null || true ;;
esac
if [ "\$activite" = active ]; then
  $1 systemctl $2 start $3; sleep 10
  $1 systemctl $2 is-active --quiet $3 || { echo "$3 ne démarre pas" >&2; exit 1; }
else
  $1 systemctl $2 stop $3
fi
mv \$f $ETAT/$4.remise  # une relance du retour ne refuse pas
echo "$3 remise dans son état d'avant : \$activation, \$activite"
EOF
}

ramener() {  # $1 persona
  local p=$1 hote
  etape "$p : $HOTE_AGENTS → $HOTE_MESH"
  hote=$(champ "$p" host) || { echo "$p : registre illisible (base du mesh ?) : rien n'est déplacé" >&2; return 1; }
  if [ "$hote" != "$HOTE_MESH" ]; then
    echo "$p est placé sur « $hote » : fusionner le revert de la PR du canon, synchroniser, et relancer" >&2
    return 1
  fi
  attendre_hors_tour "$p"
  exiger_dossier_propre agents "$p" "$DOSSIER_AGENTS" depart
  exiger_dossier_propre mesh "$p" "$DOSSIER_MESH" arrivee
  # d'abord l'hôte quitté (arrêt drainé), puis celui d'arrivée ; le dossier
  # de la VM d'agents est gardé
  remettre_unite utilisateur --user "ameesh-runner-agent@$p" "$p" | agents
  remettre_unite "" "" "ameesh-runner@$p" "$p" | mesh
}

case "${1:-}" in
  verifier) verifier ;;
  appliquer|retour)
    quoi=${2:-tout}
    case "$quoi" in cpu|deplacer|tout) ;; *) usage ;; esac
    # des if, jamais `a || b` : une fonction appelée ainsi perd set -e
    if [ "$1" = appliquer ]; then
      if [ "$quoi" != deplacer ]; then appliquer_cpu; fi
      if [ "$quoi" != cpu ]; then for p in $PERSONAS; do deplacer "$p"; done; fi
    else
      if [ "$quoi" != cpu ]; then for p in $PERSONAS; do ramener "$p"; done; fi
      if [ "$quoi" != deplacer ]; then retour_cpu; fi
    fi ;;
  *) usage ;;
esac
