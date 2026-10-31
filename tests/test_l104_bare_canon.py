# SPDX-License-Identifier: AGPL-3.0-only
"""L104 T6 (#4414): a new canon starts without any project module, end to end.

`canon check`, `canon sync` and a core work item, through the real CLI, on a
canon that activates nothing (no `extensions.ameesh.projects`). Decision 6 of
#4403: the bare registry alone (`discover(builtin=False)`,
tests/test_l98_modules_registry.py) does not prove this, and the run-time bare
profile's housekeeping and worktree no-ops stay pinned by
tests/test_l98_bare_profile.py and tests/test_l101_workspaces_facade.py.
"""
from __future__ import annotations

import json
import tempfile

from ameesh import modules
from ameesh.modules import registry
from ameesh.modules.storage import ModuleStore
from .support import PgTestCase, needs_builtin_modules
from .test_canon import publish

HOST = "h4414"
PROJECT = "demo4414"


class BareCanonTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM project_modules")
        self.db.execute("DELETE FROM canon_state")
        self.db.execute("TRUNCATE authenticators, standing_approvals, standing_reservations, "
                        "authenticator_syncs RESTART IDENTITY CASCADE")
        _, self.root = publish(tempfile.mkdtemp(dir=self.tmp), "canon4414")

    def env_for(self, **extra):
        return self.env(AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
                        AMEESH_HOST=HOST, **extra)

    def run_ok(self, *args, **extra):
        proc = self.mesh(*args, env=self.env_for(**extra))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def start_and_track_a_core_item(self, **extra):
        self.run_ok("canon", "check", "--host", HOST, **extra)
        self.run_ok("canon", "sync", "--host", HOST,
                    "--bootstrap-ref", "main", **extra)
        self.assertEqual(ModuleStore(self.db).stored(), [])
        title = "core item 4414 " + (extra.get("AMEESH_MODULES") or "standard")
        self.run_ok("work", "add", "--title", title, "--app", PROJECT, "--new-project",
                    "--type", "evolution", "--actor", "human:test", **extra)
        listed = json.loads(self.run_ok("work", "list", "--json", **extra))
        self.assertIn(title, [item["title"] for item in listed])

    @needs_builtin_modules
    def test_standard_profile_activates_nothing_and_keeps_dev(self):
        self.start_and_track_a_core_item()
        installed = json.loads(self.run_ok("modules", "--json"))
        builtin = {row["name"] for row in installed["modules"]
                   if row["source"] == "builtin"}
        self.assertEqual(builtin, {"dev", "it", "work-types"})
        self.assertEqual(modules.always_active(), ("dev",))
        self.assertIsNotNone(registry.get("dev"))
        active = json.loads(self.run_ok("modules", "--active", "--json"))
        self.assertEqual(active["active"], [])

    def test_bare_profile_runs_the_same_path_without_any_module(self):
        self.start_and_track_a_core_item(AMEESH_MODULES="bare")
        self.assertEqual(self.run_ok("modules", AMEESH_MODULES="bare").strip(), "no module")
