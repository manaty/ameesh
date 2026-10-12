# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit, bounded execution of the delivered bytes; no shell interpolation."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import subprocess
import stat
import tempfile
from contextlib import contextmanager, ExitStack
import threading
import time

OUTPUT_BYTES = 16384


def open_artifact(artifact, *, reader=None):
    path = Path(artifact).absolute()
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        if reader is None:
            fd = os.open(path, flags)
        else:
            # One-shot helper, before its output-reader thread exists: open
            # only files the worker can read, then restore producer credentials.
            uid, gid, groups = os.geteuid(), os.getegid(), os.getgroups()
            try:
                os.setgroups([])
                os.setegid(reader.pw_gid)
                os.seteuid(reader.pw_uid)
                fd = os.open(path, flags)
            finally:
                os.seteuid(uid)
                os.setegid(gid)
                os.setgroups(groups)
    except OSError as exc:
        raise ValueError("artifact must be a readable regular file without symlink substitution") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("artifact must be a regular file")
    return str(path), os.fdopen(fd, "rb")


def digest(artifact):
    path, stream = open_artifact(artifact)
    sha = hashlib.sha256()
    with stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return path, sha.hexdigest()


@contextmanager
def snapshot(artifact, *, root=None, reader=None, limits=None, deadline=None):
    from . import snapshot_limits as budget
    limits = budget.settings(limits)
    deadline = deadline if deadline is not None else time.monotonic() + limits["copy_seconds"]
    with ExitStack() as cleanup:
        # Non-authoritative library calls use a disposable private store.
        # Installed helpers always provide their shared protected on-disk root.
        if root is None:
            root = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="snapshot-store-"))
        with budget.copy_timer(deadline), budget.store_lock(root, deadline):
            path, stream = open_artifact(artifact, reader=reader)
            cleanup.enter_context(stream)
            size = os.fstat(stream.fileno()).st_size
            if size > limits["max_bytes"]:
                raise ValueError("artifact snapshot size limit exceeded; nothing copied")
            used = budget.used_bytes(root)
            budget.check_storage(root, used, 0, limits, incoming=size)
            folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="execution-", dir=root))
            copied = Path(folder, Path(path).name)
            sha = hashlib.sha256()
            count = 0
            with copied.open("xb") as target:
                while True:
                    budget.check_time(deadline)
                    block = stream.read(min(1024 * 1024, limits["max_bytes"] - count + 1))
                    budget.check_time(deadline)
                    if not block:
                        break
                    if count + len(block) > limits["max_bytes"]:
                        raise ValueError("artifact snapshot copied-byte limit exceeded; no proof recorded")
                    budget.check_storage(root, used, count, limits, incoming=len(block))
                    target.write(block)
                    target.flush()
                    count += len(block)
                    sha.update(block)
                    budget.check_time(deadline)
                    budget.check_storage(root, used, count, limits)
            budget.check_time(deadline)
            budget.check_storage(root, used, count, limits)
            os.chmod(copied, 0o555)
        yield path, str(copied), sha.hexdigest()


def execute(artifact, *, runner=None, arguments=(), timeout=60, execution_user=None,
            snapshot_root=None, expected_sha=None, snapshot_limits=None):
    if not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("execution timeout must be between 0 and 3600 seconds")
    started = time.monotonic()
    from .snapshot_limits import settings
    limits = settings(snapshot_limits)
    copy_deadline = started + min(timeout, limits["copy_seconds"])
    isolation = {}
    account = None
    if execution_user is not None:
        import pwd
        account = pwd.getpwnam(execution_user)
        if os.name != "posix" or os.geteuid() != 0 or account.pw_uid == 0:
            raise ValueError("proof worker requires root service and a non-root execution user")
        if snapshot_root is None:
            raise ValueError("isolated proof execution requires a protected snapshot root")
        from .proof_service import private_path
        snapshot_root = private_path(snapshot_root, directory=True)
        isolation = dict(user=account.pw_uid, group=account.pw_gid, extra_groups=[],
                         cwd="/", env={"PATH": "/usr/bin:/bin", "HOME": account.pw_dir,
                                       "LANG": "C.UTF-8"}, umask=0o077)
    with snapshot(artifact, root=snapshot_root, reader=account, limits=limits,
                  deadline=copy_deadline) as (source, path, sha):
        if expected_sha is not None and sha != expected_sha:
            raise ValueError("artifact differs from the declared delivery; nothing executed")
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ValueError("artifact execution timed out before launch; no proof recorded")
        # Keep the copy private until its digest was admitted.
        os.chmod(Path(path).parent, 0o755)  # worker traversal, never worker write
        command = ([runner] if runner else []) + [path] + list(arguments)
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              start_new_session=(os.name == "posix"), **isolation) as process:
            output = bytearray()

            def drain():
                while True:
                    block = process.stdout.read(4096)
                    if not block:
                        break
                    output.extend(block[:max(0, OUTPUT_BYTES - len(output))])

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                status = process.wait(timeout=max(0, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                _kill(process)
                raise ValueError("artifact execution timed out; no proof recorded")
            finally:
                reader.join(timeout=1)
                if reader.is_alive():
                    _kill(process)
                    reader.join(timeout=1)
            if reader.is_alive():
                raise ValueError("artifact output did not close; no proof recorded")
        if status != 0:
            raise ValueError("artifact execution failed (exit %s); no proof recorded" % status)
        _, final_sha = digest(path)
        if final_sha != sha:
            raise ValueError("artifact changed during execution; no proof recorded")
        excerpt = output.decode("utf-8", errors="replace").strip()
        if not excerpt:
            raise ValueError("artifact execution produced no output; no proof recorded")
        return {"artifact": source, "executed_artifact": path, "sha256": sha, "command": command,
                "exit_code": status, "output": excerpt,
                "duration_seconds": round(time.monotonic() - started, 3)}


def _kill(process):
    if os.name == "posix":
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()
    process.wait()
