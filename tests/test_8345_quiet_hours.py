# SPDX-License-Identifier: AGPL-3.0-only
"""#8345 : heures calmes par humain dans `ameesh notify`.

Témoins du lot (manaty/ameesh#6585, IMA#1804) : une fenêtre locale déclarée
sur la carte Member (ou dans `notify.quiet_hours` de l'hôte) retient les
poussées non urgentes et les rend en UNE synthèse à la sortie ; une alerte
urgente traverse la fenêtre ; le mail à l'avatar est écrit quand même ; un
humain sans fuseau lisible n'est jamais réputé en calme ; `--test` et
`doctor` nomment la fenêtre, le fuseau et l'état courant. Le changement
d'heure (DST) et le changement de fuseau sont couverts par le calcul, sans
horloge murale propre.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from ameesh import canon, cli, notify, quiet_hours, registry

from .support import PgTestCase


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _member(root: str, name: str, *, timezone=None, quiet=None, extra="") -> None:
    lines = ["---", "type: Member", "title: %s" % name]
    if timezone is not None:
        lines.append("timezone: %s" % timezone)
    if quiet is not None:
        if isinstance(quiet, dict):
            lines.append("quiet_hours:")
            for key, value in quiet.items():
                lines.append('  %s: "%s"' % (key, value))
        else:
            lines.append('quiet_hours: "%s"' % quiet)
    lines += ["---", "", extra or "# %s" % name, ""]
    _write(os.path.join(root, "membres", name + ".md"), "\n".join(lines))


def _avatar(root: str, name: str, human: str) -> None:
    _write(os.path.join(root, "agents", name + ".md"),
           "\n".join(["---", "type: Agent", "title: %s" % name,
                      "responsible: %s" % human, "avatar:",
                      "  conversation: portable", "---", "", "# %s" % name, ""]))


def _ts(timezone: str, year: int, month: int, day: int, hour: int,
        minute: int = 0, fold: int = 0) -> float:
    return datetime(year, month, day, hour, minute,
                    tzinfo=ZoneInfo(timezone), fold=fold).timestamp()


def _alert(kind="idle_with_mail", agent="ouvrier", since=1000.0, **extra) -> dict:
    out = {"schema": "ameesh-alert/1", "type": kind, "agent": agent,
           "since": since, "value": 2, "threshold": 300,
           "detail": "agent inactif avec 2 message(s) non lu(s)"}
    out.update(extra)
    return out


class _CanonFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="canon-8345-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def load(self):
        return canon.load(self.root, untrusted=True)

    def codes(self) -> set:
        return {f.code for f in canon.validate(self.load()) if f.severity == canon.ERROR}


# ==========================================================================
# fenêtre et état (sans base)
# ==========================================================================

class WindowTest(unittest.TestCase):
    def test_fenetre_lisible_et_texte_canonique(self):
        window = quiet_hours.parse(" 22:00 - 07:00 ")
        self.assertEqual(window.text, "22:00-07:00")
        self.assertEqual(window.describe(), "22:00-07:00")
        self.assertTrue(window.contains(datetime(2026, 11, 27, 22, 0,
                                                tzinfo=ZoneInfo("Asia/Manila"))))
        self.assertTrue(window.contains(datetime(2026, 11, 28, 6, 59,
                                                tzinfo=ZoneInfo("Asia/Manila"))))
        self.assertFalse(window.contains(datetime(2026, 11, 28, 7, 0,
                                                 tzinfo=ZoneInfo("Asia/Manila"))))
        self.assertFalse(window.contains(datetime(2026, 11, 27, 21, 59,
                                                 tzinfo=ZoneInfo("Asia/Manila"))))

    def test_fenetre_invalide_nommee(self):
        for value in ("22:00", "7:00-9:00", "24:00-07:00", "22:00-22:00",
                      "22:00-07:60", None, 22):
            with self.subTest(value=value), self.assertRaises(quiet_hours.QuietHoursError):
                quiet_hours.parse(value)

    def test_objet_avec_fuseau_et_cle_inconnue(self):
        window = quiet_hours.parse_value(
            {"start": "23:00", "end": "06:30", "timezone": "Asia/Manila"})
        self.assertEqual(window.text, "23:00-06:30")
        self.assertEqual(window.timezone, "Asia/Manila")
        for raw in ({"start": "22:00", "end": "07:00", "zone": "Asia/Manila"},
                    {"start": "22:00", "end": "07:00", "timezone": "Mars/Olympus"},
                    {"start": "22:00"}):
            with self.subTest(raw=raw), self.assertRaises(quiet_hours.QuietHoursError):
                quiet_hours.parse_value(raw)

    def test_carte_sans_cle_timezone(self):
        with self.assertRaises(quiet_hours.QuietHoursError):
            quiet_hours.parse_value({"start": "22:00", "end": "07:00",
                                     "timezone": "Asia/Manila"},
                                    allow_timezone=False)


class StateTest(unittest.TestCase):
    def test_sans_fenetre_sans_fuseau_ou_zone_illisible(self):
        self.assertEqual(quiet_hours.state(None, "Asia/Manila", 0).problem,
                         quiet_hours.NO_WINDOW)
        self.assertFalse(quiet_hours.state(None, None, 0).active)
        window = quiet_hours.parse("22:00-07:00")
        no_zone = quiet_hours.state(window, None, _ts("Asia/Manila", 2026, 11, 27, 23))
        self.assertFalse(no_zone.active)
        self.assertEqual(no_zone.problem, quiet_hours.NO_TIMEZONE)
        bad = quiet_hours.state(window, "Mars/Olympus",
                                _ts("Asia/Manila", 2026, 11, 27, 23))
        self.assertFalse(bad.active)
        self.assertEqual(bad.problem, quiet_hours.INVALID_TIMEZONE)

    def test_fenetre_qui_ne_traverse_pas_minuit(self):
        window = quiet_hours.parse("08:00-17:00")
        morning = quiet_hours.state(window, "Europe/Paris",
                                    _ts("Europe/Paris", 2026, 11, 27, 9))
        evening = quiet_hours.state(window, "Europe/Paris",
                                    _ts("Europe/Paris", 2026, 11, 27, 18))
        self.assertTrue(morning.active)
        self.assertFalse(evening.active)
        self.assertIn("next 2026-11-28 08:00", evening.line("human:ada"))

    def test_changement_de_fuseau_a_instant_egal(self):
        # 2026-11-27 15:00 UTC = 23:00 à Manille = 16:00 à Paris.
        window = quiet_hours.parse("22:00-07:00")
        instant = _ts("UTC", 2026, 11, 27, 15)
        manila = quiet_hours.state(window, "Asia/Manila", instant)
        paris = quiet_hours.state(window, "Europe/Paris", instant)
        self.assertTrue(manila.active)
        self.assertFalse(paris.active)
        self.assertIn("Asia/Manila", manila.line("human:ada"))
        self.assertIn("Europe/Paris", paris.line("human:ada"))

    def test_changement_d_heure_printemps_et_automne(self):
        window = quiet_hours.parse("01:00-03:00")
        # 29/03/2026 : 02:00 CET -> 03:00 CEST ; 01:30 est calme, 03:30 non.
        before = quiet_hours.state(window, "Europe/Paris",
                                   _ts("Europe/Paris", 2026, 3, 29, 1, 30))
        after = quiet_hours.state(window, "Europe/Paris",
                                  _ts("Europe/Paris", 2026, 3, 29, 3, 30))
        self.assertTrue(before.active)
        self.assertFalse(after.active)
        # 25/10/2026 : 02:30 existe deux fois (CEST puis CET), les deux calmes.
        first = quiet_hours.state(window, "Europe/Paris",
                                  _ts("Europe/Paris", 2026, 10, 25, 2, 30, fold=0))
        second = quiet_hours.state(window, "Europe/Paris",
                                   _ts("Europe/Paris", 2026, 10, 25, 2, 30, fold=1))
        self.assertTrue(first.active)
        self.assertTrue(second.active)


# ==========================================================================
# carte Member et configuration de l'hôte
# ==========================================================================

class CanonTest(_CanonFixture):
    def test_fenetre_et_fuseau_lus(self):
        _member(self.root, "ada", timezone="Asia/Manila", quiet="22:00-07:00")
        _member(self.root, "bruno", timezone="Europe/Paris",
                quiet={"start": "23:00", "end": "06:30"})
        loaded = self.load()
        ada = next(m for m in loaded.members if m.title == "ada")
        bruno = next(m for m in loaded.members if m.title == "bruno")
        self.assertEqual(ada.timezone, "Asia/Manila")
        self.assertEqual(ada.quiet_hours, "22:00-07:00")
        self.assertEqual(bruno.quiet_hours, "23:00-06:30")
        self.assertEqual(self.codes(), set())
        by_title = {member["title"]: member for member in loaded.to_dict()["members"]}
        self.assertEqual(by_title["ada"]["quiet_hours"], "22:00-07:00")

    def test_fenetre_invalide_ou_sans_fuseau_refusee(self):
        _member(self.root, "ada", timezone="Asia/Manila", quiet="25:00-07:00")
        _member(self.root, "bruno", quiet="22:00-07:00")
        loaded = self.load()
        ada = next(m for m in loaded.members if m.title == "ada")
        bruno = next(m for m in loaded.members if m.title == "bruno")
        self.assertIsNone(ada.quiet_hours)
        self.assertIsNone(bruno.quiet_hours)
        findings = [f for f in canon.validate(loaded)
                    if f.code == "member-quiet-hours-invalid"]
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(f.severity == canon.ERROR for f in findings))

    def test_fuseau_illisible_laisse_la_fenetre_sans_effet(self):
        _member(self.root, "ada", timezone="Mars/Olympus", quiet="22:00-07:00")
        loaded = self.load()
        ada = next(m for m in loaded.members if m.title == "ada")
        self.assertIsNone(ada.timezone)
        self.assertIsNone(ada.quiet_hours)
        self.assertIn("member-timezone-invalid", self.codes())


class ConfigTest(unittest.TestCase):
    def test_formes_acceptees(self):
        ncfg = notify.parse_config({
            "quiet_hours": {
                "human:ada": "22:00-07:00",
                "human:bruno": {"start": "23:00", "end": "06:00",
                                "timezone": "Asia/Manila"}}})
        self.assertEqual(ncfg.quiet_hours["human:ada"].text, "22:00-07:00")
        self.assertIsNone(ncfg.quiet_hours["human:ada"].timezone)
        self.assertEqual(ncfg.quiet_hours["human:bruno"].timezone, "Asia/Manila")

    def test_formes_refusees(self):
        for raw in ({"human:ada": "22:00-22:00"},
                    {"human:ada": "22:00"},
                    {"ada": "22:00-07:00"},
                    {"human:ada": {"start": "22:00", "end": "07:00",
                                   "timezone": "Mars/Olympus"}},
                    {"human:ada": "22:00-07:00", "ada": "23:00-06:00"}):
            with self.subTest(raw=raw), self.assertRaises(notify.NotifyConfigError):
                notify.parse_config({"quiet_hours": raw})

    def test_lignes_de_diagnostic_sans_canon(self):
        ncfg = notify.parse_config({"quiet_hours": {
            "human:ada": {"start": "22:00", "end": "07:00",
                          "timezone": "Asia/Manila"}}})
        cfg = dataclasses.replace(self._cfg(), canon=None)
        now = _ts("Asia/Manila", 2026, 11, 27, 23)
        lines = notify.quiet_hours_lines(cfg, now=now, ncfg=ncfg)
        self.assertEqual(len(lines), 1)
        self.assertIn("human:ada: in quiet hours now", lines[0])
        self.assertIn("22:00-07:00 Asia/Manila", lines[0])
        empty = notify.parse_config({})
        self.assertEqual(notify.quiet_hours_lines(cfg, now=now, ncfg=empty),
                         ["none declared (a human is never held without "
                          "quiet hours)"])

    def test_fenetre_de_config_sans_fuseau_est_dite(self):
        ncfg = notify.parse_config({"quiet_hours": {"human:ada": "22:00-07:00"}})
        cfg = dataclasses.replace(self._cfg(), canon=None)
        lines = notify.quiet_hours_lines(cfg, now=0.0, ncfg=ncfg)
        self.assertIn("without a time zone", lines[0])
        self.assertIn("never held", lines[0])

    @staticmethod
    def _cfg():
        from ameesh import config
        return config.load(env={})


# ==========================================================================
# la passe de livraison (base réelle)
# ==========================================================================

class _PgCase(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="canon-8345-pg-")
        self.addCleanup(shutil.rmtree, self.root, True)
        _member(self.root, "proprio", timezone="Asia/Manila", quiet="22:00-07:00")
        self.cfg = dataclasses.replace(self.cfg, canon=self.root,
                                       canon_untrusted=True, canon_ref="")
        self.now = _ts("Asia/Manila", 2026, 11, 27, 23)
        self.sender = notify.DrySender()
        self.logs: list = []

    def ncfg(self, **raw) -> notify.NotifyConfig:
        raw.setdefault("default", ["desktop"])
        raw.setdefault("default_human", "human:proprio")
        return notify.parse_config(raw)

    def notifier(self, ncfg=None, state=None, cfg=None, **kw) -> notify.Notifier:
        return notify.Notifier(
            cfg or self.cfg, ncfg or self.ncfg(),
            notify.empty_state() if state is None else state,
            sender=self.sender, log=self.logs.append, clock=lambda: self.now, **kw)

    def passe(self, notifier, current=None):
        return notifier.run_pass(self.db, current=current)

    def sent(self):
        return [(name, message.title) for name, message in self.sender.sent]


class QuietPassTest(_PgCase):
    def test_poussee_non_urgente_retenue_puis_une_synthese(self):
        notifier = self.notifier()
        alert = _alert()
        records = self.passe(notifier, [alert])
        self.assertEqual([r["delivery"] for r in records if r["event"] == "raised"],
                         ["resumee"])
        self.assertEqual(self.sender.sent, [])
        self.assertIn("human:proprio", notifier.state["summary"])
        # toujours dans la fenêtre : rien ne sonne, même après une heure
        self.now += 3600
        self.assertEqual(self.passe(notifier, [alert]), [])
        self.assertEqual(self.sender.sent, [])
        # sortie de fenêtre : UNE synthèse, et pas le texte de l'alerte
        self.now = _ts("Asia/Manila", 2026, 11, 28, 7, 0) + 30
        records = self.passe(notifier, [alert])
        summaries = [r for r in records if r["event"] == "summary"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(len(self.sender.sent), 1)
        _channel, message = self.sender.sent[0]
        self.assertIn("held during quiet hours (22:00-07:00)", message.title)
        self.assertIn("1 idle_with_mail", message.body)
        self.assertIn("Asia/Manila", message.body)
        self.assertEqual(notifier.state["summary"], {})
        # une passe de plus ne redouble ni la levée ni la synthèse
        self.now += 60
        self.assertEqual([r for r in self.passe(notifier, [alert])
                          if r["event"] in ("raised", "summary")], [])
        self.assertEqual(len(self.sender.sent), 1)

    def test_synthese_survit_au_redemarrage(self):
        notifier = self.notifier()
        alert = _alert()
        self.passe(notifier, [alert])
        state = json.loads(json.dumps(notifier.state))
        notifier = self.notifier(state=state)
        self.now = _ts("Asia/Manila", 2026, 11, 28, 7, 0) + 30
        self.passe(notifier, [alert])
        self.assertEqual(len(self.sender.sent), 1)
        self.assertIn("quiet hours", self.sender.sent[0][1].title)

    def test_urgence_traverse_la_fenetre(self):
        notifier = self.notifier()
        records = self.passe(notifier, [_alert(kind="stopped_with_mail")])
        self.assertEqual([r["delivery"] for r in records if r["event"] == "raised"],
                         ["envoyee"])
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(notifier.state["summary"], {})

    def test_severite_urgente_et_application_urgente(self):
        # La règle de livraison ne dépend pas du seul type : une sévérité
        # urgente, ou le booléen d'une alerte d'application, traverse aussi.
        self.assertTrue(notify.alert_urgent({"type": "x", "severity": "urgent"}))
        self.assertTrue(notify.alert_urgent({"type": "x", "level": "critical"}))
        self.assertTrue(notify.alert_urgent({"type": "app_alert", "urgent": True}))
        self.assertFalse(notify.alert_urgent({"type": "app_alert"}))

    def test_bornes_de_fenetre(self):
        # 21:59 : pas encore calme ; 22:00 : calme ; 06:59 : calme ;
        # 07:00 : sorti (la fin est exclusive).
        notifier = self.notifier()
        self.now = _ts("Asia/Manila", 2026, 11, 27, 21, 59)
        self.assertEqual([r["delivery"] for r in self.passe(notifier, [
            _alert(agent="a21")]) if r["event"] == "raised"], ["envoyee"])
        self.now = _ts("Asia/Manila", 2026, 11, 27, 22, 0)
        self.assertEqual([r["delivery"] for r in self.passe(notifier, [
            _alert(agent="a22")]) if r["event"] == "raised"], ["resumee"])
        self.now = _ts("Asia/Manila", 2026, 11, 28, 6, 59)
        self.assertEqual([r["delivery"] for r in self.passe(notifier, [
            _alert(agent="a06")]) if r["event"] == "raised"], ["resumee"])
        self.now = _ts("Asia/Manila", 2026, 11, 28, 7, 0)
        self.assertEqual([r["delivery"] for r in self.passe(notifier, [
            _alert(agent="a07")]) if r["event"] == "raised"], ["envoyee"])

    def test_plusieurs_poussées_une_seule_synthese(self):
        notifier = self.notifier()
        alerts = [_alert(agent="a%d" % index) for index in range(3)]
        self.passe(notifier, alerts)
        self.assertEqual(self.sender.sent, [])
        self.now = _ts("Asia/Manila", 2026, 11, 28, 7, 0) + 30
        records = self.passe(notifier, alerts)
        self.assertEqual(len([r for r in records if r["event"] == "summary"]), 1)
        self.assertEqual(len(self.sender.sent), 1)
        self.assertIn("3 idle_with_mail", self.sender.sent[0][1].body)

    def test_resolution_retenue_et_comptee_dans_la_synthese(self):
        notifier = self.notifier()
        alert = _alert(agent="a1")
        self.now = _ts("Asia/Manila", 2026, 11, 27, 21, 0)
        self.passe(notifier, [alert])
        self.assertEqual(len(self.sender.sent), 1)
        # l'alerte se résout pendant la fenêtre : la résolution attend
        self.now = _ts("Asia/Manila", 2026, 11, 27, 23, 0)
        records = self.passe(notifier, [])
        self.assertEqual([r["delivery"] for r in records if r["event"] == "resolved"],
                         ["resumee"])
        self.assertEqual(len(self.sender.sent), 1)
        self.now = _ts("Asia/Manila", 2026, 11, 28, 7, 0) + 30
        self.passe(notifier, [])
        self.assertEqual(len(self.sender.sent), 2)
        message = self.sender.sent[1][1]
        self.assertIn("held during quiet hours", message.title)
        self.assertIn("resolved: 1", message.body)

    def test_deux_humains_un_seul_en_calme(self):
        _member(self.root, "autre", timezone="Europe/Paris", quiet="09:00-12:00")
        notifier = self.notifier()
        quiet_alert = _alert(agent="a", responsible="human:proprio")
        awake_alert = _alert(agent="b", responsible="human:autre", since=2000.0)
        records = self.passe(notifier, [quiet_alert, awake_alert])
        sent = [r for r in records if r["event"] == "raised"]
        self.assertEqual(sorted((r["human"], r["delivery"]) for r in sent),
                         [("human:autre", "envoyee"), ("human:proprio", "resumee")])
        self.assertEqual(len(self.sender.sent), 1)
        self.assertIn("(b)", self.sender.sent[0][1].title)
        self.now = _ts("Asia/Manila", 2026, 11, 28, 7, 0) + 30
        self.passe(notifier, [quiet_alert, awake_alert])
        summaries = [title for _channel, title in self.sent() if "quiet" in title]
        self.assertEqual(len(summaries), 1)

    def test_sans_fuseau_jamais_en_calme(self):
        _member(self.root, "sans-tz", quiet="22:00-07:00")
        ncfg = self.ncfg(quiet_hours={"human:sans-tz": "22:00-07:00"})
        notifier = self.notifier(ncfg)
        state = notify.quiet_state(self.cfg, ncfg, "human:sans-tz", self.now)
        self.assertFalse(state.active)
        self.assertEqual(state.problem, quiet_hours.NO_TIMEZONE)
        records = self.passe(notifier, [_alert(agent="c",
                                               responsible="human:sans-tz")])
        self.assertEqual([r["delivery"] for r in records if r["event"] == "raised"],
                         ["envoyee"])
        self.assertEqual(len(self.sender.sent), 1)

    def test_fenetre_de_config_avec_fuseau_prime(self):
        ncfg = self.ncfg(quiet_hours={
            "human:proprio": {"start": "09:00", "end": "12:00",
                              "timezone": "Europe/Paris"}})
        self.now = _ts("Europe/Paris", 2026, 11, 27, 10, 0)
        state = notify.quiet_state(self.cfg, ncfg, "human:proprio", self.now)
        self.assertTrue(state.active)
        self.assertEqual(state.timezone, "Europe/Paris")
        self.assertEqual(state.source, "notify")
        notifier = self.notifier(ncfg)
        self.passe(notifier, [_alert()])
        self.assertEqual(self.sender.sent, [])

    def test_avatar_recoit_le_mail_pendant_la_fenetre(self):
        _avatar(self.root, "bureau", "human:proprio")
        registry.upsert(self.db, "bureau", harness="claude", host=self.cfg.host)
        self.assertIsNotNone(registry.claim(self.db, "bureau", "runner-8345", 3600))
        notifier = self.notifier()
        self.passe(notifier, [_alert()])
        rows = self.db.query(
            "SELECT payload FROM agent_mailbox WHERE recipient = 'bureau'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["alert"], "idle_with_mail")
        # l'avatar lit ; l'humain n'est pas réveillé par le signal direct
        self.assertEqual(self.sender.sent, [])
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now() "
                        "WHERE recipient = 'bureau'")
        self.now = _ts("Asia/Manila", 2026, 11, 28, 7, 0) + 30
        self.passe(notifier, [_alert()])
        self.assertEqual(len(self.sender.sent), 1)
        self.assertIn("quiet hours", self.sender.sent[0][1].title)


# ==========================================================================
# diagnostics : `--test` et `doctor`
# ==========================================================================

class DiagnosticsTest(_PgCase):
    def run_test(self, *argv) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        # `cmd_test` lit l'horloge réelle : le diagnostic doit être rejouable
        # hors de la fenêtre calme (défaut vu à la composition du paquet
        # h-20261128-01, 10:35 à Manille) — l'heure est fixée.
        with mock.patch.object(notify.time, "time", return_value=self.now), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = notify.cmd_test(self.cfg, self.ncfg(),
                                   argparse.Namespace(test=argv[0],
                                                      json="--json" in argv))
        return code, out.getvalue(), err.getvalue()

    def test_test_humain_dit_la_fenetre_et_l_etat(self):
        with mock.patch.object(notify, "send_test", return_value=[("desktop", None)]):
            code, out, err = self.run_test("human:proprio")
        self.assertEqual(code, 0, err)
        self.assertIn("quiet hours: human:proprio: in quiet hours now "
                      "(22:00-07:00 Asia/Manila", out)
        self.assertIn("desktop → human:proprio: sent", out)

    def test_test_humain_json_porte_l_etat(self):
        with mock.patch.object(notify, "send_test", return_value=[("desktop", None)]):
            code, out, _err = self.run_test("human:proprio", "--json")
        self.assertEqual(code, 0)
        record = json.loads(out)
        self.assertEqual(record["quiet_hours"]["active"], True)
        self.assertEqual(record["quiet_hours"]["window"], "22:00-07:00")
        self.assertEqual(record["quiet_hours"]["timezone"], "Asia/Manila")

    def test_doctor_nomme_les_heures_calmes(self):
        lines = notify.quiet_hours_lines(self.cfg, now=self.now)
        self.assertEqual(len(lines), 1)
        self.assertIn("human:proprio: in quiet hours now", lines[0])
        self.assertIn("22:00-07:00", lines[0])
        self.assertIn("Asia/Manila", lines[0])
        self.assertIn("canon", lines[0])

    def test_doctor_affiche_la_ligne(self):
        out = io.StringIO()
        with mock.patch.object(notify.time, "time", return_value=self.now), \
                contextlib.redirect_stdout(out):
            cli.cmd_doctor(self.cfg, notify_test=False)
        self.assertIn("quiet hours: human:proprio: in quiet hours now",
                      out.getvalue())


if __name__ == "__main__":
    unittest.main()
