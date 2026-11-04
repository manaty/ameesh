# SPDX-License-Identifier: AGPL-3.0-only
"""#5727 (avis auditeur C3 sur #5495) : le canon de l'exécuteur de staging élu.

L'exécuteur inscrit dans chacun de ses enregistrements `staging.request` le
commit du canon qu'il lit (`canon_ref`). La passe d'alertes le compare à la
tête du canon des agents : quand le plus ancien commit qui lui manque a plus
d'un jour, `staging_canon_stale` part au pilote (fiches d'hôtes ou règles
périmées ne restent pas silencieuses). Témoins sur attestations simulées et
sur un vrai dépôt git aux dates de commit fixées.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import alert_briefs, exploitation, notify, staging_executor, staging_watch
from tests import test_4880_staging_executor as fixture

DAY = 86400.0
T0 = 1_800_000_000


def _git(repo, *args, when=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_NOSYSTEM="1", HOME=repo)
    if when is not None:
        env.update(GIT_AUTHOR_DATE="@%d +0000" % when,
                   GIT_COMMITTER_DATE="@%d +0000" % when)
    return subprocess.run(["git", "-C", repo, *args], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, name, when):
    with open(os.path.join(repo, name), "w") as fh:
        fh.write(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name, when=when)
    return _git(repo, "rev-parse", "HEAD")


class DriftFindingsTest(unittest.TestCase):
    """La décision seule, sur des attestations simulées."""

    def findings(self, ref, *, head="h" * 40, since=T0, now=T0 + 2 * DAY):
        records = [("mesh", dict(canon_ref=ref, notify="mesh-design"))]
        return staging_watch.canon_drift_findings(
            records, head=head, first_ahead=lambda r, h: since, now=now)

    def test_behind_for_more_than_a_day_alerts_the_pilot(self):
        alert, = self.findings("c336c53c" + "0" * 32)
        self.assertEqual(alert["type"], "staging_canon_stale")
        self.assertEqual(alert["reason"], "mesh")
        self.assertEqual(alert["orchestrators"], ["mesh-design"])
        self.assertEqual((alert["since"], alert["value"], alert["threshold"]),
                         (T0, 2 * DAY, DAY))
        self.assertFalse(alert["urgent"])
        self.assertIn("c336c53c0000", alert["detail"])

    def test_behind_for_less_than_a_day_stays_quiet(self):
        self.assertEqual(self.findings("c" * 40, now=T0 + DAY - 1), [])
        self.assertEqual(len(self.findings("c" * 40, now=T0 + DAY)), 1)

    def test_same_commit_unknown_commit_or_no_field_stays_quiet(self):
        self.assertEqual(self.findings("h" * 40), [])
        self.assertEqual(self.findings("c" * 40, since=None), [])
        self.assertEqual(self.findings(""), [])
        self.assertEqual(self.findings("c" * 40, head=""), [])


class GitCanonTest(unittest.TestCase):
    """Le canon des agents lu dans un vrai dépôt (origin/main)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="5727-canon-")
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.join(tmp.name, "home")
        os.mkdir(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.pinned = _commit(self.repo, "a", T0 - 10 * DAY)
        self.first = _commit(self.repo, "b", T0)
        self.head = _commit(self.repo, "c", T0 + DAY / 2)
        _git(self.repo, "update-ref", "refs/remotes/origin/main", self.head)
        self.cfg = types.SimpleNamespace(canon=self.repo, canon_ref="")

    def test_first_missing_commit_dates_the_drift(self):
        first = staging_watch._first_ahead(self.repo)
        self.assertEqual(first(self.pinned, self.head), float(T0))
        self.assertIsNone(first(self.head, self.head))
        # Executor ahead of the agents' clone (it lags): nothing to say.
        self.assertIsNone(first(self.head, self.pinned))
        self.assertIsNone(first("e" * 40, self.head))

    def test_alert_from_simulated_attestations(self):
        rows = {"staging-request:mesh": [
            # The newest record is a submitted request: no canon_ref yet.
            dict(id=3, data=dict(state="pending", notify="pilot")),
            dict(id=2, data=dict(state="completed", canon_ref=self.pinned,
                                 notify="pilot"))]}
        events = types.SimpleNamespace(
            latest_type=lambda *a: [dict(id=3, object="staging-request:mesh",
                                         data=rows["staging-request:mesh"][0]["data"])],
            recent_type=lambda kind, days, limit, object=None: rows.get(object, []))
        store = types.SimpleNamespace(events=events)
        with mock.patch.object(staging_watch.storage, "of", return_value=store):
            self.assertEqual(staging_watch.canon_alerts(
                object(), now=T0 + DAY - 1, cfg=self.cfg), [])
            alert, = staging_watch.canon_alerts(object(), now=T0 + DAY, cfg=self.cfg)
            self.assertEqual((alert["canon_ref"], alert["head"]), (self.pinned, self.head))
            rows["staging-request:mesh"][1]["data"]["canon_ref"] = self.head
            self.assertEqual(staging_watch.canon_alerts(
                object(), now=T0 + 9 * DAY, cfg=self.cfg), [])

    def test_no_canon_configured_is_no_alert(self):
        cfg = types.SimpleNamespace(canon="", canon_ref="")
        self.assertEqual(staging_watch.canon_alerts(object(), now=T0, cfg=cfg), [])

    def test_executor_records_its_canon_commit(self):
        self.assertEqual(staging_executor.canon_commit(self.cfg), self.head)
        pinned = types.SimpleNamespace(canon=self.repo, canon_ref=self.pinned[:8])
        self.assertEqual(staging_executor.canon_commit(pinned), self.pinned)
        missing = types.SimpleNamespace(canon=self.repo, canon_ref="f" * 40)
        self.assertEqual(staging_executor.canon_commit(missing), "")
        self.assertEqual(staging_executor.canon_commit(
            types.SimpleNamespace(canon="", canon_ref="")), "")


class TickRecordsCanonTest(unittest.TestCase):
    """Chaque enregistrement de la passe de l'exécuteur porte `canon_ref`."""

    def test_every_record_of_the_pass_carries_canon_ref(self):
        # The #4880 executor fixture, without inheriting (and rerunning) its tests.
        case = fixture.ExecutorTest("test_attested_foreign_run_allows_next_attempt")
        case.setUp()
        self.addCleanup(case.doCleanups)
        seen = []
        events = staging_executor.storage.of.return_value.events
        record = events.record

        def keep(kind, key, project, data):
            seen.append(dict(data))
            return record(kind, key, project, data)
        with mock.patch.object(staging_executor, "canon_commit",
                               return_value="c336c53c" + "0" * 32), \
                mock.patch.object(events, "record", side_effect=keep):
            result = staging_executor.tick(case.db, case.cfg, case.options)
        self.assertEqual(result["state"], "completed")
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual({data.get("canon_ref") for data in seen},
                         {"c336c53c" + "0" * 32})


class RegistrationTest(unittest.TestCase):
    def test_type_is_declared_briefed_and_labelled(self):
        self.assertIn("staging_canon_stale", exploitation.ALERT_TYPES)
        self.assertIn("staging_canon_stale", alert_briefs.BRIEFED_TYPES)
        self.assertIn("staging_canon_stale", notify.TYPE_LABELS)

    def test_identity_ignores_the_moving_head(self):
        a = dict(type="staging_canon_stale", reason="mesh", canon_ref="c" * 40,
                 head="h" * 40, since=T0)
        b = dict(a, head="i" * 40, since=T0 + 5)
        self.assertEqual(exploitation.alert_key(a), exploitation.alert_key(b))
        self.assertNotEqual(exploitation.alert_key(a),
                            exploitation.alert_key(dict(a, canon_ref="d" * 40)))


if __name__ == "__main__":
    unittest.main()
