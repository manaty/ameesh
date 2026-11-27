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
import sys
import threading
import time
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
    #: module propriétaire (P1a #7649) ; None pour une migration du noyau
    module: str | None = None

    @property
    def label(self) -> str:
        if self.module:
            return "%s/%04d_%s" % (self.module, self.version, self.name)
        return "%04d_%s" % (self.version, self.name)

    def as_dict(self) -> dict:
        """Description additive, pour `ameesh modules --json`."""
        return {"module": self.module, "version": self.version, "name": self.name,
                "label": self.label, "checksum": self.checksum}


def discover(directory: str = MIGRATIONS_DIR, *, module: str | None = None,
             ) -> list[Migration]:
    """Les migrations `NNNN_nom.sql` d'un dossier, en ordre croissant.

    `module` étiquette chaque fichier : le registre et le libellé d'une
    migration de module sont locaux au module (`dev/0001_…`), et son numéro
    n'entre pas en collision avec celui du noyau."""
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
            module=module,
        )
    if not found:
        raise DbError("no migration in %s" % directory)
    return [found[v] for v in sorted(found)]


# --------------------------------------------------------------------------
# schémas et migrations de module (P1a, étude #7616 §4.1–4.5)
# --------------------------------------------------------------------------

#: forme d'un nom de module (même règle que `modules.NAME`)
MODULE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: forme d'un nom de schéma physique (contrainte de `module_schemas`)
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
#: séparateur entre le schéma noyau et le slug du module (Q8)
MODULE_SCHEMA_SEP = "__"
#: étude #7616 §4.5 : une fonction SECURITY DEFINER de module doit fixer son
#: search_path ; sans cela, elle s'exécuterait avec le search_path de
#: l'appelant et pourrait résoudre un objet d'un autre schéma.
_SECURITY_DEFINER = re.compile(r"\bsecurity\s+definer\b", re.IGNORECASE)
_SET_SEARCH_PATH = re.compile(r"\bset\s+search_path\b", re.IGNORECASE)
_ROUTINE = re.compile(r"\b(?:create\s+(?:or\s+replace\s+)?|alter\s+)"
                      r"(?:function|procedure)\b", re.IGNORECASE)


def module_sql_problems(migration: Migration) -> list[str]:
    """Raisons de refuser le SQL d'une migration de module (lecture prudente).

    Le texte est lu par `_lex` : commentaires retirés, corps `$$` et littéraux
    remplacés par des jetons. `SECURITY DEFINER` se juge donc **par routine**,
    dans l'instruction qui la définit : un `SET search_path` écrit dans le
    corps d'une fonction, dans un littéral ou dans une autre instruction ne
    couvre rien. Un `SECURITY DEFINER` dans un corps ou un littéral peut être
    exécuté par SQL dynamique, et un lecteur incertain ne prouve rien : ces
    cas sont refusés."""
    strings, code, sure, _source = _lex(migration.sql)
    declared = _SECURITY_DEFINER.search(code)
    dynamic = _SECURITY_DEFINER.search("\n".join(strings))
    if not declared and not dynamic:
        return []
    if dynamic or not sure:
        return ["SECURITY DEFINER cannot be verified"]
    problems = []
    for statement in code.split(";"):
        if not _SECURITY_DEFINER.search(statement):
            continue
        if not _ROUTINE.search(statement):
            problems.append("SECURITY DEFINER outside a routine definition")
        if not _SET_SEARCH_PATH.search(statement):
            problems.append("SECURITY DEFINER without SET search_path")
    return list(dict.fromkeys(problems))


def module_metadata(name: str, implementation) -> dict:
    """Les faits de schéma déclarés par un module installé (P1a #7649).

    Lit ``SCHEMA`` (nom logique, optionnel), ``MIGRATIONS`` (dossier relatif au
    paquet, ou None) et ``REQUIRES`` (dépendances de schéma), sans importer ni
    exécuter le module. Le dossier de migrations est local au module : il ne
    peut pas sortir de son paquet ou de sa distribution (étude §4.3).

    Lève ValueError, comme le refus d'une entrée installée : une déclaration
    impossible à appliquer n'est jamais un schéma à moitié créé."""
    if not isinstance(name, str) or not MODULE_NAME.fullmatch(name):
        raise ValueError("installed module %r: invalid module name" % (name,))
    logical = getattr(implementation, "SCHEMA", None)
    folder = getattr(implementation, "MIGRATIONS", None)
    requires = getattr(implementation, "REQUIRES", ())
    if logical is not None:
        logical = logical.strip() if isinstance(logical, str) else logical
        if not isinstance(logical, str) or not logical or len(logical) > 63:
            raise ValueError("installed module %r: SCHEMA must be a non-empty name of "
                             "at most 63 characters" % name)
    if folder is None:
        # Un module sans migration ne crée pas de schéma (étude §4.2) ; le nom
        # logique déclaré reste lisible dans `ameesh modules --json`.
        return {"schema": logical, "migrations": (), "requires": (),
                "directory": None}
    if not isinstance(folder, str) or not folder.strip() or os.path.isabs(folder):
        raise ValueError("installed module %r: MIGRATIONS must be a relative folder" % name)
    source = getattr(implementation, "__file__", None)
    if not source:
        raise ValueError("installed module %r: MIGRATIONS needs an installed package file"
                         % name)
    package = os.path.realpath(os.path.dirname(os.path.abspath(source)))
    directory = os.path.realpath(os.path.join(package, folder))
    try:
        inside = os.path.commonpath([package, directory]) == package
    except ValueError:  # chemins de lecteurs différents (Windows)
        inside = False
    if not inside:
        raise ValueError("installed module %r: MIGRATIONS escapes its package (%s)"
                         % (name, folder))
    if not os.path.isdir(directory):
        raise ValueError("installed module %r: MIGRATIONS folder not found: %s"
                         % (name, folder))
    migrations = tuple(discover(directory, module=name))
    for migration in migrations:
        problems = module_sql_problems(migration)
        if problems:
            raise ValueError("installed module %r: migration %s: %s"
                             % (name, migration.label, "; ".join(problems)))
    return {"schema": logical or name, "migrations": migrations,
            "requires": _requirements(name, requires), "directory": directory}


def _requirements(name: str, declared) -> tuple[str, ...]:
    """Les `REQUIRES` d'un module : noms uniques, jamais lui-même."""
    if declared is None:
        return ()
    if isinstance(declared, str) or not isinstance(declared, (list, tuple)):
        raise ValueError("installed module %r: REQUIRES must be a list of module names"
                         % name)
    out = []
    for required in declared:
        if not isinstance(required, str) or not MODULE_NAME.fullmatch(required):
            raise ValueError("installed module %r: invalid REQUIRES entry %r"
                             % (name, required))
        if required == name:
            raise ValueError("installed module %r: REQUIRES names itself" % name)
        if required in out:
            raise ValueError("installed module %r: duplicate REQUIRES entry %r"
                             % (name, required))
        out.append(required)
    return tuple(out)


def schema_name(core_schema: str, module: str) -> str:
    """Le nom physique du schéma d'un module : `<schéma_noyau>__<slug>` (Q8).

    Le slug est le nom du module en minuscules, `[a-z0-9_]` ; au-delà de 63
    octets, la fin est remplacée par un suffixe SHA-256, comme le nom logique
    reste celui du descripteur. Le nom physique n'est jamais deviné par le
    code appelant : il est inscrit dans `module_schemas` et relu là."""
    slug = re.sub(r"[^a-z0-9_]", "_", module.lower())
    if not slug or not (slug[0].isalpha() or slug[0] == "_"):
        slug = "_" + slug
    full = core_schema + MODULE_SCHEMA_SEP + slug
    encoded = full.encode("utf-8")
    if len(encoded) > 63:
        digest = hashlib.sha256(encoded).hexdigest()[:8]
        room = 63 - len(digest) - 1
        head = encoded[:room].decode("utf-8", "ignore") if room > 0 else ""
        full = head + "_" + digest
    if not SCHEMA_NAME.fullmatch(full):
        raise DbError("schema %r cannot host a module schema (derived %r)"
                      % (core_schema, full))
    return full


def schema_identity(module: str, schema: str) -> str:
    """L'empreinte de l'identité de schéma : (module, nom logique) seulement.

    Les migrations ont leurs propres empreintes dans
    `module_schema_migrations` ; un ajout de migration ne change donc pas
    l'identité du schéma (étude §4.1)."""
    payload = json.dumps({"module": module, "schema": schema}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModuleSchema:
    """Un schéma de module à créer/migrer, dans l'ordre topologique."""
    module: str
    schema: str            # nom logique déclaré (ou nom du module)
    schema_name: str       # nom physique `<noyau>__<slug>`
    schema_sha256: str
    requires: tuple[str, ...]
    migrations: tuple[Migration, ...]


def module_schemas(modules, core_schema: str) -> list[ModuleSchema]:
    """Les schémas des modules découverts, dépendances d'abord.

    `modules` sont les descripteurs de `registry.Module` (ou un `Registry`) :
    seuls ceux qui déclarent un dossier de migrations ont un schéma. Un
    `REQUIRES` inconnu, un module requis sans schéma, un doublon de nom ou un
    cycle sont refusés avant toute application (étude §4.2)."""
    rows = tuple(getattr(modules, "modules", modules) or ())
    by_name = {}
    for module in rows:
        if module.name in by_name:
            raise DbError("duplicate module: %r" % module.name)
        by_name[module.name] = module
    with_schema = {name: module for name, module in by_name.items()
                   if getattr(module, "migrations", ())}
    for name, module in with_schema.items():
        for required in getattr(module, "requires", ()):
            if required not in by_name:
                raise DbError("module %s requires unknown module %s" % (name, required))
            if required not in with_schema:
                raise DbError("module %s requires module %s, which declares no schema"
                              % (name, required))
    order: list[ModuleSchema] = []
    done: set[str] = set()
    visiting: list[str] = []

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            cycle = " -> ".join(visiting[visiting.index(name):] + [name])
            raise DbError("module schema dependency cycle: %s" % cycle)
        visiting.append(name)
        module = with_schema[name]
        for required in getattr(module, "requires", ()):
            visit(required)
        visiting.pop()
        done.add(name)
        logical = getattr(module, "schema", None) or name
        order.append(ModuleSchema(
            module=name, schema=logical,
            schema_name=schema_name(core_schema, name),
            schema_sha256=schema_identity(name, logical),
            requires=tuple(getattr(module, "requires", ())),
            migrations=tuple(module.migrations)))

    for name in sorted(with_schema):
        visit(name)
    return order


def installed_modules() -> tuple:
    """Les modules installés, lus par la façade du registre (jamais importés)."""
    from .modules import registry
    return registry.discover().modules


def _module_rows(modules) -> tuple:
    return tuple(getattr(modules, "modules", modules) or ())


def _relation_present(db, name: str) -> bool:
    return bool(db.query("SELECT to_regclass(%s) IS NOT NULL AS ok", (name,))[0]["ok"])


def _schema_present(db, name: str) -> bool:
    return bool(db.query("SELECT 1 AS ok FROM pg_namespace WHERE nspname = %s", (name,)))


def _module_schema_rows(db) -> dict[str, dict]:
    """Les lignes de `module_schemas`, vide si la migration 0591 n'est pas là."""
    if not _relation_present(db, "module_schemas"):
        return {}
    return {row["module"]: row for row in db.query(
        "SELECT module, schema_name, schema_sha256, created_at "
        "FROM module_schemas ORDER BY module")}


def applied_module(db, module: str) -> dict[int, str]:
    """Version → empreinte des migrations appliquées d'un module (lecture seule)."""
    if not _relation_present(db, "module_schema_migrations"):
        return {}
    return {int(row["version"]): row["checksum"] for row in db.query(
        "SELECT version, checksum FROM module_schema_migrations "
        "WHERE module = %s ORDER BY version", (module,))}


def _verify_module_schema(db, schema: ModuleSchema, registered: dict[str, dict],
                          *, require_schema: bool) -> None:
    """Une ligne de registre existante doit désigner ce schéma, pas un autre."""
    row = registered.get(schema.module)
    if row is None:
        return
    if (row["schema_name"] != schema.schema_name
            or row["schema_sha256"] != schema.schema_sha256):
        raise DbError(
            "module %s: registry names schema %s (%s), this code expects %s (%s); "
            "a registered module schema is never rewritten"
            % (schema.module, row["schema_name"], row["schema_sha256"][:12],
               schema.schema_name, schema.schema_sha256[:12]))
    if require_schema and not _schema_present(db, schema.schema_name):
        raise DbError("module %s: schema %s is recorded but missing"
                      % (schema.module, schema.schema_name))


def _module_survey(db, schema: ModuleSchema, registered: dict[str, dict],
                   *, require_schema: bool,
                   ) -> tuple[list[Migration], list[tuple[Migration, str]], dict[int, str]]:
    """(manquantes, modifiées, appliquées) d'un module ; modifiées porte
    l'empreinte enregistrée en base."""
    _verify_module_schema(db, schema, registered, require_schema=require_schema)
    done = applied_module(db, schema.module)
    missing, modified = [], []
    for migration in schema.migrations:
        if migration.version not in done:
            missing.append(migration)
        elif done[migration.version] != migration.checksum:
            modified.append((migration, done[migration.version]))
    return missing, modified, done


def _pending_for(db, schema: ModuleSchema, registered: dict[str, dict],
                 *, require_schema: bool) -> list[Migration]:
    missing, modified, _done = _module_survey(db, schema, registered,
                                              require_schema=require_schema)
    if modified:
        migration, applied = modified[0]
        raise DbError(
            "module migration %s modified after being applied (checksum %s != %s): "
            "an applied migration is immutable, add a new one"
            % (migration.label, applied[:12], migration.checksum[:12]))
    return missing


def pending_modules(db, schemas) -> list[Migration]:
    """Les migrations de module manquantes, dans l'ordre topologique des schémas.

    Lecture seule comme `doctor` : une empreinte modifiée est une erreur, pas
    une réécriture. Un schéma inscrit au registre mais absent de la base est
    une erreur (migration de module non appliquée ou retour arrière partiel)."""
    registered = _module_schema_rows(db)
    out = []
    for schema in schemas:
        out.extend(_pending_for(db, schema, registered, require_schema=True))
    return out


@dataclass(frozen=True)
class ModuleCheck:
    """État d'un schéma de module, en lecture seule (`doctor`, `--json`)."""
    module: str
    schema: str
    schema_name: str
    schema_sha256: str
    schema_present: bool
    registered: bool
    registered_schema: str | None
    registered_sha256: str | None
    migrations: tuple[Migration, ...]
    applied: frozenset[int]
    missing: tuple[Migration, ...]
    modified: tuple[Migration, ...]
    unknown: tuple[int, ...]

    @property
    def mismatch(self) -> bool:
        return bool(self.registered and (
            self.registered_schema != self.schema_name
            or self.registered_sha256 != self.schema_sha256))


@dataclass(frozen=True)
class ModuleState:
    """Tous les schémas de module et ce que la base en dit."""
    checks: tuple[ModuleCheck, ...] = ()
    unknown_modules: tuple[dict, ...] = ()
    unknown_schemas: tuple[str, ...] = ()


def check_modules(db, schemas) -> ModuleState:
    """État des schémas et migrations de module, **sans rien écrire**."""
    registered = _module_schema_rows(db)
    checks = []
    for schema in schemas:
        row = registered.get(schema.module)
        done = applied_module(db, schema.module) if row or _relation_present(
            db, "module_schema_migrations") else {}
        known = {migration.version for migration in schema.migrations}
        missing = tuple(m for m in schema.migrations if m.version not in done)
        modified = tuple(m for m in schema.migrations
                         if m.version in done and done[m.version] != m.checksum)
        unknown = tuple(sorted(version for version in done if version not in known))
        checks.append(ModuleCheck(
            module=schema.module, schema=schema.schema, schema_name=schema.schema_name,
            schema_sha256=schema.schema_sha256,
            schema_present=_schema_present(db, schema.schema_name),
            registered=row is not None,
            registered_schema=row["schema_name"] if row else None,
            registered_sha256=row["schema_sha256"] if row else None,
            migrations=schema.migrations, applied=frozenset(done),
            missing=missing, modified=modified, unknown=unknown))
    expected = {schema.module for schema in schemas}
    unknown_modules = tuple(row for name, row in sorted(registered.items())
                            if name not in expected)
    unknown_schemas: tuple[str, ...] = ()
    if _relation_present(db, "module_schemas"):
        prefix = db.cfg.schema + MODULE_SCHEMA_SEP
        rows = db.query("SELECT nspname FROM pg_namespace "
                        "WHERE left(nspname, %s) = %s ORDER BY nspname",
                        (len(prefix), prefix))
        known_names = {schema.schema_name for schema in schemas}
        unknown_schemas = tuple(row["nspname"] for row in rows
                                if row["nspname"] not in known_names)
    return ModuleState(tuple(checks), unknown_modules, unknown_schemas)


@dataclass(frozen=True)
class SchemaCheck:
    """Le contrôle combiné des deux registres (`doctor --probe`)."""
    missing: tuple[Migration, ...]
    modified: tuple[Migration, ...]
    unknown: tuple[int, ...]
    modules: ModuleState


def check_all(db, directory: str = MIGRATIONS_DIR, *, modules=None,
              core_schema: str | None = None) -> SchemaCheck:
    """`check` du noyau + état des schémas et migrations de module."""
    missing, modified, unknown = check(db, directory)
    schemas = module_schemas(_module_rows(modules), core_schema or db.cfg.schema)
    return SchemaCheck(tuple(missing), tuple(modified), tuple(unknown),
                       check_modules(db, schemas))


def module_doctor_lines(db, core_schema: str | None = None, modules=None,
                        ) -> tuple[bool, list[str]]:
    """Les lignes `schemas` du doctor complet ; (ok, lignes).

    Un schéma inscrit qui manque, une identité qui ne correspond plus ou une
    empreinte modifiée sont des KO ; une migration en attente est dite (WARN
    si elle est non additive), un schéma orphelin est un WARN."""
    try:
        schemas = module_schemas(_module_rows(modules) if modules is not None
                                 else installed_modules(),
                                 core_schema or db.cfg.schema)
        state = check_modules(db, schemas)
    except (DbError, ValueError) as exc:
        return False, ["schemas    : KO — %s" % exc]
    lines, ok = [], True
    for check in state.checks:
        total = len(check.migrations)
        applied = total - len(check.missing)
        if check.mismatch:
            ok = False
            lines.append("schemas    : KO — module %s: registry names %s, this code "
                         "expects %s" % (check.module, check.registered_schema,
                                         check.schema_name))
            continue
        if check.registered and not check.schema_present:
            ok = False
            lines.append("schemas    : KO — module %s: schema %s is recorded but missing"
                         % (check.module, check.schema_name))
            continue
        if check.modified:
            ok = False
            lines.append("schemas    : KO — module %s: %s modified after being applied"
                         % (check.module,
                            ", ".join(migration.label for migration in check.modified)))
        where = (("schema %s" % check.schema_name) if check.schema_present
                 else "schema %s absent" % check.schema_name)
        lines.append("schemas    : module %s — %s, %d/%d migration(s) applied"
                     % (check.module, where, applied, total))
        for migration in check.missing:
            reasons = non_additive(migration)
            if reasons:
                lines.append("schemas    : WARNING — module %s: non-additive migration "
                             "%s pending (%s)" % (check.module, migration.label,
                                                  "; ".join(reasons)))
        if any(not non_additive(migration) for migration in check.missing):
            lines.append("schemas    : module %s: %d pending migration(s): %s"
                         % (check.module,
                            sum(1 for m in check.missing if not non_additive(m)),
                            ", ".join(m.label for m in check.missing
                                      if not non_additive(m))))
        if check.unknown:
            lines.append("schemas    : WARNING — module %s: database newer than this "
                         "code (versions %s)" % (
                             check.module,
                             ",".join("%04d" % version for version in check.unknown)))
    for row in state.unknown_modules:
        lines.append("schemas    : WARNING — module %s is registered in schema %s but "
                     "not discovered by this code"
                     % (row["module"], row["schema_name"]))
    for name in state.unknown_schemas:
        lines.append("schemas    : WARNING — orphan module schema %s (no discovered "
                     "module)" % name)
    return ok, lines


# --------------------------------------------------------------------------
# numéros réservés (L122)
# --------------------------------------------------------------------------

#: chemin des migrations du paquet, dans son propre dépôt
REPO_PATH = "src/ameesh/migrations"
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


#: dépôts déclarés : `radar.projects.<p>`, puis `release.projects.<p>` et
#: `packet_watch.projects.<p>` (`github`, `repo`, `migrations`,
#: `migrations_width`) ; le dépôt du paquet se lit à son `origin`
#: (`package_repo`, #7795), aucun slug n'est compilé.


def repo_key(repo: str | None) -> str:
    """La clé d'un dépôt `owner/repo` : sans espaces, en minuscules (GitHub ne
    distingue pas la casse ; une seule suite par dépôt)."""
    return (repo or "").strip().lower()


@functools.lru_cache(maxsize=1)
def package_repo() -> str | None:
    """#7795 : le dépôt du paquet, lu à l'`origin` de son propre arbre.

    Aucun nom de forge n'est compilé : le dépôt qui porte ce code est celui de
    l'arbre d'où il tourne. Hors clone (roue dans un venv scellé), le clone de
    déploiement voisin (`~/.local/share/ameesh/src`, `src-<sha7>`) porte le
    même `origin` ; sans lui, aucun dépôt n'est déclaré et l'appelant qui en a
    besoin le lit dans les déclarations de l'hôte (`radar`, `release`,
    `packet_watch`)."""
    root = toplevel(os.path.dirname(os.path.abspath(__file__)))
    if root:
        return origin_repo(root)
    prefix = os.path.realpath(sys.prefix)
    name = os.path.basename(prefix).rstrip(os.sep)
    if not name.startswith("venv-"):
        return None
    parent = os.path.dirname(prefix)
    for candidate in (os.path.join(parent, "src"),
                      os.path.join(parent, "src-" + name[len("venv-"):])):
        root = toplevel(candidate)
        if root:
            slug = origin_repo(root)
            if slug:
                return slug
    return None


def package_layout() -> Layout | None:
    """#7795 : la disposition du dépôt du paquet lui-même, ou None."""
    repo = package_repo()
    if not repo:
        return None
    try:
        names = os.listdir(MIGRATIONS_DIR)
    except OSError:
        names = []
    widths = [len(match.group(1)) for name in names
              if (match := _REPO_FILE_RE.match(os.path.basename(name)))]
    width = max(set(widths), key=widths.count) if widths else 4
    return Layout(repo, REPO_PATH, width)


def _project_value(project, key: str, default=None):
    """Une clé d'une déclaration de projet, objet (`radar.Project`) ou dict
    brut (`release.projects`, `packet_watch.projects`)."""
    if isinstance(project, dict):
        return project.get(key, default)
    return getattr(project, key, default)


def _declared_projects(cfg) -> list:
    """Les déclarations qui nomment un dépôt et son clone, dans l'ordre de
    lecture : `radar.projects` (validé), puis `release.projects`, puis
    `packet_watch.projects`.

    #7795 : un runner peut n'avoir que `packet_watch` (le composeur) ; le
    dépôt de la disposition ne doit pas dépendre du seul bloc `radar`. Un bloc
    absent ou mal formé n'ajoute rien ; la lecture fermée reste aux appelants.
    """
    projects = list(_radar_projects(cfg))
    if cfg is None:
        return projects
    for key in ("release", "packet_watch"):
        block = getattr(cfg, key, None)
        declared = block.get("projects") if isinstance(block, dict) else None
        if not isinstance(declared, dict):
            continue
        projects.extend(entry for entry in declared.values()
                        if isinstance(entry, dict))
    return projects


def layout_for(repo: str, cfg=None) -> Layout | None:
    """La disposition des migrations du dépôt `owner/repo`, ou None.

    #7795 : un dépôt se lit dans les déclarations de l'hôte
    (`radar.projects`, puis `release.projects` et `packet_watch.projects` :
    `github`, `repo`, `migrations`, `migrations_width`). Le clone déclaré
    nomme le répertoire par ses fichiers quand la configuration ne le précise
    pas. Le dépôt du paquet lui-même se reconnaît à l'`origin` de son arbre
    (`package_repo`) ; le répertoire `src/ameesh/migrations` d'un clone
    déclaré le nomme aussi. Aucun `owner/repo` n'est compilé."""
    repo = repo_key(repo)
    if not repo:
        return None
    for project in _declared_projects(cfg):
        github = str(_project_value(project, "github", "") or "").strip()
        checkout = str(_project_value(project, "repo", "") or "").strip()
        checkout = os.path.expanduser(checkout) if checkout else ""
        if not github and checkout:
            github = origin_repo(checkout) or ""
        if repo_key(github) != repo:
            continue
        directory = str(_project_value(project, "migrations", "")
                        or "").strip().strip("/")
        if not directory and checkout and os.path.isdir(
                os.path.join(checkout, REPO_PATH)):
            directory = REPO_PATH
        if directory:
            known = Layout(repo, directory, 4)
            return Layout(repo, directory, width_of(project, known, checkout=checkout))
        if checkout:
            inferred = inferred_layout(checkout, repo)
            if inferred is not None:
                return inferred
    if repo and repo == repo_key(package_repo() or ""):
        return package_layout()
    return None


#: dossiers jamais parcourus pour inférer une disposition de migrations
_INFER_SKIP = frozenset({".git", ".hg", ".svn", "node_modules", "vendor", "dist",
                         "build", "venv", ".venv", "__pycache__"})


def inferred_layout(checkout: str, repo: str = "") -> Layout | None:
    """#7795 : la disposition lue dans un clone quand la configuration ne la
    nomme pas — le clone du dépôt du lot porte ses fichiers.

    Un dossier `migrations` (à quatre niveaux au plus, hors dépendances) qui
    contient des fichiers numérotés désigne le répertoire et sa largeur. Un
    seul candidat fait foi ; plusieurs sans `src/ameesh/migrations` sont un
    doute, jamais un choix : None (l'appelant refuse). Aucun nom de dépôt ni
    de dossier n'est compilé."""
    if not checkout or not os.path.isdir(checkout):
        return None
    candidates: list[tuple[str, list[int]]] = []
    base_depth = checkout.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(checkout):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= 4:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d not in _INFER_SKIP)
        if os.path.basename(root) != "migrations":
            continue
        widths = [len(match.group(1)) for name in files
                  if (match := _REPO_FILE_RE.match(os.path.basename(name)))]
        if widths:
            relative = os.path.relpath(root, checkout).replace(os.sep, "/")
            candidates.append((relative, widths))
    if not candidates:
        return None
    if len(candidates) > 1:
        preferred = [item for item in candidates if item[0] == REPO_PATH]
        if len(preferred) != 1:
            return None
        candidates = preferred
    directory, widths = candidates[0]
    width = max(set(widths), key=widths.count)
    return Layout(repo or "", directory, width)


def width_of(project, known: Layout | None = None, *, checkout: str | None = None) -> int:
    """#4750 : la largeur déclarée (`migrations_width`), sinon celle lue dans
    les fichiers du dépôt (#7795), sinon celle de `known`, sinon 4."""
    declared = _project_value(project, "migrations_width", 0) or 0
    if declared:
        return declared
    directory = (str(_project_value(project, "migrations", "") or "").strip().strip("/")
                 or (known.directory if known else ""))
    if checkout is None:
        checkout = str(_project_value(project, "repo", "") or "")
        checkout = os.path.expanduser(checkout) if checkout else ""
    if directory and checkout:
        try:
            names = os.listdir(os.path.join(checkout, directory))
        except OSError:
            names = []
        widths = [len(match.group(1)) for name in names
                  if (match := _REPO_FILE_RE.match(os.path.basename(name)))]
        if widths:
            return max(set(widths), key=widths.count)
    return known.width if known else 4


def _radar_projects(cfg) -> list:
    if cfg is None:
        return []
    from . import radar

    try:
        return list(radar.settings_of(cfg).projects.values())
    except radar.SettingsError:
        return []


def checkout_for(repo: str, cfg=None) -> str | None:
    """Un clone local de `repo` déclaré à l'hôte (`radar.projects`, puis
    `release.projects` et `packet_watch.projects`), ou None."""
    wanted = repo_key(repo)
    if not wanted:
        return None
    for project in _declared_projects(cfg):
        github = str(_project_value(project, "github", "") or "").strip()
        checkout = str(_project_value(project, "repo", "") or "").strip()
        checkout = os.path.expanduser(checkout) if checkout else ""
        if not github and checkout:
            github = origin_repo(checkout) or ""
        if repo_key(github) == wanted and checkout and os.path.isdir(checkout):
            return checkout
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

    # #7795 : le dépôt qui range ses migrations à `REPO_PATH` est celui du
    # paquet ; le paquet installé peut être plus récent que le clone. Un autre
    # dépôt au même chemin n'emprunte pas ces numéros ; hors clone (roue), le
    # dépôt du paquet n'est pas lisible et le chemin suffit.
    own = repo_key(package_repo() or "")
    if (layout.directory == REPO_PATH and os.path.isdir(package)
            and (not own or repo_key(layout.repo) == own)):
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
            log=None, *, additive_only: bool = False,
            allow_live_executors: str | None = None,
            modules=None) -> list[Migration]:
    """Elect one database worker, re-read inventory and commit the whole pass.

    PostgreSQL scopes the existing lock key to the actual database, regardless
    of schema, host or DSN alias. Retaining this key also excludes older workers
    while they execute their individually locked migrations. Those older
    workers must still be upgraded before relying on inventory reconciliation.
    A separate lock connection could disappear while its writer continued;
    this transaction owns both the lock and every migration write instead.

    P1a #7649 : la même transaction et le même verrou appliquent le noyau puis
    les schémas de module, dans l'ordre topologique de leurs `REQUIRES`. Les
    migrations de module sont locales au module (`module_schema_migrations`),
    et `--additive-only` couvre les deux flux avant d'écrire quoi que ce soit.
    """
    if allow_live_executors is not None and not allow_live_executors.strip():
        raise ValueError('--allow-live-executors requires a nonempty reason')
    schemas = module_schemas(_module_rows(modules), db.cfg.schema)
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
        original_path = tx.query(
            "SELECT current_setting('search_path') AS path")[0]["path"]
        if db.cfg.schema != "public":
            tx.execute("CREATE SCHEMA IF NOT EXISTS %s" % quote_ident(db.cfg.schema))
        todo = pending(tx, directory)
        registered = _module_schema_rows(tx)
        module_todo = {schema.module: _pending_for(tx, schema, registered,
                                                   require_schema=True)
                       for schema in schemas}
        everything = todo + [migration for schema in schemas
                             for migration in module_todo[schema.module]]
        if additive_only and any(non_additive(migration) for migration in everything):
            raise NonAdditiveMigration("nothing applied (--additive-only)")
        if any(non_additive(migration) for migration in everything):
            _exclude_live_executors(tx, everything, allow_live_executors)
        for migration in todo:
            tx.script(migration.sql)
            tx.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (%s,%s,%s)",
                       (migration.version, migration.name, migration.checksum))
        # Les migrations de module après le noyau : leur registre et leurs
        # dépendances de schéma existent alors dans la même transaction.
        _apply_module_migrations(tx, schemas, module_todo, original_path)
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
        for migration in everything:
            log("%s applied" % migration.label)
    return everything


def _apply_module_migrations(tx, schemas, todo, original_path: str) -> None:
    """Crée les schémas et applique leurs migrations, dépendances d'abord.

    `search_path` de chaque migration : `<schéma_module>, <schéma_noyau>,
    pg_temp` (étude #7616 §4.3) ; le noyau touché est qualifié explicitement
    par la migration. Le `search_path` d'origine est restauré après chaque
    module pour que les écritures du noyau qui suivent restent non ambiguës."""
    core_schema = tx.cfg.schema
    for schema in schemas:
        pending = todo.get(schema.module) or []
        if not pending:
            continue
        registered = _module_schema_rows(tx)
        _verify_module_schema(tx, schema, registered, require_schema=True)
        tx.execute("CREATE SCHEMA IF NOT EXISTS %s" % quote_ident(schema.schema_name))
        if schema.module not in registered:
            tx.execute(
                "INSERT INTO module_schemas (module, schema_name, schema_sha256) "
                "VALUES (%s,%s,%s)",
                (schema.module, schema.schema_name, schema.schema_sha256))
        search_path = "%s, %s, pg_temp" % (quote_ident(schema.schema_name),
                                           quote_ident(core_schema))
        for migration in pending:
            problems = module_sql_problems(migration)
            if problems:
                raise DbError("module migration %s: %s"
                              % (migration.label, "; ".join(problems)))
            tx.query("SELECT set_config('search_path', %s, true)", (search_path,))
            tx.script(migration.sql)
            tx.execute(
                "INSERT INTO module_schema_migrations "
                "(module, version, name, checksum) VALUES (%s,%s,%s,%s)",
                (migration.module, migration.version, migration.name,
                 migration.checksum))
            if non_additive(migration):
                tx.execute(
                    "INSERT INTO module_schema_non_additive_migrations "
                    "(module, version, name, checksum) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (module, version) DO NOTHING",
                    (migration.module, migration.version, migration.name,
                     migration.checksum))
        tx.query("SELECT set_config('search_path', %s, true)", (original_path,))


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _compare(known: list[Migration], done: dict[int, str],
             ) -> tuple[list[Migration], list[Migration], list[int]]:
    """(manquantes, modifiées, versions inconnues) sur un inventaire déjà lu."""
    missing = [m for m in known if m.version not in done]
    modified = [m for m in known if m.version in done and done[m.version] != m.checksum]
    versions = {m.version for m in known}
    unknown = sorted(v for v in done if v not in versions)
    return missing, modified, unknown


def check(db: PsqlDriver | PsycopgDriver, directory: str = MIGRATIONS_DIR,
          ) -> tuple[list[Migration], list[Migration], list[int]]:
    """État des migrations en **lecture seule** (sonde `doctor --probe`).

    Contrairement à `applied()`, n'exécute aucun DDL (pas de `create table if
    not exists`) : la sonde ne doit rien écrire. Rend (manquantes, modifiées
    après application, versions appliquées inconnues de ce code — base plus
    récente que l'image, par exemple après un retour arrière).
    """
    return _compare(discover(directory), _applied_readonly(db))


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
    # P1a #7649 : `ALTER TABLE|SEQUENCE … SET SCHEMA` est un déplacement
    # d'objet, pas un ajout (étude #7616 §5.2). Visé sur le texte entier, donc
    # aussi quand le DDL est exécuté depuis un bloc DO.
    (re.compile(r"\balter\s+(?:table|sequence|view|materialized\s+view|index|"
                r"function|procedure|type|domain)\s+(?:if\s+exists\s+)?(?:only\s+)?"
                r"[^;]*?\bset\s+schema\b"),
     "object moved to another schema (set schema)"),
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
#: début d'instruction qui attend le texte d'un COMMENT ON (E'' compris, et la
#: suite '…' '…' d'un littéral coupé par une fin de ligne)
_COMMENT_IS = re.compile(r"^\s*comment\s+on\s[^;]*\bis(?:\s*|\s+e)$", re.IGNORECASE)
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
    contraintes déclarées sans resserrement, triggers déclarés compatibles).

    Une migration de module n'a pas d'historique de classement à préserver :
    toutes les règles s'appliquent, y compris celles nées après 0528/0529."""
    version = migration.version if migration.module is None else None
    return non_additive_sql(migration.sql, version=version)


def non_additive_sql(sql: str, *, version: int | None = None) -> list[str]:
    """`non_additive` sur un texte SQL, noyau (`version`) ou module (None)."""
    reasons = []
    if _MARKER.search(sql):
        reasons.append("marked non-additive")
    # commentaires retirés : une explication qui cite « drop » ne compte pas ;
    # PostgreSQL finit un commentaire de ligne au LF comme au CR seul
    text = re.sub(r"--[^\n\r]*", " ", sql)
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
    if version is None or version > _PRIVILEGES_CHECKED_AFTER:
        reasons.extend(_privilege_reasons(sql))
    if version is None or version > _GUARDS_CHECKED_AFTER:
        reasons.extend(_guard_reasons(sql))
    return reasons


def _lex(sql: str, *, dollar_handler=None, string_handler=None, allow_inline_comments=False) -> tuple[list[str], str, bool, str]:
    """(chaînes, texte sans commentaires ni chaînes, lecture sûre, source sans
    documentation), en une seule passe : le délimiteur d'une chaîne ne se lit
    jamais dans un commentaire, ni l'inverse. Un identifiant cité devient un
    jeton neutre : son contenu n'est pas du code et sa casse ne survit pas aux
    minuscules. La lecture n'est pas sûre si un commentaire bloc coupe une
    instruction ou si une fin manque ; un commentaire de ligne est sans effet
    (il finit au LF ou au CR seul, comme PostgreSQL). #6153 : le littéral
    '…' d'un COMMENT ON … IS hors chaîne est de la documentation, jamais
    exécutée : il n'est ni dans les chaînes ni dans la source rendue ; un
    littéral $$ y reste une chaîne, par prudence. Le lecteur staging peut
    remplacer les corps dollar par des jetons via `dollar_handler` et admettre
    les commentaires internes (toujours remplacés par un espace). Le rappel
    `string_handler` remplace les littéraux simples par un jeton opaque ; les
    appelants historiques conservent exactement leurs valeurs par défaut."""
    strings, code, at, size, sure = [], [], 0, len(sql), True
    prose = []
    while at < size:
        two = sql[at:at + 2]
        dollar = _DOLLAR_TAG.match(sql, at) if sql[at] == "$" else None
        if (not allow_inline_comments and two == "/*"
                and "".join(code).rsplit(";", 1)[-1].strip()):
            sure = False
        if two == "--":
            end = sql.find("\n", at)
            cr = sql.find("\r", at)
            if cr >= 0 and (end < 0 or cr < end):
                end = cr
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
            elif not dollar and _COMMENT_IS.search("".join(code).rsplit(";", 1)[-1]):
                prose.append((at, end)), code.append(" ")
            elif dollar and dollar_handler is not None and closing >= 0:
                code.append(dollar_handler(sql[at:end]))
            else:
                strings.append(sql[at:end])
                code.append(string_handler(sql[at:end]) if string_handler is not None else " ")
            at = end
        else:
            code.append(sql[at])
            at += 1
    source, last = [], 0
    for start, end in prose:
        source.append(sql[last:start] + " ")
        last = end
    return strings, "".join(code), sure, "".join(source) + sql[last:]


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
    #174729) : sans `$` ni DO, sans identifiant cité, sans commentaire bloc dans
    une instruction et sans mot sensible (EXECUTE compris) dans une chaîne.
    Ailleurs, tout REVOKE et tout ADD de contrainte classent la migration, sans
    analyse."""
    def normal(value: str) -> str:
        return re.sub(r"\s+", " ", value.lower())

    strings, code, sure, source = _lex(sql)
    code, strings = normal(code), [normal(value) for value in strings]
    simple = sure and not re.search(r"[$\"\\]", source) \
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
    strings, code, sure, _ = _lex(sql)
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


def pending_non_additive_from(known, done,
                              ) -> list[tuple[Migration, list[str]]]:
    """Le calcul de `pending_non_additive` sur un inventaire déjà lu."""
    out = []
    for migration in known:
        if migration.version in done:
            continue
        reasons = non_additive(migration)
        if reasons:
            out.append((migration, reasons))
    return out


def pending_non_additive(db, directory: str = MIGRATIONS_DIR,
                         ) -> list[tuple[Migration, list[str]]]:
    """#5692 : les migrations manquantes que le code d'avant ne survivrait pas.

    Lecture seule (comme `status`), pour le doctor, les alertes et le
    préflight de staging. `require_compatible_writer` garde l'autre sens (la
    base est en avance sur ce code) ; ici le code est en avance sur la base :
    `migrate --additive-only` refuse ces migrations, et les appliquer exige
    les exécuteurs arrêtés. Le rattrapage lui-même n'est pas fait ici."""
    return pending_non_additive_from(discover(directory), set(_applied_readonly(db)))


def describe_pending_non_additive(items) -> str:
    """Les libellés et raisons d'un `pending_non_additive`, en une ligne."""
    return ", ".join("%s (%s)" % (migration.label, "; ".join(reasons))
                     for migration, reasons in items)


#: version contractuelle de l'objet de `ameesh migrate --dry-run --json` (#8339)
DRY_RUN_SCHEMA = "ameesh-migrate-dry-run/1"


@dataclass(frozen=True)
class DryRunReport:
    """Inventaire en lecture seule d'un `migrate --dry-run --json` (#8339).

    `pending` garde l'ordre d'application (noyau puis modules) ; `modified`
    porte l'empreinte enregistrée en base, pour nommer les deux côtés d'une
    migration appliquée puis modifiée ; `unknown` liste les versions que cette
    base a et que ce code ne connaît pas, noyau (`module` nul) ou module."""

    pending: tuple[Migration, ...]
    modified: tuple[tuple[Migration, str], ...]
    unknown: tuple[tuple[str | None, int], ...]

    def as_dict(self, *, additive_only: bool = False) -> dict:
        """L'objet stable ``ameesh-migrate-dry-run/1`` (jamais du texte)."""
        return {
            "schema": DRY_RUN_SCHEMA,
            "dry_run": True,
            "additive_only": additive_only,
            "pending": [
                {"module": migration.module, "version": migration.version,
                 "label": migration.label, "checksum": migration.checksum,
                 "non_additive": non_additive(migration)}
                for migration in self.pending
            ],
            "modified": [
                {"module": migration.module, "version": migration.version,
                 "label": migration.label, "checksum": migration.checksum,
                 "applied_checksum": applied}
                for migration, applied in self.modified
            ],
            "unknown": [{"module": module, "version": version}
                        for module, version in self.unknown],
        }


def dry_run(db: PsqlDriver | PsycopgDriver, directory: str = MIGRATIONS_DIR,
            *, modules=None, core_schema: str | None = None) -> DryRunReport:
    """Inventaire **sans écriture** pour `migrate --dry-run --json` (#8339).

    Même lecture que `check` + `pending_modules` : aucun DDL, pas même le
    registre des migrations. Une migration de module modifiée après coup est
    rendue dans `modified` au lieu de lever (le rapport doit nommer les deux
    empreintes) ; un schéma de module inscrit qui manque ou dont l'identité a
    changé reste un refus (`DbError`), comme `pending_modules`."""
    done = _applied_readonly(db)
    known = discover(directory)
    missing, modified, unknown = _compare(known, done)
    pending = list(missing)
    findings = [(migration, done[migration.version]) for migration in modified]
    unknown_entries = [(None, version) for version in unknown]
    schemas = module_schemas(_module_rows(modules), core_schema or db.cfg.schema)
    registered = _module_schema_rows(db)
    for schema in schemas:
        missing_module, modified_module, done_module = _module_survey(
            db, schema, registered, require_schema=True)
        pending.extend(missing_module)
        findings.extend(modified_module)
        known_versions = {migration.version for migration in schema.migrations}
        unknown_entries.extend(
            (schema.module, version) for version in sorted(done_module)
            if version not in known_versions)
    return DryRunReport(tuple(pending), tuple(findings), tuple(unknown_entries))


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


#: #5980 : seule l'admission des nouvelles connexions partage un succès.
#: Les passes longues gardent leur contrôle explicite et frais (writer_tick).
#: Une minuterie ouvrant seulement des connexions relit au plus tard après 5 s.
WRITER_ADMISSION_TTL_S = 5.0
_WRITER_ADMISSIONS = {}
_WRITER_ADMISSION_LOCK = threading.RLock()


def _writer_admission_key(db, directory):
    cfg = getattr(db, "cfg", None)
    # Sans identité de cible exploitable, aucun partage (outils et doublures).
    if cfg is None or not isinstance(getattr(cfg, "dsn", None), str):
        return None
    return (os.getpid(), cfg.dsn, cfg.schema, os.path.abspath(directory))


def _check_compatible_writer(db, directory, key):
    # Refus et erreur de transport ne laissent jamais un succès antérieur.
    _WRITER_ADMISSIONS.pop(key, None)
    started = time.monotonic()
    incompatible = incompatible_migrations(db, directory)
    if incompatible:
        labels = ", ".join("%04d_%s" % (int(r["version"]), r["name"]) for r in incompatible)
        raise IncompatibleSchema(
            "writes refused: database has non-additive migration(s) unknown to this code: "
            + labels + "; upgrade this executor/CLI before writing")
    if key is not None:
        if len(_WRITER_ADMISSIONS) >= 128:
            _WRITER_ADMISSIONS.pop(next(iter(_WRITER_ADMISSIONS)))
        # Le temps de lecture compte dans les 5 s, pas seulement l'attente.
        _WRITER_ADMISSIONS[key] = started + WRITER_ADMISSION_TTL_S


def require_compatible_writer(db, directory: str = MIGRATIONS_DIR) -> None:
    """Contrôle explicite toujours frais ; renouvelle l'admission de la cible."""
    if getattr(getattr(db, 'cfg', None), 'backend', '') == 'mediated':
        return  # requests are admitted by the server running its own code
    with _WRITER_ADMISSION_LOCK:
        key = _writer_admission_key(db, directory)
        try:
            _check_compatible_writer(db, directory, key)
        except BaseException:
            # ScopedDb peut ouvrir une connexion admise pendant cette lecture.
            # Son succès imbriqué ne doit pas survivre au refus extérieur.
            _WRITER_ADMISSIONS.pop(key, None)
            raise


def admit_writer_connection(db, directory: str = MIGRATIONS_DIR) -> None:
    """#5980 : partage un succès pendant au plus 5 s entre connexions.

    Portée : processus, DSN exact (identité comprise), schéma et inventaire
    du code. Aucun cache interprocessus. Les contrôles explicites des passes
    (`writer_tick`, tours) ne sont jamais sautés ; seule la tête de passe au
    repos d'un worker partage ce succès (#8152). Le verrou empêche qu'un succès concurrent ancien
    réarme le cache après un refus détecté par un autre fil.

    N = 5 s borne la fenêtre d'admission d'une nouvelle connexion écrivaine
    après l'apparition d'une migration non additive inconnue. Les processus
    longs gardent leur contrôle frais par passe ; les migrations non additives
    exigent toujours l'arrêt des exécuteurs. Un refus détecté invalide le
    succès immédiatement : la borne n'autorise aucune admission après refus.
    """
    if getattr(getattr(db, 'cfg', None), 'backend', '') == 'mediated':
        return
    with _WRITER_ADMISSION_LOCK:
        key = _writer_admission_key(db, directory)
        if key is not None and time.monotonic() < _WRITER_ADMISSIONS.get(key, 0):
            return
        if directory == MIGRATIONS_DIR:
            require_compatible_writer(db)
        else:
            require_compatible_writer(db, directory)


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
