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

# #6020: the real sequence (release-11ac … release-11as, after release/11z)
# counts its letters like spreadsheet columns: 11z → 11aa, 11az → 11ba; the
# number only moves by an explicit `--version`.
_VERSION = re.compile(r"^([1-9][0-9]*)([a-z]{1,3})$")
#: the existing Nexlink tags are `release-<version>`
TAG_PREFIX = "release-"
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
        raise rc.ReleaseError("Nexlink version must be a number and one to three "
                              "lowercase letters")
    return int(match[1]), len(match[2]), match[2]


def next_version(value):
    number, _size, letters = version_key(value)
    letters = list(letters)
    position = len(letters) - 1
    while position >= 0 and letters[position] == "z":
        letters[position] = "a"
        position -= 1
    if position < 0:
        letters.insert(0, "a")
    else:
        letters[position] = chr(ord(letters[position]) + 1)
    value = "%d%s" % (number, "".join(letters))
    version_key(value)
    return value


def branches(project):
    """Published versions: `release/<v>` branches and `release-<v>` tags.

    #6020: the manual cutoffs since 11ac publish a tag without a branch; the
    branch commit wins when both exist, a tag reads its peeled commit.
    """
    proc = rc._git(project.repo, "ls-remote", project.remote, "refs/heads/release/*",
                   "refs/tags/" + TAG_PREFIX + "*", check=True)
    result, tags = {}, {}
    for line in proc.stdout.splitlines():
        sha, ref = line.split()
        if ref.startswith("refs/heads/release/"):
            value = ref.removeprefix("refs/heads/release/")
            if _VERSION.fullmatch(value):
                result[value] = sha
            continue
        value = ref.removeprefix("refs/tags/" + TAG_PREFIX)
        peeled = value.endswith("^{}")
        value = value.removesuffix("^{}")
        if _VERSION.fullmatch(value) and (peeled or value not in tags):
            tags[value] = sha
    for value, sha in tags.items():
        result.setdefault(value, sha)
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
    # A tag-only version was fetched with the target (`rc.fetch` takes --tags).
    if rc._git(project.repo, "ls-remote", "--heads", project.remote,
               "refs/heads/release/" + latest, check=True).stdout.strip():
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

# #6020: staging handed to the bureau (`staging_handoff`). The cutoff runs
# the final-tree CI, tags and reports; the bureau applies the migrations,
# dispatches STAGING_WORKFLOWS on release/<v>, then attests with
# `ameesh release attest`, wherever it runs. The attestation, or the pilot's
# decision to pass, is an event of the shared log: the release host reads it,
# checks each run on GitHub and the migrations against its own Git delta, and
# never reads a staging credential.
HANDOFF_EVENT = "release.handoff"
HANDOFF_DAYS = 30
HANDOFF_LIMIT = 50
HANDOFF_DECISIONS = ("attested", "passed")
_SHA = re.compile(r"^[0-9a-f]{40}$")


def handoff_object(project_name, version):
    return "release:%s:%s" % (project_name, version)


def parse_runs(texts):
    """`WORKFLOW=RUN_ID`, exactly one per staging workflow."""
    runs = {}
    for text in texts or ():
        workflow, _, run_id = str(text).partition("=")
        if workflow not in STAGING_WORKFLOWS or not run_id.isdigit() or workflow in runs:
            raise rc.ReleaseError("--run WORKFLOW=RUN_ID expected once for each of %s"
                                  % ", ".join(STAGING_WORKFLOWS))
        runs[workflow] = int(run_id)
    if set(runs) != set(STAGING_WORKFLOWS):
        raise rc.ReleaseError("one run per staging workflow is required: %s"
                              % ", ".join(STAGING_WORKFLOWS))
    return runs


def parse_migrations(texts):
    """`TARGET:VERSION`, TARGET among the staging databases."""
    rows = set()
    for text in texts or ():
        target, _, version = str(text).partition(":")
        if target not in STAGING_DATABASES or not version.isdigit():
            raise rc.ReleaseError("--migration TARGET:VERSION expected, TARGET in %s"
                                  % ", ".join(sorted(STAGING_DATABASES)))
        rows.add((target, int(version)))
    return [{"target": target, "version": version} for target, version in sorted(rows)]


def handoff_shape_error(data):
    """Why a handoff event is malformed, or ""."""
    if not isinstance(data, dict) or data.get("decision") not in HANDOFF_DECISIONS:
        return "unknown decision"
    if not isinstance(data.get("actor"), str) or not data["actor"]:
        return "no actor"
    if data["decision"] == "passed":
        return ""
    if not isinstance(data.get("sha"), str) or not _SHA.fullmatch(data["sha"]):
        return "no attested commit"
    try:
        parse_runs("%s=%s" % item for item in (data.get("runs") or {}).items())
        parse_migrations("%s:%s" % (row["target"], row["version"])
                         for row in data.get("migrations") or ())
    except (rc.ReleaseError, AttributeError, KeyError, TypeError) as exc:
        return str(exc) or "malformed attestation"
    return ""


def record_handoff(db, *, project, version, decision, actor, authority, sha="",
                   runs=None, migrations=None, now):
    """The bureau's attestation or the pilot's pass, as one shared event."""
    from . import events
    version_key(version)
    data = dict(schema=rc.SCHEMA, version=version, decision=decision, actor=actor,
                authority=authority, at=float(now))
    if decision == "attested":
        data.update(sha=sha, runs=runs or {}, migrations=migrations or [])
    why = handoff_shape_error(data)
    if why:
        raise rc.ReleaseError("release handoff refused: " + why)
    return events.record(db, HANDOFF_EVENT, handoff_object(project, version),
                         project=project, **data)


def handoff_events(db, project, version):
    """The handoff events of one version, oldest first: [(id, data)]."""
    from . import storage
    rows = storage.of(db).events.recent_type(
        HANDOFF_EVENT, HANDOFF_DAYS, HANDOFF_LIMIT,
        object=handoff_object(project.name, version))
    return sorted((int(row["id"]), row.get("data")) for row in rows or ()
                  if isinstance(row, dict))


def verify_attestation(project, pending, data):
    """("ok", evidence) | ("wait", why) | ("reject", why), without credential."""
    sha, version = pending["sha"], pending["version"]
    if data["sha"] != sha:
        return "reject", "attested commit %s is not the frozen candidate %s" % (
            data["sha"][:12], sha[:12])
    attested = {(row["target"], int(row["version"])) for row in data["migrations"]}
    for target, rows in migration_requirements(project, sha, pending["main"]).items():
        for row in rows:
            if (target, row["version"]) not in attested:
                return "reject", "migration %s on %s not attested" % (row["name"], target)
    slug = rc.github_slug(project)
    evidence = []
    for workflow in STAGING_WORKFLOWS:
        run_id = int(data["runs"][workflow])
        try:
            run = json.loads(gh(project, ["api", "repos/%s/actions/runs/%d" % (slug, run_id)]))
        except ValueError as exc:
            raise rc.TransientReleaseError("Nexlink run %d unreadable" % run_id) from exc
        path = str(run.get("path") or "").split("@")[0]
        if (path != ".github/workflows/" + workflow or run.get("head_sha") != sha
                or run.get("head_branch") != "release/" + version
                or run.get("event") != "workflow_dispatch"):
            return "reject", "run %d is not %s dispatched on release/%s @ %s" % (
                run_id, workflow, version, sha[:12])
        if run.get("status") != "completed":
            return "wait", "run %d (%s) still %s" % (run_id, workflow, run.get("status"))
        if run.get("conclusion") != "success":
            return "reject", "run %d (%s) concluded %s" % (
                run_id, workflow, run.get("conclusion"))
        evidence.append({"workflow": workflow, "id": run_id, "url": run.get("html_url")})
    return "ok", {"runs": evidence, "migrations": data["migrations"]}


def _announce(cfg, db, project, body, report, *, urgent=True):
    if db is not None:
        rc.send_report(cfg, db, project, body, report, urgent=urgent,
                       also=(project.staging_handoff,))


def _handoff_open(cfg, db, project, pending, now, save):
    """Tag published: hand staging over, once, and say what to attest."""
    required = migration_requirements(project, pending["sha"], pending["main"])
    rows = [{"target": target, "version": row["version"], "name": row["name"]}
            for target, entries in sorted(required.items()) for row in entries]
    pending["handoff"] = dict(to=project.staging_handoff, at=now, migrations=rows,
                              deadline=now + float(project.handoff_timeout_s), rejected={})
    save()
    deadline = datetime.datetime.fromtimestamp(pending["handoff"]["deadline"],
                                               datetime.timezone.utc)
    command = ("ameesh release attest --project %s --version %s --sha %s %s%s"
               % (project.name, pending["version"], pending["sha"],
                  " ".join("--run %s=<id>" % workflow for workflow in STAGING_WORKFLOWS),
                  "".join(" --migration %s:%d" % (row["target"], row["version"])
                          for row in rows)))
    body = ("Nexlink %s : CI de l'arbre final verte, tag %s posé sur %s "
            "(branche release/%s, %s).\n"
            "Staging remis à %s : appliquer les migrations de staging (%s), puis "
            "lancer %s sur release/%s, puis attester :\n%s\n"
            "Sans attestation à %s, annonce au pilote."
            % (pending["version"], pending["tag"], pending["sha"], pending["version"],
               pending.get("pr") or "PR à venir", project.staging_handoff,
               ", ".join("%s %s" % (row["target"], row["name"]) for row in rows) or "aucune",
               ", ".join(STAGING_WORKFLOWS), pending["version"], command,
               deadline.strftime("%d/%m %H:%MZ")))
    _announce(cfg, db, project, body, dict(pending, result="waiting-attestation"),
              urgent=False)
    return dict(pending, result="waiting-attestation")


def _handoff_resolution(cfg, db, project, pending, save):
    """The first valid decision of the shared log, or None (still waiting)."""
    handoff = pending["handoff"]
    rejected = handoff.setdefault("rejected", {})
    handoff.pop("waiting", None)
    for event_id, data in (handoff_events(db, project, pending["version"])
                           if db is not None else ()):
        if str(event_id) in rejected:
            continue
        why = handoff_shape_error(data)
        if not why and data.get("authority") != "corrector" and (
                data["decision"] == "passed" or data["actor"] != project.staging_handoff):
            why = "%s may not %s this release" % (
                data["actor"], "pass" if data["decision"] == "passed" else "attest")
        if not why:
            if data["decision"] == "passed":
                return "passed", dict(event=event_id, actor=data["actor"])
            verdict, detail = verify_attestation(project, pending, data)
            if verdict == "ok":
                return "attested", dict(detail, event=event_id, actor=data["actor"])
            if verdict == "wait":
                handoff["waiting"] = detail
                save()
                return None
            why = detail
        rejected[str(event_id)] = why
        save()
        _announce(cfg, db, project, "Nexlink %s : attestation de staging #%d refusée : %s. "
                  "Corriger puis attester de nouveau." % (pending["version"], event_id, why),
                  dict(pending, result="attestation-refused", event=event_id, message=why))
    return None


def _handoff_waiting(cfg, db, project, pending, at, label, now, save):
    """Bounded wait: the deadline and a blocked next cutoff are each said once."""
    handoff = pending["handoff"]
    if now >= float(handoff["deadline"]) and not handoff.get("overdue_at"):
        handoff["overdue_at"] = now
        save()
        _announce(cfg, db, project, "Nexlink %s : attestation de staging absente après "
                  "%.1f h (remis à %s le %s). Le tag %s reste posé ; la coupure attend."
                  % (pending["version"], (now - float(handoff["at"])) / 3600.0,
                     handoff["to"], datetime.datetime.fromtimestamp(
                         float(handoff["at"]), datetime.timezone.utc).strftime("%d/%m %H:%MZ"),
                     pending["tag"]), dict(pending, result="attestation-overdue"))
    if at > float(pending["cutoff_at"]) and handoff.get("blocked_cutoff") != at:
        handoff["blocked_cutoff"] = at
        save()
        _announce(cfg, db, project, "Nexlink : la coupure %s est due, mais %s attend "
                  "l'attestation de staging de %s. Décision du pilote : attendre "
                  "(rien à faire), ou passer : ameesh release pass --project %s "
                  "--version %s (la coupure %s part alors au tic suivant)."
                  % (label, pending["version"], handoff["to"], project.name,
                     pending["version"], label),
                  dict(pending, result="cutoff-blocked", blocked_cutoff=at))
    return dict(pending, result="waiting-attestation")


def _close(cfg, db, project, slot, pending, result, save):
    report = dict(pending, result=result, project=project.name,
                  staging_ok=result == "released")
    slot.update(last_cutoff=pending["cutoff_at"], last_result=result)
    slot.pop("pending_nexlink")
    save()
    rc.write_report(cfg, project, report)
    body = "Nexlink %s : %s ; %s" % (pending["version"], result, pending.get("pr"))
    attestation = pending.get("staging_attestation")
    if attestation:
        body += " ; staging attesté par %s : %s" % (attestation["actor"], ", ".join(
            "%s run %s" % (run["workflow"], run["id"]) for run in attestation["runs"]))
    if result == "not-attested":
        body += " ; staging non attesté, passé par %s" % pending["handoff_passed"]["actor"]
    _announce(cfg, db, project, body, report, urgent=result == "not-attested")
    return report


def _train_pause(db, project, now):
    """The project train paused, as a cause for a missed cutoff (#6014)."""
    if db is None:
        return ""
    key = "%s:%s" % (project.name, project.target)
    try:
        from . import storage
        state = storage.of(db).work.train_read(key)
    except Exception:  # noqa: BLE001 - a cause is a bonus, never a second failure
        return ""
    if not isinstance(state, dict) or state.get("phase") != "paused":
        return ""
    started = state.get("started_ts") or (state.get("plan") or {}).get("created_ts")
    if started is None:
        return "train %s paused (revision %s)" % (key, state.get("revision"))
    since = datetime.datetime.fromtimestamp(float(started), datetime.timezone.utc)
    age_h = max(0.0, now - float(started)) / 3600.0
    return "train %s paused since %s (%.1f h, revision %s)" % (
        key, since.strftime("%Y-%m-%dT%H:%MZ"), age_h, state.get("revision"))


def _missed_cutoff(cfg, db, project, slot, at, label, source, now, save):
    """#6014: a due Nexlink cutoff outside its window announces itself once.

    The semver model already did this; the Nexlink model recorded the slot and
    returned in silence. The report goes through `send_report`, which writes
    the project's thread as well as the mailbox, and names the known causes
    (the previous attempt's error, the project train paused, the local state
    and host) instead of a bare "missed".
    """
    failure = slot.get("last_error")
    failure_at = float(slot.get("last_error_at") or 0)
    causes = ["the automatic tick did not start inside its %ss window"
              % int(project.window_s)]
    report = {
        "schema": rc.SCHEMA, "result": "missed", "cutoff_at": at, "label": label,
        "project": project.name, "canon_commit": source.commit,
        "completed_at": now, "window_s": float(project.window_s),
        "host": str(getattr(cfg, "host", "") or ""),
        "state_path": rc.state_path(cfg),
    }
    if failure and at <= failure_at <= now:
        causes.append("the last attempt failed: %s" % failure)
        report["attempt_error"] = failure
    if db is not None:
        train = _train_pause(db, project, now)
        if train:
            causes.append(train)
            report["train"] = train
    report["causes"] = causes
    report["message"] = "; ".join(causes)
    slot["last_cutoff"] = at
    slot["last_result"] = "missed"
    slot["last_label"] = label
    save()
    when = datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
    body = (
        "Nexlink : coupure %s du %s MANQUÉE — aucun tag ni déploiement "
        "automatique n'a été produit par le moteur de release.\n"
        "Le tic n'a pas démarré dans sa fenêtre de %ss (état local : %s, hôte %s).\n"
        "Causes connues : %s.\n"
        "Vérifier le minuteur et l'hôte de la coupe nexlink, puis poser le tag "
        "à la main si la version reste à publier."
        % (when.strftime("%H:%MZ"), when.strftime("%d/%m/%Y"),
           int(project.window_s), rc.state_path(cfg), report["host"],
           ", ".join(causes)))
    if db is not None:
        rc.send_report(cfg, db, project, body, report, urgent=True)
    return report


def execute(cfg, db, project, *, now, force=False, no_staging=False, version=None):
    # #6014: `--no-staging` selects the semver local staging script. The
    # Nexlink model stages only through its closed workflow list, so the flag
    # is ignored here instead of refusing: a host whose elected generic tick
    # still carries the legacy ameesh drop-in can release Nexlink too, and
    # nothing ever *skips* the model's staging.
    with rc._locked(os.path.join(cfg.state_dir, "release", "release.lock")):
        state = rc.load_state(cfg)
        slot = rc.project_state(state, project.name)
        rules, source = rc.compiled_rules(cfg, project.name)
        at, label = rc.cutoff_slot(rules, now)
        save = lambda: rc.save_state(cfg, state)
        pending = slot.get("pending_nexlink")
        passed = None
        if pending is not None and pending.get("handoff"):
            outcome = _handoff_resolution(cfg, db, project, pending, save)
            if outcome is None:
                return _handoff_waiting(cfg, db, project, pending, at, label, now, save)
            kind, detail = outcome
            if kind == "attested":
                pending["staging_attestation"] = detail
                return _close(cfg, db, project, slot, pending, "released", save)
            # The pilot passed: this cutoff closes without staging and the due
            # one starts now, even past its window (it was held, not missed).
            pending["handoff_passed"] = detail
            passed = _close(cfg, db, project, slot, pending, "not-attested", save)
            pending = None
        if pending is None:
            if not force and float(slot.get("last_cutoff") or 0) >= at:
                return passed or {"result": "already-recorded", "cutoff_at": at}
            due = rc.due(rules, now, slot, project.window_s)
            if not (force or passed) and due is None:
                return {"result": "not-due", "cutoff_at": at}
            if not (force or passed) and due.get("missed"):
                return _missed_cutoff(cfg, db, project, slot, at, label, source,
                                      now, save)
            head, base, main, reading, proof = snapshot(project)
            value = choose_version(project, version)
            sha = merge_candidate(project, base, main)
            previous = previous_release(project, main)
            pending = slot["pending_nexlink"] = dict(
                schema=rc.SCHEMA, project=project.name,
                version=value, tag=TAG_PREFIX + value, integration="integration/release-" + value,
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
        if not pending["empty"] and project.staging_handoff:
            return _handoff_open(cfg, db, project, pending, now, save)
        if not pending["empty"]:
            pending["migration_attestation"] = migration_gate(cfg, project, pending)
            save()
            for workflow in STAGING_WORKFLOWS:
                if not workflow_step(project, pending, workflow, staging=True, save=save, now=now):
                    return dict(pending, result="waiting-staging")
        return _close(cfg, db, project, slot, pending,
                      "empty" if pending["empty"] else "released", save)
