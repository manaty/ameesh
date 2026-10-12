#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Installs the pilot (a1) of a lending host: full ameesh runners as a GUEST
# under a capped slice, watched by the capacity guard (lot L159g, study
# docs/design/etudes/l159-capacite-des-hotes.md, sections 2.1, 3, 4.2, 4.3).
# Run by the host's owner, as root, from a checkout of ameesh; an agent never
# changes the units of a host. Read it, then run it in this order:
#
#   sudo deploy/guest/install-pilot.sh --dry-run [options]   everything, unchanged
#   sudo deploy/guest/install-pilot.sh [options]             install; guard in observation
#   (fill /etc/ameesh/guest.env and /etc/ameesh/guest-<agent>.env)
#   sudo deploy/guest/install-pilot.sh --check               what is in place
#   sudo deploy/guest/install-pilot.sh --enable AGENT…       start guest runners
#   sudo deploy/guest/install-pilot.sh --set-guard-mode enforce  the guard sets the caps
#   sudo deploy/guest/install-pilot.sh --uninstall [--purge] rollback (study §4.3)
#
# Options:
#   --dry-run            print every command and file, change nothing
#   --host NAME          host name in the canon (default: hostname -s)
#   --agent NAME         a guest agent: its secrets file is prepared
#                        (repeatable; default deepseek6, deepseek1)
#   --max-agents N       guest agents at once, for the guard (default 2)
#   --lend FILE          the offer, JSON: the `policy.lend` of the canon's
#                        Host fiche (default: the one printed by --dry-run)
#   --root DIR           the guest's volume (default /srv/ameesh-guest; a
#                        dedicated mount point, --allow-rootfs otherwise);
#                        the runner's unit is installed with it
#   --path DIR           extra PATH of the runners, first (repeatable;
#                        default: the directory of `dsh` if outside /usr)
#   --accept-larger-volume  install on a volume bigger than the offer's
#                        disk_max (refused otherwise: nothing else holds it)
#   --guard-mode M       mode of the guard: observe or enforce (default: the
#                        installed one, observe on a first install)
#   --set-guard-mode M   switch the installed guard (restarts it; observe
#                        also takes the guard's caps off the guest)
#   --enable AGENT…      enable and start the runners of these agents
#   --disable AGENT…     stop and disable them (their turn drains first)
#   --check              report the state of everything installed
#   --uninstall          rollback: drain, stop, remove units and files
#   --purge              with --uninstall: also secrets, guard config,
#                        venv, user and volume content this script created
#
# Installed (all of it removed by --uninstall; what this script created,
# user and absolute paths, is recorded in /etc/ameesh/pilot-install.state
# BEFORE it is created, and --purge removes only that):
#   * user ameesh-guest (system, locked, no sudo, no docker), home
#     $ROOT/home, $ROOT/{tmp,work}, all 0700;
#   * /opt/ameesh/venv: ameesh[psycopg] from this checkout (root-owned);
#   * /etc/systemd/system: ameesh-guest.slice, ameesh.slice.d/guest-weight.conf,
#     ameesh-guest-runner@.service (its paths on --root, + a PATH drop-in),
#     ameesh-guard.service;
#   * /etc/tmpfiles.d/ameesh-gate.conf: /run/ameesh-gate/{state,ack};
#   * /etc/ameesh/guard.json (offer), guard.env (mode), guest.env and
#     guest-<agent>.env (secrets, 0640 root:ameesh-guest, never overwritten;
#     --purge removes only those this script created).
# Not done here (study §4.2, §4.3): the canon PR (Host `policy.lend`, the
# guest agents' placements), the host's Postgres login role (L56), the
# agents' clones under $ROOT/work.
# `disk_max` is enforced by nothing but the volume's size: a volume bigger
# than it is refused (--accept-larger-volume overrides), the root filesystem
# (--allow-rootfs) does not hold it.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CHECKOUT=$(cd "$HERE/../.." && pwd)
GUEST=ameesh-guest
ROOT=/srv/ameesh-guest
ETC=/etc/ameesh
UNITS=/etc/systemd/system
VENV=/opt/ameesh/venv
TMPFILES=/etc/tmpfiles.d/ameesh-gate.conf
PATH_DROPIN=$UNITS/ameesh-guest-runner@.service.d/50-pilot-path.conf
STATE=$ETC/pilot-install.state
HOST=$(hostname -s)
AGENTS=()
MAX_AGENTS=2
LEND=""
EXTRA_PATH=()
GUARD_MODE=""
TARGETS=()
DRY=0
ALLOW_ROOTFS=0
ACCEPT_LARGER=0
MODE=install
PURGE=0

die() { echo "install-pilot.sh: $*" >&2; exit 1; }
say() { echo "== $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host|--agent|--max-agents|--lend|--root|--path|--guard-mode|--set-guard-mode)
      [ $# -ge 2 ] || die "$1 needs a value" ;;
  esac
  case "$1" in
    --host) HOST=$2; shift 2 ;;
    --agent) AGENTS+=("$2"); shift 2 ;;
    --max-agents) MAX_AGENTS=$2; shift 2 ;;
    --lend) LEND=$2; shift 2 ;;
    --root) ROOT=$2; shift 2 ;;
    --path) EXTRA_PATH+=("$2"); shift 2 ;;
    --guard-mode) GUARD_MODE=$2; shift 2 ;;
    --set-guard-mode) MODE=guard-mode; GUARD_MODE=$2; shift 2 ;;
    --allow-rootfs) ALLOW_ROOTFS=1; shift ;;
    --accept-larger-volume) ACCEPT_LARGER=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --check) MODE=check; shift ;;
    --enable|--disable) MODE=${1#--}; shift; TARGETS=("$@"); break ;;
    --uninstall) MODE=uninstall; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '3,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done
[ ${#AGENTS[@]} -gt 0 ] || AGENTS=(deepseek6 deepseek1)

valid_name() { case "$1" in ''|*[!a-z0-9_-]*|-*) return 1 ;; esac; }
valid_name "$HOST" || die "invalid --host $HOST"
for a in "${AGENTS[@]}" "${TARGETS[@]}"; do valid_name "$a" || die "invalid agent name $a"; done
case "$MAX_AGENTS" in ''|*[!0-9]*) die "invalid --max-agents $MAX_AGENTS" ;; esac
case "$GUARD_MODE" in ''|observe|enforce) ;; *) die "--guard-mode observe|enforce" ;; esac
[ "$MODE" != guard-mode ] || [ -n "$GUARD_MODE" ] || die "--set-guard-mode observe|enforce"
case "$ROOT" in /*) ;; *) die "--root must be absolute" ;; esac
case "$ROOT" in *[!A-Za-z0-9/._-]*|*/../*|*/..|*/./*|*/.|*//*|*/) die "invalid --root $ROOT" ;; esac
case "$ROOT" in /|/home|/tmp|/srv|/root|/opt|/usr|/etc|/var|/run|/mnt) die "--root $ROOT: a directory of its own expected" ;; esac
[ "$PURGE" = 0 ] || [ "$MODE" = uninstall ] || die "--purge goes with --uninstall"
# the tests' dry runs read a state of their own; a real run reads only $STATE
[ "$DRY" = 1 ] && [ -n "${PILOT_TEST_STATE:-}" ] && STATE=$PILOT_TEST_STATE
[ "$DRY" = 1 ] && [ -n "${PILOT_TEST_VENV:-}" ] && VENV=$PILOT_TEST_VENV
# the tests' dry runs see a host of their own, PILOT_TEST_HOST=DIR: its
# /etc, /opt, /srv and /run are read under DIR, its users in DIR/etc/passwd,
# its mounts in DIR/proc/self/mountinfo, and systemd is asked nothing; a
# real run reads only the host
HOSTDIR=
if [ "$DRY" = 1 ] && [ -n "${PILOT_TEST_HOST:-}" ]; then
  HOSTDIR=$PILOT_TEST_HOST
  case "$HOSTDIR" in /?*) ;; *) die "PILOT_TEST_HOST must be absolute" ;; esac
  [ -d "$HOSTDIR" ] || die "PILOT_TEST_HOST $HOSTDIR: no such directory"
fi
# at PATH: where PATH of the host is read (itself, but in a test's host)
at() {
  case "$1" in
    /etc|/etc/*|/opt|/opt/*|/srv|/srv/*|/run|/run/*|/proc/*) printf '%s%s' "$HOSTDIR" "$1" ;;
    *) printf '%s' "$1" ;;
  esac
}
# host_says CMD…: asks the live host (systemd, the user database); a test's
# host has nothing to ask: CMD fails there
host_says() { [ -z "$HOSTDIR" ] && "$@"; }
user_exists() {
  if [ -n "$HOSTDIR" ]; then grep -q "^$GUEST:" "$HOSTDIR/etc/passwd" 2>/dev/null
  else id -u "$GUEST" >/dev/null 2>&1; fi
}
# the mounts, binds included: read in mountinfo, never guessed (a dry run of
# the tests may read a mountinfo of its own)
MOUNTINFO=$(at /proc/self/mountinfo)
[ "$DRY" = 1 ] && [ -n "${PILOT_TEST_MOUNTINFO:-}" ] && MOUNTINFO=$PILOT_TEST_MOUNTINFO
# a path through a symbolic link is refused: mounts and records are compared
# as resolved paths, so the paths themselves must be resolved
resolved() { local p; p=$(at "$1"); [ "$(realpath -m -- "$p")" = "$p" ]; }
if [ "$MODE" = install ]; then
  resolved "$ROOT" || die "--root $ROOT goes through a symbolic link: give $(r=$(realpath -m -- "$(at "$ROOT")"); echo "${r#"$HOSTDIR"}")"
  resolved "$VENV" || die "$VENV goes through a symbolic link"
fi

# run CMD…: executes, or prints it under --dry-run (where the tests may
# make one command fail: PILOT_TEST_FAIL=<its name>)
run() {
  if [ "$DRY" = 1 ]; then
    echo "+ $*"
    [ "$1" != "${PILOT_TEST_FAIL:-}" ]
  else
    "$@"
  fi
}

# put PATH MODE OWNER < content: writes a file atomically, or prints it
put() {
  local path=$1 mode=$2 owner=$3 content
  content=$(cat)
  if [ "$DRY" = 1 ]; then
    printf -- '--- %s (%s %s)\n%s\n---\n' "$path" "$mode" "$owner" "$content"
    return
  fi
  install -d "$(dirname "$path")"
  printf '%s\n' "$content" > "$path.tmp"
  chmod "$mode" "$path.tmp"
  chown "$owner" "$path.tmp"
  mv -f "$path.tmp" "$path"
}

need_root() {
  [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (sudo)"
}

# remember ENTRY: records, before creating it, what this script creates:
# `user`, or KIND and an absolute path, KIND being
#   guest-tree / venv-tree   removed with its content by --purge
#   guest-dir / venv-dir     removed by --purge only if empty
# (guest-*: kept while the offload of L159b uses the volume)
remember() {
  if [ "$DRY" = 1 ]; then echo "+ record '$1' in $STATE"; return; fi
  install -d -m 0755 "$ETC"
  grep -qxF "$1" "$STATE" 2>/dev/null || echo "$1" >> "$STATE"
}
created() { grep -qxF "$1" "$(at "$STATE")" 2>/dev/null; }
# absent PATH: nothing there, not even a dangling link
absent() { local p; p=$(at "$1"); [ ! -e "$p" ] && [ ! -L "$p" ]; }
# not_a_dir PATH: something there that is not a plain directory
not_a_dir() { local p; p=$(at "$1"); [ -L "$p" ] || { [ -e "$p" ] && [ ! -d "$p" ]; }; }

# mounts_under PATH: a mount point at PATH or beneath it (a bind mount of
# the same filesystem included, which rm --one-file-system would cross);
# PATH is a resolved path, like mountinfo's
mounts_under() {
  awk -v p="$1" '{ if ($5 == p || index($5, p "/") == 1) found = 1 } END { exit !found }' \
    "$MOUNTINFO"
}
# is_mount PATH: PATH is a mount point (a bind mount of a directory included)
is_mount() {
  awk -v p="$1" '$5 == p { found = 1 } END { exit !found }' "$MOUNTINFO"
}

# remove_tree [--runtime] PATH: the script's ONLY recursive removal. PATH
# must be a resolved path of its own (--runtime: one of the fixed runtime
# directories under /run that systemd and this script manage), with no
# mount point on it or beneath (mountinfo, binds included); then
# rm -rf --one-file-system as a last defence. Returns 1, saying why, when
# it refuses: the caller keeps the path (and its record).
remove_tree() {
  local runtime=0
  if [ "$1" = --runtime ]; then runtime=1; shift; fi
  local path=$1
  # (a dry run shows it all the same)
  if absent "$path" && [ "$DRY" = 0 ]; then return 0; fi
  if [ "$runtime" = 1 ]; then
    case "$path" in
      /run/ameesh-gate|"$CONTROL"/ameesh-guest.slice.d|"$CONTROL"/ameesh-guest-runner@*.service.d) ;;
      *) echo "not removed: $path (not a runtime directory of this script)"; return 1 ;;
    esac
  elif ! purgeable "$path"; then
    echo "not removed: $path (not a path of its own)"; return 1
  fi
  if ! resolved "$path"; then
    echo "kept $path: it goes through a symbolic link now (resolve it, then run again)"
    return 1
  fi
  if mounts_under "$path"; then
    echo "kept $path: a mount point is there or beneath (unmount it, then run again)"
    return 1
  fi
  run rm -rf --one-file-system "$path"
}

# purgeable PATH: a directory of its own, two levels deep at least, never
# a top directory (/home, /tmp, /work…), a user's home nor a system tree
purgeable() {
  case "$1" in /?*/?*) ;; *) return 1 ;; esac
  case "$1" in *[!A-Za-z0-9/._-]*|*/../*|*/..|*/./*|*/.|*//*|*/) return 1 ;; esac
  case "$1" in
    /home/*/*|/root/*/*) return 0 ;;
    /home/*|/root/*|/proc/*|/sys/*|/dev/*|/run/*|/etc/*|/usr/*|/boot/*|/bin/*|/sbin/*|/lib*|/var/lib|/var/log) return 1 ;;
  esac
}

# the runtime properties `systemctl set-property --runtime` leaves
CONTROL=/run/systemd/system.control

# reset_caps: the caps an enforcing guard set (slice and runners, kept by
# systemd after the guard stops) give way to the units' own: removed, then
# the units' configured values applied again at once (a daemon-reload alone
# does not promise to write the cgroup back). The guard is stopped first.
reset_caps() {
  local props u
  say "the guard's caps taken off: the guest's units keep their own"
  remove_tree --runtime "$CONTROL/ameesh-guest.slice.d" || true
  for u in $(runner_units); do remove_tree --runtime "$CONTROL/$u.d" || true; done
  run systemctl daemon-reload
  props=$(grep -E '^(CPUWeight|IOWeight|CPUQuota|MemoryHigh|MemoryMax)=' \
            "$(at "$UNITS")/ameesh-guest.slice" 2>/dev/null | tr '\n' ' ' || true)
  if [ -n "$props" ]; then
    # shellcheck disable=SC2086
    run systemctl set-property --runtime ameesh-guest.slice $props || true
  fi
  for u in $(runner_units); do
    props=$(host_says systemctl show --value --property=MemoryMax "$u" 2>/dev/null || true)
    [ -n "$props" ] || props="<its unit's>"
    run systemctl set-property --runtime "$u" "MemoryMax=$props" || true
  done
  # the values written again are the units' own: their drop-ins go too
  remove_tree --runtime "$CONTROL/ameesh-guest.slice.d" || true
  for u in $(runner_units); do remove_tree --runtime "$CONTROL/$u.d" || true; done
  run systemctl daemon-reload
}

# the installed guard's mode (the unit's default: observe)
installed_mode() {
  local env; env=$(at "$ETC/guard.env")
  if [ -f "$env" ] && ! grep -q '^AMEESH_GUARD_ARGS=--observe' "$env"; then
    echo enforce
  else
    echo observe
  fi
}

# guest runners known to systemd (loaded or enabled)
runner_units() {
  { host_says systemctl list-units --all --plain --no-legend 'ameesh-guest-runner@*.service' \
      2>/dev/null | awk '{print $1}'
    ls "$(at "$UNITS")"/multi-user.target.wants/ 2>/dev/null | grep '^ameesh-guest-runner@.*\.service$' || true
  } | sort -u
}

set_guard_mode() {
  local args=""
  [ "$1" = observe ] && args=--observe
  put "$ETC/guard.env" 0644 root:root <<EOF
# ameesh guard mode (install-pilot.sh --set-guard-mode observe|enforce)
AMEESH_GUARD_ARGS=$args
EOF
}

# --------------------------------------------------------------------------
# --check: what is in place (read only)
# --------------------------------------------------------------------------
if [ "$MODE" = check ]; then
  status=0
  ok() { echo "ok   $*"; }
  ko() { echo "FAIL $*"; status=1; }
  # (read only: a dry run reads the same, in a test's host if one is given)
  if user_exists; then
    groups=$(host_says id -nG "$GUEST" || true)
    ok "user $GUEST ($groups)"
    for g in sudo wheel admin docker adm lxd libvirt; do
      if printf '%s\n' $groups | grep -qx "$g"; then ko "$GUEST is in group $g"; fi
    done
  else ko "no user $GUEST"; fi
  for f in ameesh-guest.slice ameesh.slice.d/guest-weight.conf ameesh-guest-runner@.service \
           ameesh-guard.service; do
    [ -f "$(at "$UNITS/$f")" ] && ok "$UNITS/$f" || ko "missing $UNITS/$f"
  done
  [ -x "$(at "$VENV")/bin/ameesh" ] \
    && ok "$VENV ($("$(at "$VENV")/bin/ameesh" --version 2>/dev/null || echo '?'))" \
    || ko "missing $VENV/bin/ameesh"
  [ -d "$(at /run/ameesh-gate/ack)" ] && ok "/run/ameesh-gate/ack" || ko "missing /run/ameesh-gate/ack"
  if host_says systemctl is-active --quiet ameesh-guard; then
    ok "ameesh-guard active ($(grep -h '^AMEESH_GUARD_ARGS=' "$ETC/guard.env" 2>/dev/null || echo 'mode ?'))"
  else ko "ameesh-guard not active"; fi
  python3 - "$(at /run/ameesh-gate/state/state.json)" <<'EOF' || status=1
import json, sys, time
try:
    s = json.load(open(sys.argv[1]))
except Exception as exc:
    print("FAIL gate state unreadable: %s" % exc); sys.exit(1)
fresh = (s.get("expires_ts") or 0) > time.time()
print("%s gate state %s, level %s, seq %s%s" % ("ok  " if fresh else "FAIL", s.get("state"),
      s.get("level"), s.get("seq"), "" if fresh else " (stale)"))
sys.exit(0 if fresh else 1)
EOF
  for f in "$(at "$ETC")/guest.env" "$(at "$ETC")"/guest-*.env; do
    [ -e "$f" ] || continue
    perm=$(stat -c '%a %U:%G' "$f")
    f=${f#"$HOSTDIR"}
    [ "$perm" = "640 root:$GUEST" ] && ok "$f ($perm)" || ko "$f is $perm, expected 640 root:$GUEST"
  done
  grep -q '^AMEESH_DSN=.' "$(at "$ETC")/guest.env" 2>/dev/null && ok "AMEESH_DSN set in guest.env" \
    || ko "AMEESH_DSN not set in $ETC/guest.env"
  for u in $(runner_units); do
    echo "     $u: $(systemctl is-enabled "$u" 2>/dev/null || true), $(systemctl is-active "$u" 2>/dev/null || true)"
  done
  host_says systemd-analyze verify "$UNITS/ameesh-guard.service" "$UNITS/ameesh-guest.slice" \
    >/dev/null 2>&1 && ok "systemd-analyze verify" || ko "systemd-analyze verify (run it for details)"
  exit $status
fi

# --------------------------------------------------------------------------
# --enable / --disable AGENT…: the guest runners
# --------------------------------------------------------------------------
if [ "$MODE" = enable ] || [ "$MODE" = disable ]; then
  need_root
  [ ${#TARGETS[@]} -gt 0 ] || die "--$MODE needs at least one agent"
  if [ "$MODE" = enable ]; then
    [ "$DRY" = 1 ] || systemctl is-active --quiet ameesh-guard \
      || die "ameesh-guard is not active: a runner would stay stopped (gate fallback)"
    [ "$DRY" = 1 ] || grep -q '^AMEESH_DSN=.' "$ETC/guest.env" \
      || die "AMEESH_DSN is not set in $ETC/guest.env (the host's login role, L56)"
    for a in "${TARGETS[@]}"; do
      [ "$DRY" = 1 ] || [ -f "$ETC/guest-$a.env" ] || die "missing $ETC/guest-$a.env"
      run systemctl enable --now "ameesh-guest-runner@$a.service"
    done
  else
    for a in "${TARGETS[@]}"; do
      run systemctl disable --now "ameesh-guest-runner@$a.service"
    done
  fi
  exit 0
fi

# --------------------------------------------------------------------------
# --set-guard-mode: switch the running guard
# --------------------------------------------------------------------------
if [ "$MODE" = guard-mode ]; then
  need_root
  say "guard mode: $GUARD_MODE"
  set_guard_mode "$GUARD_MODE"
  if [ "$GUARD_MODE" = observe ]; then
    # a stopping guard thaws the guest but leaves its caps: taken off here
    run systemctl stop ameesh-guard.service
    reset_caps
    run systemctl start ameesh-guard.service
  else
    run systemctl restart ameesh-guard.service
  fi
  exit 0
fi

# --------------------------------------------------------------------------
# --uninstall: study §4.3, in its order
# --------------------------------------------------------------------------
if [ "$MODE" = uninstall ]; then
  need_root
  say "1. drain: the guest agents return their leases"
  if host_says systemctl is-active --quiet ameesh-guard 2>/dev/null \
     && [ -x "$(at "$VENV")/bin/ameesh" ]; then
    run env AMEESH_GUARD_CONFIG="$ETC/guard.json" "$VENV/bin/ameesh" guard drain || true
  fi
  say "2. (yours) a canon PR removes the guest agents' placements on $HOST"
  say "3. stop and disable the guest runners (each drains, TimeoutStopSec=180)"
  for u in $(runner_units); do
    run systemctl disable --now "$u" || true
  done
  run systemctl disable --now ameesh-guard.service || true
  run systemctl stop ameesh-guest.slice || true
  run rm -f "$UNITS/ameesh-guest-runner@.service" "$UNITS/ameesh-guard.service" \
            "$UNITS/ameesh-guest.slice" "$UNITS/ameesh.slice.d/guest-weight.conf" "$PATH_DROPIN"
  run rmdir --ignore-fail-on-non-empty "$UNITS/ameesh.slice.d" "$(dirname "$PATH_DROPIN")" || true
  run rm -f "$TMPFILES"
  remove_tree --runtime /run/ameesh-gate || true
  # the guard's runtime caps (systemd keeps them until the reboot)
  remove_tree --runtime "$CONTROL/ameesh-guest.slice.d" || true
  for d in "$(at "$CONTROL")"/ameesh-guest-runner@*.service.d; do
    [ -e "$d" ] || continue
    remove_tree --runtime "${d#"$HOSTDIR"}" || true
  done
  run systemctl daemon-reload
  run rm -f "$ETC/guard.env"
  say "4. (yours) drop the host's Postgres login role (L56)"
  if [ "$PURGE" = 1 ]; then
    say "5. purge: secrets, offer, and what this script created"
    run rm -f "$ETC/guard.json"
    offload=0
    if [ -f "$(at /etc/ameesh-guest/offload.json)" ]; then
      offload=1
      echo "kept user $GUEST and the guest's directories: the offload (install.sh, L159b) uses them"
    fi
    # only the paths recorded (absolute, as created), the last created first;
    # never the --root of this command line. What stays (a mount, the
    # offload, a non-empty directory) keeps its record: a later purge can
    # still remove it.
    kept=()
    if [ -f "$(at "$STATE")" ]; then
      while read -r kind path; do
        case "$kind" in
          secret)
            case "$path" in
              "$ETC/guest.env"|"$ETC"/guest-*.env) run rm -f "$path" ;;
              *) echo "not removed: $path (recorded, but not a secrets file of this script)" ;;
            esac
            continue ;;
          guest-tree|venv-tree|guest-dir|venv-dir) ;;
          *) continue ;;
        esac
        if absent "$path"; then
          continue
        elif [ "${kind%%-*}" = guest ] && [ "$offload" = 1 ]; then
          kept+=("$kind $path")
        elif ! purgeable "$path"; then
          echo "not removed: $path (recorded, but not a path of its own)"
        elif [ "${kind#*-}" = tree ]; then
          remove_tree "$path" || kept+=("$kind $path")
        elif ! resolved "$path" || mounts_under "$path"; then
          echo "kept $path: a symbolic link or a mount point there (then --purge again)"
          kept+=("$kind $path")
        else
          run rmdir --ignore-fail-on-non-empty "$path"
          # a directory left non-empty keeps its record
          [ "$DRY" = 1 ] || absent "$path" || kept+=("$kind $path")
        fi
      done < <(tac "$(at "$STATE")")
    fi
    if created user; then
      if [ "$offload" = 1 ]; then
        kept+=("user")
      elif ! run userdel "$GUEST"; then
        # processes left, for instance: kept, a later purge tries again
        echo "kept user $GUEST: userdel failed (its processes?), --purge again later"
        kept+=("user")
      fi
    fi
    if [ ${#kept[@]} -gt 0 ]; then
      echo "kept in $STATE: ${kept[*]}"
      if [ "$DRY" = 0 ]; then
        printf '%s\n' "${kept[@]}" | tac > "$STATE.tmp" && mv -f "$STATE.tmp" "$STATE"
      fi
    else
      run rm -f "$STATE"
      run rmdir --ignore-fail-on-non-empty "$ETC" || true
    fi
  else
    echo "kept: secrets and offer under $ETC, $VENV, user $GUEST, $ROOT (--purge removes what this script created)"
  fi
  exit 0
fi

# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
need_root
for f in ameesh-guest.slice ameesh.slice.d/guest-weight.conf ameesh-guest-runner@.service \
         ameesh-guard.service ameesh-gate.tmpfiles.conf; do
  [ -f "$HERE/$f" ] || die "missing $HERE/$f (run from a checkout of ameesh)"
done
[ -f "$CHECKOUT/pyproject.toml" ] || die "missing $CHECKOUT/pyproject.toml"
for cmd in python3 systemctl useradd systemd-tmpfiles; do
  command -v "$cmd" >/dev/null || [ "$DRY" = 1 ] || die "missing $cmd"
done
[ -f /sys/fs/cgroup/cgroup.controllers ] || [ "$DRY" = 1 ] || die "cgroups v2 required"
if is_mount "$ROOT"; then ROOT_MOUNTED=1; else ROOT_MOUNTED=0; fi
if [ "$ALLOW_ROOTFS" = 0 ] && [ "$ROOT_MOUNTED" = 0 ]; then
  [ "$DRY" = 1 ] && echo "warning: $ROOT is not a mount point (--allow-rootfs needed for real)" \
    || die "$ROOT is not a mount point: attach a volume (study §4.1), or --allow-rootfs"
fi

# the offer: the canon's policy.lend, validated by the contract's own reader
if [ -n "$LEND" ]; then
  lend_json=$(cat "$LEND")
else
  lend_json='{"cpu_max": 2, "cpu_weight": 10, "memory_max": "9GiB", "agent_memory_max": "4.5GiB",
   "disk_max": "40GiB",
   "reclaim": {"memory_available_min": "6GiB", "cpu_pressure_max": "20%",
               "memory_pressure_max": "2%", "io_pressure_max": "20%",
               "disk_free_min": "10GiB", "disk_paths": ["/"]},
   "drain_seconds": 120, "calm_seconds": 600}'
fi
guard_json=$(python3 -I -c '
import json, os, sys
sys.path.insert(0, sys.argv[4])
from ameesh import capacity_guard
lend = json.loads(sys.argv[1])
warnings = []
offer, problems, unknown = capacity_guard.parse_lend(lend, warnings)
for warning in warnings:
    print("warning: " + warning, file=sys.stderr)
# mesh-design #459 r4c: the family "main accepted it" — a refused
# `disk_paths` (relative, `..`, NUL) and a size the previous reader truncated
# to <= 0 (`0`, `-0`, `"0.5B"`) — drops the offer; the guard must still start
# closed and lend nothing, and `canon check` reports the fiche. The
# classification reads the codes of `parse_lend`, never a substring of a
# message that quotes the value the operator wrote. Any other problem refuses
# the installation.
if problems and not capacity_guard.tolerated_only(problems):
    sys.exit("invalid offer: " + "; ".join(problems))
if problems:
    print("warning: offer dropped (the guard starts closed and lends nothing): "
          + "; ".join(problems), file=sys.stderr)
    lend, offer = None, None
if offer is not None:
    # disk_max: only the size of the guest volume holds it (no quota); a bigger
    # volume is refused, unless accepted (argv[6]); a dry run says so (argv[7]);
    # a mount point, binds included, read in mountinfo by the shell (argv[8]);
    # the filesystem is read at argv[9] (the root, under PILOT_TEST_HOST in a test)
    root, disk_max = sys.argv[5], offer["disk_max"]
    if sys.argv[8] == "1":
        # the filesystem under it (a dry run may name a root not made yet)
        probe = sys.argv[9]
        while not os.path.exists(probe):
            probe = os.path.dirname(probe)
        st = os.statvfs(probe)
        size = st.f_blocks * st.f_frsize
        if size > disk_max * 1.05 and sys.argv[6] != "1":
            msg = ("%s holds %.0f GiB, above disk_max %.0f GiB: nothing would hold disk_max "
                   "(a volume of that size, a bigger disk_max, or --accept-larger-volume)"
                   % (root, size / 2 ** 30, disk_max / 2 ** 30))
            if sys.argv[7] != "1":
                sys.exit("refused: " + msg)
            print("warning: refused for real: " + msg, file=sys.stderr)
        elif size > disk_max * 1.05:
            print("warning: %s is bigger than disk_max (accepted): disk_max is not enforced" % root,
                  file=sys.stderr)
    else:
        print("warning: %s is not a volume of its own: disk_max is indicative only" % root,
              file=sys.stderr)
print(json.dumps({"host": sys.argv[2], "max_agents": int(sys.argv[3]),
                  "work_path": sys.argv[5], "lend": lend}, indent=2))
' "$lend_json" "$HOST" "$MAX_AGENTS" "$CHECKOUT/src" "$ROOT" "$ACCEPT_LARGER" "$DRY" "$ROOT_MOUNTED" \
  "$(at "$ROOT")") \
  || die "offer or volume refused"

# PATH of the runners: the directory of the DeepSeek harness (`dsh`)
if [ ${#EXTRA_PATH[@]} -eq 0 ] && dsh=$(command -v dsh 2>/dev/null); then
  case "$(dirname "$dsh")" in /usr/bin|/usr/local/bin|/bin) ;; *) EXTRA_PATH=("$(dirname "$dsh")") ;; esac
fi

# the guard's mode: the one asked, else the one installed (a reinstall never
# switches an enforcing guard back to observe behind the owner's back)
PREVIOUS_MODE=$(installed_mode)
GUARD_MODE=${GUARD_MODE:-$PREVIOUS_MODE}

say "1. user $GUEST and its volume $ROOT"
if user_exists; then
  echo "user $GUEST exists: kept as is"
else
  remember user
  run useradd --system --user-group --home-dir "$ROOT/home" --no-create-home \
              --shell /bin/bash --comment "ameesh guest (L159g)" "$GUEST"
fi
# each directory recorded on its own, only if this script creates it: a
# directory that was there (and its content) is never purged
# (anything already there that is not a plain directory: refused, before
# any record, never taken for absent)
for d in "$ROOT" "$ROOT/home" "$ROOT/tmp" "$ROOT/work"; do
  if not_a_dir "$d"; then
    die "$d exists and is not a directory: move it away first"
  fi
done
absent "$ROOT" && remember "guest-dir $ROOT"
run install -d -m 0755 "$ROOT"
for d in home tmp work; do
  absent "$ROOT/$d" && remember "guest-tree $ROOT/$d"
  run install -d -m 0700 -o "$GUEST" -g "$GUEST" "$ROOT/$d"
done

say "2. ameesh in $VENV (root-owned; the guest can run it, not change it)"
for d in "$(dirname "$VENV")" "$VENV"; do
  if not_a_dir "$d"; then
    die "$d exists and is not a directory: move it away first"
  fi
done
if [ -x "$(at "$VENV")/bin/ameesh" ] && ! created "venv-tree $VENV"; then
  echo "$VENV exists and was not made by this script: upgraded in place"
else
  make_venv=0
  if absent "$VENV"; then
    make_venv=1
  elif [ ! -x "$(at "$VENV")/bin/pip" ]; then
    # a venv this script began and did not finish: made again
    created "venv-tree $VENV" || die "$VENV exists without bin/pip and is not this script's"
    remove_tree "$VENV" || die "$VENV not made again: see above"
    make_venv=1
  fi
  if [ "$make_venv" = 1 ]; then
    absent "$(dirname "$VENV")" && remember "venv-dir $(dirname "$VENV")"
    remember "venv-tree $VENV"
    run python3 -m venv "$VENV" || {
      remove_tree "$VENV" || true
      die "python3 -m venv failed (python3-venv, ensurepip?): $VENV removed unless said above"
    }
  fi
fi
run "$VENV/bin/pip" install --quiet --upgrade "$CHECKOUT[psycopg]"

say "3. units, gate directories, guard"
run install -m 0644 "$HERE/ameesh-guest.slice" "$UNITS/"
run install -D -m 0644 "$HERE/ameesh.slice.d/guest-weight.conf" "$UNITS/ameesh.slice.d/guest-weight.conf"
# the runner's unit, its volume being $ROOT (WorkingDirectory, HOME, TMPDIR,
# PATH, ReadWritePaths, RequiresMountsFor)
sed "s#/srv/ameesh-guest#$ROOT#g" "$HERE/ameesh-guest-runner@.service" \
  | put "$UNITS/ameesh-guest-runner@.service" 0644 root:root
run install -m 0644 "$HERE/ameesh-guard.service" "$UNITS/"
run install -m 0644 "$HERE/ameesh-gate.tmpfiles.conf" "$TMPFILES"
if [ ${#EXTRA_PATH[@]} -gt 0 ]; then
  prefix=$(IFS=:; echo "${EXTRA_PATH[*]}")
  put "$PATH_DROPIN" 0644 root:root <<EOF
# install-pilot.sh: the harnesses' directories first (dsh)
[Service]
Environment=PATH=$prefix:$ROOT/home/.local/share/mise/shims:$ROOT/home/.local/bin:/usr/local/bin:/usr/bin:/bin
EOF
fi
put "$ETC/guard.json" 0644 root:root <<<"$guard_json"
set_guard_mode "$GUARD_MODE"

say "4. secrets files (created empty, never overwritten)"
secret() {
  local path=$1
  if ! absent "$path"; then echo "kept $path"; return; fi
  remember "secret $path"
  put "$path" 0640 "root:$GUEST"
}
secret "$ETC/guest.env" <<'EOF'
# ameesh guest runners: the host's login role (L56), read by every guest agent
# AMEESH_DSN=postgresql://<role>@<mesh>/<db>?sslmode=…
EOF
for a in "${AGENTS[@]}"; do
  secret "$ETC/guest-$a.env" <<EOF
# ameesh guest runner of $a: its own secrets (harness key, forge token)
# DEEPSEEK_API_KEY=…
# GH_TOKEN=…
EOF
done

run systemctl daemon-reload
run systemd-tmpfiles --create "$TMPFILES"
# one measure with the installed ameesh and offer, nothing written
run "$VENV/bin/ameesh" guard check --config "$ETC/guard.json"
# a running guard is started again: it takes the new offer and mode
run systemctl stop ameesh-guard.service
if [ "$PREVIOUS_MODE" = enforce ] && [ "$GUARD_MODE" = observe ]; then
  reset_caps
fi
run systemctl enable --now ameesh-guard.service

say "done: the guard runs in $GUARD_MODE; no guest runner is started"
cat <<EOF
Next:
  1. fill $ETC/guest.env (AMEESH_DSN) and $ETC/guest-<agent>.env;
  2. $0 --check;
  3. after the canon PR (Host policy.lend, placements of ${AGENTS[*]} on $HOST):
     $0 --enable ${AGENTS[*]}
  4. after a day of observation: $0 --set-guard-mode enforce
Rollback: $0 --uninstall [--purge]
EOF
