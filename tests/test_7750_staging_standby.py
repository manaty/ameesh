# SPDX-License-Identifier: AGPL-3.0-only
"""#7750 : un candidat de staging qui ne passe jamais complète son venv.

L'exécutant ne mettait son venv et son canon à jour qu'après une passe
`completed`. Un candidat qui ne détient jamais le rôle n'en fait aucune : il
restait sur la version posée par l'opérateur, et une élection aurait servi un
ancien lecteur du canon. Chaque tick de candidat suit maintenant la dernière
passe complétée de sa clé (avance seulement, CI de la forge, voie scellée),
et l'alerte lit aussi le tick d'un candidat qui n'a jamais complété.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest import mock

from ameesh import canon as canon_mod
from ameesh import release_cutoff, staging_executor, staging_watch

SHA = "1" * 40
OLD = "2" * 7
NOW = 1_800_000_000.0


class DeployedCommitTest(unittest.TestCase):
    def test_the_newest_completed_record_names_the_deployed_commit(self):
        events = mock.Mock()
        events.recent_type.return_value = [
            dict(id=4, data=dict(state="pending", sha="c" * 40)),
            dict(id=3, data=dict(state="completed", sha="b" * 40)),
            dict(id=2, data=dict(state="completed", sha="a" * 40)),
        ]
        with mock.patch.object(staging_executor.storage, "of",
                               return_value=mock.Mock(events=events)):
            self.assertEqual(staging_executor.deployed_commit(object(), "mesh"),
                             "b" * 40)
        self.assertEqual(events.recent_type.call_args.kwargs["object"],
                         "staging-request:mesh")

    def test_no_completed_record_means_no_target(self):
        events = mock.Mock()
        events.recent_type.return_value = [
            dict(id=2, data=dict(state="failed", sha="a" * 40)),
            dict(id=1, data=dict(state="running", sha="b" * 40)),
        ]
        with mock.patch.object(staging_executor.storage, "of",
                               return_value=mock.Mock(events=events)):
            self.assertEqual(staging_executor.deployed_commit(object(), "mesh"), "")


class PrepareStandbyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="7750-standby-")
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.work = os.path.join(self.root, "work")
        self.share = os.path.join(self.root, "share")
        os.makedirs(self.work)
        os.makedirs(self.share)
        os.symlink("venv-" + OLD,
                   os.path.join(self.share, staging_executor.BASCULE_CURRENT))
        self.options = dict(
            key="mesh", role="mesh-design", repo=os.path.join(self.root, "repo"),
            github="o/r", candidates=["h1", "h2"],
            venv_update=dict(venv=os.path.join(self.share, "venv"),
                             package="/pkg", work=self.work,
                             python="/usr/bin/python3"))

    def prepare(self, *, target=SHA, version=None, venv_result=None,
                canon_result=None, options=None):
        version = version or {"running": OLD, "live": OLD}
        venv_result = venv_result or {"state": "installed", "from": OLD,
                                      "to": target[:7]}
        canon_result = canon_result or {"state": "current"}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                staging_executor, "deployed_commit", return_value=target))
            stack.enter_context(mock.patch.object(
                staging_executor, "executor_venv", return_value=version))
            prep = stack.enter_context(mock.patch.object(
                staging_executor, "_prepare_standby_venv",
                return_value=venv_result))
            canon = stack.enter_context(mock.patch.object(
                staging_executor, "refresh_canon", return_value=canon_result))
            evidence = staging_executor.prepare_standby(
                None, types.SimpleNamespace(host="h2"), options or self.options)
        return evidence, prep, canon

    def test_no_completed_deployment_prepares_nothing(self):
        evidence, prep, canon = self.prepare(target="")
        self.assertEqual(evidence, {})
        prep.assert_not_called()
        canon.assert_not_called()

    def test_current_candidate_refreshes_its_canon(self):
        evidence, prep, canon = self.prepare(
            target=OLD + "0" * 33,
            version={"running": OLD, "live": OLD},
            canon_result={"state": "installed", "from": "a" * 7, "to": "b" * 7})
        self.assertEqual(evidence, {"canon": {"state": "installed",
                                              "from": "a" * 7, "to": "b" * 7}})
        prep.assert_not_called()
        canon.assert_called_once()

    def test_a_just_moved_link_is_not_judged_by_the_old_process(self):
        evidence, prep, canon = self.prepare(
            target=OLD + "0" * 33,
            version={"running": "9999999", "live": OLD})
        self.assertEqual(evidence, {})
        prep.assert_not_called()
        canon.assert_not_called()

    def test_a_standby_behind_the_deployment_prepares_its_venv(self):
        evidence, prep, canon = self.prepare(target=SHA, version={
            "running": OLD, "live": OLD})
        self.assertEqual(evidence["venv"]["state"], "installed")
        prep.assert_called_once()
        # The process still runs the previous venv: the canon waits for the
        # next tick, which starts the new one.
        canon.assert_not_called()
        state = json.loads((Path(self.work) / staging_executor.STANDBY_STATE)
                           .read_text())
        self.assertEqual(state["venv"]["sha"], SHA)
        self.assertEqual(state["venv"]["state"], "installed")

    def test_a_refusal_is_retried_after_the_standby_delay_not_every_tick(self):
        refused = {"state": "refused", "step": "manifest",
                   "reason": "wheel missing"}
        evidence, prep, canon = self.prepare(target=SHA, venv_result=refused)
        self.assertEqual(evidence["venv"], refused)
        self.assertEqual(prep.call_count, 1)
        evidence, prep, canon = self.prepare(target=SHA, venv_result=refused)
        self.assertEqual(evidence, {})
        self.assertEqual(prep.call_count, 0)

    def test_unconfigured_venv_is_named_and_does_not_move_the_canon(self):
        options = dict(self.options)
        options.pop("venv_update")
        evidence, prep, canon = self.prepare(options=options)
        self.assertEqual(evidence, {"venv": {"state": "unconfigured",
                                             "to": SHA[:7]}})
        prep.assert_not_called()
        canon.assert_not_called()


class PrepareVenvTest(unittest.TestCase):
    def options(self):
        return dict(repo="/repo", github="o/r", venv_update=dict(
            venv="/share/venv", package="/pkg", work="/work",
            python="/usr/bin/python3"))

    def prepare(self, *, ancestors, fetch=None, forge=None, refresh=None,
                worktree=("/tmp/folder", "/tmp/tree")):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                staging_executor, "executor_venv",
                return_value={"running": "9999999", "live": OLD}))
            ancestor = stack.enter_context(mock.patch.object(
                staging_executor, "_git_ancestor", side_effect=ancestors))
            fetch_mock = stack.enter_context(mock.patch.object(
                staging_executor, "_fetch_origin", return_value=fetch))
            forge_mock = stack.enter_context(mock.patch.object(
                staging_executor, "forge_ci",
                return_value=forge or (None, "CI green")))
            tree = stack.enter_context(mock.patch.object(
                release_cutoff, "_worktree", return_value=worktree))
            drop = stack.enter_context(mock.patch.object(
                release_cutoff, "_drop_worktree"))
            refresh_mock = stack.enter_context(mock.patch.object(
                staging_executor, "refresh_venv",
                return_value=refresh or {"state": "installed", "to": SHA[:7]}))
            result = staging_executor._prepare_standby_venv(
                self.options(), SHA)
        return result, ancestor, fetch_mock, forge_mock, tree, drop, refresh_mock

    def test_a_venv_ahead_of_the_deployment_is_left_alone(self):
        result, ancestor, fetch, forge, tree, drop, refresh = self.prepare(
            ancestors=[True])
        self.assertEqual(result["state"], "ahead")
        fetch.assert_not_called()
        refresh.assert_not_called()

    def test_fetch_failure_is_a_named_refusal(self):
        result, ancestor, fetch, forge, tree, drop, refresh = self.prepare(
            ancestors=[False], fetch="fetch: network down")
        self.assertEqual((result["state"], result["step"]), ("refused", "fetch"))
        self.assertIn("network down", result["reason"])
        refresh.assert_not_called()

    def test_a_diverged_venv_is_refused_before_the_forge_or_a_worktree(self):
        result, ancestor, fetch, forge, tree, drop, refresh = self.prepare(
            ancestors=[False, False, False])
        self.assertEqual(result["reason"], "not a fast-forward from venv-" + OLD)
        forge.assert_not_called()
        tree.assert_not_called()
        refresh.assert_not_called()

    def test_forge_red_is_refused_before_any_build(self):
        result, ancestor, fetch, forge, tree, drop, refresh = self.prepare(
            ancestors=[False, False, True],
            forge=("forge CI: red tests", ""))
        self.assertEqual((result["state"], result["step"]), ("refused", "forge"))
        tree.assert_not_called()
        refresh.assert_not_called()

    def test_the_sealed_route_runs_in_a_temporary_worktree(self):
        result, ancestor, fetch, forge, tree, drop, refresh = self.prepare(
            ancestors=[False, False, True])
        self.assertEqual(result["state"], "installed")
        tree.assert_called_once()
        self.assertEqual(tree.call_args.kwargs["prefix"],
                         staging_executor.STANDBY_WORKTREE_PREFIX)
        refresh.assert_called_once()
        drop.assert_called_once_with("/repo", "/tmp/folder", "/tmp/tree")


class ReadinessEventTest(unittest.TestCase):
    def call(self, events, *, refresh=None, previous=None):
        events.recent_type.return_value = previous or []
        cfg = types.SimpleNamespace(host="h2")
        options = dict(key="mesh", role="mesh-design", candidates=["h1", "h2"])
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                staging_executor.storage, "of",
                return_value=mock.Mock(events=events)))
            stack.enter_context(mock.patch.object(
                staging_executor, "executor_venv",
                return_value={"running": OLD, "live": OLD}))
            stack.enter_context(mock.patch.object(
                staging_executor, "canon_commit", return_value="c" * 40))
            staging_executor.publish_sequence_readiness(
                object(), cfg, options, None, {}, refresh=refresh)
        return events

    def test_a_standby_preparation_is_published_with_the_tick(self):
        events = mock.Mock()
        self.call(events, refresh={"venv": {"state": "refused",
                                            "step": "forge",
                                            "reason": "forge CI: red"},
                                   "canon": {"state": "pinned",
                                             "ref": "b" * 40}})
        payload = events.record.call_args.args[3]
        self.assertEqual(payload["venv_refresh"]["state"], "refused")
        self.assertEqual(payload["canon_refresh"]["state"], "pinned")
        self.assertEqual(payload["canon_ref"], "c" * 40)

    def test_an_unchanged_fresh_tick_is_not_republished(self):
        events = mock.Mock()
        self.call(events)
        payload = events.record.call_args.args[3]
        events.record.reset_mock()
        self.call(events, previous=[dict(id=9, ts=time.time(), data=payload)])
        events.record.assert_not_called()

    def test_a_new_refusal_is_published_even_on_a_fresh_tick(self):
        events = mock.Mock()
        self.call(events)
        payload = events.record.call_args.args[3]
        events.record.reset_mock()
        self.call(events, previous=[dict(id=9, ts=time.time(), data=payload)],
                  refresh={"venv": {"state": "refused", "step": "manifest",
                                    "reason": "wheel missing"}})
        published = events.record.call_args.args[3]
        self.assertEqual(published["venv_refresh"]["step"], "manifest")


class TickOnlyAlertTest(unittest.TestCase):
    RECORD = ("mesh", dict(id=5, data=dict(
        state="completed", sha="a" * 40, host="h1", notify="mesh-design",
        executor_venv=dict(running="aaaaaaa", live="aaaaaaa"))))

    def tick(self, *, host="h2", live=OLD, candidates=None, **data):
        return {("mesh", host): dict(id=6, ts=NOW, data=dict(
            key="mesh", host=host, role="mesh-design",
            candidates=candidates or ["h1", "h2"],
            executor_venv=dict(running=live, live=live), **data))}

    def findings(self, ticks):
        return staging_watch.executor_version_findings(
            [self.RECORD], now=NOW, ticks=ticks)

    def test_a_candidate_without_a_completed_pass_is_named(self):
        alert, = self.findings(self.tick())
        self.assertEqual((alert["type"], alert["reason"], alert["host"]),
                         ("staging_executor_stale", "mesh", "h2"))
        self.assertEqual(alert["executor_venv"], OLD)
        self.assertIn("no completed pass on this candidate", alert["detail"])
        self.assertIn(OLD, alert["detail"])
        self.assertEqual(alert["orchestrators"], ["mesh-design"])

    def test_a_current_or_ahead_candidate_is_silent(self):
        self.assertEqual(self.findings(self.tick(live="aaaaaaa")), [])
        self.assertEqual(self.findings(self.tick(
            venv_refresh=dict(state="ahead"))), [])

    def test_a_stale_tick_outside_the_candidates_is_silent(self):
        ticks = self.tick()
        ticks[("mesh", "h2")]["ts"] = NOW - staging_watch.EXECUTOR_TICK_MAX_S - 1
        self.assertEqual(self.findings(ticks), [])
        self.assertEqual(self.findings(self.tick(candidates=["h1"])), [])

    def test_an_unreported_version_on_a_tick_only_host_is_silent(self):
        ticks = self.tick()
        ticks[("mesh", "h2")]["data"]["executor_venv"] = {}
        self.assertEqual(self.findings(ticks), [])

    def test_the_refusal_of_the_standby_preparation_names_the_cause(self):
        alert, = self.findings(self.tick(venv_refresh=dict(
            state="refused", step="forge", reason="forge CI: red tests")))
        self.assertIn("refused at forge: forge CI: red tests", alert["detail"])


class CanonHostTest(unittest.TestCase):
    def test_the_canon_alert_names_the_candidate_host(self):
        alert, = staging_watch.canon_drift_findings(
            [("mesh", dict(canon_ref="a" * 40, host="h2"))], head="b" * 40,
            first_ahead=lambda ref, head: NOW - 2 * 86400,
            now=NOW)
        self.assertEqual(alert["host"], "h2")
        self.assertIn("mesh on h2", alert["detail"])

    def test_a_tick_carrying_a_canon_is_read_when_no_pass_did(self):
        tick = dict(id=7, ts=NOW, data=dict(
            host="h2", key="mesh", role="mesh-design",
            candidates=["h2"], canon_ref="b" * 40,
            canon_refresh=dict(state="pinned", ref="b" * 40)))
        cfg = types.SimpleNamespace(canon="/canon", canon_ref="")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                staging_watch, "executor_canons", return_value=[]))
            stack.enter_context(mock.patch.object(
                staging_watch, "executor_ticks",
                return_value={("mesh", "h2"): tick}))
            stack.enter_context(mock.patch.object(
                canon_mod, "git_toplevel", return_value="/repo"))
            stack.enter_context(mock.patch.object(
                canon_mod, "git_commit", return_value="c" * 40))
            findings = stack.enter_context(mock.patch.object(
                staging_watch, "canon_drift_findings", return_value=[]))
            staging_watch.canon_alerts(object(), now=NOW, cfg=cfg)
        records = findings.call_args.args[0]
        self.assertEqual(records, [("mesh", dict(
            tick["data"], host="h2", notify="mesh-design"))])


if __name__ == "__main__":
    unittest.main()
