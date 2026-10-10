# SPDX-License-Identifier: AGPL-3.0-only
"""Disponibilité des hôtes, côté serveur (L112) : ce que le serveur sait de
la porte de chaque hôte, et les alertes qui en découlent.

* L108 reçoit `PUT /api/exec/v1/host/availability`
  (`ameesh-exec-availability/1`), le contrôle par `check_body` et l'écrit par
  `AvailabilityRegistry.put` ; `claimable` et `claim` refusent un hôte dont
  `admissible(row)` est faux (`host_unavailable`), même si l'appareil
  insistait.
* Un hôte classique muni d'une porte (essai, VM Linux simple) écrit le même
  registre sur son disque (`FileRegistry`, `<état>/exec/availability.json`) :
  `ameesh alerts` et `ameesh notify` le lisent sur ce poste.

Alertes (`exploitation`, donc poussées par `ameesh notify`) :

* `host_unavailable` : porte illisible ou absente (`reason = unknown`),
  exécuteur révoqué (`revoked`), ou hôte indisponible depuis plus de
  `DEFAULT_UNAVAILABLE_ALERT_S` (24 h) ;
* `host_drain_overdue` : retrait (`draining`/`stopped`) dont l'échéance est
  passée de plus de 30 s alors que des baux de l'hôte sont encore détenus.

Le registre de fichier est un défaut ; L108 peut fournir une implémentation
en base derrière la même interface.
"""
from __future__ import annotations

import abc
import fcntl
import json
import os
import time
from typing import Any, Iterable, Mapping, Optional

from .contract import SCHEMA_AVAILABILITY
from .gate import DEFAULT_DRAIN_S, STATES
from .host_gate import OVERDUE_GRACE_S

REGISTRY_SCHEMA = "ameesh-host-availability-registry/1"
#: hôte indisponible depuis plus longtemps : alerte (un appareil prêté est
#: souvent indisponible ; seule une longue absence mérite d'être dite)
DEFAULT_UNAVAILABLE_ALERT_S = 86400.0
#: raisons qui alertent tout de suite
URGENT_REASONS = ("unknown", "revoked")
ALERT_TYPES = ("host_unavailable", "host_drain_overdue")


def check_body(body: Any) -> dict:
    """Contrôle `ameesh-exec-availability/1` ; rend le corps normalisé ou lève
    `ValueError` (le serveur répond alors `400 bad_request`)."""
    if not isinstance(body, Mapping) or body.get("schema") != SCHEMA_AVAILABILITY:
        raise ValueError("schéma %s attendu" % SCHEMA_AVAILABILITY)
    state = body.get("state")
    if state not in STATES:
        raise ValueError("state : %s attendu" % " | ".join(STATES))
    seq = body.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise ValueError("seq : entier attendu")
    if bool(body.get("available")) != (state == "available"):
        raise ValueError("available et state se contredisent")
    until = body.get("until_ts")
    if until is not None and (not isinstance(until, (int, float)) or isinstance(until, bool)):
        raise ValueError("until_ts : nombre ou null attendu")
    caps = body.get("caps") or {}
    if not isinstance(caps, Mapping):
        raise ValueError("caps : objet attendu")
    return {"schema": SCHEMA_AVAILABILITY, "available": state == "available",
            "state": state, "seq": seq, "until_ts": until, "caps": dict(caps),
            "reason": str(body.get("reason") or "unknown")}


def admissible(row: Optional[Mapping]) -> bool:
    """L'hôte peut-il recevoir un bail ? Sans ligne : oui (hôte sans porte,
    comportement d'avant L112)."""
    if not row:
        return True
    return (row.get("body") or {}).get("state") == "available"


class AvailabilityRegistry(abc.ABC):
    """Dernier état connu de chaque hôte. Ligne : `{host, executor_id, body,
    gate?, ack?, since_ts, updated_ts}` — `since_ts` : début de l'état
    courant (inchangé tant que `state` ne change pas)."""

    @abc.abstractmethod
    def put(self, host: str, body: Mapping, *, executor_id: str = "",
            gate: Mapping | None = None, ack: Mapping | None = None,
            now: float | None = None) -> dict:
        """Enregistre l'état d'un hôte ; rend la ligne."""

    @abc.abstractmethod
    def rows(self) -> list[dict]:
        """Toutes les lignes, triées par hôte."""

    def get(self, host: str) -> Optional[dict]:
        for row in self.rows():
            if row["host"] == host:
                return row
        return None


class MemoryRegistry(AvailabilityRegistry):
    """En mémoire (essais, ou un serveur qui n'a pas encore de table)."""

    def __init__(self):
        self._rows: dict[str, dict] = {}

    def put(self, host, body, *, executor_id="", gate=None, ack=None, now=None) -> dict:
        row = _merge(self._rows.get(host), host, check_body(body), executor_id, gate, ack,
                     time.time() if now is None else float(now))
        self._rows[host] = row
        return dict(row)

    def rows(self) -> list[dict]:
        return [dict(self._rows[h]) for h in sorted(self._rows)]


class FileRegistry(AvailabilityRegistry):
    """Dans un fichier JSON (verrou `flock`, écriture atomique)."""

    def __init__(self, path: str):
        self.path = path

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("schema") != REGISTRY_SCHEMA:
            return {}
        hosts = data.get("hosts")
        return hosts if isinstance(hosts, dict) else {}

    def put(self, host, body, *, executor_id="", gate=None, ack=None, now=None) -> dict:
        body = check_body(body)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path + ".lock", "a") as verrou:
            fcntl.flock(verrou, fcntl.LOCK_EX)
            hosts = self._load()
            row = _merge(hosts.get(host), host, body, executor_id, gate, ack,
                         time.time() if now is None else float(now))
            hosts[host] = row
            tmp = "%s.%d.tmp" % (self.path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"schema": REGISTRY_SCHEMA, "hosts": hosts}, fh,
                          ensure_ascii=False, sort_keys=True, indent=1)
            os.replace(tmp, self.path)
        return dict(row)

    def rows(self) -> list[dict]:
        hosts = self._load()
        return [dict(hosts[h], host=h) for h in sorted(hosts) if isinstance(hosts[h], dict)]


def _merge(old: Optional[Mapping], host: str, body: dict, executor_id: str,
           gate: Mapping | None, ack: Mapping | None, now: float) -> dict:
    old = dict(old or {})
    same = (old.get("body") or {}).get("state") == body["state"]
    row = {"host": host, "executor_id": executor_id or old.get("executor_id") or "",
           "body": body, "since_ts": old.get("since_ts") if same and old.get("since_ts")
           else now, "updated_ts": now}
    if gate is not None:
        row["gate"] = dict(gate)
    elif same and old.get("gate"):
        row["gate"] = old["gate"]
    if ack is not None:
        row["ack"] = dict(ack)
    elif same and old.get("ack"):
        row["ack"] = old["ack"]
    return row


def default_path(cfg) -> str:
    return os.path.join(cfg.state_dir, "exec", "availability.json")


def _deadline(row: Mapping) -> float:
    gate = row.get("gate") or {}
    deadline = gate.get("drain_deadline_ts")
    if (row.get("body") or {}).get("state") == "stopped":
        return float(row.get("since_ts") or 0.0)
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        return float(deadline)
    return float(row.get("since_ts") or 0.0) + DEFAULT_DRAIN_S


def alerts(rows: Iterable[Mapping], *, now: float | None = None,
           held: Mapping[str, Iterable[str]] | None = None,
           unavailable_after_s: float = DEFAULT_UNAVAILABLE_ALERT_S) -> list[dict]:
    """Alertes au format d'`exploitation` (`ameesh-alert/1`). `held` : agents
    dont l'hôte détient encore le bail, par hôte (la base fait foi) ; à
    défaut, l'acquittement enregistré."""
    from .. import exploitation

    now = time.time() if now is None else float(now)
    out: list[dict] = []
    for row in rows:
        host = row.get("host")
        body = row.get("body") or {}
        state = body.get("state")
        if not host or state not in STATES or state == "available":
            continue
        since = float(row.get("since_ts") or now)
        reason = str(body.get("reason") or "unknown")
        absent = now - since
        if reason in URGENT_REASONS or absent >= unavailable_after_s:
            out.append(exploitation._alert(
                "host_unavailable", None, since, int(absent),
                0 if reason in URGENT_REASONS else unavailable_after_s,
                "hôte %s indisponible (%s, %s) depuis %ds%s" % (
                    host, state, reason, int(absent),
                    " : porte illisible ou absente" if reason == "unknown" else ""),
                host=host, reason=reason, gate_state=state))
        if held is not None:
            detenus = sorted(held.get(host) or ())
        else:
            detenus = sorted((row.get("ack") or {}).get("held") or ())
        deadline = _deadline(row)
        if detenus and now >= deadline + OVERDUE_GRACE_S:
            out.append(exploitation._alert(
                "host_drain_overdue", None, deadline, int(now - deadline), OVERDUE_GRACE_S,
                "hôte %s : retrait (%s) en retard de %ds, baux encore détenus : %s" % (
                    host, state, int(now - deadline), ", ".join(detenus)),
                host=host, held=detenus, gate_state=state))
    return out


def local_alerts(cfg, listing: list[dict], now: float | None = None) -> list[dict]:
    """Alertes du registre local de ce poste (`default_path`), baux tirés du
    listing de la base ; rien si le registre n'existe pas."""
    path = default_path(cfg)
    if not os.path.exists(path):
        return []
    held: dict[str, list[str]] = {}
    for row in listing:
        if row.get("lease_live") and row.get("host"):
            held.setdefault(row["host"], []).append(row["name"])
    return alerts(FileRegistry(path).rows(), now=now, held=held)
