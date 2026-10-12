# SPDX-License-Identifier: AGPL-3.0-only
"""The guest's volume of the pilot (lot L159g, the owner's decision 10408):
deploy/guest/volume-pilot.sh, in dry runs only, `scw` and the disk tools
stubbed on PATH."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

from .support import REPO

SCRIPT = os.path.join(REPO, "deploy", "guest", "volume-pilot.sh")
VOL = "11111111-2222-3333-4444-555555555555"
SRV = "99999999-8888-7777-6666-555555555555"
PROJ = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ELSEWHERE = "ffffffff-0000-0000-0000-000000000000"
OPTS = "defaults,nofail,x-systemd.device-timeout=30s"

# scw, from the state in SCW_STUB: the lists ignore their filters (the
# script must check the project itself) but demand project-id=; a list or a
# get named in "fail" fails; "server" replaces the fetched server's JSON;
# "get" overrides a volume's fields
SCW = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
st = json.loads(os.environ["SCW_STUB"])
proj, att = st.get("project", "%s"), st["attached"]
def vol(i):
    v = {"id": i, "name": "ameesh-guest", "project_id": proj, "size": 40 * 10 ** 9,
         "status": "in_use" if i in att else "available"}
    v.update(st.get("get", {}).get(i, {}))
    return v
key = " ".join(a[:3])
if key in st.get("fail", []):
    sys.exit("scw stub: %%s failed" %% key)
if a[2] == "list" and not any(x.startswith("project-id=") for x in a):
    sys.exit("scw stub: a list without project-id=")
if key == "block volume list":
    print(json.dumps([vol(i) for i in st["volumes"]]
                     + [{"id": "other", "name": "ameesh-guest-old", "project_id": proj},
                        {"id": "x-elsewhere", "name": "ameesh-guest", "project_id": "%s"}]))
elif key == "block volume get":
    print(json.dumps(vol(a[3])))
elif key == "instance server list":
    print(json.dumps([{"id": i, "name": "ameesh-agents-1", "project": proj} for i in st["servers"]]
                     + [{"id": "s-elsewhere", "name": "ameesh-agents-1", "project": "%s"}]))
elif key == "instance server get":
    print(json.dumps(st["server"]) if "server" in st else json.dumps(
        {"id": a[3], "name": "ameesh-agents-1", "project": proj,
         "volumes": {str(n): {"id": i} for n, i in enumerate(att)}}))
else:
    sys.exit("scw stub: unexpected %%s" %% a)
""" % (PROJ, ELSEWHERE, ELSEWHERE)


class VolumeScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.bin = os.path.join(d, "bin")
        os.mkdir(self.bin)
        self.stub("scw", SCW)
        # the disk tools read the stub's state in the environment;
        # STUB_FAIL=<tool> makes that one fail, its output empty
        fail = '[ "$STUB_FAIL" != %s ] || { echo "%s: I/O error" >&2; exit 1; }\n'
        self.stub("lsblk", '#!/bin/sh\n' + fail % ("lsblk", "lsblk")
                  + 'case "$*" in *TYPE*) echo disk ;; *) printf "%s" "$STUB_LSBLK" ;; esac\n')
        self.stub("blockdev", '#!/bin/sh\n' + fail % ("blockdev", "blockdev") + 'echo "$STUB_SIZE"\n')
        # systemctl list-units: the active guest runners, STUB_RUNNERS
        self.stub("systemctl", '#!/bin/sh\n' + fail % ("systemctl", "systemctl")
                  + 'printf "%s" "$STUB_RUNNERS"\n')
        # blkid -p -o export: STUB_BLKID_RC forces a failure; no type, exit 2
        # blkid -c /dev/null -o device -t LABEL=…: the disks in STUB_LABELDEVS
        self.stub("blkid", '#!/bin/sh\n'
                           'case "$*" in *"-t LABEL="*) printf "%s" "$STUB_LABELDEVS"; exit 0 ;; esac\n'
                           '[ -z "$STUB_BLKID_RC" ] || { echo "blkid: I/O error" >&2; exit "$STUB_BLKID_RC"; }\n'
                           '[ -n "$STUB_TYPE" ] || exit 2\n'
                           'echo "DEVNAME=$3"\n'
                           '[ -z "$STUB_UUID" ] || echo "UUID=$STUB_UUID"\n'
                           '[ -z "$STUB_LABEL" ] || echo "LABEL=$STUB_LABEL"\n'
                           'echo "TYPE=$STUB_TYPE"\n')
        self.stub("wipefs", '#!/bin/sh\nprintf "%s" "$STUB_WIPEFS"\nexit "${STUB_WIPEFS_RC:-0}"\n')
        # findmnt: the table holds / and, with STUB_SRC, the mount point
        self.stub("findmnt", '#!/bin/sh\n' + fail % ("findmnt", "findmnt")
                             + 'case "$*" in *TARGET*) echo /; [ -z "$STUB_SRC" ] '
                             '|| echo /srv/ameesh-guest; exit 0 ;; esac\n'
                             '[ -n "$STUB_SRC" ] || exit 1\n'
                             'case "$*" in *FSTYPE*) echo "ext4 37.1G" ;; *SOURCE*) echo "$STUB_SRC" ;;\n'
                             '  *UUID*) echo "$STUB_MUUID" ;; *LABEL*) echo "$STUB_MLABEL" ;;\n'
                             '  *SIZE*) echo 40000000000 ;; esac\n')
        self.byid = os.path.join(d, "by-id")
        os.mkdir(self.byid)
        self.disk = os.path.join(d, "sdb")
        open(self.disk, "w").close()
        os.symlink(self.disk, os.path.join(self.byid, "scsi-0SCW_sbs_volume-" + VOL))
        self.fstab = os.path.join(d, "fstab")
        with open(self.fstab, "w") as fh:
            fh.write("LABEL=cloudimg-rootfs / ext4 discard,errors=remount-ro 0 1\n")
        self.root = os.path.join(d, "srv", "ameesh-guest")
        # udev's links by label and by UUID: none until a test makes them
        self.bylabel = os.path.join(d, "by-label")
        self.byuuid = os.path.join(d, "by-uuid")
        os.mkdir(self.bylabel)
        os.mkdir(self.byuuid)

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def run_script(self, *args, scw=None, project=PROJ, **disk):
        env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"],
                   VOLUME_TEST_FSTAB=self.fstab, VOLUME_TEST_BYID=self.byid,
                   VOLUME_TEST_BYLABEL=self.bylabel, VOLUME_TEST_BYUUID=self.byuuid,
                   VOLUME_TEST_GUARD=os.path.join(self.tmp.name, "guard.json"),
                   SCW_STUB=json.dumps(scw or {"volumes": [], "servers": [SRV], "attached": []}),
                   STUB_LSBLK="sdb\n", STUB_SIZE=str(40 * 10 ** 9), STUB_TYPE="",
                   STUB_LABEL="", STUB_UUID="", STUB_WIPEFS="", STUB_WIPEFS_RC="0",
                   STUB_BLKID_RC="", STUB_SRC="", STUB_MUUID="", STUB_MLABEL="",
                   STUB_LABELDEVS=self.disk + "\n", STUB_FAIL="", STUB_RUNNERS="",
                   VOLUME_TEST_DIR=self.root)
        env.update({"STUB_" + k.upper(): v for k, v in disk.items()})
        if args[0] in ("create", "detach") and project and "--project-id" not in args:
            args += ("--project-id", project)
        return subprocess.run([SCRIPT, *args, "--dry-run"], env=env, text=True,
                              capture_output=True, timeout=60)

    def cmds(self, proc):
        return [line[2:] for line in proc.stdout.splitlines() if line.startswith("+ ")]

    def label_line(self):
        with open(self.fstab, "a") as fh:
            fh.write("LABEL=ameesh-guest /srv/ameesh-guest ext4 %s 0 2\n" % OPTS)

    def udev(self, uuid, label_to=None):
        """udev's links: the label ameesh-guest and UUID to the volume's disk
        (or the label to LABEL_TO), in place of the links before."""
        for d in (self.bylabel, self.byuuid):
            for name in os.listdir(d):
                os.remove(os.path.join(d, name))
        os.symlink(label_to or self.disk, os.path.join(self.bylabel, "ameesh-guest"))
        os.symlink(self.disk, os.path.join(self.byuuid, uuid))

    def our_line(self, uuid):
        with open(self.fstab, "a") as fh:
            fh.write("UUID=%s /srv/ameesh-guest ext4 %s 0 2\n" % (uuid, OPTS))

    # ---- create / detach (workstation) ----

    def test_create_from_nothing(self):
        p = self.run_script("create")
        self.assertEqual(p.returncode, 0, p.stderr)
        c = self.cmds(p)
        self.assertEqual(c[0], "scw block volume create name=ameesh-guest perf-iops=5000 "
                               "from-empty.size=40GB project-id=%s zone=fr-par-1" % PROJ)
        self.assertIn("scw instance server attach-volume server-id=%s volume-id=<volume-id> "
                      "volume-type=sbs_volume zone=fr-par-1" % SRV, c)

    def test_create_again_changes_nothing(self):
        p = self.run_script("create", scw={"volumes": [VOL], "servers": [SRV], "attached": ["root", VOL]})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.cmds(p), ["scw block volume wait %s zone=fr-par-1" % VOL])
        self.assertIn("mount --volume-id " + VOL, p.stdout)

    def test_create_attaches_an_existing_volume(self):
        p = self.run_script("create", scw={"volumes": [VOL], "servers": [SRV], "attached": ["root"]})
        self.assertEqual(p.returncode, 0, p.stderr)
        c = self.cmds(p)
        self.assertFalse([x for x in c if "volume create" in x])
        self.assertTrue([x for x in c if "attach-volume" in x and VOL in x])

    def test_two_volumes_of_that_name_are_refused(self):
        p = self.run_script("create", scw={"volumes": [VOL, "x"], "servers": [SRV], "attached": []})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("2 volumes", p.stderr)
        self.assertEqual(self.cmds(p), [])

    def test_the_project_is_pinned(self):
        # codex #436 r1 P1: never the CLI's default project
        p = self.run_script("create", project=None)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--project-id", p.stderr)
        self.assertEqual(self.cmds(p), [])
        p = self.run_script("detach", "--project-id", "not-a-uuid")
        self.assertIn("invalid --project-id", p.stderr)
        # the stub's lists demand project-id= and also return a server and a
        # volume of that name in another project: not counted, not chosen
        p = self.run_script("detach", "--delete", scw={"volumes": [VOL], "servers": [SRV],
                                                       "attached": ["root", VOL]})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("elsewhere", p.stdout)
        # a server or a volume that is not the project's is refused
        p = self.run_script("detach", scw={"volumes": [VOL], "servers": [SRV], "attached": [VOL],
                                           "server": {"id": SRV, "name": "ameesh-agents-1",
                                                      "project": ELSEWHERE, "volumes": {}}})
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.cmds(p), [])
        p = self.run_script("detach", "--delete", "--volume-id", VOL,
                            scw={"volumes": [], "servers": [SRV], "attached": [],
                                 "get": {VOL: {"project_id": ELSEWHERE}}})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not the pilot volume (project_id", p.stderr)
        self.assertEqual(self.cmds(p), [])

    def test_detach_keeps_the_volume_unless_delete(self):
        scw = {"volumes": [VOL], "servers": [SRV], "attached": ["root", VOL]}
        p = self.run_script("detach", scw=scw)
        self.assertEqual(p.returncode, 0, p.stderr)
        # codex #436 r1 P2: detach-volume needs the server's id
        self.assertEqual(self.cmds(p), ["scw instance server detach-volume server-id=%s "
                                        "volume-id=%s zone=fr-par-1" % (SRV, VOL)])
        self.assertIn("unmount --volume-id " + VOL, p.stdout)
        c = self.cmds(self.run_script("detach", "--delete", scw=scw))
        self.assertEqual(c[1:], ["scw block volume wait %s terminal-status=available "
                                 "zone=fr-par-1" % VOL,
                                 "scw block volume delete %s zone=fr-par-1" % VOL])

    def test_delete_only_the_pilot_volume(self):
        # codex #436 r1 P1: --delete --volume-id OTHER, never attached here
        other = "22222222-3333-4444-5555-666666666666"
        for fields in ({"name": "backups"}, {"size": 150 * 10 ** 9},
                       {"project_id": ELSEWHERE}):
            p = self.run_script("detach", "--delete", "--volume-id", other,
                                scw={"volumes": [VOL], "servers": [SRV], "attached": ["root"],
                                     "get": {other: fields}})
            self.assertNotEqual(p.returncode, 0, fields)
            self.assertIn("not the pilot volume", p.stderr)
            self.assertEqual(self.cmds(p), [], fields)
        # the pilot's volume, but in use by another server: not deleted
        p = self.run_script("detach", "--delete", scw={"volumes": [VOL], "servers": [SRV],
                                                       "attached": ["root"],
                                                       "get": {VOL: {"status": "in_use"}}})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("in_use, not attached", p.stderr)
        self.assertEqual(self.cmds(p), [])
        # the pilot's volume, detached already: deleted
        p = self.run_script("detach", "--delete", scw={"volumes": [VOL], "servers": [SRV],
                                                       "attached": ["root"]})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.cmds(p)[-1], "scw block volume delete %s zone=fr-par-1" % VOL)

    def test_a_server_fetch_that_fails_stops(self):
        # codex #436 r1 P2: never read as "not attached"
        scw = {"volumes": [VOL], "servers": [SRV], "attached": [VOL]}
        for mode in ("create", "detach"):
            for bad in ({"fail": ["instance server get"]},
                        {"server": {"id": SRV, "name": "ameesh-agents-1", "project": PROJ,
                                    "volumes": [VOL]}},
                        {"server": {"id": SRV, "name": "ameesh-agents-1", "project": PROJ}},
                        {"fail": ["block volume get"]}):
                p = self.run_script(mode, "--delete", scw=dict(scw, **bad)) if mode == "detach" \
                    else self.run_script(mode, scw=dict(scw, **bad))
                self.assertNotEqual(p.returncode, 0, (mode, bad))
                self.assertEqual(self.cmds(p), [], (mode, bad))

    def test_delete_only_with_detach(self):
        p = self.run_script("create", "--delete")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--delete goes with detach", p.stderr)

    # ---- mount / unmount (host) ----

    def test_mount_formats_a_blank_disk(self):
        p = self.run_script("mount", "--volume-id", VOL)
        self.assertEqual(p.returncode, 0, p.stderr)
        c = self.cmds(p)
        self.assertEqual(c[0], "mkfs.ext4 -L ameesh-guest " + self.disk)
        self.assertEqual(c[-1], "mount /srv/ameesh-guest")
        # the original lines kept, ours appended
        self.assertIn("LABEL=cloudimg-rootfs / ext4", p.stdout)
        self.assertIn("UUID=<uuid> /srv/ameesh-guest ext4 %s 0 2" % OPTS, p.stdout)
        # codex #436 r1 P1: fstab written after mkfs, just before the mount
        out = p.stdout
        self.assertLess(out.index("+ mkfs.ext4"), out.index("--- " + self.fstab))
        self.assertLess(out.index("--- " + self.fstab), out.index("+ install -d"))

    def test_mount_again_changes_nothing_on_the_disk(self):
        uuid = "abcd-ef"
        self.our_line(uuid)
        p = self.run_script("mount", "--volume-id", VOL, type="ext4", label="ameesh-guest",
                            uuid=uuid)
        self.assertEqual(p.returncode, 0, p.stderr)
        c = self.cmds(p)
        self.assertFalse([x for x in c if x.startswith("mkfs")])
        self.assertNotIn("--- " + self.fstab, p.stdout)
        self.assertIn("already there (kept)", p.stdout)

    def test_mount_never_formats_a_disk_with_data(self):
        for disk in ({"type": "xfs"}, {"type": "ext4", "label": "data"},
                     {"wipefs": "0x1fe dos 55 aa\n"}):
            p = self.run_script("mount", "--volume-id", VOL, **disk)
            self.assertNotEqual(p.returncode, 0, disk)
            self.assertIn("not formatted, refused", p.stderr)
            self.assertEqual(self.cmds(p), [], disk)

    def test_mount_refuses_when_a_probe_fails(self):
        # codex #436 r1 P1: a failed probe is not a blank disk
        for disk in ({"blkid_rc": "4"}, {"blkid_rc": "8"}, {"blkid_rc": "127"},
                     {"wipefs_rc": "1"}, {"wipefs_rc": "1", "wipefs": ""}):
            p = self.run_script("mount", "--volume-id", VOL, **disk)
            self.assertNotEqual(p.returncode, 0, disk)
            self.assertIn("refused", p.stderr)
            self.assertEqual(self.cmds(p), [], disk)
            self.assertNotIn("--- " + self.fstab, p.stdout)

    def test_every_probe_must_succeed(self):
        # codex #436 r2 P1: a probe that fails (its output empty) is never
        # read as "nothing there": refused, nothing run, fstab untouched
        ours = {"type": "ext4", "label": "ameesh-guest", "uuid": "u"}
        self.our_line("u")
        for mode, tool, disk in (("mount", "lsblk", {}), ("mount", "blockdev", {}),
                                 ("mount", "findmnt", {}), ("mount", "findmnt", ours),
                                 ("unmount", "findmnt", ours), ("unmount", "systemctl", ours),
                                 ("check", "findmnt", {})):
            args = (mode, "--volume-id", VOL)
            p = self.run_script(*args, fail=tool, **disk)
            self.assertNotEqual(p.returncode, 0, (mode, tool))
            self.assertIn("failed", p.stderr, (mode, tool))
            self.assertEqual(self.cmds(p), [], (mode, tool))
            self.assertNotIn("--- " + self.fstab, p.stdout, (mode, tool))
        # an fstab that cannot be read is not an empty one
        os.remove(self.fstab)
        for mode in ("mount", "unmount", "check"):
            p = self.run_script(mode, "--volume-id", VOL, **ours)
            self.assertNotEqual(p.returncode, 0, mode)
            self.assertIn("unread or empty: refused", p.stderr)
            self.assertNotIn("--- " + self.fstab, p.stdout)

    def test_a_mount_point_that_cannot_be_listed_is_refused(self):
        # codex #436 r2 P1: a failed listing of /srv/ameesh-guest is not an
        # empty directory: no mkfs, no fstab, no mount (work there would be
        # hidden under the volume)
        blocker = os.path.join(self.tmp.name, "srv")
        os.makedirs(blocker)
        with open(os.path.join(blocker, "file"), "w") as fh:
            fh.write("x")
        roots = [os.path.join(blocker, "file", "ameesh-guest")]   # ENOTDIR, not ENOENT
        if os.geteuid() != 0:                                      # EACCES (root reads it)
            locked = os.path.join(blocker, "locked")
            os.makedirs(locked)
            os.chmod(locked, 0)
            self.addCleanup(os.chmod, locked, 0o700)
            roots.append(locked)
        for root in roots:
            self.root = root
            for disk in ({}, {"type": "ext4", "label": "ameesh-guest", "uuid": "u"}):
                p = self.run_script("mount", "--volume-id", VOL, **disk)
                self.assertNotEqual(p.returncode, 0, (root, disk))
                self.assertIn("not listed: refused", p.stderr)
                self.assertEqual(self.cmds(p), [], (root, disk))
                self.assertNotIn("--- " + self.fstab, p.stdout)
        # a file where the mount point goes: refused too
        self.root = os.path.join(blocker, "file")
        p = self.run_script("mount", "--volume-id", VOL)
        self.assertIn("is not a directory", p.stderr)
        self.assertEqual(self.cmds(p), [])

    def test_unmount_leaves_a_mount_point_that_cannot_be_listed(self):
        self.our_line("u")
        self.root = os.path.join(self.fstab, "ameesh-guest")       # under a file
        p = self.run_script("unmount", "--volume-id", VOL, type="ext4", label="ameesh-guest",
                            uuid="u", src=self.disk, muuid="u")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not listed: refused", p.stderr)
        self.assertNotIn("rmdir", p.stdout)

    def test_mount_refuses_the_wrong_disk(self):
        p = self.run_script("mount", "--volume-id", VOL, size=str(150 * 10 ** 9))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("40 GB expected", p.stderr)
        p = self.run_script("mount", "--volume-id", VOL, lsblk="sdb\nsdb1\n")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("partitions", p.stderr)
        self.assertEqual(self.cmds(p), [])

    def test_mount_refuses_another_fstab_line_on_the_root(self):
        with open(self.fstab, "a") as fh:
            fh.write("/dev/vdb /srv/ameesh-guest xfs defaults 0 2\n")
        for disk in ({"type": "ext4", "label": "ameesh-guest", "uuid": "u"}, {}):
            p = self.run_script("mount", "--volume-id", VOL, **disk)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("already mounts something", p.stderr)
            self.assertEqual(self.cmds(p), [], disk)

    def test_mount_checks_the_mount_point_before_fstab(self):
        # codex #436 r1 P1: work on the root disk under the mount point, or
        # another filesystem mounted there: refused, fstab untouched
        os.makedirs(os.path.join(self.root, "work"))
        for disk in ({}, {"type": "ext4", "label": "ameesh-guest", "uuid": "u"}):
            p = self.run_script("mount", "--volume-id", VOL, **disk)
            self.assertNotEqual(p.returncode, 0, disk)
            self.assertIn("not empty on the root disk", p.stderr)
            self.assertNotIn("--- " + self.fstab, p.stdout)
            self.assertEqual(self.cmds(p), [], disk)
        p = self.run_script("mount", "--volume-id", VOL, src="/dev/vdc", muuid="w")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("already a mount point of /dev/vdc", p.stderr)
        self.assertNotIn("--- " + self.fstab, p.stdout)
        self.assertEqual(self.cmds(p), [])

    def test_mount_needs_the_volume_id(self):
        for mode in ("mount", "unmount"):
            p = self.run_script(mode)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("--volume-id", p.stderr)
        p = self.run_script("mount", "--volume-id", "../../sda")
        self.assertIn("invalid --volume-id", p.stderr)

    def test_unmount_removes_only_our_fstab_line(self):
        self.our_line("u")
        with open(self.fstab, "a") as fh:
            fh.write("UUID=v /srv/other ext4 %s 0 2\n" % OPTS)
        p = self.run_script("unmount", "--volume-id", VOL, type="ext4", label="ameesh-guest",
                            uuid="u", src=self.disk, muuid="u")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.cmds(p)[0], "umount /srv/ameesh-guest")
        written = p.stdout.split("--- %s\n" % self.fstab)[1].split("\n---")[0]
        self.assertNotIn("/srv/ameesh-guest", written)
        self.assertIn("UUID=v /srv/other", written)
        self.assertIn("LABEL=cloudimg-rootfs", written)

    def test_unmount_removes_the_by_hand_label_line(self):
        # PILOT-ameesh-agents-1.md's manual steps write LABEL=ameesh-guest
        self.label_line()
        self.udev("u")
        p = self.run_script("unmount", "--volume-id", VOL, type="ext4", label="ameesh-guest",
                            uuid="u", src=self.disk, muuid="u")
        self.assertEqual(p.returncode, 0, p.stderr)
        written = p.stdout.split("--- %s\n" % self.fstab)[1].split("\n---")[0]
        self.assertNotIn("/srv/ameesh-guest", written)

    def test_mount_adopts_a_volume_mounted_by_hand(self):
        # mesh-design #436: the volume already mounted with LABEL=ameesh-guest
        # in fstab (ameesh-agents-1, 2026-10-12): adopted, never formatted,
        # its line kept as is
        self.label_line()
        self.udev("u")
        ours = {"type": "ext4", "label": "ameesh-guest", "uuid": "u"}
        for mounted in ({"src": self.disk, "muuid": "u", "mlabel": "ameesh-guest"}, {}):
            p = self.run_script("mount", "--volume-id", VOL, **ours, **mounted)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("by LABEL=ameesh-guest, the volume's (adopted, kept as is)", p.stdout)
            self.assertIn("already there (kept)", p.stdout)
            self.assertNotIn("--- " + self.fstab, p.stdout)
            c = self.cmds(p)
            self.assertFalse([x for x in c if x.startswith(("mkfs", "wipefs"))], mounted)
            self.assertEqual(c[-1:], ["mount /srv/ameesh-guest"] if not mounted else [])

    def test_mount_adopts_only_the_volume_s_label(self):
        # the label names another disk, or this one and another, or the
        # filesystem mounted is not the volume's: refused, nothing changed
        self.label_line()
        other = os.path.join(self.tmp.name, "sdc")
        open(other, "w").close()
        ours = {"type": "ext4", "label": "ameesh-guest", "uuid": "u"}
        cases = ((other, {}),
                 (None, {"labeldevs": "%s\n%s\n" % (self.disk, other)}),
                 (None, {"labeldevs": ""}),
                 (None, {"src": self.disk, "muuid": "w"}))
        for label_to, disk in cases:
            self.udev("u", label_to)
            p = self.run_script("mount", "--volume-id", VOL, **ours, **disk)
            self.assertNotEqual(p.returncode, 0, disk)
            self.assertIn("refused", p.stderr)
            self.assertEqual(self.cmds(p), [], disk)
            self.assertNotIn("--- " + self.fstab, p.stdout)

    def test_check_takes_the_label_line_of_the_volume(self):
        # mesh-design #436: `check` says ok for the by-hand line when its
        # label and the mounted UUID are the volume's
        self.label_line()
        self.udev("u")
        mounted = {"src": self.disk, "muuid": "u", "mlabel": "ameesh-guest"}
        p = self.run_script("check", "--volume-id", VOL, **mounted)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("LABEL=ameesh-guest is the volume %s (UUID u)" % VOL, p.stdout)
        self.assertTrue(p.stdout.endswith("ok\n"))
        # without the volume's id, it cannot be told
        p = self.run_script("check", **mounted)
        self.assertEqual(p.returncode, 1)
        self.assertIn("NOT checked: give --volume-id", p.stdout)
        # another label mounted, another UUID: NOT ok
        for bad in ({"mlabel": "data"}, {"muuid": "w"}):
            p = self.run_script("check", "--volume-id", VOL, **dict(mounted, **bad))
            self.assertEqual(p.returncode, 1, bad)
            self.assertIn("NOT the volume %s's" % VOL, p.stdout)
        # the label's link to another disk: NOT ok
        self.udev("u", os.path.join(self.tmp.name, "sdc"))
        p = self.run_script("check", "--volume-id", VOL, **mounted)
        self.assertEqual(p.returncode, 1)

    def test_unmount_leaves_a_label_line_of_another_disk(self):
        self.label_line()
        self.udev("u")
        other = os.path.join(self.tmp.name, "sdc")
        p = self.run_script("unmount", "--volume-id", VOL, type="ext4", label="ameesh-guest",
                            uuid="u", src=self.disk, muuid="u",
                            labeldevs="%s\n%s\n" % (self.disk, other))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not only the volume", p.stderr)
        self.assertEqual(self.cmds(p), [])
        self.assertNotIn("--- " + self.fstab, p.stdout)

    def test_unmount_touches_only_the_pilot_filesystem(self):
        # codex #436 r1 P2: another filesystem mounted on the root, another
        # UUID, another fstab line on it, or not the pilot's ext4: nothing
        self.our_line("u")
        ours = {"type": "ext4", "label": "ameesh-guest", "uuid": "u"}
        for disk in (dict(ours, src="/dev/vdc", muuid="u"),
                     dict(ours, src=self.disk, muuid="w"),
                     dict(ours, uuid="w", src=self.disk, muuid="w"),
                     dict(ours, label="data", src=self.disk, muuid="u"),
                     {"src": self.disk, "muuid": "u"},
                     dict(ours, blkid_rc="4", src=self.disk, muuid="u")):
            p = self.run_script("unmount", "--volume-id", VOL, **disk)
            self.assertNotEqual(p.returncode, 0, disk)
            self.assertIn("nothing touched" if "blkid_rc" not in disk else "refused", p.stderr)
            self.assertEqual(self.cmds(p), [], disk)
            self.assertNotIn("--- " + self.fstab, p.stdout)
        os.remove(os.path.join(self.byid, "scsi-0SCW_sbs_volume-" + VOL))
        p = self.run_script("unmount", "--volume-id", VOL, **ours)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("absent", p.stderr)

    def test_check_says_not_mounted(self):
        p = self.run_script("check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("/srv/ameesh-guest: NOT mounted", p.stdout)
        self.assertIn("NOT one line", p.stdout)

    def test_check_compares_fstab_with_the_mounted_filesystem(self):
        # codex #436 r1 P2: one line, but for another UUID: NOT ok
        self.our_line("w")
        p = self.run_script("check", "--volume-id", VOL, src=self.disk, muuid="u")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("NOT the mounted filesystem's line", p.stdout)
        p = self.run_script("check", "--volume-id", VOL, src=self.disk, muuid="w")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.endswith("ok\n"))

    def test_unmount_with_nothing_there(self):
        p = self.run_script("unmount", "--volume-id", VOL, type="ext4", label="ameesh-guest",
                            uuid="u")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("--- " + self.fstab, p.stdout)
        self.assertIn("not mounted", p.stdout)


if __name__ == "__main__":
    unittest.main()
