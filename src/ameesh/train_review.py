# SPDX-License-Identifier: AGPL-3.0-only
"""Pure historical-review qualification for #1467.

There is deliberately no database/registry fallback here. A trusted history
provider must establish the *exact* commit-to-turn and milestone-to-turn
links before constructing Receipt objects. Proximity in time, a Git author
name, an assignee or a note claiming a model are not such links. This module
checks subject identity and independent coverage, not the truth of a supplied
provenance record. It is not exposed as a CLI taking user-supplied receipts.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from itertools import combinations

from .train import TrainError

SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
FAMILIES = {"claude", "codex", "deepseek"}
MARK_FIELDS = ("id", "actor", "at_ts", "sha", "verdict")


@dataclass(frozen=True)
class Receipt:
    subject: str                 # commit:<full sha> or milestone:<id>
    commit: str                  # resolved commit, including short mark SHA
    actor: str
    session: str
    turn: str
    harness: str
    record: str                  # exact provenance record, never free-form note
    milestone: dict | None = None

    def known(self):
        return (all(isinstance(value, str) and value.strip() for value in (
            self.subject, self.commit, self.actor, self.session, self.turn, self.record))
                and SHA.fullmatch(self.commit) is not None and self.harness in FAMILIES)


def _actor(value):
    return str(value or "").strip().casefold()


def _order(mark):
    try:
        stamp, number = float(mark["at_ts"]), int(mark["id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise TrainError("unreadable verdict identity or date") from exc
    if not math.isfinite(stamp) or number <= 0 or not _actor(mark.get("actor")):
        raise TrainError("unreadable verdict identity or date")
    return stamp, number


def qualify(head: str, commits: list[str], marks: list[dict],
            history: dict[str, Receipt | None], *, required: int) -> dict:
    """Cover every content commit with independent final-head verdicts.

    The caller supplies the complete Git range and unbounded milestone read,
    and the policy's required count (0/1/2, after release/doubt/domain guards).
    A correcting reviewer is an author of their commit. A verdict can cover
    other authors' commits, never that reviewer's own work or model family.
    Two reviews are from distinct families for *each* author independently.

    A blocked verdict remains in force across refreezes until the same actor
    gives a later OK on this head. Another reviewer's OK cannot clear it.
    Nonblocking findings belong to the backlog; the caller must expose every
    B/P1 (and ameesh sensitive P2) as blocked, not hide it in prose.
    """
    if type(required) is not int or required not in (0, 1, 2):
        raise TrainError("explicit review requirement expected")
    if not SHA.fullmatch(head) or not commits or len(set(commits)) != len(commits) \
            or any(not SHA.fullmatch(commit) for commit in commits):
        raise TrainError("complete exact content-commit range expected")
    authors = []
    for commit in commits:
        subject = "commit:" + commit
        receipt = history.get(subject)
        if not isinstance(receipt, Receipt) or not receipt.known() \
                or receipt.subject != subject or receipt.commit != commit:
            raise TrainError("unknown historical author for commit %s" % commit[:12])
        authors.append(receipt)

    latest, blocked = {}, {}
    seen_ids = set()
    for mark in sorted((m for m in marks if m.get("kind") == "verdict"), key=_order):
        actor = _actor(mark["actor"])
        if mark["id"] in seen_ids or mark.get("verdict") not in ("ok", "blocked"):
            raise TrainError("ambiguous verdict facts")
        seen_ids.add(mark["id"])
        latest[actor] = mark
        if mark["verdict"] == "blocked":
            blocked[actor] = mark
    accepted = []
    for actor, mark in sorted(latest.items()):
        if mark["verdict"] != "ok":
            continue
        subject = "milestone:%s" % mark["id"]
        receipt = history.get(subject)
        if not isinstance(receipt, Receipt) or not receipt.known():
            continue  # an unknown reviewer supplies no independent vote
        if receipt.subject != subject or _actor(receipt.actor) != actor \
                or receipt.milestone != {key: mark.get(key) for key in MARK_FIELDS}:
            raise TrainError("historical verdict receipt names different facts")
        if receipt.commit != head:
            continue
        # The exact-head proof must also resolve the SHA the mark actually
        # cites; the provider may resolve an unambiguous Git abbreviation.
        raw = str(mark.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{7,64}", raw) or not head.startswith(raw):
            raise TrainError("historical verdict receipt resolves another commit")
        accepted.append(receipt)
        if actor in blocked and _order(mark) > _order(blocked[actor]):
            del blocked[actor]
    if blocked:
        raise TrainError("unresolved blocking verdicts: %s" % ", ".join(sorted(blocked)))
    coverage = {}
    for author in authors:
        candidates = [r for r in accepted if _actor(r.actor) != _actor(author.actor)
                      and r.harness != author.harness and r.session != author.session]
        chosen = next((group for group in combinations(candidates, required)
                       if len({r.harness for r in group}) == required
                       and len({r.session for r in group}) == required), None)
        if chosen is None:
            raise TrainError("missing independent final-head reviews for commit %s (%s)"
                             % (author.commit[:12], author.actor))
        coverage[author.commit] = [r.subject for r in chosen]
    return dict(schema="ameesh-train-reviews/1", head=head, required=required,
                authors=[asdict(r) for r in authors], reviews=[asdict(r) for r in accepted],
                coverage=coverage)
