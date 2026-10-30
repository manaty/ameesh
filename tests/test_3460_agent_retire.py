# SPDX-License-Identifier: AGPL-3.0-only
"""#3460 (décision #135690 a) : `ameesh agent retire` et `agent restore`.

Un agent retiré n'est plus désigné, réclamé ni joint ; son historique reste
lisible et `restore` rend l'état d'avant. `veille` (watch.SENDER) reste une
identité d'émission système : retirée, elle émet toujours les veilles.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from unittest import mock

from ameesh import (agent_retire, backend, db as db_mod, mail, registry,
                    review_designation as rd, review_designation_activation as rda,
                    storage, undeliverable, watch, work)

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
        # un lot livré (merged, promoted) ou clos ne retient plus ; un lot en
        # build retient toujours
        self.db.execute("UPDATE work_items SET state = 'closed', close_reason = 'done'"
                        " WHERE id = %s", (lot,))
        for state in ("merged", "promoted", "build"):
            self.db.execute("INSERT INTO work_items (title, assignee, state)"
                            " VALUES (%s, 'worker', %s)", ("lot " + state, state))
        storage.of(self.db).watches.delete("worker")
        registry.release(self.db, "worker", "runner-test", lease["lease_epoch"])
        with self.assertRaises(agent_retire.RetireError) as ctx:
            self.retire()
        self.assertIn("(build)", str(ctx.exception))
        self.assertNotIn("(merged)", str(ctx.exception))
        self.assertNotIn("(promoted)", str(ctx.exception))
        self.db.execute("UPDATE work_items SET state = 'closed', close_reason = 'done'"
                        " WHERE assignee = 'worker' AND state = 'build'")
        self.assertTrue(self.retire()["changed"])

    def external_reservation(self, state="intake"):
        self.db.execute("UPDATE agent_registry SET mode = 'externe', responsible = %s"
                        " WHERE name = 'worker'", (ALICE,))
        return self.db.query(
            "INSERT INTO work_items (title, external_executor, state, close_reason)"
            " VALUES ('external gesture', 'worker', %s, %s) RETURNING id",
            (state, "done" if state == "closed" else None))[0]["id"]

    def test_external_executor_blocks_retirement_until_reservation_cleared(self):
        lot = self.external_reservation()
        with self.assertRaises(agent_retire.RetireError) as ctx:
            self.retire()
        text = str(ctx.exception)
        self.assertIn("external executor of lot #%d (intake)" % lot, text)
        self.assertIn("ameesh work external-executor %d --clear" % lot, text)
        self.assertIn("ameesh work external-executor %d <executor>" % lot, text)
        self.assertIsNone(registry.get(self.db, "worker")["retired_ts"])
        self.assertEqual(self.events(agent_retire.RETIRED_EVENT), [])
        self.assertIsNone(work.get(self.db, lot)["assignee"])
        work.set_external_executor(self.db, lot, None, clear=True, actor=ALICE,
                                   cfg=self.cfg)
        self.assertTrue(self.retire()["changed"])

    def test_external_executor_blocks_all_open_states_but_not_delivered_lots(self):
        for state in ("intake", "build", "qa", "blocked", "waiting_human"):
            with self.subTest(state=state):
                lot = self.external_reservation(state)
                with self.assertRaises(agent_retire.RetireError) as ctx:
                    self.retire()
                self.assertIn("external executor of lot #%d (%s)" % (lot, state),
                              str(ctx.exception))
                self.db.execute("UPDATE work_items SET state = 'closed',"
                                " close_reason = 'done' WHERE id = %s", (lot,))
        for state in ("merged", "promoted", "closed"):
            self.external_reservation(state)
        self.assertTrue(self.retire()["changed"])

    def test_handing_external_reservation_over_unblocks_only_previous_executor(self):
        lot = self.external_reservation()
        registry.upsert(self.db, "other", harness="codex", host=self.cfg.host,
                        cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET mode = 'externe', responsible = %s"
                        " WHERE name = 'other'", (ALICE,))
        work.set_external_executor(self.db, lot, "other", actor=ALICE, cfg=self.cfg)
        self.assertTrue(self.retire()["changed"])
        with self.assertRaisesRegex(agent_retire.RetireError,
                                    "external executor of lot #%d" % lot):
            self.retire("other")

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

    def unread(self, name="worker"):
        return storage.of(self.db).mailbox.unread(name, 10)

    def test_a_direct_deposit_to_a_retired_agent_is_refused_by_the_storage(self):
        # les notifications système passent par mail.send, sans check_send
        self.retire()
        with self.assertRaises(storage.RecipientRetired) as ctx:
            mail.send(self.db, "ameesh", "worker", "notification système", kind="event")
        self.assertIn("worker is retired", str(ctx.exception))
        self.assertEqual(self.unread(), [])
        # un nom hors du registre (humain, service) reste déposé
        mail.send(self.db, "ameesh", "human:alice", "décision", kind="event")
        self.restore()
        mail.send(self.db, "ameesh", "worker", "de retour", kind="event")
        self.assertEqual([m["body"] for m in self.unread()], ["de retour"])

    def test_a_retirement_between_the_precheck_and_the_deposit_wins(self):
        registry.upsert(self.db, "other", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)
        undeliverable.check_send(self.cfg, self.db, sender="other", dest="worker")
        self.retire()   # committé après le précontrôle
        bk = backend.PgBackend(self.cfg, self.db)
        with self.assertRaises(storage.RecipientRetired):
            bk.send("other", "worker", "bonjour")
        self.assertEqual(self.unread(), [])
        # « all » lu avant le retrait : la diffusion saute le retiré, livre les autres
        stale = [dict(row, retired_ts=None) for row in registry.overview(self.db)]
        with mock.patch.object(backend.registry, "overview", return_value=stale):
            delivered = bk.send("other", "all", "à tous")
        self.assertNotIn("worker", delivered)
        self.assertEqual(self.unread(), [])
        # une copie à un retiré n'est pas déposée ; le message principal l'est
        registry.upsert(self.db, "third", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)
        self.assertEqual(bk.send("other", "third", "avec copie", cc=["worker"]), ["third"])
        self.assertEqual(self.unread(), [])
        self.assertEqual([m["body"] for m in self.unread("third")], ["avec copie"])

    def test_a_stale_precheck_gives_a_readable_refusal_not_a_trace(self):
        from ameesh import cli
        registry.upsert(self.db, "other", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)
        self.retire()
        out, err = io.StringIO(), io.StringIO()
        # le précontrôle a lu l'agent avant son retrait
        with mock.patch.dict(os.environ, self.env(), clear=True), \
                mock.patch.object(undeliverable, "check_send", return_value=[]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["send", "worker", "bonjour", "--from", "other"])
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn("refused: worker is retired", err.getvalue())
        self.assertEqual(self.unread(), [])

    def test_a_deposit_in_progress_holds_the_retirement(self):
        """Le dépôt prend la ligne du registre en partagé ; `retire` la prend
        en exclusif : il attend le commit du dépôt, jamais l'inverse."""
        # délai par connexion (paquet de démarrage) : vaut pour les deux pilotes
        other = self.connect(statement_timeout_ms=500)
        self.addCleanup(other.close)
        with self.db.transaction() as tx:
            mail.send(tx, "ameesh", "worker", "en cours", kind="event")
            with self.assertRaises(db_mod.DbError) as ctx:
                storage.of(other).agents.retire("worker", actor=ALICE, reason="course")
            self.assertIn("statement timeout", str(ctx.exception))
        self.assertIsNone(registry.get(self.db, "worker")["retired_ts"])
        self.assertEqual([m["body"] for m in self.unread()], ["en cours"])

    def test_copy_to_a_retired_agent_is_refused_nothing_deposited(self):
        registry.upsert(self.db, "other", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)
        registry.upsert(self.db, "third", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)
        self.retire()
        out = self.cli("send", "third", "bonjour", "--from", "other", "--cc", "worker")
        self.assertEqual(out.returncode, 2, out.stdout)
        self.assertIn("copy refused — worker is retired", out.stderr)
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox WHERE sender = 'other'")[0]["n"], 0)

    def test_forward_to_a_retired_agent_is_refused_even_under_the_lock(self):
        registry.upsert(self.db, "dead", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET status = 'stopped' WHERE name = 'dead'")
        storage.of(self.db).mailbox.send(
            "ameesh", "dead", "en attente", host=None, kind="notify", payload=None,
            work_item_id=None, signature=None, signature_key=None, signed_payload=None,
            nonce=None, created_us=None, expires_us=None)
        self.retire()
        with self.assertRaises(undeliverable.Refused) as ctx:
            undeliverable.forward(self.cfg, self.db, "dead", "worker", actor=ALICE)
        self.assertIn("worker is retired", str(ctx.exception))
        # le stockage relit le retrait sous le verrou de la ligne : un retrait
        # committé après la lecture d'`undeliverable` gagne encore
        with self.assertRaises(storage.MailboxRefused) as ctx:
            storage.of(self.db).mailbox.forward("dead", "worker", ALICE)
        self.assertIn("worker is retired", str(ctx.exception))
        self.assertEqual(storage.of(self.db).mailbox.unread("worker", 10), [])

    def test_no_watch_nor_wake_for_a_retired_agent(self):
        units = os.path.join(self.tmp, "units")
        os.makedirs(units)
        env = {watch.UNIT_DIR_ENV: units, watch.IDENTITY_ENV: "worker"}
        # un réveil armé avant le retrait
        with open(os.path.join(units, watch.wake_name("worker", "deadline", "wake.json")),
                  "w", encoding="utf-8") as fh:
            json.dump({"agent": "worker", "slug": "deadline", "message": "réveil"}, fh)
        self.retire()
        run = mock.Mock(side_effect=AssertionError("systemd must not be called"))
        with mock.patch.object(watch.platform, "is_linux", return_value=True):
            with self.assertRaisesRegex(watch.WatchError, "worker is retired"):
                watch.enable(self.cfg, self.db, "worker", 1800, run=run, env=env,
                             actor=ALICE)
            with self.assertRaisesRegex(watch.WatchError, "worker is retired"):
                watch.arm(self.cfg, self.db, "worker", "later", 1800, "réveil",
                          run=run, env=env, actor=ALICE)
        with self.assertRaisesRegex(watch.WatchError, "worker is retired"):
            watch.fire(self.cfg, self.db, "worker", "deadline", run=run, env=env)
        self.assertEqual(storage.of(self.db).mailbox.unread("worker", 10), [])
        self.assertEqual(storage.of(self.db).watches.list("worker"), [])

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
        # #4779 : un relecteur doit prouver `propose` et un rôle ; la fiche du
        # test est écrite directement au registre (pas de canon).
        self.db.execute(
            "UPDATE agent_registry SET capabilities ="
            " ARRAY['read','report-drift','propose'], roles = ARRAY['implementation']"
            " WHERE name = 'reviewer'")
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

    def test_a_retired_agent_is_never_assigned(self):
        self.retire()
        lot = work.add(self.db, title="lot", app="ameesh")
        for externe in (False, True):
            with self.assertRaises(work.WorkError) as ctx:
                work.check_assignee(self.db, "worker", externe=externe, cfg=self.cfg)
            self.assertIn("worker is retired", str(ctx.exception))
        with self.assertRaises(work.WorkError):
            work.assign(self.db, lot["id"], "worker")
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_a_retired_agent_is_not_wakeable(self):
        self.assertTrue(registry.wakeable(self.db, self.cfg, "worker")[0])
        self.retire()
        # le marqueur arrive par wake_check : le refus ne dépend pas de
        # canon_claim_ok (qui, lui, ressemble à une attente de canon sync)
        self.assertIsNotNone(
            storage.of(self.db).agents.wake_check("worker")["retired_ts"])
        ok, why, note = registry.wake_status(self.db, self.cfg, "worker")
        self.assertFalse(ok)
        self.assertIn("worker is retired", why)
        self.assertIn("ameesh agent restore worker", why)
        self.assertEqual(note, "")
        self.assertFalse(registry.wakeable(self.db, self.cfg, "worker")[0])
        self.restore()
        self.assertTrue(registry.wakeable(self.db, self.cfg, "worker")[0])

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
