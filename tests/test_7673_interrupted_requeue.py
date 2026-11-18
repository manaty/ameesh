# SPDX-License-Identifier: AGPL-3.0-only
"""#7673 — contrat public 1.4, E2c : remise en attente des demandes d'un tour
interrompu par le cœur, sous garde de bail, et course avec la réponse.

Le banc vérifie ce que le contrat écrit promet
(`docs/api/module-contract-v1.md`, « Requests of an interrupted turn ») :
ensemble exact capturé (ids, consigne runner/epoch/jeton/tour), exclusion des
demandes déjà répondues, consigne en attente (`pending_prompt`) préservée,
aucune double réservation ni double réponse, garde de bail, et la course entre
la remise en attente et `public.mail.reply`.

Postgres réel. Les cas de course ouvrent une connexion par fil : le même
processus ne prouverait rien.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import threading
import unittest
from unittest import mock

from ameesh import db as db_mod, mail, registry, runner as runner_mod, storage
from ameesh.public import mail as public_mail
from ameesh.public.mail import Lease, Reply, ScopeRefused, Submission
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

OWNER = "runner-e2c"
TURN = "turn-e2c-1"


class RequeueBase(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM spend_pending")
        for name in ("alpha", "beta"):
            registry.upsert(self.db, name, harness="codex", host=self.cfg.host)
        lease = registry.claim(self.db, "beta", OWNER, 600)
        self.assertIsNotNone(lease)
        self.epoch = int(lease["lease_epoch"])
        self.lease = Lease(OWNER, self.epoch)

    def request(self, body: str = "question", kind: str = "request") -> int:
        return public_mail.submit(self.db, "alpha", Submission(
            recipient="beta", body=body, kind=kind)).id

    def hold(self, ids, *, turn: str = TURN, marker: bool = True) -> str:
        """Le prompt du tour natif : réservé, marqueur posé, soldé au lancement."""
        token = mail.new_token()
        reserved = mail.reserve(self.db, "beta", OWNER, self.epoch, token,
                                ids=list(ids), porteur="consigne", turn_id=turn)
        self.assertEqual(sorted(int(r["id"]) for r in reserved), sorted(ids))
        if marker:
            self.assertIsNotNone(registry.pending_spend_begin(
                self.db, "beta", 0, "consigne", "model-e2c", turn_id=turn))
        self.assertEqual(
            mail.deliver(self.db, "beta", OWNER, self.epoch, token, list(ids)),
            sorted(ids))
        return token

    def requeue(self, token: str, ids, *, db=None, turn: str | None = TURN,
                owner: str = OWNER, epoch: int | None = None) -> dict:
        return mail.requeue_interrupted(
            db or self.db, "beta", owner, self.epoch if epoch is None else epoch,
            token, turn, list(ids))

    def row(self, message_id: int) -> dict:
        return mail.get(self.db, message_id)

    def assert_queued(self, message_id: int) -> None:
        row = self.row(message_id)
        self.assertIsNone(row["delivered_ts"])
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["consigne_runner"])
        self.assertTrue(mail.maybe_redelivered(row))

    def assert_delivered(self, message_id: int) -> None:
        row = self.row(message_id)
        self.assertIsNotNone(row["delivered_ts"])
        self.assertEqual(row["status"], "delivered")


class RequeueContractTest(RequeueBase):
    def test_interrupted_request_is_queued_again_and_flagged(self):
        rid = self.request()
        token = self.hold([rid])
        out = self.requeue(token, [rid])
        self.assertEqual(out, {"guard": "held", "requeued": [rid], "replied": []})
        self.assert_queued(rid)
        self.assertEqual(
            self.db.query("SELECT meta->'remise_interrompue'->>'turn' AS turn_id"
                          " FROM agent_mailbox WHERE id = %s", (rid,))[0]["turn_id"], TURN)
        # le tour suivant le réserve UNE fois, « peut-être déjà vu »
        again = mail.reserve(self.db, "beta", OWNER, self.epoch, mail.new_token(),
                             porteur="consigne", turn_id="turn-e2c-2")
        self.assertEqual([int(r["id"]) for r in again], [rid])
        self.assertTrue(again[0]["deja_consigne"])
        self.assertEqual(mail.reserve(self.db, "beta", OWNER, self.epoch,
                                      mail.new_token(), porteur="consigne"), [])

    def test_only_the_exact_captured_set_moves(self):
        served = self.request("served")
        other_turn = self.request("other turn")
        not_captured = self.request("same turn, not captured")
        notify = self.request("notify", kind="notify")
        token = self.hold([served, not_captured, notify])
        other_token = mail.new_token()
        mail.reserve(self.db, "beta", OWNER, self.epoch, other_token,
                     ids=[other_turn], porteur="consigne", turn_id="turn-other")
        mail.deliver(self.db, "beta", OWNER, self.epoch, other_token, [other_turn])
        # ids given: the served request, the notify and the other turn's
        # request; only the request carrying this token AND turn moves
        out = self.requeue(token, [served, notify, other_turn])
        self.assertEqual(out["requeued"], [served])
        self.assert_queued(served)
        for kept in (other_turn, not_captured, notify):
            self.assert_delivered(kept)
        # another token or another turn id changes nothing
        rid = self.request("fresh")
        token2 = self.hold([rid], turn="turn-e2c-2")
        self.assertEqual(self.requeue(token, [rid], turn="turn-e2c-2")["requeued"], [])
        self.assertEqual(self.requeue(token2, [rid], turn=TURN)["requeued"], [])
        self.assert_delivered(rid)

    def test_already_replied_request_stays_answered(self):
        answered = self.request("answered")
        open_ = self.request("open")
        token = self.hold([answered, open_])
        receipt = public_mail.reply(self.db, "beta", answered, Reply(body="done"),
                                    lease=self.lease)
        out = self.requeue(token, [answered, open_])
        self.assertEqual(out["requeued"], [open_])
        self.assertEqual(out["replied"], [answered])
        self.assert_delivered(answered)
        self.assert_queued(open_)
        self.assertEqual(self.row(receipt.id)["in_reply_to"], answered)

    def test_withdrawn_request_is_not_requeued(self):
        rid = self.request()
        token = self.hold([rid])
        self.db.execute("UPDATE agent_mailbox SET meta = meta || '{\"retire\": true}'"
                        " WHERE id = %s", (rid,))
        self.assertEqual(self.requeue(token, [rid])["requeued"], [])
        self.assert_delivered(rid)

    def test_pending_prompt_is_preserved(self):
        rid = self.request()
        token = self.hold([rid])
        registry.set_pending_prompt(self.db, "beta", "unrelated instruction")
        before = registry.get(self.db, "beta")
        self.requeue(token, [rid])
        after = registry.get(self.db, "beta")
        for field in ("pending_prompt", "pending_prompt_id", "current_prompt",
                      "status", "lease_owner", "lease_epoch"):
            self.assertEqual(after[field], before[field], field)

    def test_marker_and_ledger_are_not_touched(self):
        rid = self.request()
        token = self.hold([rid])
        marker = registry.pending_spend_get(self.db, "beta")
        self.requeue(token, [rid])
        self.assertEqual(registry.pending_spend_get(self.db, "beta"), marker)

    def test_requeued_request_is_no_longer_pending_and_refuses_a_late_reply(self):
        rid = self.request()
        token = self.hold([rid])
        self.assertEqual([p.id for p in public_mail.pending(
            self.db, "beta", self.lease).entries], [rid])
        self.requeue(token, [rid])
        self.assertEqual(public_mail.pending(self.db, "beta", self.lease).entries, ())
        with self.assertRaises(ScopeRefused):
            public_mail.reply(self.db, "beta", rid, Reply(body="late"),
                              lease=self.lease)
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox WHERE in_reply_to = %s",
            (rid,))[0]["n"], 0)

    def test_second_requeue_is_a_no_op(self):
        rid = self.request()
        token = self.hold([rid])
        self.assertEqual(self.requeue(token, [rid])["requeued"], [rid])
        self.assertEqual(self.requeue(token, [rid])["requeued"], [])
        self.assert_queued(rid)


class LeaseGuardTest(RequeueBase):
    def test_expired_but_unclaimed_lease_is_still_ours(self):
        rid = self.request()
        token = self.hold([rid])
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 minute'"
                        " WHERE name = 'beta'")
        self.assertEqual(self.requeue(token, [rid])["guard"], "held")
        self.assert_queued(rid)

    def test_lease_released_by_the_owner_without_reclaim(self):
        rid = self.request()
        token = self.hold([rid])
        self.assertTrue(registry.release(self.db, "beta", OWNER, self.epoch))
        out = self.requeue(token, [rid])
        self.assertEqual((out["guard"], out["requeued"]), ("released", [rid]))
        self.assert_queued(rid)

    def test_lease_taken_by_another_holder_refuses(self):
        rid = self.request()
        token = self.hold([rid])
        self.assertTrue(registry.release(self.db, "beta", OWNER, self.epoch))
        self.assertIsNotNone(registry.claim(self.db, "beta", "runner-next", 600))
        out = self.requeue(token, [rid])
        self.assertEqual((out["guard"], out["requeued"]), ("refused", []))
        self.assert_delivered(rid)

    def test_a_stale_epoch_or_another_owner_refuses(self):
        rid = self.request()
        token = self.hold([rid])
        self.assertEqual(self.requeue(token, [rid], epoch=self.epoch - 1)["guard"],
                         "refused")
        self.assertEqual(self.requeue(token, [rid], owner="intruder")["guard"],
                         "refused")
        self.assert_delivered(rid)

    def test_unknown_agent_and_empty_set(self):
        self.assertEqual(self.requeue("t", [])["guard"], "missing")
        self.assertEqual(storage.of(self.db).mailbox.requeue_interrupted(
            "nobody", OWNER, 1, "t", None, [1])["guard"], "missing")


class ReplyRaceTest(RequeueBase):
    def setUp(self) -> None:
        super().setUp()
        self.rid = self.request()
        self.token = self.hold([self.rid])

    def test_a_reply_committed_while_requeue_waits_is_seen(self):
        """La réponse tient le verrou du registre ; la remise attend, puis son
        instantané (pris APRÈS le verrou) voit la réponse : la demande reste
        répondue, rien n'est remis en attente."""
        holder = self.connect()
        self.addCleanup(holder.close)
        outcome: dict = {}
        with holder.transaction() as tx:
            ok, reason, _row = registry.lease_agent_locked(tx, "beta", OWNER, self.epoch)
            self.assertTrue(ok, reason)
            mail.send(tx, "beta", "alpha", "answer", kind="reply",
                      in_reply_to=self.rid)

            def requeue() -> None:
                other = self.connect()
                try:
                    outcome.update(self.requeue(self.token, [self.rid], db=other))
                finally:
                    other.close()

            thread = threading.Thread(target=requeue)
            thread.start()
            thread.join(timeout=0.5)
            self.assertTrue(thread.is_alive(), "requeue did not wait for the lease lock")
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual((outcome["requeued"], outcome["replied"]), ([], [self.rid]))
        self.assert_delivered(self.rid)

    def test_a_reply_after_the_requeue_is_refused(self):
        holder = self.connect()
        self.addCleanup(holder.close)
        errors: list = []
        with holder.transaction() as tx:
            # la remise tient le verrou du registre (même ordre que la vraie)
            tx.query("SELECT 1 FROM agent_registry WHERE name = 'beta' FOR UPDATE")

            def answer() -> None:
                other = self.connect()
                try:
                    public_mail.reply(other, "beta", self.rid, Reply(body="late"),
                                      lease=self.lease)
                except BaseException as exc:  # noqa: BLE001 — inspected below
                    errors.append(exc)
                finally:
                    other.close()

            thread = threading.Thread(target=answer)
            thread.start()
            thread.join(timeout=0.5)
            self.assertTrue(thread.is_alive(), "reply did not wait for the lease lock")
            out = self.requeue(self.token, [self.rid], db=tx)
            self.assertEqual(out["requeued"], [self.rid])
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ScopeRefused)
        self.assert_queued(self.rid)

    def test_concurrent_reply_and_requeue_never_end_both(self):
        """Course libre, répétée : à la fin, la demande est SOIT répondue et
        remise (jamais), soit répondue, soit remise en attente sans réponse."""
        for round_ in range(8):
            rid = self.rid if round_ == 0 else self.request("q%d" % round_)
            token = self.token if round_ == 0 else self.hold([rid], turn="t%d" % round_)
            turn = TURN if round_ == 0 else "t%d" % round_
            barrier = threading.Barrier(2)
            results: dict = {}

            def answer() -> None:
                db = self.connect()
                try:
                    barrier.wait(timeout=10)
                    public_mail.reply(db, "beta", rid, Reply(body="a"), lease=self.lease)
                    results["reply"] = "ok"
                except ScopeRefused:
                    results["reply"] = "refused"
                finally:
                    db.close()

            def requeue() -> None:
                db = self.connect()
                try:
                    barrier.wait(timeout=10)
                    results["requeue"] = self.requeue(token, [rid], db=db, turn=turn)
                finally:
                    db.close()

            threads = [threading.Thread(target=answer), threading.Thread(target=requeue)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive())
            replies = self.db.query(
                "SELECT count(*) AS n FROM agent_mailbox WHERE in_reply_to = %s",
                (rid,))[0]["n"]
            requeued = rid in results["requeue"]["requeued"]
            self.assertLessEqual(replies, 1)
            self.assertNotEqual(bool(replies), requeued, "round %d: %r" % (round_, results))
            self.assertEqual(results["reply"] == "ok", bool(replies))


class RunnerInterruptionTest(PgTestCase):
    """Le tour réel de l'exécuteur : la remise en attente part de la clôture
    du courrier, seulement quand le cœur a coupé le tour."""

    def _worker(self, name: str):
        cfg = dataclasses.replace(self.cfg, interrupt_senders=())
        runner = Runner(cfg, self.db, once=True)
        runner.event_coalesce = 0.0
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def _env(self, **extra):
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=False)

    def _at_launch(self, action):
        real = subprocess.Popen

        def popen(*args, **kwargs):
            if kwargs.get("start_new_session"):  # le harnais, pas le pilote psql
                action()
            return real(*args, **kwargs)
        return mock.patch.object(runner_mod.subprocess, "Popen", side_effect=popen)

    def test_shutdown_before_popen_publication_requeues_the_request(self):
        """L'arrêt arrive pendant le lancement, avant la publication du
        `Popen` : le harnais est tué au recontrôle, la remise a pu être
        soldée ; la demande repart en attente, la notification non, et la
        consigne en attente posée pendant le tour reste intacte."""
        runner, worker = self._worker("arret")
        rid = mail.send(self.db, "orch", "arret", "à rejouer", kind="request")
        nid = mail.send(self.db, "orch", "arret", "simple avis")
        spec = worker.pick()
        self.assertEqual(sorted(spec["ids"]), sorted([rid, nid]))

        def shutdown_now():
            registry.set_pending_prompt(self.db, "arret", "consigne sans rapport")
            worker.stopping.set()
            runner.stop.set()

        with self._env(), self._at_launch(shutdown_now):
            self.assertFalse(worker.run_turn(spec))
        row = mail.get(self.db, rid)
        self.assertIsNone(row["delivered_ts"])
        self.assertIsNone(row["consigne_runner"])
        self.assertTrue(mail.maybe_redelivered(row))
        self.assertEqual([int(r["id"]) for r in mail.unread(self.db, "arret")], [rid])
        self.assertEqual(registry.get(self.db, "arret")["pending_prompt"],
                         "consigne sans rapport")

    def test_harness_failure_does_not_requeue(self):
        _runner, worker = self._worker("echec")
        rid = mail.send(self.db, "orch", "echec", "fait échouer", kind="request")
        spec = worker.pick()
        with self._env(AMEESH_TEST_EXIT="3"):
            self.assertFalse(worker.run_turn(spec))
        self.assertIsNotNone(mail.get(self.db, rid)["delivered_ts"])
        self.assertEqual(mail.unread(self.db, "echec"), [])

    def test_preemption_requeues_and_completed_turn_does_not(self):
        _runner, worker = self._worker("preempte")
        rid = mail.send(self.db, "orch", "preempte", "coupée", kind="request")
        spec = worker.pick()
        with self._env(), self._at_launch(worker.preempting.set):
            self.assertFalse(worker.run_turn(spec))
        self.assertIsNone(mail.get(self.db, rid)["delivered_ts"])
        worker.preempting.clear()
        spec = worker.pick()
        self.assertEqual(spec["ids"], [rid])
        self.assertIn("re-delivered", spec["prompt"])
        with self._env():
            self.assertTrue(worker.run_turn(spec))
        self.assertIsNotNone(mail.get(self.db, rid)["delivered_ts"])

    def test_mediated_executor_does_not_requeue(self):
        runner, worker = self._worker("medie")
        rid = mail.send(self.db, "orch", "medie", "reste remise", kind="request")
        token = mail.new_token()
        mail.reserve(self.db, "medie", runner.runner_id, worker.epoch, token,
                     ids=[rid], porteur="consigne", turn_id="t")
        mail.deliver(self.db, "medie", runner.runner_id, worker.epoch, token, [rid])
        worker.stopping.set()
        with mock.patch.object(runner, "mediated", True, create=True):
            worker._requeue_interrupted({"turn_id": "t"}, token, [rid])
        self.assertIsNotNone(mail.get(self.db, rid)["delivered_ts"])
        worker._requeue_interrupted({"turn_id": "t"}, token, [rid])
        self.assertIsNone(mail.get(self.db, rid)["delivered_ts"])

    def test_database_error_is_logged_not_raised(self):
        _runner, worker = self._worker("panne")
        worker.stopping.set()
        with mock.patch.object(mail, "requeue_interrupted",
                               side_effect=db_mod.DbError("down")):
            worker._requeue_interrupted({"turn_id": "t"}, "tok", [1])


if __name__ == "__main__":
    unittest.main()
