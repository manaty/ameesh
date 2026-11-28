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
    wanted = getattr(args, "project", None)
    if wanted:
        # #6014: an explicit --project (a dedicated unit) must fail visibly
        # when the project is absent, even when no project is declared at all.
        if wanted not in declared:
            raise release_cutoff.ReleaseError(
                "project %r is not declared under release.projects" % wanted)
        return [wanted]
    return sorted(declared)


def _dry_run(cfg, project, *, now, version, as_json: bool) -> int:
    from . import release_cutoff
    # #6243: the preview settles the pending exactly as the tick would, so a
    # dry-run never announces a cut that `execute` only skips.
    provider = release_cutoff.release_provider(project.model)
    state = (release_cutoff.load_state(cfg)
             if getattr(provider, "STATEFUL", False) else None)
    plan = release_cutoff.build_plan(cfg, project, now=now, version=version,
                                     state=state)
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
        "result": "skipped" if plan.skipped else "planned",
        "skipped": plan.skipped, "skipped_empty": plan.skipped_empty,
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    elif getattr(provider, "plan_text", None) is not None:
        print(provider.plan_text(plan))
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


def _holder_identity(holder: dict) -> list:
    """#6584: what names a staging holder, never its renewed lease expiry."""
    return [str(holder.get("kind") or ""),
            bool(holder.get("active")),
            str(holder.get("host") or ""),
            str(holder.get("execution") or ""),
            str(holder.get("sha") or ""),
            str(holder.get("state") or ""),
            str(holder.get("blocked_reason") or "")]


def _alarm_key(text: str, holders: list[dict], tag: dict | None) -> str:
    """The identity of a release alarm, not its lease expiry (#6584).

    `staging_holders_text` names the bail's `expires_at`, renewed at every
    tick: comparing the raw text made the same alarm repeat every minute. The
    key keeps the cutoff's tag, the holders' identity (execution, SHA, state)
    and the cause, with the renewed expiry neutralized.
    """
    stable = re.sub(r"bail jusqu'à [^,;)]*", "bail jusqu'à …", str(text or ""))
    return json.dumps({"text": stable,
                       "tag": str((tag or {}).get("tag") or ""),
                       "holders": sorted(_holder_identity(holder)
                                         for holder in holders)},
                      ensure_ascii=False, sort_keys=True)


#: #8182: after a GitHub quota reset, let the recharge settle before retrying.
QUOTA_RESUME_MARGIN_S = 60.0


def _quota_probe_resource(text: str, refusal=None) -> str | None:
    """The API bucket a GitHub refusal may have exhausted, else None (#8182).

    The text and the refusal's body only route the probe: a GitHub 403/429 is
    graded by the shared headers, not by its wording (REST vs GraphQL). Other
    errors — a git timeout, a rights 404 — never buy a probe. The markers are
    the ones `plan_github.SharedReads.note` already uses. #8358: GraphQL's
    secondary limit can arrive as HTTP 200 with `submitted too quickly` in the
    body, so the refusal's body routes it to the `graphql` bucket as well.
    """
    from . import plan_github
    lowered = str(text or "").lower()
    body = ""
    if refusal is not None:
        try:
            body = str(refusal[2] or "").lower()
        except (IndexError, TypeError):
            body = ""
    blob = lowered + "\n" + body
    if ("github" not in blob and "gh:" not in blob and "gh api" not in blob
            and plan_github.GRAPHQL_SECONDARY_LIMIT_MARK not in blob):
        return None
    if not any(mark in blob for mark in (
            "http 403", "http 429", "api rate limit exceeded",
            plan_github.SECONDARY_LIMIT_MARK,
            plan_github.GRAPHQL_SECONDARY_LIMIT_MARK)):
        return None
    if (plan_github.GRAPHQL_SECONDARY_LIMIT_MARK in blob
            or "graphql:" in lowered):
        return "graphql"
    return "core"


def _quota_probe():
    """`gh api -i rate_limit`: the headers and body, spending no quota."""
    from . import plan_github
    return plan_github.gh_api_response(
        plan_github.Gh(timeout=15.0), "rate_limit")


def _quota_wait_until(cfg, text: str, now: float, refusal=None) -> float | None:
    """When the shared detector says GitHub is recharged, else None (#8182).

    The original refusal (status, headers, body, when the wrapper could keep
    it) is graded first: only it carries a secondary limit's `Retry-After`.
    #8358: GraphQL's secondary limit can be an HTTP 200 whose body says
    `submitted too quickly`; the refusal's body routes it to the `graphql`
    bucket, where `SharedReads.note` stores the wait. Then the `/rate_limit`
    probe covers a primary budget. Both go through
    `plan_github.SharedReads.note`, the detector the CI and GitHub readers
    already use; nothing here parses quota headers by hand.
    """
    from . import plan_github
    resource = _quota_probe_resource(text, refusal)
    if resource is None:
        return None
    state_dir = str(getattr(cfg, "state_dir", "") or "")
    if not state_dir:
        return None
    try:
        reads = plan_github.SharedReads(state_dir, priority="gate")
        until = reads.blocked(now, resource)
        if until is not None:
            return float(until)
        if refusal is not None:
            try:
                status, headers, body = refusal
            except (TypeError, ValueError):
                status = None
            else:
                until = reads.note(headers, status=status, now=now,
                                   resource=resource, body=body)
                if until is not None:
                    return float(until)
        status, headers, body = _quota_probe()
        if status == 200 and body:
            # The body names each bucket; the response headers only describe
            # the default core one. `note` still does the grading below.
            try:
                payload = json.loads(body)
                row = ((payload.get("resources") or {}).get(resource) or {})
            except (ValueError, AttributeError):
                row = {}
            if isinstance(row, dict) and row.get("remaining") == 0 \
                    and isinstance(row.get("reset"), (int, float)):
                headers = {"X-RateLimit-Remaining": "0",
                           "X-RateLimit-Reset": str(int(row["reset"])),
                           "X-RateLimit-Resource": resource}
        until = reads.note(headers, status=status, now=now,
                           resource=resource, body=body)
        return float(until) if until is not None else None
    except Exception:  # noqa: BLE001 - a quota probe never masks the failure
        return None


def _report_quota_wait(cfg, db, project, slot, state, text, cutoff_at, label,
                       now: float, reset: float) -> None:
    """One non-urgent notice per quota window, plus the durable wait (#8182).

    The wait is stored before mailing, so the next ticks skip the engine even
    if delivery fails; `release_cutoff.execute` reads it before dispatching to
    the model. The key is the reset instant: a new window may report again, a
    repeated failure in the same window never does.
    """
    from . import release_cutoff
    resume_at = max(float(reset), now) + QUOTA_RESUME_MARGIN_S
    key = "%d" % int(reset)
    if (slot.get("quota_wait_key") == key
            and float(slot.get("quota_wait_until") or 0) > now):
        return
    slot["quota_wait_key"] = key
    slot["quota_wait_reset"] = float(reset)
    slot["quota_wait_until"] = resume_at
    slot["quota_wait_at"] = now
    slot["last_error_cutoff"] = cutoff_at
    slot["last_error"] = "GitHub calls limited (window ends at %s)" % time.strftime(
        "%H:%M:%SZ", time.gmtime(reset))
    slot["last_error_at"] = now
    # A quota notice is not the previous alarm: a failure after the wait must
    # be able to speak again (the core model's key is not rewritten here).
    slot.pop("last_error_key", None)
    release_cutoff.save_state(cfg, state)
    if db is None:
        return
    when = time.strftime("%H:%M:%SZ", time.gmtime(reset))
    resume = time.strftime("%H:%M:%SZ", time.gmtime(resume_at))
    body = (
        "Coupe %s en attente : GitHub refuse les appels (quota épuisé ou "
        "limite secondaire ; fin de fenêtre à %s, marge de %ds). Reprise "
        "automatique à %s ; l'état de reprise est conservé et les gestes déjà "
        "faits restent acquis. Aucune action attendue.\n\n"
        "Détail : %s"
        % (project.name, when, int(QUOTA_RESUME_MARGIN_S), resume, text))
    report = {"schema": release_cutoff.SCHEMA, "result": "waiting-quota",
              "project": project.name, "cutoff_at": cutoff_at, "label": label,
              "completed_at": now, "reset_at": float(reset),
              "resume_at": resume_at, "message": text}
    if cutoff_at is not None:
        # The report keeps the pending candidate's version when the model's
        # provider knows it (#7660: no client state key in the core).
        version = "quota-wait"
        try:
            provider = release_cutoff.release_provider(project.model)
            reader = getattr(provider, "pending_version", None)
            if callable(reader):
                version = str(reader(slot) or version)
        except Exception:  # noqa: BLE001 - a report name never masks the wait
            pass
        try:
            report["report_path"] = release_cutoff.write_report(
                cfg, project, dict(report, version=version))
        except Exception:  # noqa: BLE001 - the alert still goes out
            pass
    release_cutoff.send_report(cfg, db, project, body, report, urgent=False)


def _report_error(cfg, db, project, exc, now: float) -> bool:
    """Mail an error once per cutoff, transient or definitive (#6014).

    A transient error used to reach the journal only: a cutoff could fail
    every tick of its window and leave no trace outside the release host.
    The durable `last_error` is still kept for the missed report's cause, and
    the de-duplication stays per cutoff so a retry loop mails once.

    #6520: before alarming, read the cutoff's tag and the staging holder. A
    tag whose own staging holds the lock is information, never "aucun tag".
    #6584: the de-duplication key is the cutoff and the holders' identity,
    never the lease expiry renewed at every tick.
    #8182: a GitHub quota rejection is not a failed cutoff: the reset is
    stored and one non-urgent notice replaces the per-tick urgent event.
    Returns True when the failure was a quota wait (not a failure).
    """
    from . import release_cutoff
    if getattr(exc, "already_reported", False):
        # #6039 N8: the raiser already sent the one coherent message (refused
        # preflight closes its pending; there is no state to resume).
        return False
    transient = isinstance(exc, release_cutoff.TransientReleaseError)
    busy = isinstance(exc, release_cutoff.ReleaseBusy)
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
        quota_reset = _quota_wait_until(
            cfg, text, now, getattr(exc, "github_refusal", None))
        if quota_reset is not None:
            _report_quota_wait(cfg, db, project, slot, state, text,
                               cutoff_at, _label, now, quota_reset)
            return True
        if project.model in release_cutoff.CORE_RELEASE_MODELS:
            # #6520: before alarming, read the cutoff's tag and who holds the
            # staging door. A tag published at/after the cutoff is a durable
            # fact: never say "aucun tag" again, and a lock held by that tag's
            # own staging is information, not an incident.
            tag = (release_cutoff.cutoff_tag(project, cutoff_at, now)
                   if cutoff_at is not None else None)
            holders = []
            if busy:
                try:
                    holders = release_cutoff.staging_holders(project, db)
                except Exception:  # noqa: BLE001 - diagnostics never mask the error
                    holders = []
            same = [holder for holder in holders
                    if tag and holder.get("active")
                    and holder.get("sha") == tag["commit"]]
            # #6584: the lease's expires_at is renewed at every tick; it is
            # not a new cause. Compare the holder identity, never the expiry.
            alarm_key = _alarm_key(text, holders, tag)
            if busy and "another release holds" in text:
                # The running tick on this host owns the cutoff and reports
                # it; a lock contention is not a release failure.
                return False
            if tag and busy and same:
                if (slot.get("staging_info_key") == alarm_key
                        and slot.get("staging_info_cutoff") == cutoff_at):
                    return False
                slot["staging_info_cutoff"] = cutoff_at
                slot["staging_info_key"] = alarm_key
                slot["staging_info"] = text
                slot["staging_info_at"] = now
                release_cutoff.save_state(cfg, state)
                if db is not None:
                    by = ((" par %s" % same[0]["host"])
                          if same[0].get("host") else "")
                    pending = str(same[0].get("state") or "") == "pending"
                    body = ("Coupure %s faite%s : tag %s sur %s. Staging %s "
                            "(%s).\n\nRien à corriger : le staging de cette coupure "
                            "est %s et aucun second tag ne sera posé. "
                            "L'attestation suivra."
                            % (tag["version"], by, tag["tag"], tag["commit"][:12],
                               "en attente" if pending else "en cours",
                               release_cutoff.staging_holder_text(same[0]),
                               "en file" if pending else "déjà lancé"))
                    report = {"schema": release_cutoff.SCHEMA, "result": "staging-info",
                              "project": project.name, "cutoff_at": cutoff_at,
                              "label": _label, "completed_at": now, "message": text,
                              "tag": tag["tag"], "version": tag["version"],
                              "release_sha": tag["commit"],
                              "staging_holders": holders}
                    if cutoff_at is not None:
                        report["report_path"] = release_cutoff.write_report(
                            cfg, project, dict(report, version=tag["version"]))
                    release_cutoff.send_report(cfg, db, project, body, report,
                                               urgent=False)
                return False
            if (slot.get("last_error_key") == alarm_key
                    and slot.get("last_error_cutoff") == cutoff_at):
                return False
            slot["last_error_cutoff"] = cutoff_at
            slot["last_error_key"] = alarm_key
            slot["last_error"] = text
            slot["last_error_at"] = now
            release_cutoff.save_state(cfg, state)
            if db is not None:
                detail = (" Le staging est tenu par %s."
                          % release_cutoff.staging_holders_text(holders)
                          if holders else " Détenteur du verrou non lisible.")
                if tag:
                    body = ("Release %s : coupure %s déjà taguée (%s sur %s). %s%s\n\n"
                            "Aucun second tag ne sera posé ; corriger la cause nommée."
                            % (tag["version"], _label, tag["tag"],
                               tag["commit"][:12], text, detail))
                else:
                    body = ("Release %s NON faite%s : %s%s\n\n"
                            "Aucun tag, aucun déploiement. Corrige la cause ; le "
                            "prochain tick réessaiera tant que la coupure est dans "
                            "sa fenêtre."
                            % (project.name,
                               " (erreur transitoire)" if transient else "",
                               text, detail))
                report = {"schema": release_cutoff.SCHEMA, "result": "refused",
                          "project": project.name, "cutoff_at": cutoff_at,
                          "label": _label, "completed_at": now, "message": text,
                          "staging_holders": holders}
                if tag:
                    report.update(tag=tag["tag"], version=tag["version"],
                                  release_sha=tag["commit"])
                if transient:
                    report["transient"] = True
                if cutoff_at is not None:
                    report["report_path"] = release_cutoff.write_report(
                        cfg, project, dict(report,
                                           version=tag["version"] if tag else "refused"))
                release_cutoff.send_report(cfg, db, project, body, report, urgent=True)
            return False
        if (slot.get("last_error") == text
                and slot.get("last_error_cutoff") == cutoff_at):
            return False
        slot["last_error_cutoff"] = cutoff_at
        slot["last_error"] = text
        slot["last_error_at"] = now
        release_cutoff.save_state(cfg, state)
        if db is not None:
            provider = None
            if project.model not in release_cutoff.CORE_RELEASE_MODELS:
                try:
                    provider = release_cutoff.release_provider(project.model)
                except release_cutoff.ReleaseError:
                    provider = None       # the model itself is the reported error
            if getattr(provider, "error_body", None) is not None:
                body = provider.error_body(slot, text, transient)
            else:
                body = ("Release %s NON faite%s : %s\n\nAucun tag, aucun déploiement. "
                        "Corrige la cause ; le prochain tick réessaiera tant que la "
                        "coupure est dans sa fenêtre."
                        % (project.name, " (erreur transitoire)" if transient else "", text))
            report = {"schema": release_cutoff.SCHEMA, "result": "refused",
                      "project": project.name, "cutoff_at": cutoff_at,
                      "label": _label, "completed_at": now, "message": text}
            if transient:
                report["transient"] = True
            if cutoff_at is not None:
                report["report_path"] = release_cutoff.write_report(
                    cfg, project, dict(report, version="refused"))
            release_cutoff.send_report(cfg, db, project, body, report, urgent=True)
    except Exception:  # a report never masks the original error
        pass
    return False


CONFIG_ERROR_INTERVAL_S = 6 * 3600.0


def _report_configuration_error(cfg, name: str, exc, now: float | None = None) -> None:
    """#6014: an explicit release project that this host cannot cut.

    `release tick --project nexlink` used to fail in the journal alone when
    the host's `release.projects` did not declare it — exactly the state that
    left the 05/11 cutoffs without a tag and without a report. Say it once
    per interval, in the project's thread and to its pilot.
    """
    from . import release_cutoff
    now = time.time() if now is None else float(now)
    try:
        state = release_cutoff.load_state(cfg)
        slot = release_cutoff.project_state(state, name)
        if now - float(slot.get("last_config_error_at") or 0) < CONFIG_ERROR_INTERVAL_S:
            return
        slot["last_config_error_at"] = now
        slot["last_config_error"] = str(exc)
        release_cutoff.save_state(cfg, state)
    except Exception as error:  # noqa: BLE001 - a report never masks the error
        print("release: configuration error not recorded: %s" % error, file=sys.stderr)
        return
    try:
        from .db import open_db
        db = open_db(cfg)
    except Exception as error:  # noqa: BLE001 - no database, no message
        print("release: configuration error not delivered: %s" % error, file=sys.stderr)
        return
    try:
        project = release_cutoff.ProjectRelease(name, repo="", target="", github="")
        body = ("Coupe %s impossible sur cet hôte : %s.\n\n"
                "Le projet n'est pas déclaré dans release.projects de %s ; "
                "aucune coupure ne part et le chemin « missed » ne peut pas "
                "s'appliquer. Ajouter le bloc (repo, github, target, model) ou "
                "retirer l'unité qui demande ce projet."
                % (name, exc, str(getattr(cfg, "host", "") or "cet hôte")))
        report = {"schema": release_cutoff.SCHEMA,
                  "result": "configuration-error", "project": name,
                  "completed_at": now, "message": str(exc),
                  "host": str(getattr(cfg, "host", "") or "")}
        release_cutoff.send_report(cfg, db, project, body, report, urgent=True)
    finally:
        db.close()


def _release_tick(cfg, args, declared) -> int:
    """`release cutoff|tick` for the declared release projects (#2448)."""
    from . import release_cutoff
    try:
        names = _project_names(cfg, args)
    except release_cutoff.ReleaseError as exc:
        print("error: %s" % exc, file=sys.stderr)
        if getattr(args, "project", None):
            _report_configuration_error(cfg, str(args.project), exc)
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
                    # #7365 : un serveur reporté à chaque coupure est un
                    # retard de version, pas un état ; la garde le déclare au
                    # pilote et à l'avatar une fois par release.
                    from . import staging_deferred
                    try:
                        report["staging_deferred"] = staging_deferred.tick(cfg, db, project)
                    except Exception as exc:
                        print("staging-deferred %s: %s" % (name, exc), file=sys.stderr)
                        report["staging_deferred"] = {"result": "unreadable", "error": str(exc)}
                        status = 1
                    # #7666 : la stable de la semaine, désignée et publiée
                    # une fois ; elle ne défait jamais la coupure.
                    if project.stable_channel:
                        from . import stable_channel
                        try:
                            report["stable"] = stable_channel.tick(cfg, db, project, now=now)
                        except Exception as exc:
                            print("stable %s: %s" % (name, exc), file=sys.stderr)
                            report["stable"] = {"result": "error", "error": str(exc)}
                            status = 1
            except release_cutoff.TransientReleaseError as exc:
                print("%s: %s" % (name, exc), file=sys.stderr)
                # #6014: a transient failure is still a cutoff not done; say
                # it once per cutoff instead of leaving it in the journal.
                # #8182: a quota wait is scheduled, not a failed cutoff, but
                # it never clears a failure an earlier project recorded.
                if not _report_error(cfg, db, project, exc, now):
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
                                            "kept-active", "waiting-ci", "waiting-staging",
                                            "empty", "skipped", "waiting-attestation",
                                            "held-attestation", "staging-unattested",
                                            "waiting-quota"):
                status = 1
        return status
    finally:
        db.close()


def _stable(cfg, args) -> int:
    """#7666: `release stable designate|publish|pin|verify`."""
    from . import release_cutoff, stable_channel
    command = args.stable_command
    try:
        if command in ("pin", "verify"):
            # the consumer side: any clone of the project, no release block
            repo = os.path.abspath(os.path.expanduser(args.repo))
            if command == "pin":
                sys.stdout.write(stable_channel.pin(repo, args.remote, args.week))
                return 0
            with open(args.file, encoding="utf-8") as stream:
                text = stream.read()
            installed = None
            if args.installed:
                from importlib import metadata
                installed = metadata.version("ameesh")
            manifest = stable_channel.verify(text, repo, args.remote, installed=installed)
            print("%s: stable %s = %s (%s)%s"
                  % (args.file, manifest["week"], manifest["tag"], manifest["commit"][:12],
                     ", installed" if installed else ""))
            return 0
        declared = release_cutoff.projects(cfg)
        if args.project not in declared:
            raise release_cutoff.ReleaseError(
                "project %r is not declared under release.projects" % args.project)
        project = declared[args.project]
        now = time.time() if args.now is None else float(args.now)
        from .db import open_db
        db = open_db(cfg)
        try:
            report = stable_channel.tick(cfg, db, project, now=now,
                                         decision=getattr(args, "decision", None),
                                         dry_run=command == "designate")
        finally:
            db.close()
    except (release_cutoff.ReleaseError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("result") not in ("error",) else 1


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
        return None  # compatibility until the avatar migrates host units
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


def _catchup_actor(cfg, db, explicit, right=None) -> str:
    """#7372: the right's holder, the avatar, or a human at a terminal declares.

    An agent session must be *bound* (`identity.require_bound`: runner lease or
    session binding), never a bare `AGENT_MAIL_NAME`, which is self-declared
    (#7372 B1); it then acts as itself and must be the avatar (its Agent card
    names the avatar block; `tenancy.operator` applies the same rule for infrastructure
    gestures). `--actor` never lets an agent act on behalf of another, and an
    unbound session may only name a known human. A root catch-up is not a
    generic agent gesture: the owner's decision (#219407 a) names its holder.
    #7788: the bound session that is the beneficiary of the active ``right``
    (`staging_deferred.active_right`: signed grant, scope, expiry, revocation)
    declares as itself; an expired or revoked right names nobody.
    """
    from . import agent_names, avatars, canon, identity, release_cutoff
    text = agent_names.resolve((explicit or "").strip(), db=db,
                               operation="release.catchup")
    binding = identity.resolve_binding(cfg, db)
    name = binding.name or ""
    if os.environ.get("AGENT_MAIL_NAME") and not name:
        raise release_cutoff.ReleaseError(
            "catchup identity refused: %s" % (binding.reason or "invalid session identity"))
    if name and not name.startswith(work.HUMAN_PREFIX):
        try:
            bound = identity.require_bound(cfg, db, what="release catchup")
        except ValueError as exc:
            raise release_cutoff.ReleaseError(
                "catchup identity refused: %s" % exc) from exc
        name = bound.name
        if text and text != name:
            raise release_cutoff.ReleaseError(
                "this session belongs to %s; it does not declare on behalf of %s"
                % (name, text))
        if (right and right.get("active")
                and str(right.get("beneficiary") or "") == name):
            return name
        try:
            canons = canon.load_configured(cfg) if cfg is not None else []
            if avatars.is_avatar_in(canons, name):
                return name
        except Exception as exc:  # noqa: BLE001 - an unreadable authority refuses
            raise release_cutoff.ReleaseError(
                "cannot read the avatar of %s: %s" % (name, exc)) from exc
        raise release_cutoff.ReleaseError(
            "%s is neither the holder of an active root right on this host "
            "nor an avatar (#219407 a)" % name)
    if name:
        # A human name must come from a bound terminal. A bare
        # `AGENT_MAIL_NAME=human:…` is self-declared like any agent name; the
        # unbound path below still accepts `--actor human:<id>` on a terminal.
        if not binding.ok or binding.source not in identity.BOUND_SOURCES:
            raise release_cutoff.ReleaseError(
                "catchup identity refused: %s is not a bound human terminal; an "
                "unbound terminal names itself with --actor human:<id>" % name)
        if text and text != name:
            raise release_cutoff.ReleaseError(
                "this session belongs to %s; it does not declare on behalf of %s"
                % (name, text))
        return name
    if not text:
        raise release_cutoff.ReleaseError(
            "no bound session identity: a human at a terminal names itself with "
            "--actor human:<id>; an agent session must be bound to an avatar")
    if not text.startswith(work.HUMAN_PREFIX):
        raise release_cutoff.ReleaseError(
            "an unbound session cannot declare as %r: only a bound avatar or a "
            "human terminal" % text)
    human = work.known_human(db, text[len(work.HUMAN_PREFIX):], cfg)
    if not human:
        raise release_cutoff.ReleaseError("%s is not a known human" % text)
    return human


def _catchup(cfg, args) -> int:
    """`ameesh release catchup` (#7372): declare the avatar's root gesture.

    The four `vm.sh` steps stay the avatar's manual gesture; this command
    records its outcome as a durable `staging.catchup` event, with the host,
    the active commit read from `controler`, the attested tag (the release
    tags are refreshed from the remote first, #8188), the author and the
    owner right read from the project configuration. A declaration is a
    trace, not a proof: the guard keeps the lag until a staging attestation
    reads the host. `ok` is refused after the right's expiry or if its signed
    decision is withdrawn or superseded.
    """
    from . import release_cutoff, staging_deferred
    from .db import open_db
    try:
        project = release_cutoff.projects(cfg).get(args.project)
        if project is None:
            raise release_cutoff.ReleaseError(
                "project %r is not declared under release.projects" % args.project)
        # #7662: `projects(cfg)` starts from the bare profile, whose empty
        # `StagingTopology()` exists but has no host. Fill it from the canon
        # before reading the default server, as the other release paths do.
        project = release_cutoff.with_staging_topology(cfg, project)
        db = open_db(cfg)
        try:
            host = getattr(args, "host", None)
            if not host:
                topology = getattr(project, "staging_topology", None)
                if topology is None or not topology.hosts:
                    topology = release_cutoff.topology_for(db)
                host = topology.server
            if not host:
                raise release_cutoff.ReleaseError(
                    "no --host and the canon declares no staging server: name "
                    "the reported host explicitly")
            base = staging_deferred.baseline(db, project, host)
            if base is None:
                print("%s: %s is not reported by the latest tagged staging "
                      "attestation; nothing to declare" % (project.name, host))
                return 0
            right = staging_deferred.active_right(db, project, host)
            if args.result == "ok" and (right is None or not right.get("active")):
                raise release_cutoff.ReleaseError(
                    "no active owner root right covers %s: %s"
                    % (host, "decision #%s %s"
                       % (right.get("decision"), right.get("why") or "inactive")
                       if right else "none configured in release.projects.%s."
                       "staging_rights" % project.name))
            actor = _catchup_actor(cfg, db, args.actor, right)
            row = staging_deferred.declare(
                db, project, host, args.sha, base, args.result, actor, args.note)
        finally:
            db.close()
    except (release_cutoff.ReleaseError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("staging catch-up declared: %s %s at %s (%s, tag %s, event %s); "
          "trace only, the alert stays until an attestation proves the host"
          % (project.name, row["host"], row["sha"][:12], row["result"],
             row["tag"] or "?", row.get("event")))
    return 0


def _project_name(cfg, args) -> str:
    """The project named by ``--project``, else the configured default (#7813).

    The core compiles no client project name: the explicit ``--project`` wins,
    else ``release.default_project`` must name it, else the caller must choose
    (#7813 B2: a sole project is not inferred)."""
    from . import release_cutoff
    if args.project:
        return args.project
    if cfg is None:
        raise release_cutoff.ReleaseError("--project is required")
    return release_cutoff.default_project(cfg)


def _attest(cfg, args) -> int:
    """`ameesh release attest` (#6020): a reserved note on the attestation lot.

    The journal author is what the release tick trusts: the session's bound
    identity for an attestation (the tick keeps only the `staging_handoff`
    agent's), a verified pilot for `--skip` (`work.corrector`). `--actor` is
    never an author: it only names the human at a terminal for `--skip`.
    """
    from . import identity, release_cutoff
    from .db import open_db
    db = open_db(cfg)
    try:
        name = _project_name(cfg, args)
        declared = release_cutoff.projects(cfg) if cfg is not None else {}
        project = declared.get(name)
        model = project.model if project is not None else name
        provider = release_cutoff.release_provider(model)
        if provider is None:
            raise release_cutoff.ReleaseError(
                "release attest is not provided by release model %r" % model)
        provider.version_key(args.version)
        if args.skip:
            if args.sha or args.run or args.migration:
                raise release_cutoff.ReleaseError("--skip takes no --sha, --run or --migration")
            if not (args.reason or "").strip():
                raise release_cutoff.ReleaseError("--skip requires --reason")
            actor = work.corrector(cfg, db, args.actor, what="release attest --skip",
                                   history=False)
            text = provider.skip_text(name, args.version, args.reason)
        else:
            if args.actor:
                raise release_cutoff.ReleaseError(
                    "an attestation is signed by the session's own identity, not --actor")
            binding = identity.resolve_binding(cfg, db)
            if not (binding.ok and binding.name):
                raise release_cutoff.ReleaseError(
                    "attestation refused: no bound session identity (%s)"
                    % (binding.reason or "unbound"))
            actor = binding.name
            if not args.sha or not re.fullmatch(r"[0-9a-f]{40}", args.sha):
                raise release_cutoff.ReleaseError("--sha: the full tagged commit is required")
            text = provider.attestation_text(
                name, args.version, sha=args.sha,
                runs=provider.parse_runs(args.run),
                migrations=provider.parse_migrations(args.migration))
        lot = provider.write_decision(db, name, args.version,
                                      "skipped" if args.skip else "attested",
                                      text, actor)
        print("release %s %s: %s noted on lot #%d by %s; the release tick checks it"
              % (name, args.version, "skip" if args.skip else "attestation",
                 lot, actor))
        return 0
    except (work.WorkError, release_cutoff.ReleaseError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()


#: #7584: the abandon trace carries the hour of the gesture. A caller may
#: repeat the clock, never forge it: an explicit --now farther from the real
#: clock than this window is refused, and the trace keeps the real hour.
ABANDON_NOW_WINDOW_S = 300.0


def _abandon_moment(args) -> float:
    """The real hour of the abandon gesture (#7584)."""
    from . import release_cutoff
    real = time.time()
    if args.now is None:
        return real
    try:
        given = float(args.now)
    except (TypeError, ValueError) as exc:
        raise release_cutoff.ReleaseError(
            "--now must be an epoch in seconds") from exc
    if abs(given - real) > ABANDON_NOW_WINDOW_S:
        raise release_cutoff.ReleaseError(
            "--now is %+d s from the real clock: the abandon trace carries the "
            "hour of the gesture (at most %d s apart)"
            % (round(given - real), ABANDON_NOW_WINDOW_S))
    return real


def _require_elected_release_host(cfg, db) -> None:
    """#7584: refuse the local-state gesture outside the elected release host.

    The release state is host-local (``cfg.state_dir``). Outside the host that
    holds the release role's live lease, the command would read and rewrite
    another host's stale state, never the engine's. A host with no configured
    role keeps the historical host-pinned gesture. The check is direct, not
    through `_elected_tick`: that is the timer entry and strips the session
    identity, while this pilot gesture still has to prove itself through
    `work.corrector`.
    """
    from . import release_cutoff, role_run
    role = (getattr(cfg, "release", None) or {}).get("role")
    watch_role = (getattr(cfg, "packet_watch", None) or {}).get("role")
    if role and watch_role and role != watch_role:
        raise release_cutoff.ReleaseError(
            "release.role and packet_watch.role must agree")
    role = role or watch_role
    if not role:
        return
    here = str(getattr(cfg, "host", "") or "").strip()
    try:
        report = role_run.live(db, role)
    except role_run.RoleError as exc:
        raise release_cutoff.ReleaseError(
            "release abandon refused: the release role %r has no readable "
            "election (%s)" % (role, exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a broken read must not pass
        raise release_cutoff.ReleaseError(
            "release abandon refused: the release role %r is unreadable (%s)"
            % (role, exc)) from exc
    if not report.get("held") or str(report.get("host") or "") != here:
        raise release_cutoff.ReleaseError(
            "release abandon must run on the elected release host %s (role %s): "
            "the release state is local to that host, and this host is %s"
            % (report.get("host") or "none", role, here or "unnamed"))


def _abandon(cfg, args) -> int:
    """`ameesh release abandon` (#7567): the pilot closes a red-CI cutoff.

    A frozen Nexlink cutoff whose final-tree CI part is conclusively red has
    no automatic exit: the engine re-reads the same run at every tick. This
    gesture takes the release lock, runs only on the host that holds the
    release role's live lease (#7584: the release state is local), proves the
    pilot through `work.corrector` (a human terminal names itself with
    `--actor human:<id>`), then lets the selected module provider's `abandon`
    refuse an already published tag, a staging handover or a non-conclusive CI
    part before writing the durable trace and announcing to the release
    recipients.
    """
    from . import release_cutoff
    from .db import open_db
    db = None
    try:
        declared = release_cutoff.projects(cfg)
        name = _project_name(cfg, args)
        if name not in declared:
            raise release_cutoff.ReleaseError(
                "project %r is not declared under release.projects" % name)
        project = declared[name]
        provider = release_cutoff.release_provider(project.model)
        if provider is None or not callable(getattr(provider, "abandon", None)):
            raise release_cutoff.ReleaseError(
                "release abandon is not provided by release model %r" % project.model)
        moment = _abandon_moment(args)
        db = open_db(cfg)
        _require_elected_release_host(cfg, db)
        actor = work.corrector(cfg, db, args.actor, what="release abandon",
                               history=False)
        report = provider.abandon(
            cfg, db, project, now=moment, reason=args.reason, actor=actor)
    except (work.WorkError, release_cutoff.ReleaseError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
    elif getattr(provider, "abandon_text", None) is not None:
        print(provider.abandon_text(report, project.name))
    return 0



def _retag(cfg, args) -> int:
    from . import release_cutoff
    from .db import open_db
    db = None
    try:
        projects = release_cutoff.projects(cfg)
        name = _project_name(cfg, args)
        if name not in projects:
            raise release_cutoff.ReleaseError("project %r is not declared under release.projects" % name)
        project = projects[name]
        provider = release_cutoff.release_provider(project.model)
        if provider is None or not callable(getattr(provider, "retag", None)):
            raise release_cutoff.ReleaseError("release retag is not provided by this release model")
        db = open_db(cfg)
        _require_elected_release_host(cfg, db)
        actor = work.corrector(cfg, db, args.actor, what="release retag", history=False)
        report = provider.retag(cfg, db, project, version=args.version, sha=args.sha,
                                reason=args.reason, actor=actor, now=time.time())
        print(json.dumps(report, ensure_ascii=False, default=str))
        return 0
    except (work.WorkError, release_cutoff.ReleaseError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()

def run(cfg, args) -> int:
    """`ameesh release open|check|reason|cutoff|tick|calendar|packet-watch|attest|abandon`."""
    from . import release_cutoff
    command = args.release_command
    if command == "stable":
        return _stable(cfg, args)
    if command == "catchup":
        return _catchup(cfg, args)
    if command == "attest":
        return _attest(cfg, args)
    if command == "retag":
        return _retag(cfg, args)
    if command == "abandon":
        return _abandon(cfg, args)
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
                    if report["result"] not in ("staged", "queued",
                                                "below-threshold"):
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
                           help="identified pilot/avatar, or human:<id> at a human terminal")
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
    catchup_p = rel.add_parser(
        "catchup", help="#7372: declare the avatar's root catch-up of a reported "
                        "staging server (four vm.sh steps) in the trace and the "
                        "cutoff report")
    catchup_p.add_argument("--project", default="ameesh")
    catchup_p.add_argument("--host", default=None,
                           help="reported staging server; defaults to the server "
                                "declared by the canon's Host cards (#7662)")
    catchup_p.add_argument("--sha", required=True,
                           help="the host's active commit after the gesture (full 40 hex, "
                                "read from vm.sh controler)")
    catchup_p.add_argument("--result", choices=("ok", "failed"), default="ok")
    catchup_p.add_argument("--note", default="",
                           help="what happened, for a failure or a partial gesture")
    catchup_p.add_argument("--actor", default="",
                           help="a human terminal names itself (human:<id>); a bound "
                                "session acts as itself and must hold the active root "
                                "right (#7788) or be an avatar")
    catchup_p.set_defaults(func=func)
    attest_p = rel.add_parser(
        "attest", help="#6020: the staging agent attests a handed-over release staging "
                       "(migrations, then the staging dispatches) on the cutoff's "
                       "attestation lot; --skip: the pilot stops waiting")
    attest_p.add_argument("--project", default=None,
                          help="release project; default: release.default_project, "
                               "else required")
    attest_p.add_argument("--version", required=True)
    attest_p.add_argument("--sha", default=None, help="the tagged commit")
    attest_p.add_argument("--run", action="append", default=[],
                          help="WORKFLOW=RUN_ID, once per staging workflow")
    attest_p.add_argument("--migration", action="append", default=[],
                          help="TARGET:VERSION:SHA256 applied on staging (hub, content)")
    attest_p.add_argument("--skip", action="store_true",
                          help="pilot only: close the held cutoff without staging")
    attest_p.add_argument("--reason", default="")
    attest_p.add_argument("--actor", default=None,
                          help="--skip at a human terminal: human:<id>")
    attest_p.set_defaults(func=func)
    retag_p = rel.add_parser("retag", help="declare a verified retagged release cutoff")
    retag_p.add_argument("--project", default=None)
    retag_p.add_argument("--version", required=True)
    retag_p.add_argument("--sha", required=True)
    retag_p.add_argument("--reason", required=True)
    retag_p.add_argument("--actor", default=None)
    retag_p.set_defaults(func=func)
    abandon_p = rel.add_parser(
        "abandon", help="#7567: the verified pilot abandons a frozen release cutoff "
                        "whose final-tree CI part is conclusively red; no tag, no "
                        "staging, no deployment; #7584: only on the elected release "
                        "host")
    abandon_p.add_argument("--project", default=None,
                           help="release project; default: release.default_project, "
                                "else required")
    abandon_p.add_argument("--reason", required=True)
    abandon_p.add_argument("--actor", default=None,
                           help="at a human terminal: human:<id>")
    abandon_p.add_argument("--now", type=float, default=None,
                           help="epoch seconds: accepted only within %d s of the "
                                "real clock; the trace keeps the hour of the "
                                "gesture" % ABANDON_NOW_WINDOW_S)
    abandon_p.add_argument("--json", action="store_true")
    abandon_p.set_defaults(func=func)
    stable_p = rel.add_parser(
        "stable", help="#7666: weekly stable channel (designation, publication) and the "
                       "consumer's single pin file (%s)" % "ameesh-stable.json")
    stable = stable_p.add_subparsers(dest="stable_command", required=True)
    designate_p = stable.add_parser(
        "designate", help="print the due week's designation; no tag, no push")
    publish_p = stable.add_parser(
        "publish", help="designate and publish the due week (tag stable/<week>); the "
                        "first one needs a signed owner decision")
    publish_p.add_argument("--decision", type=int, default=None,
                           help="owner decision approving the first public stable "
                                "(option publish)")
    for each in (designate_p, publish_p):
        each.add_argument("--project", required=True)
        each.add_argument("--now", type=float, default=None,
                          help="epoch seconds (default: now)")
        each.set_defaults(func=func)
    pin_p = stable.add_parser(
        "pin", help="print the pin file of a published stable week (default: the latest)")
    pin_p.add_argument("--week", default=None, help="YYYY-Www")
    verify_p = stable.add_parser(
        "verify", help="check a pin file against the published channel")
    verify_p.add_argument("file", help="the pin file (ameesh-stable.json)")
    verify_p.add_argument("--installed", action="store_true",
                          help="also require the running ameesh installation's version")
    for each in (pin_p, verify_p):
        each.add_argument("--repo", default=".",
                          help="any git clone of the project (default: .)")
        each.add_argument("--remote", default="origin",
                          help="the publishing repository: a remote of --repo or a "
                               "URL (default: origin)")
        each.set_defaults(func=func)
