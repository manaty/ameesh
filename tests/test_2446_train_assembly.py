# SPDX-License-Identifier: AGPL-3.0-only
"""#2446 : assemblage du service de train depuis la configuration de l'hôte.

Chaque pièce (dépôt, commit canon audité, contrat CI, provenance, réservation
et échéance) est explicite ; une pièce absente ou inconnue refuse par son nom.
Aucun cas ne retombe sur un autre projet, un autre commit ou un autre mode.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock
from ameesh import (config, review_provenance, train, train_assembly, train_canon,
                    train_contracts, train_reservation, train_runner, train_shadow)

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
id: manaty
review_policies:
  risk_classes:
    default: normal
    projects:
      ameesh:
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
              - "**"
            reviews: 2
"""


class _Repo:
    def __init__(self, root, mechanics_text=MECHANICS, federation_text=FEDERATION):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git("init", "-q")
        if mechanics_text is not None:
            self.write("processus/mecanique.yaml", mechanics_text)
        self.write("federation.yaml", federation_text)
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


class AssemblyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.canon = _Repo(os.path.join(self.tmp.name, "canon"))
        self.project = _Repo(os.path.join(self.tmp.name, "project"))
        self.project.write("README", "fixture\n")
        self.project.commit("main")
        self.commit = self.canon.git("rev-parse", "HEAD")
        self.state = os.path.join(self.tmp.name, "state")
        self.config = {
            "schema": train_assembly.SCHEMA,
            "host": "host-a",
            "mode": "shadow",
            "projects": {
                "ameesh": {
                    "repo": self.project.root,
                    "target": "main",
                    "github": "o/r",
                    "canon": self.canon.root,
                    "canon_commit": self.commit,
                    "ci_contract": "ameesh",
                    "provenance": "review_provenance",
                    "reservation": "packet_reservation",
                    "deadline_minutes": 120,
                    "candidate": False,
                    "initial_since": None,
                },
            },
        }
        self.cfg = types.SimpleNamespace(host="host-a", runner="runner-a@host-a",
                                         state_dir=self.state, train=self.config)

    def assemble(self, **overrides):
        cfg = types.SimpleNamespace(**dict(vars(self.cfg), **overrides))
        return train_assembly.assemble(cfg, notify=lambda message: None)

    def test_absent_key_is_the_historical_inactive_service(self):
        self.assertEqual(self.assemble(train={}).passages, ())
        self.assertIsNone(self.assemble(train={}).lease)

    def test_another_designated_host_assembles_nothing_and_takes_no_lease(self):
        result = self.assemble(host="host-b")
        self.assertEqual(result.passages, ())
        self.assertIsNone(result.lease)
        self.assertIn("host-a", result.reason)

    def test_shadow_assembles_the_exact_audited_inputs(self):
        result = self.assemble()
        self.assertTrue(result.active, result)
        passage, = result.passages
        self.assertIsInstance(passage, train_shadow.Passage)
        self.assertTrue(passage.shadow)
        self.assertIsNone(passage.reservation)
        self.assertEqual((passage.policy_path, passage.policy_commit),
                         (self.canon.root, self.commit))
        self.assertEqual(passage.workflows, train_contracts.ameesh())
        self.assertIs(passage.provider, review_provenance)
        self.assertEqual(passage.project.name, "ameesh")
        self.assertEqual(passage.project.repo, os.path.realpath(self.project.root))
        self.assertEqual(passage.project.target, "main")
        self.assertTrue(passage.store.path.startswith(self.state))
        self.assertIsNotNone(result.lease)
        self.assertTrue(result.lease.owner.startswith("train-service:runner-a@host-a:"))

    def test_active_assembles_a_real_passage_and_the_configured_deadline(self):
        result = self.assemble(train=dict(self.config, mode="active"))
        passage, = result.passages
        self.assertIsInstance(passage, train_runner.Passage)
        self.assertFalse(getattr(passage, "shadow", False))
        self.assertEqual(passage.owner, "runner-a@host-a")
        self.assertIsInstance(passage.reservation, train_reservation.Reservation)
        self.assertAlmostEqual(passage.reservation.deadline({"x": 1}, 1000.0), 8200.0)
        self.assertEqual(passage.policy_commit, self.commit)

    def test_each_passage_compares_its_pin_with_the_gate_canon(self):
        """#2914: the pin is compared with the gate's canon, never replaced."""
        for mode in ("shadow", "active"):
            passage, = self.assemble(train=dict(self.config, mode=mode)).passages
            self.assertIsInstance(passage.canon, train_canon.Watch)
            self.assertEqual(passage.canon.pin, self.commit)
            # this fixture host configures no canon for the gate: unknown, named
            reading = passage.canon.read(None)
            self.assertEqual(reading.state, train_canon.UNKNOWN)
            self.assertIn("no canon configured", reading.why)
            self.assertEqual(passage.policy_commit, self.commit)

    def test_candidate_enrollment_requires_an_activation_boundary(self):
        project = dict(self.config["projects"]["ameesh"], candidate=True)
        with self.assertRaisesRegex(train.TrainError, "initial_since"):
            self.assemble(train=dict(self.config, projects={"ameesh": project}))
        project = dict(project, initial_since="2026-10-18T11:00:00+02:00")
        result = self.assemble(train=dict(self.config, projects={"ameesh": project}))
        self.assertIsInstance(result.passages[0], train_shadow.Passage)
        result = self.assemble(train=dict(self.config, mode="active",
                                          projects={"ameesh": project}))
        self.assertIsInstance(result.passages[0], train_runner.Passage)

    def test_named_refusals_never_substitute_another_piece(self):
        cases = []
        raw = dict(self.config)
        raw.pop("schema")
        cases.append((raw, "missing key"))
        cases.append((dict(self.config, extra=1), "unknown key"))
        cases.append((dict(self.config, mode="production"), "shadow \\| active"))
        project = dict(self.config["projects"]["ameesh"])
        project.pop("canon_commit")
        cases.append((dict(self.config, projects={"ameesh": project}), "missing key"))
        project = dict(self.config["projects"]["ameesh"], ci_contract="other")
        cases.append((dict(self.config, projects={"ameesh": project}), "unknown provider"))
        project = dict(self.config["projects"]["ameesh"], provenance="guess")
        cases.append((dict(self.config, projects={"ameesh": project}), "unknown provider"))
        project = dict(self.config["projects"]["ameesh"], reservation="land")
        cases.append((dict(self.config, projects={"ameesh": project}), "unknown provider"))
        project = dict(self.config["projects"]["ameesh"], canon_commit="HEAD")
        cases.append((dict(self.config, projects={"ameesh": project}),
                      "full audited commit"))
        project = dict(self.config["projects"]["ameesh"], deadline_minutes=0)
        cases.append((dict(self.config, projects={"ameesh": project}),
                      "positive finite number"))
        project = dict(self.config["projects"]["ameesh"], target="release/1.0")
        cases.append((dict(self.config, projects={"ameesh": project}), "release target"))
        project = dict(self.config["projects"]["ameesh"], repo=self.tmp.name + "/absent")
        cases.append((dict(self.config, projects={"ameesh": project}),
                      "directory not found"))
        project = dict(self.config["projects"]["ameesh"], canon_commit="a" * 40)
        project["canon_commit"] = "a" * 40
        cases.append((dict(self.config, projects={"ameesh": project}), "commit not found"))
        for raw, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(train.TrainError, expected):
                    self.assemble(train=raw)

    def test_canon_must_carry_both_regular_audited_files(self):
        broken = _Repo(os.path.join(self.tmp.name, "broken"), mechanics_text=None)
        project = dict(self.config["projects"]["ameesh"], canon=broken.root,
                       canon_commit=broken.git("rev-parse", "HEAD"))
        with self.assertRaisesRegex(train.TrainError, "regular processus/mecanique.yaml"):
            self.assemble(train=dict(self.config, projects={"ameesh": project}))
        broken = _Repo(os.path.join(self.tmp.name, "broken2"),
                       federation_text="federation: {}\n")
        project = dict(self.config["projects"]["ameesh"], canon=broken.root,
                       canon_commit=broken.git("rev-parse", "HEAD"))
        with self.assertRaisesRegex(train.TrainError, "compiled canon refused"):
            self.assemble(train=dict(self.config, projects={"ameesh": project}))

    def test_canon_must_declare_the_packet_calendar_and_full_packet_ci(self):
        before, _, after = MECHANICS.partition("packets:")
        _, _, rest = after.partition("build:")
        no_cadence = _Repo(os.path.join(self.tmp.name, "no-cadence"),
                           mechanics_text=before + "build:" + rest)
        project = dict(self.config["projects"]["ameesh"], canon=no_cadence.root,
                       canon_commit=no_cadence.git("rev-parse", "HEAD"))
        with self.assertRaisesRegex(train.TrainError, "packet cadence"):
            self.assemble(train=dict(self.config, projects={"ameesh": project}))
        impacted = _Repo(os.path.join(self.tmp.name, "impacted"),
                         mechanics_text=MECHANICS.replace("packet: full", "packet: impacted"))
        project = dict(self.config["projects"]["ameesh"], canon=impacted.root,
                       canon_commit=impacted.git("rev-parse", "HEAD"))
        with self.assertRaisesRegex(train.TrainError, "full packet CI"):
            self.assemble(train=dict(self.config, projects={"ameesh": project}))

    def test_deadline_is_never_a_default(self):
        project = dict(self.config["projects"]["ameesh"])
        project.pop("deadline_minutes")
        with self.assertRaisesRegex(train.TrainError, "missing key"):
            self.assemble(train=dict(self.config, projects={"ameesh": project}))

    def test_live_reload_follows_the_effective_pin_and_refuses_a_bad_one(self):
        """#3366: the service re-reads the pin without a runner restart."""
        self.canon.write("docs/guide.md", "guide\n")
        new = self.canon.commit("docs only")
        result = self.assemble()
        passage, = result.passages
        self.assertTrue(callable(result.refresh))
        fresh = types.SimpleNamespace(
            host="host-a", train=dict(self.config, projects={
                "ameesh": dict(self.config["projects"]["ameesh"], canon_commit=new)}))
        with mock.patch.object(train_assembly.config_mod, "load", return_value=fresh):
            changes = result.refresh()
        self.assertEqual(changes, [{"project": "ameesh", "previous": self.commit,
                                    "pin": new, "canon": self.canon.root}])
        self.assertEqual(passage.policy_commit, new)
        self.assertEqual(passage.canon.pin, new)
        # the next passage reads the gate at the new pin; its rules follow it
        passage.canon.reader = lambda db: (new, "")
        db = object()
        with mock.patch.object(train_canon, "gate_rules_gap", return_value="") as gap:
            self.assertEqual(passage.canon.rules(db), "")
            self.assertTrue(passage.canon.read(None).aligned)
        # the first call is the passage's rules closure: (db, cfg, project, ...)
        first = gap.call_args_list[0]
        self.assertIs(first.args[0], db)
        self.assertIs(first.args[1], passage.cfg)
        self.assertEqual(first.args[2], "ameesh")
        self.assertEqual(first.kwargs["pin"], new)
        self.assertEqual(first.kwargs["canon_path"], self.canon.root)
        # a non-pin assembly change is a restart, never a silent half-reload
        changed = types.SimpleNamespace(
            host="host-a", train=dict(self.config, mode="active", projects={
                "ameesh": dict(self.config["projects"]["ameesh"], canon_commit=new)}))
        with mock.patch.object(train_assembly.config_mod, "load", return_value=changed):
            with self.assertRaisesRegex(train.TrainError, "restart the runner"):
                result.refresh()
        self.assertEqual(passage.policy_commit, new)
        # an unreadable pin is a named refusal, never a silent fallback
        broken = types.SimpleNamespace(
            host="host-a", train=dict(self.config, projects={
                "ameesh": dict(self.config["projects"]["ameesh"], canon_commit="a" * 40)}))
        with self.assertRaisesRegex(train.TrainError, "commit not found"):
            train_assembly.reload(result.passages, load=lambda: broken)
        self.assertEqual(passage.policy_commit, new)
        # a service asked from another host refuses the configuration
        with self.assertRaisesRegex(train.TrainError, "designates host-a"):
            train_assembly.reload(result.passages, load=lambda: fresh,
                                  host="host-b")

    def test_refresh_reads_the_profile_on_disk_without_a_restart(self):
        """#3366: the effective file (host + profile) is what the service rereads."""
        self.canon.write("docs/guide.md", "guide\n")
        new = self.canon.commit("docs only")
        host = os.path.join(self.tmp.name, "host-config.json")
        profile = os.path.join(self.tmp.name, "critiques.json")
        with open(host, "w", encoding="utf-8") as target:
            json.dump({"host": "host-a", "train": self.config}, target)
        with open(profile, "w", encoding="utf-8") as target:
            json.dump({"inherit": host, "train": {"projects": {
                "ameesh": {"canon_commit": new}}}}, target)
        cfg = types.SimpleNamespace(**dict(vars(self.cfg), config_path=profile,
                                           config_base=host))
        result = train_assembly.assemble(cfg)
        passage, = result.passages
        self.assertEqual(passage.policy_commit, self.commit)
        changes = result.refresh()
        self.assertEqual(changes, [{"project": "ameesh", "previous": self.commit,
                                    "pin": new, "canon": self.canon.root}])
        self.assertEqual(passage.policy_commit, new)
        # a mode change read from the same file still needs the runner restart
        with open(profile, "w", encoding="utf-8") as target:
            json.dump({"inherit": host, "train": {"mode": "active"}}, target)
        with self.assertRaisesRegex(train.TrainError, "restart the runner"):
            result.refresh()
        self.assertEqual(passage.policy_commit, new)

    def test_config_load_carries_the_raw_train_key(self):
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w", encoding="utf-8") as target:
            json.dump({"train": self.config}, target)
        cfg = config.load({"AMEESH_CONFIG": path})
        self.assertEqual(cfg.train, self.config)


if __name__ == "__main__":
    unittest.main()
