#!/bin/bash
# #6269 v6 — pair bureau-goal (10.77.0.6) sur le concentrateur, échec FERMÉ :
# start : pair éventuel retiré et absence prouvée -> filtre posé et vérifié -> pair ajouté.
# stop  : pair retiré et absence prouvée -> seulement alors filtres retirés ; sinon filtres gardés, rc 1.
# Usage : ameesh-bg-peer.sh start|stop|status   (lancé par ameesh-bg-peer.service)
set -euo pipefail
IF=wg0; SRC=10.77.0.6/32; DB=10.77.0.1/32
PUB_FILE=/etc/ameesh-bg-peer/bureau-goal.pub      # clé PUBLIQUE de BG, root 0644
IPT="iptables -w 5"
pub() { local p; p=$(tr -d '[:space:]' < "$PUB_FILE"); [[ $p =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$ ]] || { echo "clé publique invalide" >&2; exit 2; }; printf '%s' "$p"; }
pair_present() { wg show $IF peers | grep -qxF "$1" || wg show $IF allowed-ips | grep -q '10\.77\.0\.6/32'; }
retirer_pair() { wg set $IF peer "$1" remove || true; ! pair_present "$1"; }
# regle CHAINE <spécification> : test sans position (-C), insertion en tête (-I CHAINE 1)
regle() { local c=$1; shift; $IPT -C "$c" "$@" 2>/dev/null || $IPT -I "$c" 1 "$@"; }
case "${1:-}" in
start)
  PUB=$(pub)
  retirer_pair "$PUB" || { echo "STOP : pair .6 encore présent, filtre non touché" >&2; exit 1; }
  $IPT -N BG-IN 2>/dev/null || true
  $IPT -F BG-IN
  $IPT -A BG-IN -d $DB -p tcp --dport 5432 -j ACCEPT
  $IPT -A BG-IN -j DROP
  regle INPUT -i $IF -s $SRC -j BG-IN
  $IPT -N DOCKER-USER 2>/dev/null || true
  regle DOCKER-USER -i $IF -s $SRC -j DROP           # DNAT Docker 55432 -> FORWARD
  regle FORWARD -i $IF -s $SRC -j DROP               # ceinture
  $IPT -C INPUT -i $IF -s $SRC -j BG-IN
  $IPT -C DOCKER-USER -i $IF -s $SRC -j DROP
  $IPT -C FORWARD -i $IF -s $SRC -j DROP
  [ "$($IPT -S BG-IN | tail -n +2)" = "$(printf '%s\n%s' "-A BG-IN -d $DB -p tcp -m tcp --dport 5432 -j ACCEPT" "-A BG-IN -j DROP")" ] || { echo "STOP : BG-IN inattendue" >&2; exit 1; }
  wg set $IF peer "$PUB" allowed-ips 10.77.0.6/32
  wg show $IF allowed-ips | grep -qF "$PUB	10.77.0.6/32"
  ;;
stop)
  PUB=$(pub)
  retirer_pair "$PUB" || { echo "pair .6 toujours présent : filtres CONSERVÉS" >&2; exit 1; }
  while $IPT -C INPUT -i $IF -s $SRC -j BG-IN 2>/dev/null; do $IPT -D INPUT -i $IF -s $SRC -j BG-IN; done
  while $IPT -C DOCKER-USER -i $IF -s $SRC -j DROP 2>/dev/null; do $IPT -D DOCKER-USER -i $IF -s $SRC -j DROP; done
  while $IPT -C FORWARD -i $IF -s $SRC -j DROP 2>/dev/null; do $IPT -D FORWARD -i $IF -s $SRC -j DROP; done
  $IPT -F BG-IN 2>/dev/null || true; $IPT -X BG-IN 2>/dev/null || true
  ;;
status)
  wg show $IF allowed-ips | grep "10.77.0.6/32" || echo "pair absent"
  $IPT -L BG-IN -v -n 2>/dev/null || echo "BG-IN absente"
  $IPT -S INPUT | grep -- "-s $SRC" || true; $IPT -S DOCKER-USER | grep -- "-s $SRC" || true; $IPT -S FORWARD | grep -- "-s $SRC" || true
  ;;
*) echo "usage: $0 start|stop|status" >&2; exit 2;;
esac
