# SPDX-License-Identifier: AGPL-3.0-only
"""#1617 — the Postgres port of the reviewer designation at freeze.

`review_designation` does the **selection** without any I/O; this module is the
real `Port`: it reads the lot's milestones, re-reads the idempotent mark of the
freeze and writes the designation in **one** transaction (lock of the lot row,
mark in the journal, mails in the mailbox). A reviewer mail is a `request`
linked to the lot (#2478): the reviewer's runner opens a turn on it, once
per head (#2495: a reviewer already holding a request for this head, or an
unread one naming none, gets no second mail); the capacity notification to the
pilot stays a `notify`. The readable thread is
written only after the commit (`mail.send(..., after_commit=…)`, pattern #315
of `deep_analysis`): a cancelled send leaves no trace in the external thread.

Two uses, depending on who holds the transaction:

- **at freeze** (narrow hook of #1467): the port is built on the milestone's
  open transaction — `PgPort(tx, after_commit=threads)`; `commit` opens no
  transaction, writes into the milestone's one, and the caller drains `threads`
  after its commit;
- **outside the hook** (recovery, CLI): `PgPort(db)` — `commit` opens its own
  transaction, locks the lot, then drains the thread itself after the commit.

The mark is a row of the lot journal, prefixed by the lot and the milestone:
`review-designation:<lot>:<freeze>: {json}`. It carries the state, the required
number, the reviewers, the exact SHA, the authors of the frozen content and the
mails it deposited (`sent`); `recorded` re-reads it as-is, so the same freeze
replayed returns the recorded
designation without a second mail. A new freeze is another mark: the old one no
longer counts, and the port refuses to write for a freeze that is no longer the
latest (`StaleFreeze`) — no return to an old head. A **forced** replay (#2487)
writes a newer mark over a reported one, but still checks what already left for
the same freeze: a review request or waiting notice already deposited is not
sent again (#2541). That evidence is durable: the mark records the mails it
deposited, the journal is not purged with the mailbox, and recipients are
compared as primary names, so an alias matches the resolved name the mailbox
holds (#2557).
"""
from __future__ import annotations

import json
from typing import Mapping, Optional, Sequence

from . import mail, plan_git, registry, storage
from .review_designation import (DESIGNATED, REQUEST_KIND, WAITING,
                                 DesignationError, StaleFreeze, covering_request)

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


def _mark_author(author) -> dict:
    """One author of the frozen content, with its proof trace when it has one.

    `proof: turn-journal` and `fallback_commits` are written only for an author
    admitted by the #2721 fallback (a rewritten or pre-hook commit): the mark
    keeps which commits relied on the commit's turn journal, so the decision is
    auditable without pretending they carry an exact receipt."""
    row = {"name": str((author or {}).get("name") or ""),
           "family": str((author or {}).get("family") or "")}
    families = (author or {}).get("families")
    if isinstance(families, (list, tuple, set, frozenset)):
        values = [str(value).strip() for value in families if str(value or "").strip()]
        if values:
            # #3584 : la marque garde tout l'ensemble prouvé, pas la seule
            # réduction ambiguë à "".
            row["families"] = values
    proof = str((author or {}).get("proof") or "").strip()
    if proof:
        row["proof"] = proof
        commits = (author or {}).get("fallback_commits")
        if isinstance(commits, (list, tuple)):
            row["fallback_commits"] = [str(commit) for commit in commits]
    return row


def _mark_note(lot: int, freeze_id: int, *, state: str, required: int, reviewers,
               missing: int, why: str, authors, policy_ref: str, sha: str,
               sent=(), unknown=()) -> str:
    payload = {
        "state": state,
        "required": int(required),
        "reviewers": [{"name": str((r or {}).get("name") or ""),
                       "family": str((r or {}).get("family") or "")}
                      for r in reviewers or ()],
        "missing": int(missing),
        "why": str(why or ""),
        "freeze": {"id": int(freeze_id), "sha": sha},
        "authors": [_mark_author(a) for a in authors or ()],
        # #2864 : la lecture de provenance n'est complète que si elle nomme les
        # commits qu'elle n'a pas pu attribuer. La marque le garde pour que la
        # proposition de relecture échoue fermé sur une lecture partielle.
        "unknown_authors": [str(value) for value in unknown or () if str(value).strip()],
        "policy_ref": str(policy_ref or ""),
        # #2557 : les courriels réellement déposés pour ce gel, en noms
        # primaires. La marque est écrite dans la même transaction qu'eux et
        # survit à une purge de la boîte ; c'est la preuve durable du rejeu.
        "sent": [{"to": str((s or {}).get("to") or ""),
                  "kind": str((s or {}).get("kind") or "notify"),
                  "capacity": bool((s or {}).get("capacity"))}
                 for s in sent or ()],
    }
    return "%s %s" % (mark_prefix(lot, freeze_id),
                      json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _designation(lot: int, freeze_id: int, *, state: str, required: int, reviewers,
                 missing: int, why: str, authors, policy_ref: str, sha: str,
                 unknown_authors=()) -> dict:
    """The shape returned by `commit`, the one `apply` passes to its caller."""
    return {
        "state": state,
        "required": int(required),
        "reviewers": [dict(r) for r in reviewers or ()],
        "missing": int(missing),
        "why": str(why or ""),
        "freeze": {"id": int(freeze_id), "sha": sha},
        "authors": [dict(a) for a in authors or ()],
        "unknown_authors": [str(value) for value in unknown_authors or ()
                            if str(value).strip()],
        "policy_ref": str(policy_ref or ""),
    }


def _primary_name(db, name: str, cache: Optional[dict] = None) -> str:
    """Le nom primaire d'un destinataire, alias résolu (#2557).

    `mail.send` range déjà le nom primaire dans la boîte ; la lecture du rejeu
    doit donc comparer des noms primaires, pas des orthographes. Un nom inconnu
    reste inchangé, comme dans `agent_names.resolve`."""
    text = str(name or "").strip()
    if not text:
        return ""
    if cache is not None and text in cache:
        return cache[text]
    from .agent_names import resolve as resolve_agent_name
    primary = str(resolve_agent_name(
        text, db=db, operation="review_designation.mailbox") or text)
    if cache is not None:
        cache[text] = primary
    return primary


def _designation_payload(payload) -> dict:
    """La charge `review_designation` d'un courrier (objet JSON ou texte)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    found = (payload or {}).get("review_designation") if isinstance(payload, dict) else None
    return found if isinstance(found, dict) else {}


def _sent_review_mails(db, lot: int, freeze_id: int, *, since_ts: float,
                       cache: Optional[dict] = None) -> tuple:
    """Les courriels de désignation déjà partis pour CE gel (#2541, #2557).

    Rend `(destinataires des demandes de relecture, destinataires de l'avis
    d'attente)`, en noms **primaires**. Deux preuves sont lues :

    - la **marque du gel** au journal (`review-designation:…`) : elle est écrite
      dans la même transaction que les courriels et le journal n'est pas purgé
      avec la boîte. Les marques récentes portent la liste `sent` des courriels
      réellement déposés ; une marque `designated` plus ancienne prouve à elle
      seule que les demandes de ses relecteurs sont parties (un destinataire
      inconnu la transformerait en `waiting`, et un envoi en échec annule la
      transaction avec elle) ;
    - la **boîte** (`agent_mailbox`) pour les marques antérieures à #2557 et
      pour l'avis d'attente d'une ancienne marque `waiting`, qui ne nomme pas
      son destinataire.

    Le rejeu forcé écrit une marque plus récente, mais un second réveil du même
    relecteur ou une seconde alerte de capacité ne sont pas des faits nouveaux.
    """
    cache = {} if cache is None else cache
    requests: set = set()
    waiting: set = set()

    def add(recipient, *, request: bool = False, capacity: bool = False) -> None:
        primary = _primary_name(db, recipient, cache)
        if not primary:
            return
        if request:
            requests.add(primary)
        elif capacity:
            waiting.add(primary)

    # 1. les marques du gel : preuve durable, indépendante de la boîte.
    marks = storage.of(db).work.events_noted(
        mark_prefix(lot, freeze_id), float(since_ts), ids=[int(lot)])
    for row in marks or ():
        noted = designation_of((row or {}).get("note"))
        if noted is None:
            continue
        if int((noted.get("freeze") or {}).get("id") or 0) != int(freeze_id):
            continue
        for entry in noted.get("sent") or ():
            if not isinstance(entry, Mapping):
                continue
            add(entry.get("to"),
                request=str(entry.get("kind") or "") == "request",
                capacity=bool(entry.get("capacity")))
        if noted.get("state") == DESIGNATED:
            # Une marque `designated` n'est écrite qu'une fois toutes les
            # demandes déposées : elle prouve leurs destinataires même sans
            # liste `sent` (marques d'avant #2557).
            for reviewer in noted.get("reviewers") or ():
                add((reviewer or {}).get("name"), request=True)

    # 2. la boîte : preuve des marques antérieures à #2557, et de l'avis
    # d'attente d'une marque `waiting` qui ne nomme pas son destinataire.
    rows = db.query(
        "SELECT recipient, kind, payload FROM agent_mailbox"
        " WHERE work_item_id = %s"
        "   AND payload->'review_designation'->>'freeze' = %s"
        " ORDER BY id",
        (str(int(lot)), str(int(freeze_id))))
    for row in rows or ():
        designation = _designation_payload((row or {}).get("payload"))
        if str(designation.get("freeze")) != str(int(freeze_id)):
            continue
        if str((row or {}).get("kind") or "") == "request":
            add((row or {}).get("recipient"), request=True)
        elif str(designation.get("state") or "") == WAITING:
            add((row or {}).get("recipient"), capacity=True)
    return requests, waiting


def _canonical_messages(db, messages, cache: Optional[dict] = None) -> list:
    """Les courriels du port avec leur destinataire en nom primaire (#2557)."""
    cache = {} if cache is None else cache
    out = []
    for message in messages or ():
        row = dict(message or {})
        row["to"] = _primary_name(db, row.get("to"), cache)
        out.append(row)
    return out


def _already_sent(message: Mapping, sent: tuple) -> bool:
    """Ce courrier du rejeu est-il déjà parti pour le même gel (#2541) ?

    `message["to"]` est déjà un nom primaire (`_canonical_messages`)."""
    recipient = str((message or {}).get("to") or "").strip()
    if not recipient:
        return False
    requests, waiting = sent
    if str((message or {}).get("kind") or "") == "request":
        return recipient in requests
    return bool((message or {}).get("capacity")) and recipient in waiting


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
    there would publish it before the caller's commit (#1775, N2).

    `at_freeze` says whether the write happens inside the `frozen` milestone:
    an unknown recipient is then a waiting designation instead of a refusal
    (#1634), because raising would cancel the acquired freeze. It defaults to
    True when `after_commit` is given (the hook's transaction), False when the
    port owns the transaction (a recovery outside the hook keeps the refusal).
    """

    def __init__(self, db, *, sender: str = SENDER, after_commit: Optional[list] = None,
                 at_freeze: Optional[bool] = None):
        self.db = db
        self.sender = sender or SENDER
        self.after_commit = after_commit
        self.at_freeze = ((after_commit is not None) if at_freeze is None
                          else bool(at_freeze))

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
               notify: str = "", messages: Sequence[Mapping] = (),
               unknown_authors: Sequence[str] = (), force: bool = False) -> dict:
        """Writes the freeze's designation in one transaction and returns its shape.

        Under the lock of the lot row: rechecks that `freeze` is still the
        latest freeze (otherwise `StaleFreeze`, **nothing** is written),
        rechecks the idempotent mark (a concurrent call returns the winner's
        designation, without a second mail), deposits the mails then the mark —
        all in the same transaction. A designation is final for its freeze; a
        waiting state is retryable and its notification leaves only once per
        freeze. `force` (#2487) writes a new mark even when the freeze already
        has one (a forced selection after a refused carry must supersede the
        reported mark); the caller uses it only for an explicit replay. That
        replay does not send a request or a waiting notice already deposited
        for the same freeze (#2541): the port filters them on the freeze's
        marks and the mailbox, with primary names, so a purge or an alias does
        not make a mail leave twice (#2557)."""
        lot = int(lot)
        if self.after_commit is not None:
            return self._write(self.db, lot, freeze=freeze, required=required,
                               state=state, reviewers=reviewers, missing=missing,
                               why=why, authors=authors, policy_ref=policy_ref,
                               notify=notify, messages=messages,
                               unknown_authors=unknown_authors,
                               hook=self.at_freeze, force=force)
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
                               messages=messages, unknown_authors=unknown_authors,
                               hook=False, force=force)
        # the external thread only after the commit: a cancelled send leaves nothing
        for record in threads:
            record(self.db.cfg, self.db)
        return out

    def _write(self, db, lot: int, *, freeze: Mapping, required: int, state: str,
               reviewers: Sequence[Mapping], missing: int, why: str, authors,
               policy_ref: str, notify: str, messages, hook: bool,
               unknown_authors: Sequence[str] = (), force: bool = False) -> dict:
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
        names: dict = {}
        messages = _canonical_messages(
            db, [dict(message or {}) for message in messages or ()], names)
        if force:
            # #2541/#2557 : le rejeu forcé écrit une marque plus récente, mais
            # ne renvoie pas les demandes de relecture ni l'avis d'attente déjà
            # partis pour ce gel. Le filtre lit la marque du gel (preuve
            # durable, survit à une purge de la boîte) et la boîte, avec des
            # noms primaires (un alias ne fait pas repartir un doublon). Il
            # précède le contrôle des destinataires : un courrier déjà déposé
            # n'a plus à être validé.
            sent = _sent_review_mails(
                db, lot, freeze_id, since_ts=float(last.get("at_ts") or 0.0),
                cache=names)
            messages = [message for message in messages
                        if not _already_sent(message, sent)]
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
        existing = None if force else self._recorded_in(
            db, lot, freeze_id, since_ts=float(last.get("at_ts") or 0.0))
        # A designation is final; a waiting state is not rewritten (a single
        # notification per freeze) but **yields** to the designation when
        # capacity comes back: it is the same freeze, the following mark
        # prevails. A forced replay writes a newer mark even over a final one:
        # the caller is an explicit recovery after a refused carry (#2487).
        if existing is not None and (existing.get("state") == DESIGNATED
                                     or state == WAITING):
            return existing
        if undelivered:
            ops.note(lot, str(rows[0].get("state") or ""),
                     "désignation au gel #%d : notification non remise — %s. Gel conservé, "
                     "aucun relecteur prévenu ; relancer la désignation hors hook avec un "
                     "destinataire du registre." % (freeze_id, what), self.sender)
        threads = self.after_commit if self.after_commit is not None else []
        deposited = []
        for row in messages:
            recipient = str(row.get("to") or "").strip()
            if (str(row.get("kind") or "") == REQUEST_KIND
                    and covering_request(mail.review_requests(db, recipient, lot),
                                         lot=lot, reviewer=recipient, sha=sha)):
                # #2495/#2504 : this reviewer already holds a request for this
                # head (or an unread one naming none): the designation is
                # written, no second mail. The requests are selected per lot in
                # storage, so an old one cannot fall out of a history window.
                continue
            mail.send(db, self.sender, recipient, str(row.get("body") or ""),
                      kind=str(row.get("kind") or "notify"),
                      work_item_id=str(lot),
                      payload={"review_designation": {
                          "lot": lot, "freeze": freeze_id, "sha": sha,
                          "required": int(required), "state": state,
                          "reviewer": str(row.get("family") or "")}},
                      after_commit=threads)
            # #2557 : la marque porte les courriels réellement déposés ; le
            # rejeu forcé les relit même si la boîte a été purgée.
            deposited.append({"to": recipient,
                              "kind": str(row.get("kind") or "notify"),
                              "capacity": bool(row.get("capacity"))})
        ops.note(lot, str(rows[0].get("state") or ""),
                 _mark_note(lot, freeze_id, state=state, required=required,
                            reviewers=reviewers, missing=missing, why=why,
                            authors=authors, policy_ref=policy_ref, sha=sha,
                            sent=deposited, unknown=unknown_authors),
                 self.sender)
        return _designation(lot, freeze_id, state=state, required=required,
                            reviewers=reviewers, missing=missing, why=why,
                            authors=authors, policy_ref=policy_ref, sha=sha,
                            unknown_authors=unknown_authors)
