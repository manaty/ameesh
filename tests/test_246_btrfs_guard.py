# SPDX-License-Identifier: AGPL-3.0-only
"""#246: the disk guard reads btrfs allocation (unallocated space, metadata
headroom) from sysfs and closes admission before the floor — offline, on a
fake /sys tree with the golden values of the 2026-10-11 outage."""
from __future__ import annotations

import os
import re
import shutil
import struct
import tempfile
import unittest
from unittest import mock

from ameesh import exploitation, notify, platform, resources, storage
from ameesh.backlog import HOST_BLOCKING_ALERTS

from . import test_canon
from .test_l31_pressure import set_host_resources

MIB = 1024 ** 2
GIB = 1024 ** 3
UUID = "64c30c91-fd01-4082-802f-9dbd7bf4e8d5"


class FakeHost:
    """A fake /proc/self/mountinfo and /sys tree for one btrfs filesystem."""

    def __init__(self, root: str):
        self.root = root
        #: what the filesystem ioctls answer (None: unavailable); btrfs()
        #: sets the block sizes
        self.fs_sizes: tuple[str, list[int]] | None = None
        self.sysfs = os.path.join(root, "sys")
        self.mountinfo = os.path.join(root, "mountinfo")
        self.work = os.path.join(root, "home", "work")
        os.makedirs(self.work)

    def mount(self, source: str, fstype: str = "btrfs", point: str | None = None,
              over: tuple[str, str] | None = None) -> None:
        """`over`: (fstype, source) of a second mount on the same point."""
        point = (point or os.path.join(self.root, "home")).replace(" ", "\\040")
        with open(self.mountinfo, "w", encoding="utf-8") as fh:
            fh.write("22 1 0:21 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n")
            fh.write("35 22 0:31 /@home %s rw,noatime shared:2 - %s %s rw,ssd\n"
                     % (point, fstype, source))
            if over:
                fh.write("41 35 8:17 / %s rw,relatime shared:3 - %s %s rw\n"
                         % ((point,) + over))

    def btrfs(self, devices: dict[str, int], disk_total: dict[str, int],
              metadata: tuple[int, int, int], meta_kind: str = "metadata",
              unavailable: dict[str, int] | None = None) -> None:
        """`devices`: name -> size in bytes; `disk_total`: kind -> bytes;
        `metadata`: (total_bytes, bytes_used, bytes_may_use) of `meta_kind`;
        `unavailable`: its other counters (bytes_reserved, bytes_pinned,
        bytes_readonly: 0 by default)."""
        fs = os.path.join(self.sysfs, "fs", "btrfs", UUID)
        os.makedirs(os.path.join(fs, "devices"))
        for name, size in devices.items():
            block = os.path.join(self.sysfs, "class", "block", name)
            os.makedirs(block)
            self.write(os.path.join(block, "size"), size // 512)
            os.symlink(block, os.path.join(fs, "devices", name))
        for kind, value in disk_total.items():
            os.makedirs(os.path.join(fs, "allocation", kind), exist_ok=True)
            self.write(os.path.join(fs, "allocation", kind, "disk_total"), value)
        counters = dict(zip(("total_bytes", "bytes_used", "bytes_may_use"), metadata),
                        bytes_reserved=0, bytes_pinned=0, bytes_readonly=0)
        counters.update(unavailable or {})
        os.makedirs(os.path.join(fs, "allocation", meta_kind), exist_ok=True)
        for name, value in counters.items():
            self.write(os.path.join(fs, "allocation", meta_kind, name), value)
        self.fs_sizes = (UUID.replace("-", ""), list(devices.values()))

    @staticmethod
    def write(path: str, value) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("%s\n" % value)

    def read(self) -> dict:
        return platform.btrfs_allocation(self.work, mountinfo=self.mountinfo,
                                         sysfs=self.sysfs,
                                         device_sizes=lambda path: self.fs_sizes)


def outage(host: FakeHost) -> None:
    """2026-10-11 19:20: 1 MiB unallocated; metadata 536 MiB free, all of it in
    `bytes_may_use`."""
    data, system = 400 * GIB, 32 * MIB
    meta_total, meta_used = 8 * GIB, 8 * GIB - 536 * MIB
    host.btrfs({"dm-0": data + 2 * meta_total + system + MIB},
               {"data": data, "metadata": 2 * meta_total, "system": system},
               (meta_total, meta_used, 536 * MIB))


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-246-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.host = FakeHost(self.tmp)


class ReaderTest(_Tmp):
    def test_outage_golden_values(self):
        self.host.mount("/dev/dm-0")
        outage(self.host)
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": MIB,
                                            "btrfs_metadata_headroom_bytes": 0})

    def test_device_through_a_symlink_and_several_devices(self):
        # /dev/mapper/root -> ../dm-0, and a second device added to the volume
        mapper = os.path.join(self.tmp, "dev", "mapper")
        os.makedirs(mapper)
        open(os.path.join(self.tmp, "dev", "dm-0"), "w").close()
        os.symlink("../dm-0", os.path.join(mapper, "root"))
        self.host.mount(os.path.join(mapper, "root"))
        self.host.btrfs({"dm-0": 100 * GIB, "loop0": 4 * GIB},
                        {"data": 90 * GIB, "metadata": 4 * GIB, "system": 16 * MIB},
                        (2 * GIB, GIB, 512 * MIB))
        self.assertEqual(self.host.read(), {
            "btrfs_unallocated_bytes": 104 * GIB - 94 * GIB - 16 * MIB,
            "btrfs_metadata_headroom_bytes": 512 * MIB})

    def test_mount_point_with_a_space(self):
        point = os.path.join(self.tmp, "my home")
        self.host.work = os.path.join(point, "work")
        os.makedirs(self.host.work)
        self.host.mount("/dev/dm-0", point=point)
        outage(self.host)
        self.assertEqual(self.host.read()["btrfs_unallocated_bytes"], MIB)

    def test_not_btrfs_is_unknown(self):
        self.host.mount("/dev/dm-0", fstype="ext4")
        outage(self.host)
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": None,
                                            "btrfs_metadata_headroom_bytes": None})

    def test_unreadable_sysfs_is_unknown(self):
        self.host.mount("/dev/dm-0")
        # no /sys/fs/btrfs at all, then a filesystem that is not this device
        self.assertEqual(self.host.read()["btrfs_unallocated_bytes"], None)
        self.host.btrfs({"sdz": 10 * GIB}, {"data": GIB, "metadata": GIB, "system": MIB},
                        (GIB, 0, 0))
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": None,
                                            "btrfs_metadata_headroom_bytes": None})

    def test_missing_disk_total_leaves_unallocated_unknown(self):
        self.host.mount("/dev/dm-0")
        self.host.btrfs({"dm-0": 10 * GIB}, {"data": GIB, "metadata": GIB},
                        (GIB, 0, 0))
        # a group directory without its `disk_total`
        os.makedirs(os.path.join(self.host.sysfs, "fs", "btrfs", UUID, "allocation", "system"))
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": None,
                                            "btrfs_metadata_headroom_bytes": GIB})

    def test_every_allocation_group_is_counted(self):
        # codex r1 (1): recent kernels add data-reloc and metadata-treelog
        # (zoned); a fully allocated 100 GiB volume must read 0, not 21 GiB
        self.host.mount("/dev/dm-0")
        self.host.btrfs({"dm-0": 100 * GIB},
                        {"data": 70 * GIB, "metadata": 8 * GIB, "system": GIB,
                         "data-reloc": 20 * GIB, "metadata-treelog": GIB},
                        (4 * GIB, 3 * GIB, 0))
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": 0,
                                            "btrfs_metadata_headroom_bytes": GIB})

    def test_mixed_groups_are_read(self):
        # codex r1 (1): a mixed-group filesystem has `mixed` instead of data
        # and metadata; its headroom is the metadata headroom
        self.host.mount("/dev/dm-0")
        self.host.btrfs({"dm-0": 16 * GIB}, {"mixed": 15 * GIB, "system": 32 * MIB},
                        (15 * GIB, 14 * GIB, 512 * MIB), meta_kind="mixed")
        fs = os.path.join(self.host.sysfs, "fs", "btrfs", UUID, "allocation")
        self.host.write(os.path.join(fs, "global_rsv_size"), 512 * MIB)  # a file, not a group
        self.assertEqual(self.host.read(), {
            "btrfs_unallocated_bytes": GIB - 32 * MIB,
            "btrfs_metadata_headroom_bytes": 512 * MIB})

    def test_the_filesystem_device_size_wins(self):
        # codex r1 (2): a 200 GiB block device holding a 100 GiB filesystem
        # (shrunk, or device grown without resize), fully allocated
        self.host.mount("/dev/dm-0")
        self.host.btrfs({"dm-0": 200 * GIB},
                        {"data": 90 * GIB, "metadata": 9 * GIB, "system": GIB},
                        (4 * GIB, 3 * GIB, 0))
        self.host.fs_sizes = (UUID.replace("-", ""), [100 * GIB])
        self.assertEqual(self.host.read()["btrfs_unallocated_bytes"], 0)
        # codex r2 (a): ioctls unavailable, or the sizes of another
        # filesystem (fsid mismatch): unknown, not the 100 GiB gap between
        # the block device and the filesystem
        for sizes in (None, ("0" * 32, [100 * GIB])):
            self.host.fs_sizes = sizes
            self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": None,
                                                "btrfs_metadata_headroom_bytes": GIB})

    def test_every_unavailable_counter_is_deducted(self):
        # codex r2 (b): the kernel holds reserved, pinned, read-only and
        # zone-unusable bytes unavailable — total 8, used 6, reserved 2 is
        # exhausted metadata, even with 33 GiB unallocated
        self.host.mount("/dev/dm-0")
        self.host.btrfs({"dm-0": 50 * GIB}, {"data": 8 * GIB, "metadata": 8 * GIB, "system": GIB},
                        (8 * GIB, 6 * GIB, 0), unavailable={"bytes_reserved": 2 * GIB})
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": 33 * GIB,
                                            "btrfs_metadata_headroom_bytes": 0})
        meta = os.path.join(self.host.sysfs, "fs", "btrfs", UUID, "allocation", "metadata")
        for name, value in (("bytes_reserved", 0), ("bytes_pinned", 256 * MIB),
                            ("bytes_readonly", 128 * MIB), ("bytes_zone_unusable", 64 * MIB)):
            self.host.write(os.path.join(meta, name), value)
        self.assertEqual(self.host.read()["btrfs_metadata_headroom_bytes"],
                         2 * GIB - 448 * MIB)
        # bytes_zone_unusable is optional (before Linux 5.12); the others are not
        os.remove(os.path.join(meta, "bytes_zone_unusable"))
        self.assertEqual(self.host.read()["btrfs_metadata_headroom_bytes"],
                         2 * GIB - 384 * MIB)
        os.remove(os.path.join(meta, "bytes_pinned"))
        self.assertIsNone(self.host.read()["btrfs_metadata_headroom_bytes"])

    def test_device_sizes_from_the_ioctls(self):
        # the unprivileged ioctls, faked: device 2 was removed (ENODEV)
        fsid = bytes.fromhex(UUID.replace("-", ""))
        sizes = {1: 509943480320, 3: 4089446400}

        def ioctl(fd, request, buf):
            if request == platform.host.BTRFS_IOC_FS_INFO:
                self.assertEqual(len(buf), 1024)
                struct.pack_into("=QQ16s", buf, 0, 3, 2, fsid)
                return 0
            self.assertEqual((request, len(buf)), (platform.host.BTRFS_IOC_DEV_INFO, 4096))
            devid = struct.unpack_from("=Q", buf)[0]
            if devid not in sizes:
                raise OSError(19, "No such device")
            struct.pack_into("=QQ", buf, 24, sizes[devid] // 2, sizes[devid])
            return 0

        with mock.patch("fcntl.ioctl", ioctl):
            self.assertEqual(platform.host.btrfs_device_sizes(self.tmp),
                             (fsid.hex(), [509943480320, 4089446400]))
        self.assertEqual(platform.host.BTRFS_IOC_FS_INFO, 0x8400941F)
        self.assertEqual(platform.host.BTRFS_IOC_DEV_INFO, 0xD000941E)
        with mock.patch("fcntl.ioctl", side_effect=OSError(25, "Not a typewriter")):
            self.assertIsNone(platform.host.btrfs_device_sizes(self.tmp))

    def test_the_visible_mount_wins(self):
        # codex r1 (3): ext4 mounted over the btrfs point hides it
        outage(self.host)
        self.host.mount("/dev/dm-0", over=("ext4", "/dev/sdb1"))
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": None,
                                            "btrfs_metadata_headroom_bytes": None})
        self.host.mount("/dev/sdb1", fstype="ext4", over=("btrfs", "/dev/dm-0"))
        self.assertEqual(self.host.read()["btrfs_unallocated_bytes"], MIB)

    def test_a_mount_hidden_under_a_later_parent_mount_is_not_read(self):
        # codex r2 (c): btrfs on /work/cache, then ext4 over /work: path
        # lookup of /work/cache/project goes through ext4, the btrfs child of
        # the root mount is unreachable
        outage(self.host)
        work = os.path.join(self.tmp, "work")
        cache = os.path.join(work, "cache")
        self.host.work = os.path.join(cache, "project")
        os.makedirs(self.host.work)

        def mountinfo(*lines):
            with open(self.host.mountinfo, "w", encoding="utf-8") as fh:
                fh.write("22 1 0:21 / / rw - ext4 /dev/sda1 rw\n")
                for line in lines:
                    fh.write(line + "\n")

        mountinfo("35 22 0:31 /@cache %s rw - btrfs /dev/dm-0 rw" % cache,
                  "41 22 8:17 / %s rw - ext4 /dev/sdb1 rw" % work)
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": None,
                                            "btrfs_metadata_headroom_bytes": None})
        # the same btrfs mounted on the ext4 (child of 41) is visible
        mountinfo("41 22 8:17 / %s rw - ext4 /dev/sdb1 rw" % work,
                  "35 41 0:31 /@cache %s rw - btrfs /dev/dm-0 rw" % cache)
        self.assertEqual(self.host.read()["btrfs_unallocated_bytes"], MIB)
        # a longer, visible prefix below a hidden one: the stack top of the
        # visible mount, here a btrfs over the ext4 on the same point
        mountinfo("35 22 0:31 /@cache %s rw - btrfs /dev/dm-0 rw" % cache,
                  "41 22 8:17 / %s rw - ext4 /dev/sdb1 rw" % work,
                  "42 41 0:31 /@work %s rw - btrfs /dev/dm-0 rw" % work)
        self.assertEqual(self.host.read()["btrfs_unallocated_bytes"], MIB)
        self.assertEqual(platform.host._mount_source(self.host.work, self.host.mountinfo),
                         (work, "btrfs", "/dev/dm-0"))

    def test_sample_publishes_the_btrfs_reading(self):
        with mock.patch.object(resources.platform, "btrfs_allocation",
                               return_value={"btrfs_unallocated_bytes": MIB,
                                             "btrfs_metadata_headroom_bytes": 0}) as read:
            reading = resources.sample("h", path=self.host.work, now=1.0)
        read.assert_called_once_with(self.host.work)
        self.assertEqual(reading["btrfs_unallocated_bytes"], MIB)
        self.assertEqual(reading["btrfs_metadata_headroom_bytes"], 0)


class FloorsTest(unittest.TestCase):
    def test_defaults(self):
        limits = resources.thresholds(None)
        self.assertEqual(limits["min_btrfs_unallocated"], 10 * GIB)
        self.assertEqual(limits["min_btrfs_metadata_headroom"], GIB)

    def test_policy_overrides(self):
        class Policy:
            resources = {"min_btrfs_unallocated": "20GiB",
                         "min_btrfs_metadata_headroom": "512MiB"}

        limits = resources.thresholds(Policy)
        self.assertEqual(limits["min_btrfs_unallocated"], 20 * GIB)
        self.assertEqual(limits["min_btrfs_metadata_headroom"], 512 * MIB)

    def test_outage_is_critical(self):
        verdict = resources.pressure({"disk_free_bytes": 16 * 1000 ** 3,
                                      "btrfs_unallocated_bytes": MIB,
                                      "btrfs_metadata_headroom_bytes": 0}, None)
        # `df` showed 16 GB free: the old floor alone never fired
        self.assertNotIn("min_disk_free", [b["key"] for b in verdict["breaches"]])
        self.assertEqual({b["key"] for b in verdict["breaches"]},
                         {"min_btrfs_unallocated", "min_btrfs_metadata_headroom"})
        self.assertTrue(verdict["blocked"])
        self.assertTrue(verdict["critical"])

    def test_admission_closes_before_the_critical_level(self):
        # 8 GiB unallocated: under the 10 GiB floor, above its half
        verdict = resources.pressure({"btrfs_unallocated_bytes": 8 * GIB,
                                      "btrfs_metadata_headroom_bytes": 4 * GIB}, None)
        self.assertTrue(verdict["blocked"])
        self.assertFalse(verdict["critical"])
        # the metadata floor alone, at warning then critical level
        verdict = resources.pressure({"btrfs_metadata_headroom_bytes": 768 * MIB}, None)
        self.assertEqual([(b["key"], b["critical"]) for b in verdict["breaches"]],
                         [("min_btrfs_metadata_headroom", False)])
        verdict = resources.pressure({"btrfs_metadata_headroom_bytes": 512 * MIB}, None)
        self.assertTrue(verdict["critical"])

    def test_unknown_reading_and_limits_without_btrfs_keys(self):
        self.assertFalse(resources.pressure({"btrfs_unallocated_bytes": None}, None)["blocked"])
        # limits computed before #246 (no btrfs keys) do not raise
        self.assertEqual(resources.breaches({"btrfs_unallocated_bytes": 0},
                                            {"min_mem_available": 1, "max_swap_used": 1,
                                             "max_load": 1.0, "min_disk_free": 1}), [])

    def test_physical_limits_keep_the_highest_floor(self):
        class Fiche:
            def __init__(self, value):
                self.policy = type("P", (), {"resources": {"min_btrfs_unallocated": value},
                                             "max_agents": None})()

        class Canon:
            def __init__(self, ident, value):
                self.id, self.fiche = ident, Fiche(value)

            def host(self, name):
                return self.fiche

        limits = resources.host_limits([Canon("a", "5GiB"), Canon("b", "20GiB")], "pc")
        self.assertEqual(limits["limits"]["min_btrfs_unallocated"], 20 * GIB)
        self.assertEqual(limits["origin"]["min_btrfs_unallocated"], "b")


class CanonTest(test_canon._TmpMixin, unittest.TestCase):
    def test_btrfs_floors_are_known_keys(self):
        from ameesh import canon
        root = self.example_copy()
        set_host_resources(root, min_btrfs_unallocated="20GiB",
                           min_btrfs_metadata_headroom="2GiB")
        loaded = canon.load(root, untrusted=True)
        self.assertEqual(loaded.host("atelier").policy.resources, {
            "min_btrfs_unallocated": 20 * GIB, "min_btrfs_metadata_headroom": 2 * GIB})
        self.assertNotIn("host-resources-unknown",
                         [f.code for f in canon.validate(loaded)])


class AlertTest(unittest.TestCase):
    def alerts(self, reading: dict) -> list[dict]:
        store = mock.MagicMock()
        store.hosts.current.return_value = [dict({"host": "pc", "sampled_ts": 100.0},
                                                 **reading)]
        store.turn_resources.orphans.return_value = []
        store.turn_resources.stale_running.return_value = []
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch("ameesh.canon.load_configured", return_value=[]), \
                mock.patch.object(exploitation, "_housekeeping_alerts", return_value=[]):
            return exploitation._host_alerts(None, None, now=200.0)

    def test_once_per_episode_and_again_on_critical(self):
        warning = self.alerts({"btrfs_unallocated_bytes": 8 * GIB})
        self.assertEqual([a["type"] for a in warning], ["host_disk_low"])
        self.assertFalse(warning[0]["urgent"])
        self.assertIn("btrfs non alloué 8.0 GiB (seuil 10.0 GiB)", warning[0]["detail"])
        # one episode, one key, whatever the level (codex r1 (4))
        again = self.alerts({"btrfs_unallocated_bytes": 7 * GIB})
        self.assertEqual(exploitation.alert_key(warning[0]), exploitation.alert_key(again[0]))
        critical = self.alerts({"btrfs_unallocated_bytes": MIB,
                                "btrfs_metadata_headroom_bytes": 0})
        self.assertEqual([a["type"] for a in critical], ["host_disk_low"])
        self.assertTrue(critical[0]["urgent"])
        self.assertEqual(critical[0]["reason"], "critique")
        self.assertEqual(exploitation.alert_key(warning[0]),
                         exploitation.alert_key(critical[0]))
        self.assertEqual(sorted(critical[0]["breaches"]),
                         ["min_btrfs_metadata_headroom", "min_btrfs_unallocated"])

    def test_other_floors_stay_in_host_pressure(self):
        found = self.alerts({"mem_available_bytes": 0, "btrfs_unallocated_bytes": 8 * GIB})
        by_type = {a["type"]: a for a in found}
        self.assertEqual(set(by_type), {"host_disk_low", "host_pressure"})
        self.assertEqual(by_type["host_pressure"]["breaches"], ["min_mem_available"])

    def test_warning_critical_flips_stay_one_episode(self):
        # codex r1 (4): warning -> critical -> warning -> critical while still
        # under the floor: one push, one escalation, no resolution until the
        # reading is back above the floor
        sent: list = []
        sender = mock.Mock(send=lambda channel, message: sent.append(message))
        notifier = notify.Notifier(None, notify.parse_config({"default": ["desktop"]}),
                                   sender=sender, log=lambda text: None,
                                   clock=lambda: 1000.0)
        readings = [{"btrfs_unallocated_bytes": 8 * GIB},
                    {"btrfs_unallocated_bytes": MIB},
                    {"btrfs_unallocated_bytes": 8 * GIB},
                    {"btrfs_unallocated_bytes": MIB},
                    {"btrfs_unallocated_bytes": 20 * GIB}]
        events = []
        with mock.patch.multiple(notify.Notifier, _brief_orchestrators=mock.DEFAULT,
                                 _brief_agents=mock.DEFAULT, _github_issues=mock.DEFAULT,
                                 _ci_failures=mock.DEFAULT):
            for reading in readings:
                current = [dict(a, responsible="human:proprio")
                           for a in self.alerts(reading)]
                events.append([r["event"] for r in notifier.run_pass(None, current=current)])
        self.assertEqual(events, [["raised"], ["raised"], [], [], ["resolved"]])
        # the warning, the escalation (urgent), then the resolution
        self.assertEqual([m.urgent for m in sent], [False, True, False])
        self.assertIn("plus aucun nouveau tour", sent[1].body)

    def test_raised_critical_is_not_escalated_again(self):
        sent: list = []
        notifier = notify.Notifier(None, notify.parse_config({"default": ["desktop"]}),
                                   sender=mock.Mock(send=lambda c, m: sent.append(m)),
                                   log=lambda text: None, clock=lambda: 1000.0)
        with mock.patch.multiple(notify.Notifier, _brief_orchestrators=mock.DEFAULT,
                                 _brief_agents=mock.DEFAULT, _github_issues=mock.DEFAULT,
                                 _ci_failures=mock.DEFAULT):
            for reading in ({"btrfs_unallocated_bytes": MIB},
                            {"btrfs_unallocated_bytes": 8 * GIB},
                            {"btrfs_unallocated_bytes": MIB}):
                current = [dict(a, responsible="human:proprio")
                           for a in self.alerts(reading)]
                notifier.run_pass(None, current=current)
        self.assertEqual([m.urgent for m in sent], [True])

    def test_supervisor_grants_cover_the_new_columns(self):
        # codex r1 (5): the supervisor contract lists every column it reads
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "deploy", "sql", "role-superviseur.sql"),
                  encoding="utf-8") as fh:
            sql = re.sub(r"--[^\n]*", "", fh.read())
        block = re.search(r"\('host_resources', ARRAY\[(.*?)\]\)", sql, re.S).group(1)
        granted = set(re.findall(r"'(\w+)'", block))
        self.assertLessEqual({"btrfs_unallocated_bytes", "btrfs_metadata_headroom_bytes"},
                             granted)
        self.assertLessEqual(set(storage.postgres.hosts.MEASURE_COLUMNS), granted)

    def test_pushed_to_the_owner_and_blocks_backlog_takes(self):
        self.assertIn("host_disk_low", notify.DEFAULT_TYPES)
        self.assertIn("host_disk_low", notify.TYPE_LABELS)
        self.assertIn("host_disk_low", exploitation.ALERT_TYPES)
        self.assertIn("host_disk_low", HOST_BLOCKING_ALERTS)


if __name__ == "__main__":
    unittest.main()
