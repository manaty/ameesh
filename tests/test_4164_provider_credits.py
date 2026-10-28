# SPDX-License-Identifier: AGPL-3.0-only
"""#4164 — provider account credits: exhaustion, gate, one alert, auto-resume.

Acceptance of manaty/ameesh#3181 (OpenAI IMA incident of 2026-10-28): a fake
provider returning `insufficient_quota` gives exactly one alert, no further
grant to that account's agents, their turns kept requested with the reason;
then a successful probe sets the account back to `ok`, the held turns are
granted, and the alert resolves once (the recovery notice).
"""
from __future__ import annotations

import json
import os
import re
import unittest
from unittest import mock

from ameesh import balance, budget, exploitation, provider_credits, registry, storage

from .support import FAKEBIN, REPO
from .test_l30_accounts import _Base

#: the OpenAI error of the incident, as the Codex CLI reports a failed turn
OPENAI_EXHAUSTED = ("429 insufficient_quota: You exceeded your current quota, please "
                    "check your plan and billing details. credit_balance_exhausted")


class ClassifyTest(unittest.TestCase):

    def test_credit_exhaustion_per_provider(self):
        for harness, texte in (
                ("codex", OPENAI_EXHAUSTED),
                ("codex", '{"code": "insufficient_quota"}'),
                ("codex", "You have no credits remaining"),
                ("codex", "credit_balance_exhausted"),
                ("claude", "Your credit balance is too low to access the Anthropic API"),
                ("deepseek", "dsh: QUOTA: 402 Insufficient Balance"),
                ("deepseek", "HTTP 402: Insufficient Balance")):
            with self.subTest(harness=harness, texte=texte):
                self.assertTrue(provider_credits.classify(harness, texte))
                self.assertTrue(provider_credits.classify(harness, None, texte))

    def test_plan_caps_capacity_and_rate_limits_are_not_credits(self):
        for harness, texte in (
                ("claude", "Claude AI usage limit reached"),
                ("codex", "You've hit your usage limit. Try again in 3 days."),
                ("codex", "weekly limit reached"),
                ("codex", "The model is at capacity, please try again later"),
                ("codex", "429 Too Many Requests: rate_limit_exceeded"),
                ("deepseek", "dsh: RATE_LIMIT: slow down")):
            with self.subTest(harness=harness, texte=texte):
                self.assertIsNone(provider_credits.classify(harness, texte))

    def test_openai_signatures_belong_to_openai(self):
        # `insufficient_quota` is OpenAI's credit error, not another provider's
        self.assertIsNone(provider_credits.classify("deepseek", "insufficient_quota"))

    def test_the_model_s_own_words_are_never_read(self):
        # only provider texts (error, stderr) classify: the result is not an argument
        self.assertIsNone(provider_credits.classify("codex", "", ""))


class _CreditsBase(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM provider_account_credits")

    def _row(self, harness="codex", account=""):
        return storage.of(self.db).provider_credits.get(harness, account)

    def _probe_due(self, harness="codex", account=""):
        self.db.execute("update provider_account_credits set next_probe_at = now()"
                        " - interval '1 second' where harness = %s and account = %s",
                        (harness, account))

    def _credit_alerts(self):
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "provider_credits_exhausted"]


class StorageTest(_CreditsBase):

    def test_transitions_are_reported_once(self):
        store = storage.of(self.db).provider_credits
        kw = dict(reason="insufficient_quota", detail="x", source="turn", agent="a",
                  host="h", probe_after_s=600)
        self.assertTrue(store.mark_exhausted("codex", "", **kw))
        since = self._row()["since_ts"]
        self.assertFalse(store.mark_exhausted("codex", "", **kw))
        self.assertEqual(self._row()["since_ts"], since)   # alert key unchanged
        self.assertFalse(store.claim_probe("codex", "", 600))   # not due yet
        self._probe_due()
        self.assertTrue(store.claim_probe("codex", "", 600))
        self.assertFalse(store.claim_probe("codex", "", 600))   # one caller per due date
        self.assertTrue(store.mark_ok("codex", "", source="probe"))
        self.assertFalse(store.mark_ok("codex", "", source="probe"))
        row = self._row()
        self.assertEqual((row["state"], row["next_probe_ts"], row["reason"]),
                         ("ok", None, None))
        self.assertFalse(store.claim_probe("codex", "", 600))   # ok: nothing to probe
        self.assertFalse(store.mark_ok("deepseek", "", source="balance"))  # unseen

    def test_listing_reads_only_what_the_supervisor_is_granted(self):
        # the alerts read `listing()` under the supervisor role: a column it is
        # not granted (`detail`, free provider text) would empty the alerts
        from ameesh.storage.postgres import provider_credits as pg
        with open(os.path.join(REPO, "deploy", "sql", "role-superviseur.sql"),
                  encoding="utf-8") as fh:
            grants = re.search(r"\('provider_account_credits', ARRAY\[(.*?)\]",
                               fh.read(), re.S)
        granted = set(re.findall(r"'([a-z_]+)'", grants.group(1)))
        columns = {row["column_name"] for row in self.db.query(
            "select column_name from information_schema.columns"
            " where table_name = 'provider_account_credits'"
            " and table_schema = current_schema()")}
        self.assertEqual(columns - granted, {"detail"})
        read = set(re.findall(r"[a-z_]+", pg._LISTING)) & columns
        self.assertEqual(read - granted, set())
        provider_credits.mark_exhausted(self.db, "codex", "", reason="r", detail="d")
        self.assertNotIn("detail", storage.of(self.db).provider_credits.listing()[0])
        self.assertEqual(self._row()["detail"], "d")

    def test_balance_reading_sets_and_clears_exhaustion(self):
        lu = lambda total, available: [balance.BalanceReading(  # noqa: E731
            "deepseek", "USD", total, available=available)]
        self.assertIsNone(balance.credit_state(self.db, "deepseek", "", lu(3.0, True)))
        self.assertIsNone(self._row("deepseek"))
        self.assertTrue(balance.credit_state(self.db, "deepseek", "", lu(0.4, False)))
        self.assertEqual(self._row("deepseek")["state"], "exhausted")
        self.assertIsNone(balance.credit_state(self.db, "deepseek", "", lu(0.0, None)))
        self.assertFalse(balance.credit_state(self.db, "deepseek", "", lu(20.0, True)))
        self.assertEqual(self._row("deepseek")["state"], "ok")


class AcceptanceTest(_CreditsBase):
    """manaty/ameesh#3181, with the fake Codex harness as the OpenAI provider."""

    def _turn(self, worker, prompt=None, **env_extra):
        if prompt is not None:
            registry.set_pending_prompt(self.db, worker.name, prompt)
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "PATH": self._python_du_banc() + os.pathsep + os.environ.get("PATH", "")}
        env.update(env_extra)
        with mock.patch.dict(os.environ, env, clear=False):
            if "AMEESH_TEST_TURN_FAILED" not in env_extra:
                os.environ.pop("AMEESH_TEST_TURN_FAILED", None)
            spec = worker.pick()
            if spec is not None:
                worker.run_turn(spec)
            return spec

    def test_exhaustion_holds_alerts_once_then_probe_resumes(self):
        _r1, a1 = self._worker("ima-verif", "codex", {})
        _r2, a2 = self._worker("ima-docs", "codex", {})
        try:
            # 1. the provider says the credits are gone
            self.assertIsNotNone(self._turn(a1, "verifier",
                                            AMEESH_TEST_TURN_FAILED=OPENAI_EXHAUSTED))
            self.assertFalse(a1.fast_failure)          # not a fast failure (L48)
            row = self._row()
            self.assertEqual((row["state"], row["source"], row["agent"]),
                             ("exhausted", "turn", "ima-verif"))
            ligne = registry.get(self.db, "ima-verif")
            self.assertEqual((ligne["status"], ligne["status_text"]),
                             ("blocked", "provider_credits_exhausted: codex/(default)"))
            self.assertTrue(ligne["last_error"].startswith("provider_credits_exhausted:"))
            self.assertEqual(ligne["pending_prompt"], "verifier")  # still requested
            tours = len(self.turns())

            # 2. no further grant to the account's agents, turns kept requested
            self.assertIsNone(self._turn(a2, "documents"))
            self.assertIsNone(self._turn(a1))
            self.assertIsNone(self._turn(a2))
            self.assertEqual(len(self.turns()), tours)
            for name, prompt in (("ima-verif", "verifier"), ("ima-docs", "documents")):
                ligne = registry.get(self.db, name)
                self.assertEqual((ligne["status"], ligne["status_text"],
                                  ligne["pending_prompt"]),
                                 ("blocked", "provider_credits_exhausted: codex/(default)",
                                  prompt))

            # 3. exactly one alert, to the owner, with a stable key
            alertes = self._credit_alerts()
            self.assertEqual(len(alertes), 1)
            alerte = alertes[0]
            self.assertEqual((alerte["harness"], alerte["account"], alerte["reason"],
                              alerte["held"], alerte["targets"]),
                             ("codex", "", "provider_credits_exhausted",
                              ["ima-docs", "ima-verif"], ["owner"]))
            cle = exploitation.alert_key(alerte)

            # 4. a failed probe keeps the episode: same alert, no new note
            self._probe_due()
            self.assertIsNotNone(self._turn(a2, AMEESH_TEST_TURN_FAILED=OPENAI_EXHAUSTED))
            self.assertEqual(len(self.turns()), tours + 1)   # one bounded probe
            self.assertEqual(self._row()["source"], "probe")
            self.assertEqual([exploitation.alert_key(a) for a in self._credit_alerts()],
                             [cle])
            self.assertIsNone(self._turn(a1))                # probe not due again
            self.assertEqual(len(self.turns()), tours + 1)

            # 5. the account is topped up: the next probe succeeds
            self._probe_due()
            self.assertIsNotNone(self._turn(a1))
            self.assertEqual(self._row()["state"], "ok")
            self.assertEqual(self._credit_alerts(), [])     # resolution = recovery notice
            self.assertEqual(registry.get(self.db, "ima-verif")["status"], "idle")
            # the other held turn is granted without operator action
            self.assertIsNotNone(self._turn(a2))
            ligne = registry.get(self.db, "ima-docs")
            self.assertEqual(ligne["status"], "idle")
            self.assertFalse(ligne["pending_prompt"])
            self.assertEqual(len(self.turns()), tours + 3)
            fil = self._fil()
            self.assertEqual(fil.count("Probe turn succeeded"), 1)
            self.assertIn("Block provider_credits_exhausted: codex/(default) lifted", fil)
        finally:
            a1.watchdog_stop.set()
            a2.watchdog_stop.set()

    def test_an_agent_without_work_never_takes_the_probe(self):
        # B1 cx2: the due probe is claimed only for a turn actually chosen
        _r1, oisif = self._worker("oisif", "codex", {})
        _r2, actif = self._worker("actif", "codex", {})
        try:
            provider_credits.mark_exhausted(self.db, "codex", "", reason="insufficient_quota")
            registry.set_pending_prompt(self.db, "actif", "travail")
            self._probe_due()
            echeance = self._row()["next_probe_ts"]
            self.assertIsNone(self._turn(oisif))         # nothing to do
            self.assertEqual(self.turns(), [])
            self.assertEqual(self._row()["next_probe_ts"], echeance)  # probe still free
            self.assertIsNotNone(self._turn(actif))      # the agent with work probes
            self.assertEqual(len(self.turns()), 1)
            self.assertEqual(self._row()["state"], "ok")
            ligne = registry.get(self.db, "actif")
            self.assertEqual((ligne["status"], ligne["pending_prompt"]), ("idle", None))
        finally:
            oisif.watchdog_stop.set()
            actif.watchdog_stop.set()

    def test_a_probe_lost_to_another_agent_keeps_the_instruction(self):
        _r, worker = self._worker("tard", "codex", {})
        try:
            provider_credits.mark_exhausted(self.db, "codex", "", reason="insufficient_quota")
            self._probe_due()
            # another runner claims the due probe between the gate and the choice
            vrai = provider_credits.claim_probe

            def course(db, harness, account, *a, **kw):
                vrai(db, harness, account)                 # the other runner wins
                return vrai(db, harness, account, *a, **kw)

            with mock.patch.object(provider_credits, "claim_probe", side_effect=course):
                self.assertIsNone(self._turn(worker, "travail"))
            self.assertEqual(self.turns(), [])
            ligne = registry.get(self.db, "tard")
            self.assertEqual((ligne["status"], ligne["status_text"], ligne["pending_prompt"]),
                             ("blocked", "provider_credits_exhausted: codex/(default)",
                              "travail"))
        finally:
            worker.watchdog_stop.set()

    def test_a_probe_lost_keeps_the_suite_and_runs_it_after_recovery(self):
        # B2 cx2: a resume consumed by `_pick` (suite after a cap, attach
        # resume) is given back when the probe is lost, and runs once `ok`
        _r, worker = self._worker("suite", "codex", {})
        try:
            provider_credits.mark_exhausted(self.db, "codex", "", reason="insufficient_quota")
            self._probe_due()
            worker.suite = "cap"
            worker._reprise_attach = True
            vrai = provider_credits.claim_probe

            def course(db, harness, account, *a, **kw):
                vrai(db, harness, account)                 # the other runner wins
                return vrai(db, harness, account, *a, **kw)

            with mock.patch.object(provider_credits, "claim_probe", side_effect=course):
                self.assertIsNone(self._turn(worker))
            self.assertEqual(self.turns(), [])
            self.assertEqual((worker.suite, worker._reprise_attach), ("cap", True))
            # topped up: the kept suite is the turn that leaves
            provider_credits.mark_ok(self.db, "codex", "", source="test")
            spec = self._turn(worker)
            self.assertEqual(spec["kind"], "suite")
            self.assertIsNone(worker.suite)
            self.assertEqual(len(self.turns()), 1)
        finally:
            worker.watchdog_stop.set()

    def test_no_rotation_summary_on_an_exhausted_account(self):
        # B mty-amh-docs-01: a rotation summary is a model call too — it waits
        # while the account is exhausted, without pushing the probe back
        _r, worker = self._worker("rot", "codex", {})
        try:
            provider_credits.mark_exhausted(self.db, "codex", "", reason="insufficient_quota")
            echeance = self._row()["next_probe_ts"]
            worker.rotation_forcee = "context cap"
            with mock.patch.object(worker, "current_session", return_value="sess-1"), \
                    mock.patch.object(worker, "run_turn", return_value=False) as tour:
                self.assertFalse(worker.maybe_rotate())
                self.assertFalse(worker.maybe_rotate())
                tour.assert_not_called()
                self.assertEqual(worker.rotation_forcee, "context cap")  # kept
                self.assertEqual(self._row()["next_probe_ts"], echeance)
                provider_credits.mark_ok(self.db, "codex", "", source="test")
                worker.maybe_rotate()
                tour.assert_called_once()
        finally:
            worker.watchdog_stop.set()

    def test_no_account_switch_summary_on_an_exhausted_old_account(self):
        from ameesh import account_turn
        _r, worker = self._worker("bascule", "codex", {})
        try:
            provider_credits.mark_exhausted(self.db, "codex", "", reason="insufficient_quota")
            with mock.patch.object(worker, "run_turn", return_value=True) as tour:
                account_turn.rotate(worker, "codex", None, None, "sess-1",
                                    new_label="claude/(default)", undeclared=True)
                tour.assert_not_called()
        finally:
            worker.watchdog_stop.set()

    def test_a_failure_outside_the_probe_does_not_push_the_probe_back(self):
        store = storage.of(self.db).provider_credits
        kw = dict(reason="insufficient_quota", detail="x", source="turn", agent="a",
                  host="h", probe_after_s=600)
        store.mark_exhausted("codex", "", **kw)
        self._probe_due()
        echeance = self._row()["next_probe_ts"]
        self.assertFalse(store.mark_exhausted("codex", "", **kw))
        self.assertEqual(self._row()["next_probe_ts"], echeance)
        self.assertTrue(store.claim_probe("codex", "", 600))   # still due

    def test_a_plan_usage_limit_is_not_an_exhaustion(self):
        _r, worker = self._worker("plan", "codex", {})
        try:
            self._turn(worker, "travail",
                       AMEESH_TEST_TURN_FAILED="You've hit your usage limit.")
            self.assertIsNone(self._row())
            self.assertFalse(provider_credits.is_held(
                registry.get(self.db, "plan")["status_text"]))
        finally:
            worker.watchdog_stop.set()

    def test_budget_and_show_display_the_state(self):
        _r, worker = self._worker("vu", "codex", {})
        try:
            vide = budget.format_status(budget.status(self.cfg, self.db))
            self.assertIn("provider accounts: no credit exhaustion seen", vide)
            provider_credits.mark_exhausted(self.db, "codex", "", reason="insufficient_quota")
            data = budget.status(self.cfg, self.db)
            self.assertEqual([(r["harness"], r["state"]) for r in data["provider_credits"]],
                             [("codex", "exhausted")])
            json.dumps(data)   # the JSON schema stays serialisable
            texte = budget.format_status(data)
            self.assertIn("provider accounts (real credit state", texte)
            self.assertRegex(texte, r"codex/\(default\)\s+exhausted since .*insufficient_quota")
            proc = self.mesh("show", "vu")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertRegex(proc.stdout, r"credits  : codex/\(default\) exhausted since")
            proc = self.mesh("show", "vu", "--json")
            self.assertEqual(json.loads(proc.stdout)["provider_credits"][0]["state"],
                             "exhausted")
        finally:
            worker.watchdog_stop.set()


if __name__ == "__main__":
    unittest.main()
