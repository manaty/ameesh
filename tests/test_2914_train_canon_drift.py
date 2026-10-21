# SPDX-License-Identifier: AGPL-3.0-only
"""#2914 : le canon épinglé du train comparé au canon que lit la porte.

À chaque passage : en ombre, l'écart est inscrit au plan et le pilote alerté ;
en actif, le train refuse nommément de composer un nouveau paquet, un paquet
en vol va au bout. L'épingle ne suit jamais la porte d'elle-même (#1467).
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from unittest import mock

from ameesh import train, train_canon, train_runner, train_shadow

PIN = "a" * 40
GATE = "b" * 40


class CompareTest(unittest.TestCase):
    def test_three_outcomes(self):
        self.assertEqual(train_canon.compare(PIN, PIN.upper()).state, train_canon.ALIGNED)
        drift = train_canon.compare(PIN, GATE)
        self.assertEqual(drift.state, train_canon.DRIFT)
        self.assertIn(PIN[:12], drift.describe())
        self.assertIn(GATE[:12], drift.describe())
        unknown = train_canon.compare(PIN, "", "état illisible")
        self.assertEqual(unknown.state, train_canon.UNKNOWN)
        self.assertIn("état illisible", unknown.describe())


class GateCommitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.here = os.path.join(self.tmp.name, "canon")
        self.other = os.path.join(self.tmp.name, "other")
        self.cfg = types.SimpleNamespace(canon=self.here, host="h")

    @staticmethod
    def canon(repo, commit="c" * 40, prefix=""):
        source = types.SimpleNamespace(mode="git", repo=repo, commit=commit, prefix=prefix)
        return types.SimpleNamespace(root=repo, sources=[source])

    def read(self, canons, audited=lambda db, cfg, canon, source: GATE, path=None):
        with mock.patch("ameesh.canon.load_configured", return_value=canons), \
                mock.patch("ameesh.canon_sync.audited_commit", side_effect=audited):
            return train_canon.gate_commit(object(), self.cfg, path or self.here)

    def test_the_canon_at_the_train_path_gives_the_audited_commit(self):
        seen = []

        def audited(db, cfg, canon, source):
            seen.append(source.repo)
            return GATE.upper()
        self.assertEqual(self.read([self.canon(self.other), self.canon(self.here)],
                                   audited), (GATE, ""))
        self.assertEqual(seen, [self.here])

    def test_a_single_configured_canon_elsewhere_is_the_gate(self):
        self.assertEqual(self.read([self.canon(self.other)]), (GATE, ""))

    def test_several_canons_elsewhere_are_ambiguous(self):
        commit, why = self.read([self.canon(self.other),
                                 self.canon(os.path.join(self.tmp.name, "third"))])
        self.assertEqual(commit, "")
        self.assertIn("ambiguous", why)

    def test_no_canon_configured_or_unreadable_state_is_named(self):
        self.cfg.canon = ""
        self.assertIn("no canon configured", self.read([])[1])
        self.cfg.canon = self.here

        def broken(db, cfg, canon, source):
            raise RuntimeError("pg down")
        commit, why = self.read([self.canon(self.here)], broken)
        self.assertEqual(commit, "")
        self.assertIn("pg down", why)
        commit, why = self.read([self.canon(self.here)],
                                lambda db, cfg, canon, source: "")
        self.assertEqual(commit, "")
        self.assertIn("no exact audited commit", why)

    def test_a_sub_directory_canon_is_not_the_gate(self):
        commit, why = self.read([self.canon(self.here, prefix="canon/")])
        self.assertEqual(commit, "")
        self.assertIn("exact Git commit", why)


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.gate = (GATE, "")
        self.watch = train_canon.Watch(
            "ameesh", PIN, lambda db: self.gate, pilot=lambda db: "mesh-design",
            mailer=lambda db, recipient, body, reading: self.sent.append(
                (recipient, body, reading.state)))

    def test_one_alert_per_distinct_reading(self):
        reading = self.watch.read(None)
        self.assertEqual(reading.state, train_canon.DRIFT)
        self.assertEqual(self.watch.alert(None, reading, "shadow"), "mesh-design")
        self.assertEqual(self.watch.alert(None, self.watch.read(None), "shadow"), "")
        self.assertEqual(len(self.sent), 1)
        recipient, body, state = self.sent[0]
        self.assertIn("train.projects.ameesh.canon_commit", body)
        self.assertIn("#1467", body)
        self.gate = ("c" * 40, "")
        self.watch.alert(None, self.watch.read(None), "shadow")
        self.assertEqual(len(self.sent), 2)

    def test_aligned_sends_nothing_and_reader_failure_is_unknown(self):
        self.gate = (PIN, "")
        self.assertEqual(self.watch.alert(None, self.watch.read(None), "active"), "")
        self.assertEqual(self.sent, [])

        def broken(db):
            raise OSError("no state")
        self.watch.reader = broken
        reading = self.watch.read(None)
        self.assertEqual(reading.state, train_canon.UNKNOWN)
        self.assertIn("no state", reading.why)

    def test_mail_failure_does_not_raise_and_retries(self):
        def failing(db, recipient, body, reading):
            raise RuntimeError("mailbox down")
        self.watch.mailer = failing
        reading = self.watch.read(None)
        self.assertEqual(self.watch.alert(None, reading, "active"), "")
        self.watch.mailer = lambda db, recipient, body, reading: self.sent.append(recipient)
        self.assertEqual(self.watch.alert(None, reading, "active"), "mesh-design")


class _Rules:
    commit = PIN
    project = "ameesh"
    ci_packet = "full"

    @staticmethod
    def next_packet(now):
        return now + 3600

    @staticmethod
    def last_packet(now):
        return now - 60


class ShadowDriftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notices, self.sent = [], []
        self.gate = (GATE, "")
        watch = train_canon.Watch(
            "ameesh", PIN, lambda db: self.gate, pilot=lambda db: "pilot",
            mailer=lambda db, recipient, body, reading: self.sent.append(recipient))
        mechanics = types.SimpleNamespace(load=lambda path, commit, name: _Rules())
        project = types.SimpleNamespace(name="ameesh", repo=self.tmp.name, target="main",
                                        github="o/r")
        self.shadow = train_shadow.Passage(
            types.SimpleNamespace(state_dir=self.tmp.name, host="h", runner="r"),
            project, policy_path=self.tmp.name, policy_commit=PIN, workflows=(),
            provider=None, mechanics=mechanics, repository=self.tmp.name,
            notify=self.notices.append, canon=watch)
        self.shadow.plan = lambda db, rules, slot, now: dict(cutoff=slot, local_stamp="s",
                                                             members=[], deferred=[])
        self.shadow.compare = lambda state, rules: []

    def test_drift_is_written_in_the_plan_and_alerted_once(self):
        result, deadline = self.shadow.scheduled_step(None, 10_000.0)
        self.assertEqual(result["result"], "shadow-plan")
        self.assertEqual(result["plan"]["canon"],
                         dict(pin=PIN, gate=GATE, state="drift", why=""))
        self.assertEqual(self.shadow.store.load()["plans"][-1]["canon"]["state"], "drift")
        self.assertTrue(any("canon épinglé" in notice for notice in self.notices))
        self.assertEqual(self.sent, ["pilot"])
        self.shadow.scheduled_step(None, 10_010.0)
        self.assertEqual(self.sent, ["pilot"])

    def test_aligned_plan_carries_the_reading_without_alert(self):
        self.gate = (PIN, "")
        result, _ = self.shadow.scheduled_step(None, 10_000.0)
        self.assertEqual(result["plan"]["canon"]["state"], "aligned")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.notices, [])


class ActiveDriftTest(unittest.TestCase):
    def setUp(self):
        self.notices, self.sent = [], []
        self.gate = (GATE, "")
        self.state = None
        watch = train_canon.Watch(
            "ameesh", PIN, lambda db: self.gate, pilot=lambda db: "pilot",
            mailer=lambda db, recipient, body, reading: self.sent.append(recipient))
        cfg = types.SimpleNamespace(state_dir="/nonexistent")
        project = types.SimpleNamespace(name="ameesh", repo="/nonexistent", target="main",
                                        github="o/r")
        self.passage = train_runner.Passage(
            cfg, project, owner="runner-a", policy=train.Policy(enabled=True),
            policy_path="/nonexistent", policy_commit=PIN, workflows=(), provider=None,
            mechanics=None, notify=lambda reason, state: self.notices.append(reason),
            canon=watch)
        runtime = types.SimpleNamespace(
            load=lambda: self.state,
            policy=types.SimpleNamespace(schedule=_Rules()))
        patches = (mock.patch.object(train_runner.Passage, "runtime", return_value=runtime),
                   mock.patch.object(train_runner.Passage, "_step",
                                     return_value={"result": "engine"}))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_drift_refuses_a_new_composition_by_name(self):
        for state in (None, dict(phase="landed"), dict(phase="empty"),
                      dict(phase="composing")):
            self.state = state
            result, deadline = self.passage.scheduled_step(None, 1000.0)
            self.assertEqual(result["result"], "canon-drift", state)
            self.assertIn("composition refused", result["reason"])
            self.assertEqual(result["canon"]["gate"], GATE)
            self.assertEqual(deadline, 4600.0)
        self.assertEqual(self.sent, ["pilot"])
        self.assertEqual(self.passage.step(None, 1000.0)["result"], "canon-drift")

    def test_a_packet_in_flight_or_a_paused_train_runs_its_course(self):
        for phase in ("publishing", "ci", "draining", "paused"):
            self.state = dict(phase=phase)
            result, _ = self.passage.scheduled_step(None, 1000.0)
            self.assertEqual(result, {"result": "engine"}, phase)
        self.assertTrue(any("train kept" in notice for notice in self.notices))

    def test_unreadable_gate_canon_refuses_too(self):
        self.gate = ("", "no canon configured for the gate on this host")
        result, _ = self.passage.scheduled_step(None, 1000.0)
        self.assertEqual(result["result"], "canon-drift")
        self.assertIn("illisible", result["reason"])

    def test_aligned_runs_the_engine(self):
        self.gate = (PIN, "")
        result, _ = self.passage.scheduled_step(None, 1000.0)
        self.assertEqual(result, {"result": "engine"})
        self.assertEqual((self.notices, self.sent), ([], []))


if __name__ == "__main__":
    unittest.main()
