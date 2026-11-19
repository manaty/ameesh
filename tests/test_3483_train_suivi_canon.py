# SPDX-License-Identifier: AGPL-3.0-only
"""#7880 — le suivi automatique du train compare les règles compilées.

Le cas du 19/11 (8fa883ec -> f1c77137) est **suivi par 1** : le bloc
``review_policies`` touché ajoute des chemins propres à ameesh, il ne change
aucune règle compilée du projet nexlink du train ; l'avance est mécanique.
Quand le barème d'un projet du train change, le refus reste et ouvre le
porteur de requalification relue, assigné au rôle de pilote lu dans le canon.

Ce module porte aussi la preuve que le filtre n'est pas « les chemins présents
dans le dépôt du projet » : une règle ajoutée pour un chemin absent du dépôt
change l'objet compilé et refuse quand même.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import config, registry, train_assembly, train_canon, train_requalify, work

from .support import PgTestCase


MECHANICS = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
packets:
  cutoffs:
    - "00:00"
    - "01:00"
    - "02:00"
    - "03:00"
    - "04:00"
    - "05:00"
    - "06:00"
    - "07:00"
    - "08:00"
    - "09:00"
    - "10:00"
    - "11:00"
    - "12:00"
    - "13:00"
    - "14:00"
    - "15:00"
    - "16:00"
    - "17:00"
    - "18:00"
    - "19:00"
    - "20:00"
    - "21:00"
    - "22:00"
    - "23:00"
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
projects: {}
"""

FEDERATION = """federation: "0.1"
id: fixture
review_policies:
  risk_classes:
    default: normal
    projects:
      ameesh:
        default: light
        rules:
          - paths:
              - "src/ameesh/**"
            class: sensitive
      nexlink:
        default: light
        rules:
          - paths:
              - "src/**"
            class: sensitive
  train_reviews:
    schema: ameesh-train-reviews-policy/1
    projects:
      ameesh:
        rules:
          - paths:
              - "src/ameesh/**"
            reviews: 2
      nexlink:
        rules:
          - paths:
              - "src/**"
            reviews: 1
"""


class _Repo:
    def __init__(self, root, mechanics=MECHANICS, federation=FEDERATION):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git("init", "-q")
        self.write("processus/mecanique.yaml", mechanics)
        self.write("federation.yaml", federation)
        self.commit("initial")

    def git(self, *args):
        proc = subprocess.run(["git", "-C", self.root, *args], capture_output=True,
                              text=True, env=self.env)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, name, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as target:
            target.write(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")


def _with_other_project_path(federation):
    """The 19/11 pattern: an ameesh-only path, no nexlink rule change."""
    return federation.replace(
        '          - paths:\n'
        '              - "src/ameesh/**"\n'
        '            reviews: 2\n',
        '          - paths:\n'
        '              - "src/ameesh/**"\n'
        '              - "src/ameesh/credential_gate*.py"\n'
        '            reviews: 2\n')


def _with_nexlink_review_count(federation, reviews=2):
    """A real rule change for the train project: its review count moves."""
    return federation.replace(
        '      nexlink:\n'
        '        rules:\n'
        '          - paths:\n'
        '              - "src/**"\n'
        '            reviews: 1\n',
        '      nexlink:\n'
        '        rules:\n'
        '          - paths:\n'
        '              - "src/**"\n'
        '            reviews: %d\n' % reviews)


def _with_nexlink_absent_path(federation):
    """A rule for a path absent from the project repository still counts."""
    return federation.replace(
        '      nexlink:\n'
        '        rules:\n'
        '          - paths:\n'
        '              - "src/**"\n'
        '            reviews: 1\n',
        '      nexlink:\n'
        '        rules:\n'
        '          - paths:\n'
        '              - "src/**"\n'
        '              - "absent/**"\n'
        '            reviews: 2\n')


class _Fixture:
    """A canon, a host configuration and the two files that carry the pin."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.canon = _Repo(os.path.join(tmp, "canon"))
        self.pin = self.canon.git("rev-parse", "HEAD")
        self.project = {
            "repo": os.path.join(tmp, "project"),
            "target": "main",
            "github": "o/r",
            "canon": self.canon.root,
            "canon_commit": self.pin,
            "ci_contract": "ameesh",
            "provenance": "review_provenance",
            "reservation": "packet_reservation",
            "deadline_minutes": 120,
            "candidate": False,
            "initial_since": None,
        }
        os.makedirs(self.project["repo"], exist_ok=True)
        self.projects = {"nexlink": dict(self.project)}
        self.base = os.path.join(tmp, "config.json")
        self.profile_dir = os.path.join(tmp, "runner-profiles")
        os.makedirs(self.profile_dir, exist_ok=True)
        self.profile = os.path.join(self.profile_dir, "critiques.json")
        self.write_config()
        self.cfg = types.SimpleNamespace(
            host="host-a", config_base=self.base, config_path=self.profile,
            train={"schema": "ameesh-train-host/1", "host": "host-a",
                   "mode": "shadow", "projects": self.projects})

    def add_project(self, name):
        self.projects[name] = dict(self.project)
        self.write_config()

    def write_config(self):
        raw = {
            "host": "host-a",
            "train": {
                "schema": "ameesh-train-host/1", "host": "host-a", "mode": "shadow",
                "projects": {name: dict(project)
                             for name, project in self.projects.items()},
            },
        }
        with open(self.base, "w", encoding="utf-8") as target:
            json.dump(raw, target, indent=2)
        profile = {"inherit": self.base, "train": {"projects": {
            name: {"canon_commit": self.pin} for name in self.projects}}}
        with open(self.profile, "w", encoding="utf-8") as target:
            json.dump(profile, target, indent=2)

    def files(self):
        return (self.base, self.profile)


class SemanticFollowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = _Fixture(self.tmp.name)
        self.cfg = self.fixture.cfg

    def gate(self, federation=None, mechanics=None):
        if federation is not None:
            self.fixture.canon.write("federation.yaml", federation)
        if mechanics is not None:
            self.fixture.canon.write("processus/mecanique.yaml", mechanics)
        return self.fixture.canon.commit("gate")

    def automatic(self, gate, *, gate_gap=""):
        with mock.patch.object(config, "_config_dir_default",
                               return_value=self.fixture.profile_dir), \
             mock.patch.object(train_canon, "gate_rules_gap", return_value=gate_gap):
            return train_requalify.automatic(
                self.cfg, object(), "nexlink",
                gate_reader=lambda *args: (gate, ""))

    def read(self, path):
        with open(path, encoding="utf-8") as source:
            return json.load(source)

    def test_case_19_11_other_project_paths_are_followed(self):
        gate = self.gate(_with_other_project_path(FEDERATION))
        result = self.automatic(gate)
        self.assertEqual(result["state"], "requalified")
        self.assertEqual(result["pin"], self.fixture.pin)
        self.assertEqual(result["gate"], gate)
        self.assertIn("federation.yaml", result["changed"])
        self.assertEqual(result["motif"], "règles compilées inchangées")
        for path in self.fixture.files():
            self.assertEqual(
                self.read(path)["train"]["projects"]["nexlink"]["canon_commit"], gate)
        journal = os.path.join(self.fixture.tmp, "train-canon.jsonl")
        with open(journal, encoding="utf-8") as source:
            row = json.loads(source.readline())
        self.assertEqual(row["motif"], "règles compilées inchangées")
        self.assertEqual((row["pin"], row["gate"]), (self.fixture.pin, gate))
        self.assertIn("federation.yaml", row["changed"])

    def test_train_project_count_change_still_refuses(self):
        gate = self.gate(_with_nexlink_review_count(FEDERATION, reviews=2))
        with self.assertRaises(train_requalify.RequalifyReviewRequired) as raised:
            self.automatic(gate)
        self.assertIn("nexlink", str(raised.exception))
        self.assertIn("reviewed requalification required", str(raised.exception))
        self.assertEqual(raised.exception.guard, ("federation.yaml",))
        for path in self.fixture.files():
            self.assertEqual(
                self.read(path)["train"]["projects"]["nexlink"]["canon_commit"],
                self.fixture.pin)
        self.assertFalse(os.path.exists(
            os.path.join(self.fixture.tmp, "train-canon.jsonl")))

    def test_a_rule_for_a_path_absent_from_the_project_repository_refuses(self):
        gate = self.gate(_with_nexlink_absent_path(FEDERATION))
        with self.assertRaises(train_requalify.RequalifyReviewRequired):
            self.automatic(gate)
        for path in self.fixture.files():
            self.assertEqual(
                self.read(path)["train"]["projects"]["nexlink"]["canon_commit"],
                self.fixture.pin)

    def test_global_mechanics_change_refuses(self):
        gate = self.gate(mechanics=MECHANICS.replace(
            "  distinct_families: true\n",
            "  distinct_families: true\n  light_reviews: 2\n"))
        with self.assertRaises(train_requalify.RequalifyReviewRequired):
            self.automatic(gate)
        for path in self.fixture.files():
            self.assertEqual(
                self.read(path)["train"]["projects"]["nexlink"]["canon_commit"],
                self.fixture.pin)

    def test_gate_other_reading_must_agree_with_the_new_pin(self):
        gate = self.gate(_with_other_project_path(FEDERATION))
        with self.assertRaises(train_requalify.RequalifyReviewRequired) as raised:
            self.automatic(gate, gate_gap="light_reviews porte 2 / train 1")
        self.assertIn("light_reviews", str(raised.exception))

    def test_every_project_of_the_train_is_compared(self):
        self.fixture.add_project("ameesh")
        # The passage is for nexlink, whose rules do not change; ameesh's
        # review count does. The whole train is compared, so the move refuses.
        gate = self.gate(_with_other_project_path(FEDERATION).replace(
            '      ameesh:\n'
            '        rules:\n'
            '          - paths:\n'
            '              - "src/ameesh/**"\n'
            '              - "src/ameesh/credential_gate*.py"\n'
            '            reviews: 2\n',
            '      ameesh:\n'
            '        rules:\n'
            '          - paths:\n'
            '              - "src/ameesh/**"\n'
            '            reviews: 1\n'))
        with self.assertRaises(train_requalify.RequalifyReviewRequired) as raised:
            self.automatic(gate)
        self.assertIn("ameesh : rules", str(raised.exception))


class CarrierLotTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.fixture = _Fixture(self.tmpdir.name)
        self.cfg = self.fixture.cfg
        registry.upsert(self.db, "pilot", harness="claude", host=self.cfg.host)

    def gate(self, federation, message="gate"):
        self.fixture.canon.write("federation.yaml", federation)
        return self.fixture.canon.commit(message)

    def automatic(self, gate):
        with mock.patch.object(config, "_config_dir_default",
                               return_value=self.fixture.profile_dir), \
             mock.patch.object(train_canon, "pilot_of",
                               return_value=lambda db: "pilot"):
            return train_requalify.automatic(
                self.cfg, self.db, "nexlink",
                gate_reader=lambda *args: (gate, ""))

    def carriers(self):
        source = train_requalify._carrier_source("nexlink", self.fixture.pin)
        row = work.open_with_source(self.db, source)
        return [row] if row is not None else []

    def test_refusal_opens_one_assigned_carrier_with_diff_and_dry_run(self):
        gate = self.gate(_with_nexlink_review_count(FEDERATION, 2))
        with self.assertRaises(train_requalify.RequalifyReviewRequired):
            self.automatic(gate)
        rows = self.carriers()
        self.assertEqual(len(rows), 1, rows)
        lot = rows[0]
        self.assertEqual(lot["state"], "intake")
        self.assertEqual(lot["assignee"], "pilot")
        self.assertIn("requalification relue du pin canon", lot["title"])
        self.assertIn(self.fixture.pin[:12], lot["title"])
        self.assertIn(gate[:12], lot["title"])
        body = lot["body"]
        self.assertIn("federation.yaml", body)
        self.assertIn("--dry-run", body)
        self.assertIn("nexlink", body)
        key = train_requalify._carrier_ref("nexlink", self.fixture.pin, gate)
        self.assertEqual(work.existing_by_unique_ref(self.db, key)["id"], lot["id"])
        # A replay of the same reading returns the same lot, never a second one.
        self.assertEqual(train_requalify.ensure_requalification_lot(
            self.cfg, self.db, "nexlink", pin=self.fixture.pin, gate=gate,
            changed=("federation.yaml",), guard=("federation.yaml",),
            gap="nexlink : rules"), lot["id"])
        self.assertEqual(len(self.carriers()), 1)

    def test_a_new_gate_updates_the_open_lot_without_a_pile(self):
        first = self.gate(_with_nexlink_review_count(FEDERATION, 2), "first")
        with self.assertRaises(train_requalify.RequalifyReviewRequired):
            self.automatic(first)
        rows = self.carriers()
        self.assertEqual(len(rows), 1)
        lot_id = int(rows[0]["id"])
        second = self.gate(_with_nexlink_absent_path(FEDERATION), "second")
        with self.assertRaises(train_requalify.RequalifyReviewRequired):
            self.automatic(second)
        rows = self.carriers()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["id"]), lot_id)
        self.assertIn(second[:12], rows[0]["title"])
        self.assertNotIn(first[:12], rows[0]["title"])
        # A standby host reading the older gate must not regress the carrier.
        with self.assertRaises(train_requalify.RequalifyReviewRequired):
            self.automatic(first)
        rows = self.carriers()
        self.assertEqual(len(rows), 1)
        self.assertIn(second[:12], rows[0]["title"])
        self.assertNotIn(first[:12], rows[0]["title"])

    def test_mechanical_follow_opens_no_carrier(self):
        gate = self.gate(_with_other_project_path(FEDERATION))
        with mock.patch.object(train_canon, "gate_rules_gap", return_value=""):
            result = self.automatic(gate)
        self.assertEqual(result["state"], "requalified")
        self.assertEqual(self.carriers(), [])

    def test_alert_cites_the_carrier_lot(self):
        gate = self.gate(_with_nexlink_review_count(FEDERATION, 2))
        sent = []
        watch = train_canon.Watch(
            "nexlink", self.fixture.pin,
            lambda db: (gate, ""), pilot=lambda db: "pilot",
            mailer=lambda db, recipient, body, reading: sent.append((recipient, body)),
            carrier=lambda db, reading:
                train_requalify.ensure_requalification_lot_for_reading(
                    self.cfg, db, "nexlink", reading))
        reading = watch.read(self.db)
        self.assertEqual(watch.alert(self.db, reading, "active"), "pilot")
        rows = self.carriers()
        self.assertEqual(len(rows), 1)
        self.assertIn("Porteur : lot #%d" % int(rows[0]["id"]), sent[0][1])


if __name__ == "__main__":
    unittest.main()
