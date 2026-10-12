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
OPTS = "defaults,nofail,x-systemd.device-timeout=30s"

SCW = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
st = json.loads(os.environ["SCW_STUB"])
if a[:3] == ["block", "volume", "list"]:
    print(json.dumps([{"id": i, "name": "ameesh-guest"} for i in st["volumes"]]
                     + [{"id": "other", "name": "ameesh-guest-old"}]))
elif a[:3] == ["instance", "server", "list"]:
    print(json.dumps([{"id": i, "name": "ameesh-agents-1"} for i in st["servers"]]))
elif a[:3] == ["instance", "server", "get"]:
    print(json.dumps({"volumes": {str(n): {"id": i} for n, i in enumerate(st["attached"])}}))
else:
    sys.exit("scw stub: unexpected %s" % a)
"""


class VolumeScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.bin = os.path.join(d, "bin")
        os.mkdir(self.bin)
        self.stub("scw", SCW)
        # the disk tools read the stub's state in the environment
        self.stub("lsblk", '#!/bin/sh\ncase "$*" in *TYPE*) echo disk ;; '
                           '*) printf "%s" "$STUB_LSBLK" ;; esac\n')
        self.stub("blockdev", '#!/bin/sh\necho "$STUB_SIZE"\n')
        self.stub("blkid", '#!/bin/sh\ncase "$*" in *TYPE*) v=$STUB_TYPE ;; '
                           '*LABEL*) v=$STUB_LABEL ;; *UUID*) v=$STUB_UUID ;; esac\n'
                           '[ -n "$v" ] && echo "$v" || exit 2\n')
        self.stub("wipefs", '#!/bin/sh\nprintf "%s" "$STUB_WIPEFS"\n')
        self.stub("findmnt", '#!/bin/sh\nexit 1\n')
        self.byid = os.path.join(d, "by-id")
        os.mkdir(self.byid)
        self.disk = os.path.join(d, "sdb")
        open(self.disk, "w").close()
        os.symlink(self.disk, os.path.join(self.byid, "scsi-0SCW_sbs_volume-" + VOL))
        self.fstab = os.path.join(d, "fstab")
        with open(self.fstab, "w") as fh:
            fh.write("LABEL=cloudimg-rootfs / ext4 discard,errors=remount-ro 0 1\n")

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def run_script(self, *args, scw=None, **disk):
        env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"],
                   VOLUME_TEST_FSTAB=self.fstab, VOLUME_TEST_BYID=self.byid,
                   VOLUME_TEST_GUARD=os.path.join(self.tmp.name, "guard.json"),
                   SCW_STUB=json.dumps(scw or {"volumes": [], "servers": [SRV], "attached": []}),
                   STUB_LSBLK="sdb\n", STUB_SIZE=str(40 * 10 ** 9), STUB_TYPE="",
                   STUB_LABEL="", STUB_UUID="", STUB_WIPEFS="")
        env.update({"STUB_" + k.upper(): v for k, v in disk.items()})
        return subprocess.run([SCRIPT, *args, "--dry-run"], env=env, text=True,
                              capture_output=True, timeout=60)

    def cmds(self, proc):
        return [line[2:] for line in proc.stdout.splitlines() if line.startswith("+ ")]

    # ---- create / detach (workstation) ----

    def test_create_from_nothing(self):
        p = self.run_script("create")
        self.assertEqual(p.returncode, 0, p.stderr)
        c = self.cmds(p)
        self.assertEqual(c[0], "scw block volume create name=ameesh-guest perf-iops=5000 "
                               "from-empty.size=40GB zone=fr-par-1")
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

    def test_detach_keeps_the_volume_unless_delete(self):
        scw = {"volumes": [VOL], "servers": [SRV], "attached": ["root", VOL]}
        c = self.cmds(self.run_script("detach", scw=scw))
        self.assertEqual(c, ["scw instance server detach-volume volume-id=%s zone=fr-par-1" % VOL])
        c = self.cmds(self.run_script("detach", "--delete", scw=scw))
        self.assertEqual(c[-1], "scw block volume delete %s zone=fr-par-1" % VOL)

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

    def test_mount_again_changes_nothing_on_the_disk(self):
        uuid = "abcd-ef"
        with open(self.fstab, "a") as fh:
            fh.write("UUID=%s /srv/ameesh-guest ext4 %s 0 2\n" % (uuid, OPTS))
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
        p = self.run_script("mount", "--volume-id", VOL, type="ext4", label="ameesh-guest",
                            uuid="u")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("already mounts something else", p.stderr)

    def test_mount_needs_the_volume_id(self):
        p = self.run_script("mount")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--volume-id", p.stderr)
        p = self.run_script("mount", "--volume-id", "../../sda")
        self.assertIn("invalid --volume-id", p.stderr)

    def test_unmount_removes_only_our_fstab_line(self):
        with open(self.fstab, "a") as fh:
            fh.write("UUID=u /srv/ameesh-guest ext4 %s 0 2\n" % OPTS)
            fh.write("UUID=v /srv/other ext4 %s 0 2\n" % OPTS)
        p = self.run_script("unmount")
        self.assertEqual(p.returncode, 0, p.stderr)
        written = p.stdout.split("--- %s\n" % self.fstab)[1].split("\n---")[0]
        self.assertNotIn("/srv/ameesh-guest", written)
        self.assertIn("UUID=v /srv/other", written)
        self.assertIn("LABEL=cloudimg-rootfs", written)

    def test_check_says_not_mounted(self):
        p = self.run_script("check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("/srv/ameesh-guest: NOT mounted", p.stdout)
        self.assertIn("NOT one line", p.stdout)

    def test_unmount_with_nothing_there(self):
        p = self.run_script("unmount")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("--- " + self.fstab, p.stdout)


if __name__ == "__main__":
    unittest.main()
