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
import time
import unittest
from unittest import mock

from ameesh import exploitation, registry, role_run, storage, watch

from .support import PgTestCase

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
                    "PATH": "/usr/bin:/bin"}
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

    def send(self, cfg):
        with mock.patch.object(watch.fil, "record"):
            return watch.send(cfg, self.db, NAME, now=self.now)

    # -- émission ---------------------------------------------------------
    def test_the_lease_holder_emits_even_when_recorded_elsewhere(self):
        self.put("atelier")
        self.hold("banc")
        self.assertEqual(self.send(self.banc), "instruction sent to %s" % NAME)
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
        self.assertEqual(self.send(self.banc), "instruction sent to %s" % NAME)
        self.hold("atelier", epoch=8)
        # the old holder stands by as soon as the lease moves
        self.assertIn("held by atelier", self.send(self.banc))
        self.assertEqual(len(self.veille()), 1)
        # the new holder sends on its next firing, without re-enabling
        self.assertEqual(self.send(self.atelier), "instruction sent to %s" % NAME)
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
        self.assertGreaterEqual(alert["threshold"], 13)
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
