# SPDX-License-Identifier: AGPL-3.0-only
"""#8180 — rotation vers un compte dont le login est expiré.

Incident avatar-smichea (23/11) : la rotation a placé l'avatar sur
`secondaire`, dont le login Claude était expiré. La sonde n'avait pas encore
écrit `failing` ; `accounts.choose` ne lisait que l'état stocké, `accounts
list` disait `ok` (ses jauges étaient sous le seuil) et le message
« this login reached its time limit » n'était pas classé : cinq échecs rapides
ont arrêté l'agent, muet 5 h 40.

Les quatre attendus : le choix écarte un login expiré, la liste le dit
inutilisable, l'échec d'authentification bloque et signale (compte + commande
de reconnexion) puis reprend seul, et l'owner reçoit l'alerte d'échéance même
hors `notify.types`.
"""
from __future__ import annotations

import dataclasses
import os
import time
import unittest
from unittest import mock

from ameesh import accounts, auth_failure, cost, exploitation, notify, registry, storage

from .support import FAKEBIN
from .test_l226b_auth_failure import _AuthBase


EXPIRED_MESSAGE = (
    "Failed to authenticate: this login reached its time limit and cannot be "
    "renewed. Run `claude auth login`")


class ClassifyExpiredLoginTest(unittest.TestCase):
    def test_the_incident_message_is_an_auth_failure(self):
        for field in ("error", "result", "stderr_tail"):
            self.assertEqual(auth_failure.classify(**{field: EXPIRED_MESSAGE}),
                             "login expired", field)

    def test_a_long_answer_quoting_it_is_not_a_failure(self):
        answer = ("The provider said: " + EXPIRED_MESSAGE + " ") * 20
        self.assertIsNone(auth_failure.classify(error="success", result=answer))

    def test_a_third_party_auth_failure_is_not_the_login(self):
        # Verdict B de l'auditeur (24/11) : « failed to authenticate » seul
        # est générique — un serveur MCP ou un outil tiers le dit aussi, et
        # kept_failure condamnerait alors un compte sain. Le motif est ancré
        # sur la forme du fournisseur.
        for message in ("MCP server x: failed to authenticate",
                        "gh: failed to authenticate"):
            for field in ("error", "result", "stderr_tail"):
                self.assertIsNone(auth_failure.classify(**{field: message}),
                                  (message, field))

    def test_the_provider_401_form_is_the_login(self):
        message = 'Failed to authenticate. API Error: 401 {"type": "error"}'
        for field in ("error", "result", "stderr_tail"):
            self.assertIn(auth_failure.classify(**{field: message}),
                          ("http 401", "login expired"), field)


class _Health(_AuthBase):
    """A codex harness with two accounts, as the choice sees it."""

    def _items(self):
        return accounts.parse({"codex": self._codex_comptes()})["codex"]

    def _book(self):
        return cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def _login(self, account, expires_at, reason="refresh token present", checked_age=0.0):
        store = storage.of(self.db).credentials
        store.report(self.cfg.host, "codex", account, "healthy", reason=reason,
                     source="sentinel", expires_at=expires_at)
        if checked_age:
            self.db.execute(
                "UPDATE credential_health SET checked_at = now() - make_interval(secs => %s)"
                " WHERE host = %s AND harness = 'codex' AND account = %s",
                (float(checked_age), self.cfg.host, account))


class ExpiredLoginHealthTest(_Health):
    def test_a_passed_expiry_on_a_fresh_reading_is_unusable(self):
        now = time.time()
        self._login("secondaire", now - 60,
                    reason="logged in (claude.ai); fix: ameesh accounts login codex secondaire")
        sick = auth_failure.failing(self.db, self.cfg.host, "codex")
        self.assertIn("secondaire", sick)
        self.assertEqual(sick["secondaire"]["state"], "failing")
        self.assertIn("forecast expiry passed", sick["secondaire"]["reason"])
        self.assertIn("not extended by use", sick["secondaire"]["reason"])
        self.assertIn("ameesh accounts login codex secondaire",
                      sick["secondaire"]["reason"])

    def test_a_stale_reading_is_not_evidence(self):
        # #8042 : une ligne saine que la sonde ne relit plus ne doit pas
        # condamner un compte (le credential a pu être remplacé depuis).
        self._login("secondaire", time.time() - 60, checked_age=3 * 3600)
        self.assertEqual(auth_failure.failing(self.db, self.cfg.host, "codex"), {})

    def test_an_expiry_still_in_the_future_stays_usable(self):
        self._login("secondaire", time.time() + 3600)
        self.assertEqual(auth_failure.failing(self.db, self.cfg.host, "codex"), {})

    def test_expiring_reports_only_the_accounts_dying_before_the_horizon(self):
        now = time.time()
        self._login("primaire", now + 120)
        self._login("secondaire", now + 7200)
        self.assertEqual(auth_failure.expiring(self.db, self.cfg.host, "codex",
                                                now=now, horizon=now + 1800),
                         {"primaire"})


class ChoiceExpiredLoginTest(_Health):
    def test_an_expired_login_is_never_chosen_even_by_continuity(self):
        now = time.time()
        self._login("secondaire", now - 60)
        choice = accounts.choose(self.db, self.cfg.host, "codex", self._items(),
                                 self._book(), now=now, session_account="secondaire")
        self.assertEqual(choice.profile.name, "primaire")
        self.assertFalse(choice.auth_blocked)
        self.assertIn("forecast expiry passed", choice.why)
        self.assertIn("secondaire", choice.why)

    def test_every_expired_login_is_auth_blocked_not_a_budget_pause(self):
        now = time.time()
        self._login("primaire", now - 60)
        self._login("secondaire", now - 30)
        choice = accounts.choose(self.db, self.cfg.host, "codex", self._items(),
                                 self._book(), now=now)
        self.assertTrue(choice.auth_blocked)
        self.assertIn("no healthy codex account", choice.why)
        self.assertIn("forecast expiry passed", choice.why)

    def test_a_login_dying_before_the_turn_cap_is_ranked_last(self):
        now = time.time()
        self._login("primaire", now + 60)      # expires during the next turn
        self._login("secondaire", now + 7200)
        choice = accounts.choose(self.db, self.cfg.host, "codex", self._items(),
                                 self._book(), now=now, cfg=self.cfg)
        self.assertEqual(choice.profile.name, "secondaire")
        # with no account that outlives the turn, the demoted one is still
        # used (a preference, never a refusal).
        self._login("secondaire", now + 120)
        choice = accounts.choose(self.db, self.cfg.host, "codex", self._items(),
                                 self._book(), now=now, cfg=self.cfg)
        self.assertEqual(choice.profile.name, "primaire")


class AccountsListTest(_Health):
    def test_an_expired_login_is_listed_unusable_not_ok(self):
        now = time.time()
        self._login("secondaire", now - 60,
                    reason="logged in (claude.ai); fix: ameesh accounts login codex secondaire")
        cfg = dataclasses.replace(self.cfg, accounts={"codex": self._codex_comptes()})
        rows = accounts.report(cfg, self.db, self._book(), now=now, record=False)
        by = {row["account"]: row for row in rows}
        self.assertEqual(by["secondaire"]["credential_state"], "expired")
        self.assertFalse(by["secondaire"]["credential_ok"])
        self.assertFalse(by["secondaire"]["next"])
        self.assertEqual(by["primaire"]["credential_state"], "")
        self.assertTrue(by["primaire"]["next"])
        text = accounts.format_rows(rows)
        self.assertIn("expired", text)
        self.assertIn("login unusable (expired)", text)
        self.assertIn("ameesh accounts login codex secondaire", text)
        # the JSON row keeps the stored health, and the explicit flags
        self.assertEqual(by["secondaire"]["credential"]["state"], "healthy")
        self.assertFalse(by["secondaire"]["usable"])


class NotifyCredentialDeliveryTest(unittest.TestCase):
    def test_credential_alerts_ignore_notify_types(self):
        ncfg = notify.parse_config({"types": ["long_turn"]})
        notifier = notify.Notifier(type("Cfg", (), {"host": "h1"})(), ncfg, dry_run=True)
        for kind in ("auth_blocked", "credential_failing",
                     "credential_expiring", "credential_probe_stale"):
            self.assertTrue(notifier._deliverable({"type": kind}), kind)
        self.assertTrue(notifier._deliverable({"type": "long_turn"}))
        self.assertFalse(notifier._deliverable({"type": "idle_with_mail"}))
        self.assertTrue(notifier._deliverable({"type": "idle_with_mail",
                                               "targets": ["owner"]}))


class ExpiringAlertMessageTest(unittest.TestCase):
    def test_the_alert_says_the_expiry_is_absolute(self):
        now = time.time()
        row = {"host": "h1", "harness": "claude", "account": "secondaire",
               "state": "healthy", "reason": "logged in (claude.ai); fix: "
               "ameesh accounts login claude secondaire --token-file FILE",
               "source": "sentinel", "checked_ts": now - 60,
               "probed_ts": now - 60, "probe_missing_ts": None,
               "failing_since_ts": None, "expires_ts": now + 3600,
               "last_failure_ts": None, "failures": 0}
        creds = type("Creds", (), {
            "listing": staticmethod(lambda: [row]),
            "probe_transition": staticmethod(lambda: None)})
        store = type("Store", (), {"credentials": creds})
        with mock.patch.object(exploitation.storage, "of", return_value=store):
            [alert] = exploitation._credentials(object(), now)
        self.assertEqual(alert["type"], "credential_expiring")
        self.assertIn("forecast is absolute", alert["detail"])
        self.assertIn("ameesh accounts login claude secondaire", alert["detail"])


class RunnerExpiredLoginTest(_Health):
    LOGIN = {"AMEESH_TEST_RESULT": EXPIRED_MESSAGE, "AMEESH_TEST_EXIT": "1"}

    def _suite(self, worker):
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("AMEESH_TEST_RESULT", "AMEESH_TEST_EXIT"):
                os.environ.pop(name, None)
            spec = worker.pick()
            if spec is not None:
                worker.run_turn(spec)
            return spec

    def test_expired_login_blocks_names_the_fix_and_resumes(self):
        runner, worker = self._worker("expire", "claude", {})
        try:
            self.assertIsNotNone(self._tour(worker, **self.LOGIN))
            self.assertFalse(worker.fast_failure)
            line = registry.get(self.db, "expire")
            self.assertEqual((line["status"], line["status_text"]),
                             ("blocked", "auth_blocked: claude/(default)"))
            self.assertIn("login expired", line["last_error"])
            self.assertIn("ameesh accounts login claude --token-file FILE",
                          line["last_error"])
            alert = [a for a in exploitation.alerts(self.cfg, self.db)
                     if a["type"] == "auth_blocked"]
            self.assertEqual(len(alert), 1)
            self.assertIn("ameesh accounts login claude", alert[0]["detail"])
            # no relaunch on the dead credential
            self.assertIsNone(self._suite(worker))
            # a fresh healthy probe resumes the agent, without `ameesh resume`
            self._report("claude", "", "healthy")
            self.assertIsNotNone(self._suite(worker))
            self.assertEqual(registry.get(self.db, "expire")["status"], "idle")
        finally:
            worker.watchdog_stop.set()
