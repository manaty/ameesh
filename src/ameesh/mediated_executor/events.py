# SPDX-License-Identifier: AGPL-3.0-only
"""Flux d'événements de `/api/exec/v1/events` (contrat L107, figé).

`wakeups.subscribe([agent_mail, agent_lease, ameesh_budget])` devient
`GET /api/exec/v1/events` :

* **SSE** (`Accept: text/event-stream`) : un événement par bloc

      id: k3f9:17
      event: agent_mail
      data: {"to": "inge-front", "id": 812}

  `id` est un **curseur** `<flux>:<n>` : `<flux>` identifie le processus
  serveur (tiré à son démarrage), `n` croît strictement dans ce flux.
  `data` est le payload NOTIFY d'origine, intact ; battement `: ping`
  toutes les 20 s ; reprise par l'en-tête `Last-Event-ID` (ou `?after=`).
* **Attente longue** (repli) : `GET /events?wait=25&after=k3f9:17` rend
  `ameesh-exec-events/1` dès qu'un événement existe après `after`, ou une
  liste vide au bout de `wait` secondes (borné à 25).
* **Trou de reprise** : si `after` est sorti du tampon du serveur (1 000
  événements par processus) ou vient d'un autre flux (serveur redémarré,
  autre réplique), le serveur envoie un événement `reset` : le client
  le traite comme un réveil de tous ses agents (il relit en base).

Un réveil ne porte jamais de donnée qui fasse foi et peut se perdre :
l'exécuteur relit toujours en base et garde son sondage.

Payloads (ceux des déclencheurs, contrat 1.1) : `agent_mail` =
`{"to", "id", "from"}` (0001) ; `agent_lease` = `{"name", "owner",
"epoch", "status"}` (le déclencheur de 0001 écrit `agent`, le serveur le
renomme `name` : `stream.wire_payload`) ; `ameesh_budget` = celui de 0042.

Filtrage (serveur, L108) : `agent_mail` et `agent_lease` seulement si le
destinataire (`to`) ou l'agent (`name`) est admis sur l'hôte de
l'exécuteur ; `ameesh_budget` passe à tous.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from .contract import SCHEMA_EVENTS

#: canaux transmis (ceux de `wakeups.subscribe` dans l'exécuteur)
CHANNELS = ("agent_mail", "agent_lease", "ameesh_budget")
#: événement de service : trou de reprise, tout relire
RESET = "reset"
#: battement SSE (secondes)
PING_INTERVAL_S = 20
#: attente longue maximale (secondes)
MAX_WAIT_S = 25
#: tampon circulaire du serveur (événements par processus)
RING_SIZE = 1000


def cursor(stream: str, n: int) -> str:
    """Curseur de reprise `<flux>:<n>`."""
    return "%s:%d" % (stream, n)


def parse_cursor(text: str) -> tuple[str, int]:
    """`(flux, n)` ; `ValueError` si mal formé (le serveur rend alors `reset`)."""
    stream, sep, n = (text or "").rpartition(":")
    if not sep or not stream:
        raise ValueError("curseur mal formé : %r" % text)
    return stream, int(n)


@dataclasses.dataclass(frozen=True)
class Event:
    """Un événement du flux. `id` : curseur `<flux>:<n>` ; `data` : le
    payload NOTIFY d'origine (objet). Un `reset` a `data = {"reason": …}`."""

    id: str
    channel: str
    data: Mapping[str, Any]

    def to_json(self) -> dict:
        return {"id": self.id, "channel": self.channel, "data": dict(self.data)}

    @classmethod
    def from_json(cls, d: Mapping) -> "Event":
        return cls(str(d["id"]), str(d["channel"]), dict(d.get("data") or {}))

    def as_signal(self) -> dict:
        """La forme que rend `Subscription.wait` (storage.interface) :
        `{"channel", "payload"}`, payload en texte JSON comme NOTIFY."""
        return {"channel": self.channel,
                "payload": json.dumps(dict(self.data), ensure_ascii=False)}


def format_sse(event: Event) -> str:
    """Un bloc SSE (terminé par une ligne vide)."""
    data = json.dumps(dict(event.data), ensure_ascii=False, separators=(",", ":"))
    return "id: %s\nevent: %s\ndata: %s\n\n" % (event.id, event.channel, data)


def format_ping() -> str:
    return ": ping\n\n"


def format_retry(retry_ms: int = 3000) -> str:
    """Premier bloc d'une connexion SSE : délai de reconnexion conseillé."""
    return "retry: %d\n\n" % retry_ms


def parse_sse(lines: Iterable[str]) -> Iterator[Event]:
    """Lit un flux SSE ligne à ligne (lignes sans fin de ligne ou avec) ;
    ignore commentaires et battements ; rend les événements complets."""
    ev_id: Optional[str] = None
    channel = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if data:
                yield Event(ev_id or "", channel,
                            json.loads("\n".join(data)))
            ev_id, channel, data = None, "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            ev_id = value
        elif field == "event":
            channel = value
        elif field == "data":
            data.append(value)


def long_poll_body(events: Sequence[Event], *, last_id: str) -> dict:
    """Corps de la réponse d'attente longue (`ameesh-exec-events/1`).

    `last_id` : curseur à repasser en `after` à l'appel suivant (celui du
    dernier événement, ou le courant si la liste est vide). Un trou de
    reprise se signale par un événement `reset` en tête de liste."""
    return {"schema": SCHEMA_EVENTS, "events": [e.to_json() for e in events],
            "last_id": last_id}


def parse_long_poll(body: Mapping) -> tuple[list[Event], str]:
    if body.get("schema") != SCHEMA_EVENTS:
        raise ValueError("schéma %s attendu" % SCHEMA_EVENTS)
    return ([Event.from_json(e) for e in body.get("events") or []],
            str(body.get("last_id") or ""))
