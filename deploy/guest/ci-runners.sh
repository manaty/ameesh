#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# GitHub Actions runners of the ameesh GUEST on a lending host (lot #1256):
# a temporary loan of idle capacity from an existing CI VM of another project
# (for example a Nexlink `scaleway-ci` host), never a runner of the host's own
# application. Read the plan first: deploy/guest/PILOT-ci-nexlink.md.
#
# The host must already carry the L159b/L159g stack (same checkout):
#   deploy/guest/install.sh        user ameesh-guest, rootless Podman, linger
#   deploy/guest/install-pilot.sh  the capped slice, the guard, the gate,
#                                  with --agent-units 'ameesh-guest-*.service'
# This script adds the runners themselves and never touches the application's
# own units (`actions.runner.*`), its working trees (/home/runner), its Docker
# daemon (/run/docker.sock) nor its registrations.
#
#   sudo deploy/guest/ci-runners.sh --dry-run --check
#   sudo deploy/guest/ci-runners.sh --prepare --count 2
#   sudo deploy/guest/ci-runners.sh --register --token-file /root/reg.token
#   sudo deploy/guest/ci-runners.sh --enable
#   sudo deploy/guest/ci-runners.sh --check          # the isolation evidence
#   sudo deploy/guest/ci-runners.sh --disable        # no new job there
#   sudo deploy/guest/ci-runners.sh --remove         # terminal jobs only
#
# Modes (exactly one):
#   --prepare         install the runner software, the units and the `docker`
#                     shim; nothing starts, nothing registers
#   --register        register each runner to the ameesh repository; needs
#                     --token-file F (a short-lived registration token, read
#                     by root only, never written, never printed)
#   --enable [R…]     enable and start the runners (default: every prepared one)
#   --disable [R…]    stop and disable them; refuses while a job is running
#                     there (or --wait-terminal S)
#   --check           what is in place, and the isolation evidence
#   --remove          stop, deregister on the host side and remove everything
#                     this script created (the state file lists it); refuses
#                     while a job runs (or --wait-terminal S). Deleting the
#                     registrations on GitHub is a separate, operator step
#                     printed at the end (see the plan)
#
# Options:
#   --dry-run             print every command and file, change nothing
#   --host NAME           the host's short name (default: hostname -s)
#   --count N             runners, 1 to 4 (default 2, or the count a
#                         previous --prepare recorded in the state file)
#   --tarball FILE        use this actions-runner tarball instead of the
#                         download (offline hosts)
#   --version V           runner version to download (default 2.338.0, the
#                         fleet's), with --sha256
#   --sha256 HEX          expected sha256 of the tarball (default: the pinned
#                         digest of --version)
#   --wait-terminal S     with --disable/--remove: wait at most S seconds for
#                         the running job to end, then refuse
#   --keep-workspaces     with --remove: keep the runner directories (logs)
#
# Environment (test seams, dry runs only: nothing is changed by the tests):
#   CI_RUNNERS_TEST_HOST, CI_RUNNERS_TEST_STATE, CI_RUNNERS_TEST_BUSY
#
# Safety, by construction:
#   * every systemd unit this script writes matches `ameesh-guest-ci@*`; it
#     never stops, starts, enables, disables or removes `actions.runner.*`;
#   * containers run rootless, under the runner's cgroup, inside
#     `ameesh-guest.slice`: no `/run/docker.sock`, no `docker` group, so a job
#     cannot use the application's Docker daemon nor read its images;
#   * the registration token is read from a root-only file and only ever
#     passed to `config.sh`; the dry run prints `<token>` instead.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=/srv/ameesh-guest
GUEST=ameesh-guest
ETC=/etc/ameesh
UNITS=/etc/systemd/system
UNIT=ameesh-guest-ci@.service
SRC_UNIT=$HERE/$UNIT
STATE=$ETC/ci-runners.state
SHIM=$ROOT/bin/docker
REPO_URL=https://github.com/manaty/ameesh
REPO_SLUG=manaty/ameesh
VERSION=2.338.0
# the digest published for actions/runner v2.338.0 linux-x64 (the fleet's)
SHA256=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32
URL_BASE=https://github.com/actions/runner/releases/download
HOST=$(hostname -s)
COUNT=2
COUNT_SET=0
TARBALL=""
WAIT=0
KEEP=0
PURGE=0
DRY=0
MODE=""
TOKEN_FILE=""
TARGETS=()

die() { echo "ci-runners.sh: $*" >&2; exit 1; }
say() { echo "== $*"; }
ko() { echo "KO $*" >&2; BAD=$((BAD + 1)); }
ok() { echo "ok $*"; }
BAD=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host|--count|--tarball|--version|--sha256|--token-file|--wait-terminal)
      [ $# -ge 2 ] || die "$1 needs a value" ;;
  esac
  case "$1" in
    --host) HOST=$2; shift 2 ;;
    --count) COUNT=$2; COUNT_SET=1; shift 2 ;;
    --tarball) TARBALL=$2; shift 2 ;;
    --version) VERSION=$2; shift 2 ;;
    --sha256) SHA256=$2; shift 2 ;;
    --token-file) TOKEN_FILE=$2; shift 2 ;;
    --wait-terminal) WAIT=$2; shift 2 ;;
    --keep-workspaces) KEEP=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --prepare) MODE=prepare; shift ;;
    --register) MODE=register; shift ;;
    --enable) MODE=enable; shift
      while [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; do TARGETS+=("$1"); shift; done ;;
    --disable) MODE=disable; shift
      while [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; do TARGETS+=("$1"); shift; done ;;
    --check) MODE=check; shift ;;
    --remove) MODE=remove; shift ;;
    -h|--help) sed -n '3,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done
[ -n "$MODE" ] || die "one mode: --prepare, --register, --enable, --disable, --check, --remove"
case "$COUNT" in ''|*[!0-9]*) die "invalid --count $COUNT" ;; esac
[ "$COUNT" -ge 1 ] && [ "$COUNT" -le 4 ] || die "--count 1..4, not $COUNT"
case "$WAIT" in ''|*[!0-9]*) die "invalid --wait-terminal $WAIT" ;; esac
case "$HOST" in ''|*[!a-z0-9-]*|-*) die "invalid --host $HOST" ;; esac
case "$VERSION" in ''|*[!A-Za-z0-9._-]*) die "invalid --version $VERSION" ;; esac
case "$SHA256" in ''|*[!0-9a-f]*) die "invalid --sha256 $SHA256" ;; esac
[ ${#SHA256} = 64 ] || die "--sha256: 64 hex digits expected"
[ "$MODE" = register ] || [ -z "$TOKEN_FILE" ] || die "--token-file goes with --register"
[ "$MODE" = disable ] || [ "$MODE" = remove ] || [ "$WAIT" = 0 ] \
  || die "--wait-terminal goes with --disable or --remove"
[ "$KEEP" = 0 ] || [ "$MODE" = remove ] || die "--keep-workspaces goes with --remove"

# The tests' dry runs see a host of their own (CI_RUNNERS_TEST_HOST=DIR): its
# /etc, /srv and /run are read under DIR, and systemd is asked nothing. A real
# run reads only the host. Same seam as install-pilot.sh's PILOT_TEST_HOST.
HOSTDIR=
if [ "$DRY" = 1 ] && [ -n "${CI_RUNNERS_TEST_HOST:-}" ]; then
  HOSTDIR=$CI_RUNNERS_TEST_HOST
  STATE=${CI_RUNNERS_TEST_STATE:-$HOSTDIR/etc/ameesh/ci-runners.state}
fi
at() { if [ -n "$HOSTDIR" ]; then echo "$HOSTDIR$1"; else echo "$1"; fi; }
# The number of runners is the one --prepare recorded: --register, --enable,
# --disable, --check and --remove adopt it when the operator does not repeat
# --count (a --remove that fell back to 2 would leave r3/r4 behind). An
# explicit --count still wins.
if [ "$COUNT_SET" = 0 ] && [ -f "$STATE" ]; then
  recorded_count=$(awk '/^count /{c=$2} END{print c}' "$STATE" 2>/dev/null || true)
  case "$recorded_count" in ''|*[!0-9]*) ;; *) COUNT=$recorded_count ;; esac
fi
host_says() { [ -z "$HOSTDIR" ] && "$@" || true; }
run() {
  if [ "$DRY" = 1 ]; then printf 'DRY %s\n' "$*"; else "$@"; fi
}
safe_run() { # run, but a failure is reported, never fatal (check mode)
  if [ "$DRY" = 1 ]; then printf 'DRY %s\n' "$*"; else "$@" || ko "$*"; fi
}
put() { # put PATH MODE OWNER  (content on stdin)
  local path mode owner
  path=$(at "$1"); mode=$2; owner=$3
  if [ "$DRY" = 1 ]; then
    printf 'DRY write %s (%s %s):\n' "$1" "$mode" "$owner"
    sed 's/^/    /'
  else
    install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" /dev/stdin "$path"
  fi
}
remember() { # remember PATH KIND
  if [ "$DRY" = 1 ]; then printf 'DRY state += %s %s\n' "$2" "$1"; else
    mkdir -p "$(dirname "$STATE")"
    printf '%s %s\n' "$2" "$1" >>"$STATE"
  fi
}
recorded() { # recorded PATH
  [ -f "$STATE" ] && grep -qxF "created $1" "$STATE"
}
absent() { # absent PATH : nothing of the host's there, so this script creates it
  [ ! -e "$(at "$1")" ]
}
restore_howto() {
  cat <<EOF
next, from a machine with an admin token on $REPO_SLUG (the operator, not the host):
  gh api "repos/$REPO_SLUG/actions/runners?per_page=100" --paginate \\
    -q '.runners[] | select(.name|startswith("$RUNNER_PREFIX-")) | "\(.id) \(.name)"'
  gh api -X DELETE "repos/$REPO_SLUG/actions/runners/<id>"     # once per line
then verify: the repository's runner list is back to its pre-loan set, and
the application's own runners are all still online (never unregistered here).
EOF
}

RUNNER_PREFIX="ameesh-nxl-$HOST"

user_exists() { # NAME
  if [ -n "$HOSTDIR" ]; then grep -q "^$1:" "$(at /etc/passwd)" 2>/dev/null
  else getent passwd "$1" >/dev/null 2>&1; fi
}
guest_groups() {
  if [ -n "$HOSTDIR" ]; then
    # the supplementary groups the test host gives the guest (its /etc/group)
    awk -F: -v u="$GUEST" '$4 ~ "(^|,)" u "(,|$)" {print $1}' "$(at /etc/group)" 2>/dev/null
  else id -nG "$GUEST" 2>/dev/null; fi
}
free_kib() { # a filesystem under PATH
  local p; p=$(at "$1")
  while [ ! -e "$p" ]; do p=$(dirname "$p"); done
  df -Pk "$p" | awk 'NR==2 {print $4}'
}
app_units() { # the application's own runner units: read, never written
  host_says systemctl list-units --all --plain --no-legend 'actions.runner.*' 2>/dev/null \
    | awk '{print $1}' | sort || true
}
app_count() { app_units | grep -c . || true; }
guest_busy() { # N : a job of runner N is running
  [ -n "${CI_RUNNERS_TEST_BUSY:-}" ] && return 0
  host_says pgrep -u "$GUEST" -af 'Runner.Worker' 2>/dev/null \
    | grep -q "ci/r$1/" || return 1
}
wait_terminal() { # N
  local n=$1 waited=0
  guest_busy "$n" || return 0
  [ "$WAIT" -gt 0 ] || return 1
  say "runner $n is running a job: waiting at most ${WAIT}s for it to end"
  while [ "$waited" -lt "$WAIT" ]; do
    run sleep 15
    if [ "$DRY" = 1 ]; then
      return 1   # a dry run never pretends the job ended
    fi
    waited=$((waited + 15))
    guest_busy "$n" || { say "runner $n is terminal"; return 0; }
  done
  return 1
}
unit_state() { host_says systemctl show "ameesh-guest-ci@$1.service" --property="$2" --value 2>/dev/null || true; }
slice_state() { host_says systemctl show ameesh-guest.slice --property="$1" --value 2>/dev/null || true; }

need_root() { [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (sudo)"; }

# --- the L159b/L159g prerequisites ------------------------------------------
preconditions() {
  need_root
  [ -f "$SRC_UNIT" ] || die "$SRC_UNIT is missing (run from the ameesh checkout)"
  [ -n "$HOSTDIR" ] || [ -f /sys/fs/cgroup/cgroup.controllers ] || die "cgroups v2 required"
  [ -f "$(at /etc/systemd/system/ameesh-guest.slice)" ] \
    || die "ameesh-guest.slice is not installed: run deploy/guest/install-pilot.sh first"
  user_exists "$GUEST" || die "user $GUEST is missing: run deploy/guest/install.sh first"
  for g in $(guest_groups); do
    case "$g" in sudo|wheel|admin|docker|adm|lxd|libvirt)
      die "$GUEST is in group $g: the guest must never be root-equivalent" ;;
    esac
  done
  command -v podman >/dev/null 2>&1 || die "podman is missing: run deploy/guest/install.sh first"
  command -v psql >/dev/null 2>&1 \
    || die "psql is missing: the ameesh CI installs postgresql-client with sudo, which the guest has not; install it on the host"
  command -v git >/dev/null 2>&1 || die "git is missing"
  local free; free=$(free_kib "$ROOT")
  [ "${free:-0}" -ge 15728640 ] \
    || die "$ROOT holds $(( ${free:-0} / 1024 )) MiB free, under the 15 GiB floor"
  if [ -n "$(host_says systemctl list-units --all --plain --no-legend \
      'gha-runner-scale-set*' 'arc-*' '*autoscal*' 2>/dev/null)" ]; then
    die "an autoscaler unit is present: this host is not a static VM (the loan needs one)"
  fi
  # rootless Podman is what keeps the application's Docker daemon out of reach
  local rootless
  rootless=$(host_says runuser -u "$GUEST" -- podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || true)
  if [ -z "$HOSTDIR" ]; then
    [ "$rootless" = true ] || die "rootless Podman does not answer for $GUEST (deploy/guest/install.sh --check)"
  else
    echo "DRY rootless Podman check for $GUEST (test host)"
  fi
  # the guard must count our units as guest units, or it will not stop them
  local guard; guard=$(at "$ETC/guard.json")
  if [ -f "$guard" ]; then
    python3 -I - "$guard" <<'PY' || die "the guard does not cover ameesh-guest-ci@*: re-run install-pilot.sh with --agent-units 'ameesh-guest-*.service'"
import fnmatch, json, sys
cfg = json.load(open(sys.argv[1]))
pattern = cfg.get("agent_units") or "ameesh-guest-runner@*.service"
sys.exit(0 if fnmatch.fnmatch("ameesh-guest-ci@1.service", pattern) else 1)
PY
  else
    echo "warning: $ETC/guard.json is absent: the guard will not stop these runners" >&2
  fi
}

# --- prepare ----------------------------------------------------------------
prepare() {
  say "prepare $COUNT runner(s) under $ROOT/ci, user $GUEST, slice ameesh-guest.slice"
  local n dir tarball_in=$TARBALL shim_new=0 unit_new=0
  # never overwrite a file that is not ours: a shim or a unit of this name
  # can only come from this lot, anything else is an anomaly to look at
  if [ -e "$(at "$SHIM")" ] && ! grep -qF "#1256" "$(at "$SHIM")" 2>/dev/null; then
    die "$SHIM exists and is not this script's shim (lot #1256): move it aside first"
  fi
  if [ -e "$(at "/etc/systemd/system/$UNIT")" ] \
     && ! cmp -s "$SRC_UNIT" "$(at "/etc/systemd/system/$UNIT")"; then
    die "/etc/systemd/system/$UNIT exists and differs from this checkout's: move it aside first"
  fi
  if [ -z "$tarball_in" ]; then
    tarball_in=$ROOT/ci/cache/actions-runner-linux-x64-$VERSION.tar.gz
  fi
  # only what this script creates is recorded, so --remove gives the host
  # back the way --prepare found it (an existing tree is left alone)
  if absent "$ROOT/ci"; then
    run install -d -o "$GUEST" -g "$GUEST" -m 0700 "$ROOT/ci"
    remember "$ROOT/ci" created
  fi
  if absent "$ROOT/ci/cache"; then
    run install -d -o "$GUEST" -g "$GUEST" -m 0700 "$ROOT/ci/cache"
    remember "$ROOT/ci/cache" created
  fi
  if absent "$ROOT/bin"; then
    run install -d -o root -g root -m 0755 "$ROOT/bin"
    remember "$ROOT/bin" created
  fi
  if absent "$SHIM"; then shim_new=1; fi
  put "$SHIM" 0755 root:root <<'SHIM'
#!/bin/sh
# The `docker` CLI the GitHub runner expects, over rootless Podman (lot
# #1256): the container engine of the guest, never the application's daemon.
set -eu
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
exec podman --cgroup-manager=cgroupfs "$@"
SHIM
  if [ "$shim_new" = 1 ]; then remember "$SHIM" created; fi
  if [ -n "$TARBALL" ]; then
    run test -r "$TARBALL"
    run test "$(sha256sum "$TARBALL" | awk '{print $1}')" = "$SHA256"
  elif [ -f "$(at "$tarball_in")" ] && [ "$DRY" = 0 ]; then
    say "cached tarball reused: $tarball_in"
  else
    run curl -fL --retry 3 --connect-timeout 20 \
      -o "$tarball_in" "$URL_BASE/v$VERSION/actions-runner-linux-x64-$VERSION.tar.gz"
    run sh -c "echo '$SHA256  $tarball_in' | sha256sum -c -"
  fi
  for n in $(seq 1 "$COUNT"); do
    dir=$ROOT/ci/r$n
    if [ -f "$(at "$dir/.runner")" ]; then
      say "r$n is already registered: software kept (re-run --remove first to reinstall)"
      continue
    fi
    if [ -e "$(at "$dir")" ] && ! recorded "$dir"; then
      die "$dir already exists and is not this script's: move it aside first"
    fi
    if absent "$dir"; then
      run install -d -o "$GUEST" -g "$GUEST" -m 0755 "$dir"
      remember "$dir" created
    fi
    run tar -xzf "$(at "$tarball_in")" -C "$(at "$dir")"
    run chown -R "$GUEST:$GUEST" "$(at "$dir")"
  done
  if absent "/etc/systemd/system/$UNIT"; then unit_new=1; fi
  put "/etc/systemd/system/$UNIT" 0644 root:root <"$SRC_UNIT"
  if [ "$unit_new" = 1 ]; then remember "/etc/systemd/system/$UNIT" created; fi
  run systemctl daemon-reload
  if [ "$DRY" = 0 ]; then
    {
      printf 'host %s\n' "$HOST"
      printf 'count %s\n' "$COUNT"
      printf 'version %s\n' "$VERSION"
      printf 'sha256 %s\n' "$SHA256"
      printf 'prefix %s\n' "$RUNNER_PREFIX"
      printf 'application_units %s\n' "$(app_count)"
    } >>"$STATE"
  fi
  say "prepared; next: --register --token-file FILE, then --enable, then --check"
}

# --- register ---------------------------------------------------------------
register() {
  need_root
  [ -n "$TOKEN_FILE" ] || die "--register needs --token-file FILE"
  local token; token=$(cat "$(at "$TOKEN_FILE")" 2>/dev/null || true)
  [ -n "$token" ] || die "$TOKEN_FILE is empty or unreadable"
  local n dir
  for n in $(seq 1 "$COUNT"); do
    dir=$ROOT/ci/r$n
    [ -f "$(at "$dir/config.sh")" ] || die "r$n is not prepared (--prepare --count $COUNT)"
    if [ -f "$(at "$dir/.runner")" ]; then
      say "r$n is already registered: skipped"
      continue
    fi
    if [ "$DRY" = 1 ]; then
      printf 'DRY runuser -u %s -- %s/config.sh --unattended --replace --url %s --token <token> --name %s-%s --labels ameesh-ci --work _work\n' \
        "$GUEST" "$dir" "$REPO_URL" "$RUNNER_PREFIX" "$n"
    else
      runuser -u "$GUEST" -- "$(at "$dir")/config.sh" --unattended --replace \
        --url "$REPO_URL" --token "$token" --name "$RUNNER_PREFIX-$n" \
        --labels ameesh-ci --work _work
    fi
  done
  say "registered; next: --enable, then a first job (qualification) and --check"
}

# --- enable / disable -------------------------------------------------------
targets() {
  if [ ${#TARGETS[@]} -gt 0 ]; then printf '%s\n' "${TARGETS[@]}"; else seq 1 "$COUNT"; fi
}
enable() {
  need_root
  local n
  for n in $(targets); do
    case "$n" in ''|*[!0-9]*) die "invalid runner $n" ;; esac
    [ -f "$(at "$ROOT/ci/r$n/.runner")" ] || die "r$n is not registered"
    run systemctl enable --now "ameesh-guest-ci@$n.service"
  done
}
disable() {
  need_root
  local n
  for n in $(targets); do
    case "$n" in ''|*[!0-9]*) die "invalid runner $n" ;; esac
    if ! wait_terminal "$n"; then
      die "runner $n is running a job: wait for the terminal state (or --wait-terminal S)"
    fi
    run systemctl disable --now "ameesh-guest-ci@$n.service"
  done
}

# --- check ------------------------------------------------------------------
check() {
  local host_name; host_name=$(hostname 2>/dev/null || echo "?")
  say "host: $host_name $(uname -r 2>/dev/null || true)"
  safe_run df -Pk "$(at "$ROOT")"
  say "user: $GUEST"
  if [ -n "$HOSTDIR" ]; then
    grep "^$GUEST:" "$(at /etc/passwd)" || ko "$GUEST is absent"
  else
    id "$GUEST" || ko "$GUEST is absent"
  fi
  local groups; groups=$(guest_groups)
  echo "groups: $groups"
  for g in $groups; do
    case "$g" in sudo|wheel|admin|docker|adm|lxd|libvirt) ko "$GUEST is in group $g" ;; esac
  done
  [ -z "$(command -v psql 2>/dev/null)" ] && ko "psql is absent (the CI step would need sudo)"
  say "slice caps"
  for p in CPUWeight IOWeight CPUQuotaPerSecUSec MemoryHigh MemoryMax TasksMax; do
    printf 'ameesh-guest.slice %s=%s\n' "$p" "$(slice_state "$p")"
  done
  say "containers: rootless Podman, the guest's own store"
  if [ -n "$HOSTDIR" ]; then
    echo "rootless=true (test host); host Docker access not probed"
  else
    runuser -u "$GUEST" -- podman info --format 'rootless={{.Host.Security.Rootless}} graph={{.Store.GraphRoot}}' \
      || ko "podman does not answer for $GUEST"
    # the application's daemon may exist; what matters is that the guest
    # cannot write to it (and that the shim never points there)
    if [ ! -e /run/docker.sock ]; then
      ok "no /run/docker.sock"
    elif runuser -u "$GUEST" -- test -w /run/docker.sock 2>/dev/null; then
      ko "$GUEST can write to /run/docker.sock (the application's daemon)"
    else
      ok "/run/docker.sock exists but $GUEST cannot reach it"
    fi
  fi
  say "shim: $SHIM"
  [ -x "$(at "$SHIM")" ] || ko "$SHIM is missing or not executable"
  say "runners"
  local n
  for n in $(seq 1 "$COUNT"); do
    local dir=$ROOT/ci/r$n
    local state enabled
    state=$(unit_state "$n" ActiveState); enabled=$(unit_state "$n" UnitFileState)
    printf 'r%s: unit=%s/%s dir=%s\n' "$n" "${state:-?}" "${enabled:-?}" "$dir"
    if [ -f "$(at "$dir/.runner")" ]; then
      python3 -I - "$(at "$dir/.runner")" <<'PY' || ko "r$n: .runner unreadable"
import json, sys
d = json.load(open(sys.argv[1]))
print("  name=%s server=%s" % (d.get("agentName"), d.get("serverUrl")))
if "manaty/ameesh" not in (d.get("serverUrl") or ""):
    sys.exit(1)
PY
      [ -f "$(at "$dir/.credentials")" ] && printf '  credentials: %s\n' \
        "$(stat -c '%a %U:%G' "$(at "$dir/.credentials")" 2>/dev/null || echo '?')" \
        || ko "r$n: .credentials is missing"
    else
      ko "r$n: not registered"
    fi
    [ "$state" = active ] || echo "note: r$n is not active (enabled=$enabled)"
  done
  say "application untouched"
  local now; now=$(app_count)
  local then; then=$(awk '/^application_units /{print $2}' "$STATE" 2>/dev/null || echo '')
  echo "actions.runner.* units: now=$now before=${then:-?}"
  if [ -n "$then" ] && [ "$now" != "$then" ]; then
    ko "the application's runner units changed ($then -> $now)"
  fi
  [ -d "$(at /home/runner)" ] && echo "note: /home/runner exists; this script never writes there"
  say "guard"
  local guard; guard=$(at "$ETC/guard.json")
  if [ -f "$guard" ]; then
    python3 -I - "$guard" <<'PY' || ko "the guard does not cover ameesh-guest-ci@*"
import fnmatch, json, sys
cfg = json.load(open(sys.argv[1]))
pattern = cfg.get("agent_units") or "ameesh-guest-runner@*.service"
print("agent_units=%s" % pattern)
sys.exit(0 if fnmatch.fnmatch("ameesh-guest-ci@1.service", pattern) else 1)
PY
  else
    ko "$ETC/guard.json is absent: nothing stops the guest when the application needs the host"
  fi
  [ "$BAD" = 0 ] || die "$BAD check(s) failed"
  say "check: every line ok"
}

# --- remove -----------------------------------------------------------------
remove() {
  need_root
  local n kept=()
  for n in $(seq 1 "$COUNT"); do
    if ! wait_terminal "$n"; then
      die "runner $n is running a job: --remove only after a terminal job (or --wait-terminal S)"
    fi
    run systemctl disable --now "ameesh-guest-ci@$n.service" || true
  done
  if recorded "/etc/systemd/system/$UNIT" || [ "$DRY" = 1 ]; then
    run rm -f "$(at "/etc/systemd/system/$UNIT")"
  elif cmp -s "$SRC_UNIT" "$(at "/etc/systemd/system/$UNIT")"; then
    # not recorded (older install): remove only if it is byte-identical to ours
    run rm -f "$(at "/etc/systemd/system/$UNIT")"
  else
    say "kept /etc/systemd/system/$UNIT (not this script's)"
  fi
  run systemctl daemon-reload
  for n in $(seq 1 "$COUNT"); do
    if [ "$KEEP" = 1 ]; then
      kept+=("$ROOT/ci/r$n")
      continue
    fi
    if recorded "$ROOT/ci/r$n"; then
      run rm -rf "$(at "$ROOT/ci/r$n")"
    fi
  done
  [ "$KEEP" = 1 ] && [ ${#kept[@]} -gt 0 ] && say "kept (--keep-workspaces): ${kept[*]}"
  if [ "$KEEP" = 0 ]; then
    recorded "$ROOT/ci/cache" && run rm -rf "$(at "$ROOT/ci/cache")"
    recorded "$ROOT/ci" && run rmdir --ignore-fail-on-non-empty "$(at "$ROOT/ci")" || true
  fi
  recorded "$SHIM" && run rm -f "$(at "$SHIM")"
  if [ "$DRY" = 0 ]; then
    [ "$KEEP" = 1 ] || rm -f "$STATE"
  else
    echo "DRY remove state: $STATE"
  fi
  say "host side removed"
  restore_howto
}

case "$MODE" in
  prepare) preconditions; prepare ;;
  register) preconditions; register ;;
  enable) enable ;;
  disable) disable ;;
  check) check ;;
  remove) remove ;;
esac
