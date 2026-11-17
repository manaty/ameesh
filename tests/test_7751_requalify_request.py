# SPDX-License-Identifier: AGPL-3.0-only
"""#7751: a reviewed canon requalification requested from another host.

The requester persists the exact reading and cited opinions without a lease or
a configuration write; the live train service lease holder applies them at its
next pass with the same reviewed guards, and refuses a stale pin or gate.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

from ameesh import storage, train_cli, train_requalify as rq
from ameesh import train_requalify_request as requests
from ameesh import train_requalify_review as review
from ameesh.train import TrainError

from . import test_3366_train_canon_requalify as fixture
from .support import PgTestCase


class _CanonFixture:
    """A real canon repository and the two declaring configuration files."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.canon = fixture._Repo(os.path.join(self.tmp.name, "canon"))
        self.c0 = self.canon.git("rev-parse", "HEAD")
        self.canon.write("processus/mecanique.yaml", "schema: ameesh-mechanics/2\n")
        self.c1 = self.canon.commit("rules change")
        self.canon.write("plan/roadmap.md", "next\n")
        self.c2 = self.canon.commit("plan only")
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
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(
            rq.config, "_config_dir_default",
            return_value=os.path.join(self.tmp.name, "runner-profiles")))

    def write_base(self, extra=None, pin=None):
        text = {"host": "host-a", "train": {
            "schema": "ameesh-train-host/1", "host": "host-a", "mode": "shadow",
            "projects": {"ameesh": dict(self.project, canon_commit=pin or self.c0)}}}
        text.update(extra or {})
        with open(self.base, "w", encoding="utf-8") as target:
            json.dump(text, target, indent=2)

    def write_profile(self, pin=None):
        with open(self.profile, "w", encoding="utf-8") as target:
            json.dump({"inherit": os.path.abspath(self.base), "train": {"projects": {
                "ameesh": {"canon_commit": pin or self.c0}}}}, target)

    def host_cfg(self, host, *, role=None, pin=None):
        raw = {"schema": "ameesh-train-host/1", "host": "host-a",
               "mode": "shadow",
               "projects": {"ameesh": dict(self.project,
                                           canon_commit=pin or self.c0)}}
        if role is not None:
            raw["role"] = role
        return types.SimpleNamespace(
            host=host, config_path=self.profile, config_base=self.base, train=raw)

    def pins(self):
        with open(self.base, encoding="utf-8") as source:
            base = json.load(source)["train"]["projects"]["ameesh"]["canon_commit"]
        with open(self.profile, encoding="utf-8") as source:
            profile = json.load(source)["train"]["projects"]["ameesh"]["canon_commit"]
        return base, profile


class RequestStorageTest(PgTestCase):
    """The durable request lives in the database, without a lease."""

    def setUp(self):
        super().setUp()
        self.lot = int(fixture.work.add(self.db, title="requalify trace",
                                        app="ameesh")["id"])

    def test_request_is_persisted_and_a_retry_returns_the_same_row(self):
        ops = storage.of(self.db).work
        first = ops.train_requalify_request(
            "ameesh", "a" * 40, "b" * 40,
            ["milestone:43369", "milestone:43370"], self.lot, "pilot")
        self.assertEqual((first["project"], first["pin"], first["gate"]),
                         ("ameesh", "a" * 40, "b" * 40))
        self.assertEqual(first["reviews"], ["milestone:43369", "milestone:43370"])
        self.assertEqual((first["lot"], first["requester"], first["state"]),
                         (self.lot, "pilot", "requested"))
        self.assertTrue(first["requested_at"])
        second = ops.train_requalify_request(
            "ameesh", "a" * 40, "b" * 40,
            ["milestone:43369", "milestone:43370"], self.lot, "pilot")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(ops.train_requalify_requests("ameesh")), 1)
        self.assertEqual(ops.train_requalify_requests("nexlink"), [])

    def test_a_different_reading_is_another_pending_request(self):
        ops = storage.of(self.db).work
        first = ops.train_requalify_request(
            "ameesh", "a" * 40, "b" * 40, ["milestone:1"], self.lot, "pilot")
        second = ops.train_requalify_request(
            "ameesh", "a" * 40, "c" * 40, ["milestone:1"], self.lot, "pilot")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual([row["id"] for row in ops.train_requalify_requests("ameesh")],
                         [first["id"], second["id"]])

    def test_resolution_is_compare_and_set_and_names_the_holder(self):
        ops = storage.of(self.db).work
        row = ops.train_requalify_request(
            "ameesh", "a" * 40, "b" * 40, ["milestone:1"], self.lot, "pilot")
        applied = ops.train_requalify_resolve(row["id"], "applied", actor="host-a")
        self.assertEqual((applied["state"], applied["resolved_by"]),
                         ("applied", "host-a"))
        self.assertTrue(applied["resolved_at"])
        self.assertIsNone(ops.train_requalify_resolve(
            row["id"], "refused", actor="host-b", error="late"))
        self.assertEqual(ops.train_requalify_requests("ameesh"), [])

    def test_a_refusal_keeps_its_exact_reason(self):
        ops = storage.of(self.db).work
        row = ops.train_requalify_request(
            "ameesh", "a" * 40, "b" * 40, ["milestone:1"], self.lot, "pilot")
        refused = ops.train_requalify_resolve(
            row["id"], "refused", actor="host-a",
            error="stale requalification request: gate moved from b to c")
        self.assertEqual(refused["state"], "refused")
        self.assertIn("gate moved", refused["error"])
        self.assertIsNone(ops.train_requalify_resolve(row["id"], "applied",
                                                      actor="host-a"))

    def test_only_applied_or_refused_resolves(self):
        ops = storage.of(self.db).work
        row = ops.train_requalify_request(
            "ameesh", "a" * 40, "b" * 40, ["milestone:1"], self.lot, "pilot")
        self.assertIsNone(ops.train_requalify_resolve(row["id"], "requested",
                                                      actor="host-a"))
        self.assertEqual(ops.train_requalify_requests("ameesh")[0]["id"], row["id"])


class RequestApplicationTest(_CanonFixture, PgTestCase):
    """The holder applies the recorded reading with the reviewed guards."""

    def setUp(self):
        super().setUp()
        self.lot = int(fixture.work.add(self.db, title="canon trace",
                                        app="ameesh")["id"])

    def record(self, gate):
        """A recorded request for (pin c0, gate); returns the stored row."""
        requester = self.host_cfg("host-b")
        with mock.patch.object(requests.train_canon, "gate_commit",
                               return_value=(gate, "")):
            return requests.request(requester, self.db, "ameesh", lot=self.lot,
                                    reviews=["milestone:43370", "milestone:43369"],
                                    actor="pilot")

    def notes(self):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (self.lot,))]

    def evidence(self, reviews=None):
        return [{"reference": reference, "actor": "reader", "commit": self.c1,
                 "record": "proof:" + reference}
                for reference in (reviews or ["milestone:43369", "milestone:43370"])]

    def test_a_request_from_another_host_is_applied_by_the_holder(self):
        row = self.record(self.c1)
        self.assertEqual((row["pin"], row["gate"], row["state"]),
                         (self.c0, self.c1, "requested"))
        # The request path accepts host-b; the direct move still refuses it.
        with self.assertRaisesRegex(rq.RequalifyError, "designates host-a"):
            rq.plan(self.host_cfg("host-b"), self.db, "ameesh",
                    gate_reader=lambda *args: (self.c1, ""))
        with mock.patch.object(review, "qualify",
                               return_value=self.evidence()) as qualify:
            outcomes = requests.apply_pending(
                self.host_cfg("host-a"), self.db, "ameesh",
                gate_reader=lambda *args: (self.c1, ""))
        self.assertEqual(outcomes[0]["state"], "applied")
        self.assertEqual(self.pins(), (self.c1, self.c1))
        self.assertEqual(qualify.call_count, 1)
        args = qualify.call_args.args
        # (cfg, db, spec, pin, gate, changed, reviews)
        self.assertEqual((args[3], args[4]), (self.c0, self.c1))
        self.assertEqual(args[6], ["milestone:43369", "milestone:43370"])
        note = self.notes()[-1]
        self.assertIn("canon pin requalified", note)
        self.assertIn("request: %s" % row["id"], note)
        self.assertIn("applied on host-a", note)
        saved = storage.of(self.db).work.train_requalify_requests("ameesh")
        self.assertEqual(saved, [])
        applied = self.db.query(
            "SELECT state, resolved_by, error FROM integration_train_requalifications"
            " WHERE id = %s", (row["id"],))[0]
        self.assertEqual((applied["state"], applied["resolved_by"], applied["error"]),
                         ("applied", "host-a", None))

    def test_a_host_without_a_train_configuration_requests_with_explicit_pin(self):
        """The pilot's host needs no train block: the exact reading is stated."""
        blind = types.SimpleNamespace(host="host-b", config_path=self.profile,
                                      config_base=self.base, train={})
        row = requests.request(
            blind, self.db, "ameesh", lot=self.lot,
            reviews=["milestone:43369", "milestone:43370"], actor="pilot",
            pin=self.c0, gate=self.c1)
        self.assertEqual((row["pin"], row["gate"], row["state"]),
                         (self.c0, self.c1, "requested"))
        with mock.patch.object(review, "qualify", return_value=self.evidence()):
            outcomes = requests.apply_pending(
                self.host_cfg("host-a"), self.db, "ameesh",
                gate_reader=lambda *args: (self.c1, ""))
        self.assertEqual(outcomes[0]["state"], "applied")
        self.assertEqual(self.pins(), (self.c1, self.c1))

    def test_a_host_without_a_train_configuration_needs_the_exact_pin(self):
        blind = types.SimpleNamespace(host="host-b", config_path=self.profile,
                                      config_base=self.base, train={})
        with self.assertRaisesRegex(rq.RequalifyError, "give --pin"):
            requests.request(
                blind, self.db, "ameesh", lot=self.lot,
                reviews=["milestone:43369"], actor="pilot")

    def test_a_missing_pilot_assent_is_refused_without_writing(self):
        row = self.record(self.c1)
        refusal = TrainError("reviewed requalification requires a current pilot opinion")
        with mock.patch("ameesh.train_requalify_review.qualify",
                        side_effect=refusal):
            outcomes = requests.apply_pending(
                self.host_cfg("host-a"), self.db, "ameesh",
                gate_reader=lambda *args: (self.c1, ""))
        self.assertEqual(outcomes[0]["state"], "refused")
        self.assertIn("current pilot opinion", outcomes[0]["error"])
        self.assertEqual(self.pins(), (self.c0, self.c0))
        self.assertFalse([note for note in self.notes() if "requalified" in note])
        saved = self.db.query(
            "SELECT state, error FROM integration_train_requalifications"
            " WHERE id = %s", (row["id"],))[0]
        self.assertEqual(saved["state"], "refused")
        self.assertIn("current pilot opinion", saved["error"])

    def test_a_gate_changed_between_request_and_application_is_refused(self):
        row = self.record(self.c1)
        with mock.patch("ameesh.train_requalify_review.qualify") as qualify:
            outcomes = requests.apply_pending(
                self.host_cfg("host-a"), self.db, "ameesh",
                gate_reader=lambda *args: (self.c2, ""))
        self.assertEqual(outcomes[0]["state"], "refused")
        self.assertIn("stale requalification request: gate moved", outcomes[0]["error"])
        self.assertIn(self.c1[:12], outcomes[0]["error"])
        self.assertIn(self.c2[:12], outcomes[0]["error"])
        qualify.assert_not_called()
        self.assertEqual(self.pins(), (self.c0, self.c0))
        saved = self.db.query(
            "SELECT state, error FROM integration_train_requalifications"
            " WHERE id = %s", (row["id"],))[0]
        self.assertEqual(saved["state"], "refused")
        self.assertIn("gate moved", saved["error"])

    def test_a_pin_changed_between_request_and_application_is_refused(self):
        self.record(self.c1)
        self.write_base(pin=self.c1)
        self.write_profile(pin=self.c1)
        with mock.patch("ameesh.train_requalify_review.qualify") as qualify:
            outcomes = requests.apply_pending(
                self.host_cfg("host-a", pin=self.c1), self.db, "ameesh",
                gate_reader=lambda *args: (self.c1, ""))
        self.assertEqual(outcomes[0]["state"], "refused")
        self.assertIn("stale requalification request: pin moved", outcomes[0]["error"])
        self.assertIn(self.c0[:12], outcomes[0]["error"])
        qualify.assert_not_called()
        self.assertEqual(self.pins(), (self.c1, self.c1))


class RequestHolderOnlyTest(unittest.TestCase):
    """Only the live service lease holder applies at a pass."""

    def service(self, lease):
        from ameesh import train_runner
        passage = mock.Mock()
        passage.scheduled_step.return_value = ({"result": "ci"}, 200)
        service = train_runner.Service(
            types.SimpleNamespace(), [passage], stop=threading.Event(),
            report=mock.Mock(), lease=lease, refresh=lambda: [])
        return service, passage

    def test_a_pass_of_the_lease_holder_applies_pending_requests(self):
        lease = types.SimpleNamespace(epoch=1, effect=lambda: contextlib.nullcontext())
        service, passage = self.service(lease)
        with mock.patch("ameesh.train_requalify_request.apply_holder",
                        return_value=[]) as apply_holder, \
                mock.patch("ameesh.scoped_db.ScopedDb", return_value=mock.Mock()):
            self.assertEqual(service.pass_once(0, 100), 200)
        apply_holder.assert_called_once()
        passage.scheduled_step.assert_called_once()

    def test_without_the_live_lease_no_request_is_applied(self):
        idle = types.SimpleNamespace(epoch=None,
                                     effect=lambda: contextlib.nullcontext())
        for lease in (None, idle):
            with self.subTest(lease=lease):
                service, passage = self.service(lease)
                with mock.patch("ameesh.train_requalify_request.apply_holder") as apply_holder, \
                        mock.patch("ameesh.scoped_db.ScopedDb", return_value=mock.Mock()):
                    self.assertEqual(service.pass_once(0, 100), 200)
                apply_holder.assert_not_called()
                passage.scheduled_step.assert_called_once()


class RequestCommandTest(unittest.TestCase):
    def test_request_requires_reviewed_and_passes_the_bound_actor(self):
        db = mock.Mock()
        ctx = mock.Mock()
        with mock.patch.object(train_cli.config, "load", return_value=ctx), \
                mock.patch.object(train_cli.db_mod, "open_db", return_value=db), \
                mock.patch("ameesh.train_requalify_review.actor",
                           return_value="bureau"), \
                mock.patch.object(requests, "request", return_value={}) as request, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(train_cli.main([
                "--project", "nexlink", "requalify", "--reviewed", "--request",
                "--lot", "7751", "--review", "milestone:1"]), 0)
        self.assertEqual(request.call_args.kwargs["actor"], "bureau")
        self.assertEqual(request.call_args.kwargs["reviews"], ["milestone:1"])
        self.assertEqual(request.call_args.kwargs["lot"], 7751)
        self.assertFalse(request.call_args.kwargs["dry_run"])
        db.close.assert_called_once_with()

    def test_request_passes_the_explicit_reading(self):
        db = mock.Mock()
        ctx = mock.Mock()
        with mock.patch.object(train_cli.config, "load", return_value=ctx), \
                mock.patch.object(train_cli.db_mod, "open_db", return_value=db), \
                mock.patch("ameesh.train_requalify_review.actor",
                           return_value="pilot"), \
                mock.patch.object(requests, "request", return_value={}) as request, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(train_cli.main([
                "--project", "nexlink", "requalify", "--reviewed", "--request",
                "--lot", "7751", "--pin", "a" * 40, "--gate", "b" * 40,
                "--review", "milestone:1"]), 0)
        self.assertEqual(request.call_args.kwargs["pin"], "a" * 40)
        self.assertEqual(request.call_args.kwargs["gate"], "b" * 40)

    def test_pin_or_gate_outside_a_request_is_refused(self):
        for option in (["--pin", "a" * 40], ["--gate", "b" * 40]):
            with self.subTest(option=option):
                db = mock.Mock()
                with mock.patch.object(train_cli.config, "load",
                                       return_value=mock.Mock()), \
                        mock.patch.object(train_cli.db_mod, "open_db",
                                          return_value=db), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(train_cli.main([
                        "--project", "nexlink", "requalify", "--lot", "7751",
                        *option]), 1)
                db.close.assert_called_once_with()

    def test_request_without_reviewed_is_refused_before_any_identity(self):
        db = mock.Mock()
        with mock.patch.object(train_cli.config, "load", return_value=mock.Mock()), \
                mock.patch.object(train_cli.db_mod, "open_db", return_value=db), \
                mock.patch("ameesh.train_requalify_review.actor") as actor:
            self.assertEqual(train_cli.main([
                "--project", "nexlink", "requalify", "--request",
                "--lot", "7751", "--review", "milestone:1"]), 1)
        actor.assert_not_called()
        db.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
