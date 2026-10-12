# SPDX-License-Identifier: AGPL-3.0-only
"""L160g (#473): the conditions of the Nexlink orchestrator on L160f.

Generated files regenerated on the merged tree, never resolved; gates chosen
by the paths the merge touches, GitHub CI green on the frozen commit before
entering the queue; SQL migrations reserved through ameesh; protected targets
(main, release branches, tags) never merged by the queue."""
from __future__ import annotations

import dataclasses

from ameesh import land, merge_queue, radar, resolutions

from .test_l160b_radar import _git
from .test_l160e2_land import _Land

import unittest

NEXLINK = {"repo": "/srv/nexlink", "target": "develop", "land": "local",
           "gates": ["backend/scripts/check-rollbacks.sh",
                     {"run": "npm run domains-export -- --check",
                      "paths": ["backend/domains/**", "backend/domains.json"]}],
           "generated": [{"path": "backend/domains.json", "regen": "npm run domains-export",
                          "check": "npm run domains-export -- --check"}],
           "ci_required": True,
           "protected": ["main", "desktop-v*", "nexlink-releases*", "refs/tags/*"]}


class SettingsTest(unittest.TestCase):
    def test_nexlink(self):
        project = radar.parse_settings({"projects": {"nexlink": NEXLINK}}).projects["nexlink"]
        self.assertEqual(project.gates, ("backend/scripts/check-rollbacks.sh",))
        self.assertEqual(project.path_gates, (radar.Gate(
            "npm run domains-export -- --check",
            ("backend/domains/**", "backend/domains.json")),))
        self.assertEqual(project.generated, (radar.Generated(
            "backend/domains.json", "npm run domains-export",
            "npm run domains-export -- --check"),))
        self.assertTrue(project.ci_required)
        self.assertEqual(project.protected[0], "main")

    def test_invalid(self):
        for key, value in (("generated", [{"path": "x"}]), ("generated", ["x"]),
                           ("gates", [{"run": "x", "paths": [1]}]),
                           ("gates", [{"run": "x", "when": ["a"]}]),
                           ("ci_required", "yes"), ("protected", [""])):
            with self.subTest(key=key, value=value):
                with self.assertRaises(radar.SettingsError):
                    radar.parse_settings({"projects": {"p": dict(NEXLINK, **{key: value})}})

    def test_a_landed_project_has_a_gate_of_some_kind(self):
        only_paths = dict(NEXLINK, gates=[{"run": "x", "paths": ["a/**"]}])
        radar.parse_settings({"projects": {"p": only_paths}})
        with self.assertRaises(radar.SettingsError):
            radar.parse_settings({"projects": {"p": dict(NEXLINK, gates=[])}})


class ProtectedTest(unittest.TestCase):
    def test_globs(self):
        project = radar.parse_settings({"projects": {"n": NEXLINK}}).projects["n"]
        for target in ("main", "origin/main", "desktop-v1.4", "nexlink-releases",
                       "nexlink-releases/2026-10", "refs/tags/v0.2.11"):
            self.assertIsNotNone(radar.protected_target(project, target), target)
        for target in ("develop", "origin/develop", "feature/main"):
            self.assertIsNone(radar.protected_target(project, target), target)
        self.assertIsNone(radar.protected_target(radar.Project("p", "/r"), "main"))


class ProtectedQueueTest(_Land):
    def protect(self):
        self.project = dataclasses.replace(self.project, protected=("main",))

    def test_queue_add_refused(self):
        self.ready(4)
        self.protect()
        clone = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))
        with self.assertRaisesRegex(merge_queue.QueueError, "protected target"):
            merge_queue.enter(None, 4, repo=self.repo, actor="orch", ci=None, clone=clone,
                              project=self.project)
        self.assertNotIn(4, self.fake.queue_rows)

    def test_land_ejects_a_lot_queued_before(self):
        self.queued(4)
        self.protect()
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("protected target", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)


if __name__ == "__main__":
    unittest.main()
