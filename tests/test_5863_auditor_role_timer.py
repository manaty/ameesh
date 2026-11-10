# SPDX-License-Identifier: AGPL-3.0-only
"""#5863: the auditor's short pass is posed by `ameesh role timers --role auditeur`.

Constat (AG2, 05/11) : `~/.config/systemd/user/ameesh-auditeur-court.timer`
portait `OnCalendar=*-*-* 00/3:15:00`, posé à la main (24/10, #3371) et sans
unité versionnée, alors que la carte canon de l'auditeur dit « un passage court
chaque heure ». Témoins : un candidat du rôle `auditeur` reçoit l'unité sous le
nom de la copie manuelle (remplacée, pas doublée), chaque heure à :15, sous
`role run auditeur` ; ce rôle ne porte aucune autre passe et ne touche pas au
drop-in du notificateur ; un maillage sans rôle `auditeur` n'installe rien.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ameesh import role_timers


def row(candidates=("agents-a", "agents-b")):
    return {"name": "auditeur", "releve": "election", "host": "agents-a",
            "candidats": list(candidates)}


def plan(host="agents-a"):
    return role_timers.plan(host=host, role="auditeur", row=row(),
                            settings=None, watch_intervals=())


class PlanTest(unittest.TestCase):
    def test_a_candidate_gets_the_short_pass_only(self):
        report = plan()
        self.assertEqual([item["name"] for item in report["timers"]], ["auditeur-court"])
        self.assertEqual(report["timers"][0]["unit"], "ameesh-auditeur-court")
        self.assertEqual(report["timers"][0]["interval_s"], 3600)

    def test_a_non_candidate_gets_nothing(self):
        self.assertEqual(plan(host="poste")["timers"], [])


class RenderTest(unittest.TestCase):
    def test_the_timer_is_hourly_on_the_wall_clock(self):
        spec = role_timers.TimerSpec("auditeur-court", "auditeur-court", 3600, "d")
        timer = role_timers.render_timer(spec)
        self.assertIn("OnCalendar=*-*-* *:15:00\n", timer)
        self.assertNotIn("00/3", timer)
        self.assertIn("Persistent=true", timer)

    def test_the_service_runs_the_tick_under_its_lease(self):
        spec = role_timers.TimerSpec("auditeur-court", "auditeur-court", 3600, "d")
        service = role_timers.render_service(spec, role="auditeur")
        self.assertIn(
            "ExecStart=%h/.local/share/ameesh/venv/bin/ameesh role run auditeur -- "
            "%h/.local/share/ameesh/venv/bin/ameesh auditor tick\n", service)
        # #6643: the body now lives in the tick, not in the unit
        self.assertNotIn("agent-mail send", service)
        self.assertNotIn(role_timers.AUDITOR_MAIL, service)


class InstallTest(unittest.TestCase):
    def test_a_hand_copy_is_replaced_and_the_dropin_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "ameesh-auditeur-court.timer").write_text(
                "[Timer]\nOnCalendar=*-*-* 00/3:15:00\n", encoding="utf-8")
            (directory / "ameesh-notify.service").write_text("[Service]\n", encoding="utf-8")
            dropin = directory / "ameesh-notify.service.d" / "10-role-timers.conf"
            dropin.parent.mkdir()
            dropin.write_text("observation role\n", encoding="utf-8")
            report = plan()
            role_timers.install(report, directory=directory, apply=False)
            self.assertEqual(sorted(p.name for p in directory.glob("ameesh-auditeur-*")),
                             ["ameesh-auditeur-court.service", "ameesh-auditeur-court.timer"])
            self.assertIn("*:15:00", (directory / "ameesh-auditeur-court.timer").read_text())
            self.assertEqual(dropin.read_text(), "observation role\n")
            self.assertEqual(role_timers.check(report, directory=directory, apply=False), [])
            role_timers.remove(report, directory=directory, apply=False)
            self.assertFalse((directory / "ameesh-auditeur-court.timer").exists())
            self.assertEqual(dropin.read_text(), "observation role\n")

    def test_the_three_hour_copy_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            report = plan()
            role_timers.install(report, directory=directory, apply=False)
            (directory / "ameesh-auditeur-court.timer").write_text(
                "[Timer]\nOnCalendar=*-*-* 00/3:15:00\n", encoding="utf-8")
            problems = role_timers.check(report, directory=directory, apply=False)
            self.assertEqual(len(problems), 1)
            self.assertIn("ameesh-auditeur-court.timer is out of date", problems[0])


class MainTest(unittest.TestCase):
    def run_main(self, registry_row, argv):
        db = mock.Mock()
        with mock.patch.object(role_timers.config_mod, "load",
                               return_value=SimpleNamespace(host="agents-a")), \
                mock.patch.object(role_timers.db_mod, "connect", return_value=db), \
                mock.patch.object(role_timers.registry, "get", return_value=registry_row), \
                mock.patch.object(role_timers.plan_github, "settings_of") as settings:
            status = role_timers.main(argv)
        return status, settings

    def test_a_mesh_without_auditor_installs_nothing(self):
        status, _ = self.run_main(None, ["--role", "auditeur", "--install", "--no-apply"])
        self.assertEqual(status, 0)

    def test_the_auditor_role_reads_no_github(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, settings = self.run_main(
                row(), ["--role", "auditeur", "--install", "--no-apply",
                        "--systemd-dir", tmp])
            self.assertEqual(status, 0)
            settings.assert_not_called()
            self.assertTrue((Path(tmp) / "ameesh-auditeur-court.timer").is_file())


if __name__ == "__main__":
    unittest.main()
