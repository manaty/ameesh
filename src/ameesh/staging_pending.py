# SPDX-License-Identifier: AGPL-3.0-only
"""#3291: merged fixes that still hold work because staging has not caught up.

Only active holds count (#3297). An explicit dependency is satisfied by the
merge and says nothing about staging, so it is not a deployment wait. A hold
is prose: a `#N` or `migration N` is read only in a clause that names a
deployment wait — a conflict, a PR, a package, a freeze or a review is not
one, and `release-check` is not a `release` (#8308). A wait for a review, an
opinion or a verdict is not a deployment wait either: it is excluded with its
whole clause. #7893: a delivered but not closed lot carries no active hold; its
delivery note — the notes written in the delivery stage of the journal — is
read with the same clause rules, so a verification that waits for a merged
fix still counts before staging. Evidence is dated: the last attestation of
each host, not a tag, a package version number or the lot's branch head.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import datetime
import json
import re
import sys
import time
from pathlib import Path

from . import planned_waits, storage

SCHEMA = "ameesh-staging-pending/1"
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
#: #7893: only a full commit id is compared with the local object set; a short
#: id is resolved by git, which knows prefixes and ambiguity.
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_LOT = re.compile(r"(?<![\w/])#([1-9][0-9]*)\b")
_MIGRATION = re.compile(r"\bmigration\s+0*([1-9][0-9]*)\b", re.I)

#: #3297: a hold note is free prose. Clauses end on sentence marks; the dots
#: of a version (`1.20.0`) do not end one.
_CLAUSE = re.compile(r"[;!?\n]+|(?<!\d)\.(?!\d)")
#: #8308: the deployment half of `_WAIT`. A clause that also names one of
#: these keeps its references when a review wait shares the clause: "avis
#: rendu, #8294 en attente de staging" waits for the staging, not the review.
_DEPLOY_WAIT = re.compile(
    r"\bdéploiement\b|\bdéploy[ée]e?s?\b|\bdeploy(?:ed|ment|s|ing)?\b(?!-)|"
    r"\bstaging\b|\brelease\b(?![-‑–—_ ]?checks?\b)|\bmise\s+à\s+jour\b|"
    r"\b(?:pas|non)\s+(?:encore\s+)?(?:actif|active)\b|"
    r"\bn'est\s+pas\s+(?:encore\s+)?(?:actif|active|dans)\b|"
    r"\bdoit\s+porter\b|\bdans\s+le\s+tag\b|\bdans\s+la\s+release\b|"
    r"\bdans\s+la\s+version\b",
    re.I)
#: A reference is a deployment wait only in a clause that says so. A bare
#: `#3266` in "conflit avec #3266" is not a wait, and neither is a reference
#: to a PR, a ticket, a package or a review. Explicit wait words count
#: ("attendre #3111"); "bloqué par #X" does not — it states an execution
#: dependency, which is satisfied at merge. #8308: `release-check` is the
#: name of a guard, not a release to deploy.
_WAIT = re.compile(
    r"\battend(?:s|re|u)?\b|\battente\b|\bwait(?:ing)?\b|"
    r"\babsent\w*\b|\bmanque\w*\b|\bne\s+contient\b|"
    r"\b(?:pas|non)\s+(?:encore\s+)?(?:appliqué|appliquée)\b|"
    + _DEPLOY_WAIT.pattern,
    re.I)
#: #8308: a wait for a review, an opinion or a verdict is not a deployment
#: wait. The whole clause is excluded, not only the reference within 32
#: characters: "les lots (#1, #2) restent en attente d'avis" must name no
#: deployment. The link has to be grammatical ("attente d'avis", "verdict
#: attendu"): a mere "avis" elsewhere in a deployment clause keeps counting,
#: and `_deployment_refs` also keeps a clause that names a deployment wait
#: (B of mty-amh-impl-12: "avis rendu, #8294 en attente de staging").
_REVIEW_WAIT = re.compile(
    r"\battente\s+(?:d[’']\s*|de\s+|du\s+|des\s+)?"
    r"(?:l[’']|la\s+|le\s+|les\s+|un\s+|une\s+)?"
    r"(?:avis|relectures?|revues?|verdicts?|reviews?)\b|"
    r"\battend\w*\s+(?:l[’']|la\s+|le\s+|les\s+|un\s+|une\s+)?"
    r"(?:avis|relectures?|revues?|verdicts?|reviews?)\b|"
    r"\b(?:avis|relectures?|revues?|verdicts?|reviews?)\b[^;!?\n]*?"
    r"(?:\battend(?:u|ue|us|ues)\b|\ben\s+attente\b)|"
    r"\bwait(?:ing)?\s+for\s+(?:(?:a|an|the)\s+)?(?:review|verdict)\b|"
    r"\bawait(?:ing|s|ed)?\s+(?:(?:a|an|the)\s+)?(?:review|verdict)\b|"
    r"\b(?:review|verdict)\s+(?:pending|awaited)\b",
    re.I)
#: What makes a `#N` something else than a lot to deploy. Read in a short
#: window before the reference (the form "ticket IMA #796" is also caught
#: after it by `_NOT_A_LOT_AFTER`).
_NOT_A_LOT = re.compile(
    r"\b(?:conflit|écart[ée]e?s?|exclu[es]?|remplac[ée]e?s?|pas\s+le\s+lot|"
    r"relu\s+par|relecture|verdict|"
    r"PR|pull\s+request|issue|ticket|paquet|package|panier|basket|unit|"
    r"véhicule|vehicle|décision|decision|approval|gel|freeze|note|commit|"
    r"migration)\b", re.I)
_NOT_A_LOT_AFTER = re.compile(r"^\s*\(\s*(?:ticket|issue|PR|pull)\b", re.I)


def _deployment_refs(note) -> tuple[set[int], set[int]]:
    """Lots and migrations a hold really waits for at staging (#3297).

    Every `#N` of a hold note used to count, so a conflict (#3266), a PR, a
    package, a freeze or a review reference inflated the threshold. Only a
    reference inside a clause that ties it to a deployment wait counts.
    #8308: a clause that waits for a review, an opinion or a verdict is
    excluded whole, unless it also names a deployment wait; `release-check`
    is not a release.
    """
    lots, migrations = set(), set()
    for clause in _CLAUSE.split(str(note or "")):
        # #8308: a review wait excludes its whole clause; but when the same
        # clause names a deployment wait, the deployment is what remains (B
        # of mty-amh-impl-12: "avis rendu, #8294 en attente de staging").
        if _REVIEW_WAIT.search(clause) and not _DEPLOY_WAIT.search(clause):
            continue
        if not _WAIT.search(clause):
            continue
        for match in _LOT.finditer(clause):
            before = clause[max(0, match.start() - 32):match.start()]
            after = clause[match.end():match.end() + 24]
            if _NOT_A_LOT.search(before) or _NOT_A_LOT_AFTER.search(after):
                continue
            lots.add(int(match.group(1)))
        migrations.update(int(number) for number in _MIGRATION.findall(clause))
    return lots, migrations


def _merge_attestation(out: dict, data, at: float, source: str) -> None:
    """Merge one attestation into the latest-state-per-host view."""
    if not isinstance(data, dict):
        return
    sha = str(data.get("sha") or "").lower()
    if not _SHA.fullmatch(sha) or not isinstance(data.get("hotes"), dict):
        return
    reportes = data.get("reportes") or {}
    if not isinstance(reportes, dict):
        return
    for host, detail in reportes.items():
        raison = (str(detail.get("raison") or "").strip()
                  if isinstance(detail, dict) else "")
        if not raison:
            continue
        if host not in out or at >= out[host]["at"]:
            out[host] = {"sha": None, "absent": True, "raison": raison,
                         "at": at, "attestation": source}
    for host, detail in data["hotes"].items():
        current = str(detail.get("actif") or "").lower()
        if not _SHA.fullmatch(current) or not (
                sha.startswith(current) or current.startswith(sha)):
            continue
        if host not in out or at >= out[host]["at"]:
            out[host] = {"sha": sha, "absent": False, "at": at,
                         "attestation": source}


def _stamp(data, fallback: float) -> float:
    """The attestation's ISO date as epoch, or `fallback` when unreadable."""
    stamp = data.get("date") if isinstance(data, dict) else None
    if isinstance(stamp, str):
        try:
            return datetime.datetime.fromisoformat(
                stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def active_hosts(db=None) -> dict[str, dict]:
    """Latest attested state per host; a reported poste is kept as absent.

    A newer attestation supersedes an older one (including a deliberate
    rollback or a reported absence, #3848). An unknown commit is never resolved
    by substituting a tag. A reported host has `sha: None` and `absent: True`:
    callers that compare content (`regressions`, `waiting`) skip it, so an
    absent poste holds neither staging nor lots.

    #3981: the local files are read first, then the shared `ameesh_events`
    attestations (when `db` is given) — a staging launched from a VM writes the
    shared proof on HOTE_OUTILS, not on this host. The most recent date wins.
    """
    from . import release_cutoff as rc
    folder = Path(rc.attestation_path("0" * 40)).parent
    out = {}
    for path in sorted(folder.glob("actif-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _merge_attestation(out, data, _stamp(data, path.stat().st_mtime),
                               str(path))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    for data in rc.stored_attestations(db):
        try:
            _merge_attestation(out, data, _stamp(data, 0.0),
                               "ameesh_events:%s" % (data.get("sha") or "?"))
        except (TypeError, AttributeError):
            continue
    return out


def regressions(project, candidate: str, active=None, *, unknown=None,
                db=None) -> dict[str, str]:
    """Hosts whose attested content would be lost by deploying candidate.

    An unresolvable attestation refuses the action rather than permitting a
    rollback because this clone has not fetched that commit yet. The read-only
    report passes `unknown` to record that host instead of failing every tick
    (#3297); the release and staging paths keep the default refusal.
    """
    from . import release_cutoff as rc
    out = {}
    if project.name != "ameesh":
        return out
    for host, evidence in (active_hosts(db) if active is None else active).items():
        # un poste reporte ne porte aucun contenu a preserver (#3848)
        if evidence.get("absent"):
            continue
        try:
            sha = rc.commit_of(project.repo, evidence["sha"])
        except rc.TransientReleaseError:
            raise
        except rc.ReleaseError:
            if unknown is None:
                raise
            unknown.append({"host": host, "active": evidence["sha"],
                            "reason": "active commit absent from the release clone"})
            continue
        result = rc._git(project.repo, "merge-base", "--is-ancestor", sha, candidate)
        if result.returncode not in (0, 1):
            raise rc.TransientReleaseError("cannot compare active %s to candidate %s"
                                           % (sha[:12], candidate[:12]))
        if result.returncode:
            out[host] = sha
    return out


def _merged(milestones):
    rows = [m for m in milestones if m.get("kind") == "merged"
            and not m.get("annulled") and m.get("sha")]
    return max(rows, key=lambda m: (float(m.get("at_ts") or 0), int(m.get("id") or 0)),
               default=None)


def _local_commits(repo) -> set[str] | None:
    """#7893: full SHAs of the commit objects present in the release clone.

    A merged head absent from the clone is unknown evidence (#3297), but
    asking git for one on the partial clone of the release makes it attempt a
    lazy fetch for that head, about ten seconds apiece on the real
    repository. The delivery notes multiply the heads (#7893), so one
    enumeration of the local objects answers every presence check. `None`
    when the enumeration itself is unreadable: the caller then keeps the
    per-reference resolution of `commit_of`.
    """
    from . import release_cutoff as rc
    try:
        proc = rc._git(repo, "cat-file", "--batch-all-objects",
                       "--batch-check=%(objectname) %(objecttype)")
    except rc.ReleaseError:
        return None
    if proc.returncode:
        return None
    out = set()
    for line in (proc.stdout or "").splitlines():
        name, _, kind = line.partition(" ")
        if kind.strip() == "commit":
            out.add(name)
    return out


def _ancestors(repo, sha) -> set[str]:
    """#7893: commits reachable from `sha`, in one git process.

    `merge-base --is-ancestor` spawns one process per (merged head, host) and
    the delivery notes multiply the heads; one `rev-list` per host commit
    gives the same answer for every pair."""
    from . import release_cutoff as rc
    proc = rc._git(repo, "rev-list", sha)
    if proc.returncode:
        raise rc.TransientReleaseError("unreadable staging ancestry")
    return set((proc.stdout or "").split())


def waiting(db, project, *, active=None) -> dict:
    """Read-only, shared by the staging alert and the auditor's throughput view."""
    from . import release_cutoff as rc, work
    st = storage.of(db)
    all_rows = st.work.items(state=None, assignee=None, limit=None)
    rows = {int(r["id"]): r for r in all_rows}
    candidates = [r for r in all_rows
                  if r.get("state") in (
                      lifecycle.REQUESTED,
                      lifecycle.IN_PROGRESS,
                      lifecycle.QA,
                      lifecycle.VALIDATED,
                      lifecycle.BLOCKED,
                  )]
    # #7893: a delivered but not closed lot is not held any more; its
    # delivery note can still name the deployment wait of its verification.
    delivered = [r for r in all_rows if r.get("state") in lifecycle.DELIVERED]
    ids = [int(r["id"]) for r in candidates]
    holds = planned_waits.active_holds(
        db, st.progress.lot_hold_events(ids, planned_waits.HOLD_NOTE_PATTERNS))
    # #3297: an explicit dependency is satisfied by the merge; it does not say
    # that the blocker must be active on the hosts. Only active holds are read.
    refs: dict[int, set[int]] = {}
    # A migration reference is resolved only against declared reservations of
    # this project. No bare integer or ticket number can become a migration.
    migrations = {}
    for row in all_rows:
        if row.get("app") != project.name:
            continue
        reservations = work.reserved_migrations(row)
        if row.get("migration_landed") is not None:
            reservations = [{"number": int(row["migration_landed"])}]
        for reservation in reservations:
            migrations.setdefault(reservation["number"], set()).add(int(row["id"]))
    for ident, by_actor in holds.items():
        for hold in by_actor.values():
            lots, numbers = _deployment_refs(hold["note"])
            refs.setdefault(ident, set()).update(lots)
            for number in numbers:
                refs[ident].update(migrations.get(number, ()))
    # #7893: the delivery note is read with the same #3297 clause filter, so a
    # PR, a conflict, a package or a review reference still names no wait.
    for event in st.progress.lot_delivery_notes([int(r["id"]) for r in delivered]):
        lots, numbers = _deployment_refs(event.get("note"))
        if not lots and not numbers:
            continue
        ident = int(event["work_item_id"])
        refs.setdefault(ident, set()).update(lots)
        for number in numbers:
            refs[ident].update(migrations.get(number, ()))
    active = active_hosts(db) if active is None else active
    # #3848 : un poste reporte absent ne retient ni staging ni lots. Seuls les
    # hotes reellement actifs sont compares.
    actifs_reels = {host: evidence for host, evidence in active.items()
                    if not evidence.get("absent")}
    needed, unknown = [], []
    milestones: dict[int, dict | None] = {}
    commits: dict[str, str | None] = {}
    # #7893: the delivery notes multiply the merged heads; one local-object
    # enumeration avoids a lazy fetch per absent head, and one `rev-list` per
    # host commit avoids a `merge-base` process per pair.
    local = _local_commits(project.repo)
    ancestors: dict[str, set[str]] = {}

    def commit(ref):
        # #3297: a merged head absent from this clone (squashed or unfetched
        # branch) is unknown evidence, not an unreadable report on every tick.
        if ref not in commits:
            text = str(ref or "").strip()
            if (local is not None and _FULL_SHA.fullmatch(text.lower())
                    and text.lower() not in local):
                commits[ref] = None
            else:
                try:
                    commits[ref] = rc.commit_of(project.repo, ref)
                except rc.TransientReleaseError:
                    raise
                except rc.ReleaseError:
                    commits[ref] = None
        return commits[ref]

    def is_ancestor(ancestor, current):
        if current not in ancestors:
            ancestors[current] = _ancestors(project.repo, current)
        return ancestor in ancestors[current]

    for ident, blockers in sorted(refs.items()):
        for blocker in sorted(blockers):
            source = rows.get(blocker)
            if not source or source.get("app") != project.name or blocker == ident:
                continue
            if blocker not in milestones:
                milestones[blocker] = _merged(st.work.milestones(blocker, limit=None))
            merged = milestones[blocker]
            if not merged:
                continue
            sha = commit(merged["sha"])
            if sha is None:
                unknown.append({"lot": ident, "blocker": blocker, "sha": merged["sha"],
                                "reason": "merged commit absent from the release clone"})
                continue
            missing_hosts = []
            for host, evidence in actifs_reels.items():
                current = commit(evidence["sha"])
                if current is None:
                    unknown.append({"lot": ident, "blocker": blocker, "sha": sha,
                                    "host": host, "active": evidence["sha"],
                                    "reason": "active commit absent from the release clone"})
                    continue
                if not is_ancestor(sha, current):
                    missing_hosts.append(host)
            if not actifs_reels:
                unknown.append({"lot": ident, "blocker": blocker, "sha": sha,
                                "reason": "no active host attestation"})
            elif missing_hosts:
                needed.append({"lot": ident, "app": rows[ident].get("app"),
                               "blocker": blocker, "sha": sha,
                               "hosts": sorted(missing_hosts), "merged_at": merged.get("at_ts")})
    return {"schema": SCHEMA, "project": project.name, "active": active,
            "waiting": needed, "unknown": unknown,
            "lots": sorted({r["lot"] for r in needed})}


def _window(rules, now):
    policy = rules.staging
    cutoff = rules.next_cutoff(now) - now
    packet = rules.next_packet(now) - now
    return {"before_cutoff_seconds": cutoff, "before_packet_seconds": packet,
            "window_ok": cutoff >= policy.before_cutoff_seconds
                         and packet > policy.before_packet_seconds}


def inspect(cfg, db, project, *, now: float) -> dict:
    from . import release_cutoff as rc
    rules, _source = rc.compiled_rules(cfg, project.name)
    policy = getattr(rules, "staging", None)
    if policy is None or project.name != "ameesh":
        return {"schema": SCHEMA, "project": project.name, "result": "off",
                "reason": "staging rule or host attestation reader absent"}
    rc.fetch(project.repo, project.remote, project.target)
    report = waiting(db, project)
    report["threshold"] = policy.waiting_lots_min
    report["result"] = "needed" if len(report["lots"]) >= policy.waiting_lots_min else "below-threshold"
    if report["result"] != "needed":
        return report
    head = rc.commit_of(project.repo, "%s/%s" % (project.remote, project.target))
    candidate = head if rc.commit_is_green(project.repo, project, head) else rc.last_green(
        project.repo, head, project)
    report["candidate"] = candidate
    report.update(_window(rules, now))
    unresolved = []
    report["regressions"] = (
        regressions(project, candidate, report["active"],
                    unknown=unresolved, db=db) if candidate else {})
    if unresolved:
        report["unknown"].extend(unresolved)
    # A last-green commit before the merged fix would deploy nothing useful.
    missing = set()
    for row in report["waiting"]:
        if not candidate or rc._git(project.repo, "merge-base", "--is-ancestor",
                                    row["sha"], candidate).returncode != 0:
            missing.add(row["lot"])
    report["candidate_unblocks"] = sorted(set(report["lots"]) - missing)
    report["candidate_ready"] = (len(report["candidate_unblocks"]) >= policy.waiting_lots_min
                                 and not unresolved)
    return report


def apply(cfg, db, project, *, now: float, actor=None) -> dict:
    """Pilot/bureau gesture: the usual staging script, no version or tag."""
    from . import release_cutoff as rc, staging_watch, work
    project = rc.with_staging_topology(cfg, project)
    work.corrector(cfg, db, actor, what="staging", history=False)
    started = time.monotonic()
    # Nonblocking lease: never wait through a cutoff then use an old plan.
    key = rc.land.lock_key(project, project.target)
    owner = "staging:" + rc.uuid.uuid4().hex
    # #5450: the global fallback is still followed by the operator delay
    # before the stop, then by the normal failure path and its unit grace.
    ttl = max(project.lock_ttl_s,
              project.staging_timeout_s + staging_watch.STOP_AFTER_ALERT_S
              + rc.STAGING_UNIT_GRACE_S
              + rc.STAGING_KILL_GRACE_S + rc.STAGING_DRAIN_S)
    epoch = storage.of(db).work.land_lock(key, owner, ttl)
    if epoch is None:
        raise rc.ReleaseBusy("target lease held by train/release")
    try:
        report = inspect(cfg, db, project, now=now)
        if (report["result"] != "needed" or not report.get("candidate")
                or not report.get("candidate_ready") or report.get("regressions")):
            return report
        rules, _source = rc.compiled_rules(cfg, project.name)
        report.update(_window(rules, now + time.monotonic() - started))
        if not report["window_ok"]:
            return report
        if storage.of(db).work.land_lock(key, owner, ttl, epoch=epoch) is None:
            raise rc.ReleaseBusy("target lease lost before staging")
        included = sorted({r["blocker"] for r in report["waiting"]
                           if r["lot"] in report["candidate_unblocks"]})
        report["staging"] = rc._staging_step(project, report["candidate"],
                                             included, cfg.state_dir, db=db)
        # #7904: the elected executor hands a normal request to its queue
        # (exit 75, then None in `_staging_step`). That is not a failure: the
        # report names the request and the next tick reconciles its
        # attestation. A real non-zero staging exit stays "failed".
        if report["staging"].get("queued"):
            report["result"] = "queued"
        else:
            report["result"] = ("staged"
                                if report["staging"].get("exit") == 0 else "failed")
        return report
    finally:
        storage.of(db).work.land_unlock(key, owner, epoch)


def tick(cfg, db, project, *, now: float) -> dict:
    """Alert on the existing release timer, once per candidate/waiting set."""
    from . import release_cutoff as rc
    report = inspect(cfg, db, project, now=now)
    lock = str(Path(cfg.state_dir) / "release" / "release.lock")
    with rc._locked(lock):
        return _notify(cfg, db, project, report)


def skipped_retired(project, alert: str, target: str, report: dict) -> None:
    """Un destinataire retiré n'interrompt pas les autres (#7851).

    `mail.send` refuse un agent retiré (`RecipientRetired`, #3460). À l'appel,
    la cible est écartée, son nom est consigné dans le rapport
    (`skipped_retired`) et sur la sortie d'erreur — le journal du tick —
    jamais l'exception ne remonte et n'empêche les autres cibles d'être
    servies.
    """
    target = str(target)
    report.setdefault("skipped_retired", []).append(target)
    label = "staging-needed" if alert == "pending" else "staging-deferred"
    print("%s %s: recipient %s is retired; skipped"
          % (label, project.name, target), file=sys.stderr)


def _notify(cfg, db, project, report):
    from . import mail, release_cutoff as rc, work
    state = rc.load_state(cfg)
    slot = rc.project_state(state, project.name)
    if report["result"] != "needed":
        if slot.pop("staging_alerts", None) is not None:
            rc.save_state(cfg, state)
        return report
    fingerprint = json.dumps([report["lots"], report["candidate"], report["active"]], sort_keys=True)
    sent = slot.setdefault("staging_alerts", {})
    notify = ((getattr(cfg, "packet_watch", {}) or {}).get("projects", {})
              .get(project.name, {}).get("notify", ()))
    if isinstance(notify, str):
        notify = [notify]
    if not notify:
        from . import sous_utilisation
        notify = sous_utilisation.alert_orchestrators(
            cfg, db, storage.of(db).operations.listing(), declared=())
    # #7634 : la route humaine est l'avatar du responsable du projet, lu au
    # canon ; aucun nom d'avatar n'est connu du cœur. Sans avatar déclaré,
    # l'agent configuré du projet (`report`) garde la route d'avant.
    # #7851 : un avatar retiré ou absent du registre ne prend pas la route —
    # `mail.send` le refuserait et les autres cibles seraient perdues ; le
    # `report` configuré reprend, comme sans avatar.
    from . import avatars, staging_deferred
    try:
        rows = storage.of(db).operations.listing() if db is not None else ()
    except Exception:  # noqa: BLE001 - sans registre lisible, la route d'avant
        rows = ()
    owner = staging_deferred.owner_human(cfg, db, project, rows)
    avatar = avatars.name_for(cfg, owner, rows) if owner else None
    targets = set(notify)
    if avatar:
        targets.add(avatar)
    elif project.report:
        targets.add(str(project.report))
    targets = sorted(targets)
    text = ("Staging %s : %d lots attendent des correctifs fusionnés non actifs : %s. "
            "Candidate verte : %s. Vérifier puis lancer ameesh release staging-needed "
            "--project %s --apply (sans tag/version)."
            % (project.name, len(report["lots"]), ", ".join("#%d" % n for n in report["lots"]),
               report["candidate"] or "aucune", project.name))
    for target in targets:
        if sent.get(target) == fingerprint:
            continue
        try:
            mail.send(db, work.SYSTEM_SENDER, target, text, kind="event",
                      payload={"urgent": True, "staging_pending": report})
        except storage.RecipientRetired:
            # #7851 : retiré entre la lecture du registre et le dépôt ; la
            # cible est écartée et tracée, les autres reçoivent leur alerte.
            skipped_retired(project, "pending", target, report)
        sent[target] = fingerprint
        rc.save_state(cfg, state)
    return report
