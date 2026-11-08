# SPDX-License-Identifier: AGPL-3.0-only
"""A1 — enveloppe de signal de réparation et adaptateurs purs (#6315).

La boucle d'auto-réparation (décision 0022, étude
``docs/design/etudes/boucle-auto-reparation.md``) a besoin d'un **reçu
commun** : toute entrée, quelle que soit sa source, devient une même
enveloppe ``repair-signal/1``.  Ce module la définit et adapte les sources
qu'ameesh possède déjà :

* ``thread``        : une entrée de fil (``fil.Entry``) ;
* ``inbox``         : un message de la boîte (``mail``) ;
* ``github_watch``  : un événement de veille GitHub (``github_watch``) ;
* ``alerts``        : une alerte du relevé ``ameesh alerts``.

**Un signal est une donnée, jamais une instruction ni une autorisation.**
Les adaptateurs ne lisent jamais le texte (corps, titre, détail, pile) : ils
n'en gardent ni copie ni empreinte, seulement une référence bornée vers le
magasin d'origine, où l'accès reste gouverné.  Aucun adaptateur n'ouvre de
lot, ne décide ni n'écrit en base : le journal d'entrée est U5, le triage
U3, le rapport U1.

L'identité est stable : ``(projet, environnement, source, ID natif)`` hachée
une fois.  Relire deux fois la même entrée donne le même identifiant ; seule
l'``occurrence`` grandit.  L'**environnement est obligatoire** : sans lui, un
signal ne peut pas être corrélé à une version déployée (A4) ; il est refusé,
jamais attribué par défaut.

Le lecteur d'enregistrement est strict : un champ inconnu, un schéma
inconnu, une source inconnue ou un identifiant recalculé différent sont
refusés.  C'est le contrat que le stockage N-1 devra préserver.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: le schéma de l'enveloppe, dans le champ ``schema`` de chaque reçu
SCHEMA = "repair-signal/1"
#: le schéma du résumé de compteurs rendu à U1 (``summary``)
SUMMARY_SCHEMA = "repair-signals-summary/1"
#: les sources que ce module adapte ; une source inconnue est refusée
SOURCES = ("thread", "inbox", "github_watch", "alerts")
#: résultat de traitement ; ``pending`` est l'état à la réception
OUTCOMES = ("pending", "lot", "basket", "decision", "ignored", "unknown",
            "refused")
PENDING = "pending"
#: les champs exacts d'un enregistrement ``repair-signal/1``, dans l'ordre
FIELDS = ("schema", "id", "project", "environment", "source", "native_id",
          "occurrence", "cause_key", "observed_version", "observed_at",
          "received_at", "evidence_ref", "outcome")

#: préfixe de l'identifiant stable (le schéma n'entre pas dans l'identité :
#: une évolution additive de l'enveloppe ne doit pas renuméroter les reçus)
_ID_PREFIX = "rs1_"
_ID_NAMESPACE = "repair-signal"

#: bornes des champs libres, alignées sur le contrat des empreintes (#3885)
MAX_PROJECT = 64
MAX_ENVIRONMENT = 32
MAX_NATIVE = 300
MAX_CAUSE = 200
MAX_VERSION = 120

_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENVIRONMENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_THREAD_ID_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)/"
                           r"([A-Za-z0-9_][A-Za-z0-9._-]*)#([0-9]+)$")
_GITHUB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+"
                           r"(?:#issuecomment-[0-9]+)?$")
_ALERT_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


__all__ = ["SCHEMA", "SUMMARY_SCHEMA", "SOURCES", "OUTCOMES", "PENDING",
           "FIELDS", "Signal", "SignalError", "identity", "from_record",
           "from_thread", "from_inbox", "from_github_watch", "from_alert",
           "from_source", "ADAPTERS", "summary"]


class SignalError(ValueError):
    """Une entrée est refusée explicitement ; le message ne recopie rien."""


def _bounded(value: Any, field_name: str, limit: int, *,
             required: bool = False) -> str:
    """Un champ texte borné, sans caractère de contrôle ni bord ambigu."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        raise SignalError("%s: a string is required" % field_name)
    if _CONTROL_RE.search(text):
        raise SignalError("%s: control characters are refused" % field_name)
    if text != text.strip():
        raise SignalError("%s: leading or trailing whitespace is refused"
                          % field_name)
    if required and not text:
        raise SignalError("%s: required" % field_name)
    if len(text) > limit:
        raise SignalError("%s: at most %d characters" % (field_name, limit))
    return text


def _project(value: Any) -> str:
    text = _bounded(value, "project", MAX_PROJECT, required=True)
    if not _PROJECT_RE.match(text):
        raise SignalError("project: expected a project name "
                          "(letters, digits, . _ -)")
    return text


def _environment(value: Any) -> str:
    text = _bounded(value, "environment", MAX_ENVIRONMENT, required=True)
    if not _ENVIRONMENT_RE.match(text):
        raise SignalError("environment: expected a lower-case label "
                          "such as staging or production")
    return text


def _at(value: Any, field_name: str, *, required: bool = False) -> str:
    """Horodatage UTC normalisé ``YYYY-MM-DDTHH:MM:SSZ``.

    Accepte une epoch (secondes, comme ``fil``/``mail``/``alerts``) ou une
    chaîne déjà au format du contrat (comme l'API GitHub).  Une valeur absente
    vaut « inconnu » (chaîne vide) sauf si le champ est obligatoire ; on ne
    remplit jamais un inconnu avec l'heure de lecture.
    """
    if value is None or value == "":
        if required:
            raise SignalError("%s: required" % field_name)
        return ""
    if isinstance(value, bool):
        raise SignalError("%s: an epoch or an ISO UTC timestamp is required"
                          % field_name)
    if isinstance(value, (int, float)):
        if value < 0:
            raise SignalError("%s: a non-negative epoch is required"
                              % field_name)
        try:
            when = datetime.datetime.fromtimestamp(float(value),
                                                   datetime.timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise SignalError("%s: epoch out of range" % field_name) from exc
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str):
        raise SignalError("%s: an epoch or an ISO UTC timestamp is required"
                          % field_name)
    text = value.strip()
    if not _AT_RE.match(text):
        raise SignalError("%s: expected YYYY-MM-DDTHH:MM:SSZ" % field_name)
    try:
        datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SignalError("%s: invalid UTC date" % field_name) from exc
    return text


def _now_at() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _occurrence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SignalError("occurrence: an integer >= 1 is required")
    return value


def _outcome(value: Any) -> str:
    if value not in OUTCOMES:
        raise SignalError("outcome: expected one of %s" % ", ".join(OUTCOMES))
    return value


def _source(value: Any) -> str:
    if value not in SOURCES:
        raise SignalError("source: expected one of %s" % ", ".join(SOURCES))
    return value


def _native_id(source: str, project: str, value: Any) -> str:
    text = _bounded(value, "native_id", MAX_NATIVE, required=True)
    if source == "thread":
        match = _THREAD_ID_RE.match(text)
        if match is None:
            raise SignalError("native_id: a thread entry id is expected")
        if match.group(1) != project:
            raise SignalError("native_id: thread entry belongs to another "
                              "project")
    elif source == "inbox":
        if not text.isdigit() or int(text) <= 0:
            raise SignalError("native_id: a mailbox message id is expected")
    elif source == "github_watch":
        if not _GITHUB_ID_RE.match(text):
            raise SignalError("native_id: a GitHub issue or comment reference "
                              "is expected")
    elif source == "alerts":
        if not _ALERT_KEY_RE.match(text):
            raise SignalError("native_id: an alert key (64 hex) is expected")
    return text


def _evidence(source: str, native_id: str) -> str:
    """La seule référence de preuve autorisée pour cette source."""
    return {"thread": "thread:", "inbox": "mail:",
            "github_watch": "github:", "alerts": "alert:"}[source] + native_id


def identity(project: Any, environment: Any, source: Any,
             native_id: Any) -> str:
    """L'identifiant stable d'un signal, indépendant de son contenu.

    Même projet, environnement, source et ID natif → même identifiant, même
    relu après un redémarrage ou une nouvelle occurrence.  L'occurrence, la
    date et le texte n'y entrent pas.
    """
    project = _project(project)
    environment = _environment(environment)
    source = _source(source)
    native_id = _native_id(source, project, native_id)
    material = "\0".join((_ID_NAMESPACE, project, environment, source,
                          native_id)).encode("utf-8")
    return _ID_PREFIX + hashlib.sha256(material).hexdigest()[:32]


@dataclass(frozen=True)
class Signal:
    """Une enveloppe ``repair-signal/1`` validée.

    ``id`` et ``evidence_ref`` sont dérivés : un appelant ne peut pas forger
    une référence de preuve arbitraire (URL, texte libre) ni un identifiant
    qui contredirait son identité.
    """

    project: str
    environment: str
    source: str
    native_id: str
    received_at: str
    occurrence: int = 1
    cause_key: str = ""
    observed_version: str = ""
    observed_at: str = ""
    outcome: str = PENDING
    id: str = field(init=False, default="", compare=True)
    evidence_ref: str = field(init=False, default="", compare=True)

    def __post_init__(self) -> None:
        project = _project(self.project)
        environment = _environment(self.environment)
        source = _source(self.source)
        native_id = _native_id(source, project, self.native_id)
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "native_id", native_id)
        object.__setattr__(self, "occurrence", _occurrence(self.occurrence))
        object.__setattr__(self, "cause_key",
                           _bounded(self.cause_key, "cause_key", MAX_CAUSE))
        object.__setattr__(self, "observed_version",
                           _bounded(self.observed_version, "observed_version",
                                    MAX_VERSION))
        object.__setattr__(self, "observed_at",
                           _at(self.observed_at, "observed_at"))
        object.__setattr__(self, "received_at",
                           _at(self.received_at, "received_at", required=True))
        object.__setattr__(self, "outcome", _outcome(self.outcome))
        object.__setattr__(self, "id",
                           identity(project, environment, source, native_id))
        object.__setattr__(self, "evidence_ref", _evidence(source, native_id))

    def record(self) -> dict:
        """L'enregistrement JSON, exactement les champs du contrat."""
        values = {name: getattr(self, name)
                  for name in FIELDS if name != "schema"}
        return {"schema": SCHEMA, **values}

    def as_json(self) -> dict:
        """Alias explicite de :meth:`record` pour les lecteurs de rapport."""
        return self.record()


def from_record(record: Mapping) -> Signal:
    """Lit un enregistrement ``repair-signal/1``, strictement.

    Un champ inconnu est refusé, jamais ignoré : un ajout de contrat doit
    passer par ce module d'abord.  L'identifiant et la référence de preuve
    sont recalculés et comparés ; un reçu altéré est refusé.
    """
    if not isinstance(record, Mapping):
        raise SignalError("a repair-signal/1 mapping is required")
    unknown = [key for key in record if key not in FIELDS]
    if unknown:
        raise SignalError("unknown field(s): %s"
                          % ", ".join(str(key) for key in sorted(unknown,
                                                                 key=str)))
    missing = [name for name in FIELDS if name not in record]
    if missing:
        raise SignalError("missing field(s): %s" % ", ".join(missing))
    if record["schema"] != SCHEMA:
        raise SignalError("schema: expected %s" % SCHEMA)
    signal = Signal(project=record["project"],
                    environment=record["environment"],
                    source=record["source"], native_id=record["native_id"],
                    received_at=record["received_at"],
                    occurrence=record["occurrence"],
                    cause_key=record["cause_key"],
                    observed_version=record["observed_version"],
                    observed_at=record["observed_at"],
                    outcome=record["outcome"])
    if record["id"] != signal.id:
        raise SignalError("id: does not match the signal identity")
    if record["evidence_ref"] != signal.evidence_ref:
        raise SignalError("evidence_ref: does not match the source reference")
    return signal


def _received(value: Any) -> str:
    return _at(value, "received_at", required=True) if value is not None \
        else _now_at()


def _field(record: Any, name: str, default: Any = None) -> Any:
    """Un champ de source, mapping ou objet (``fil.Entry``), jamais le texte."""
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _envelope(*, project, environment, source, native_id, received_at,
              occurrence, outcome, cause_key, observed_version,
              observed_at) -> Signal:
    return Signal(project=project, environment=environment, source=source,
                  native_id=native_id, received_at=received_at,
                  occurrence=occurrence, cause_key=cause_key,
                  observed_version=observed_version, observed_at=observed_at,
                  outcome=outcome)


def from_thread(entry: Any, *, project: str, environment: str,
                received_at: Any = None, occurrence: int = 1,
                outcome: str = PENDING, cause_key: str = "",
                observed_version: str = "") -> Signal:
    """Adapte une entrée de fil (``fil.Entry``) sans lire son texte.

    L'ID natif est l'ID d'entrée du fil (``<projet>/<fil>#<offset>``) ; la
    référence de preuve pointe ce fil.  Le corps, l'auteur et le destinataire
    restent dans le fil : ils ne sont ni copiés ni hachés ici.
    """
    native_id = _field(entry, "id")
    if not native_id:
        raise SignalError("thread: an entry id is required")
    observed = _at(_field(entry, "ts"), "thread ts")
    return _envelope(project=project, environment=environment,
                     source="thread", native_id=native_id,
                     received_at=_received(received_at),
                     occurrence=occurrence, outcome=outcome,
                     cause_key=cause_key,
                     observed_version=observed_version,
                     observed_at=observed)


def from_inbox(message: Any, *, project: str, environment: str,
               received_at: Any = None, occurrence: int = 1,
               outcome: str = PENDING, cause_key: str = "",
               observed_version: str = "") -> Signal:
    """Adapte un message de la boîte (``mail``) sans lire son corps.

    Accepte une ligne de la base (``id``, ``created_ts``) ou la forme
    ``mail.normalize`` (``id``, ``ts``).  La référence ``mail:<id>`` laisse
    le message dans la boîte, où sa rétention et ses droits s'appliquent.
    """
    native = _field(message, "id")
    if isinstance(native, bool) or not isinstance(native, int) or native <= 0:
        raise SignalError("inbox: a positive message id is required")
    observed = _at(_field(message, "created_ts", _field(message, "ts")),
                   "mail ts")
    return _envelope(project=project, environment=environment,
                     source="inbox", native_id=str(native),
                     received_at=_received(received_at),
                     occurrence=occurrence, outcome=outcome,
                     cause_key=cause_key,
                     observed_version=observed_version,
                     observed_at=observed)


def from_github_watch(event: Any, *, project: str, environment: str,
                      received_at: Any = None, occurrence: int = 1,
                      outcome: str = PENDING, cause_key: str = "",
                      observed_version: str = "") -> Signal:
    """Adapte un événement de veille GitHub sans lire corps ni titre.

    L'ID natif d'une demande est ``dépôt#numéro`` ; celui d'un commentaire
    ajoute ``#issuecomment-<id>``, pour que deux commentaires d'une même
    demande ne se confondent pas.  ``digest`` et ``url`` de la veille ne sont
    pas recopiés : la référence canonique suffit.
    """
    issue_ref = _field(event, "issue_ref")
    kind = _field(event, "kind")
    if not isinstance(issue_ref, str) or not issue_ref:
        raise SignalError("github_watch: an issue reference is required")
    if kind == "issue":
        native_id = issue_ref
    elif kind == "comment":
        object_id = _field(event, "object_id")
        if isinstance(object_id, bool) or not isinstance(object_id, int) \
                or object_id <= 0:
            raise SignalError("github_watch: a positive comment id is required")
        native_id = "%s#issuecomment-%d" % (issue_ref, object_id)
    else:
        raise SignalError("github_watch: kind issue or comment is required")
    observed = _at(_field(event, "updated_at"), "github updated_at")
    return _envelope(project=project, environment=environment,
                     source="github_watch", native_id=native_id,
                     received_at=_received(received_at),
                     occurrence=occurrence, outcome=outcome,
                     cause_key=cause_key,
                     observed_version=observed_version,
                     observed_at=observed)


def from_alert(alert: Any, *, project: str, environment: str,
               received_at: Any = None, occurrence: int = 1,
               outcome: str = PENDING, cause_key: str = "",
               observed_version: str = "") -> Signal:
    """Adapte une alerte de ``ameesh alerts`` sans lire son ``detail``.

    La clé d'alerte est l'identité durable : un SHA-256 existant est conservé,
    sinon la clé source est sérialisée canoniquement puis hachée (notamment
    le tableau JSON en texte de ``notify.key_of``). ``detail``, ``host``,
    ``value`` et les autres champs de
    diagnostic restent dans le relevé d'alertes.  Un ``project`` porté par
    l'alerte et différent du contexte est refusé, jamais réattribué.
    """
    key = _field(alert, "key")
    if not isinstance(key, str) or not key.strip():
        raise SignalError("alerts: an alert key is required")
    if not _ALERT_KEY_RE.fullmatch(key):
        try:
            source_key = json.loads(key)
        except ValueError:
            source_key = key
        try:
            canonical = json.dumps(source_key, sort_keys=True,
                                   separators=(',', ':'), ensure_ascii=True,
                                   allow_nan=False)
        except ValueError:
            raise SignalError("alerts: invalid alert key") from None
        key = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    carried = _field(alert, "project")
    if carried not in (None, "") and carried != project:
        raise SignalError("alerts: alert belongs to another project")
    schema = _field(alert, "schema")
    if schema not in (None, "", "ameesh-alert/1"):
        raise SignalError("alerts: expected the ameesh-alert/1 schema")
    observed = _at(_field(alert, "since"), "alert since")
    return _envelope(project=project, environment=environment,
                     source="alerts", native_id=key,
                     received_at=_received(received_at),
                     occurrence=occurrence, outcome=outcome,
                     cause_key=cause_key,
                     observed_version=observed_version,
                     observed_at=observed)


#: le nom de source vers son adaptateur pur, pour U5/U6 et les fixtures
ADAPTERS = {
    "thread": from_thread,
    "inbox": from_inbox,
    "github_watch": from_github_watch,
    "alerts": from_alert,
}


def from_source(source: str, record: Any, **context) -> Signal:
    """Adapte un enregistrement de la source nommée."""
    source = _source(source)
    return ADAPTERS[source](record, **context)


def summary(signals: Iterable) -> dict:
    """Compteurs A1 pour le rapport U1 : par source, environnement, issue.

    ``signals`` contient des :class:`Signal` ou des enregistrements
    ``repair-signal/1`` (relus strictement).  Les clés ne sont jamais
    inventées : un environnement ou une source absente du lot n'apparaît pas
    avec un zéro trompeur, et les valeurs inconnues sont comptées à part.
    """
    by_source: Counter = Counter()
    by_environment: Counter = Counter()
    by_outcome: Counter = Counter()
    by_source_environment: Counter = Counter()
    unknown = Counter()
    count = 0
    occurrences = 0
    for item in signals:
        signal = item if isinstance(item, Signal) else from_record(item)
        count += 1
        occurrences += signal.occurrence
        by_source[signal.source] += 1
        by_environment[signal.environment] += 1
        by_outcome[signal.outcome] += 1
        by_source_environment["%s/%s" % (signal.source,
                                         signal.environment)] += 1
        if not signal.cause_key:
            unknown["cause_key"] += 1
        if not signal.observed_version:
            unknown["observed_version"] += 1
        if not signal.observed_at:
            unknown["observed_at"] += 1
    return {
        "schema": SUMMARY_SCHEMA,
        "signals": count,
        "occurrences": occurrences,
        "by_source": dict(sorted(by_source.items())),
        "by_environment": dict(sorted(by_environment.items())),
        "by_outcome": dict(sorted(by_outcome.items())),
        "by_source_environment": dict(sorted(by_source_environment.items())),
        "unknown": dict(sorted(unknown.items())),
    }
