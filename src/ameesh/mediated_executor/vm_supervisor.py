# SPDX-License-Identifier: AGPL-3.0-only
"""VM-side rootless supervisor. Activation belongs to the host operator."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import threading
import time

from ..guard import read_guest_json
from ..host_gate import parse_state
from .vm_recipe import command


NAME = "ameesh-vm-executor"


def directory(path: str) -> int:
    """Open each component without following symlinks, including parents."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in Path(path).parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def remove_code(fd: int) -> None:
    try:
        os.unlink("code", dir_fd=fd)
    except FileNotFoundError:
        pass


def wipe(fd: int) -> None:
    """Keep the mount root; unlink symlinks rather than following them."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("safe directory removal is unavailable")
    for name in os.listdir(fd):
        if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
            shutil.rmtree(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


def gate_state(path: str, now: float) -> str:
    try:
        state = parse_state(read_guest_json(path), now=now, lending=True)
        if state.until_ts is not None and state.until_ts <= now:
            return "stopped"
        return state.state
    except (TypeError, ValueError, OverflowError):
        return "stopped"


def supervise(recipe: dict, stop: threading.Event, *, launch=subprocess.Popen,
              run=subprocess.run, now=time.time, log=print) -> int:
    argv = command(recipe)
    if os.geteuid() == 0:
        raise ValueError("supervisor requires the dedicated non-root guest identity")
    # The owner provisions these directories on a dedicated volume. Keep
    # descriptors open for destructive operations even if paths are replaced.
    with ExitStack() as stack:
        fds = {}
        for key in ("volume", "state_dir", "ack_dir", "enroll_dir"):
            fd = directory(recipe[key])
            stack.callback(os.close, fd)
            fds[key] = fd
        # A lock outside the volume survives a revocation wipe.
        lock = os.open("vm-supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=fds["enroll_dir"])
        stack.callback(os.close, lock)
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError("supervisor lock must be a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        delay = 30
        state_path = str(Path(recipe["state_dir"]) / "state.json")
        ack_path = str(Path(recipe["ack_dir"]) / "ack.json")
        while not stop.is_set():
            # Never start during withdrawal or with a missing/stale guard.
            if gate_state(state_path, now()) != "available":
                stop.wait(1)
                continue
            # The first ack must be produced by this launch. The guest has
            # exclusive use of ack_dir; an older launch must be stopped first.
            try:
                os.unlink("ack.json", dir_fd=fds["ack_dir"])
            except FileNotFoundError:
                pass
            process = None
            stopping = False
            try:
                process = launch(argv)
                while process.poll() is None:
                    ack = read_guest_json(ack_path)
                    if ack and ack.get("schema") == "ameesh-host-ack/1":
                        # The image writes its ack only after enrollment and
                        # volume identity checks, when the runner is started.
                        remove_code(fds["enroll_dir"])
                    if stop.is_set() or gate_state(state_path, now()) == "stopped":
                        stopping = True
                        result = run(["podman", "stop", "--ignore", "--time=100", NAME],
                                     timeout=110, check=False)
                        if result.returncode:
                            raise RuntimeError("Podman could not stop the executor")
                        process.wait(timeout=10)
                        break
                    stop.wait(1)
                rc = process.wait()
            finally:
                # Also erase an unused/refused code and a code with no ack.
                try:
                    remove_code(fds["enroll_dir"])
                finally:
                    if process is not None and process.poll() is None:
                        run(["podman", "kill", NAME], timeout=10, check=False)
                        process.wait(timeout=10)
            log("VM executor exited: %s" % rc)
            if rc == 6:
                wipe(fds["volume"])
                return 6
            if stopping:
                return 0
            if rc != 1:
                # Podman errors (125..127), unexpected exits and signals are
                # never treated as a retryable executor failure.
                return rc if 0 <= rc <= 255 else 2
            if stop.wait(delay):
                return 0
            delay = min(600, delay * 2)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe")
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: stop.set())
    try:
        with open(args.recipe, encoding="utf-8") as fh:
            recipe = json.load(fh)
        return supervise(recipe, stop)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print("VM supervisor failed: %s" % type(exc).__name__, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
