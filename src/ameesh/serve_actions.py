# SPDX-License-Identifier: AGPL-3.0-only
"""The API's human actions (L87): `POST /api/v1/...`, served by `ameesh serve`.

Embeddable interface study (docs/design/etudes/ui-integrable.md, §2.1). Each
route calls the SAME library function as the CLI (one source of truth) and
acts under the identity the API authenticated, passed explicitly: never the
serve process's environment (`AGENT_MAIL_NAME`, `$USER`, session binding).

Every response is `ameesh-action/1`: the action, `by` (the human who acted),
`for` (the human on whose behalf: the same in local mode, a deputy's
principal in team mode, 0033 §7, L86) and the result.

What commits the organisation never goes through here (study §2.1): no route
approves, produces or attaches a receipt (a test checks it), a cap is only
ever **lowered**, and an assignment is never forced to an external agent.
`POST /actions/{action}/approval-request` files the request with
ameesh-approve and returns the **link**: the human signs on that origin with
their passkey, and the receipt comes back through `ameesh action
fetch-receipt`, never through this API.

Operator pause/resume (L87b, #1462) retains prompts and lets the active turn
finish. Alert acknowledgements (L87c, #1463) persist for the human recipient.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Callable

from . import fil

#: a request body, at most (JSON)
MAX_BODY = 16 * 1024
#: a message or a thread entry, at most (characters)
MAX_TEXT = 8000
_ACTION_ID_RE = re.compile(r"^act_[0-9A-Za-z]{26}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WINDOWS = {"hour": 3600, "day": 86400}


class ActionError(Exception):
    """A refused action: HTTP status, error code, message (`ApiError`'s
    shape; `serve` turns it into `ameesh-error/1`)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclasses.dataclass(frozen=True)
class Action:
    path: str                       # OpenAPI template, /api/v1/agents/{agent}/stop
    name: str                       # the action, in `ameesh-action/1`
    summary: str
    build: Callable                 # (ctx, db, params, body) -> dict (the result)
    body: dict                      # field -> (OpenAPI schema, required)

    @property
    def regex(self) -> re.Pattern:
        pattern = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(self.path))
        return re.compile("^" + pattern + "$")


# --------------------------------------------------------------------------
# who acts
# --------------------------------------------------------------------------

def principal(ctx) -> str:
    """The human on whose behalf the action is done (0033 §7). Local mode has
    no deputyship: the human of the API acts for themself."""
    return getattr(ctx, "on_behalf_of", None) or ctx.human


def actor(ctx) -> str:
    """The trace of who acted: `human:X`, or `human:X for human:Y` when a
    deputy acts for their principal (team mode, L86)."""
    on_behalf = principal(ctx)
    return ctx.human if on_behalf == ctx.human else "%s for %s" % (ctx.human, on_behalf)


def mail_name(ctx) -> str:
    """The human as a mailbox sender: the bare id, as `agent-mail send`
    writes it (the thread renders a declared human as `human:<id>`)."""
    return ctx.human.split(":", 1)[1]


# --------------------------------------------------------------------------
# body fields
# --------------------------------------------------------------------------

def _text(body: dict, name: str, *, limit: int = MAX_TEXT) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ActionError(400, "bad_body", "%s: a non-empty text is required" % name)
    if len(value) > limit:
        raise ActionError(400, "bad_body", "%s: %d characters at most" % (name, limit))
    try:
        fil.ensure_readable(value)
    except fil.UnreadableBody as exc:
        raise ActionError(400, "unreadable", str(exc)) from exc
    return value


def _name(body: dict, name: str, *, required: bool = True) -> str | None:
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ActionError(400, "bad_body", "%s: a name is required" % name)
    return value


def _bool(body: dict, name: str) -> bool:
    value = body.get(name, False)
    if not isinstance(value, bool):
        raise ActionError(400, "bad_body", "%s: true or false" % name)
    return value


def _lot_label(db, value) -> int | None:
    """An open lot named by a message, as its thread label; never assigned."""
    from . import assignments
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ActionError(400, "bad_body", "lot: a lot number is required")
    try:
        item = assignments.resolve_lot(db, str(value))
    except assignments.AssignmentError:
        item = None
    if item is None:
        raise ActionError(404, "not_found", "unknown or closed lot")
    return int(item["id"])


# --------------------------------------------------------------------------
# the actions
# --------------------------------------------------------------------------

def _message(ctx, db, params, body) -> dict:
    """Message to an agent: its mailbox and its thread, as `agent-mail send`
    from a human (wakes the agent; a human's message is never an
    acknowledgement). `lot` only labels the thread: a message never assigns."""
    from . import mail, registry, undeliverable
    text = _text(body, "text")
    lot = _lot_label(db, body.get("lot"))
    urgent = _bool(body, "urgent")
    sender = mail_name(ctx)
    if registry.get(db, params["agent"]) is None:
        raise ActionError(404, "not_found", "unknown agent")
    try:
        # a stopped recipient is refused, nothing deposited (as the CLI without --queue)
        undeliverable.check_send(ctx.cfg, db, sender=sender, dest=params["agent"],
                                 explicit=True)
    except undeliverable.Refused as exc:
        raise ActionError(409, "refused", str(exc)) from exc
    payload = {"human": True}
    if urgent:
        payload["urgent"] = True
    message_id = mail.send(db, sender, params["agent"], text, host=ctx.cfg.host,
                           kind="notify", payload=payload,
                           work_item_id=None if lot is None else str(lot))
    return {"agent": params["agent"], "message_id": message_id, "lot": lot}


def _chat(ctx, db, params, body) -> dict:
    """A message to an agent's shared conversation (L83): the server writes the
    `human_message` entry and delivers the mail that wakes the agent. The
    author is the human the API authenticated, never a body field; the
    relation check of 0033 §7/§9 arrives with the OIDC tokens (L86)."""
    from . import chat, registry, undeliverable
    text = _text(body, "text")
    lot = _lot_label(db, body.get("lot"))
    sender = mail_name(ctx)
    if registry.get(db, params["agent"]) is None:
        raise ActionError(404, "not_found", "unknown agent")
    try:
        undeliverable.check_send(ctx.cfg, db, sender=sender, dest=params["agent"],
                                 explicit=True)
    except undeliverable.Refused as exc:
        raise ActionError(409, "refused", str(exc)) from exc
    return chat.post_human_message(ctx.cfg, db, human=ctx.human, sender=sender,
                                   agent=params["agent"], text=text, lot=lot)


def _publish(ctx, db, params, body) -> dict:
    """An entry in a project's thread, or in a lot's (the lot must belong to
    the project)."""
    text = _text(body, "text")
    project, lot = params["project"], params.get("lot")
    transport = fil.transport_for(ctx.cfg)
    if lot is None:
        if not transport.exists(fil.ThreadRef(project, None)):
            raise ActionError(404, "not_found", "no such project thread")
    else:
        from . import work
        if not re.match(r"^[0-9]{1,9}$", lot):
            raise ActionError(400, "bad_path", "lot: a lot number is required")
        item = work.get(db, int(lot))
        if item is None:
            raise ActionError(404, "not_found", "unknown lot")
        if fil.lot_project(db, int(lot)) != project:
            raise ActionError(404, "not_found", "lot %s is not in project %s" % (lot, project))
    entry = fil.record(ctx.cfg, db, sender=ctx.human, recipients=(), text=text,
                       project=project, lot=lot, meta={"by": actor(ctx)}
                       if actor(ctx) != ctx.human else None)
    if entry is None:
        raise ActionError(503, "thread_unavailable", "the thread could not be written")
    return {"project": project, "lot": None if lot is None else int(lot), "entry": entry}


def _interrupt(ctx, db, params, body) -> dict:
    from . import exploitation
    text = _text(body, "text")
    try:
        message_id = exploitation.interrupt(ctx.cfg, db, params["agent"], text,
                                            sender=mail_name(ctx))
    except exploitation.InterruptRefused as exc:
        raise ActionError(403 if exc.code == "not_allowed" else 404,
                          "forbidden" if exc.code == "not_allowed" else "not_found",
                          str(exc)) from exc
    return {"agent": params["agent"], "message_id": message_id}


def _stop(ctx, db, params, body) -> dict:
    from . import exploitation
    reason = _text(body, "reason", limit=exploitation.STOP_REASON_MAX)
    try:
        return exploitation.stop(ctx.cfg, db, params["agent"], reason, actor=actor(ctx),
                                 sender=ctx.human)
    except exploitation.StopRefused as exc:
        raise ActionError(404 if exc.code == "unknown_agent" else 400,
                          "not_found" if exc.code == "unknown_agent" else "bad_body",
                          str(exc)) from exc


def _operator_pause(ctx, db, params, body, *, paused: bool) -> dict:
    from . import agent_pause
    reason = _text(body, "reason", limit=agent_pause.REASON_MAX)
    try:
        return agent_pause.change(db, params["agent"], paused=paused, reason=reason,
                                  actor=ctx.human, principal=principal(ctx))
    except agent_pause.PauseRefused as exc:
        if exc.code == "unknown_agent":
            raise ActionError(404, "not_found", str(exc)) from exc
        if exc.code == "external_agent":
            raise ActionError(409, "refused", str(exc)) from exc
        raise ActionError(400, "bad_body", str(exc)) from exc


def _pause(ctx, db, params, body) -> dict:
    return _operator_pause(ctx, db, params, body, paused=True)


def _resume(ctx, db, params, body) -> dict:
    return _operator_pause(ctx, db, params, body, paused=False)


def _assign(ctx, db, params, body) -> dict:
    """Reassign a lot (rules of 0030 §2, `work.check_assignee`). Never to an
    external agent: forcing that commits the organisation (study §2.1)."""
    from . import work
    assignee = body.get("assignee")
    if not isinstance(assignee, str) or not (
            _NAME_RE.match(assignee) or re.match(r"^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$",
                                                 assignee)):
        raise ActionError(400, "bad_body", "assignee: an agent or human:<id> is required")
    try:
        done = work.assign(db, int(params["id"]), assignee, externe=False, actor=actor(ctx),
                           cfg=ctx.cfg)
    except work.WorkError as exc:
        status = 404 if "not found" in str(exc).lower() else 409
        raise ActionError(status, "not_found" if status == 404 else "refused",
                          str(exc)) from exc
    item = done.get("item") or {}
    return {"lot": int(params["id"]), "assignee": item.get("assignee", assignee),
            "previous": done.get("previous"), "check": done.get("check")}


def _drain(ctx, db, params, body) -> dict:
    from . import host_lifecycle
    duration = body.get("duration_s")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int)):
        raise ActionError(400, "bad_body", "duration_s: whole seconds")
    try:
        return host_lifecycle.drain(ctx.cfg, db, params["host"], duration=duration,
                                    actor=actor(ctx))
    except ValueError as exc:
        raise ActionError(400, "bad_body", str(exc)) from exc


def _lower_cap(ctx, db, params, body) -> dict:
    """Lower a cap of the mesh or of an agent; raising one is refused (it
    commits the organisation: a PR to the canon or the CLI's holder)."""
    from . import budget
    name = body.get("window")
    window = _WINDOWS.get(name) if isinstance(name, str) else None
    if window is None:
        raise ActionError(400, "bad_body", "window: hour or day")
    usd = body.get("usd")
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not usd > 0 \
            or usd != usd or usd == float("inf"):
        raise ActionError(400, "bad_body", "usd: a positive amount is required")
    agent = _name(body, "agent", required=False)
    try:
        changed, old = budget.change(ctx.cfg, db, agent=agent, window=window, usd=float(usd),
                                     actor=actor(ctx), lower_only=True,
                                     sender=ctx.human)
    except budget.BudgetError as exc:
        raise ActionError(409, "not_a_lowering", str(exc)) from exc
    return {"scope": agent or "mesh", "window": body["window"], "usd": float(usd),
            "previous": old, "changed": changed}


def _approval_request(ctx, db, params, body) -> dict:
    """File the approval request of an action with ameesh-approve, for the
    human of the API as approver; return the link to open in the foreground.
    Nothing is approved here."""
    from . import actions, actions_cli, approve_client
    if not _ACTION_ID_RE.match(params["action"]):
        raise ActionError(400, "bad_path", "action refused")
    action = actions.get(db, params["action"])
    if action is None:
        raise ActionError(404, "not_found", "unknown action")
    try:
        reply = actions_cli.request_approval(ctx.cfg, db, action, approver=principal(ctx),
                                             requested_by=ctx.human)
    except actions.ActionError as exc:
        raise ActionError(409, "invalid_action", str(exc)) from exc
    except approve_client.ApproveClientError as exc:
        if exc.code == "config":
            raise ActionError(503, "approve_not_configured", str(exc)) from exc
        if exc.code in ("unreachable", "redirect", "format", "mismatch"):
            raise ActionError(502, "approve_" + exc.code, str(exc)) from exc
        raise ActionError(409, "approve_refused", str(exc)) from exc
    return {key: reply.get(key) for key in ("request_id", "link", "link_exp", "exp",
                                             "action_id", "approver", "digest")}


def _ack_alert(ctx, db, params, body) -> dict:
    from . import alert_ack
    try:
        return alert_ack.acknowledge(ctx.cfg, db, params["key"], actor=ctx.human,
                                     principal=principal(ctx))
    except alert_ack.AckRefused as exc:
        status = {"not_found": 404, "forbidden": 403, "native_only": 403}.get(exc.code, 400)
        raise ActionError(status, exc.code, str(exc)) from exc


_TEXT = ({"type": "string", "minLength": 1, "maxLength": MAX_TEXT}, True)
ACTIONS = (
    Action("/alerts/{key}/ack", "ack_alert",
           "Acknowledge an alert as its human recipient; the alert remains visible",
           _ack_alert, {}),
    Action("/agents/{agent}/messages", "message",
           "Message to an agent: its mailbox and its thread (`agent-mail send`)", _message,
           {"text": _TEXT, "lot": ({"type": ["integer", "null"], "minimum": 1}, False),
            "urgent": ({"type": "boolean"}, False)}),
    Action("/agents/{agent}/chat", "chat",
           "A message to an agent's shared conversation (L83: the server writes the entry "
           "and wakes the agent by mail)", _chat,
           {"text": _TEXT, "lot": ({"type": ["integer", "null"], "minimum": 1}, False)}),
    Action("/threads/{project}", "publish", "Publish in a project's thread", _publish,
           {"text": _TEXT}),
    Action("/threads/{project}/{lot}", "publish", "Publish in a lot's thread", _publish,
           {"text": _TEXT}),
    Action("/agents/{agent}/interrupt", "interrupt",
           "Interrupt an agent's turn (`ameesh interrupt`; the human must be in "
           "AMEESH_INTERRUPT_SENDERS)", _interrupt, {"text": _TEXT}),
    Action("/agents/{agent}/stop", "stop",
           "Stop an agent by hand, with a reason (0030 §4: `manuel`)", _stop,
           {"reason": ({"type": "string", "minLength": 1, "maxLength": 500}, True)}),
    Action("/agents/{agent}/pause", "pause",
           "Operator pause: finish this turn, start no new one", _pause,
           {"reason": ({"type": "string", "minLength": 1, "maxLength": 2000}, True)}),
    Action("/agents/{agent}/resume", "resume",
           "Lift only the operator pause; do not restart a stopped agent", _resume,
           {"reason": ({"type": "string", "minLength": 1, "maxLength": 2000}, True)}),
    Action("/lots/{id}/assign", "assign",
           "Reassign a lot (`ameesh work assign`, 0030 §2; never to an external agent)",
           _assign, {"assignee": ({"type": "string"}, True)}),
    Action("/hosts/{host}/drain", "drain",
           "Stop placing sessions on a host (`ameesh host drain`)", _drain,
           {"duration_s": ({"type": ["integer", "null"], "minimum": 30,
                            "maximum": 7 * 86400}, False)}),
    Action("/budget/caps", "lower_cap",
           "Lower a cap of the mesh or of an agent (never raise one)", _lower_cap,
           {"window": ({"enum": list(_WINDOWS)}, True),
            "usd": ({"type": "number", "exclusiveMinimum": 0}, True),
            "agent": ({"type": ["string", "null"]}, False)}),
    Action("/actions/{action}/approval-request", "approval_request",
           "File the approval request of an action with ameesh-approve; returns the link "
           "(approval stays a passkey on ameesh-approve's origin)", _approval_request, {}),
)

__all__ = ["ACTIONS", "Action", "ActionError", "MAX_BODY", "actor", "principal"]
