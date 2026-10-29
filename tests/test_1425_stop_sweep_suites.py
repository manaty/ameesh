# SPDX-License-Identifier: AGPL-3.0-only
"""#1425 : suites N1/N2/N5/N6 de la R2 Claude sur #753.

#753 rend les lots d'un agent arrêté — « arrêt délibéré » seulement (B1). La R2
Claude (ameesh-dev4, 14/10) laissait quatre constats non bloquants, fermés ici
sans rouvrir cette porte :

- N1 : témoins manquants du chemin CLI (`cmd_stop`) et de l'attribution d'un
  lot promu par la passe du runner (`release_stopped_lots_once`) ;
- N2 : le dessaisissement passait AVANT les gardes de la passe (secteur,
  drainage, code périmé) — il vient après ;
- N5 : `measurement_actor` relisait les canons à chaque passe — la résolution
  n'est payée que s'il y a un arrêt délibéré, et la lecture des canons est
  espacée d'une fenêtre ;
- N6 : ordre des verrous `agent_registry` -> `work_items` vérifié (aucun chemin
  inverse), documenté dans le storage.
"""
from __future__ import annotations

import threading
import types
from unittest import mock

from ameesh import registry
from ameesh import runner as runner_mod
from ameesh import work

from .support import PgTestCase


class _Base(PgTestCase):
    """Mêmes fabriques que `tests/test_753_stop_returns_lots.py`."""

    def agent(self, name, *, status="idle", team="nexlink"):
        registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = %s, team = %s,"
            " responsible = 'human:proprio' WHERE name = %s",
            (status, team, name))

    def lot(self, title, *, assignee=None):
        item = work.add(self.db, title=title, app="nexlink")
        if assignee:
            work.assign(self.db, item["id"], assignee, cfg=self.cfg,
                        force="test: fixture, lots multiples (#540)")
        return work.get(self.db, item["id"])

    def promoted(self, assignee="dev1"):
        lot = self.lot("deployed", assignee=assignee)
        work.close_merged(self.db, lot["id"], sha="a" * 40, actor=assignee)
        work.move(self.db, lot["id"], "promoted", note="déployé", actor="auditeur")
        return lot

    def stopped(self, name, reason="manuel"):
        registry.set_status(self.db, name, "stopped", status_text="stopped",
                            stop_reason=reason)

    def sweep_runner(self, **kwargs):
        """Le strict nécessaire de `release_stopped_lots_once`."""
        base = dict(mediated=False, db=self.db, cfg=self.cfg, runner_id="runner-test",
                    dry_run=False)
        base.update(kwargs)
        return types.SimpleNamespace(**base)


class CmdStopReleasesLotsTest(_Base):
    """N1 : le chemin CLI (`ameesh stop`) dessaisit comme la passe, journal
    compris, et remet le lot promu à l'acteur de la mesure."""

    def test_cmd_stop_returns_open_lots_and_hands_promoted_to_the_auditor(self):
        self.agent("dev1")
        self.agent("auditeur")
        open_lot = self.lot("A", assignee="dev1")
        promoted = self.promoted("dev1")
        self.assertEqual(runner_mod.cmd_stop(self.cfg, self.db, ["dev1"]), 0)
        self.assertIsNone(work.get(self.db, open_lot["id"])["assignee"])
        item = work.get(self.db, promoted["id"])
        self.assertEqual((item["state"], item["assignee"]), ("delivered", "auditeur"))
        journal = self.db.query(
            "SELECT note, actor FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id DESC LIMIT 1", (open_lot["id"],))[0]
        self.assertIn("#753", journal["note"])
        self.assertEqual(journal["actor"], "stop")
        row = [r for r in registry.overview(self.db) if r["name"] == "dev1"][0]
        self.assertEqual((row["status"], row["stop_reason"]), ("stopped", "manuel"))

    def test_cmd_stop_on_unknown_name_returns_nothing(self):
        """N4 : un nom absent du registre ne rend pas les lots d'un homonyme
        (la transaction relit le registre)."""
        self.agent("dev1")
        lot = self.lot("A", assignee="dev1")
        # Un lot orphelin porte exactement le nom absent que la CLI arrête.
        self.db.execute("UPDATE work_items SET assignee = %s WHERE id = %s",
                        ("dev1-absent", lot["id"]))
        runner_mod.cmd_stop(self.cfg, self.db, ["dev1-absent"])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1-absent")


class SweepHandsPromotedTest(_Base):
    """N1 : la passe du runner remet aussi le lot promu à l'acteur de mesure
    (le témoin B1 ne couvrait que les lots ouverts)."""

    def test_sweep_hands_promoted_lot_to_measure_actor(self):
        self.agent("dev1")
        self.agent("auditeur")
        promoted = self.promoted("dev1")
        self.stopped("dev1")
        done = runner_mod.Runner.release_stopped_lots_once(self.sweep_runner())
        self.assertEqual([row["id"] for row in done], [promoted["id"]])
        # #1425 : le chemin réel porte la même clé que le dry-run — sans elle,
        # `cmd_stop` et la passe journalisaient « retourné à la file » pour un
        # lot remis à l'auditeur.
        self.assertEqual(done[0].get("assigned_to"), "auditeur")
        item = work.get(self.db, promoted["id"])
        self.assertEqual((item["state"], item["assignee"]), ("delivered", "auditeur"))
        note = self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id DESC LIMIT 1", (promoted["id"],))[0]["note"]
        self.assertIn("measurement", note)


class SweepAfterGuardsTest(_Base):
    """N2 : la passe ne dessaisit pas sous une garde (secteur, drainage, code
    périmé) ; sur une passe qui travaille, elle précède la reprise des agents."""

    def fake(self, **kwargs):
        fake = types.SimpleNamespace(
            lock=threading.Lock(), workers={},
            power_hold=threading.Event(), draining=threading.Event(),
            restart_for_stale=False, _gate_settled=threading.Event(),
            mediated=True, host_gate=None, db=self.db, host="atelier",
            agents_filter=None, once=True, runner_id="runner-test", dry_run=False,
            # #804 : `sweep` déclenche la prise au repos ; ce témoin ne
            # l'exerce pas (0 = désactivée), seule l'ordre release/claim compte.
            take_interval=0, _take_next=0.0)
        fake._gate_settled.set()
        fake.expire_delegations_once = lambda: None
        fake.expire_operator_pauses_once = lambda: None  # #3961
        # L165 (EP2) : `sweep` publie les scores de rôles puis purge les
        # libertés d'élection observées ; la doublure n'a ni horloge ni
        # registre de rôles, elle neutralise la passe et part sans liberté.
        fake.role_evaluation_once = lambda: None
        fake._election_free_since = {}
        # L85 (#373) : `sweep` prune aussi le journal d'événements.
        fake.prune_events_once = lambda *args, **kwargs: None
        # h-0115 : `sweep` publie aussi la vivacité ; le faux runner de ce
        # témoin ne l'exerce pas, seul l'ordre release/claim compte.
        fake.report_liveness = lambda: None
        # #3070 : `sweep` lève les portes d'appareil restées fermées ; cette
        # doublure n'a ni porte ni horloge, la passe est neutralisée.
        fake.lift_device_gates_once = lambda: None
        fake.release_stopped_lots_once = mock.Mock(return_value=[])
        for key, value in kwargs.items():
            setattr(fake, key, value)
        return fake

    def swept(self, fake):
        with mock.patch.object(registry, "reap", return_value=[]), \
                mock.patch.object(registry, "claimable") as claimable:
            runner_mod.Runner.sweep(fake)
        return claimable

    def test_stale_restart_does_not_release(self):
        fake = self.fake(restart_for_stale=True)
        claimable = self.swept(fake)
        fake.release_stopped_lots_once.assert_not_called()
        claimable.assert_not_called()

    def test_draining_does_not_release(self):
        fake = self.fake()
        fake.draining.set()
        claimable = self.swept(fake)
        fake.release_stopped_lots_once.assert_not_called()
        claimable.assert_not_called()

    def test_power_hold_does_not_release(self):
        fake = self.fake()
        fake.power_hold.set()
        claimable = self.swept(fake)
        fake.release_stopped_lots_once.assert_not_called()
        claimable.assert_not_called()

    def test_working_pass_releases_before_claiming(self):
        fake = self.fake()
        order = []
        fake.release_stopped_lots_once = mock.Mock(
            side_effect=lambda: order.append("release"))

        def claim(*args, **kwargs):
            order.append("claim")
            return []

        with mock.patch.object(registry, "reap", return_value=[]), \
                mock.patch.object(registry, "claimable", side_effect=claim):
            runner_mod.Runner.sweep(fake)
        self.assertEqual(order, ["release", "claim"])


class MeasureCanonsSpacingTest(_Base):
    """N5 : la résolution de l'acteur de mesure ne coûte rien sans arrêt
    délibéré, et la lecture des canons est espacée d'une fenêtre."""

    def test_no_deliberate_stop_does_not_read_canons(self):
        self.agent("dev1")
        self.lot("A", assignee="dev1")           # agent idle : rien à dessaisir
        with mock.patch.object(work, "configured_canons",
                               side_effect=AssertionError("canons lus sans arrêt")):
            self.assertEqual(
                runner_mod.Runner.release_stopped_lots_once(self.sweep_runner()), [])
        self.stopped("dev1", "erreur")           # arrêt réversible : rien non plus
        with mock.patch.object(work, "configured_canons",
                               side_effect=AssertionError("canons lus sans arrêt")):
            self.assertEqual(
                runner_mod.Runner.release_stopped_lots_once(self.sweep_runner()), [])

    def test_canons_are_read_once_per_window(self):
        self.agent("dev1")
        self.lot("A", assignee="dev1")
        # #2877 : la résolution n'est payée que si l'agent tient encore un lot ;
        # le lot promu reste à dev1 (pas d'auditeur) et le garde à chaque passe
        self.promoted("dev1")
        self.stopped("dev1")
        fake = self.sweep_runner()               # pas d'agent nommé `auditeur`
        with mock.patch.object(work, "configured_canons", return_value=[]) as read:
            runner_mod.Runner.release_stopped_lots_once(fake)
            first = read.call_count
            runner_mod.Runner.release_stopped_lots_once(fake)
            second = read.call_count
        self.assertEqual((first, second), (1, 1))
        fake._measure_canons_cache = (0.0, [])   # fenêtre écoulée : relecture
        with mock.patch.object(work, "configured_canons", return_value=[]) as again:
            runner_mod.Runner.release_stopped_lots_once(fake)
        self.assertEqual(again.call_count, 1)
