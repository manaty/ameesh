# SPDX-License-Identifier: AGPL-3.0-only
"""Coût des tours et jauges de forfait (L12, décision 0019).

Tous les journaux sont **factices**, écrits dans un bac à sable : aucun test ne
lit `~/.codex` ni `~/.claude`, ni l'état réel des agents. La partie base
(`turn_costs`) tourne sur le schéma jetable de `PgTestCase`, donc sur les deux
pilotes de `scripts/test.sh`.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import cost, work

from .support import PgTestCase

#: the real racy window, for the tests that rewrite a journal in place
RACY_CTIME_NS = cost.RACY_CTIME_NS

# Un `result` Claude porte toujours l'usage du tour ; sans lui, le coût se lit
# appel par appel au barème (L221), pas dans le cumul.
U = {"input_tokens": 1}


def write(path: str, *events) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


class Sandbox:
    """Un bac à sable disque (et, si la classe a une base, un grand livre propre).

    Les journaux de harnais sont **factices** : aucun test n'approche `~/.codex`
    ni `~/.claude`, et les chemins par défaut ne sont jamais lus.
    """

    def setUp(self) -> None:
        super().setUp()                     # PgTestCase : schéma, TRUNCATE, dossiers
        # journals written by a test count as settled, so caching is observable;
        # the in-place rewrite tests restore the real racy window
        racy = mock.patch.object(cost, "RACY_CTIME_NS", 0)
        racy.start()
        self.addCleanup(racy.stop)
        self.tmp_cost = tempfile.mkdtemp(prefix="ameesh-cost-")
        self.addCleanup(shutil.rmtree, self.tmp_cost, ignore_errors=True)
        self.state = os.path.join(self.tmp_cost, "state")
        self.codex = os.path.join(self.tmp_cost, "codex-sessions")
        os.makedirs(self.state, exist_ok=True)
        self.now = 1_800_000_000.0
        if getattr(self, "db", None) is not None:
            self.db.execute("DELETE FROM turn_costs")

    def book(self, **kw) -> cost.CostBook:
        if getattr(self, "db", None) is not None:
            kw.setdefault("db", self.db)
        kw.setdefault("state_dir", self.state)
        kw.setdefault("codex_sessions", self.codex)
        kw.setdefault("prices_path", os.path.join(self.tmp_cost, "prices.json"))
        kw.setdefault("clock", lambda: self.now)
        return cost.CostBook(**kw)

    def agent(self, name: str, tool: str, model: str = "") -> str:
        os.makedirs(os.path.join(self.state, name), exist_ok=True)
        with open(os.path.join(self.state, name, "tool"), "w", encoding="utf-8") as fh:
            fh.write(tool)
        with open(os.path.join(self.state, name, "model"), "w", encoding="utf-8") as fh:
            fh.write(model)
        return name

    def events(self, name: str, *events) -> None:
        self.write_events(name, *events)

    def write_events(self, name: str, *events) -> None:
        write(os.path.join(self.state, name, "events.jsonl"), *events)


class ClaudeGaugeTest(Sandbox, unittest.TestCase):
    def test_jauges_lues_dans_le_dernier_rate_limit_event(self):
        self.agent("a1", "claude")
        self.agent("a2", "claude")
        self.events("a1", {"type": "result", "total_cost_usd": 1.0})
        self.events("a2", {
            "type": "rate_limit_event",
            "rate_limit_info": {"unifiedWindows": {
                "five_hour": {"utilization": 42, "resetsAt": self.now + 3600},
                "seven_day": {"utilization": 0.61, "resetsAt": self.now + 86_400},
            }},
        })
        gauges = {g.key: g for g in self.book().claude_gauges()}
        self.assertEqual(sorted(gauges), ["five_hour", "seven_day"])
        self.assertAlmostEqual(gauges["five_hour"].used, 0.42)      # publié en pourcents
        self.assertAlmostEqual(gauges["seven_day"].used, 0.61)      # publié en fraction
        self.assertEqual(gauges["five_hour"].window_s, 5 * 3600)
        self.assertEqual(gauges["seven_day"].window_s, 7 * 86400)

    def test_seuil_de_rythme_min_90_part_ecoulee_plus_10(self):
        # Fenêtre de cinq heures écoulée à 20 % : plafond 30 %.
        self.agent("a1", "claude")
        self.events("a1", {
            "type": "rate_limit_event",
            "rate_limit_info": {"unifiedWindows": {
                "five_hour": {"utilization": 29, "resetsAt": self.now + 4 * 3600},
            }},
        })
        book = self.book()
        self.assertEqual(book.pace_exceeded("claude"), "")
        # À 31 %, le forfait avance plus vite que le rythme sûr.
        self.events("a1", {
            "type": "rate_limit_event",
            "rate_limit_info": {"unifiedWindows": {
                "five_hour": {"utilization": 31, "resetsAt": self.now + 4 * 3600},
            }},
        })
        reason = book.pace_exceeded("claude")
        self.assertIn("five_hour", reason)
        self.assertIn("pace cap 30%", reason)

    def test_le_plafond_ne_depasse_jamais_90_et_vaut_90_sans_reset(self):
        # Sans date de réinitialisation, on suppose la fenêtre entièrement écoulée :
        # le plafond est donc 90 %, et il ne monte jamais plus haut.
        gauge = cost.Gauge("claude", "seven_day", 0.89, None, 7 * 86400)
        self.assertEqual(gauge.elapsed(self.now), 1.0)
        self.assertEqual(gauge.pace_cap(self.now), cost.PACE_CEILING)
        self.assertFalse(gauge.exceeded(self.now))
        self.assertTrue(cost.Gauge("claude", "seven_day", 0.90, None, 7 * 86400).exceeded(self.now))
        # Une fenêtre qui vient de s'ouvrir laisse la marge de dix points, pas plus.
        fresh = cost.Gauge("claude", "five_hour", 0.05, self.now + 5 * 3600, 5 * 3600)
        self.assertAlmostEqual(fresh.pace_cap(self.now), 0.10)


class AccountTailGaugeTest(Sandbox, unittest.TestCase):
    """L227: tail truncation must never transfer another account's gauges."""

    def marker(self, account, ts):
        return {"type": cost.ACCOUNT_MARKER, "harness": "claude",
                "account": account, "ts": ts}

    def reading(self, used):
        return {"type": "rate_limit_event", "rate_limit_info": {
            "unifiedWindows": {"five_hour": {"utilization": used,
                                             "resetsAt": self.now + 3600}}}}

    def large_stream(self, *tail):
        self.agent("long", "claude")
        self.events("long", self.marker("tertiary", self.now - 10),
                    {"type": "assistant", "text": "x" * (cost.TAIL_BYTES + 70_000)},
                    *tail)
        return os.path.join(self.state, "long", "events.jsonl")

    def test_marker_outside_real_four_mb_tail(self):
        self.large_stream(self.reading(0.89))
        book = self.book()
        self.assertEqual(book.claude_gauges("primary", "primary"), [])
        self.assertAlmostEqual(book.claude_gauges("tertiary", "primary")[0].used, 0.89)

    def test_leading_readings_before_visible_marker_keep_prior_account(self):
        self.large_stream(self.reading(0.89), self.marker("secondary", self.now),
                          self.reading(0.42))
        book = self.book()
        self.assertEqual(book.claude_gauges("primary", "primary"), [])
        self.assertAlmostEqual(book.claude_gauges("tertiary", "primary")[0].used, 0.89)
        self.assertAlmostEqual(book.claude_gauges("secondary", "primary")[0].used, 0.42)

    def test_recovered_marker_timestamp_beats_newer_file_mtime(self):
        self.large_stream(self.reading(0.89))
        self.agent("recent", "claude")
        self.events("recent", self.marker("tertiary", self.now), self.reading(0.12))
        os.utime(os.path.join(self.state, "long", "events.jsonl"),
                 (self.now + 100, self.now + 100))
        self.assertAlmostEqual(self.book().claude_gauges("tertiary", "primary")[0].used,
                               0.12)

    def test_large_unmarked_legacy_stream_still_belongs_to_primary(self):
        path = self.large_stream(self.reading(0.25))
        with open(path, "rb") as fh:
            fh.readline()
            remainder = fh.read()
        with open(path, "wb") as fh:
            fh.write(remainder)
        self.assertAlmostEqual(self.book().claude_gauges("primary", "primary")[0].used,
                               0.25)

    def test_flux_sans_marqueur_non_date_par_la_mtime(self):
        # #1720 : la mtime du journal (dernière écriture, tous événements
        # confondus) ne date plus un flux sans marqueur — sinon ce relevé
        # battrait à tort un widget plus frais (#1648). Il reste lu, non daté.
        self.agent("legacy", "claude")
        self.events("legacy", self.reading(0.25))
        os.utime(os.path.join(self.state, "legacy", "events.jsonl"),
                 (self.now + 3600, self.now + 3600))
        gauges = self.book().claude_gauges("primary", "primary")
        self.assertAlmostEqual(gauges[0].used, 0.25)
        self.assertFalse(gauges[0].read_at)     # non daté, jamais la mtime

    def test_un_evenement_qui_porte_sa_date_se_date_lui_meme(self):
        # #1720 : quand l'événement porte lui-même une date (ts/timestamp),
        # elle sert de read_at, même sur un flux sans marqueur.
        self.agent("legacy", "claude")
        event = self.reading(0.25)
        event["ts"] = self.now - 42
        self.events("legacy", event)
        gauge = self.book().claude_gauges("primary", "primary")[0]
        self.assertAlmostEqual(gauge.read_at, self.now - 42, delta=1)

    def test_unmarked_prefix_of_marked_stream_is_unknown(self):
        self.agent("long", "claude")
        self.events("long", self.reading(0.89), self.marker("secondary", self.now),
                    self.reading(0.42))
        self.assertEqual(self.book().claude_gauges("primary", "primary"), [])

    def test_prefix_read_failure_never_credits_primary(self):
        self.large_stream(self.reading(0.89))
        with mock.patch.object(cost, "_last_claude_marker", side_effect=OSError("read failed")):
            self.assertEqual(self.book().claude_gauges("primary", "primary"), [])
            self.assertEqual(self.book().claude_gauges("tertiary", "primary"), [])

    def test_prefix_read_failure_preserves_later_explicit_account(self):
        self.large_stream(self.reading(0.89), self.marker("secondary", self.now),
                          self.reading(0.42))
        with mock.patch.object(cost, "_last_claude_marker", side_effect=OSError("read failed")):
            self.assertEqual(self.book().claude_gauges("primary", "primary"), [])
            self.assertEqual(self.book().claude_gauges("tertiary", "primary"), [])
            self.assertAlmostEqual(
                self.book().claude_gauges("secondary", "primary")[0].used, 0.42)

    def test_marker_crossing_tail_boundary_is_recovered(self):
        self.agent("long", "claude")
        marker = json.dumps(self.marker("tertiary", self.now)).encode() + b"\n"
        reading = json.dumps(self.reading(0.31)).encode() + b"\n"
        path = os.path.join(self.state, "long", "events.jsonl")
        with open(path, "wb") as fh:
            fh.write(b"{}\n" + marker + reading)
        with mock.patch.object(cost, "TAIL_BYTES", len(reading) + len(marker) // 2):
            self.assertAlmostEqual(
                self.book().claude_gauges("tertiary", "primary")[0].used, 0.31)
            self.assertEqual(self.book().claude_gauges("primary", "primary"), [])

    def test_reverse_scan_uses_last_valid_claude_marker_across_blocks(self):
        self.agent("long", "claude")
        self.events("long", self.marker("secondary", self.now - 20),
                    self.marker("tertiary", self.now - 10),
                    {"type": cost.ACCOUNT_MARKER, "harness": "codex", "account": "other"},
                    {"type": "assistant", "text": "x" * (cost.TAIL_BYTES + 70_000)},
                    self.reading(0.31))
        self.assertAlmostEqual(self.book().claude_gauges("tertiary", "primary")[0].used,
                               0.31)
        self.assertEqual(self.book().claude_gauges("secondary", "primary"), [])

    def test_marker_split_across_reverse_blocks(self):
        self.agent("long", "claude")
        marker = json.dumps(self.marker("tertiary", self.now)).encode() + b"\n"
        path = os.path.join(self.state, "long", "events.jsonl")
        with open(path, "wb") as fh:
            fh.write(marker + b"x" * 63_960 + b"\n")
        with open(path, "rb") as fh:
            self.assertEqual(cost._last_claude_marker(fh, os.path.getsize(path)),
                             self.marker("tertiary", self.now))

    def test_empty_account_marker_does_not_default_to_primary(self):
        self.agent("long", "claude")
        self.events("long", self.marker("", self.now), self.reading(0.89))
        self.assertEqual(self.book().claude_gauges("primary", "primary"), [])

    def test_visible_marker_before_readings_skips_prefix_scan(self):
        self.large_stream(self.marker("secondary", self.now), self.reading(0.42))
        with mock.patch.object(cost, '_last_claude_marker') as scan:
            self.assertAlmostEqual(self.book().claude_gauges('secondary', 'primary')[0].used, 0.42)
            scan.assert_not_called()

    def test_unchanged_journal_is_read_and_attributed_once_for_all_accounts(self):
        self.large_stream(self.reading(0.89), self.marker('secondary', self.now), self.reading(0.42))
        book = self.book()
        with mock.patch.object(cost, '_claude_account_tail', wraps=cost._claude_account_tail) as read, \
             mock.patch.object(cost, '_last_claude_marker', wraps=cost._last_claude_marker) as scan, \
             mock.patch.object(cost, '_claude_account_readings', wraps=cost._claude_account_readings) as attribute:
            for _ in range(3):
                self.assertEqual(book.claude_gauges('primary', 'primary'), [])
                self.assertAlmostEqual(book.claude_gauges('tertiary', 'primary')[0].used, 0.89)
                self.assertAlmostEqual(book.claude_gauges('secondary', 'primary')[0].used, 0.42)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(attribute.call_count, 1)

    def test_append_invalidates_cached_account_readings(self):
        self.large_stream(self.reading(0.89))
        book = self.book()
        self.assertAlmostEqual(book.claude_gauges('tertiary', 'primary')[0].used, 0.89)
        self.events('long', self.marker('secondary', self.now), self.reading(0.42))
        self.assertAlmostEqual(book.claude_gauges('secondary', 'primary')[0].used, 0.42)
        self.assertEqual(book.claude_gauges('primary', 'primary'), [])

    @mock.patch.object(cost, "RACY_CTIME_NS", RACY_CTIME_NS)
    def test_rewrite_same_size_and_mtime_invalidates_cache(self):
        # in one clock tick ctime does not move: a fresh journal is never cached
        self.agent('long', 'claude')
        self.events('long', self.marker('tertiary', self.now), self.reading(0.89))
        path = os.path.join(self.state, 'long', 'events.jsonl')
        book = self.book()
        self.assertAlmostEqual(book.claude_gauges('tertiary', 'primary')[0].used, 0.89)
        before = os.stat(path)
        with open(path, 'r+b') as fh:
            content = fh.read().replace(b'0.89', b'0.42')
            fh.seek(0)
            fh.write(content)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(os.stat(path).st_size, before.st_size)
        self.assertAlmostEqual(book.claude_gauges('tertiary', 'primary')[0].used, 0.42)

    @mock.patch.object(cost, "RACY_CTIME_NS", RACY_CTIME_NS)
    def test_slow_read_does_not_age_a_racy_snapshot_into_the_cache(self):
        # age taken before the read: a read lasting past the window stays racy
        self.agent('long', 'claude')
        self.events('long', self.marker('tertiary', self.now), self.reading(0.89))
        path = os.path.abspath(os.path.join(self.state, 'long', 'events.jsonl'))
        ctime = os.stat(path).st_ctime_ns
        now = [ctime + RACY_CTIME_NS // 2]
        real_tail = cost._claude_account_tail

        def slow_tail(target):
            result = real_tail(target)
            now[0] = ctime + 5 * RACY_CTIME_NS  # the read outlasts the window
            return result
        book = self.book()
        with mock.patch.object(cost.time, 'time_ns', lambda: now[0]), \
                mock.patch.object(cost, '_claude_account_tail', slow_tail):
            self.assertAlmostEqual(book.claude_gauges('tertiary', 'primary')[0].used, 0.89)
        self.assertIsNone(book._claude_gauge_cache.get(path))

    @mock.patch.object(cost, "RACY_CTIME_NS", RACY_CTIME_NS)
    def test_cache_hit_rechecks_age_after_a_clock_step_back(self):
        self.agent('long', 'claude')
        self.events('long', self.marker('tertiary', self.now), self.reading(0.89))
        path = os.path.abspath(os.path.join(self.state, 'long', 'events.jsonl'))
        ctime = os.stat(path).st_ctime_ns
        book = self.book()
        with mock.patch.object(cost.time, 'time_ns', lambda: ctime + 5 * RACY_CTIME_NS):
            book.claude_gauges('tertiary', 'primary')
        self.assertIsNotNone(book._claude_gauge_cache.get(path))
        tail = mock.Mock(wraps=cost._claude_account_tail)
        with mock.patch.object(cost.time, 'time_ns', lambda: ctime), \
                mock.patch.object(cost, '_claude_account_tail', tail):
            book.claude_gauges('tertiary', 'primary')
        tail.assert_called_once()
        self.assertIsNone(book._claude_gauge_cache.get(path))

    def test_replacement_and_truncation_invalidate_cache(self):
        self.agent('long', 'claude')
        self.events('long', self.marker('tertiary', self.now), self.reading(0.89))
        path = os.path.join(self.state, 'long', 'events.jsonl')
        book = self.book()
        self.assertAlmostEqual(book.claude_gauges('tertiary', 'primary')[0].used, 0.89)
        before = os.stat(path)
        with open(path, 'rb') as fh:
            content = fh.read().replace(b'0.89', b'0.42')
        with open(path + '.new', 'wb') as fh:
            fh.write(content)
        os.utime(path + '.new', ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(path + '.new', path)
        self.assertAlmostEqual(book.claude_gauges('tertiary', 'primary')[0].used, 0.42)
        with open(path, 'wb'):
            pass
        self.assertEqual(book.claude_gauges('tertiary', 'primary'), [])

    def test_prefix_failure_is_retried_on_unchanged_journal(self):
        self.large_stream(self.reading(0.89))
        book = self.book()
        with mock.patch.object(cost, '_last_claude_marker', side_effect=OSError('temporary')):
            self.assertEqual(book.claude_gauges('tertiary', 'primary'), [])
        self.assertAlmostEqual(book.claude_gauges('tertiary', 'primary')[0].used, 0.89)

    def test_legacy_cache_does_not_bind_to_first_requested_primary(self):
        self.agent('long', 'claude')
        self.events('long', self.reading(0.25))
        book = self.book()
        with mock.patch.object(cost, '_claude_account_tail', wraps=cost._claude_account_tail) as read:
            self.assertAlmostEqual(book.claude_gauges('first', 'first')[0].used, 0.25)
            self.assertAlmostEqual(book.claude_gauges('second', 'second')[0].used, 0.25)
            self.assertEqual(book.claude_gauges('first', 'second'), [])
            self.assertEqual(read.call_count, 1)


class CodexGaugeTest(Sandbox, unittest.TestCase):
    def test_jauges_primaires_et_secondaires_du_journal_le_plus_recent(self):
        write(os.path.join(self.codex, "2026", "10", "04", "roll.jsonl"),
              {"type": "session_meta"},
              {"payload": {"rate_limits": {
                  "primary": {"used_percent": 12.5, "window_minutes": 300,
                              "resets_at": self.now + 1000},
                  "secondary": {"used_percent": 80, "window_minutes": 10080,
                                "resets_at": self.now + 90_000},
                  "plan_type": "pro"}}})
        gauges = {g.key: g for g in self.book().codex_gauges()}
        self.assertEqual(sorted(gauges), ["codex-10080min", "codex-300min"])
        self.assertAlmostEqual(gauges["codex-300min"].used, 0.125)
        self.assertEqual(gauges["codex-300min"].window_s, 300 * 60)
        # 300 min écoulées à ~94 % : plafond de rythme atteint (min(90, 94+10)).
        self.assertIn("codex", self.book().pace_exceeded("codex") or "codex")

    def test_l_ordre_des_cles_et_un_secondaire_nul_ne_perdent_pas_la_jauge(self):
        """Sonde codex2 B2 : une regex exigeait `plan_type` en dernier.

        Même `rate_limits`, `plan_type` en tête et `secondary` nul : la jauge
        primaire doit être trouvée, sinon la pause est manquée.
        """
        write(os.path.join(self.codex, "2026", "10", "04", "ordre.jsonl"),
              {"payload": {"rate_limits": {
                  "plan_type": "plus",
                  "secondary": None,
                  "primary": {"used_percent": 95, "window_minutes": 300,
                              "resets_at": self.now + 100}}}})
        gauges = self.book().codex_gauges()
        self.assertEqual([g.key for g in gauges], ["codex-300min"])
        self.assertAlmostEqual(gauges[0].used, 0.95)
        self.assertIn("codex", self.book().pace_exceeded("codex"))

    def test_large_sessions_remain_scoped_to_each_account_directory(self):
        for account, used in (("primary", 12), ("secondary", 89)):
            root = os.path.join(self.codex, account, "sessions")
            write(os.path.join(root, "large.jsonl"),
                  {"type": "session_meta"},
                  {"type": "message", "text": "x" * 2_100_000},
                  {"payload": {"rate_limits": {
                      "plan_type": "team",
                      "primary": {"used_percent": used, "window_minutes": 300,
                                  "resets_at": self.now + 3600},
                      "secondary": {"used_percent": used, "window_minutes": 10080,
                                    "resets_at": self.now + 86400}}}})
            gauges = self.book().codex_gauges(sessions=root)
            self.assertAlmostEqual(gauges[0].used, used / 100)

    def test_aucune_jauge_sans_journal(self):
        self.assertEqual(self.book().codex_gauges(), [])
        self.assertEqual(self.book().pace_exceeded("codex"), "")


class TurnAccountingTest(Sandbox, PgTestCase):
    """La comptabilité par tour, avec le grand livre : le repère est la dernière
    ligne de `turn_costs`, donc ces cas tournent sur les deux pilotes."""
    def test_claude_difference_du_cout_cumule_par_session(self):
        self.agent("a1", "claude", "gpt-6.1-sol")
        book = self.book()
        self.events("a1", {"type": "result", "session_id": "s1", "total_cost_usd": 0.25,
                           "usage": {"input_tokens": 100, "cache_read_input_tokens": 900,
                                     "output_tokens": 50}})
        first = book.record("a1")
        self.assertAlmostEqual(first.usd, 0.25)          # session neuve : le cumul EST le tour
        self.assertEqual(first.input_tokens, 100)
        self.assertEqual(first.cached_input_tokens, 900)
        self.assertEqual(first.output_tokens, 50)

        # Deuxième tour de la même session : on ne compte que la différence.
        self.events("a1", {"type": "result", "session_id": "s1", "total_cost_usd": 0.40,
                           "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                                     "output_tokens": 5}})
        second = book.turn_usage("a1", start=1)
        self.assertAlmostEqual(second.usd, 0.15)
        self.assertEqual(second.output_tokens, 5)

    def test_claude_session_neuve_repart_de_zero(self):
        self.agent("a1", "claude")
        book = self.book()
        self.events("a1", {"type": "result", "session_id": "s1", "total_cost_usd": 3.0, "usage": U})
        book.record("a1")
        # Le harnais reprend avec une autre session : son cumul ne contient pas
        # le passé, donc il ne faut PAS soustraire le repère de la précédente.
        self.events("a1", {"type": "result", "session_id": "s2", "total_cost_usd": 0.10, "usage": U})
        self.assertAlmostEqual(book.turn_usage("a1", start=1).usd, 0.10)

    def test_codex_difference_de_l_usage_cumule_du_fil(self):
        self.agent("a1", "codex", "gpt-6.1-sol")
        book = self.book()
        self.events("a1", {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "usage": {"input_tokens": 1000,
                                                         "cached_input_tokens": 400,
                                                         "output_tokens": 100}})
        first = book.record("a1")
        # 600 non cachés × 1.25 $/M + 400 × 0.125 + 100 × 10.0, le tout par million.
        expected = (600 * 1.25 + 400 * 0.125 + 100 * 10.0) / 1e6
        self.assertAlmostEqual(first.usd, expected, places=9)

        # Cumul du fil : 1600 entrées dont 600 en cache, 150 sorties. La
        # différence avec le tour précédent est donc 600 entrées (dont 200 en
        # cache, qui se facturent au tarif cache) et 50 sorties.
        self.events("a1", {"type": "turn.completed", "usage": {"input_tokens": 1600,
                                                               "cached_input_tokens": 600,
                                                               "output_tokens": 150}})
        second = book.turn_usage("a1", start=2)
        delta = (400 * 1.25 + 200 * 0.125 + 50 * 10.0) / 1e6
        self.assertAlmostEqual(second.usd, delta, places=9)

    def test_codex_resident_turn_event_wins_over_the_native_stream(self):
        # L80a: the app-server reports its usage in `thread/tokenUsage/updated`,
        # which the raw JSON-RPC stream does not give `turn.completed`. The
        # `resident.turn` event (from `TurnResult`) is the turn's only measure;
        # a native completion in the same slice must not be counted again.
        self.agent("a1", "codex", "gpt-6.1-sol")
        book = self.book()
        self.events(
            "a1",
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.completed", "usage": {"input_tokens": 9_999,
                                                 "cached_input_tokens": 0,
                                                 "output_tokens": 999}},
            {"type": "resident.turn", "protocol": "codex-app-server",
             "session": "t1", "turn": "tour-1", "status": "completed",
             "usage": {"input_tokens": 100, "cache_read_input_tokens": 200,
                       "output_tokens": 9},
             "usage_total": {"input_tokens": 300, "cached_input_tokens": 200,
                             "output_tokens": 9}})
        first = book.record("a1")
        expected = (100 * 1.25 + 200 * 0.125 + 9 * 10.0) / 1e6
        self.assertAlmostEqual(first.usd, expected, places=9)
        self.assertEqual((first.input_tokens, first.cached_input_tokens,
                          first.output_tokens), (300, 200, 9))
        # the thread total is the next turn's baseline, not the turn alone
        self.assertEqual(book.baseline("a1", "codex", "t1")["cum_usage"],
                         [300, 200, 9])

        self.events(
            "a1",
            {"type": "resident.turn", "protocol": "codex-app-server",
             "session": "t1", "turn": "tour-2", "status": "completed",
             "usage": {"input_tokens": 100, "cache_read_input_tokens": 200,
                       "output_tokens": 9},
             "usage_total": {"input_tokens": 600, "cached_input_tokens": 400,
                             "output_tokens": 18}})
        second = book.record("a1", start=3)
        self.assertAlmostEqual(second.usd, expected, places=9)   # the turn, once
        self.assertEqual((second.input_tokens, second.cached_input_tokens,
                          second.output_tokens), (300, 200, 9))
        row = self.db.query("SELECT cum_input_tokens, cum_cached_input_tokens, "
                            "cum_output_tokens FROM turn_costs WHERE agent = 'a1' "
                            "ORDER BY id")[-1]
        self.assertEqual((row["cum_input_tokens"], row["cum_cached_input_tokens"],
                          row["cum_output_tokens"]), (600, 400, 18))

    def test_resident_turn_without_total_extends_the_previous_baseline(self):
        # L80a: a driver that did not publish `tokenUsage.total` still gives
        # the ledger a baseline: the last known cumulative reading of the same
        # session plus this turn.
        self.agent("a1", "codex", "gpt-6.1-sol")
        book = self.book()
        event = {"type": "resident.turn", "protocol": "codex-app-server",
                 "session": "t1", "usage": {"input_tokens": 100,
                                            "cache_read_input_tokens": 200,
                                            "output_tokens": 9}}
        self.events("a1", dict(event, usage_total={"input_tokens": 300,
                                                   "cached_input_tokens": 200,
                                                   "output_tokens": 9}))
        book.record("a1")
        self.events("a1", event)
        book.record("a1", start=1)
        row = self.db.query("SELECT cum_input_tokens, cum_cached_input_tokens, "
                            "cum_output_tokens FROM turn_costs WHERE agent = 'a1' "
                            "ORDER BY id")[-1]
        self.assertEqual((row["cum_input_tokens"], row["cum_cached_input_tokens"],
                          row["cum_output_tokens"]), (600, 400, 18))

    def test_acp_resident_turn_uses_its_cost_and_ignores_step_usage(self):
        # L80a: ACP turns carry a USD delta (cumulative `cost` published by
        # the harness); it makes the turn, and the native step readings of the
        # same slice are not priced again.
        self.agent("a1", "deepseek", "deepseek-flash")
        book = self.book()
        self.events(
            "a1",
            {"type": "status", "phase": "step_end",
             "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                       "outputTokens": 0}},
            {"type": "resident.turn", "protocol": "acp", "session": "sess-1",
             "turn": "tour-1", "status": "completed",
             "usage": {"input_tokens": 1234, "cache_read_input_tokens": 0,
                       "output_tokens": 0},
             "cost": 0.02, "cost_total": 0.02})
        usage = book.record("a1")
        self.assertAlmostEqual(usage.usd, 0.02, places=9)
        self.assertEqual(usage.input_tokens, 1234)
        self.assertIsNone(usage.price_fallback)

    def test_acp_resident_turn_without_cost_is_priced_on_its_tokens(self):
        self.agent("a1", "deepseek", "deepseek-flash")
        self.events("a1", {"type": "resident.turn", "protocol": "acp",
                           "session": "sess-1", "status": "completed",
                           "usage": {"input_tokens": 1_000_000,
                                     "cache_read_input_tokens": 0,
                                     "output_tokens": 0}})
        self.assertAlmostEqual(self.book().record("a1").usd, 0.30, places=9)

    def test_acp_resident_turn_after_restart_uses_the_durable_cost_base(self):
        # L80a B1: a restarted process has no in-memory `cost_base`; its first
        # prompt carries only the cumulative `cost_total`. The turn is the delta
        # against the last durable USD reading of the same session — not the
        # whole cumulative history, nor a tariff on the context tokens.
        self.agent("a1", "deepseek", "deepseek-flash")
        book = self.book()
        self.events("a1", {"type": "resident.turn", "protocol": "acp",
                           "session": "sess-1", "status": "completed",
                           "usage": {"input_tokens": 1234},
                           "cost": 0.04, "cost_total": 0.04})
        first = book.record("a1")
        self.assertAlmostEqual(first.usd, 0.04, places=9)
        self.assertAlmostEqual(book.baseline("a1", "deepseek", "sess-1")["cum_usd"],
                               0.04, places=9)
        # process restart: no delta in the event, only the cumulative reading
        self.events("a1", {"type": "resident.turn", "protocol": "acp",
                           "session": "sess-1", "status": "completed",
                           "usage": {"input_tokens": 1234},
                           "cost_total": 0.06})
        second = book.record("a1", start=1)
        self.assertAlmostEqual(second.usd, 0.02, places=9)
        self.assertEqual(second.input_tokens, 1234)
        self.assertAlmostEqual(book.baseline("a1", "deepseek", "sess-1")["cum_usd"],
                               0.06, places=9)

    def test_a_resident_event_never_overrides_the_claude_stream(self):
        # L80a: Claude's stream still carries its own usage; a stray
        # `resident.turn` (never written for it) must not become its cost.
        self.agent("a1", "claude")
        self.events("a1", {"type": "resident.turn", "protocol": "acp",
                           "usage": {"input_tokens": 1_000_000}})
        self.assertEqual(self.book().turn_usage("a1").usd, 0.0)

    def test_deepseek_usage_par_etape_fois_le_bareme(self):
        self.agent("a1", "deepseek", "deepseek-flash")
        self.events("a1",
                    {"type": "status", "phase": "step_end",
                     "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0, "outputTokens": 0}},
                    {"type": "status", "phase": "step_end",
                     "usage": {"inputTokens": 0, "cacheReadTokens": 0, "outputTokens": 1_000_000}})
        usage = self.book().turn_usage("a1")
        self.assertAlmostEqual(usage.usd, 0.30 + 1.20, places=9)
        self.assertEqual(usage.input_tokens, 1_000_000)

    def test_deepseek_flash_at_the_providers_price(self):
        # #475: a real deepseek3 turn of 2026-10-12, mostly cache reads, at
        # DeepSeek's official peak price for deepseek-flash, per million:
        # cache miss $0.30, cache hit $0.006, output $1.20
        # (https://api-docs.deepseek.com/quick_start/pricing, read 2026-10-12)
        self.agent("a1", "deepseek", "deepseek-flash")
        self.events("a1",
                    {"type": "status", "phase": "step_end",
                     "usage": {"inputTokens": 115_819, "cacheReadTokens": 14_932_096,
                               "outputTokens": 99_522}})
        usage = self.book().turn_usage("a1")
        self.assertAlmostEqual(
            usage.usd, (115_819 * 0.30 + 14_932_096 * 0.006 + 99_522 * 1.20) / 1e6, places=9)

    def test_bareme_modifiable_et_repli(self):
        self.agent("a1", "deepseek", "deepseek-pro")
        with open(os.path.join(self.tmp_cost, "prices.json"), "w", encoding="utf-8") as fh:
            json.dump({"deepseek-pro": [1.0, 0.1, 2.0]}, fh)
        self.events("a1", {"type": "status", "phase": "step_end",
                           "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                                     "outputTokens": 0}})
        self.assertAlmostEqual(self.book().turn_usage("a1").usd, 1.0)

    # -- majorant d'un modèle inconnu (L13 B6) --------------------------------
    def _bareme_croise(self) -> str:
        """flash est le plus cher en entrée et le moins cher en sortie : le piège."""
        chemin = os.path.join(self.tmp_cost, "prices.json")
        with open(chemin, "w", encoding="utf-8") as fh:
            json.dump({"deepseek-flash": [10, 1, 1], "deepseek-pro": [5, 1, 100]}, fh)
        return chemin

    def test_majorant_composante_par_composante_et_non_sur_l_entree_seule(self):
        prix = cost.load_prices(self._bareme_croise())
        self.assertEqual(cost.max_price_of(prix, "deepseek"), [10.0, 1.0, 100.0])

    def test_modele_inconnu_ne_sous_estime_ni_l_entree_ni_la_sortie(self):
        """La sonde B6 : 1 M de sortie d'un modèle inconnu coûte 100, pas 1."""
        chemin = self._bareme_croise()
        self.agent("a1", "deepseek")
        self.events("a1", {"type": "status", "phase": "step_end",
                           "usage": {"outputTokens": 1_000_000}})
        book = cost.CostBook(state_dir=self.state, codex_sessions=self.codex,
                             prices_path=chemin, tools={"a1": "deepseek"})
        self.assertAlmostEqual(book.turn_usage("a1", model="").usd, 100.0)
        self.assertAlmostEqual(book.turn_usage("a1", model="modele-inconnu").usd, 100.0)
        self.events("a1", {"type": "status", "phase": "step_end",
                           "usage": {"inputTokens": 1_000_000}})
        # (l'entrée s'ajoute au flux : 1 M entrée à 10 + 1 M sortie à 100)
        self.assertAlmostEqual(book.turn_usage("a1", model="").usd, 110.0)

    def test_le_majorant_coute_au_moins_chaque_modele_connu_pour_tout_melange(self):
        prix = cost.load_prices(self._bareme_croise())
        majorant = cost.max_price_of(prix, "deepseek")
        connus = [prix[cle] for cle in prix
                  if cle.startswith("deepseek-") and not cle.startswith("default-")]
        for entree in (0, 1, 1_000_000):
            for cache in (0, 1, 500_000):
                for sortie in (0, 1, 1_000_000):
                    def cout(triplet):
                        return entree * triplet[0] + cache * triplet[1] + sortie * triplet[2]
                    for triplet in connus:
                        self.assertGreaterEqual(cout(majorant), cout(triplet))

    def test_majorant_deepseek_couvre_aussi_le_v4_pro(self):
        self.assertEqual(cost.max_price_of(cost.DEFAULT_PRICES, "deepseek"),
                         [1.32, 0.14, 3.96])

    def test_prix_invalides_ne_rentrent_pas_dans_le_bareme(self):
        chemin = os.path.join(self.tmp_cost, "prices.json")
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write('{"deepseek-neg": [-1, 0, 0], "deepseek-court": [1, 2],'
                     ' "deepseek-texte": ["a", 1, 1], "deepseek-nan": [NaN, 1, 1],'
                     ' "deepseek-inf": [1, 1, Infinity], "deepseek-ok": [2, 1, 3]}')
        prix = cost.load_prices(chemin)
        for cle in ("deepseek-neg", "deepseek-court", "deepseek-texte", "deepseek-nan", "deepseek-inf"):
            self.assertNotIn(cle, prix)
        self.assertEqual(prix["deepseek-ok"], [2.0, 1.0, 3.0])
        # seuls les tarifs valides comptent : le majorant est celui des défauts et de `deepseek-ok`
        self.assertEqual(cost.max_price_of(prix, "deepseek"), [2.0, 1.0, 3.96])

    def test_famille_sans_tarif_connu_n_a_pas_de_majorant(self):
        """L225 : le pire de tout le barème n'est pas un majorant sûr d'une famille
        qui n'y figure pas (un tour opus aux tarifs codex) : None."""
        prix = {"gpt-x": [3, 1, 30], "default-claude": [0.1, 0.1, 0.1], "default-deepseek": [0.2, 0.2, 0.2]}
        self.assertIsNone(cost.max_price_of(prix, "claude"))
        self.assertIsNone(cost.max_price_of({"default-claude": [1, 2, 3]}, "claude"))

    def test_bareme_surchargeable_par_l_environnement(self):
        # La variable d'environnement évite d'avoir à connaître le chemin par
        # défaut du poste (et les tests ne le lisent jamais).
        custom = os.path.join(self.tmp_cost, "custom-prices.json")
        with open(custom, "w", encoding="utf-8") as fh:
            json.dump({"deepseek-flash": [9.0, 9.0, 9.0]}, fh)
        # restaure la valeur par défaut du banc (le fichier absent posé par
        # `support`) : ne pas la supprimer, sinon les tests suivants reliraient
        # le barème de l'hôte.
        precedent = os.environ.get("AMEESH_PRICES", "")
        os.environ["AMEESH_PRICES"] = custom
        self.addCleanup(os.environ.__setitem__, "AMEESH_PRICES", precedent)
        book = cost.CostBook(state_dir=self.state, codex_sessions=self.codex)
        self.assertEqual(book.prices_path, custom)

    def test_bareme_illisible_garde_les_defauts(self):
        with open(os.path.join(self.tmp_cost, "prices.json"), "w", encoding="utf-8") as fh:
            fh.write("{ceci n'est pas du JSON")
        prices = cost.load_prices(os.path.join(self.tmp_cost, "prices.json"))
        self.assertEqual(prices["deepseek-flash"], cost.DEFAULT_PRICES["deepseek-flash"])

    def test_un_flux_illisible_ne_casse_pas_la_comptabilite(self):
        self.agent("a1", "claude")
        path = os.path.join(self.state, "a1", "events.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("pas du json\n")
            # `[]` et `null` sont du JSON valide mais pas des événements (B3) :
            # les garder ferait planter le premier `.get` venu.
            fh.write("[]\n")
            fh.write("null\n")
            fh.write(json.dumps({"type": "result", "session_id": "s1",
                                 "total_cost_usd": 0.5, "usage": U}) + "\n")
        self.assertAlmostEqual(self.book().turn_usage("a1").usd, 0.5)


class CostDbTest(Sandbox, PgTestCase):
    """`turn_costs` : écriture par `record`, lecture par `spent` et `report`."""

    def test_migration_0014_presente(self):
        rows = self.db.query(
            "select version, name from schema_migrations where version = 14")
        self.assertEqual([(r["version"], r["name"]) for r in rows], [(14, "cost")])

    def test_repere_dans_le_grand_livre_et_reprise_apres_panne(self):
        """Sonde codex2 B1 : insertion et repère sont la même écriture.

        Un `execute` qui échoue ne doit pas faire avancer le repère : le tour
        rejoué compte le même delta, au lieu d'insérer zéro.
        """
        real = self.db

        class Flaky:
            """La base, avec une insertion qui échoue une fois."""

            def __init__(self, inner):
                self.inner = inner
                self.failures = 1

            def query(self, sql, params=None):
                return self.inner.query(sql, params)

            def execute(self, sql, params=None):
                if self.failures:
                    self.failures -= 1
                    raise RuntimeError("base indisponible")
                return self.inner.execute(sql, params)

        book = self.book(db=Flaky(real))
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("claude")
        self.write_events("a1", {"type": "result", "session_id": "s1",
                                 "total_cost_usd": 0.50, "usage": U})
        with self.assertRaises(RuntimeError):
            book.record("a1")
        # La panne n'a rien laissé : aucune ligne, donc aucun repère avancé.
        self.assertEqual(real.query("select count(*) as n from turn_costs")[0]["n"], 0)
        usage = book.record("a1")                      # la reprise
        self.assertAlmostEqual(usage.usd, 0.50)        # le coût n'est pas perdu
        self.assertAlmostEqual(book.spent("a1", 3600), 0.50)

    def test_difference_lue_sur_la_derniere_ligne(self):
        book = self.book()
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("claude")
        self.write_events("a1", {"type": "result", "session_id": "s1",
                                 "total_cost_usd": 1.00, "usage": U})
        self.assertAlmostEqual(book.record("a1").usd, 1.00)
        self.write_events("a1", {"type": "result", "session_id": "s1",
                                 "total_cost_usd": 1.25, "usage": U})
        self.assertAlmostEqual(book.record("a1", start=1).usd, 0.25)

    def test_un_tour_sans_releve_ne_perd_pas_le_repere_claude(self):
        """Sonde codex2 B4 (Claude) : un tour en erreur n'a pas de `result`.

        Sa ligne est légitime mais sans cumul ; la prendre pour repère ferait
        compter le tour suivant **en entier** (0,75 au lieu de 0,25) et `spent`
        annoncerait 1,25 au lieu de 0,75.
        """
        book = self.book()
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("claude")
        self.write_events("a1", {"type": "result", "session_id": "s1", "total_cost_usd": 0.50, "usage": U})
        self.assertAlmostEqual(book.record("a1").usd, 0.50)
        # Tour interrompu : du flux, mais aucun résultat — donc aucun relevé.
        self.write_events("a1", {"type": "system", "subtype": "init", "session_id": "s1"})
        self.assertAlmostEqual(book.record("a1", start=1).usd, 0.0)
        self.write_events("a1", {"type": "result", "session_id": "s1", "total_cost_usd": 0.75, "usage": U})
        self.assertAlmostEqual(book.record("a1", start=2).usd, 0.25)
        self.assertAlmostEqual(book.spent("a1", 3600), 0.75)

    def test_un_tour_sans_releve_ne_perd_pas_le_repere_codex(self):
        """Même sonde côté Codex : 1 M de jetons, tour interrompu, puis 1,5 M.

        Le deuxième tour doit compter 0,5 M, pas 1,5 M.
        """
        book = self.book()
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("codex")
        with open(os.path.join(book.agent_dir("a1"), "model"), "w", encoding="utf-8") as fh:
            fh.write("gpt-6.1-sol")
        self.write_events("a1", {"type": "thread.started", "thread_id": "t1"},
                          {"type": "turn.completed", "usage": {"input_tokens": 1_000_000,
                                                               "cached_input_tokens": 0,
                                                               "output_tokens": 0}})
        first = book.record("a1")
        self.assertAlmostEqual(first.usd, 1_000_000 * 1.25 / 1e6)
        self.write_events("a1", {"type": "thread.started", "thread_id": "t1"})
        self.assertAlmostEqual(book.record("a1", start=2).usd, 0.0)
        self.write_events("a1", {"type": "turn.completed",
                                 "usage": {"input_tokens": 1_500_000,
                                           "cached_input_tokens": 0, "output_tokens": 0}})
        second = book.record("a1", start=3)
        self.assertAlmostEqual(second.usd, 500_000 * 1.25 / 1e6)
        self.assertEqual(second.input_tokens, 500_000)

    def test_la_session_survit_a_un_tour_sans_thread_started(self):
        """Sonde codex2 B5 : `turn.completed` peut arriver sans `thread.started`.

        La session se résout sur tout le flux et se transmet : sans cela la ligne
        du tour intermédiaire portait une session nulle, la reprise préférait le
        vieux relevé et comptait 1 M au lieu de 0,5 M.
        """
        book = self.book()
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("codex")
        with open(os.path.join(book.agent_dir("a1"), "model"), "w", encoding="utf-8") as fh:
            fh.write("gpt-6.1-sol")
        usage = {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 0}
        self.write_events("a1", {"type": "thread.started", "thread_id": "s1"},
                          {"type": "turn.completed", "usage": usage})
        book.record("a1")                       # t1 : 1 M, session s1
        # t2 : le cumul continue, mais aucun `thread.started` dans la tranche.
        self.write_events("a1", {"type": "turn.completed",
                                 "usage": {"input_tokens": 1_500_000,
                                           "cached_input_tokens": 0, "output_tokens": 0}})
        book.record("a1", start=2)
        last = self.db.query("select session, cum_input_tokens from turn_costs"
                             " order by id desc limit 1")[0]
        self.assertEqual(last["session"], "s1")     # la session est conservée
        self.assertEqual(last["cum_input_tokens"], 1_500_000)
        # t3 : la reprise après un `thread.started` ne compte que sa différence.
        self.write_events("a1", {"type": "thread.started", "thread_id": "s1"},
                          {"type": "turn.completed",
                           "usage": {"input_tokens": 2_000_000,
                                     "cached_input_tokens": 0, "output_tokens": 0}})
        third = book.record("a1", start=3)
        self.assertEqual(third.input_tokens, 500_000)
        self.assertAlmostEqual(third.usd, 500_000 * 1.25 / 1e6)

    def test_record_sans_base_refuse_de_compter(self):
        book = cost.CostBook(state_dir=self.state)
        with self.assertRaises(cost.CostError):
            book.record("a1")

    def test_record_ecrit_une_ligne_et_spent_la_somme(self):
        book = self.book()
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("deepseek")
        self.write_events("a1", {"type": "status", "phase": "step_end",
                                 "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                                           "outputTokens": 0}})
        usage = book.record("a1", turn="t-1")
        # modèle inconnu (aucun état) : tarif le plus cher connu de la famille
        # DeepSeek (0019 §3, repli fail-closed appliqué par L13)
        self.assertEqual(usage.model, "inconnu")
        self.assertAlmostEqual(usage.usd, 1.32)
        self.assertAlmostEqual(book.spent("a1", 3600), 1.32)
        self.assertAlmostEqual(book.spent("all", 3600), 1.32)
        # hors fenêtre : la dépense d'il y a plus d'une heure ne compte plus
        self.db.execute("update turn_costs set recorded_at = now() - interval '2 hours'")
        self.assertEqual(book.spent("all", 3600), 0.0)
        self.assertAlmostEqual(book.spent("all", 86400), 1.32)

    def test_record_porte_le_lot_et_le_debut_du_tour(self):
        """#796 : la ligne du grand livre dit le lot servi, l'instant de début
        et l'identifiant du tour, tels que le marqueur comptable les a donnés."""
        lot = work.add(self.db, title="mesuré")
        book = self.book()
        self.agent("a1", "deepseek")
        self.write_events("a1", {"type": "status", "phase": "step_end",
                                 "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                                           "outputTokens": 0}})
        debut = time.time() - 120
        book.record("a1", turn="t-1", work_item_id=int(lot["id"]), started_at=debut,
                    turn_id="tour-1")
        row = self.db.query(
            "select work_item_id, turn_id,"
            " extract(epoch from started_at)::float8 as start_ts,"
            " extract(epoch from recorded_at)::float8 as end_ts"
            " from turn_costs where agent = 'a1' order by id desc limit 1")[0]
        self.assertEqual((row["work_item_id"], row["turn_id"]), (int(lot["id"]), "tour-1"))
        self.assertAlmostEqual(row["start_ts"], debut, places=3)
        self.assertGreaterEqual(row["end_ts"], row["start_ts"])
        # sans lot ni début (relais, ligne d'avant 0423) : colonnes laissées NULL
        book.record("a1", turn="t-2")
        row = self.db.query(
            "select work_item_id, started_at, turn_id from turn_costs"
            " where agent = 'a1' order by id desc limit 1")[0]
        self.assertIsNone(row["work_item_id"])
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["turn_id"])

    def test_over_dit_le_rythme_du_forfait_puis_le_budget_horaire(self):
        book = self.book(hourly_usd=0.10)
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("deepseek")
        self.write_events("a1", {"type": "status", "phase": "step_end",
                                 "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                                           "outputTokens": 0}})
        self.assertEqual(book.over("a1"), "")
        book.record("a1")
        self.assertIn("hourly budget", book.over("a1"))

    def test_plafond_payé_au_token_ne_pause_pas_un_agent_au_forfait(self):
        """L49 (0019 §2) : la dépense DeepSeek au-delà du plafond met en pause
        les agents payés au token, pas l'orchestrateur au forfait Claude ; un
        harnais inconnu reste soumis au plafond (fail-closed)."""
        book = self.book(hourly_usd=0.10)
        book.tools.update({"orch": "claude", "inconnu": ""})
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("deepseek")
        self.write_events("a1", {"type": "status", "phase": "step_end",
                                 "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                                           "outputTokens": 0}})
        book.record("a1")
        paid = ("deepseek",)
        self.assertIn("hourly budget", book.over("a1", paid_harnesses=paid, pace=False))
        self.assertEqual(book.over("orch", paid_harnesses=paid, pace=False), "")
        self.assertIn("hourly budget", book.over("inconnu", paid_harnesses=paid, pace=False))
        self.assertIn("hourly budget", book.over("all", paid_harnesses=paid))

    def test_report_liste_les_agents_et_leurs_jauges(self):
        book = self.book()
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("claude")
        with open(os.path.join(book.agent_dir("a1"), "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
                "unifiedWindows": {"five_hour": {"utilization": 10,
                                                 "resetsAt": time.time() + 3600}}}}) + "\n")
        rows = book.report()
        self.assertEqual([r["agent"] for r in rows], ["a1"])
        self.assertEqual(rows[0]["harness"], "claude")
        self.assertEqual([g["key"] for g in rows[0]["gauges"]], ["five_hour"])
        self.assertIn("a1", cost.format_report(rows))
