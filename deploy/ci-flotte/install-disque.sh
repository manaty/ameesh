#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# #5630 — bureau : poser d'abord /etc/ameesh-ci-disque.env depuis l'exemple.
# sudo bash deploy/ci-flotte/install-disque.sh --install|--check|--remove
# CI_FLOTTE_TEST_ROOT : installation isolée, sans systemctl ni root.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
R=${CI_FLOTTE_TEST_ROOT:-}
TOOL=/usr/local/lib/ameesh-ci/ci-disque.py
UNITS=/etc/systemd/system
TIMER=ameesh-ci-disque.timer
SERVICE=ameesh-ci-disque.service
case "${1:-}" in
  --install|--check|--remove) MODE=${1#--} ;;
  *) echo 'usage: install-disque.sh --install|--check|--remove' >&2; exit 2 ;;
esac
[ $# = 1 ] || exit 2
check() {
  local ko=0 pair src dest
  for pair in "../../scripts/ci-disque.py:$TOOL" "$SERVICE:$UNITS/$SERVICE" "$TIMER:$UNITS/$TIMER"; do
    src=$HERE/${pair%%:*}; dest=$R${pair#*:}
    if ! cmp -s "$src" "$dest"; then echo "KO missing or differs: $dest"; ko=1
    else echo "OK $dest"; fi
  done
  [ -d "$R/var/lib/ameesh-ci-disk" ] || { echo 'KO native disk spool absent'; ko=1; }
  if [ -z "$R" ]; then
    [ "$(stat -c %U /var/lib/ameesh-ci-disk)" = runner ] || { echo 'KO native disk spool owner'; ko=1; }
    [ -f /etc/ameesh-ci-disque.env ] || { echo 'KO /etc/ameesh-ci-disque.env absent'; ko=1; }
    systemctl is-active --quiet "$TIMER" || { echo "KO $TIMER inactive"; ko=1; }
  fi
  return "$ko"
}
case "$MODE" in
  install)
    if [ -z "$R" ] && [ ! -f /etc/ameesh-ci-disque.env ]; then
      echo 'poser /etc/ameesh-ci-disque.env depuis le fichier exemple avant installation' >&2
      exit 1
    fi
    if [ -z "$R" ]; then
      install -d -o runner -g runner -m 0750 /var/lib/ameesh-ci-disk
    else
      install -d -m 0750 "$R/var/lib/ameesh-ci-disk"
    fi
    install -D -m 0755 "$HERE/../../scripts/ci-disque.py" "$R$TOOL"
    for unit in "$SERVICE" "$TIMER"; do
      install -D -m 0644 "$HERE/$unit" "$R$UNITS/$unit"
    done
    if [ -z "$R" ]; then
      systemctl daemon-reload
      systemctl enable --now "$TIMER"
    fi
    check
    ;;
  check) check ;;
  remove)
    if [ -z "$R" ]; then systemctl disable --now "$TIMER"; systemctl stop "$SERVICE"; fi
    rm -f "$R$TOOL" "$R$UNITS/$SERVICE" "$R$UNITS/$TIMER"
    if [ -z "$R" ]; then systemctl daemon-reload; fi
    ;;
esac
