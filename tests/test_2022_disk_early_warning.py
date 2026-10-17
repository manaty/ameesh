# SPDX-License-Identifier: AGPL-3.0-only
"""#2022: early disk warning — a volume used beyond 85 %, under 15 GiB free,
or whose trend reaches the pause floor within a day raises
`host_disk_soon_full` before `min_disk_free` pauses the agents (16/10:
agents-1 at 99 %, 11 agents paused, nobody warned). A warning only: the
pressure verdict does not change. Host alerts sort first."""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import exploitation, notify, resources, storage

GIB = 1024 ** 3
HOUR = 3600.0


def series(start_free: int, per_hour: int, hours: int, *, path: str = "/",
           end_ts: float = 99 * HOUR) -> list[dict]:
    """Hourly readings of one volume ending at `end_ts` (an hour before
    `reading`), its free space
    falling by `per_hour` bytes an hour."""
    out = []
    for i in range(hours):
        free = start_free - per_hour * i
        out.append({"id": i + 1, "sampled_ts": end_ts - (hours - 1 - i) * HOUR,
                    "volumes": [{"path": path, "paths": [path], "disk_free_bytes": free,
                                 "disk_total_bytes": 500 * GIB}]})
    return out


def reading(free: int, total: int | None = 500 * GIB, path: str = "/",
            ts: float = 100 * HOUR) -> dict:
    return {"id": 99, "host": "pc", "sampled_ts": ts,
            "volumes": [{"path": path, "paths": [path], "disk_free_bytes": free,
                         "disk_total_bytes": total}]}


class ThresholdTest(unittest.TestCase):
    def test_defaults_and_declared(self):
        limits = resources.thresholds(None)
        self.assertEqual(limits["warn_disk_used"], 0.85)
        self.assertEqual(limits["warn_disk_free"], 15 * GIB)
        limits = resources.thresholds({"resources": {"warn_disk_used": "90%",
                                                     "warn_disk_free": "40GiB"}})
        self.assertEqual(limits["warn_disk_used"], 0.9)
        self.assertEqual(limits["warn_disk_free"], 40 * GIB)

    def test_warning_never_pauses(self):
        limits = resources.thresholds(None)
        low = dict(reading(10 * GIB), disk_free_bytes=10 * GIB)
        self.assertTrue(resources.disk_warnings(low, limits))
        self.assertFalse(resources.pressure(low, limits=limits)["blocked"])

    def test_strictest_declared_across_fiches(self):
        class Fiche:
            def __init__(self, resources_):
                self.policy = mock.Mock(resources=resources_, max_agents=None, lend=None)

        class Canon:
            def __init__(self, ident, resources_):
                self.id, self.fiche = ident, Fiche(resources_)

            def host(self, name):
                return self.fiche

        limits = resources.host_limits(
            [Canon("a", {"warn_disk_free": "10GiB", "warn_disk_used": "90%"}),
             Canon("b", {"warn_disk_free": "30GiB", "warn_disk_used": "80%"})], "pc")
        self.assertEqual(limits["limits"]["warn_disk_free"], 30 * GIB)
        self.assertEqual(limits["limits"]["warn_disk_used"], 0.8)


class WarningsTest(unittest.TestCase):
    limits = resources.thresholds(None)

    def test_used_share_and_free_space(self):
        self.assertEqual(resources.disk_warnings(reading(100 * GIB), self.limits), [])
        used = resources.disk_warnings(reading(70 * GIB), self.limits)
        self.assertEqual(used[0]["reasons"], ["used"])
        self.assertFalse(used[0]["critical"])
        # a small disk: under 15 GiB free before 85 % used
        small = resources.disk_warnings(reading(14 * GIB, total=50 * GIB), self.limits)
        self.assertEqual(small[0]["reasons"], ["free"])
        # without a total (reading from before #2022): the free space alone
        old = resources.disk_warnings(reading(14 * GIB, total=None), self.limits)
        self.assertEqual(old[0]["reasons"], ["free"])
        self.assertIsNone(old[0]["used"])

    def test_under_the_pause_floor_is_critical(self):
        found = resources.disk_warnings(reading(GIB), self.limits)
        self.assertTrue(found[0]["critical"])

    def test_trend_reaches_the_floor_within_a_day(self):
        # 200 GiB free, losing 10 GiB an hour: the 2 GiB floor in ~20 h,
        # although neither the share nor the free space warns yet
        history = series(260 * GIB, 10 * GIB, 6)
        now = reading(200 * GIB)
        found = resources.disk_warnings(now, self.limits, history)
        self.assertEqual(found[0]["reasons"], ["trend"])
        self.assertAlmostEqual(found[0]["floor_eta_s"] / HOUR, 19.8, places=1)
        self.assertFalse(found[0]["critical"])
        # losing 40 GiB an hour: within 6 h, critical
        fast = resources.disk_warnings(reading(200 * GIB), self.limits,
                                       series(400 * GIB, 40 * GIB, 6))
        self.assertTrue(fast[0]["critical"])

    def test_no_trend_without_enough_history_or_when_stable(self):
        self.assertEqual(resources.disk_warnings(reading(200 * GIB), self.limits,
                                                 series(260 * GIB, 10 * GIB, 3)), [])
        self.assertEqual(resources.disk_warnings(reading(200 * GIB), self.limits,
                                                 series(200 * GIB, 0, 8)), [])
        # growing free space (cleanup): no trend
        self.assertEqual(resources.disk_warnings(reading(200 * GIB), self.limits,
                                                 series(100 * GIB, -10 * GIB, 8)), [])
        # far beyond a day: no trend
        self.assertEqual(resources.disk_warnings(reading(200 * GIB), self.limits,
                                                 series(205 * GIB, GIB, 6)), [])

    def test_floor_eta_fit(self):
        points = [(i * HOUR, 100 * GIB - i * 5 * GIB) for i in range(7)]
        self.assertAlmostEqual(resources.disk_floor_eta(points, 0) / HOUR, 14.0)
        # too short a span
        self.assertIsNone(resources.disk_floor_eta(
            [(i * 60.0, 100 - i) for i in range(10)], 0))
        self.assertEqual(resources.disk_floor_eta(points, 200 * GIB), 0.0)

    def test_series_of_a_reading_from_before_0359(self):
        history = [{"sampled_ts": i * HOUR, "volumes": [], "disk_path": "/w",
                    "disk_free_bytes": 100 - i} for i in range(3)]
        self.assertEqual(resources._volume_series(history, "/w"),
                         [(0.0, 100), (HOUR, 99), (2 * HOUR, 98)])


class AlertTest(unittest.TestCase):
    def alerts(self, current: dict, history=()) -> list[dict]:
        store = mock.MagicMock()
        store.hosts.current.return_value = [current]
        store.hosts.since.return_value = list(history) + [current]
        store.turn_resources.orphans.return_value = []
        store.turn_resources.stale_running.return_value = []
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch("ameesh.canon.load_configured", return_value=[]), \
                mock.patch.object(exploitation, "_housekeeping_alerts", return_value=[]):
            return exploitation._host_alerts(None, None, now=100 * HOUR)

    def test_raised_before_the_pause(self):
        found = self.alerts(reading(60 * GIB))
        self.assertEqual([a["type"] for a in found], ["host_disk_soon_full"])
        alert = found[0]
        self.assertEqual(alert["host"], "pc")
        self.assertEqual(alert["paths"], ["/"])
        self.assertFalse(alert["urgent"])
        self.assertIn("/ 88 % used, 60.0 GiB free", alert["detail"])
        self.assertIn("before the 2.0 GiB floor that pauses the agents", alert["detail"])

    def test_trend_in_detail_and_one_episode_on_critical(self):
        warning = self.alerts(reading(200 * GIB), series(260 * GIB, 10 * GIB, 6))[0]
        self.assertIn("floor in 19.8 h", warning["detail"])
        self.assertEqual(round(warning["floor_eta_s"] / HOUR), 20)
        critical = self.alerts(reading(GIB))[0]
        self.assertTrue(critical["urgent"])
        self.assertEqual(critical["reason"], "critique")
        self.assertEqual(exploitation.alert_key(warning), exploitation.alert_key(critical))

    def test_history_unreadable_keeps_the_thresholds(self):
        store = mock.MagicMock()
        store.hosts.since.side_effect = RuntimeError("db down")
        with mock.patch.object(storage, "of", return_value=store):
            alert = exploitation._disk_soon_full_alert(
                None, reading(10 * GIB), resources.thresholds(None))
        self.assertEqual(alert["type"], "host_disk_soon_full")

    def test_pushed_by_default_and_declared(self):
        self.assertIn("host_disk_soon_full", notify.DEFAULT_TYPES)
        self.assertIn("host_disk_soon_full", notify.TYPE_LABELS)
        self.assertIn("host_disk_soon_full", exploitation.ALERT_TYPES)
        self.assertIn("host_disk_soon_full", exploitation.ESCALATING)


class PriorityTest(unittest.TestCase):
    def test_host_alerts_sort_before_the_noise(self):
        noise = [exploitation._alert(kind, "a", None, 1, 0, "d")
                 for kind in ("long_turn", "slo_breach", "tmp_orphan")]
        host = [exploitation._alert(kind, None, None, 1, 0, "d", host="pc")
                for kind in ("host_power_low", "host_disk_soon_full", "host_pressure")]
        out = sorted(noise + host, key=exploitation._alert_sort_key)
        self.assertEqual([a["type"] for a in out][:3],
                         ["host_pressure", "host_disk_soon_full", "host_power_low"])
        self.assertEqual([a["type"] for a in out][3:],
                         ["long_turn", "slo_breach", "tmp_orphan"])


if __name__ == "__main__":
    unittest.main()
