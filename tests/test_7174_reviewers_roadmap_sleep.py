# SPDX-License-Identifier: AGPL-3.0-only
"""#7174 — le sommeil « feuille de route épuisée » ne vise pas les relecteurs.

Constat du 13/11 : `file-decoupage` a endormi ameesh-cx1 et ameesh-cx2, deux
relecteurs codex, au titre de la feuille de route épuisée. Le gel de #6961
(P1) a ensuite désigné cx2, endormi ; la demande n'a pas de destinataire et
l'avis P1 a dépassé son SLO. Le siège manuel de cx2 comptait pour couvrir la
famille codex, donc la désignation ne pouvait pas se replier sur codex3.

Témoins :

* le sommeil ne retient pas un agent dont la fiche porte le rôle de
  relecture, même sans relecture due (`file_decoupage.wakeable`) ;
* au gel, un siège manuel sur un relecteur en pause opérateur ne couvre plus :
  un relecteur disponible de la même famille est désigné à sa place, sans
  réveiller le dormeur ;
* sans relecteur disponible de la même famille, la désignation lève le
  sommeil automatique et désigne le relecteur endormi (#5129) ;
* une pause humaine n'est jamais levée par ce chemin : le gel attend et le
  pilote reçoit la notification de capacité.
"""
from __future__ import annotations

import json
import time
import unittest

from ameesh import agent_pause, file_decoupage as fd, registry, storage, work
from ameesh import review_designation as rd
from ameesh.work_reviewers import designated

from .support import PgTestCase
from .test_2447_review_designation_activation import (
    _ActivationFixture, register_agent,
)

SLEEP_NOTE = "roadmap: épuisée ameesh (test)"
LIFT_MARK = "sleep lifted for review:"


def agent(name, *, roles=("implementation",), **extra):
    row = {"name": name, "mode": "execute", "status": "idle", "lease_live": True,
           "lease_owner": "runner:h1", "unread": 0, "assigned_lot_id": None,
           "active_lots": 0, "actionable_lots": 0, "projects": ["ameesh"],
           "responsible": "human:alice", "capabilities": ["read", "propose"],
           "roles": list(roles)}
    row.update(extra)
    return row


class ReviewerRoleWakeableTest(unittest.TestCase):
    """#7174 : le rôle de relecture sort de la capacité « réveillable »."""

    def test_a_reviewer_role_is_never_wakeable(self):
        listing = [agent("dev0"), agent("rev0", roles=("implementation", "relecture")),
                   agent("rev-alias", roles=("review",))]
        item = fd.measure(listing, [], {}, 1000.0, [], configured=False)["ameesh"]
        self.assertEqual(item.wakeable, ["dev0"])
        self.assertEqual(item.idle, ["dev0"])

    def test_the_other_roles_stay_wakeable(self):
        item = fd.measure([agent("dev0", roles=("implementation",))], [], {}, 1000.0,
                          [], configured=False)["ameesh"]
        self.assertEqual(item.wakeable, ["dev0"])
        self.assertEqual(item.idle, ["dev0"])


class PausedManualSeatTest(_ActivationFixture, PgTestCase):
    """Le gel ne demande pas un avis à un relecteur mis en pause."""

    def setUp(self):
        super().setUp()
        # Le lot de découpage ouvert qui a posé le sommeil : la levée y laisse
        # sa trace (#5129).
        self.split = work.add(
            self.db, type="evolution", source=fd.source_of("ameesh"),
            title="Découper la feuille de route de ameesh", app="ameesh",
            actor="pilote")
        work.note(self.db, int(self.split["id"]), SLEEP_NOTE, actor="pilote")
        # Le premier gel désigne r1 (claude) et r2 (codex) automatiquement.
        self.freeze()

    # -- helpers ----------------------------------------------------------
    def add_agent(self, name, harness):
        register_agent(self.db, name, harness=harness, host=self.cfg.host,
                       cwd=self.repo.root, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET team = 'ameesh', capabilities ="
            " ARRAY['read','report-drift','propose'], roles = ARRAY['implementation']"
            " WHERE name = %s", (name,))

    def make_manual(self, name):
        """Le pilote désigne `name` après le gel : le siège redevient manuel."""
        storage.of(self.db).work.designate_reviewer(
            self.lot, name, actor="human:smichea",
            event="reviewer %s designated" % name)

    def asleep(self, name, *, actor=fd.SLEEP_ACTOR, principal="human:alice",
               reason=None):
        agent_pause.change(
            self.db, name, paused=True,
            reason=reason or (fd.SLEEP_REASON % "ameesh"),
            actor=actor, principal=principal, until=time.time() + 3600)

    def refreeze(self, label="tête 2"):
        self.repo.write("src/ameesh/mail.py", label + "\n")
        self.frozen = self.repo.commit(label)
        self.receipts()
        return self.freeze()

    def requested_on_head(self):
        """Les demandes d'avis en attente qui portent la tête gelée courante."""
        head = str(self.frozen or "").casefold()
        requested = {}
        for row in self.mailbox():
            if row["kind"] != "request" or str(row["work_item_id"]) != str(self.lot):
                continue
            payload = row.get("payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            designation = payload.get("review_designation") or {}
            freeze = (payload.get("review_request") or {}).get("freeze") or {}
            sha = str(designation.get("sha") or freeze.get("sha") or "")
            if sha.casefold() == head:
                requested[row["recipient"]] = sha
        return requested

    def split_lifts(self):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE %s ORDER BY id",
            (int(self.split["id"]), LIFT_MARK + "%"))]

    # -- witnesses --------------------------------------------------------
    def test_an_available_same_family_reviewer_replaces_the_paused_seat(self):
        self.add_agent("r3", "codex")
        self.make_manual("r2")
        self.asleep("r2")
        _mark, out = self.refreeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(sorted(row["name"] for row in out["reviewers"]),
                         ["r1", "r3"])
        # r2 n'est pas réveillé : le siège est passé à codex3, disponible.
        self.assertTrue(registry.get(self.db, "r2")["operator_paused"])
        self.assertNotIn("r2", self.requested_on_head())
        self.assertEqual(self.requested_on_head(), {"r1": self.frozen, "r3": self.frozen})
        self.assertEqual(self.split_lifts(), [])

    def test_without_a_same_family_reviewer_the_sleep_is_lifted(self):
        self.make_manual("r2")
        self.asleep("r2")
        _mark, out = self.refreeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(sorted(row["name"] for row in out["reviewers"]),
                         ["r1", "r2"])
        # Le sommeil automatique de r2 est levé et il reçoit sa demande ; la
        # levée est journalisée sur le lot de découpage.
        self.assertFalse(registry.get(self.db, "r2")["operator_paused"])
        self.assertIn("r2", self.requested_on_head())
        lifts = self.split_lifts()
        self.assertEqual(len(lifts), 1)
        self.assertIn("r2", lifts[0])
        self.assertIn("codex", lifts[0])

    def test_a_human_pause_is_never_lifted_and_the_pilot_is_notified(self):
        self.make_manual("r2")
        self.asleep("r2", actor="human:alice", principal="human:alice",
                    reason="maintenance du poste")
        _mark, out = self.refreeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual([row["name"] for row in out["reviewers"]], ["r1"])
        self.assertTrue(registry.get(self.db, "r2")["operator_paused"])
        self.assertNotIn("r2", self.requested_on_head())
        self.assertEqual(self.split_lifts(), [])
        self.assertTrue(any(row["recipient"] == "pilot"
                            and row["kind"] == "notify"
                            for row in self.mailbox()))


if __name__ == "__main__":
    unittest.main()
