#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# #5655: native ameesh-ci VM runners (NOT ameesh-guest-ci@).
# sudo bash deploy/ci-flotte/install-action-cache.sh --install|--check|--remove
# CI_FLOTTE_TEST_ROOT: isolated filesystem, no systemctl call or root needed.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
R=${CI_FLOTTE_TEST_ROOT:-}
CACHE=/usr/local/lib/ameesh-ci/action-archives
HELPER=$HERE/../guest/action-archive-cache.py
WORKFLOWS=$HERE/../../.github/workflows
case "${1:-}" in
  --install|--check|--remove) MODE=${1#--} ;;
  *) echo 'usage: install-action-cache.sh --install|--check|--remove' >&2; exit 2 ;;
esac
[ $# = 1 ] || exit 2
if [ -z "$R" ] && [ "$(id -u)" != 0 ]; then echo 'root required' >&2; exit 1; fi
shopt -s nullglob
units=("$R"/etc/systemd/system/actions.runner.manaty-ameesh.*.service)
[ ${#units[@]} -gt 0 ] || { echo 'no native manaty-ameesh runner units found' >&2; exit 1; }
content() {
  cat <<EOF
# #5655: root-owned shared action archives for native CI runners.
[Service]
Environment=ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE=$CACHE
EOF
}
case "$MODE" in
  install)
    python3 -I "$HELPER" --workflows "$WORKFLOWS" --cache "$R$CACHE"
    for unit in "${units[@]}"; do
      dir=$unit.d
      [ ! -L "$dir" ] && [ ! -L "$dir/action-cache.conf" ] || { echo 'symlink drop-in refused' >&2; exit 1; }
      install -d -m 0755 "$dir"
      content | install -m 0644 /dev/stdin "$dir/action-cache.conf"
    done
    if [ -z "$R" ]; then systemctl daemon-reload; fi
    echo 'Installed. Drain then restart each native runner between jobs to load the environment.'
    ;;
  check)
    python3 -I "$HELPER" --check --workflows "$WORKFLOWS" --cache "$R$CACHE"
    for unit in "${units[@]}"; do
      [ ! -L "$unit.d" ] && [ ! -L "$unit.d/action-cache.conf" ] || exit 1
      content | cmp -s - "$unit.d/action-cache.conf" || { echo "KO $unit cache drop-in missing or differs"; exit 1; }
      if [ -z "$R" ]; then
        systemctl show "$(basename "$unit")" --property=Environment --value | \
          tr ' ' '\n' | grep -qxF "ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE=$CACHE" || {
            echo "KO $unit environment not loaded"; exit 1;
          }
      fi
      echo "OK $unit cache configured"
    done
    ;;
  remove)
    for unit in "${units[@]}"; do
      [ ! -L "$unit.d" ] || exit 1
      dropin=$unit.d/action-cache.conf
      if [ -e "$dropin" ]; then
        content | cmp -s - "$dropin" || { echo "refusing foreign drop-in: $dropin"; exit 1; }
        unlink "$dropin"
        rmdir --ignore-fail-on-non-empty "$unit.d"
      fi
    done
    # Keep immutable archives: harmless, and an active runner can still be
    # reading one. Removal of the opt-in is sufficient to disable the cache.
    if [ -z "$R" ]; then systemctl daemon-reload; fi
    echo 'Removed native runner cache drop-ins; archives retained for active readers.'
    ;;
esac
