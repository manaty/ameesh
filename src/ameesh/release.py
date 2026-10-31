# SPDX-License-Identifier: AGPL-3.0-only
"""L750/L756 (#561/#566) — le périmètre d'une release, et sa porte.

Une release se fige à un commit **nommé** de sa cible quand le lot de release
est ouvert (`ameesh release open <lot> --base <commit>`) :

* les fusions de la cible postérieures au gel vont à la release suivante ;
* seuls les correctifs d'un bloquant de cette release sont cherry-pickés sur
  `release/x.y`, chacun avec sa propre exécution impactée ;
* la suite **complète** tourne une fois sur la tête gelée, et c'est elle qui
  certifie la release (L756) — jamais le plan impacté d'une PR, jamais le
  check vide d'une PR de version seule.

`ameesh release check <lot>` refuse une branche de release dont la base a
bougé après le gel sans raison enregistrée (`ameesh release reason <lot>
--text …`), et refuse l'absence d'une exécution de suite complète verte sur la
tête gelée exacte. La file de fusion applique la même porte à un lot de classe
release qui la traverse (`merge_queue.readiness`).

Le périmètre et la raison vivent au **journal du lot** (`release-perimeter:
<sha>`, `release-base-moved: <texte>`), comme toute règle dictée : ils se
relisent par `ameesh work show` et par les relecteurs.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import argparse
import re
import sys
import time

from . import lot_class, plan_git, work

#: le périmètre de release, écrit au journal du lot : `<préfixe> <sha>`
PERIMETER_PREFIX = "release-perimeter:"
#: la raison enregistrée d'un écart : `<préfixe> <texte>`
REASON_PREFIX = "release-base-moved:"
#: un commit abrégé (7) ou complet (40), jamais autre chose
_HEX = re.compile(r"^[0-9a-f]{7,40}$")


class ReleaseError(RuntimeError):
    """La porte d'une release refuse : le message dit quoi corriger."""


def _events(db, lot):
    return work.events(db, lot, limit=200) or []


def _last_note(db, lot, prefix: str) -> str:
    """Le dernier texte journalisé sous `prefix` (les événements sont relus du
    plus récent au plus ancien), ou la chaîne vide."""
    for row in _events(db, lot):
        text = str(row.get("note") or "")
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def perimeter(db, lot) -> str:
    """Le commit de la cible gelé comme périmètre, ou la chaîne vide."""
    return _last_note(db, lot, PERIMETER_PREFIX)


def reason(db, lot) -> str:
    """La raison enregistrée d'une base qui a bougé, ou la chaîne vide."""
    return _last_note(db, lot, REASON_PREFIX)


def record_perimeter(db, lot, base: str, *, actor: str = "") -> str:
    """Fige le périmètre de la release `lot` au commit `base` de sa cible.

    Idempotent pour le même commit ; refuse un autre périmètre : une autre
    release est un autre lot. Lève `ReleaseError` (commit illisible, lot
    inconnu, périmètre déjà figé ailleurs)."""
    value = str(base or "").strip().lower()
    if not _HEX.fullmatch(value):
        raise ReleaseError("release perimeter: %r is not a commit id (7 to 40 hex digits)"
                           % base)
    if work.get(db, lot) is None:
        raise ReleaseError("lot %s not found" % lot)
    current = perimeter(db, lot).lower()
    if current:
        if current.startswith(value) or value.startswith(current):
            return current
        raise ReleaseError("lot #%s already freezes its perimeter at %s: a new release is a new "
                           "lot" % (lot, current[:12]))
    work.note(db, lot, "%s %s" % (PERIMETER_PREFIX, value), actor=actor)
    return value


def record_reason(db, lot, text: str, *, actor: str = "") -> str:
    """Enregistre pourquoi la base de la release a bougé après le gel."""
    value = str(text or "").strip()
    if not value:
        raise ReleaseError("release base moved: a reason is required (--text \"…\")")
    if work.get(db, lot) is None:
        raise ReleaseError("lot %s not found" % lot)
    work.note(db, lot, "%s %s" % (REASON_PREFIX, value), actor=actor)
    return value


def base_moved(repo: str, branch: str, target: str, frozen: str) -> dict:
    """La base de la branche `branch` a-t-elle bougé depuis le périmètre ?

    La base est le merge-base de la branche et de sa cible : tant que la
    branche n'a pas absorbé la cible, il vaut le périmètre ; dès qu'une fusion
    (ou un rebasage) la fait avancer, il n'est plus le commit gelé. Le dépôt
    est lu tel quel — l'appelant fait `git fetch` avant."""
    tip = plan_git._commit(repo, branch)
    if not tip:
        raise ReleaseError("branch %s is not in %s" % (branch, repo))
    ref = plan_git._commit(repo, target)
    if not ref:
        raise ReleaseError("target %s is not in %s" % (target, repo))
    base = plan_git._out(repo, "merge-base", tip, ref)
    if not base:
        raise ReleaseError("cannot read the merge base of %s and %s in %s"
                           % (branch, target, repo))
    pinned = plan_git._commit(repo, frozen)
    if not pinned:
        raise ReleaseError("the frozen perimeter %s is not a commit of %s"
                           % (frozen[:12], repo))
    return {"branch": branch, "target": target, "tip": tip, "base": base,
            "perimeter": pinned, "moved": base != pinned}


def project_of(cfg, item: dict):
    """Le projet radar du lot (`row["app"]`), ou None : jamais d'exception."""
    from . import radar
    name = str(item.get("app") or "").strip()
    if cfg is None or not name:
        return None
    try:
        return radar.settings_of(cfg).projects.get(name)
    except Exception:  # noqa: BLE001 — réglages radar illisibles : pas de projet
        return None


def check(db, lot, *, repo: str | None = None, cfg=None, gh=None, full_ci=None) -> dict:
    """La porte d'une release : périmètre gelé, base non élargie, suite
    complète verte sur la **tête fraîche** de la branche. Certifier la seule
    tête gelée laisserait passer un commit postérieur au gel (correctif
    cherry-pické) sans exécution propre : la tête qui serait publiée est celle
    qui est certifiée. Rend le constat (`{lot, target, perimeter, base, moved,
    reason, frozen_sha, tip, certified, head_moved, full_suite}`) ou lève
    `ReleaseError` avec le geste qui manque."""
    from . import merge_queue
    row = work.get(db, lot)
    if row is None:
        raise ReleaseError("lot %s not found" % lot)
    repo = repo or lot_class.repository(cfg, row)
    if not repo:
        raise ReleaseError("lot #%s has no repository (radar.projects.%s.repo, or --repo)"
                           % (lot, row.get("app") or "?"))
    target = lot_class.default_base(cfg, row, repo)
    why = lot_class.release_reason(row, target)
    branch = str(row.get("branch") or "").strip()
    if not why and branch.startswith("release/"):
        # une PR de release (release/x.y -> main) peut n'être release que par sa
        # branche : la porte s'applique quand même (le geste est une publication)
        why = "release branch %s" % branch
    if not why:
        raise ReleaseError("lot #%s is not a release lot (%s)" % (lot, why or "no release axis"))
    frozen = perimeter(db, lot)
    if not frozen:
        raise ReleaseError("lot #%s has no frozen release perimeter: ameesh release open %s "
                           "--base <commit of %s>" % (lot, lot, target or "the target"))
    if not branch:
        raise ReleaseError("lot #%s has no branch" % lot)
    movement = base_moved(repo, branch, target, frozen)
    recorded = reason(db, lot)
    if movement["moved"] and not recorded:
        raise ReleaseError(
            "release branch %s absorbed %s after the freeze (perimeter %s, merge base %s): "
            "later merges go to the next release, only a fix for this one is cherry-picked "
            "(with its own impacted run). Record the reason if this is deliberate: "
            "ameesh release reason %s --text \"…\""
            % (branch, target, movement["perimeter"][:12], movement["base"][:12], lot))
    freeze = plan_git.latest_freeze(work.milestones(db, lot))
    if not freeze or not freeze.get("sha"):
        raise ReleaseError("lot #%s is not frozen: ameesh work milestone %s frozen --sha <commit>"
                           % (lot, lot))
    frozen_tip = merge_queue.pinned(repo, freeze["sha"])
    if frozen_tip is None:
        raise ReleaseError("the freeze of lot #%s names %r, not a commit of %s"
                           % (lot, freeze["sha"], repo))
    head = movement["tip"]
    project = project_of(cfg, row)
    try:
        runs = merge_queue.required_full_ci(project, head, full_ci, lot=lot)
    except merge_queue.QueueError as exc:
        raise ReleaseError(str(exc)) from exc
    return {"schema": "ameesh-release/1", "lot": lot, "target": target,
            "perimeter": movement["perimeter"],
            "branch": branch, "tip": head, "base": movement["base"],
            "moved": movement["moved"], "reason": recorded, "frozen_sha": frozen_tip,
            "certified": head, "head_moved": head != frozen_tip,
            "class_why": why, "full_suite": runs}


def render(report: dict) -> list[str]:
    """Les lignes de `ameesh release check` (une preuve lisible)."""
    runs = report.get("full_suite") or {}
    return [
        "lot #%s: release perimeter %s (%s)" % (report["lot"], report["perimeter"][:12],
                                                report["class_why"]),
        "  branch     %s -> %s, base %s%s" % (
            report["branch"], report["target"], report["base"][:12],
            " (MOVED after the freeze)" if report["moved"] else " (frozen)"),
        "  reason     %s" % (report["reason"] or "—"),
        "  frozen     %s" % report["frozen_sha"][:12],
        "  head       %s%s" % (report["tip"][:12],
                               " (MOVED after the freeze: certified itself)"
                               if report.get("head_moved") else ""),
        "  full suite %s/%s run(s), %s" % (runs.get("runs", 0), runs.get("suites", "?"),
                                           "green" if runs.get("green") else "NOT green"),
    ]


def _project_names(cfg, args) -> list[str]:
    from . import release_cutoff
    declared = release_cutoff.projects(cfg)
    if not declared:
        return []
    wanted = getattr(args, "project", None)
    if wanted:
        if wanted not in declared:
            raise release_cutoff.ReleaseError(
                "project %r is not declared under release.projects" % wanted)
        return [wanted]
    return sorted(declared)


def _dry_run(cfg, project, *, now, version, as_json: bool) -> int:
    from . import release_cutoff
    plan = release_cutoff.build_plan(cfg, project, now=now, version=version)
    report = {
        "schema": release_cutoff.SCHEMA, "mode": "dry-run",
        "project": project.name, "repo": project.repo, "target": project.target,
        "canon_commit": plan.canon_commit, "cutoff_at": plan.cutoff_at,
        "cutoff_label": plan.cutoff_label, "head": plan.head,
        "green": plan.green, "last_green": plan.last_green, "base": plan.base,
        "current_version": plan.current_version,
        "previous": plan.previous, "version": plan.version,
        "floor": plan.floor, "floor_reason": plan.floor_reason,
        "packets": [{"pr": packet.pr, "package": packet.package,
                     "units": packet.units, "manifest": packet.manifest,
                     "excluded": packet.excluded} for packet in plan.packets],
        "direct": plan.direct, "included": plan.included,
        "excluded": plan.excluded, "migrations": plan.migrations,
        "notes": plan.notes, "empty": plan.empty,
        "tree_green": plan.tree_green,
        "kept_active": plan.kept_active,
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    elif project.model == "nexlink":
        print("Nexlink %s — develop %s + main %s ; CI arbre final à produire%s"
              % (plan.version, plan.base[:12], plan.previous["commit"][:12],
                 " ; delta vide : branche créée sans staging" if plan.empty else ""))
    elif plan.kept_active:
        print("actif conservé — candidate %s plus ancienne ou divergente : aucun tag, "
              "aucun déploiement" % plan.base[:12])
    elif plan.empty:
        print("rien de nouveau — coupure %s — cible %s @ %s : base %s = %s, aucun tag"
              % (plan.cutoff_label, project.target, plan.head[:12], plan.base[:12],
                 plan.previous["tag"]))
    else:
        print("release %s — coupure %s — cible %s @ %s"
              % (plan.version, plan.cutoff_label, project.target, plan.head[:12]))
        if plan.tree_green:
            print("CI tête: %s ; arbre = tête de paquet %s, suite complète verte "
                  "(%s)" % (release_cutoff.ci_state_text(plan.ci),
                            plan.tree_green["head"][:12], ", ".join(
                                run["url"] or "run %s" % run["id"]
                                for run in plan.tree_green["runs"])))
        else:
            print("CI verte: %s%s" % ("oui" if plan.green else "non",
                                      "" if plan.green else
                                      " ; dernier vert " + (plan.last_green or "?")[:12]))
        print("paquets: %s" % (", ".join(
            "%s%s" % (packet.package, " (#%d)" % packet.pr if packet.pr else "")
            for packet in plan.packets) or "aucun"))
        print("lots inclus: %s" % (", ".join("#%d" % lot for lot in plan.included) or "aucun"))
        print("lots écartés: %s" % (", ".join("#%d" % lot for lot, _reason in plan.excluded)
                                    or "aucun"))
        print("migrations: %s" % (", ".join(plan.migrations) or "aucune"))
        print("notes:\n%s" % plan.notes)
    return 0


def _report_error(cfg, db, project, exc, now: float) -> None:
    """Mail a definitive error once; transient errors go to the journal only."""
    from . import release_cutoff
    if isinstance(exc, release_cutoff.TransientReleaseError):
        return
    try:
        state = release_cutoff.load_state(cfg)
        slot = release_cutoff.project_state(state, project.name)
        text = str(exc)
        try:
            rules, _source = release_cutoff.compiled_rules(cfg, project.name)
            cutoff_at, _label = release_cutoff.cutoff_slot(rules, now)
        except release_cutoff.ReleaseError:
            # A missing calendar can itself be the failure being reported.
            # Keep the previous once-per-error behavior until it is readable.
            cutoff_at, _label = None, ""
        if (slot.get("last_error") == text
                and slot.get("last_error_cutoff") == cutoff_at):
            return
        slot["last_error_cutoff"] = cutoff_at
        slot["last_error"] = text
        slot["last_error_at"] = now
        release_cutoff.save_state(cfg, state)
        if db is not None:
            body = ("Coupe Nexlink en attente : %s. État de reprise conservé ; "
                    "consulter pending_nexlink pour les branches, tags et runs déjà créés."
                    % text) if project.model == "nexlink" else (
                    "Release %s NON faite : %s\n\nAucun tag, aucun déploiement. "
                    "Corrige la cause ; le prochain tick réessaiera tant que la "
                    "coupure est dans sa fenêtre." % (project.name, text))
            report = {"schema": release_cutoff.SCHEMA, "result": "refused",
                      "project": project.name, "cutoff_at": cutoff_at,
                      "label": _label, "completed_at": now, "message": text}
            if cutoff_at is not None:
                report["report_path"] = release_cutoff.write_report(
                    cfg, project, dict(report, version="refused"))
            release_cutoff.send_report(cfg, db, project, body, report, urgent=True)
    except Exception:  # a report never masks the original error
        pass


def _release_tick(cfg, args, declared) -> int:
    """`release cutoff|tick` for the declared release projects (#2448)."""
    from . import release_cutoff
    try:
        names = _project_names(cfg, args)
    except release_cutoff.ReleaseError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    now = time.time() if args.now is None else float(args.now)
    if getattr(args, "dry_run", False):
        for name in names:
            try:
                _dry_run(cfg, declared[name], now=now,
                         version=getattr(args, "version", None), as_json=args.json)
            except release_cutoff.ReleaseError as exc:
                print("%s: %s" % (name, exc), file=sys.stderr)
                return 1
        return 0
    from .db import open_db
    db = open_db(cfg)
    try:
        status = 0
        for name in names:
            project = declared[name]
            try:
                report = release_cutoff.execute(
                    cfg, db, project, now=now, force=getattr(args, "force", False),
                    no_staging=getattr(args, "no_staging", False),
                    version=getattr(args, "version", None))
                if args.release_command == "tick":
                    from . import staging_pending
                    try:
                        report["staging_pending"] = staging_pending.tick(cfg, db, project, now=now)
                    except Exception as exc:
                        # An unreadable observation must not undo a completed
                        # cutoff or suppress the packet watcher on this tick.
                        print("staging-needed %s: %s" % (name, exc), file=sys.stderr)
                        report["staging_pending"] = {"result": "unreadable", "error": str(exc)}
                        status = 1
            except release_cutoff.TransientReleaseError as exc:
                print("%s: %s" % (name, exc), file=sys.stderr)
                status = 1
                continue
            except release_cutoff.ReleaseError as exc:
                print("%s: %s" % (name, exc), file=sys.stderr)
                _report_error(cfg, db, project, exc, now)
                status = 1
                continue
            if args.json:
                print(json.dumps(report, ensure_ascii=False, default=str))
            else:
                print("%s: %s%s" % (name, report.get("result"),
                                    " (%s)" % report.get("version")
                                    if report.get("version") else ""))
            if report.get("result") not in ("released", "reconciled", "nothing-new",
                                            "already-recorded", "not-due", "missed",
                                            "kept-active", "waiting-ci", "waiting-staging", "empty"):
                status = 1
        return status
    finally:
        db.close()


def _packet_watch(cfg, args) -> int:
    """#2601: the "missed slot" alert of the hourly packets, on the same tick.

    Never fatal for the release: a failure is printed and returns 1."""
    from . import packet_watch, release_cutoff
    try:
        declared = packet_watch.projects(cfg)
    except packet_watch.WatchError as exc:
        print("packet-watch: %s" % exc, file=sys.stderr)
        return 1
    if not declared:
        if args.release_command == "packet-watch":
            print("packet-watch: off (no packet_watch block for this host)")
        return 0
    now = time.time() if args.now is None else float(args.now)
    from .db import open_db
    try:
        db = open_db(cfg)
    except Exception as exc:  # noqa: BLE001 - base injoignable : rien à conclure
        print("packet-watch: %s" % exc, file=sys.stderr)
        return 1
    try:
        results = packet_watch.tick(
            cfg, db, now=now, dry_run=getattr(args, "dry_run", False),
            only=getattr(args, "project", None) if args.release_command == "packet-watch"
            else None,
            rules_of=lambda name: release_cutoff.compiled_rules(cfg, name)[0])
    except packet_watch.WatchError as exc:
        print("packet-watch: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()
    status = 0
    for result in results:
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, default=str))
        else:
            print("packet-watch %s: %s%s%s" % (
                result["project"], result["result"],
                " (%s)" % result["slot"] if result.get("slot") else "",
                " #%d" % result["lot"] if result.get("lot") else ""))
            if result.get("body"):
                print(result["body"])
        if result["result"] in ("error", "not-delivered"):
            print("packet-watch %s: %s" % (result["project"],
                                           result.get("error") or result.get("failed")),
                  file=sys.stderr)
            status = 1
    return status


def _elected_tick(cfg, args):
    """Return the elected wrapper status, or None inside its fenced child."""
    role = (getattr(cfg, "release", None) or {}).get("role")
    watch_role = (getattr(cfg, "packet_watch", None) or {}).get("role")
    if role and watch_role and role != watch_role:
        raise ReleaseError("release.role and packet_watch.role must agree")
    role = role or watch_role
    if not role:
        return None  # compatibility until the bureau migrates host units
    # Validate the independent staging route before acquiring an execution
    # lease: synchronous staging may restart the runner that owns this role.
    from . import release_cutoff
    projects = release_cutoff.projects(cfg)
    from . import role_run, pilot_gesture
    wrapped_role = (os.environ.get(role_run.ENV_ROLE) or "").strip()
    if wrapped_role and os.environ.get(role_run.ENV_PASS) == "release":
        from .db import open_db
        db = open_db(cfg)
        try:
            # The wrapper exports the canonical registry name, even when its
            # configured role was an alias. Never acquire a second pass here.
            if wrapped_role != role:
                configured_role = role_run.live(db, role)["role"]
                if configured_role != wrapped_role:
                    raise pilot_gesture.GestureRefused(
                        "release child does not match the configured role")
            fence = pilot_gesture.gate(cfg, db, "release", "release " + args.release_command)
            if fence is None:
                raise pilot_gesture.GestureRefused("release tick requires an elected role")
            # The runner alone renews the role; a timer must not keep a dead
            # runner elected. The wrapper watches its live epoch throughout.
        finally:
            db.close()
        return None
    command = [sys.executable, "-m", "ameesh", "release", args.release_command]
    for name in ("project", "now", "version", "actor"):
        value = getattr(args, name, None)
        if value is not None:
            command += ["--" + name, str(value)]
    for name in ("force", "no_staging", "json", "dry_run", "apply"):
        if getattr(args, name, False):
            command.append("--" + name.replace("_", "-"))
    selected = getattr(args, "project", None)
    credentials = frozenset().union(*(
        release_cutoff.child_credentials(project)
        for name, project in projects.items() if selected is None or name == selected))
    return role_run.run(cfg, argparse.Namespace(
        role=role, pass_name="release", command=command,
        min_left=0, dry_run=False, json=False), credentials=credentials)


def run(cfg, args) -> int:
    """`ameesh release open|check|reason|cutoff|tick|calendar|packet-watch`."""
    from . import release_cutoff
    command = args.release_command
    if (command in ("tick", "packet-watch", "cutoff")
            or command == "staging-needed" and getattr(args, "apply", False)):
        status = _elected_tick(cfg, args)
        if status is not None:
            return status
    if command == "staging-needed":
        from . import staging_pending
        from .db import open_db
        declared = release_cutoff.projects(cfg)
        names = _project_names(cfg, args)
        db = open_db(cfg)
        try:
            status = 0
            for name in names:
                if args.apply:
                    report = staging_pending.apply(cfg, db, declared[name], now=time.time(),
                                                   actor=args.actor)
                    if report["result"] not in ("staged", "below-threshold"):
                        status = 1
                else:
                    report = staging_pending.inspect(cfg, db, declared[name], now=time.time())
                print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
            return status
        finally:
            db.close()
    if command == "calendar":
        declared = release_cutoff.projects(cfg)
        if not declared:
            print("release: off (no release block for this host)")
            return 0
        now = time.time() if args.now is None else float(args.now)
        for name, project in sorted(declared.items()):
            try:
                rules, source = release_cutoff.compiled_rules(cfg, name)
                at, label = release_cutoff.cutoff_slot(rules, now)
                nxt = rules.next_cutoff(now)
                print("%s: canon %s, coupures %s, dernière %s, prochaine %s, cible %s"
                      % (name, source.commit[:12], ", ".join(rules.cutoffs), label,
                         _dt.datetime.fromtimestamp(nxt, rules.zone()).strftime("%d/%m %H:%M"),
                         project.target))
            except Exception as exc:
                print("%s: calendrier illisible (%s)" % (name, exc))
        return 0
    if command == "packet-watch":
        return _packet_watch(cfg, args)
    if command in ("cutoff", "tick"):
        declared = release_cutoff.projects(cfg)
        if not declared:
            print("release: off (no release block for this host)")
            return _packet_watch(cfg, args) if command == "tick" else 0
        if command == "tick":
            status = _release_tick(cfg, args, declared)
            return max(status, _packet_watch(cfg, args))
        return _release_tick(cfg, args, declared)
    from .db import open_db
    db = open_db(cfg)
    try:
        if command == "open":
            base = record_perimeter(db, args.id, args.base, actor=args.actor)
            print("lot #%d: release perimeter frozen at %s" % (args.id, base[:12]))
            return 0
        if command == "reason":
            text = record_reason(db, args.id, args.text, actor=args.actor)
            print("lot #%d: reason recorded (%s)" % (args.id, text))
            return 0
        report = check(db, args.id, repo=args.repo, cfg=cfg)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            print("\n".join(render(report)))
        return 0
    except ReleaseError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


def add_parser(sub, func) -> None:
    parser = sub.add_parser(
        "release", help="release perimeter and gate (L750/L756): freeze the base at a named "
                        "commit, refuse a moved base or a missing full suite")
    rel = parser.add_subparsers(dest="release_command", required=True)
    open_p = rel.add_parser(
        "open", help="freeze the release perimeter at a named commit of its target")
    open_p.add_argument("id", type=int)
    open_p.add_argument("--base", required=True,
                        help="commit of the target the release is cut from")
    open_p.add_argument("--actor", default="")
    open_p.set_defaults(func=func)
    check_p = rel.add_parser(
        "check", help="refuse a moved base without a reason, or a missing full-suite run")
    check_p.add_argument("id", type=int)
    check_p.add_argument("--repo", default=None, help="the project repository (default: its "
                                                      "declared radar.projects repository)")
    check_p.add_argument("--json", action="store_true", help="`ameesh-release/1` report")
    check_p.set_defaults(func=func)
    cutoff_p = rel.add_parser(
        "cutoff", help="release at a compiled canon cutoff: tag the green head, generate "
                       "notes, deploy staging, report to the owner (#2448)")
    cutoff_p.add_argument("--project", default=None)
    cutoff_p.add_argument("--now", type=float, default=None,
                          help="epoch seconds (default: now)")
    cutoff_p.add_argument("--force", action="store_true",
                          help="ignore the recorded cutoff and the recent-tag guard")
    cutoff_p.add_argument("--dry-run", action="store_true",
                          help="read and print the plan; no tag, no push, no staging")
    cutoff_p.add_argument("--no-staging", action="store_true")
    cutoff_p.add_argument("--version", default=None,
                          help="override the next patch version")
    cutoff_p.add_argument("--json", action="store_true")
    cutoff_p.set_defaults(func=func)
    tick_p = rel.add_parser(
        "tick", help="run the release when a compiled cutoff is due (systemd timer)")
    tick_p.add_argument("--project", default=None)
    tick_p.add_argument("--now", type=float, default=None)
    tick_p.add_argument("--force", action="store_true")
    tick_p.add_argument("--no-staging", action="store_true")
    tick_p.add_argument("--version", default=None)
    tick_p.add_argument("--json", action="store_true")
    tick_p.set_defaults(func=func)
    staging_p = rel.add_parser(
        "staging-needed", help="#3291: merged fixes holding lots before staging; "
                               "--apply runs staging without a tag or version")
    staging_p.add_argument("--project", default=None)
    staging_p.add_argument("--apply", action="store_true")
    staging_p.add_argument("--actor", default=None,
                           help="identified pilot/bureau, or human:<id> at a human terminal")
    staging_p.set_defaults(func=func)
    watch_p = rel.add_parser(
        "packet-watch", help="#2601: alert the composer and the pilot when a compiled "
                             "packet slot passes with ready lots and a free target "
                             "(also run by `tick`)")
    watch_p.add_argument("--project", default=None)
    watch_p.add_argument("--now", type=float, default=None)
    watch_p.add_argument("--dry-run", action="store_true",
                         help="print the alert; no mail, no state")
    watch_p.add_argument("--json", action="store_true")
    watch_p.set_defaults(func=func)
    calendar_p = rel.add_parser(
        "calendar", help="show the compiled cutoffs and the next one")
    calendar_p.add_argument("--now", type=float, default=None)
    calendar_p.set_defaults(func=func)
    reason_p = rel.add_parser(
        "reason", help="record why the release branch's base moved after the freeze")
    reason_p.add_argument("id", type=int)
    reason_p.add_argument("--text", required=True)
    reason_p.add_argument("--actor", default="")
    reason_p.set_defaults(func=func)
