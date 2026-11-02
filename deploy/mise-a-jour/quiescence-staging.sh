#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Sonde de quiescence de l'executant elu du staging (#5494, plan #5484 L1).
#
#   deploy/mise-a-jour/quiescence-staging.sh
#
# Programme `quiescence_probe` de staging-executor.json. `staging_executor.tick`
# l'appelle avant toute reprise, avec STAGING_PREVIOUS_HOST (hote de
# l'execution interrompue), STAGING_PREVIOUS_RUN (son run) et BASCULE_ENV (les
# acces de deploiement du candidat). Il rend 0 seulement apres avoir prouve :
#
#  (a) sur l'hote precedent : aucun processus dans un groupe
#      `ameesh-staging.service`, quel que soit le compte qui l'execute (hors
#      l'executant appelant et la sonde elle-meme quand l'hote precedent est
#      l'hote courant) ; aucun processus qui nomme l'arbre du run
#      (`ameesh-release-staging-<run>-`) ; aucun verrou `staging.lock` tenu ;
#      aucun `.retour-apres-arret` en attente dans un arbre conserve du run
#      (le retour de cet hote n'est connu que la-bas : marche manuelle) ;
#  (b) sur chaque cible VM de la topologie : aucune session ssh root venue de
#      l'hote precedent, aucune operation de vm.sh en cours (bascule-*.py,
#      paquet ameesh.bundle) ;
#  (c) aucune unite `poste-maj-staging-*` vivante sur l'hote precedent, ni sur
#      le poste s'il est declare (QUIESCENCE_SSH_PC_SMICHEA) et joignable.
#
# La sonde ne termine rien : un arret non prouve rend 1. Tuer une sequence au
# milieu d'une etape laisserait un hote a mi-parcours dont le retour n'est
# ecrit que dans l'arbre de l'hote precedent ; l'arret est le geste de
# l'operateur (`systemctl --user stop ameesh-staging.service`) ou de la
# surveillance de l'executant (#5450). Hote precedent ou cible injoignable,
# topologie illisible, rapport incomplet : 1, sans aucune mutation (la surete
# passe avant la vivacite, arbitrage #5484). Chaque refus est remis au pilote
# et au bureau (NOTIF_QUIESCENCE), une fois par run et par raison.
#
# Topologie : celle de staging.sh (HOTES_STAGING, sinon bascule-a1.env,
# bascule-a2.env, bascule-agents-3.env et SERVEUR_SSH ou VM). Options lues
# dans BASCULE_ENV ou l'environnement :
#   HOTE_LOCAL                  hote courant (sinon AMEESH_HOST, hostname -s)
#   QUIESCENCE_SSH_<HOTE>       cible ssh qui remplace celle de la topologie
#                               (ex. une cle a commande forcee, L2)
#   QUIESCENCE_ADRESSES_<HOTE>  adresses sous lesquelles les cibles voient
#                               l'hote (defaut : celle de sa cible ssh)
#   QUIESCENCE_ABSENTS          hotes que la sequence ne touche jamais (meme
#                               liste que ses `--absent`) : non verifies
#   NOTIF_QUIESCENCE            destinataires (defaut : NOTIF_STAGING bureau)
# Aucun compte n'est nomme : les groupes, verrous et arbres sont cherches pour
# tous les comptes de l'hote (la commande distante tourne en root).
set -euo pipefail

unset AMEESH_DSN AGENT_MESH_DSN 2>/dev/null || true
PRECEDENT=${STAGING_PREVIOUS_HOST:-}
RUN=${STAGING_PREVIOUS_RUN:-}
BASCULE_ENV=${BASCULE_ENV:-$HOME/.config/ameesh/bascule.env}
TRAVAIL=${QUIESCENCE_TRAVAIL:-$HOME/.local/state/ameesh/mise-a-jour}
SSH=${QUIESCENCE_SSH:-ssh}
DELAI=${QUIESCENCE_DELAI:-60}

refus() {  # raison : trace, alerte (une par run et raison), code 1
  local raison=$1
  echo "quiescence non prouvee (run ${RUN:-?}, hote precedent ${PRECEDENT:-?}) : $raison" >&2
  alerter "$raison" || echo "alerte de quiescence non remise" >&2
  exit 1
}

alerter() {
  local marque corps dest
  [[ $RUN =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  mkdir -p "$TRAVAIL" && chmod 700 "$TRAVAIL"
  marque="$TRAVAIL/quiescence-$RUN.alerte"
  [ -r "$marque" ] && [ "$(cat "$marque")" = "$1" ] && return 0
  corps="L'executant elu du staging ne reprend pas la demande : l'arret du run $RUN (hote $PRECEDENT) n'est pas prouve. Raison : $1. Aucune mutation faite. Prouver l'arret a la main (unite, ssh, verrou, retour en attente), puis laisser la minuterie reprendre ; voir docs/DEPLOIEMENT-HOTE-AGENTS.md."
  for dest in ${NOTIF_QUIESCENCE:-${NOTIF_STAGING:-mesh-design} bureau}; do
    if [ -n "${QUIESCENCE_NOTIFIER:-}" ]; then
      "$QUIESCENCE_NOTIFIER" "$dest" "staging : quiescence non prouvee ($PRECEDENT)" "$corps" || return 1
    else
      python3 - "$dest" "staging : quiescence non prouvee ($PRECEDENT)" "$corps" <<'PY' || return 1
import sys
from ameesh import config, db, mail, work
dest, sujet, corps = sys.argv[1:4]
conn = db.connect(config.load())
try:
    mail.send(conn, work.SYSTEM_SENDER, dest, "%s\n\n%s" % (sujet, corps),
              kind="event", payload={"urgent": True, "staging": {"sujet": sujet}})
finally:
    conn.close()
PY
    fi
  done
  printf '%s' "$1" > "$marque"
}

[ -n "$PRECEDENT" ] || refus "STAGING_PREVIOUS_HOST absent"
[[ $RUN =~ ^[A-Za-z0-9_.-]+$ ]] || refus "STAGING_PREVIOUS_RUN absent ou invalide"
[[ $PRECEDENT =~ ^[A-Za-z0-9_.-]+$ ]] || refus "STAGING_PREVIOUS_HOST invalide"
[ -r "$BASCULE_ENV" ] || refus "acces de deploiement illisibles ($BASCULE_ENV)"
# shellcheck disable=SC1090
. "$BASCULE_ENV"

var_de() {  # prefixe hote → nom de variable (pc-smichea → PREFIXE_PC_SMICHEA)
  printf '%s%s\n' "$1" "$(printf '%s' "$2" | tr 'a-z.-' 'A-Z__')"
}

if [ -n "${HOTE_LOCAL:-}" ]; then COURANT=$HOTE_LOCAL
elif [ -n "${AMEESH_HOST:-}" ]; then COURANT=$AMEESH_HOST
else COURANT=$(hostname -s); fi

# --- topologie (meme regle que staging.sh) --------------------------------
declare -A CIBLE=()
ORDRE=()
if [ -z "${HOTES_STAGING:-}" ]; then
  DIR_BASCULE=$(dirname "$BASCULE_ENV")
  compose=()
  for f in "$DIR_BASCULE/bascule-a1.env" "$DIR_BASCULE/bascule-a2.env" "$DIR_BASCULE/bascule-agents-3.env"; do
    [ -r "$f" ] || continue
    entree=$(
      # shellcheck disable=SC1090
      . "$f" >/dev/null 2>&1 || exit 1
      [ -n "${HOTE_AGENTS:-}" ] && [ -n "${AGENTS_SSH:-}" ] || exit 1
      printf '%s=%s' "$HOTE_AGENTS" "$AGENTS_SSH"
    ) || entree=""
    [ -n "$entree" ] && compose+=("$entree")
  done
  SERVEUR_SSH=${SERVEUR_SSH:-${VM:+root@$VM}}
  [ ${#compose[@]} -ge 3 ] && [ -n "$SERVEUR_SSH" ] \
    || refus "topologie illisible (ni HOTES_STAGING ni bascule-*.env et SERVEUR_SSH complets)"
  HOTES_STAGING="${compose[*]} manaty-mesh-1=$SERVEUR_SSH"
fi
for entree in $HOTES_STAGING; do
  nom=${entree%%=*} cible=${entree#*=}
  [ "$entree" != "$nom" ] && [ -n "$nom" ] && [ -n "$cible" ] \
    || refus "HOTES_STAGING : entree invalide « $entree »"
  case "$cible" in poste|local) cible="" ;; esac
  surcharge=$(var_de QUIESCENCE_SSH_ "$nom")
  cible=${!surcharge:-$cible}
  if [ -n "$cible" ]; then
    [[ $cible =~ ^[a-zA-Z0-9_.@:-]+$ && $cible != -* ]] \
      || refus "cible ssh invalide pour $nom"
  fi
  CIBLE[$nom]=$cible
  ORDRE+=("$nom")
done
declare -A ABSENT=()
for nom in ${QUIESCENCE_ABSENTS:-}; do ABSENT[$nom]=1; done

if [ "$PRECEDENT" != "$COURANT" ] && [ -z "${CIBLE[$PRECEDENT]:-}" ]; then
  refus "hote precedent $PRECEDENT hors de la topologie et sans QUIESCENCE_SSH : arret non verifiable"
fi
# Adresses sous lesquelles les cibles voient l'hote precedent (sessions ssh).
var=$(var_de QUIESCENCE_ADRESSES_ "$PRECEDENT")
ADRESSES=${!var:-}
if [ -z "$ADRESSES" ]; then
  cible_precedent=${CIBLE[$PRECEDENT]:-}
  [ -n "$cible_precedent" ] \
    || refus "hote precedent $PRECEDENT sans cible ssh ni QUIESCENCE_ADRESSES : arret non verifiable"
  ADRESSES=${cible_precedent#*@}
fi

# --- collecteur : lecture seule, une ligne par fait, « ok » en dernier -------
# Arguments : role (precedent|cible|poste) run sid-permis pid-permis adresses…
# Lignes : etranger, run, verrou, retour, poste, session, operation, illisible.
# shellcheck disable=SC2016
COLLECTEUR='set -u
role=$1 run=$2 sid_permis=$3 pid_permis=$4; shift 4
moi=$$
args_de() { ps -o args= -p "$1" 2>/dev/null | tr "\t\n" "  "; }
procs_de() { find "$1" -name cgroup.procs -exec cat {} + 2>/dev/null; }
if [ "$role" = precedent ]; then
  while IFS= read -r groupe; do
    uid=$(printf "%s" "$groupe" | sed -n "s/.*user-\([0-9]*\)\.slice.*/\1/p")
    for pid in $(procs_de "$groupe"); do
      [ "$pid" = "$pid_permis" ] && continue
      sid=$(ps -o sid= -p "$pid" 2>/dev/null | tr -d " ")
      [ -n "$sid" ] || continue
      [ "$sid" = "$sid_permis" ] && continue
      printf "etranger\t%s\t%s\t%s\n" "${uid:-systeme}" "$pid" "$(args_de "$pid")"
    done
  done < <(find /sys/fs/cgroup -type d -name ameesh-staging.service 2>/dev/null)
  ps -eo pid=,sid=,args= 2>/dev/null | while read -r pid sid args; do
    [ "$pid" = "$moi" ] || [ "$sid" = "$sid_permis" ] && continue
    case "$args" in *"ameesh-release-staging-$run-"*) printf "run\t%s\t%s\n" "$pid" "$args" ;; esac
  done
  for verrou in /root/.local/state/ameesh/mise-a-jour/staging.lock /home/*/.local/state/ameesh/mise-a-jour/staging.lock; do
    [ -e "$verrou" ] || continue
    flock -n -s "$verrou" true 2>/dev/null; rc=$?
    [ "$rc" = 0 ] && continue
    [ "$rc" = 1 ] && printf "verrou\t%s\n" "$verrou" || printf "verrou\t%s (illisible)\n" "$verrou"
  done
  # Arbre conserve : a cote du clone dedie (au plus trois niveaux sous le
  # dossier personnel, ex. ~/staging/ameesh) ou dans le dossier temporaire.
  find /root /home /tmp /var/tmp -maxdepth 4 -type d -name "ameesh-release-staging-$run-*" 2>/dev/null \
    | while IFS= read -r d; do
        [ -e "$d/worktree/.retour-apres-arret" ] && printf "retour\t%s\n" "$d/worktree/.retour-apres-arret"
      done
fi
if [ "$role" = precedent ] || [ "$role" = poste ]; then
  while IFS= read -r groupe; do
    [ -n "$(procs_de "$groupe")" ] && printf "poste\t%s\n" "${groupe##*/}"
  done < <(find /sys/fs/cgroup -type d -name "poste-maj-staging-*.service" 2>/dev/null)
fi
if [ "$#" -gt 0 ]; then
  declare -A connue=()
  for a in "$@"; do
    connue[$a]=1
    for ip in $(getent ahosts "$a" 2>/dev/null | cut -d" " -f1 | sort -u); do connue[$ip]=1; done
  done
  # La session de ce collecteur (SSH_CONNECTION) est exclue.
  read -r moi_ip moi_port _ <<< "${SSH_CONNECTION:-- -}"
  if ! command -v ss >/dev/null 2>&1; then
    printf "illisible\tss absent\n"
  else
    ss -Htnp state established "( sport = :22 )" 2>/dev/null | while read -r _ _ _ pair proc; do
      port=${pair##*:} ip=${pair%:*}; ip=${ip#[}; ip=${ip%]}; ip=${ip#::ffff:}
      [ -n "${connue[$ip]:-}" ] || continue
      [ "$ip" = "$moi_ip" ] && [ "$port" = "$moi_port" ] && continue
      qui="?"
      for pid in $(printf "%s" "$proc" | grep -o "pid=[0-9]*" | cut -d= -f2); do
        u=$(args_de "$pid" | sed -n "s/^sshd[^:]*: \([^@ ]*\)@.*/\1/p; s/^sshd[^:]*: \([^ ]*\) \[priv\].*/\1/p" | head -n1)
        [ -n "$u" ] && { qui=$u; [ "$u" = root ] && break; }
      done
      [ "$qui" = "?" ] || [ "$qui" = root ] && printf "session\t%s\t%s\n" "$ip" "$qui"
    done
  fi
  # Motifs composes : le texte de ce collecteur ne doit pas s y reconnaitre.
  b=bascule- p=ameesh.bundle.
  ps -eo pid=,sid=,args= 2>/dev/null | while read -r pid sid args; do
    [ "$pid" = "$moi" ] || [ "$sid" = "$sid_permis" ] && continue
    case "$args" in
      *"/.cache/ameesh/$b"*.py*|*"/.local/state/ameesh/mise-a-jour/$b"*.py*|*"/tmp/$p"*)
        printf "operation\t%s\t%s\n" "$pid" "$args" ;;
    esac
  done
fi
echo ok'

collecter() {  # nom role [adresses…] → rapport ; 1 si injoignable ou incomplet
  local nom=$1 role=$2 sortie sid=- pid=0
  shift 2
  if [ "$nom" = "$COURANT" ]; then
    # Sur l'hote courant, l'executant appelant (parent) et la session de la
    # sonde sont permis dans le groupe de l'unite (meme espace de pid).
    sid=$(ps -o sid= -p $$ | tr -d ' ') pid=$PPID
  fi
  if [ -n "${CIBLE[$nom]:-}" ]; then
    # En root, meme sur l'hote courant : un compte non root ne voit ni les
    # groupes, ni les arbres, ni les sessions des autres comptes.
    sortie=$(timeout "$DELAI" "$SSH" -o BatchMode=yes -o ConnectTimeout=10 "${CIBLE[$nom]}" \
      "bash -s -- $(printf '%q ' "$role" "$RUN" "$sid" "$pid" "$@")" <<< "$COLLECTEUR") || return 1
  elif [ "$nom" = "$COURANT" ]; then
    sortie=$(timeout "$DELAI" "${QUIESCENCE_BASH:-bash}" -c "$COLLECTEUR" collecteur \
      "$role" "$RUN" "$sid" "$pid" "$@") || return 1
  else
    return 1
  fi
  [ "$(printf '%s\n' "$sortie" | tail -n1)" = ok ] || return 1
  printf '%s\n' "$sortie" | sed '$d'
}

# --- preuve -----------------------------------------------------------------
faits=()
# (a), (c), et (b) : l'hote precedent est aussi une cible de la sequence
# (vm.sh s'y deploie par ssh root vers sa propre adresse).
# shellcheck disable=SC2086
rapport=$(collecter "$PRECEDENT" precedent $ADRESSES) \
  || refus "hote precedent $PRECEDENT injoignable ou rapport incomplet"
[ -n "$rapport" ] && faits+=("$PRECEDENT: ${rapport//$'\n'/; }")
# (b) les autres cibles ; (c) le poste s'il est declare.
for nom in "${ORDRE[@]}"; do
  [ "$nom" = "$PRECEDENT" ] && continue
  [ -n "${ABSENT[$nom]:-}" ] && { echo "$nom : declare absent, non verifie" >&2; continue; }
  if [ -z "${CIBLE[$nom]}" ]; then
    echo "$nom : poste sans cible ssh, non verifie (aucune unite d'un run lance depuis une VM)" >&2
    continue
  fi
  # shellcheck disable=SC2086
  if [ "$nom" = pc-smichea ]; then
    rapport=$(collecter "$nom" poste) \
      || { echo "$nom : poste injoignable, non verifie" >&2; continue; }
  else
    rapport=$(collecter "$nom" cible $ADRESSES) \
      || refus "cible $nom injoignable ou rapport incomplet"
  fi
  [ -n "$rapport" ] && faits+=("$nom: ${rapport//$'\n'/; }")
done
if [ ${#faits[@]} -gt 0 ]; then
  refus "$(printf '%s | ' "${faits[@]}" | tr '\t' ' ' | sed 's/ | $//')"
fi
rm -f "$TRAVAIL/quiescence-$RUN.alerte"
echo "quiescence prouvee : run $RUN arrete sur $PRECEDENT, aucune operation sur ${#ORDRE[@]} hote(s) de la topologie" >&2
