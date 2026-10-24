# SPDX-License-Identifier: AGPL-3.0-only
"""#3273 — la relance d'inactivité nomme le pilote de l'équipe de l'agent.

Constat (analyse profonde de l'auditeur, 24/10) : `IDLE_PROMPT` écrivait
« agent-mail send orchestrateur » en dur ; les agents ameesh au repos disaient
donc « rien à faire » au pilote Nexlink (36 courriers en 24 h), qui ne peut ni
assigner ni suivre un lot ameesh. La relance nomme désormais le pilote d'un
projet de l'agent (projet primaire d'abord), jamais l'agent lui-même ; sans
pilote connu, elle ne fait écrire personne.
"""
from __future__ import annotations

import time
import types
from unittest import TestCase, mock

from ameesh import adapters, alert_briefs, registry, reprise, runner

from . import test_1270_rate_limit_retry as t1270


def _worker(name, agent=None):
    return types.SimpleNamespace(name=name, agent=agent or {}, db=object(), cfg=None)


class IdlePromptTextTest(TestCase):
    def test_the_prompt_names_the_given_pilot(self):
        text = adapters.idle_prompt("mesh-design")
        self.assertIn("agent-mail send mesh-design,", text)
        self.assertNotIn("orchestrateur", text)

    def test_without_a_pilot_nobody_is_written_to(self):
        for pilot in ("", "  ", None):
            text = adapters.idle_prompt(pilot)
            self.assertNotIn("agent-mail send", text)
            self.assertIn("If you have nothing to do, stop.", text)


class IdlePilotTest(TestCase):
    LISTING = [
        {"name": "ameesh-dev2", "projects": ["ameesh"]},
        {"name": "mty-nxl-impl-01", "projects": ["nexlink"]},
        {"name": "mesh-design", "projects": ["ameesh"]},
        {"name": "orchestrateur", "projects": ["nexlink"]},
        {"name": "both", "projects": ["nexlink", "ameesh"]},
        {"name": "nobody", "projects": []},
    ]
    PILOTS = {"ameesh": ["mesh-design"], "nexlink": ["orchestrateur"]}

    def pilot_of(self, name, listing=None):
        def pilots(project, listing, **_kw):
            return list(self.PILOTS.get(project, []))
        with mock.patch.object(registry, "overview",
                               return_value=listing or self.LISTING), \
                mock.patch.object(alert_briefs, "project_pilots", side_effect=pilots):
            return runner.AgentWorker._idle_pilot(_worker(name))

    def test_an_ameesh_agent_writes_to_the_ameesh_pilot(self):
        self.assertEqual(self.pilot_of("ameesh-dev2"), "mesh-design")

    def test_a_nexlink_agent_keeps_the_nexlink_pilot(self):
        self.assertEqual(self.pilot_of("mty-nxl-impl-01"), "orchestrateur")

    def test_the_primary_project_comes_first(self):
        self.assertEqual(self.pilot_of("both"), "orchestrateur")

    def test_a_pilot_never_writes_to_itself(self):
        # mesh-design est le seul pilote d'ameesh : personne d'autre à prévenir
        self.assertEqual(self.pilot_of("mesh-design"), "")

    def test_an_agent_without_project_writes_to_nobody(self):
        self.assertEqual(self.pilot_of("nobody"), "")

    def test_no_cross_project_fallback_with_the_real_pilot_router(self):
        listing = [{"name": "ameesh-dev2", "projects": ["ameesh"]},
                   {"name": "orchestrateur", "projects": ["nexlink"]}]
        with mock.patch.object(registry, "overview", return_value=listing), \
                mock.patch("ameesh.sous_utilisation.alert_orchestrators",
                           return_value=["orchestrateur"]):
            pilot = runner.AgentWorker._idle_pilot(_worker("ameesh-dev2"))
        self.assertEqual(pilot, "")
        self.assertNotIn("agent-mail send", adapters.idle_prompt(pilot))

    def test_a_secondary_project_is_tried_if_the_primary_has_no_pilot(self):
        listing = [{"name": "both", "projects": ["other", "ameesh"]},
                   {"name": "mesh-design", "projects": ["ameesh"]},
                   {"name": "orchestrateur", "projects": ["nexlink"]}]
        with mock.patch.object(registry, "overview", return_value=listing), \
                mock.patch("ameesh.sous_utilisation.alert_orchestrators",
                           return_value=["orchestrateur", "mesh-design"]):
            self.assertEqual(runner.AgentWorker._idle_pilot(_worker("both")), "mesh-design")

    def test_an_unreadable_registry_writes_to_nobody(self):
        with mock.patch.object(registry, "overview", side_effect=RuntimeError("db down")):
            self.assertEqual(runner.AgentWorker._idle_pilot(_worker("ameesh-dev2")), "")



class IdleSpecTest(t1270.TourDeRepriseTest):
    """Le tour de relance réel (`_pick`) porte le pilote résolu : même worker
    factice que #1270 (gardes neutralisées, rien en attente), relance due."""

    def _idle_worker(self, projects):
        w = self._worker("", time.time() + 60)    # aucune reprise cadencée due
        w.rate_limit, w.rate_limit_streak = "", 0
        w.nudged, w.last_activity = False, time.monotonic() - 10_000
        w.agent, w.cfg = {}, None
        listing = [{"name": w.name, "projects": projects},
                   {"name": "mesh-design", "projects": ["ameesh"]},
                   {"name": "orchestrateur", "projects": ["nexlink"]}]
        return w, listing

    def _idle_pick(self, projects):
        w, listing = self._idle_worker(projects)
        pilots = {"ameesh": ["mesh-design"], "nexlink": ["orchestrateur"]}
        # #3033 : la base factice ne doit aucun avis (sinon « illisible »)
        with mock.patch.object(registry, "overview", return_value=listing), \
                mock.patch.object(alert_briefs, "project_pilots",
                                  side_effect=lambda p, l, **kw: list(pilots.get(p, []))), \
                mock.patch.object(reprise, "pending_review_lines", return_value=[]):
            self.assertTrue(w.idle_due())
            return self._pick(w)

    def test_an_ameesh_agent_is_told_to_write_to_mesh_design(self):
        spec = self._idle_pick(["ameesh"])
        self.assertEqual(spec["kind"], "idle")
        self.assertIn("agent-mail send mesh-design,", spec["prompt"])
        self.assertNotIn("orchestrateur", spec["prompt"])

    def test_an_agent_without_project_is_told_to_write_to_nobody(self):
        spec = self._idle_pick([])
        self.assertEqual(spec["kind"], "idle")
        self.assertEqual(spec["prompt"], adapters.IDLE_PROMPT_NO_PILOT)

    # les cas de #1270 restent ceux de leur propre module
    test_peek_wakes_at_the_retry_deadline = None
    test_peek_does_not_wake_before_the_deadline = None
    test_peek_does_not_wake_on_an_exhausted_quota = None
    test_echeance_passee_lance_un_tour_de_reprise = None
    test_avant_l_echeance_rien = None
    test_un_quota_n_est_pas_repris_sans_fin = None
