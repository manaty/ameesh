#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Prepares a lending VM for heavy-suite offloading (lot L159b, mode b1 of the
# L159 study, docs/design/etudes/l159-capacite-des-hotes.md, sections 2.2, 3
# and 4). Run by the VM's owner, as root, from a checkout of ameesh:
#
#   sudo deploy/guest/install.sh --key "ssh-ed25519 AAAA… agent@poste" \
#        [--key …] [--from 203.0.113.7,198.51.100.0/24] [options]
#   sudo deploy/guest/install.sh --check [PATH…]     what the guest can read
#   sudo deploy/guest/install.sh --uninstall [--purge]
#
# Options:
#   --dry-run            print every command and file, change nothing
#   --user NAME          guest user (default ameesh-guest)
#   --root DIR           its volume (default /srv/ameesh-guest; a dedicated
#                        mount point is expected, --allow-rootfs otherwise)
#   --job-user NAME      user the jobs run as (default ameesh-job, L159b2);
#                        another UID than the guest's, through the root-owned
#                        launcher and a sudoers rule
#   --no-job-user        legacy layout: jobs keep the guest's UID (L159b)
#   --slots N            concurrent jobs (default 3)
#   --cpu-share F        CPU quota of the guest, share of the host (default 0.75)
#   --memory-max SIZE    hard cap of the whole guest (default 60 % of RAM)
#   --memory-high SIZE   soft cap (default 50 % of RAM)
#   --job-memory-max S   hard cap per job (default memory-max / slots)
#   --max-job-seconds S  longest job (default 3600)
#   --deny-port P[,P…]   extra loopback ports of the application to refuse
#   --allow-net CIDR     internal network the guest may reach (repeatable)
#   --gate file          obey /run/ameesh-gate/state/state.json (L159e)
#   --no-firewall        skip the nftables rules
#
# What it does (all of it undone by --uninstall):
#   * user NAME, no password, no sudo, no docker group, home = --root (0710),
#     subuid/subgid range, linger (rootless Podman needs its user manager);
#   * with --job-user: a second user (its own UID, no password, no sudo, its
#     own home and its own user manager), a root-owned launcher admitted by a
#     sudoers rule, and a shared group: the job reads the shell's mirrors but
#     cannot write them, and it reads neither the shell's /proc, nor its HOME,
#     nor its caches (reviews r5/r7 of #213);
#   * ameesh-offload-shell (root-owned) as the forced command of the keys,
#     through an sshd Match block: no shell, no forwarding, no tty;
#   * caps on user-UID.slice (guest and job user): CPUWeight/IOWeight 10,
#     CPUQuota, MemoryHigh, MemoryMax, no swap, TasksMax;
#   * nftables rules by uid: no application port on the loopback, no internal
#     network, no metadata service; HTTPS out stays open;
#   * /etc/ameesh-guest/offload.json and profiles (root-owned: the guest
#     cannot raise its slots, its caps, nor write a profile hook).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/../.." && pwd)/src/ameesh/platform/offload_shell.py
LAUNCHER_SRC=$HERE/ameesh-job-launcher
LAUNCHER_PY_SRC=$HERE/ameesh-job-launcher.py
GUEST=ameesh-guest
ROOT=/srv/ameesh-guest
ETC=/etc/ameesh-guest
LIB=/usr/local/lib/ameesh-guest
SHELL_BIN=$LIB/ameesh-offload-shell
LAUNCHER_BIN=$LIB/ameesh-job-launcher
SUDOERS=/etc/sudoers.d/ameesh-guest-job
SSHD_DROPIN=/etc/ssh/sshd_config.d/50-ameesh-guest.conf
FW_UNIT=/etc/systemd/system/ameesh-guest-firewall.service
JOB_USER=ameesh-job
JOB_GROUP=ameesh-jobs
JOB_MODE=1
SLOTS=3
CPU_SHARE=0.75
MEMORY_MAX=""
MEMORY_HIGH=""
JOB_MEMORY_MAX=""
MAX_JOB_SECONDS=3600
DENY_PORTS="2375,2376,3000,3306,5432,5672,6379,8000,8080,8443,9000,9090,9200,11211,15672,27017"
ALLOW_NETS=()
KEYS=()
FROM=""
GATE=none
FIREWALL=1
DRY=0
ALLOW_ROOTFS=0
MODE=install
PURGE=0
CHECK_PATHS=()

die() { echo "install.sh: $*" >&2; exit 1; }
say() { echo "== $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --key) KEYS+=("$2"); shift 2 ;;
    --from) FROM=$2; shift 2 ;;
    --user) GUEST=$2; shift 2 ;;
    --job-user) JOB_USER=$2; shift 2 ;;
    --no-job-user) JOB_MODE=0; shift ;;
    --root) ROOT=$2; shift 2 ;;
    --slots) SLOTS=$2; shift 2 ;;
    --cpu-share) CPU_SHARE=$2; shift 2 ;;
    --memory-max) MEMORY_MAX=$2; shift 2 ;;
    --memory-high) MEMORY_HIGH=$2; shift 2 ;;
    --job-memory-max) JOB_MEMORY_MAX=$2; shift 2 ;;
    --max-job-seconds) MAX_JOB_SECONDS=$2; shift 2 ;;
    --deny-port) DENY_PORTS="$DENY_PORTS,$2"; shift 2 ;;
    --allow-net) ALLOW_NETS+=("$2"); shift 2 ;;
    --gate) GATE=$2; shift 2 ;;
    --no-firewall) FIREWALL=0; shift ;;
    --allow-rootfs) ALLOW_ROOTFS=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --check) MODE=check; shift; CHECK_PATHS=("$@"); break ;;
    --uninstall) MODE=uninstall; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '3,47p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

case "$GUEST" in ''|*[!a-z0-9_-]*) die "invalid --user $GUEST" ;; esac
case "$JOB_USER" in ''|*[!a-z0-9_-]*) die "invalid --job-user $JOB_USER" ;; esac
[ "$JOB_USER" != "$GUEST" ] || die "--job-user must differ from --user"
case "$SLOTS" in ''|*[!0-9]*) die "invalid --slots $SLOTS" ;; esac
case "$GATE" in none|file) ;; *) die "--gate none|file" ;; esac

# run CMD…: executes, or prints it under --dry-run
run() {
  if [ "$DRY" = 1 ]; then echo "+ $*"; else "$@"; fi
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

guest_uid() {
  if id -u "$GUEST" >/dev/null 2>&1; then id -u "$GUEST"
  elif [ "$DRY" = 1 ]; then echo 990
  else die "no user $GUEST"; fi
}

job_uid() {
  if id -u "$JOB_USER" >/dev/null 2>&1; then id -u "$JOB_USER"
  elif [ "$DRY" = 1 ]; then echo 991
  else die "no user $JOB_USER"; fi
}

check_no_power_groups() {
  local user=$1 group
  [ "$DRY" = 0 ] || return 0
  for group in $(id -nG "$user"); do
    case "$group" in
      sudo|wheel|admin|docker|adm|lxd|libvirt) die "$user is in group $group: remove it first" ;;
    esac
  done
}

add_subids() {
  local user=$1 start
  if ! grep -q "^$user:" /etc/subuid 2>/dev/null \
     || ! grep -q "^$user:" /etc/subgid 2>/dev/null; then
    start=$(awk -F: 'BEGIN { m = 100000 } { e = $2 + $3; if (e > m) m = e } END { print m }' \
              /etc/subuid /etc/subgid 2>/dev/null || echo 100000)
    run usermod --add-subuids "$start-$(( start + 65535 ))" \
                --add-subgids "$start-$(( start + 65535 ))" "$user"
  fi
}

need_root() {
  [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (sudo)"
}

# --------------------------------------------------------------------------
# --check: what the guest can read outside its volume (study, section 3)
# --------------------------------------------------------------------------
if [ "$MODE" = check ]; then
  need_root
  id -u "$GUEST" >/dev/null 2>&1 || die "no user $GUEST"
  [ ${#CHECK_PATHS[@]} -gt 0 ] || CHECK_PATHS=(/root /home /opt /srv /var/www /var/lib
                                               /run/secrets /run/docker.sock /run/podman)
  say "groups of $GUEST: $(id -nG "$GUEST")"
  status=0
  for group in sudo wheel admin docker adm lxd libvirt; do
    if id -nG "$GUEST" | tr ' ' '\n' | grep -qx "$group"; then
      echo "FAIL $GUEST is in group $group"; status=1
    fi
  done
  if [ "$JOB_MODE" = 1 ]; then
    id -u "$JOB_USER" >/dev/null 2>&1 || die "no user $JOB_USER"
    say "groups of $JOB_USER: $(id -nG "$JOB_USER")"
    for group in sudo wheel admin docker adm lxd libvirt; do
      if id -nG "$JOB_USER" | tr ' ' '\n' | grep -qx "$group"; then
        echo "FAIL $JOB_USER is in group $group"; status=1
      fi
    done
    if runuser -u "$JOB_USER" -- test -x "$ROOT" \
       && runuser -u "$JOB_USER" -- test -r "$ROOT/mirrors"; then
      echo "ok   $JOB_USER traverses $ROOT and reads $ROOT/mirrors"
    else
      echo "FAIL $JOB_USER cannot traverse $ROOT or read $ROOT/mirrors"; status=1
    fi
    if [ -f "$LAUNCHER_BIN" ] && [ "$(stat -c %U "$LAUNCHER_BIN")" = root ] \
       && [ -z "$(find "$LAUNCHER_BIN" -perm /022 -print -quit 2>/dev/null)" ]; then
      echo "ok   $LAUNCHER_BIN (root-owned, not writable by others)"
    else
      echo "FAIL $LAUNCHER_BIN is missing or writable by others"; status=1
    fi
    if [ -f "$SUDOERS" ] && visudo -cf "$SUDOERS" >/dev/null 2>&1; then
      echo "ok   $SUDOERS"
    else
      echo "FAIL $SUDOERS is missing or refused by visudo"; status=1
    fi
    # the job must write nothing the shell's own git reads, and read nothing
    # of the shell's private state; it does read the mirrors (by design)
    if runuser -u "$JOB_USER" -- test -w "$ROOT/mirrors" 2>/dev/null; then
      echo "FAIL $JOB_USER can write $ROOT/mirrors"; status=1
    else
      echo "ok   $ROOT/mirrors is not writable by $JOB_USER"
    fi
    job_reads=$(runuser -u "$JOB_USER" -- find "$ROOT/locks" "$ROOT/jobs.jsonl" \
                  -readable -print 2>/dev/null | head -n 5 || true)
    if [ -n "$job_reads" ]; then
      echo "FAIL readable by $JOB_USER (should be the shell's alone): $job_reads"; status=1
    else
      echo "ok   $ROOT/locks and jobs.jsonl are the shell's alone"
    fi
    if runuser -u "$GUEST" -- ls "$ROOT/home" >/dev/null 2>&1; then
      echo "FAIL $GUEST can read the job's home $ROOT/home"; status=1
    else
      echo "ok   $ROOT/home is the job's alone"
    fi
  fi
  for path in "${CHECK_PATHS[@]}"; do
    [ -e "$path" ] || continue
    found=$(runuser -u "$GUEST" -- find "$path" -path "$ROOT" -prune -o \
              \( -type f -o -type s \) -readable -print 2>/dev/null | head -n 20 || true)
    if [ -n "$found" ]; then
      echo "READABLE by $GUEST under $path (first 20):"; echo "$found" | sed 's/^/  /'
      status=1
    else
      echo "ok   $path"
    fi
  done
  exit $status
fi

# --------------------------------------------------------------------------
# --uninstall: section 4.3 of the study
# --------------------------------------------------------------------------
if [ "$MODE" = uninstall ]; then
  need_root
  uid=$(id -u "$GUEST" 2>/dev/null || echo "")
  juid=$(id -u "$JOB_USER" 2>/dev/null || echo "")
  say "removing the forced command, launcher, firewall, caps and users"
  run rm -f "$SSHD_DROPIN" "$SUDOERS"
  run systemctl reload ssh 2>/dev/null || run systemctl reload sshd || true
  if [ -f "$FW_UNIT" ]; then
    run systemctl disable --now ameesh-guest-firewall.service || true
    run rm -f "$FW_UNIT"
  fi
  if [ -n "$uid" ]; then
    run loginctl disable-linger "$GUEST" || true
    run loginctl terminate-user "$GUEST" || true
    run rm -rf "/etc/systemd/system/user-$uid.slice.d/50-ameesh-guest.conf"
    run userdel "$GUEST" || true
  fi
  if [ -n "$juid" ]; then
    run loginctl disable-linger "$JOB_USER" || true
    run loginctl terminate-user "$JOB_USER" || true
    run rm -rf "/etc/systemd/system/user-$juid.slice.d/50-ameesh-job.conf"
    run userdel "$JOB_USER" || true
  fi
  if [ "$JOB_MODE" = 1 ]; then
    run groupdel "$JOB_GROUP" 2>/dev/null || true
  fi
  run systemctl daemon-reload
  run rm -rf "$ETC" "$LIB"
  if [ "$PURGE" = 1 ]; then
    run rm -rf --one-file-system "$ROOT"
  else
    echo "kept $ROOT (mirrors, caches, home, jobs.jsonl); --purge removes it"
  fi
  exit 0
fi

# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
need_root
[ ${#KEYS[@]} -gt 0 ] || die "at least one --key (the public key of the workstation)"
[ -f "$SRC" ] || die "missing $SRC (run from a checkout of ameesh)"
for cmd in python3 systemctl useradd; do
  command -v "$cmd" >/dev/null || [ "$DRY" = 1 ] || die "missing $cmd"
done
if [ "$JOB_MODE" = 1 ]; then
  [ -f "$LAUNCHER_SRC" ] || die "missing $LAUNCHER_SRC"
  [ -f "$LAUNCHER_PY_SRC" ] || die "missing $LAUNCHER_PY_SRC"
  for cmd in groupadd visudo; do
    command -v "$cmd" >/dev/null || [ "$DRY" = 1 ] || die "missing $cmd"
  done
fi

mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
cpus=$(nproc)
[ -n "$MEMORY_MAX" ] || MEMORY_MAX="$(( mem_kb * 60 / 100 / 1024 ))M"
[ -n "$MEMORY_HIGH" ] || MEMORY_HIGH="$(( mem_kb * 50 / 100 / 1024 ))M"
if [ -z "$JOB_MEMORY_MAX" ]; then
  max_mb=$(numfmt --from=iec "${MEMORY_MAX%B}" 2>/dev/null || echo $(( mem_kb * 1024 * 60 / 100 )))
  JOB_MEMORY_MAX="$(( max_mb / 1024 / 1024 / SLOTS ))M"
fi
CPU_QUOTA=$(awk -v s="$CPU_SHARE" -v n="$cpus" 'BEGIN { printf "%d%%", s * n * 100 }')

if [ "$DRY" = 0 ] && [ "$ALLOW_ROOTFS" = 0 ] && ! mountpoint -q "$ROOT"; then
  die "$ROOT is not a mount point: mount the dedicated volume first (study 4.1), or --allow-rootfs"
fi

say "packages: git, rootless podman, nftables, psql (missing ones only)"
missing=()
for pair in git:git podman:podman newuidmap:uidmap slirp4netns:slirp4netns \
            fuse-overlayfs:fuse-overlayfs psql:postgresql-client nft:nftables; do
  [ "${pair%%:*}" != nft ] || [ "$FIREWALL" = 1 ] || continue
  command -v "${pair%%:*}" >/dev/null || missing+=("${pair#*:}")
done
if [ ${#missing[@]} -gt 0 ]; then
  if command -v apt-get >/dev/null; then
    run apt-get install -y --no-install-recommends "${missing[@]}" dbus-user-session
  elif [ "$DRY" = 0 ]; then
    die "install ${missing[*]}, then rerun"
  fi
fi

say "user $GUEST (home $ROOT, no password, no sudo, no docker)"
if ! id -u "$GUEST" >/dev/null 2>&1; then
  run useradd --home-dir "$ROOT" --no-create-home --shell /bin/sh --user-group \
      --comment "ameesh offloaded test jobs (L159b)" "$GUEST"
fi
# `*`, not `!`: no password, yet sshd still accepts the key without PAM
run usermod -p '*' "$GUEST"
check_no_power_groups "$GUEST"
uid=$(guest_uid)
add_subids "$GUEST"

if [ "$JOB_MODE" = 1 ]; then
  say "job user $JOB_USER (group $JOB_GROUP): the jobs' UID, not the shell's"
  if ! getent group "$JOB_GROUP" >/dev/null 2>&1; then
    run groupadd --system "$JOB_GROUP"
  fi
  if ! id -u "$JOB_USER" >/dev/null 2>&1; then
    run useradd --home-dir "$ROOT/home" --no-create-home --shell /usr/sbin/nologin \
        --gid "$JOB_GROUP" --comment "ameesh offloaded jobs (L159b2)" "$JOB_USER"
  fi
  run usermod -p '*' "$JOB_USER"
  check_no_power_groups "$JOB_USER"
  juid=$(job_uid)
  add_subids "$JOB_USER"
fi

# the shared group: the job traverses the volume and reads the mirrors, the
# shell keeps them; everything the job may write is granted job by job. The
# setgid bit keeps that group on the directories the shell creates later (the
# shell is not a member of the group, so it cannot chgrp them itself).
if [ "$JOB_MODE" = 1 ]; then
  run install -d -m 2710 -o "$GUEST" -g "$JOB_GROUP" "$ROOT"
  for sub in mirrors cache jobs; do
    run install -d -m 2750 -o "$GUEST" -g "$JOB_GROUP" "$ROOT/$sub"
  done
else
  run install -d -m 0700 -o "$GUEST" -g "$GUEST" "$ROOT"
  for sub in mirrors cache jobs; do
    run install -d -m 0700 -o "$GUEST" -g "$GUEST" "$ROOT/$sub"
  done
fi
run install -d -m 0700 -o "$GUEST" -g "$GUEST" "$ROOT/locks" "$ROOT/.config/containers"
if [ "$JOB_MODE" = 1 ]; then
  run install -d -m 0700 -o "$JOB_USER" -g "$JOB_GROUP" "$ROOT/home"
fi
# an upgrade from the 0700 (legacy) layout: install -d does not fix modes, and
# the caches and mirrors the guest built alone must become readable (caches
# also writable: the job writes its own bytecode there) by the shared group
if [ "$JOB_MODE" = 1 ] && [ "$DRY" = 0 ]; then
  chmod 2710 "$ROOT" 2>/dev/null || true
  chgrp "$JOB_GROUP" "$ROOT" "$ROOT/mirrors" "$ROOT/cache" "$ROOT/jobs" 2>/dev/null || true
  chmod 2750 "$ROOT/mirrors" "$ROOT/cache" "$ROOT/jobs" 2>/dev/null || true
  chgrp -R "$JOB_GROUP" "$ROOT/cache" "$ROOT/mirrors" 2>/dev/null || true
  find "$ROOT/cache" -type d -exec chmod g+rwxs {} + 2>/dev/null || true
  find "$ROOT/cache" -type f -exec chmod g+rw {} + 2>/dev/null || true
  find "$ROOT/mirrors" -type d -exec chmod g+rxs {} + 2>/dev/null || true
  find "$ROOT/mirrors" -type f -exec chmod g+r {} + 2>/dev/null || true
  chmod 0600 "$ROOT/jobs.jsonl" 2>/dev/null || true
fi
run loginctl enable-linger "$GUEST"
if [ "$JOB_MODE" = 1 ]; then
  run loginctl enable-linger "$JOB_USER"
fi

say "forced command $SHELL_BIN and profiles in $ETC (root-owned)"
run install -d -m 0755 "$LIB" "$ETC" "$ETC/profiles"
run install -m 0755 -o root -g root "$SRC" "$SHELL_BIN"
run install -m 0755 -o root -g root "$HERE/job-postgres.sh" "$LIB/job-postgres"
if [ "$JOB_MODE" = 1 ]; then
  say "job launcher $LAUNCHER_BIN (root-owned) and its sudoers rule"
  run install -m 0755 -o root -g root "$LAUNCHER_SRC" "$LAUNCHER_BIN"
  run install -m 0644 -o root -g root "$LAUNCHER_PY_SRC" "$LIB/ameesh-job-launcher.py"
  put "$SUDOERS" 0440 root:root <<EOF
# ameesh-guest (L159b2): the shell starts each job phase as the job's user;
# the launcher refuses to run as root, so a wrong rule fails closed
Defaults:$GUEST !requiretty
$GUEST ALL=($JOB_USER) NOPASSWD: $LAUNCHER_BIN
EOF
  if [ "$DRY" = 0 ]; then
    visudo -cf "$SUDOERS" >/dev/null \
      || { rm -f "$SUDOERS"; die "visudo refused $SUDOERS (removed)"; }
  fi
fi
for profile in "$HERE"/profiles/*.json; do
  run install -m 0644 -o root -g root "$profile" "$ETC/profiles/$(basename "$profile")"
done
gate_json='{"mode": "none"}'
[ "$GATE" = file ] && gate_json='{"mode": "file", "path": "/run/ameesh-gate/state/state.json"}'
executor_json='"executor": "user-scope"'
if [ "$JOB_MODE" = 1 ]; then
  executor_json='"executor": "job-user",
  "job_user": "'"$JOB_USER"'",
  "job_home": "'"$ROOT"'/home",
  "launcher": "'"$LAUNCHER_BIN"'"'
fi
put "$ETC/offload.json" 0644 root:root <<EOF
{
  "root": "$ROOT",
  "profiles": "$ETC/profiles",
  "slots": $SLOTS,
  "agent_memory_max": "$JOB_MEMORY_MAX",
  "max_job_seconds": $MAX_JOB_SECONDS,
  "scope": true,
  "keep_days": 7,
  $executor_json,
  "gate": $gate_json
}
EOF

say "SSH: restricted keys, forced command, no forwarding, no tty"
# -I: isolated, neither the guest's site-packages nor its PYTHON* variables
PYTHON=$(command -v python3 || echo /usr/bin/python3)
command_line="$PYTHON -I $SHELL_BIN --config $ETC/offload.json"
# choom (util-linux): the global OOM killer picks the guest's jobs first (study 1.6)
CHOOM=$(command -v choom || true)
[ -z "$CHOOM" ] || command_line="$CHOOM -n 500 -- $command_line"
# exec: sshd runs the forced command through the guest's shell (-c); without
# it, that shell would stay alive holding the session's streams
command_line="exec $command_line"
key_opts="restrict"
[ -z "$FROM" ] || key_opts="$key_opts,from=\"$FROM\""
key_opts="$key_opts,command=\"$command_line\""
{
  echo "# ameesh-guest (L159b): written by deploy/guest/install.sh, one line per workstation"
  for key in "${KEYS[@]}"; do
    case "$key" in ssh-*|ecdsa-*|sk-*) ;; *) die "--key must be a public key line" ;; esac
    echo "$key_opts $key"
  done
} | put "$ETC/authorized_keys" 0644 root:root
# the Match block holds even if the guest wrote ~/.ssh/authorized_keys:
# sshd reads the root-owned file only, and forces the command anyway
put "$SSHD_DROPIN" 0644 root:root <<EOF
# ameesh-guest (L159b): written by deploy/guest/install.sh
Match User $GUEST
    AuthorizedKeysFile $ETC/authorized_keys
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowAgentForwarding no
    AllowTcpForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    PermitUserRC no
    ForceCommand $command_line
Match all
EOF
if [ "$DRY" = 0 ]; then
  if ! sshd -t; then
    rm -f "$SSHD_DROPIN"
    die "sshd -t refused the Match block (removed); is sshd_config.d included?"
  fi
  sshd -T -C "user=$GUEST,host=offload,addr=127.0.0.1" | grep -qiF "forcecommand $command_line" \
    || { rm -f "$SSHD_DROPIN"; die "sshd does not apply the Match block (Include of sshd_config.d at the top?)"; }
fi
run systemctl reload ssh 2>/dev/null || run systemctl reload sshd

put_slice_caps() {
  local user=$1 uid=$2 tag=$3
  say "caps on user-$uid.slice ($user): CPUQuota=$CPU_QUOTA MemoryHigh=$MEMORY_HIGH MemoryMax=$MEMORY_MAX"
  put "/etc/systemd/system/user-$uid.slice.d/50-$tag.conf" 0644 root:root <<EOF
# $tag (L159b): yields to the application (study 1.6, level 0)
[Slice]
CPUWeight=10
IOWeight=10
CPUQuota=$CPU_QUOTA
MemoryHigh=$MEMORY_HIGH
MemoryMax=$MEMORY_MAX
MemorySwapMax=0
TasksMax=4096
EOF
}
put_slice_caps "$GUEST" "$uid" ameesh-guest
if [ "$JOB_MODE" = 1 ]; then
  put_slice_caps "$JOB_USER" "$juid" ameesh-job
fi
run systemctl daemon-reload

if [ "$FIREWALL" = 1 ]; then
  fw_uids="$uid"
  [ "$JOB_MODE" = 0 ] || fw_uids="$uid $juid"
  say "nftables: by uid ($fw_uids), no application port on the loopback, no internal network"
  resolvers=$(awk '/^nameserver/ && $2 !~ /^127\./ && $2 !~ /:/ {print $2}' /etc/resolv.conf \
                2>/dev/null | paste -sd, - || true)
  allow=""
  rules=""
  for u in $fw_uids; do
    [ -z "$resolvers" ] || allow="$allow
    meta skuid $u ip daddr { $resolvers } meta l4proto { tcp, udp } th dport 53 accept"
    for net in "${ALLOW_NETS[@]}"; do
      allow="$allow
    meta skuid $u ip daddr $net accept"
    done
    rules="$rules
    meta skuid $u ip daddr 127.0.0.0/8 tcp dport { $DENY_PORTS } reject with tcp reset
    meta skuid $u ip6 daddr ::1 tcp dport { $DENY_PORTS } reject with tcp reset
    meta skuid $u ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16 } reject
    meta skuid $u ip6 daddr { fc00::/7, fe80::/10 } reject"
  done
  put "$ETC/firewall.nft" 0644 root:root <<EOF
# ameesh-guest (L159b): outgoing rules of uids $fw_uids (study, section 3)
table inet ameesh_guest
delete table inet ameesh_guest
table inet ameesh_guest {
  chain output {
    type filter hook output priority 0; policy accept;
$allow
$rules
  }
}
EOF
  put "$FW_UNIT" 0644 root:root <<EOF
[Unit]
Description=ameesh-guest outgoing rules (L159b)
After=network-pre.target nftables.service
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $ETC/firewall.nft
ExecStop=/usr/sbin/nft delete table inet ameesh_guest

[Install]
WantedBy=multi-user.target
EOF
  run systemctl daemon-reload
  run systemctl enable ameesh-guest-firewall.service
  run systemctl restart ameesh-guest-firewall.service
fi

check_podman() {
  local user=$1 uid=$2 home=$3
  say "rootless Podman for $user"
  runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" HOME="$home" \
    podman system migrate >/dev/null 2>&1 || true
  if runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" HOME="$home" \
      podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null | grep -qx true; then
    echo "podman: rootless ok for $user"
  else
    echo "WARNING: rootless podman does not answer for $user; the postgres profile will fail" >&2
  fi
}
if [ "$DRY" = 0 ]; then
  check_podman "$GUEST" "$uid" "$ROOT"
  if [ "$JOB_MODE" = 1 ]; then
    check_podman "$JOB_USER" "$juid" "$ROOT/home"
  fi
fi

say "done"
cat <<EOF
Next, on the workstation (~/.ssh/config):
  Host ameesh-offload
      HostName $(hostname -f 2>/dev/null || hostname)
      User $GUEST
      IdentityFile ~/.ssh/<the key given with --key>
      IdentitiesOnly yes
then: AMEESH_OFFLOAD_HOST=ameesh-offload ameesh offload status
Check what the guest can read: sudo $0 --check
EOF
