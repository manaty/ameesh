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
#   sudo deploy/guest/install-pilot.sh --offload-only [options]  the guard alone,
#                        for install.sh --gate file (b1, PILOT-ci-vm.md)
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
#   --enable AGENT…      prepare the workspace of these agents (below), then
#                        enable and start their runners
#   --disable AGENT…     stop and disable them (their turn drains first)
#   --canon DIR          with --enable (and --check): a checkout of the canon,
#                        read at its merged branch by ameesh's own reader, as
#                        the checkout's owner: each agent's project and work
#                        folder on --host (under $ROOT/work, refused elsewhere)
#   --repo P=URL#BRANCH  with --enable: the forge of project P, never from the
#                        canon (repeatable): https without
#                        user or token in it, e.g.
#                        P=https://github.com/ORG/REPO.git#BRANCH
#   --dsh-from DIR       with --enable: the ~/.dsh whose hooks.json and
#                        profiles/ the guest gets (default: the sudo user's)
#   --check              report the state of everything installed
#   --offload-only       mode b1 only (L159h, CI VM): the guard, its gate
#                        and the guest's slice, without any guest runner,
#                        agent or secrets file (no --agent, --path, --enable,
#                        --disable); the guard admits no agent (max_agents 0)
#                        and install.sh --gate file then obeys its gate.
#                        Also with --check, which then expects no secrets
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
#     --purge removes only those this script created);
#   * by --enable, once (lot #483): /opt/ameesh/git-credential-guest, the
#     git credential helper of the guest (it hands git the runner's GH_TOKEN,
#     which systemd reads in guest-<agent>.env: never written elsewhere,
#     never printed), the only helper of each clone (its .git/config: the
#     helpers before reset); the guest's ~/.dsh/hooks.json and profiles/,
#     from --dsh-from: read once without following a link, text files of
#     the kinds a profile needs only, refused if one looks like it holds a
#     secret, and what is checked is what is copied;
#     and for each agent, its clone of its project in its work folder,
#     detached on origin/BRANCH (the token handed to git through a file of
#     the guest's own, 0600, removed at exit: in no environment). What is there already is kept (never
#     fetched, reset nor overwritten); a folder holding something else is
#     refused. Everything in the guest's home and work is written as the
#     guest, never by root.
# Not done here (study §4.2, §4.3): the canon PR (Host `policy.lend`, the
# guest agents' placements), the host's Postgres login role (L56).
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
OFFLOAD_ONLY=0
MAX_AGENTS_GIVEN=0
CANON=""
declare -A REPOS=()
DSH_FROM=""
HELPER=/opt/ameesh/git-credential-guest

die() { echo "install-pilot.sh: $*" >&2; exit 1; }
say() { echo "== $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host|--agent|--max-agents|--lend|--root|--path|--guard-mode|--set-guard-mode|\
    --canon|--repo|--dsh-from)
      [ $# -ge 2 ] || die "$1 needs a value" ;;
  esac
  case "$1" in
    --host) HOST=$2; shift 2 ;;
    --agent) AGENTS+=("$2"); shift 2 ;;
    --max-agents) MAX_AGENTS=$2; MAX_AGENTS_GIVEN=1; shift 2 ;;
    --lend) LEND=$2; shift 2 ;;
    --root) ROOT=$2; shift 2 ;;
    --path) EXTRA_PATH+=("$2"); shift 2 ;;
    --guard-mode) GUARD_MODE=$2; shift 2 ;;
    --set-guard-mode) MODE=guard-mode; GUARD_MODE=$2; shift 2 ;;
    --allow-rootfs) ALLOW_ROOTFS=1; shift ;;
    --accept-larger-volume) ACCEPT_LARGER=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --canon) CANON=$2; shift 2 ;;
    --repo)
      [[ "$2" == ?*=?*'#'?* ]] || die "--repo PROJECT=URL#BRANCH, not $2"
      REPOS[${2%%=*}]=${2#*=}; shift 2 ;;
    --dsh-from) DSH_FROM=$2; shift 2 ;;
    --check) MODE=check; shift ;;
    --offload-only) OFFLOAD_ONLY=1; shift ;;
    # the agents: every argument up to the next option
    --enable|--disable)
      MODE=${1#--}; shift
      while [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; do TARGETS+=("$1"); shift; done ;;
    --uninstall) MODE=uninstall; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '3,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done
if [ "$OFFLOAD_ONLY" = 1 ]; then
  # b1 (L159h): offloaded jobs carry no secret; no agent is ever a guest here
  [ ${#AGENTS[@]} = 0 ] && [ ${#EXTRA_PATH[@]} = 0 ] || die "--offload-only: no --agent nor --path"
  case "$MODE" in enable|disable) die "--offload-only: no guest runner to --$MODE" ;; esac
  [ "$MAX_AGENTS_GIVEN" = 0 ] || [ "$MAX_AGENTS" = 0 ] || die "--offload-only: the guard admits no agent (max_agents 0)"
  MAX_AGENTS=0
else
  [ ${#AGENTS[@]} -gt 0 ] || AGENTS=(deepseek6 deepseek1)
fi

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
[ ${#REPOS[@]} = 0 ] && [ -z "$DSH_FROM" ] || [ "$MODE" = enable ] \
  || die "--repo and --dsh-from go with --enable"
[ -z "$CANON" ] || [ "$MODE" = enable ] || [ "$MODE" = check ] \
  || die "--canon goes with --enable or --check"
[ "$MODE" != enable ] || [ -n "$CANON" ] \
  || die "--enable needs --canon DIR: each agent's project and work folder come from it"
for d in "$CANON" "$DSH_FROM"; do
  case "$d" in ''|/?*) ;; *) die "$d: an absolute path expected" ;; esac
done
# a project's forge: https, a host and a path, no user, port, query nor
# token in it (git would keep it in .git/config); a plain branch name
for p in "${!REPOS[@]}"; do
  valid_name "$p" || die "--repo: invalid project $p"
  url=${REPOS[$p]%%#*} branch=${REPOS[$p]#*#}
  [[ "$url" =~ ^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._-]+)+/?$ ]] && [[ "$url" != *..* ]] \
    || die "--repo $p: $url is not an https URL without user nor token"
  [[ "$branch" =~ ^[A-Za-z0-9_][A-Za-z0-9._/-]*$ ]] && [[ "$branch" != *..* ]] \
    && [[ "$branch" != */ ]] && [[ "$branch" != *.lock ]] || die "--repo $p: invalid branch $branch"
done
# the tests' dry runs read a state of their own; a real run reads only $STATE
[ "$DRY" = 1 ] && [ -n "${PILOT_TEST_STATE:-}" ] && STATE=$PILOT_TEST_STATE
[ "$DRY" = 1 ] && [ -n "${PILOT_TEST_VENV:-}" ] && VENV=$PILOT_TEST_VENV
# (and a canon and a ~/.dsh of their own, whatever the runbook names)
[ "$DRY" = 1 ] && [ -n "$CANON" ] && [ -n "${PILOT_TEST_CANON:-}" ] && CANON=$PILOT_TEST_CANON
[ "$DRY" = 1 ] && [ -n "$DSH_FROM" ] && [ -n "${PILOT_TEST_DSH:-}" ] && DSH_FROM=$PILOT_TEST_DSH
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
# the guest's uid (nothing when there is no such user)
guest_uid() {
  if [ -n "$HOSTDIR" ]; then awk -F: -v u="$GUEST" '$1 == u { print $3 }' "$HOSTDIR/etc/passwd"
  else id -u "$GUEST" 2>/dev/null || true; fi
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
# the guests' workspace (lot #483): clone, git credential, dsh profile
# --------------------------------------------------------------------------
GHOME=$ROOT/home

# as_guest CMD…: runs CMD as the guest (its HOME, its rights), never as
# root in a directory the guest can change; from / (root's directory may be
# unreadable to the guest, and git needs its own); printed under --dry-run
as_guest() { (cd / && run runuser -u "$GUEST" -- "$@"); }

# canon_workspaces AGENT…: "agent<TAB>project<TAB>folder" for each, read in
# the canon (--canon) by ameesh's own reader at the canon's merged branch,
# as the checkout's owner (root would read a git repository it does not
# own); refused: an agent unknown, without a project or not placed on
# --host, a folder that is not one directory of its own under $ROOT/work
canon_workspaces() {
  local as=()
  if [ "$(id -u)" = 0 ]; then
    as=(runuser -u "$(stat -c %U -- "$CANON")" --)
  fi
  "${as[@]}" python3 -I - "$CHECKOUT/src" "$CANON" "$HOST" "$ROOT/work" "$@" <<'EOF'
import os, re, sys
sys.path.insert(0, sys.argv[1])
from ameesh import canon
root, host_name, work = sys.argv[2], sys.argv[3], sys.argv[4]
c = canon.load(root)
if not c.readable:
    sys.exit("canon %s unreadable: %s" % (root, "; ".join(
        f.message for f in c.load_findings if f.severity == canon.ERROR) or "?"))
host = canon.find_host([c], host_name)
if host is None:
    sys.exit("host %s is not in the canon %s" % (host_name, root))
bad = []
for name in sys.argv[5:]:
    agent = next((a for a in c.agents if a.title == name), None)
    if agent is None:
        bad.append("%s: no such agent in the canon" % name); continue
    if not any(p.agent == name and (host_name in p.host_names()
                                   or set(p.tags()) & set(host.tags or []))
               for p in c.placements):
        bad.append("%s: not placed on %s in the canon (its PR first)" % (name, host_name)); continue
    projects = agent.effective_projects
    if not projects:
        bad.append("%s: no project in the canon" % name); continue
    folder = host.policy.work_dir(projects[0], name)
    if not folder:
        bad.append("%s: %s gives it no work folder (policy.work_dirs)" % (name, host_name)); continue
    if (os.path.dirname(folder) != work
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", os.path.basename(folder))):
        bad.append("%s: work folder %s is not a directory of its own in %s"
                   % (name, folder, work)); continue
    print("%s\t%s\t%s" % (name, projects[0], folder))
if bad:
    sys.exit("\n".join(bad))
EOF
}

# quiet / unquiet: xtrace off around a secret, then as it was (under
# bash -x, an assignment or an argument would go to stderr: codex #457 r1)
quiet() { XTRACE=${-//[^x]/}; set +x; }
unquiet() { [ -z "${XTRACE:-}" ] || set -x; }
# the GH_TOKEN of AGENT, from its secrets file (into a variable only: never
# printed, never in a command line, never traced)
token_of() {
  { quiet; } 2>/dev/null
  local v
  v=$(sed -n 's/^[[:space:]]*GH_TOKEN=//p' "$(at "$ETC/guest-$1.env")" 2>/dev/null | tail -n 1)
  v=${v%\"}; v=${v#\"}; v=${v%\'}; v=${v#\'}
  printf '%s' "$v"
  unquiet
}
has_token() {
  { quiet; } 2>/dev/null
  local rc=0
  [ -n "$(token_of "$1")" ] || rc=1
  unquiet
  return $rc
}

# origin_of DIR: the origin of the clone in DIR, read in its .git/config
# (by root, a file the guest owns: read, never run; includes not followed)
origin_of() {
  local cfg; cfg=$(at "$1/.git/config")
  [ -f "$cfg" ] && [ ! -L "$cfg" ] && [ ! -L "$(at "$1")" ] || return 1
  git -C / config --file "$cfg" --no-includes --get remote.origin.url
}
# same_repo URL URL: one repository (a trailing .git or / aside)
same_repo() {
  local a=${1%/} b=${2%/}
  a=${a%.git}; b=${b%.git}
  [ "${a,,}" = "${b,,}" ]
}

# the git credential helper of the guest: git asks it, it answers with the
# runner's GH_TOKEN for https://github.com only
helper_text() {
  cat <<'EOF'
#!/bin/sh
# install-pilot.sh (L159g, #483): git credential helper of the guest runners.
# The token is the runner's GH_TOKEN, which systemd reads in
# /etc/ameesh/guest-<agent>.env: handed to git, never written, never printed.
[ "$1" = get ] || exit 0
[ -n "${GH_TOKEN:-}" ] || exit 0
protocol= host=
while IFS= read -r line && [ -n "$line" ]; do
  case "$line" in
    protocol=*) protocol=${line#protocol=} ;;
    host=*) host=${line#host=} ;;
  esac
done
[ "$protocol" = https ] && [ "$host" = github.com ] || exit 0
printf 'username=x-access-token\npassword=%s\n' "$GH_TOKEN"
EOF
}

# dsh_read list SRC / dsh_read tar SRC FILE… / dsh_read check DIR
# A dsh profile read by root once, never through a link (O_NOFOLLOW, each
# directory opened from its parent, from / down: codex #457 r3), never
# blocked by a FIFO (O_NONBLOCK, a plain file only): hooks.json and, in each profiles/<name>/,
# its files of the kinds a profile needs (DSH_KINDS), text only (UTF-8, no
# NUL, 1 MiB at most). A link, another kind of file, a binary or a file
# that looks like it holds a secret refuses it all (dot files and
# subdirectories of a profile are left: sessions, logs, modules).
# `list` prints what would be copied; `tar` reads it all again, checks it
# again and writes those FILEs, from the very bytes it checked, as a tar on
# stdout (what is checked is what is copied: codex #457 r2); `check` names
# what the same rules refuse in DIR, one per line (--check: codex #457 r3).
DSH_KINDS='json yml yaml md txt toml'
dsh_read() {
  python3 -I - "$DSH_KINDS" "${PILOT_TEST_STAGE_HOOK:-}" "${PILOT_TEST_ROOT_HOOK:-}" "$DRY" "$@" <<'EOF'
import errno, fcntl, io, os, re, stat, subprocess, sys, tarfile, time
kinds, hook, root_hook = sys.argv[1].split(), sys.argv[2], sys.argv[3]
dry, mode, args = sys.argv[4] == "1", sys.argv[5], sys.argv[6:]
SECRET = re.compile(
    rb"sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    rb"|-----BEGIN ([A-Z0-9]+ )*PRIVATE KEY|PuTTY-User-Key-File"
    rb"""|(api[_-]?key|token|secret|passw(or)?d)["']?\s*[:=]\s*["']?[^"'\s,}]{8,}""", re.I)
KEYNAME = re.compile(r"id_.*|.*\.(pem|key|p12|pfx|jks|keystore|der)", re.I)
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
MAX = 1 << 20

def secret(name, data):
    return bool(KEYNAME.fullmatch(name.rsplit("/", 1)[-1]) or SECRET.search(data))

src, wanted = args[0], args[1:]
bad, files = [], {}
NOFOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY

def opened(name, dir_fd, directory):
    try:
        return os.open(name, NOFOLLOW | (os.O_DIRECTORY if directory else 0), dir_fd=dir_fd)
    except OSError as exc:
        bad.append("%s: %s" % (name, "a link" if exc.errno in (errno.ELOOP, errno.ENOTDIR)
                               else exc.strerror))

def walk(path):
    """PATH opened from / down, one directory at a time, never through a
    link: no absolute path opened again after it was checked."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    for part in [p for p in path.split("/") if p]:
        try:
            nfd = os.open(part, NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
        except OSError as exc:
            bad.append("%s: %s" % (path, "goes through a symbolic link"
                                   if exc.errno in (errno.ELOOP, errno.ENOTDIR) else exc.strerror))
            os.close(fd)
            return None
        os.close(fd)
        fd = nfd
    return fd

def read(rel, name, dir_fd):
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        bad.append("%s: %s" % (rel, exc.strerror)); return
    if not stat.S_ISREG(st.st_mode):
        bad.append("%s: not a plain file" % rel); return
    fd = opened(name, dir_fd, False)
    if fd is None:
        return
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            bad.append("%s: not a plain file" % rel); return
        fcntl.fcntl(fh.fileno(), fcntl.F_SETFL,
                    fcntl.fcntl(fh.fileno(), fcntl.F_GETFL) & ~os.O_NONBLOCK)
        data = fh.read(MAX + 1)
    if len(data) > MAX:
        bad.append("%s: over 1 MiB" % rel); return
    try:
        if b"\0" in data:
            raise ValueError
        data.decode("utf-8")
    except ValueError:
        bad.append("%s: not text (UTF-8)" % rel); return
    files[rel] = data

if mode != "check" and (not os.path.isabs(src)
                        or os.path.realpath(src) != os.path.normpath(src)):
    sys.exit("install-pilot.sh: --dsh-from %s goes through a symbolic link: give %s"
             % (src, os.path.realpath(src)))
if root_hook and dry and mode == "tar":
    subprocess.run(root_hook, shell=True, check=True)   # the tests' swap
root = walk(src)
if root is None:
    if mode == "check":
        print("\n".join(bad)); sys.exit(0)
    sys.exit("install-pilot.sh: %s: not copied: %s" % (src, "; ".join(bad)))
read("hooks.json", "hooks.json", root)
prof = opened("profiles", root, True)
if prof is not None:
    names = sorted(os.listdir(prof))
    if hook and dry and mode == "tar":
        subprocess.run(hook, shell=True, check=True)   # the tests' swap
    for p in names:
        if p.startswith("."):
            continue
        st = os.stat(p, dir_fd=prof, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            bad.append("profiles/%s: a link" % p); continue
        if not stat.S_ISDIR(st.st_mode) or not NAME.fullmatch(p):
            bad.append("profiles/%s: not a profile's directory" % p); continue
        pfd = opened(p, prof, True)
        if pfd is None:
            continue
        for f in sorted(os.listdir(pfd)):
            rel = "profiles/%s/%s" % (p, f)
            if f.startswith("."):
                continue
            st = os.stat(f, dir_fd=pfd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                continue
            if not (stat.S_ISREG(st.st_mode) and NAME.fullmatch(f)
                    and "." in f and f.rsplit(".", 1)[1].lower() in kinds):
                bad.append("%s: not a file a profile needs (%s)" % (rel, ", ".join(kinds))); continue
            read(rel, f, pfd)
        os.close(pfd)
    os.close(prof)
os.close(root)
secrets = [rel for rel, data in sorted(files.items()) if secret(rel, data)]
if mode == "check":
    print("\n".join(bad + ["%s looks like it holds a secret" % rel for rel in secrets]))
    sys.exit(0)
if bad:
    sys.exit("install-pilot.sh: %s: not copied:\n  %s" % (src, "\n  ".join(bad)))
if secrets:
    sys.exit("install-pilot.sh: not copied, they look like they hold a secret: %s" % " ".join(secrets))
if "hooks.json" not in files or len(files) < 2:
    sys.exit("install-pilot.sh: %s: no hooks.json, or no profile in profiles/" % src)
if mode == "list":
    print("\n".join(["hooks.json"] + sorted(r for r in files if r != "hooks.json")))
    sys.exit(0)
missing = [w for w in wanted if w not in files]
if missing:
    sys.exit("install-pilot.sh: %s changed while read: %s" % (src, " ".join(missing)))
out = io.BytesIO()
with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
    for rel in wanted:
        info = tarfile.TarInfo(rel)
        info.size, info.mode, info.mtime = len(files[rel]), 0o644, int(time.time())
        tar.addfile(info, io.BytesIO(files[rel]))
sys.stdout.buffer.write(out.getvalue())
EOF
}

# git_cred AGENT CMD…: git as the guest, with an inline helper alone (the
# helpers of every config file reset on the command line, read after them
# all; the config of the environment unset). The helper reads AGENT's
# GH_TOKEN itself, as the guest, in /etc/ameesh/guest-AGENT.env (0640
# root:ameesh-guest: the guest's to read, as its runner does), on each
# request, for https://github.com only: root hands it nothing (no file, no
# pipe, no descriptor a child of git inherits: codex #457 r4)
git_cred() {
  local a=$1 helper
  shift
  helper=$(printf '!f() { [ "$1" = get ] || return 0; p= h=; while IFS= read -r l && [ -n "$l" ]; do case "$l" in protocol=*) p=${l#protocol=} ;; host=*) h=${l#host=} ;; esac; done; [ "$p" = https ] && [ "$h" = github.com ] || return 0; t=$(sed -n "s/^[[:space:]]*GH_TOKEN=//p" %s | tail -n 1 | sed "s/^[\\"\\x27]//; s/[\\"\\x27]\\$//"); [ -n "$t" ] && printf "username=x-access-token\\npassword=%%s\\n" "$t"; }; f' \
    "$ETC/guest-$a.env")
  # (GIT_TRACE_REDACT unset: a trace, if any, keeps its redaction: mesh-design #457 r5)
  as_guest env -u GIT_CONFIG_COUNT -u GIT_CONFIG_PARAMETERS -u GH_TOKEN -u GIT_TRACE_REDACT \
    GIT_TERMINAL_PROMPT=0 \
    git -c credential.helper= -c "credential.helper=$helper" "$@"
}
# cred_audit DIR before|after: what in the clone in DIR's config, as git
# reads it there for the guest (its home; every scope, worktree too, includes
# followed), is a credential setting not ours, one per line (codex #457 r3).
# Any credential.* but credential.helper is refused (a helper for one URL
# would be asked after our reset). `before` --enable writes the clone's
# .git/config: a credential.helper is only taken there, or in the system's
# or the guest's global file (both read before it, so reset). `after`: the
# helpers in use, after the last reset, must be ours alone.
# Read by the guest itself on a real host (root never follows its includes).
cred_audit() {
  local as=()
  [ -n "$HOSTDIR" ] || [ "$(id -u)" != 0 ] || as=(runuser -u "$GUEST" --)
  (cd / && "${as[@]}" python3 -I - "$(at "$1")/.git" "$(at "$GHOME")" "$HELPER" "$2" <<'EOF'
import os, subprocess, sys
gd, home, ours, when = sys.argv[1:5]
env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
env.update(HOME=home, XDG_CONFIG_HOME=home + "/.config")
r = subprocess.run(["git", "-c", "safe.directory=*", "--git-dir=" + gd, "config", "-z",
                    "--show-scope", "--show-origin", "--get-regexp", r"^credential\."],
                   capture_output=True, env=env)
if r.returncode not in (0, 1) or (r.returncode == 1 and r.stdout):
    print("unreadable (%s)" % (r.stderr.decode(errors="replace").splitlines() or ["?"])[0])
    sys.exit(0)
items = r.stdout.decode(errors="replace").split("\0")[:-1]
own = ("file:" + gd + "/config", "file:.git/config")
foreign, helpers = [], []
for scope, origin, kv in zip(items[0::3], items[1::3], items[2::3]):
    key, nl, value = kv.partition("\n")
    where = "%s, %s" % (scope, origin)
    if key.lower() != "credential.helper":
        foreign.append("%s (%s): not ours" % (key, where))
    elif when == "before":
        if not (scope in ("system", "global") or origin in own):
            foreign.append("credential.helper %s (%s): read after the clone's reset" % (value, where))
    elif not nl or value:
        helpers.append(value if nl else "(no value)")
    else:
        helpers = []
if when == "after" and helpers != [ours]:
    foreign.append("credential helpers in use: %s, not %s alone" % (", ".join(helpers) or "none", ours))
print("\n".join(foreign))
EOF
  ) | sed '/^$/d'
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
    if [ "$OFFLOAD_ONLY" = 1 ] && [ "$f" = ameesh-guest-runner@.service ]; then
      if [ -e "$(at "$UNITS/$f")" ]; then ko "$UNITS/$f present: a guest runner on a b1 host"
      else ok "no $UNITS/$f (--offload-only)"; fi
      continue
    fi
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
  if [ "$OFFLOAD_ONLY" = 1 ]; then
  # b1 (L159h): no secret at all on this host, no guest runner
  for f in "$(at "$ETC")/guest.env" "$(at "$ETC")"/guest-*.env; do
    if [ -e "$f" ]; then ko "${f#"$HOSTDIR"} present: a secrets file on a b1 host"; fi
  done
  for u in $(runner_units); do ko "$u present: a guest runner on a b1 host"; done
  else
  for f in "$(at "$ETC")/guest.env" "$(at "$ETC")"/guest-*.env; do
    [ -e "$f" ] || continue
    perm=$(stat -c '%a %U:%G' "$f")
    f=${f#"$HOSTDIR"}
    [ "$perm" = "640 root:$GUEST" ] && ok "$f ($perm)" || ko "$f is $perm, expected 640 root:$GUEST"
  done
  grep -q '^AMEESH_DSN=.' "$(at "$ETC")/guest.env" 2>/dev/null && ok "AMEESH_DSN set in guest.env" \
    || ko "AMEESH_DSN not set in $ETC/guest.env"
  # the guests' workspace (#483); a test's host has its files owned by the
  # test's user, its "root" (and its ameesh-guest, in its passwd)
  root_uid=0; [ -z "$HOSTDIR" ] || root_uid=$(id -u)
  guid=$(guest_uid)
  h=$(at "$HELPER")
  if [ -f "$h" ] && [ ! -L "$h" ] && [ "$(cat "$h")" = "$(helper_text)" ] \
     && [ "$(stat -c '%u %a' "$h")" = "$root_uid 755" ]; then
    ok "$HELPER (git credential helper, root 755)"
  else ko "$HELPER missing, changed, or not root 755 (--enable writes it)"; fi
  d=$(at "$GHOME/.dsh")
  if [ -f "$d/hooks.json" ] && [ -d "$d/profiles" ] && [ ! -L "$d" ] \
     && [ "$(stat -c %u "$d")" = "$guid" ]; then
    ok "$GHOME/.dsh: hooks.json, profiles $(cd "$d/profiles" && ls | tr '\n' ' ')"
    # the rules of the copy, read the same way (codex #457 r3)
    while IFS= read -r line; do
      [ -z "$line" ] || ko "$GHOME/.dsh/$line"
    done < <(dsh_read check "$d")
  else ko "$GHOME/.dsh: no hooks.json and profiles of the guest's own (--enable copies them)"; fi
  guests=()
  for f in "$(at "$ETC")"/guest-*.env; do
    [ -e "$f" ] || continue
    a=${f##*/guest-}; a=${a%.env}
    valid_name "$a" && guests+=("$a")
  done
  declare -A folder=()
  if [ ${#guests[@]} -gt 0 ] && [ -n "$CANON" ]; then
    if ws=$(canon_workspaces "${guests[@]}" 2>&1); then
      while IFS=$'\t' read -r a _ dir; do folder[$a]=$dir; done <<<"$ws"
    else
      while IFS= read -r line; do ko "canon: $line"; done <<<"$ws"
    fi
  elif [ ${#guests[@]} -gt 0 ]; then
    echo "     (no --canon: each work folder taken for $ROOT/work/<agent>)"
    for a in "${guests[@]}"; do folder[$a]=$ROOT/work/$a; done
  fi
  # one UID for every guest: no token is kept from the others (mesh-design #457 r5)
  [ ${#guests[@]} -eq 0 ] || echo "     note: the guests of one host share a UID, so their tokens:" \
    "one GitHub token per guest host, with the fewest rights possible"
  for a in "${guests[@]}"; do
    has_token "$a" && ok "GH_TOKEN set in guest-$a.env" || ko "GH_TOKEN not set in $ETC/guest-$a.env"
    dir=${folder[$a]:-}
    [ -n "$dir" ] || continue
    if origin=$(origin_of "$dir") && [ "$(stat -c %u "$(at "$dir")")" = "$guid" ]; then
      head=$(head -c 200 "$(at "$dir/.git/HEAD")" 2>/dev/null || true)
      case "$head" in "ref: "*) head=${head#ref: } ;; *) head="detached at ${head:0:12}" ;; esac
      ok "$dir: a clone of $origin, $head"
      # every credential setting git reads there: our helper alone
      audit=$(cred_audit "$dir" after)
      if [ -z "$audit" ]; then ok "$dir: credential.helper $HELPER alone"
      else while IFS= read -r line; do ko "$dir: $line"; done <<<"$audit"; fi
    else ko "$dir: not a clone of the guest's (--enable makes it)"; fi
  done
  for u in $(runner_units); do
    echo "     $u: $(systemctl is-enabled "$u" 2>/dev/null || true), $(systemctl is-active "$u" 2>/dev/null || true)"
  done
  fi
  # #501: inspect live native runners without printing their environment.
  # Separate helper keeps this addition independent of pilot provisioning.
  if [ -z "$HOSTDIR" ]; then
    python3 "$HERE/check-native-gates.py" || status=1
  else
    ok "native gate process check skipped on the test host"
  fi
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
    done
    user_exists || [ "$DRY" = 1 ] || die "no user $GUEST: install first"
    # everything checked before anything is done: the canon, each project's
    # forge, the work folders, the tokens, the dsh profile
    ws=$(canon_workspaces "${TARGETS[@]}") || die "the canon refuses (above): nothing done"
    # the work folders and the guest's home on the volume: no link on the
    # way, no mount on them (codex #457 r1)
    for d in "$ROOT/work" "$GHOME"; do
      resolved "$d" || die "$d goes through a symbolic link: refused, nothing done"
      ! mounts_under "$(at "$d")" || die "a mount point at or beneath $d: refused, nothing done"
    done
    clones=() kept=()
    while IFS=$'\t' read -r a project dir; do
      [ -n "${REPOS[$project]:-}" ] \
        || die "$a works on $project: give its forge, --repo $project=https://…#BRANCH"
      if origin=$(origin_of "$dir"); then
        same_repo "$origin" "${REPOS[$project]%%#*}" \
          || die "$dir is a clone of $origin, not of ${REPOS[$project]%%#*}: move it away first"
        # a clone never checked out (a branch that failed): not kept
        [ -f "$(at "$dir/.git/index")" ] && [ ! -L "$(at "$dir/.git/index")" ] \
          || die "$dir is a clone of $origin never checked out: move it away first"
        # no credential setting the clone's reset would leave (codex #457 r3)
        audit=$(cred_audit "$dir" before)
        [ -z "$audit" ] || die "$dir: credential settings not ours, remove them first:"$'\n'"$audit"
        echo "kept $dir: a clone of $origin (never fetched nor reset here)"
        kept+=("$dir")
        continue
      fi
      if ! absent "$dir"; then
        # (an unreadable folder is never taken for an empty one)
        not_a_dir "$dir" || ! entries=$(ls -A -- "$(at "$dir")") || [ -n "$entries" ] \
          && die "$dir is not empty and not a clone of ${REPOS[$project]%%#*}: move it away first"
      fi
      if ! has_token "$a"; then
        [ "$DRY" = 1 ] || die "GH_TOKEN not set in $ETC/guest-$a.env: $dir cannot be cloned"
        echo "warning: GH_TOKEN not set in $ETC/guest-$a.env (refused for real)"
      fi
      clones+=("$a $dir ${REPOS[$project]}")
    done <<<"$ws"
    dsh_dst=$GHOME/.dsh
    copy=()
    if [ -f "$(at "$dsh_dst/hooks.json")" ] && [ -d "$(at "$dsh_dst/profiles")" ]; then
      echo "kept $dsh_dst: hooks.json and profiles there already"
    else
      [ ! -L "$(at "$dsh_dst")" ] || die "$dsh_dst is a symbolic link: refused"
      if [ -z "$DSH_FROM" ] && [ -z "$HOSTDIR" ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        DSH_FROM=$(getent passwd "$SUDO_USER" | cut -d: -f6)/.dsh
      fi
      [ -n "$DSH_FROM" ] || die "no dsh profile in $dsh_dst: give --dsh-from DIR (a ~/.dsh)"
      list=$(dsh_read list "$DSH_FROM") || exit 1
      mapfile -t copy <<<"$list"
      # what the guest has already is kept, never overwritten (codex #457 r1);
      # no link on the way there (codex #457 r2), nor a directory taken for
      # one (the copy is made as the guest all the same)
      for d in "$dsh_dst" "$dsh_dst/profiles" $(printf '%s\n' "${copy[@]}" | sed -n 's#^\(profiles/[^/]*\)/.*#\1#p' | sort -u | sed "s#^#$dsh_dst/#"); do
        [ ! -L "$(at "$d")" ] && ! not_a_dir "$d" || die "$d is a symbolic link or not a directory: refused, nothing done"
      done
      keep=()
      for f in "${copy[@]}"; do
        if [ -e "$(at "$dsh_dst/$f")" ] || [ -L "$(at "$dsh_dst/$f")" ]; then
          echo "kept $dsh_dst/$f: there already"
        else keep+=("$f"); fi
      done
      copy=("${keep[@]}")
    fi
    # each branch to clone exists, asked of the forge by the guest before
    # anything is written (codex #457 r1)
    for c in "${clones[@]}"; do
      read -r a dir spec <<<"$c"
      git_cred "$a" ls-remote --exit-code --heads -- "${spec%%#*}" "refs/heads/${spec#*#}" \
        || die "${spec%%#*}: no branch ${spec#*#} (or the forge refused): nothing done"
    done

    say "the guest's git credential: $HELPER, for https://github.com"
    helper_text | put "$HELPER" 0755 root:root
    if [ ${#copy[@]} -gt 0 ]; then
      say "the guest's dsh profile: ${copy[*]}, from $DSH_FROM"
      as_guest install -d -m 0700 "$dsh_dst"
      # read once by root (what was checked), written by the guest
      if [ "$DRY" = 1 ]; then
        dsh_read tar "$DSH_FROM" "${copy[@]}" >/dev/null || exit 1
        echo "+ (dsh_read tar $DSH_FROM ${copy[*]}) | runuser -u $GUEST -- tar -C $dsh_dst -xf - --no-same-owner"
      else
        (cd / && dsh_read tar "$DSH_FROM" "${copy[@]}" \
           | runuser -u "$GUEST" -- tar -C "$dsh_dst" -xf - --no-same-owner) \
          || die "the dsh profile was not copied to $dsh_dst"
      fi
    fi
    for c in "${clones[@]}"; do
      read -r a dir spec <<<"$c"
      say "$a: $dir, a clone of ${spec%%#*} detached on origin/${spec#*#}"
      git_cred "$a" -C "$ROOT/work" clone --quiet --no-checkout -- "${spec%%#*}" "$dir" \
        || die "the clone of ${spec%%#*} failed"
      as_guest git -C "$dir" checkout --quiet --detach "origin/${spec#*#}" \
        || die "origin/${spec#*#} not checked out in $dir: a rerun refuses it, move it away"
      kept+=("$dir")
    done
    # each clone's helpers: those before reset (an empty one), then ours
    # alone, in its own .git/config (read after the guest's every file);
    # then read again, every scope: ours alone, or the runner is not started
    for dir in "${kept[@]}"; do
      as_guest git -C "$dir" config --local --replace-all credential.helper ""
      as_guest git -C "$dir" config --local --add credential.helper "$HELPER"
      [ "$DRY" = 1 ] || { audit=$(cred_audit "$dir" after) && [ -z "$audit" ]; } \
        || die "$dir: credential settings not ours alone:"$'\n'"$audit"
    done

    for a in "${TARGETS[@]}"; do
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
  run rm -f "$TMPFILES" "$HELPER"
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
# PATH, ReadWritePaths, RequiresMountsFor); none in --offload-only
if [ "$OFFLOAD_ONLY" = 0 ]; then
  sed "s#/srv/ameesh-guest#$ROOT#g" "$HERE/ameesh-guest-runner@.service" \
    | put "$UNITS/ameesh-guest-runner@.service" 0644 root:root
fi
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

if [ "$OFFLOAD_ONLY" = 1 ]; then
  say "4. secrets files: none (--offload-only, offloaded jobs carry no secret)"
else
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
fi

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
if [ "$OFFLOAD_ONLY" = 1 ]; then
  cat <<EOF
Next (mode b1, L159h):
  1. install.sh --gate file --key … --from … (the offloaded jobs obey this gate);
  2. $0 --check --offload-only;
  3. after a day of observation: $0 --offload-only --set-guard-mode enforce
Rollback: install.sh --uninstall, then $0 --uninstall [--purge]
EOF
  exit 0
fi
cat <<EOF
Next:
  1. fill $ETC/guest.env (AMEESH_DSN) and $ETC/guest-<agent>.env;
  2. $0 --check;
  3. after the canon PR (Host policy.lend, placements of ${AGENTS[*]} on $HOST):
     $0 --enable ${AGENTS[*]} --canon <canon checkout> --repo <project>=https://…#<branch>
     (their clones, git credential and dsh profile, then their runners)
  4. after a day of observation: $0 --set-guard-mode enforce
Rollback: $0 --uninstall [--purge]
EOF
