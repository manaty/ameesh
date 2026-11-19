# SPDX-License-Identifier: AGPL-3.0-only
"""#7749 : un avatar sans destinataire vivant ne prend pas le brief.

Le défaut constaté (#225493/#225494) : le canon déclare ``avatar-essai``
comme avatar de ``human:smichea``, mais cet avatar est en pause budget. Le
chemin des courriers d'alerte posait alors ``desks = ()`` et le brief SLO P1
(``avatar``/``auditeur``) nommait l'avatar absent : plus aucun détenteur du
geste. Le repli est la route configurée de l'humain (``release.projects.*
.report``, le bureau), puis l'auditeur ; il est journalisé une fois par
épisode et le texte nomme la route réellement visée.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import time
import unittest

from ameesh import alert_briefs, fil, notify, registry

from .support import PgTestCase


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _card(title: str, human: str = "smichea") -> str:
    return "\n".join([
        "---", "type: Agent", "title: " + title,
        "responsible: human:" + human,
        "avatar:", "  conversation: portable", "---", "",
    ])


def _role_card(title: str, role: str, human: str = "smichea") -> str:
    """Une fiche Agent de gouvernance : un rôle déclaré, pas un avatar."""
    return "\n".join([
        "---", "type: Agent", "title: " + title,
        "responsible: human:" + human,
        "roles: [" + role + "]", "---", "",
    ])


#: la pause réelle d'`avatar-essai` au moment du constat (17/11, 21:15Z)
BUDGET_PAUSE = ("budget : pause (account tertiaire, seven_day 54 % reset) "
                "until 18/11 06:00@1794981600")


class AvatarBriefFallbackTest(PgTestCase):
    """Le brief d'alerte retombe sur la route humaine configurée."""

    #: un lot par témoin : la clé de brief d'un SLO ne porte ni `since` ni
    #: l'état, et les événements du schéma persistent d'un test à l'autre.
    LOTS = {
        "test_avatar_en_pause_budget_replie_sur_le_bureau_configure": 7741,
        "test_une_route_externe_recoit_sa_copie_au_fil_lisible": 7742,
        "test_une_route_morte_laisse_l_auditeur_detenteur": 7743,
        "test_un_avatar_vivant_garde_la_route_de_l_humain": 7744,
    }

    def setUp(self) -> None:
        super().setUp()
        self.lot = self.LOTS.get(self._testMethodName, 7749)
        self.root = tempfile.mkdtemp(prefix="canon-7749-")
        self.addCleanup(shutil.rmtree, self.root, True)
        _write(os.path.join(self.root, "membres", "smichea.md"),
               "---\ntype: Member\ntitle: smichea\n---\n")
        _write(os.path.join(self.root, "agents", "avatar-essai.md"),
               _card("avatar-essai"))
        # #7661 : l'auditeur se reconnaît à la fiche, pas à son nom ; le canon
        # de test déclare donc son rôle, comme le canon réel (sans avatar).
        _write(os.path.join(self.root, "agents", "auditeur.md"),
               _role_card("auditeur", "auditeur"))
        self.cfg = dataclasses.replace(
            self.cfg, canon=self.root, canon_untrusted=True, canon_ref="",
            release={"projects": {"ameesh": {"report": "bureau"}}})
        self.now = time.time()
        self.ncfg = notify.parse_config({
            "default_human": "human:smichea", "default": [], "types": []})
        for name in ("avatar-essai", "bureau", "pilot", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        # l'avatar déclaré est en pause budget et sans bail : il ne lira pas
        self.db.execute(
            "UPDATE agent_registry SET status = 'blocked', status_text = %s,"
            " lease_owner = NULL, lease_expires_at = NULL"
            " WHERE name = 'avatar-essai'", (BUDGET_PAUSE,))
        self.db.execute(
            "UPDATE agent_registry SET responsible = 'human:smichea',"
            " status = 'idle' WHERE name = 'bureau'")
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner-' || name,"
            " lease_expires_at = now() + interval '1 hour'"
            " WHERE name IN ('bureau', 'pilot', 'auditeur')")
        self.logs: list[str] = []
        self.notifier = notify.Notifier(
            self.cfg, self.ncfg, state=notify.empty_state(), sender=object(),
            log=self.logs.append, clock=lambda: self.now)

    def alert(self) -> dict:
        return {
            "type": "slo_breach", "lot": self.lot, "agent": "pilot",
            "project": "ameesh", "critical": True, "level": 3,
            "step": "blocked_freeze", "targets": ["pilot", "avatar", "auditeur"],
            "title": "Avatar en pause : la route humaine reprend",
            "lot_state": "qa", "value": 9000, "threshold": 3600,
            "since": self.now - 9000, "detail": "P1",
            "orchestrators": ["pilot"],
        }

    def run_briefs(self) -> None:
        self.notifier._brief_agents(
            self.db, [self.alert()], notify.Router(self.cfg, self.db, self.ncfg),
            self.now)

    def mails(self, name: str) -> list:
        return self.db.query(
            "SELECT body, payload FROM agent_mailbox WHERE recipient = %s"
            " ORDER BY id", (name,))

    def fallback_events(self) -> list:
        """Les replis de CET épisode (les événements du schéma persistent
        d'un témoin à l'autre : le lot identifie l'alerte)."""
        return self.db.query(
            "SELECT type FROM ameesh_events WHERE type = 'avatar.brief_fallback'"
            " AND data->>'lot' = %s", (str(self.lot),))

    def test_avatar_en_pause_budget_replie_sur_le_bureau_configure(self):
        self.run_briefs()
        # l'avatar absent ne reçoit rien ; le bureau configuré reçoit le brief
        self.assertEqual(self.mails("avatar-essai"), [])
        [mail] = self.mails("bureau")
        self.assertIn("bureau or auditor", mail["body"])
        self.assertIn("the bureau handles it with the auditor", mail["body"])
        self.assertNotIn("avatar-essai", mail["body"])
        # le pilote et l'auditeur restent destinataires du palier
        self.assertEqual(len(self.mails("auditeur")), 1)
        # le repli est journalisé, une fois pour l'épisode
        self.assertEqual(len(self.fallback_events()), 1)
        self.assertTrue(any("avatar-essai not live (budget paused)" in line
                            and "desk bureau" in line for line in self.logs))
        self.run_briefs()
        self.assertEqual(len(self.mails("bureau")), 1)
        self.assertEqual(len(self.fallback_events()), 1)

    def test_une_route_externe_recoit_sa_copie_au_fil_lisible(self):
        """Le bureau réel est `externe` : sa copie va au fil, jamais en boîte."""
        self.db.execute(
            "UPDATE agent_registry SET mode = 'externe', status = 'idle',"
            " lease_owner = NULL, lease_expires_at = NULL WHERE name = 'bureau'")
        self.run_briefs()
        self.assertEqual(self.mails("bureau"), [])
        [entry] = fil.transport_for(self.cfg).read(fil.ThreadRef("ameesh", self.lot))
        self.assertIn("bureau or auditor", entry.text)
        self.assertIn("the bureau handles it with the auditor", entry.text)
        self.assertNotIn("avatar-essai", entry.text)
        self.assertIn("bureau", entry.recipient)

    def test_une_route_morte_laisse_l_auditeur_detenteur(self):
        """Route configurée sans destinataire vivant : l'auditeur prend le geste."""
        self.db.execute(
            "UPDATE agent_registry SET status = 'stopped', status_text = '',"
            " lease_owner = NULL, lease_expires_at = NULL WHERE name = 'bureau'")
        self.run_briefs()
        [mail] = self.mails("auditeur")
        self.assertIn("No live recipient: bureau (status stopped)", mail["body"])
        self.assertNotIn("avatar-essai", mail["body"])

    def test_un_avatar_vivant_garde_la_route_de_l_humain(self):
        """Non-régression : un avatar joignable reste la route, sans repli."""
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle', status_text = '',"
            " lease_owner = 'runner-avatar', lease_expires_at = now() + interval '1 hour'"
            " WHERE name = 'avatar-essai'")
        self.db.execute("UPDATE agent_registry SET mode = 'externe' WHERE name = 'bureau'")
        self.run_briefs()
        self.assertEqual(self.fallback_events(), [])
        entries = fil.transport_for(self.cfg).read(fil.ThreadRef("ameesh", self.lot))
        self.assertEqual(entries, [])


class DeskNamesTest(unittest.TestCase):
    """Le libellé du geste nomme la route retenue, jamais l'avatar absent."""

    def test_le_texte_slo_nomme_le_bureau_configure(self):
        alert = {"type": "slo_breach", "lot": 7749, "agent": "pilot",
                 "level": 3, "step": "blocked_freeze", "targets": ["avatar", "auditeur"],
                 "critical": True, "title": "un titre", "lot_state": "qa",
                 "value": 9000, "threshold": 3600}
        text = alert_briefs._slo_breach(alert, desk="bureau")
        self.assertIn("bureau or auditor", text)
        self.assertIn("the bureau handles it with the auditor", text)
        self.assertNotIn("the avatar handles it", text)


class EscalationLabelTest(unittest.TestCase):
    def test_escalation_uses_resolved_route_not_stale_target(self):
        alert = {"type": "slo_breach", "lot": 7852, "agent": "worker",
                 "level": 1, "step": "blocked_freeze", "targets": ["assignee"],
                 "critical": True, "title": "test", "lot_state": "qa",
                 "value": 9000, "threshold": 3600}
        rows = {"worker": {"status": "stopped"},
                "avatar-essai": {"status": "stopped"},
                "bureau": {"status": "idle"}, "pilot": {"status": "idle"}}
        out = alert_briefs._slo_briefs(alert, rows, ["pilot"], set(), [], set(),
                                        avatar_names=["bureau"])
        for text in out.values():
            self.assertIn("the bureau handles it with the auditor", text)
            self.assertNotIn("the avatar-essai handles it", text)
        self.assertIn("pilot", out)

    def test_dead_legacy_target_is_not_named_as_handler(self):
        alert = {"type": "slo_breach", "lot": 7852, "agent": "worker",
                 "level": 3, "step": "blocked_freeze", "targets": ["avatar-essai", "auditeur"],
                 "critical": True, "title": "test", "lot_state": "qa",
                 "value": 9000, "threshold": 3600}
        rows = {"avatar-essai": {"status": "stopped"}, "auditeur": {"status": "idle"}}
        out = alert_briefs._slo_briefs(alert, rows, [], set(), ["auditeur"], set())
        self.assertIn("the avatar handles it with the auditor", out["auditeur"])
        self.assertNotIn("the avatar-essai handles it", out["auditeur"])
