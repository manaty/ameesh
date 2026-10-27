# SPDX-License-Identifier: AGPL-3.0-only
"""« Mon poste » (L93) : l'état local des exécuteurs, sur un canal local.

Le propriétaire d'une machine doit voir ce qui y tourne — les personas, les
lots, les tours, les ressources du bac à sable et le bail — **sans** être
membre de l'organisation qui emprunte la machine. Cet état ne passe donc pas
par l'API du mesh : il est servi par l'exécuteur sur une **socket Unix**, en
lecture seule, sans jeton réseau (`ameesh-device/1`, étude UI §4).

* la socket d'état ``device-<runner>.sock`` (0600, dossier 0700) écrit **un
  objet JSON par connexion**, puis ferme : un client sonde à sa cadence ;
* la socket de contrôle ``control-<runner>.sock`` (0600) reçoit un ordre
  JSON par connexion. Le seul ordre admis est ``{"op": "stop", "agent": …}``
  — l'**arrêt propre** : le même geste que ``agent-runner stop`` (statut
  ``stopped``, raison ``manuel``, lots rendus à la file, 0030). Aucune
  approbation, aucun ordre générique, aucune route réseau. L'arrêt immédiat
  est celui du runner Compute (destruction du bac à sable) : ameesh constate
  alors l'expiration du bail, déjà traitée par l'exécuteur.

Les deux sockets vérifient, quand la plateforme le permet, que le pair a le
même UID que l'exécuteur (SO_PEERCRED) : sur une machine personnelle,
l'humain et ses exécuteurs partagent l'utilisateur ; un autre utilisateur de
la machine ne lit ni n'arrête rien.

Le schéma est volontairement pauvre en secrets : identifiants, titres, états,
instants et mesures. Aucun jeton, aucune clé, aucun contenu de courrier.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import socketserver
import stat
import struct
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable, Iterable, Optional

from . import canon as canon_mod
from . import storage
from .config import NAME_RE

#: schéma de l'état local (« mon poste »)
SCHEMA = "ameesh-device/1"
#: schéma de la réponse du canal de contrôle
CONTROL_SCHEMA = "ameesh-device-control/1"
#: préfixes des fichiers servis dans le dossier local
STATE_PREFIX = "device-"
CONTROL_PREFIX = "control-"
#: âge maximal d'un relevé d'hôte repris tel quel (secondes)
READING_MAX_AGE_S = 120.0
#: cache de l'état, par exécuteur (secondes) : une sonde ne martèle pas la base
SNAPSHOT_CACHE_S = 1.0
#: taille maximale lue sur une socket (un ordre, jamais un fichier)
MAX_MESSAGE = 8192

SocketPath = str


class DeviceError(RuntimeError):
    """Canal local inutilisable (chemin, droits, socket déjà servi)."""


# --------------------------------------------------------------------------
# chemins
# --------------------------------------------------------------------------

def runtime_dir() -> str:
    """Dossier des canaux locaux : AMEESH_DEVICE_DIR, sinon XDG_RUNTIME_DIR.

    Hors session utilisateur (service système), repli sous le dossier d'état
    d'ameesh : le chemin reste local à la machine, jamais un port réseau.
    """
    explicit = os.environ.get("AMEESH_DEVICE_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "ameesh")
    state = os.environ.get("AMEESH_STATE") or os.path.expanduser(
        "~/.local/state/ameesh")
    return os.path.join(state, "device")


def _safe(value: str) -> str:
    """Un nom de fichier sûr (l'identifiant d'exécuteur reste lisible)."""
    out = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return out[:80] or "runner"


def paths(runner_id: str, directory: Optional[str] = None) -> tuple[SocketPath, SocketPath]:
    """``(état, contrôle)`` pour cet exécuteur.

    Un chemin de socket Unix est borné par le noyau (108 octets environ) :
    un dossier trop profond est refusé ici, avec un message lisible, plutôt
    que par un ``bind`` obscur au démarrage de l'exécuteur.
    """
    base = directory or runtime_dir()
    stem = _safe(runner_id)
    pair = (os.path.join(base, STATE_PREFIX + stem + ".sock"),
            os.path.join(base, CONTROL_PREFIX + stem + ".sock"))
    for path in pair:
        if len(path.encode("utf-8")) >= 100:
            raise DeviceError("local socket path too long (%d bytes): %s"
                              % (len(path.encode("utf-8")), path))
    return pair


def discover(directory: Optional[str] = None) -> list[SocketPath]:
    """Les sockets d'état servies sur cette machine, triées."""
    base = directory or runtime_dir()
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [os.path.join(base, n) for n in names
            if n.startswith(STATE_PREFIX) and n.endswith(".sock")]


# --------------------------------------------------------------------------
# schéma
# --------------------------------------------------------------------------

def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value else None


def _int(value: Any) -> Optional[int]:
    n = _number(value)
    return int(n) if n is not None else None


def _text(value: Any, limit: int = 400) -> str:
    return str(value)[:limit] if value not in (None, "") else ""


def _bigint(value: Any) -> Optional[int]:
    """Un identifiant de lot (colonne texte du registre) en entier, ou None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _first_project(row: Mapping[str, Any]) -> str:
    projects = row.get("projects")
    if isinstance(projects, (list, tuple)) and projects:
        return _text(projects[0], 100)
    if isinstance(projects, str) and projects:
        return _text(projects.split(",")[0].strip(), 100)
    return ""


def _organisations(cfg) -> dict[Optional[str], str]:
    """``canon`` → nom lisible ; la clé ``None`` est le canon par défaut.

    Le canon est relu au plus une fois par minute : cette lecture est un
    dossier git, pas une requête de la base, et l'état local est sondé.
    """
    key = (getattr(cfg, "canon", ""), tuple(getattr(cfg, "extra_canons", ()) or ()))
    with _ORG_LOCK:
        if _ORG_CACHE["key"] == key and time.time() - _ORG_CACHE["ts"] < 60.0:
            return dict(_ORG_CACHE["value"])
    value: dict[Optional[str], str] = {}
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception:
        canons = []
    for canon in canons:
        value[None if canon.is_default else canon.id] = canon.id or "default"
    if None not in value:
        value[None] = os.path.basename(os.path.normpath(getattr(cfg, "canon", "") or "")) \
            or "default"
    with _ORG_LOCK:
        _ORG_CACHE.update({"key": key, "ts": time.time(), "value": dict(value)})
    return value


_ORG_LOCK = threading.Lock()
_ORG_CACHE: dict[str, Any] = {"key": None, "ts": 0.0, "value": {}}


def build(cfg, *,
          agents: Iterable[Mapping[str, Any]],
          runner_id: str = "",
          socket_path: str = "",
          control_socket: str = "",
          pid: Optional[int] = None,
          since_ts: Optional[float] = None,
          host_reading: Optional[Mapping[str, Any]] = None,
          runner_readings: Iterable[Mapping[str, Any]] = (),
          lots: Optional[Mapping[int, Mapping[str, Any]]] = None,
          organisations: Optional[Mapping[Optional[str], str]] = None,
          gate: Optional[Mapping[str, Any]] = None,
          now: Optional[float] = None) -> dict:
    """Construit ``ameesh-device/1`` à partir de lignes déjà lues.

    Séparé de la base pour être testable sans Postgres : ``snapshot`` lit, ce
    module met en forme.
    """
    now = time.time() if now is None else float(now)
    host = getattr(cfg, "host", "") or ""
    orgs = dict(organisations or {None: "default"})
    lots = lots or {}
    readings = {}
    for reading in runner_readings:
        rid = reading.get("runner_id")
        if rid:
            readings[str(rid)] = reading

    meshes: dict[str, dict] = {}
    for row in agents:
        if (row.get("host") or "") != host:
            continue
        name = _text(row.get("name"), 100)
        if not name:
            continue
        lot_id = _bigint(row.get("session_work_item"))
        if lot_id is None:
            lot_id = _int(row.get("assigned_lot_id"))
        lot = lots.get(lot_id) if lot_id is not None else None
        if lot is None and lot_id is not None:
            lot = {"id": lot_id, "title": row.get("session_lot_title"),
                   "state": row.get("session_lot_state"), "app": None}
        project = _text((lot or {}).get("app"), 100) or _first_project(row) \
            or _text(row.get("team"), 100) or _text(row.get("chantier"), 100)
        key = row.get("canon") or None
        mesh_id = _text(orgs.get(key) or key or orgs.get(None), 100) or "default"
        mesh = meshes.setdefault(mesh_id, {
            "mesh": mesh_id,
            "organisation": _text(orgs.get(key) or mesh_id, 100) or mesh_id,
            "agents": [],
        })
        runner_name = "runner-%s@%s" % (name, host)
        reading = readings.get(runner_name) or {}
        cgroup = reading.get("cgroup") if isinstance(reading.get("cgroup"), Mapping) else {}
        turn_started = _number(row.get("turn_started_ts"))
        lease_live = bool(row.get("lease_live"))
        running = (row.get("status") == "running" and turn_started is not None
                   and lease_live)
        mesh["agents"].append({
            "name": name,
            "persona": name,
            "harness": _text(row.get("harness"), 40),
            "status": _text(row.get("status"), 40),
            "status_text": _text(row.get("status_text"), 300),
            "project": project,
            "lot": None if lot is None else {
                "id": lot_id,
                "title": _text(lot.get("title"), 300),
                "state": _text(lot.get("state"), 40),
                "app": _text(lot.get("app"), 100),
            },
            "turn": {
                "active": running,
                "started_ts": turn_started,
                "label": _text(row.get("turn_label"), 100),
            },
            "lease": {
                "live": lease_live,
                "owner": _text(row.get("lease_owner"), 120),
                "expires_ts": _number(row.get("lease_expires_ts")),
            },
            "sandbox": {
                "measured": bool(cgroup),
                "cpu": _number(cgroup.get("cpu_used")),
                "cpu_some": _number(cgroup.get("cpu_some")),
                "memory_bytes": _int(cgroup.get("mem_current_bytes")),
                "memory_max_bytes": _int(cgroup.get("mem_max_bytes")),
                "memory_used": _number(cgroup.get("mem_used")),
                "throttled": _number(cgroup.get("throttled")),
                # le réseau du bac à sable n'est pas mesuré par ameesh : le
                # dire (champ null + `unmeasured`) plutôt que d'inventer zéro
                "network": None,
            },
            "runner": runner_id,
            "since_ts": _number(row.get("status_since_ts"))
                        or _number(row.get("status_reason_since_ts")),
            "last_turn_ts": _number(row.get("last_turn_ts")),
        })

    reading = host_reading or {}
    resources = {
        "sampled_ts": _number(reading.get("sampled_ts")),
        "cpu_count": _int(reading.get("cpu_count")),
        "load1": _number(reading.get("load1")),
        "mem_available_bytes": _int(reading.get("mem_available_bytes")),
        "mem_total_bytes": _int(reading.get("mem_total_bytes")),
        "app_cpu": _number(reading.get("app_cpu")),
        "guest_cpu": _number(reading.get("guest_cpu")),
        "guest_mem_bytes": _int(reading.get("guest_mem_bytes")),
        "turns_in_progress": _int(reading.get("turns_in_progress")),
    }
    unmeasured = list(reading.get("unmeasured") or []) if isinstance(
        reading.get("unmeasured"), (list, tuple)) else []
    if "network" not in unmeasured:
        unmeasured.append("network")

    return {
        "schema": SCHEMA,
        "generated_ts": now,
        "host": host,
        "runners": [{
            "id": runner_id,
            "pid": pid if pid is not None else os.getpid(),
            "since_ts": since_ts,
            "socket": socket_path,
            "control_socket": control_socket,
        }],
        "meshes": [meshes[key] for key in sorted(meshes)],
        "resources": resources,
        "compute": {"gate": dict(gate) if gate else None, "lease": None},
        "unmeasured": unmeasured,
    }


def snapshot(cfg, db, *,
             names: Optional[Iterable[str]] = None,
             runner_id: str = "",
             socket_path: str = "",
             control_socket: str = "",
             gate: Optional[Mapping[str, Any]] = None,
             now: Optional[float] = None) -> dict:
    """Lit la base et rend ``ameesh-device/1`` pour cet exécuteur.

    ``names`` absent (``None``) sert tous les agents de l'hôte ; un filtre
    donné, même vide, ne sert que ses noms (une liste d'admission vide
    n'affiche personne, jamais « tous », #3972)."""
    wanted = {str(n) for n in names} if names is not None else None
    host = getattr(cfg, "host", "") or ""
    store = storage.of(db)
    rows = [row for row in store.operations.listing()
            if (row.get("host") or "") == host
            and (wanted is None or row.get("name") in wanted)]
    lot_ids = sorted({_bigint(row.get("session_work_item")) for row in rows
                      if _bigint(row.get("session_work_item")) is not None}
                     | {_int(row.get("assigned_lot_id")) for row in rows
                        if _int(row.get("assigned_lot_id")) is not None})
    lots: dict[int, dict] = {}
    if lot_ids:
        marks = ", ".join(["%s"] * len(lot_ids))
        for row in db.query("SELECT id, app, title, state FROM work_items"
                            " WHERE id IN (" + marks + ")", tuple(lot_ids)):
            lots[int(row["id"])] = dict(row)
    readings = store.hosts.current(host) if host else []
    host_reading = readings[0] if readings else None
    runners = store.hosts.runners(host, READING_MAX_AGE_S) if host else []
    return build(cfg, agents=rows, runner_id=runner_id, socket_path=socket_path,
                 control_socket=control_socket, host_reading=host_reading,
                 runner_readings=runners, lots=lots,
                 organisations=_organisations(cfg), gate=gate, now=now)


# --------------------------------------------------------------------------
# arrêt propre (le seul ordre du canal de contrôle)
# --------------------------------------------------------------------------

def stop_agent(cfg, db, name: str, *, strict: bool = False) -> list[dict]:
    """Arrange l'agent comme ``agent-runner stop`` : propre, jamais brutal.

    Le tour en cours finit (ou s'arrête au point sûr de l'exécuteur) ; les
    lots ouverts retournent à la file d'équipe ; le mesh reçoit l'événement
    par la base. Lève ``ValueError`` pour un nom invalide ; ``strict`` refuse
    aussi un agent inconnu ou placé sur un autre hôte (canal local).
    """
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("invalid agent name: %r" % (name,))
    if not strict:
        return _write_stop(cfg, db, name)
    # B1 (relecture cx3 du 20/10/2026, message #120743) : le contrôle d'hôte
    # et l'arrêt ne formaient pas un seul geste. Un placement concurrent
    # pouvait déplacer l'agent entre la lecture du registre et les écritures,
    # et l'on arrêtait alors l'agent d'un autre hôte. On verrouille la ligne
    # du registre d'abord — même ordre que la remise des lots, registre puis
    # lots — et on garde ce verrou jusqu'au COMMIT : le placement attend.
    with db.transaction() as tx:
        rows = tx.query(
            "SELECT name, host FROM agent_registry"
            " WHERE name = (SELECT ameesh_resolve_agent_name(%s, %s))"
            " FOR NO KEY UPDATE", (name, "device.stop"))
        if not rows:
            raise ValueError("unknown agent: %s" % name)
        row = rows[0]
        if (row.get("host") or "") != (getattr(cfg, "host", "") or ""):
            raise ValueError("agent %s is not on this host" % name)
        return _write_stop(cfg, tx, row["name"])


def _write_stop(cfg, db, name: str) -> list[dict]:
    """Le geste d'arrêt : statut ``stopped``, puis remise des lots.

    L'arrêt strict l'appelle dans la transaction qui tient le verrou du
    registre ; l'arrêt non strict l'appelle directement.
    """
    from . import registry, work as work_mod
    registry.set_status(db, name, "stopped", status_text="stopped by hand",
                        stop_reason="manuel")
    measure_actor = work_mod.measurement_actor(db, cfg)
    returned = work_mod.release_lots(
        db, name,
        reason="stopped by hand: returned to the team queue (#753)",
        actor="stop", measure_actor=measure_actor,
        measure_reason=("promoted lot whose measurement is still to be redone: "
                        "handed to %s (#753)" % measure_actor) if measure_actor else "",
        stopped_with=work_mod.RELEASABLE_STOP_REASONS)
    return list(returned or ())


# --------------------------------------------------------------------------
# canaux Unix
# --------------------------------------------------------------------------

def _peer_uid(conn: socket.socket) -> Optional[int]:
    """UID du pair sur Linux (SO_PEERCRED) ; None si la plateforme l'ignore."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return int(uid)
    except (OSError, struct.error):
        return None


def _same_user(conn: socket.socket) -> bool:
    uid = _peer_uid(conn)
    mine = getattr(os, "getuid", lambda: None)()
    return uid is None or mine is None or uid == mine


class _BaseServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, path: str, handler, *, mode: int = 0o600):
        self.path = path
        self._mode = mode
        _prepare(path)
        super().__init__(path, handler)
        try:
            os.chmod(path, mode)
            self._inode = os.stat(path).st_ino
        except OSError:
            self.server_close()
            raise

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            # ne retire que NOTRE socket : un successeur a pu reprendre le nom
            try:
                if os.stat(self.path).st_ino == getattr(self, "_inode", -1):
                    os.unlink(self.path)
            except (FileNotFoundError, OSError):
                pass


def _remove_stale_socket(path: str) -> None:
    """Remove only an owned socket whose listener is demonstrably absent."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DeviceError("local channel path unreadable: %s (%s)" % (path, exc)) from exc
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid():
        raise DeviceError("refusing to replace a non-socket or foreign-owned path: %s" % path)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(path)
    except OSError as exc:
        if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
            raise DeviceError("local channel liveness uncertain: %s (%s)" % (path, exc)) from exc
        try:
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise DeviceError("local channel changed during probe: %s" % path)
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as remove_exc:
            raise DeviceError("stale socket %s cannot be removed: %s"
                              % (path, remove_exc)) from remove_exc
    else:
        raise DeviceError("local channel already served: %s" % path)
    finally:
        probe.close()


def _prepare(path: str) -> None:
    """Use a private directory without changing an existing one's permissions.

    Startup also reaps abandoned channel names from previous runner PIDs,
    including in the persistent state-directory fallback.
    """
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, mode=0o700)
    except FileExistsError:
        pass
    else:
        os.chmod(directory, 0o700)
    info = os.lstat(directory)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise DeviceError("local channel directory must be owned and private (0700): %s"
                          % directory)
    _remove_stale_socket(path)
    for name in os.listdir(directory):
        if not (name.startswith((STATE_PREFIX, CONTROL_PREFIX)) and name.endswith(".sock")):
            continue
        other = os.path.join(directory, name)
        if other == path:
            continue
        try:
            _remove_stale_socket(other)
        except DeviceError:
            # Live, busy, inaccessible, replaced or unrelated: leave it alone.
            pass


class _StateHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        if not _same_user(self.connection):
            return
        self.connection.settimeout(10.0)
        try:
            body = self.server.snapshot()
            self.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8") + b"\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return
        except Exception as exc:  # une sonde ne tue pas l'exécuteur
            try:
                self.wfile.write(json.dumps({
                    "schema": SCHEMA, "error": "unavailable",
                    "message": str(exc)[:200],
                }).encode("utf-8") + b"\n")
            except OSError:
                pass


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        if not _same_user(self.connection):
            return
        self.connection.settimeout(10.0)
        try:
            line = self.rfile.readline(MAX_MESSAGE)
        except OSError:
            return
        reply: dict[str, Any]
        try:
            message = json.loads(line.decode("utf-8") or "{}")
            if not isinstance(message, Mapping) or message.get("op") != "stop":
                raise ValueError("unsupported op")
            agent = message.get("agent")
            if not isinstance(agent, str) or not NAME_RE.match(agent):
                raise ValueError("invalid agent")
            allowed = self.server.allowed
            if allowed is not None and agent not in allowed:
                raise ValueError("agent not served by this runner")
            returned = self.server.stop_fn(agent) or []
            reply = {"schema": CONTROL_SCHEMA, "ok": True, "agent": agent,
                     "returned": [int(row["id"]) for row in returned
                                  if isinstance(row, Mapping) and _bigint(row.get("id")) is not None]}
        except (ValueError, TypeError) as exc:
            reply = {"schema": CONTROL_SCHEMA, "ok": False, "error": str(exc)[:200]}
        except Exception as exc:  # base ou registre : refus lisible
            reply = {"schema": CONTROL_SCHEMA, "ok": False,
                     "error": "stop failed: %s" % str(exc)[:160]}
        try:
            self.wfile.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            self.wfile.flush()
        except OSError:
            return


class LocalChannel:
    """Un canal Unix dans un fil ; ``stop`` ferme et retire la socket."""

    def __init__(self, path: str, handler, *, mode: int = 0o600):
        self.path = path
        self._server = _BaseServer(path, handler, mode=mode)
        self._thread: Optional[threading.Thread] = None
        self._started = False

    def start(self) -> "LocalChannel":
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.2},
                                        name="device-channel", daemon=True)
        self._thread.start()
        self._started = True
        return self

    def stop(self) -> None:
        try:
            if self._started:
                # `shutdown` attend `serve_forever` : ne l'appeler que si le
                # fil a bien été lancé (un canal créé puis refusé ne bloque pas)
                self._server.shutdown()
        finally:
            self._server.server_close()
            self._started = False
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(timeout=2.0)


class StateChannel(LocalChannel):
    """Canal d'état, en **lecture seule** : un objet JSON par connexion."""

    def __init__(self, path: str, builder: Callable[[], Mapping[str, Any]],
                 *, cache_s: float = SNAPSHOT_CACHE_S,
                 log: Optional[Callable[[str], None]] = None):
        self.builder = builder
        self.cache_s = max(0.0, float(cache_s))
        self.log = log or (lambda _m: None)
        self._cache: Optional[dict] = None
        self._cache_ts = 0.0
        self._lock = threading.Lock()
        self._last_error = ""

        def snapshot() -> Mapping[str, Any]:
            with self._lock:
                now = time.monotonic()
                if self._cache is not None and now - self._cache_ts < self.cache_s:
                    return self._cache
            try:
                body = dict(self.builder())
                with self._lock:
                    self._cache, self._cache_ts = body, time.monotonic()
                    self._last_error = ""
                return body
            except Exception as exc:
                message = "%s: %s" % (type(exc).__name__, str(exc)[:160])
                if message != self._last_error:
                    self.log("device state: %s" % message)
                    self._last_error = message
                return {"schema": SCHEMA, "error": "unavailable",
                        "message": str(exc)[:200]}

        super().__init__(path, _StateHandler)
        self._server.snapshot = snapshot


class ControlChannel(LocalChannel):
    """Canal de contrôle : uniquement l'arrêt propre d'un agent de l'hôte.

    An omitted or empty ``allowed`` filter serves all local agents, just
    like the runner's ``agents_filter``; ``stop_fn`` enforces the host check.
    """

    def __init__(self, path: str, stop_fn: Callable[[str], Any],
                 *, allowed: Optional[Iterable[str]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.stop_fn = stop_fn
        self.allowed = (frozenset(str(a) for a in allowed) if allowed is not None else None) or None
        self.log = log or (lambda _m: None)
        super().__init__(path, _ControlHandler)
        self._server.stop_fn = stop_fn
        self._server.allowed = self.allowed

    def set_allowed(self, allowed: Iterable[str]) -> None:
        """#3925 : la liste d'admission relue de l'exécuteur ; ici une liste
        vide n'admet aucun agent (pas « tous », comme au constructeur)."""
        self.allowed = frozenset(str(a) for a in allowed)
        self._server.allowed = self.allowed


# --------------------------------------------------------------------------
# client et fusion
# --------------------------------------------------------------------------

def query(path: str, *, timeout: float = 5.0) -> dict:
    """Lit l'état d'un canal, ou lève ``DeviceError`` (absent, illisible)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        raw = b"".join(chunks).split(b"\n", 1)[0]
        if not raw:
            raise DeviceError("empty device state from %s" % path)
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict) or body.get("schema") != SCHEMA:
            raise DeviceError("not an ameesh-device/1 state: %s" % path)
        return body
    except (OSError, ValueError) as exc:
        raise DeviceError("device state unreadable at %s: %s" % (path, exc))
    finally:
        sock.close()


def stop(path: str, agent: str, *, timeout: float = 10.0) -> dict:
    """Envoie l'arrêt propre au canal de contrôle ; rend la réponse."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(json.dumps({"op": "stop", "agent": agent}).encode("utf-8") + b"\n")
        with sock.makefile("rb") as fh:
            raw = fh.readline(MAX_MESSAGE)
        if not raw:
            raise DeviceError("empty control reply from %s" % path)
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict) or body.get("schema") != CONTROL_SCHEMA:
            raise DeviceError("not an ameesh-device-control/1 reply: %s" % path)
        return body
    except (OSError, ValueError) as exc:
        raise DeviceError("device control unusable at %s: %s" % (path, exc))
    finally:
        sock.close()


def merge(snapshots: Iterable[Mapping[str, Any]]) -> dict:
    """Fusionne les états de plusieurs exécuteurs d'une même machine.

    Chaque exécuteur ne sert que ses agents ; la vue « mon poste » les réunit.
    Un doublon (même mesh, même nom) garde le relevé le plus récent
    (``generated_ts``), jamais celui dont le tour a commencé le plus tôt.
    """
    bodies = [body for body in snapshots if isinstance(body, Mapping)
              and body.get("schema") == SCHEMA and body.get("host")]
    if not bodies:
        return {"schema": SCHEMA, "error": "unavailable", "message": "no local runner"}
    host = str(bodies[0].get("host"))
    bodies.sort(key=lambda b: _number(b.get("generated_ts")) or 0.0)
    meshes: dict[str, dict] = {}
    agents_seen: dict[tuple[str, str], float] = {}
    runners: dict[str, dict] = {}
    resources: dict = {}
    compute: dict = {"gate": None, "lease": None}
    unmeasured: set = set()
    for body in bodies:
        report_ts = _number(body.get("generated_ts")) or 0.0
        for runner in body.get("runners") or []:
            if isinstance(runner, Mapping) and runner.get("id"):
                runners[str(runner["id"])] = dict(runner)
        if isinstance(body.get("resources"), Mapping) and body["resources"].get("sampled_ts"):
            stamp = _number(body["resources"].get("sampled_ts")) or 0.0
            if stamp > (_number(resources.get("sampled_ts")) or 0.0):
                resources = dict(body["resources"])
        gate = (body.get("compute") or {}).get("gate") if isinstance(
            body.get("compute"), Mapping) else None
        if gate and not compute.get("gate"):
            compute["gate"] = dict(gate)
        if isinstance(body.get("compute"), Mapping) and body["compute"].get("lease"):
            compute["lease"] = body["compute"]["lease"]
        for item in body.get("unmeasured") or []:
            unmeasured.add(str(item))
        for mesh in body.get("meshes") or []:
            if not isinstance(mesh, Mapping):
                continue
            key = str(mesh.get("mesh") or "default")
            target = meshes.setdefault(key, {
                "mesh": key,
                "organisation": mesh.get("organisation") or key,
                "agents": [],
            })
            for agent in mesh.get("agents") or []:
                if not isinstance(agent, Mapping):
                    continue
                name = str(agent.get("name") or "")
                if not name:
                    continue
                # N1 (#382) : la fraîcheur d'un doublon est celle du relevé,
                # pas celle du tour. Un running ancien (generated_ts=100,
                # started_ts=90) ne doit pas masquer un idle plus récent
                # (generated_ts=110, started_ts=null).
                if agents_seen.get((key, name), -1.0) > report_ts:
                    continue
                if agents_seen.get((key, name)) is not None:
                    target["agents"] = [a for a in target["agents"]
                                        if not (isinstance(a, Mapping)
                                                and a.get("name") == name)]
                agents_seen[(key, name)] = report_ts
                target["agents"].append(dict(agent))
    for mesh in meshes.values():
        mesh["agents"].sort(key=lambda a: str(a.get("name") or ""))
    return {
        "schema": SCHEMA,
        "generated_ts": max((_number(b.get("generated_ts")) or 0.0 for b in bodies),
                            default=0.0),
        "host": host,
        "runners": [runners[k] for k in sorted(runners)],
        "meshes": [meshes[k] for k in sorted(meshes)],
        "resources": resources,
        "compute": compute,
        "unmeasured": sorted(unmeasured),
    }
