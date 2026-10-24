#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Lot #3312: the runners of a CI VM of the fleet (ameesh-ci-N, Ubuntu 24.04,
# `actions.runner.*` units) are never stopped in the middle of a job by the
# host's own maintenance.
#
# Cause (24/10/2026, run 40068028315; same on 21/10, run 39554271532):
# apt-daily-upgrade.timer (06:00 + up to 60 min, a different time per VM)
# runs unattended-upgrade; needrestart, in automatic mode, then restarts every
# service that maps an upgraded library — the eight runners included, jobs
# cancelled ("The runner has received a shutdown signal").
#
# What this installs (and nothing else; the runner units are not modified):
#   /etc/needrestart/conf.d/ameesh-ci-runners.conf  needrestart defers the
#       runners (like docker, dbus) instead of restarting them
#   /usr/local/sbin/ameesh-ci-runner-drain          restarts an outdated
#       runner only while it runs no job; `--all --stop --wait S` drains the
#       host before a planned stop
#   /etc/systemd/system/ameesh-ci-runner-drain.{service,timer}
#                                                   the pass, every 5 min
#
#   sudo deploy/ci-flotte/install.sh --dry-run --install
#   sudo deploy/ci-flotte/install.sh --install
#   sudo deploy/ci-flotte/install.sh --check
#   sudo deploy/ci-flotte/install.sh --remove
#
# Planned stop of a CI VM (reboot, resize, image): drain first, never cut:
#   sudo ameesh-ci-runner-drain --all --stop --wait 3600 && sudo systemctl reboot
#
# Test seam (dry runs and --check only): CI_FLOTTE_TEST_ROOT prefixes every
# path written or read.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
R=${CI_FLOTTE_TEST_ROOT:-}
CONF=/etc/needrestart/conf.d/ameesh-ci-runners.conf
TOOL=/usr/local/sbin/ameesh-ci-runner-drain
UNITS=/etc/systemd/system
SERVICE=ameesh-ci-runner-drain.service
TIMER=ameesh-ci-runner-drain.timer
DRY=0
MODE=""

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --install|--check|--remove)
      [ -z "$MODE" ] || { echo "one mode only" >&2; exit 2; }
      MODE=${1#--} ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 2 ;;
  esac
  shift
done
[ -n "$MODE" ] || { echo "a mode is required: --install, --check or --remove" >&2; exit 2; }

run() {
  if [ "$DRY" = 1 ]; then echo "+ $*"; else "$@"; fi
}

place() {  # place SRC DEST MODE
  if [ -n "$R" ] || [ "$DRY" = 1 ]; then
    echo "+ install -D -m $3 $1 $R$2"
    [ "$DRY" = 1 ] || install -D -m "$3" "$1" "$R$2"
  else
    install -D -m "$3" "$1" "$2"
  fi
}

check() {
  local ko=0
  for pair in "needrestart-ameesh-ci.conf:$CONF" "ci-runner-drain.py:$TOOL" \
      "$SERVICE:$UNITS/$SERVICE" "$TIMER:$UNITS/$TIMER"; do
    local src=$HERE/${pair%%:*} dest=$R${pair#*:}
    if [ ! -f "$dest" ]; then echo "KO missing: $dest"; ko=1
    elif ! cmp -s "$src" "$dest"; then echo "KO differs from the checkout: $dest"; ko=1
    else echo "OK $dest"; fi
  done
  if [ -z "$R" ]; then
    if systemctl is-active --quiet "$TIMER"; then echo "OK $TIMER active"
    else echo "KO $TIMER not active"; ko=1; fi
    # the proof that counts: needrestart, in its own words, defers the runners
    if command -v needrestart >/dev/null && \
        perl -e 'our %nrconf; do $ARGV[0]; my @d = grep { "actions.runner.x.service" =~ /$_/ && !$nrconf{override_rc}{$_} } keys %{$nrconf{override_rc}}; exit(@d ? 0 : 1)' \
        /etc/needrestart/needrestart.conf 2>/dev/null; then
      echo "OK needrestart defers actions.runner.*"
    else
      echo "KO needrestart does not defer actions.runner.*"; ko=1
    fi
  fi
  return $ko
}

case "$MODE" in
  install)
    place "$HERE/needrestart-ameesh-ci.conf" "$CONF" 0644
    place "$HERE/ci-runner-drain.py" "$TOOL" 0755
    place "$HERE/$SERVICE" "$UNITS/$SERVICE" 0644
    place "$HERE/$TIMER" "$UNITS/$TIMER" 0644
    if [ -z "$R" ]; then
      run systemctl daemon-reload
      run systemctl enable --now "$TIMER"
    fi
    [ "$DRY" = 1 ] || check
    ;;
  check)
    check
    ;;
  remove)
    if [ -z "$R" ]; then
      run systemctl disable --now "$TIMER"
    fi
    run rm -f "$R$CONF" "$R$TOOL" "$R$UNITS/$SERVICE" "$R$UNITS/$TIMER"
    if [ -z "$R" ]; then run systemctl daemon-reload; fi
    ;;
esac
