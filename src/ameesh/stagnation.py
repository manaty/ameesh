# SPDX-License-Identifier: AGPL-3.0-only
"""Lots stagnants et « en attente de quoi, de qui » — module UNIQUE (L26 + L29).

Appelé par l'alerte `stale_lot` (`ameesh alerts`, L26), par `ameesh progress`
et par `ameesh work list/show` (L29) : les trois vues disent la même chose.

**Activité** d'un lot : modification de sa ligne, transition ou note,
jalon, action liée, message lié (`agent_mailbox.work_item_id`). Un lot
**stagnant** est un lot ni fusionné (`merged`, `promoted`) ni fermé
(`closed`, L29) sans activité depuis un seuil (défaut 6 h ; `--stale-after`,
`AMEESH_STALE_AFTER`).

L368: a future planned start, an explicit open commitment/plan dependency on
another open lot, or a dated external hold (`gate:<name>` in a commitment's
`depends_on`, ended by its due day) defers stagnation; `planned_waits` resolves
those sources for all three views, without interpreting notes or changing
activity.

**Attente** (`waiting`) : `{what, who, label}`, déduite de l'état, des jalons
(L10, lus par `progress._jalons_de_lot`) et des actions, jamais inventée
(`who` est None quand ameesh ne sait pas qui). Ordre :

1. une action du lot qui attend un humain — approbation (`proposed`) ou
   réconciliation (`unknown`) — passe avant tout (`merge-approval`,
   `merge-outcome` pour la fusion `git-merge`, `decision` sinon) ;
2. `waiting_human` → `decision` (du responsable de la fiche du lot) ;
3. un verdict `blocked` portant sur le dernier gel (même SHA, même quand le
   gel a été renouvelé après l'avis) → `fix` (l'auteur) ;
4. puis selon l'état : `start`, `build`, `unblock`, `verdict`, `merge`.

`waiting_for` (forme d'origine de L26, champ du même nom de `ameesh-alert/1`)
en est le résumé en un mot : `verdict`, `correction`, `fusion`,
`decision_humaine`, ou None.
"""
from __future__ import annotations

import os
import re
import time
from typing import Iterable

from . import storage

#: seuil par défaut d'un lot stagnant (secondes)
DEFAULT_THRESHOLD_S = 6 * 3600.0
STALE_ENV = "AMEESH_STALE_AFTER"
#: ce qu'un lot peut attendre, forme résumée de L26 (valeurs de `waiting_for`)
WAITING_FOR = ("verdict", "correction", "fusion", "decision_humaine")
#: ce qu'un lot peut attendre, forme détaillée (`waiting.what`)
WHATS = ("start", "build", "verdict", "fix", "merge-approval", "merge", "merge-outcome",
         "decision", "unblock")
_LEGACY = {"verdict": "verdict", "fix": "correction", "merge": "fusion",
           "decision": "decision_humaine", "merge-approval": "decision_humaine",
           "merge-outcome": "decision_humaine"}
#: états d'un lot qui n'attend plus rien
DONE_STATES = ("merged", "promoted", "closed")
#: états d'action qui attendent un humain
_HUMAN_ACTION_STATES = ("proposed", "unknown")
MERGE_CONNECTOR = "git-merge"

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdj]?)\s*$", re.I)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "j": 86400}


class StaleError(ValueError):
    """Seuil illisible."""


def parse_duration(text: str) -> float:
    """« 90m », « 6h », « 2d »/« 2j », « 3600 » → secondes (strictement positif)."""
    match = _DURATION_RE.match(text or "")
    if not match or float(match.group(1)) <= 0:
        raise StaleError("unreadable duration: %r (90m, 6h, 2d...)" % text)
    return float(match.group(1)) * _UNITS[match.group(2).lower()]


def stale_after(text: str | None = None, env: dict | None = None) -> float:
    """Le seuil : option, sinon `AMEESH_STALE_AFTER`, sinon 6 h."""
    env = os.environ if env is None else env
    value = text or env.get(STALE_ENV)
    return parse_duration(value) if value else DEFAULT_THRESHOLD_S


# --------------------------------------------------------------------------
# activité
# --------------------------------------------------------------------------

def last_activity(item: dict, events: Iterable[dict] = (), milestones: Iterable[dict] = (),
                  actions: Iterable[dict] = (), messages_ts: float | None = None,
                  events_ts: float | None = None) -> float | None:
    """Dernière activité : ligne du lot, transition ou note, jalon, action, message.

    `events_ts` porte le dernier instant du journal quand `events` ne contient
    que les transitions (`lot_transitions`) : une note récente ne doit pas
    disparaître de l'activité (#1647)."""
    stamps = [item.get("updated_ts"), item.get("created_ts"), messages_ts, events_ts]
    stamps += [e.get("created_ts") for e in events]
    stamps += [m.get("at_ts") for m in milestones]
    stamps += [a.get("updated_ts") or a.get("created_ts") for a in actions]
    stamps = [float(s) for s in stamps if s is not None]
    return max(stamps) if stamps else None


def stale(state: str, last_ts: float | None, now: float, threshold: float) -> dict | None:
    """`{since_ts, idle_s, threshold_s}` si le lot ouvert n'a rien fait depuis le seuil."""
    if state in DONE_STATES or last_ts is None:
        return None
    idle = now - float(last_ts)
    if idle < threshold:
        return None
    return {"since_ts": round(float(last_ts), 3), "idle_s": int(idle),
            "threshold_s": int(threshold)}


# --------------------------------------------------------------------------
# attente
# --------------------------------------------------------------------------

_LABELS = {
    "start": ("start by %s", "assignment (lot unassigned)"),
    "build": ("work by %s", "work (lot unassigned)"),
    "verdict": ("verdict by %s", "verdict from a reviewer (none designated)"),
    "fix": ("fix by %s", "fix (author not designated)"),
    "merge-approval": ("merge approval by %s", "merge approval"),
    "merge": ("merge by %s", "merge (executor not designated)"),
    "merge-outcome": ("merge outcome unknown: reconciliation by %s",
                      "merge outcome unknown: reconciliation"),
    "decision": ("human decision by %s", "human decision"),
    "unblock": ("unblocking by %s", "unblocking"),
}
#: display labels of the `who_state` values (the values stay as stored/emitted)
_STATE_LABELS = {"inconnu": "unknown", "arrêté": "stopped", "externe": "external session"}


def _waiting(what: str, who) -> dict:
    if isinstance(who, (list, tuple)):
        who = ", ".join(str(w) for w in who if w) or None
    who = who or None
    with_who, without = _LABELS[what]
    return {"what": what, "who": who, "label": with_who % who if who else without}


def _merge_wait(actions: list[dict], responsible, assignee) -> dict:
    """L'attente d'une fusion : la dernière action `git-merge` tranche (L26)."""
    merges = [a for a in actions if a.get("connector") == MERGE_CONNECTOR]
    last = merges[-1] if merges else None
    if last is not None:
        if last.get("state") == "approved":
            return _waiting("merge", last.get("proposed_by"))
        if last.get("state") == "failed":
            return _waiting("fix", assignee)
    return _waiting("merge", responsible)


def waiting(item: dict, jalons: dict, actions: Iterable[dict] = (), *,
            responsible: str | None = None,
            review_class: str | None = None) -> dict | None:
    """Ce qu'attend un lot ouvert, et de qui : `{what, who, label}` (voir le module).

    `jalons` : la forme de `progress._jalons_de_lot` ; `responsible` : le
    responsable humain de la fiche WorkPackage du lot, s'il en a une.
    `review_class` : la classe de revue du lot (`lot_class.LIGHT`, ...) quand
    l'appelant l'a résolue ; la relecture d'un lot **léger** suit la fusion
    (§2), donc un lot léger sans verdict courant attend la fusion, pas un
    verdict (#1822). `None` (classe inconnue) garde l'attente prudente d'un
    verdict, sans deviner aucune classe."""
    state = item.get("state") or ""
    assignee = item.get("assignee") or None
    if state in DONE_STATES:
        return None
    actions = list(actions)
    # 1. une action qui attend un humain passe avant tout (L26)
    for act in reversed(actions):
        if act.get("state") not in _HUMAN_ACTION_STATES:
            continue
        approvers = list(act.get("approvers") or []) or responsible
        if act.get("connector") == MERGE_CONNECTOR:
            if act["state"] == "proposed":
                return _waiting("merge-approval", approvers)
            return _waiting("merge-outcome", responsible)
        return _waiting("decision", approvers if act["state"] == "proposed" else responsible)
    if state == "waiting_human":
        return _waiting("decision", responsible)
    # 3. un verdict bloquant ne compte que s'il suit le dernier gel (L26)
    if jalons.get("verdict_kind") == "blocked" and jalons.get("verdict_current"):
        return _waiting("fix", assignee)
    if state == "blocked":
        return _waiting("unblock", responsible or assignee)
    if state == "intake":
        return _waiting("start", assignee)
    if state == "build":
        return _waiting("build", assignee)
    # qa : en revue
    if jalons.get("verdict_kind") == "ok" and jalons.get("verdict_current"):
        return _merge_wait(actions, responsible, assignee)
    from . import lot_class  # import tardif : lot_class lit work et canon
    if review_class == lot_class.LIGHT:
        # §2 : la relecture d'un lot léger suit la fusion ; aucun verdict
        # n'est dû avant, même si un relecteur est désigné (#1822).
        return _merge_wait(actions, responsible, assignee)
    return _waiting("verdict", jalons.get("reviewer"))


def class_value(reading: dict | None) -> str | None:
    """La classe d'une lecture `lot_class.of_lot`, ou None au moindre doute.

    Un doute (dépôt, gel ou politique illisibles) ne devine aucune classe :
    l'appelant garde l'attente prudente (#1822)."""
    if not isinstance(reading, dict) or reading.get("doubt"):
        return None
    return str(reading.get("class") or "") or None


def review_class_of(db, row: dict, *, cfg=None, memo: dict | None = None) -> str | None:
    """La classe de revue du lot `row`, lue une fois par passage (`memo`).

    `lot_class.of_lot` ne lève jamais ; la lecture reste donc sûre dans une vue
    groupée, et tout doute rend None (attente prudente, #1822)."""
    from . import lot_class

    rid = int(row["id"])
    if memo is not None and rid in memo:
        return memo[rid]
    try:
        value = class_value(lot_class.of_lot(db, rid, cfg=cfg, item=row))
    except Exception:  # noqa: BLE001 - une vue ne tombe pas sur la classe
        value = None
    if memo is not None:
        memo[rid] = value
    return value


#: attentes dont le `who` est un agent qui doit travailler (L36)
_AGENT_WAITS = ("start", "build", "fix", "verdict")


def agent_states(db) -> dict[str, str]:
    """`{nom: état}` de tous les agents du registre (`progress.agent_state`)."""
    return agent_availability(db)[0]


def agent_availability(db) -> tuple[dict[str, str], dict[str, str | None]]:
    """`({nom: état}, {nom externe: responsable ou None})` en une lecture (L37)."""
    return availability_of(storage.of(db).progress.agents(None))


def availability_of(rows: Iterable[dict]) -> tuple[dict[str, str], dict[str, str | None]]:
    """Les deux tables de `with_availability` depuis des lignes d'agents."""
    from . import progress  # import tardif : progress importe ce module

    rows = list(rows)
    states = {r["name"]: progress.agent_state(r) for r in rows}
    externals = {r["name"]: (r.get("responsible") or None) for r in rows
                 if r.get("mode") == "externe"}
    return states, externals


def with_availability(detail: dict | None, states: dict[str, str] | None,
                      externals: dict[str, str | None] | None = None) -> dict | None:
    """Dit quand celui qu'attend un lot ne peut pas travailler (L36, décision 0030).

    Un agent absent du registre est « inconnu », un agent arrêté (ou mort) est
    « arrêté » : l'étiquette le dit, et `who_state` le porte pour les outils.
    L37 : un agent EXTERNE (`externals`, nom → responsable) est une session
    humaine qu'ameesh ne réveille pas : « (session externe, <responsable>) »,
    `who_state` = `externe` et `responsible` = l'humain qui porte le lot.
    Les humains (`human:…`) et les attentes sans agent ne sont pas touchés."""
    if not detail or states is None or detail.get("what") not in _AGENT_WAITS:
        return detail
    if not detail.get("who"):
        return detail
    externals = externals or {}
    notes: dict[str, str] = {}
    responsible: str | None = None
    for name in (n.strip() for n in str(detail["who"]).split(",")):
        if not name or name.startswith("human:"):
            continue
        state = states.get(name)
        if state is None:
            notes[name] = "inconnu"
        elif name in externals:
            notes[name] = "externe"
            responsible = responsible or externals[name]
        elif state == "stopped":
            notes[name] = "arrêté"
    if not notes:
        return detail
    single = len(notes) == 1 and "," not in str(detail["who"])
    if single and next(iter(notes.values())) == "externe":
        suffix = " (external session, %s)" % (responsible or "no responsible")
    elif single:
        suffix = " (agent %s)" % _STATE_LABELS[next(iter(notes.values()))]
    else:
        suffix = " (%s)" % ", ".join(
            "%s %s" % (n, _STATE_LABELS[st]) for n, st in notes.items())
    out = dict(detail, label=detail["label"] + suffix,
               who_state=notes if len(notes) > 1 else next(iter(notes.values())))
    if "externe" in notes.values():
        out["responsible"] = responsible
    return out


def legacy(detail: dict | None) -> str | None:
    """Le résumé en un mot (forme L26, `ameesh-alert/1`)."""
    return _LEGACY.get((detail or {}).get("what"))


def waiting_for(item: dict, events: Iterable[dict], milestones: Iterable[dict],
                actions: Iterable[dict], *, responsible: str | None = None) -> str | None:
    """Forme d'origine de L26 : un mot de `WAITING_FOR`, ou None."""
    from . import progress  # import tardif : progress importe ce module

    actions = list(actions)
    jalons = progress._jalons_de_lot(item, list(events), actions, list(milestones))
    return legacy(waiting(item, jalons, actions, responsible=responsible))


# --------------------------------------------------------------------------
# lecture groupée (alerte stale_lot, work list, progress)
# --------------------------------------------------------------------------

def _grouped(st, ids: list[int]) -> tuple[dict, dict, dict, dict, dict]:
    events: dict = {}
    milestones: dict = {}
    actions: dict = {}
    messages: dict = {}
    last_events: dict = {}
    if ids:
        # #1647 : les vues groupées ne lisent que les transitions ; le texte
        # des notes (des mégaoctets) n'est ni transféré ni recopié, et le
        # dernier instant du journal vient d'une agrégation.
        for ev in st.progress.lot_transitions(ids):
            events.setdefault(int(ev["work_item_id"]), []).append(ev)
        for row in st.progress.lot_event_last(ids):
            last_events[int(row["work_item_id"])] = float(row["last_ts"])
        for ms in st.progress.lot_milestones(ids):
            milestones.setdefault(int(ms["work_item_id"]), []).append(ms)
        for act in st.progress.lot_actions(ids):
            actions.setdefault(int(act["work_item"]), []).append(act)
        for row in st.progress.lot_messages(ids):
            messages[int(row["work_item_id"])] = row.get("last_ts")
    return events, milestones, actions, messages, last_events


def describe(db, rows: list[dict], *, now: float | None = None,
             threshold: float = DEFAULT_THRESHOLD_S,
             open_items: list[dict] | None = None,
             cfg=None, review_class: dict | None = None) -> dict[int, dict]:
    """Pour chaque lot (lignes de `work_items`) : `{waiting, waiting_for,
    last_activity_ts, project, stale}`. `project` (#705, lu par
    `projects.lot_project`) : le projet du lot, dont l'orchestrateur est
    prévenu quand un lot en qa n'a pas de relecteur désigné. Une lecture
    groupée, aucune requête par lot.

    `cfg` et `review_class` (#1822) : le contexte de résolution de la classe de
    revue. La classe n'est lue — une fois par passage — que pour les lots dont
    l'attente serait un verdict : un lot léger attend la fusion, pas un verdict
    (§2). `review_class` porte les classes déjà résolues par l'appelant
    (`{id: classe ou None}`) ; une classe absente ou douteuse garde l'attente
    prudente d'un verdict, sans deviner."""
    from . import progress  # import tardif : progress importe ce module
    from . import projects

    now = time.time() if now is None else float(now)
    st = storage.of(db)
    ids = [int(r["id"]) for r in rows]
    packages = {p["id"]: p for p in st.packages.all(include_absent=True)}
    agents = st.progress.agents(None)
    states, externals = availability_of(agents)
    agent_projects = {r["name"]: projects.agent_project(r) for r in agents}
    events, milestones, actions, messages, last_events = _grouped(st, ids)
    from . import planned_waits
    from . import work_dependencies
    dependencies = work_dependencies.grouped(db, ids)
    holds = planned_waits.read(db, rows, now=now, packages=packages, open_items=open_items,
                              dependencies=[e for edges in dependencies.values() for e in edges])
    out: dict[int, dict] = {}
    classes: dict = {}
    known = review_class or {}
    for row in rows:
        rid = int(row["id"])
        acts = actions.get(rid, [])
        package = packages.get(row.get("package_id")) or {}
        jalons = progress._jalons_de_lot(row, events.get(rid, []), acts, milestones.get(rid))
        detail = waiting(row, jalons, acts,
                         responsible=package.get("responsible"))
        # #1822 : la classe n'est lue que pour un lot qui attendrait un
        # verdict ; déjà résolue par l'appelant, elle ne l'est pas relue.
        if detail is not None and detail.get("what") == "verdict":
            cls = known[rid] if rid in known else review_class_of(
                db, row, cfg=cfg, memo=classes)
            if cls:
                detail = waiting(row, jalons, acts,
                                 responsible=package.get("responsible"),
                                 review_class=cls)
        detail = with_availability(detail, states, externals)
        last = last_activity(row, events.get(rid, []), milestones.get(rid, []), acts,
                             messages.get(rid), events_ts=last_events.get(rid))
        out[rid] = {"waiting": detail, "waiting_for": legacy(detail),
                    "last_activity_ts": round(last, 3) if last is not None else None,
                    "project": projects.lot_project(row, agent_projects,
                                                    package_team=package.get("team")),
                    "planned_wait": holds.get(rid),
                    "dependencies": dependencies.get(rid, []),
                    "stale": None if rid in holds else
                             stale(row.get("state") or "", last, now, threshold)}
    return out


def stagnant_lots(db, *, threshold_s: float = DEFAULT_THRESHOLD_S,
                  now: float | None = None, limit: int = 500,
                  cfg=None, review_class: dict | None = None) -> list[dict]:
    """Les lots ouverts inactifs depuis au moins `threshold_s` secondes.

    Une ligne : id, title, state, assignee, `last_activity_ts`, `idle_s`,
    `waiting_for` (un mot de `WAITING_FOR` ou None, forme L26), `waiting`
    (`{what, who, label}`, L29) et `project` (#705 : le projet du lot, dont
    l'orchestrateur est prévenu quand aucun relecteur n'est désigné).
    `cfg`/`review_class` : le contexte de classe de revue (#1822), transmis à
    `describe`."""
    from . import sous_utilisation
    now = time.time() if now is None else float(now)
    st = storage.of(db)
    # L119 (0037) : un élément de la file d'amélioration qui attend son
    # preneur n'est pas stagnant, il est en file
    # Read all open lots before filtering: deferred lots must not occupy the
    # render limit and hide an actionable stale lot or a dependency beyond it.
    open_items = st.operations.open_lots_activity(None)
    rows = [r for r in open_items
            if r.get("last_activity_ts") is not None
            and now - float(r["last_activity_ts"]) >= float(threshold_s)
            and not sous_utilisation.is_backlog_item(r)]
    described = describe(db, rows, now=now, threshold=threshold_s, open_items=open_items,
                         cfg=cfg, review_class=review_class)
    out = []
    for row in rows:
        lot = int(row["id"])
        info = described[lot]
        if info["stale"] is None:
            continue
        out.append({
            "id": lot,
            "title": row.get("title"),
            "state": row.get("state"),
            "assignee": row.get("assignee") or None,
            "last_activity_ts": float(row["last_activity_ts"]),
            "idle_s": int(now - float(row["last_activity_ts"])),
            "waiting_for": info["waiting_for"],
            "waiting": info["waiting"],
            "project": info.get("project"),
        })
    return out[:max(0, int(limit))]
