# SPDX-License-Identifier: AGPL-3.0-only
"""#8193 : les sujets de lots (tranche 3 de #8191, issue manaty/ameesh#6403).

Un sujet groupe des lots d'un même projet sous un slug court (`pricing-v2`),
créé par `ameesh work subject add` (table `work_subjects`, 0608). Le lot porte
sa référence dans `work_items.subject_id`, distincte de `workstream` (repli de
projet de la garde des modules, 0363).

Une fiche WorkPackage du canon reste un sujet : `work list --subject <id de
fiche>` rend aussi les lots rattachés à la fiche (`package_id`) ou à ses
enfants (`package_parent`), sans copie de la fiche ni PR canon.
"""
from __future__ import annotations

import re

from . import storage
from .db import Db
from .work import WorkError

#: un slug court : minuscules, chiffres et tirets, sans tiret au bord
SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
SLUG_MAX = 63


def _unknown(slug: str, project: str | None) -> WorkError:
    where = " in project %s" % project if project else ""
    return WorkError("unknown subject %r%s: create it first with "
                     "\"ameesh work subject add %s --title …\"" % (slug, where, slug))


def check_slug(slug: str) -> str:
    slug = (slug or "").strip()
    if not SLUG.match(slug) or len(slug) > SLUG_MAX:
        raise WorkError("subject slug %r: lowercase letters, digits and inner dashes, "
                        "at most %d characters (e.g. pricing-v2)" % (slug, SLUG_MAX))
    return slug


def add(db: Db, slug: str, title: str, *, project: str, actor: str = "") -> tuple[dict, bool]:
    """Crée le sujet ; idempotent sur (projet, slug) : (sujet, créé ?)."""
    slug = check_slug(slug)
    title = (title or "").strip()
    if not title:
        raise WorkError("subject %s: --title is required" % slug)
    if not (project or "").strip():
        raise WorkError("subject %s: no project (pass --app <project>)" % slug)
    return storage.of(db).work.subject_add(project.strip(), slug, title, author=actor)


def find(db: Db, slug: str, *, project: str | None = None) -> dict | None:
    """Le sujet `slug` du projet ; sans projet, le seul sujet de ce slug
    (refus s'il est porté par plusieurs projets)."""
    slug = (slug or "").strip()
    if not project and ":" in slug:
        # `<projet>:<slug>` lève l'ambiguïté là où `--app` n'existe pas (work list)
        project, slug = slug.split(":", 1)
    rows = storage.of(db).work.subjects(project=project or None, slug=slug)
    if len(rows) > 1:
        raise WorkError("subject %s exists in several projects (%s): pass --app, or "
                        "<project>:%s" % (slug, ", ".join(row["project"] for row in rows), slug))
    return rows[0] if rows else None


def get(db: Db, slug: str, *, project: str | None = None) -> dict:
    subject = find(db, slug, project=project)
    if subject is None:
        raise _unknown(slug, project)
    return subject


def list_subjects(db: Db, *, project: str | None = None, open_only: bool = False) -> list[dict]:
    return storage.of(db).work.subjects(project=project or None, open_only=open_only)


def close(db: Db, slug: str, *, project: str | None = None, actor: str = "") -> dict:
    subject = get(db, slug, project=project)
    return storage.of(db).work.subject_close(subject["id"], actor=actor) or subject


def for_lot(db: Db, slug: str, *, app: str | None) -> dict:
    """Le sujet auquel rattacher un lot du projet `app` : connu, ouvert, du
    même projet que le lot."""
    subject = get(db, slug, project=(app or "").strip() or None)
    if subject["state"] != "open":
        raise WorkError("subject %s is closed: reopen is not supported, create another "
                        "subject with \"ameesh work subject add\"" % subject["slug"])
    return subject


def attach(db: Db, item_id: int, slug: str | None, *, actor: str = "") -> dict:
    """Rattache un lot existant au sujet `slug` (None : le détache)."""
    from . import work
    item = work.get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    subject = for_lot(db, slug, app=item.get("app")) if slug else None
    row = storage.of(db).work.subject_attach(
        item_id, subject["id"] if subject else None,
        note="subject: %s" % subject["slug"] if subject else "subject: detached",
        actor=actor)
    if row is None:
        raise WorkError("lot %s not found" % item_id)
    return row


def lots(db: Db, ref: str, *, project: str | None = None) -> tuple[dict, list[dict]]:
    """Les lots du sujet `ref` : un slug de `work subject add`, ou l'id d'une
    fiche WorkPackage du canon (lots de la fiche et de ses enfants). Rend
    (description, lots) ; refus si `ref` n'est ni l'un ni l'autre."""
    ref = (ref or "").strip()
    subject = find(db, ref, project=project)
    card = storage.of(db).packages.get(ref)
    if subject is None and card is None:
        raise _unknown(ref, project)
    rows = storage.of(db).work.subject_items(
        subject_id=subject["id"] if subject else None,
        package_id=card["id"] if card else None)
    described = dict(subject) if subject else {"slug": ref, "title": card.get("title") or ""}
    if card is not None:
        described["package"] = card["id"]
    return described, rows


def filtered(db: Db, ref: str, *, state: str | None = None, assignee: str | None = None,
             limit: int | None = None) -> list[dict]:
    """`work list --subject` : les lots du sujet, avec les filtres de `work list`
    (état, assigné, limite), du plus récemment modifié au plus ancien."""
    from . import work
    from .agent_names import resolve as resolve_agent_name
    _, rows = lots(db, ref)
    if state:
        if state in ("merged", "promoted"):
            rows = [r for r in rows if (r.get("legacy_state") or r["state"]) == state]
        else:
            state = work._canonical_state(state)
            rows = [r for r in rows if r["state"] == state]
    if assignee:
        assignee = resolve_agent_name(assignee, db=db, operation="work.list_items")
        rows = [r for r in rows if r.get("assignee") == assignee]
    rows.sort(key=lambda r: float(r.get("updated_ts") or 0), reverse=True)
    return rows if limit is None else rows[:int(limit)]
