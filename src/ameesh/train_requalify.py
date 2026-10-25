# SPDX-License-Identifier: AGPL-3.0-only
"""#3366 — the pilot's mechanical requalification of the train's canon pin.

The train reads its rules at the commit pinned in the host configuration
(``train.projects.<p>.canon_commit``). The automatic host service follows the
audited gate only through this guard (#3483). When the canon has moved and its diff does not touch the two
files that carry the train's rules — ``processus/mecanique.yaml`` and
``federation.yaml`` — the move is mechanical: the command below writes the
gate's audited commit where the pin is declared, and traces the gesture on the
lot the pilot names. Any other diff refuses: a rules change needs a human
review, not a requalification.

It writes **every** configuration file that explicitly carries the pin (the
host ``config.json`` and the profiles that override it), so the recursive
profile merge (#3345) cannot leave one executor behind; it never invents a pin
in a file that inherits it. Each write is atomic.
"""
from __future__ import annotations

import dataclasses
import fcntl
from datetime import datetime, timezone
import json
import os
import tempfile

from . import config, train_assembly, train_canon, work
from .train import TrainError


class RequalifyError(TrainError):
    """The requalification is refused; nothing was written."""


def _read(path):
    try:
        with open(path, encoding="utf-8") as source:
            raw = json.load(source)
    except (OSError, ValueError) as exc:
        raise RequalifyError("configuration file %s unreadable: %s" % (path, exc)) from exc
    if not isinstance(raw, dict):
        raise RequalifyError("configuration file %s: a JSON object is required" % path)
    return raw


def _entry(raw, project):
    """The project's ``train.projects.<p>`` declaration, or None."""
    block = raw.get("train")
    if not isinstance(block, dict):
        return None
    projects = block.get("projects")
    if not isinstance(projects, dict):
        return None
    entry = projects.get(project)
    return entry if isinstance(entry, dict) else None


def _pin(raw, project):
    entry = _entry(raw, project)
    pin = (entry or {}).get("canon_commit")
    return pin if isinstance(pin, str) and pin.strip() else ""


def _canon(raw, project, spec):
    """The canon repository this file reads for the project."""
    entry = _entry(raw, project) or {}
    value = entry.get("canon", spec.canon)
    if not isinstance(value, str) or not value.strip():
        raise RequalifyError("train.projects.%s.canon: a non-empty string is required"
                             % project)
    return os.path.abspath(os.path.expanduser(value.strip()))


def _paths(cfg, project):
    """Every readable configuration file that explicitly carries the pin.

    The host base and the profiles inheriting from it are read with the same
    inheritance rules as :mod:`ameesh.config` (#1466/#3345). The file the
    current process reads is always kept, even when it is autonomous or lives
    outside the profile directory.
    """
    base = str(getattr(cfg, "config_base", "") or "").strip()
    current = str(getattr(cfg, "config_path", "") or "").strip()
    host = base or current
    candidates = []
    if host:
        candidates.append(host)
    if current and current not in candidates:
        candidates.append(current)
    if host:
        audit = config.audit_profiles(extra=(current,) if current else (),
                                      directory=None, host=host)
        if audit.errors:
            raise RequalifyError(
                "refused: unreadable executor profile(s): %s"
                % "; ".join(audit.errors[:3]))
        candidates.extend(audit.profiles)
    found, seen = [], set()
    for path in candidates:
        path = os.path.abspath(path)
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        raw = _read(path)
        if _pin(raw, project):
            found.append((path, raw))
    return found


def _validate(spec, path, raw, project, gate):
    """Refuse unless the gate is a readable canon commit for this file.

    A file may declare its own ``canon`` path; the pin written there must
    resolve in that repository, not only in the effective one.
    """
    canon = _canon(raw, project, spec)
    where = "train.projects.%s (in %s)" % (project, path)
    candidate = dataclasses.replace(spec, canon=canon, canon_commit=gate)
    try:
        train_assembly._canon_commit(candidate, where)
    except train_assembly.ConfigurationError as exc:
        raise RequalifyError(str(exc)) from exc


def _plan(cfg, db, project, *, gate_reader=None):
    try:
        spec = train_assembly.project_of(cfg, project)
    except train_assembly.ConfigurationError as exc:
        raise RequalifyError(str(exc)) from exc
    if gate_reader is None:
        gate, why = train_canon.gate_commit(db, cfg, project)
    else:
        gate, why = gate_reader(db, cfg, project)
    gate = str(gate or "").strip().lower()
    if not gate:
        raise RequalifyError("no gate canon commit to qualify: %s"
                             % (why or "unknown"))
    pin = str(spec.canon_commit or "").strip().lower()
    files = _paths(cfg, project)
    if not files:
        raise RequalifyError(
            "no configuration file carries train.projects.%s.canon_commit"
            % project)
    pins = {_pin(raw, project).strip().lower() for _path, raw in files}
    if pins != {pin}:
        raise RequalifyError(
            "refused: the configuration files disagree on train.projects.%s."
            "canon_commit (%s); align them before requalifying"
            % (project, ", ".join(sorted(pins))))
    canons = {_canon(raw, project, spec) for _path, raw in files}
    if len(canons) != 1:
        raise RequalifyError(
            "refused: the configuration files disagree on train.projects.%s."
            "canon (%s); align them before requalifying"
            % (project, ", ".join(sorted(canons))))
    if pin == gate:
        return {"state": "aligned", "project": project, "pin": pin, "gate": gate,
                "changed": [], "files": [path for path, _raw in files]}, []
    for path, raw in files:
        _validate(spec, path, raw, project, gate)
    canon = canons.pop()
    from . import resolutions
    ancestor = resolutions._run(canon, "merge-base", "--is-ancestor", pin, gate)
    if ancestor.returncode:
        raise RequalifyError("refused: gate is not a descendant of the pin; "
                             "reviewed requalification required")
    changed = train_assembly.canon_diff(canon, pin, gate)
    guard = [name for name in changed if name in train_assembly.REQUALIFY_GUARD]
    if guard:
        raise RequalifyError(
            "refused: the canon diff %s..%s touches %s — a rules change is not "
            "a mechanical requalification; reviewed requalification required"
            % (pin[:12], gate[:12], ", ".join(guard)))
    return {"state": "requalified", "project": project, "pin": pin, "gate": gate,
            "changed": changed, "files": [path for path, _raw in files]}, files


def plan(cfg, db, project, *, gate_reader=None):
    """Compute the requalification; no write, no trace."""
    result, _files = _plan(cfg, db, project, gate_reader=gate_reader)
    return result


def _bytes(path):
    with open(path, "rb") as source:
        return source.read()


def _replace(path, payload):
    """Atomic byte replacement, keeping the file's mode."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    mode = os.stat(path).st_mode & 0o777
    handle, temporary = tempfile.mkstemp(
        dir=directory, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        os.fchmod(handle, mode)
        with os.fdopen(handle, "wb") as output:
            output.write(payload)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write(path, raw):
    """Atomic JSON replacement, keeping the file's mode."""
    _replace(path, (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def apply(cfg, db, project, *, lot, actor, gate_reader=None, record=None):
    """Serialize manual and automatic writers of this host's train pins."""
    directory = os.path.dirname(os.path.abspath(cfg.config_base or cfg.config_path))
    with open(os.path.join(directory, ".train-canon.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _apply(cfg, db, project, lot=lot, actor=actor,
                      gate_reader=gate_reader, record=record)


def _apply(cfg, db, project, *, lot, actor, gate_reader=None, record=None):
    """Requalify the pin, write every declaring file, trace on the lot."""
    result, files = _plan(cfg, db, project, gate_reader=gate_reader)
    if result["state"] == "aligned":
        return result
    if record is None and work.get(db, int(lot)) is None:
        raise RequalifyError("lot #%s not found: no trace target" % lot)
    originals = [(path, _bytes(path)) for path, _raw in files]
    written = []
    try:
        for path, raw in files:
            entry = _entry(raw, project)
            entry["canon_commit"] = result["gate"]
            _write(path, raw)
            written.append(path)
    except BaseException:
        # A multi-file write is atomic per file; if a later one fails, restore
        # the ones already written so no executor is left on a half-move. The
        # original bytes are put back verbatim: a failed command leaves no
        # formatting change behind.
        for path, payload in originals:
            if path in written:
                try:
                    _replace(path, payload)
                except Exception:  # noqa: BLE001 - the original error is the cause
                    pass
        raise
    text = ("canon pin requalified: %s %s -> %s; diff: %s; config: %s"
            % (project, result["pin"][:12], result["gate"][:12],
               ", ".join(result["changed"]) or "none", ", ".join(written)))
    try:
        if record is None:
            work.note(db, int(lot), text, actor=actor)
        else:
            record(result)
    except BaseException:
        # The trace is part of the gesture: without it, put every file back so
        # a retry performs the move and the note together, never a silent pin.
        for path, payload in originals:
            try:
                _replace(path, payload)
            except Exception:  # noqa: BLE001 - the note error is the cause
                pass
        raise
    result["files"] = written
    result["lot"] = int(lot) if lot is not None else None
    result["actor"] = actor
    return result


def execute(cfg, db, project, *, lot, actor, dry_run=False, gate_reader=None):
    """The command body: plan or apply, with one named result."""
    if dry_run:
        result = plan(cfg, db, project, gate_reader=gate_reader)
        result["dry_run"] = True
        return result
    return apply(cfg, db, project, lot=lot, actor=actor, gate_reader=gate_reader)


def automatic(cfg, db, project, *, gate_reader=None):
    """#3483: follow the audited gate independently on every train host.

    All local profiles follow. The durable host journal is required: a write
    failure rolls the pin writes back. A host lock serializes executor profiles.
    """
    directory = os.path.dirname(os.path.abspath(cfg.config_base or cfg.config_path))
    def record(result):
        row = dict(result, host=cfg.host, actor="train", event="canon-requalified",
                   at=datetime.now(timezone.utc).isoformat(),
                   guard=list(train_assembly.REQUALIFY_GUARD))
        with open(os.path.join(directory, "train-canon.jsonl"), "a+",
                  encoding="utf-8") as journal:
            journal.seek(0, os.SEEK_END)
            before = journal.tell()
            try:
                journal.write(json.dumps(row, ensure_ascii=False) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
            except BaseException:
                journal.seek(before)
                journal.truncate()
                raise
    return apply(cfg, db, project, lot=None, actor="train",
                 gate_reader=gate_reader, record=record)
