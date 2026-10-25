# SPDX-License-Identifier: AGPL-3.0-only
"""#3460 (décision #135690 a) : `ameesh agent retire` et `agent restore`.

Un agent retiré n'est plus désigné, réclamé ni joint ; son historique reste
lisible et `restore` rend l'état d'avant. `veille` (watch.SENDER) reste une
identité d'émission système : retirée, elle émet toujours les veilles.
"""
from __future__ import annotations

import json
import os

from ameesh import (agent_retire, registry, review_designation as rd,
                    review_designation_activation as rda, storage, undeliverable,
                    watch, work)

from .support import PgTestCase

ALICE = "human:alice"


class RetireTest(PgTestCase):
    def setUp(self):
        super().setUp()
        # tables que PgTestCase ne vide pas entre deux tests
        self.db.execute("DELETE FROM agent_watches")
        self.first_event = self.db.query(
            "SELECT coalesce(max(id), 0) AS id FROM ameesh_events")[0]["id"]
        registry.upsert(self.db, "worker", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)

    def retire(self, name="worker", **kw):
        kw.setdefault("reason", "décision #135690 a")
        return agent_retire.retire(self.cfg, self.db, name, actor=ALICE, **kw)

    def restore(self, name="worker"):
        return agent_retire.restore(self.cfg, self.db, name, actor=ALICE)

    def events(self, kind):
        return self.db.query("SELECT object, data FROM ameesh_events WHERE type = %s"
                             " AND id > %s ORDER BY id", (kind, self.first_event))

    def test_retire_marks_the_row_only_and_restore_gives_it_back(self):
        before = registry.get(self.db, "worker")
        out = self.retire()
        self.assertTrue(out["changed"])
        row = registry.get(self.db, "worker")
        self.assertIsNotNone(row["retired_ts"])
        self.assertEqual((row["retired_by"], row["retired_reason"]),
                         (ALICE, "décision #135690 a"))
        for key in ("status", "mode", "lease_owner", "lease_epoch", "host"):
            self.assertEqual(row[key], before[key], key)
        self.assertFalse(self.retire()["changed"])
        self.assertTrue(self.restore()["changed"])
        row = registry.get(self.db, "worker")
        self.assertEqual((row["retired_ts"], row["retired_by"], row["retired_reason"]),
                         (None, None, None))
        self.assertFalse(self.restore()["changed"])
        self.assertEqual([e["object"] for e in self.events(agent_retire.RETIRED_EVENT)],
                         ["agent:worker"])
        self.assertEqual([e["data"]["actor"] for e in self.events(
            agent_retire.RESTORED_EVENT)], [ALICE])

    def test_reason_and_known_agent_are_required(self):
        with self.assertRaises(agent_retire.RetireError):
            self.retire(reason="  ")
        with self.assertRaises(agent_retire.RetireError):
            self.retire("nobody")
        with self.assertRaises(agent_retire.RetireError):
            self.restore("nobody")

    def test_an_agent_cannot_retire_on_its_own_authority(self):
        with self.assertRaises(work.WorkError):
            agent_retire.retire(self.cfg, self.db, "worker", reason="x", actor="worker")
        self.assertIsNone(registry.get(self.db, "worker")["retired_ts"])

    def test_retired_is_never_claimed_and_restore_reopens_the_claim(self):
        self.retire()
        self.assertIsNone(registry.claim(self.db, "worker", "runner-test", 600))
        self.assertNotIn("worker", [r["name"] for r in registry.claimable(
            self.db, self.cfg.host)])
        self.restore()
        self.assertIsNotNone(registry.claim(self.db, "worker", "runner-test", 600))

    def test_live_lease_open_lot_and_watch_refuse_naming_each(self):
        lease = registry.claim(self.db, "worker", "runner-test", 600)
        self.assertIsNotNone(lease)
        self.db.execute(
            "INSERT INTO work_items (title, assignee, state) VALUES ('lot', 'worker', 'qa')")
        lot = self.db.query("SELECT id FROM work_items WHERE assignee = 'worker'")[0]["id"]
        storage.of(self.db).watches.put("worker", 1800, self.cfg.host, actor=ALICE)
        with self.assertRaises(agent_retire.RetireError) as ctx:
            self.retire()
        text = str(ctx.exception)
        self.assertIn("live lease (runner-test)", text)
        self.assertIn("lot #%d (qa)" % lot, text)
        self.assertIn("ameesh watch disable --agent worker", text)
        self.assertIsNone(registry.get(self.db, "worker")["retired_ts"])
        self.assertEqual(self.events(agent_retire.RETIRED_EVENT), [])
        # un lot clos ne retient plus
        self.db.execute("UPDATE work_items SET state = 'closed', close_reason = 'done'"
                        " WHERE id = %s", (lot,))
        storage.of(self.db).watches.delete("worker")
        registry.release(self.db, "worker", "runner-test", lease["lease_epoch"])
        self.assertTrue(self.retire()["changed"])

    def test_mail_to_and_from_a_retired_agent_is_refused(self):
        registry.upsert(self.db, "other", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)
        self.retire()
        for queue in (False, True):
            with self.assertRaises(undeliverable.Refused) as ctx:
                undeliverable.check_send(self.cfg, self.db, sender="other", dest="worker",
                                         queue=queue)
            self.assertIn("worker is retired", str(ctx.exception))
            self.assertIn("ameesh agent restore worker", str(ctx.exception))
        with self.assertRaises(undeliverable.Refused):
            undeliverable.check_send(self.cfg, self.db, sender="worker", dest="other",
                                     explicit=True)
        self.restore()
        undeliverable.check_send(self.cfg, self.db, sender="other", dest="worker")

    def test_broadcast_skips_a_retired_agent_even_with_queue(self):
        for name in ("other", "third"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        self.retire()
        for extra in ((), ("--queue",)):
            out = self.cli("send", "all", "bonjour", "--from", "other", *extra)
            self.assertEqual(out.returncode, 0, out.stderr)
        recipients = {r["recipient"] for r in self.db.query(
            "SELECT recipient FROM agent_mailbox WHERE sender = 'other'")}
        self.assertIn("third", recipients)
        self.assertNotIn("worker", recipients)

    def test_veille_retired_still_sends_watches_and_names_legacy_units(self):
        registry.upsert(self.db, watch.SENDER, harness="other", host="pc", cwd=None,
                        mode="externe")
        units = os.path.join(self.tmp, "units")
        os.makedirs(units)
        open(os.path.join(units, "ameesh-veille-mesh-design.timer"), "w").close()
        open(os.path.join(units, "ameesh-watch-worker.timer"), "w").close()
        out = self.retire(watch.SENDER, env={watch.UNIT_DIR_ENV: units})
        self.assertTrue(out["changed"])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("ameesh-veille-mesh-design.timer", out["warnings"][0])
        self.assertNotIn("ameesh-watch-worker", out["warnings"][0])
        # le courrier vers veille reste refusé (#3342), son émission continue
        with self.assertRaises(undeliverable.Refused):
            undeliverable.check_send(self.cfg, self.db, sender="worker", dest=watch.SENDER)
        storage.of(self.db).watches.put("worker", 1800, self.cfg.host, actor=ALICE)
        self.assertEqual(watch.send(self.cfg, self.db, "worker", env={}),
                         "instruction sent to worker")

    def test_designation_never_picks_a_retired_agent(self):
        registry.upsert(self.db, "reviewer", harness="codex", host=self.cfg.host,
                        cwd=self.tmp)
        self.retire("reviewer")
        rows = [r for r in registry.overview(self.db) if r["name"] == "reviewer"]
        self.assertTrue(rd.is_retired(rows[0]))
        self.assertEqual(rd.candidates_from_registry(rows, families={}), [])
        self.assertEqual(rda._unreachable_reviewer(rows[0], set()), "retired")
        with self.assertRaises(work.WorkError) as ctx:
            work.check_reviewer(self.db, "reviewer")
        self.assertIn("reviewer is retired", str(ctx.exception))
        self.restore("reviewer")
        self.assertEqual(work.check_reviewer(self.db, "reviewer"), "reviewer")

    def test_cli_retire_and_restore(self):
        out = self.mesh("agent", "retire", "worker", "--reason", "décision #135690 a",
                        "--actor", ALICE, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual((payload["agent"], payload["changed"]), ("worker", True))
        shown = self.mesh("show", "worker")
        self.assertIn("retired  : since", shown.stdout)
        out = self.mesh("agent", "restore", "worker", "--actor", ALICE)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("worker restored by human:alice", out.stdout)
        out = self.mesh("agent", "retire", "nobody", "--reason", "x", "--actor", ALICE)
        self.assertEqual(out.returncode, 1)
        self.assertIn("unknown agent: nobody", out.stderr)
