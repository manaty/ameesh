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
#   * user NAME, no password, no sudo, no docker group, home = --root (0700),
#     subuid/subgid range, linger (rootless Podman needs its user manager);
#   * ameesh-offload-shell (root-owned) as the forced command of the keys,
#     through an sshd Match block: no shell, no forwarding, no tty;
#   * caps on user-UID.slice: CPUWeight/IOWeight 10, CPUQuota, MemoryHigh,
#     MemoryMax, no swap, TasksMax;
#   * nftables rules by uid: no application port on the loopback, no internal
#     network, no metadata service; HTTPS out stays open;
#   * /etc/ameesh-guest/offload.json and profiles (root-owned: the guest
#     cannot raise its slots, its caps, nor write a profile hook).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/../.." && pwd)/src/ameesh/platform/offload_shell.py
GUEST=ameesh-guest
ROOT=/srv/ameesh-guest
ETC=/etc/ameesh-guest
LIB=/usr/local/lib/ameesh-guest
SHELL_BIN=$LIB/ameesh-offload-shell
SSHD_DROPIN=/etc/ssh/sshd_config.d/50-ameesh-guest.conf
FW_UNIT=/etc/systemd/system/ameesh-guest-firewall.service
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
    -h|--help) sed -n '3,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

case "$GUEST" in ''|*[!a-z0-9_-]*) die "invalid --user $GUEST" ;; esac
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
  say "removing the forced command, firewall, caps and user $GUEST"
  run rm -f "$SSHD_DROPIN"
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
  run systemctl daemon-reload
  run rm -rf "$ETC" "$LIB"
  if [ "$PURGE" = 1 ]; then
    run rm -rf --one-file-system "$ROOT"
  else
    echo "kept $ROOT (mirrors, caches, jobs.jsonl); --purge removes it"
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
if [ "$DRY" = 0 ]; then
  for group in $(id -nG "$GUEST"); do
    case "$group" in
      sudo|wheel|admin|docker|adm|lxd|libvirt) die "$GUEST is in group $group: remove it first" ;;
    esac
  done
fi
uid=$(guest_uid)
if ! grep -q "^$GUEST:" /etc/subuid 2>/dev/null || ! grep -q "^$GUEST:" /etc/subgid 2>/dev/null; then
  start=$(awk -F: 'BEGIN { m = 100000 } { e = $2 + $3; if (e > m) m = e } END { print m }' \
            /etc/subuid /etc/subgid 2>/dev/null || echo 100000)
  run usermod --add-subuids "$start-$(( start + 65535 ))" \
              --add-subgids "$start-$(( start + 65535 ))" "$GUEST"
fi
run install -d -m 0700 -o "$GUEST" -g "$GUEST" "$ROOT"
for sub in mirrors cache jobs locks .config/containers; do
  run install -d -m 0700 -o "$GUEST" -g "$GUEST" "$ROOT/$sub"
done
run loginctl enable-linger "$GUEST"

say "forced command $SHELL_BIN and profiles in $ETC (root-owned)"
run install -d -m 0755 "$LIB" "$ETC" "$ETC/profiles"
run install -m 0755 -o root -g root "$SRC" "$SHELL_BIN"
run install -m 0755 -o root -g root "$HERE/job-postgres.sh" "$LIB/job-postgres"
for profile in "$HERE"/profiles/*.json; do
  run install -m 0644 -o root -g root "$profile" "$ETC/profiles/$(basename "$profile")"
done
gate_json='{"mode": "none"}'
[ "$GATE" = file ] && gate_json='{"mode": "file", "path": "/run/ameesh-gate/state/state.json"}'
put "$ETC/offload.json" 0644 root:root <<EOF
{
  "root": "$ROOT",
  "profiles": "$ETC/profiles",
  "slots": $SLOTS,
  "agent_memory_max": "$JOB_MEMORY_MAX",
  "max_job_seconds": $MAX_JOB_SECONDS,
  "scope": true,
  "keep_days": 7,
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

say "caps on user-$uid.slice: CPUQuota=$CPU_QUOTA MemoryHigh=$MEMORY_HIGH MemoryMax=$MEMORY_MAX"
put "/etc/systemd/system/user-$uid.slice.d/50-ameesh-guest.conf" 0644 root:root <<EOF
# ameesh-guest (L159b): the guest yields to the application (study 1.6, level 0)
[Slice]
CPUWeight=10
IOWeight=10
CPUQuota=$CPU_QUOTA
MemoryHigh=$MEMORY_HIGH
MemoryMax=$MEMORY_MAX
MemorySwapMax=0
TasksMax=4096
EOF
run systemctl daemon-reload

if [ "$FIREWALL" = 1 ]; then
  say "nftables: by uid $uid, no application port on the loopback, no internal network"
  resolvers=$(awk '/^nameserver/ && $2 !~ /^127\./ && $2 !~ /:/ {print $2}' /etc/resolv.conf \
                2>/dev/null | paste -sd, - || true)
  allow=""
  [ -z "$resolvers" ] || allow="    meta skuid $uid ip daddr { $resolvers } meta l4proto { tcp, udp } th dport 53 accept"
  for net in "${ALLOW_NETS[@]}"; do
    allow="$allow
    meta skuid $uid ip daddr $net accept"
  done
  put "$ETC/firewall.nft" 0644 root:root <<EOF
# ameesh-guest (L159b): outgoing rules of uid $uid (study, section 3)
table inet ameesh_guest
delete table inet ameesh_guest
table inet ameesh_guest {
  chain output {
    type filter hook output priority 0; policy accept;
$allow
    meta skuid $uid ip daddr 127.0.0.0/8 tcp dport { $DENY_PORTS } reject with tcp reset
    meta skuid $uid ip6 daddr ::1 tcp dport { $DENY_PORTS } reject with tcp reset
    meta skuid $uid ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16 } reject
    meta skuid $uid ip6 daddr { fc00::/7, fe80::/10 } reject
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

say "rootless Podman for $GUEST"
if [ "$DRY" = 0 ]; then
  runuser -u "$GUEST" -- env XDG_RUNTIME_DIR="/run/user/$uid" HOME="$ROOT" \
    podman system migrate >/dev/null 2>&1 || true
  if runuser -u "$GUEST" -- env XDG_RUNTIME_DIR="/run/user/$uid" HOME="$ROOT" \
      podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null | grep -qx true; then
    echo "podman: rootless ok"
  else
    echo "WARNING: rootless podman does not answer for $GUEST; the postgres profile will fail" >&2
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
