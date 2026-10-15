# SPDX-License-Identifier: AGPL-3.0-only
"""#1580 (slice 3) — the omarchy widget gauges and model scope.

Owner report (2026-10-15 09:54): claude2's "Fable Weekly" bucket stayed at 0%
while its general weekly was at 93%, and the automatic choice — which only
reads `rate_limit_event.unifiedWindows` — did not see that bucket: the whole
Fable week was lost at reset.

Witnesses: the widget file reader (inert where it does not exist, stale
beyond its shortest window), the scope of a model bucket (it counts only for
a session of that model) and the score (`rank`) which first consumes the lost
budget — including a model bucket.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from ameesh import account_turn, accounts, cost, widget_usage
from ameesh.cost import Gauge, Gauges, Overage

from .test_l30_accounts import _Base, _dossier

JOUR = 86400.0
HEURE = 3600.0
DIR_ENV = widget_usage.DIR_ENV


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _profil(dossier: str, nom: str = "primaire") -> accounts.Profile:
    return accounts.Profile("claude", nom, path=dossier)


class WidgetUsageTest(unittest.TestCase):
    """The widget file reader: shape, inertia, freshness."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-widget-")
        self.dossier = os.path.join(self.tmp, "usage")
        os.makedirs(self.dossier, exist_ok=True)
        self.compte = os.path.join(self.tmp, "claude-secondary")
        os.makedirs(self.compte, exist_ok=True)
        self.now = time.time()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ecrit(self, nom: str, contenu, mtime: float | None = None) -> str:
        chemin = os.path.join(self.dossier, nom)
        with open(chemin, "w", encoding="utf-8") as fh:
            if isinstance(contenu, str):
                fh.write(contenu)
            else:
                json.dump(contenu, fh)
        if mtime is not None:
            os.utime(chemin, (mtime, mtime))
        return chemin

    def _instantane(self, **extra) -> dict:
        payload = {
            "schemaVersion": 1, "id": "x", "name": "Claude Code", "ready": True,
            "updatedAt": _iso(self.now - 60),
            "limits": [
                {"label": "Session (5-hour)", "percent": 0.27,
                 "resetsAt": _iso(self.now + 4 * HEURE)},
                {"label": "Weekly (7-day)", "percent": 0.13,
                 "resetsAt": _iso(self.now + 6 * JOUR)},
                {"label": "Fable Weekly", "title": "Fable Weekly", "percent": 0.0,
                 "resetsAt": _iso(self.now + 6 * JOUR)},
            ],
        }
        payload.update(extra)
        return payload

    def test_buckets_read_and_model_scope(self):
        self._ecrit("claude-secondary.json", self._instantane())
        gauges = widget_usage.read(_profil(self.compte, "secondaire"),
                                   now=self.now, directory=self.dossier)
        self.assertIsNotNone(gauges)
        self.assertEqual([g.key for g in gauges],
                         ["five_hour", "seven_day", "seven_day@fable"])
        self.assertEqual([g.window_s for g in gauges],
                         [5 * HEURE, 7 * JOUR, 7 * JOUR])
        self.assertAlmostEqual(gauges[0].used, 0.27)
        self.assertAlmostEqual(gauges[1].used, 0.13)
        self.assertEqual([g.scope for g in gauges], ["", "", "fable"])
        # resetsAt ISO → epoch, to the second
        self.assertAlmostEqual(gauges[0].resets_at, self.now + 4 * HEURE, places=2)
        # #1789/N2: the reading exposes its date, like the log gauges
        for gauge in gauges:
            self.assertAlmostEqual(gauge.read_at, self.now - 60, places=2)

    def test_mtime_fallback_also_carries_the_date(self):
        """#1789/N2: without `updatedAt`, the reading's date comes from the
        mtime and stays exposed (`read_at`)."""
        stamp = self.now - 120
        instantane = self._instantane()
        del instantane["updatedAt"]
        self._ecrit("claude-secondary.json", instantane, mtime=stamp)
        gauges = widget_usage.read(_profil(self.compte, "secondaire"),
                                   now=self.now, directory=self.dossier)
        self.assertIsNotNone(gauges)
        self.assertAlmostEqual(gauges[0].read_at, stamp, places=2)

    def test_bucket_title_with_parenthesis(self):
        """#1789/N4: "Fable Weekly (7-day)" gives the scope "fable", not
        "fable ( )" — the bucket is no longer set aside by model filtering."""
        instantane = self._instantane()
        instantane["limits"] = [
            {"label": "Weekly (7-day)", "percent": 0.93,
             "resetsAt": _iso(self.now + 6 * JOUR)},
            {"label": "Fable Weekly (7-day)", "title": "Fable Weekly (7-day)",
             "percent": 0.0, "resetsAt": _iso(self.now + 6 * JOUR)},
        ]
        self._ecrit("claude-secondary.json", instantane)
        gauges = widget_usage.read(_profil(self.compte, "secondaire"),
                                   now=self.now, directory=self.dossier)
        self.assertIsNotNone(gauges)
        self.assertEqual(widget_usage._scope_of("Fable Weekly (7-day)"), "fable")
        self.assertEqual([g.key for g in gauges], ["seven_day", "seven_day@fable"])
        self.assertEqual([g.scope for g in gauges], ["", "fable"])
        fable = accounts.model_scoped(gauges, "claude-fable-5")
        self.assertIn("seven_day@fable", [g.key for g in fable])

    def test_iso_with_and_without_microseconds(self):
        avant = widget_usage._iso_epoch("2026-10-21T12:00:00+00:00")
        apres = widget_usage._iso_epoch("2026-10-21T12:00:00.245717+00:00")
        self.assertAlmostEqual(apres - avant, 0.245717, places=5)
        self.assertAlmostEqual(
            widget_usage._iso_epoch("2026-10-21T12:00:00Z"), avant, places=6)
        # both forms are read from a fresh snapshot
        self._ecrit("claude-secondary.json", self._instantane(limits=[
            {"label": "Weekly (7-day)", "percent": 1.0,
             "resetsAt": "2026-10-21T12:00:00+00:00"},
            {"label": "Session (5-hour)", "percent": 0.5,
             "resetsAt": "2026-10-15T15:30:00.245717+00:00"},
        ]))
        gauges = widget_usage.read(_profil(self.compte, "secondaire"),
                                   now=self.now, directory=self.dossier)
        self.assertEqual([g.key for g in gauges], ["seven_day", "five_hour"])
        self.assertAlmostEqual(gauges[0].resets_at, avant, places=5)
        court = widget_usage._iso_epoch("2026-10-15T15:30:00.245717+00:00")
        self.assertAlmostEqual(gauges[1].resets_at, court, places=5)
        self.assertAlmostEqual(gauges[1].used, 0.5)

    def test_percent_clamped_and_scale(self):
        self._ecrit("claude-secondary.json", self._instantane(limits=[
            {"label": "Session (5-hour)", "percent": 42,
             "resetsAt": _iso(self.now + HEURE)},
            {"label": "Weekly (7-day)", "percent": 150,
             "resetsAt": _iso(self.now + 6 * JOUR)},
        ]))
        gauges = widget_usage.read(_profil(self.compte, "secondaire"),
                                   now=self.now, directory=self.dossier)
        # above 1: dry, never read as a percentage (1.05 ≠ 1%)
        self.assertAlmostEqual(gauges[0].used, 1.0)
        self.assertAlmostEqual(gauges[1].used, 1.0)

    def test_inertia_missing_or_unreadable_source(self):
        profil = _profil(self.compte, "secondaire")
        # missing directory
        self.assertIsNone(widget_usage.read(profil, now=self.now,
                                            directory=os.path.join(self.tmp, "rien")))
        # unreadable JSON
        self._ecrit("claude-secondary.json", "{pas du json")
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))
        # unknown schema
        self._ecrit("claude-secondary.json", self._instantane(schemaVersion=2))
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))
        # not ready
        self._ecrit("claude-secondary.json", self._instantane(ready=False))
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))
        # no readable bucket
        self._ecrit("claude-secondary.json", self._instantane(limits=[
            {"label": "Something else", "percent": 0.5}]))
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))
        # the default account reads `claude.json`
        self._ecrit("claude.json", self._instantane())
        defaut = os.path.join(self.tmp, "claude")
        os.makedirs(defaut, exist_ok=True)
        self.assertIsNotNone(widget_usage.read(_profil(defaut, "primaire"),
                                               now=self.now, directory=self.dossier))

    def test_freshness_shortest_window(self):
        profil = _profil(self.compte, "secondaire")
        # 6 h behind, with a 5 h window: stale (#1561)
        self._ecrit("claude-secondary.json", self._instantane(
            updatedAt=_iso(self.now - 6 * HEURE)))
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))
        # 4 h behind: still within the short window
        self._ecrit("claude-secondary.json", self._instantane(
            updatedAt=_iso(self.now - 4 * HEURE)))
        self.assertIsNotNone(widget_usage.read(profil, now=self.now,
                                               directory=self.dossier))
        # a file without a short window lives on its long window's scale
        self._ecrit("claude-secondary.json", self._instantane(
            updatedAt=_iso(self.now - 6 * HEURE),
            limits=[{"label": "Weekly (7-day)", "percent": 0.5,
                     "resetsAt": _iso(self.now + 6 * JOUR)}]))
        self.assertIsNotNone(widget_usage.read(profil, now=self.now,
                                               directory=self.dossier))

    def test_updated_at_absent_fallback_on_mtime(self):
        profil = _profil(self.compte, "secondaire")
        payload = self._instantane()
        payload.pop("updatedAt")
        self._ecrit("claude-secondary.json", payload, mtime=self.now - 60)
        self.assertIsNotNone(widget_usage.read(profil, now=self.now,
                                               directory=self.dossier))
        self._ecrit("claude-secondary.json", payload, mtime=self.now - 6 * HEURE)
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))

    def test_reading_carries_its_date(self):
        """#1648: a snapshot's buckets carry the reading date (`updatedAt`,
        mtime fallback), so the caller can compare two dated sources."""
        profil = _profil(self.compte, "secondaire")
        self._ecrit("claude-secondary.json", self._instantane(
            updatedAt=_iso(self.now - 4 * HEURE)))
        vus = widget_usage.read(profil, now=self.now, directory=self.dossier)
        self.assertIsNotNone(vus)
        self.assertEqual(len({round(g.read_at or 0, 3) for g in vus}), 1)
        self.assertAlmostEqual(vus[0].read_at, self.now - 4 * HEURE, delta=1)
        payload = self._instantane()
        payload.pop("updatedAt")
        self._ecrit("claude-secondary.json", payload, mtime=self.now - 30)
        vus = widget_usage.read(profil, now=self.now, directory=self.dossier)
        self.assertIsNotNone(vus)
        self.assertAlmostEqual(vus[0].read_at, self.now - 30, delta=1)

    def test_directory_from_environment_variable(self):
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            self.assertEqual(widget_usage.usage_dir(), self.dossier)
            self._ecrit("claude-secondary.json", self._instantane())
            self.assertIsNotNone(widget_usage.read(_profil(self.compte, "secondaire"),
                                                   now=self.now))
        self.assertEqual(widget_usage.usage_dir({"X": "1"}),
                         os.path.abspath(os.path.expanduser(widget_usage.DEFAULT_DIR)))


class PorteeParModeleTest(unittest.TestCase):
    """Model bucket filtering and the score that follows from it."""

    def setUp(self) -> None:
        self.now = time.time()

    def _jauges(self) -> Gauges:
        return Gauges([
            Gauge("claude", "five_hour", 0.1, self.now + 10 * HEURE, 5 * HEURE),
            Gauge("claude", "seven_day", 0.9, self.now + 20 * HEURE, 7 * JOUR),
            Gauge("claude", "seven_day@fable", 0.0, self.now + 2 * HEURE, 7 * JOUR,
                  scope="fable"),
        ], Overage(True, False))

    def test_filtering_by_model(self):
        jauges = self._jauges()
        fable = accounts.model_scoped(jauges, "claude-fable-5")
        self.assertEqual([g.key for g in fable],
                         ["five_hour", "seven_day", "seven_day@fable"])
        autre = accounts.model_scoped(jauges, "claude-opus-5-5")
        self.assertEqual([g.key for g in autre], ["five_hour", "seven_day"])
        inconnu = accounts.model_scoped(jauges, "")
        self.assertEqual([g.key for g in inconnu], ["five_hour", "seven_day"])
        # no session (display): all buckets, including model ones
        self.assertIs(accounts.model_scoped(jauges, None), jauges)
        # the copy keeps the reading's type and overage flags
        self.assertIsInstance(autre, Gauges)
        self.assertTrue(autre.overage.capable)
        # no model gauge: the list is returned as is
        sans = Gauges([Gauge("claude", "five_hour", 0.1, self.now + HEURE, 5 * HEURE)])
        self.assertIs(accounts.model_scoped(sans, "fable"), sans)

    def test_lost_budget_of_the_model_bucket(self):
        jauges = self._jauges()
        # for a Fable agent: the Fable bucket resets earliest, its
        # unused capacity is the lost budget
        self.assertAlmostEqual(
            accounts.lost_at_reset(accounts.model_scoped(jauges, "fable"), self.now),
            1.0)
        # for another model: the Fable bucket does not count, the 5 h
        # window is the closest (10 h) and its unused share is 0.9
        self.assertAlmostEqual(
            accounts.lost_at_reset(accounts.model_scoped(jauges, "opus"), self.now),
            0.9)

    def test_rank_prefers_the_model_bucket_for_its_model(self):
        # declared priorities: the secondaire goes before the primaire at
        # equal budget (L226b "primaries last")
        primaire = accounts.Profile("claude", "primaire", priority=100)
        secondaire = accounts.Profile("claude", "secondaire", priority=20)
        # A: same lag as B; its Fable bucket at 0% resets in
        # 2 h, its 5 h window in 5 h
        ga = Gauges([
            Gauge("claude", "five_hour", 0.05, self.now + 5 * HEURE, 5 * HEURE),
            Gauge("claude", "seven_day", 0.05, self.now + 7 * JOUR, 7 * JOUR),
            Gauge("claude", "seven_day@fable", 0.0, self.now + 2 * HEURE, 7 * JOUR,
                  scope="fable"),
        ])
        # B: same lag, same deadline, no model bucket
        gb = Gauges([
            Gauge("claude", "five_hour", 0.05, self.now + 5 * HEURE, 5 * HEURE),
            Gauge("claude", "seven_day", 0.05, self.now + 7 * JOUR, 7 * JOUR),
        ])
        ev = lambda p, g: accounts.Evaluation(p, gauges=g)  # noqa: E731
        # Fable agent: A first (Fable bucket lost in 2 h, no loss at B)
        fable = {"primaire": ev(primaire, accounts.model_scoped(ga, "fable")),
                 "secondaire": ev(secondaire, gb)}
        self.assertEqual([p.name for p in accounts.rank([primaire, secondaire], fable,
                                                         self.now)],
                         ["primaire", "secondaire"])
        # other model: the Fable bucket is set aside, A and B at equal budget,
        # the declared order breaks the tie (secondaire 20 before primaire 100)
        autre = {"primaire": ev(primaire, accounts.model_scoped(ga, "opus")),
                 "secondaire": ev(secondaire, gb)}
        self.assertEqual([p.name for p in accounts.rank([primaire, secondaire], autre,
                                                         self.now)],
                         ["secondaire", "primaire"])


class GaugesOfTest(unittest.TestCase):
    """`gauges_of`: fresh widget first, logs otherwise, overage kept."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-widget-")
        self.dossier = os.path.join(self.tmp, "usage")
        os.makedirs(self.dossier, exist_ok=True)
        self.compte = os.path.join(self.tmp, "claude")
        os.makedirs(self.compte, exist_ok=True)
        self.now = time.time()
        self.journal = Gauges(
            [Gauge("claude", "five_hour", 0.8, self.now + HEURE, 5 * HEURE)],
            Overage(True, False))
        self.book = SimpleNamespace(claude_gauges=lambda **kw: self.journal)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ecrit(self, payload) -> None:
        with open(os.path.join(self.dossier, "claude.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_fresh_widget_priority_and_overage_from_the_log(self):
        self._ecrit({"schemaVersion": 1, "ready": True,
                     "updatedAt": _iso(self.now - 60),
                     "limits": [
                         {"label": "Session (5-hour)", "percent": 0.2,
                          "resetsAt": _iso(self.now + 4 * HEURE)},
                         {"label": "Weekly (7-day)", "percent": 0.3,
                          "resetsAt": _iso(self.now + 6 * JOUR)},
                         {"label": "Fable Weekly", "title": "Fable Weekly",
                          "percent": 0.0, "resetsAt": _iso(self.now + 6 * JOUR)}]})
        profil = _profil(self.compte, "primaire")
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            vus = accounts.gauges_of(self.book, profil, [profil],
                                     model="claude-fable-5", now=self.now)
        self.assertEqual([g.key for g in vus],
                         ["five_hour", "seven_day", "seven_day@fable"])
        self.assertTrue(vus.overage.capable)     # taken from the log

    def test_fallback_on_the_log_if_source_missing_or_stale(self):
        profil = _profil(self.compte, "primaire")
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            vus = accounts.gauges_of(self.book, profil, [profil], now=self.now)
            self.assertEqual([g.key for g in vus], ["five_hour"])
        self._ecrit({"schemaVersion": 1, "ready": True,
                     "updatedAt": _iso(self.now - 6 * HEURE),
                     "limits": [{"label": "Session (5-hour)", "percent": 0.2,
                                 "resetsAt": _iso(self.now + 4 * HEURE)}]})
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            vus = accounts.gauges_of(self.book, profil, [profil], now=self.now)
            self.assertEqual([g.key for g in vus], ["five_hour"])
            self.assertAlmostEqual(vus[0].used, 0.8)    # the log, not the widget

    def test_api_key_does_not_read_the_primaire_widget(self):
        """#1789/N3: an API key has no configuration directory — without a
        guard, `home()` falls back on the default directory and its line
        would display the primaire's gauges."""
        cle = accounts.Profile("claude", "cle", kind="api_key_env",
                               env="ANTHROPIC_API_KEY")
        self._ecrit({"schemaVersion": 1, "ready": True,
                     "updatedAt": _iso(self.now - 60),
                     "limits": [{"label": "Session (5-hour)", "percent": 0.2,
                                 "resetsAt": _iso(self.now + 4 * HEURE)}]})
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}), \
                mock.patch.object(widget_usage, "read") as lecture:
            vus = accounts.gauges_of(self.book, cle, [cle], now=self.now)
        lecture.assert_not_called()
        self.assertEqual([g.key for g in vus], ["five_hour"])
        self.assertAlmostEqual(vus[0].used, 0.8)        # the account's log

    def test_two_directories_with_the_same_name_do_not_share_the_file(self):
        """#1789/N3: `a/claude` and `b/claude` fell on the same `claude.json`;
        the key moves to the full path — the widget does not write that file,
        the log takes over instead of displaying another directory's gauges."""
        a = _profil(os.path.join(self.tmp, "a", "claude"), "a")
        b = _profil(os.path.join(self.tmp, "b", "claude"), "b")
        for profil in (a, b):
            os.makedirs(profil.path, exist_ok=True)
        self._ecrit({"schemaVersion": 1, "ready": True,
                     "updatedAt": _iso(self.now - 60),
                     "limits": [{"label": "Session (5-hour)", "percent": 0.2,
                                 "resetsAt": _iso(self.now + 4 * HEURE)}]})
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            for profil in (a, b):
                vus = accounts.gauges_of(self.book, profil, [a, b], now=self.now)
                self.assertEqual([g.key for g in vus], ["five_hour"])
                self.assertAlmostEqual(vus[0].used, 0.8)   # the log, not the widget

    def _fable_snapshot(self, age: float) -> None:
        self._ecrit({"schemaVersion": 1, "ready": True,
                     "updatedAt": _iso(self.now - age),
                     "limits": [
                         {"label": "Session (5-hour)", "percent": 0.2,
                          "resetsAt": _iso(self.now + 4 * HEURE)},
                         {"label": "Weekly (7-day)", "percent": 0.3,
                          "resetsAt": _iso(self.now + 6 * JOUR)},
                         {"label": "Fable Weekly", "title": "Fable Weekly",
                          "percent": 0.0, "resetsAt": _iso(self.now + 6 * JOUR)}]})

    def _dated_journal(self, age: float) -> Gauges:
        return Gauges([Gauge("claude", "five_hour", 0.8, self.now + HEURE,
                             5 * HEURE, read_at=self.now - age)],
                      Overage(True, False))

    def test_newer_journal_beats_the_widget(self):
        """#1648: a 4 h old widget (still inside its short window) no longer
        masks a journal dated one minute ago. The journal wins — the model
        buckets come back with the widget's next reading."""
        self._fable_snapshot(4 * HEURE)
        self.book = SimpleNamespace(claude_gauges=lambda **kw: self._dated_journal(60))
        profil = _profil(self.compte, "primaire")
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            vus = accounts.gauges_of(self.book, profil, [profil],
                                     model="claude-fable-5", now=self.now)
        self.assertEqual([g.key for g in vus], ["five_hour"])
        self.assertAlmostEqual(vus[0].used, 0.8)
        self.assertTrue(vus.overage.capable)        # the journal's overage
        self.assertEqual(accounts._read_at_of(vus), self.now - 60)

    def test_newer_widget_keeps_its_model_buckets(self):
        """Reverse: journal dated 10 min ago, widget 30 s ago — the widget
        wins, model bucket included (#1580 behaviour kept)."""
        self._fable_snapshot(30)
        self.book = SimpleNamespace(claude_gauges=lambda **kw: self._dated_journal(600))
        profil = _profil(self.compte, "primaire")
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            vus = accounts.gauges_of(self.book, profil, [profil],
                                     model="claude-fable-5", now=self.now)
        self.assertEqual([g.key for g in vus],
                         ["five_hour", "seven_day", "seven_day@fable"])
        self.assertTrue(vus.overage.capable)        # taken from the journal


class ChooseAvecWidgetTest(_Base):
    """End to end: the choice carries the agent's model (slice 3).

    The primaire's Fable bucket is dry and resets soon: a Fable agent must
    avoid it (the bucket holds it back), an agent of another model must be
    able to ignore it. The two accounts are otherwise identical, the primaire
    coming first by its declared order.
    """

    def _comptes(self):
        a = _dossier(os.path.join(self.tmp, "comptes", "claude-fable"),
                     creds=".credentials.json")
        b = _dossier(os.path.join(self.tmp, "comptes", "claude-autre"),
                     creds=".credentials.json")
        return [{"name": "primaire", "path": a, "priority": 10},
                {"name": "secondaire", "path": b}]

    def _widget(self, nom: str, fable: float | None) -> None:
        self.dossier = os.path.join(self.tmp, "usage")
        os.makedirs(self.dossier, exist_ok=True)
        limites = [
            {"label": "Session (5-hour)", "percent": 0.05,
             "resetsAt": _iso(self.now + 5 * HEURE)},
            {"label": "Weekly (7-day)", "percent": 0.05,
             "resetsAt": _iso(self.now + 7 * JOUR)},
        ]
        if fable is not None:
            limites.append({"label": "Fable Weekly", "title": "Fable Weekly",
                            "percent": fable,
                            "resetsAt": _iso(self.now + 2 * HEURE)})
        with open(os.path.join(self.dossier, nom + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"schemaVersion": 1, "ready": True,
                       "updatedAt": _iso(self.now - 60), "limits": limites}, fh)

    def _choix(self, modele: str, registre: bool = False) -> str:
        self.now = time.time()
        comptes = self._comptes()
        self._widget("claude-fable", 1.0)
        self._widget("claude-autre", None)
        runner, worker = self._worker("wf", "claude", {"claude": comptes})
        if registre:
            # `ameesh set <agent> model=…`: the model is in the registry,
            # without a state file — it is the one the runner passes to the turn
            worker.agent = dict(worker.agent or {}, model=modele)
        else:
            os.makedirs(os.path.join(self.cfg.state_dir, worker.name), exist_ok=True)
            with open(os.path.join(self.cfg.state_dir, worker.name, "model"), "w",
                      encoding="utf-8") as fh:
                fh.write(modele + "\n")
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db,
                             tools={worker.name: "claude"})
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            raison = account_turn.choose(worker, book)
        self.assertEqual(raison, "")
        return worker._account.name

    def test_fable_agent_avoids_the_dry_bucket(self):
        self.assertEqual(self._choix("claude-fable-5"), "secondaire")

    def test_other_model_ignores_the_model_bucket(self):
        self.assertEqual(self._choix("claude-opus-5-5"), "primaire")

    def test_model_from_the_registry(self):
        self.assertEqual(self._choix("claude-fable-5", registre=True), "secondaire")
        self.assertEqual(self._choix("claude-opus-5-5", registre=True), "primaire")


if __name__ == "__main__":
    unittest.main()
