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


@contextmanager
def locked(store):
    store = Path(store).absolute()
    if store.is_symlink():
        raise ReleaseError("release store cannot be a symlink")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    if store.stat().st_uid != os.geteuid() or store.stat().st_mode & 0o077:
        raise ReleaseError("release store must be private and owned")
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
    with locked(store) as root:
        destination = root / release_id
        if os.path.lexists(destination):
            completed(root, release_id)
            if json.loads((destination / "ready.json").read_text()) != manifest:
                raise ReleaseError("release receipt mismatch")
            return release_id
        destination.mkdir(mode=0o700)
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


def completed(root, release_id):
    if not isinstance(release_id, str) or not re.fullmatch(r"[0-9a-f]{40}-[0-9a-f]{64}", release_id):
        raise ReleaseError("invalid release identity")
    path = root / release_id
    receipt = path / "ready.json"
    if (path.is_symlink() or receipt.is_symlink() or not receipt.is_file()
            or (path / "source").is_symlink() or not (path / "source").is_dir()
            or (path / "venv").is_symlink() or not (path / "venv/bin/python").is_file()):
        raise ReleaseError("release has no completed receipt")
    manifest = json.loads(receipt.read_text())
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if release_id != manifest.get("commit", "") + "-" + identity:
        raise ReleaseError("release receipt identity mismatch")
    return path


def activate(store, release_id, *, fenced=False):
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
        if previous == release_id:
            return
        if previous is not None:
            completed(root, previous)
        # Persist intent before switching. A crash leaves enough information
        # to determine which pointer is installed and restore the prior pair.
        atomic_json(root / "switch.json", {"previous": previous, "target": release_id})
        candidate = root / (".current-" + uuid.uuid4().hex)
        try:
            candidate.symlink_to(release_id)
            os.replace(candidate, current)
            sync_directory(root)
        finally:
            candidate.unlink(missing_ok=True)


def rollback(store, *, fenced=False):
    # Activation revalidates the pointer under its lock; rollback keeps the
    # same lock throughout, preventing a concurrent deployment being undone.
    if not fenced:
        raise ReleaseError("runner fence required before rollback")
    with locked(store) as root:
        intent = json.loads((root / "switch.json").read_text())
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
    undo = commands.add_parser("rollback")
    undo.add_argument("--fenced", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            print(prepare(args.store, args.repository, json.loads(Path(args.manifest).read_text())))
        elif args.command == "activate":
            activate(args.store, args.release, fenced=args.fenced)
        else:
            rollback(args.store, fenced=args.fenced)
        return 0
    except Exception:
        print("versioned release failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
