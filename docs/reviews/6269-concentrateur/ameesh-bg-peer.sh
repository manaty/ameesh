#!/bin/bash
# #6269 — pair bureau-goal (10.77.0.6) sur le concentrateur, échec FERMÉ :
# le filtre est posé d'abord, le pair WireGuard ensuite ; à l'arrêt, le pair part d'abord.
# Usage : ameesh-bg-peer.sh start|stop|status   (lancé par ameesh-bg-peer.service)
set -euo pipefail
IF=wg0; SRC=10.77.0.6/32; DB=10.77.0.1/32
PUB_FILE=/etc/ameesh-bg-peer/bureau-goal.pub      # clé PUBLIQUE de BG, root 0644
IPT="iptables -w 5"
regle() { $IPT -C "$@" 2>/dev/null || $IPT -I "$@"; }   # idempotent : -C, sinon -I (en tête)
case "${1:-}" in
start)
  PUB=$(tr -d '[:space:]' < "$PUB_FILE"); [[ $PUB =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$ ]] || { echo "clé publique invalide" >&2; exit 2; }
  # 1. chaîne dédiée INPUT : 5432 vers .1 seulement, tout le reste rejeté
  $IPT -N BG-IN 2>/dev/null || true
  $IPT -F BG-IN
  $IPT -A BG-IN -d $DB -p tcp --dport 5432 -j ACCEPT
  $IPT -A BG-IN -j DROP
  regle INPUT 1 -i $IF -s $SRC -j BG-IN
  # 2. transit : DNAT Docker (55432 -> 172.17.0.3:5432) passe par FORWARD, pas INPUT
  $IPT -N DOCKER-USER 2>/dev/null || true
  regle DOCKER-USER 1 -i $IF -s $SRC -j DROP
  regle FORWARD 1 -i $IF -s $SRC -j DROP           # ceinture
  # 3. vérification avant le pair : sinon STOP sans pair
  $IPT -C INPUT -i $IF -s $SRC -j BG-IN && $IPT -C DOCKER-USER -i $IF -s $SRC -j DROP && $IPT -C FORWARD -i $IF -s $SRC -j DROP
  # 4. pair WireGuard, seulement maintenant (jamais dans wg0.conf)
  wg set $IF peer "$PUB" allowed-ips 10.77.0.6/32
  wg show $IF allowed-ips | grep -q "10.77.0.6/32"
  ;;
stop)
  PUB=$(tr -d '[:space:]' < "$PUB_FILE")
  wg set $IF peer "$PUB" remove 2>/dev/null || true          # pair d'abord
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
