# SPDX-License-Identifier: AGPL-3.0-only
"""#4425 : collecteur du bureau (attestations natives, acceptations #4006).

Aucune base et aucun reseau : les sources sont des fichiers de fixture, la
projection d'approbation est un objet normalise comme celui de #4006. Les
preuves reelles se qualifient depuis bureau-goal (test_process_observations_real).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from ameesh import process_observations as po
from ameesh import process_observations_cli as cli

NOW = dt.datetime(2026, 10, 29, 12, 0, 0, tzinfo=dt.timezone.utc)
SHA = "a" * 40
OTHER = "b" * 40
REPO = "manaty/ameesh"
C_REPO = "thepackengers/issue-manager-agent"


def _write(path: str, payload: object) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def actif(path: str, sha: str, date: str, lots, *, hosts=None,
          actifs: dict | None = None, reportes: dict | None = None) -> None:
    hosts = hosts or ["ameesh-agents-1", "ameesh-agents-2",
                      "ameesh-agents-3", "pc-smichea"]
    actifs = actifs or {}
    _write(path, {
        "schema": "ameesh-staging-actif/1",
        "cle": "%s-%dhotes" % (sha[:12], len(hosts)),
        "date": date,
        "sha": sha,
        "hotes": {host: {"actif": actifs.get(host, sha),
                         "etapes": {"sante": "ok"},
                         "sonde": {"message": 1}} for host in hosts},
        "reportes": dict(reportes or {}),
        "non_concluants": {},
        "lots": lots,
        "notifies": {},
        "emissions": {},
    })


def release(path: str, sha: str, completed_at: float, lots, *,
            tag: str = "v1.2.3", project: str = "ameesh",
            actifs: dict | None = None, date: str | None = None) -> None:
    actifs = actifs or {}
    attestation = {
        "schema": "ameesh-staging-actif/1",
        "cle": "%s-4hotes" % sha[:12],
        "sha": sha,
        "date": date or dt.datetime.fromtimestamp(
            completed_at, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hotes": {host: {"actif": actifs.get(host, sha)} for host in (
            "ameesh-agents-1", "ameesh-agents-2",
            "ameesh-agents-3", "pc-smichea")},
    }
    _write(path, {
        "schema": "ameesh-release-cutoff/1",
        "project": project,
        "result": "released",
        "tag": tag,
        "version": tag.lstrip("v"),
        "release_sha": sha,
        "included": lots,
        "completed_at": completed_at,
        "staging": {"exit": 0, "attestation": attestation},
    })


def approval(comment_id: int = 1, *, state: str = "current",
             superseded_by: str = "", revoked_by: str = "",
             expires_at: dt.datetime | None = None,
             body_sha256: str = "c" * 64,
             revision: str = SHA) -> SimpleNamespace:
    return SimpleNamespace(
        id="approval-%d" % comment_id,
        comment_id=comment_id,
        author="smichea",
        created_at=NOW - dt.timedelta(hours=1),
        updated_at=NOW - dt.timedelta(hours=1),
        url="https://github.com/%s/issues/704#issuecomment-%d"
            % (C_REPO, comment_id),
        statement="Owner approval",
        body_sha256=body_sha256,
        artifact=SimpleNamespace(
            repo=C_REPO, revision=revision,
            path="knowledge/architecture/ima-ameesh-process-contract.md"),
        state=state,
        superseded_by=superseded_by,
        revoked_by=revoked_by,
        expires_at=expires_at,
    )


class Bench(unittest.TestCase):
    def setUp(self) -> None:
        # Un umask de service : les dossiers de fixture ne doivent pas etre
        # modifiables par le groupe (le collecteur le refuse).
        self._umask = os.umask(0o022)
        self.addCleanup(os.umask, self._umask)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = self.temporary.name
        # Le journal exige un dossier 0700 ; le parent immediat est prive.
        self.state = os.path.join(self.directory, "observations")
        os.makedirs(self.state, mode=0o700)
        self.journal_path = os.path.join(self.state, "journal.jsonl")
        self.projection_path = os.path.join(self.state, "projection.json")
        self.staging = os.path.join(self.directory, "staging")
        os.makedirs(self.staging, mode=0o700)
        os.makedirs(os.path.join(self.staging, "mise-a-jour"), mode=0o700)

    def source(self, **overrides) -> po.Source:
        values = dict(
            journal=self.journal_path, projection=self.projection_path,
            authority="ima", journal_id="ima",
            repos=(REPO, C_REPO), issues=(),
            staging_state_dir=self.staging, staging_app="ameesh",
            staging_hosts=("ameesh-agents-1", "ameesh-agents-2",
                           "ameesh-agents-3", "pc-smichea"),
            acceptances=False)
        values.update(overrides)
        return po.Source(**values)

    def config(self, **overrides) -> dict:
        section = {
            "authority": "ima",
            "journal": self.journal_path,
            "projection": self.projection_path,
            "max_age_s": 300,
            "source": {
                "journal_id": "ima",
                "repos": [REPO, C_REPO],
                "issues": [],
                "staging": {
                    "state_dir": self.staging, "app": "ameesh",
                    "hosts": ["ameesh-agents-1", "ameesh-agents-2",
                              "ameesh-agents-3", "pc-smichea"],
                },
                "acceptances": False,
                "ttl_s": 300,
            },
        }
        section.update(overrides)
        return {"observations": section}

    def refresh(self, source: po.Source, *, now: dt.datetime = NOW,
                approvals=None) -> po.Projection:
        projection = po.refresh(source, now=now, approvals=approvals)
        written = po.write_projection(projection, source.projection)
        self.assertEqual(written, "written")
        return po.local_projection(self.config(), now=now)

    def entries(self) -> int:
        journal = po.Journal(self.journal_path, create=True)
        journal.reload()
        return len(journal.entries)


class StagingCollectorTest(Bench):
    def test_actif_attestation_feeds_a_deployed_observation(self):
        actif(os.path.join(self.staging, "mise-a-jour",
                           "actif-%s.json" % SHA[:12]),
              SHA, "2026-10-29T10:00:00Z", [7])
        source = self.source()
        projection = self.refresh(source)
        self.assertEqual(len(projection.observations), 1)
        record = projection.observations[0]
        self.assertEqual(record.kind, po.KIND_DEPLOYED)
        self.assertEqual(record.environment, po.ENVIRONMENT_STAGING)
        self.assertEqual(record.work_item_id, 7)
        self.assertEqual(record.app, "ameesh")
        self.assertEqual(record.artifact, SHA)
        self.assertEqual(record.state, po.STATE_CURRENT)
        self.assertEqual(record.provenance["kind"], po.PROVENANCE_STAGING)
        self.assertEqual(record.provenance["ref"],
                         "mise-a-jour/actif-%s.json" % SHA[:12])
        self.assertEqual(record.provenance["hosts"],
                         ["ameesh-agents-1", "ameesh-agents-2",
                          "ameesh-agents-3", "pc-smichea"])
        sources = po.read_sources(projection, work_item_id=7, app="ameesh",
                                  now=NOW)
        self.assertEqual([item["id"] for item in sources], [record.id])
        self.assertEqual(sources[0]["sha"], SHA)
        self.assertEqual(sources[0]["env"], "staging")
        # Le rafraichissement est idempotent : aucune seconde ecriture.
        before = self.entries()
        po.refresh(source, now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(self.entries(), before)

    def test_two_identical_attestations_are_not_an_edit(self):
        """Le chemin fait partie de l'id : un doublon n'est pas une edition."""
        for name in ("actif-a.json", "actif-b.json"):
            actif(os.path.join(self.staging, "mise-a-jour", name),
                  SHA, "2026-10-29T10:00:00Z", [7])
        projection = self.refresh(self.source())
        self.assertEqual(len(projection.observations), 2)
        self.assertEqual(sorted(record.state for record in projection.observations),
                         [po.STATE_CURRENT, po.STATE_SUPERSEDED])
        self.assertNotIn("observation-edited",
                         [refusal["code"] for refusal in projection.refusals])

    def test_next_deployment_supersedes_the_previous_one(self):
        actif(os.path.join(self.staging, "mise-a-jour", "actif-a.json"),
              SHA, "2026-10-29T09:00:00Z", [7])
        release(os.path.join(self.staging, "release", "reports", "r1.json"),
                OTHER, (NOW - dt.timedelta(hours=1)).timestamp(), [7, 8])
        projection = self.refresh(self.source())
        states = {(record.work_item_id, record.artifact): record.state
                  for record in projection.observations}
        self.assertEqual(states[(7, SHA)], po.STATE_SUPERSEDED)
        self.assertEqual(states[(7, OTHER)], po.STATE_CURRENT)
        self.assertEqual(states[(8, OTHER)], po.STATE_CURRENT)
        current = po.read_sources(projection, work_item_id=7, app="ameesh",
                                  now=NOW)
        self.assertEqual([item["sha"] for item in current], [OTHER])
        # Le deploiement suivant ne revoque pas : il remplace, et l'ancien
        # reste lisible dans l'historique de la projection.
        old = projection.by_id(
            "staging-%s-7" % _digest_of_actif(SHA, [7])[:24])
        self.assertIsNotNone(old)
        self.assertEqual(old.state, po.STATE_SUPERSEDED)

    def test_a_late_release_cut_does_not_supersede_a_newer_deployment(self):
        """La date du deploiement est celle de l'attestation, pas de la coupe."""
        actif(os.path.join(self.staging, "mise-a-jour", "actif-a.json"),
              SHA, "2026-10-29T09:00:00Z", [7])
        actif(os.path.join(self.staging, "mise-a-jour", "actif-b.json"),
              OTHER, "2026-10-29T11:00:00Z", [7])
        release(os.path.join(self.staging, "release", "reports", "r1.json"),
                SHA, NOW.timestamp(), [7], date="2026-10-29T09:00:00Z")
        projection = self.refresh(self.source())
        current = po.read_sources(projection, work_item_id=7, app="ameesh",
                                  now=NOW)
        self.assertEqual([item["sha"] for item in current], [OTHER])
        by_artifact = {(record.artifact, record.provenance["kind"]): record.state
                       for record in projection.observations}
        self.assertEqual(
            by_artifact[(SHA, po.PROVENANCE_RELEASE)], po.STATE_SUPERSEDED)
        self.assertEqual(
            by_artifact[(OTHER, po.PROVENANCE_STAGING)], po.STATE_CURRENT)

    def test_invalid_source_files_are_witnessed_never_fatal(self):
        actif(os.path.join(self.staging, "mise-a-jour", "actif-ok.json"),
              SHA, "2026-10-29T10:00:00Z", [7])
        with open(os.path.join(self.staging, "mise-a-jour",
                               "actif-bad.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        _write(os.path.join(self.staging, "mise-a-jour", "actif-wrong.json"),
               {"schema": "other/1"})
        projection = self.refresh(self.source())
        codes = [refusal["code"] for refusal in projection.refusals]
        self.assertIn("source-invalid", codes)
        self.assertEqual(len(projection.observations), 1)
        self.assertEqual(projection.observations[0].artifact, SHA)

    def test_an_attestation_with_too_many_lots_is_refused(self):
        lots = list(range(1, po.MAX_LOTS_PER_ATTESTATION + 2))
        actif(os.path.join(self.staging, "mise-a-jour", "actif-big.json"),
              SHA, "2026-10-29T10:00:00Z", lots)
        projection = self.refresh(self.source())
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_a_group_writable_source_is_refused(self):
        path = os.path.join(self.staging, "mise-a-jour", "actif-open.json")
        actif(path, SHA, "2026-10-29T10:00:00Z", [7])
        os.chmod(path, 0o666)
        projection = self.refresh(self.source())
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_a_group_writable_source_directory_is_refused(self):
        directory = os.path.join(self.staging, "mise-a-jour")
        actif(os.path.join(directory, "actif-a.json"),
              SHA, "2026-10-29T10:00:00Z", [7])
        os.chmod(directory, 0o777)
        with self.assertRaises(po.ReaderError) as raised:
            self.refresh(self.source())
        self.assertEqual(raised.exception.code, "reader-unavailable")

    def test_required_hosts_are_enforced(self):
        actif(os.path.join(self.staging, "mise-a-jour", "actif-partial.json"),
              SHA, "2026-10-29T10:00:00Z", [7], hosts=["ameesh-agents-1"])
        projection = self.refresh(self.source(staging_hosts=(
            "ameesh-agents-1", "pc-smichea")))
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_a_host_active_on_another_commit_is_refused(self):
        """Le nom de l'hote ne suffit pas : son commit actif doit concorder."""
        actif(os.path.join(self.staging, "mise-a-jour", "actif-mismatch.json"),
              SHA, "2026-10-29T10:00:00Z", [7], hosts=["vm"],
              actifs={"vm": OTHER})
        projection = self.refresh(self.source(staging_hosts=("vm",)))
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_empty_host_proof_is_refused(self):
        actif(os.path.join(self.staging, "mise-a-jour", "actif-empty.json"),
              SHA, "2026-10-29T10:00:00Z", [7], hosts=["vm"],
              actifs={"vm": ""})
        projection = self.refresh(self.source(staging_hosts=("vm",)))
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_a_reported_host_needs_a_reason(self):
        actif(os.path.join(self.staging, "mise-a-jour", "actif-report.json"),
              SHA, "2026-10-29T10:00:00Z", [7], hosts=["ameesh-agents-1"],
              reportes={"pc-smichea": {}})
        projection = self.refresh(self.source(
            staging_hosts=("ameesh-agents-1", "pc-smichea"),
            staging_reportable_hosts=("pc-smichea",)))
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_a_reported_host_with_a_reason_is_allowed(self):
        actif(os.path.join(self.staging, "mise-a-jour", "actif-report.json"),
              SHA, "2026-10-29T10:00:00Z", [7], hosts=["ameesh-agents-1"],
              reportes={"pc-smichea": {"raison": "poste eteint",
                                       "mise_a_jour": "a son retour"}})
        projection = self.refresh(self.source(
            staging_hosts=("ameesh-agents-1", "pc-smichea"),
            staging_reportable_hosts=("pc-smichea",)))
        self.assertEqual(len(projection.observations), 1)
        self.assertEqual(projection.observations[0].artifact, SHA)

    def test_a_required_agent_vm_cannot_be_reported(self):
        """Une VM requise ne peut jamais etre satisfaite par un report."""
        actif(os.path.join(self.staging, "mise-a-jour", "actif-agents.json"),
              SHA, "2026-10-29T10:00:00Z", [7],
              hosts=["ameesh-agents-1", "ameesh-agents-3"],
              reportes={"ameesh-agents-2": {"raison": "VM eteinte"}})
        projection = self.refresh(self.source(staging_hosts=(
            "ameesh-agents-1", "ameesh-agents-2", "ameesh-agents-3")))
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_an_active_only_host_cannot_be_reported(self):
        actif(os.path.join(self.staging, "mise-a-jour", "actif-active.json"),
              SHA, "2026-10-29T10:00:00Z", [7],
              hosts=["ameesh-agents-1", "ameesh-agents-2"],
              reportes={"ameesh-agents-3": {"raison": "VM eteinte"}})
        projection = self.refresh(self.source(
            staging_hosts=("ameesh-agents-1", "ameesh-agents-2",
                           "ameesh-agents-3"),
            staging_reportable_hosts=("ameesh-agents-3",),
            staging_active_hosts=("ameesh-agents-3",)))
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_a_reportable_host_can_be_reported(self):
        actif(os.path.join(self.staging, "mise-a-jour", "actif-agents.json"),
              SHA, "2026-10-29T10:00:00Z", [7],
              hosts=["ameesh-agents-1", "ameesh-agents-2"],
              reportes={"pc-smichea": {"raison": "poste eteint"}})
        projection = self.refresh(self.source(
            staging_hosts=("ameesh-agents-1", "ameesh-agents-2", "pc-smichea"),
            staging_reportable_hosts=("pc-smichea",)))
        self.assertEqual(len(projection.observations), 1)

    def test_a_release_report_with_a_mismatched_host_proof_is_refused(self):
        release(os.path.join(self.staging, "release", "reports", "r1.json"),
                OTHER, (NOW - dt.timedelta(hours=1)).timestamp(), [7],
                actifs={"ameesh-agents-1": SHA})
        projection = self.refresh(self.source())
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_a_malformed_completed_at_is_witnessed_not_fatal(self):
        attestation = {
            "schema": "ameesh-staging-actif/1",
            "cle": "%s-4hotes" % OTHER[:12],
            "sha": OTHER,
            "date": "2026-10-29T10:00:00Z",
            "hotes": {host: {"actif": OTHER} for host in (
                "ameesh-agents-1", "ameesh-agents-2",
                "ameesh-agents-3", "pc-smichea")},
        }
        _write(os.path.join(self.staging, "release", "reports", "bad.json"), {
            "schema": "ameesh-release-cutoff/1",
            "project": "ameesh",
            "result": "released",
            "tag": "v1.2.3",
            "version": "1.2.3",
            "release_sha": OTHER,
            "included": [7],
            "completed_at": 1e300,
            "staging": {"exit": 0, "attestation": attestation},
        })
        projection = self.refresh(self.source())
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_source_unavailable_writes_nothing(self):
        source = self.source(staging_state_dir=os.path.join(
            self.directory, "absent"))
        with self.assertRaises(po.ReaderError) as raised:
            po.refresh(source, now=NOW)
        self.assertEqual(raised.exception.code, "reader-unavailable")
        self.assertFalse(os.path.exists(self.journal_path))

    def test_dry_run_does_not_write_the_journal(self):
        actif(os.path.join(self.staging, "mise-a-jour", "actif-a.json"),
              SHA, "2026-10-29T10:00:00Z", [7])
        projection = po.refresh(self.source(), now=NOW, dry_run=True)
        self.assertEqual(len(projection.observations), 1)
        self.assertFalse(os.path.exists(self.journal_path))


class AcceptanceCollectorTest(Bench):
    def source(self, **overrides) -> po.Source:
        values = dict(staging_state_dir="", acceptances=True)
        values.update(overrides)
        return super().source(**values)

    def test_current_acceptance_is_a_global_pass(self):
        projection = self.refresh(
            self.source(), approvals=SimpleNamespace(
                approvals=(approval(1),)))
        self.assertEqual(len(projection.observations), 1)
        record = projection.observations[0]
        self.assertEqual(record.kind, po.KIND_PASS)
        self.assertEqual(record.environment, po.ENVIRONMENT_ACCEPTANCE)
        self.assertIsNone(record.work_item_id)
        self.assertEqual(record.artifact, SHA)
        self.assertEqual(record.provenance["kind"], po.PROVENANCE_GITHUB)
        self.assertEqual(record.provenance["comment_id"], 1)
        sources = po.read_sources(projection, work_item_id=7, app="ameesh",
                                  now=NOW)
        self.assertEqual([item["id"] for item in sources], [record.id])

    def test_source_revocation_supersession_and_disappearance(self):
        source = self.source()
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=(approval(1),)))
        ident = projection.observations[0].id
        # 1. revocation a la source : etat derive, pas une edition.
        projection = self.refresh(
            source, approvals=SimpleNamespace(
                approvals=(approval(1, state="revoked",
                                    revoked_by="revocation-1"),)))
        record = projection.by_id(ident)
        self.assertEqual(record.state, po.STATE_REVOKED)
        self.assertEqual(record.revoked_by, "rev-2")
        self.assertEqual(projection.revocations[0]["reason"],
                         "source-revoked:revocation-1")
        self.assertEqual(po.read_sources(projection, work_item_id=7,
                                         app="ameesh", now=NOW), [])
        with self.assertRaises(po.ObservationRefusal) as raised:
            po.require_observation(projection, ident, work_item_id=7,
                                   app="ameesh", now=NOW)
        self.assertEqual(raised.exception.code, "observation-revoked")

    def test_supersession_is_applied(self):
        source = self.source()
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=(approval(1),)))
        ident = projection.observations[0].id
        projection = self.refresh(
            source, approvals=SimpleNamespace(
                approvals=(approval(1, state="superseded",
                                    superseded_by="approval-2"),)))
        record = projection.by_id(ident)
        self.assertEqual(record.state, po.STATE_SUPERSEDED)
        self.assertEqual(record.superseded_by, "approval-2")

    def test_disappeared_approval_is_revoked(self):
        source = self.source()
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=(approval(1),)))
        ident = projection.observations[0].id
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=()))
        record = projection.by_id(ident)
        self.assertEqual(record.state, po.STATE_REVOKED)
        self.assertEqual(projection.revocations[0]["reason"],
                         "source-disappeared")

    def test_edited_approval_is_revoked_not_rewritten(self):
        source = self.source()
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=(approval(1),)))
        ident = projection.observations[0].id
        projection = self.refresh(
            source, approvals=SimpleNamespace(
                approvals=(approval(1, body_sha256="d" * 64),)))
        record = projection.by_id(ident)
        self.assertEqual(record.state, po.STATE_REVOKED)
        self.assertIn("observation-edited",
                      [refusal["code"] for refusal in projection.refusals])
        self.assertEqual(projection.revocations[0]["reason"], "source-edited")

    def test_expired_acceptance_is_revoked_by_the_next_refresh(self):
        source = self.source()
        expiring = approval(1, expires_at=NOW + dt.timedelta(seconds=1))
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=(expiring,)), now=NOW)
        ident = projection.observations[0].id
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=(expiring,)),
            now=NOW + dt.timedelta(seconds=2))
        record = projection.by_id(ident)
        self.assertEqual(record.state, po.STATE_REVOKED)
        self.assertEqual(projection.revocations[0]["reason"],
                         "acceptance-expired")

    def test_acceptance_expiry_is_enforced_at_read_without_refresh(self):
        source = self.source()
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=(
                approval(1, expires_at=NOW + dt.timedelta(seconds=1)),)))
        ident = projection.observations[0].id
        self.assertEqual(
            [item["id"] for item in po.read_sources(
                projection, work_item_id=7, app="ameesh", now=NOW)],
            [ident])
        later = NOW + dt.timedelta(seconds=2)
        self.assertEqual(po.read_sources(projection, work_item_id=7,
                                         app="ameesh", now=later), [])
        with self.assertRaises(po.ObservationRefusal) as raised:
            po.require_observation(projection, ident, work_item_id=7,
                                   app="ameesh", now=later)
        self.assertEqual(raised.exception.code, "observation-expired")

    def test_an_approval_expiry_change_is_a_source_edit(self):
        source = self.source()
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=(approval(1),)))
        ident = projection.observations[0].id
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=(
                approval(1, expires_at=NOW + dt.timedelta(hours=1)),)))
        record = projection.by_id(ident)
        self.assertEqual(record.state, po.STATE_REVOKED)
        self.assertEqual(projection.revocations[0]["reason"], "source-edited")

    def test_approval_outside_the_identity_is_refused(self):
        other = approval(2, revision=OTHER)
        other.artifact.repo = "other/repo"
        projection = self.refresh(
            self.source(), approvals=SimpleNamespace(approvals=(other,)))
        self.assertEqual(projection.observations, ())
        self.assertIn("source-invalid",
                      [refusal["code"] for refusal in projection.refusals])

    def test_staging_and_acceptances_share_one_projection(self):
        actif(os.path.join(self.staging, "mise-a-jour", "actif-a.json"),
              SHA, "2026-10-29T10:00:00Z", [7])
        source = Bench.source(self, acceptances=True)
        projection = self.refresh(
            source, approvals=SimpleNamespace(approvals=(approval(1),)))
        kinds = sorted(record.kind for record in projection.observations)
        self.assertEqual(kinds, [po.KIND_DEPLOYED, po.KIND_PASS])
        records = po.read_sources(projection, work_item_id=7, app="ameesh",
                                  now=NOW)
        self.assertEqual(sorted(item["kind"] for item in records),
                         [po.KIND_DEPLOYED, po.KIND_PASS])

    def test_disabling_acceptances_with_live_records_is_refused(self):
        source = self.source()
        self.refresh(source, approvals=SimpleNamespace(approvals=(approval(1),)))
        disabled = Bench.source(self)          # staging present, acceptances off
        with self.assertRaises(po.ReaderError) as raised:
            po.refresh(disabled, now=NOW)
        self.assertEqual(raised.exception.code, "reader-config")


class CliTest(Bench):
    def _config_file(self) -> str:
        path = os.path.join(self.directory, "config.json")
        _write(path, self.config())
        return path

    def _run(self, argv) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(argv)
        return code, output.getvalue()

    def test_refresh_check_show_and_revoke(self):
        real_now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        stamp = real_now.strftime("%Y-%m-%dT%H:%M:%SZ")
        actif(os.path.join(self.staging, "mise-a-jour", "actif-a.json"),
              SHA, stamp, [7])
        config = self._config_file()
        code, output = self._run(["refresh", "--config", config, "--json"])
        self.assertEqual(code, 0)
        summary = json.loads(output)
        self.assertEqual(summary["counts"]["current"], 1)
        self.assertEqual(summary["written"], "written")
        code, output = self._run(["check", "--config", config, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["counts"]["observations"], 1)
        code, output = self._run(["show", "--config", config, "--lot", "7",
                                  "--json"])
        self.assertEqual(code, 0)
        shown = json.loads(output)
        self.assertEqual(len(shown["observations"]), 1)
        ident = shown["observations"][0]["id"]
        code, output = self._run(["revoke", "--config", config, "--id", ident,
                                  "--reason", "source invalide", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["counts"]["revoked"], 1)
        code, output = self._run(["show", "--config", config, "--id", ident,
                                  "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["observations"][0]["state"],
                         "revoked")

    def test_unknown_observation_is_a_stable_refusal(self):
        config = self._config_file()
        code, _ = self._run(["refresh", "--config", config, "--json"])
        self.assertEqual(code, 0)
        code, _ = self._run(["show", "--config", config, "--id", "unknown"])
        self.assertEqual(code, 3)


def _digest_of_actif(sha: str, lots) -> str:
    """Le digest de l'attestation, calcule comme le collecteur (test only)."""
    return po._sha256_json({
        "schema": "ameesh-staging-actif/1",
        "cle": "%s-4hotes" % sha[:12],
        "sha": sha,
        "date": "2026-10-29T09:00:00Z",
        "hotes": {host: {"actif": sha, "non_concluant": ""} for host in (
            "ameesh-agents-1", "ameesh-agents-2",
            "ameesh-agents-3", "pc-smichea")},
        "reportes": [],
        "lots": sorted(lots),
        "app": "ameesh",
        "ref": "mise-a-jour/actif-a.json",
    })


if __name__ == "__main__":
    unittest.main()
