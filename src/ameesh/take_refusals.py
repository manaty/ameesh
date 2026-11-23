# SPDX-License-Identifier: AGPL-3.0-only
"""#8119 — pourquoi la prise automatique (L165) n'a pas pris un lot, et
l'échéance d'une heure.

Le constat du 22/11 : des lots et des paniers sont restés sans assigné six à
onze heures pendant que la plupart des agents dormaient au motif « feuille de
route épuisée » (#8035). Le journal des refus répond à deux questions, dans
l'ordre où le pilote les pose :

* **pourquoi L165 n'a pas pris cet élément** — prérequis ouvert, empreinte
  tenue par un lot en cours, hold de pilote, décision, exécutant externe,
  classe sensible sans autre famille, capacité, équipe, agent endormi,
  agent occupé, plafond §4, budget, hôte fermé, ou prêt (le cas anormal) ;
* **que faire** — un élément démarrable sans assigné depuis plus d'une heure
  alors que des agents éligibles dorment réveille ces agents (la passe de
  prise le prend ensuite) ; s'il reste unprenable, le pilote reçoit un
  courrier d'information qui nomme le motif.

Le journal lui-même est écrit sur le lot (`take refusal: …`), une note par
heure au plus : il se lit avec `ameesh work show <lot>`, sans table neuve. Le
courrier au pilote n'est envoyé qu'une fois par motif : une note plus récente
du même motif ne le répète pas.

Rien ici ne nomme un projet ni un métier : les raisons se lisent sur le lot,
sur les dépendances, sur les empreintes et sur le registre des agents.
"""
from __future__ import annotations

import time

from . import work_lifecycle as lifecycle

#: préfixe du journal des refus, écrit sur le lot ou le panier
REFUSAL_PREFIX = "take refusal:"
#: préfixe du motif de levée d'un sommeil par l'échéance
DEADLINE_PREFIX = "take deadline:"
#: échéance : un élément démarrable sans assigné depuis plus d'une heure alors
#: que des agents éligibles dorment est pris automatiquement
TAKE_DEADLINE_S = 3600.0
#: une note de refus par élément et par heure au plus
JOURNAL_WINDOW_S = 3600.0
#: raisons qui disent l'élément lui-même démarrable : seul le preneur manquait
STARTABLE_REASONS = ("asleep", "busy", "cap", "budget", "host", "ready", "unknown")
#: raisons qui nomment un blocage du lot, pas un manque de preneur
BLOCKED_REASONS = ("external", "hold", "prerequisite", "scheduled", "footprint",
                   "decision", "reserved", "team", "capability", "family",
                   "sensitive", "split", "not_queued", "roadmap")

_LABELS = {
    "external": "réservé à un exécutant externe",
    "hold": "hold de pilote",
    "prerequisite": "prérequis bloquant ouvert",
    "scheduled": "début prévu à venir",
    "footprint": "empreinte tenue par un lot en cours",
    "decision": "décision en attente",
    "reserved": "capacité réservée (décision ou production)",
    "team": "aucune équipe preneuse",
    "capability": "capacité exigée absente du registre",
    "family": "famille de relecture absente",
    "sensitive": "classe sensible sans autre famille",
    "split": "lot de découpage (geste du pilote)",
    "not_queued": "lot en cours non rendu à la file",
    "roadmap": "un lot de roadmap de l'équipe passe avant",
    "asleep": "agents éligibles endormis",
    "busy": "agents éligibles occupés",
    "cap": "plafond de lots en vol atteint",
    "budget": "plafond de budget atteint",
    "host": "hôte fermé",
    "ready": "prêt mais non pris (cas anormal)",
    "unknown": "motif non déterminé",
}


def _duration(seconds: float) -> str:
    minutes = max(0, int(round(float(seconds) / 60.0)))
    if minutes < 60:
        return "%d min" % minutes
    hours, minutes = divmod(minutes, 60)
    return "%d h %02d" % (hours, minutes) if minutes else "%d h" % hours


def _wakeable(row: dict) -> bool:
    """Un agent du registre qui peut ouvrir un tour (pas éphémère, pas retiré,
    pas une session externe). Un agent endormi reste éligible : l'échéance peut
    le réveiller."""
    return (not row.get("ephemeral") and not row.get("retired_ts")
            and (row.get("mode") or "execute") == "execute")


def _unassigned(db) -> list[dict]:
    """Les lots et paniers sans assigné, avec leur ligne complète.

    `unassigned_lots` ne rend que les éléments dont les dépendances et
    l'empreinte sont satisfaites : les éléments bloqués, justement ceux que le
    journal doit nommer, sont relus par `open_lots_activity` puis par
    `work.get`. Un lot `improvement` est un panier : il vient de la file.
    """
    from . import storage
    st = storage.of(db)
    takeable = {int(item["id"]): dict(item)
                for item in st.work.unassigned_lots(None) or ()}
    out: list[dict] = []
    seen: set[int] = set()
    for row in st.operations.open_lots_activity(None) or ():
        ident = int(row["id"])
        if row.get("assignee") or row.get("type") == "improvement":
            continue
        if row.get("state") not in lifecycle.ACTIVE:
            continue
        seen.add(ident)
        item = takeable.get(ident)
        if item is None:
            item = st.work.get(ident)
            if item is None:
                continue
            item = dict(item)
            item.setdefault("lot_team", row.get("lot_team"))
            item.setdefault("created_ts", row.get("created_ts"))
        out.append({"item": item, "kind": "lot"})
    for item in st.work.backlog(open_only=True, limit=200, ready_only=True) or ():
        ident = int(item["id"])
        if ident in seen:
            continue
        seen.add(ident)
        out.append({"item": dict(item), "kind": "basket"})
    return out


class _Context:
    """Les lectures partagées d'une passe (registre, attentes, holds, empreinte,
    agents au repos), pour ne pas les refaire par élément."""

    def __init__(self, cfg, db, listing, now):
        from . import backlog, projects, sous_utilisation, storage
        self.cfg = cfg
        self.db = db
        self.now = float(now)
        self.st = storage.of(db)
        self.listing = list(listing or ())
        self.backlog = backlog
        self.projects = projects
        self.su = sous_utilisation
        self.by_project: dict[str, list] = {}
        for row in self.listing:
            for project in projects.agent_projects(row):
                self.by_project.setdefault(project, []).append(row)
        self.idle = {row["name"] for row in sous_utilisation.idle_agents(
            self.listing, self.now, 0.0, waiting_free=True, actionable_idle=True)}
        self._budget = None
        self._budget_read = False
        self._blocked_hosts = None
        #: une fiche de registre par agent, pas une par (élément, agent) :
        #: le journal d'une passe lit les mêmes fiches pour chaque élément.
        self._agents: dict[str, dict] = {}
        try:
            from . import host_holds, host_lifecycle
            holds = host_holds.live(db)
            self._blocked_hosts = set(holds)
            self._blocked_hosts |= set(host_lifecycle.closed_hosts(
                db, holds=list(holds.values())))
        except Exception:  # noqa: BLE001 - une lecture en échec ne devine pas
            self._blocked_hosts = None

    def project(self, item: dict) -> str:
        return self.backlog._lot_project(item)

    def agent_row(self, row: dict) -> dict:
        """La ligne de registre complète (capacités, harnais, rôles)."""
        from . import registry
        name = row.get("name") or ""
        if name not in self._agents:
            try:
                self._agents[name] = registry.get(self.db, name) or {}
            except Exception:  # noqa: BLE001 - une fiche illisible ne rend pas preneur
                self._agents[name] = {}
        return dict(row, **self._agents[name])

    def candidates(self, item: dict, kind: str) -> list[dict]:
        """Les agents éligibles pour cet élément, endormis ou non."""
        out = []
        for row in self.listing:
            if not _wakeable(row):
                continue
            agent = self.agent_row(row)
            if kind == "basket":
                fits = self.backlog.fits(item, agent, others=self.listing)
            else:
                fits = self.backlog.lot_fits(item, agent, others=self.listing,
                                             db=self.db)
            if fits:
                out.append(row)
        return out

    def budget_over(self, name: str) -> bool:
        if not self._budget_read:
            self._budget_read = True
            try:
                self._budget = self.backlog._default_budget_check(self.cfg, self.db)
            except Exception:  # noqa: BLE001 - plafonds illisibles : pas de motif
                self._budget = None
        if self._budget is None:
            return False
        try:
            return bool(self._budget(name))
        except Exception:  # noqa: BLE001 - une lecture en échec ne bloque pas
            return False

    def cap(self) -> int:
        from . import work
        return work.build_limit(self.cfg)

    def holds(self, items):
        try:
            return self.backlog._take_holds(self.db, items, cfg=self.cfg)
        except Exception:  # noqa: BLE001 - un hold illisible ne devine pas
            return {}

    def waits(self, items):
        from . import planned_waits
        try:
            return planned_waits.read(self.db, items, now=self.now)
        except Exception:  # noqa: BLE001 - une attente illisible ne devine pas
            return {}

    def footprint(self, lots):
        if not lots:
            return {}
        try:
            all_lots = self.st.operations.open_lots_activity(None) or ()
            return self.su.footprint_blockers(self.db, lots, all_lots)
        except Exception:  # noqa: BLE001 - une empreinte illisible ne devine pas
            return {}


def _reason(ctx: _Context, item: dict, kind: str, holds, waits, footprint) -> tuple:
    """`(raison, détail, éligibles, endormis)` pour un élément."""
    from . import file_decoupage, review_lot
    ident = int(item["id"])
    if kind == "lot":
        if (item.get("state") == lifecycle.IN_PROGRESS
                and not item.get("returned_to_queue")):
            return ("not_queued", "lot en cours sans preneur, non rendu à la file",
                    [], [])
        if file_decoupage.is_split_lot(item):
            return "split", "le lot de découpage appartient au pilote", [], []
        if (item.get("external_executor") or "").strip():
            return ("external", "réservé à %s" % item["external_executor"], [], [])
        if ctx.backlog._hold_blocks(item, holds.get(ident), ctx.now):
            return "hold", "hold de pilote encore vivant", [], []
        wait = waits.get(ident)
        if wait:
            if wait.get("what") == "dependency":
                return ("prerequisite", "attend %s" % ", ".join(
                    "#%d" % int(i) for i in wait.get("lots") or ()), [], [])
            if wait.get("what") == "scheduled":
                return "scheduled", "début prévu %s" % wait.get("start"), [], []
            return "prerequisite", str(wait), [], []
        blockers = footprint.get(ident)
        if blockers:
            return ("footprint", "attend l'empreinte de %s" % ", ".join(
                "#%d" % int(b["id"]) for b in blockers), [], [])
        if item.get("pending_decision") or item.get("decision_milestone"):
            return "decision", "une décision est en attente", [], []
        if set(item.get("required_capabilities") or ()) & set(
                ctx.backlog.RESERVED_CAPABILITIES):
            return ("reserved", "capacité réservée : %s" % ", ".join(sorted(
                set(item.get("required_capabilities") or ())
                & set(ctx.backlog.RESERVED_CAPABILITIES))), [], [])
    project = ctx.project(item)
    if not project:
        return "team", "aucune équipe ni projet connu", [], []
    cands = ctx.candidates(item, kind)
    if not cands:
        same_project = [row for row in ctx.by_project.get(project, ())
                        if _wakeable(row)]
        if not same_project:
            return "team", "aucun agent réveillable dans l'équipe %s" % project, [], []
        if ctx.backlog.declared_class(item):
            return ("sensitive", "classe déclarée, aucune autre famille au registre",
                    [], [])
        if kind == "lot" and review_lot.review_ref(item):
            return "family", "famille de relecture absente", [], []
        required = ", ".join(item.get("required_capabilities") or ()) or "—"
        return ("capability", "aucun agent de %s ne porte %s" % (project, required),
                [], [])
    asleep = [row for row in cands
              if row.get("operator_paused")
              and file_decoupage.is_sleep_reason(row.get("operator_pause_reason"))]
    ready = [row for row in cands if row.get("name") in ctx.idle]
    if not ready:
        if asleep:
            return ("asleep", "endormis : %s" % ", ".join(
                sorted(row["name"] for row in asleep)),
                sorted(row["name"] for row in cands),
                sorted(row["name"] for row in asleep))
        return ("busy", "occupés : %s" % ", ".join(
            sorted(row["name"] for row in cands)),
            sorted(row["name"] for row in cands), [])
    try:
        cap = ctx.cap()
        if all(ctx.st.work.count_builds(row["name"]) >= cap for row in ready):
            return ("cap", "plafond de %d lots en vol atteint par %s" % (
                cap, ", ".join(sorted(row["name"] for row in ready))),
                sorted(row["name"] for row in cands), [])
    except Exception:  # noqa: BLE001 - canon illisible : le motif suivant
        pass
    if ctx._blocked_hosts is not None and all(
            row.get("host") in ctx._blocked_hosts for row in ready):
        return ("host", "hôtes fermés : %s" % ", ".join(sorted(
            {str(row.get("host") or "?") for row in ready})),
            sorted(row["name"] for row in cands), [])
    over = [row for row in ready if ctx.budget_over(row["name"])]
    if over and len(over) == len(ready):
        return ("budget", "plafond de budget atteint par %s" % ", ".join(
            sorted(row["name"] for row in over)),
            sorted(row["name"] for row in cands), [])
    return ("ready", "des agents au repos sont éligibles", sorted(
        row["name"] for row in cands), [])


def explain(cfg, db, *, now: float | None = None, listing: list | None = None,
            min_age: float = 0.0) -> list[dict]:
    """Les éléments sans assigné, avec le motif du refus de L165.

    Rend `[{id, kind, title, state, team, age_s, reason, detail, eligible,
    asleep, startable}]`, du plus ancien au plus récent. `startable` est vrai
    quand le lot lui-même n'est pas bloqué : seul le preneur manquait.
    `min_age` écarte tôt les éléments trop jeunes (aucune lecture coûteuse).
    """
    now = time.time() if now is None else float(now)
    from . import storage
    st = storage.of(db)
    if listing is None:
        listing = st.operations.listing()
    entries = _unassigned(db)
    if not entries:
        return []
    ctx = _Context(cfg, db, listing, now)
    lots = [entry["item"] for entry in entries if entry["kind"] == "lot"]
    items = [entry["item"] for entry in entries]
    holds = ctx.holds(items)
    waits = ctx.waits(items)
    footprint = ctx.footprint(lots)
    rows = []
    # Les lots d'abord : un panier n'est pas pris tant qu'un lot de roadmap de
    # son équipe peut l'être (#2584). Le motif du panier nomme alors ce lot,
    # au lieu de laisser croire qu'aucun agent ne pouvait le prendre.
    for entry in entries:
        if entry["kind"] != "lot":
            continue
        item = entry["item"]
        ident = int(item["id"])
        # une horloge de base légèrement en avance ne rend pas un élément
        # « futur » invisible : son âge est nul, pas négatif.
        age = max(0.0, now - float(item.get("created_ts") or now))
        if age < float(min_age):
            continue
        reason, detail, eligible, asleep = _reason(
            ctx, item, "lot", holds, waits, footprint)
        rows.append({
            "id": ident,
            "kind": "lot",
            "title": item.get("title") or "",
            "state": item.get("state") or "",
            "team": ctx.project(item),
            "age_s": age,
            "reason": reason,
            "detail": detail,
            "eligible": eligible,
            "asleep": asleep,
            "startable": reason not in BLOCKED_REASONS,
            "item": item,
        })
    roadmap = {}
    for row in rows:
        if row["startable"] and row["team"]:
            roadmap.setdefault(row["team"], []).append(int(row["id"]))
    for entry in entries:
        if entry["kind"] != "basket":
            continue
        item = entry["item"]
        ident = int(item["id"])
        # une horloge de base légèrement en avance ne rend pas un élément
        # « futur » invisible : son âge est nul, pas négatif.
        age = max(0.0, now - float(item.get("created_ts") or now))
        if age < float(min_age):
            continue
        team = ctx.project(item)
        if roadmap.get(team):
            reason = "roadmap"
            detail = "attend %s" % ", ".join(
                "#%d" % lot for lot in sorted(roadmap[team])[:5])
            eligible, asleep = [], []
        else:
            reason, detail, eligible, asleep = _reason(
                ctx, item, "basket", holds, waits, footprint)
        rows.append({
            "id": ident,
            "kind": "basket",
            "title": item.get("title") or "",
            "state": item.get("state") or "",
            "team": team,
            "age_s": age,
            "reason": reason,
            "detail": detail,
            "eligible": eligible,
            "asleep": asleep,
            "startable": reason not in BLOCKED_REASONS,
            "item": item,
        })
    rows.sort(key=lambda row: (-row["age_s"], row["id"]))
    return rows


def journal(cfg, db, *, now: float | None = None, listing: list | None = None,
            dry_run: bool = False, min_age: float = TAKE_DEADLINE_S) -> list[dict]:
    """Écrit le journal des refus des éléments sans assigné depuis `min_age`,
    une note par heure et par élément ; alerte le pilote une fois par motif.

    Une note est écrite pour TOUS les motifs (prérequis, empreinte, hold,
    décision, équipe, capacité, classe, famille, endormi, occupé, plafond,
    budget, hôte) : c'est le journal lisible par le pilote. Le courrier
    d'information ne part que pour un élément démarrable dont le motif est un
    manque de preneur (`STARTABLE_REASONS`) — pour un blocage du lot, le
    journal suffit et l'action est connue. Une lecture ou une écriture en
    échec ne défait pas les autres notes.
    """
    rows = explain(cfg, db, now=now, listing=listing, min_age=min_age)
    if dry_run or not rows:
        return rows
    from . import mail, sous_utilisation, work
    from .backlog import SENDER
    now = time.time() if now is None else float(now)
    orchestras = None
    try:
        orchestras = sous_utilisation.orchestrators(cfg, db, listing)
    except Exception:  # noqa: BLE001 - pas d'orchestrateur lisible : note seule
        orchestras = []
    for row in rows:
        ident = row["id"]
        try:
            recent = work.events_noted(db, ident, REFUSAL_PREFIX,
                                       now - JOURNAL_WINDOW_S)
        except Exception:  # noqa: BLE001 - une lecture en échec n'écrit pas
            continue
        if recent:
            continue
        label = _LABELS.get(row["reason"], row["reason"])
        text = ("%s %s (depuis %s) — %s : %s"
                % (REFUSAL_PREFIX, label, _duration(row["age_s"]),
                   row["reason"], row["detail"]))
        if row["eligible"]:
            text += "; éligibles : %s" % ", ".join(row["eligible"])
        if row["asleep"]:
            text += "; endormis : %s" % ", ".join(row["asleep"])
        try:
            work.note(db, ident, text, actor=SENDER)
        except Exception:  # noqa: BLE001 - le journal ne bloque pas la passe
            continue
        if not row["startable"] or not orchestras:
            continue
        # Un courrier par motif, pas par heure : la note du journal est le
        # journal, le courrier est l'alerte. Un motif nouveau (ou un élément
        # qui redevient démarrable) alerte de nouveau.
        try:
            previous = work.events_noted(db, ident, REFUSAL_PREFIX, 0.0)
        except Exception:  # noqa: BLE001 - pas d'historique : on alerte
            previous = []
        marker = " — %s :" % row["reason"]
        same = [event for event in previous
                if marker in str(event.get("note") or "")]
        if len(same) > 1:
            continue
        _alert_pilot(db, orchestras, row, label, text)
    return rows


def _alert_pilot(db, orchestras, row: dict, label: str, text: str) -> None:
    """Le courrier d'information au pilote : motif, âge, agents éligibles."""
    from . import mail
    from .backlog import SENDER
    body = (
        "L165 take deadline: lot #%d \"%s\" has no assignee after %s (%s, %s). "
        "Reason: %s. A startable lot without an assignee must be taken "
        "automatically; if it cannot, the pilot is told why (lot #8119). "
        "Eligible agents: %s. Asleep: %s. The journal line is `%s`."
        % (row["id"], row["title"], _duration(row["age_s"]),
           "basket" if row["kind"] == "basket" else "lot", row["state"],
           label, ", ".join(row["eligible"]) or "none",
           ", ".join(row["asleep"]) or "none", text))
    payload = {"info": True,
               "take_deadline": {"lot": row["id"], "reason": row["reason"],
                                 "age_s": int(row["age_s"])}}
    for name in orchestration_names(orchestras):
        try:
            mail.send(db, SENDER, name, body, kind="event",
                      work_item_id=str(row["id"]), payload=payload)
        except Exception:  # noqa: BLE001 - un courrier en échec ne bloque rien
            continue


#: préfixe des notes de rattrapage historique (distinct de REFUSAL_PREFIX :
#: le journal courant ne doit pas prendre une note d'histoire pour sa note
#: horaire)
HISTORY_PREFIX = "historique des refus L165:"
#: fenêtre du rattrapage au démarrage d'un service de notification
HISTORY_WINDOW_S = 48 * 3600.0


def _is_assignment_note(note) -> bool:
    from .storage.interface import ASSIGN_NOTE_PREFIXES
    return str(note or "").strip().startswith(ASSIGN_NOTE_PREFIXES)


def _is_return_note(note) -> bool:
    from .storage.interface import RETURNED_NOTE
    text = str(note or "").strip()
    return (text.startswith(RETURNED_NOTE)
            or text.startswith("pause budget (reprise) : lot rendu à la file"))


def _ids_after(note: str, marker: str) -> set[int]:
    """Les `#N` d'une note de journal, après `marker` (ex. empreinte)."""
    tail = note.split(marker, 1)[1] if marker in note else ""
    out = set()
    for token in tail.replace(",", " ").split():
        token = token.strip("();[]")
        if token.startswith("#") and token[1:].isdigit():
            out.add(int(token[1:]))
    return out


def _history_episodes(db, since: float, until: float) -> list[dict]:
    """Les épisodes sans assigné d'éléments dont la vie croise la fenêtre.

    Un épisode commence à la création (ou à un retour à la file) et finit à
    la première attribution ou fermeture ; il est coupé à `until`. Les
    événements sont la seule source : `work_items` ne garde que l'état courant.
    """
    from . import backlog, storage
    st = storage.of(db)
    items = db.query(
        "SELECT id, type, app, workstream, team, title, state, assignee,"
        " external_executor, created_at, closed_at,"
        " extract(epoch from created_at)::float8 AS created_ts,"
        " extract(epoch from closed_at)::float8 AS closed_ts"
        " FROM work_items"
        " WHERE created_at <= to_timestamp(%s)"
        " AND (closed_at IS NULL OR closed_at >= to_timestamp(%s))"
        " ORDER BY id", (float(until), float(since)))
    by_item: dict[int, list] = {}
    ids = [int(item["id"]) for item in items]
    if ids:
        marks = ", ".join(["%s"] * len(ids))
        try:
            rows = db.query(
                "SELECT work_item_id, state, note, actor,"
                " extract(epoch from created_at)::float8 AS created_ts"
                " FROM work_item_events"
                " WHERE work_item_id IN (" + marks + ")"
                " AND created_at <= to_timestamp(%s)"
                " ORDER BY work_item_id, id", tuple(ids) + (float(until),))
        except Exception:  # noqa: BLE001 - un journal illisible rend l'épisode indéterminé
            rows = []
        for row in rows:
            by_item.setdefault(int(row["work_item_id"]), []).append(row)
    out = []
    for item in items:
        ident = int(item["id"])
        events = by_item.get(ident, [])
        start = float(item.get("created_ts") or since)
        for event in events:
            ts = float(event.get("created_ts") or 0.0)
            note = str(event.get("note") or "")
            if ts > until:
                break
            if _is_return_note(note):
                start = ts
                continue
            if _is_assignment_note(note):
                if start is not None:
                    _append_episode(out, item, events, start, ts, since, until,
                                    "assigned", note=note)
                start = None
                continue
            if event.get("state") in lifecycle.COMPLETED and start is not None:
                _append_episode(out, item, events, start, ts, since, until,
                                "closed")
                start = None
        if start is not None:
            _append_episode(out, item, events, start, until, since, until, "open")
    return out


def _append_episode(out, item, events, start, end, since, until, ended, *,
                    note="") -> None:
    start = max(float(start), float(since))
    end = min(float(end), float(until))
    if end <= start:
        return
    out.append({"item": item, "events": events, "start": start, "end": end,
                "ended": ended, "assign_note": note})


def _state_before(events, moment: float) -> tuple[set, set, bool]:
    """(prérequis, empreintes, hold) au début d'un épisode, d'après le journal."""
    from . import planned_waits
    deps: set[int] = set()
    footprints: set[int] = set()
    held = False
    for event in events:
        ts = float(event.get("created_ts") or 0.0)
        if ts > moment:
            break
        note = str(event.get("note") or "")
        if note.startswith("Dependency added: lot #"):
            deps |= _ids_after(note, "Dependency added:")
        elif note.startswith("Dependency removed: lot #"):
            deps -= _ids_after(note, "Dependency removed:")
        elif note.startswith("en attente d'empreinte"):
            footprints |= _ids_after(note, "en attente d'empreinte")
        elif note.startswith("empreinte libérée"):
            footprints -= _ids_after(note, "empreinte libérée")
        elif planned_waits.is_hold_note(note):
            held = True
        elif planned_waits.is_lift_note(note):
            held = False
    return deps, footprints, held


def _episode_evidence(episode: dict) -> tuple[dict, dict, float]:
    """(couverture des prérequis, couverture des empreintes, durée du hold).

    Chaque bloqueur est chronométré sur l'épisode : un prérequis ou une
    empreinte nommé au début, ou apparu en route, ne compte que s'il couvre au
    moins la moitié de l'attente. Une libération juste avant la prise (empreinte
    rendue, prérequis retiré) ne doit pas effacer la cause de l'attente ; un
    blocage d'une minute avant la fin ne doit pas la résumer non plus.
    """
    from . import planned_waits
    start, end = float(episode["start"]), float(episode["end"])
    deps, footprints, held = _state_before(episode["events"], start)
    dep_since = {ident: start for ident in deps}
    fp_since = {ident: start for ident in footprints}
    dep_cov: dict[int, float] = {}
    fp_cov: dict[int, float] = {}
    held_since = start if held else None
    held_total = 0.0
    for event in episode["events"]:
        ts = float(event.get("created_ts") or 0.0)
        if ts <= start or ts > end:
            continue
        note = str(event.get("note") or "")
        if note.startswith("Dependency added: lot #"):
            for ident in _ids_after(note, "Dependency added:"):
                dep_since.setdefault(ident, ts)
        elif note.startswith("Dependency removed: lot #"):
            for ident in _ids_after(note, "Dependency removed:"):
                if ident in dep_since:
                    dep_cov[ident] = dep_cov.get(ident, 0.0) + ts - dep_since.pop(ident)
        elif note.startswith("en attente d'empreinte"):
            for ident in _ids_after(note, "en attente d'empreinte"):
                fp_since.setdefault(ident, ts)
        elif note.startswith("empreinte libérée"):
            for ident in _ids_after(note, "empreinte libérée"):
                if ident in fp_since:
                    fp_cov[ident] = fp_cov.get(ident, 0.0) + ts - fp_since.pop(ident)
        elif planned_waits.is_hold_note(note):
            if held_since is None:
                held_since = ts
        elif planned_waits.is_lift_note(note):
            if held_since is not None:
                held_total += ts - held_since
                held_since = None
    for ident, since in dep_since.items():
        dep_cov[ident] = dep_cov.get(ident, 0.0) + end - since
    for ident, since in fp_since.items():
        fp_cov[ident] = fp_cov.get(ident, 0.0) + end - since
    if held_since is not None:
        held_total += end - held_since
    return dep_cov, fp_cov, held_total


def _episode_reason(item: dict, episode: dict, lots: list[dict]) -> tuple[str, str, str]:
    """`(raison, détail, certitude)` d'un épisode, d'après les seules preuves."""
    from . import file_decoupage
    ident = int(item["id"])
    if file_decoupage.is_split_lot(item):
        return "split", "lot de découpage (geste du pilote)", "observed"
    if (item.get("external_executor") or "").strip():
        return ("external", "réservé à %s" % item["external_executor"], "observed")
    start, end = float(episode["start"]), float(episode["end"])
    wait = end - start
    dep_cov, fp_cov, held_total = _episode_evidence(episode)
    if held_total >= wait / 2.0:
        return "hold", "hold de pilote vivant pendant l'attente", "observed"
    deps = sorted((ident for ident, cover in dep_cov.items() if cover >= wait / 2.0))
    if deps:
        return ("prerequisite", "prérequis ouvert(s) : %s" % ", ".join(
            "#%d" % i for i in deps), "observed")
    footprints = sorted(ident for ident, cover in fp_cov.items()
                        if cover >= wait / 2.0)
    if footprints:
        return ("footprint", "empreinte tenue par %s" % ", ".join(
            "#%d" % i for i in footprints), "observed")
    if item.get("type") == "improvement":
        overlapping = [lot for lot in lots
                       if int(lot["item"]["id"]) != ident
                       and lot["project"] == episode["project"]
                       and lot["start"] < end and lot["end"] > start]
        if overlapping:
            return ("roadmap", "lot(s) de l'équipe sans preneur : %s" % ", ".join(
                "#%d" % int(lot["item"]["id"]) for lot in overlapping[:5]), "inferred")
        return ("indeterminate", "aucun motif daté pour ce panier ; aucun lot "
                "de l'équipe sans preneur pendant l'attente", "indeterminate")
    return ("indeterminate", "aucun prérequis, empreinte ou hold ne couvre la "
            "moitié de l'attente dans le journal (%d événement(s))"
            % len(episode["events"]), "indeterminate")


def history(cfg, db, *, since: float, until: float | None = None,
            min_wait: float = TAKE_DEADLINE_S) -> list[dict]:
    """Les attentes sans assigné d'une période, reconstituées depuis le journal.

    Le journal vivant (`explain`) ne voit que l'état courant : un lot resté
    sans assigné le 22/11 puis pris ou clos avant le déploiement en disparaît.
    `history` relit les événements conservés (création, attribution, retours à
    la file, prérequis, empreintes, holds) et rend, pour chaque épisode d'au
    moins `min_wait`, la raison prouvée ou `indeterminate` quand le journal ne
    la porte pas. La certitude dit laquelle des deux : `observed` (un
    événement nomme le motif), `inferred` (un lot de l'équipe sans preneur
    explique le panier), `indeterminate`.
    """
    from . import backlog
    until = time.time() if until is None else float(until)
    episodes = _history_episodes(db, float(since), until)
    for episode in episodes:
        episode["project"] = backlog._lot_project(episode["item"])
    lots = [episode for episode in episodes
            if episode["item"].get("type") != "improvement"]
    rows = []
    for episode in episodes:
        item = episode["item"]
        wait = float(episode["end"]) - float(episode["start"])
        if wait < float(min_wait):
            continue
        reason, detail, certainty = _episode_reason(item, episode, lots)
        rows.append({
            "id": int(item["id"]),
            "kind": "basket" if item.get("type") == "improvement" else "lot",
            "title": item.get("title") or "",
            "team": episode["project"],
            "state": item.get("state") or "",
            "assignee": item.get("assignee") or "",
            "from_ts": float(episode["start"]),
            "to_ts": float(episode["end"]),
            "wait_s": wait,
            "reason": reason,
            "detail": detail,
            "certainty": certainty,
            "ended": episode["ended"] != "open",
            "closed": item.get("state") in lifecycle.COMPLETED,
        })
    rows.sort(key=lambda row: (-row["wait_s"], row["id"]))
    return rows


def _moment(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(float(ts)))


def journal_history(cfg, db, *, now: float | None = None,
                    window: float = HISTORY_WINDOW_S,
                    min_wait: float = TAKE_DEADLINE_S) -> list[dict]:
    """Rattrapage au démarrage : note les attentes historiques une fois.

    Un élément encore sans assigné est déjà couvert par `journal` ; seuls les
    épisodes finis (pris ou clos) reçoivent la note `HISTORY_PREFIX`. Une note
    par élément au plus, quel que soit le nombre d'épisodes : le journal du lot
    garde la première attente reconstituée, les suivantes se lisent dans
    `history`.
    """
    from . import work
    from .backlog import SENDER
    now = time.time() if now is None else float(now)
    rows = history(cfg, db, since=now - float(window), until=now,
                   min_wait=min_wait)
    written = []
    for row in rows:
        if not row["ended"]:
            continue
        ident = int(row["id"])
        try:
            if work.events_noted(db, ident, HISTORY_PREFIX, 0.0):
                continue
            text = ("%s %s (du %s au %s, %s) — %s : %s [%s]"
                    % (HISTORY_PREFIX, _LABELS.get(row["reason"], row["reason"]),
                       _moment(row["from_ts"]), _moment(row["to_ts"]),
                       _duration(row["wait_s"]), row["reason"], row["detail"],
                       row["certainty"]))
            work.note(db, ident, text, actor=SENDER)
        except Exception:  # noqa: BLE001 - le rattrapage ne bloque pas la passe
            continue
        written.append(row)
    return written


def orchestration_names(orchestras) -> list[str]:
    """Les destinataires d'une alerte : des noms, sans doublon."""
    if not orchestras:
        return []
    if isinstance(orchestras, dict):
        orchestras = orchestras.keys()
    return sorted({str(name).strip() for name in orchestras or () if str(name).strip()})


def wake_deadline(cfg, db, *, now: float | None = None, listing: list | None = None,
                  min_age: float = TAKE_DEADLINE_S) -> list[dict]:
    """L'échéance : un élément démarrable sans assigné depuis plus de
    `min_age` alors que des agents éligibles dorment réveille ces agents.

    Seul le sommeil automatique du découpage est levé (`_wake` vérifie la
    raison exacte et l'acteur) : une pause humaine ou budgétaire n'est jamais
    touchée. La passe de prise qui appelle ce geste relit ensuite son relevé
    et prend l'élément. Rend `[{lot, project, agents}]`.
    """
    from . import file_decoupage, storage
    now = time.time() if now is None else float(now)
    st = storage.of(db)
    if listing is None:
        listing = st.operations.listing()
    rows = [row for row in explain(cfg, db, now=now, listing=listing, min_age=min_age)
            if row["reason"] == "asleep" and row["startable"]]
    out = []
    for row in rows:
        project = row["team"]
        if not project or not row["asleep"]:
            continue
        reason = ("%s #%d without an assignee for %s, eligible agent(s) asleep: %s"
                  % (DEADLINE_PREFIX, row["id"], _duration(row["age_s"]),
                     ", ".join(row["asleep"])))
        try:
            woken = file_decoupage._wake(db, project, listing, reason=reason)
        except Exception:  # noqa: BLE001 - la pause expire d'elle-même (#3961)
            continue
        if woken:
            out.append({"lot": row["id"], "project": project, "agents": woken})
    return out
