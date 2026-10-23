# SPDX-License-Identifier: AGPL-3.0-only
"""#3180: the host lifecycle commands accept --actor from a human terminal."""
import contextlib
import io
from unittest import mock

from ameesh import db as db_mod, host_lifecycle, registry, storage, work
from .support import PgTestCase


class HostCliActorTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle,host_resources CASCADE")
        self.api = storage.of(self.db)
        registry.upsert(self.db, "dev", harness="codex", host="source", cwd="/work/source")
        self.lease = registry.claim(self.db, "dev", "runner", ttl_seconds=300)
        self.reading = self.api.hosts.record(
            {"host": "source", "gate_state": "available"})

    def run_cli(self, argv):
        real_connect = db_mod.connect

        def connect(_cfg):
            return real_connect(self.cfg)

        with mock.patch.object(host_lifecycle.config, "load", return_value=self.cfg), \
                mock.patch.object(host_lifecycle.db_mod, "connect", side_effect=connect):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = host_lifecycle.main(argv)
        return code, out.getvalue()

    def test_drain_cli_takes_its_actor(self):
        with mock.patch.object(host_lifecycle.work, "corrector",
                               return_value="human:smichea") as corrector:
            code, out = self.run_cli(["drain", "source", "--actor", "human:smichea"])
        self.assertEqual(code, 0, out)
        self.assertEqual(corrector.call_args.args[2], "human:smichea")
        row = self.api.host_lifecycle.get("source")
        self.assertEqual((row["mode"], row["actor"]), ("draining", "human:smichea"))

    def test_undrain_cli_forwards_the_explicit_actor(self):
        self.api.host_lifecycle.drain("source", actor="human:op", critical=("dev",))
        with mock.patch.object(host_lifecycle.work, "corrector",
                               return_value="human:smichea") as corrector:
            code, out = self.run_cli(["undrain", "source", "--actor", "human:smichea"])
        self.assertEqual(code, 0, out)
        self.assertEqual(corrector.call_args.args[2], "human:smichea")
        row = self.api.host_lifecycle.get("source")
        self.assertEqual((row["mode"], row["actor"]), ("returning", "human:smichea"))

    def loss_then_return(self):
        self.db.execute("UPDATE host_resources SET sampled_at="
                        "clock_timestamp()-interval '1000 seconds' WHERE host='source'")
        registry.release(self.db, "dev", "runner", self.lease["lease_epoch"])
        self.api.host_lifecycle.detect_losses(actor="remote", older_than=180,
                                              critical=("dev",))
        self.db.execute("UPDATE host_lifecycle "
                        "SET updated_at=clock_timestamp()-interval '900 seconds' "
                        "WHERE host='source'")
        # A sustained return: two readings in the detection window, then a fresh one.
        self.db.execute(
            "INSERT INTO host_resources(host, sampled_at, gate_state) VALUES "
            "('source', clock_timestamp()-interval '200 seconds', 'available'), "
            "('source', clock_timestamp()-interval '100 seconds', 'available')")
        self.api.hosts.record({"host": "source", "gate_state": "available"})

    def test_reconcile_cli_forwards_the_explicit_actor(self):
        self.loss_then_return()
        with mock.patch.object(host_lifecycle.work, "corrector",
                               return_value="human:smichea") as corrector:
            code, out = self.run_cli(["reconcile", "source", "--actor", "human:smichea"])
        self.assertEqual(code, 0, out)
        self.assertEqual(corrector.call_args.args[2], "human:smichea")
        row = self.api.host_lifecycle.get("source")
        self.assertEqual((row["mode"], row["actor"]), ("available", "human:smichea"))

    def test_unbound_terminal_is_a_named_refusal_not_a_traceback(self):
        self.api.host_lifecycle.drain("source", actor="human:op", critical=("dev",))
        refusal = work.WorkError("correction: say who corrects (--actor human:<id>)")
        with mock.patch.object(host_lifecycle.work, "corrector", side_effect=refusal):
            code, out = self.run_cli(["undrain", "source"])
        self.assertEqual(code, 1)
        self.assertIn("host request refused: correction: say who corrects", out)
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "draining")
