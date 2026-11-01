# SPDX-License-Identifier: AGPL-3.0-only
"""Rejeu contrefactuel de la politique de contexte — ``ameesh-context-replay/1``.

CTX-05b (#5135) rejoue un corpus ``ameesh-context-corpus/1`` (CTX-05a, #5134),
session après session, à travers les fonctions de décision **livrées** :
``AgentWorker.reread_budget_due`` (CTX-04a, #4970) et
``resident_runner.window_maintenance`` (CTX-04b, #4971). Elles sont appelées,
jamais recopiées : le banc juge le code qui tournera.

Chaque tour rend deux blocs séparés : ``observed``, lu de ``decision`` du
corpus et de rien d'autre, et ``counterfactual``, calculé des mesures et des
réglages candidats sans lire ``decision.observed``. Les réglages sont une
entrée, jamais une valeur par défaut du banc (contrat §7.4) : un réglage absent
reste éteint ou inconnu, et l'inconnue est nommée.

Rien n'est activé : aucune écriture au registre ni au grand livre, aucun réglage
changé. Le rapport s'écrit hors dépôt, en ``0600``.

Limites du rejeu, reprises dans le rapport (:data:`LIMITS`).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from types import SimpleNamespace

from .context_corpus import CorpusError, _inside_worktree, iter_turns
from .context_metrics import DERIVED, MEASURED

SCHEMA = "ameesh-context-replay/1"
RESIDENT_MODE = "resident"
#: seule qualité de mesure que `_window_measure_keep` retient pour la projection
RELIABLE = (MEASURED, DERIVED)

#: réglages candidats admis, en jetons sauf la part de `W` et le minimum de tours
BUDGET_SETTINGS = ("turn_reread_budget", "session_reread_budget")
RESERVE_SETTINGS = ("window_input_reserve", "window_output_reserve")
GUARD_SETTINGS = ("context_max_tokens", "session_max_tokens", "session_min_turns")
SETTINGS = frozenset(BUDGET_SETTINGS + RESERVE_SETTINGS + GUARD_SETTINGS
                     + ("window_maintenance_share", "session_policy"))
MATCH_KEYS = ("harness", "model", "role")
SESSION_POLICIES = ("par-lot", "taille", "jamais")

DECISIONS = ("none", "close", "rotation", "compaction")
#: un tour sans décision contrefactuelle établie : ni « none » ni une valeur
NOT_REPLAYED = None

UNKNOWN_NO_CANDIDATE = "candidate_settings"
UNKNOWN_DIVERGED = "counterfactual_divergence"
UNKNOWN_RESIDENT = "resident_session"
UNKNOWN_SESSION = "session"
UNKNOWN_C_LAST = "C_last"
UNKNOWN_S_HIST = "S_hist"
UNKNOWN_MIN_TURNS = "session_min_turns"
UNKNOWN_SESSION_TURNS = "session_turns"
UNKNOWN_POLICY = "session_policy"
UNKNOWN_R_TURN = "R_turn"
UNKNOWN_R_SESSION_BASE = "R_session_base"
UNKNOWN_COMPACTED = "compacted"

LIMITS = (
    "granularity: the corpus holds end-of-turn measures; a budget is judged "
    "at the end of the turn, not at the call that crossed it",
    "divergence: after a counterfactual compaction or rotation the observed "
    "measures no longer describe the session; its later turns get no decision",
    "latency: the turn-duration cap and the rotation latency threshold are "
    "not replayed",
    "execution gates: _rotation_allowed (pause, budget, auth, credits), host "
    "pressure and the quiet window can delay a due decision; an added rotation "
    "may be a delayed one",
    "harnesses: token conventions differ, so nothing is summed across profiles",
    "alignment: an observed rotation is published on the first turn of the next "
    "session and is compared on the last turn of the session it ended; a "
    "rotation whose next turn lies beyond the corpus window reads as added",
)


class ReplayError(ValueError):
    """Les réglages candidats ou le rapport ne sont pas conformes."""


# --------------------------------------------------------------------------
# réglages candidats
# --------------------------------------------------------------------------

def _finite_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _validate_settings(raw, name: str) -> dict:
    if not isinstance(raw, Mapping):
        raise ReplayError("%s: settings must be an object" % name)
    extra = set(raw) - SETTINGS
    if extra:
        raise ReplayError("%s: unknown settings: %s" % (name, ", ".join(sorted(extra))))
    for key, value in raw.items():
        if key == "session_policy":
            if value not in SESSION_POLICIES:
                raise ReplayError("%s: invalid session_policy" % name)
        elif not _finite_number(value) or value < 0:
            raise ReplayError("%s: %s must be a non-negative number" % (name, key))
    if raw.get("window_maintenance_share", 0) > 1:
        raise ReplayError("%s: window_maintenance_share is a share of W" % name)
    return dict(raw)


def load_candidates(raw) -> list[dict]:
    """Valide ``{"candidates": [{"id", "match": {…}, "settings": {…}}, …]}``.

    La première entrée qui correspond au profil du tour s'applique ; une clé de
    ``match`` absente vaut pour toute valeur.
    """
    entries = raw.get("candidates") if isinstance(raw, Mapping) else None
    if not isinstance(entries, list) or not entries:
        raise ReplayError("candidates: a non-empty list is required")
    out, seen = [], set()
    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or set(entry) - {"id", "match", "settings"}:
            raise ReplayError("candidates[%d]: expected id, match, settings" % position)
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ReplayError("candidates[%d]: a unique id is required" % position)
        seen.add(ident)
        match = entry.get("match", {})
        if not isinstance(match, Mapping) or set(match) - set(MATCH_KEYS):
            raise ReplayError("%s: match accepts %s" % (ident, ", ".join(MATCH_KEYS)))
        out.append({"id": ident, "match": dict(match),
                    "settings": _validate_settings(entry.get("settings"), ident)})
    return out


def _candidate_for(line: Mapping, candidates: Iterable[Mapping]) -> dict | None:
    for candidate in candidates:
        if all(line.get(key) == value for key, value in candidate["match"].items()):
            return candidate
    return None


# --------------------------------------------------------------------------
# les décisions d'un tour
# --------------------------------------------------------------------------

def _value(line: Mapping, name: str):
    return (line.get(name) or {}).get("value")


def _reliable(line: Mapping, name: str):
    """Comme `_window_measure_keep` : absente, partielle ou de repli = inconnue."""
    mesure = line.get(name) or {}
    return mesure.get("value") if mesure.get("quality") in RELIABLE else None


def _setting(settings: Mapping, line: Mapping, name: str):
    """Le réglage candidat, sinon celui observé au tour ; jamais une valeur du banc."""
    if name in settings:
        return settings[name]
    return (line.get("policy") or {}).get(name)


def _session_policy(settings: Mapping, line: Mapping):
    return settings.get("session_policy") or (line.get("policy") or {}).get("session")


def _guards(line: Mapping, settings: Mapping) -> tuple[dict | None, dict | None, list[str]]:
    """(plafond de contexte, garde de taille, inconnues), comme `maybe_rotate`."""
    unknowns: list[str] = []
    policy = _session_policy(settings, line)
    if policy is None:
        unknowns.append(UNKNOWN_POLICY)
    if policy == "jamais":
        return None, None, unknowns
    cap_guard = size_guard = None
    cap = _setting(settings, line, "context_max_tokens")
    if cap and cap > 0:
        # `last_turn_context_reread` : sans relevé, les relus du dernier tour,
        # jamais pour Codex (#2932).
        measure, context = "C_last", _value(line, "C_last")
        if context is None and line.get("harness") != "codex":
            measure, context = "R_turn", _value(line, "R_turn")
        if context is None:
            unknowns.append(UNKNOWN_C_LAST)
        elif context >= cap:
            cap_guard = {"kind": "context_cap", "measure": measure,
                         "value": context, "cap": cap}
    s_hist = _value(line, "S_hist")
    limit = _setting(settings, line, "session_max_tokens")
    if limit and limit > 0:
        turns = (line.get("legacy") or {}).get("session_turns")
        min_turns = settings.get("session_min_turns")
        missing = [nom for nom, connu in (
            (UNKNOWN_S_HIST, s_hist is not None),
            (UNKNOWN_SESSION_TURNS, turns is not None),
            (UNKNOWN_MIN_TURNS, min_turns is not None)) if not connu]
        if missing:
            unknowns.extend(missing)
        elif turns >= min_turns and s_hist >= limit:
            size_guard = {"kind": "session_size", "measure": "S_hist",
                          "value": s_hist, "cap": limit}
    return cap_guard, size_guard, unknowns


def _reread(line: Mapping, settings: Mapping) -> tuple[dict | None, list[str]]:
    """CTX-04a, par `AgentWorker.reread_budget_due`, la fonction livrée."""
    from .runner import AgentWorker
    turn_budget = settings.get("turn_reread_budget", 0)
    session_budget = settings.get("session_reread_budget", 0)
    if not (turn_budget > 0 or session_budget > 0):
        return None, []
    r_turn, r_session = _value(line, "R_turn"), _value(line, "R_session")
    base = _value(line, "R_session_base")
    unknowns = []
    if turn_budget > 0 and r_turn is None:
        unknowns.append(UNKNOWN_R_TURN)
    if session_budget > 0 and base is None:
        unknowns.append(UNKNOWN_R_SESSION_BASE)
    worker = SimpleNamespace(cfg=SimpleNamespace(
        turn_reread_budget=turn_budget, session_reread_budget=session_budget))
    due = AgentWorker.reread_budget_due(
        worker, {"R_turn": r_turn, "R_session": r_session, "base": base})
    if due:
        due = {key: value for key, value in due.items() if key != "reason"}
    return due, unknowns


def _window(line: Mapping, settings: Mapping, *, compact_tried: bool,
            compacted: bool | None) -> dict | None:
    """CTX-04b, par `resident_runner.window_maintenance`, la fonction livrée."""
    from . import resident_runner
    share = settings.get("window_maintenance_share", 0)
    if not share > 0:
        return None
    resident = line.get("mode") == RESIDENT_MODE
    projection = resident_runner.window_maintenance(
        # Un tour qui a compacté ne donne aucune mesure : son dernier appel
        # précède la frontière (#4971). Non publié : même prudence, jamais
        # « pas de compaction » supposé.
        c_last=_reliable(line, "C_last") if compacted is False else None,
        o_last=_reliable(line, "O_last") if compacted is False else None,
        # `W` du profil du modèle effectif seulement ; hors résident, pas de `W`.
        window=_reliable(line, "W") if resident else None,
        share=share,
        input_reserve=settings.get("window_input_reserve", -1),
        output_reserve=settings.get("window_output_reserve", -1),
        compact_supported=line.get("harness") in resident_runner.NATIVE_COMPACT_HARNESSES,
        compact_tried=compact_tried)
    if compacted is None:
        projection["unknowns"] = projection["unknowns"] + [UNKNOWN_COMPACTED]
    if not resident:
        projection["unknowns"] = projection["unknowns"] + [UNKNOWN_RESIDENT]
    if projection["decision"] == resident_runner.WINDOW_RENEW \
            and _session_policy(settings, line) == "jamais":
        projection["decision"] = ""
    return projection


def _decide(line: Mapping, settings: Mapping, *, compact_tried: bool,
            compacted: bool | None) -> dict:
    from . import resident_runner
    cap_guard, size_guard, guard_unknowns = _guards(line, settings)
    reread, reread_unknowns = _reread(line, settings)
    window = _window(line, settings, compact_tried=compact_tried, compacted=compacted)
    unknowns = list(dict.fromkeys(
        guard_unknowns + reread_unknowns + (window["unknowns"] if window else [])))
    window_decision = window["decision"] if window else ""
    # Une clôture ne fait pas tourner : seules les inconnues des gardes et, en
    # résident, de la fenêtre empêchent de dire qu'aucune rotation n'aurait eu lieu.
    rotation_known = not guard_unknowns and not (
        window and line.get("mode") == RESIDENT_MODE and window["unknowns"])
    # L'ordre de `maybe_rotate` : plafond dur, entretien de fenêtre, puis taille.
    guard = cap_guard or (None if window_decision else size_guard)
    if guard:
        decision, cause = "rotation", guard["kind"]
    elif window_decision == resident_runner.COMPACT:
        decision, cause = "compaction", "window_maintenance"
    elif window_decision == resident_runner.WINDOW_RENEW:
        decision, cause = "rotation", "window_maintenance"
    elif reread:
        decision, cause = "close", reread["kind"]
    else:
        decision, cause = "none", None
    return {"decision": decision, "cause": cause, "guard": guard,
            "reread": reread, "window": window, "unknowns": unknowns,
            "rotation_known": rotation_known}


def _not_replayed(unknown: str) -> dict:
    return {"decision": NOT_REPLAYED, "cause": None, "guard": None, "reread": None,
            "window": None, "unknowns": [unknown], "rotation_known": False}


def _compaction_flags(line: Mapping) -> tuple[bool, bool | None]:
    """(une compaction a été demandée, ce tour a compacté ; None : non publié)."""
    decision = line.get("decision") or {}
    block = decision.get("compaction") or {}
    compacted = decision.get("compacted")
    return bool(block.get("requested")) or compacted is True, compacted


def _judgeable(line: Mapping) -> dict:
    """Ce que la mesure du tour permet de juger, quels que soient les réglages."""
    return {
        "C_last": _value(line, "C_last") is not None,
        "window": (line.get("mode") == RESIDENT_MODE
                   and (line.get("decision") or {}).get("compacted") is False
                   and all(_reliable(line, name) is not None
                           for name in ("W", "C_last", "O_last"))),
    }


def _in_session_order(lines: Iterable[Mapping]) -> list[Mapping]:
    """Les tours de chaque session par `session_seq`, les sessions dans l'ordre
    du corpus ; sans rang, l'ordre du fichier."""
    sessions: dict = {}
    for line in lines:
        sessions.setdefault((line.get("agent"), line.get("session")), []).append(line)
    out: list[Mapping] = []
    for group in sessions.values():
        if all(line.get("session_seq") is not None for line in group):
            group = sorted(group, key=lambda line: line["session_seq"])
        out.extend(group)
    return out


# --------------------------------------------------------------------------
# le rejeu
# --------------------------------------------------------------------------

def _observed(line: Mapping) -> dict:
    decision = line.get("decision") or {}
    return {"decision": decision.get("observed"), "cause": decision.get("cause")}


def _rotated_out(lines: Iterable[Mapping]) -> dict:
    """Par session : le motif de la rotation observée qui l'a terminée.

    Elle se lit sur le premier tour de la session suivante (`previous_session`).
    """
    out = {}
    for line in lines:
        rotation = (line.get("decision") or {}).get("rotation")
        if isinstance(rotation, Mapping) and rotation.get("previous_session"):
            out[(line.get("agent"), rotation["previous_session"])] = rotation.get("kind")
    return out


def _aligned(turn: Mapping, rotated_out: Mapping, last_turns: set) -> str | None:
    """La décision observée à la fin de ce tour, pour la comparer au contrefactuel."""
    key = (turn["agent"], turn["session"])
    if key in rotated_out and turn["turn_id"] in last_turns:
        return "rotation"
    observed = turn["observed"]["decision"]
    # une rotation portée par ce tour a terminé la session d'avant, pas celle-ci
    return "none" if observed == "rotation" else observed


def _profile_of(line: Mapping) -> dict:
    return {key: line.get(key) for key in MATCH_KEYS}


def replay_turns(lines: Iterable[Mapping], candidates: list[dict]) -> list[dict]:
    """Rejoue les tours dans l'ordre du corpus, une session à la fois.

    L'état d'une session : une compaction encore en attente de la mesure qui
    tranche, et la divergence après une compaction ou une rotation contrefactuelle.
    """
    states: dict = {}
    out = []
    for line in _in_session_order(lines):
        session = line.get("session")
        key = (line.get("agent"), session)
        state = states.setdefault(key, {"tried": False, "diverged": False})
        candidate = _candidate_for(line, candidates)
        requested, compacted = _compaction_flags(line)
        if candidate is None:
            counterfactual = _not_replayed(UNKNOWN_NO_CANDIDATE)
        elif state["diverged"]:
            counterfactual = _not_replayed(UNKNOWN_DIVERGED)
        else:
            counterfactual = _decide(line, candidate["settings"],
                                     compact_tried=state["tried"], compacted=compacted)
            if session is None:
                counterfactual["unknowns"].append(UNKNOWN_SESSION)
            window = counterfactual["window"]
            if window is not None and window["near"] is False:
                # une mesure retombée sous le seuil rouvre droit à une compaction
                state["tried"] = False
            if counterfactual["decision"] in ("compaction", "rotation") and session:
                state["diverged"] = True
        if requested:
            state["tried"] = True
        out.append({
            "turn_id": line.get("turn_id"),
            "agent": line.get("agent"),
            "session": session,
            "profile": _profile_of(line),
            "candidate": candidate["id"] if candidate else None,
            "judgeable": _judgeable(line),
            "observed": _observed(line),
            "counterfactual": counterfactual,
        })
    return out


def _rotation_gaps(lines: list[Mapping], turns: list[dict]) -> dict:
    """Par session : tournée dans le corpus, tournée par la politique, ou les deux.

    Une rotation n'est « évitée » que démontrée : tous les tours de la session
    rejoués sans inconnue qui puisse la décider. Sinon, la session est `unknown`,
    y compris après une compaction contrefactuelle, dont la suite n'est pas rejouée.
    """
    observed = _rotated_out(lines)
    counterfactual = {
        (turn["agent"], turn["session"]) for turn in turns
        if turn["counterfactual"]["decision"] == "rotation" and turn["session"]}
    undetermined = {
        (turn["agent"], turn["session"]) for turn in turns
        if not turn["counterfactual"]["rotation_known"]}
    gaps: dict = {}
    for turn in turns:
        key = (turn["agent"], turn["session"])
        if key in gaps or not turn["session"] or turn["candidate"] is None:
            continue
        in_observed, in_counterfactual = key in observed, key in counterfactual
        known = in_counterfactual or key not in undetermined
        gaps[key] = {"profile": _profile_key(turn["profile"]),
                     "avoided": known and in_observed and not in_counterfactual,
                     "added": in_counterfactual and not in_observed,
                     "both": in_observed and in_counterfactual,
                     "unknown": not known}
    return gaps


def _profile_key(profile: Mapping) -> str:
    return json.dumps(profile, sort_keys=True)


def synthesis(lines: list[Mapping], turns: list[dict]) -> list[dict]:
    """Une synthèse par profil ; rien n'est additionné entre harnais (R5)."""
    profiles: dict = {}
    rotated_out = _rotated_out(lines)
    last_by_session = {(turn["agent"], turn["session"]): turn["turn_id"] for turn in turns}
    last_turns = set(last_by_session.values())
    for turn in turns:
        key = _profile_key(turn["profile"])
        entry = profiles.setdefault(key, {
            "profile": turn["profile"], "turns": 0, "observed": Counter(),
            "counterfactual": Counter(), "same": 0, "different": 0,
            "not_replayed": 0, "unknowns": Counter(),
            "judgeable": {"C_last": 0, "window": 0},
            "rotations": {"avoided": 0, "added": 0, "both": 0, "unknown": 0}})
        entry["turns"] += 1
        for name, known in turn["judgeable"].items():
            entry["judgeable"][name] += int(known)
        observed, counterfactual = turn["observed"], turn["counterfactual"]
        entry["observed"][str(observed["decision"])] += 1
        entry["unknowns"].update(counterfactual["unknowns"])
        for name in MATCH_KEYS:
            if turn["profile"][name] is None:
                entry["unknowns"]["profile.%s" % name] += 1
        if counterfactual["decision"] is NOT_REPLAYED:
            entry["not_replayed"] += 1
            continue
        entry["counterfactual"][counterfactual["decision"]] += 1
        same = (_aligned(turn, rotated_out, last_turns)
                == counterfactual["decision"])
        entry["same" if same else "different"] += 1
    for gap in _rotation_gaps(lines, turns).values():
        rotations = profiles[gap["profile"]]["rotations"]
        for name in rotations:
            rotations[name] += int(gap[name])
    out = []
    for key in sorted(profiles):
        entry = profiles[key]
        for name in ("observed", "counterfactual", "unknowns"):
            entry[name] = dict(sorted(entry[name].items()))
        out.append(entry)
    return out


def replay(lines: Iterable[Mapping], candidates: list[dict]) -> dict:
    """Le rapport complet ; même entrée, même rapport (aucune horloge, aucun hasard)."""
    lines = list(lines)
    turns = replay_turns(lines, candidates)
    return {"schema": SCHEMA, "turn_count": len(turns), "candidates": candidates,
            "limits": list(LIMITS), "synthesis": synthesis(lines, turns),
            "turns": turns}


def dumps_report(report: Mapping) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n"


def write_report(path: str, report: Mapping, *, force: bool = False) -> str:
    """Écrit le rapport hors dépôt, atomiquement, en ``0600``."""
    target = os.path.abspath(os.path.expanduser(path))
    directory = os.path.dirname(target)
    if _inside_worktree(os.path.realpath(directory)):
        raise ReplayError("the report is written outside a Git worktree")
    if os.path.exists(target) and not force:
        raise ReplayError("%s already exists (use --force to replace it)" % target)
    if not os.path.isdir(directory):
        raise ReplayError("the output directory does not exist: %s" % directory)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".replay-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(dumps_report(report))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


# --------------------------------------------------------------------------
# commande
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh context replay", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", required=True, help="ameesh-context-corpus/1 JSONL")
    parser.add_argument("--candidates", required=True, help="candidate settings, JSON")
    parser.add_argument("--out", required=True,
                        help="output JSON, outside the repository, mode 0600")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        with open(args.candidates, encoding="utf-8") as handle:
            candidates = load_candidates(json.load(handle))
        report = replay(iter_turns(args.corpus), candidates)
        target = write_report(args.out, report, force=args.force)
    except (OSError, ValueError) as exc:  # CorpusError, ReplayError, JSON
        print("ameesh context replay: %s" % exc, file=sys.stderr)
        return 1
    for entry in report["synthesis"]:
        print(json.dumps({key: entry[key] for key in (
            "profile", "turns", "observed", "counterfactual", "same", "different",
            "not_replayed", "judgeable", "rotations", "unknowns")}, ensure_ascii=False, sort_keys=True))
    print("ameesh context replay: %d turn(s) -> %s" % (report["turn_count"], target))
    return 0


__all__ = ["CorpusError", "LIMITS", "ReplayError", "SCHEMA", "build_parser",
           "dumps_report", "load_candidates", "main", "replay", "replay_turns",
           "synthesis", "write_report"]
