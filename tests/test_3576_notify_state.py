# SPDX-License-Identifier: AGPL-3.0-only
"""#3576 : l'état de livraison des alertes est partagé entre les hôtes.

La passe `alerts` suit le bail élu (#3383) : au changement d'hôte, le nouveau
détenteur repartait d'un `state.json` local vide, re-poussait chaque alerte
active (décision #138918 poussée deux fois, 12:41Z puis 13:54Z le 25/10) et
perdait ou annonçait en double les résolutions de l'ancien. Le document et son
bail d'exécution vivent désormais dans `notify_state` (migration 0487).

Témoins : deux hôtes successifs ne poussent qu'une fois, la résolution part
une seule fois du second, un hôte en veille ne livre pas, et un détenteur
dont l'epoch est périmé ne peut pas écraser l'état du nouveau.
"""
from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from unittest import mock

from ameesh import notify, storage

from .support import PgTestCase

STATE_DOC = {"schema": notify.STATE_SCHEMA, "active": {}, "resolved": {},
             "rate": {}, "summary": {}, "last_summary": {}, "briefs": {},
             "notified": {"identity": 1000.0}}


def ncfg():
    return notify.parse_config({
        "routes": {"human:owner": ["ntfy"]},
        "channels": {"ntfy": {"url": "https://ntfy.example", "topic": "t"}},
    })


def decision(since=900.0):
    return {"schema": "ameesh-alert/1", "type": "decision_pending",
            "agent": "mesh-design", "lot": 3576, "responsible": "human:owner",
            "detail": "décision #1 en attente", "since": since}


def lasting(since=900.0):
    """Une alerte qui envoie aussi sa résolution (`decision_pending` est
    silencieuse à la résolution, L124)."""
    return {"schema": "ameesh-alert/1", "type": "dead_runner",
            "agent": "mesh-design", "host": "agents-a", "responsible": "human:owner",
            "detail": "runner mort", "since": since}


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, channel, message):
        self.sent.append((channel.name, message.title))


class NotifyStateStorageTest(PgTestCase):
    """Le bail d'état et son epoch : un seul écrivain, jamais un périmé."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE notify_state")

    def store(self):
        return storage.of(self.db).notify_liveness

    def test_read_is_none_until_seeded(self):
        self.assertIsNone(self.store().read_state("alerts"))
        epoch = self.store().acquire_state("alerts", "agents-a:1", 60)
        self.assertIsNotNone(epoch)
        self.assertIsNone(self.store().read_state("alerts"))
        self.assertTrue(self.store().save_state(
            "alerts", STATE_DOC, owner="agents-a:1", epoch=epoch))
        body = self.store().read_state("alerts")
        self.assertEqual(body["schema"], notify.STATE_SCHEMA)
        self.assertEqual(body["notified"], {"identity": 1000.0})

    def test_the_lease_is_exclusive_and_only_the_holder_writes(self):
        store = self.store()
        epoch_a = store.acquire_state("alerts", "agents-a:1", 60)
        self.assertIsNotNone(epoch_a)
        self.assertIsNone(store.acquire_state("alerts", "agents-b:2", 60))
        self.assertTrue(store.save_state(
            "alerts", dict(STATE_DOC, rate={"human:owner": [1.0]}),
            owner="agents-a:1", epoch=epoch_a))
        self.assertTrue(store.release_state("alerts", "agents-a:1", epoch_a))
        epoch_b = store.acquire_state("alerts", "agents-b:2", 60)
        self.assertGreater(epoch_b, epoch_a)
        # Le détenteur périmé ne remplace pas l'état du nouveau.
        self.assertFalse(store.save_state(
            "alerts", dict(STATE_DOC, rate={"human:owner": [2.0]}),
            owner="agents-a:1", epoch=epoch_a))
        self.assertTrue(store.save_state(
            "alerts", dict(STATE_DOC, rate={"human:owner": [3.0]}),
            owner="agents-b:2", epoch=epoch_b))
        self.assertEqual(store.read_state("alerts")["rate"], {"human:owner": [3.0]})
        self.assertFalse(store.release_state("alerts", "agents-a:1", epoch_a))

    def test_an_expired_lease_is_free_and_its_writer_is_refused(self):
        store = self.store()
        # Une TTL négative est expirée dès la prise : l'heure est celle de la
        # base, jamais celle de l'hôte (qui pourrait retarder).
        epoch_a = store.acquire_state("alerts", "agents-a:1", -1.0)
        self.assertIsNotNone(epoch_a)
        # Le bail est déjà expiré : son propre écrivain est refusé.
        self.assertFalse(store.save_state(
            "alerts", STATE_DOC, owner="agents-a:1", epoch=epoch_a))
        epoch_b = store.acquire_state("alerts", "agents-b:2", 60)
        self.assertIsNotNone(epoch_b)
        self.assertGreater(epoch_b, epoch_a)
        self.assertFalse(store.save_state(
            "alerts", STATE_DOC, owner="agents-a:1", epoch=epoch_a))
        self.assertTrue(store.save_state(
            "alerts", STATE_DOC, owner="agents-b:2", epoch=epoch_b))


class SharedDeliveryTest(PgTestCase):
    """Deux hôtes successifs : une levée, une résolution, aucun doublon."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE notify_state")
        self.dir_a = tempfile.mkdtemp(prefix="3576-a-", dir=self.tmp)
        self.dir_b = tempfile.mkdtemp(prefix="3576-b-", dir=self.tmp)
        self.cfg_a = dataclasses.replace(self.cfg, host="agents-a",
                                         state_dir=self.dir_a)
        self.cfg_b = dataclasses.replace(self.cfg, host="agents-b",
                                         state_dir=self.dir_b)

    def notifier(self, cfg, sender, now=1000.0, logs=None):
        logs = [] if logs is None else logs
        return notify.Notifier(cfg, ncfg(), notify.empty_state(), sender=sender,
                               log=logs.append, clock=lambda: now)

    def pass_on(self, cfg, notifier, current):
        lease, deliver = notify.enter_shared_state(self.db, notifier, cfg)
        self.assertTrue(deliver)
        notifier.run_pass(self.db, current=current)
        self.assertTrue(notify.leave_shared_state(self.db, notifier, lease))
        return lease

    def test_a_host_change_does_not_push_the_same_alert_twice(self):
        first = FakeSender()
        self.pass_on(self.cfg_a, self.notifier(self.cfg_a, first), [decision()])
        self.assertEqual(len(first.sent), 1)

        second = FakeSender()
        other = self.notifier(self.cfg_b, second)
        self.pass_on(self.cfg_b, other, [decision()])
        self.assertEqual(second.sent, [])

    def test_the_new_holder_sends_the_resolution_once(self):
        first = FakeSender()
        self.pass_on(self.cfg_a, self.notifier(self.cfg_a, first), [lasting()])
        self.assertEqual(len(first.sent), 1)

        second = FakeSender()
        other = self.notifier(self.cfg_b, second)
        self.pass_on(self.cfg_b, other, [])
        self.assertEqual(len(second.sent), 1)
        self.assertEqual(second.sent[0][0], "ntfy")

        third = FakeSender()
        later = self.notifier(self.cfg_b, third)
        self.pass_on(self.cfg_b, later, [])
        self.assertEqual(third.sent, [])

    def test_a_standby_host_delivers_nothing_and_waits_for_the_release(self):
        sender_a = FakeSender()
        notifier_a = self.notifier(self.cfg_a, sender_a)
        lease_a, deliver_a = notify.enter_shared_state(self.db, notifier_a, self.cfg_a)
        self.assertTrue(deliver_a)
        self.assertIsNotNone(lease_a)

        sender_b = FakeSender()
        notifier_b = self.notifier(self.cfg_b, sender_b)
        lease_b, deliver_b = notify.enter_shared_state(self.db, notifier_b, self.cfg_b)
        self.assertFalse(deliver_b)
        self.assertIsNone(lease_b)
        self.assertEqual(sender_b.sent, [])

        notify.leave_shared_state(self.db, notifier_a, lease_a)
        lease_b, deliver_b = notify.enter_shared_state(self.db, notifier_b, self.cfg_b)
        self.assertTrue(deliver_b)
        notify.leave_shared_state(self.db, notifier_b, lease_b)

    def test_the_local_console_alerts_survive_the_shared_read(self):
        # #532 : le passage local précède la base ; ses alertes pgbouncer ne
        # doivent pas être écrasées par le document partagé.
        seeded = FakeSender()
        self.pass_on(self.cfg_a, self.notifier(self.cfg_a, seeded), [])
        local = self.notifier(self.cfg_b, FakeSender())
        local_entry = {"alert": {"type": "pgbouncer_pool_waiting",
                                 "since": 900.0},
                       "key": "pool", "status": "envoi", "human": "human:owner",
                       "channels": {"ntfy": {"ok": True, "attempts": 1}}}
        local.state["active"]["pool"] = local_entry
        local.state["notified"]["pool-identity"] = 900.0
        lease, deliver = notify.enter_shared_state(self.db, local, self.cfg_b)
        self.assertTrue(deliver)
        self.assertIn("pool", local.state["active"])
        self.assertEqual(local.state["active"]["pool"], local_entry)
        self.assertIn("pool-identity", local.state["notified"])
        notify.leave_shared_state(self.db, local, lease)


class CmdNotifySharedTest(PgTestCase):
    """Le fil de `cmd_notify --once --only alerts` prend, sème et rend le bail."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE notify_state")

    def test_a_standby_service_keeps_its_other_passes(self):
        # Le bail d'état ne ferme que la livraison d'alertes : le service
        # complet d'un hôte en veille garde take/issues/github-watch/ci.
        holder_cfg = dataclasses.replace(
            self.cfg, host="agents-a",
            state_dir=tempfile.mkdtemp(prefix="3576-holder-", dir=self.tmp))
        holder = notify.Notifier(holder_cfg, ncfg(), notify.empty_state(),
                                 sender=FakeSender(), log=lambda text: None)
        lease, deliver = notify.enter_shared_state(self.db, holder, holder_cfg)
        self.assertTrue(deliver)
        try:
            cfg = dataclasses.replace(
                self.cfg, host="agents-b",
                state_dir=tempfile.mkdtemp(prefix="3576-standby-", dir=self.tmp))
            args = notify.build_parser().parse_args(["--once"])
            fake = mock.Mock()
            fake.state = notify.empty_state()
            fake.log = lambda text: None
            with mock.patch.object(notify, "Notifier", return_value=fake), \
                    mock.patch.object(notify.db_mod, "connect", return_value=self.db), \
                    mock.patch.object(notify.db_mod, "require_schema"):
                self.assertEqual(notify.cmd_notify(cfg, ncfg(), args), 0)
            fake.run_local_pass.assert_called_once()
            fake.run_pass.assert_called_once()
            self.assertEqual(fake.run_pass.call_args.kwargs["only"],
                             frozenset({"take", "issues", "github-watch", "ci"}))
        finally:
            notify.leave_shared_state(self.db, holder, lease)

    def test_once_seeds_the_shared_state_and_releases_the_lease(self):
        cfg = dataclasses.replace(
            self.cfg, host="agents-a",
            state_dir=tempfile.mkdtemp(prefix="3576-cmd-", dir=self.tmp))
        args = notify.build_parser().parse_args(["--once", "--only", "alerts"])
        fake = mock.Mock()
        fake.state = notify.empty_state()
        fake.log = lambda text: None
        with mock.patch.object(notify, "Notifier", return_value=fake), \
                mock.patch.object(notify.db_mod, "connect", return_value=self.db), \
                mock.patch.object(notify.db_mod, "require_schema"):
            self.assertEqual(notify.cmd_notify(cfg, ncfg(), args), 0)
        fake.run_pass.assert_called_once()
        body = storage.of(self.db).notify_liveness.read_state("alerts")
        self.assertEqual(body["schema"], notify.STATE_SCHEMA)
        row = self.db.query("select owner, expires_at from notify_state"
                            " where scope = 'alerts'")
        self.assertEqual(len(row), 1)
        self.assertIsNone(row[0]["owner"])
        self.assertIsNone(row[0]["expires_at"])


if __name__ == "__main__":
    unittest.main()
