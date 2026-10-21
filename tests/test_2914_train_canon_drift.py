# SPDX-License-Identifier: AGPL-3.0-only
"""#2914 : le canon épinglé du train comparé au canon que lit la porte.

À chaque passage : en ombre, l'écart est inscrit au plan et le pilote alerté ;
en actif, le train refuse nommément de composer un nouveau paquet, un paquet
en vol va au bout. L'épingle ne suit jamais la porte d'elle-même (#1467).
"""
from __future__ import annotations

import contextlib
import tempfile
import types
import unittest
from unittest import mock

from ameesh import mechanics, review, train, train_canon, train_runner, train_shadow
from ameesh import review_designation_activation as rda

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
    """The gate's commit is the designation's own source selection (cx3, B/P1)."""

    def setUp(self):
        self.cfg = types.SimpleNamespace(canon="/canon-gate", host="h")
        self.audited = {}
        self.applicable = set()

    def canon(self, repo, audited, applicable=True, prefix=""):
        source = types.SimpleNamespace(mode="git", repo=repo, commit="c" * 40,
                                       prefix=prefix)
        self.audited[repo] = audited
        if applicable:
            self.applicable.add(repo)
        return types.SimpleNamespace(root=repo, sources=[source])

    def load(self, repo, commit, project):
        if repo not in self.applicable:
            raise mechanics.MechanicsError("project %r not declared" % project)
        return types.SimpleNamespace(
            reviews_for=lambda paths: types.SimpleNamespace(reviews=1, risk="light"),
            review_policy=None, rules=(), families=("claude", "codex"),
            source="%s@%s" % (repo, commit))

    def patched(self, canons):
        def audited(db, cfg, canon, source):
            value = self.audited[source.repo]
            if isinstance(value, Exception):
                raise value
            return value
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch("ameesh.canon.load_configured",
                                       return_value=canons))
        stack.enter_context(mock.patch("ameesh.canon_sync.audited_commit",
                                       side_effect=audited))
        stack.enter_context(mock.patch.object(mechanics, "load", side_effect=self.load))
        stack.enter_context(mock.patch.object(review, "classify",
                                              return_value={"class": "light"}))
        return stack

    def gate(self, canons):
        with self.patched(canons):
            return train_canon.gate_commit(object(), self.cfg, "ameesh")

    def designation(self, canons):
        with self.patched(canons):
            return rda._policy(self.cfg, object(), "ameesh", ["src/x.py"])

    def test_cx3_probe_two_applicable_canons_at_different_commits_are_ambiguous(self):
        canons = [self.canon("/canon-gate", GATE), self.canon("/canon-train", PIN)]
        self.assertEqual(self.designation(canons).commit, GATE)
        commit, why = self.gate(canons)
        self.assertEqual(commit, "")
        self.assertIn("ambiguous", why)
        watch = train_canon.Watch("ameesh", PIN, lambda db: self.gate(canons))
        self.assertEqual(watch.read(None).state, train_canon.UNKNOWN)

    def test_a_canon_that_does_not_apply_is_skipped_like_the_designation(self):
        canons = [self.canon("/canon-gate", GATE, applicable=False),
                  self.canon("/canon-train", PIN)]
        self.assertEqual(self.designation(canons).commit, PIN)
        self.assertEqual(self.gate(canons), (PIN, ""))

    def test_applicable_canons_at_one_commit_give_it(self):
        canons = [self.canon("/canon-gate", GATE.upper()), self.canon("/canon-b", GATE)]
        self.assertEqual(self.designation(canons).commit.lower(), GATE)
        self.assertEqual(self.gate(canons), (GATE, ""))

    def test_no_canon_or_no_applicable_canon_is_named(self):
        self.cfg.canon = ""
        commit, why = self.gate([])
        self.assertEqual(commit, "")
        self.assertIn("no canon configured", why)
        self.cfg.canon = "/canon-gate"
        canons = [self.canon("/canon-gate", RuntimeError("pg down")),
                  self.canon("/canon-sub", GATE, prefix="canon/"),
                  self.canon("/canon-train", PIN, applicable=False)]
        commit, why = self.gate(canons)
        self.assertEqual(commit, "")
        self.assertIn("pg down", why)
        self.assertIn("sub-directory", why)
        self.assertIn("not declared", why)


class GateRulesGapTest(unittest.TestCase):
    """qa-01: the gate's floor/flags are the strictest over ALL canons."""

    def setUp(self):
        self.cfg = types.SimpleNamespace(canon="/canon-gate", host="h")
        self.flags = {}

    @staticmethod
    def canon(repo, commit):
        source = types.SimpleNamespace(mode="git", repo=repo, commit=commit, prefix="")
        return types.SimpleNamespace(root=repo, sources=[source])

    def gap(self, canons, pin_repo="/canon-train", pin=PIN):
        def light(repo, commit):
            value = self.flags[(repo, commit)]
            if isinstance(value, Exception):
                raise value
            return value[0]

        def flags(repo, commit):
            value = self.flags[(repo, commit)]
            if isinstance(value, Exception):
                raise value
            return value[1:]
        with mock.patch("ameesh.canon.load_configured", return_value=canons), \
                mock.patch.object(mechanics, "light_reviews_at", side_effect=light), \
                mock.patch.object(mechanics, "review_flags_at", side_effect=flags):
            return train_canon.gate_rules_gap(self.cfg, pin_repo, pin)

    def test_equal_rules_are_no_gap(self):
        self.flags = {("/canon-train", PIN): (1, True, True)}
        self.assertEqual(self.gap([self.canon("/canon-train", PIN)]), "")

    def test_a_non_applicable_canon_raising_the_floor_is_a_gap(self):
        self.flags = {("/canon-train", PIN): (0, True, True),
                      ("/canon-other", GATE): (1, False, True)}
        gap = self.gap([self.canon("/canon-train", PIN), self.canon("/canon-other", GATE)])
        self.assertIn("light_reviews porte 1 / train 0", gap)
        self.assertNotIn("exclude_author_family", gap)

    def test_unreadable_document_is_a_gap_never_zero(self):
        self.flags = {("/canon-train", PIN): mechanics.MechanicsError("bad yaml")}
        self.assertIn("bad yaml", self.gap([self.canon("/canon-train", PIN)]))

    def test_watch_turns_a_rule_gap_into_a_named_drift(self):
        watch = train_canon.Watch("ameesh", PIN, lambda db: (PIN, ""),
                                  rules=lambda db: "light_reviews porte 1 / train 0")
        reading = watch.read(None)
        self.assertEqual((reading.state, reading.gate), (train_canon.DRIFT, PIN))
        self.assertIn("règles d'avis", reading.describe())
        watch.rules = lambda db: ""
        self.assertTrue(watch.read(None).aligned)

        def broken(db):
            raise OSError("canon unreadable")
        watch.rules = broken
        self.assertEqual(watch.read(None).state, train_canon.UNKNOWN)


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

    def test_no_pilot_known_retries_at_the_next_passage(self):
        self.watch.pilot = lambda db: ""
        reading = self.watch.read(None)
        self.assertEqual(self.watch.alert(None, reading, "shadow"), "")
        self.watch.pilot = lambda db: "mesh-design"
        self.assertEqual(self.watch.alert(None, reading, "shadow"), "mesh-design")
        self.assertEqual(len(self.sent), 1)

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
