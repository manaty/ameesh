# SPDX-License-Identifier: AGPL-3.0-only
"""Alertes de sous-utilisation (lot L94) : le gaspillage alerte autant que la
surcharge.

Demande du propriétaire (2026-10-10) : « la surconso comme la sous-conso
devrait alerter ameesh ». Le même jour, les forfaits dormaient (Claude à
12 % sur 7 jours, Codex primaire à 20 % pour un rythme permis de 52 %),
quatre agents au forfait au repos sans lot (l'orchestrateur, tenu par une
session `attach`, ne répartissait plus), pendant que des agents payés au
token travaillaient et que la VM du mesh tournait presque à vide.

Quatre types d'alerte, levés par `ameesh alerts` et poussés par
`ameesh notify` (0030 §4), mêmes règles de dédoublonnage et de résolution :

* `plan_underused` : un compte au forfait dont la capacité inutilisée sera
  perdue à la prochaine remise à zéro au rythme mesuré — fin de fenêtre
  proche et peu utilisée (`fin_de_fenetre`), ou perte projetée dépassant
  le seuil réglé (`sous_rythme`). Volume perdu estimé : `1 − utilisé` de la
  fenêtre si rien ne change (même définition que les « pertes à la remise à
  zéro » de L74, `Gauge.expiring`), et la projection au rythme actuel ; le
  débit des six dernières heures ignore les relevés pris pendant que les
  non-critiques étaient tenus par le plafond de pause (#2816) ;
* `idle_capacity` : des agents réveillables (mode `execute`, exécuteur
  vivant, au repos, sous leur budget, sans lot actionnable ni courrier)
  alors que du travail attend (lots ouverts sans assigné, courrier en
  souffrance chez un agent occupé) ou que des agents payés au token
  travaillent pendant que des agents au forfait dorment (`forfait_au_repos`,
  le cas aggravant). #1677 : le repos se lit sur les lots actionnables
  (`actionable_lots`), pas sur le temps depuis le dernier tour — le seuil ne
  sert plus que de repli sans ce relevé. Le message propose l'action :
  répartir, ou réveiller l'orchestrateur ;
* `orchestrator_held` : un orchestrateur tenu par une session interactive
  (`ameesh attach`) depuis plus du seuil, avec du courrier non lu — il ne
  répartit plus (prolonge 0030) ;
* `host_underused` : un hôte presque à vide (charge par CPU et tours en
  cours sous les seuils) sur toute la fenêtre, alors qu'un autre hôte du mesh
  est sous pression ou que des agents y attendent. **Suggestion** de
  déplacement (0028), jamais un déplacement automatique.

Lecture seule ; tout passe par `storage.of(db)`. Un seuil nul (ou négatif)
désactive l'alerte correspondante.
"""
from __future__ import annotations

import os
import time

from . import work_lifecycle as lifecycle
from . import accounts as accounts_mod
from . import cost as cost_mod
from . import db as db_mod
from . import planned_waits
from . import storage

UNDERUSE_TYPES = ("host_idle_reserved", "baseline_oversized", "plan_underused", "idle_capacity", "orchestrator_held", "host_underused",
                  "backlog_empty", "roadmap_exhausted")

#: `balance_low` (L94, ajout validé par le propriétaire) : solde bas d'un
#: fournisseur payé au token — autonomie au rythme RÉEL sous 48 h, ou solde
#: sous 20 USD ; urgente sous 12 h ou sous 5 USD. Le rythme réel vient des
#: relevés de solde (`provider_balances`), moyenne glissante sur 6 h, les
#: hausses (recharges) ignorées — jamais de l'estimation `turn_costs`.
DEFAULT_BALANCE_HOURS = 48.0
DEFAULT_BALANCE_MIN_USD = 20.0
DEFAULT_BALANCE_WINDOW_S = 6 * 3600.0
BALANCE_URGENT_HOURS = 12.0
BALANCE_URGENT_USD = 5.0
#: en deçà de cette durée couverte par les relevés, pas de rythme
BALANCE_MIN_SPAN_S = 1800.0

#: seuils par défaut
#: `plan_underused/fin_de_fenetre` : il reste moins de 24 h (au plus le quart
#: de la fenêtre : 1 h 15 pour une fenêtre de 5 h) et moins de 50 % projetés
DEFAULT_PLAN_TAIL_S = 24 * 3600.0
DEFAULT_PLAN_USED_PCT = 50.0
#: `plan_underused/sous_rythme` : perte projetée à la remise à zéro
#: supérieure à 25 points de capacité (option historique --plan-pace-gap)
DEFAULT_PLAN_PACE_GAP_PCT = 25.0
#: part maximale de la fenêtre prise par `fin_de_fenetre`
PLAN_TAIL_MAX_FRACTION = 0.25
#: `sous_rythme` : fenêtres d'au moins un jour, dont au moins le quart est
#: écoulé (en début de fenêtre, l'écart ne dit encore rien)
PLAN_PACE_MIN_WINDOW_S = 86400.0
PLAN_PACE_MIN_ELAPSED = 0.25
#: âge maximal des relevés de jauge lus (une fenêtre de 7 jours + marge)
PLAN_GAUGES_SINCE_S = 8 * 86400.0
#: rythme récent : six heures, avec au moins trente minutes entre relevés
PLAN_RATE_WINDOW_S = 6 * 3600.0
PLAN_RATE_MIN_SPAN_S = 1800.0
#: `idle_capacity` : seuil (repli quand le relevé ne porte pas
#: `actionable_lots` ; #1677)
DEFAULT_IDLE_CAPACITY_S = 1800.0
#: `orchestrator_held` : tenu par `attach` depuis 30 min
DEFAULT_ORCHESTRATOR_HELD_S = 1800.0
#: orchestrateur « qui a assigné des lots » : dans les 30 derniers jours
ASSIGNERS_SINCE_S = 30 * 86400.0
#: `host_underused` : sur 1 h, charge 1 min par CPU toujours sous 0,25 et
#: moins d'un tour en cours en moyenne
DEFAULT_HOST_UNDERUSED_S = 3600.0
DEFAULT_HOST_UNDERUSED_LOAD = 0.25
DEFAULT_HOST_UNDERUSED_TURNS = 1.0
#: un hôte sans relevé depuis 10 min n'est pas « à vide » : il est éteint
HOST_FRESH_S = 600.0
#: états de lot qui attendent un preneur (`idle_capacity`)
WAITING_LOT_STATES = lifecycle.ACTIVE
#: L165b : un lot dans ces états attend un autre que son assigné (relecture,
#: humain, autre lot) ; l'agent qui n'a que ceux-là est libre pour un lot
#: d'équipe et compte dans `idle_capacity`
LOTS_WAITING_ON_OTHERS = lifecycle.WAITING_ON_OTHERS
#: mots qui désignent un orchestrateur dans `roles`/`role` d'une fiche Agent
ORCHESTRATOR_ROLES = ("orchestrateur", "orchestrator")
#: L165b : rôles de l'agent de conception, prévenu aussi d'`idle_capacity`
DESIGN_ROLES = ("conception", "design")
#: L242c : rôles qui rendent un agent critique (jamais en pause budget tant
#: qu'un compte a de la marge), par défaut ; une organisation les règle par
#: `critical_roles` dans son federation.yaml (manaty/home#25)
CRITICAL_ROLES = ("orchestrateur", "conception", "auditeur", "relecture")
#: L242c : les autres noms reconnus d'un rôle dans `roles`/`role` d'une fiche
ROLE_SYNONYMS = {"orchestrateur": ("orchestrator",), "conception": ("design",),
                 "auditeur": ("auditor",), "relecture": ("review", "reviewer")}
_ROLE_OF = {alias: role for role, aliases in ROLE_SYNONYMS.items() for alias in aliases}
#: L242c (#424 r2) : constats de chargement qui laissent un canon lu en partie
#: (fiche ou manifeste illisible) — ses rôles ne sont alors pas connus
READ_ERRORS = ("canon-git-error", "frontmatter-invalid", "federation-invalid")

PLAN_REASONS = ("fin_de_fenetre", "sous_rythme")
IDLE_REASONS = ("forfait_au_repos", "travail_en_attente")
HOST_REASONS = ("pression_ailleurs", "agents_en_attente")


def _duration(seconds) -> str:
    """« 52 min », « 3 h 10 », « 2 j 4 h » (même forme que L74)."""
    minutes = max(0, int(round(float(seconds) / 60.0)))
    if minutes < 60:
        return "%d min" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%d h %02d" % (hours, minutes) if minutes else "%d h" % hours
    days, hours = divmod(hours, 24)
    return "%d d %d h" % (days, hours) if hours else "%d d" % days


def _alert(kind, agent, since, value, threshold, detail, **extra) -> dict:
    from .exploitation import _alert as make
    return make(kind, agent, since, value, threshold, detail, **extra)


def _state(row: dict) -> str:
    from . import progress
    return progress.agent_state(row)


def _attached(row: dict) -> bool:
    return bool(row.get("lease_live")) and str(row.get("lease_owner") or "").startswith(
        "attach:")


def _awake_runner(row: dict) -> bool:
    """Exécuteur vivant (bail d'un runner, pas d'une session attachée)."""
    return bool(row.get("lease_live")) and bool(row.get("lease_owner")) \
        and not _attached(row)


def paid_harnesses(host: str | None = None) -> tuple | None:
    """Harnais payés au token (descripteurs), ou None s'ils sont illisibles :
    on ne devine pas, le cas aggravant n'est alors pas évalué."""
    try:
        return cost_mod.paid_harnesses_of(host)
    except Exception:
        return None


def _paid_spend(db, paid, seconds: float = 86400.0, *, cfg=None) -> tuple | None:
    """`(paid, estimated)` per token over `seconds` (#325): `paid`, the fall of
    the provider balances in USD (`balance.compare`, L95) for the paid harnesses
    that have readings, else None; `estimated`, ameesh's ledger for the others."""
    if not paid:
        return None
    from . import balance
    now = time.time()
    try:
        real = [entry for entry in balance.compare(db, paid, now - seconds, now, cfg=cfg)
                if entry["provider"] in paid and entry["currency"] == "USD"]
    except Exception:
        real = []                       # balances unreadable: the estimate for all
    try:
        rest = [h for h in paid if h not in {entry["provider"] for entry in real}]
        estimated = (float(storage.of(db).turn_costs.spent(seconds, agent="all",
                                                           harnesses=rest))
                     if rest else 0.0)
    except Exception:
        return None
    return (sum(entry["real_spent"] for entry in real) if real else None), estimated


def _common_responsible(rows) -> str | None:
    counts: dict = {}
    for row in rows:
        who = (row or {}).get("responsible")
        if who:
            counts[who] = counts.get(who, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


# --------------------------------------------------------------------------
# plan_underused
# --------------------------------------------------------------------------

def _gauge(row: dict) -> cost_mod.Gauge:
    """La jauge d'une ligne d'historique, portée par modèle restituée.

    #1789/N6 : la clé d'un seau propre à un modèle porte sa portée
    (`seven_day@fable`, #1580) — l'historique ne stocke pas le `scope` ; le
    relire ici évite qu'un seau de modèle soit pris pour la fenêtre générale
    du compte (alertes de sous-utilisation, `plan_underused`)."""
    key = row["key"]
    _, _, scope = key.partition("@")
    return cost_mod.Gauge(row["harness"], key, float(row.get("used") or 0.0),
                          row.get("resets_at_ts"), float(row.get("window_s") or 0.0)
                          or float(cost_mod.KNOWN_WINDOWS["seven_day"]), scope=scope)


def expiring(gauge, now: float) -> tuple | None:
    """(échéance, capacité inutilisée) de la fenêtre en cours, ou None — la
    définition de L74 (`Gauge.expiring`), reprise telle quelle tant que L74
    n'est pas fusionné : None si la fenêtre n'est pas datée ou déjà échue (la
    suivante ne court qu'au premier usage, rien n'expire)."""
    method = getattr(gauge, "expiring", None)
    if callable(method):
        return method(now)
    if not gauge.resets_at or gauge.reset_passed(now):
        return None
    return float(gauge.resets_at), max(0.0, 1.0 - float(gauge.used or 0.0))


def _gauge_history(db, *, since_s: float) -> list:
    """Historique des jauges pour la projection au rythme récent (#2759).

    Le try/except seul ne suffisait pas : une erreur SQL dans une transaction
    ouverte l'avorte (psycopg la garde avortée jusqu'au ROLLBACK, le pilote
    psql ferme sa session), et les lectures de dépense au token qui suivent
    échouaient alors aussi. Ici, dans une transaction appelante, l'historique
    est lu sur une connexion INDÉPENDANTE : son échec reste local et ne touche
    pas la transaction. Hors transaction (le cas nominal d'`ameesh alerts`),
    la lecture reste sur la connexion reçue. Un historique illisible rend `[]`,
    le repli documenté sur la moyenne de fenêtre."""
    def read(source):
        return storage.of(source).operations.gauge_history(
            since_s=since_s, harness=None)

    if not db_mod.in_transaction(db):
        try:
            return read(db)
        except Exception:
            return []
    try:
        own = db_mod.connect(db.cfg)
    except Exception:
        return []
    try:
        return read(own)
    except Exception:
        return []
    finally:
        try:
            own.close()
        except Exception:
            pass


def latest_account_gauges(db, now: float) -> dict:
    """{(harnais, compte) : [Gauge]} : le dernier relevé de chaque jauge.

    Les relevés sans compte (`account` nul : `ameesh cost gauges`, hôte sans
    comptes déclarés) d'un harnais qui a aussi des relevés de compte sont
    écartés : ce sont ceux du compte par défaut, déjà comptés."""
    rows = storage.of(db).operations.latest_gauges(since_s=PLAN_GAUGES_SINCE_S)
    named = {row["harness"] for row in rows if row.get("account")}
    out: dict = {}
    for row in rows:
        account = row.get("account") or None
        if account is None and row["harness"] in named:
            continue
        out.setdefault((row["harness"], account), []).append(
            (_gauge(row), row.get("observed_ts")))
    return out


def _projected_plan_loss(gauge, account, now: float, history: list, *,
                         multiplier: float = 1.0,
                         ceiling: float = cost_mod.PACE_CEILING,
                         reserve: float = accounts_mod.RESERVE) -> tuple:
    """(perte, source, durée) : débit des six dernières heures de production.

    Même harnais, compte, jauge et échéance ; les échos d'un relevé ne
    créent pas du temps mesuré. Sans deux relevés couvrant 30 min, ou si
    l'usage recule, repli explicite sur la moyenne depuis le début de fenêtre.
    Le rythme autorisé et son multiplicateur ne prédisent pas le débit réel.
    #2816 : un relevé pris alors que les non-critiques étaient tenus (usage
    au plafond de pause, réserve déduite) mesure ce plafond, pas la demande ;
    il est écarté, et l'absence de relevés libres fait retomber sur la
    moyenne de fenêtre.
    """
    points = {}
    for row in history:
        if (row["harness"], row.get("account") or None, row["key"]) != (
                gauge.harness, account, gauge.key):
            continue
        stamp = row.get("produced_ts") or row.get("observed_ts")
        reset = row.get("resets_at_ts")
        if stamp is None or reset is None:
            continue
        stamp = float(stamp)
        if not now - PLAN_RATE_WINDOW_S <= stamp <= now:
            continue
        if abs(float(reset) - float(gauge.resets_at)) > 1.0:
            continue
        used_at_stamp = float(row.get("used") or 0.0)
        if used_at_stamp >= gauge.held_cap(stamp, reserve, multiplier, ceiling):
            continue
        points[stamp] = used_at_stamp
    samples = sorted(points.items())
    if len(samples) >= 2:
        start, initial = samples[0]
        end, used = samples[-1]
        span = end - start
        if (span >= PLAN_RATE_MIN_SPAN_S and used == float(gauge.used)
                and all(b[1] >= a[1] for a, b in zip(samples, samples[1:]))):
            rate = (used - initial) / span
            projected = used + rate * (float(gauge.resets_at) - end)
            return max(0.0, 1.0 - projected), "recent", span
    elapsed = gauge.elapsed(now)
    projected = float(gauge.used) / elapsed if elapsed > 0 else float(gauge.used)
    return max(0.0, 1.0 - projected), "window_average", elapsed * gauge.window_s


def _declared_profiles(cfg) -> dict:
    """{harnais : [Profile]} de la configuration, sans lever (#1227 N3)."""
    if cfg is None:
        return {}
    try:
        # #3647 : au rythme compilé du canon, comme le choix
        return accounts_mod.with_canon_pace(
            cfg, accounts_mod.parse(getattr(cfg, "accounts", None) or {}))
    except accounts_mod.AccountError:
        return {}


def _declared_profile(items: list, account: str | None) -> accounts_mod.Profile | None:
    """Le profil du relevé (#1227, #2233), None si inconnu ou ambigu."""
    if items:
        if account:
            return accounts_mod.by_name(items, account)
        elif len(items) == 1:
            return items[0]
    return None


def plan_underused(db, listing: list, now: float, *,
                   tail_s: float = DEFAULT_PLAN_TAIL_S,
                   used_pct: float = DEFAULT_PLAN_USED_PCT,
                   pace_gap_pct: float = DEFAULT_PLAN_PACE_GAP_PCT,
                   paid=None, held=(), cfg=None) -> list:
    """`cfg` (#1227 N3) : la configuration des comptes déclarés — le rythme et
    le plafond du compte nommé par le relevé remplacent les 90 % et le ×1 en
    dur ; un compte absent, inconnu ou ambigu garde les défauts. #2233 : les
    plafonds fixes et courts de `accounts.limits` bloquent aussi une fausse
    promesse de capacité sur une autre fenêtre du même compte. #2648 :
    la réserve des critiques est déduite du seuil de pause et du pace_cap
    annoncé pour les non-critiques. #2757 :
    le déclenchement dépend de la perte projetée au débit récent, avec repli
    explicite sur la moyenne de fenêtre si les relevés sont insuffisants ;
    le plafond autorisé reste une limite de capacité, pas une prévision.
    #2816 : les relevés du débit pris alors que les non-critiques étaient
    tenus par ce plafond mesurent la pause, pas la demande ; ils sont écartés,
    ce qui fait retomber sur la moyenne de fenêtre quand il n'en reste pas
    assez."""
    used_max = max(0.0, float(used_pct)) / 100.0
    gap_min = max(0.0, float(pace_gap_pct)) / 100.0
    if (tail_s <= 0 or used_max <= 0) and gap_min <= 0:
        return []
    out: list = []
    spend = None
    spend_read = False
    book = None
    declared = _declared_profiles(cfg)
    history = None
    for (harness, account), gauges in sorted(latest_account_gauges(db, now).items(),
                                              key=lambda kv: (kv[0][0], kv[0][1] or "")):
        items = declared.get(harness) or []
        profile = _declared_profile(items, account)
        multiplier = profile.pace_multiplier if profile else 1.0
        ceiling = profile.pace_ceiling if profile else cost_mod.PACE_CEILING
        # une autre fenêtre du compte au seuil : sa capacité n'est pas
        # utilisable d'ici là, rien n'est « perdu » par négligence
        live = [(g, seen) for g, seen in gauges if expiring(g, now) is not None]
        # #1789/N6 : un seau propre à un modèle (#1580 tranche 3) ne rend pas
        # la capacité générale du compte inutilisable — seules les fenêtres
        # générales se taisent entre elles ; un seau de modèle reste alerte.
        blocking = [(g, seen) for g, seen in gauges
                    if not g.scope and not g.reset_passed(now)]
        caps = {}
        if profile is not None:
            measured = cost_mod.Gauges(g for g, _ in gauges)
            # #2233 : l'historique ne porte pas la capacité d'overage. Ne
            # lire le journal du compte que si son plafond pourrait bloquer.
            if profile.overage == "auto" and any(
                    g.used_at(now) >= profile.overage_cap for g, _ in blocking):
                try:
                    if book is None:
                        book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db)
                    current = accounts_mod.gauges_of(book, profile, items, now=now)
                    measured.overage = getattr(current, "overage", None)
                except Exception:  # compte illisible : aucune capacité promise
                    continue
            # Comme avant #2233, aucun relèvement de fin de fenêtre sans
            # preuve fraîche ; les plafonds fixes et courts viennent de limits.
            # #2648 : même éligible que la pause d'un agent non critique — la
            # réserve des critiques (10 points) comprise. Sans elle, une
            # fenêtre dans cette bande (ex. seven_day à 83 % pour un rythme
            # ramené à 80 %) laissait annoncer une capacité qu'aucun agent ne
            # pouvait consommer : la pause jugeait le compte au seuil.
            caps = accounts_mod.limits(profile, measured, now, reserve=True,
                                       ceilings={
                                           g.key: accounts_mod.Ceiling(ceiling)
                                           for g in measured})
            if any(g.used_at(now) >= caps[g.key][0] for g, _ in blocking):
                continue
        for gauge, seen in live:
            if caps and gauge.used_at(now) >= caps[gauge.key][0]:
                continue
            if profile is None and any(other.exceeded(now, 0.0, multiplier, ceiling)
                                       for other, _ in blocking if other is not gauge):
                continue
            resets_at, unused = expiring(gauge, now)
            remaining = resets_at - now
            elapsed = gauge.elapsed(now)
            # #2648 : le rythme annoncé est celui de la pause d'un
            # non-critique (réserve déduite), pas le plafond ordinaire.
            cap = gauge.held_cap(now, accounts_mod.RESERVE, multiplier, ceiling)
            used = float(gauge.used or 0.0)
            if history is None:
                history = _gauge_history(db, since_s=PLAN_RATE_WINDOW_S)
            lost_at_pace, rate_source, rate_span = _projected_plan_loss(
                gauge, account, now, history, multiplier=multiplier,
                ceiling=ceiling, reserve=accounts_mod.RESERVE)
            if gap_min > 0 and lost_at_pace <= gap_min:
                continue
            reason = None
            tail = min(float(tail_s), PLAN_TAIL_MAX_FRACTION * gauge.window_s)
            if tail_s > 0 and used_max > 0 and remaining <= tail \
                    and lost_at_pace > max(0.0, 1.0 - used_max):
                reason = "fin_de_fenetre"
            elif gap_min > 0 and gauge.window_s >= PLAN_PACE_MIN_WINDOW_S \
                    and elapsed >= PLAN_PACE_MIN_ELAPSED and lost_at_pace > gap_min:
                reason = "sous_rythme"
            if reason is None:
                continue
            # #254 : les agents de ce harnais tous arrêtés par une pause
            # d'hôte voulue — rien n'est perdu par négligence
            agents = [row["name"] for row in listing if row.get("harness") == harness
                      and (row.get("mode") or "execute") == "execute"]
            if held and agents and all(name in held for name in agents):
                continue
            who = "%s/%s" % (harness, account) if account else harness
            if reason == "fin_de_fenetre":
                head = ("account %s, window %s: %.0f %% used, resets in %s"
                        % (who, gauge.key, used * 100, _duration(remaining)))
            else:
                head = ("account %s, window %s: %.0f %% used, projected loss %.0f %% "
                        "exceeds %.0f %%, resets in %s"
                        % (who, gauge.key, used * 100, lost_at_pace * 100, gap_min * 100,
                           _duration(remaining)))
            detail = ("%s; estimated loss at reset: %.0f %% of the window if nothing "
                      "changes (%.0f %% at the current pace)"
                      % (head, unused * 100, lost_at_pace * 100))
            detail += "; rate: %s over %s" % (rate_source, _duration(rate_span))
            idle = sorted(row["name"] for row in listing
                          if row.get("harness") == harness
                          and (row.get("mode") or "execute") == "execute"
                          and _state(row) == "idle" and _awake_runner(row)
                          and row["name"] not in held)
            if idle:
                detail += "; idle %s agents: %s" % (harness, ", ".join(idle))
            if paid and harness not in paid:
                if not spend_read:
                    spend, spend_read = _paid_spend(db, paid, cfg=cfg), True
                real, estimated = spend or (None, 0.0)
                parts = []
                if real:
                    parts.append("$%.2f paid per token over 24 h (provider balances)" % real)
                if estimated:
                    parts.append("about $%.2f estimated per token over 24 h (no usable "
                                 "balance reading)" % estimated)
                if parts:
                    detail += "; meanwhile, " + ", plus ".join(parts)
            responsible = _common_responsible(
                [row for row in listing if row.get("harness") == harness])
            out.append(_alert(
                "plan_underused", None, resets_at - gauge.window_s,
                int(round(unused * 100)),
                int(round(used_max * 100 if reason == "fin_de_fenetre" else gap_min * 100)),
                detail, harness=harness, account=account, gauge=gauge.key, reason=reason,
                used=round(used, 4), pace_cap=round(cap, 4), resets_at=round(resets_at, 3),
                lost=round(unused, 4), lost_at_pace=round(lost_at_pace, 4),
                rate_source=rate_source, rate_span_s=round(rate_span, 3),
                observed_ts=round(float(seen), 3) if seen is not None else None,
                idle_agents=idle, responsible=responsible))
    return out


# --------------------------------------------------------------------------
# orchestrateurs
# --------------------------------------------------------------------------

def _bare(name: str) -> str:
    name = str(name or "").strip()
    return name[len("agent:"):] if name.startswith("agent:") else name


def role_orchestrators(canons, roles=None) -> set:
    """Les agents dont la fiche Agent du canon nomme un rôle d'orchestrateur
    (`roles` ou `role` : `orchestrateur`, `orchestrator`), ou l'un des
    `roles` donnés (synonymes dans les deux sens, L242c)."""
    wanted = {_canonical(r) for r in (roles or ORCHESTRATOR_ROLES)}
    found = set()
    for canon in canons or []:
        for agent in getattr(canon, "agents", None) or []:
            if wanted & set(_card_roles(agent)):
                found.add(agent.title)
    return found


def _canonical(role) -> str:
    """Le nom d'un rôle, en minuscules, synonyme ramené au rôle (L242c,
    #424 r1 : `review` et `relecture` se reconnaissent dans les deux sens)."""
    role = str(role).strip().lower()
    return _ROLE_OF.get(role, role)


def _card_roles(agent) -> list:
    """Les rôles d'une fiche Agent tels que le canon les a validés (`roles`
    analysé : une valeur invalide n'en donne aucun, #424 r1), sinon `role:`
    s'il est un texte ; ramenés à leur nom (`_canonical`). Une fiche sans
    analyse (objet factice) : le frontmatter, liste de textes ou texte."""
    data = getattr(getattr(agent, "fiche", None), "data", None) or {}
    if hasattr(agent, "roles"):
        named = agent.roles
    else:
        named = data.get("roles")
        if isinstance(named, str):
            named = [named]
        elif not (isinstance(named, list) and all(isinstance(r, str) for r in named)):
            named = None
    if named is None:
        single = data.get("role")
        named = [single] if isinstance(single, str) else []
    return [role for role in (_canonical(r) for r in named) if role]


def roles_declared(canons) -> bool:
    """L242c : une fiche Agent d'un des canons déclare-t-elle des rôles ?
    Alors seuls les rôles désignent ses orchestrateurs (plus de déduction par
    les lots confiés)."""
    return any(_card_roles(agent)
               for canon in canons or [] for agent in getattr(canon, "agents", None) or [])


def _owners(row, canons) -> list:
    """Les canons propriétaires de `row` (un canon sans `owns` : tous)."""
    return [c for c in canons or []
            if getattr(c, "owns", None) is None or c.owns((row or {}).get("canon"))]


def _card(canon, name):
    return next((a for a in getattr(canon, "agents", None) or [] if a.title == name), None)


def deduces(row, canons) -> bool:
    """L242c (#424 r1) : la déduction par les lots confiés vaut pour un agent
    dont l'organisation (son canon propriétaire) ne déclare aucun rôle."""
    return not roles_declared(_owners(row, canons))


def card_role(row, canons, roles) -> bool:
    """La fiche de l'agent `row`, dans son canon propriétaire seulement, nomme-
    t-elle l'un des `roles` ? Une fiche homonyme d'un autre canon ne compte
    jamais (#324 r1, #424 r1)."""
    wanted = {_canonical(r) for r in roles}
    for canon in _owners(row, canons):
        if not getattr(canon, "readable", True):
            continue
        definition = _card(canon, row["name"])
        if definition is not None and wanted & set(_card_roles(definition)):
            return True
    return False


def critical_roles(canon) -> tuple:
    """L242c : les rôles critiques d'une organisation (`critical_roles` de son
    federation.yaml, liste ; vide : aucun), sinon `CRITICAL_ROLES` ; chacun
    ramené à son nom (`_canonical`)."""
    declared = (getattr(canon, "federation", None) or {}).get("critical_roles")
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, (list, tuple)):
        declared = CRITICAL_ROLES
    return tuple(dict.fromkeys(role for role in (_canonical(r) for r in declared
                                                 if isinstance(r, str)) if role))


def fully_read(canon) -> bool:
    """L242c (#424 r2) : le canon s'est-il lu en entier ? Pas s'il est
    illisible, ni si une fiche ou son manifeste n'a pu être lu (`READ_ERRORS`
    de niveau erreur dans `load_findings`) : une fiche manquante ne doit pas
    faire perdre un rôle en silence."""
    if not getattr(canon, "readable", True):
        return False
    return not any(getattr(f, "code", None) in READ_ERRORS
                   and getattr(f, "severity", None) == "error"
                   for f in getattr(canon, "load_findings", None) or ())


#: L242c (#424 r3) : verdict perdu (cache absent, illisible, corrompu, valeur
#: invalide, fichier ou dossier dont on ne peut se fier) — « inconnu » vaut
#: critique
UNKNOWN = "unknown"
#: L242c (#424 r2) : les derniers verdicts lus, dans l'état de l'hôte —
#: relus par un exécuteur redémarré et par `ameesh resume` (#424 r4 : dans
#: leur dossier, `CRITICAL_DIR`)
#: #424 r4 : le dossier du cache (0700, à nous ; verrouillé lui-même) et ses
#: données (renommées seules)
CRITICAL_DIR, CRITICAL_DATA = "critical-known", "known.json"


class _Untrusted(OSError):
    """#424 r4 : le dossier du cache n'est pas digne de confiance."""


def _memory_path(cfg) -> str | None:
    state = getattr(cfg, "state_dir", None)
    return os.path.join(state, CRITICAL_DIR) if state else None


def _memory_dir(cfg, *, create: bool) -> int | None:
    """#424 r4 : le dossier du cache, ouvert sans suivre de lien
    (`O_DIRECTORY|O_NOFOLLOW`, relatif au dossier d'état), à nous et en 0700 —
    tout le reste s'ouvre relativement à lui (`dir_fd`) : un parent remplacé ou
    lié ne redirige rien. None s'il n'existe pas (et `create` faux) ;
    `_Untrusted` s'il n'est pas digne de confiance.

    Résiduel accepté (#424 r5, mesh-design) : le dossier d'état lui-même
    s'ouvre en suivant les liens, sans contrôle de propriétaire. Le détourner
    suppose une action malveillante sous le même UID dans le dossier d'état
    de l'exécuteur, hors de notre modèle de menace."""
    import stat
    state = os.open(cfg.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        if create:
            try:
                os.mkdir(CRITICAL_DIR, 0o700, dir_fd=state)
            except FileExistsError:
                pass
        try:
            fd = os.open(CRITICAL_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                         | os.O_CLOEXEC, dir_fd=state)
        except FileNotFoundError:
            return None
        except OSError as exc:      # ELOOP / ENOTDIR : un lien, un fichier
            raise _Untrusted("critical cache dir: %s" % exc) from exc
    finally:
        os.close(state)
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        os.close(fd)
        raise _Untrusted("critical cache dir: not ours, or not 0700")
    return fd


def _read_memory(folder: int):
    """#424 r4 : `(état, données)` du cache dans le dossier `folder` : `missing`,
    `ok` (le dict tel qu'écrit, valeurs invalides comprises) ou `corrupt`
    (illisible, pas du JSON objet, lien, pas un fichier ordinaire, pas à nous,
    lisible par d'autres)."""
    import json
    import stat
    try:
        fd = os.open(CRITICAL_DATA, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=folder)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "corrupt", None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() \
                or st.st_mode & 0o077:
            return "corrupt", None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return "corrupt", None
    finally:
        os.close(fd)
    return ("ok", data) if isinstance(data, dict) else ("corrupt", None)


def critical_memory(cfg) -> dict | None:
    """Les derniers verdicts lus : {agent: True|False|None}, None pour une
    valeur invalide. {} sans dossier d'état configuré (la mémoire du processus
    seule). None (fail-closed, tout y est inconnu) : cache absent (#424 r4 :
    effacé ou masqué, jamais « rien n'a été lu »), illisible, corrompu, ou
    dont on ne peut se fier (`_memory_dir`, `_read_memory`)."""
    if _memory_path(cfg) is None:
        return {}
    try:
        folder = _memory_dir(cfg, create=False)
    except OSError:
        return None
    if folder is None:
        return None
    try:
        state, data = _read_memory(folder)
    finally:
        os.close(folder)
    if state != "ok":
        return None
    return {str(k): v if isinstance(v, bool) else None for k, v in data.items()}


def remembered(memory: dict | None, name: str):
    """Le dernier verdict lu de `name` : True, False, ou `UNKNOWN` (vaut
    critique, `critical_verdict`) — cache perdu, valeur invalide, ou nom
    absent du cache (#424 r6 : jamais lu, ou cache recréé depuis par un
    agent d'un autre canon ; aucun « non » faute de lecture)."""
    if memory is None or name not in memory:
        return UNKNOWN
    value = memory[name]
    return value if isinstance(value, bool) else UNKNOWN


def remember_critical(cfg, name: str, verdict: bool) -> bool:
    """Retient le verdict lu de `name` ; vrai quand le disque le porte (déjà,
    ou une fois écrit) — comparé au disque, jamais à la mémoire du processus
    (#424 r4). Sous un verrou (`flock` sur le dossier lui-même, ouvert une
    fois : aucun chemin de verrou à remplacer) : relecture, mise à jour, fichier temporaire exclusif, fsync, renommage des
    seules données, fsync du dossier, mode 0600. Un cache corrompu n'est
    jamais réécrit (il reste « inconnu », critique) ; ses valeurs invalides
    sont gardées telles quelles. Ne lève jamais : l'appelant réessaie.

    Résiduel accepté (#424 r5, mesh-design) : le dossier du cache remplacé
    entre son ouverture et le `flock` : l'écriture va dans le dossier
    détaché, la lecture suivante voit le nouveau (absent ou corrompu :
    critique). Cela suppose une action malveillante sous le même UID dans le
    dossier d'état de l'exécuteur, hors de notre modèle de menace."""
    import fcntl
    import json
    if _memory_path(cfg) is None:
        return False
    try:
        folder = _memory_dir(cfg, create=True)
    except OSError:
        return False
    if folder is None:
        return False
    tmp = None
    try:
        fcntl.flock(folder, fcntl.LOCK_EX)
        state, known = _read_memory(folder)
        if state == "corrupt":
            return False
        known = dict(known or {})
        if known.get(name) is verdict:
            return True
        known[name] = verdict
        tmp = ".%s.%d.%s" % (CRITICAL_DATA, os.getpid(), os.urandom(6).hex())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600, dir_fd=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(known, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, CRITICAL_DATA, src_dir_fd=folder, dst_dir_fd=folder)
        tmp = None
        os.fsync(folder)
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp, dir_fd=folder)
            except OSError:
                pass
        os.close(folder)


def critical_by_role(row, canons) -> bool | None:
    """L242c : l'agent du registre `row` porte-t-il un rôle critique de son
    organisation, dans la fiche de son canon propriétaire ? La persona
    `auditeur` l'est sans fiche tant que le rôle auditeur est critique.
    None (#424 r1) : son canon propriétaire n'est pas chargé ou illisible —
    inconnu, jamais « non » (`critical_verdict`) ; de même s'il n'a été lu
    qu'en partie (#424 r2, `fully_read`)."""
    from .alert_briefs import AUDITOR_NAME
    owners = _owners(row, canons)
    if not owners or not all(fully_read(c) for c in owners):
        return None
    for canon in owners:
        roles = critical_roles(canon)
        if row.get("name") == AUDITOR_NAME and "auditeur" in roles:
            return True
        if card_role(row, [canon], roles):
            return True
    return False


#: #424 r7 : les agents d'un canon étranger déjà signalés par ce processus
_FOREIGN_WARNED: set = set()


def foreign_canon(row, canons, *, complete: bool) -> str | None:
    """#424 r7 (mesh-design) : le canon de `row` quand AUCUN canon de l'hôte ne
    le possède, tous les canons configurés chargés (`complete`) ET lus en
    entier (`fully_read`, #424 r8) — un état connu et permanent, pas une
    panne : l'agent garde le verdict d'avant L242c (non critique). Un canon
    configuré mais illisible ou pas encore chargé, ou dont le manifeste n'a
    pas pu être lu, n'est pas étranger : son identité peut être restée le nom
    du dossier (`canon.py`, `_identify` avant manifeste), donc rien ne prouve
    que l'agent n'est d'aucun canon — le verdict connu/cache puis la règle
    fail-safe s'appliquent (#424 r8). None sinon."""
    if (not complete or not canons
            or not all(fully_read(c) for c in canons)     # #424 r8
            or _owners(row, canons)):
        return None
    return (row or {}).get("canon") or "?"


def warn_foreign(name: str) -> bool:
    """#424 r7 : vrai la première fois seulement (un avertissement par
    processus et par agent d'un canon étranger)."""
    if name in _FOREIGN_WARNED:
        return False
    _FOREIGN_WARNED.add(name)
    return True


def critical_verdict(row, canons, *, configured: bool, known=None) -> tuple:
    """L242c : (critique ?, inconnu ?) pour l'agent `row`. Sans canon sur
    l'hôte : la persona auditeur seule (rôles par défaut). Canon propriétaire
    illisible (#424 r1) : le dernier verdict lu (`known`, `remembered`) ;
    sinon — perdu, invalide, jamais lu (#424 r6) — critique (fail-safe).
    Jamais une baisse silencieuse ; la surcharge `critical_agents` se juge
    avant."""
    from .alert_briefs import AUDITOR_NAME
    if not configured and not canons:
        return row.get("name") == AUDITOR_NAME, False
    verdict = critical_by_role(row, canons)
    if verdict is not None:
        return verdict, False
    if isinstance(known, bool):
        return known, True
    return True, True


def critical_candidates(canons) -> set:
    """Les noms qui PEUVENT être critiques par leur rôle (toute fiche, tout
    canon) : un nom hors de cet ensemble évite la lecture du registre."""
    from .alert_briefs import AUDITOR_NAME
    found = {AUDITOR_NAME}
    for canon in canons or []:
        roles = critical_roles(canon)
        if roles:
            found |= role_orchestrators([canon], roles)
    return found


def declared_orchestrators(env=None) -> set:
    """Les orchestrateurs déclarés par `AMEESH_ALERT_ORCHESTRATORS` (noms
    séparés par des virgules, `agent:` admis)."""
    value = (env if env is not None else os.environ).get("AMEESH_ALERT_ORCHESTRATORS") or ""
    return {_bare(n) for n in value.split(",") if n.strip()}


def orchestrators(cfg, db, listing: list, *, declared=(), canons=None,
                  roles=ORCHESTRATOR_ROLES, history: bool = True) -> list:
    """Les orchestrateurs connus : déclarés (`--orchestrators`,
    `AMEESH_ALERT_ORCHESTRATORS`), fiche Agent du canon dont `roles` (ou
    `role`) nomme un orchestrateur (ou l'un des `roles` donnés) dans le canon
    propriétaire de l'agent (#424 r1), ou agent qui a confié des lots
    (délégation ou `work assign`) dans les 30 derniers jours — tant que son
    organisation ne déclare aucun rôle (L242c, `deduces`) — sauf avec
    `history=False` (#740). Seulement des agents du registre.

    Modèle de rôle (autorité : correction, plan, analyse) : il inclut
    l'historique. Pour les **destinataires d'alerte**, l'historique ne confère
    pas le rôle : `alert_orchestrators` (#1114), qui exclut les assigneurs
    récents non déclarés."""
    names = {row["name"] for row in listing}
    found = {_bare(n) for n in declared or () if n}
    if canons is None:
        from . import canon as canon_mod
        try:
            canons = canon_mod.load_configured(cfg) if cfg is not None else []
        except Exception:
            canons = []
    found |= {row["name"] for row in listing if card_role(row, canons, roles)}
    if history:
        deducing = [row for row in listing if deduces(row, canons)]
        if deducing:
            try:
                assigners = {_bare(n) for n in storage.of(db).operations.assigners(
                    since_s=ASSIGNERS_SINCE_S)}
            except Exception:
                assigners = set()
            found |= {row["name"] for row in deducing if row["name"] in assigners}
    # L123 : le chat du propriétaire transmet, il ne répartit pas
    from .chat import is_chat_agent
    chats = {row["name"] for row in listing if is_chat_agent(row)}
    return sorted(n for n in found if n in names and n not in chats)


def alert_orchestrators(cfg, db, listing: list, *, declared=(), canons=None) -> list:
    """Les orchestrateurs qui peuvent recevoir une alerte : déclarés
    (`--orchestrators`, `AMEESH_ALERT_ORCHESTRATORS`) ou rôle `orchestrateur`
    d'une fiche Agent du canon. L'historique des lots confiés (30 jours) ne
    confère pas ce rôle (#1114) : un agent qui a seulement assigné récemment
    n'est pas un destinataire, et ne peut donc pas entrer dans la boucle
    « recevoir l'alerte pousse à assigner, ce qui entretient la liste ».

    Le repli « sans équipe » d'`alert_briefs._of_teams` ne s'applique plus
    qu'aux agents présents ici : un hub sans équipe (bureau) est donc admis
    seulement par déclaration explicite — rôle au canon ou
    `AMEESH_ALERT_ORCHESTRATORS` —, jamais par le seul historique."""
    return orchestrators(cfg, db, listing, declared=declared, canons=canons,
                         history=False)


def orchestrator_held(listing: list, now: float, orchestras: list, *,
                      held_s: float = DEFAULT_ORCHESTRATOR_HELD_S) -> list:
    if held_s <= 0:
        return []
    out = []
    for row in listing:
        if row["name"] not in orchestras or not _attached(row):
            continue
        since = row.get("status_since_ts")
        unread = int(row.get("unread") or 0)
        if since is None or now - float(since) < held_s or not unread:
            continue
        out.append(_alert(
            "orchestrator_held", row["name"], since, unread, held_s,
            "orchestrator held by an interactive session (%s) for %s, %d unread "
            "message(s): it no longer dispatches. Hand it back (quit the session: "
            "the runner takes the lease back), or handle the mail in the session"
            % (row.get("lease_owner"), _duration(now - float(since)), unread),
            owner=row.get("lease_owner"), responsible=row.get("responsible") or None))
    return out


# --------------------------------------------------------------------------
# idle_capacity
# --------------------------------------------------------------------------

def idle_since(row: dict) -> float:
    """Début du repos : le dernier changement de statut ou le dernier tour."""
    return max(float(row.get("status_since_ts") or 0.0), float(row.get("last_turn_ts") or 0.0))


def free_of_lots(row: dict, *, waiting_free: bool = False) -> bool:
    """L'agent n'a-t-il aucun lot qui l'occupe ? Strict : aucun lot assigné
    ouvert. `waiting_free` (L165b) : chacun de ses lots attend un autre que lui
    (`LOTS_WAITING_ON_OTHERS`) ou commence après aujourd'hui (`active_lots`
    nul)."""
    if not waiting_free or "active_lots" not in row:
        return row.get("assigned_lot_id") is None
    return not int(row.get("active_lots") or 0)


def session_lot_busy(row: dict, *, waiting_free: bool = False) -> bool:
    """Le lot de session de l'agent (assigné à lui ou à un autre) est-il
    encore ouvert et l'occupe-t-il ? `waiting_free` (L165b) : pas s'il attend
    un autre (`LOTS_WAITING_ON_OTHERS`) ou commence après aujourd'hui
    (`session_lot_later`). Partagé par la prise (`backlog._awaited`) et
    `idle_agents` (#318 r2)."""
    from . import stagnation
    state = row.get("session_lot_state")
    if not state or state in stagnation.DONE_STATES:
        return False
    return not (waiting_free and (state in LOTS_WAITING_ON_OTHERS
                                  or row.get("session_lot_later")))


def idle_agents(listing: list, now: float, idle_s: float, *,
                waiting_free: bool = False, actionable_idle: bool = False) -> list:
    """Les agents réveillables au repos : mode `execute`, exécuteur vivant (pas
    une session attachée), au repos (ni en tour, ni en pause de budget, ni
    arrêtés). Partagé par `idle_capacity`, `backlog_empty` et la prise
    automatique (L119) ; `waiting_free` : voir `free_of_lots` (L165b) ; une
    consigne en attente et un lot de session qui l'occupe
    (`session_lot_busy`) occupent l'agent, comme pour la prise
    (`backlog._awaited`).

    #1677 : `actionable_idle` lit le repos sur les lots **actionnables**
    (`actionable_lots`, lu par `operations.listing` : rien qui l'occupe,
    aucune correction attendue d'un verdict bloquant, aucune relecture
    désignée due — #1726) au lieu du temps depuis le dernier tour — un agent
    sans lot prend des tours de courrier toutes les quelques minutes et ne
    serait donc jamais « au repos » (N9). La **prise** de lots et les alertes
    de sous-utilisation s'en servent ; la remise (`handoff.hand`, L233b) garde
    son contrat d'origine, « au repos depuis au moins `idle_s` », qu'un relevé
    sans `actionable_lots` (bancs, appels anciens) impose de toute façon.

    #3480 : un agent éphémère (`ameesh agent spawn --ttl`) n'est jamais au
    repos. C'est un instrument de mesure, pas une capacité : il ne prend rien
    (L119, L165, propositions #1677, remise L233b) et ne fait pas lever une
    alerte de repos (`idle_capacity`, `backlog_empty`). La garde est ici, dans
    le seul calcul du repos, pour que ces appelants l'héritent au lieu de la
    répéter chacun ; la réassignation manuelle reste possible, comme pour la
    désignation (#3411)."""
    idle = []
    for row in listing:
        # #3480 : vivant ou expiré, un éphémère n'est pas une capacité.
        if row.get("ephemeral"):
            continue
        # un agent `externe` n'est jamais compté — dont le chat du
        # propriétaire (L123), qui n'a ni exécuteur ni lot
        if (row.get("mode") or "execute") != "execute" or _state(row) != "idle":
            continue
        if not _awake_runner(row) or int(row.get("unread") or 0):
            continue
        if not free_of_lots(row, waiting_free=waiting_free):
            continue
        if waiting_free and (row.get("has_pending_prompt")
                             or session_lot_busy(row, waiting_free=True)):
            continue
        if int(row.get("actionable_lots") or 0) > 0:
            continue
        if not (actionable_idle and "actionable_lots" in row):
            since = idle_since(row)
            if not since or now - since < idle_s:
                continue
        idle.append(row)
    return idle


def is_backlog_item(lot: dict) -> bool:
    """Élément de la file d'amélioration encore à prendre (L119) : il attend un
    preneur automatique, ce n'est ni un lot en souffrance ni un lot stagnant.
    §4 : un élément `build` sans assigné n'est un élément de la file que s'il a
    été RENDU (`returned_to_queue`) — un élément en cours sans preneur n'y est
    pas."""
    if (lot.get("assignee") or "").strip():
        return False
    if lot.get("state") == lifecycle.REQUESTED:
        return lot.get("type") == "improvement"
    return (lot.get("type") == "improvement" and lot.get("state") == lifecycle.IN_PROGRESS
            and bool(lot.get("returned_to_queue")))


def waiting_project_lots(lots: list, *, db=None, now: float | None = None) -> list:
    """Les lots du projet ouverts sans assigné (hors file d'amélioration) : du
    travail demandé qui attend qu'on le confie. Tant qu'il y en a, la prise
    automatique s'abstient (0037) et `idle_capacity` le signale. Quand la base
    est fournie, seuls les lots **démarrables** attendent un preneur : un lot
    qu'une attente planifiée retient (L368 : dépendance explicite ouverte,
    engagement `depends_on` d'un lot ou d'un paquet ouvert, début prévu à
    venir) attend son prérequis, pas un agent (#1625).

    #3447 : un lot réservé à un exécutant externe (`external_executor`) ne
    compte pas — il n'attend pas un agent de l'équipe (L165 ne le prend pas) ;
    `external_project_lots` le nomme à part pour l'alerte et le débit.

    Une lecture des attentes en échec est conservatrice : le lot reste compté,
    la porte reste fermée — jamais une amélioration prise sur un doute."""
    waiting = [lot for lot in lots
               if not (lot.get("assignee") or "").strip()
               and not (lot.get("external_executor") or "").strip()
               and lot.get("state") in WAITING_LOT_STATES and not is_backlog_item(lot)]
    if not waiting or db is None:
        return waiting
    from . import db as db_mod
    from . import planned_waits
    try:
        waits = planned_waits.read(db, waiting, now=now)
    except db_mod.Unavailable:
        raise
    except Exception:  # noqa: BLE001 - attentes illisibles : on ne devine pas
        return waiting
    return [lot for lot in waiting if int(lot["id"]) not in waits]


def external_project_lots(lots: list) -> list:
    """#3447 : les lots ouverts réservés à un exécutant externe déclaré.

    Ni « sans assigné » (l'alerte les nommait ainsi), ni prenables : ils
    attendent le geste de leur exécutant (bureau, owner), pas un agent. Rendus
    à part pour que l'alerte et le débit nomment cet exécutant."""
    return [lot for lot in lots
            if not (lot.get("assignee") or "").strip()
            and (lot.get("external_executor") or "").strip()
            and lot.get("state") in WAITING_LOT_STATES
            and not is_backlog_item(lot)]


def _budget_held(cfg, db, rows: list) -> dict:
    """#1021 : `{agent: raison}` des agents dont le prochain tour est tenu
    (budget, rythme ou authentification), lu là où l'exécuteur le calcule
    (`account_turn.turn_hold`). `cfg` None (appelant sans hôte) : rien n'est
    jugé. Une tenue illisible laisse l'agent dans l'alerte : c'est
    l'attribution qui tranche, fail-closed."""
    if cfg is None:
        return {}
    from . import account_turn
    from . import db as db_mod
    held = {}
    for row in rows:
        try:
            reason = account_turn.turn_hold(cfg, db, row["name"], row)
        except db_mod.Unavailable:
            raise
        except Exception:  # noqa: BLE001 - une lecture en échec ne masque pas l'alerte
            continue
        if reason:
            held[row["name"]] = reason
    return held


def footprint_blockers(db, waiting_lots: list, lots: list, *,
                       store=None) -> dict:
    """#4699 : `{lot: [bloqueurs]}` des lots d'intake sans porteur que la garde
    d'empreinte (#3185) retient — la lecture partagée par le débit (#4561) et
    `idle_capacity`, jamais une copie.

    Les empreintes de tous les lots ouverts sont lues une fois (le lot retenu
    comme ses occupants) ; `slo.footprint_blockers` applique la règle de la
    garde. Un lot retenu n'est pas démarrable : `work assign` le refuse, et
    `split_startable` le range avec son bloqueur."""
    from . import slo
    if not waiting_lots:
        return {}
    store = store or storage.of(db)
    paths: dict = {}
    for row in store.work_footprints.for_items(
            [int(lot["id"]) for lot in lots]):
        paths.setdefault(int(row["work_item_id"]), []).append(str(row["path"]))
    out: dict = {}
    for lot in waiting_lots:
        blockers = slo.footprint_blockers(lot, lots, paths)
        if blockers:
            out[int(lot["id"])] = blockers
    return out


def split_startable(waiting_lots: list, waits: dict,
                    footprint: dict | None = None) -> tuple:
    """#1608 : sépare les lots confiables (`startable`) de ceux qu'une attente
    planifiée retient (`{id: attente}` de L368) ou que la garde d'empreinte
    occupe (`{id: [bloqueurs]}` de #3185, lue par `footprint_blockers`). Un
    orchestrateur à qui l'on propose un lot bloqué l'assigne pour rien : le
    brief ne propose que les premiers et nomme le bloqueur des seconds."""
    footprint = footprint or {}
    startable = []
    blocked: dict = {}
    for lot in waiting_lots:
        ident = int(lot["id"])
        if ident in waits:
            blocked[ident] = waits[ident]
        elif ident in footprint:
            blocked[ident] = {"what": "footprint",
                              "lots": [int(b["id"]) for b in footprint[ident]]}
        else:
            startable.append(lot)
    return startable, blocked


def lots_for_idle(lots: list, idle: list) -> list:
    """#2337 B4 : ne garder que les lots qu'un agent au repos peut prendre —
    le projet du lot (`app`, `workstream` ; `projects.lot_project`) doit
    figurer dans ceux d'au moins un agent. Un lot sans projet connu reste (on
    ne devine pas) et un repos sans projet déclaré ne filtre rien (cas hub).
    Sans ce croisement, cinq agents Nexlink au repos et un lot ameesh
    démarrable levaient encore l'alerte."""
    from . import projects as projects_mod
    wanted = {project for row in idle
              for project in projects_mod.agent_projects(row)}
    if not wanted:
        return list(lots)
    return [lot for lot in lots
            if not projects_mod.lot_project(lot)
            or projects_mod.lot_project(lot) in wanted]


def waiting_text(blocked: dict, limit: int = 6) -> str:
    """« #12 waits on #3, #4; #15 is scheduled for 2026-10-16 » (L368),
    « #12 waits on the footprint of #7 » (#3185, #4699)."""
    parts = []
    for ident, wait in sorted(blocked.items())[:limit]:
        if wait.get("what") == "dependency":
            parts.append("#%d waits on %s" % (
                ident, ", ".join("#%d" % int(i) for i in wait.get("lots") or ())))
        elif wait.get("what") == "footprint":
            parts.append("#%d waits on the footprint of %s" % (
                ident, ", ".join("#%d" % int(i) for i in wait.get("lots") or ())))
        elif wait.get("what") == "scheduled":
            parts.append("#%d is scheduled for %s" % (ident, wait.get("start")))
        else:
            parts.append("#%d is waiting" % ident)
    if len(blocked) > limit:
        parts.append("…")
    return "; ".join(parts)


def idle_capacity(db, listing: list, now: float, orchestras: list, *,
                  idle_s: float = DEFAULT_IDLE_CAPACITY_S, paid=None,
                  lots: list | None = None, designers=(), held=(), held_hosts=(),
                  pressured: dict | None = None, cfg=None) -> list:
    """#254 : `held`, les agents d'un hôte en pause, ni comptés ni à
    réveiller ; `held_hosts` (hôtes en pause) et `pressured` ({hôte:
    verdict} des hôtes sous pression, relevé frais) ne sont jamais proposés :
    leurs agents sont laissés de côté et nommés.

    #1021 : `cfg` (l'hôte qui juge) — un agent encore `idle` dont les comptes
    viennent de franchir le seuil n'est pas proposé non plus : la pause n'est
    écrite qu'au sondage suivant de son exécuteur, et l'attribution le refuse
    déjà (`work.check_assignee`)."""
    if idle_s <= 0:
        return []
    pressured = pressured or {}
    # L165b : un agent dont tous les lots attendent un autre est au repos
    idle = [row for row in idle_agents(listing, now, idle_s, waiting_free=True,
                                       actionable_idle=True)
            if row["name"] not in held]
    on_hold = sorted((row["name"], row.get("host")) for row in idle
                     if row.get("host") in held_hosts)
    idle = [row for row in idle if row.get("host") not in held_hosts]
    spared = sorted((row["name"], row.get("host")) for row in idle
                    if row.get("host") in pressured)
    idle = [row for row in idle if row.get("host") not in pressured]
    # #1021 : la tenue budget/rythme, lue là où l'exécuteur la calcule
    budget_held = _budget_held(cfg, db, idle)
    if budget_held:
        idle = [row for row in idle if row["name"] not in budget_held]
    if not idle:
        return []
    if lots is None:
        # #4717 : le débit lit tous les lots ouverts (`debit.startable_lots`,
        # `open_lots_activity(None)`) ; le plafond à 500 laissait un occupant
        # au-delà invisible et l'alerte proposait un lot que `work assign`
        # refuse ensuite (#3185/#4699).
        lots = storage.of(db).operations.open_lots_activity(None)
    # #1608 : la liste garde les lots retenus par une attente et les classe
    # (démarrables / en attente d'un prérequis) ; le filtrage `db=db` de
    # #1021 reste dans `waiting_project_lots` pour la prise automatique (0037).
    waiting_lots = waiting_project_lots(lots)
    # #3447 : les lots réservés à un exécutant externe ne sont pas « sans
    # assigné » ; ils sont nommés à part, avec leur exécutant.
    reserved_lots = external_project_lots(lots)
    # #2337 B4 : un lot démarrable d'un autre projet n'est pas une capacité
    # pour ces agents (cinq agents Nexlink au repos + un lot ameesh levait
    # encore l'alerte) ; on croise avec les projets des agents au repos.
    waiting_lots = lots_for_idle(waiting_lots, idle)
    reserved_lots = lots_for_idle(reserved_lots, idle)
    waits = planned_waits.read(db, waiting_lots, now=now) if waiting_lots else {}
    # #4699 : la garde d'empreinte (#3185) retient un lot d'intake sans porteur
    # que `work assign` refuse ; le même filtre que le débit (#4561), rangé
    # dans `blocked_lots` avec ses bloqueurs.
    footprint = footprint_blockers(db, waiting_lots, lots) if waiting_lots else {}
    startable_lots, blocked_lots = split_startable(waiting_lots, waits, footprint)
    backlog = [row for row in listing
               if _state(row) in ("working", "paused") and int(row.get("unread") or 0)
               and row.get("oldest_unread_ts") is not None
               and now - float(row["oldest_unread_ts"]) >= idle_s]
    paid_working = sorted(row["name"] for row in listing
                          if paid and row.get("harness") in paid
                          and _state(row) == "working" and not _attached(row))
    plan_idle = [row for row in idle if paid is not None and row.get("harness") not in paid]
    aggravating = bool(paid_working and plan_idle)
    # #2337 : la levée se lit sur les lots DÉMARRABLES — un lot qu'une
    # dépendance ou une date retient n'est pas du travail à confier, et
    # `waiting_lots` en compte 24 sans qu'un agent puisse rien prendre. Les
    # bloqués restent dans le détail d'une alerte levée par autre chose (un
    # courrier en souffrance, le forfait au repos).
    if not (startable_lots or backlog or aggravating or reserved_lots):
        return []
    names = sorted(row["name"] for row in idle)
    # #1677 (b) : le repos se lit sur les lots actionnables, plus sur le temps
    # depuis le dernier tour (N9) — le texte ne promet plus une durée.
    parts = ["%d wakeable agent(s) idle without an actionable lot: %s"
             % (len(names), ", ".join(names))]
    if waiting_lots:
        parts.append("%d open lot(s) without an assignee (%s)" % (
            len(waiting_lots), ", ".join("#%d" % int(l["id"]) for l in waiting_lots[:8])
            + (", …" if len(waiting_lots) > 8 else "")))
        if startable_lots:
            parts.append("%d startable now: %s" % (
                len(startable_lots),
                ", ".join("#%d" % int(l["id"]) for l in startable_lots[:8])
                + (", …" if len(startable_lots) > 8 else "")))
        if blocked_lots:
            parts.append("%d waiting for a prerequisite: %s" % (
                len(blocked_lots), waiting_text(blocked_lots)))
    if reserved_lots:
        parts.append("%d lot(s) reserved to an external executor, not assignable: %s"
                     % (len(reserved_lots), ", ".join(
                         "#%d (%s)" % (int(l["id"]), l.get("external_executor"))
                         for l in reserved_lots[:8])
                        + (", …" if len(reserved_lots) > 8 else "")))
    if backlog:
        parts.append("mail waiting at %s" % ", ".join(
            sorted(row["name"] for row in backlog)))
    if on_hold:
        parts.append("left out, their host on hold: %s" % ", ".join(
            "%s on %s" % pair for pair in on_hold))
    if budget_held:
        parts.append("left out, next turn held: %s" % ", ".join(
            "%s (%s)" % (name, reason) for name, reason in sorted(budget_held.items())))
    if spared:
        parts.append("left out, their host under pressure: %s" % "; ".join(
            "%s on %s (%s)" % (name, host, ", ".join(
                "%s %s, threshold %s" % (b["label"], b["value"], b["limit"])
                for b in pressured[host]["breaches"][:2]))
            for name, host in spared))
    if aggravating:
        parts.append("plan agents idle (%s) while per-token agents "
                     "are working (%s)" % (", ".join(sorted(r["name"] for r in plan_idle)),
                                          ", ".join(paid_working)))
    by_name = {row["name"]: row for row in listing}
    # #254 : un orchestrateur arrêté avec son hôte en pause n'est pas à réveiller
    attached = [n for n in orchestras if _attached(by_name.get(n) or {})]
    asleep = [n for n in orchestras if n not in attached and n not in held
              and _state(by_name.get(n) or {}) in ("stopped", "paused")]
    if attached:
        action = ("wake the orchestrator: %s is held by an interactive session "
                  "(attach), hand it back; or dispatch by hand "
                  "(ameesh work assign <lot> <agent>)" % ", ".join(attached))
    elif asleep:
        action = ("wake the orchestrator %s (stopped or paused), or dispatch by hand "
                  "(ameesh work assign <lot> <agent>)" % ", ".join(asleep))
    else:
        action = "dispatch: ameesh work assign <lot> <agent>, or write to the orchestrator"
    if waiting_lots and not startable_lots:
        action = "no startable lot: clear the named prerequisite before assigning work"
    if reserved_lots and not startable_lots:
        action = ("no assignable lot: %s is reserved to %s (external executor, not "
                  "wakeable) — the pilot declared it and that executor acts; the "
                  "reservation is not an assignment (clear it with ameesh work "
                  "external-executor <lot> --clear)" % (
                      ", ".join("#%d" % int(l["id"]) for l in reserved_lots[:5]),
                      ", ".join(sorted({str(l.get("external_executor"))
                                        for l in reserved_lots[:5]}))))
    detail = "; ".join(parts) + ". Action: " + action
    responsible = None
    for name in attached + asleep + list(orchestras):
        responsible = (by_name.get(name) or {}).get("responsible")
        if responsible:
            break
    responsible = responsible or _common_responsible(idle)
    return [_alert(
        "idle_capacity", None, None, len(names), idle_s, detail,
        reason="forfait_au_repos" if aggravating else "travail_en_attente",
        agents=names, lots=[int(l["id"]) for l in waiting_lots[:50]],
        startable_lots=[int(l["id"]) for l in startable_lots[:50]],
        blocked_lots={str(i): w for i, w in sorted(blocked_lots.items())[:50]},
        external_lots=[{"lot": int(l["id"]),
                        "executor": str(l.get("external_executor"))}
                       for l in reserved_lots[:50]],
        paid_working=paid_working, orchestrators=list(orchestras),
        designers=[n for n in designers if n not in orchestras],
        responsible=responsible)]


def orchestrator_briefs(alert: dict, listing: list, lot_titles: dict | None = None) -> dict:
    """L118 : `idle_capacity` part aussi aux orchestrateurs, qui peuvent agir,
    et (L165b) aux agents de conception.

    Rend `{orchestrateur: texte}` : à chacun, les agents au repos sans lot de
    SON équipe (tous s'il n'en a pas) et les lots ouverts sans agent (filtrés
    en amont sur les projets des agents au repos, #2337 B4 ; un lot sans
    projet connu reste). Un orchestrateur sans agent à occuper ne
    reçoit rien ; il n'est jamais cité dans sa propre liste.

    #1608 : seuls les lots sans attente planifiée sont proposés (L368) ; les
    autres sont nommés avec leur bloqueur, pour qu'un orchestrateur ne les
    assigne pas pour rien. Une alerte sans `startable_lots` (producteur plus
    ancien, test) garde le comportement d'avant."""
    if alert.get("type") != "idle_capacity":
        return {}
    from . import projects as projects_mod
    by_name = {row["name"]: row for row in listing}
    titles = lot_titles or {}
    lots = [int(l) for l in alert.get("lots") or ()]
    startable = alert.get("startable_lots")
    startable = lots if startable is None else [int(i) for i in startable]
    blocked = {int(k): v for k, v in (alert.get("blocked_lots") or {}).items()}
    external = [(int(row.get("lot")), str(row.get("executor") or "?"))
                for row in alert.get("external_lots") or ()
                if row.get("lot") is not None]
    out = {}
    for orch in list(alert.get("orchestrators") or ()) + list(alert.get("designers") or ()):
        mine = set(projects_mod.agent_projects(by_name.get(orch) or {}))
        names = [n for n in alert.get("agents") or () if n != orch and (
            not mine or mine & set(projects_mod.agent_projects(by_name.get(n) or {})))]
        if not names:
            continue
        parts = ["Idle capacity: %d wakeable agent(s) without an actionable lot: %s."
                 % (len(names), ", ".join(names))]
        if startable:
            parts.append("Open lots without an agent: %s." % ", ".join(
                "#%d%s" % (i, " \"%s\"" % titles[i] if titles.get(i) else "")
                for i in startable[:12]) + (" …" if len(startable) > 12 else ""))
        elif lots:
            parts.append("No startable open lot without an agent: %s." % waiting_text(blocked))
        else:
            parts.append("No open lot without an agent.")
        if startable and blocked:
            parts.append("Waiting for a prerequisite: %s." % waiting_text(blocked))
        if external:
            parts.append("Reserved to an external executor, not assignable: %s."
                         % ", ".join("#%d (%s)" % pair for pair in external[:12])
                         + (" …" if len(external) > 12 else ""))
        if startable:
            parts.append("Give them work tied to a lot: ameesh mail send <agent> "
                         "\"…\" --lot <id> (or --new-lot \"title\"), or ameesh work assign <lot> "
                         "<agent> --claim.")
        elif external:
            parts.append("Do not assign the reserved lots: their external executor acts "
                         "(the pilot can clear a reservation with ameesh work "
                         "external-executor <lot> --clear).")
        elif not lots:
            parts.append("Give them work tied to a lot: ameesh mail send <agent> "
                         "\"…\" --lot <id> (or --new-lot \"title\"), or ameesh work assign <lot> "
                         "<agent> --claim.")
        else:
            parts.append("Do not assign a lot that waits (its blocker is named above): clear the "
                         "blocker, or create a startable lot (ameesh work add, or --new-lot \"title\").")
        out[orch] = " ".join(parts)
    return out


# --------------------------------------------------------------------------
# host_underused
# --------------------------------------------------------------------------

def host_verdicts(db, canons) -> dict:
    """{hôte: (dernier relevé, verdict de pression)}, aux limites physiques
    des fiches Host de tous les canons."""
    from . import resources as resources_mod
    verdicts = {}
    for reading in storage.of(db).hosts.current(None):
        try:
            # #2825 N4 : le défaut de CET hôte vient de son relevé, pas du
            # processus qui interroge la flotte.
            limits = resources_mod.host_limits(
                canons, reading["host"], reading=reading)["limits"]
        except Exception:
            limits = None
        verdicts[reading["host"]] = (reading, resources_mod.pressure(reading, limits=limits))
    return verdicts


def pressured_hosts(verdicts: dict, now: float) -> dict:
    """#254 : {hôte: verdict} des hôtes sous pression d'après un relevé
    frais (un relevé vieux ne dit rien de l'hôte maintenant)."""
    return {host: verdict for host, (reading, verdict) in verdicts.items()
            if verdict["blocked"] and reading.get("sampled_ts") is not None
            and now - float(reading["sampled_ts"]) <= HOST_FRESH_S}


def host_underused(cfg, db, listing: list, now: float, *,
                   window_s: float = DEFAULT_HOST_UNDERUSED_S,
                   load_max: float = DEFAULT_HOST_UNDERUSED_LOAD,
                   turns_max: float = DEFAULT_HOST_UNDERUSED_TURNS,
                   idle_mail_s: float = 300.0, canons=None,
                   verdicts: dict | None = None) -> list:
    if window_s <= 0 or load_max <= 0:
        return []
    hosts = storage.of(db).hosts
    if canons is None:
        from . import canon as canon_mod
        try:
            canons = canon_mod.load_configured(cfg) if cfg is not None else []
        except Exception:
            canons = []
    if verdicts is None:
        verdicts = host_verdicts(db, canons)
    out = []
    for host, (reading, verdict) in sorted(verdicts.items()):
        if verdict["blocked"]:
            continue
        if reading.get("sampled_ts") is None or now - float(reading["sampled_ts"]) > HOST_FRESH_S:
            continue
        pressured = [(h, v) for h, (_r, v) in sorted(verdicts.items())
                     if h != host and v["blocked"]]
        waiting = sorted(
            row["name"] for row in listing
            if (row.get("host") or "") != host and (row.get("mode") or "execute") == "execute"
            and (_state(row) == "paused"
                 or (_state(row) == "idle" and int(row.get("unread") or 0)
                     and row.get("oldest_unread_ts") is not None
                     and now - float(row["oldest_unread_ts"]) >= idle_mail_s)))
        if not pressured and not waiting:
            continue
        usage = hosts.usage(host, window_s)
        if not usage or not usage.get("samples") or usage.get("first_ts") is None:
            continue
        if now - float(usage["first_ts"]) < 0.9 * window_s:
            continue                    # fenêtre pas encore couverte par les relevés
        peak = usage.get("max_load_per_cpu")
        turns = float(usage.get("avg_turns") or 0.0)
        if peak is None or float(peak) >= load_max or turns >= turns_max:
            continue
        why = []
        if pressured:
            why.append("%s under pressure (%s)" % (", ".join(h for h, _v in pressured), "; ".join(
                "%s %s (threshold %s)" % (b["label"], b["value"], b["limit"])
                for _h, v in pressured for b in v["breaches"][:2])))
        if waiting:
            why.append("agents waiting elsewhere: %s" % ", ".join(waiting))
        detail = ("host %s underused for %s (load at most %.2f per CPU, %.1f turn(s) "
                  "running on average) while %s. Suggestion: place admitted agents there "
                  "(ameesh placement check --agent <name>, decision 0028); nothing is moved "
                  "automatically" % (host, _duration(window_s), float(peak), turns,
                                   "; ".join(why)))
        out.append(_alert(
            "host_underused", None, None, round(float(peak), 3), load_max, detail,
            host=host, reason="pression_ailleurs" if pressured else "agents_en_attente",
            pressured=[h for h, _v in pressured], waiting=waiting,
            avg_turns=round(turns, 2), samples=int(usage["samples"])))
    return out


# --------------------------------------------------------------------------
# balance_low
# --------------------------------------------------------------------------

def _local_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def balance_rate(points: list) -> tuple | None:
    """(dépense, durée couverte en s) d'une série de soldes chronologique :
    somme des baisses entre relevés successifs ; une hausse est une recharge,
    ignorée. None si la série couvre moins de `BALANCE_MIN_SPAN_S`."""
    if len(points) < 2:
        return None
    span = float(points[-1]["observed_ts"]) - float(points[0]["observed_ts"])
    if span < BALANCE_MIN_SPAN_S:
        return None
    spent = sum(max(0.0, float(a["total"]) - float(b["total"]))
                for a, b in zip(points, points[1:]))
    return spent, span


def balance_low(db, listing: list, now: float, *,
                hours: float = DEFAULT_BALANCE_HOURS,
                min_usd: float = DEFAULT_BALANCE_MIN_USD,
                window_s: float = DEFAULT_BALANCE_WINDOW_S, cfg=None) -> list:
    if hours <= 0 and min_usd <= 0:
        return []
    rows = storage.of(db).operations.balances(provider=None,
                                              since_s=max(window_s, BALANCE_MIN_SPAN_S))
    from . import balance
    rows = balance.normalize_readings(rows, cfg)
    series: dict = {}
    for row in rows:
        series.setdefault((row["provider"], row["currency"], row.get("account")),
                          []).append(row)
    out = []
    for (provider, currency, account), points in sorted(
            series.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "")):
        points.sort(key=lambda r: float(r["observed_ts"]))
        last = points[-1]
        total = float(last["total"])
        measured = balance_rate(points)
        rate = measured[0] / measured[1] * 3600.0 if measured else None
        autonomy_h = (total / rate) if rate else None
        causes = []
        if hours > 0 and autonomy_h is not None and autonomy_h < hours:
            causes.append("autonomie")
        usd = currency.upper() == "USD"
        if min_usd > 0 and usd and total < min_usd:
            causes.append("solde")
        if not causes:
            continue
        who = "%s/%s" % (provider, account) if account else provider
        parts = ["balance %s: %.2f %s (read on %s)" % (
            who, total, currency, _local_time(last["observed_ts"]))]
        if rate is not None:
            parts.append("actual rate %.2f %s/h (average over %s, top-ups ignored)"
                         % (rate, currency, _duration(measured[1])))
        else:
            parts.append("actual rate unknown (not enough readings)")
        empty_ts = None
        if autonomy_h is not None:
            empty_ts = float(last["observed_ts"]) + autonomy_h * 3600.0
            parts.append("runway ≈ %s, expected to run out on %s"
                         % (_duration(autonomy_h * 3600.0), _local_time(empty_ts)))
        elif rate == 0:
            parts.append("no spending seen over the window")
        parts.append("top up the account, or steer work to the plans")
        urgent = ((autonomy_h is not None and autonomy_h < BALANCE_URGENT_HOURS)
                  or (usd and total < BALANCE_URGENT_USD))
        out.append(_alert(
            "balance_low", None, None, round(total, 4),
            min_usd if causes == ["solde"] else hours, "; ".join(parts),
            provider=provider, currency=currency, account=account, causes=causes,
            balance=round(total, 4), rate_per_h=round(rate, 4) if rate is not None else None,
            autonomy_h=round(autonomy_h, 2) if autonomy_h is not None else None,
            empty_ts=round(empty_ts, 3) if empty_ts is not None else None,
            observed_ts=round(float(last["observed_ts"]), 3), urgent=bool(urgent),
            responsible=_common_responsible(
                [r for r in listing if r.get("harness") == provider])))
    return out


# --------------------------------------------------------------------------
# backlog_empty (L119, décision 0037)
# --------------------------------------------------------------------------

def backlog_empty(db, listing: list, now: float, *,
                  idle_s: float = DEFAULT_IDLE_CAPACITY_S) -> list:
    """`backlog_empty` : des agents réveillables sont au repos sans lot depuis
    plus de `idle_s` et la file d'amélioration n'a plus d'élément à prendre.
    ameesh ne doit s'arrêter que faute de travail : l'humain responsable est
    invité à remplir la file (`ameesh work backlog add`)."""
    if idle_s <= 0:
        return []
    idle = idle_agents(listing, now, idle_s, actionable_idle=True)
    if not idle:
        return []
    # #1696 (N2 de #1242) : une file dont tous les éléments sont bloqués n'est
    # pas prenable — même filtre `ready_only` que la prise automatique (L165).
    if storage.of(db).work.backlog(open_only=True, limit=1, ready_only=True):
        return []
    # des lots du projet démarrables attendent un preneur : c'est
    # `idle_capacity` qui parle ; des lots tous retenus par une attente
    # planifiée ne taisent pas une file vide (#1625)
    if waiting_project_lots(storage.of(db).operations.open_lots_activity(500), db=db, now=now):
        return []
    names = sorted(row["name"] for row in idle)
    return [_alert(
        "backlog_empty", None, None, len(names), idle_s,
        "%d wakeable agent(s) idle without an actionable lot (%s) and the improvement "
        "backlog is empty. Action: add items with expected value "
        "(ameesh work backlog add), or assign a lot (ameesh work assign)"
        % (len(names), ", ".join(names)),
        agents=names, responsible=_common_responsible(idle))]


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def alerts(cfg, db, listing: list, now: float | None = None, *,
           plan_tail_s: float = DEFAULT_PLAN_TAIL_S,
           plan_used_pct: float = DEFAULT_PLAN_USED_PCT,
           plan_pace_gap_pct: float = DEFAULT_PLAN_PACE_GAP_PCT,
           idle_capacity_s: float = DEFAULT_IDLE_CAPACITY_S,
           orchestrator_held_s: float = DEFAULT_ORCHESTRATOR_HELD_S,
           orchestrators_declared=(),
           host_underused_s: float = DEFAULT_HOST_UNDERUSED_S,
           host_underused_load: float = DEFAULT_HOST_UNDERUSED_LOAD,
           host_underused_turns: float = DEFAULT_HOST_UNDERUSED_TURNS,
           balance_hours: float = DEFAULT_BALANCE_HOURS,
           balance_min_usd: float = DEFAULT_BALANCE_MIN_USD,
           balance_window_s: float = DEFAULT_BALANCE_WINDOW_S,
           backlog_empty_s: float = DEFAULT_IDLE_CAPACITY_S,
           idle_mail_s: float = 300.0, held=(), held_hosts=()) -> list:
    """Les alertes de sous-utilisation en cours (non triées). `held` (#254) :
    les agents d'un hôte en pause voulue."""
    now = time.time() if now is None else float(now)
    from . import canon as canon_mod
    try:
        canons = canon_mod.load_configured(cfg) if cfg is not None else []
    except Exception:
        canons = []
    paid = paid_harnesses(getattr(cfg, "host", None))
    out = plan_underused(db, listing, now, tail_s=plan_tail_s, used_pct=plan_used_pct,
                         pace_gap_pct=plan_pace_gap_pct, paid=paid, held=held, cfg=cfg)
    orchestras = alert_orchestrators(cfg, db, listing, declared=orchestrators_declared,
                                     canons=canons)
    for alert in out:
        # L132 : `plan_underused` part aussi aux orchestrateurs (alert_briefs)
        alert["orchestrators"] = list(orchestras)
    out += orchestrator_held(listing, now, orchestras, held_s=orchestrator_held_s)
    names = {row["name"] for row in listing}
    designers = sorted(n for n in role_orchestrators(canons, DESIGN_ROLES) if n in names)
    try:
        # #2851 : un seul relevé d'hôtes pour les deux alertes (la lecture
        # `current` était payée deux fois par passe).
        verdicts = host_verdicts(db, canons)
        pressured = pressured_hosts(verdicts, now)
    except Exception:
        verdicts, pressured = {}, {}
    out += idle_capacity(db, listing, now, orchestras, idle_s=idle_capacity_s, paid=paid,
                         designers=designers, held=held, held_hosts=held_hosts,
                         pressured=pressured, cfg=cfg)
    out += host_underused(cfg, db, listing, now, window_s=host_underused_s,
                          load_max=host_underused_load, turns_max=host_underused_turns,
                          idle_mail_s=idle_mail_s, canons=canons, verdicts=verdicts)
    from . import host_capacity_alerts
    out += host_capacity_alerts.alerts(
        cfg, db, listing, now, canons=canons,
        readings={host: reading for host, (reading, _verdict) in verdicts.items()},
        held_hosts=held_hosts, orchestrators=orchestras)
    out += balance_low(db, listing, now, hours=balance_hours, min_usd=balance_min_usd,
                       window_s=balance_window_s, cfg=cfg)
    out += backlog_empty(db, listing, now, idle_s=backlog_empty_s)
    # #3256 A : la feuille de route notée épuisée sur un lot de découpage
    from . import file_decoupage
    out += file_decoupage.exhausted_alerts(db, listing)
    # #4148: adapter failures and withdrawals stuck past their bound are
    # addressed to the mesh agents, like the reserved-capacity warnings.
    from . import host_release
    try:
        out += host_release.alerts(cfg, db, now, listing=listing,
                                   orchestrators=orchestras)
    except Exception:  # an unavailable control never drops the other alerts
        pass
    return out
