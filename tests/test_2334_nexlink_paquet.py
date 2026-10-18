# SPDX-License-Identifier: AGPL-3.0-only
"""#2334 — la garde §11 bis active sur la cible Nexlink (`develop`).

#2333 porte la garde générique (`packet_guard`) et la branche sur
`work merge-pr`, `work fire`, la file d'atterrissage (`land`) et `ci run`.
Ces témoins-ci la tiennent sur ce que Nexlink déclare réellement : la cible
`develop` (`radar.projects.nexlink`), une tête de travail (`lot/**`,
`agent/**`, `review/**`), et le paquet `integration/**` qui seul passe sans
exception. L'exception `incident|unblock` est acceptée, journalisée à la tête
exacte (`packet-exception …`) et ne couvre pas une autre tête.

Aucun code de garde n'est dupliqué : les gestes sont ceux de #2333 ; ce lot
active leur cible Nexlink et en pose les témoins (`develop`, jamais `main`).
"""
from __future__ import annotations

import types
import unittest

from ameesh import fire, land, merge_pr, merge_queue, packet_guard, radar, work

from .support import PgTestCase
from .test_1003_merge_pr import _Base as _MergeBase, _git
from .test_l160e_merge_queue import _Base as _QueueBase

A = "a" * 40
B = "b" * 40
BRANCHE = "lot/2334-garde-paquet"
PAQUET = "integration/paquet-h-20261018-0300"


class NexlinkTargetTest(unittest.TestCase):
    """La cible déclarée par Nexlink est une cible d'intégration pour la garde."""

    def test_the_nexlink_project_declares_develop(self):
        settings = radar.parse_settings({"projects": {"nexlink": {
            "repo": "~/work/nexlink", "target": "develop",
            "land": "local", "gates": ["true"]}}})
        project = settings.projects["nexlink"]
        self.assertEqual(project.target, "develop")
        self.assertTrue(packet_guard.is_integration_target(project.target))
        self.assertTrue(packet_guard.is_integration_target("origin/develop"))

    def test_a_lot_head_is_refused_on_develop(self):
        for target in ("develop", "origin/develop", "refs/remotes/origin/develop"):
            with self.subTest(target=target), \
                    self.assertRaises(packet_guard.PacketGuardError) as caught:
                packet_guard.check_merge(None, 2334, BRANCHE, target, A)
            message = str(caught.exception)
            self.assertIn("§11 bis", message)
            self.assertIn("develop", message)
            self.assertIn("integration/**", message)
            self.assertIn("--exception", message)

    def test_a_lot_stacked_on_a_work_branch_is_not_refused(self):
        # un lot empilé sur une autre branche de travail ne déplace pas
        # `develop` : la règle du paquet ne le concerne pas.
        out = packet_guard.check_merge(None, 2334, BRANCHE, "lot/2333-garde-paquet", A)
        self.assertTrue(out["allowed"])
        self.assertIn("not an integration target", out["why"])

    def test_an_empty_target_is_a_doubt_and_refuses(self):
        with self.assertRaises(packet_guard.PacketGuardError):
            packet_guard.check_merge(None, 2334, BRANCHE, "", A)

    def test_agent_and_review_heads_are_refused_on_develop(self):
        for source in ("agent/mty-nxl-impl-01-2334", "review/mty-nxl-impl-01-2334"):
            with self.subTest(source=source), \
                    self.assertRaises(packet_guard.PacketGuardError):
                packet_guard.check_merge(None, 2334, source, "develop", A)

    def test_an_integration_packet_passes_to_develop(self):
        out = packet_guard.check_merge(None, 2334, PAQUET, "develop", A)
        self.assertTrue(out["allowed"])
        self.assertEqual(out["exception"], "")

    def test_ci_lot_is_refused_for_nexlink(self):
        with self.assertRaises(packet_guard.PacketGuardError) as caught:
            packet_guard.check_ci(None, 2334, BRANCHE, mode="lot")
        message = str(caught.exception)
        self.assertIn("§11 bis", message)
        self.assertIn("--full", message)
        full = packet_guard.check_ci(None, 2334, BRANCHE, mode="full")
        self.assertTrue(full["allowed"])
        pending = packet_guard.check_ci(None, 2334, BRANCHE, mode="lot",
                                        exception="incident",
                                        reason="develop rouge 18/10")
        self.assertTrue(pending["allowed"])
        self.assertTrue(pending["pending"], "la note est écrite après le plan")


class NexlinkMergePrTest(_MergeBase):
    """`work merge-pr` refuse une PR de lot vers `develop` avant toute écriture."""

    def setUp(self):
        super().setUp()
        _git(self.origin, "update-ref", "refs/heads/develop", self.base)
        self.lot_target = "develop"

    def test_a_nexlink_lot_pr_is_refused_before_any_write(self):
        gh = self.gh(baseRefName="develop")
        with self.assertRaises(merge_pr.MergePrError) as caught:
            self.merge(gh, target="develop", exception="", reason="")
        message = str(caught.exception)
        self.assertIn("§11 bis", message)
        self.assertIn("develop", message)
        self.assertEqual(len(gh.calls), 1, "le refus précède toute autre lecture")
        self.assertEqual(self.tip("develop"), self.base, "rien n'a été écrit")


class NexlinkQueueAdmissionTest(_QueueBase):
    """`work queue add` refuse une tête de lot vers `develop`, admet le paquet."""

    PACKET_GUARD = False

    def setUp(self):
        super().setUp()
        _git(self.repo, "branch", "develop", "main")

    def project(self):
        return radar.Project(name="nexlink", repo=self.repo, target="develop",
                             github="o/r")

    def test_queue_add_refuses_a_nexlink_lot_head(self):
        self.ready(1)
        with self.assertRaisesRegex(merge_queue.QueueError, "§11 bis"):
            self.enter(1, project=self.project())
        self.assertEqual(self.fake.queue_rows, {}, "aucune ligne de file écrite")

    def test_queue_add_accepts_a_nexlink_packet(self):
        tip = self.tip(1)
        _git(self.repo, "branch", PAQUET, tip)
        self.fake.lots[1]["branch"] = PAQUET
        self.ready(1)
        row = self.enter(1, project=self.project())
        self.assertEqual(row["target"], "develop")
        self.assertEqual(self.fake.queue_rows[1]["target"], "develop")


class NexlinkFireLandTest(PgTestCase):
    """Le feu et la file, sur la cible `develop` de Nexlink, avec Postgres."""

    def _lot(self, branch=BRANCHE, target="develop", sha=A):
        lot = work.add(self.db, title="garde nexlink", branch=branch,
                       branch_target=target)["id"]
        work.milestone(self.db, lot, "frozen", sha=sha, actor="pilote")
        return lot

    def _exceptions(self, lot):
        return [e["note"] for e in work.events(self.db, lot)
                if (e.get("note") or "").startswith("packet-exception")]

    def test_fire_refuses_a_nexlink_lot_without_a_packet(self):
        lot = self._lot()
        with self.assertRaises(fire.FireError) as caught:
            fire.give(self.db, lot, sha=A, actor="pilote")
        self.assertIn("§11 bis", str(caught.exception))
        self.assertFalse(fire.state(self.db, lot)["active"])
        self.assertEqual(self._exceptions(lot), [])

    def test_fire_journals_the_incident_exception_on_develop(self):
        lot = self._lot()
        row = fire.give(self.db, lot, sha=A, actor="pilote",
                        exception="incident", reason="develop rouge 18/10")
        self.assertEqual(row["kind"], "fire")
        self.assertEqual(self._exceptions(lot), [
            "packet-exception incident sha=%s reason=develop rouge 18/10" % A])

    def test_an_integration_packet_fires_on_develop(self):
        lot = self._lot(branch=PAQUET)
        row = fire.give(self.db, lot, sha=A, actor="pilote")
        self.assertEqual(row["kind"], "fire")

    def _landing(self, lot, *, branch=BRANCHE, target="develop", sha=A):
        ejected: dict = {}

        def eject(reason, todo, **extra):
            ejected.update(reason=reason, todo=todo)
            return {"lot": lot, "result": "ejected", "reason": reason, "todo": todo}

        landing = types.SimpleNamespace(
            db=self.db, lot=lot,
            row={"branch": branch, "target": target, "frozen_sha": sha},
            project=types.SimpleNamespace(repo=".", name="nexlink"), eject=eject)
        return landing, ejected

    def test_land_ejects_a_nexlink_queue_lot_before_the_push(self):
        lot = self._lot()
        landing, ejected = self._landing(lot)
        out = land._fire_guard(landing)
        self.assertEqual(out["result"], "ejected")
        self.assertIn("§11 bis", ejected["reason"])
        self.assertIn("queue remove", ejected["todo"])

    def test_land_accepts_an_integration_packet_on_develop(self):
        lot = self._lot(branch=PAQUET)
        landing, _ejected = self._landing(lot, branch=PAQUET)
        self.assertIsNone(land._fire_guard(landing))

    def test_land_accepts_a_declared_exception_on_the_exact_head(self):
        lot = self._lot()
        packet_guard.declare(self.db, lot, "incident", "develop rouge 18/10", A)
        landing, _ejected = self._landing(lot)
        self.assertIsNone(land._fire_guard(landing))
        landing_b, ejected = self._landing(lot, sha=B)
        out = land._fire_guard(landing_b)
        self.assertEqual(out["result"], "ejected")
        self.assertIn("§11 bis", ejected["reason"])
