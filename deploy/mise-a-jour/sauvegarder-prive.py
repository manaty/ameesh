#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Private credential channels and atomic dump publication (L232b).

Archive listing checks structural readability, not an isolated restore.
The release controller must separately verify restoration before migration.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid


class BackupError(ValueError):
    pass


def run(args, *, env=None):
    # A driver diagnostic may repeat its DSN, password or upload credentials.
    try:
        subprocess.run(args, env=env, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=1800)
    except (OSError, subprocess.SubprocessError):
        raise BackupError("backup subprocess failed: " + Path(args[0]).name) from None


def parameters_from(config):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    parameters = conninfo_to_dict(json.loads(Path(config).read_text())["dsn"])
    if parameters.get("dbname", "").startswith(("postgresql://", "postgres://")):
        raise BackupError("nested database URI requires a flat connection configuration")
    # SSL key passwords need a separate protected channel; do not silently
    # leave one in the supposedly public conninfo passed to the child.
    if parameters.get("sslpassword"):
        raise BackupError("encrypted SSL key requires a protected credential integration")
    return parameters


@contextmanager
def private_connection(parameters, directory):
    from psycopg.conninfo import make_conninfo
    parameters = dict(parameters)
    password = parameters.pop("password", None)
    if password is not None and any(c in password for c in "\r\n\x00"):
        raise BackupError("password cannot be represented in a private pgpass file")
    public_dsn = make_conninfo(**parameters)
    env = dict(os.environ)
    passfile = None
    try:
        if password is not None:
            passfile = Path(directory) / ("pgpass-" + uuid.uuid4().hex)
            with passfile.open("x") as stream:
                os.chmod(passfile, 0o600)
                escaped = password.replace("\\", "\\\\").replace(":", "\\:")
                stream.write("*:*:*:*:" + escaped + "\n")
            env.pop("PGPASSWORD", None)
            env["PGPASSFILE"] = str(passfile)
        yield public_dsn, env
    finally:
        if passfile is not None:
            passfile.unlink(missing_ok=True)


def dump(config, output):
    return dump_parameters(parameters_from(config), output)


def dump_parameters(parameters, output, *, snapshot=None):
    output = Path(output)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.parent.chmod(0o700)
    if os.path.lexists(output):
        raise BackupError("backup destination already exists")
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=output.parent) as staging:
        private = Path(staging)
        archive = private / "archive.dump"
        with private_connection(parameters, private) as (public_dsn, env):
            args = ["pg_dump", "--no-password", "-Fc", "--dbname", public_dsn]
            if snapshot is not None:
                args.extend(["--snapshot", snapshot])
            run(args + ["--file", str(archive)], env=env)
            if not archive.is_file() or archive.stat().st_size == 0:
                raise BackupError("backup is empty or missing")
            archive.chmod(0o600)
            run(["pg_restore", "--list", str(archive)], env=env)
        with archive.open("rb") as stream:
            os.fsync(stream.fileno())
        # Hard-link publication refuses concurrent replacement of an existing
        # backup; the incomplete file never has its final name.
        os.link(archive, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def snapshot_dump(config, output, inventory, *, schema="public", tables=("agent_registry", "agent_mailbox")):
    """Capture expected rows and the archive from one repeatable-read snapshot.

    The full database is dumped; the private inventory identifies the mesh
    schema and representative tables that restoration must check.
    """
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    output, inventory = Path(output), Path(inventory)
    if (inventory.parent.resolve() != output.parent.resolve() or inventory == output
            or os.path.lexists(inventory) or os.path.lexists(output)):
        raise BackupError("new archive and inventory must share one private directory")
    if (not isinstance(schema, str) or not schema or "\x00" in schema
            or not isinstance(tables, (tuple, list)) or not tables
            or any(not isinstance(table, str) or not table or "\x00" in table for table in tables)):
        raise BackupError("invalid expected source inventory")
    parameters = parameters_from(config)
    with psycopg.connect(make_conninfo(**parameters)) as source:
        source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
        if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9A-Fa-f-]+", snapshot):
            raise BackupError("source did not export a valid snapshot")
        versions = source.execute(sql.SQL("SELECT version,checksum FROM {}.schema_migrations ORDER BY version")
                                  .format(sql.Identifier(schema))).fetchall()
        if not versions:
            raise BackupError("source has no recorded migration inventory")
        counts = {}
        for table in sorted(set(tables)):
            counts[table] = source.execute(sql.SQL("SELECT count(*) FROM {}.{}")
                                          .format(sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]
        # The exporting transaction stays open until pg_dump imports and
        # finishes the snapshot, including writes made concurrently afterwards.
        dump_parameters(parameters, output, snapshot=snapshot)
        expected = {"schema": schema, "migrations": [list(row) for row in versions],
                    "row_counts": counts, "artifact_sha256": digest(output)}
        with tempfile.TemporaryDirectory(prefix=".inventory-", dir=output.parent) as staging:
            candidate = Path(staging) / "inventory.json"
            with candidate.open("x") as stream:
                candidate.chmod(0o600)
                json.dump(expected, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(candidate, inventory)
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {"artifact_sha256": expected["artifact_sha256"], "inventory": str(inventory)}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verify_restore(config, archive, expected):
    """Restore only into a new random database, checking the pinned inventory.

    config is the separately approved restore identity, never a runner DSN.
    expected is a private inventory captured with the source backup snapshot.
    No restore, clean or DROP command can target an existing database.
    """
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    schema = expected.get("schema")
    counts, migrations = expected.get("row_counts"), expected.get("migrations")
    if (not isinstance(schema, str) or not schema
            or not isinstance(counts, dict) or not counts or not isinstance(migrations, list)
            or not migrations or any(not isinstance(name, str) or not name
                                    or isinstance(count, bool) or not isinstance(count, int) or count < 0
                                    for name, count in counts.items())):
        raise BackupError("restore needs a complete expected schema/migrations/row inventory")
    archive = Path(archive)
    sha = digest(archive)
    if sha != expected.get("artifact_sha256"):
        raise BackupError("restore artifact digest mismatch")
    parameters = parameters_from(config)
    database = "ameesh_verify_" + uuid.uuid4().hex
    # In-process libpq receives credentials directly; they never become argv.
    with psycopg.connect(make_conninfo(**parameters), autocommit=True) as admin:
        created = False
        try:
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
            created = True
            target = dict(parameters, dbname=database)
            with tempfile.TemporaryDirectory(prefix=".restore-", dir=archive.parent) as staging:
                with private_connection(target, staging) as (public_dsn, env):
                    run(["pg_restore", "--no-password", "--exit-on-error", "--no-owner", "--no-privileges",
                         "--dbname", public_dsn, str(archive)], env=env)
            with psycopg.connect(make_conninfo(**target)) as restored:
                versions = restored.execute(sql.SQL("SELECT version,checksum FROM {}.schema_migrations ORDER BY version")
                                            .format(sql.Identifier(schema))).fetchall()
                if [list(row) for row in versions] != migrations:
                    raise BackupError("restored migration inventory mismatch")
                for table, count in counts.items():
                    actual = restored.execute(sql.SQL("SELECT count(*) FROM {}.{}")
                                              .format(sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]
                    if actual != count:
                        raise BackupError("restored row inventory mismatch")
            if digest(archive) != sha:
                raise BackupError("restore artifact changed during verification")
        finally:
            if created:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
    # A cleanup failure prevents a verified result as well.
    return {"artifact_sha256": sha, "restore_verified": True, "disposable_database_removed": True}


def upload(archive, host):
    access, secret, bucket, region = (os.environ[name] for name in
                                     ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_BUCKET", "SCW_REGION"))
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value) for value in (bucket, region, host)):
        raise BackupError("invalid storage location")
    if any(c in access + secret for c in "\r\n\x00"):
        raise BackupError("invalid upload credentials")
    archive = Path(archive)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", archive.name):
        raise BackupError("invalid archive name")
    # The caller provides TMPDIR on disk; never fall back to a shared tmpfs.
    staging_root = Path(os.environ["TMPDIR"])
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging_root.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="backup-upload-", dir=staging_root) as staging:
        credentials = Path(staging) / "curl.conf"
        with credentials.open("x") as stream:
            credentials.chmod(0o600)
            stream.write("user = " + json.dumps(access + ":" + secret, ensure_ascii=False) + "\n")
        env = {key: value for key, value in os.environ.items() if key not in ("SCW_ACCESS_KEY", "SCW_SECRET_KEY")}
        run(["curl", "--disable", "--config", str(credentials), "--fail", "--silent", "--show-error", "--retry", "3",
             "--upload-file", str(archive), "--aws-sigv4", "aws:amz:" + region + ":s3",
             "https://" + bucket + ".s3." + region + ".scw.cloud/" + host + "/" + archive.name], env=env)


def retrieve(credentials, host, name, sha256, output):
    """Retrieve with the restore identity and publish only the pinned artifact.

    The credentials file belongs to the separate recovery identity; upload
    credentials inherited from the caller are never used by the curl child.
    """
    credentials = Path(credentials)
    with credentials.open("r") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise BackupError("restore credentials need a private owned regular file")
        settings = json.load(stream)
    access, secret, bucket, region = (settings[key] for key in
                                     ("access_key", "secret_key", "bucket", "region"))
    if (not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value)
                for value in (bucket, region, host))
            or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name)
            or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or not all(isinstance(value, str) and value and not any(c in value for c in "\r\n\x00")
                       for value in (access, secret))):
        raise BackupError("invalid pinned recovery location or credentials")
    output = Path(output)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.parent.chmod(0o700)
    if os.path.lexists(output):
        raise BackupError("recovery destination already exists")
    with tempfile.TemporaryDirectory(prefix=".recovery-", dir=output.parent) as staging:
        private = Path(staging)
        curl_config, archive = private / "curl.conf", private / "archive.dump"
        with curl_config.open("x") as stream:
            curl_config.chmod(0o600)
            stream.write("user = " + json.dumps(access + ":" + secret, ensure_ascii=False) + "\n")
        env = {key: value for key, value in os.environ.items()
               if key not in ("SCW_ACCESS_KEY", "SCW_SECRET_KEY")}
        # No redirects: a recovery object is bound to this exact HTTPS origin
        # and key, and authentication must never be forwarded elsewhere.
        run(["curl", "--disable", "--config", str(curl_config), "--fail", "--silent", "--show-error",
             "--retry", "3", "--proto", "=https", "--aws-sigv4", "aws:amz:" + region + ":s3",
             "--output", str(archive),
             "https://" + bucket + ".s3." + region + ".scw.cloud/" + host + "/" + name], env=env)
        if not archive.is_file() or archive.stat().st_size == 0 or digest(archive) != sha256:
            raise BackupError("retrieved recovery artifact digest mismatch")
        archive.chmod(0o600)
        run(["pg_restore", "--list", str(archive)], env=env)
        with archive.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(archive, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {"artifact_sha256": sha256, "retrieved": True}


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("dump")
    backup.add_argument("--config", required=True)
    backup.add_argument("--output", required=True)
    snapshot = commands.add_parser("snapshot-dump")
    snapshot.add_argument("--config", required=True)
    snapshot.add_argument("--output", required=True)
    snapshot.add_argument("--inventory", required=True)
    snapshot.add_argument("--schema", default="public")
    snapshot.add_argument("--table", action="append", dest="tables")
    transfer = commands.add_parser("upload")
    transfer.add_argument("--file", required=True)
    transfer.add_argument("--host", required=True)
    recovery = commands.add_parser("retrieve")
    recovery.add_argument("--credentials", required=True)
    recovery.add_argument("--host", required=True)
    recovery.add_argument("--name", required=True)
    recovery.add_argument("--sha256", required=True)
    recovery.add_argument("--output", required=True)
    verify = commands.add_parser("verify-restore")
    verify.add_argument("--config", required=True)
    verify.add_argument("--file", required=True)
    verify.add_argument("--expected", required=True)
    args = parser.parse_args()
    try:
        if args.command == "dump":
            dump(args.config, args.output)
        elif args.command == "snapshot-dump":
            proof = snapshot_dump(args.config, args.output, args.inventory, schema=args.schema,
                                  tables=args.tables or ("agent_registry", "agent_mailbox"))
            print(json.dumps(proof))
        elif args.command == "upload":
            upload(args.file, args.host)
        elif args.command == "retrieve":
            print(json.dumps(retrieve(args.credentials, args.host, args.name, args.sha256, args.output)))
        else:
            proof = verify_restore(args.config, args.file, json.loads(Path(args.expected).read_text()))
            print(json.dumps(proof))
        return 0
    except Exception:
        # No raw exception or subprocess diagnostic reaches logs or mail.
        print("private backup failed; existing completed backups retained", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
