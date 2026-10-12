# SPDX-License-Identifier: AGPL-3.0-only
"""Isolated proof producer: request execution, never submit evidence fields."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

HELPER = "/usr/local/libexec/ameesh-it-proof"
CONFIG_ROOT = "/etc/ameesh-it-proof"
MAX_REQUEST = 65536


def request(project, lot, artifact, *, runner=None, arguments=(), timeout=60):
    runner_path = shutil.which(runner) if runner else None
    if runner and runner_path is None:
        raise ValueError("runner executable not found")
    payload = dict(project=project, lot=int(lot), artifact=str(Path(artifact).absolute()),
                   runner=runner_path, arguments=list(arguments), timeout=timeout)
    try:
        result = subprocess.run(["sudo", "-n", HELPER],
                                input=json.dumps(payload), capture_output=True, text=True,
                                timeout=float(timeout) + 30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("isolated IT proof helper unavailable: %s" % exc) from exc
    if result.returncode:
        raise ValueError("isolated IT proof helper failed: %s" % result.stderr.strip()[:500])
    if len(result.stdout) > MAX_REQUEST:
        raise ValueError("proof helper response too large")
    response = json.loads(result.stdout)
    if "error" in response:
        raise ValueError(response["error"])
    return response["milestone"]


def produce(db, payload, *, actor, execution_user, allowed_runners=(), snapshot_root=None):
    from . import item, prove
    if not isinstance(payload, dict) or set(payload) != {
            "project", "lot", "artifact", "runner", "arguments", "timeout"}:
        raise ValueError("proof requests accept execution inputs only, never evidence")
    if not isinstance(payload["artifact"], str) or not Path(payload["artifact"]).is_absolute():
        raise ValueError("artifact must be an absolute path")
    if payload["runner"] is not None and (not isinstance(payload["runner"], str)
                                          or not Path(payload["runner"]).is_absolute()):
        raise ValueError("runner must be an absolute executable path")
    if payload["runner"] is not None and payload["runner"] not in allowed_runners:
        raise ValueError("runner is not approved in this project's private proof configuration")
    if not isinstance(payload["arguments"], list) or any(
            not isinstance(arg, str) for arg in payload["arguments"]):
        raise ValueError("arguments must be strings")
    if not isinstance(payload["lot"], int) or isinstance(payload["lot"], bool):
        raise ValueError("lot must be an integer")
    row = item(db, payload["lot"])
    if (row.get("app") or row.get("workstream") or "") != payload["project"]:
        raise ValueError("proof helper project does not own this lot")
    return prove(db, payload["lot"], payload["artifact"], actor=actor,
                 runner=payload["runner"], arguments=payload["arguments"],
                 timeout=float(payload["timeout"]), execution_user=execution_user, snapshot_root=snapshot_root)


def private_path(path, *, directory=False):
    """Root-owned, unmodifiable path: service config/code must be isolated."""
    path = Path(path).absolute()
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("service path must be root-owned and unmodifiable: %s" % parent)
    info = path.stat()
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError("socket parent must be a directory")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077):
        raise ValueError("service config must be a root-owned private regular file")
    return str(path)



def main():
    from ... import config, db as db_mod, modules
    import pwd
    if os.geteuid() != 0:
        raise ValueError("proof helper requires root to drop artifact execution privileges")
    raw = sys.stdin.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        raise ValueError("proof request too large")
    payload = json.loads(raw)
    project = payload.get("project") if isinstance(payload, dict) else None
    if not isinstance(project, str) or not modules.NAME.fullmatch(project):
        raise ValueError("invalid proof project")
    config_path = private_path(Path(CONFIG_ROOT, project + ".json"))
    settings = json.loads(Path(config_path).read_text())
    execution_user = settings["execution_user"]
    if pwd.getpwnam(execution_user).pw_uid == 0:
        raise ValueError("execution user must be unprivileged")
    snapshot_root = private_path(settings.get("snapshot_root", "/var/lib/ameesh-it-proof"), directory=True)
    cfg = config.load(env={"AMEESH_CONFIG": config_path})
    db = db_mod.connect(cfg)
    try:
        rows = db.query(
            "SELECT r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolbypassrls "
            "OR has_table_privilege(current_user, 'project_modules', 'INSERT,UPDATE,DELETE') "
            "AS unsafe, EXISTS (SELECT 1 FROM pg_roles p WHERE p.rolname = 'ameesh_it_proof' "
            "AND pg_has_role(current_user, p.oid, 'USAGE')) AS writer "
            "FROM pg_roles r WHERE r.rolname = current_user")
        if not rows or rows[0]["unsafe"] or not rows[0]["writer"]:
            raise ValueError("helper requires the isolated nonadministrative IT proof database role")
        result = produce(db, payload, actor="uid:%s" % os.environ.get("SUDO_UID", "0"),
                         execution_user=execution_user, allowed_runners=settings.get("runners", []),
                         snapshot_root=snapshot_root)
        print(json.dumps({"milestone": result}, default=str))
    finally:
        db.close()
    return 0
