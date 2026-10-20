# SPDX-License-Identifier: AGPL-3.0-only
"""Mixed Codex sessions cannot hide a saturated short window (#2839)."""
from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from ameesh import accounts, account_turn, cost
from ameesh.codex_gauges import AmbiguousCodexGauges

from .test_cost import Sandbox, write
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

    def test_cached_reading_invalidated_by_same_size_rewrite(self):
        path = journal(self.codex, "cached", event(self.now, short=100))
        before = os.stat(path)
        self.assertTrue(self.book().codex_gauges().in_overage(self.now))
        with open(path) as stream:
            content = stream.read().replace('"used_percent": 100', '"used_percent": 1.0')
        with open(path, "w") as stream:
            stream.write(content)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertFalse(self.book().codex_gauges().in_overage(self.now))

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

    def test_contradictory_weekly_resets_refused(self):
        journal(self.codex, "a", event(self.now))
        journal(self.codex, "b", event(self.now, reset_shift=743))
        with self.assertRaisesRegex(AmbiguousCodexGauges, "resets"):
            self.book().codex_gauges()

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
