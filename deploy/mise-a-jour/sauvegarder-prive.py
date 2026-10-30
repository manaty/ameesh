#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Private credential channels and atomic dump publication (L232b).

Archive listing checks structural readability, not an isolated restore.
The release controller must separately verify restoration before migration.
"""
import argparse
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import quote
import zlib


class BackupError(ValueError):
    pass


# SigV4 signe le corps sous ce nom d'en-tête. curl 7.88 (Debian 12) le signe
# quand on le lui donne, mais ne l'ajoute jamais seul ; Scaleway refuse alors
# la requête (HTTP 400 Required header missing). Chaque requête nomme donc son
# empreinte : celle du fichier pour un PUT, celle du corps vide pour un GET.
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def run(args, *, env=None, timeout=1800):
    # A driver diagnostic may repeat its DSN, password or upload credentials.
    # Report only the process, its timeout or exit status, and never its stderr.
    name = Path(args[0]).name
    try:
        subprocess.run(args, env=env, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BackupError("%s timed out after %s s" % (name, timeout)) from None
    except subprocess.CalledProcessError as exc:
        raise BackupError("%s exited with status %s" % (name, exc.returncode)) from None
    except subprocess.SubprocessError:
        raise BackupError("%s failed" % name) from None
    except OSError as exc:
        raise BackupError("%s could not be started (%s)" % (name, exc.strerror or "OSError")) from None


def private_parent(path, what):
    """Create or tighten a private directory, never narrow a shared one.

    A directory this process owns is narrowed to 0700 (an installer may have
    created it 0755). A shared directory — another owner's, world-writable or
    sticky, like `/tmp` — is refused as it is: its mode is never changed under
    whoever else is using it, whatever uid this helper runs as.
    """
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise BackupError(what + " must be a private owned directory") from None
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & (stat.S_IWOTH | stat.S_ISVTX):
            raise BackupError(what + " must be a private owned directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return path


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
    private_parent(output.parent, "backup directory")
    if os.path.lexists(output):
        raise BackupError("backup destination already exists")
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=output.parent) as staging:
        private = Path(staging)
        archive = private / "archive.dump"
        with private_connection(parameters, private) as (public_dsn, env):
            args = ["pg_dump", "--no-password", "-Fc", "--dbname", public_dsn]
            if snapshot is not None:
                args.extend(["--snapshot", snapshot])
            try:
                run(args + ["--file", str(archive)], env=env)
            except BackupError as exc:
                try:
                    written = archive.stat().st_size
                except OSError:
                    written = 0
                raise BackupError("%s; %d bytes written" % (exc, written)) from None
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
    schema and representative tables that restoration must check, and pins the
    uncompressed live size (`source_bytes`) that bounds the target copy.
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
        # #3836/#3844 : l'export de snapshot reste ouvert pendant tout le
        # pg_dump (minutes sur une base qui grossit). La garde serveur
        # idle_in_transaction_session_timeout (180 s, rôle ameesh et base) le
        # prendrait pour une transaction abandonnée et tuerait un dump
        # légitime. Ce détenteur est explicite, en lecture seule, et meurt
        # avec le processus ; pg_dump fait de même pour sa propre session.
        # L'exemption vaut pour cette transaction seulement.
        source.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
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
        # The uncompressed live size bounds the copy a verified restore
        # materialises in the target cluster; captured in the same snapshot.
        size = source.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise BackupError("source did not report a usable database size")
        # The exporting transaction stays open until pg_dump imports and
        # finishes the snapshot, including writes made concurrently afterwards.
        dump_parameters(parameters, output, snapshot=snapshot)
        expected = {"schema": schema, "migrations": [list(row) for row in versions],
                    "row_counts": counts, "artifact_sha256": digest(output),
                    "source_bytes": size}
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


VERIFY_PREFIX = "ameesh_verify_"
VERIFY_MARKER = re.compile(r"\.verify-([0-9a-f]{32})\.json\Z")


def verify_marker(parent, database):
    """The private durable intent naming one disposable verification database."""
    return Path(parent) / (".verify-" + database[len(VERIFY_PREFIX):] + ".json")


def read_verify_marker(path):
    """Return the database named by one marker, or None when it is not ours.

    A marker is only trusted about a database this module creates: exact name,
    private owned regular file, no other key. Anything else is left alone, so a
    tampered document can never aim a DROP at a live database.
    """
    match = VERIFY_MARKER.fullmatch(path.name)
    if match is None:
        return None
    try:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            return None
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    database = VERIFY_PREFIX + match.group(1)
    if not isinstance(value, dict) or value != {"database": database}:
        return None
    return database


def write_verify_marker(path, database):
    """Record, before CREATE, the disposable database a kill would leave behind."""
    with path.open("x") as stream:
        path.chmod(0o600)
        stream.write(json.dumps({"database": database}, sort_keys=True))
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def reap_verify_databases(admin, parent):
    """Drop the recorded databases of verifications killed before cleanup.

    The retention directory is private to one host and every verification of
    that host is serialised by the controller, so a marker can only name a
    disposable database whose verifying process is gone.
    """
    from psycopg import sql
    for path in sorted(Path(parent).glob(".verify-*.json")):
        database = read_verify_marker(path)
        if database is None:
            continue
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
        path.unlink(missing_ok=True)


def verify_restore(config, archive, expected):
    """Restore only into a new random database, checking the pinned inventory.

    config is the separately approved restore identity, never a runner DSN.
    expected is a private inventory captured with the source backup snapshot.
    No restore, clean or DROP command can target a database this module did not
    create: only the exact names carried by its own private markers are reaped.
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
    database = VERIFY_PREFIX + uuid.uuid4().hex
    marker = verify_marker(archive.parent, database)
    # In-process libpq receives credentials directly; they never become argv.
    with psycopg.connect(make_conninfo(**parameters), autocommit=True) as admin:
        # A verification killed between CREATE and DROP names its database in a
        # private marker; reaping it here is the cleanup that survives the kill,
        # and it runs before another cleartext copy is created.
        reap_verify_databases(admin, archive.parent)
        try:
            write_verify_marker(marker, database)
        except OSError:
            raise BackupError("restore cleanup marker unavailable") from None
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
            marker.unlink(missing_ok=True)
    # A cleanup failure prevents a verified result as well.
    return {"artifact_sha256": sha, "restore_verified": True, "disposable_database_removed": True}


def object_version(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 1024
            or value.lower() == "null" or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise BackupError("immutable non-null object version required")
    return value


def header_version(path):
    with path.open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise BackupError("oversized storage headers")
    blocks = re.split(rb"\r?\n\r?\n", data)
    block = next((part for part in reversed(blocks) if part.startswith(b"HTTP/")), b"")
    values = []
    for line in block.splitlines()[1:]:
        key, sep, value = line.partition(b":")
        if sep and key.lower() in (b"x-amz-version-id", b"versionid"):
            values.append(value.strip().decode("ascii"))
    if len(values) != 1:
        raise BackupError("storage did not attest exact object version")
    return object_version(values[0])


def storage_credentials(path):
    """Private storage credential document, read without exposing its values.

    A caller (the enrolled release controller) supplies this file instead of the
    process environment; uploads then never depend on inherited SCW variables.
    """
    with Path(path).open("r") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_nlink != 1):
            raise BackupError("storage credentials need a private owned regular file")
        value = json.load(stream)
    if not isinstance(value, dict) or set(value) != {"access_key", "secret_key", "bucket", "region"}:
        raise BackupError("invalid storage credential document")
    if (not all(isinstance(value[key], str) and value[key] for key in value)
            or any(c in value["access_key"] + value["secret_key"] for c in "\r\n\x00")
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value["bucket"])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value["region"])):
        raise BackupError("invalid storage credentials")
    return value


def upload(archive, host, *, receipt=None, credentials=None, staging=None):
    if credentials is None:
        access, secret, bucket, region = (os.environ[name] for name in
                                         ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_BUCKET", "SCW_REGION"))
    else:
        if not isinstance(credentials, dict) or set(credentials) != {"access_key", "secret_key", "bucket", "region"}:
            raise BackupError("invalid upload credentials")
        access, secret, bucket, region = (credentials[key] for key in
                                          ("access_key", "secret_key", "bucket", "region"))
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value) for value in (bucket, region, host)):
        raise BackupError("invalid storage location")
    if any(c in access + secret for c in "\r\n\x00"):
        raise BackupError("invalid upload credentials")
    archive = Path(archive)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", archive.name):
        raise BackupError("invalid archive name")
    if receipt is not None:
        receipt = Path(receipt)
        if receipt.parent.resolve() != archive.parent.resolve() or os.path.lexists(receipt):
            raise BackupError("new upload receipt must share the private archive directory")
        info = archive.parent.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise BackupError("private owned receipt directory required")
    # The caller provides on-disk staging; never fall back to a shared tmpfs.
    staging_root = private_parent(Path(staging) if staging is not None else Path(os.environ["TMPDIR"]),
                                  "upload staging directory")
    # The payload hash is signed under x-amz-content-sha256; it is also the
    # value the optional receipt pins, so one pass serves both.
    sha = digest(archive)
    with tempfile.TemporaryDirectory(prefix="backup-upload-", dir=staging_root) as workspace:
        curl_config = Path(workspace) / "curl.conf"
        with curl_config.open("x") as stream:
            curl_config.chmod(0o600)
            stream.write("user = " + json.dumps(access + ":" + secret, ensure_ascii=False) + "\n")
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        headers = Path(workspace) / "headers"
        extra = ["--dump-header", str(headers)] if receipt is not None else []
        if extra:
            descriptor = os.open(headers, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        run(["curl", "--disable", "--config", str(curl_config), "--fail", "--silent", "--show-error", "--retry", "3",
             "--proto", "=https", *extra, "--upload-file", str(archive),
             "-H", "x-amz-content-sha256: " + sha,
             "--aws-sigv4", "aws:amz:" + region + ":s3",
             "https://" + bucket + ".s3." + region + ".scw.cloud/" + host + "/" + archive.name], env=env)
        if receipt is not None:
            version_id = header_version(headers)
            if digest(archive) != sha:
                raise BackupError("uploaded archive changed")
            value = {"object_id": host + "/" + archive.name, "sha256": sha,
                     "version_id": version_id, "bucket": bucket, "region": region}
            candidate = Path(workspace) / "receipt.json"
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            # Staging may be on another volume: copy into the destination
            # directory before atomic no-replacement publication.
            temporary = receipt.with_name("." + receipt.name + "-" + uuid.uuid4().hex)
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(candidate.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, receipt)
                directory = os.open(receipt.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
            return value


def retrieve(credentials, host, name, sha256, output, *, version_id=None):
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
    if version_id is not None:
        object_version(version_id)
    output = Path(output)
    private_parent(output.parent, "recovery destination directory")
    if os.path.lexists(output):
        raise BackupError("recovery destination already exists")
    with tempfile.TemporaryDirectory(prefix=".recovery-", dir=output.parent) as staging:
        private = Path(staging)
        curl_config, archive = private / "curl.conf", private / "archive.dump"
        with curl_config.open("x") as stream:
            curl_config.chmod(0o600)
            stream.write("user = " + json.dumps(access + ":" + secret, ensure_ascii=False) + "\n")
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        headers = private / "headers"
        extra = ["--dump-header", str(headers)] if version_id is not None else []
        if extra:
            descriptor = os.open(headers, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        url = "https://" + bucket + ".s3." + region + ".scw.cloud/" + host + "/" + name
        if version_id is not None:
            url += "?versionId=" + quote(version_id, safe="")
        # No redirects: a recovery object is bound to this exact HTTPS origin
        # and key, and authentication must never be forwarded elsewhere.
        run(["curl", "--disable", "--config", str(curl_config), "--fail", "--silent", "--show-error",
             "--retry", "3", "--proto", "=https",
             "-H", "x-amz-content-sha256: " + EMPTY_SHA256,
             "--aws-sigv4", "aws:amz:" + region + ":s3",
             *extra, "--output", str(archive), url], env=env)
        if version_id is not None and header_version(headers) != version_id:
            raise BackupError("retrieved object version mismatch")
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
    return {"artifact_sha256": sha256, "retrieved": True,
            **({"version_id": version_id} if version_id is not None else {})}


def pull(output, sha256, *, expected_bytes=None, timeout=3600, source=None):
    """Publish a gzip-compressed server-side dump read from ``source``.

    The archive is published by hard link only after its exact size and
    SHA-256 match the server's attestation. A timeout reports the bytes already
    written, never a subprocess diagnostic. The server lists the archive with
    ``pg_restore --list`` before sealing it, so this digest binds the local copy
    to a structurally readable dump without a second client-version check.
    """
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise BackupError("invalid dump digest")
    if (expected_bytes is not None
            and (isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int)
                 or expected_bytes <= 0)):
        raise BackupError("invalid dump size")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise BackupError("invalid retrieval timeout")
    output = Path(output)
    private_parent(output.parent, "backup directory")
    if os.path.lexists(output):
        raise BackupError("backup destination already exists")
    source = sys.stdin.buffer if source is None else source
    written = 0
    digest = hashlib.sha256()

    class _TimedOut(Exception):
        pass

    def expired(signum, frame):
        raise _TimedOut()

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(max(1, int(timeout)))
    try:
        with tempfile.TemporaryDirectory(prefix=".pull-", dir=output.parent) as staging:
            archive = Path(staging) / "archive.dump"
            descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(descriptor, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as sink, \
                        gzip.GzipFile(fileobj=source, mode="rb") as compressed:
                    while True:
                        chunk = compressed.read(1024 * 1024)
                        if not chunk:
                            break
                        sink.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                        if expected_bytes is not None and written > expected_bytes:
                            raise BackupError("dump retrieval exceeds the attested size: "
                                              "%d bytes written" % written)
                    sink.flush()
                    os.fsync(sink.fileno())
            except (OSError, EOFError, zlib.error) as exc:
                raise BackupError("dump retrieval failed (%s); %d bytes written"
                                  % (type(exc).__name__, written)) from None
            if expected_bytes is not None and written != expected_bytes:
                raise BackupError("dump retrieval incomplete: %d of %d bytes written"
                                  % (written, expected_bytes))
            if digest.hexdigest() != sha256:
                raise BackupError("dump retrieval digest mismatch after %d bytes written" % written)
            try:
                os.link(archive, output)
            except OSError:
                raise BackupError("could not publish retrieved backup") from None
            directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except _TimedOut:
        raise BackupError("dump retrieval timed out after %s s; %d bytes written"
                          % (timeout, written)) from None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    return {"artifact_sha256": sha256, "bytes": written}


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
    transfer.add_argument("--receipt")
    recovery = commands.add_parser("retrieve")
    recovery.add_argument("--credentials", required=True)
    recovery.add_argument("--host", required=True)
    recovery.add_argument("--name", required=True)
    recovery.add_argument("--sha256", required=True)
    recovery.add_argument("--output", required=True)
    recovery.add_argument("--version-id")
    pull_command = commands.add_parser("pull")
    pull_command.add_argument("--output", required=True)
    pull_command.add_argument("--sha256", required=True)
    pull_command.add_argument("--bytes", type=int, dest="expected_bytes")
    pull_command.add_argument("--timeout", type=float, default=3600)
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
            upload(args.file, args.host, receipt=args.receipt)
        elif args.command == "retrieve":
            print(json.dumps(retrieve(args.credentials, args.host, args.name, args.sha256, args.output, version_id=args.version_id)))
        elif args.command == "pull":
            print(json.dumps(pull(args.output, args.sha256,
                                  expected_bytes=args.expected_bytes, timeout=args.timeout)))
        else:
            proof = verify_restore(args.config, args.file, json.loads(Path(args.expected).read_text()))
            print(json.dumps(proof))
        return 0
    except BackupError as exc:
        # Every BackupError message is built by this module: process name,
        # timeout or exit status, bytes written. It never repeats a DSN or a
        # subprocess diagnostic, so the cause can reach logs and mail.
        print("private backup failed: %s" % exc, file=sys.stderr)
        return 1
    except Exception:
        # No raw exception or subprocess diagnostic reaches logs or mail.
        print("private backup failed; existing completed backups retained", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
