# SPDX-License-Identifier: AGPL-3.0-only
"""#7634 : joindre un humain, c'est écrire à son avatar.

L'avatar est une fiche Agent du canon qui porte un bloc ``avatar`` et dont
``responsible`` est l'humain visé. Le cœur ne connaît aucun nom d'avatar :
``notify`` dépose l'alerte dans sa boîte, sans pousser le canal direct ; si
l'avatar est arrêté ou injoignable au-delà du délai, la route directe de
l'humain reprend, avec un événement ``avatar.fallback`` au journal. Un humain
sans avatar garde la route directe d'avant.
"""
from __future__ import annotations

import ast
import dataclasses
import os
import shutil
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from ameesh import (alert_briefs, avatars, canon as canon_mod, config, db_sante,
                    debit, mail, migration_alerts, notify, pgbouncer_local,
                    registry)

from .support import PgTestCase


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _card(title: str, human: str = "smichea", *, block: bool = True) -> str:
    lines = ["---", "type: Agent", "title: " + title,
             "responsible: human:" + human]
    if block:
        lines += ["avatar:", "  conversation: portable"]
    lines += ["---", ""]
    return "\n".join(lines)


class _CanonCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="canon-7634-")
        self.addCleanup(shutil.rmtree, self.root, True)
        _write(os.path.join(self.root, "membres", "smichea.md"),
               "---\ntype: Member\ntitle: smichea\n---\n")
        self.cfg = dataclasses.replace(config.load(env={}), canon=self.root,
                                       canon_untrusted=True, canon_ref="")

    def avatar(self, title: str, *, block: bool = True,
               human: str = "smichea") -> None:
        _write(os.path.join(self.root, "agents", title + ".md"),
               _card(title, human, block=block))


class AvatarCanonTest(_CanonCase):
    """La résolution : un avatar par humain, lu à la fiche, jamais au nom."""

    def test_un_avatar_par_humain(self):
        self.avatar("bureau")
        self.assertEqual(avatars.configured(self.cfg),
                         {"human:smichea": "bureau"})
        self.assertEqual(avatars.name_for(self.cfg, "human:smichea"), "bureau")
        self.assertTrue(avatars.is_avatar(self.cfg, "bureau"))
        self.assertEqual(avatars.human_of(self.cfg, "bureau"), "human:smichea")
        self.assertFalse(avatars.is_avatar(self.cfg, "autre"))

    def test_une_fiche_sans_bloc_n_est_pas_un_avatar(self):
        self.avatar("bureau", block=False)
        self.assertEqual(avatars.configured(self.cfg), {})

    def test_deux_avatars_du_meme_humain_sont_ecartes(self):
        self.avatar("bureau")
        self.avatar("autre-bureau")
        self.assertEqual(avatars.configured(self.cfg), {})

    def test_un_canon_illisible_ne_devine_aucun_avatar(self):
        cfg = dataclasses.replace(self.cfg,
                                  canon=os.path.join(self.root, "absent"))
        self.assertEqual(avatars.configured(cfg), {})
        self.assertIsNone(avatars.name_for(cfg, "human:smichea"))

    def test_un_responsable_sans_membre_ne_fait_pas_un_avatar(self):
        self.avatar("bureau", human="inconnu")
        self.assertEqual(avatars.configured(self.cfg), {})

    def test_les_routes_humaines_du_debit_lisent_canon_et_registre(self):
        """#7634 : le nom historique n'est une route que lu au runtime."""
        self.avatar("bureau")
        canons = canon_mod.load_configured(self.cfg)
        rows = [{"name": "bureau", "responsible": "human:smichea",
                 "mode": "externe"}]
        self.assertEqual(debit.human_route_names(self.cfg, rows, canons),
                         {"bureau"})
        # sans canon, le `report` configuré et le `responsible` du registre
        # suffisent ; un agent externe non rattaché n'entre pas.
        cfg = dataclasses.replace(
            self.cfg, canon="",
            release={"projects": {"ameesh": {"report": "bureau"}}})
        self.assertEqual(debit.human_route_names(cfg, rows, []), {"bureau"})
        self.assertEqual(debit.human_route_names(
            cfg, [{"name": "bureau2", "mode": "externe"}], []), set())


class NotifyAvatarRouteTest(PgTestCase):
    """La route de `notify` : la boîte de l'avatar, puis la route directe."""

    def setUp(self) -> None:
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="canon-7634-notify-")
        self.addCleanup(shutil.rmtree, self.root, True)
        _write(os.path.join(self.root, "membres", "smichea.md"),
               "---\ntype: Member\ntitle: smichea\n---\n")
        _write(os.path.join(self.root, "agents", "bureau.md"),
               _card("bureau"))
        self.cfg = dataclasses.replace(self.cfg, canon=self.root,
                                       canon_untrusted=True, canon_ref="")
        registry.upsert(self.db, "bureau", harness="claude", host=self.cfg.host)
        self.assertIsNotNone(registry.claim(self.db, "bureau", "runner-7634", 3600))
        self.now = time.time()
        self.ncfg = notify.parse_config({
            "default_human": "human:smichea",
            "routes": {"human:smichea": ["app"]},
            "types": ["mail_undeliverable"],
        })
        self.sender = notify.DrySender()
        self.notifier = notify.Notifier(
            self.cfg, self.ncfg, notify.empty_state(), sender=self.sender,
            log=lambda text: None, clock=lambda: self.now)

    def alert(self) -> dict:
        return {"type": "mail_undeliverable", "agent": "claude3",
                "responsible": "human:smichea", "value": 1, "threshold": 900,
                "reason": "arrete", "senders": ["claude1"],
                "since": self.now - 1200, "detail": "undeliverable",
                "urgent": True}

    def mails(self, name: str) -> list:
        return self.db.query(
            "SELECT body, payload FROM agent_mailbox WHERE recipient = %s"
            " ORDER BY id", (name,))

    def alerts(self, name: str) -> list:
        """Les courriers qui portent l'alerte elle-même.

        La boîte reçoit aussi les briefs d'escalade du même type (le desk est
        l'avatar ici) : les tests de route comptent les alertes, pas les
        briefs qui les nomment. Seul le dépôt chez l'avatar porte `human`."""
        return [row for row in self.mails(name)
                if (row["payload"] or {}).get("alert") == "mail_undeliverable"
                and (row["payload"] or {}).get("human")]

    def entry(self) -> dict:
        [entry] = self.notifier.state["active"].values()
        return entry

    def events(self, kind: str, alert: dict) -> list:
        return self.db.query(
            "SELECT type FROM ameesh_events WHERE type = %s"
            " AND data->>'key' = %s", (kind, notify.key_of(alert)))

    def test_l_alerte_arrive_dans_la_boite_de_l_avatar_sans_canal_direct(self):
        alert = self.alert()
        before = len(self.events("avatar.routed", alert))
        self.notifier.run_pass(self.db, current=[alert])
        [sent] = self.alerts("bureau")
        self.assertIn("undeliverable mail", sent["body"])
        self.assertEqual(sent["payload"]["human"], "human:smichea")
        # #7634 : le canal direct ne porte qu'un signal court, sans le texte.
        [(channel, signal)] = self.sender.sent
        self.assertEqual(channel, "app")
        self.assertIn("Ton avatar a besoin de toi", signal.title)
        self.assertNotIn("undeliverable", signal.body)
        entry = self.entry()
        self.assertEqual((entry["status"], entry["avatar"]), ("avatar", "bureau"))
        self.assertEqual(len(self.events("avatar.routed", alert)), before + 1)

    def test_avatar_arrete_replie_apres_le_delai_et_journalise(self):
        alert = self.alert()          # le `since` ne bouge pas d'une passe à l'autre
        self.notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.alerts("bureau")), 1)
        self.db.execute("UPDATE agent_registry SET status = 'stopped'"
                        " WHERE name = 'bureau'")
        before = len(self.events("avatar.fallback", alert))
        with mock.patch.object(notify, "AVATAR_FALLBACK_S", 60.0):
            self.now += 61.0
            self.notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.alerts("bureau")), 1)   # pas de doublon
        # signal court au dépôt, puis texte entier au repli
        self.assertEqual([name for name, _ in self.sender.sent], ["app", "app"])
        self.assertNotIn("undeliverable", self.sender.sent[0][1].body)
        self.assertIn("undeliverable", self.sender.sent[1][1].body)
        self.assertTrue(self.entry()["avatar_fallback"])
        self.assertEqual(len(self.events("avatar.fallback", alert)), before + 1)

    def test_avatar_joignable_garde_l_alerte_dans_sa_boite(self):
        alert = self.alert()
        self.notifier.run_pass(self.db, current=[alert])
        entry = self.entry()
        # l'avatar a remis (lu) son courrier : pas de repli, même longtemps après
        self.assertTrue(mail.mark_delivered(self.db, [entry["avatar_mail_id"]]))
        with mock.patch.object(notify, "AVATAR_FALLBACK_S", 60.0):
            self.now += 3600.0
            self.notifier.run_pass(self.db, current=[alert])
        self.assertEqual([name for name, _ in self.sender.sent], ["app"])  # le signal
        self.assertFalse(self.entry()["avatar_fallback"])

    def test_avatar_externe_joignable_mais_non_remis_replie(self):
        """#7634 (relecture auditeur) : un externe joignable mais non lu."""
        self.db.execute("UPDATE agent_registry SET mode = 'externe'"
                        " WHERE name = 'bureau'")
        alert = self.alert()
        self.notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.alerts("bureau")), 1)
        before = len(self.events("avatar.fallback", alert))
        with mock.patch.object(notify, "AVATAR_FALLBACK_S", 60.0):
            self.now += 61.0
            self.notifier.run_pass(self.db, current=[alert])
        self.assertTrue(self.entry()["avatar_fallback"])
        self.assertEqual(len(self.events("avatar.fallback", alert)), before + 1)
        self.assertIn("undeliverable", self.sender.sent[-1][1].body)

    def test_humain_sans_avatar_garde_la_route_directe(self):
        os.unlink(os.path.join(self.root, "agents", "bureau.md"))
        self.notifier.run_pass(self.db, current=[self.alert()])
        self.assertEqual(self.mails("bureau"), [])
        self.assertEqual([name for name, _ in self.sender.sent], ["app"])

    def test_passe_locale_sans_base_livre_directement_sans_avatar(self):
        """#7634 (relecture cx1) : la passe locale n'a pas de `db`.

        Levée, retry et résolution y restent directes : ni NameError, ni
        tentative d'écriture dans une boîte avatar inaccessible."""
        ncfg = notify.parse_config({
            "default_human": "human:smichea",
            "routes": {"human:smichea": ["app"]},
            "types": ["pgbouncer_pool_waiting"],
        })
        sender = notify.DrySender()
        notifier = notify.Notifier(
            self.cfg, ncfg, notify.empty_state(), sender=sender,
            log=lambda text: None, clock=lambda: self.now)
        alert = {"type": "pgbouncer_pool_waiting", "host": self.cfg.host,
                 "responsible": "human:smichea", "since": self.now - 900,
                 "detail": "pool waiting"}
        with mock.patch.object(pgbouncer_local, "probe", return_value=[alert]), \
                mock.patch.object(migration_alerts, "local",
                                  return_value=([], [])), \
                mock.patch.object(db_sante, "local", return_value=([], [])):
            records = notifier.run_local_pass()
        self.assertEqual([name for name, _ in sender.sent], ["app"])
        self.assertIn("pool waiting", sender.sent[0][1].body)
        self.assertEqual([row["event"] for row in records], ["raised"])
        [entry] = notifier.state["active"].values()
        self.assertNotEqual(entry["status"], "avatar")
        self.assertNotIn("avatar_mail_id", entry)
        self.assertEqual(self.mails("bureau"), [])
        # La console se calme : la résolution locale part aussi, sans base.
        with mock.patch.object(pgbouncer_local, "probe", return_value=[]), \
                mock.patch.object(migration_alerts, "local",
                                  return_value=([], [])), \
                mock.patch.object(db_sante, "local", return_value=([], [])):
            records = notifier.run_local_pass()
        self.assertEqual([name for name, _ in sender.sent], ["app", "app"])
        self.assertEqual([row["event"] for row in records], ["resolved"])
        self.assertEqual(notifier.state["active"], {})
        self.assertEqual(self.mails("bureau"), [])

    def test_passe_locale_sans_base_retente_sans_nameerror(self):
        """#7634 (relecture cx1) : le retry local ne lit pas une base absente."""
        class _FailFirst(notify.DrySender):
            def __init__(self):
                super().__init__()
                self.failed = False

            def send(self, channel, message):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("boom")
                super().send(channel, message)

        ncfg = notify.parse_config({
            "default_human": "human:smichea",
            "routes": {"human:smichea": ["app"]},
            "types": ["pgbouncer_pool_waiting"],
        })
        sender = _FailFirst()
        notifier = notify.Notifier(
            self.cfg, ncfg, notify.empty_state(), sender=sender,
            log=lambda text: None, clock=lambda: self.now)
        alert = {"type": "pgbouncer_pool_waiting", "host": self.cfg.host,
                 "responsible": "human:smichea", "since": self.now - 900,
                 "detail": "pool waiting"}

        def run_local():
            with mock.patch.object(pgbouncer_local, "probe",
                                   return_value=[alert]), \
                    mock.patch.object(migration_alerts, "local",
                                      return_value=([], [])), \
                    mock.patch.object(db_sante, "local", return_value=([], [])):
                return notifier.run_local_pass()

        run_local()
        [key] = list(notifier.state["active"])
        self.assertEqual(notifier.state["active"][key]["status"], "envoi")
        self.assertTrue(notifier._pending(notifier.state["active"][key]))
        run_local()
        self.assertEqual([name for name, _ in sender.sent], ["app"])
        self.assertFalse(notifier._pending(notifier.state["active"][key]))


class DeskReplacementTest(unittest.TestCase):
    """Le libellé de route est réécrit, jamais le titre du lot (#7634)."""

    def test_un_titre_qui_parle_d_avatar_reste_intact(self):
        alert = {"lot": 7634, "level": 3, "step": "blocked_freeze",
                 "blocked_by": "auditeur", "agent": "mty-amh-impl-08",
                 "lot_state": "qa", "value": 1200, "threshold": 600,
                 "title": "Joindre un humain, c'est écrire à son avatar"}
        text = alert_briefs._slo_breach(alert, desk="bureau")
        self.assertIn("écrire à son avatar", text)
        self.assertIn("bureau or auditor", text)
        self.assertNotIn("écrire à son bureau", text)


class CoreHasNoDeskNameTest(unittest.TestCase):
    """Le nom du bureau Manaty ne reste pas dans le cœur (#7634).

    Les notes (docstrings et commentaires) gardent l'histoire et les constats
    cités ; ce qui décide — les chaînes exécutées (messages, rôles, regex,
    destinations) — ne nomme plus aucun avatar.
    """

    def test_aucune_chaine_du_coeur_ne_nomme_le_bureau(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "ameesh"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef)):
                    if ast.get_docstring(node, clean=False):
                        body = node.body[0]
                        docstrings.add((body.lineno, body.col_offset))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and "bureau" in node.value.lower()
                        and (node.lineno, node.col_offset) not in docstrings):
                    offenders.append("%s:%d" % (path.relative_to(root.parent),
                                                node.lineno))
        self.assertEqual(offenders, [])
