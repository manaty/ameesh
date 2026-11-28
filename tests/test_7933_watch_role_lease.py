# SPDX-License-Identifier: AGPL-3.0-only
"""#7933 : la veille d'un rôle élu suit le bail, et une veille muette alerte.

Le constat (pilote, 20/11) : la veille de l'orchestrateur était inscrite sur
`ameesh-agents-2`, le bail du rôle sur `ameesh-agents-1`. AG2 sortait en
attente (`role run` : `standby (other-host)`), AG1 appelait `watch send` mais
la ligne `agent_watches.host` désignait AG2 : « the watch of orchestrateur is
on ameesh-agents-2: nothing sent from here ». Douze jours sans consigne, sans
alerte.

Témoins : le détenteur du bail émet même si l'inscription nomme un autre hôte
(et prend la ligne à son nom, dans la transaction du message) ; un hôte qui ne
tient pas le bail n'émet jamais ; une relève déplace l'émetteur sans
réinscription ; une époque périmée est refusée par la garde de transaction
(jeton de garde, un seul émetteur par époque) ; une veille activée qui n'a
rien déposé au-delà de N périodes alerte, sauf agent arrêté ou retiré. L'unité
d'un rôle élu passe par `role run`, pour porter l'époque au tir.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ameesh import db as db_mod
from ameesh import exploitation, role_run, role_timers, storage, watch

from .support import PgTestCase, launcher_home

NAME = "gov-7933"
EVERY = 1800


class WatchRoleLeaseTest(PgTestCase):
    def setUp(self) -> None:
        self.db.execute("DELETE FROM agent_mailbox")
        self.db.execute("DELETE FROM agent_watches")
        self.register(NAME, "claude")
        self.db.execute(
            "UPDATE agent_registry SET releve = 'election',"
            " candidats = ARRAY['banc','atelier'], responsible = 'human:owner',"
            " status = 'idle', status_since = now() - interval '1 hour',"
            " last_turn_at = now() - interval '1 hour' WHERE name = %s", (NAME,))
        self.banc = dataclasses.replace(self.cfg, host="banc")
        self.atelier = dataclasses.replace(self.cfg, host="atelier")
        self.now = time.time()
        self.env = {watch.UNIT_DIR_ENV: tempfile.mkdtemp(prefix="ameesh-7933-"),
                    "PATH": "/usr/bin:/bin",
                    "HOME": launcher_home("ameesh-7933-home-")}
        self.addCleanup(shutil.rmtree, self.env[watch.UNIT_DIR_ENV], True)
        self.linger = "yes"
        self.calls = []
        patcher = mock.patch.object(watch.platform, "is_linux", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_(self, argv, **kw):
        if argv[0] == "loginctl":
            return mock.Mock(returncode=0, stdout=self.linger + "\n", stderr="")
        self.calls.append(argv[2:])
        return mock.Mock(returncode=0, stdout="", stderr="")

    def put(self, host="atelier"):
        storage.of(self.db).watches.put(NAME, EVERY, host, actor="human:owner")

    def hold(self, host, epoch=7, expires_s=300):
        self.db.execute(
            "UPDATE agent_registry SET host = %s, lease_owner = 'runner-test',"
            " lease_epoch = %s, lease_expires_at = clock_timestamp()"
            " + make_interval(secs => %s) WHERE name = %s",
            (host, epoch, expires_s, NAME))

    def veille(self):
        return [m for m in storage.of(self.db).mailbox.unread(NAME, 200)
                if m["sender"] == "veille"]

    def send(self, cfg, now=None):
        with mock.patch.object(watch.fil, "record"):
            return watch.send(cfg, self.db, NAME,
                              now=self.now if now is None else now)

    # -- émission ---------------------------------------------------------
    def test_the_lease_holder_emits_even_when_recorded_elsewhere(self):
        self.put("atelier")
        self.hold("banc")
        self.assertIn("instruction sent to %s" % NAME, self.send(self.banc))
        [row] = storage.of(self.db).watches.list(NAME)
        # the holder took the row over in the message's transaction
        self.assertEqual(row["host"], "banc")
        self.assertEqual(len(self.veille()), 1)
        # the host recorded at enable time does not emit: it does not hold
        # the lease, and no epoch of its own authorizes it
        out = self.send(self.atelier)
        self.assertIn("held by banc", out)
        self.assertEqual(len(self.veille()), 1)

    def test_a_handover_moves_the_emitter(self):
        self.put("banc")
        self.hold("banc", epoch=7)
        self.assertIn("instruction sent to %s" % NAME, self.send(self.banc))
        self.hold("atelier", epoch=8)
        # the old holder stands by as soon as the lease moves
        self.assertIn("held by atelier", self.send(self.banc))
        self.assertEqual(len(self.veille()), 1)
        # the new holder sends on its next firing, without re-enabling
        self.assertIn("instruction sent to %s" % NAME, self.send(self.atelier))
        [row] = storage.of(self.db).watches.list(NAME)
        self.assertEqual(row["host"], "atelier")
        self.assertEqual(len(self.veille()), 2)
        # and the old host does not send a second copy
        self.assertIn("held by atelier", self.send(self.banc))
        self.assertEqual(len(self.veille()), 2)

    def test_a_stale_epoch_never_emits(self):
        # a unit started by `role run` under epoch 6 while the lease is at 7:
        # the transaction fence refuses the message and keeps the row
        self.put("atelier")
        self.hold("banc", epoch=7)
        with mock.patch.dict(os.environ, {role_run.ENV_ROLE: NAME,
                                          role_run.ENV_EPOCH: "6"}):
            out = self.send(self.banc)
        self.assertIn("lease moved", out)
        self.assertEqual(self.veille(), [])
        self.assertEqual(storage.of(self.db).watches.list(NAME)[0]["host"], "atelier")

    def test_a_plain_non_elected_watch_keeps_the_host_rule(self):
        self.db.execute("UPDATE agent_registry SET releve = 'hote', host = 'banc'"
                        " WHERE name = %s", (NAME,))
        self.put("atelier")
        out = self.send(self.banc)
        self.assertIn("is on atelier", out)
        self.assertEqual(self.veille(), [])

    # -- alerte de muette ------------------------------------------------
    def test_a_mute_watch_alerts(self):
        self.put("atelier")
        self.hold("banc")
        limit = exploitation.watch_silence_limit(EVERY)
        since = self.now - limit - 60
        self.db.execute(
            "UPDATE agent_watches SET enabled_at = to_timestamp(%s),"
            " last_sent_at = to_timestamp(%s) WHERE agent = %s",
            (since - 60, since, NAME))
        out = [a for a in exploitation.alerts(self.cfg, self.db, now=self.now,
                                              local_pool=False)
               if a["type"] == "watch_silent"]
        self.assertEqual(len(out), 1)
        alert = out[0]
        self.assertEqual(alert["agent"], NAME)
        self.assertEqual(alert["host"], "atelier")
        self.assertGreaterEqual(alert["value"], alert["threshold"])
        self.assertEqual(alert["threshold"], 3)
        self.assertIn("lease holder banc", alert["detail"])
        # the same alert while the silence lasts: a growing `value` does not
        # open a new episode (`since` is the last send)
        again = [a for a in exploitation.alerts(self.cfg, self.db,
                                                now=self.now + EVERY,
                                                local_pool=False)
                 if a["type"] == "watch_silent"]
        self.assertEqual([(a["since"], a["value"]) for a in again],
                         [(alert["since"], alert["value"] + 1)])

    def test_a_fresh_send_or_a_stopped_agent_is_not_mute(self):
        self.put("atelier")
        self.hold("banc")
        old = self.now - exploitation.watch_silence_limit(EVERY) - 60
        self.db.execute("UPDATE agent_watches SET last_sent_at = to_timestamp(%s)"
                        " WHERE agent = %s", (old, NAME))
        self.db.execute("UPDATE agent_registry SET status = 'stopped' WHERE name = %s",
                        (NAME,))
        self.assertEqual([a for a in exploitation.alerts(
            self.cfg, self.db, now=self.now, local_pool=False)
            if a["type"] == "watch_silent"], [])
        # retired: never woken again, expected silence
        self.db.execute("UPDATE agent_registry SET status = 'idle',"
                        " retired_at = now() WHERE name = %s", (NAME,))
        self.assertEqual([a for a in exploitation.alerts(
            self.cfg, self.db, now=self.now, local_pool=False)
            if a["type"] == "watch_silent"], [])
        # a fresh deposit resolves it
        self.db.execute("UPDATE agent_registry SET retired_at = NULL WHERE name = %s",
                        (NAME,))
        self.db.execute("UPDATE agent_watches SET last_sent_at = to_timestamp(%s)"
                        " WHERE agent = %s", (self.now, NAME))
        self.assertEqual([a for a in exploitation.alerts(
            self.cfg, self.db, now=self.now, local_pool=False)
            if a["type"] == "watch_silent"], [])

    # -- battement des tirs qui ne déposent rien (B1) ---------------------
    def watched(self):
        return storage.of(self.db).watches.list(NAME)[0]

    def watch_silent(self, now=None):
        return [a for a in exploitation.alerts(
            self.cfg, self.db, now=time.time() if now is None else now,
            local_pool=False) if a["type"] == "watch_silent"]

    def test_a_not_due_tick_beats_without_moving_the_cadence(self):
        self.put("banc")
        self.hold("banc")
        self.assertIn("instruction sent to %s" % NAME, self.send(self.banc))
        before = self.watched()
        out = self.send(self.banc, now=time.time() + watch.ROLE_TICK_S)
        self.assertIn("next instruction", out)
        after = self.watched()
        self.assertEqual(after["last_fired_reason"], "not-due")
        self.assertEqual(after["last_attempt_ts"], before["last_attempt_ts"])
        self.assertGreaterEqual(after["last_fired_ts"], before["last_fired_ts"])
        # the heartbeat keeps a healthy watch out of `watch_silent` even when
        # its last deposit is older than the limit
        old = self.now - exploitation.watch_silence_limit(EVERY) - 60
        self.db.execute("UPDATE agent_watches SET last_sent_at = to_timestamp(%s)"
                        " WHERE agent = %s", (old, NAME))
        self.assertEqual(self.watch_silent(), [])

    def test_a_recent_turn_skip_beats(self):
        self.put("banc")
        self.hold("banc")
        self.send(self.banc)
        before = self.watched()
        later = time.time() + EVERY + 10
        self.db.execute("UPDATE agent_registry SET last_turn_at = to_timestamp(%s)"
                        " WHERE name = %s", (later - 60, NAME))
        out = self.send(self.banc, now=later)
        self.assertIn("turn within", out)
        row = self.watched()
        self.assertEqual(row["last_fired_reason"], "recent-turn")
        self.assertGreaterEqual(row["last_attempt_ts"], before["last_attempt_ts"])
        old = self.now - exploitation.watch_silence_limit(EVERY) - 60
        self.db.execute("UPDATE agent_watches SET last_sent_at = to_timestamp(%s)"
                        " WHERE agent = %s", (old, NAME))
        self.assertEqual(self.watch_silent(), [])

    def test_an_unread_note_skip_beats(self):
        self.put("banc")
        self.hold("banc")
        self.send(self.banc)  # deposits an unread instruction
        out = self.send(self.banc, now=time.time() + EVERY + 10)
        self.assertIn("not read yet", out)
        row = self.watched()
        self.assertEqual(row["last_fired_reason"], "unread-note")
        # a beat after the last deposit keeps the alert quiet
        old = self.now - exploitation.watch_silence_limit(EVERY) - 60
        self.db.execute("UPDATE agent_watches SET last_sent_at = to_timestamp(%s)"
                        " WHERE agent = %s", (old, NAME))
        self.assertEqual(self.watch_silent(), [])
        # the same row without the beat is mute: the alert still measures the
        # most recent firing, not the agent's turns
        self.db.execute(
            "UPDATE agent_watches SET enabled_at = to_timestamp(%s),"
            " last_fired_at = to_timestamp(%s), last_attempt_at = to_timestamp(%s)"
            " WHERE agent = %s", (old - 60, old, old, NAME))
        self.assertEqual(len(self.watch_silent()), 1)

    # -- compatibilité N-1 (ameesh-cx2) ----------------------------------
    def test_an_n1_writer_row_is_read_and_a_recent_send_is_not_mute(self):
        added = ("last_fired_at", "last_fired_reason", "last_attempt_at")
        # the N-1 `put` did not know the three columns
        self.previous_insert(
            "agent_watches",
            {"agent": NAME, "every_s": EVERY, "host": "atelier",
             "enabled_by": "human:owner"}, added=added)
        # the N-1 `deliver` wrote only last_sent_at
        self.previous_write(
            "update agent_watches set last_sent_at = now() where agent = %s",
            (NAME,), added=added)
        row = self.watched()
        self.assertIsNone(row["last_fired_ts"])
        self.assertIsNone(row["last_fired_reason"])
        self.assertIsNone(row["last_attempt_ts"])
        # a recent N-1 deposit still keeps the alert quiet, even without a beat
        self.assertEqual(self.watch_silent(), [])
        # the new code accepts the N-1 row and populates the new columns when
        # the lease holder emits (no re-registration, no backfill)
        self.hold("banc")
        self.assertIn("instruction sent to %s" % NAME, self.send(self.banc))
        row = self.watched()
        self.assertEqual(row["host"], "banc")
        self.assertEqual(row["last_fired_reason"], "sent")
        self.assertIsNotNone(row["last_fired_ts"])
        self.assertIsNotNone(row["last_attempt_ts"])

    # -- concurrence bail / dépôt (ameesh-cx2) ---------------------------
    def test_a_handover_waits_for_the_deposit(self):
        self.put("atelier")
        self.hold("banc", epoch=7)
        started, release = threading.Event(), threading.Event()
        fire_db = self.connect()
        result: dict = {}
        deposits: list = []

        def deposit(tx):
            deposits.append(tx)
            started.set()
            self.assertTrue(release.wait(10))

        def fire():
            try:
                result["row"] = storage.of(fire_db).watches.deliver(
                    NAME, "banc", deposit, takeover=True, epoch=7)
            except Exception as exc:  # noqa: BLE001 — reported to the test
                result["error"] = exc

        thread = threading.Thread(target=fire)
        thread.start()
        self.assertTrue(started.wait(10))
        # a handover trying to commit between the epoch check and the deposit
        # waits on the registry row lock; it cannot interleave
        mover = self.connect(statement_timeout_ms=400)
        try:
            with self.assertRaises(db_mod.DbError):
                mover.execute(
                    "UPDATE agent_registry SET lease_owner = 'next', lease_epoch = 8,"
                    " lease_expires_at = clock_timestamp() + interval '300 seconds'"
                    " WHERE name = %s", (NAME,))
        finally:
            mover.close()
        release.set()
        thread.join(20)
        fire_db.close()
        self.assertNotIn("error", result)
        self.assertTrue(result["row"]["sent"])
        # the deposit is committed; the handover now goes through and the
        # watch follows the new epoch without a second copy
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'next', lease_epoch = 8,"
            " lease_expires_at = clock_timestamp() + interval '300 seconds'"
            " WHERE name = %s", (NAME,))
        self.assertEqual(len(deposits), 1)

    # -- réconciliation par les minuteries de rôle (ameesh-cx2) ----------
    def test_role_timers_install_reconciles_the_watch_without_enable(self):
        self.put("atelier")  # the row keeps the host of the original enable
        self.hold("banc")
        directory = Path(self.env[watch.UNIT_DIR_ENV])
        report = {"schema": "ameesh-role-timers/1", "host": "banc",
                  "role": NAME, "releve": "election",
                  "candidates": ["banc", "atelier"], "candidate": True,
                  "observation": False, "auditor": False, "timers": [],
                  "missing_projects": []}
        result = role_timers.install(
            report, directory=directory, bin_path="/opt/ameesh/bin/ameesh",
            apply=False, db=self.db)
        timer = directory / watch.unit_name(NAME, "timer")
        service = directory / watch.unit_name(NAME, "service")
        self.assertIn(timer.name, result["installed"])
        self.assertIn("OnUnitActiveSec=60\n", timer.read_text(encoding="utf-8"))
        self.assertIn("role run %s --" % NAME, service.read_text(encoding="utf-8"))
        # installation did not re-enable the watch: its row still names atelier
        self.assertEqual(self.watched()["host"], "atelier")
        self.assertEqual(role_timers.check(
            report, directory=directory, apply=False,
            bin_path="/opt/ameesh/bin/ameesh", db=self.db), [])
        # a candidate without its unit is a named gap
        timer.unlink()
        problems = role_timers.check(
            report, directory=directory, apply=False,
            bin_path="/opt/ameesh/bin/ameesh", db=self.db)
        self.assertTrue(any("missing %s" % timer.name in problem
                            for problem in problems), problems)
        # and the holder still emits from that unit, without `watch enable`
        self.assertIn("instruction sent to %s" % NAME, self.send(self.banc))

    def test_an_unreadable_watch_storage_does_not_block_the_role_timers(self):
        # #5947: the watch reconciliation is a complement of the deployment
        # gesture. A database that cannot serve the watch rows (N-1 schema
        # during an upgrade, missing grant, simulated base) names the failure
        # but does not fail the pose of the role timers.
        directory = Path(self.env[watch.UNIT_DIR_ENV])
        report = {"schema": "ameesh-role-timers/1", "host": "banc",
                  "role": NAME, "releve": "election", "candidate": True,
                  "observation": False, "auditor": False,
                  "timers": [{"name": "coherence", "only": "coherence",
                              "interval_s": 21600, "description": "cohérence"}],
                  "missing_projects": []}
        lines = []
        with mock.patch.object(watch, "reconcile_host",
                               side_effect=RuntimeError("base simulée, watches non servi")):
            result = role_timers.install(
                report, directory=directory, bin_path="/opt/ameesh/bin/ameesh",
                apply=False, db=object(), out=lines.append)
        self.assertTrue((directory / "ameesh-role-coherence.timer").is_file())
        self.assertIn("ameesh-role-coherence.timer", result["installed"])
        self.assertIn("RuntimeError", result["watch_error"])
        self.assertTrue(any("not reconciled" in line for line in lines), lines)

    def test_an_unreadable_watch_storage_is_a_named_gap_in_the_check(self):
        # #5947: the control cannot prove the watch without the rows; it stays
        # red, with a named gap instead of a traceback.
        directory = Path(self.env[watch.UNIT_DIR_ENV])
        report = {"schema": "ameesh-role-timers/1", "host": "banc",
                  "role": NAME, "candidate": True, "observation": False,
                  "auditor": False, "timers": [], "missing_projects": []}
        with mock.patch.object(watch, "check_host",
                               side_effect=RuntimeError("base simulée, watches non servi")):
            problems = role_timers.check(
                report, directory=directory, apply=False,
                bin_path="/opt/ameesh/bin/ameesh", db=object())
        self.assertTrue(any("unreadable watch storage" in problem
                            for problem in problems), problems)

    # -- unité et affichage ----------------------------------------------
    def test_enable_of_an_elected_role_wraps_the_unit_and_names_the_holder(self):
        self.hold("banc")
        with mock.patch.object(watch.fil, "record") as note:
            warnings = watch.enable(self.banc, self.db, NAME, EVERY,
                                    run=self.run_, env=self.env,
                                    actor="human:owner")
        self.assertEqual(warnings, [])
        service = os.path.join(self.env[watch.UNIT_DIR_ENV],
                               watch.unit_name(NAME, "service"))
        with open(service, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("role run %s --" % NAME, text)
        self.assertIn("watch send --agent %s" % NAME, text)
        self.assertIn("elected role %s" % NAME, note.call_args.kwargs["text"])
        # the shown line names the lease holder, not only the recorded host
        [row] = watch.show(self.db, NAME)
        self.assertEqual(row["elected"]["holder"], "banc")
        line = "\n".join(watch.format_show([row], self.now))
        self.assertIn("lease holder banc", line)

    def test_enable_on_another_host_of_an_elected_role_does_not_move_the_watch(self):
        self.put("atelier")
        with mock.patch.object(watch.fil, "record"):
            warnings = watch.enable(self.banc, self.db, NAME, EVERY,
                                    run=self.run_, env=self.env,
                                    actor="human:owner")
        self.assertEqual(len(warnings), 1)
        self.assertIn("follows the elected role", warnings[0])
        self.assertNotIn("sends nothing more", warnings[0])


if __name__ == "__main__":
    unittest.main()
