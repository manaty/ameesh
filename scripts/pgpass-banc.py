#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#2170 : le profil de connexion du banc, transmis à la base jetable.

    scripts/pgpass-banc.py <DSN d'administration> <fichier>

Les tests créent des bases jetables (`agent_mesh_test_*` depuis #1992, mais
aussi `t_executor_db_*` ou d'autres noms) sur le même serveur que la base
d'administration. Quand le mot de passe du banc vient de `~/.pgpass` (ou de
`PGPASSFILE`), libpq ne le trouve pas pour ces nouveaux noms de base : le
profil existant ne couvre souvent que `agent_mesh`. Ce script lit la première
entrée du profil qui correspond à la connexion d'administration, puis écrit un
`PGPASSFILE` privé (mode 0600) que `scripts/test.sh` passe aux deux pilotes.

Le profil privé couvre le couple hôte/port du banc et l'utilisateur de
l'entrée, avec le champ base `*` : c'est la seule forme que libpq offre pour
des noms de bases tirés à chaque exécution. Pendant les deux passes, il est le
seul profil que lit libpq : les autres entrées du profil de l'utilisateur sont
masquées, ce qui est voulu (même hôte, même port, même rôle). Il ne copie
aucune autre entrée du profil et n'accorde aucun droit côté serveur ; le rôle
du banc les avait déjà. Il est supprimé à la sortie du script de test. Aucun
secret n'est affiché ; le fichier source doit être un fichier ordinaire non
lisible par le groupe ni les autres, comme libpq l'exige.
"""
from __future__ import annotations

import getpass
import os
import stat
import sys
from urllib.parse import unquote, urlsplit

USAGE = "usage : pgpass-banc.py <DSN d'administration> <fichier>"


class Refus(Exception):
    """Le profil ne peut pas être transmis ; le message ne cite aucun secret."""


def parse_dsn(dsn: str) -> tuple[str, str, str, str]:
    """(hôte, port, utilisateur, base) d'un DSN URI du banc."""
    if not dsn.startswith(("postgresql://", "postgres://")):
        raise Refus("le DSN d'administration doit être une URI postgresql://")
    try:
        parts = urlsplit(dsn)
        port = str(parts.port or 5432)
    except ValueError as exc:
        raise Refus("port invalide dans le DSN d'administration") from exc
    host = parts.hostname or ""
    if not host or host.startswith("/"):
        raise Refus("hôte TCP absent du DSN d'administration")
    user = unquote(parts.username or getpass.getuser())
    database = unquote(parts.path[1:]) or user
    return host, port, user, database


def _split(line: str) -> list[str] | None:
    """Les cinq champs d'une ligne de profil, ou None si elle est incomplète.

    Les quatre premiers `:` séparent les champs ; le reste est le mot de
    passe, où un `:` non échappé est conservé (libpq le lit ainsi)."""
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":" and len(fields) < 4:
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    if escaped:
        current.append("\\")
    if len(fields) != 4:
        return None
    fields.append("".join(current))
    return fields


def _match(pattern: str, value: str) -> bool:
    return pattern == "*" or pattern == value


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:")


def _read_passfile(path: str) -> list[str]:
    try:
        info = os.stat(path)
    except OSError as exc:
        raise Refus("profil de connexion illisible : %s" % (exc.strerror or path)) from exc
    if not stat.S_ISREG(info.st_mode):
        raise Refus("le profil de connexion n'est pas un fichier ordinaire")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise Refus("le profil de connexion est accessible au groupe ou aux autres")
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as stream:
            return [line.rstrip("\r\n") for line in stream]
    except OSError as exc:
        raise Refus("profil de connexion illisible : %s" % (exc.strerror or path)) from exc


def entry_for(lines: list[str], host: str, port: str, user: str, database: str) -> list[str] | None:
    """La première entrée qui couvre la connexion d'administration."""
    for line in lines:
        if not line or line.startswith("#"):
            continue
        fields = _split(line)
        if fields is None:
            continue
        if (_match(fields[0], host) and _match(fields[1], port)
                and _match(fields[2], database) and _match(fields[3], user)):
            return fields
    return None


def write_passfile(path: str, host: str, port: str, user: str, password: str) -> None:
    """Une seule entrée, pour les bases du banc, en 0600."""
    if any(char in password for char in "\r\n\0"):
        raise Refus("le mot de passe ne peut pas tenir sur une ligne de profil")
    line = ":".join(_escape(value) for value in (host, port, "*", user, password)) + "\n"
    descriptor = -1
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.chmod(path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", errors="surrogateescape") as stream:
            descriptor = -1
            stream.write(line)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if created:                 # jamais un fichier partiel avec un secret
            try:
                os.unlink(path)
            except OSError:
                pass
        raise


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    admin_dsn, output = args
    source = os.environ.get("PGPASSFILE") or os.path.expanduser("~/.pgpass")
    try:
        host, port, user, admin_database = parse_dsn(admin_dsn)
        fields = entry_for(_read_passfile(source), host, port, user, admin_database)
        if fields is None:
            print("aucune entrée du profil ne couvre la base d'administration %s"
                  % admin_database, file=sys.stderr)
            return 3
        password = fields[4]
        if not password:
            print("l'entrée du profil n'a pas de mot de passe", file=sys.stderr)
            return 3
        write_passfile(output, host, port, user, password)
    except Refus as exc:
        print("pgpass-banc : %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("pgpass-banc : écriture du profil impossible : %s"
              % (exc.strerror or exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
