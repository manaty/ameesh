# SPDX-License-Identifier: AGPL-3.0-only
"""Sous-commandes `ameesh work` du plan de travail (lot L29).

  ameesh work link <id> <fiche>|--none
  ameesh work close <id> --abandoned | --superseded-by <id> [--note …]
  ameesh work sync-merges --git-dir D [--target origin/main] [--repo R] [--dry-run] [--json]
  ameesh work sync-github --repo R [--limit N] [--dry-run] [--json]
  ameesh work project-github --repo R [--dry-run] [--canon-url URL] [--json]

Gardées hors de `mesh_cli` (qui les branche) pour que leurs évolutions n'y
touchent pas. Voir `plan_github` pour les règles de GitHub (vue, jamais
source ; lecture seule pour `sync-github`).
"""
from __future__ import annotations

import argparse
import json
import sys

from . import plan_git, plan_github, work

COMMANDS = ("link", "close", "sync-merges", "sync-github", "project-github")


def add_parsers(work_sub, func) -> None:
    """Ajoute les sous-commandes du plan à `ameesh work`."""
    p_link = work_sub.add_parser("link", help="rattacher un lot à une fiche WorkPackage (L29)")
    p_link.add_argument("id", type=int)
    p_link.add_argument("package", nargs="?", default=None, help="identifiant de la fiche")
    p_link.add_argument("--none", action="store_true", help="détacher le lot du plan")
    p_link.add_argument("--actor", default="")
    p_link.set_defaults(func=func)

    p_close = work_sub.add_parser(
        "close", help="fermer un lot abandonné ou remplacé (jalon closed, L29)")
    p_close.add_argument("id", type=int)
    how = p_close.add_mutually_exclusive_group(required=True)
    how.add_argument("--abandoned", action="store_true", help="le lot ne sera pas fait")
    how.add_argument("--superseded-by", type=int, default=None, metavar="ID",
                     help="le lot est remplacé par ce lot")
    p_close.add_argument("--note", default="")
    p_close.add_argument("--actor", default="")
    p_close.set_defaults(func=func)

    p_merges = work_sub.add_parser(
        "sync-merges", help="fermer les lots dont le contenu gelé est fusionné (git, L29)")
    p_merges.add_argument("--git-dir", default=".", help="clone local (lecture seule)")
    p_merges.add_argument("--target", default=plan_git.DEFAULT_TARGET,
                          help="branche cible (défaut origin/main ; aucun fetch)")
    p_merges.add_argument("--repo", default=None,
                          help="owner/repo : à défaut de preuve par le contenu, la PR liée")
    p_merges.add_argument("--dry-run", action="store_true", help="dire sans fermer")
    p_merges.add_argument("--json", action="store_true")
    p_merges.set_defaults(func=func)

    p_sync = work_sub.add_parser(
        "sync-github", help="fermer les lots dont la PR est fusionnée (lecture seule de GitHub)")
    p_sync.add_argument("--repo", required=True, help="owner/repo")
    p_sync.add_argument("--limit", type=int, default=200, help="PR fusionnées lues (défaut 200)")
    p_sync.add_argument("--dry-run", action="store_true", help="dire sans fermer")
    p_sync.add_argument("--json", action="store_true")
    p_sync.set_defaults(func=func)

    p_proj = work_sub.add_parser(
        "project-github", help="projeter le plan en issues GitHub (vue, jamais source)")
    p_proj.add_argument("--repo", required=True, help="owner/repo")
    p_proj.add_argument("--dry-run", action="store_true", help="montrer sans rien écrire")
    p_proj.add_argument("--canon-url", default=None,
                        help="base des liens vers les fiches (ex. https://…/blob/{commit})")
    p_proj.add_argument("--json", action="store_true")
    p_proj.set_defaults(func=func)


def run(db, args: argparse.Namespace) -> int:
    """Exécute une sous-commande du plan sur la connexion `db` ouverte."""
    command = args.work_command
    if command == "link":
        if args.none == bool(args.package):
            print("erreur : une fiche OU --none", file=sys.stderr)
            return 2
        row = work.link(db, args.id, None if args.none else args.package, actor=args.actor)
        print("lot #%d %s" % (row["id"], "rattaché à %s" % row["package_id"]
                               if row.get("package_id") else "détaché du plan"))
        return 0
    if command == "close":
        row = work.close(db, args.id, abandoned=args.abandoned,
                         superseded_by=args.superseded_by, note=args.note, actor=args.actor)
        print("lot #%d fermé : %s" % (row["id"], "abandonné" if row["close_reason"] == "abandoned"
                                      else "remplacé par #%s" % row["superseded_by"]))
        return 0
    if command == "sync-merges":
        return _sync_merges(db, args)
    try:
        return _github(db, args)
    except plan_github.GithubError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


def _sync_merges(db, args: argparse.Namespace) -> int:
    """Le contenu d'abord (git), puis, à défaut et si `--repo`, la PR liée."""
    try:
        report = plan_git.sync_merges(db, args.git_dir, target=args.target,
                                      dry_run=args.dry_run)
    except plan_git.MergeProbeError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    gh_report = None
    if args.repo:
        try:
            gh_report = plan_github.sync_github(db, plan_github.Gh(), args.repo,
                                                dry_run=args.dry_run)
        except plan_github.GithubError as exc:
            print("erreur : %s" % exc, file=sys.stderr)
            return 1
    refused = bool(gh_report and gh_report["refused"])
    if args.json:
        print(json.dumps({"git": report, "github": gh_report}, ensure_ascii=False, indent=2))
        return 1 if refused else 0
    print("%s%s (%s) : %d lot(s) fermé(s) par le contenu" % (
        "[essai] " if args.dry_run else "", report["git_dir"], report["target"],
        report["merged"]))
    for row in report["results"]:
        print("  lot #%-5s %-14s %-11s %-9s %s" % (
            row["work_item"], row.get("package") or "—", row["result"],
            row.get("how") or "—", row.get("detail") or ""))
    if gh_report is not None:
        print("%s : %d lot(s) fermé(s) par leur PR fusionnée" % (gh_report["repo"],
                                                               gh_report["merged"]))
        for row in gh_report["results"]:
            print("  %-22s lot #%-5s %-11s %s" % (row["pr"], row["work_item"], row["result"],
                                                  row.get("detail") or ""))
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
        print("%s%s : %d PR fusionnée(s) lue(s), %d lot(s) fermé(s)" % (
            "[essai] " if args.dry_run else "", report["repo"], report["pull_requests"],
            report["merged"]))
        for row in report["results"]:
            print("  %-22s lot #%-5s %-12s %-11s %s" % (
                row["pr"], row["work_item"], row.get("package") or "—", row["result"],
                row.get("detail") or ""))
        for row in report["refused"]:
            print("ATTENTION : lot #%s fermé mais sa PR %s est fusionnée — aucune réouverture "
                  "silencieuse ; décidez (ameesh work show %s)"
                  % (row["work_item"], row["pr"], row["work_item"]), file=sys.stderr)
        return 1 if report["refused"] else 0
    if command == "project-github":
        report = plan_github.project_github(db, gh, args.repo, dry_run=args.dry_run,
                                            canon_url=args.canon_url)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        _print_projection(report)
        return 0
    print("erreur : sous-commande inconnue : %s" % command, file=sys.stderr)
    return 2


def _print_projection(report: dict) -> None:
    tag = "[essai] " if report["dry_run"] else ""
    actions = {"create": "à créer" if report["dry_run"] else "créée",
               "update": "à mettre à jour" if report["dry_run"] else "mise à jour",
               "unchanged": "inchangée"}
    print("%sprojection du plan sur %s : %d issue(s)" % (tag, report["repo"],
                                                         len(report["issues"])))
    for name in report["labels_created"]:
        print("  label %s %s" % (name, "à créer" if report["dry_run"] else "créé"))
    for row in report["issues"]:
        print("  %-5s %-16s %s%s%s" % (
            row["kind"], row["package"],
            "#%s " % row["number"] if row.get("number") else "",
            actions.get(row["action"], row["action"]),
            " (%s)" % ", ".join(row["changes"]) if row.get("changes") else ""))
    for row in report["sub_issues"]:
        verb = "ajouter" if "add" in row else "retirer"
        print("  sous-issue : %s %s %s l'epic %s" % (
            verb, row.get("add") or row.get("remove"), "à" if "add" in row else "de",
            row["epic"]))
    if report["sub_issues_api"] is False:
        print("  API sub-issues indisponible : lots listés en tâches dans le corps des epics")
    for row in report["proposals"]:
        print("PROPOSITION : issue #%s (%s) — %s" % (row["number"], row["package"],
                                                      row["detail"]))
    for row in report["orphans"]:
        print("  issue #%s (%s) : %s" % (row["number"], row["package"], row["detail"]))
    for row in report["duplicates"]:
        print("  issue #%s : doublon de #%s pour %s (ignorée)" % (
            row["number"], row["kept"], row["package"]))
    for note in report.get("notes") or []:
        print("  note : %s" % note)
