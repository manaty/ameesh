# SPDX-License-Identifier: AGPL-3.0-only
"""Relais de modèle du serveur du mesh (lot L111, étude de l'exécuteur médié §5).

Un appareil prêté (Nexlink Compute) fait tourner un agent payé au token sans
jamais détenir la clé du fournisseur : son harnais appelle le **relais**, sur
le serveur du mesh, avec un **jeton de session** court ; le relais vérifie le
jeton, le modèle et le budget, ajoute la vraie clé et transmet le flux.

    POST /api/exec/v1/llm/deepseek/v1/messages   jeton de session — le tour
    GET  /api/exec/v1/llm/deepseek/v1/models     jeton de session — modèles admis

**Format.** `dsh` (0.2.0-rc.2) parle le protocole *Messages* (compatible
Anthropic : `POST {base}/v1/messages`, clé dans `x-api-key`, flux SSE
`message_start` … `message_delta` … `message_stop`), PAS le
`chat/completions` d'OpenAI qu'envisageait l'étude. Il lit l'URL de base dans
`DEEPSEEK_BASE_URL` et la clé dans `DEEPSEEK_API_KEY` (l'environnement de
lancement prime sur le magasin d'identifiants de `$DSH_HOME`) : vérifié par
essai réel, voir EXPLOITATION.md. Le relais vers l'API officielle vise
`https://api.deepseek.com/anthropic`.

**Contrat, dans l'ordre de traitement :**

1. *Jeton* : `x-api-key` (ou `Authorization: Bearer`) est un jeton de
   session ; `TokenVerifier.verify_token(jeton) -> Identity` (enrôlement et
   émission : L110 ; ici une interface et un bouchon `StaticTokens`). Le
   vérificateur refuse un jeton échu, révoqué ou dont le bail n'est plus
   courant. Échec → 401.
2. *Débit* : seau à jetons par agent (requêtes par minute) et nombre de flux
   simultanés par agent bornés → 429 avec `Retry-After`.
3. *Taille* : `Content-Length` obligatoire et borné (413 sans lecture), JSON
   objet seul (400).
4. *Modèle* : `model` doit être admis pour l'agent et l'hôte
   (`ModelPolicy.refusal`) : la politique `models` de la fiche Host (motifs)
   ∩ le modèle de la persona (défaut du descripteur s'il n'en déclare pas) →
   403. `max_tokens` est plafonné.
5. *Budget* (L70, `budget_limits`) : avant chaque requête, la dépense payée au
   token est relue (plafonds du mesh par heure et par jour, plafonds de
   l'agent, `budget_usd_per_day` de la persona) ; atteint → 402 ; dépense
   illisible → 503 (fail-closed).
6. *Clé* : lue dans les secrets du serveur (`ServerSecrets` : fichier privé
   0600 ou variable de l'environnement du service), **jamais dans le
   canon** ; elle ne figure dans aucune réponse ni aucun journal.
7. *Flux* : la réponse du fournisseur est recopiée telle quelle, ligne à
   ligne, au fil de l'eau ; seuls `content-type` et `request-id` sont
   repris de ses en-têtes.
8. *Mesure* : l'usage que renvoie le fournisseur (`input_tokens`,
   `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`,
   modèle réel) est relevé dans le flux et écrit dans `turn_costs` avec
   `source = 'relay'`, l'agent, le tour et le bail (`executor`,
   `lease_owner`, `lease_epoch`) : c'est la ligne qui fait foi au plafond.
   Un flux coupé avant le relevé final est compté par excès (fail-closed).
   Une écriture en échec garde la dépense en mémoire, comptée au plafond et
   réessayée à la requête suivante.

Le relais est un module **séparé** : `Relay.handle(handler)` traite une
requête sur n'importe quel `BaseHTTPRequestHandler`, pour être monté sous
`/api/exec/v1/llm/` par le serveur de l'API d'exécuteur (L108) ; en
attendant, `python -m ameesh.relay serve` le sert seul (voir `main`).

Côté appareil, `apply_turn_env` pose au lancement de chaque tour l'URL du
relais et un jeton court dans l'environnement du harnais — jamais sur disque.
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import http.client
import importlib
import json
import logging
import math
import os
import re
import stat
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Protocol

from . import cost as cost_mod
from . import storage

log = logging.getLogger("ameesh.relay")

#: préfixe des routes du relais dans l'API d'exécuteur
PREFIX = "/api/exec/v1/llm/"

#: fournisseurs relayés : nom de route → (harnais du grand livre, amont par défaut)
PROVIDERS = {
    "deepseek": {"harness": "deepseek",
                 "upstream": "https://api.deepseek.com/anthropic",
                 "upstream_env": "AMEESH_RELAY_DEEPSEEK_UPSTREAM",
                 "key_env": "DEEPSEEK_API_KEY",
                 "key_file_env": "AMEESH_RELAY_DEEPSEEK_KEY_FILE"},
}

#: côté appareil : variables du harnais qui portent l'URL du relais et le jeton
HARNESS_ENV = {
    "deepseek": {"provider": "deepseek", "base_url": "DEEPSEEK_BASE_URL",
                 "key": "DEEPSEEK_API_KEY"},
}

#: en-têtes du client recopiés vers le fournisseur (tout le reste est retiré :
#: identifiants de l'appareil, télémétrie, cookies…)
FORWARD_HEADERS = ("anthropic-version", "anthropic-beta")
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_MAX_BODY = 32 * 1024 * 1024
DEFAULT_MAX_TOKENS = 65536
DEFAULT_RATE_PER_MINUTE = 120
DEFAULT_CONCURRENCY = 2
DEFAULT_UPSTREAM_TIMEOUT = 300.0
#: plafond d'une réponse non-flux lue en entier (corps JSON, erreurs)
MAX_BUFFERED_RESPONSE = 8 * 1024 * 1024
#: plafond d'une réponse en flux (octets recopiés)
DEFAULT_MAX_STREAM = 256 * 1024 * 1024

_TURN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


# ==========================================================================
# identité : le jeton de session (L110)
# ==========================================================================

@dataclass(frozen=True)
class Identity:
    """Ce que prouve un jeton de session : quel exécuteur, quel hôte, quel
    agent, sous quel bail, pour quel tour (facultatif)."""

    executor: str
    host: str
    agent: str
    lease_owner: str
    lease_epoch: int
    turn: str | None = None
    expires_ts: float | None = None


class TokenError(Exception):
    """Jeton absent, inconnu, échu, révoqué ou bail périmé (→ 401)."""


class TokenVerifier(Protocol):
    def verify_token(self, token: str) -> Identity:
        """L'identité portée par ce jeton, ou `TokenError`.

        Contrat pour L110 : rejeter un jeton échu ou révoqué, et un jeton dont
        le bail (agent, owner, epoch) n'est plus le bail courant de l'agent."""


class StaticTokens:
    """Bouchon de `TokenVerifier` (tests, banc) : jeton → identité, en mémoire."""

    def __init__(self, tokens: dict | None = None, clock: Callable[[], float] = time.time):
        self.tokens = dict(tokens or {})
        self.clock = clock

    def verify_token(self, token: str) -> Identity:
        identity = self.tokens.get(token or "")
        if identity is None:
            raise TokenError("jeton inconnu")
        if identity.expires_ts is not None and identity.expires_ts <= self.clock():
            raise TokenError("jeton échu")
        return identity


class ExecutorTokens:
    """`TokenVerifier` sur l'identité des exécuteurs (L110), monté par
    `ameesh serve` : le jeton est vérifié par `identite.verify_token`
    (`kind="session"` : échu, révoqué, bail perdu ou epoch changé → refus),
    puis le bail est relu en base (`lease_owner`, même agent, même epoch,
    owner de cet exécuteur, vivant) : c'est cet owner qui est inscrit au
    grand livre.

    `verify(token) -> Principal` lève `AuthError` ; `lease(principal) ->
    owner | None`."""

    def __init__(self, verify: Callable[[str], object],
                 lease: Callable[[object], str | None]):
        self._verify = verify
        self._lease = lease

    def verify_token(self, token: str) -> Identity:
        from .executeur_mediee.interfaces import AuthError
        try:
            principal = self._verify(token)
        except AuthError as exc:
            raise TokenError(exc.code) from None
        if getattr(principal, "kind", None) != "session" or not principal.agent:
            raise TokenError("jeton de session attendu")
        owner = self._lease(principal)
        if not owner:
            raise TokenError("bail du jeton de session terminé")
        return Identity(executor=principal.executor_id, host=principal.host,
                        agent=principal.agent, lease_owner=owner,
                        lease_epoch=int(principal.epoch),
                        expires_ts=principal.expires_ts or None)


# ==========================================================================
# modèles admis
# ==========================================================================

class ModelPolicy(Protocol):
    def refusal(self, identity: Identity, model: str) -> str:
        """'' si `model` est admis pour cet agent sur cet hôte, sinon la raison."""

    def admitted(self, identity: Identity) -> list[str]:
        """Les motifs de modèles admis (pour `GET /v1/models`)."""

    def daily_usd(self, identity: Identity) -> float | None:
        """`budget_usd_per_day` de la persona, ou None."""


class StaticPolicy:
    """Politique en mémoire : `{agent: [motifs]}` (tests, banc)."""

    def __init__(self, models: dict, daily: dict | None = None):
        self.models = {k: list(v) for k, v in models.items()}
        self.daily = dict(daily or {})

    def admitted(self, identity: Identity) -> list[str]:
        return list(self.models.get(identity.agent) or [])

    def refusal(self, identity: Identity, model: str) -> str:
        patterns = self.admitted(identity)
        if model and any(fnmatch.fnmatchcase(model, p) for p in patterns):
            return ""
        return "modèle %s non admis pour %s (admis : %s)" % (
            model or "absent", identity.agent, ", ".join(patterns) or "aucun")

    def daily_usd(self, identity: Identity) -> float | None:
        return self.daily.get(identity.agent)


class CanonPolicy:
    """Politique tirée du canon : persona ∩ politique `models` de la fiche Host.

    * la persona et l'hôte doivent exister dans le canon, le harnais de la
      persona doit être celui du fournisseur relayé, et le placement ne doit
      violer aucune règle de l'hôte (`canon.placement_violations`) ;
    * modèle admis = le modèle de la persona (sinon le défaut du descripteur
      du harnais), s'il passe aussi les motifs `policy.models` de l'hôte.

    `canon` est un appelable qui rend le canon courant (relu à chaud par le
    serveur) : la politique suit le canon approuvé, jamais une déclaration de
    l'appareil.
    """

    def __init__(self, canon: Callable[[], object], harness: str = "deepseek"):
        self.canon = canon
        self.harness = harness

    def _lookup(self, identity: Identity):
        from . import canon as canon_mod
        current = self.canon()
        persona = next((a for a in getattr(current, "agents", []) or []
                        if a.title == identity.agent), None)
        host = next((h for h in getattr(current, "hosts", []) or []
                     if h.title == identity.host), None)
        if persona is None:
            return None, None, "persona %s absente du canon" % identity.agent
        if host is None:
            return None, None, "hôte %s absent du canon" % identity.host
        if (persona.harness or "") != self.harness:
            return None, None, "persona %s : harnais %s, pas %s" % (
                identity.agent, persona.harness or "non déclaré", self.harness)
        # le placement est jugé avec le modèle que l'exécuteur passera
        # vraiment (celui de la persona, sinon le défaut du descripteur, L60)
        effective = {"harness": persona.harness, "provider": persona.provider,
                     "model": self._persona_model(persona) or None,
                     "credential_mode": persona.credential_mode}
        violations = canon_mod.placement_violations(effective, host)
        if violations:
            return None, None, "; ".join(violations)
        return persona, host, ""

    def _persona_model(self, persona) -> str:
        if persona.model:
            return persona.model
        # même défaut que l'exécuteur (L60) : celui du descripteur du harnais
        from . import harnesses
        descriptor = harnesses.get(self.harness)
        return descriptor.default_of("model") if descriptor is not None else ""

    def admitted(self, identity: Identity) -> list[str]:
        persona, host, problem = self._lookup(identity)
        if problem:
            return []
        model = self._persona_model(persona)
        patterns = host.policy.models
        if not model:
            return []
        if patterns is not None and not any(fnmatch.fnmatchcase(model, p) for p in patterns):
            return []
        return [model]

    def refusal(self, identity: Identity, model: str) -> str:
        persona, host, problem = self._lookup(identity)
        if problem:
            return problem
        admitted = self.admitted(identity)
        if model and model in admitted:
            return ""
        return "modèle %s non admis pour %s sur %s (admis : %s)" % (
            model or "absent", identity.agent, identity.host, ", ".join(admitted) or "aucun")

    def daily_usd(self, identity: Identity) -> float | None:
        persona, _host, problem = self._lookup(identity)
        if problem or persona is None:
            return None
        return persona.budget_usd_per_day


# ==========================================================================
# secrets du serveur
# ==========================================================================

class SecretError(Exception):
    """Clé du fournisseur indisponible sur le serveur (→ 503)."""


class ServerSecrets:
    """La clé d'un fournisseur, lue côté serveur seulement.

    Ordre : fichier privé nommé par `AMEESH_RELAY_<FOURNISSEUR>_KEY_FILE`
    (mode 0600 exigé, comme `api_key_env`/`key_file` des comptes), sinon la
    variable de l'environnement **du service** (`DEEPSEEK_API_KEY`). Jamais le
    canon, jamais la base. Relue à chaque requête : une rotation de clé prend
    effet sans redémarrage.
    """

    def __init__(self, environ: dict | None = None):
        self.environ = os.environ if environ is None else environ

    def key(self, provider: str) -> str:
        spec = PROVIDERS.get(provider)
        if spec is None:
            raise SecretError("fournisseur inconnu")
        path = (self.environ.get(spec["key_file_env"]) or "").strip()
        if path:
            try:
                info = os.stat(path)
                if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                    raise SecretError("fichier de clé lisible par d'autres (chmod 600)")
                with open(path, encoding="utf-8") as fh:
                    value = fh.read().strip()
            except OSError as exc:
                raise SecretError("fichier de clé illisible (%s)" % exc.strerror) from None
        else:
            value = (self.environ.get(spec["key_env"]) or "").strip()
        if not value:
            raise SecretError("clé du fournisseur absente du serveur")
        return value


# ==========================================================================
# mesure de l'usage
# ==========================================================================

@dataclass
class Usage:
    model: str = ""
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    #: le relevé final (`message_delta`, ou corps complet) a été vu
    complete: bool = False
    #: le début (`message_start`, ou corps complet) a été vu
    started: bool = False
    #: caractères produits vus dans le flux (estimation d'un flux coupé)
    produced_chars: int = 0

    @property
    def billed_input(self) -> int:
        """Jetons d'entrée au tarif plein : l'écriture de cache est comptée
        comme de l'entrée (DeepSeek n'a pas de tarif d'écriture distinct)."""
        return self.input_tokens + self.cache_creation_tokens


def _int(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


class UsageMeter:
    """Relève l'usage dans les événements du protocole Messages.

    `message_start.message.usage` porte l'entrée (et le cache) ;
    `message_delta.usage` porte la sortie **cumulée** (et, chez certains
    fournisseurs, l'entrée répétée) : on garde le maximum de chaque compteur.
    """

    def __init__(self, requested_model: str):
        self.usage = Usage(model=requested_model)

    def _absorb(self, usage: dict) -> None:
        if not isinstance(usage, dict):
            return
        u = self.usage
        u.input_tokens = max(u.input_tokens, _int(usage.get("input_tokens")))
        u.cache_read_tokens = max(u.cache_read_tokens, _int(usage.get("cache_read_input_tokens")))
        u.cache_creation_tokens = max(u.cache_creation_tokens,
                                      _int(usage.get("cache_creation_input_tokens")))
        u.output_tokens = max(u.output_tokens, _int(usage.get("output_tokens")))

    def event(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "message_start":
            message = data.get("message") or {}
            if isinstance(message, dict):
                if message.get("model"):
                    self.usage.model = str(message["model"])[:200]
                self._absorb(message.get("usage") or {})
            self.usage.started = True
        elif kind == "message_delta":
            self._absorb(data.get("usage") or {})
            self.usage.complete = True
        elif kind == "content_block_delta":
            delta = data.get("delta") or {}
            if isinstance(delta, dict):
                for key in ("text", "thinking", "partial_json"):
                    value = delta.get(key)
                    if isinstance(value, str):
                        self.usage.produced_chars += len(value)

    def sse_line(self, line: bytes) -> None:
        """Une ligne du flux SSE (`data: {...}`)."""
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            self.event(json.loads(payload))
        except ValueError:
            return

    def body(self, raw: bytes) -> None:
        """Une réponse complète (requête sans flux)."""
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if isinstance(data, dict):
            if data.get("model"):
                self.usage.model = str(data["model"])[:200]
            self._absorb(data.get("usage") or {})
            self.usage.started = self.usage.complete = True


def price_usage(usage: Usage, harness: str, prices: dict) -> float:
    """Coût en dollars : barème × jetons ; un modèle inconnu du barème est
    facturé au majorant de sa famille (0019 §3, fail-closed)."""
    model = usage.model or ""
    triplet = prices[model] if model in prices else cost_mod.max_price_of(prices, harness)
    return (usage.billed_input * triplet[0] + usage.cache_read_tokens * triplet[1]
            + usage.output_tokens * triplet[2]) / 1_000_000.0


def settle(usage: Usage, request_bytes: int, max_tokens: int) -> Usage:
    """L'usage à facturer, complété par excès quand le flux a été coupé.

    Sans relevé d'entrée : un jeton pour trois octets de requête. Sans relevé
    final de sortie : le plus grand de ce qui a été vu, d'un jeton pour deux
    caractères produits — borné par `max_tokens`.
    """
    out = dataclasses.replace(usage)
    if not out.started:
        out.input_tokens = max(out.input_tokens, int(math.ceil(request_bytes / 3.0)))
    if not out.complete:
        estimate = int(math.ceil(out.produced_chars / 2.0))
        out.output_tokens = min(max(out.output_tokens, estimate), max(max_tokens, out.output_tokens))
    return out


# ==========================================================================
# débit
# ==========================================================================

class Limiter:
    """Seau à jetons par agent (requêtes/minute) et flux simultanés par agent."""

    def __init__(self, per_minute: int, concurrency: int,
                 clock: Callable[[], float] = time.monotonic):
        self.per_minute = max(1, int(per_minute))
        self.concurrency = max(1, int(concurrency))
        self.clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}
        self._active: dict[str, int] = {}

    def take(self, key: str) -> float:
        """0 si la requête passe, sinon le délai conseillé (secondes)."""
        with self._lock:
            now = self.clock()
            tokens, at = self._buckets.get(key, (float(self.per_minute), now))
            tokens = min(float(self.per_minute), tokens + (now - at) * self.per_minute / 60.0)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return (1.0 - tokens) * 60.0 / self.per_minute
            self._buckets[key] = (tokens - 1.0, now)
            return 0.0

    def enter(self, key: str) -> bool:
        with self._lock:
            if self._active.get(key, 0) >= self.concurrency:
                return False
            self._active[key] = self._active.get(key, 0) + 1
            return True

    def leave(self, key: str) -> None:
        with self._lock:
            left = self._active.get(key, 0) - 1
            if left > 0:
                self._active[key] = left
            else:
                self._active.pop(key, None)


# ==========================================================================
# le relais
# ==========================================================================

@dataclass
class RelayConfig:
    upstreams: dict = dataclasses.field(default_factory=dict)
    max_body: int = DEFAULT_MAX_BODY
    max_tokens: int = DEFAULT_MAX_TOKENS
    rate_per_minute: int = DEFAULT_RATE_PER_MINUTE
    concurrency: int = DEFAULT_CONCURRENCY
    upstream_timeout: float = DEFAULT_UPSTREAM_TIMEOUT
    max_stream: int = DEFAULT_MAX_STREAM

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "RelayConfig":
        env = os.environ if environ is None else environ

        def number(name: str, default, kind=int):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = kind(raw)
            except ValueError:
                raise SystemExit("%s : nombre attendu" % name) from None
            if value <= 0:
                raise SystemExit("%s : valeur strictement positive attendue" % name)
            return value

        upstreams = {name: (env.get(spec["upstream_env"]) or spec["upstream"]).rstrip("/")
                     for name, spec in PROVIDERS.items()}
        return cls(upstreams=upstreams,
                   max_body=number("AMEESH_RELAY_MAX_BODY", DEFAULT_MAX_BODY),
                   max_tokens=number("AMEESH_RELAY_MAX_TOKENS", DEFAULT_MAX_TOKENS),
                   rate_per_minute=number("AMEESH_RELAY_RATE", DEFAULT_RATE_PER_MINUTE),
                   concurrency=number("AMEESH_RELAY_CONCURRENCY", DEFAULT_CONCURRENCY),
                   upstream_timeout=number("AMEESH_RELAY_UPSTREAM_TIMEOUT",
                                           DEFAULT_UPSTREAM_TIMEOUT, float),
                   max_stream=number("AMEESH_RELAY_MAX_STREAM", DEFAULT_MAX_STREAM))


class Refused(Exception):
    """Refus d'une requête : statut HTTP, type d'erreur (format Messages), raison."""

    def __init__(self, status: int, kind: str, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


def check_upstream(url: str) -> None:
    """HTTPS exigé, sauf boucle locale (faux fournisseur des essais)."""
    parts = urllib.parse.urlsplit(url)
    loopback = parts.hostname in ("127.0.0.1", "::1", "localhost")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("URL d'amont invalide : %s" % url)
    if parts.scheme != "https" and not loopback:
        raise ValueError("amont en clair hors boucle locale refusé : %s" % url)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("URL d'amont : ni identifiants, ni requête, ni fragment")


class Relay:
    """Le relais : vérifie, borne, transmet, mesure, enregistre.

    `db` : connexion `ameesh.db` (grand livre, plafonds) ; `cfg` : la
    configuration de l'hôte serveur (`budget_usd_per_hour`…, défauts des
    plafonds du mesh). L'amont est une URL par fournisseur
    (`RelayConfig.upstreams`) : le faux fournisseur local des essais.
    """

    def __init__(self, *, verifier: TokenVerifier, policy: ModelPolicy, db, cfg,
                 secrets: ServerSecrets | None = None, config: RelayConfig | None = None,
                 prices: dict | None = None, budget_cache=None,
                 paid_harnesses: Callable[[], tuple] | None = None):
        from . import budget as budget_mod
        self.verifier = verifier
        self.policy = policy
        self.db = db
        self.cfg = cfg
        self.secrets = secrets or ServerSecrets()
        self.config = config or RelayConfig.from_env()
        for url in self.config.upstreams.values():
            check_upstream(url)
        self.prices = prices if prices is not None else cost_mod.load_prices(
            os.environ.get("AMEESH_PRICES"))
        self.budget_cache = budget_cache or budget_mod.Cache()
        self.paid_harnesses = paid_harnesses or cost_mod.paid_harnesses_of
        self.limiter = Limiter(self.config.rate_per_minute, self.config.concurrency)
        self._db_lock = threading.Lock()
        #: dépenses mesurées dont l'écriture a échoué : comptées au plafond et
        #: réécrites à la requête suivante (jamais perdues tant que le
        #: processus vit)
        self._unrecorded: list[dict] = []

    # -- entrée -------------------------------------------------------------
    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        """Traite une requête dont le chemin commence par `PREFIX`."""
        identity = None
        slot = False
        try:
            provider, rest = self._route(handler)
            identity = self._authenticate(handler)
            wait = self.limiter.take(identity.agent)
            if wait:
                raise Refused(429, "rate_limit_error", "débit du relais dépassé", wait)
            if handler.command == "GET":
                self._models(handler, identity)
                return
            raw = self._read_body(handler)
            body = self._parse(raw)
            model = body.get("model")
            if not isinstance(model, str) or not model:
                raise Refused(400, "invalid_request_error", "champ model absent")
            try:
                reason = self.policy.refusal(identity, model)
            except Exception as exc:  # canon illisible : rien n'est admis
                log.error("relais : politique illisible (%s)", type(exc).__name__)
                raise Refused(503, "api_error", "politique des modèles illisible") from None
            if reason:
                log.warning("relais : refus de modèle (%s, %s) : %s",
                            identity.agent, identity.host, reason)
                raise Refused(403, "permission_error", reason)
            cap = self.config.max_tokens
            asked = body.get("max_tokens")
            if not isinstance(asked, int) or asked <= 0 or asked > cap:
                body["max_tokens"] = cap
            self._check_budget(identity, provider)
            if not self.limiter.enter(identity.agent):
                raise Refused(429, "rate_limit_error", "trop de flux simultanés pour cet agent", 1.0)
            slot = True
            try:
                key = self.secrets.key(provider)
            except SecretError as exc:
                log.error("relais : %s", exc)
                raise Refused(503, "api_error", "relais indisponible (clé du serveur)") from None
            self._forward(handler, identity, provider, rest, body, key, len(raw))
        except Refused as exc:
            self._error(handler, exc)
        finally:
            if slot and identity is not None:
                self.limiter.leave(identity.agent)

    # -- étapes -------------------------------------------------------------
    def _route(self, handler) -> tuple[str, str]:
        path = urllib.parse.urlsplit(handler.path).path
        if not path.startswith(PREFIX):
            raise Refused(404, "not_found_error", "route inconnue")
        provider, _, rest = path[len(PREFIX):].partition("/")
        if provider not in PROVIDERS:
            raise Refused(404, "not_found_error", "fournisseur inconnu")
        rest = rest.rstrip("/")
        if handler.command == "POST" and rest == "v1/messages":
            return provider, rest
        if handler.command == "GET" and rest == "v1/models":
            return provider, rest
        # `/v1/files` n'est pas relayé : les fichiers téléversés vivraient
        # sous la clé du serveur, partagée entre appareils ; dsh retombe sur
        # les images en ligne (base64) quand la résolution d'un fichier échoue.
        raise Refused(404, "not_found_error", "route non relayée")

    def _authenticate(self, handler) -> Identity:
        token = (handler.headers.get("x-api-key") or "").strip()
        if not token:
            auth = handler.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        if not token:
            raise Refused(401, "authentication_error", "jeton de session absent")
        try:
            return self.verifier.verify_token(token)
        except TokenError as exc:
            raise Refused(401, "authentication_error", "jeton refusé (%s)" % exc) from None

    def _read_body(self, handler) -> bytes:
        length = handler.headers.get("content-length")
        if length is None or not length.strip().isdigit():
            raise Refused(411, "invalid_request_error", "Content-Length obligatoire")
        size = int(length)
        if size > self.config.max_body:
            handler.close_connection = True
            raise Refused(413, "request_too_large",
                          "requête de %d octets (plafond %d)" % (size, self.config.max_body))
        raw = handler.rfile.read(size)
        if len(raw) != size:
            raise Refused(400, "invalid_request_error", "corps incomplet")
        return raw

    @staticmethod
    def _parse(raw: bytes) -> dict:
        try:
            body = json.loads(raw)
        except ValueError:
            raise Refused(400, "invalid_request_error", "corps JSON attendu") from None
        if not isinstance(body, dict):
            raise Refused(400, "invalid_request_error", "objet JSON attendu")
        return body

    def _check_budget(self, identity: Identity, provider: str) -> None:
        """Plafonds L70 et budget de la persona ; fail-closed."""
        from . import budget as budget_mod
        from . import db as db_mod
        self._flush_unrecorded()
        try:
            with self._db_lock:
                limits = self.budget_cache.limits(self.cfg, self.db)
                paid = tuple(self.paid_harnesses())
                harness = PROVIDERS[provider]["harness"]
                agent_limits = {a: dict(v) for a, v in limits.agents.items()}
                daily = self.policy.daily_usd(identity)
                if daily and daily > 0:
                    mine = agent_limits.setdefault(identity.agent, {})
                    mine[86400] = min(mine.get(86400, daily), daily)
                book = cost_mod.CostBook(state_dir="", db=self.db,
                                         tools={identity.agent: harness},
                                         hourly_usd=limits.per_hour, daily_usd=limits.per_day,
                                         agent_limits=agent_limits)
                reason = book.over(identity.agent, paid_harnesses=paid, pace=False)
                if not reason and self._unrecorded:
                    reason = self._over_with_unrecorded(book, identity, paid)
        except (db_mod.DbError, cost_mod.CostError, budget_mod.BudgetError) as exc:
            log.error("relais : dépense illisible (%s) : refus", " ".join(str(exc).split())[:200])
            raise Refused(503, "api_error", "dépense illisible : relais fermé") from None
        if reason:
            raise Refused(402, "budget_exceeded", reason)

    def _over_with_unrecorded(self, book, identity: Identity, paid) -> str:
        pending_all = sum(r["usd"] for r in self._unrecorded)
        pending_mine = sum(r["usd"] for r in self._unrecorded if r["agent"] == identity.agent)
        for window, cap in ((3600, book.hourly_usd), (86400, book.daily_usd)):
            if cap and cap > 0 and book.spent("all", window, harnesses=paid) + pending_all >= cap:
                return "budget du mesh atteint (dépense non encore écrite comprise)"
        for window, cap in (book.agent_limits.get(identity.agent) or {}).items():
            if cap and cap > 0 and (book.spent(identity.agent, window, harnesses=paid)
                                    + pending_mine) >= cap:
                return "budget de l'agent %s atteint (dépense non encore écrite comprise)" \
                    % identity.agent
        return ""

    def _models(self, handler, identity: Identity) -> None:
        data = [{"type": "model", "id": m, "display_name": m}
                for m in self.policy.admitted(identity)]
        payload = json.dumps({"data": data, "has_more": False}).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(payload)

    # -- transmission -------------------------------------------------------
    def _connection(self, url: str) -> http.client.HTTPConnection:
        parts = urllib.parse.urlsplit(url)
        kind = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        return kind(parts.hostname, parts.port, timeout=self.config.upstream_timeout)

    def _forward(self, handler, identity: Identity, provider: str, rest: str, body: dict,
                 key: str, request_bytes: int) -> None:
        base = self.config.upstreams[provider]
        target = urllib.parse.urlsplit(base)
        prefix = target.path.rstrip("/")
        # même règle que dsh : un `/v1` final de la base est réutilisé
        path = (prefix[:-3] if prefix.endswith("/v1") else prefix) + "/" + rest
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream" if body.get("stream") else "application/json",
                   "Accept-Encoding": "identity",
                   "x-api-key": key,
                   "anthropic-version": DEFAULT_ANTHROPIC_VERSION}
        for name in FORWARD_HEADERS:
            value = handler.headers.get(name)
            if value:
                headers[name] = value
        meter = UsageMeter(str(body.get("model") or ""))
        turn = identity.turn or self._turn_header(handler)
        session = (handler.headers.get("x-deepseek-harness-session-id") or "").strip()
        session = session if _TURN_RE.match(session or "-") else ""
        conn = self._connection(base)
        reached = False
        status = 0
        try:
            try:
                conn.request("POST", path, body=payload, headers=headers)
                response = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                log.warning("relais : amont injoignable (%s)", type(exc).__name__)
                raise Refused(502, "api_error", "fournisseur injoignable") from None
            reached = True
            status = response.status
            ctype = response.getheader("content-type") or "application/json"
            streaming = status == 200 and ctype.startswith("text/event-stream")
            handler.send_response(status)
            handler.send_header("Content-Type", ctype)
            request_id = response.getheader("request-id")
            if request_id and len(request_id) < 200:
                handler.send_header("request-id", request_id)
            handler.send_header("Cache-Control", "no-store")
            if streaming:
                handler.send_header("Connection", "close")
                handler.close_connection = True
                handler.end_headers()
                self._pipe_stream(handler, response, meter)
            else:
                raw = response.read(MAX_BUFFERED_RESPONSE + 1)
                if len(raw) > MAX_BUFFERED_RESPONSE:
                    raw = raw[:MAX_BUFFERED_RESPONSE]
                    handler.close_connection = True
                if status == 200:
                    meter.body(raw)
                handler.send_header("Content-Length", str(len(raw)))
                handler.end_headers()
                try:
                    handler.wfile.write(raw)
                except OSError:
                    pass
        finally:
            conn.close()
            if reached and status == 200:
                billed = settle(meter.usage, request_bytes, int(body.get("max_tokens") or 0))
                self._record(identity, provider, billed, turn, session or None)

    def _pipe_stream(self, handler, response, meter: UsageMeter) -> None:
        sent = 0
        while True:
            try:
                line = response.readline(1024 * 1024 + 1)
            except (OSError, http.client.HTTPException):
                break
            if not line:
                break
            meter.sse_line(line)
            sent += len(line)
            if sent > self.config.max_stream:
                log.warning("relais : flux tronqué au plafond (%d octets)", self.config.max_stream)
                break
            try:
                handler.wfile.write(line)
                if line in (b"\n", b"\r\n"):
                    handler.wfile.flush()
            except OSError:
                # l'appareil a coupé : on arrête l'amont ; l'usage vu est
                # facturé par excès (`settle`)
                break
        try:
            handler.wfile.flush()
        except OSError:
            pass

    @staticmethod
    def _turn_header(handler) -> str | None:
        value = (handler.headers.get("x-ameesh-turn") or "").strip()
        return value if value and _TURN_RE.match(value) else None

    # -- grand livre --------------------------------------------------------
    def _record(self, identity: Identity, provider: str, usage: Usage, turn: str | None,
                session: str | None) -> None:
        harness = PROVIDERS[provider]["harness"]
        row = {"agent": identity.agent, "harness": harness, "turn": turn,
               "model": usage.model or None, "session": session,
               "usd": round(price_usage(usage, harness, self.prices), 6),
               "input_tokens": usage.billed_input,
               "cached_input_tokens": usage.cache_read_tokens,
               "output_tokens": usage.output_tokens,
               "executor": identity.executor, "lease_owner": identity.lease_owner,
               "lease_epoch": int(identity.lease_epoch)}
        if not self._write(row):
            self._unrecorded.append(row)
            log.error("relais : dépense de %s non écrite (%.6f $) : gardée en mémoire",
                      identity.agent, row["usd"])

    def _write(self, row: dict) -> bool:
        from . import db as db_mod
        try:
            with self._db_lock:
                storage.of(self.db).turn_costs.insert(
                    agent=row["agent"], harness=row["harness"], turn=row["turn"],
                    model=row["model"], session=row["session"], usd=row["usd"],
                    input_tokens=row["input_tokens"],
                    cached_input_tokens=row["cached_input_tokens"],
                    output_tokens=row["output_tokens"], cum_usd=None, cum_input_tokens=None,
                    cum_cached_input_tokens=None, cum_output_tokens=None,
                    source="relay", executor=row["executor"],
                    lease_owner=row["lease_owner"], lease_epoch=row["lease_epoch"])
            return True
        except db_mod.DbError:
            return False

    def _flush_unrecorded(self) -> None:
        while self._unrecorded:
            if not self._write(self._unrecorded[0]):
                return
            self._unrecorded.pop(0)

    # -- erreurs ------------------------------------------------------------
    @staticmethod
    def _error(handler, exc: Refused) -> None:
        payload = json.dumps({"type": "error",
                              "error": {"type": exc.kind, "message": exc.message}},
                             ensure_ascii=False).encode("utf-8")
        try:
            handler.send_response(exc.status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Cache-Control", "no-store")
            # le corps d'une requête refusée n'est pas toujours lu : la
            # connexion ne doit pas resservir (sinon il serait lu comme une
            # requête suivante)
            handler.send_header("Connection", "close")
            handler.close_connection = True
            if exc.retry_after:
                handler.send_header("Retry-After", str(max(1, int(math.ceil(exc.retry_after)))))
            handler.end_headers()
            handler.wfile.write(payload)
        except OSError:
            pass


# ==========================================================================
# serveur autonome (en attendant le montage dans `ameesh serve`, L108)
# ==========================================================================

class RelayHandler(BaseHTTPRequestHandler):
    server_version = "ameesh-relay"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, format, *args) -> None:  # noqa: A002 (signature de http.server)
        # jamais d'en-tête ni de corps : la méthode, le chemin et le statut
        log.info("%s %s", self.address_string(), format % args)

    def do_POST(self) -> None:
        self.server.relay.handle(self)

    def do_GET(self) -> None:
        if urllib.parse.urlsplit(self.path).path == "/api/exec/v1/llm/health":
            payload = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.server.relay.handle(self)


class RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, relay: Relay, address: tuple[str, int]):
        super().__init__(address, RelayHandler)
        self.relay = relay


def make_server(relay: Relay, bind: str = "127.0.0.1:0") -> RelayHTTPServer:
    host, _, port = bind.rpartition(":")
    return RelayHTTPServer(relay, (host or "127.0.0.1", int(port or 0)))


def load_verifier(spec: str) -> TokenVerifier:
    """`module:objet` : un objet qui a `verify_token`, ou une fabrique sans argument."""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise SystemExit("AMEESH_RELAY_VERIFIER : forme module:objet attendue")
    target = getattr(importlib.import_module(module_name), attr)
    if not hasattr(target, "verify_token"):
        target = target()
    if not hasattr(target, "verify_token"):
        raise SystemExit("AMEESH_RELAY_VERIFIER : l'objet n'a pas de verify_token")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ameesh.relay",
                                     description="Relais de modèle du serveur du mesh (L111).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="servir le relais seul")
    p_serve.add_argument("--bind", default=os.environ.get("AMEESH_RELAY_BIND", "127.0.0.1:8787"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    spec = (os.environ.get("AMEESH_RELAY_VERIFIER") or "").strip()
    if not spec:
        print("relais : aucun vérificateur de jeton (AMEESH_RELAY_VERIFIER=module:objet, "
              "fourni par l'enrôlement des exécuteurs, L110) : refus de démarrer",
              file=sys.stderr)
        return 2
    from . import config as config_mod
    from . import db as db_mod
    cfg = config_mod.load()
    db = db_mod.connect(cfg)
    policy = CanonPolicy(_canon_loader(cfg))
    relay = Relay(verifier=load_verifier(spec), policy=policy, db=db, cfg=cfg)
    server = make_server(relay, args.bind)
    print("relais de modèle sur %s:%d" % server.server_address[:2], file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _canon_loader(cfg) -> Callable[[], object]:
    """Le canon approuvé du serveur, relu au plus toutes les 30 s."""
    from . import canon as canon_mod
    state = {"at": 0.0, "canon": None}
    lock = threading.Lock()

    def current():
        with lock:
            if state["canon"] is None or time.monotonic() - state["at"] > 30.0:
                # canon approuvé (branche de confiance) : jamais les fichiers
                # de travail ni une déclaration de l'appareil
                state["canon"] = canon_mod.from_config(cfg)
                state["at"] = time.monotonic()
            return state["canon"]
    return current


# ==========================================================================
# côté appareil : l'environnement du harnais au lancement du tour
# ==========================================================================

class RelayTurnError(Exception):
    """Le tour ne peut pas être relayé (jeton indisponible) : il n'est pas lancé."""


#: source de jetons de session de l'exécuteur médié (L109/L110) :
#: `(agent, owner, epoch, turn_id) -> jeton`. None : pas de jeton → pas de tour.
SessionTokenSource = Callable[[str, str, int, str], str]
_token_source: SessionTokenSource | None = None


def set_session_token_source(source: SessionTokenSource | None) -> None:
    """Branche la source de jetons de session (exécuteur médié)."""
    global _token_source
    _token_source = source


def relay_url(environ: dict | None = None) -> str:
    """URL de base du relais pour cet exécuteur, ou '' (exécuteur non médié).

    `AMEESH_RELAY_URL` explicite, sinon `AMEESH_EXEC_URL` + `/api/exec/v1/llm`.
    """
    env = os.environ if environ is None else environ
    explicit = (env.get("AMEESH_RELAY_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    exec_url = (env.get("AMEESH_EXEC_URL") or "").strip().rstrip("/")
    return exec_url + "/api/exec/v1/llm" if exec_url else ""


def turn_env(harness: str, base_url: str, token: str) -> dict:
    """Les variables du harnais pour un tour relayé : URL du relais et jeton."""
    spec = HARNESS_ENV.get(harness)
    if spec is None:
        raise RelayTurnError("harnais %s non relayé" % harness)
    return {spec["base_url"]: base_url.rstrip("/") + "/" + spec["provider"],
            spec["key"]: token}


def apply_turn_env(env: dict, harness: str, *, agent: str, owner: str, epoch: int,
                   turn_id: str, environ: dict | None = None) -> bool:
    """Pose l'URL du relais et un jeton de session neuf dans `env` (le seul
    environnement du processus du harnais) ; rien n'est écrit sur disque.

    Faux : exécuteur non médié (aucune URL de relais) ou harnais non relayé,
    `env` inchangé. Lève `RelayTurnError` quand le tour DOIT passer par le
    relais mais qu'aucun jeton n'est disponible : jamais de repli sur une clé
    locale. Toute clé de fournisseur héritée est retirée de `env`.
    """
    base = relay_url(environ)
    if not base or harness not in HARNESS_ENV:
        return False
    for spec in HARNESS_ENV.values():
        env.pop(spec["key"], None)
        env.pop(spec["base_url"], None)
    if _token_source is None:
        raise RelayTurnError("exécuteur médié sans source de jeton de session")
    try:
        token = _token_source(agent, owner, int(epoch), turn_id)
    except Exception as exc:  # la source est un client HTTP : toute panne refuse le tour
        raise RelayTurnError("jeton de session indisponible (%s)" % type(exc).__name__) from None
    if not token:
        raise RelayTurnError("jeton de session vide")
    env.update(turn_env(harness, base, token))
    return True


if __name__ == "__main__":
    sys.exit(main())
