# SPDX-License-Identifier: AGPL-3.0-only
"""Public production database surface, contract v1.6 (#6573).

An installed module already trusted with the deployment's database identity
opens its connection through this module instead of importing the core's
``ameesh.config``, ``ameesh.db`` and ``require_schema()``.  The written
boundary is ``docs/api/module-contract-v1.md``; internal helpers of this
package are not part of it.

The surface owns one thing: :func:`connect` takes no argument, reads the same
environment/configuration as an installed core command (``AMEESH_DSN`` or the
configured DSN, schema, driver and backend), opens one connection and checks
the agent-mesh schema **immediately** with the core's own probe.  The returned
object is the core connection, opaque for this contract: it is meant for
:mod:`ameesh.public.mail` and for :meth:`close`, never as an SQL escape hatch
on the core's tables, whose names and columns remain internal.

The credential remains the trust boundary: this surface authenticates no
remote client, and does not mint, widen or verify a runner identity.
Mediated/remote executor connections are refused before any surface call.

:func:`connect` never takes a DSN or credential argument and adds no attribute
returning one; the message of every published error is redacted so it never
carries the DSN or a password.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, quote_plus

from .. import config as config_mod
from .. import db as db_mod
from .. import dsn as dsn_mod
from .. import storage

#: revision of the written contract implemented by this module
CONTRACT_VERSION = "1.6"


class DatabaseError(RuntimeError):
    """The public production connection could not be established."""

    code = "database"


class DatabaseUnavailable(DatabaseError):
    """The configured database, or its configuration, cannot be opened."""

    code = "unavailable"


class SchemaRefused(DatabaseError):
    """The database opened but its agent-mesh schema is not usable."""

    code = "schema"


class UnsupportedStorage(DatabaseError, NotImplementedError):
    """A mediated or remote executor connection is outside this surface."""

    code = "unsupported"


#: userinfo password of a PostgreSQL URI (``postgresql://user:pw@host``)
_URI_PASSWORD = re.compile(r"(postgres(?:ql)?://[^:/@\s]+:)[^@\s]+@")
#: libpq key/value password, quoted or bare
_PASSWORD_PAIR = re.compile(r"(?i)\bpassword\s*=\s*(?:'[^']*'|[^\s;]+)")
#: URI query password (``?password=…``)
_QUERY_PASSWORD = re.compile(r"(?i)([?&]password=)[^&\s]+")


def _redact(value: Any, cfg: Any = None) -> str:
    """One-line diagnostic that never carries the DSN or a password.

    The core already avoids printing a password in most errors, but this
    surface is public: a driver message might echo a connection string, so the
    known DSN and every of its encoded password forms are replaced.  Unknown
    URI userinfo and ``password=`` pairs are redacted too.
    """
    message = " ".join(str(value).split())[:300]
    message = _QUERY_PASSWORD.sub(r"\1***", message)
    message = _PASSWORD_PAIR.sub("password=***", message)
    message = _URI_PASSWORD.sub(r"\1***@", message)
    dsn = str(getattr(cfg, "dsn", "") or "")
    if dsn:
        if dsn in message:
            message = message.replace(dsn, config_mod.mask_dsn(dsn))
        try:
            password, _rest = dsn_mod.split_password(dsn, {})
        except dsn_mod.DsnError:
            password = ""
        if password:
            for variant in (password, quote(password, safe=""),
                            quote_plus(password)):
                if variant:
                    message = message.replace(variant, "***")
    return message


def _admit(connection: Any, cfg: Any) -> None:
    """Refuse a mediated storage and check the schema before returning it."""
    if storage.is_remote(connection) or getattr(
            storage.of(connection), "driver", "") == "mediated":
        raise UnsupportedStorage(
            "mediated or remote executor connections are not part of the "
            "public database surface")
    try:
        # Not deferred: the caller gets the connection only once the schema is
        # known to be usable, never after an error discovered on first write.
        db_mod.require_schema(connection)
    except db_mod.SchemaMissing as exc:
        raise SchemaRefused(
            "the agent-mesh schema is missing or incompatible on the configured "
            "database: run \"ameesh migrate\" first") from exc
    except db_mod.Unavailable as exc:
        raise DatabaseUnavailable(
            "database unavailable while checking the schema: %s"
            % _redact(exc, cfg)) from exc
    except db_mod.DbError as exc:
        raise SchemaRefused(
            "the agent-mesh schema cannot be verified: %s"
            % _redact(exc, cfg)) from exc


def connect() -> Any:
    """Open the deployment's connection and return it, the schema checked.

    No argument: the connection comes from the process's usual ameesh
    configuration.  The caller owns it and closes it with :meth:`close`; use
    it with :mod:`ameesh.public.mail`, not as SQL access to the core tables.

    Raises :class:`DatabaseUnavailable` (code ``unavailable``) when the
    configuration or the database cannot be opened, :class:`SchemaRefused`
    (code ``schema``) when the schema is absent, incompatible or cannot be
    verified, and :class:`UnsupportedStorage` (code ``unsupported``) for a
    mediated or remote executor connection.  No message carries the DSN or a
    password.
    """
    try:
        cfg = config_mod.load()
    except SystemExit as exc:
        raise DatabaseUnavailable(
            "configuration cannot be read: %s" % _redact(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — one stable public refusal
        raise DatabaseUnavailable(
            "configuration cannot be read: %s" % _redact(exc)) from exc

    if getattr(cfg, "backend", "") == "mediated":
        # The mediated backend is refused before opening anything: its
        # "connection" is a remote executor client, not the core database.
        raise UnsupportedStorage(
            "mediated or remote executor connections are not part of the "
            "public database surface")

    try:
        connection = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        raise DatabaseUnavailable(
            "database unavailable: %s" % _redact(exc, cfg)) from exc
    except db_mod.DbError as exc:
        # Writer admission reports an incompatible schema as a DbError before
        # the connection exists; that refusal belongs to the schema code.
        raise SchemaRefused(
            "the agent-mesh schema is missing or incompatible on the configured "
            "database") from exc
    except Exception as exc:  # noqa: BLE001 — one stable public refusal
        raise DatabaseUnavailable(
            "database unavailable: %s" % _redact(exc, cfg)) from exc

    try:
        _admit(connection, cfg)
    except BaseException:
        connection.close()
        raise
    return connection


__all__ = ["CONTRACT_VERSION", "connect", "DatabaseError", "DatabaseUnavailable",
           "SchemaRefused", "UnsupportedStorage"]
