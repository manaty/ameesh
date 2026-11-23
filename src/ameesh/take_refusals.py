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
