# SPDX-License-Identifier: AGPL-3.0-only
"""#3277: an agent's temporary directory the pass does not manage, over quota.

Constat of 24/10 on pc-smichea: `.menage/tmp/codex2` held 25 GiB (a quota of
4 GiB) since 12/10 and no alert said it. Its owner file carried the key
`14c7cf1a…` — the mesh key of the same `ameesh` database reached directly
(`10.77.0.1:5432`), before the runners moved to pgbouncer (#532, 13/10).
`mesh_key` hashes the route, so the move gave the mesh a new key, every
agent a new `<agent>~<key>` directory, and the old ones read as "another
mesh's" (L219): skipped by `continue`, never measured, evicted nor alerted.

This pass covers those directories:

* any agent directory owned by another mesh key is measured; over the
  quota of this host, an `over_quota` journal line (size, biggest entries,
  owner key) raises the `tmp_over_quota` alert. It is re-written at most
  every `UNOWNED_REFRESH_S` (the alert reads 24 h), and lifted by a
  `resolved` line once under quota or a `gone` one once the directory is
  gone. Nothing is deleted there: another mesh's directory is never touched
  (L219);
* a key declared in `former_mesh_keys` (Host card, `policy.housekeeping`)
  is a former route of THIS mesh: its directories are ours, no turn writes
  in them any more (the agent's current one is `<agent>~<key>`). Each
  first-level entry then follows the ended-lot rule of the #2743 clones:
  it is removed only when its name cites lots that all exist, are all
  assigned to this agent and all ended more than `CLONE_IDLE_S` ago, when
  nothing in it moved for `CLONE_IDLE_S`, no process of the user holds it
  and it carries no Git copy. Everything else — an unattributed entry
  first — is kept, and named in the alert with its reason.

Subtrees of another user (root: wine, container leftovers) are neither
read nor removed: their bytes stay, said in the alert as "as root".
"""
from __future__ import annotations

import os
import stat

from . import housekeeping as hk
from . import housekeeping_clones as hc
from . import resources as resources_mod
from . import storage

#: entries named in the alert, biggest first
TOP = hk.TMP_TOP
#: path -> (signature, at, measure): a directory unchanged at its first level
#: is not walked again before `UNOWNED_REFRESH_S` (a pass runs every 10 min)
_MEASURES: dict = {}


def _signature(path: str):
    """The top directory and its first-level entries (name, mtime, ctime):
    a deep write that changes none of them is seen at the next refresh."""
    try:
        top = os.lstat(path)
        names = sorted(os.listdir(path))
    except OSError:
        return None
    out = [(top.st_ino, top.st_dev, top.st_mtime, top.st_ctime)]
    for name in names:
        try:
            st = os.lstat(os.path.join(path, name))
        except OSError:
            continue
        out.append((name, st.st_mtime, st.st_ctime))
    return tuple(out)


def walk(path: str, limit: int = hk.WALK_LIMIT) -> dict:
    """`scan_activity`, but a directory of ANOTHER user that this user cannot
    read is not a doubt: it is left out (`foreign`), only its own dates
    count. One of ours that cannot be read, a mount point inside or more than
    `limit` entries leave the walk incomplete (nothing is judged)."""
    out = {"bytes": 0, "latest": None, "complete": True, "why": "", "foreign": []}
    uid = os.getuid()
    try:
        top = os.lstat(path)
    except OSError as exc:
        out.update(complete=False, why="unreadable (%s)" % (exc.strerror or exc))
        return out
    latest = max(top.st_mtime, top.st_ctime)
    out["bytes"] = hk._blocks(top)
    if not stat.S_ISDIR(top.st_mode):
        out["latest"] = latest
        return out
    count = 0
    stack = [(path, top.st_uid)]
    while stack:
        current, owner = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            if owner != uid:
                out["foreign"].append(current)
                continue
            out.update(complete=False, why="unreadable directory: %s" % current)
            continue
        with it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    out.update(complete=False, why="unreadable entry: %s" % entry.path)
                    continue
                count += 1
                latest = max(latest, st.st_mtime, st.st_ctime)
                out["bytes"] += hk._blocks(st)
                if stat.S_ISDIR(st.st_mode):
                    if st.st_dev != top.st_dev:
                        out.update(complete=False, latest=latest,
                                   why="mount point inside: %s" % entry.path)
                        return out
                    stack.append((entry.path, st.st_uid))
                if count >= limit:
                    out.update(complete=False, latest=latest,
                               why="more than %d entries: activity not verifiable" % limit)
                    return out
    out["latest"] = latest
    return out


def measure(path: str, *, now: float) -> dict:
    """Size of `path` and of each first-level entry, cached while unchanged."""
    sig = _signature(path)
    cached = _MEASURES.get(path)
    if (sig is not None and cached and cached[0] == sig
            and now - cached[1] < hk.UNOWNED_REFRESH_S):
        return cached[2]
    children = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        names = []
    for name in names:
        full = os.path.join(path, name)
        children.append(dict(walk(full), name=name, path=full))
    try:
        own = hk._blocks(os.lstat(path))
    except OSError:
        own = 0
    result = {"bytes": own + sum(c["bytes"] for c in children), "children": children}
    if sig is not None:
        _MEASURES[path] = (sig, now, result)
    return result


def verdict(db, child: dict, agent: str, *, agents, now: float, holder,
            mounts=None) -> tuple[str, str, dict]:
    """('', lots, fresh) when the first-level entry `child` of a former
    directory of `agent` may be removed, else (why it is kept, lots, fresh).

    #3277 r1 (codex B1) : l'activité ne se lit jamais dans la mesure en cache
    (un écrit profond ne change pas la signature de premier niveau) : `fresh`
    est un parcours neuf, fait ici, juste avant la suppression, sous le verrou
    de l'appelant ; ses octets sont ceux qui seront comptés.
    #3277 r1 (codex B2) : `mounts`, les points de montage sous la racine
    (`housekeeping._mount_points`) ; un bind mount sur le même système de
    fichiers garde son `st_dev`. Une entrée qui est, contient ou se trouve
    sous un point de montage (le dossier de l'agent monté), ou une table
    illisible (None), n'est jamais supprimée."""
    fresh: dict = {}
    ids = hc.named_lots(child["name"], agents)
    if not ids:
        return "no lot named: unattributed, never removed automatically", "", fresh
    lots = ",".join("#%s" % ident for ident in ids)
    rows = []
    try:
        for ident in dict.fromkeys(ids):
            row = storage.of(db).work.get(ident)
            if row is None:
                return "lot #%s unknown" % ident, lots, fresh
            rows.append(row)
    except Exception:  # noqa: BLE001 - registre illisible : on garde
        return "lot state unreadable", lots, fresh
    for row in rows:
        if row.get("assignee") != agent:
            return "lot #%s is not %s's" % (row.get("id"), agent), lots, fresh
        if str(row.get("state") or "") not in hk.LOT_ENDED:
            return "lot #%s %s, not ended" % (row.get("id"), row.get("state")), lots, fresh
        ended = hc._lot_end(row)
        if ended is None:
            return "lot #%s ended but its end time is unreadable" % row.get("id"), lots, fresh
        if now - ended < hc.CLONE_IDLE_S:
            return "lot #%s ended %s ago (waits %s)" % (
                row.get("id"), _ago(now - ended), hk.fmt_idle(hc.CLONE_IDLE_S)), lots, fresh
    if mounts is None:
        return "mount table unreadable", lots, fresh
    real = os.path.realpath(child["path"])
    # #3277 r2 (codex B2bis) : un montage DANS l'entrée, mais aussi au-dessus
    # d'elle sous la racine (le dossier de l'agent lui-même monté) : son
    # contenu n'est pas à nous
    inside = sorted(m for m in mounts
                    if hk._in_subtree(m, real) or hk._in_subtree(real, m))
    if inside:
        return "mount point %s: never removed" % inside[0], lots, fresh
    fresh = dict(walk(child["path"]), name=child["name"], path=child["path"])
    if not fresh["complete"]:
        return "activity not verifiable (%s)" % fresh["why"], lots, fresh
    if fresh["latest"] is None or now - fresh["latest"] < hc.CLONE_IDLE_S:
        return "active %s ago" % _ago(now - (fresh["latest"] or now)), lots, fresh
    held = holder(child["path"])
    if held:
        return held, lots, fresh
    if os.path.isdir(child["path"]) and not os.path.islink(child["path"]):
        copies, doubt = hk._git_copies_report(child["path"], skip=fresh["foreign"])
        if doubt:
            return "git copies not fully inspected (%s)" % doubt, lots, fresh
        if copies:
            return "git copy inside (%s); inspect with: %s" % (
                copies[0], hk.tmp_recovery_command(child["path"])), lots, fresh
    return "", lots, fresh


def _ago(seconds: float) -> str:
    """« 11 d », « 5 h », « 40 min » : l'âge d'une entrée abandonnée."""
    seconds = max(0.0, float(seconds))
    if seconds >= 2 * 86400:
        return "%d d" % (seconds // 86400)
    if seconds >= 3600:
        return "%d h" % (seconds // 3600)
    return hk.fmt_idle(seconds)


def _top(children, kept: dict) -> list[dict]:
    out = []
    for child in sorted(children, key=lambda c: -c["bytes"])[:TOP]:
        row = {"name": child["name"], "bytes": child["bytes"]}
        if child["name"] in kept:
            row["kept"] = kept[child["name"]]
        if child.get("foreign"):
            row["foreign"] = len(child["foreign"])
        out.append(row)
    return out


def describe(top) -> str:
    """The biggest entries, for the alert: `diag462 25.0 GiB (kept: …)`."""
    parts = []
    for row in top or ():
        text = "%s %s" % (row.get("name"), resources_mod.fmt_value("q", row.get("bytes") or 0))
        if row.get("kept"):
            text += " (kept: %s)" % row["kept"]
        if row.get("foreign"):
            text += " (%d subtree(s) of another user, to remove as their owner)" % row["foreign"]
        parts.append(text)
    return "; ".join(parts)


def check(db, *, troot: str, name: str, agent: str, owner: str, key: str,
          policy, now: float, dry_run: bool, previous: dict | None, agents,
          holder) -> dict:
    """One directory `troot/name` whose owner file names `owner` (not `key`).

    Returns `entries` (journal), `plan` (dry run) and `skipped`."""
    full = os.path.join(troot, name)
    # mesh-design 24/10 : une clé n'est ancienne que sur l'hôte dont la fiche
    # la déclare (`effective(canons, host)`), et jamais la clé courante
    former = owner != key and owner in policy.former_mesh_keys
    entries: list[dict] = []
    plan: list[dict] = []
    skipped: list[str] = []
    found = measure(full, now=now)
    kept: dict = {}
    freed = 0
    if former and found["bytes"] > policy.tmp_quota:
        lock = None if dry_run else hk.tmp_lock(troot, name, exclusive=True)
        if not dry_run and lock is None:
            skipped.append("tmp %s kept: in turn (lock held)" % full)
            kept = {c["name"]: "in turn (lock held)" for c in found["children"]}
        else:
            try:
                mounts = hk._mount_points(troot)
                for child in found["children"]:
                    why, lots, fresh = verdict(db, child, agent, agents=agents, now=now,
                                               holder=holder, mounts=mounts)
                    if why:
                        kept[child["name"]] = why
                        continue
                    child = fresh
                    detail = ("former key %s of this mesh: lot %s ended, idle %s"
                              % (owner, lots, _ago(now - child["latest"])))
                    plan.append({"kind": "tmp", "path": child["path"], "agent": agent,
                                 "action": "suppression", "bytes": child["bytes"],
                                 "detail": detail})
                    if dry_run:
                        continue
                    # #3277 r3 (codex cx1 B1) : `os.walk` suit un chemin de
                    # départ qui est un lien, même `followlinks=False` : un
                    # lien n'est jamais ouvert en écriture, seul le lien part
                    if not os.path.islink(child["path"]):
                        hk._make_owned_dirs_writable(child["path"])
                    try:
                        hk._safe_rmtree(child["path"], troot)
                    except (OSError, hk.RefusedDeletion) as exc:
                        kept[child["name"]] = "deletion refused (%s)" % exc
                        continue
                    left = hk.size_of(child["path"]) if os.path.lexists(child["path"]) else 0
                    freed += child["bytes"] - left
                    if left:
                        kept[child["name"]] = "%s of another user left" % (
                            resources_mod.fmt_value("q", left))
                    entries.append({"kind": "tmp", "action": "deleted",
                                    "path": child["path"], "agent": agent,
                                    "lot": lots.replace("#", ""),
                                    "bytes": child["bytes"] - left, "detail": detail})
            finally:
                if lock is not None:
                    lock.close()
        if freed:
            _MEASURES.pop(full, None)
            found = measure(full, now=now)
    size = found["bytes"]
    was_over = (previous or {}).get("action") == "over_quota"
    if size > policy.tmp_quota:
        data = ((previous or {}).get("data") or {}) if was_over else {}
        top = _top(found["children"], kept)
        due = (not was_over or freed
               or now - float((previous or {}).get("at_ts") or 0) >= hk.UNOWNED_REFRESH_S)
        if due and not dry_run:
            entries.append({
                "kind": "tmp", "action": "over_quota", "path": full, "agent": agent,
                "bytes": size,
                "detail": "quota %s exceeded; owner key %s (%s): %s" % (
                    resources_mod.fmt_value("q", policy.tmp_quota), owner,
                    "former route of this mesh" if former else
                    "another mesh, or a former route of this one to declare in "
                    "`former_mesh_keys`", describe(top)),
                "data": {"quota": policy.tmp_quota, "owner": owner, "former": former,
                         "top": top, "first_seen": data.get("first_seen") or now}})
        if dry_run:
            plan.append({"kind": "tmp", "path": full, "agent": agent, "action": "alerte",
                         "bytes": size, "detail": "over quota %s: %s" % (
                             resources_mod.fmt_value("q", policy.tmp_quota), describe(top))})
    elif was_over and not dry_run:
        entries.append({"kind": "tmp", "action": "resolved", "path": full, "agent": agent,
                        "bytes": size, "data": {"quota": policy.tmp_quota},
                        "detail": "quota %s holds again; the `over_quota` episode is over"
                                  % resources_mod.fmt_value("q", policy.tmp_quota)})
    return {"entries": entries, "plan": plan, "skipped": skipped}


def clear_cache() -> None:
    _MEASURES.clear()

