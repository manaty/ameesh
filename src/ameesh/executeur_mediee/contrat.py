# SPDX-License-Identifier: AGPL-3.0-only
"""Contrat de l'API d'exécuteur médiée (`/api/exec/v1`, lot L107).

Ce module est FIGÉ : le serveur (L108), le client distant (L109),
l'identité (L110) et la porte d'hôte (L112) s'appuient dessus en parallèle.
Une modification incompatible passe par une nouvelle version du contrat
(`ameesh-exec-contract/2`), jamais par une retouche silencieuse.

Version 1.1 (assemblage de la voie B) : les écarts relevés par L108 à L112
sont tranchés ici, dans `contrat.json`, dans les jeux dorés et dans
`docs/EXECUTEUR-MEDIEE.md` (« Contrat 1.1 ») ; le client et le serveur
suivent la même table. Ajouts compatibles : lignes servies aussi par
`session/op` (`Operation.session`), forçages `executeur`,
`owner_enveloppe`, `epoch_enveloppe`, empreinte d'idempotence définie pour
les nombres décimaux.

Il contient :

* le chargeur de la table des opérations (`contrat.json`, livrée dans la
  roue : l'exécuteur de la VM et le serveur lisent le même fichier) ;
* les enveloppes JSON d'une opération : requête (`ameesh-exec-op/1`),
  enveloppe de bail (`fence`), résultat (`ameesh-exec-result/1`) et erreur
  (`ameesh-exec-error/1`) ;
* l'idempotence (`Idempotency-Key`, empreinte de requête) ;
* la table des erreurs et leur correspondance côté client avec les
  exceptions de `ameesh.db` (`Unavailable`, `Forbidden`,
  `NotSupportedRemotely`) ;
* un petit vérificateur de schémas (le sous-ensemble de JSON Schema que la
  table emploie).

Référence lisible : `docs/EXECUTEUR-MEDIEE.md`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from functools import lru_cache
from importlib import resources
from typing import Any, Mapping, Optional, Sequence

from .. import jcs
from ..db import DbError, Unavailable

# --------------------------------------------------------------------------
# versions et schémas
# --------------------------------------------------------------------------

#: préfixe de toutes les routes de l'API d'exécuteur
PREFIX = "/api/exec/v1"

SCHEMA_CONTRACT = "ameesh-exec-contract/1"
SCHEMA_OP = "ameesh-exec-op/1"
SCHEMA_RESULT = "ameesh-exec-result/1"
SCHEMA_ERROR = "ameesh-exec-error/1"
SCHEMA_EVENTS = "ameesh-exec-events/1"
SCHEMA_HOST = "ameesh-exec-host/1"
SCHEMA_AVAILABILITY = "ameesh-exec-availability/1"
SCHEMA_ENROLL = "ameesh-exec-enroll/1"
SCHEMA_TOKEN = "ameesh-exec-token/1"
SCHEMA_SESSION_TOKEN = "ameesh-exec-session-token/1"
SCHEMA_HEALTH = "ameesh-exec-health/1"
SCHEMA_HOST_STATE = "ameesh-host-state/1"
SCHEMA_HOST_ACK = "ameesh-host-ack/1"

#: routes (méthode, chemin relatif au préfixe) ; `{agent}` est un nom d'agent
ROUTES = {
    "health": ("GET", "/health"),
    "enroll": ("POST", "/enroll"),
    "token": ("POST", "/token"),
    "host": ("GET", "/host"),
    "availability": ("PUT", "/host/availability"),
    "op": ("POST", "/op"),
    "session_token": ("POST", "/session-token"),
    "session_op": ("POST", "/session/op"),
    "events": ("GET", "/events"),
    "bundle_in": ("GET", "/work/{agent}/bundle"),
    "bundle_out": ("POST", "/work/{agent}/bundle"),
    "llm": ("POST", "/llm/{provider}/{path}"),
}

#: en-tête d'idempotence, obligatoire pour toute écriture
IDEMPOTENCY_HEADER = "Idempotency-Key"
#: en-tête posé par le serveur quand la réponse est rejouée
IDEMPOTENCY_REPLAYED_HEADER = "Idempotency-Replayed"
#: conservation des clés d'idempotence par le serveur
IDEMPOTENCY_RETENTION_S = 24 * 3600

#: taille maximale d'un corps de requête `op` (au-delà : 413 too_large)
MAX_BODY_BYTES = 1 << 20


# --------------------------------------------------------------------------
# erreurs
# --------------------------------------------------------------------------

class Forbidden(DbError):
    """Opération hors portée (agent ou hôte), exécuteur révoqué ou hôte
    indisponible (HTTP 403). `code` porte le code d'erreur du serveur."""

    def __init__(self, message: str, code: str = "forbidden_scope"):
        super().__init__(message)
        self.code = code


class ExecutorRevoked(Forbidden):
    """L'exécuteur a été révoqué : il doit s'arrêter (pas de nouvel essai)."""

    def __init__(self, message: str):
        super().__init__(message, "executor_revoked")


class NotSupportedRemotely(DbError):
    """Opération hors de la table : levée par le client AVANT tout appel
    réseau, ou sur un 404 `op_not_allowed` du serveur."""


@dataclasses.dataclass(frozen=True)
class ErrorSpec:
    code: str
    status: int
    #: exception levée côté client : "DbError", "Forbidden", "ExecutorRevoked",
    #: "NotSupportedRemotely", "Unavailable", ou "retry_token" (nouveau jeton
    #: puis un seul nouvel essai, sinon DbError)
    client: str
    meaning: str


#: table des codes d'erreur ; le corps est toujours `ameesh-exec-error/1`
ERRORS: dict[str, ErrorSpec] = {e.code: e for e in (
    ErrorSpec("bad_request", 400, "DbError", "corps illisible, schéma inconnu"),
    ErrorSpec("bad_args", 400, "DbError", "arguments non liables à la signature, champ interdit, fence incohérent avec les arguments"),
    ErrorSpec("fence_required", 400, "DbError", "opération de portée B sans enveloppe de bail"),
    ErrorSpec("idempotency_key_required", 400, "DbError", "écriture sans Idempotency-Key"),
    ErrorSpec("token_expired", 401, "retry_token", "jeton échu"),
    ErrorSpec("token_invalid", 401, "retry_token", "jeton inconnu ou mal formé"),
    ErrorSpec("budget_exceeded", 402, "DbError", "relais de modèle : plafond atteint (L111)"),
    ErrorSpec("forbidden_scope", 403, "Forbidden", "agent ou hôte hors de la portée de l'exécuteur"),
    ErrorSpec("host_unavailable", 403, "Forbidden", "hôte déclaré indisponible (porte d'hôte) : pas de réclamation"),
    ErrorSpec("executor_revoked", 403, "ExecutorRevoked", "exécuteur révoqué : l'exécuteur s'arrête"),
    ErrorSpec("op_not_allowed", 404, "NotSupportedRemotely", "opération hors de la table, ou mauvaise route (op / session/op)"),
    ErrorSpec("idempotency_mismatch", 409, "DbError", "même Idempotency-Key, autre requête"),
    ErrorSpec("too_large", 413, "DbError", "corps trop gros"),
    ErrorSpec("rate_limited", 429, "Unavailable", "trop de requêtes ; Retry-After donné"),
    ErrorSpec("internal", 500, "Unavailable", "erreur du serveur"),
    ErrorSpec("unavailable", 503, "Unavailable", "base ou serveur indisponible ; Retry-After facultatif"),
)}


def error_body(code: str, message: str, *, retry_after: float | None = None) -> dict:
    """Corps d'erreur ; `message` ne porte jamais de donnée d'autrui."""
    if code not in ERRORS:
        raise ValueError("code d'erreur inconnu : %s" % code)
    body = {"schema": SCHEMA_ERROR, "error": code, "message": message}
    if retry_after is not None:
        body["retry_after"] = retry_after
    return body


def client_exception(status: int, body: Mapping | None) -> DbError:
    """L'exception que le client lève pour cette réponse d'erreur.

    Toute réponse 5xx, illisible, ou sans code connu donne `Unavailable` (les
    reprises L72 de l'exécuteur s'appliquent) ; un 4xx inconnu donne
    `DbError`. Les codes `retry_token` rendent `DbError` : c'est au
    transport de renouveler le jeton AVANT d'arriver ici."""
    code = (body or {}).get("error") if isinstance(body, Mapping) else None
    message = ((body or {}).get("message") if isinstance(body, Mapping) else None) or ""
    spec = ERRORS.get(code or "")
    text = "%s (%s) : %s" % (code or "http", status, message)
    if spec is None:
        return Unavailable(text) if status >= 500 or status == 0 else DbError(text)
    if spec.client == "Forbidden":
        return Forbidden(text, spec.code)
    if spec.client == "ExecutorRevoked":
        return ExecutorRevoked(text)
    if spec.client == "NotSupportedRemotely":
        return NotSupportedRemotely(text)
    if spec.client == "Unavailable":
        return Unavailable(text)
    return DbError(text)


# --------------------------------------------------------------------------
# enveloppes
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Fence:
    """Enveloppe de bail : obligatoire pour une opération de portée B.

    Le serveur la recontrôle sous verrou de ligne dans la transaction de
    l'opération (bail détenu par `owner`/`epoch`, non échu, hôte de
    l'exécuteur, `owner` préfixé par `exec:<executor_id>:`). Un bail perdu
    n'est PAS une erreur : la réponse est 200, `fenced: true`, et `value`
    vaut la valeur de refus de l'opération (`Operation.refusal`)."""

    agent: str
    owner: str
    epoch: int

    def to_json(self) -> dict:
        return {"agent": self.agent, "owner": self.owner, "epoch": self.epoch}

    @classmethod
    def from_json(cls, data: Any) -> "Fence":
        if (not isinstance(data, Mapping) or not isinstance(data.get("agent"), str)
                or not isinstance(data.get("owner"), str)
                or not isinstance(data.get("epoch"), int)
                or isinstance(data.get("epoch"), bool)):
            raise ValueError("fence : {agent: str, owner: str, epoch: int} attendu")
        return cls(data["agent"], data["owner"], data["epoch"])


def owner_for(executor_id: str, host: str, pid: int) -> str:
    """L'owner de bail d'un exécuteur médié : `exec:<executor_id>:<hôte>:<pid>`."""
    return "exec:%s:%s:%d" % (executor_id, host, pid)


@dataclasses.dataclass(frozen=True)
class OpRequest:
    """Corps de `POST /op` et `POST /session/op` (`ameesh-exec-op/1`).

    Les paramètres positionnels-ou-nommés vont dans `args` (dans l'ordre)
    ou dans `kwargs` ; les paramètres nommés seulement (`*`) vont dans
    `kwargs`. Le serveur lie comme Python (`Contract.bind`)."""

    op: str
    args: tuple = ()
    kwargs: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    fence: Optional[Fence] = None

    def to_json(self) -> dict:
        body = {"schema": SCHEMA_OP, "op": self.op, "args": list(self.args),
                "kwargs": dict(self.kwargs)}
        if self.fence is not None:
            body["fence"] = self.fence.to_json()
        return body

    @classmethod
    def from_json(cls, data: Any) -> "OpRequest":
        if not isinstance(data, Mapping) or data.get("schema") != SCHEMA_OP:
            raise ValueError("schéma %s attendu" % SCHEMA_OP)
        if not isinstance(data.get("op"), str):
            raise ValueError("op : chaîne attendue")
        args = data.get("args", [])
        kwargs = data.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, Mapping):
            raise ValueError("args : liste ; kwargs : objet")
        fence = data.get("fence")
        return cls(data["op"], tuple(args), dict(kwargs),
                   Fence.from_json(fence) if fence is not None else None)


@dataclasses.dataclass(frozen=True)
class OpResult:
    """Corps d'une réponse 200 (`ameesh-exec-result/1`).

    `value` : la valeur de l'interface telle quelle (instants `*_ts` en
    secondes epoch flottantes, JSON en objets, séquences en listes, None en
    null). Un refus métier reste une valeur. `fenced` : vrai si le bail de
    l'enveloppe n'était plus valide (`value` = valeur de refus).
    `server_ts` : horloge du serveur (secondes epoch)."""

    value: Any
    server_ts: float
    fenced: bool = False

    def to_json(self) -> dict:
        body = {"schema": SCHEMA_RESULT, "ok": True, "value": self.value,
                "server_ts": self.server_ts}
        if self.fenced:
            body["fenced"] = True
        return body

    @classmethod
    def from_json(cls, data: Any) -> "OpResult":
        if (not isinstance(data, Mapping) or data.get("schema") != SCHEMA_RESULT
                or data.get("ok") is not True):
            raise ValueError("schéma %s attendu" % SCHEMA_RESULT)
        return cls(data.get("value"), float(data.get("server_ts") or 0.0),
                   bool(data.get("fenced", False)))


def new_idempotency_key() -> str:
    """Clé tirée UNE fois par appel logique, réutilisée à chaque nouvel essai."""
    return str(uuid.uuid4())


def request_sha256(body: Mapping) -> str:
    """Empreinte d'une requête pour l'idempotence : SHA-256 (hex) du JSON
    canonique (RFC 8785) du corps complet, `fence` compris. Même clé et même
    empreinte : réponse rejouée ; même clé, autre empreinte : 409.

    Contrat 1.1 : les nombres décimaux (un TTL de 90.5 s, un coût de
    0.031 $) sont écrits comme ECMAScript (RFC 8785 §3.2.2.3,
    `jcs.canonicalize(doubles=True)`) ; `90.0` et `90` ont la même
    empreinte. Restent refusés (`jcs.JcsError`, 400 `bad_args`) : NaN, les
    infinis et les entiers hors de ±(2^53 − 1)."""
    return hashlib.sha256(jcs.canonicalize(dict(body), doubles=True)).hexdigest()


# --------------------------------------------------------------------------
# la table des opérations
# --------------------------------------------------------------------------

#: portées : A agent admis ; B A + bail (fence) ; H hôte forcé ;
#: S jeton de session ; agregat donnée agrégée
SCOPES = ("A", "B", "H", "S", "agregat")
TRANSPORTS = ("op", "session/op", "events")


@dataclasses.dataclass(frozen=True)
class Param:
    name: str
    #: "positionnel" (positionnel ou nommé) ou "nomme" (nommé seulement)
    kind: str
    schema: dict
    required: bool
    default: Any = None


@dataclasses.dataclass(frozen=True)
class Operation:
    """Une ligne de la table (voir `contrat.json`)."""

    name: str
    write: bool
    transport: str
    scope: tuple
    fence: bool
    #: paramètre qui porte le nom d'agent contrôlé (portées A et B), ou None
    agent_param: Optional[str]
    #: paramètres remplacés par le serveur : {param: "hote_executeur" |
    #: "agent_session" | valeur littérale}
    forced: Mapping[str, Any]
    params: tuple
    result: dict
    #: valeur rendue avec `fenced: true` (portée B seulement)
    refusal: Any
    #: rejouer l'appel sans clé ne change rien (information ; la clé reste
    #: obligatoire pour toute écriture)
    naturally_idempotent: bool
    rules: tuple
    #: contrat 1.1 : ligne de la route `op` servie AUSSI par `session/op`
    #: (hook `agent-mail`, `mail inbox`, index des fils depuis la VM)
    session: bool = False

    @property
    def routes(self) -> tuple:
        """Les routes qui servent la ligne."""
        if self.session and self.transport == "op":
            return ("op", "session/op")
        return (self.transport,)

    @property
    def domain(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def method(self) -> str:
        return self.name.split(".", 1)[1]

    @property
    def requires_idempotency_key(self) -> bool:
        return self.write


@dataclasses.dataclass(frozen=True)
class Contract:
    version: str
    prefix: str
    operations: Mapping[str, Operation]
    #: {domaine: [méthodes]} : opérations de l'interface refusées à dessein
    refused: Mapping[str, tuple]
    refused_reasons: Mapping[str, str]
    interface_total: int

    def get(self, name: str) -> Operation:
        """L'opération admise, sinon `NotSupportedRemotely`."""
        op = self.operations.get(name)
        if op is None:
            raise NotSupportedRemotely("opération non admise à distance : %s" % name)
        return op

    def bind(self, name: str, args: Sequence = (), kwargs: Mapping | None = None) -> dict:
        """Lie `args`/`kwargs` aux paramètres de l'opération comme Python ;
        rend `{param: valeur}` avec les défauts. `ValueError` (→ 400
        bad_args) si la liaison échoue."""
        op = self.get(name)
        kwargs = dict(kwargs or {})
        positional = [p for p in op.params if p.kind == "positionnel"]
        if len(args) > len(positional):
            raise ValueError("%s : trop d'arguments positionnels" % name)
        bound: dict = {}
        for p, v in zip(positional, args):
            bound[p.name] = v
        known = {p.name for p in op.params}
        for k, v in kwargs.items():
            if k not in known:
                raise ValueError("%s : paramètre inconnu %s" % (name, k))
            if k in bound:
                raise ValueError("%s : %s donné deux fois" % (name, k))
            bound[k] = v
        for p in op.params:
            if p.name not in bound:
                if p.required:
                    raise ValueError("%s : %s manquant" % (name, p.name))
                bound[p.name] = p.default
            elif not conforms(bound[p.name], p.schema):
                raise ValueError("%s : %s hors schéma" % (name, p.name))
        return bound

    def validate_request(self, req: OpRequest, *, route: str,
                         idempotency_key: str | None = None) -> dict:
        """Contrôles de forme du serveur, communs à L108 et aux essais de
        L109 ; rend les arguments liés. Lève `NotSupportedRemotely`
        (op_not_allowed) ou `ValueError` avec pour message le code d'erreur
        (`bad_args`, `fence_required`, `idempotency_key_required`).
        Les contrôles de PORTÉE ne sont pas ici : ils sont à L108."""
        op = self.get(req.op)
        if route not in op.routes:
            raise NotSupportedRemotely("%s n'est pas servie par %s" % (req.op, route))
        try:
            bound = self.bind(req.op, req.args, req.kwargs)
        except ValueError as exc:
            raise ValueError("bad_args") from exc
        if op.fence and req.fence is None:
            raise ValueError("fence_required")
        if not op.fence and req.fence is not None and route == "session/op":
            raise ValueError("bad_args")
        if op.fence and req.fence is not None:
            if op.agent_param and agent_name(bound.get(op.agent_param)) not in (
                    None, req.fence.agent):
                raise ValueError("bad_args")
            for k in ("owner", "epoch"):
                if k in bound and bound[k] is not None and bound[k] != getattr(req.fence, k):
                    raise ValueError("bad_args")
        if op.write and not idempotency_key:
            raise ValueError("idempotency_key_required")
        return bound


def agent_name(value: Any) -> Any:
    """Le nom d'agent d'une valeur de `param_agent` : `agent:<nom>` (membre
    d'un fil, `fil.member`) donne `<nom>` ; toute autre valeur est rendue
    telle quelle."""
    if isinstance(value, str) and value.startswith("agent:"):
        return value[len("agent:"):]
    return value


def _param(d: Mapping) -> Param:
    return Param(d["nom"], d["sorte"], d["schema"], d["requis"], d.get("defaut"))


def _operation(d: Mapping) -> Operation:
    return Operation(
        name=d["nom"], write=d["ecriture"], transport=d["transport"],
        scope=tuple(d["portee"]), fence=d["fence"], agent_param=d["param_agent"],
        forced=dict(d["forces"]), params=tuple(_param(p) for p in d["parametres"]),
        result=d["resultat"], refusal=d["refus"],
        naturally_idempotent=d["idempotence"]["naturelle"], rules=tuple(d["regles"]),
        session=bool(d.get("session", False)))


def parse(data: Mapping) -> Contract:
    if data.get("schema") != SCHEMA_CONTRACT:
        raise ValueError("schéma %s attendu" % SCHEMA_CONTRACT)
    ops = {}
    for d in data["operations"]:
        op = _operation(d)
        if op.name in ops:
            raise ValueError("opération en double : %s" % op.name)
        if op.transport not in TRANSPORTS or not set(op.scope) <= set(SCOPES):
            raise ValueError("ligne invalide : %s" % op.name)
        if op.session and op.transport != "op":
            raise ValueError("ligne invalide (session hors route op) : %s" % op.name)
        ops[op.name] = op
    ref = data["refusees"]
    return Contract(version=data["version"], prefix=data["prefixe"], operations=ops,
                    refused={k: tuple(v) for k, v in ref["operations"].items()},
                    refused_reasons=dict(ref["raisons"]),
                    interface_total=ref["total_interface"])


@lru_cache(maxsize=1)
def load() -> Contract:
    """La table livrée avec ameesh (`ameesh/executeur_mediee/contrat.json`)."""
    text = resources.files(__package__).joinpath("contrat.json").read_text("utf-8")
    return parse(json.loads(text))


# --------------------------------------------------------------------------
# vérificateur de schémas (sous-ensemble de JSON Schema employé par la table)
# --------------------------------------------------------------------------

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, Mapping),
    "array": lambda v: isinstance(v, (list, tuple)),
    "null": lambda v: v is None,
}


def conforms(value: Any, schema: Mapping) -> bool:
    """`value` respecte-t-il `schema` ? Mots-clés : type (chaîne ou liste),
    items, additionalProperties, anyOf, $ref (`ameesh-exec-events/1` :
    accepté tel quel, la valeur n'est jamais transportée par /op)."""
    if "$ref" in schema:
        return True
    if "anyOf" in schema:
        return any(conforms(value, s) for s in schema["anyOf"])
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else list(types)
        if not any(_TYPES[t](value) for t in types):
            return False
    if isinstance(value, (list, tuple)) and "items" in schema:
        return all(conforms(v, schema["items"]) for v in value)
    if isinstance(value, Mapping) and isinstance(schema.get("additionalProperties"), Mapping):
        return all(conforms(v, schema["additionalProperties"]) for v in value.values())
    return True
