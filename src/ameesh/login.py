# SPDX-License-Identifier: AGPL-3.0-only
"""L232d socle : défis de connexion OAuth privés pour les CLI installés.

Ce module ne lance aucune connexion de lui-même : il construit la commande
**épinglée** d'un adaptateur (`claude auth login --claudeai`,
`codex login --device-auth`, `gh auth login --web`), la supervise dans un
groupe de processus dédié, et ne garde l'URL d'autorisation et le code à usage
unique que le temps de les remettre au service `ameesh-approve` par le rappel
`publish` (jamais sur disque, jamais dans un journal, jamais dans un `repr`).

L'humain décide (`done` | `cancel`) dans la vue privée ; le superviseur tue
alors le groupe (SIGTERM, puis SIGKILL après `grace`) et, sur `done`,
interroge la santé du compte (`health`) : un reçu « terminé » ne prouve pas le
grant fournisseur.

Règles du socle (mesh-design #81614) :

* une seule demande vivante par `(hôte, harnais, compte)` ;
* échéance `min(15 min, échéance fournisseur)` ;
* version installée hors épinglage ou flux non reconnu : refus, jamais de
  repli ni de copie de cache ;
* catégories structurées (`expired`, `cancelled`, `provider_refused`,
  `flow_unrecognized`, `healthy`…), jamais de valeur ni de condensat.

Frontière connue, laissée au pilote (§12) : le flux Claude est un
**collage en retour** (le CLI affiche l'URL, le code vient du navigateur vers
le terminal). Aucun canal privé de retour n'existe dans cette coupe : un flux
`paste_back` est donc refusé proprement (`flow_unrecognized`), sans jamais
publier l'URL.
"""
from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

#: échéance maximale d'un défi, alignée sur le service (`LOGIN_MAX_TTL`)
TTL_S = 900.0
#: délai entre SIGTERM et SIGKILL du groupe de processus
TERMINATE_GRACE_S = 5.0
#: sortie privée gardée en mémoire, jamais écrite sur disque
CAPTURE_MAX = 64 * 1024
#: pas d'attente des boucles de surveillance
POLL_S = 0.05

STARTING = "starting"
VIEWING = "viewing"
DONE = "done"
CANCELLED = "cancelled"
EXPIRED = "expired"
FAILED = "failed"
#: états d'une demande encore pilotable
LIVE = (STARTING, VIEWING)
#: états clos
TERMINAL = (DONE, CANCELLED, EXPIRED, FAILED)

DECISIONS = ("done", "cancel")

#: catégories journalisables (jamais une valeur, jamais un condensat)
CATEGORIES = ("already_pending", "unsupported_version", "flow_unrecognized",
              "expired", "cancelled", "provider_refused", "healthy", "failed")

_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
_URL_RE = re.compile(r"https://[^\s<>\"'`\\]+")
#: ce que les CLI disent d'un refus fournisseur (repris de `sentinel`)
_REFUSED_RE = re.compile(
    r"token in \S+ is invalid|not logged in|no active account"
    r"|do not currently have an active account|authentication is denied"
    r"|invalid (access|secret) key|unauthori[sz]ed|access denied|invalid_grant",
    re.IGNORECASE)


class LoginError(Exception):
    """Refus structuré : `category` est journalisable, `message` est lisible."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
        self.message = message


def new_request_id() -> str:
    """Identifiant local, même forme que ceux du service (`lgn_...`)."""
    return "lgn_" + "".join(secrets.choice(_CROCKFORD) for _ in range(26))


@dataclass(frozen=True)
class Adapter:
    """Commande épinglée d'un fournisseur ; l'URL et le code sont privés."""

    harness: str
    argv: tuple
    versions: tuple          #: couples « majeure.mineure » admis
    url_domains: tuple       #: hôtes officiels de l'URL d'autorisation
    code: re.Pattern | None = None   #: code à usage unique imprimé par le CLI
    paste_back: bool = False         #: le code revient par le terminal (Claude)
    refused: tuple = ()              #: options jamais transmises
    note: str = ""

    def __repr__(self) -> str:  # une commande épinglée n'est pas un secret
        return "Adapter(%r, %r)" % (self.harness, self.argv)


#: adaptateurs du lot, versions relevées le 16/10 (plan §11)
ADAPTERS = {
    "claude": Adapter(
        harness="claude",
        argv=("claude", "auth", "login", "--claudeai"),
        versions=("2.1",),
        url_domains=("claude.ai", "claude.com", "anthropic.com"),
        paste_back=True,
        refused=("--console", "--api-key", "setup-token"),
        note="--console et setup-token écartés (facturation API, hors pilote)"),
    "codex": Adapter(
        harness="codex",
        argv=("codex", "login", "--device-auth"),
        versions=("0.159",),
        url_domains=("chatgpt.com", "openai.com"),
        code=re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b"),
        refused=("--api-key",),
        note="device-auth seulement ; aucun repli par copie de cache"),
    "gh": Adapter(
        harness="gh",
        argv=("gh", "auth", "login", "--hostname", "github.com",
              "--git-protocol", "https", "--web"),
        versions=("2.102",),
        url_domains=("github.com",),
        code=re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b"),
        refused=("--clipboard", "--insecure-storage", "--with-token"),
        note="--clipboard et --insecure-storage écartés (hôte distant)"),
}


def supports(adapter: Adapter, version: str | None) -> bool:
    """La version installée est-elle épinglée pour cet adaptateur ?

    Une pré-version (`2.102.0-rc.1`) n'est jamais réputée conforme : sa
    signature de sortie n'est pas qualifiée.
    """
    base = version or ""
    if not base or "-" in base:
        return False
    parts = base.split(".")
    return len(parts) >= 2 and ".".join(parts[:2]) in adapter.versions


def check_extra(adapter: Adapter, extra=()) -> tuple:
    """Refuse une option hors épinglage (jamais de `--console`, etc.)."""
    for arg in extra:
        if not isinstance(arg, str) or not arg:
            raise LoginError("flow_unrecognized", "%s: invalid option" % adapter.harness)
        name = arg.split("=", 1)[0]
        if name in adapter.refused:
            raise LoginError("flow_unrecognized",
                             "%s refuses %s" % (adapter.harness, name))
    return tuple(extra)


#: variables de compte héritées qu'une connexion ne doit jamais voir
_DROP_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
             "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
             "OPENAI_API_KEY", "CODEX_API_KEY", "SSH_ASKPASS", "BROWSER")
#: dossier de configuration du CLI, par harnais
_HOME_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "gh": "GH_CONFIG_DIR"}


def login_env(adapter: Adapter, home: str = "", environ=None) -> dict:
    """Environnement épuré du CLI : aucune clé héritée, dossier du compte.

    Les variables de jeton connues sont retirées, jamais remplacées ; le CLI
    ne peut donc pas réussir « par héritage » ce que la connexion prépare.
    """
    env = dict(os.environ if environ is None else environ)
    for name in _DROP_ENV:
        env.pop(name, None)
    variable = _HOME_ENV.get(adapter.harness)
    if variable and home:
        env[variable] = home
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


@dataclass(frozen=True)
class Flow:
    """URL d'autorisation et code à usage unique, privés par construction."""

    url: str = field(repr=False, default="")
    code: str = field(repr=False, default="")

    def __repr__(self) -> str:
        return "Flow(<private>)"


def _clean_url(raw: str) -> str:
    return raw.rstrip(".,;:!?)]}'\"<>")


def scan_flow(adapter: Adapter, version: str | None, text: str) -> Flow | None:
    """Le flux reconnu dans la sortie du CLI, ou None tant qu'elle est
    incomplète. Refuse (`flow_unrecognized`) une version non épinglée, une URL
    hors des hôtes officiels, ou une URL d'autorisation absente d'une sortie
    qui prétend pourtant en porter une."""
    if not supports(adapter, version):
        raise LoginError("unsupported_version",
                         "%s %s is not a pinned version (%s)"
                         % (adapter.harness, version or "?", ", ".join(adapter.versions)))
    match = _URL_RE.search(text or "")
    if not match:
        return None
    url = _clean_url(match.group(0))
    if not url:
        return None
    host = urlsplit(url).hostname or ""
    if not any(host == domain or host.endswith("." + domain)
               for domain in adapter.url_domains):
        raise LoginError("flow_unrecognized",
                         "%s: authorization URL is not an official host" % adapter.harness)
    if adapter.code is None:
        return Flow(url=url)
    found = adapter.code.search(text or "")
    if not found:
        return None
    return Flow(url=url, code=found.group(1))


def parse_flow(adapter: Adapter, version: str | None, text: str) -> Flow:
    """`scan_flow` qui exige un flux complet (sinon `flow_unrecognized`)."""
    flow = scan_flow(adapter, version, text)
    if flow is None:
        raise LoginError("flow_unrecognized",
                         "%s: no recognized login flow in the output" % adapter.harness)
    return flow


@dataclass
class Request:
    """État journalisable d'une demande ; ni URL, ni code, ni lien."""

    id: str
    host: str
    harness: str
    account: str
    home: str
    version: str
    created: float
    expires: float
    state: str = STARTING
    category: str = ""
    pid: int | None = None
    login_id: str = ""
    link: str = field(default="", repr=False)
    #: une décision est en cours : la fin du processus ne doit plus rien clore
    closing: bool = field(default=False, repr=False, compare=False)

    def describe(self) -> dict:
        return {"id": self.id, "host": self.host, "harness": self.harness,
                "account": self.account, "version": self.version,
                "state": self.state, "category": self.category,
                "expires": self.expires, "pid": self.pid, "login_id": self.login_id}

    def __repr__(self) -> str:
        return ("Request(%r, %s/%s/%s, %s%s)"
                % (self.id, self.host, self.harness, self.account, self.state,
                   " " + self.category if self.category else ""))


class Registry:
    """Une seule demande vivante par `(hôte, harnais, compte)`, horloge injectée."""

    def __init__(self, *, clock=time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._requests: dict[str, Request] = {}
        self._keys: dict[tuple, str] = {}

    def open(self, *, host: str, harness: str, account: str, home: str = "",
             version: str = "", ttl: float = TTL_S,
             provider_exp: float | None = None) -> Request:
        now = self._clock()
        key = (host, harness, account)
        with self._lock:
            current = self._requests.get(self._keys.get(key, ""))
            if current is not None and current.state in LIVE:
                raise LoginError("already_pending",
                                 "%s/%s on %s already has a live login request"
                                 % (harness, account, host))
            expires = now + max(0.0, float(ttl))
            if provider_exp is not None:
                expires = min(expires, float(provider_exp))
            request = Request(id=new_request_id(), host=host, harness=harness,
                              account=account, home=home, version=version or "",
                              created=now, expires=expires)
            self._requests[request.id] = request
            self._keys[key] = request.id
            return request

    def get(self, request_id: str) -> Request | None:
        with self._lock:
            return self._requests.get(request_id or "")

    def live(self, *, host: str | None = None, harness: str | None = None,
             account: str | None = None) -> list[Request]:
        with self._lock:
            return [r for r in self._requests.values() if r.state in LIVE
                    and (host is None or r.host == host)
                    and (harness is None or r.harness == harness)
                    and (account is None or r.account == account)]

    def finish(self, request: Request, state: str, category: str = "") -> Request:
        if state not in TERMINAL:
            raise LoginError("format", "terminal state expected")
        with self._lock:
            if request.state in TERMINAL:
                return request
            request.state = state
            request.category = category
            request.closing = True
            key = (request.host, request.harness, request.account)
            if self._keys.get(key) == request.id:
                del self._keys[key]
        return request

    def reap(self, now: float | None = None) -> list[Request]:
        """Clôt les demandes vivantes dont l'échéance est passée."""
        now = self._clock() if now is None else now
        with self._lock:
            due = [r for r in self._requests.values()
                   if r.state in LIVE and r.expires <= now]
        for request in due:
            self.finish(request, EXPIRED, "expired")
        return due

    def touch(self, request: Request, **fields) -> Request:
        """Met à jour des champs autorisés (`state`, `login_id`, `link`, `pid`)."""
        allowed = {"state", "category", "pid", "login_id", "link"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise LoginError("format", "unknown field(s): %s" % ", ".join(unknown))
        with self._lock:
            for name, value in fields.items():
                setattr(request, name, value)
        return request


class Supervisor:
    """Pilote un adaptateur : démarrage, flux privé, décision, santé.

    `publish(payload)` remet `{host, provider, account, approver, requested_by,
    scope, url, code, provider_exp}` au service de défi et rend au moins
    `{"login_id": "lgn_…"}` (et `link` s'il le donne). `health(request)` rend
    `"healthy"` (ou True) après une décision `done`. Aucun des deux ne doit
    journaliser ce qu'il reçoit.
    """

    def __init__(self, *, registry: Registry | None = None, clock=time.time,
                 spawn=subprocess.Popen, publish=None, health=None,
                 sleep=time.sleep, grace: float = TERMINATE_GRACE_S,
                 capture_max: int = CAPTURE_MAX):
        self.registry = registry if registry is not None else Registry(clock=clock)
        self.clock = clock
        self.spawn = spawn
        self.publish = publish
        self.health = health
        self.sleep = sleep
        self.grace = grace
        self.capture_max = capture_max
        self._procs: dict[str, object] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    # -- cycle de vie ------------------------------------------------------
    def begin(self, *, host: str, harness: str, account: str, home: str = "",
              version: str | None = None, approver: str = "", requested_by: str = "",
              scope: str = "", provider_exp: float | None = None,
              ttl: float = TTL_S, environ=None, cwd: str | None = None) -> Request:
        adapter = ADAPTERS.get(harness)
        if adapter is None:
            raise LoginError("unsupported_version", "no login adapter for %r" % harness)
        if not supports(adapter, version):
            raise LoginError("unsupported_version",
                             "%s %s is not a pinned version (%s)"
                             % (harness, version or "?", ", ".join(adapter.versions)))
        if adapter.paste_back:
            # §12 : le code de retour navigateur → terminal n'a pas de canal
            # privé dans cette coupe ; on refuse avant toute publication.
            raise LoginError("flow_unrecognized",
                             "%s: paste-back flow has no private return channel here"
                             % harness)
        if not self.publish:
            raise LoginError("failed", "no challenge service configured")
        request = self.registry.open(host=host, harness=harness, account=account,
                                     home=home, version=version or "",
                                     ttl=ttl, provider_exp=provider_exp)
        env = login_env(adapter, home, environ)
        try:
            proc = self.spawn(list(adapter.argv), cwd=cwd or (home or None), env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            self.registry.finish(request, FAILED, "failed")
            raise LoginError("failed", "cannot start %s: %s"
                             % (harness, type(exc).__name__)) from exc
        request.pid = proc.pid
        with self._lock:
            self._procs[request.id] = proc
        thread = threading.Thread(target=self._read, args=(request, adapter, proc,
                                                           approver, requested_by, scope),
                                  name="l232d-login-%s" % request.id, daemon=True)
        with self._lock:
            self._threads[request.id] = thread
        thread.start()
        return request

    def wait(self, request_id: str, timeout: float = 10.0) -> Request:
        """Attend qu'une demande quitte `starting`, ou son échéance proche."""
        request = self.registry.get(request_id)
        if request is None:
            raise LoginError("unknown", "unknown login request")
        deadline = time.monotonic() + max(0.0, timeout)
        while request.state == STARTING and time.monotonic() < deadline:
            self.sleep(POLL_S)
        return request

    def finish(self, request_id: str, decision: str) -> Request:
        """`done` | `cancel` : tue le groupe, puis interroge la santé si `done`."""
        if decision not in DECISIONS:
            raise LoginError("format", "decision: done or cancel expected")
        request = self.registry.get(request_id)
        if request is None:
            raise LoginError("unknown", "unknown login request")
        if request.state in TERMINAL:
            raise LoginError(request.category or "finished", "request already closed")
        if decision == "done" and not self.health:
            raise LoginError("failed", "no health probe configured")
        # la décision est en cours : la fin du processus (SIGTERM) ne doit plus
        # clore la demande en « provider_refused »
        request.closing = True
        self._terminate(request)
        if decision == "cancel":
            return self.registry.finish(request, CANCELLED, "cancelled")
        healthy = False
        try:
            healthy = self.health(request) in (True, "healthy")
        except Exception:  # une sonde en panne ne prouve jamais la santé
            healthy = False
        return self.registry.finish(request, DONE, "healthy" if healthy else "provider_refused")

    def reap(self, now: float | None = None) -> list[Request]:
        """Clôt et tue les demandes expirées."""
        due = self.registry.reap(now)
        for request in due:
            self._terminate(request)
        return due

    def forget(self, request_id: str) -> None:
        """Oublie les références à une demande close (processus, fil)."""
        with self._lock:
            self._procs.pop(request_id, None)
            self._threads.pop(request_id, None)

    # -- interne -----------------------------------------------------------
    def _read(self, request: Request, adapter: Adapter, proc, approver: str,
              requested_by: str, scope: str) -> None:
        captured: list[bytes] = []
        size = 0
        try:
            stream = proc.stdout
            while True:
                chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
                if not chunk:
                    break
                if request.state != STARTING:
                    continue  # flux déjà publié : on vide sans rien garder
                size += len(chunk)
                if size > self.capture_max:
                    self._fail(request, adapter, "flow_unrecognized",
                               "output exceeded the private capture bound")
                    return
                captured.append(chunk)
                text = b"".join(captured).decode("utf-8", "replace")
                try:
                    flow = scan_flow(adapter, request.version, text)
                except LoginError as exc:
                    self._fail(request, adapter, exc.category, exc.message)
                    return
                if flow is None:
                    continue
                self._publish(request, adapter, flow, approver, requested_by, scope)
                if request.state != VIEWING:
                    return
        except Exception:  # la lecture ne doit jamais tuer le superviseur
            if request.state in LIVE:
                self._fail(request, adapter, "failed", "login capture failed")
            return
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
        if request.state != STARTING:
            self._settle(request, proc)
            return
        # fin de sortie sans flux reconnu
        output = b"".join(captured).decode("utf-8", "replace")
        category = "provider_refused" if _REFUSED_RE.search(output) else "flow_unrecognized"
        message = ("%s refused the login" % adapter.harness if category == "provider_refused"
                   else "%s: no recognized login flow in the output" % adapter.harness)
        self._fail(request, adapter, category, message)

    def _settle(self, request: Request, proc) -> None:
        """Le CLI s'est terminé après publication : un code non nul est un refus."""
        try:
            code = proc.wait(timeout=1.0)
        except Exception:
            return
        if code and request.state == VIEWING and not request.closing:
            self.registry.finish(request, FAILED, "provider_refused")

    def _publish(self, request: Request, adapter: Adapter, flow: Flow,
                 approver: str, requested_by: str, scope: str) -> None:
        payload = {"host": request.host, "provider": adapter.harness,
                   "account": request.account, "approver": approver,
                   "requested_by": requested_by, "url": flow.url, "code": flow.code,
                   "scope": scope}
        if request.expires:
            payload["provider_exp"] = int(request.expires)
        try:
            answer = self.publish(payload) or {}
        except Exception:
            self._fail(request, adapter, "failed", "challenge service refused the request")
            return
        del payload, flow
        login_id = str(answer.get("login_id") or "")
        link = str(answer.get("link") or "")
        if not re.fullmatch(r"lgn_[0-9a-z]{26}", login_id):
            self._fail(request, adapter, "failed", "challenge service answered an unreadable id")
            return
        self.registry.touch(request, state=VIEWING, login_id=login_id)
        if link:
            self.registry.touch(request, link=link)

    def _fail(self, request: Request, adapter: Adapter, category: str,
              message: str) -> None:
        if request.state not in LIVE or request.closing:
            return
        self.registry.finish(request, FAILED, category)
        _ = (adapter, message)  # jamais journalisé : la valeur reste privée
        self._terminate(request)

    def _terminate(self, request: Request) -> None:
        with self._lock:
            proc = self._procs.get(request.id)
        if proc is None:
            return
        pgid = None
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None
        if proc.poll() is None and pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.monotonic() + max(0.0, self.grace)
            while proc.poll() is None and time.monotonic() < deadline:
                self.sleep(POLL_S)
            if proc.poll() is None and pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass
        with self._lock:
            self._procs.pop(request.id, None)
