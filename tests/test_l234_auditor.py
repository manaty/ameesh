# SPDX-License-Identifier: AGPL-3.0-only
"""L234 : surveillance sous pression et prises dans le périmètre de l'auditeur.

Ces tests n'ouvrent ni base ni harnais.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import threading
import types
import unittest
from unittest import mock

from ameesh import auditor, backlog, canon as canon_mod, work
from .support import FIXTURE_BUILD_CAP, patch_mechanics
from ameesh.runner import AgentWorker, Runner
from ameesh.platform import audit_guard


def agent(name, *, path=None, **data):
    path = path or "agents/" + name + ".md"
    return types.SimpleNamespace(title=name, fiche=types.SimpleNamespace(
        data=data, path=path, ref="owner:" + path + "@abcd"))


def canon(agents, *, ident="default", default=True, readable=True):
    return canon_mod.Canon(root="/test", id=ident, is_default=default,
                           readable=readable, agents=agents)


class AuditorTest(unittest.TestCase):
    def setUp(self):
        patch_mechanics(self)
        self.runner = Runner.__new__(Runner)
        self.runner.db = object()
        self.rows = {name: {"name": name, "canon": None} for name in
                     ("controle", "controle-fr", "chef", "dev", "auditeur")}
        self.getter = mock.patch("ameesh.registry.get",
                                 side_effect=lambda db, name: self.rows.get(name))
        self.getter.start()
        self.addCleanup(self.getter.stop)
        # #2451 : plafond §4 de fixture (le canon compilé en vrai)
        cap = mock.patch.object(work, "build_limit", return_value=FIXTURE_BUILD_CAP)
        cap.start()
        self.addCleanup(cap.stop)
        available = mock.patch("ameesh.platform.audit_guard.available", return_value=True)
        available.start()
        self.addCleanup(available.stop)
        self.runner.refresh_orchestrators(canon([
            agent("controle", roles=["auditor"]), agent("controle-fr", role="Auditeur"),
            agent("chef", roles=["orchestrateur"]), agent("dev", roles=["read"])]))

    def test_roles_and_persona(self):
        for name in ("auditeur", "controle", "controle-fr"):
            self.assertTrue(self.runner.is_auditor(name))
        self.assertFalse(self.runner.is_auditor("dev"))
        self.runner.refresh_orchestrators(canon([]))
        self.assertFalse(self.runner.is_auditor("controle"))

    def held(self, name, **pressure):
        worker = types.SimpleNamespace(name=name, runner=self.runner, agent=self.rows[name])
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

    def take(self, item, name="auditeur", queue=False, *, canons=None, owner=None,
             load_error=None, cfg=None):
        row = {"name": name, "team": "ameesh", "host": "test", "harness": "codex",
               "canon": owner}
        work = types.SimpleNamespace(auto_takes_since=lambda *a: 0,
                                     count_builds=lambda *a: 0,   # §4 : agent sous le plafond
                                     unassigned_lots=lambda *a: [] if queue else [item],
                                     project_plan_rows=lambda *a, **kw: [item],
                                     review_proposals=lambda *a, **kw: [],
                                     backlog=lambda **kw: [item] if queue else [])
        st = types.SimpleNamespace(work=work, operations=types.SimpleNamespace(
            open_lots_activity=lambda *a: []),
            hosts=types.SimpleNamespace(holds=lambda: []),   # #254: no host on hold
            progress=types.SimpleNamespace(                 # #3556 : aucun hold
                lot_hold_events=lambda *a, **kw: []))
        real_cfg = cfg is not None
        cfg = cfg or types.SimpleNamespace(host=None)
        loaded = canon([agent("controle", roles=["auditor"])])
        load_patch = (mock.patch("ameesh.auditor.load_all", wraps=auditor.load_all)
                      if real_cfg else mock.patch("ameesh.auditor.load_all",
                          return_value=canons or [loaded], side_effect=load_error))
        with mock.patch("ameesh.backlog.storage.of", return_value=st), \
                mock.patch("ameesh.registry.get", return_value=dict(row, capabilities=[])), \
                load_patch, \
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


    def test_r1_ownership_conflicting_canon_cannot_grant_auditor_role(self):
        a = canon([agent("dev", roles=["read"])], ident="A")
        b = canon([agent("dev", roles=["auditor"])], ident="B", default=False)
        self.runner.refresh_orchestrators(a, b)
        self.assertFalse(self.runner.is_auditor("dev"))
        self.assertTrue(self.held("dev", blocked=True, critical=True, breaches=[]))
        lot = {"id": 1, "type": "evolution", "title": "Evolution", "state": "intake",
               "created_ts": 100, "lot_team": "ameesh"}
        self.assertEqual(len(self.take(lot, "dev", canons=[a, b])), 1)
        self.rows["dev"]["canon"] = "B"
        self.assertTrue(self.runner.is_auditor("dev"))
        self.assertEqual(self.take(lot, "dev", canons=[a, b], owner="B"), [])

    def test_r1_unreadable_canon_never_allows_automatic_take(self):
        cfg = types.SimpleNamespace(canon_entries=[types.SimpleNamespace(path="bad")])
        for result in (canon([], readable=False), canon([])):
            if result.readable:
                result.scope_unread = {"owner"}
            with mock.patch("ameesh.canon.from_config", return_value=result):
                with self.assertRaises(canon_mod.CanonError):
                    auditor.load_all(cfg)
        with mock.patch("ameesh.canon.from_config", side_effect=canon_mod.CanonError("missing")):
            with self.assertRaises(canon_mod.CanonError):
                auditor.load_all(cfg)
        lot = {"id": 1, "type": "evolution", "title": "Evolution", "state": "intake",
               "created_ts": 100, "lot_team": "ameesh"}
        self.assertEqual(self.take(lot, "controle", load_error=canon_mod.CanonError("missing")), [])

    def test_r3_any_load_error_closes_automatic_take(self):
        cfg = types.SimpleNamespace(canon_entries=[types.SimpleNamespace(path="bad")])
        loaded = canon([])
        loaded.load_findings = [canon_mod.Finding("canon-git-error", canon_mod.ERROR,
                                                "objet git illisible")]
        with mock.patch("ameesh.canon.from_config", return_value=loaded):
            with self.assertRaises(canon_mod.CanonError):
                auditor.load_all(cfg)

    def test_r3_malformed_auditor_card_closes_automatic_take(self):
        lot = {"id": 1, "type": "evolution", "title": "Evolution", "state": "intake",
               "created_ts": 100, "lot_team": "ameesh"}
        with tempfile.TemporaryDirectory() as root:
            cards = Path(root) / "agents"
            cards.mkdir()
            card = cards / "controle.md"
            cfg = types.SimpleNamespace(canon=root, canon_ref="", canon_untrusted=True,
                                        host=None)
            for type_line in ("type: Agent\n", ""):
                with self.subTest(type_line=type_line):
                    card.write_text("---\n" + type_line + "title: controle\nroles: [auditor\n---\n",
                                    encoding="utf-8")
                    loaded = canon_mod.from_config(cfg)
                    self.assertTrue(loaded.readable)
                    self.assertFalse(loaded.scope_unread)
                    self.assertIsNone(loaded.agent("controle"))
                    self.assertIn("frontmatter-invalid", {f.code for f in loaded.load_findings})
                    with self.assertRaises(canon_mod.CanonError):
                        auditor.load_all(cfg)
                    self.assertEqual(self.take(lot, "controle", cfg=cfg), [])

    def test_r1_real_card_path_is_protected_without_matching_agent_name(self):
        loaded = canon([agent("controle", path="personas/sentinel-safety.md", roles=["auditor"])])
        lot = {"id": 1, "type": "bug", "title": "Bug", "state": "intake",
               "created_ts": 100, "lot_team": "ameesh"}
        self.assertEqual(len(self.take(lot, "controle", canons=[loaded])), 1)
        for value in ("personas/sentinel-safety.md", "owner:personas/sentinel-safety.md@efgh"):
            self.assertEqual(self.take(dict(lot, body=value), "controle", canons=[loaded]), [])
            self.assertEqual(len(self.take(dict(lot, body=value), "dev", canons=[loaded])), 1)

    def test_r1_hard_ceiling_for_exempt_turns(self):
        with mock.patch("ameesh.platform.audit_guard.shutil.which", return_value="/bin/systemd-run"):
            argv = audit_guard.command(["harness", "argument with spaces"], 30)
        self.assertIn("--property=MemoryMax=%d" % audit_guard.MEMORY_MAX, argv)
        self.assertIn("--property=MemorySwapMax=0", argv)
        self.assertIn("--property=RuntimeMaxSec=30", argv)
        self.assertIn("--property=KillMode=control-group", argv)
        self.assertEqual(argv[-2:], ["harness", "argument with spaces"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cgroup"
            scope = root / "test.scope"
            scope.mkdir(parents=True)
            proc = Path(tmp) / "proc"
            proc.write_text("0::/test.scope\n")
            (scope / "memory.swap.max").write_text("0")
            for maximum, valid in ((str(audit_guard.MEMORY_MAX), True),
                                   (str(audit_guard.MEMORY_MAX * 2), False), ("max", False)):
                (scope / "memory.max").write_text(maximum)
                self.assertEqual(audit_guard.memory_enforced(audit_guard.MEMORY_MAX,
                                                            proc=proc, root=root), valid)
        with mock.patch.object(audit_guard, "memory_enforced", return_value=False), \
                mock.patch.object(audit_guard.os, "execvpe") as execute, \
                mock.patch("builtins.print"):
            self.assertEqual(audit_guard.main(["100", "--", "harness"]), 125)
            execute.assert_not_called()

    @unittest.skipUnless(hasattr(os, "killpg"), "POSIX groups")
    def test_hard_timer_stops_silent_process_without_reading_output(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                                start_new_session=True, stdout=subprocess.PIPE)
        def cleanup():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=3)
        self.addCleanup(cleanup)
        def stop(reason, **kwargs):
            self.assertTrue(kwargs["hard"])
            os.killpg(proc.pid, signal.SIGKILL)
        worker = types.SimpleNamespace(stop_group_now=stop)
        AgentWorker._audit_deadline_monitor(worker, threading.Event(), time.monotonic() + 0.05)
        self.assertNotEqual(proc.wait(timeout=3), 0)
        self.assertIn("hard cap", worker._audit_guard_error)

    def test_no_exemption_without_scope_and_timer_cancellation(self):
        with mock.patch.object(audit_guard, "available", return_value=False):
            self.assertTrue(self.held("auditeur", blocked=True, critical=True, breaches=[]))
        worker = types.SimpleNamespace(stop_group_now=mock.Mock())
        done = threading.Event()
        done.set()
        AgentWorker._audit_deadline_monitor(worker, done, time.monotonic())
        worker.stop_group_now.assert_not_called()
