# SPDX-License-Identifier: AGPL-3.0-only
"""#4134 (E de #2846) : première admission native TPK et réconciliation des versions.

Deux niveaux de preuve, tous deux sur les artefacts réels mais hors dépôt :

* la carte TPK publiée au pin `thepackengers/home@83525af...` compile en
  `process-workflow/1` avec la forme approuvée (14 étapes, 46 arêtes, 7 types,
  `draft` initial, projection `thepackengers/3`) ;
* la projection réelle de #704 (fixture capturée par le bureau) est lue par le
  registre B réel, sur PostgreSQL : sans l'approbation owner de l'artefact
  exact, le transfert est refusé `approval-missing` (témoin déterministe) ;
  avec elle, la première adoption est acceptée avec son reçu et sa preuve.
  Une tête changée, une approbation révoquée et une projection dont
  `artifact_repos` omet `thepackengers/home` sont refusées.

Les fixtures (carte et commentaires #704) sont capturées hors du dépôt, dans le
coffre du bureau ; le test est sauté sans elles. Aucun contenu privé n'est
commité. Pour la qualification officielle, les deux variables sont posées sur
bureau-goal, avec le code 1.26.0/904cefc et le jeton #4005.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
import uuid
from dataclasses import replace
from unittest import mock

from ameesh import process_approvals as pa
from ameesh import process_card
from ameesh import process_registry as pr
from ameesh import process_verifiers as pv

#: les fixtures sont lues AVANT `tests.support`, qui retire de l'environnement
#: toute variable AMEESH_* hors réglages du banc (L142).
CARD_FIXTURE_ENV = "AMEESH_TPK_CARD_FIXTURE"
APPROVALS_FIXTURE_ENV = "AMEESH_APPROVALS_704_FIXTURE"
CARD_FIXTURE = os.environ.get(CARD_FIXTURE_ENV)
APPROVALS_FIXTURE = os.environ.get(APPROVALS_FIXTURE_ENV)

from .support import PgTestCase  # noqa: E402 (import after the capture)
from .test_process_approvals import (APPROVER, ISSUE, REPO, _comment,  # noqa: E402
                                     _issue)
from .test_process_registry import ACME_ACTOR, ACME_EPOCH, ACME_RUNNER  # noqa: E402

UTC = dt.timezone.utc

#: pin publié de la carte TPK (thepackengers/home#253, fusion 2026-10-20)
HOME_REPO = "thepackengers/home"
HOME_REV = "83525afb03d4c70e23bdb70f80d9545c518dc15a"
HOME_PATH = "ameesh/processus/tpk-work-delivery.md"
PIN = "%s@%s:%s" % (HOME_REPO, HOME_REV, HOME_PATH)
OTHER_REV = "b" * 40
OTHER_PIN = "%s@%s:%s" % (HOME_REPO, OTHER_REV, HOME_PATH)

#: empreintes de la carte publiée et de son bloc `workflow:` (contrat 6c771e2)
CARD_SHA256 = "1db5cd93bbd2cb231e7ccfb47dd55f8e7a0872e00c5f767bc431bc2ad01734f0"
WORKFLOW_SHA256 = "4a39217ed5a2a31014a5ecb6bc7917d89c9e6e1208cc22ddbef17aac9c9f0b91"

#: approbation du contrat Process dans #704 (celle qui ne couvre PAS la carte)
CONTRACT_APPROVAL = 6206927953



def _workflow_block(raw: bytes) -> bytes:
    """Le bloc `workflow:` du frontmatter, bornes incluses, sans normalisation."""
    match = re.search(
        rb"^workflow:\n(.*?)\n(?:description:|status:|tags:|generated:|sources:|---)",
        raw, re.S | re.M)
    if match is None:
        raise AssertionError("carte sans bloc workflow: au pin attendu ?")
    return b"workflow:\n" + match.group(1).rstrip()


class TpkCardPinTest(unittest.TestCase):
    """La carte publiée, seule (aucune base, aucun réseau)."""

    def setUp(self) -> None:
        if not CARD_FIXTURE:
            self.skipTest("set %s to the card captured at the published pin"
                          % CARD_FIXTURE_ENV)
        self.path = CARD_FIXTURE

    def test_card_pin_compiles_to_the_approved_shape(self) -> None:
        with open(self.path, "rb") as handle:
            raw = handle.read()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), CARD_SHA256)
        result = process_card.check_page(self.path, ref=PIN, version=1)
        self.assertTrue(result.has_workflow)
        artifact = result.artifact
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.workflow_ref, PIN)
        self.assertEqual(artifact.workflow_version, 1)
        self.assertEqual(artifact.initial, "draft")
        self.assertEqual(len(artifact.stages), 14)
        self.assertEqual(len(artifact.transitions), 46)
        self.assertEqual(len(artifact.types), 7)
        self.assertIn("governance-tracking", artifact.types)
        self.assertEqual(artifact.projection.project, "thepackengers/3")
        self.assertEqual(result.encoding.stage_labels["draft"],
                         "Spécification / draft")
        self.assertEqual(result.encoding.stage_labels["stopped"], "STOP")
        planning = [t for t in artifact.transitions
                    if t.source == "draft" and t.guard == "planning"]
        self.assertEqual({"backlog", "sprint"}, {t.target for t in planning})

    def test_workflow_block_is_the_approved_contract_block(self) -> None:
        with open(self.path, "rb") as handle:
            raw = handle.read()
        self.assertEqual(hashlib.sha256(_workflow_block(raw)).hexdigest(),
                         WORKFLOW_SHA256)


class TpkAdmissionTest(PgTestCase):
    """Le registre B réel, sur PostgreSQL, avec la projection #704 réelle."""

    def setUp(self) -> None:
        super().setUp()
        saved = pr.installed_verifiers()

        def restore() -> None:
            pr.INSTALLED_VERIFIERS.clear()
            pr.INSTALLED_VERIFIERS.update(saved)

        self.addCleanup(restore)
        pr.INSTALLED_VERIFIERS.clear()
        pv.install_verifiers(pr)
        self.db.execute(
            "INSERT INTO agent_registry (name, status, lease_owner, lease_epoch,"
            "                            lease_expires_at)"
            " VALUES (%s, 'idle', %s, %s, now() + interval '1 hour')",
            (ACME_ACTOR, ACME_RUNNER, ACME_EPOCH))
        self.card_path = CARD_FIXTURE
        if not self.card_path:
            self.skipTest("set %s to the card captured at the published pin"
                          % CARD_FIXTURE_ENV)
        self.artifact = process_card.check_page(
            self.card_path, ref=PIN, version=1).artifact
        self.assertIsNotNone(self.artifact)
        self.fixture_path = APPROVALS_FIXTURE
        self.tmp = tempfile.mkdtemp(prefix="t4134-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # -- helpers -----------------------------------------------------------

    def source(self, *, artifact_repos: bool = True) -> pa.Source:
        return pa.Source(
            repo=REPO, issue=ISSUE, approvers=(APPROVER,),
            token_file="/nonexistent/qualification",
            projection="/nonexistent/projection.json",
            artifact_repos=(HOME_REPO,) if artifact_repos else ())

    def build(self, issue, comments, *, now: dt.datetime | None = None,
              artifact_repos: bool = True) -> pa.Projection:
        return pa.build_projection(
            self.source(artifact_repos=artifact_repos), issue, comments,
            now=now or dt.datetime.now(UTC))

    def real(self) -> pa.Projection:
        if not self.fixture_path:
            self.skipTest("set %s to the #704 fixture captured on bureau-goal"
                          % APPROVALS_FIXTURE_ENV)
        with open(self.fixture_path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        return self.build(fixture["issue"], fixture["comments"])

    def publish(self, projection: pa.Projection) -> str:
        path = os.path.join(self.tmp, "projection-%s.json" % uuid.uuid4().hex)
        self.assertEqual(pa.write_projection(projection, path), "written")
        return path

    def lot(self, app: str = "tpk") -> int:
        return int(self.db.query(
            "INSERT INTO work_items (title, state, app)"
            " VALUES ('tpk admission fixture', 'intake', %s) RETURNING id",
            (app,))[0]["id"])

    def events(self, lot: int):
        return self.db.query(
            "SELECT state, workflow_stage, workflow_version, evidence"
            "  FROM work_item_events WHERE work_item_id = %s ORDER BY id", (lot,))

    def ref(self, lot: int):
        row = self.db.query(
            "SELECT workflow_ref FROM work_items WHERE id = %s", (lot,))
        return row[0]["workflow_ref"]

    def transfer(self, lot: int, *, path: str, pin: str = PIN, version: int = 1,
                 spec=None, approval=CONTRACT_APPROVAL, project: str = "tpk",
                 artifact_repos: bool = True):
        source = {"repo": REPO, "issue": ISSUE, "approvers": [APPROVER]}
        if artifact_repos:
            source["artifact_repos"] = [HOME_REPO]
        config = {"approvals": {"projection": path, "source": source}}
        with mock.patch.object(pa.config_mod, "load", return_value=config):
            return pr.transfer_revision(
                self.db, work_item_id=lot, command_id=str(uuid.uuid4()),
                expected_version=0, workflow_ref=pin, workflow_version=version,
                authority=pr.Authority(actor=ACME_ACTOR, source="runner",
                                       epoch=ACME_EPOCH, runner_id=ACME_RUNNER),
                spec=self.artifact if spec is None else spec,
                evidence={"approval": str(approval), "lot": lot,
                          "project": project})

    def refused(self, lot: int, *, path: str, needle: str, pin: str = PIN,
                spec=None, approval=CONTRACT_APPROVAL,
                artifact_repos: bool = True) -> pr.RegistryRefusal:
        with self.assertRaises(pr.RegistryRefusal) as caught:
            self.transfer(lot, path=path, pin=pin, spec=spec,
                          approval=approval, artifact_repos=artifact_repos)
        self.assertEqual(caught.exception.code, "verifier-refused")
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.events(lot), [])
        self.assertIsNone(self.ref(lot))
        return caught.exception

    @staticmethod
    def home(projection: pa.Projection):
        return [a for a in projection.approvals
                if a.artifact.repo == HOME_REPO
                and a.artifact.revision == HOME_REV
                and a.artifact.path == HOME_PATH
                and a.state == "current"]

    @staticmethod
    def home_comment(*, revision: str = HOME_REV,
                     created: str = "2026-10-28T02:00:00Z",
                     ident: int = 91001) -> dict:
        body = ("Owner approval — TPK Process card admission pin\n\n"
                "Admitted artifact: `%s@%s:%s`\n"
                % (HOME_REPO, revision, HOME_PATH))
        return _comment(ident, body, created=created)

    # -- preuves -----------------------------------------------------------

    def test_revision_transfer_installation_receipt(self) -> None:
        receipt = pv.installation_receipt(pr)
        self.assertTrue(receipt["installed"])
        self.assertEqual(receipt["contract"], "revision-transfer/1")
        self.assertEqual(receipt["approval_source"], "github")
        self.assertEqual(receipt["authority"], "ima")
        self.assertEqual(receipt["references"], ["approval", "lot", "project"])
        self.assertEqual(receipt["sources"], ["approvals"])
        self.assertEqual(set(receipt["source_sha256"]),
                         {"ameesh.process_verifiers", "ameesh.process_approvals"})

    def test_refusal_without_pin_approval(self) -> None:
        projection = self.real()
        if self.home(projection):
            self.skipTest("owner pin approval present: the accepted path applies")
        path = self.publish(projection)
        lot = self.lot()
        self.refused(lot, path=path, needle="approval-missing")

    def test_accepted_adoption_with_pin_approval(self) -> None:
        projection = self.real()
        home = self.home(projection)
        if not home:
            self.skipTest("owner pin approval absent: the refusal path applies")
        approval = home[0]
        path = self.publish(projection)
        lot = self.lot()
        receipt = self.transfer(lot, path=path,
                                approval=str(approval.comment_id))
        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.action, "transfer")
        self.assertEqual((receipt.state, receipt.workflow_stage,
                          receipt.workflow_ref, receipt.workflow_version),
                         ("intake", "draft", PIN, 1))
        row = self.db.query(
            "SELECT workflow_ref, workflow_stage, workflow_version"
            "  FROM work_items WHERE id = %s", (lot,))[0]
        self.assertEqual((row["workflow_ref"], row["workflow_stage"],
                          row["workflow_version"]), (PIN, "draft", 1))
        [event] = self.events(lot)
        evidence = pr._as_json(event["evidence"])
        verification = evidence["verification"]
        self.assertEqual(verification["verifier"], "revision_transfer")
        self.assertTrue(verification["ok"])
        proof = verification["facts"]["proof"]
        self.assertEqual(proof["contract"], "revision-transfer/1")
        self.assertEqual(proof["approval_source"], "github")
        self.assertEqual(proof["authority"], "ima")
        self.assertEqual(proof["lot"], lot)
        self.assertEqual(proof["project"], "tpk")
        self.assertEqual(proof["workflow_ref"], PIN)
        self.assertEqual(proof["workflow_version"], 1)
        self.assertEqual(proof["approval"]["comment_id"], approval.comment_id)
        self.assertEqual(proof["approval"]["artifact"]["repo"], HOME_REPO)
        self.assertEqual(proof["approval"]["artifact"]["revision"], HOME_REV)
        self.assertEqual(proof["approval"]["artifact"]["path"], HOME_PATH)
        self.assertEqual(proof["projection"]["digest"], projection.digest)
        self.assertEqual(proof["projection"]["source_digest"],
                         projection.source["source_digest"])
        self.assertEqual(evidence["command"]["previous_ref"], None)
        self.assertRegex(evidence["intent"]["sha256"], r"^[0-9a-f]{64}$")

    def test_head_change_is_refused(self) -> None:
        projection = self.build(_issue(), [self.home_comment()])
        home = self.home(projection)
        self.assertEqual(len(home), 1)
        path = self.publish(projection)
        lot = self.lot()
        spec = replace(self.artifact, workflow_ref=OTHER_PIN)
        self.refused(lot, path=path, needle="approval-stale", pin=OTHER_PIN,
                     spec=spec, approval=str(home[0].comment_id))

    def test_revoked_approval_is_refused(self) -> None:
        approval = self.home_comment(created="2026-10-28T02:00:00Z")
        revocation = _comment(
            91002, "Owner approval revoked — #issuecomment-%d" % approval["id"],
            created="2026-10-28T02:05:00Z")
        projection = self.build(_issue(), [approval, revocation])
        path = self.publish(projection)
        lot = self.lot()
        self.refused(lot, path=path, needle="approval-revoked",
                     approval=str(approval["id"]))

    def test_home_artifact_requires_artifact_repos(self) -> None:
        approval = self.home_comment()
        projection = self.build(_issue(), [approval])
        self.assertEqual(len(self.home(projection)), 1)
        path = self.publish(projection)
        lot = self.lot()
        with self.assertRaises(pr.RegistryRefusal) as caught:
            self.transfer(lot, path=path, approval=str(approval["id"]),
                          artifact_repos=False)
        self.assertEqual(caught.exception.code, "verifier-refused")
        self.assertIn("approvals", str(caught.exception))
        self.assertEqual(self.events(lot), [])
        self.assertIsNone(self.ref(lot))
