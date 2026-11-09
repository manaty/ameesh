# SPDX-License-Identifier: AGPL-3.0-only
"""A11 — journal des intentions et des issues de reprise (#6475).

La boucle d'auto-réparation (décision 0022, étude
``docs/design/etudes/boucle-auto-reparation.md`` §A11) doit mesurer les
reprises **avant** d'en automatiser une seule.  Ce module définit le reçu
commun d'une reprise, sans jamais en déclencher :

* l'**origine** dit qui l'a demandée : ``operator`` (geste humain),
  ``agent`` (geste d'un agent) ou ``automatic`` (relance mécanique) ;
* l'**intention** dit quelle opération déjà autorisée est rejouée
  (``session.resume``, ``turn.retry``, ``role.pass``…) ;
* la **cause stable** et l'**épisode** identifient la panne sans recopier de
  texte : seuls des jetons bornés entrent ici ;
* la **tentative** vient d'un compteur durable, partagé par cause et par
  épisode ; il survit à la relève du détenteur parce qu'il vit en base, pas
  dans la mémoire d'un runner ;
* l'**issue** est séparée de l'intention : ``requested``, ``engaged``,
  ``completed``, ``failed``, ``unknown`` ou ``avoided`` (reprise évitée par
  idempotence, par exemple parce qu'un autre détenteur exécute déjà).

Une entrée est identifiée par une **clé d'idempotence** fournie par
l'appelant (64 hexadécimaux) : reçue deux fois, elle ne crée pas de doublon
et ne rejoue pas la tentative.  Aucune donnée brute (corps, message, pile,
chemin) n'est acceptée ; un champ inconnu est refusé par le lecteur strict.

Ce module est pur : le stockage transactionnel est U5
(``storage/postgres/repair_signals.py``), le rapport U1 lit son résumé.
U14 observe ; U15 seule branchera une reprise bornée sur un chemin autorisé.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: le schéma d'un reçu de reprise
SCHEMA = "repair-retry/1"
#: le schéma du résumé agrégé rendu au rapport U1
SUMMARY_SCHEMA = "repair-retry-summary/1"
#: qui demande la reprise
ORIGINS = ("operator", "agent", "automatic")
#: opérations déjà autorisées et déclarées rejouables (A11).  Une opération
#: nouvelle doit être déclarée ici avant d'être journalisée : le contrat ne
#: devine jamais qu'un effet est rejouable.
INTENTS = ("session.resume", "turn.retry", "harness.relaunch", "role.pass",
           "staging.run", "read.retry")
#: causes stables et fermées : un jeton libre serait un texte, donc refusé.
#: ``unknown`` est une cause honnête, jamais une cause devinée.
CAUSES = ("rate_limit", "admission_refused", "lease_lost", "lease_expired",
          "lease_busy", "lease_short", "no_holder", "other_holder",
          "delegated", "db_error", "crash", "session_lost", "stale_head",
          "command", "command_failed", "source_error", "operator", "agent",
          "unknown")
#: issues terminales : une seule par reçu, jamais réécrite par U14
OUTCOMES = ("completed", "failed", "unknown")

#: champs exacts d'un reçu ``repair-retry/1``, dans l'ordre
FIELDS = ("schema", "idempotency_key", "project", "environment", "origin",
          "intent", "cause", "episode", "attempt", "requested_at",
          "engaged_at", "completed_at", "failed_at", "unknown_at",
          "avoided_at")

#: bornes des jetons : aucun texte libre ne traverse le journal
MAX_PROJECT = 64
MAX_ENVIRONMENT = 32
MAX_EPISODE = 200
MAX_ORIGIN = 16
MAX_INTENT = 64
MAX_CAUSE = 64

_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_EPISODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}$")
_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.[0-9]{1,6})?Z$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

__all__ = ["SCHEMA", "SUMMARY_SCHEMA", "ORIGINS", "INTENTS", "CAUSES",
           "OUTCOMES", "FIELDS", "Retry", "RetryError", "key_of",
           "from_record", "summary", "summary_from_groups",
           "observe_manual_resume", "observe_manual_resume_avoided",
           "RUNNER_INTENT", "runner_retry_key", "runner_retry_avoided_key",
           "observe_runner_retry_requested", "observe_runner_retry_engaged",
           "observe_runner_retry_settled", "observe_runner_retry_avoided"]


class RetryError(ValueError):
    """Une entrée de reprise est refusée explicitement, sans recopier de texte."""


def _bounded(value: Any, field_name: str, limit: int, *,
             required: bool = False) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        raise RetryError("%s: a string is required" % field_name)
    if _CONTROL_RE.search(text):
        raise RetryError("%s: control characters are refused" % field_name)
    if text != text.strip():
        raise RetryError("%s: leading or trailing whitespace is refused"
                         % field_name)
    if required and not text:
        raise RetryError("%s: required" % field_name)
    if len(text) > limit:
        raise RetryError("%s: at most %d characters" % (field_name, limit))
    return text


def _project(value: Any) -> str:
    text = _bounded(value, "project", MAX_PROJECT, required=True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", text):
        raise RetryError("project: expected a project name "
                         "(letters, digits, . _ -)")
    return text


def _environment(value: Any) -> str:
    text = _bounded(value, "environment", MAX_ENVIRONMENT, required=True)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", text):
        raise RetryError("environment: expected a lower-case label "
                         "such as staging or production")
    return text


def _origin(value: Any) -> str:
    if value not in ORIGINS:
        raise RetryError("origin: expected one of %s" % ", ".join(ORIGINS))
    return value


def _intent(value: Any) -> str:
    if value not in INTENTS:
        raise RetryError("intent: expected one of %s" % ", ".join(INTENTS))
    return value


def _cause(value: Any) -> str:
    if value not in CAUSES:
        raise RetryError("cause: expected one of %s" % ", ".join(CAUSES))
    return value


def _episode(value: Any) -> str:
    text = _bounded(value, "episode", MAX_EPISODE, required=True)
    if not _EPISODE_RE.fullmatch(text):
        raise RetryError("episode: expected a bounded identity such as "
                         "agent:name or role:name/pass")
    return text


def _key(value: Any) -> str:
    text = _bounded(value, "idempotency_key", 64, required=True)
    if not _KEY_RE.fullmatch(text):
        raise RetryError("idempotency_key: 64 lowercase hex characters "
                         "are required")
    return text


def _attempt(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RetryError("attempt: a non-negative integer is required")
    return value


def _at(value: Any, field_name: str, *, required: bool = False) -> str:
    """Un instant UTC normalisé ; ``None`` reste « pas encore arrivé »."""
    if value is None or value == "":
        if required:
            raise RetryError("%s: required" % field_name)
        return ""
    if isinstance(value, datetime.datetime):
        when = value
        if when.tzinfo is None:
            raise RetryError("%s: a timezone-aware timestamp is required"
                             % field_name)
        when = when.astimezone(datetime.timezone.utc)
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, bool):
        raise RetryError("%s: an ISO UTC timestamp is required" % field_name)
    if isinstance(value, (int, float)):
        if value < 0:
            raise RetryError("%s: a non-negative epoch is required"
                             % field_name)
        try:
            when = datetime.datetime.fromtimestamp(float(value),
                                                   datetime.timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise RetryError("%s: epoch out of range" % field_name) from exc
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str):
        raise RetryError("%s: an ISO UTC timestamp is required" % field_name)
    text = value.strip()
    if not _AT_RE.fullmatch(text):
        raise RetryError("%s: expected YYYY-MM-DDTHH:MM:SSZ" % field_name)
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetryError("%s: invalid UTC date" % field_name) from exc
    return parsed.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _now_at() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def key_of(*parts: Any) -> str:
    """La clé d'idempotence stable d'une intention de reprise.

    Même suite de parties → même clé ; l'ordre compte.  Les parties sont des
    identifiants bornés, jamais un texte de diagnostic : la clé ne fuit rien.
    """
    material = json.dumps([str(part) for part in parts], ensure_ascii=True,
                          separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class Retry:
    """Un reçu ``repair-retry/1`` validé, sans texte libre.

    L'intention (``requested_at``) et l'effet (``engaged_at`` puis une issue)
    sont deux champs distincts : une reprise demandée n'est jamais comptée
    comme engagée, ni une opération engagée comme achevée.
    """

    project: str
    environment: str
    origin: str
    intent: str
    cause: str
    episode: str
    idempotency_key: str
    requested_at: str
    attempt: int = 0
    engaged_at: str = ""
    completed_at: str = ""
    failed_at: str = ""
    unknown_at: str = ""
    avoided_at: str = ""
    schema: str = field(default=SCHEMA, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", _project(self.project))
        object.__setattr__(self, "environment", _environment(self.environment))
        object.__setattr__(self, "origin", _origin(self.origin))
        object.__setattr__(self, "intent", _intent(self.intent))
        object.__setattr__(self, "cause", _cause(self.cause))
        object.__setattr__(self, "episode", _episode(self.episode))
        object.__setattr__(self, "idempotency_key", _key(self.idempotency_key))
        object.__setattr__(self, "attempt", _attempt(self.attempt))
        object.__setattr__(self, "requested_at",
                           _at(self.requested_at, "requested_at", required=True))
        for name in ("engaged_at", "completed_at", "failed_at", "unknown_at",
                     "avoided_at"):
            object.__setattr__(self, name, _at(getattr(self, name), name))
        terminal = [name for name in ("completed_at", "failed_at", "unknown_at")
                    if getattr(self, name)]
        if len(terminal) > 1:
            raise RetryError("at most one terminal outcome is allowed")
        if self.avoided_at and (self.engaged_at or terminal):
            raise RetryError("an avoided retry carries no effect")
        if self.avoided_at and self.attempt:
            raise RetryError("an avoided retry consumes no attempt")
        if terminal and not self.engaged_at:
            raise RetryError("a terminal outcome requires an engagement")
        if not self.avoided_at and not terminal and not self.engaged_at \
                and not self.attempt:
            raise RetryError("a requested retry consumes at least one attempt")

    @property
    def state(self) -> str:
        """L'état courant, dérivé des seuls horodatages (jamais un doublon)."""
        if self.avoided_at:
            return "avoided"
        if self.completed_at:
            return "completed"
        if self.failed_at:
            return "failed"
        if self.unknown_at:
            return "unknown"
        if self.engaged_at:
            return "engaged"
        return "requested"

    def record(self) -> dict:
        """L'enregistrement JSON, exactement les champs du contrat."""
        values = {name: getattr(self, name)
                  for name in FIELDS if name != "schema"}
        return {"schema": SCHEMA, **values}


def from_record(record: Mapping) -> Retry:
    """Lit un reçu ``repair-retry/1``, strictement.

    Un champ inconnu est refusé, jamais ignoré : le contrat N-1 doit rester
    lisible et un ajout passe d'abord par ce module.
    """
    if not isinstance(record, Mapping):
        raise RetryError("a repair-retry/1 mapping is required")
    unknown = [name for name in record if name not in FIELDS]
    if unknown:
        raise RetryError("unknown field(s): %s"
                         % ", ".join(str(name) for name in sorted(unknown,
                                                                  key=str)))
    missing = [name for name in FIELDS if name not in record]
    if missing:
        raise RetryError("missing field(s): %s" % ", ".join(missing))
    if record["schema"] != SCHEMA:
        raise RetryError("schema: expected %s" % SCHEMA)
    return Retry(project=record["project"],
                 environment=record["environment"],
                 origin=record["origin"], intent=record["intent"],
                 cause=record["cause"], episode=record["episode"],
                 idempotency_key=record["idempotency_key"],
                 attempt=record["attempt"],
                 requested_at=record["requested_at"],
                 engaged_at=record["engaged_at"],
                 completed_at=record["completed_at"],
                 failed_at=record["failed_at"],
                 unknown_at=record["unknown_at"],
                 avoided_at=record["avoided_at"])


def _grouped(records: Iterable) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for item in records:
        retry = item if isinstance(item, Retry) else from_record(item)
        key = (retry.origin, retry.intent, retry.cause)
        group = groups.setdefault(key, {
            "origin": retry.origin, "intent": retry.intent,
            "cause": retry.cause, "requested": 0, "engaged": 0,
            "completed": 0, "failed": 0, "unknown": 0, "avoided": 0,
            "retries": 0, "max_attempt": 0})
        group["requested"] += 1
        for state in ("engaged", "completed", "failed", "unknown", "avoided"):
            if getattr(retry, state + "_at"):
                group[state] += 1
        if retry.attempt > 1:
            group["retries"] += 1
        group["max_attempt"] = max(group["max_attempt"], retry.attempt)
    return [groups[key] for key in sorted(groups)]


def _summary_from_groups(groups: Iterable[Mapping]) -> dict:
    totals = {name: 0 for name in ("requested", "engaged", "completed",
                                   "failed", "unknown", "avoided", "retries")}
    by_origin: dict[str, dict] = {}
    max_attempt = 0
    for row in groups:
        origin = str(row["origin"])
        bucket = by_origin.setdefault(
            origin, {name: 0 for name in totals})
        for name in totals:
            value = int(row.get(name) or 0)
            totals[name] += value
            bucket[name] += value
        max_attempt = max(max_attempt, int(row.get("max_attempt") or 0))
    return {
        "schema": SUMMARY_SCHEMA,
        "groups": [dict(row) for row in groups],
        "by_origin": {name: by_origin[name] for name in sorted(by_origin)},
        "totals": totals,
        "max_attempt": max_attempt,
    }


def summary(records: Iterable) -> dict:
    """Compteurs A11 d'un journal déjà lu : aucune base, aucune invention.

    ``records`` contient des :class:`Retry` ou des enregistrements
    ``repair-retry/1`` (relus strictement).  ``requested`` compte les
    intentions, ``engaged`` les effets commencés, ``completed``/``failed``/
    ``unknown`` les issues, ``avoided`` les reprises évitées par idempotence ;
    ``retries`` ne compte que les tentatives au-delà de la première.
    """
    return summary_from_groups(_grouped(records))


def summary_from_groups(groups: Iterable[Mapping]) -> dict:
    """Assemble le résumé U1 depuis des groupes déjà agrégés par le stockage.

    Le stockage U5 fait l'agrégat SQL (mêmes colonnes que :func:`_grouped`)
    et rend ce résumé ; le rapport U1 ne lit ainsi ni clé ni épisode.
    """
    return _summary_from_groups(groups)


# --------------------------------------------------------------------------
# observation best-effort des reprises existantes (U14, aucun effet nouveau)
# --------------------------------------------------------------------------

_LOG_LIMIT = 160


def _log(log, text):
    if log is not None:
        log(text[:_LOG_LIMIT])


def _store(db):
    from . import storage
    return storage.of(db).repair_signals


def _scope_of(project, environment):
    """Le projet/l'environnement du journal, validés sans deviner."""
    return _project(project), _environment(environment)


def observe_manual_resume(db, *, project, environment, agent, actor, origin,
                          invocation, intent="session.resume",
                          settled="completed", log=None):
    """Journal a manual resume/adopt: intent, engagement and observed issue.

    The authorized operation is applied by the caller: ``completed`` when the
    instruction is in the registry, ``unknown`` when a live holder still has
    to apply it.  The retry U14 measures is that operation; the resumed turn
    itself is the runner's work and is never claimed here.

    ``invocation`` is stable for one command: replaying it stays one attempt,
    while two distinct manual gestures remain two attempts.  Best effort: a
    journal failure never blocks, delays or changes the operation.
    """
    try:
        if settled not in ("completed", "unknown"):
            raise RetryError("settled: expected completed or unknown")
        project, environment = _scope_of(project, environment)
        identity = key_of("session.resume", agent, actor, invocation)
        api = _store(db)
        result = api.retry_begin(
            identity, project=project, environment=environment, origin=origin,
            intent=intent, cause=(origin if origin in ("operator", "agent")
                                  else "unknown"),
            episode="agent:%s" % agent)
        result["settled"] = bool(api.retry_settle(identity, settled))
        return result
    except Exception as exc:  # noqa: BLE001 - observation, jamais un effet
        _log(log, "repair-retry: manual resume not journaled (%s)"
             % type(exc).__name__)
        return None


def observe_manual_resume_avoided(db, *, project, environment, agent, actor,
                                  origin, invocation, cause="other_holder",
                                  intent="session.resume", log=None):
    """Journal a resume/adopt request that idempotence made unnecessary.

    Another holder already runs it (live lease, turn in progress) or the
    registry changed before the write.  No attempt is consumed and the
    refused operation is left untouched; best effort like the intent itself.
    """
    try:
        project, environment = _scope_of(project, environment)
        identity = key_of("session.resume.avoid", agent, actor, invocation)
        return bool(_store(db).retry_avoid(
            identity, project=project, environment=environment, origin=origin,
            intent=intent, cause=cause, episode="agent:%s" % agent))
    except Exception as exc:  # noqa: BLE001 - observation, jamais un effet
        _log(log, "repair-retry: avoided resume not journaled (%s)"
             % type(exc).__name__)
        return None


# --------------------------------------------------------------------------
# relances automatiques du runner (U14 suite, #6487)
# --------------------------------------------------------------------------

#: intention d'une relance automatique : le tour d'une consigne en attente
RUNNER_INTENT = "turn.retry"


def runner_retry_key(project, agent, prompt):
    """La clé d'idempotence d'une consigne en attente, stable entre détenteurs.

    La consigne est l'identité de la relance : deux détenteurs qui observent
    le même état ``pending_prompt`` calculent la même clé, et une consigne
    remise en attente après une interruption (perte de bail, retrait d'hôte,
    préemption) ne rejoue pas la tentative.  Le texte de la consigne n'est
    jamais écrit : seul son condensé entre dans la clé.
    """
    return key_of("runner.turn", project, agent, prompt)


def runner_retry_avoided_key(project, agent, prompt):
    """La clé d'une relance évitée par idempotence (un autre détenteur)."""
    return key_of("runner.turn.avoid", project, agent, prompt)


def observe_runner_retry_requested(db, *, key, project, environment, agent,
                                   cause="unknown", log=None):
    """Journal the demand of an automatic relaunch: the queued instruction.

    The runner reads ``pending_prompt`` (the state is set) before consuming
    it; this is the *requested* intent, with no effect claimed.  The cause is
    ``unknown`` by default: the runner does not know why the instruction was
    queued and the contract forbids guessing it.  Best effort like the manual
    observation: a journal failure never delays or changes admission.
    """
    try:
        project, environment = _scope_of(project, environment)
        return _store(db).retry_request(
            key, project=project, environment=environment, origin="automatic",
            intent=RUNNER_INTENT, cause=cause, episode="agent:%s" % agent)
    except Exception as exc:  # noqa: BLE001 - observation, jamais un effet
        _log(log, "repair-retry: runner relaunch not journaled (%s)"
             % type(exc).__name__)
        return None


def observe_runner_retry_engaged(db, *, key, log=None):
    """Journal that the runner consumed the queued instruction (turn start).

    ``False`` when another holder already engaged the same key (idempotent
    replay); the journal is observation only and never refuses the turn.
    """
    try:
        return bool(_store(db).retry_engage(key))
    except Exception as exc:  # noqa: BLE001 - observation, jamais un effet
        _log(log, "repair-retry: runner engagement not journaled (%s)"
             % type(exc).__name__)
        return None


def observe_runner_retry_settled(db, *, key, outcome, log=None):
    """Journal the issue of a runner relaunch: completed or unknown, once."""
    try:
        if outcome not in OUTCOMES:
            raise RetryError("outcome: expected one of %s" % ", ".join(OUTCOMES))
        return bool(_store(db).retry_settle(key, outcome))
    except Exception as exc:  # noqa: BLE001 - observation, jamais un effet
        _log(log, "repair-retry: runner outcome not journaled (%s)"
             % type(exc).__name__)
        return None


def observe_runner_retry_avoided(db, *, key, project, environment, agent,
                                 cause="other_holder", log=None):
    """Journal a relaunch that idempotence made unnecessary.

    Another holder consumed the same queued instruction first (or the state
    changed before the write): no attempt is consumed, the queued demand is
    left untouched, and the *avoided* entry is separate from the engaged one.
    """
    try:
        project, environment = _scope_of(project, environment)
        return bool(_store(db).retry_avoid(
            key, project=project, environment=environment, origin="automatic",
            intent=RUNNER_INTENT, cause=cause, episode="agent:%s" % agent))
    except Exception as exc:  # noqa: BLE001 - observation, jamais un effet
        _log(log, "repair-retry: avoided runner relaunch not journaled (%s)"
             % type(exc).__name__)
        return None
