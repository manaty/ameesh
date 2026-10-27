# SPDX-License-Identifier: AGPL-3.0-only
"""#3913 : le bail d'état des alertes survit à une passe tuée, sans 10 min.

Le 27/10 à 06:16Z, un redémarrage de Postgres a tué la passe `alerts`
d'agents-2 qui tenait le bail de `notify_state`. Le rendu était tenté sur la
connexion morte et l'échec avalé : le bail `agents-2:PID:jeton` restait
orphelin 600 s, exactement `notify_gap.ALIVE_S`, et les passes suivaient en
attente sans battement jusqu'à lever `notify_undeliverable` — « aucun
notifieur vivant ne couvre human:smichea ».

Témoins :
* un bail vivant est renouvelé sans changer d'epoch, et une seconde passe ne
  le vole pas pendant une passe longue ;
* un bail laissé par un processus mort du MÊME hôte est repris tout de suite ;
  celui d'un processus vivant ne l'est pas ;
* un rendu dont la connexion est morte passe par une connexion neuve ;
* une réclamation de brief antérieure au début de l'épisode ne produit plus
  un « Reminder, alert still active for 15 h 53 » ;
* `STATE_LEASE_S` + le tick de la minuterie de rôle restent sous `ALIVE_S`.
"""
from __future__ import annotations

import dataclasses
import os
import tempfile
import time
import unittest
from unittest import mock

from ameesh import notify, notify_gap, role_timers, storage

from .support import PgTestCase


def ncfg():
    return notify.parse_config({
        "routes": {"human:owner": ["ntfy"]},
        "channels": {"ntfy": {"url": "https://ntfy.example", "topic": "t"}},
    })


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, channel, message):
        self.sent.append((channel.name, message.title))


class LeaseBoundsTest(unittest.TestCase):
    """Le bail perdu doit se reprendre avant le seuil de l'alerte."""

    def test_a_lost_lease_recovers_before_the_liveness_threshold(self):
        self.assertGreater(notify.STATE_LEASE_S, 0.0)
        self.assertGreater(notify.STATE_RENEW_S, 0.0)
        self.assertLess(notify.STATE_RENEW_S, notify.STATE_LEASE_S)
        # deux occasions de renouveler avant l'expiration
        self.assertLessEqual(notify.STATE_RENEW_S * 2, notify.STATE_LEASE_S)
        # un bail perdu + le tick de la minuterie restent sous ALIVE_S
        self.assertLess(
            notify.STATE_LEASE_S + role_timers.ALERTS_INTERVAL_S,
            notify_gap.ALIVE_S)


class BriefBodyTest(unittest.TestCase):
    """#3913 : « Reminder » seulement pour le même épisode."""

    TEXT = "no living notifier covers human:alice"

    def test_a_previous_episode_brief_is_not_a_reminder(self):
        # épisode commencé à 200 ; le brief précédent date de 100
        self.assertEqual(
            notify_gap.brief_body(self.TEXT, 100.0, 200.0, 1000.0), self.TEXT)

    def test_the_same_episode_is_a_reminder(self):
        body = notify_gap.brief_body(self.TEXT, 300.0, 200.0, 1000.0)
        self.assertTrue(body.startswith("Reminder, alert still active for "))
        self.assertTrue(body.endswith(": " + self.TEXT))

    def test_the_reminder_age_is_the_episode_age_not_the_last_brief(self):
        early = notify_gap.brief_body(self.TEXT, 300.0, 200.0, 1000.0)
        late = notify_gap.brief_body(self.TEXT, 500.0, 200.0, 1000.0)
        self.assertEqual(early, late)

    def test_a_first_brief_stays_simple(self):
        self.assertEqual(
            notify_gap.brief_body(self.TEXT, None, 200.0, 1000.0), self.TEXT)


def _dead_pid() -> int:
    """Un PID qui n'existe pas sur cet hôte (sans le créer)."""
    for pid in range(3_999_999, 3_999_000, -1):
        if not notify._pid_alive(pid):
            return pid
    raise AssertionError("no dead pid found")


class LeaseStorageTest(PgTestCase):
    """Renouvellement, lecture du bail, et refus d'un bail expiré."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE notify_state")

    def store(self):
        return storage.of(self.db).notify_liveness

    def test_renew_state_extends_a_live_lease_without_changing_its_epoch(self):
        epoch = self.store().acquire_state("alerts", "agents-a:1", 60)
        self.assertIsNotNone(epoch)
        # On rapproche l'expiration pour ne pas dépendre d'une attente : le
        # renouvellement doit la repousser sans toucher à l'epoch.
        self.db.execute(
            "update notify_state set expires_at ="
            " clock_timestamp() + interval '1 second' where scope = 'alerts'")
        self.assertTrue(self.store().renew_state("alerts", "agents-a:1", epoch, 5))
        row = self.store().state_lease("alerts")
        self.assertEqual(row["owner"], "agents-a:1")
        self.assertEqual(int(row["epoch"]), epoch)
        self.assertGreater(float(row["expires_ts"]) - float(row["now_ts"]), 3.0)

    def test_an_expired_lease_is_never_resurrected_by_a_renewal(self):
        epoch = self.store().acquire_state("alerts", "agents-a:1", 60)
        self.assertIsNotNone(epoch)
        self.db.execute(
            "update notify_state set expires_at ="
            " clock_timestamp() - interval '1 second' where scope = 'alerts'")
        self.assertFalse(self.store().renew_state("alerts", "agents-a:1", epoch, 60))
        self.assertIsNotNone(self.store().acquire_state(
            "alerts", "agents-b:2", 60))

    def test_an_expired_lease_is_never_resurrected_by_a_renewal(self):
        epoch = self.store().acquire_state("alerts", "agents-a:1", 0.2)
        self.assertIsNotNone(epoch)
        time.sleep(0.4)
        self.assertFalse(self.store().renew_state("alerts", "agents-a:1", epoch, 60))
        self.assertIsNotNone(self.store().acquire_state(
            "alerts", "agents-b:2", 60))

    def test_state_lease_is_none_without_an_owner(self):
        epoch = self.store().acquire_state("alerts", "agents-a:1", 60)
        self.assertIsNotNone(epoch)
        self.assertTrue(self.store().release_state("alerts", "agents-a:1", epoch))
        self.assertIsNone(self.store().state_lease("alerts"))


class LeaseRecoveryTest(PgTestCase):
    """La passe tuée d'un hôte ne bloque plus la reprise."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE notify_state")
        self.dir_a = tempfile.mkdtemp(prefix="3913-a-", dir=self.tmp)
        self.dir_b = tempfile.mkdtemp(prefix="3913-b-", dir=self.tmp)
        self.cfg_a = dataclasses.replace(self.cfg, host="agents-a",
                                         state_dir=self.dir_a)
        self.cfg_b = dataclasses.replace(self.cfg, host="agents-b",
                                         state_dir=self.dir_b)

    def notifier(self, cfg):
        return notify.Notifier(cfg, ncfg(), notify.empty_state(),
                               sender=FakeSender(), log=lambda text: None)

    def test_a_long_pass_keeps_its_lease_and_the_standby_stays_out(self):
        notifier = self.notifier(self.cfg_a)
        lease, deliver = notify.enter_shared_state(
            self.db, notifier, self.cfg_a, lease_s=2.0, renew_s=0.25)
        self.assertTrue(deliver)
        self.assertIsNotNone(lease)
        try:
            time.sleep(2.6)  # la TTL seule serait expirée depuis 0,6 s
            row = storage.of(self.db).notify_liveness.state_lease("alerts")
            self.assertEqual(row["owner"], lease.owner)
            self.assertGreater(float(row["expires_ts"]), float(row["now_ts"]))
            lease_b, deliver_b = notify.enter_shared_state(
                self.db, self.notifier(self.cfg_b), self.cfg_b,
                lease_s=2.0, renew_s=0.25)
            self.assertFalse(deliver_b)
            self.assertIsNone(lease_b)
        finally:
            notify.leave_shared_state(self.db, notifier, lease, cfg=self.cfg_a)

    def test_a_dead_same_host_holder_is_taken_back_at_once(self):
        dead = _dead_pid()
        epoch = storage.of(self.db).notify_liveness.acquire_state(
            "alerts", "agents-a:%d:dead" % dead, 600)
        self.assertIsNotNone(epoch)
        notifier = self.notifier(self.cfg_a)
        lease, deliver = notify.enter_shared_state(
            self.db, notifier, self.cfg_a)
        self.assertTrue(deliver)
        self.assertIsNotNone(lease)
        self.assertGreater(lease.epoch, epoch)
        self.assertEqual(lease.owner.split(":")[0], "agents-a")
        notify.leave_shared_state(self.db, notifier, lease, cfg=self.cfg_a)

    def test_a_live_same_host_holder_is_never_stolen(self):
        owner = "agents-a:%d:live" % os.getpid()
        epoch = storage.of(self.db).notify_liveness.acquire_state(
            "alerts", owner, 600)
        self.assertIsNotNone(epoch)
        lease, deliver = notify.enter_shared_state(
            self.db, self.notifier(self.cfg_a), self.cfg_a)
        self.assertFalse(deliver)
        self.assertIsNone(lease)
        self.assertTrue(storage.of(self.db).notify_liveness.release_state(
            "alerts", owner, epoch))

    def test_a_lease_of_another_host_is_left_to_its_ttl(self):
        owner = "agents-b:%d:elsewhere" % _dead_pid()
        epoch = storage.of(self.db).notify_liveness.acquire_state(
            "alerts", owner, 600)
        self.assertIsNotNone(epoch)
        lease, deliver = notify.enter_shared_state(
            self.db, self.notifier(self.cfg_a), self.cfg_a)
        self.assertFalse(deliver)
        self.assertIsNone(lease)
        self.assertTrue(storage.of(self.db).notify_liveness.release_state(
            "alerts", owner, epoch))


class ReleaseOnFreshConnectionTest(PgTestCase):
    """#3913 : le rendu ne s'avale plus sur la connexion morte."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE notify_state")

    def test_release_uses_a_new_connection_when_the_pass_one_is_dead(self):
        store = storage.of(self.db).notify_liveness
        epoch = store.acquire_state("alerts", "agents-a:1", 600)
        self.assertIsNotNone(epoch)
        lease = notify.StateLease("agents-a:1", epoch)

        class DeadLiveness:
            def release_state(self, *args, **kwargs):
                raise notify.db_mod.DbError(
                    "terminating connection due to administrator command")

        class DeadStore:
            notify_liveness = DeadLiveness()

        real = storage.of(self.db)
        with mock.patch.object(
                notify.storage, "of", side_effect=[DeadStore(), real]), \
                mock.patch.object(notify.db_mod, "connect",
                                  return_value=object()) as connect:
            self.assertTrue(notify.release_shared_state(
                object(), lease, cfg=self.cfg))
        self.assertEqual(connect.call_count, 1)
        self.assertIsNone(store.state_lease("alerts"))


if __name__ == "__main__":
    unittest.main()
