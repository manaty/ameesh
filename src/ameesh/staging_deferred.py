# SPDX-License-Identifier: AGPL-3.0-only
"""#7365: a reported staging server is a version lag, not a permanent state.

The staging sequence deploys the four VMs of the mesh (``STAGING_VMS``) and
may report the workstation, which updates at its return, and a server without
root access. Canon §14 allows a report, never a permanent one: the next cutoff
never goes back below the last commit, and a reported host is caught up.

The sequence cannot catch up a server without root itself: in server mode
``vm.sh`` runs ``ssh root@$VM``, installs ``/opt/ameesh`` and writes
``/root/.cache/ameesh``, and the staging account holds no root path. The
gesture is the bureau's, after each cutoff, under an owner root right (§10).
This module is its guard, run by the release timer: the latest tagged staging
attestation names each reported host except the workstation, its reason and
the last tag that proved it, and mails the pilot and the bureau once per
release. The gesture is declared after each cutoff instead of being forgotten.

A workstation report is normal and is never alerted. No readable attestation,
no tag, an unreadable state: nothing is concluded, no mail is sent.

A later attestation lifts the alert only when its host proof is a commit: the
host's active commit must be the tagged one or a descendant of it (a rollback
or an old catch-up at an ancestor is still a lag, #7365 review).

#7372: the owner gave the bureau a standing root right for this gesture
(decision #219407, signed 2026-11-16, until 2026-12-12, revocable). The
right comes from the project configuration (``staging_rights``: host,
decision, expiry), and the decision is re-read on each pass: a withdrawal or
a superseding decision takes effect without a release. The bureau's four
``vm.sh`` steps stay a manual gesture; after them, it declares the gesture
with ``ameesh release catchup``: a ``staging.catchup`` event carries the
host, the active commit, the attested tag, the result, the author and the
decision. The next cutoff report names the declared gesture. A declaration is
a trace, never a proof: it is a typed value, not a read of the host, so the
lag alert stays until a staging attestation proves the host.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from pathlib import Path

SCHEMA = "ameesh-staging-deferred/1"
#: the state slot key holding the last alert sent, per recipient
SLOT = "staging_deferred_alerts"

#: #7372: event type of a declared catch-up (the trace of the bureau's gesture)
CATCHUP_EVENT = "staging.catchup"
CATCHUP_SCHEMA = "ameesh-staging-catchup/1"
CATCHUP_DAYS = 120
CATCHUP_LIMIT = 500

#: #7372: no client host, decision or date is compiled here. A project's
#: `staging_rights` (release.projects.<project>.staging_rights) names the host,
#: the owner decision and its expiry; the decision itself is re-read on each
#: use (answered, signed, not withdrawn or superseded), so a revocation takes
#: effect on the next pass. A right is not an automation: the four `vm.sh`
#: steps stay the bureau's gesture; this module records and reports it, and
#: the guard keeps the lag until a staging attestation proves the host.


def _registry_rows(db):
    from . import registry
    try:
        return list(registry.overview(db))
    except Exception:  # noqa: BLE001 - an unreadable registry never blocks the alert
        return []


def _recipients(cfg, db, project) -> list[str]:
    """The bureau, which performs the gesture, then the project's pilot."""
    from . import alert_briefs
    names = ["bureau"]
    try:
        pilots = alert_briefs.project_pilots(project.name, _registry_rows(db),
                                             cfg=cfg, db=db)
    except Exception:  # noqa: BLE001 - the bureau still receives the gesture
        pilots = []
    for name in pilots:
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _rows(db) -> list[dict]:
    """The shared staging attestations, newest first (unreadable: empty)."""
    from . import release_cutoff as rc
    return [row for row in rc.stored_attestations(db) if isinstance(row, dict)]


def _tagged(rows, by_commit):
    """(index, tag, attestation) of the latest attestation of a release tag."""
    for index, data in enumerate(rows):
        sha = data.get("sha")
        tag = by_commit.get(sha) if isinstance(sha, str) else None
        if tag is not None:
            return index, tag, data
    return None


def _deferred(data) -> dict:
    """The hosts reported by an attestation, except the workstation.

    A `pc-smichea` report is the normal "updated at its return": it is a
    report, not a lag. Any other reported host is a runtime the sequence
    never deploys, which §14 forbids to leave permanent.
    """
    from . import release_cutoff as rc
    reportes = data.get("reportes") or {}
    if not isinstance(reportes, dict):
        return {}
    out = {}
    for host, detail in reportes.items():
        if not isinstance(host, str) or not host or host == rc.STAGING_POSTE:
            continue
        out[host] = detail if isinstance(detail, dict) else {}
    return out


#: a commit written by an attestation (full or abbreviated), never a ref name
_SHA_RE = re.compile(r"\A[0-9a-f]{7,40}\Z")


def _commit(value) -> str | None:
    """The commit an attestation field names, or None when it names none."""
    return value if isinstance(value, str) and _SHA_RE.match(value) else None


def _is_ancestor(repo: str, ancestor: str, descendant: str) -> bool | None:
    """Whether `ancestor` is `descendant` or an ancestor of it.

    None when git cannot answer (missing repository or commit): an unreadable
    proof never lifts the alert (#7365 review).
    """
    if ancestor == descendant:
        return True
    try:
        run = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor",
                              ancestor, descendant],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode == 0:
        return True
    if run.returncode == 1:
        return False
    return None


def _at_least(actif, baseline, repo) -> bool:
    """The host's active commit is the tagged one or a descendant of it."""
    actif, baseline = _commit(actif), _commit(baseline)
    if actif is None or baseline is None:
        return False
    return _is_ancestor(repo, baseline, actif) is True


def right_for(project, host, today=None) -> dict | None:
    """The configured standing owner right covering ``host``, if any.

    ``project`` is a release project carrying ``staging_rights`` (host,
    decision, expiry). ``today`` is a date (UTC now by default). None when no
    right names the host: the §10 gesture then stays a per-gesture decision.
    This reads the configuration only; `active_right` re-reads the decision.
    """
    day = today or _dt.datetime.now(_dt.timezone.utc).date()
    if isinstance(day, str):
        day = _dt.date.fromisoformat(day)
    name = getattr(project, "name", project)
    for right in getattr(project, "staging_rights", ()) or ():
        if right.get("host") != host:
            continue
        expires = str(right.get("expires") or "")
        return {"project": name, "host": host, "decision": right.get("decision"),
                "expires": expires,
                "expired": day > _dt.date.fromisoformat(expires)}
    return None


def active_right(db, project, host, today=None) -> dict | None:
    """The right covering ``host``, re-read against the owner decision.

    A configured right is only usable while its decision is answered, signed,
    withdrawn neither by its requester nor replaced (`superseded_by`), and
    unexpired. An unreadable decision, or any doubt, never grants the gesture;
    the next pass sees a revocation without waiting for a release.
    """
    right = right_for(project, host, today=today)
    if right is None:
        return None
    if right["expired"]:
        return {**right, "active": False, "why": "expired"}
    try:
        from . import decisions
        view = decisions.get(db, right["decision"])
    except Exception as exc:  # noqa: BLE001 - an unreadable authority grants nothing
        return {**right, "active": False, "why": "decision unreadable: %s" % exc}
    answer = (view or {}).get("answer") or {}
    proof = answer.get("proof") if isinstance(answer, dict) else None
    if (not view or view.get("state") != "answered" or view.get("withdrawn")
            or view.get("superseded_by") or not isinstance(proof, dict)
            or not proof.get("digest")):
        return {**right, "active": False,
                "why": "decision not answered, withdrawn or superseded"}
    try:
        signed, why = decisions.verify_proof(db, view)
    except Exception as exc:  # noqa: BLE001 - an unverifiable proof grants nothing
        signed, why = False, "unreadable proof: %s" % exc
    if not signed:
        return {**right, "active": False, "why": "decision proof refused: %s" % why}
    return {**right, "active": True, "why": ""}


def declarations(db, project) -> dict[str, dict]:
    """The newest declared catch-up per host, {} when the log is unreadable.

    The newest event wins: a later failed gesture is the last word on that
    host until an attestation or another declaration says otherwise.
    """
    from . import storage
    try:
        rows = storage.of(db).events.recent_type(
            CATCHUP_EVENT, CATCHUP_DAYS, CATCHUP_LIMIT)
    except Exception:  # noqa: BLE001 - an unreadable trace never proves anything
        return {}
    name = getattr(project, "name", project)
    out = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        data = row.get("data")
        if not isinstance(data, dict):
            continue
        # the row's project column is authoritative (the event payload does
        # not repeat it: `record` takes it as an argument)
        if (row.get("project") or data.get("project")) != name:
            continue
        host = data.get("host")
        if not isinstance(host, str) or not host or host in out:
            continue
        out[host] = data
    return out


def baseline(db, project, host) -> dict | None:
    """The tagged staging attestation that reports ``host``, or None.

    None when the host is not reported by the latest tagged attestation
    (already proved, or no tagged proof at all): there is nothing to catch
    up, and an unreadable history concludes nothing.
    """
    from . import release_cutoff as rc
    rows = _rows(db)
    chosen = _tagged(rows, rc._tags_by_commit(project))
    if chosen is None:
        return None
    _index, tag, data = chosen
    deferred = _deferred(data)
    if host not in deferred:
        return None
    return {"sha": data.get("sha") or "", "tag": str(tag.get("tag") or ""),
            "version": str(tag.get("version") or ""),
            "date": str(data.get("date") or ""),
            "raison": str((deferred.get(host) or {}).get("raison") or "absent")}


def declare(db, project, host, sha, base, result, actor, note="") -> dict:
    """Record the bureau's catch-up gesture (#7372) as a durable trace.

    The declaration is host, active commit, attested tag, result, author and
    the decision that authorized it. It does not by itself prove the host is
    active: the guard re-reads the commit ancestry (fail-closed).
    """
    from . import events
    if result not in ("ok", "failed"):
        raise ValueError("result: 'ok' or 'failed' expected")
    sha = str(sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("sha: the full 40-hex active commit is required")
    right = active_right(db, project, host)
    if result == "ok" and (right is None or not right.get("active")):
        raise ValueError("no active owner root right covers %s" % host)
    name = getattr(project, "name", project)
    payload = {
        "schema": CATCHUP_SCHEMA, "project": name, "host": host, "sha": sha,
        "tag": str((base or {}).get("tag") or ""),
        "baseline": str((base or {}).get("sha") or ""),
        "result": result, "actor": " ".join(str(actor or "").split())[:120],
        "decision": (right or {}).get("decision"),
        "note": " ".join(str(note or "").split())[:500],
        "date": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data = {key: value for key, value in payload.items() if key != "project"}
    event = events.record(
        db, CATCHUP_EVENT, "staging-catchup:%s:%s" % (name, host),
        project=name, **data)
    return {**payload, "event": event}


def annotate(db, project, staging) -> dict:
    """Add the declared catch-ups and standing rights to a report payload.

    Called before the cutoff report is written; an unreadable trace simply
    leaves the report as it was (a report never fails on a declaration read).
    """
    from . import release_cutoff as rc
    if not isinstance(staging, dict):
        return staging
    attestation = staging.get("attestation")
    reportes = attestation.get("reportes") if isinstance(attestation, dict) else None
    if not isinstance(reportes, dict):
        return staging
    hosts = [host for host in reportes if isinstance(host, str)
             and host and host != rc.STAGING_POSTE]
    if not hosts:
        return staging
    found = declarations(db, project)
    staging["catchups"] = {host: found[host] for host in hosts if host in found}
    rights = {host: active_right(db, project, host) for host in hosts}
    staging["rights"] = {host: right for host, right in rights.items() if right}
    return staging


def _evidence(rows, index, host) -> dict | None:
    """The newest evidence on `host` after the tagged attestation.

    A later report (the host was still deferred) outranks an older proof: a
    regression is not a catch-up. A row that does not name the host says
    nothing and is skipped.
    """
    for data in rows[:index]:
        hotes = data.get("hotes") or {}
        preuve = hotes.get(host) if isinstance(hotes, dict) else None
        if isinstance(preuve, dict):
            return preuve
        reportes = data.get("reportes") or {}
        if isinstance(reportes, dict) and host in reportes:
            return None
    return None


def _proved_since(rows, index, host, baseline, repo) -> bool:
    """A newer staging attestation proves the host caught up (#7365).

    This is the other outcome — the root path was given and `--absent`
    removed. The proof must name the host's active commit, at the tagged
    commit or after it: an old catch-up or a rollback at an ancestor leaves
    the lag in place. A report older than a proof is history, not a lag.

    #7372: a declared gesture is deliberately NOT a proof. It is a typed
    value, not a read of the host; it is reported next to the lag and never
    silences it. Only an attestation that read the host can.
    """
    preuve = _evidence(rows, index, host)
    return preuve is not None and _at_least(preuve.get("actif"), baseline, repo)


def _last_proof(rows, start, hosts, by_commit) -> dict:
    """For each host, the newest attestation before `start` that proved it."""
    out = {}
    for host in hosts:
        for data in rows[start + 1:]:
            hotes = data.get("hotes") or {}
            if isinstance(hotes, dict) and host in hotes:
                sha = str(data.get("sha") or "")
                tag = by_commit.get(sha)
                out[host] = {"sha": sha, "date": str(data.get("date") or ""),
                             "tag": (tag or {}).get("tag", ""),
                             "version": (tag or {}).get("version", "")}
                break
    return out


def _text(project, tag, data, deferred, proofs, declarations=None, rights=None) -> str:
    """The alert body: the tag not deployed, the reason, the right, the gesture."""
    hosts = sorted(deferred)
    lines = ["Staging %s : le tag %s (%s) n'est pas déployé sur %s."
             % (project.name, tag["tag"], tag.get("version") or "?", ", ".join(hosts))]
    for host in hosts:
        reason = " ".join(str((deferred[host] or {}).get("raison") or "absent").split())
        lines.append("- %s : reporté — %s" % (host, reason))
        proof = proofs.get(host)
        if proof and proof.get("tag"):
            lines.append("  dernière preuve de staging sur cet hôte : %s (%s, %s)"
                         % (proof["tag"], (proof.get("sha") or "")[:12],
                            proof.get("date") or "date inconnue"))
        elif proof:
            lines.append("  dernière preuve de staging sur cet hôte : commit %s (%s)"
                         % ((proof.get("sha") or "")[:12],
                            proof.get("date") or "date inconnue"))
        else:
            lines.append("  aucune attestation de staging n'a jamais prouvé cet hôte")
        right = (rights or {}).get(host)
        if isinstance(right, dict):
            if right.get("expired"):
                lines.append("  droit root #%d expiré le %s : le geste n'est plus "
                             "autorisé ; demander une décision owner avant de le "
                             "reprendre." % (right.get("decision"), right.get("expires")))
            elif not right.get("active"):
                lines.append("  droit root #%d retiré ou illisible (%s) : demander une "
                             "décision owner avant de le reprendre."
                             % (right.get("decision"), right.get("why") or "inconnu"))
            else:
                lines.append("  droit root du bureau : décision #%d (owner), valable "
                             "jusqu'au %s ; geste déclaré au rapport de coupure et "
                             "tracé (`ameesh release catchup`), sans valeur de preuve."
                             % (right.get("decision"), right.get("expires")))
        declared = (declarations or {}).get(host)
        if isinstance(declared, dict):
            lines.append("  dernier geste déclaré (trace, non prouvé) : commit %s (%s, "
                         "%s, par %s)" % ((declared.get("sha") or "")[:12],
                                          declared.get("result") or "?",
                                          declared.get("date") or "date inconnue",
                                          declared.get("actor") or "auteur inconnu"))
    lines += [
        "§14 : un hôte reporté est rattrapé, jamais reporté en permanence ; la coupure "
        "suivante ne redescend jamais sous ce commit. Ce n'est pas le report du poste "
        "(mis à jour à son retour) : ce serveur n'a pas d'accès root, la séquence ne "
        "peut pas l'installer (vm.sh serveur passe par root).",
        "Geste bureau, après chaque coupure (droit owner §10, ou droit permanent "
        "ci-dessus quand il est actif) : depuis le compte de staging, avec son "
        "BASCULE_ENV (VM = adresse du serveur, PERSONAS_VM vide), "
        "deploy/mise-a-jour/vm.sh verifier ; vm.sh installer %s ; vm.sh redemarrer ; "
        "vm.sh controler. Retour arrière : vm.sh retour. Postgres n'est jamais arrêté."
        % str(data.get("sha") or "?")[:12],
        "Déclarer le geste ensuite : ameesh release catchup --project %s --host %s "
        "--sha <commit actif relevé par controler> [--result failed --note « … »] ; "
        "le rapport de coupure suivant le nomme."
        % (project.name, hosts[0] if len(hosts) == 1 else "<hôte>"),
        "Procédure : docs/DEPLOIEMENT-HOTE-AGENTS.md, « Serveur sans accès root : "
        "geste bureau après chaque coupure ».",
    ]
    return "\n".join(lines)


def tick(cfg, db, project, today=None) -> dict:
    """Alert the pilot and the bureau once per release whose staging reported
    a server. Never blocks a release: an unreadable input concludes nothing."""
    from . import release_cutoff as rc
    report = {"schema": SCHEMA, "project": project.name, "result": "none"}
    rows = _rows(db)
    if not rows:
        return report
    by_commit = rc._tags_by_commit(project)
    chosen = _tagged(rows, by_commit)
    if chosen is None:
        return report
    index, tag, data = chosen
    baseline_sha = data.get("sha")
    repo = getattr(project, "repo", "")
    declared = declarations(db, project)
    deferred = {host: detail for host, detail in _deferred(data).items()
                if not _proved_since(rows, index, host, baseline_sha, repo)}
    if not deferred:
        return report
    proofs = _last_proof(rows, index, deferred, by_commit)
    rights = {host: active_right(db, project, host, today=today) for host in deferred}
    key = json.dumps({"tag": tag["tag"], "hosts": sorted(deferred)}, sort_keys=True)
    report.update({"tag": tag["tag"], "version": tag.get("version") or "",
                   "hosts": sorted(deferred), "key": key,
                   "rights": {host: right for host, right in rights.items() if right},
                   "declarations": {host: declared[host] for host in deferred
                                    if host in declared}})
    lock = str(Path(cfg.state_dir) / "release" / "release.lock")
    with rc._locked(lock):
        state = rc.load_state(cfg)
        slot = rc.project_state(state, project.name)
        sent = slot.setdefault(SLOT, {})
        targets = [name for name in _recipients(cfg, db, project)
                   if sent.get(name) != key]
        if not targets:
            report["result"] = "already-sent"
            return report
        text = _text(project, tag, data, deferred, proofs, declared, rights)
        from . import mail, work
        for target in targets:
            mail.send(db, work.SYSTEM_SENDER, target, text, kind="event",
                      payload={"urgent": True, "staging_deferred": report})
            sent[target] = key
        rc.save_state(cfg, state)
        report.update({"result": "alerted", "recipients": targets})
    return report
