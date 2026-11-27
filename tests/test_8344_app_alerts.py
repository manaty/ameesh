# SPDX-License-Identifier: AGPL-3.0-only
"""#8344 : une application lève et résout ses propres alertes.

Constat (manaty/ameesh#6584, IMA#1804) : `ameesh notify` ne suivait que les
alertes dérivées du mesh ; une application ne pouvait ni lever ni résoudre la
sienne, et passait à côté des routes, de l'avatar (#7634), de la limite par
humain, des reprises et du registre `notified`. Ces témoins couvrent la fiche
Application (`alerts:`), la levée idempotente et la résolution unique, la
livraison par la passe `alerts` (humain, avatar, route d'équipe, urgence,
débit), et le refus d'un type inconnu sans toucher aux alertes du mesh.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import app_alerts, canon as canon_mod, config, exploitation, notify, registry

from .support import PgTestCase

ALICE = "human:alice"
APP = "ima"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


ALERTS_BLOCK = """alerts:
  types:
    triage_stalled:
      urgent: false
    intake_down:
      urgent: true
  routes:
    triage-team: [team]
"""


def _canon(root: str, block: str = ALERTS_BLOCK, *, avatar: bool = False) -> None:
    _write(os.path.join(root, "membres", "alice.md"),
           "---\ntype: Member\ntitle: alice\n---\n")
    _write(os.path.join(root, "applications", APP + ".md"),
           "\n".join(["---", "type: Application", "title: " + APP,
                      "responsible: " + ALICE] + block.splitlines()
                     + ["---", "", "# " + APP, ""]))
    if avatar:
        _write(os.path.join(root, "agents", "bureau.md"),
               "---\ntype: Agent\ntitle: bureau\nresponsible: human:alice\n"
               "avatar:\n  conversation: portable\n---\n")


class ParseTest(unittest.TestCase):
    def test_bloc_absent(self):
        self.assertEqual(app_alerts.parse_alerts(None), (None, []))

    def test_bloc_valide(self):
        policy, defects = app_alerts.parse_alerts({
            "types": {"triage_stalled": None, "intake_down": {"urgent": True}},
            "routes": {"triage-team": ["team", "slack"]}})
        self.assertEqual(defects, [])
        self.assertFalse(policy.types["triage_stalled"].urgent)
        self.assertTrue(policy.types["intake_down"].urgent)
        self.assertEqual(policy.routes, {"triage-team": ("team", "slack")})

    def test_defauts_nommes(self):
        _, defects = app_alerts.parse_alerts({
            "types": {"Bad-Type": {}, "ok": {"urgent": "yes"}, "x": {"label": 1}},
            "routes": {"r": []}, "other": 1})
        codes = sorted(code for code, _ in defects)
        self.assertEqual(codes.count("application-alerts-type-invalid"), 3)
        self.assertIn("application-alerts-route-invalid", codes)
        self.assertIn("application-alerts-invalid", codes)
        self.assertEqual(app_alerts.parse_alerts([])[1][0][0],
                         "application-alerts-invalid")


class CanonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="canon-8344-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cfg = dataclasses.replace(config.load(env={}), canon=self.root,
                                       canon_untrusted=True, canon_ref="")

    def test_la_fiche_declare_types_et_routes(self):
        _canon(self.root)
        [application] = canon_mod.load(self.root, untrusted=True).applications
        self.assertEqual(application.to_dict()["alerts"]["routes"],
                         {"triage-team": ["team"]})
        policy = app_alerts.policies(self.cfg)[APP]
        self.assertEqual(sorted(policy.types), ["intake_down", "triage_stalled"])
        self.assertEqual(app_alerts.route_channels(self.cfg),
                         {"route:ima/triage-team": ("team",)})

    def test_un_bloc_fautif_rend_la_fiche_invalide_et_ne_declare_rien(self):
        _canon(self.root, "alerts:\n  types:\n    Bad: {}\n")
        loaded = canon_mod.load(self.root, untrusted=True)
        [application] = loaded.applications
        self.assertFalse(application.valid)
        findings = canon_mod.validate(loaded)
        self.assertIn("application-alerts-type-invalid",
                      [f.code for f in findings])
        self.assertEqual(app_alerts.policies(self.cfg), {})


class _PgCase(PgTestCase):
    avatar = False

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM app_alerts")
        self.root = tempfile.mkdtemp(prefix="canon-8344-pg-")
        self.addCleanup(shutil.rmtree, self.root, True)
        _canon(self.root, avatar=self.avatar)
        self.cfg = dataclasses.replace(self.cfg, canon=self.root,
                                       canon_untrusted=True, canon_ref="")
        self.now = time.time()
        self.ncfg = self.notify_config()
        self.sender = notify.DrySender()
        self.logs: list = []
        self.notifier = self.make_notifier()

    def notify_config(self, **extra) -> notify.NotifyConfig:
        return notify.parse_config(dict({
            "routes": {ALICE: ["app"]},
            "channels": {"team": {"type": "app"}},
            "types": ["mail_undeliverable"]}, **extra))

    def make_notifier(self, state=None, ncfg=None):
        return notify.Notifier(
            self.cfg, ncfg or self.ncfg,
            notify.empty_state() if state is None else state,
            sender=self.sender, log=self.logs.append, clock=lambda: self.now)

    def raise_(self, key="q1", kind="triage_stalled", **kwargs):
        if "route" not in kwargs:
            kwargs.setdefault("to", ALICE)
        kwargs.setdefault("text", "triage queue stalled for 2 h")
        return app_alerts.raise_alert(self.db, self.cfg, app=APP, type=kind,
                                      key=key, actor="ima-agent", **kwargs)

    def resolve(self, key="q1", kind="triage_stalled"):
        return app_alerts.resolve_alert(self.db, app=APP, type=kind, key=key,
                                        actor="ima-agent")

    def pass_(self, notifier=None):
        notifier = notifier or self.notifier
        return notifier.run_pass(self.db, current=app_alerts.alerts(self.db))

    def sent(self):
        return [(name, message.title) for name, message in self.sender.sent]


class RaiseResolveTest(_PgCase):
    def test_une_levee_repetee_ne_change_rien(self):
        first = self.raise_()
        self.assertEqual(first["state"], "raised")
        self.now += 5
        again = self.raise_(text="another wording")
        self.assertEqual(again["state"], "open")
        self.assertEqual(again["raised_ts"], first["raised_ts"])
        self.assertEqual(again["text"], "triage queue stalled for 2 h")
        self.assertEqual(len(app_alerts.listing(self.db)), 1)

    def test_resolution_unique_puis_nouvel_episode(self):
        first = self.raise_()
        self.assertEqual(self.resolve()["state"], "resolved")
        self.assertEqual(self.resolve()["state"], "not_open")
        self.assertEqual(app_alerts.listing(self.db), [])
        self.assertEqual(len(app_alerts.listing(self.db, open_only=False)), 1)
        reopened = self.raise_()
        self.assertEqual(reopened["state"], "raised")
        self.assertIsNone(reopened["resolved_ts"])
        self.assertGreaterEqual(reopened["raised_ts"], first["raised_ts"])

    def test_resoudre_une_cle_jamais_levee(self):
        self.assertEqual(self.resolve(key="never")["state"], "not_open")

    def test_refus_sans_ecriture(self):
        cases = {
            "unknown_type": dict(kind="not_declared"),
            "unknown_route": dict(route="nope"),
            "bad_recipient": dict(to="alice"),
            "bad_key": dict(key="two\nlines"),
            "bad_text": dict(text="   "),
        }
        for code, kwargs in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(app_alerts.AppAlertRefused) as caught:
                    self.raise_(**kwargs)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(app_alerts.AppAlertRefused) as caught:
            app_alerts.raise_alert(self.db, self.cfg, app="other", type="x",
                                   key="k", text="t", to=ALICE)
        self.assertEqual(caught.exception.code, "unknown_app")
        with self.assertRaises(app_alerts.AppAlertRefused) as caught:
            app_alerts.raise_alert(self.db, self.cfg, app=APP, type="triage_stalled",
                                   key="k", text="t", to=ALICE, route="triage-team")
        self.assertEqual(caught.exception.code, "bad_recipient")
        self.assertEqual(app_alerts.listing(self.db, open_only=False), [])

    def test_urgence_du_type_sauf_severite_explicite(self):
        self.assertFalse(self.raise_(key="a")["urgent"])
        self.assertTrue(self.raise_(key="b", kind="intake_down")["urgent"])
        self.assertTrue(self.raise_(key="c", urgent=True)["urgent"])

    def test_les_alertes_du_mesh_lisent_les_alertes_ouvertes(self):
        self.raise_()
        found = [a for a in exploitation.alerts(self.cfg, self.db, local_pool=False)
                 if a["type"] == app_alerts.ALERT_TYPE]
        self.assertEqual([(a["app"], a["app_type"], a["app_key"], a["responsible"])
                          for a in found], [(APP, "triage_stalled", "q1", ALICE)])

    def test_une_table_illisible_est_nommee(self):
        unreadable: list = []
        with mock.patch.object(app_alerts, "alerts", side_effect=RuntimeError("x")):
            out = exploitation.alerts(self.cfg, self.db, local_pool=False,
                                      unreadable=unreadable)
        self.assertIn(app_alerts.FAMILY, unreadable)
        self.assertNotIn(app_alerts.ALERT_TYPE, [a["type"] for a in out])


class DeliveryTest(_PgCase):
    def test_poussee_une_fois_resolue_une_fois(self):
        self.raise_()
        self.pass_()
        self.assertEqual(self.sent(),
                         [("app", "ima: triage stalled (q1)")])
        self.assertIn("triage queue stalled", self.sender.sent[0][1].body)
        # la même condition durable, deux passes de plus : rien ne repart
        self.raise_()
        self.now += 60
        self.pass_()
        self.pass_()
        self.assertEqual(len(self.sender.sent), 1)
        [entry] = self.notifier.state["active"].values()
        self.assertIn(self.notifier._notified_id(entry, ALICE),
                      self.notifier.state["notified"])
        self.resolve()
        self.now += 60
        self.pass_()
        self.pass_()
        self.assertEqual(self.sent()[1:], [("app", "ima: resolved — triage stalled (q1)")])
        self.assertEqual(self.notifier.state["active"], {})

    def test_un_redemarrage_ne_renvoie_pas(self):
        self.raise_()
        self.pass_()
        state = json.loads(json.dumps(self.notifier.state))
        self.pass_(self.make_notifier(state))
        self.assertEqual(len(self.sender.sent), 1)

    def test_nouvel_episode_dans_la_meme_seconde_sans_passe_intermediaire(self):
        """B1 (cx2) : l'épisode durable, pas l'heure arrondie, fait la clé."""
        self.raise_()
        self.pass_()
        self.resolve()
        self.assertEqual(self.raise_()["episode"], 2)   # même seconde, sans passe
        self.now += 60
        self.pass_()
        self.assertEqual([title for _, title in self.sent()],
                         ["ima: triage stalled (q1)",
                          "ima: resolved — triage stalled (q1)",
                          "ima: triage stalled (q1)"])
        self.assertEqual(len(self.notifier.state["notified"]), 2)

    def test_nouvel_episode_dans_la_meme_seconde_avec_passe_intermediaire(self):
        self.raise_()
        self.pass_()
        self.resolve()
        self.pass_()
        self.raise_()
        self.pass_()
        self.assertEqual([title for _, title in self.sent()],
                         ["ima: triage stalled (q1)",
                          "ima: resolved — triage stalled (q1)",
                          "ima: triage stalled (q1)"])

    def test_plus_de_500_alertes_ouvertes_toutes_collectees(self):
        """B2 (cx2) : aucune page ne cache les alertes les plus récentes."""
        self.db.execute(
            "INSERT INTO app_alerts (app, type, key, recipient, text)"
            " SELECT %s, 'triage_stalled', 'k' || g, %s, 'stalled'"
            " FROM generate_series(1, 520) g", (APP, ALICE))
        self.raise_(key="last", kind="intake_down")
        collected = app_alerts.alerts(self.db)
        self.assertEqual(len(collected), 521)
        notifier = self.make_notifier(ncfg=self.notify_config(rate_per_minute=0))
        self.pass_(notifier)
        self.assertIn("ima: intake down (last)",
                      [title for _, title in self.sent()])
        self.db.execute("UPDATE app_alerts SET resolved_at = clock_timestamp()"
                        " WHERE key LIKE 'k%%'")
        self.now += 60
        self.pass_(notifier)
        self.assertEqual(list(e["alert"]["app_key"]
                              for e in notifier.state["active"].values()), ["last"])

    def test_urgence_portee_par_le_message(self):
        self.raise_(key="n")
        self.raise_(key="u", kind="intake_down")
        self.pass_()
        urgent = {message.title: message.urgent for _, message in self.sender.sent}
        self.assertEqual(urgent, {"ima: triage stalled (n)": False,
                                  "ima: intake down (u)": True})

    def test_route_d_equipe(self):
        self.raise_(route="triage-team")
        self.pass_()
        self.assertEqual(self.sent(), [("team", "ima: triage stalled (q1)")])
        [entry] = self.notifier.state["active"].values()
        self.assertEqual((entry["human"], entry["source"]),
                         ("route:ima/triage-team", "route"))
        self.resolve()
        self.pass_()
        self.assertEqual(self.sent()[-1], ("team", "ima: resolved — triage stalled (q1)"))

    def test_route_sans_canal_sur_l_hote_attend(self):
        notifier = self.make_notifier(ncfg=notify.parse_config({
            "routes": {ALICE: ["app"]}}))
        self.raise_(route="triage-team")
        self.pass_(notifier)
        self.assertEqual(self.sender.sent, [])
        [entry] = notifier.state["active"].values()
        self.assertEqual(entry["status"], "sans_canal")

    def test_limite_par_humain_et_route_a_part(self):
        notifier = self.make_notifier(ncfg=self.notify_config(rate_per_minute=1))
        for key in ("a", "b", "c"):
            self.raise_(key=key)
        self.raise_(key="r", route="triage-team")
        self.pass_(notifier)
        # une alerte à alice, son résumé de débit, et la route sur son canal
        titles = sorted((name, message.title.split(":")[0])
                        for name, message in self.sender.sent)
        self.assertEqual([name for name, _ in titles], ["app", "app", "team"])
        self.assertEqual(sum(1 for name, message in self.sender.sent
                             if name == "app" and message.title.startswith("ima:")), 1)
        statuses = sorted(e["status"] for e in notifier.state["active"].values())
        self.assertEqual(statuses, ["envoi", "envoi", "resumee", "resumee"])

    def test_table_illisible_aucune_resolution(self):
        self.raise_()
        self.pass_()

        def unreadable(*args, **kwargs):
            kwargs["unreadable"].append(app_alerts.FAMILY)
            return []
        with mock.patch.object(exploitation, "alerts", side_effect=unreadable):
            self.notifier.run_pass(self.db, thresholds={})
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(len(self.notifier.state["active"]), 1)

    def test_le_type_ne_depend_pas_de_notify_types(self):
        self.assertTrue(self.notifier._deliverable(
            {"type": app_alerts.ALERT_TYPE, "app": APP}))


class AvatarTest(_PgCase):
    avatar = True

    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "bureau", harness="claude", host=self.cfg.host)
        self.assertIsNotNone(registry.claim(self.db, "bureau", "runner-8344", 3600))

    def mails(self) -> list:
        return [row["payload"] for row in self.db.query(
            "SELECT payload FROM agent_mailbox WHERE recipient = 'bureau' ORDER BY id")
            if (row["payload"] or {}).get("alert") == app_alerts.ALERT_TYPE]

    def test_non_urgente_ne_reveille_pas_l_avatar(self):
        self.raise_()
        self.pass_()
        [payload] = self.mails()
        self.assertNotIn("urgent", payload)
        self.assertEqual((payload["app"], payload["app_key"]), (APP, "q1"))
        [(channel, signal)] = self.sender.sent
        self.assertEqual(channel, "app")
        self.assertFalse(signal.urgent)
        self.assertNotIn("triage", signal.body)

    def test_la_resolution_suit_l_avatar_une_fois(self):
        self.raise_()
        self.pass_()
        self.resolve()
        self.now += 60
        self.pass_()
        self.pass_()
        self.assertEqual(len(self.mails()), 2)
        self.assertEqual(len(self.sender.sent), 1)          # le seul signal
        self.assertEqual(self.notifier.state["active"], {})

    def test_urgente_reveille_l_avatar(self):
        self.raise_(kind="intake_down")
        self.pass_()
        [payload] = self.mails()
        self.assertTrue(payload["urgent"])
        self.assertTrue(self.sender.sent[0][1].urgent)


class CliTest(_PgCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch("ameesh.db.open_db", return_value=self.db), \
                mock.patch.object(self.db, "close"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = notify.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_lever_resoudre_lister(self):
        base = ("--app", APP, "--type", "triage_stalled", "--key", "q1")
        code, out, _ = self.run_cli("raise", *base, "--to", ALICE, "--text", "stalled")
        self.assertEqual((code, out.strip()), (0, "ima/triage_stalled q1: raised"))
        code, out, _ = self.run_cli("raise", *base, "--to", ALICE, "--text", "stalled")
        self.assertIn("already open", out)
        code, out, _ = self.run_cli("list", "--json")
        self.assertEqual([row["key"] for row in json.loads(out)], ["q1"])
        code, out, _ = self.run_cli("resolve", *base, "--json")
        self.assertEqual(json.loads(out)["state"], "resolved")

    def test_type_inconnu_refuse(self):
        code, _, err = self.run_cli("raise", "--app", APP, "--type", "unknown",
                                    "--key", "k", "--to", ALICE, "--text", "t")
        self.assertEqual(code, 2)
        self.assertIn("unknown_type", err)


if __name__ == "__main__":
    unittest.main()
