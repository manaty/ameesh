# SPDX-License-Identifier: AGPL-3.0-only
"""Weekly stable channel and single consumer pin (#7666 / #5858, #7636 step 4-5).

External meshes follow one **stable** version per ISO week; the construction
version (the 11:00/23:00 cutoffs) stays free. The designation is code, not
judgement (canon §7), and reads only durable facts:

* week `W` is the ISO week in the compiled canon timezone; its designation
  instant `D(W)` is the first compiled cutoff of its Monday;
* a release tag `vX.Y.Z` reachable from the target is **eligible** when it was
  tagged at least `SOAK_S` before `D(W)`, its commit is green
  (`release_cutoff.commit_is_green`), its staging attestation is complete
  (`release_cutoff.read_attestation`), and its version is not below the
  previous published stable;
* the stable of `W` is the eligible tag with the highest version; with none,
  the previous stable is carried over to `W` unchanged; with neither, `W`
  has no stable.

The publication is an immutable annotated tag `stable/<W>` on the designated
commit. Its message is the channel manifest (`ameesh-stable/1`, JSON); the
consumer's single pin file is a verbatim copy of that message. A published
week is never moved or re-designated: the tag is the durable fact and the
idempotency key of the weekly tick.

The repository is public: the first stable tag of a project is a public
publication and needs a signed owner decision (`--decision`, option
`publish`); afterwards the release tick publishes each week without a gesture.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import re
import subprocess
import tempfile

from . import release_cutoff as rc

#: schema of the channel manifest (tag message and consumer pin file)
SCHEMA = "ameesh-stable/1"
#: the stable tag namespace; `v*` release tags are never listed with it
TAG_PREFIX = "stable/"
#: a candidate must have been tagged this long before the designation instant:
#: at least one later cutoff and a day of staging on the mesh
SOAK_S = 24 * 3600.0
#: the documented file name of the consumer's single pin point
PIN_FILE = "ameesh-stable.json"
#: decision option that approves the first public stable publication
FIRST_OPTION = "publish"
#: the scope clause the signed `publish` option label must contain, so that a
#: decision signed for another publication cannot open this channel
FIRST_SCOPE = "stable-channel:%s@%s"
#: the release tick runs every minute: a week already published, or still
#: awaiting the first decision, is re-read from the remote at most this often
RECHECK_S = 3600.0
#: the fields of a manifest, all required (nullable ones may be null)
FIELDS = ("schema", "channel", "project", "week", "designated_at", "version", "tag",
          "tag_object", "commit", "repository", "release_url", "carried", "previous",
          "decision")
_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class StableError(rc.ReleaseError):
    """The stable channel refuses: the message says what to fix."""


# --------------------------------------------------------------------------
# calendar
# --------------------------------------------------------------------------

def week_of(at: float, rules) -> str:
    """The ISO week label (`2026-W47`) of an instant in the canon timezone."""
    year, week, _ = _dt.datetime.fromtimestamp(at, rules.zone()).isocalendar()
    return "%04d-W%02d" % (year, week)


def designation_at(rules, week: str) -> float:
    """`D(W)`: the first compiled cutoff of the Monday of `week`."""
    match = _WEEK.fullmatch(str(week or ""))
    if not match:
        raise StableError("week %r: YYYY-Www expected" % week)
    try:
        monday = _dt.date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError as exc:
        raise StableError("week %r: %s" % (week, exc)) from exc
    slots = sorted(stamp for stamp, _ in rules.cutoffs_on(monday))
    if not slots:
        raise StableError("the canon declares no cutoff: no stable designation instant")
    return slots[0]


def current_week(rules, now: float) -> tuple[str, float] | None:
    """The week whose designation instant is due at `now`, or None before it."""
    week = week_of(now, rules)
    at = designation_at(rules, week)
    return (week, at) if at <= now else None


def iso(at: float, rules) -> str:
    return _dt.datetime.fromtimestamp(at, rules.zone()).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# git facts
# --------------------------------------------------------------------------

def _git(repo: str, *args: str, check: bool = True, env=None, timeout: float = 120.0) -> str:
    proc = rc._git(repo, *args, env=env, timeout=timeout)
    if check and proc.returncode:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
        raise StableError("git %s: %s" % (args[0], detail or "failed"))
    return proc.stdout or ""


def tag_object(repo: str, tag: str) -> str:
    """The annotated tag object of `tag` (refused for a lightweight tag)."""
    kind = _git(repo, "cat-file", "-t", "refs/tags/%s" % tag).strip()
    if kind != "tag":
        raise StableError("%s is not an annotated tag" % tag)
    return _git(repo, "rev-parse", "refs/tags/%s" % tag).strip()


def published(repo: str) -> dict[str, dict]:
    """The local stable tags, `{week: manifest}` (unreadable messages refused)."""
    out = {}
    listing = _git(repo, "for-each-ref", "--format=%(refname:strip=2)",
                   "refs/tags/%s" % TAG_PREFIX.rstrip("/"))
    for name in listing.split():
        week = name[len(TAG_PREFIX):]
        if not name.startswith(TAG_PREFIX) or not _WEEK.fullmatch(week):
            continue
        out[week] = read_tag(repo, week)
    return out


def read_tag(repo: str, week: str) -> dict:
    """The manifest carried by the local tag `stable/<week>`, checked."""
    ref = "refs/tags/%s%s" % (TAG_PREFIX, week)
    if _git(repo, "cat-file", "-t", ref).strip() != "tag":
        raise StableError("%s%s is not an annotated tag" % (TAG_PREFIX, week))
    # the raw tag object: its message is the manifest byte for byte
    raw = _git(repo, "cat-file", "tag", ref)
    manifest = parse(raw.partition("\n\n")[2])
    if manifest["week"] != week:
        raise StableError("%s%s carries week %s" % (TAG_PREFIX, week, manifest["week"]))
    commit = _git(repo, "rev-parse", "%s^{commit}" % ref).strip()
    if commit != manifest["commit"]:
        raise StableError("%s%s points at %s, its manifest names %s"
                          % (TAG_PREFIX, week, commit[:12], manifest["commit"][:12]))
    return manifest


def remote_weeks(repo: str, remote: str) -> list[str]:
    """The stable weeks published on `remote`, oldest first."""
    proc = rc._git(repo, "ls-remote", "--tags", remote, "refs/tags/%s*" % TAG_PREFIX,
                   timeout=120.0)
    if proc.returncode:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-200:]
        raise rc.TransientReleaseError("git ls-remote %s: %s" % (remote, detail))
    weeks = set()
    for line in (proc.stdout or "").splitlines():
        name = line.partition("\t")[2]
        if name.endswith("^{}"):
            continue
        week = name[len("refs/tags/" + TAG_PREFIX):]
        if name.startswith("refs/tags/" + TAG_PREFIX) and _WEEK.fullmatch(week):
            weeks.add(week)
    return sorted(weeks)


def fetch_week(repo: str, remote: str, week: str) -> dict:
    """Fetch `stable/<week>` from `remote` (never moving a local one), read it."""
    ref = "refs/tags/%s%s" % (TAG_PREFIX, week)
    proc = rc._git(repo, "fetch", "--no-tags", remote, "%s:%s" % (ref, ref), timeout=300.0)
    if proc.returncode:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[-200:]
        raise StableError("git fetch %s %s: %s" % (remote, ref, detail))
    return read_tag(repo, week)


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------

def render(manifest: dict) -> str:
    """The canonical text of a manifest: sorted keys, two spaces, final newline."""
    return json.dumps({key: manifest[key] for key in FIELDS}, ensure_ascii=False,
                      indent=2, sort_keys=True) + "\n"


def parse(text: str) -> dict:
    """A manifest (tag message or pin file), with its shape checked."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise StableError("stable manifest: not JSON (%s)" % exc) from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise StableError("stable manifest: schema %s expected" % SCHEMA)
    missing = [key for key in FIELDS if key not in data]
    extra = sorted(set(data) - set(FIELDS))
    if missing or extra:
        raise StableError("stable manifest: missing %s, unknown %s"
                          % (", ".join(missing) or "none", ", ".join(extra) or "none"))
    if data["channel"] != "stable" or not _WEEK.fullmatch(str(data["week"])):
        raise StableError("stable manifest: channel stable and a YYYY-Www week expected")
    match = rc._TAG.fullmatch(str(data["tag"]))
    if not match or rc.version_text(tuple(int(n) for n in match.groups())) != data["version"]:
        raise StableError("stable manifest: tag %r does not name version %r"
                          % (data["tag"], data["version"]))
    for key in ("commit", "tag_object"):
        if not _HEX40.fullmatch(str(data[key])):
            raise StableError("stable manifest: %s must be 40 hex digits" % key)
    if not isinstance(data["carried"], bool):
        raise StableError("stable manifest: carried must be a boolean")
    return data


# --------------------------------------------------------------------------
# designation (pure)
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Candidate:
    version: str
    tag: str
    commit: str
    at: float
    green: bool
    attested: bool

    def refusal(self, *, before: float, floor: str | None) -> str:
        """Why this tag is not eligible at `before`, or "" when it is."""
        if self.at <= 0 or self.at > before - SOAK_S:
            return "tagged less than %d h before the designation" % (SOAK_S // 3600)
        if floor and rc.version_tuple(self.version) < rc.version_tuple(floor):
            return "below the previous stable %s" % floor
        if not self.green:
            return "CI not green"
        if not self.attested:
            return "no complete staging attestation"
        return ""


def choose(candidates, *, before: float, previous: dict | None) -> tuple[Candidate | None, list]:
    """The eligible candidate with the highest version, and the refusals read.

    Only the candidates newer than the soak bound are inspected beyond the
    first eligible one, so the result never depends on the reading order.
    """
    floor = previous["version"] if previous else None
    refused = []
    best = None
    for row in sorted(candidates, key=lambda c: rc.version_tuple(c.version), reverse=True):
        why = row.refusal(before=before, floor=floor)
        if why:
            refused.append({"tag": row.tag, "reason": why})
            continue
        best = row
        break
    return best, refused


def designate(project, rules, week: str, *, previous: dict | None, tags=None,
              green=None, attested=None, repository: str = "", release_url=None,
              decision: int | None = None) -> dict:
    """The designation report of `week`: `{result, manifest?, refused}`.

    `tags`, `green` and `attested` default to the real readers; tests inject
    them. `result` is `designated`, `carried` or `none`.
    """
    before = designation_at(rules, week)
    green = green or (lambda sha: rc.commit_is_green(project.repo, project, sha))
    attested = attested or (lambda sha: rc.read_attestation(sha) is not None)
    if tags is None:
        # the remote target, refreshed with its tags: a stale local branch
        # would hide an eligible release and freeze the week on an old one
        rc.fetch(project.repo, project.remote, project.target)
        tags = rc.tags(project.repo, "%s/%s" % (project.remote, project.target))
    rows = tags
    floor = previous["version"] if previous else None
    candidates = []
    for row in rows:
        # cheap filters first: the CI and attestation readers are not free
        late = row["at"] <= 0 or row["at"] > before - SOAK_S
        low = floor and rc.version_tuple(row["version"]) < rc.version_tuple(floor)
        candidates.append(Candidate(
            version=row["version"], tag=row["tag"], commit=row["commit"], at=row["at"],
            green=False if late or low else bool(green(row["commit"])),
            attested=False if late or low else bool(attested(row["commit"]))))
        if not late and not low and candidates[-1].green and candidates[-1].attested:
            break  # tags are newest first: the first eligible one is the highest
    best, refused = choose(candidates, before=before, previous=previous)
    report = {"week": week, "designated_at": iso(before, rules), "refused": refused}
    if best is None and previous is None:
        report["result"] = "none"
        return report
    if best is None or (previous and best.commit == previous["commit"]):
        manifest = dict(previous)
        manifest.update({"week": week, "designated_at": iso(before, rules), "carried": True,
                         "previous": previous["week"], "decision": None})
        report.update({"result": "carried", "manifest": manifest})
        return report
    url = release_url(best.tag) if release_url else ""
    report.update({"result": "designated", "manifest": {
        "schema": SCHEMA, "channel": "stable", "project": project.name, "week": week,
        "designated_at": iso(before, rules), "version": best.version, "tag": best.tag,
        "tag_object": tag_object(project.repo, best.tag), "commit": best.commit,
        "repository": repository, "release_url": url, "carried": False,
        "previous": previous["week"] if previous else None, "decision": decision}})
    return report


# --------------------------------------------------------------------------
# publication
# --------------------------------------------------------------------------

def repository_of(project) -> str:
    try:
        return "https://github.com/%s" % rc.github_slug(project)
    except rc.ReleaseError:
        return rc._out(project.repo, "remote", "get-url", project.remote).strip()


def previous_of(published_weeks: dict, week: str) -> dict | None:
    """The latest published manifest strictly before `week`."""
    earlier = sorted(w for w in published_weeks if w < week)
    return published_weeks[earlier[-1]] if earlier else None


def check_first_decision(db, project, decision_id: int | None) -> int:
    """The signed owner decision approving the first public stable publication.

    Validity (answered, current, signed) is not enough: the signed label of
    the answered `publish` option must carry the scope clause
    `stable-channel:<project>@<repository>` of this exact gesture. The
    options are part of the signed document; the decision's project is not.
    """
    from . import decisions
    if not decision_id:
        raise StableError(
            "%s has no published stable yet: the first stable tag is a public publication "
            "and needs a signed owner decision (option %r), --decision ID"
            % (project.name, FIRST_OPTION))
    scope = FIRST_SCOPE % (project.name, repository_of(project))
    view = decisions.get(db, int(decision_id))
    if view is None:
        raise StableError("decision #%s not found" % decision_id)
    answer = view.get("answer") or {}
    if view.get("state") != "answered" or answer.get("option") != FIRST_OPTION:
        raise StableError("decision #%s does not approve %r" % (decision_id, FIRST_OPTION))
    if view.get("project") != project.name:
        raise StableError("decision #%s is for project %s" % (decision_id, view.get("project")))
    labels = [str(o.get("label") or "") for o in view.get("options") or ()
              if isinstance(o, dict) and o.get("key") == FIRST_OPTION]
    if len(labels) != 1 or scope not in labels[0].split():
        raise StableError("decision #%s: its signed %r option does not name the scope %s"
                          % (decision_id, FIRST_OPTION, scope))
    try:
        decisions.require_current(db, view)
    except decisions.DecisionError as exc:
        raise StableError("decision #%s: %s" % (decision_id, exc)) from exc
    ok, why = decisions.verify_proof(db, view)
    if not ok:
        raise StableError("decision #%s: owner approval without a valid proof: %s"
                          % (decision_id, why))
    return int(decision_id)


def publish(project, manifest: dict, *, push: bool = True) -> str:
    """Create `stable/<week>` with the manifest and push it; idempotent.

    An existing tag with the same manifest is success; any other content is a
    refusal (a published week never moves).
    """
    week = manifest["week"]
    name = TAG_PREFIX + week
    text = render(parse(render(manifest)))
    if rc._git(project.repo, "rev-parse", "--verify", "-q", "refs/tags/%s" % name).returncode == 0:
        existing = read_tag(project.repo, week)
        if render(existing) != text:
            raise StableError("%s already exists with another manifest" % name)
    else:
        fd, path = tempfile.mkstemp(prefix="stable-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
            _git(project.repo, "tag", "-a", "--cleanup=verbatim", "-F", path, name,
                 manifest["commit"], env=rc._release_env("ameesh stable"))
        finally:
            os.unlink(path)
    if push:
        remote = rc.remote_tag(project.repo, project.remote, name)
        if remote is None:
            _git(project.repo, "push", project.remote, "refs/tags/%s" % name, timeout=300.0)
        elif remote != manifest["commit"]:
            raise StableError("%s on %s points at %s" % (name, project.remote, remote[:12]))
    return name


def _state_path(cfg, project) -> str | None:
    state_dir = getattr(cfg, "state_dir", None)
    if not state_dir:
        return None
    return os.path.join(state_dir, "release", "stable-%s.json" % project.name)


def _seen(cfg, project) -> dict:
    """The local cache of the last remote reading; the remote tag is the fact."""
    path = _state_path(cfg, project)
    try:
        with open(path or "", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _remember(cfg, project, week: str, result: str, now: float) -> None:
    path = _state_path(cfg, project)
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump({"week": week, "result": result, "at": now}, stream)
    os.replace(temporary, path)


def tick(cfg, db, project, *, now: float, rules=None, decision: int | None = None,
         dry_run: bool = False) -> dict:
    """Designate and publish the due week once; the remote tag is the state."""
    if rules is None:
        rules, _ = rc.compiled_rules(cfg, project.name)
    due = current_week(rules, now)
    if due is None:
        return {"result": "not-due", "week": week_of(now, rules)}
    week, _ = due
    seen = _seen(cfg, project)
    if (not dry_run and decision is None and seen.get("week") == week
            and seen.get("result") in ("published", "awaiting-decision")
            and 0 <= now - float(seen.get("at") or 0) < RECHECK_S):
        return {"result": seen["result"], "week": week, "cached": True}
    weeks = remote_weeks(project.repo, project.remote)
    if week in weeks:
        _remember(cfg, project, week, "published", now)
        return {"result": "published", "week": week}
    known = {w: fetch_week(project.repo, project.remote, w) for w in weeks[-1:]}
    previous = previous_of(known, week)
    if previous is None and not dry_run:
        if decision is None:
            # the weekly tick never opens the series: a human-gated gesture does
            _remember(cfg, project, week, "awaiting-decision", now)
            return {"result": "awaiting-decision", "week": week,
                    "reason": "first public stable: ameesh release stable publish "
                              "--project %s --decision ID" % project.name}
        decision = check_first_decision(db, project, decision)
    slug = repository_of(project)
    report = designate(project, rules, week, previous=previous, repository=slug,
                       release_url=lambda tag: "%s/releases/tag/%s" % (slug, tag),
                       decision=decision if previous is None else None,
                       attested=lambda sha: rc.read_attestation(sha, db) is not None)
    if report["result"] == "none" or dry_run:
        return report
    report["tag"] = publish(project, report["manifest"])
    _remember(cfg, project, week, "published", now)
    return report


# --------------------------------------------------------------------------
# consumer pin
# --------------------------------------------------------------------------

def pin(repo: str, remote: str, week: str | None = None) -> str:
    """The pin file text of `week` (default: the latest published one)."""
    weeks = remote_weeks(repo, remote)
    if not weeks:
        raise StableError("%s publishes no stable version yet" % remote)
    chosen = week or weeks[-1]
    if chosen not in weeks:
        raise StableError("%s has no stable for %s" % (remote, chosen))
    return render(fetch_week(repo, remote, chosen))


def verify(text: str, repo: str, remote: str, *, installed: str | None = None) -> dict:
    """Check a pin file against the published channel (and an installed version).

    The pin must equal the remote `stable/<week>` manifest byte for byte, and
    the release tag's object and commit on the remote must be the pinned ones.
    """
    manifest = parse(text)
    published_text = render(fetch_week(repo, remote, manifest["week"]))
    if text != published_text:
        raise StableError("pin differs from the published %s%s manifest"
                          % (TAG_PREFIX, manifest["week"]))
    peeled = rc.remote_tag(repo, remote, manifest["tag"])
    if peeled != manifest["commit"]:
        raise StableError("%s on %s is %s, not the pinned commit"
                          % (manifest["tag"], remote, (peeled or "absent")[:12]))
    objects = _git(repo, "ls-remote", "--tags", remote, "refs/tags/%s" % manifest["tag"])
    if manifest["tag_object"] not in objects.split():
        raise StableError("%s on %s is not the pinned tag object" % (manifest["tag"], remote))
    if installed is not None and installed != manifest["version"]:
        raise StableError("installed ameesh %s, pinned %s" % (installed, manifest["version"]))
    return manifest
