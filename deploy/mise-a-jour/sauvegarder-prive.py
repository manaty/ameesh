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
    parameters = parameters_from(config)
    output = Path(output)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.parent.chmod(0o700)
    if os.path.lexists(output):
        raise BackupError("backup destination already exists")
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=output.parent) as staging:
        private = Path(staging)
        archive = private / "archive.dump"
        with private_connection(parameters, private) as (public_dsn, env):
            run(["pg_dump", "--no-password", "-Fc", "--dbname", public_dsn, "--file", str(archive)], env=env)
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


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("dump")
    backup.add_argument("--config", required=True)
    backup.add_argument("--output", required=True)
    transfer = commands.add_parser("upload")
    transfer.add_argument("--file", required=True)
    transfer.add_argument("--host", required=True)
    verify = commands.add_parser("verify-restore")
    verify.add_argument("--config", required=True)
    verify.add_argument("--file", required=True)
    verify.add_argument("--expected", required=True)
    args = parser.parse_args()
    try:
        if args.command == "dump":
            dump(args.config, args.output)
        elif args.command == "upload":
            upload(args.file, args.host)
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
