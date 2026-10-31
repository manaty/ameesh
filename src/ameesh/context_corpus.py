# SPDX-License-Identifier: AGPL-3.0-only
"""Corpus de rejeu de la politique de contexte — ``ameesh-context-corpus/1``.

CTX-05a (#5134) reconstruit, **en lecture seule**, un corpus de tours depuis
le grand livre des coûts (``turn_costs``) et le journal des événements
(``turn.context`` publié par CTX-02b, #4870). Le corpus sert au rejeu
contrefactuel de CTX-05b : une ligne par tour, compteurs seuls, aucune
décision n'est prise ici.

Ce que le corpus ne contient jamais : prompt, résultat d'outil, texte de
courrier, secret, chemin privé. Le constructeur ne recopie que les champs
d'une liste blanche ; les mesures absentes restent ``None`` avec leur raison,
jamais ``0`` (contrat ``ameesh-context/1`` §2.4).

Le corpus réel n'est pas versionné dans le dépôt : :func:`write_corpus` refuse
un chemin situé dans un arbre Git et écrit le fichier en ``0600``. Les cinq
reproductions R1 à R5 de l'étude CTX-01 vivent, elles, en fixtures versionnées
``tests/fixtures/ctx05/`` ; elles se relisent avec :func:`read_corpus`.

Le schéma est documenté dans ``docs/design/ctx-01-compteurs-contexte.md`` §8.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from . import context_metrics, context_profile, storage
from .context_metrics import (DERIVED, FALLBACK, MEASURED, PARTIAL, UNKNOWN,
                              QUALITIES)

SCHEMA = "ameesh-context-corpus/1"
SOURCE_SCHEMA = context_metrics.SCHEMA
DEFAULT_DAYS = 1.0
DEFAULT_BATCH = 1000
#: la lecture se fait par lots et avance par curseur jusqu'à la fin de la
#: fenêtre : aucune troncature silencieuse. Une fenêtre très large reste
#: bornée par le temps et le harnais demandés.
MAX_BATCH = 100_000

#: qualités admises pour le modèle effectif (le contrat nomme `requested`)
MODEL_QUALITIES = tuple(QUALITIES) + ("requested",)

#: raisons propres au corpus ; les raisons de mesure viennent de
#: `context_metrics` et restent stables.
REASON_NOT_PUBLISHED = "not_published"
REASON_PROJECTION_NOT_PUBLISHED = "projection_not_published"
REASON_PROJECTION_UNKNOWN = "projection_unknown"
REASON_WINDOW_UNKNOWN = "window_unknown"
REASON_WINDOW_PROVENANCE_UNKNOWN = "window_provenance_unknown"
REASON_TURN_ID_MISSING = "turn_id_missing"
REASON_LEDGER_ROW_MISSING = "ledger_row_missing"
REASON_LEDGER_TURN_AMBIGUOUS = "ledger_turn_ambiguous"
REASON_SESSION_TOKENS_MISMATCH = "session_tokens_mismatch"
REASON_COMPACTION_REQUESTED = "compaction_requested"
REASON_POST_TOKENS_UNKNOWN = "post_tokens_unknown"
REASON_EVENT_NOT_OBJECT = "event_not_an_object"
REASON_EVENT_SCHEMA_MISMATCH = "event_schema_mismatch"
REASON_EVENT_AGENT_MISSING = "event_agent_missing"
REASON_QUALITY_UNKNOWN = "quality_unknown"

#: liste blanche du schéma, vérifiée à la lecture comme à l'écriture
LINE_KEYS = frozenset({
    "schema", "turn_id", "agent", "started_ts", "ended_ts", "duration_s",
    "harness", "model", "model_quality", "mode", "role", "session",
    "W", "auxiliary_models",
    "C_last", "O_last", "C_next_input", "C_next", "R_turn", "R_session",
    "O", "S_hist", "cache_read_tokens", "cache_write_tokens",
    "decision", "policy", "legacy", "quality", "reasons", "source",
})
REQUIRED_LINE_KEYS = frozenset({
    "schema", "turn_id", "agent", "harness", "model", "model_quality",
    "mode", "role", "session", "W", "auxiliary_models", "C_last", "O_last",
    "C_next_input", "C_next", "R_turn", "R_session", "O", "S_hist",
    "cache_read_tokens", "cache_write_tokens", "decision", "policy", "legacy",
    "quality", "reasons", "source",
})
MEASURE_KEYS = frozenset({"value", "quality", "reasons", "granularity", "source"})
CORE_MEASURES = ("C_last", "O_last", "R_turn", "R_session", "O", "S_hist",
                 "cache_read_tokens", "cache_write_tokens")
ALL_MEASURES = ("W", "C_last", "O_last", "C_next_input", "C_next", "R_turn",
                "R_session", "O", "S_hist", "cache_read_tokens",
                "cache_write_tokens")
DECISION_KEYS = frozenset({"observed", "cause", "close", "rotation",
                           "compaction", "window"})
OBSERVED_DECISIONS = ("none", "close", "rotation", "compaction")
CLOSE_KEYS = frozenset({"kind", "context_tokens", "context_max_tokens",
                        "elapsed_s", "turn_max_seconds", "measure", "value",
                        "budget"})
ROTATION_KEYS = frozenset({"kind", "previous_session"})
COMPACTION_KEYS = frozenset({"pre_tokens", "post_tokens", "quality", "reasons",
                             "trigger", "requested"})
WINDOW_KEYS = frozenset({"C_last", "O_last", "W", "C_next_input", "C_next",
                         "O_reserve_next", "share", "near", "decision",
                         "unknowns"})
POLICY_KEYS = frozenset({"session", "context_max_tokens", "session_max_tokens",
                         "turn_reread_budget", "session_reread_budget",
                         "window_maintenance_share"})
SOURCE_KEYS = frozenset({"event_id", "observation_id", "ledger_id", "schema",
                         "host"})
_OPAQUE_SESSION_RE = re.compile(r"^sha256:[0-9a-f]{32,64}$")
#: un déclencheur de compaction est un jeton, jamais du texte de harnais
_TRIGGER_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")


class CorpusError(ValueError):
    """Le corpus (ou la charge qui le construit) n'est pas conforme."""


# --------------------------------------------------------------------------
# primitives de normalisation
# --------------------------------------------------------------------------

def _text(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _number(value):
    """Un nombre JSON fini, entier quand il l'est ; ``None`` sinon.

    Jamais ``0`` par défaut, jamais ``True`` converti en ``1``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value.is_integer():
            return int(value)
    return value


def _token(value) -> int | None:
    """Un jeton : entier positif ou nul, ``None`` si absent."""
    number = _number(value)
    if number is None:
        return None
    try:
        return max(int(number), 0)
    except (OverflowError, ValueError):
        return None


def _window_token(value) -> int | None:
    """Une fenêtre de modèle : strictement positive, sinon inconnue (§3.1)."""
    token = _token(value)
    return token if token is not None and token > 0 else None


def _trigger(value) -> str | None:
    """Un déclencheur de compaction normalisé ; le texte libre est écarté."""
    text = _text(value)
    if text is None:
        return None
    text = text.strip().lower()
    return text if _TRIGGER_RE.match(text) else None


def _reasons(values) -> list[str]:
    out: list[str] = []
    if not isinstance(values, (list, tuple, set, frozenset)):
        return out
    if isinstance(values, (set, frozenset)):
        values = sorted(values)
    for value in values:
        value = _text(value)
        if value and value not in out:
            out.append(value)
    return out


def opaque_session(agent: str | None, session: str | None) -> str | None:
    """Une session opaque et stable, sans le texte de l'identifiant.

    Le hachage lie l'agent à la session : deux agents qui réutilisent le même
    identifiant de session ne se confondent pas, et le corpus ne porte jamais
    l'identifiant brut.
    """
    session = _text(session)
    if session is None:
        return None
    digest = sha256(
        (SCHEMA + "\0" + str(agent or "") + "\0" + session).encode("utf-8")
    ).hexdigest()
    return "sha256:" + digest[:32]


def measure(value, *, quality: str = UNKNOWN, reasons=(),
            granularity: str = "call", source=None) -> dict:
    """Une mesure publiée au schéma du corpus."""
    value = _token(value)
    quality = quality if quality in QUALITIES else UNKNOWN
    reasons = _reasons(reasons)
    if value is None:
        # Une absence n'est jamais « mesurée » : elle garde sa raison.
        if quality in (MEASURED, DERIVED):
            quality = UNKNOWN
        if not reasons:
            reasons = [REASON_NOT_PUBLISHED]
    elif quality == UNKNOWN:
        # Une valeur présente ne peut pas être « inconnue » : elle est au
        # moins partielle tant que sa qualité n'est pas établie.
        quality, reasons = PARTIAL, _reasons(reasons + [REASON_QUALITY_UNKNOWN])
    return {
        "value": value,
        "quality": quality,
        "reasons": reasons,
        "granularity": _text(granularity) or "unknown",
        "source": _text(source),
    }


def _copy_measure(raw, default_reason: str, *, granularity: str = "call") -> dict:
    """Recopie une mesure ``ameesh-context/1`` sans clé hors contrat."""
    if not isinstance(raw, Mapping):
        return measure(None, quality=UNKNOWN, reasons=(default_reason,),
                       granularity="unknown", source=None)
    value = _token(raw.get("value"))
    quality = raw.get("quality") if raw.get("quality") in QUALITIES else UNKNOWN
    reasons = _reasons(raw.get("reasons"))
    if value is None and not reasons:
        reasons = [default_reason]
    return measure(value, quality=quality, reasons=reasons,
                   granularity=raw.get("granularity") or granularity,
                   source=raw.get("source"))


# --------------------------------------------------------------------------
# lecture du grand livre
# --------------------------------------------------------------------------

class LedgerSession:
    """Les lignes ``turn_costs`` d'une session, dans l'ordre du grand livre.

    ``S_hist`` d'un tour est la somme ``input_tokens + output_tokens`` des
    lignes jusqu'à la sienne incluse : c'est la même mesure que le compteur
    legacy de session (§5.3), reconstruite sans supposer une valeur. Un
    ``turn_id`` ambigu (plusieurs lignes) n'est jamais tranché.
    """

    def __init__(self, rows: Iterable[Mapping] | None = None):
        self.rows = [dict(row) for row in (rows or ()) if isinstance(row, Mapping)]
        self._by_turn: dict[str, list[dict]] = {}
        self._cumulative: dict[int, int] = {}
        total = 0
        for row in self.rows:
            try:
                ident = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            total += max(int(row.get("input_tokens") or 0), 0)
            total += max(int(row.get("output_tokens") or 0), 0)
            self._cumulative[ident] = total
            turn_id = _text(row.get("turn_id"))
            if turn_id:
                self._by_turn.setdefault(turn_id, []).append(row)

    def find(self, turn_id) -> tuple[dict | None, int | None, str | None]:
        """(ligne, ``S_hist``, raison d'absence) pour ce tour."""
        turn_id = _text(turn_id)
        if turn_id is None:
            return None, None, REASON_TURN_ID_MISSING
        found = self._by_turn.get(turn_id, ())
        if not found:
            return None, None, REASON_LEDGER_ROW_MISSING
        if len(found) > 1:
            return None, None, REASON_LEDGER_TURN_AMBIGUOUS
        row = found[0]
        try:
            return row, self._cumulative[int(row["id"])], None
        except (KeyError, TypeError, ValueError):
            return None, None, REASON_LEDGER_ROW_MISSING


def _legacy(payload: Mapping) -> dict:
    raw = payload.get("legacy")
    if not isinstance(raw, Mapping):
        raw = {}
    from .cost import CONTEXT_LEGACY_FIELDS
    return {name: _number(raw.get(name)) for name in CONTEXT_LEGACY_FIELDS}


def _policy(payload: Mapping) -> dict:
    raw = payload.get("policy")
    if not isinstance(raw, Mapping):
        decision = payload.get("decision")
        raw = decision.get("policy") if isinstance(decision, Mapping) \
            and isinstance(decision.get("policy"), Mapping) else {}
    return {
        "session": _text(raw.get("session")),
        "context_max_tokens": _number(raw.get("context_max_tokens")),
        "session_max_tokens": _number(raw.get("session_max_tokens")),
        "turn_reread_budget": _number(raw.get("turn_reread_budget")),
        "session_reread_budget": _number(raw.get("session_reread_budget")),
        "window_maintenance_share": _number(
            raw.get("window_maintenance_share")),
    }


# --------------------------------------------------------------------------
# profil, fenêtre et décision
# --------------------------------------------------------------------------

def _profile(payload: Mapping, context: Mapping) -> Mapping | None:
    profile = context.get("profile")
    return profile if isinstance(profile, Mapping) else None


def _window_measure(payload: Mapping, context: Mapping, decision: Mapping,
                    model: str | None) -> tuple[dict, list[str], str | None]:
    """La fenêtre ``W``, sa provenance et les modèles auxiliaires.

    Priorité au profil ``ameesh-context/1`` publié par CTX-03 ; à défaut, un
    ``model_usage`` brut est résolu par :func:`context_profile.from_model_usage`
    (le modèle auxiliaire en première position ne donne jamais la fenêtre) ;
    à défaut, le ``W`` déjà utilisé par la décision de fenêtre est repris,
    marqué ``fallback`` avec sa provenance inconnue. Jamais une valeur
    supposée.
    """
    profile = _profile(payload, context)
    if profile is not None:
        value = _window_token(profile.get("window_tokens"))
        quality = profile.get("window_quality")
        if quality not in QUALITIES:
            quality = MEASURED if value is not None else UNKNOWN
        reasons = _reasons(profile.get("reasons"))
        if value is None and not reasons:
            reasons = [REASON_WINDOW_UNKNOWN]
        raw_aux = profile.get("auxiliary_models")
        aux = ([name for name in raw_aux if _text(name)]
               if isinstance(raw_aux, (list, tuple, set, frozenset)) else [])
        model_quality = profile.get("model_quality")
        if model_quality not in MODEL_QUALITIES:
            model_quality = None
        return (measure(value, quality=quality, reasons=reasons,
                        granularity="profile",
                        source=profile.get("window_source")),
                aux, model_quality)

    model_usage = context.get("model_usage")
    if not isinstance(model_usage, Mapping) or not model_usage:
        model_usage = payload.get("model_usage")
    if isinstance(model_usage, Mapping) and model_usage:
        observed = context_profile.from_model_usage(
            model_usage, announced=model)
        value = observed.window_tokens
        quality = observed.window_quality
        reasons = list(observed.reasons)
        if value is None and not reasons:
            reasons = [REASON_WINDOW_UNKNOWN]
        aux = [name for name in observed.auxiliary_models if _text(name)] \
            if isinstance(observed.auxiliary_models,
                          (list, tuple, set, frozenset)) else []
        return (measure(value, quality=quality, reasons=reasons,
                        granularity="profile", source=observed.window_source),
                aux,
                observed.model_quality
                if observed.model_quality in MODEL_QUALITIES else None)

    window = decision.get("window") if isinstance(decision, Mapping) else None
    if isinstance(window, Mapping):
        value = _window_token(window.get("W"))
        if value is not None:
            return (measure(value, quality=FALLBACK,
                            reasons=(REASON_WINDOW_PROVENANCE_UNKNOWN,),
                            granularity="profile",
                            source="event:decision.window"), [], None)

    return (measure(None, quality=UNKNOWN, reasons=(REASON_WINDOW_UNKNOWN,),
                    granularity="profile", source=None), [], None)


def _close(raw) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    out = {"kind": _text(raw.get("kind")) or "unknown"}
    for key in ("context_tokens", "context_max_tokens", "elapsed_s",
                "turn_max_seconds", "value", "budget"):
        if key in raw:
            out[key] = _number(raw.get(key))
    if "measure" in raw:
        out["measure"] = _text(raw.get("measure"))
    return out


def _rotation(raw, agent: str) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    out = {"kind": _text(raw.get("kind")) or "unknown"}
    previous = opaque_session(agent, _text(raw.get("old_session")))
    if previous is not None:
        out["previous_session"] = previous
    return out


def _compaction(context: Mapping, window: Mapping | None) -> dict | None:
    raw = context.get("compaction")
    if isinstance(raw, Mapping):
        pre = _token(raw.get("pre"))
        post = _token(raw.get("post"))
        quality = raw.get("quality")
        if quality not in QUALITIES:
            quality = MEASURED if post is not None else (
                PARTIAL if pre is not None else UNKNOWN)
        if post is None and quality in (MEASURED, DERIVED):
            # Un « après » absent ne peut pas être une compaction réussie.
            quality = PARTIAL if pre is not None else UNKNOWN
        reasons = _reasons(raw.get("reasons"))
        if post is None and REASON_POST_TOKENS_UNKNOWN not in reasons:
            reasons.append(REASON_POST_TOKENS_UNKNOWN)
        return {
            "pre_tokens": pre,
            "post_tokens": post,
            "quality": quality,
            "reasons": reasons,
            "trigger": _trigger(raw.get("trigger")),
            "requested": bool(raw.get("requested", False)),
        }
    if isinstance(window, Mapping) and window.get("decision") == "compact":
        # Une compaction DEMANDÉE n'est pas une compaction réussie : l'après
        # reste inconnu tant que le harnais ne le publie pas (contrat §4.2).
        return {
            "pre_tokens": None,
            "post_tokens": None,
            "quality": UNKNOWN,
            "reasons": [REASON_COMPACTION_REQUESTED, REASON_POST_TOKENS_UNKNOWN],
            "trigger": "window_maintenance",
            "requested": True,
        }
    return None


def _window_block(raw) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    raw_unknowns = raw.get("unknowns")
    unknowns = [item.strip() for item in raw_unknowns
                if isinstance(item, str) and item.strip()] \
        if isinstance(raw_unknowns, (list, tuple, set, frozenset)) else []
    near = raw.get("near")
    return {
        "C_last": _token(raw.get("C_last")),
        "O_last": _token(raw.get("O_last")),
        "W": _token(raw.get("W")),
        "C_next_input": _token(raw.get("C_next_input")),
        "C_next": _token(raw.get("C_next")),
        "O_reserve_next": _token(raw.get("O_reserve_next")),
        "share": _number(raw.get("share")),
        "near": near if isinstance(near, bool) else None,
        "decision": _text(raw.get("decision")),
        "unknowns": unknowns,
    }


def _decision(payload: Mapping, context: Mapping) -> dict:
    raw = payload.get("decision")
    if not isinstance(raw, Mapping):
        raw = {}
    agent = _text(payload.get("agent")) or "unknown"
    window = _window_block(raw.get("window"))
    close = _close(raw.get("close"))
    rotation = _rotation(raw.get("rotation"), agent)
    compaction = _compaction(context, window)
    if compaction is not None:
        observed, cause = "compaction", (compaction.get("trigger")
                                         or "compaction")
    elif rotation is not None:
        observed, cause = "rotation", rotation["kind"]
    elif window is not None and window.get("decision") == "window_renew":
        observed, cause = "rotation", "window_maintenance"
    elif close is not None:
        observed, cause = "close", close["kind"]
    else:
        observed, cause = "none", None
    return {
        "observed": observed,
        "cause": cause,
        "close": close,
        "rotation": rotation,
        "compaction": compaction,
        "window": window,
    }


# --------------------------------------------------------------------------
# construction d'une ligne
# --------------------------------------------------------------------------

def _projection(window: Mapping | None, name: str) -> dict:
    if not isinstance(window, Mapping):
        return measure(None, quality=UNKNOWN,
                       reasons=(REASON_PROJECTION_NOT_PUBLISHED,),
                       granularity="call", source="none")
    value = _token(window.get(name))
    if value is None:
        reasons = window.get("unknowns") or (REASON_PROJECTION_UNKNOWN,)
        return measure(None, quality=UNKNOWN, reasons=reasons,
                       granularity="call", source="event:decision.window")
    return measure(value, quality=DERIVED, granularity="call",
                   source="event:decision.window")


def _s_hist(payload: Mapping, ledger: LedgerSession | None, *,
            agent: str, session: str | None) -> tuple[dict, int | None]:
    """``S_hist`` reconstruit du grand livre, ou absent avec sa raison."""
    legacy = payload.get("legacy")
    raw = legacy if isinstance(legacy, Mapping) else {}
    event_value = _token(raw.get("session_tokens"))
    if session is None:
        return (measure(None, quality=UNKNOWN,
                        reasons=(context_metrics.REASON_NO_SESSION,),
                        granularity="session", source="none"), None)
    if ledger is None:
        if event_value is None:
            return (measure(None, quality=UNKNOWN,
                            reasons=(REASON_LEDGER_ROW_MISSING,),
                            granularity="session", source="none"), None)
        return (measure(event_value, quality=MEASURED, granularity="session",
                        source="event:legacy.session_tokens"), None)
    row, cumulative, reason = ledger.find(payload.get("turn_id"))
    if row is None:
        reason = reason or REASON_LEDGER_ROW_MISSING
        if event_value is None:
            return (measure(None, quality=UNKNOWN, reasons=(reason,),
                            granularity="session", source="none"), None)
        return (measure(event_value, quality=FALLBACK, reasons=(reason,),
                        granularity="session",
                        source="event:legacy.session_tokens"), None)
    quality, reasons = MEASURED, []
    if event_value is not None and event_value != cumulative:
        quality = PARTIAL
        reasons = [REASON_SESSION_TOKENS_MISMATCH]
    return (measure(cumulative, quality=quality, reasons=reasons,
                    granularity="session", source="ledger"),
            int(row["id"]))


def corpus_line(payload: Mapping, *, event_id: int | None = None,
                ledger: LedgerSession | None = None) -> dict:
    """Construit une ligne ``ameesh-context-corpus/1`` depuis un événement
    ``turn.context`` et, si fourni, le grand livre de sa session.

    Seuls les champs de la liste blanche sont lus : un prompt, un résultat
    d'outil ou un texte de courrier présent dans la charge ne peut pas entrer
    dans le corpus. Lève :class:`CorpusError` si la charge n'est pas un
    ``ameesh-context/1``.
    """
    if not isinstance(payload, Mapping):
        raise CorpusError("a turn.context payload must be an object")
    if payload.get("schema") != SOURCE_SCHEMA:
        raise CorpusError("unknown or missing schema (expected %s)"
                          % SOURCE_SCHEMA)
    context = payload.get("context")
    if not isinstance(context, Mapping):
        context = {}

    agent = _text(payload.get("agent")) or "unknown"
    session_raw = _text(payload.get("session")) or _text(context.get("session"))
    session = opaque_session(agent, session_raw)
    model = _text(payload.get("model")) or _text(context.get("model"))
    profile = _profile(payload, context)
    if profile is not None:
        # Le modèle effectif du profil CTX-03 prime sur le nom du lancement.
        model = _text(profile.get("model_effective")) or model
    harness_raw = _text(payload.get("harness")) or _text(context.get("harness"))
    harness = context_metrics.convention(harness_raw).harness \
        if harness_raw else "unknown"
    decision_raw = payload.get("decision")
    if not isinstance(decision_raw, Mapping):
        decision_raw = {}
    window_measure, auxiliary_models, profile_model_quality = _window_measure(
        payload, context, decision_raw, model)
    if profile_model_quality in MODEL_QUALITIES:
        model_quality = profile_model_quality
    else:
        model_quality = MEASURED if model else UNKNOWN

    measures_raw = context.get("measures")
    if not isinstance(measures_raw, Mapping):
        measures_raw = {}
    cache_raw = context.get("cache")
    if not isinstance(cache_raw, Mapping):
        cache_raw = {}

    result = {
        "schema": SCHEMA,
        "turn_id": _text(payload.get("turn_id")) or _text(context.get("turn")),
        "agent": agent,
        "started_ts": _number(payload.get("started_ts")),
        "ended_ts": _number(payload.get("ended_ts")),
        "duration_s": None,
        "harness": harness,
        "model": model,
        "model_quality": model_quality,
        "mode": _text(payload.get("mode")) or _text(context.get("mode")),
        "role": _text(payload.get("role")) or _text(context.get("role")),
        "session": session,
        "W": window_measure,
        "auxiliary_models": auxiliary_models,
        "C_last": _copy_measure(
            measures_raw.get("C_last"), context_metrics.REASON_CONTEXT_UNKNOWN),
        "O_last": _copy_measure(
            measures_raw.get("O_last"), context_metrics.REASON_OUTPUT_UNKNOWN),
        "R_turn": _copy_measure(
            measures_raw.get("R_turn"), context_metrics.REASON_NO_MEASURE),
        "R_session": _copy_measure(
            measures_raw.get("R_session"),
            context_metrics.REASON_TURN_NOT_MEASURED),
        "O": _copy_measure(
            measures_raw.get("O"), context_metrics.REASON_NO_MEASURE),
        "cache_read_tokens": _copy_measure(
            cache_raw.get("read_tokens")
            if isinstance(cache_raw, Mapping) else None,
            context_metrics.REASON_CACHE_READ_UNKNOWN),
        "cache_write_tokens": _copy_measure(
            cache_raw.get("write_tokens")
            if isinstance(cache_raw, Mapping) else None,
            context_metrics.REASON_CACHE_WRITE_UNKNOWN),
    }
    started, ended = result["started_ts"], result["ended_ts"]
    if isinstance(started, (int, float)) and isinstance(ended, (int, float)) \
            and ended >= started:
        result["duration_s"] = round(float(ended) - float(started), 3)
    result["C_next_input"] = _projection(
        decision_raw.get("window"), "C_next_input")
    result["C_next"] = _projection(decision_raw.get("window"), "C_next")
    result["decision"] = _decision(payload, context)
    result["policy"] = _policy(payload)
    result["legacy"] = _legacy(payload)
    s_hist, ledger_id = _s_hist(
        payload, ledger, agent=agent, session=session_raw)
    result["S_hist"] = s_hist
    result["source"] = {
        "schema": SOURCE_SCHEMA,
        "event_id": event_id if isinstance(event_id, int)
        and not isinstance(event_id, bool) else None,
        "observation_id": _text(payload.get("observation_id")),
        "ledger_id": ledger_id,
        "host": _text(payload.get("host")),
    }

    core = [result[name] for name in CORE_MEASURES]
    if all(item["value"] is None for item in core):
        quality = UNKNOWN
    elif bool(context.get("partial")) or any(
            item["quality"] in (PARTIAL, UNKNOWN, FALLBACK) for item in core):
        quality = PARTIAL
    else:
        quality = MEASURED
    result["quality"] = quality
    reasons = set(_reasons(context.get("reasons")))
    for item in core:
        reasons.update(item["reasons"])
    result["reasons"] = sorted(reason for reason in reasons if reason)
    validate_line(result)
    return result


# --------------------------------------------------------------------------
# lecture, validation et écriture du corpus
# --------------------------------------------------------------------------

def _mapping(value, name: str) -> dict:
    if not isinstance(value, Mapping):
        raise CorpusError("%s must be an object" % name)
    return dict(value)


def _extra(mapping: Mapping, allowed: frozenset, name: str) -> None:
    extra = set(mapping) - set(allowed)
    if extra:
        raise CorpusError("%s: unexpected key(s): %s"
                          % (name, ", ".join(sorted(extra))))


def _validate_measure(raw, name: str) -> None:
    mapping = _mapping(raw, name)
    _extra(mapping, MEASURE_KEYS, name)
    value = mapping.get("value")
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CorpusError("%s.value must be a non-negative integer or null"
                              % name)
    quality = mapping.get("quality")
    if quality not in QUALITIES:
        raise CorpusError("%s.quality must be one of %s"
                          % (name, ", ".join(QUALITIES)))
    if value is None and quality not in (UNKNOWN, PARTIAL, FALLBACK):
        raise CorpusError("%s: an absent value cannot be %s"
                          % (name, quality))
    if value is not None and quality == UNKNOWN:
        raise CorpusError("%s: a present value cannot be unknown" % name)
    reasons = mapping.get("reasons")
    if not isinstance(reasons, list) or any(not _text(item) for item in reasons):
        raise CorpusError("%s.reasons must be a list of non-empty strings" % name)
    if value is None and not reasons:
        raise CorpusError("%s: an absent value needs a reason" % name)
    if not _text(mapping.get("granularity")):
        raise CorpusError("%s.granularity is required" % name)
    source = mapping.get("source")
    if source is not None and not isinstance(source, str):
        raise CorpusError("%s.source must be a string or null" % name)


def _validate_close(raw, name: str) -> None:
    mapping = _mapping(raw, name)
    _extra(mapping, CLOSE_KEYS, name)
    if not _text(mapping.get("kind")):
        raise CorpusError("%s.kind is required" % name)
    for key in ("context_tokens", "context_max_tokens", "elapsed_s",
                "turn_max_seconds", "value", "budget"):
        if key in mapping:
            value = mapping[key]
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, (int, float))):
                raise CorpusError("%s.%s must be a number or null"
                                  % (name, key))
    if "measure" in mapping and mapping["measure"] is not None \
            and not isinstance(mapping["measure"], str):
        raise CorpusError("%s.measure must be a string or null" % name)


def _validate_rotation(raw, name: str) -> None:
    mapping = _mapping(raw, name)
    _extra(mapping, ROTATION_KEYS, name)
    if not _text(mapping.get("kind")):
        raise CorpusError("%s.kind is required" % name)
    previous = mapping.get("previous_session")
    if previous is not None and not _OPAQUE_SESSION_RE.match(str(previous)):
        raise CorpusError("%s.previous_session must be opaque" % name)


def _validate_compaction(raw, name: str) -> None:
    mapping = _mapping(raw, name)
    _extra(mapping, COMPACTION_KEYS, name)
    for key in ("pre_tokens", "post_tokens"):
        value = mapping.get(key)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, int) or value < 0):
            raise CorpusError("%s.%s must be a non-negative integer or null"
                              % (name, key))
    quality = mapping.get("quality")
    if quality not in QUALITIES:
        raise CorpusError("%s.quality must be one of %s"
                          % (name, ", ".join(QUALITIES)))
    post = mapping.get("post_tokens")
    if post is None and quality in (MEASURED, DERIVED):
        raise CorpusError("%s: a missing after cannot be %s" % (name, quality))
    if post is not None and quality == UNKNOWN:
        raise CorpusError("%s: a measured after cannot be unknown" % name)
    reasons = mapping.get("reasons")
    if not isinstance(reasons, list) or any(not _text(item) for item in reasons):
        raise CorpusError("%s.reasons must be a list of non-empty strings" % name)
    if post is None and not reasons:
        raise CorpusError("%s: an unknown after needs a reason" % name)
    if "requested" in mapping and not isinstance(mapping["requested"], bool):
        raise CorpusError("%s.requested must be a boolean" % name)
    trigger = mapping.get("trigger")
    if trigger is not None and not isinstance(trigger, str):
        raise CorpusError("%s.trigger must be a string or null" % name)


def _validate_window(raw, name: str) -> None:
    mapping = _mapping(raw, name)
    _extra(mapping, WINDOW_KEYS, name)
    for key in ("C_last", "O_last", "W", "C_next_input", "C_next",
                "O_reserve_next"):
        value = mapping.get(key)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, int) or value < 0):
            raise CorpusError("%s.%s must be a non-negative integer or null"
                              % (name, key))
    share = mapping.get("share")
    if share is not None and (isinstance(share, bool)
                              or not isinstance(share, (int, float))):
        raise CorpusError("%s.share must be a number or null" % name)
    near = mapping.get("near")
    if near is not None and not isinstance(near, bool):
        raise CorpusError("%s.near must be a boolean or null" % name)
    decision = mapping.get("decision")
    if decision is not None and not isinstance(decision, str):
        raise CorpusError("%s.decision must be a string or null" % name)
    unknowns = mapping.get("unknowns")
    if not isinstance(unknowns, list) or any(not _text(item) for item in unknowns):
        raise CorpusError("%s.unknowns must be a list of non-empty strings"
                          % name)


def _validate_decision(raw) -> None:
    mapping = _mapping(raw, "decision")
    _extra(mapping, DECISION_KEYS, "decision")
    observed = mapping.get("observed")
    if observed not in OBSERVED_DECISIONS:
        raise CorpusError("decision.observed must be one of %s"
                          % ", ".join(OBSERVED_DECISIONS))
    cause = mapping.get("cause")
    if observed == "none":
        if cause is not None:
            raise CorpusError("decision.cause must be null when observed is none")
    elif not _text(cause):
        raise CorpusError("decision.cause is required for an observed decision")
    for key in ("close", "rotation", "compaction", "window"):
        if key not in mapping:
            raise CorpusError("decision.%s is required" % key)
        if mapping[key] is None:
            continue
        if key == "close":
            _validate_close(mapping[key], "decision.close")
        elif key == "rotation":
            _validate_rotation(mapping[key], "decision.rotation")
        elif key == "compaction":
            _validate_compaction(mapping[key], "decision.compaction")
        else:
            _validate_window(mapping[key], "decision.window")


def _validate_policy(raw) -> None:
    mapping = _mapping(raw, "policy")
    _extra(mapping, POLICY_KEYS, "policy")
    session = mapping.get("session")
    if session is not None and not isinstance(session, str):
        raise CorpusError("policy.session must be a string or null")
    for key in POLICY_KEYS - {"session"}:
        value = mapping.get(key)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, (int, float))):
            raise CorpusError("policy.%s must be a number or null" % key)


def _validate_legacy(raw) -> None:
    from .cost import CONTEXT_LEGACY_FIELDS
    mapping = _mapping(raw, "legacy")
    _extra(mapping, frozenset(CONTEXT_LEGACY_FIELDS), "legacy")
    missing = set(CONTEXT_LEGACY_FIELDS) - set(mapping)
    if missing:
        raise CorpusError("legacy: missing field(s): %s"
                          % ", ".join(sorted(missing)))
    for key, value in mapping.items():
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, (int, float))):
            raise CorpusError("legacy.%s must be a number or null" % key)


def _validate_source(raw) -> None:
    mapping = _mapping(raw, "source")
    _extra(mapping, SOURCE_KEYS, "source")
    if mapping.get("schema") != SOURCE_SCHEMA:
        raise CorpusError("source.schema must be %s" % SOURCE_SCHEMA)
    event_id = mapping.get("event_id")
    if event_id is not None and (isinstance(event_id, bool)
                                 or not isinstance(event_id, int)
                                 or event_id < 0):
        raise CorpusError("source.event_id must be a non-negative integer or null")
    ledger_id = mapping.get("ledger_id")
    if ledger_id is not None and (isinstance(ledger_id, bool)
                                  or not isinstance(ledger_id, int)
                                  or ledger_id < 0):
        raise CorpusError("source.ledger_id must be a non-negative integer or null")
    for key in ("observation_id", "host"):
        value = mapping.get(key)
        if value is not None and not isinstance(value, str):
            raise CorpusError("source.%s must be a string or null" % key)


def validate_line(line) -> dict:
    """Refuse une ligne hors schéma ou hors liste blanche.

    Toute clé inconnue est refusée : le corpus ne doit jamais transporter un
    champ qu'un producteur aurait ajouté (prompt, texte d'outil, secret).
    """
    mapping = _mapping(line, "line")
    _extra(mapping, LINE_KEYS, "line")
    missing = REQUIRED_LINE_KEYS - set(mapping)
    if missing:
        raise CorpusError("line: missing field(s): %s"
                          % ", ".join(sorted(missing)))
    if mapping.get("schema") != SCHEMA:
        raise CorpusError("line: schema must be %s" % SCHEMA)
    turn_id = mapping.get("turn_id")
    if turn_id is not None and not _text(turn_id):
        raise CorpusError("line.turn_id must be a non-empty string or null")
    if not _text(mapping.get("agent")):
        raise CorpusError("line.agent is required")
    if not _text(mapping.get("harness")):
        raise CorpusError("line.harness is required")
    for key in ("started_ts", "ended_ts", "duration_s"):
        if key in mapping:
            value = mapping[key]
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, (int, float))):
                raise CorpusError("line.%s must be a number or null" % key)
    model = mapping.get("model")
    if model is not None and not isinstance(model, str):
        raise CorpusError("line.model must be a string or null")
    if mapping.get("model_quality") not in MODEL_QUALITIES:
        raise CorpusError("line.model_quality must be one of %s"
                          % ", ".join(MODEL_QUALITIES))
    for key in ("mode", "role"):
        value = mapping.get(key)
        if value is not None and not isinstance(value, str):
            raise CorpusError("line.%s must be a string or null" % key)
    session = mapping.get("session")
    if session is not None and not _OPAQUE_SESSION_RE.match(str(session)):
        raise CorpusError("line.session must be opaque (sha256:…)")
    for name in ALL_MEASURES:
        _validate_measure(mapping.get(name), name)
    auxiliary = mapping.get("auxiliary_models")
    if not isinstance(auxiliary, list) or any(not _text(item) for item in auxiliary):
        raise CorpusError("line.auxiliary_models must be a list of strings")
    _validate_decision(mapping.get("decision"))
    _validate_policy(mapping.get("policy"))
    _validate_legacy(mapping.get("legacy"))
    if mapping.get("quality") not in QUALITIES:
        raise CorpusError("line.quality must be one of %s"
                          % ", ".join(QUALITIES))
    reasons = mapping.get("reasons")
    if not isinstance(reasons, list) or any(not _text(item) for item in reasons):
        raise CorpusError("line.reasons must be a list of non-empty strings")
    _validate_source(mapping.get("source"))
    return mapping


def iter_corpus(path: str):
    """Lit un corpus JSONL et valide chaque ligne, une par une."""
    with open(path, encoding="utf-8") as handle:
        for number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CorpusError("%s:%d: invalid JSON: %s"
                                  % (path, number, exc.msg)) from exc
            try:
                yield validate_line(line)
            except CorpusError as exc:
                raise CorpusError("%s:%d: %s" % (path, number, exc)) from exc


def read_corpus(path: str) -> list[dict]:
    """Le corpus complet, validé."""
    return list(iter_corpus(path))


def _inside_worktree(directory: str) -> bool:
    """Le répertoire est-il dans un arbre Git (un dépôt) ?"""
    current = os.path.abspath(directory)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def write_corpus(path: str, lines: Iterable[Mapping], *,
                 force: bool = False) -> str:
    """Écrit le corpus hors dépôt, atomiquement, en ``0600``.

    Refuse un chemin dans un arbre Git (le corpus réel n'est pas versionné) et
    un fichier existant sans ``force``. Les lignes sont validées avant la
    moindre écriture : un corpus partiel n'est jamais laissé derrière une
    erreur.
    """
    target = os.path.abspath(os.path.expanduser(path))
    if _inside_worktree(os.path.realpath(os.path.dirname(target) or ".")):
        raise CorpusError("the corpus is written outside a Git worktree, "
                          "never inside the repository")
    if os.path.exists(target) and not force:
        raise CorpusError("%s already exists (use --force to replace it)" % target)
    payload = [validate_line(line) for line in lines]
    serialized = "".join(
        json.dumps(line, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n"
        for line in payload)
    directory = os.path.dirname(target) or "."
    if not os.path.isdir(directory):
        raise CorpusError("the output directory does not exist: %s" % directory)
    temporary = os.path.join(
        directory, ".%s.%d.%s.tmp" % (os.path.basename(target),
                                      os.getpid(),
                                      uuid.uuid4().hex[:8]))
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        handle = os.fdopen(os.open(temporary, flags, 0o600), "w",
                           encoding="utf-8")
        try:
            with handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise CorpusError("cannot write %s: %s" % (target, exc)) from exc
    return target


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ExportResult:
    """Le résultat d'un export : lignes, fenêtre, harnais et événements écartés."""

    lines: list
    since_ts: float
    until_ts: float
    harness: str | None = None
    sessions: int = 0
    skipped: tuple = ()

    @property
    def turns(self) -> int:
        return len(self.lines)


def _harness_key(harness: str | None) -> str | None:
    text = _text(harness)
    if text is None:
        return None
    normalised = context_metrics.convention(text).harness
    return normalised if normalised != "unknown" else text.lower()


def export_corpus(db, *, since_ts: float, until_ts: float | None = None,
                  harness: str | None = None,
                  batch: int = DEFAULT_BATCH) -> ExportResult:
    """Reconstruit le corpus d'une fenêtre, en lecture seule.

    Les événements ``turn.context`` sont lus par lots (``batch``) jusqu'à
    épuisement de la fenêtre ; le grand livre de chaque session n'est lu
    qu'une fois. Un événement qui n'est pas un ``ameesh-context/1`` complet
    (schéma, agent) est écarté avec sa raison, jamais transformé en ligne
    inventée. Un ``turn_id`` sans ligne de grand livre garde ``S_hist`` du
    legacy de l'événement, marqué ``fallback``.
    """
    until = time.time() if until_ts is None else float(until_ts)
    since = float(since_ts)
    if not math.isfinite(since) or not math.isfinite(until):
        raise CorpusError("the export window must be finite")
    if since >= until:
        raise CorpusError("the export window is empty (since >= until)")
    if not isinstance(batch, int) or isinstance(batch, bool):
        raise CorpusError("batch must be an integer")
    if batch < 1 or batch > MAX_BATCH:
        raise CorpusError("batch must be between 1 and %d" % MAX_BATCH)
    harness_key = _harness_key(harness)
    store = storage.of(db)
    raw_events: list[dict] = []
    after_id = 0
    while True:
        rows = store.events.context_events(
            since, until, batch, after_id=after_id, harness=harness_key)
        if not rows:
            break
        raw_events.extend(rows)
        after_id = int(rows[-1]["id"])
        if len(rows) < batch:
            break

    parsed: list[tuple[dict, Mapping]] = []
    skipped: list[dict] = []
    for row in raw_events:
        data = row.get("data")
        if not isinstance(data, Mapping):
            skipped.append({"event_id": row.get("id"),
                            "reason": REASON_EVENT_NOT_OBJECT})
            continue
        if data.get("schema") != SOURCE_SCHEMA:
            skipped.append({"event_id": row.get("id"),
                            "reason": REASON_EVENT_SCHEMA_MISMATCH})
            continue
        if not _text(data.get("agent")):
            skipped.append({"event_id": row.get("id"),
                            "reason": REASON_EVENT_AGENT_MISSING})
            continue
        parsed.append((row, data))

    ledgers: dict[tuple[str, str], LedgerSession] = {}
    for _row, data in parsed:
        agent = _text(data.get("agent"))
        session = _text(data.get("session"))
        if agent is None or session is None:
            continue
        key = (agent, session)
        if key not in ledgers:
            ledgers[key] = LedgerSession(store.turn_costs.session_rows(*key))

    lines: list[dict] = []
    for row, data in parsed:
        agent = _text(data.get("agent")) or "unknown"
        session = _text(data.get("session"))
        ledger = ledgers.get((agent, session)) if session is not None else None
        lines.append(corpus_line(data, event_id=_token(row.get("id")),
                                 ledger=ledger))
    lines.sort(key=lambda line: (
        line.get("started_ts") if isinstance(line.get("started_ts"),
                                             (int, float)) else 0.0,
        (line.get("source") or {}).get("event_id") or 0))
    return ExportResult(lines=lines, since_ts=since, until_ts=until,
                        harness=harness_key, sessions=len(ledgers),
                        skipped=tuple(skipped))


# --------------------------------------------------------------------------
# ligne de commande
# --------------------------------------------------------------------------

def _time_arg(text: str) -> float:
    raw = str(text).strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected an epoch or an ISO-8601 time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh context corpus", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    export = sub.add_parser(
        "export", help="reconstruct a replay corpus from the turn ledger")
    export.add_argument("--out", required=True,
                        help="output JSONL, outside the repository, mode 0600")
    window = export.add_mutually_exclusive_group()
    window.add_argument("--days", type=float, default=DEFAULT_DAYS,
                        help="window length (default %s day)" % DEFAULT_DAYS)
    window.add_argument("--since", type=_time_arg,
                        help="window start (epoch or ISO-8601)")
    export.add_argument("--until", type=_time_arg,
                        help="window end, exclusive (epoch or ISO-8601; "
                             "default: now)")
    export.add_argument("--harness", help="only this harness")
    export.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                        help="events read per batch (default %d)" % DEFAULT_BATCH)
    export.add_argument("--force", action="store_true",
                        help="replace an existing corpus")
    check = sub.add_parser("check", help="validate a corpus file")
    check.add_argument("file")
    return parser


def main(argv: list[str] | None = None) -> int:
    from . import config as config_mod
    from . import db as db_mod
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if args.command == "check":
        try:
            lines = read_corpus(args.file)
        except (OSError, CorpusError) as exc:
            print("ameesh context corpus: %s" % exc, file=sys.stderr)
            return 1
        print("ameesh context corpus: %d turn(s), schema %s"
              % (len(lines), SCHEMA))
        return 0
    if args.command != "export":
        parser.print_help()
        return 2
    until = args.until if args.until is not None else time.time()
    since = args.since if args.since is not None else until - args.days * 86400.0
    if since >= until:
        print("ameesh context corpus: the window is empty (since >= until)",
              file=sys.stderr)
        return 2
    cfg = config_mod.load()
    try:
        db = db_mod.open_db(cfg)
    except db_mod.DbError as exc:
        print("ameesh context corpus: database unavailable: %s" % exc,
              file=sys.stderr)
        return 1
    try:
        try:
            result = export_corpus(db, since_ts=since, until_ts=until,
                                   harness=args.harness, batch=args.batch)
            target = write_corpus(args.out, result.lines, force=args.force)
        except (CorpusError, OSError, db_mod.DbError) as exc:
            print("ameesh context corpus: %s" % exc, file=sys.stderr)
            return 1
        print("ameesh context corpus: %d turn(s), %d session(s), %d skipped;"
              " %s..%s%s -> %s"
              % (result.turns, result.sessions, len(result.skipped),
                 _iso(result.since_ts), _iso(result.until_ts),
                 " [%s]" % result.harness if result.harness else "",
                 target))
        for item in result.skipped:
            print("  skipped event %s: %s"
                  % (item.get("event_id"), item.get("reason")), file=sys.stderr)
        return 0
    finally:
        db.close()


__all__ = [
    "ALL_MEASURES", "CORE_MEASURES", "CorpusError", "DECISION_KEYS",
    "DEFAULT_BATCH", "ExportResult", "LINE_KEYS", "LedgerSession",
    "MEASURE_KEYS", "MODEL_QUALITIES", "REQUIRED_LINE_KEYS", "SCHEMA",
    "SOURCE_SCHEMA", "build_parser", "corpus_line", "export_corpus",
    "iter_corpus", "main", "measure", "opaque_session", "read_corpus",
    "validate_line", "write_corpus",
]
