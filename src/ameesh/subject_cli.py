# SPDX-License-Identifier: AGPL-3.0-only
"""#8193 : `ameesh work subject add|list|show|close|attach` (sujets de lots).

Gardées hors de `mesh_cli` (qui les branche), comme `plan_cli`. Le domaine
est `work_subjects`."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import work_subjects

COMMANDS = ("subject",)


def add_parsers(work_sub, func) -> None:
    """Ajoute `subject` à `ameesh work`."""
    p_subject = work_sub.add_parser(
        "subject", help="#8193: subjects grouping lots of a project (add, list, show, "
                        "close, attach)")
    sub = p_subject.add_subparsers(dest="subject_command", required=True)
    p_add = sub.add_parser("add", help="create a subject (idempotent on the slug)")
    p_add.add_argument("slug", help="short slug, unique per project (e.g. pricing-v2)")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--app", default=None,
                       help="project; default: the creating agent's project")
    p_add.add_argument("--actor", default="")
    p_add.add_argument("--json", action="store_true")
    p_list = sub.add_parser("list", help="the subjects")
    p_list.add_argument("--open", action="store_true", help="open subjects only")
    p_list.add_argument("--app", default=None, help="only this project's subjects")
    p_list.add_argument("--json", action="store_true")
    p_show = sub.add_parser("show", help="a subject and its lots (state, assignee, next wake)")
    p_show.add_argument("slug")
    p_show.add_argument("--app", default=None)
    p_show.add_argument("--json", action="store_true")
    p_close = sub.add_parser("close", help="close a subject (its lots are untouched)")
    p_close.add_argument("slug")
    p_close.add_argument("--app", default=None)
    p_close.add_argument("--actor", default="")
    p_attach = sub.add_parser("attach", help="attach an existing lot to a subject of its "
                                             "project, or detach it with --none")
    p_attach.add_argument("id", type=int)
    p_attach.add_argument("slug", nargs="?", default=None)
    p_attach.add_argument("--none", action="store_true", help="detach the lot from its subject")
    p_attach.add_argument("--actor", default="")
    for parser in (p_add, p_list, p_show, p_close, p_attach):
        parser.set_defaults(func=func)


def _ts(value) -> str:
    if not value:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(value)))


def _subject_line(row: dict) -> str:
    return "%-20s %-12s %-7s %-16s %s" % (
        row["slug"][:20], (row.get("project") or "—")[:12], row["state"],
        _ts(row.get("created_ts")), row["title"])


def lot_line(row: dict) -> str:
    """Une ligne de lot d'un sujet : état, assigné, prochain réveil."""
    return "%-6s %-14s %-18s %-16s %s" % (
        "#%d" % int(row["id"]), row["state"], (row.get("assignee") or "—")[:18],
        _ts(row.get("wake_ts")), (row.get("title") or "")[:60])


LOT_HEADER = "%-6s %-14s %-18s %-16s %s" % ("ID", "STATE", "ASSIGNEE", "NEXT WAKE", "TITLE")


def _lot_view(row: dict) -> dict:
    return {key: row.get(key) for key in
            ("id", "title", "type", "state", "assignee", "app", "wake_ts",
             "subject_id", "package_id")}


def run(cfg, db, args: argparse.Namespace) -> int:
    command = args.subject_command
    if command == "add":
        from . import work_projects
        project = work_projects.creation_project(cfg, db, args.actor, args.app)
        subject, created = work_subjects.add(db, args.slug, args.title, project=project,
                                             actor=args.actor)
        if args.json:
            print(json.dumps({"created": created, "subject": subject},
                             ensure_ascii=False, indent=2, default=str))
        elif created:
            print("subject %s created in %s: %s" % (subject["slug"], subject["project"],
                                                   subject["title"]))
        else:
            print("subject %s already exists in %s (%s): %s"
                  % (subject["slug"], subject["project"], subject["state"], subject["title"]))
        return 0
    if command == "list":
        rows = work_subjects.list_subjects(db, project=args.app, open_only=args.open)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            return 0
        if not rows:
            print("no subjects")
            return 0
        print("%-20s %-12s %-7s %-16s %s" % ("SLUG", "PROJECT", "STATE", "CREATED", "TITLE"))
        for row in rows:
            print(_subject_line(row))
        return 0
    if command == "show":
        subject = work_subjects.get(db, args.slug, project=args.app)
        _, lots = work_subjects.lots(db, subject["slug"], project=subject["project"])
        if args.json:
            print(json.dumps({"subject": subject, "lots": [_lot_view(r) for r in lots]},
                             ensure_ascii=False, indent=2, default=str))
            return 0
        print("subject  : %s (%s) — %s" % (subject["slug"], subject["project"], subject["title"]))
        print("state    : %s   author: %s   created: %s%s" % (
            subject["state"], subject.get("author") or "—", _ts(subject.get("created_ts")),
            "   closed: %s by %s" % (_ts(subject.get("closed_ts")), subject.get("closed_by") or "—")
            if subject["state"] == "closed" else ""))
        if not lots:
            print("no lots")
            return 0
        print(LOT_HEADER)
        for row in lots:
            print(lot_line(row))
        return 0
    if command == "close":
        subject = work_subjects.close(db, args.slug, project=args.app, actor=args.actor)
        print("subject %s (%s) closed" % (subject["slug"], subject["project"]))
        return 0
    # attach
    if args.none == bool(args.slug):
        print("error: a subject slug OR --none", file=sys.stderr)
        return 2
    row = work_subjects.attach(db, args.id, None if args.none else args.slug, actor=args.actor)
    print("lot #%d %s" % (row["id"], "attached to subject %s" % args.slug
                          if args.slug else "detached from its subject"))
    return 0
