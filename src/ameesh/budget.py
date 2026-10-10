# SPDX-License-Identifier: AGPL-3.0-only
"""Plafonds de budget du mesh, réglés en base (lot L70, décision 0019 §2, R20).

  ameesh budget [--json] [--last N]
        plafonds en vigueur et leur source (base, config, défaut), dépense
        payée au token sur 1 h et 24 h, agents en pause budget, derniers
        changements ;
  ameesh budget set [--per-hour X] [--per-day Y] [--agent A]
        pose un plafond du mesh (sans `--agent`) ou propre à un agent ;
  ameesh budget unset [--per-hour] [--per-day] [--agent A]
        retire le réglage en base : le plafond revient à la configuration de
        l'hôte (mesh) ou disparaît (agent).

**Précédence** (plafonds du mesh) : base > configuration de l'hôte
(`budget_usd_per_hour`, `budget_usd_per_day`, ou leur variable
d'environnement) > défaut (10 $/h, pas de plafond par jour). Un plafond
d'agent n'existe qu'en base ; il s'ajoute aux plafonds du mesh.

**Règle** (identique pour l'heure et le jour, fenêtres glissantes) : seule la
dépense des harnais **payés au token** compte, et un agent au forfait n'est
jamais mis en pause par ces plafonds (L49) ; le plafond du mesh somme tous les
agents, celui d'un agent sa seule dépense.

**À chaud.** Les exécuteurs relisent la table au plus tous les `DEFAULT_TTL`
secondes (cache court, partagé par leurs workers), et tout de suite sur le
réveil `ameesh_budget` qu'émet la base à chaque changement. Une base sans la
migration 0042, ou injoignable au moment de la lecture, laisse la dernière
valeur lue — à défaut, la configuration de l'hôte.

**Autorité.** Seul un humain habilité règle le budget : une session d'agent
(`AGENT_MAIL_NAME` posée, ou session liée par `ameesh mail bind`) est
refusée ; l'humain est l'utilisateur Unix (`human:<user>`) et doit figurer
dans `humans` de la configuration quand la liste est déclarée, ou être le
responsable de l'agent visé. Chaque changement est journalisé en base
(`budget_events` : acteur, ancienne et nouvelle valeur) et dans le fil.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from . import db as db_mod
from . import fil, identity, registry, storage
from .config import Config

#: fenêtres réglables : option → secondes
WINDOWS = {"per_hour": 3600, "per_day": 86400}
#: clé de configuration de l'hôte de chaque fenêtre (plafonds du mesh)
CONFIG_KEYS = {3600: "budget_usd_per_hour", 86400: "budget_usd_per_day"}
LABELS = {3600: "par heure", 86400: "par jour"}

SOURCE_DB = "base"
SOURCE_CONFIG = "config"
SOURCE_DEFAULT = "défaut"

#: durée de vie du cache des exécuteurs (secondes)
DEFAULT_TTL = 15.0
#: préfixe du statut d'un agent en pause budget (`runner.budget_ok`)
PAUSE_PREFIX = "budget : "


class BudgetError(RuntimeError):
    """Réglage refusé (autorité, valeur) — toujours avec une raison lisible."""


@dataclass(frozen=True)
class Limits:
    """Les plafonds en vigueur : ceux du mesh avec leur source, et ceux des agents."""

    per_hour: float
    per_hour_source: str
    per_day: float
    per_day_source: str
    #: {agent: {3600|86400: usd}} (base seulement)
    agents: dict = field(default_factory=dict)
    #: lignes brutes de la base (qui, quand), pour l'affichage
    rows: tuple = ()

    def book_kwargs(self) -> dict:
        """Les arguments de `cost.CostBook` qui portent ces plafonds."""
        return {"hourly_usd": self.per_hour, "daily_usd": self.per_day,
                "agent_limits": {a: dict(v) for a, v in self.agents.items()}}

    def as_dict(self) -> dict:
        """Forme JSON (`ameesh budget --json`, `cost report`, `progress`)."""
        return {
            "per_hour_usd": self.per_hour or None, "per_hour_source": self.per_hour_source,
            "per_day_usd": self.per_day or None, "per_day_source": self.per_day_source,
            "agents": {a: {"per_hour_usd": v.get(3600), "per_day_usd": v.get(86400)}
                       for a, v in sorted(self.agents.items())},
        }


def resolve(cfg: Config, rows: list | None, *, hourly_default: float | None = None) -> Limits:
    """Les plafonds en vigueur : base > configuration de l'hôte > défaut.

    `hourly_default` remplace `cfg.budget_usd_per_hour` (l'exécuteur passe
    le sien) ; sa source reste celle de la configuration.
    """
    rows = list(rows or [])
    mesh = {int(r["window_s"]): r for r in rows if not (r.get("scope") or "")}
    agents: dict = {}
    for r in rows:
        if r.get("scope"):
            agents.setdefault(r["scope"], {})[int(r["window_s"])] = float(r["usd"])
    explicit = set(getattr(cfg, "budget_explicit", ()) or ())
    values = []
    for window in (3600, 86400):
        if window in mesh:
            values += [float(mesh[window]["usd"]), SOURCE_DB]
            continue
        key = CONFIG_KEYS[window]
        local = float(hourly_default if (window == 3600 and hourly_default is not None)
                      else getattr(cfg, key, 0.0) or 0.0)
        values += [max(0.0, local), SOURCE_CONFIG if key in explicit else SOURCE_DEFAULT]
    return Limits(values[0], values[1], values[2], values[3], agents, tuple(rows))


def read_rows(db) -> list:
    """Les lignes `budget_limits` ; lève `db.DbError` (table absente, base en panne)."""
    return storage.of(db).budgets.limits()


def current(cfg: Config, db) -> Limits:
    """Lecture directe (CLI) : une base sans la migration 0042 vaut « rien en base »."""
    try:
        rows = read_rows(db)
    except db_mod.DbError:
        rows = []
    return resolve(cfg, rows)


class Cache:
    """Cache court des plafonds en base, partagé par les workers d'un exécuteur.

    Une lecture en échec garde la dernière valeur lue (ou « rien en base ») et
    ne se retente qu'au délai suivant : la garde ne tombe jamais faute de base,
    et ne sonde pas la base à chaque tour quand la table manque.
    """

    def __init__(self, ttl: float = DEFAULT_TTL, clock=time.monotonic) -> None:
        self.ttl = max(0.0, float(ttl))
        self.clock = clock
        self._lock = threading.Lock()
        self._rows: list = []
        self._at: float | None = None
        self.error = ""

    def invalidate(self) -> None:
        with self._lock:
            self._at = None

    def rows(self, db) -> list:
        with self._lock:
            now = self.clock()
            if self._at is not None and now - self._at < self.ttl:
                return list(self._rows)
            try:
                self._rows = read_rows(db)
                self.error = ""
            except db_mod.DbError as exc:
                self.error = " ".join(str(exc).split())[:200]
            self._at = now
            return list(self._rows)

    def limits(self, cfg: Config, db, *, hourly_default: float | None = None) -> Limits:
        return resolve(cfg, self.rows(db), hourly_default=hourly_default)


# --------------------------------------------------------------------------
# autorité
# --------------------------------------------------------------------------

def unix_user() -> str:
    return os.environ.get("USER") or getpass.getuser()


def authorize(cfg: Config, db, agent: str | None = None) -> str:
    """L'acteur humain (`human:<user>`) habilité à régler ce budget, ou BudgetError.

    Refusé : toute session d'agent (AGENT_MAIL_NAME posée, ou session externe
    liée à un agent). Admis : un membre de `humans` (configuration de l'hôte)
    quand la liste est déclarée, ou le responsable de l'agent visé ; sans
    liste ni responsable (banc), tout humain de l'hôte.
    """
    name = os.environ.get("AGENT_MAIL_NAME") or ""
    if name:
        raise BudgetError("refus : cette session est celle de l'agent %s (AGENT_MAIL_NAME) ; "
                          "seul un humain habilité règle le budget" % name)
    binding = identity.resolve_binding(cfg, db)
    if binding.name:
        raise BudgetError("refus : cette session est liée à l'agent %s (ameesh mail bind) ; "
                          "seul un humain habilité règle le budget" % binding.name)
    user = unix_user()
    actor = "human:%s" % user
    humans = cfg.human_names
    if user in humans:
        return actor
    if agent:
        row = registry.get(db, agent)
        if row is None:
            raise BudgetError("agent inconnu : %s" % agent)
        responsible = (row.get("responsible") or "").strip()
        if responsible == actor:
            return actor
        if responsible or humans:
            raise BudgetError(
                "refus : %s n'est ni membre humain du mesh (humans) ni responsable de %s (%s)"
                % (actor, agent, responsible or "aucun"))
        return actor
    if humans:
        raise BudgetError("refus : %s n'est pas membre humain du mesh (humans : %s)"
                          % (actor, ", ".join(sorted(humans))))
    return actor


# --------------------------------------------------------------------------
# réglage
# --------------------------------------------------------------------------

def _fmt_usd(value) -> str:
    return "aucun" if value is None else "%.2f $" % float(value)


def change(cfg: Config, db, *, agent: str | None, window: int, usd: float | None,
           actor: str) -> tuple[bool, float | None]:
    """Pose (`usd`) ou retire (None) un plafond ; journalise base + fil."""
    scope = agent or ""
    changed, old = storage.of(db).budgets.put(scope, window, usd, actor=actor)
    if changed:
        portee = "de l'agent %s" % agent if agent else "du mesh"
        suite = "" if usd is not None else (
            " (retour à la configuration de l'hôte)" if not agent else "")
        texte = ("Budget %s %s : %s → %s, par %s%s." % (
            portee, LABELS[window], _fmt_usd(old), _fmt_usd(usd), actor, suite))
        fil.record(cfg, db, sender=actor, recipients=[agent] if agent else [], text=texte,
                   meta={"action": "budget", "audit": "budget", "scope": scope or "mesh",
                         "window_s": window, "old_usd": old, "new_usd": usd})
    return changed, old


# --------------------------------------------------------------------------
# lecture : dépense et pauses
# --------------------------------------------------------------------------

def status(cfg: Config, db, *, last: int = 5) -> dict:
    """L'état affiché par `ameesh budget` (schéma `ameesh-budget/1`)."""
    from . import cost as cost_mod
    limits = current(cfg, db)
    book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db)
    spend: dict = {"paid_harnesses": None, "1h_usd": None, "24h_usd": None, "agents": {}}
    try:
        paid = cost_mod.paid_harnesses_of()
        spend["paid_harnesses"] = list(paid)
        spend["1h_usd"] = round(book.spent("all", 3600, harnesses=paid), 6)
        spend["24h_usd"] = round(book.spent("all", 86400, harnesses=paid), 6)
        for agent in sorted(limits.agents):
            spend["agents"][agent] = {
                "1h_usd": round(book.spent(agent, 3600, harnesses=paid), 6),
                "24h_usd": round(book.spent(agent, 86400, harnesses=paid), 6)}
    except cost_mod.CostError as exc:
        spend["error"] = str(exc)
    paused = [{"agent": r["name"], "reason": (r.get("status_text") or "")[len(PAUSE_PREFIX):]}
              for r in registry.overview(db)
              if r.get("status") == "blocked"
              and (r.get("status_text") or "").startswith(PAUSE_PREFIX)]
    try:
        events = storage.of(db).budgets.events(max(0, last))
    except db_mod.DbError:
        events = []
    return {"schema": "ameesh-budget/1", "limits": limits.as_dict(),
            "rows": list(limits.rows), "spend": spend, "paused": paused, "events": events}


def _moment(epoch) -> str:
    if not epoch:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(epoch)))


def summary_line(limits: Limits) -> str:
    """Une ligne : les plafonds du mesh en vigueur et leur source."""
    def one(value: float, source: str) -> str:
        return "%s (%s)" % ("%.2f $" % value if value > 0 else "aucun", source)
    text = "plafonds du mesh (payé au token) : %s %s · %s %s" % (
        LABELS[3600], one(limits.per_hour, limits.per_hour_source),
        LABELS[86400], one(limits.per_day, limits.per_day_source))
    if limits.agents:
        text += " · %d agent(s) à plafond propre" % len(limits.agents)
    return text


def format_status(data: dict) -> str:
    lim = data["limits"]
    by = {(r.get("scope") or "", int(r["window_s"])): r for r in data["rows"]}
    out = ["plafonds du mesh (payé au token, fenêtres glissantes) :"]
    for window, key in ((3600, "per_hour"), (86400, "per_day")):
        value = lim["%s_usd" % key]
        source = lim["%s_source" % key]
        row = by.get(("", window))
        qui = " — %s, %s" % (row["set_by"], _moment(row.get("updated_ts"))) if row else ""
        out.append("  %-9s : %-10s (%s%s)" % (LABELS[window], _fmt_usd(value), source, qui))
    if lim["agents"]:
        out.append("plafonds par agent (base) :")
        for agent, caps in lim["agents"].items():
            dep = data["spend"]["agents"].get(agent) or {}
            out.append("  %-16s %s %s · %s %s   dépense 1 h %s · 24 h %s" % (
                agent, LABELS[3600], _fmt_usd(caps["per_hour_usd"]),
                LABELS[86400], _fmt_usd(caps["per_day_usd"]),
                _fmt_usd(dep.get("1h_usd")), _fmt_usd(dep.get("24h_usd"))))
    sp = data["spend"]
    if sp.get("error"):
        out.append("dépense payée au token : illisible (%s)" % sp["error"])
    else:
        out.append("dépense payée au token (%s) : 1 h %s · 24 h %s" % (
            ", ".join(sp["paid_harnesses"] or []) or "aucun harnais",
            _fmt_usd(sp["1h_usd"]), _fmt_usd(sp["24h_usd"])))
    if data["paused"]:
        out.append("en pause budget :")
        for p in data["paused"]:
            out.append("  %-16s %s" % (p["agent"], p["reason"]))
    else:
        out.append("en pause budget : aucun agent")
    if data["events"]:
        out.append("derniers changements :")
        for ev in data["events"]:
            out.append("  %s  %-14s %-9s %s → %s  %s" % (
                _moment(ev.get("at_ts")), ev["scope"] or "mesh", LABELS.get(
                    int(ev["window_s"]), ev["window_s"]),
                _fmt_usd(ev.get("old_usd")), _fmt_usd(ev.get("new_usd")), ev["actor"]))
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_parsers(sub) -> None:
    """`ameesh budget [set|unset]` dans le parseur de `mesh_cli`."""
    p = sub.add_parser("budget", help="plafonds de budget du mesh, en base (L70)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--last", type=int, default=5, help="derniers changements montrés")
    bsub = p.add_subparsers(dest="budget_command")
    ps = bsub.add_parser("set", help="poser un plafond (mesh, ou --agent)")
    ps.add_argument("--per-hour", type=float, default=None, metavar="USD")
    ps.add_argument("--per-day", type=float, default=None, metavar="USD")
    ps.add_argument("--agent", default=None)
    pu = bsub.add_parser("unset", help="retirer un plafond réglé en base")
    pu.add_argument("--per-hour", action="store_true")
    pu.add_argument("--per-day", action="store_true")
    pu.add_argument("--agent", default=None)
    pshow = bsub.add_parser("show", help="plafonds en vigueur (défaut)")
    pshow.add_argument("--json", action="store_true")
    pshow.add_argument("--last", type=int, default=5)
    p.set_defaults(func=cmd_budget)


def cmd_budget(cfg: Config, args: argparse.Namespace) -> int:
    what = getattr(args, "budget_command", None) or "show"
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        if what == "show":
            data = status(cfg, db, last=getattr(args, "last", 5))
            if getattr(args, "json", False):
                print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            else:
                print(format_status(data))
            return 0
        if what == "set":
            demandes = {w: v for w, v in ((3600, args.per_hour), (86400, args.per_day))
                        if v is not None}
            if not demandes:
                print("usage : ameesh budget set --per-hour X [--per-day Y] [--agent A]",
                      file=sys.stderr)
                return 2
            for window, value in demandes.items():
                if not value > 0 or value != value or value == float("inf"):
                    print("plafond %s invalide : %r (strictement positif ; pour revenir à "
                          "la configuration : ameesh budget unset)" % (LABELS[window], value),
                          file=sys.stderr)
                    return 2
        else:
            demandes = {w: None for w, flag in ((3600, args.per_hour), (86400, args.per_day))
                        if flag}
            if not demandes:
                print("usage : ameesh budget unset [--per-hour] [--per-day] [--agent A]",
                      file=sys.stderr)
                return 2
        if what == "set" and args.agent and registry.get(db, args.agent) is None:
            print("agent inconnu : %s" % args.agent, file=sys.stderr)
            return 1
        try:
            actor = authorize(cfg, db, args.agent)
        except BudgetError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        for window, value in demandes.items():
            changed, old = change(cfg, db, agent=args.agent, window=window, usd=value,
                                  actor=actor)
            portee = args.agent or "mesh"
            if changed:
                print("budget %s %s : %s → %s (par %s ; effet immédiat sur tous les exécuteurs)"
                      % (portee, LABELS[window], _fmt_usd(old), _fmt_usd(value), actor))
            else:
                print("budget %s %s : inchangé (%s)" % (portee, LABELS[window], _fmt_usd(old)))
        print(summary_line(current(cfg, db)))
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    from . import mesh_cli
    return mesh_cli.main(["budget"] + list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
