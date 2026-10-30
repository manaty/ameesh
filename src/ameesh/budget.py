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
import re
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
LABELS = {3600: "per hour", 86400: "per day"}

SOURCE_DB = "base"
SOURCE_CONFIG = "config"
SOURCE_DEFAULT = "défaut"
#: display label of each source (the values above are stored/emitted as is)
SOURCE_LABELS = {SOURCE_DB: "database", SOURCE_CONFIG: "config", SOURCE_DEFAULT: "default"}


def source_label(source: str) -> str:
    """The English label of a cap source, for display only."""
    return SOURCE_LABELS.get(source, source)

#: durée de vie du cache des exécuteurs (secondes)
DEFAULT_TTL = 15.0
#: préfixe du statut d'un agent en pause budget (`runner.budget_ok`)
PAUSE_PREFIX = "budget : "
#: L242b (5) : après `PAUSE_PREFIX`, la pause d'un agent critique (posée par
#: l'exécuteur, seul à savoir qui l'est) — alerte urgente `critical_budget_pause`
CRITICAL_MARK = "critical agent: "


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
        raise BudgetError("refused: this session belongs to agent %s (AGENT_MAIL_NAME); "
                          "only an authorized human sets the budget" % name)
    binding = identity.resolve_binding(cfg, db)
    if binding.name:
        raise BudgetError("refused: this session is bound to agent %s (ameesh mail bind); "
                          "only an authorized human sets the budget" % binding.name)
    user = unix_user()
    actor = "human:%s" % user
    humans = cfg.human_names
    if user in humans:
        return actor
    if agent:
        row = registry.get(db, agent)
        if row is None:
            raise BudgetError("unknown agent: %s" % agent)
        responsible = (row.get("responsible") or "").strip()
        if responsible == actor:
            return actor
        if responsible or humans:
            raise BudgetError(
                "refused: %s is neither a human member of the mesh (humans) nor responsible "
                "for %s (%s)" % (actor, agent, responsible or "none"))
        return actor
    if humans:
        raise BudgetError("refused: %s is not a human member of the mesh (humans: %s)"
                          % (actor, ", ".join(sorted(humans))))
    return actor


# --------------------------------------------------------------------------
# réglage
# --------------------------------------------------------------------------

def _fmt_usd(value) -> str:
    return "none" if value is None else "%.2f $" % float(value)


def change(cfg: Config, db, *, agent: str | None, window: int, usd: float | None,
           actor: str, lower_only: bool = False,
           sender: str | None = None) -> tuple[bool, float | None]:
    """Pose (`usd`) ou retire (None) un plafond ; journalise base + fil.

    `sender` (#1497) : l'auteur de l'entrée du fil quand `actor` n'est pas un
    nom de membre nu — le `human:X for human:Y` d'un suppléant (L86) est une
    trace, posée alors en `meta.by`, pas un expéditeur.

    `lower_only` (L87, API) : BudgetError sauf si `usd` est sous le plafond en
    vigueur, comparé sous le verrou de la ligne ; sans ligne, le plafond du
    mesh est celui de la configuration (0 : aucun), et un agent sans plafond
    propre n'en a aucun."""
    scope = agent or ""
    ceiling = None
    if lower_only and not agent:
        configured = resolve(cfg, [])
        ceiling = (configured.per_hour if window == 3600 else configured.per_day) or None
    try:
        changed, old = storage.of(db).budgets.put(scope, window, usd, actor=actor,
                                                  lower_only=lower_only, ceiling=ceiling)
    except ValueError as exc:
        raise BudgetError("refused: %s" % exc) from exc
    if changed:
        portee = "of agent %s" % agent if agent else "of the mesh"
        suite = "" if usd is not None else (
            " (back to the host configuration)" if not agent else "")
        texte = ("Budget %s %s: %s → %s, by %s%s." % (
            portee, LABELS[window], _fmt_usd(old), _fmt_usd(usd), actor, suite))
        meta = {"action": "budget", "audit": "budget", "scope": scope or "mesh",
                "window_s": window, "old_usd": old, "new_usd": usd}
        if sender and sender != actor:
            meta["by"] = actor
        fil.record(cfg, db, sender=sender or actor, recipients=[agent] if agent else [],
                   text=texte, meta=meta)
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
        # L242b : ce que la garde compte contre le plafond horaire (baisse
        # réelle des soldes, l'estimation corrigée entre deux relevés)
        spend["1h_counted_usd"] = round(book.paid_hour(paid), 6)
        spend["noncritical_hourly_share"] = getattr(cfg, "noncritical_hourly_share", 1.0)
        spend["24h_usd"] = round(book.spent("all", 86400, harnesses=paid), 6)
        # #513 : idem contre le plafond par jour glissant
        spend["24h_counted_usd"] = round(book.paid_spent(paid, 86400), 6)
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
    from . import account_budget, provider_credits
    return {"schema": "ameesh-budget/1", "limits": limits.as_dict(),
            "rows": list(limits.rows), "spend": spend, "paused": paused, "events": events,
            # #1469 : enveloppes par compte de facturation (jour calendaire)
            "accounts": account_budget.usage(db),
            # #4164 : l'état réel des crédits de chaque compte fournisseur, à
            # côté des enveloppes (une enveloppe ne voit pas un compte épuisé)
            "provider_credits": provider_credits.listing(db)}


def _moment(epoch) -> str:
    if not epoch:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(epoch)))


def summary_line(limits: Limits) -> str:
    """Une ligne : les plafonds du mesh en vigueur et leur source."""
    def one(value: float, source: str) -> str:
        return "%s (%s)" % ("%.2f $" % value if value > 0 else "none", source_label(source))
    text = "mesh caps (pay-per-token): %s %s · %s %s" % (
        LABELS[3600], one(limits.per_hour, limits.per_hour_source),
        LABELS[86400], one(limits.per_day, limits.per_day_source))
    if limits.agents:
        text += " · %d agent(s) with their own cap" % len(limits.agents)
    return text


def format_status(data: dict) -> str:
    lim = data["limits"]
    by = {(r.get("scope") or "", int(r["window_s"])): r for r in data["rows"]}
    out = ["mesh caps (pay-per-token, sliding windows):"]
    for window, key in ((3600, "per_hour"), (86400, "per_day")):
        value = lim["%s_usd" % key]
        source = source_label(lim["%s_source" % key])
        row = by.get(("", window))
        qui = " — %s, %s" % (row["set_by"], _moment(row.get("updated_ts"))) if row else ""
        out.append("  %-9s: %-10s (%s%s)" % (LABELS[window], _fmt_usd(value), source, qui))
    if lim["agents"]:
        out.append("per-agent caps (database):")
        for agent, caps in lim["agents"].items():
            dep = data["spend"]["agents"].get(agent) or {}
            out.append("  %-16s %s %s · %s %s   spend 1 h %s · 24 h %s" % (
                agent, LABELS[3600], _fmt_usd(caps["per_hour_usd"]),
                LABELS[86400], _fmt_usd(caps["per_day_usd"]),
                _fmt_usd(dep.get("1h_usd")), _fmt_usd(dep.get("24h_usd"))))
    sp = data["spend"]
    if sp.get("error"):
        out.append("pay-per-token spend: unreadable (%s)" % sp["error"])
    else:
        out.append("pay-per-token spend (%s): 1 h %s · 24 h %s" % (
            ", ".join(sp["paid_harnesses"] or []) or "no harness",
            _fmt_usd(sp["1h_usd"]), _fmt_usd(sp["24h_usd"])))
        if sp.get("1h_counted_usd") is not None:
            share = sp.get("noncritical_hourly_share")
            out.append("hourly cap counts %s (real balance drop; the estimate between two "
                       "readings)%s" % (
                           _fmt_usd(sp["1h_counted_usd"]),
                           "; non-critical agents stop at %.0f%% of it" % (share * 100)
                           if share is not None and share < 1 else ""))
        if sp.get("24h_counted_usd") is not None:
            out.append("daily cap counts %s (same rule)" % _fmt_usd(sp["24h_counted_usd"]))
    if data.get("accounts"):
        from . import account_budget
        out.append("billing account envelopes (calendar day, shared by all agents and hosts):")
        for a in data["accounts"]:
            out.append("  %-18s limit %s · spent %s · reserved %s · kept %s · remaining %s"
                       "   day %s %s, reset %s" % (
                           a["billing_account"], _fmt_usd(a["usd"]), _fmt_usd(a["settled"]),
                           _fmt_usd(a["reserved"]), _fmt_usd(a["kept"]),
                           _fmt_usd(a["remaining"]), a["period"], a["tz"],
                           account_budget.moment(a.get("reset_ts"), a["tz"])))
    if "provider_credits" in data:
        from . import provider_credits
        rows = data["provider_credits"] or []
        if rows:
            out.append("provider accounts (real credit state, shared by all hosts):")
            for r in rows:
                out.append("  %-18s %s" % (provider_credits.who(r["harness"], r.get("account")),
                                           provider_credits.describe(r)))
        else:
            out.append("provider accounts: no credit exhaustion seen")
    if data["paused"]:
        out.append("paused for budget:")
        for p in data["paused"]:
            out.append("  %-16s %s" % (p["agent"], p["reason"]))
    else:
        out.append("paused for budget: no agent")
    if data["events"]:
        out.append("recent changes:")
        for ev in data["events"]:
            out.append("  %s  %-14s %-9s %s → %s  %s" % (
                _moment(ev.get("at_ts")), ev["scope"] or "mesh", LABELS.get(
                    int(ev["window_s"]), "per calendar day"),
                _fmt_usd(ev.get("old_usd")), _fmt_usd(ev.get("new_usd")), ev["actor"]))
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_parsers(sub) -> None:
    """`ameesh budget [set|unset]` dans le parseur de `mesh_cli`."""
    p = sub.add_parser("budget", help="mesh budget caps, stored in the database (L70)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--last", type=int, default=5, help="number of recent changes shown")
    bsub = p.add_subparsers(dest="budget_command")
    ps = bsub.add_parser("set", help="set a cap (mesh, or --agent)")
    ps.add_argument("--per-hour", type=float, default=None, metavar="USD")
    ps.add_argument("--per-day", type=float, default=None, metavar="USD")
    ps.add_argument("--agent", default=None)
    ps.add_argument("--account", default=None, metavar="BILLING_ACCOUNT",
                    help="#1469: daily envelope of a billing account (calendar day, --per-day)")
    ps.add_argument("--tz", default=None, help="zone of the account's calendar day "
                    "(default Europe/Paris)")
    pu = bsub.add_parser("unset", help="remove a cap set in the database")
    pu.add_argument("--per-hour", action="store_true")
    pu.add_argument("--per-day", action="store_true")
    pu.add_argument("--agent", default=None)
    pu.add_argument("--account", default=None, metavar="BILLING_ACCOUNT")
    pr = bsub.add_parser("release", help="#1469: release a kept account reservation "
                         "(uncertain turn checked by a human)")
    pr.add_argument("reservation", type=int)
    from . import tranche_budget
    tranche_budget.add_parser(bsub)
    # #4166 : prépayé déclaré d'un compte fournisseur sans API de solde
    ppre = bsub.add_parser(
        "prepaid", help="#4166: prepaid amount of a provider account (no balance "
        "API): `set`, `unset`, `show`")
    psub = ppre.add_subparsers(dest="prepaid_command")
    pps = psub.add_parser("set", help="declare the amount available NOW (a top-up "
                         "restarts the projection from this instant)")
    pps.add_argument("--harness", required=True, metavar="HARNESS")
    pps.add_argument("--account", default="", metavar="ACCOUNT",
                     help="logical account (default: the harness's default credential)")
    pps.add_argument("--usd", type=float, required=True, metavar="USD")
    pps.set_defaults(func=cmd_budget)
    ppu = psub.add_parser("unset", help="remove the declared prepaid amount")
    ppu.add_argument("--harness", required=True, metavar="HARNESS")
    ppu.add_argument("--account", default="", metavar="ACCOUNT")
    ppu.set_defaults(func=cmd_budget)
    ppshow = psub.add_parser("show", help="declared prepaid amounts and assessed "
                             "balances")
    ppshow.add_argument("--json", action="store_true")
    ppshow.set_defaults(func=cmd_budget)
    ppre.set_defaults(func=cmd_budget)
    pshow = bsub.add_parser("show", help="caps in force (default)")
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
        if what == "tranche":
            from . import tranche_budget
            return tranche_budget.command(cfg, db, args)
        if what == "prepaid":
            return _cmd_prepaid(cfg, db, args)
        if what == "release" or getattr(args, "account", None):
            return _cmd_account(cfg, db, args, what)
        if what == "set":
            demandes = {w: v for w, v in ((3600, args.per_hour), (86400, args.per_day))
                        if v is not None}
            if not demandes:
                print("usage: ameesh budget set --per-hour X [--per-day Y] [--agent A]",
                      file=sys.stderr)
                return 2
            for window, value in demandes.items():
                if not value > 0 or value != value or value == float("inf"):
                    print("invalid %s cap: %r (must be strictly positive; to go back to "
                          "the configuration: ameesh budget unset)" % (LABELS[window], value),
                          file=sys.stderr)
                    return 2
        else:
            demandes = {w: None for w, flag in ((3600, args.per_hour), (86400, args.per_day))
                        if flag}
            if not demandes:
                print("usage: ameesh budget unset [--per-hour] [--per-day] [--agent A]",
                      file=sys.stderr)
                return 2
        if what == "set" and args.agent and registry.get(db, args.agent) is None:
            print("unknown agent: %s" % args.agent, file=sys.stderr)
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
                print("budget %s %s: %s → %s (by %s; effective immediately on all runners)"
                      % (portee, LABELS[window], _fmt_usd(old), _fmt_usd(value), actor))
            else:
                print("budget %s %s: unchanged (%s)" % (portee, LABELS[window], _fmt_usd(old)))
        print(summary_line(current(cfg, db)))
        return 0
    finally:
        db.close()


def _cmd_account(cfg: Config, db, args: argparse.Namespace, what: str) -> int:
    """#1469 : enveloppe quotidienne d'un compte de facturation, et libération
    d'une réservation gardée — humains habilités seulement, journalisé."""
    from . import accounts as accounts_mod
    try:
        actor = authorize(cfg, db)
    except BudgetError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    api = storage.of(db).budgets
    if what == "release":
        row = api.account_release(args.reservation, actor=actor)
        if row is None:
            print("no kept or open reservation #%d" % args.reservation, file=sys.stderr)
            return 1
        texte = ("Account reservation #%d released (%s, agent %s, %s, day %s), by %s."
                 % (row["id"], row["billing_account"], row["agent"], _fmt_usd(row["usd"]),
                    row["period"], actor))
        fil.record(cfg, db, sender=actor, recipients=[], text=texte,
                   meta={"action": "budget", "audit": "budget",
                         "scope": "account:%s" % row["billing_account"],
                         "reservation": row["id"], "released_usd": row["usd"]})
        print(texte)
        return 0
    account = args.account
    if not accounts_mod.BILLING_RE.match(account or ""):
        print("invalid billing account: %r" % account, file=sys.stderr)
        return 2
    if what == "set":
        value = args.per_day
        if args.per_hour is not None or value is None:
            print("usage: ameesh budget set --account ID --per-day USD [--tz ZONE]",
                  file=sys.stderr)
            return 2
        if not value > 0 or value != value or value == float("inf"):
            print("invalid per-day cap: %r" % value, file=sys.stderr)
            return 2
        if args.tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(args.tz)
            except Exception:  # noqa: BLE001 - zone inconnue
                print("unknown time zone: %s" % args.tz, file=sys.stderr)
                return 2
    else:
        value = None
    changed, old = api.account_put(account, value, actor=actor,
                                   tz=getattr(args, "tz", None))
    if changed:
        texte = "Budget of billing account %s per calendar day: %s → %s, by %s." % (
            account, _fmt_usd(old), _fmt_usd(value), actor)
        fil.record(cfg, db, sender=actor, recipients=[], text=texte,
                   meta={"action": "budget", "audit": "budget", "scope": "account:%s" % account,
                         "window_s": 0, "old_usd": old, "new_usd": value})
        print("%s (effective immediately on all runners)" % texte)
    else:
        print("budget of billing account %s: unchanged (%s)" % (account, _fmt_usd(old)))
    return 0


#: #4166 : un compte fournisseur logique (harnais, nom de compte) est un nom,
#: pas un identifiant de facturation : même forme que les autres slugs.
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _cmd_prepaid(cfg: Config, db, args: argparse.Namespace) -> int:
    """#4166 : prépayé déclaré d'un compte fournisseur sans API de solde.

    Autorité humaine (comme un plafond de budget) : une session d'agent est
    refusée. La déclaration ne change jamais l'état `exhausted` (seule la
    sonde le rouvre, #4164) ; l'évaluation qui suit pose `low`/`ok` depuis le
    solde projeté, et le fil garde une trace lisible."""
    from . import provider_credits
    sub = getattr(args, "prepaid_command", None) or "show"
    if sub == "show":
        rows = provider_credits.listing(db)
        if getattr(args, "json", False):
            print(json.dumps({"schema": "ameesh-provider-credits/1",
                              "provider_credits": rows},
                             ensure_ascii=False, indent=2, default=str))
        elif not rows:
            print("provider accounts: no credit state recorded")
        else:
            for row in rows:
                print("  %-24s %s" % (provider_credits.who(
                    row["harness"], row.get("account")),
                    provider_credits.describe(row)))
        return 0
    harness = (getattr(args, "harness", "") or "").strip().lower()
    account = (getattr(args, "account", "") or "").strip()
    if not _PROVIDER_RE.match(harness):
        print("invalid harness: %r" % harness, file=sys.stderr)
        return 2
    if account and not _PROVIDER_RE.match(account):
        print("invalid account: %r" % account, file=sys.stderr)
        return 2
    try:
        actor = authorize(cfg, db)
    except BudgetError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if sub == "set":
        value = args.usd
        if not value > 0 or value != value or value == float("inf"):
            print("invalid prepaid amount: %r (strictly positive USD expected)"
                  % value, file=sys.stderr)
            return 2
        _changed, old = provider_credits.declare(db, harness, account, value, actor=actor)
        texte = ("Provider account %s prepaid amount declared: %s → %.2f USD, by %s "
                 "(the balance is projected from the settled spend from now on)."
                 % (provider_credits.who(harness, account), _fmt_usd(old), value, actor))
        fil.record(cfg, db, sender=actor, recipients=[], text=texte,
                   meta={"action": "provider_prepaid", "audit": "budget",
                         "harness": harness, "account": account,
                         "old_usd": old, "new_usd": value})
        row = provider_credits.assess_one(db, harness, account)
        print("%s\n%s" % (texte, provider_credits.describe(row)))
        return 0
    # unset
    changed, old = provider_credits.clear(db, harness, account, actor=actor)
    if not changed:
        print("no prepaid amount declared for %s"
              % provider_credits.who(harness, account))
        return 0
    texte = ("Provider account %s prepaid amount removed (%s), by %s: the balance "
             "is no longer projected." % (
                 provider_credits.who(harness, account), _fmt_usd(old), actor))
    fil.record(cfg, db, sender=actor, recipients=[], text=texte,
               meta={"action": "provider_prepaid", "audit": "budget",
                     "harness": harness, "account": account,
                     "old_usd": old, "new_usd": None})
    row = provider_credits.assess_one(db, harness, account)
    print("%s\n%s" % (texte, provider_credits.describe(row)))
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import mesh_cli
    return mesh_cli.main(["budget"] + list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
