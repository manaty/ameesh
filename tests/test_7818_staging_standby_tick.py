# SPDX-License-Identifier: AGPL-3.0-only
"""#7818 : le tick d'un candidat sans bail prépare son venv, l'alerte attend.

L'implémentation du rattrapage vient de #7750. Ce lot fige le branchement au
niveau du tick — un candidat qui ne détient pas le rôle appelle
`prepare_standby` et publie son `venv_refresh` dans le même tick — et empêche
`staging_executor_stale` de parler d'un tick antérieur à la passe `completed`
qui a nommé le déploiement : ce tick ne pouvait pas encore le lire, seul le
suivant le peut.
"""
from __future__ import annotations

import contextlib
import types
import unittest
from unittest import mock

from ameesh import staging_executor, staging_watch

SHA = "a" * 40
OLD = "b" * 7
NOW = 1_800_000_000.0


class TickStandbyTest(unittest.TestCase):
    """Le tick, pas la fonction : le candidat sans bail se prépare et publie."""

    def setUp(self):
        self.db = object()
        self.cfg = types.SimpleNamespace(host="h2")
        self.options = dict(key="mesh-staging", role="mesh-design",
                            candidates=["h1", "h2"], repo="/repo",
                            bascule_env="/bascule.env")
        self.refresh = {"venv": {"state": "installed", "from": OLD,
                                 "to": SHA[:7]}}

    def call(self, *, live, candidates=None, prepare=None):
        options = dict(self.options)
        if candidates is not None:
            options["candidates"] = candidates
        prepare = prepare or mock.Mock(return_value=self.refresh)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                staging_executor.role_run, "live", return_value=live))
            prepare = stack.enter_context(mock.patch.object(
                staging_executor, "prepare_standby", prepare))
            stack.enter_context(mock.patch(
                "ameesh.platform.child_environment.execution_transport",
                return_value={}))
            stack.enter_context(mock.patch.object(
                staging_executor, "sequence_config", return_value={}))
            stack.enter_context(mock.patch.object(
                staging_executor, "sequence_problem", return_value=None))
            publish = stack.enter_context(mock.patch.object(
                staging_executor, "publish_sequence_readiness"))
            result = staging_executor.tick(self.db, self.cfg, options)
        return result, prepare, publish

    def test_a_standby_tick_prepares_and_publishes_the_refresh(self):
        result, prepare, publish = self.call(live={"held": True,
                                                   "holder": "h1"})
        self.assertEqual(result, {"state": "standby",
                                  "reason": "another role holder"})
        prepare.assert_called_once_with(self.db, self.cfg, self.options)
        # The evidence of the preparation rides the tick's readiness event.
        self.assertEqual(publish.call_args.kwargs["refresh"], self.refresh)

    def test_the_role_holder_does_not_prepare_before_its_pass(self):
        lease = mock.Mock()
        lease.acquire.return_value = None
        with mock.patch.object(staging_executor, "ExecutionLease",
                               return_value=lease):
            result, prepare, publish = self.call(
                live={"held": True, "holder": "h2"})
        self.assertEqual(result["reason"], "execution still leased")
        prepare.assert_not_called()
        self.assertEqual(publish.call_args.kwargs["refresh"], {})

    def test_a_host_outside_the_candidates_does_not_prepare(self):
        result, prepare, publish = self.call(live={"held": True,
                                                   "holder": "h1"},
                                             candidates=["h1"])
        self.assertEqual(result["reason"], "not a candidate")
        prepare.assert_not_called()
        publish.assert_not_called()


class AlertGraceTest(unittest.TestCase):
    """Un tick antérieur à la passe complétée ne peut pas nommer un retard."""

    def record(self, sha=SHA, *, id=10):
        return ("mesh-staging", dict(
            id=id, data=dict(state="completed", sha=sha, host="h1",
                             notify="mesh-design",
                             executor_venv=dict(running=sha[:7],
                                                live=sha[:7]))))

    def tick(self, *, id=11, live=OLD, **data):
        return {("mesh-staging", "h2"): dict(
            id=id, ts=NOW, data=dict(
                key="mesh-staging", host="h2", role="mesh-design",
                candidates=["h1", "h2"],
                executor_venv=dict(running=live, live=live), **data))}

    def findings(self, ticks, *, record=None):
        return staging_watch.executor_version_findings(
            [record or self.record()], now=NOW, ticks=ticks)

    def test_a_tick_before_the_completed_pass_is_silent(self):
        self.assertEqual(self.findings(self.tick(id=10)), [])
        self.assertEqual(self.findings(self.tick(id=9)), [])

    def test_the_first_tick_after_the_pass_speaks_again(self):
        alert, = self.findings(self.tick(id=11))
        self.assertEqual((alert["type"], alert["host"]),
                         ("staging_executor_stale", "h2"))
        self.assertIn(OLD, alert["detail"])

    def test_a_refused_preparation_after_the_pass_still_speaks(self):
        alert, = self.findings(self.tick(
            id=11, venv_refresh=dict(state="refused", step="forge",
                                     reason="forge CI: red tests")))
        self.assertIn("refused at forge", alert["detail"])

    def test_a_tick_without_an_id_still_speaks(self):
        alert, = self.findings(self.tick(id=0))
        self.assertEqual(alert["host"], "h2")


if __name__ == "__main__":
    unittest.main()
