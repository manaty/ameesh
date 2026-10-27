# SPDX-License-Identifier: AGPL-3.0-only
"""#4006 — pont d'approbation IMA/GitHub, projection locale fail-closed.

Aucune base, aucun reseau dans le coeur : les commentaires #704 sont des
fixtures, le transport GitHub est injecte. Les preuves couvrent les temoins de
refus demandes par le lot : approbation absente, revoquee, perimee (tete
changee), projection perimee, lecteur indisponible. Le test de qualification
sur les vrais enregistrements #704 est separe
(`test_process_approvals_704.py`) et ne tourne que sur bureau-goal.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from ameesh import process_approvals as pa
from ameesh import process_approvals_cli as cli
from ameesh import process_registry as pr
from ameesh import process_verifiers as pv

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
REPO = "thepackengers/issue-manager-agent"
ISSUE = 704
APPROVER = "smichea"
FULL_A = "d918149d9d301f0ba7a2aff80e196e8e3b1e30ac"
FULL_B = "6c771e2e16bc04e8ce3b04a55df67ad4aca77cb8"
PATH_A = "knowledge/architecture/ima-ameesh-work-contract.md"
PATH_B = "knowledge/architecture/ima-ameesh-process-contract.md"
TOKEN = "approval-test-" + "x" * 32


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue(state: str = "open",
           updated: str = "2026-10-26T07:35:18Z") -> dict:
    return {"number": ISSUE, "state": state, "updated_at": updated}


def _comment(ident: int, body: str, *, login: str = APPROVER,
             kind: str = "User", created: str = "2026-10-16T01:56:00Z",
             updated: str | None = None, url: str | None = None) -> dict:
    return {
        "id": ident, "body": body,
        "user": {"login": login, "type": kind},
        "created_at": created, "updated_at": updated or created,
        "html_url": url or "https://github.com/%s/issues/%d#issuecomment-%d"
        % (REPO, ISSUE, ident),
    }


def _body(full: str, path: str, *, ref: str | None = None,
          repo: str = REPO,
          marker: str = "Owner approval recorded \u2014 16 October 2026") -> str:
    return ("%s\n\nS\u00e9bastien approved the exact artifact at %s.\n\n"
            "https://github.com/%s/blob/%s/%s\n"
            % (marker, full, repo, ref or full[:7], path))


class ApprovalCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="approvals-")
        self.addCleanup(self._cleanup, self.tmp)
        self.token_file = os.path.join(self.tmp, "token")
        with open(self.token_file, "w", encoding="ascii") as handle:
            handle.write(TOKEN + "\n")
        os.chmod(self.token_file, 0o600)
        self.projection_path = os.path.join(self.tmp, "projection.json")
        self.identity = pa.ApprovalIdentity(
            repo=REPO, issue=ISSUE, approvers=(APPROVER,))

    @staticmethod
    def _cleanup(path: str) -> None:
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(root, name))
            for name in dirs:
                with contextlib.suppress(OSError):
                    os.rmdir(os.path.join(root, name))
        with contextlib.suppress(OSError):
            os.rmdir(path)

    def load(self, path: str | None = None, *, now: dt.datetime = NOW,
             **kwargs):
        identity = kwargs.pop("identity", self.identity)
        return pa.load_projection(path=path or self.projection_path,
                                  now=now, identity=identity, **kwargs)

    def source(self, **overrides) -> pa.Source:
        values = {
            "repo": REPO, "issue": ISSUE, "approvers": (APPROVER,),
            "token_file": self.token_file,
            "projection": self.projection_path,
        }
        values.update(overrides)
        return pa.Source(**values)

    def build(self, comments: list[dict], *, source: pa.Source | None = None,
              issue: dict | None = None, now: dt.datetime = NOW,
              resolve=None) -> pa.Projection:
        return pa.build_projection(
            source or self.source(), issue or _issue(), comments,
            now=now, resolve=resolve)

    def write(self, projection: pa.Projection, path: str | None = None) -> str:
        target = path or self.projection_path
        status = pa.write_projection(projection, target)
        self.assertEqual(status, "written")
        return target

    # -- normalisation -----------------------------------------------------

    def test_legacy_approval_reads_full_revision_and_scope(self):
        projection = self.build([_comment(1001, _body(FULL_A, PATH_A))])
        self.assertEqual(projection.refusals, ())
        self.assertEqual(len(projection.approvals), 1)
        approval = projection.approvals[0]
        self.assertEqual(approval.author, APPROVER)
        self.assertEqual(approval.artifact.repo, REPO)
        self.assertEqual(approval.artifact.revision, FULL_A)
        self.assertEqual(approval.artifact.path, PATH_A)
        self.assertEqual(approval.state, "current")
        got = pa.require_approval(
            projection, repo=REPO, revision=FULL_A, path=PATH_A, now=NOW)
        self.assertEqual(got.id, approval.id)

    def test_short_revision_is_resolved_from_the_body(self):
        body = _body(FULL_B, PATH_B, ref=FULL_B[:7])
        projection = self.build([_comment(1002, body)])
        self.assertEqual(projection.approvals[0].artifact.revision, FULL_B)

    def test_short_revision_needs_an_exact_resolution(self):
        short = FULL_A[:7]
        body = ("Owner approval \u2014 16 October 2026\n\n"
                "https://github.com/%s/blob/%s/%s\n" % (REPO, short, PATH_A))
        calls = []

        def resolve(repo: str, ref: str) -> str:
            calls.append((repo, ref))
            return FULL_A

        projection = self.build([_comment(1003, body)], resolve=resolve)
        self.assertEqual(calls[0], (REPO, short))
        # Le SHA complet rendu est relu a la forge avant d'etre accepte.
        self.assertEqual(calls[1], (REPO, FULL_A))
        self.assertEqual(projection.approvals[0].artifact.revision, FULL_A)

    def test_a_resolved_sha_must_start_with_the_requested_ref(self):
        body = ("Owner approval \u2014 16 October 2026\n\n"
                "https://github.com/%s/blob/abc1234/%s\n" % (REPO, PATH_A))
        projection = self.build(
            [_comment(1004, body)],
            resolve=lambda repo, ref: FULL_A)
        self.assertEqual(projection.approvals, ())
        self.assertIn("revision-unresolved",
                      {item["code"] for item in projection.refusals})

    def test_a_pin_resolved_by_a_branch_cannot_forge_a_commit(self):
        source = self.source(artifact_repos=("target/repo",))
        body = ("Owner approval \u2014 16 October 2026\n\n"
                "target/repo@abc1234:docs/a.md\n")
        projection = self.build(
            [_comment(1005, body)],
            source=source, resolve=lambda repo, ref: "d" * 40)
        self.assertEqual(projection.approvals, ())
        self.assertIn("revision-unresolved",
                      {item["code"] for item in projection.refusals})

    def test_unresolved_short_revision_is_refused(self):
        body = ("Owner approval \u2014 16 October 2026\n\n"
                "https://github.com/%s/blob/abc1234/%s\n" % (REPO, PATH_A))
        projection = self.build([_comment(1004, body)])
        self.assertEqual(projection.approvals, ())
        codes = {item["code"] for item in projection.refusals}
        self.assertIn("revision-unresolved", codes)
        self.assertIn("approval-no-artifact", codes)

    def test_full_revision_is_rechecked_when_a_resolver_exists(self):
        body = _body(FULL_A, PATH_A, ref=FULL_A)

        def resolve(repo: str, ref: str) -> str:
            return "c" * 40

        projection = self.build([_comment(1005, body)], resolve=resolve)
        self.assertEqual(projection.approvals, ())
        self.assertIn("revision-unresolved",
                      {item["code"] for item in projection.refusals})

    def test_mutable_revision_is_refused(self):
        body = ("Owner approval \u2014 16 October 2026\n\n"
                "https://github.com/%s/blob/main/%s\n" % (REPO, PATH_A))
        projection = self.build([_comment(1006, body)])
        self.assertEqual(projection.approvals, ())
        self.assertIn("mutable-revision",
                      {item["code"] for item in projection.refusals})

    def test_path_traversal_is_refused(self):
        body = ("Owner approval \u2014 16 October 2026\n\n"
                "https://github.com/%s/blob/%s/../etc/passwd\n"
                % (REPO, FULL_A[:7]))
        projection = self.build([_comment(1007, body)])
        self.assertEqual(projection.approvals, ())
        self.assertIn("path-invalid",
                      {item["code"] for item in projection.refusals})

    def test_out_of_scope_repository_is_refused(self):
        body = _body(FULL_A, PATH_A, repo="other/repository")
        projection = self.build([_comment(1008, body)])
        self.assertEqual(projection.approvals, ())
        self.assertIn("out-of-scope",
                      {item["code"] for item in projection.refusals})

    def test_bot_and_non_approver_are_witnessed_not_accepted(self):
        projection = self.build([
            _comment(1009, _body(FULL_A, PATH_A), login="ima-bot", kind="Bot"),
            _comment(1010, _body(FULL_B, PATH_B), login="someone"),
        ])
        self.assertEqual(projection.approvals, ())
        codes = {item["code"] for item in projection.refusals}
        self.assertEqual(codes, {"author-bot", "author-not-approver"})

    def test_a_status_comment_is_ignored_silently(self):
        projection = self.build([
            _comment(1011, "Migration checkpoint \u2014 16 October 2026"),
        ])
        self.assertEqual(projection.approvals, ())
        self.assertEqual(projection.refusals, ())

    def test_an_edited_approval_is_refused(self):
        projection = self.build([
            _comment(1012, _body(FULL_A, PATH_A),
                     updated="2026-10-17T00:00:00Z"),
        ])
        self.assertEqual(projection.approvals, ())
        self.assertIn("approval-edited",
                      {item["code"] for item in projection.refusals})

    def test_an_edited_revocation_still_revokes(self):
        approval = _comment(1030, _body(FULL_A, PATH_A))
        revocation = _comment(
            1031, "Owner approval revoked \u2014 #issuecomment-1030",
            created="2026-10-18T00:00:00Z", updated="2026-10-18T00:05:00Z")
        projection = self.build([approval, revocation])
        self.assertEqual(projection.approvals[0].state, "revoked")
        self.write(projection)
        loaded = self.load()
        self.assertEqual(loaded.approvals[0].state, "revoked")

    def test_an_edited_replacement_approval_fences_the_old_one(self):
        first = _comment(1032, _body(FULL_A, PATH_A))
        replacement = _comment(
            1033, _body(FULL_B, PATH_A), created="2026-10-17T00:00:00Z",
            updated="2026-10-17T00:05:00Z")
        projection = self.build([first, replacement])
        self.assertEqual(projection.current(), ())
        self.assertEqual(projection.approvals[0].state, "revoked")
        self.assertEqual(projection.approvals[0].revoked_by, "edited:1033")
        self.assertIn({"comment": 1033, "code": "approval-edited"},
                      [dict(item) for item in projection.refusals])
        self.write(projection)
        loaded = self.load()
        self.assertEqual(loaded.current(), ())
        with self.assertRaises(pa.ApprovalRefusal) as caught:
            pa.require_approval(loaded, repo=REPO, revision=FULL_A,
                                path=PATH_A, now=NOW)
        self.assertEqual(caught.exception.code, "approval-revoked")

    def test_an_unscoped_edited_approval_refuses_the_projection(self):
        edited = _comment(
            1034, "Owner approval recorded \u2014 no artifact reference",
            created="2026-10-17T00:00:00Z",
            updated="2026-10-17T00:05:00Z")
        with self.assertRaises(pa.ProjectionError) as caught:
            self.build([edited])
        self.assertEqual(caught.exception.code, "projection-invalid")

    def test_a_revocation_naming_a_branch_still_revokes_the_scope(self):
        approval = _comment(1035, _body(FULL_A, PATH_A))
        revocation = _comment(
            1036, "Approval revoked\n\n"
                  "https://github.com/%s/blob/main/%s\n" % (REPO, PATH_A),
            created="2026-10-18T00:00:00Z")
        projection = self.build([approval, revocation])
        self.assertEqual(projection.approvals[0].state, "revoked")

    def test_revocation_targets_and_scope_add_up(self):
        first = _comment(1037, _body(FULL_A, PATH_A))
        second = _comment(1038, _body(FULL_B, PATH_B))
        revocation = _comment(
            1039, "Owner approval revoked \u2014 context #issuecomment-1037\n\n"
                  "https://github.com/%s/blob/%s/%s\n"
                  % (REPO, FULL_B, PATH_B),
            created="2026-10-18T00:00:00Z")
        projection = self.build([first, second, revocation])
        self.assertEqual([a.state for a in projection.approvals],
                         ["revoked", "revoked"])

    def test_an_approval_request_is_not_an_approval(self):
        for marker in ("Owner approval requested for this contract",
                       "Owner approval needed", "Owner approval pending"):
            projection = self.build([_comment(
                1040, _body(FULL_A, PATH_A, marker=marker))])
            self.assertEqual(projection.approvals, (), marker)

    def test_an_unknown_commit_refuses_only_its_record(self):
        class Transport:
            def get(self, path):
                if path.endswith("/" + "0" * 40):
                    return 422, {}, b"{}"
                return 200, {}, json.dumps({"sha": FULL_A}).encode()

        resolve = pa._commit_resolver(Transport(), float("inf"))
        projection = self.build([
            _comment(1041, _body(FULL_A, PATH_A, ref=FULL_A)),
            _comment(1042, _body("0" * 40, PATH_B, ref="0" * 40))],
            resolve=resolve)
        self.assertEqual([a.comment_id for a in projection.approvals], [1041])
        self.assertIn({"comment": 1042, "code": "revision-unresolved"},
                      [dict(item) for item in projection.refusals])

    def test_supersession_is_per_artifact_path(self):
        first = _comment(1013, _body(FULL_A, PATH_A))
        second = _comment(1014, _body(FULL_B, PATH_A),
                          created="2026-10-17T00:00:00Z")
        projection = self.build([first, second])
        by_id = {item.id: item for item in projection.approvals}
        old = [item for item in projection.approvals
               if item.artifact.revision == FULL_A][0]
        new = [item for item in projection.approvals
               if item.artifact.revision == FULL_B][0]
        self.assertEqual(old.state, "superseded")
        self.assertEqual(old.superseded_by, new.id)
        self.assertEqual(new.state, "current")
        self.assertEqual(len(by_id), 2)
        with self.assertRaises(pa.ApprovalRefusal) as caught:
            pa.require_approval(projection, repo=REPO, revision=FULL_A,
                                path=PATH_A, now=NOW)
        self.assertEqual(caught.exception.code, "approval-superseded")

    def test_revocation_by_comment_id(self):
        approval = _comment(1015, _body(FULL_A, PATH_A))
        revocation = _comment(
            1016, "Owner approval revoked \u2014 #issuecomment-1015",
            created="2026-10-18T00:00:00Z")
        projection = self.build([approval, revocation])
        record = projection.approvals[0]
        self.assertEqual(record.state, "revoked")
        self.assertEqual(record.revoked_by, "revocation:1016")
        with self.assertRaises(pa.ApprovalRefusal) as caught:
            pa.require_approval(projection, repo=REPO, revision=FULL_A,
                                path=PATH_A, now=NOW)
        self.assertEqual(caught.exception.code, "approval-revoked")

    def test_revocation_by_scope_does_not_cover_later_approvals(self):
        first = _comment(1017, _body(FULL_A, PATH_A))
        revocation = _comment(
            1018, "Owner approval revoked \u2014\n\n"
                  "https://github.com/%s/blob/%s/%s\n"
                  % (REPO, FULL_A, PATH_A),
            created="2026-10-18T00:00:00Z")
        later = _comment(1019, _body(FULL_B, PATH_A),
                         created="2026-10-19T00:00:00Z")
        projection = self.build([first, revocation, later])
        old = [item for item in projection.approvals
               if item.artifact.revision == FULL_A][0]
        new = [item for item in projection.approvals
               if item.artifact.revision == FULL_B][0]
        self.assertEqual(old.state, "revoked")
        self.assertEqual(new.state, "current")
        got = pa.require_approval(projection, repo=REPO, revision=FULL_B,
                                  path=PATH_A, now=NOW)
        self.assertEqual(got.id, new.id)

    def test_a_revocation_targeting_nobody_is_witnessed(self):
        approval = _comment(1020, _body(FULL_A, PATH_A))
        revocation = _comment(
            1021, "Owner approval revoked \u2014 #issuecomment-999999",
            created="2026-10-18T00:00:00Z")
        projection = self.build([approval, revocation])
        self.assertEqual(projection.approvals[0].state, "current")
        self.assertIn({"comment": 1021, "code": "revocation-time"},
                      [dict(item) for item in projection.refusals])

    def test_structured_envelope_and_expiry(self):
        payload = {
            "schema": pa.ENVELOPE_SCHEMA,
            "decision": "approved",
            "artifacts": [{"repo": REPO, "revision": FULL_A, "path": PATH_A}],
            "expires_at": "2026-10-01T00:00:00Z",
        }
        body = ("Owner approval \u2014 structured\n\n"
                "<!-- ameesh-approval:v1 %s -->\n" % json.dumps(payload))
        projection = self.build([_comment(1022, body)])
        self.assertEqual(len(projection.approvals), 1)
        with self.assertRaises(pa.ApprovalRefusal) as caught:
            pa.require_approval(projection, repo=REPO, revision=FULL_A,
                                path=PATH_A, now=NOW)
        self.assertEqual(caught.exception.code, "approval-expired")

    def test_a_malformed_envelope_never_falls_back_to_prose(self):
        body = ("Owner approval \u2014 16 October 2026\n\n"
                "<!-- ameesh-approval:v1 {not json} -->\n\n"
                "https://github.com/%s/blob/%s/%s\n"
                % (REPO, FULL_A[:7], PATH_A))
        projection = self.build([_comment(1023, body)])
        self.assertEqual(projection.approvals, ())
        self.assertIn("approval-envelope-invalid",
                      {item["code"] for item in projection.refusals})

    # -- identite locale de la source (cx2-01) -----------------------------

    def test_local_identity_refuses_another_source(self):
        foreign = self.source(repo="other/repo", issue=123,
                              approvers=("attacker",),
                              artifact_repos=("target/repo",))
        body = _body(FULL_A, PATH_A, repo="target/repo")
        projection = pa.build_projection(
            foreign, _issue(), [_comment(1100, body, login="attacker")],
            now=NOW)
        self.write(projection)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(now=NOW)
        self.assertEqual(caught.exception.code, "projection-source")
        config = {"approvals": {
            "projection": self.projection_path,
            "source": {"repo": REPO, "issue": ISSUE,
                       "approvers": [APPROVER]}}}
        with self.assertRaises(pa.ProjectionError) as caught:
            pa.load_projection(config, now=NOW)
        self.assertEqual(caught.exception.code, "projection-source")

    def test_local_identity_refuses_other_approvers(self):
        foreign = self.source(approvers=("attacker",))
        projection = pa.build_projection(
            foreign, _issue(),
            [_comment(1101, _body(FULL_A, PATH_A), login="attacker")],
            now=NOW)
        self.write(projection)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(now=NOW)
        self.assertEqual(caught.exception.code, "projection-source")

    def test_local_identity_refuses_an_extra_artifact_repo(self):
        widened = self.source(artifact_repos=("target/repo",))
        body = _body(FULL_A, PATH_A, repo="target/repo")
        projection = pa.build_projection(
            widened, _issue(), [_comment(1102, body)], now=NOW)
        self.write(projection)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(now=NOW)
        self.assertEqual(caught.exception.code, "projection-source")
        identity = pa.ApprovalIdentity(
            repo=REPO, issue=ISSUE, approvers=(APPROVER,),
            artifact_repos=("target/repo",))
        loaded = self.load(now=NOW, identity=identity)
        self.assertEqual(loaded.approvals[0].artifact.repo, "target/repo")

    def test_local_identity_is_required(self):
        projection = self.build([_comment(1103, _body(FULL_A, PATH_A))])
        self.write(projection)
        with self.assertRaises(pa.ProjectionError) as caught:
            pa.load_projection(path=self.projection_path, now=NOW)
        self.assertEqual(caught.exception.code, "projection-source")
        with self.assertRaises(pa.ProjectionError) as caught:
            pa.identity_from_config({"projection": self.projection_path})
        self.assertEqual(caught.exception.code, "projection-source")

    # -- projection locale -------------------------------------------------

    def test_projection_roundtrip_and_digest(self):
        projection = self.build([_comment(1024, _body(FULL_A, PATH_A))])
        self.write(projection)
        loaded = self.load(now=NOW)
        self.assertEqual(loaded.digest, projection.digest)
        self.assertEqual(loaded.path, os.path.abspath(self.projection_path))
        self.assertEqual(loaded.source["source_digest"],
                         projection.source["source_digest"])
        self.assertEqual(len(loaded.current()), 1)

    def test_write_never_overwrites_a_newer_projection(self):
        older = self.build([_comment(1024, _body(FULL_A, PATH_A))],
                           now=NOW - dt.timedelta(seconds=60))
        newer = self.build([_comment(1024, _body(FULL_A, PATH_A))], now=NOW)
        self.write(newer)
        self.assertEqual(pa.write_projection(older, self.projection_path),
                         "not-newer")
        loaded = self.load(now=NOW)
        self.assertEqual(loaded.generated_at, NOW)

    def test_projection_is_fail_closed_when_stale(self):
        projection = self.build([_comment(1025, _body(FULL_A, PATH_A))])
        self.write(projection)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(now=NOW + dt.timedelta(seconds=301))
        self.assertEqual(caught.exception.code, "projection-stale")

    def test_consumer_max_age_can_be_stricter_than_the_ttl(self):
        projection = self.build([_comment(1024, _body(FULL_A, PATH_A))])
        self.write(projection)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(now=NOW + dt.timedelta(seconds=120), max_age_s=60)
        self.assertEqual(caught.exception.code, "projection-stale")

    def test_owner_uid_is_enforced_when_configured(self):
        projection = self.build([_comment(1024, _body(FULL_A, PATH_A))])
        self.write(projection)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(now=NOW, owner_uid=os.geteuid() + 1)
        self.assertEqual(caught.exception.code, "projection-owner")
        self.assertEqual(self.load(
            now=NOW, owner_uid=os.geteuid()).digest, projection.digest)

    def test_projection_is_fail_closed_when_in_the_future(self):
        projection = self.build([_comment(1026, _body(FULL_A, PATH_A))],
                                now=NOW + dt.timedelta(seconds=120))
        self.write(projection)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(now=NOW)
        self.assertEqual(caught.exception.code, "projection-future")

    def test_projection_digest_catches_tampering(self):
        projection = self.build([_comment(1027, _body(FULL_A, PATH_A))])
        self.write(projection)
        with open(self.projection_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["approvals"][0]["author"] = "mallory"
        with open(self.projection_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(now=NOW)
        self.assertEqual(caught.exception.code, "projection-digest")

    def test_invalid_schema_is_refused_even_with_a_valid_digest(self):
        projection = self.build([_comment(1028, _body(FULL_A, PATH_A))])
        payload = projection.as_dict()
        payload.pop("digest")
        payload["schema"] = "ameesh-approvals/999"
        from ameesh import jcs
        payload["digest"] = hashlib.sha256(jcs.canonicalize(payload)).hexdigest()
        with open(self.projection_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(self.projection_path, 0o644)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(now=NOW)
        self.assertEqual(caught.exception.code, "projection-invalid")

    def test_projection_permissions_are_checked(self):
        projection = self.build([_comment(1029, _body(FULL_A, PATH_A))])
        self.write(projection)
        os.chmod(self.projection_path, 0o666)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(now=NOW)
        self.assertEqual(caught.exception.code, "projection-permissions")

    def test_projection_symlink_is_refused(self):
        projection = self.build([_comment(1030, _body(FULL_A, PATH_A))])
        self.write(projection)
        link = os.path.join(self.tmp, "link.json")
        os.symlink(self.projection_path, link)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(path=link, now=NOW)
        self.assertEqual(caught.exception.code, "projection-unreadable")

    def test_projection_oversize_is_refused(self):
        path = os.path.join(self.tmp, "big.json")
        with open(path, "wb") as handle:
            handle.write(b"{" + b" " * (pa.MAX_PROJECTION_BYTES + 1))
        os.chmod(path, 0o644)
        with self.assertRaises(pa.ProjectionError) as caught:
            self.load(path=path, now=NOW)
        self.assertEqual(caught.exception.code, "projection-bounds")

    def test_projection_missing_and_stale_head(self):
        empty = self.build([])
        with self.assertRaises(pa.ApprovalRefusal) as caught:
            pa.require_approval(empty, repo=REPO, revision=FULL_A,
                                path=PATH_A, now=NOW)
        self.assertEqual(caught.exception.code, "approval-missing")
        projection = self.build([_comment(1031, _body(FULL_A, PATH_A))])
        self.write(projection)
        loaded = self.load(now=NOW)
        with self.assertRaises(pa.ApprovalRefusal) as caught:
            pa.require_approval(loaded, repo=REPO, revision=FULL_B,
                                path=PATH_A, now=NOW)
        self.assertEqual(caught.exception.code, "approval-stale")
        with self.assertRaises(pa.ApprovalRefusal) as caught:
            pa.require_approval(loaded, repo=REPO, revision=FULL_A,
                                path=PATH_A, source=pa.SOURCE_API, now=NOW)
        self.assertEqual(caught.exception.code, "approval-api-not-authority")

    # -- configuration et secret -------------------------------------------

    def test_token_value_in_configuration_is_refused(self):
        with self.assertRaises(pa.ReaderError) as caught:
            pa.source_from_config({
                "projection": self.projection_path,
                "source": {"repo": REPO, "issue": ISSUE,
                           "approvers": [APPROVER],
                           "token": "ghp_secret", "token_file": self.token_file},
            })
        self.assertEqual(caught.exception.code, "reader-config")
        self.assertNotIn("ghp_secret", str(caught.exception))

    def test_api_base_is_https_outside_loopback(self):
        section = {
            "projection": self.projection_path,
            "source": {"repo": REPO, "issue": ISSUE, "approvers": [APPROVER],
                       "token_file": self.token_file,
                       "api_base": "http://api.github.com"},
        }
        with self.assertRaises(pa.ReaderError) as caught:
            pa.source_from_config(section)
        self.assertEqual(caught.exception.code, "reader-config")
        section["source"]["api_base"] = "http://127.0.0.1:9"
        source = pa.source_from_config(section)
        self.assertEqual(source.api_base, "http://127.0.0.1:9")

    def test_token_file_permissions_and_symlink(self):
        os.chmod(self.token_file, 0o644)
        source = self.source()
        with self.assertRaises(pa.ReaderError) as caught:
            pa.GitHubTransport(source)
        self.assertEqual(caught.exception.code, "reader-token")
        self.assertNotIn(TOKEN, str(caught.exception))
        os.chmod(self.token_file, 0o600)
        link = os.path.join(self.tmp, "token-link")
        os.symlink(self.token_file, link)
        with self.assertRaises(pa.ReaderError) as caught:
            pa.GitHubTransport(self.source(token_file=link))
        self.assertEqual(caught.exception.code, "reader-token")
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_projection_never_contains_the_token(self):
        projection = self.build([_comment(1032, _body(FULL_A, PATH_A))])
        raw = json.dumps(projection.as_dict())
        self.assertNotIn(TOKEN, raw)
        self.write(projection)
        with open(self.projection_path, encoding="utf-8") as handle:
            self.assertNotIn(TOKEN, handle.read())

    # -- lecteur reseau (transport injecte) --------------------------------

    class FakeTransport:
        def __init__(self, issue: dict, pages: list[list[dict]],
                     commits: dict | None = None) -> None:
            self.issue = issue
            self.pages = pages
            self.commits = commits or {}
            self.paths: list[str] = []

        def get(self, path: str):
            self.paths.append(path)
            if path.endswith("/issues/%d" % ISSUE):
                return 200, {}, json.dumps(self.issue).encode()
            if "/comments?" in path:
                page = int(re.search(r"(?:^|[?&])page=(\d+)", path).group(1))
                chunk = self.pages[page - 1] if page <= len(self.pages) else []
                return 200, {}, json.dumps(chunk).encode()
            if "/commits/" in path:
                ref = path.rsplit("/", 1)[-1]
                sha = self.commits.get(ref, ref if len(ref) == 40 else "e" * 40)
                return 200, {}, json.dumps({"sha": sha}).encode()
            return 404, {}, b""

    def test_refresh_reads_issue_comments_and_commits(self):
        transport = self.FakeTransport(
            _issue(), [[_comment(1033, _body(FULL_A, PATH_A,
                                            ref=FULL_A))]],
            commits={FULL_A: FULL_A})
        projection = pa.refresh(self.source(), transport=transport)
        self.assertEqual(len(projection.approvals), 1)
        self.assertTrue(any("/issues/704" in path for path in transport.paths))
        self.assertTrue(any("/comments?" in path for path in transport.paths))
        self.assertTrue(any("/commits/%s" % FULL_A in path
                            for path in transport.paths))

    def test_refresh_paginates_and_bounds(self):
        first_page = [_comment(2000 + index, "checkpoint %d" % index)
                      for index in range(100)]
        transport = self.FakeTransport(
            _issue(), [first_page, [_comment(2100, _body(FULL_A, PATH_A))]],
            commits={FULL_A: FULL_A})
        projection = pa.refresh(self.source(), transport=transport)
        self.assertEqual(len(projection.approvals), 1)
        self.assertTrue(any("page=2" in path for path in transport.paths))

    def test_refresh_http_error_and_timeout(self):
        class Error:
            def get(self, path):
                return 403, {}, b""

        class Timeout:
            def get(self, path):
                raise TimeoutError("too slow")

        with self.assertRaises(pa.ReaderError) as caught:
            pa.refresh(self.source(), transport=Error())
        self.assertEqual(caught.exception.code, "reader-http")
        with self.assertRaises(pa.ReaderError) as caught:
            pa.refresh(self.source(), transport=Timeout())
        self.assertEqual(caught.exception.code, "reader-timeout")

    # -- integration lecture des sources / snapshot ------------------------

    def test_read_sources_includes_a_fresh_projection(self):
        projection = self.build([_comment(1034, _body(FULL_A, PATH_A))])
        self.write(projection)
        config = {"approvals": {
            "projection": self.projection_path, "max_age_s": 300,
            "source": {"repo": REPO, "issue": ISSUE,
                       "approvers": [APPROVER]}}}
        context = self._context()
        original = pa.load_projection

        def at_fixture_time(*args, **kwargs):
            kwargs["now"] = NOW
            return original(*args, **kwargs)

        with mock.patch.object(pa.config_mod, "load", return_value=config), \
                mock.patch.object(pa, "load_projection",
                                  side_effect=at_fixture_time):
            sources = pr.read_sources(context)
        self.assertIn("approvals", sources)
        self.assertEqual(sources["approvals"]["records"][0]["artifact"]["path"],
                         PATH_A)
        self.assertEqual(sources["approvals"]["projection"]["digest"],
                         projection.digest)

    def test_read_sources_omits_a_foreign_projection(self):
        foreign = self.source(repo="other/repo", issue=123,
                              approvers=("attacker",),
                              artifact_repos=("target/repo",))
        body = _body(FULL_A, PATH_A, repo="target/repo")
        projection = pa.build_projection(
            foreign, _issue(), [_comment(1104, body, login="attacker")],
            now=NOW)
        self.write(projection)
        config = {"approvals": {
            "projection": self.projection_path,
            "source": {"repo": REPO, "issue": ISSUE,
                       "approvers": [APPROVER]}}}
        with mock.patch.object(pa.config_mod, "load", return_value=config):
            sources = pr.read_sources(self._context())
        self.assertNotIn("approvals", sources)

    def test_read_sources_omits_a_missing_or_stale_projection(self):
        context = self._context()
        with mock.patch.object(
                pa.config_mod, "load",
                return_value={"approvals": {
                    "projection": self.projection_path,
                    "source": {"repo": REPO, "issue": ISSUE,
                               "approvers": [APPROVER]}}}):
            self.assertNotIn("approvals", pr.read_sources(context))
        projection = self.build([_comment(1035, _body(FULL_A, PATH_A))],
                                now=NOW - dt.timedelta(seconds=600))
        self.write(projection)
        with mock.patch.object(
                pa.config_mod, "load",
                return_value={"approvals": {
                    "projection": self.projection_path, "max_age_s": 300,
                    "source": {"repo": REPO, "issue": ISSUE,
                               "approvers": [APPROVER]}}}):
            self.assertNotIn("approvals", pr.read_sources(context))

    def _context(self):
        class FakeDb:
            def query(self, *args, **kwargs):
                return []

        return pr.VerificationContext(
            db=FakeDb(), work_item={"id": 4006},
            command=SimpleNamespace(work_item_id=4006),
            spec=object(), from_stage="implementation", to_stage="review")

    def test_source_mapping_consumer_keeps_the_exact_rules(self):
        projection = self.build([_comment(1037, _body(FULL_A, PATH_A))])
        source = projection.as_source()
        got = pa.approval_for_source(
            source, repo=REPO, revision=FULL_A, path=PATH_A, now=NOW)
        self.assertEqual(got.artifact.path, PATH_A)
        with self.assertRaises(pa.ApprovalRefusal) as caught:
            pa.approval_for_source(source, repo=REPO, revision=FULL_B,
                                   path=PATH_A, now=NOW)
        self.assertEqual(caught.exception.code, "approval-stale")
        with self.assertRaises(pa.ProjectionError) as caught:
            pa.approval_for_source(source, repo=REPO, revision=FULL_A,
                                   path=PATH_A,
                                   now=NOW + dt.timedelta(seconds=301))
        self.assertEqual(caught.exception.code, "projection-stale")
        with self.assertRaises(pa.ProjectionError) as caught:
            pa.approval_for_source({}, repo=REPO, revision=FULL_A,
                                   path=PATH_A, now=NOW)
        self.assertEqual(caught.exception.code, "projection-invalid")

    def test_verifier_snapshot_exposes_approvals(self):
        records = [{"id": "a", "state": "current", "artifact": {}}]
        snapshot = pv.Snapshot(
            approvals={"projection": {"digest": "d"}, "records": records},
            sources=("approvals",))
        self.assertEqual([dict(item) for item in pv.approval_records(snapshot)],
                         records)
        self.assertIn("approvals", pv.available_sources(snapshot))
        self.assertNotIn("approvals",
                         pv.available_sources(pv.Snapshot(sources=("milestones",))))
        self.assertEqual(pv.approval_records(pv.Snapshot()), ())
        self.assertEqual(
            pv.context_snapshot(mock.Mock(
                work_item={}, read_sources=lambda: {
                    "approvals": {"projection": {}, "records": records}})
            ).approvals["records"], records)

    # -- CLI et client HTTP reel (boucle locale) ---------------------------

    def test_cli_refresh_check_and_show_over_loopback(self):
        issue = _issue()
        comments = [_comment(1036, _body(FULL_A, PATH_A))]

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    self.send_response(401)
                    self.end_headers()
                    return
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/repos/%s/issues/%d" % (REPO, ISSUE):
                    payload = issue
                elif parsed.path == ("/repos/%s/issues/%d/comments"
                                     % (REPO, ISSUE)):
                    page = int(urllib.parse.parse_qs(parsed.query).get(
                        "page", ["1"])[0])
                    payload = comments if page == 1 else []
                elif parsed.path.startswith("/repos/%s/commits/" % REPO):
                    ref = parsed.path.rsplit("/", 1)[-1]
                    payload = {"sha": ref}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        config = {
            "approvals": {
                "projection": self.projection_path,
                "max_age_s": 300,
                "source": {
                    "repo": REPO, "issue": ISSUE, "approvers": [APPROVER],
                    "token_file": self.token_file,
                    "api_base": "http://127.0.0.1:%d" % port,
                    "ttl_s": 300, "timeout_s": 5, "total_timeout_s": 10,
                },
            }
        }
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = cli.main(["refresh", "--config", config_path, "--json"])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertTrue(os.path.exists(self.projection_path))
        with open(self.projection_path, encoding="utf-8") as handle:
            self.assertNotIn(TOKEN, handle.read())
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["check", "--config", config_path,
                                       "--json"]), 0)
            self.assertEqual(cli.main([
                "show", "--config", config_path, "--repo", REPO,
                "--revision", FULL_A, "--path", PATH_A, "--json"]), 0)
            self.assertEqual(cli.main([
                "show", "--config", config_path, "--repo", REPO,
                "--revision", FULL_A, "--path", PATH_A,
                "--source", "api"]), 3)
        self.assertIn("approval-api-not-authority", stderr.getvalue())
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
