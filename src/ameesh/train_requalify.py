# SPDX-License-Identifier: AGPL-3.0-only
"""#3366 — the pilot's mechanical requalification of the train's canon pin.

The train reads its rules at the commit pinned in the host configuration
(``train.projects.<p>.canon_commit``). The automatic host service follows the
audited gate through this guard (#3483, #7880). When the canon has moved and
its diff does not touch the two files that carry the train's rules —
``processus/mecanique.yaml`` and ``federation.yaml`` — the move is mechanical:
the command below writes the gate's audited commit where the pin is declared,
and traces the gesture on the lot the pilot names.

A guard-touching diff is followed only when what the train *compiles* for every
project of the train is identical at the pin and at the gate, and the gate's
other readings (``train_canon.gate_rules_gap``) agree with the new pin. That is
the semantic follow-up (#7880): adding a path for another project to
``federation.yaml`` no longer blocks a train whose own rules did not change.
Other rule changes require the explicit reviewed path (#4037): exact recorded
opinions, including a pilot's, are checked before the move, and the train opens
one carrier lot per project/pin so the refusal always has an owner.

It writes **every** configuration file that explicitly carries the pin (the
host ``config.json`` and the profiles that override it), so the recursive
profile merge (#3345) cannot leave one executor behind; it never invents a pin
in a file that inherits it. Each write is atomic.
"""
from __future__ import annotations

import dataclasses
from contextlib import contextmanager
import fcntl
from datetime import datetime, timezone
import json
import os
import re
import tempfile

from . import config, train_assembly, train_canon, work
from .train import TrainError


class RequalifyError(TrainError):
    """The requalification is refused; nothing was written."""


class RequalifyReviewRequired(RequalifyError):
    """The guard changed the train's compiled rules: a reviewed move is due.

    The refusal carries everything needed to open its carrier lot: the two
    commits, the diff, the guard files and the compiled gap. ``automatic``
    opens (or updates) that lot, and the gate alert cites it (#7880).
    """

    def __init__(self, message, *, project, pin, gate, changed=(), guard=(), gap=""):
        super().__init__(message)
        self.project = project
        self.pin = pin
        self.gate = gate
        self.changed = tuple(changed or ())
        self.guard = tuple(guard or ())
        self.gap = gap


#: stable prefix of the carrier lot's source, per project and pin. A newer gate
#: for the same pin updates that open lot instead of stacking another one.
CARRIER_SOURCE = "train-requalification"


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


def _compiled(canon, commit, project):
    """One project's compiled train rules, with the commit identity removed.

    ``mechanics.load`` binds its object to the exact commit and source path;
    two readings of equal rules at different commits differ only there (the
    repair policy carries the commit too). Every other field counts: the
    comparison is strict, never a subset of fields.
    """
    from . import mechanics
    rules = mechanics.load(canon, commit, project)
    repair = rules.repair_policy
    if repair is not None:
        repair = dataclasses.replace(repair, commit="")
    return dataclasses.replace(rules, commit="", source="", repair_policy=repair)


def _compiled_fields(before, after):
    """Top-level fields of two compiled objects that differ."""
    return [field.name for field in dataclasses.fields(before)
            if getattr(before, field.name) != getattr(after, field.name)]


def compiled_rules_gap(cfg, db, *, pin, gate):
    """What the train compiles differently between ``pin`` and ``gate``, or "".

    Every project of the train is compared in its own canon (#7880): the
    compiled object (``mechanics.load``) must be strictly equal at the two
    commits, and the gate's other readings at the gate (``gate_rules_gap``)
    must leave no gap against the new pin. Paths present in the project
    repository are never a filter: only the compiled rules matter. An
    unreadable side is a difference, never a reassuring "unchanged".
    """
    pin = str(pin or "").strip().lower()
    gate = str(gate or "").strip().lower()
    try:
        settings = train_assembly.Settings.parse(getattr(cfg, "train", None) or {})
    except Exception as exc:  # noqa: BLE001 - unreadable train block: never unchanged
        return "train configuration illisible: %s: %s" % (
            type(exc).__name__, str(exc)[:160])
    gaps = []
    for project in settings.projects:
        name = project.name
        try:
            before = _compiled(project.canon, pin, name)
            after = _compiled(project.canon, gate, name)
        except Exception as exc:  # noqa: BLE001 - unreadable rules: a difference
            gaps.append("%s : règles compilées illisibles (%s: %s)"
                        % (name, type(exc).__name__, str(exc)[:160]))
            continue
        fields = _compiled_fields(before, after)
        if fields:
            gaps.append("%s : %s" % (name, ", ".join(fields)))
            continue
        try:
            gap = train_canon.gate_rules_gap(db, cfg, name, project.canon, gate)
        except Exception as exc:  # noqa: BLE001 - unreadable gate: a difference
            gaps.append("%s : lectures de la porte illisibles (%s: %s)"
                        % (name, type(exc).__name__, str(exc)[:160]))
            continue
        if gap:
            gaps.append("%s : %s" % (name, gap))
    return "; ".join(gaps)


def _carrier_source(project, pin):
    return "%s:%s:%s" % (CARRIER_SOURCE, project, str(pin or "").strip().lower())


def _carrier_ref(project, pin, gate):
    return "%s:%s" % (_carrier_source(project, pin),
                      str(gate or "").strip().lower())


def _carrier_title(project, pin, gate):
    return "Train %s : requalification relue du pin canon %s → %s" % (
        project, pin[:12], gate[:12])


_GATE_LINE = "- Porte : "


def _body_gate(body):
    """The full gate commit recorded by :func:`_carrier_body`, or ""."""
    for line in str(body or "").splitlines():
        if line.startswith(_GATE_LINE):
            value = line[len(_GATE_LINE):].strip().lower()
            if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
                return value
    return ""


def _gate_is_newer(canon, current, gate):
    """True when ``gate`` is ``current`` or descends from it (never a regression).

    A standby host can read an older audited gate than the one already carried
    by the open lot. Its passage must not rewrite the lot to that older target;
    the lot keeps the newer reading until the pilot moves the pin.
    """
    if not current or current == gate:
        return True
    if not canon or not os.path.isdir(canon):
        return False
    from . import resolutions
    try:
        proc = resolutions._run(canon, "merge-base", "--is-ancestor", current, gate)
    except Exception:  # noqa: BLE001 - unreadable ancestry: keep the current lot
        return False
    return proc.returncode == 0


def _carrier_body(project, *, pin, gate, canon, changed, guard, gap, pilot):
    diff = ", ".join(changed) if changed else "illisible"
    files = ", ".join(guard) if guard else "aucun"
    command = (
        "python -m ameesh.train_cli --project %s requalify --lot <ce lot> "
        "--reviewed --review milestone:<id> --review milestone:<id> --dry-run"
        % project)
    lines = [
        "Train %s : requalification relue du pin canon %s → %s."
        % (project, pin[:12], gate[:12]),
        "",
        "- Pin : %s" % pin,
        "- Porte : %s" % gate,
        "- Diff : %s" % diff,
        "- Fichiers de règles touchés : %s" % files,
        "- Écart des règles compilées : %s" % (gap or "?"),
        "- Effet : le train %s refuse de composer un nouveau paquet ; un paquet "
        "en vol va au bout." % project,
        "",
        "Requalification relue requise (voie relue #4037, avis du canon) :",
        "1. Relire le diff : `git -C %s diff %s %s`."
        % (canon or "?", pin, gate),
        "2. Poser les avis requis par le canon sur la porte %s."
        % gate[:12],
        "3. Dry-run : `%s`." % command,
        "4. Appliquer sur l'hôte du train (détenteur du service), ou enregistrer "
        "la demande depuis un autre hôte (`--request`, #7751).",
    ]
    if pilot:
        lines.append("- Pilote du canon : %s." % pilot)
    return "\n".join(lines)


def ensure_requalification_lot(cfg, db, project, *, pin, gate, canon="",
                               changed=None, guard=None, gap=None):
    """Open or update the single carrier lot of a refused requalification.

    Idempotent by ``unique_ref`` project+pin+gate. A newer gate on the same pin
    updates the open lot (source project+pin) instead of stacking another one.
    The lot is assigned to the canon's declared pilot role (the orchestrator of
    the canon's project, ``train_canon.canon_pilot_of``) when that agent is
    wakeable; otherwise it stays in the team queue and the body names the role,
    never dropped. The train project's own orchestrator is a different role
    (the drift alert's recipient). Returns the lot id, or None when there is
    nothing to carry (same commit, no guard file, or compiled rules unchanged).
    """
    pin = str(pin or "").strip().lower()
    gate = str(gate or "").strip().lower()
    if not pin or not gate or pin == gate:
        return None
    try:
        spec = train_assembly.project_of(cfg, project)
    except Exception:  # noqa: BLE001 - the alert may run on a partial configuration
        spec = None
    canon = canon or (spec.canon if spec is not None else "")
    if changed is None:
        try:
            changed = train_assembly.canon_diff(canon, pin, gate) if canon else []
        except Exception:  # noqa: BLE001 - unreadable diff: no guard proof
            changed = []
    changed = tuple(changed or ())
    if guard is None:
        guard = tuple(name for name in changed
                      if name in train_assembly.REQUALIFY_GUARD)
    guard = tuple(guard or ())
    if not guard:
        return None
    if gap is None:
        gap = compiled_rules_gap(cfg, db, pin=pin, gate=gate)
    if not gap:
        return None
    try:
        pilot = train_canon.canon_pilot_of(cfg)(db)
    except Exception:  # noqa: BLE001 - no readable role: unassigned carrier
        pilot = ""
    source = _carrier_source(project, pin)
    title = _carrier_title(project, pin, gate)
    body = _carrier_body(project, pin=pin, gate=gate, canon=canon,
                         changed=changed, guard=guard, gap=gap, pilot=pilot)
    with db.transaction() as tx:
        # One writer per project+pin: two hosts (or two passages) never stack
        # carriers for the same refusal, and a newer gate updates this one.
        tx.query("SELECT pg_advisory_xact_lock(hashtextextended(lower(%s), 0))",
                 (source,))
        existing = work.open_with_source(tx, source)
        if existing is not None:
            unchanged = (existing.get("title") == title
                         and existing.get("body") == body)
            if unchanged or not _gate_is_newer(
                    canon, _body_gate(existing.get("body")), gate):
                return int(existing["id"])
            row = work.update_generated(
                tx, int(existing["id"]), title=title, body=body, priority=1,
                note="avance de la porte %s → %s" % (pin[:12], gate[:12]),
                actor=train_canon.SENDER)
            if row is not None:
                return int(existing["id"])
        fields = dict(
            title=title, type="evolution", source=source, app=project, body=body,
            priority=1, actor=train_canon.SENDER, footprint=[],
            unique_ref=_carrier_ref(project, pin, gate))
        created = None
        if pilot:
            try:
                created = work.add(tx, assignee=pilot, **fields)
            except Exception:  # noqa: BLE001 - assignment is best effort, the lot is due
                created = None
        if created is None:
            created = work.add(tx, **fields)
        return int(created["id"])


def ensure_requalification_lot_for_reading(cfg, db, project, reading):
    """The carrier lot for a gate reading, or None when the move is mechanical.

    Used by the alert path: a reading that does not touch the guard, or whose
    compiled rules are unchanged, needs no carrier and returns None.
    """
    pin = str(getattr(reading, "pin", "") or "").strip().lower()
    gate = str(getattr(reading, "gate", "") or "").strip().lower()
    if not pin or not gate or pin == gate:
        return None
    try:
        spec = train_assembly.project_of(cfg, project)
    except Exception:  # noqa: BLE001 - no exact canon: nothing is invented
        return None
    try:
        changed = train_assembly.canon_diff(spec.canon, pin, gate)
    except Exception:  # noqa: BLE001 - unreadable diff: the alert keeps its text
        return None
    guard = [name for name in changed if name in train_assembly.REQUALIFY_GUARD]
    if not guard:
        return None
    gap = compiled_rules_gap(cfg, db, pin=pin, gate=gate)
    if not gap:
        return None
    return ensure_requalification_lot(
        cfg, db, project, pin=pin, gate=gate, canon=spec.canon,
        changed=changed, guard=guard, gap=gap)


def _plan(cfg, db, project, *, gate_reader=None, reviewed=False, reviews=(),
          expected_pin=None, expected_gate=None):
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
    # #7751: a persisted request names the reading it was reviewed against.
    # An application never follows a newer pin or gate on the strength of
    # opinions recorded for another reading: it refuses and names both values.
    if expected_pin is not None:
        expected_pin = str(expected_pin).strip().lower()
        if pin != expected_pin:
            raise RequalifyError(
                "stale requalification request: pin moved from %s to %s"
                % (expected_pin[:12] or "?", pin[:12] or "?"))
    if expected_gate is not None:
        expected_gate = str(expected_gate).strip().lower()
        if gate != expected_gate:
            raise RequalifyError(
                "stale requalification request: gate moved from %s to %s"
                % (expected_gate[:12] or "?", gate[:12] or "?"))
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
        result = {"state": "aligned", "project": project, "pin": pin, "gate": gate,
                  "changed": [], "files": [path for path, _raw in files]}
        if reviewed:
            from . import train_requalify_review
            result["reviews"] = train_requalify_review.qualify(
                cfg, db, dataclasses.replace(spec, canon=next(iter(canons))),
                pin, gate, [], reviews)
            result["mode"] = "reviewed"
        return result, []
    for path, raw in files:
        _validate(spec, path, raw, project, gate)
    canon = canons.pop()
    from . import resolutions
    ancestor = resolutions._run(canon, "merge-base", "--is-ancestor", pin, gate)
    if ancestor.returncode not in (0, 1):
        raise RequalifyError("refused: cannot check gate ancestry (git exit %s): %s"
                             % (ancestor.returncode, ancestor.stderr.strip()))
    if ancestor.returncode == 1:
        raise RequalifyError("refused: gate is not a descendant of the pin; "
                             "reviewed requalification required")
    changed = train_assembly.canon_diff(canon, pin, gate)
    guard = [name for name in changed if name in train_assembly.REQUALIFY_GUARD]
    motif = ""
    if guard and not reviewed:
        # #7880: a guard file can move without changing what this train
        # compiles (another project's policy, a comment, a path the train does
        # not read). Compare the compiled rules of every train project at both
        # commits; only unchanged rules make the move mechanical.
        gap = compiled_rules_gap(cfg, db, pin=pin, gate=gate)
        if gap:
            raise RequalifyReviewRequired(
                "refused: the canon diff %s..%s touches %s — a rules change is "
                "not a mechanical requalification; reviewed requalification "
                "required (%s)"
                % (pin[:12], gate[:12], ", ".join(guard), gap),
                project=project, pin=pin, gate=gate, changed=changed,
                guard=guard, gap=gap)
        motif = "règles compilées inchangées"
    result = {"state": "requalified", "project": project, "pin": pin, "gate": gate,
              "changed": changed, "files": [path for path, _raw in files]}
    if motif:
        result["motif"] = motif
        result["guard"] = guard
    if reviewed:
        from . import train_requalify_review
        result["reviews"] = train_requalify_review.qualify(
            cfg, db, dataclasses.replace(spec, canon=canon), pin, gate, changed, reviews)
        result["mode"] = "reviewed"
    return result, files


def plan(cfg, db, project, *, gate_reader=None, reviewed=False, reviews=(),
         expected_pin=None, expected_gate=None):
    """Compute the requalification; no write, no trace."""
    result, _files = _plan(cfg, db, project, gate_reader=gate_reader,
                           reviewed=reviewed, reviews=reviews,
                           expected_pin=expected_pin, expected_gate=expected_gate)
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


def apply(cfg, db, project, *, lot, actor, gate_reader=None, record=None,
          reviewed=False, reviews=(), request=None, expected_pin=None,
          expected_gate=None):
    """Serialize manual and automatic writers of this host's train pins."""
    directory = os.path.dirname(os.path.abspath(cfg.config_base or cfg.config_path))
    with open(os.path.join(directory, ".train-canon.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        extra = {"reviewed": True, "reviews": reviews} if reviewed else {}
        if request is not None:
            extra["request"] = request
        if expected_pin is not None:
            extra["expected_pin"] = expected_pin
        if expected_gate is not None:
            extra["expected_gate"] = expected_gate
        return _apply(cfg, db, project, lot=lot, actor=actor,
                      gate_reader=gate_reader, record=record, **extra)


def _apply(cfg, db, project, *, lot, actor, gate_reader=None, record=None,
           reviewed=False, reviews=(), request=None, expected_pin=None,
           expected_gate=None):
    """Requalify the pin, write every declaring file, trace on the lot."""
    result, files = _plan(cfg, db, project, gate_reader=gate_reader,
                          reviewed=reviewed, reviews=reviews,
                          expected_pin=expected_pin, expected_gate=expected_gate)
    if result["state"] == "aligned":
        return result
    if lot is not None and work.get(db, int(lot)) is None:
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
    if reviewed:
        text += "; reviews: " + json.dumps(result["reviews"], ensure_ascii=False)
    if request is not None:
        # #7751: the move runs on the holder's host for a request recorded
        # elsewhere; the trace names the request and where it was applied.
        text += "; request: %s; applied on %s" % (request, cfg.host)
    try:
        if record is None:
            # #6034: the reload follows only a journaled move, mechanical too.
            with journal(cfg, result, actor=actor, lot=int(lot),
                         request=request):
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


def execute(cfg, db, project, *, lot, actor, dry_run=False, gate_reader=None,
            reviewed=False, reviews=(), expected_pin=None, expected_gate=None):
    """The command body: plan or apply, with one named result."""
    if reviews and not reviewed:
        raise RequalifyError("--review requires --reviewed")
    extra = {"reviewed": True, "reviews": reviews} if reviewed else {}
    if expected_pin is not None:
        extra["expected_pin"] = expected_pin
    if expected_gate is not None:
        extra["expected_gate"] = expected_gate
    if dry_run:
        result = plan(cfg, db, project, gate_reader=gate_reader, **extra)
        result["dry_run"] = True
        return result
    return apply(cfg, db, project, lot=lot, actor=actor, gate_reader=gate_reader, **extra)


@contextmanager
def journal(cfg, result, *, actor, lot=None, request=None):
    """Durable host trace; undo the append if a companion lot trace fails.

    The caller holds .train-canon.lock for the whole pin and journal move.
    """
    path = train_assembly.journal_path(cfg)
    directory = os.path.dirname(path)
    row = dict(result, host=cfg.host, actor=actor, lot=lot, event="canon-requalified",
               at=datetime.now(timezone.utc).isoformat(),
               guard=list(train_assembly.REQUALIFY_GUARD))
    if request is not None:
        row["request"] = request
    with open(path, "a+",
              encoding="utf-8") as stream:
        stream.seek(0, os.SEEK_END)
        before = stream.tell()
        try:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            # Persist the directory entry on the first journal creation too.
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            yield
        except BaseException:
            stream.seek(before)
            stream.truncate()
            stream.flush()
            raise


def automatic(cfg, db, project, *, gate_reader=None):
    """#3483: follow the audited gate independently on every train host.

    All local profiles follow. The durable host journal is required: a write
    failure rolls the pin writes back. A host lock serializes executor profiles.

    #7880: when the guard changes the compiled rules, the refusal opens (or
    updates) its carrier lot before being raised. The pin is not written; the
    caller keeps it and reports the refusal. A failure to open the lot never
    turns the refusal into a move.
    """
    def record(result):
        with journal(cfg, result, actor="train"):
            pass
    try:
        return apply(cfg, db, project, lot=None, actor="train",
                     gate_reader=gate_reader, record=record)
    except RequalifyReviewRequired as exc:
        try:
            ensure_requalification_lot(
                cfg, db, project, pin=exc.pin, gate=exc.gate,
                changed=exc.changed, guard=exc.guard, gap=exc.gap)
        except Exception:  # noqa: BLE001 - the refusal stands without the carrier
            pass
        raise
