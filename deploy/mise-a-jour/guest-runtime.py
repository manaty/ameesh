# SPDX-License-Identifier: AGPL-3.0-only
"""#4793: refresh an existing guest's root-owned runtime from a staging tree.

No provisioning: keys, profiles, users, firewall and offload.json stay intact.
Sent on stdin to the administrator's Python, also for rollback to older trees.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


FILES = (
    ("src/ameesh/platform/offload_shell.py", "ameesh-offload-shell", 0o755),
    ("deploy/guest/job-postgres.sh", "job-postgres", 0o755),
    ("deploy/guest/ameesh-job-launcher", "ameesh-job-launcher", 0o755),
    ("deploy/guest/ameesh-job-launcher.py", "ameesh-job-launcher.py", 0o644),
)


def trusted(path, *, owner=0, directory=False):
    st = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(st.st_mode) or st.st_uid != owner or st.st_mode & 0o022:
        raise ValueError("guest runtime: unsafe installed path %s" % path)
    return st


def replace(path, data, mode):
    """The old executable stays complete until the rename; no in-place write."""
    fd, name = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def refresh(source, *, check=False, lib=Path("/usr/local/lib/ameesh-guest"),
            config=Path("/etc/ameesh-guest/offload.json"), owner=0):
    # A host without the guest is deliberately not provisioned by staging.
    if not os.path.lexists(lib) and not os.path.lexists(config):
        return {"guest": "absent", "files": []}
    trusted(lib, owner=owner, directory=True)
    selected = FILES if any(os.path.lexists(lib / f[1]) for f in FILES[2:]) else FILES[:2]
    prepared = []
    for relative, name, mode in selected:
        target = lib / name
        st = trusted(target, owner=owner)
        origin = source / relative
        if not stat.S_ISREG(origin.lstat().st_mode):
            raise ValueError("guest runtime: source is not a regular file: %s" % origin)
        data = origin.read_bytes()
        old = target.read_bytes()
        prepared.append((target, data, mode, old, stat.S_IMODE(st.st_mode)))
    changed = []
    try:
        for target, data, mode, old, old_mode in prepared:
            if old != data or old_mode != mode:
                if check:
                    raise ValueError("guest runtime differs from staging tree: %s" % target)
                replace(target, data, mode)
                changed.append((target, old, old_mode))
        rows = []
        for target, data, mode, _old, _old_mode in prepared:
            st = trusted(target, owner=owner)
            actual = target.read_bytes()
            if actual != data or stat.S_IMODE(st.st_mode) != mode:
                raise ValueError("guest runtime verification failed: %s" % target)
            rows.append({"name": target.name, "sha256": hashlib.sha256(actual).hexdigest()})
    except BaseException:
        for target, old, old_mode in reversed(changed):
            replace(target, old, old_mode)
        raise
    return {"guest": "verified", "changed": len(changed), "files": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    # A normal host without a guest can be checked without elevation.
    if os.path.lexists("/usr/local/lib/ameesh-guest") and os.geteuid() != 0:
        parser.error("administrator required to verify/update the guest runtime")
    try:
        print(json.dumps(refresh(args.source, check=args.check), sort_keys=True))
    except (OSError, ValueError) as exc:
        parser.exit(1, "%s\n" % exc)


if __name__ == "__main__":
    main()
