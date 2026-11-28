# SPDX-License-Identifier: AGPL-3.0-only
"""#7967 : une réservation expirée ne condamne plus le train, et l'alerte
« passage manqué » tombe au bout d'une heure.

Le rapport du 20/11 : les coupures de 19:00Z et 20:00Z ne sont jamais parties.
Le journal de l'hôte montre que le runner n'a pas dormi : il a bien tenté les
passages de 18:00, 19:00 et 20:00, mais le plan de 18:00 était resté
`draining` sur une erreur de contrat CI, puis sa réservation absolue a expiré
à 20:01. `ReservedLedger.acquire` refusait alors toute nouvelle époque (le
ticket était mort), donc l'état `draining` n'était plus jamais lu ni terminé :
la cible restait réservée et aucun paquet ne composait plus.

Les trois gardes testées ici :
1. l'expiration termine le plan sans promotion et rend la réservation ;
2. `publishing`/`promoting` restent au pilote (effet incertain) ;
3. le passage qui invalide recompose la coupure due dans la foulée.
"""
from __future__ import annotations

import threading
import types
import unittest
from unittest import mock

from ameesh import (exploitation, land, packet_reservation, train,
                    train_reservation, train_runner)
from ameesh.runner import Runner

from .test_1467_train import MemoryPorts, row


class ExpiredReservationTest(unittest.TestCase):
    """Le banc d'effets du train, sans SQL : états et effets exacts."""

    def make(self):
        self.ports = MemoryPorts([row(1)])
        # Une référence distante existe : la branche #7885 (1) ne doit pas
        # terminer le plan avant l'échéance, sinon le test ne prouverait rien.
        self.ports.published = lambda plan: plan["head"]
        self.engine = train.Engine(self.ports, source="github.com/o/r",
                                   target="main",
                                   policy=train.Policy(enabled=True, build_beta=False))
        return self.engine

    def draining(self):
        """Le vrai chemin d'erreur de #7885 : `publishing` lève avant l'effet."""
        original = self.ports.qualify
        calls = {"n": 0}

        def changed(rows, base, mode):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise train.TrainError("admission evidence changed before train effect")
            return original(rows, base, mode)

        self.ports.qualify = changed
        outcome = self.engine.step(3600)
        self.assertEqual(outcome["result"], "refused", outcome)
        self.ports.qualify = original
        state = self.ports.document
        self.assertEqual(state["phase"], "draining")
        self.ports.ci_by_head[state["plan"]["head"]] = "pending"
        return state

    def expire(self, deadline):
        state = self.ports.document
        state["reservation"] = {"deadline": deadline}
        return state

    def test_expiry_with_pending_ci_keeps_the_plan_and_the_target(self):
        # B #44821/#44822/#44824 : l'échéance seule ne prouve pas la fin des
        # runs. Libérer la cible pendant que la CI du paquet tourne lancerait
        # la suivante par-dessus (§11 bis). On attend l'issue terminale.
        self.make()
        state = self.draining()
        head = state["plan"]["head"]
        self.expire(1500.0)
        result = self.engine.step(1500.0)
        self.assertEqual(result["result"], "draining", result)
        self.assertEqual(self.ports.document["phase"], "draining")
        self.assertTrue(self.ports.held)
        self.assertEqual([effect[0] for effect in self.ports.effects], [])
        # Issue terminale établie : le plan est invalidé, sans promotion.
        self.ports.ci_by_head[head] = "green"
        result = self.engine.step(1501.0)
        self.assertEqual(result["result"], "invalidated", result)
        stored = self.ports.document
        self.assertEqual(stored["phase"], "invalidated")
        self.assertFalse(self.ports.held)
        self.assertIn("expired at 1970-01-01T00:25:00Z", stored["error"])
        self.assertIn("phase draining", stored["error"])
        self.assertIn("terminal CI green", stored["error"])
        self.assertIn("admission evidence changed", stored["error"])
        self.assertEqual(stored["diagnosis"],
                         {"stage": "draining", "why": stored["error"]})
        self.assertEqual(self.ports.notices[-1], stored["error"])
        self.assertNotIn("promote", [effect[0] for effect in self.ports.effects])
        # La passe suivante ne rejoue pas le plan.
        self.assertEqual(self.engine.step(1502.0)["result"], "idle")

    def test_expiry_with_unreadable_ci_keeps_draining(self):
        # Une lecture impossible ne prouve aucune fin de run.
        self.make()
        self.draining()
        self.expire(1500.0)
        self.ports.ci = mock.Mock(side_effect=train.TrainError(
            "cannot read exact train CI evidence: api down"))
        result = self.engine.step(1500.0)
        self.assertEqual(result["result"], "draining", result)
        self.assertEqual(self.ports.document["phase"], "draining")
        self.assertTrue(self.ports.held)
        self.assertIn("CI unreadable", result["error"])

    def test_expiry_with_terminal_ci_invalidates_without_promotion(self):
        self.make()
        state = self.draining()
        self.expire(1500.0)
        self.ports.ci_by_head[state["plan"]["head"]] = "red"
        result = self.engine.step(1500.0)
        self.assertEqual(result["result"], "invalidated", result)
        self.assertEqual(self.ports.document["phase"], "invalidated")
        self.assertFalse(self.ports.held)
        self.assertIn("terminal CI red", self.ports.document["error"])
        self.assertNotIn("promote", [effect[0] for effect in self.ports.effects])

    def test_expiry_with_absent_unpublished_ci_invalidates(self):
        # Aucun run ne peut être dû : ni feu, ni publication, ni CI.
        self.make()
        state = self.draining()
        self.ports.published = lambda plan: ""
        self.ports.ci_by_head[state["plan"]["head"]] = "absent"
        self.expire(1500.0)
        result = self.engine.step(1500.0)
        self.assertEqual(result["result"], "invalidated", result)
        self.assertIn("no publication and no CI run", self.ports.document["error"])
        self.assertFalse(self.ports.held)

    def test_expiry_in_build_beta_does_not_compose_over_a_running_ci(self):
        # Le témoin demandé en build/beta : la coupure due (7300 > dernier
        # départ 3600) ne part pas tant que la CI du paquet n'a pas d'issue
        # terminale ; elle repart seulement après l'invalidation.
        self.make()
        state = self.draining()
        previous = self.engine.epoch
        schedule = types.SimpleNamespace(last_packet=lambda now: 7200.0)
        self.engine = train.Engine(self.ports, source="github.com/o/r",
                                   target="main",
                                   policy=train.Policy(enabled=True, build_beta=True,
                                                       schedule=schedule))
        self.engine.epoch = previous      # même bail, autre politique
        self.expire(1500.0)
        probes = len(self.ports.probes)
        result = self.engine.step(7300.0)
        self.assertEqual(result["result"], "draining", result)
        self.assertEqual(len(self.ports.probes), probes)
        self.assertEqual(self.ports.document["phase"], "draining")
        self.ports.ci_by_head[state["plan"]["head"]] = "green"
        self.assertEqual(self.engine.step(7301.0)["result"], "invalidated")
        self.assertEqual(len(self.ports.probes), probes)
        # La cible est libérée : la passe suivante compose la coupure due.
        self.assertEqual(self.engine.step(7302.0)["result"], "ci")
        self.assertEqual(len(self.ports.probes), probes + 1)

    def test_before_the_deadline_the_same_error_stays_draining(self):
        self.make()
        self.draining()
        self.expire(1500.0)
        result = self.engine.step(1499.0)
        self.assertEqual(result["result"], "draining", result)
        self.assertEqual(self.ports.document["phase"], "draining")
        self.assertTrue(self.ports.held)

    def test_an_unreadable_or_missing_deadline_is_fail_closed(self):
        self.make()
        self.draining()
        for value in (None, {}, {"deadline": "soon"}, {"deadline": float("nan")},
                      {"deadline": True}):
            with self.subTest(value=value):
                state = self.ports.document
                if value is None:
                    state.pop("reservation", None)
                else:
                    state["reservation"] = value
                result = self.engine.step(9000.0)
                self.assertEqual(result["result"], "draining", result)
                self.assertEqual(self.ports.document["phase"], "draining")
                self.assertTrue(self.ports.held)

    def test_a_terminal_suite_refusal_invalidates_at_once(self):
        # mesh-design (20/11 20:39) : « un refus sur des runs terminés est une
        # issue terminale ; l'échéance est le filet ». Le run est completed et
        # sa suite requise ne prouve rien : aucun autre résultat n'arrivera.
        self.make()
        self.draining()
        self.expire(9999.0)          # la réservation n'est pas la cause
        self.ports.ci = mock.Mock(side_effect=train.SuiteRefused(
            "required suite job missing, skipped or interrupted"))
        result = self.engine.step(3601)
        self.assertEqual(result["result"], "invalidated", result)
        stored = self.ports.document
        self.assertEqual(stored["phase"], "invalidated")
        self.assertFalse(self.ports.held)
        self.assertEqual(stored["diagnosis"]["stage"], "ci")
        self.assertIn("required suite job", stored["error"])
        self.assertNotIn("promote", [effect[0] for effect in self.ports.effects])

    def test_a_ci_read_failure_is_not_a_terminal_refusal(self):
        # Lecture impossible (API, réseau) : rien n'est prouvé, on attend.
        self.make()
        self.draining()
        self.expire(9999.0)
        self.ports.ci = mock.Mock(side_effect=train.TrainError(
            "cannot read exact train CI evidence: api down"))
        result = self.engine.step(3601)
        self.assertEqual(result["result"], "refused", result)
        self.assertEqual(self.ports.document["phase"], "draining")
        self.assertTrue(self.ports.held)

    def test_publishing_and_promoting_are_left_to_the_pilot(self):
        # Un push ou un merge a pu partir : l'expiration ne conclut pas.
        self.assertNotIn("publishing", train.EXPIRABLE)
        self.assertNotIn("promoting", train.EXPIRABLE)
        self.make()
        self.draining()
        self.expire(1500.0)
        state = self.ports.document
        state["phase"] = "promoting"
        state["plan"] = dict(state["plan"], base="a" * 40, head="b" * 40)
        state["fire"] = {"id": 1}
        result = self.engine.step(1500.0)
        self.assertEqual(result["result"], "refused", result)
        self.assertIn("uncertain promotion", result["error"])
        self.assertEqual(self.ports.document["phase"], "promoting")


class ExpiredReservationLedgerTest(unittest.TestCase):
    """`ReservedLedger.acquire` doit encore donner une époque pour terminer.

    Sans cette reprise, l'expiration rend le verrou mais le train ne peut plus
    jamais le reprendre : `step` répond `busy` à vie (le journal du 20/11)."""

    def ledger(self, phase="draining", deadline=1000.0, clock=2000.0,
               reservation=None):
        ledger = object.__new__(train_reservation.ReservedLedger)
        ledger.key = "nexlink:develop"
        ledger.owner = "train:runner-test"
        ledger.contract = "d" * 64
        ledger.provider = packet_reservation
        ledger.latest = None
        ledger.service = None
        ledger.reservation = types.SimpleNamespace(clock=lambda: clock)
        plan = dict(branch="integration/train-1", base="a" * 40, head="b" * 40)
        ticket = packet_reservation.Ticket(
            key=ledger.key, owner=ledger.owner, epoch=3,
            branch=plan["branch"], base=plan["base"], head=plan["head"],
            ci_contract=ledger.contract, deadline=deadline,
            checked_at=900.0, ttl_s=7200.0)
        state = dict(revision=4, phase=phase, plan=plan,
                     reservation=ticket.to_json() if reservation is None else reservation)
        ledger.ops = mock.Mock()
        ledger.ops.train_read.return_value = state
        ledger.ops.land_lock.return_value = 8
        ledger._renew = mock.Mock(side_effect=train.TrainError(
            "packet reservation absolute deadline expired"))
        return ledger

    def test_an_expired_drainable_phase_takes_a_new_epoch(self):
        for phase in train.EXPIRABLE:
            with self.subTest(phase=phase):
                ledger = self.ledger(phase=phase)
                self.assertEqual(ledger.acquire(None), 8)
                ledger.ops.land_lock.assert_called_once_with(
                    ledger.key, ledger.owner, land.LOCK_TTL_S, epoch=None)

    def test_a_held_epoch_is_renewed_on_the_expired_ticket(self):
        # Le moteur garde la cible sous le verrou ordinaire pendant qu'il
        # attend l'issue des runs : pas de nouvelle époque à chaque passe.
        ledger = self.ledger()
        ledger.ops.land_lock.return_value = 7
        self.assertEqual(ledger.acquire(7), 7)
        ledger.ops.land_lock.assert_called_once_with(
            ledger.key, ledger.owner, land.LOCK_TTL_S, epoch=7)
        ledger._renew.assert_not_called()

    def test_an_expired_ticket_renews_the_ordinary_lock(self):
        # B #44835 : le verrou ordinaire (1800 s) est plus court que la
        # cadence ; il doit être renouvelé jusqu'à l'invalidation, sinon un
        # composeur manuel peut lancer sa CI pendant que l'ancienne court.
        ledger = self.ledger()
        self.assertEqual(ledger.next_maintenance(),
                         2000.0 + land.LOCK_TTL_S / 3.0)
        ledger.ops.land_lock.return_value = 7
        self.assertEqual(ledger.maintain(2600.0, epoch=7),
                         2600.0 + land.LOCK_TTL_S / 3.0)
        ledger.ops.land_lock.assert_called_once_with(
            ledger.key, ledger.owner, land.LOCK_TTL_S, epoch=7)
        ledger._renew.assert_not_called()
        # Aucune écriture du ticket mort : l'échéance reste absolue.
        ledger.ops.train_save.assert_not_called()

    def test_the_ordinary_lock_survives_a_gap_longer_than_its_ttl(self):
        # Cadence nexlink 1 h > TTL 1800 s : six renouvellements au tiers du
        # TTL gardent la cible tenue bien au-delà d'un TTL, sans composition.
        ledger = self.ledger()
        ledger.ops.land_lock.return_value = 7
        t = ledger.next_maintenance()
        for _ in range(6):
            t = ledger.maintain(t, epoch=7)
        self.assertGreater(t, 2000.0 + land.LOCK_TTL_S)
        self.assertEqual(ledger.ops.land_lock.call_count, 6)
        self.assertTrue(all(call.kwargs["epoch"] == 7
                            for call in ledger.ops.land_lock.call_args_list))
        ledger.ops.train_save.assert_not_called()

    def test_a_lost_ordinary_lock_is_retaken_on_maintenance(self):
        ledger = self.ledger()
        ledger.ops.land_lock.side_effect = [None, 9]
        self.assertEqual(ledger.maintain(2600.0, epoch=7),
                         2600.0 + land.LOCK_TTL_S / 3.0)
        self.assertEqual(ledger.ops.land_lock.call_args_list, [
            mock.call(ledger.key, ledger.owner, land.LOCK_TTL_S, epoch=7),
            mock.call(ledger.key, ledger.owner, land.LOCK_TTL_S, epoch=None)])

    def test_publishing_and_promoting_never_retake_the_epoch(self):
        for phase in ("publishing", "promoting"):
            with self.subTest(phase=phase):
                ledger = self.ledger(phase=phase)
                self.assertIsNone(ledger.acquire(None))
                ledger.ops.land_lock.assert_not_called()

    def test_a_live_ticket_never_retakes_the_epoch(self):
        ledger = self.ledger(deadline=9000.0)
        self.assertIsNone(ledger.acquire(None))
        ledger.ops.land_lock.assert_not_called()

    def test_an_unreadable_ticket_never_retakes_the_epoch(self):
        for reservation in ({}, {"deadline": 9000.0}):
            with self.subTest(reservation=reservation):
                ledger = self.ledger(reservation=reservation)
                self.assertIsNone(ledger.acquire(None))
                ledger.ops.land_lock.assert_not_called()


class ServiceRepassTest(unittest.TestCase):
    """#2965 + #7967 : la cible vient d'être libérée, la coupure due part."""

    def service(self, passage):
        return train_runner.Service(object(), [passage], stop=threading.Event(),
                                    report=mock.Mock())

    def test_an_invalidated_passage_is_repassed_at_once(self):
        passage = mock.Mock()
        passage.scheduled_step.return_value = ({"result": "invalidated"}, 200)
        service = self.service(passage)
        service.pending.clear()
        with mock.patch("ameesh.scoped_db.ScopedDb", return_value=mock.Mock()):
            self.assertEqual(service.pass_once(0, 100), 200)
        self.assertEqual(service.pending, {0})

    def test_a_draining_passage_is_not_repassed(self):
        passage = mock.Mock()
        passage.scheduled_step.return_value = ({"result": "draining"}, 200)
        service = self.service(passage)
        service.pending.clear()
        with mock.patch("ameesh.scoped_db.ScopedDb", return_value=mock.Mock()):
            self.assertEqual(service.pass_once(0, 100), 200)
        self.assertEqual(service.pending, set())


class PausedAgentTest(unittest.TestCase):
    """La pause opérateur ne touche que les tours : le service de train reste
    lancé. Le journal du 20/11 le prouve (passages tentés pendant la pause de
    17:58 à 21:58), ce test fixe le découplage (#7967)."""

    def test_a_paused_agent_does_not_gate_the_train_service(self):
        runner = Runner.__new__(Runner)
        passage = types.SimpleNamespace(policy=train.Policy(enabled=True),
                                        reservation=object())
        runner.train_passages, runner.train_service = [passage], None
        runner.cfg, runner.stop = object(), threading.Event()
        runner.train_refresh = None
        runner.once = runner.dry_run = runner.mediated = False
        runner.agent = {"operator_paused": True}
        with mock.patch.object(train_runner, "Service") as service:
            runner.start_train_service()
        service.assert_called_once()
        service.return_value.start.assert_called_once_with()


class TrainStuckHourTest(unittest.TestCase):
    """#7967 : le passage manqué est nommé au bout d'une heure, pas d'un jour."""

    class Db:
        def __init__(self, rows):
            self.rows = rows

        def query(self, *args, **kwargs):
            return self.rows

    def row(self, phase="draining", **document):
        return {"target_key": "nexlink:develop", "revision": 140,
                "document": dict({"phase": phase, "started_ts": 1000.0,
                                  "failures": 0, "plan": {"id": "plan-1"}},
                                 **document)}

    def test_59_minutes_is_not_yet_a_missed_passage(self):
        db = self.Db([self.row()])
        self.assertEqual(exploitation._train_stuck(db, 1000.0 + 59 * 60), [])

    def test_the_threshold_follows_the_project_cadence(self):
        # §7 : pas de constante propre à un client. Cadence ameesh 11:00/23:00
        # (12 h) : le passage suivant n'est dû qu'à 23:00, pas à 12:00.
        rules = types.SimpleNamespace(next_packet=lambda at: (
            23 * 3600.0 if at < 23 * 3600.0 else 35 * 3600.0))
        started = 11 * 3600.0
        db = self.Db([self.row(phase="ci", started_ts=started)])
        with mock.patch.object(exploitation, "_packet_calendar",
                               return_value=rules):
            self.assertEqual(exploitation._train_stuck(
                db, started + 11 * 3600.0, cfg=object()), [])
            alerts = exploitation._train_stuck(
                db, started + 13 * 3600.0, cfg=object())
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["threshold"], 12)
        self.assertEqual(alerts[0]["value"], 13)

    def test_61_minutes_raises_one_named_alert(self):
        db = self.Db([self.row(error="required suite job missing, skipped or interrupted")])
        alerts = exploitation._train_stuck(db, 1000.0 + 61 * 60)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["type"], "train_stuck")
        self.assertEqual(alert["phase"], "draining")
        self.assertEqual(alert["plan"], "plan-1")
        self.assertIn("phase draining", alert["detail"])
        self.assertIn("required suite job missing", alert["detail"])
        self.assertEqual(alert["since"], 1000.0)
        # Une seule alerte par train et par phase, quel que soit l'âge.
        later = exploitation._train_stuck(self.Db([self.row()]),
                                          1000.0 + 5 * 3600)[0]
        self.assertEqual(exploitation.alert_key(alert), exploitation.alert_key(later))


if __name__ == "__main__":
    unittest.main()
