# SPDX-License-Identifier: AGPL-3.0-only
"""L234 : surveillance sous pression et prises dans le périmètre de l'auditeur.

Ces tests n'ouvrent ni base ni harnais.
"""
import threading
import types
import unittest
from unittest import mock

from ameesh import backlog
from ameesh.runner import AgentWorker, Runner


def agent(name, **data):
    return types.SimpleNamespace(title=name, fiche=types.SimpleNamespace(data=data))


class AuditorTest(unittest.TestCase):
    def setUp(self):
        self.runner = Runner.__new__(Runner)
        self.runner.refresh_orchestrators(types.SimpleNamespace(agents=[
            agent("controle", roles=["auditor"]), agent("controle-fr", role="Auditeur"),
            agent("chef", roles=["orchestrateur"]), agent("dev", roles=["read"])]))

    def test_roles_and_persona(self):
        for name in ("auditeur", "controle", "controle-fr"):
            self.assertTrue(self.runner.is_auditor(name))
        self.assertFalse(self.runner.is_auditor("dev"))
        self.runner.refresh_orchestrators(types.SimpleNamespace(agents=[]))
        self.assertFalse(self.runner.is_auditor("controle"))

    def held(self, name, **pressure):
        worker = types.SimpleNamespace(name=name, runner=self.runner)
        return AgentWorker._held_by_pressure(worker, pressure)

    def test_auditor_passes_even_critical_and_power_pressure(self):
        for critical in (False, True):
            for breaches in ([{"key": "max_load"}], [{"power": "low"}]):
                for name in ("auditeur", "controle"):
                    self.assertFalse(self.held(name, blocked=True, critical=critical,
                                               breaches=breaches))
                self.assertTrue(self.held("dev", blocked=True, critical=critical,
                                         breaches=breaches))
        self.assertFalse(self.held("chef", blocked=True, critical=False, breaches=[]))
        self.assertTrue(self.held("chef", blocked=True, critical=True, breaches=[]))

    def test_priority_wrappers_preserve_command_arguments(self):
        argv = ["harness", "an argument with spaces"]
        with mock.patch("ameesh.runner.shutil.which", side_effect=lambda x: "/usr/bin/" + x):
            self.assertEqual(self.runner.audit_command("controle", argv),
                             ["/usr/bin/ionice", "-t", "-c", "3", "/usr/bin/nice",
                              "-n", "10"] + argv)
            self.assertEqual(self.runner.audit_command("dev", argv), argv)
        with mock.patch("ameesh.runner.shutil.which", return_value=None):
            self.assertEqual(self.runner.audit_command("auditeur", argv), argv)
        with mock.patch("ameesh.runner.shutil.which", side_effect=lambda x:
                        "/usr/bin/nice" if x == "nice" else None):
            self.assertEqual(self.runner.audit_command("auditeur", argv),
                             ["/usr/bin/nice", "-n", "10"] + argv)

    def test_pressure_journal_counts_auditors_separately(self):
        self.runner._pressure_lock = threading.Lock()
        self.runner._pressure_episode = {"orchestrator_turns": 0}
        self.runner.pressure_bypassed("auditeur")
        self.runner.pressure_bypassed("controle")
        self.runner.pressure_bypassed("chef")
        self.assertEqual(self.runner._pressure_episode["auditor_turns"], 2)
        self.assertEqual(self.runner._pressure_episode["orchestrator_turns"], 1)

    def take(self, item, name="auditeur", queue=False):
        row = {"name": name, "team": "ameesh", "host": "test", "harness": "codex"}
        work = types.SimpleNamespace(auto_takes_since=lambda *a: 0,
                                     unassigned_lots=lambda *a: [] if queue else [item],
                                     backlog=lambda **kw: [item] if queue else [])
        st = types.SimpleNamespace(work=work, operations=types.SimpleNamespace(
            open_lots_activity=lambda *a: []))
        cfg = types.SimpleNamespace(host=None)
        canon = types.SimpleNamespace(agents=[agent("controle", roles=["auditor"])])
        with mock.patch("ameesh.backlog.storage.of", return_value=st), \
                mock.patch("ameesh.registry.get", return_value={"capabilities": []}), \
                mock.patch("ameesh.canon.load_configured", return_value=[canon]), \
                mock.patch("ameesh.sous_utilisation.idle_agents", return_value=[row]):
            return backlog.auto_take(cfg, object(), now=10000, listing=[row],
                                     paid=set(), budget_check=lambda n: "", dry_run=True,
                                     orchestras=[])

    def test_auto_take_skips_evolutions_and_protected_bugs_for_both_roles(self):
        normal = {"id": 1, "type": "bug", "title": "Crash du runner", "state": "intake",
                  "created_ts": 100, "lot_team": "ameesh"}
        for name in ("auditeur", "controle"):
            self.assertEqual(len(self.take(normal, name)), 1)
            self.assertEqual(len(self.take(dict(normal, body="Corriger la fiche de dev"), name)), 1)
            for change in ({"type": "evolution"}, {"body": "docs/AUDITEUR.md"},
                           {"title": "décision 0036"}, {"body": "fiche de l'auditeur"},
                           {"package_id": "agents/" + name}):
                with self.subTest(name=name, change=change):
                    self.assertEqual(self.take(dict(normal, **change), name), [])
                    self.assertEqual(len(self.take(dict(normal, **change), "dev")), 1)

    def test_auditors_do_not_take_improvement_queue(self):
        item = {"id": 2, "type": "improvement", "title": "Optimisation", "team": "ameesh"}
        self.assertEqual(self.take(item, queue=True), [])
        self.assertEqual(self.take(item, "controle", queue=True), [])
        self.assertEqual(len(self.take(item, "dev", queue=True)), 1)
