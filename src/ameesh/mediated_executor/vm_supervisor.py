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
import sys
import threading
import time

from ..guard import read_guest_json
from ..host_gate import parse_state
from .vm_recipe import command


NAME = "ameesh-vm-executor"
#: #1781 N3 : `shutil.rmtree(dir_fd=)` n'existe qu'à partir de Python 3.11,
#: alors que `pyproject.toml` déclare 3.9. Sur 3.9/3.10, l'effacement de
#: révocation (code 6) lèverait une TypeError après le lancement et le volume
#: resterait : refus au démarrage, avant tout lancement.
RMTREE_DIR_FD_MIN = (3, 11)


class UnsupportedPython(ValueError):
    """#1402 : le seul refus dont `main` affiche le message — il ne nomme
    que des versions, jamais un chemin de la recette."""


def require_supported_python(version_info=None) -> None:
    """Refuse un interpréteur où l'effacement de révocation ne peut pas courir.

    `supervise` l'appelle avant toute autre chose : le défaut est un refus
    net, pas une erreur au moment d'effacer le volume."""
    info = sys.version_info if version_info is None else version_info
    if tuple(info[:2]) < RMTREE_DIR_FD_MIN:
        raise UnsupportedPython(
            "the mediated supervisor requires Python >= %d.%d "
            "(shutil.rmtree(dir_fd=) for the revocation wipe); found %d.%d"
            % (RMTREE_DIR_FD_MIN + tuple(info[:2])))


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
    """The guard's state, `stopped` once its lending window is over, and
    `stale` for a gate that is absent, malformed or without a fresh
    heartbeat: the guard is then no longer there to stop or restart us."""
    try:
        state = parse_state(read_guest_json(path), now=now, lending=True)
        if state.until_ts is not None and state.until_ts <= now:
            return "stopped"
        return state.state
    except (TypeError, ValueError, OverflowError):
        return "stale"


def exit_text(rc: int) -> str:
    """#1402 N3 : the raw return code, and the signal's name when one ended
    the process (`Popen` reports it negative)."""
    if rc >= 0:
        return str(rc)
    try:
        return "%d (%s)" % (rc, signal.Signals(-rc).name)
    except ValueError:
        return "%d (signal %d)" % (rc, -rc)


def supervise(recipe: dict, stop: threading.Event, *, launch=subprocess.Popen,
              run=subprocess.run, now=time.time, log=print) -> int:
    require_supported_python()
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
            # Why the container was stopped: "exit" (signal or stale gate,
            # this supervisor ends) or "wait" (a fresh guard said `stopped`,
            # or its window ended: wait for the next `available`).
            stopping = None
            try:
                process = launch(argv)
                while process.poll() is None:
                    ack = read_guest_json(ack_path)
                    if ack and ack.get("schema") == "ameesh-host-ack/1":
                        # The image writes its ack only after enrollment and
                        # volume identity checks, when the runner is started.
                        remove_code(fds["enroll_dir"])
                    gate = gate_state(state_path, now())
                    if stop.is_set() or gate in ("stopped", "stale"):
                        stopping = "wait" if gate == "stopped" and not stop.is_set() else "exit"
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
            log("VM executor exited: %s" % exit_text(rc))
            if rc == 6:
                wipe(fds["volume"])
                return 6
            if stopping == "exit":
                return 0
            if stopping == "wait":
                # #1402 N1 : never exit on a fresh `stopped`. The unit stays
                # running, so a guard stop (level 4) finds it, records it in
                # `guard.stopped` and starts it again; a window that simply
                # ended relaunches here at the next `available`.
                delay = 30
                continue
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
    except UnsupportedPython as exc:
        print("VM supervisor failed: %s" % exc, flush=True)
        return 2
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # The type only: a message may name a path of the recipe.
        print("VM supervisor failed: %s" % type(exc).__name__, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
