# SPDX-License-Identifier: AGPL-3.0-only
"""#7866 — incident routing: the canon's responders, then availability.

The stable half of an incident route lives in the canon: an `Application` card
declares, in order, the human responders (a role or a member) and the
severities the chain covers.  This module owns that schema, and the **pure**
availability rule that filters the declared humans at the moment of an
incident.

An `incident:` block of an `Application` card:

    incident:
      severities: [P1, P2, security]   # optional; all severities by default
      responders:                       # ordered, first available wins
        - role: project-lead
        - member: human:smichea
      escalation:
        wake: human:smichea             # who is called; never a phone number

A card without `incident:` stays valid (`incident` is None).  A malformed
block is refused by `canon check` with a named defect; the parser below returns
those defects so the canon does not have to duplicate the schema.

Availability, strongest first, each with its freshness window:

* `attach` — a live lease held for the member: locally
  `attach:<user>@<host>` matching one of their `local_accounts`, or distantly
  `attach:<tenant>:<device>` whose `device-es256` credential is theirs (the
  lease itself is the freshness bound);
* `interaction` — a human interaction in ameesh's journal that is proven to
  come from one of the member's own devices (`user@host` proof or a declared
  `local_accounts` host; mail sent, decision answered, never a delivery or a
  delegated answer) or, failing that, Memoria's public interface, fresher than
  `INTERACTION_FRESH_S`;
* `presence` — Slack presence (`active` means available, `away` means not),
  fresher than `PRESENCE_FRESH_S`;
* `timezone` — the member's declared IANA time zone and usual hours, the last
  resort.  A member without a time zone is never deemed reachable by default.

A **missing or stale signal is not an absence**: it passes to the next one.
Only a present `away` presence and the timezone/hours fallback can rule a
human out.  The resolver never stores what it observed: the journal keeps the
chosen responder and the *kinds* of signals used, never an activity timestamp,
a presence value or a list of who was online.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python sans tzdata
    ZoneInfo = None  # type: ignore

#: severities a chain can cover.  `security`, `data-loss` and `outage` are
#: conditions the owner's wake rule names, beside the usual P1..P4 grades.
SEVERITIES = ("P1", "P2", "P3", "P4", "security", "data-loss", "outage")
#: conditions that wake immediately, even when a responder is available
WAKE_IMMEDIATE = frozenset(("security", "data-loss", "outage"))
#: a P1 with no available responder wakes too; no other grade ever wakes
WAKE_ON_NO_RESPONDER = frozenset(("P1",))
#: usual local hours of a human, last-resort signal (start inclusive, end
#: exclusive).  Incidents do not know about weekends: the window applies daily.
USUAL_HOURS = (8, 20)
#: a human interaction older than this is stale and does not decide alone
INTERACTION_FRESH_S = 1800
#: Slack presence older than this is stale (the API answers live; the bound
#: protects against a cached provider)
PRESENCE_FRESH_S = 300

_MEMBER_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class Responder:
    """One ordered link of the chain: a member role, or a named member."""

    role: str | None = None
    member: str | None = None

    def key(self) -> str:
        return "role:%s" % self.role if self.role else "member:%s" % self.member


@dataclass(frozen=True)
class IncidentPolicy:
    """The `incident:` block of an application card, already normalized."""

    severities: tuple[str, ...]
    responders: tuple[Responder, ...]
    escalation: str | None = None

    def covers(self, severity: str) -> bool:
        return severity in self.severities

    def to_dict(self) -> dict:
        out: dict = {"severities": list(self.severities), "responders": []}
        for responder in self.responders:
            if responder.role is not None:
                out["responders"].append({"role": responder.role})
            else:
                out["responders"].append({"member": responder.member})
        if self.escalation:
            out["escalation"] = {"wake": self.escalation}
        return out


@dataclass(frozen=True)
class Availability:
    """The verdict for one human, with only the signal *kinds* used."""

    available: bool
    reason: str
    signals: tuple[str, ...] = ()


def severity_of(text: str) -> str:
    """Normalize a severity (`p1` → `P1`); ValueError when unknown."""
    value = str(text or "").strip()
    if value.upper() in ("P1", "P2", "P3", "P4"):
        return value.upper()
    lowered = value.lower().replace("_", "-")
    if lowered in ("security", "data-loss", "outage"):
        return lowered
    raise ValueError("unknown severity %r (known: %s)" % (text, ", ".join(SEVERITIES)))


def _strings(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def parse_incident(value: Any) -> tuple[IncidentPolicy | None, list[tuple[str, str]]]:
    """Parse an `incident:` block into `(policy, defects)`.

    `policy` is None without a block.  On a malformed block, a policy is still
    returned with the readable entries: the card is refused by `canon check`
    (each defect below is an error), and the resolver never silently routes
    with half a chain.  Defects are `(code, message)` pairs; the canon adds
    them with its `application` subject.
    """
    if value is None:
        return None, []
    if not isinstance(value, dict):
        return None, [("application-incident-invalid",
                       "`incident`: mapping expected (`severities`, `responders`, "
                       "`escalation`), not %r" % (value,))]
    defects: list[tuple[str, str]] = []
    unknown = sorted(set(value) - {"severities", "responders", "escalation"})
    for key in unknown:
        defects.append(("application-incident-key-unknown",
                        "`incident.%s`: unknown key (known: severities, responders, "
                        "escalation)" % key))

    severities: list[str] = list(SEVERITIES)
    if value.get("severities") is not None:
        raw = _strings(value["severities"])
        if raw is None:
            defects.append(("application-incident-severities-invalid",
                            "`incident.severities`: list of severities expected (%s), "
                            "not %r" % (", ".join(SEVERITIES), value["severities"])))
            severities = []
        elif not raw:
            defects.append(("application-incident-severities-empty",
                            "`incident.severities`: at least one severity expected "
                            "(omit the key to cover all of them)"))
            severities = []
        else:
            severities = []
            for item in raw:
                try:
                    name = severity_of(item)
                except ValueError:
                    defects.append(("application-incident-severity-unknown",
                                    "`incident.severities`: %r is not a known severity "
                                    "(%s)" % (item, ", ".join(SEVERITIES))))
                    continue
                if name not in severities:
                    severities.append(name)

    responders: list[Responder] = []
    raw_responders = value.get("responders")
    if raw_responders is None:
        defects.append(("application-incident-responders-missing",
                        "`incident.responders`: ordered list of `role:` / `member:` "
                        "entries expected"))
    elif not isinstance(raw_responders, list):
        defects.append(("application-incident-responders-invalid",
                        "`incident.responders`: list expected, not %r"
                        % (raw_responders,)))
    else:
        if not raw_responders:
            defects.append(("application-incident-responders-missing",
                            "`incident.responders`: at least one responder expected"))
        for index, entry in enumerate(raw_responders):
            where = "`incident.responders[%d]`" % index
            if not isinstance(entry, dict):
                defects.append(("application-incident-responder-invalid",
                                "%s: mapping with exactly one of `role` / `member` "
                                "expected, not %r" % (where, entry)))
                continue
            extra = sorted(set(entry) - {"role", "member"})
            if extra or len([k for k in ("role", "member") if entry.get(k)]) != 1:
                defects.append(("application-incident-responder-invalid",
                                "%s: exactly one of `role` / `member` expected (unknown "
                                "key(s): %s)" % (where, ", ".join(extra) or "none")))
                continue
            if entry.get("member") is not None:
                member = entry["member"]
                if not isinstance(member, str) or not _MEMBER_RE.match(member.strip()):
                    defects.append(("application-incident-member-invalid",
                                    "%s.member: `human:<id>` expected, not %r"
                                    % (where, member)))
                    continue
                responders.append(Responder(member=member.strip()))
            else:
                role = entry.get("role")
                if not isinstance(role, str) or not role.strip():
                    defects.append(("application-incident-responder-invalid",
                                    "%s.role: a non-empty role expected, not %r"
                                    % (where, role)))
                    continue
                responders.append(Responder(role=role.strip().lower()))

    escalation: str | None = None
    raw_escalation = value.get("escalation")
    if raw_escalation is not None:
        if isinstance(raw_escalation, str):
            raw_escalation = {"wake": raw_escalation}
        if not isinstance(raw_escalation, dict):
            defects.append(("application-incident-escalation-invalid",
                            "`incident.escalation`: mapping with `wake: human:<id>` "
                            "expected, not %r" % (raw_escalation,)))
        else:
            extra = sorted(set(raw_escalation) - {"wake"})
            for key in extra:
                defects.append(("application-incident-escalation-invalid",
                                "`incident.escalation.%s`: unknown key (only `wake` "
                                "is read; the phone number stays in the voice "
                                "service)" % key))
            wake = raw_escalation.get("wake")
            if wake is not None:
                if not isinstance(wake, str) or not _MEMBER_RE.match(wake.strip()):
                    defects.append(("application-incident-escalation-invalid",
                                    "`incident.escalation.wake`: `human:<id>` expected, "
                                    "not %r" % (wake,)))
                else:
                    escalation = wake.strip()

    return IncidentPolicy(severities=tuple(severities),
                          responders=tuple(responders),
                          escalation=escalation), defects


def in_usual_hours(timezone: str, now: _dt.datetime) -> bool:
    """Is `now` inside the usual local hours of `timezone`?  Invalid zone: no."""
    if not timezone or ZoneInfo is None:
        return False
    try:
        local = now.astimezone(ZoneInfo(timezone))
    except Exception:
        return False
    return USUAL_HOURS[0] <= local.hour < USUAL_HOURS[1]


def local_hour(timezone: str, now: _dt.datetime) -> int | None:
    if not timezone or ZoneInfo is None:
        return None
    try:
        return now.astimezone(ZoneInfo(timezone)).hour
    except Exception:
        return None


class Signals(Protocol):
    """The read-only probes the resolver needs.  Each returns None when the
    signal is absent or stale (the chain passes to the next one), never a
    default that would make someone look online."""

    def attach(self, member: Any) -> bool | None:
        ...

    def interaction(self, member: Any, now: _dt.datetime) -> bool | None:
        ...

    def presence(self, member: Any, now: _dt.datetime) -> bool | None:
        ...


class NoSignals:
    """A resolver without a database or a network: only the timezone fallback."""

    def attach(self, member: Any) -> bool | None:
        return None

    def interaction(self, member: Any, now: _dt.datetime) -> bool | None:
        return None

    def presence(self, member: Any, now: _dt.datetime) -> bool | None:
        return None


def availability(member: Any, signals: Signals, now: _dt.datetime,
                 ) -> tuple[Availability, set[str]]:
    """The availability verdict, and the signal kinds actually consulted.

    `member` only needs `timezone` and `local_accounts`/`fiche` attributes for
    the live probes; the pure rule reads `timezone`.  The returned set keeps
    the kinds consulted, never their value.
    """
    consulted: set[str] = set()

    attached = signals.attach(member)
    if attached is not None:
        consulted.add("attach")
        if attached:
            return Availability(True, "attached", ("attach",)), consulted

    interaction = signals.interaction(member, now)
    if interaction is not None:
        consulted.add("interaction")
        if interaction:
            return Availability(True, "recent-interaction", ("interaction",)), consulted

    presence = signals.presence(member, now)
    if presence is not None:
        consulted.add("presence")
        if presence:
            return Availability(True, "present", ("presence",)), consulted
        # A present `away` is a real negative signal: the next responder is
        # tried, the chain does not fall through to the timezone fallback.
        return Availability(False, "away", ("presence",)), consulted

    timezone = getattr(member, "timezone", None)
    if not timezone:
        # A member without a declared timezone is never deemed reachable by
        # default (owner's constraint).  Their stronger signals could still
        # have made them available above.
        return Availability(False, "no-timezone", ()), consulted
    consulted.add("timezone")
    if in_usual_hours(timezone, now):
        return Availability(True, "usual-hours", ("timezone",)), consulted
    return Availability(False, "outside-hours", ("timezone",)), consulted


def wake_plan(severity: str, chosen: Any | None) -> str:
    """`none`, `immediate` or `no-responder`: the owner's wake rule."""
    if severity in WAKE_IMMEDIATE:
        return "immediate"
    if chosen is None and severity in WAKE_ON_NO_RESPONDER:
        return "no-responder"
    return "none"


#: #7866 : the signed owner go for the phone wake.  The decision question
#: carries exactly one machine-readable line (no prose heuristics, the same
#: rule as `prod-go/1`):
#:
#:     incident-call/1 application=<id> until=<YYYY-MM-DDTHH:MM:SSZ>
#:
#: The go is not a free option key: only the reserved key and its exact
#: positive label below may carry the authorization.  Both are part of the
#: signed request (`decisions.answer_document`), so a refusal option labelled
#: "Non" can never be presented — or signed — as the authorization.
CALL_GO_OPTION = "incident-call"
CALL_GO_LABEL = "Oui, autoriser les appels de réveil"
CALL_GO_LINE = re.compile(
    r"^incident-call/1 application=([A-Za-z0-9._-]{1,64}) "
    r"until=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)$", re.M)
#: an emergency wake go is short-lived: a week, then the owner renews it
CALL_GO_MAX_HOURS = 24 * 7


@dataclass(frozen=True)
class CallGo:
    application: str
    until: float


def parse_call_go(question: Any) -> CallGo | None:
    """Read the `incident-call/1` declaration of a decision question.

    None when the question carries none; ValueError when the declaration is
    ambiguous (two lines, two markers) or unreadable — the gate fails closed.
    """
    text = str(question or "")
    if "incident-call/" not in text:
        return None
    matches = list(CALL_GO_LINE.finditer(text))
    if len(matches) != 1 or text.count("incident-call/") != 1:
        raise ValueError("one exact incident-call/1 declaration required")
    application, until = matches[0].groups()
    try:
        expiry = _dt.datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    except ValueError as exc:
        raise ValueError("incident-call/1 until is not a UTC date") from exc
    return CallGo(application=application, until=expiry)


__all__ = [
    "Availability", "CALL_GO_LABEL", "CALL_GO_MAX_HOURS", "CALL_GO_OPTION",
    "CallGo", "INTERACTION_FRESH_S", "IncidentPolicy", "NoSignals",
    "PRESENCE_FRESH_S", "Responder", "SEVERITIES", "USUAL_HOURS",
    "WAKE_IMMEDIATE", "WAKE_ON_NO_RESPONDER", "availability", "in_usual_hours",
    "local_hour", "parse_call_go", "parse_incident", "severity_of", "wake_plan",
]
