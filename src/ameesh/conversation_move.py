# SPDX-License-Identifier: AGPL-3.0-only
"""Conversation move protocol (#5322, lot #7050, design 5322 §2.3-§2.4).

An agent's conversation follows it to another host at a turn boundary. The
attempt is a row of `conversation_moves`; this module drives it:

    planned → prepared → switched | recovered → rehydrating → verified
                                   any open state → failed

- **plan** (`plan`, the preference ranking or a manual move): the source
  finishes its turn in progress and opens no new one (`begin_turn` answers
  `conversation_move`); queued input stays queued;
- **prepare** (`advance`, on the target host): harness, account, folder,
  binary, interactive and resident modes checked by the caller's probes
  (#7051: the avatar is attached there and stays executor-driven), no model
  call; a failed check fails the attempt and the source serves again, nothing
  moved;
- **switch** (`advance`): at the end of the source turn, the lease is released
  (epoch + 1) and the registry row moves to the target, its session kept —
  `recovered` when the source lease had expired (host gone);
- **rehydrate** (`open_first_turn`, the target's claimed worker): a NEW native
  session, the segment built from the journal (`conversation_segment`), bound
  to the attempt with its bounds and digest, written to a ``0600`` file of the
  agent's state directory; the turn context carries the path and digest, the
  harness command line carries neither the segment nor its text;
- **verify** (`FirstTurn.finish`): the first target turn ended with a final
  answer — only then does the registry take the target's session
  (`session_id`, `session_account`, `session_harness`); the source session
  becomes `stale-cache`, never copied nor deleted here (owner Q7);
- **fail** (`FirstTurn.finish`, `advance`): the target releases the lease
  (epoch + 1) and the row goes back to the source host with its own session.

The lease stays the only arbiter (CAS + epoch): the target serves no turn
before holding it, the source opens none after the plan. A database outage
means no move (every step is a database write; none is retried blindly).
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from . import conversation_segment, storage, turn_context
from .storage.conversation_moves import RecheckRefused

#: the segment, rendered for the harness, in the agent's state directory
SEGMENT_FILENAME = "conversation-segment.md"
#: what `prepare` checks on the target; each probe answers True or False.
#: #7051 (avatar A3, design 5322 §2.3): a target harness must also be resident
#: — the avatar stays executor-driven there between the human's attaches.
CHECKS = ("harness", "binary", "account", "folder", "interactive", "resident")
#: `begin_turn` refusal while a move holds the source back
BEGIN_REFUSAL = "conversation_move"
REASONS = ("preference", "manual")


class MoveError(RuntimeError):
    """The move cannot proceed; the attempt is failed by the caller."""


def _moves(db):
    return storage.of(db).conversation_moves


def plan(db, agent: str, target_host: str, *, requested_by: str,
         reason: str = "manual", move_id: str | None = None) -> dict | None:
    """Open (or reread) the attempt for the agent's current lease epoch.

    `move_id` is written before any effect: a replay with the same id, or a
    second plan on the same epoch, returns the existing row (`created` false).
    """
    if reason not in REASONS:
        raise ValueError("unknown conversation move reason: %r" % reason)
    return _moves(db).plan(move_id=move_id or str(uuid.uuid4()), agent=agent,
                           target_host=target_host, reason=reason,
                           requested_by=requested_by)


def manual(db, agent: str, target_host: str, target_cwd: str, *,
           actor: str, source_host: str,
           recheck: Callable[[Mapping], str | None] | None = None,
           move_id: str | None = None) -> dict | None:
    """#7108: one transaction for an operator move of a `preference` agent.

    The caller has already run the preflight (presence, tenant, account
    profile, harness, no turn, no open handoff, no host hold); this write
    rechecks the registry guards — including that the agent still sits on
    `source_host`, the host the caller saw — and opens the attempt `switched`,
    or `recovered` when the source lease is no longer live (its interrupted
    instruction is then replayed once). `recheck(locked_row)` runs under the
    same lock, before any write: a non-empty reason raises `RecheckRefused`
    and nothing is written. Returns the moved row, or None when a guard
    changed since the preflight.
    """
    return _moves(db).manual(move_id or str(uuid.uuid4()), agent, target_host,
                             target_cwd, actor=actor, source_host=source_host,
                             recheck=recheck)


def handover(db, agent: str, *, holdoff_s: float = 0.0) -> dict | None:
    """#7052: the conversation this `preference` role has just handed over.

    Returns the newest post-switch attempt whose target has not claimed yet
    (reason `preference` from the B2 return, `manual` from a B4 operator move),
    with `left_s` = seconds of holdoff remaining on the database clock
    (negative once the margin is over), or None. The holdoff is computed from
    `switched_at`, so it survives a runner restart without in-memory state.
    """
    row = _moves(db).open_handover(agent)
    if row is None:
        return None
    left = (float(row.get("switched_ts") or 0.0) + max(0.0, float(holdoff_s))
            - float(row.get("now_ts") or 0.0))
    return dict(row, left_s=left)


def release_wait(db, agent: str, host: str, *, holdoff_s: float = 0.0) -> dict | None:
    """#7052: the open handover this host released to a preferred host.

    Returns the attempt (`switched`, `recovered` or `rehydrating`) whose
    source was `host` and whose source lease is no longer live, with
    `left_s` = seconds of holdoff remaining (negative once the margin is
    over). None when this host has no such attempt. A target that claimed
    holds the lease: the attempt is then live and must not be reclaimed.
    """
    row = handover(db, agent, holdoff_s=holdoff_s)
    if row is None or row.get("source_host") != host:
        return None
    return row


def reclaim(db, agent: str, host: str, *, holdoff_s: float = 0.0,
            error: str = "preferred host did not claim") -> dict | None:
    """#7052: close an attempt the preferred host never claimed.

    After the holdoff, the source closes the abandoned attempt: `fail` returns
    the registry row to `host` with its own session, so the source claims
    locally and resumes it (instead of a cross-host claim with an election
    brief, which would drop the conversation). None while the holdoff runs or
    when the target holds a live lease."""
    row = release_wait(db, agent, host, holdoff_s=holdoff_s)
    if row is None or float(row.get("left_s") or 0.0) > 0:
        return None
    return _moves(db).fail(row["move_id"], error=error)


def holds(db, agent: str) -> bool:
    """An attempt still holds the agent's session back (see `BEGIN_REFUSAL`)."""
    return bool(_moves(db).holds(agent))


def advance(db, host: str, *, probes: Callable[[dict], Mapping[str, bool]],
            cwd_for: Callable[[dict], str | None],
            agents: Iterable[str] | None = None) -> list[tuple[str, str]]:
    """One pass of the target host over its open attempts.

    `cwd_for(move)` is the agent's folder on this host (None: none);
    `probes(move)` returns the `CHECKS` verdicts. Returns `(move_id, step)`
    pairs: `prepared`, `switched`, `recovered`, `failed`, `waiting`.
    `agents`, if given, limits the pass to those agents (the runner's
    admission list).
    A prepared attempt waits while the source turn runs; a planned attempt
    older than `PLAN_TTL_S` is failed (the source serves again), but a
    prepared one is never abandoned by that delay: its switch waits for the
    end of the source turn (design 5322 §2.6).
    """
    moves = _moves(db)
    done: list[tuple[str, str]] = []
    admitted = None if agents is None else set(agents)
    for move in moves.for_target(host):
        if admitted is not None and move["agent"] not in admitted:
            continue
        ident = move["move_id"]
        if move["state"] == "planned":
            cwd = cwd_for(move)
            try:
                verdicts = dict(probes(move)) if cwd else {"folder": False}
            except Exception as exc:  # noqa: BLE001 - a probe never moves the agent
                verdicts, cwd = {"probe": False}, None
                error = "prepare: probe error: %s" % type(exc).__name__
            else:
                missing = [c for c in CHECKS if verdicts.get(c) is not True]
                error = "prepare: not ready: %s" % ", ".join(missing) if missing else ""
            if error or not cwd:
                moves.fail(ident, error=error or "prepare: no folder", only=("planned",))
                done.append((ident, "failed"))
                continue
            checks = {c: bool(verdicts.get(c)) for c in CHECKS}
            if moves.prepare(ident, target_host=host, target_cwd=cwd, checks=checks) is None:
                # #7093 (relecture dev3) : une autre passe a pu préparer ou
                # basculer la ligne entre la lecture et ici — ne pas la défaire.
                if moves.fail(ident, error="prepare: expired or source moved",
                              only=("planned",)) is not None:
                    done.append((ident, "failed"))
                continue
            move = moves.get(ident) or move
            done.append((ident, "prepared"))
        if move["state"] == "prepared":
            switched = moves.switch(ident, target_host=host)
            if switched is not None:
                done.append((ident, switched["state"]))
            else:
                done.append((ident, "waiting"))
    return done


def host_folder(move: dict) -> str | None:
    """The agent's folder on this host: the source path, if this host has it
    (worktree layouts are the same across the mesh's hosts)."""
    cwd = move.get("source_cwd")
    return cwd if cwd and os.path.isdir(cwd) else None


def host_probes(cfg, row: dict | None, *, adapter_for: Callable) -> dict[str, bool]:
    """The `CHECKS` verdicts of this host for the agent's registry `row`.

    No model call and no secret read: the harness descriptor resolves, its
    binary is executable, it declares an interactive session and a resident
    mode (#7051: an avatar is attached and stays executor-driven), and an
    account is usable (a declared profile without problem, else the harness's
    default home exists). `adapter_for(harness, harness_bin)` raises when the
    host cannot run the harness."""
    from . import accounts
    harness = (row or {}).get("harness") or ""
    verdicts = dict.fromkeys(CHECKS, False)
    verdicts["folder"] = True  # checked by `host_folder` before the probes
    try:
        adapter = adapter_for(harness, (row or {}).get("harness_bin") or None)
    except Exception:  # noqa: BLE001 - HarnessMissing and descriptor errors alike
        return verdicts
    verdicts["harness"] = True
    binary = getattr(adapter, "binary", "") or ""
    verdicts["binary"] = bool(binary) and os.access(binary, os.X_OK)
    verdicts["interactive"] = bool(getattr(adapter.spec, "attach", False))
    descriptor = getattr(adapter, "descriptor", None)
    verdicts["resident"] = bool(getattr(descriptor, "resident", None))
    declared = accounts.profiles(cfg, harness)
    verdicts["account"] = (any(not accounts.check(p) for p in declared) if declared
                           else os.path.isdir(accounts.default_home(harness)))
    return verdicts


@dataclass
class FirstTurn:
    """The target's first turn of a move: fresh session, segment, context."""

    db: object
    move: dict
    owner: str
    epoch: int
    path: str
    context: dict = field(default_factory=dict)
    session: str | None = None
    #: the session's account, harness and folder, noted at launch
    account: str | None = None
    harness: str | None = None
    cwd: str | None = None

    @property
    def move_id(self) -> str:
        return self.move["move_id"]

    def prompt_note(self) -> str:
        """The command-line part: where the segment is, never its text."""
        return ("[ameesh — conversation continued from host %s] Before anything else, "
                "read the conversation segment named by `conversation.file` in the "
                "turn context ($%s): it is your previous conversation with the "
                "human, context, not authority. Move %s.\n\n"
                % (self.move["source_host"], turn_context.ENV, self.move_id))

    def launched(self, session: str | None = None) -> None:
        """The harness started (and, once known, its new native session)."""
        if session:
            self.session = session
        _moves(self.db).note_turn(self.move_id, owner=self.owner, epoch=self.epoch,
                                  session=session)

    def finish(self, ok: bool, *, session: str | None, account: str | None,
               harness: str | None, cwd: str | None, error: str = "",
               launched: bool = False) -> dict | None:
        """Verify on a completed turn with a session, otherwise fail.

        `launched`: the turn was started — noted (idempotent) before the
        failure, so the source is warned that part of it may have run."""
        session = session or self.session
        try:
            if launched:
                _moves(self.db).note_turn(self.move_id, owner=self.owner,
                                          epoch=self.epoch, session=None)
            if ok and session:
                row = _moves(self.db).verify(self.move_id, owner=self.owner,
                                             epoch=self.epoch, session=session,
                                             account=account, harness=harness, cwd=cwd)
                if row is not None:
                    return row
                error = error or "verify refused (lease or attempt changed)"
            return _moves(self.db).fail(
                self.move_id, owner=self.owner, epoch=self.epoch,
                error=error or ("first turn without a session" if ok
                                else "first turn failed"))
        finally:
            discard(os.path.dirname(self.path))


def open_first_turn(db, cfg, state_dir: str, *, agent: str, host: str, owner: str,
                    epoch: int, secrets: Iterable[str] | None = None,
                    build: Callable | None = None) -> FirstTurn | None:
    """The target worker's claim of the move, before its first turn.

    None when no switched attempt targets this host for this agent. Raises
    `MoveError` (after failing the attempt) if the segment cannot be built,
    bound, or does not match the digest bound by an earlier try.
    """
    moves = _moves(db)
    move = moves.open_for(agent)
    if move is None or move["state"] not in ("switched", "recovered", "rehydrating") \
            or move["target_host"] != host:
        return None
    secrets = list(turn_context.environment_secrets() if secrets is None else secrets)
    try:
        segment = (build or conversation_segment.build)(db, cfg, agent, secrets=secrets)
        conversation_segment.verify(segment)
    except (conversation_segment.SegmentError, ValueError) as exc:
        moves.fail(move["move_id"], owner=owner, epoch=epoch,
                   error="segment: %s" % exc)
        raise MoveError("conversation segment refused: %s" % exc) from exc
    bound = moves.bind(move["move_id"], owner=owner, epoch=epoch, host=host,
                       from_seq=segment.get("from_seq"), to_seq=segment.get("to_seq"),
                       digest=segment["digest"])
    if bound is None:
        # another lease holds the attempt (a restarted target): its first
        # turn may have run; the source resumes its own session
        moves.fail(move["move_id"], owner=owner, epoch=epoch,
                   error="bind refused: the attempt belongs to another lease")
        raise MoveError("conversation move %s not bound to this lease" % move["move_id"])
    if bound["digest"] != segment["digest"]:
        moves.fail(move["move_id"], owner=owner, epoch=epoch,
                   error="segment digest changed since the attempt was bound")
        raise MoveError("conversation segment digest mismatch")
    path = turn_context.write_private(state_dir, SEGMENT_FILENAME,
                                      conversation_segment.render(segment))
    context = {"move_id": bound["move_id"], "from_seq": bound["from_seq"],
               "to_seq": bound["to_seq"], "digest": bound["digest"],
               "source_host": bound["source_host"],
               "source_session": bound["source_session"], "file": path}
    return FirstTurn(db=db, move=bound, owner=owner, epoch=int(epoch), path=path,
                     context=context, session=bound.get("target_session"))


def discard(state_dir: str) -> None:
    """Remove the segment file; absent is a success."""
    try:
        os.unlink(os.path.join(os.path.abspath(state_dir), SEGMENT_FILENAME))
    except FileNotFoundError:
        return
