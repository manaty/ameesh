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


# --------------------------------------------------------------------------
# numéros réservés (L122)
# --------------------------------------------------------------------------

#: chemin des migrations dans le dépôt ameesh
REPO_PATH = "src/ameesh/migrations"


def numbered(names) -> dict[int, list[str]]:
    """Numéro → fichiers `NNNN_*.sql` qui le portent (doublons visibles)."""
    out: dict[int, list[str]] = {}
    for name in names:
        match = _FILE_RE.match(os.path.basename(name))
        if match:
            out.setdefault(int(match.group(1)), []).append(os.path.basename(name))
    return out


def _git(repo: str, *args: str) -> str:
    import subprocess

    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def highest_known(repo: str | None = None, directory: str = MIGRATIONS_DIR) -> int:
    """Le plus haut numéro de migration connu : celles du paquet installé et,
    dans le dépôt `repo` (défaut : le dépôt git courant s'il contient
    `src/ameesh/migrations`), celles de l'arbre de travail et de TOUTES les
    branches locales et distantes — une PR ouverte compte, même sans
    réservation (lots antérieurs à L122). Rien n'est récupéré (pas de fetch)."""
    names = [n for n in os.listdir(directory)] if os.path.isdir(directory) else []
    top = _git(repo or os.getcwd(), "rev-parse", "--show-toplevel").strip()
    if top and os.path.isdir(os.path.join(top, REPO_PATH)):
        names += os.listdir(os.path.join(top, REPO_PATH))
        refs = _git(top, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes")
        for ref in refs.split():
            if ref.endswith("/HEAD"):
                continue
            names += _git(top, "ls-tree", "--name-only", ref, REPO_PATH + "/").split()
    return max(numbered(names) or [0])


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


def check(db: PsqlDriver | PsycopgDriver, directory: str = MIGRATIONS_DIR,
          ) -> tuple[list[Migration], list[Migration], list[int]]:
    """État des migrations en **lecture seule** (sonde `doctor --probe`).

    Contrairement à `applied()`, n'exécute aucun DDL (pas de `create table if
    not exists`) : la sonde ne doit rien écrire. Rend (manquantes, modifiées
    après application, versions appliquées inconnues de ce code — base plus
    récente que l'image, par exemple après un retour arrière).
    """
    done = _applied_readonly(db)
    known = discover(directory)
    missing = [m for m in known if m.version not in done]
    modified = [m for m in known if m.version in done and done[m.version] != m.checksum]
    versions = {m.version for m in known}
    unknown = sorted(v for v in done if v not in versions)
    return missing, modified, unknown


#: Gestes qui cassent le code d'avant sur une base migrée (L151) : il lit ou
#: écrit encore ce qui disparaît, change de type ou devient obligatoire. Une
#: migration qui n'en contient aucun est « additive » : on peut l'appliquer
#: AVANT d'installer le code qui l'apporte. Remplacer une contrainte (drop puis
#: add) reste additif : les migrations du dépôt ne le font que pour l'élargir.
_NON_ADDITIVE = (
    (re.compile(r"\bdrop\s+(table|column|schema|type)\b"), "suppression (drop %s)"),
    (re.compile(r"\brename\b"), "renommage"),
    (re.compile(r"\balter\s+column\s+\S+\s+(set\s+data\s+)?type\b"),
     "changement de type d'une colonne"),
    (re.compile(r"\bset\s+not\s+null\b"), "colonne rendue obligatoire (set not null)"),
    (re.compile(r"\btruncate\b"), "vidage (truncate)"),
    (re.compile(r"\bdelete\s+from\b"), "suppression de lignes (delete)"),
    (re.compile(r"\badd\s+column\s+(if\s+not\s+exists\s+)?[^,;]*?\bnot\s+null\b(?![^,;]*\bdefault\b)"),
     "colonne obligatoire sans défaut (add column … not null)"),
)
#: une vue ou une fonction supprimée puis recréée dans la même migration
#: (`r.*` figé à la création d'une vue) reste additive
_DROP_RECREATED = re.compile(
    r"\bdrop\s+(view|materialized\s+view|function)\s+(?:if\s+exists\s+)?([\w.\"]+)")
#: marqueur explicite, pour ce que l'examen du texte ne voit pas (une
#: contrainte resserrée, une fonction réécrite) : `-- ameesh: non additive`
_MARKER = re.compile(r"--\s*ameesh\s*:\s*non[- ]additive", re.IGNORECASE)


def non_additive(migration: Migration) -> list[str]:
    """Raisons pour lesquelles `migration` casserait le code d'avant ; vide si
    elle ne fait qu'ajouter (tables, colonnes nullables ou avec défaut, index,
    contraintes remplacées)."""
    reasons = []
    if _MARKER.search(migration.sql):
        reasons.append("marquée non additive")
    # commentaires retirés : une explication qui cite « drop » ne compte pas
    text = re.sub(r"--[^\n]*", " ", migration.sql)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"\s+", " ", text.lower())
    for pattern, label in _NON_ADDITIVE:
        match = pattern.search(text)
        if match:
            reasons.append(label % match.group(1) if "%s" in label else label)
    for match in _DROP_RECREATED.finditer(text):
        kind, name = match.group(1), match.group(2)
        recreated = re.compile(r"\bcreate\s+(?:or\s+replace\s+)?%s\s+%s\b"
                               % (r"\s+".join(kind.split()), re.escape(name)))
        if not recreated.search(text, match.end()):
            reasons.append("suppression (drop %s %s)" % (kind, name))
    return reasons

def _applied_readonly(db: PsqlDriver | PsycopgDriver) -> dict[int, str]:
    """Versions appliquées et leur empreinte, sans `create table` : vide si
    `schema_migrations` n'existe pas encore."""
    row = db.query("SELECT to_regclass('schema_migrations') IS NOT NULL AS ok")[0]
    if not row.get("ok"):
        return {}
    return {int(r["version"]): r["checksum"]
            for r in db.query("SELECT version, checksum FROM schema_migrations")}


def status(db: PsqlDriver | PsycopgDriver) -> tuple[list[Migration], set[int]]:
    """(migrations connues, versions appliquées) — pour `agent-mesh doctor`.

    Lecture seule (L146) : `doctor` n'écrit rien, pas même la table du
    registre des migrations (`applied()` la crée au besoin)."""
    return discover(), set(_applied_readonly(db))
