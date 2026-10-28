# SPDX-License-Identifier: AGPL-3.0-only
"""#4007 — vérificateur natif `revision_transfer` (A de #2846), adossé au pont #4006.

Contrat `revision-transfer/1` : la révision visée est un pin exact
`owner/dépôt@<sha>:<chemin>`, la projection `approvals` porte une approbation
IMA **courante** de cet artefact exact, la commande la cite et nomme le lot et
le projet de la ligne verrouillée, l'approbateur est indépendant de l'acteur
et de l'assigné. Seuls des codes de refus Process déjà publiés sortent ; le
code du pont reste dans `facts["detail"]`.

Deux niveaux de preuve :

* l'appelable serveur de C (pur, sans base) : acceptation avec preuve et
  chaque refus du lot (approbation absente, périmée, révoquée, remplacée,
  expirée ; projection absente ou périmée ; approbateur non indépendant ;
  autre révision ; mauvais lot ; mauvais projet ; pin mobile) ;
* le registre B réel (`transfer_revision`, API publique, vérificateurs
  installés par `install_verifiers`) : la première adoption d'un lot de test
  est acceptée avec reçu, une approbation révoquée ne laisse aucune trace.

Aucun override : ni source d'approbation ni horloge ne s'injectent ; les
projections sont datées de l'heure réelle. Organisations fictives (`acme`).
"""
from __future__ import annotations

import datetime as dt
import inspect
import os
import shutil
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from ameesh import process_approvals as pa
from ameesh import process_registry as pr
from ameesh import process_verifiers as pv

from .support import PgTestCase
from .test_process_approvals import (APPROVER, AUTHORITY, FULL_A, FULL_B, ISSUE, PATH_A,
                                     PATH_B, REPO, _body, _comment, _issue)
from .test_process_registry import ACME_ACTOR, ACME_EPOCH, ACME_RUNNER, _Spec

UTC = dt.timezone.utc
PIN = "%s@%s:%s" % (REPO, FULL_B, PATH_B)
LOT = 4242
COMMENT = 2001


def _now() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def _source() -> pa.Source:
    return pa.Source(repo=REPO, issue=ISSUE, approvers=(APPROVER,),
                     authority=AUTHORITY, token_file="/nonexistent", projection="/nonexistent")


def _approval(ident: int = COMMENT, *, full: str = FULL_B, path: str = PATH_B,
              created: str = "2026-10-20T00:00:00Z") -> dict:
    return _comment(ident, _body(full, path), created=created)


def _projection(comments, *, now: dt.datetime | None = None) -> pa.Projection:
    return pa.build_projection(_source(), _issue(), comments, now=now or _now())


class _Registry:
    """Registre B minimal : la couture `install_verifier` publiée."""

    Verification = pr.Verification

    def __init__(self) -> None:
        self.installed: dict = {}

    def install_verifier(self, name, verifier, *, replace=False):
        current = self.installed.get(name)
        if current is not None and current is not verifier and not replace:
            raise pr.RegistryRefusal("verifier-name", name)
        self.installed[name] = verifier

    def installed_verifiers(self):
        return dict(self.installed)


def _context(*, approvals=None, evidence=None, ref: str = PIN,
             actor: str = ACME_ACTOR, item=None, workflow_version: int = 1):
    """Le contexte que B passe à l'appelable, sources déjà lues."""
    raw = pr.Command(
        work_item_id=LOT, command_id=str(uuid.uuid4()), expected_version=0,
        authority=pr.Authority(actor=actor, source="runner", epoch=1,
                               runner_id="acme-runner"),
        workflow_ref=ref, workflow_version=workflow_version, action="transfer",
        evidence=({"approval": str(COMMENT), "lot": LOT, "project": "acme"}
                  if evidence is None else evidence))
    sources = {"milestones": [], "receipts": [], "decisions": [],
               "deliveries": []}
    if approvals is not None:
        sources["approvals"] = approvals
    row = {"id": LOT, "state": "intake", "app": "acme", "assignee": "acme-dev2",
           "type": "evolution", "title": "acme : un lot"}
    row.update(item or {})
    return SimpleNamespace(work_item=row, command=raw, sources=sources,
                           spec=None, from_stage="", to_stage="draft")


class TransferVerifierTest(unittest.TestCase):
    """L'appelable serveur `revision_transfer`, sans base."""

    def setUp(self) -> None:
        self.registry = _Registry()
        pv.install_verifiers(self.registry)
        self.check = self.registry.installed[pv.TRANSFER_VERIFIER]
        self.fresh = _projection([_approval()]).as_source()

    def refused(self, context, code: str, detail: str = ""):
        result = self.check(context)
        self.assertIsInstance(result, pr.Verification)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.facts["code"], code, result.reason)
        self.assertIn(code, pv.REFUSAL_CODES)      # aucun code nouveau
        self.assertNotIn("proof", result.facts)
        if detail:
            self.assertEqual(result.facts.get("detail"), detail)
        return result

    # -- installation ------------------------------------------------------

    def test_install_verifiers_installe_revision_transfer(self):
        self.assertEqual(pv.TRANSFER_VERIFIER, pr.TRANSFER_VERIFIER)
        self.assertIn(pv.TRANSFER_VERIFIER, pv.RESERVED_VERIFIERS)
        self.assertIn(pv.TRANSFER_VERIFIER, self.registry.installed)
        names = pv.install_verifiers(self.registry)   # idempotent
        self.assertEqual(names[-1], pv.TRANSFER_VERIFIER)

    def test_recu_d_installation(self):
        receipt = pv.installation_receipt(self.registry)
        self.assertTrue(receipt["installed"])
        self.assertEqual(receipt["verifier"], "revision_transfer")
        self.assertEqual(receipt["contract"], "revision-transfer/1")
        self.assertEqual(receipt["approval_source"], "github")
        self.assertNotIn("authority", receipt)
        self.assertEqual(receipt["sources"], ["approvals"])
        self.assertEqual(set(receipt["source_sha256"]),
                         {"ameesh.process_verifiers", "ameesh.process_approvals"})
        for digest in receipt["source_sha256"].values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        # un appelable étranger sous le nom réservé n'est pas « installé »
        foreign = _Registry()
        foreign.install_verifier(pv.TRANSFER_VERIFIER,
                                 lambda context: pr.Verification(ok=True))
        self.assertFalse(pv.installation_receipt(foreign)["installed"])
        self.assertFalse(pv.installation_receipt(_Registry())["installed"])

    def test_matrice_de_capacite(self):
        names = tuple(self.registry.installed)
        without = pv.capability_report(registry=self.registry,
                                       sources=("milestones",))
        self.assertEqual(without.refusal(pv.TRANSFER_VERIFIER),
                         "source-unavailable")
        cap = without.capability(pv.TRANSFER_VERIFIER)
        self.assertTrue(cap.reserved)
        self.assertEqual(cap.sources, ("approvals",))
        report = pv.capability_report(registry=self.registry,
                                      sources=pv.SOURCE_COLLECTIONS)
        self.assertIn(pv.TRANSFER_VERIFIER, report.executable_guards)
        self.assertEqual(report.refusal(pv.TRANSFER_VERIFIER), "")
        uninstalled = pv.capability_report(registry=_Registry(),
                                           sources=pv.SOURCE_COLLECTIONS)
        self.assertEqual(uninstalled.refusal(pv.TRANSFER_VERIFIER),
                         "verifier-uninstalled")
        self.assertIn(pv.TRANSFER_VERIFIER, names)

    def test_aucun_override_de_source_ni_d_horloge(self):
        # ni `source=` ni `now=` : la source et l'horloge sont celles du module
        for function in (pv.verify, pv.server_verifier, pv.install_verifiers):
            params = inspect.signature(function).parameters
            self.assertNotIn("source", params)
            self.assertNotIn("now", params)
        # une clé cliente qui voudrait choisir la source est refusée
        evidence = {"approval": str(COMMENT), "lot": LOT, "project": "acme",
                    "source": "api"}
        self.refused(_context(approvals=self.fresh, evidence=evidence),
                     "evidence-invalid")

    # -- acceptation -------------------------------------------------------

    def test_premier_transfert_accepte_avec_preuve(self):
        result = self.check(_context(approvals=self.fresh))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.facts["code"], "accepted")
        self.assertEqual(result.facts["stage"], "draft")
        self.assertEqual(result.facts["references"],
                         ["approval", "lot", "project"])
        proof = result.facts["proof"]
        self.assertEqual(proof["contract"], pv.TRANSFER_CONTRACT)
        self.assertEqual(proof["approval_source"], "github")
        self.assertEqual(proof["authority"], AUTHORITY)
        self.assertEqual((proof["lot"], proof["project"]), (LOT, "acme"))
        self.assertEqual(proof["workflow_ref"], PIN)
        self.assertEqual(proof["approval"]["comment_id"], COMMENT)
        self.assertEqual(proof["approval"]["author"], APPROVER)
        self.assertEqual(proof["approval"]["artifact"],
                         {"repo": REPO, "revision": FULL_B, "path": PATH_B})
        self.assertEqual(proof["projection"]["digest"],
                         self.fresh["projection"]["digest"])
        self.assertEqual(proof["projection"]["issue"], ISSUE)

    def test_l_approbation_se_cite_par_id_ou_comment_id(self):
        record = self.fresh["records"][0]
        evidence = {"approval": record["id"], "lot": str(LOT),
                    "project": "acme"}
        self.assertTrue(self.check(_context(approvals=self.fresh,
                                            evidence=evidence)).ok)
        evidence["approval"] = "9999"
        self.refused(_context(approvals=self.fresh, evidence=evidence),
                     "evidence-invalid")

    # -- refus de la source -------------------------------------------------

    def test_projection_absente(self):
        self.refused(_context(approvals=None), "source-unavailable")

    def test_projection_perimee(self):
        stale = _projection([_approval()],
                            now=_now() - dt.timedelta(seconds=600)).as_source()
        self.refused(_context(approvals=stale), "source-unavailable",
                     "projection-stale")

    def test_projection_illisible(self):
        self.refused(_context(approvals={"records": []}), "source-unavailable",
                     "projection-invalid")

    # -- refus de l'approbation ---------------------------------------------

    def test_approbation_absente(self):
        other = _projection([_approval(full=FULL_A, path=PATH_A)]).as_source()
        self.refused(_context(approvals=other), "evidence-missing",
                     "approval-missing")

    def test_approbation_revoquee(self):
        revoked = _projection([
            _approval(),
            _comment(2002, "Owner approval revoked — #issuecomment-%d"
                     % COMMENT, created="2026-10-21T00:00:00Z"),
        ]).as_source()
        self.refused(_context(approvals=revoked), "evidence-invalid",
                     "approval-revoked")

    def test_tete_changee_approbation_d_une_autre_revision(self):
        older = _projection([_approval(full=FULL_A)]).as_source()
        self.refused(_context(approvals=older), "revision-mismatch",
                     "approval-stale")

    def test_approbation_remplacee(self):
        replaced = _projection([
            _approval(),
            _approval(2003, full=FULL_A, created="2026-10-22T00:00:00Z"),
        ]).as_source()
        self.refused(_context(approvals=replaced), "revision-mismatch",
                     "approval-superseded")

    def test_approbation_expiree(self):
        expired = _projection([_approval()]).as_source()
        expired["records"][0]["expires_at"] = "2026-01-01T00:00:00Z"
        self.refused(_context(approvals=expired), "evidence-invalid",
                     "approval-expired")

    def test_autre_revision_visee(self):
        other = "%s@%s:%s" % (REPO, FULL_A, PATH_B)
        self.refused(_context(approvals=self.fresh, ref=other),
                     "revision-mismatch", "approval-stale")

    def test_pin_mobile_ou_abrege(self):
        for ref in ("%s@main:%s" % (REPO, PATH_B),
                    "%s@%s:%s" % (REPO, FULL_B[:7], PATH_B),
                    "acme/process-workflow@0f1e2d3c",
                    "file:/tmp/carte.md"):
            with self.subTest(ref=ref):
                self.refused(_context(approvals=self.fresh, ref=ref),
                             "evidence-invalid")

    # -- liaison au lot, au projet, à l'acteur -------------------------------

    def test_relecteur_non_independant(self):
        self.refused(_context(approvals=self.fresh, actor=APPROVER),
                     "reviewer-not-independent")
        self.refused(_context(approvals=self.fresh,
                              item={"assignee": APPROVER.upper()}),
                     "reviewer-not-independent")

    def test_mauvais_lot(self):
        evidence = {"approval": str(COMMENT), "lot": LOT + 1,
                    "project": "acme"}
        self.refused(_context(approvals=self.fresh, evidence=evidence),
                     "item-mismatch")

    def test_mauvais_projet(self):
        evidence = {"approval": str(COMMENT), "lot": LOT, "project": "other"}
        self.refused(_context(approvals=self.fresh, evidence=evidence),
                     "item-mismatch")
        self.refused(_context(approvals=self.fresh, item={"app": ""}),
                     "item-mismatch")

    def test_references_absentes_ou_booleennes(self):
        self.refused(_context(approvals=self.fresh,
                              evidence={"lot": LOT, "project": "acme"}),
                     "evidence-missing")
        self.refused(_context(approvals=self.fresh,
                              evidence={"approval": True, "lot": LOT,
                                        "project": "acme"}),
                     "command-invalid")

    def test_lot_terminal(self):
        self.refused(_context(approvals=self.fresh, item={"state": "closed"}),
                     "terminal-reopening")

    def test_codes_du_pont_vers_codes_publies(self):
        for code in ("approval-missing", "approval-stale", "approval-superseded",
                     "approval-revoked", "approval-expired",
                     "approval-ambiguous", "approval-invalid",
                     "approval-source", "approval-api-not-authority",
                     "projection-stale", "projection-future",
                     "projection-invalid", "unknown-bridge-code"):
            with self.subTest(code=code):
                self.assertIn(pv.transfer_refusal_code(code), pv.REFUSAL_CODES)


class TransferRegistryTest(PgTestCase):
    """Le registre B réel : `transfer_revision`, API publique, vérificateurs
    installés par `install_verifiers`, projection lue par `read_sources`."""

    def setUp(self) -> None:
        super().setUp()
        saved = pr.installed_verifiers()

        def restore():
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
        self.spec = _Spec(ref=PIN, version=1)
        tmp = tempfile.mkdtemp(prefix="t4007-", dir=os.environ.get("TMPDIR"))
        self.projection_path = os.path.join(tmp, "projection.json")
        self.addCleanup(shutil.rmtree, tmp, True)

    def lot(self) -> int:
        return int(self.db.query(
            "INSERT INTO work_items (title, state, app)"
            " VALUES ('acme : un lot', 'intake', 'acme') RETURNING id")[0]["id"])

    def publish(self, comments) -> None:
        status = pa.write_projection(_projection(comments), self.projection_path)
        self.assertEqual(status, "written")

    def transfer(self, lot: int):
        config = {"approvals": {
            "authority": AUTHORITY, "projection": self.projection_path,
            "source": {"repo": REPO, "issue": ISSUE, "approvers": [APPROVER]}}}
        with mock.patch.object(pa.config_mod, "load", return_value=config):
            return pr.transfer_revision(
                self.db, work_item_id=lot, command_id=str(uuid.uuid4()),
                expected_version=0, workflow_ref=PIN, workflow_version=1,
                authority=pr.Authority(actor=ACME_ACTOR, source="runner",
                                       epoch=ACME_EPOCH, runner_id=ACME_RUNNER),
                spec=self.spec,
                evidence={"approval": str(COMMENT), "lot": lot,
                          "project": "acme"})

    def events(self, lot: int):
        return self.db.query(
            "SELECT state, workflow_stage, workflow_version, evidence"
            "  FROM work_item_events WHERE work_item_id = %s ORDER BY id", (lot,))

    def test_premier_transfert_d_un_lot_de_test_accepte_avec_recu(self):
        self.assertTrue(pv.installation_receipt(pr)["installed"])
        self.publish([_approval()])
        lot = self.lot()
        receipt = self.transfer(lot)
        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.action, "transfer")
        self.assertEqual((receipt.state, receipt.workflow_stage,
                          receipt.workflow_ref, receipt.workflow_version),
                         ("intake", "draft", PIN, 1))
        row = self.db.query("SELECT workflow_ref, workflow_stage,"
                            " workflow_version FROM work_items WHERE id = %s",
                            (lot,))[0]
        self.assertEqual((row["workflow_ref"], row["workflow_stage"],
                          row["workflow_version"]), (PIN, "draft", 1))
        [event] = self.events(lot)
        evidence = pr._as_json(event["evidence"])
        verification = evidence["verification"]
        self.assertEqual(verification["verifier"], "revision_transfer")
        self.assertTrue(verification["ok"])
        proof = verification["facts"]["proof"]
        self.assertEqual(proof["contract"], "revision-transfer/1")
        self.assertEqual(proof["lot"], lot)
        self.assertEqual(proof["approval"]["comment_id"], COMMENT)
        self.assertEqual(proof["approval"]["artifact"]["revision"], FULL_B)
        self.assertEqual(evidence["command"]["previous_ref"], None)

    def test_approbation_revoquee_ne_laisse_aucune_trace(self):
        self.publish([
            _approval(),
            _comment(2002, "Owner approval revoked — #issuecomment-%d"
                     % COMMENT, created="2026-10-21T00:00:00Z"),
        ])
        lot = self.lot()
        with self.assertRaises(pr.RegistryRefusal) as caught:
            self.transfer(lot)
        self.assertEqual(caught.exception.code, "verifier-refused")
        self.assertIn("approval-revoked", str(caught.exception))
        self.assertEqual(self.events(lot), [])
        row = self.db.query("SELECT workflow_ref FROM work_items WHERE id = %s",
                            (lot,))[0]
        self.assertIsNone(row["workflow_ref"])

    def test_sans_projection_le_transfert_refuse(self):
        lot = self.lot()                           # aucun fichier publié
        with self.assertRaises(pr.RegistryRefusal) as caught:
            self.transfer(lot)
        self.assertEqual(caught.exception.code, "verifier-refused")
        self.assertEqual(self.events(lot), [])


if __name__ == "__main__":
    unittest.main()
