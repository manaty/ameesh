# SPDX-License-Identifier: AGPL-3.0-only
"""#1924 : seam B/C (#1922 process_registry) — l'appelable serveur par garde.

Aucune base : un faux registre remplace `process_registry` et un `SimpleNamespace`
imite la forme publiee du `VerificationContext` (db, work_item, command, spec,
from_stage, to_stage). B reste seul a lire les sources ; C ne lit rien.
"""
from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh.process_verifiers import (GUARD_NAMES, INSTALLED_TYPES,
                                      RESERVED_VERIFIERS, available_sources,
                                      capability_report, context_command,
                                      context_snapshot, host_capabilities,
                                      install_verifiers, server_verifier)

REV = "a" * 40
OTHER = "b" * 40
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
OBSERVATION = {"id": "obs-staging-pass", "env": "staging", "sha": REV,
               "kind": "pass"}
RECEIPTS = ({"guard": "deploy_observation", "accepted": True,
             "facts": {"observation": "obs-staging-pass"},
             "evidence": {"schema": "process-receipt/1",
                          "facts": {"observation": "obs-staging-pass"}}},)
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

    def installed_verifiers(self):
        """L'introspection publiee par B (`INSTALLED_VERIFIERS`)."""
        return dict(self.installed)


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
        self.assertEqual(installed, GUARD_NAMES + RESERVED_VERIFIERS)
        self.assertEqual(tuple(registry.installed),
                         GUARD_NAMES + RESERVED_VERIFIERS)
        first = dict(registry.installed)
        install_verifiers(registry)          # memes objets : aucun ecrasement
        self.assertEqual(registry.installed, first)

    def test_capabilities_sondent_le_registre_installe(self):
        registry = Registry()
        names = install_verifiers(registry)
        report = capability_report(registry=registry, sources=SOURCES)
        self.assertEqual(report.executable_guards, names)
        self.assertEqual(tuple(cap.guard for cap in report.reserved),
                         RESERVED_VERIFIERS)
        self.assertTrue(report.capability("hold").reserved)
        without = capability_report(registry=registry, sources={})
        self.assertFalse(without.capability("staging_pass").executable)
        self.assertEqual(without.refusal("staging_pass"), "source-unavailable")
        self.assertEqual(without.refusal("ghost"), "guard-unsupported")

    def test_available_sources_du_contexte(self):
        self.assertEqual(available_sources(context()),
                         ("milestones", "receipts", "observations",
                          "decisions", "deliveries"))
        self.assertEqual(
            available_sources(SimpleNamespace(sources=["milestones"])),
            ("milestones",))

    def test_host_capabilities_garde_la_forme(self):
        report = host_capabilities(sources=SOURCES)
        self.assertEqual(tuple(cap.guard for cap in report.guards),
                         GUARD_NAMES)
        self.assertEqual(tuple(cap.guard for cap in report.reserved),
                         RESERVED_VERIFIERS)
        self.assertEqual(tuple(t.type for t in report.types), INSTALLED_TYPES)

    def test_appelable_reserve_des_retenues(self):
        registry = Registry()
        install_verifiers(registry)
        hold = registry.installed["hold"]
        accepted = hold(context(command=command(action="hold",
                                                hold="blocked")))
        self.assertIsInstance(accepted, Verification)
        self.assertTrue(accepted.ok, accepted.reason)
        self.assertEqual(accepted.facts["stage"], "work")
        without_decision = hold(context(
            sources={"decisions": ()},
            command=command(action="hold", hold="waiting_human")))
        self.assertFalse(without_decision.ok)
        self.assertEqual(without_decision.facts["code"], "hold-no-decision")
        released = hold(context(work_item=item(state="blocked"),
                                command=command(action="release")))
        self.assertTrue(released.ok, released.reason)
        bypass = registry.installed["start_work"](context(
            work_item=item(state="blocked"), command=command(guard="start_work")))
        self.assertFalse(bypass.ok)
        self.assertEqual(bypass.facts["code"], "hold-advance")

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
        self.assertEqual(mapped.action, "advance")
        self.assertEqual(mapped.hold, "")
        held = context_command(context(command=command(action="hold",
                                                       hold="blocked")))
        self.assertEqual(held.action, "hold")
        self.assertEqual(held.hold, "blocked")

    def test_context_snapshot_revision_suit_le_dernier_jalon(self):
        """Un gel ancien ne masque pas une fusion recente (revue Codex B4)."""
        sources = dict(SOURCES)
        sources["milestones"] = ({"kind": "frozen", "sha": OTHER},
                                 {"kind": "merged", "sha": REV})
        snapshot = context_snapshot(context(sources=sources))
        self.assertEqual(snapshot.revision, REV)
        self.assertEqual(snapshot.workflow_stage, "work")

    def test_appelable_staging_pass_apres_fusion_differente(self):
        """La preuve de staging porte sur le commit fusionne, pas sur le gel
        qui l'a precedee (revue Codex B4)."""
        registry = Registry()
        install_verifiers(registry)
        sources = dict(SOURCES)
        sources["milestones"] = ({"kind": "frozen", "sha": OTHER},
                                 {"kind": "merged", "sha": REV})
        result = registry.installed["staging_pass"](
            context(sources=sources,
                    command=command(guard="staging_pass",
                                    evidence={"staging": "obs-staging-pass"})))
        self.assertIsInstance(result, Verification)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.facts["code"], "accepted")

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

    def test_sources_par_read_sources_du_module(self):
        """La convention confirmee par le pilote : process_registry.read_sources
        (context) -> Mapping, lu dans la transaction de B."""
        import ameesh
        module = types.ModuleType("ameesh.process_registry")
        calls = []

        def read_sources(context):
            calls.append(context)
            return {"milestones": ({"kind": "frozen", "sha": REV},)}

        module.read_sources = read_sources
        without = SimpleNamespace(db=object(), work_item=item(),
                                  command=command(), spec=object(),
                                  from_stage="work", to_stage="next")
        # `sys.modules` ET l'attribut du paquet : sans le second, un
        # `process_registry` reel deja importe l'emporterait selon l'ordre des
        # tests (dependance d'environnement).
        with mock.patch.dict(sys.modules,
                             {"ameesh.process_registry": module}):
            with mock.patch.object(ameesh, "process_registry", module,
                                   create=True):
                snapshot = context_snapshot(without)
                names = available_sources(without)
        self.assertEqual(calls, [without, without])
        self.assertEqual(snapshot.revision, REV)
        self.assertIn("milestones", snapshot.sources)
        self.assertEqual(names, ("milestones",))

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
        # banc sans base : le lecteur reel de B est remplace (la lecture des
        # sources reste chez B, dans sa transaction) ; B convertit toute
        # exception d'un verificateur en refus `verifier-error`.
        with mock.patch.object(pr, "read_sources", lambda context: {}):
            result = server_verifier("start_work", pr.Verification)(real)
        self.assertIsInstance(result, pr.Verification)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.facts["code"], "accepted")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
