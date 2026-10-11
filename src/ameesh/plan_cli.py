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
            "plan", "estimates")


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

    p_branches = work_sub.add_parser(
        "sync-branches", help="fermer les lots dont la fusion dans leur cible est constatée, "
                              "avec ou sans PR : branche du lot, ou commit de fusion qui "
                              "porte « ameesh-work: <id> » (ou #<id> si git config "
                              "ameesh.lotRef hash) — L118 ; fait aussi par l'exécuteur")
    p_branches.add_argument("--host", default=None,
                            help="hôte dont les assignés sont examinés (défaut : cet hôte)")
    p_branches.add_argument("--all-hosts", action="store_true",
                            help="tous les assignés (leurs dossiers doivent être ici)")
    p_branches.add_argument("--dry-run", action="store_true", help="dire sans fermer")
    p_branches.add_argument("--actor", default="")
    p_branches.add_argument("--json", action="store_true")
    p_branches.set_defaults(func=func)

    p_sync = work_sub.add_parser(
        "sync-github", help="fermer les lots dont la PR est fusionnée (lecture seule de GitHub)")
    p_sync.add_argument("--repo", required=True, help="owner/repo")
    p_sync.add_argument("--limit", type=int, default=200, help="PR fusionnées lues (défaut 200)")
    p_sync.add_argument("--dry-run", action="store_true", help="dire sans fermer")
    p_sync.add_argument("--json", action="store_true")
    p_sync.set_defaults(func=func)

    p_plan = work_sub.add_parser(
        "plan", help="dates prévues d'une tâche ou d'une fiche WorkPackage (L96)")
    p_plan.add_argument("target", help="numéro de tâche, ou identifiant de fiche")
    p_plan.add_argument("--debut", "--start", dest="debut", default=None,
                        help="début prévu : AAAA-MM-JJ, demain, lundi… ; - efface")
    p_plan.add_argument("--fin", "--end", dest="fin", default=None, help="fin prévue")
    p_plan.add_argument("--livraison", "--deploiement", "--delivery", dest="livraison",
                        default=None, help="livraison ou déploiement prévu")
    p_plan.add_argument("--source", default=None,
                        help="d'où vient la date (conversation, décision, message…) ; "
                             "aussi celle de l'estimation, à défaut de --estimate-source")
    p_plan.add_argument("--estimate", default=None, metavar="DURÉE",
                        help="durée estimée d'une tâche : 90m, 2h, 1h30, 1,5h, 2d (L157)")
    p_plan.add_argument("--estimate-source", default=None, metavar="SOURCE",
                        help="d'où vient l'estimation (conception, historique…)")
    p_plan.add_argument("--actor", default="")
    p_plan.add_argument("--json", action="store_true")
    p_plan.set_defaults(func=func)

    p_est = work_sub.add_parser(
        "estimates", help="écarts réel/estimé des lots livrés, par type de lot et par auteur "
                          "d'estimation (médiane, p80) — L157, pour l'auditeur")
    p_est.add_argument("--app", default=None, metavar="PROJET", help="un projet seulement")
    p_est.add_argument("--json", action="store_true", help="schéma ameesh-estimates/1")
    p_est.set_defaults(func=func)

    p_proj = work_sub.add_parser(
        "project-github", help="une issue GitHub par lot d'un projet (--app, L126), ou le "
                               "plan en issues (--repo seul) — vue, jamais source")
    p_proj.add_argument("--app", default=None, metavar="PROJET",
                        help="projet dont chaque lot a son issue ; dépôt lu dans "
                             "github.projects (configuration de l'hôte)")
    p_proj.add_argument("--repo", default=None,
                        help="owner/repo : le dépôt du plan ; avec --app, remplace le dépôt "
                             "configuré")
    p_proj.add_argument("--dry-run", action="store_true",
                        help="lire GitHub et montrer sans rien écrire")
    p_proj.add_argument("--canon-url", default=None,
                        help="plan : base des liens vers les fiches (ex. https://…/blob/{commit})")
    p_proj.add_argument("--json", action="store_true")
    p_proj.set_defaults(func=func)


def run(db, args: argparse.Namespace) -> int:
    """Exécute une sous-commande du plan sur la connexion `db` ouverte."""
    command = args.work_command
    if command == "plan":
        return _plan(db, args)
    if command == "estimates":
        from . import estimates
        report = estimates.report(db, app=args.app)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(estimates.format_report(report))
        return 0
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
    if command == "sync-branches":
        return _sync_branches(db, args)
    try:
        return _github(db, args)
    except plan_github.GithubError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
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
                "--estimate se pose sur une tâche (son numéro), pas sur la fiche %s"
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
                print("tâche #%s : %s%s" % (text, out["estimate"]["label"],
                                            " (source : %s)" % out["estimate"]["source"]
                                            if out["estimate"].get("source") else ""))
            return 0
        out["estimate"] = estimates.view(estimated)
    else:
        out = roadmap.plan(db, args.target, source=args.source, actor=args.actor, **kwargs)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    row = out["plan"]
    label = "tâche #%s" % out["target"] if out["kind"] == "task" else "fiche %s" % out["target"]
    print("%s : début %s · fin %s · livraison %s%s%s" % (
        label, row.get("planned_start") or "—", row.get("planned_end") or "—",
        row.get("planned_delivery") or "—",
        " (source : %s)" % row["planned_source"] if row.get("planned_source") else "",
        " · %s" % out["estimate"]["label"] if out.get("estimate") else ""))
    return 0


#: libellés des issues d'un relevé des branches
_BRANCH_RESULTS = {"merged": "FERMÉ (livré)", "already": "déjà livré", "refused": "lot fermé",
                   "would-merge": "serait fermé", "open": "ouvert", "skipped": "ignoré",
                   "no-repo": "sans dépôt", "no-target": "sans cible", "error": "erreur"}


def _sync_branches(db, args: argparse.Namespace) -> int:
    """L118 : la fusion d'une branche, avec ou sans PR."""
    host = None if args.all_hosts else (args.host or db.cfg.host)
    report = plan_git.sync_branches(db, host=host, dry_run=args.dry_run, actor=args.actor)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not report["results"]:
        print("aucun lot ouvert à examiner%s" % (" sur %s" % host if host else ""))
        return 0
    for entry in report["results"]:
        print("lot #%-5d %-36s %-14s %s" % (
            entry["work_item"], entry["branch"] or "(sans branche)",
            _BRANCH_RESULTS.get(entry["result"], entry["result"]), entry["detail"]))
    print("%d lot(s) fermé(s)%s" % (report["merged"], " (essai)" if args.dry_run else ""))
    return 0


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
        if args.app:
            return _project_lots(db, gh, args)
        if not args.repo:
            print("erreur : --app <projet> (une issue par lot, L126) ou --repo owner/repo "
                  "(le plan)", file=sys.stderr)
            return 2
        report = plan_github.project_github(db, gh, args.repo, dry_run=args.dry_run,
                                            canon_url=args.canon_url)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        _print_projection(report)
        return 0
    print("erreur : sous-commande inconnue : %s" % command, file=sys.stderr)
    return 2


def _project_lots(db, gh, args: argparse.Namespace) -> int:
    """L126 : une issue par lot du projet `--app`, dans le dépôt configuré."""
    cfg = getattr(db, "cfg", None)
    settings = plan_github.settings_of(cfg)
    project = settings.project_name(args.app) or args.app.strip()
    repo = args.repo or settings.repo_for(args.app)
    if not repo:
        print("erreur : projet %s absent de `github.projects` (configuration de l'hôte) : "
              "ajoutez-le, ou précisez --repo owner/repo" % args.app, file=sys.stderr)
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
_LOT_ACTIONS = {"create": ("à créer", "créée"), "update": ("à mettre à jour", "mise à jour"),
                "close": ("à fermer", "fermée"), "unchanged": ("inchangée", "inchangée"),
                "refused": ("REFUSÉE", "REFUSÉE"),
                "deferred": ("reportée", "reportée au passage suivant")}


def _print_lot_issues(report: dict) -> None:
    dry = report["dry_run"]
    print("%sissues des lots du projet %s sur %s (%s) : %d lot(s)" % (
        "[essai] " if dry else "", report["project"], report["repo"],
        "dépôt public : titre et résumé public seulement" if report["public"]
        else "dépôt privé : corps du lot publié, sans secret", len(report["issues"])))
    if report["visibility"] not in ("public", "private"):
        print("  visibilité du dépôt : %s — traité comme public" % report["visibility"])
    for name in report["labels_created"]:
        print("  étiquette %s %s" % (name, "à créer" if dry else "créée"))
    quiet = 0
    for row in report["issues"]:
        if row["action"] == "unchanged":
            quiet += 1
            continue
        verb = _LOT_ACTIONS.get(row["action"], (row["action"],) * 2)[0 if dry else 1]
        print("  lot %-5s %-6s %-16s %s%s%s" % (
            row["work_item"], "#%s" % row["number"] if row.get("number") else "", verb,
            row.get("title") or "", " [%s]" % " ".join(row["labels"])
            if row["action"] == "create" else "",
            " (%s)" % ", ".join(row["changes"]) if row.get("changes") else ""))
        if row.get("comment"):
            print("        commentaire : %s" % row["comment"])
        if row.get("error"):
            print("        ERREUR : %s" % row["error"])
    if quiet:
        print("  %d issue(s) inchangée(s)" % quiet)
    for row in report["refused"]:
        print("REFUSÉ : lot %s — %s : %s ; %s tant que le titre du lot n'est pas corrigé" % (
            row["work_item"], row["field"], ", ".join(row["reasons"]),
            "titre de #%s gardé tel quel" % row["number"] if row.get("number")
            else "rien n'est publié"))
    for row in report["withheld"]:
        print("  retenu : lot %s — %s non publié (%s)" % (row["work_item"], row["field"],
                                                         ", ".join(row["reasons"])))
    for row in report["human_edits"]:
        if row["kept"]:
            print("  modifiée dans GitHub : #%s (lot %s), %s gardé(s) tel(s) quel(s) — jamais "
                  "repris dans ameesh" % (row["number"], row["work_item"],
                                          ", ".join(row["kept"])))
        if row["replaced"]:
            print("  modifiée dans GitHub : #%s (lot %s), %s réécrit(s) : le lot a changé" % (
                row["number"], row["work_item"], ", ".join(row["replaced"])))
    for row in report["issue_refs"]:
        if row["result"] != "set":
            print("  issue_ref du lot %s : %s" % (row["work_item"], row.get("detail")))
    for row in report["duplicates"]:
        print("  doublon : #%s du lot %s (gardée : #%s)%s" % (
            row["number"], row["work_item"], row["kept"], " — fermée" if row.get("closed")
            else ""))
    for row in report["orphans"]:
        print("  issue(s) %s : %s (lot %s)" % (", ".join("#%s" % n for n in row["numbers"]),
                                               row["detail"], row["work_item"]))
    counts = report["counts"]
    print("%d %s, %d %s, %d %s, %d refusée(s)%s%s" % (
        counts["created"], "à créer" if dry else "créée(s)",
        counts["updated"], "à mettre à jour" if dry else "mise(s) à jour",
        counts["closed"], "à fermer" if dry else "fermée(s)", counts["refused"],
        ", %d reportée(s)" % counts["deferred"] if counts["deferred"] else "",
        ", %d erreur(s)" % counts["errors"] if counts["errors"] else ""))


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
