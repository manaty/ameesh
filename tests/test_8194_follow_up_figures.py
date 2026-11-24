# SPDX-License-Identifier: AGPL-3.0-only
"""#8194: a follow-up stays out of every mesh figure (#6403 criterion 2)."""
import time

from ameesh import (debit, estimates, exploitation, file_decoupage, model_evaluation_report,
                    plan, progress, projects, registry, stagnation, storage, take_refusals,
                    work)
from ameesh import work_lifecycle as lifecycle

from .support import PgTestCase

DAY = 86400.0


class FollowUpFiguresTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("avatar", "dev", "idle"):
            registry.upsert(self.db, name, harness="codex", host=self.cfg.host,
                            cwd=self.tmp, mode="execute")
        delivered = work.add(self.db, title="Delivered", app="ameesh", actor="orch",
                             estimate="2h", footprint=[])
        work.move(self.db, delivered["id"], "build")
        work.close_merged(self.db, delivered["id"], sha="abcdef1")
        self.db.execute("UPDATE work_items SET started_at = now() - interval '3 hours'"
                        " WHERE id = %s", (delivered["id"],))
        self.db.execute("UPDATE work_item_estimates SET at = now() - interval '4 hours'"
                        " WHERE work_item_id = %s", (delivered["id"],))
        building = work.add(self.db, title="Building", app="ameesh", assignee="dev",
                            actor="dev", estimate="1h", footprint=[])
        work.move(self.db, building["id"], "build")
        work.add(self.db, title="Free", app="ameesh", footprint=[])
        self.lots = [delivered["id"], building["id"]]

    def follow_ups(self):
        """Open, due, done and dropped follow-ups of an agent without lots,
        and of an agent building one."""
        made = []
        for assignee in ("idle", "dev"):
            for _ in range(2):
                made.append(work.add(self.db, title="Check the answer", type="follow-up",
                                     app="ameesh", assignee=assignee, actor=assignee))
        work.schedule_wake(self.db, made[0]["id"], time.time() - 600, actor="idle")
        work.close(self.db, made[1]["id"], done=True, actor="idle")
        work.close(self.db, made[2]["id"], abandoned=True, actor="dev")
        return made

    def figures(self, now):
        st = storage.of(self.db)
        listing = st.operations.listing()
        stamp = {"generated_ts", "generated_at", "now", "now_ts"}

        def clean(value):
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items() if k not in stamp}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value

        return {
            "estimates": clean(estimates.report(self.db, app="ameesh", now=now)),
            "reference": clean(estimates.build_reference(
                st.work.estimate_history(app=None, limit=500), now=now)),
            "progress": [lot["id"] for lot in
                         progress.timeline(self.db, self.cfg, now=now)["lots"]],
            "stale": sorted(int(row["id"]) for row in
                            stagnation.stagnant_lots(self.db, now=now, cfg=self.cfg)),
            # work list/show and the API read `stale` through plan.annotate
            "annotated_stale": [row["stale"] for row in plan.annotate(
                self.db, [work.get(self.db, ident) for ident in self.lots], now=now)],
            # open lots of the progress page and `ameesh projects`
            "projects": {group["name"]: (group["lots_open"],
                                         len(group["lots_without_agent"]))
                         for group in projects.snapshot(self.db, now=now)["projects"]},
            "slo": sorted(repr(sorted(row.items())) for row in
                          exploitation._slo_breaches_read(self.cfg, self.db, listing, now)),
            "refusals": sorted(int(row["id"]) for row in
                               take_refusals.explain(self.cfg, self.db, now=now,
                                                     listing=listing)),
            "refusal_history": sorted(int(row["id"]) for row in take_refusals.history(
                self.cfg, self.db, since=now - 7 * DAY, until=now)),
            "agents": {row["name"]: (row.get("assigned_lot_id"), row.get("active_lots"),
                                     row.get("actionable_lots")) for row in listing},
            "debit": clean({key: value for key, value in debit.read(
                self.db, cfg=self.cfg, now=now, canons=[], jobs=False).items()
                if key not in ("ci", "turns", "budget")}),
            "decoupage": clean(file_decoupage.run(self.cfg, self.db, now=now, dry_run=True,
                                                  state={}, listing=listing, canons=[])),
            "model_evaluation": clean(model_evaluation_report.read(
                self.db, project="ameesh", now=now)),
        }

    def test_follow_ups_leave_every_figure_unchanged(self):
        now = time.time() + 2 * DAY
        before = self.figures(now)
        made = self.follow_ups()
        after = self.figures(now)
        for key in before:
            with self.subTest(figure=key):
                self.assertEqual(after[key], before[key])
        # the lots exist, and the figures did see the development lots
        self.assertEqual(len(made), 4)
        # an open follow-up idle for two days is never stale either
        for row in plan.annotate(self.db, [work.get(self.db, made[0]["id"]),
                                           work.get(self.db, made[3]["id"])], now=now):
            self.assertIsNone(row["stale"])
        self.assertTrue(any(before["annotated_stale"]))
        self.assertTrue(before["projects"])
        self.assertTrue(before["estimates"]["lots"])
        self.assertTrue(before["progress"])

    def test_shared_predicate(self):
        self.assertTrue(lifecycle.is_follow_up({"type": "follow-up"}))
        for lot in ({"type": "evolution"}, {"type": None}, {}, None):
            self.assertFalse(lifecycle.is_follow_up(lot))
        self.assertEqual(lifecycle.not_follow_up_sql("w"),
                         "w.type IS DISTINCT FROM 'follow-up'")
