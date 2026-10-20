# SPDX-License-Identifier: AGPL-3.0-only
"""Mixed Codex sessions cannot hide a saturated short window (#2839)."""
from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from ameesh import accounts, account_turn, codex_gauges, cost
from ameesh.codex_gauges import AmbiguousCodexGauges

from .test_cost import RACY_CTIME_NS, Sandbox, write
from .test_l30_accounts import _Base


def event(now, *, short=100, week=49, plan="team", reset_shift=0):
    return {"timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "payload": {"rate_limits": {
                "plan_type": plan, "credits": {"has_credits": True},
                "primary": ({"used_percent": short, "window_minutes": 300,
                             "resets_at": now + 3600} if short is not None else None),
                "secondary": {"used_percent": week, "window_minutes": 10080,
                              "resets_at": now + 86400 + reset_shift}}}}


def journal(root, name, *events, identity="account-a", mtime=None):
    path = os.path.join(root, name + ".jsonl")
    write(path, {"type": "session_meta", "payload": {"creator_account_id": identity}},
          *events)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class CodexReadingsTest(Sandbox, unittest.TestCase):
    def test_undeclared_default_account_pauses_on_ambiguity_and_recovers(self):
        agent = self.agent("default-codex", "codex")
        book = self.book()
        journal(self.codex, "team", event(self.now, short=5, week=10))
        for variant in ("plan", "identity"):
            with self.subTest(variant=variant):
                path = journal(
                    self.codex, "conflict", event(
                        self.now, short=5, week=10,
                        plan="pro" if variant == "plan" else "team"),
                    identity="other-private-account" if variant == "identity" else "account-a")
                gauges = book.gauges("codex")
                self.assertEqual(gauges, [])
                self.assertIn("ambiguous Codex account", gauges.errors["codex"])
                reason = book.pace_exceeded("codex")
                self.assertIn("pause (account (default))", reason)
                self.assertIn("ambiguous Codex account", reason)
                self.assertNotIn("other-private-account", reason)
                self.assertEqual(book.over(agent, pace=True), reason)
                self.assertEqual(book.over(agent, pace=False), reason)
                worker = mock.Mock(cfg=mock.Mock(accounts={}), agent={"harness": "codex"},
                                   name=agent)
                self.assertEqual(account_turn.choose(worker, book, pause=False), reason)
                self.assertIn("codex", book.gauges().errors)
                os.unlink(path)
                self.assertEqual(book.pace_exceeded("codex"), "")
                self.assertEqual(book.gauges("codex").errors, {})

    def test_undeclared_default_account_report_exposes_ambiguity(self):
        self.agent("default-codex", "codex")
        journal(self.codex, "team", event(self.now))
        journal(self.codex, "pro", event(self.now, short=None, plan="pro"))
        book = self.book()
        for record in (False, True):
            with self.subTest(record=record):
                rows = book.report(record=record)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["gauges"], [])
                self.assertIn("ambiguous Codex account", rows[0]["gauge_error"])
                self.assertIn(rows[0]["gauge_error"], cost.format_report(rows))

    def test_partial_newer_session_keeps_saturated_short_window(self):
        journal(self.codex, "full", event(self.now), mtime=self.now)
        journal(self.codex, "partial", event(self.now, short=None), mtime=self.now + 1)
        gauges = self.book().codex_gauges()
        self.assertEqual({g.key: g.used for g in gauges},
                         {"codex-300min": 1.0, "codex-10080min": 0.49})
        self.assertTrue(gauges.in_overage(self.now))

    def test_many_newer_partial_sessions_cannot_evict_live_short_window(self):
        journal(self.codex, "full", event(self.now), mtime=self.now)
        for i in range(cost.CODEX_FILES + 1):
            journal(self.codex, "partial-%s" % i, event(self.now, short=None),
                    mtime=self.now + i + 1)
        self.assertTrue(self.book().codex_gauges().in_overage(self.now))

    @mock.patch.object(cost, "RACY_CTIME_NS", RACY_CTIME_NS)
    def test_cached_reading_invalidated_by_same_size_rewrite(self):
        path = journal(self.codex, "cached", event(self.now, short=100))
        before = os.stat(path)
        clock_ns = [before.st_ctime_ns + 5 * RACY_CTIME_NS]
        # Hold the whole stat signature constant: deterministic coarse-ctime
        # rewrite, including a previously settled cache hit becoming racy.
        with mock.patch.object(codex_gauges.os, "stat", return_value=before), \
                mock.patch.object(codex_gauges.time, "time_ns", side_effect=lambda: clock_ns[0]):
            first = codex_gauges.session_reading(path, before.st_mtime)
            self.assertEqual(first[0][0].used, 1.0)
            self.assertIn(path, codex_gauges._READINGS)
            with open(path) as stream:
                content = stream.read().replace('"used_percent": 100', '"used_percent": 1.0')
            with open(path, "w") as stream:
                stream.write(content)
            clock_ns[0] = before.st_ctime_ns + RACY_CTIME_NS // 2
            fresh = codex_gauges.session_reading(path, before.st_mtime)
            self.assertEqual(fresh[0][0].used, 0.01)
            self.assertNotIn(path, codex_gauges._READINGS)
            clock_ns[0] = before.st_ctime_ns + 5 * RACY_CTIME_NS
            settled = codex_gauges.session_reading(path, before.st_mtime)
            self.assertEqual(settled[0][0].used, 0.01)

    @mock.patch.object(cost, "RACY_CTIME_NS", RACY_CTIME_NS)
    def test_slow_read_cannot_age_racy_snapshot_into_cache(self):
        path = journal(self.codex, "slow", event(self.now))
        before = os.stat(path)
        clock_ns = [before.st_ctime_ns + RACY_CTIME_NS // 2]
        parse = codex_gauges._session_reading

        def slow_read(*args):
            readings = parse(*args)
            clock_ns[0] = before.st_ctime_ns + 5 * RACY_CTIME_NS
            return readings

        with mock.patch.object(codex_gauges.time, "time_ns", side_effect=lambda: clock_ns[0]), \
                mock.patch.object(codex_gauges, "_session_reading", side_effect=slow_read):
            codex_gauges.session_reading(path, before.st_mtime)
        self.assertNotIn(path, codex_gauges._READINGS)

    def test_retained_short_window_expires_at_its_own_reset(self):
        observed = self.now
        journal(self.codex, "full", event(observed), mtime=observed)
        book = self.book()
        self.assertTrue(book.codex_gauges().in_overage(observed + 3599))
        partial = event(observed, short=None)
        partial["timestamp"] = datetime.fromtimestamp(
            observed + 3600, timezone.utc).isoformat()
        journal(self.codex, "after-reset", partial, mtime=observed + 3600)
        for offset in (3600, 3601):
            with self.subTest(offset=offset):
                self.now = observed + offset
                gauges = book.codex_gauges()
                short = next(g for g in gauges if g.key == "codex-300min")
                self.assertEqual(short.used, 1.0)  # Historical value stays visible.
                self.assertEqual(short.used_at(self.now), 0.0)
                self.assertFalse(gauges.in_overage(self.now))
                self.assertEqual(next(g.used_at(self.now) for g in gauges
                                      if g.key == "codex-10080min"), 0.49)

    def test_partial_event_in_same_session_keeps_short_window(self):
        journal(self.codex, "same", event(self.now), event(self.now, short=None))
        self.assertEqual(len(self.book().codex_gauges()), 2)

    def test_event_timestamp_wins_over_unrelated_file_writes(self):
        old = event(self.now - 100, short=10, week=10)
        new = event(self.now, short=100, week=49)
        # Identical reset dates, as reported by one account in the same window.
        old["payload"]["rate_limits"]["primary"]["resets_at"] = self.now + 3600
        old["payload"]["rate_limits"]["secondary"]["resets_at"] = self.now + 86400
        journal(self.codex, "new", new, mtime=self.now)
        journal(self.codex, "touched-old", old, mtime=self.now + 100)
        self.assertTrue(self.book().codex_gauges().in_overage(self.now))

    def test_mixed_plans_refused_even_with_same_creator(self):
        journal(self.codex, "team", event(self.now), mtime=self.now)
        journal(self.codex, "pro", event(self.now, short=None, plan="pro"),
                mtime=self.now + 1)
        with self.assertRaisesRegex(AmbiguousCodexGauges, "plans"):
            self.book().codex_gauges()

    def test_mixed_creator_identities_refused_without_exposing_them(self):
        journal(self.codex, "a", event(self.now))
        journal(self.codex, "b", event(self.now), identity="private-account-b")
        with self.assertRaises(AmbiguousCodexGauges) as ctx:
            self.book().codex_gauges()
        self.assertNotIn("private-account-b", str(ctx.exception))

    def test_latest_weekly_observation_wins_despite_reset_jitter_or_early_reset(self):
        for shift in (1, 743, 3 * 86400):
            with self.subTest(shift=shift):
                old = event(self.now - 100, week=90)
                old["payload"]["rate_limits"]["secondary"]["resets_at"] = self.now + 86400
                journal(self.codex, "old-%s" % shift, old, mtime=self.now + 100)
                journal(self.codex, "new-%s" % shift,
                        event(self.now, short=None, week=49, reset_shift=shift),
                        mtime=self.now)
                gauges = self.book().codex_gauges()
                week = next(g for g in gauges if g.key == "codex-10080min")
                self.assertEqual(week.used, 0.49)
                self.assertEqual(week.resets_at, self.now + 86400 + shift)
                self.assertTrue(gauges.in_overage(self.now))
                for name in ("old", "new"):
                    os.unlink(os.path.join(self.codex, "%s-%s.jsonl" % (name, shift)))

    def test_sample_caps_file_reads_and_reconciles_only_once(self):
        for i in range(codex_gauges.MAX_FILES + 7):
            journal(self.codex, str(i), event(self.now), mtime=self.now + i)
        with mock.patch.object(codex_gauges, "session_reading",
                               wraps=codex_gauges.session_reading) as read, \
                mock.patch.object(codex_gauges, "reconcile",
                                  wraps=codex_gauges.reconcile) as reconcile:
            self.assertTrue(self.book().codex_gauges().in_overage(self.now))
        self.assertEqual(read.call_count, codex_gauges.MAX_FILES)
        self.assertEqual(reconcile.call_count, 1)

    def test_bounded_sample_cannot_hide_short_window_behind_many_partial_files(self):
        journal(self.codex, "full", event(self.now), mtime=self.now)
        for i in range(codex_gauges.MAX_FILES):
            journal(self.codex, str(i), event(self.now, short=None), mtime=self.now + i + 1)
        with self.assertRaisesRegex(AmbiguousCodexGauges, "incomplete.*short window"):
            self.book().codex_gauges()

    def test_truncated_tail_cannot_hide_short_window_in_same_file(self):
        journal(self.codex, "long", event(self.now),
                {"text": "x" * (codex_gauges.TAIL_BYTES + 100)},
                event(self.now, short=None))
        with self.assertRaisesRegex(AmbiguousCodexGauges, "incomplete.*short window"):
            self.book().codex_gauges()

    def test_bounded_weekly_only_pro_reading_remains_usable(self):
        for i in range(codex_gauges.MAX_FILES + 1):
            journal(self.codex, str(i), event(self.now, short=None, plan="pro"))
        self.assertEqual(len(self.book().codex_gauges()), 1)

    def test_expired_other_plan_does_not_hold_account_forever(self):
        journal(self.codex, "old", event(self.now - 8 * 86400, plan="pro"))
        journal(self.codex, "new", event(self.now, short=5))
        self.assertFalse(self.book().codex_gauges().in_overage(self.now))

    def test_session_identity_survives_tail_truncation(self):
        journal(self.codex, "large", {"text": "x" * 2_100_000}, event(self.now))
        journal(self.codex, "other", event(self.now), identity="account-b")
        with self.assertRaises(AmbiguousCodexGauges):
            self.book().codex_gauges()


class CodexFailoverTest(_Base):
    def test_runner_with_budget_off_refuses_undeclared_ambiguous_account(self):
        now = float(int(time.time()))
        root = os.path.join(self.tmp, "default-sessions")
        journal(root, "team", event(now, short=95))
        conflict = journal(root, "pro", event(now, short=None, plan="pro"))
        runner, worker = self._worker("default-codex", "codex", {})
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db,
                             codex_sessions=root, tools={worker.name: "codex"})
        try:
            with mock.patch.object(runner, "budget_limits", return_value=mock.Mock(per_hour=0)), \
                    mock.patch("ameesh.account_turn.cost_mod.CostBook", return_value=book):
                self.assertFalse(worker.budget_ok())
                self.assertIn("ambiguous Codex account", worker._budget_reason)
                os.unlink(conflict)
                # 95% is still above the pace cap, but pace is disabled.
                self.assertTrue(worker.budget_ok())
        finally:
            worker.watchdog_stop.set()

    def test_declared_healthy_account_does_not_read_ambiguous_default(self):
        now = float(int(time.time()))
        declared = self._codex_comptes()
        for profile in declared:
            journal(os.path.join(profile["path"], "sessions"), "healthy",
                    event(now, short=1, week=1))
        runner, worker = self._worker("declared-codex", "codex", {"codex": declared})
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db,
                             tools={worker.name: "codex"})
        try:
            with mock.patch.object(book, "default_account_error",
                                   side_effect=AssertionError("default must not be read")):
                self.assertEqual(worker._own_reason(book, margin=False), "")
        finally:
            worker.watchdog_stop.set()

    def test_weekly_only_reading_after_short_reset_releases_account(self):
        observed = float(int(time.time()))
        now = observed
        declared = self._codex_comptes()
        items = accounts.parse({"codex": declared})["codex"]
        secondary = os.path.join(items[1].path, "sessions")
        journal(secondary, "full", event(observed), mtime=observed)
        book = cost.CostBook(state_dir=self.tmp, db=self.db, clock=lambda: now)
        self.assertFalse(accounts.evaluate(book, items[1], items, now, record=False).ok)
        now = observed + 3601
        partial = event(observed, short=None)
        partial["timestamp"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
        journal(secondary, "after-reset", partial, mtime=now)
        result = accounts.evaluate(book, items[1], items, now, record=False)
        self.assertTrue(result.ok, result.reason)
        self.assertFalse(result.hard)
        choice = accounts.choose(self.db, self.cfg.host, "codex", items, book,
                                 now=now, session_account="secondaire")
        self.assertEqual(choice.profile.name, "secondaire")
        allowed, reason = account_turn.summary_possible(items[1], book, items, now)
        self.assertTrue(allowed, reason)

    def test_threshold_and_ambiguity_failover_for_new_and_existing_sessions(self):
        now = float(int(time.time()))
        declared = self._codex_comptes()
        items = accounts.parse({"codex": declared})["codex"]
        primary, secondary = [os.path.join(p.path, "sessions") for p in items]
        journal(primary, "healthy", event(now, short=5, week=10), identity="primary")
        journal(secondary, "saturated", event(now), mtime=now)
        journal(secondary, "partial", event(now, short=None), mtime=now + 1)
        book = cost.CostBook(state_dir=self.tmp, db=self.db, clock=lambda: now)
        for ambiguous in (False, True):
            if ambiguous:
                journal(secondary, "other-plan", event(now, short=None, plan="pro"),
                        mtime=now + 2)
            for paced in (True, False):
                for session in (None, "secondaire"):
                    with self.subTest(ambiguous=ambiguous, paced=paced, session=session):
                        choice = accounts.choose(self.db, self.cfg.host, "codex", items,
                                                 book, now=now, paced=paced,
                                                 session_account=session)
                        self.assertEqual(choice.profile.name, "primaire")
            profile = accounts.evaluate(book, items[1], items, now, record=False)
            self.assertFalse(profile.ok)
            self.assertTrue(profile.hard)
            allowed, _ = account_turn.summary_possible(items[1], book, items, now)
            self.assertFalse(allowed)
            cfg = mock.Mock(host=self.cfg.host, accounts={"codex": declared})
            rows = accounts.report(cfg, self.db, book, now=now, record=False)
            self.assertEqual([r["account"] for r in rows if r["next"]], ["primaire"])
            self.assertFalse(next(r for r in rows if r["account"] == "secondaire")["ok"])
