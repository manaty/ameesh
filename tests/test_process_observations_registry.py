# SPDX-License-Identifier: AGPL-3.0-only
"""#4425 : couture B/C des observations (lecture fail-closed et garde Process).

Aucune base : le lecteur de B est alimente par une projection construite en
memoire, et C recoit exactement ce que B aurait collecte. La base reelle est
couverte par `tests/test_process_registry.py`.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import process_approvals as approvals_mod
from ameesh import process_observations as po
from ameesh import process_registry as pr
from ameesh import process_verifiers as pv

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
SHA = "a" * 40
COMMAND_ID = "11111111-1111-4111-8111-111111111111"


class FakeDb:
    """Le lecteur B interroge la base : un banc vide suffit a cette couture."""

    def query(self, sql, params=()):
        return []


class Bench(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = self.temporary.name
        self.journal_path = os.path.join(self.directory, "journal.jsonl")
        self.projection_path = os.path.join(self.directory, "projection.json")
        self.source = po.Source(
            journal=self.journal_path, projection=self.projection_path,
            authority="ima", journal_id="ima",
            repos=("manaty/ameesh",), issues=(), staging_app="ameesh")
        self.ident = "staging-deployed-7"
        self.pass_ident = "staging-pass-7"
        self.projection = self._projection(revoked=False)

    def _projection(self, *, revoked: bool) -> po.Projection:
        journal = po.Journal(self.journal_path, create=True)
        journal.reload()
        if not journal.entries:
            journal.record(po.Observation(
                id=self.ident, kind=po.KIND_DEPLOYED,
                environment=po.ENVIRONMENT_STAGING, work_item_id=7,
                app="ameesh", artifact=SHA, source_revision=SHA[:12],
                actor="", observed_at=NOW,
                provenance={"kind": po.PROVENANCE_STAGING,
                            "ref": "actif-a.json", "sha256": "d" * 64,
                            "attestation": SHA[:12],
                            "hosts": ["ameesh-agents-1"]},
                source_digest="d" * 64))
        if revoked:
            journal.revoke(self.ident, reason="source invalide")
        return po.build_projection(self.source, journal, now=NOW,
                                   collector={"host": "bureau-goal"})

    def context(self) -> pr.VerificationContext:
        return pr.VerificationContext(
            db=FakeDb(), work_item={"id": 7, "app": "ameesh"},
            command=SimpleNamespace(work_item_id=7), spec=None,
            from_stage="work", to_stage="next")

    def read(self, projection: po.Projection) -> dict:
        with mock.patch.object(
                approvals_mod, "local_projection",
                side_effect=approvals_mod.ProjectionError(
                    "projection-missing", "bench")), \
                mock.patch.object(po, "local_projection",
                                  return_value=projection):
            return pr.read_sources(self.context())


class RegistrySeamTest(Bench):
    def test_observations_are_read_for_the_lot_and_app(self):
        sources = self.read(self.projection)
        self.assertIn("observations", sources)
        ids = [item["id"] for item in sources["observations"]]
        self.assertIn(self.ident, ids)
        self.assertNotIn(self.pass_ident, ids)   # le journal refuse le QA
        records = {item["id"]: item for item in sources["observations"]}
        self.assertEqual(records[self.ident]["sha"], SHA)
        self.assertEqual(records[self.ident]["env"], "staging")

    def test_absent_projection_removes_the_source(self):
        with mock.patch.object(
                approvals_mod, "local_projection",
                side_effect=approvals_mod.ProjectionError(
                    "projection-missing", "bench")), \
                mock.patch.object(
                    po, "local_projection",
                    side_effect=po.ProjectionError(
                        "projection-missing", "bench")):
            sources = pr.read_sources(self.context())
        self.assertNotIn("observations", sources)

    def test_revoked_observation_is_not_collected(self):
        projection = self._projection(revoked=True)
        sources = self.read(projection)
        self.assertEqual(sources["observations"], [])


class VerifierSeamTest(Bench):
    def _snapshot(self, records, *, sources) -> pv.Snapshot:
        receipts = tuple({
            "accepted": True,
            "guard": "deploy_observation",
            "event_id": index + 1,
            "command_id": COMMAND_ID,
            "workflow_stage": "work",
            "workflow_version": 1,
            "sha256": "c" * 64,
            "evidence": {"schema": "process-receipt/1",
                         "facts": {"observation": record["id"]}},
            "facts": {"observation": record["id"]},
        } for index, record in enumerate(records))
        return pv.Snapshot(
            item={"id": 7, "type": "evolution", "title": "lot",
                  "state": "work", "assignee": "agent:dev",
                  "reviewer": "agent:rev", "loops": 0},
            workflow_ref="canon@test:workflows/ameesh.json",
            workflow_version=1,
            workflow_stage="work", revision=SHA,
            milestones=({"kind": "merged", "sha": SHA},),
            receipts=receipts, observations=tuple(records),
            sources=tuple(sources))

    def _command(self, guard: str) -> pv.Command:
        references = {"staging": self.pass_ident
                      if guard == "staging_pass" else self.ident}
        if guard == "merged_and_staging_deployed":
            references["merge"] = SHA
        return pv.Command(work_item_id=7, command_id=COMMAND_ID, guard=guard,
                          from_stage="work", to_stage="next",
                          references=references)

    def test_staging_guards_accept_the_collected_observation(self):
        sources = self.read(self.projection)
        for guard in ("merged_and_staging_deployed",):
            with self.subTest(guard=guard):
                snapshot = self._snapshot(
                    sources["observations"],
                    sources=("milestones", "receipts", "observations"))
                decision = pv.verify(self._command(guard), snapshot)
                self.assertTrue(decision.accepted, decision)

    def test_missing_source_refuses_source_unavailable(self):
        sources = self.read(self.projection)
        snapshot = self._snapshot(sources["observations"],
                                  sources=("milestones", "receipts"))
        decision = pv.verify(self._command("staging_pass"), snapshot)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "source-unavailable")

    def test_revoked_observation_refuses_evidence_missing(self):
        projection = self._projection(revoked=True)
        sources = self.read(projection)
        snapshot = self._snapshot(sources["observations"],
                                  sources=("milestones", "receipts",
                                           "observations"))
        decision = pv.verify(self._command("staging_pass"), snapshot)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "evidence-missing")

    def test_a_revoked_record_is_refused_even_if_passed_directly(self):
        """Defense en profondeur : C ne fait pas confiance a l'appelant."""
        record = {"id": self.pass_ident, "kind": "pass", "env": "staging",
                  "sha": SHA, "state": "revoked"}
        snapshot = self._snapshot((record,),
                                  sources=("milestones", "receipts",
                                           "observations"))
        decision = pv.verify(self._command("staging_pass"), snapshot)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "observation-result-mismatch")


if __name__ == "__main__":
    unittest.main()
