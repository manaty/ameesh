# SPDX-License-Identifier: AGPL-3.0-only
"""Stockage distant de l'exécuteur médié (lot L109).

`RemoteStorage` implémente `storage.interface.Storage` par appels à
`/api/exec/v1`, à travers un `ExecTransport`. Seules les 61 opérations de la
table du contrat (L107) passent ; toute autre lève `NotSupportedRemotely`
AVANT tout appel réseau.

* **Route** : `op` (jeton d'exécuteur), `session/op` (jeton de session) ou
  `events` (`wakeups.subscribe` → `RemoteSubscription`). Une connexion en
  mode session (harnais dans la VM) n'a que le jeton de session : une
  opération de la route `op` y lève `NotSupportedRemotely`, sans appel.
* **Enveloppe de bail** (portée B) : tirée des arguments (`owner`, `epoch`)
  quand l'opération les porte, sinon du **carnet des baux** du client, tenu
  à jour par `leases.claim` / `renew` / `release` et par chaque réponse
  `fenced`. Sans bail connu pour l'agent, l'opération rend sa valeur de
  refus sans appel : c'est ce que le serveur rendrait (`fenced: true`).
* **Idempotence** : une clé par appel logique d'écriture, réutilisée par le
  transport à chaque nouvel essai.
* **Valeurs** : celles de l'interface, telles que le serveur les rend.
"""
from __future__ import annotations

import functools
import threading
import typing
from typing import Any, Callable, Mapping, Optional

from ... import db as db_mod
from ...executeur_mediee import contrat as C
from ...executeur_mediee.interfaces import ExecTransport, HostInfo, IssuedToken
from .. import interface as SI

#: nom du pilote (`Storage.driver`, `RemoteDb.driver`)
DRIVER = "mediated"
EXECUTOR = "executor"
SESSION = "session"


def _jsonable(value: Any) -> Any:
    """Les valeurs Python des appels, en JSON (tuples et ensembles en listes)."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    return value


# --------------------------------------------------------------------------
# carnet des baux
# --------------------------------------------------------------------------

class LeaseBook:
    """Les baux que CET exécuteur détient : agent → (owner, epoch), et les
    tours ouverts (turn_id → agent) pour `turn_resources.close_turn`.

    Le carnet ne fait jamais foi : le serveur recontrôle chaque enveloppe.
    Il sert à poser l'enveloppe des écritures qui ne portent pas le bail
    (`agents.set_status`, `pending_spend.*`, `turn_costs.insert`…)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, tuple[str, int]] = {}
        self._turns: dict[str, str] = {}

    def hold(self, agent: str, owner: str, epoch: int) -> None:
        with self._lock:
            self._leases[agent] = (owner, int(epoch))

    def forget(self, agent: str, epoch: Optional[int] = None) -> None:
        """Oublie le bail (à cet epoch seulement, si donné)."""
        with self._lock:
            held = self._leases.get(agent)
            if held is not None and (epoch is None or held[1] == int(epoch)):
                del self._leases[agent]
                for turn, who in list(self._turns.items()):
                    if who == agent:
                        del self._turns[turn]

    def fence(self, agent: Optional[str]) -> Optional[C.Fence]:
        with self._lock:
            held = self._leases.get(agent or "")
        return C.Fence(agent, held[0], held[1]) if held and agent else None

    def held(self) -> dict:
        with self._lock:
            return dict(self._leases)

    def open_turn(self, turn_id: str, agent: str) -> None:
        with self._lock:
            self._turns[str(turn_id)] = agent

    def turn_agent(self, turn_id: str) -> Optional[str]:
        with self._lock:
            return self._turns.get(str(turn_id))

    def close_turn(self, turn_id: str) -> None:
        with self._lock:
            self._turns.pop(str(turn_id), None)


# --------------------------------------------------------------------------
# la connexion distante
# --------------------------------------------------------------------------

class RemoteDb:
    """La « connexion » d'un exécuteur médié, à la place de `ameesh.db.connect`.

    Pas de SQL : `query`, `execute`, `script` et `transaction` lèvent
    `NotSupportedRemotely`. `storage.of(db)` rend `self.storage`.

    `mode` : `executor` (jeton d'exécuteur ; l'exécuteur de la VM) ou
    `session` (jeton de session lié au bail ; le harnais et ses commandes)."""

    driver = DRIVER
    #: le schéma est celui du serveur : rien à vérifier ici (`require_schema`)
    _schema_ok = True

    def __init__(self, cfg: Any, transport: ExecTransport, *, mode: str = EXECUTOR,
                 contract: Optional[C.Contract] = None, url: str = ""):
        if mode not in (EXECUTOR, SESSION):
            raise ValueError("mode inconnu : %r" % mode)
        self.cfg = cfg
        self.transport = transport
        self.mode = mode
        self.contract = contract or C.load()
        self.url = url or getattr(transport, "url", "") or ""
        self.book = LeaseBook()
        #: curseur du flux d'événements, gardé d'un abonnement à l'autre
        self.events_cursor: Optional[str] = None
        #: le flux SSE n'est pas servi : attente longue directement
        self.events_long_poll = False
        self._closed = False
        self.storage = RemoteStorage(self)

    @property
    def name(self) -> str:
        return "%s (%s)" % (DRIVER, self.url or "?")

    # -- ce que l'exécuteur appelle sur la connexion ----------------------
    def ping(self) -> None:
        """Le serveur répond-il (et ce jeton vaut-il encore) ? `GET /host`
        en mode exécuteur ; en mode session, `leases.state` n'est pas une
        sonde neutre : rien n'est envoyé."""
        if self.mode == EXECUTOR:
            self.transport.host_info()

    def host_info(self) -> HostInfo:
        return self.transport.host_info()

    def session_token(self, fence: C.Fence) -> IssuedToken:
        return self.transport.session_token(fence)

    def close(self) -> None:
        self._closed = True

    # -- pas de SQL à distance --------------------------------------------
    def _no_sql(self, *_a, **_k):
        raise C.NotSupportedRemotely("pas de SQL sur une connexion d'exécuteur médié")

    query = execute = script = query_batch = transaction = listen = _no_sql


# --------------------------------------------------------------------------
# le stockage
# --------------------------------------------------------------------------

def _domains() -> dict:
    hints = typing.get_type_hints(SI.Storage)
    return {d: cls for d, cls in hints.items()
            if isinstance(cls, type) and issubclass(cls, SI.Domain)}


def _abstract_methods(cls: type) -> list:
    return sorted(n for n in dir(cls)
                  if not n.startswith("_")
                  and getattr(getattr(cls, n, None), "__isabstractmethod__", False))


def _method(op_name: str, original: Callable) -> Callable:
    @functools.wraps(original)
    def method(self, *args, **kwargs):
        return self._remote.invoke(op_name, args, kwargs)
    method.__isabstractmethod__ = False
    return method


@functools.lru_cache(maxsize=None)
def _remote_class(domain: str, cls: type) -> type:
    """Sous-classe du domaine de l'interface dont chaque opération passe par
    `RemoteStorage.invoke` (qui lève `NotSupportedRemotely` hors table)."""
    methods = {n: _method("%s.%s" % (domain, n), getattr(cls, n))
               for n in _abstract_methods(cls)}

    def __init__(self, db, remote):
        cls.__init__(self, db)
        self._remote = remote

    methods["__init__"] = __init__
    methods["__module__"] = __name__
    return type("Remote" + cls.__name__, (cls,), methods)


class RemoteStorage(SI.Storage):
    """`storage.interface.Storage` par l'API d'exécuteur médiée."""

    driver = DRIVER

    def __init__(self, db: RemoteDb):
        self.db = db
        for domain, cls in _domains().items():
            setattr(self, domain, _remote_class(domain, cls)(db, self))

    # -- appel d'une opération --------------------------------------------
    def invoke(self, name: str, args: tuple, kwargs: dict) -> Any:
        db = self.db
        op = db.contract.get(name)  # NotSupportedRemotely hors table, sans réseau
        if op.transport == "events":
            from .events import RemoteSubscription
            return RemoteSubscription(db, list(args[0] if args else kwargs.get("channels") or []))
        if op.transport == "op" and db.mode != EXECUTOR and not op.session:
            raise C.NotSupportedRemotely(
                "%s exige le jeton d'exécuteur (route op) : non admise depuis une session"
                % name)
        if op.transport == "session/op" and db.mode != SESSION:
            raise C.NotSupportedRemotely(
                "%s exige le jeton de session (route session/op)" % name)
        try:
            bound = db.contract.bind(name, args, kwargs)
        except ValueError as exc:
            raise db_mod.DbError("appel invalide de %s : %s" % (name, exc))
        fence = None
        if op.fence:
            fence = self._fence(op, bound)
            if fence is None:
                # aucun bail détenu pour cet agent : le serveur rendrait le refus
                return op.refusal
        request = C.OpRequest(name, tuple(_jsonable(list(args))), _jsonable(dict(kwargs)),
                              fence)
        key = C.new_idempotency_key() if op.write else None
        if op.transport == "session/op" or db.mode == SESSION:
            # contrat 1.1 : une ligne `session: true` passe par `session/op`
            # avec le jeton de session
            result = db.transport.session_call(request, idempotency_key=key)
        else:
            result = db.transport.call(request, idempotency_key=key)
        self._after(op, bound, fence, result)
        return result.value

    def _fence(self, op: C.Operation, bound: dict) -> Optional[C.Fence]:
        book = self.db.book
        agent = bound.get(op.agent_param) if op.agent_param else None
        if agent is None and op.name == "turn_resources.close_turn":
            agent = book.turn_agent(bound.get("turn_id"))
        if not agent:
            return None
        # `threads.index` : l'auteur est un membre du fil (`fil.member`)
        agent = str(C.agent_name(str(agent)))
        owner, epoch = bound.get("owner"), bound.get("epoch")
        if owner is not None and epoch is not None:
            return C.Fence(agent, str(owner), int(epoch))
        return book.fence(agent)

    def _after(self, op: C.Operation, bound: dict, fence: Optional[C.Fence],
               result: C.OpResult) -> None:
        """Tient le carnet des baux à jour d'après la réponse."""
        book = self.db.book
        value = result.value
        if result.fenced:
            if fence is not None:
                book.forget(fence.agent, fence.epoch)
            return
        name = op.name
        if name == "leases.claim":
            if isinstance(value, Mapping) and value.get("lease_epoch") is not None:
                book.hold(str(value.get("name") or bound["name"]),
                          str(value.get("lease_owner") or bound["owner"]),
                          int(value["lease_epoch"]))
        elif name == "leases.renew" and value is None and fence is not None:
            book.forget(fence.agent, fence.epoch)
        elif name == "leases.release" and fence is not None:
            book.forget(fence.agent, fence.epoch)
        elif name == "turn_resources.open_turn" and fence is not None:
            book.open_turn(bound["turn_id"], fence.agent)
        elif name == "turn_resources.close_turn":
            book.close_turn(bound["turn_id"])


def supported(name: str) -> bool:
    """L'opération `domaine.méthode` passe-t-elle à distance ?"""
    return name in C.load().operations

