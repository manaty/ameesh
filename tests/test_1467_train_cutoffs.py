# SPDX-License-Identifier: AGPL-3.0-only
"""Canon §11 bis: one packet per compiled epoch, including Paris DST."""
from datetime import datetime
import types
import unittest
from zoneinfo import ZoneInfo

from ameesh import train
from .test_1467_train import MemoryPorts, row


def stamp(text):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("Europe/Paris")).timestamp()


def admitted(lot, when="2026-10-15T10:00:00"):
    return dict(row(lot), queued_ts=stamp(when))


class CutoffTest(unittest.TestCase):
    def test_beta_policy_cannot_require_individual_lot_ci(self):
        with self.assertRaisesRegex(ValueError, "without per-lot CI"):
            train.Policy(enabled=True, ci_mode="targeted")

    def engine(self, rows, **policy):
        self.ports = MemoryPorts(rows)
        return train.Engine(self.ports, source="github.com/o/r", target="main",
                            policy=train.Policy(enabled=True, **policy))

    def test_compiled_packet_cutoff_cannot_be_unknown_nonfinite_or_future(self):
        now = stamp("2026-10-15T12:00:00")
        for at in (None, True, float("nan"), float("inf"), now + 1):
            with self.subTest(at=at), self.assertRaises(train.TrainError):
                train.Policy(schedule=types.SimpleNamespace(last_packet=lambda _: at)).cutoff(now)

    def test_long_ci_resumes_latest_hour_without_a_catchup_burst(self):
        schedule = types.SimpleNamespace(last_packet=lambda now: now - now % 3600)
        engine = self.engine([admitted(1)], schedule=schedule)
        first = engine.step(stamp("2026-10-15T11:00:00"))["plan"]
        self.assertEqual(engine.step(stamp("2026-10-15T14:00:00"))["result"], "ci")
        self.assertEqual(len(self.ports.probes), 1)
        self.ports.ci_by_head[first["head"]] = "green"
        self.assertEqual(engine.step(stamp("2026-10-15T14:00:01"))["result"], "landed")
        self.ports.rows = [admitted(2, "2026-10-15T12:00:00")]
        second = engine.step(stamp("2026-10-15T14:00:02"))["plan"]
        self.ports.ci_by_head[second["head"]] = "green"
        self.assertEqual(engine.step(stamp("2026-10-15T14:00:03"))["result"], "landed")
        self.ports.rows = [admitted(3, "2026-10-15T13:00:00")]
        self.assertEqual(engine.step(stamp("2026-10-15T14:00:04"))["result"], "idle")
        self.assertEqual(engine.step(stamp("2026-10-15T15:00:00"))["result"], "ci")
        self.assertEqual([e[0] for e in self.ports.effects].count("ci"), 3)

    def test_repeated_paris_hour_starts_distinct_packets_across_restart(self):
        # Explicit supplier epochs: this checks the consumer, not how #1757
        # computes the calendar. Both instants are 02:00 in Europe/Paris.
        first = datetime.fromisoformat("2026-10-25T02:00:00+02:00").timestamp()
        second = datetime.fromisoformat("2026-10-25T02:00:00+01:00").timestamp()
        schedule = types.SimpleNamespace(last_packet=lambda now:
            first if now < second else second)
        engine = self.engine([dict(row(1), queued_ts=first - 1)], schedule=schedule)
        earlier = engine.step(first + 5)["plan"]
        self.ports.ci_by_head[earlier["head"]] = "green"
        self.assertEqual(engine.step(first + 10)["result"], "landed")
        self.ports.rows = [dict(row(2), queued_ts=first + 15)]
        resumed = train.Engine(self.ports, source=engine.source, target=engine.target,
                               policy=engine.policy)
        self.assertEqual(resumed.step(second - 1)["result"], "idle")
        result = resumed.step(second + 5)
        self.assertEqual(result["result"], "ci", result)
        later = result["plan"]
        self.assertEqual([r["work_item_id"] for r in later["members"]], [2])
        self.assertNotEqual(earlier["branch"], later["branch"])
        self.assertNotEqual(earlier["id"], later["id"])
        self.ports.ci_by_head[later["head"]] = "green"
        self.assertEqual(resumed.step(second + 10)["result"], "landed")
        self.ports.rows = [dict(row(3), queued_ts=second + 15)]
        self.assertEqual(resumed.step(second + 1800)["result"], "idle")
        self.assertEqual([e[0] for e in self.ports.effects].count("ci"), 2)

    def test_pending_packet_survives_spring_gap_without_another_dispatch(self):
        first = datetime.fromisoformat("2026-03-29T01:00:00+01:00").timestamp()
        second = datetime.fromisoformat("2026-03-29T03:00:00+02:00").timestamp()
        schedule = types.SimpleNamespace(last_packet=lambda now:
            first if now < second else second)
        engine = self.engine([dict(row(1), queued_ts=first - 1)], schedule=schedule)
        original = engine.step(first + 5)["plan"]
        late = dict(row(2), queued_ts=first + 15)
        self.ports.rows.append(late)
        result = engine.step(second + 5)
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(result["plan"], original)
        self.assertEqual([e[0] for e in self.ports.effects].count("ci"), 1)
        self.ports.ci_by_head[original["head"]] = "green"
        self.assertEqual(engine.step(second + 10)["result"], "landed")
        self.ports.rows = [late]
        result = engine.step(second + 15)
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual([r["work_item_id"] for r in result["plan"]["members"]], [2])
        self.assertEqual([e[0] for e in self.ports.effects].count("ci"), 2)

    def test_withdrawn_packet_drains_its_ci_before_next_hour_can_compose(self):
        schedule = types.SimpleNamespace(last_packet=lambda now: now - now % 3600)
        engine = self.engine([admitted(1)], schedule=schedule)
        first = engine.step(stamp("2026-10-15T11:00:00"))["plan"]
        self.ports.rows = [admitted(2, "2026-10-15T11:01:00")]
        self.assertEqual(engine.step(stamp("2026-10-15T11:05:00"))["result"], "refused")
        self.assertEqual(self.ports.document["phase"], "draining")
        self.assertTrue(self.ports.held)
        self.assertEqual(engine.step(stamp("2026-10-15T12:00:00"))["result"], "draining")
        self.assertEqual(len(self.ports.probes), 1)
        self.assertEqual([e[0] for e in self.ports.effects].count("ci"), 1)
        original = self.ports.ci
        self.ports.ci = lambda plan: dict(head="f" * 40, tree=plan["tree"],
                                         status="green", complete=True)
        self.assertEqual(engine.step(stamp("2026-10-15T12:00:00"))["result"], "refused")
        self.assertEqual(self.ports.document["phase"], "draining")
        self.assertTrue(self.ports.held)
        self.ports.ci = original
        self.ports.ci_by_head[first["head"]] = "green"
        result = engine.step(stamp("2026-10-15T12:00:01"))
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual([r["work_item_id"] for r in result["plan"]["members"]], [2])
        self.assertNotEqual(result["plan"]["id"], first["id"])
        self.assertNotIn("promote", [e[0] for e in self.ports.effects])
        self.assertEqual([e[0] for e in self.ports.effects].count("ci"), 2)

    def test_default_ignores_count_and_elapsed_hour_until_cutoff(self):
        engine = self.engine([admitted(n) for n in range(1, 10)])
        self.assertEqual(engine.step(stamp("2026-10-15T10:59:59"))["result"], "idle")
        self.assertEqual(self.ports.effects, [])
        self.assertEqual(engine.step(stamp("2026-10-15T11:00:00"))["result"], "ci")
        self.assertEqual(len(self.ports.probes), 1)

    def test_packet_excludes_late_admissions_and_their_dependents(self):
        engine = self.engine([admitted(1), admitted(2, "2026-10-15T11:00:01"), admitted(3)])
        self.ports.edges = [dict(work_item_id=3, blocker_id=2, blocking=True)]
        result = engine.step(stamp("2026-10-15T11:00:10"))
        self.assertEqual(result["result"], "ci")
        self.assertEqual([r["work_item_id"] for r in result["plan"]["members"]], [1])

    def test_empty_cutoff_does_not_start_a_packet_for_a_late_lot(self):
        engine = self.engine([])
        self.assertEqual(engine.step(stamp("2026-10-15T11:00:00"))["result"], "idle")
        self.ports.rows = [admitted(1, "2026-10-15T11:01:00")]
        self.assertEqual(engine.step(stamp("2026-10-15T15:00:00"))["result"], "idle")
        self.assertEqual(engine.step(stamp("2026-10-15T23:00:00"))["result"], "ci")

    def test_landed_packet_and_restart_cannot_start_a_second_packet_at_same_cutoff(self):
        engine = self.engine([admitted(1)])
        result = engine.step(stamp("2026-10-15T11:00:00"))
        self.ports.ci_by_head[result["plan"]["head"]] = "green"
        self.assertEqual(engine.step(stamp("2026-10-15T11:05:00"))["result"], "landed")
        self.ports.rows = [admitted(2)]
        resumed = train.Engine(self.ports, source=engine.source, target=engine.target,
                               policy=engine.policy)
        self.assertEqual(resumed.step(stamp("2026-10-15T22:59:59"))["result"], "idle")
        self.assertEqual(resumed.step(stamp("2026-10-15T23:00:00"))["result"], "ci")
        self.assertEqual([e[0] for e in self.ports.effects].count("ci"), 2)

    def test_red_packet_pauses_without_any_diagnostic_or_second_ci(self):
        engine = self.engine([admitted(1), admitted(2)])
        result = engine.step(stamp("2026-10-15T11:00:00"))
        self.ports.ci_by_head[result["plan"]["head"]] = "red"
        self.assertEqual(engine.step(stamp("2026-10-15T11:05:00"))["result"], "paused")
        self.assertEqual(engine.step(stamp("2026-10-15T23:00:00"))["result"], "paused")
        self.assertEqual(len(self.ports.probes), 1)
        self.assertEqual([e[0] for e in self.ports.effects], ["publish", "ci"])
        self.assertTrue(self.ports.notices)

    def test_paris_cutoffs_follow_local_date_and_dst(self):
        cases = {
            "2026-10-15T10:59:59": "2026-10-14T23:00:00",
            "2026-10-15T11:00:00": "2026-10-15T11:00:00",
            "2026-10-16T00:01:00": "2026-10-15T23:00:00",
            "2026-10-25T10:59:59": "2026-10-24T23:00:00",
            "2026-10-25T11:00:00": "2026-10-25T11:00:00",
            "2026-03-29T10:59:59": "2026-03-28T23:00:00",
            "2026-03-29T11:00:00": "2026-03-29T11:00:00",
        }
        for now, expected in cases.items():
            with self.subTest(now=now):
                self.assertEqual(train.cutoff(stamp(now)), stamp(expected))


if __name__ == "__main__":
    unittest.main()
