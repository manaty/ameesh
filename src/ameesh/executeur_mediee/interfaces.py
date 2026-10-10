# SPDX-License-Identifier: AGPL-3.0-only
"""Interfaces Python figées entre les lots de l'exécuteur médié (L107).

Qui implémente quoi :

* `ExecTransport` (client, L109) : le transport HTTP de `RemoteStorage` et
  de `RemoteSubscription`. Il s'appuie sur un `TokenSource` (L110).
* `TokenSource` (client, L110) : le jeton d'accès de l'exécuteur, tiré d'une
  assertion ES256 signée par la clé de la VM.
* `ScopeRules` et `ExecDispatcher` (serveur, L108) : la portée de chaque
  opération de la table et le répartiteur de `/op` et `/session/op`.
* `ExecutorAuth` / `IdentityProvider` (serveur, L110) : vérification des
  jetons (`verify(token) -> Principal`), enrôlement, émission, révocation.
  L108 et L111 n'en voient que `verify`.
* `HostGate` (exécuteur, L112) : dans `porte.py`.

Formats des jetons (L110 les émet, L108/L111 les vérifient par `verify`) :

* jeton d'accès : opaque, `amx1.` + 43 caractères base64url (32 octets
  aléatoires), 10 min ; seul son SHA-256 est gardé en base ;
* jeton de session : opaque, `ams1.` + 43 caractères base64url, lié à
  (exécuteur, agent, epoch), meurt avec le bail ou au changement d'epoch ;
* assertion de l'exécuteur (`POST /token`) : JWS compact ES256, en-tête
  `{"alg": "ES256", "typ": "JWT", "kid": <empreinte>}`, charge
  `{iss: executor_id, aud: URL du serveur, iat, exp (≤ iat + 60), jti}` ;
  `jti` à usage unique.
"""
from __future__ import annotations

import abc
import dataclasses
from typing import Any, Iterator, Mapping, Optional, Sequence

from .contrat import Contract, Fence, OpRequest, OpResult, Operation
from .evenements import Event
from .porte import GateState

ACCESS_TOKEN_PREFIX = "amx1."
SESSION_TOKEN_PREFIX = "ams1."
ACCESS_TOKEN_TTL_S = 600
ASSERTION_MAX_TTL_S = 60
#: cache de l'état de l'exécuteur (révocation) côté serveur
EXECUTOR_STATE_CACHE_S = 5
#: variables d'environnement passées au harnais dans la VM
ENV_SESSION_TOKEN = "AMEESH_EXEC_TOKEN"
ENV_SERVER_URL = "AMEESH_EXEC_URL"


# --------------------------------------------------------------------------
# identité (L110 ; vue par L108, L109, L111)
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Principal:
    """Qui parle, tel que le serveur l'a vérifié.

    `kind` : "executor" (jeton d'accès) ou "session" (jeton de session :
    `agent` et `epoch` sont alors renseignés et font foi)."""

    kind: str
    executor_id: str
    mesh: str
    host: str
    agent: Optional[str] = None
    epoch: Optional[int] = None
    #: liste blanche d'agents fixée à l'invitation (None : pas de restriction)
    agents_allowlist: Optional[tuple] = None
    expires_ts: float = 0.0


class AuthError(Exception):
    """Refus d'authentification ; `code` ∈ token_expired, token_invalid,
    executor_revoked (voir `contrat.ERRORS`)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class IssuedToken:
    """Corps de `POST /token` et `POST /session-token`
    (`ameesh-exec-token/1`, `ameesh-exec-session-token/1`)."""

    token: str
    expires_ts: float
    kind: str  # "executor" | "session"

    def to_json(self) -> dict:
        from .contrat import SCHEMA_SESSION_TOKEN, SCHEMA_TOKEN
        return {"schema": SCHEMA_SESSION_TOKEN if self.kind == "session" else SCHEMA_TOKEN,
                "access_token": self.token, "token_type": "Bearer",
                "expires_ts": self.expires_ts}


class ExecutorAuth(abc.ABC):
    """Vérification des jetons, seule vue de l'identité pour L108 et L111.
    L108 démarre avec un bouchon ; L110 fournit l'implémentation réelle."""

    @abc.abstractmethod
    def verify(self, token: str) -> Principal:
        """Le principal du jeton (accès ou session). Relit l'état de
        l'exécuteur (cache ≤ 5 s) ; lève `AuthError` (token_expired,
        token_invalid, executor_revoked)."""


class IdentityProvider(ExecutorAuth):
    """Serveur, L110 : enrôlement et émission des jetons."""

    @abc.abstractmethod
    def enroll(self, request: Mapping[str, Any], *, server_url: str) -> dict:
        """`POST /enroll` (`ameesh-exec-enroll/1`) : vérifie le code
        (non consommé, non échu), la preuve ES256 sur
        JCS({code, public_key, server_url}), consomme le code et crée
        l'exécuteur. Rend `{"executor_id", "mesh", "host", "server_time"}`.
        Lève `AuthError("token_invalid")` pour un code ou une preuve faux."""

    @abc.abstractmethod
    def issue_access_token(self, assertion: str, *, server_url: str) -> IssuedToken:
        """`POST /token` : vérifie l'assertion ES256 (iss, aud, exp ≤ 60 s,
        jti neuf) et émet un jeton d'accès de 10 min."""

    @abc.abstractmethod
    def issue_session_token(self, principal: Principal, fence: Fence) -> IssuedToken:
        """`POST /session-token` : `fence` doit être un bail vivant de
        l'exécuteur `principal` (contrôlé par l'appelant, L108, dans sa
        transaction) ; le jeton porte (exécuteur, agent, epoch). Il vit
        tant que ce bail vit (renouvellements compris), au plus 12 h ;
        `expires_ts` annonce l'échéance du bail à l'émission. Lève
        `AuthError` (`token_invalid`, `executor_revoked`) ou
        `ScopeError("forbidden_scope")` (owner d'un autre exécuteur, agent
        hors de la liste de l'invitation, bail perdu)."""

    @abc.abstractmethod
    def revoke(self, executor_id: str, *, by: str, why: str) -> int:
        """Révoque : jetons invalidés, baux de l'exécuteur relâchés, dans
        une transaction. Rend le nombre de baux relâchés."""


class TokenSource(abc.ABC):
    """Client, L110 (utilisé par le transport de L109)."""

    @abc.abstractmethod
    def access_token(self, *, refresh: bool = False) -> str:
        """Un jeton d'accès valide (en cache tant qu'il vit plus de 60 s) ;
        `refresh=True` en force un nouveau (après un 401). Lève
        `ameesh.db.Unavailable` si le serveur est injoignable,
        `contrat.ExecutorRevoked` si l'exécuteur est révoqué."""


# --------------------------------------------------------------------------
# client (L109)
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class HostInfo:
    """Corps de `GET /host` (`ameesh-exec-host/1`)."""

    host: str
    mesh: str
    executor_id: str
    #: limites physiques de l'hôte (contrat 1.1) : `{"max_agents": int |
    #: null, "resources": {"min_mem_available": octets, "max_swap_used":
    #: octets, "max_load": nombre, "min_disk_free": octets}}` — `max_agents`
    #: et `limits` de `resources.host_limits`, les seuils absents prenant
    #: leur défaut prudent côté exécuteur (`resources.thresholds`)
    limits: Mapping[str, Any]
    lease_ttl_s: float
    lease_renew_s: float
    harnesses: Sequence[str]
    models: Sequence[str]
    agents: Sequence[str]
    available: bool
    contract_version: str

    def to_json(self) -> dict:
        from .contrat import SCHEMA_HOST
        d = dataclasses.asdict(self)
        d.update({k: list(v) for k, v in d.items() if isinstance(v, tuple)})
        return {"schema": SCHEMA_HOST, **d}

    @classmethod
    def from_json(cls, d: Mapping) -> "HostInfo":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


class ExecTransport(abc.ABC):
    """Le transport de l'exécuteur médié (L109). Toutes les méthodes lèvent
    les exceptions de `contrat.client_exception` (`Unavailable`,
    `Forbidden`, `NotSupportedRemotely`, `DbError`) ; un 401 déclenche UN
    renouvellement de jeton (`TokenSource.access_token(refresh=True)`) et
    un seul nouvel essai."""

    @abc.abstractmethod
    def call(self, request: OpRequest, *, idempotency_key: Optional[str]) -> OpResult:
        """`POST /op`. `idempotency_key` obligatoire pour une écriture,
        tirée une fois par appel logique (`contrat.new_idempotency_key`) et
        réutilisée telle quelle à chaque nouvel essai."""

    @abc.abstractmethod
    def session_call(self, request: OpRequest, *, idempotency_key: Optional[str]) -> OpResult:
        """`POST /session/op` avec le jeton de session (`AMEESH_EXEC_TOKEN`)."""

    @abc.abstractmethod
    def poll_events(self, after: Optional[str], wait: float) -> tuple[list[Event], str]:
        """Attente longue : `GET /events?wait=&after=` ; rend
        (événements, curseur suivant)."""

    @abc.abstractmethod
    def stream_events(self, after: Optional[str]) -> Iterator[Event]:
        """SSE : itère les événements jusqu'à coupure (`Unavailable`) ;
        `Last-Event-ID` = `after`."""

    @abc.abstractmethod
    def host_info(self) -> HostInfo:
        """`GET /host`."""

    @abc.abstractmethod
    def put_availability(self, state: GateState) -> None:
        """`PUT /host/availability` avec `porte.availability_body(state)`."""

    @abc.abstractmethod
    def session_token(self, fence: Fence) -> IssuedToken:
        """`POST /session-token` après `begin_turn`."""


# --------------------------------------------------------------------------
# serveur (L108)
# --------------------------------------------------------------------------

class ScopeError(Exception):
    """Refus de portée ; `code` : forbidden_scope, host_unavailable,
    bad_args (champ interdit, ex. `agents.upsert` hors `cwd`)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class ScopeRules(abc.ABC):
    """Portée par opération (L108) : une règle par ligne de la table."""

    @abc.abstractmethod
    def admitted_agents(self, principal: Principal) -> frozenset:
        """Agents admis sur l'hôte de l'exécuteur : placés par le canon sur
        cet hôte ∩ liste blanche de l'invitation ∩ mode d'identifiants
        compatible avec le relais."""

    @abc.abstractmethod
    def apply(self, principal: Principal, op: Operation, bound: dict,
              fence: Optional[Fence]) -> dict:
        """Contrôle la portée AVANT l'appel et rend les arguments à passer
        au pilote, valeurs forcées appliquées (`op.forced`). Lève
        `ScopeError`. Le fencing (portée B) n'est PAS ici : il est fait par
        le répartiteur dans la transaction de l'opération."""

    @abc.abstractmethod
    def filter_result(self, principal: Principal, op: Operation, value: Any) -> Any:
        """Restreint le rendu (portées H, S, agrégat : `agents.harnesses`,
        `agents.overview`, `mailbox.get`, `operations.message_lots`…)."""


class ExecDispatcher(abc.ABC):
    """Répartiteur de `/op` et `/session/op` (L108).

    Pour une requête : `Contract.validate_request` → `ScopeRules.apply` →
    transaction { idempotence (rejouer ou 409) ; si portée B :
    `SELECT … FROM agent_registry WHERE name = fence.agent FOR UPDATE`,
    recontrôle du bail (owner, epoch, échéance `clock_timestamp()`, hôte,
    préfixe `exec:<executor_id>:`), sinon valeur de refus et
    `fenced: true` ; appel du pilote Postgres dans la même transaction ;
    écriture de la réponse d'idempotence } → `ScopeRules.filter_result`."""

    contract: Contract

    @abc.abstractmethod
    def dispatch(self, principal: Principal, request: OpRequest, *, route: str,
                 idempotency_key: Optional[str]) -> tuple[int, dict, dict]:
        """Rend (statut HTTP, corps JSON, en-têtes) : `OpResult.to_json()`
        ou `contrat.error_body(...)`."""
