# SPDX-License-Identifier: AGPL-3.0-only
"""L226b — classification of authentication failures in failed turns."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from ameesh import accounts, auth_failure, cost, exploitation, registry, storage

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
    """`accounts.healthy`: never overage (n°6810)."""

    def test_plan_account_never_replaced_by_an_api_key(self):
        items = [accounts.Profile(harness="claude", name="primaire"),
                 accounts.Profile(harness="claude", name="secondaire", path="/x"),
                 accounts.Profile(harness="claude", name="cle", kind="api_key_env",
                                  env="ANTHROPIC_API_KEY", key_env="K")]
        self.assertEqual(accounts.healthy(items, {}), items)
        self.assertEqual([p.name for p in accounts.healthy(items, {"primaire": {}})],
                         ["secondaire"])
        self.assertEqual(accounts.healthy(items, {"primaire": {}, "secondaire": {}}), [])

    def test_api_keys_among_themselves(self):
        items = [accounts.Profile(harness="deepseek", name=n, kind="api_key_env",
                                  key_env="K") for n in ("a", "b")]
        self.assertEqual([p.name for p in accounts.healthy(items, {"a": {}})], ["b"])


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
    """L30 `choose` restricted to healthy accounts (decision n°6810)."""

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
        self.assertIn("primaire en échec d'authentification", choix.switched["reason"])
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
        self.assertIn("aucun compte codex sain", choix.why)

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
        self.assertIn("au seuil", choix.reason)


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
            self.assertIn("Tour en échec d'authentification sur claude/primaire", fil)
            self.assertIn("Blocage auth_blocked: claude/primaire levé", fil)
        finally:
            worker.watchdog_stop.set()

    def test_no_healthy_account_stays_blocked_alerts_and_resumes(self):
        runner, worker = self._worker("nu", "claude", {})
        try:
            self.assertIsNotNone(self._tour(worker, **self.LOGIN))
            self.assertEqual(len(self.turns()), 1)
            ligne = registry.get(self.db, "nu")
            self.assertEqual(ligne["status_text"], "auth_blocked: claude/(défaut)")
            self.assertTrue(ligne["last_error"].startswith("auth_blocked: invalid api key"))
            # no relaunch: nothing consumed, no turn
            self.assertIsNone(self._suite(worker))
            self.assertIsNone(self._suite(worker))
            self.assertEqual(len(self.turns()), 1)
            self.assertEqual(registry.get(self.db, "nu")["pending_prompt"], "travail")
            alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                       if a["type"] == "auth_blocked"]
            self.assertEqual([(a["agent"], a["harness"], a["account"]) for a in alertes],
                             [("nu", "claude", "(défaut)")])
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
                             ("blocked", "auth_blocked: claude/(défaut)"))
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


if __name__ == "__main__":
    unittest.main()
