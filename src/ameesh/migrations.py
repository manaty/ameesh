# SPDX-License-Identifier: AGPL-3.0-only
"""Migrations versionnées : `NNNN_nom.sql`, appliquées une fois, vérifiées.

Chaque fichier est immuable : son empreinte SHA-256 est enregistrée dans
`schema_migrations`. Modifier une migration déjà appliquée est une erreur, pas
une mise à jour silencieuse. L'application d'un fichier est atomique
(`psql -1` / transaction psycopg) et protégée par un verrou consultatif, pour
que deux exécuteurs qui démarrent en même temps ne se marchent pas dessus.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from .db import DbError, PsqlDriver, PsycopgDriver, quote_ident

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
_FILE_RE = re.compile(r"^(\d{4})_([A-Za-z0-9._-]+)\.sql$")
_LOCK = "ameesh_migrations"

BOOTSTRAP = """
create table if not exists schema_migrations (
    version    integer primary key,
    name       text not null,
    checksum   text not null,
    applied_at timestamptz not null default now()
)
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: str
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return "%04d_%s" % (self.version, self.name)


def discover(directory: str = MIGRATIONS_DIR) -> list[Migration]:
    found: dict[int, Migration] = {}
    for filename in sorted(os.listdir(directory)):
        match = _FILE_RE.match(filename)
        if not match:
            continue
        version = int(match.group(1))
        if version in found:
            raise DbError("version de migration en double : %04d" % version)
        path = os.path.join(directory, filename)
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
        found[version] = Migration(
            version=version,
            name=match.group(2),
            path=path,
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )
    if not found:
        raise DbError("aucune migration dans %s" % directory)
    return [found[v] for v in sorted(found)]


def applied(db: PsqlDriver | PsycopgDriver) -> dict[int, str]:
    db.script(BOOTSTRAP)
    rows = db.query("SELECT version, checksum, name FROM schema_migrations ORDER BY version")
    return {int(row["version"]): row["checksum"] for row in rows}


def pending(db: PsqlDriver | PsycopgDriver, directory: str = MIGRATIONS_DIR) -> list[Migration]:
    done = applied(db)
    out = []
    for migration in discover(directory):
        if migration.version not in done:
            out.append(migration)
        elif done[migration.version] != migration.checksum:
            raise DbError(
                "migration %s modifiée après application (empreinte %s != %s) : "
                "une migration appliquée est immuable, ajoutez-en une nouvelle"
                % (migration.label, done[migration.version][:12], migration.checksum[:12])
            )
    return out


def migrate(db: PsqlDriver | PsycopgDriver, directory: str = MIGRATIONS_DIR, log=None) -> list[Migration]:
    """Applique les migrations manquantes ; renvoie celles qui viennent de l'être."""
    if db.cfg.schema != "public":
        # `SET search_path` accepte un schéma inexistant : on le crée ici,
        # explicitement, plutôt que d'échouer sur un « no schema has been
        # selected to create in ».
        db.execute("CREATE SCHEMA IF NOT EXISTS %s" % quote_ident(db.cfg.schema))
    todo = pending(db, directory)
    for migration in todo:
        script = (
            # Une migration peut être longue : on lève le délai le temps de la
            # transaction (SET LOCAL retombe au commit), sinon le
            # statement_timeout par défaut la couperait.
            "SET LOCAL statement_timeout = 0;\n"
            "select pg_advisory_xact_lock(hashtext('%s'));\n%s\n"
            "insert into schema_migrations (version, name, checksum) values (%d, %s, %s);\n"
            % (
                _LOCK,
                migration.sql,
                migration.version,
                _lit(migration.name),
                _lit(migration.checksum),
            )
        )
        db.script(script)
        if log:
            log("%s appliquée" % migration.label)
    return todo


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def status(db: PsqlDriver | PsycopgDriver) -> tuple[list[Migration], set[int]]:
    """(migrations connues, versions appliquées) — pour `agent-mesh doctor`."""
    return discover(), set(applied(db))
