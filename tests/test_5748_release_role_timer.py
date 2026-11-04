# SPDX-License-Identifier: AGPL-3.0-only
"""#5748: the release tick is posed by `ameesh role timers` on every candidate.

Constat (AG1, 04/11 11:14Z) : `packet_watch.role` déclaré, détenteur du bail
`mesh-design`, mais `ameesh-release.timer` introuvable : depuis #5015 le tic se
clôture lui-même sous le rôle élu, mais l'unité n'était posée qu'à la main, sur
un seul hôte ; la relève déplaçait le bail, pas la minuterie. Témoins : un
candidat dont la configuration nomme le rôle reçoit l'unité sous le nom de
`deploy/systemd` (une copie manuelle est remplacée, pas doublée) ; un coupeur
à hôte fixe n'est ni réécrit ni retiré ; la minuterie n'est jamais coupée
(#3364), ni par une configuration refusée ni par `--remove`.
"""
from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ameesh import hostcheck, role_timers

WATCH = {"role": "mesh-design",
         "projects": {"ameesh": {"repo": "/srv/ameesh", "notify": ["composeur", "pilote"]}}}


def cfg(release=None, packet_watch=None, host="agents-a"):
    return SimpleNamespace(host=host, release=release or {},
                           packet_watch=packet_watch if packet_watch is not None else WATCH)


def row(candidates=("agents-a", "agents-b")):
    return {"name": "mesh-design", "releve": "election", "host": "agents-a",
            "candidats": list(candidates)}


def plan(host="agents-a", release=("", "release tick")):
    return role_timers.plan(
        host=host, role="mesh-design", row=row(),
        settings=SimpleNamespace(projects={"ameesh": "manaty/ameesh"}, interval=900),
        watch_intervals=[900], notify_humans=["human:owner"], release=release)


def release_item(report):
    return next(item for item in report["timers"] if item["name"] == "release")


class ReachTest(unittest.TestCase):
    def test_the_watch_role_plans_the_tick(self):
        blocked, detail = role_timers.release_reach(cfg(), "mesh-design")
        self.assertEqual(blocked, "")
        self.assertIn("mesh-design", detail)

    def test_a_host_pinned_cutter_is_not_ours(self):
        legacy = {"host": "agents-b", "projects": {"ameesh": {"notify": ["pilote"]}}}
        self.assertIsNone(role_timers.release_reach(
            cfg(packet_watch=legacy, release={"host": "agents-b"}), "mesh-design"))
        self.assertIsNone(role_timers.release_reach(cfg(), "auditeur"))

    def test_roles_that_disagree_are_refused(self):
        blocked, _ = role_timers.release_reach(
            cfg(release={"role": "orchestrateur"}), "mesh-design")
        self.assertIn("disagree", blocked)

    def test_a_release_block_the_tick_refuses_is_named(self):
        blocked, detail = role_timers.release_reach(
            cfg(release={"role": "mesh-design", "bogus": 1}), "mesh-design")
        self.assertEqual(blocked, "release configuration refused")
        self.assertIn("bogus", detail)


class PlanTest(unittest.TestCase):
    def test_without_the_reach_the_plan_is_unchanged(self):
        names = [item["name"] for item in plan(release=None)["timers"]]
        self.assertNotIn("release", names)

    def test_a_candidate_gets_the_tick_under_the_deploy_name(self):
        item = release_item(plan())
        self.assertEqual(item["unit"], "ameesh-release")
        self.assertEqual(item["interval_s"], 60)
        self.assertFalse(item["blocked"])

    def test_a_refused_configuration_is_advisory(self):
        item = release_item(plan(release=("release configuration refused", "x")))
        self.assertTrue(item["advisory"])
        problems = role_timers.check(plan(release=("refused", "x")),
                                     directory=Path("/nonexistent"), apply=False)
        self.assertFalse([p for p in problems if p.startswith("release")])
        self.assertIn("release: refused — x",
                      role_timers.warnings(plan(release=("refused", "x"))))

    def test_a_non_candidate_gets_nothing(self):
        self.assertEqual(plan(host="agents-c")["timers"], [])


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="5748-units-")
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_rendered_unit_runs_the_self_fencing_tick(self):
        role_timers.install(plan(), directory=self.dir, bin_path="/opt/ameesh/bin/ameesh",
                            apply=False)
        service = (self.dir / "ameesh-release.service").read_text(encoding="utf-8")
        timer = (self.dir / "ameesh-release.timer").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/opt/ameesh/bin/ameesh release tick\n", service)
        self.assertIn("UnsetEnvironment=PYTHONPATH", service)
        self.assertIn("TimeoutStartSec=11h30min", service)
        self.assertIn("OnUnitActiveSec=60s", timer)
        deploy = Path(__file__).resolve().parents[1] / "deploy/systemd/ameesh-release.timer"
        for line in ("OnBootSec=1min", "OnUnitActiveSec=60s", "Persistent=true"):
            self.assertIn(line, deploy.read_text(encoding="utf-8"))
        self.assertEqual(role_timers.check(plan(), directory=self.dir, apply=False,
                                           bin_path="/opt/ameesh/bin/ameesh"), [])

    def test_a_hand_copy_is_replaced_not_doubled(self):
        (self.dir / "ameesh-release.service").write_text("[Service]\nExecStart=old\n")
        role_timers.install(plan(), directory=self.dir, apply=False)
        self.assertIn("release tick", (self.dir / "ameesh-release.service").read_text())
        self.assertEqual(sorted(p.name for p in self.dir.glob("*release*")),
                         ["ameesh-release.service", "ameesh-release.timer"])

    def test_the_timer_is_never_cut(self):
        hand = "[Service]\nExecStart=hand\n"
        (self.dir / "ameesh-release.service").write_text(hand)
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(role_timers, "_systemctl", return_value=ok) as systemctl:
            role_timers.install(plan(release=("refused", "x")), directory=self.dir)
            role_timers.remove(plan(), directory=self.dir)
        self.assertEqual((self.dir / "ameesh-release.service").read_text(), hand)
        for call in systemctl.call_args_list:
            self.assertNotIn("ameesh-release.timer", call.args)


class DoctorTest(unittest.TestCase):
    def test_a_role_names_the_role_timers_gesture(self):
        lines = hostcheck.packet_watch_lines(
            types.SimpleNamespace(host="agents-a", packet_watch=WATCH),
            run=lambda *args: "inactive\n")
        text = "\n".join(lines)
        self.assertIn("ameesh role timers --role mesh-design --install", text)
        self.assertNotIn("enable --now", text)
        self.assertIn("one cutter", text)


if __name__ == "__main__":
    unittest.main()
