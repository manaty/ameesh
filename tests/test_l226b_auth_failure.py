# SPDX-License-Identifier: AGPL-3.0-only
"""L226b — classification of authentication failures in failed turns."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from ameesh import account_turn, accounts, auth_failure, cost, exploitation, registry, storage

from .support import FAKEBIN
from .test_l30_accounts import _Base, _codex_rollout


class ClassifyTest(unittest.TestCase):
    def test_known_auth_messages(self):
        cases = {
            'API Error: 401 {"type":"error","error":{"type":"authentication_error",'
            '"message":"OAuth token has expired."}}': "authentication_error",
            "Invalid API key · Please run /login": "invalid api key",
            "Please run /login to authenticate": "login required",
            "ERROR: (gcloud.auth.print-access-token) Reauthentication failed. "
            "cannot prompt during non-interactive execution.": "gcloud reauthentication failed",
            "unexpected status 401 Unauthorized: Missing bearer": "http 401",
            "OAuth token has expired": "token expired or revoked",
            "invalid_grant: Token has been expired or revoked.": "token expired or revoked",
        }
        for text, reason in cases.items():
            self.assertEqual(auth_failure.classify(error=text), reason, text)
            self.assertEqual(auth_failure.classify(stderr_tail="noise\n" + text), reason, text)

    def test_result_text_read_only_when_short(self):
        self.assertEqual(auth_failure.classify(error="success",
                                               result="Invalid API key · Please run /login"),
                         "invalid api key")
        long_answer = ("I fixed the handler that returned 401 Unauthorized for expired "
                       "tokens. " * 20)
        self.assertGreater(len(long_answer), auth_failure.MAX_RESULT_CHARS)
        self.assertIsNone(auth_failure.classify(error="success", result=long_answer))

    def test_other_failures_are_not_auth(self):
        for text in ("rate_limit_error: 429 Too Many Requests", "overloaded_error",
                     "le harnais a rendu le code 1", "context window exceeded",
                     "bail perdu en cours de tour", "HTTP 4010 widgets", ""):
            self.assertIsNone(auth_failure.classify(error=text, stderr_tail=text), text)

    def test_stderr_tail(self):
        self.assertEqual(auth_failure.stderr_tail(None), "")
        self.assertEqual(auth_failure.stderr_tail("/nonexistent/stderr.log"), "")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stderr.log")
            with open(path, "wb") as fh:
                fh.write(b"x" * 20000 + b"\xff Reauthentication failed.\n")
            tail = auth_failure.stderr_tail(path)
            self.assertLessEqual(len(tail.encode("utf-8")), auth_failure.STDERR_TAIL_BYTES + 8)
            self.assertEqual(auth_failure.classify(stderr_tail=tail),
                             "gcloud reauthentication failed")


class HealthyCandidatesTest(unittest.TestCase):
    """`accounts.healthy`: never a pay-per-token fallback (owner, 2026-10-07)."""

    def test_plan_account_never_replaced_by_an_api_key(self):
        items = [accounts.Profile(harness="claude", name="primaire"),
                 accounts.Profile(harness="claude", name="secondaire", path="/x"),
                 accounts.Profile(harness="claude", name="cle", kind="api_key_env",
                                  env="ANTHROPIC_API_KEY", key_env="K")]
        # not even when every plan account is healthy
        self.assertEqual(accounts.healthy(items, {}), items[:2])
        self.assertEqual([p.name for p in accounts.healthy(items, {"primaire": {}})],
                         ["secondaire"])
        self.assertEqual(accounts.healthy(items, {"primaire": {}, "secondaire": {}}), [])

    def test_api_keys_among_themselves(self):
        items = [accounts.Profile(harness="deepseek", name=n, kind="api_key_env",
                                  key_env="K") for n in ("a", "b")]
        self.assertEqual([p.name for p in accounts.healthy(items, {"a": {}})], ["b"])


HOUR = 3600.0
WEEK = 7 * 24 * HOUR


class _Book:
    """A cost book whose gauges are given per account."""

    db = None

    def __init__(self, gauges: dict):
        self._gauges = gauges

    def claude_gauges(self, account=None, primary=None):
        return self._gauges.get(account, [])

    def record_gauges(self, gauges, account=None):
        return 0


class _CapsBase(unittest.TestCase):
    """Claude accounts (primary, secondary, tertiary) and their gauges."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l226b-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _items(self, **settings):
        raw = [{"name": "primaire"}]
        for name in ("secondaire", "tertiaire"):
            path = os.path.join(self.tmp, name)
            os.makedirs(path, mode=0o700, exist_ok=True)
            raw.append({"name": name, "path": path, "check_credentials": False,
                        **settings.get(name, {})})
        raw[0].update(settings.get("primaire", {}))
        return accounts.parse({"claude": raw})["claude"]

    @staticmethod
    def _gauges(five, seven, now, overage=None, five_elapsed=0.9):
        return cost.Gauges([
            cost.Gauge("claude", "five_hour", five, now + (1 - five_elapsed) * 5 * HOUR,
                       5 * HOUR),
            cost.Gauge("claude", "seven_day", seven, now + 0.1 * WEEK, WEEK)], overage)

    def _ev(self, items, gauges, now):
        book = _Book(gauges)
        return {p.name: accounts.evaluate(book, p, items, now) for p in items}


class CapsAndOrderTest(_CapsBase):
    """L226b, owner decision 2026-10-07: primaries last and capped at 50 %,
    overage-capable accounts capped at 80 %, never an account in overage."""

    def test_settings_parsed_and_checked(self):
        items = self._items(tertiaire={"priority": 1, "overage": "allowed",
                                       "overage_cap": 0.7, "short_window_cap": 0.8},
                            primaire={"cap": 0.4})
        primary, secondary, tertiary = items
        self.assertEqual((primary.cap, accounts.account_cap(primary)), (0.4, 0.4))
        self.assertEqual(accounts.account_cap(secondary), None)
        self.assertEqual([accounts.priority(p) for p in items], [100, 50, 1])
        self.assertEqual((tertiary.overage, tertiary.overage_cap, tertiary.short_window_cap),
                         ("allowed", 0.7, 0.8))
        self.assertEqual(accounts.account_cap(self._items()[0]), accounts.PRIMARY_CAP)
        for bad in ({"cap": 0}, {"cap": 1.5}, {"cap": "x"}, {"priority": "1"},
                    {"priority": True}, {"overage": "maybe"}, {"short_window_cap": -1}):
            with self.assertRaises(accounts.AccountError, msg=bad):
                self._items(secondaire=bad)

    def test_primary_ineligible_above_half_of_any_window(self):
        now = 1_800_000_000.0
        items = self._items()
        evs = self._ev(items, {"primaire": self._gauges(0.20, 0.50, now)}, now)
        self.assertFalse(evs["primaire"].ok)
        self.assertIn("primary cap 50%", evs["primaire"].reason)
        self.assertEqual(evs["primaire"].limits["seven_day"], (0.5, "primary cap"))
        evs = self._ev(items, {"primaire": self._gauges(0.49, 0.30, now)}, now)
        self.assertTrue(evs["primaire"].ok)
        # the same reading on a secondary account is under its pace cap
        evs = self._ev(items, {"secondaire": self._gauges(0.20, 0.50, now)}, now)
        self.assertTrue(evs["secondaire"].ok)

    def test_overage_capable_capped_at_80_and_never_in_overage(self):
        now = 1_800_000_000.0
        items = self._items(secondaire={"short_window_cap": 0.95})
        capable = cost.Overage(capable=True)
        evs = self._ev(items, {"secondaire": self._gauges(0.80, 0.10, now, capable)}, now)
        self.assertFalse(evs["secondaire"].ok)
        self.assertIn("overage cap 80%", evs["secondaire"].reason)
        evs = self._ev(items, {"secondaire": self._gauges(0.80, 0.10, now,
                                                          cost.Overage(capable=False))}, now)
        self.assertTrue(evs["secondaire"].ok)
        # declared: `overage: none` ignores the harness, `allowed` caps without a reading
        none = self._items(secondaire={"short_window_cap": 0.95, "overage": "none"})
        evs = self._ev(none, {"secondaire": self._gauges(0.80, 0.10, now, capable)}, now)
        self.assertTrue(evs["secondaire"].ok)
        allowed = self._items(secondaire={"short_window_cap": 0.95, "overage": "allowed"})
        evs = self._ev(allowed, {"secondaire": self._gauges(0.80, 0.10, now)}, now)
        self.assertFalse(evs["secondaire"].ok)
        # in overage: never chosen, whatever its usage
        evs = self._ev(items, {"tertiaire": self._gauges(0.10, 0.10, now, cost.Overage(
            capable=True, in_use=True))}, now)
        self.assertFalse(evs["tertiaire"].ok)
        self.assertIn("in overage", evs["tertiaire"].reason)

    def test_short_window_cap_replaces_the_pace_cap(self):
        # bureau's sprint patch of 2026-10-11 (sprint.json short_window_cap 0.80)
        now = 1_800_000_000.0
        items = self._items(secondaire={"short_window_cap": 0.80})
        early = {"secondaire": self._gauges(0.60, 0.10, now, five_elapsed=0.3),
                 "tertiaire": self._gauges(0.60, 0.10, now, five_elapsed=0.3)}
        evs = self._ev(items, early, now)
        self.assertTrue(evs["secondaire"].ok)          # 60 % < 80 % fixed
        self.assertFalse(evs["tertiaire"].ok)          # 60 % > pace cap 40 %
        evs = self._ev(items, {"secondaire": self._gauges(0.80, 0.10, now,
                                                          five_elapsed=0.3)}, now)
        self.assertIn("short window cap 80%", evs["secondaire"].reason)
        # the long window keeps the pace cap
        self.assertEqual(evs["secondaire"].limits["seven_day"][1], "pace cap")

    def test_failover_order_primary_last(self):
        now = 1_800_000_000.0
        items = self._items(tertiaire={"priority": 10})
        # the primary is the least used, yet comes last
        gauges = {"primaire": self._gauges(0.05, 0.05, now),
                  "secondaire": self._gauges(0.30, 0.30, now),
                  "tertiaire": self._gauges(0.60, 0.60, now)}
        evs = self._ev(items, gauges, now)
        ranked = accounts.rank(items, evs, now)
        self.assertEqual([p.name for p in ranked], ["tertiaire", "secondaire", "primaire"])
        self.assertIn("first in the failover order", accounts.reason_of(ranked, evs, now))
        # without priorities, the pace decides among the others; primary still last
        items = self._items()
        evs = self._ev(items, gauges, now)
        self.assertEqual([p.name for p in accounts.rank(items, evs, now)],
                         ["secondaire", "tertiaire", "primaire"])


class OverageReadingTest(unittest.TestCase):
    def test_claude_rate_limit_event(self):
        event = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "overageStatus": "rejected",
            "overageDisabledReason": "out_of_credits", "isUsingOverage": False,
            "unifiedWindows": {"five_hour": {"utilization": 0.82, "resetsAt": 1791742800}}}}
        self.assertEqual(cost._claude_overage(event), cost.Overage(False, False))
        event["rate_limit_info"].update(overageStatus="allowed", isUsingOverage=True)
        self.assertEqual(cost._claude_overage(event), cost.Overage(True, True))
        self.assertEqual(cost._claude_overage({"type": "rate_limit_event"}), cost.Overage())

    def test_codex_credits(self):
        gauges = [cost.Gauge("codex", "codex-300min", 0.5, None, 300 * 60)]
        self.assertEqual(cost._codex_overage({}, gauges), cost.Overage())
        limits = {"credits": {"has_credits": True, "unlimited": False, "balance": None}}
        self.assertEqual(cost._codex_overage(limits, gauges), cost.Overage(True, False))
        full = [cost.Gauge("codex", "codex-300min", 1.0, None, 300 * 60)]
        self.assertEqual(cost._codex_overage(limits, full), cost.Overage(True, True))

    def test_codex_journal_carries_the_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "sessions", "2026", "10", "11")
            os.makedirs(folder)
            with open(os.path.join(folder, "rollout.jsonl"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "event_msg", "payload": {
                    "type": "token_count", "rate_limits": {
                        "primary": {"used_percent": 5.0, "window_minutes": 300,
                                    "resets_at": time.time() + 3600},
                        "secondary": None,
                        "credits": {"has_credits": True}}}}) + "\n")
            gauges = cost.CostBook(state_dir=tmp).codex_gauges(
                sessions=os.path.join(tmp, "sessions"))
            self.assertEqual(len(gauges), 1)
            self.assertEqual(gauges.overage, cost.Overage(True, False))


class _AuthBase(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM credential_health")

    def _health(self, harness: str) -> dict:
        return storage.of(self.db).credentials.health(self.cfg.host, harness)

    def _report(self, harness, account, state, source="sentinel"):
        return storage.of(self.db).credentials.report(
            self.cfg.host, harness, account, state, reason="test", source=source)


class CredentialsStorageTest(_AuthBase):
    def test_report_keeps_failing_since_and_clears_it(self):
        self.assertIsNone(self._report("claude", "", "failing")["previous"])
        first = self._health("claude")[""]["failing_since_ts"]
        self.assertIsNotNone(first)
        self.assertEqual(self._report("claude", "", "failing", "turn")["previous"], "failing")
        row = self._health("claude")[""]
        self.assertEqual((row["failing_since_ts"], row["source"]), (first, "turn"))
        self.assertEqual(self._report("claude", "", "healthy")["previous"], "failing")
        self.assertIsNone(self._health("claude")[""]["failing_since_ts"])
        self.assertEqual(auth_failure.failing(self.db, self.cfg.host, "claude"), {})


class ChooseTest(_AuthBase):
    """L30 `choose` restricted to healthy accounts."""

    def _items(self):
        return accounts.parse({"codex": self._codex_comptes()})["codex"]

    def test_failing_account_skipped_even_by_session_continuity(self):
        items = self._items()
        now = time.time()
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        self.assertEqual(accounts.choose(self.db, self.cfg.host, "codex", items, book,
                                         now=now).profile.name, "primaire")
        self._report("codex", "primaire", "failing", "turn")
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now,
                                session_account="primaire")
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertFalse(choix.auth_blocked)
        self.assertIn("primaire authentication failing", choix.switched["reason"])
        # healthy again: back to the usual rule (declared order breaks the tie)
        self._report("codex", "primaire", "healthy")
        self.assertEqual(accounts.choose(self.db, self.cfg.host, "codex", items, book,
                                         now=now).profile.name, "primaire")

    def test_no_healthy_account_is_auth_blocked_not_a_budget_pause(self):
        items = self._items()
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        for nom in ("primaire", "secondaire"):
            self._report("codex", nom, "failing")
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book)
        self.assertTrue(choix.auth_blocked)
        self.assertEqual(choix.profile.name, "primaire")
        self.assertIn("no healthy codex account", choix.why)

    def test_healthy_ones_at_threshold_is_still_a_budget_pause(self):
        items = self._items()
        now = time.time()
        _codex_rollout(items[1].path, 96, now + 3600)
        self._report("codex", "primaire", "failing")
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertTrue(choix.auth_blocked)  # primaire is under its threshold, but sick
        _codex_rollout(items[0].path, 95, now + 3600)
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertIsNone(choix.profile)
        self.assertIn("at threshold", choix.reason)


class ContinuityCapTest(_AuthBase):
    """The caps apply to continuity too: a session on the primary leaves it
    above 50 %, for the account first in the order (L226b)."""

    def test_session_leaves_the_primary_above_half(self):
        secondaire = self._codex_comptes()[1]
        home = os.path.join(self.tmp, "codex-default")
        os.makedirs(home, mode=0o700, exist_ok=True)
        items = accounts.parse({"codex": [{"name": "primaire"}, secondaire]})["codex"]
        now = time.time()
        with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
            book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
            _codex_rollout(home, 40, now + 1800)   # under 50 % (and its pace cap, 90 %)
            choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now,
                                    session_account="primaire")
            self.assertTrue(choix.kept)
            self.assertEqual(choix.profile.name, "primaire")
            _codex_rollout(home, 55, now + 1800)
            choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now,
                                    session_account="primaire")
        self.assertFalse(choix.kept)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertIn("primary cap 50%", choix.why)


class RunnerTest(_AuthBase):
    """The runner: auth failure of a turn, failover, auth_blocked, recovery."""

    def _suite(self, worker):
        """The next turn, consigne left pending by the failed one (fake harness)."""
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("AMEESH_TEST_RESULT", "AMEESH_TEST_EXIT"):
                os.environ.pop(name, None)
            spec = worker.pick()
            if spec is not None:
                worker.run_turn(spec)
            return spec

    LOGIN = {"AMEESH_TEST_RESULT": "Invalid API key · Please run /login",
             "AMEESH_TEST_EXIT": "1"}

    def test_auth_failure_fails_over_to_a_healthy_account(self):
        comptes = self._claude_comptes(partage=True)
        runner, worker = self._worker("au", "claude", {"claude": comptes})
        try:
            self.assertIsNotNone(self._tour(worker, **self.LOGIN))
            self.assertFalse(worker.fast_failure)
            ligne = registry.get(self.db, "au")
            self.assertEqual(ligne["status"], "blocked")
            self.assertEqual(ligne["status_text"], "auth_blocked: claude/primaire")
            self.assertEqual(ligne["pending_prompt"], "travail")  # consigne remise
            sante = self._health("claude")["primaire"]
            self.assertEqual((sante["state"], sante["source"], sante["reason"]),
                             ("failing", "turn", "invalid api key"))
            # next turn: on the healthy account, block lifted
            self.assertIsNotNone(self._suite(worker))
            self.assertEqual(self.turns()[-1]["compte"]["CLAUDE_CONFIG_DIR"],
                             comptes[1]["path"])
            self.assertEqual(registry.get(self.db, "au")["status"], "idle")
            fil = self._fil()
            self.assertIn("Turn failed authentication on claude/primaire", fil)
            self.assertIn("Block auth_blocked: claude/primaire lifted", fil)
        finally:
            worker.watchdog_stop.set()

    def test_no_healthy_account_stays_blocked_alerts_and_resumes(self):
        runner, worker = self._worker("nu", "claude", {})
        try:
            self.assertIsNotNone(self._tour(worker, **self.LOGIN))
            self.assertEqual(len(self.turns()), 1)
            ligne = registry.get(self.db, "nu")
            self.assertEqual(ligne["status_text"], "auth_blocked: claude/(default)")
            self.assertTrue(ligne["last_error"].startswith("auth_blocked: invalid api key"))
            # no relaunch: nothing consumed, no turn
            self.assertIsNone(self._suite(worker))
            self.assertIsNone(self._suite(worker))
            self.assertEqual(len(self.turns()), 1)
            self.assertEqual(registry.get(self.db, "nu")["pending_prompt"], "travail")
            alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                       if a["type"] == "auth_blocked"]
            self.assertEqual([(a["agent"], a["harness"], a["account"]) for a in alertes],
                             [("nu", "claude", "(default)")])
            # the sentinel sees the credential healthy again: the agent resumes
            self._report("claude", "", "healthy")
            self.assertIsNotNone(self._suite(worker))
            self.assertEqual(len(self.turns()), 2)
            self.assertEqual(registry.get(self.db, "nu")["status"], "idle")
            self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db)
                              if a["type"] == "auth_blocked"], [])
        finally:
            worker.watchdog_stop.set()

    def test_sentinel_failing_blocks_before_any_turn(self):
        runner, worker = self._worker("pre", "claude", {})
        try:
            self._report("claude", "", "failing")
            self.assertIsNone(self._tour(worker))
            self.assertEqual(self.turns(), [])
            ligne = registry.get(self.db, "pre")
            self.assertEqual((ligne["status"], ligne["status_text"]),
                             ("blocked", "auth_blocked: claude/(default)"))
            self.assertEqual(ligne["pending_prompt"], "travail")
        finally:
            worker.watchdog_stop.set()

    def test_other_failures_keep_the_usual_path(self):
        runner, worker = self._worker("autre", "claude", {})
        try:
            self._tour(worker, AMEESH_TEST_RESULT="rate_limit_error: 429",
                       AMEESH_TEST_EXIT="1")
            ligne = registry.get(self.db, "autre")
            self.assertFalse(ligne["status_text"].startswith("auth_blocked"))
            self.assertEqual(self._health("claude"), {})
        finally:
            worker.watchdog_stop.set()


class CliTest(_AuthBase):
    def _config(self, comptes: dict) -> str:
        chemin = os.path.join(self.tmp, "config-hote.json")
        with open(chemin, "w", encoding="utf-8") as fh:
            json.dump({"accounts": comptes}, fh)
        return chemin

    def test_accounts_healthy(self):
        comptes = self._codex_comptes()
        env = self.env(AMEESH_CONFIG=self._config({"codex": comptes}))
        self._report("codex", "secondaire", "failing", "turn")
        self.assertEqual(self.mesh("accounts", "healthy", "codex", env=env).returncode, 1)
        self.assertEqual(self.mesh("accounts", "healthy", "codex", "inconnu",
                                   env=env).returncode, 1)
        proc = self.mesh("accounts", "healthy", "codex", "secondaire", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self._health("codex")["secondaire"]
        self.assertEqual((row["state"], row["source"]), ("healthy", "manual"))
        data = json.loads(self.mesh("accounts", "list", "--json", env=env).stdout)
        self.assertEqual([(c["account"], c["state"]) for c in data["credentials"]],
                         [("secondaire", "healthy")])
        # a harness without declared accounts: its default credential
        proc = self.mesh("accounts", "healthy", "claude", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._health("claude")[""]["state"], "healthy")


class StaleOverageTest(_CapsBase):
    """Codex review r1 (P2): an overage reading goes stale once the windows
    that ran out have reset, or an unused account would stay excluded."""

    def _reading(self, now, five_reset):
        return cost.Gauges([
            cost.Gauge("claude", "five_hour", 1.0, five_reset, 5 * HOUR),
            cost.Gauge("claude", "seven_day", 0.30, now + 0.1 * WEEK, WEEK)],
            cost.Overage(capable=True, in_use=True))

    def test_in_overage_until_the_spent_window_resets(self):
        now = 1_800_000_000.0
        items = self._items(tertiaire={"overage": "none"})
        running = self._reading(now, now + HOUR)
        evs = self._ev(items, {"tertiaire": running}, now)
        self.assertFalse(evs["tertiaire"].ok)
        self.assertIn("in overage", evs["tertiaire"].reason)
        self.assertEqual(evs["tertiaire"].until, now + HOUR)  # not the 7-day reset
        evs = self._ev(items, {"tertiaire": self._reading(now, now - 60)}, now)
        self.assertTrue(evs["tertiaire"].ok, evs["tertiaire"].reason)
        # a spent window with no reset date keeps the flag
        undated = self._reading(now, None)
        self.assertTrue(undated.in_overage(now))
        self.assertFalse(cost.Gauges(list(undated)).in_overage(now))


class ForcedRaceTest(_AuthBase):
    """Codex review r1 (P1): a manual force posted while `choose` switches is
    evaluated like any forced account, never launched past its caps."""

    def test_forced_primary_above_its_cap_is_not_launched(self):
        secondaire = self._codex_comptes()[1]
        home = os.path.join(self.tmp, "codex-default")
        os.makedirs(home, mode=0o700, exist_ok=True)
        items = accounts.parse({"codex": [{"name": "primaire"}, secondaire]})["codex"]
        now = time.time()
        store_type = type(storage.of(self.db).accounts)
        original = store_type.switch

        def switch(store, host, harness, *, expected, to, kind, reason, agent, **kw):
            # the owner forces the primary between our read and our switch
            original(store, host, harness, expected=None, to="primaire", kind="manuel",
                     reason="forçage manuel", agent=None, forced=True)
            return False

        with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
            book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
            _codex_rollout(home, 60, now + 1800)  # above the primary cap (50 %)
            with mock.patch.object(store_type, "switch", switch):
                choix = accounts.choose(self.db, self.cfg.host, "codex", items, book,
                                        now=now)
        self.assertTrue(choix.forced)
        self.assertIsNone(choix.profile)
        self.assertEqual(choix.active.name, "primaire")
        self.assertIn("primary cap 50%", choix.reason)


class RotationSummaryTest(_CapsBase):
    """Codex review r1 (P1): the summary of a failover rotation runs on the
    old account only if the choice could still take it apart from pace — no
    model call on an account in auth failure, past a fixed cap or in overage."""

    def test_summary_possible(self):
        now = 1_800_000_000.0
        items = self._items()
        primaire, secondaire = items[0], items[1]
        quiet = self._gauges(0.30, 0.10, now)
        ok, why = account_turn.summary_possible(primaire, _Book({"primaire": quiet}), items,
                                                now=now)
        self.assertTrue(ok, why)
        # pace cap only (0027): still usable for a summary
        paced = self._gauges(0.95, 0.10, now, five_elapsed=0.3)
        ok, why = account_turn.summary_possible(secondaire, _Book({"secondaire": paced}),
                                                items, now=now)
        self.assertTrue(ok, why)
        ok, why = account_turn.summary_possible(primaire, _Book({"primaire": quiet}), items,
                                                now=now, sick={"primaire": {
                                                    "reason": "invalid api key"}})
        self.assertFalse(ok)
        self.assertIn("invalid api key", why)
        ok, why = account_turn.summary_possible(
            primaire, _Book({"primaire": self._gauges(0.60, 0.10, now)}), items, now=now)
        self.assertFalse(ok)
        self.assertIn("primary cap 50", why)
        ok, why = account_turn.summary_possible(
            secondaire, _Book({"secondaire": self._gauges(0.10, 0.10, now, cost.Overage(
                capable=True, in_use=True))}), items, now=now)
        self.assertFalse(ok)
        self.assertIn("overage", why)
        ok, why = account_turn.summary_possible(
            secondaire, _Book({"secondaire": self._gauges(0.85, 0.10, now, cost.Overage(
                capable=True))}), items, now=now)
        self.assertFalse(ok)
        self.assertIn("overage cap 80", why)

    def test_rotation_off_a_failing_account_makes_no_model_call(self):
        items = self._items()
        worker = mock.Mock()
        worker.name = "rot"
        worker.cfg.state_dir = self.tmp
        worker.cfg.host = "h"
        with mock.patch.object(accounts, "profiles", return_value=items), \
                mock.patch.object(auth_failure, "failing",
                                  return_value={"primaire": {"reason": "http 401"}}), \
                mock.patch.object(registry, "clear_session", return_value=False):
            account_turn.rotate(worker, "claude", items[0], items[1], "sess-1")
        worker.run_turn.assert_not_called()


class TurnStderrTest(_AuthBase):
    """Codex review r1 (P2): a turn is classified on its own stderr, never on
    an earlier turn's message left in the append-mode log."""

    def test_stderr_tail_from_the_turn_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stderr.log")
            with open(path, "wb") as fh:
                fh.write(b"Invalid API key \xc2\xb7 Please run /login\n")
                start = fh.tell()
                fh.write(b"rate_limit_error: 429\n")
            self.assertEqual(auth_failure.stderr_tail(path, start=start),
                             "rate_limit_error: 429\n")
            self.assertEqual(auth_failure.stderr_tail(path, start=None), "")
            self.assertIn("/login", auth_failure.stderr_tail(path))

    def test_earlier_auth_message_does_not_block_a_later_429(self):
        runner, worker = self._worker("r429", "claude", {})
        try:
            with open(worker._path("stderr.log"), "ab") as fh:
                fh.write(b"Invalid API key \xc2\xb7 Please run /login\n")
            self._tour(worker, AMEESH_TEST_RESULT="rate_limit_error: 429",
                       AMEESH_TEST_EXIT="1")
            ligne = registry.get(self.db, "r429")
            self.assertFalse((ligne["status_text"] or "").startswith("auth_blocked"))
            self.assertEqual(self._health("claude"), {})
        finally:
            worker.watchdog_stop.set()


class ReviewR2Test(_CapsBase):
    """Codex review r2: unreadable gauges and a reading without windows."""

    def test_unreadable_gauges_allow_no_summary(self):
        class Broken(_Book):
            def claude_gauges(self, account=None, primary=None):
                raise ValueError("window_minutes: 'bad'")

        items = self._items()
        ok, why = account_turn.summary_possible(items[1], Broken({}), items,
                                                now=1_800_000_000.0)
        self.assertFalse(ok)
        self.assertIn("unreadable", why)

    def test_overage_reading_without_windows(self):
        now = 1_800_000_000.0
        items = self._items()
        bare = cost.Gauges([], cost.Overage(capable=True, in_use=True))
        evs = self._ev(items, {"secondaire": bare}, now)  # no ValueError
        self.assertFalse(evs["secondaire"].ok)
        self.assertIn("in overage", evs["secondaire"].reason)
        self.assertIsNone(evs["secondaire"].until)


class RunnerReviewR2Test(_AuthBase):
    """Codex review r2: ordinary rotation behind the turn guards, and the
    auth_blocked mark set again after another status replaced it."""

    def test_ordinary_rotation_waits_for_a_healthy_account(self):
        runner, worker = self._worker("rot", "claude", {})
        try:
            worker.rotation_forcee = "test"
            self._report("claude", "", "failing")
            with mock.patch.object(worker, "current_session", return_value="sess-1"), \
                    mock.patch.object(worker, "run_turn", return_value=False) as tour:
                self.assertFalse(worker.maybe_rotate())
                tour.assert_not_called()
                self.assertEqual(worker.rotation_forcee, "test")  # kept for later
                self.assertTrue(registry.get(self.db, "rot")["status_text"]
                                .startswith("auth_blocked"))
                self._report("claude", "", "healthy")
                worker.maybe_rotate()
                tour.assert_called_once()
        finally:
            worker.watchdog_stop.set()

    def test_ordinary_rotation_only_on_the_session_account(self):
        comptes = self._claude_comptes(partage=True)
        runner, worker = self._worker("rot2", "claude", {"claude": comptes})
        try:
            worker.rotation_forcee = "test"
            registry.set_session_account(self.db, "rot2", "primaire")
            worker.agent = registry.get(self.db, "rot2")
            # the guard chose the other account: the switch of the next turn rotates
            self._report("claude", "primaire", "failing")
            with mock.patch.object(worker, "current_session", return_value="sess-1"), \
                    mock.patch.object(worker, "run_turn", return_value=False) as tour:
                self.assertFalse(worker.maybe_rotate())
                tour.assert_not_called()
        finally:
            worker.watchdog_stop.set()

    def test_auth_mark_set_again_after_a_budget_pause(self):
        runner, worker = self._worker("ab", "claude", {})
        try:
            self._report("claude", "", "failing")
            self.assertIsNone(self._tour(worker))
            texte = "auth_blocked: claude/(default)"
            self.assertEqual(registry.get(self.db, "ab")["status_text"], texte)
            # a budget pause replaced the mark, then lifted itself: idle
            registry.set_status(self.db, "ab", "blocked", status_text="budget : test")
            registry.set_status(self.db, "ab", "idle", status_text="")
            self.assertIsNone(worker.pick())
            ligne = registry.get(self.db, "ab")
            self.assertEqual((ligne["status"], ligne["status_text"]), ("blocked", texte))
            self.assertEqual([a["agent"] for a in exploitation.alerts(self.cfg, self.db)
                              if a["type"] == "auth_blocked"], ["ab"])
        finally:
            worker.watchdog_stop.set()


class LegacySessionTest(_CapsBase):
    """Codex review r3 (P1): a session from before L30 (no account recorded)
    ran on the harness's default folder; the failover reads health and gauges
    under the declared account of that folder, never under '(défaut)'."""

    def _worker(self, items):
        worker = mock.Mock()
        worker.name = "legacy"
        worker.agent = {"harness": "claude"}
        worker.cfg.state_dir = self.tmp
        worker.cfg.host = "h"
        worker._account_override = None
        worker._account = items[1]
        worker._compte_session = None
        worker.current_session.return_value = "sess-1"
        worker.peek.return_value = True
        return worker

    def _continuity(self, items, failing=None, gauges=None):
        worker = self._worker(items)
        quiet = self._gauges(0.10, 0.10, 1_800_000_000.0)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}), \
                mock.patch.object(accounts, "profiles", return_value=items), \
                mock.patch.object(auth_failure, "failing", return_value=failing or {}), \
                mock.patch.object(accounts, "gauges_of", side_effect=lambda _b, p, _i: (
                    gauges or {}).get(p.name, quiet)), \
                mock.patch.object(registry, "clear_session", return_value=False):
            account_turn.continuity(worker)
        return worker

    def test_legacy_session_resolves_to_the_declared_primary(self):
        items = self._items()
        self.assertEqual(account_turn.legacy_profile("claude", items), items[0])
        worker = self._worker(items)
        with mock.patch.object(accounts, "profiles", return_value=items):
            self.assertEqual(account_turn.session_account(worker, items), "primaire")
        # nothing declared on the default folder: the synthetic profile
        other = items[1:]
        self.assertEqual(account_turn.legacy_profile("claude", other).name, "(default)")

    def test_no_summary_on_a_failing_primary(self):
        items = self._items()
        worker = self._continuity(items, failing={"primaire": {"reason": "http 401"}})
        worker.run_turn.assert_not_called()

    def test_no_summary_on_a_primary_in_overage(self):
        items = self._items()
        now = time.time()
        overage = self._gauges(0.10, 0.10, now, cost.Overage(capable=True, in_use=True))
        worker = self._continuity(items, gauges={"primaire": overage})
        worker.run_turn.assert_not_called()

    def test_summary_on_a_healthy_primary(self):
        worker = self._continuity(self._items())
        worker.run_turn.assert_called_once()


class UnpacedChoiceTest(_AuthBase):
    """Codex review r3 (P1): with the budget guard off the pace no longer
    holds, but an account in overage or past a fixed cap is never chosen."""

    def setUp(self) -> None:
        super().setUp()
        self.now = time.time()
        self.overage = cost.Gauges([cost.Gauge(
            "claude", "five_hour", 0.10, self.now + HOUR, 5 * HOUR)],
            cost.Overage(capable=True, in_use=True))
        # past its pace only (95 % at a third of the window)
        self.paced = cost.Gauges([cost.Gauge(
            "claude", "five_hour", 0.95, self.now + 3.5 * HOUR, 5 * HOUR)])

    def _choose(self, items, gauges, **kwargs):
        return accounts.choose(self.db, self.cfg.host, "claude", items, _Book(gauges),
                               now=self.now, **kwargs)

    def test_pace_lifted_absolute_rules_kept(self):
        items = accounts.parse({"claude": self._claude_comptes()})["claude"]
        gauges = {"primaire": self.overage, "secondaire": self.paced}
        self.assertTrue(self._ev_hard(items[0], items, gauges))
        self.assertFalse(self._ev_hard(items[1], items, gauges))
        self.assertIsNone(self._choose(items, gauges).profile)
        choix = self._choose(items, gauges, paced=False)
        self.assertEqual(choix.profile.name, "secondaire")
        # both held by an absolute rule: no account, whatever the guard
        gauges["secondaire"] = self.overage
        choix = self._choose(items, gauges, paced=False)
        self.assertIsNone(choix.profile)
        self.assertIn("in overage", choix.reason)

    def test_forced_account_in_overage_is_not_launched(self):
        items = accounts.parse({"claude": self._claude_comptes()})["claude"]
        storage.of(self.db).accounts.init_active(self.cfg.host, "claude", "primaire")
        self.db.execute("UPDATE account_active SET forced = TRUE")
        # no other account free: the force holds, the account in overage is not launched
        gauges = {"primaire": self.overage, "secondaire": self.overage}
        self.assertIsNone(self._choose(items, gauges, paced=False).profile)
        gauges["primaire"] = self.paced
        self.assertEqual(self._choose(items, gauges, paced=False).profile.name, "primaire")
        # another account free (bureau, 2026-10-12): the force is lifted, never
        # a turn on the account in overage
        gauges = {"primaire": self.overage, "secondaire": self.paced}
        choix = self._choose(items, gauges, paced=False)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertFalse(storage.of(self.db).accounts.active(self.cfg.host, "claude")["forced"])

    def test_fixed_cap_is_hard_even_below_a_lower_pace_cap(self):
        items = accounts.parse({"claude": [{"name": "primaire"}]})["claude"]
        # 60 % at a tenth of the window: the pace cap is lower than 50 %, but
        # the primary cap is what holds it
        reading = cost.Gauges([cost.Gauge(
            "claude", "five_hour", 0.60, self.now + 4.5 * HOUR, 5 * HOUR)])
        result = accounts.evaluate(_Book({"primaire": reading}), items[0], items, self.now)
        self.assertTrue(result.hard)
        self.assertIn("primary cap 50", result.reason)

    def test_guard_off_no_turn_and_no_rotation_on_an_account_in_overage(self):
        comptes = self._claude_comptes()
        runner, worker = self._worker("ovr", "claude", {"claude": comptes})
        runner.budget_usd_per_hour = 0.0
        try:
            worker.rotation_forcee = "test"
            with mock.patch.object(accounts, "gauges_of",
                                   side_effect=lambda _b, _p, _i: self.overage), \
                    mock.patch.object(worker, "current_session", return_value="sess-1"), \
                    mock.patch.object(worker, "run_turn", return_value=False) as tour:
                self.assertFalse(worker.budget_ok())
                self.assertFalse(worker.maybe_rotate())
                tour.assert_not_called()
                ligne = registry.get(self.db, "ovr")
                self.assertEqual(ligne["status"], "blocked")
                self.assertIn("in overage", ligne["status_text"])
            # out of overage: the guard lets it through, the pause is lifted
            worker.agent = registry.get(self.db, "ovr")  # as `pick` reloads it
            with mock.patch.object(accounts, "gauges_of",
                                   side_effect=lambda _b, _p, _i: self.paced):
                self.assertTrue(worker.budget_ok())
            self.assertEqual(registry.get(self.db, "ovr")["status"], "idle")
        finally:
            worker.watchdog_stop.set()

    def _ev_hard(self, profile, items, gauges):
        return accounts.evaluate(_Book(gauges), profile, items, self.now).hard


if __name__ == "__main__":
    unittest.main()
