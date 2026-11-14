# SPDX-License-Identifier: AGPL-3.0-only
"""Nexlink cutoff state machine (#3529): no production gesture, no polling loop.

Every mutation is preceded by durable intent. An ambiguous workflow dispatch
is reconciled by its exact SHA/ref/time; it is never blindly dispatched twice.
"""
from __future__ import annotations

import datetime
import dataclasses
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
# number only moves by an explicit `--version`. #6039: one or more lowercase
# letters, with no three-letter ceiling (11zzz → 11aaaa); sorting compares
# the letter count before the letters, which is the column order.
_VERSION = re.compile(r"^([1-9][0-9]*)([a-z]+)$")
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


def staging_jobs(project, sha, workflow):
    """#6020: the display names of the frozen workflow's staging deploy jobs.

    A green run whose deploy job was skipped (`test_only`) deployed nothing:
    the attestation must show each of these jobs successful. A name built
    from an expression cannot be matched and is refused."""
    from .canon import load_yaml, YamlError
    validate_staging(project, sha, workflow)
    text = rc._git(project.repo, "show", sha + ":.github/workflows/" + workflow,
                   check=True).stdout
    try:
        names = [str(job.get("name") or key) for key, job in load_yaml(text)["jobs"].items()
                 if job.get("environment") == "staging"]
    except (AttributeError, TypeError, KeyError, YamlError) as exc:
        raise rc.ReleaseError("Malformed staging workflow") from exc
    if any("${{" in name for name in names):
        raise rc.ReleaseError("staging deploy job name is an expression: %s" % workflow)
    return names


def version_key(value):
    match = _VERSION.fullmatch(value)
    if not match:
        raise rc.ReleaseError("Nexlink version must be a number and one or more "
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


def release_tags(project):
    """The remote `release-*` tags, version -> commit (peeled when annotated).

    The remote is authoritative: a pending resumed on another host must not
    miss the tag because its local tags are stale. `branches` lets a release
    branch win over its tag, and the branch may have received `main` after
    the tag (real 11au case, 07/11: the tag is on develop's head, the branch
    one merge ahead), so the tags are also exposed on their own.
    """
    proc = rc._git(project.repo, "ls-remote", project.remote,
                   "refs/tags/" + TAG_PREFIX + "*", check=True)
    versions = {}
    for line in proc.stdout.splitlines():
        commit, ref = line.split()
        value = ref.removeprefix("refs/tags/" + TAG_PREFIX)
        peeled = value.endswith("^{}")
        value = value.removesuffix("^{}")
        if _VERSION.fullmatch(value) and (peeled or value not in versions):
            versions[value] = commit
    return versions


def tagged_at(project, sha):
    """#6152: `release-*` tag versions naming exactly `sha`."""
    return {value for value, commit in release_tags(project).items() if commit == sha}


def already_cut(project, base, previous):
    """#6152: the version whose release already names `base`, or None.

    A manual cutoff tags develop's head (rel-01, 07/11 09:42Z); the next
    automatic tick would cut the same tree again under the next version
    (11av). A tag on the chosen base is the durable proof; `previous_release
    == base` states the same fact when the release ref is a branch. The
    caller then skips the slot: no tag, no branch, no failure mail.
    """
    tagged = tagged_at(project, base)
    if tagged:
        return max(tagged, key=version_key)
    if previous == base:
        known = branches(project)
        matches = [value for value, sha in known.items() if sha == base]
        if matches:
            return max(matches, key=version_key)
    return None


def pending_already_cut(project, pending):
    """#6152: `(version, empty)` when a frozen candidate must not resume.

    A pending opened before this guard survives in the state (the real 11av
    at 10:00Z, base tagged 11au); resuming it would tag the same tree twice.
    Its own tag is looked up by *version*, not on the base: `publish` tags
    `pending["sha"]`, the merge candidate, which can differ from the base
    (review B/P1). A crash after that push must reconcile, not skip.

    An empty candidate left by an earlier tick has nothing to publish either:
    resuming it would tag an empty release, and a failing CI part can keep it
    pending forever (real 11av, sql-invariant.yml). It closes `skipped`, with
    no tag; the returned flag says so, so the announcement does not claim a
    release that never existed. Its own tag still wins: a published candidate
    reconciles even when empty.
    """
    version = pending.get("version")
    tags = release_tags(project)
    if version in tags:
        return None
    at_base = {value for value, commit in tags.items() if commit == pending["base"]}
    if at_base:
        return max(at_base, key=version_key), False
    if pending.get("previous") == pending["base"]:
        known = branches(project)
        matches = [value for value, sha in known.items()
                   if sha == pending["base"] and value != version]
        if matches:
            return max(matches, key=version_key), False
    if pending.get("empty"):
        return version, True
    return None


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


def plan(cfg, project, *, now, version=None, state=None):
    """Read-only preview; the final merge must still pass its own CI.

    #6243: with the durable `state`, the preview settles the same skip
    `execute` decides before cutting. A frozen candidate the tick would only
    abandon must not be announced as a fresh cut. A candidate from an earlier
    slot is settled *and* the tick still cuts the current slot, so the preview
    keeps that cut and only names the pending.
    """
    rules, source = rc.compiled_rules(cfg, project.name)
    at, label = rc.cutoff_slot(rules, now)
    head, base, main, reading, proof = snapshot(project)
    previous = previous_release(project, main)
    skipped = already_cut(project, base, previous)
    skipped_empty = False
    settling = ""
    pending = None
    if state is not None:
        pending = rc.project_state(state, project.name).get("pending_nexlink")
    if pending is not None:
        settle = pending_already_cut(project, pending)
        if settle and float(pending.get("cutoff_at") or 0) >= at:
            skipped, skipped_empty = settle
        elif settle:
            settling = ("Le candidat figé release-%s (créneau %s) sera soldé "
                        "skipped avant la coupe." % (
                            pending.get("version"),
                            pending.get("label") or label))
    value = skipped or choose_version(project, version)
    notes = "Merge develop %s + main %s; final-tree CI pending." % (base, main)
    if skipped:
        if skipped_empty:
            notes = ("Créneau sauté : le candidat figé release-%s est vide, aucun "
                     "contenu nouveau à publier ; aucun tag, aucune branche." % skipped)
        else:
            notes = ("Créneau sauté : la base %s porte déjà release-%s ; aucun tag, "
                     "aucune branche." % (base, skipped))
    elif proof is not None:
        notes += " " + tree_proof_text(head, proof)
    if settling:
        notes = settling + " " + notes
    return rc.Plan(project=project, rules=rules, canon_commit=source.commit,
                   cutoff_at=at, cutoff_label=label, head=head, ci=reading,
                   green=base == head, last_green=base, base=base,
                   current_version="", previous={"commit": main, "release_commit": previous}, version=value,
                   floor="patch", floor_reason="Nexlink release branch sequence",
                   packets=[], direct=[], migrations=[], included=[], excluded=[],
                   notes=notes, empty=rc.same_tree(project.repo, base, previous),
                   tree_green=proof, skipped=skipped, skipped_empty=skipped_empty)


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
    # #6020: lightweight, like the existing release-11ac … release-11as
    # (orchestrator, 06/11). `remote_tag` reads the peeled commit either way.
    remote = rc.remote_tag(project.repo, project.remote, tag)
    if remote and remote != sha:
        raise rc.ReleaseError("Nexlink release tag already points elsewhere")
    if not remote:
        local = rc._git(project.repo, "rev-parse", "--verify", "refs/tags/" + tag)
        if local.returncode == 0:
            if rc.commit_of(project.repo, tag) != sha:
                raise rc.ReleaseError("Nexlink local tag differs from frozen candidate")
        else:
            rc._git(project.repo, "tag", tag, sha, check=True)
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
# the final-tree CI, tags and reports, then opens one attestation lot
# reserved to that agent (decision of the pilot, 06/11 03:50Z: no table, no
# migration). The bureau applies the migrations, dispatches STAGING_WORKFLOWS
# on release/<v>, then attests with `ameesh release attest`, which writes a
# note under a reserved prefix: `work.note` refuses it, so its journal author
# is the session's bound identity, never an `--actor`. The release host
# reads it, checks each run on GitHub and the migrations against its own Git
# delta, and never reads a staging credential.
ATTEST_PREFIX = "release-attest:"
SKIP_PREFIX = "release-skip:"
RESERVED_PREFIXES = (ATTEST_PREFIX, SKIP_PREFIX)
_SHA = re.compile(r"^[0-9a-f]{40}$")


def handoff_source(project_name, version):
    """The `source` of the attestation lot of one cutoff."""
    return "release-handoff:%s:%s" % (project_name, version)


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
    """`TARGET:VERSION:CHECKSUM`, TARGET among the staging databases.

    #6039: a version alone does not prove the content applied. The bureau
    attests the SHA256 of each migration file of the frozen delta, and the
    release host compares it with its own `migration_requirements`."""
    rows = set()
    for text in texts or ():
        parts = str(text).split(":")
        expected = ("--migration TARGET:VERSION:CHECKSUM expected, TARGET in %s"
                    % ", ".join(sorted(STAGING_DATABASES)))
        if len(parts) != 3:
            raise rc.ReleaseError(expected)
        target, version, checksum = parts
        if (target not in STAGING_DATABASES or not version.isdigit()
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
            raise rc.ReleaseError(expected)
        rows.add((target, int(version), checksum))
    return [{"target": target, "version": version, "checksum": checksum}
            for target, version, checksum in sorted(rows)]


def attestation_text(project_name, version, *, sha, runs, migrations):
    return "%s%s:%s %s" % (ATTEST_PREFIX, project_name, version, json.dumps(
        {"sha": sha, "runs": runs, "migrations": migrations}, sort_keys=True,
        separators=(",", ":")))


def skip_text(project_name, version, reason):
    return "%s%s:%s %s" % (SKIP_PREFIX, project_name, version,
                           " ".join(str(reason or "").split())[:300])


def read_decision(project_name, version, note):
    """("attested", data) | ("skipped", reason) | None for another note.

    A malformed attestation of this version is ("malformed", why)."""
    for prefix, kind in ((ATTEST_PREFIX, "attested"), (SKIP_PREFIX, "skipped")):
        head = "%s%s:%s " % (prefix, project_name, version)
        if not (note or "").startswith(head):
            continue
        rest = note[len(head):]
        if kind == "skipped":
            return kind, rest
        try:
            data = json.loads(rest)
            if not isinstance(data, dict) or not _SHA.fullmatch(str(data.get("sha"))):
                raise ValueError("no attested commit")
            parse_runs("%s=%s" % item for item in (data.get("runs") or {}).items())
            parse_migrations("%s:%s:%s" % (row["target"], row["version"],
                                           row.get("checksum") or "")
                             for row in data.get("migrations") or ())
        except (ValueError, rc.ReleaseError, AttributeError, KeyError, TypeError) as exc:
            return "malformed", str(exc) or "malformed attestation"
        return kind, data
    return None


def open_handoff_lot(db, project, pending):
    """The cutoff's attestation lot, created once, reserved to the bureau.

    Written through storage, like the split lot (#3256 A): the reservation
    is the release configuration's (`staging_handoff`), not a session's
    gesture. The advisory lock and the source make a retry find it again."""
    from . import storage, work
    source = handoff_source(project.name, pending["version"])
    executor = work.check_external_executor(db, project.staging_handoff)
    handoff = pending["handoff"]
    deadline = datetime.datetime.fromtimestamp(float(handoff["deadline"]),
                                               datetime.timezone.utc)
    with db.transaction() as tx:
        tx.query("SELECT pg_advisory_xact_lock(hashtextextended(lower(%s), 0))", (source,))
        existing = work.open_with_source(tx, source)
        if existing is not None:
            return int(existing["id"])
        created = storage.of(tx).work.add(
            type="evolution", source=source, app=project.name,
            title="staging %s %s : attestation (échéance %s)" % (
                project.name, pending["version"], deadline.strftime("%d/%m %H:%MZ")),
            body=handoff["instructions"], issue_ref=None, workstream=None,
            assignee=None, budget_usd=None, priority=1, actor=work.SYSTEM_SENDER,
            note="created by the %s cutoff %s; reserved to external executor %s"
                 % (project.name, pending["version"], executor),
            external_executor=executor, footprint=[])
    return int(created["id"])


#: #6020 (B1 cx3): the structured proof of a decision note, written only by
#: `write_decision`; no free-text journal write (`work move --note` …) fills
#: `evidence`, like the review floor (#3183).
DECISION_SCHEMA = "ameesh-release-handoff/1"


def write_decision(db, project_name, version, kind, text, actor):
    """Write a decision note, with its proof, on the open attestation lot."""
    from . import work, work_lifecycle
    item = work.open_with_source(db, handoff_source(project_name, version))
    if item is None:
        raise rc.ReleaseError("no open attestation lot for %s %s" % (project_name, version))
    evidence = {"schema": DECISION_SCHEMA, "kind": kind, "project": project_name,
                "version": version, "actor": actor}
    with db.transaction() as tx:
        tx.execute("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                   (int(item["id"]),))
        tx.execute("INSERT INTO work_item_events (work_item_id, state, note, actor, evidence)"
                   " VALUES (%s, %s, %s, %s, %s::jsonb)",
                   (int(item["id"]), work_lifecycle.phase(item), text, actor,
                    json.dumps(evidence)))
        tx.execute("UPDATE work_items SET updated_at = now() WHERE id = %s",
                   (int(item["id"]),))
    return int(item["id"])


def decision_authentic(row, project_name, version, kind):
    """The note carries the proof `write_decision` wrote, for its own author."""
    evidence = row.get("evidence")
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            return False
    return (isinstance(evidence, dict) and evidence.get("schema") == DECISION_SCHEMA
            and evidence.get("kind") == kind and evidence.get("project") == project_name
            and evidence.get("version") == version
            and evidence.get("actor") == (row.get("actor") or "") and bool(row.get("actor")))


def verify_attestation(project, pending, data):
    """("ok", evidence) | ("wait", why) | ("reject", why), without credential."""
    sha, version = pending["sha"], pending["version"]
    if data["sha"] != sha:
        return "reject", "attested commit %s is not the frozen candidate %s" % (
            data["sha"][:12], sha[:12])
    attested = {(row["target"], int(row["version"])): row.get("checksum") or ""
                for row in data.get("migrations") or ()}
    for target, rows in migration_requirements(project, sha, pending["main"]).items():
        for row in rows:
            checksum = attested.get((target, row["version"]))
            if checksum is None:
                return "reject", "migration %s on %s not attested" % (row["name"], target)
            if checksum != row["checksum"]:
                return "reject", ("migration %s on %s: attested content differs from the "
                                  "frozen file (checksum %s…, expected %s…)"
                                  % (row["name"], target, (checksum or "?")[:12],
                                     row["checksum"][:12]))
    slug = rc.github_slug(project)
    evidence = []
    for workflow in STAGING_WORKFLOWS:
        run_id = int(data["runs"][workflow])
        try:
            # B1 impl-03: the frozen workflow is the closed staging one.
            jobs = staging_jobs(project, sha, workflow)
        except rc.TransientReleaseError:
            raise
        except (rc.ReleaseError, subprocess.CalledProcessError) as exc:
            return "reject", "%s at %s is not a staging workflow: %s" % (
                workflow, sha[:12], exc)
        try:
            run = json.loads(gh(project, ["api", "repos/%s/actions/runs/%d" % (slug, run_id)]))
            if not isinstance(run, dict):
                raise ValueError("not a run")
        except rc.TransientReleaseError as exc:
            if "404" in str(exc) or "Not Found" in str(exc):
                return "reject", "run %d (%s) not found" % (run_id, workflow)
            return "wait", "run %d (%s) unreadable: %s" % (run_id, workflow, exc)
        except ValueError:
            return "wait", "run %d (%s) unreadable" % (run_id, workflow)
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
        # B2 cx3: a green run with its deploy job skipped deployed nothing.
        try:
            listed = json.loads(gh(project, ["api", "repos/%s/actions/runs/%d/jobs?per_page=100"
                                             % (slug, run_id)])).get("jobs") or []
        except (rc.TransientReleaseError, ValueError, AttributeError) as exc:
            return "wait", "jobs of run %d (%s) unreadable: %s" % (run_id, workflow, exc)
        for name in jobs:
            done = [job for job in listed if isinstance(job, dict) and (
                job.get("name") == name or str(job.get("name") or "").startswith(name + " ("))]
            if not done or any(job.get("conclusion") != "success" for job in done):
                return "reject", "run %d (%s): deploy job %s did not succeed (%s)" % (
                    run_id, workflow, name, ", ".join(
                        str(job.get("conclusion")) for job in done) or "absent")
        evidence.append({"workflow": workflow, "id": run_id, "url": run.get("html_url"),
                         "jobs": jobs})
    return "ok", {"runs": evidence, "migrations": data.get("migrations") or []}


def _announce(cfg, db, project, body, report, *, urgent=True, also=None):
    if db is None:
        return {}
    recipients = (project.staging_handoff,) if also is None else tuple(also)
    return rc.send_report(cfg, db, project, body, report, urgent=urgent,
                          also=recipients)


def _handoff_delivered(delivery, recipient) -> bool:
    """#6039 N5: the bureau itself got the report, not merely "a send ran"."""
    return bool(recipient and isinstance(delivery, dict) and delivery.get(recipient))


def staging_migration_preflight(project, pending, required):
    """Advisory only: the Git delta is not the applicator's pending inventory.

    No staging or ameesh proof-store access is made from this handoff reader.
    The applicator uses `ameesh staging-migrations` for the blocking check.
    """
    from . import staging_migrations as sm
    if not required:
        return []
    try:
        tree = sm.GitTree(project.repo, pending["sha"])
        policy = tree.policy()
        found = {}
        for rows in required.values():
            for row in rows:
                path = "supabase/migrations/" + row["name"] + ".sql"
                if path in found:
                    continue
                result = sm.classify(tree.read(path).decode("utf-8"), policy)
                found[path] = dict(path=path, checksum=row["checksum"],
                                   classification=result.kind, reasons=list(result.reasons),
                                   warning=("" if result.kind == "additive" else
                                            "applicator must verify declaration/proof if pending"))
        return list(found.values())
    except (sm.Refused, UnicodeError) as exc:
        return [dict(classification="unknown", warning=str(exc))]


def handoff_preflight(project, pending):
    """B1 dev3: every check that can refuse a handoff, run before the tag.

    The frozen staging workflows (closed list, deploy jobs) and the delta's
    migrations; a refusal here leaves no tag and no orphan cutoff."""
    for workflow in STAGING_WORKFLOWS:
        staging_jobs(project, pending["sha"], workflow)
    required = migration_requirements(project, pending["sha"], pending["main"])
    classified = staging_migration_preflight(project, pending, required)
    return {"migration_safety": classified,
            "migrations": [{"target": target, "version": row["version"],
                            "name": row["name"], "checksum": row["checksum"]}
                           for target, entries in sorted(required.items())
                           for row in entries]}


def drop_abandoned(project, slot, save):
    """B1 cx3 (gel 34030): the refused cutoff pushed its integration ref but
    no tag, so the next cutoff reuses the version. Delete that ref only while
    it still holds the abandoned candidate (lease); a ref moved by someone
    else stays, and `ensure_ref` refuses it as before."""
    stale = slot.get("abandoned_integration")
    if not stale:
        return
    ref = "refs/heads/" + stale["ref"]
    found = rc._git(project.repo, "ls-remote", project.remote, ref, check=True).stdout.split()
    if found and found[0] == stale["sha"]:
        rc._git(project.repo, "push", "--force-with-lease=%s:%s" % (ref, stale["sha"]),
                project.remote, ":" + ref, check=True)
    slot.pop("abandoned_integration")
    save()


def _preflight_refused(cfg, db, project, slot, pending, exc, save, now):
    """B1/B2 dev3 (gel 34015): a refused preflight left no tag; the cutoff
    closes as `refused-preflight` so the next one is not held behind it,
    and the pilot reruns this one with `release cutoff --force` once fixed.

    #6039 N8: this message is the only one; it records the error exactly as
    `_report_error` will read it, so the tick's generic report cannot claim
    a pending that this refusal just closed."""
    if isinstance(exc, subprocess.CalledProcessError):
        reason = "git %s failed" % " ".join(map(str, exc.cmd[-2:]))
    elif isinstance(exc, subprocess.TimeoutExpired):
        reason = "git %s timed out" % " ".join(map(str, exc.cmd[-2:]))
    else:
        reason = str(exc) or type(exc).__name__
    slot.update(last_cutoff=pending["cutoff_at"], last_result="refused-preflight",
                abandoned_integration=dict(ref=pending["integration"], sha=pending["sha"]))
    slot.pop("pending_nexlink")
    save()
    try:
        drop_abandoned(project, slot, save)
    except rc.ReleaseError as dropped:  # retried before the next candidate
        print("release: %s" % dropped)
    _announce(cfg, db, project,
              "Nexlink %s : coupure refusée avant le tag (préflight de remise : %s). "
              "Aucun tag posé ; après correction, `ameesh release cutoff --project %s "
              "--force`." % (pending["version"], reason, project.name),
              dict(pending, result="refused-preflight", error=reason))
    text = "Nexlink staging handoff preflight: %s" % reason
    slot.update(last_error=text, last_error_cutoff=pending["cutoff_at"],
                last_error_at=now)
    save()
    refused = rc.ReleaseError(text)
    # #6039 N8: the tick's generic `_report_error` reads this marker; the
    # refusal message above is the one and only report of this error.
    refused.already_reported = True
    raise refused from exc


def _handoff_body(pending):
    """The durable command sent to the bureau, with its lot when opened."""
    handoff = pending["handoff"]
    body = handoff["instructions"]
    if handoff.get("lot"):
        body += "\nLot d'attestation : #%d." % handoff["lot"]
    if handoff.get("lot_error"):
        body += ("\nLot d'attestation NON ouvert : %s. Corriger staging_handoff ; "
                 "le tic suivant réessaie." % handoff["lot_error"])
    return body


def _handoff_open(cfg, db, project, pending, now, save):
    """Tag published: hand staging over, once, and say what to attest.

    Nothing here may fail before `pending["handoff"]` is saved: the checks
    that can refuse (`handoff_preflight`) ran before the tag (B1 dev3)."""
    rows = pending["handoff_preflight"]["migrations"]
    deadline = now + float(project.attestation_timeout_s)
    command = ("ameesh release attest --project %s --version %s --sha %s %s%s"
               % (project.name, pending["version"], pending["sha"],
                  " ".join("--run %s=<id>" % workflow for workflow in STAGING_WORKFLOWS),
                  "".join(" --migration %s:%d:%s"
                          % (row["target"], row["version"], row["checksum"])
                          for row in rows)))
    when = datetime.datetime.fromtimestamp(deadline, datetime.timezone.utc)
    instructions = (
        "Nexlink %s : CI de l'arbre final verte, tag %s posé sur %s "
        "(branche release/%s, %s).\n"
        "Staging remis à %s : appliquer les migrations de staging (%s), puis "
        "lancer %s sur release/%s, puis attester (depuis la session de %s) :\n%s\n"
        "Sans attestation à %s, annonce au pilote."
        % (pending["version"], pending["tag"], pending["sha"], pending["version"],
           pending.get("pr") or "PR à venir", project.staging_handoff,
           ", ".join("%s %s" % (row["target"], row["name"]) for row in rows) or "aucune",
           ", ".join(STAGING_WORKFLOWS), pending["version"], project.staging_handoff,
           command, when.strftime("%d/%m %H:%MZ")))
    warnings = ["%s: %s %s" % (row.get("path", "migration inventory"),
                                   row.get("classification", "unknown"), row["warning"])
                for row in pending["handoff_preflight"].get("migration_safety", [])
                if row.get("warning")]
    if warnings:
        instructions += "\nSQL advisory (Git delta, not pending inventory):\n" + "\n".join(warnings)
    # #6039 N5: `reported` is durable after the bureau's delivery, not before:
    # a crash or a caught mail failure between this save and `_announce` is
    # retried by the next tick (`_handoff_reannounce`), without waiting for
    # the deadline notice.
    pending["handoff"] = dict(to=project.staging_handoff, at=now, migrations=rows,
                              deadline=deadline, rejected={}, reported=False,
                              instructions=instructions)
    save()
    if db is not None:
        from . import work
        from .db import DbError
        try:
            pending["handoff"]["lot"] = open_handoff_lot(db, project, pending)
        except (work.WorkError, DbError) as exc:
            # The tag is out: the wait still runs and the next tick retries.
            pending["handoff"]["lot_error"] = str(exc)
        save()
    delivery = _announce(cfg, db, project, _handoff_body(pending),
                         dict(pending, result="waiting-attestation"), urgent=False,
                         also=(pending["handoff"]["to"],))
    if _handoff_delivered(delivery, pending["handoff"]["to"]):
        pending["handoff"]["reported"] = True
        save()
    return dict(pending, result="waiting-attestation")


def _handoff_reannounce(cfg, db, project, pending, save):
    """#6039 N5: a handoff report is at-least-once, to the bureau itself.

    The tag is already out when the report is built; if the process stops
    between the durable `handoff` save and the send, or `send_report` caught a
    mail failure, the bureau would never receive the attestation command.
    Every tick re-sends it until the delivery map says the recipient got it;
    the deadline notice keeps its own deduplication."""
    handoff = pending["handoff"]
    if handoff.get("reported"):
        return
    recipient = handoff.get("to") or project.staging_handoff
    delivery = _announce(cfg, db, project, _handoff_body(pending),
                         dict(pending, result="waiting-attestation"), urgent=False,
                         also=(recipient,))
    if _handoff_delivered(delivery, recipient):
        handoff["reported"] = True
        save()


def _handoff_resolution(cfg, db, project, pending, save):
    """The first applicable decision of the attestation lot, or None.

    An attestation whose runs are still running waits, but later notes are
    still read: a skip, or a newer valid attestation, is never held behind it."""
    from . import work
    handoff = pending["handoff"]
    rejected = handoff.setdefault("rejected", {})
    if db is None:
        return None
    if not handoff.get("lot"):
        handoff["lot"] = open_handoff_lot(db, project, pending)
        handoff.pop("lot_error", None)
        save()
    waiting = None
    for row in work.events_noted(db, handoff["lot"], "release-"):
        event_id, actor = str(row["id"]), str(row.get("actor") or "")
        if event_id in rejected:
            continue
        decision = read_decision(project.name, pending["version"], row.get("note"))
        if decision is None:
            continue
        kind, data = decision
        if not decision_authentic(row, project.name, pending["version"],
                                  "skipped" if kind == "skipped" else "attested"):
            # B1 cx3: same text through another journal write (`move --note`).
            kind, data = "malformed", "note without the proof of ameesh release attest"
        if kind == "skipped":
            return "skipped", dict(event=int(row["id"]), actor=actor, reason=data)
        why = data if kind == "malformed" else ""
        expected_actor = handoff.get("to") or project.staging_handoff
        if not why and actor != expected_actor:
            why = "%s is not the staging agent %s" % (actor or "?", expected_actor)
        if not why:
            verdict, detail = verify_attestation(project, pending, data)
            if verdict == "ok":
                return "attested", dict(detail, event=int(row["id"]), actor=actor)
            if verdict == "wait":
                waiting = waiting or detail
                continue
            why = detail
        rejected[event_id] = why
        save()
        _announce(cfg, db, project, "Nexlink %s : attestation de staging (note %s du lot "
                  "#%d) refusée : %s. Corriger puis attester de nouveau."
                  % (pending["version"], event_id, handoff["lot"], why),
                  dict(pending, result="attestation-refused", event=int(row["id"]),
                       message=why))
    if waiting:
        handoff["waiting"] = waiting
    else:
        handoff.pop("waiting", None)
    save()
    return None


def _handoff_resolution_guarded(cfg, db, project, pending, save):
    """B2 impl-03: a read failure (GitHub, journal, lot) is said once per
    distinct error and never skips the deadline and held-cutoff announcements."""
    from . import work
    from .db import DbError
    handoff = pending["handoff"]
    try:
        outcome = _handoff_resolution(cfg, db, project, pending, save)
    except (rc.ReleaseError, work.WorkError, DbError, OSError,
            subprocess.SubprocessError) as exc:
        text = str(exc) or type(exc).__name__
        if handoff.get("error") != text:
            handoff["error"] = text
            save()
            _announce(cfg, db, project, "Nexlink %s : lecture de l'attestation de staging "
                      "en échec : %s. L'attente continue." % (pending["version"], text),
                      dict(pending, result="attestation-unreadable", message=text))
        return None
    if handoff.pop("error", None) is not None:
        save()
    return outcome


def _handoff_waiting(cfg, db, project, pending, at, label, now, save):
    """Bounded wait: the deadline and a held next cutoff are each said once."""
    handoff = pending["handoff"]
    lot = " (lot #%d)" % handoff["lot"] if handoff.get("lot") else ""
    if now >= float(handoff["deadline"]) and not handoff.get("overdue_at"):
        handoff["overdue_at"] = now
        save()
        _announce(cfg, db, project, "Nexlink %s : attestation de staging absente après "
                  "%.1f h (remis à %s le %s%s). Le tag %s reste posé ; la coupure attend."
                  % (pending["version"], (now - float(handoff["at"])) / 3600.0,
                     handoff["to"], datetime.datetime.fromtimestamp(
                         float(handoff["at"]), datetime.timezone.utc).strftime("%d/%m %H:%MZ"),
                     lot, pending["tag"]), dict(pending, result="attestation-overdue"))
    if at > float(pending["cutoff_at"]) and handoff.get("held_cutoff") != at:
        handoff["held_cutoff"] = at
        save()
        _announce(cfg, db, project, "Nexlink : la coupure %s est due, mais %s attend "
                  "l'attestation de staging de %s%s. Décision du pilote : attendre "
                  "(rien à faire), ou passer : ameesh release attest --project %s "
                  "--version %s --skip --reason \"…\" (la coupure %s part alors au tic "
                  "suivant)." % (label, pending["version"], handoff["to"], lot,
                                 project.name, pending["version"], label),
                  dict(pending, result="held-attestation", held_cutoff=at))
        return dict(pending, result="held-attestation")
    return dict(pending, result="waiting-attestation")


def _close_handoff_lot(db, pending, note):
    from . import work
    lot = (pending.get("handoff") or {}).get("lot")
    if db is None or not lot:
        return
    try:
        work.close(db, int(lot), done=True, note=note, actor=work.SYSTEM_SENDER)
    except work.WorkError as exc:  # already closed by hand: the cutoff still closes
        print("release: attestation lot #%s not closed: %s" % (lot, exc))


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
    skipped = pending.get("handoff_skipped")
    if skipped:
        body += " ; staging non attesté, passé par %s (%s)" % (
            skipped["actor"], skipped["reason"] or "sans raison")
    if pending.get("handoff"):
        _close_handoff_lot(db, pending, body)
    _announce(cfg, db, project, body, report, urgent=result == "staging-unattested")
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


def _skip_cutoff(cfg, db, project, slot, at, label, source, now, save, *,
                 version, head, base, empty=False):
    """#6152: the base is already a release; skip the slot, never cut it twice.

    A manual cutoff (rel-01, 07/11 09:42Z) tagged develop's head; the 10:00Z
    tick would have cut the same tree again as 11av. The slot is recorded as
    `skipped` (not `missed`), nothing is tagged, branched or dispatched, and
    the short announcement is not a failure report. `empty` names the other
    reason: a frozen candidate with no content to publish, which never had a
    release tag to name.
    """
    # Never move the recorded slot backwards: a stale pending is skipped on
    # its own cutoff, then the current tick still handles the due one.
    slot["last_cutoff"] = max(float(slot.get("last_cutoff") or 0), at)
    slot["last_result"] = "skipped"
    slot["last_label"] = label
    slot.pop("pending_nexlink", None)
    save()
    if empty:
        message = ("créneau sauté : coupe vide, aucun contenu nouveau à publier "
                   "(release-%s non posée)" % version)
        tag = None
    else:
        message = "créneau sauté : tête déjà coupée en release-%s" % version
        tag = TAG_PREFIX + version
    report = {
        "schema": rc.SCHEMA, "result": "skipped", "cutoff_at": at, "label": label,
        "project": project.name, "canon_commit": source.commit,
        "completed_at": now, "version": version, "tag": tag, "empty": empty,
        "head": head, "base": base, "message": message,
        "host": str(getattr(cfg, "host", "") or ""),
        "state_path": rc.state_path(cfg),
    }
    rc.write_report(cfg, project, report)
    if db is not None:
        body = ("Nexlink : %s (%s). Aucun tag ni branche créés, aucun "
                "déploiement produit pour ce créneau." % (message, base[:12]))
        rc.send_report(cfg, db, project, body, report, urgent=False)
    return report


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
        # #6039 N4: the handoff name is part of the release configuration;
        # resolve its alias and refuse an unknown agent before any tag, so
        # the attestation actor and the report recipient are the same one.
        if project.staging_handoff:
            project = dataclasses.replace(
                project, staging_handoff=rc.canonical_handoff(
                    cfg, db, project.staging_handoff))
        pending = slot.get("pending_nexlink")
        if (project.staging_handoff and pending is not None and pending.get("handoff")
                and pending["handoff"].get("to") != project.staging_handoff):
            # A frozen cutoff keeps its tag; only its recipient is repaired.
            # A removed configuration leaves the frozen `to` in force.
            pending["handoff"]["to"] = project.staging_handoff
            pending["handoff"]["reported"] = False
            save()
        passed = None
        if pending is not None:
            skip = pending_already_cut(project, pending)
            if skip:
                # #6152: a pending opened before this guard (the real 11av at
                # 10:00Z, base already tagged 11au), or an empty candidate left
                # by an earlier tick, must not resume and tag a tree a release
                # already covers. Drop its integration candidate (lease) and
                # record its slot as skipped, then let a due slot proceed.
                earlier, empty = skip
                slot["abandoned_integration"] = {
                    "ref": pending["integration"], "sha": pending["sha"]}
                try:
                    drop_abandoned(project, slot, save)
                except rc.ReleaseError as dropped:  # retried with the next tick
                    print("release: %s" % dropped)
                passed = _skip_cutoff(
                    cfg, db, project, slot, pending["cutoff_at"],
                    pending.get("label") or label, source, now, save,
                    version=earlier,
                    head=pending.get("head") or pending["base"],
                    base=pending["base"], empty=empty)
                pending = None
        if pending is not None and pending.get("handoff"):
            _handoff_reannounce(cfg, db, project, pending, save)
            outcome = _handoff_resolution_guarded(cfg, db, project, pending, save)
            if outcome is None:
                return _handoff_waiting(cfg, db, project, pending, at, label, now, save)
            kind, detail = outcome
            if kind == "attested":
                pending["staging_attestation"] = detail
                return _close(cfg, db, project, slot, pending, "released", save)
            # The pilot skipped: this cutoff closes without staging and the due
            # one starts now, even past its window (it was held, not missed).
            pending["handoff_skipped"] = detail
            passed = _close(cfg, db, project, slot, pending, "staging-unattested", save)
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
            drop_abandoned(project, slot, save)
            head, base, main, reading, proof = snapshot(project)
            previous = previous_release(project, main)
            skipped = already_cut(project, base, previous)
            if skipped:
                # #6152: never cut a tree a manual (or earlier) cutoff already
                # tagged; the slot is skipped, not missed, before any tag.
                return _skip_cutoff(cfg, db, project, slot, at, label, source,
                                    now, save, version=skipped, head=head, base=base)
            value = choose_version(project, version)
            sha = merge_candidate(project, base, main)
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
        if (not pending["empty"] and project.staging_handoff
                and "migration_safety" not in pending.get("handoff_preflight", {})):
            try:
                pending["handoff_preflight"] = handoff_preflight(project, pending)
            except rc.TransientReleaseError:
                # #6039 N9: a git timeout or a transient read keeps the
                # pending for the next tick; it must not close the cutoff in
                # `refused-preflight` (which would require `--force`).
                raise
            except subprocess.TimeoutExpired as exc:
                raise rc.TransientReleaseError(
                    "Nexlink staging handoff preflight unavailable: %s" % exc) from exc
            except (rc.ReleaseError, subprocess.SubprocessError, OSError) as exc:
                _preflight_refused(cfg, db, project, slot, pending, exc, save, now)
            save()
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
