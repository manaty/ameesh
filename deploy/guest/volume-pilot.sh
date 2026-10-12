#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# The guest's volume of the pilot (a1) on ameesh-agents-1 (lot L159g, the
# owner's decision 10408 of 2026-10-12, option a): a 40 GB Block Storage
# volume, 5K IOPS, mounted on /srv/ameesh-guest, holds the offer's
# `disk_max 40GiB`. Run by the owner; an agent never changes a host's disks.
# Every step is idempotent: run again, it does only what is missing.
#
#   volume-pilot.sh create [--dry-run]       from a workstation with `scw`:
#                                            the volume, then attached
#   sudo volume-pilot.sh mount --volume-id ID [--dry-run]
#                                            on the host: ext4 if the disk
#                                            is blank, /etc/fstab, mounted
#   sudo volume-pilot.sh check [--volume-id ID]  on the host: what is in place
# Rollback, in this order (after `install-pilot.sh --uninstall`):
#   sudo volume-pilot.sh unmount [--dry-run] on the host: unmounted, its
#                                            /etc/fstab line removed; the
#                                            data stays on the volume
#   volume-pilot.sh detach [--delete] [--dry-run]
#                                            from the workstation: detached;
#                                            --delete also deletes it (data
#                                            lost, billing stops)
#
# Options:
#   --dry-run            print every command and file, change nothing
#   --volume-id ID       the volume (printed by `create`)
#   --server NAME        the instance (default ameesh-agents-1)
#   --zone Z             default fr-par-1
#   --root DIR           mount point (default /srv/ameesh-guest)
#
# The disk is found by the volume's id (/dev/disk/by-id/scsi-0SCW_sbs_volume-ID),
# never guessed. It is formatted only when it carries no signature at all;
# an ext4 labelled ameesh-guest is kept as is; anything else is refused.
# `disk_max` is not changed here: 40GiB is already install-pilot.sh's default
# offer and the canon's (manaty/home#24); `check` compares it with the size.
set -euo pipefail

NAME=ameesh-guest
SIZE_GB=40
IOPS=5000
SERVER=ameesh-agents-1
ZONE=fr-par-1
ROOT=/srv/ameesh-guest
OPTS=defaults,nofail,x-systemd.device-timeout=30s
VOLID=
DRY=0
DELETE=0
FSTAB=/etc/fstab
BYID=/dev/disk/by-id
GUARD=/etc/ameesh/guard.json

die() { echo "volume-pilot: $*" >&2; exit 1; }

MODE=${1:-}
[ $# -gt 0 ] && shift
case "$MODE" in
  create|mount|check|unmount|detach) ;;
  -h|--help|'') sed -n '3,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  *) die "unknown command $MODE (create, mount, check, unmount, detach)" ;;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --delete) DELETE=1; shift ;;
    --volume-id) VOLID=${2:?}; shift 2 ;;
    --server) SERVER=${2:?}; shift 2 ;;
    --zone) ZONE=${2:?}; shift 2 ;;
    --root) ROOT=${2:?}; shift 2 ;;
    *) die "unknown option $1" ;;
  esac
done
case "$VOLID" in ''|[0-9a-f]*-*-*-*-*) ;; *) die "invalid --volume-id $VOLID" ;; esac
case "$VOLID" in *[!0-9a-f-]*) die "invalid --volume-id $VOLID" ;; esac
case "$ROOT" in /srv/?*) ;; *) die "--root under /srv expected" ;; esac
case "$ROOT" in *[!A-Za-z0-9/._-]*|*/..*|*//*|*/) die "invalid --root $ROOT" ;; esac
[ "$DELETE" = 0 ] || [ "$MODE" = detach ] || die "--delete goes with detach"
# the tests' dry runs read files of their own; a real run reads only these
if [ "$DRY" = 1 ]; then
  FSTAB=${VOLUME_TEST_FSTAB:-$FSTAB}
  BYID=${VOLUME_TEST_BYID:-$BYID}
  GUARD=${VOLUME_TEST_GUARD:-$GUARD}
fi

# run CMD…: executes, or prints it under --dry-run
run() {
  if [ "$DRY" = 1 ]; then echo "+ $*"; else "$@"; fi
}
need_root() {
  [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (sudo)"
}
# json EXPR: reads JSON on stdin, prints python's EXPR of it (`d`)
json() { python3 -I -c "import json, sys; d = json.load(sys.stdin); print($1)"; }

# ---- workstation (scw) ----------------------------------------------------

# the volume's id, by its exact name; empty when there is none
find_volume() {
  scw block volume list name="$NAME" zone="$ZONE" -o json \
    | json "'\n'.join(v['id'] for v in (d or []) if v['name'] == '$NAME')"
}
find_server() {
  scw instance server list name="$SERVER" zone="$ZONE" -o json \
    | json "'\n'.join(s['id'] for s in (d or []) if s['name'] == '$SERVER')"
}
# the ids of the volumes attached to the server
server_volumes() {
  scw instance server get "$1" zone="$ZONE" -o json \
    | json "'\n'.join(v['id'] for v in (d.get('volumes') or {}).values())"
}
one() {  # one WHAT IDS: exactly one id, or none
  local n
  n=$(printf '%s' "$2" | grep -c . || true)
  [ "$n" -le 1 ] || die "$n $1 named that in $ZONE: give the right one by hand"
}

if [ "$MODE" = create ] || [ "$MODE" = detach ]; then
  command -v scw >/dev/null || die "scw not found (a workstation with the scw CLI)"
  srv=$(find_server); one servers "$srv"
  [ -n "$srv" ] || die "no server $SERVER in $ZONE"
  vol=${VOLID:-$(find_volume)}; one volumes "$vol"
  attached=0
  if [ -n "$vol" ] && server_volumes "$srv" | grep -qxF "$vol"; then attached=1; fi
fi

if [ "$MODE" = create ]; then
  if [ -z "$vol" ]; then
    run scw block volume create name="$NAME" perf-iops="$IOPS" \
      from-empty.size="${SIZE_GB}GB" zone="$ZONE"
    if [ "$DRY" = 1 ]; then vol="<volume-id>"; else
      vol=$(find_volume); one volumes "$vol"
      [ -n "$vol" ] || die "volume $NAME not found after its creation"
    fi
  else
    echo "volume $NAME: $vol (already there)"
  fi
  run scw block volume wait "$vol" zone="$ZONE"
  if [ "$attached" = 1 ]; then
    echo "volume $vol: already attached to $SERVER"
  else
    run scw instance server attach-volume server-id="$srv" volume-id="$vol" \
      volume-type=sbs_volume zone="$ZONE"
  fi
  echo "next, on $SERVER: sudo deploy/guest/volume-pilot.sh mount --volume-id $vol"
  exit 0
fi

if [ "$MODE" = detach ]; then
  [ -n "$vol" ] || { echo "no volume $NAME in $ZONE: nothing to do"; exit 0; }
  if [ "$attached" = 1 ]; then
    echo "(on $SERVER first: sudo deploy/guest/volume-pilot.sh unmount)"
    run scw instance server detach-volume volume-id="$vol" zone="$ZONE"
  else
    echo "volume $vol: not attached to $SERVER"
  fi
  if [ "$DELETE" = 1 ]; then
    run scw block volume wait "$vol" zone="$ZONE"
    run scw block volume delete "$vol" zone="$ZONE"
  fi
  exit 0
fi

# ---- host -----------------------------------------------------------------

# our fstab line, for the filesystem UUID
fstab_line() { printf 'UUID=%s %s ext4 %s 0 2\n' "$1" "$ROOT" "$OPTS"; }
# the fstab lines that mount something on $ROOT
fstab_root() { awk -v p="$ROOT" '$1 !~ /^#/ && $2 == p' "$FSTAB" 2>/dev/null || true; }
# write_fstab < content: /etc/fstab replaced atomically, the first original
# kept once in $FSTAB.ameesh-guest.orig
write_fstab() {
  local content
  content=$(cat)
  if [ "$DRY" = 1 ]; then printf -- '--- %s\n%s\n---\n' "$FSTAB" "$content"; return; fi
  [ -e "$FSTAB.$NAME.orig" ] || cp -p "$FSTAB" "$FSTAB.$NAME.orig"
  printf '%s\n' "$content" > "$FSTAB.tmp"
  findmnt --verify --tab-file "$FSTAB.tmp" >/dev/null 2>&1 \
    || { rm -f "$FSTAB.tmp"; die "the new $FSTAB does not verify (findmnt --verify): unchanged"; }
  chmod --reference="$FSTAB" "$FSTAB.tmp"
  mv -f "$FSTAB.tmp" "$FSTAB"
}
mounted_source() { findmnt -n -o SOURCE --mountpoint "$ROOT" 2>/dev/null || true; }

if [ "$MODE" = mount ] || [ "$MODE" = check ]; then
  [ "$MODE" = check ] || [ -n "$VOLID" ] || die "--volume-id ID (printed by create)"
fi

if [ "$MODE" = mount ]; then
  need_root
  link=$BYID/scsi-0SCW_sbs_volume-$VOLID
  for _ in $(seq 30); do [ -e "$link" ] && break; sleep 1; done
  [ -e "$link" ] || die "$link absent: is the volume attached (volume-pilot.sh create)?"
  dev=$(realpath -- "$link")
  [ "$(lsblk -dno TYPE "$dev")" = disk ] || die "$dev is not a disk"
  [ "$(lsblk -no NAME "$dev" | wc -l)" = 1 ] || die "$dev has partitions: refused"
  size=$(blockdev --getsize64 "$dev")
  [ "$size" -ge $((SIZE_GB * 1000 ** 3 * 95 / 100)) ] && [ "$size" -le $((SIZE_GB * 1000 ** 3 * 105 / 100)) ] \
    || die "$dev holds $size bytes, ${SIZE_GB} GB expected: refused"
  fstype=$(blkid -p -s TYPE -o value "$dev" 2>/dev/null || true)
  label=$(blkid -p -s LABEL -o value "$dev" 2>/dev/null || true)
  if [ -z "$fstype" ] && ! wipefs -n "$dev" 2>/dev/null | grep -q .; then
    # nothing on it, not even a partition table: the new volume
    run mkfs.ext4 -L "$NAME" "$dev"
  elif [ "$fstype" = ext4 ] && [ "$label" = "$NAME" ]; then
    echo "$dev: ext4 $NAME already there (kept)"
  else
    die "$dev carries ${fstype:-a signature} (label '${label}'): not formatted, refused"
  fi
  uuid=$(blkid -p -s UUID -o value "$dev" 2>/dev/null || true)
  [ -n "$uuid" ] || { [ "$DRY" = 1 ] && uuid="<uuid>"; } || die "$dev: no filesystem UUID"
  want=$(fstab_line "$uuid")
  have=$(fstab_root)
  if [ "$have" = "$want" ]; then
    echo "$FSTAB: $ROOT already there"
  elif [ -n "$have" ]; then
    die "$FSTAB already mounts something else on $ROOT: $have"
  else
    { cat "$FSTAB"; printf '%s\n' "$want"; } | write_fstab
  fi
  src=$(mounted_source)
  if [ -n "$src" ]; then
    [ "$(realpath -- "$src")" = "$dev" ] || die "$ROOT is already a mount point of $src"
    echo "$ROOT: already mounted"
  else
    if [ -d "$ROOT" ] && [ -n "$(ls -A "$ROOT")" ]; then
      die "$ROOT is not empty on the root disk: the volume would hide it; move it first"
    fi
    run install -d -m 0755 "$ROOT"
    run systemctl daemon-reload
    run mount "$ROOT"
  fi
  [ "$DRY" = 0 ] || exit 0
  MODE=check
fi

if [ "$MODE" = check ]; then
  ok=1
  src=$(mounted_source)
  if [ -z "$src" ]; then
    echo "$ROOT: NOT mounted"; ok=0
  else
    echo "$ROOT: mounted, $src, $(findmnt -n -o FSTYPE,SIZE --mountpoint "$ROOT")"
    if [ -n "$VOLID" ] && [ "$(realpath -- "$src")" != "$(realpath -m -- "$BYID/scsi-0SCW_sbs_volume-$VOLID")" ]; then
      echo "$ROOT: NOT the volume $VOLID"; ok=0
    fi
  fi
  case "$(fstab_root | grep -c .)" in
    1) echo "$FSTAB: $(fstab_root)" ;;
    *) echo "$FSTAB: NOT one line for $ROOT"; ok=0 ;;
  esac
  if [ -r "$GUARD" ] && [ -n "$src" ]; then
    size=$(findmnt -bn -o SIZE --mountpoint "$ROOT")
    python3 -I - "$GUARD" "$size" <<'EOF' || ok=0
import json, re, sys
raw = str(json.load(open(sys.argv[1]))["disk_max"])
m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]i?)?B?", raw)
unit = {None: 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
        "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}[m.group(2)]
disk_max, size = float(m.group(1)) * unit, int(sys.argv[2])
fits = size <= disk_max * 1.05
print("disk_max %s, volume %.1f GiB: %s" % (raw, size / 2**30, "ok" if fits else "NOT held"))
sys.exit(0 if fits else 1)
EOF
  fi
  [ "$ok" = 1 ] && echo "ok" || { echo "NOT ok"; exit 1; }
  exit 0
fi

if [ "$MODE" = unmount ]; then
  need_root
  active=$(systemctl list-units 'ameesh-guest-runner@*' --state=active --no-legend 2>/dev/null || true)
  [ -z "$active" ] || die "guest runners still active (install-pilot.sh --uninstall first): $active"
  if [ -n "$(mounted_source)" ]; then
    run umount "$ROOT"
  else
    echo "$ROOT: not mounted"
  fi
  # only our line: its mount point, type and options
  if [ -n "$(fstab_root)" ]; then
    awk -v p="$ROOT" -v o="$OPTS" '!($1 ~ /^UUID=/ && $2 == p && $3 == "ext4" && $4 == o)' "$FSTAB" \
      | write_fstab
    run systemctl daemon-reload
  fi
  [ -z "$(fstab_root)" ] || [ "$DRY" = 1 ] || die "$FSTAB still mounts something on $ROOT (not ours): left as is"
  if [ -d "$ROOT" ] && [ -z "$(ls -A "$ROOT")" ]; then run rmdir "$ROOT"; fi
  echo "next, from the workstation: deploy/guest/volume-pilot.sh detach [--delete]"
fi
