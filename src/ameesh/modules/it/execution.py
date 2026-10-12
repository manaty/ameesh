# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit, bounded execution of the delivered bytes; no shell interpolation."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import subprocess
import threading
import time

OUTPUT_BYTES = 16384


def digest(artifact):
    path = Path(artifact).resolve(strict=True)
    if not path.is_file():
        raise ValueError("artifact must be a regular file")
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return str(path), sha.hexdigest()


def execute(artifact, *, runner=None, arguments=(), timeout=60):
    if not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("execution timeout must be between 0 and 3600 seconds")
    path, sha = digest(artifact)
    # The artifact is always an argv operand, never just a claim in a note.
    command = ([runner] if runner else []) + [path] + list(arguments)
    started = time.monotonic()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=(os.name == "posix")) as process:
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
            status = process.wait(timeout=timeout)
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
    final_path, final_sha = digest(path)
    if final_path != path or final_sha != sha:
        raise ValueError("artifact changed during execution; no proof recorded")
    excerpt = output.decode("utf-8", errors="replace").strip()
    if not excerpt:
        raise ValueError("artifact execution produced no output; no proof recorded")
    return {"artifact": path, "sha256": sha, "command": command,
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
