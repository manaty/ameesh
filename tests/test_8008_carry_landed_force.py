# SPDX-License-Identifier: AGPL-3.0-only
"""#8008 — a forced replay never mails the seats of a carry that landed.

The executor pass (`verdict_carry.run_pass`) and the deferred recovery decide
`designate(force=True)` on a reading taken before the CLI's carry commits. The
carry then writes its final mark (no `provisional`) under the lot lock; the
forced selection must keep the carried seats instead of sending them a delta
request (#7697 freeze 44692, #7865, #7694, #7913).
"""
from __future__ import annotations

from unittest import mock

from ameesh import review_designation_activation as act
from ameesh import verdict_carry

from .test_2487_designation_carry import _Fixture


class CarryLandedForceTest(_Fixture):
    def _realigned_freeze(self):
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")
        frozen = self.repo.merge_target()
        mark, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "carried", outcome)
        effective = act._effective_mark(self.db, self.lot, mark["id"])
        self.assertTrue(effective[2].get("provisional"), effective)
        return mark, int(effective[0])

    def _carry(self):
        carried = verdict_carry.carry(self.cfg, self.db, self.lot, repo=self.repo.root)
        self.assertEqual(carried["result"], "carried", carried)

    def _kept(self, mark):
        self.assertEqual(self.review_mail(), [])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        effective = act._effective_mark(self.db, self.lot, mark["id"])
        self.assertEqual(effective[1], "carried")
        self.assertFalse(effective[2].get("provisional"), effective)

    def test_forced_replay_after_the_carry_keeps_the_carried_seats(self):
        mark, provisional = self._realigned_freeze()
        self._carry()
        applied = act.designate(self.cfg, self.db, self.lot, force=True,
                                expected_mark=provisional)
        self.assertEqual(applied["state"], "carried", applied)
        self._kept(mark)

    def test_carry_landing_during_the_forced_selection_keeps_the_seats(self):
        # The race of #7697: the forced replay reads the provisional mark,
        # the carry commits while it prepares, the selection lock sees it.
        mark, provisional = self._realigned_freeze()
        real = act.prepare

        def carry_then_prepare(*args, **kwargs):
            self._carry()
            return real(*args, **kwargs)

        with mock.patch.object(act, "prepare", side_effect=carry_then_prepare):
            applied = act.designate(self.cfg, self.db, self.lot, force=True,
                                    expected_mark=provisional)
        self.assertEqual(applied["state"], "carried", applied)
        self._kept(mark)

    def test_executor_pass_does_not_repair_an_accomplished_carry(self):
        mark, _ = self._realigned_freeze()
        self._carry()
        failure = {"lot": self.lot, "result": "not-carried", "frozen": "",
                   "target": "main", "tip": self.repo.tip, "carried": [],
                   "refused": [], "detail": ""}
        with mock.patch.object(verdict_carry, "carry", return_value=failure):
            verdict_carry.run_pass(self.cfg, self.db)
        self._kept(mark)

    def test_forced_replay_of_the_read_provisional_mark_still_designates(self):
        _, provisional = self._realigned_freeze()
        applied = act.designate(self.cfg, self.db, self.lot, force=True,
                                expected_mark=provisional)
        self.assertEqual(applied["state"], "designated", applied)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["rev1", "rev2"])

    def test_executor_pass_racing_the_cli_carry_sends_no_request(self):
        # #7697 end to end: this host's carry failed, the pass reads the
        # provisional mark, the CLI's carry lands while the forced selection
        # prepares; the kept seats get no delta request.
        mark, _ = self._realigned_freeze()
        real_carry, real_prepare = verdict_carry.carry, act.prepare
        failure = {"lot": self.lot, "result": "not-carried", "frozen": "",
                   "target": "main", "tip": self.repo.tip, "carried": [],
                   "refused": [], "detail": ""}

        def cli_carry_then_prepare(*args, **kwargs):
            landed = real_carry(self.cfg, self.db, self.lot, repo=self.repo.root)
            self.assertEqual(landed["result"], "carried", landed)
            return real_prepare(*args, **kwargs)

        with mock.patch.object(verdict_carry, "carry", return_value=failure), \
                mock.patch.object(act, "prepare", side_effect=cli_carry_then_prepare):
            report = verdict_carry.run_pass(self.cfg, self.db)
        mine = [row for row in report if row["lot"] == self.lot]
        self.assertEqual([row.get("designation") for row in mine], ["carried"], report)
        self._kept(mark)
