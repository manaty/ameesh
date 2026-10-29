# SPDX-License-Identifier: AGPL-3.0-only
"""Journal d'observations IMA (#4425, B de #2846).

La decision du proprietaire (#155437, option a) confie a IMA le role de
collecteur de confiance : les deploiements, les resultats QA et les
acceptations humaines sont des **observations** natives, ecrites par une API
de journal locale, puis projetees en lecture seule vers les hotes.

Deux objets separes, comme le pont d'approbation #4006 :

* le **journal** est un fichier JSONL append-only sur ``bureau-goal``
  (0600, dossier 0700). Il n'est ecrit que par les fonctions de ce module
  (:func:`Journal.record`, :func:`Journal.supersede`,
  :func:`Journal.revoke`) ; une entree n'est jamais modifiee ni retiree. Le
  journal garde donc l'historique des corrections et des revocations meme
  quand un commentaire GitHub source est modifie ou supprime : chaque
  enregistrement porte le digest de sa source, et un digest different est
  refuse (``observation-edited``) au lieu d'etre reecrit ;
* la **projection** ``ameesh-observations/1`` est derivee du journal a chaque
  rafraichissement, avec ``generated_at`` / ``expires_at`` et un digest
  JCS+SHA-256. Les hotes lisent ce fichier JSON, sans reseau ni secret, et le
  refusent quand il est absent, illisible, perime, d'une autre source ou
  invalide (fail-closed).

Sources collectees sur ``bureau-goal`` :

* les attestations de staging natives (``ameesh-staging-actif/1`` dans
  ``mise-a-jour/actif-*.json``, et les rapports de coupure
  ``ameesh-release-cutoff/1`` dans ``release/reports/*.json``) -> ``deployed`` ;
* les acceptations humaines de la projection d'approbation #4006
  (``ameesh-approvals/1``), avec sa revocation et son echeance -> ``pass``
  d'environnement ``acceptance``. Aucun seuil propre n'est ajoute a celui de
  #4006 ;
* les resultats QA d'IMA : **aucune enveloppe n'est inventee**. Le contrat de
  ce format est le ticket externe IMA #796 ; tant qu'il n'est pas la, une
  configuration ``qa`` est refusee (``reader-config``) et aucun ``pass`` /
  ``fail`` QA n'est collecte. Le lecteur et le temoin sont prets pour ce
  contrat.

Mapping des genres du relais : ``merged_and_staging_deployed`` et
``preprod_deployed`` -> ``deployed`` ; ``*_pass`` -> ``pass`` ;
``*_failure`` -> ``fail``. ``repair_started`` n'est mappe sur rien : ce n'est
pas une preuve de deploiement, aucune garde ne l'accepte.

Fraicheur (constantes nommees, documentees dans
``docs/design/process-workflow-registry.md``) :

* projection : TTL 300 s par defaut, rafraichissement conseille chaque minute
  (au moins deux periodes de refresh dans le TTL) ; desaccord d'horloge 60 s ;
* ``deployed`` : **pas d'expiration a l'age** ; il est remplace par le
  deploiement suivant du meme environnement, sur le meme lot et le meme
  projet, et le courant est le dernier non revoque ;
* QA ``pass`` / ``fail`` : 7 jours (``QA_TTL_S``) ; une acceptation humaine
  suit l'echeance de la projection #4006, sans TTL propre.

Codes de refus stables (les codes Process de #4007 ne sont pas remplaces) :

* lecteur / collecteur : ``reader-config``, ``reader-unavailable``,
  ``reader-bounds``, ``acceptances-unavailable`` ;
* journal : ``journal-missing``, ``journal-unreadable``,
  ``journal-permissions``, ``journal-bounds``, ``journal-invalid`` ;
* projection : ``projection-missing``, ``projection-unreadable``,
  ``projection-permissions``, ``projection-owner``, ``projection-bounds``,
  ``projection-invalid``, ``projection-digest``, ``projection-stale``,
  ``projection-future``, ``projection-source`` ;
* observation : ``observation-missing``, ``observation-invalid``,
  ``observation-edited``, ``observation-revoked``, ``observation-superseded``,
  ``observation-expired``, ``observation-future``, ``observation-stale``,
  ``observation-source`` ;
* temoins de collecte (sans contenu de source) : ``source-invalid``,
  ``source-bounds``.

Aucun secret n'entre dans Git, dans le journal, dans la projection ou dans un
message d'erreur. Le collecteur ne lit pas le jeton GitHub : il lit la
projection locale de #4006, deja fail-closed.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import glob
import hashlib
import hmac
import json
import os
import re
import socket
import stat
import tempfile
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from . import config as config_mod
from . import jcs

#: schema de la projection locale
SCHEMA = "ameesh-observations/1"
#: genres d'observation natifs
KIND_DEPLOYED = "deployed"
KIND_PASS = "pass"
KIND_FAIL = "fail"
OBSERVATION_KINDS = (KIND_DEPLOYED, KIND_PASS, KIND_FAIL)
#: environnements ; ``acceptance`` est une acceptation humaine, jamais un
#: environnement de deploiement (les gardes staging/preprod l'ignorent).
ENVIRONMENT_STAGING = "staging"
ENVIRONMENT_PREPROD = "preprod"
ENVIRONMENT_ACCEPTANCE = "acceptance"
OBSERVATION_ENVIRONMENTS = (ENVIRONMENT_STAGING, ENVIRONMENT_PREPROD,
                            ENVIRONMENT_ACCEPTANCE)
#: etats derives d'un enregistrement
STATE_CURRENT = "current"
STATE_SUPERSEDED = "superseded"
STATE_REVOKED = "revoked"
OBSERVATION_STATES = (STATE_CURRENT, STATE_SUPERSEDED, STATE_REVOKED)
#: provenance d'un enregistrement
PROVENANCE_GITHUB = "github-comment"
PROVENANCE_STAGING = "staging-attestation"
PROVENANCE_RELEASE = "release-report"
PROVENANCE_KINDS = (PROVENANCE_GITHUB, PROVENANCE_STAGING, PROVENANCE_RELEASE)
#: operations du journal append-only
JOURNAL_OPS = ("record", "supersede", "revoke")

#: bornes de lecture et d'ecriture
MAX_PROJECTION_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_OBSERVATIONS = 5000
#: Le journal porte aussi les supersessions et revocations : borne large mais
#: finie, verifiee avant chaque ecriture comme a la lecture.
MAX_JOURNAL_ENTRIES = MAX_OBSERVATIONS * 4
#: Une attestation qui nomme plus de lots que cela est refusee : la borne de
#: projection ne doit jamais etre depassee par un seul fichier source.
MAX_LOTS_PER_ATTESTATION = 1000
MAX_REVOCATIONS = 2000
#: La projection ne porte que les enregistrements courants et une fenetre
#: d'historique ; le journal append-only garde tout. Sans cette fenetre, une
#: histoire qui grandit rendrait la projection impubliable pour toujours.
MAX_HISTORY = 1000
MAX_REFUSALS = 2000
MAX_STAGING_FILES = 500
MAX_TTL_S = 3600
MAX_AGE_S = 86400
DEFAULT_TTL_S = 300
#: Au moins deux periodes du refresh conseille (une minute).
MIN_TTL_S = 120
CLOCK_SKEW_S = 60
#: QA : une observation ``pass`` / ``fail`` non humaine expire a 7 jours.
QA_TTL_S = 7 * 24 * 3600

_AUTHORITY_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_IDENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$")
_ISSUE_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100})#([1-9][0-9]{0,8})$")
_OBSERVATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/@+#=-]{0,511}$")
_APP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ARTIFACT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
#: un commit actif d'attestation est un SHA complet ou son prefixe (7 a 40)
_HOST_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class ObservationError(RuntimeError):
    """Refus stable du module : un code, un message sans secret."""

    code = "observation-error"

    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.detail = detail


class ReaderError(ObservationError):
    """Le collecteur (bureau-goal) ne peut pas lire une source configuree."""


class ProjectionError(ObservationError):
    """La projection locale est absente, illisible, perimee ou invalide."""


class ObservationRefusal(ObservationError):
    """Une observation ne couvre pas la demande courante."""


class JournalError(ObservationError):
    """Le journal append-only est absent, illisible ou incoherent."""


# ---------------------------------------------------------------------------
# temps et formes simples
# ---------------------------------------------------------------------------

def _utc(value: dt.datetime | None = None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime expected")
    return value.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any, what: str = "timestamp") -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s: non-empty ISO 8601 string expected" % what)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("%s: invalid ISO 8601 timestamp" % what) from exc
    if parsed.tzinfo is None:
        raise ValueError("%s: timezone required" % what)
    return parsed.astimezone(dt.timezone.utc)


def _as_int(value: Any, what: str, *, minimum: int = 0,
            maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s: integer expected" % what)
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError("%s: out of bounds" % what)
    return value


def _require_text(value: Any, what: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise ValueError("%s: string expected" % what)
    text = value.strip()
    if not text:
        raise ValueError("%s: non-empty string expected" % what)
    if len(text) > maximum:
        raise ValueError("%s: too long" % what)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("%s: control character refused" % what)
    return text


def _optional_text(value: Any, what: str, *, maximum: int = 4096) -> str:
    if value is None or value == "":
        return ""
    return _require_text(value, what, maximum=maximum)


def _check_authority(value: Any) -> str:
    text = _require_text(value, "authority", maximum=64).lower()
    if not _AUTHORITY_RE.fullmatch(text):
        raise ValueError("authority: lowercase name expected")
    return text


def _check_ident(value: Any, what: str = "journal_id") -> str:
    text = _require_text(value, what, maximum=64).lower()
    if not _IDENT_RE.fullmatch(text):
        raise ValueError("%s: lowercase name expected" % what)
    return text


def _check_repo(value: Any, what: str = "repo") -> str:
    text = _require_text(value, what, maximum=201).lower()
    if not _REPO_RE.fullmatch(text):
        raise ValueError("%s: owner/repository expected" % what)
    return text


def _check_issue(value: Any) -> str:
    text = _require_text(value, "issue", maximum=220)
    match = _ISSUE_RE.fullmatch(text)
    if not match:
        raise ValueError("issue: owner/repository#N expected")
    return "%s#%d" % (match.group(1).lower(), int(match.group(2)))


def _check_app(value: Any, what: str = "app", *, allow_empty: bool = False) -> str:
    if allow_empty and (value is None or value == ""):
        return ""
    text = _require_text(value, what, maximum=64)
    if not _APP_RE.fullmatch(text):
        raise ValueError("%s: lowercase app name expected" % what)
    return text


def _check_id(value: Any, what: str = "observation id") -> str:
    text = _require_text(value, what, maximum=512)
    if not _OBSERVATION_ID_RE.fullmatch(text):
        raise ValueError("%s: invalid identifier" % what)
    return text


def _check_artifact(value: Any) -> str:
    text = _require_text(value, "artifact", maximum=64).lower()
    if not _ARTIFACT_RE.fullmatch(text):
        raise ValueError("artifact: hexadecimal revision expected")
    return text


def _check_digest(value: Any, what: str = "digest") -> str:
    text = _require_text(value, what, maximum=64).lower()
    if not _DIGEST_RE.fullmatch(text):
        raise ValueError("%s: SHA-256 expected" % what)
    return text


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(jcs.canonicalize(value)).hexdigest()


def _text(value: Any, *, maximum: int = 4096) -> str:
    """Texte optionnel borne : jamais une exception pour un champ lisible."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text[:maximum]


# ---------------------------------------------------------------------------
# provenance, observation, projection
# ---------------------------------------------------------------------------

def _check_provenance(value: Any) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("provenance: JSON object expected")
    kind = _require_text(value.get("kind"), "provenance kind", maximum=32)
    if kind not in PROVENANCE_KINDS:
        raise ValueError("provenance kind: unknown")
    if kind == PROVENANCE_GITHUB:
        repo = _check_repo(value.get("repo"), "provenance repo")
        comment_id = _as_int(value.get("comment_id"), "comment_id", minimum=1)
        url = _require_text(value.get("url"), "provenance url", maximum=2048)
        if not (url.startswith("https://github.com/")
                or url.startswith("https://api.github.com/")):
            raise ValueError("provenance url: GitHub URL expected")
        body_sha256 = _check_digest(value.get("body_sha256"), "body_sha256")
        path = _require_text(value.get("path"), "provenance path", maximum=512)
        return {"kind": kind, "repo": repo, "comment_id": comment_id,
                "url": url, "body_sha256": body_sha256, "path": path}
    if kind == PROVENANCE_STAGING:
        ref = _require_text(value.get("ref"), "provenance ref", maximum=1024)
        digest = _check_digest(value.get("sha256"), "provenance sha256")
        attestation = _optional_text(value.get("attestation"),
                                     "provenance attestation", maximum=200)
        hosts_raw = value.get("hosts") or []
        if not isinstance(hosts_raw, (list, tuple)):
            raise ValueError("provenance hosts: list expected")
        hosts = tuple(sorted({_require_text(host, "host", maximum=200)
                              for host in hosts_raw}))
        return {"kind": kind, "ref": ref, "sha256": digest,
                "attestation": attestation, "hosts": list(hosts)}
    ref = _require_text(value.get("ref"), "provenance ref", maximum=1024)
    digest = _check_digest(value.get("sha256"), "provenance sha256")
    release = _require_text(value.get("release"), "provenance release",
                            maximum=200)
    version = _optional_text(value.get("version"), "provenance version",
                             maximum=100)
    completed = _optional_text(value.get("completed_at"),
                               "provenance completed_at", maximum=40)
    if completed:
        completed = _iso(_parse_ts(completed, "provenance completed_at"))
    return {"kind": kind, "ref": ref, "sha256": digest,
            "release": release, "version": version,
            "completed_at": completed}


def _record_observation(value: Any) -> dict:
    """La forme immuable d'un enregistrement (sans etat derive)."""
    if not isinstance(value, Mapping):
        raise ValueError("observation: JSON object expected")
    work_item_raw = value.get("work_item_id")
    if work_item_raw in (None, "", 0, "0"):
        work_item_id: int | None = None
    else:
        work_item_id = _as_int(work_item_raw, "work_item_id", minimum=1)
    kind = _require_text(value.get("kind"), "kind", maximum=16)
    if kind not in OBSERVATION_KINDS:
        raise ValueError("kind: deployed, pass or fail expected")
    environment = _require_text(value.get("environment"), "environment",
                                maximum=16)
    if environment not in OBSERVATION_ENVIRONMENTS:
        raise ValueError("environment: unknown")
    provenance = _check_provenance(value.get("provenance"))
    if kind == KIND_DEPLOYED:
        if environment not in (ENVIRONMENT_STAGING, ENVIRONMENT_PREPROD):
            raise ValueError("deployed observation outside a deployment environment")
        if provenance["kind"] not in (PROVENANCE_STAGING, PROVENANCE_RELEASE):
            raise ValueError("deployed observation without a native staging proof")
    else:
        # #4425 : le contrat QA IMA (#796) n'existe pas ; aucun pass/fail QA
        # n'entre dans le journal avant lui. Sans cette garde, l'API
        # d'ecriture contournerait le refus de configuration et une preuve QA
        # inventee satisferait staging_pass/staging_failure/repair_started.
        if environment != ENVIRONMENT_ACCEPTANCE:
            raise ValueError(
                "QA observation contract is not installed: no QA pass/fail "
                "in a deployment environment")
        if provenance["kind"] != PROVENANCE_GITHUB:
            raise ValueError(
                "acceptance observation without a #4006 approval proof")
    expires_raw = value.get("expires_at")
    expires_at = _iso(_parse_ts(expires_raw, "expires_at")) \
        if expires_raw else None
    return {
        "id": _check_id(value.get("id")),
        "kind": kind,
        "environment": environment,
        "work_item_id": work_item_id,
        "app": _check_app(value.get("app"), allow_empty=True),
        "artifact": _check_artifact(value.get("artifact")),
        "source_revision": _require_text(value.get("source_revision"),
                                         "source_revision", maximum=512),
        "actor": _text(value.get("actor"), maximum=200),
        "observed_at": _iso(_parse_ts(value.get("observed_at"), "observed_at")),
        "expires_at": expires_at,
        "provenance": provenance,
        "source_digest": _check_digest(value.get("source_digest"),
                                       "source_digest"),
    }


@dataclass(frozen=True)
class Observation:
    """Un enregistrement du journal, avec son etat derive."""

    id: str
    kind: str
    environment: str
    work_item_id: int | None
    app: str
    artifact: str
    source_revision: str
    actor: str
    observed_at: dt.datetime
    provenance: Mapping[str, Any]
    source_digest: str
    #: Echeance propre de la source (accord #4006) ; None = aucune echeance
    #: supplementaire. Le lecteur l'applique sans attendre un refresh.
    expires_at: dt.datetime | None = None
    state: str = STATE_CURRENT
    superseded_by: str = ""
    revoked_by: str = ""
    digest: str = ""

    def record_payload(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "environment": self.environment,
            "work_item_id": self.work_item_id,
            "app": self.app,
            "artifact": self.artifact,
            "source_revision": self.source_revision,
            "actor": self.actor,
            "observed_at": _iso(self.observed_at),
            "expires_at": _iso(self.expires_at) if self.expires_at else None,
            "provenance": dict(self.provenance),
            "source_digest": self.source_digest,
        }

    def _payload(self) -> dict:
        payload = self.record_payload()
        payload["state"] = self.state
        payload["superseded_by"] = self.superseded_by
        payload["revoked_by"] = self.revoked_by
        return payload

    def as_dict(self) -> dict:
        payload = self._payload()
        payload["digest"] = _sha256_json(payload)
        return payload

    def as_source(self) -> dict:
        """La forme lue par le registre B / les gardes Process."""
        return {
            "id": self.id,
            "kind": self.kind,
            "env": self.environment,
            "environment": self.environment,
            "sha": self.artifact,
            "state": self.state,
            "lot": self.work_item_id,
            "app": self.app,
            "actor": self.actor,
            "observed_at": _iso(self.observed_at),
            "expires_at": _iso(self.expires_at) if self.expires_at else None,
            "source_revision": self.source_revision,
            "provenance": dict(self.provenance),
        }


def _parse_observation(value: Any) -> Observation:
    if not isinstance(value, Mapping):
        raise ValueError("observation: JSON object expected")
    payload = _record_observation(value)
    state = _require_text(value.get("state"), "state", maximum=16)
    if state not in OBSERVATION_STATES:
        raise ValueError("state: current, superseded or revoked expected")
    superseded_by = _optional_text(value.get("superseded_by"),
                                   "superseded_by", maximum=512)
    revoked_by = _optional_text(value.get("revoked_by"), "revoked_by",
                                maximum=64)
    if state == STATE_SUPERSEDED and not superseded_by:
        raise ValueError("superseded observation: superseded_by required")
    if state == STATE_REVOKED and not revoked_by:
        raise ValueError("revoked observation: revoked_by required")
    digest = _check_digest(value.get("digest"), "observation digest")
    expected_payload = dict(payload)
    expected_payload["state"] = state
    expected_payload["superseded_by"] = superseded_by
    expected_payload["revoked_by"] = revoked_by
    if not hmac.compare_digest(digest, _sha256_json(expected_payload)):
        raise ValueError("observation digest mismatch")
    return Observation(
        id=payload["id"], kind=payload["kind"],
        environment=payload["environment"],
        work_item_id=payload["work_item_id"], app=payload["app"],
        artifact=payload["artifact"],
        source_revision=payload["source_revision"],
        actor=payload["actor"],
        observed_at=_parse_ts(payload["observed_at"], "observed_at"),
        expires_at=_parse_ts(payload["expires_at"], "expires_at")
        if payload.get("expires_at") else None,
        provenance=dict(payload["provenance"]),
        source_digest=payload["source_digest"], state=state,
        superseded_by=superseded_by, revoked_by=revoked_by, digest=digest)


def _parse_revocation(value: Any) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("revocation: JSON object expected")
    ident = _check_id(value.get("id"), "revocation id")
    if not re.fullmatch(r"rev-[1-9][0-9]{0,8}", ident):
        raise ValueError("revocation id: rev-N expected")
    target = _check_id(value.get("target"), "revocation target")
    reason = _require_text(value.get("reason"), "revocation reason",
                           maximum=512)
    at = _iso(_parse_ts(value.get("at"), "revocation at"))
    actor = _text(value.get("actor"), maximum=200)
    return {"id": ident, "target": target, "reason": reason, "at": at,
            "actor": actor}


def _parse_refusal(value: Any) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("refusal: JSON object expected")
    code = _require_text(value.get("code"), "refusal code", maximum=64)
    if not _CODE_RE.fullmatch(code):
        raise ValueError("refusal code: invalid")
    source = _text(value.get("source"), maximum=300)
    return {"code": code, "source": source}


def _expired(observation: Observation, now: dt.datetime) -> bool:
    """Age maximal par genre (#4425) : deploye jamais, QA 7 j, accord #4006.

    Une acceptation humaine suit l'echeance propre de l'accord #4006, portee
    par l'enregistrement : elle est appliquee a chaque lecture, sans attendre
    un refresh. Sans echeance, la projection #4006 reste la seule borne (pas
    de TTL ajoute). L'age QA de 7 jours ne vise ni ``deployed`` ni
    ``acceptance``.
    """
    if observation.expires_at is not None \
            and now > observation.expires_at:
        return True
    if observation.kind != KIND_DEPLOYED \
            and observation.environment != ENVIRONMENT_ACCEPTANCE:
        return now > observation.observed_at + dt.timedelta(seconds=QA_TTL_S)
    return False


def _future(observation: Observation, now: dt.datetime) -> bool:
    return observation.observed_at > now + dt.timedelta(seconds=CLOCK_SKEW_S)


@dataclass(frozen=True)
class Projection:
    """La projection locale, deja validee et datee."""

    source: Mapping[str, Any]
    generated_at: dt.datetime
    expires_at: dt.datetime
    observations: tuple[Observation, ...]
    revocations: tuple[Mapping[str, Any], ...]
    refusals: tuple[Mapping[str, Any], ...]
    collector: Mapping[str, Any] = field(default_factory=dict)
    digest: str = ""
    path: str = ""
    authority: str = ""

    def meta(self) -> dict:
        source = dict(self.source)
        return {
            "generated_at": _iso(self.generated_at),
            "expires_at": _iso(self.expires_at),
            "digest": self.digest,
            "source_digest": str(source.get("source_digest") or ""),
            "journal_id": str(source.get("journal_id") or ""),
            "repos": list(source.get("repos") or []),
            "issues": list(source.get("issues") or []),
            "entries": int(source.get("entries") or 0),
            "authority": self.authority,
        }

    def current(self, now: dt.datetime | None = None) -> tuple[Observation, ...]:
        current = _utc(now)
        return tuple(
            observation for observation in self.observations
            if observation.state == STATE_CURRENT
            and not _expired(observation, current)
            and not _future(observation, current))

    def by_id(self, ident: str) -> Observation | None:
        for observation in self.observations:
            if observation.id == ident:
                return observation
        return None

    def as_source(self, now: dt.datetime | None = None) -> dict:
        return {"projection": self.meta(),
                "records": [observation.as_source()
                            for observation in self.current(now)]}

    def _payload(self) -> dict:
        return {
            "schema": SCHEMA,
            "authority": self.authority,
            "source": dict(self.source),
            "generated_at": _iso(self.generated_at),
            "expires_at": _iso(self.expires_at),
            "collector": dict(self.collector),
            "observations": [observation.as_dict()
                             for observation in self.observations],
            "revocations": [dict(revocation)
                            for revocation in self.revocations],
            "refusals": [dict(refusal) for refusal in self.refusals],
        }

    def as_dict(self) -> dict:
        payload = self._payload()
        if self.digest:
            payload["digest"] = self.digest
        return payload


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _section(config: Any) -> dict:
    if config is None:
        config = config_mod.load()
    if isinstance(config, Mapping):
        section = config.get("observations")
    else:
        section = getattr(config, "observations", None)
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ProjectionError("projection-source",
                              "observations: JSON object expected")
    return dict(section)


def _source_mapping(section: Any) -> Mapping:
    if not isinstance(section, Mapping):
        raise ReaderError("reader-config", "observations: JSON object expected")
    raw = section.get("source")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ReaderError("reader-config",
                          "observations.source: JSON object expected")
    return raw


def _check_absolute(path_value: Any, what: str) -> str:
    text = _require_text(path_value, what, maximum=4096)
    path = os.path.abspath(os.path.expanduser(text))
    if not os.path.isabs(path):
        raise ValueError("%s: absolute path expected" % what)
    return path


def _check_repos(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("repos: list expected")
    return tuple(sorted({_check_repo(item, "repos") for item in value}))


def _check_issues(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("issues: list expected")
    return tuple(sorted({_check_issue(item) for item in value}))


@dataclass(frozen=True)
class Source:
    """Configuration du collecteur (bureau-goal seulement)."""

    journal: str
    projection: str
    authority: str
    journal_id: str
    repos: tuple[str, ...]
    issues: tuple[str, ...]
    staging_state_dir: str = ""
    staging_app: str = ""
    staging_hosts: tuple[str, ...] = ()
    #: Sous-ensemble de ``staging_hosts`` qui peut etre reporte (jamais les
    #: VM d'agents requises) ; vide = aucun report autorise.
    staging_reportable_hosts: tuple[str, ...] = ()
    #: Hotes qui doivent toujours etre actifs, jamais reportes (les VM
    #: d'agents du canon) ; prioritaire sur ``staging_reportable_hosts``.
    staging_active_hosts: tuple[str, ...] = ()
    acceptances: bool = False
    ttl_s: int = DEFAULT_TTL_S
    reader: str = ""


@dataclass(frozen=True)
class ObservationIdentity:
    """Identite attendue de la projection, ancree dans la configuration locale.

    Le fichier de projection est auto-declare : il ne prouve pas sa provenance.
    Un hote refuse donc une projection dont l'autorite, le journal, les depots
    ou les issues ne correspondent pas a cette identite locale.
    """

    authority: str
    journal_id: str
    repos: tuple[str, ...]
    issues: tuple[str, ...]


def source_from_config(section: Any) -> Source:
    """Construit et valide la configuration du collecteur.

    La section attendue est ``cfg["observations"]`` : ``authority`` (sans
    defaut), ``journal``, ``projection``, ``max_age_s``, ``owner_uid``
    eventuel, et ``source`` (``journal_id``, ``repos``, ``issues``,
    ``staging``, ``acceptances``, ``ttl_s``). Aucune valeur de jeton n'est
    lue ici : la collecte des acceptations passe par la projection #4006.
    """
    if not isinstance(section, Mapping):
        raise ReaderError("reader-config", "observations: JSON object expected")
    if "token" in section or "token_value" in section:
        raise ReaderError("reader-config",
                          "a token value is never read from the configuration")
    raw = _source_mapping(section)
    if "token" in raw or "token_value" in raw:
        raise ReaderError("reader-config",
                          "a token value is never read from the configuration")
    qa = section.get("qa", raw.get("qa"))
    if qa:
        # #4425 : le contrat du format QA est le ticket externe IMA #796.
        raise ReaderError(
            "reader-config",
            "QA observation contract is not installed: no envelope is invented")
    try:
        authority = _check_authority(section.get("authority"))
        journal = _check_absolute(section.get("journal"), "journal")
        projection = _check_absolute(section.get("projection"), "projection")
        if journal == projection:
            raise ValueError("journal and projection must be distinct files")
        journal_id = _check_ident(raw.get("journal_id"))
        repos = _check_repos(raw.get("repos"))
        issues = _check_issues(raw.get("issues"))
        ttl_s = _as_int(raw.get("ttl_s", DEFAULT_TTL_S), "ttl_s",
                        minimum=MIN_TTL_S, maximum=MAX_TTL_S)
        reader = _optional_text(section.get("reader", raw.get("reader")),
                                "reader", maximum=200)
        acceptances_raw = raw.get("acceptances", False)
        if not isinstance(acceptances_raw, bool):
            raise ValueError("acceptances: boolean expected")
        acceptances = acceptances_raw
        staging_raw = raw.get("staging")
        staging_state_dir = ""
        staging_app = ""
        staging_hosts: tuple[str, ...] = ()
        staging_reportable_hosts: tuple[str, ...] = ()
        staging_active_hosts: tuple[str, ...] = ()
        if staging_raw is not None:
            if not isinstance(staging_raw, Mapping):
                raise ValueError("staging: JSON object expected")
            staging_state_dir = _check_absolute(staging_raw.get("state_dir"),
                                                "staging.state_dir")
            staging_app = _check_app(staging_raw.get("app"), "staging.app")
            hosts_raw = staging_raw.get("hosts") or []
            if not isinstance(hosts_raw, (list, tuple)):
                raise ValueError("staging.hosts: list expected")
            staging_hosts = tuple(sorted({
                _require_text(host, "staging host", maximum=200)
                for host in hosts_raw}))
            if not staging_hosts:
                # Sans ensemble attendu, une attestation d'un seul hote
                # suffirait a prouver un deploiement : la completude doit etre
                # declaree, jamais supposee.
                raise ValueError("staging.hosts: non-empty list required")
            reportable_raw = staging_raw.get("reportable_hosts") or []
            if not isinstance(reportable_raw, (list, tuple)):
                raise ValueError("staging.reportable_hosts: list expected")
            staging_reportable_hosts = tuple(sorted({
                _require_text(host, "reportable host", maximum=200)
                for host in reportable_raw}))
            outside = sorted(set(staging_reportable_hosts) - set(staging_hosts))
            if outside:
                raise ValueError(
                    "staging.reportable_hosts must be a subset of staging.hosts: %s"
                    % ", ".join(outside))
            active_raw = staging_raw.get("active_hosts") or []
            if not isinstance(active_raw, (list, tuple)):
                raise ValueError("staging.active_hosts: list expected")
            staging_active_hosts = tuple(sorted({
                _require_text(host, "active host", maximum=200)
                for host in active_raw}))
            outside = sorted(set(staging_active_hosts) - set(staging_hosts))
            if outside:
                raise ValueError(
                    "staging.active_hosts must be a subset of staging.hosts: %s"
                    % ", ".join(outside))
        if not staging_state_dir and not acceptances:
            raise ValueError(
                "at least one source is required: staging or acceptances")
    except (TypeError, ValueError) as exc:
        raise ReaderError("reader-config", str(exc)) from exc
    return Source(journal=journal, projection=projection, authority=authority,
                  journal_id=journal_id, repos=repos, issues=issues,
                  staging_state_dir=staging_state_dir,
                  staging_app=staging_app, staging_hosts=staging_hosts,
                  staging_reportable_hosts=staging_reportable_hosts,
                  staging_active_hosts=staging_active_hosts,
                  acceptances=acceptances, ttl_s=ttl_s, reader=reader)


def identity_from_config(section: Any) -> ObservationIdentity:
    """Identite de source attendue, lue dans ``observations.source``.

    Les hotes n'ont besoin ni du journal ni des sources de collecte, mais ils
    declarent l'autorite, ``journal_id``, ``repos`` et ``issues`` (meme
    vides). Sans ce bloc, la projection est refusee : un fichier valide ne
    prouve pas sa provenance.
    """
    if not isinstance(section, Mapping):
        raise ProjectionError("projection-source",
                              "observations: JSON object expected")
    raw = section.get("source")
    if not isinstance(raw, Mapping):
        raise ProjectionError("projection-source",
                              "observations.source identity is required")
    if "repos" not in raw or "issues" not in raw:
        raise ProjectionError(
            "projection-source",
            "observations.source.repos and issues are required (even empty)")
    try:
        authority = _check_authority(section.get("authority"))
        journal_id = _check_ident(raw.get("journal_id"))
        repos = _check_repos(raw.get("repos"))
        issues = _check_issues(raw.get("issues"))
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-source", str(exc)) from exc
    return ObservationIdentity(authority=authority, journal_id=journal_id,
                               repos=repos, issues=issues)


def projection_path(config: Any = None) -> str:
    """Chemin absolu de la projection locale, ou ``projection-missing``."""
    section = _section(config)
    raw = section.get("projection")
    if not raw:
        raise ProjectionError("projection-missing",
                              "observations.projection is not configured")
    try:
        return _check_absolute(raw, "observations.projection")
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-source", str(exc)) from exc


def journal_path(config: Any = None) -> str:
    """Chemin absolu du journal local, ou ``journal-missing``."""
    section = _section(config)
    raw = section.get("journal")
    if not raw:
        raise JournalError("journal-missing",
                           "observations.journal is not configured")
    try:
        path = _check_absolute(raw, "observations.journal")
    except (TypeError, ValueError) as exc:
        raise JournalError("journal-invalid", str(exc)) from exc
    return path


# ---------------------------------------------------------------------------
# journal append-only
# ---------------------------------------------------------------------------

def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _prepare_private_dir(path: str) -> None:
    """Cree/verifie un dossier 0700, jamais un lien symbolique."""
    if os.path.islink(path):
        raise JournalError("journal-permissions",
                           "journal directory is a symlink")
    if not os.path.exists(path):
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise JournalError("journal-unreadable",
                               "journal directory is not creatable") from exc
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
    try:
        info = os.stat(path)
    except OSError as exc:
        raise JournalError("journal-unreadable",
                           "journal directory is unreadable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise JournalError("journal-unreadable",
                           "journal directory expected")
    if info.st_mode & 0o077:
        raise JournalError("journal-permissions",
                           "journal directory must be 0700")
    if info.st_uid != os.getuid():
        raise JournalError("journal-permissions",
                           "journal directory owner mismatch")


def _read_journal_file(path: str, *, missing_ok: bool) -> list[dict]:
    path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(path) or "."
    if missing_ok and not os.path.exists(path):
        return []
    if os.path.islink(parent):
        raise JournalError("journal-permissions",
                           "journal directory is a symlink")
    try:
        parent_info = os.stat(parent)
    except OSError as exc:
        raise JournalError("journal-unreadable",
                           "journal directory is unreadable") from exc
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_mode & 0o077:
        raise JournalError("journal-permissions",
                           "journal directory must be 0700")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise JournalError("journal-missing",
                           "journal not found: %s" % path) from exc
    except OSError as exc:
        raise JournalError("journal-unreadable",
                           "journal is unreadable: %s" % path) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise JournalError("journal-unreadable",
                               "journal: regular file expected")
        if info.st_mode & 0o077:
            raise JournalError("journal-permissions", "journal must be 0600")
        if info.st_size > MAX_JOURNAL_BYTES:
            raise JournalError("journal-bounds", "journal too large")
        raw = os.read(fd, MAX_JOURNAL_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_JOURNAL_BYTES:
        raise JournalError("journal-bounds", "journal too large")
    entries: list[dict] = []
    previous = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JournalError("journal-invalid", "journal is not UTF-8") from exc
    # `str.splitlines()` couperait aussi U+2028/U+0085 : une raison ou un
    # chemin UTF-8 valide rendrait alors le journal illisible (B2). Le journal
    # n'utilise que LF comme separateur d'entree.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            if index == len(lines):
                continue
            raise JournalError("journal-invalid", "blank line in journal")
        try:
            payload = jcs.loads(line)
        except (TypeError, ValueError) as exc:
            raise JournalError("journal-invalid",
                               "journal line is not JSON") from exc
        entry = _validate_entry(payload, seq=len(entries) + 1,
                                previous=previous)
        entries.append(entry)
        previous = entry["digest"]
        if len(entries) > MAX_JOURNAL_ENTRIES:
            raise JournalError("journal-bounds", "too many journal entries")
    return entries


def _validate_entry(value: Any, *, seq: int, previous: str,
                    verify_digest: bool = True) -> dict:
    if not isinstance(value, Mapping):
        raise JournalError("journal-invalid", "entry: JSON object expected")
    try:
        body = {key: item for key, item in value.items() if key != "digest"}
        digest = value.get("digest")
        if verify_digest and (not isinstance(digest, str)
                              or not hmac.compare_digest(
                                  digest, _sha256_json(body))):
            raise JournalError("journal-invalid", "entry digest mismatch")
        if _as_int(value.get("seq"), "seq", minimum=1) != seq:
            raise JournalError("journal-invalid", "entry sequence mismatch")
        if value.get("prev") != previous:
            raise JournalError("journal-invalid", "entry chain mismatch")
        at = _iso(_parse_ts(value.get("at"), "entry at"))
        op = value.get("op")
        if op not in JOURNAL_OPS:
            raise JournalError("journal-invalid", "unknown journal operation")
        entry: dict[str, Any] = {"seq": seq, "prev": previous, "at": at,
                                 "op": op}
        if verify_digest:
            entry["digest"] = digest
        if op == "record":
            entry["observation"] = _record_observation(value.get("observation"))
        elif op == "supersede":
            entry["id"] = _check_id(value.get("id"), "supersede target")
            entry["by"] = _check_id(value.get("by"), "supersede by")
            entry["reason"] = _text(value.get("reason"), maximum=512)
        else:
            entry["id"] = _check_id(value.get("id"), "revoke target")
            revocation = _check_id(value.get("revocation"), "revocation id")
            if revocation != "rev-%d" % seq:
                raise JournalError("journal-invalid",
                                   "revocation id does not match its entry")
            entry["revocation"] = revocation
            entry["reason"] = _require_text(value.get("reason"),
                                            "revoke reason", maximum=512)
            entry["actor"] = _text(value.get("actor"), maximum=200)
    except JournalError:
        raise
    except (TypeError, ValueError) as exc:
        raise JournalError("journal-invalid", str(exc)) from exc
    return entry


def _append_line(path: str, line: str) -> None:
    path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(path) or "."
    _prepare_private_dir(parent)
    existed = os.path.exists(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise JournalError("journal-unreadable",
                           "journal is not appendable: %s" % path) from exc
    try:
        data = line.encode("utf-8")
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise JournalError("journal-unreadable",
                               "journal: regular file expected")
        if info.st_mode & 0o077:
            raise JournalError("journal-permissions", "journal must be 0600")
        if info.st_size > MAX_JOURNAL_BYTES:
            raise JournalError("journal-bounds", "journal too large")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            # La borne porte sur le fichier **apres** l'ajout : un ajout
            # accepte ne doit jamais rendre le journal illisible au reload.
            info = os.fstat(fd)
            if info.st_size > MAX_JOURNAL_BYTES - len(data):
                raise JournalError(
                    "journal-bounds",
                    "journal would exceed its byte bound; entry refused")
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    if not existed:
        _fsync_dir(parent)


@dataclass(frozen=True)
class Candidate:
    """Un enregistrement propose par une source, avec son etat source."""

    observation: Observation
    state: str = STATE_CURRENT
    superseded_by: str = ""
    revoked_reason: str = ""


class Journal:
    """Le journal append-only : lecture, chaine de digests, ecritures bornees.

    ``write=False`` derive un journal en memoire (``--dry-run``) : les
    operations sont validees et chainees, mais aucun octet n'est ecrit.
    """

    def __init__(self, path: str, *, create: bool = False,
                 write: bool = True) -> None:
        self.path = os.path.abspath(os.path.expanduser(path))
        self.create = create
        self.write = write
        self.entries: list[dict] = []

    @contextlib.contextmanager
    def lock(self):
        """Verrou exclusif du journal, pris pour toute une collecte."""
        parent = os.path.dirname(self.path) or "."
        if self.create:
            _prepare_private_dir(parent)
        lock_path = self.path + ".lock"
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise JournalError("journal-unreadable",
                               "journal lock is not creatable") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def reload(self) -> None:
        self.entries = _read_journal_file(self.path, missing_ok=self.create)

    def _append(self, body: dict, *, at: dt.datetime | None = None) -> dict:
        seq = len(self.entries) + 1
        if seq > MAX_JOURNAL_ENTRIES:
            # Refuser **avant** d'ecrire : un journal au-dela de la borne ne
            # serait plus relisible (fail-closed), pas une ecriture partielle.
            raise JournalError("journal-bounds", "journal entry bound reached")
        previous = self.entries[-1]["digest"] if self.entries else ""
        entry = dict(body)
        entry.update({"seq": seq, "prev": previous, "at": _iso(_utc(at))})
        # B2 : le digest se calcule sur l'entree **normalisee**, jamais sur la
        # forme d'entree ; sinon une valeur normalisee par la validation
        # produirait une ligne dont le digest ne correspond plus au contenu.
        normalized = _validate_entry(entry, seq=seq, previous=previous,
                                     verify_digest=False)
        normalized["digest"] = _sha256_json(normalized)
        validated = _validate_entry(normalized, seq=seq, previous=previous)
        if self.write:
            _append_line(self.path, json.dumps(validated, ensure_ascii=False,
                                               sort_keys=True) + "\n")
        self.entries.append(validated)
        return validated

    def _records(self) -> dict[str, Observation]:
        records: dict[str, Observation] = {}
        for entry in self.entries:
            if entry["op"] != "record":
                continue
            payload = entry["observation"]
            observation = Observation(
                id=payload["id"], kind=payload["kind"],
                environment=payload["environment"],
                work_item_id=payload["work_item_id"], app=payload["app"],
                artifact=payload["artifact"],
                source_revision=payload["source_revision"],
                actor=payload["actor"],
                observed_at=_parse_ts(payload["observed_at"], "observed_at"),
                expires_at=_parse_ts(payload["expires_at"], "expires_at")
                if payload.get("expires_at") else None,
                provenance=dict(payload["provenance"]),
                source_digest=payload["source_digest"])
            if observation.id in records:
                raise JournalError("journal-invalid",
                                   "duplicate observation in journal")
            records[observation.id] = observation
        return records

    def derive(self) -> tuple[tuple[Observation, ...],
                              tuple[Mapping[str, Any], ...]]:
        records = self._records()
        revocations: list[dict] = []
        for entry in self.entries:
            if entry["op"] == "supersede":
                target = records.get(entry["id"])
                if target is None:
                    raise JournalError("journal-invalid",
                                       "supersede target is unknown")
                if target.state == STATE_REVOKED:
                    raise JournalError("journal-invalid",
                                       "supersede after revocation")
                if target.state == STATE_SUPERSEDED:
                    if target.superseded_by != entry["by"]:
                        raise JournalError("journal-invalid",
                                           "observation superseded twice")
                    continue
                records[entry["id"]] = replace(
                    target, state=STATE_SUPERSEDED,
                    superseded_by=entry["by"])
            elif entry["op"] == "revoke":
                target = records.get(entry["id"])
                if target is None:
                    raise JournalError("journal-invalid",
                                       "revocation target is unknown")
                if target.state == STATE_REVOKED:
                    raise JournalError("journal-invalid",
                                       "observation revoked twice")
                records[entry["id"]] = replace(
                    target, state=STATE_REVOKED,
                    revoked_by=entry["revocation"])
                revocations.append({
                    "id": entry["revocation"], "target": entry["id"],
                    "reason": entry["reason"], "at": entry["at"],
                    "actor": entry["actor"]})
        ordered = tuple(sorted(records.values(),
                               key=lambda item: (_iso(item.observed_at),
                                                 item.id)))
        return ordered, tuple(revocations)

    def digest(self) -> str:
        return _sha256_json(self.entries)

    def record(self, observation: Observation,
               *, at: dt.datetime | None = None) -> tuple[dict, bool]:
        """Enregistre un enregistrement ; idempotent par identifiant.

        Un meme identifiant avec un autre digest de source est refuse
        (``observation-edited``) : le journal n'est jamais reecrit.
        """
        try:
            payload = _record_observation(observation.record_payload())
        except (TypeError, ValueError) as exc:
            raise JournalError("observation-invalid", str(exc)) from exc
        records = self._records()
        existing = records.get(payload["id"])
        if existing is not None:
            if existing.source_digest != payload["source_digest"]:
                raise JournalError(
                    "observation-edited",
                    "observation %s was edited at its source" % payload["id"])
            return next(entry for entry in self.entries
                        if entry["op"] == "record"
                        and entry["observation"]["id"] == payload["id"]), False
        entry = self._append({"op": "record", "observation": payload}, at=at)
        return entry, True

    def supersede(self, ident: str, by: str, *, reason: str = "",
                  at: dt.datetime | None = None) -> dict | None:
        ident = _check_id(ident, "supersede target")
        by = _check_id(by, "supersede by")
        records = {item.id: item for item in self.derive()[0]}
        target = records.get(ident)
        if target is None:
            raise JournalError("observation-missing",
                               "observation %s is unknown" % ident)
        if target.state == STATE_REVOKED:
            raise JournalError("observation-revoked",
                               "observation %s was revoked" % ident)
        if target.state == STATE_SUPERSEDED:
            if target.superseded_by == by:
                return None
            raise JournalError("observation-superseded",
                               "observation %s was superseded" % ident)
        # Une acceptation #4006 est remplacee par un identifiant de la source
        # #4006, externe au journal. Une observation native (deploiement, QA)
        # ne peut etre remplacee que par une observation connue, courante, du
        # meme lot/projet/environnement et non plus ancienne : un `by` forge
        # ne peut pas retirer une preuve sans successeur reel.
        if target.provenance.get("kind") != PROVENANCE_GITHUB:
            replacement = records.get(by)
            if replacement is None or by == ident:
                raise JournalError(
                    "observation-invalid",
                    "supersede by: unknown observation %s" % by)
            if replacement.state != STATE_CURRENT:
                raise JournalError(
                    "observation-invalid",
                    "supersede by: observation %s is not current" % by)
            if (replacement.app, replacement.work_item_id,
                    replacement.environment) != (target.app,
                                                 target.work_item_id,
                                                 target.environment):
                raise JournalError(
                    "observation-invalid",
                    "supersede by: unrelated observation %s" % by)
            if replacement.observed_at < target.observed_at:
                raise JournalError(
                    "observation-invalid",
                    "supersede by: older observation %s" % by)
        return self._append({"op": "supersede", "id": ident, "by": by,
                             "reason": _text(reason, maximum=512)}, at=at)

    def revoke(self, ident: str, *, reason: str,
               at: dt.datetime | None = None, actor: str = "") -> dict | None:
        ident = _check_id(ident, "revoke target")
        try:
            reason = _require_text(reason, "revoke reason", maximum=512)
        except (TypeError, ValueError) as exc:
            raise JournalError("observation-invalid", str(exc)) from exc
        records = {item.id: item for item in self.derive()[0]}
        target = records.get(ident)
        if target is None:
            raise JournalError("observation-missing",
                               "observation %s is unknown" % ident)
        if target.state == STATE_REVOKED:
            for entry in self.entries:
                if entry["op"] == "revoke" and entry["id"] == ident:
                    return entry
            return None
        return self._append({"op": "revoke", "id": ident,
                             "revocation": "rev-%d" % (len(self.entries) + 1),
                             "reason": reason,
                             "actor": _text(actor, maximum=200)}, at=at)


# ---------------------------------------------------------------------------
# collecte des sources (bureau-goal)
# ---------------------------------------------------------------------------

def _read_source_json(path: str) -> Mapping:
    parent = os.path.dirname(path) or "."
    try:
        parent_info = os.stat(parent)
    except OSError as exc:
        raise ReaderError("reader-unavailable",
                          "source directory is unreadable") from exc
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_mode & 0o022:
        # Un dossier modifiable par le groupe ou les autres permet de remplacer
        # l'attestation : elle ne prouve rien.
        raise ReaderError("reader-unavailable",
                          "source directory is group/other writable")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ReaderError("reader-unavailable",
                          "source file is unreadable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReaderError("reader-unavailable",
                              "source: regular file expected")
        if info.st_mode & 0o022:
            raise ReaderError("reader-unavailable",
                              "source file is group/other writable")
        if info.st_size > MAX_SOURCE_BYTES:
            raise ReaderError("reader-bounds", "source file too large")
        raw = os.read(fd, MAX_SOURCE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ReaderError("reader-bounds", "source file too large")
    try:
        payload = jcs.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReaderError("reader-unavailable",
                          "source file is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise ReaderError("reader-unavailable",
                          "source file: JSON object expected")
    return payload


def _lot_list(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("lots: list expected")
    lots: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            raise ValueError("lots: positive lot expected")
        if isinstance(item, int):
            lot = item
        elif isinstance(item, str) and re.fullmatch(r"[1-9][0-9]*", item):
            lot = int(item)
        else:
            raise ValueError("lots: positive lot expected")
        if lot <= 0:
            raise ValueError("lots: positive lot expected")
        lots.add(lot)
    if len(lots) > MAX_LOTS_PER_ATTESTATION:
        raise ValueError("lots: too many lots in one attestation")
    return tuple(sorted(lots))


def _staging_date(value: Any) -> dt.datetime:
    return _parse_ts(value, "attestation date")


def _attestation_digest(payload: Mapping, lots: Iterable[int], *,
                        app: str, ref: str) -> str:
    hotes = payload.get("hotes")
    if not isinstance(hotes, Mapping) or not hotes:
        raise ValueError("hotes: non-empty object expected")
    compact = {}
    for host, proof in hotes.items():
        if not isinstance(host, str) or not host.strip():
            raise ValueError("hotes: host name expected")
        if not isinstance(proof, Mapping):
            raise ValueError("hotes: proof object expected")
        compact[host] = {
            "actif": str(proof.get("actif") or ""),
            "non_concluant": str(proof.get("non_concluant") or ""),
        }
    canonical = {
        "schema": str(payload.get("schema") or ""),
        "cle": str(payload.get("cle") or ""),
        "sha": str(payload.get("sha") or "").lower(),
        "date": str(payload.get("date") or ""),
        "hotes": compact,
        "reportes": sorted(str(host) for host in (payload.get("reportes") or {})),
        "lots": sorted(lots),
        # L'identifiant doit couvrir tout ce que l'enregistrement porte :
        # changer le projet ou le chemin de l'attestation cree une nouvelle
        # observation, jamais une edition de l'ancienne.
        "app": app,
        "ref": ref,
    }
    return _sha256_json(canonical)


def _check_host_proofs(payload: Mapping, sha: str, required: Iterable[str],
                       reportable: Iterable[str] = (),
                       active_only: Iterable[str] = ()) -> None:
    """Valide les preuves natives par hote d'une attestation.

    Un hote actif doit porter un ``actif`` non vide, prefixe du commit atteste
    (comme ``staging_pending._merge_attestation``) ; un hote reporte doit
    porter une raison non vide et ne pas etre aussi actif. Un hote **requis**
    n'est reportable que s'il figure explicitement dans ``reportable`` : une VM
    d'agents requise ne peut donc jamais etre satisfaite par un report. Un nom
    present ne suffit pas : sans cette concordance, l'attestation ne prouve pas
    que le commit est actif sur cet hote.
    """
    required_set = set(required)
    reportable_set = set(reportable)
    active_only_set = set(active_only)
    hotes = payload.get("hotes")
    if not isinstance(hotes, Mapping) or not hotes:
        raise ValueError("hotes: non-empty object expected")
    reportes = payload.get("reportes") or {}
    if not isinstance(reportes, Mapping):
        raise ValueError("reportes: JSON object expected")
    for host, proof in hotes.items():
        if not isinstance(host, str) or not host.strip():
            raise ValueError("hotes: host name expected")
        if not isinstance(proof, Mapping):
            raise ValueError("hotes: proof object expected")
        actif = str(proof.get("actif") or "").strip().lower()
        if not _HOST_SHA_RE.fullmatch(actif) \
                or not (sha.startswith(actif) or actif.startswith(sha)):
            raise ValueError(
                "hotes: active commit does not match the attestation")
    for host, detail in reportes.items():
        if not isinstance(host, str) or not host.strip():
            raise ValueError("reportes: host name expected")
        if host in hotes:
            raise ValueError("hotes/reportes: host both active and reported")
        reason = detail.get("raison") if isinstance(detail, Mapping) else None
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reportes: a reported host needs a reason")
        if host in active_only_set:
            raise ValueError(
                "reportes: host %s must stay active (never reported)" % host)
        if host in required_set and host not in reportable_set:
            raise ValueError(
                "reportes: required host %s is not reportable" % host)
    missing = sorted(required_set - (set(hotes) | set(reportes)))
    if missing:
        raise ValueError("attestation misses required host(s): %s"
                         % ", ".join(missing))


def _actif_candidates(source: Source, path: str) -> list[Candidate]:
    payload = _read_source_json(path)
    if payload.get("schema") != "ameesh-staging-actif/1":
        raise ValueError("unknown attestation schema")
    sha = str(payload.get("sha") or "").lower()
    if not _FULL_SHA_RE.fullmatch(sha):
        raise ValueError("attestation sha: full commit expected")
    _check_host_proofs(payload, sha, source.staging_hosts,
                       source.staging_reportable_hosts,
                       source.staging_active_hosts)
    lots = _lot_list(payload.get("lots"))
    if not lots:
        raise ValueError("attestation without lot")
    observed_at = _staging_date(payload.get("date"))
    relative = os.path.relpath(path, source.staging_state_dir)
    digest = _attestation_digest(payload, lots, app=source.staging_app,
                                 ref=relative)
    provenance = {
        "kind": PROVENANCE_STAGING,
        "ref": relative,
        "sha256": digest,
        "attestation": str(payload.get("cle") or ""),
        "hosts": sorted(payload["hotes"]),
    }
    _check_provenance(provenance)
    candidates = []
    for lot in lots:
        candidates.append(Candidate(Observation(
            id="staging-%s-%d" % (digest[:24], lot),
            kind=KIND_DEPLOYED, environment=ENVIRONMENT_STAGING,
            work_item_id=lot, app=source.staging_app, artifact=sha,
            source_revision=str(payload.get("cle") or sha[:12]),
            actor="", observed_at=observed_at, provenance=provenance,
            source_digest=digest)))
    return candidates


def _release_candidates(source: Source, path: str) -> list[Candidate]:
    report = _read_source_json(path)
    if report.get("schema") != "ameesh-release-cutoff/1":
        raise ValueError("unknown release report schema")
    if report.get("project") != source.staging_app:
        raise ValueError("release report of another project")
    if report.get("result") not in ("released", "reconciled"):
        raise ValueError("release report without a release")
    staging = report.get("staging")
    if not isinstance(staging, Mapping) or staging.get("exit") != 0:
        raise ValueError("release report without a successful staging")
    attestation = staging.get("attestation")
    if not isinstance(attestation, Mapping):
        raise ValueError("release report without a staging attestation")
    if attestation.get("schema") != "ameesh-staging-actif/1":
        raise ValueError("unknown staging attestation schema")
    sha = str(report.get("release_sha") or "").lower()
    if not _FULL_SHA_RE.fullmatch(sha):
        raise ValueError("release report: full release_sha expected")
    if str(attestation.get("sha") or "").lower() != sha:
        raise ValueError("release report and attestation disagree on the commit")
    hotes = attestation.get("hotes")
    if not isinstance(hotes, Mapping) or not hotes:
        raise ValueError("release report: staging attestation without hosts")
    _check_host_proofs(attestation, sha, source.staging_hosts,
                       source.staging_reportable_hosts,
                       source.staging_active_hosts)
    lots = _lot_list(report.get("included") or attestation.get("lots"))
    if not lots:
        raise ValueError("release report without included lot")
    # Le deploiement est date par l'attestation staging, pas par la coupure de
    # release : un rapport coupe plus tard pour un commit plus ancien ne doit
    # pas remplacer un deploiement plus recent. `completed_at` reste dans le
    # digest et la provenance, jamais dans l'ordre des observations.
    observed_at = _staging_date(attestation.get("date"))
    completed_at: dt.datetime | None = None
    completed = report.get("completed_at")
    if isinstance(completed, (int, float)) and not isinstance(completed, bool):
        try:
            completed_at = dt.datetime.fromtimestamp(float(completed),
                                                     dt.timezone.utc)
        except (OverflowError, OSError, TypeError, ValueError) as exc:
            raise ValueError("release report: invalid completed_at") from exc
    relative = os.path.relpath(path, source.staging_state_dir)
    digest = _sha256_json({
        "schema": "ameesh-release-cutoff/1",
        "project": source.staging_app,
        "release_sha": sha,
        "tag": str(report.get("tag") or ""),
        "version": str(report.get("version") or ""),
        "included": sorted(lots),
        "completed_at": _iso(completed_at) if completed_at else "",
        "ref": relative,
        "attestation": {
            "schema": str(attestation.get("schema") or ""),
            "cle": str(attestation.get("cle") or ""),
            "sha": sha,
            "date": str(attestation.get("date") or ""),
        },
    })
    provenance = {
        "kind": PROVENANCE_RELEASE,
        "ref": relative,
        "sha256": digest,
        "release": str(report.get("tag") or ""),
        "version": str(report.get("version") or ""),
        "completed_at": _iso(completed_at) if completed_at else "",
    }
    _check_provenance(provenance)
    candidates = []
    for lot in lots:
        candidates.append(Candidate(Observation(
            id="release-%s-%d" % (digest[:24], lot),
            kind=KIND_DEPLOYED, environment=ENVIRONMENT_STAGING,
            work_item_id=lot, app=source.staging_app, artifact=sha,
            source_revision=str(report.get("tag") or sha[:12]),
            actor="", observed_at=observed_at, provenance=provenance,
            source_digest=digest)))
    return candidates


def _collect_staging(source: Source) -> tuple[list[Candidate], list[dict]]:
    root = source.staging_state_dir
    try:
        root_info = os.stat(root)
    except OSError as exc:
        raise ReaderError("reader-unavailable",
                          "staging state directory is not available") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise ReaderError("reader-unavailable",
                          "staging state directory is not a directory")
    if root_info.st_mode & 0o022:
        raise ReaderError("reader-unavailable",
                          "staging state directory is group/other writable")
    for relative in ("mise-a-jour", "release",
                     os.path.join("release", "reports")):
        directory = os.path.join(root, relative)
        if not os.path.exists(directory):
            continue
        try:
            info = os.stat(directory)
        except OSError as exc:
            raise ReaderError("reader-unavailable",
                              "source directory is unreadable") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            # Un dossier intermediaire modifiable par le groupe ou les autres
            # permet de remplacer les attestations : toute la source est
            # refusee, pas seulement le fichier.
            raise ReaderError("reader-unavailable",
                              "source directory is group/other writable")
    patterns = (os.path.join(root, "mise-a-jour", "actif-*.json"),
                os.path.join(root, "release", "reports", "*.json"))
    paths: list[str] = []
    for pattern in patterns:
        paths.extend(glob.glob(pattern))
    # La borne garde les fichiers les plus recents : ce sont eux qui portent le
    # dernier deploiement, pas les plus anciens.
    try:
        paths.sort(key=os.path.getmtime, reverse=True)
    except OSError:
        paths.sort()
    refusals: list[dict] = []
    if len(paths) > MAX_STAGING_FILES:
        paths = paths[:MAX_STAGING_FILES]
        refusals.append({"code": "source-bounds", "source": "staging"})
    candidates: list[Candidate] = []
    for path in paths:
        try:
            if os.path.basename(path).startswith("actif-"):
                candidates.extend(_actif_candidates(source, path))
            else:
                candidates.extend(_release_candidates(source, path))
        except (OSError, OverflowError, ReaderError, TypeError, ValueError):
            # Un fichier illisible ou incomplet ne prouve rien : il est
            # consigne sans contenu, jamais interprete comme un succes.
            refusals.append({"code": "source-invalid",
                             "source": os.path.basename(path)})
    return candidates, refusals


def _approval_candidates(source: Source, approvals: Any, *,
                         now: dt.datetime) -> tuple[list[Candidate], list[dict]]:
    records = getattr(approvals, "approvals", None)
    if not isinstance(records, (list, tuple)):
        raise ReaderError("acceptances-unavailable",
                          "the approval projection has no approvals list")
    candidates: list[Candidate] = []
    refusals: list[dict] = []
    for record in records:
        try:
            artifact = record.artifact
            repo = _check_repo(artifact.repo, "approval artifact repo")
            if source.repos and repo not in source.repos:
                raise ValueError("approval repository is outside the identity")
            revision = _check_artifact(artifact.revision)
            path = _require_text(artifact.path, "approval artifact path",
                                 maximum=512)
            comment_id = _as_int(record.comment_id, "comment_id", minimum=1)
            author = _require_text(record.author, "author", maximum=200)
            created_at = _utc(record.created_at)
            body_sha256 = _check_digest(record.body_sha256, "body_sha256")
            url = _require_text(record.url, "url", maximum=2048)
            state = str(record.state or STATE_CURRENT)
            if state not in OBSERVATION_STATES:
                raise ValueError("approval state: unknown")
            superseded_by = _text(record.superseded_by, maximum=512)
            revoked_by = _text(record.revoked_by, maximum=400)
            expires_at = record.expires_at
            revoked_reason = ""
            if state == STATE_REVOKED:
                revoked_reason = "source-revoked:%s" % (revoked_by or "unknown")
            elif state == STATE_CURRENT and expires_at is not None \
                    and now > _utc(expires_at):
                state = STATE_REVOKED
                revoked_reason = "acceptance-expired"
            # Le digest de source couvre le contenu immuable du commentaire,
            # jamais l'etat derive (une supersession ou une revocation
            # legitimes ne sont pas une edition de la source).
            source_digest = _sha256_json({
                "comment_id": comment_id,
                "author": author,
                "created_at": _iso(created_at),
                "body_sha256": body_sha256,
                "repo": repo,
                "revision": revision,
                "path": path,
                "url": url,
                "expires_at": _iso(_utc(expires_at)) if expires_at else "",
            })
            ident = "approval-%d-%s" % (
                comment_id, hashlib.sha256(
                    ("%s\0%s\0%s" % (repo, revision, path)).encode("utf-8")
                ).hexdigest()[:16])
            observation = Observation(
                id=ident, kind=KIND_PASS,
                environment=ENVIRONMENT_ACCEPTANCE, work_item_id=None,
                app="", artifact=revision, source_revision=path,
                actor=author, observed_at=created_at,
                expires_at=_utc(expires_at) if expires_at is not None else None,
                provenance={"kind": PROVENANCE_GITHUB, "repo": repo,
                            "comment_id": comment_id, "url": url,
                            "body_sha256": body_sha256, "path": path},
                source_digest=source_digest)
            candidates.append(Candidate(
                observation, state=state, superseded_by=superseded_by,
                revoked_reason=revoked_reason))
        except (AttributeError, TypeError, ValueError):
            refusals.append({"code": "source-invalid",
                             "source": "approval"})
    return candidates, refusals


def _collect_acceptances(source: Source, approvals: Any, *,
                         now: dt.datetime) -> tuple[list[Candidate], list[dict]]:
    if approvals is None:
        from . import process_approvals as approvals_mod
        try:
            approvals = approvals_mod.local_projection()
        except approvals_mod.ApprovalError as exc:
            raise ReaderError(
                "acceptances-unavailable",
                "the approval projection is unavailable (%s)" % exc.code) from exc
    return _approval_candidates(source, approvals, now=now)


def _dedupe_refusals(refusals: Iterable[Mapping]) -> tuple[Mapping, ...]:
    seen: set[tuple[str, str]] = set()
    kept: list[dict] = []
    for refusal in refusals:
        code = _text(refusal.get("code"), maximum=64)
        source = _text(refusal.get("source"), maximum=300)
        if not code or (code, source) in seen:
            continue
        seen.add((code, source))
        kept.append({"code": code, "source": source})
        if len(kept) >= MAX_REFUSALS:
            break
    return tuple(kept)


# ---------------------------------------------------------------------------
# derivation de la projection
# ---------------------------------------------------------------------------

def _collector_info(source: Source, collector: Mapping | None) -> dict:
    if collector is not None:
        return dict(collector)
    return {"host": socket.gethostname(),
            "identity": os.environ.get("AGENT_MAIL_NAME", ""),
            "reader": source.reader,
            "version": _version()}


def _version() -> str:
    try:
        from . import version as version_fn
        return str(version_fn())
    except Exception:  # pragma: no cover - version is optional
        return ""


def build_projection(source: Source, journal: Journal, *,
                     now: dt.datetime | None = None,
                     refusals: Iterable[Mapping] = (),
                     collector: Mapping | None = None) -> Projection:
    """Derive la projection du journal ; n'ecrit ni le journal ni le fichier."""
    current = _utc(now)
    observations, revocations = journal.derive()
    # La projection est une vue : tous les courants, plus une fenetre
    # d'historique. Le journal append-only garde l'histoire complete ; sans
    # fenetre, une histoire qui grandit rendrait la projection impubliable
    # pour toujours (verrou permanent).
    current_records = [item for item in observations
                       if item.state == STATE_CURRENT]
    if len(current_records) > MAX_OBSERVATIONS:
        raise ProjectionError("projection-bounds", "too many current observations")
    history = sorted(
        (item for item in observations if item.state != STATE_CURRENT),
        key=lambda item: (_iso(item.observed_at), item.id), reverse=True)
    # La fenetre ne doit jamais laisser une reference `superseded_by` ouverte :
    # un enregistrement natif n'entre que si son successeur est deja dans la
    # vue. Le total producteur/lecteur est borne par MAX_OBSERVATIONS.
    history_limit = min(MAX_HISTORY,
                        max(0, MAX_OBSERVATIONS - len(current_records)))
    kept: dict[str, Observation] = {item.id: item
                                    for item in current_records}
    for item in history[:history_limit]:
        # Une reference `superseded_by` non vide doit rester fermee, meme
        # quand l'enregistrement a ensuite ete revoque (`derive` conserve le
        # lien) : sinon le lecteur refuse toute la projection.
        if item.superseded_by \
                and item.provenance.get("kind") != PROVENANCE_GITHUB \
                and item.superseded_by not in kept:
            continue
        kept[item.id] = item
    observations = tuple(sorted(
        kept.values(), key=lambda item: (_iso(item.observed_at), item.id)))
    kept_ids = set(kept)
    revocations = tuple(revocation for revocation in revocations
                        if revocation["target"] in kept_ids)
    if len(revocations) > MAX_REVOCATIONS:
        raise ProjectionError("projection-bounds", "too many revocations")
    payload = {
        "schema": SCHEMA,
        "authority": source.authority,
        "source": {
            "journal_id": source.journal_id,
            "repos": list(source.repos),
            "issues": list(source.issues),
            "source_digest": journal.digest(),
            "entries": len(journal.entries),
        },
        "generated_at": _iso(current),
        "expires_at": _iso(current + dt.timedelta(seconds=source.ttl_s)),
        "collector": _collector_info(source, collector),
        "observations": [observation.as_dict()
                         for observation in observations],
        "revocations": [dict(revocation) for revocation in revocations],
        "refusals": [dict(refusal)
                     for refusal in _dedupe_refusals(refusals)],
    }
    projection = Projection(
        source=payload["source"], generated_at=current,
        expires_at=current + dt.timedelta(seconds=source.ttl_s),
        observations=observations, revocations=revocations,
        refusals=tuple(payload["refusals"]),
        collector=payload["collector"], authority=source.authority,
        path=source.projection)
    return replace(projection, digest=_sha256_json(projection._payload()))


def _merge(source: Source, journal: Journal, candidates: list[Candidate],
           refusals: list[dict], *, now: dt.datetime) -> Projection:
    def state_only(exc: JournalError) -> bool:
        """Une transition deja faite n'est pas une panne d'ecriture."""
        return exc.code in ("observation-missing", "observation-revoked",
                            "observation-superseded")

    known = {observation.id: observation
             for observation in journal.derive()[0]}
    by_id = {candidate.observation.id: candidate for candidate in candidates}
    for candidate in candidates:
        observation = candidate.observation
        existing = known.get(observation.id)
        if existing is None:
            if candidate.state != STATE_CURRENT:
                # Un etat deja supersede/revoque a la source n'entre pas au
                # journal : il n'a jamais ete une preuve courante.
                continue
            try:
                journal.record(observation, at=now)
            except JournalError as exc:
                if exc.code not in ("observation-edited",
                                    "observation-invalid"):
                    # Une panne d'ecriture du journal n'est pas un temoin de
                    # source : elle interrompt le refresh, la projection
                    # precedente reste en place et perit.
                    raise
                refusals.append({"code": exc.code,
                                 "source": observation.id})
            continue
        if existing.source_digest != observation.source_digest:
            refusals.append({"code": "observation-edited",
                             "source": observation.id})
            if existing.state == STATE_CURRENT:
                try:
                    journal.revoke(observation.id, reason="source-edited",
                                   at=now, actor="collector")
                except JournalError as exc:
                    if not state_only(exc):
                        raise
            continue
        if existing.state != STATE_CURRENT:
            if candidate.state == STATE_CURRENT:
                code = "observation-revoked" \
                    if existing.state == STATE_REVOKED \
                    else "observation-superseded"
                refusals.append({"code": code, "source": observation.id})
            continue
        if candidate.state == STATE_REVOKED:
            journal.revoke(observation.id,
                           reason=candidate.revoked_reason or "source-revoked",
                           at=now, actor="collector")
        elif candidate.state == STATE_SUPERSEDED:
            journal.supersede(observation.id,
                              candidate.superseded_by or "source-superseded",
                              reason="source-superseded", at=now)

    derived = journal.derive()[0]
    # Une acceptation C qui disparait de la projection fraiche est revoquee :
    # un commentaire supprime ne peut pas rester une preuve.
    if source.acceptances:
        for observation in derived:
            if observation.state != STATE_CURRENT:
                continue
            if observation.provenance.get("kind") != PROVENANCE_GITHUB:
                continue
            if observation.id not in by_id:
                try:
                    journal.revoke(observation.id, reason="source-disappeared",
                                   at=now, actor="collector")
                except JournalError as exc:
                    if not state_only(exc):
                        raise

    # Un deploiement est remplace par le suivant du meme environnement, sur le
    # meme lot et le meme projet ; il n'expire jamais a l'age.
    derived = journal.derive()[0]
    groups: dict[tuple[str, int, str], list[Observation]] = {}
    for observation in derived:
        if observation.state != STATE_CURRENT:
            continue
        if observation.kind != KIND_DEPLOYED \
                or observation.work_item_id is None:
            continue
        groups.setdefault((observation.app, observation.work_item_id,
                           observation.environment), []).append(observation)
    for rows in groups.values():
        rows.sort(key=lambda item: (_iso(item.observed_at), item.id))
        latest = rows[-1]
        for older in rows[:-1]:
            try:
                journal.supersede(older.id, latest.id,
                                  reason="next-deployment", at=now)
            except JournalError as exc:
                if not state_only(exc):
                    raise
    return build_projection(source, journal, now=now, refusals=refusals)


def _collect_sources(source: Source, approvals: Any, *,
                     now: dt.datetime) -> tuple[list[Candidate], list[dict]]:
    candidates: list[Candidate] = []
    refusals: list[dict] = []
    if source.staging_state_dir:
        found, witnesses = _collect_staging(source)
        candidates.extend(found)
        refusals.extend(witnesses)
    if source.acceptances:
        found, witnesses = _collect_acceptances(source, approvals, now=now)
        candidates.extend(found)
        refusals.extend(witnesses)
    if len(candidates) > MAX_OBSERVATIONS:
        # Refuser avant toute ecriture : une collecte qui depasse la borne de
        # projection ne doit pas laisser un journal a moitie ecrit.
        raise ReaderError("reader-bounds", "too many observations collected")
    return candidates, refusals


def _check_acceptances_disabled(source: Source, journal: Journal) -> None:
    """Un journal qui porte des acceptations ne se relit pas sans #4006.

    Desactiver ``acceptances`` ne doit pas figer une acceptation courante pour
    toujours : la source est requise tant que le journal en porte une, sinon
    l'operateur revoque explicitement l'enregistrement.
    """
    if source.acceptances:
        return
    for observation in journal.derive()[0]:
        if observation.state == STATE_CURRENT \
                and observation.provenance.get("kind") == PROVENANCE_GITHUB:
            raise ReaderError(
                "reader-config",
                "acceptance records exist but acceptances is disabled: "
                "re-enable it or revoke them explicitly")


def refresh(source: Source, *, now: dt.datetime | None = None,
            dry_run: bool = False, approvals: Any = None,
            collector: Mapping | None = None) -> Projection:
    """Collecte les sources et ecrit le journal ; ne remplace pas le fichier.

    ``dry_run`` ne touche ni le journal ni la projection. La collecte entiere
    (projection #4006 comprise) se fait **sous le verrou du journal** : une
    lecture d'acceptations concurrente d'une ecriture ne peut pas revoquer une
    approbation plus fraiche. Une source configuree indisponible fait echouer
    le rafraichissement, la projection precedente reste en place et perime
    (fail-closed).
    """
    current = _utc(now)
    journal = Journal(source.journal, create=True, write=not dry_run)
    if dry_run:
        journal.reload()
        _check_acceptances_disabled(source, journal)
        candidates, refusals = _collect_sources(source, approvals, now=current)
        return _merge(source, journal, candidates, refusals, now=current)
    with journal.lock():
        journal.reload()
        _check_acceptances_disabled(source, journal)
        candidates, refusals = _collect_sources(source, approvals, now=current)
        return _merge(source, journal, candidates, refusals, now=current)


# ---------------------------------------------------------------------------
# projection locale (hotes d'agents)
# ---------------------------------------------------------------------------

def _read_projection_file(path: str, *, owner_uid: int | None) -> bytes:
    path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(path) or "."
    if os.path.islink(parent):
        raise ProjectionError("projection-unreadable",
                              "projection directory is a symlink")
    try:
        parent_info = os.stat(parent)
    except OSError as exc:
        raise ProjectionError("projection-unreadable",
                              "projection directory is unreadable") from exc
    if not stat.S_ISDIR(parent_info.st_mode):
        raise ProjectionError("projection-unreadable",
                              "projection directory expected")
    if parent_info.st_mode & 0o022:
        raise ProjectionError("projection-permissions",
                              "projection directory is group/other writable")
    if owner_uid is not None and parent_info.st_uid != owner_uid:
        raise ProjectionError("projection-owner",
                              "projection directory owner mismatch")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise ProjectionError("projection-missing",
                              "projection not found: %s" % path) from exc
    except OSError as exc:
        raise ProjectionError("projection-unreadable",
                              "projection is unreadable: %s" % path) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ProjectionError("projection-unreadable",
                                  "projection: regular file expected")
        if info.st_mode & 0o022:
            raise ProjectionError("projection-permissions",
                                  "projection is group/other writable")
        if owner_uid is not None and info.st_uid != owner_uid:
            raise ProjectionError("projection-owner",
                                  "projection owner mismatch")
        if info.st_size > MAX_PROJECTION_BYTES:
            raise ProjectionError("projection-bounds", "projection too large")
        raw = os.read(fd, MAX_PROJECTION_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_PROJECTION_BYTES:
        raise ProjectionError("projection-bounds", "projection too large")
    return raw


def _parse_projection(payload: Any, *, now: dt.datetime,
                      max_age_s: int, max_ttl_s: int) -> Projection:
    if not isinstance(payload, Mapping):
        raise ProjectionError("projection-invalid", "JSON object expected")
    if payload.get("schema") != SCHEMA:
        raise ProjectionError("projection-invalid", "unknown projection schema")
    try:
        authority = _check_authority(payload.get("authority"))
        source = payload.get("source")
        if not isinstance(source, Mapping):
            raise ValueError("source: JSON object expected")
        if "repos" not in source or "issues" not in source:
            raise ValueError("source: repos and issues are required")
        journal_id = _check_ident(source.get("journal_id"))
        repos = _check_repos(source.get("repos"))
        issues = _check_issues(source.get("issues"))
        source_digest = _check_digest(source.get("source_digest"),
                                      "source_digest")
        entries = _as_int(source.get("entries"), "entries", minimum=0)
        generated = _parse_ts(payload.get("generated_at"), "generated_at")
        expires = _parse_ts(payload.get("expires_at"), "expires_at")
        ttl = (expires - generated).total_seconds()
        if ttl <= 0 or ttl > max_ttl_s:
            raise ValueError("projection ttl: out of bounds")
        collector_raw = payload.get("collector") or {}
        if not isinstance(collector_raw, Mapping):
            raise ValueError("collector: JSON object expected")
        collector = {
            "host": _text(collector_raw.get("host"), maximum=200),
            "identity": _text(collector_raw.get("identity"), maximum=200),
            "reader": _text(collector_raw.get("reader"), maximum=200),
            "version": _text(collector_raw.get("version"), maximum=100),
        }
        observations_raw = payload.get("observations")
        if not isinstance(observations_raw, list):
            raise ValueError("observations: list expected")
        if len(observations_raw) > MAX_OBSERVATIONS:
            raise ValueError("too many observations")
        observations = tuple(_parse_observation(item)
                             for item in observations_raw)
        revocations_raw = payload.get("revocations") or []
        if not isinstance(revocations_raw, list):
            raise ValueError("revocations: list expected")
        if len(revocations_raw) > MAX_REVOCATIONS:
            raise ValueError("too many revocations")
        revocations = tuple(_parse_revocation(item)
                            for item in revocations_raw)
        refusals_raw = payload.get("refusals") or []
        if not isinstance(refusals_raw, list):
            raise ValueError("refusals: list expected")
        if len(refusals_raw) > MAX_REFUSALS:
            raise ValueError("too many refusals")
        refusals = tuple(_parse_refusal(item) for item in refusals_raw)
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-invalid", str(exc)) from exc
    if now < generated - dt.timedelta(seconds=CLOCK_SKEW_S):
        raise ProjectionError("projection-future", "projection is in the future")
    if now > expires:
        raise ProjectionError("projection-stale", "projection has expired")
    if (now - generated).total_seconds() > max_age_s:
        raise ProjectionError("projection-stale",
                              "projection is older than allowed")
    seen: set[str] = set()
    revocation_by_id = {revocation["id"]: revocation
                        for revocation in revocations}
    known = {observation.id: observation for observation in observations}
    for observation in observations:
        if observation.id in seen:
            raise ProjectionError("projection-invalid",
                                  "duplicate observation id")
        seen.add(observation.id)
        if observation.revoked_by:
            revocation = revocation_by_id.get(observation.revoked_by)
            if revocation is None or revocation["target"] != observation.id:
                raise ProjectionError("projection-invalid",
                                      "revoked_by: unknown revocation")
        if observation.superseded_by \
                and observation.provenance.get("kind") != PROVENANCE_GITHUB:
            replacement = known.get(observation.superseded_by)
            if replacement is None:
                raise ProjectionError("projection-invalid",
                                      "superseded_by: unknown observation")
            if (replacement.app, replacement.work_item_id,
                    replacement.environment) != (observation.app,
                                                 observation.work_item_id,
                                                 observation.environment):
                raise ProjectionError("projection-invalid",
                                      "superseded_by: unrelated observation")
            if replacement.observed_at < observation.observed_at:
                raise ProjectionError("projection-invalid",
                                      "superseded_by: older observation")
    for revocation in revocations:
        if revocation["target"] not in known:
            raise ProjectionError("projection-invalid",
                                  "revocation target: unknown observation")
        target = next(observation for observation in observations
                      if observation.id == revocation["target"])
        if target.state != STATE_REVOKED \
                or target.revoked_by != revocation["id"]:
            raise ProjectionError("projection-invalid",
                                  "revocation is not applied to its target")
    return Projection(
        source={"journal_id": journal_id, "repos": list(repos),
                "issues": list(issues), "source_digest": source_digest,
                "entries": entries},
        generated_at=generated, expires_at=expires,
        observations=observations, revocations=revocations, refusals=refusals,
        collector=collector, authority=authority)


def _check_identity(projection: Projection,
                    identity: ObservationIdentity) -> None:
    if not identity.authority or projection.authority != identity.authority:
        raise ProjectionError("projection-source",
                              "projection authority does not match the identity")
    if str(projection.source.get("journal_id") or "") != identity.journal_id:
        raise ProjectionError("projection-source",
                              "projection journal does not match the identity")
    if tuple(projection.source.get("repos") or ()) != identity.repos:
        raise ProjectionError("projection-source",
                              "projection repositories do not match the identity")
    if tuple(projection.source.get("issues") or ()) != identity.issues:
        raise ProjectionError("projection-source",
                              "projection issues do not match the identity")
    allowed = set(identity.repos)
    for observation in projection.observations:
        provenance = observation.provenance
        if provenance.get("kind") == PROVENANCE_GITHUB:
            if provenance.get("repo") not in allowed:
                raise ProjectionError(
                    "projection-source",
                    "an observation comes from outside the identity")


def load_projection(config: Any = None, *, path: str | None = None,
                    now: dt.datetime | None = None,
                    max_age_s: int | None = None,
                    owner_uid: int | None = None,
                    identity: ObservationIdentity | None = None) -> Projection:
    """Lit et valide la projection locale ; toute absence est un refus."""
    section = _section(config) if (config is not None or path is None) else {}
    if identity is None and (config is not None or path is None):
        identity = identity_from_config(section)
    if path is None:
        path = projection_path(config)
    path = os.path.abspath(os.path.expanduser(path))
    try:
        if max_age_s is None:
            max_age_s = _as_int(section.get("max_age_s", DEFAULT_TTL_S),
                                "max_age_s", minimum=1, maximum=MAX_AGE_S)
        if owner_uid is None and section.get("owner_uid") is not None:
            owner_uid = _as_int(section.get("owner_uid"), "owner_uid",
                                minimum=0)
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-source", str(exc)) from exc
    if owner_uid is None:
        # Un fichier de projection n'est pas signe : par defaut il doit
        # appartenir au lecteur. Une copie synchronisee par root declare
        # explicitement `owner_uid: 0`.
        owner_uid = os.getuid()
    current = _utc(now)
    raw = _read_projection_file(path, owner_uid=owner_uid)
    try:
        payload = jcs.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-invalid", "invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ProjectionError("projection-invalid", "JSON object expected")
    digest = payload.get("digest")
    body = {key: value for key, value in payload.items() if key != "digest"}
    try:
        expected = _sha256_json(body)
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-invalid", "invalid JSON value") from exc
    if not isinstance(digest, str) or not hmac.compare_digest(digest, expected):
        raise ProjectionError("projection-digest", "projection digest mismatch")
    projection = _parse_projection(body, now=current, max_age_s=max_age_s,
                                   max_ttl_s=MAX_TTL_S)
    if identity is None:
        raise ProjectionError("projection-source",
                              "expected source identity is required")
    _check_identity(projection, identity)
    return replace(projection, digest=digest, path=path)


def local_projection(config: Any = None, *,
                     now: dt.datetime | None = None) -> Projection:
    """Lecture de la projection de l'hote (aucun reseau, aucun secret)."""
    return load_projection(config, now=now)


# ---------------------------------------------------------------------------
# consommation (fail-closed)
# ---------------------------------------------------------------------------

def read_sources(projection: Projection, *, work_item_id: int,
                 app: str, now: dt.datetime | None = None) -> list[dict]:
    """Les observations courantes du lot, dans la forme lue par les gardes.

    Une observation globale (acceptation humaine sans lot) est visible de tous
    les lots ; une observation d'un autre lot ou d'un autre projet n'est jamais
    visible. Les etats revoques, remplaces, perimes et futurs sont exclus :
    une garde ne recoit que des preuves valides a l'instant de la lecture.
    """
    lot = _as_int(work_item_id, "work_item_id", minimum=1)
    wanted_app = _check_app(app, allow_empty=True)
    current = _utc(now)
    rows = []
    for observation in projection.current(current):
        if observation.work_item_id not in (None, lot):
            continue
        if observation.app and observation.app != wanted_app:
            continue
        rows.append(observation.as_source())
    return rows


def require_observation(projection: Projection, ident: str, *,
                        work_item_id: int | None = None,
                        app: str = "", artifact: str = "",
                        environment: str = "", kind: str = "",
                        now: dt.datetime | None = None) -> Observation:
    """Rend l'observation exacte, ou refuse avec un code stable."""
    try:
        ident = _check_id(ident)
    except (TypeError, ValueError) as exc:
        raise ObservationRefusal("observation-invalid", str(exc)) from exc
    observation = projection.by_id(ident)
    if observation is None:
        # La fenetre d'historique peut avoir retire un enregistrement revoque :
        # la liste des revocations reste la temoin terminal.
        if any(revocation["target"] == ident
               for revocation in projection.revocations):
            raise ObservationRefusal("observation-revoked",
                                     "observation %s was revoked" % ident)
        raise ObservationRefusal("observation-missing",
                                 "observation %s is unknown" % ident)
    current = _utc(now)
    if observation.state == STATE_REVOKED:
        raise ObservationRefusal("observation-revoked",
                                 "observation %s was revoked" % ident)
    if observation.state == STATE_SUPERSEDED:
        raise ObservationRefusal("observation-superseded",
                                 "observation %s was superseded" % ident)
    if _future(observation, current):
        raise ObservationRefusal("observation-future",
                                 "observation %s is in the future" % ident)
    if _expired(observation, current):
        raise ObservationRefusal("observation-expired",
                                 "observation %s has expired" % ident)
    try:
        if work_item_id is not None:
            lot = _as_int(work_item_id, "work_item_id", minimum=1)
            if observation.work_item_id not in (None, lot):
                raise ObservationRefusal(
                    "observation-source",
                    "observation %s is for another lot" % ident)
        if app:
            wanted_app = _check_app(app)
            if observation.app and observation.app != wanted_app:
                raise ObservationRefusal(
                    "observation-source",
                    "observation %s is for another app" % ident)
        if environment:
            wanted_env = _require_text(environment, "environment", maximum=16)
            if wanted_env not in OBSERVATION_ENVIRONMENTS:
                raise ObservationRefusal("observation-source",
                                         "unknown environment %r" % wanted_env)
            if observation.environment != wanted_env:
                raise ObservationRefusal(
                    "observation-source",
                    "observation %s is for another environment" % ident)
        if kind:
            wanted_kind = _require_text(kind, "kind", maximum=16)
            if wanted_kind not in OBSERVATION_KINDS:
                raise ObservationRefusal("observation-source",
                                         "unknown kind %r" % wanted_kind)
            if observation.kind != wanted_kind:
                raise ObservationRefusal(
                    "observation-source",
                    "observation %s is of another kind" % ident)
        if artifact:
            wanted_artifact = _check_artifact(artifact)
            if observation.artifact != wanted_artifact:
                raise ObservationRefusal(
                    "observation-stale",
                    "observation %s is for another revision" % ident)
    except ObservationRefusal:
        raise
    except (TypeError, ValueError) as exc:
        raise ObservationRefusal("observation-invalid", str(exc)) from exc
    return observation


def summary(projection: Projection, *, written: str = "",
            now: dt.datetime | None = None) -> dict:
    """Resume stable pour ``check`` / ``refresh`` (aucun secret)."""
    current = _utc(now)
    states = {state: 0 for state in OBSERVATION_STATES}
    kinds: dict[str, int] = {}
    environments: dict[str, int] = {}
    expired = 0
    for observation in projection.observations:
        states[observation.state] = states.get(observation.state, 0) + 1
        kinds[observation.kind] = kinds.get(observation.kind, 0) + 1
        environments[observation.environment] = \
            environments.get(observation.environment, 0) + 1
        if observation.state == STATE_CURRENT and _expired(observation, current):
            expired += 1
    return {
        "schema": SCHEMA,
        "authority": projection.authority,
        "projection": projection.path,
        "generated_at": _iso(projection.generated_at),
        "expires_at": _iso(projection.expires_at),
        "digest": projection.digest,
        "source": projection.meta(),
        "collector": dict(projection.collector),
        "counts": {
            "observations": len(projection.observations),
            "current": states.get(STATE_CURRENT, 0),
            "superseded": states.get(STATE_SUPERSEDED, 0),
            "revoked": states.get(STATE_REVOKED, 0),
            "expired": expired,
            "revocations": len(projection.revocations),
            "refusals": len(projection.refusals),
            "kinds": kinds,
            "environments": environments,
        },
        "written": written,
    }


# ---------------------------------------------------------------------------
# ecriture atomique (bureau-goal)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _locked(path: str):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _previous_projection_state(path: str) -> tuple[dt.datetime, int] | None:
    """Date et nombre d'entrees du journal d'une projection existante.

    Sert a refuser une republication plus ancienne dans la meme seconde : le
    journal est append-only, une projection qui voit **moins** d'entrees que
    le fichier en place est en retard, jamais plus fraiche.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROJECTION_BYTES:
            return None
        raw = os.read(fd, MAX_PROJECTION_BYTES + 1)
    finally:
        os.close(fd)
    try:
        payload = json.loads(raw)
        generated = _parse_ts(payload.get("generated_at"), "generated_at")
        source = payload.get("source")
        entries = source.get("entries") if isinstance(source, Mapping) else None
        if isinstance(entries, bool) or not isinstance(entries, int):
            entries = -1
        return generated, entries
    except (TypeError, ValueError):
        return None


def write_projection(projection: Projection, path: str, *,
                     mode: int = 0o644) -> str:
    """Ecrit la projection atomiquement ; rend ``written`` ou ``not-newer``.

    Le fichier est lisible par les hotes (0644) mais jamais modifiable par le
    groupe ou les autres. Aucun secret n'y est ecrit : uniquement des
    identifiants, revisions, empreintes et temoins sans contenu de source.
    """
    path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(path) or "."
    if os.path.islink(path):
        raise ProjectionError("projection-unreadable",
                              "projection path is a symlink")
    if os.path.islink(parent):
        raise ProjectionError("projection-unreadable",
                              "projection directory is a symlink")
    os.makedirs(parent, mode=0o755, exist_ok=True)
    if os.stat(parent).st_mode & 0o022:
        raise ProjectionError("projection-permissions",
                              "projection directory is group/other writable")
    if mode & 0o022:
        raise ProjectionError("projection-permissions",
                              "projection mode must not be group/other writable")
    data = json.dumps(projection.as_dict(), ensure_ascii=False,
                      sort_keys=True, indent=2) + "\n"
    if len(data.encode("utf-8")) > MAX_PROJECTION_BYTES:
        # Une projection que les hotes refuseraient ne doit pas remplacer la
        # precedente : le collecteur echoue et l'ancienne perime (fail-closed).
        raise ProjectionError("projection-bounds", "projection too large")
    with _locked(path + ".lock"):
        if os.path.exists(path):
            previous = _previous_projection_state(path)
            if previous is not None:
                previous_ts, previous_entries = previous
                if previous_ts > projection.generated_at:
                    return "not-newer"
                # Le journal est append-only : une projection qui voit moins
                # d'entrees que le fichier en place est en retard, meme si son
                # horodatage est plus recent. Elle ne l'ecrase jamais (pas de
                # retour en arriere d'une revocation ni d'un deploiement).
                if previous_entries > int(
                        projection.source.get("entries") or 0):
                    return "not-newer"
        fd, temporary = tempfile.mkstemp(prefix=".tmp-observations-", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
            _fsync_dir(parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    return "written"


__all__ = [
    "Candidate", "CLOCK_SKEW_S", "DEFAULT_TTL_S", "ENVIRONMENT_ACCEPTANCE",
    "ENVIRONMENT_PREPROD", "ENVIRONMENT_STAGING", "Journal", "JournalError",
    "KIND_DEPLOYED", "KIND_FAIL", "KIND_PASS", "Observation",
    "ObservationError", "ObservationIdentity", "ObservationRefusal",
    "OBSERVATION_ENVIRONMENTS", "OBSERVATION_KINDS", "OBSERVATION_STATES",
    "PROVENANCE_GITHUB", "PROVENANCE_RELEASE", "PROVENANCE_STAGING",
    "Projection", "ProjectionError", "QA_TTL_S", "ReaderError", "SCHEMA",
    "Source", "STATE_CURRENT", "STATE_REVOKED", "STATE_SUPERSEDED",
    "build_projection", "identity_from_config", "journal_path",
    "load_projection", "local_projection", "projection_path", "read_sources",
    "refresh", "require_observation", "source_from_config", "summary",
    "write_projection",
]
