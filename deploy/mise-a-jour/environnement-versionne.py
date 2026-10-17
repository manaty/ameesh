#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Offline release preparation and one-pointer code/venv switching (L232b).

The host controller supplies an approved manifest and fences runners before
activation. This utility does not grant deployment authority or drain units.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid


class ReleaseError(ValueError):
    pass


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def run(args, *, env=None, cwd=None):
    try:
        return subprocess.run(args, env=env, cwd=cwd, check=True, capture_output=True,
                              text=True, timeout=1800).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("release subprocess failed") from None


#: The release store stays root-owned and non-writable for anyone else, but its
#: directory chain must be traversable by the enrolled executor account: the
#: controller runs the active release's own runner under that account
#: (`runuser -u <account> -- <store>/<release>/venv/bin/agent-runner`). 0711
#: grants traverse without listing; private receipts stay 0600/0700.
STORE_MODE = 0o711


@contextmanager
def locked(store):
    store = Path(store).absolute()
    if store.is_symlink():
        raise ReleaseError("release store cannot be a symlink")
    store.mkdir(mode=STORE_MODE, parents=True, exist_ok=True)
    info = store.stat()
    if info.st_uid != os.geteuid() or not stat.S_ISDIR(info.st_mode):
        raise ReleaseError("release store must be owned")
    if info.st_mode & 0o022:
        # A store another account can write is refused, never silently repaired.
        raise ReleaseError("release store must not be writable by others")
    os.chmod(store, STORE_MODE)
    if not store.stat().st_mode & 0o001:
        raise ReleaseError("release store must be traversable by the executor")
    descriptor = os.open(store / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield store
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    candidate = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    try:
        with candidate.open("x") as stream:
            candidate.chmod(0o600)
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
        sync_directory(path.parent)
    finally:
        candidate.unlink(missing_ok=True)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare(store, repository, manifest):
    """Build in the final path: installed venv shebangs cannot be relocated.

    No current pointer changes on failure. Wheel inputs include build and
    runtime dependencies and must all be pinned; pip never resolves online.
    """
    commit = manifest.get("commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError("full commit required")
    python = Path(manifest["python"])
    if not python.is_absolute() or digest(python) != manifest["python_sha256"]:
        raise ReleaseError("interpreter digest mismatch")
    wheels = manifest["wheels"]
    if not isinstance(wheels, list):
        raise ReleaseError("pinned wheel list required")
    for wheel in wheels:
        path = Path(wheel["path"])
        if (not path.is_absolute() or path.is_symlink() or not path.name.endswith(".whl")
                or digest(path) != wheel["sha256"]):
            raise ReleaseError("wheel digest mismatch")
    resolved = run(["git", "-C", str(repository), "rev-parse", "--verify", commit + "^{commit}"])
    if resolved != commit:
        raise ReleaseError("repository commit mismatch")
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    release_id = commit + "-" + identity
    # The venv tree must be readable and executable by the enrolled executor
    # account (health runs the release's own runner under it). Private files
    # keep their explicit 0600/0700 modes below; a 0022 umask only widens the
    # venv's own default modes, never the store's private ones.
    previous_umask = os.umask(0o022)
    try:
        return _build_release(repository=repository, commit=commit, manifest=manifest,
                              wheels=wheels, python=python, release_id=release_id,
                              root_store=store)
    finally:
        os.umask(previous_umask)


def _build_release(*, root_store, repository, commit, manifest, wheels, python, release_id):
    with locked(root_store) as root:
        destination = root / release_id
        if os.path.lexists(destination):
            if destination.is_dir() and not destination.is_symlink():
                os.chmod(destination, STORE_MODE)
            completed(root, release_id)
            if json.loads((destination / "ready.json").read_text()) != manifest:
                raise ReleaseError("release receipt mismatch")
            return release_id
        destination.mkdir(mode=STORE_MODE)
        os.chmod(destination, STORE_MODE)
        try:
            source = destination / "source"
            source.mkdir(mode=0o700)
            archive = destination / "source.tar"
            run(["git", "-C", str(repository), "archive", "--format=tar",
                 "--output=" + str(archive), commit])
            with tarfile.open(archive) as bundle:
                # Repositories with links require a separately reviewed
                # packaging policy. No archive entry can escape the snapshot.
                for member in bundle.getmembers():
                    path = Path(member.name)
                    if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                        raise ReleaseError("unsafe snapshot entry")
                bundle.extractall(source, filter="data")
            archive.unlink()
            venv = destination / "venv"
            env = dict(os.environ, PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX="1",
                       PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONNOUSERSITE="1")
            for key in list(env):
                if key.startswith("PIP_") and key not in ("PIP_CONFIG_FILE", "PIP_NO_INDEX", "PIP_DISABLE_PIP_VERSION_CHECK"):
                    env.pop(key)
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            run([str(python), "-m", "venv", "--copies", str(venv)], env=env)
            executable = str(venv / "bin/python")
            if run([executable, "-c", "import platform; print(platform.python_version())"], env=env) != manifest["python_version"]:
                raise ReleaseError("interpreter version mismatch")
            # Copy approved wheels privately, then hash again. Installation
            # reads these stable copies, never a mutable shared wheelhouse.
            inputs = destination / "wheels"
            inputs.mkdir(mode=0o700)
            copied = []
            for wheel in wheels:
                target = inputs / Path(wheel["path"]).name
                if target.exists():
                    raise ReleaseError("duplicate wheel filename")
                shutil.copyfile(wheel["path"], target)
                target.chmod(0o600)
                if digest(target) != wheel["sha256"]:
                    raise ReleaseError("wheel changed during preparation")
                copied.append(str(target))
            if copied:
                run([executable, "-m", "pip", "install", "--no-index", "--no-deps", *copied], env=env)
            pip_version = run([executable, "-c", "import pip; print(pip.__version__)"], env=env)
            if pip_version != manifest["pip_version"]:
                raise ReleaseError("pip version mismatch")
            run([executable, "-m", "pip", "install", "--no-index", "--no-deps",
                 "--no-build-isolation", str(source)], env=env)
            run([executable, "-m", "pip", "check"], env=env)
            run([executable, "-c", "import psycopg, ameesh.budget, ameesh.projects"], env=env, cwd=root)
            # The completed receipt must not reach disk before its contents.
            for directory, _, files in os.walk(destination):
                for name in files:
                    file = Path(directory) / name
                    if not file.is_symlink():
                        with file.open("rb") as stream:
                            os.fsync(stream.fileno())
                sync_directory(directory)
            atomic_json(destination / "ready.json", manifest)
            sync_directory(root)
            return release_id
        except BaseException:
            shutil.rmtree(destination)
            raise


def _executor_traversable(path):
    """The enrolled executor must traverse the venv it is asked to run."""
    for directory in (path, path / "bin"):
        try:
            info = directory.stat()
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode) or not info.st_mode & 0o001:
            return False
    return True


def completed(root, release_id):
    if not isinstance(release_id, str) or not re.fullmatch(r"[0-9a-f]{40}-[0-9a-f]{64}", release_id):
        raise ReleaseError("invalid release identity")
    path = root / release_id
    receipt = path / "ready.json"
    if (path.is_symlink() or receipt.is_symlink() or not receipt.is_file()
            or (path / "source").is_symlink() or not (path / "source").is_dir()
            or (path / "venv").is_symlink() or not (path / "venv/bin/python").is_file()):
        raise ReleaseError("release has no completed receipt")
    info = path.stat()
    if (info.st_uid != os.geteuid() or info.st_mode & 0o022
            or not info.st_mode & 0o001
            or not _executor_traversable(path / "venv")):
        # A release whose runner the enrolled executor cannot traverse must
        # never be activated or declared healthy.
        raise ReleaseError("release tree not runnable by the executor")
    manifest = json.loads(receipt.read_text())
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if release_id != manifest.get("commit", "") + "-" + identity:
        raise ReleaseError("release receipt identity mismatch")
    return path


def operation_file(root, operation):
    if not isinstance(operation, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", operation):
        raise ReleaseError("invalid switch operation")
    directory = root / ".operations"
    if directory.is_symlink():
        raise ReleaseError("linked operation directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ReleaseError("operation directory must be private and owned")
    return directory / (operation + ".json")


def switch_record(root, operation):
    path = operation_file(root, operation)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ReleaseError("unsafe switch receipt")
        record = json.load(stream)
    if not isinstance(record, dict) or set(record) != {"previous", "target"}:
        raise ReleaseError("invalid switch receipt")
    completed(root, record["previous"])
    completed(root, record["target"])
    return record


def pin_switch(store, operation, release_id):
    """Pin the operation's prior pair before configuration/activation begins.

    Enrollment of a first release (without a rollback pair) is separate. This
    utility still does not grant authority or establish the runner claim fence.
    """
    with locked(store) as root:
        completed(root, release_id)
        path = operation_file(root, operation)
        if os.path.lexists(path):
            record = switch_record(root, operation)
            if record["target"] != release_id:
                raise ReleaseError("operation target changed")
            return record
        current = root / "current"
        if not current.is_symlink():
            raise ReleaseError("prior release required for autonomous rollback")
        previous = os.readlink(current)
        completed(root, previous)
        record = {"previous": previous, "target": release_id}
        atomic_json(path, record)
        sync_directory(root)
        return record


def activate(store, release_id, *, fenced=False, operation=None):
    """One symlink serves current/source and current/venv as a matched pair.

    `fenced` is the caller's assertion, not an authorization receipt. The
    enrolled controller must verify authority and the drain/claim fence.
    """
    if not fenced:
        raise ReleaseError("runner fence required before switching")
    with locked(store) as root:
        completed(root, release_id)
        current = root / "current"
        previous = os.readlink(current) if current.is_symlink() else None
        if os.path.lexists(current) and previous is None:
            raise ReleaseError("current must be a release pointer")
        if operation is not None:
            record = switch_record(root, operation)
            if record["target"] != release_id or previous not in (record["previous"], release_id):
                raise ReleaseError("current or target differs from pinned operation")
        if previous == release_id:
            return
        if previous is not None:
            completed(root, previous)
        # Persist intent before switching. A crash leaves enough information
        # to determine which pointer is installed and restore the prior pair.
        if operation is None:
            atomic_json(root / "switch.json", {"previous": previous, "target": release_id})
        candidate = root / (".current-" + uuid.uuid4().hex)
        try:
            candidate.symlink_to(release_id)
            os.replace(candidate, current)
            sync_directory(root)
        finally:
            candidate.unlink(missing_ok=True)


def rollback(store, *, fenced=False, operation=None):
    # Activation revalidates the pointer under its lock; rollback keeps the
    # same lock throughout, preventing a concurrent deployment being undone.
    if not fenced:
        raise ReleaseError("runner fence required before rollback")
    with locked(store) as root:
        intent = (switch_record(root, operation) if operation is not None
                  else json.loads((root / "switch.json").read_text()))
        previous = intent["previous"]
        if previous is None:
            raise ReleaseError("no previous release")
        completed(root, previous)
        current = root / "current"
        actual = os.readlink(current)
        if actual == previous:
            return
        if actual != intent["target"]:
            raise ReleaseError("current differs from the recorded switch")
        candidate = root / (".rollback-" + uuid.uuid4().hex)
        try:
            candidate.symlink_to(previous)
            os.replace(candidate, current)
            sync_directory(root)
        finally:
            candidate.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("prepare")
    stage.add_argument("--repository", required=True)
    stage.add_argument("--manifest", required=True)
    switch = commands.add_parser("activate")
    switch.add_argument("--release", required=True)
    switch.add_argument("--fenced", action="store_true")
    switch.add_argument("--operation")
    pin = commands.add_parser("pin-switch")
    pin.add_argument("--operation", required=True)
    pin.add_argument("--release", required=True)
    undo = commands.add_parser("rollback")
    undo.add_argument("--fenced", action="store_true")
    undo.add_argument("--operation")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            print(prepare(args.store, args.repository, json.loads(Path(args.manifest).read_text())))
        elif args.command == "activate":
            activate(args.store, args.release, fenced=args.fenced, operation=args.operation)
        elif args.command == "pin-switch":
            pin_switch(args.store, args.operation, args.release)
        else:
            rollback(args.store, fenced=args.fenced, operation=args.operation)
        return 0
    except Exception:
        print("versioned release failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
