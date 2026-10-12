# SPDX-License-Identifier: AGPL-3.0-only
"""Sous-commandes `ameesh work` du plan de travail (lot L29).

  ameesh work link <id> <fiche>|--none
  ameesh work close <id> --abandoned | --superseded-by <id> [--note …]
  ameesh work sync-merges --git-dir D [--target origin/main] [--repo R] [--dry-run] [--json]
  ameesh work sync-branches [--host H | --all-hosts] [--dry-run] [--json]  (L118 : branche
                            ou commit de fusion qui désigne le lot)
  ameesh work sync-github --repo R [--limit N] [--dry-run] [--json]
  ameesh work project-github --repo R [--dry-run] [--canon-url URL] [--json]
  ameesh work project-github --app P [--repo R] [--dry-run] [--json]   (L126)
  ameesh work plan <id|fiche> [--debut J] [--fin J] [--livraison J] [--source S]  (L96)
                  [--estimate 90m] [--estimate-source S]                (L157, une tâche)
  ameesh work estimates [--app P] [--json]   (L157 : écarts réel/estimé, pour l'auditeur)

Gardées hors de `mesh_cli` (qui les branche) pour que leurs évolutions n'y
touchent pas. Voir `plan_github` pour les règles de GitHub (vue, jamais
source ; lecture seule pour `sync-github`).
"""
from __future__ import annotations

import argparse
import json
import sys

from . import plan_git, plan_github, work

COMMANDS = ("link", "close", "sync-merges", "sync-branches", "sync-github", "project-github",
            "plan", "estimates", "schedule", "measure")


def add_parsers(work_sub, func) -> None:
    """Ajoute les sous-commandes du plan à `ameesh work`."""
    p_link = work_sub.add_parser("link", help="link a work item to a WorkPackage (L29)")
    p_link.add_argument("id", type=int)
    p_link.add_argument("package", nargs="?", default=None, help="package id")
    p_link.add_argument("--none", action="store_true", help="detach the work item from the plan")
    p_link.add_argument("--actor", default="")
    p_link.set_defaults(func=func)

    p_close = work_sub.add_parser(
        "close", help="close an abandoned or superseded work item (closed milestone, L29)")
    p_close.add_argument("id", type=int)
    how = p_close.add_mutually_exclusive_group(required=True)
    how.add_argument("--abandoned", action="store_true", help="the work item will not be done")
    how.add_argument("--superseded-by", type=int, default=None, metavar="ID",
                     help="the work item is superseded by this one")
    p_close.add_argument("--note", default="")
    p_close.add_argument("--actor", default="")
    p_close.set_defaults(func=func)

    p_merges = work_sub.add_parser(
        "sync-merges", help="close the work items whose frozen content is merged (git, L29)")
    p_merges.add_argument("--git-dir", default=".", help="local clone (read-only)")
    p_merges.add_argument("--target", default=plan_git.DEFAULT_TARGET,
                          help="target branch (default origin/main; no fetch)")
    p_merges.add_argument("--repo", default=None,
                          help="owner/repo: failing proof by content, the linked PR")
    p_merges.add_argument("--dry-run", action="store_true", help="report without closing")
    p_merges.add_argument("--json", action="store_true")
    p_merges.set_defaults(func=func)

    p_branches = work_sub.add_parser(
        "sync-branches", help="close the work items whose merge into their target is observed, "
                              "with or without a PR: work item branch, or merge commit "
                              "carrying \"ameesh-work: <id>\" (or #<id> if git config "
                              "ameesh.lotRef hash); L118; also done by the runner")
    p_branches.add_argument("--host", default=None,
                            help="host whose assignees are examined (default: this host)")
    p_branches.add_argument("--all-hosts", action="store_true",
                            help="all assignees (their directories must be here)")
    p_branches.add_argument("--dry-run", action="store_true", help="report without closing")
    p_branches.add_argument("--actor", default="")
    p_branches.add_argument("--json", action="store_true")
    p_branches.set_defaults(func=func)

    p_sync = work_sub.add_parser(
        "sync-github", help="close the work items whose PR is merged (GitHub read-only)")
    p_sync.add_argument("--repo", required=True, help="owner/repo")
    p_sync.add_argument("--limit", type=int, default=200, help="merged PRs read (default 200)")
    p_sync.add_argument("--dry-run", action="store_true", help="report without closing")
    p_sync.add_argument("--json", action="store_true")
    p_sync.set_defaults(func=func)

    p_plan = work_sub.add_parser(
        "plan", help="planned dates of a task or a WorkPackage (L96)")
    p_plan.add_argument("target", help="task number, or package id")
    p_plan.add_argument("--debut", "--start", dest="debut", default=None,
                        help="planned start: YYYY-MM-DD, tomorrow, monday...; - clears")
    p_plan.add_argument("--fin", "--end", dest="fin", default=None, help="planned end")
    p_plan.add_argument("--livraison", "--delivery", dest="livraison",
                        default=None, help="planned delivery or deployment")
    p_plan.add_argument("--source", default=None,
                        help="where the date comes from (conversation, decision, message...); "
                             "also that of the estimate, failing --estimate-source")
    p_plan.add_argument("--estimate", default=None, metavar="DURATION",
                        help="estimated duration of a task: 90m, 2h, 1h30, 1,5h, 2d (L157)")
    p_plan.add_argument("--estimate-source", default=None, metavar="SOURCE",
                        help="where the estimate comes from (design, history...)")
    p_plan.add_argument("--actor", default="")
    p_plan.add_argument("--json", action="store_true")
    p_plan.set_defaults(func=func)

    p_est = work_sub.add_parser(
        "estimates", help="actual/estimated gaps of delivered work items, by work item type and by "
                          "estimate author (median, p80); L157, for the auditor")
    p_est.add_argument("--app", default=None, metavar="PROJECT", help="one project only")
    p_est.add_argument("--json", action="store_true", help="schema ameesh-estimates/1")
    p_est.add_argument("--publish", action="store_true",
                       help="auditor pass (L157b): computes and publishes the duration "
                            "reference table (schema ameesh-estimate-reference/1), which "
                            "work add and work show read; clears estimate_bias")
    p_est.add_argument("--actor", default="", help="who publishes (--publish)")
    p_est.set_defaults(func=func)

    p_sched = work_sub.add_parser(
        "schedule", help="planned dates of the open assigned work items at the p80 of the "
                         "reference table, queues ordered at the median (L157c); shows, "
                         "--apply writes")
    p_sched.add_argument("--assignee", default=None, help="one agent's queue only")
    p_sched.add_argument("--apply", action="store_true",
                         help="write the dates (work items without dates, or dated by the schedule)")
    p_sched.add_argument("--force", action="store_true",
                         help="with --apply: also overwrite dates set by a person")
    p_sched.add_argument("--actor", default="")
    p_sched.add_argument("--json", action="store_true", help="schema ameesh-schedule/1")
    p_sched.set_defaults(func=func)

    p_meas = work_sub.add_parser(
        "measure", help="actual time of delivered work items by phase (agent, CI, review, human) "
                        "and size of the merged diff (L157c); the runner does it after each "
                        "branch sweep")
    p_meas.add_argument("ids", nargs="*", type=int,
                        help="these work items (measured again); default: those not measured yet")
    p_meas.add_argument("--repo", action="append", default=None, metavar="DIR",
                        help="repository holding the merge commits (repeatable; default: the "
                             "current one)")
    p_meas.add_argument("--no-ci", action="store_true",
                        help="do not read the CI runs (gh): CI time stays unknown")
    p_meas.add_argument("--json", action="store_true")
    p_meas.set_defaults(func=func)

    p_proj = work_sub.add_parser(
        "project-github", help="one GitHub issue per work item of a project (--app, L126), or "
                               "the plan as issues (--repo alone); a view, never a source")
    p_proj.add_argument("--app", default=None, metavar="PROJECT",
                        help="project whose work items each get an issue; "
                             "repository read from github.projects (host configuration)")
    p_proj.add_argument("--repo", default=None,
                        help="owner/repo: the plan's repository; with --app, overrides the "
                             "configured repository")
    p_proj.add_argument("--dry-run", action="store_true",
                        help="read GitHub and show without writing anything")
    p_proj.add_argument("--canon-url", default=None,
                        help="plan: base of the links to the packages (e.g. https://.../blob/{commit})")
    p_proj.add_argument("--json", action="store_true")
    p_proj.set_defaults(func=func)


def run(db, args: argparse.Namespace) -> int:
    """Exécute une sous-commande du plan sur la connexion `db` ouverte."""
    command = args.work_command
    if command == "plan":
        return _plan(db, args)
    if command == "estimates":
        from . import estimates
        if args.publish:
            if args.app:
                print("error: --publish covers all projects (without --app)",
                      file=sys.stderr)
                return 2
            row = estimates.publish(db, actor=args.actor)
            if args.json:
                print(json.dumps(row, ensure_ascii=False, indent=2))
            else:
                print(estimates.format_published(row))
            return 0
        report = estimates.report(db, app=args.app)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(estimates.format_report(report))
        return 0
    if command == "schedule":
        from . import lot_schedule
        sched = lot_schedule.build(db, assignee=args.assignee,
                                   force=bool(args.apply and args.force))
        written = (lot_schedule.apply(db, sched, force=args.force, actor=args.actor)
                   if args.apply else None)
        if args.json:
            print(json.dumps(dict(sched, written=[e["id"] for e in written or ()]),
                             ensure_ascii=False, indent=2))
        else:
            print(lot_schedule.format_schedule(sched, written))
        return 0
    if command == "measure":
        import os
        from . import lot_measures
        repos = [os.path.abspath(os.path.expanduser(r)) for r in args.repo or [os.getcwd()]]
        rows = lot_measures.measure(db, args.ids or None, repos=repos,
                                    ci=None if args.no_ci else lot_measures.gh_ci_runs)
        if args.json:
            print(json.dumps([lot_measures.view(r) | {"id": int(r["work_item_id"])}
                              for r in rows], ensure_ascii=False, indent=2))
        else:
            print(lot_measures.format_measures(rows))
        return 0
    if command == "link":
        if args.none == bool(args.package):
            print("error: a package OR --none", file=sys.stderr)
            return 2
        row = work.link(db, args.id, None if args.none else args.package, actor=args.actor)
        print("work item #%d %s" % (row["id"], "linked to %s" % row["package_id"]
                                     if row.get("package_id") else "detached from the plan"))
        return 0
    if command == "close":
        row = work.close(db, args.id, abandoned=args.abandoned,
                         superseded_by=args.superseded_by, note=args.note, actor=args.actor)
        print("work item #%d closed: %s" % (row["id"], "abandoned"
                                           if row["close_reason"] == "abandoned"
                                           else "superseded by #%s" % row["superseded_by"]))
        return 0
    if command == "sync-merges":
        return _sync_merges(db, args)
    if command == "sync-branches":
        return _sync_branches(db, args)
    try:
        return _github(db, args)
    except plan_github.GithubError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


def _plan(db, args: argparse.Namespace) -> int:
    """L96 : dates prévues d'une tâche ou d'une fiche WorkPackage ; L157 :
    durée estimée d'une tâche (`--estimate`)."""
    from . import estimates, roadmap

    kwargs = {k: v for k, v in (("start", args.debut), ("end", args.fin),
                                ("delivery", args.livraison)) if v is not None}
    if args.estimate is not None:
        text = str(args.target).strip().lstrip("#")
        if not text.isdigit():
            raise estimates.EstimateError(
                "--estimate applies to a task (its number), not to the package %s"
                % args.target)
        estimates.parse(args.estimate)          # illisible : rien n'est écrit
        out = None
        if kwargs:
            out = roadmap.plan(db, args.target, source=args.source, actor=args.actor,
                               **kwargs)
        estimated = estimates.set_estimate(
            db, int(text), args.estimate, source=args.estimate_source or args.source,
            actor=args.actor)
        if out is None:             # l'estimation seule : aucune date touchée
            out = {"target": int(text), "kind": "task", "plan": None,
                   "estimate": estimates.view(estimated)}
            if args.json:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            else:
                print("task #%s: %s%s" % (text, out["estimate"]["label"],
                                          " (source: %s)" % out["estimate"]["source"]
                                            if out["estimate"].get("source") else ""))
            return 0
        out["estimate"] = estimates.view(estimated)
    else:
        out = roadmap.plan(db, args.target, source=args.source, actor=args.actor, **kwargs)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    row = out["plan"]
    label = "task #%s" % out["target"] if out["kind"] == "task" else "package %s" % out["target"]
    print("%s: start %s · end %s · delivery %s%s%s" % (
        label, row.get("planned_start") or "—", row.get("planned_end") or "—",
        row.get("planned_delivery") or "—",
        " (source: %s)" % row["planned_source"] if row.get("planned_source") else "",
        " · %s" % out["estimate"]["label"] if out.get("estimate") else ""))
    return 0


#: libellés des issues d'un relevé des branches
_BRANCH_RESULTS = {"merged": "CLOSED (delivered)", "already": "already delivered",
                   "refused": "work item closed", "would-merge": "would be closed",
                   "open": "open", "skipped": "skipped", "no-repo": "no repository",
                   "no-target": "no target", "error": "error"}


def _sync_branches(db, args: argparse.Namespace) -> int:
    """L118 : la fusion d'une branche, avec ou sans PR."""
    host = None if args.all_hosts else (args.host or db.cfg.host)
    report = plan_git.sync_branches(db, host=host, dry_run=args.dry_run, actor=args.actor)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not report["results"]:
        print("no open work item to examine%s" % (" on %s" % host if host else ""))
        return 0
    for entry in report["results"]:
        print("work item #%-5d %-36s %-17s %s" % (
            entry["work_item"], entry["branch"] or "(no branch)",
            _BRANCH_RESULTS.get(entry["result"], entry["result"]), entry["detail"]))
    print("%d work item(s) closed%s" % (report["merged"], " (dry run)" if args.dry_run else ""))
    return 0


def _sync_merges(db, args: argparse.Namespace) -> int:
    """Le contenu d'abord (git), puis, à défaut et si `--repo`, la PR liée."""
    try:
        report = plan_git.sync_merges(db, args.git_dir, target=args.target,
                                      dry_run=args.dry_run)
    except plan_git.MergeProbeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    gh_report = None
    if args.repo:
        try:
            gh_report = plan_github.sync_github(db, plan_github.Gh(), args.repo,
                                                dry_run=args.dry_run)
        except plan_github.GithubError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
    refused = bool(gh_report and gh_report["refused"])
    if args.json:
        print(json.dumps({"git": report, "github": gh_report}, ensure_ascii=False, indent=2))
        return 1 if refused else 0
    print("%s%s (%s): %d work item(s) closed by content" % (
        "[dry run] " if args.dry_run else "", report["git_dir"], report["target"],
        report["merged"]))
    for row in report["results"]:
        print("  work item #%-5s %-14s %-11s %-9s %s" % (
            row["work_item"], row.get("package") or "—", row["result"],
            row.get("how") or "—", row.get("detail") or ""))
    if gh_report is not None:
        print("%s: %d work item(s) closed by their merged PR" % (gh_report["repo"],
                                                                gh_report["merged"]))
        for row in gh_report["results"]:
            print("  %-22s work item #%-5s %-11s %s" % (row["pr"], row["work_item"],
                                                        row["result"], row.get("detail") or ""))
    return 1 if refused else 0


def _github(db, args: argparse.Namespace) -> int:
    command = args.work_command
    gh = plan_github.Gh()
    if command == "sync-github":
        report = plan_github.sync_github(db, gh, args.repo, limit=args.limit,
                                         dry_run=args.dry_run)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if report["refused"] else 0
        print("%s%s: %d merged PR(s) read, %d work item(s) closed" % (
            "[dry run] " if args.dry_run else "", report["repo"], report["pull_requests"],
            report["merged"]))
        for row in report["results"]:
            print("  %-22s work item #%-5s %-12s %-11s %s" % (
                row["pr"], row["work_item"], row.get("package") or "—", row["result"],
                row.get("detail") or ""))
        for row in report["refused"]:
            print("WARNING: work item #%s closed but its PR %s is merged; no silent "
                  "reopening; decide (ameesh work show %s)"
                  % (row["work_item"], row["pr"], row["work_item"]), file=sys.stderr)
        return 1 if report["refused"] else 0
    if command == "project-github":
        if args.app:
            return _project_lots(db, gh, args)
        if not args.repo:
            print("error: --app <project> (one issue per work item, L126) or --repo owner/repo "
                  "(the plan)", file=sys.stderr)
            return 2
        report = plan_github.project_github(db, gh, args.repo, dry_run=args.dry_run,
                                            canon_url=args.canon_url)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        _print_projection(report)
        return 0
    print("error: unknown subcommand: %s" % command, file=sys.stderr)
    return 2


def _project_lots(db, gh, args: argparse.Namespace) -> int:
    """L126 : une issue par lot du projet `--app`, dans le dépôt configuré."""
    cfg = getattr(db, "cfg", None)
    settings = plan_github.settings_of(cfg)
    project = settings.project_name(args.app) or args.app.strip()
    repo = args.repo or settings.repo_for(args.app)
    if not repo:
        print("error: project %s missing from `github.projects` (host configuration): "
              "add it, or give --repo owner/repo" % args.app, file=sys.stderr)
        return 2
    report = plan_github.project_lots(
        db, gh, project, repo, dry_run=args.dry_run, settings=settings,
        forge_hosts=getattr(cfg, "forge_hosts", None) or (),
        local_host=getattr(cfg, "host", None))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    _print_lot_issues(report)
    return 0


#: libellés des actions sur une issue de lot (essai, réel)
_LOT_ACTIONS = {"create": ("to create", "created"), "update": ("to update", "updated"),
                "close": ("to close", "closed"), "unchanged": ("unchanged", "unchanged"),
                "refused": ("REFUSED", "REFUSED"),
                "deferred": ("deferred", "deferred to the next pass")}


def _print_lot_issues(report: dict) -> None:
    dry = report["dry_run"]
    print("%swork item issues of project %s on %s (%s): %d work item(s)" % (
        "[dry run] " if dry else "", report["project"], report["repo"],
        "public repository: title and public summary only" if report["public"]
        else "private repository: work item body published, without secrets",
        len(report["issues"])))
    if report["visibility"] not in ("public", "private"):
        print("  repository visibility: %s; treated as public" % report["visibility"])
    for name in report["labels_created"]:
        print("  label %s %s" % (name, "to create" if dry else "created"))
    quiet = 0
    for row in report["issues"]:
        if row["action"] == "unchanged":
            quiet += 1
            continue
        verb = _LOT_ACTIONS.get(row["action"], (row["action"],) * 2)[0 if dry else 1]
        print("  work item %-5s %-6s %-17s %s%s%s" % (
            row["work_item"], "#%s" % row["number"] if row.get("number") else "", verb,
            row.get("title") or "", " [%s]" % " ".join(row["labels"])
            if row["action"] == "create" else "",
            " (%s)" % ", ".join(row["changes"]) if row.get("changes") else ""))
        if row.get("comment"):
            print("        comment: %s" % row["comment"])
        if row.get("error"):
            print("        ERROR: %s" % row["error"])
    if quiet:
        print("  %d issue(s) unchanged" % quiet)
    for row in report["refused"]:
        print("REFUSED: work item %s, %s: %s; %s until the work item title is fixed" % (
            row["work_item"], row["field"], ", ".join(row["reasons"]),
            "title of #%s kept as is" % row["number"] if row.get("number")
            else "nothing is published"))
    for row in report["withheld"]:
        print("  withheld: work item %s, %s not published (%s)" % (
            row["work_item"], row["field"], ", ".join(row["reasons"])))
    for row in report["human_edits"]:
        if row["kept"]:
            print("  edited in GitHub: #%s (work item %s), %s kept as is; never "
                  "imported into ameesh" % (row["number"], row["work_item"],
                                            ", ".join(row["kept"])))
        if row["replaced"]:
            print("  edited in GitHub: #%s (work item %s), %s rewritten: the work item changed" % (
                row["number"], row["work_item"], ", ".join(row["replaced"])))
    for row in report["issue_refs"]:
        if row["result"] != "set":
            print("  issue_ref of work item %s: %s" % (row["work_item"], row.get("detail")))
    for row in report["duplicates"]:
        print("  duplicate: #%s of work item %s (kept: #%s)%s" % (
            row["number"], row["work_item"], row["kept"], "; closed" if row.get("closed")
            else ""))
    for row in report["orphans"]:
        print("  issue(s) %s: %s (work item %s)" % (", ".join("#%s" % n for n in row["numbers"]),
                                               row["detail"], row["work_item"]))
    counts = report["counts"]
    print("%d %s, %d %s, %d %s, %d refused%s%s" % (
        counts["created"], "to create" if dry else "created",
        counts["updated"], "to update" if dry else "updated",
        counts["closed"], "to close" if dry else "closed", counts["refused"],
        ", %d deferred" % counts["deferred"] if counts["deferred"] else "",
        ", %d error(s)" % counts["errors"] if counts["errors"] else ""))


def _print_projection(report: dict) -> None:
    tag = "[dry run] " if report["dry_run"] else ""
    actions = {"create": "to create" if report["dry_run"] else "created",
               "update": "to update" if report["dry_run"] else "updated",
               "unchanged": "unchanged"}
    print("%splan projection on %s: %d issue(s)" % (tag, report["repo"],
                                                   len(report["issues"])))
    for name in report["labels_created"]:
        print("  label %s %s" % (name, "to create" if report["dry_run"] else "created"))
    for row in report["issues"]:
        print("  %-5s %-16s %s%s%s" % (
            row["kind"], row["package"],
            "#%s " % row["number"] if row.get("number") else "",
            actions.get(row["action"], row["action"]),
            " (%s)" % ", ".join(row["changes"]) if row.get("changes") else ""))
    for row in report["sub_issues"]:
        verb = "add" if "add" in row else "remove"
        print("  sub-issue: %s %s %s the epic %s" % (
            verb, row.get("add") or row.get("remove"), "to" if "add" in row else "from",
            row["epic"]))
    if report["sub_issues_api"] is False:
        print("  sub-issues API unavailable: work items listed as tasks in the epic bodies")
    for row in report["proposals"]:
        print("PROPOSAL: issue #%s (%s): %s" % (row["number"], row["package"],
                                                row["detail"]))
    for row in report["orphans"]:
        print("  issue #%s (%s): %s" % (row["number"], row["package"], row["detail"]))
    for row in report["duplicates"]:
        print("  issue #%s: duplicate of #%s for %s (ignored)" % (
            row["number"], row["kept"], row["package"]))
    for note in report.get("notes") or []:
        print("  note: %s" % note)
