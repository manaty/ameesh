# SPDX-License-Identifier: AGPL-3.0-only
"""#1617 — the Postgres port of the reviewer designation at freeze.

`review_designation` does the **selection** without any I/O; this module is the
real `Port`: it reads the lot's milestones, re-reads the idempotent mark of the
freeze and writes the designation in **one** transaction (lock of the lot row,
mark in the journal, mails in the mailbox). The readable thread is written only
after the commit (`mail.send(..., after_commit=…)`, pattern #315 of
`deep_analysis`): a cancelled send leaves no trace in the external thread.

Two uses, depending on who holds the transaction:

- **at freeze** (narrow hook of #1467): the port is built on the milestone's
  open transaction — `PgPort(tx, after_commit=threads)`; `commit` opens no
  transaction, writes into the milestone's one, and the caller drains `threads`
  after its commit;
- **outside the hook** (recovery, CLI): `PgPort(db)` — `commit` opens its own
  transaction, locks the lot, then drains the thread itself after the commit.

The mark is a row of the lot journal, prefixed by the lot and the milestone:
`review-designation:<lot>:<freeze>: {json}`. It carries the state, the required
number, the reviewers, the exact SHA and the authors of the frozen content;
`recorded` re-reads it as-is, so the same freeze replayed returns the recorded
designation without a second mail. A new freeze is another mark: the old one no
longer counts, and the port refuses to write for a freeze that is no longer the
latest (`StaleFreeze`) — no return to an old head.
"""
from __future__ import annotations

import json
from typing import Mapping, Optional, Sequence

from . import mail, plan_git, registry, storage
from .review_designation import DESIGNATED, WAITING, DesignationError, StaleFreeze

#: prefix of the journal marks: `<prefix><lot>:<milestone>: <json>`
MARK_PREFIX = "review-designation:"
#: the sender of automatic mails: the registry's machine identity
SENDER = "ameesh"
#: milestones re-read to find the latest freeze (wide: the lot's whole timeline)
MILESTONES = 200


def mark_prefix(lot: int, freeze_id: int) -> str:
    """The exact prefix of a freeze's mark: the lot and the milestone, together.

    Both are in the prefix so that `events_noted` returns only the marks of
    **this** freeze: `review-designation:3:12:` confuses neither lot 3 freeze 1
    with freeze 12, nor two lots."""
    return "%s%d:%d:" % (MARK_PREFIX, int(lot), int(freeze_id))


def designation_of(note: str) -> Optional[dict]:
    """The designation carried by a journal row, or None if it is not a mark.
    An unreadable mark is refused, never interpreted: we do not send a second
    mail on a doubt."""
    text = str(note or "")
    if not text.startswith(MARK_PREFIX):
        return None
    _, separator, payload = text.partition(": ")
    if not separator:
        raise DesignationError("unreadable review designation mark: %r" % text[:120])
    try:
        found = json.loads(payload)
    except ValueError as exc:
        raise DesignationError(
            "unreadable review designation mark: %s" % exc) from exc
    if not isinstance(found, dict) or found.get("state") not in (DESIGNATED, WAITING) \
            or not isinstance(found.get("freeze"), dict):
        raise DesignationError("incomplete review designation mark: %r" % text[:120])
    return found


def _mark_note(lot: int, freeze_id: int, *, state: str, required: int, reviewers,
               missing: int, why: str, authors, policy_ref: str, sha: str) -> str:
    payload = {
        "state": state,
        "required": int(required),
        "reviewers": [{"name": str((r or {}).get("name") or ""),
                       "family": str((r or {}).get("family") or "")}
                      for r in reviewers or ()],
        "missing": int(missing),
        "why": str(why or ""),
        "freeze": {"id": int(freeze_id), "sha": sha},
        "authors": [{"name": str((a or {}).get("name") or ""),
                     "family": str((a or {}).get("family") or "")}
                    for a in authors or ()],
        "policy_ref": str(policy_ref or ""),
    }
    return "%s %s" % (mark_prefix(lot, freeze_id),
                      json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _designation(lot: int, freeze_id: int, *, state: str, required: int, reviewers,
                 missing: int, why: str, authors, policy_ref: str, sha: str) -> dict:
    """The shape returned by `commit`, the one `apply` passes to its caller."""
    return {
        "state": state,
        "required": int(required),
        "reviewers": [dict(r) for r in reviewers or ()],
        "missing": int(missing),
        "why": str(why or ""),
        "freeze": {"id": int(freeze_id), "sha": sha},
        "authors": [dict(a) for a in authors or ()],
        "policy_ref": str(policy_ref or ""),
    }


def _nested_transaction(db) -> bool:
    """True if the `transaction()` block just opened is nested.

    A nested `with` does not commit, psql as well as psycopg: writing there and
    draining the thread would publish traces before the caller's commit (#1775,
    N2 — class B2 of review cx2 on #1617). psql counts **nested** levels
    (`_PsqlTransaction._depth`), psycopg the **total** level
    (`PsycopgDriver._tx_depth`, held with `_tx_lock`: inside our block, it
    counts only our own nesting).
    """
    depth = getattr(db, "_depth", None)
    if depth is not None:
        return bool(depth)
    return int(getattr(db, "_tx_depth", 1) or 0) > 1


class PgPort:
    """The `Port` of `review_designation` on Postgres.

    `db` is a connection (`PgPort(db)`) or an open transaction
    (`PgPort(tx, after_commit=threads)`). In the second case, the caller
    **must** provide `after_commit`: its entries are the thread writes to
    execute after its transaction's commit; the port then opens no transaction
    (no nesting in the `frozen` milestone's one). Built without `after_commit`
    on an open transaction, the port refuses to write (`DesignationError`,
    nothing written): a nested `with` does not commit, so draining the thread
    there would publish it before the caller's commit (#1775, N2)."""

    def __init__(self, db, *, sender: str = SENDER, after_commit: Optional[list] = None):
        self.db = db
        self.sender = sender or SENDER
        self.after_commit = after_commit

    # -- reads ------------------------------------------------------------
    def latest_freeze(self, lot: int) -> Optional[dict]:
        """The latest `frozen` milestone: `{id, sha}`, or None. It alone is
        authoritative — a branch name is not a freeze."""
        row = plan_git.latest_freeze(
            storage.of(self.db).work.milestones(int(lot), MILESTONES))
        if not row:
            return None
        return {"id": int(row["id"]), "sha": str(row.get("sha") or "")}

    def recorded(self, lot: int, freeze_id: int) -> Optional[dict]:
        """The designation already written for **this** freeze, or None.

        Reads the journal mark (the last one of this freeze if several, the
        journal order decides); this is what makes the freeze replayable without
        a second mail."""
        return self._recorded_in(self.db, int(lot), int(freeze_id))

    def _freeze_since(self, db, lot: int, freeze_id: int) -> Optional[float]:
        """The `at_ts` of the `frozen` milestone, if it is within the read window.

        A designation mark cannot precede its freeze: bounding the journal read
        to that instant avoids a `LIKE` over the whole journal (#1775, N3)."""
        for row in storage.of(db).work.milestones(int(lot), MILESTONES):
            if int(row["id"]) == int(freeze_id):
                return float(row.get("at_ts") or 0.0)
        return None

    def _recorded_in(self, db, lot: int, freeze_id: int, *,
                     since_ts: Optional[float] = None) -> Optional[dict]:
        if since_ts is None:
            since_ts = self._freeze_since(db, lot, freeze_id)
            if since_ts is None:
                # Milestone outside the read window: bounding it would require
                # re-reading the whole journal, and no write can target a
                # freeze that is no longer the latest (`commit` refuses it).
                return None
        found = None
        for row in storage.of(db).work.events_noted(mark_prefix(lot, freeze_id), since_ts):
            noted = designation_of(row.get("note"))
            if noted is None:
                continue
            if int(noted["freeze"].get("id") or 0) != freeze_id:
                raise DesignationError(
                    "review designation mark of lot #%d names freeze %r, not %d"
                    % (lot, noted["freeze"].get("id"), freeze_id))
            found = noted  # journal order (increasing id): the last one wins
        return found

    # -- writes -----------------------------------------------------------
    def commit(self, lot: int, *, freeze: Mapping, required: int, state: str,
               reviewers: Sequence[Mapping], missing: int, why: str,
               authors: Sequence[Mapping] = (), policy_ref: str = "",
               notify: str = "", messages: Sequence[Mapping] = ()) -> dict:
        """Writes the freeze's designation in one transaction and returns its shape.

        Under the lock of the lot row: rechecks that `freeze` is still the
        latest freeze (otherwise `StaleFreeze`, **nothing** is written),
        rechecks the idempotent mark (a concurrent call returns the winner's
        designation, without a second mail), deposits the mails then the mark —
        all in the same transaction. A designation is final for its freeze; a
        waiting state is retryable and its notification leaves only once per
        freeze."""
        lot = int(lot)
        if self.after_commit is not None:
            return self._write(self.db, lot, freeze=freeze, required=required,
                               state=state, reviewers=reviewers, missing=missing,
                               why=why, authors=authors, policy_ref=policy_ref,
                               notify=notify, messages=messages, hook=True)
        threads: list = []
        inner = PgPort(self.db, sender=self.sender, after_commit=threads)
        with self.db.transaction() as tx:
            if _nested_transaction(tx):
                # The transaction belongs to the caller: it will not commit
                # here. Draining the thread now would publish traces before its
                # commit (and an external rollback would leave them behind):
                # nothing is written, the caller must pass `after_commit` and
                # drain it itself after its commit (N2).
                raise DesignationError(
                    "commit on an open transaction requires after_commit: no commit "
                    "happens here, so the thread must be drained by the caller that "
                    "owns the commit (nothing written)")
            out = inner._write(tx, lot, freeze=freeze, required=required, state=state,
                               reviewers=reviewers, missing=missing, why=why,
                               authors=authors, policy_ref=policy_ref, notify=notify,
                               messages=messages, hook=False)
        # the external thread only after the commit: a cancelled send leaves nothing
        for record in threads:
            record(self.db.cfg, self.db)
        return out

    def _write(self, db, lot: int, *, freeze: Mapping, required: int, state: str,
               reviewers: Sequence[Mapping], missing: int, why: str, authors,
               policy_ref: str, notify: str, messages, hook: bool) -> dict:
        freeze = dict(freeze or {})
        sha = str(freeze.get("sha") or "").strip().lower()
        try:
            freeze_id = int(freeze.get("id"))
        except (TypeError, ValueError):
            raise DesignationError("freeze without milestone id: %r" % (freeze,)) from None
        ops = storage.of(db).work
        rows = db.query("SELECT id, state FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                        (lot,))
        if not rows:
            raise DesignationError("lot #%d not found: no designation" % lot)
        last = plan_git.latest_freeze(ops.milestones(lot, MILESTONES))
        if not last or int(last["id"]) != freeze_id \
                or str(last.get("sha") or "").strip().lower() != sha:
            raise StaleFreeze(
                "freeze #%d (%s) of lot #%d is no longer the last: nothing written"
                % (freeze_id, sha[:12] or "?", lot))
        if state not in (DESIGNATED, WAITING):
            raise DesignationError("a designation is %s or %s, not %r"
                                   % (DESIGNATED, WAITING, state))
        reviewers = [dict(r) for r in reviewers or ()]
        if state == DESIGNATED and not reviewers:
            raise DesignationError("a designated review names its reviewers")
        if state == WAITING and reviewers:
            raise DesignationError("a waiting designation designates no reviewer")
        messages = [dict(message or {}) for message in messages or ()]
        undelivered = []
        for row in messages:
            recipient = str(row.get("to") or "").strip()
            if not recipient:
                undelivered.append({"to": "", "reason": "no recipient"})
            elif registry.get(db, recipient) is None:
                undelivered.append({"to": recipient, "reason": "not in the registry"})
        if undelivered:
            if not hook:
                # outside the hook: the refusal is better than a designation
                # that never arrives (a mail to an unknown name wakes nobody),
                # and nothing is written
                first = undelivered[0]
                if not first["to"]:
                    raise DesignationError(
                        "no recipient for the %s of lot #%d: name the pilot (notify)"
                        % (state, lot))
                raise DesignationError(
                    "unknown recipient %r for the %s of lot #%d: nothing written"
                    % (first["to"], state, lot))
            # #1634 — at the freeze (hook), raising would cancel the acquired
            # `frozen` milestone. The freeze stays; no mail leaves, the mark is
            # a waiting one (retryable: a recovery outside the hook designates
            # when it can) and the lot journal says which recipient was missing.
            what = ", ".join("%s (%s)" % (u["to"] or "—", u["reason"]) for u in undelivered)
            why = "%snotification undeliverable at freeze: %s" % (
                (why + "; ") if why else "", what)
            if state == DESIGNATED:
                state, reviewers, missing = WAITING, [], int(required)
            messages = []
        existing = self._recorded_in(db, lot, freeze_id,
                                     since_ts=float(last.get("at_ts") or 0.0))
        # A designation is final; a waiting state is not rewritten (a single
        # notification per freeze) but **yields** to the designation when
        # capacity comes back: it is the same freeze, the following mark
        # prevails.
        if existing is not None and (existing.get("state") == DESIGNATED
                                     or state == WAITING):
            return existing
        if undelivered:
            ops.note(lot, str(rows[0].get("state") or ""),
                     "désignation au gel #%d : notification non remise — %s. Gel conservé, "
                     "aucun relecteur prévenu ; relancer la désignation hors hook avec un "
                     "destinataire du registre." % (freeze_id, what), self.sender)
        threads = self.after_commit if self.after_commit is not None else []
        for row in messages:
            recipient = str(row.get("to") or "").strip()
            mail.send(db, self.sender, recipient, str(row.get("body") or ""),
                      work_item_id=str(lot),
                      payload={"review_designation": {
                          "lot": lot, "freeze": freeze_id, "sha": sha,
                          "required": int(required), "state": state,
                          "reviewer": str(row.get("family") or "")}},
                      after_commit=threads)
        ops.note(lot, str(rows[0].get("state") or ""),
                 _mark_note(lot, freeze_id, state=state, required=required,
                            reviewers=reviewers, missing=missing, why=why,
                            authors=authors, policy_ref=policy_ref, sha=sha),
                 self.sender)
        return _designation(lot, freeze_id, state=state, required=required,
                            reviewers=reviewers, missing=missing, why=why,
                            authors=authors, policy_ref=policy_ref, sha=sha)
