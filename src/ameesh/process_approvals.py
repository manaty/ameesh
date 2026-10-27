# SPDX-License-Identifier: AGPL-3.0-only
"""Pont d'approbation IMA/GitHub, avec revocation (#4006, C de #2846).

Le proprietaire a tranche l'hebergement (decision #143364, option a) : **un
seul lecteur, sur la VM bureau-goal**, avec un jeton GitHub existant en
lecture des issues du depot IMA. Les hotes d'agents ne parlent jamais a
GitHub : ils lisent une **projection locale en lecture seule**, produite par
le bureau. La projection est fail-closed : absente, illisible, perimee, ou
issue d'une autre source, elle est refusee.

Ce module ne decide pas a la place d'IMA. Il lit les enregistrements
d'approbation que le proprietaire a ecrits sur l'issue IMA #704 et les
normalise :

* auteur : le commentaire doit venir d'un approbateur configure, de type
  ``User`` (jamais un bot) ;
* portee : une URL GitHub ``blob/<revision>/<chemin>`` ou une reference
  ``owner/repo@<revision>:<chemin>``. Une revision mobile (branche, tag) est
  refusee ; une revision courte est resolue par le SHA complet present dans
  le corps, sinon par l'API GitHub ;
* etat courant : la derniere approbation d'un meme ``(depot, chemin)``
  remplace les precedentes (supersession) ; une revocation posterieure, par
  identifiant de commentaire ou par portee, revoque les approbations
  anterieures ; une approbation modifiee apres coup est refusee ;
* fraicheur : la projection porte ``generated_at`` / ``expires_at`` et un
  consommateur impose en plus son propre age maximal ;
* autorite : ``source=api`` n'est jamais une autorite ; seule la source
  ``github`` de la projection, elle-meme adossee a l'issue IMA, compte.

Aucun secret n'entre dans Git, dans la projection ou dans un message
d'erreur : la configuration **nomme** le fichier 0600 du jeton, elle n'en
contient jamais la valeur. Le lecteur reseau tourne sur bureau-goal ; les
hotes ne lisent qu'un fichier JSON, avec des permissions verifiees.

Format de projection (``ameesh-approvals/1``) : ``schema``, ``authority``
(``ima``), ``source`` (depot, issue, etat, ``source_digest``), ``generated_at``,
``expires_at``, ``approvers``, ``approvals``, ``revocations``, ``refusals``,
``reader`` et ``digest`` (JCS + SHA-256 du reste). Les approbations sont
normalisees ; les refus sont des temoins sans contenu de commentaire.

Codes de refus stables (ils ne remplacent pas les codes Process de #4007) :

* lecteur : ``reader-config``, ``reader-token``, ``reader-timeout``,
  ``reader-unavailable``, ``reader-http``, ``reader-invalid``,
  ``reader-bounds`` ;
* projection : ``projection-missing``, ``projection-unreadable``,
  ``projection-permissions``, ``projection-owner``, ``projection-bounds``,
  ``projection-invalid``, ``projection-digest``, ``projection-stale``,
  ``projection-future``, ``projection-source`` ;
* approbation : ``approval-missing``, ``approval-stale``,
  ``approval-revoked``, ``approval-superseded``, ``approval-ambiguous``,
  ``approval-expired``, ``approval-api-not-authority``,
  ``approval-source`` ;
* temoins de normalisation : ``author-bot``, ``author-not-approver``,
  ``approval-edited``, ``approval-no-artifact``, ``approval-envelope-invalid``,
  ``mutable-revision``, ``revision-unresolved``, ``path-invalid``,
  ``out-of-scope``, ``revocation-unscoped``, ``revocation-time``.

Qualification : le code et les tests sur fixtures ne dependent pas de #4005.
La qualification sur les vrais enregistrements #704 se fait depuis
bureau-goal, une fois le jeton consigne au lot #4005 (voir
``tests/test_process_approvals_704.py``).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

from . import config as config_mod
from . import httpkit, jcs

#: schema de la projection locale
SCHEMA = "ameesh-approvals/1"
#: schema d'un bloc structure dans un commentaire (avenir)
ENVELOPE_SCHEMA = "ameesh-approval/1"
#: l'autorite reste IMA ; la projection n'en est qu'une lecture
AUTHORITY = "ima"
#: source acceptee par le consommateur ; ``api`` ne l'est jamais
SOURCE_GITHUB = "github"
SOURCE_API = "api"

#: bornes de lecture
MAX_PROJECTION_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_COMMENTS = 1000
MAX_PAGES = 20
MAX_ARTIFACTS_PER_COMMENT = 32
MAX_REFUSALS = 2000
MAX_TTL_S = 3600
MAX_AGE_S = 86400
CLOCK_SKEW_S = 60
DEFAULT_TTL_S = 300
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_TOTAL_TIMEOUT_S = 30.0
DEFAULT_API_BASE = "https://api.github.com"

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_FULL_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_BLOB_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9_.-]{1,100})/blob/([^/\s]+)/([^\s)#?<>\"'`]+)")
_PIN_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100})"
    r"@([^:\s]+):([^\s,;)\]<>\"'`]+)")
#: Reference en prose stricte, utilisee par certains enregistrements #704
#: (« Reviewed artifact: `<sha>`, knowledge/... »). Le depot est celui de
#: l'issue ; le chemin doit commencer par un dossier de contrat connu.
_PLAIN_ARTIFACT_RE = re.compile(
    r"(?:reviewed artifact|approved artifact|reviewed contract|"
    r"approved proposal)\s*:\s*`?([0-9a-fA-F]{7,64})`?\s*,?\s*"
    r"((?:knowledge|docs|src|ameesh|scripts|tests)/[^\s,;)]+)", re.I)
_FULL_SHA_RE = re.compile(r"\b([0-9a-fA-F]{40})\b")
_COMMENT_REF_RE = re.compile(r"#issuecomment-([0-9]{1,20})")
#: Le marqueur s'arrete au mot « approval » (ou « approval recorded ») : une
#: demande (« Owner approval requested/needed/pending ») n'est pas un accord.
_APPROVAL_RE = re.compile(
    r"^\s*(?:#+\s*)?owner\s+approval(?:\s+recorded)?\s*(?:$|[\u2014\u2013:(.\-])",
    re.I)
_REVOKE_RE = re.compile(
    r"^\s*(?:#+\s*)?(?:owner\s+)?approval\s+(revoked|withdrawn|superseded)\b", re.I)
_ENVELOPE_RE = re.compile(r"<!--\s*ameesh-approval:v1\s*(\{.*?\})\s*-->", re.S)


class ApprovalError(RuntimeError):
    """Refus stable du pont : un code, un message sans secret."""

    code = "approval-error"

    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.detail = detail


class ReaderError(ApprovalError):
    """Le lecteur GitHub (bureau-goal) est indisponible ou refuse la lecture."""


class ProjectionError(ApprovalError):
    """La projection locale est absente, illisible, perimee ou invalide."""


class ApprovalRefusal(ApprovalError):
    """Une approbation ne couvre pas la demande courante."""


# ---------------------------------------------------------------------------
# temps
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


def _as_float(value: Any, what: str, *, minimum: float = 0.0,
              maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s: number expected" % what)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("%s: finite number expected" % what)
    if number < minimum or (maximum is not None and number > maximum):
        raise ValueError("%s: out of bounds" % what)
    return number


# ---------------------------------------------------------------------------
# formes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    """Un artefact approuve : depot + revision exacte + chemin relatif."""

    repo: str
    revision: str
    path: str

    def as_dict(self) -> dict:
        return {"repo": self.repo, "revision": self.revision, "path": self.path}

    def key(self) -> tuple:
        return (self.repo, self.revision, self.path)


@dataclass(frozen=True)
class Source:
    """Configuration du lecteur (bureau-goal seulement)."""

    repo: str
    issue: int
    approvers: tuple[str, ...]
    token_file: str
    projection: str
    artifact_repos: tuple[str, ...] = ()
    ttl_s: int = DEFAULT_TTL_S
    timeout_s: float = DEFAULT_TIMEOUT_S
    total_timeout_s: float = DEFAULT_TOTAL_TIMEOUT_S
    api_base: str = DEFAULT_API_BASE
    max_comments: int = MAX_COMMENTS
    reader: str = ""

    @property
    def repos(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.repo,) + self.artifact_repos))


@dataclass(frozen=True)
class ApprovalIdentity:
    """Identite attendue de la source, ancree dans la configuration locale.

    Le fichier de projection est auto-declare : il ne prouve pas sa provenance.
    Un hote refuse donc une projection dont le depot, l'issue, les approbateurs
    ou le perimetre d'artefacts ne correspondent pas a cette identite locale.
    """

    repo: str
    issue: int
    approvers: tuple[str, ...]
    artifact_repos: tuple[str, ...] = ()

    @property
    def repos(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.repo,) + self.artifact_repos))


@dataclass(frozen=True)
class Approval:
    """Une approbation normalisee, pour un seul artefact."""

    id: str
    comment_id: int
    author: str
    created_at: dt.datetime
    updated_at: dt.datetime
    url: str
    statement: str
    body_sha256: str
    artifact: Artifact
    state: str = "current"          # current | superseded | revoked
    superseded_by: str = ""
    revoked_by: str = ""
    expires_at: dt.datetime | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "comment_id": self.comment_id,
            "kind": "approval",
            "author": self.author,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "url": self.url,
            "statement": self.statement,
            "body_sha256": self.body_sha256,
            "artifact": self.artifact.as_dict(),
            "state": self.state,
            "superseded_by": self.superseded_by,
            "revoked_by": self.revoked_by,
            "expires_at": _iso(self.expires_at) if self.expires_at else None,
        }

    def as_source(self) -> dict:
        return self.as_dict()


@dataclass(frozen=True)
class Revocation:
    """Une revocation explicite, par identifiant de commentaire ou par portee."""

    id: str
    comment_id: int
    author: str
    created_at: dt.datetime
    updated_at: dt.datetime
    url: str
    statement: str
    body_sha256: str
    targets: tuple[int, ...] = ()
    scope: tuple[Artifact, ...] = ()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "comment_id": self.comment_id,
            "kind": "revocation",
            "author": self.author,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "url": self.url,
            "statement": self.statement,
            "body_sha256": self.body_sha256,
            "targets": list(self.targets),
            "scope": [a.as_dict() for a in self.scope],
        }


@dataclass(frozen=True)
class Projection:
    """La projection locale, deja validee et datee."""

    source: Mapping[str, Any]
    generated_at: dt.datetime
    expires_at: dt.datetime
    approvers: tuple[str, ...]
    approvals: tuple[Approval, ...]
    revocations: tuple[Revocation, ...]
    refusals: tuple[Mapping[str, Any], ...]
    digest: str = ""
    reader: Mapping[str, Any] = field(default_factory=dict)
    path: str = ""

    def current(self) -> tuple[Approval, ...]:
        return tuple(a for a in self.approvals if a.state == "current")

    def meta(self) -> dict:
        source = dict(self.source)
        return {
            "generated_at": _iso(self.generated_at),
            "expires_at": _iso(self.expires_at),
            "digest": self.digest,
            "source_digest": str(source.get("source_digest") or ""),
            "repo": str(source.get("repo") or ""),
            "issue": int(source.get("issue") or 0),
            "approvers": list(self.approvers),
        }

    def as_source(self) -> dict:
        return {"projection": self.meta(),
                "records": [a.as_source() for a in self.approvals]}

    def _payload(self) -> dict:
        return {
            "schema": SCHEMA,
            "authority": AUTHORITY,
            "source": dict(self.source),
            "generated_at": _iso(self.generated_at),
            "expires_at": _iso(self.expires_at),
            "approvers": list(self.approvers),
            "approvals": [a.as_dict() for a in self.approvals],
            "revocations": [r.as_dict() for r in self.revocations],
            "refusals": [dict(r) for r in self.refusals],
            "reader": dict(self.reader),
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
        section = config.get("approvals")
    else:
        section = getattr(config, "approvals", None)
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ProjectionError("projection-source", "approvals: JSON object expected")
    return dict(section)


def _require_text(value: Any, what: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise ValueError("%s: string expected" % what)
    text = value.strip()
    if not text:
        raise ValueError("%s: non-empty string expected" % what)
    if len(text) > maximum:
        raise ValueError("%s: too long" % what)
    return text


def _check_repo(value: Any, what: str) -> str:
    text = _require_text(value, what, maximum=201).lower()
    if not _REPO_RE.fullmatch(text):
        raise ValueError("%s: owner/repository expected" % what)
    return text


def _check_path(value: Any, what: str = "artifact path") -> str:
    text = _require_text(value, what, maximum=512)
    if text.startswith("/") or "\\" in text or "\x00" in text:
        raise ValueError("%s: repository-relative POSIX path expected" % what)
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("%s: path traversal refused" % what)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("%s: control character refused" % what)
    return text


def _check_api_base(value: Any) -> str:
    text = _require_text(value, "api_base", maximum=300).rstrip("/")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("api_base: http(s) URL expected")
    if parsed.scheme == "http" and not httpkit.is_loopback(parsed.hostname):
        raise ValueError("api_base: https required outside the loopback")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("api_base: origin only")
    return text


def source_from_config(section: Any) -> Source:
    """Construit et valide la configuration du lecteur (bureau-goal).

    La section attendue est ``cfg["approvals"]`` : ``projection``, ``source``
    (``repo``, ``issue``, ``approvers``, ``token_file``, ...). Un
    ``token_file`` est obligatoire ; une valeur de jeton dans la configuration
    est refusee (``reader-config``), jamais ignoree.
    """
    if not isinstance(section, Mapping):
        raise ReaderError("reader-config", "approvals: JSON object expected")
    if "token" in section or "token_value" in section:
        raise ReaderError("reader-config",
                          "a token value is never read from the configuration")
    raw = section.get("source")
    if raw is None:
        raw = section
    if not isinstance(raw, Mapping):
        raise ReaderError("reader-config", "approvals.source: JSON object expected")
    if "token" in raw or "token_value" in raw:
        raise ReaderError("reader-config",
                          "a token value is never read from the configuration")
    try:
        projection = _require_text(section.get("projection"), "projection",
                                   maximum=4096)
        if not os.path.isabs(os.path.expanduser(projection)):
            raise ValueError("projection: absolute path expected")
        token_file = _require_text(raw.get("token_file"), "token_file",
                                   maximum=4096)
        if not os.path.isabs(os.path.expanduser(token_file)):
            raise ValueError("token_file: absolute path expected")
        issue = _as_int(raw.get("issue"), "issue", minimum=1, maximum=10**9)
        repo = _check_repo(raw.get("repo"), "repo")
        approvers_raw = raw.get("approvers")
        if not isinstance(approvers_raw, (list, tuple)) or not approvers_raw:
            raise ValueError("approvers: non-empty list expected")
        approvers = tuple(dict.fromkeys(
            _require_text(login, "approver", maximum=39).lower()
            for login in approvers_raw))
        for login in approvers:
            if not _LOGIN_RE.fullmatch(login):
                raise ValueError("approvers: invalid GitHub login")
        artifact_raw = raw.get("artifact_repos") or ()
        if not isinstance(artifact_raw, (list, tuple)):
            raise ValueError("artifact_repos: list expected")
        artifact_repos = tuple(dict.fromkeys(
            _check_repo(item, "artifact_repos") for item in artifact_raw))
        ttl_s = _as_int(raw.get("ttl_s", DEFAULT_TTL_S), "ttl_s",
                        minimum=1, maximum=MAX_TTL_S)
        timeout_s = _as_float(raw.get("timeout_s", DEFAULT_TIMEOUT_S),
                              "timeout_s", minimum=1.0, maximum=60.0)
        total_timeout_s = _as_float(
            raw.get("total_timeout_s", DEFAULT_TOTAL_TIMEOUT_S),
            "total_timeout_s", minimum=timeout_s, maximum=120.0)
        max_comments = _as_int(raw.get("max_comments", MAX_COMMENTS),
                               "max_comments", minimum=1, maximum=10000)
        api_base = _check_api_base(raw.get("api_base", DEFAULT_API_BASE))
        reader_raw = section.get("reader", raw.get("reader"))
        reader = _require_text(reader_raw, "reader", maximum=200) \
            if reader_raw else ""
    except (TypeError, ValueError) as exc:
        raise ReaderError("reader-config", str(exc)) from exc
    return Source(repo=repo, issue=issue, approvers=approvers,
                  token_file=os.path.abspath(os.path.expanduser(token_file)),
                  projection=os.path.abspath(os.path.expanduser(projection)),
                  artifact_repos=artifact_repos, ttl_s=ttl_s,
                  timeout_s=timeout_s, total_timeout_s=total_timeout_s,
                  api_base=api_base, max_comments=max_comments, reader=reader)


def identity_from_config(section: Any) -> ApprovalIdentity:
    """Identite de source attendue, lue dans ``approvals.source``.

    Les hotes n'ont pas besoin de ``token_file`` (jamais lu par la lecture
    locale) ; ils declarent ``repo``, ``issue``, ``approvers`` et, si besoin,
    ``artifact_repos``. Sans ce bloc, la projection est refusee : un fichier
    valide ne prouve pas sa provenance.
    """
    if not isinstance(section, Mapping):
        raise ProjectionError("projection-source", "approvals: JSON object expected")
    raw = section.get("source")
    if raw is None:
        raw = section
    if not isinstance(raw, Mapping):
        raise ProjectionError("projection-source",
                              "approvals.source identity is required")
    try:
        repo = _check_repo(raw.get("repo"), "source repo")
        issue = _as_int(raw.get("issue"), "source issue", minimum=1,
                        maximum=10**9)
        approvers_raw = raw.get("approvers")
        if not isinstance(approvers_raw, (list, tuple)) or not approvers_raw:
            raise ValueError("approvers: non-empty list expected")
        approvers = tuple(dict.fromkeys(
            _require_text(login, "approver", maximum=39).lower()
            for login in approvers_raw))
        for login in approvers:
            if not _LOGIN_RE.fullmatch(login):
                raise ValueError("approvers: invalid GitHub login")
        artifact_raw = raw.get("artifact_repos") or ()
        if not isinstance(artifact_raw, (list, tuple)):
            raise ValueError("artifact_repos: list expected")
        artifact_repos = tuple(dict.fromkeys(
            _check_repo(item, "artifact_repos") for item in artifact_raw))
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-source", str(exc)) from exc
    return ApprovalIdentity(repo=repo, issue=issue, approvers=approvers,
                            artifact_repos=artifact_repos)


def _check_identity(projection: Projection, identity: ApprovalIdentity) -> None:
    source = projection.source
    if str(source.get("repo") or "") != identity.repo:
        raise ProjectionError("projection-source",
                              "projection repository does not match the identity")
    try:
        issue = int(source.get("issue") or 0)
    except (TypeError, ValueError):
        issue = 0
    if issue != identity.issue:
        raise ProjectionError("projection-source",
                              "projection issue does not match the identity")
    if tuple(projection.approvers) != identity.approvers:
        raise ProjectionError("projection-source",
                              "projection approvers do not match the identity")
    allowed = set(identity.repos)
    for approval in projection.approvals:
        if approval.artifact.repo not in allowed:
            raise ProjectionError("projection-source",
                                  "approval artifact is outside the identity")
    for revocation in projection.revocations:
        for artifact in revocation.scope:
            if artifact.repo not in allowed:
                raise ProjectionError("projection-source",
                                      "revocation scope is outside the identity")


def projection_path(config: Any = None) -> str:
    """Chemin absolu de la projection locale, ou ``projection-missing``."""
    section = _section(config)
    raw = section.get("projection")
    if not raw:
        raise ProjectionError("projection-missing",
                              "approvals.projection is not configured")
    try:
        path = os.path.abspath(os.path.expanduser(_require_text(
            raw, "projection", maximum=4096)))
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-source", str(exc)) from exc
    if not os.path.isabs(path):
        raise ProjectionError("projection-source",
                              "approvals.projection must be absolute")
    return path


# ---------------------------------------------------------------------------
# lecture GitHub (bureau-goal)
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Un 3xx est refuse : le jeton ne suit jamais une redirection."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code,
                                     "redirect refused", headers, fp)


class GitHubTransport:
    """Le client HTTP minimal du lecteur : GET JSON, jeton en memoire seule."""

    def __init__(self, source: Source) -> None:
        self.source = source
        try:
            self._token = httpkit.read_token_file(
                source.token_file, what="IMA GitHub token",
                hint="fichier 0600 nomme par approvals.source.token_file",
                error=ValueError)
        except (OSError, ValueError) as exc:
            # Le message nomme le fichier, jamais la valeur du jeton.
            raise ReaderError("reader-token", str(exc)) from exc
        self._opener = urllib.request.build_opener(_NoRedirect())

    def get(self, path: str) -> tuple[int, Mapping[str, str], bytes]:
        if not path.startswith("/") or "://" in path:
            raise ReaderError("reader-config", "invalid API path")
        url = self.source.api_base + path
        request = urllib.request.Request(url, headers={
            "Authorization": "Bearer %s" % self._token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ameesh-approvals-reader",
        })
        try:
            with self._opener.open(request, timeout=self.source.timeout_s) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ReaderError("reader-bounds", "GitHub response too large")
                return (int(response.status), dict(response.headers.items()), raw)
        except urllib.error.HTTPError as exc:
            raise ReaderError("reader-http", "GitHub API returned HTTP %d" % exc.code,
                              detail={"status": int(exc.code)}) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise ReaderError("reader-timeout", "GitHub API timed out") from exc
            raise ReaderError("reader-unavailable", "GitHub API unavailable") from exc


def _get_json(transport: Any, path: str, *, what: str) -> Any:
    try:
        status, _headers, body = transport.get(path)
    except ReaderError:
        raise
    except (TimeoutError, OSError) as exc:  # transport injecte, sans detail
        if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
            raise ReaderError("reader-timeout", "%s: timeout" % what) from exc
        raise ReaderError("reader-unavailable",
                          "%s: transport failure (%s)"
                          % (what, type(exc).__name__)) from exc
    except Exception as exc:  # transport injecte, jamais de detail sensible
        raise ReaderError("reader-unavailable",
                          "%s: transport failure (%s)"
                          % (what, type(exc).__name__)) from exc
    if status != 200:
        raise ReaderError("reader-http", "%s: HTTP %d" % (what, status),
                          detail={"status": int(status)})
    try:
        return json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ReaderError("reader-invalid", "%s: invalid JSON" % what) from exc


def read_source(source: Source, *, transport: Any = None,
                now: dt.datetime | None = None) -> tuple[dict, list[dict]]:
    """Lit l'issue IMA et ses commentaires, avec bornes et delai total."""
    if transport is None:
        transport = GitHubTransport(source)
    deadline = time.monotonic() + source.total_timeout_s

    def check_deadline() -> None:
        if time.monotonic() > deadline:
            raise ReaderError("reader-timeout", "GitHub read budget exhausted")

    check_deadline()
    issue = _get_json(transport,
                      "/repos/%s/issues/%d" % (source.repo, source.issue),
                      what="issue")
    if not isinstance(issue, Mapping):
        raise ReaderError("reader-invalid", "issue: JSON object expected")
    try:
        number = _as_int(issue.get("number"), "issue number", minimum=1)
        state = _require_text(issue.get("state"), "issue state", maximum=20).lower()
        _parse_ts(issue.get("updated_at"), "issue updated_at")
    except (TypeError, ValueError) as exc:
        raise ReaderError("reader-invalid", str(exc)) from exc
    if number != source.issue:
        raise ReaderError("reader-invalid", "issue number does not match the source")
    if state not in ("open", "closed"):
        raise ReaderError("reader-invalid", "issue state: open or closed expected")

    comments: list[dict] = []
    page = 1
    while True:
        check_deadline()
        if page > MAX_PAGES:
            raise ReaderError("reader-bounds", "too many GitHub comment pages")
        path = ("/repos/%s/issues/%d/comments?per_page=100&page=%d"
                % (source.repo, source.issue, page))
        chunk = _get_json(transport, path, what="comments")
        if not isinstance(chunk, list):
            raise ReaderError("reader-invalid", "comments: JSON array expected")
        for item in chunk:
            if not isinstance(item, Mapping):
                raise ReaderError("reader-invalid", "comment: JSON object expected")
            comments.append(dict(item))
            if len(comments) > source.max_comments:
                raise ReaderError("reader-bounds", "too many GitHub comments")
        if len(chunk) < 100:
            break
        page += 1
    return dict(issue), comments


def _commit_resolver(transport: Any, deadline: float) -> Callable[[str, str], str]:
    cache: dict[tuple[str, str], str] = {}

    def resolve(repo: str, ref: str) -> str:
        key = (repo, ref)
        if key in cache:
            return cache[key]
        if time.monotonic() > deadline:
            raise ReaderError("reader-timeout", "GitHub read budget exhausted")
        try:
            commit = _get_json(
                transport,
                "/repos/%s/commits/%s" % (repo, urllib.parse.quote(ref, safe="")),
                what="commit")
        except ReaderError as exc:
            # Un commit inconnu ne bloque que cet enregistrement : la
            # projection ne doit pas devenir indisponible pour un seul ref.
            status = (exc.detail or {}).get("status") \
                if isinstance(exc.detail, Mapping) else None
            if status in (404, 422):
                raise ApprovalRefusal(
                    "revision-unresolved",
                    "the forge does not know this revision") from exc
            raise
        if not isinstance(commit, Mapping):
            raise ReaderError("reader-invalid", "commit: JSON object expected")
        sha = str(commit.get("sha") or "").lower()
        if not _FULL_REVISION_RE.fullmatch(sha):
            raise ReaderError("reader-invalid", "commit: full SHA expected")
        cache[key] = sha
        return sha

    return resolve


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

def _first_line(body: str) -> str:
    for line in body.splitlines():
        text = line.strip()
        if text:
            return text[:300]
    return ""


def _comment_login(comment: Mapping) -> tuple[str, str]:
    user = comment.get("user")
    if not isinstance(user, Mapping):
        return "", ""
    login = user.get("login")
    kind = user.get("type")
    return (login if isinstance(login, str) else "",
            kind if isinstance(kind, str) else "")


def _comment_time(comment: Mapping, key: str) -> dt.datetime:
    try:
        return _parse_ts(comment.get(key), "comment %s" % key)
    except ValueError as exc:
        raise ApprovalRefusal("approval-invalid", str(exc)) from exc


def _artifact(repo: str, revision: str, path: str, source: Source,
              resolve: Callable[[str, str], str] | None,
              body_sha: str) -> Artifact:
    repo = repo.lower()
    if repo not in source.repos:
        raise ApprovalRefusal("out-of-scope",
                              "artifact repository is outside the approved source")
    try:
        path = _check_path(urllib.parse.unquote(path).rstrip(".,;:"))
    except (TypeError, ValueError) as exc:
        raise ApprovalRefusal("path-invalid", str(exc)) from exc
    text = revision.lower()
    if not _REVISION_RE.fullmatch(text):
        raise ApprovalRefusal("mutable-revision",
                              "an approval must name an exact hexadecimal revision")
    if len(text) < 40:
        # Le corps cite souvent le SHA complet a cote de l'URL courte.
        matches = [sha.lower() for sha in _FULL_SHA_RE.findall(body_sha)
                   if sha.lower().startswith(text)]
        matches = list(dict.fromkeys(matches))
        if len(matches) == 1:
            text = matches[0]
        elif len(matches) > 1:
            raise ApprovalRefusal("revision-unresolved",
                                  "several full revisions match the short one")
        elif resolve is not None:
            requested = text
            text = resolve(repo, text).lower()
            # Une branche hex nommee comme un SHA ne doit pas fournir un
            # commit arbitraire : le SHA complet doit commencer par la
            # reference demandee.
            if not text.startswith(requested):
                raise ApprovalRefusal(
                    "revision-unresolved",
                    "the resolved commit does not match the requested revision")
        if not _FULL_REVISION_RE.fullmatch(text):
            raise ApprovalRefusal("revision-unresolved",
                                  "the exact revision could not be resolved")
    if resolve is not None:
        # Meme un SHA complet (ou trouve dans le corps) est relu a la forge :
        # une branche nommee comme un SHA, ou un SHA etranger au contrat, ne
        # doit pas se faire passer pour la revision exacte.
        observed = resolve(repo, text).lower()
        if observed != text:
            raise ApprovalRefusal("revision-unresolved",
                                  "the revision does not name the observed commit")
    return Artifact(repo=repo, revision=text, path=path)


def _legacy_artifacts(
        body: str, source: Source,
        resolve: Callable[[str, str], str] | None,
) -> tuple[list[Artifact], list[dict]]:
    found: list[Artifact] = []
    refusals: list[dict] = []
    seen: set[tuple] = set()
    for match in _BLOB_URL_RE.finditer(body):
        repo, revision, path = match.groups()
        try:
            artifact = _artifact(repo, revision, path, source, resolve, body)
        except ApprovalRefusal as exc:
            refusals.append({"code": exc.code})
            continue
        if artifact.key() not in seen:
            seen.add(artifact.key())
            found.append(artifact)
    for match in _PIN_RE.finditer(body):
        repo, revision, path = match.groups()
        try:
            artifact = _artifact(repo, revision, path, source, resolve, body)
        except ApprovalRefusal as exc:
            refusals.append({"code": exc.code})
            continue
        if artifact.key() not in seen:
            seen.add(artifact.key())
            found.append(artifact)
    for match in _PLAIN_ARTIFACT_RE.finditer(body):
        revision, path = match.groups()
        try:
            artifact = _artifact(source.repo, revision, path, source,
                                 resolve, body)
        except ApprovalRefusal as exc:
            refusals.append({"code": exc.code})
            continue
        if artifact.key() not in seen:
            seen.add(artifact.key())
            found.append(artifact)
    if len(found) > MAX_ARTIFACTS_PER_COMMENT:
        raise ApprovalRefusal("approval-invalid", "too many artifacts in one comment")
    return found, refusals


def _envelope(body: str) -> dict | None:
    match = _ENVELOPE_RE.search(body)
    if not match:
        return None
    raw = match.group(1)
    if len(raw) > 16384:
        raise ApprovalRefusal("approval-envelope-invalid", "envelope too large")
    try:
        value = jcs.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ApprovalRefusal("approval-envelope-invalid", "invalid JSON envelope") from exc
    if not isinstance(value, Mapping):
        raise ApprovalRefusal("approval-envelope-invalid", "object expected")
    schema = value.get("schema")
    if schema not in (None, ENVELOPE_SCHEMA):
        raise ApprovalRefusal("approval-envelope-invalid", "unknown envelope schema")
    return dict(value)


def _structured_artifacts(
        envelope: Mapping, source: Source,
        resolve: Callable[[str, str], str] | None,
) -> tuple[list[Artifact], list[dict]]:
    raw = envelope.get("artifacts", envelope.get("scope"))
    if not isinstance(raw, list) or not raw:
        raise ApprovalRefusal("approval-no-artifact",
                              "the envelope names no artifact")
    found: list[Artifact] = []
    refusals: list[dict] = []
    for item in raw:
        if not isinstance(item, Mapping):
            refusals.append({"code": "approval-envelope-invalid"})
            continue
        try:
            repo = _require_text(item.get("repo"), "artifact repo", maximum=201)
            revision = _require_text(item.get("revision"), "artifact revision", maximum=64)
            path = _require_text(item.get("path"), "artifact path", maximum=512)
            found.append(_artifact(repo, revision, path, source, resolve,
                                   json.dumps(item, sort_keys=True)))
        except (TypeError, ValueError) as exc:
            refusals.append({"code": "path-invalid" if "path" in str(exc)
                             else "approval-envelope-invalid"})
        except ApprovalRefusal as exc:
            refusals.append({"code": exc.code})
    if not found and not refusals:
        raise ApprovalRefusal("approval-no-artifact",
                              "the envelope names no artifact")
    return found, refusals


def _scope_artifacts(body: str, envelope: Mapping | None, source: Source,
                     *, strict: bool = False) -> tuple[list[Artifact], list[dict]]:
    """References d'une decision, bornees par depot + chemin.

    La revision est ignoree : une URL de branche ou un SHA court non resolu
    nomme quand meme une portee (fail-closed). Le champ `revision` du scope
    est un marqueur (0*40) ; `_apply_revocations` ne compare que depot et
    chemin. En mode ``strict`` (approbation editee), une reference reconnue
    mais non bornable (depot hors perimetre, chemin invalide, entree
    structuree illisible) est remontee : l'appelant doit refuser la
    projection, pas se contenter des references valides.
    """
    items: list[tuple[Any, Any]] = []
    unbounded: list[dict] = []
    if envelope is not None:
        raw = envelope.get("artifacts", envelope.get("scope"))
        if not isinstance(raw, list):
            unbounded.append({"code": "approval-envelope-invalid"})
            raw = []
        for item in raw:
            if isinstance(item, Mapping):
                items.append((item.get("repo"), item.get("path")))
            else:
                unbounded.append({"code": "approval-envelope-invalid"})
    else:
        items += [(match.group(1), match.group(3))
                  for match in _BLOB_URL_RE.finditer(body)]
        items += [(match.group(1), match.group(3))
                  for match in _PIN_RE.finditer(body)]
        items += [(source.repo, match.group(2))
                  for match in _PLAIN_ARTIFACT_RE.finditer(body)]
    found: list[Artifact] = []
    seen: set[tuple[str, str]] = set()
    for repo, path in items:
        try:
            repo = _check_repo(repo, "scope repo")
            path = _check_path(urllib.parse.unquote(str(path)).rstrip(".,;:`"))
        except (TypeError, ValueError):
            unbounded.append({"code": "path-invalid"})
            continue
        if repo not in source.repos:
            unbounded.append({"code": "out-of-scope"})
            continue
        if (repo, path) in seen:
            continue
        seen.add((repo, path))
        found.append(Artifact(repo=repo, revision="0" * 40, path=path))
        if len(found) > MAX_ARTIFACTS_PER_COMMENT:
            unbounded.append({"code": "approval-invalid"})
            break
    if not strict:
        return found, []
    return found, unbounded


def _revocation_scope(body: str, envelope: Mapping | None,
                      source: Source) -> list[Artifact]:
    """Portee d'une revocation, toleree aux references non bornables."""
    return _scope_artifacts(body, envelope, source)[0]


def _revocation_targets(body: str) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(match.group(1))
                               for match in _COMMENT_REF_RE.finditer(body)))


def _normalize_comment(comment: Mapping, source: Source,
                       resolve: Callable[[str, str], str] | None,
                       ) -> tuple[list[Approval], list[Revocation], list[dict]]:
    body = comment.get("body")
    if not isinstance(body, str) or not body.strip():
        return [], [], []
    first = _first_line(body)
    try:
        envelope = _envelope(body)
    except ApprovalRefusal as exc:
        # Un bloc structure malforme ne retombe jamais sur la prose.
        return [], [], [{"comment": _comment_id_or_zero(comment), "code": exc.code}]
    decision = envelope.get("decision") if envelope is not None else None
    approval = envelope is not None and decision == "approved"
    revocation = envelope is not None and decision in (
        "revoked", "withdrawn", "superseded")
    if envelope is not None and not approval and not revocation:
        return [], [], [{"comment": _comment_id_or_zero(comment),
                         "code": "approval-envelope-invalid"}]
    if not approval and not revocation:
        approval = bool(_APPROVAL_RE.match(first))
        revocation = bool(_REVOKE_RE.match(first))
    if not approval and not revocation:
        return [], [], []

    login, user_type = _comment_login(comment)
    comment_id = _comment_id_or_zero(comment)
    if not comment_id:
        return [], [], [{"comment": 0, "code": "approval-invalid"}]
    if user_type.lower() == "bot":
        return [], [], [{"comment": comment_id, "code": "author-bot"}]
    if login.lower() not in source.approvers:
        return [], [], [{"comment": comment_id, "code": "author-not-approver"}]
    try:
        created = _comment_time(comment, "created_at")
        updated = _comment_time(comment, "updated_at")
    except ApprovalRefusal as exc:
        return [], [], [{"comment": comment_id, "code": exc.code}]
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    url = str(comment.get("html_url") or "").strip()
    if not url:
        url = "https://github.com/%s/issues/%d#issuecomment-%d" % (
            source.repo, source.issue, comment_id)
    statement = first
    refusals: list[dict] = []
    if updated != created and not revocation:
        # Une approbation modifiee n'est pas une decision. Mais si elle
        # remplace un accord anterieur, l'ignorer reactiverait cet accord :
        # on borne TOUTES les references qu'elle nomme, revision comprise,
        # et on refuse l'approbation. Une reference reconnue mais non
        # bornable interdit la projection : une liste partielle ne prouve
        # pas que l'ancien accord est entierement couvert.
        artifacts, unbounded = _scope_artifacts(
            body, envelope, source, strict=True)
        if unbounded or not artifacts:
            raise ProjectionError(
                "projection-invalid",
                "an edited approval cannot be scoped safely")
        fence = Revocation(
            id="edited:%d" % comment_id, comment_id=comment_id,
            author=login.lower(), created_at=created, updated_at=updated,
            url=url, statement=statement, body_sha256=body_sha,
            targets=(), scope=tuple(artifacts))
        refusals.append({"code": "approval-edited"})
        return [], [fence], _bounded_refusals(comment_id, refusals)

    if revocation:
        targets = _revocation_targets(body)
        if envelope is not None:
            raw_targets = envelope.get("targets") or []
            if not isinstance(raw_targets, list):
                return [], [], [{"comment": comment_id,
                                 "code": "approval-envelope-invalid"}]
            try:
                targets = tuple(dict.fromkeys(
                    targets + tuple(_as_int(item, "revocation target", minimum=1)
                                    for item in raw_targets)))
            except (TypeError, ValueError):
                return [], [], [{"comment": comment_id,
                                 "code": "approval-envelope-invalid"}]
        # Cibles et portee s'additionnent : un lien de contexte vers un autre
        # commentaire n'efface pas la portee nommee.
        scope = _revocation_scope(body, envelope, source)
        if not targets and not scope:
            refusals.append({"code": "revocation-unscoped"})
            return [], [], _bounded_refusals(comment_id, refusals)
        record = Revocation(
            id="revocation:%d" % comment_id, comment_id=comment_id,
            author=login.lower(), created_at=created, updated_at=updated,
            url=url, statement=statement, body_sha256=body_sha,
            targets=targets, scope=tuple(scope))
        return [], [record], _bounded_refusals(comment_id, refusals)

    artifacts: list[Artifact]
    try:
        if envelope is not None:
            artifacts, extra = _structured_artifacts(envelope, source, resolve)
            refusals.extend(extra)
        else:
            artifacts, extra = _legacy_artifacts(body, source, resolve)
            refusals.extend(extra)
    except ApprovalRefusal as exc:
        refusals.append({"code": exc.code})
        return [], [], _bounded_refusals(comment_id, refusals)
    if not artifacts:
        refusals.append({"code": "approval-no-artifact"})
        return [], [], _bounded_refusals(comment_id, refusals)

    expires_at: dt.datetime | None = None
    if envelope is not None and envelope.get("expires_at") is not None:
        try:
            expires_at = _parse_ts(envelope.get("expires_at"), "approval expires_at")
        except ValueError:
            return [], [], [{"comment": comment_id,
                             "code": "approval-envelope-invalid"}]
    records = [
        Approval(
            id="%d:%s@%s:%s" % (comment_id, artifact.repo, artifact.revision,
                                artifact.path),
            comment_id=comment_id, author=login.lower(), created_at=created,
            updated_at=updated, url=url, statement=statement,
            body_sha256=body_sha, artifact=artifact, expires_at=expires_at)
        for artifact in artifacts
    ]
    return records, [], _bounded_refusals(comment_id, refusals)


def _comment_id_or_zero(comment: Mapping) -> int:
    try:
        return _as_int(comment.get("id"), "comment id", minimum=1)
    except (TypeError, ValueError):
        return 0


def _bounded_refusals(comment_id: int, refusals: Iterable[Mapping]) -> list[dict]:
    out = []
    for item in refusals:
        code = str(item.get("code") or "")
        if not _CODE_RE.fullmatch(code):
            code = "approval-invalid"
        out.append({"comment": comment_id, "code": code})
        if len(out) >= 16:
            break
    return out


def _apply_supersession(approvals: list[Approval]) -> list[Approval]:
    by_scope: dict[tuple[str, str], list[Approval]] = {}
    for approval in approvals:
        by_scope.setdefault((approval.artifact.repo, approval.artifact.path),
                            []).append(approval)
    updated: list[Approval] = []
    for records in by_scope.values():
        records.sort(key=lambda item: (item.created_at, item.comment_id, item.id))
        latest = records[-1]
        for record in records:
            if record.id == latest.id:
                updated.append(record)
            else:
                updated.append(replace(record, state="superseded",
                                       superseded_by=latest.id))
    return updated


def _apply_revocations(approvals: list[Approval],
                       revocations: Iterable[Revocation]) -> list[Approval]:
    out = list(approvals)
    ordered = sorted(revocations,
                     key=lambda item: (item.created_at, item.comment_id))
    for index, approval in enumerate(out):
        for revocation in ordered:
            if (revocation.created_at, revocation.comment_id) <= (
                    approval.created_at, approval.comment_id):
                continue
            target = approval.comment_id in revocation.targets
            in_scope = any(
                artifact.repo == approval.artifact.repo
                and artifact.path == approval.artifact.path
                for artifact in revocation.scope)
            if not target and not in_scope:
                continue
            out[index] = replace(approval, state="revoked",
                                 revoked_by=revocation.id)
            break
    return out


def _source_digest(issue: Mapping, comments: Iterable[Mapping]) -> str:
    records = []
    for comment in comments:
        body = comment.get("body")
        records.append({
            "id": _comment_id_or_zero(comment),
            "updated_at": str(comment.get("updated_at") or ""),
            "body_sha256": hashlib.sha256(
                (body if isinstance(body, str) else "").encode("utf-8")).hexdigest(),
        })
    records.sort(key=lambda item: item["id"])
    payload = {
        "issue": {
            "number": issue.get("number"),
            "state": issue.get("state"),
            "updated_at": issue.get("updated_at"),
        },
        "comments": records,
    }
    return hashlib.sha256(jcs.canonicalize(payload)).hexdigest()


def build_projection(source: Source, issue: Mapping, comments: Iterable[Mapping], *,
                     now: dt.datetime | None = None,
                     fetched_at: dt.datetime | None = None,
                     resolve: Callable[[str, str], str] | None = None,
                     reader: str = "") -> Projection:
    """Normalise l'issue et ses commentaires ; ne fait aucun reseau si
    ``resolve`` est fourni ou si les SHA complets sont dans les corps."""
    generated = _utc(now)
    fetched = _utc(fetched_at or generated)
    approvers = tuple(source.approvers)
    approvals: list[Approval] = []
    revocations: list[Revocation] = []
    refusals: list[dict] = []
    comments = list(comments)
    for comment in comments:
        try:
            records, revoked, refused = _normalize_comment(
                comment, source, resolve)
        except ReaderError:
            raise
        except ApprovalRefusal as exc:
            refusals.append({"comment": _comment_id_or_zero(comment),
                             "code": exc.code})
            continue
        approvals.extend(records)
        revocations.extend(revoked)
        refusals.extend(refused)
        if len(refusals) > MAX_REFUSALS:
            raise ApprovalRefusal("approval-bounds", "too many refusals")
    # Un commentaire ne peut pas produire deux fois le meme identifiant.
    unique: dict[str, Approval] = {}
    for approval in approvals:
        if approval.id in unique:
            raise ApprovalRefusal("approval-ambiguous",
                                  "duplicate approval record")
        unique[approval.id] = approval
    approvals = _apply_supersession(list(unique.values()))
    approvals = _apply_revocations(approvals, revocations)
    # Une revocation qui vise un commentaire inexistant ou non anterieur est
    # un temoin, pas un accord silencieux.
    known = {approval.comment_id for approval in approvals}
    for revocation in revocations:
        for target in revocation.targets:
            if target not in known:
                refusals.append({"comment": revocation.comment_id,
                                 "code": "revocation-time"})
        if revocation.targets and not any(
                target in known for target in revocation.targets):
            refusals.append({"comment": revocation.comment_id,
                             "code": "revocation-time"})
    try:
        issue_state = _require_text(issue.get("state"), "issue state",
                                    maximum=20).lower()
        issue_updated = _iso(_parse_ts(issue.get("updated_at"),
                                       "issue updated_at"))
    except (TypeError, ValueError) as exc:
        raise ApprovalRefusal("approval-invalid", str(exc)) from exc
    if issue_state not in ("open", "closed"):
        raise ApprovalRefusal("approval-invalid",
                              "issue state: open or closed expected")
    source_meta = {
        "repo": source.repo,
        "issue": source.issue,
        "issue_state": issue_state,
        "issue_updated_at": issue_updated,
        "comments": len(comments),
        "fetched_at": _iso(fetched),
        "source_digest": _source_digest(issue, comments),
    }
    expires = generated + dt.timedelta(seconds=source.ttl_s)
    projection = Projection(
        source=source_meta, generated_at=generated, expires_at=expires,
        approvers=approvers, approvals=tuple(approvals),
        revocations=tuple(revocations), refusals=tuple(refusals),
        reader={"name": reader or source.reader,
                "ameesh": _version()})
    # Le digest couvre tout sauf lui-meme.
    digest = hashlib.sha256(jcs.canonicalize(projection._payload())).hexdigest()
    return replace(projection, digest=digest)


def _version() -> str:
    try:
        from . import version as version_fn
        return str(version_fn())
    except Exception:  # pragma: no cover - version is optional
        return ""


def refresh(source: Source, *, transport: Any = None,
            now: dt.datetime | None = None) -> Projection:
    """Lit GitHub et rend la projection ; n'ecrit rien et ne journalise jamais
    le jeton."""
    if transport is None:
        transport = GitHubTransport(source)
    deadline = time.monotonic() + source.total_timeout_s
    issue, comments = read_source(source, transport=transport, now=now)
    resolver = _commit_resolver(transport, deadline)
    return build_projection(source, issue, comments, now=now,
                            resolve=resolver, reader=source.reader)


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
        parent_st = os.stat(parent)
    except OSError as exc:
        raise ProjectionError("projection-unreadable",
                              "projection directory is unreadable") from exc
    if not stat.S_ISDIR(parent_st.st_mode):
        raise ProjectionError("projection-unreadable",
                              "projection directory expected")
    if parent_st.st_mode & 0o022:
        raise ProjectionError("projection-permissions",
                              "projection directory is group/other writable")
    if owner_uid is not None and parent_st.st_uid != owner_uid:
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
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ProjectionError("projection-unreadable",
                                  "projection: regular file expected")
        if st.st_mode & 0o022:
            raise ProjectionError("projection-permissions",
                                  "projection is group/other writable")
        if owner_uid is not None and st.st_uid != owner_uid:
            raise ProjectionError("projection-owner",
                                  "projection owner mismatch")
        if st.st_size > MAX_PROJECTION_BYTES:
            raise ProjectionError("projection-bounds", "projection too large")
        raw = os.read(fd, MAX_PROJECTION_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_PROJECTION_BYTES:
        raise ProjectionError("projection-bounds", "projection too large")
    return raw


def _parse_artifact(value: Any) -> Artifact:
    if not isinstance(value, Mapping):
        raise ValueError("artifact: JSON object expected")
    repo = _check_repo(value.get("repo"), "artifact repo")
    revision = _require_text(value.get("revision"), "artifact revision",
                             maximum=64).lower()
    if not _REVISION_RE.fullmatch(revision):
        raise ValueError("artifact revision: hexadecimal only")
    path = _check_path(value.get("path"))
    return Artifact(repo=repo, revision=revision, path=path)


def _parse_approval(value: Any) -> Approval:
    if not isinstance(value, Mapping):
        raise ValueError("approval: JSON object expected")
    if value.get("kind") not in (None, "approval"):
        raise ValueError("approval kind: approval expected")
    state = str(value.get("state") or "")
    if state not in ("current", "superseded", "revoked"):
        raise ValueError("approval state: current, superseded or revoked expected")
    superseded_by = str(value.get("superseded_by") or "")
    revoked_by = str(value.get("revoked_by") or "")
    if state == "superseded" and not superseded_by:
        raise ValueError("superseded approval: superseded_by required")
    if state == "revoked" and not revoked_by:
        raise ValueError("revoked approval: revoked_by required")
    expires_at = value.get("expires_at")
    created = _parse_ts(value.get("created_at"), "approval created_at")
    updated = _parse_ts(value.get("updated_at"), "approval updated_at")
    if updated != created:
        raise ValueError("approval was edited after creation")
    body_sha256 = _require_text(value.get("body_sha256"), "body_sha256",
                                maximum=64)
    if not _DIGEST_RE.fullmatch(body_sha256):
        raise ValueError("body_sha256: SHA-256 expected")
    return Approval(
        id=_require_text(value.get("id"), "approval id", maximum=512),
        comment_id=_as_int(value.get("comment_id"), "comment_id", minimum=1),
        author=_require_text(value.get("author"), "author", maximum=39).lower(),
        created_at=created, updated_at=updated,
        url=_require_text(value.get("url"), "approval url", maximum=2048),
        statement=str(value.get("statement") or "")[:300],
        body_sha256=body_sha256,
        artifact=_parse_artifact(value.get("artifact")),
        state=state, superseded_by=superseded_by, revoked_by=revoked_by,
        expires_at=_parse_ts(expires_at, "approval expires_at")
        if expires_at else None)


def _parse_revocation(value: Any) -> Revocation:
    if not isinstance(value, Mapping):
        raise ValueError("revocation: JSON object expected")
    targets_raw = value.get("targets") or []
    if not isinstance(targets_raw, list):
        raise ValueError("revocation targets: list expected")
    scope_raw = value.get("scope") or []
    if not isinstance(scope_raw, list):
        raise ValueError("revocation scope: list expected")
    targets = tuple(dict.fromkeys(
        _as_int(item, "revocation target", minimum=1) for item in targets_raw))
    scope = tuple(_parse_artifact(item) for item in scope_raw)
    if not targets and not scope:
        raise ValueError("revocation: a target or a scope is required")
    created = _parse_ts(value.get("created_at"), "revocation created_at")
    updated = _parse_ts(value.get("updated_at"), "revocation updated_at")
    if updated < created:
        raise ValueError("revocation updated before its creation")
    body_sha256 = _require_text(value.get("body_sha256"), "body_sha256",
                                maximum=64)
    if not _DIGEST_RE.fullmatch(body_sha256):
        raise ValueError("body_sha256: SHA-256 expected")
    return Revocation(
        id=_require_text(value.get("id"), "revocation id", maximum=512),
        comment_id=_as_int(value.get("comment_id"), "comment_id", minimum=1),
        author=_require_text(value.get("author"), "author", maximum=39).lower(),
        created_at=created, updated_at=updated,
        url=_require_text(value.get("url"), "revocation url", maximum=2048),
        statement=str(value.get("statement") or "")[:300],
        body_sha256=body_sha256,
        targets=targets, scope=scope)


def _parse_projection(payload: Any, *, now: dt.datetime,
                      max_age_s: int, clock_skew_s: float,
                      max_ttl_s: int) -> Projection:
    if not isinstance(payload, Mapping):
        raise ProjectionError("projection-invalid", "JSON object expected")
    if payload.get("schema") != SCHEMA:
        raise ProjectionError("projection-invalid", "unknown projection schema")
    if payload.get("authority") != AUTHORITY:
        raise ProjectionError("projection-invalid", "authority must be IMA")
    source = payload.get("source")
    if not isinstance(source, Mapping):
        raise ProjectionError("projection-invalid", "source: JSON object expected")
    try:
        repo = _check_repo(source.get("repo"), "source repo")
        issue = _as_int(source.get("issue"), "source issue", minimum=1)
        issue_state = _require_text(source.get("issue_state"),
                                    "issue state", maximum=20).lower()
        if issue_state not in ("open", "closed"):
            raise ValueError("issue state: open or closed expected")
        _parse_ts(source.get("issue_updated_at"), "issue updated_at")
        _parse_ts(source.get("fetched_at"), "fetched_at")
        comments = _as_int(source.get("comments"), "comments", minimum=0)
        source_digest = _require_text(source.get("source_digest"),
                                      "source_digest", maximum=64)
        if not _DIGEST_RE.fullmatch(source_digest):
            raise ValueError("source_digest: SHA-256 expected")
        generated = _parse_ts(payload.get("generated_at"), "generated_at")
        expires = _parse_ts(payload.get("expires_at"), "expires_at")
        ttl = (expires - generated).total_seconds()
        if ttl <= 0 or ttl > max_ttl_s:
            raise ValueError("projection ttl: out of bounds")
        approvers_raw = payload.get("approvers")
        if not isinstance(approvers_raw, list) or not approvers_raw:
            raise ValueError("approvers: non-empty list expected")
        approvers = tuple(dict.fromkeys(
            _require_text(item, "approver", maximum=39).lower()
            for item in approvers_raw))
        for login in approvers:
            if not _LOGIN_RE.fullmatch(login):
                raise ValueError("approvers: invalid GitHub login")
        approvals_raw = payload.get("approvals")
        if not isinstance(approvals_raw, list):
            raise ValueError("approvals: list expected")
        approvals = tuple(_parse_approval(item) for item in approvals_raw)
        revocations_raw = payload.get("revocations") or []
        if not isinstance(revocations_raw, list):
            raise ValueError("revocations: list expected")
        revocations = tuple(_parse_revocation(item) for item in revocations_raw)
        refusals_raw = payload.get("refusals") or []
        if not isinstance(refusals_raw, list):
            raise ValueError("refusals: list expected")
        if len(refusals_raw) > MAX_REFUSALS:
            raise ValueError("too many refusals")
        refusals = []
        for item in refusals_raw[:MAX_REFUSALS]:
            if not isinstance(item, Mapping):
                raise ValueError("refusal: JSON object expected")
            code = str(item.get("code") or "")
            if not _CODE_RE.fullmatch(code):
                raise ValueError("refusal code: invalid")
            refusals.append({"comment": _as_int(item.get("comment"),
                                                "refusal comment", minimum=0),
                             "code": code})
        reader = payload.get("reader") or {}
        if not isinstance(reader, Mapping):
            raise ValueError("reader: JSON object expected")
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-invalid", str(exc)) from exc
    if now < generated - dt.timedelta(seconds=clock_skew_s):
        raise ProjectionError("projection-future", "projection is in the future")
    if now > expires:
        raise ProjectionError("projection-stale", "projection has expired")
    if (now - generated).total_seconds() > max_age_s:
        raise ProjectionError("projection-stale", "projection is older than allowed")
    # Un identifiant ne peut pas designer deux approbations ; l'auteur et
    # les renvois doivent appartenir a la projection.
    seen: set[str] = set()
    revocation_ids = {revocation.id for revocation in revocations}
    for approval in approvals:
        if approval.id in seen:
            raise ProjectionError("projection-invalid", "duplicate approval id")
        seen.add(approval.id)
        if approval.author not in approvers:
            raise ProjectionError("projection-invalid",
                                  "approval author is not an approver")
        if approval.superseded_by and approval.superseded_by not in {
                item.id for item in approvals}:
            raise ProjectionError("projection-invalid",
                                  "superseded_by: unknown approval")
        if approval.revoked_by and approval.revoked_by not in revocation_ids:
            raise ProjectionError("projection-invalid",
                                  "revoked_by: unknown revocation")
    for revocation in revocations:
        if revocation.author not in approvers:
            raise ProjectionError("projection-invalid",
                                  "revocation author is not an approver")
    return Projection(
        source={"repo": repo, "issue": issue,
                "issue_state": str(source.get("issue_state")),
                "issue_updated_at": _iso(_parse_ts(source["issue_updated_at"])),
                "comments": comments,
                "fetched_at": _iso(_parse_ts(source["fetched_at"])),
                "source_digest": source_digest},
        generated_at=generated, expires_at=expires, approvers=approvers,
        approvals=approvals, revocations=revocations, refusals=tuple(refusals),
        reader=dict(reader))


def load_projection(config: Any = None, *, path: str | None = None,
                    now: dt.datetime | None = None,
                    max_age_s: int | None = None,
                    owner_uid: int | None = None,
                    identity: ApprovalIdentity | None = None) -> Projection:
    """Lit et valide la projection locale ; toute absence est un refus.

    ``config`` peut etre un objet ``Config``, un mapping de configuration, ou
    ``None`` (configuration de l'hote). L'identite de source (depot, issue,
    approbateurs, perimetre d'artefacts) vient de la configuration locale et
    est **obligatoire** : un fichier de projection valide ne prouve pas sa
    provenance. Un appel direct par chemin doit fournir ``identity``.

    Le fichier doit etre un fichier regulier, non modifiable par le groupe ou
    les autres ; ``owner_uid``, s'il est fourni, fixe son proprietaire attendu.
    """
    # Un chemin explicite ne depend pas de la config hote pour ses reglages,
    # mais l'identite de source reste obligatoire.
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
            owner_uid = _as_int(section.get("owner_uid"), "owner_uid", minimum=0)
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-source", str(exc)) from exc
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
        expected = hashlib.sha256(jcs.canonicalize(body)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-invalid", "invalid JSON value") from exc
    if not isinstance(digest, str) or not hmac.compare_digest(digest, expected):
        raise ProjectionError("projection-digest", "projection digest mismatch")
    projection = _parse_projection(body, now=current, max_age_s=max_age_s,
                                   clock_skew_s=CLOCK_SKEW_S,
                                   max_ttl_s=MAX_TTL_S)
    if identity is None:
        raise ProjectionError("projection-source",
                              "expected source identity is required")
    _check_identity(projection, identity)
    return replace(projection, digest=digest, path=path)


def local_projection(config: Any = None, *,
                     now: dt.datetime | None = None) -> Projection:
    """Lecture de la projection de l'hote (aucun reseau)."""
    return load_projection(config, now=now)


# ---------------------------------------------------------------------------
# consommation (fail-closed)
# ---------------------------------------------------------------------------

def require_approval(projection: Projection, *,
                     repo: str, revision: str, path: str,
                     source: str = SOURCE_GITHUB,
                     now: dt.datetime | None = None) -> Approval:
    """Rend l'approbation courante exacte, ou refuse.

    ``source=api`` n'est jamais une autorite : le consommateur doit nommer la
    source ``github`` de la projection. Une approbation d'une autre revision
    est ``approval-stale`` (la tete a change), jamais acceptee par
    approximation.
    """
    if source == SOURCE_API:
        raise ApprovalRefusal("approval-api-not-authority",
                              "source=api is not an authority")
    if source != SOURCE_GITHUB:
        raise ApprovalRefusal("approval-source",
                              "only the github projection is an authority")
    try:
        wanted = Artifact(repo=_check_repo(repo, "repo"),
                          revision=_require_text(revision, "revision",
                                                 maximum=64).lower(),
                          path=_check_path(path))
    except (TypeError, ValueError) as exc:
        raise ApprovalRefusal("approval-invalid", str(exc)) from exc
    if not _REVISION_RE.fullmatch(wanted.revision):
        raise ApprovalRefusal("approval-invalid", "revision: hexadecimal only")
    current = _utc(now)
    if current > projection.expires_at:
        raise ProjectionError("projection-stale", "projection has expired")
    if current < projection.generated_at - dt.timedelta(seconds=CLOCK_SKEW_S):
        raise ProjectionError("projection-future", "projection is in the future")
    matching = [a for a in projection.approvals if a.artifact == wanted]
    current_records = [a for a in matching if a.state == "current"]
    if len(current_records) == 1:
        record = current_records[0]
        if record.expires_at and current > record.expires_at:
            raise ApprovalRefusal("approval-expired", "approval has expired")
        return record
    if len(current_records) > 1:
        raise ApprovalRefusal("approval-ambiguous", "several approvals cover it")
    revoked = [a for a in matching if a.state == "revoked"]
    if revoked:
        raise ApprovalRefusal("approval-revoked", "approval was revoked")
    superseded = [a for a in matching if a.state == "superseded"]
    if superseded:
        raise ApprovalRefusal("approval-superseded", "approval was superseded")
    same_path = [a for a in projection.approvals
                 if a.artifact.repo == wanted.repo
                 and a.artifact.path == wanted.path]
    if same_path:
        raise ApprovalRefusal("approval-stale",
                              "approval covers another revision of this artifact")
    raise ApprovalRefusal("approval-missing", "no approval for this artifact")


def projection_from_source(source: Any, *, now: dt.datetime | None = None) -> Projection:
    """Reconstruit une projection depuis la forme publiee par `read_sources`.

    Les hotes ne lisent pas le fichier au moment du verificateur : B a deja
    charge et valide la projection, puis l'a passee dans le snapshot sous
    ``{"projection": {...}, "records": [...]}``. Cette fonction permet a un
    verificateur de C d'appliquer les memes regles exactes sans relire de
    fichier ni refaire confiance a une entree cliente.
    """
    if not isinstance(source, Mapping):
        raise ProjectionError("projection-invalid", "approvals: JSON object expected")
    meta = source.get("projection")
    records = source.get("records")
    if not isinstance(meta, Mapping) or not isinstance(records, (list, tuple)):
        raise ProjectionError("projection-invalid",
                              "projection and records are required")
    try:
        generated = _parse_ts(meta.get("generated_at"), "generated_at")
        expires = _parse_ts(meta.get("expires_at"), "expires_at")
        approvals = tuple(_parse_approval(item) for item in records)
        approvers_raw = meta.get("approvers") or ()
        if not isinstance(approvers_raw, (list, tuple)):
            raise ValueError("approvers: list expected")
        approvers = tuple(str(item).lower() for item in approvers_raw)
        issue = _as_int(meta.get("issue") or 0, "issue", minimum=0)
    except (TypeError, ValueError) as exc:
        raise ProjectionError("projection-invalid", str(exc)) from exc
    current = _utc(now)
    if current > expires:
        raise ProjectionError("projection-stale", "projection has expired")
    if current < generated - dt.timedelta(seconds=CLOCK_SKEW_S):
        raise ProjectionError("projection-future", "projection is in the future")
    return Projection(
        source={"repo": str(meta.get("repo") or ""),
                "issue": issue,
                "source_digest": str(meta.get("source_digest") or "")},
        generated_at=generated, expires_at=expires, approvers=approvers,
        approvals=approvals, revocations=(), refusals=(),
        digest=str(meta.get("digest") or ""))


def approval_for_source(source: Any, *, repo: str, revision: str, path: str,
                        now: dt.datetime | None = None) -> Approval:
    """Consomme la forme ``read_sources`` avec les memes refus que
    ``require_approval`` ; aucun fichier ni reseau n'est lu ici."""
    projection = projection_from_source(source, now=now)
    return require_approval(projection, repo=repo, revision=revision,
                            path=path, now=now)


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


def _generated_at_of(path: str) -> dt.datetime | None:
    """Date de generation d'une projection existante, sans controle de
    fraicheur (l'ecrivain ne doit pas ecraser une projection plus recente)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_PROJECTION_BYTES:
            return None
        raw = os.read(fd, MAX_PROJECTION_BYTES + 1)
    finally:
        os.close(fd)
    try:
        payload = json.loads(raw)
        return _parse_ts(payload.get("generated_at"), "generated_at")
    except (TypeError, ValueError):
        return None


def write_projection(projection: Projection, path: str, *,
                     mode: int = 0o644) -> str:
    """Ecrit la projection atomiquement ; rend ``written`` ou ``not-newer``.

    Le fichier est lisible par les hotes (0644) mais jamais modifiable par le
    groupe ou les autres. Aucun secret n'y est ecrit : uniquement les
    approbations normalisees, leurs URL et des empreintes.
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
    with _locked(path + ".lock"):
        if os.path.exists(path):
            previous_ts = _generated_at_of(path)
            if previous_ts is not None and previous_ts >= projection.generated_at:
                return "not-newer"
        fd, tmp = tempfile.mkstemp(prefix=".tmp-approvals-", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(projection.as_dict(), handle, ensure_ascii=False,
                          sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
            _fsync_dir(parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    return "written"


__all__ = [
    "AUTHORITY", "Approval", "ApprovalError", "ApprovalRefusal", "Artifact",
    "ENVELOPE_SCHEMA", "GitHubTransport", "Projection", "ProjectionError",
    "ReaderError", "Revocation", "SCHEMA", "SOURCE_API", "SOURCE_GITHUB",
    "Source", "build_projection", "load_projection", "local_projection",
    "ApprovalIdentity", "approval_for_source", "identity_from_config",
    "projection_from_source", "projection_path", "read_source", "refresh",
    "require_approval", "source_from_config", "write_projection",
]
