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


def _harness(worker) -> str:
    return (worker.agent or {}).get("harness") or ""


def choose(worker, book=None, *, pause: bool = True) -> str | None:
    """Choisit le compte du prochain tour. None : aucun compte déclaré.

    Rend '' quand un compte est retenu (posé dans `worker._account`), la raison
    de pause sinon. `pause=False` (garde de budget désactivée) : on bascule
    quand même, et le rythme ne met jamais en pause — mais L226b : un compte
    en overage ou au-dessus d'un plafond fixe n'est jamais retenu ; s'il n'en
    reste pas d'autre, la raison est rendue et le tour ne part pas.
    """
    harness = _harness(worker)
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
                                 critical=is_critical(worker))
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
    previous = _session_account(worker)
    if previous is None:
        # session d'avant L30 : ouverte avec le dossier par défaut du harnais
        return accounts.Profile(harness=_harness(worker), name="(default)").home() \
            == compte.home()
    return previous == compte.name


def continuity(worker) -> None:
    """Session ouverte sous un autre compte : reprise, ou rotation avec résumé.

    Reprise quand le harnais le permet (`accounts.session_portable` : même
    stockage de sessions) ; sinon le résumé est produit sous l'ANCIEN compte,
    la session est oubliée, et le tour suivant repart du résumé sous le
    nouveau — et le fil le dit. Seulement s'il y a du travail : on ne résume
    pas une session pour rien.
    """
    harness = _harness(worker)
    try:
        items = accounts.profiles(worker.cfg, harness)
    except accounts.AccountError:
        return
    if not items:
        return
    new = for_turn(worker)
    if new is None or new is INVALID:
        return
    session = worker.current_session()  # même règle que le tour (L26 : oubli par restart)
    if not session:
        return
    previous = _session_account(worker)
    if previous == new.name:
        return
    if previous is None:
        # session d'avant L30 : ouverte avec le dossier par défaut du harnais,
        # résolu en son compte déclaré (L226b : santé et jauges sous son nom)
        old = legacy_profile(harness, items)
        if old.home() == new.home():
            return
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
        return
    if not worker.peek():
        return
    rotate(worker, harness, old, new, session)


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


def rotate(worker, harness: str, old, new, session: str) -> bool:
    """Rotation avec résumé de reprise (mécanisme L11) pour une bascule de compte."""
    nom_ancien = old.name if old is not None else "(removed)"
    _log("[%s] account switch %s: session %s (account %s) cannot be resumed under %s — "
         "rotation with summary" % (worker.name, harness, session, nom_ancien, new.name))
    resume = ""
    possible = old is not None and not accounts.check(old)
    if possible:
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
        # le résumé se fait sous l'ANCIEN compte, qui seul peut reprendre la session
        worker._account_override = old
        try:
            if worker.run_turn({"kind": "prompt", "prompt": adapters.SUMMARY_PROMPT,
                                "ids": []}):
                resume = worker.turn_summary()  # L218 : entier, borné
        finally:
            worker._account_override = None
    sans_resume = not resume
    note = resume
    if sans_resume:
        note = FALLBACK_RESUME % (session, nom_ancien, new.name)
        # L39 (0030) : ancien compte inutilisable ou résumé en échec — brief de
        # reprise DÉTERMINISTE (registre, lots, fil, courrier, transcript), sans
        # appel de modèle ; le fil n'en garde que la mention.
        resume = note
        try:
            from . import reprise
            resume = note + "\n\n" + reprise.deterministic_brief(
                worker.cfg, worker.db, registry.get(worker.db, worker.name) or worker.agent,
                session=session, account=old,
                reason="account switch %s: %s → %s" % (harness, nom_ancien, new.name),
                consigne="This turn's instruction follows this brief: resume your work "
                         "from there, and end each turn with a clear status.")
        except Exception as exc:  # un brief manquant ne bloque jamais la rotation
            _log("[%s] deterministic resume brief unavailable (%s)" % (worker.name, exc))
    worker._session_history(session, note)
    if not registry.clear_session(worker.db, worker.name, worker.runner.runner_id,
                                  worker.epoch):
        _log("[%s] switch rotation cancelled: lease lost before clearing" % worker.name)
        return False
    worker.resume_summary = resume
    # le cadre de reprise dit qui a écrit le bloc : l'agent, ou ameesh (L39)
    worker.resume_origin = "ameesh" if sans_resume else "agent"
    try:
        os.unlink(worker._path("session"))
    except OSError:
        pass
    worker.agent = registry.get(worker.db, worker.name) or worker.agent
    worker.session_turns = 0
    worker.session_tokens = 0.0
    worker._compte_session = None
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
