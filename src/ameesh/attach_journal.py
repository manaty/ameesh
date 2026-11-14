# SPDX-License-Identifier: AGPL-3.0-only
"""#7047 (avatar A0, design 5322 §2.2): an interactive attach writes its
exchanges to `conversation_log` through the harness hooks.

`run_attach` launches the harness without the executor's `Recorder`; the
hook `UserPromptSubmit` records the human prompt and the hook `Stop` the
final answer, only when the session is an attach (`AMEESH_ATTACH=1`) whose
live lease is the attach's own (`attach:…` owner, checked by the binding).
A driven turn (runner lease) keeps its `Recorder`: the hook abstains, so
nothing is written twice.

Event ids are `attach:<session>:<turn>:<seq>`: `<turn>` is fixed at the
prompt (its millisecond timestamp, so a lost state file never reuses an id
of an earlier turn), `<seq>` counts the entries of that turn (1 = prompt,
then one per `Stop`: a `Stop` that delivers mail continues the turn).

Never blocking: any failure (database, transcript, state file) is reported
on stderr and the hook goes on with mail delivery.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import attach_session, child_reports, conversation, db as db_mod, fil, registry

ENV = "AMEESH_ATTACH"
STATE_FILE = "attach-conversation.json"
#: the tail of a transcript read to find the final answer
TRANSCRIPT_TAIL = 2 * 1024 * 1024
EVENTS = ("UserPromptSubmit", "Stop")


def wanted(event: str, binding) -> bool:
    """Only the attach session itself, under its own live lease."""
    return (event in EVENTS and os.environ.get(ENV) == "1"
            and binding.bound_to_lease
            and (binding.runner_id or "").startswith("attach:"))


def _state_path(cfg, name: str) -> str:
    return os.path.join(cfg.agent_dir(name), STATE_FILE)


def _read_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def next_ids(cfg, name: str, session: str, event: str, *, now=None) -> tuple[str, int]:
    """(turn, seq) of this entry; the state is written before the entry, so
    a replayed hook takes a new seq rather than overwriting an answer."""
    path = _state_path(cfg, name)
    state = _read_state(path)
    if event == "UserPromptSubmit" or state.get("session") != session \
            or not isinstance(state.get("seq"), int) or not state.get("turn"):
        turn = "%d" % int((time.time() if now is None else now) * 1000)
        seq = 1 if event == "UserPromptSubmit" else 2
    else:
        turn, seq = str(state["turn"]), state["seq"] + 1
    _write_state(path, {"session": session, "turn": turn, "seq": seq})
    return turn, seq


def final_from_transcript(path: str, *, tail: int = TRANSCRIPT_TAIL) -> str | None:
    """The text of the last assistant message(s) after the last user entry
    (prompt or tool result) of a Claude Code transcript; sidechains
    (subagents) are skipped. None when nothing typed is found."""
    if not isinstance(path, str) or not path.endswith(".jsonl") or not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - tail))
        data = fh.read()
    lines = data.decode("utf-8", "replace").splitlines()
    if size > tail:
        lines = lines[1:]  # the first line is cut
    parts: list[str] = []
    found = False
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        kind = entry.get("type")
        if kind == "user":
            break
        if kind != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        found = True
        texts = [c["text"] for c in message.get("content") or []
                 if isinstance(c, dict) and c.get("type") == "text"
                 and isinstance(c.get("text"), str)]
        parts[:0] = texts
    return "".join(parts) if found else None


def final_answer(data: dict) -> str | None:
    text = data.get("last_assistant_message")
    if isinstance(text, str):
        return text
    return final_from_transcript(data.get("transcript_path"))


def human_author(binding) -> str:
    """The verified grant human (D3), else the attach lease's identity (A0).

    A distant attach knows the human from its checked grant
    (`AMEESH_ATTACH_HUMAN`, set by the host after verification); a local
    attach keeps the lease suffix it always had. The grant human is the A0
    author: two people never share a lease identity.
    """
    verified = attach_session.human_from_env()
    if verified:
        return verified
    return "human:" + binding.runner_id[len("attach:"):]


def _copy_to_parent(db, cfg, name: str, author: str, text: str, entry: dict) -> None:
    """Design §4.2: a human message written to a child is copied to its parent.

    Only a spawned child has a parent; `copy_human` enforces the same
    `responsible` (never one human's private conversation to another's
    avatar) and makes the copy idempotent by `source_seq`. A non-child is
    left alone, as is an entry without a sequence.
    """
    if not (registry.get(db, name) or {}).get("ephemeral"):
        return
    try:
        source_seq = int(entry.get("seq"))
    except (AttributeError, TypeError, ValueError):
        return
    child_reports.copy_human(db, cfg, name, author=author, body=text,
                             source_seq=source_seq)


def capture(cfg, db, binding, event: str, data: dict) -> dict | None:
    """Record this hook's entry; returns it, or None when nothing is due."""
    if db is None or not wanted(event, binding):
        return None
    session = str(data.get("session_id") or "")[:200]
    if not session:
        return None
    if event == "UserPromptSubmit":
        text = data.get("prompt")
        if not isinstance(text, str) or not text.strip():
            return None
    else:
        text = final_answer(data)
        if text is None:
            return None
    name = binding.name
    turn, seq = next_ids(cfg, name, session, event)
    # B1 (relecture cx2) : le fil de la persona, jamais celui de l'opérateur
    project = fil.project_for(cfg, fil.agent_project(registry.get(db, name)))
    log = conversation.Log(db, cfg, agent=name, project=project)
    event_id = "attach:%s:%s:%d" % (session, turn, seq)
    turn_ref = "attach:%s" % turn
    if event == "UserPromptSubmit":
        human = human_author(binding)
        entry = log.append("human_message", text, author=human, session=session,
                           turn=turn_ref, event_id=event_id)
    else:
        entry = log.append("turn_end", text, session=session, turn=turn_ref,
                           event_id=event_id, meta={"status": "completed"},
                           thread_text=text if text.strip() else None)
    log.drain_threads()
    if event == "UserPromptSubmit":
        _copy_to_parent(db, cfg, name, human, text, entry)
    return entry


def capture_quietly(cfg, db, binding, event: str, data: dict) -> None:
    """`capture`, never raising: the turn goes on whatever happens here."""
    try:
        capture(cfg, db, binding, event, data)
    except Exception as exc:  # noqa: BLE001 — journal non bloquant (A0)
        print("agent-mail: attach conversation entry not recorded (%s)"
              % db_mod.explain(exc, 200), file=sys.stderr)
