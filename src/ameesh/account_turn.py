# SPDX-License-Identifier: AGPL-3.0-only
"""Les comptes multiples côté exécuteur (lot L30, décision 0027).

Points d'accroche de `runner.AgentWorker`, regroupés ici pour que l'exécuteur
ne porte que des appels d'une ligne :

* `choose`     — avant chaque tour, dans la garde de budget : choisit le compte
                 (0034 : la session garde son compte sous son seuil ; sinon
                 celui qui a le plus de retard sur son rythme, amendement
                 L117 ; pause si tous sont au seuil) et journalise le choix et
                 sa raison ;
* `continuity` — avant de consommer le travail : si la session de l'agent a
                 été ouverte sous un autre compte et que le harnais ne peut pas
                 la reprendre sous le nouveau, rotation avec résumé (L11) ;
* `for_turn`, `apply`, `mark` — dans le tour : le compte du tour, son
                 environnement (CLAUDE_CONFIG_DIR / CODEX_HOME / clé), et le
                 marqueur qui attribue au compte les relevés du tour.

Jamais au milieu d'un tour : tout se décide avant le lancement du harnais.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import accounts, adapters, auth_failure, cost as cost_mod, db as db_mod, registry

#: compte indisponible (configuration ou base) : le tour ne part pas
INVALID = object()

#: reprise quand le résumé n'a pas pu être produit sous l'ancien compte
FALLBACK_RESUME = (
    "Resuming after an account switch: the previous session (%s), opened under "
    "account %s, cannot be resumed under account %s and could not be "
    "summarized. Reread your project thread and the state of your lot before continuing."
)


def _log(message: str) -> None:
    from .runner import log_async
    log_async(message)


def own_harness(worker) -> str:
    """Le harnais de l'agent (son registre)."""
    return (worker.agent or {}).get("harness") or ""


def _harness(worker) -> str:
    """Le harnais du tour : celui du compte imposé (résumé d'une rotation),
    sinon le harnais emprunté par un agent critique (L242b (3)), sinon le
    sien."""
    override = getattr(worker, "_harness_override", None)
    if isinstance(override, str) and override:
        return override
    borrowed = getattr(worker, "_borrowed", None)
    if isinstance(borrowed, tuple):
        return borrowed[0]
    return own_harness(worker)


def choose(worker, book=None, *, pause: bool = True, margin: bool | None = None) -> str | None:
    """Choisit le compte du prochain tour. None : aucun compte déclaré.

    Rend '' quand un compte est retenu (posé dans `worker._account`), la raison
    de pause sinon. `pause=False` (garde de budget désactivée) : on bascule
    quand même, et le rythme ne met jamais en pause — mais L226b : un compte
    en overage ou au-dessus d'un plafond fixe n'est jamais retenu ; s'il n'en
    reste pas d'autre, la raison est rendue et le tour ne part pas.

    `margin` (L242b) : la réserve des non-critiques sous le rythme ; par
    défaut, pour un non-critique seulement (`accounts.choose`).
    """
    harness = own_harness(worker)        # L242b (3) : toujours le sien d'abord
    try:
        items = accounts.profiles(worker.cfg, harness)
    except accounts.AccountError as exc:
        return "invalid account configuration: %s" % exc if pause else None
    if not items:
        return None
    if book is None:
        book = cost_mod.CostBook(state_dir=worker.cfg.state_dir, db=worker.db,
                                 tools={worker.name: harness})
    try:
        choice = accounts.choose(worker.db, worker.cfg.host, harness, items, book,
                                 agent=worker.name, paced=pause,
                                 session_account=session_account(worker, items),
                                 critical=is_critical(worker), margin=bool(margin))
    except db_mod.Unavailable:
        # Base injoignable (L72) : ce n'est ni une pause budget ni un problème
        # de migration — l'exécuteur suspend les tours et réessaie, avec son
        # propre statut « base injoignable », levé au retour de la base.
        raise
    except db_mod.DbError as exc:
        # sans état des comptes, on ne sait pas quel compte est sous le seuil
        return ("account state unavailable (%s)" % db_mod.explain(exc)
                if pause else None)
    if choice.switched:
        journal(worker, harness, choice.switched)
    if choice.profile is None:
        # garde de budget désactivée (`pause=False`) : `accounts.choose` a déjà
        # levé le rythme ; il ne reste que des comptes en overage ou au-dessus
        # d'un plafond fixe (L226b) — pas de tour, quels que soient les réglages
        return choice.reason
    log_choice(worker, harness, choice)
    worker._account = choice.profile
    return ""


def fallback(worker, reason: str, limits=None) -> str:
    """L242b (3) : un agent critique en pause budget sur son harnais tourne
    sous un autre, dans l'ordre de `critical_fallback_harnesses` (Codex au
    forfait, puis DeepSeek à clé d'API). Le premier qui a de la marge est
    retenu (`worker._borrowed = (harnais, compte ou None)`, `worker._account`)
    et '' est rendu ; sinon `reason`, inchangée.

    Un harnais retenu doit être installé (`adapters.adapter_for`), avoir un
    compte sous son seuil (choix propre à l'agent, `critical=True` ; sans
    compte déclaré, son forfait par défaut sous son rythme) et, s'il est payé
    au token, tenir dans le plafond du mesh (plein : l'agent est critique ;
    les non-critiques s'arrêtent avant, L242b (2)). Le changement de harnais
    se fait par rotation avec résumé (`continuity`)."""
    for harness in getattr(worker.cfg, "critical_fallback_harnesses", None) or ():
        if harness == own_harness(worker):
            continue
        try:
            adapters.adapter_for(harness)
        except Exception:  # noqa: BLE001 - harnais absent de l'hôte : suivant
            continue
        try:
            items = accounts.profiles(worker.cfg, harness)
        except accounts.AccountError:
            continue
        kwargs = limits.book_kwargs() if limits is not None else {}
        book = cost_mod.CostBook(state_dir=worker.cfg.state_dir, db=worker.db,
                                 tools={worker.name: harness}, **kwargs)
        profile = None
        try:
            if items:
                choice = accounts.choose(
                    worker.db, worker.cfg.host, harness, items, book, agent=worker.name,
                    critical=True, session_account=session_account(worker, items)
                    if session_harness(worker) == harness else None)
                if choice.profile is None or choice.auth_blocked:
                    continue
                profile = choice.profile
            elif book.pace_exceeded(harness):
                continue
            if book.over(worker.name, pace=False):
                continue                    # payé au token : plafond du mesh
        except db_mod.Unavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - état illisible : suivant
            _log("[%s] fallback harness %s unreadable (%s)" % (worker.name, harness, exc))
            continue
        previous = getattr(worker, "_borrowed_seen", None)
        worker._borrowed = (harness, profile)
        worker._account = profile
        label = "%s/%s" % (harness, profile.name if profile else "(default)")
        if previous != label:
            worker._borrowed_seen = label
            texte = ("Critical agent at threshold on every %s account (%s): its turns "
                     "run under %s until its own account has headroom again (L242b)."
                     % (own_harness(worker), reason, label))
            _log("[%s] %s" % (worker.name, texte))
            worker._fil_note(texte, meta={"action": "harnais", "harnais": harness,
                                          "compte": profile.name if profile else None})
        return ""
    return reason


def borrowing(worker) -> bool:
    """L242b (3) : le dernier tour lancé est-il parti sous un autre harnais
    que le sien (`turn_harness` de l'état local) ?"""
    read = getattr(worker, "_state_read", None)
    last = read("turn_harness") if callable(read) else ""
    return bool(isinstance(last, str) and last and last != own_harness(worker))


def session_harness(worker) -> str:
    """Le harnais sous lequel la session courante a tourné (L242b (3)) : noté
    par l'exécuteur à chaque session (`session_harness` de l'état local),
    sinon le sien."""
    read = getattr(worker, "_state_read", None)
    noted = read("session_harness") if callable(read) else ""
    return noted if isinstance(noted, str) and noted else own_harness(worker)


def is_critical(worker) -> bool:
    """L242b : l'agent est-il critique (orchestrateur, conception, auditeur,
    ou déclaré) ? Son compte est alors choisi pour lui seul (`accounts.choose`)."""
    check = getattr(getattr(worker, "runner", None), "is_critical", None)
    try:
        return bool(check and check(worker.name, worker.agent))
    except Exception:  # noqa: BLE001 - rôles illisibles : le choix ordinaire
        return False


def session_account(worker, items) -> str | None:
    """Le compte de la session en cours de l'agent (0034 §4), ou None.

    None sans session (ouverture : le choix est libre). Session d'avant L30
    (aucun compte noté) : le compte déclaré dont le dossier est celui que le
    harnais prend par défaut, s'il y en a un.
    """
    try:
        if not worker.current_session():
            return None
    except Exception:
        return None
    name = _session_account(worker)
    if name:
        return name
    legacy = legacy_profile(items[0].harness, items)
    return legacy.name if legacy in items else None


def legacy_profile(harness: str, items):
    """Le compte d'une session d'avant L30 (aucun compte noté) : elle a été
    ouverte avec le dossier par défaut du harnais. L226b : le compte déclaré
    qui a ce dossier, pour que sa santé, ses jauges et ses plafonds soient lus
    sous son nom ; sinon le profil synthétique « (default) »."""
    default = accounts.Profile(harness=harness, name="(default)")
    for profile in items:
        if profile.home() == default.home():
            return profile
    return default


def log_choice(worker, harness: str, choice) -> None:
    """Chaque choix de compte est journalisé avec sa raison (0034 §5).

    Une ligne au journal de l'exécuteur quand le compte retenu ou la nature
    du choix (retard sur le rythme, continuité, forçage) change pour cet
    agent — pas à chaque sondage : la garde passe ici avant chaque tour et à
    chaque sondage d'un agent en attente, et la raison (« 35 % utilisé
    (rythme 55 %) ») vieillit.
    """
    cle = (choice.profile.name, choice.kept, choice.forced)
    if getattr(worker, "_account_choice_logged", None) == cle:
        return
    worker._account_choice_logged = cle
    _log("[%s] account %s: %s — %s" % (worker.name, harness, choice.profile.name,
                                      choice.why or "only usable account"))


def journal(worker, harness: str, switched: dict) -> None:
    """Une bascule = une ligne de journal + une entrée dans le fil de l'équipe.

    La ligne `account_switches` est déjà écrite (même transaction que le
    changement de compte) ; ici, la trace lisible. L242b : la bascule propre
    à un agent critique (`kind` `agent`) n'a pas de ligne en base ; sa trace
    n'est écrite qu'une fois par changement.
    """
    if switched["kind"] == "agent":
        cle = (switched["from"], switched["to"])
        if getattr(worker, "_agent_switch_logged", None) == cle:
            return
        worker._agent_switch_logged = cle
        texte = ("Account switch %s for this critical agent only: %s → %s (%s). The "
                 "shared choice and any force stay as they are." % (
                     harness, switched["from"], switched["to"], switched["reason"]))
        _log("[%s] %s" % (worker.name, texte))
        worker._fil_note(texte, meta={"action": "compte", "type": "agent",
                                      "harnais": harness, "de": switched["from"],
                                      "vers": switched["to"]})
        return
    texte = ("Account switch %s: %s → %s (%s). New sessions go to "
             "account %s; a running session keeps its account while it is under "
             "its threshold (0034)." % (harness, switched["from"], switched["to"],
                                    switched["reason"], switched["to"]))
    _log("[%s] %s" % (worker.name, texte))
    worker._fil_note(texte, meta={"action": "compte", "type": switched["kind"],
                                  "harnais": harness, "de": switched["from"],
                                  "vers": switched["to"]})


def for_turn(worker):
    """Le compte de ce tour : imposé (résumé de bascule), choisi, ou actif.

    None : aucun compte déclaré pour le harnais (comportement d'avant L30).
    `INVALID` : configuration ou état illisible — le tour ne part pas.
    """
    if worker._account_override is not None:
        return worker._account_override
    harness = _harness(worker)
    try:
        items = accounts.profiles(worker.cfg, harness)
    except accounts.AccountError as exc:
        worker._account_error = "invalid account configuration: %s" % exc
        return INVALID
    if not items:
        return None
    if worker._account is not None and accounts.by_name(items, worker._account.name):
        return worker._account
    try:
        return accounts.current(worker.db, worker.cfg.host, harness, items)
    except db_mod.Unavailable:
        raise  # panne de base (L72) : la consigne est remise, pas d'échec de tour
    except db_mod.DbError as exc:
        worker._account_error = "account state unavailable: %s" % db_mod.explain(exc)
        return INVALID


def apply(worker, env: dict, compte) -> bool:
    """Environnement du compte pour ce tour ; faux (tour remis en attente) sinon.

    Appelé à CHAQUE lancement, garde de budget active ou non : les variables
    d'identifiants des comptes non choisis sont retirées, puis le profil choisi
    est validé et appliqué (`accounts.launch_env`). Sans comptes déclarés sur
    l'hôte, rien ne change.
    """
    if compte is INVALID:
        message = getattr(worker, "_account_error", "") or "account unavailable"
        _log("[%s] %s: turn not started" % (worker.name, message))
        worker.fail_turn("account unavailable", message)
        return False
    try:
        declared = accounts.parse(getattr(worker.cfg, "accounts", None) or {})
        if declared:
            accounts.launch_env(env, declared, compte)
    except accounts.AccountError as exc:
        _log("[%s] %s: turn not started" % (worker.name, exc))
        worker.fail_turn("account unusable", str(exc))
        return False
    return True


def mark(worker, events_path: str, compte) -> None:
    """Écrit le marqueur de compte dans le flux, juste avant le tour."""
    ligne = {"type": cost_mod.ACCOUNT_MARKER, "harness": compte.harness,
             "account": compte.name, "ts": time.time(), "agent": worker.name}
    try:
        with open(events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ligne, ensure_ascii=False) + "\n")
    except OSError as exc:
        _log("[%s] account marker not written (%s)" % (worker.name, exc))
    worker._compte_session = compte.name


def marker_account(cfg, name: str, harness: str) -> str | None:
    """Le compte du dernier marqueur du flux `events.jsonl` de l'agent (L30)."""
    book = cost_mod.CostBook(state_dir=cfg.state_dir, db=None, tools={name: harness})
    return book.account_at(name, sys.maxsize, harness)


def recorded_session_account(cfg, row: dict | None) -> str | None:
    """Le compte d'origine de la session enregistrée d'un agent (L39, 0030) :
    `agent_registry.session_account`, sinon le dernier marqueur du flux
    (sessions antérieures à L39). None : inconnu."""
    row = row or {}
    if row.get("session_account"):
        return str(row["session_account"])
    return marker_account(cfg, row.get("name") or "", row.get("harness") or "")


def _session_account(worker) -> str | None:
    """Le compte sous lequel la session courante a tourné.

    L39 (0030) : d'abord le registre (`session_account`, écrit avec l'id de
    session par l'exécuteur, ou par `ameesh adopt` pour une session qu'il n'a
    pas ouverte) ; sinon le dernier marqueur du flux (compatibilité), lu une
    fois puis tenu à jour par `mark` : `pick` passe ici à chaque sondage,
    sans relire le flux.
    """
    enregistre = (worker.agent or {}).get("session_account")
    if enregistre:
        return str(enregistre)
    if not hasattr(worker, "_compte_session"):
        worker._compte_session = marker_account(worker.cfg, worker.name, _harness(worker))
    return worker._compte_session


def session_on(worker, compte) -> bool:
    """La session courante tourne-t-elle sous `compte` (règle de `continuity`) ?
    Vrai sans comptes déclarés (`compte` None) ou sans session."""
    if compte is None or not worker.current_session():
        return True
    if compte.harness != session_harness(worker):
        # #389 r1 : deux comptes de harnais différents peuvent porter le même
        # nom — jamais la même session
        return False
    previous = _session_account(worker)
    if previous is None:
        # session d'avant L30 : ouverte avec le dossier par défaut du harnais
        return accounts.Profile(harness=_harness(worker), name="(default)").home() \
            == compte.home()
    return previous == compte.name


def continuity(worker) -> bool:
    """Session ouverte sous un autre compte : reprise, ou rotation avec résumé.

    Reprise quand le harnais le permet (`accounts.session_portable` : même
    stockage de sessions) ; sinon le résumé est produit sous l'ANCIEN compte,
    la session est oubliée, et le tour suivant repart du résumé sous le
    nouveau — et le fil le dit. Seulement s'il y a du travail : on ne résume
    pas une session pour rien. Faux quand une rotation requise n'a pas eu
    lieu (bail perdu, résumé non gardé sur disque) : le tour n'est pas tenté
    (#389 r2).
    """
    harness = _harness(worker)
    session = worker.current_session()
    if session and session_harness(worker) != harness:
        # L242b (3) : la session a tourné sous un autre harnais (repli d'un
        # agent critique, ou retour au sien) — jamais reprenable : rotation
        # avec résumé sous l'ancien harnais, s'il y a du travail
        if worker.peek():
            return rotate_harness(worker, session_harness(worker), harness, session)
        return True
    try:
        items = accounts.profiles(worker.cfg, harness)
    except accounts.AccountError:
        return True
    if not items:
        return True
    new = for_turn(worker)
    if new is None or new is INVALID:
        return True
    session = worker.current_session()  # même règle que le tour (L26 : oubli par restart)
    if not session:
        return True
    previous = _session_account(worker)
    if previous == new.name:
        return True
    if previous is None:
        # session d'avant L30 : ouverte avec le dossier par défaut du harnais,
        # résolu en son compte déclaré (L226b : santé et jauges sous son nom)
        old = legacy_profile(harness, items)
        if old.home() == new.home():
            return True
    else:
        old = accounts.by_name(items, previous)
    if accounts.session_portable(harness, old, new):
        texte = ("Account switch %s: session %s, opened under account %s, is "
                 "resumed as is under account %s (shared session storage)."
                 % (harness, session, old.name, new.name))
        _log("[%s] %s" % (worker.name, texte))
        worker._fil_note(texte, meta={"action": "compte", "session": session,
                                      "continuite": "reprise"})
        worker._compte_session = new.name
        # L39 : le registre suit (sinon `_session_account` relirait l'ancien
        # compte à chaque sondage)
        try:
            registry.set_session_account(worker.db, worker.name, new.name)
            if worker.agent is not None:
                worker.agent["session_account"] = new.name
        except db_mod.DbError as exc:
            _log("[%s] session account not recorded (%s)" % (worker.name, exc))
        return True
    if not worker.peek():
        return True
    return rotate(worker, harness, old, new, session)


def summary_possible(old, book, items, now: float | None = None, *,
                     sick: dict | None = None) -> tuple[bool, str]:
    """L'ancien compte peut-il encore porter le tour de résumé d'une rotation ?
    (L39, décision 0030) `(vrai, "")` ou `(faux, raison)`.

    Non s'il est retiré de la configuration, si son profil est inutilisable
    (`accounts.check` : dossier, identifiants, clé) ou si l'une de ses jauges
    est SATURÉE (100 %, fenêtre non remise à zéro) : le fournisseur refuserait
    le tour. Un compte seulement « au seuil » de rythme (garde 0019) reste
    utilisable pour un résumé — c'est la règle de 0027.

    L226b : non plus s'il est en échec d'authentification (`sick`, les lignes
    de `auth_failure.failing`), en overage, ou au-dessus d'un plafond fixe
    (`accounts.fixed_caps` : 50 % d'un primaire, plafond d'overage) — aucun
    appel de modèle sur un compte que le choix exclut hors rythme.
    """
    if old is None:
        return False, "account removed from the configuration"
    problems = accounts.check(old)
    if problems:
        return False, "; ".join(problems)
    if sick and old.name in sick:
        return False, "authentication failing (%s)" % (
            sick[old.name].get("reason") or "?")
    now = time.time() if now is None else now
    try:
        gauges = accounts.gauges_of(book, old, items) if book is not None else []
    except Exception as exc:
        # L226b : jauges illisibles — pas d'appel de modèle dans le doute
        return False, "%s gauges unreadable (%s)" % (old.harness, exc)
    if accounts.in_overage(gauges, now):
        return False, "%s plan in overage (billed beyond the plan)" % old.harness
    fixed = accounts.fixed_caps(old, gauges)
    for gauge in gauges:
        used = float(gauge.used_at(now) or 0.0)  # échue : 0 % (L71)
        if used >= 1.0:
            return False, "plan %s %s saturated (%.0f %%)" % (
                old.harness, gauge.key, float(gauge.used) * 100)
        for cap, label in fixed:
            if used >= cap:
                return False, "plan %s %s: %.0f %% (%s %.0f %%)" % (
                    old.harness, gauge.key, used * 100, label, cap * 100)
    return True, ""


def paid_cap(worker, harness: str) -> str:
    """La raison qui interdit un tour payé au token sous `harness` (plafonds
    du mesh et de l'agent, `CostBook.over` ; un forfait n'y est pas soumis),
    ou ''. Une part du plafond horaire pour un non-critique (L242b (2)) ;
    état illisible : interdit, dans le doute pas d'appel de modèle."""
    budget_limits = getattr(getattr(worker, "runner", None), "budget_limits", None)
    if budget_limits is None or harness not in cost_mod.paid_harnesses_of():
        return ""
    try:
        limits = budget_limits(worker.db)
        if limits.per_hour <= 0:
            return ""
        book = cost_mod.CostBook(state_dir=worker.cfg.state_dir, db=worker.db,
                                 tools={worker.name: harness}, **limits.book_kwargs())
        part = 1.0 if is_critical(worker) else getattr(
            worker.cfg, "noncritical_hourly_share", 0.75)
        return book.over(worker.name, pace=False, hourly_share=part)
    except db_mod.Unavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - dans le doute, pas de résumé
        return "paid cap unreadable (%s)" % exc


def turn_hold(cfg, db, name: str, row: dict, *, now: float | None = None) -> str:
    """#1021 : la tenue budget/rythme qui empêche le prochain tour de `name`,
    ou '' — lue là où la garde de l'exécuteur la calcule.

    Un lot confié à un agent que son exécuteur ne réveillera pas dort (#885,
    #886, #967, assignés à des agents Codex en pause budget) : l'attribution
    (`work.check_assignee`) doit donc voir la tenue, pas seulement la
    réveillabilité du registre. Deux sources, sans aucune écriture ni journal
    (appelable d'un tour de CLI) :

    * la pause DÉJÀ ANNONCÉE par l'exécuteur (`status` `blocked` et
      `status_text` « budget : … », la règle de `handoff.paused`) : elle vient
      de l'hôte de l'agent, seul à connaître ses comptes — elle vaut donc
      aussi pour un agent d'un autre hôte ;
    * une évaluation FRAÎCHE, quand l'agent est sur CET hôte et que son
      harnais a des comptes déclarés : la pause n'est écrite qu'au sondage
      suivant de son exécuteur (quelques secondes) et une attribution ne doit
      pas passer dans cette fenêtre. Mêmes règles que `Runner.budget_reason` :
      `accounts.choose` en simulation (réserve des non-critiques, L226b ;
      rythme levé si la garde est coupée), puis les plafonds du mesh pour le
      tour payé au token ; un agent critique cède à un harnais de repli qui a
      de la marge (L242b (3)).

    Les comptes d'un autre hôte ne sont pas lisibles ici (leurs journaux sont
    sur son disque) : pour un agent distant, seule la pause annoncée est
    jugée, rafraîchie à chaque sondage de son exécuteur. Sans comptes déclarés,
    de même : c'est la pause annoncée qui tranche.
    """
    from . import budget as budget_mod
    from . import handoff

    if handoff.paused(row):
        return (row.get("status_text") or "")[len(budget_mod.PAUSE_PREFIX):].strip()
    host = getattr(cfg, "host", None) if cfg is not None else None
    if not host or (row.get("host") or "") != host:
        return ""
    harness = row.get("harness") or ""
    try:
        items = accounts.profiles(cfg, harness)
    except accounts.AccountError as exc:
        return "invalid account configuration: %s" % exc
    if not items:
        return ""
    now = time.time() if now is None else float(now)
    limites = budget_mod.current(cfg, db)
    critical = _is_critical(cfg, db, name, row)
    paced = limites.per_hour > 0
    margin = not critical
    environ = _declared_environ(cfg)
    try:
        reason = _own_hold(cfg, db, name, harness, items, limites, now=now,
                           critical=critical, paced=paced, margin=margin,
                           environ=environ)
        if reason and critical and _fallback_available(cfg, db, name, harness, limites,
                                                       now=now, environ=environ):
            return ""
    except db_mod.Unavailable:
        raise
    except db_mod.DbError as exc:
        # état des comptes illisible : le tour ne partirait pas (fail-closed),
        # comme `Runner.budget_ok` (« account state unavailable »)
        return "account state unavailable (%s)" % db_mod.explain(exc)
    return reason


def _declared_environ(cfg) -> dict:
    """L'environnement du processus, complété des clés DÉCLARÉES
    (`api_key_env`) : leur présence dans ce shell n'est pas ce que juge une
    attribution — une variable vide ici est tenue pour déclarée, c'est
    l'exécuteur qui la porte.

    L'exécuteur, sur le même hôte, porte la clé au lancement du tour
    (`accounts.launch_env`) ; un problème d'authentification réel se lit
    `auth_blocked` sur la ligne de l'agent, pas ici. Sans ce complément, un
    `ameesh work assign` lancé d'un shell sans la clé refuserait tout agent
    d'un hôte à comptes `api_key_env` (« missing key variable ») — un faux
    positif du même genre que la tenue qu'on veut voir.
    """
    environ = dict(os.environ)
    try:
        declared = accounts.parse(getattr(cfg, "accounts", None) or {})
    except accounts.AccountError:
        return environ
    for items in declared.values():
        for profile in items:
            if profile.key_env and not environ.get(profile.key_env):
                environ[profile.key_env] = "declared"
    return environ


def _own_hold(cfg, db, name: str, harness: str, items: list, limites, *, now: float,
              critical: bool, paced: bool, margin: bool, environ=None) -> str:
    """Raison de tenue sur le harnais de l'agent, '' si un tour peut partir.

    Réplique la décision de `Runner.budget_reason` sans exécuteur : le compte
    (ou la pause), puis le plafond payé au token restant."""
    book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db, tools={name: harness},
                             **limites.book_kwargs())
    choice = accounts.choose(db, cfg.host, harness, items, book, now=now, agent=name,
                             environ=environ, simulate=True, critical=critical,
                             paced=paced, margin=margin)
    if choice.profile is None:
        return choice.reason
    if choice.auth_blocked:
        return choice.why
    part = getattr(cfg, "noncritical_hourly_share", 0.75) if margin else 1.0
    return book.over(name, pace=False, hourly_share=part)


def _fallback_available(cfg, db, name: str, own: str, limites, *, now: float,
                        environ=None) -> bool:
    """Un harnais de repli peut-il porter le tour ? (L242b (3) : un agent
    critique en pause sur son harnais tourne sous un autre s'il a de la
    marge). La même décision que `fallback`, sans ses effets (ni journal, ni
    `_borrowed`) : ici on ne fait que juger une attribution."""
    for harness in getattr(cfg, "critical_fallback_harnesses", None) or ():
        if harness == own:
            continue
        try:
            adapters.adapter_for(harness)
        except Exception:  # noqa: BLE001 - harnais absent de l'hôte : suivant
            continue
        try:
            items = accounts.profiles(cfg, harness)
        except accounts.AccountError:
            continue
        book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db, tools={name: harness},
                                 **limites.book_kwargs())
        try:
            if items:
                choice = accounts.choose(db, cfg.host, harness, items, book, now=now,
                                         agent=name, environ=environ, simulate=True,
                                         critical=True)
                if choice.profile is None or choice.auth_blocked:
                    continue
            elif book.pace_exceeded(harness):
                continue
            if book.over(name, pace=False):
                continue
        except db_mod.Unavailable:
            raise
        except Exception:  # noqa: BLE001 - état illisible : harnais suivant
            continue
        return True
    return False


def _is_critical(cfg, db, name: str, row: dict) -> bool:
    """`auditor.is_critical` sans exécuteur (rôles du canon, orchestrateurs
    déclarés) : rôles illisibles, l'agent est jugé ordinaire."""
    from . import auditor as auditor_mod
    from . import sous_utilisation

    try:
        canons = auditor_mod.load_all(cfg, strict=False) if cfg is not None else []
        declared = sous_utilisation.declared_orchestrators()
        return auditor_mod.is_critical(cfg, db, name, canons, declared=declared, agent=row)
    except Exception:  # noqa: BLE001 - rôles illisibles : le choix ordinaire
        return False


def rotate_harness(worker, old_harness: str, new_harness: str, session: str) -> bool:
    """L242b (3) : rotation avec résumé au changement de harnais. Le résumé se
    fait sous l'ANCIEN harnais et le compte de la session (sans compte
    déclaré : son environnement par défaut), seul à pouvoir la reprendre."""
    try:
        items = accounts.profiles(worker.cfg, old_harness)
    except accounts.AccountError:
        items = []
    old = None
    if items:
        previous = _session_account(worker)
        old = accounts.by_name(items, previous) if previous else legacy_profile(
            old_harness, items)
    new = getattr(worker, "_account", None)
    new_label = "%s/%s" % (new_harness, new.name if new is not None else "(default)")
    return rotate(worker, old_harness, old, new, session, new_label=new_label,
                  undeclared=not items)


def rotate(worker, harness: str, old, new, session: str, *, new_label: str | None = None,
           undeclared: bool = False) -> bool:
    """Rotation avec résumé de reprise (mécanisme L11) pour une bascule de compte.

    L242b (3) : `new_label` (« codex/secondaire ») dit un changement de
    harnais — `harness` est alors l'ANCIEN, sous lequel se fait le résumé ;
    `undeclared` : l'ancien harnais n'a pas de compte déclaré (le résumé
    tourne sous son environnement par défaut)."""
    switch = new_label is not None
    nom_ancien = old.name if old is not None else ("(default)" if undeclared else "(removed)")
    nom_nouveau = new_label if switch else new.name
    _log("[%s] %s %s: session %s (account %s) cannot be resumed under %s — "
         "rotation with summary" % (worker.name, "harness switch from" if switch
                                    else "account switch", harness, session, nom_ancien,
                                    nom_nouveau))
    resume = ""
    possible = (old is not None and not accounts.check(old)) or (undeclared and old is None)
    if possible and old is not None:
        # L39 : une jauge saturée de l'ancien compte ferait échouer le résumé ;
        # L226b : ni compte en échec d'authentification, ni plafond fixe
        # dépassé, ni overage — dans le doute, pas d'appel de modèle (brief
        # déterministe)
        try:
            book = cost_mod.CostBook(state_dir=worker.cfg.state_dir, db=None,
                                     tools={worker.name: harness})
            possible, raison = summary_possible(
                old, book, accounts.profiles(worker.cfg, harness),
                sick=auth_failure.failing(worker.db, worker.cfg.host, harness))
        except Exception as exc:
            possible, raison = False, "state of the old account unreadable (%s)" % exc
        if not possible:
            _log("[%s] no summary under account %s: %s" % (worker.name, nom_ancien, raison))
    if possible:
        # #389 r1 : le résumé est un appel de modèle sous l'ANCIEN harnais —
        # payé au token, il tient dans le plafond du mesh comme un tour
        raison = paid_cap(worker, harness)
        if raison:
            possible = False
            _log("[%s] no summary under %s/%s: %s" % (worker.name, harness, nom_ancien,
                                                      raison))

    if possible:
        # le résumé se fait sous l'ANCIEN compte (et l'ancien harnais), qui
        # seul peut reprendre la session
        worker._account_override = old
        if switch:
            worker._harness_override = harness
        try:
            if worker.run_turn({"kind": "prompt", "prompt": adapters.SUMMARY_PROMPT,
                                "ids": []}):
                resume = worker.turn_summary()  # L218 : entier, borné
        finally:
            worker._account_override = None
            worker._harness_override = None
    sans_resume = not resume
    note = resume
    if sans_resume:
        note = FALLBACK_RESUME % (session, nom_ancien, nom_nouveau)
        # L39 (0030) : ancien compte inutilisable ou résumé en échec — brief de
        # reprise DÉTERMINISTE (registre, lots, fil, courrier, transcript), sans
        # appel de modèle ; le fil n'en garde que la mention.
        resume = note
        try:
            from . import reprise
            resume = note + "\n\n" + reprise.deterministic_brief(
                worker.cfg, worker.db, registry.get(worker.db, worker.name) or worker.agent,
                session=session, account=old,
                reason="%s %s: %s → %s" % ("harness switch from" if switch
                                           else "account switch", harness, nom_ancien,
                                           nom_nouveau),
                consigne="This turn's instruction follows this brief: resume your work "
                         "from there, and end each turn with a clear status.")
        except Exception as exc:  # un brief manquant ne bloque jamais la rotation
            _log("[%s] deterministic resume brief unavailable (%s)" % (worker.name, exc))
    worker._session_history(session, note)
    # le cadre de reprise dit qui a écrit le bloc : l'agent, ou ameesh (L39) ;
    # gardé sur disque avant l'oubli (#389 r1 : une panne ne le perd pas)
    if not worker._resume_keep(resume, "ameesh" if sans_resume else "agent"):
        _log("[%s] switch rotation cancelled: resume summary not kept on disk" % worker.name)
        return False
    if not registry.clear_session(worker.db, worker.name, worker.runner.runner_id,
                                  worker.epoch):
        worker._resume_drop()
        _log("[%s] switch rotation cancelled: lease lost before clearing" % worker.name)
        return False
    try:
        os.unlink(worker._path("session"))
    except OSError:
        pass
    worker.agent = registry.get(worker.db, worker.name) or worker.agent
    worker.session_turns = 0
    worker.session_tokens = 0.0
    worker._compte_session = None
    if switch:
        worker._fil_note(
            "Harness switch: session %s, opened under %s/%s, cannot be resumed under %s "
            "(L242b: critical agent fallback, or back to its own harness). Rotation with "
            "resume summary (L11)%s; the old id is kept in `session-history.jsonl`.\n\n%s"
            % (session, harness, nom_ancien, nom_nouveau,
               " — summary unavailable, resuming from a deterministic brief (L39)"
               if sans_resume else "", note),
            meta={"action": "harnais", "session": session, "continuite": "rotation"})
        return True
    worker._fil_note(
        "Account switch %s: session %s, opened under account %s, cannot be "
        "resumed under account %s (separate session storage). Rotation with resume "
        "summary (L11)%s; the old id is kept in `session-history.jsonl`.\n\n%s"
        % (harness, session, nom_ancien, new.name,
           " — summary unavailable, resuming from a deterministic brief (L39)"
           if sans_resume else "", note),
        meta={"action": "compte", "session": session, "continuite": "rotation"})
    return True


# --------------------------------------------------------------------------
# attach (session interactive)
# --------------------------------------------------------------------------

def attach_profile(cfg, db, harness: str):
    """(ok, compte) pour `ameesh attach` : le compte actif, vérifié."""
    try:
        items = accounts.profiles(cfg, harness)
    except accounts.AccountError as exc:
        print("attach: invalid account configuration: %s" % exc, file=sys.stderr)
        return False, None
    if not items:
        return True, None
    try:
        profile = accounts.current(db, cfg.host, harness, items)
    except db_mod.DbError as exc:
        print("attach: account state unavailable: %s" % db_mod.explain(exc),
              file=sys.stderr)
        return False, None
    problems = accounts.check(profile)
    if problems:
        print("attach: account %s/%s unusable: %s"
              % (harness, profile.name, "; ".join(problems)), file=sys.stderr)
        return False, None
    return True, profile


def apply_env_attach(env: dict, profile, cfg=None) -> bool:
    """Même nettoyage et même validation au lancement que pour un tour."""
    try:
        declared = accounts.parse(getattr(cfg, "accounts", None) or {}) if cfg else {}
        if declared:
            accounts.launch_env(env, declared, profile)
        elif profile is not None:
            accounts.apply_env(env, profile)
        return True
    except accounts.AccountError as exc:
        print("attach: %s" % exc, file=sys.stderr)
        return False
