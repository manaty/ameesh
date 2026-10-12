# SPDX-License-Identifier: AGPL-3.0-only
"""#253: the disk guard samples every filesystem the host writes to (work
roots, state dir, deps cache root, docker's root dir, `/`), one reading per
distinct filesystem (btrfs fsid, st_dev otherwise); the worst reading of each
floor decides and the breach names its path.

Since 2026-10-12 04:43 the work root is on its own SSD: sampling it alone,
the guard of #246 watched the SSD and no longer the system disk that failed
on 2026-10-11 (state, deps cache, docker)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import exploitation, menage, platform, resources, storage

from .support import PgTestCase

GIB = 1024 ** 3
FSID_SSD = "aa" * 16
FSID_SYSTEM = "bb" * 16


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        # resolved: the volume readers resolve symlinks (#289 r1)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ameesh-253-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        resources._docker_root.clear()
        self.addCleanup(resources._docker_root.clear)

    def dir(self, *parts: str) -> str:
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path


class DiskPathsTest(_Tmp):
    """One test per path the guard watches."""

    def cfg(self, roots=(), state_dir=None):
        return types.SimpleNamespace(worktree_roots=list(roots), state_dir=state_dir)

    def paths(self, cfg, policy=None, docker=None):
        with mock.patch.object(resources, "docker_root", return_value=docker):
            return resources.disk_paths(cfg, policy)

    def test_every_work_root_that_exists(self):
        a, b = self.dir("ssd"), self.dir("other")
        paths = self.paths(self.cfg([a, os.path.join(self.tmp, "missing"), b]))
        self.assertEqual(paths[:2], [a, b])

    def test_current_directory_without_a_work_root(self):
        self.assertEqual(self.paths(self.cfg())[0], os.getcwd())

    def test_state_dir(self):
        state = self.dir("state")
        self.assertIn(state, self.paths(self.cfg(state_dir=state)))

    def test_deps_cache_root_of_the_policy(self):
        state, cache = self.dir("state"), self.dir("cache")
        paths = self.paths(self.cfg(state_dir=state))
        self.assertIn(os.path.join(state, menage.MANAGED_DIR, "cache"), paths)
        paths = self.paths(self.cfg(state_dir=state), menage.Policy(cache_root=cache))
        self.assertIn(cache, paths)

    def test_tmp_roots_unless_in_memory(self):
        """r1 (1): the turns' tmp root and the system tmp on their own disk
        filesystem are watched; a tmpfs keeps its own measure."""
        state, scratch, system = self.dir("state"), self.dir("scratch"), self.dir("tmp")
        cfg, policy = self.cfg(state_dir=state), menage.Policy(tmp_root=scratch)
        with mock.patch.dict(os.environ, {"AMEESH_SYSTEM_TMP": system}), \
                mock.patch.object(menage, "in_memory", return_value=False):
            paths = self.paths(cfg, policy)
            self.assertIn(os.path.join(state, menage.MANAGED_DIR, "tmp"), self.paths(cfg))
        self.assertIn(scratch, paths)
        self.assertIn(system, paths)
        with mock.patch.dict(os.environ, {"AMEESH_SYSTEM_TMP": system}), \
                mock.patch.object(menage, "in_memory", side_effect=lambda p: p == system):
            paths = self.paths(cfg, policy)
        self.assertIn(scratch, paths)
        self.assertNotIn(system, paths)

    def test_docker_root_best_effort(self):
        self.assertIn("/var/lib/docker", self.paths(self.cfg(), docker="/var/lib/docker"))
        self.assertNotIn(None, self.paths(self.cfg(), docker=None))

    def test_root_always(self):
        self.assertEqual(self.paths(self.cfg())[-1], os.sep)


class TmpfsRootsTest(_Tmp):
    """#306 (2): state on a tmpfs and the turns' tmp root on another tmpfs:
    both are sampled, the fullest one is the reading's tmp measure."""

    def test_fullest_tmpfs_among_the_tmp_roots(self):
        turn_tmp, system = self.dir("turn-tmp"), self.dir("tmp")
        cfg = types.SimpleNamespace(worktree_roots=[], state_dir=self.dir("state"))
        policy = menage.Policy(tmp_root=turn_tmp)
        sizes = {turn_tmp: (10 * GIB, 9.5 * GIB), system: (12 * GIB, 1 * GIB)}

        def usage(path, **_kw):
            size, used = sizes[path]
            return {"tmp_path": path, "tmp_fstype": "tmpfs",
                    "tmp_size_bytes": int(size), "tmp_used_bytes": int(used)}

        with mock.patch.dict(os.environ, {"AMEESH_SYSTEM_TMP": system}), \
                mock.patch.object(menage, "in_memory", return_value=True), \
                mock.patch.object(menage, "_disk_root", side_effect=lambda d, _: d), \
                mock.patch.object(resources, "docker_root", return_value=None):
            self.assertEqual(resources.tmp_paths(cfg, policy), [turn_tmp, system])
            self.assertNotIn(turn_tmp, resources.disk_paths(cfg, policy))
            with mock.patch.object(resources, "fs_usage", side_effect=usage):
                reading = resources.sample("pc", paths=[], now=1.0,
                                           tmp=resources.tmp_paths(cfg, policy))
        self.assertEqual(reading["tmp_path"], turn_tmp)
        self.assertEqual(resources.tmpfs_fraction(reading), 0.95)
        limits = resources.thresholds({})
        found = {b["key"]: b for b in resources.breaches(reading, limits)}
        self.assertIn(turn_tmp, found["max_tmpfs_used"]["label"])
        with mock.patch.object(resources, "fs_usage", side_effect=usage):
            self.assertEqual(resources.sample("pc", paths=[], now=1.0,
                                              tmp=system)["tmp_path"], system)

    def test_the_tmp_type_is_the_mount_the_path_resolves_to(self):
        # #289 r2 (2): a tmpfs at /turn/tmpfs, then an ext4 mounted over
        # /turn: /turn/tmpfs/managed is on the ext4, never read as a tmpfs
        managed = self.dir("turn", "tmpfs", "managed")
        turn = os.path.join(self.tmp, "turn")
        table = [{"id": 1, "point": os.sep, "dev": 1, "fstype": "ext4", "source": "/dev/a"},
                 {"id": 7, "point": os.path.join(turn, "tmpfs"), "dev": 7, "fstype": "tmpfs",
                  "source": "tmpfs"},
                 {"id": 8, "point": turn, "dev": 8, "fstype": "ext4", "source": "/dev/e"}]
        for answer in (8, None):    # the kernel's mount id, else the visible mount
            with mock.patch.object(platform, "is_linux", return_value=True), \
                    mock.patch.object(platform, "mountinfo", return_value=table), \
                    mock.patch.object(platform, "mount_id", return_value=answer):
                reading = resources.fs_usage(managed)
            self.assertEqual(reading["tmp_fstype"], "ext4")
            self.assertIsNone(resources.tmpfs_fraction(reading))

    def test_a_tmp_root_not_created_yet_is_read_on_its_tmpfs(self):
        # #289 r1 (3): the managed tmp root does not exist yet on a 95 % full
        # tmpfs; the system tmp is at 10 %: the tmpfs it will live on is read
        turn_tmp, system = self.dir("turn-tmp"), self.dir("tmp")
        managed = os.path.join(turn_tmp, "ameesh", "tmp")
        sizes = {turn_tmp: (10 * GIB, 9.5 * GIB), system: (10 * GIB, 1 * GIB)}

        def usage(path, **_kw):
            if path not in sizes:   # the real fs_usage: no reading off a dir
                return {"tmp_path": path, "tmp_fstype": None,
                        "tmp_size_bytes": None, "tmp_used_bytes": None}
            size, used = sizes[path]
            return {"tmp_path": path, "tmp_fstype": "tmpfs",
                    "tmp_size_bytes": int(size), "tmp_used_bytes": int(used)}

        with mock.patch.object(resources, "fs_usage", side_effect=usage):
            reading = resources.tmp_usage([managed, system])
        self.assertEqual(reading["tmp_path"], managed)
        self.assertEqual(resources.tmpfs_fraction(reading), 0.95)


class MountinfoTest(_Tmp):
    """#306: the mount table, read without touching the mount points."""

    def test_parse_and_deepest_covering_mount(self):
        path = os.path.join(self.tmp, "mountinfo")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("33 2 0:30 /@ / rw,relatime shared:1 - btrfs /dev/mapper/root rw\n"
                     "60 33 0:30 /@home /home rw shared:2 - btrfs /dev/mapper/root rw\n"
                     "70 33 259:3 / /data/my\\040disk rw - ext4 /dev/nvme1n1p1 rw\n"
                     "71 70 0:44 / /data/my\\040disk/private/docker rw shared:9"
                     " master:3 - btrfs /dev/sdb rw\n"
                     "garbage\n")
        table = platform.mountinfo(path)
        self.assertEqual([m["point"] for m in table],
                         ["/", "/home", "/data/my disk", "/data/my disk/private/docker"])
        self.assertEqual(table[2]["dev"], os.makedev(259, 3))
        self.assertEqual((table[3]["fstype"], table[3]["source"]), ("btrfs", "/dev/sdb"))
        deep = platform.covering_mount("/data/my disk/private/docker/overlay2", table)
        self.assertEqual(deep["dev"], os.makedev(0, 44))
        self.assertEqual(platform.covering_mount("/data/my disk/x", table)["point"],
                         "/data/my disk")
        self.assertEqual(platform.covering_mount("/homework", table)["point"], "/")
        self.assertEqual(platform.mountinfo(os.path.join(self.tmp, "none")), [])
        self.assertEqual([m["id"] for m in table], [33, 60, 70, 71])
        # #289 r1: a mount over a parent hides the deeper one
        table.append({"id": 80, "point": "/data/my disk/private", "dev": 9,
                      "fstype": "ext4", "source": "/dev/x"})
        self.assertEqual(platform.covering_mount(
            "/data/my disk/private/docker/overlay2", table)["id"], 80)

    def test_mount_id_names_the_mount_a_path_resolves_to(self):
        if not platform.is_linux():
            self.skipTest("Linux only")
        self.assertEqual(platform.mount_id(self.tmp), platform.covering_mount(self.tmp)["id"])
        self.assertIsNone(platform.mount_id(os.path.join(self.tmp, "missing")))


class DockerRootTest(_Tmp):
    def run_docker(self, stdout="/var/lib/docker\n", code=0):
        return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")

    def test_read_from_docker_info_and_kept(self):
        with mock.patch.object(resources.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(resources.subprocess, "run",
                                  return_value=self.run_docker()) as run:
            self.assertEqual(resources.docker_root(now=0.0), "/var/lib/docker")
            self.assertEqual(resources.docker_root(now=10_000.0), "/var/lib/docker")
        run.assert_called_once()
        self.assertIn("{{.DockerRootDir}}", run.call_args[0][0])

    def test_without_docker_none(self):
        with mock.patch.object(resources.shutil, "which", return_value=None):
            self.assertIsNone(resources.docker_root(now=0.0))

    def test_failure_retried_later(self):
        with mock.patch.object(resources.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(resources.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("docker", 5)) as run:
            self.assertIsNone(resources.docker_root(now=0.0))
            self.assertIsNone(resources.docker_root(now=60.0))
            self.assertEqual(run.call_count, 1)
            self.assertIsNone(resources.docker_root(now=resources.DOCKER_RETRY_S + 1))
            self.assertEqual(run.call_count, 2)
        with mock.patch.object(resources.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(resources.subprocess, "run",
                                  return_value=self.run_docker("not a path", 0)):
            self.assertIsNone(resources.docker_root(now=10 * resources.DOCKER_RETRY_S))


class VolumesTest(_Tmp):
    def fake(self, fsids: dict, free: dict, btrfs: dict):
        """Patches the readers: btrfs fsid, free space and allocation per path."""
        def fs_info(path):
            for prefix, fsid in fsids.items():
                if path.startswith(prefix):
                    return (fsid, None)
            return None

        def usage(path):
            for prefix, values in btrfs.items():
                if path.startswith(prefix):
                    return {"btrfs_unallocated_bytes": values[0],
                            "btrfs_metadata_headroom_bytes": values[1]}
            return {"btrfs_unallocated_bytes": None, "btrfs_metadata_headroom_bytes": None}

        def disk(path):
            for prefix, value in free.items():
                if path.startswith(prefix):
                    return value
            return None

        return [mock.patch.object(platform, "is_linux", return_value=True),
                mock.patch.object(platform, "btrfs_fs_info", side_effect=fs_info),
                mock.patch.object(resources, "btrfs_usage", side_effect=usage),
                mock.patch.object(resources, "disk_free", side_effect=disk)]

    def test_dedupe_by_btrfs_fsid(self):
        ssd, state, cache = self.dir("ssd"), self.dir("system", "state"), \
            self.dir("system", "cache")
        system = os.path.join(self.tmp, "system")
        patches = self.fake({ssd: FSID_SSD, system: FSID_SYSTEM},
                            {ssd: 400 * GIB, system: 70 * GIB},
                            {ssd: (400 * GIB, 200 * GIB), system: (3 * GIB, 5 * GIB)})
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        vols = resources.volumes([ssd, state, cache, state])
        self.assertEqual([v["path"] for v in vols], [ssd, state])
        self.assertEqual(vols[1]["paths"], [state, cache])
        self.assertEqual(vols[1]["btrfs_unallocated_bytes"], 3 * GIB)

    def test_dedupe_by_st_dev_off_btrfs(self):
        a, b = self.dir("a"), self.dir("b")
        with mock.patch.object(platform, "btrfs_fs_info", return_value=None):
            vols = resources.volumes([a, b])
        self.assertEqual(len(vols), 1)  # one temporary filesystem
        self.assertEqual(vols[0]["paths"], [a, b])
        self.assertIsNotNone(vols[0]["disk_free_bytes"])

    def test_unreadable_path_probes_its_parent_on_the_same_filesystem(self):
        parent = self.dir("lib")
        locked = self.dir("lib", "docker")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)
        if os.access(locked, os.R_OK):
            self.skipTest("running as root")
        self.assertEqual(resources._probe(locked), parent)
        self.assertEqual(resources._probe(parent), parent)

    def test_untraversable_or_missing_path_read_on_its_ancestor(self):
        """r1 (2): /data/private/docker with `private` denying traversal (stat
        fails) and a cache dir not created yet are read on the filesystem of
        their nearest accessible ancestor, so an exhausted one breaches."""
        data = self.dir("data")
        docker = os.path.join(self.dir("data", "private", "docker"))
        private = os.path.dirname(docker)
        os.chmod(private, 0)
        self.addCleanup(os.chmod, private, 0o700)
        try:
            os.stat(docker)
            self.skipTest("running as root")
        except OSError:
            pass
        missing = os.path.join(self.tmp, "nocache", "deps")
        self.assertEqual(resources._probe(docker), data)
        self.assertEqual(resources._probe(missing), self.tmp)
        patches = self.fake({data: FSID_SYSTEM}, {data: 70 * GIB, self.tmp: 50 * GIB},
                            {data: (0, 5 * GIB)})
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        reading = resources.sample("pc", paths=[docker], now=100.0)
        self.assertEqual(reading["volumes"][0]["disk_free_bytes"], 70 * GIB)
        self.assertEqual(reading["btrfs_unallocated_bytes"], 0)
        limits = resources.thresholds({"resources": {"min_btrfs_unallocated": "10GiB"}})
        found = {b["key"]: b for b in resources.breaches(reading, limits)}
        self.assertEqual(found["min_btrfs_unallocated"]["path"], docker)
        vol = resources.volumes([missing])[0]
        self.assertEqual(vol["path"], missing)
        self.assertEqual(vol["disk_free_bytes"], 50 * GIB)

    def test_mount_behind_an_untraversable_dir_never_read_on_its_parent(self):
        """#306 (1): a btrfs mounted AT /data/private/docker, `private` denying
        traversal: the docker volume is read through another reachable mount
        of its filesystem, else unreadable — never on the healthy /data."""
        data = self.dir("data")
        docker = self.dir("data", "private", "docker")
        other = self.dir("dockerfs")
        private = os.path.dirname(docker)
        os.chmod(private, 0)
        self.addCleanup(os.chmod, private, 0o700)
        try:
            os.stat(docker)
            self.skipTest("running as root")
        except OSError:
            pass
        table = [{"id": 1, "point": os.sep, "dev": 1, "fstype": "ext4", "source": "/dev/a"},
                 {"id": 2, "point": data, "dev": 2, "fstype": "btrfs", "source": "/dev/b"},
                 {"id": 3, "point": docker, "dev": 3, "fstype": "btrfs", "source": "/dev/c"}]
        patches = self.fake({data: FSID_SYSTEM, other: FSID_SSD},
                            {data: 70 * GIB, other: 30 * GIB},
                            {data: (300 * GIB, 5 * GIB), other: (0, 1 * GIB)})
        patches.append(mock.patch.object(platform, "mount_id",
                                         side_effect=lambda p: 4 if p == other else None))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(platform, "mountinfo", return_value=table):
            self.assertIsNone(resources._anchor(docker))
            vols = resources.volumes([data, docker])
        self.assertEqual([v["path"] for v in vols], [data, docker])
        self.assertTrue(vols[1]["unreadable"])
        self.assertIsNone(vols[1]["btrfs_unallocated_bytes"])
        self.assertEqual(vols[0]["btrfs_unallocated_bytes"], 300 * GIB)
        # the same filesystem also mounted where the executor can reach it
        table.append({"id": 4, "point": other, "dev": 3, "fstype": "btrfs", "source": "/dev/c"})
        with mock.patch.object(platform, "mountinfo", return_value=table):
            self.assertEqual(resources._probe(docker), other)
            reading = resources.sample("pc", paths=[data, docker], now=1.0)
        self.assertEqual(reading["btrfs_unallocated_bytes"], 0)
        limits = resources.thresholds({"resources": {"min_btrfs_unallocated": "10GiB"}})
        found = {b["key"]: b for b in resources.breaches(reading, limits)}
        self.assertEqual(found["min_btrfs_unallocated"]["path"], docker)

    def untraversable_docker(self):
        """(data, docker): /data/private/docker, `private` denying traversal;
        skipped as root."""
        data = self.dir("data")
        docker = self.dir("data", "private", "docker")
        private = os.path.dirname(docker)
        os.chmod(private, 0)
        self.addCleanup(os.chmod, private, 0o700)
        try:
            os.stat(docker)
            self.skipTest("running as root")
        except OSError:
            pass
        for p in self.fake({data: FSID_SYSTEM}, {data: 70 * GIB}, {data: (300 * GIB, 5 * GIB)}):
            p.start()
            self.addCleanup(p.stop)
        return data, docker

    def test_a_mount_hidden_by_a_mount_over_its_parent_is_not_read(self):
        """#289 r1 (1): the docker filesystem also mounted at /rescue/fs, then
        /rescue mounted over by a healthy filesystem holding a `fs` dir: the
        docker volume is not read on that healthy `fs`."""
        data, docker = self.untraversable_docker()
        rescue_fs = self.dir("rescue", "fs")
        rescue = os.path.dirname(rescue_fs)
        table = [{"id": 1, "point": os.sep, "dev": 1, "fstype": "ext4", "source": "/dev/a"},
                 {"id": 2, "point": data, "dev": 2, "fstype": "btrfs", "source": "/dev/b"},
                 {"id": 3, "point": docker, "dev": 3, "fstype": "btrfs", "source": "/dev/c"},
                 {"id": 4, "point": rescue_fs, "dev": 3, "fstype": "btrfs", "source": "/dev/c"},
                 {"id": 5, "point": rescue, "dev": 5, "fstype": "ext4", "source": "/dev/e"}]
        # the kernel resolves /rescue/fs to the mount over /rescue
        ids = {rescue_fs: 5, rescue: 5}
        with mock.patch.object(platform, "mountinfo", return_value=table), \
                mock.patch.object(platform, "mount_id", side_effect=ids.get):
            self.assertEqual(platform.covering_mount(rescue_fs, table)["id"], 5)
            self.assertIsNone(resources._anchor(docker))
            # listed in another order, the mount id alone still tells
            with mock.patch.object(platform, "mountinfo",
                                   return_value=[table[i] for i in (0, 1, 2, 4, 3)]):
                self.assertIsNone(resources._anchor(docker))
                vols = resources.volumes([data, docker])
                self.assertTrue(vols[1]["unreadable"])
                # where the kernel does reach that mount, it is read there
                ids[rescue_fs] = 4
                self.assertEqual(resources._anchor(docker), rescue_fs)

    def test_an_alternate_mount_must_open_read_only(self):
        """#289 r2 (1): O_PATH opens a mode-000 directory; the btrfs ioctls
        need O_RDONLY. /rescue (mode 000) and /rescue2 (readable) mount the
        docker filesystem: /rescue2 is read."""
        data, docker = self.untraversable_docker()
        rescue, rescue2 = self.dir("rescue"), self.dir("rescue2")
        os.chmod(rescue, 0)
        self.addCleanup(os.chmod, rescue, 0o700)
        table = [{"id": 1, "point": os.sep, "dev": 1, "fstype": "ext4", "source": "/dev/a"},
                 {"id": 2, "point": data, "dev": 2, "fstype": "btrfs", "source": "/dev/b"},
                 {"id": 3, "point": docker, "dev": 3, "fstype": "btrfs", "source": "/dev/c"},
                 {"id": 4, "point": rescue, "dev": 3, "fstype": "btrfs", "source": "/dev/c"},
                 {"id": 5, "point": rescue2, "dev": 3, "fstype": "btrfs", "source": "/dev/c"}]
        with mock.patch.object(platform, "mountinfo", return_value=table), \
                mock.patch.object(platform, "mount_id", side_effect={rescue: 4, rescue2: 5}.get):
            self.assertEqual(resources._anchor(docker), rescue2)
            os.chmod(rescue2, 0)
            self.addCleanup(os.chmod, rescue2, 0o700)
            self.assertIsNone(resources._anchor(docker))

    def test_a_symlink_to_an_unreachable_mount_is_resolved(self):
        """#289 r1 (2): /docker -> /data/private/docker, a mount behind an
        untraversable dir: unreadable, never credited to the link's parent."""
        data, docker = self.untraversable_docker()
        link = os.path.join(self.tmp, "docker-link")
        os.symlink(docker, link)
        table = [{"id": 1, "point": os.sep, "dev": 1, "fstype": "ext4", "source": "/dev/a"},
                 {"id": 2, "point": data, "dev": 2, "fstype": "btrfs", "source": "/dev/b"},
                 {"id": 3, "point": docker, "dev": 3, "fstype": "btrfs", "source": "/dev/c"}]
        with mock.patch.object(platform, "mountinfo", return_value=table):
            self.assertIsNone(resources._anchor(link))
            self.assertEqual(resources.volume_key(link), "mount:3")
            vols = resources.volumes([data, link])
        self.assertEqual([v["path"] for v in vols], [data, link])
        self.assertTrue(vols[1]["unreadable"])

    def test_sample_takes_the_worst_reading_per_floor(self):
        ssd, state = self.dir("ssd"), self.dir("system", "state")
        system = os.path.join(self.tmp, "system")
        patches = self.fake({ssd: FSID_SSD, system: FSID_SYSTEM},
                            {ssd: 1 * GIB, system: 70 * GIB},
                            {ssd: (400 * GIB, 200 * GIB), system: (3 * GIB, 5 * GIB)})
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        reading = resources.sample("pc", paths=[ssd, state], now=100.0)
        self.assertEqual(reading["disk_free_bytes"], 1 * GIB)
        self.assertEqual(reading["disk_path"], ssd)
        self.assertEqual(reading["btrfs_unallocated_bytes"], 3 * GIB)
        self.assertEqual(reading["btrfs_metadata_headroom_bytes"], 5 * GIB)
        self.assertEqual(len(reading["volumes"]), 2)
        limits = resources.thresholds({"resources": {
            "min_disk_free": "2GiB", "min_btrfs_unallocated": "10GiB",
            "min_btrfs_metadata_headroom": "1GiB"}})
        found = {b["key"]: b for b in resources.breaches(reading, limits)}
        self.assertEqual(set(found), {"min_disk_free", "min_btrfs_unallocated"})
        self.assertEqual(found["min_disk_free"]["path"], ssd)
        self.assertEqual(found["min_btrfs_unallocated"]["path"], state)
        self.assertIn(state, resources.describe([found["min_btrfs_unallocated"]]))

    def test_single_path_unchanged(self):
        path = self.dir("work")
        with mock.patch.object(resources, "disk_free", return_value=5 * GIB), \
                mock.patch.object(resources, "btrfs_usage", return_value={
                    "btrfs_unallocated_bytes": None,
                    "btrfs_metadata_headroom_bytes": None}):
            reading = resources.sample("pc", path=path, now=1.0)
        self.assertEqual((reading["disk_free_bytes"], reading["disk_path"]), (5 * GIB, path))

    def test_reading_before_0359_still_judged(self):
        limits = resources.thresholds(None)
        found = resources.breaches({"disk_free_bytes": 1, "disk_path": "/w",
                                    "btrfs_unallocated_bytes": 1}, limits)
        self.assertEqual({b["key"]: b["path"] for b in found
                          if b["key"] in ("min_disk_free", "min_btrfs_unallocated")},
                         {"min_disk_free": "/w", "min_btrfs_unallocated": "/w"})


class AlertTest(unittest.TestCase):
    def test_host_disk_low_names_the_system_disk(self):
        reading = {"host": "pc", "sampled_ts": 100.0, "btrfs_unallocated_bytes": 3 * GIB,
                   "volumes": [
                       {"path": "/home/me/development", "paths": ["/home/me/development"],
                        "disk_free_bytes": 400 * GIB,
                        "btrfs_unallocated_bytes": 400 * GIB,
                        "btrfs_metadata_headroom_bytes": 200 * GIB},
                       {"path": "/home/me/.local/state/ameesh",
                        "paths": ["/home/me/.local/state/ameesh", "/var/lib/docker", "/"],
                        "disk_free_bytes": 70 * GIB, "btrfs_unallocated_bytes": 3 * GIB,
                        "btrfs_metadata_headroom_bytes": 5 * GIB}]}
        store = mock.MagicMock()
        store.hosts.current.return_value = [reading]
        store.turn_resources.orphans.return_value = []
        store.turn_resources.stale_running.return_value = []
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch("ameesh.canon.load_configured", return_value=[]), \
                mock.patch.object(exploitation, "_housekeeping_alerts", return_value=[]):
            alerts = exploitation._host_alerts(None, None, now=200.0)
        low = [a for a in alerts if a["type"] == "host_disk_low"]
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0]["paths"], ["/home/me/.local/state/ameesh"])
        self.assertIn("/home/me/.local/state/ameesh", low[0]["detail"])
        # the path stays out of the key: one episode
        moved = dict(low[0], paths=["/"])
        self.assertEqual(exploitation.alert_key(low[0]), exploitation.alert_key(moved))


class VolumesDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")

    def test_volumes_round_trip_and_hosts_json(self):
        vols = [{"path": "/ssd", "paths": ["/ssd"], "disk_free_bytes": 400 * GIB,
                 "btrfs_unallocated_bytes": None, "btrfs_metadata_headroom_bytes": None},
                {"path": "/state", "paths": ["/state", "/"], "disk_free_bytes": 70 * GIB,
                 "btrfs_unallocated_bytes": 3 * GIB,
                 "btrfs_metadata_headroom_bytes": 5 * GIB}]
        hosts = storage.of(self.db).hosts
        hosts.record({"host": "pc-253", "disk_free_bytes": 70 * GIB, "disk_path": "/state",
                      "volumes": vols})
        self.assertEqual(hosts.latest("pc-253")["volumes"], vols)
        self.assertEqual(hosts.current("pc-253")[0]["volumes"], vols)
        hosts.record({"host": "old", "disk_free_bytes": 1})  # before 0359
        self.assertEqual(hosts.latest("old")["volumes"], [])
        proc = self.mesh("hosts", "pc-253", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        objet = json.loads(proc.stdout.splitlines()[0])
        self.assertEqual([v["path"] for v in objet["volumes"]], ["/ssd", "/state"])
        texte = self.mesh("hosts", "pc-253")
        self.assertEqual(texte.returncode, 0, texte.stderr)
        self.assertIn("volume     /state: free 70.0 GiB", texte.stdout)
        self.assertIn("(also /)", texte.stdout)

    def test_supervisor_reads_volumes(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "deploy", "sql", "role-superviseur.sql"),
                  encoding="utf-8") as fh:
            self.assertIn("'volumes'", fh.read())


if __name__ == "__main__":
    unittest.main()
