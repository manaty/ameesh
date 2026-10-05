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
    except db_mod.DbError as exc:
        # sans état des comptes, on ne sait pas quel compte est sous le seuil
        return ("état des comptes indisponible (%s) : « ameesh migrate » ?"
                % " ".join(str(exc).split())[:160]) if pause else None
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
    except db_mod.DbError as exc:
        worker._account_error = "état des comptes indisponible : %s" % (
            " ".join(str(exc).split())[:160])
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


def _session_account(worker) -> str | None:
    """Le compte sous lequel la session courante a tourné (dernier marqueur).

    Lu une fois dans le flux, puis tenu à jour par `mark` : `pick` passe ici à
    chaque sondage, sans relire le flux.
    """
    if not hasattr(worker, "_compte_session"):
        book = cost_mod.CostBook(state_dir=worker.cfg.state_dir, db=None,
                                 tools={worker.name: _harness(worker)})
        worker._compte_session = book.account_at(worker.name, sys.maxsize, _harness(worker))
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
        return
    if not worker.peek():
        return
    rotate(worker, harness, old, new, session)


def rotate(worker, harness: str, old, new, session: str) -> bool:
    """Rotation avec résumé de reprise (mécanisme L11) pour une bascule de compte."""
    nom_ancien = old.name if old is not None else "(retiré)"
    _log("[%s] bascule de compte %s : session %s (compte %s) non reprenable sous %s — "
         "rotation avec résumé" % (worker.name, harness, session, nom_ancien, new.name))
    resume = ""
    if old is not None and not accounts.check(old):
        # le résumé se fait sous l'ANCIEN compte, qui seul peut reprendre la session
        worker._account_override = old
        try:
            if worker.run_turn({"kind": "prompt", "prompt": adapters.SUMMARY_PROMPT,
                                "ids": []}):
                resume = (worker.last_output or "").strip()
        finally:
            worker._account_override = None
    sans_resume = not resume
    if sans_resume:
        resume = FALLBACK_RESUME % (session, nom_ancien, new.name)
    worker._session_history(session, resume)
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
           " — résumé indisponible, reprise sur le fil" if sans_resume else "", resume),
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
        print("attach : état des comptes indisponible : %s" % exc, file=sys.stderr)
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
