# SPDX-License-Identifier: AGPL-3.0-only
"""#8062 — échéances d'identifiants muettes sur le mesh.

Trois pièces, aucune base ni hôte réel :

* `credential_probe_stale` : une alerte par (hôte, harnais) dont le dernier
  relevé de `credential_health` dépasse trois périodes de la sonde (30 min) ;
  la relecture d'une même paire ne refait pas un courrier ;
* le battement de la sonde : un échec maintenu (`kept_failure`) est relu à
  chaque passage et `checked_at` est touché, sinon la paire paraîtrait muette
  alors que la sonde tourne (`Credentials.touch`) ;
* l'installation et le contrôle : la mise à jour d'hôte pose et active
  `ameesh-credentials.timer` (unité utilisateur sur une VM ou le poste, unité
  système sur le serveur), et `ameesh doctor` la vérifie (KO si absente ou
  arrêtée) — le silence ne doit plus être vert.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ameesh import exploitation, hostcheck, notify, sentinel
from ameesh.storage.postgres.credentials import Credentials

from . import support  # noqa: F401 — isole l'environnement agent de l'hôte

NOW = 1_800_000_000.0
PERIOD = sentinel.PERIOD_S
LIMIT = PERIOD * sentinel.PROBE_SILENCE_PERIODS
ROOT = Path(__file__).resolve().parents[1]


class CredentialProbeAlertsTest(unittest.TestCase):
    def _alerts(self, rows, now=NOW):
        creds = mock.Mock()
        creds.listing.return_value = rows
        with mock.patch.object(exploitation.storage, "of",
                               return_value=SimpleNamespace(credentials=creds)):
            return exploitation._credential_probe_alerts(object(), now)

    def _row(self, **kw):
        row = {"host": "h1", "harness": "claude", "account": "a", "state": "healthy",
               "reason": "", "source": "sentinel", "checked_ts": NOW - 60,
               "failing_since_ts": None, "expires_ts": None}
        row.update(kw)
        return row

    def test_a_stale_pair_alerts_once_per_host_and_harness(self):
        stale = NOW - LIMIT - 5
        alerts = self._alerts([self._row(account="a", checked_ts=stale),
                               self._row(account="b", checked_ts=stale - 10)])
        [alert] = alerts
        self.assertEqual(alert["type"], "credential_probe_stale")
        self.assertEqual((alert["host"], alert["harness"]), ("h1", "claude"))
        self.assertEqual(alert["since"], stale)           # le plus récent du couple
        self.assertEqual(alert["threshold"], LIMIT)
        self.assertGreaterEqual(alert["value"], LIMIT)
        self.assertIn("ameesh-credentials.timer", alert["detail"])
        self.assertIn("deploy/mise-a-jour", alert["detail"])

    def test_one_fresh_account_proves_the_probe_runs(self):
        self.assertEqual(self._alerts([
            self._row(account="a", checked_ts=NOW - LIMIT - 5),
            self._row(account="b", checked_ts=NOW - 60),
        ]), [])

    def test_the_limit_is_three_probe_periods(self):
        [alert] = self._alerts([self._row(checked_ts=NOW - LIMIT)])
        self.assertEqual(alert["type"], "credential_probe_stale")
        self.assertEqual(self._alerts([self._row(checked_ts=NOW - LIMIT + 1)]), [])

    def test_relay_harnesses_are_not_periodic_readings(self):
        # `deepseek` n'écrit une ligne qu'à la récupération d'une clé : son
        # ancienneté ne prouve pas que la sonde s'est tue.
        self.assertEqual(self._alerts([self._row(harness="deepseek",
                                                 checked_ts=NOW - 30 * 86400)]), [])

    def test_a_row_without_a_reading_is_ignored(self):
        self.assertEqual(self._alerts([self._row(checked_ts=None)]), [])

    def test_alert_identity_stays_stable_while_the_silence_grows(self):
        row = self._row(checked_ts=NOW - LIMIT - 5)
        first = self._alerts([row])[0]
        later = self._alerts([row], now=NOW + 4 * PERIOD)[0]
        self.assertEqual(exploitation.alert_key(first), exploitation.alert_key(later))
        other = self._alerts([self._row(harness="codex", checked_ts=row["checked_ts"])])[0]
        self.assertNotEqual(exploitation.alert_key(first), exploitation.alert_key(other))

    def test_the_type_is_declared_pushed_and_labelled(self):
        self.assertIn("credential_probe_stale", exploitation.ALERT_TYPES)
        self.assertIn("credential_probe_stale", notify.DEFAULT_TYPES)
        self.assertIn("credential_probe_stale", notify.TYPE_LABELS)

    def test_an_unreadable_listing_does_not_hide_the_other_families(self):
        from ameesh import db as db_mod
        creds = mock.Mock()
        creds.listing.side_effect = db_mod.DbError("gone")
        with mock.patch.object(exploitation.storage, "of",
                               return_value=SimpleNamespace(credentials=creds)):
            self.assertEqual(exploitation._credential_probe_alerts(object(), NOW), [])
        creds.listing.side_effect = db_mod.Unavailable("down")
        with mock.patch.object(exploitation.storage, "of",
                               return_value=SimpleNamespace(credentials=creds)):
            with self.assertRaises(db_mod.Unavailable):
                exploitation._credential_probe_alerts(object(), NOW)


class KeptFailureHeartbeatTest(unittest.TestCase):
    def _check(self, verdict, *, dry_run=False):
        current = {"a": {"state": "failing", "source": "turn", "failures": 1,
                         "failing_since_ts": NOW - 600, "checked_ts": NOW - 600,
                         "last_failure_ts": NOW - 600,
                         "failing_fingerprint": sentinel._digest("x")}}
        creds = mock.Mock()
        creds.health.return_value = current
        cfg = SimpleNamespace(host="h1", accounts={}, credential_sentinel={})
        with mock.patch.object(sentinel, "probe_all", return_value=[verdict]), \
                mock.patch("ameesh.storage.of",
                           return_value=SimpleNamespace(credentials=creds)):
            rows = sentinel.check(cfg, object(), now=NOW, dry_run=dry_run)
        return rows, creds

    def test_a_kept_failure_is_touched_at_each_probe(self):
        # #8062 : la sonde a relu l'identifiant, même si le verdict ne change
        # pas ; `checked_at` est son battement, pas le début de l'échec.
        rows, creds = self._check(sentinel.Verdict("claude", "a", "unknown",
                                                   "probe failed: Timeout"))
        creds.report.assert_not_called()
        creds.touch.assert_called_once_with("h1", "claude", "a", expect_failures=1)
        self.assertEqual(rows[0]["state"], "failing")

    def test_a_dry_run_writes_no_heartbeat(self):
        _rows, creds = self._check(sentinel.Verdict("claude", "a", "unknown",
                                                    "probe failed: Timeout"),
                                   dry_run=True)
        creds.touch.assert_not_called()
        creds.report.assert_not_called()


class TouchStorageTest(unittest.TestCase):
    def test_touch_stamps_checked_at_only_and_respects_the_failure_count(self):
        db = mock.Mock()
        db.query.return_value = [{"account": "a"}]
        self.assertTrue(Credentials(db).touch("h1", "claude", "a", expect_failures=2))
        sql, params = db.query.call_args[0]
        self.assertEqual(sql.split(" where ", 1)[0],
                         "update credential_health set checked_at = now()")
        self.assertIn("state = 'failing'", sql)
        self.assertIn("failures = %s::int", sql)
        self.assertEqual(params, ("h1", "claude", "a", 2, 2))

    def test_touch_without_a_guard_has_no_condition(self):
        db = mock.Mock()
        db.query.return_value = [{"account": "a"}]
        self.assertTrue(Credentials(db).touch("h1", "claude", "a"))
        self.assertEqual(db.query.call_args[0][1], ("h1", "claude", "a", None, None))


def _systemctl(*, user=None, system=None):
    """Faux `systemctl` : None = gestionnaire illisible, dict = unité lue."""
    def run(*args):
        if args[:1] == (hostcheck.SYSTEM,):
            scope, rest = "system", args[1:]
        else:
            scope, rest = "user", args
        conf = {"user": user, "system": system}[scope]
        if conf is None:
            return None
        if "show" in rest:
            if "LoadState" in rest:
                return conf.get("load", "loaded") + "\n"
            if "ActiveState" in rest:
                return conf.get("active", "active") + "\n"
            return ""
        if "is-enabled" in rest:
            return conf.get("enabled", "enabled") + "\n"
        return ""
    return run


class DoctorCheckTest(unittest.TestCase):
    def test_a_user_unit_active_on_a_vm_or_the_poste_is_green(self):
        ko, lines = hostcheck.credentials_timer_lines(
            run=_systemctl(user={"load": "loaded", "active": "active",
                                 "enabled": "enabled"}))
        self.assertFalse(ko)
        self.assertEqual(lines, ["ameesh-credentials.timer active (user unit)"])

    def test_a_system_unit_active_on_the_server_is_green(self):
        # `runuser -l ameesh` sur un serveur n'a pas de gestionnaire
        # utilisateur : l'unité système doit suffire, sans faux KO.
        ko, lines = hostcheck.credentials_timer_lines(
            run=_systemctl(user=None,
                           system={"load": "loaded", "active": "active",
                                   "enabled": "enabled"}))
        self.assertFalse(ko)
        self.assertEqual(lines, ["ameesh-credentials.timer active (system unit)"])

    def test_an_absent_timer_is_a_ko_and_names_the_host_update(self):
        ko, lines = hostcheck.credentials_timer_lines(
            run=_systemctl(user={"load": "not-found"}, system={"load": "not-found"}))
        self.assertTrue(ko)
        self.assertIn("not installed", lines[0])
        self.assertIn("vm.sh installer", lines[1])
        self.assertIn("poste.sh installer", lines[1])
        self.assertIn("never copy a unit by hand", lines[1])

    def test_a_present_but_stopped_user_timer_is_a_ko(self):
        ko, lines = hostcheck.credentials_timer_lines(
            run=_systemctl(user={"load": "loaded", "active": "inactive",
                                 "enabled": "disabled"}))
        self.assertTrue(ko)
        self.assertIn("present but not running", lines[0])
        self.assertEqual(lines[1], "             systemctl --user enable --now "
                                   "ameesh-credentials.timer")

    def test_a_stopped_system_timer_uses_the_system_scope_in_the_fix(self):
        ko, lines = hostcheck.credentials_timer_lines(
            run=_systemctl(user=None,
                           system={"load": "loaded", "active": "inactive",
                                   "enabled": "disabled"}))
        self.assertTrue(ko)
        self.assertEqual(lines[1], "             systemctl enable --now "
                                   "ameesh-credentials.timer")

    def test_without_any_systemd_reading_the_check_does_not_conclude(self):
        ko, lines = hostcheck.credentials_timer_lines(run=_systemctl(user=None,
                                                                     system=None))
        self.assertFalse(ko)
        self.assertIn("not checked: systemd unavailable", lines[0])


class HostUpdateWiringTest(unittest.TestCase):
    """La mise à jour d'hôte est le seul chemin qui fait durer la minuterie."""

    def test_the_agents_vm_installer_installs_and_enables_it(self):
        text = (ROOT / "deploy/mise-a-jour/vm.sh").read_text(encoding="utf-8")
        self.assertIn("installer_sentinelle_agents() {", text)
        self.assertIn('installer_sentinelle_agents "$court"', text)
        self.assertIn("deploy/systemd/ameesh-credentials.service", text)
        self.assertIn("systemctl --user enable --now ameesh-credentials.timer", text)

    def test_the_server_installer_installs_the_system_unit(self):
        for name in ("deploy/mise-a-jour/vm.sh",
                     "deploy/serveur/installer-serveur.sh"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("deploy/serveur/ameesh-credentials.service", text, name)
            self.assertIn("systemctl enable --now ameesh-credentials.timer", text,
                          name)

    def test_the_poste_installer_installs_the_user_unit(self):
        text = (ROOT / "deploy/mise-a-jour/poste.sh").read_text(encoding="utf-8")
        self.assertIn('"$cible/deploy/systemd/ameesh-credentials.service"', text)
        self.assertIn("systemctl --user enable --now ameesh-credentials.timer", text)

    def test_the_units_carry_the_two_layouts(self):
        user = (ROOT / "deploy/systemd/ameesh-credentials.service").read_text(
            encoding="utf-8")
        server = (ROOT / "deploy/serveur/ameesh-credentials.service").read_text(
            encoding="utf-8")
        timer = (ROOT / "deploy/systemd/ameesh-credentials.timer").read_text(
            encoding="utf-8")
        self.assertIn("ExecStart=%h/.local/share/ameesh/venv/bin/ameesh "
                      "accounts check", user)
        self.assertIn("User=ameesh", server)
        self.assertIn("ExecStart=/opt/ameesh/venv/bin/ameesh accounts check", server)
        self.assertIn("OnUnitActiveSec=10min", timer)
        self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()
