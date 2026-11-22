# SPDX-License-Identifier: AGPL-3.0-only
"""#8062 — échéances d'identifiants muettes sur le mesh.

Quatre pièces, sans hôte réel (le trigger 0604 est rejoué sur le banc) :

* `credential_probe_stale` : une alerte par (hôte, harnais) dont le dernier
  relevé de `credential_health` dépasse trois périodes de la sonde (30 min) ;
  la relecture d'une même paire ne refait pas un courrier ;
* le battement de la sonde : un échec maintenu (`kept_failure`) est relu à
  chaque passage et `checked_at` est touché, sinon la paire paraîtrait muette
  alors que la sonde tourne (`Credentials.touch`) ;
* le trigger 0604 : il date la sonde d'un écrivain N-1 depuis `checked_at` et
  préserve la date à travers les écritures `turn`, y compris celles qui
  ignorent la colonne ;
* l'installation et le contrôle : la mise à jour d'hôte pose et active
  `ameesh-credentials.timer` (unité utilisateur sur une VM ou le poste, unité
  système sur le serveur), et `ameesh doctor` la vérifie (KO si absente ou
  arrêtée) — le silence ne doit plus être vert.
"""
from __future__ import annotations

import datetime
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ameesh import exploitation, hostcheck, migrations, notify, sentinel
from ameesh.storage.postgres.credentials import Credentials

from . import support  # noqa: F401 — isole l'environnement agent de l'hôte
from .support import PgTestCase

NOW = 1_800_000_000.0
PERIOD = sentinel.PERIOD_S
LIMIT = PERIOD * sentinel.PROBE_SILENCE_PERIODS
ROOT = Path(__file__).resolve().parents[1]


class CredentialProbeAlertsTest(unittest.TestCase):
    def _alerts(self, rows, now=NOW, cfg=None, hosts=()):
        creds = mock.Mock()
        creds.listing.return_value = rows
        store = SimpleNamespace(
            credentials=creds,
            hosts=SimpleNamespace(current=mock.Mock(return_value=list(hosts))))
        with mock.patch.object(exploitation.storage, "of", return_value=store):
            return exploitation._credential_probe_alerts(object(), now, cfg)

    def _row(self, **kw):
        row = {"host": "h1", "harness": "claude", "account": "a", "state": "healthy",
               "reason": "", "source": "sentinel", "checked_ts": NOW - 60,
               "probed_ts": NOW - 60, "probe_missing_ts": None,
               "failing_since_ts": None, "expires_ts": None}
        row.update(kw)
        return row

    def test_a_stale_pair_alerts_once_per_host_and_harness(self):
        stale = NOW - LIMIT - 5
        alerts = self._alerts([self._row(account="a", probed_ts=stale,
                                         checked_ts=stale),
                               self._row(account="b", probed_ts=stale - 10,
                                         checked_ts=stale - 10)])
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
            self._row(account="a", probed_ts=NOW - LIMIT - 5),
            self._row(account="b", probed_ts=NOW - 60),
        ]), [])

    def test_the_limit_is_three_probe_periods(self):
        [alert] = self._alerts([self._row(probed_ts=NOW - LIMIT,
                                          checked_ts=NOW - LIMIT)])
        self.assertEqual(alert["type"], "credential_probe_stale")
        self.assertEqual(self._alerts([self._row(probed_ts=NOW - LIMIT + 1,
                                                 checked_ts=NOW - LIMIT + 1)]), [])

    def test_relay_harnesses_are_not_periodic_readings(self):
        # `deepseek` n'écrit une ligne qu'à la récupération d'une clé : son
        # ancienneté ne prouve pas que la sonde s'est tue.
        self.assertEqual(self._alerts([self._row(harness="deepseek",
                                                 probed_ts=NOW - 30 * 86400)]), [])

    def test_an_n1_sentinel_write_without_probed_at_is_a_heartbeat(self):
        # 0604 peut être appliquée avant le code qui écrit probed_at : la
        # sonde N-1 écrit checked_at et source=sentinel, c'est une lecture.
        self.assertEqual(self._alerts([self._row(probed_ts=None,
                                                 checked_ts=NOW - 60)]), [])

    def test_an_n1_sentinel_write_wins_over_an_older_probe_date(self):
        # Une lecture N-1 (`checked_at`) plus récente qu'un `probed_at`
        # antérieur est le vrai battement ; le maximum des deux compte.
        self.assertEqual(self._alerts([self._row(
            probed_ts=NOW - 2 * LIMIT, checked_ts=NOW - 60)]), [])

    def test_an_n1_sentinel_silence_still_alerts(self):
        # La compatibilité n'éteint pas la garde : sans écriture récente de
        # sonde, l'alerte reste.
        [alert] = self._alerts([self._row(probed_ts=None,
                                          checked_ts=NOW - LIMIT - 5)])
        self.assertEqual(alert["type"], "credential_probe_stale")
        self.assertEqual(alert["since"], NOW - LIMIT - 5)

    def test_turn_failures_do_not_refresh_the_probe_heartbeat(self):
        # #8042/#8062 B : `Credentials.report(source='turn')` écrit aussi
        # checked_at ; des échecs de tour répétés ne doivent pas masquer une
        # sonde arrêtée.
        stale = NOW - LIMIT - 5
        [alert] = self._alerts([self._row(source="turn", checked_ts=NOW - 60,
                                          probed_ts=stale)])
        self.assertEqual(alert["type"], "credential_probe_stale")
        self.assertEqual(alert["since"], stale)

    def test_a_fresh_probe_survives_a_turn_write_on_the_same_row(self):
        # #230300 : un seul compte, sondé il y a une minute, puis un échec
        # source=turn sur la même ligne : la date de sonde reste, aucune
        # alerte avant trois périodes.
        self.assertEqual(self._alerts([self._row(
            source="turn", probed_ts=NOW - 60, checked_ts=NOW - 5)]), [])

    def test_a_pair_with_only_turn_rows_is_not_named_immediately(self):
        # Une paire jamais sondée (ou dont la dernière sonde N-1 a été écrasée
        # avant 0604) reçoit une ancre à la première écriture ; elle n'est pas
        # nommée « jamais sondée » dans la foulée.
        self.assertEqual(self._alerts([self._row(
            source="turn", probed_ts=None, probe_missing_ts=NOW - 60,
            checked_ts=NOW - 5)]), [])

    def test_a_pair_with_only_turn_rows_is_named_once_the_anchor_is_old(self):
        row = self._row(source="turn", probed_ts=None,
                        probe_missing_ts=NOW - 60, checked_ts=NOW - 5)
        [alert] = self._alerts([row], now=NOW + LIMIT)
        self.assertEqual(alert["type"], "credential_probe_stale")
        self.assertEqual(alert["since"], NOW - 60)
        self.assertIn("no probe reading", alert["detail"])

    def test_repeated_turn_writes_do_not_move_the_missing_probe_anchor(self):
        # #230332 : une paire jamais sondée dont les turn se répètent ne doit
        # pas repousser indéfiniment l'alerte ; seule l'ancre compte.
        row = self._row(source="turn", probed_ts=None,
                        probe_missing_ts=NOW - 2 * 3600, checked_ts=NOW - 5)
        [alert] = self._alerts([row])
        self.assertEqual(alert["type"], "credential_probe_stale")
        self.assertEqual(alert["since"], NOW - 2 * 3600)
        self.assertIn("no probe reading", alert["detail"])

    def test_an_n1_probe_date_survives_a_later_turn_write(self):
        # #230332, le rejeu exact : sonde N-1 il y a 2 h (le trigger 0604 la
        # date depuis checked_at), puis un turn il y a 1 min. La date de sonde
        # reste celle d'il y a 2 h et l'alerte part sans attendre un silence.
        stale = NOW - 2 * 3600
        [alert] = self._alerts([self._row(source="turn", probed_ts=stale,
                                          checked_ts=NOW - 60)])
        self.assertEqual(alert["type"], "credential_probe_stale")
        self.assertEqual(alert["since"], stale)
        self.assertIn("last reading", alert["detail"])

    def test_a_pre_migration_turn_only_row_gets_a_grace(self):
        # Ligne existante sans date de sonde au moment de 0604 : l'ancre est
        # l'instant de la migration, pas le checked_at ancien du turn.
        row = self._row(source="turn", probed_ts=None,
                        probe_missing_ts=NOW - 5, checked_ts=NOW - 3 * 3600)
        self.assertEqual(self._alerts([row]), [])
        [alert] = self._alerts([row], now=NOW + LIMIT)
        self.assertEqual(alert["since"], NOW - 5)

    def test_alert_identity_stays_stable_while_the_silence_grows(self):
        row = self._row(probed_ts=NOW - LIMIT - 5, checked_ts=NOW - LIMIT - 5)
        first = self._alerts([row])[0]
        later = self._alerts([row], now=NOW + 4 * PERIOD)[0]
        self.assertEqual(exploitation.alert_key(first), exploitation.alert_key(later))
        other = self._alerts([self._row(harness="codex",
                                        probed_ts=row["probed_ts"],
                                        checked_ts=row["checked_ts"])])[0]
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


def _canon(**policy):
    card = SimpleNamespace(policy=SimpleNamespace(**policy))
    return SimpleNamespace(id="c", host=lambda _name: card)


class QuietHostsTest(unittest.TestCase):
    """#5207 × #8062 : un poste éteint (`intermittent`) ou un hôte retiré
    (`max_agents: 0`) ne fait pas pousser une alerte de sonde muette."""

    HOST = "pc-smichea"
    OFF = {"host": HOST, "sampled_ts": NOW - 4 * 86400}
    ON = {"host": HOST, "sampled_ts": NOW - 30}

    def _quiet(self, policy, readings):
        store = SimpleNamespace(hosts=SimpleNamespace(
            current=mock.Mock(return_value=list(readings))))
        with mock.patch("ameesh.canon.load_configured",
                        return_value=[_canon(**policy)]), \
                mock.patch.object(exploitation, "host_unsampled_after_s",
                                  return_value=3600), \
                mock.patch.object(exploitation.storage, "of", return_value=store):
            return exploitation._probe_quiet_hosts(
                SimpleNamespace(), object(), {self.HOST}, NOW)

    def test_an_intermittent_host_off_is_quiet(self):
        self.assertEqual(self._quiet({"intermittent": True, "max_agents": 27},
                                     [self.OFF]), {self.HOST})

    def test_a_purged_reading_changes_nothing(self):
        self.assertEqual(self._quiet({"intermittent": True, "max_agents": 27},
                                     []), {self.HOST})

    def test_its_probe_alerts_come_back_when_it_is_on(self):
        self.assertEqual(self._quiet({"intermittent": True, "max_agents": 27},
                                     [self.ON]), set())

    def test_a_retired_host_is_quiet_even_when_its_row_stays(self):
        self.assertEqual(self._quiet({"intermittent": False, "max_agents": 0},
                                     [self.OFF]), {self.HOST})

    def test_an_ordinary_host_still_alerts(self):
        self.assertEqual(self._quiet({"intermittent": False, "max_agents": 27},
                                     [self.OFF]), set())

    def test_the_server_publish_reading_is_not_a_retirement(self):
        # #5559/#5857 : `max_agents: 0` avec un relevé `publish@` = serveur
        # sans exécuteur, pas hôte retiré : sa sonde doit parler.
        reading = dict(self.OFF, runner_id="publish@%s" % self.HOST)
        self.assertEqual(self._quiet({"intermittent": False, "max_agents": 0},
                                     [reading]), set())

    def _alerts(self, rows, policy, readings):
        creds = mock.Mock()
        creds.listing.return_value = rows
        store = SimpleNamespace(
            credentials=creds,
            hosts=SimpleNamespace(current=mock.Mock(return_value=list(readings))))
        with mock.patch("ameesh.canon.load_configured",
                        return_value=[_canon(**policy)]), \
                mock.patch.object(exploitation, "host_unsampled_after_s",
                                  return_value=3600), \
                mock.patch.object(exploitation.storage, "of", return_value=store):
            return exploitation._credential_probe_alerts(
                SimpleNamespace(), NOW, SimpleNamespace())

    def test_the_alert_itself_is_silent_while_the_poste_is_off(self):
        row = {"host": self.HOST, "harness": "claude", "account": "a",
               "state": "healthy", "reason": "", "source": "sentinel",
               "checked_ts": NOW - LIMIT - 5, "failing_since_ts": None,
               "expires_ts": None}
        self.assertEqual(self._alerts([row], {"intermittent": True,
                                              "max_agents": 27}, [self.OFF]), [])
        [alert] = self._alerts([row], {"intermittent": True, "max_agents": 27},
                               [self.ON])
        self.assertEqual(alert["type"], "credential_probe_stale")

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
                         "update credential_health set checked_at = now(), "
                         "probed_at = now()")
        self.assertIn("state = 'failing'", sql)
        self.assertIn("failures = %s::int", sql)
        self.assertEqual(params, ("h1", "claude", "a", 2, 2))

    def test_touch_without_a_guard_has_no_condition(self):
        db = mock.Mock()
        db.query.return_value = [{"account": "a"}]
        self.assertTrue(Credentials(db).touch("h1", "claude", "a"))
        self.assertEqual(db.query.call_args[0][1], ("h1", "claude", "a", None, None))


class ReportProbeStampTest(unittest.TestCase):
    """#8062 : `probed_at` est la date de sonde, distincte de `checked_at`."""

    def _report(self, source, rows):
        db = mock.Mock()
        db.query.side_effect = rows
        out = Credentials(db).report("h1", "claude", "a", "healthy",
                                     source=source, expect_failures=0)
        return db, out

    def test_a_sentinel_report_stamps_the_probe_date(self):
        db, _ = self._report("sentinel", [[{"state": "healthy", "previous": None}]])
        sql, params = db.query.call_args[0]
        self.assertIn("case when %s = 'sentinel' then now() end", sql)
        self.assertIn("probed_at = case when excluded.source = 'sentinel'", sql)
        self.assertEqual(params[8:10], ("sentinel", "sentinel"))

    def test_a_turn_report_does_not_replace_the_probe_date(self):
        db, _ = self._report("turn", [[{"state": "healthy", "previous": None}]])
        _sql, params = db.query.call_args[0]
        self.assertEqual(params[8:10], ("turn", "turn"))

    def test_a_skipped_sentinel_report_still_marks_the_probe(self):
        db, out = self._report("sentinel", [[], [{"account": "a"}]])
        self.assertTrue(out["skipped"])
        sql, params = db.query.call_args[0]
        self.assertIn("set probed_at = now()", sql)
        self.assertEqual(params, ("h1", "claude", "a"))

    def test_a_skipped_turn_report_does_not_mark_the_probe(self):
        db, out = self._report("turn", [[]])
        self.assertTrue(out["skipped"])
        self.assertEqual(db.query.call_count, 1)


class ListingProbeColumnsTest(unittest.TestCase):
    def test_listing_reads_the_probe_and_missing_anchor_dates(self):
        db = mock.Mock()
        db.query.return_value = []
        Credentials(db).listing()
        sql = db.query.call_args[0][0]
        self.assertIn("probed_at)::float8 as probed_ts", sql)
        self.assertIn("probe_missing_since)::float8 as probe_missing_ts", sql)


class ProbeDateWiringTest(unittest.TestCase):
    def test_migration_0604_stamps_n1_probes_and_has_no_backfill(self):
        # #230332 : un trigger date la sonde N-1 depuis `checked_at` et
        # préserve la date à travers les turn ; `probe_missing_since` ancre
        # une paire jamais sondée, que les turn ne déplacent pas. Aucun
        # backfill : une date reprise serait crue après un turn.
        text = (ROOT / "src/ameesh/migrations/0604_credentials_probe_heartbeat.sql"
                ).read_text(encoding="utf-8")
        self.assertIn("add column if not exists probed_at", text)
        self.assertIn("add column if not exists probe_missing_since", text)
        self.assertIn("-- ameesh: trigger compatible with previous code", text)
        self.assertIn("create trigger credential_health_probe_heartbeat", text)
        self.assertIn("new.probe_missing_since := null", text)
        self.assertNotIn("update credential_health set probed_at", text)
        migration = next(m for m in migrations.discover() if m.version == 604)
        self.assertEqual(migrations.non_additive(migration), [])

    def test_the_supervisor_role_reads_the_probe_dates(self):
        text = (ROOT / "deploy/sql/role-superviseur.sql").read_text(encoding="utf-8")
        self.assertIn("'probed_at'", text)
        self.assertIn("'probe_missing_since'", text)


class ProbeHeartbeatTriggerTest(PgTestCase):
    """0604 sur le banc : le trigger date la sonde N-1 depuis `checked_at` et
    la préserve à travers les écritures `turn` (#230332)."""

    HOST = "h1"
    HARNESS = "claude"
    ACCOUNT = "a"
    ADDED = ("probed_at", "probe_missing_since")

    def setUp(self):
        super().setUp()
        self.db.execute("delete from credential_health")

    def _n1_insert(self, source, checked_at):
        self.previous_insert(
            "credential_health",
            {"host": self.HOST, "harness": self.HARNESS, "account": self.ACCOUNT,
             "state": "healthy", "reason": "", "source": source,
             "checked_at": checked_at},
            added=self.ADDED)

    def _n1_turn(self, checked_at):
        self.previous_update(
            "credential_health",
            {"state": "failing", "reason": "auth", "source": "turn",
             "checked_at": checked_at},
            where={"host": self.HOST, "harness": self.HARNESS,
                   "account": self.ACCOUNT},
            added=self.ADDED)

    def _row(self):
        return self.db.query(
            "select source,"
            " extract(epoch from probed_at)::float8 as probed_ts,"
            " extract(epoch from probe_missing_since)::float8 as missing_ts"
            " from credential_health where host = %s and harness = %s"
            " and account = %s", (self.HOST, self.HARNESS, self.ACCOUNT))[0]

    def _alerts(self, now):
        store = SimpleNamespace(credentials=Credentials(self.db))
        with mock.patch.object(exploitation.storage, "of", return_value=store):
            return exploitation._credential_probe_alerts(object(), now, None)

    def test_an_n1_probe_date_survives_a_later_turn_write(self):
        probe_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        turn_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
        self._n1_insert("sentinel", probe_at)
        self._n1_turn(turn_at)
        row = self._row()
        self.assertEqual(row["source"], "turn")
        self.assertAlmostEqual(row["probed_ts"], probe_at.timestamp(), delta=2)
        self.assertIsNone(row["missing_ts"])
        [alert] = self._alerts(turn_at.timestamp() + 1)
        self.assertEqual(alert["type"], "credential_probe_stale")
        self.assertAlmostEqual(alert["since"], probe_at.timestamp(), delta=2)
        self.assertIn("last reading", alert["detail"])

    def test_repeated_turn_writes_keep_the_anchor_of_a_never_probed_pair(self):
        first = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        later = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
        self._n1_insert("turn", first)
        self._n1_turn(later)
        row = self._row()
        self.assertIsNone(row["probed_ts"])
        self.assertAlmostEqual(row["missing_ts"], first.timestamp(), delta=2)
        [alert] = self._alerts(later.timestamp() + 1)
        self.assertIn("no probe reading", alert["detail"])
        self.assertAlmostEqual(alert["since"], first.timestamp(), delta=2)

    def test_an_n1_sentinel_update_advances_an_older_probe_date(self):
        first = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        second = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
        self._n1_insert("sentinel", first)
        self.previous_update(
            "credential_health",
            {"state": "healthy", "reason": "", "source": "sentinel",
             "checked_at": second},
            where={"host": self.HOST, "harness": self.HARNESS,
                   "account": self.ACCOUNT},
            added=self.ADDED)
        row = self._row()
        self.assertAlmostEqual(row["probed_ts"], second.timestamp(), delta=2)
        self.assertIsNone(row["missing_ts"])

    def test_a_fresh_sentinel_report_clears_the_missing_anchor(self):
        first = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        self._n1_insert("turn", first)
        Credentials(self.db).report(self.HOST, self.HARNESS, self.ACCOUNT,
                                    "healthy", source="sentinel",
                                    expect_failures=0)
        row = self._row()
        self.assertIsNotNone(row["probed_ts"])
        self.assertIsNone(row["missing_ts"])
        self.assertEqual(self._alerts(datetime.datetime.now(
            datetime.timezone.utc).timestamp()), [])

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
