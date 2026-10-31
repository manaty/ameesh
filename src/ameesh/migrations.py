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
import contextvars
import functools
import json
import os
import re
from dataclasses import dataclass

from .db import DbError, Unavailable, PsqlDriver, PsycopgDriver, quote_ident

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
_FILE_RE = re.compile(r"^(\d{4})_([A-Za-z0-9._-]+)\.sql$")
_LOCK = "ameesh_migrations"
#: Attente maximale d'un verrou (de table comme de ligne) par une migration
#: (L835) : la transaction lève le statement_timeout (une migration peut être
#: longue), mais une migration qui attend ACCESS EXCLUSIVE fait la queue devant
#: tous les lecteurs et écrivains de la table — sans borne, elle les bloque
#: indéfiniment. Passé ce délai, la transaction est annulée : la migration
#: échoue, reste en attente, et rien n'est enregistré (le DDL est
#: transactionnel). `lock_timeout` vaut pour toute acquisition de verrou, y
#: compris une ligne tenue par une autre transaction (`select … for update`) :
#: une migration de données qui doit attendre une ligne pose donc son propre
#: `set local lock_timeout`, qui l'emporte — sinon elle échoue à 5 s.
#:
#: Le verrou consultatif qui sérialise les migrations, lui, n'est pas borné
#: (posé avant ce réglage) : deux `migrate` concurrents s'attendent au lieu de
#: s'échouer l'un l'autre. Cette attente dure ce que dure la migration qui
#: tient le verrou — une migration peut être longue (statement_timeout levé) ;
#: c'est voulu, LOCK_TIMEOUT ne la borne pas.
LOCK_TIMEOUT = "5s"

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
#: `migrations`, `migrations_width`) de la configuration de l'hôte
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
            return Layout(repo, project.migrations, width_of(project, known))
    return known


def width_of(project, known: Layout | None = None) -> int:
    """#4750 : la largeur déclarée (`migrations_width`), sinon celle du dépôt
    connu, sinon 4."""
    return (getattr(project, "migrations_width", 0) or (known.width if known else 4))


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


#: branches de tronc qu'un scan ne peut jamais ignorer (#1603), en plus de
#: celle que désigne `origin/HEAD`
TRUNK_BRANCHES = ("main", "master", "develop")


def trunk_branches(checkout: str) -> set[str]:
    """Les branches de tronc d'un clone : `TRUNK_BRANCHES` et la branche par
    défaut de son `origin` (`refs/remotes/origin/HEAD`) quand elle est connue."""
    default = _git(checkout, "symbolic-ref", "-q", "refs/remotes/origin/HEAD").strip()
    names = set(TRUNK_BRANCHES)
    if default.startswith("refs/remotes/origin/"):
        names.add(default[len("refs/remotes/origin/"):])
    return names


def toplevel(path: str) -> str:
    return _git(path, "rev-parse", "--show-toplevel").strip()


def _trunk_refs(checkout: str, refs: list[str]) -> set[str]:
    """#4097 : les refs de tronc du clone, locales ou `origin/`."""
    trunks = trunk_branches(checkout)
    return {ref for ref in refs for prefix in ("refs/heads/", "refs/remotes/origin/")
            if ref.startswith(prefix) and ref[len(prefix):] in trunks}


def _landed_refs(checkout: str, refs: list[str], trunk_refs: set[str]) -> set[str]:
    """#4097 : les refs (hors tronc) déjà contenues dans une ref de tronc.
    Une lecture illisible ne retire rien."""
    landed: set[str] = set()
    for trunk in sorted(trunk_refs):
        landed.update(_git(checkout, "for-each-ref", "--format=%(refname)", "--merged",
                           trunk, "refs/heads", "refs/remotes").split())
    return landed - trunk_refs


def repo_numbers(checkout: str, layout: Layout, *, skip_branch: str | None = None,
                 also_skip: tuple[str, ...] = (), package: str = MIGRATIONS_DIR,
                 worktree: bool = True) -> dict[int, list[str]]:
    """Numéro → endroits où il existe dans le dépôt : l'arbre de travail et
    TOUTES les branches locales et distantes du clone (une PR ouverte compte,
    même sans réservation) ; pour ameesh, le paquet installé aussi. Les refs
    de la branche `skip_branch` (celle du lot) et de `also_skip` (les branches
    à ignorer explicitement, p. ex. la branche d'intégration d'un paquet en
    composition, #1603) ne comptent pas, ni l'arbre de travail s'il est sur
    l'une d'elles. `worktree=False` ignore l'arbre de travail entier : le train
    y compose un membre dont la migration n'est pas encore réservée (#2400).
    #4097 : une branche déjà entrée dans un tronc (ancêtre de `develop`,
    `main`…) ne compte pas : ses migrations ont atterri sur le tronc, peut-être
    sous un autre numéro (un paquet renumérote 602 → 599), et la branche
    d'agent restée sur la forge tenait encore l'ancien. Le tronc, lui, est
    toujours lu.
    Rien n'est récupéré (pas de fetch)."""
    found: dict[int, list[str]] = {}

    def add(names, where):
        for name in names:
            match = _REPO_FILE_RE.match(os.path.basename(name))
            if match:
                found.setdefault(int(match.group(1)), []).append(
                    "%s:%s" % (where, os.path.basename(name)))

    if layout.repo == AMEESH_REPO and os.path.isdir(package):
        add(os.listdir(package), "package")
    names = {name for name in (skip_branch, *also_skip) if name}
    skip = {"refs/heads/" + name for name in names}
    head = _git(checkout, "symbolic-ref", "-q", "HEAD").strip()
    directory = os.path.join(checkout, layout.directory)
    refs = _git(checkout, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes")
    trunk_refs = _trunk_refs(checkout, refs.split())
    if worktree and os.path.isdir(directory) and head not in skip:
        local = os.listdir(directory)
        if trunk_refs and _git(checkout, "for-each-ref", "--contains", "HEAD",
                               "--format=%(refname)", *sorted(trunk_refs)).strip():
            # #4097 : HEAD déjà entré dans un tronc — ses fichiers commités y
            # sont (peut-être renumérotés) ; seuls les ajouts locaux comptent
            committed = {os.path.basename(name) for name in _git(
                checkout, "ls-tree", "--name-only", "HEAD", layout.directory + "/").split()}
            local = [name for name in local if name not in committed]
        add(local, "worktree")
    skip |= _landed_refs(checkout, refs.split(), trunk_refs)
    for ref in refs.split():
        if ref.endswith("/HEAD") or ref in skip or (
                names and ref.startswith("refs/remotes/")
                and ref.split("/", 3)[-1] in names):
            continue
        add(_git(checkout, "ls-tree", "--name-only", ref, layout.directory + "/").split(),
            ref.split("/", 2)[-1])
    return found


#: préfixes d'une cible qui nomment sa branche (`origin/x`, `refs/…/x`), #2620
_TARGET_PREFIXES = ("refs/heads/", "refs/remotes/origin/", "origin/")


def target_numbers(checkout: str, layout: Layout, target: str) -> dict[int, list[str]] | None:
    """Numéro → fichiers que porte la **branche cible** seule (#2620).

    L'unicité d'un numéro de migration est un invariant de la cible : une
    branche non fusionnée qui porte un numéro ne l'a pas fait atterrir, et un
    paquet la renumérote à sa fusion (#2400). Refuser une réservation à numéro
    exact pour un fichier absent de la cible rendait le geste impossible
    (#2494 refusait `524`, porté par la branche non fusionnée #2299). On ne lit
    donc que la cible — `origin/<branche>` publiée d'abord (un clone partagé
    garde sa branche locale figée alors que les agents poussent, #1333), la
    locale à défaut — jamais l'arbre de travail, le paquet installé, la branche
    du lot ni une autre branche. Rend None quand aucune ref de la cible
    n'existe (l'appelant le dit) ; rien n'est récupéré (pas de fetch)."""
    resolved = target_commit(checkout, target)
    if resolved is None:
        return None
    label, commit = resolved
    found: dict[int, list[str]] = {}
    for name in tree_files(checkout, layout, commit):
        found.setdefault(int(_REPO_FILE_RE.match(name).group(1)), []).append(
            "%s:%s" % (label, name))
    return found


def target_commit(checkout: str, target: str) -> tuple[str, str] | None:
    """`(libellé, commit)` de la branche cible : `origin/<branche>` publiée
    d'abord, la locale à défaut (#1333, #2620) ; None si aucune n'existe."""
    text = (target or "").strip()
    if not text:
        return None
    name = text
    for prefix in _TARGET_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for label, ref in (("origin/" + name, "refs/remotes/origin/" + name),
                       (name, "refs/heads/" + name),
                       (text, text)):
        commit = _git(checkout, "rev-parse", "--verify", "--quiet",
                      ref + "^{commit}").strip()
        if commit:
            return label, commit
    return None


def tree_files(checkout: str, layout: Layout, commit: str) -> dict[str, str]:
    """#4100 : nom → blob des fichiers numérotés du dossier de migrations au
    commit `commit` (vide si le dossier manque)."""
    found: dict[str, str] = {}
    for line in _git(checkout, "ls-tree", commit, layout.directory + "/").splitlines():
        meta, _, path = line.partition("\t")
        name = os.path.basename(path)
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and _REPO_FILE_RE.match(name):
            found[name] = parts[2]
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
            log=None, *, additive_only: bool = False, allow_live_executors: str | None = None) -> list[Migration]:
    """Elect one database worker, re-read inventory and commit the whole pass.

    PostgreSQL scopes the existing lock key to the actual database, regardless
    of schema, host or DSN alias. Retaining this key also excludes older workers
    while they execute their individually locked migrations. Those older
    workers must still be upgraded before relying on inventory reconciliation.
    A separate lock connection could disappear while its writer continued;
    this transaction owns both the lock and every migration write instead.
    """
    if allow_live_executors is not None and not allow_live_executors.strip():
        raise ValueError('--allow-live-executors requires a nonempty reason')
    with db.transaction() as tx:
        # The lease snapshot must be taken after the relation lock, even if
        # the operator's connection defaults to REPEATABLE READ.
        tx.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        # The configured statement timeout bounds election. Only the elected
        # worker lifts it for migration SQL, as the previous implementation did.
        tx.query("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LOCK,))
        tx.execute("SET LOCAL statement_timeout = 0")
        # ... puis on borne l'attente des verrous de table (L835) : sans cela
        # une migration qui attend ACCESS EXCLUSIVE bloque derrière elle tous
        # les lecteurs et écrivains de la table. Posé après le verrou
        # consultatif : celui-ci doit pouvoir attendre son tour aussi longtemps
        # que la migration qui le tient (LOCK_TIMEOUT ne le borne donc pas).
        tx.execute("SET LOCAL lock_timeout = '%s'" % LOCK_TIMEOUT)
        if db.cfg.schema != "public":
            tx.execute("CREATE SCHEMA IF NOT EXISTS %s" % quote_ident(db.cfg.schema))
        todo = pending(tx, directory)
        if additive_only and any(non_additive(migration) for migration in todo):
            raise NonAdditiveMigration("nothing applied (--additive-only)")
        if any(non_additive(migration) for migration in todo):
            _exclude_live_executors(tx, todo, allow_live_executors)
        for migration in todo:
            tx.script(migration.sql)
            tx.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (%s,%s,%s)",
                       (migration.version, migration.name, migration.checksum))
        # Backfill historical non-additive migrations as soon as the additive
        # marker migration is present. Future non-additive markers commit in
        # the SAME transaction as their SQL, under the exclusive worker lock.
        if marker_available(tx):
            for migration in discover(directory):
                if non_additive(migration):
                    tx.execute(
                        "INSERT INTO schema_non_additive_migrations (version, name, checksum) "
                        "VALUES (%s,%s,%s) ON CONFLICT (version) DO NOTHING",
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
#: AVANT d'installer le code qui l'apporte.
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
#: fonction réécrite) : `-- ameesh: non additive`
_MARKER = re.compile(r"--\s*ameesh\s*:\s*non[- ]additive", re.IGNORECASE)
#: #5055 : retirer un droit ou resserrer une contrainte casse aussi le code
#: d'avant. Les versions déjà livrées gardent leur classement : le rétablir
#: ferait entrer leurs marqueurs dans `schema_non_additive_migrations`.
_PRIVILEGES_CHECKED_AFTER = 528
_REVOKE = re.compile(r"\brevoke\b[^;]*?\bon\s+(?:(table|function|sequence)\s+)?([\w.\"]+)"
                     r"\s*(\([^)]*\))?\s*(from\b)?|\brevoke\b")
_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d]\w*)?\$")
#: lu, comme _REVOKE, sur le texte sans chaînes : ses `;` et ses mots sont du code
_ALTER_TABLE = re.compile(r"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?([\w.\"]+)\s[^;]*")
#: mots qui, dans une chaîne, peuvent être du code exécuté : la forme n'est plus simple
_SENSITIVE = re.compile(r";|\b(?:revoke|grant|alter|add|create|drop|execute|constraint"
                        r"|unique|primary|check|foreign|references)\b")
_ADD_CONSTRAINT = re.compile(r"\badd\s+(?:constraint\s+([\w\"]+)"
                             r"|(primary\s+key|unique|check|foreign\s+key)\b)")
#: le texte ne dit pas si une contrainte élargit ou resserre : celle qui ne
#: resserre pas (remplacée pour l'élargir, posée sur une colonne neuve) le déclare
_CONSTRAINT_MARKER = re.compile(r"--\s*ameesh\s*:\s*constraint\s+not\s+tightened", re.IGNORECASE)
#: #5142 : un trigger, une règle ou une politique sur une table existante change
#: ce que fait l'écriture du code d'avant (0529, #5087). Les versions déjà livrées,
#: 0529 comprise, gardent leur classement, comme pour _PRIVILEGES_CHECKED_AFTER.
_GUARDS_CHECKED_AFTER = 529
_CREATE_GUARD = re.compile(r"\bcreate\s+(?:or\s+replace\s+)?(?:constraint\s+)?"
                           r"(trigger|rule|policy)\b([^;]*)")
#: une règle se lit `as on insert to t` ; un trigger et une politique, `on t`
_GUARD_TARGET = {"rule": re.compile(r"\bto\s+(?:only\s+)?([\w.\"]+)"),
                 "trigger": re.compile(r"\bon\s+(?:only\s+)?([\w.\"]+)"),
                 "policy": re.compile(r"\bon\s+(?:only\s+)?([\w.\"]+)")}
_ENABLE_GUARD = re.compile(r"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?([\w.\"]+)\s[^;]*?"
                           r"\benable\s+(?:always\s+|replica\s+)?(trigger|rule)\b")
_ROW_SECURITY = re.compile(r"\b(?:enable|force)\s+row\s+level\s+security\b")
_GUARD_IN_STRING = re.compile(
    r"\b(?:create\s+(?:or\s+replace\s+)?(?:constraint\s+)?|enable\s+(?:always\s+|replica\s+)?)"
    r"(trigger|rule|policy)\b|\b(row\s+level\s+security)\b")
#: seul l'auteur sait qu'un trigger reproduit le comportement d'avant tant que le
#: nouveau code ne l'allume pas : il le déclare, la relecture le vérifie
_TRIGGER_MARKER = re.compile(r"--\s*ameesh\s*:\s*trigger\s+compatible\s+with\s+previous\s+code",
                             re.IGNORECASE)


def non_additive(migration: Migration) -> list[str]:
    """Raisons pour lesquelles `migration` casserait le code d'avant ; vide si
    elle ne fait qu'ajouter (tables, colonnes nullables ou avec défaut, index,
    contraintes déclarées sans resserrement, triggers déclarés compatibles)."""
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
    if migration.version > _PRIVILEGES_CHECKED_AFTER:
        reasons.extend(_privilege_reasons(migration.sql))
    if migration.version > _GUARDS_CHECKED_AFTER:
        reasons.extend(_guard_reasons(migration.sql))
    return reasons


def _lex(sql: str) -> tuple[list[str], str, bool]:
    """(chaînes, texte sans commentaires ni chaînes, lecture sûre), en une seule
    passe : le délimiteur d'une chaîne ne se lit jamais dans un commentaire, ni
    l'inverse. Un identifiant cité devient un jeton neutre : son contenu n'est
    pas du code et sa casse ne survit pas aux minuscules. La lecture n'est pas
    sûre si un commentaire coupe une instruction ou si une fin manque."""
    strings, code, at, size, sure = [], [], 0, len(sql), True
    while at < size:
        two = sql[at:at + 2]
        dollar = _DOLLAR_TAG.match(sql, at) if sql[at] == "$" else None
        if two in ("--", "/*") and "".join(code).rsplit(";", 1)[-1].strip():
            sure = False
        if two == "--":
            end = sql.find("\n", at)
            at = size if end < 0 else end
        elif two == "/*":
            depth, at = 1, at + 2
            while at < size and depth:
                two = sql[at:at + 2]
                depth += (two == "/*") - (two == "*/")
                at += 2 if two in ("/*", "*/") else 1
            sure = sure and not depth
            code.append(" ")
        elif dollar or sql[at] in "'\"":
            if dollar:
                closing = sql.find(dollar.group(0), dollar.end())
                end = size if closing < 0 else closing + len(dollar.group(0))
            else:
                end = at + 1
                # E'' seul connaît l'échappement par barre oblique
                escaped = sql[at] == "'" and at and sql[at - 1] in "eE" and not (
                    at > 1 and (sql[at - 2].isalnum() or sql[at - 2] == "_"))
                while end < size:
                    if escaped and sql[end] == "\\":
                        end += 2
                    elif sql[end] == sql[at]:
                        if sql[end + 1:end + 2] != sql[at]:
                            break
                        end += 2
                    else:
                        end += 1
                sure = sure and end < size
                end = min(end + 1, size)
            sure = sure and not (dollar and closing < 0)
            if sql[at] == '"':
                code.append('"_"')
            else:
                strings.append(sql[at:end]), code.append(" ")
            at = end
        else:
            code.append(sql[at])
            at += 1
    return strings, "".join(code), sure


def _param_types(params: str, *, named: bool) -> list[str]:
    """Types d'une signature ; `named` : le premier mot d'un paramètre est son nom."""
    types = []
    for param in params.strip(" ()").split(","):
        words = re.split(r"\s+(?:default\b|=)|=", param)[0].split()
        types.append(" ".join(words[1:] if named and len(words) > 1 else words))
    return types


def _proven_new(code: str, kind: str, name: str, signature: str = "") -> bool:
    """`name` (sans guillemets) est-il prouvé neuf ? Seul un CREATE nu, hors chaîne SQL,
    échoue sur un objet existant : `or replace`, `if not exists`, un drop
    préalable, une chaîne non exécutée et une autre surcharge laissent au code
    d'avant son objet."""
    quoted = re.escape(name) + r"(?![\w.\"])"
    kinds = {"table": r"table|view|materialized\s+view|sequence", "function": "function",
             "sequence": "sequence"}[kind]
    if re.search(r"\bdrop\s+(?:%s)\s+(?:if\s+exists\s+)?%s" % (kinds, quoted), code):
        return False
    match = re.search(r"\bcreate\s+(?:%s)\s+%s\s*(\([^)]*\))?" % (kinds, quoted), code)
    if not match:
        return False
    if signature:
        return bool(match.group(1)) and (_param_types(match.group(1), named=True)
                                         == _param_types(signature, named=False))
    return True


def _privilege_reasons(sql: str) -> list[str]:
    """Droits retirés et contraintes posées sur ce qui n'est pas prouvé neuf (#5055).
    Seule une migration de forme simple peut prouver un objet neuf (mesh-design,
    #174729) : sans `$` ni DO, sans identifiant cité, sans commentaire dans une
    instruction et sans mot sensible (EXECUTE compris) dans une chaîne. Ailleurs,
    tout REVOKE et tout ADD de contrainte classent la migration, sans analyse."""
    def normal(value: str) -> str:
        return re.sub(r"\s+", " ", value.lower())

    strings, code, sure = _lex(sql)
    code, strings = normal(code), [normal(value) for value in strings]
    simple = sure and not re.search(r"[$\"\\]", sql) \
        and not re.search(r"\bdo\b", code) \
        and not any(_SENSITIVE.search(value) for value in strings)
    reasons = []
    if not simple and any(re.search(r"\brevoke\b", value) or (
            re.search(r"\badd\b", value)
            and re.search(r"\b(?:constraint|unique|primary|check|foreign)\b", value))
            for value in strings):
        reasons.append("privilege removed or constraint added in a string (not analysed)")
    for match in _REVOKE.finditer(code):
        kind, name, signature, single = match.groups()
        if not (simple and name and single
                and _proven_new(code, kind or "table", name, signature or "")):
            reasons.append("privilege removed (revoke%s)" % (" on " + name if name else ""))
    if not _CONSTRAINT_MARKER.search(sql):
        for statement in _ALTER_TABLE.finditer(code):
            if simple and _proven_new(code, "table", statement.group(1)):
                continue
            reasons.extend("constraint possibly tightened (add %s)"
                           % (name and "constraint " + name or form)
                           for name, form in _ADD_CONSTRAINT.findall(statement.group(0)))
    return reasons


def _guard_reasons(sql: str) -> list[str]:
    """Triggers, règles et politiques qui changent l'écriture du code d'avant (#5142).
    Un CREATE nu hors chaîne prouve la table neuve, même à côté de la fonction `$$`
    de son trigger : ce qu'une chaîne exécuterait y est classé à part, sans analyse."""
    strings, code, sure = _lex(sql)
    code = re.sub(r"\s+", " ", code.lower())
    trigger_declared = bool(_TRIGGER_MARKER.search(sql))

    def proven_new(table: str | None) -> bool:
        return bool(sure and table and '"' not in table and _proven_new(code, "table", table))

    reasons = []
    for kind, rest in _CREATE_GUARD.findall(code):
        target = _GUARD_TARGET[kind].search(rest)
        table = target.group(1) if target else None
        if not proven_new(table) and not (kind == "trigger" and trigger_declared):
            reasons.append("%s on a table not proven new (%s)" % (kind, table or "?"))
    for table, kind in _ENABLE_GUARD.findall(code):
        if not proven_new(table) and not (kind == "trigger" and trigger_declared):
            reasons.append("%s enabled on a table not proven new (%s)" % (kind, table))
    if _ROW_SECURITY.search(code):
        reasons.append("row level security enabled")
    for value in strings:
        for match in _GUARD_IN_STRING.finditer(re.sub(r"\s+", " ", value.lower())):
            if not (match.group(1) == "trigger" and trigger_declared):
                reasons.append("%s in a string (not analysed)" % (match.group(1) or match.group(2)))
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


class IncompatibleSchema(DbError):
    """The database has a non-additive migration absent from this code."""


def marker_available(db) -> bool:
    return bool(db.query(
        "SELECT to_regclass('schema_non_additive_migrations') IS NOT NULL AS ok"
    )[0]["ok"])


def incompatible_migrations(db, directory: str = MIGRATIONS_DIR) -> list[dict]:
    """Read-only, uncached: long-lived timers must see a new incompatibility."""
    known = known_writer_migrations(directory)
    try:
        rows = db.query(
            "SELECT version, name, checksum FROM schema_non_additive_migrations ORDER BY version")
    except Unavailable:
        raise  # connectivity failure keeps the existing reconnect semantics
    except DbError as exc:
        if exc.sqlstate == '42P01':
            try:
                installed = db.query("SELECT 1 FROM schema_migrations WHERE version = 524")
            except DbError as bootstrap:
                if bootstrap.sqlstate != '42P01':
                    raise IncompatibleSchema('writes refused: schema compatibility inventory unreadable: '
                                             + str(bootstrap)) from bootstrap
                installed = []
            if not installed:
                return []  # bootstrap: additive marker not deployed yet
        raise IncompatibleSchema("writes refused: schema compatibility marker unreadable: "
                                 + str(exc)) from exc
    return [row for row in rows if known.get(int(row['version'])) != row['checksum']]


def require_compatible_writer(db, directory: str = MIGRATIONS_DIR) -> None:
    if getattr(getattr(db, 'cfg', None), 'backend', '') == 'mediated':
        return  # requests are admitted by the server running its own code
    incompatible = incompatible_migrations(db, directory)
    if incompatible:
        labels = ", ".join("%04d_%s" % (int(r["version"]), r["name"]) for r in incompatible)
        raise IncompatibleSchema(
            "writes refused: database has non-additive migration(s) unknown to this code: "
            + labels + "; upgrade this executor/CLI before writing")


@functools.lru_cache(maxsize=16)
def known_writer_migrations(directory: str = MIGRATIONS_DIR) -> dict[int, str]:
    """Code inventory is immutable for a versioned process; DB inventory isn't."""
    return {m.version: m.checksum for m in discover(directory)}


_ACTIVE_WRITER_TICKS = contextvars.ContextVar('ameesh_writer_ticks', default=())


def writer_tick(method=None, *, on_admission_error=None):
    """One fresh check at a process tick; nested passes share that admission."""
    if method is None:
        return lambda fn: writer_tick(fn, on_admission_error=on_admission_error)
    @functools.wraps(method)
    def checked(self, *args, **kwargs):
        active = _ACTIVE_WRITER_TICKS.get()
        key = id(self.db)
        if key in active:
            return method(self, *args, **kwargs)
        try:
            require_compatible_writer(self.db)
        except (Unavailable, IncompatibleSchema) as exc:
            if on_admission_error is None:
                raise
            return on_admission_error(self, method.__name__, exc)
        if on_admission_error is not None:
            on_admission_error(self, method.__name__, None)
        token = _ACTIVE_WRITER_TICKS.set(active + (key,))
        try:
            return method(self, *args, **kwargs)
        finally:
            _ACTIVE_WRITER_TICKS.reset(token)
    return checked


class LiveExecutors(DbError):
    """Non-additive SQL requires stopped executors or an audited override."""


def _exclude_live_executors(tx, todo, reason):
    # Claims/attach/renew all take ROW EXCLUSIVE on this existing relation.
    # SHARE excludes them, including INSERT of a new agent, until COMMIT.
    # Take the relation lock BEFORE reading leases (a fresh RC snapshot).
    exists = tx.query("SELECT to_regclass('agent_registry') IS NOT NULL AS ok")[0]['ok']
    if not exists:
        return  # initial schema creation has no executor admissions yet
    tx.execute('LOCK TABLE agent_registry IN SHARE MODE')
    live = tx.query("SELECT name, lease_owner FROM agent_registry "
                    "WHERE lease_owner IS NOT NULL AND lease_expires_at > clock_timestamp() "
                    "ORDER BY name")
    if not live:
        return
    if not reason or not reason.strip():
        raise LiveExecutors('non-additive migration refused: live executor lease(s): '
                            + ', '.join(row['name'] for row in live)
                            + '; stop executors or use --allow-live-executors REASON')
    tx.execute("INSERT INTO ameesh_events(type, object, data) "
               "VALUES ('schema.migration_override', 'schema:' || current_schema(), %s::jsonb || jsonb_build_object('database_user', current_user))",
               (json.dumps({'reason': reason.strip(), 'live_leases': live,
                            'migrations': [m.label for m in todo],
                            'host': tx.cfg.host, 'runner': tx.cfg.runner}),))
