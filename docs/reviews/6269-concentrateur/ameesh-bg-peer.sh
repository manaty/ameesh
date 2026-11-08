#!/bin/bash
# #6269 v9 — pair bureau-goal (10.77.0.6) sur le concentrateur, échec FERMÉ :
# start : pair éventuel retiré et absence prouvée -> filtre posé et vérifié -> pair ajouté.
# stop  : pair retiré et absence prouvée par une lecture réussie -> seulement alors filtres retirés ;
#         lecture wg en erreur ou pair présent -> filtres CONSERVÉS, rc 1.
# Usage : ameesh-bg-peer.sh start|stop|status   (lancé par ameesh-bg-peer.service)
set -euo pipefail
IF=wg0; SRC=10.77.0.6/32; DB=10.77.0.1/32
PUB_FILE=/etc/ameesh-bg-peer/bureau-goal.pub      # clé PUBLIQUE de BG, root 0644
IPT="iptables -w 5"
pub() { local p; p=$(tr -d '[:space:]' < "$PUB_FILE"); [[ $p =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$ ]] || { echo "clé publique invalide" >&2; exit 2; }; printf '%s' "$p"; }
# etat_pair PUB : imprime "present" ou "absent" ; rc != 0 si wg0 est illisible (jamais pris pour une absence)
etat_pair() { local d; d=$(wg show $IF allowed-ips) || return 1
  if awk -v p="$1" '$1==p {f=1} {for(i=2;i<=NF;i++) if($i=="10.77.0.6/32") f=1} END{exit !f}' <<<"$d"; then echo present; else echo absent; fi; }
# retirer_pair PUB : rc 0 seulement si l'absence est PROUVÉE par une lecture réussie
retirer_pair() { local e; e=$(etat_pair "$1") || return 1; [ "$e" = absent ] && return 0
  wg set $IF peer "$1" remove || return 1; e=$(etat_pair "$1") || return 1; [ "$e" = absent ]; }
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
  regle FORWARD -i $IF -s $SRC -j DROP               # ceinture, en tête de FORWARD (avant le saut DOCKER-USER)
  $IPT -C INPUT -i $IF -s $SRC -j BG-IN
  $IPT -C DOCKER-USER -i $IF -s $SRC -j DROP
  $IPT -C FORWARD -i $IF -s $SRC -j DROP
  [ "$($IPT -S BG-IN | tail -n +2)" = "$(printf '%s\n%s' "-A BG-IN -d $DB -p tcp -m tcp --dport 5432 -j ACCEPT" "-A BG-IN -j DROP")" ] || { echo "STOP : BG-IN inattendue" >&2; exit 1; }
  wg set $IF peer "$PUB" allowed-ips 10.77.0.6/32
  wg show $IF allowed-ips | awk -v p="$PUB" '$1==p && $2=="10.77.0.6/32" && NF==2 {ok=1} END{exit !ok}' || { echo "STOP : pair non vérifié" >&2; exit 1; }
  ;;
stop)
  PUB=$(pub)
  retirer_pair "$PUB" || { echo "pair .6 présent (toute clé) ou wg0 illisible : filtres CONSERVÉS" >&2; exit 1; }
  # chaque saut : chaîne lue avec succès, puis -C (rc 0 présent, rc 1 absent, autre = STOP) et -D
  for spec in "INPUT -i $IF -s $SRC -j BG-IN" "DOCKER-USER -i $IF -s $SRC -j DROP" "FORWARD -i $IF -s $SRC -j DROP"; do
    c=${spec%% *}; r=${spec#* }
    $IPT -S "$c" >/dev/null || { [ "$c" = DOCKER-USER ] && continue; echo "STOP : chaîne $c illisible" >&2; exit 1; }
    while :; do
      rc=0; $IPT -C "$c" $r 2>/dev/null || rc=$?
      case $rc in 0) $IPT -D "$c" $r ;; 1) break ;; *) echo "STOP : -C $c rc=$rc" >&2; exit 1 ;; esac
    done
  done
  if $IPT -S BG-IN >/dev/null 2>&1; then
    sauts=$($IPT -S) || { echo "STOP : lecture iptables -S" >&2; exit 1; }
    ! grep -qE -- '-j BG-IN( |$)' <<<"$sauts" || { echo "STOP : un saut vers BG-IN subsiste" >&2; exit 1; }
    $IPT -F BG-IN; $IPT -X BG-IN
  fi
  ;;
status)
  etat_pair "$(pub)" || echo "wg0 illisible"
  $IPT -L BG-IN -v -n 2>/dev/null || echo "BG-IN absente"
  $IPT -S INPUT | grep -- "-s $SRC" || true; $IPT -S DOCKER-USER | grep -- "-s $SRC" || true; $IPT -S FORWARD | grep -- "-s $SRC" || true
  ;;
*) echo "usage: $0 start|stop|status" >&2; exit 2;;
esac
