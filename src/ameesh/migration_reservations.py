# SPDX-License-Identifier: AGPL-3.0-only
"""L160g: repository-scoped reservation probe and legacy Markdown import.

Import requires an explicit migration-number to ameesh-lot mapping. Legacy
labels and issue numbers are never interpreted as registry lot identifiers.
"""
from __future__ import annotations

import os
import re

from . import migrations, storage, work
from .canon import GitError

_NUMBER = re.compile(r"^([0-9]{3,4})_[^/]+\.sql$")
_RESERVED = re.compile(r"\b(?:réservé|réservée|réservation|reserved)\b", re.IGNORECASE)
_INACTIVE = re.compile(r"\b(?:fusionné|fusionnée|fusion|merged|libéré|libérée|released|annulé|annulée|cancelled|canceled)\b", re.IGNORECASE)


def probe(db, project, branches) -> list[dict]:
    """Report SQL files added by a lot without that lot's reservation.

    Existing files in the common base are historical, so are not reported.
    No writes and no fetch; holders are checked in the project's repository.
    An unreadable registry produces a radar error, never a clean report.
    """
    if not project.migrations:
        return []
    from . import radar
    slug = migrations.repo_key(radar.github_slug(project))
    if not slug:
        raise GitError("project %s: migration reservation probe needs a GitHub repository" % project.name)
    found, holders = [], {}
    try:
        registry = storage.of(db).work
        for branch in branches:
            for path in branch.get("added") or ():
                if os.path.dirname(path) != project.migrations.rstrip("/"):
                    continue
                match = _NUMBER.fullmatch(os.path.basename(path))
                if not match:
                    continue
                number = int(match.group(1))
                if number not in holders:
                    holders[number] = registry.migration_holder(number, slug)
                holder = holders[number]
                if holder and int(holder["id"]) == int(branch["work_item"]):
                    continue
                found.append({"work_item": branch["work_item"], "target": branch["target"],
                              "path": path, "number": match.group(1), "repo": slug,
                              "holder": holder["id"] if holder else None})
    except Exception as exc:
        raise GitError("migration reservation registry unreadable: %s" % exc) from exc
    return found


def parse_markdown(text: str) -> list[dict]:
    """Read active rows from a Number | Lot | Who | State Markdown table.

    English and the legacy French statuses are accepted. Unknown statuses
    are refused for numbered rows, rather than silently dropping intentions.
    """
    rows, seen = [], set()
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip().replace("**", "").replace("`", "")
                 for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if not cells or not re.fullmatch(r"[0-9]{1,4}", cells[0]):
            continue
        if len(cells) != 4 or not 1 <= int(cells[0]) <= 9999:
            raise work.WorkError("RESERVATIONS.md line %s: expected number | lot | who | state" % line_number)
        number = int(cells[0])
        if _INACTIVE.search(cells[3]):
            continue
        if not _RESERVED.search(cells[3]):
            raise work.WorkError("RESERVATIONS.md line %s: unknown reservation status %r" % (line_number, cells[3]))
        if number in seen:
            raise work.WorkError("RESERVATIONS.md: duplicate active reservation %s" % number)
        seen.add(number)
        rows.append({"number": number, "label": cells[1], "who": cells[2], "status": cells[3]})
    return rows


def import_reservations(db, rows, mapping: dict, *, repo: str, apply=False,
                        actor="", cfg=None) -> list[dict]:
    """Preview by default; apply a complete, validated mapping atomically.

    Row locks prevent concurrent reservations of the same lot from being
    overwritten. The existing unique index arbitrates repository/number
    collisions. Unlike a new reservation, this records a legacy intention
    whose SQL may already exist in a branch. No SQL migrations are executed.
    """
    slug = migrations.repo_key(repo)
    if not slug or migrations.layout_for(slug, cfg) is None:
        raise work.WorkError("repository %s: migrations layout unknown" % slug)
    if not isinstance(mapping, dict) or any(
            not re.fullmatch(r"[0-9]{1,4}", str(n)) or not isinstance(lot, int)
            or isinstance(lot, bool) for n, lot in mapping.items()):
        raise work.WorkError("mapping must be a JSON object of migration numbers to integer lot ids")
    try:
        mapped = {int(n): int(lot) for n, lot in mapping.items()}
    except (TypeError, ValueError, AttributeError) as exc:
        raise work.WorkError("mapping must be a JSON object of migration numbers to lot ids") from exc
    numbers = {r["number"] for r in rows}
    if len(mapped) != len(mapping) or len(numbers) != len(rows) or set(mapped) - numbers:
        raise work.WorkError("mapping contains inactive or unknown migration numbers, or duplicate rows")
    if any(lot <= 0 for lot in mapped.values()) or len(set(mapped.values())) != len(mapped):
        raise work.WorkError("each reservation needs a distinct positive lot id")

    def preview(conn):
        registry, result = storage.of(conn).work, []
        for row in rows:
            number, lot = row["number"], mapped.get(row["number"])
            state = "unmapped"
            if lot is not None:
                item = work.get(conn, lot)
                if not item or item["state"] in work.MERGED_STATES or item["state"] == "closed":
                    raise work.WorkError("lot #%s is missing or closed" % lot)
                if work.lot_repository(item, cfg) != slug:
                    raise work.WorkError("lot #%s belongs to another repository" % lot)
                current = item.get("migration")
                if current is not None and (current != number or item.get("migration_repo") != slug):
                    raise work.WorkError("lot #%s already has a different reservation" % lot)
                holder = registry.migration_holder(number, slug)
                if holder and int(holder["id"]) != lot:
                    raise work.WorkError("migration %s of %s already held by lot #%s" % (number, slug, holder["id"]))
                state = "existing" if current is not None else "reserve"
            result.append(dict(row, lot=lot, action=state))
        return result

    if not apply:
        return preview(db)
    if numbers - set(mapped):
        raise work.WorkError("apply needs an explicit lot mapping for every active reservation")
    with db.transaction() as tx:
        for lot in sorted(mapped.values()):
            tx.query("SELECT id FROM work_items WHERE id = %s FOR UPDATE", (lot,))
        result = preview(tx)
        for row in result:
            if row["action"] == "existing":
                continue
            storage.of(tx).work.reserve_migration(
                row["lot"], slug, row["number"], 0, actor=actor,
                note="legacy RESERVATIONS.md import: migration %(number)s reserved (" + slug + ")")
            row["action"] = "reserved"
        return result
