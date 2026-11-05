# SPDX-License-Identifier: AGPL-3.0-only
"""#5854: a live periodic canon sync keeps its claim; completion signs the
snapshot actually applied.

Two defects are pinned here:

* (a) the claim was a plain ``interval`` lease: a sync slower than the
  interval could be replaced while it was still running, and its completion
  was lost (the token had changed). A live claimant now renews its claim, so
  a second executor waits; a dead claimant still expires after the interval.
* (b) ``periodic_sync`` signed the canon it read on entry, while ``sync`` may
  ``catch_up`` to a newer commit. The published signature then described a
  snapshot older than the registry actually applied: the next executor read
  a mismatch and closed the host gate until the next claim.

Sensitive class (canon, runner): review by another family.
"""
from __future__ import annotations

import threading
import time
import types
from unittest import mock

from ameesh import canon_sync, storage

from .support import PgTestCase


def _canon(commit: str = "abc") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id="canon5854", is_default=True, untrusted=False, readable=True,
        sources=[types.SimpleNamespace(member="root", repo="/clone",
                                       commit=commit, mode="git")],
        root="/clone", load_findings=[])


class LiveClaimTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")

    def test_a_live_claimant_is_not_replaced_after_the_interval(self):
        started, release = threading.Event(), threading.Event()
        reports, errors, messages = [], [], []

        def slow_sync(db, canon, host):
            started.set()
            if not release.wait(30):
                raise TimeoutError("test sync did not resume")
            storage.of(db).canon.record_state(
                host, "ok", root="", source="", commit="abc", good=True, diagnostic="")
            return canon_sync.SyncReport(host, "root", False, [], [])

        def worker():
            conn = self.connect()
            try:
                reports.append(canon_sync.periodic_sync(
                    conn, _canon(), "host5854", 2.0,
                    on_incomplete=messages.append))
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                errors.append(exc)
            finally:
                conn.close()

        with mock.patch.object(canon_sync, "state_key", return_value=""), \
                mock.patch.object(canon_sync, "configured_ref", return_value="origin/main"), \
                mock.patch.object(canon_sync, "sync", side_effect=slow_sync):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(started.wait(15), "the periodic sync did not start")
                # Past the 2 s interval since the claim: only the live
                # claimant's renewals can keep the claim from being stolen.
                time.sleep(2.6)
                self.assertFalse(
                    storage.of(self.db).canon.claim_sync("host5854", "", "thief", 2.0),
                    "a live claimant's sync claim was replaced after the interval")
            finally:
                release.set()
                thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(reports), 1)
        self.assertEqual(messages, [])
        self.assertIsNotNone(storage.of(self.db).canon.shared_sync("host5854", ""))

    def test_only_the_current_claim_token_renews_the_claim(self):
        store = storage.of(self.db).canon
        self.assertTrue(store.claim_sync("host5854", "", "live", 300))
        self.db.execute(
            "UPDATE canon_state SET sync_claimed_at = now() - interval '301 seconds' "
            "WHERE host = 'host5854' AND canon = ''")
        self.assertFalse(store.renew_sync("host5854", "", "thief"))
        self.assertTrue(store.renew_sync("host5854", "", "live"))
        self.assertFalse(store.claim_sync("host5854", "", "thief", 300))
        self.assertFalse(store.renew_sync("host5854", "", "thief"))


class AppliedSignatureTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")

    def test_completion_publishes_the_catch_up_snapshot(self):
        entered, applied = _canon("abc"), _canon("def")
        messages = []

        def fake_sync(db, canon, host):
            # As a real `sync` does after `catch_up`: it applied `applied`,
            # not the canon the caller read on entry.
            storage.of(db).canon.record_state(
                host, "ok", root="", source="", commit="def", good=True, diagnostic="")
            return canon_sync.SyncReport(
                host, "root", False, [], [],
                applied_signature=canon_sync._sync_signature(db, applied))

        with mock.patch.object(canon_sync, "state_key", return_value=""), \
                mock.patch.object(canon_sync, "configured_ref", return_value="origin/main"), \
                mock.patch.object(canon_sync, "sync", side_effect=fake_sync):
            report = canon_sync.periodic_sync(
                self.db, entered, "host5854", 300, on_incomplete=messages.append)
            self.assertIsNotNone(report)
            expected = canon_sync._sync_signature(self.db, applied)
            entered_signature = canon_sync._sync_signature(self.db, entered)
        shared = storage.of(self.db).canon.shared_sync("host5854", "")
        self.assertIsNotNone(shared)
        self.assertNotEqual(entered_signature, expected)
        self.assertEqual(shared["sync_signature"], expected)
        # The next executor reads the applied canon: it reuses the completed
        # result instead of reporting a snapshot mismatch and closing the gate.
        with mock.patch.object(canon_sync, "state_key", return_value=""), \
                mock.patch.object(canon_sync, "configured_ref", return_value="origin/main"):
            reused = canon_sync.periodic_sync(
                self.db, applied, "host5854", 300, on_incomplete=messages.append)
        self.assertIsNotNone(reused)
        self.assertTrue(reused.shared)
        self.assertEqual(messages, [])
