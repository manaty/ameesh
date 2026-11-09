#!/usr/bin/env bash
# #5495 C3 v2.4 — fenêtre de release : fermer, attendre la fin du tick, restaurer.
# Défaut : À BLANC (aucun geste). Sur feu du pilote, et après les deux avis, le bureau lance --apply.
#
# Défaut de séquence #203817 (09/11 09:45Z) : la sonde go attendait un instant « hors tick ».
# v2.4 ferme la fenêtre D'ABORD : état de la minuterie gardé, minuterie arrêtée, attente bornée
# (120 s) que le service soit inactif et qu'aucun job ameesh-release ne reste ; puis la sonde go
# (ou le drain de la relève) ; la minuterie n'est restaurée qu'après l'activation.
#
#   ./fenetre-release.sh etat
#       lecture seule : UnitFileState et ActiveState de ameesh-release.timer sur AG1 et AG2.
#   ./fenetre-release.sh [--apply] fermer
#       enregistre l'état (2 lignes) dans /root/ameesh-operator-C3-fenetre-<hôte>-<TS>/ sur chaque
#       hôte, puis arrête la minuterie et exige service inactif + aucun job. Au premier refus :
#       STOP, l'état gardé permet de restaurer ; rien d'autre n'est touché.
#   ./fenetre-release.sh [--apply] restaurer <TS>
#       relit l'état gardé sur chaque hôte et restaure : active → start + ActiveState=active ;
#       inactive → rien (vérifié) ; état absent/illisible → STOP avant tout geste.
#
# Aucune écriture hors des reçus /root ; aucun secret lu ni affiché. Hôtes : ceux du C3.
set -uo pipefail

declare -A IP=([AG1]=78.232.56.22 [AG2]=78.232.60.1)
HOTES="AG1 AG2"
BORNE_S=${BORNE_S:-120}
RECUS_BASE=${RECUS_BASE:-/root}
POLL_S=${POLL_S:-5}
[ "$POLL_S" -ge 1 ] 2>/dev/null || POLL_S=1

R() { local h=$1; shift; ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@${IP[$h]}" "$@"; }
lire() {  # lire <hôte> <commande> : la sortie, ou ERREUR-LECTURE(rc) ; ne masque jamais un rc
  local h=$1 out rc; shift
  out=$(R "$h" "$@" 2>/dev/null); rc=$?
  if [ $rc -eq 0 ]; then printf '%s' "$out"; else printf 'ERREUR-LECTURE(%s)' "$rc"; fi
}
stop() { echo "STOP : $*" >&2; exit 1; }
geste() {  # geste <hôte> <libellé> <commande>
  echo "GESTE [$1] : $2"
  if [ "$APPLY" = 1 ]; then R "$1" "$3" || stop "geste en échec [$1] : $2"; fi
}

APPLY=0
[ "${1:-}" = --apply ] && { APPLY=1; shift; }
mode=${1:-}; shift || true
[ -n "$mode" ] || stop "mode requis : etat | fermer | restaurer"
TS=$(date -u +%Y%m%dT%H%M%SZ)-$$
echo "fenêtre release #5495 v2.4 mode=$mode apply=$APPLY TS=$TS"

revoir_minuterie() {  # <hôte> : (UnitFileState ActiveState), jamais un faux « inconnu »
  local h=$1 u a
  u=$(lire "$h" "systemctl --user -M ameesh@ show ameesh-release.timer -p UnitFileState --value")
  a=$(lire "$h" "systemctl --user -M ameesh@ show ameesh-release.timer -p ActiveState --value")
  printf '%s %s' "$u" "$a"
}

revoir_service() {  # <hôte> : (ActiveState jobs)
  local h=$1 s j
  s=$(lire "$h" "systemctl --user -M ameesh@ show ameesh-release.service -p ActiveState --value")
  j=$(lire "$h" "systemctl --user -M ameesh@ list-jobs --no-legend")
  case "$j" in
    ERREUR-LECTURE*) printf '%s %s' "$s" "$j" ;;
    *) printf '%s %s' "$s" "$(printf '%s\n' "$j" | grep -c ameesh-release)" ;;
  esac
}

attendre_tick_fini() {  # <hôte> : 0 si service inactif et aucun job, 1 sinon
  local h=$1 i=0 s j
  while [ $i -lt $((BORNE_S / POLL_S)) ]; do
    read -r s j <<<"$(revoir_service "$h")"
    [ "$s" = inactive ] && [ "$j" = 0 ] && return 0
    sleep "$POLL_S"; i=$((i + 1))
  done
  echo "KO  $h : tick de release non terminé après ${BORNE_S}s (ActiveState=$s, jobs=$j)" >&2
  return 1
}

case $mode in
etat)
  for h in $HOTES; do
    read -r u a <<<"$(revoir_minuterie "$h")"
    read -r s j <<<"$(revoir_service "$h")"
    echo "état $h : timer UnitFileState=$u ActiveState=$a ; service=$s ; jobs_release=$j"
  done
  ;;

fermer)
  # Tous les états d'abord (aucun geste), comme la règle v2.3 : un état illisible ne doit pas
  # laisser une minuterie arrêtée par notre faute.
  declare -A RECU
  for h in $HOTES; do
    read -r u a <<<"$(revoir_minuterie "$h")"
    [ "$u" = enabled ] || stop "$h minuterie release UnitFileState=$u (attendu enabled) : fenêtre non fermée"
    [ "$a" = active ] || stop "$h minuterie release ActiveState=$a (attendu active) : fenêtre non fermée"
    RECU[$h]=$RECUS_BASE/ameesh-operator-C3-fenetre-$h-$TS
    R "$h" "test ! -e ${RECU[$h]}" || stop "$h reçus déjà présents : ${RECU[$h]}"
  done
  for h in $HOTES; do
    geste "$h" "reçus ${RECU[$h]} : UnitFileState + ActiveState (aucune mutation)" \
      "install -d -m 700 ${RECU[$h]} && { systemctl --user -M ameesh@ show ameesh-release.timer -p UnitFileState --value; systemctl --user -M ameesh@ show ameesh-release.timer -p ActiveState --value; } > ${RECU[$h]}/release-timer.etat && test \$(wc -l < ${RECU[$h]}/release-timer.etat) = 2"
  done
  for h in $HOTES; do
    geste "$h" "minuterie release arrêtée" "systemctl --user -M ameesh@ stop ameesh-release.timer"
    if [ "$APPLY" = 1 ]; then
      [ "$(lire "$h" "systemctl --user -M ameesh@ show ameesh-release.timer -p ActiveState --value")" = inactive ] \
        || stop "$h minuterie release non arrêtée (état gardé dans ${RECU[$h]}) : lancer restaurer $TS"
      attendre_tick_fini "$h" || stop "$h tick de release non terminé (état gardé dans ${RECU[$h]}) : lancer restaurer $TS"
      echo "OK  $h fenêtre fermée : service inactif, aucun job de release"
    fi
  done
  echo "fin fermer ; TS=$TS ; restaurer : ./fenetre-release.sh --apply restaurer $TS"
  ;;

restaurer)
  TSR=${1:?TS de fermer}
  declare -A RELANCE
  for h in $HOTES; do
    A=$RECUS_BASE/ameesh-operator-C3-fenetre-$h-$TSR
    g=$(lire "$h" "sed -n 1p $A/release-timer.etat; sed -n 2p $A/release-timer.etat" | paste -sd' ')
    case "$g" in
      "enabled active")   RELANCE[$h]=active ;;
      "enabled inactive") RELANCE[$h]=inactive ;;
      *)
        u=$(lire "$h" "systemctl --user -M ameesh@ show ameesh-release.timer -p UnitFileState --value")
        if [ "$u" = enabled ]; then
          echo "ATTENTION $h : état gardé « $g » inconnu ; UnitFileState=enabled → relance explicite vérifiée" >&2
          RELANCE[$h]=active
        else
          stop "$h état gardé « $g » inconnu et UnitFileState « $u » : minuterie NON restaurée. Diagnostic : cat $A/release-timer.etat ; systemctl --user -M ameesh@ status ameesh-release.timer"
        fi ;;
    esac
  done
  for h in $HOTES; do
    if [ "${RELANCE[$h]}" = active ]; then
      geste "$h" "minuterie release restaurée : start puis ActiveState=active exigé" \
        "systemctl --user -M ameesh@ start ameesh-release.timer && [ \"\$(systemctl --user -M ameesh@ show ameesh-release.timer -p ActiveState --value)\" = active ]"
      if [ "$APPLY" = 1 ]; then
        [ "$(lire "$h" "systemctl --user -M ameesh@ show ameesh-release.timer -p ActiveState --value")" = active ] \
          || stop "$h minuterie release non active après start"
        echo "OK  $h minuterie release active"
      fi
    else
      [ "$(lire "$h" "systemctl --user -M ameesh@ show ameesh-release.timer -p ActiveState --value")" = inactive ] \
        || stop "$h minuterie release non inactive alors que l'état gardé était inactive"
      echo "OK  $h minuterie release laissée inactive (état gardé)"
    fi
  done
  echo "fin restaurer ; TS=$TSR"
  ;;

*) stop "mode inconnu : $mode" ;;
esac
