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
#   --disk-floor-mib N    refuse --prepare/--register when the guest
#                         directory holds less than N MiB free (default
#                         15360 = 15 GiB). The Host fiche's reclaim.disk_free_min
#                         plus the install margin: a guest installed under the
#                         threshold that stops it would never run (plan §3, §6).
#                         --prepare records the qualified value in its state
#                         file; --register and the next --prepare adopt it
#                         when the option is not repeated (R1 delta B5)
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
#   CI_RUNNERS_TEST_HOST, CI_RUNNERS_TEST_STATE, CI_RUNNERS_TEST_BUSY,
#   CI_RUNNERS_TEST_ACTIVE (active units), CI_RUNNERS_TEST_CHILD (live runner
#   processes, stopped by the /run/ci-runners-test-child-gone-<N> marker),
#   CI_RUNNERS_TEST_STOP_DELAY, CI_RUNNERS_TEST_GUEST_READS (logical paths the
#   guest can read on the test host; the real probe runs `runuser`)
#
# Safety, by construction:
#   * every systemd unit this script writes matches `ameesh-guest-ci@*`; it
#     never stops, starts, enables, disables or removes `actions.runner.*`;
#   * the unit runs `/usr/local/lib/ameesh-guest-ci/ci-runner-gate` (installed here), not `run.sh`: the
#     GitHub runner starts only while the guard's gate is a fresh `available`
#     out of `enforce`, and is stopped when the gate closes, disappears or
#     goes stale (R1 #1256);
#   * containers run rootless, under the runner's cgroup, inside
#     `ameesh-guest.slice`: no `/run/docker.sock`, no `docker` group, so a job
#     cannot use the application's Docker daemon nor read its images;
#   * the registration token is read from a root-only file and only ever
#     passed to `config.sh`; the dry run prints `<token>` instead.
#   * the runner archive is verified by its pinned digest on a root-private
#     snapshot, and extracted by the guest: root never extracts a path the
#     guest can rewrite (R1 delta B4, #1256).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=/srv/ameesh-guest
GUEST=ameesh-guest
ETC=/etc/ameesh
UNITS=/etc/systemd/system
UNIT=ameesh-guest-ci@.service
SRC_UNIT=$HERE/$UNIT
STATE=$ETC/ci-runners.state
TOOLS=/usr/local/lib/ameesh-guest-ci
SHIM=$TOOLS/docker
# root-owned tools live outside the guest-writable root (R1 binome B4)
# the gate wrapper (R1 #1256): the unit's ExecStart, installed by --prepare
GATE_WRAPPER=$TOOLS/ci-runner-gate
# the guard's state document (L159f), read by the wrapper
GATE_STATE=/run/ameesh-gate/state/state.json
REPO_URL=https://github.com/manaty/ameesh
REPO_SLUG=manaty/ameesh
VERSION=2.338.0
# the digest published for actions/runner v2.338.0 linux-x64 (the fleet's)
SHA256=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32
URL_BASE=https://github.com/actions/runner/releases/download
HOST=$(hostname -s)
COUNT=2
COUNT_SET=0
# the prepare/register floor (MiB): the Host fiche's `reclaim.disk_free_min`
# plus the runner install margin. The preflight revises it on measurement
# (#1256: 6144 on the measured ci-3, 8.95 GiB free), never below the guard's
# own stop threshold. --prepare records it in the state file; the later
# commands adopt it (R1 delta B5) unless the operator repeats the option.
DISK_FLOOR_MIB=15360
FLOOR_SET=0
TARBALL=""
WAIT=0
KEEP=0
PURGE=0
DRY=0
MODE=""
TOKEN_FILE=""
TARGETS=()
# the root-private snapshot of the runner archive (R1 delta B4): a directory
# of its own, 0700, removed on exit
STAGE_DIR=""

die() { echo "ci-runners.sh: $*" >&2; exit 1; }
say() { echo "== $*"; }
ko() { echo "KO $*" >&2; BAD=$((BAD + 1)); }
ok() { echo "ok $*"; }
BAD=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host|--count|--tarball|--version|--sha256|--token-file|--wait-terminal|--disk-floor-mib)
      [ $# -ge 2 ] || die "$1 needs a value" ;;
  esac
  case "$1" in
    --host) HOST=$2; shift 2 ;;
    --count) COUNT=$2; COUNT_SET=1; shift 2 ;;
    --disk-floor-mib) DISK_FLOOR_MIB=$2; FLOOR_SET=1; shift 2 ;;
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
case "$DISK_FLOOR_MIB" in ''|*[!0-9]*) die "invalid --disk-floor-mib $DISK_FLOOR_MIB" ;; esac
[ ${#DISK_FLOOR_MIB} -le 7 ] || die "--disk-floor-mib: at most 1048576 MiB (1 TiB) expected"
[ "$DISK_FLOOR_MIB" -ge 1 ] || die "--disk-floor-mib: a positive number of MiB expected"
[ "$DISK_FLOOR_MIB" -le 1048576 ] || die "--disk-floor-mib: at most 1048576 MiB (1 TiB) expected"
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
# The disk floor qualified at --prepare is recorded too (R1 delta B5): on the
# measured ci-3 the plan prepares with 6144, and --register without the option
# used to fall back to the 15360 default and refuse the same host. An explicit
# --disk-floor-mib still wins; a recorded value only counts if it is one this
# script would have accepted.
if [ "$FLOOR_SET" = 0 ] && [ -f "$STATE" ]; then
  recorded_floor=$(awk '/^floor /{f=$2} END{print f}' "$STATE" 2>/dev/null || true)
  case "$recorded_floor" in
    ''|*[!0-9]*) ;;
    *) if [ "$recorded_floor" -ge 1 ] && [ "$recorded_floor" -le 1048576 ]; then
         DISK_FLOOR_MIB=$recorded_floor
       fi ;;
  esac
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
absent() { # absent PATH : nothing of the host's there, not even a dangling symlink
  [ ! -e "$(at "$1")" ] && [ ! -L "$(at "$1")" ]
}
no_symlink_path() { # SUFFIX (under $ROOT) : no component may be a symlink
  # $ROOT belongs to the guest: a symlink planted under it could otherwise
  # redirect the guest outside the loan (R1 delta B4, #1256). Directory
  # mutations run as the guest too, so a rename after this check cannot
  # redirect a privileged write or removal.
  local rel=${1#/} cur part
  cur=$(at "$ROOT")
  if [ -L "$cur" ]; then
    die "$ROOT is a symbolic link: refusing (the guest's volume must be a real directory)"
  fi
  while [ -n "$rel" ]; do
    part=${rel%%/*}
    if [ "$part" = "$rel" ]; then rel=""; else rel=${rel#*/}; fi
    [ -n "$part" ] || continue
    cur=$cur/$part
    if [ -L "$cur" ]; then
      die "$cur is a symbolic link: refusing (a guest-writable path must not redirect root)"
    fi
  done
}
root_tools_safe() {
  # Root writes only below a root-owned, non-writable chain outside $ROOT.
  # A check on a guest-owned parent cannot prevent a rename after the check.
  python3 -I - "$(at "$TOOLS")" "$HOSTDIR" <<'PYSAFE'
import os, stat, sys
path = sys.argv[1]
paths = []
while True:
    paths.append(path)
    parent = os.path.dirname(path)
    if parent == path:
        break
    path = parent
for path in reversed(paths):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        continue
    if not stat.S_ISDIR(info.st_mode) or (not sys.argv[2]
                                          and (info.st_uid != 0 or info.st_mode & 0o022)):
        sys.exit("unsafe root tools directory: " + path)
for name in ("docker", "ci-runner-gate"):
    path = os.path.join(sys.argv[1], name)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        continue
    if not stat.S_ISREG(info.st_mode) or (not sys.argv[2]
                                          and (info.st_uid != 0 or info.st_mode & 0o022)):
        sys.exit("unsafe root tools file: " + path)
PYSAFE
}
stage_cleanup() { # the root-private archive snapshot never outlives the run
  if [ -n "$STAGE_DIR" ]; then rm -rf "$STAGE_DIR"; fi
}
snapshot() { # SRC DST : the archive bytes, read by root into its private file
  cat -- "$1" >"$2"
}
trap stage_cleanup EXIT
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
# R2 #1256 B2 : l'isolation dans l'autre sens. Un job de l'application est
# root-equivalent sur cette VM (les jobs de la flotte lancent Docker) : il ne
# doit donc pas pouvoir lire les secrets du runner ameesh, ni son arbre de
# travail. Le test tourne en tant qu'invité (`runuser`), jamais en root.
guest_reads() { # PATH logique : 0 si ameesh-guest peut le lire
  if [ -n "$HOSTDIR" ]; then
    case ",${CI_RUNNERS_TEST_GUEST_READS:-}," in
      *",$1,"*) return 0 ;;
      *) return 1 ;;
    esac
  fi
  runuser -u "$GUEST" -- test -r "$(at "$1")" 2>/dev/null
}
app_isolation_paths() { # les chemins logiques de l'application à sonder
  local root=/home/runner mapped; mapped=$(at "$root")
  [ -e "$mapped" ] || return 0
  printf '%s\n' "$root"
  [ -d "$mapped" ] || return 0
  local p
  while IFS= read -r p; do
    printf '%s\n' "$root${p#"$mapped"}"
  done < <(find "$mapped" -maxdepth 5 \
             \( -name .runner -o -name .credentials \
                -o -name .credentials_rsaparams -o -name _work \) \
             -print 2>/dev/null)
}
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
unit_state() { # N PROPERTY (the test host fakes the states it asserts)
  if [ -n "$HOSTDIR" ] && [ -n "${CI_RUNNERS_TEST_ACTIVE:-}" ]; then
    local active=0
    case ",$CI_RUNNERS_TEST_ACTIVE," in *",$1,"*) active=1 ;; esac
    case "$2" in
      ActiveState) [ "$active" = 1 ] && echo active || echo inactive; return ;;
      UnitFileState) [ "$active" = 1 ] && echo enabled || echo disabled; return ;;
    esac
    return
  fi
  host_says systemctl show "ameesh-guest-ci@$1.service" --property="$2" --value 2>/dev/null || true
}
# C2 de la R1 amont : une unité active n'est pas encore une désobéissance. Le
# wrapper (`ci-runner-gate.py:supervise`) arrête le child puis reste actif à
# attendre la réouverture ; seul un processus runner (listener, worker ou
# run.sh) encore vivant au-delà du délai d'arrêt du wrapper prouve qu'il ne
# l'arrête pas. Le wrapper lui-même porte `--dir .../ci/rN` : il est exclu.
runner_child() { # N : un processus runner de l'unité N est vivant
  if [ -n "$HOSTDIR" ]; then
    case ",${CI_RUNNERS_TEST_CHILD:-}," in *",$1,"*) ;; *) return 1 ;; esac
    # le témoin peut poser ce marqueur pendant l'attente : child arrêté
    [ -e "$(at "/run/ci-runners-test-child-gone-$1")" ] && return 1
    return 0
  fi
  pgrep -u "$GUEST" -af 'Runner\.(Listener|Worker)|/run\.sh' 2>/dev/null \
    | grep -q "ci/r$1/"
}
runner_stop_delay_s() { # le délai du wrapper : intervalle de veille + grâce
  local delay=${CI_RUNNERS_TEST_STOP_DELAY:-}
  if [ -z "$HOSTDIR" ]; then
    delay=$(python3 -I - "$HERE/ci-runner-gate.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ci_runner_gate", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
    print(int(mod.DEFAULT_INTERVAL + mod.DEFAULT_GRACE) + 2)
except Exception:  # source illisible : la borne dure de l'unité (TimeoutStopSec)
    print(180)
PY
) || delay=180
  fi
  case "$delay" in ''|*[!0-9]*) echo 0 ;; *) echo "$delay" ;; esac
}
# la porte que le wrapper obéit : fraîche, `available` ET publiée en `enforce`
# (mêmes règles que `ci-runner-gate.py:read_gate`) ; rend « yes » ou « no ».
gate_obeys() {
  python3 -I - "$(at "$GATE_STATE")" "$HERE/../../src" <<'PY'
import json, sys, time
sys.path.insert(0, sys.argv[2])
try:
    from ameesh.capacity_guard import gate_state
except ImportError:
    print("no"); sys.exit(0)
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        doc = json.load(fh)
except (OSError, ValueError):
    doc = None
guard = doc.get("guard") if isinstance(doc, dict) else None
mode = guard.get("mode") if isinstance(guard, dict) else None
print("yes" if gate_state(doc, time.time(), lending=True) == "available"
      and mode == "enforce" else "no")
PY
}
wait_runner_stops() { # N : 0 child disparu, 2 porte rouverte (child légitime), 1 délai épuisé
  local n=$1 delay deadline
  delay=$(runner_stop_delay_s)
  say "r$n: the gate is closed and its runner process is alive; waiting up to ${delay}s for the wrapper to stop it"
  deadline=$(( $(date +%s) + delay ))
  while runner_child "$n"; do
    # la porte est relue à chaque tour : une réouverture available/enforce
    # rend le child légitime, le KO ne juge pas sur une porte périmée
    [ "$(gate_obeys)" = yes ] && return 2
    [ "$(date +%s)" -lt "$deadline" ] || return 1
    sleep 1
  done
  return 0
}
slice_state() { host_says systemctl show ameesh-guest.slice --property="$1" --value 2>/dev/null || true; }

need_root() { [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (sudo)"; }

# the guard (L159g) is what stops the guest runners when the application needs
# the host. #1256 R1 : sans lui, ou s'il ne couvre pas nos unités, rien ne les
# arrête — la préparation et le démarrage sont donc refusés, pas avertis.
guard_config() { echo "$(at "$ETC/guard.json")"; }
guard_covers() { # the guard's agent_units pattern stops ameesh-guest-ci@*
  local guard; guard=$(guard_config)
  [ -f "$guard" ] || return 1
  python3 -I - "$guard" <<'PY'
import fnmatch, json, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception:  # noqa: BLE001 - illisible : non couvert
    sys.exit(1)
pattern = cfg.get("agent_units") or "ameesh-guest-runner@*.service"
sys.exit(0 if fnmatch.fnmatch("ameesh-guest-ci@1.service", pattern) else 1)
PY
}
guard_enforcing() { # the guard's installed mode is enforce, not observe
  local env; env=$(at "$ETC/guard.env")
  [ -f "$env" ] || return 1   # absent: the unit's default is observe
  ! grep -q '^AMEESH_GUARD_ARGS=--observe' "$env"
}
gate_now() { # the gate state the guest obeys: available, draining, stopped, unknown
  python3 -I - "$(at "$GATE_STATE")" "$HERE/../../src" <<'PY'
import json, sys, time
sys.path.insert(0, sys.argv[2])
try:
    from ameesh.capacity_guard import gate_state
except ImportError:  # pas de contrat lisible : la porte est fermee
    print("unknown"); sys.exit(0)
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        doc = json.load(fh)
except (OSError, ValueError):
    doc = None
print(gate_state(doc, time.time(), lending=True))
PY
}

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
  local free floor_kib; free=$(free_kib "$ROOT"); floor_kib=$((DISK_FLOOR_MIB * 1024))
  [ "${free:-0}" -ge "$floor_kib" ] \
    || die "$ROOT holds $(( ${free:-0} / 1024 )) MiB free, under the ${DISK_FLOOR_MIB} MiB floor (--disk-floor-mib)"
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
  # (#1256 R1 : absent, ou sans couverture, il refuse au lieu d'avertir)
  if ! guard_covers; then
    if [ -f "$(guard_config)" ]; then
      die "the guard does not cover ameesh-guest-ci@*: re-run install-pilot.sh with --agent-units 'ameesh-guest-*.service'"
    fi
    die "$(guard_config) is absent: the guard is what stops these runners when the application needs the host; run install-pilot.sh first"
  fi
  # the unit's wrapper reads the gate with the guest's venv (R1 #1256): a
  # guest that cannot import the contract is known here, not at the first job
  if [ -z "$HOSTDIR" ]; then
    runuser -u "$GUEST" -- /opt/ameesh/venv/bin/python3 -c \
      'from ameesh.capacity_guard import gate_state' >/dev/null 2>&1 \
      || die "$GUEST cannot import the gate contract from /opt/ameesh/venv: the gate wrapper would refuse every start"
  else
    echo "DRY gate contract import check for $GUEST (test host)"
  fi
}

# --- prepare ----------------------------------------------------------------
prepare() {
  say "prepare $COUNT runner(s) under $ROOT/ci, user $GUEST, slice ameesh-guest.slice"
  local n dir tarball_in=$ROOT/ci/cache/actions-runner-linux-x64-$VERSION.tar.gz shim_new=0 wrapper_new=0 unit_new=0
  root_tools_safe || die "unsafe root tools path"
  # never overwrite a file that is not ours: a shim or a unit of this name
  # can only come from this lot, anything else is an anomaly to look at
  if [ -e "$(at "$SHIM")" ] && ! grep -qF "#1256" "$(at "$SHIM")" 2>/dev/null; then
    die "$SHIM exists and is not this script's shim (lot #1256): move it aside first"
  fi
  if [ -e "$(at "$GATE_WRAPPER")" ] && ! grep -qF "#1256" "$(at "$GATE_WRAPPER")" 2>/dev/null; then
    die "$GATE_WRAPPER exists and is not this script's gate wrapper (lot #1256): move it aside first"
  fi
  if [ -e "$(at "/etc/systemd/system/$UNIT")" ] \
     && ! cmp -s "$SRC_UNIT" "$(at "/etc/systemd/system/$UNIT")"; then
    die "/etc/systemd/system/$UNIT exists and differs from this checkout's: move it aside first"
  fi
  # only what this script creates is recorded, so --remove gives the host
  # back the way --prepare found it (an existing tree is left alone). $ROOT
  # belongs to the guest: even after a symlink check, all directory creation
  # and removal below it runs as the guest. Root only writes the tools outside
  # this tree, under a root-owned chain (R1 delta B4, #1256).
  no_symlink_path ci
  no_symlink_path ci/cache
  if absent "$ROOT/ci"; then
    run runuser -u "$GUEST" -- install -d -m 0700 "$(at "$ROOT/ci")"
    remember "$ROOT/ci" created
  fi
  if absent "$ROOT/ci/cache"; then
    run runuser -u "$GUEST" -- install -d -m 0700 "$(at "$ROOT/ci/cache")"
    remember "$ROOT/ci/cache" created
  fi
  if absent "$TOOLS"; then
    run install -d -o root -g root -m 0755 "$(at "$TOOLS")"
    remember "$TOOLS" created
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
  # the unit's ExecStart (R1 #1256): the GitHub runner obeys the guard's gate
  # through it, so it starts and stops with the state, not with systemd alone
  if absent "$GATE_WRAPPER"; then wrapper_new=1; fi
  put "$GATE_WRAPPER" 0755 root:root <"$HERE/ci-runner-gate.py"
  if [ "$wrapper_new" = 1 ]; then remember "$GATE_WRAPPER" created; fi
  # #1256 R1 delta B4 : le cache vit sous $ROOT/ci, que l'invité possède, et
  # la destination d'extraction aussi. Root n'extrait donc plus jamais une
  # archive depuis un chemin que l'invité peut remplacer entre le digest et
  # le tar : il recopie les octets dans un instantané privé (dossier 0700,
  # fichier 0600, sous /var/tmp), y vérifie le digest, et c'est l'invité qui
  # extrait cet instantané, par le descripteur que root lui ouvre. Le cache
  # ne sert que de commodité : re-vérifié à chaque prepare, et réécrit par
  # l'invité lui-même après une extraction réussie (publication sûre).
  local stage
  if [ "$DRY" = 1 ]; then
    stage="${TMPDIR:-/var/tmp}/ameesh-ci-stage.XXXXXX/runner.tar.gz"
  else
    STAGE_DIR=$(mktemp -d "${TMPDIR:-/var/tmp}/ameesh-ci-stage.XXXXXX") \
      || die "cannot create the root-private snapshot directory under ${TMPDIR:-/var/tmp}"
    chmod 0700 "$STAGE_DIR"
    stage=$STAGE_DIR/runner.tar.gz
    : >"$stage"
    chmod 0600 "$stage"
  fi
  if [ -n "$TARBALL" ]; then
    run test -r "$TARBALL"
    run snapshot "$TARBALL" "$stage"
  elif [ -f "$(at "$tarball_in")" ]; then
    no_symlink_path "ci/cache/actions-runner-linux-x64-$VERSION.tar.gz"
    say "cached tarball reused: $tarball_in (root-private snapshot, digest verified)"
    run snapshot "$(at "$tarball_in")" "$stage"
  else
    run curl -fL --retry 3 --connect-timeout 20 \
      -o "$stage" "$URL_BASE/v$VERSION/actions-runner-linux-x64-$VERSION.tar.gz"
  fi
  if [ "$DRY" = 1 ]; then
    printf 'DRY sha256sum -c - <= "%s  %s"\n' "$SHA256" "$stage"
  else
    printf '%s  %s\n' "$SHA256" "$stage" | sha256sum -c -
  fi
  for n in $(seq 1 "$COUNT"); do
    dir=$ROOT/ci/r$n
    no_symlink_path "ci/r$n"
    if [ -f "$(at "$dir/.runner")" ]; then
      say "r$n is already registered: software kept (re-run --remove first to reinstall)"
      continue
    fi
    if [ -e "$(at "$dir")" ] && ! recorded "$dir"; then
      die "$dir already exists and is not this script's: move it aside first"
    fi
    if absent "$dir"; then
      run runuser -u "$GUEST" -- install -d -m 0755 "$(at "$dir")"
      remember "$dir" created
    fi
    # extraction sous l'invité, depuis l'instantané privé de root : un
    # remplacement de chemin après le digest ne fait plus écrire root
    if [ "$DRY" = 1 ]; then
      printf 'DRY runuser -u %s -- tar -xzf - -C %s < %s (root-private snapshot)\n' \
        "$GUEST" "$(at "$dir")" "$stage"
    else
      runuser -u "$GUEST" -- tar -xzf - -C "$(at "$dir")" <"$stage"
    fi
  done
  # publication du cache par l'invité (le cache est sous $ROOT/ci, donc à
  # lui) : les octets viennent de l'instantané vérifié, et la prochaine
  # préparation re-vérifie le digest de toute façon
  if [ "$DRY" = 1 ]; then
    printf 'DRY runuser -u %s -- sh -c %s < %s (cache publication)\n' \
      "$GUEST" "'cat >$tarball_in'" "$stage"
  else
    runuser -u "$GUEST" -- sh -c 'cat >"$1"' sh "$(at "$tarball_in")" <"$stage"
  fi
  if absent "/etc/systemd/system/$UNIT"; then unit_new=1; fi
  put "/etc/systemd/system/$UNIT" 0644 root:root <"$SRC_UNIT"
  if [ "$unit_new" = 1 ]; then remember "/etc/systemd/system/$UNIT" created; fi
  run systemctl daemon-reload
  if [ "$DRY" = 0 ]; then
    {
      printf 'host %s\n' "$HOST"
      printf 'count %s\n' "$COUNT"
      printf 'floor %s\n' "$DISK_FLOOR_MIB"
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
  # #1256 R1 : activation fermée par défaut. Le garde doit couvrir nos unités
  # ET être en enforce avant le premier job : en observation il ne les arrête
  # pas, donc les invités restent éteints tant qu'il observe (plan §5, §6).
  guard_covers \
    || die "the guard does not cover ameesh-guest-ci@*: install-pilot.sh --agent-units 'ameesh-guest-*.service' first"
  guard_enforcing \
    || die "the guard is in observation: --enable only in enforce mode (deploy/guest/install-pilot.sh --set-guard-mode enforce), guests off until then"
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
  local n active_runners=0
  for n in $(seq 1 "$COUNT"); do
    local dir=$ROOT/ci/r$n
    local state enabled
    state=$(unit_state "$n" ActiveState); enabled=$(unit_state "$n" UnitFileState)
    printf 'r%s: unit=%s/%s dir=%s\n' "$n" "${state:-?}" "${enabled:-?}" "$dir"
    [ "$state" = active ] && active_runners=$((active_runners + 1)) || true
    if [ -f "$(at "$dir/.runner")" ]; then
      # C1 (R1 amont #1256) : `config.sh --url` est relu dans `gitHubUrl`
      # (ConfigurationManager.cs : GitHubUrl=inputUrl, ServerUrl=TenantUrl) ;
      # `serverUrl` ne nomme que le transport Actions et reste affiché.
      python3 -I - "$(at "$dir/.runner")" "$REPO_URL" <<'PY' \
        || ko "r$n: .runner unreadable or registered on another repository"
import json, sys


def normalize(url):
    url = (url or "").strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    host, _, path = rest.partition("/")
    return "%s://%s/%s" % (scheme.lower(), host.lower(), path)


d = json.load(open(sys.argv[1]))
print("  name=%s repo=%s server=%s"
      % (d.get("agentName"), d.get("gitHubUrl"), d.get("serverUrl")))
sys.exit(0 if normalize(d.get("gitHubUrl")) == normalize(sys.argv[2]) else 1)
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
  say "application isolation (a job of the application is root-equivalent here)"
  local probe probes
  probes=$(app_isolation_paths)
  if [ -z "$probes" ]; then
    echo "note: no application runner tree at /home/runner: nothing to probe"
  else
    while IFS= read -r probe; do
      if guest_reads "$probe"; then
        ko "$GUEST can read $probe: an application job could impersonate the ameesh runner (R2 B2)"
      else
        ok "$GUEST cannot read $probe"
      fi
    done <<<"$probes"
  fi
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
  say "gate"
  local gate; gate=$(gate_now)
  if [ -f "$(at "$GATE_STATE")" ]; then
    echo "state: $gate ($GATE_STATE)"
  else
    echo "state: $gate (no document yet at $GATE_STATE; normal before install-pilot)"
  fi
  if [ -x "$(at "$GATE_WRAPPER")" ]; then
    echo "wrapper: $GATE_WRAPPER"
  else
    echo "wrapper: $GATE_WRAPPER is missing (--prepare installs it; the unit refuses to start)"
    if [ "$active_runners" != 0 ]; then
      ko "a guest runner is active without its gate wrapper: nothing obeys the guard"
    fi
  fi
  # C2 de la R1 amont : le wrapper arrête le child sur une porte fermée ou
  # périmée (R1 #1256) puis **reste actif** à attendre la réouverture
  # (`ci-runner-gate.py:supervise`). Une unité active sans processus runner
  # est donc ce fonctionnement correct ; un processus runner encore vivant
  # après le délai d'arrêt du wrapper est la désobéissance.
  if [ "$active_runners" != 0 ] && [ "$gate" != available ]; then
    local waiting=0 offender="" rc=0
    for n in $(seq 1 "$COUNT"); do
      [ "$(unit_state "$n" ActiveState)" = active ] || continue
      if runner_child "$n"; then
        rc=0
        wait_runner_stops "$n" || rc=$?
        if [ "$rc" = 0 ]; then
          say "r$n: the runner process is gone (gate $gate)"
        elif [ "$rc" = 2 ]; then
          say "r$n: the gate reopened while waiting; its runner is legitimate"
        else
          offender=$n
        fi
      else
        waiting=$((waiting + 1))
      fi
    done
    # relecture fraîche avant le verdict : une porte rouverte pendant
    # l'attente ne doit pas devenir un KO sur la porte initialement fermée
    if [ -n "$offender" ] && [ "$(gate_obeys)" = yes ]; then
      say "r$offender: the gate reopened while checking; its runner is legitimate"
      offender=""
    fi
    if [ -n "$offender" ]; then
      ko "r$offender still runs after the wrapper's stop delay while the gate is $(gate_now): the guard is not obeyed"
    elif [ "$waiting" != 0 ]; then
      echo "note: $waiting guest runner(s) active without their runner process: the wrapper waits for the gate"
    fi
  fi
  [ "$BAD" = 0 ] || die "$BAD check(s) failed"
  say "check: every line ok"
}

# --- remove -----------------------------------------------------------------
remove() {
  need_root
  root_tools_safe || die "unsafe root tools path"
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
      run runuser -u "$GUEST" -- rm -rf "$(at "$ROOT/ci/r$n")"
    fi
  done
  [ "$KEEP" = 1 ] && [ ${#kept[@]} -gt 0 ] && say "kept (--keep-workspaces): ${kept[*]}"
  if [ "$KEEP" = 0 ]; then
    recorded "$ROOT/ci/cache" && run runuser -u "$GUEST" -- rm -rf "$(at "$ROOT/ci/cache")"
    recorded "$ROOT/ci" && run runuser -u "$GUEST" -- rmdir --ignore-fail-on-non-empty "$(at "$ROOT/ci")" || true
  fi
  recorded "$SHIM" && run rm -f "$(at "$SHIM")"
  recorded "$GATE_WRAPPER" && run rm -f "$(at "$GATE_WRAPPER")"
  recorded "$TOOLS" && run rmdir --ignore-fail-on-non-empty "$(at "$TOOLS")" || true
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
