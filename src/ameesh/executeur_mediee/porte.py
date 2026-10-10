# SPDX-License-Identifier: AGPL-3.0-only
"""Porte d'hôte (`HostGate`) : l'état d'inactivité venu du runner Compute.

Contrat figé par L107 ; L112 livre `FileGate` et `SocketGate`.

Le runner Compute (hors ameesh) écrit l'état de l'appareil ; l'exécuteur le
lit et acquitte ce qu'il a fait :

* **fichier** : état dans `/run/ameesh-gate/state.json`
  (`ameesh-host-state/1`), écrit par fichier temporaire puis `rename` ;
  l'exécuteur le relit sur inotify, sinon toutes les 2 s, et écrit son
  acquittement dans `/run/ameesh-gate/ack.json` (`ameesh-host-ack/1`) de la
  même façon ;
* **socket** : `/run/ameesh-gate/gate.sock`, JSON Lines, mêmes messages ; le
  runner pousse un état par ligne, l'exécuteur répond par un acquittement
  par ligne.

États :

* `available` : l'exécuteur réclame, dans la limite
  `min(caps.max_concurrent, max_agents de l'hôte)` ;
* `draining` : aucune nouvelle réclamation ; chaque tour finit au point sûr ;
  au-delà de `drain_deadline_ts` (défaut : maintenant + 90 s), préemption du
  tour (consigne remise en attente) ; puis `leases.release` de chaque bail,
  effacement des dossiers, acquittement `drained: true` ;
* `stopped` : arrêt immédiat, sans écriture (le bail échoira).

Un état illisible, absent ou d'un schéma inconnu vaut `draining` (prudence :
on finit le travail en cours, on n'en prend pas d'autre).
"""
from __future__ import annotations

import abc
import dataclasses
import threading
import time
from typing import Any, Mapping, Optional, Sequence

from .contrat import SCHEMA_HOST_ACK, SCHEMA_HOST_STATE

STATES = ("available", "draining", "stopped")
#: raisons connues (texte libre admis : affiché tel quel)
REASONS = ("idle", "user_active", "battery", "window_end", "revoked", "unknown")

GATE_DIR = "/run/ameesh-gate"
STATE_FILE = GATE_DIR + "/state.json"
ACK_FILE = GATE_DIR + "/ack.json"
SOCKET_PATH = GATE_DIR + "/gate.sock"
#: relecture du fichier sans inotify (secondes)
FILE_POLL_S = 2.0
#: délai de drainage par défaut si `drain_deadline_ts` est absent
DEFAULT_DRAIN_S = 90.0


@dataclasses.dataclass(frozen=True)
class GateState:
    """`ameesh-host-state/1`. `seq` croît à chaque écriture du runner.

    `caps` (profil Compute §4.10) : `max_concurrent` (int), `cpu_share`
    (0–1), `memory_mb` (int) ; clés absentes : pas de limite de la porte."""

    state: str
    seq: int
    until_ts: Optional[float] = None
    drain_deadline_ts: Optional[float] = None
    caps: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    reason: str = "unknown"

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError("état de porte inconnu : %r" % self.state)

    @property
    def may_claim(self) -> bool:
        return self.state == "available"

    def to_json(self) -> dict:
        return {"schema": SCHEMA_HOST_STATE, "state": self.state, "seq": self.seq,
                "until_ts": self.until_ts, "drain_deadline_ts": self.drain_deadline_ts,
                "caps": dict(self.caps), "reason": self.reason}

    @classmethod
    def from_json(cls, d: Any) -> "GateState":
        """Lit un état ; `ValueError` si le schéma ou les champs sont faux
        (l'appelant applique alors `unreadable()`)."""
        if not isinstance(d, Mapping) or d.get("schema") != SCHEMA_HOST_STATE:
            raise ValueError("schéma %s attendu" % SCHEMA_HOST_STATE)
        seq = d.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise ValueError("seq : entier attendu")
        caps = d.get("caps") or {}
        if not isinstance(caps, Mapping):
            raise ValueError("caps : objet attendu")
        return cls(state=d.get("state"), seq=seq, until_ts=d.get("until_ts"),
                   drain_deadline_ts=d.get("drain_deadline_ts"), caps=dict(caps),
                   reason=str(d.get("reason") or "unknown"))

    @classmethod
    def unreadable(cls, seq: int = -1) -> "GateState":
        """L'état retenu quand la porte est illisible : drainage."""
        return cls(state="draining", seq=seq, reason="unknown")


@dataclasses.dataclass(frozen=True)
class GateAck:
    """`ameesh-host-ack/1` : ce que l'exécuteur a fait de l'état `seq`.

    `in_turn` : agents encore en tour ; `held` : baux encore détenus ;
    `drained` : vrai quand plus aucun bail n'est détenu après un
    `draining` (le runner Compute peut alors arrêter la VM)."""

    seq: int
    state: str
    in_turn: Sequence[str] = ()
    held: Sequence[str] = ()
    drained: bool = False
    ts: float = 0.0

    def to_json(self) -> dict:
        return {"schema": SCHEMA_HOST_ACK, "seq": self.seq, "state": self.state,
                "in_turn": list(self.in_turn), "held": list(self.held),
                "drained": self.drained, "ts": self.ts}

    @classmethod
    def from_json(cls, d: Any) -> "GateAck":
        if not isinstance(d, Mapping) or d.get("schema") != SCHEMA_HOST_ACK:
            raise ValueError("schéma %s attendu" % SCHEMA_HOST_ACK)
        return cls(seq=int(d["seq"]), state=str(d["state"]),
                   in_turn=tuple(d.get("in_turn") or ()), held=tuple(d.get("held") or ()),
                   drained=bool(d.get("drained")), ts=float(d.get("ts") or 0.0))


def availability_body(state: GateState) -> dict:
    """Corps de `PUT /api/exec/v1/host/availability`
    (`ameesh-exec-availability/1`) : l'exécuteur relaie l'état de sa porte
    au serveur à chaque changement (et au démarrage)."""
    from .contrat import SCHEMA_AVAILABILITY
    return {"schema": SCHEMA_AVAILABILITY, "available": state.state == "available",
            "state": state.state, "seq": state.seq, "until_ts": state.until_ts,
            "caps": dict(state.caps), "reason": state.reason}


class HostGate(abc.ABC):
    """La porte d'hôte, vue de l'exécuteur. Implémentations : `AlwaysAvailable`
    (défaut, comportement sans Compute), `FileGate` et `SocketGate` (L112).

    Fils : `state` et `acknowledge` sont appelés par la boucle de
    l'exécuteur ; `wait_change` par un fil de surveillance ; `close` peut
    venir d'un autre fil et débloque `wait_change`."""

    @abc.abstractmethod
    def state(self) -> GateState:
        """L'état courant, sans attente (dernier état lu ; `unreadable()`
        si aucun n'a pu l'être)."""

    @abc.abstractmethod
    def wait_change(self, timeout: float) -> GateState:
        """Attend au plus `timeout` secondes un état de `seq` différent du
        dernier rendu ; rend l'état courant (inchangé si le délai expire)."""

    @abc.abstractmethod
    def acknowledge(self, ack: GateAck) -> None:
        """Publie l'acquittement (fichier `ack.json` ou ligne sur la socket).
        Ne lève pas : un échec est journalisé, l'acquittement suivant le
        remplacera."""

    def close(self) -> None:
        """Libère les ressources (inotify, socket) ; idempotent."""


class AlwaysAvailable(HostGate):
    """Porte par défaut : toujours `available`, sans limite ; les
    acquittements sont gardés en mémoire (lisibles par les essais)."""

    def __init__(self):
        self._state = GateState(state="available", seq=0, reason="idle")
        self._closed = threading.Event()
        self.acks: list[GateAck] = []

    def state(self) -> GateState:
        return self._state

    def wait_change(self, timeout: float) -> GateState:
        self._closed.wait(max(0.0, timeout))
        return self._state

    def acknowledge(self, ack: GateAck) -> None:
        self.acks.append(dataclasses.replace(ack, ts=ack.ts or time.time()))

    def close(self) -> None:
        self._closed.set()
