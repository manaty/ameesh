# SPDX-License-Identifier: AGPL-3.0-only
"""Host-side admission source of a runner (#3925, manaty/ameesh#2975).

`agent-runner --agents-file FILE`: the list of agents this runner may claim
lives in a file owned by the host operator, re-read while the runner runs.
An agent written into it becomes claimable on the next pass, without a
restart; an agent taken out of it is no longer claimed, and its worker
releases the lease once its current turn is over.

The file only narrows: eligibility still comes from the registry and the
governed canon (Agent and Placement cards, `canon_claim_predicate_sql`), and
the host's `max_agents`, gate and account checks still apply at claim time.

Format: one name per line (or comma-separated), `#` starts a comment, blank
lines are ignored. An invalid entry is skipped with its reason; it never
stops the runner nor its other agents. A file that disappears or cannot be
read keeps the last list that was read.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .config import NAME_RE


@dataclass
class Parsed:
    names: list[str] = field(default_factory=list)
    #: `(line number, entry, reason)` for each skipped entry
    problems: list[tuple[int, str, str]] = field(default_factory=list)


def parse(text: str) -> Parsed:
    """Names of an admission file, in order, without duplicates."""
    out = Parsed()
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0]
        for entry in (e.strip() for e in line.split(",")):
            if not entry:
                continue
            if entry == "all" or not NAME_RE.match(entry):
                out.problems.append((number, entry, "not an agent name"))
                continue
            if entry not in seen:
                seen.add(entry)
                out.names.append(entry)
    return out


def stamp(path: str) -> tuple | None:
    """What says the file changed: `(mtime_ns, size, inode)`, None if absent."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def read(path: str) -> Parsed:
    """Reads and parses the file; `OSError`/`UnicodeDecodeError` propagate."""
    with open(path, encoding="utf-8") as fh:
        return parse(fh.read())
