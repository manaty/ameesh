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
from ameesh.storage.postgres import notify_liveness as liveness_mod

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


def decision(since=900.0):
    """Une demande de décision routée vers human:owner par `ncfg()`."""
    return {"schema": "ameesh-alert/1", "type": "decision_pending",
            "agent": "mesh-design", "lot": 3913, "responsible": "human:owner",
            "detail": "décision #1 en attente", "since": since}


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

    def test_a_local_deadline_stops_a_pass_without_any_database_call(self):
        # #3913 B1 : même si le fil de renouvellement est bloqué ou la base
        # muette, l'échéance monotone locale interdit de livrer au-delà de la
        # TTL (aucun aller-retour base dans ce témoin).
        renewal = notify._StateRenewal(object(), "agents-a:1:tok", 1,
                                       ttl=0.05, interval=10)
        lease = notify.StateLease("agents-a:1:tok", 1, renewal)
        self.assertFalse(lease.expired())
        time.sleep(0.1)
        self.assertTrue(lease.expired())

    def test_the_initial_deadline_starts_before_the_acquisition(self):
        # #3913 (relecture cx1) : l'ancre est celle d'AVANT l'appel
        # d'acquisition ; une prise retardée de plus que la TTL ne donne pas
        # un droit local neuf au retour.
        past = notify._StateRenewal(
            object(), "agents-a:1:tok", 1, ttl=0.1, interval=10,
            acquired_at=time.monotonic() - 0.2)
        self.assertTrue(past.expired())
        fresh = notify._StateRenewal(
            object(), "agents-a:1:tok", 1, ttl=10, interval=10,
            acquired_at=time.monotonic())
        self.assertFalse(fresh.expired())


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

    def test_a_stale_pass_pushes_nothing_after_another_host_took_the_lease(self):
        # #3913 B1 (témoin de la relecture cx3) : A prend, sa passe est
        # retardée au-delà de la TTL, B prend et livre ; A reprend sa passe
        # périmée -> aucune poussée, et l'état de B n'est pas écrasé.
        notifier_a = self.notifier(self.cfg_a)
        lease_a, deliver_a = notify.enter_shared_state(
            self.db, notifier_a, self.cfg_a, lease_s=0.2, renew_s=0.05)
        self.assertTrue(deliver_a)
        lease_a.renewal.stop()            # la passe A ne renouvelle plus
        time.sleep(0.3)                   # sa TTL est passée
        notifier_b = self.notifier(self.cfg_b)
        lease_b, deliver_b = notify.enter_shared_state(
            self.db, notifier_b, self.cfg_b, lease_s=60, renew_s=20)
        self.assertTrue(deliver_b)
        notifier_b.run_pass(self.db, current=[decision()])
        self.assertEqual(len(notifier_b.sender.sent), 1)
        saved_b = storage.of(self.db).notify_liveness.read_state("alerts")
        # A reprend sa passe avec la même décision : rien ne part, et son
        # rendu ne réécrit pas le document de B.
        notifier_a.run_pass(self.db, current=[decision()])
        self.assertEqual(notifier_a.sender.sent, [])
        self.assertFalse(notify.leave_shared_state(
            self.db, notifier_a, lease_a, cfg=self.cfg_a))
        self.assertEqual(
            storage.of(self.db).notify_liveness.read_state("alerts"), saved_b)
        notify.leave_shared_state(self.db, notifier_b, lease_b, cfg=self.cfg_b)

    def test_a_pass_stops_as_soon_as_the_renewal_is_refused(self):
        # #3913 B1 : `renew_state` refuse explicitement (bail pris ailleurs) ;
        # la passe ne doit pas pousser même si son échéance locale court encore.
        notifier = self.notifier(self.cfg_a)
        lease, deliver = notify.enter_shared_state(
            self.db, notifier, self.cfg_a)
        self.assertTrue(deliver)
        lease.renewal.lost = True
        notifier.run_pass(self.db, current=[decision()])
        self.assertEqual(notifier.sender.sent, [])
        self.assertFalse(notify.leave_shared_state(
            self.db, notifier, lease, cfg=self.cfg_a))

    def test_a_pass_stops_when_the_lease_is_lost_during_the_observation(self):
        # #3913 B1 : le bail survit à la prise, puis se perd PENDANT la
        # lecture/le calcul. Le second verdict, juste avant les accusés et la
        # première poussée, doit arrêter la passe.
        notifier = self.notifier(self.cfg_a)
        notifier.only = frozenset({"alerts"})
        lease, deliver = notify.enter_shared_state(
            self.db, notifier, self.cfg_a)
        self.assertTrue(deliver)
        with mock.patch.object(notifier, "_delivery_open",
                               side_effect=[True, False]) as gate, \
                mock.patch.object(notify.exploitation, "alerts",
                                  return_value=[decision()]):
            notifier.run_pass(self.db)
        self.assertEqual(gate.call_count, 2)
        self.assertEqual(notifier.sender.sent, [])
        notify.leave_shared_state(self.db, notifier, lease, cfg=self.cfg_a)

    def test_a_delayed_acquisition_cannot_open_a_late_delivery(self):
        # #3913 (relecture cx1) : la TTL serveur commence DANS acquire_state ;
        # un retour retardé de plus que la TTL ne doit pas laisser une échéance
        # locale neuve au retour, sans aucun renouvellement.
        original = liveness_mod.NotifyLiveness.acquire_state

        def delayed(domain, scope, owner, ttl):
            time.sleep(0.3)
            return original(domain, scope, owner, ttl)

        notifier = self.notifier(self.cfg_a)
        with mock.patch.object(liveness_mod.NotifyLiveness, "acquire_state",
                               new=delayed):
            lease, deliver = notify.enter_shared_state(
                self.db, notifier, self.cfg_a, lease_s=0.2, renew_s=10)
        self.assertTrue(deliver)
        self.assertIsNotNone(lease)
        self.assertTrue(lease.expired())
        notifier.run_pass(self.db, current=[decision()])
        self.assertEqual(notifier.sender.sent, [])
        self.assertFalse(notify.leave_shared_state(
            self.db, notifier, lease, cfg=self.cfg_a))

    def test_a_delayed_reacquisition_after_reclaim_is_anchored_too(self):
        # Même borne pour la DEUXIÈME tentative (reprise d'un bail local mort).
        dead = _dead_pid()
        seeded = storage.of(self.db).notify_liveness.acquire_state(
            "alerts", "agents-a:%d:dead" % dead, 600)
        self.assertIsNotNone(seeded)
        original = liveness_mod.NotifyLiveness.acquire_state
        calls = {"n": 0}

        def delayed(domain, scope, owner, ttl):
            calls["n"] += 1
            if calls["n"] == 2:
                time.sleep(0.3)
            return original(domain, scope, owner, ttl)

        notifier = self.notifier(self.cfg_a)
        with mock.patch.object(liveness_mod.NotifyLiveness, "acquire_state",
                               new=delayed):
            lease, deliver = notify.enter_shared_state(
                self.db, notifier, self.cfg_a, lease_s=0.2, renew_s=10)
        self.assertEqual(calls["n"], 2)
        self.assertTrue(deliver)
        self.assertIsNotNone(lease)
        self.assertTrue(lease.expired())
        notifier.run_pass(self.db, current=[decision()])
        self.assertEqual(notifier.sender.sent, [])
        self.assertFalse(notify.leave_shared_state(
            self.db, notifier, lease, cfg=self.cfg_a))


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
