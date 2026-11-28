#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#8387 — au gel, les colonnes d'identité des migrations du diff sont classées.

`test_4299_agent_rename.test_every_identity_column_is_classified` lit le
schéma Postgres après les migrations : le gel, qui est sans base (#3687), ne
peut pas le jouer. Cette garde en donne la variante statique. Elle lit les
`CREATE TABLE` et `ALTER TABLE … ADD COLUMN` des migrations SQL du diff gelé
(`AMEESH_FREEZE_PATHS`), ne retient que les colonnes de type texte, tableau ou
`jsonb` (la requête de test_4299), applique `IDENTITY_COLUMNS` et refuse une
colonne absente de `REWRITTEN + CASCADED + KEPT + IGNORED` (`NOT_AGENTS`
compris), toutes lues dans `src/ameesh/agent_rename.py` de l'arbre contrôlé.

Les commentaires SQL, les chaînes et les corps `$$…$$` sont masqués avant
l'analyse : un `ADD COLUMN` cité dans un commentaire ne rougit pas, et un SQL
dynamique non lisible n'est pas deviné. Sans `AMEESH_FREEZE_PATHS` (lancement
manuel), toutes les migrations du dépôt sont lues : un doute ne devient jamais
un vert. Une variable illisible refuse (sortie 2), jamais un vert rassurant.

Usage : scripts/garde-colonnes-identite.py [--root REPO]
Sortie : 0 les colonnes sont classées, 1 au moins une ne l'est pas,
         2 lecture impossible (migrations, entrée du gel ou agent_rename).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass

#: #3687 : les chemins du diff gelé, posés par `ameesh.freeze_checks`
PATHS_ENV = "AMEESH_FREEZE_PATHS"
#: les migrations versionnées du produit
MIGRATIONS_DIR = "src/ameesh/migrations"
#: le module qui porte le classement (REWRITTEN/CASCADED/KEPT/IGNORED…)
RENAME_MODULE = "ameesh.agent_rename"

#: un début de définition de table
CREATE_TABLE = re.compile(
    r"\bCREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", re.I)
#: un début d'ALTER TABLE ; le corps est lu jusqu'au point-virgule
ALTER_TABLE = re.compile(r"\bALTER\s+TABLE\b", re.I)
#: un membre de corps de CREATE TABLE qui est une contrainte, pas une colonne
CONSTRAINT = re.compile(
    r"^(?:CONSTRAINT|PRIMARY\s+KEY|UNIQUE|CHECK|FOREIGN\s+KEY|EXCLUDE|LIKE)\b",
    re.I)
#: un type lu par test_4299 (`text`, `character varying`, `varchar`, `jsonb`)
TEXT_TYPE = re.compile(r"^(?:CHARACTER\s+VARYING|VARCHAR|TEXT|JSONB)\b", re.I)
#: un nom simple, pour qualifier une table (`public.foo`)
IDENTIFIER = re.compile(r'\s*(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_$]*))')
#: les mots qui terminent un type dans une définition de colonne
TYPE_END = re.compile(
    r"\b(?:DEFAULT|NOT\s+NULL|NULL|CONSTRAINT|CHECK|REFERENCES|COLLATE|"
    r"GENERATED|PRIMARY|UNIQUE)\b", re.I)
#: un `ADD` d'ALTER TABLE, avec ses qualificatifs facultatifs
ADD_ACTION = re.compile(
    r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_$]*))', re.I)
#: les actions `ADD` qui ne sont pas des colonnes
NOT_A_COLUMN = {
    "constraint", "primary", "unique", "check", "foreign", "exclude",
}


class GuardError(Exception):
    """Lecture impossible : le gel doit refuser, pas supposer."""


@dataclass(frozen=True)
class Finding:
    """Une colonne d'identité non classée, à sa ligne."""
    path: str
    line: int
    table: str
    column: str

    def render(self) -> str:
        return ("%s:%d: %s.%s is an identity column; classify it in %s "
                "(REWRITTEN/CASCADED/KEPT/IGNORED)"
                % (self.path, self.line, self.table, self.column, RENAME_MODULE))


def sanitize(sql: str) -> str:
    """Masque commentaires, chaînes et corps `$$…$$`, en gardant les offsets.

    Les identifiants entre guillemets doubles restent lisibles ; tout ce qui
    est masqué devient un espace (les sauts de ligne sont conservés) pour que
    les numéros de ligne restent ceux du fichier."""
    out = list(sql)
    size = len(sql)

    def blank(start: int, end: int) -> None:
        for index in range(start, min(end, size)):
            if out[index] != "\n":
                out[index] = " "

    index = 0
    while index < size:
        char = sql[index]
        if char == "-" and sql.startswith("--", index):
            end = sql.find("\n", index)
            end = size if end < 0 else end
            blank(index, end)
            index = end
        elif char == "/" and sql.startswith("/*", index):
            depth, end = 1, index + 2
            while end < size and depth:
                if sql.startswith("/*", end):
                    depth, end = depth + 1, end + 2
                elif sql.startswith("*/", end):
                    depth, end = depth - 1, end + 2
                else:
                    end += 1
            blank(index, end)
            index = end
        elif char == "'":
            end = index + 1
            while end < size:
                if sql[end] == "'":
                    if end + 1 < size and sql[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            blank(index, end)
            index = end
        elif char == '"':
            # Un identifiant cité : on le garde, mais ses `--` ou `/*` ne sont
            # pas des commentaires. Un `""` intérieur reste un guillemet.
            index += 1
            while index < size:
                if sql[index] == '"':
                    if index + 1 < size and sql[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
        elif char == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[index:])
            if not match:
                index += 1
                continue
            delimiter = match.group(0)
            end = sql.find(delimiter, index + len(delimiter))
            if end < 0:
                raise GuardError("unterminated $%s$ at offset %d"
                                 % (delimiter.strip("$"), index))
            end += len(delimiter)
            blank(index, end)
            index = end
        else:
            index += 1
    return "".join(out)


def line_of(sql: str, offset: int) -> int:
    return sql.count("\n", 0, offset) + 1


def split_top(body: str) -> list[tuple[int, str]]:
    """Découpe un corps SQL sur les virgules de premier niveau, offsets compris."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(body):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append((start, body[start:index]))
            start = index + 1
    parts.append((start, body[start:]))
    return parts


def read_identifier(sql: str, offset: int) -> tuple[str, int]:
    """Lit un identifiant cité ou nu à `offset`, et rend `(nom, fin)`.

    Un identifiant nu est replié en minuscules, comme Postgres le fait."""
    match = IDENTIFIER.match(sql, offset)
    if not match:
        raise GuardError("identifier expected at offset %d" % offset)
    if match.group(1) is not None:
        return match.group(1).replace('""', '"'), match.end()
    return match.group(2).lower(), match.end()


def read_table(sql: str, offset: int) -> tuple[str, int]:
    """Lit un nom de table, qualification `schéma.table` comprise (dernier nom)."""
    name, offset = read_identifier(sql, offset)
    while True:
        dot = re.match(r"\s*\.", sql[offset:])
        if not dot:
            return name, offset
        name, offset = read_identifier(sql, offset + dot.end())


def type_is_identity(definition: str) -> bool:
    """Le type est-il lu par test_4299 (texte, tableau, `jsonb`) ?"""
    definition = definition.lstrip()
    if TEXT_TYPE.match(definition):
        return True
    head = TYPE_END.split(definition, maxsplit=1)[0]
    return head.rstrip().endswith("[]")


def _create_columns(sql: str) -> list[Finding]:
    out = []
    for start in CREATE_TABLE.finditer(sql):
        table, offset = read_table(sql, start.end())
        while offset < len(sql) and sql[offset].isspace():
            offset += 1
        if offset >= len(sql) or sql[offset] != "(":
            continue
        body_start, depth, index = offset + 1, 1, offset + 1
        while index < len(sql) and depth:
            if sql[index] == "(":
                depth += 1
            elif sql[index] == ")":
                depth -= 1
            index += 1
        body = sql[body_start:index - 1]
        for part_offset, part in split_top(body):
            text = part.lstrip()
            if not text or CONSTRAINT.match(text):
                continue
            column_offset = body_start + part_offset + (len(part) - len(text))
            try:
                column, end = read_identifier(sql, column_offset)
            except GuardError:
                continue
            if type_is_identity(sql[end:body_start + part_offset + len(part)]):
                out.append(Finding("", line_of(sql, column_offset), table, column))
    return out


def _alter_columns(sql: str) -> list[Finding]:
    out = []
    for start in ALTER_TABLE.finditer(sql):
        offset = start.end()
        only = re.match(r"\s*ONLY\b", sql[offset:], re.I)
        if only:
            offset += only.end()
        exists = re.match(r"\s*IF\s+EXISTS\b", sql[offset:], re.I)
        if exists:
            offset += exists.end()
        try:
            table, offset = read_table(sql, offset)
        except GuardError:
            continue
        end = sql.find(";", offset)
        end = len(sql) if end < 0 else end
        actions = sql[offset:end]
        for part_offset, part in split_top(actions):
            # #8387 B1 : une action à la fois. Lire le type jusqu'au bout de
            # l'ALTER laissait le `ADD` suivant contaminer la définition
            # (`bigint[], ADD COLUMN note integer` ignoré ; `bigint, ADD
            # COLUMN note text[]` retenu à tort).
            definition = part.lstrip()
            lead = len(part) - len(definition)
            add = ADD_ACTION.match(definition)
            if not add:
                continue
            quoted = add.group(1) is not None
            column = add.group(1) if quoted else add.group(2).lower()
            if not quoted and column in NOT_A_COLUMN:
                continue
            if type_is_identity(definition[add.end():]):
                out.append(Finding("", line_of(sql, offset + part_offset + lead),
                                   table, column))
    return out


def text_columns(sql: str, path: str = "") -> list[Finding]:
    """Les colonnes de type texte / tableau / jsonb, dans l'ordre des lignes."""
    clean = sanitize(sql)
    found = _create_columns(clean) + _alter_columns(clean)
    return sorted((Finding(path, item.line, item.table, item.column)
                   for item in found), key=lambda item: item.line)


def classification(root: str) -> tuple[re.Pattern, set[str]]:
    """`(IDENTITY_COLUMNS, colonnes classées)` lus dans l'arbre contrôlé."""
    source = os.path.join(root, "src")
    # l'arbre contrôlé passe devant un `ameesh` installé ou déjà sur le chemin
    if sys.path[:1] != [source]:
        sys.path.insert(0, source)
    try:
        from ameesh import agent_rename
    except Exception as exc:  # noqa: BLE001 - toute lecture ratée est un doute
        raise GuardError("cannot read %s: %s" % (RENAME_MODULE, exc)) from exc
    known = {column.rstrip("[]") for column in
             agent_rename.REWRITTEN + agent_rename.CASCADED
             + agent_rename.KEPT}
    known |= set(agent_rename.IGNORED) | set(agent_rename.NOT_AGENTS)
    return agent_rename.IDENTITY_COLUMNS, known


def read_paths(environ) -> list[str] | None:
    """Les chemins du diff gelé, ou None hors du gel (variable absente)."""
    raw = environ.get(PATHS_ENV)
    if raw is None:
        return None
    try:
        paths = json.loads(raw)
    except ValueError as exc:
        raise GuardError("%s is not JSON: %s" % (PATHS_ENV, exc)) from exc
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise GuardError("%s is not a list of paths" % PATHS_ENV)
    return paths


def migration_files(root: str, paths: list[str] | None) -> list[str]:
    """Les migrations à lire : celles du diff, ou toutes hors du gel."""
    if paths is None:
        return sorted(os.path.relpath(path, root).replace(os.sep, "/")
                      for path in glob.glob(os.path.join(
                          root, MIGRATIONS_DIR, "*.sql")))
    out = []
    for path in paths:
        if not path.startswith(MIGRATIONS_DIR + "/") or not path.endswith(".sql"):
            continue
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise GuardError("unsafe migration path: %r" % path)
        out.append(path)
    return sorted(set(out))


def check(root: str, paths: list[str] | None, identity: re.Pattern,
          known: set[str]) -> list[Finding]:
    findings = []
    for relative in migration_files(root, paths):
        full = os.path.join(root, *relative.split("/"))
        try:
            with open(full, encoding="utf-8") as handle:
                sql = handle.read()
        except OSError as exc:
            raise GuardError("cannot read %s: %s" % (relative, exc)) from exc
        findings.extend(item for item in text_columns(sql, relative)
                        if identity.match(item.column)
                        and item.table + "." + item.column not in known)
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="racine de l'arbre ameesh")
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root)
    if not os.path.isdir(os.path.join(root, "src", "ameesh")):
        print("gard: %s is not an ameesh tree" % root, file=sys.stderr)
        return 2
    try:
        paths = read_paths(os.environ)
        files = migration_files(root, paths)
        if not files:
            print("-- identity columns #4299: no migration in the diff")
            return 0
        identity, known = classification(root)
        findings = check(root, paths, identity, known)
    except GuardError as exc:
        print("gard: %s" % exc, file=sys.stderr)
        return 2
    for finding in findings:
        print(finding.render())
    if findings:
        print("%d unclassified identity column(s) in %d migration(s); "
              "classify them in %s"
              % (len(findings), len(files), RENAME_MODULE))
        return 1
    print("ok identity columns #4299: %d migration(s), nothing unclassified"
          % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
