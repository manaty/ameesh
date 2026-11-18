#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Installs the credential-gate account, policy files and systemd unit for lot
# #7767 (docs/design/pre-act-credentials-7767.md, sections 4.2 and 4.3).
#
# Run as root, from a checkout of ameesh, on the host that performs the acts
# (today bureau-goal, to be confirmed by the owner decision):
#
#   sudo deploy/gate/install.sh --client bureau [--pgpass FILE] [--start]
#   sudo deploy/gate/install.sh --check [--live]
#   sudo deploy/gate/install.sh --uninstall
#   sudo deploy/gate/install.sh --uninstall --purge --force
#
# What it does:
#   * creates the system account ameesh-gate (no login, no sudo, home
#     /var/lib/ameesh-gate) and the client group ameesh-gate-client;
#   * builds a venv for the gate account from this checkout (or --wheel);
#   * installs /etc/ameesh-gate/{config,broker}.json from the examples when
#     they are absent, root:ameesh-gate 0640, and never overwrites them;
#   * creates the private CLOUDSDK_CONFIG/GH_CONFIG_DIR directories 0700;
#   * installs the systemd unit and the design document, then reloads systemd;
#   * --check verifies modes, ownership, JSON and the policy (--live also
#     checks the canon and the database);
#   * --uninstall stops and removes the unit but keeps the account and the
#     credential stores; --purge --force removes them after an explicit
#     escrow warning.
#
# It never creates, copies or prints a cloud/GitHub credential. The owner
# decision names the service account, the keyring, the GitHub identity and the
# escrow destination (design section 7); this script only installs the gate.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/../.." && pwd)
SERVICE_SRC=$HERE/ameesh-gate.service
POLICY_EXAMPLE=$HERE/broker.json.example
CONFIG_EXAMPLE=$HERE/config.json.example
README_SRC=$HERE/README.md
DESIGN_SRC=$SRC/docs/design/pre-act-credentials-7767.md

GATE_USER=ameesh-gate
GATE_GROUP=ameesh-gate
CLIENT_GROUP=ameesh-gate-client
ROOT=/var/lib/ameesh-gate
ETC=/etc/ameesh-gate
UNIT=/etc/systemd/system/ameesh-gate.service
DOC=/usr/share/doc/ameesh/pre-act-credentials-7767.md
DOC_GATE=/usr/share/doc/ameesh/gate-README.md
VENV=$ROOT/venv
PYTHON=${PYTHON:-python3}

MODE=install
DRY=0
START=0
LIVE=0
FORCE=0
PURGE=0
WHEEL=""
PGPASS=""
CLIENTS=()

die() { echo "install.sh: $*" >&2; exit 1; }
say() { echo "== $*"; }
warn() { echo "install.sh: warning: $*" >&2; }

run() {
  if [ "$DRY" = 1 ]; then
    printf '+ '; printf '%q ' "$@"; echo
  else
    "$@"
  fi
}

as_gate() {
  runuser -u "$GATE_USER" -- "$@"
}

# A mode check that also prints what it found, for --check.
check_mode() {
  local path=$1 wanted=$2 owner=$3 group=$4 got
  if [ ! -e "$path" ]; then
    echo "missing: $path"
    return 1
  fi
  got=$(stat -c '%a %U %G' "$path" 2>/dev/null || echo '?')
  if [ "$got" != "$wanted $owner $group" ]; then
    echo "wrong mode/owner: $path ($got, want $wanted $owner $group)"
    return 1
  fi
  return 0
}

usage() {
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --client) CLIENTS+=("$2"); shift 2 ;;
    --pgpass) PGPASS=$2; shift 2 ;;
    --wheel) WHEEL=$2; shift 2 ;;
    --start) START=1; shift ;;
    --check) MODE=check; shift ;;
    --live) LIVE=1; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    --purge) PURGE=1; MODE=uninstall; shift ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

for client in "${CLIENTS[@]+"${CLIENTS[@]}"}"; do
  getent passwd "$client" >/dev/null || die "unknown --client user $client"
  [ "$client" = "$GATE_USER" ] && die "--client must not be the gate account"
done

[ "$(id -u)" = 0 ] || die "root required (this script installs a system account and unit)"
[ -f "$SERVICE_SRC" ] || die "missing $SERVICE_SRC"
[ -f "$POLICY_EXAMPLE" ] || die "missing $POLICY_EXAMPLE"
[ -f "$CONFIG_EXAMPLE" ] || die "missing $CONFIG_EXAMPLE"
[ -f "$DESIGN_SRC" ] || die "missing $DESIGN_SRC"

check_static() {
  local failures=0
  say "static checks"
  id -u "$GATE_USER" >/dev/null 2>&1 || { echo "missing user $GATE_USER"; failures=$((failures + 1)); }
  getent group "$GATE_GROUP" >/dev/null || { echo "missing group $GATE_GROUP"; failures=$((failures + 1)); }
  getent group "$CLIENT_GROUP" >/dev/null || { echo "missing group $CLIENT_GROUP"; failures=$((failures + 1)); }
  check_mode "$ROOT" 700 "$GATE_USER" "$GATE_GROUP" || failures=$((failures + 1))
  check_mode "$ETC" 750 root "$GATE_GROUP" || failures=$((failures + 1))
  check_mode "$ETC/broker.json" 640 root "$GATE_GROUP" || failures=$((failures + 1))
  check_mode "$ETC/config.json" 640 root "$GATE_GROUP" || failures=$((failures + 1))
  check_mode "$ROOT/gcloud" 700 "$GATE_USER" "$GATE_GROUP" || failures=$((failures + 1))
  check_mode "$ROOT/gh" 700 "$GATE_USER" "$GATE_GROUP" || failures=$((failures + 1))
  check_mode "$UNIT" 644 root root || failures=$((failures + 1))
  [ -x "$VENV/bin/ameesh" ] || { echo "missing $VENV/bin/ameesh"; failures=$((failures + 1)); }

  if [ -e "$ROOT/.pgpass" ]; then
    check_mode "$ROOT/.pgpass" 600 "$GATE_USER" "$GATE_GROUP" || failures=$((failures + 1))
  fi

  if [ -f "$ETC/config.json" ] && [ -x "$VENV/bin/python" ]; then
    if ! as_gate "$VENV/bin/python" - "$ETC/config.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
if not isinstance(data, dict):
    raise SystemExit("config: object required")
if "password=" in str(data.get("dsn", "")):
    raise SystemExit("config.dsn: a password belongs in .pgpass, not in the DSN")
PY
    then
      failures=$((failures + 1))
    fi
  fi

  if [ -f "$ETC/broker.json" ] && [ -x "$VENV/bin/python" ]; then
    if ! as_gate "$VENV/bin/python" - "$ETC/broker.json" <<'PY'
import sys

from ameesh.credential_gate import load_policy

policy = load_policy(sys.argv[1])
print("policy: %s, rules: %d, agent: %s" % (policy.path, len(policy.rules), policy.agent))
PY
    then
      failures=$((failures + 1))
    fi
  fi

  local members
  members=$(getent group "$CLIENT_GROUP" | cut -d: -f4)
  say "client group members: ${members:-none}"
  case ",$members," in
    *",$GATE_USER,"*) echo "the gate account must not be a client"; failures=$((failures + 1)) ;;
  esac
  if [ "$failures" -ne 0 ]; then
    echo "static checks: $failures failure(s)"
    return 1
  fi
  echo "static checks: ok"
  return 0
}

check_live() {
  say "live checks (canon and database, as $GATE_USER)"
  as_gate env HOME="$ROOT" AMEESH_CONFIG="$ETC/config.json" \
    AMEESH_CREDENTIAL_GATE="$ETC/broker.json" \
    "$VENV/bin/ameesh" gate broker check
}

if [ "$MODE" = check ]; then
  check_static || exit 1
  if [ "$LIVE" = 1 ]; then
    check_live || exit 1
  fi
  exit 0
fi

if [ "$MODE" = uninstall ]; then
  say "stopping and removing the unit"
  run systemctl disable --now ameesh-gate.service || warn "service was not active"
  run rm -f "$UNIT"
  run systemctl daemon-reload || warn "systemd reload failed"
  run rm -f "$DOC" "$DOC_GATE"
  if [ "$PURGE" = 1 ]; then
    [ "$FORCE" = 1 ] || die "purge removes the credential stores: escrow them offline, then rerun with --purge --force"
    [ "$ROOT" = /var/lib/ameesh-gate ] || die "refusing to purge $ROOT"
    [ "$ETC" = /etc/ameesh-gate ] || die "refusing to purge $ETC"
    say "purging $ROOT and $ETC (credentials must already be escrowed)"
    run rm -rf "$ROOT" "$ETC"
    run userdel "$GATE_USER" || warn "could not remove user $GATE_USER"
    run groupdel "$CLIENT_GROUP" || warn "could not remove group $CLIENT_GROUP"
    run groupdel "$GATE_GROUP" || warn "could not remove group $GATE_GROUP"
  else
    say "kept $ROOT and $ETC (credential stores, policy); use --purge --force to remove them"
  fi
  exit 0
fi

# -- install ---------------------------------------------------------------

say "account and groups"
if ! getent group "$GATE_GROUP" >/dev/null; then
  run groupadd --system "$GATE_GROUP"
fi
if ! getent group "$CLIENT_GROUP" >/dev/null; then
  run groupadd --system "$CLIENT_GROUP"
fi
if ! getent passwd "$GATE_USER" >/dev/null; then
  run useradd --system --gid "$GATE_GROUP" --home-dir "$ROOT" \
    --no-create-home --shell /usr/sbin/nologin "$GATE_USER"
fi
for client in "${CLIENTS[@]+"${CLIENTS[@]}"}"; do
  run usermod -aG "$CLIENT_GROUP" "$client"
done

say "directories"
run install -d -m 0750 -o root -g "$GATE_GROUP" "$ETC"
run install -d -m 0700 -o "$GATE_USER" -g "$GATE_GROUP" "$ROOT"
run install -d -m 0700 -o "$GATE_USER" -g "$GATE_GROUP" "$ROOT/gcloud" "$ROOT/gh"

say "gate venv"
if [ -z "$WHEEL" ]; then
  # pip runs the build backend of the source tree as root: the tree must not
  # be writable by the client account (review N2 of #7767).
  [ "$(stat -c '%u' "$SRC")" = 0 ] || \
    die "source tree $SRC is not root-owned; pip would run its build backend as root. Use a root-owned checkout or --wheel FILE"
  writable=$(find "$SRC" -xdev -perm -022 -print -quit 2>/dev/null || true)
  [ -z "$writable" ] || die "source tree $SRC has a group/world-writable entry: $writable; use --wheel FILE"
fi
if [ ! -x "$VENV/bin/pip" ]; then
  run "$PYTHON" -m venv "$VENV"
fi
if [ -n "$WHEEL" ]; then
  [ -f "$WHEEL" ] || die "missing wheel $WHEEL"
  run "$VENV/bin/pip" install --no-index --find-links "$(dirname "$WHEEL")" "$WHEEL"
else
  run "$VENV/bin/pip" install --no-cache-dir --disable-pip-version-check "$SRC"
fi
if [ "$DRY" = 0 ]; then
  chown -R "$GATE_USER:$GATE_GROUP" "$VENV"
  commit=$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo unknown)
  printf '%s\n' "$commit" > "$ROOT/INSTALLED_COMMIT"
  chown "$GATE_USER:$GATE_GROUP" "$ROOT/INSTALLED_COMMIT"
  chmod 0644 "$ROOT/INSTALLED_COMMIT"
fi

say "policy and configuration (existing files are never overwritten)"
run install -m 0640 -o root -g "$GATE_GROUP" "$POLICY_EXAMPLE" "$ETC/broker.json.example"
run install -m 0640 -o root -g "$GATE_GROUP" "$CONFIG_EXAMPLE" "$ETC/config.json.example"
if [ ! -e "$ETC/broker.json" ]; then
  run install -m 0640 -o root -g "$GATE_GROUP" "$POLICY_EXAMPLE" "$ETC/broker.json"
  warn "installed the example policy: review it as code before --start"
fi
if [ ! -e "$ETC/config.json" ]; then
  run install -m 0640 -o root -g "$GATE_GROUP" "$CONFIG_EXAMPLE" "$ETC/config.json"
  warn "installed the example configuration: set the DSN and canon"
fi

if [ -n "$PGPASS" ]; then
  [ -f "$PGPASS" ] || die "missing --pgpass file $PGPASS"
  pgmode=$(stat -c '%a' "$PGPASS")
  if (( (8#$pgmode) & 077 )); then
    die "$PGPASS is readable by group or others; a .pgpass must be 0600"
  fi
  run install -m 0600 -o "$GATE_USER" -g "$GATE_GROUP" "$PGPASS" "$ROOT/.pgpass"
fi
if [ ! -e "$ROOT/.pgpass" ]; then
  warn "no $ROOT/.pgpass: the DSN must authenticate without a password, or install one with --pgpass"
fi

say "systemd unit and documentation"
run install -d -m 0755 /usr/share/doc/ameesh
run install -m 0644 -o root -g root "$SERVICE_SRC" "$UNIT"
run install -m 0644 -o root -g root "$DESIGN_SRC" "$DOC"
if [ -f "$README_SRC" ]; then
  run install -m 0644 -o root -g root "$README_SRC" "$DOC_GATE"
fi
run systemctl daemon-reload

if [ "$DRY" = 1 ]; then
  say "dry run: nothing was changed"
  exit 0
fi

check_static || die "static checks failed; fix the reported paths and rerun --check"

if [ "$START" = 1 ] || [ "$LIVE" = 1 ]; then
  check_live || die "live check failed; the service was not started"
fi

if [ "$START" = 1 ]; then
  say "starting ameesh-gate.service"
  systemctl enable --now ameesh-gate.service
  systemctl --no-pager --lines=0 status ameesh-gate.service || true
else
  say "not started: review $ETC/broker.json, then sudo systemctl enable --now ameesh-gate.service"
fi
