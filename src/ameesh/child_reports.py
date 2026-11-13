# SPDX-License-Identifier: AGPL-3.0-only
"""#7034: the child → parent report channel (design #7031 §4.2).

A spawned child reports progress, questions and its final report to the
parent. The channel is asynchronous, idempotent and bounded, and carries **no
authority**: a report is an input in the parent's journal, never a decision.

Three sinks, in this order:

1. the parent's mailbox (`mail.send`), which is the wake-up and also writes
   the readable R12 thread;
2. the parent's conversation journal (`conversation_log`, kind `input`,
   `meta={child, report_kind, child_seq}`), written after the mail so a mail
   failure does not leave a journal entry announcing a report nobody got;
3. nothing else — the report is not signed and is never replayed as a human
   message.

The runner also copies a human message delivered to a child into the parent's
journal (same `responsible` only), and writes a `final` report when the child
expires. `child report` is the explicit CLI for the child itself.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import conversation, db as db_mod, mail, registry, spawn, storage
from .platform import host as host_platform
from .platform import process as process_platform

KINDS = ("progress", "question", "final")
STATUSES = ("completed", "failed", "expired")
LIMIT = spawn.REPORT_LIMIT


class ReportError(ValueError):
    """The report cannot be delivered as asked (no child, bad body...)."""


def profile(db, child: str) -> dict | None:
    """The child's spawn profile (None for an ordinary agent)."""
    return storage.of(db).ephemerals.profile(child)


def _event(child: str, kind: str, seq: int | None = None) -> str:
    if kind in ("started", "final"):
        return "child:%s:%s" % (child, kind)
    return "child:%s:%s:%s" % (child, kind, int(seq or 0))


def _child(db, child: str):
    """(parent, profile row or None, registry row): a child predates #7034."""
    profile_row = profile(db, child)
    row = registry.get(db, child) or {}
    if not row.get("ephemeral"):
        raise ReportError("%s is not a spawned child" % child)
    parent = (profile_row or {}).get("parent") or row.get("created_by")
    parent = str(parent or "").strip()
    if not parent:
        raise ReportError("%s has no creating parent" % child)
    return parent, profile_row, row


def _deliver(db, cfg, child: str, kind: str, body: str, *,
             event_id: str, seq: int | None = None,
             status: str | None = None) -> dict:
    """Mail, then journal. `event_id` makes both sinks idempotent."""
    parent, profile_row, _row = _child(db, child)
    body = spawn.check_text(body, "report", LIMIT)
    if not body.strip():
        raise ReportError("a report body is required")
    payload = {"child_report": {"child": child, "kind": kind}}
    if seq is not None:
        payload["child_report"]["seq"] = int(seq)
    if status is not None:
        payload["child_report"]["status"] = status
    message_id = mail.send(db, child, parent, body, kind="notify",
                           payload=payload, idempotency_key=event_id)
    meta = {"child": child, "report_kind": kind}
    if seq is not None:
        meta["child_seq"] = int(seq)
    if status is not None:
        meta["status"] = status
    # `mail.send` already wrote the readable R12 copy: no second outbox.
    entry = conversation.Log(db, cfg, agent=parent).append(
        "input", body, author="agent:" + child, event_id=event_id,
        meta=meta, thread=False)
    return {"child": child, "parent": parent, "kind": kind, "seq": seq,
            "message_id": message_id, "entry": entry}


def report(db, cfg, child: str, kind: str, body: str, *,
           status: str | None = None) -> dict:
    """An explicit progress/question/final report from an existing child."""
    if kind not in KINDS:
        raise ReportError("unknown report kind: %r" % (kind,))
    if kind == "final":
        status = status or "completed"
        if status not in STATUSES:
            raise ReportError("unknown final status: %r" % (status,))
        result = _deliver(db, cfg, child, "final", body,
                          event_id=_event(child, "final"), status=status)
        storage.of(db).ephemerals.set_report(child, final=status)
        # B 40580 : un final ne laisse pas le contexte derrière lui, même si
        # l'enfant n'est pas encore échu (privauté, design §4.3).
        storage.of(db).ephemerals.purge_context(child)
        _remove_child_state(cfg, child)
        return result
    seq = storage.of(db).ephemerals.next_report_seq(child)
    return _deliver(db, cfg, child, kind, body,
                    event_id=_event(child, kind, seq), seq=seq)


def started(db, cfg, child: str) -> dict | None:
    """The runner's `started` report: once, idempotent, never fatal."""
    state = storage.of(db).ephemerals.report(child)
    if state is not None and state.get("started_at") is not None:
        return None
    parent, profile_row, _row = _child(db, child)
    profile_row = profile_row or {}
    body = ("Child %s started (parent %s, harness %s; tools: %s; MCP: %s). "
            "This is a report from the child, not an authorization."
            % (child, parent, profile_row.get("harness") or "?",
               ", ".join(profile_row.get("tools") or ()) or "none",
               ", ".join(profile_row.get("mcp") or ()) or "none"))
    result = _deliver(db, cfg, child, "started", body,
                      event_id=_event(child, "started"))
    storage.of(db).ephemerals.set_report(child, started=True)
    return result


def turn_end(db, cfg, child: str, *, final: str | None,
             status: str = "completed") -> dict | None:
    """The runner's end-of-turn report: progress, or final if the child ends.

    A `final` is sent when the ephemeral is expired or stopped; a failed turn
    is reported as failed progress otherwise (the child may resume).
    """
    # B 40591 : les fichiers privés de l'enfant ne survivent pas au tour qui
    # les a utilisés, même sur un hôte de placement où le parent ne peut pas
    # les supprimer. Un enfant mort à l'arrêt n'en laisse donc aucun ; le
    # prochain tour les réécrit depuis la base.
    _remove_child_state(cfg, child)
    parent, _profile_row, row = _child(db, child)
    expires = row.get("ephemeral_expires_ts")
    expired = bool(expires and expires <= time.time())
    stopped = row.get("status") == "stopped"
    text = (final or "").strip()
    if expired or stopped:
        state = "expired" if expired else ("failed" if status != "completed" else "completed")
        body = ("Child %s %s (parent %s). Last answer:\n\n%s"
                % (child, "expired" if expired else "stopped",
                   parent, text or "(none)"))
        result = _deliver(db, cfg, child, "final", body,
                          event_id=_event(child, "final"), status=state)
        storage.of(db).ephemerals.set_report(child, final=state)
        # B 40580 : le contexte ne survit pas au rapport final (design §4.3).
        storage.of(db).ephemerals.purge_context(child)
        _remove_child_state(cfg, child)
        return result
    if not text:
        return None
    body = "Child %s progress (turn %s):\n\n%s" % (child, status, text)
    seq = storage.of(db).ephemerals.next_report_seq(child)
    return _deliver(db, cfg, child, "progress", body,
                    event_id=_event(child, "progress", seq), seq=seq)


def copy_human(db, cfg, child: str, *, author: str, body: str,
               source_seq: int) -> dict | None:
    """Copy one human message delivered to the child into the parent journal.

    Only when the two share the same `responsible` (design #7031 §4.2): a
    human's private conversation with one avatar is never copied to another
    human's. The copy carries no authority and creates no second thread.
    """
    parent, _profile_row, child_row = _child(db, child)
    if child_row.get("responsible") != (registry.get(db, parent) or {}).get("responsible"):
        return None
    try:
        event_id = "child:%s:human:%d" % (child, int(source_seq))
        return conversation.Log(db, cfg, agent=parent).append(
            "input", body, author=author, event_id=event_id,
            meta={"child": child, "copied_from": int(source_seq)}, thread=False)
    except (ValueError, db_mod.DbError):
        return None


def sweep_expired(db, cfg, parent: str, *, warn=None) -> list[dict]:
    """Final reports for expired children of `parent`, then purge their context.

    Called at the start of the parent's turn: a child that dies without a
    final report leaves one, never a silence. Best effort — the parent's turn
    never waits on a child report.
    """
    done = []
    for row in storage.of(db).ephemerals.expired_children(parent):
        child = row["name"]
        try:
            state = storage.of(db).ephemerals.report(child)
            if state is None or state.get("final_at") is None:
                body = ("Child %s expired (parent %s) without a final turn. "
                        "Nothing more will come from it; its context is deleted."
                        % (child, parent))
                _deliver(db, cfg, child, "final", body,
                         event_id=_event(child, "final"), status="expired")
                storage.of(db).ephemerals.set_report(child, final="expired")
            storage.of(db).ephemerals.purge_context(child)
            _remove_child_state(cfg, child)
            done.append({"child": child, "status": "expired"})
        except Exception as exc:  # noqa: BLE001 — the parent's turn continues
            if warn is not None:
                warn("child report: final for %s not delivered (%s)" % (child, exc))
    return done


#: Host-local spool of the cleanup tasks a turn still owes: one marker per
#: child, written before its private files and removed with them.
CLEANUP_DIR = "spawn-cleanup"
CLEANUP_SUFFIX = ".json"


def cleanup_path(cfg, child: str) -> str:
    """The durable cleanup marker of one child on THIS host."""
    name = child.encode("utf-8").hex() + CLEANUP_SUFFIX
    return os.path.join(cfg.state_dir, CLEANUP_DIR, name)


def _writer_alive(marker: dict) -> bool:
    """True when the marker's writer is still the same live process.

    `boot_id` + start time, read through the platform layer (L63): a PID
    alone may be reused, and a reboot makes every recorded start stale.
    """
    try:
        pid = int(marker.get("pid"))
        start = float(marker.get("start"))
    except (TypeError, ValueError):
        return False
    boot = marker.get("boot")
    if pid <= 0 or not isinstance(boot, str):
        return False
    try:
        if boot != host_platform.boot_id():
            return False
        return process_platform.same_start(start, process_platform.start_time(pid))
    except Exception:  # noqa: BLE001 — an unreadable identity is not "alive"
        return False


def begin_turn(cfg, child: str) -> None:
    """Record the local cleanup task BEFORE the private files are written.

    B 40603 : a runner killed between `write_context` and `turn_end` (SIGKILL,
    crash, host loss) leaves the files behind, and the parent's sweep runs on
    ANOTHER host: it cannot remove them. The marker survives on the child's
    host and `sweep_local` executes the task at the next turn there, so it is
    kept until it runs, independent of a normal end of turn.
    """
    try:
        start = process_platform.start_time(os.getpid())
        boot = host_platform.boot_id()
    except Exception as exc:  # noqa: BLE001 — never write a task we cannot check
        raise OSError("process identity unavailable: %s" % exc) from None
    if start is None or boot is None:
        raise OSError("process identity unavailable: cleanup cannot be guaranteed")
    marker = json.dumps({"child": child, "pid": os.getpid(),
                         "boot": boot, "start": start})
    spawn.write_private(cleanup_path(cfg, child), marker + "\n")


def sweep_local(cfg, *, warn=None) -> list[str]:
    """Execute the cleanup tasks left by dead turns on THIS host.

    A marker whose writer is still the same live process is an in-flight
    turn: its files are left alone. An unreadable marker proves nobody can
    still be using the files, so privacy wins and they are removed.
    """
    directory = os.path.join(cfg.state_dir, CLEANUP_DIR)
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    except OSError as exc:
        if warn is not None:
            warn("child cleanup: spool unreadable (%s)" % exc)
        return []
    done = []
    for name in names:
        if not name.endswith(CLEANUP_SUFFIX):
            continue
        path = os.path.join(directory, name)
        try:
            child = bytes.fromhex(name[:-len(CLEANUP_SUFFIX)]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                marker = json.load(handle)
        except (OSError, ValueError):
            marker = {}
        if _writer_alive(marker):
            continue
        # B 40616 : la tâche n'est retirée qu'après suppression CONFIRMÉE de
        # tous les fichiers ; une erreur transitoire la laisse en place pour
        # la reprise, et `_remove_child_state` la marque orpheline (PID 0)
        # pour qu'un tour vivant ne la fasse plus passer pour en cours.
        if not _remove_child_state(cfg, child):
            if warn is not None:
                warn("child cleanup: %s kept for retry" % child)
            continue
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            if warn is not None:
                warn("child cleanup: marker of %s kept for retry" % child)
            continue
        done.append(child)
    return done


def remove_state(cfg, child: str) -> bool:
    """Best-effort immediate cleanup; True when every file is confirmed gone."""
    return _remove_child_state(cfg, child)


def _mark_orphan(cfg, child: str) -> None:
    """Keep the task, but no longer as an in-flight turn (B 40616)."""
    try:
        spawn.write_private(cleanup_path(cfg, child),
                            json.dumps({"child": child, "pid": 0}) + "\n")
    except OSError:
        pass


def _remove_child_state(cfg, child: str) -> bool:
    """Remove the private files and the cleanup task.

    True only when every unlink is confirmed (missing counts as gone). On any
    failure the marker is kept — and marked orphan so any sweep retries it —
    instead of announcing a cleanup that did not happen (B 40616).
    """
    directory = cfg.agent_dir(child)
    clean = True
    for name in (spawn.CONTEXT_FILE, spawn.PROFILE_FILE, spawn.MCP_FILE):
        try:
            os.unlink(os.path.join(directory, name))
        except FileNotFoundError:
            pass
        except OSError:
            clean = False
    if not clean:
        _mark_orphan(cfg, child)
        return False
    try:
        os.unlink(cleanup_path(cfg, child))
    except FileNotFoundError:
        pass
    except OSError:
        # The files are gone: no privacy leak remains. The stale marker is
        # harmless and a dead writer's next sweep removes it.
        pass
    return True


# --------------------------------------------------------------------------
# `ameesh child report` — the explicit CLI, from inside the child's turn
# --------------------------------------------------------------------------

def cmd_report(cfg, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ameesh child report")
    parser.add_argument("--kind", required=True, choices=KINDS)
    parser.add_argument("--body-file", help="one UTF-8 file (`-` = stdin)")
    parser.add_argument("--text", help="the body, as an argument")
    parser.add_argument("--status", choices=STATUSES, default=None,
                        help="final status (default: completed)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        from . import turn_context
        document = turn_context.current()
    except Exception as exc:  # never echo a document or a path
        print("child report: not in a verified turn (%s)" % exc, file=sys.stderr)
        return 1
    child = document["turn"]["agent"]
    if args.body_file == "-":
        body = sys.stdin.read(LIMIT + 1)
    elif args.body_file:
        try:
            with open(args.body_file, encoding="utf-8") as handle:
                body = handle.read(LIMIT + 1)
        except OSError as exc:
            print("child report: body file unreadable: %s" % exc, file=sys.stderr)
            return 1
    elif args.text is not None:
        body = args.text
    else:
        body = sys.stdin.read(LIMIT + 1)
    try:
        db = db_mod.connect(cfg)
    except db_mod.DbError as exc:
        print("child report: database unavailable: %s" % exc, file=sys.stderr)
        return 1
    try:
        db_mod.require_schema(db)
        result = report(db, cfg, child, args.kind, body, status=args.status)
    except (ReportError, spawn.SpawnToolsError) as exc:
        print("child report: refused: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()
    print("report %s %s: #%s" % (args.kind, child, result["message_id"]))
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "report":
        print("usage: ameesh child report --kind progress|question|final "
              "[--body-file F | --text T]", file=sys.stderr)
        return 2
    from . import config as config_mod
    try:
        cfg = config_mod.load()
    except (ValueError, OSError) as exc:
        print("child report: invalid configuration: %s" % exc, file=sys.stderr)
        return 2
    return cmd_report(cfg, argv[1:])
