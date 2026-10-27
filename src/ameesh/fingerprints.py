# SPDX-License-Identifier: AGPL-3.0-only
"""#3885 — failure fingerprints: one work item per new fingerprint (canon §16).

The owner's rule of 2026-10-27 (canon ``processus/lots.md`` §16, PR
manaty/home#190) says a server failure is never shown to the user: the request
is kept, the bug is fixed, the request is replayed.  Detection is a journal of
failures keyed by a stable **fingerprint** (route, SQLSTATE or exception name,
deployed revision, no personal data), written by the product (#3882 for
Nexlink).  This module is the mechanical part on the ameesh side:

* :func:`open_lot` is the primitive behind
  ``ameesh work fingerprint open``.  It is idempotent: while a lot is open for
  ``empreinte:<app>:<key>`` the call **notes** the occurrence (count, last
  seen, revision) instead of opening a second lot, and promotes the lot to P1
  as soon as one user is touched.  A fingerprint that comes back after its lot
  was merged/promoted/closed opens a new **recurrence** lot linked to the
  previous one.  Two concurrent passes are serialized by a transaction
  advisory lock; the partial unique index added by migration 0492 is the
  database backstop, so two writers can never leave two open lots;
* :func:`tick` is the **fingerprints** pass of the elected watch role.  The
  source is declared per project by the canon (``fingerprints`` key of an
  Agent card with the ``github-watch`` role, like ``github_watch``), read with
  a **read-only token** from a 0600 file.  Without a token the pass refuses
  explicitly instead of reading nothing.  A page is strictly validated: only
  the contract fields are accepted, an unknown field is refused, and a
  failure of one page never advances its cursor.

The pass is carried only by ``ameesh role timers`` / ``ameesh role run``: the
full ``ameesh notify`` service never runs it, so only the live lease holder
reads the source.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import canon, httpkit, plan_github, storage, work
from .db import DbError, is_unique_violation

#: the durable identity of a failure fingerprint
PREFIX = "empreinte:"
#: the namespace of the database guard (migration 0492)
FINGERPRINT_INDEX = "work_items_fingerprint_open_uniq"
#: the only page schema this reader accepts
SCHEMA = "nexlink-fingerprints/1"
#: the exact fields of a page and of an entry (#3882 contract).  An unknown
#: field is refused, never ignored: a new field must be added here first.
PAGE_FIELDS = frozenset(("schema", "cursor", "fingerprints"))
ENTRY_FIELDS = frozenset(("fingerprint", "route", "code", "revision",
                          "first_seen", "last_seen", "occurrences", "users"))
#: 0600 file holding the read-only token; the service environment may name it
TOKEN_ENV = "AMEESH_FINGERPRINTS_TOKEN_FILE"
DEFAULT_TOKEN_FILE = "~/.config/ameesh/fingerprints.token"
#: a page is bounded; a source that grows past this is refused, not truncated
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_PAGE_ITEMS = 5000
#: one line of the source, bounded before it reaches a lot body or journal
MAX_ROUTE = 300
MAX_CODE = 120
MAX_REVISION = 120
MAX_FINGERPRINT = 200
MAX_SEEN = 64
MAX_TITLE = 200

_APP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KEY_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_EMAIL_RE = re.compile(r"(?i)(?<![\w.%+-])[a-z0-9._%+-]+@[a-z0-9-]+"
                       r"(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_QUERY_RE = re.compile(r"(?i)(?:token|secret|password|api[_-]?key|access[_-]?key)")

class FingerprintError(ValueError):
    """The primitive or the pass refuses explicitly (actionable message)."""


@dataclass(frozen=True)
class Declaration:
    """One canon-declared source: a project, an HTTPS endpoint and a cadence."""

    project: str
    url: str
    interval: float
    agent: str = ""

    def as_json(self) -> dict:
        return {"project": self.project, "url": self.url,
                "interval": self.interval, "agent": self.agent}


@dataclass(frozen=True)
class Entry:
    """One strictly validated fingerprint of a page."""

    fingerprint: str
    route: str
    code: str
    revision: str
    first_seen: str
    last_seen: str
    occurrences: int
    users: int


# --------------------------------------------------------------------------
# the primitive: open or note one fingerprint
# --------------------------------------------------------------------------

def source_of(app: str, key: str) -> str:
    """The durable source ``empreinte:<app>:<key>`` (validated)."""
    app = (app or "").strip()
    key = (key or "").strip()
    if not _APP_RE.match(app):
        raise FingerprintError("--app: expected a project name (letters, digits, . _ -), "
                               "not %r" % app)
    if not _KEY_RE.match(key):
        raise FingerprintError("--key: expected 1..200 printable characters "
                               "without control characters")
    return "%s%s:%s" % (PREFIX, app, key)


def _one_line(value: str, field: str, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if _CONTROL_RE.search(text):
        raise FingerprintError("%s: control characters are refused" % field)
    if required and not text:
        raise FingerprintError("%s: required" % field)
    if len(text) > limit:
        raise FingerprintError("%s: at most %d characters" % (field, limit))
    return text


def _check_no_secret(text: str, field: str) -> None:
    """Refuse a field that carries a secret or an e-mail address.

    The source contract promises no personal data; a regression must fail
    loudly before the value reaches a lot journal, not be copied in.
    """
    if plan_github.Guard().problems(text, public=False):
        raise FingerprintError("%s: a secret pattern is refused; the source must "
                               "not export secrets" % field)
    if _EMAIL_RE.search(text):
        raise FingerprintError("%s: an e-mail address is refused; the source must "
                               "not export personal data" % field)


def _count(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FingerprintError("%s: an integer is required" % field)
    if value < 0:
        raise FingerprintError("%s: a non-negative integer is required" % field)
    return value


def _checked(*, app, key, title, users, count, route, code, revision,
             first_seen, last_seen):
    source = source_of(app, key)
    title = _one_line(title, "--title", MAX_TITLE, required=True)
    users = _count(users, "--users")
    count = _count(count, "--count")
    fields = {
        "route": _one_line(route, "--route", MAX_ROUTE),
        "code": _one_line(code, "--code", MAX_CODE),
        "revision": _one_line(revision, "--revision", MAX_REVISION),
        "first_seen": _one_line(first_seen, "--first-seen", MAX_SEEN),
        "last_seen": _one_line(last_seen, "--last-seen", MAX_SEEN),
    }
    for value in fields.values():
        _check_no_secret(value, "the fingerprint")
    _check_no_secret(title, "--title")
    return source, title, users, count, fields


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _body(*, source, users, count, fields, previous=None) -> str:
    lines = [
        "Empreinte d'échec serveur (canon `processus/lots.md` §16).",
        "",
        "Empreinte : `%s`" % source,
        "Route : %s" % (fields["route"] or "—"),
        "Code (SQLSTATE ou exception) : %s" % (fields["code"] or "—"),
        "Révision déployée : %s" % (fields["revision"] or "—"),
        "Première vue : %s" % (fields["first_seen"] or "—"),
        "Dernière vue : %s" % (fields["last_seen"] or "—"),
        "Occurrences : %d" % count,
        "Utilisateurs touchés : %d" % users,
        "",
        "Un seul lot ouvert par empreinte : les doublons sont notés au journal, "
        "pas ouverts en double.",
        "P1 dès qu'un utilisateur est touché.",
    ]
    if previous is not None:
        lines += ["", "Récidive du lot #%d (fermé) : même empreinte, nouvelle "
                      "occurrence." % int(previous)]
    return "\n".join(lines)


def _recurrence_title(title: str, previous_id: int) -> str:
    suffix = " (récidive de #%d)" % int(previous_id)
    room = max(1, MAX_TITLE - len(suffix))
    return title[:room].rstrip() + suffix


def _open_row(db, source: str) -> dict | None:
    rows = storage.of(db).work.by_source(source)
    for row in rows:
        if row.get("state") not in work.MERGED_STATES and row.get("state") != "closed":
            return row
    return None


def _latest_row(db, source: str) -> dict | None:
    rows = storage.of(db).work.by_source(source)
    return rows[-1] if rows else None


def _seen_note(*, fields, count: int, users: int, escalated: bool) -> str:
    text = ("Empreinte revue : occurrences=%d, dernière vue=%s, révision=%s, "
            "utilisateurs touchés=%d"
            % (count, fields["last_seen"] or _now_iso(),
               fields["revision"] or "—", users))
    if escalated:
        text += " ; P1 (un utilisateur est touché)"
    return text


def _note_seen(db, row: dict, *, fields, count: int, users: int, actor: str) -> bool:
    """Note the occurrence on the open lot and promote it to P1 if needed.

    Returns True when the priority was changed.  The row is read again under
    the transaction lock by ``set_priority``; a concurrent close is refused
    there rather than writing a priority on a finished lot.
    """
    escalated = False
    if users > 0 and row.get("priority") != 1:
        updated = storage.of(db).work.set_priority(
            int(row["id"]), 1, current=row.get("priority"),
            reason="empreinte touchant %d utilisateur(s) (canon §16)" % users,
            actor=actor)
        escalated = updated is not None
    work.note(db, int(row["id"]),
              _seen_note(fields=fields, count=count, users=users,
                         escalated=escalated),
              actor=actor)
    return escalated


def _open_once(db, *, source, app, title, users, count, fields, actor) -> dict:
    with db.transaction() as tx:
        # One lock per identity: two hosts (or two processes) serialize here
        # before reading the open lot, so the loser notes the winner.
        tx.query("SELECT pg_advisory_xact_lock(hashtextextended(lower(%s), 0))",
                 (source,))
        open_row = _open_row(tx, source)
        if open_row is not None:
            # Lock the lot before noting it: a concurrent `work close` must
            # not receive a note on a lot that just became terminal. If it
            # won the race, fall through and open the recurrence.
            locked = storage.of(tx).work.lock(int(open_row["id"]))
            if locked is not None and locked.get("state") not in (
                    *work.MERGED_STATES, "closed"):
                escalated = _note_seen(tx, locked, fields=fields, count=count,
                                       users=users, actor=actor)
                return {"created": False, "noted": True, "escalated": escalated,
                        "lot": int(locked["id"]), "source": source,
                        "recurrence_of": None,
                        "priority": 1 if (escalated or locked.get("priority") == 1)
                        else locked.get("priority")}
        previous = _latest_row(tx, source)
        previous_id = int(previous["id"]) if previous is not None else None
        recurrence = previous_id is not None and previous.get("state") in (
            *work.MERGED_STATES, "closed")
        new_title = (_recurrence_title(title, previous_id) if recurrence else title)
        note = ("récidive du lot #%d (empreinte déjà close)" % previous_id
                if recurrence else "")
        priority = 1 if users > 0 else 2
        item = work.add(
            tx, title=new_title, type="bug", priority=priority, source=source,
            app=app, body=_body(source=source, users=users,
                                count=count, fields=fields,
                                previous=previous_id if recurrence else None),
            actor=actor, note=note)
        return {"created": True, "noted": False, "escalated": False,
                "lot": int(item["id"]), "source": source,
                "recurrence_of": previous_id if recurrence else None,
                "priority": priority}


def open_lot(db, *, app: str, key: str, title: str, users=0, count=0,
             route: str = "", code: str = "", revision: str = "",
             first_seen: str = "", last_seen: str = "",
             actor: str = "") -> dict:
    """Open one lot for ``empreinte:<app>:<key>``, or note the open one.

    Idempotent by identity, never by call: a repeated call notes the
    occurrence (the pass may re-read a window after a failure).  ``users > 0``
    gives priority 1 at creation and promotes an open P2/P3 lot.  A closed
    fingerprint opens a recurrence lot linked to the previous one.
    """
    source, title, users, count, fields = _checked(
        app=app, key=key, title=title, users=users, count=count, route=route,
        code=code, revision=revision, first_seen=first_seen, last_seen=last_seen)
    actor = (actor or "").strip() or os.environ.get("AMEESH_ROLE") or work.SYSTEM_SENDER
    for attempt in (0, 1):
        try:
            return _open_once(db, source=source, app=app.strip(), title=title,
                              users=users, count=count, fields=fields, actor=actor)
        except DbError as exc:
            # The advisory lock covers our own writers; this catches a manual
            # row (or an old code path) that inserted without it.  Re-read the
            # winner and note it instead of failing the pass.
            if attempt or not is_unique_violation(exc, FINGERPRINT_INDEX):
                raise
    raise FingerprintError("empreinte %s: concurrent open, retry" % source)


# --------------------------------------------------------------------------
# the canon declaration and the strict read contract
# --------------------------------------------------------------------------

def _https(url: str) -> str:
    parts = urllib.parse.urlsplit(str(url or "").strip())
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("url must be an absolute https URL")
    if parts.username or parts.password:
        raise ValueError("url must not carry credentials")
    for key, _value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if _SECRET_QUERY_RE.search(key):
            raise ValueError("url must not carry a secret in its query")
    return urllib.parse.urlunsplit(parts)


def declarations(canons, log=None) -> tuple[list[Declaration], str]:
    """The canon's ``fingerprints`` cards: projects, endpoints, cadences.

    A card is an Agent fiche with the ``github-watch`` role (the watch role of
    §11) and a ``fingerprints`` mapping keyed by project::

        fingerprints:
          nexlink:
            url: https://…/internal/fingerprints
            interval: 60

    ``url`` and ``interval`` are the only settings; an unknown setting, a
    foreign project or an invalid URL is logged and skipped, never silently
    applied.  On a conflict the first declaration wins.  The second element
    is a readable error when the canon cannot be read at all, ``""``
    otherwise.
    """
    warn = log or (lambda _text: None)
    found: dict[str, Declaration] = {}
    for bundle in canons:
        for agent in bundle.agents:
            title = getattr(agent, "title", "")
            fiche = getattr(agent, "fiche", None)
            data = getattr(fiche, "data", None)
            declared = data.get("fingerprints") if isinstance(data, dict) else None
            if declared is None:
                continue
            if "github-watch" not in (getattr(agent, "roles", None) or ()):
                warn("%s: fingerprints requires role github-watch; declaration ignored"
                     % (title or "?"))
                continue
            if (not isinstance(title, str) or not isinstance(declared, dict)):
                warn("invalid fingerprints declaration on %s; ignored" % (title or "?"))
                continue
            projects = getattr(agent, "effective_projects", None) or ()
            for project, spec in declared.items():
                if not isinstance(project, str) or project not in projects:
                    warn("%s: fingerprints project %r must belong to the agent; ignored"
                         % (title or "?", project))
                    continue
                if not isinstance(spec, dict):
                    warn("%s: invalid fingerprints declaration for %s; ignored"
                         % (title or "?", project))
                    continue
                unknown = set(spec) - {"url", "interval"}
                if unknown:
                    warn("%s: unknown fingerprints setting %s for %s; ignored"
                         % (title or "?", ", ".join(sorted(map(str, unknown))), project))
                    continue
                try:
                    url = _https(spec.get("url"))
                except ValueError as exc:
                    warn("%s: fingerprints url for %s: %s; ignored"
                         % (title or "?", project, exc))
                    continue
                interval = spec.get("interval", 60)
                if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                        or not 60 <= interval <= 3600):
                    warn("%s: fingerprints interval must be 60..3600 seconds for %s; "
                         "ignored" % (title or "?", project))
                    continue
                candidate = Declaration(project, url, float(interval), title)
                current = found.get(project)
                if current is not None:
                    if current != candidate:
                        warn("%s/%s: multiple fingerprints sources; keeping %s"
                             % (project, url, current.agent or "the first"))
                    continue
                found[project] = candidate
    return list(found.values()), ""


def declarations_of_config(cfg, log=None) -> tuple[list[Declaration], str]:
    """Load the canon and read its ``fingerprints`` cards."""
    try:
        canons = canon.load_configured(cfg)
    except canon.CanonError as exc:
        return [], "canon unreadable: %s" % " ".join(str(exc).split())[:200]
    return declarations(canons, log)


def _text(value, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise FingerprintError("%s: a string is required" % field)
    if _CONTROL_RE.search(value):
        raise FingerprintError("%s: control characters are refused" % field)
    if len(value) > limit:
        raise FingerprintError("%s: at most %d characters" % (field, limit))
    return value


def _int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FingerprintError("%s: an integer is required" % field)
    if value < 0:
        raise FingerprintError("%s: a non-negative integer is required" % field)
    return value


def parse_page(payload: dict) -> tuple[list[Entry], str | None]:
    """Validate the exact #3882 contract; return the entries and the cursor.

    An unknown field (at the page or entry level), a missing field or a wrong
    type refuses the whole page.  A field is never ignored: a source that adds
    one must update this reader first.  A fingerprint is the lot identity; a
    page may carry the same fingerprint for several revisions (the source's
    key is ``(fingerprint, revision)``), and each revision is kept so its
    occurrence is noted.  Exact duplicates are collapsed on the last row.
    """
    if not isinstance(payload, dict):
        raise FingerprintError("fingerprints page: a JSON object is required")
    unknown = sorted(set(payload) - PAGE_FIELDS)
    if unknown:
        raise FingerprintError("fingerprints page: unknown field(s) %s"
                               % ", ".join(unknown))
    missing = sorted(PAGE_FIELDS - set(payload))
    if missing:
        raise FingerprintError("fingerprints page: missing field(s) %s"
                               % ", ".join(missing))
    if payload.get("schema") != SCHEMA:
        raise FingerprintError("fingerprints page: schema %r refused (expected %r)"
                               % (payload.get("schema"), SCHEMA))
    cursor = payload.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1000
                               or _CONTROL_RE.search(cursor)):
        raise FingerprintError("fingerprints page: cursor must be a bounded string")
    rows = payload.get("fingerprints")
    if not isinstance(rows, list):
        raise FingerprintError("fingerprints page: fingerprints must be a list")
    if len(rows) > MAX_PAGE_ITEMS:
        raise FingerprintError("fingerprints page: more than %d entries (refused, "
                               "not truncated)" % MAX_PAGE_ITEMS)
    by_key: dict[tuple[str, str], Entry] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise FingerprintError("fingerprints[%d]: an object is required" % index)
        unknown = sorted(set(row) - ENTRY_FIELDS)
        if unknown:
            raise FingerprintError("fingerprints[%d]: unknown field(s) %s"
                                   % (index, ", ".join(unknown)))
        missing = sorted(ENTRY_FIELDS - set(row))
        if missing:
            raise FingerprintError("fingerprints[%d]: missing field(s) %s"
                                   % (index, ", ".join(missing)))
        entry = Entry(
            fingerprint=_text(row["fingerprint"], "fingerprints[%d].fingerprint"
                              % index, MAX_FINGERPRINT).strip(),
            route=_text(row["route"], "fingerprints[%d].route" % index, MAX_ROUTE),
            code=_text(row["code"], "fingerprints[%d].code" % index, MAX_CODE),
            revision=_text(row["revision"], "fingerprints[%d].revision" % index,
                           MAX_REVISION),
            first_seen=_text(row["first_seen"], "fingerprints[%d].first_seen" % index,
                             MAX_SEEN),
            last_seen=_text(row["last_seen"], "fingerprints[%d].last_seen" % index,
                            MAX_SEEN),
            occurrences=_int(row["occurrences"], "fingerprints[%d].occurrences" % index),
            users=_int(row["users"], "fingerprints[%d].users" % index),
        )
        if not entry.fingerprint:
            raise FingerprintError("fingerprints[%d].fingerprint: required" % index)
        for value in (entry.fingerprint, entry.route, entry.code, entry.revision,
                      entry.first_seen, entry.last_seen):
            _check_no_secret(value, "fingerprints[%d]" % index)
        by_key[(entry.fingerprint, entry.revision)] = entry
    return list(by_key.values()), cursor


# --------------------------------------------------------------------------
# the watch pass
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A read-only token must never follow a redirect to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        raise FingerprintError("fingerprints source: redirect refused (%s)" % code)


def _with_since(url: str, cursor: str | None) -> str:
    if not cursor:
        return url
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "since"]
    query.append(("since", cursor))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def read_page(decl: Declaration, cursor: str | None, token: str,
              *, timeout: float = 30.0, opener=None) -> dict:
    """Read one page with the read-only bearer token (HTTPS, no redirect)."""
    request = urllib.request.Request(
        _with_since(decl.url, cursor),
        headers={"Authorization": "Bearer %s" % token,
                 "Accept": "application/json",
                 "User-Agent": "ameesh-fingerprints/1"})
    open_url = (opener or urllib.request.build_opener(_NoRedirect)).open
    try:
        with open_url(request, timeout=timeout) as response:
            raw = response.read(MAX_PAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FingerprintError("fingerprints source %s: HTTP %s"
                               % (decl.project, exc.code)) from None
    except urllib.error.URLError as exc:
        raise FingerprintError("fingerprints source %s: unreachable (%s)"
                               % (decl.project, exc.reason)) from None
    except FingerprintError:
        raise
    except Exception as exc:  # noqa: BLE001 - readable refusal, never a traceback
        raise FingerprintError("fingerprints source %s: read failed (%s)"
                               % (decl.project, type(exc).__name__)) from None
    if len(raw) > MAX_PAGE_BYTES:
        raise FingerprintError("fingerprints source %s: page larger than %d bytes"
                               % (decl.project, MAX_PAGE_BYTES))
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FingerprintError("fingerprints source %s: invalid JSON"
                               % decl.project) from None
    return payload


def title_of(entry: Entry) -> str:
    route = entry.route.strip() or "route inconnue"
    code = entry.code.strip() or "code inconnu"
    return ("Panne serveur %s — %s" % (route, code))[:MAX_TITLE]


def tick(cfg, db, *, now: float | None = None, log=None,
         token_file: str | None = None, opener=None,
         actor: str = "") -> list[dict]:
    """The ``fingerprints`` pass: read every declared source, open/note lots.

    Only the elected role runs it (``ameesh role run`` sets ``AMEESH_ROLE``).
    Without a source, nothing is read.  Without a token file, the pass refuses
    explicitly for every declared project.  A page is strictly validated; its
    cursor advances only after every entry was accepted.
    """
    emit = log or (lambda _text: None)
    decls, error = declarations_of_config(cfg, emit)
    if error:
        raise FingerprintError(error)
    if not decls:
        emit("fingerprints: no source declared in the canon")
        return []
    path = (token_file or os.environ.get(TOKEN_ENV) or DEFAULT_TOKEN_FILE)
    try:
        token = httpkit.read_token_file(
            path, what="fingerprints read-only token",
            hint="the source cannot be read without it (0600, on every candidate host)")
    except ValueError as exc:
        # Explicit refusal, like a missing GitHub key: never a silent pass.
        for decl in decls:
            try:
                storage.of(db).fingerprints.fail(decl.project, decl.url, str(exc))
            except Exception:  # noqa: BLE001 - the refusal is the point
                pass
        raise FingerprintError(str(exc)) from None
    actor = (actor or os.environ.get("AMEESH_ROLE") or work.SYSTEM_SENDER).strip()
    results: list[dict] = []
    failures: list[str] = []
    for decl in decls:
        state = storage.of(db).fingerprints.state(decl.project)
        cursor = (state or {}).get("cursor")
        # The role timer wakes at most every 90 s for the takeover; the
        # declared cadence is enforced on the persisted read time, like the
        # GitHub watch cursor, so a 1 h source is not polled every minute.
        moment = time.time() if now is None else float(now)
        last_ts = (state or {}).get("last_read_ts")
        if last_ts is not None and moment - float(last_ts) < decl.interval:
            emit("fingerprints %s: next read in %ds"
                 % (decl.project, max(1, int(decl.interval - (moment - float(last_ts))))))
            continue
        try:
            payload = read_page(decl, cursor, token, opener=opener)
            entries, next_cursor = parse_page(payload)
        except FingerprintError as exc:
            failures.append(str(exc))
            storage.of(db).fingerprints.fail(decl.project, decl.url, str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - one source never hides another
            text = ("fingerprints source %s: %s" % (decl.project, type(exc).__name__))
            failures.append(text)
            storage.of(db).fingerprints.fail(decl.project, decl.url, text)
            continue
        project_failed = False
        for entry in entries:
            try:
                result = open_lot(
                    db, app=decl.project, key=entry.fingerprint,
                    title=title_of(entry), users=entry.users,
                    count=entry.occurrences, route=entry.route, code=entry.code,
                    revision=entry.revision, first_seen=entry.first_seen,
                    last_seen=entry.last_seen, actor=actor)
            except Exception as exc:  # noqa: BLE001 - bounded, readable refusal
                project_failed = True
                text = ("fingerprints source %s, entry %s: %s"
                        % (decl.project, entry.fingerprint[:80], str(exc)[:200]))
                failures.append(text)
                storage.of(db).fingerprints.fail(decl.project, decl.url, text)
                break
            results.append(dict(result, project=decl.project,
                                fingerprint=entry.fingerprint,
                                occurrences=entry.occurrences, users=entry.users))
            emit("fingerprints %s: lot #%d %s (occurrences %d, users %d)"
                 % (decl.project, result["lot"],
                    "opened" if result["created"] else "noted",
                    entry.occurrences, entry.users))
        if not project_failed:
            storage.of(db).fingerprints.advance(
                decl.project, decl.url, next_cursor, now=now)
    if failures:
        raise FingerprintError("; ".join(failures)[:1000])
    return results
