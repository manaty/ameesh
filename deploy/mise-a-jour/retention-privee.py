#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit recovery-set retention; unknown backups are never expired.

The enrolled controller supplies policy dates and accepts a release only
after health verification. This helper does not authenticate that controller.
"""
import argparse
from contextlib import contextmanager
import fcntl
import fnmatch
import hashlib
import importlib.util
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


def version_identifier(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 1024
            or value.lower() == "null" or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise RetentionError("immutable non-null object version required")
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
    objects = set()
    unqualified = set()
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
        remote = record.get("remote", [])
        if not isinstance(remote, list):
            raise RetentionError("invalid remote recovery inventory")
        for item in remote:
            if not isinstance(item, dict):
                raise RetentionError("invalid remote recovery object")
            object_id = item.get("object_id")
            if not isinstance(object_id, str) or object_id.count("/") != 1:
                raise RetentionError("invalid remote recovery reference")
            host, name = object_id.split("/")
            identifier(host)
            identifier(name)
            if "version_id" in item:
                version_identifier(item["version_id"])
            reference = (object_id, item.get("version_id"))
            if (reference in objects or object_id in unqualified
                    or (item.get("version_id") is None and any(ref[0] == object_id for ref in objects))
                    or item.get("sha256") not in record["files"].values()
                    or type(item.get("deleted")) is not bool
                    or (item["deleted"] and record["state"] == "retained")
                    or (not item["deleted"] and record["state"] == "deleted")):
                raise RetentionError("invalid or shared remote recovery object")
            objects.add(reference)
            if item.get("version_id") is None:
                unqualified.add(object_id)


def register(root, artifact, files, *, expires_at, rollback_until, remote=()):
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
                                 rollback_until=rollback_until, accepted=False, hold=False, state="retained",
                                 remote=[dict(object_id=item["object_id"], sha256=item["sha256"], deleted=False,
                                              **({"version_id": item["version_id"]} if "version_id" in item else {}))
                                         for item in remote])
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


PROVIDER_FIELDS = {"host", "credentials", "staging", "max_bytes"}
# Fixed sibling module; never a path or module name supplied by a caller.
PROVIDER_MODULE = "retention-scaleway"
PROVIDER_DOCUMENT_LIMIT = 65536


def provider_module():
    """Load the fixed provider backend installed beside this helper."""
    path = Path(__file__).with_name(PROVIDER_MODULE + ".py")
    try:
        info = os.lstat(path)
    except OSError:
        raise RetentionError("provider retention module unavailable") from None
    if (stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode)):
        raise RetentionError("untrusted provider retention module")
    spec = importlib.util.spec_from_file_location("private_retention_scaleway", path)
    loaded = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(loaded)
    except Exception:
        raise RetentionError("provider retention module unavailable") from None
    return loaded


class ScopedProvider:
    """One enrolled object host over the fixed versioned provider backend."""

    def __init__(self, backend, host):
        self.backend, self.host = backend, host

    def _scoped(self, object_id):
        if not isinstance(object_id, str) or object_id.split("/", 1)[0] != self.host:
            raise RetentionError("provider object outside enrolled host scope")
        return object_id

    def inspect(self, object_id, **version):
        return self.backend.inspect(self._scoped(object_id), **version)

    def delete_if_digest(self, object_id, sha256, **version):
        return self.backend.delete_if_digest(self._scoped(object_id), sha256, **version)


def provider_adapter(path):
    """Build the enrolled version-retention provider for exactly one host.

    The protected document has exact keys: `host` (the object host this job may
    expire), `credentials` (private provider credential file), `staging`
    (private on-disk staging root) and `max_bytes` (explicit staging budget).
    The backend is the fixed sibling module; credential values never enter an
    argument, a receipt or an error message. The enrolled host must be covered
    by the credential's own host allowlist and no other host may be addressed.
    """
    enrollment = Path(path)
    if not enrollment.is_absolute():
        raise RetentionError("absolute provider enrollment required")
    try:
        with private_stream(enrollment) as stream:
            data = stream.read(PROVIDER_DOCUMENT_LIMIT + 1)
    except OSError:
        raise RetentionError("private provider enrollment required") from None
    if len(data) > PROVIDER_DOCUMENT_LIMIT:
        raise RetentionError("provider enrollment exceeds limit")
    try:
        document = json.loads(data)
    except ValueError:
        raise RetentionError("provider enrollment is not JSON") from None
    if not isinstance(document, dict) or set(document) != PROVIDER_FIELDS:
        raise RetentionError("exact provider enrollment required")
    host = document["host"]
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", host):
        raise RetentionError("invalid provider host")
    for key in ("credentials", "staging"):
        value = document[key]
        if (not isinstance(value, str) or not value.startswith("/") or "\x00" in value
                or ".." in Path(value).parts):
            raise RetentionError("absolute provider path required")
    budget = document["max_bytes"]
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise RetentionError("explicit provider staging budget required")
    module = provider_module()
    try:
        backend = module.VersionRetention(document["credentials"], document["staging"],
                                          max_bytes=budget)
    except Exception:
        raise RetentionError("provider retention adapter unavailable") from None
    if host not in getattr(backend, "hosts", ()):
        raise RetentionError("provider enrollment outside credential host scope")
    return ScopedProvider(backend, host)


def prune(root, *, now, remote_adapter=None, remote_provider=None):
    """Delete remote members before local files, under the protection lock.

    An enrolled adapter implements inspect(object_id) -> SHA256 or None,
    and delete_if_digest(object_id, sha256) -> exact absence receipt. The
    latter must enforce the digest condition atomically, including races
    after inspect; plain unconditional DELETE is not a valid implementation.
    `remote_provider` is a zero-argument factory resolved only when a candidate
    still has an undeleted remote member, so an enrolled backend is not built,
    contacted or required when nothing remote has to be expired.
    """
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
            for item in record.get("remote", []):
                if item["deleted"]:
                    continue
                if remote_adapter is None and remote_provider is not None:
                    remote_adapter = remote_provider()
                if remote_adapter is None:
                    raise RetentionError("remote retention adapter is not enrolled")
                actual = remote_adapter.inspect(item["object_id"], **(
                    {"version_id": item["version_id"]} if "version_id" in item else {}))
                if actual != item["sha256"] and not (actual is None and record["state"] == "deleting"):
                    raise RetentionError("remote recovery artifact changed or missing")
        for artifact, record in candidates.items():
            record.setdefault("deletion_started_at", now)
            record["state"] = "deleting"
            save(root, records)
            for item in record.get("remote", []):
                if item["deleted"]:
                    continue
                # Replay accepts already-absent objects only after durable
                # intent. Exact receipts prevent a wrong object/SHA response
                # from unlocking local deletion.
                version = {"version_id": item["version_id"]} if "version_id" in item else {}
                receipt = remote_adapter.delete_if_digest(item["object_id"], item["sha256"], **version)
                if (not isinstance(receipt, dict) or receipt.get("absent") is not True
                        or receipt != {"object_id": item["object_id"], "sha256": item["sha256"], "absent": True, **version}):
                    raise RetentionError("remote deletion was not confirmed")
                item["deleted"] = True
                save(root, records)
            for name in record["files"]:
                (root / name).unlink(missing_ok=True)
            sync(root)
            record["state"] = "deleted"
            record["deleted_at"] = now
            save(root, records)
        return list(candidates)


# Files the daily server backup publishes. Only an artifact with one of these
# names, referenced by no catalog record, ever expires by age.
DAILY_ARTIFACTS = "ameesh-*.dump"


def expire_unregistered(root, *, now, max_age):
    """Bounded local retention for daily artifacts no catalog record governs.

    The server's daily dump keeps its own declared window; an artifact named by
    any catalog record belongs to a recovery set and is never touched here,
    whatever its age or state. Candidates must be private owned regular files
    with a single link: any other candidate is left in place and reported.
    """
    timestamp(now)
    timestamp(max_age)
    with locked(root) as (root, records):
        protected = set()
        for record in records.values():
            protected.update(record["files"])
        deleted, refused = [], []
        for path in sorted(root.iterdir(), key=lambda entry: entry.name):
            name = path.name
            if name.startswith(".") or name in protected or not fnmatch.fnmatch(name, DAILY_ARTIFACTS):
                continue
            try:
                info = path.lstat()
            except OSError:
                refused.append(name)
                continue
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1):
                refused.append(name)
                continue
            if now - info.st_mtime < max_age:
                continue
            path.unlink()
            deleted.append(name)
        if deleted:
            sync(root)
        return {"deleted": deleted, "refused": refused}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("register")
    add.add_argument("artifact")
    add.add_argument("files", nargs="+")
    add.add_argument("--expires-at", type=float, required=True)
    add.add_argument("--rollback-until", type=float, required=True)
    add.add_argument("--remote-inventory")
    accept = commands.add_parser("accept")
    accept.add_argument("artifact")
    hold = commands.add_parser("hold")
    hold.add_argument("artifact")
    hold.add_argument("--release", action="store_true")
    expire = commands.add_parser("prune")
    expire.add_argument("--now", type=float, required=True)
    expire.add_argument("--provider-enrollment")
    daily = commands.add_parser("expire-unregistered")
    daily.add_argument("--now", type=float, required=True)
    daily.add_argument("--max-age", type=float, required=True)
    args = parser.parse_args()
    try:
        if args.command == "register":
            remote = json.loads(private_file(args.remote_inventory)) if args.remote_inventory else []
            register(args.root, args.artifact, args.files, expires_at=args.expires_at,
                     rollback_until=args.rollback_until, remote=remote)
        elif args.command == "accept":
            protect(args.root, args.artifact, accepted=True)
        elif args.command == "hold":
            protect(args.root, args.artifact, hold=not args.release)
        elif args.command == "expire-unregistered":
            print(json.dumps(expire_unregistered(args.root, now=args.now, max_age=args.max_age)))
        else:
            provider = (lambda: provider_adapter(args.provider_enrollment)) if args.provider_enrollment else None
            print(json.dumps({"deleted": prune(args.root, now=args.now, remote_provider=provider)}))
        return 0
    except Exception:
        print("private retention failed; inspect artifact state before retry", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
