# SPDX-License-Identifier: AGPL-3.0-only
"""#3366 — the pilot's mechanical canon-pin requalification.

The pin moves to the gate commit only when the canon diff leaves the two rule
files alone; every configuration file that declares the pin follows, the lot
receives the trace, and anything else refuses without writing.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import train_requalify as requalify
from ameesh import work

from .support import PgTestCase


class _Repo:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git("init", "-q")
        self.write("processus/mecanique.yaml", "schema: ameesh-mechanics/1\n")
        self.write("federation.yaml", 'federation: "0.1"\n')
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


class CanonPinRequalifyTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.canon = _Repo(os.path.join(self.tmp.name, "canon"))
        self.c0 = self.canon.git("rev-parse", "HEAD")
        self.canon.write("processus/lots.md", "lots only\n")
        self.c1 = self.canon.commit("lots only")
        self.canon.write("processus/mecanique.yaml", "schema: ameesh-mechanics/2\n")
        self.c2 = self.canon.commit("rules change")
        self.canon.write("federation.yaml", 'federation: "0.2"\n')
        self.c3 = self.canon.commit("federation change")
        self.project = {
            "repo": os.path.join(self.tmp.name, "project"),
            "target": "main",
            "github": "o/r",
            "canon": self.canon.root,
            "canon_commit": self.c0,
            "ci_contract": "ameesh",
            "provenance": "review_provenance",
            "reservation": "packet_reservation",
            "deadline_minutes": 120,
            "candidate": False,
            "initial_since": None,
        }
        self.base = os.path.join(self.tmp.name, "config.json")
        self.profile = os.path.join(self.tmp.name, "critiques.json")
        self.write_base({})
        self.write_profile({})
        self.cfg = types.SimpleNamespace(
            host="host-a", config_path=self.profile, config_base=self.base,
            train={"schema": "ameesh-train-host/1", "host": "host-a",
                   "mode": "shadow", "projects": {"ameesh": dict(self.project)}})
        self.lot = int(work.add(self.db, title="canon pin", app="ameesh")["id"])

    def write_base(self, extra=None):
        extra = extra or {}
        text = {"host": "host-a", "train": {
            "schema": "ameesh-train-host/1", "host": "host-a", "mode": "shadow",
            "projects": {"ameesh": dict(self.project)}}}
        text.update(extra)
        with open(self.base, "w", encoding="utf-8") as target:
            json.dump(text, target, indent=2)

    def write_profile(self, extra=None):
        extra = extra or {}
        # inherit from THIS base, never from the host's ~/.config/ameesh:
        # `inherit: true` resolves to that host file, absent on a CI runner.
        text = {"inherit": os.path.abspath(self.base), "train": {"projects": {
            "ameesh": {"canon_commit": self.c0}}}}
        text.update(extra)
        with open(self.profile, "w", encoding="utf-8") as target:
            json.dump(text, target, indent=2)

    def reader(self, gate):
        return lambda db, cfg, project: (gate, "")

    def pins(self):
        with open(self.base, encoding="utf-8") as source:
            base = json.load(source)["train"]["projects"]["ameesh"]["canon_commit"]
        with open(self.profile, encoding="utf-8") as source:
            profile = json.load(source)["train"]["projects"]["ameesh"]["canon_commit"]
        return base, profile

    def notes(self):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (self.lot,))]

    def test_lots_only_moves_every_declaring_file_and_traces_the_lot(self):
        result = requalify.apply(self.cfg, self.db, "ameesh", lot=self.lot,
                                 actor="pilot", gate_reader=self.reader(self.c1))
        self.assertEqual(result["state"], "requalified")
        self.assertEqual((result["pin"], result["gate"]), (self.c0, self.c1))
        self.assertEqual(self.pins(), (self.c1, self.c1))
        self.assertEqual(result["files"], [os.path.abspath(self.base),
                                           os.path.abspath(self.profile)])
        note = self.notes()[-1]
        self.assertIn("canon pin requalified", note)
        self.assertIn(self.c0[:12], note)
        self.assertIn(self.c1[:12], note)
        self.assertIn("processus/lots.md", note)

    @staticmethod
    def _bytes(path):
        with open(path, "rb") as source:
            return source.read()

    def test_a_failed_trace_restores_every_file(self):
        before = (self._bytes(self.base), self._bytes(self.profile))
        with mock.patch.object(requalify.work, "note",
                               side_effect=RuntimeError("trace down")):
            with self.assertRaisesRegex(RuntimeError, "trace down"):
                requalify.apply(self.cfg, self.db, "ameesh", lot=self.lot,
                                actor="pilot", gate_reader=self.reader(self.c1))
        self.assertEqual((self._bytes(self.base),
                          self._bytes(self.profile)), before)

    def test_a_rule_change_refuses_and_writes_nothing(self):
        before = (self._bytes(self.base), self._bytes(self.profile))
        with self.assertRaisesRegex(requalify.RequalifyError, "rules change"):
            requalify.apply(self.cfg, self.db, "ameesh", lot=self.lot,
                            actor="pilot", gate_reader=self.reader(self.c2))
        self.assertEqual((self._bytes(self.base),
                          self._bytes(self.profile)), before)
        self.assertFalse([n for n in self.notes() if "requalified" in n])

    def test_a_federation_change_refuses_and_writes_nothing(self):
        before = (self._bytes(self.base), self._bytes(self.profile))
        with self.assertRaisesRegex(requalify.RequalifyError,
                                    "federation.yaml"):
            requalify.apply(self.cfg, self.db, "ameesh", lot=self.lot,
                            actor="pilot", gate_reader=self.reader(self.c3))
        self.assertEqual((self._bytes(self.base),
                          self._bytes(self.profile)), before)
        self.assertFalse([n for n in self.notes() if "requalified" in n])

    def test_divergent_canon_paths_refuse_before_writing(self):
        other = os.path.join(self.tmp.name, "other-canon")
        with open(self.profile, "w", encoding="utf-8") as target:
            json.dump({"inherit": os.path.abspath(self.base), "train": {"projects": {"ameesh": {
                "canon": other, "canon_commit": self.c0}}}}, target)
        with self.assertRaisesRegex(requalify.RequalifyError, "disagree on .*canon"):
            requalify.plan(self.cfg, self.db, "ameesh",
                           gate_reader=self.reader(self.c1))
        with open(self.base, encoding="utf-8") as source:
            self.assertEqual(
                json.load(source)["train"]["projects"]["ameesh"]["canon_commit"],
                self.c0)

    def test_an_unreadable_profile_refuses_the_move(self):
        broken = types.SimpleNamespace(errors=("critiques.json: bad JSON",),
                                       profiles=())
        with mock.patch.object(requalify.config, "audit_profiles",
                               return_value=broken):
            with self.assertRaisesRegex(requalify.RequalifyError,
                                        "unreadable executor profile"):
                requalify.plan(self.cfg, self.db, "ameesh",
                               gate_reader=self.reader(self.c1))

    def test_an_aligned_pin_writes_nothing_and_traces_nothing(self):
        result = requalify.execute(self.cfg, self.db, "ameesh", lot=self.lot,
                                   actor="pilot", dry_run=True,
                                   gate_reader=self.reader(self.c0))
        self.assertEqual(result["state"], "aligned")
        self.assertEqual(self.pins(), (self.c0, self.c0))
        self.assertFalse([n for n in self.notes() if "requalified" in n])

    def test_dry_run_does_not_write_or_trace(self):
        with mock.patch.object(requalify, "plan", wraps=requalify.plan) as planned:
            result = requalify.execute(self.cfg, self.db, "ameesh", lot=self.lot,
                                       actor="pilot", dry_run=True,
                                       gate_reader=self.reader(self.c1))
        planned.assert_called_once()
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.pins(), (self.c0, self.c0))
        self.assertFalse([n for n in self.notes() if "requalified" in n])

    def test_an_inherited_pin_is_not_invented_in_the_profile(self):
        with open(self.profile, "w", encoding="utf-8") as target:
            json.dump({"inherit": os.path.abspath(self.base)}, target)
        result = requalify.apply(self.cfg, self.db, "ameesh", lot=self.lot,
                                 actor="pilot", gate_reader=self.reader(self.c1))
        self.assertEqual(result["files"], [os.path.abspath(self.base)])
        with open(self.base, encoding="utf-8") as source:
            base_pin = json.load(source)["train"]["projects"]["ameesh"]["canon_commit"]
        with open(self.profile, encoding="utf-8") as source:
            profile = json.load(source)
        self.assertEqual(base_pin, self.c1)
        self.assertNotIn("train", profile)  # the profile was left untouched

    def test_divergent_pins_refuse_instead_of_bypassing_the_guard(self):
        with open(self.profile, "w", encoding="utf-8") as target:
            json.dump({"inherit": os.path.abspath(self.base), "train": {"projects": {"ameesh": {
                "canon_commit": self.c1}}}}, target)
        with self.assertRaisesRegex(requalify.RequalifyError, "disagree"):
            requalify.plan(self.cfg, self.db, "ameesh",
                           gate_reader=self.reader(self.c2))
        self.assertFalse([n for n in self.notes() if "requalified" in n])

    def test_a_gate_commit_outside_the_canon_refuses(self):
        with self.assertRaisesRegex(requalify.RequalifyError, "commit not found"):
            requalify.plan(self.cfg, self.db, "ameesh",
                           gate_reader=self.reader("f" * 40))

    def test_a_command_on_another_host_refuses(self):
        cfg = types.SimpleNamespace(**dict(vars(self.cfg), host="host-b"))
        with self.assertRaisesRegex(requalify.RequalifyError, "designates host-a"):
            requalify.plan(cfg, self.db, "ameesh",
                           gate_reader=self.reader(self.c1))

    def test_no_file_declares_the_pin_refuses(self):
        with open(self.base, "w", encoding="utf-8") as target:
            json.dump({"train": {"schema": "ameesh-train-host/1", "host": "host-a",
                                 "mode": "shadow", "projects": {}}}, target)
        with open(self.profile, "w", encoding="utf-8") as target:
            json.dump({"inherit": os.path.abspath(self.base)}, target)
        cfg = types.SimpleNamespace(
            host="host-a", config_path=self.profile, config_base=self.base,
            train={"schema": "ameesh-train-host/1", "host": "host-a",
                   "mode": "shadow", "projects": {"ameesh": dict(self.project)}})
        with self.assertRaisesRegex(requalify.RequalifyError, "no configuration file"):
            requalify.plan(cfg, self.db, "ameesh",
                           gate_reader=self.reader(self.c1))


if __name__ == "__main__":
    unittest.main()
