# SPDX-License-Identifier: AGPL-3.0-only
"""#7866 — `ameesh responder`: route an incident to an available human.

The canon declares **who may answer** (an `Application` card's ordered
`incident.responders`, with `Member.timezone` and `Member.incident_eligible`);
this module decides **who is available now** and journals only the choice, the
reason and the *kinds* of signals used.

    ameesh responder <application> --severity P1 [--json] [--now ISO]

Signals, strongest first, each with its freshness window (see
`incident_routing`): an attached human (a live local `attach:<account>` lease,
or a distant `attach:<tenant>:<device>` lease whose device credential is
theirs), last interaction (ameesh's journal, then Memoria's public interface),
Slack presence (`active` / `away`), then the timezone and usual hours
fallback.  An absent or stale signal passes to the next one; a present `away`
presence and the local-hours fallback can rule a human out.

Only the human's **own activity** counts. A journal row proves it with the
`user@host` proof (`payload.human_account`) or, failing that, a host declared
in `Member.local_accounts`; a decision answer typed by the bureau under the
§10 delegation carries its mention and is never counted. Mail delivery, a
decision's reply mail and an actor-only `action_events` row are not device
proofs and are never used: the decision close writes the delivery timestamp
itself when anyone answers. Fewer signals, never a false one.

The phone wake is **off by default** and is a new expense: it requires
`AMEESH_INCIDENT_CALL=1`, a configured voice endpoint, and a signed owner
decision whose id is given by `AMEESH_INCIDENT_CALL_GO`.  The resolver only
asks for a call with a short reason; the phone number stays in the voice
service's secrets, never in the canon, the journal or this process.  A call is
requested immediately for `security`, `data-loss` and `outage`, and for a P1
when nobody is available; no other severity wakes anyone.  `--now` is a
simulation: it never calls and writes no real event.

Public contract: `docs/api/incident-responder-v1.md` (`ameesh-responder/1`).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import attach_session
from . import canon as canon_mod
from . import db as db_mod
from . import decisions as decisions_mod
from . import events
from . import incident_routing as routing

SCHEMA = "ameesh-responder/1"
VOICE_SCHEMA = "ameesh-voice-call/1"
MEMORIA_SCHEMA = "memoria-last-interaction/1"
#: journal event type, one per routed incident (never per candidate)
EVENT = "incident.routed"
#: the wake reasons this resolver can produce, as codes (never free text that
#: could carry a phone number)
WAKE_STATUSES = ("not-needed", "requested", "disabled", "refused", "unconfigured",
                 "failed", "simulated")
#: the only reason text sent to the voice service
REASON_MAX = 200


@dataclass
class Resolution:
    """The resolver's verdict, with only what may be journaled."""

    application: Any
    severity: str
    responder: Any | None = None
    entry: routing.Responder | None = None
    reason: str = ""
    signals: tuple[str, ...] = ()
    consulted: tuple[str, ...] = ()


def candidates(canon: canon_mod.Canon, policy: routing.IncidentPolicy,
               ) -> list[tuple[Any, routing.Responder]]:
    """The humans declared by the chain, in canon order, deduplicated.

    A role expands to the Members holding it, sorted by title for a
    deterministic choice; a direct member is itself.  A human who is not
    `incident_eligible` is never a candidate (the filter lives in `resolve`,
    so the reason can be named without inventing a responder)."""
    out: list[tuple[Any, routing.Responder]] = []
    seen: set[str] = set()
    for entry in policy.responders:
        if entry.member is not None:
            human = canon.resolve_human(entry.member)
            members = [m for m in canon.members if human and m.title == human[6:]]
        else:
            wanted = (entry.role or "").strip().lower()
            members = sorted((m for m in canon.members
                              if wanted in {str(r).strip().lower()
                                            for r in (m.roles or [])}),
                             key=lambda m: m.title)
        for member in members:
            if member.title in seen:
                continue
            seen.add(member.title)
            out.append((member, entry))
    return out


def resolve(canon: canon_mod.Canon, application: Any, severity: str,
            signals: routing.Signals, now: _dt.datetime) -> Resolution:
    """First declared responder available now; None when nobody is."""
    policy = application.incident
    result = Resolution(application=application, severity=severity)
    if policy is None:
        result.reason = "no-incident-block"
        return result
    if not policy.covers(severity):
        result.reason = "severity-not-covered"
        return result
    consulted: set[str] = set()
    for member, entry in candidates(canon, policy):
        if not getattr(member, "incident_eligible", False):
            # An explicit opt-in is required; a missing key is not a silent yes.
            continue
        verdict, used = routing.availability(member, signals, now)
        consulted |= used
        if verdict.available:
            result.responder = member
            result.entry = entry
            result.reason = verdict.reason
            result.signals = verdict.signals
            result.consulted = tuple(sorted(consulted))
            return result
    result.reason = "no-responder"
    result.consulted = tuple(sorted(consulted))
    return result


# --------------------------------------------------------------------------
# live signals
# --------------------------------------------------------------------------

def _secret(env: Mapping[str, str], name: str) -> str:
    """A service secret from the environment, or from its `_FILE` variant."""
    value = str(env.get(name) or "").strip()
    if value:
        return value
    path = str(env.get(name + "_FILE") or "").strip()
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _moment(value: Any) -> _dt.datetime | None:
    """An ISO-8601 instant, as an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _fresh(moment: _dt.datetime | None, now: _dt.datetime, window_s: int) -> bool:
    return moment is not None and 0 <= (now - moment).total_seconds() <= window_s


def delegated_answer(text: Any) -> bool:
    """Is a signed decision answer the bureau's/relay's, not the owner's own?

    `signer-owner.sh` (§10) refuses to sign without the exact mention it
    writes — "<option> — saisi par le bureau, délégation §10" or "… réponse de
    l'owner relayée par le bureau, source <session>" — and `match_option`
    already reads those two forms. A delegated answer is never the human's own
    activity, whatever host recorded it (#7866, B1 de l'auditeur)."""
    value = str(text or "")
    return bool(decisions_mod.AVATAR_SAISI_RE.search(value)
                or decisions_mod.AVATAR_RELAY_RE.search(value))


def _http(method: str, url: str, headers: Mapping[str, str] | None = None,
          data: bytes | None = None, timeout: float = 2.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}),
                                     method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(getattr(response, "status", 200)), response.read(65536)


@dataclass
class LiveSignals:
    """The probes of a host: database journal, Memoria and Slack.

    Every probe returns None on absence, staleness or any provider failure:
    an unavailable signal degrades cleanly and never invents a presence.
    """

    db: Any = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    transport: Callable[..., tuple[int, bytes]] = _http
    clock: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(_dt.timezone.utc)
    _slack_users: dict[str, str | None] = field(default_factory=dict, repr=False)
    #: `member -> (instant of the read, verdict)`; a presence read is kept for
    #: `PRESENCE_FRESH_S`, the freshness window of that signal.  In memory only.
    _presence_cache: dict[str, tuple[float, bool | None]] = field(
        default_factory=dict, repr=False)

    # -- attach --------------------------------------------------------------
    def attach(self, member: Any) -> bool | None:
        if self.db is None:
            return None
        accounts = self._accounts(member)
        try:
            rows = self.db.query(
                "select lease_owner from agent_registry"
                " where lease_owner is not null"
                "   and lease_expires_at > clock_timestamp()"
                "   and lease_owner like 'attach:%'")
        except Exception:
            return None
        owners = {str(row.get("lease_owner") or "") for row in rows}
        if any("attach:%s" % account in owners for account in accounts):
            return True
        # #7869 : un attach distant porte `attach:<tenant>:<appareil>`, où
        # l'appareil est l'identifiant borné d'un authentificateur
        # `device-es256` ; le tenant seul ne dit pas l'humain. On relit donc
        # les appareils actifs du membre et on reconstruit le bail qu'ils
        # auraient écrit (`remote_owner`, la forme même de l'hôte). Un
        # appareil d'un autre humain, un appareil révoqué ou un bail d'une
        # autre forme ne rend personne disponible : le signal manque, il ne
        # ment pas.
        remote: dict[str, str] = {}
        for owner in owners:
            if not owner.startswith("attach:"):
                continue
            tenant, sep, device = owner[len("attach:"):].partition(":")
            if sep and device:
                remote[owner] = tenant
        if not remote:
            return None
        try:
            rows = self.db.query(
                "select credential_id from authenticators"
                " where approver = %s and facade = 'device-es256'"
                "   and revoked_at is null",
                ("human:%s" % getattr(member, "title", ""),))
        except Exception:
            return None
        for row in rows:
            credential = str(row.get("credential_id") or "")
            if not credential:
                continue
            for owner, tenant in remote.items():
                try:
                    expected = attach_session.remote_owner(
                        "" if tenant == "-" else tenant, credential)
                except ValueError:
                    continue
                if expected == owner:
                    return True
        return None

    # -- last interaction ----------------------------------------------------
    @staticmethod
    def _accounts(member: Any) -> list[str]:
        """The member's declared `user@host` accounts (the device proof)."""
        return [str(a).strip()
                for a in (getattr(member, "local_accounts", None) or [])
                if str(a).strip()]

    @staticmethod
    def _own_device(row: Mapping[str, Any], accounts: list[str]) -> bool:
        """Did this journal row happen on one of the member's own devices?

        The exact `user@host` proof (`payload.human_account`, written by the
        CLI for an unbound human entry) wins over the coarser host column.
        A bureau or an avatar answering/acting under `human:<id>` is bound to
        its agent: it carries no proof and runs on its own host, so it is not
        the human's own activity (#7866, B1 de l'auditeur). A row with neither
        proof nor a declared host proves nothing and is ignored — a missing
        signal passes to the next one, it is not an absence.
        """
        proof = str(row.get("proof") or "").strip()
        if proof:
            return proof in accounts
        host = str(row.get("host") or "").strip()
        if not host:
            return False
        return any(account.rsplit("@", 1)[1] == host
                   for account in accounts if "@" in account)

    def journal_last(self, member: Any) -> _dt.datetime | None:
        if self.db is None:
            return None
        human = "human:%s" % getattr(member, "title", "")
        accounts = self._accounts(member)
        try:
            rows = self.db.query(
                "select 'sent' as source,"
                "       extract(epoch from m.created_at) as ts,"
                "       m.host as host,"
                "       m.payload->>'human_account' as proof,"
                "       m.payload ? 'decision_answer' as decision_reply,"
                "       null::text as answer_text"
                "  from agent_mailbox m"
                " where m.sender = %s and m.created_at > now() - interval '1 day'"
                " union all"
                " select 'answer',"
                "        (m.payload->'decision'->'answer'->>'ts')::float8,"
                "        m.payload->'decision'->'answer'->>'host',"
                "        m.payload->'decision'->'answer'->>'human_account',"
                "        false,"
                "        m.payload->'decision'->'answer'->>'text'"
                "   from agent_mailbox m"
                "  where m.kind = 'request'"
                "    and m.payload->'decision'->'answer'->>'by' = %s"
                "    and m.created_at > now() - interval '1 day'",
                (human, human))
        except Exception:
            return None
        # Two sources are deliberately absent.
        #
        # `action_events` names an actor but no device: a relay acting under
        # `human:<id>` cannot be told apart.
        #
        # A mail *delivery* (`delivered_at`) is not a device proof either: the
        # decision close writes it on the request row when anyone answers —
        # the bureau included — so it would resurrect the delegated answer
        # excluded below (B de cx2, B1' de l'auditeur, #7866). A real delivery
        # carries no `user@host` proof of the reader.
        #
        # A decision's reply mail (`payload.decision_answer`) is the answer's
        # byproduct: the answer row already carries the proof and the
        # delegation mention, so the reply is never counted on its own.
        newest: _dt.datetime | None = None
        for row in rows:
            source = str(row.get("source") or "")
            if source not in ("sent", "answer"):
                continue
            if source == "answer" and delegated_answer(row.get("answer_text")):
                # A bureau/relay answer is never the human's own activity,
                # whatever host recorded it. The signed text carries the
                # mention that `match_option` already relies on.
                continue
            if source == "sent" and row.get("decision_reply"):
                continue
            if not self._own_device(row, accounts):
                continue
            try:
                moment = _dt.datetime.fromtimestamp(float(row.get("ts")),
                                                    _dt.timezone.utc)
            except (TypeError, ValueError, OSError):
                continue
            if newest is None or moment > newest:
                newest = moment
        return newest

    def memoria_last(self, member: Any) -> _dt.datetime | None:
        base = str(self.env.get("AMEESH_INCIDENT_MEMORIA_URL") or "").strip()
        if not base:
            return None
        human = "human:%s" % getattr(member, "title", "")
        url = base.rstrip("/") + "/v1/interactions/last?" + urllib.parse.urlencode(
            {"human": human})
        headers = {"Accept": "application/json"}
        token = _secret(self.env, "AMEESH_INCIDENT_MEMORIA_TOKEN")
        if token:
            headers["Authorization"] = "Bearer %s" % token
        try:
            status, body = self.transport("GET", url, headers, None, 2.0)
            if status != 200:
                return None
            document = json.loads(body.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(document, dict) or document.get("schema") not in (
                MEMORIA_SCHEMA, None):
            return None
        if document.get("human") not in (None, human):
            return None
        return _moment(document.get("last_interaction"))

    def interaction(self, member: Any, now: _dt.datetime) -> bool | None:
        journal = self.journal_last(member)
        if _fresh(journal, now, routing.INTERACTION_FRESH_S):
            return True
        memoria = self.memoria_last(member)
        if _fresh(memoria, now, routing.INTERACTION_FRESH_S):
            return True
        return None

    # -- Slack presence ------------------------------------------------------
    def _slack_token(self) -> str:
        return _secret(self.env, "AMEESH_INCIDENT_SLACK_TOKEN")

    def _slack_user(self, member: Any) -> str | None:
        title = str(getattr(member, "title", ""))
        if title in self._slack_users:
            return self._slack_users[title]
        user: str | None = None
        raw = str(self.env.get("AMEESH_INCIDENT_SLACK_USERS") or "").strip()
        if raw:
            try:
                mapping = json.loads(raw)
            except ValueError:
                mapping = {}
            if isinstance(mapping, dict) and isinstance(mapping.get(title), str):
                user = mapping[title].strip() or None
        if user is None:
            emails: list[str] = []
            fiche = getattr(member, "fiche", None)
            data = getattr(fiche, "data", None) or {}
            for key in ("emails", "email"):
                value = data.get(key)
                if isinstance(value, str):
                    emails.append(value.strip())
                elif isinstance(value, list):
                    emails.extend(str(v).strip() for v in value if str(v).strip())
            token = self._slack_token()
            if token and emails:
                url = "https://slack.com/api/users.lookupByEmail?" + urllib.parse.urlencode(
                    {"email": emails[0]})
                try:
                    status, body = self.transport(
                        "GET", url, {"Authorization": "Bearer %s" % token,
                                     "Accept": "application/json"}, None, 2.0)
                    document = json.loads(body.decode("utf-8")) if status == 200 else {}
                except Exception:
                    document = {}
                if isinstance(document, dict) and document.get("ok") is True:
                    found = (document.get("user") or {}).get("id")
                    if isinstance(found, str) and found:
                        user = found
        self._slack_users[title] = user
        return user

    def presence(self, member: Any, now: _dt.datetime) -> bool | None:
        title = str(getattr(member, "title", ""))
        cached = self._presence_cache.get(title)
        if cached is not None:
            age = now.timestamp() - cached[0]
            if 0 <= age <= routing.PRESENCE_FRESH_S:
                return cached[1]
        verdict = self._presence_read(member)
        self._presence_cache[title] = (now.timestamp(), verdict)
        return verdict

    def _presence_read(self, member: Any) -> bool | None:
        token = self._slack_token()
        if not token:
            return None
        user = self._slack_user(member)
        if not user:
            return None
        url = "https://slack.com/api/users.getPresence?" + urllib.parse.urlencode(
            {"user": user})
        try:
            status, body = self.transport(
                "GET", url, {"Authorization": "Bearer %s" % token,
                             "Accept": "application/json"}, None, 2.0)
            if status != 200:
                return None
            document = json.loads(body.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(document, dict) or document.get("ok") is not True:
            return None
        presence = document.get("presence")
        if presence == "active":
            return True
        if presence == "away":
            return False
        return None


# --------------------------------------------------------------------------
# wake: the phone call, off by default and behind a signed owner go
# --------------------------------------------------------------------------

@dataclass
class Wake:
    plan: str = "none"
    status: str = "not-needed"
    reason: str = ""
    request_id: str | None = None

    def to_dict(self) -> dict:
        out = {"plan": self.plan, "status": self.status}
        if self.reason:
            out["reason"] = self.reason
        if self.request_id:
            out["request_id"] = self.request_id
        return out


def _wake_owner(application: Any) -> str | None:
    policy = application.incident
    if policy is not None and policy.escalation:
        return policy.escalation
    return getattr(application, "responsible", None)


def verify_owner_go(db: Any, decision_id: Any, owner: str,
                    application: Any = None, *, now: float | None = None,
                    ) -> tuple[bool, str]:
    """A signed answer by `owner` to the named decision — provided the signed
    question carries exactly one `incident-call/1` declaration for *this*
    application, the request offers the reserved `incident-call` option with
    its exact positive label, and the signed answer selects it inside its
    window.

    The operator names the decision in `AMEESH_INCIDENT_CALL_GO`.  A pending,
    unsigned, misaddressed, superseded, unverifiable, unrelated, refusing,
    out-of-scope, mislabelled or expired decision is a refusal, never a call.
    """
    try:
        ident = int(str(decision_id).strip().lstrip("#"))
    except (TypeError, ValueError):
        return False, "owner-go-invalid"
    try:
        view = decisions_mod.get(db, ident)
    except Exception:
        return False, "owner-go-unreadable"
    if view is None:
        return False, "owner-go-not-found"
    if view.get("state") != "answered":
        return False, "owner-go-pending"
    answer = view.get("answer") or {}
    if answer.get("by") != owner:
        return False, "owner-go-not-owner"
    if answer.get("channel") != "signed":
        return False, "owner-go-unsigned"
    try:
        ok, _why = decisions_mod.verify_proof(db, view)
    except Exception:
        return False, "owner-go-unreadable"
    if not ok:
        return False, "owner-go-unverified"
    try:
        decisions_mod.require_current(db, view)
    except Exception:
        return False, "owner-go-superseded"
    # #7866 (B cx2) : the signed data itself must scope the go.  A "no", an
    # unrelated question, another application or an expired window never
    # activates the call.
    try:
        rule = routing.parse_call_go(view.get("question"))
    except ValueError:
        return False, "owner-go-unscoped"
    if rule is None:
        return False, "owner-go-unscoped"
    app = str(application or "").strip()
    if not app or rule.application != app:
        return False, "owner-go-other-application"
    instant = time.time() if now is None else float(now)
    if instant >= rule.until:
        return False, "owner-go-expired"
    if rule.until > instant + routing.CALL_GO_MAX_HOURS * 3600:
        return False, "owner-go-window"
    # #7866 (B cx2, second round): the go is not any declared option key.
    # Only the reserved key may carry it, and only with its exact positive
    # label in the signed request.  A "Non" option can never authorize.
    option = next((candidate for candidate in view.get("options") or ()
                   if str(candidate.get("key") or "") == routing.CALL_GO_OPTION),
                  None)
    if option is None:
        return False, "owner-go-option-missing"
    label = " ".join(str(option.get("label") or "").split())
    if label != routing.CALL_GO_LABEL:
        return False, "owner-go-option-not-positive"
    if str(answer.get("option") or "") != routing.CALL_GO_OPTION:
        return False, "owner-go-not-approved"
    return True, ""


def _short_reason(text: str) -> str:
    reason = " ".join(str(text or "").split())
    return reason[:REASON_MAX]


def request_call(application: Any, severity: str, resolution: Resolution,
                 *, db: Any, env: Mapping[str, str],
                 transport: Callable[..., tuple[int, bytes]] = _http,
                 go_check: Callable[[Any, Any, str, str], tuple[bool, str]] = verify_owner_go,
                 ) -> Wake:
    """Ask the voice service for one call, only when the wake rule applies."""
    plan = routing.wake_plan(severity, resolution.responder)
    wake = Wake(plan=plan)
    if plan == "none":
        return wake
    owner = _wake_owner(application)
    if not owner:
        wake.status, wake.reason = "refused", "no-wake-owner"
        return wake
    if str(env.get("AMEESH_INCIDENT_CALL") or "").strip().lower() not in (
            "1", "true", "yes", "on"):
        wake.status, wake.reason = "disabled", "flag-off"
        return wake
    go_id = str(env.get("AMEESH_INCIDENT_CALL_GO") or "").strip()
    if not go_id:
        wake.status, wake.reason = "refused", "owner-go-required"
        return wake
    app_name = getattr(application, "id", "") or getattr(application, "title", "")
    try:
        allowed, why = go_check(db, go_id, owner, app_name)
    except Exception:
        allowed, why = False, "owner-go-unreadable"
    if not allowed:
        wake.status, wake.reason = "refused", why or "owner-go-refused"
        return wake
    url = str(env.get("AMEESH_INCIDENT_VOICE_URL") or "").strip()
    token = _secret(env, "AMEESH_INCIDENT_VOICE_TOKEN")
    if not url or not token:
        wake.status, wake.reason = "unconfigured", "voice-endpoint-missing"
        return wake
    app_name = getattr(application, "id", "") or getattr(application, "title", "")
    reason = _short_reason(
        "Incident %s sur %s. %s" % (
            severity, app_name,
            "Escalade immediate." if resolution.responder is not None
            else "Aucun repondant disponible."))
    body = json.dumps({"schema": VOICE_SCHEMA,
                       "application": getattr(application, "id", "")
                       or getattr(application, "title", ""),
                       "severity": severity, "reason": reason},
                      ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        status, response = transport(
            "POST", url, {"Authorization": "Bearer %s" % token,
                          "Content-Type": "application/json",
                          "Accept": "application/json"}, body, 5.0)
    except Exception:
        wake.status, wake.reason = "failed", "voice-unreachable"
        return wake
    if status not in (200, 201, 202, 204):
        wake.status, wake.reason = "failed", "voice-refused"
        return wake
    wake.status = "requested"
    try:
        document = json.loads(response.decode("utf-8")) if response else {}
    except ValueError:
        document = {}
    if isinstance(document, dict) and isinstance(document.get("call_id"), str):
        wake.request_id = document["call_id"]
    return wake


# --------------------------------------------------------------------------
# public document and journal
# --------------------------------------------------------------------------

def document(resolution: Resolution, wake: Wake, *, journal_id: int | None = None,
             error: dict | None = None) -> dict:
    application = resolution.application
    chosen = resolution.responder
    if error is not None:
        ok = False
    elif wake.plan == "none":
        ok = chosen is not None
    else:
        # The wake rule applies: the incident is only handled when the call
        # was actually requested (a disabled/refused call is a breach, not a
        # success even if a responder was also available for an immediate
        # security/data-loss/outage).
        ok = wake.status == "requested"
    out: dict = {
        "schema": SCHEMA,
        "ok": bool(ok),
        "application": {"id": getattr(application, "id", ""),
                        "title": getattr(application, "title", "")},
        "severity": resolution.severity,
        "responder": ({"member": "human:%s" % chosen.title, "title": chosen.title}
                      if chosen is not None else None),
        "reason": resolution.reason,
        "signals": list(resolution.signals if chosen is not None
                        else resolution.consulted),
        "wake": wake.to_dict(),
        "journal": {"event": journal_id} if journal_id is not None else None,
    }
    if resolution.entry is not None:
        entry = ({"role": resolution.entry.role} if resolution.entry.role
                 else {"member": resolution.entry.member})
        out["via"] = entry
    if error is not None:
        out["error"] = error
    return out


def journal_route(db: Any, resolution: Resolution, wake: Wake) -> int:
    """Append the one event the privacy rule allows: who, why, which signal
    kinds.  No activity timestamp, no presence value, no candidate list."""
    chosen = resolution.responder
    data = {
        "schema": SCHEMA,
        "application": getattr(resolution.application, "id", "")
        or getattr(resolution.application, "title", ""),
        "severity": resolution.severity,
        "responder": "human:%s" % chosen.title if chosen is not None else None,
        "reason": resolution.reason,
        "signals": list(resolution.signals if chosen is not None
                        else resolution.consulted),
        "wake": {"plan": wake.plan, "status": wake.status},
    }
    if wake.reason:
        data["wake"]["reason"] = wake.reason
    return events.record(db, EVENT,
                         "application:%s" % (getattr(resolution.application, "id", "")
                                             or getattr(resolution.application, "title", "")),
                         project=getattr(resolution.application, "id", "") or None,
                         **data)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh responder",
        description="Route an incident to the first available human declared by "
                    "the canon (schema ameesh-responder/1).")
    parser.add_argument("application", help="Application card id or title")
    parser.add_argument("--severity", required=True,
                        help="P1..P4, security, data-loss or outage")
    parser.add_argument("--json", action="store_true", help="one JSON document")
    parser.add_argument("--now", default=None, metavar="ISO",
                        help="simulation instant (default: now, UTC); a "
                             "simulation never calls and writes no event")
    parser.add_argument("--canon", default=None, help="canon directory or configured id")
    parser.add_argument("--ref", default=None, help="canon ref (default: configured ref)")
    return parser


def build_parser() -> argparse.ArgumentParser:
    return _parser()


def _now(value: str | None) -> _dt.datetime:
    if not value:
        return _dt.datetime.now(_dt.timezone.utc)
    moment = _moment(value)
    if moment is None:
        raise ValueError("--now: ISO-8601 instant expected, not %r" % (value,))
    return moment


def _refuse(args: argparse.Namespace, code: str, message: str) -> int:
    if args.json:
        print(json.dumps({"schema": SCHEMA, "ok": False,
                          "error": {"code": code, "message": message}},
                         ensure_ascii=False, sort_keys=True))
    else:
        print("ameesh responder: %s" % message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    from . import config as config_mod

    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        severity = routing.severity_of(args.severity)
        now = _now(args.now)
    except ValueError as exc:
        return _refuse(args, "invalid-argument", str(exc))
    cfg = config_mod.load()
    if args.ref:
        import dataclasses
        cfg = dataclasses.replace(cfg, canon_ref=args.ref)
    try:
        loaded = canon_mod.from_config(cfg, root=args.canon)
    except Exception as exc:
        return _refuse(args, "canon-unreadable", str(exc))
    application = loaded.application(args.application)
    if application is None:
        return _refuse(args, "application-not-found",
                       "no Application card %r in the canon" % (args.application,))
    findings = canon_mod.validate(loaded)
    blocking = canon_mod.blocking(findings)
    if blocking.global_errors:
        codes = ", ".join(sorted({f.code for f in blocking.global_errors}))
        return _refuse(args, "canon-invalid",
                       "the canon has blocking error(s): %s (ameesh canon check)" % codes)
    scoped = [f for f in findings
              if f.application and f.application in (application.id, application.title)]
    if scoped:
        codes = ", ".join(sorted({f.code for f in scoped}))
        return _refuse(args, "application-invalid",
                       "Application %r has error(s): %s (ameesh canon check)"
                       % (application.id or application.title, codes))
    if application.incident is None:
        return _refuse(args, "no-incident-block",
                       "Application %r declares no `incident:` block"
                       % (application.id or application.title))
    if not application.incident.covers(severity):
        return _refuse(args, "severity-not-covered",
                       "Application %r does not cover severity %s"
                       % (application.id or application.title, severity))
    try:
        db = db_mod.open_db(cfg)
    except db_mod.DbError as exc:
        return _refuse(args, "database-unavailable",
                       "database unavailable: %s" % exc)
    try:
        signals = LiveSignals(db=db)
        resolution = resolve(loaded, application, severity, signals, now)
        if args.now is not None:
            # #7866 (N1 auditeur) : une simulation ne demande AUCUN appel (une
            # fois le drapeau et le go actifs, un P1 simulé réveillerait
            # l'owner) et n'écrit aucun événement réel. Elle rend le routage
            # seulement ; le statut dit qu'aucun appel n'a été demandé.
            wake = Wake(plan=routing.wake_plan(severity, resolution.responder),
                        status="simulated")
            journal_id = None
        else:
            wake = request_call(application, severity, resolution, db=db, env=os.environ)
            journal_id = journal_route(db, resolution, wake)
        out = document(resolution, wake, journal_id=journal_id)
    except Exception as exc:
        return _refuse(args, "resolver-error", "%s: %s" % (type(exc).__name__, exc))
    finally:
        db.close()
    if args.json:
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    else:
        if resolution.responder is None:
            print("responder: nobody available for %s %s (%s); wake %s%s"
                  % (out["application"]["id"] or out["application"]["title"],
                     severity, resolution.reason, wake.status,
                     " (%s)" % wake.reason if wake.reason else ""))
        else:
            print("responder: %s for %s %s via %s; wake %s%s"
                  % (resolution.responder.title, out["application"]["id"]
                     or out["application"]["title"], severity, resolution.reason,
                     wake.status, " (%s)" % wake.reason if wake.reason else ""))
        print("journal: %s" % EVENT)
    return 0 if out["ok"] else 1


__all__ = ["EVENT", "LiveSignals", "Resolution", "SCHEMA", "Wake", "candidates",
           "document", "journal_route", "main", "request_call", "resolve",
           "verify_owner_go"]
