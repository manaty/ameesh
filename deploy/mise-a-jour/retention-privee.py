#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit local recovery-set retention; unknown backups are never expired.

The enrolled controller supplies policy dates and accepts a release only
after health verification. This helper does not authenticate that controller.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import uuid


class RetentionError(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise RetentionError("invalid artifact identifier")
    return value


def timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise RetentionError("invalid policy timestamp")
    return value


@contextmanager
def private_stream(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077 or metadata.st_nlink != 1):
            raise RetentionError("private owned regular file required")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def private_file(path):
    with private_stream(path) as stream:
        return stream.read()


def private_digest(path):
    value = hashlib.sha256()
    length = 0
    with private_stream(path) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
            length += len(chunk)
    if not length:
        raise RetentionError("empty recovery artifact")
    return value.hexdigest()


def sync(root):
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(root, records):
    candidate = root / (".retention-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(records, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, root / ".retention.json")
        sync(root)
    finally:
        candidate.unlink(missing_ok=True)


@contextmanager
def locked(root):
    root = Path(root).absolute()
    # Resolve the whole directory chain, including intermediate symlinks.
    if root.resolve() != root:
        raise RetentionError("retention root cannot traverse symlinks")
    metadata = root.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise RetentionError("private owned retention directory required")
    descriptor = os.open(root / ".retention.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise RetentionError("private owned lock required")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        catalog = root / ".retention.json"
        records = json.loads(private_file(catalog)) if os.path.lexists(catalog) else {}
        validate(records)
        yield root, records
    finally:
        os.close(descriptor)


def validate(records):
    if not isinstance(records, dict):
        raise RetentionError("invalid retention catalog")
    names = set()
    for artifact, record in records.items():
        identifier(artifact)
        if not isinstance(record, dict) or record.get("state") not in ("retained", "deleting", "deleted"):
            raise RetentionError("invalid retention record")
        timestamp(record["expires_at"])
        timestamp(record["rollback_until"])
        if type(record["accepted"]) is not bool or type(record["hold"]) is not bool:
            raise RetentionError("invalid recovery protection")
        if record["state"] in ("deleting", "deleted"):
            timestamp(record["deletion_started_at"])
        if record["state"] == "deleted":
            timestamp(record["deleted_at"])
        if not isinstance(record["files"], dict) or not record["files"]:
            raise RetentionError("empty recovery set")
        for name, sha in record["files"].items():
            identifier(name)
            if name.startswith(".") or name in names or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise RetentionError("invalid or shared recovery file")
            names.add(name)


def register(root, artifact, files, *, expires_at, rollback_until):
    identifier(artifact)
    timestamp(expires_at)
    timestamp(rollback_until)
    with locked(root) as (root, records):
        if artifact in records:
            raise RetentionError("artifact already registered")
        hashes = {}
        for name in files:
            identifier(name)
            if name.startswith("."):
                raise RetentionError("reserved filename")
            hashes[name] = private_digest(root / name)
        records[artifact] = dict(files=hashes, expires_at=expires_at,
                                 rollback_until=rollback_until, accepted=False, hold=False, state="retained")
        validate(records)
        save(root, records)


def protect(root, artifact, *, accepted=None, hold=None):
    with locked(root) as (root, records):
        record = records[identifier(artifact)]
        if record["state"] != "retained":
            raise RetentionError("deletion already started")
        if accepted is not None:
            record["accepted"] = accepted
        if hold is not None:
            record["hold"] = hold
        validate(records)
        save(root, records)


def prune(root, *, now):
    timestamp(now)
    with locked(root) as (root, records):
        candidates = {artifact: record for artifact, record in records.items()
                      if record["state"] != "deleted" and record["accepted"] and not record["hold"]
                      and now >= max(record["expires_at"], record["rollback_until"])}
        # Preflight every candidate before any deletion. A changed file blocks
        # the batch; a missing file is valid only while replaying a durable intent.
        for record in candidates.values():
            for name, sha in record["files"].items():
                path = root / name
                if not os.path.lexists(path) and record["state"] == "deleting":
                    continue
                if private_digest(path) != sha:
                    raise RetentionError("recovery artifact changed")
        for artifact, record in candidates.items():
            record.setdefault("deletion_started_at", now)
            record["state"] = "deleting"
            save(root, records)
            for name in record["files"]:
                (root / name).unlink(missing_ok=True)
            sync(root)
            record["state"] = "deleted"
            record["deleted_at"] = now
            save(root, records)
        return list(candidates)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("register")
    add.add_argument("artifact")
    add.add_argument("files", nargs="+")
    add.add_argument("--expires-at", type=float, required=True)
    add.add_argument("--rollback-until", type=float, required=True)
    accept = commands.add_parser("accept")
    accept.add_argument("artifact")
    hold = commands.add_parser("hold")
    hold.add_argument("artifact")
    hold.add_argument("--release", action="store_true")
    expire = commands.add_parser("prune")
    expire.add_argument("--now", type=float, required=True)
    args = parser.parse_args()
    try:
        if args.command == "register":
            register(args.root, args.artifact, args.files, expires_at=args.expires_at, rollback_until=args.rollback_until)
        elif args.command == "accept":
            protect(args.root, args.artifact, accepted=True)
        elif args.command == "hold":
            protect(args.root, args.artifact, hold=not args.release)
        else:
            print(json.dumps({"deleted": prune(args.root, now=args.now)}))
        return 0
    except Exception:
        print("private retention failed; inspect artifact state before retry", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
