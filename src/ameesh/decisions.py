# SPDX-License-Identifier: AGPL-3.0-only
"""Decision requests to the owner, received and answered in one place (work item L124).

#4256: new requests carry a recipient-local business-day deadline and a
project-owned validation route. Private audit and contradiction links live in
migration 0515; ``decide validation-history`` requires the owner's key.

  ameesh decide ask (--lot <id|ref> | --new-lot "title") --question "…"
                    --option a="…" [--option b="…" …] [--recommend a [--why "…"]]
                    [--urgent] [--by 2h|18:00|"2026-10-12 09:00"] [--needs-signature] [--json]
                    [--supersedes <answered decision id>]
        an agent files a request: attached to the project and the work item, the
        work item moves to `waiting_human` (it returns to its previous state on
        the answer), the target owner is notified;
        `--from-mail <id>` (L233): the mail in which the agent wrote this
        decision in prose (the lot of the mail by default);
        `--supersedes <id>` (#4991): replaces an answered decision only upon
        a valid signed answer; pending decisions use `withdraw`. The exact
        replaced id is signed, both links are shown, and an additive event
        in `decision_supersessions` preserves both decisions' original answers;
  ameesh decide <id> <option | "free text"> [--key FILE [--as NAME] | --signed] [--json]
        the owner answers (a HUMAN identity only); the answer goes to the
        requester by mail (it wakes them up), to the thread and to the work item;
  ameesh decide withdraw <id> [--why "…"] [--json]     the requester withdraws their request;
  ameesh decide repair-forward <id> [--json]           restore an accidentally forwarded request
        (a copy already withdrawn by its requester is kept: its trace stays);
  ameesh decide show <id> [--json]                     a request, its answer, its proof;
  ameesh decisions [--all] [--for human:ID] [--json]   the queue, across all projects
        (served by `actions_cli`, together with gated actions).

Finding of 2026-10-10: orchestrators were waiting for decisions from the
owner for 2h43 to 4h08; no request was addressed to them, they went into
other agents' mail (sometimes a dead mailbox), and the owner could only be
reached by attaching to an orchestrator's session. Now a request has a human
recipient, a queue, a notification, a reminder at its deadline, and a
traced answer.

**Only the queue counts** (L233, owner feedback of 2026-10-12): a lot
enters `waiting_human` only while a request is open for it (`work.move`
refuses otherwise; alert `waiting_without_question` for older rows), and a
decision written in prose in mail to bureau or to a human is not a request:
`ameesh decisions` lists such mail (`prose_decisions`, cue words
`PROSE_CUES`, last 24 h) with the command that converts it,
`ameesh decide ask --from-mail <id>`, run by its author.

**Recipient** (first found): the responsible of the work item's plan entry
(walking up the parents), the responsible of the requesting agent, that of
the work item's assignee, `notify.default_human`, the only declared human
(`humans`); otherwise any human of the mesh (`human:*`).

**Data**, no migration: a request is an `agent_mailbox` row
(`kind = 'request'`, recipient `human:<id>`, object `payload.decision`),
"undelivered" while it waits; the answer closes it with one conditional
write (a single winner). The work item's transitions are in
`work_item_events`. The answer records the identity, the channel (`cli`,
`chat`, `signed`) and the exact text.

**Identity of who answers**: a human, `human:<Unix user>`. Refused: an
agent session (`AGENT_MAIL_NAME`, runner lease, session bound with
`ameesh mail bind`), and a command whose ANCESTOR process carries an agent
identity (a variable removed by the agent stays visible in the initial
environment of its ancestors). Exception: the owner's `ameesh chat` session
(L123), verified through its recorded ancestor process — channel `chat`.
This is a guard against confusion, not a security boundary (agents and the
human share the Unix user): the boundary, for production or irreversible
actions, is the answer SIGNED (`--needs-signature`) with the owner's key,
using the `ameesh approve` mechanism (`mesh_approvals`, single use).
"""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import re
import sys
import time

from . import config as config_mod
from . import db as db_mod
from . import fil, identity, platform, registry, storage, work, work_lifecycle
from . import validation_deadlines as validations
from .config import NAME_RE, Config

#: schéma du JSON d'une demande (`--json`, clé `requests` de `ameesh decisions`)
SCHEMA = "ameesh-decision/1"
#: clé de l'objet décision dans `agent_mailbox.payload`
PAYLOAD_KEY = "decision"
#: nature de la ligne de demande et du courrier de réponse (`agent_mailbox.kind`)
KIND = "request"
REPLY_KIND = "reply"
STATES = ("pending", "answered", "withdrawn", "closed")
#: canaux d'une réponse (`signed` : signée par la clé du propriétaire)
CHANNELS = ("cli", "chat", "signed")
CHANNEL_LABELS = {"cli": "cli", "chat": "chat", "signed": "signed"}
#: destinataire d'une demande sans responsable connu : tout humain du mesh
OWNER_ANY = "human:*"
#: action et nature des approbations signées qui portent une réponse
APPROVAL_ACTION = "decision"
APPROVAL_KIND = "decision"
#: durée de validité d'une approbation signée par `decide --key`
SIGN_TTL_S = 3600.0
OPTION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,15}$")
MAX_OPTIONS = 12
MAX_QUESTION = 4000
MAX_LABEL = 600
MAX_ANSWER = 4000
#: bornes de lecture (une file, pas un export)
MAX_PENDING = 500
HISTORY_LIMIT = 50

#: variables posées par `ameesh chat` pour la session du harnais (L123)
CHAT_ENV = "AMEESH_CHAT"
CHAT_PID_ENV = "AMEESH_CHAT_PID"
#: variables d'un exécuteur (bail) : une commande qui les porte, ou dont un
#: ancêtre les porte, est celle d'un agent mené
RUNNER_ENV = ("AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID")
LEASE_ENV = ("AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH")

_TIME_RE = re.compile(r"^\s*(\d{1,2})[:h](\d{2})\s*$")

#: L233: a decision written in prose in mail to the owner's desk or to a
#: human never counts; `ameesh decisions` (and so `ameesh chat`) shows such
#: mail and offers to convert it (`ameesh decide ask --from-mail <id>`).
#: Recipients watched besides any `human:*`:
PROSE_RECIPIENTS = ("bureau",)
#: how far back mail is read (seconds)
PROSE_WINDOW_S = 24 * 3600.0
#: at most this many candidate mails, the most recent
PROSE_LIMIT = 500
#: cue words of a decision expected from the owner (French and English)
PROSE_CUES = re.compile(
    r"\bd[ée]cisions?\b|\bgo\s*\?|\bgo\s*/\s*no[\s-]*go\b|[àa]\s+valider\b"
    r"|[àa]\s+trancher\b|\barbitrages?\b|\bto\s+(?:validate|approve|decide)\b"
    r"|\bneeds?\s+(?:your\s+)?(?:approval|sign-?off)\b", re.IGNORECASE)


class DecisionError(RuntimeError):
    """Demande ou réponse refusée — toujours avec ce qu'il faut faire."""


# --------------------------------------------------------------------------
# identité de qui répond
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Answerer:
    """Qui répond : un humain, par la ligne de commande ou par son chat."""

    human: str
    channel: str            # cli | chat
    chat: str | None = None


def unix_user() -> str:
    return os.environ.get("USER") or getpass.getuser()


def current_human() -> str:
    """L'humain d'une commande lancée hors de toute session d'agent :
    `human:<utilisateur Unix>` (même règle que `ameesh budget`)."""
    return "human:%s" % unix_user()


def agent_in_ancestry(*, allow: str | None = None, chain=None, environ_of=None) -> str | None:
    """Le premier processus de l'ascendance (celui-ci compris) dont
    l'environnement INITIAL porte une identité d'agent : bail d'exécuteur
    (`AMEESH_RUNNER_ID`) ou `AGENT_MAIL_NAME` (autre que `allow`, l'identité
    du chat vérifié). Rend une ligne de diagnostic, ou None.

    Un ancêtre illisible (disparu, autre utilisateur) est ignoré ; un OS qui
    ne dit ni l'ascendance ni l'environnement ne trouve rien (garde contre la
    confusion, la frontière est la réponse signée)."""
    try:
        pids = list(platform.ancestry() if chain is None else chain)
    except platform.NotAvailable:
        return None
    read = environ_of or platform.environ
    for pid in pids:
        try:
            env = read(pid)
        except platform.NotAvailable:
            return None
        if not env:
            continue
        name = (env.get("AGENT_MAIL_NAME") or "").strip()
        runner = next((env[key] for key in RUNNER_ENV if env.get(key)), "")
        if runner:
            return "process %s: agent %s run by %s" % (pid, name or "?", runner)
        if name and name != allow:
            return "process %s: agent %s" % (pid, name)
    return None


def agent_session_reason(cfg: Config, db, *, chain=None, environ_of=None) -> str | None:
    """Pourquoi cette session est celle d'un agent ; None si c'est celle d'un humain.

    `AGENT_MAIL_NAME` posée (un agent, ou un chat dont la session n'est pas
    vérifiée), bail d'exécuteur dans l'environnement, session liée à un agent
    (`ameesh mail bind`), ancêtre qui porte une identité d'agent."""
    name = (os.environ.get("AGENT_MAIL_NAME") or "").strip()
    if name:
        return "this session belongs to agent %s (AGENT_MAIL_NAME)" % name
    runner = next((os.environ[key] for key in RUNNER_ENV if os.environ.get(key)), "")
    if runner:
        return "this session is run by runner %s" % runner
    binding = identity.resolve_binding(cfg, db)
    if binding.name:
        return "this session is bound to agent %s (ameesh mail bind)" % binding.name
    found = agent_in_ancestry(chain=chain, environ_of=environ_of)
    if found:
        return "this command was launched from an agent's session (%s)" % found
    return None


def human_session(cfg: Config, db, *, chain=None, environ_of=None) -> str:
    """L'humain de cette session (`human:<utilisateur>`), ou DecisionError si
    c'est celle d'un agent (`agent_session_reason`)."""
    reason = agent_session_reason(cfg, db, chain=chain, environ_of=environ_of)
    if reason:
        raise DecisionError("refused: %s; only a human answers a decision request — "
                            "answer from your own terminal, or in `ameesh chat`" % reason)
    return current_human()


def chat_session(cfg: Config, *, chain=None, environ_of=None) -> Answerer | None:
    """La session `ameesh chat` du propriétaire (L123), vérifiée ; None hors
    chat. DecisionError si la session se dit chat sans l'être."""
    name = (os.environ.get(CHAT_ENV) or "").strip()
    if not name:
        return None
    if (os.environ.get("AGENT_MAIL_NAME") or "").strip() != name \
            or any(os.environ.get(key) for key in RUNNER_ENV + LEASE_ENV):
        raise DecisionError("refused: inconsistent chat session (identity or runner lease) "
                            "— restart `ameesh chat` from your terminal")
    from . import chat as chat_mod
    record = chat_mod.verify_session(cfg, name, os.environ.get(CHAT_PID_ENV), chain=chain)
    found = agent_in_ancestry(allow=name, chain=chain, environ_of=environ_of)
    if found:
        raise DecisionError("refused: this command descends from an agent's session (%s), "
                            "not only from chat %s" % (found, name))
    human = str(record.get("human") or "")
    if human != current_human():
        raise DecisionError("refused: chat %s belongs to %s, not to %s"
                            % (name, human or "?", current_human()))
    return Answerer(human, "chat", name)


def answering_human(cfg: Config, db, *, chain=None, environ_of=None) -> Answerer:
    """Qui répond : le chat vérifié du propriétaire, sinon l'humain du terminal."""
    chat = chat_session(cfg, chain=chain, environ_of=environ_of)
    if chat is not None:
        return chat
    return Answerer(human_session(cfg, db, chain=chain, environ_of=environ_of), "cli")


# --------------------------------------------------------------------------
# lecture
# --------------------------------------------------------------------------

def _payload(row: dict) -> dict:
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return dict(payload.get(PAYLOAD_KEY) or {})


def view_of(row: dict, now: float | None = None) -> dict:
    """La demande d'une ligne de `agent_mailbox` (schéma `ameesh-decision/1`)."""
    now = time.time() if now is None else float(now)
    d = _payload(row)
    created = float(row.get("created_ts") or 0.0)
    closed = row.get("closed_ts")
    state = d.get("state") or "pending"
    # Mail delivery/old forwards can close the envelope without a decision.
    # A delivery timestamp proves closure, never an owner's answer.
    if closed is not None and state == "pending":
        state = "closed"
    due = d.get("due_ts")
    due = float(due) if due is not None else None
    lot = d.get("lot")
    owner = row.get("recipient") or OWNER_ANY
    return {
        "schema": SCHEMA,
        "id": int(row["id"]),
        "state": state,
        "project": d.get("project"),
        "lot": int(lot) if str(lot if lot is not None else "").isdigit() else lot,
        "lot_title": d.get("lot_title"),
        "requester": row.get("sender"),
        "owner": None if owner == OWNER_ANY else owner,
        "owner_source": d.get("owner_source"),
        "question": d.get("question") or "",
        "options": [dict(o) for o in d.get("options") or []],
        "recommend": d.get("recommend"),
        "why": d.get("why"),
        "urgent": bool(d.get("urgent")),
        "needs_signature": bool(d.get("needs_signature")),
        "supersedes": d.get("supersedes"),
        "superseded_by": d.get("superseded_by"),
        "due_ts": due,
        "overdue": state == "pending" and due is not None and now >= due,
        "created_ts": round(created, 3),
        "closed_ts": round(float(closed), 3) if closed is not None else None,
        "age_s": int(max(0.0, now - created)),
        "previous_state": d.get("previous_state"),
        "host": row.get("host"),
        "answer": d.get("answer"),
        "withdrawn": d.get("withdrawn"),
        "validation": d.get("validation"),
    }


def get(db, decision_id: int, now: float | None = None) -> dict | None:
    row = storage.of(db).decisions.get(int(decision_id))
    return view_of(row, now) if row else None


def require_current(view: dict) -> None:
    """Reject authority permanently replaced by a signed decision (#4991)."""
    if view.get("superseded_by") is not None:
        raise DecisionError("decision #%d superseded by decision #%d"
                            % (view["id"], view["superseded_by"]))


def pending(db, now: float | None = None, *, human: str | None = None) -> list[dict]:
    """Les demandes en attente, les plus anciennes d'abord ; `human` : celles
    qui lui sont adressées, et celles sans destinataire précis."""
    rows = [view_of(r, now) for r in storage.of(db).decisions.pending(limit=MAX_PENDING)]
    if human:
        rows = [r for r in rows if r["owner"] in (None, human)]
    return rows


def history(db, now: float | None = None, *, human: str | None = None,
            limit: int = HISTORY_LIMIT) -> list[dict]:
    """Les demandes closes (répondues, retirées ou closes sans réponse), les
    plus récentes d'abord."""
    rows = [view_of(r, now) for r in storage.of(db).decisions.history(limit=limit)]
    if human:
        rows = [r for r in rows if r["owner"] in (None, human)]
    return rows


# --------------------------------------------------------------------------
# destinataire
# --------------------------------------------------------------------------

def _human(value) -> str | None:
    from .notify import normalize_human
    return normalize_human(value)


def resolve_owner(cfg: Config, db, requester_row: dict | None, item: dict
                  ) -> tuple[str | None, str | None]:
    """(humain visé, source) : `lot` (fiche du plan), `agent` (demandeur),
    `assigne` (assigné du lot), `defaut` (`notify.default_human`),
    `humain_unique` (seul humain déclaré) ; (None, None) sinon."""
    packages = storage.of(db).packages
    ident = item.get("package_id") or item.get("package_parent")
    seen: set = set()
    while ident and ident not in seen and len(seen) < 16:
        seen.add(ident)
        package = packages.get(ident) or {}
        human = _human(package.get("responsible"))
        if human:
            return human, "lot"
        ident = package.get("parent")
    human = _human((requester_row or {}).get("responsible"))
    if human:
        return human, "agent"
    assignee = (item.get("assignee") or "").strip()
    if assignee.startswith(work.HUMAN_PREFIX):
        human = _human(assignee)
        if human:
            return human, "assigne"
    elif assignee and assignee != (requester_row or {}).get("name"):
        human = _human((registry.get(db, assignee) or {}).get("responsible"))
        if human:
            return human, "assigne"
    try:
        from .notify import NotifyConfigError, parse_config
        default = parse_config(cfg.notify).default_human
    except (NotifyConfigError, ValueError, TypeError):
        default = None
    if default:
        return default, "defaut"
    names = sorted(cfg.human_names)
    if len(names) == 1:
        human = _human(names[0])
        if human:
            return human, "humain_unique"
    return None, None


def _project(db, item: dict, requester_row: dict | None) -> str | None:
    """Le projet affiché d'une demande : celui du lot (`app`, `workstream`,
    équipe de sa fiche), sinon celui du demandeur (équipe, `workstream`)."""
    def text(value) -> str:
        return " ".join(str(value or "").split())
    name = text(item.get("app")) or text(item.get("workstream"))
    if not name and item.get("package_id"):
        name = text((storage.of(db).packages.get(item["package_id"]) or {}).get("team"))
    if not name:
        row = requester_row or {}
        name = (text(row.get("team")) or text(row.get("workstream"))
                or text(row.get("chantier")))
    return name or None


# --------------------------------------------------------------------------
# demande
# --------------------------------------------------------------------------

def parse_options(values) -> list[dict]:
    """`["a=Fusionner", "b=Attendre"]` → `[{key, label}]` ; DecisionError sinon."""
    out: list[dict] = []
    for value in values or ():
        key, sep, label = str(value).partition("=")
        key, label = key.strip(), " ".join(label.split())
        if not sep or not OPTION_KEY_RE.match(key) or not label:
            raise DecisionError("unreadable option: %r (expected --option a=\"label\", a key of "
                                "letters or digits, 16 characters at most)" % (value,))
        if any(o["key"].lower() == key.lower() for o in out):
            raise DecisionError("duplicate option %s" % key)
        if len(label) > MAX_LABEL:
            raise DecisionError("option %s: label too long (%d characters at most)"
                                % (key, MAX_LABEL))
        reason = fil.unreadable_reason(label)
        if reason:
            raise DecisionError("unreadable option %s — %s" % (key, reason))
        out.append({"key": key, "label": label})
    if len(out) > MAX_OPTIONS:
        raise DecisionError("%d options at most" % MAX_OPTIONS)
    return out


def parse_due(text: str, now: float | None = None) -> float:
    """`--by` : une durée (« 30m », « 2h », « 1d »), une heure du jour
    (« 18:00 », le lendemain si elle est passée) ou une date (« 2026-10-12
    09:00 », heure locale) → instant epoch."""
    from . import authority, stagnation
    now = time.time() if now is None else float(now)
    value = str(text or "").strip()
    match = _TIME_RE.match(value)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise DecisionError("unreadable deadline: %r" % value)
        local = time.localtime(now)
        moment = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hour, minute, 0,
                              0, 0, -1))
        return moment if moment > now else moment + 86400.0
    if re.match(r"^\s*\d{4}-\d{2}-\d{2}", value):
        try:
            return authority.parse_expiry(value) / 1_000_000.0
        except authority.AuthorityError as exc:
            raise DecisionError(str(exc)) from exc
    try:
        return now + stagnation.parse_duration(value)
    except stagnation.StaleError as exc:
        raise DecisionError("--by: %s (or a time 18:00, a date 2026-10-12 09:00)"
                            % exc) from exc


def _local(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _owner_label(owner: str | None) -> str:
    return owner or "any human of the mesh"


def request_text(decision: dict, *, requester: str, owner: str | None,
                 decision_id: int | None = None) -> str:
    """Le texte lisible d'une demande (fil, courrier, R12)."""
    lot = decision.get("lot")
    head = "Decision%s requested from %s by %s — work item #%s \"%s\"%s:" % (
        " #%d" % decision_id if decision_id is not None else "", _owner_label(owner), requester,
        lot, decision.get("lot_title") or "",
        " (project %s)" % decision["project"] if decision.get("project") else "")
    lines = [head, decision.get("question") or ""]
    for option in decision.get("options") or ():
        lines.append("  %s) %s%s" % (option["key"], option["label"],
                                     "   ← recommended" if option["key"] == decision.get("recommend")
                                     else ""))
    if decision.get("recommend"):
        lines.append("Recommendation: %s%s" % (
            decision["recommend"], " — %s" % decision["why"] if decision.get("why") else ""))
    validation = decision.get("validation") or {}
    due_label = validation.get("due_local") or (_local(decision["due_ts"]) if decision.get("due_ts") else None)
    tail = ["Deadline: %s (%s)." % (due_label, validation.get("timezone", "local"))] if due_label else []
    if validation.get("status") == "escalated":
        # #4542 : le remplaçant doit savoir pourquoi la demande lui arrive échue.
        tail.insert(0, "ESCALATED: no answer from the first validator by the deadline; "
                       "you are the substitute.")
    if validation.get("defaulted"):
        tail.append("Default: one business day.")
    lines.extend(validation.get("links") or [])
    if decision.get("urgent"):
        tail.append("URGENT.")
    if decision.get("needs_signature"):
        tail.append("SIGNED answer required (production or irreversible action).")
    if decision.get("supersedes") is not None:
        tail.append("Replaces decision #%d upon signed answer." % decision["supersedes"])
    if tail:
        lines.append(" ".join(tail))
    lines.append("Answer: ameesh decide %s <option | \"text\"> (or in ameesh chat)"
                 % (decision_id if decision_id is not None else "<number>"))
    return "\n".join(lines)


def _hold_lot(db, item: dict, *, note: str, actor: str) -> str:
    """Le lot passe en `waiting_human` (journal) ; rend son état."""
    state = work_lifecycle.phase(item)
    if state != "waiting_human" and "waiting_human" in work.TRANSITIONS.get(state, ()):
        try:
            return work.move(db, int(item["id"]), "waiting_human", note=note, actor=actor)["state"]
        except work.WorkError:
            state = (work.get(db, int(item["id"])) or item)["state"]
    work.note(db, int(item["id"]), note, actor=actor)
    return state


def ask(cfg: Config, db, *, requester: str, question: str, options=(), lot: str | None = None,
        new_lot: str | None = None, recommend: str | None = None, why: str | None = None,
        urgent: bool = False, due_ts: float | None = None, needs_signature: bool = False,
        source_mail: int | None = None, parent_lot: str | None = None,
        now: float | None = None, domain: str = "decision", issue_links=(),
        supersedes: int | None = None) -> dict:
    """Dépose une demande de décision de `requester` (un agent) ; rend la
    demande (`ameesh-decision/1`) avec `lot_state`. DecisionError si refusée
    (rien n'est déposé).

    `source_mail` (L233): the mail in which `requester` wrote this decision in
    prose; it must be theirs. Its lot is the default `lot`, and once the
    request names it, the mail is no longer shown as a prose decision."""
    now = time.time() if now is None else float(now)
    if supersedes is not None:
        if type(supersedes) is not int or supersedes <= 0:
            raise DecisionError("supersedes: positive decision id required")
        old = get(db, supersedes, now)
        if not old or old["state"] != "answered":
            raise DecisionError("supersedes requires an answered decision; pending uses withdraw")
        require_current(old)
        needs_signature = True
    mail_lot = None
    if source_mail is not None:
        mail_row = storage.of(db).decisions.source_mail(int(source_mail))
        if mail_row is None:
            raise DecisionError("--from-mail %s: no such mail" % source_mail)
        if mail_row.get("sender") != requester:
            raise DecisionError("--from-mail %s: written by %s, not by you (%s) — only its "
                                "author turns it into a request"
                                % (source_mail, mail_row.get("sender"), requester))
        mail_lot = str(mail_row.get("work_item_id") or "").strip()
        if lot is None and new_lot is None and mail_lot:
            lot = mail_lot
    question = str(question or "").strip()
    if not question:
        raise DecisionError("empty question (--question \"…\")")
    if len(question) > MAX_QUESTION:
        raise DecisionError("question too long (%d characters at most): summarize, and "
                            "refer to the thread or the work item for details" % MAX_QUESTION)
    reason = fil.unreadable_reason(question)
    if reason:
        raise DecisionError("unreadable question — %s" % reason)
    opts = parse_options(options)
    recommend = (recommend or "").strip() or None
    why = " ".join(str(why or "").split()) or None
    if recommend is not None:
        if not opts:
            raise DecisionError("--recommend without options: offer options (--option a=…)")
        match = [o["key"] for o in opts if o["key"].lower() == recommend.lower()]
        if not match:
            raise DecisionError("--recommend %s: not one of the options (%s)"
                                % (recommend, ", ".join(o["key"] for o in opts)))
        recommend = match[0]
    if why and recommend is None:
        raise DecisionError("--why goes with --recommend")
    if (lot is None) == (new_lot is None):
        raise DecisionError("--lot <id|ref> OR --new-lot \"title\" (exactly one): a "
                            "request is attached to a work item")
    if due_ts is not None and float(due_ts) <= now:
        raise DecisionError("deadline already passed (%s)" % _local(due_ts))

    def body(db):
        requester_row = registry.get(db, requester)
        if parent_lot is not None and new_lot is None:
            raise DecisionError("--parent-lot goes with --new-lot")
        if new_lot is not None:
            title = " ".join(new_lot.split())
            if not title:
                raise DecisionError("--new-lot: empty title")
            try:
                from . import assignments, work_projects
                project = work_projects.creation_project(cfg, db, requester)
                if not project:
                    parent_ref = parent_lot or mail_lot
                    parent = None
                    if parent_ref:
                        number = str(parent_ref).strip().lstrip("#")
                        # A follow-up may inherit from a completed lot. It reads
                        # its project; it does not assign or reopen that lot.
                        parent = (work.get(db, int(number)) if number.isdigit()
                                  else assignments.resolve_lot(db, parent_ref))
                        if parent is None:
                            raise DecisionError("parent work item %s not found" % parent_ref)
                    project = work_projects.creation_project(cfg, db, requester, parent=parent)
                # #540 : le lot de sa propre question reste au demandeur, qui
                # attend la réponse (jamais dans la file d'équipe)
                item = work._decision_lot(db, title=title, requester=requester, app=project,
                                          cfg=cfg)
                # #1391 : une question de décision est une demande légitime
                # (humaine) — pas d'avertissement de route ici, même si le titre
                # ressemble à une suite. La route du canon §2 est rendue par les
                # deux points d'entrée de création de travail (`work add`, `mail
                # --new-lot`), où le flux de suites de relecture a été observé.
            except work.WorkError as exc:
                raise DecisionError("work item not created — %s" % exc) from exc
            except assignments.AssignmentError as exc:
                raise DecisionError(str(exc)) from exc
        else:
            from . import assignments
            try:
                item = assignments.resolve_lot(db, lot)
            except assignments.AssignmentError as exc:
                raise DecisionError(str(exc)) from exc
            if item is None:
                raise DecisionError("work item %s not found: an open work item number, a unique "
                                    "reference, or --new-lot \"title\"" % lot)
        owner, source = resolve_owner(cfg, db, requester_row, item)
        project = _project(db, item, requester_row)
        try:
            route = validations.routing(cfg, project, domain, owner or OWNER_ANY)
        except (ValueError, KeyError) as exc:
            raise DecisionError(str(exc)) from exc
        recipient = route["nodes"][0]
        owner = recipient["human"]
        due = (validations.business_due(now, recipient["timezone"])
               if due_ts is None else float(due_ts))
        links = list(issue_links)
        if item.get("issue_ref"):
            ref = item["issue_ref"]
            if "#" in ref:
                repo, number = ref.rsplit("#", 1)
                links.append("https://github.com/%s/issues/%s" % (repo, number))
        validation = validations.metadata(route, due=due, defaulted=due_ts is None, links=links)
        lot_id = int(item["id"])
        previous = work_lifecycle.phase(item)
        if previous == "waiting_human":
            # une autre demande tient déjà le lot : son état d'avant est le bon
            inherited = [view_of(r, now).get("previous_state")
                         for r in storage.of(db).decisions.pending_for_lot(str(lot_id))]
            previous = next((p for p in inherited if p), None)
        decision = {
            "v": 1, "state": "pending", "question": question, "options": opts,
            "recommend": recommend, "why": why, "urgent": bool(urgent),
            "due_ts": round(due, 3), "needs_signature": bool(needs_signature),
            "validation": validation,
            "owner_source": source, "project": _project(db, item, requester_row),
            "lot": lot_id, "lot_title": item.get("title") or "", "previous_state": previous,
        }
        if source_mail is not None:
            decision["source_mail"] = int(source_mail)
        if supersedes is not None:
            decision["supersedes"] = supersedes

        def write(tx):
            row = storage.of(tx).decisions.create(
                sender=requester, recipient=owner or OWNER_ANY,
                body=request_text(decision, requester=requester, owner=owner),
                payload={PAYLOAD_KEY: decision}, work_item_id=str(lot_id), host=cfg.host)
            decision_id = int(row["id"])
            validations.register(tx, decision_id, route, now=now, due=due,
                                 defaulted=due_ts is None, links=validation["links"])
            fil.record(cfg, tx, sender=requester, recipients=[owner or OWNER_ANY],
                       text=request_text(decision, requester=requester, owner=owner,
                                         decision_id=decision_id),
                       ts=row.get("created_ts"),
                       project=fil.project_for(cfg, row.get("sender_project")),
                       lot=lot_id, mailbox_ids=[decision_id],
                       meta={"kind": KIND, "decision": decision_id,
                             "urgent": True if urgent else None,
                             "signature": "exigée" if needs_signature else None})
            state = _hold_lot(tx, item, actor=requester,
                              note="decision #%d requested from %s: %s" % (
                                  decision_id, _owner_label(owner),
                                  fil.excerpt(question, 160)))
            return row, decision_id, state

        row, decision_id, lot_state = write(db)
        out = get(db, decision_id, now) or {"id": decision_id}
        out["lot_state"] = lot_state
        return out

    # L165 (EP4) : router une décision vers un humain est un geste du pilote
    # hors base (elle ouvre un tour et déplace le lot). Le bail élu est
    # vérifié et journalisé AVANT la création du lot ou la première écriture ;
    # les deux sont dans la même transaction que le hold.
    from . import pilot_gesture
    ref = ("decision new lot: %s" % " ".join(new_lot.split())
           if new_lot is not None else "decision lot #%s" % str(lot).lstrip("#"))
    try:
        fence = pilot_gesture.gate(cfg, db, "decision", ref)
        if fence is None:
            with db.transaction() as tx:
                return body(tx)
        with db.transaction() as tx:
            fence.hold(tx)
            return body(tx)
    except pilot_gesture.GestureRefused as exc:
        raise DecisionError(str(exc)) from exc


# --------------------------------------------------------------------------
# decisions written in prose (L233)
# --------------------------------------------------------------------------

def prose_cues(text: str) -> list[str]:
    """The cue words of an owner decision found in `text` (lower case, once
    each, in order of appearance)."""
    out: list[str] = []
    for match in PROSE_CUES.finditer(text or ""):
        cue = " ".join(match.group(0).lower().split())
        if cue not in out:
            out.append(cue)
    return out


def prose_decisions(db, now: float | None = None, *, since_s: float = PROSE_WINDOW_S,
                    recipients=PROSE_RECIPIENTS, limit: int = PROSE_LIMIT) -> list[dict]:
    """Mail of the last `since_s` seconds in which an agent wrote to bureau
    (`recipients`) or to a human something that reads as a decision expected
    from the owner, and that no request names as its source. Oldest first:
    `{id, sender, recipient, lot, created_ts, age_s, cues, excerpt, convert}`
    — `convert`, the command its author runs to file it as a request."""
    now = time.time() if now is None else float(now)
    rows = storage.of(db).decisions.prose_candidates(
        recipients=list(recipients), since_ts=now - float(since_s), limit=int(limit))
    out = []
    for row in rows:
        cues = prose_cues(row.get("body") or "")
        if not cues:
            continue
        lot = str(row.get("work_item_id") or "").strip()
        lot = int(lot) if lot.isdigit() else (lot or None)
        out.append({
            "id": int(row["id"]), "sender": row["sender"], "recipient": row["recipient"],
            "lot": lot, "created_ts": round(float(row["created_ts"]), 3),
            "age_s": int(max(0.0, now - float(row["created_ts"]))), "cues": cues,
            "excerpt": fil.excerpt(row.get("body") or "", 220),
            "convert": "ameesh decide ask --from-mail %d%s --question \"…\" --option a=\"…\""
                       % (int(row["id"]), "" if lot else " --lot <id>"),
        })
    return out


def format_prose(row: dict, now: float | None = None) -> list[str]:
    """Lines of a prose decision for `ameesh decisions`."""
    return [
        "mail #%d %s → %s, %s ago%s — cues: %s" % (
            row["id"], row["sender"], row["recipient"], short_span(row["age_s"]),
            ", lot #%s" % row["lot"] if row.get("lot") else "", ", ".join(row["cues"])),
        "    « %s »" % row["excerpt"],
        "    not a request: %s should file it — %s" % (row["sender"], row["convert"]),
    ]


# --------------------------------------------------------------------------
# réponse
# --------------------------------------------------------------------------

#: Les deux mentions que `signer-owner.sh` (manaty/home, #3443) ajoute à
#: l'option d'une signature au nom de l'owner, décision #135142 b : la première
#: sans source, la seconde avec la session d'origine. Elles sont figées dans le
#: script : `match_option` ne reconnaît qu'elles, jamais un autre texte (#3454).
BUREAU_MENTIONS = (
    "— saisi par le bureau, délégation §10",
    "— réponse de l'owner relayée par le bureau, source ",
)

#: Le contrat de `--source` de `signer-owner.sh` : la session relayée est un mot
#: de [A-Za-z0-9._:@-], sans tiret initial, 200 caractères au plus. Une mention
#: qui ne le respecte pas n'est pas une signature du bureau (#3454, B1 de
#: ameesh-cx3) : `--text` peut y mettre du texte libre, qui reste libre.
RELAY_SOURCE_RE = re.compile(r"[A-Za-z0-9._:@][A-Za-z0-9._:@-]{0,199}")


def match_option(view: dict, text: str) -> str | None:
    """La clé de l'option que `text` désigne (casse de la clé ignorée), sinon None.

    Reconnaît « <clé> » et les deux textes que `signer-owner.sh` signe au nom
    de l'owner : « <clé> — saisi par le bureau, délégation §10 » et
    « <clé> — réponse de l'owner relayée par le bureau, source <session> »
    (#3443, décision #135142 b), <session> devant suivre le contrat de
    `--source` du script ([A-Za-z0-9._:@-], sans tiret initial, 200 au plus).
    Le texte signé reste entier : l'empreinte `answer_digest` couvre le texte
    et l'option, et une preuve déjà enregistrée garde l'option qu'elle a
    signée (#3454)."""
    wanted = str(text or "").strip()
    for option in view.get("options") or ():
        key = str(option.get("key") or "")
        if wanted.lower() == key.lower():
            return option["key"]
        head = key + " "
        if wanted[:len(head)].lower() != head.lower():
            continue
        mention = wanted[len(head):]
        if mention == BUREAU_MENTIONS[0]:
            return option["key"]
        relay = BUREAU_MENTIONS[1]
        if mention.startswith(relay) and RELAY_SOURCE_RE.fullmatch(mention[len(relay):]):
            return option["key"]
    return None


def _option_label(view: dict, key: str | None) -> str | None:
    for option in view.get("options") or ():
        if option.get("key") == key:
            return option.get("label")
    return None


def answer_digest(view: dict, text: str, option: str | None, human: str) -> str:
    """L'empreinte que signe une réponse (sha256 hex) : la demande (numéro,
    demandeur, lot, question, options, heure), la réponse exacte, son auteur."""
    from . import authority
    document = {
        "v": 1, "kind": "ameesh-decision-answer", "decision": int(view["id"]),
        "request": {"requester": view.get("requester"), "lot": view.get("lot"),
                    "question": view.get("question"), "options": view.get("options") or [],
                    "created": int(float(view.get("created_ts") or 0))},
        "answer": text, "option": option, "by": human,
    }
    # Preserve every pre-existing signature; only new replacement requests
    # extend the signed document with the exact authority they replace.
    if view.get("supersedes") is not None:
        document["request"]["supersedes"] = view["supersedes"]
    return authority.hash_artifact(json.dumps(document, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False))


def _approver_of(human: str, approver: str | None) -> str:
    name = (approver or human.split(":", 1)[-1]).strip()
    if not NAME_RE.match(name):
        raise DecisionError("owner key: give its name in the registry (--as NAME)")
    return name


def signature_hint(view: dict, text: str, option: str | None, human: str) -> str:
    digest = answer_digest(view, text, option, human)
    answer = text if " " not in text else '"%s"' % text.replace('"', '\\"')
    return ("decision #%d: SIGNED answer required (production or irreversible action). "
            "Sign it yourself, in your terminal:\n"
            "  ameesh decide %d %s --key <owner private key> [--as <key name>]\n"
            "or first sign its digest, then answer with --signed:\n"
            "  ameesh approve --key <key> --as <key name> --action %s --kind %s --hash %s\n"
            "  ameesh decide %d %s --signed"
            % (view["id"], view["id"], answer, APPROVAL_ACTION, APPROVAL_KIND, digest,
               view["id"], answer))


def _sign(db, view: dict, text: str, option: str | None, human: str, *,
          key_path: str | None, approver: str | None) -> dict:
    """La preuve d'une réponse signée : approbation `mesh_approvals` (clé de
    rôle `owner`) sur l'empreinte de la réponse, CONSOMMÉE par la décision."""
    from . import authority, signing
    digest = answer_digest(view, text, option, human)
    if key_path:
        try:
            seed = signing.read_private(os.path.expanduser(key_path))
        except (OSError, ValueError) as exc:
            raise DecisionError("unreadable private key: %s" % exc) from exc
        try:
            authority.create_approval(
                db, seed, approver=_approver_of(human, approver), action=APPROVAL_ACTION,
                artifact_kind=APPROVAL_KIND, artifact_hash=digest, ttl=SIGN_TTL_S,
                meta={"decision": int(view["id"]), "by": human})
        except authority.AuthorityError as exc:
            raise DecisionError("signature rejected: %s" % exc) from exc
    row, verdict = authority.find_approval(db, APPROVAL_ACTION, digest,
                                           consume_by="decision:%d" % int(view["id"]))
    if row is None or not verdict.ok:
        raise DecisionError("signed answer rejected: %s\n%s" % (
            verdict.reason, signature_hint(view, text, option, human)))
    return {"approval_id": int(row["id"]), "approver": row.get("approver"),
            "signature_key": row.get("signature_key"), "digest": digest}


def verify_proof(db, view: dict, *, live: bool = False) -> tuple[bool, str]:
    """Revérifie la preuve d'une réponse signée : approbation consommée par
    CETTE décision, empreinte recalculée, clé `owner` toujours valide,
    signature Ed25519."""
    from . import authority, signing
    answer = view.get("answer") or {}
    proof = answer.get("proof") or {}
    if not proof:
        return False, "unsigned answer"
    digest = answer_digest(view, answer.get("text") or "", answer.get("option"),
                           answer.get("by") or "")
    if digest != proof.get("digest"):
        return False, "the digest no longer matches the recorded answer"
    rows = storage.of(db).approvals.recent(action=APPROVAL_ACTION, artifact_hash=digest, limit=20)
    row = next((r for r in rows if int(r["id"]) == int(proof.get("approval_id") or 0)), None)
    if row is None:
        return False, "approval #%s not found" % proof.get("approval_id")
    if row.get("consumed_by") != "decision:%d" % int(view["id"]):
        return False, "approval #%s not consumed by this decision" % row["id"]
    info = authority.key_info(db, row.get("approver") or "") or {}
    if (info.get("key_role") or "") != "owner" or not info.get("public_key"):
        return False, "the key of %s is not (or no longer) an owner key" % row.get(
            "approver")
    if info.get("key_revoked_ts"):
        return False, "key of %s revoked since" % row.get("approver")
    if info.get("public_key_fingerprint") != row.get("signature_key"):
        return False, "key of %s changed since the signature" % row.get("approver")
    try:
        parts = str(row.get("signed_payload") or "").split("|", 2)
        signed = json.loads(parts[2]) if len(parts) == 3 else {}
        ok = (signed.get("artifact_hash") == digest and signed.get("action") == APPROVAL_ACTION
              and signing.verify(authority.unb64(info["public_key"]),
                                 str(row["signed_payload"]).encode("utf-8"),
                                 authority.unb64(row.get("signature") or "")))
    except (ValueError, authority.AuthorityError):
        ok = False
    if not ok:
        return False, "invalid Ed25519 signature"
    if live:
        # A replacement takes effect now, so a historical valid signature is
        # insufficient: require its exact, still-live approval and nonce.
        try:
            nonce = storage.of(db).nonces.state(row["approver"], row["nonce"])
            fields = ("approver", "action", "artifact_kind", "artifact_hash", "decision", "nonce")
            ok = (parts[:2] == [authority.DOMAIN, "approval"]
                  and all(signed.get(k) == row.get(k) for k in fields)
                  and signed.get("decision") == "approved"
                  and signed.get("artifact_kind") == APPROVAL_KIND
                  and type(signed.get("expires")) is int and type(signed.get("ts")) is int
                  and signed["expires"] > time.time() * 1_000_000
                  and signed["ts"] <= (time.time() + 120) * 1_000_000
                  and signed["expires"] > signed["ts"]
                  and int(round(float(row["expires_ts"]) * 1_000_000)) == signed["expires"]
                  and int(round(float(row["created_ts"]) * 1_000_000)) == signed["ts"]
                  and nonce and nonce.get("consumed_by") == "decision:%d" % view["id"])
        except (KeyError, TypeError, ValueError, OverflowError):
            ok = False
        if not ok:
            return False, "supersession requires a live approved signature and consumed nonce"
    return True, "signed with the owner key %s (%s…), approval #%s" % (
        row.get("approver"), str(row.get("signature_key") or "")[:12], row["id"])


def _closed_reason(view: dict) -> str:
    if view["state"] == "closed":
        return "decision #%d closed without an answer or withdrawal" % view["id"]
    if view["state"] == "withdrawn":
        who = (view.get("withdrawn") or {}).get("by") or view.get("requester")
        return "decision #%d withdrawn by %s" % (view["id"], who)
    answer = view.get("answer") or {}
    return "decision #%d already answered by %s (%s): \"%s\"" % (
        view["id"], answer.get("by") or "?", CHANNEL_LABELS.get(answer.get("channel"), "?"),
        fil.excerpt(answer.get("text") or "", 120))


def _release_lot(db, view: dict, *, actor: str, note: str, replay: bool = False) -> dict:
    """Après la réponse (ou le retrait) : le lot revient à son état d'avant la
    demande s'il attend encore et qu'aucune autre demande ne le tient ; sinon
    une note. Rend `{lot, state, moved, others}`.

    `replay` (#3892) : une réponse identique est rejouée ; un lot déjà sorti
    de `waiting_human`, ou retenu par une autre demande, ne reçoit pas une
    seconde note."""
    lot = view.get("lot")
    if lot is None or not str(lot).isdigit():
        return {"lot": lot, "state": None, "moved": False, "others": 0}
    item = work.get(db, int(lot))
    if item is None:
        return {"lot": lot, "state": None, "moved": False, "others": 0}
    others = [r for r in storage.of(db).decisions.pending_for_lot(str(lot))
              if int(r["id"]) != int(view["id"])]
    if replay and (item["state"] != "waiting_human" or others):
        return {"lot": int(lot), "state": item["state"], "moved": False, "others": len(others)}
    target = view.get("previous_state")
    if item["state"] == "waiting_human" and not others and target \
            and target in work.TRANSITIONS["waiting_human"]:
        try:
            work.move(db, int(lot), target, note="%s — the work item returns to %s" % (note, target),
                      actor=actor)
            return {"lot": int(lot), "state": target, "moved": True, "others": 0}
        except work.WorkError:
            item = work.get(db, int(lot)) or item
    if replay:
        # une réponse identique ne laisse jamais de seconde note sur le lot
        return {"lot": int(lot), "state": item["state"], "moved": False,
                "others": len(others)}
    suffix = ""
    if item["state"] == "waiting_human":
        suffix = (" — the work item stays waiting: %d other decision(s) pending" % len(others)
                  if others else " — the work item stays in waiting_human (it was before the request)")
    work.note(db, int(lot), note + suffix, actor=actor)
    return {"lot": int(lot), "state": item["state"], "moved": False, "others": len(others)}


def answer_text(view: dict) -> str:
    """Le courrier de réponse au demandeur (lisible, R12)."""
    answer = view.get("answer") or {}
    channel = answer.get("channel")
    if answer.get("option"):
        what = "option %s \"%s\"" % (answer["option"], answer.get("option_label") or "")
    else:
        what = "free answer"
    lines = ["Decision #%d — answer from owner %s (%s) on work item #%s \"%s\": %s." % (
        view["id"], answer.get("by"), CHANNEL_LABELS.get(channel, channel), view.get("lot"),
        view.get("lot_title") or "", what),
        "Exact text: \"%s\"" % (answer.get("text") or ""),
        "Your question: \"%s\"" % fil.excerpt(view.get("question") or "", 300)]
    if channel == "signed":
        proof = answer.get("proof") or {}
        lines.append("Answer SIGNED with the owner key (approval #%s, key %s…), "
                     "verified and consumed: `ameesh decide show %d`."
                     % (proof.get("approval_id"), str(proof.get("signature_key") or "")[:12],
                        view["id"]))
    if view.get("supersedes") is not None:
        lines.append("Decision #%d supersedes decision #%d; the former authority is refused."
                     % (view["id"], view["supersedes"]))
    lines.append("This answer, recorded in the decision queue, is the owner's "
                 "decision: resume the work item accordingly (verifiable: ameesh decide "
                 "show %d)." % view["id"])
    return "\n".join(lines)


class _DecisionRace(Exception):
    """La clôture conditionnelle a perdu la course : rien n'est écrit, on relit."""


def _same_answer(view: dict, text: str, option: str | None, human: str) -> bool:
    """La réponse close est-elle exactement celle que l'humain re-saisit ?"""
    answer = view.get("answer") or {}
    return (view.get("state") == "answered"
            and answer.get("by") == human
            and (answer.get("text") or "").strip() == text
            and (answer.get("option") or None) == (option or None))


def _reply_for(db, view: dict, human: str) -> dict | None:
    """Le courrier déjà déposé pour cette réponse, ou None (lecture locale)."""
    try:
        return storage.of(db).decisions.reply_for(int(view["id"]), sender=human)
    except NotImplementedError:
        return None


def _answer_mail(cfg: Config, db, view: dict, *, human: str,
                 after: list | None = None) -> tuple[int | None, str | None, bool]:
    """Dépose le courrier de la réponse gardée, ou rend celui déjà déposé.

    Rejouable : le courrier se recompose depuis `payload.decision.answer`. Un
    courrier manquant (panne entre la réponse et lui) est renvoyé, un courrier
    déjà déposé n'est jamais doublé. Une panne de base lève : la transaction
    de la réponse est annulée et la re-saisie la refait."""
    from . import identity, mail, resident_queue
    existing = _reply_for(db, view, human)
    if existing is not None:
        return int(existing["id"]), None, False
    binding = identity.resolve_binding(cfg, db)
    human_account = ""
    if binding.source == "none":
        user, host = resident_queue.local_account(cfg)
        human_account = "%s@%s" % (user or "?", host or "?")
    answer = view.get("answer") or {}
    payload = {"human": True, "urgent": True,
               **({"human_account": human_account} if human_account else {}),
               "decision_answer": {"decision": view["id"], "lot": view.get("lot"),
                                   "by": human, "channel": answer.get("channel"),
                                   "option": answer.get("option"),
                                   "text": answer.get("text")}}
    try:
        message_id = mail.send(
            db, human, view["requester"], answer_text(view), host=cfg.host, kind=REPLY_KIND,
            work_item_id=str(view["lot"]) if view.get("lot") is not None else None,
            # L125 : le courrier d'un humain réveille tout de suite (jamais
            # retenu par la fenêtre de regroupement, jamais passif). #795 : la
            # réponse de l'owner est prioritaire (0018 §2) — elle préempte le
            # tour du demandeur, qui repart avec elle en tête.
            payload=payload,
            thread_meta={"decision": view["id"], "channel": answer.get("channel")},
            idempotency_key="decision:%d:reply" % int(view["id"]),
            after_commit=after)
    except (fil.UnreadableBody, storage.MailboxRefused) as exc:
        return None, ("answer recorded, but the mail to %s was not delivered (%s): "
                      "let them know" % (view["requester"], exc)), False
    except Exception as exc:  # noqa: BLE001 - la réponse est gardée : le dire
        if isinstance(exc, db_mod.DbError):
            raise
        return None, ("answer recorded, but the mail to %s was not delivered (%s): "
                      "let them know" % (view["requester"], exc)), False
    return int(message_id), None, True


def _replay_answer(cfg: Config, db, view: dict, *, text: str, option: str | None,
                   human: str, now: float) -> dict:
    """Rejoue une réponse identique : libère le lot s'il attend encore et
    renvoie le courrier s'il manque, sans second effet."""
    note = ("decision #%d: identical answer replayed by %s (already recorded)"
            % (view["id"], human))
    if storage.is_remote(db):
        lot = _release_lot(db, view, actor=human, note=note, replay=True)
        message_id, warning, sent = _answer_mail(cfg, db, view, human=human)
        return {"decision": view, "lot": lot, "message_id": message_id,
                "warning": warning, "replayed": True, "mail_sent": sent}
    after: list = []
    with db.transaction() as tx:
        latest = get(tx, view["id"], now) or view
        if not _same_answer(latest, text, option, human):
            raise DecisionError(_closed_reason(latest))
        view = latest
        lot = _release_lot(tx, view, actor=human, note=note, replay=True)
        message_id, warning, sent = _answer_mail(cfg, tx, view, human=human, after=after)
    for record in after:
        record(cfg, db)
    return {"decision": view, "lot": lot, "message_id": message_id,
            "warning": warning, "replayed": True, "mail_sent": sent}


def answer(cfg: Config, db, decision_id: int, text: str, *, human: str, channel: str,
           key_path: str | None = None, approver: str | None = None, signed: bool = False,
           now: float | None = None) -> dict:
    """Enregistre la réponse de `human` (déjà identifié : `answering_human`).

    #3892 : la réponse et la libération du lot sont UNE transaction ; le
    courrier se recompose depuis la réponse gardée (`_answer_mail`). Une
    re-saisie identique est un rejeu sans double effet : ni seconde clôture,
    ni seconde note, ni second courrier.

    Rend `{decision, lot, message_id, warning}` (+ `replayed`). DecisionError :
    demande inconnue ou close, humain non visé, texte illisible, signature
    exigée absente ou refusée."""
    now = time.time() if now is None else float(now)
    if channel not in CHANNELS:
        raise DecisionError("unknown channel: %r" % channel)
    view = get(db, decision_id, now)
    if view is None:
        raise DecisionError("decision #%s not found (ameesh decisions)" % decision_id)
    if view["owner"] and view["owner"] != human and not validations.can_answer(db, decision_id, human):
        raise DecisionError("decision #%d is addressed to %s: %s does not answer it"
                            % (view["id"], view["owner"], human))
    text = str(text or "").strip()
    if not text:
        raise DecisionError("empty answer: an option (%s) or a text"
                            % (", ".join(o["key"] for o in view["options"]) or "none"))
    if len(text) > MAX_ANSWER:
        raise DecisionError("answer too long (%d characters at most)" % MAX_ANSWER)
    reason = fil.unreadable_reason(text)
    if reason:
        raise DecisionError("unreadable answer — %s" % reason)
    option = match_option(view, text)
    if view["state"] != "pending":
        if validations.can_answer(db, decision_id, human):
            followup = validations.late_response(cfg, db, view, human=human,
                                                  text=text, option=option, now=now)
            if followup is not None:
                return followup
        if _same_answer(view, text, option, human):
            return _replay_answer(cfg, db, view, text=text, option=option,
                                  human=human, now=now)
        raise DecisionError(_closed_reason(view))
    signed_requested = bool(key_path or signed)
    if signed_requested:
        if channel == "chat":
            raise DecisionError("a signed answer is given in your terminal, not in the "
                                "chat:\n" + signature_hint(view, text, option, human))
    elif view["needs_signature"]:
        raise DecisionError(signature_hint(view, text, option, human))
    if signed_requested:
        channel = "signed"

    def record_for(proof) -> dict:
        return {"state": "answered", "answer": {
            "by": human, "channel": channel, "text": text, "option": option,
            "option_label": _option_label(view, option), "ts": round(now, 3),
            "host": cfg.host, "proof": proof}}

    def said(view: dict) -> str:
        if option:
            return "option %s \"%s\"" % (option, _option_label(view, option) or "")
        return "\"%s\"" % fil.excerpt(text, 160)

    if storage.is_remote(db):
        # Pas de transaction sur un exécuteur médié : la clôture est déjà UNE
        # écriture conditionnelle ; le courrier garde sa clé d'idempotence et
        # la re-saisie identique est un rejeu.
        proof = _sign(db, view, text, option, human, key_path=key_path,
                      approver=approver) if signed_requested else None
        closed = storage.of(db).decisions.close(int(view["id"]), status="answered",
                                                record=record_for(proof))
        if closed is None:
            latest = get(db, decision_id, now)
            if latest is not None and _same_answer(latest, text, option, human):
                return _replay_answer(cfg, db, latest, text=text, option=option,
                                      human=human, now=now)
            raise DecisionError(_closed_reason(latest) if latest else
                                "decision #%s not found" % decision_id)
        view = view_of(closed, now)
        lot = _release_lot(db, view, actor=human, note="decision #%d answered by %s (%s): %s"
                           % (view["id"], human, CHANNEL_LABELS[channel], said(view)))
        message_id, warning, _sent = _answer_mail(cfg, db, view, human=human)
        return {"decision": view, "lot": lot, "message_id": message_id,
                "warning": warning, "replayed": False}

    after: list = []
    try:
        with db.transaction() as tx:
            proof = _sign(tx, view, text, option, human, key_path=key_path,
                          approver=approver) if signed_requested else None
            closed = storage.of(tx).decisions.close(int(view["id"]), status="answered",
                                                    record=record_for(proof))
            if closed is None:
                # Une autre réponse a gagné : la transaction est annulée (y
                # compris une approbation consommée), puis on relit.
                raise _DecisionRace()
            view = view_of(closed, now)
            validations.response(tx, view, human=human, text=text, now=now)
            lot = _release_lot(
                tx, view, actor=human,
                note="decision #%d answered by %s (%s): %s"
                     % (view["id"], human, CHANNEL_LABELS[channel], said(view)))
            message_id, warning, _sent = _answer_mail(
                cfg, tx, view, human=human, after=after)
    except _DecisionRace:
        latest = get(db, decision_id, now)
        if latest is not None and validations.can_answer(db, decision_id, human):
            followup = validations.late_response(cfg, db, latest, human=human,
                                                  text=text, option=option, now=now)
            if followup is not None:
                return followup
        if latest is not None and _same_answer(latest, text, option, human):
            return _replay_answer(cfg, db, latest, text=text, option=option,
                                  human=human, now=now)
        raise DecisionError(_closed_reason(latest) if latest else
                            "decision #%s not found" % decision_id)
    for record in after:
        record(cfg, db)
    return {"decision": view, "lot": lot, "message_id": message_id,
            "warning": warning, "replayed": False}


def withdraw(cfg: Config, db, decision_id: int, *, requester: str, why: str | None = None,
             now: float | None = None) -> dict:
    """Le demandeur retire sa demande ; le lot revient à son état d'avant."""
    now = time.time() if now is None else float(now)
    view = get(db, decision_id, now)
    if view is None:
        raise DecisionError("decision #%s not found" % decision_id)
    if view["state"] != "pending":
        raise DecisionError(_closed_reason(view))
    if view["requester"] != requester:
        raise DecisionError("decision #%d: only its requester (%s) can withdraw it"
                            % (view["id"], view["requester"]))
    why = " ".join(str(why or "").split()) or None
    record = {"state": "withdrawn", "withdrawn": {"by": requester, "why": why,
                                                  "ts": round(now, 3)}}

    def close_and_release(conn) -> tuple[dict, dict, dict]:
        closed = storage.of(conn).decisions.close(int(view["id"]), status="delivered",
                                                  record=record)
        if closed is None:
            raise _DecisionRace()
        closed_view = view_of(closed, now)
        text = "decision #%d withdrawn by %s%s" % (
            closed_view["id"], requester, ": %s" % why if why else "")
        lot = _release_lot(conn, closed_view, actor=requester, note=text)
        return closed_view, lot, closed

    if storage.is_remote(db):
        view, lot, closed = close_and_release(db)
    else:
        # #3892 : le retrait et la libération du lot sont UNE transaction,
        # comme la réponse ; une panne entre les deux ne laisse plus le lot
        # en waiting_human sans sa demande.
        try:
            with db.transaction() as tx:
                view, lot, closed = close_and_release(tx)
                validations.withdrawn(tx, view, by=requester, now=now)
        except _DecisionRace:
            latest = get(db, decision_id, now)
            raise DecisionError(_closed_reason(latest) if latest else
                                "decision #%s not found" % decision_id)
    fil.record(cfg, db, sender=requester, recipients=[view["owner"] or OWNER_ANY],
               text="Decision #%d withdrawn by %s%s — question: \"%s\"" % (
                   view["id"], requester, " (%s)" % why if why else "",
                   fil.excerpt(view["question"], 300)),
               project=fil.project_for(cfg, closed.get("sender_project")), lot=view.get("lot"),
               meta={"kind": KIND, "decision": view["id"], "state": "withdrawn"})
    return {"decision": view, "lot": lot}


# --------------------------------------------------------------------------
# vues : alertes, en-tête de `ameesh projects`, file de `ameesh decisions`
# --------------------------------------------------------------------------

def escalated(view: dict) -> bool:
    """#4542 : la demande a été escaladée au remplaçant après son échéance."""
    return (view.get("validation") or {}).get("status") == "escalated"


def describe(view: dict, limit: int = 220) -> str:
    """Une ligne : numéro, demandeur, question, options, recommandation."""
    options = " ; ".join("%s) %s" % (o["key"], fil.excerpt(o["label"], 40))
                         for o in view.get("options") or ())
    return "decision #%d from %s for %s%s: \"%s\"%s%s — answer: ameesh decide %d <option> " \
           "(or ameesh chat)" % (
               view["id"], view.get("requester"), _owner_label(view.get("owner")),
               " (escalated)" if escalated(view) else "",
               fil.excerpt(view.get("question") or "", limit),
               " — %s" % options if options else "",
               "; recommended: %s" % view["recommend"] if view.get("recommend") else "",
               view["id"])


def board_summary(rows, *, viewer: str | None = None, now: float | None = None) -> dict:
    """L'en-tête de `ameesh projects` (clé `decisions` de `ameesh-projects/1`) :
    demandes en attente, celles qui attendent `viewer` (et celles sans
    destinataire précis), la plus ancienne, urgentes, échues."""
    now = time.time() if now is None else float(now)
    rows = list(rows or ())
    mine = [r for r in rows if viewer is None or r.get("owner") in (viewer, OWNER_ANY, None)]
    oldest = min((float(r["created_ts"]) for r in mine if r.get("created_ts") is not None),
                 default=None)
    return {
        "viewer": viewer,
        "pending": len(rows),
        "mine": len(mine),
        "oldest_ts": round(oldest, 3) if oldest is not None else None,
        "urgent": sum(1 for r in mine if r.get("urgent")),
        "overdue": sum(1 for r in mine if r.get("due_ts") is not None
                       and now >= float(r["due_ts"])),
    }


def short_span(seconds) -> str:
    """Une durée courte : 42s, 12m, 2h05, 1d03h."""
    seconds = int(max(0, seconds or 0))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh%02d" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd%02dh" % (seconds // 86400, (seconds % 86400) // 3600)


def headline(summary: dict | None, now: float) -> str | None:
    """« 3 décisions t'attendent, la plus ancienne depuis 2h05 … » ; None sans objet."""
    if not summary:
        return None
    count = summary["mine"] if summary.get("viewer") else summary["pending"]
    if not count or summary.get("oldest_ts") is None:
        return None
    extra = []
    if summary.get("urgent"):
        extra.append("%d urgent" % summary["urgent"])
    if summary.get("overdue"):
        extra.append("%d overdue" % summary["overdue"])
    plural = count > 1
    if summary.get("viewer"):
        head = "%d decision%s waiting for you" % (count, "s" if plural else "")
        hint = "ameesh decisions, or ameesh chat"
    else:
        head = "%d decision%s waiting for a human" % (count, "s" if plural else "")
        hint = "ameesh decisions"
    return "%s, the oldest for %s%s — %s" % (
        head, short_span(now - float(summary["oldest_ts"])),
        " (of which %s)" % ", ".join(extra) if extra else "", hint)


def format_view(view: dict, now: float | None = None) -> list[str]:
    """Les lignes d'une demande dans `ameesh decisions` et `decide show`."""
    now = time.time() if now is None else float(now)
    flags = []
    if view.get("urgent"):
        flags.append("URGENT")
    if view.get("overdue"):
        flags.append("OVERDUE")
    if escalated(view):
        flags.append("ESCALATED")
    if view.get("needs_signature"):
        flags.append("signature required")
    head = "#%d  %s ago · %s · work item #%s \"%s\" · from %s → %s%s" % (
        view["id"], short_span(now - float(view["created_ts"])),
        view.get("project") or "(no project)",
        view.get("lot"), fil.excerpt(view.get("lot_title") or "", 60), view.get("requester"),
        _owner_label(view.get("owner")), "  [%s]" % "] [".join(flags) if flags else "")
    lines = [head]
    if view.get("supersedes") is not None:
        lines.append("      supersedes decision #%d%s" % (
            view["supersedes"], "" if view["state"] == "answered" else
            " upon signed answer" if view["state"] == "pending" else " (not effective)"))
    if view.get("superseded_by") is not None:
        lines.append("      superseded by decision #%d; authority refused" % view["superseded_by"])
    for line in (view.get("question") or "").splitlines() or [""]:
        lines.append("      %s" % line)
    for option in view.get("options") or ():
        lines.append("        %s) %s%s" % (option["key"], option["label"],
                                           "   ← recommended"
                                           if option["key"] == view.get("recommend") else ""))
    if view.get("recommend"):
        lines.append("      recommendation: %s%s" % (
            view["recommend"], " — %s" % view["why"] if view.get("why") else ""))
    if view["state"] == "pending":
        due = view.get("due_ts")
        lines.append("      deadline %s%s · answer: ameesh decide %d <option | \"text\">%s" % (
            _local(due) if due else "—",
            (" (overdue by %s)" % short_span(now - due) if now >= due else
             " (in %s)" % short_span(due - now)) if due else "",
            view["id"], " --key <key>" if view.get("needs_signature") else ""))
    elif view["state"] == "answered":
        answer = view.get("answer") or {}
        lines.append("      answered by %s (%s) on %s: \"%s\"" % (
            answer.get("by"), CHANNEL_LABELS.get(answer.get("channel"), "?"),
            _local(answer.get("ts") or view.get("closed_ts") or now),
            fil.excerpt(answer.get("text") or "", 200)))
    elif view["state"] == "closed":
        lines.append("      closed on %s without an answer or withdrawal" %
                     _local(view["closed_ts"]))
    else:
        withdrawn = view.get("withdrawn") or {}
        lines.append("      withdrawn by %s on %s%s" % (
            withdrawn.get("by") or view.get("requester"),
            _local(withdrawn.get("ts") or view.get("closed_ts") or now),
            ": %s" % withdrawn["why"] if withdrawn.get("why") else ""))
    return lines


def queue(db, *, human: str | None = None, include_closed: bool = False,
          now: float | None = None) -> dict:
    """Les demandes pour `ameesh decisions` : `pending` (les plus anciennes
    d'abord) et, avec `include_closed`, `closed` (les plus récentes d'abord :
    réponses, retraits et demandes closes sans réponse)."""
    now = time.time() if now is None else float(now)
    return {"pending": pending(db, now, human=human),
            "closed": history(db, now, human=human) if include_closed else []}


# --------------------------------------------------------------------------
# entrée : ameesh decide
# --------------------------------------------------------------------------

USAGE = ("usage: ameesh decide ask (--lot <id|ref> | --new-lot \"title\") --question \"…\" "
         "--option a=\"…\" [--option b=\"…\"] [--recommend a [--why \"…\"]] [--urgent] "
         "[--by 2h] [--needs-signature] [--from-mail <id>] [--json]\n"
         "        ameesh decide <id> <option | \"free text\"> "
         "[--key FILE [--as NAME] | --signed] [--json]\n"
         "        ameesh decide withdraw <id> [--why \"…\"] [--json]\n"
         "        ameesh decide repair-forward <id> [--json]\n"
         "        ameesh decide show <id> [--json]")


def _decision_id(text) -> int | None:
    value = str(text or "").strip()
    value = value[1:] if value.startswith("#") else value
    return int(value) if value.isdigit() else None


def _dump(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _open(cfg: Config):
    return db_mod.open_db(cfg)


def _parser(prog: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=prog, description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)


def cmd_ask(cfg: Config, argv: list[str]) -> int:
    parser = _parser("ameesh decide ask")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--lot", default=None, help="open work item: number or unique reference")
    target.add_argument("--new-lot", default=None, help="create the work item (assigned to the requester)")
    parser.add_argument("--parent-lot", default=None,
                        help="project fallback for --new-lot; default: the source mail's lot")
    parser.add_argument("--from-mail", type=int, default=None, metavar="ID",
                        help="L233: the mail in which you wrote this decision in prose "
                             "(its lot is the default --lot)")
    parser.add_argument("--question", required=True)
    parser.add_argument("--option", action="append", default=[], metavar="KEY=LABEL")
    parser.add_argument("--recommend", default=None, metavar="KEY")
    parser.add_argument("--why", default=None, help="why this recommendation")
    parser.add_argument("--urgent", action="store_true")
    parser.add_argument("--by", default=None,
                        help="deadline: 30m, 2h, 1d, 18:00 or \"2026-10-12 09:00\" "
                             "(default: one business day in recipient timezone)")
    parser.add_argument("--domain", default="decision", help="configured validation authority domain")
    parser.add_argument("--issue", action="append", default=[], help="related issue URL (repeatable)")
    parser.add_argument("--needs-signature", action="store_true",
                        help="production or irreversible action: signed answer required")
    parser.add_argument("--supersedes", type=int, metavar="ID",
                        help="replace an answered decision upon this signed answer")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get(CHAT_ENV):
        raise DecisionError("the chat relays the owner's requests to orchestrators; "
                            "it does not file decision requests")
    due = parse_due(args.by) if args.by else None
    db = _open(cfg)
    try:
        binding = identity.resolve_binding(cfg, db)
        if not binding.ok:
            raise DecisionError(
                "unbound requester (%s): a decision request is filed by an agent, "
                "whose identity is set by the runner (AGENT_MAIL_NAME)"
                % (binding.reason or "no identity"))
        view = ask(cfg, db, requester=binding.name, question=args.question,
                   options=args.option, lot=args.lot, new_lot=args.new_lot,
                   recommend=args.recommend, why=args.why, urgent=args.urgent, due_ts=due,
                   needs_signature=args.needs_signature, source_mail=args.from_mail,
                   parent_lot=args.parent_lot, domain=args.domain, issue_links=args.issue,
                   supersedes=args.supersedes)
    finally:
        db.close()
    if args.json:
        _dump(view)
        return 0
    print("decision #%d requested from %s%s — work item #%s \"%s\": %s" % (
        view["id"], _owner_label(view["owner"]),
        " (%s)" % {"lot": "responsible of the work item's plan entry", "agent": "your responsible",
                   "assigne": "responsible of the work item's assignee",
                   "defaut": "default human for alerts",
                   "humain_unique": "only declared human"}.get(view["owner_source"], "")
        if view["owner_source"] else "",
        view["lot"], view.get("lot_title") or "", view.get("lot_state")))
    print("deadline %s%s%s; the owner is notified, and reminded at the deadline." % (
        (view.get("validation") or {}).get("due_local") or _local(view["due_ts"]), " · URGENT" if view["urgent"] else "",
        " · SIGNED answer required" if view["needs_signature"] else ""))
    print("The answer will be delivered to you by mail (it wakes you up): do not wait in "
          "a loop, end your turn. Withdraw the request: ameesh decide withdraw %d"
          % view["id"])
    return 0


def cmd_answer(cfg: Config, argv: list[str]) -> int:
    parser = _parser("ameesh decide")
    parser.add_argument("id")
    parser.add_argument("answer", nargs="+", help="an option, or free text")
    proof = parser.add_mutually_exclusive_group()
    proof.add_argument("--key", default=None,
                       help="owner private key: signs the answer (ameesh approve)")
    proof.add_argument("--signed", action="store_true",
                       help="the answer is already signed (ameesh approve --action decision "
                            "--hash …): find and consume it")
    parser.add_argument("--as", dest="as_name", default=None,
                        help="name of the owner key in the registry (default: your name)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    decision_id = _decision_id(args.id)
    if decision_id is None:
        print(USAGE, file=sys.stderr)
        return 2
    if args.as_name and not args.key:
        print("--as only makes sense with --key", file=sys.stderr)
        return 2
    db = _open(cfg)
    try:
        who = answering_human(cfg, db)
        result = answer(cfg, db, decision_id, " ".join(args.answer), human=who.human,
                        channel=who.channel, key_path=args.key, approver=args.as_name,
                        signed=args.signed)
    finally:
        db.close()
    if args.json:
        _dump(result)
        return 0
    if result.get("followup"):
        print("late response recorded; decision #%d remains unchanged; "
              "new decision #%d (contradicts)" % (decision_id, result["followup"]))
        return 0
    if result.get("followup_pending"):
        print("late response recorded; decision #%d remains unchanged; "
              "contradictory follow-up pending" % decision_id)
        return 0
    if result.get("late_recorded"):
        print("late response recorded; decision #%d remains unchanged" % decision_id)
        return 0
    view, answer_ = result["decision"], result["decision"]["answer"]
    replayed = bool(result.get("replayed"))
    print("decision #%d: %s from %s (%s): %s\"%s\"" % (
        view["id"], "identical answer replayed (already recorded)" if replayed
        else "answer recorded", answer_["by"], CHANNEL_LABELS[answer_["channel"]],
        "option %s — " % answer_["option"] if answer_.get("option") else "", answer_["text"]))
    lot = result["lot"]
    if lot.get("moved"):
        print("work item #%s: returns to %s" % (lot["lot"], lot["state"]))
    elif lot.get("lot") is not None and lot.get("state"):
        print("work item #%s: stays in %s%s" % (lot["lot"], lot["state"],
                                           " (%d other decision(s) pending)"
                                           % lot["others"] if lot.get("others") else ""))
    if result.get("message_id"):
        if replayed and result.get("mail_sent"):
            print("answer mail sent now (message #%d): the missing reply is delivered"
                  % result["message_id"])
        elif replayed:
            print("answer mail already present (message #%d): nothing sent twice"
                  % result["message_id"])
        else:
            print("answer delivered to %s (message #%d): it wakes them up"
                  % (view["requester"], result["message_id"]))
    if result.get("warning"):
        print("warning: %s" % result["warning"], file=sys.stderr)
    return 0


def cmd_withdraw(cfg: Config, argv: list[str]) -> int:
    parser = _parser("ameesh decide withdraw")
    parser.add_argument("id")
    parser.add_argument("--why", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    decision_id = _decision_id(args.id)
    if decision_id is None:
        print(USAGE, file=sys.stderr)
        return 2
    db = _open(cfg)
    try:
        binding = identity.resolve_binding(cfg, db)
        if not binding.ok or os.environ.get(CHAT_ENV):
            raise DecisionError("only the agent that requested the decision can withdraw it (agent "
                                "identity required)")
        result = withdraw(cfg, db, decision_id, requester=binding.name, why=args.why)
    finally:
        db.close()
    if args.json:
        _dump(result)
        return 0
    lot = result["lot"]
    print("decision #%d withdrawn%s" % (
        result["decision"]["id"],
        "; work item #%s %s %s" % (lot["lot"], "returns to" if lot.get("moved") else "stays in",
                              lot["state"]) if lot.get("state") else ""))
    return 0


def cmd_repair_forward(cfg: Config, argv: list[str]) -> int:
    parser = _parser("ameesh decide repair-forward")
    parser.add_argument("id", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db = _open(cfg)
    try:
        binding = identity.resolve_binding(cfg, db)
        if not binding.ok:
            raise DecisionError("repair-forward requires an agent identity")
        try:
            result = storage.of(db).decisions.repair_forward(args.id, actor=binding.name)
        except ValueError as exc:
            raise DecisionError(str(exc)) from None
    finally:
        db.close()
    if args.json:
        _dump(result)
    elif result.get("copy_already_withdrawn"):
        print("decision #%d restored to pending; copy #%d was already withdrawn (kept)" %
              (result["decision"], result["withdrawn_copy"]))
    else:
        print("decision #%d restored to pending; copy #%d withdrawn" %
              (result["decision"], result["withdrawn_copy"]))
    return 0


def cmd_show(cfg: Config, argv: list[str]) -> int:
    parser = _parser("ameesh decide show")
    parser.add_argument("id")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    decision_id = _decision_id(args.id)
    if decision_id is None:
        print(USAGE, file=sys.stderr)
        return 2
    db = _open(cfg)
    try:
        view = get(db, decision_id)
        if view is None:
            raise DecisionError("decision #%s not found" % args.id)
        proof = verify_proof(db, view) if (view.get("answer") or {}).get("proof") else None
    finally:
        db.close()
    if proof is not None:
        view["proof_check"] = {"ok": proof[0], "detail": proof[1]}
    if args.json:
        _dump(view)
        return 0
    now = time.time()
    for line in format_view(view, now):
        print(line)
    if view["state"] == "answered":
        answer_ = view.get("answer") or {}
        print("      exact text: \"%s\"" % (answer_.get("text") or ""))
    if proof is not None:
        print("      proof: %s — %s" % ("VALID" if proof[0] else "INVALID", proof[1]))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    cfg = config_mod.load()
    try:
        if command == "validation-history":
            return validations.main(cfg, rest)
        if command == "ask":
            return cmd_ask(cfg, rest)
        if command == "withdraw":
            return cmd_withdraw(cfg, rest)
        if command == "repair-forward":
            return cmd_repair_forward(cfg, rest)
        if command == "show":
            return cmd_show(cfg, rest)
        if _decision_id(command) is not None:
            return cmd_answer(cfg, argv)
        print(USAGE, file=sys.stderr)
        return 2
    except DecisionError as exc:
        print(str(exc) if str(exc).startswith("refus") else "refused: %s" % exc,
              file=sys.stderr)
        return 1
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
