# SPDX-License-Identifier: AGPL-3.0-only
"""EP8.5a (#6871) : la grappe PostgreSQL du mesh, lue sans grappe réelle.

`ameesh db status` dit le nœud local (primaire ou secours), les secours de
`pg_stat_replication` (état, `sync_state`, retard d'écriture/flush/replay) et,
quand `patroni_url` est configurée, la vue Patroni par son API REST
(`/patroni` et `/cluster`). `ameesh doctor` imprime une ligne `db` tirée de la
même lecture.

Les alertes `db_*` de l'étude EP8 §7 sont calculées ici, puis branchées sur la
passe d'alertes ordinaire (`exploitation.alerts`) : elles partent donc de la
passe d'un hôte du mesh, PC éteint.

- `db_no_standby` : une grappe est attendue et aucun secours ne diffuse ;
- `db_replication_lag` : le pire retard de rejeu dépasse le seuil ;
- `db_sync_lost` : `synchronous_standby_names` est posé et aucun secours
  connecté n'est `sync`/`quorum` ;
- `db_two_primaries` : Patroni voit deux leaders, ou ce nœud écrit alors que
  le leader DCS est un autre membre ;
- `db_stats_masked` : des walsenders sont visibles mais PostgreSQL en masque
  l'état et les retards (rôle sans `pg_read_all_stats`) : rien n'est
  interprété, et le chemin de lecture autorisé est dans
  `docs/EXPLOITATION.md`.

Depuis PostgreSQL 10, `pg_stat_get_wal_senders` ne laisse que `pid` aux rôles
qui n'ont pas les privilèges de `pg_read_all_stats` (superutilisateur ou
membre de `pg_monitor` compris) : tous les autres champs de la ligne sont
NULL. Une telle ligne n'est jamais comptée comme un secours ni interprétée ;
le statut dit la visibilité insuffisante et l'alerte `db_stats_masked` la
porte. Zéro ligne, elle, reste une absence certaine : le filtrage des lignes
n'est pas masqué, seules leurs colonnes le sont.

Le mono-nœud actuel est le défaut : ni `patroni_url`, ni réplication
configurée, `db_min_standbys = 0` — le statut est lisible et aucune alerte de
grappe ne part. Aucune grappe n'est contactée pour tester ce module : les
lignes SQL et le JSON Patroni sont des faux.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

STATUS_SCHEMA = "ameesh-db-status/1"
DEFAULT_LAG_THRESHOLD_S = 30.0
#: deux appels au plus par lecture, et jamais deux fois dans une passe à blanc
#: (`db.batched`) : le relevé doit rester plus court que l'intervalle du
#: notifieur.
PATRONI_TIMEOUT_S = 2.0
#: états `sync_state` qui prouvent une synchronisation (un seul suffit)
SYNC_STATES = ("sync", "quorum")
#: rôles Patroni d'un primaire : `/cluster` dit `leader`, `/patroni` dit
#: `primary` ; `master` est l'ancien nom, toléré.
LEADER_ROLES = ("leader", "primary", "master")

_LOCAL_SQL = """
SELECT pg_is_in_recovery() AS in_recovery,
       current_setting('server_version') AS server_version,
       current_setting('synchronous_standby_names') AS synchronous_standby_names,
       current_setting('synchronous_commit') AS synchronous_commit,
       pg_postmaster_start_time() AS started_at,
       host(inet_server_addr()) AS server_addr,
       inet_server_port() AS server_port,
       current_database() AS database,
       current_user AS role_name,
       pg_has_role(current_user, 'pg_read_all_stats', 'USAGE') AS read_all_stats,
       current_setting('is_superuser') AS superuser
"""

_REPLICATION_SQL = """
SELECT pid, application_name, client_addr::text AS client_addr,
       state, sync_state, sync_priority,
       EXTRACT(EPOCH FROM write_lag) AS write_lag_s,
       EXTRACT(EPOCH FROM flush_lag) AS flush_lag_s,
       EXTRACT(EPOCH FROM replay_lag) AS replay_lag_s,
       sent_lsn::text AS sent_lsn, replay_lsn::text AS replay_lsn
  FROM pg_stat_replication
 ORDER BY application_name
"""

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class StatusUnavailable(RuntimeError):
    """La lecture du statut n'a pas de sens sur ce backend (médié)."""


# --------------------------------------------------------------------------
# lecture
# --------------------------------------------------------------------------

def _short(value, limit: int = 160) -> str:
    return " ".join(str(value).split())[:limit]


def mask_url(url: str) -> str:
    """L'URL Patroni sans identifiants ni requête : un statut se cite sans secret."""
    try:
        parts = urllib.parse.urlsplit(url)
        netloc = parts.netloc
        if parts.username is not None or parts.password is not None:
            host = parts.hostname or ""
            netloc = host + (":%d" % parts.port if parts.port else "")
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return url


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("t", "true", "1", "yes", "on")
    return bool(value)


def _as_seconds(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json(base_url: str, path: str, *, timeout: float = PATRONI_TIMEOUT_S):
    url = base_url.rstrip("/") + path
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _OPENER.open(request, timeout=timeout) as response:
        payload = response.read(1_000_000)
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Patroni answered a %s, not an object" % type(data).__name__)
    return data


def patroni(cfg, *, reader=_json) -> dict | None:
    """La vue Patroni, ou None si `patroni_url` n'est pas configurée.

    Une API illisible ne casse jamais le statut : chaque appel a son erreur
    dans `errors` (`local`, `cluster`) et la détection « deux primaires »
    n'exploite que les réponses qui ont conclu, indépendamment l'une de
    l'autre.
    """
    url = str(getattr(cfg, "patroni_url", "") or "").strip()
    if not url:
        return None
    out = {"url": mask_url(url), "local": None, "members": [], "error": None,
           "errors": {"local": None, "cluster": None},
           "name": None, "scope": None, "version": None, "paused": None}
    try:
        local = reader(url, "/patroni")
    except Exception as exc:  # une sonde réseau ne casse pas le statut
        local = None
        out["errors"]["local"] = _short(exc)
    if isinstance(local, dict):
        meta = local.get("patroni")
        meta = meta if isinstance(meta, dict) else {}
        out["name"] = meta.get("name")
        out["scope"] = meta.get("scope")
        out["version"] = meta.get("version")
        out["paused"] = local.get("pause") if "pause" in local else None
        out["local"] = {
            "name": meta.get("name"),
            "role": local.get("role"),
            "state": local.get("state"),
            "timeline": local.get("timeline"),
            "dcs_last_seen": local.get("dcs_last_seen"),
            "cluster_unlocked": local.get("cluster_unlocked"),
            "pending_restart": local.get("pending_restart"),
        }
    try:
        cluster = reader(url, "/cluster")
    except Exception as exc:
        cluster = None
        out["errors"]["cluster"] = _short(exc)
    if isinstance(cluster, dict):
        members = cluster.get("members")
        if isinstance(members, list):
            out["members"] = [m for m in members if isinstance(m, dict)]
        out["scope"] = out["scope"] or cluster.get("scope")
        if "pause" in cluster:
            out["paused"] = bool(cluster.get("pause"))
    out["error"] = "; ".join(
        "%s: %s" % (source, message)
        for source, message in out["errors"].items() if message) or None
    return out


def expected_standbys(cfg, status: dict | None = None) -> int:
    """Combien de secours la grappe doit avoir pour ne pas alerter.

    `db_min_standbys` explicite l'emporte ; sinon une `patroni_url` ou un
    `synchronous_standby_names` non vide vaut un secours attendu. Mono-nœud
    sans configuration : zéro, donc aucune alerte de grappe.
    """
    try:
        explicit = int(getattr(cfg, "db_min_standbys", 0) or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit
    if str(getattr(cfg, "patroni_url", "") or "").strip():
        return 1
    node = (status or {}).get("node") or {}
    if str(node.get("synchronous_standby_names") or "").strip():
        return 1
    return 0


def _node_row(db) -> dict:
    rows = db.query(_LOCAL_SQL)
    if not rows:
        raise StatusUnavailable("the database returned no row for the local node")
    return rows[0]


def _replication_rows(db) -> list[dict]:
    return list(db.query(_REPLICATION_SQL) or [])


def _masked_row(row) -> bool:
    """Vrai pour une ligne de `pg_stat_replication` aux colonnes masquées.

    PostgreSQL ne laisse que `pid` aux rôles sans les privilèges de
    `pg_read_all_stats` : `application_name`, `state`, `sync_state` et les
    retards sont NULL. Une telle ligne ne dit ni l'état ni le retard ; elle
    ne doit jamais être comptée comme un secours.
    """
    return row.get("state") is None and row.get("application_name") is None


def _standbys(rows):
    """Les secours interprétables, et le nombre de lignes masquées écartées."""
    out = []
    masked = 0
    for row in rows:
        if _masked_row(row):
            masked += 1
            continue
        out.append({
            "name": row.get("application_name") or "",
            "client_addr": row.get("client_addr"),
            "state": row.get("state"),
            "sync_state": row.get("sync_state"),
            "sync_priority": row.get("sync_priority"),
            "write_lag_s": _as_seconds(row.get("write_lag_s")),
            "flush_lag_s": _as_seconds(row.get("flush_lag_s")),
            "replay_lag_s": _as_seconds(row.get("replay_lag_s")),
            "sent_lsn": row.get("sent_lsn"),
            "replay_lsn": row.get("replay_lsn"),
        })
    return out, masked


def _lag(standbys: list[dict]):
    values = [s["replay_lag_s"] for s in standbys
              if s.get("replay_lag_s") is not None]
    return max(values) if values else None


def read(cfg, db, *, patroni_reading: dict | None = None) -> dict:
    """Le statut de la base vue par cette connexion, en JSON sérialisable.

    `patroni_reading` permet au test (ou à un appelant qui l'a déjà lue) de
    fournir la vue Patroni ; None la lit quand `patroni_url` est configurée.
    """
    if getattr(getattr(db, "cfg", None), "backend", "") == "mediated":
        raise StatusUnavailable("the mediated backend does not expose pg_stat_replication")
    from . import db as db_mod
    row = _node_row(db)
    in_recovery = _as_bool(row.get("in_recovery"))
    replication_rows = _replication_rows(db)
    standbys, masked_rows = _standbys(replication_rows)
    privileged = _as_bool(row.get("read_all_stats")) or _as_bool(
        row.get("superuser"))
    if patroni_reading is None and not db_mod.is_dry_pass(db):
        patroni_reading = patroni(cfg)
    node_name = str(getattr(cfg, "host", "") or "")
    server_addr = row.get("server_addr")
    if patroni_reading:
        node_name = patroni_reading.get("name") or node_name
    elif server_addr:
        node_name = server_addr
    leaders = _leaders(patroni_reading or {})
    if leaders:
        primary = {"name": leaders[0].get("name"), "source": "patroni"}
    elif not in_recovery:
        primary = {"name": node_name, "source": "local"}
    else:
        primary = None
    threshold = _as_seconds(getattr(cfg, "db_lag_threshold_s", None))
    if threshold is None:
        threshold = DEFAULT_LAG_THRESHOLD_S
    status = {
        "schema": STATUS_SCHEMA,
        "collected_at": time.time(),
        "client": {"host": str(getattr(cfg, "host", "") or "")},
        "node": {
            "name": node_name,
            "role": "standby" if in_recovery else "primary",
            "in_recovery": in_recovery,
            "server_version": row.get("server_version"),
            "server_addr": server_addr,
            "server_port": row.get("server_port"),
            "started_at": row.get("started_at"),
            "database": row.get("database"),
            "role_name": row.get("role_name"),
            "synchronous_standby_names": row.get("synchronous_standby_names") or "",
            "synchronous_commit": row.get("synchronous_commit") or "",
        },
        "primary": primary,
        "standbys": standbys,
        "visibility": {
            "role": row.get("role_name"),
            "superuser": _as_bool(row.get("superuser")),
            "read_all_stats": _as_bool(row.get("read_all_stats")),
            "privileged": privileged,
            "masked_rows": masked_rows,
        },
        "replication": {
            "count": len(standbys),
            "masked": masked_rows,
            "sync_count": sum(1 for s in standbys
                              if s.get("sync_state") in SYNC_STATES),
            "max_lag_s": _lag(standbys),
            "threshold_s": threshold,
            "expected": 0,
        },
        "patroni": patroni_reading,
    }
    status["replication"]["expected"] = expected_standbys(cfg, status)
    return status


def _leaders(patroni_reading: dict) -> list[dict]:
    members = patroni_reading.get("members") or []
    return [m for m in members
            if str(m.get("role") or "").strip().lower() in LEADER_ROLES]


def _patroni_errors(patroni_reading: dict) -> dict:
    """Les erreurs par source Patroni (`local`, `cluster`).

    Une vue sans le détail par source (ancien format, faux minimal) est
    traitée comme deux lectures en erreur : on n'en infère rien.
    """
    errors = patroni_reading.get("errors")
    if isinstance(errors, dict):
        return {"local": errors.get("local"),
                "cluster": errors.get("cluster")}
    error = patroni_reading.get("error")
    return {"local": error, "cluster": error}


def _two_primaries(patroni_reading: dict) -> list[str]:
    """Les noms en conflit si deux nœuds peuvent écrire, sinon [].

    Deux leaders dans `/cluster` sont concluants même si `/patroni` est en
    erreur ; la comparaison du rôle local au leader DCS ne vaut que si les
    deux réponses ont conclu. Chaque preuve disponible est exploitée seule,
    sans inférer les données absentes.
    """
    errors = _patroni_errors(patroni_reading)
    leaders = _leaders(patroni_reading)
    if not errors.get("cluster") and len(leaders) > 1:
        return [str(m.get("name") or "?") for m in leaders]
    local = patroni_reading.get("local") or {}
    if (not errors.get("local") and not errors.get("cluster")
            and str(local.get("role") or "").strip().lower() in LEADER_ROLES):
        name = local.get("name")
        if leaders and leaders[0].get("name") != name:
            return [str(name or "?"), str(leaders[0].get("name") or "?")]
    return []


# --------------------------------------------------------------------------
# alertes
# --------------------------------------------------------------------------

def _alert(kind: str, value, threshold, detail: str, **extra) -> dict:
    out = {"schema": "ameesh-alert/1", "type": kind, "agent": None,
           "since": None, "value": value, "threshold": threshold,
           "detail": detail}
    out.update(extra)
    return out


def alerts_from_status(cfg, status: dict | None) -> list[dict]:
    """Les alertes `db_*` d'un statut déjà lu (aucune I/O)."""
    if not status:
        return []
    node = status.get("node") or {}
    replication = status.get("replication") or {}
    standbys = status.get("standbys") or []
    # Une sauvegarde de base (`state = backup`) tient une ligne dans
    # `pg_stat_replication` sans être un secours : elle ne compte pas.
    standbys = [s for s in standbys
                if str(s.get("state") or "").strip().lower() != "backup"]
    in_recovery = _as_bool(node.get("in_recovery"))
    visibility = status.get("visibility") or {}
    masked = int(replication.get("masked") or 0)
    out = []
    if masked:
        # Des lignes existent mais PostgreSQL en masque l'état et les retards :
        # ni secours, ni retard, ni synchrone ne peuvent être interprétés.
        # On le dit explicitement au lieu d'inventer les trois alertes.
        role = visibility.get("role") or "the status role"
        out.append(_alert(
            "db_stats_masked", masked, 0,
            "%d replication connection(s) visible but their state and lag are "
            "masked: %s lacks pg_read_all_stats (grant it, or read with the "
            "documented monitoring role)" % (masked, role),
            masked=masked, role=role))
    if not in_recovery and not masked:
        # Un secours ne voit pas les secours du primaire : ces trois alertes
        # se lisent depuis le primaire, jamais depuis un nœud qui rejoue.
        # Le deux-primaires, lui, se lit de partout (vue Patroni de grappe).
        expected = expected_standbys(cfg, status)
        if expected > 0 and len(standbys) < expected:
            out.append(_alert(
                "db_no_standby", len(standbys), expected,
                "%d connected standby/ies for at least %d expected on %s"
                % (len(standbys), expected, node.get("name") or "the primary"),
                expected=expected, connected=len(standbys)))
        threshold = _as_seconds(getattr(cfg, "db_lag_threshold_s", None))
        if threshold is None:
            threshold = _as_seconds(replication.get("threshold_s"))
        if threshold is None:
            threshold = DEFAULT_LAG_THRESHOLD_S
        lag = replication.get("max_lag_s")
        if lag is not None and lag > threshold:
            worst = max(standbys, key=lambda s: s.get("replay_lag_s") or 0.0,
                        default={})
            out.append(_alert(
                "db_replication_lag", round(float(lag), 3), threshold,
                "replay lag %.1f s on %s is over the %.1f s threshold"
                % (float(lag), worst.get("name") or "a standby", threshold),
                standby=worst.get("name")))
        names = str(node.get("synchronous_standby_names") or "").strip()
        if names and standbys and not any(
                s.get("sync_state") in SYNC_STATES for s in standbys):
            out.append(_alert(
                "db_sync_lost", 0, 1,
                "synchronous_standby_names is %r but no connected standby is "
                "sync/quorum" % _short(names, 80),
                standbys=len(standbys)))
    patroni_reading = status.get("patroni") or {}
    if patroni_reading:
        names = _two_primaries(patroni_reading)
        if names:
            out.append(_alert(
                "db_two_primaries", len(names), 1,
                "two primaries may accept writes: %s" % ", ".join(names),
                nodes=names))
    return out


def alerts(cfg, db) -> list[dict]:
    """Les alertes de grappe de la passe d'alertes : une lecture, zéro geste.

    Un backend médié n'a pas de base à observer (comme `db_capacity`) : il ne
    produit ni ne résout ces alertes, et la passe ordinaire ne tombe pas.
    """
    try:
        status = read(cfg, db)
    except StatusUnavailable:
        return []
    return alerts_from_status(cfg, status)


# --------------------------------------------------------------------------
# mise en texte
# --------------------------------------------------------------------------

def _version(status: dict) -> str:
    return str((status.get("node") or {}).get("server_version") or "?").split()[0]


def _one_standby(standby: dict) -> str:
    bits = [standby.get("name") or "?"]
    if standby.get("sync_state") in SYNC_STATES:
        bits.append("sync")
    elif standby.get("sync_state"):
        bits.append(str(standby["sync_state"]))
    if standby.get("replay_lag_s") is not None:
        bits.append("replay %.1f s" % standby["replay_lag_s"])
    return " ".join(bits)


def _patroni_text(patroni_reading: dict) -> str:
    """La vue Patroni en une phrase, chaque source d'erreur dite à part."""
    errors = _patroni_errors(patroni_reading)
    local = patroni_reading.get("local") or {}
    members = patroni_reading.get("members") or []
    leaders = _leaders(patroni_reading)
    if errors.get("local") and errors.get("cluster"):
        return "unreadable (local: %s; cluster: %s)" % (
            _short(errors["local"], 60), _short(errors["cluster"], 60))
    if errors.get("cluster"):
        return "local %s, cluster unreadable (%s)" % (
            local.get("role") or "?", _short(errors["cluster"], 60))
    if errors.get("local"):
        return "leader %s, %d member%s (local unreadable: %s)" % (
            leaders[0].get("name") if leaders else "none",
            len(members), "" if len(members) == 1 else "s",
            _short(errors["local"], 60))
    return "leader %s, %d member%s, scope %s%s" % (
        leaders[0].get("name") if leaders else "none",
        len(members), "" if len(members) == 1 else "s",
        patroni_reading.get("scope") or "?",
        ", paused" if patroni_reading.get("paused") else "")


def line(status: dict) -> str:
    """La ligne `db` du doctor, un seul paragraphe."""
    node = status.get("node") or {}
    replication = status.get("replication") or {}
    visibility = status.get("visibility") or {}
    standbys = status.get("standbys") or []
    masked = int(replication.get("masked") or 0)
    bits = ["%s (%s, PostgreSQL %s)" % (node.get("name") or "?",
                                        node.get("role") or "?", _version(status))]
    if _as_bool(node.get("in_recovery")):
        bits.append("standbys are read on the primary")
    elif standbys:
        bits.append("%d standby%s: %s" % (
            len(standbys), "" if len(standbys) == 1 else "s",
            "; ".join(_one_standby(s) for s in standbys)))
    elif masked:
        bits.append("%d walsender%s with masked statistics" % (
            masked, "" if masked == 1 else "s"))
    elif replication.get("expected"):
        bits.append("no standby (%d expected)" % replication["expected"])
    else:
        bits.append("mono-node, no standby")
    if masked:
        bits.append("grant pg_read_all_stats to %s" % (
            visibility.get("role") or "the reading role"))
    elif not visibility.get("privileged", True):
        bits.append("statistics restricted (role lacks pg_read_all_stats)")
    if replication.get("max_lag_s") is not None:
        bits.append("lag %.1f s / %.1f s" % (
            replication["max_lag_s"], replication.get("threshold_s") or 0.0))
    patroni_reading = status.get("patroni")
    if patroni_reading is None:
        bits.append("Patroni not configured")
    else:
        bits.append("Patroni " + _patroni_text(patroni_reading))
    return ", ".join(bits)


def doctor_lines(cfg, db) -> list[str]:
    try:
        status = read(cfg, db)
    except Exception as exc:  # une lecture ne casse pas le doctor
        return ["db         : not checked (%s)" % _short(exc, 120)]
    out = ["db         : %s" % line(status)]
    active = alerts_from_status(cfg, status)
    if active:
        out.append("db         : DEGRADED (%s)" % ", ".join(
            sorted({a["type"] for a in active})))
    return out


def format_lines(status: dict) -> list[str]:
    """Le texte de `ameesh db status` (une ligne par fait)."""
    node = status.get("node") or {}
    client = status.get("client") or {}
    replication = status.get("replication") or {}
    visibility = status.get("visibility") or {}
    standbys = status.get("standbys") or []
    masked = int(replication.get("masked") or 0)
    patroni_reading = status.get("patroni")
    out = ["node       : %s (%s, PostgreSQL %s, up since %s)" % (
        node.get("name") or "?", node.get("role") or "?",
        _version(status), node.get("started_at") or "?")]
    out.append("client     : %s, database %s, user %s" % (
        client.get("host") or "?", node.get("database") or "?",
        node.get("role_name") or "?"))
    if masked or not visibility.get("privileged", True):
        out.append("visibility : role %s, pg_read_all_stats %s%s" % (
            node.get("role_name") or "?",
            "yes" if visibility.get("privileged") else "no",
            ", %d masked row(s)" % masked if masked else ""))
    primary = status.get("primary")
    if primary:
        out.append("primary    : %s (%s)" % (primary.get("name") or "?",
                                             primary.get("source") or "?"))
    else:
        out.append("primary    : unknown (connected to a standby)")
    if _as_bool(node.get("in_recovery")):
        out.append("standbys   : not visible from a standby "
                   "(read them on the primary)")
    elif standbys:
        out.append("standbys   : %d connected, %d synchronous — %s" % (
            len(standbys), replication.get("sync_count") or 0,
            "; ".join(_one_standby(s) for s in standbys)))
    elif masked:
        out.append("standbys   : %d walsender(s) with masked statistics "
                   "(grant pg_read_all_stats to %s)" % (
                       masked, node.get("role_name") or "the reading role"))
    elif replication.get("expected"):
        out.append("standbys   : none (%d expected)" % replication["expected"])
    else:
        out.append("standbys   : none (mono-node)")
    lag = replication.get("max_lag_s")
    out.append("replication: lag %s / threshold %.1f s; "
               "synchronous_standby_names = %r" % (
                   "n/a" if lag is None else "%.1f s" % lag,
                   replication.get("threshold_s") or 0.0,
                   node.get("synchronous_standby_names") or ""))
    if patroni_reading is None:
        out.append("patroni    : not configured")
    else:
        out.append("patroni    : " + _patroni_text(patroni_reading))
    active = status.get("alerts") or []
    out.append("alerts     : %s" % (
        ", ".join(a["type"] for a in active) if active else "none"))
    out.append("verdict    : %s" % ("DEGRADED" if active else "OK"))
    return out


# --------------------------------------------------------------------------
# commande
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ameesh db",
        description="read the mesh database: primary, standbys, lag, Patroni")
    sub = parser.add_subparsers(dest="command")
    status_parser = sub.add_parser(
        "status", help="primary, standbys, replay lag and Patroni state")
    status_parser.add_argument("--json", action="store_true",
                               help="one JSON object (%s)" % STATUS_SCHEMA)
    args = parser.parse_args(argv)
    if args.command != "status":
        parser.print_help()
        return 2
    from . import config, db as db_mod
    cfg = config.load()
    try:
        db = db_mod.connect(cfg, writer=False)
    except db_mod.Unavailable as exc:
        print("db status: database unavailable (%s)" % _short(exc), file=sys.stderr)
        return 1
    try:
        status = read(cfg, db)
    except (db_mod.DbError, StatusUnavailable) as exc:
        print("db status: unreadable (%s)" % _short(exc), file=sys.stderr)
        return 1
    finally:
        db.close()
    status["alerts"] = alerts_from_status(cfg, status)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, sort_keys=True, default=str))
    else:
        for text in format_lines(status):
            print(text)
    return 1 if status["alerts"] else 0
