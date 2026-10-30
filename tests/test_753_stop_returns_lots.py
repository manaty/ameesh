# SPDX-License-Identifier: AGPL-3.0-only
"""#753 : à l'arrêt d'un agent, ses lots ouverts retournent à la file d'équipe
(assigné effacé, journal avec la raison) ; un agent ne les garde pas."""
from __future__ import annotations

import types

from ameesh import registry, storage, work
from ameesh import runner as runner_mod

from .support import PgTestCase


class _Base(PgTestCase):
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


class StopReleasesLotsTest(_Base):
    def test_release_open_lots_with_journal_and_idempotence(self):
        self.agent("dev1")
        lots = [self.lot("A", assignee="dev1"),
                self.lot("B", assignee="dev1"),
                self.lot("C", assignee="dev1")]
        registry.set_status(self.db, "dev1", "stopped", status_text="stopped by hand",
                            stop_reason="manuel")
        done = work.release_lots(self.db, "dev1", reason="stopped (#753)", actor="stop")
        self.assertEqual({row["id"] for row in done}, {lot["id"] for lot in lots})
        for lot in lots:
            self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        ev = self.db.query(
            "SELECT note, actor FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id DESC LIMIT 1", (lots[0]["id"],))[0]
        self.assertIn("#753", ev["note"])
        self.assertEqual(ev["actor"], "stop")
        # idempotent : plus rien à rendre
        self.assertEqual(work.release_lots(self.db, "dev1", reason="again", actor="stop"), [])

    def test_release_leaves_other_agents_and_closed_lots(self):
        self.agent("dev1")
        self.agent("dev2")
        mine = self.lot("mine", assignee="dev1")
        theirs = self.lot("theirs", assignee="dev2")
        merged = self.lot("merged", assignee="dev1")
        work.close_merged(self.db, merged["id"], sha="deadbeef" * 5, actor="test")
        done = work.release_lots(self.db, "dev1", reason="r", actor="a")
        self.assertEqual([row["id"] for row in done], [mine["id"]])
        self.assertEqual(work.get(self.db, theirs["id"])["assignee"], "dev2")
        self.assertEqual(work.get(self.db, merged["id"])["assignee"], "dev1")

    def test_unassigned_lot_is_untouched(self):
        self.agent("dev1")
        free = self.lot("free")
        self.assertEqual(work.release_lots(self.db, "dev1", reason="r", actor="a"), [])
        self.assertIsNone(work.get(self.db, free["id"])["assignee"])


class PromotedMeasurementTest(_Base):
    """#753 (2e incrément) : un lot promu (mesure post-déploiement à refaire) ne
    reste pas avec l'agent arrêté — il va à l'acteur de la mesure (auditeur).
    La mesure de livraison de `work_item_measures` (durée, diff) ne dit rien de
    cette action : une re-mesure de livraison après la promotion ne la clôt pas
    (revue auditeur, R1 #753)."""

    def measure(self, item_id, *, offset_s=0.0):
        self.db.execute(
            "INSERT INTO work_item_measures (work_item_id, started_at, merged_at,"
            " total_minutes, agent_minutes, review_minutes, human_minutes)"
            " VALUES (%s, now(), now(), 1, 1, 0, 0)", (item_id,))
        if offset_s:
            self.remeasure(item_id, offset_s=offset_s)

    def remeasure(self, item_id, *, offset_s=0.0):
        self.db.execute(
            "UPDATE work_item_measures SET measured_at = now() + make_interval(secs => %s)"
            " WHERE work_item_id = %s", (float(offset_s), item_id))

    def promoted(self, assignee="dev1", *, remeasure_after=False):
        self.agent(assignee)
        lot = self.lot("deployed", assignee=assignee)
        work.close_merged(self.db, lot["id"], sha="a" * 40, actor=assignee)
        self.measure(lot["id"], offset_s=-3600)     # mesure de livraison à la fusion
        work.move(self.db, lot["id"], "promoted", note="déployé", actor="auditeur")
        if remeasure_after:
            self.remeasure(lot["id"], offset_s=3600)
        return lot

    def test_measurement_actor_is_the_auditor(self):
        self.agent("auditeur")
        self.assertEqual(work.measurement_actor(self.db, self.cfg), "auditeur")

    def test_stopped_measure_actor_is_not_used(self):
        self.agent("auditeur")
        registry.set_status(self.db, "auditeur", "stopped", status_text="stopped",
                            stop_reason="manuel")
        self.assertEqual(work.measurement_actor(self.db, self.cfg), "")

    def test_stopped_auditor_is_not_a_destination(self):
        # R1 auditeur, témoin demandé : dev1 arrêté, auditeur arrêté — aucun
        # promu n'est réattribué à un agent arrêté ; le lot reste en place (et
        # `orphan_lot` le signale, test_l37_mode_et_alertes).
        self.agent("auditeur")
        registry.set_status(self.db, "auditeur", "stopped", status_text="stopped",
                            stop_reason="manuel")
        lot = self.promoted()
        actor = work.measurement_actor(self.db, self.cfg)
        self.assertEqual(actor, "")
        done = work.release_lots(self.db, "dev1", reason="stopped (#753)", actor="stop",
                                 measure_actor=actor, measure_reason="m")
        self.assertEqual(done, [])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_promoted_lot_goes_to_the_measure_actor(self):
        lot = self.promoted()
        done = work.release_lots(self.db, "dev1", reason="stopped (#753)", actor="stop",
                                 measure_actor="auditeur",
                                 measure_reason="post-deployment measurement to redo (#753)")
        self.assertEqual([row["id"] for row in done], [lot["id"]])
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["state"], item["assignee"]), ("delivered", "auditeur"))
        ev = self.db.query(
            "SELECT note, actor FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id DESC LIMIT 1", (lot["id"],))[0]
        self.assertIn("post-deployment measurement", ev["note"])
        self.assertEqual(ev["actor"], "stop")
        # idempotent : le lot a changé d'assigné
        self.assertEqual(work.release_lots(self.db, "dev1", reason="again", actor="stop",
                                           measure_actor="auditeur",
                                           measure_reason="m"), [])

    def test_delivery_remeasure_after_promotion_does_not_close_the_action(self):
        lot = self.promoted(remeasure_after=True)
        done = work.release_lots(self.db, "dev1", reason="r", actor="a",
                                 measure_actor="auditeur", measure_reason="m")
        self.assertEqual([row["id"] for row in done], [lot["id"]])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "auditeur")

    def test_merged_lot_is_not_handed_to_the_measure_actor(self):
        self.agent("dev1")
        lot = self.lot("merged, not deployed", assignee="dev1")
        work.close_merged(self.db, lot["id"], sha="c" * 40, actor="dev1")
        self.assertEqual(work.release_lots(self.db, "dev1", reason="r", actor="a",
                                           measure_actor="auditeur", measure_reason="m"), [])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_measure_actor_same_as_stopped_agent_leaves_the_lot(self):
        lot = self.promoted(assignee="auditeur")
        done = work.release_lots(self.db, "auditeur", reason="r", actor="a",
                                 measure_actor="auditeur", measure_reason="m")
        self.assertEqual(done, [])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "auditeur")


class DryRunTest(_Base):
    def test_dry_run_returns_candidates_without_writing(self):
        self.agent("dev1")
        open_lot = self.lot("open", assignee="dev1")
        done = work.release_lots(self.db, "dev1", reason="r", actor="a", dry_run=True)
        self.assertTrue(done and all(row.get("dry_run") for row in done))
        self.assertEqual({row["id"] for row in done}, {open_lot["id"]})
        self.assertEqual(work.get(self.db, open_lot["id"])["assignee"], "dev1")


class DeliberateStopTest(_Base):
    """#753 (B1, R2 Claude) : seuls les arrêts DÉLIBÉRÉS rendent les lots. Un
    arrêt automatique et réversible (`erreur` de `stop_after_failures`,
    `retire_du_canon` que `canon.revive` annule, bail expiré) laisse l'agent
    retrouver ses lots à la reprise — sinon une panne fournisseur viderait en
    rafale tous ses lots."""

    def stopped(self, name, reason):
        registry.set_status(self.db, name, "stopped", status_text="stopped",
                            stop_reason=reason)

    def test_automatic_stop_keeps_the_lots(self):
        for reason in ("erreur", "bail_expire", "retire_du_canon", "externe"):
            name = "dev-" + reason
            self.agent(name)
            lot = self.lot("lot " + reason, assignee=name)
            self.stopped(name, reason)
            done = work.release_lots(self.db, name, reason="r", actor="a",
                                     stopped_with=work.RELEASABLE_STOP_REASONS)
            self.assertEqual(done, [], reason)
            self.assertEqual(work.get(self.db, lot["id"])["assignee"], name, reason)

    def test_manual_stop_still_releases(self):
        self.agent("dev1")
        lot = self.lot("A", assignee="dev1")
        self.stopped("dev1", "manuel")
        done = work.release_lots(self.db, "dev1", reason="r", actor="a",
                                 stopped_with=work.RELEASABLE_STOP_REASONS)
        self.assertEqual([row["id"] for row in done], [lot["id"]])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_resumed_agent_keeps_its_lots(self):
        """N3 : la transaction revérifie le registre. Un agent relancé entre le
        relevé et le geste ne perd pas ses lots."""
        self.agent("dev1")
        lot = self.lot("A", assignee="dev1")
        self.stopped("dev1", "manuel")
        registry.set_status(self.db, "dev1", "idle", status_text="resumed")
        done = work.release_lots(self.db, "dev1", reason="r", actor="a",
                                 stopped_with=work.RELEASABLE_STOP_REASONS)
        self.assertEqual(done, [])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_dry_run_keeps_the_non_deliberate_agent_out(self):
        self.agent("dev1")
        error = self.lot("B", assignee="dev1")
        self.stopped("dev1", "erreur")
        done = work.release_lots(self.db, "dev1", reason="r", actor="a",
                                 dry_run=True,
                                 stopped_with=work.RELEASABLE_STOP_REASONS)
        self.assertEqual(done, [])
        self.assertEqual(work.get(self.db, error["id"])["assignee"], "dev1")


class SweepReleasesLotsTest(_Base):
    """N1 : la passe du runner (`release_stopped_lots_once`) dessaisit les
    arrêts délibérés, et seulement eux."""

    def runner(self):
        return types.SimpleNamespace(
            mediated=False, db=self.db, cfg=self.cfg, runner_id="runner-test",
            dry_run=False)

    def test_sweep_releases_manual_only(self):
        self.agent("manuel1")
        self.agent("erreur1")
        manual = self.lot("A", assignee="manuel1")
        error = self.lot("B", assignee="erreur1")
        registry.set_status(self.db, "manuel1", "stopped", status_text="s",
                            stop_reason="manuel")
        registry.set_status(self.db, "erreur1", "stopped", status_text="s",
                            stop_reason="erreur")
        done = runner_mod.Runner.release_stopped_lots_once(self.runner())
        self.assertEqual({row["id"] for row in done}, {manual["id"]})
        self.assertIsNone(work.get(self.db, manual["id"])["assignee"])
        self.assertEqual(work.get(self.db, error["id"])["assignee"], "erreur1")
