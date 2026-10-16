# SPDX-License-Identifier: AGPL-3.0-only
"""#1924 : seam B/C (#1922 process_registry) — l'appelable serveur par garde.

Aucune base : un faux registre remplace `process_registry` et un `SimpleNamespace`
imite la forme publiee du `VerificationContext` (db, work_item, command, spec,
from_stage, to_stage). B reste seul a lire les sources ; C ne lit rien.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from ameesh.process_verifiers import (GUARD_NAMES, context_command,
                                      context_snapshot, install_verifiers,
                                      server_verifier)

REV = "a" * 40
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
OBSERVATION = {"id": "obs-staging-pass", "env": "staging", "sha": REV,
               "kind": "pass"}
RECEIPTS = ({"guard": "deploy_observation", "accepted": True,
             "evidence": {"observation": "obs-staging-pass"}},)
SOURCES = {
    "milestones": ({"kind": "frozen", "sha": REV},
                   {"kind": "merged", "sha": REV}),
    "receipts": RECEIPTS,
    "observations": (OBSERVATION,),
    "decisions": (),
    "deliveries": ({"id": "del-1", "sha": REV, "accepted": True},),
}


class Verification:
    """La forme de `process_registry.Verification` (ok, reason, facts)."""

    def __init__(self, ok, reason="", facts=None):
        self.ok = ok
        self.reason = reason
        self.facts = facts or {}


class Registry:
    Verification = Verification

    def __init__(self):
        self.installed = {}

    def install_verifier(self, name, verifier, *, replace=False):
        current = self.installed.get(name)
        if current is not None and current is not verifier and not replace:
            raise AssertionError("verificateur deja installe : %s" % name)
        self.installed[name] = verifier


def item(**overrides) -> dict:
    values = {"id": 7, "type": "evolution", "title": "lot temoin",
              "state": "build", "assignee": "agent:dev1",
              "reviewer": "agent:reviewer", "loops": 0, "closed_at": None,
              "workflow_ref": "canon@abc:workflow.json",
              "workflow_stage": "work", "workflow_version": 3}
    values.update(overrides)
    return values


def command(**overrides):
    values = dict(work_item_id=7, command_id=COMMAND_ID, guard="start_work",
                  from_stage="work", to_stage="next", evidence={},
                  authority=SimpleNamespace(actor="agent:dev1",
                                            source="runner"))
    values.update(overrides)
    return SimpleNamespace(**values)


def context(**overrides):
    values = dict(db=object(), work_item=item(), command=command(),
                  spec=object(), from_stage="work", to_stage="next",
                  sources=dict(SOURCES))
    values.update(overrides)
    return SimpleNamespace(**values)


class SeamTest(unittest.TestCase):

    def test_install_verifiers_installe_les_14_et_reste_idempotent(self):
        registry = Registry()
        installed = install_verifiers(registry)
        self.assertEqual(installed, GUARD_NAMES)
        self.assertEqual(tuple(registry.installed), GUARD_NAMES)
        first = dict(registry.installed)
        install_verifiers(registry)          # memes objets : aucun ecrasement
        self.assertEqual(registry.installed, first)

    def test_appelable_accepte_sur_preuves(self):
        registry = Registry()
        install_verifiers(registry)
        result = registry.installed["staging_pass"](
            context(command=command(guard="staging_pass",
                                    evidence={"staging": "obs-staging-pass"})))
        self.assertIsInstance(result, Verification)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.facts["code"], "accepted")

    def test_appelable_refuse_sans_source(self):
        registry = Registry()
        install_verifiers(registry)
        result = registry.installed["staging_pass"](
            context(sources={},
                    command=command(guard="staging_pass",
                                    evidence={"staging": "obs-staging-pass"})))
        self.assertIsInstance(result, Verification)
        self.assertFalse(result.ok)
        self.assertEqual(result.facts["code"], "source-unavailable")
        self.assertIn("non collectees", result.reason)

    def test_appelable_ne_rend_jamais_un_booleen(self):
        registry = Registry()
        install_verifiers(registry)
        for guard in GUARD_NAMES:
            with self.subTest(guard=guard):
                result = registry.installed[guard](
                    context(command=command(guard=guard)))
                self.assertIsInstance(result, Verification)

    def test_server_verifier_rend_le_meme_objet(self):
        self.assertIs(server_verifier("planning", Verification),
                      server_verifier("planning", Verification))

    def test_context_command_mappe_evidence_et_autorite(self):
        raw = command(guard="staging_pass", evidence={"staging": "obs-1"})
        mapped = context_command(context(command=raw))
        self.assertEqual(mapped.guard, "staging_pass")
        self.assertEqual(mapped.references, {"staging": "obs-1"})
        self.assertEqual(mapped.actor, "agent:dev1")
        self.assertEqual(mapped.source, "runner")
        self.assertEqual(mapped.from_stage, "work")
        self.assertEqual(mapped.to_stage, "next")

    def test_context_snapshot_derive_revision_et_sources(self):
        snapshot = context_snapshot(context())
        self.assertEqual(snapshot.revision, REV)
        self.assertEqual(snapshot.workflow_ref, "canon@abc:workflow.json")
        self.assertEqual(snapshot.workflow_version, 3)
        self.assertEqual(snapshot.decisions_pending, 0)
        self.assertEqual(snapshot.deliveries,
                         ({"id": "del-1", "sha": REV, "accepted": True},))
        for name in SOURCES:
            self.assertIn(name, snapshot.sources)

    def test_sources_par_lecteur_du_contexte(self):
        without = SimpleNamespace(
            db=object(), work_item=item(), command=command(), spec=object(),
            from_stage="work", to_stage="next",
            read_sources=lambda: {"milestones": ({"kind": "frozen",
                                                  "sha": REV},)})
        snapshot = context_snapshot(without)
        self.assertEqual(snapshot.revision, REV)
        self.assertIn("milestones", snapshot.sources)
        self.assertNotIn("observations", snapshot.sources)

    def test_seam_avec_le_vrai_process_registry(self):
        """Le jour ou B est fusionne, l'appelable rend son Verification reel."""
        try:
            from ameesh import process_registry as pr
        except ImportError:  # pragma: no cover - B fusionne ensuite
            self.skipTest("#1922 (B) pas encore fusionne dans ce worktree")
        raw = pr.Command(work_item_id=7, command_id=COMMAND_ID,
                         expected_version=0,
                         authority=pr.Authority(actor="agent:dev1",
                                                source="runner",
                                                runner_id="runner-1"),
                         workflow_ref="canon@abc:workflow.json",
                         workflow_version=3, action="advance",
                         from_stage="work", to_stage="next",
                         guard="start_work", evidence={})
        real = pr.VerificationContext(db=None, work_item=item(), command=raw,
                                      spec=None, from_stage="work",
                                      to_stage="next")
        result = server_verifier("start_work", pr.Verification)(real)
        self.assertIsInstance(result, pr.Verification)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.facts["code"], "accepted")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
