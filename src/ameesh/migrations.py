# SPDX-License-Identifier: AGPL-3.0-only
"""Migrations versionnées : `NNNN_nom.sql`, appliquées une fois, vérifiées.

Chaque fichier est immuable : son empreinte SHA-256 est enregistrée dans
`schema_migrations`. Modifier une migration déjà appliquée est une erreur, pas
une mise à jour silencieuse. Une passe entière est atomique et protégée par
un verrou consultatif de la base, acquis avant la lecture de l'inventaire.
Le verrou et les écritures partagent la même transaction et connexion.
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
            raise DbError("duplicate migration version: %04d" % version)
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
        raise DbError("no migration in %s" % directory)
    return [found[v] for v in sorted(found)]


# --------------------------------------------------------------------------
# numéros réservés (L122)
# --------------------------------------------------------------------------

#: chemin des migrations dans le dépôt ameesh
REPO_PATH = "src/ameesh/migrations"
#: le dépôt d'ameesh lui-même : seul à compter aussi les migrations du paquet
AMEESH_REPO = "manaty/ameesh"
#: un numéro pris dans un dépôt : tout nom qui commence par des chiffres et `_`,
#: quel que soit le reste (`377_receipt+fix.sql`, `360_restore.sql`)
_REPO_FILE_RE = re.compile(r"^(\d+)_")
#: `owner/repo` GitHub, sans le suffixe `.git`
_SLUG_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
#: l'URL courte ssh : `[user@]github.com:owner/repo[.git]`
_SCP_RE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?([A-Za-z0-9.-]+):(?!/)(.+)$")


@dataclass(frozen=True)
class Layout:
    """Où un dépôt range ses migrations, et sur combien de chiffres (#443)."""

    repo: str
    directory: str
    width: int = 4

    def label(self, number: int) -> str:
        return "%0*d" % (self.width, number)

    def file(self, number: int) -> str:
        return "%s/%s_<name>.sql" % (self.directory, self.label(number))


#: dépôts connus ; un autre se déclare dans `radar.projects.<p>` (`github`,
#: `migrations`) de la configuration de l'hôte
LAYOUTS = {
    AMEESH_REPO: Layout(AMEESH_REPO, REPO_PATH, 4),
    "manaty/nexlink": Layout("manaty/nexlink", "supabase/migrations", 3),
}


def repo_key(repo: str | None) -> str:
    """La clé d'un dépôt `owner/repo` : sans espaces, en minuscules (GitHub ne
    distingue pas la casse ; une seule suite par dépôt)."""
    return (repo or "").strip().lower()


def layout_for(repo: str, cfg=None) -> Layout | None:
    """La disposition des migrations du dépôt `owner/repo`, ou None."""
    repo = repo_key(repo)
    known = LAYOUTS.get(repo)
    for project in _radar_projects(cfg):
        if repo_key(project.github) == repo and project.migrations:
            return Layout(repo, project.migrations, known.width if known else 4)
    return known


def _radar_projects(cfg) -> list:
    if cfg is None:
        return []
    from . import radar

    try:
        return list(radar.settings_of(cfg).projects.values())
    except radar.SettingsError:
        return []


def checkout_for(repo: str, cfg=None) -> str | None:
    """Un clone local de `repo` déclaré à l'hôte (`radar.projects.<p>.repo`)."""
    for project in _radar_projects(cfg):
        if repo_key(project.github) == repo_key(repo) and os.path.isdir(project.repo):
            return project.repo
    return None


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


def github_repo(url: str) -> str | None:
    """Le dépôt `owner/repo` (minuscules) d'une URL git dont l'hôte est
    exactement github.com : https, ssh:// ou forme courte `git@github.com:`.
    None pour tout autre hôte ou chemin (pas de recherche de sous-chaîne)."""
    from urllib.parse import urlsplit

    url = (url or "").strip()
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http", "ssh", "git"):
            return None
        host, path = (parts.hostname or ""), parts.path.lstrip("/")
    else:
        match = _SCP_RE.match(url)
        if not match:
            return None
        host, path = match.group(1), match.group(2)
    if host.lower() != "github.com":
        return None
    match = _SLUG_RE.match(path)
    return repo_key("%s/%s" % match.groups()) if match else None


def origin_repo(checkout: str) -> str | None:
    """Le dépôt GitHub `owner/repo` de l'`origin` d'un clone, ou None."""
    return github_repo(_git(checkout, "remote", "get-url", "origin"))


def toplevel(path: str) -> str:
    return _git(path, "rev-parse", "--show-toplevel").strip()


def repo_numbers(checkout: str, layout: Layout, *, skip_branch: str | None = None,
                 package: str = MIGRATIONS_DIR) -> dict[int, list[str]]:
    """Numéro → endroits où il existe dans le dépôt : l'arbre de travail et
    TOUTES les branches locales et distantes du clone (une PR ouverte compte,
    même sans réservation) ; pour ameesh, le paquet installé aussi. Les refs
    de la branche `skip_branch` (celle du lot) ne comptent pas, ni l'arbre de
    travail s'il est sur elle. Rien n'est récupéré (pas de fetch)."""
    found: dict[int, list[str]] = {}

    def add(names, where):
        for name in names:
            match = _REPO_FILE_RE.match(os.path.basename(name))
            if match:
                found.setdefault(int(match.group(1)), []).append(
                    "%s:%s" % (where, os.path.basename(name)))

    if layout.repo == AMEESH_REPO and os.path.isdir(package):
        add(os.listdir(package), "package")
    skip = {"refs/heads/" + skip_branch} if skip_branch else set()
    head = _git(checkout, "symbolic-ref", "-q", "HEAD").strip()
    directory = os.path.join(checkout, layout.directory)
    if os.path.isdir(directory) and head not in skip:
        add(os.listdir(directory), "worktree")
    refs = _git(checkout, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes")
    for ref in refs.split():
        if ref.endswith("/HEAD") or ref in skip or (
                skip_branch and ref.startswith("refs/remotes/")
                and ref.split("/", 3)[-1] == skip_branch):
            continue
        add(_git(checkout, "ls-tree", "--name-only", ref, layout.directory + "/").split(),
            ref.split("/", 2)[-1])
    return found


def applied(db: PsqlDriver | PsycopgDriver) -> dict[int, str]:
    db.execute(BOOTSTRAP)
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
                "migration %s modified after being applied (checksum %s != %s): "
                "an applied migration is immutable, add a new one"
                % (migration.label, done[migration.version][:12], migration.checksum[:12])
            )
    return out


class NonAdditiveMigration(DbError):
    """The elected release worker found a migration outside its guard."""


def migrate(db: PsqlDriver | PsycopgDriver, directory: str = MIGRATIONS_DIR,
            log=None, *, additive_only: bool = False) -> list[Migration]:
    """Elect one database worker, re-read inventory and commit the whole pass.

    PostgreSQL scopes the existing lock key to the actual database, regardless
    of schema, host or DSN alias. Retaining this key also excludes older workers
    while they execute their individually locked migrations. Those older
    workers must still be upgraded before relying on inventory reconciliation.
    A separate lock connection could disappear while its writer continued;
    this transaction owns both the lock and every migration write instead.
    """
    with db.transaction() as tx:
        # The configured statement timeout bounds election. Only the elected
        # worker lifts it for migration SQL, as the previous implementation did.
        tx.query("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LOCK,))
        tx.execute("SET LOCAL statement_timeout = 0")
        if db.cfg.schema != "public":
            tx.execute("CREATE SCHEMA IF NOT EXISTS %s" % quote_ident(db.cfg.schema))
        todo = pending(tx, directory)
        if additive_only and any(non_additive(migration) for migration in todo):
            raise NonAdditiveMigration("nothing applied (--additive-only)")
        for migration in todo:
            tx.script(migration.sql)
            tx.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (%s,%s,%s)",
                       (migration.version, migration.name, migration.checksum))
    # Neither a failed SQL statement nor an uncertain COMMIT publishes success.
    if log:
        for migration in todo:
            log("%s applied" % migration.label)
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
    (re.compile(r"\bdrop\s+(table|column|schema|type)\b"), "removal (drop %s)"),
    (re.compile(r"\brename\b"), "rename"),
    (re.compile(r"\balter\s+column\s+\S+\s+(set\s+data\s+)?type\b"),
     "column type change"),
    (re.compile(r"\bset\s+not\s+null\b"), "column made mandatory (set not null)"),
    (re.compile(r"\btruncate\b"), "table emptied (truncate)"),
    (re.compile(r"\bdelete\s+from\b"), "row deletion (delete)"),
    (re.compile(r"\badd\s+column\s+(if\s+not\s+exists\s+)?[^,;]*?\bnot\s+null\b(?![^,;]*\bdefault\b)"),
     "mandatory column without default (add column ... not null)"),
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
        reasons.append("marked non-additive")
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
            reasons.append("removal (drop %s %s)" % (kind, name))
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
