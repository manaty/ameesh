# SPDX-License-Identifier: AGPL-3.0-only
"""GitHub silence is observable even when the designated observer is down."""
from __future__ import annotations

import dataclasses
from unittest import mock

from .support import PgTestCase
from ameesh import exploitation, github_observation, notify, storage


REPO = "acme/widget"
NOW = 1800000000.0
FRESH_S = exploitation.CI_GUARD_FRESH_S


class GithubObservationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE ci_queue_state")
        self.github_cfg = dataclasses.replace(
            self.cfg, github={"projects": {"widget": REPO}})

    def reading(self, repo=REPO, *, seen=NOW):
        storage.of(self.db).ci.observe_queue(
            repo, oldest_id=None, oldest_at=None, queued=0)
        self.db.execute("UPDATE ci_queue_state SET seen_at = to_timestamp(%s) "
                        "WHERE repo = %s", (seen, repo))

    def alerts(self, cfg=None, now=NOW):
        return github_observation.alerts(
            cfg or self.github_cfg, self.db, now, fresh_s=FRESH_S)

    def test_stale_reading_names_repo_time_and_checks(self):
        self.reading(seen=NOW - FRESH_S - 1)
        (alert,) = self.alerts()
        self.assertEqual(alert["type"], "github_observation_stale")
        self.assertEqual(alert["repo"], REPO)
        self.assertEqual(alert["last_seen_ts"], NOW - FRESH_S - 1)
        for text in (REPO, exploitation._stamp(NOW - FRESH_S - 1),
                     "github.host (not designated)", "ameesh-notify", "gh token"):
            self.assertIn(text, alert["detail"])

    def test_fresh_empty_queue_and_exact_threshold_are_healthy(self):
        for seen in (NOW, NOW - FRESH_S):
            with self.subTest(seen=seen):
                self.reading(seen=seen)
                self.assertEqual(self.alerts(), [])

    def test_never_observed_alerts_on_first_pass_without_invented_time(self):
        (first,) = self.alerts()
        (later,) = self.alerts(now=NOW + 3600)
        self.assertIsNone(first["since"])
        self.assertIsNone(first["last_seen_ts"])
        self.assertIn("never recorded", first["detail"])
        self.assertEqual(exploitation.alert_key(first), exploitation.alert_key(later))

    def test_each_configured_repo_once_case_insensitive(self):
        cfg = dataclasses.replace(self.github_cfg, github={"projects": {
            "widget": REPO, "alias": {"repo": "ACME/Widget"},
            "another": "acme/another"}})
        self.reading(repo="ACME/WIDGET")
        self.reading(repo="acme/unconfigured", seen=NOW - 7200)
        self.assertEqual([a["repo"] for a in self.alerts(cfg)], ["acme/another"])
        self.assertEqual(len(self.alerts(cfg, now=NOW + FRESH_S + 1)), 2)

    def test_no_projects_is_disabled(self):
        with mock.patch.object(github_observation.storage, "of") as store:
            self.assertEqual(self.alerts(self.cfg), [])
        store.assert_not_called()

    def test_any_notifier_raises_deduplicates_and_resolves(self):
        """Exercise the real alerts→notify path, with no network or real sends."""
        self.reading(seen=NOW - FRESH_S - 1)
        sender = notify.DrySender()
        ncfg = notify.parse_config({"default_human": "human:owner",
                                   "default": ["desktop"]})
        state = notify.empty_state()
        for host in (None, "observer-host"):
            with self.subTest(designated_host=host):
                cfg = dataclasses.replace(self.github_cfg, github={
                    "projects": {"widget": REPO}, "host": host})
                notifier = notify.Notifier(cfg, ncfg, state, sender=sender,
                                           clock=lambda: NOW, log=lambda _: None)
                # The designated observer's periodic jobs stay out of this
                # test: only the common alert pass reads the shared DB state.
                with mock.patch.object(notifier, "_github_issues"), \
                        mock.patch.object(notifier, "_github_inputs"), \
                        mock.patch.object(notifier, "_ci_failures"):
                    records = notifier.run_pass(self.db)
                own = [r for r in records if r["type"] == "github_observation_stale"]
                self.assertEqual([r["event"] for r in own],
                                 ["raised"] if host is None else [])
        self.assertEqual(len(sender.sent), 1)
        self.assertIn(REPO, sender.sent[0][1].body)
        self.reading()
        with mock.patch.object(notifier, "_github_issues"), \
                mock.patch.object(notifier, "_github_inputs"), \
                mock.patch.object(notifier, "_ci_failures"):
            records = notifier.run_pass(self.db)
        own = [r for r in records if r["type"] == "github_observation_stale"]
        self.assertEqual([r["event"] for r in own], ["resolved"])
        self.assertEqual(len(sender.sent), 2)
