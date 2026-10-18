#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Liste calculée des lots prêts pour le paquet horaire (#2404).

    scripts/paquet-candidats.py [--repo .] [--project ameesh] [--target main] \\
        [--json] [--ready-out "$TMPDIR/prets-0800.txt"]

La composition d'un paquet part de cette liste, **jamais** des seuls lots
cités par les courriers : un lot prêt que personne n'a cité doit apparaître
soit comme unité du paquet, soit comme ligne « écarté » motivée du manifeste.
Le calcul est celui de `ameesh.packet_candidates` : état `qa` (sans filtre
d'assigné), gel effectif égal à la **tête publiée** de la branche (lecture
fraîche d'`origin`), verdicts requis acquis sur cette tête par la porte de
fusion, essai de fusion propre contre la cible.

Sortie lisible par défaut : les lots prêts d'abord, puis les lots en attente
avec leur motif (`freeze-not-head`, `verdicts-missing`, `conflict`, …), puis
les lectures impossibles. `--json` rend le schéma
`ameesh-packet-candidates/1`. `--ready-out` écrit les ids prêts, un par ligne,
dans le format de `scripts/paquet-inclusion.py --ready-file` :

    scripts/paquet-candidats.py --repo . --ready-out "$TMPDIR/prets-0800.txt"
    scripts/paquet-inclusion.py --manifest reviews/integration-paquet-h-…md \\
        --head origin/integration/paquet-h-… --ready-file "$TMPDIR/prets-0800.txt" \\
        --expected-file "$TMPDIR/admissions-0800.txt"

Codes de retour : 0 si la liste est complète (même sans lot prêt), 1 si une
lecture a échoué (la liste est incomplète : ne pas composer dessus), 2 si
l'entrée est invalide (dépôt, projet, base).

Lecture seule : la base n'est pas modifiée ; les branches sont lues dans le
clone privé du radar ou dans un clone jetable, jamais dans le dépôt des agents.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ameesh import config, db, merge_queue, packet_candidates, plan_git  # noqa: E402


def resolve_project(cfg, repo: str, name: str | None):
    """Le projet déclaré (`radar.projects`), sinon une identité de lecture.

    Rend `(project, declared)` : `declared` dit si le clone privé du radar peut
    servir (un projet seulement deviné ne doit pas écrire dans ce clone).
    """
    if name:
        projects = merge_queue.declared_projects(cfg)
        project = projects.get(name)
        if project is None:
            raise packet_candidates.CandidateError(
                "project %r is not declared in radar.projects (declared: %s)"
                % (name, ", ".join(sorted(projects)) or "none"))
        return project, True
    try:
        project = merge_queue.radar_project(cfg, repo)
    except merge_queue.QueueError as exc:
        raise packet_candidates.CandidateError(str(exc)) from exc
    if project is not None:
        return project, True
    return SimpleNamespace(name="", target="", repo=repo), False


def render(payload: dict) -> str:
    lines = [
        "paquet-candidats : projet %s, cible %s, base %s (%s)"
        % (payload["project"] or "?", payload["target"], payload["base"][:12],
           "tête de la cible lue de origin" if payload["base_source"] == "target"
           else "commit imposé par --base"),
        "%d lot(s) prêt(s), %d en attente, %d erreur(s)"
        % (len(payload["ready"]), len(payload["waiting"]), len(payload["errors"])),
    ]
    by_id = {entry["work_item"]: entry for entry in payload["candidates"]}
    if payload["ready"]:
        lines += ["", "PRÊTS (à composer, ou à écarter avec un motif au manifeste)"]
        for lot in payload["ready"]:
            entry = by_id[lot]
            klass = (entry.get("review_class") or {}).get("class") or "?"
            lines.append("  #%-5d %s  %-14s %-9s %s"
                         % (lot, entry["published_head"][:12],
                            entry.get("assignee") or "-", klass, entry["branch"]))
    if payload["waiting"]:
        lines += ["", "EN ATTENTE (motif calculé)"]
        for lot in payload["waiting"]:
            entry = by_id[lot]
            reasons = "; ".join(
                "%s%s" % (cause["code"],
                          ": " + cause["detail"] if cause.get("detail") else "")
                for cause in entry["causes"])
            lines.append("  #%-5d %s  %s"
                         % (lot, (entry.get("published_head") or "-")[:12], reasons))
    if payload["errors"]:
        lines += ["", "ERREURS (liste incomplète : ne pas composer dessus)"]
        lines += ["  %s" % error for error in payload["errors"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Liste calculée des lots prêts pour le paquet horaire (#2404).")
    parser.add_argument("--repo", default=".",
                        help="dépôt git du projet (défaut : .)")
    parser.add_argument("--project", default=None,
                        help="nom du projet radar.projects (défaut : celui du dépôt)")
    parser.add_argument("--app", default=None,
                        help="nom du projet dans les lots (défaut : nom du projet)")
    parser.add_argument("--target", default=None,
                        help="branche cible (défaut : cible du projet, sinon origin/HEAD)")
    parser.add_argument("--base", default=None,
                        help="commit de base de l'essai de fusion "
                             "(défaut : tête fraîche de la cible)")
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument("--ready-out", default=None, metavar="FILE",
                        help="écrit les ids prêts, un par ligne, pour --ready-file")
    args = parser.parse_args(argv)

    repo = os.path.realpath(args.repo)
    try:
        cfg = config.load()
    except Exception as exc:  # noqa: BLE001 - configuration illisible
        print("paquet-candidats: cannot load the configuration: %s" % exc, file=sys.stderr)
        return 2
    try:
        project, declared = resolve_project(cfg, repo, args.project)
    except packet_candidates.CandidateError as exc:
        print("paquet-candidats: %s" % exc, file=sys.stderr)
        return 2
    target = args.target or getattr(project, "target", "") or plan_git.default_target(repo)
    if not target:
        print("paquet-candidats: no target branch: pass --target", file=sys.stderr)
        return 2
    app = args.app or getattr(project, "name", "") or packet_candidates.default_app(repo)
    conn = None
    scratch = None
    try:
        conn = db.connect(cfg)
        clone, scratch = merge_queue.working_clone(cfg, repo, project if declared else None)
        payload = packet_candidates.inventory(
            conn, project, repo=repo, clone=clone, target=target, app=app,
            base=args.base, cfg=cfg)
        if args.ready_out:
            path = Path(args.ready_out)
            path.write_text(
                "# paquet-candidats %s %s\n%s\n"
                % (payload["target"], payload["base"][:12],
                   "\n".join("#%d" % lot for lot in payload["ready"])),
                encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str)
              if args.json else render(payload))
        return 1 if payload["errors"] else 0
    except packet_candidates.CandidateError as exc:
        print("paquet-candidats: %s" % exc, file=sys.stderr)
        return 1
    except (db.DbError, OSError, ValueError) as exc:
        print("paquet-candidats: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
        if scratch:
            import shutil
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
