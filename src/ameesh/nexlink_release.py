# SPDX-License-Identifier: AGPL-3.0-only
"""Nexlink cutoff state machine (#3529): no production gesture, no polling loop.

Every mutation is preceded by durable intent. An ambiguous workflow dispatch
is reconciled by its exact SHA/ref/time; it is never blindly dispatched twice.
"""
from __future__ import annotations

import datetime
import hashlib
import subprocess
import urllib.request
import urllib.error
import json
import os
import re

from . import release_cutoff as rc

_VERSION = re.compile(r"^([1-9][0-9]*)([a-z])$")
# Closed lists: host configuration cannot substitute a production workflow or
# pass deployment inputs. CI uses the existing frozen-head integration contract.
STAGING_WORKFLOWS = ("deploy-api.yml", "deploy-web.yml", "deploy-media-worker.yml")
STAGING_SERVICES = dict(zip(STAGING_WORKFLOWS,
    ("nexlink-api-staging", "nexlink-web-staging", "nexlink-media-worker-staging")))
STAGING_DATABASES = {"hub": "krbuozsvvodmrpixdinc", "content": "ewvfhdihqgoipmsblaed"}
# #3640: a Nexlink packet lands on develop as
# `Merge #3654 « Nexlink package 180 on develop — … » (integration/paquet-180-nexlink)`;
# GitHub's own subject (`Merge pull request #N from owner/integration/…`) is
# read by release_cutoff.
_PACKET_MERGE = re.compile(r"^Merge #(\d+) « .* » \((integration/[^\s()]+)\)\s*$")


def ci_plan(project, pending):
    """Use the release tree's own planner, dry-run only, never its polling loop."""
    folder, path = rc._worktree(project.repo, pending["sha"])
    try:
        program = ("import { main } from './scripts/ci/release-top-dispatch.mjs'; "
                   "process.exitCode = await main(process.argv.slice(1), {log: () => {}});")
        proc = subprocess.run(["node", "--input-type=module", "-e", program, "--",
                               "--head", pending["sha"], "--ref", pending["integration"],
                               "--base", pending["main"], "--sql-anchor", pending["main"],
                               "--dry-run"], cwd=path, capture_output=True, text=True,
                              timeout=60)
        if proc.returncode:
            raise rc.ReleaseError("Nexlink final-tree CI planner refused: " + proc.stderr[-500:])
        report = json.loads(proc.stdout)
        if (report.get("schema") != "nexlink-release-top-dispatch/1" or report.get("blocked")
                or report.get("head") != pending["sha"]
                or report.get("ref") != pending["integration"]):
            raise rc.ReleaseError("Nexlink final-tree CI plan is invalid or blocked")
        selected = []
        anchors = {row["context"]: row for row in report["anchor"]}
        for context in report["due"]:
            if not re.fullmatch(r"lot/[a-z][a-z0-9-]*", context):
                raise rc.ReleaseError("Invalid Nexlink CI context")
            stem = context.removeprefix("lot/")
            if stem.startswith(("deploy-", "promote-", "sync-secrets-")) or "prod" in stem:
                raise rc.ReleaseError("Deployment workflow refused in CI plan")
            row = {"workflow": stem + ".yml"}
            if context in anchors:
                anchor = anchors[context]
                if (anchor["input"] not in ("landing_target", "migration_target")
                        or anchor["sha"] != pending["main"]):
                    raise rc.ReleaseError("Invalid SQL landing anchor")
                row["anchor_input"] = anchor["input"]
            selected.append(row)
        limit = report["maxInflight"]
        if type(limit) is not int or limit < 1:
            raise rc.ReleaseError("Invalid Nexlink CI concurrency limit")
        return {"parts": selected, "max_inflight": limit}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        raise rc.ReleaseError("Nexlink final-tree CI plan unavailable") from exc
    finally:
        rc._drop_worktree(project.repo, folder, path)


def validate_staging(project, sha, workflow):
    from .canon import load_yaml, YamlError
    if workflow not in STAGING_SERVICES:
        raise rc.ReleaseError("Staging workflow is not in the closed list or YAML unavailable")
    text = rc._git(project.repo, "show", sha + ":.github/workflows/" + workflow,
                   check=True).stdout
    try:
        doc = load_yaml(text)
        if doc.get("env", {}).get("SERVICE") != STAGING_SERVICES[workflow]:
            raise rc.ReleaseError("Staging workflow service differs from the closed staging list")
        dispatch = doc.get("on", doc.get(True, {})).get("workflow_dispatch")
        if dispatch is not None:
            allowed_inputs = {"test_only", "always_on"}
            for name, entry in (dispatch.get("inputs") or {}).items():
                if (name not in allowed_inputs or entry.get("type") != "boolean"
                        or entry.get("default") is not False):
                    raise rc.ReleaseError("Staging dispatch input could select another target")
        deploys = 0
        for job in doc["jobs"].values():
            if "uses" in job:
                raise rc.ReleaseError("Reusable deployment workflow is refused")
            if job.get("env", {}).get("SERVICE", STAGING_SERVICES[workflow]) != STAGING_SERVICES[workflow]:
                raise rc.ReleaseError("Job overrides the closed staging service")
            if "environment" in job:
                if job["environment"] != "staging":
                    raise rc.ReleaseError("Deployment environment must be literal staging")
                deploys += 1
            for step in job.get("steps", []):
                if step.get("env", {}).get("SERVICE", STAGING_SERVICES[workflow]) != STAGING_SERVICES[workflow]:
                    raise rc.ReleaseError("Step overrides the closed staging service")
                account = step.get("with", {}).get("service_account")
                if account and (account != "${{ vars.GCP_STAGING_DEPLOYER_SA }}"
                                or job.get("environment") != "staging"):
                    raise rc.ReleaseError("Deployment account must be the staging deployer")
        if not deploys:
            raise rc.ReleaseError("No staging deployment job in workflow")
    except (AttributeError, TypeError, KeyError, YamlError) as exc:
        raise rc.ReleaseError("Malformed staging workflow") from exc


def version_key(value):
    match = _VERSION.fullmatch(value)
    if not match:
        raise rc.ReleaseError("Nexlink version must be a number and one lowercase letter")
    return int(match[1]), ord(match[2]) - ord("a")


def next_version(value):
    number, letter = version_key(value)
    return "%d%s" % (number + (letter == 25), chr(ord("a") + (letter + 1) % 26))


def branches(project):
    proc = rc._git(project.repo, "ls-remote", "--heads", project.remote,
                   "refs/heads/release/*", check=True)
    result = {}
    for line in proc.stdout.splitlines():
        sha, ref = line.split()
        value = ref.removeprefix("refs/heads/release/")
        if _VERSION.fullmatch(value):
            result[value] = sha
    return result


def choose_version(project, explicit=None):
    known = branches(project)
    latest = max(known, key=version_key) if known else None
    if latest is None and explicit is None:
        raise rc.ReleaseError("No Nexlink release branch: an explicit initial version is required")
    value = explicit or next_version(latest)
    version_key(value)
    if latest and version_key(value) <= version_key(latest):
        raise rc.ReleaseError("Nexlink version must follow existing release branches")
    return value


def previous_release(project, fallback):
    known = branches(project)
    if not known:
        return fallback
    latest = max(known, key=version_key)
    rc.fetch(project.repo, project.remote, "release/" + latest)
    return known[latest]


def packet_head(repo, sha):
    """#3640: the packet head ^2 a develop merge integrated with its exact tree.

    The subject only names a packet merge; the proof is the second parent
    carrying the merge's tree (#2692: a conflict resolution proves nothing).
    """
    subject = rc._out(repo, "log", "-1", "--format=%s", sha)
    if not (_PACKET_MERGE.match(subject) or rc._PACKAGE_MERGE.match(subject)):
        return None
    parents = rc._out(repo, "rev-list", "--parents", "-n", "1", sha).split()
    if len(parents) != 3:
        return None
    head = parents[2]
    return head if rc.same_tree(repo, sha, head) else None


def _latest_runs(suite):
    """The latest attempt of each workflow; a skipped run executed nothing."""
    latest = {}
    for run in suite.get("runs", ()):
        if run.get("conclusion") == "skipped":
            continue
        workflow = run.get("workflow")
        if workflow not in latest or int(run["id"]) > int(latest[workflow]["id"]):
            latest[workflow] = run
    return latest


def tree_green(project, sha, reading):
    """#3640 (décision owner #141353 a, #3633 pour ameesh): develop's head is
    green for the cutoff when its tree is a green packet head's.

    Its own CI does not count (red, cancelled, absent or in progress). The
    packet head must be the merge's ^2 with the same tree, its CI reading
    green, and every workflow that ran on the head must have its latest
    attempt successful on the packet head: Nexlink suites are path-filtered,
    so the head's own workflows are the suite of this content, not the last
    green commit's. A head without any run inherits from the packet head's
    green; the final-tree CI (`ci_plan`) still runs before publishing.
    Returns the evidence or None; a read failure stays transient.
    """
    if rc.reading_is_green(reading):
        return None
    head = packet_head(project.repo, sha)
    if head is None:
        return None
    if not rc.reading_is_green(rc.ci_reading(project, head)):
        return None
    proven = _latest_runs(rc.suite_reading(project, head))
    required = _latest_runs(rc.suite_reading(project, sha))
    runs = []
    for workflow in sorted(required):
        run = proven.get(workflow)
        if run is None or run.get("conclusion") != "success":
            return None
        runs.append(run)
    return {"head": head,
            "tree": rc._out(project.repo, "rev-parse", "--verify", "%s^{tree}" % sha),
            "required": sorted(required),
            "runs": [rc._run_evidence(run) for run in runs]}


def tree_proof_text(sha, proof):
    runs = ", ".join(run["url"] or "run %s" % run["id"] for run in proof["runs"])
    return ("tree of %s equals packet head %s, green on the head's workflows: %s"
            % (sha[:12], proof["head"][:12], runs or "no run on the head"))


def snapshot(project):
    rc.fetch(project.repo, project.remote, project.target)
    rc.fetch(project.repo, project.remote, "main")
    head = rc.commit_of(project.repo, "%s/develop" % project.remote)
    main = rc.commit_of(project.repo, "%s/main" % project.remote)
    reading = rc.ci_reading(project, head)
    proof = None
    if rc.commit_is_green(project.repo, project, head, reading=reading):
        base = head
    else:
        proof = tree_green(project, head, reading)
        base = head if proof is not None else rc.last_green(project.repo, head, project)
    if base is None:
        raise rc.ReleaseError("Nexlink develop has no green first-parent commit")
    return head, base, main, reading, proof


def plan(cfg, project, *, now, version=None):
    """Read-only preview; the final merge must still pass its own CI."""
    rules, source = rc.compiled_rules(cfg, project.name)
    at, label = rc.cutoff_slot(rules, now)
    head, base, main, reading, proof = snapshot(project)
    value = choose_version(project, version)
    previous = previous_release(project, main)
    notes = "Merge develop %s + main %s; final-tree CI pending." % (base, main)
    if proof is not None:
        notes += " " + tree_proof_text(head, proof)
    return rc.Plan(project=project, rules=rules, canon_commit=source.commit,
                   cutoff_at=at, cutoff_label=label, head=head, ci=reading,
                   green=base == head, last_green=base, base=base,
                   current_version="", previous={"commit": main, "release_commit": previous}, version=value,
                   floor="patch", floor_reason="Nexlink release branch sequence",
                   packets=[], direct=[], migrations=[], included=[], excluded=[],
                   notes=notes, empty=rc.same_tree(project.repo, base, previous),
                   tree_green=proof)


def merge_candidate(project, base, main):
    folder, path = rc._worktree(project.repo, base)
    try:
        proc = rc._git(path, "-c", "user.name=ameesh release", "-c",
                       "user.email=ameesh-release@localhost", "merge", "--no-ff",
                       "--no-edit", "-m", "Nexlink cutoff: merge pinned main", main)
        if proc.returncode:
            raise rc.ReleaseError("Nexlink final-tree merge conflicts; resolve before cutoff")
        return rc.commit_of(path, "HEAD")
    finally:
        rc._drop_worktree(project.repo, folder, path)


def ensure_ref(project, ref, sha):
    """Create only; never overwrite a release or another operator's candidate."""
    proc = rc._git(project.repo, "ls-remote", project.remote, ref, check=True)
    if proc.stdout.strip():
        found = proc.stdout.split()[0]
        if found != sha:
            raise rc.ReleaseError("immutable ref %s differs from the frozen candidate" % ref)
        return
    rc._git(project.repo, "push", "--force-with-lease=%s:" % ref,
            project.remote, "%s:%s" % (sha, ref), check=True)


def gh(project, args):
    code, out, err = rc.plan_github.Gh().run(args)
    if code:
        raise rc.TransientReleaseError("Nexlink GitHub operation failed: %s" %
                                       " ".join((err or out).split())[:300])
    return out


def runs(project, workflow, ref, sha):
    payload = gh(project, ["run", "list", "-R", rc.github_slug(project),
                          "--workflow", workflow, "--branch", ref, "--commit", sha,
                          "--event", "workflow_dispatch", "--limit", "100", "--json",
                          "databaseId,headSha,headBranch,event,createdAt,status,conclusion,url"])
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise rc.TransientReleaseError("Nexlink workflow runs unreadable") from exc


def workflow_step(project, pending, workflow, *, staging, save, now):
    allowed = STAGING_WORKFLOWS if staging else tuple(
        part["workflow"] for part in pending["ci_parts"])
    if workflow not in allowed:
        raise rc.ReleaseError("workflow is outside the closed staging/CI list")
    if staging:
        validate_staging(project, pending["sha"], workflow)
    # GitHub staging admits release/*; this branch pins the tagged SHA.
    ref = ("release/" + pending["version"]) if staging else pending["integration"]
    ledger = pending.setdefault("dispatches", {})
    key = ("staging/" if staging else "ci/") + workflow
    attempt = ledger.get(key)
    if attempt is None:
        attempt = ledger[key] = {"intent_at": now}
        save()  # before the external side effect, including a timeout
        args = ["workflow", "run", workflow, "-R", rc.github_slug(project), "--ref", ref]
        if not staging:
            args += ["-f", "expected_sha=" + pending["sha"]]
            part = next(part for part in pending["ci_parts"] if part["workflow"] == workflow)
            if part.get("anchor_input"):
                args += ["-f", part["anchor_input"] + "=" + pending["main"]]
        gh(project, args)
        attempt["accepted"] = True
        save()
        return False
    candidates = []
    for row in runs(project, workflow, ref, pending["sha"]):
        stamp = datetime.datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00"))
        if (row["headSha"] == pending["sha"] and row["headBranch"] == ref
                and row["event"] == "workflow_dispatch"
                and stamp.timestamp() >= attempt["intent_at"] - 1):
            candidates.append(row)
    if len(candidates) != 1:
        pending["waiting"] = "%s: %s; reconcile dispatch intent, never resend blindly" % (
            key, "run not visible" if not candidates else "ambiguous runs")
        save()
        return False
    run = candidates[0]
    attempt["run"] = run
    save()
    if run["status"] != "completed":
        return False
    if run["conclusion"] != "success":
        raise rc.ReleaseError("Nexlink %s did not prove success: %s (%s)" %
                              (key, run["conclusion"], run["url"]))
    return True


def migration_requirements(project, sha, base):
    """Only migrations in this release delta; old holes are outside this gate.

    Nexlink migrate.sh: hub skips content-only files; content applies every
    migration except its explicit skips (147, 178, 333). Source: #3528 releaser.
    """
    names = rc._out(project.repo, "diff", "--no-renames", "--name-only", "--diff-filter=AM", base, sha,
                    "--", "supabase/migrations").splitlines()
    required = {"hub": [], "content": []}
    for name in names:
        match = re.search(r"/([0-9]+)[^/]*\.sql$", name)
        if not match:
            continue
        version = int(match[1])
        data = subprocess.run(["git", "-C", project.repo, "show", sha + ":" + name],
                              capture_output=True, check=True, timeout=30).stdout
        content_only = re.search(rb"(?im)^--[ \t]*migrate-target:[ \t]*content[ \t]*$", data) is not None
        row = dict(version=version, name=os.path.basename(name).removesuffix(".sql"),
                   checksum=hashlib.sha256(data).hexdigest())
        if not content_only:
            required["hub"].append(row)
        if version not in (147, 178, 333):
            required["content"].append(row)
    return {target: rows for target, rows in required.items() if rows}


# Read-only staging migration ledger access, granted only to this release model.
CHILD_CREDENTIALS = frozenset({"SUPABASE_ACCESS_TOKEN"})


def migration_rows(project, target, required):
    """Read-only query to the literal staging project, no caller-selected URL."""
    token = os.environ.get("SUPABASE_ACCESS_TOKEN", "")
    if not token:
        try:
            with open(os.path.join(project.repo, ".env"), encoding="utf-8") as stream:
                for line in stream:
                    if line.startswith("SUPABASE_ACCESS_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip("\"'")
                        break
        except FileNotFoundError:
            pass
    if not token:
        raise rc.ReleaseError("staging migration ledger credential unavailable; API dispatch refused")
    sql = ("select version,name,applied_by,checksum,applied_at from public.schema_migrations "
           "where version in (%s)" % ",".join(str(row["version"]) for row in required))
    request = urllib.request.Request(
        "https://api.supabase.com/v1/projects/" + STAGING_DATABASES[target] + "/database/query",
        data=json.dumps({"query": sql}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (OSError, ValueError) as exc:
        raise rc.ReleaseError("staging migration ledger unreadable; API dispatch refused") from exc


def migration_gate(cfg, project, pending):
    evidence = {}
    for target, requirements in migration_requirements(
            project, pending["sha"], pending["main"]).items():
        rows = migration_rows(project, target, requirements)
        if not isinstance(rows, list):
            raise rc.ReleaseError("staging migration ledger malformed: " + target)
        confirmed = []
        for required in requirements:
            matching = [row for row in rows if isinstance(row, dict)
                        and str(row.get("version")) == str(required["version"])]
            if len(matching) != 1:
                raise rc.ReleaseError("staging migration attestation missing: %s on %s" %
                                      (required["name"], target))
            row = matching[0]
            if (row.get("name") != required["name"]
                    or row.get("checksum") != required["checksum"]
                    or not row.get("applied_by") or not row.get("applied_at")):
                raise rc.ReleaseError("staging migration attestation differs: %s on %s" %
                                      (required["name"], target))
            confirmed.append(dict(row, project_ref=STAGING_DATABASES[target]))
        evidence[target] = confirmed
    return evidence


def publish(project, pending):
    sha, tag = pending["sha"], pending["tag"]
    ensure_ref(project, "refs/heads/release/" + pending["version"], sha)
    remote = rc.remote_tag(project.repo, project.remote, tag)
    if remote and remote != sha:
        raise rc.ReleaseError("Nexlink annotated tag already points elsewhere")
    if remote:
        rc._git(project.repo, "fetch", project.remote, "refs/tags/" + tag, check=True)
        if rc._out(project.repo, "cat-file", "-t", "FETCH_HEAD") != "tag":
            raise rc.ReleaseError("Nexlink release tag must be annotated")
    if not remote:
        local = rc._git(project.repo, "rev-parse", "--verify", "refs/tags/" + tag)
        if local.returncode == 0:
            if (rc.commit_of(project.repo, tag) != sha
                    or rc._out(project.repo, "cat-file", "-t", tag) != "tag"):
                raise rc.ReleaseError("Nexlink local tag differs from frozen candidate")
        else:
            rc._git(project.repo, "-c", "user.name=ameesh release", "-c",
                    "user.email=ameesh-release@localhost", "tag", "-a", tag,
                    sha, "-m", "Nexlink " + pending["version"], check=True)
        rc._git(project.repo, "push", project.remote, "refs/tags/" + tag, check=True)
    slug = rc.github_slug(project)
    head = "release/" + pending["version"]
    existing = json.loads(gh(project, ["pr", "list", "-R", slug, "--head", head,
                                      "--base", "main", "--state", "all", "--json", "url"]))
    if existing:
        return existing[0]["url"]
    return gh(project, ["pr", "create", "-R", slug, "--head", head, "--base", "main",
                        "--title", "Nexlink " + pending["version"], "--body",
                        "Frozen final tree: %s. Staging only; production requires owner go."
                        % sha]).strip()


def execute(cfg, db, project, *, now, force=False, no_staging=False, version=None):
    if no_staging:
        raise rc.ReleaseError("Nexlink cutoff cannot bypass staging with --no-staging")
    with rc._locked(os.path.join(cfg.state_dir, "release", "release.lock")):
        state = rc.load_state(cfg)
        slot = rc.project_state(state, project.name)
        rules, source = rc.compiled_rules(cfg, project.name)
        at, label = rc.cutoff_slot(rules, now)
        save = lambda: rc.save_state(cfg, state)
        pending = slot.get("pending_nexlink")
        if pending is None:
            if not force and float(slot.get("last_cutoff") or 0) >= at:
                return {"result": "already-recorded", "cutoff_at": at}
            due = rc.due(rules, now, slot, project.window_s)
            if not force and due is None:
                return {"result": "not-due", "cutoff_at": at}
            if not force and due.get("missed"):
                slot["last_cutoff"] = at
                save()
                return {"result": "missed", "cutoff_at": at}
            head, base, main, reading, proof = snapshot(project)
            value = choose_version(project, version)
            sha = merge_candidate(project, base, main)
            previous = previous_release(project, main)
            pending = slot["pending_nexlink"] = dict(
                schema=rc.SCHEMA, project=project.name,
                version=value, tag="nexlink-" + value, integration="integration/release-" + value,
                head=head, base=base, main=main, sha=sha, cutoff_at=at, label=label,
                previous=previous, empty=rc.same_tree(project.repo, previous, sha),
                canon_commit=source.commit, tree_green=proof)
            save()
        elif version and version != pending["version"]:
            raise rc.ReleaseError("a frozen Nexlink cutoff is already in progress")
        ensure_ref(project, "refs/heads/" + pending["integration"], pending["sha"])
        rc.fetch(project.repo, project.remote, pending["integration"])
        if "ci_parts" not in pending:
            planned = ci_plan(project, pending)
            pending["ci_parts"] = planned["parts"]
            pending["ci_max_inflight"] = planned["max_inflight"]
            save()
        ready = True
        # Honor the release planner's live-parts limit; existing attempts are observed
        # before starting the next wave, without sleeping for their completion.
        active = 0
        for part in pending["ci_parts"]:
            key = "ci/" + part["workflow"]
            if key not in pending.get("dispatches", {}) and active >= pending["ci_max_inflight"]:
                ready = False
                continue
            if not workflow_step(project, pending, part["workflow"],
                                 staging=False, save=save, now=now):
                ready = False
                active += 1
        if not ready:
            return dict(pending, result="waiting-ci")
        pending["pr"] = publish(project, pending)
        save()
        if not pending["empty"]:
            pending["migration_attestation"] = migration_gate(cfg, project, pending)
            save()
            for workflow in STAGING_WORKFLOWS:
                if not workflow_step(project, pending, workflow, staging=True, save=save, now=now):
                    return dict(pending, result="waiting-staging")
        report = dict(pending, result="empty" if pending["empty"] else "released",
                      project=project.name, staging_ok=not pending["empty"])
        slot.update(last_cutoff=pending["cutoff_at"], last_result=report["result"])
        slot.pop("pending_nexlink")
        save()
        rc.write_report(cfg, project, report)
        if db is not None:
            rc.send_report(cfg, db, project, "Nexlink %s : %s ; %s" %
                           (pending["version"], report["result"], pending["pr"]), report)
        return report
