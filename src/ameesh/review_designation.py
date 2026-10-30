# SPDX-License-Identifier: AGPL-3.0-only
"""#1617 — automatic designation of reviewers at freeze (canon §7, #1606 point 4).

Canon §2 requires, for a frozen sensitive lot, "a review by another model
family (two for SQL, security or prod)". Until now an agent chose the reviewer
by hand, and on 15/10 eighteen lots waited without a reviewer, for up to 42 h.
This module does the **selection**: it takes the versioned policy
(`review_policies.train_reviews`, read by #1616/#1467), the exact frozen head,
the registry and, since #2668, the plan advance of each family
(`review_pace`), and returns either designated reviewers with their mail, or
an **explicit waiting state** with a capacity notification — never an implicit
admission. The family furthest behind its plan comes first, the load only
breaking a tie; an unreadable plan reading is neutral (#1561), and the pace
never makes an eligible family ineligible: two opinions still take two
distinct families.

What the module deliberately does not do:

- it reads neither the canon, nor the registry, nor the database: the policy is
  given to it as already parsed rules, the candidates as already read rows, and
  the real hookup (lot lock, idempotent mark, mails) is an injected `Port`.
  Selection is thus testable on its own, and the readers remain those of #1616
  (compiled canon) and #1467 (`train_policy.Versioned`);
- it **never** infers a family from an agent's name or assignment: the only
  source is the session's real family (`registry.live_harness`, #1037 / L242b);
- it neither counts nor requalifies verdicts: the required number comes from
  the maximum of the rules per path, the independence of the rendered opinions
  remains that of the merge gate (`merge_queue.review_gate`) and its authors
  reading (`review_authors`, #2681), and a new freeze invalidates the stale
  designation instead of reusing it.

Idempotence: the designation is tied to the **`frozen` milestone** (its
identifier and its exact SHA), not to the lot. The same freeze replayed returns
the already recorded designation without sending a second mail; a new freeze
(new milestone, new SHA) is another object, hence a new designation — the old
one no longer counts. A designation is **final** for its freeze; a waiting
state is a **retryable** marker (one notification per freeze, the designation
replaces it if capacity or evidence comes back). The `Port` refuses to write
for a freeze that is no longer the latest: no return to an old head.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Protocol, Sequence

from . import review

#: states returned by the selection
DESIGNATED = "designated"
WAITING = "waiting"
NONE = "none"
#: the kind of a designation mail: a request linked to the lot wakes the
#: reviewer's runner (#2478). The capacity notification stays a `notify`.
REQUEST_KIND = "request"
#: a harness that is not a model family (registry without a session)
PLACEHOLDER_FAMILIES = frozenset(("", "none", "other", "unknown", "inconnu"))
#: #4779: what a card must prove before its agent can be a reviewer. The
#: `propose` capacity is the ability to produce a change and a verdict; an
#: empty `roles` list means the card gives the agent no mandate at all. A
#: canary (`capabilities: [read]`, `roles: []`) was designated at a freeze and
#: held the merge gate of #4770 without any possible opinion. The registry
#: copies both fields from the card (`canon_sync`); a row without them is not
#: a proof — the candidacy is refused and the doubt is journaled.
REVIEWER_CAPABILITY = "propose"
#: why a row cannot be an automatic or manual reviewer (#4779)
CAPABILITIES_UNREADABLE = "capabilities-unreadable"
NO_PROPOSE = "no-propose"
ROLES_UNREADABLE = "roles-unreadable"
NO_ROLES = "no-roles"
#: the reasons that are a **read failure**, not a clean refusal: they are
#: journaled (an absent/unreadable field is not evidence of anything)
UNREADABLE_PROFILE_REASONS = frozenset((CAPABILITIES_UNREADABLE, ROLES_UNREADABLE))
#: the human-readable explanation of each refusal reason
PROFILE_REASONS = {
    CAPABILITIES_UNREADABLE: "its `capabilities` are absent or unreadable in the registry",
    NO_PROPOSE: "it does not carry the `propose` capacity",
    ROLES_UNREADABLE: "its `roles` are unreadable in the registry",
    NO_ROLES: "it declares no role",
}
#: the registry states of an agent that no longer lives: the lease reaper
#: writes `dead`, and a `stopped` agent waits for a human. Read by the
#: automatic designation (#3415) and by the reassignment scan of
#: `review_designation_activation` (#3328), so the two never disagree.
NON_LIVE_STATUS = frozenset(("stopped", "dead"))
#: #4491: the runner's marked pause (`status` `blocked`) — a failing
#: credential (`auth_blocked`, L226b), exhausted provider credits, a host that
#: is not ready, a missing account. No turn is taken until the cause is fixed,
#: so a request deposited there would hold the seat. The runner writes the
#: **same** status at the end of every failed turn (`last turn: … (failed)`,
#: or a `429` episode retried in a minute): those stay candidates, like the
#: account's short-window pause (#3876). `is_blocked_pause` is the single
#: reading that tells the named long causes from the short ones.
BLOCKED_STATUS = frozenset(("blocked",))
#: the registry states that forbid designation: the non-live ones and the
#: legacy `paused` state. An operator pause is a **column**, read by
#: `is_available` (#3415), not a status: the row keeps `idle`. A `blocked`
#: status is read by `is_blocked_pause` (#4491), because it covers both a lost
#: seat and the short-window pause that must stay a candidate.
UNAVAILABLE_STATUS = NON_LIVE_STATUS | frozenset(("paused",))
#: #4491: the `status_text` prefix the runner writes when the credential is
#: failing (`auth_failure.STATUS_PREFIX`, L226b). The status is `blocked`; the
#: text is checked too, so a row whose status was not refreshed still does not
#: take a request.
AUTH_BLOCKED_PREFIX = "auth_blocked"
#: the `status_text` prefixes of an operations or budget pause, and (#3973)
#: of an account configuration block (`accounts.CONFIG_BLOCK_PREFIX`, #3969):
#: an agent with no valid account on its host takes no turn until the host is
#: fixed, so a request deposited there would hold the seat. #4491 adds the
#: authentication block (`auth_failure.STATUS_PREFIX`, L226b).
PAUSE_PREFIXES = ("paused", "budget : ", "accounts : ", AUTH_BLOCKED_PREFIX)
#: #4491 B1: the **named long causes** written under `blocked`. Only these
#: make a lost seat: the end-of-turn failure marker (#1268, "failures x1
#: since …: last turn: … (failed)") and the transient rate episode (#1270,
#: "[rate_limited x1 …, retry in 60s, retry at …]", or "quota") are not here —
#: their pause lasts a minute and the reviewer resumes on its own (#3876).
#: `provider_credits.STATUS_PREFIX` and `runner.AgentWorker.HOST_STATUS` are
#: named here rather than imported (the runner is a heavy, higher layer);
#: `PAUSE_PREFIXES` already carries the account, auth and budget blocks.
BLOCKED_LONG_PREFIXES = (
    "provider_credits_exhausted",
    "hôte non prêt",
) + PAUSE_PREFIXES
#: a commit SHA (full or abbreviated): a branch name is not a freeze
_SHA = re.compile(r"[0-9a-f]{7,64}")


def _same_freeze(left: str, right: str) -> bool:
    """The same freeze: equality, or a prefix of at least 7 characters on both sides.

    A `frozen` milestone may carry an abbreviated SHA (finding #1617: milestone
    6281 carries "cbe0d00db") while the collector has the full SHA: strict
    equality wrongly declared `FreezeMoved` (#1775, N1).
    """
    left, right = str(left or "").strip().lower(), str(right or "").strip().lower()
    if left == right:
        return True
    short, long_ = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= 7 and long_.startswith(short)


class DesignationError(ValueError):
    """Refused input: the module does not guess, it refuses with a reason."""


class StaleFreeze(DesignationError):
    """The examined freeze is no longer the latest: the `Port` wrote **nothing**.

    A stale designation is not reused: the engine re-reads the latest freeze and
    starts over (never a return to an old head)."""


class FreezeMoved(StaleFreeze):
    """The freeze moved during the write: nothing was written, and the inputs of
    the examined freeze (paths, authors, class) no longer hold.

    The engine does not designate with the inputs of another head: the caller
    recollects the paths, authors and class of the **new** freeze, then replays.
    A changed mail SHA is not enough (Codex review cx2, B1)."""


# --------------------------------------------------------------------------
# the policy: number of required opinions per path
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """A rule of the versioned policy: patterns and a number of opinions.

    `paths` are patterns of the same family as `review.compile_glob`
    (`**`, `*`, `?`, `[...]`), compiled once at construction. `reviews` is 1
    or 2: the policy knows no other number (PR85), and a declared zero would be
    a rule that says nothing.
    """

    paths: tuple[str, ...]
    reviews: int
    #: the compiled patterns, in declared order
    regexes: tuple = field(default=(), compare=False, repr=False)

    def __post_init__(self):
        paths = tuple(self.paths or ())
        if not paths or any(not isinstance(p, str) or not p.strip() for p in paths):
            raise DesignationError("a review rule needs non-empty path patterns")
        if type(self.reviews) is not int or self.reviews not in (1, 2):
            raise DesignationError(
                "a review rule declares 1 or 2 reviews, not %r" % (self.reviews,))
        try:
            regexes = tuple(review.compile_glob(p) for p in paths)
        except re.error as exc:
            raise DesignationError("invalid review path pattern: %s" % exc) from exc
        object.__setattr__(self, "paths", paths)
        object.__setattr__(self, "regexes", regexes)

    def matches(self, path: str) -> bool:
        return any(regex.match(path) for regex in self.regexes)


def rules_of(declaration) -> tuple[Rule, ...]:
    """The rules of a `train_reviews.projects.<project>.rules` declaration.

    Strict shape, the one `ameesh.mechanics` (#1616) requires at the same
    commit: the project entry is exactly `{rules: [...]}`, each rule exactly
    `{paths: [...], reviews: 1|2}`, with no unknown key. Any other shape is
    refused — no permissive fallback: an unreadable policy designates nobody.
    (The `ameesh-train-reviews-policy/1` schema dresses the whole `train_reviews`
    object, not the project entry: it has nothing to do here.)
    """
    if not isinstance(declaration, dict) or set(declaration) != {"rules"} \
            or not isinstance(declaration["rules"], list) or not declaration["rules"]:
        raise DesignationError("explicit train review rules required; no implicit default")
    out = []
    for entry in declaration["rules"]:
        if not isinstance(entry, dict) or set(entry) != {"paths", "reviews"} \
                or not isinstance(entry["paths"], list):
            raise DesignationError("a review rule is {paths: [...], reviews: 1|2}")
        out.append(Rule(paths=tuple(entry["paths"]), reviews=entry["reviews"]))
    return tuple(out)


def required_count(rules: Sequence[Rule], paths: Iterable[str],
                   light: int = 0) -> int:
    """The number of opinions required for these paths: the **maximum** of the
    rules and of the declared light floor.

    The light floor comes from the caller that read it (`mechanics.reviews_for`,
    §2: one opinion, #2642); it is never guessed here. `light` defaults to 0 for
    the pure rule reader, whose only inputs are the rules. A path that no rule
    covers counts as the floor. Coverage of non-light paths is checked by the
    policy reader (#1467), not here.
    """
    if type(light) is not int or light < 0:
        raise DesignationError("light opinions: a non-negative integer is expected")
    rules = tuple(rules or ())
    highest = light
    for path in paths or ():
        for rule in rules:
            if rule.matches(path):
                highest = max(highest, rule.reviews)
    return highest


# --------------------------------------------------------------------------
# the candidates: real family, availability, load
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """A possible reviewer, as the registry describes it.

    `family` is the **real** family of the session
    (`registry.live_harness`); `available`, `load` and `model` come from the
    registry and from the charge computed by the caller (assigned open lots and
    pending reviews, #3004). `model` is only used to choose the model-scoped
    gauges that apply to this reviewer (#2668); it never changes the family or
    the eligibility. The module completes nothing: an empty family or a harness
    outside the known families makes the candidate ineligible.
    """

    name: str
    family: str
    available: bool = True
    load: int = 0
    model: str = ""

    def eligible(self, *, forbidden_names, forbidden_families, families) -> bool:
        name = (self.name or "").strip()
        family = (self.family or "").strip().lower()
        return bool(name) and self.available and family in families \
            and name not in forbidden_names and family not in forbidden_families


def live_family(row: Mapping, *, families) -> str:
    """The real family of a registry row, never the declared label.

    `registry.live_harness` (imported locally here so the module remains
    importable without a database) prefers `session_harness` over the declared
    harness: an agent that borrows a harness (L242b) is of the family under
    which it runs. A harness outside the known families is not a family; the
    family list comes from the compiled canon (#1616) as soon as it declares
    it.
    """
    from . import registry
    family = str(registry.live_harness(dict(row or {})) or "").strip().lower()
    return family if family in families else ""


def is_available(row: Mapping, *, closed_hosts: Iterable[str] = ()) -> bool:
    """Availability in the registry: neither stopped, nor dead, nor in a lost
    `blocked` pause, nor paused, nor under an operator pause, nor a human
    session, nor on a host whose claims are closed (#3328, #3415, #4491).

    An agent in a turn remains available: the mail waits for it and opens its
    next turn. Unavailable are a `stopped`/`dead`/`paused` agent (#3415: a
    reaped lease writes `dead`), a `blocked` agent with a **named long cause**
    (#4491: the runner marks a failing credential, exhausted provider credits,
    a host that is not ready, a missing account; `is_blocked_pause`), an
    `external` session (never run by the executor, L46), an operations or
    budget pause (`status_text` "paused…" or "budget : …", except the
    short-window pause of the account, #3876), an account configuration block
    (#3973: "accounts : …", no valid account on the host, #3969), an
    authentication block (#4491: "auth_blocked…", the credential is failing,
    L226b), an operator pause (`operator_paused`, a column: the row keeps
    `status` `idle` and its text "last turn: …", so the status and the prefixes
    never see it), and — #3328 — an agent whose host is `draining`/`lost`
    (claims closed): the request deposited there is not read until the host
    comes back, so the seat would stay blocked while the freeze waits. The host
    evidence is the one agent-mail uses (`alert_briefs.claims_closed_hosts`,
    #3036); an unreadable host state closes nothing, and a missing lease is not
    read: an idle reviewer is woken by the request mail, so it is still a
    candidate. The end-of-turn failure marker (#1268) and the transient
    `429`/`quota` episode (#1270) are minute-scale pauses, not lost seats:
    they stay candidates like the short window (#3876), as the auditeur's B1
    on #4491 required. A row with no readable mode or status is refused: doubt
    designates nobody.
    """
    row = dict(row or {})
    if (row.get("mode") or "execute") != "execute":
        return False
    # #3415: an operator pause is a human "no new turn" (#2200): the seat would
    # wait for a resume, so the designation never deposits a request there.
    if row.get("operator_paused"):
        return False
    status = str(row.get("status") or "").strip().lower()
    if not status or status in UNAVAILABLE_STATUS:
        return False
    # #4491: a `blocked` row is the runner's marked pause. Only the account's
    # short window (#3876) may keep it a candidate; an authentication, host,
    # database or account block takes no turn and would hold the seat.
    if is_blocked_pause(row):
        return False
    text = str(row.get("status_text") or "").strip().lower()
    if any(text.startswith(prefix) for prefix in PAUSE_PREFIXES) \
            and not is_short_window_pause(text):
        return False
    host = str(row.get("host") or "").strip()
    closed = {str(value).strip() for value in closed_hosts or () if str(value).strip()}
    return not (host and host in closed)


def is_short_window_pause(text: str) -> bool:
    """#3876: a budget pause that is only the account's short window (#3779).

    The runner writes the short-window refusal (cooldown, start gap, `2/2
    concurrent turns`) under the budget prefix, like a gauge pause. It lasts
    minutes, at most `account_window.COOLDOWN_MAX_S`: on 27/10 such a pause on
    cx1 and cx2 removed the whole codex family from the freeze of #3843, and a
    two-opinion lot waited for a hand designation while cx2 reviewed again
    within minutes. The request mail waits for the next admitted turn, as for
    an agent in a turn; a gauge pause (threshold, overage) stays unavailable.
    """
    from . import account_window, budget
    text = str(text or "").strip()
    prefix = budget.PAUSE_PREFIX.strip().lower()
    if not text.lower().startswith(prefix):
        return False
    rest = text[len(prefix):].lstrip()
    mark = budget.CRITICAL_MARK.lower()
    if rest.lower().startswith(mark):
        rest = rest[len(mark):]
    return account_window.is_short_window(rest)


def is_blocked_pause(row: Mapping) -> bool:
    """#4491: a `blocked` pause with a **named long cause**, not a short pause.

    The runner writes **one** status, `blocked`, for very different causes:
    a failing credential (`auth_blocked`, L226b), exhausted provider credits,
    a host that is not ready, a missing account, a gauge pause — and, at the
    end of **every failed turn**, a minute-scale retry (#1268 failure marker,
    #1270 `429`/`quota` episode with its `retry at`), and the account's
    short-window refusal (#3779). Only the named long causes take no turn
    until a human or the next poll fixes them; the failed-turn and short-window
    pauses stay reviewer candidates (#3876 and the auditeur's B1 on #4491).
    Treating the failure marker as a lost seat made a reviewer who failed one
    turn lose its seat and be replaced 60 s before resuming.

    A row that is not `blocked` is never a blocked pause: the long pauses
    written as `status_text` prefixes are read by `PAUSE_PREFIXES`. A long
    series of real failures is not read here either: the runner itself stops
    the agent after its fast-failure threshold (`stopped`, already a non-live
    status), and a slow series is an incident for the alerts, not a proof
    that the reviewer will not read its request.
    """
    if str((row or {}).get("status") or "").strip().lower() not in BLOCKED_STATUS:
        return False
    text = str((row or {}).get("status_text") or "").strip().lower()
    if not any(text.startswith(prefix) for prefix in BLOCKED_LONG_PREFIXES):
        return False
    if is_short_window_pause(text):
        return False
    from . import account_window
    return not account_window.is_short_window(text)


def reviewed_projects(row: Mapping) -> list[str]:
    """#3185: the projects a registry row reviews beside its team (`relit`).

    The column is a text array; a reader that returns it as `{a,b}` text is
    read the same way as `projects.agent_projects` does. NULL or absent: none.
    """
    declared = (row or {}).get("relit")
    if not declared:
        return []
    if isinstance(declared, str):
        declared = declared.strip("{}").split(",")
    return [p for p in (str(value or "").strip() for value in declared) if p]


def is_ephemeral(row: Mapping) -> bool:
    """#3411: an ephemeral agent (`ameesh agent spawn --ttl`) is never an
    automatic reviewer, alive or expired, idle or not.

    The two ephemerals of #3389 were designated reviewers of ordinary lots
    (one living, one expired): a measurement agent created for a one-off
    experiment has no reviewing role, and an expired one is dead. No exception
    is opened for a "review ephemeral": `agent spawn` can only grant
    `read`/`propose` (`canon.EPHEMERAL_CAPABILITIES`), so no ephemeral is
    created for that role today. An explicit mark would have to be added to the
    spawn contract and read here. The guard belongs to the automatic
    **selection** of candidates, never to the reassignment of a seat already
    written (a manual designation of an ephemeral stays possible).
    """
    return bool((row or {}).get("ephemeral"))


def is_retired(row: Optional[Mapping]) -> bool:
    """#3460: the agent was retired (`ameesh agent retire`): never designated."""
    return bool((row or {}).get("retired_ts"))


def _array_values(value) -> Optional[list[str]]:
    """The values of a registry text array, or None when it cannot be read.

    psycopg returns a list; a stored dump may carry the `{a,b}` text form.
    A scalar is a one-value array, as canon `_list` accepts a string too.
    Any other type is unreadable: the caller must not guess from it.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            text = text[1:-1]
        return [part.strip().strip('"') for part in text.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        # un élément NULL d'un tableau Postgres n'est pas un rôle
        return [str(part).strip() for part in value if part is not None]
    return None


def reviewer_eligibility(row: Mapping) -> tuple[bool, str]:
    """#4779: does this registry row's card allow the agent to review?

    `(True, "")` when the card proves both the `propose` capacity and at least
    one role, otherwise `(False, reason)` with a reason from `PROFILE_REASONS`.

    A missing/unreadable `capabilities` field is a **read failure**, not a
    refusal: the caller journals it (`UNREADABLE_PROFILE_REASONS`). A readable
    list without `propose` is a clean refusal. `roles` NULL means the card
    declares none (canon `_roles` returns None when the key is absent) and is
    a clean refusal too; only a non-array value is a read failure.
    """
    caps = (row or {}).get("capabilities")
    if caps is None:
        return False, CAPABILITIES_UNREADABLE
    values = _array_values(caps)
    if values is None:
        return False, CAPABILITIES_UNREADABLE
    if REVIEWER_CAPABILITY not in {value.strip().lower() for value in values}:
        return False, NO_PROPOSE
    roles = (row or {}).get("roles")
    if roles is None:
        return False, NO_ROLES
    values = _array_values(roles)
    if values is None:
        return False, ROLES_UNREADABLE
    if not [value for value in values if value]:
        return False, NO_ROLES
    return True, ""


def candidates_from_registry(rows: Iterable[Mapping], *, loads: Optional[Mapping] = None,
                             project: str = "", families,
                             closed_hosts: Iterable[str] = (),
                             unreadable_profiles: Optional[list] = None) -> list[Candidate]:
    """The candidates of a project: real family, availability, caller's charge.

    `loads` maps a name to the agent's charge (`review_designation_activation.
    _loads`: open lots assigned to them plus the reviews they still owe,
    #3004); when absent, the load is zero. `project` (the lot's `app`)
    restricts to agents of the same team: an ameesh lot is reviewed by an
    ameesh agent. An empty project does not filter — the caller knows what it
    is doing. `closed_hosts` (#3328) is the host liveness evidence from
    `alert_briefs.claims_closed_hosts`: a candidate whose host is closed is not
    available, exactly as agent-mail does not deliver there.

    #3185: an agent whose card declares the project under `relit:` is a
    candidate too, with its real family like any other: a project whose own
    agents cover a single family (nexlink: codex, plus DeepSeek harnesses)
    gets its second family from declared reviewers of another team. The
    exclusions (authors, correctors, the project's pilot) stay the caller's.
    #3411: an ephemeral agent is never a candidate (`is_ephemeral`).
    #3460: nor is a retired agent (`is_retired`).
    #4779: nor is an agent whose card does not prove the `propose` capacity
    and a role (`reviewer_eligibility`). An unreadable capability/roles field
    is not a proof: the candidacy is refused and the caller can journal it
    through `unreadable_profiles` (a list of `(name, reason)`).
    """
    loads = loads or {}
    wanted = (project or "").strip()
    closed = {str(value).strip() for value in closed_hosts or () if str(value).strip()}
    out = []
    for row in rows or ():
        if is_ephemeral(row) or is_retired(row):
            continue
        team = str((row or {}).get("team") or "").strip()
        if wanted and team != wanted and wanted not in reviewed_projects(row):
            continue
        name = str((row or {}).get("name") or "").strip()
        if not name:
            continue
        eligible, reason = reviewer_eligibility(row)
        if not eligible:
            if reason in UNREADABLE_PROFILE_REASONS and unreadable_profiles is not None:
                unreadable_profiles.append((name, reason))
            continue
        try:
            load = int(loads.get(name, 0))
        except (TypeError, ValueError):
            load = 0
        row = dict(row or {})
        model = str(row.get("model") or row.get("session_model") or "").strip()
        out.append(Candidate(name=name, family=live_family(row, families=families),
                             available=is_available(row, closed_hosts=closed),
                             load=max(load, 0),
                             model=model))
    return out


# --------------------------------------------------------------------------
# the selection: two distinct families, excluding every author (correctors
# included)
# --------------------------------------------------------------------------

def proven_families(author) -> set:
    """Every model family **proven** for one author row (#3584).

    A row carries its unique family in `family`; when an actor wrote under
    several families (a codex agent that froze in a deepseek fallback session,
    #2924 B6), `family` is empty by ambiguity and `families` keeps the whole
    proven set. That set is a proof, not an unknown: the caller excludes each
    of its families instead of waiting. Placeholders are not proofs, and a
    `test_only` corrector (#3518) forbids only their name.
    """
    row = dict(author or {}) if isinstance(author, Mapping) \
        else {"name": str(author or "").strip()}
    if row.get("test_only"):
        return set()
    proven = set()
    family = str(row.get("family") or "").strip().lower()
    if family and family not in PLACEHOLDER_FAMILIES:
        proven.add(family)
    values = row.get("families")
    if isinstance(values, (list, tuple, set, frozenset)):
        for value in values:
            value = str(value or "").strip().lower()
            if value and value not in PLACEHOLDER_FAMILIES:
                proven.add(value)
    return proven


def families_of(authors: Iterable) -> tuple[set, set]:
    """`(names, families)` forbidden, drawn from the authors of the frozen content.

    An author is `{"name", "family", "families"}`: the lot's assignee, each
    co-author and each reviewer who pushed a correction onto the head (exact
    evidence #1465, canon §2 and #2888). `families` carries every proven
    family when the actor wrote under several (#2924 B6): each one is
    forbidden (#3584). A `test_only` corrector (#3518/#3577) forbids only
    their name: their tests do not author the product delta, even on a
    sensitive path. An author with no proof is not guessed: `select` then
    waits instead of designating (see `unknown_families`).
    """
    names, families = set(), set()
    for author in authors or ():
        row = dict(author or {}) if isinstance(author, Mapping) \
            else {"name": str(author or "").strip()}
        name = str(row.get("name") or "").strip()
        if name:
            names.add(name)
        families.update(proven_families(row))
    return names, families


def unknown_families(authors: Iterable) -> list[str]:
    """The authors without any proven real family (empty or a label).

    Without a proof, a reviewer's independence cannot be proven: a reviewer
    of the same real family would be designated without anyone knowing. A
    row whose `families` set is non-empty is **not** unknown (#3584): the
    caller excludes each of those families. A `test_only` corrector (#3518)
    forbids only their name, as in `families_of`: their family is not needed,
    so they are never unknown (#3658)."""
    out = set()
    for author in authors or ():
        row = dict(author or {}) if isinstance(author, Mapping) \
            else {"name": str(author or "").strip()}
        name = str(row.get("name") or "").strip()
        if name and not row.get("test_only") and not proven_families(row):
            out.add(name)
    return sorted(out)


def names_of(rows: Iterable) -> set:
    """The names carried by `{name, ...}` rows or bare strings, stripped.

    The selection applies it to its `fixers` input: the same rows may be read
    from a journal mark (strings) or from the frozen range (mappings). A
    corrector's family comes from `families_of`, never from the name.
    """
    names = set()
    for row in rows or ():
        name = str((row or {}).get("name") or "").strip() if isinstance(row, Mapping) \
            else str(row or "").strip()
        if name:
            names.add(name)
    return names


def manual_seats(already: Iterable, *, authors: Iterable = (), fixers: Iterable = (),
                 families) -> list:
    """The manual reviewers that can count toward the required opinions.

    A manual reviewer is kept in the column whatever happens (`#2627`); this
    helper only says which ones cover a seat: a known family from the policy,
    distinct from every author's family, distinct from the other manual
    reviewers, and not an author itself. An unknown family, or the author's
    family, does not prove independence: the seat remains to designate.

    `fixers` are the designated reviewers who pushed a small first-round
    correction (#2739). They are authors of the head (#2888): their family is
    forbidden exactly like any author's, and the agent itself cannot count a
    seat — it would be reviewing its own fix. The name list is kept as a
    safety net when the caller's `authors` list is partial. A row with no
    proven family (`test_only`, #3518, or a corrector whose commit the author
    rewrote, #4457) excludes only the name; its family stays eligible.
    """
    names, forbidden = families_of(authors)
    fixer_names = names_of(fixers)
    forbidden = forbidden | families_of(fixers)[1]
    if unknown_families(authors):
        # An author of unknown family makes every reviewer's independence
        # unprovable: no manual reviewer can cover a seat either (#2627).
        return []
    covered = set(forbidden)
    seats = []
    seen_names = set()
    for row in already or ():
        row = dict(row or {})
        name = str(row.get("name") or "").strip()
        family = str(row.get("family") or "").strip().lower()
        if (not name or name in seen_names or name in names or name in fixer_names
                or not family or family in PLACEHOLDER_FAMILIES
                or family not in families or family in covered):
            continue
        seen_names.add(name)
        covered.add(family)
        seats.append({"name": name, "family": family})
    return seats


def automatic_seats(automatic: Iterable, *, authors: Iterable = (), fixers: Iterable = (),
                    families) -> list:
    """The automatic reviewers already designated for **the same freeze** (#3596).

    A retry of the same freeze keeps the seats it already handed out: they
    count as covered seats; only the missing ones are selected, and none is
    mailed again. They remain **automatic** (a new freeze retires them as
    today) and are therefore kept apart from `manual_seats`, which carries the
    manual exception. A manual seat that names the same agent or family is
    discarded by the caller, never the automatic one: the seat was already
    provided, and counting it twice would admit an opinion nobody holds.

    The independence rules are those of the first selection: a known family
    from the policy, outside every author's family and every corrector's
    family, and the agent itself is never a seat. An author of unknown family
    makes independence unprovable: no seat counts (canon §2).
    """
    if unknown_families(authors):
        return []
    names, forbidden = families_of(authors)
    fixer_names = names_of(fixers)
    forbidden = forbidden | families_of(fixers)[1]
    covered = set(forbidden)
    seats = []
    seen_names = set()
    for row in automatic or ():
        row = dict(row or {})
        name = str(row.get("name") or "").strip()
        family = str(row.get("family") or "").strip().lower()
        if (not name or name in seen_names or name in names or name in fixer_names
                or not family or family in PLACEHOLDER_FAMILIES
                or family not in families or family in covered):
            continue
        seen_names.add(name)
        covered.add(family)
        seats.append({"name": name, "family": family})
    return seats


def _finite(value) -> Optional[float]:
    """A finite float, or None; a malformed advance is ignored, never guessed."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalise_advances(advances) -> dict:
    """`{family: advance}` or `{family: {scope: advance}}`, numbers only.

    A flat value is the family's general advance; a nested mapping carries the
    general `""` key and one key per model scope (`fable`), so a candidate's
    model can pick the gauges that apply to it (#2668, cx3 B/P1). A nested
    `None` means the family carries that scope but no account has a current
    applicable reading: the model is neutral, never rescued by the general
    reading of an account whose scoped window is stale.
    """
    out: dict = {}
    for family, value in (advances or {}).items():
        family = str(family or "").strip().lower()
        if not family:
            continue
        if isinstance(value, Mapping):
            nested = {}
            for scope, number in value.items():
                scope = str(scope or "").strip().lower()
                if number is None:
                    nested[scope] = None
                    continue
                number = _finite(number)
                if number is not None:
                    nested[scope] = number
            if nested:
                out[family] = nested
        else:
            number = _finite(value)
            if number is not None:
                out[family] = number
    return out


def _advance_of(candidate, pace: Mapping) -> float:
    """The advance applicable to one candidate, model-scoped gauges included.

    A scope counts when its name is part of the candidate's model, the rule of
    `accounts.model_scoped`; the general `""` reading is used only when no
    scope matches. A matching scope with no current reading (`None`) makes the
    candidate neutral (0) — it does not fall back to the general reading of a
    stale account (cx3 B/P1). A family absent from `pace` is neutral too.
    """
    entry = pace.get((candidate.family or "").strip().lower())
    if entry is None:
        return 0.0
    if not isinstance(entry, Mapping):
        return entry
    model = str(getattr(candidate, "model", "") or "").strip().lower()
    matching = [value for scope, value in entry.items()
                if scope and scope in model]
    if matching:
        values = [value for value in matching if value is not None]
        return max(values) if values else 0.0
    general = entry.get("")
    return general if general is not None else 0.0


def select(*, required: int, authors: Iterable = (), candidates: Iterable = (),
           families, already: Iterable = (), fixers: Iterable = (),
           advances: Optional[Mapping] = None, automatic: Iterable = ()) -> dict:
    """Chooses `required` (1 or 2) independent reviewers, by plan pace then load.

    - each reviewer's family differs from that of **every** author, and for two
      opinions the two families differ from each other (canon §2);
    - the family comes from the real session (`Candidate.family`), never from
      the name;
    - an unavailable candidate, without a known family, an author itself, or an
      agent that pushed a reviewer's correction is discarded: a reviewer's
      correction makes its author an author of the head (#2888), so neither the
      exact agent nor its family can review that head;
    - an author whose real family is unknown (empty or a label) makes the
      designation **wait**: without it, a reviewer's independence is not proven
      (canon §2);
    - `advances` maps a family to its advance over the plan pace, in points
      (`review_pace`; positive = ahead, negative = behind). A family value may
      be a `{scope: advance}` mapping: the general `""` plus the model-scoped
      buckets, selected by the candidate's `model` (a `fable` bucket counts
      only for a Fable reviewer, never for another model or an unknown one).
      When several families are eligible, the family **furthest behind** comes
      first; an absent reading is neutral (0, at its pace), so the other
      criteria decide (#2668, #1561). Never a reason to discard an eligible
      family: two opinions still take two distinct families;
    - the load of the agent only breaks a pace tie (least loaded first), then
      the name: the same order chooses the family's representative and orders
      the families; the selection is deterministic. The load is the caller's
      charge (`Candidate.load`), which counts the agent's open lots **and** the
      reviews they still owe (#3004);
    - `already` names the manual reviewers already designated (#2627). The ones
      that prove an independent family cover a seat; only the **missing** seats
      are designated, never a manual reviewer removed or overridden;
    - `automatic` names the automatic reviewers already designated for **this
      freeze** (#3596): a retry keeps their seats and only provides the missing
      ones. They count like the manual seats, never twice (a name or family
      already covered by a manual seat is skipped), and they stay automatic:
      a new freeze retires them as today;
    - `fixers` names the designated reviewers whose small first-round
      correction is an authored commit of the head (#2739/#2888). Their family
      is forbidden like any author's family; their exact name is excluded too,
      as a safety net: they would review their own fix. A fixer row with an
      empty family is a **personal** exclusion only (`test_only`, #3518, or a
      rewritten correction, #4457): its family remains eligible.

    Returns `{state, required, reviewers, missing, manual, why, forbidden,
    families, fixers}`: `waiting` when independent families are missing or an author
    family is unknown, with the missing count — never an implicit admission.
    A partial designation is still `waiting`, but `reviewers` names the seats
    it could already provide (families available now); the missing ones wait
    for capacity. `manual` the manual seats that count; `families` the eligible
    automatic families, previously designated ones included (outside the
    authors and the manual seats). With the manual seats, that count is the
    independent families available on this head: a reviewer's correction that
    makes the reviewer an author of the head removes one, so it is receivable
    only if the rest still covers the required opinions (canon §2, #2670).
    """
    if type(required) is not int or required not in (0, 1, 2):
        raise DesignationError("required reviews must be 0, 1 or 2, not %r" % (required,))
    names, forbidden = families_of(authors)
    fixer_names = names_of(fixers)
    #: #4679 : un correcteur dont le commit local (avant rebase/amend) est
    #: introuvable reste exclu par son nom ; le gel dit le doute et le SHA,
    #: au lieu de le redésigner en silence.
    fixer_doubts: dict[str, str] = {}
    for row in fixers or ():
        if isinstance(row, Mapping) and row.get("unresolved"):
            name = str(row.get("name") or "").strip()
            if name:
                fixer_doubts[name] = str(row.get("unresolved") or "").strip()

    def excluded_fixers_text() -> str:
        text = ", ".join(sorted(fixer_names))
        if fixer_doubts:
            text += (" (unresolved correction: %s — commit not recoverable,"
                     " excluded by doubt)" % ", ".join(
                         "%s %s" % (name, sha[:12])
                         for name, sha in sorted(fixer_doubts.items())))
        return text

    forbidden = forbidden | families_of(fixers)[1]
    seats = manual_seats(already, authors=authors, fixers=fixers, families=families)
    kept = automatic_seats(automatic, authors=authors, fixers=fixers,
                           families=families)
    if kept:
        # #3596: an automatic seat already provided on this freeze is never
        # counted twice, and it is the one kept (the manual column may name the
        # same agent or family): only a genuinely additional manual seat counts.
        kept_names = {row["name"] for row in kept}
        kept_families = {row["family"] for row in kept}
        seats = [row for row in seats
                 if row["name"] not in kept_names
                 and row["family"] not in kept_families]
    # #3968 : `forbidden` ne porte que les familles **d'auteurs** (et de
    # correcteurs). Les familles déjà couvertes par un siège (manuel ou
    # automatique du même gel) sont une autre preuve : les confondre dans le
    # même mot faisait lire « codex interdit comme auteur » alors que codex
    # était le siège manuel de codex1, sur une tête sans commit codex.
    seat_rows = [dict(row) for row in seats + kept]
    seat_families = {row["family"] for row in seat_rows}
    out = {"state": NONE, "required": required, "reviewers": [], "missing": 0,
           "manual": seats, "why": "no review required",
           "forbidden": sorted(forbidden),
           "author_families": sorted(forbidden),
           "seat_families": sorted(seat_families),
           "seats": seat_rows, "families": [],
           "fixers": sorted(fixer_names)}
    if required == 0:
        return out
    out["families"] = sorted({row["family"] for row in kept})
    remaining = max(required - len(seats) - len(kept), 0)
    if remaining == 0:
        out.update({"state": DESIGNATED, "missing": 0,
                    "reviewers": [dict(row) for row in kept],
                    "why": ("reviewer(s) already designated for this freeze cover"
                            " the required reviews") if kept
                           else "manual reviewer(s) already cover the required reviews"})
        return out
    unknown = unknown_families(authors)
    if unknown:
        # An author with no known real family: independence is not proven, so
        # we wait instead of discarding the only name (canon §2).
        out.update({"state": WAITING, "missing": remaining,
                    "reviewers": [dict(row) for row in kept],
                    "why": "author family unknown: %s" % ", ".join(unknown)})
        return out
    seat_names = {row["name"] for row in seats} | {row["name"] for row in kept}
    eligible = [c for c in candidates or ()
                if c.eligible(forbidden_names=names | seat_names | fixer_names,
                              forbidden_families=forbidden | seat_families,
                              families=families)]
    # #2668: a family's advance over its plan pace (points), lower is further
    # behind. An unreadable or stale reading (#1561) is neutral: 0, as if at
    # pace, so the load and the name decide. A model-scoped value is selected
    # by the candidate's own model.
    pace = _normalise_advances(advances)
    # a single candidate per family: the applicable pace first (cx3 B/P1: a
    # Fable bucket must not make a less-loaded Fable agent hide a further
    # behind Opus agent of the same family), then the least loaded, then the
    # name.
    best: dict[str, Candidate] = {}
    for candidate in eligible:
        family = candidate.family.strip().lower()
        current = best.get(family)
        if current is None or ((_advance_of(candidate, pace), candidate.load,
                                candidate.name)
                               < (_advance_of(current, pace), current.load,
                                  current.name)):
            best[family] = candidate
    ordered = [best[family] for family in sorted(best)]
    # pace first, then load, then name; a family absent from `pace` is neutral
    ordered.sort(key=lambda c: (_advance_of(c, pace), c.load, c.name))
    chosen = ordered[:remaining]
    reviewers = [dict(row) for row in kept]
    reviewers.extend({"name": c.name, "family": c.family} for c in chosen)
    out["reviewers"] = reviewers
    out["families"] = sorted(set(best) | {row["family"] for row in kept})
    if len(chosen) < remaining:
        # #3968 : le refus nomme sa cause en deux ensembles distincts. Les
        # familles d'auteurs interdisent un avis ; les familles des sièges déjà
        # pourvus ne sont pas des familles d'auteurs (elles couvrent un avis
        # requis). Les fondre dans un seul « forbidden » faisait conclure à un
        # défaut d'attribution sur une tête sans commit de la famille en cause.
        missing_why = ("%d independent %s missing: %d eligible famil%s (%s); "
                       "forbidden author families: %s"
                       % (remaining - len(chosen),
                          "review" if remaining - len(chosen) == 1 else "reviews",
                          len(ordered), "y" if len(ordered) == 1 else "ies",
                          ", ".join(sorted(best)) or "none",
                          ", ".join(sorted(forbidden)) or "none"))
        if seat_rows:
            missing_why += ("; already covered by seat(s): %s"
                            % ", ".join("%s (%s)" % (row["name"], row["family"])
                                        for row in sorted(
                                            seat_rows,
                                            key=lambda item: (item["name"],
                                                              item["family"]))))
        if kept:
            missing_why += (" (already designated: %s)"
                            % ", ".join(row["name"] for row in kept))
        if fixer_names:
            missing_why += " (corrector(s) excluded: %s)" % excluded_fixers_text()
        out.update({
            "state": WAITING,
            "missing": remaining - len(chosen),
            "reviewers": reviewers,
            "why": missing_why,
        })
        return out
    why = "designated %d reviewer(s)" % len(chosen)
    if kept:
        why += " (kept %d already designated for this freeze)" % len(kept)
    if fixer_names:
        why += " (corrector(s) excluded: %s)" % excluded_fixers_text()
    if pace:
        reading = ["%s %+.0f pt" % (c.family.strip().lower(), _advance_of(c, pace) * 100)
                   for c in chosen if c.family.strip().lower() in pace]
        if reading:
            why += " (plan pace: %s)" % ", ".join(reading)
    # #3004: the journal (why) keeps the load each chosen reviewer was retained
    # with, so a designation can be audited against the reviewer's real queue.
    if chosen:
        why += " (load: %s)" % ", ".join(
            "%s %d" % (c.name, max(int(c.load or 0), 0)) for c in chosen)
    out.update({"state": DESIGNATED, "why": why})
    return out


# --------------------------------------------------------------------------
# the mails: the exact head, or the missing capacity
# --------------------------------------------------------------------------

def _correction_room(required: int, eligible_families: Optional[int],
                     correction: Optional[Mapping]) -> Optional[int]:
    """The independent families a correction by the recipient would leave.

    The exact preview (`review_correction.outlook`, #3416) wins when it is
    conclusive (`ok` or `short`); otherwise the family count of the head minus
    the recipient's own family. None: nothing is known, the mail states the
    rule alone (doubt never says "push").
    """
    room = dict(correction or {})
    if room.get("state") in ("ok", "short") and int(room.get("required") or 0) > 0:
        return max(int(room.get("remaining") or 0), 0)
    if eligible_families is not None:
        return max(int(eligible_families) - 1, 0)
    return None


def _do_not_push_line(remaining: int, required: int, lot, sha: str = "") -> str:
    """The first instruction when a reviewer correction would be too tight (#4325)."""
    return (
        "- **do not push this correction: return the fix to the author, verdict "
        "blocked** (`ameesh work milestone %s verdict blocked --sha %s`, with the "
        "defect). A correction of yours would make you an author of the new head "
        "and leave %d independent famil%s for %d required opinion(s): the merge "
        "gate could not be satisfied. The author fixes it and freezes a new head "
        "(canon §2, #2670/#2888);"
        % (lot, sha or "<frozen sha>", remaining,
           "y" if remaining == 1 else "ies", required))


def reviewer_message(lot: dict, *, freeze: Mapping, required: int, rank: int,
                     reviewer: Mapping, authors: Iterable = (), base: str = "",
                     risk: str = "", policy_ref: str = "",
                     eligible_families: Optional[int] = None,
                     correction: Optional[Mapping] = None) -> str:
    """The mail that hands the reviewer the exact head and its place.

    The SHA is written in full, with its `frozen` milestone: the verdict will
    have to cite it as-is (`ameesh work milestone <lot> verdict ok --sha <sha>`).
    Any correction makes the reviewer an author of the new head, whatever its
    size (canon §2, #2888): their own opinion no longer counts and their family
    is no longer eligible for the opinions on that head. `eligible_families` is
    the number of independent families available on this head (automatic and
    manual seats): if the correction would leave fewer than `required`
    families, the mail tells the reviewer not to push it and to return it to
    the author instead (canon §2, #2670). Without the count, the mail states
    the rule alone. The `~50 lines` threshold only says whether a correction is
    a first-round fix or goes back to the author; it never keeps a family
    eligible.

    `correction` is the room computed for this very reviewer as a corrector
    (`review_correction.outlook`, the `--if-corrector` preview, #3416): when it
    reads `ok` or `short` it overrides the family count. A correction that
    would leave too few families opens the instructions with "do not push"
    (#4325): the mail no longer starts with "fix it yourself".
    """
    sha = str((freeze or {}).get("sha") or "")
    fid = (freeze or {}).get("id")
    row = dict(lot or {})
    who = ", ".join(str((a or {}).get("name") or "") for a in authors or () if a) or "unknown"
    lines = [
        "Automatic designation at freeze (canon §2, #1617): review %d/%d of lot #%s."
        % (rank, required, row.get("id")),
        "",
        "Lot: #%s \"%s\"" % (row.get("id"), " ".join(str(row.get("title") or "").split())),
        "- Freeze: frozen milestone #%s, exact head %s." % (fid, sha),
        "- Author(s) of the frozen content: %s." % who,
    ]
    if base:
        lines.append("- Diff to review: `git diff %s..%s` (%s)."
                     % (base[:12], sha[:12], risk or "unknown class"))
    else:
        lines.append("- To review: head %s (%s)." % (sha[:12], risk or "unknown class"))
    if policy_ref:
        lines.append("- Review policy: %s (two opinions for SQL, security or prod)."
                     % policy_ref)
    lines += [
        "- Your family: %s — rendered opinions come from distinct families, "
        "outside those of the authors." % ((reviewer or {}).get("family") or "?"),
        "",
        "To do:",
    ]
    remaining = _correction_room(required, eligible_families, correction)
    if remaining is not None and remaining < required:
        lines += [
            _do_not_push_line(remaining, required, row.get("id"), sha),
            "- read the diff **of this exact head** and check the edge cases;",
            "- render the verdict: `ameesh work milestone %s verdict ok|blocked "
            "--sha %s`;" % (row.get("id"), sha),
            "- a non-blocking defect goes to the backlog, it does not block the merge;",
            "- do not rerun the suites: targeted tests are the author's job, CI "
            "runs on the frozen head once the required opinions are acquired.",
            "",
            "This mail comes from the freeze: if the branch moves, a new freeze "
            "cancels this designation and a new head will be handed to you.",
        ]
        return "\n".join(lines)
    lines += [
        "- read the diff **of this exact head** and check the edge cases;",
        "- render the verdict: `ameesh work milestone %s verdict ok|blocked --sha %s`;"
        % (row.get("id"), sha),
        "- fix a blocking defect yourself and push to the lot branch "
        "(canon §2): **any correction makes you an author of the new head**, "
        "whatever its size — your own opinion no longer counts and your family "
        "is no longer eligible for the opinions on that head; the second family "
        "reviews the lot **with** your fix, in the same round;",
        "- a fix longer than ~50 lines, or a design change, is not a first-round "
        "correction: it goes back to the author;",
        "- your commits carry their exact origin (`ameesh provenance show --commit "
        "<sha>`): the next designation reads it to establish who authored the "
        "head;",
    ]
    if remaining is not None:
        lines.append(
            "- this correction is receivable: it leaves %d independent "
            "famil%s for %d required opinion(s) (canon §2, #2670/#2888);"
            % (remaining, "y" if remaining == 1 else "ies", required))
    else:
        lines.append(
            "- any correction makes you an author of the new head: do not push "
            "one that would leave fewer independent families than required "
            "opinions (canon §2, #2670/#2888) — return it to the author, who "
            "fixes it and freezes a new head;")
    lines += [
        "- a non-blocking defect goes to the backlog, it does not block the merge;",
        "- do not rerun the suites: targeted tests are the author's job, CI "
        "runs on the frozen head once the required opinions are acquired.",
        "",
        "This mail comes from the freeze: if the branch moves, a new freeze cancels "
        "this designation and a new head will be handed to you.",
    ]
    return "\n".join(lines)


def manual_reviewer_message(lot: Mapping, *, freeze: Optional[Mapping] = None,
                            actor: str = "",
                            correction: Optional[Mapping] = None) -> str:
    """The request mail of a manual designation (#705, #2478).

    Unlike the automatic mail, this designation can happen as soon as the lot
    enters `qa`: the head may not be frozen yet. The mail names the latest
    frozen head when one exists and says what to watch otherwise. It is sent as
    a `request` linked to the lot, so the reviewer's runner opens a turn (#2478).

    `correction` is the `--if-corrector` preview of the recipient on the frozen
    head (`review_correction.check`, #3416): when it reads `short`, the
    instructions open with "do not push" instead of "fix it yourself" (#4325).
    Any other reading keeps the text that states the rule.
    """
    row = dict(lot or {})
    freeze = dict(freeze or {})
    sha = str(freeze.get("sha") or "")
    fid = freeze.get("id")
    lines = [
        "Review request (#2076): you are the designated reviewer of lot #%s."
        % row.get("id"),
        "",
        "Lot: #%s \"%s\"" % (row.get("id"), " ".join(str(row.get("title") or "").split())),
    ]
    if row.get("app"):
        lines.append("- Project: %s." % row["app"])
    if sha:
        lines += [
            "- Latest frozen head: %s (frozen milestone #%s)."
            % (sha, fid if fid is not None else "?"),
            "- Review exactly this head and render your verdict:",
            "  ameesh work milestone %s verdict ok|blocked --sha %s" % (row.get("id"), sha),
        ]
    else:
        lines += [
            "- No frozen head is recorded yet: the author declares it with",
            "  ameesh work milestone %s frozen --sha <commit>." % row.get("id"),
            "- Review exactly the frozen head and render your verdict:",
            "  ameesh work milestone %s verdict ok|blocked --sha <frozen sha>"
            % row.get("id"),
        ]
    lines.append("- This request is linked to lot #%s: your runner opens a turn on it."
                 % row.get("id"))
    if actor:
        lines.append("- Designated by: %s." % actor)
    room = dict(correction or {})
    if room.get("state") == "short" and int(room.get("required") or 0) > 0:
        lines += [
            "",
            "To do:",
            _do_not_push_line(max(int(room.get("remaining") or 0), 0),
                              int(room["required"]), row.get("id"), sha),
            "- read the diff of the frozen head and check the edge cases;",
        ]
    else:
        lines += [
            "",
            "To do:",
            "- read the diff of the frozen head and check the edge cases;",
            "- a blocking defect: fix it yourself and push to the lot branch "
            "(canon §2). Any correction makes you an author of the new head: your "
            "own opinion no longer counts and your family is no longer eligible; "
            "a fix longer than ~50 lines, a design change, or a correction that "
            "would leave fewer independent families than required opinions goes "
            "back to the author (canon §2, #2670/#2888);",
        ]
    lines += [
        "- a non-blocking defect goes to the backlog, it does not block the merge;",
        "- do not rerun the suites: targeted tests are the author's job, CI runs "
        "on the frozen head once the required opinions are acquired.",
    ]
    # #2495 : nothing cancels a manual request; a new freeze does not hand a
    # new head to this reviewer either. Say what to review if the branch moves.
    lines += ["", "A new freeze does not cancel this request: if the branch moves, "
              "review the latest frozen head (`ameesh work show %s`) and render your "
              "verdict on it." % row.get("id")]
    return "\n".join(lines)


def _request_sha(row: Mapping) -> Optional[str]:
    """Review request head, empty before freeze, None for another mail."""
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, Mapping):
        return None
    auto = payload.get("review_designation")
    if isinstance(auto, Mapping):
        return str(auto.get("sha") or "").strip().lower()
    manual = payload.get("review_request")
    if isinstance(manual, Mapping):
        freeze = manual.get("freeze")
        if isinstance(freeze, Mapping):
            return str(freeze.get("sha") or "").strip().lower()
        return ""
    return None


def covering_request(rows: Iterable[Mapping], *, lot, reviewer: str,
                     sha: str = "") -> Optional[dict]:
    """The earlier request that already covers (lot, reviewer, head), if any (#2495).

    One request per reviewer and head: a re-designation, or a manual
    designation followed by the automatic one at the freeze, sends no second
    mail. `rows` are the requests already addressed to the reviewer for this
    lot (`mail.review_requests`, #2504): the lot is filtered in storage, so no
    `history` window can hide an old request. A request covers
    the new one when it names the same frozen head, or when it names no head
    (manual designation before the freeze) and is either still unread — the
    reviewer will read it and review the frozen head — or the new one names no
    head either. A delivered request without a head does not cover a freeze:
    the reviewer already took that turn, the freeze must wake them again.
    """
    name = str(reviewer or "").strip()
    target = str(sha or "").strip().lower()
    for row in rows or ():
        if str(row.get("kind") or "") != REQUEST_KIND:
            continue
        if str(row.get("work_item_id") or "").strip() != str(lot).strip():
            continue
        if str(row.get("recipient") or "").strip() != name:
            continue
        prior = _request_sha(row)
        if prior is None:
            continue
        if prior and target and _same_freeze(prior, target):
            return dict(row)
        if not prior and (not target or not row.get("delivered_ts")):
            return dict(row)
    return None


def capacity_message(lot: dict, *, freeze: Mapping, required: int, missing: int,
                     why: str, families: str = "", designated: Iterable = ()) -> str:
    """The waiting notification: independent review capacity is missing.

    It states what is missing — review capacity or author evidence — and asks
    for no admission: the lot remains waiting for the required number of
    opinions. `designated` names the seats already provided on this freeze
    (#3596): the notice then says the designation is incomplete, not
    impossible, and the missing seats alone wait for capacity.
    """
    row = dict(lot or {})
    kept = [dict(entry or {}) for entry in designated or ()]
    first = ("Designation at freeze incomplete (canon §2, #1617) — review pending."
             if kept else
             "Designation at freeze impossible (canon §2, #1617) — review pending.")
    lines = [
        first,
        "",
        "Lot: #%s \"%s\"" % (row.get("id"), " ".join(str(row.get("title") or "").split())),
        "- Freeze: frozen milestone #%s, head %s." % ((freeze or {}).get("id"),
                                                str((freeze or {}).get("sha") or "")[:12]),
        "- Required opinions: %d; missing: %d." % (required, missing),
        "- Reason: %s" % why,
    ]
    if kept:
        lines.append("- Already designated: %s." % ", ".join(
            "%s (%s)" % (entry.get("name"), entry.get("family") or "?")
            for entry in kept))
    if families:
        lines.append("- Available families: %s." % families)
    lines += [
        "",
        "No implicit admission: the lot remains pending its opinions. Restore the "
        "missing capacity or evidence (another model family in the registry, an "
        "available reviewer, the exact provenance of the commits) then rerun the "
        "designation.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the hookup: a single transactional write on the Port side
# --------------------------------------------------------------------------


class Port(Protocol):
    """The real hookup: database, registry and mail. The engine does no I/O.

    `commit` is **the** transaction: it locks the lot, rechecks that `freeze` is
    still the latest freeze — otherwise it raises `StaleFreeze` without writing
    anything (no return to an old head) —, rechecks the idempotent mark of the
    freeze (a concurrent call returns the winner's designation, without a
    second mail) and deposits the mails in the same transaction.

    A **designation is final** for its freeze: replayed, it returns the same
    thing without a second mail. A **waiting state** is a retryable marker: one
    notification per freeze, and if capacity comes back (or author evidence),
    the designation replaces the marker for the same freeze — the mails to the
    reviewers only leave then, once.
    """

    def latest_freeze(self, lot: int) -> Optional[dict]:
        """The lot's latest `frozen` milestone: `{id, sha}`, or None."""

    def recorded(self, lot: int, freeze_id: int) -> Optional[dict]:
        """The designation already recorded for **this** freeze, or None."""

    def commit(self, lot: int, *, freeze: Mapping, required: int, state: str,
               reviewers: Sequence[Mapping], missing: int, why: str,
               authors: Sequence[Mapping], policy_ref: str = "",
               notify: str = "", messages: Sequence[Mapping] = (),
               unknown_authors: Sequence[str] = ()) -> dict:
        """Writes the freeze's designation in one transaction and returns its shape."""


def apply(lot_item: Mapping, *, policy, paths: Iterable[str], port: Port,
          freeze_sha: str, authors: Iterable = (), candidates: Iterable = (), risk: str = "light",
          base: str = "", policy_ref: str = "", notify: str = "",
          unknown_authors: Iterable = (), families,
          already: Iterable = (), fixers: Iterable = (), required: Optional[int] = None,
          advances: Optional[Mapping] = None, held: Iterable = (),
          doubts: Iterable = ()) -> dict:
    """Designates the reviewers of the lot's latest freeze, or says why it waits.

    `policy` carries the already parsed rules (`Rule` or `{rules: [...]}`
    declaration); `risk` is the class read by the policy reader (`light` or
    `sensitive`) and serves as a guard: a non-light class with no required
    opinion is a refusal, never a silent zero. `port` does the write (see
    `Port`). Returns the designation: `{state, required, reviewers, missing,
    why, freeze}` with `replayed` when it already existed.

    `required` is the count already read by the policy reader
    (`mechanics.reviews_for`) and includes the light floor (#2642) and the
    critical floor of the merge gate (#2627): it is authoritative and overrides
    the count read from the rules. Without it, the count comes from the rules
    alone (pure callers). It must be 0, 1 or 2; without a changed path nothing
    is due, whatever the caller's floor.

    `unknown_authors` names the commits of the freeze whose exact provenance
    (#1465) is unknown: without it, a reviewer's independence cannot be
    established, so the designation waits instead of guessing — an explicit
    state, never a fallback.

    `already` carries the manual reviewers already designated (#2627): the ones
    that prove an independent family cover a seat, and only the missing seats
    are selected. None is ever removed, and two manual reviewers of the same
    family count one seat.

    `fixers` names the designated reviewers whose small first-round correction
    is an authored commit of the head (#2739/#2888): their family is forbidden
    like any author's family, and the exact agent is never designated again.
    A fixer row with an empty family is a **personal** exclusion only
    (`test_only`, #3518, or a rewritten correction, #4457): its family remains
    eligible and another agent of it may be designated.
    `doubts` (#4679 B/P2) names unresolved correction receipts **without any
    lot attachment**: they prove nothing about this head, so they exclude
    nobody; the designation's `why` says them, so the freeze still reports
    what could not be resolved.

    `advances` maps a family to its advance over the plan pace, in points
    (`review_pace`, #2668), or to a `{scope: advance}` mapping whose scoped
    buckets are selected by the candidate's model: the selection prefers the
    family furthest behind, the load only breaking a tie. A family without a
    current reading is neutral, and the pace never makes an eligible family
    ineligible: two required opinions still take two distinct families.

    `held` carries the manual seats that already cover a required opinion but
    must be **asked again** for this exact head (#4582): a seat kept from an
    earlier freeze whose verdict was not carried sleeps until the 2 h SLO when
    nothing wakes it. Only the rows that the selection counts as manual seats
    are mailed; the caller has already excluded the seats whose verdict is on
    the frozen head or reported onto it, and the port still skips a request
    that already names this head (#2495). The designation names them in its
    mark's `sent`, so the exact-head request is auditable.

    The freeze is re-read **once**, and the provided inputs (paths, authors,
    class) are those of this freeze: if the `Port` refuses a freeze that has
    become stale (`StaleFreeze`, nothing was written), `apply` raises
    `FreezeMoved` — designating the new head with the old paths or authors
    would be an implicit admission. The caller recollects the inputs of the new
    freeze, then replays.

    `freeze_sha` names the freeze from which the caller drew these inputs: if it
    is no longer the latest freeze at read time, `apply` also raises
    `FreezeMoved` without writing anything — a freeze that arrived between
    collection and the call never receives the old inputs.
    """
    item = dict(lot_item or {})
    lot = int(item.get("id") or 0)
    if not lot:
        raise DesignationError("lot without id")
    rules = policy if isinstance(policy, (tuple, list)) else rules_of(policy)
    rules = tuple(rules)
    authors = tuple(authors or ())
    for author in authors:
        if not isinstance(author, Mapping) or not str(author.get("name") or "").strip():
            raise DesignationError("an author is {name, family}, not %r" % (author,))
    unknown = [str(value) for value in (unknown_authors or ()) if str(value).strip()]
    freeze = port.latest_freeze(lot)
    if not freeze or not str(freeze.get("sha") or "").strip():
        raise DesignationError("lot #%d is not frozen: no frozen milestone" % lot)
    sha = str(freeze["sha"]).strip().lower()
    if not _SHA.fullmatch(sha):
        raise DesignationError(
            "the freeze of lot #%d names %r, not a commit id" % (lot, freeze["sha"]))
    freeze = {"id": int(freeze["id"]), "sha": sha}
    expected = str(freeze_sha or "").strip().lower()
    if not _SHA.fullmatch(expected):
        raise DesignationError(
            "the inputs of lot #%d name freeze %r, not a commit id" % (lot, freeze_sha))
    if not _same_freeze(expected, sha):
        raise FreezeMoved(
            "the inputs of lot #%d were collected for freeze %s, the last freeze is %s:"
            " nothing written; recollect the paths, authors and class of the new"
            " freeze, then retry" % (lot, expected[:12], sha[:12]))
    paths = tuple(paths or ())
    # Sans chemin changé il n'y a rien à relire, quel que soit le plancher.
    if not paths:
        required = 0
    elif required is None:
        required = required_count(rules, paths)
    if type(required) is not int or required not in (0, 1, 2):
        raise DesignationError("required reviews must be 0, 1 or 2, not %r" % (required,))
    if risk != "light" and required == 0:
        raise DesignationError(
            "review count unknown for a non-light lot (#%d, class %s)" % (lot, risk))
    if required == 0:
        return {"state": NONE, "required": 0, "reviewers": [], "missing": 0,
                "why": ("light lot: no pre-merge reviewer is due (the other-family"
                        " review happens after merge)"), "freeze": freeze}
    existing = port.recorded(lot, freeze["id"])
    if existing and existing.get("state") == DESIGNATED:
        out = dict(existing)
        out["replayed"] = True
        return out
    # #3596: a retry of the same freeze keeps the automatic seats it already
    # named. They count as seats (never re-mailed), so only the missing ones
    # are selected — and a waiting state that gains no seat stays replayable.
    previous = []
    if existing and existing.get("state") == WAITING:
        previous = [{"name": str(row.get("name") or "").strip(),
                     "family": str(row.get("family") or "").strip()}
                    for row in existing.get("reviewers") or ()
                    if isinstance(row, Mapping) and str(row.get("name") or "").strip()]
    previous_names = {row["name"] for row in previous}
    if unknown:
        # A missing author proof forbids proving independence: explicit
        # waiting, notification, no **new** reviewer designated.
        why = ("author provenance unknown for %d commit(s) of the frozen range: %s"
               % (len(unknown), ", ".join(unknown[:5])))
        selection = {"state": WAITING, "required": required,
                     "reviewers": [dict(row) for row in previous],
                     "missing": max(required - len(previous), 0),
                     "manual": [], "why": why}
    else:
        selection = select(required=required, authors=authors, candidates=candidates,
                           families=families, already=already, fixers=fixers,
                           advances=advances, automatic=previous)
    # #4679 B/P2 : un reçu de correctif introuvable **sans rattachement de lot**
    # ne prouve rien sur cette tête. Il n'entre ni dans `fixers` ni dans la
    # porte ; le gel le dit, pour que l'objet absent ne disparaisse pas en
    # silence. Un acteur déjà correcteur de la tête n'est pas répété.
    excluded = {str((row or {}).get("name") or "").strip().casefold()
                for row in fixers or () if isinstance(row, Mapping)}
    reported = []
    for row in doubts or ():
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "").strip()
        sha = str(row.get("sha") or "").strip()
        if name and sha and name.casefold() not in excluded:
            reported.append((name, sha))
    if reported:
        unique = sorted(set(reported))
        selection = dict(selection)
        note = ("%d unresolved correction receipt(s) without a lot attachment"
                " ignored, no exclusion: %s%s"
                % (len(unique),
                   ", ".join("%s %s" % (name, sha[:12]) for name, sha in unique[:4]),
                   ", …" if len(unique) > 4 else ""))
        selection["why"] = ("%s; %s" % (selection["why"], note)
                            if selection.get("why") else note)
    if (existing is not None and existing.get("state") == WAITING
            and selection["state"] == WAITING
            and {str(row.get("name") or "") for row in selection["reviewers"]}
            <= previous_names):
        # The same waiting state of this freeze is already recorded and
        # notified: no second notice, no rewrite. If capacity came back, the
        # selection is a superset and is written below.
        out = dict(existing)
        out["replayed"] = True
        return out
    state = selection["state"]
    manual = [dict(row) for row in selection.get("manual") or ()]
    by_name = {str(row.get("name") or "").strip(): row for row in manual}
    held_seats: list = []
    for row in held or ():
        name = str((row or {}).get("name") if isinstance(row, Mapping)
                   else row or "").strip()
        seat = by_name.get(name)
        if seat is not None and seat not in held_seats:
            held_seats.append(dict(seat))
    if state == DESIGNATED and not selection["reviewers"] and not held_seats:
        # The manual reviewers already cover every required seat (#2627): no
        # automatic reviewer, no mail, no designation mark to write. A held
        # seat whose verdict was not carried still needs its exact-head
        # request, even when no automatic reviewer is missing (#4582).
        return {"state": "manual", "required": required, "reviewers": [],
                "missing": 0, "manual": manual,
                "why": selection["why"], "freeze": freeze}
    messages = []
    if state == WAITING and not existing:
        # One capacity notice per freeze; a retry adds only the seats it can
        # now provide (their request mails), without a second notice.
        messages.append({
            "to": notify,
            "family": "",
            "kind": "notify",
            "body": capacity_message(item, freeze=freeze, required=required,
                                     missing=selection["missing"], why=selection["why"],
                                     designated=selection["reviewers"]),
            "capacity": True,
        })
    first_rank = len(manual) + 1
    # The independent families available on this head: the manual seats and
    # the automatic families, the already designated ones included. Any
    # reviewer correction authors the head and removes its own family from the
    # opinions (canon §2, #2670/#2888).
    eligible_families = len(manual) + len(selection.get("families") or ())
    from . import review_correction

    # #4582: a held manual seat covers its opinion, but its earlier request
    # named another head. The exact-head request is what wakes it; without it
    # the review sleeps until the SLO. Rank follows the seat in the column.
    positions = {str(row.get("name") or "").strip(): index + 1
                 for index, row in enumerate(manual)}
    for seat in held_seats:
        correction = (None if unknown else review_correction.outlook(
            required=required, reviewer=seat, authors=authors,
            candidates=candidates, families=families, seats=already,
            earlier=fixers))
        messages.append({
            "to": seat["name"],
            "family": seat.get("family") or "",
            "kind": REQUEST_KIND,
            "manual": True,
            "body": reviewer_message(item, freeze=freeze, required=required,
                                     rank=positions.get(seat["name"], 1),
                                     reviewer=seat,
                                     authors=authors, base=base, risk=risk,
                                     policy_ref=policy_ref,
                                     eligible_families=eligible_families,
                                     correction=correction),
        })
    for index, reviewer in enumerate(selection.get("reviewers") or ()):
        if str(reviewer.get("name") or "") in previous_names:
            # #3596: an automatic seat of this freeze already holds its request.
            continue
        # #4325: the room this very reviewer would leave as a corrector, read
        # before the mail so it opens with "do not push" when it is too tight.
        correction = (None if unknown else review_correction.outlook(
            required=required, reviewer=reviewer, authors=authors,
            candidates=candidates, families=families, seats=already,
            earlier=fixers))
        messages.append({
            "to": reviewer["name"],
            "family": reviewer["family"],
            "kind": REQUEST_KIND,
            "body": reviewer_message(item, freeze=freeze, required=required,
                                     rank=first_rank + index, reviewer=reviewer,
                                     authors=authors, base=base, risk=risk,
                                     policy_ref=policy_ref,
                                     eligible_families=eligible_families,
                                     correction=correction),
        })
    try:
        return port.commit(lot, freeze=freeze, required=required, state=state,
                           reviewers=selection["reviewers"], missing=selection["missing"],
                           why=selection["why"], authors=authors, policy_ref=policy_ref,
                           notify=notify, messages=tuple(messages),
                           unknown_authors=tuple(unknown))
    except StaleFreeze as exc:
        # Nothing was written. The provided inputs held for the old freeze:
        # recollect them for the new one before replaying (B1, codex review cx2).
        raise FreezeMoved(
            "the freeze of lot #%d moved during the write (was %s): nothing written; "
            "recollect the paths, authors and class of the new freeze, then retry"
            % (lot, sha[:12])) from exc
