# SPDX-License-Identifier: AGPL-3.0-only
"""#8197 : attente nommée et inventaire des suivis (tranche 6 de #8191, issue
manaty/ameesh#6403, écart 5).

Un suivi (`follow-up`) en attente reprend l'état `blocked` du cycle de vie,
sans nouvel état ni colonne : `ameesh work wait <id> --on <qui>` le déplace en
`blocked` avec la note `waiting on: <qui>`. `waiting_on` est la dernière de ces
notes, `since` l'entrée dans l'attente courante (lus dans le journal du lot).
`work wait <id> --clear`, ou une note de qui que ce soit, le rouvre (`intake`) ;
une note qui arme un réveil le laisse en attente (on attend encore, on
contrôlera plus tard), une note automatique de la mécanique (`ameesh`) aussi.

`ameesh work list --assignee <agent> --type follow-up [--by-subject] [--json]`
rend les suivis ouverts de l'agent, groupés par sujet (#8193), en une lecture.
"""
from __future__ import annotations

import time

from . import storage
from .db import Db
from .work import SYSTEM_SENDER, WorkError

FOLLOW_UP = "follow-up"
#: la note d'attente ; `waiting_on` est le texte qui la suit
WAITING_PREFIX = "waiting on: "
#: une attente nomme une personne ou un lot, pas un paragraphe
WAITING_ON_MAX = 200
#: l'auteur des notes automatiques de la mécanique, qui ne rouvrent rien
MECHANICS_ACTOR = SYSTEM_SENDER
#: le groupe des suivis sans sujet
NO_SUBJECT = "sans sujet"


def _follow_up(db: Db, item_id: int) -> dict:
    from . import work
    item = work.get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item.get("type") != FOLLOW_UP:
        raise WorkError("lot %s is a %s: work wait is reserved to follow-up lots; a "
                        "development lot waits with ameesh work move %s blocked"
                        % (item_id, item.get("type"), item_id))
    if item["state"] == "closed":
        raise WorkError("follow-up %s is closed: nothing to wait for" % item_id)
    return item


def wait(db: Db, item_id: int, on: str, *, actor: str = "",
         wake_ts: float | None = None) -> dict:
    """Met le suivi en attente de `on` ; déjà en attente, change qui est
    attendu sans déplacer `since`. `wake_ts` arme en plus le prochain contrôle."""
    from . import work
    on = " ".join((on or "").split())
    if not on:
        raise WorkError("work wait: --on names who or what is awaited (a person, a lot)")
    if len(on) > WAITING_ON_MAX:
        raise WorkError("work wait: --on is at most %d characters (who is awaited, "
                        "not the story: write it with ameesh work note)" % WAITING_ON_MAX)
    item = _follow_up(db, item_id)
    note = WAITING_PREFIX + on
    if item["state"] == "blocked":
        storage.of(db).work.note(item_id, "blocked", note, actor)
    else:
        work.move(db, item_id, "blocked", note=note, actor=actor)
    if wake_ts is not None:
        work.schedule_wake(db, item_id, wake_ts, text=note, actor=actor)
    return work.get(db, item_id)


def clear(db: Db, item_id: int, *, actor: str = "", why: str = "") -> dict:
    """Rouvre le suivi en attente (`blocked` → `intake`)."""
    from . import work
    item = _follow_up(db, item_id)
    if item["state"] != "blocked":
        raise WorkError("follow-up %s is not waiting (state %s)" % (item_id, item["state"]))
    why = " ".join((why or "").split())
    return work.move(db, item_id, "intake",
                     note="waiting cleared" + (": " + why if why else ""), actor=actor)


def reopens(item: dict, actor: str, *, wake: bool) -> bool:
    """Une note rouvre-t-elle ce suivi en attente ? Toute note sans réveil armé,
    quel qu'en soit l'auteur : la réponse attendue arrive souvent hors du mesh,
    par la veille GitHub ou un autre agent (pilote, 24/11). Jamais une note
    automatique de la mécanique (`MECHANICS_ACTOR`)."""
    if item.get("type") != FOLLOW_UP or item.get("state") != "blocked" or wake:
        return False
    return (actor or "").strip() not in ("", MECHANICS_ACTOR)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def entry(row: dict) -> dict:
    """La ligne d'inventaire d'un suivi."""
    waiting = row["state"] == "blocked"
    note = row.get("waiting_note") or ""
    waiting_on = note[len(WAITING_PREFIX):] if note.startswith(WAITING_PREFIX) else None
    since = row.get("waiting_since_ts") if waiting else None
    return {
        "id": row["id"],
        "title": row["title"],
        "state": "waiting" if waiting else "open",
        # un `blocked` posé par `work move` (sans `work wait`) attend quelqu'un
        # que le journal ne nomme pas
        "waiting_on": waiting_on if waiting else None,
        "since": _iso(since),
        "since_ts": since,
        "next_wake": _iso(row.get("wake_ts")),
        "next_wake_ts": row.get("wake_ts"),
        "subject": row.get("subject_slug"),
        "issue_ref": row.get("issue_ref"),
        "app": row.get("app"),
        "updated_ts": row.get("updated_ts"),
    }


def inventory(db: Db, assignee: str) -> dict:
    """Les suivis ouverts de l'agent groupés par sujet (slug), les suivis sans
    sujet en dernier sous « sans sujet »."""
    from .agent_names import resolve as resolve_agent_name
    if not (assignee or "").strip():
        raise WorkError("the follow-up inventory is per agent: pass --assignee <agent>")
    assignee = resolve_agent_name(assignee.strip(), db=db, operation="work.list_items")
    rows = storage.of(db).work.follow_ups(assignee, waiting_prefix=WAITING_PREFIX)
    groups: dict = {}
    for row in rows:
        key = row.get("subject_slug")
        group = groups.setdefault(key, {
            "subject": key,
            "title": row.get("subject_title") if key else NO_SUBJECT,
            "github_ref": row.get("subject_github_ref") if key else None,
            "follow_ups": []})
        group["follow_ups"].append(entry(row))
    ordered = sorted(groups.values(), key=lambda g: (g["subject"] is None, g["subject"] or ""))
    return {"assignee": assignee, "count": len(rows),
            "waiting": sum(1 for row in rows if row["state"] == "blocked"),
            "subjects": ordered}


def _age(ts: float, now: float) -> str:
    from . import plan
    return plan.describe_age(max(0.0, now - float(ts)))


def _line(item: dict, now: float) -> str:
    parts = []
    if item["state"] == "waiting":
        who = item["waiting_on"] or "?"
        parts.append("waiting on %s" % who
                     + (" for %s" % _age(item["since_ts"], now) if item["since_ts"] else ""))
    else:
        parts.append("open")
    if item["next_wake_ts"]:
        parts.append("next check %s" % time.strftime(
            "%Y-%m-%d %H:%M %Z", time.localtime(item["next_wake_ts"])))
    else:
        parts.append("no check scheduled")
    if item["issue_ref"]:
        parts.append(item["issue_ref"])
    return "  #%d %s\n      %s" % (item["id"], item["title"], " · ".join(parts))


def render(data: dict, *, by_subject: bool = True, now: float | None = None) -> str:
    """Le rendu texte de l'inventaire, pour l'humain."""
    now = time.time() if now is None else now
    if not data["count"]:
        return "no open follow-up for %s" % data["assignee"]
    lines = ["%d open follow-up(s) for %s, %d waiting" % (
        data["count"], data["assignee"], data["waiting"])]
    if not by_subject:
        items = sorted((i for g in data["subjects"] for i in g["follow_ups"]),
                       key=lambda i: i["id"])
        lines.extend(_line(item, now) for item in items)
        return "\n".join(lines)
    for group in data["subjects"]:
        if group["subject"]:
            head = "%s — %s" % (group["subject"], group["title"])
            if group["github_ref"]:
                head += " (%s)" % group["github_ref"]
        else:
            head = NO_SUBJECT
        lines.append("")
        lines.append("%s (%d)" % (head, len(group["follow_ups"])))
        lines.extend(_line(item, now) for item in group["follow_ups"])
    return "\n".join(lines)
