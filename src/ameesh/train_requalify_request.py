# SPDX-License-Identifier: AGPL-3.0-only
"""#7751 — reviewed canon requalification requested from another host.

The reviewed move (#4037) historically had to run on the train host: it writes
that host's configuration and resolves a bound pilot/design/avatar identity.
When the pilot is on another host, the command could not even be started there.
This module records the exact request in the database instead:

* the **requester** (a bound pilot, design agent or linked avatar) states the
  project, the pin, the gate, the cited ``milestone:<id>`` opinions and the
  trace lot; no lease is taken and no configuration is written;
* the **live train service lease holder** applies it at its next pass, on its
  own configuration, through :func:`ameesh.train_requalify.apply` with
  ``reviewed=True`` — exactly the same guards as
  :func:`ameesh.train_requalify_review.qualify` (exact opinions on the commit,
  current pilot assent, author provenance, opposition, policy floor);
* the holder's name or role widens nothing: authority comes from the cited
  evidence, never from the host. A request whose pin or gate moved is refused
  and named, and its trace is not written.
"""
from __future__ import annotations

import os
import re

from . import (config, mail, storage, train_assembly, train_canon,
               train_requalify, work)
from .train import TrainError

#: exactly the reference form the reviewed qualification accepts
MILESTONE = re.compile(r"milestone:[1-9][0-9]*\Z")
#: a full canon commit: the train never qualifies an abbreviation
FULL_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
SENDER = "ameesh"


def _references(reviews):
    """Deduplicated, ordered ``milestone:<id>`` references; refuse any other."""
    if not reviews:
        raise train_requalify.RequalifyError(
            "a requalification request requires --review milestone:<id>")
    references = sorted({str(reference) for reference in reviews})
    for reference in references:
        if not MILESTONE.fullmatch(reference):
            raise train_requalify.RequalifyError(
                "unsupported review reference %r: milestone:<id> required"
                % reference)
    return references


def _commit(value, what):
    value = str(value or "").strip().lower()
    if not FULL_COMMIT.fullmatch(value):
        raise train_requalify.RequalifyError(
            "%s: a full canon commit is required" % what)
    return value


def request(cfg, db, project, *, lot, reviews, actor, dry_run=False,
            pin=None, gate=None):
    """Persist the reviewed move the live train holder must apply.

    A host that reads the project's train configuration reads the pin there; a
    host without it (the pilot's, the design agent's) states the exact pin it
    wants moved with ``pin``. Any host may record the request; the holder
    re-reads its own configuration and refuses a stale reading. No identity or
    role check happens here: the CLI binds the requester through
    :func:`ameesh.train_requalify_review.actor` before calling this function,
    and the authority of the move remains the pilot opinion checked by
    ``qualify`` on the holder.
    """
    references = _references(reviews)
    raw = getattr(cfg, "train", None) or {}
    if raw:
        try:
            spec = train_assembly.project_of(cfg, project, host_check=False)
        except train_assembly.ConfigurationError as exc:
            raise train_requalify.RequalifyError(str(exc)) from exc
        if pin is None:
            pin = spec.canon_commit
    elif pin is None:
        # This host does not carry the train block (the pilot's, the design
        # agent's): the exact reading must be stated, never guessed.
        raise train_requalify.RequalifyError(
            "no `train` configuration on this host: give --pin, the exact "
            "canon pin to move")
    pin = _commit(pin, "pin")
    if gate is None:
        gate, why = train_canon.gate_commit(db, cfg, project)
        if not gate:
            raise train_requalify.RequalifyError(
                "no gate canon commit to requalify: %s" % (why or "unknown"))
    gate = _commit(gate, "gate")
    if work.get(db, int(lot)) is None:
        raise train_requalify.RequalifyError(
            "lot #%s not found: no trace target" % lot)
    preview = dict(project=project, pin=pin, gate=gate, reviews=references,
                   lot=int(lot), requester=actor, state="requested")
    if dry_run:
        return dict(preview, dry_run=True)
    saved = storage.of(db).work.train_requalify_request(
        project, pin, gate, references, int(lot), actor)
    if saved is None:
        raise train_requalify.RequalifyError(
            "requalification request refused: not stored")
    return saved


def _notify(db, row, text):
    """Tell the requester what the holder did; a mail failure changes nothing."""
    try:
        mail.send(db, SENDER, row["requester"], text,
                  payload={"train_requalification_request": {
                      "id": row["id"], "project": row["project"],
                      "state": row["state"], "pin": row["pin"],
                      "gate": row["gate"], "lot": row["lot"],
                      "error": row.get("error") or ""}})
    except Exception:  # noqa: BLE001 - the durable row is the proof
        pass


def apply_pending(cfg, db, project, *, gate_reader=None, report=None):
    """Apply every pending request of this project with the reviewed guards.

    Called only by the live train service lease holder, on its own host and
    configuration. A refusal (stale pin or gate, missing pilot opinion,
    opposition, unreadable provenance, policy floor) is recorded on the request
    with its exact reason and mailed to the requester; the train continues its
    own pass. A transient error (file system, database) leaves the request
    pending for the next pass.
    """
    outcomes = []
    for row in storage.of(db).work.train_requalify_requests(project):
        try:
            result = train_requalify.apply(
                cfg, db, project, lot=int(row["lot"]), actor=row["requester"],
                gate_reader=gate_reader, reviewed=True, reviews=row["reviews"],
                request=row["id"], expected_pin=row["pin"],
                expected_gate=row["gate"])
        except TrainError as exc:
            saved = storage.of(db).work.train_requalify_resolve(
                row["id"], "refused", actor=cfg.host, error=str(exc))
            if saved is not None:
                if report is not None:
                    report("requalification request #%s refused: %s"
                           % (row["id"], exc))
                _notify(db, saved, "\n".join([
                    "Requalification %s refusée par le train (#7751)." % project,
                    "",
                    "- Demande : #%s ; épingle %s ; porte %s."
                    % (row["id"], row["pin"], row["gate"]),
                    "- Motif : %s" % exc,
                    "- Rien n'a été écrit : la demande est refusée et nommée.",
                ]))
            outcomes.append(dict(id=row["id"], state="refused", error=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - transient: retry next pass
            if report is not None:
                report("requalification request #%s not applied (%s: %s)"
                       % (row["id"], type(exc).__name__, exc))
            outcomes.append(dict(id=row["id"], state="pending",
                                 error="%s: %s" % (type(exc).__name__, exc)))
            continue
        saved = storage.of(db).work.train_requalify_resolve(
            row["id"], "applied", actor=cfg.host)
        if saved is not None:
            if report is not None:
                report("requalification request #%s applied on %s: %s -> %s"
                       % (row["id"], cfg.host,
                          str(result.get("pin") or "")[:12],
                          str(result.get("gate") or "")[:12]))
            _notify(db, saved, "\n".join([
                "Requalification %s appliquée par le train (#7751)." % project,
                "",
                "- Demande : #%s ; épingle %s ; porte %s."
                % (row["id"], row["pin"], row["gate"]),
                "- Appliquée sur %s ; trace sur le lot #%s."
                % (cfg.host, row["lot"]),
            ]))
        outcomes.append(dict(id=row["id"], state="applied",
                             pin=result.get("pin"), gate=result.get("gate")))
    return outcomes


def apply_holder(cfg, *, report=None):
    """The live service holder applies pending requests on its own host.

    The effective configuration is re-read from the same file the service was
    assembled from. No request runs on a standby host: the caller
    (:meth:`ameesh.train_runner.Service.pass_once`) only reaches here while it
    holds the train service lease.
    """
    from .scoped_db import ScopedDb

    config_path = str(getattr(cfg, "config_path", "") or "")
    if not config_path:
        return []
    fresh = config.load(dict(os.environ, AMEESH_CONFIG=config_path))
    raw = getattr(fresh, "train", None) or {}
    if not raw:
        return []
    settings = train_assembly.Settings.parse(raw)
    startup = getattr(cfg, "train", None) or {}
    if startup:
        # Only the pin is hot-reloaded (#3366): a request never runs under a
        # service whose repo, target, role or project set changed since startup.
        train_assembly._same_assembly(
            train_assembly.Settings.parse(startup), settings)
    this_host = str(getattr(fresh, "host", "") or "").strip()
    if settings.role is None and this_host != settings.host:
        return []
    outcomes = []
    for spec in settings.projects:
        db = None
        try:
            db = ScopedDb(fresh, family="runner.scoped.train.requalify")
            outcomes.extend(apply_pending(fresh, db, spec.name, report=report))
        except Exception as exc:  # noqa: BLE001 - the pass keeps its own work
            if report is not None:
                report("requalification requests not applied for %s (%s: %s)"
                       % (spec.name, type(exc).__name__, exc))
        finally:
            if db is not None:
                db.close()
    return outcomes
