# SPDX-License-Identifier: AGPL-3.0-only
"""Les comptes multiples côté exécuteur (lot L30, décision 0027).

Points d'accroche de `runner.AgentWorker`, regroupés ici pour que l'exécuteur
ne porte que des appels d'une ligne :

* `choose`     — avant chaque tour, dans la garde de budget : choisit le compte
                 (bascule, retour au primaire, ou pause si tous sont au seuil) ;
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

from . import accounts, adapters, cost as cost_mod, db as db_mod, registry

#: compte indisponible (configuration ou base) : le tour ne part pas
INVALID = object()

#: reprise quand le résumé n'a pas pu être produit sous l'ancien compte
FALLBACK_RESUME = (
    "Reprise après une bascule de compte : la session précédente (%s), ouverte sous "
    "le compte %s, ne peut pas être reprise sous le compte %s et n'a pas pu être "
    "résumée. Relis le fil de ton projet et l'état de ton lot avant de continuer."
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
    quand même, mais on ne s'arrête jamais — tous au seuil, on reste sur le
    compte actif.
    """
    harness = _harness(worker)
    try:
        items = accounts.profiles(worker.cfg, harness)
    except accounts.AccountError as exc:
        return "configuration des comptes invalide : %s" % exc if pause else None
    if not items:
        return None
    if book is None:
        book = cost_mod.CostBook(state_dir=worker.cfg.state_dir, db=worker.db,
                                 tools={worker.name: harness})
    try:
        choice = accounts.choose(worker.db, worker.cfg.host, harness, items, book,
                                 agent=worker.name)
    except db_mod.Unavailable:
        # Base injoignable (L72) : ce n'est ni une pause budget ni un problème
        # de migration — l'exécuteur suspend les tours et réessaie, avec son
        # propre statut « base injoignable », levé au retour de la base.
        raise
    except db_mod.DbError as exc:
        # sans état des comptes, on ne sait pas quel compte est sous le seuil
        return ("état des comptes indisponible (%s)" % db_mod.explain(exc)
                if pause else None)
    if choice.switched:
        journal(worker, harness, choice.switched)
    if choice.profile is None:
        if pause:
            return choice.reason
        worker._account = choice.active
        return None
    worker._account = choice.profile
    return ""


def journal(worker, harness: str, switched: dict) -> None:
    """Une bascule = une ligne de journal + une entrée dans le fil de l'équipe.

    La ligne `account_switches` est déjà écrite (même transaction que le
    changement de compte) ; ici, la trace lisible.
    """
    if switched["kind"] == "retour":
        texte = ("Retour au compte %s pour %s (%s) : les tours suivants quittent le "
                 "compte %s." % (switched["to"], harness, switched["reason"], switched["from"]))
    else:
        texte = ("Bascule de compte %s : %s → %s (%s). Les tours suivants utilisent le "
                 "compte %s ; retour automatique au primaire après la remise à zéro de "
                 "sa fenêtre." % (harness, switched["from"], switched["to"],
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
        worker._account_error = "configuration des comptes invalide : %s" % exc
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
        worker._account_error = "état des comptes indisponible : %s" % db_mod.explain(exc)
        return INVALID


def apply(worker, env: dict, compte) -> bool:
    """Environnement du compte pour ce tour ; faux (tour remis en attente) sinon.

    Appelé à CHAQUE lancement, garde de budget active ou non : les variables
    d'identifiants des comptes non choisis sont retirées, puis le profil choisi
    est validé et appliqué (`accounts.launch_env`). Sans comptes déclarés sur
    l'hôte, rien ne change.
    """
    if compte is INVALID:
        message = getattr(worker, "_account_error", "") or "compte indisponible"
        _log("[%s] %s : tour non lancé" % (worker.name, message))
        worker.fail_turn("compte indisponible", message)
        return False
    try:
        declared = accounts.parse(getattr(worker.cfg, "accounts", None) or {})
        if declared:
            accounts.launch_env(env, declared, compte)
    except accounts.AccountError as exc:
        _log("[%s] %s : tour non lancé" % (worker.name, exc))
        worker.fail_turn("compte inutilisable", str(exc))
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
        _log("[%s] marqueur de compte non écrit (%s)" % (worker.name, exc))
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
        # session d'avant L30 : ouverte avec le dossier par défaut du harnais
        old = accounts.Profile(harness=harness, name="(défaut)")
        if old.home() == new.home():
            return
    else:
        old = accounts.by_name(items, previous)
    if accounts.session_portable(harness, old, new):
        texte = ("Bascule de compte %s : la session %s, ouverte sous le compte %s, est "
                 "reprise telle quelle sous le compte %s (stockage de sessions partagé)."
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
            _log("[%s] compte de session non noté (%s)" % (worker.name, exc))
        return
    if not worker.peek():
        return
    rotate(worker, harness, old, new, session)


def summary_possible(old, book, items, now: float | None = None) -> tuple[bool, str]:
    """L'ancien compte peut-il encore porter le tour de résumé d'une rotation ?
    (L39, décision 0030) `(vrai, "")` ou `(faux, raison)`.

    Non s'il est retiré de la configuration, si son profil est inutilisable
    (`accounts.check` : dossier, identifiants, clé) ou si l'une de ses jauges
    est SATURÉE (100 %, fenêtre non remise à zéro) : le fournisseur refuserait
    le tour. Un compte seulement « au seuil » de rythme (garde 0019) reste
    utilisable pour un résumé — c'est la règle de 0027.
    """
    if old is None:
        return False, "compte retiré de la configuration"
    problems = accounts.check(old)
    if problems:
        return False, "; ".join(problems)
    now = time.time() if now is None else now
    try:
        gauges = accounts.gauges_of(book, old, items) if book is not None else []
    except Exception:
        gauges = []
    for gauge in gauges:
        if gauge.reset_passed(now):
            continue
        if float(gauge.used or 0.0) >= 1.0:
            return False, "forfait %s %s saturé (%.0f %%)" % (
                old.harness, gauge.key, float(gauge.used) * 100)
    return True, ""


def rotate(worker, harness: str, old, new, session: str) -> bool:
    """Rotation avec résumé de reprise (mécanisme L11) pour une bascule de compte."""
    nom_ancien = old.name if old is not None else "(retiré)"
    _log("[%s] bascule de compte %s : session %s (compte %s) non reprenable sous %s — "
         "rotation avec résumé" % (worker.name, harness, session, nom_ancien, new.name))
    resume = ""
    possible = old is not None and not accounts.check(old)
    if possible:
        # L39 : une jauge saturée de l'ancien compte ferait échouer le résumé
        try:
            book = cost_mod.CostBook(state_dir=worker.cfg.state_dir, db=None,
                                     tools={worker.name: harness})
            possible, _raison = summary_possible(old, book,
                                                 accounts.profiles(worker.cfg, harness))
        except Exception:
            possible = True
    if possible:
        # le résumé se fait sous l'ANCIEN compte, qui seul peut reprendre la session
        worker._account_override = old
        try:
            if worker.run_turn({"kind": "prompt", "prompt": adapters.SUMMARY_PROMPT,
                                "ids": []}):
                resume = (worker.last_output or "").strip()
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
                reason="bascule de compte %s : %s → %s" % (harness, nom_ancien, new.name),
                consigne="La consigne de ce tour suit ce brief : reprends ton travail à "
                         "partir de là, et termine chaque tour par un état clair.")
        except Exception as exc:  # un brief manquant ne bloque jamais la rotation
            _log("[%s] brief de reprise déterministe indisponible (%s)" % (worker.name, exc))
    worker._session_history(session, note)
    if not registry.clear_session(worker.db, worker.name, worker.runner.runner_id,
                                  worker.epoch):
        _log("[%s] rotation de bascule annulée : bail perdu avant l'effacement" % worker.name)
        return False
    worker.resume_summary = resume
    try:
        os.unlink(worker._path("session"))
    except OSError:
        pass
    worker.agent = registry.get(worker.db, worker.name) or worker.agent
    worker.session_turns = 0
    worker.session_tokens = 0.0
    worker._compte_session = None
    worker._fil_note(
        "Bascule de compte %s : la session %s, ouverte sous le compte %s, ne peut pas être "
        "reprise sous le compte %s (stockage de sessions distinct). Rotation avec résumé "
        "de reprise (L11)%s ; l'ancien id est conservé dans `session-history.jsonl`.\n\n%s"
        % (harness, session, nom_ancien, new.name,
           " — résumé indisponible, reprise sur un brief déterministe (L39)"
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
        print("attach : configuration des comptes invalide : %s" % exc, file=sys.stderr)
        return False, None
    if not items:
        return True, None
    try:
        profile = accounts.current(db, cfg.host, harness, items)
    except db_mod.DbError as exc:
        print("attach : état des comptes indisponible : %s" % db_mod.explain(exc),
              file=sys.stderr)
        return False, None
    problems = accounts.check(profile)
    if problems:
        print("attach : compte %s/%s inutilisable : %s"
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
        print("attach : %s" % exc, file=sys.stderr)
        return False
