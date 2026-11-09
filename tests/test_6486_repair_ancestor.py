# SPDX-License-Identifier: AGPL-3.0-only
"""#6486 : A4 ancestry reaches production through ingest.

The reviewed U7 (#6473) already accepted an ``is_ancestor`` predicate in
``fingerprints.observe_cause``, but ``repair_observation.ingest`` never
supplied one: an old SHA was reported as ``version-differente``. These tests
exercise the bounded git predicate, its injection from ingest on a real
repository, and the configuration path that resolves the project's clone.
"""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import repair_correlation as correlation
from ameesh import repair_observation as obs, repair_signals as signals
from ameesh import repair_triage as triage, work

from .support import PgTestCase

NOW = datetime.datetime(2026, 11, 8, 12, 0,
                        tzinfo=datetime.timezone.utc).timestamp()
HOST = "ameesh-agents-1"


def _git(repo, *args, env):
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True,
        text=True, env=env).stdout.strip()


def _env():
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    })
    return env


def make_repo(parent=None):
    """A real two-commit repository: ``(path, base, active)``."""
    path = tempfile.mkdtemp(prefix="ameesh-ancestor-", dir=parent)
    env = _env()
    _git(path, "init", "-q", env=env)
    with open(os.path.join(path, "seed.txt"), "w", encoding="utf-8") as stream:
        stream.write("base\n")
    _git(path, "add", "seed.txt", env=env)
    _git(path, "commit", "-q", "-m", "base", env=env)
    base = _git(path, "rev-parse", "HEAD", env=env)
    with open(os.path.join(path, "seed.txt"), "w", encoding="utf-8") as stream:
        stream.write("active\n")
    _git(path, "commit", "-q", "-am", "active", env=env)
    active = _git(path, "rev-parse", "HEAD", env=env)
    return path, base, active


class GitAncestorTest(unittest.TestCase):
    def setUp(self):
        self.path, self.base, self.active = make_repo()
        self.addCleanup(shutil.rmtree, self.path, ignore_errors=True)

    def predicate(self):
        predicate = correlation.git_is_ancestor(self.path)
        self.assertIsNotNone(predicate)
        return predicate

    def test_old_commit_and_short_prefix_are_proved(self):
        predicate = self.predicate()
        self.assertIs(predicate(self.base, self.active), True)
        self.assertIs(predicate(self.base[:8], self.active), True)

    def test_direction_is_proved_both_ways(self):
        predicate = self.predicate()
        self.assertIs(predicate(self.base, self.active), True)
        self.assertIs(predicate(self.active, self.base), False)
        # git counts a commit as its own ancestor; correlate checks equality
        # before calling the predicate, so the strict relation is unchanged.
        self.assertIs(predicate(self.active, self.active), True)

    def test_missing_object_is_unproven(self):
        predicate = self.predicate()
        self.assertIsNone(predicate("f" * 40, self.active))
        self.assertIsNone(predicate(self.base, "f" * 40))

    def test_invalid_arguments_are_unproven(self):
        predicate = self.predicate()
        for value in ("", "--help", "not-a-sha", self.base + "x"):
            self.assertIsNone(predicate(value, self.active), value)

    def test_timeout_and_non_repository_are_unproven(self):
        predicate = self.predicate()
        with mock.patch.object(
                correlation.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("git", 1)):
            self.assertIsNone(predicate(self.base, self.active))
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        fallback = correlation.git_is_ancestor(empty)
        self.assertIsNotNone(fallback)
        self.assertIsNone(fallback(self.base, self.active))

    def test_no_clone_means_no_predicate(self):
        self.assertIsNone(
            correlation.git_is_ancestor(os.path.join(self.path, "missing")))
        self.assertIsNone(correlation.git_is_ancestor(""))


class ProjectCheckoutTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mkdtemp(prefix="ameesh-checkout-")
        self.addCleanup(shutil.rmtree, self.path, ignore_errors=True)
        os.makedirs(os.path.join(self.path, ".git"))

    def test_observe_injects_the_declared_release_checkout(self):
        cfg = SimpleNamespace(release={"projects": {
            "ameesh": {"repo": self.path}}})
        with mock.patch.object(obs, "scan", return_value=True) as scan:
            obs.observe(mock.Mock(), project="ameesh", cfg=cfg,
                        source="alerts", now=NOW, log=lambda text: None)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(scan.call_args.kwargs["repo"], self.path)

    def test_radar_declaration_is_the_fallback(self):
        cfg = SimpleNamespace(release=None, radar={"projects": {
            "ameesh": {"repo": self.path}}})
        self.assertEqual(obs.project_checkout(cfg, "ameesh"), self.path)

    def test_absent_configuration_or_checkout_stays_empty(self):
        self.assertEqual(obs.project_checkout(None, "ameesh"), "")
        self.assertEqual(
            obs.project_checkout(SimpleNamespace(release=None, radar=None),
                                 "ameesh"), "")
        self.assertEqual(
            obs.project_checkout(
                SimpleNamespace(release={"projects": {
                    "ameesh": {"repo": os.path.join(self.path, "missing")}}}),
                "ameesh"), "")


class IngestAncestorTest(PgTestCase):
    """The real path: ``ingest`` -> ``observe_cause`` -> ``correlate``."""

    def setUp(self):
        super().setUp()
        for table in ("fingerprint_cause_observations", "repair_signals",
                      "repair_signal_streams", "repair_signal_refusals"):
            self.db.execute("DELETE FROM " + table)
        self.path, self.base, self.active = make_repo(self.tmp)
        self.addCleanup(shutil.rmtree, self.path, ignore_errors=True)

    def signal(self, ident, version):
        return signals.from_inbox(
            {"id": ident}, project="ameesh", environment="staging",
            received_at=NOW, cause_key="command-failed",
            observed_version=version)

    def facts(self, signal):
        return triage.QualificationFacts(
            nature="product_failure", confidence="confirmed",
            evidence_ref=signal.evidence_ref, component="composer",
            reason="timeout", test="test_contract")

    def row(self, ident, version, **extra):
        signal = self.signal(ident, version)
        row = {"key": ident, "entry": {"id": ident},
               "observed_version": version, "cause_key": "command-failed",
               "facts": self.facts(signal)}
        row.update(extra)
        return row

    def ingest(self, rows, expected, next_cursor, repo=""):
        return obs.ingest(
            self.db, project="ameesh", environment="staging", source="inbox",
            rows=rows, expected_cursor=expected, next_cursor=next_cursor,
            now=NOW, repo=repo)

    def active_view(self):
        return {HOST: {"sha": self.active, "absent": False, "at": NOW - 60,
                       "attestation": "actif-%s.json" % self.active[:12]}}

    def closed_cause(self, repo=""):
        lot = work.add(self.db, title="Existing cause", app="ameesh",
                       type="bug", actor="test")["id"]
        self.assertTrue(self.ingest(
            [self.row(1, self.active, outcome="lot",
                      reference="lot:%s" % lot)],
            None, "one", repo=repo))
        work.close(self.db, lot, abandoned=True, actor="test")
        return lot

    def record(self, signal):
        rows = self.db.query(
            "SELECT record FROM fingerprint_cause_observations"
            " WHERE record->>'signal_id'=%s", (signal.id,))
        self.assertEqual(len(rows), 1)
        return rows[0]["record"]

    def test_ingest_with_repo_proves_old_version(self):
        lot = self.closed_cause(repo=self.path)
        second = self.signal(2, self.base)
        with mock.patch.object(correlation, "active_view",
                               return_value=self.active_view()), \
                mock.patch.object(correlation, "journal_view",
                                  return_value=[]):
            self.assertTrue(self.ingest([self.row(2, self.base)],
                                        "one", "two", repo=self.path))
        record = self.record(second)
        self.assertEqual(record["version"]["verdict"],
                         correlation.VERSION_ANTERIEURE)
        self.assertEqual(record["relation"], "old_version")
        self.assertEqual(record["candidates"], [lot])

    def test_ingest_without_repo_keeps_the_unproven_relation(self):
        self.closed_cause()
        second = self.signal(2, self.base)
        with mock.patch.object(correlation, "active_view",
                               return_value=self.active_view()), \
                mock.patch.object(correlation, "journal_view",
                                  return_value=[]):
            self.assertTrue(self.ingest([self.row(2, self.base)],
                                        "one", "two"))
        record = self.record(second)
        self.assertEqual(record["version"]["verdict"],
                         correlation.VERSION_DIFFERENTE)
        self.assertEqual(record["relation"], "same_cause_version_unknown")

    def test_a_broken_predicate_does_not_lose_the_observation(self):
        """A git failure stays unproven; the A3 receipt is still written."""
        self.closed_cause()
        second = self.signal(2, self.base)
        real_run = subprocess.run

        def broken(command, *args, **kwargs):
            # Only the predicate's git process fails; psql keeps working.
            if command and command[0] == "git":
                raise OSError("git is gone")
            return real_run(command, *args, **kwargs)

        with mock.patch.object(correlation, "active_view",
                               return_value=self.active_view()), \
                mock.patch.object(correlation, "journal_view",
                                  return_value=[]), \
                mock.patch.object(correlation.subprocess, "run",
                                  side_effect=broken):
            self.assertTrue(self.ingest([self.row(2, self.base)],
                                        "one", "two", repo=self.path))
        record = self.record(second)
        self.assertEqual(record["version"]["verdict"],
                         correlation.VERSION_DIFFERENTE)
        self.assertEqual(record["relation"], "same_cause_version_unknown")
