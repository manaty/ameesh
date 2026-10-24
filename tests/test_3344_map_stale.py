# SPDX-License-Identifier: AGPL-3.0-only
"""#3344 (panier N de #3343) : une carte de mesure périmée n'est plus
silencieuse.

La carte du 17/10 est restée seule sept jours : `scripts/test-plan.py` le
disait en annotation CI, aucune alerte ameesh ne le voyait. Le relevé de la CI
(`ci.Watcher`) lit au plus une fois par heure la dernière exécution réussie de
`carte.yml` sur la base, l'écrit dans `ci_map_state` (0480), et `ameesh
alerts` en tire `map_stale` au-delà de 48 h.
"""
from __future__ import annotations

import time
from unittest import mock

from .support import PgTestCase
from ameesh import ci, exploitation, notify, plan_github, storage


REPO = "acme/widget"
DAY = 86400.0


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class _Api:
    """Le client GitHub du relevé : workflows découverts et dernière carte."""

    def __init__(self, *, workflows=("tests.yml", "carte.yml"), runs=None, error=None):
        self.workflows = workflows
        self.runs = [] if runs is None else runs
        self.error = error
        self.calls: list[str] = []

    def __call__(self, path):
        self.calls.append(path)
        if path.startswith("repos/%s/actions/workflows?" % REPO):
            return {"workflows": [{"path": ".github/workflows/" + w, "state": "active"}
                                  for w in self.workflows]}
        if "/actions/workflows/carte.yml/runs" in path:
            if self.error is not None:
                raise self.error
            return {"workflow_runs": self.runs}
        raise AssertionError("appel inattendu : " + path)

    def map_calls(self):
        return [c for c in self.calls if "/carte.yml/runs" in c]


class MapStaleTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE ci_map_state")
        self.st = storage.of(self.db)

    def watcher(self, api):
        self.logs: list[str] = []
        return ci.Watcher(self.cfg, api=api, log=self.logs.append)

    def row(self):
        rows = self.st.ci.maps()
        return rows[0] if rows else None

    def age_reading(self, seconds):
        self.db.execute("UPDATE ci_map_state SET seen_at = now() - %s * interval '1 second'",
                        (seconds,))

    # -- relevé -------------------------------------------------------------
    def test_reads_last_successful_map_run_of_the_base(self):
        published = time.time() - 3 * DAY
        api = _Api(runs=[{"id": 42, "head_sha": "996f7180" + "0" * 32,
                          "updated_at": _iso(published)}])
        self.watcher(api)._map(self.db, REPO, ("main",))
        (call,) = api.map_calls()
        self.assertIn("branch=main", call)
        self.assertIn("status=success", call)
        row = self.row()
        self.assertEqual((row["repo"], row["branch"], row["run_id"]), (REPO, "main", 42))
        self.assertTrue(row["head_sha"].startswith("996f7180"))
        self.assertAlmostEqual(row["published_ts"], round(published), delta=1)

    def test_repository_without_map_workflow_is_not_read(self):
        api = _Api(workflows=("tests.yml",))
        self.watcher(api)._map(self.db, REPO, ("main",))
        self.assertEqual(api.map_calls(), [])
        self.assertIsNone(self.row())

    def test_map_workflow_removed_forgets_the_reading(self):
        """Relecture cx1 : sans quoi le relevé vieillirait en alerte perpétuelle."""
        self.watcher(_Api(runs=[]))._map(self.db, REPO, ("main",))
        self.assertIsNotNone(self.row())
        self.watcher(_Api(workflows=("tests.yml",)))._map(self.db, REPO, ("main",))
        self.assertIsNone(self.row())

    def test_no_successful_run_is_recorded_without_a_date(self):
        self.watcher(_Api(runs=[]))._map(self.db, REPO, ("main",))
        row = self.row()
        self.assertIsNone(row["published_ts"])
        self.assertIsNone(row["run_id"])

    def test_read_at_most_once_per_period(self):
        api = _Api(runs=[{"id": 1, "head_sha": "a", "updated_at": _iso(time.time())}])
        watcher = self.watcher(api)
        watcher._map(self.db, REPO, ("main",))
        watcher._map(self.db, REPO, ("main",))
        self.assertEqual(len(api.map_calls()), 1)
        self.age_reading(ci.MAP_READ_S + 5)
        watcher._map(self.db, REPO, ("main",))
        self.assertEqual(len(api.map_calls()), 2)

    def test_github_error_is_logged_and_keeps_the_previous_reading(self):
        published = time.time() - DAY
        self.watcher(_Api(runs=[{"id": 7, "head_sha": "b",
                                 "updated_at": _iso(published)}]))._map(
            self.db, REPO, ("main",))
        self.age_reading(ci.MAP_READ_S + 5)
        watcher = self.watcher(_Api(error=RuntimeError("HTTP 502")))
        watcher._map(self.db, REPO, ("main",))
        self.assertEqual(self.row()["run_id"], 7)
        self.assertTrue(any("carte.yml" in line and "502" in line for line in self.logs))

    def test_rate_limit_stops_the_pass(self):
        api = _Api(error=plan_github.GithubRateLimited("x", reset=None))
        with self.assertRaises(plan_github.GithubRateLimited):
            self.watcher(api)._map(self.db, REPO, ("main",))

    def test_watch_reads_the_map_after_the_guard(self):
        watcher = self.watcher(_Api())
        with mock.patch.object(watcher, "followed_workflows", return_value=()), \
                mock.patch.object(watcher, "_listed", return_value=[]), \
                mock.patch.object(watcher, "_unblock", return_value=[]), \
                mock.patch.object(watcher, "_catch_up", return_value=[]), \
                mock.patch.object(watcher, "_refresh_bases"), \
                mock.patch.object(watcher, "_guard"), \
                mock.patch.object(watcher, "_map") as read:
            watcher.watch(self.db, REPO)
        read.assert_called_once_with(self.db, REPO, watcher.bases(REPO))

    def test_prune_keeps_the_active_perimeter_only(self):
        for repo in (REPO, "acme/removed"):
            self.st.ci.observe_map(repo, branch="main", run_id=1, head_sha="a",
                                   published_at=_iso(time.time()))
        self.st.ci.prune_maps([])
        self.assertEqual(len(self.st.ci.maps()), 2)
        self.st.ci.prune_maps([REPO.upper()])
        self.assertEqual([r["repo"] for r in self.st.ci.maps()], [REPO])

    # -- alerte -------------------------------------------------------------
    def alerts(self, now=None):
        return exploitation._map_stale(self.db, time.time() if now is None else now)

    def observe(self, published):
        self.st.ci.observe_map(REPO, branch="main", run_id=40000001, head_sha="c",
                               published_at=None if published is None else _iso(published))

    def test_map_older_than_48_h_alerts_with_its_age_and_the_remedy(self):
        published = time.time() - 163 * 3600
        self.observe(published)
        (alert,) = self.alerts()
        self.assertEqual(alert["type"], "map_stale")
        self.assertEqual(alert["repo"], REPO)
        self.assertEqual(alert["threshold"], round(exploitation.MAP_STALE_S))
        self.assertAlmostEqual(alert["since"], round(published), delta=1)
        for text in (REPO, "40000001", "163 h ago", "over 48 h", "full suite",
                     "dispatch carte.yml on main"):
            self.assertIn(text, alert["detail"])

    def test_map_under_48_h_is_healthy(self):
        self.observe(time.time() - 47 * 3600)
        self.assertEqual(self.alerts(), [])

    def test_never_published_alerts_without_an_invented_date(self):
        self.observe(None)
        (alert,) = self.alerts()
        self.assertIsNone(alert["since"])
        self.assertIn("no successful carte.yml run", alert["detail"])

    def test_old_reading_alerts_with_an_unknown_age(self):
        """Relecture cx1 (N/P2) : une erreur propre à carte.yml laisse la file
        fraîche ; le relevé de carte vieilli ne doit pas taire l'alerte."""
        for published in (time.time() - 10 * DAY, time.time() - 3600, None):
            with self.subTest(published=published):
                self.observe(published)
                self.age_reading(exploitation.MAP_READ_FRESH_S + 5)
                (alert,) = self.alerts()
                self.assertEqual(alert["type"], "map_stale")
                self.assertIsNone(alert["value"])
                self.assertIn("map age unknown", alert["detail"])
                self.assertIn("carte.yml errors", alert["detail"])

    def test_same_alert_from_one_pass_to_the_next(self):
        self.observe(time.time() - 3 * DAY)
        (first,) = self.alerts()
        (later,) = self.alerts(now=time.time() + 600)
        self.assertEqual(exploitation.alert_key(first), exploitation.alert_key(later))

    def test_declared_and_pushed_by_default(self):
        self.assertIn("map_stale", exploitation.ALERT_TYPES)
        self.assertIn("map_stale", notify.DEFAULT_TYPES)
        self.observe(time.time() - 3 * DAY)
        with mock.patch.object(exploitation, "_map_stale",
                               wraps=exploitation._map_stale) as seen:
            out = exploitation.alerts(self.cfg, self.db)
        seen.assert_called_once()
        self.assertIn("map_stale", [a["type"] for a in out])
