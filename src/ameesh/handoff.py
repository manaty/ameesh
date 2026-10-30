# SPDX-License-Identifier: AGPL-3.0-only
"""L233b / L165 : les lots d'un agent Claude en pause budget.

À chaque passage d'`ameesh notify`, pour chaque agent Claude NON critique
(`auditor.is_critical`) en pause budget (`budget.PAUSE_PREFIX`) :

- ses lots NON démarrés (`intake`) sont rendus à la file d'équipe (assigné
  NULL, note au journal) : la prise automatique (`backlog.auto_take`, L165,
  #754) les donne ensuite à tout agent libre capable de l'équipe, DeepSeek ou
  Codex, dans son budget et sa classe de revue ;
- ses lots DÉMARRÉS (`build`) sont repris directement, un lot par agent
  preneur, par un agent DeepSeek ou Codex libre
  (`sous_utilisation.idle_agents`) du même projet (`projects`), dans son
  budget, qui sait faire le lot (`backlog.lot_fits`) — la branche poussée et
  l'état git sont vérifiés avant la passation.

L'attribution d'un `build` reste gardée (`work.check_assignee`,
réassignation conditionnelle à l'assigné ET à l'état) ; une note va sur le
lot, un courrier aux deux agents. Le rendu d'un `intake` est gardé de même
(transaction sous verrou consultatif : source encore en pause, état encore
`intake`) ; sa note suffit, la prise automatique prévient le preneur. Un
plafond par heure glissante borne les rendus ET les reprises, pour tout le
mesh.

Jamais repris :
- un lot en revue ou gelé (état `qa`, ou jalon de gel posé) ;
- un lot sensible : capacité réservée (`backlog.RESERVED_CAPABILITIES`), ou
  branche qui touche un chemin de classe `sensitive` (politique de revue du
  canon) ; une politique illisible vaut sensible ;
- un lot qui attend une décision (demande ou jalon de décision) ;
- un lot dont la dernière attribution vient d'un humain (`human:…`) : le
  propriétaire a pu vouloir cet agent précis ;
- un lot `build` dont la branche n'est pas poussée, ou dont un worktree de
  l'agent source (son dossier de travail, les worktrees suivis du lot) a des
  changements non commités ou des commits non poussés : il est sauté, avec
  une note sur le lot (une fois par pause). Ces worktrees ne se lisent que
  sur l'hôte de l'agent source : ailleurs, le lot est sauté de même.

À la fin de la pause, rien ne revient seul : la note le dit, l'orchestrateur
décide. La revue codex reste exigée comme pour tout lot.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import os
import time

from . import storage
from .storage import interface

#: début commun des notes de reprise (le plafond horaire compte ces lignes,
#: rendus et reprises confondus ; la note d'un lot sauté, SKIP_NOTE, n'en fait
#: pas partie)
NOTE_PREFIX = "pause budget (reprise) : "
#: début de la note de reprise d'un lot `build` (le plafond horaire compte ces
#: lignes)
HANDOFF_NOTE = interface.HANDOFF_NOTE
#: début de la note d'un lot `intake` rendu à la file d'équipe (L165)
RETURN_NOTE = NOTE_PREFIX + "lot rendu à la file d'équipe"
#: début de la note d'un lot `build` sauté (une fois par pause)
SKIP_NOTE = "pause budget : lot non repris"
#: harnais de l'agent en pause, et des preneurs
SOURCE_HARNESS = "claude"
TARGET_HARNESSES = ("deepseek", "codex")
#: états traités : `intake` rendu à la file, `build` repris (jamais `qa`)
STATES = lifecycle.ACTIVE
#: reprises au plus par heure glissante, pour tout le mesh (0 : coupé)
DEFAULT_MAX_PER_HOUR = 4
#: repos minimal du preneur
DEFAULT_IDLE_S = 300.0
#: lots lus par agent en pause
LOTS_PER_AGENT = 50
#: lignes de journal lues pour la note d'un lot sauté (une fois par pause)
EVENTS_READ = 1000
#: débuts de note d'une attribution (`work assign`, création, délégation,
#: prises automatiques, reprise budget) — une seule source, avec `wip_exceptions`
#: et `requeued_sql` (§4)
_ASSIGN_NOTES = interface.ASSIGN_NOTE_PREFIXES


def paused(row: dict) -> bool:
    """L'agent est-il en pause budget ?"""
    from . import budget as budget_mod
    return (row.get("status") == "blocked"
            and (row.get("status_text") or "").startswith(budget_mod.PAUSE_PREFIX))


def gauge_paused(row: dict) -> bool:
    """#4066 : la pause budget qui retire les lots — pas la seule fenêtre
    courte du compte (#3779 : écart de départ, cooldown, plafond de tours).

    Le runner écrit la fenêtre courte sous le même préfixe « budget : » ; elle
    dure au plus `account_window.COOLDOWN_MAX_S` et, sur un compte partagé,
    se renouvelle au départ de chaque voisin : la lire comme une pause de jauge
    rendait les lots d'un agent tenu quelques secondes. Même reconnaissance que
    la désignation (#3876) et que `work.check_assignee` (#4056). `paused` reste
    la tenue (`account_turn.turn_hold`) : l'agent n'a pas de tour pendant ce
    temps, mais garde ses lots."""
    from . import review_designation
    return paused(row) and not review_designation.is_short_window_pause(
        row.get("status_text") or "")


def gauge_text(status_text: str) -> bool:
    """`gauge_paused` sur le seul `status_text` (déjà `blocked`, préfixe relu
    en SQL), pour la relecture sous verrou de `WorkItems.handoff`."""
    return gauge_paused({"status": "blocked", "status_text": status_text})


def assign_prefixes() -> tuple:
    """Débuts des notes d'une attribution du lot (toutes : attribution,
    création, délégation, prises automatiques, reprise sur pause budget), plus
    le rendu à la file (L165) que `slo.handover_prefixes` écarte."""
    return _ASSIGN_NOTES + (RETURN_NOTE,)


def last_assigner(events: list) -> str:
    """L'acteur de la dernière attribution du lot (`events` du plus récent au
    plus ancien, `assignment_events`) ; sans ligne d'attribution, celui de la
    création (la ligne la plus ancienne)."""
    prefixes = assign_prefixes()
    for event in events:
        if (event.get("note") or "").startswith(prefixes):
            return event.get("actor") or ""
    return (events[-1].get("actor") or "") if events else ""


def excluded(item: dict) -> str:
    """Pourquoi le lot n'est jamais repris (vide : il peut l'être), sans git
    ni journal."""
    from . import backlog
    if item.get("state") not in STATES:
        return "state %s" % item.get("state")
    if item.get("frozen_ts"):
        return "frozen"
    if set(item.get("required_capabilities") or ()) & set(backlog.RESERVED_CAPABILITIES):
        return "reserved capability"
    if item.get("pending_decision") or item.get("decision_milestone"):
        return "decision pending"
    if (item.get("delegated_by") or "").startswith("human:"):
        return "delegated by a human"
    return ""


def lot_git(item: dict, source: dict, *, host: str | None, worktrees: list,
            state=None, git=None) -> dict:
    """L'état git du lot pour une reprise : `{ok, reason, paths}`.

    `paths`, les fichiers que la branche poussée change depuis la branche
    principale (pour la classe de revue). Un lot `build` exige une branche
    poussée et des worktrees de l'agent source propres et poussés ; un lot
    `intake` n'exige rien, mais sa branche poussée, s'il en a une, doit être
    lisible pour être classée."""
    from . import menage
    state = state or menage.worktree_state
    git = git or menage._git
    branch = (item.get("branch") or "").strip()
    build = item.get("state") == lifecycle.IN_PROGRESS
    if not branch:
        return {"ok": not build, "reason": "no branch" if build else "", "paths": []}
    if (source.get("host") or "") != (host or ""):
        return {"ok": False, "paths": None,
                "reason": "worktrees on host %s, not readable from %s" % (
                    source.get("host") or "?", host or "?")}
    # les worktrees du lot : suivis pour lui (détachés compris), ou sur sa
    # branche d'après git ; leur HEAD doit être sur la branche distante du lot
    # (codex r2, #409)
    lot_dirs = [w.get("path") for w in worktrees
                if w.get("agent") == source["name"]
                and (str(w.get("lot") or "") == str(item["id"]) or w.get("branch") == branch)]
    dirs = []
    for path in [source.get("cwd")] + lot_dirs:
        if path and path not in dirs and os.path.isdir(path):
            dirs.append(path)
    if not dirs:
        return {"ok": False, "paths": None, "reason": "no worktree of %s on this host"
                % source["name"]}
    # sur la branche du lot d'après git (d'un autre agent peut-être : seul
    # son HEAD compte, pas sa propreté)
    heads = list(dirs)
    for path in dirs:
        for other in _branch_worktrees(path, branch, git):
            lot_dirs.append(other)
            if other not in heads and os.path.isdir(other):
                heads.append(other)
    if build:
        for path in dirs:
            seen = state(path)
            if not seen.get("clean"):
                return {"ok": False, "paths": None,
                        "reason": "uncommitted changes in %s" % path}
            if not seen.get("pushed"):
                return {"ok": False, "paths": None,
                        "reason": "commits not pushed in %s" % path}
    repo = dirs[0]
    remote = "refs/remotes/origin/%s" % branch
    found = git(["rev-parse", "--verify", "--quiet", remote + "^{commit}"], repo)
    if found is None or found.returncode != 0:
        if build:
            return {"ok": False, "paths": None, "reason": "branch %s not pushed" % branch}
        return {"ok": True, "reason": "", "paths": []}
    if build:
        # `pushed` dit seulement que HEAD est sur UNE branche distante : le
        # travail de la source doit être sur celle du lot, que le preneur
        # reprend (codex r1, #409)
        for path in heads:
            missing = _not_on_branch(path, branch, remote, git, lot_dir=path in lot_dirs)
            if missing:
                return {"ok": False, "paths": None,
                        "reason": "%s in %s not on origin/%s" % (missing, path, branch)}
    main = menage._default_remote_ref(repo)
    base = git(["merge-base", main, remote], repo) if main else None
    if base is None or base.returncode != 0 or not base.stdout.strip():
        return {"ok": False, "paths": None,
                "reason": "branch %s: no common base with the main branch" % branch}
    diff = git(["diff", "--name-only", "-z", base.stdout.strip(), remote], repo)
    if diff is None or diff.returncode != 0:
        return {"ok": False, "paths": None, "reason": "branch %s: diff unreadable" % branch}
    return {"ok": True, "reason": "", "paths": [p for p in diff.stdout.split("\0") if p]}


def _branch_worktrees(path: str, branch: str, git) -> list:
    """Les worktrees du dépôt de `path` sur la branche `branch` (`git worktree
    list --porcelain`)."""
    listed = git(["worktree", "list", "--porcelain"], path)
    if listed is None or listed.returncode != 0:
        return []
    found, current = [], None
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == "branch refs/heads/%s" % branch and current:
            found.append(current)
    return found


def _not_on_branch(path: str, branch: str, remote: str, git, *,
                   lot_dir: bool = False) -> str:
    """Ce qui, dans le worktree `path`, manque à la branche distante du lot
    (vide sinon) : sa branche locale `branch`, et HEAD quand il est sur
    `branch` ou que `path` est un worktree du lot (`lot_dir`, détaché
    compris). Un autre dépôt sans cette branche n'a rien à y mettre."""
    refs = []
    local = git(["rev-parse", "--verify", "--quiet", "refs/heads/%s^{commit}" % branch], path)
    if local is not None and local.returncode == 0:
        refs.append("refs/heads/%s" % branch)
    current = git(["symbolic-ref", "-q", "--short", "HEAD"], path)
    if lot_dir or (current is not None and current.returncode == 0
                   and current.stdout.strip() == branch):
        refs.append("HEAD")
    for ref in refs:
        inside = git(["merge-base", "--is-ancestor", ref, remote], path)
        if inside is None or inside.returncode != 0:
            return "branch %s" % branch if ref != "HEAD" else "HEAD"
    return ""


def sensitive(paths: list, canons, project=None) -> str:
    """Pourquoi ce changement est sensible (vide sinon) : la classe de revue
    de la politique de CHAQUE canon, pas du seul premier — un chemin sensible
    pour l'un l'est (codex r1, #409) ; une politique illisible vaut sensible.

    `project` (l'app du lot) choisit la politique du projet dans chaque canon :
    les globs d'un projet ne classent pas les fichiers d'un autre (§8). Un
    projet non déclaré sans repli est un doute, donc sensible.
    """
    from . import review as review_mod
    if not paths:
        return ""
    for federation in [getattr(c, "federation", None) for c in canons] or [None]:
        try:
            policy, problems = review_mod.policy_from_federation(federation, project)
            if any(severity == "error" for _code, severity, _text in problems):
                return "review policy unreadable"
            result = review_mod.classify(policy, paths)
        except Exception as exc:
            return "review class unknown (%s)" % exc
        if result["class"] == "sensitive":
            return "sensitive paths (%s)" % ", ".join(
                f["path"] for f in result["files"] if f["class"] == "sensitive")[:300]
    return ""


def _noted_since(events: list, since_ts: float) -> bool:
    return any((e.get("note") or "").startswith(SKIP_NOTE)
               and float(e.get("created_ts") or 0) >= since_ts for e in events)


def handoff_note(item: dict, source: str, target: str, reason: str) -> str:
    return ("%s par %s : %s est en pause budget (%s). Lot en %s%s. À la fin de la "
            "pause, rien ne revient seul : l'orchestrateur décide de rendre le lot "
            "ou de le laisser." % (
                HANDOFF_NOTE, target, source, reason or "plafond atteint", item["state"],
                ", branche %s" % item["branch"] if item.get("branch") else ""))


def return_note(item: dict, source: str, reason: str) -> str:
    """Note d'un lot `intake` rendu à la file d'équipe (L165)."""
    return ("%s : %s est en pause budget (%s). Le lot n'était pas démarré : il "
            "retourne à la file d'équipe, sans assigné, où la prise automatique "
            "peut le confier à tout agent libre capable de l'équipe, DeepSeek ou "
            "Codex. À la fin de la pause, il ne revient pas seul : l'orchestrateur "
            "décide." % (RETURN_NOTE, source, reason or "plafond atteint"))


def target_message(item: dict, source: str) -> str:
    branch = item.get("branch")
    return (
        "Work item #%d \"%s\" is now yours: %s is paused on budget. State: %s.%s "
        "A review by another family stays required before any merge. When %s's pause ends, "
        "nothing comes back on its own: the orchestrator decides. Never take an "
        "irreversible or production action without the usual approval." % (
            int(item["id"]), item.get("title") or "", source, item["state"],
            " Continue on the pushed branch %s (git fetch, then check it out)." % branch
            if branch else "", source))


def source_message(item: dict, target: str) -> str:
    branch = item.get("branch")
    return (
        "Work item #%d \"%s\" went to %s while you are paused on budget.%s At the end "
        "of your pause it does not come back on its own: the orchestrator decides." % (
            int(item["id"]), item.get("title") or "", target,
            " Do not push to %s any more." % branch if branch else ""))


def budget_handoff(cfg, db, *, now: float | None = None,
                   max_per_hour: int = DEFAULT_MAX_PER_HOUR,
                   idle_s: float = DEFAULT_IDLE_S, listing: list | None = None,
                   canons=None, declared=None, orchestras=None, budget_check=None,
                   dry_run: bool = False, state=None, git=None) -> list:
    """Un passage ; rend les mouvements faits (ou prévus en `dry_run`) :
    `[{item, agent, previous, message_id, queued}]` — `agent` vaut None et
    `queued` True quand un lot `intake` est rendu à la file d'équipe — et note
    les lots `build` sautés pour leur état git."""
    from . import auditor as auditor_mod
    from . import backlog, mail, projects, registry, sous_utilisation, work
    from . import budget as budget_mod

    if max_per_hour <= 0:
        return []
    now = time.time() if now is None else float(now)
    st = storage.of(db)
    room = int(max_per_hour) - st.work.auto_takes_since(backlog.TAKE_WINDOW_S, NOTE_PREFIX)
    if room <= 0:
        return []
    listing = st.operations.listing() if listing is None else listing
    sources = [row for row in listing
               if row.get("harness") == SOURCE_HARNESS and gauge_paused(row)]
    if not sources:
        return []
    if canons is None:
        try:
            canons = auditor_mod.load_all(cfg, strict=False) if cfg is not None else []
        except Exception:
            return []           # rôles illisibles : on ne devine pas qui est critique
    if declared is None:
        declared = sous_utilisation.declared_orchestrators()
    # jugé sur sa ligne du registre : elle seule porte son canon propriétaire
    # (`canon`), sans lequel un agent critique d'un canon secondaire serait lu
    # au canon par défaut (codex r1, #409) ; hors registre, rien n'est repris
    agents = {row["name"]: registry.get(db, row["name"]) for row in sources}
    sources = [row for row in sources
               if agents[row["name"]] is not None
               and not auditor_mod.is_critical(cfg, db, row["name"], canons,
                                               declared=declared,
                                               agent=dict(row, **agents[row["name"]]))]
    if not sources:
        return []
    if orchestras is None:
        try:
            orchestras = sous_utilisation.orchestrators(cfg, db, listing, canons=canons)
        except Exception:
            orchestras = []
    # un lot `intake` se rend à la file sans preneur ; un `build` exige un
    # preneur libre (DeepSeek ou Codex), le plus long repos d'abord : aucun
    # preneur n'a de lot (`idle_agents`)
    from . import mechanics
    from .review_lot import family_of
    take_documents = []
    targets = sorted((row for row in sous_utilisation.idle_agents(listing, now, idle_s)
                      if row.get("harness") in TARGET_HARNESSES
                      and row["name"] not in orchestras),
                     key=lambda r: (sous_utilisation.idle_since(r), r["name"]))
    if budget_check is None:
        try:
            budget_check = backlog._default_budget_check(cfg, db)
        except Exception:
            return []           # plafonds illisibles : on ne devine pas

    def within_budget(row) -> bool:
        try:
            return not budget_check(row["name"])
        except Exception:
            return False

    host = getattr(cfg, "host", None)
    worktrees = None

    def checked_git(item, source):
        """L'état git du lot ; les worktrees de l'hôte ne sont lus qu'une fois."""
        nonlocal worktrees
        if worktrees is None:
            worktrees = st.housekeeping.worktrees(host, ("active", "kept")) if host else []
        return lot_git(item, source, host=host, worktrees=worktrees, state=state, git=git)

    used: set = set()
    done_list: list = []
    for source in sorted(sources, key=lambda r: r["name"]):
        project_of = {source["name"]: projects.agent_project(source)}
        reason = (source.get("status_text") or "")[len(budget_mod.PAUSE_PREFIX):]
        for item in st.work.handoff_lots(source["name"], LOTS_PER_AGENT):
            if len(done_list) >= room:
                return done_list
            if excluded(item):
                continue
            project = projects.lot_project(item, project_of)
            if not project:
                continue
            # la dernière attribution, quelle que soit la longueur du journal
            # (codex r1, #409)
            if last_assigner(st.work.assignment_events(
                    int(item["id"]), assign_prefixes())).startswith("human:"):
                continue
            if item["state"] == lifecycle.REQUESTED:
                # lot non démarré : rendu à la file d'équipe (assigné NULL) ;
                # la prise automatique (L165, #754) choisit le preneur
                checked = checked_git(item, source)
                if not checked["ok"]:
                    continue
                if sensitive(checked["paths"], canons, project):
                    continue
                if dry_run:
                    done_list.append({"item": item, "agent": None,
                                      "previous": source["name"], "message_id": None,
                                      "queued": True})
                    continue
                # relu sous verrou à l'écriture (codex r1, #409) : l'état encore
                # `intake`, la source encore en pause, le plafond horaire ;
                # aucun preneur n'est exigé
                row = st.work.handoff(int(item["id"]), None, current=source["name"],
                                      state=lifecycle.REQUESTED, paused_prefix=budget_mod.PAUSE_PREFIX,
                                      still_paused=gauge_text,
                                      note_prefix=NOTE_PREFIX, window_s=backlog.TAKE_WINDOW_S,
                                      max_per_window=max_per_hour,
                                      note=return_note(item, source["name"], reason),
                                      actor=backlog.SENDER)
                if row is None:
                    continue                        # changé entre-temps
                done_list.append({"item": row, "agent": None, "previous": source["name"],
                                  "message_id": None, "queued": True})
                continue
            # lot démarré : un preneur libre du bon projet, dans son budget
            target = None
            for row in targets:
                if row["name"] in used or projects.agent_project(row) != project:
                    continue
                if not within_budget(row):
                    continue
                agent = registry.get(db, row["name"])
                if agent is None:
                    continue
                agent = dict(row, **agent)
                # #1136 : le filtre des sources s'applique aussi aux preneurs,
                # jugé sur la ligne du registre (canon propriétaire) : un agent
                # critique (conception, relecture, auditeur — même Codex) ne
                # reprend pas un lot rendu.
                if auditor_mod.is_critical(cfg, db, row["name"], canons,
                                          declared=declared, agent=agent):
                    continue
                if not auditor_mod.may_take(agent, canons):
                    continue
                auditors = auditor_mod.cards([agent], canons)
                if not backlog.lot_fits(dict(item, lot_team=project),
                                        dict(agent, team=project),
                                        auditor=row["name"] in auditors,
                                        card=auditors.get(row["name"]),
                                        others=listing, db=db):
                    continue
                # Politique requise seulement pour un transfert build réel,
                # jamais pour rendre un intake ou sans cible admissible.
                if not take_documents:
                    take_documents.append(mechanics.configured(cfg))
                document = take_documents[0]
                if family_of(row.get("harness"), document.families) in (
                        document.auto_take_excluded_families(now)):
                    continue
                target = row
                break
            if target is None:
                continue
            checked = checked_git(item, source)
            if not checked["ok"]:
                if not dry_run and not _noted_since(
                        st.work.events(int(item["id"]), EVENTS_READ),
                        float(source.get("status_since_ts") or 0.0)):
                    st.work.note(int(item["id"]), item["state"],
                                 "%s (vers %s) : %s" % (SKIP_NOTE, target["name"],
                                                        checked["reason"]),
                                 backlog.SENDER)
                continue
            if sensitive(checked["paths"], canons, project):
                continue
            if dry_run:
                done_list.append({"item": item, "agent": target["name"],
                                  "previous": source["name"], "message_id": None,
                                  "queued": False})
                used.add(target["name"])
                continue
            try:
                check = work.check_assignee(db, target["name"], cfg=cfg)
            except work.WorkError:
                used.add(target["name"])        # non réveillable : preneur suivant
                continue
            # tout relu sous verrou à l'écriture (codex r1, #409) : l'état
            # contrôlé ci-dessus (pas un autre des STATES), la source encore en
            # pause, le preneur encore sans lot, le plafond horaire
            row = st.work.handoff(int(item["id"]), check["assignee"], current=source["name"],
                                  state=lifecycle.IN_PROGRESS, paused_prefix=budget_mod.PAUSE_PREFIX,
                                  still_paused=gauge_text,
                                  note_prefix=NOTE_PREFIX, window_s=backlog.TAKE_WINDOW_S,
                                  max_per_window=max_per_hour,
                                  note=handoff_note(item, source["name"], check["assignee"],
                                                    reason),
                                  actor=backlog.SENDER)
            if row is None:
                continue                        # changé entre-temps
            payload = {"budget_handoff": {"lot": int(row["id"]), "from": source["name"],
                                          "to": check["assignee"]}}
            message_id = mail.send(db, backlog.SENDER, check["assignee"],
                                   target_message(row, source["name"]), kind="event",
                                   work_item_id=str(row["id"]), payload=payload)
            mail.send(db, backlog.SENDER, source["name"],
                      source_message(row, check["assignee"]), kind="event",
                      work_item_id=str(row["id"]), payload=payload)
            used.add(target["name"])
            done_list.append({"item": row, "agent": check["assignee"],
                              "previous": source["name"], "message_id": message_id,
                              "queued": False})
    return done_list
