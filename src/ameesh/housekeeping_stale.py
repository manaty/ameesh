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
  in them any more (the agent's current one is `<agent>~<key>`). A whole
  directory left without any activity for `UNOWNED_IDLE_S` (24 h) is
  removed as a temporary directory whose session is over -- under the same
  guards as any removal (lock, mount table, processes, Git copies). Before
  that, and as long as the directory is still busy or protected, each
  first-level entry may follow the ended-lot rule of the #2743 clones when
  the directory is over quota: it is removed only when its name cites lots
  that all exist, are all assigned to this agent and all ended more than
  `CLONE_IDLE_S` ago, when nothing in it moved for `CLONE_IDLE_S`, no
  process of the user holds it and it carries no Git copy. Everything else —
  an unattributed entry first — is kept, and named in the alert with its
  reason.

Subtrees of another user (root: wine, container leftovers) are neither
read nor removed: their bytes stay, said in the alert as "as root".
"""
from __future__ import annotations

import os
import re
import stat

from . import build_artifacts
from . import housekeeping as hk
from . import housekeeping_clones as hc
from . import resources as resources_mod
from . import storage

#: entries named in the alert, biggest first
TOP = hk.TMP_TOP
#: path -> (signature, at, measure): a directory unchanged at its first level
#: is not walked again before `UNOWNED_REFRESH_S` (a pass runs every 10 min)
_MEASURES: dict = {}
#: `prevalidation_calls.PLAN_NAME`: the L161f plan is written at the top of
#: the candidate tree for the offload, then left there by a close that did not
#: happen. It is generated, never user work (#6258).
_GENERATED_PLAN = re.compile(r"^ameesh-prevalidation-plan(?:-\d+)?\.json$")


def _drop_generated_plan(path: str) -> None:
    """Remove the untracked L161f plan left at the top of a candidate tree.
    A tracked file with that name belongs to the project: it stays."""
    try:
        names = os.listdir(path)
    except OSError:
        return
    for name in names:
        if not _GENERATED_PLAN.match(name):
            continue
        full = os.path.join(path, name)
        if os.path.islink(full) or not os.path.isfile(full):
            continue
        tracked = hk._git(["ls-files", "--error-unmatch", "--", name], path)
        if tracked is None or tracked.returncode == 0:
            continue
        try:
            os.unlink(full)
        except OSError:
            pass


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


def walk(path: str, limit: int = hk.WALK_LIMIT, now: float | None = None) -> dict:
    """`scan_activity`, but a directory of ANOTHER user that this user cannot
    read is not a doubt: it is left out (`foreign`), only its own dates
    count. One of ours that cannot be read, a mount point inside or more than
    `limit` entries leave the walk incomplete (nothing is judged). A future
    mtime is ignored (#6258), exactly as in `scan_activity`."""
    import time
    now = time.time() if now is None else float(now)
    out = {"bytes": 0, "latest": None, "complete": True, "why": "", "foreign": []}
    uid = os.getuid()
    try:
        top = os.lstat(path)
    except OSError as exc:
        out.update(complete=False, why="unreadable (%s)" % (exc.strerror or exc))
        return out
    latest = max(top.st_ctime, top.st_mtime if top.st_mtime <= now else 0.0)
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
                latest = max(latest, st.st_ctime,
                             st.st_mtime if st.st_mtime <= now else 0.0)
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
        children.append(dict(walk(full, now=now), name=name, path=full))
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
    fresh = dict(walk(child["path"], now=now), name=child["name"], path=child["path"])
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


def folder_verdict(path: str, *, now: float, holder, mounts) -> tuple[str, dict]:
    """('', fresh) when the whole former-key directory `path` may be cleaned,
    else (why it is kept, fresh).

    A directory under a former key of this mesh receives no turn any more.
    After `UNOWNED_IDLE_S` without activity it is the temporary directory of
    a finished session: its safe entries may go, under the same guards as any
    removal -- a readable mount table, no mount point at or above it, a
    complete fresh walk, and no process of the user inside. Git copies are
    judged entry by entry by `clear_entries`, never here."""
    if mounts is None:
        return "mount table unreadable", {}
    real = os.path.realpath(path)
    inside = sorted(m for m in mounts
                    if hk._in_subtree(m, real) or hk._in_subtree(real, m))
    if inside:
        return "mount point %s: never removed" % inside[0], {}
    fresh = dict(walk(path, now=now))
    if not fresh["complete"]:
        return "activity not verifiable (%s)" % fresh["why"], fresh
    if fresh["latest"] is None or now - fresh["latest"] < hk.UNOWNED_IDLE_S:
        return "active %s ago (less than %s)" % (
            _ago(now - (fresh["latest"] or now)), hk.fmt_idle(hk.UNOWNED_IDLE_S)), fresh
    held = holder(path)
    if held:
        return held, fresh
    return "", fresh


def clear_entries(found: dict, *, troot: str, policy, dry_run: bool = False) -> dict:
    """Remove the safe first-level entries of an idle former-key directory.

    A Git copy is recovered only when `tmp_git_work` proves its work saved
    (clean and pushed or integrated, no ignored file outside the build
    output -- `__pycache__` is the one regenerable extra -- no holder); the
    generated L161f plan left in a prevalidation tree is not work and is
    dropped first; the removal itself never uses `--force`. Anything
    doubtful, or a subtree of another user that survives the deletion, is
    kept with its reason. Returns `{freed, left, kept, removed}`; `freed` is
    what would go in a dry run."""
    rules = tuple(build_artifacts.rules_of(policy.artifacts)) + (
        build_artifacts.Rule("__pycache__"),)
    kept: dict = {}
    freed = 0
    left_total = 0
    removed = 0
    for child in found.get("children") or ():
        name, path = child["name"], child["path"]
        if not dry_run and os.path.isdir(path) and not os.path.islink(path):
            _drop_generated_plan(path)
        if os.path.isdir(path) and not os.path.islink(path):
            copies, doubt = hk._git_copies_report(path)
            if doubt:
                kept[name] = "git copies not fully inspected (%s)" % doubt
                continue
            if copies:
                if not dry_run:
                    for copy in copies:
                        _drop_generated_plan(copy)
                work = hk.tmp_git_work(path, rules, report=(copies, doubt))
                if work:
                    kept[name] = work
                    continue
                if not dry_run:
                    ok, error, repos = hk.recover_tmp_git_copies(
                        path, report=(copies, doubt))
                    if not ok:
                        kept[name] = "git worktree removal refused — %s" % error
                        continue
                    for repo in repos:
                        hk.prune_worktrees(repo)
        if dry_run:
            freed += child["bytes"]
            removed += 1
            continue
        if not os.path.islink(path):
            # #3277 r3 (codex cx1 B1) : un lien n'est jamais ouvert en
            # écriture, seul le lien part
            hk._make_owned_dirs_writable(path)
        try:
            hk._safe_rmtree(path, troot)
        except (OSError, hk.RefusedDeletion) as exc:
            left = hk.size_of(path) if os.path.lexists(path) else 0
            if left:
                left_total += left
            kept[name] = "deletion refused (%s)" % exc
            continue
        left = hk.size_of(path) if os.path.lexists(path) else 0
        if left:
            left_total += left
            kept[name] = "%s of another user left" % resources_mod.fmt_value("q", left)
        if not os.path.lexists(path):
            removed += 1
        if left < child["bytes"]:
            freed += child["bytes"] - left
    return {"freed": freed, "left": left_total, "kept": kept, "removed": removed}


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
    whole_dir = False
    if former:
        lock = None if dry_run else hk.tmp_lock(troot, name, exclusive=True)
        if not dry_run and lock is None:
            skipped.append("tmp %s kept: in turn (lock held)" % full)
            kept = {c["name"]: "in turn (lock held)" for c in found["children"]}
        else:
            try:
                if not dry_run and hk.read_owner(troot, name) != owner:
                    skipped.append("tmp %s kept: owner changed during the pass" % full)
                else:
                    mounts = hk._mount_points(troot)
                    why, fresh = folder_verdict(full, now=now, holder=holder,
                                                mounts=mounts)
                    if not why:
                        # A former route writes nowhere any more: once the
                        # whole directory has been idle for a day, every safe
                        # entry goes. This is what frees the thousands of
                        # unattributed scratch entries the ended-lot rule
                        # keeps (#6258).
                        whole_dir = True
                        cleared = clear_entries(found, troot=troot, policy=policy,
                                                dry_run=dry_run)
                        freed += cleared["freed"]
                        kept = cleared["kept"]
                        detail = ("former key %s of this mesh: no turn writes here "
                                  "any more; idle %s, %d safe entry(ies) removed"
                                  % (owner, _ago(now - fresh["latest"]),
                                     cleared["removed"]))
                        if kept:
                            detail += "; kept: %s" % ", ".join(
                                "%s (%s)" % (kept_name, kept_why)
                                for kept_name, kept_why in sorted(kept.items()))
                        plan.append({"kind": "tmp", "path": full, "agent": agent,
                                     "action": "suppression", "bytes": freed,
                                     "detail": detail})
                        if not dry_run:
                            if cleared["removed"] or freed:
                                entries.append({"kind": "tmp", "action": "deleted",
                                                "path": full, "agent": agent,
                                                "bytes": freed, "detail": detail})
                            if cleared["left"]:
                                entries.append({
                                    "kind": "tmp", "action": "unremovable",
                                    "path": full, "agent": agent,
                                    "bytes": cleared["left"],
                                    "detail": "former key %s: %s left in place "
                                              "(another user, often root: container "
                                              "or build leftovers); nothing more can "
                                              "be removed here"
                                              % (owner, resources_mod.fmt_value(
                                                  "q", cleared["left"])),
                                    "data": {"quota": policy.tmp_quota,
                                             "unremovable_bytes": cleared["left"],
                                             # the over-quota alert already names
                                             # this directory; #530: the bytes are
                                             # reported without a second alert.
                                             "blocked": False,
                                             "episode_since": now}})
                            if not kept and not cleared["left"]:
                                try:
                                    os.rmdir(full)
                                except OSError as exc:
                                    kept["(directory)"] = "directory not empty (%s)" % (
                                        exc.strerror or exc)
                            if not kept:
                                for leftover in (hk.owner_file(troot, name),
                                                 hk.lock_file(troot, name)):
                                    try:
                                        os.unlink(leftover)
                                    except OSError:
                                        pass
                    elif found["bytes"] > policy.tmp_quota:
                        # Busy or protected, but over quota: the ended-lot
                        # cleanup of #3277 still frees what is proven, without
                        # waiting for the whole directory to become idle.
                        for child in found["children"]:
                            why_child, lots, fresh_child = verdict(
                                db, child, agent, agents=agents, now=now,
                                holder=holder, mounts=mounts)
                            if why_child:
                                kept[child["name"]] = why_child
                                continue
                            child = fresh_child
                            detail = ("former key %s of this mesh: lot %s ended, "
                                      "idle %s" % (owner, lots,
                                                   _ago(now - child["latest"])))
                            plan.append({"kind": "tmp", "path": child["path"],
                                         "agent": agent, "action": "suppression",
                                         "bytes": child["bytes"], "detail": detail})
                            if dry_run:
                                continue
                            if not os.path.islink(child["path"]):
                                hk._make_owned_dirs_writable(child["path"])
                            try:
                                hk._safe_rmtree(child["path"], troot)
                            except (OSError, hk.RefusedDeletion) as exc:
                                kept[child["name"]] = "deletion refused (%s)" % exc
                                continue
                            child_left = (hk.size_of(child["path"])
                                          if os.path.lexists(child["path"]) else 0)
                            freed += child["bytes"] - child_left
                            if child_left:
                                kept[child["name"]] = "%s of another user left" % (
                                    resources_mod.fmt_value("q", child_left))
                            entries.append({"kind": "tmp", "action": "deleted",
                                            "path": child["path"], "agent": agent,
                                            "lot": lots.replace("#", ""),
                                            "bytes": child["bytes"] - child_left,
                                            "detail": detail})
                    else:
                        skipped.append("tmp %s kept: %s" % (full, why))
            finally:
                if lock is not None:
                    lock.close()
        if freed and not dry_run:
            _MEASURES.pop(full, None)
            found = measure(full, now=now)
    size = found["bytes"]
    was_over = (previous or {}).get("action") == "over_quota"
    if whole_dir and dry_run:
        # the plan already says what would go; no separate alert
        pass
    elif size > policy.tmp_quota:
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
                    "former route of this mesh: the whole directory is cleaned after "
                    "%s without activity, the entries of ended lots before that" % (
                        hk.fmt_idle(hk.UNOWNED_IDLE_S))
                    if former else
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

