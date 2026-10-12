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
# étape regarde l'état avant d'agir, et se relance sans dommage.
#
#   deploy/serveur/alleger-serveur.sh verifier
#   deploy/serveur/alleger-serveur.sh appliquer [cpu|deplacer|tout]
#   deploy/serveur/alleger-serveur.sh retour    [cpu|deplacer|tout]
#
#   verifier   rien n'est modifié : charge et mémoire du serveur, plafond CPU
#              de la slice, unités des personas sur les deux VM, statut et
#              lots en build de chaque persona, état de son dossier de
#              travail (modifications non commitées, commits non poussés)
#   appliquer  cpu : complément /etc/systemd/system/ameesh-agents.slice.d/
#              20-cpu.conf (CPUQuota), appliqué tout de suite, sans
#              redémarrer personne (les tours en cours sont ralentis, jamais
#              tués) ; deplacer : pour chaque persona, une fois le canon
#              fusionné et synchronisé, hors tour, dossier propre et poussé :
#              exécuteur du serveur arrêté et désactivé, dossier de travail
#              (worktree) créé sur la VM d'agents, unité utilisateur activée
#   retour     l'inverse : complément retiré et plafond levé ; personas
#              ramenées (après le revert du canon), hors tour, dossier de la
#              VM d'agents propre et poussé
#
# Ordre : verifier → fusion de la PR du canon → appliquer → verifier. Le
# canon d'abord : une fois le placement synchronisé, l'exécuteur du serveur
# finit son tour et rend le bail (L147), il ne prend plus de tour de travail ;
# celui de la VM d'agents refuse une persona que le registre place ailleurs.
#
# Paramètres (environnement) :
#   MESH_SSH      cible SSH du serveur (défaut : root@$VM de $BASCULE_ENV)
#   AGENTS_SSH    cible SSH de la VM d'agents (obligatoire pour deplacer)
#   PERSONAS      personas déplacées (défaut : deepseek5 deepseek7)
#   CPU_QUOTA     plafond de la slice des agents du serveur (défaut : 250%)
#   HOTE_AGENTS   nom canon de la VM d'agents (défaut : ameesh-agents-1)
#   HOTE_MESH     nom canon du serveur (défaut : manaty-mesh-1)
#   ATTENTE_MAX   attente maximale de la fin d'un tour, en s (défaut : 7200)
#   FORCER_DOSSIER=1  déplacer même si le dossier de travail a des fichiers
#                 modifiés ou des commits non poussés (ils restent sur
#                 l'ancien hôte, jamais effacés ; l'agent repart sans eux)
#   AMEESH        CLI ameesh du poste, pour lire registre et lots
# Aucun secret n'est lu ni affiché.
set -euo pipefail

BASCULE_ENV=${BASCULE_ENV:-$HOME/.config/ameesh/bascule.env}
if [ -z "${MESH_SSH:-}" ] && [ -r "$BASCULE_ENV" ]; then
  # shellcheck disable=SC1090
  . "$BASCULE_ENV"
  [ -n "${VM:-}" ] && MESH_SSH="root@$VM"
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

SLICE=ameesh-agents.slice
COMPLEMENT=/etc/systemd/system/$SLICE.d/20-cpu.conf
DOSSIER_MESH='/home/ameesh/nexlink-%s'        # policy.work_roots du serveur
DOSSIER_AGENTS='/home/ameesh/work/nexlink-%s' # policy.work_roots de la VM d'agents
# dépôt nexlink de la VM d'agents : chaque agent y a son worktree, comme
# deepseek2 à deepseek4
DEPOT_AGENTS=/home/ameesh/work/nexlink

etape() { printf '\n== %s\n' "$*"; }
ok() { printf 'ok         %s\n' "$*"; }
attention() { printf 'attention  %s\n' "$*"; ATTENTION=1; }
ATTENTION=0

# Script root sur une VM (stdin), en root ou par sudo -n.
root_sur() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$1" \
    'if [ "$(id -u)" = 0 ]; then bash -s; else sudo -n bash -s; fi'
}
mesh() { root_sur "$MESH_SSH"; }
agents() {
  [ -n "$AGENTS_SSH" ] || { echo "AGENTS_SSH manquant" >&2; exit 2; }
  root_sur "$AGENTS_SSH"
}
# Préambule des scripts de la VM d'agents : `utilisateur …` lance une commande
# sous l'utilisateur ameesh, avec son systemd utilisateur.
UTILISATEUR='u=$(id -u ameesh)
utilisateur() { runuser -u ameesh -- env HOME=/home/ameesh XDG_RUNTIME_DIR=/run/user/$u \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$u/bus "$@"; }'

champ() {  # $1 persona, $2 champ du registre
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
etat_dossier() {
  "$1" <<EOF
d='$2'
[ -e "\$d/.git" ] || { echo absent; exit 0; }
g() { runuser -u ameesh -- env HOME=/home/ameesh git -C "\$d" "\$@"; }
sale=\$(g status --porcelain --untracked-files=normal | wc -l)
local_seul=\$(g log --oneline HEAD \$(g for-each-ref --format='%(refname)' 'refs/heads/agent/$3-*' 'refs/heads/agent/$3/*') --not --remotes | wc -l)
if [ "\$sale" = 0 ] && [ "\$local_seul" = 0 ]; then echo propre
else echo "\$sale fichier(s) modifié(s) ou non suivi(s), \$local_seul commit(s) non poussé(s) (branche \$(g rev-parse --abbrev-ref HEAD))"
fi
EOF
}

# --- verifier -----------------------------------------------------------------

verifier() {
  etape "serveur ($MESH_SSH)"
  mesh <<EOF
echo "hôte : \$(hostname) ; charge : \$(cut -d' ' -f1-3 /proc/loadavg) ; \$(nproc) vCPU ; mémoire disponible : \$(awk '/MemAvailable/ {printf "%d Mo", \$2/1024}' /proc/meminfo)"
echo "$SLICE : \$(systemctl show $SLICE -p CPUQuotaPerSecUSec -p MemoryMax --value | paste -sd' ') (CPUQuotaPerSecUSec, MemoryMax)"
if [ -f $COMPLEMENT ]; then echo "complément : \$(grep -h '^CPUQuota' $COMPLEMENT)"; else echo "complément : absent"; fi
for u in \$(systemctl list-units 'ameesh-runner@*' --all --plain --no-legend | awk '{print \$1}'); do
  echo "\$u : \$(systemctl is-active \$u), \$(systemctl is-enabled \$u 2>/dev/null), slice \$(systemctl show \$u -p Slice --value)"
done
EOF
  if [ -n "$AGENTS_SSH" ]; then
    etape "VM d'agents ($AGENTS_SSH)"
    agents <<EOF
$UTILISATEUR
echo "hôte : \$(hostname) ; charge : \$(cut -d' ' -f1-3 /proc/loadavg) ; \$(nproc) vCPU ; mémoire disponible : \$(awk '/MemAvailable/ {printf "%d Mo", \$2/1024}' /proc/meminfo)"
utilisateur systemctl --user list-units 'ameesh-runner-agent@*' --plain --no-legend | awk '{print \$1" : "\$3}'
EOF
  fi
  for p in $PERSONAS; do
    local hote statut lots dm da
    etape "$p"
    hote=$(champ "$p" host); statut=$(champ "$p" status); lots=$(lots_en_build "$p")
    echo "registre : hôte $hote, statut $statut, bail $(champ "$p" lease_owner)"
    if [ -n "$lots" ]; then attention "$p a des lots en build : $lots (déménager entre deux lots)"
    else ok "$p n'a aucun lot en build"; fi
    # shellcheck disable=SC2059
    dm=$(etat_dossier mesh "$(printf "$DOSSIER_MESH" "$p")" "$p")
    case "$dm" in propre|absent) ok "dossier du serveur : $dm" ;; *) attention "dossier du serveur : $dm" ;; esac
    if [ -n "$AGENTS_SSH" ]; then
      # shellcheck disable=SC2059
      da=$(etat_dossier agents "$(printf "$DOSSIER_AGENTS" "$p")" "$p")
      case "$da" in propre|absent) ok "dossier de la VM d'agents : $da" ;; *) attention "dossier de la VM d'agents : $da" ;; esac
    fi
    case "$hote" in
      "$HOTE_AGENTS") ok "placé sur $HOTE_AGENTS (canon synchronisé)" ;;
      "$HOTE_MESH") echo "placé sur $HOTE_MESH : la PR du canon n'est pas encore fusionnée et synchronisée" ;;
      *) attention "placé sur « $hote »" ;;
    esac
  done
  echo
  [ "$ATTENTION" = 0 ] && echo "aucune attention" || echo "des points d'attention (ci-dessus)"
}

# --- plafond CPU ----------------------------------------------------------------

appliquer_cpu() {
  etape "plafond CPU de $SLICE sur le serveur : $CPU_QUOTA"
  mesh <<EOF
set -euo pipefail
systemctl cat $SLICE >/dev/null 2>&1 || { echo "$SLICE absente : l'installer d'abord (docs/EXPLOITATION.md, « Plafonds mémoire des agents »)" >&2; exit 1; }
voulu='# alleger-serveur.sh (bureau, 2026-10-12) : la base du mesh garde du CPU
[Slice]
CPUQuota=$CPU_QUOTA'
if [ "\$(cat $COMPLEMENT 2>/dev/null)" = "\$voulu" ]; then
  echo "complément déjà en place"
else
  install -d -m 0755 "\$(dirname $COMPLEMENT)"
  printf '%s\n' "\$voulu" > $COMPLEMENT.tmp && chmod 0644 $COMPLEMENT.tmp && mv $COMPLEMENT.tmp $COMPLEMENT
  systemctl daemon-reload
  echo "complément écrit, systemd rechargé"
fi
# appliqué tout de suite au cgroup, sans redémarrer d'unité
systemctl set-property --runtime $SLICE CPUQuota=$CPU_QUOTA
echo "$SLICE : CPUQuotaPerSecUSec=\$(systemctl show $SLICE -p CPUQuotaPerSecUSec --value)"
hors=\$(for u in \$(systemctl list-units 'ameesh-runner@*' --state=active --plain --no-legend | awk '{print \$1}'); do
  [ "\$(systemctl show \$u -p Slice --value)" = $SLICE ] || echo \$u; done)
[ -z "\$hors" ] || echo "attention : hors de $SLICE, donc hors plafond : \$hors (complément 10-plafonds.conf, puis redémarrage hors tour : vm.sh redemarrer)"
EOF
}

retour_cpu() {
  etape "plafond CPU de $SLICE levé sur le serveur"
  mesh <<EOF
set -euo pipefail
if [ -f $COMPLEMENT ]; then rm -f $COMPLEMENT; systemctl daemon-reload; echo "complément retiré"; else echo "complément déjà absent"; fi
systemctl set-property --runtime $SLICE CPUQuota=
echo "$SLICE : CPUQuotaPerSecUSec=\$(systemctl show $SLICE -p CPUQuotaPerSecUSec --value)"
EOF
}

# --- déménagement ---------------------------------------------------------------

# Attend que la persona soit hors tour ; ses lots en build sont signalés.
attendre_hors_tour() {  # $1 persona
  local debut=$SECONDS st lots
  while st=$(champ "$1" status); [ "$st" = running ]; do
    [ $((SECONDS - debut)) -lt "$ATTENTE_MAX" ] \
      || { echo "$1 toujours en tour après ${ATTENTE_MAX}s : relancer plus tard" >&2; return 1; }
    echo "$1 en tour, attente…"; sleep 30
  done
  lots=$(lots_en_build "$1")
  [ -z "$lots" ] || echo "attention : $1 a des lots en build ($lots) : il les reprendra sur son nouvel hôte, depuis leur branche poussée"
}

exiger_dossier_propre() {  # $1 mesh|agents, $2 persona, $3 modèle du chemin
  local etat
  # shellcheck disable=SC2059
  etat=$(etat_dossier "$1" "$(printf "$3" "$2")" "$2")
  case "$etat" in
    propre|absent) return 0 ;;
    *) if [ "${FORCER_DOSSIER:-0}" = 1 ]; then
         echo "attention : $2 : dossier de travail pas en sécurité chez l'origine ($etat) ; déplacé quand même (FORCER_DOSSIER=1), le dossier reste sur l'ancien hôte"
         return 0
       fi
       echo "$2 : son dossier de travail n'est pas en sécurité chez l'origine ($etat) ; rien n'est déplacé. L'agent pousse son travail (ou le propriétaire), puis relancer ; ou FORCER_DOSSIER=1." >&2
       return 1 ;;
  esac
}

deplacer() {  # $1 persona
  local p=$1 hote dossier
  etape "$p : $HOTE_MESH → $HOTE_AGENTS"
  hote=$(champ "$p" host)
  if [ "$hote" != "$HOTE_AGENTS" ]; then
    echo "$p est placé sur « $hote » : fusionner la PR du canon, puis « ameesh canon sync » (ou attendre la passe d'un exécuteur), et relancer" >&2
    return 1
  fi
  attendre_hors_tour "$p"
  exiger_dossier_propre mesh "$p" "$DOSSIER_MESH"
  # Hors tour, le placement synchronisé : l'arrêt drainé est immédiat ou attend
  # la fin du tour (TimeoutStopSec 35 min), jamais un tour tué.
  mesh <<EOF
set -euo pipefail
u=ameesh-runner@$p
if systemctl is-active --quiet \$u || systemctl is-enabled --quiet \$u 2>/dev/null; then
  systemctl disable --now \$u; echo "\$u arrêté et désactivé"
else echo "\$u déjà arrêté et désactivé"; fi
EOF
  # shellcheck disable=SC2059
  dossier=$(printf "$DOSSIER_AGENTS" "$p")
  agents <<EOF
set -euo pipefail
$UTILISATEUR
if [ -e '$dossier/.git' ]; then echo "dossier de travail déjà présent : $dossier"
else
  utilisateur git -C '$DEPOT_AGENTS' fetch -q origin
  utilisateur git -C '$DEPOT_AGENTS' worktree add -q --detach '$dossier' origin/main
  echo "worktree créé : $dossier (détaché sur origin/main ; l'agent prend la branche de son lot)"
fi
u=ameesh-runner-agent@$p
utilisateur systemctl --user enable --now \$u
sleep 10
utilisateur systemctl --user is-active --quiet \$u || { echo "\$u ne démarre pas : journalctl --user -u \$u -n 50 (sous ameesh)" >&2; exit 1; }
echo "\$u actif"
EOF
  sleep 20
  echo "registre : hôte $(champ "$p" host), statut $(champ "$p" status), bail $(champ "$p" lease_owner)"
}

ramener() {  # $1 persona
  local p=$1 hote
  etape "$p : $HOTE_AGENTS → $HOTE_MESH"
  hote=$(champ "$p" host)
  if [ "$hote" != "$HOTE_MESH" ]; then
    echo "$p est placé sur « $hote » : fusionner le revert de la PR du canon, synchroniser, et relancer" >&2
    return 1
  fi
  attendre_hors_tour "$p"
  exiger_dossier_propre agents "$p" "$DOSSIER_AGENTS"
  agents <<EOF
set -euo pipefail
$UTILISATEUR
u=ameesh-runner-agent@$p
if utilisateur systemctl --user is-active --quiet \$u || utilisateur systemctl --user is-enabled --quiet \$u 2>/dev/null; then
  utilisateur systemctl --user disable --now \$u; echo "\$u arrêté et désactivé (dossier gardé)"
else echo "\$u déjà arrêté et désactivé"; fi
EOF
  mesh <<EOF
set -euo pipefail
u=ameesh-runner@$p
systemctl enable --now \$u; sleep 10
systemctl is-active --quiet \$u || { echo "\$u ne démarre pas : journalctl -u \$u -n 50" >&2; exit 1; }
echo "\$u actif"
EOF
}

case "${1:-}" in
  verifier) verifier ;;
  appliquer|retour)
    quoi=${2:-tout}
    case "$quoi" in cpu|deplacer|tout) ;; *) sed -n '13,15p' "$0" >&2; exit 2 ;; esac
    if [ "$1" = appliquer ]; then
      [ "$quoi" = deplacer ] || appliquer_cpu
      if [ "$quoi" != cpu ]; then for p in $PERSONAS; do deplacer "$p"; done; fi
    else
      if [ "$quoi" != cpu ]; then for p in $PERSONAS; do ramener "$p"; done; fi
      [ "$quoi" = deplacer ] || retour_cpu
    fi ;;
  *) sed -n '4,49p' "$0" >&2; exit 2 ;;
esac
