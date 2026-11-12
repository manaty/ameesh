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
import os
import sys
import time

from . import conversation, db as db_mod, mail, registry, spawn, storage

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


def _remove_child_state(cfg, child: str) -> None:
    """Remove the child's private policy/context files, best effort."""
    directory = cfg.agent_dir(child)
    for name in (spawn.CONTEXT_FILE, spawn.PROFILE_FILE, spawn.MCP_FILE):
        try:
            os.unlink(os.path.join(directory, name))
        except FileNotFoundError:
            pass
        except OSError:
            pass


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
