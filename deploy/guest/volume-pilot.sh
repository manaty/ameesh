#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# The guest's volume of the pilot (a1) on ameesh-agents-1 (lot L159g, the
# owner's decision 10408 of 2026-10-12, option a): a 40 GB Block Storage
# volume, 5K IOPS, mounted on /srv/ameesh-guest, holds the offer's
# `disk_max 40GiB`. Run by the owner; an agent never changes a host's disks.
# Every step is idempotent: run again, it does only what is missing.
#
#   volume-pilot.sh create --project-id P [--dry-run]
#                                            from a workstation with `scw`:
#                                            the volume, then attached
#   sudo volume-pilot.sh mount --volume-id ID [--dry-run]
#                                            on the host: ext4 if the disk
#                                            is blank, /etc/fstab, mounted
#   sudo volume-pilot.sh check [--volume-id ID]  on the host: what is in place
# Rollback, in this order (after `install-pilot.sh --uninstall`):
#   sudo volume-pilot.sh unmount --volume-id ID [--dry-run]
#                                            on the host: unmounted, its
#                                            /etc/fstab line removed; the
#                                            data stays on the volume
#   volume-pilot.sh detach --project-id P [--delete] [--dry-run]
#                                            from the workstation: detached;
#                                            --delete also deletes it (data
#                                            lost, billing stops)
#
# Options:
#   --dry-run            print every command and file, change nothing
#   --project-id P       the ameesh Scaleway project (`scw account project
#                        list`): every list is filtered by it and every
#                        server or volume checked against it; never the
#                        CLI's default project
#   --volume-id ID       the volume (printed by `create`)
#   --server NAME        the instance (default ameesh-agents-1)
#   --zone Z             default fr-par-1
#   --root DIR           mount point (default /srv/ameesh-guest)
#
# The disk is found by the volume's id (/dev/disk/by-id/scsi-0SCW_sbs_volume-ID),
# never guessed. It is formatted only when both probes (blkid, wipefs)
# succeed and find no signature at all; a probe that fails refuses. An ext4
# labelled ameesh-guest is kept as is; anything else is refused. /etc/fstab
# is written last, once every check passed. A volume mounted by hand before
# (its line LABEL=ameesh-guest, our options) is adopted as it is, line
# included, once that label is shown to name this volume and no other disk,
# and the filesystem mounted is the volume's (its UUID). `unmount` and `detach --delete`
# act only on the pilot's volume: its name, project and size checked first,
# the mounted filesystem's source and UUID too.
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
PROJECT=
VOLID=
DRY=0
DELETE=0
FSTAB=/etc/fstab
BYID=/dev/disk/by-id
BYLABEL=/dev/disk/by-label
BYUUID=/dev/disk/by-uuid
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
    --project-id) PROJECT=${2:?}; shift 2 ;;
    --volume-id) VOLID=${2:?}; shift 2 ;;
    --server) SERVER=${2:?}; shift 2 ;;
    --zone) ZONE=${2:?}; shift 2 ;;
    --root) ROOT=${2:?}; shift 2 ;;
    *) die "unknown option $1" ;;
  esac
done
uuid_like() {  # uuid_like OPTION VALUE: empty, or a lowercase UUID
  case "$2" in ''|[0-9a-f]*-*-*-*-*) ;; *) die "invalid $1 $2" ;; esac
  case "$2" in *[!0-9a-f-]*) die "invalid $1 $2" ;; esac
}
uuid_like --volume-id "$VOLID"
uuid_like --project-id "$PROJECT"
case "$ROOT" in /srv/?*) ;; *) die "--root under /srv expected" ;; esac
case "$ROOT" in *[!A-Za-z0-9/._-]*|*/..*|*//*|*/) die "invalid --root $ROOT" ;; esac
case "$SERVER" in ''|*[!A-Za-z0-9._-]*) die "invalid --server $SERVER" ;; esac
[ "$DELETE" = 0 ] || [ "$MODE" = detach ] || die "--delete goes with detach"
# the tests' dry runs read files of their own; a real run reads only these
if [ "$DRY" = 1 ]; then
  FSTAB=${VOLUME_TEST_FSTAB:-$FSTAB}
  BYID=${VOLUME_TEST_BYID:-$BYID}
  BYLABEL=${VOLUME_TEST_BYLABEL:-$BYLABEL}
  BYUUID=${VOLUME_TEST_BYUUID:-$BYUUID}
  GUARD=${VOLUME_TEST_GUARD:-$GUARD}
fi
DIR=$ROOT   # the mount point's directory, read and removed
[ "$DRY" = 0 ] || DIR=${VOLUME_TEST_DIR:-$DIR}

# run CMD…: executes, or prints it under --dry-run
run() {
  if [ "$DRY" = 1 ]; then echo "+ $*"; else "$@"; fi
}
need_root() {
  [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (sudo)"
}
# json CODE ARG…: python CODE on the JSON read from stdin (`d`, the args `a`);
# a shape it does not expect raises, and the script stops (set -e, pipefail)
json() {
  local code=$1; shift
  python3 -I -c "import json, sys
d = json.load(sys.stdin); a = sys.argv[1:]
$code" "$@"
}

# ---- workstation (scw) ----------------------------------------------------

# the volumes named $NAME in the project, one id a line
find_volume() {
  scw block volume list name="$NAME" project-id="$PROJECT" zone="$ZONE" -o json \
    | json "assert isinstance(d, list), 'a list of volumes expected'
print('\n'.join(v['id'] for v in d if v['name'] == a[0] and v['project_id'] == a[1]))" \
      "$NAME" "$PROJECT"
}
find_server() {
  scw instance server list name="$SERVER" project-id="$PROJECT" zone="$ZONE" -o json \
    | json "assert isinstance(d, list), 'a list of servers expected'
print('\n'.join(s['id'] for s in d if s['name'] == a[0] and s['project'] == a[1]))" \
      "$SERVER" "$PROJECT"
}
# the ids of the volumes attached to the server, which must be ours: a
# failed fetch or another shape stops the script, never reads as "none"
server_volumes() {
  scw instance server get "$1" zone="$ZONE" -o json \
    | json "assert d['id'] == a[0] and d['name'] == a[1] and d['project'] == a[2], \
    'server %s is not %s in the project' % (d.get('id'), a[1])
vols = d['volumes']
assert isinstance(vols, dict), 'volumes: a mapping expected'
ids = [v['id'] for v in vols.values()]
assert all(isinstance(i, str) and i for i in ids), 'volume ids expected'
print('\n'.join(ids))" "$1" "$SERVER" "$PROJECT"
}
# pilot_volume ID: the volume is the pilot's (name, project, size), or stop;
# prints its status
pilot_volume() {
  scw block volume get "$1" zone="$ZONE" -o json \
    | json "bad = [k for k, want in (('id', a[0]), ('name', a[1]), ('project_id', a[2]),
                               ('size', int(a[3]))) if d.get(k) != want]
if bad:
    sys.exit('volume-pilot: volume %s is not the pilot volume (%s): refused'
             % (a[0], ', '.join('%s %r' % (k, d.get(k)) for k in bad)))
print(d['status'])" "$1" "$NAME" "$PROJECT" "$((SIZE_GB * 1000 ** 3))"
}
one() {  # one WHAT IDS: exactly one id, or none
  local n
  n=$(printf '%s' "$2" | grep -c . || true)
  [ "$n" -le 1 ] || die "$n $1 named that in $ZONE: give the right one by hand"
}

if [ "$MODE" = create ] || [ "$MODE" = detach ]; then
  command -v scw >/dev/null || die "scw not found (a workstation with the scw CLI)"
  [ -n "$PROJECT" ] || die "--project-id P (scw account project list): the CLI's default project is not used"
  srv=$(find_server); one servers "$srv"
  [ -n "$srv" ] || die "no server $SERVER in $ZONE in project $PROJECT"
  if [ -n "$VOLID" ]; then vol=$VOLID; else vol=$(find_volume); one volumes "$vol"; fi
  status=
  [ -z "$vol" ] || status=$(pilot_volume "$vol")
  vols=$(server_volumes "$srv")
  attached=0
  if [ -n "$vol" ] && printf '%s\n' "$vols" | grep -qxF "$vol"; then attached=1; fi
fi

if [ "$MODE" = create ]; then
  if [ -z "$vol" ]; then
    run scw block volume create name="$NAME" perf-iops="$IOPS" \
      from-empty.size="${SIZE_GB}GB" project-id="$PROJECT" zone="$ZONE"
    if [ "$DRY" = 1 ]; then vol="<volume-id>"; else
      vol=$(find_volume); one volumes "$vol"
      [ -n "$vol" ] || die "volume $NAME not found after its creation"
      pilot_volume "$vol" >/dev/null
    fi
  else
    echo "volume $NAME: $vol (already there, $status)"
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
  [ -n "$vol" ] || { echo "no volume $NAME in $ZONE in project $PROJECT: nothing to do"; exit 0; }
  if [ "$attached" = 1 ]; then
    echo "(on $SERVER first: sudo deploy/guest/volume-pilot.sh unmount --volume-id $vol)"
    run scw instance server detach-volume server-id="$srv" volume-id="$vol" zone="$ZONE"
  else
    echo "volume $vol: not attached to $SERVER ($status)"
  fi
  if [ "$DELETE" = 1 ]; then
    # in use elsewhere: Scaleway refuses the deletion, it is refused here first
    [ "$attached" = 1 ] || [ "$status" = available ] \
      || die "volume $vol is $status, not attached to $SERVER: not deleted"
    run scw block volume wait "$vol" terminal-status=available zone="$ZONE"
    run scw block volume delete "$vol" zone="$ZONE"
  fi
  exit 0
fi

# ---- host -----------------------------------------------------------------

# our fstab line, for the filesystem UUID
fstab_line() { printf 'UUID=%s %s ext4 %s 0 2\n' "$1" "$ROOT" "$OPTS"; }
# the by-hand line (PILOT-ameesh-agents-1.md, a volume mounted before this
# script): ours only once its label is shown to be the volume's
label_line() { printf 'LABEL=%s %s ext4 %s 0 2\n' "$NAME" "$ROOT" "$OPTS"; }
# label_is DEV / uuid_is UUID DEV: udev's link of that label or UUID is DEV
label_is() { [ "$(realpath -e -- "$BYLABEL/$NAME" 2>/dev/null)" = "$1" ]; }
uuid_is() { [ -n "$1" ] && [ "$(realpath -e -- "$BYUUID/$1" 2>/dev/null)" = "$2" ]; }
# label_only DEV (root): LABEL=$NAME names DEV and no other disk, every
# device probed again (blkid without its cache); the line would mount
# another disk at the next boot otherwise
label_only() {
  local devs
  label_is "$1" || return 1
  devs=$(blkid -c /dev/null -o device -t "LABEL=$NAME" 2>/dev/null) || return 1
  [ "$(printf '%s\n' "$devs" | xargs -r -n1 realpath -- | sort -u)" = "$1" ]
}
# not_ours UUID < fstab: the lines that are neither ours for UUID nor the
# by-hand line of PILOT-ameesh-agents-1.md (LABEL=ameesh-guest, same options)
not_ours() {
  awk -v u="UUID=$1" -v l="LABEL=$NAME" -v p="$ROOT" -v o="$OPTS" \
    '!(($1 == u || $1 == l) && $2 == p && $3 == "ext4" && $4 == o)'
}
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
# the UUID of the filesystem mounted on $ROOT; empty when none
mounted_uuid() { findmnt -n -o UUID --mountpoint "$ROOT" 2>/dev/null || true; }
mounted_label() { findmnt -n -o LABEL --mountpoint "$ROOT" 2>/dev/null || true; }
# probe DEV: sets fstype, label and uuid from blkid's low-level probe; all
# empty only when blkid says it found nothing (exit 2). Any other failure
# (I/O error, ambiguous signatures, blkid missing) stops the script.
probe() {
  local out rc=0
  fstype= label= uuid=
  out=$(blkid -p -o export "$1" 2>&1) || rc=$?
  case "$rc" in
    0) ;;
    2) return 0 ;;
    *) die "$1: blkid failed ($rc): ${out:-no output}; refused" ;;
  esac
  fstype=$(printf '%s\n' "$out" | sed -n 's/^TYPE=//p')
  label=$(printf '%s\n' "$out" | sed -n 's/^LABEL=//p')
  uuid=$(printf '%s\n' "$out" | sed -n 's/^UUID=//p')
  [ -n "$fstype" ] || die "$1: blkid found a signature without a type: refused"
}
# volume_dev: the volume's disk, by its id
volume_dev() { printf '%s\n' "$BYID/scsi-0SCW_sbs_volume-$VOLID"; }

if [ "$MODE" != create ] && [ "$MODE" != detach ]; then
  [ "$MODE" = check ] || [ -n "$VOLID" ] || die "--volume-id ID (printed by create)"
fi

if [ "$MODE" = mount ]; then
  need_root
  link=$(volume_dev)
  for _ in $(seq 30); do [ -e "$link" ] && break; sleep 1; done
  [ -e "$link" ] || die "$link absent: is the volume attached (volume-pilot.sh create)?"
  dev=$(realpath -- "$link")
  [ "$(lsblk -dno TYPE "$dev")" = disk ] || die "$dev is not a disk"
  [ "$(lsblk -no NAME "$dev" | wc -l)" = 1 ] || die "$dev has partitions: refused"
  size=$(blockdev --getsize64 "$dev")
  [ "$size" -ge $((SIZE_GB * 1000 ** 3 * 95 / 100)) ] && [ "$size" -le $((SIZE_GB * 1000 ** 3 * 105 / 100)) ] \
    || die "$dev holds $size bytes, ${SIZE_GB} GB expected: refused"
  probe "$dev"
  format=0
  if [ -z "$fstype" ]; then
    # blkid found nothing; wipefs must succeed and list nothing either
    sigs=$(wipefs -n "$dev" 2>&1) || die "$dev: wipefs failed: ${sigs:-no output}; refused"
    [ -z "$sigs" ] || die "$dev carries a signature ($sigs): not formatted, refused"
    format=1   # nothing on it, not even a partition table: the new volume
  elif [ "$fstype" = ext4 ] && [ "$label" = "$NAME" ]; then
    [ -n "$uuid" ] || die "$dev: ext4 without a UUID: refused"
  else
    die "$dev carries $fstype (label '${label}'): not formatted, refused"
  fi
  # every check before anything changes: the mount point, its directory, fstab
  have=$(fstab_root)
  src=$(mounted_source)
  if [ -n "$src" ]; then
    [ "$(realpath -- "$src")" = "$dev" ] || die "$ROOT is already a mount point of $src"
    [ "$format" = 1 ] || [ "$(mounted_uuid)" = "$uuid" ] \
      || die "$ROOT: mounted UUID $(mounted_uuid), not the volume's $uuid: refused"
  elif [ -d "$DIR" ] && [ -n "$(ls -A -- "$DIR")" ]; then
    die "$ROOT is not empty on the root disk: the volume would hide it; move it first"
  fi
  if [ "$format" = 1 ]; then
    [ -z "$have" ] || die "$FSTAB already mounts something on $ROOT: $have"
    run mkfs.ext4 -L "$NAME" "$dev"
    if [ "$DRY" = 1 ]; then uuid="<uuid>"; else
      probe "$dev"
      [ "$fstype" = ext4 ] && [ -n "$uuid" ] || die "$dev: no ext4 UUID after mkfs"
    fi
  else
    echo "$dev: ext4 $NAME already there (kept)"
  fi
  want=$(fstab_line "$uuid")
  if [ "$have" = "$want" ]; then
    echo "$FSTAB: $ROOT already there"
  elif [ "$format" = 0 ] && [ "$have" = "$(label_line)" ]; then
    # mounted by hand before this script: adopted as is, if the label is
    # this volume's and no other disk's
    label_only "$dev" \
      || die "$FSTAB mounts LABEL=$NAME on $ROOT, a label not only the volume $VOLID's: refused"
    echo "$FSTAB: $ROOT by LABEL=$NAME, the volume's (adopted, kept as is)"
    want=$have
  elif [ -n "$have" ]; then
    die "$FSTAB already mounts something else on $ROOT: $have"
  fi
  # fstab last, then the mount
  [ "$have" = "$want" ] || { cat "$FSTAB"; printf '%s\n' "$want"; } | write_fstab
  if [ -n "$src" ]; then
    echo "$ROOT: already mounted"
  else
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
  muuid=
  if [ -z "$src" ]; then
    echo "$ROOT: NOT mounted"; ok=0
  else
    echo "$ROOT: mounted, $src, $(findmnt -n -o FSTYPE,SIZE --mountpoint "$ROOT")"
    if [ -n "$VOLID" ] && [ "$(realpath -- "$src")" != "$(realpath -m -- "$(volume_dev)")" ]; then
      echo "$ROOT: NOT the volume $VOLID"; ok=0
    fi
    muuid=$(mounted_uuid)
    [ -n "$muuid" ] || { echo "$ROOT: NO filesystem UUID"; ok=0; }
  fi
  lines=$(fstab_root)
  if [ "$(printf '%s' "$lines" | grep -c . || true)" != 1 ]; then
    echo "$FSTAB: NOT one line for $ROOT"; ok=0
  elif [ -n "$muuid" ] && [ "$lines" = "$(label_line)" ]; then
    # the by-hand line: ours when the label and the mounted UUID are the
    # volume's (udev's links), the mounted filesystem labelled so
    echo "$FSTAB: $lines"
    vdev=$(realpath -m -- "$(volume_dev)")
    if [ -z "$VOLID" ]; then
      echo "$FSTAB: LABEL=$NAME NOT checked: give --volume-id"; ok=0
    elif [ "$(mounted_label)" = "$NAME" ] && label_is "$vdev" && uuid_is "$muuid" "$vdev"; then
      echo "$FSTAB: LABEL=$NAME is the volume $VOLID (UUID $muuid)"
    else
      echo "$FSTAB: LABEL=$NAME NOT the volume $VOLID's (mounted label '$(mounted_label)', UUID $muuid)"; ok=0
    fi
  elif [ -n "$muuid" ] && [ "$lines" != "$(fstab_line "$muuid")" ]; then
    # the line must mount the filesystem mounted now, with our options
    echo "$FSTAB: $lines"
    echo "$FSTAB: NOT the mounted filesystem's line, expected: $(fstab_line "$muuid")"; ok=0
  else
    echo "$FSTAB: $lines"
  fi
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
  # the volume's filesystem, by its disk: what is mounted on $ROOT and the
  # fstab line must be that one, or nothing is touched
  link=$(volume_dev)
  [ -e "$link" ] || die "$link absent: the volume is not attached here; $FSTAB and $ROOT left as they are"
  dev=$(realpath -- "$link")
  probe "$dev"
  [ "$fstype" = ext4 ] && [ "$label" = "$NAME" ] && [ -n "$uuid" ] \
    || die "$dev is not the ext4 $NAME (${fstype:-no signature}, label '${label}'): nothing touched"
  src=$(mounted_source)
  if [ -n "$src" ]; then
    [ "$(realpath -- "$src")" = "$dev" ] || die "$ROOT is a mount point of $src, not of the volume $VOLID: nothing touched"
    [ "$(mounted_uuid)" = "$uuid" ] || die "$ROOT: mounted UUID $(mounted_uuid), not $uuid: nothing touched"
  fi
  # the fstab lines on $ROOT must all be ours before anything changes
  others=$(not_ours "$uuid" < "$FSTAB" | awk -v p="$ROOT" '$1 !~ /^#/ && $2 == p')
  [ -z "$others" ] || die "$FSTAB mounts something else on $ROOT: $others; nothing touched"
  if fstab_root | grep -qxF "$(label_line)"; then
    label_only "$dev" \
      || die "$FSTAB mounts LABEL=$NAME on $ROOT, a label not only the volume $VOLID's: nothing touched"
  fi
  if [ -n "$src" ]; then run umount "$ROOT"; else echo "$ROOT: not mounted"; fi
  if [ -n "$(fstab_root)" ]; then
    not_ours "$uuid" < "$FSTAB" | write_fstab
    run systemctl daemon-reload
  fi
  if [ -d "$DIR" ] && [ -z "$(ls -A -- "$DIR")" ]; then run rmdir "$ROOT"; fi
  echo "next, from the workstation: deploy/guest/volume-pilot.sh detach --project-id P [--delete]"
fi
