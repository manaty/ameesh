# SPDX-License-Identifier: AGPL-3.0-only
"""#8263 : un relevé Codex incomplet est un doute, pas un plafond.

Le 24/11 à 22:10Z, le relevé de secondaire est revenu incomplet (« short
window absent from bounded session sample ») : cx1 a été mis en pause
« jusqu'au 27/11 08:31Z » (la remise à zéro de primaire, à 95 %) et une
alerte urgente `critical_budget_pause` est partie, alors que secondaire se
relisait dans la minute à 85 % avec une fenêtre de 5 h à 0 %.

Attendu : la jauge est relue chez le fournisseur avant de tenir la pause ;
la date annoncée ne vient pas d'un autre compte ; l'alerte urgente attend la
confirmation des passages suivants. Aucun test ne lance le vrai `codex`.
"""
from __future__ import annotations

import os
from unittest import mock

from ameesh import accounts, budget, cost, exploitation, gauge_probe, registry
from ameesh.codex_gauges import AmbiguousCodexGauges, IncompleteCodexGauges

from .support import PgTestCase

WEEK = 7 * 86400.0
SHORT = 5 * 3600.0
NOW = 1_800_000_000.0


class _Book:
    def __init__(self, db=None):
        self.db = db
        self.recorded = []

    def record_gauges(self, gauges, account=None):
        self.recorded.append((list(gauges), account))
        return len(gauges)


def provider(short_used: float | None, week_used: float) -> cost.Gauges:
    gauges = []
    if short_used is not None:
        gauges.append(cost.Gauge("codex", "codex-300min", short_used, NOW + SHORT,
                                 SHORT, read_at=NOW, produced_at=NOW, source="t"))
    gauges.append(cost.Gauge("codex", "codex-10080min", week_used, NOW + 12 * 3600,
                             WEEK, read_at=NOW, produced_at=NOW, source="t"))
    return cost.Gauges(gauges, cost.Overage(False, False))


class EvaluateTest(PgTestCase):
    def setUp(self):
        super().setUp()
        gauge_probe._ATTEMPTS.clear()
        self.addCleanup(gauge_probe._ATTEMPTS.clear)
        home = os.path.join(self.tmp, "codex-secondaire")
        os.makedirs(home, exist_ok=True)
        os.chmod(home, 0o700)
        self.profile = accounts.Profile("codex", "secondaire", kind="config_dir",
                                        path=home, declared=True,
                                        check_credentials=False)
        self.book = _Book(self.db)

    def _evaluate(self, fresh, *, error=None, probe=True, record=True):
        error = error or IncompleteCodexGauges(["short window"])
        with mock.patch.object(accounts, "gauges_of", side_effect=error), \
                mock.patch.object(gauge_probe, "probe", return_value=fresh) as sonde:
            result = accounts.evaluate(self.book, self.profile, [self.profile], NOW,
                                       record=record, critical=True, probe=probe)
        return result, sonde

    def test_the_incident_reading_is_reread_and_the_account_placed(self):
        result, sonde = self._evaluate(provider(0.0, 0.85))
        self.assertTrue(result.ok, result.reason)
        self.assertFalse(result.doubt)
        sonde.assert_called_once()
        published, account = self.book.recorded[-1]
        self.assertEqual(account, "secondaire")
        self.assertEqual({g.key for g in published}, {"codex-300min", "codex-10080min"})

    def test_a_reread_without_the_missing_window_keeps_the_doubt(self):
        result, sonde = self._evaluate(provider(None, 0.10))
        sonde.assert_called_once()
        self.assertFalse(result.ok)
        self.assertTrue(result.doubt)
        self.assertIsNone(result.until)
        self.assertIn("incomplete Codex account reading", result.reason)

    def test_a_confirmed_cap_on_the_reread_still_holds(self):
        result, _sonde = self._evaluate(provider(1.0, 0.85))
        self.assertFalse(result.ok)
        self.assertFalse(result.doubt)
        self.assertEqual(result.held[0], "codex-300min")
        self.assertEqual(result.until, NOW + SHORT)

    def test_the_reread_shares_the_probe_retry_delay(self):
        self._evaluate(None)
        result, sonde = self._evaluate(provider(0.0, 0.85))
        sonde.assert_not_called()
        self.assertTrue(result.doubt)

    def test_a_display_never_probes(self):
        for kwargs in ({"probe": False}, {"record": False}):
            result, sonde = self._evaluate(provider(0.0, 0.85), **kwargs)
            sonde.assert_not_called()
            self.assertTrue(result.doubt)

    def test_a_contradiction_is_not_a_doubt_and_is_not_reread(self):
        error = AmbiguousCodexGauges(
            "ambiguous Codex account: conflicting live session identities or plans")
        result, sonde = self._evaluate(provider(0.0, 0.85), error=error)
        sonde.assert_not_called()
        self.assertFalse(result.doubt)
        self.assertTrue(result.hard_all)


class PauseLabelTest(PgTestCase):
    def test_no_date_borrowed_from_another_account(self):
        primaire = accounts.Evaluation(
            accounts.Profile("codex", "primaire"), reason="codex plan: 95 % used",
            until=NOW + 2 * 86400, until_kind="reset", held=("codex-10080min", 0.95))
        secondaire = accounts.Evaluation(
            accounts.Profile("codex", "secondaire"),
            reason=str(IncompleteCodexGauges(["short window"])), hard=True,
            hard_all=True, doubt=True)
        self.assertIn("until", accounts.pause_label({"primaire": primaire}))
        self.assertEqual(accounts.pause_label(
            {"primaire": primaire, "secondaire": secondaire}), "")


class AlertTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "cx1", harness="codex", host=self.cfg.host)

    def _alerts(self, raison, seconds):
        registry.set_status(self.db, "cx1", "blocked", status_text="%s%s%s" % (
            budget.PAUSE_PREFIX, budget.CRITICAL_MARK, raison))
        self.db.execute(
            "UPDATE agent_registry SET status_reason_since ="
            " now() - make_interval(secs => %s) WHERE name = %s", (seconds, "cx1"))
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "critical_budget_pause"]

    def test_a_doubtful_pause_alerts_once_confirmed(self):
        raison = ("all codex accounts at threshold — secondaire: %s"
                  % IncompleteCodexGauges(["short window"]))
        self.assertEqual(self._alerts(raison, 20), [])
        self.assertEqual(len(self._alerts(
            raison, exploitation.DOUBTFUL_PAUSE_CONFIRM_S + 30)), 1)

    def test_a_proven_cap_alerts_at_once(self):
        raison = ("pause (account primaire, codex-10080min 95 % reset) until 08:31; "
                  "all codex accounts at threshold")
        self.assertEqual(len(self._alerts(raison, 20)), 1)
