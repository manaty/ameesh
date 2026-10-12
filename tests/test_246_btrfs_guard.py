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
    """A fake /sys tree of btrfs filesystems and what the ioctls answer on
    the work path."""

    def __init__(self, root: str):
        self.root = root
        #: what the filesystem ioctls answer on the work path: (fsid, (size,
        #: allocated) per device or None), None when not btrfs; btrfs() sets
        #: its own
        self.fs_info: tuple[str, list[tuple[int, int]] | None] | None = None
        self.sysfs = os.path.join(root, "sys")
        self.work = os.path.join(root, "home", "work")
        os.makedirs(self.work)

    def btrfs(self, devices: dict[str, int | tuple[int, int]], disk_total: dict[str, int],
              metadata: tuple[int, int, int], meta_kind: str = "metadata",
              unavailable: dict[str, int] | None = None, uuid: str = UUID,
              profiles: tuple[str, ...] = ()) -> None:
        """`devices`: name -> (size, allocated bytes), or the size alone on
        a single device (allocated: every `disk_total`); `disk_total`: kind
        -> bytes; `metadata`: (total_bytes, bytes_used, bytes_may_use) of
        `meta_kind`; `unavailable`: its other counters (bytes_reserved,
        bytes_pinned, bytes_readonly: 0 by default); `profiles`: the profile
        directories under `meta_kind` (none: an unnamed profile)."""
        fs = os.path.join(self.sysfs, "fs", "btrfs", uuid)
        for kind, value in disk_total.items():
            os.makedirs(os.path.join(fs, "allocation", kind), exist_ok=True)
            self.write(os.path.join(fs, "allocation", kind, "disk_total"), value)
        counters = dict(zip(("total_bytes", "bytes_used", "bytes_may_use"), metadata),
                        bytes_reserved=0, bytes_pinned=0, bytes_readonly=0)
        counters.update(unavailable or {})
        os.makedirs(os.path.join(fs, "allocation", meta_kind), exist_ok=True)
        for name, value in counters.items():
            self.write(os.path.join(fs, "allocation", meta_kind, name), value)
        for profile in profiles:
            os.makedirs(os.path.join(fs, "allocation", meta_kind, profile), exist_ok=True)
        if len(devices) == 1:
            devices = {name: value if isinstance(value, tuple)
                       else (value, sum(disk_total.values()))
                       for name, value in devices.items()}
        self.fs_info = (uuid.replace("-", ""), list(devices.values()))

    @staticmethod
    def write(path: str, value) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("%s\n" % value)

    def read(self) -> dict:
        return platform.btrfs_allocation(self.work, sysfs=self.sysfs,
                                         fs_info=lambda path: self.fs_info)


def outage(host: FakeHost) -> None:
    """2026-10-11 19:20: 1 MiB unallocated; metadata (DUP) 536 MiB free, all
    of it in `bytes_may_use`."""
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
        # metadata exhausted; the 1 MiB left could hold 512 KiB of DUP metadata
        outage(self.host)
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": MIB,
                                            "btrfs_metadata_headroom_bytes": MIB // 2})
        reading = dict(self.host.read(), disk_free_bytes=16 * GIB)
        self.assertTrue(all(b["critical"] for b in resources.breaches(
            reading, resources.thresholds(None)) if b["key"] in resources.BTRFS_KEYS))

    def test_several_devices(self):
        # DUP metadata on two devices: half of each device's unallocated space
        self.host.btrfs({"dm-0": (100 * GIB, 92 * GIB), "loop0": (4 * GIB, 2 * GIB + 16 * MIB)},
                        {"data": 90 * GIB, "metadata": 4 * GIB, "system": 16 * MIB},
                        (2 * GIB, GIB, 512 * MIB), profiles=("dup",))
        unallocated = 104 * GIB - 94 * GIB - 16 * MIB
        self.assertEqual(self.host.read(), {
            "btrfs_unallocated_bytes": unallocated,
            "btrfs_metadata_headroom_bytes": 512 * MIB + 4 * GIB + GIB - 8 * MIB})

    def test_not_btrfs_is_unknown(self):
        # the ioctl fails on the path (ENOTTY): not btrfs, whatever sysfs holds
        outage(self.host)
        self.host.fs_info = None
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": None,
                                            "btrfs_metadata_headroom_bytes": None})

    def test_unreadable_sysfs_is_unknown(self):
        self.host.fs_info = (UUID.replace("-", ""), [(10 * GIB, 0)])
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": None,
                                            "btrfs_metadata_headroom_bytes": None})

    def test_missing_disk_total_leaves_both_unknown(self):
        self.host.btrfs({"dm-0": 10 * GIB}, {"data": GIB, "metadata": GIB},
                        (GIB, 0, 0))
        # a group directory without its `disk_total`
        os.makedirs(os.path.join(self.host.sysfs, "fs", "btrfs", UUID, "allocation", "system"))
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": None,
                                            "btrfs_metadata_headroom_bytes": None})

    def test_every_allocation_group_is_counted(self):
        # codex r1 (1): recent kernels add data-reloc and metadata-treelog
        # (zoned); a fully allocated 100 GiB volume must read 0, not 21 GiB
        self.host.btrfs({"dm-0": 100 * GIB},
                        {"data": 70 * GIB, "metadata": 8 * GIB, "system": GIB,
                         "data-reloc": 20 * GIB, "metadata-treelog": GIB},
                        (4 * GIB, 3 * GIB, 0))
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": 0,
                                            "btrfs_metadata_headroom_bytes": GIB})

    def test_mixed_groups_are_read(self):
        # codex r1 (1): a mixed-group filesystem has `mixed` instead of data
        # and metadata; its headroom is the metadata headroom
        self.host.btrfs({"dm-0": 16 * GIB}, {"mixed": 15 * GIB, "system": 32 * MIB},
                        (15 * GIB, 14 * GIB, 512 * MIB), meta_kind="mixed")
        fs = os.path.join(self.host.sysfs, "fs", "btrfs", UUID, "allocation")
        self.host.write(os.path.join(fs, "global_rsv_size"), 512 * MIB)  # a file, not a group
        self.assertEqual(self.host.read(), {
            "btrfs_unallocated_bytes": GIB - 32 * MIB,
            "btrfs_metadata_headroom_bytes": 512 * MIB + GIB - 32 * MIB})

    def test_the_filesystem_device_size_wins(self):
        # codex r1 (2), r2 (a): a 200 GiB block device holding a 100 GiB
        # filesystem, fully allocated: the filesystem's own device sizes;
        # without them (a device not answering) unknown, never the block size
        self.host.btrfs({"dm-0": 100 * GIB},
                        {"data": 90 * GIB, "metadata": 9 * GIB, "system": GIB},
                        (4 * GIB, 3 * GIB, 0))
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": 0,
                                            "btrfs_metadata_headroom_bytes": GIB})
        self.host.fs_info = (UUID.replace("-", ""), None)
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": None,
                                            "btrfs_metadata_headroom_bytes": None})

    def test_every_unavailable_counter_is_deducted(self):
        # codex r2 (b): the kernel holds reserved, pinned, read-only and
        # zone-unusable bytes unavailable — total 8, used 6, reserved 2 is
        # exhausted metadata, here with no unallocated space to grow into
        self.host.btrfs({"dm-0": 50 * GIB}, {"data": 33 * GIB, "metadata": 16 * GIB,
                                             "system": GIB},
                        (8 * GIB, 6 * GIB, 0), unavailable={"bytes_reserved": 2 * GIB})
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": 0,
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

    def test_small_metadata_with_room_to_grow_is_no_shortage(self):
        # codex r3 (1): 30 GiB unallocated, DUP metadata of 256 MiB with 16
        # MiB left — btrfs allocates a new chunk, no floor is breached
        self.host.btrfs({"dm-0": 64 * GIB},
                        {"data": 33 * GIB, "metadata": 512 * MIB, "system": 512 * MIB},
                        (256 * MIB, 240 * MIB, 0))
        reading = self.host.read()
        self.assertEqual(reading, {"btrfs_unallocated_bytes": 30 * GIB,
                                   "btrfs_metadata_headroom_bytes": 16 * MIB + 15 * GIB})
        self.assertEqual([b for b in resources.breaches(
            dict(reading, disk_free_bytes=40 * GIB), resources.thresholds(None))
            if b["key"] in resources.BTRFS_KEYS], [])
        # single-profile metadata (copy factor 1) grows by all of it; RAID1C3 by a third
        meta = os.path.join(self.host.sysfs, "fs", "btrfs", UUID, "allocation", "metadata")
        for disk_total, copies in ((256 * MIB, 1), (768 * MIB, 3)):
            self.host.write(os.path.join(meta, "disk_total"), disk_total)
            unallocated = 64 * GIB - 33 * GIB - 512 * MIB - disk_total
            self.host.fs_info = (self.host.fs_info[0], [(64 * GIB, 64 * GIB - unallocated)])
            self.assertEqual(self.host.read(), {
                "btrfs_unallocated_bytes": unallocated,
                "btrfs_metadata_headroom_bytes": 16 * MIB + unallocated // copies})

    def test_device_info_from_the_ioctls(self):
        # the unprivileged ioctls, faked: device 2 was removed (ENODEV)
        fsid = bytes.fromhex(UUID.replace("-", ""))
        sizes = {1: 509943480320, 3: 4089446400}
        answer = {"count": 2}

        def ioctl(fd, request, buf):
            if request == platform.host.BTRFS_IOC_FS_INFO:
                self.assertEqual(len(buf), 1024)
                struct.pack_into("=QQ16s", buf, 0, 3, answer["count"], fsid)
                return 0
            self.assertEqual((request, len(buf)), (platform.host.BTRFS_IOC_DEV_INFO, 4096))
            devid = struct.unpack_from("=Q", buf)[0]
            if devid not in sizes:
                raise OSError(19, "No such device")
            struct.pack_into("=QQ", buf, 24, sizes[devid] // 2, sizes[devid])
            return 0

        with mock.patch("fcntl.ioctl", ioctl):
            self.assertEqual(platform.host.btrfs_fs_info(self.tmp),
                             (fsid.hex(), [(509943480320, 254971740160),
                                           (4089446400, 2044723200)]))
            # a device that does not answer: the fsid stays, the sizes are unknown
            answer["count"] = 3
            self.assertEqual(platform.host.btrfs_fs_info(self.tmp), (fsid.hex(), None))
        self.assertEqual(platform.host.BTRFS_IOC_FS_INFO, 0x8400941F)
        self.assertEqual(platform.host.BTRFS_IOC_DEV_INFO, 0xD000941E)
        with mock.patch("fcntl.ioctl", side_effect=OSError(25, "Not a typewriter")):
            self.assertIsNone(platform.host.btrfs_fs_info(self.tmp))

    def test_the_filesystem_visible_at_the_path_is_read(self):
        # codex r1 (3), r2 (c), r3 (2): overmounts, hidden descendants,
        # propagated shadow mounts — the kernel resolves the path, the ioctl
        # on it names the filesystem actually there. Two btrfs in sysfs: the
        # exhausted one is not read when the path is on the other
        other = "5c1b80f6-32bd-4e8e-84ca-3f4f078c22ae"
        self.host.btrfs({"sdb": 100 * GIB}, {"data": 10 * GIB, "metadata": 2 * GIB,
                                             "system": GIB},
                        (GIB, 0, 0), uuid=other)
        healthy = self.host.fs_info
        outage(self.host)
        self.assertEqual(self.host.read()["btrfs_unallocated_bytes"], MIB)
        self.host.fs_info = healthy
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": 87 * GIB,
                                            "btrfs_metadata_headroom_bytes": GIB + 87 * GIB // 2})

    def test_real_path_reads_through_the_ioctl(self):
        # end to end on this host: a btrfs work path reads both values, any
        # other filesystem (or none) reads None
        reading = platform.btrfs_allocation(self.tmp)
        if platform.host.btrfs_fs_info(self.tmp) is None:
            self.assertEqual(reading, {"btrfs_unallocated_bytes": None,
                                       "btrfs_metadata_headroom_bytes": None})
        else:
            self.assertIsInstance(reading["btrfs_unallocated_bytes"], int)
        self.assertEqual(platform.btrfs_allocation("/proc"),
                         {"btrfs_unallocated_bytes": None, "btrfs_metadata_headroom_bytes": None})

    def test_sample_publishes_the_btrfs_reading(self):
        with mock.patch.object(resources.platform, "btrfs_allocation",
                               return_value={"btrfs_unallocated_bytes": MIB,
                                             "btrfs_metadata_headroom_bytes": 0}) as read:
            reading = resources.sample("h", path=self.host.work, now=1.0)
        read.assert_called_once_with(self.host.work)
        self.assertEqual(reading["btrfs_unallocated_bytes"], MIB)
        self.assertEqual(reading["btrfs_metadata_headroom_bytes"], 0)


class ProfileGrowthTest(_Tmp):
    """#252: metadata grows into each device's unallocated space as its
    profile allows, not into the sum divided by the copy factor."""

    def raid1_smaller_full(self, profiles=("raid1",)) -> None:
        # codex r4 on #235: two devices, the smaller full, 100 GiB
        # unallocated on the larger, metadata exhausted (total 4 GiB,
        # disk_total 8 GiB) — the summed formula read 50 GiB of headroom
        self.host.btrfs({"sda": (20 * GIB, 20 * GIB), "sdb": (120 * GIB, 20 * GIB)},
                        {"data": 32 * GIB - 64 * MIB, "metadata": 8 * GIB,
                         "system": 64 * MIB},
                        (4 * GIB, 4 * GIB - 256 * MIB, 256 * MIB), profiles=profiles)

    def test_raid1_with_one_device_full_cannot_grow(self):
        self.raid1_smaller_full()
        reading = self.host.read()
        self.assertEqual(reading, {"btrfs_unallocated_bytes": 100 * GIB,
                                   "btrfs_metadata_headroom_bytes": 0})
        self.assertEqual([b["key"] for b in resources.breaches(
            dict(reading, disk_free_bytes=40 * GIB), resources.thresholds(None))
            if b["key"] in resources.BTRFS_KEYS], ["min_btrfs_metadata_headroom"])

    def test_growth_per_profile(self):
        # 100, 10 and 10 GiB unallocated on three devices
        free = [100 * GIB, 10 * GIB, 10 * GIB]
        for profile, growth in (("single", 120), ("raid0", 120), ("dup", 60),
                                ("raid1", 20), ("raid10", 20), ("raid1c3", 10),
                                ("raid1c4", 0), ("raid5", 20), ("raid6", 10)):
            with self.subTest(profile=profile):
                self.assertEqual(platform.host.btrfs_growth(profile, free, 2), growth * GIB)
        # the kernel spreads mirrored chunks: three equal devices hold RAID1 on
        # half of their sum, RAID5 on two thirds
        even = [10 * GIB] * 3
        self.assertEqual(platform.host.btrfs_growth("raid1", even, 2), 15 * GIB)
        self.assertEqual(platform.host.btrfs_growth("raid5", even, 2), 20 * GIB)
        # an unnamed profile: its copy factor, DUP on one device, a mirror on several
        self.assertEqual(platform.host.btrfs_growth(None, free, 1), 120 * GIB)
        self.assertEqual(platform.host.btrfs_growth(None, [100 * GIB], 2), 50 * GIB)
        self.assertEqual(platform.host.btrfs_growth(None, free, 2), 20 * GIB)
        self.assertEqual(platform.host.btrfs_growth(None, free, 3), 10 * GIB)

    def test_unnamed_profile_on_several_devices_reads_as_a_mirror(self):
        self.raid1_smaller_full(profiles=())
        self.assertEqual(self.host.read()["btrfs_metadata_headroom_bytes"], 0)

    def test_a_conversion_reads_the_smaller_growth(self):
        # DUP being converted to RAID1, 30 and 10 GiB unallocated: DUP could
        # take 20 GiB, RAID1 10 GiB
        self.host.btrfs({"sda": (40 * GIB, 30 * GIB), "sdb": (40 * GIB, 10 * GIB)},
                        {"data": 36 * GIB, "metadata": 4 * GIB},
                        (2 * GIB, GIB, 0), profiles=("dup", "raid1"))
        self.assertEqual(self.host.read(), {"btrfs_unallocated_bytes": 40 * GIB,
                                            "btrfs_metadata_headroom_bytes": 11 * GIB})


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
        self.assertIn("btrfs unallocated 8.0 GiB (threshold 10.0 GiB)", warning[0]["detail"])
        # L156d: the alert text is in English
        self.assertIn("btrfs almost fully allocated", warning[0]["detail"])
        self.assertIn("no new turn except an orchestrator's", warning[0]["detail"])

        # one episode, one key, whatever the level (codex r1 (4))
        again = self.alerts({"btrfs_unallocated_bytes": 7 * GIB})
        self.assertEqual(exploitation.alert_key(warning[0]), exploitation.alert_key(again[0]))
        critical = self.alerts({"btrfs_unallocated_bytes": MIB,
                                "btrfs_metadata_headroom_bytes": 0})
        self.assertEqual([a["type"] for a in critical], ["host_disk_low"])
        self.assertTrue(critical[0]["urgent"])
        self.assertEqual(critical[0]["reason"], "critique")
        self.assertIn("no new turn at all", critical[0]["detail"])

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
        self.assertIn("no new turn at all", sent[1].body)

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
