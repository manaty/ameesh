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
#: the registry states that forbid designation
UNAVAILABLE_STATUS = frozenset(("stopped", "paused"))
#: the `status_text` prefixes of an operations or budget pause
PAUSE_PREFIXES = ("paused", "budget : ")
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
    registry and from the number of open lots. `model` is only used to choose
    the model-scoped gauges that apply to this reviewer (#2668); it never
    changes the family or the eligibility. The module completes nothing: an
    empty family or a harness outside the known families makes the candidate
    ineligible.
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


def is_available(row: Mapping) -> bool:
    """Availability in the registry: neither stopped, nor paused, nor a human session.

    An agent in a turn remains available: the mail waits for it and opens its
    next turn. Unavailable are a `stopped`/`paused` agent, an `external` session
    (never run by the executor, L46) and an operations or budget pause
    (`status_text` "paused…" or "budget : …"). A row with no readable mode or
    status is refused: doubt designates nobody.
    """
    row = dict(row or {})
    if (row.get("mode") or "execute") != "execute":
        return False
    status = str(row.get("status") or "").strip().lower()
    if not status or status in UNAVAILABLE_STATUS:
        return False
    text = str(row.get("status_text") or "").strip().lower()
    return not any(text.startswith(prefix) for prefix in PAUSE_PREFIXES)


def candidates_from_registry(rows: Iterable[Mapping], *, loads: Optional[Mapping] = None,
                             project: str = "", families) -> list[Candidate]:
    """The candidates of a project: real family, availability, load of open lots.

    `loads` maps a name to the number of open lots (`build`/`qa`); when absent,
    the load is zero. `project` (the lot's `app`) restricts to agents of the
    same team: an ameesh lot is reviewed by an ameesh agent. An empty project
    does not filter — the caller knows what it is doing.
    """
    loads = loads or {}
    wanted = (project or "").strip()
    out = []
    for row in rows or ():
        team = str((row or {}).get("team") or "").strip()
        if wanted and team != wanted:
            continue
        name = str((row or {}).get("name") or "").strip()
        if not name:
            continue
        try:
            load = int(loads.get(name, 0))
        except (TypeError, ValueError):
            load = 0
        row = dict(row or {})
        model = str(row.get("model") or row.get("session_model") or "").strip()
        out.append(Candidate(name=name, family=live_family(row, families=families),
                             available=is_available(row), load=max(load, 0),
                             model=model))
    return out


# --------------------------------------------------------------------------
# the selection: two distinct families, excluding authors and fixers
# --------------------------------------------------------------------------

def families_of(authors: Iterable) -> tuple[set, set]:
    """`(names, families)` forbidden, drawn from the authors of the frozen content.

    An author is `{"name", "family"}`: the lot's assignee and each fixer who
    pushed onto the head (exact evidence #1465). An unknown family is not
    guessed: `select` then waits instead of designating (see
    `unknown_families`).
    """
    names, families = set(), set()
    for author in authors or ():
        row = dict(author or {})
        name = str(row.get("name") or "").strip()
        family = str(row.get("family") or "").strip().lower()
        if name:
            names.add(name)
        if family and family not in PLACEHOLDER_FAMILIES:
            families.add(family)
    return names, families


def unknown_families(authors: Iterable) -> list[str]:
    """The authors whose real family is unknown (empty or a label).

    Without their family, a reviewer's independence cannot be proven: a reviewer
    of the same real family would be designated without anyone knowing."""
    return sorted({str((a or {}).get("name") or "").strip() for a in authors or ()
                   if str((a or {}).get("family") or "").strip().lower()
                   in PLACEHOLDER_FAMILIES})


def manual_seats(already: Iterable, *, authors: Iterable = (),
                 families) -> list:
    """The manual reviewers that can count toward the required opinions.

    A manual reviewer is kept in the column whatever happens (`#2627`); this
    helper only says which ones cover a seat: a known family from the policy,
    distinct from every author's family, distinct from the other manual
    reviewers, and not an author itself. An unknown family, or the author's
    family, does not prove independence: the seat remains to designate.
    """
    names, forbidden = families_of(authors)
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
        if (not name or name in seen_names or name in names
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
           families, already: Iterable = (),
           advances: Optional[Mapping] = None) -> dict:
    """Chooses `required` (1 or 2) independent reviewers, by plan pace then load.

    - each reviewer's family differs from that of **every** author or fixer, and
      for two opinions the two families differ from each other (canon §2);
    - the family comes from the real session (`Candidate.family`), never from
      the name;
    - an unavailable candidate, without a known family, or an author itself is
      discarded;
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
      the families; the selection is deterministic;
    - `already` names the manual reviewers already designated (#2627). The ones
      that prove an independent family cover a seat; only the **missing** seats
      are designated, never a manual reviewer removed or overridden.

    Returns `{state, required, reviewers, missing, manual, why, forbidden}`:
    `waiting` when independent families are missing or an author family is
    unknown, with the missing count — never an implicit admission. `reviewers`
    names only the automatic reviewers of this call; `manual` the manual seats
    that count.
    """
    if type(required) is not int or required not in (0, 1, 2):
        raise DesignationError("required reviews must be 0, 1 or 2, not %r" % (required,))
    names, forbidden = families_of(authors)
    seats = manual_seats(already, authors=authors, families=families)
    out = {"state": NONE, "required": required, "reviewers": [], "missing": 0,
           "manual": seats, "why": "no review required",
           "forbidden": sorted(forbidden)}
    if required == 0:
        return out
    remaining = max(required - len(seats), 0)
    if remaining == 0:
        out.update({"state": DESIGNATED, "missing": 0,
                    "why": "manual reviewer(s) already cover the required reviews"})
        return out
    unknown = unknown_families(authors)
    if unknown:
        # An author with no known real family: independence is not proven,
        # so we wait instead of discarding the only name (canon §2).
        out.update({"state": WAITING, "missing": remaining,
                    "why": "author family unknown: %s" % ", ".join(unknown)})
        return out
    seat_names = {row["name"] for row in seats}
    seat_families = {row["family"] for row in seats}
    eligible = [c for c in candidates or ()
                if c.eligible(forbidden_names=names | seat_names,
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
    out["reviewers"] = [{"name": c.name, "family": c.family} for c in chosen]
    if len(chosen) < remaining:
        out.update({
            "state": WAITING,
            "missing": remaining - len(chosen),
            "reviewers": [],
            "why": ("%d independent %s missing: %d eligible famil%s (%s), "
                    "forbidden %s" % (
                        remaining - len(chosen),
                        "review" if remaining == 1 else "reviews",
                        len(ordered), "y" if len(ordered) == 1 else "ies",
                        ", ".join(sorted(best)) or "none",
                        ", ".join(sorted(forbidden | seat_families)) or "none")),
        })
        return out
    why = "designated %d reviewer(s)" % len(chosen)
    if pace:
        reading = ["%s %+.0f pt" % (c.family.strip().lower(), _advance_of(c, pace) * 100)
                   for c in chosen if c.family.strip().lower() in pace]
        if reading:
            why += " (plan pace: %s)" % ", ".join(reading)
    out.update({"state": DESIGNATED, "why": why})
    return out


# --------------------------------------------------------------------------
# the mails: the exact head, or the missing capacity
# --------------------------------------------------------------------------

def reviewer_message(lot: dict, *, freeze: Mapping, required: int, rank: int,
                     reviewer: Mapping, authors: Iterable = (), base: str = "",
                     risk: str = "", policy_ref: str = "") -> str:
    """The mail that hands the reviewer the exact head and its place.

    The SHA is written in full, with its `frozen` milestone: the verdict will
    have to cite it as-is (`ameesh work milestone <lot> verdict ok --sha <sha>`).
    The reviewer who fixes becomes an author of their fix: their own opinion no
    longer counts (canon §2), the next selection will discard them.
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
        "- read the diff **of this exact head** and check the edge cases;",
        "- render the verdict: `ameesh work milestone %s verdict ok|blocked --sha %s`;"
        % (row.get("id"), sha),
        "- fix a blocking defect yourself and push to the lot branch "
        "(canon §2): you then become the author of your fix, your opinion on the "
        "new head no longer counts, another reviewer takes over;",
        "- a fix longer than ~50 lines, or a design change, goes back to the author;",
        "- in a sensitive class, the second family reviews the lot **with** your fix, "
        "in the same round, distinct from every author;",
        "- your commits carry their exact origin (`ameesh provenance show --commit "
        "<sha>`): by fixing you become an author of the new head;",
        "- a non-blocking defect goes to the backlog, it does not block the merge;",
        "- do not rerun the suites: targeted tests are the author's job, CI "
        "runs on the frozen head once the required opinions are acquired.",
        "",
        "This mail comes from the freeze: if the branch moves, a new freeze cancels "
        "this designation and a new head will be handed to you.",
    ]
    return "\n".join(lines)


def manual_reviewer_message(lot: Mapping, *, freeze: Optional[Mapping] = None,
                            actor: str = "") -> str:
    """The request mail of a manual designation (#705, #2478).

    Unlike the automatic mail, this designation can happen as soon as the lot
    enters `qa`: the head may not be frozen yet. The mail names the latest
    frozen head when one exists and says what to watch otherwise. It is sent as
    a `request` linked to the lot, so the reviewer's runner opens a turn (#2478).
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
    lines += [
        "",
        "To do:",
        "- read the diff of the frozen head and check the edge cases;",
        "- a blocking defect: fix it yourself and push to the lot branch "
        "(canon §2); a fix longer than ~50 lines, or a design change, goes back "
        "to the author;",
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
                     why: str, families: str = "") -> str:
    """The waiting notification: no independent reviewer can be designated.

    It states what is missing — review capacity or author evidence — and asks
    for no admission: the lot remains waiting for the required number of
    opinions.
    """
    row = dict(lot or {})
    return "\n".join([
        "Designation at freeze impossible (canon §2, #1617) — review pending.",
        "",
        "Lot: #%s \"%s\"" % (row.get("id"), " ".join(str(row.get("title") or "").split())),
        "- Freeze: frozen milestone #%s, head %s." % ((freeze or {}).get("id"),
                                                str((freeze or {}).get("sha") or "")[:12]),
        "- Required opinions: %d; missing: %d." % (required, missing),
        "- Reason: %s" % why,
    ] + (["- Available families: %s." % families] if families else []) + [
        "",
        "No implicit admission: the lot remains pending its opinions. Restore the "
        "missing capacity or evidence (another model family in the registry, an "
        "available reviewer, the exact provenance of the commits) then rerun the "
        "designation.",
    ])


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
               notify: str = "", messages: Sequence[Mapping] = ()) -> dict:
        """Writes the freeze's designation in one transaction and returns its shape."""


def apply(lot_item: Mapping, *, policy, paths: Iterable[str], port: Port,
          freeze_sha: str, authors: Iterable = (), candidates: Iterable = (), risk: str = "light",
          base: str = "", policy_ref: str = "", notify: str = "",
          unknown_authors: Iterable = (), families,
          already: Iterable = (), required: Optional[int] = None,
          advances: Optional[Mapping] = None) -> dict:
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

    `advances` maps a family to its advance over the plan pace, in points
    (`review_pace`, #2668), or to a `{scope: advance}` mapping whose scoped
    buckets are selected by the candidate's model: the selection prefers the
    family furthest behind, the load only breaking a tie. A family without a
    current reading is neutral, and the pace never makes an eligible family
    ineligible: two required opinions still take two distinct families.

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
    if unknown:
        # A missing author proof forbids proving independence: explicit
        # waiting, notification, no reviewer designated.
        why = ("author provenance unknown for %d commit(s) of the frozen range: %s"
               % (len(unknown), ", ".join(unknown[:5])))
        selection = {"state": WAITING, "required": required, "reviewers": [],
                     "missing": required, "manual": [], "why": why}
    else:
        selection = select(required=required, authors=authors, candidates=candidates,
                           families=families, already=already, advances=advances)
    if selection["state"] == WAITING and existing:
        # The waiting state of this freeze is already recorded and notified: we
        # do not send a second one, but the selection was retried — if capacity
        # came back, the branch below finally designates.
        out = dict(existing)
        out["replayed"] = True
        return out
    state = selection["state"]
    if state == DESIGNATED and not selection["reviewers"]:
        # The manual reviewers already cover every required seat (#2627): no
        # automatic reviewer, no mail, no designation mark to write.
        return {"state": "manual", "required": required, "reviewers": [],
                "missing": 0, "manual": [dict(row) for row in selection["manual"]],
                "why": selection["why"], "freeze": freeze}
    messages = []
    if state == DESIGNATED:
        first_rank = len(selection.get("manual") or ()) + 1
        for rank, reviewer in enumerate(selection["reviewers"], start=first_rank):
            messages.append({
                "to": reviewer["name"],
                "family": reviewer["family"],
                "kind": REQUEST_KIND,
                "body": reviewer_message(item, freeze=freeze, required=required,
                                         rank=rank, reviewer=reviewer, authors=authors,
                                         base=base, risk=risk, policy_ref=policy_ref),
            })
    else:
        messages.append({
            "to": notify,
            "family": "",
            "kind": "notify",
            "body": capacity_message(item, freeze=freeze, required=required,
                                     missing=selection["missing"], why=selection["why"]),
            "capacity": True,
        })
    try:
        return port.commit(lot, freeze=freeze, required=required, state=state,
                           reviewers=selection["reviewers"], missing=selection["missing"],
                           why=selection["why"], authors=authors, policy_ref=policy_ref,
                           notify=notify, messages=tuple(messages))
    except StaleFreeze as exc:
        # Nothing was written. The provided inputs held for the old freeze:
        # recollect them for the new one before replaying (B1, codex review cx2).
        raise FreezeMoved(
            "the freeze of lot #%d moved during the write (was %s): nothing written; "
            "recollect the paths, authors and class of the new freeze, then retry"
            % (lot, sha[:12])) from exc
