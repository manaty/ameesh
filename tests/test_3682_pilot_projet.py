# SPDX-License-Identifier: AGPL-3.0-only
"""#3682 — la désignation lit le pilote d'un projet par la règle des briefs.

Constat : un lot `liquichain` n'a aucun orchestrateur qui déclare ce projet au
registre (`mesh-design` déclare `ameesh`, `orchestrateur` déclare `nexlink`).
`_notify` rendait alors « pas de destinataire » et la reprise
`ameesh work reviewers <lot> --apply` levait « name the pilot » au lieu de
déposer l'avis d'attente. La résolution partagée (`alert_briefs.project_pilots`,
#2935) garde les orchestrateurs déclarés quand aucun ne nomme le projet : un
pilote d'un autre projet plutôt que personne, sans valeur codée en dur.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import registry, review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import review_designation_pg as pg
from ameesh import storage, work

from .support import PgTestCase

SHA_A = "a" * 40
FAMILIES = ("claude", "codex", "deepseek")


class PilotResolutionTest(unittest.TestCase):
    """La règle de résolution, sans base : le projet, puis les déclarés."""

    LISTING = [
        {"name": "mesh-design", "projects": ["ameesh"]},
        {"name": "orchestrateur", "projects": ["nexlink"]},
    ]

    def notify(self, project, *, names=("mesh-design", "orchestrateur")):
        with mock.patch("ameesh.sous_utilisation.alert_orchestrators",
                        return_value=list(names)):
            return act._notify(None, None, project, self.LISTING)

    def test_a_project_without_a_declared_pilot_keeps_the_declared_ones(self):
        self.assertEqual(self.notify("liquichain"), "mesh-design")

    def test_a_declared_project_keeps_its_own_pilot(self):
        self.assertEqual(self.notify("ameesh"), "mesh-design")
        self.assertEqual(self.notify("nexlink"), "orchestrateur")

    def test_without_any_declared_orchestrator_nobody_is_named(self):
        self.assertEqual(self.notify("liquichain", names=()), "")


class LiquichainCapacityNoticeTest(PgTestCase):
    """Le témoin du lot : l'avis d'attente part à un destinataire."""

    def setUp(self):
        super().setUp()
        self.item = work.add(self.db, title="Liquichain : attente de relecture",
                             app="liquichain")
        self.lot = int(self.item["id"])
        for name in ("mesh-design", "orchestrateur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host,
                            status="idle")
        storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=SHA_A, actor="me", verdict=None, note="")

    def test_the_waiting_notice_is_addressed_to_the_project_pilot(self):
        listing = registry.overview(self.db)
        with mock.patch("ameesh.sous_utilisation.alert_orchestrators",
                        return_value=["mesh-design", "orchestrateur"]):
            notify = act._notify(None, self.db, "liquichain", listing)
        self.assertEqual(notify, "mesh-design")
        out = rd.apply(
            dict(self.item, state="build"), families=FAMILIES,
            policy=(rd.Rule(paths=("**/*.sql",), reviews=2),),
            paths=["src/ameesh/db.py"], port=pg.PgPort(self.db),
            freeze_sha=SHA_A, authors=[{"name": "author", "family": "deepseek"}],
            candidates=[], risk="sensitive", base="c" * 40, required=2,
            policy_ref="canon@deadbeef", notify=notify)
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual(
            [(row["recipient"], row["kind"]) for row in self.db.query(
                "SELECT recipient, kind FROM agent_mailbox ORDER BY id")],
            [("mesh-design", "notify")])


if __name__ == "__main__":
    unittest.main()
