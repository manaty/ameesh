# SPDX-License-Identifier: AGPL-3.0-only
"""#3925 (manaty/ameesh#2975) : admission dynamique bornée d'un exécuteur.

`--agents-file` : la liste d'admission côté hôte est relue en marche ; un agent
ajouté est réclamé sans redémarrage, un agent retiré rend son bail après son
tour ; une entrée invalide n'arrête rien ; une liste vide n'admet personne.
"""
from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from ameesh import admission, device_state, registry
from ameesh import runner as runner_mod
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


class ParseTest(unittest.TestCase):
    def test_comments_commas_duplicates_and_invalid_entries(self):
        parsed = admission.parse(
            "# agents IMA\nalpha\nbeta, gamma  # rôle\n\nalpha\nall\nbad name\n-x\n")
        self.assertEqual(parsed.names, ["alpha", "beta", "gamma"])
        self.assertEqual([(n, e) for n, e, _ in parsed.problems],
                         [(6, "all"), (7, "bad name"), (8, "-x")])

    def test_stamp_of_a_missing_file_is_none(self):
        self.assertIsNone(admission.stamp("/nonexistent/admission/list"))


class RefreshTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("alpha", "beta", "gamma"):
            registry.upsert(self.db, name, host=self.cfg.host)
        self.path = os.path.join(self.tmp, "admission.txt")
        self.logs: list[str] = []
        patcher = mock.patch.object(runner_mod, "log_async", side_effect=self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)
        # une écriture dans la même nanoseconde garde l'empreinte : on la force
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def runner(self, agents=None):
        return Runner(self.cfg, self.db, agents=agents, agents_file=self.path, once=True)

    def claimable(self, runner):
        if runner.admits_nobody:
            return []
        return sorted(r["name"] for r in
                      registry.claimable(self.db, self.cfg.host, runner.agents_filter))

    def test_added_agent_is_claimable_without_restart_and_list_is_mutated_in_place(self):
        self.write("alpha\n")
        runner = self.runner()
        same = runner.agents_filter
        self.assertEqual(self.claimable(runner), ["alpha"])
        self.write("alpha\nbeta\n")
        self.assertTrue(runner.refresh_admission())
        self.assertIs(runner.agents_filter, same)
        self.assertEqual(self.claimable(runner), ["alpha", "beta"])
        self.assertFalse(runner.refresh_admission())  # rien de changé
        self.assertTrue(any("admitted beta" in line for line in self.logs))

    def test_static_agents_stay_admitted_with_the_file(self):
        self.write("beta\n")
        runner = self.runner(agents=["gamma"])
        self.assertEqual(self.claimable(runner), ["beta", "gamma"])
        self.write("")
        runner.refresh_admission()
        self.assertEqual(self.claimable(runner), ["gamma"])

    def test_empty_or_missing_file_admits_nobody_never_everyone(self):
        runner = self.runner()  # fichier absent au démarrage
        self.assertEqual(runner.agents_filter, [])
        self.assertTrue(runner.admits_nobody)
        with mock.patch.object(runner_mod.registry, "claimable") as claimable:
            runner._note_gate_wait("porte")
        claimable.assert_not_called()
        self.assertEqual(runner.served_rows(self.db), [])

    def test_vanished_or_invalid_file_keeps_the_last_list(self):
        self.write("alpha\nbeta\n")
        runner = self.runner()
        os.unlink(self.path)
        self.assertFalse(runner.refresh_admission())
        self.assertEqual(runner.agents_filter, ["alpha", "beta"])
        with open(self.path, "wb") as fh:
            fh.write(b"\xff\xfe\x00bad")
        self.assertFalse(runner.refresh_admission())
        self.assertEqual(runner.agents_filter, ["alpha", "beta"])
        self.write("alpha\nnot valid\nbeta\n")
        runner.refresh_admission()
        self.assertEqual(runner.agents_filter, ["alpha", "beta"])
        self.assertTrue(any("'not valid' skipped" in line for line in self.logs))

    def test_withdrawn_agent_worker_is_woken(self):
        self.write("alpha\n")
        runner = self.runner()
        worker = mock.Mock(wake=threading.Event())
        runner.workers["alpha"] = worker
        self.write("")
        runner.refresh_admission()
        self.assertTrue(worker.wake.is_set())
        self.assertTrue(any("withdrawn after their current turn alpha" in line
                            for line in self.logs))

    def test_control_channel_follows_the_list_and_empty_admits_none(self):
        channel = device_state.ControlChannel.__new__(device_state.ControlChannel)
        channel._server = mock.Mock()
        channel.set_allowed([])
        self.assertEqual(channel._server.allowed, frozenset())
        self.write("alpha\n")
        runner = self.runner()
        runner._control_channel = channel
        self.write("alpha\nbeta\n")
        runner.refresh_admission()
        self.assertEqual(channel.allowed, frozenset({"alpha", "beta"}))


class WithdrawalTest(PgTestCase):
    def _cwd(self, name):
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        return cwd

    def _worker(self, name, runner):
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def test_removed_from_the_list_releases_the_lease_between_turns(self):
        self.register("doc-1", "claude", cwd=self._cwd("doc-1"))
        runner = Runner(self.cfg, self.db, agents=["doc-1"], once=True)
        worker = self._worker("doc-1", runner)
        self.assertEqual(worker.admission_withdrawn(), "")
        runner.agents_filter[:] = []
        self.assertIn("admission list", worker.admission_withdrawn())
        with mock.patch.object(runner_mod, "log"), \
                mock.patch.object(worker, "run_turn") as turn:
            worker.run()
        turn.assert_not_called()
        self.assertIsNone(registry.get(self.db, "doc-1").get("lease_owner"))

    def test_canon_placement_withdrawn_releases_the_lease(self):
        self.register("doc-2", "claude", cwd=self._cwd("doc-2"))
        runner = Runner(self.cfg, self.db, once=True)
        worker = self._worker("doc-2", runner)
        self.assertEqual(worker.admission_withdrawn(), "")  # hors canon : admis
        self.db.execute("UPDATE agent_registry SET canon_ref = 'agents/doc-2.md', "
                        "placement_ok = false, placement_diagnostic = 'placement retiré' "
                        "WHERE name = 'doc-2'")
        self.assertIn("placement retiré", worker.admission_withdrawn())
        self.db.execute("UPDATE agent_registry SET releve = 'election' WHERE name = 'doc-2'")
        self.assertEqual(worker.admission_withdrawn(), "")  # relève élue : EP2


if __name__ == "__main__":
    unittest.main()
