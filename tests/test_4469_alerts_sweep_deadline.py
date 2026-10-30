# SPDX-License-Identifier: AGPL-3.0-only
"""#4469 — la passe d'alertes borne les balayages d'auctorialité.

Sur agents-1 (1.28.0), la porte des avis lue par la passe SLO
(`_review_gate_open` → `review_gate_for_item`) enchaînait des `git grep`
sans échéance : la passe dépassait TimeoutStartSec et systemd la tuait,
plus aucun battement du notifieur.

Témoins :
* `_slo_breaches` pose une échéance de passe, ou garde celle de l'appelant ;
* une porte dont un balayage a été refusé rend None et ne se mémorise pas ;
* un balayage lent ne retient pas la passe au-delà de son budget.
"""
from __future__ import annotations

import contextlib
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import canon, exploitation, grep_cache, lot_class, merge_queue, \
    plan_git, review_authors

HEAD = "f" * 40
BASE = "b" * 40


def _marks():
    return [{"id": 10, "kind": "frozen", "sha": HEAD, "actor": "author",
             "at_ts": 1.0}]


def _slow_git(*_args, deadline=None, **_kwargs):
    """Un `git grep` de 5 s, qui s'arrête à l'échéance comme `canon._git`."""
    wait = 5.0 if deadline is None else min(5.0, max(0.0, deadline.remaining()))
    time.sleep(wait)
    if deadline is not None and deadline.expired():
        raise canon.GitDeadline("git grep: canon deadline exhausted")
    return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")


class SlowSweepTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(canon="/canon", canon_ref="")
        self.db = object()
        self.klass = {"class": "normal", "files": [{"path": "src/ameesh/x.py"}]}

    def _gate(self, item, cache):
        def review_gate(*_args, **_kwargs):
            # comme `_carried_from_previous` : une couche lit l'échéance en doute
            try:
                review_authors._sweep_grep("/repo", HEAD + "^", ("-h", "-I", "-F"),
                                           ["un = 1"], "git grep -h -I -F -f - x")
            except review_authors.SweepDeadline:
                return {"ok": False, "doubt": True}
            return {"ok": True}

        with mock.patch.object(plan_git, "latest_freeze",
                               return_value={"sha": HEAD}), \
                mock.patch.object(review_authors, "_local_object_repo",
                                  side_effect=lambda _repo: contextlib.nullcontext(
                                      ("/local-reader", {}))), \
                mock.patch.object(lot_class, "repo_for_freeze",
                                  return_value=("/repo", HEAD)), \
                mock.patch.object(lot_class, "default_base",
                                  return_value="origin/main"), \
                mock.patch.object(merge_queue, "_class_base",
                                  return_value="origin/main"), \
                mock.patch.object(plan_git, "_commit", return_value=BASE), \
                mock.patch.object(lot_class, "of_lot", return_value=self.klass), \
                mock.patch.object(merge_queue, "review_gate",
                                  side_effect=review_gate):
            return merge_queue.review_gate_for_item(
                self.db, self.cfg, item, _marks(), gate_cache=cache, now=1000.0)

    def test_the_pass_opens_its_own_deadline(self):
        seen = []

        def read(*_args, **_kwargs):
            seen.append(review_authors.active_sweep())
            return []

        with mock.patch.object(exploitation, "_slo_breaches_read", side_effect=read):
            exploitation._slo_breaches(self.cfg, self.db, [], 0.0)
            with review_authors.sweep_deadline(7, "caller") as outer:
                exploitation._slo_breaches(self.cfg, self.db, [], 0.0)
        self.assertEqual(seen[0].budget, exploitation.ALERTS_AUTHORS_SWEEP_BUDGET_S)
        self.assertIs(seen[1], outer)
        self.assertIsNone(review_authors.active_sweep())

    def test_a_slow_sweep_does_not_hold_the_pass_past_its_budget(self):
        cache: dict = {}
        gates = []

        def read(*_args, **_kwargs):
            for lot in range(10):
                gates.append(self._gate({"id": 100 + lot, "app": "ameesh"}, cache))
            return []

        started = time.monotonic()
        with mock.patch.object(exploitation, "ALERTS_AUTHORS_SWEEP_BUDGET_S", 0.2), \
                mock.patch.object(exploitation, "_slo_breaches_read",
                                  side_effect=read), \
                mock.patch.object(grep_cache, "neutral_attributes",
                                  return_value={}), \
                mock.patch.object(plan_git, "_git", side_effect=_slow_git):
            exploitation._slo_breaches(self.cfg, self.db, [], 0.0)
        self.assertLess(time.monotonic() - started, 2.0)
        # doute : aucune porte ne conclut, aucun verdict n'est mémorisé
        self.assertEqual(gates, [None] * 10)
        self.assertEqual(cache, {})

    def test_a_live_sweep_still_concludes_and_caches(self):
        cache: dict = {}
        fast = mock.Mock(return_value=SimpleNamespace(returncode=1, stdout=b"",
                                                      stderr=b""))
        with review_authors.sweep_deadline(60, "alerts"), \
                mock.patch.object(grep_cache, "neutral_attributes",
                                  return_value={}), \
                mock.patch.object(plan_git, "_git", fast):
            gate = self._gate({"id": 4469, "app": "ameesh"}, cache)
        self.assertEqual(gate, {"ok": True})
        self.assertIn("4469", cache)
        self.assertEqual(fast.call_args.args[0], "/local-reader")
        self.assertEqual(fast.call_args.args[1], "grep")


if __name__ == "__main__":
    unittest.main()
