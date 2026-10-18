# SPDX-License-Identifier: AGPL-3.0-only
"""#2211: follow-ups of the #2185 review (export mode, corrupt refs, inherited cycle)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from .support import PgTestCase
from ameesh import roadmap, roadmap_edit as edit, storage


@unittest.skipUnless(os.name == "posix", "POSIX file permissions")
class AtomicExportModeTest(unittest.TestCase):
    def test_private_content_stays_private_before_replacement(self):
        with tempfile.TemporaryDirectory(prefix="ameesh-2211-mode-") as scratch:
            target = Path(scratch) / "private.json"
            target.write_text("old", encoding="utf-8")
            target.chmod(0o600)
            link = Path(scratch) / "link.json"
            link.symlink_to(target.name)
            for destination in (target, link):
                with self.subTest(destination=destination.name):
                    observed = []

                    def inspect(fd):
                        observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
                        temporary = next(Path(scratch).glob("private.json.*.tmp"))
                        self.assertEqual(temporary.read_text(encoding="utf-8"), "private")

                    previous = os.umask(0o022)
                    try:
                        with mock.patch.object(roadmap.os, "fsync", side_effect=inspect):
                            roadmap._write_atomic(str(destination), "private")
                    finally:
                        os.umask(previous)
                    self.assertEqual(observed, [0o600])
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                    self.assertEqual(target.read_text(encoding="utf-8"), "private")
                    self.assertTrue(link.is_symlink())


class RoadmapFollowupsPgTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE commitments RESTART IDENTITY CASCADE")

    def rows(self):
        return storage.of(self.db).roadmap.commitments()

    def store_dependencies(self, commitment, refs):
        """Write depends_on directly: the CLI rightly refuses to create these."""
        self.db.execute("UPDATE commitments SET depends_on = %s::jsonb WHERE id = %s",
                        (json.dumps(refs), commitment["id"]))

    def test_a_new_export_honours_the_umask(self):
        roadmap.add_commitment(self.db, "Mode", due="2026-12-01")
        outfile = Path(self.tmp) / "mode.json"
        previous = os.umask(0o022)
        try:
            out = self.mesh("plan", "export", "--output", str(outfile))
        finally:
            os.umask(previous)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(stat.S_IMODE(outfile.stat().st_mode), 0o644)

    def test_an_export_writes_through_a_symlinked_target(self):
        roadmap.add_commitment(self.db, "Link", due="2026-12-01")
        referent = Path(self.tmp) / "real.json"
        referent.write_text("old", encoding="utf-8")
        referent.chmod(0o640)
        link = Path(self.tmp) / "link.json"
        link.symlink_to(referent)
        out = self.mesh("plan", "export", "--output", str(link))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(link.is_symlink(), "the link itself must survive")
        self.assertEqual(json.loads(referent.read_text(encoding="utf-8"))["schema"], edit.SCHEMA)
        self.assertEqual(stat.S_IMODE(referent.stat().st_mode), 0o640)

    def test_corrupt_stored_dependencies_do_not_crash_an_edit(self):
        a = roadmap.add_commitment(self.db, "A", due="2026-12-01")
        b = roadmap.add_commitment(self.db, "B", due="2026-12-02")
        # Legacy jsonb written outside the CLI: a dict is not hashable.
        self.store_dependencies(a, [{"unhashable": 1}])
        row = edit.update(self.db, a["id"], {"depends_on": ["e%d" % b["id"]]})
        self.assertEqual(row["depends_on"], ["e%d" % b["id"]])

    def test_corrupt_stored_dependencies_do_not_crash_an_import(self):
        a = edit.create(self.db, {"what": "A", "key": "a"})
        b = edit.create(self.db, {"what": "B", "key": "b"})
        self.store_dependencies(a, [[], {"nested": True}])
        body = edit.export(self.db)
        for entry in body["commitments"]:
            if entry["key"] == "a":
                entry["depends_on"] = ["e%d" % b["id"]]
        preview = edit.import_plan(self.db, body, update_existing=True)
        self.assertEqual([change["action"] for change in preview["changes"]], ["update", "unchanged"])

    def test_an_inherited_cycle_does_not_refuse_a_downstream_dependency(self):
        a = roadmap.add_commitment(self.db, "A", due="2026-12-01")
        b = roadmap.add_commitment(self.db, "B", due="2026-12-02")
        c = roadmap.add_commitment(self.db, "C", due="2026-12-03")
        # A <-> B is the inherited cycle; C is a dependency downstream of A.
        self.store_dependencies(a, ["e%d" % b["id"]])
        self.store_dependencies(b, ["e%d" % a["id"]])
        warnings = []
        row = edit.update(self.db, a["id"],
                          {"depends_on": ["e%d" % b["id"], "e%d" % c["id"]]},
                          warnings=warnings)
        self.assertEqual(row["depends_on"], ["e%d" % b["id"], "e%d" % c["id"]])
        cycles = [w for w in warnings if w["code"] == "dependency_cycle"]
        self.assertEqual([(w["code"], w.get("stale")) for w in cycles],
                         [("dependency_cycle", True)])
        self.assertEqual(edit.get(self.db, c["id"])["depends_on"], [])

    def test_a_new_edge_that_closes_a_cycle_is_still_refused(self):
        a = roadmap.add_commitment(self.db, "A", due="2026-12-01")
        b = roadmap.add_commitment(self.db, "B", due="2026-12-02")
        c = roadmap.add_commitment(self.db, "C", due="2026-12-03")
        self.store_dependencies(a, ["e%d" % b["id"]])
        self.store_dependencies(b, ["e%d" % a["id"]])
        self.store_dependencies(c, ["e%d" % a["id"]])
        with self.assertRaises(edit.InvalidDependencies) as caught:
            edit.update(self.db, a["id"],
                        {"depends_on": ["e%d" % b["id"], "e%d" % c["id"]]})
        self.assertEqual(caught.exception.report["errors"][0]["code"], "dependency_cycle")
        self.assertEqual(edit.get(self.db, a["id"])["depends_on"], ["e%d" % b["id"]])


if __name__ == "__main__":
    unittest.main()
