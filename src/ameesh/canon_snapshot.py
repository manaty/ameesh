# SPDX-License-Identifier: AGPL-3.0-only
"""Public, read-only canon/directory snapshot (E1a, #7669).

The CLI is the public boundary; the Python loader and database remain private.
Only explicit directory fields leave the loader. A content digest identifies
the immutable payload, independently from the timestamped ref observation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time

from . import canon

SCHEMA = "ameesh-canon-snapshot/1"
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_UNSAFE_FINDINGS = frozenset({
    "canon-fetch-failed", "canon-local-divergence", "canon-worktree-dirty",
    "member-absent", "member-repository-mismatch",
})


def _finding(finding):
    # Messages can contain card values, filesystem paths or remote credentials.
    # The contract exposes diagnostic codes and card locations, never raw text.
    return {"code": finding.code, "severity": finding.severity,
            "member": finding.member, "path": finding.path}


def _source_errors(source):
    if source.mode != "git" or not _COMMIT.fullmatch(source.commit or ""):
        return ["untrusted-source"]
    if canon.git_commit(source.repo, source.rev) != source.commit:
        return ["source-moved"]
    if canon.git_commit(source.repo, "HEAD") != source.commit:
        return ["source-head-diverged"]
    result = canon._git(source.repo, "status", "--porcelain",
                        "--untracked-files=normal")
    if result.returncode:
        return ["source-state-unavailable"]
    return ["source-worktree-dirty"] if result.stdout.strip() else []


def _directory(value):
    """Only JSON data, including string object keys, can cross the boundary."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("directory object keys must be strings")
        return {key: _directory(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_directory(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError("directory must be JSON data")


def build(loaded, *, expected_commit, previous_commit=None, expected_scope=None,
          single_source=False, fetch_requested=False):
    """Project a loaded canon; fail closed before exposing directory content.

    expected_commit is the caller's trusted root pin, not a moving branch name.
    previous_commit optionally proves ancestry in that same root repository.
    This reads Git and validates cards; it never opens a database or syncs.
    """
    result = {"schema": SCHEMA, "ok": False, "snapshot_id": None,
              "snapshot": None, "findings": [], "refusals": [],
              "freshness": {"observed_at": None, "refs_verified": False,
                            "scope": "fetched-refs" if fetch_requested else "local-refs",
                            "remote_current": None}}

    def refuse(code, member=None):
        row = {"code": code, "member": member}
        if row not in result["refusals"]:
            result["refusals"].append(row)

    if not _COMMIT.fullmatch(expected_commit or ""):
        refuse("invalid-expected-commit")
    if previous_commit is not None and not _COMMIT.fullmatch(previous_commit):
        refuse("invalid-previous-commit")
    if result["refusals"]:
        return result
    try:
        if not loaded.readable or not loaded.sources:
            refuse("canon-unreadable")
        if loaded.untrusted:
            refuse("canon-untrusted")
        if single_source and len(loaded.sources) != 1:
            refuse("unexpected-source-count")
        if loaded.sources:
            root = loaded.sources[0]
            if root.commit != expected_commit:
                refuse("expected-commit-mismatch", root.member)
            if previous_commit and not canon.git_contains(root.repo, previous_commit,
                                                         expected_commit):
                refuse("non-monotonic-source", root.member)
            if expected_scope is not None:
                scope, error = canon.parse_scope(expected_scope)
                if error or scope != loaded.scopes.get(root.member):
                    refuse("expected-scope-mismatch", root.member)
        for source in loaded.sources:
            for code in _source_errors(source):
                refuse(code, source.member)
        findings = canon.validate(loaded)
        result["findings"] = sorted((_finding(f) for f in findings),
                                    key=lambda f: (f["code"], f["member"] or "",
                                                   f["path"] or "", f["severity"]))
        for finding in findings:
            if finding.severity == canon.ERROR or finding.code in _UNSAFE_FINDINGS:
                refuse(finding.code, finding.member)
        if loaded.scope_unread:
            refuse("scope-unreadable")
        if result["refusals"]:
            return result
        # Copy only the declared export fields, never Fiche.data wholesale.
        payload = {
            "canon": loaded.id,
            "root_commit": expected_commit,
            "sources": sorted([
                {"member": s.member, "commit": s.commit, "ref": s.rev,
                 "prefix": s.prefix, "scope": loaded.scopes.get(s.member),
                 "trust": "git-pinned"}
                for s in loaded.sources
            ], key=lambda s: (s["member"], s["prefix"], s["commit"])),
            "members": sorted([
                {"title": m.title, "roles": m.roles, "deputies": m.deputies,
                 "superiors": m.superiors, "ref": m.fiche.ref,
                 "directory": _directory(m.fiche.data.get("directory"))}
                for m in loaded.members
            ], key=lambda m: (m["title"], m["ref"])),
            "agents": sorted([
                {"name": a.title, "type": a.fiche.type,
                 "responsible": a.responsible, "team": a.team,
                 "roles": a.roles, "projects": a.effective_projects,
                 "aliases": a.aliases or [], "capabilities": a.capabilities,
                 "ref": a.fiche.ref, "directory": _directory(a.fiche.data.get("directory"))}
                for a in loaded.agents
            ], key=lambda a: (a["name"], a["ref"])),
        }
        # Detached content; reject non-JSON YAML objects rather than stringifying
        # arbitrary card data, or permitting NaN in a versioned wire contract.
        payload = copy.deepcopy(payload)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        # Validate again after all projection work: no moving ref, dirty tree,
        # or checkout switch may acquire a successful freshness observation.
        for source in loaded.sources:
            for code in _source_errors(source):
                refuse(code, source.member)
        if result["refusals"]:
            return result
        result.update(ok=True, snapshot=payload,
                      snapshot_id="sha256:" + hashlib.sha256(encoded).hexdigest())
        result["freshness"].update(observed_at=time.time(), refs_verified=True)
    except (canon.GitError, OSError):
        refuse("source-state-unavailable")
    except (TypeError, ValueError, RecursionError):
        refuse("snapshot-data-invalid")
    return result


def cmd_snapshot(cfg, args):
    from .canon_cli import _load_all

    # Loading multiple configured canons is supported by other commands, but a
    # caller pin identifies exactly one root; never choose one implicitly.
    try:
        loaded = _load_all(cfg, args)
        if len(loaded) != 1:
            raise ValueError("select exactly one canon")
        result = build(loaded[0], expected_commit=args.expect_commit,
                       previous_commit=args.previous_commit,
                       expected_scope=args.expect_scope,
                       single_source=args.single_source,
                       fetch_requested=args.fetch)
    except (canon.CanonError, canon.GitError, OSError, ValueError):
        result = {"schema": SCHEMA, "ok": False, "snapshot_id": None,
                  "snapshot": None, "findings": [],
                  "refusals": [{"code": "canon-selection-unavailable", "member": None}],
                  "freshness": {"observed_at": None, "refs_verified": False,
                                "scope": "fetched-refs" if args.fetch else "local-refs",
                                "remote_current": None}}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1
