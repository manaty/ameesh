# SPDX-License-Identifier: AGPL-3.0-only
"""Banc Postgres local : orphelins `t_*`, gonflement des catalogues, signal.

Le banc (`scripts/pg-up.sh`, conteneur `ameesh-pg`, base `agent_mesh`) est
partagé par tous les agents d'un hôte. Chaque classe de test crée un schéma
`t_<classe>_<hex>` et le supprime au `tearDownClass` (`tests/support.py`) ;
un processus tué (timeout, SIGKILL, OOM) ou un `setUpClass` qui échoue avant
le cleanup laisse le schéma derrière lui, et des milliers de lignes de
catalogue meurent à chaque fois. L'autovacuum les récupère tant qu'il tient ;
sur un banc resté longtemps sans redémarrage, il peut cesser de passer sur
les catalogues (constat #3735 : 341 schémas, 23 Go, plus aucun autovacuum
depuis le 17/10). Cette maintenance :

* purge les schémas `t_*` **prouvés** orphelins — créés depuis plus de
  ``ORPHAN_MIN_AGE_HOURS`` (âge de leur première relation, une borne basse :
  un schéma récent n'est jamais purgé), sans propriétaire vivant et sans
  session qui les nomme, les verrouille ou les porte dans un `search_path`
  déclaré. La preuve de propriété est un commentaire de schéma
  (``ameesh-bench-owner pid=… [start=…]``, PID et starttime de l'hôte) posé
  par la suite : une suite au repos entre deux transactions n'a ni verrou ni
  requête nommante, et reste pourtant propriétaire ; sa mort (le processus
  disparaît) libère la purge. Une preuve **absente ou illisible garde le
  schéma** (#3735 B/P1) : seule la mort prouvée autorise le DROP. Les schémas
  **sans marqueur** ne sont **jamais purgés automatiquement** : une suite
  legacy peut les posséder, et son processus Python survit au redémarrage du
  postmaster (`PsqlDriver` n'a pas de connexion persistante : entre deux
  échanges aucun backend ne reste, la suite se reconnecte ensuite). Seul un
  opérateur, après avoir arrêté tout processus de suite, les admet
  explicitement (``python -m ameesh.bench purge --admit-unmarked``) ;
* purge les bases jetables laissées **invalides** (`datconnlimit = -2`) par un
  DROP DATABASE annulé pendant son checkpoint (#4822) : sans cela elles ne
  peuvent plus être ouvertes et l'autovacuum du banc s'arrête (#4680) ;
* signale le gonflement et un dernier autovacuum trop ancien (`doctor`) ;
* refuse de conclure quand l'activité n'est pas lisible (pas superutilisateur,
  `pg_stat_file` refusé) : le doute ne supprime rien.

Paramètres compilés, jamais un littéral au point d'appel (surcharge par
l'environnement pour un hôte ou un témoin, comme les autres seuils d'alerte
d'ameesh) :

* ``AMEESH_BENCH_ORPHAN_HOURS`` (défaut 6) : âge minimal d'un orphelin ;
* ``AMEESH_BENCH_AUTOVACUUM_HOURS`` (défaut 24) : seuil du signal autovacuum ;
* ``AMEESH_BENCH_CATALOG_BYTES`` / ``AMEESH_BENCH_DATABASE_BYTES`` : seuils
  du signal de gonflement (4 Gio / 8 Gio par défaut) ;

Le volume du banc n'est jamais touché ; aucune opération ici n'est un
`VACUUM FULL` (geste d'exploitation, hors de cette maintenance).
"""
from __future__ import annotations

import dataclasses
import os
import re
import socket
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import db as db_mod
from . import platform as platform_mod

#: préfixe des schémas de test (`tests/support.py: PgTestCase`)
SCHEMA_PREFIX = "t_"
#: motif strict d'un nom de schéma purgé : jamais d'identifiant arbitraire
SCHEMA_RE = re.compile(r"^t_[a-z0-9_]{1,58}$")

#: #4822 : bases jetables créées par la suite et ses scripts (`scripts/test.sh`,
#: `tests/test_l56_role_executor.py`, la restauration de sauvegarde). Seuls ces
#: noms sont purgés quand un DROP DATABASE annulé les a laissés invalides.
TEST_DATABASE_PREFIXES = (
    "agent_mesh_test_",
    "t_executor_db_",
    "ameesh_backup_test_",
    "ameesh_verify_",
)
TEST_DATABASE_RE = re.compile(
    r"^(?:%s)[a-z0-9_]*$" % "|".join(re.escape(prefix)
                                     for prefix in TEST_DATABASE_PREFIXES))

#: PostgreSQL marque une base dont le DROP DATABASE a été interrompu par un
#: `datconnlimit = -2` (DATCONNLIMIT_INVALID_DB) : plus aucune connexion n'y
#: est possible, seul un nouveau DROP DATABASE la retire.
INVALID_CONNLIMIT = -2

#: catalogues suivis par le signal et le diagnostic
CATALOGS = ("pg_attribute", "pg_class", "pg_depend", "pg_constraint",
            "pg_index", "pg_type", "pg_proc")

#: paramètre compilé : âge minimal d'un schéma t_* inactif avant purge
ORPHAN_MIN_AGE_HOURS = 6.0
#: paramètre compilé : âge au-delà duquel le dernier autovacuum est signalé
AUTOVACUUM_WARN_HOURS = 24.0
#: paramètre compilé : seuils du signal de gonflement
CATALOG_WARN_BYTES = 4 * 1024 ** 3
DATABASE_WARN_BYTES = 8 * 1024 ** 3

#: délai d'attente d'un verrou avant d'abandonner un schéma (jamais le forcer)
LOCK_TIMEOUT_S = 5.0
#: plafond d'une purge, pour ne pas retarder une suite indéfiniment
PURGE_BUDGET_S = 300.0
#: délai de connexion au banc (un banc absent ne doit pas retarder doctor)
CONNECT_TIMEOUT_S = 3

ENV_ORPHAN_HOURS = "AMEESH_BENCH_ORPHAN_HOURS"
ENV_AUTOVACUUM_HOURS = "AMEESH_BENCH_AUTOVACUUM_HOURS"
ENV_CATALOG_BYTES = "AMEESH_BENCH_CATALOG_BYTES"
ENV_DATABASE_BYTES = "AMEESH_BENCH_DATABASE_BYTES"


class BenchError(RuntimeError):
    """Lecture du banc impossible (droits, fonction absente) : ne rien purger."""


def _env_number(name: str, default: float, *, integer: bool = False) -> float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)
    if value < 0:
        return float(default)
    return int(value) if integer else value


def orphan_min_age_s() -> float:
    """Âge minimal (secondes) d'un orphelin, paramètre compilé surchargeable."""
    return _env_number(ENV_ORPHAN_HOURS, ORPHAN_MIN_AGE_HOURS) * 3600.0


def autovacuum_warn_s() -> float:
    return _env_number(ENV_AUTOVACUUM_HOURS, AUTOVACUUM_WARN_HOURS) * 3600.0


def catalog_warn_bytes() -> int:
    return int(_env_number(ENV_CATALOG_BYTES, CATALOG_WARN_BYTES, integer=True))


def database_warn_bytes() -> int:
    return int(_env_number(ENV_DATABASE_BYTES, DATABASE_WARN_BYTES, integer=True))


def bench_dsn() -> str:
    """DSN du banc : celui de la suite s'il est posé, sinon le banc local."""
    from .config import DEFAULT_DSN
    return (os.environ.get("AMEESH_TEST_DSN")
            or os.environ.get("AGENT_MESH_TEST_DSN")
            or DEFAULT_DSN)


def _with_connect_timeout(dsn: str) -> str:
    """Borne la connexion d'un banc absent (libpq attendrait sinon longtemps)."""
    if "connect_timeout" in dsn:
        return dsn
    if dsn.startswith(("postgresql://", "postgres://")):
        return dsn + ("&" if "?" in dsn else "?") + "connect_timeout=%d" % CONNECT_TIMEOUT_S
    if "=" in dsn:
        return dsn + " connect_timeout=%d" % CONNECT_TIMEOUT_S
    return dsn


def fmt_bytes(value: int | float | None) -> str:
    if value is None:
        return "?"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (size, unit)
        size /= 1024.0
    return "%.1f TiB" % size


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    value = max(0.0, float(seconds))
    if value < 60:
        return "%ds" % int(value)
    if value < 3600:
        return "%dm" % int(value // 60)
    if value < 86400:
        hours = int(value // 3600)
        minutes = int((value % 3600) // 60)
        return "%dh%02dm" % (hours, minutes)
    days = int(value // 86400)
    hours = int((value % 86400) // 3600)
    return "%dd%02dh" % (days, hours)


@dataclass(frozen=True)
class Reading:
    """État du banc : tailles, orphelins, dernier autovacuum, date du relevé."""

    database: str
    size_bytes: int
    catalogs: tuple[tuple[str, int], ...]
    orphan_schemas: int
    last_autovacuum: float | None
    server_start: float | None
    now: float

    @property
    def catalog_bytes(self) -> int:
        return sum(size for _, size in self.catalogs)

    @property
    def uptime_s(self) -> float | None:
        return None if self.server_start is None else max(0.0, self.now - self.server_start)

    @property
    def autovacuum_age_s(self) -> float | None:
        return None if self.last_autovacuum is None else max(0.0, self.now - self.last_autovacuum)

    def summary(self) -> str:
        top = sorted(self.catalogs, key=lambda item: item[1], reverse=True)[:3]
        details = ", ".join("%s %s" % (name, fmt_bytes(size)) for name, size in top)
        return ("%s, catalogs %s (%s), %d %s* schema(s), last autovacuum %s"
                % (fmt_bytes(self.size_bytes), fmt_bytes(self.catalog_bytes), details,
                   self.orphan_schemas, SCHEMA_PREFIX,
                   fmt_age(self.autovacuum_age_s)))

    def warnings(self) -> list[str]:
        """Signaux, du plus grave au moins grave ; aucun ne bloque un test."""
        out: list[str] = []
        catalog_limit = catalog_warn_bytes()
        database_limit = database_warn_bytes()
        vac_limit = autovacuum_warn_s()
        if self.catalog_bytes > catalog_limit:
            out.append(
                "bench catalogs %s > %s: purge the t_* orphans, then VACUUM "
                "(not FULL) in a quiet slot (#3735)"
                % (fmt_bytes(self.catalog_bytes), fmt_bytes(catalog_limit)))
        if self.size_bytes > database_limit:
            out.append("bench database %s > %s: check the t_* orphans and the "
                       "catalog bloat (#3735)"
                       % (fmt_bytes(self.size_bytes), fmt_bytes(database_limit)))
        age = self.autovacuum_age_s
        uptime = self.uptime_s
        if age is not None and age > vac_limit:
            out.append(
                "bench last autovacuum %s ago (> %s): autovacuum may be stalled; "
                "vacuum the catalogs or restart the bench"
                % (fmt_age(age), fmt_age(vac_limit)))
        elif age is None and uptime is not None and uptime > vac_limit:
            out.append(
                "bench has no autovacuum on its catalogs after %s of uptime: "
                "autovacuum may be stalled; vacuum the catalogs or restart the bench"
                % fmt_age(uptime))
        return out

    def doctor_lines(self) -> list[str]:
        lines = ["bench      : %s" % self.summary()]
        lines.extend("warning    : %s" % warning for warning in self.warnings())
        return lines


def read(db, *, now: float | None = None) -> Reading:
    """Lit l'état du banc (aucune écriture) ; lève DbError si la lecture échoue."""
    now = time.time() if now is None else float(now)
    row = db.query(
        "SELECT current_database() AS database, "
        "pg_database_size(current_database())::bigint AS size_bytes, "
        "extract(epoch FROM pg_postmaster_start_time())::double precision AS server_start"
    )[0]
    catalog_rows = db.query(
        "SELECT c.relname AS relname, pg_relation_size(c.oid)::bigint AS size_bytes "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'pg_catalog' AND c.relkind = 'r'"
    )
    sizes = {str(row["relname"]): int(row["size_bytes"] or 0) for row in catalog_rows}
    catalogs = tuple((name, sizes.get(name, 0)) for name in CATALOGS)
    names = ", ".join("'%s'" % name for name in CATALOGS)
    vac_rows = db.query(
        "SELECT relname AS relname, "
        "extract(epoch FROM last_autovacuum)::double precision AS last_autovacuum "
        "FROM pg_stat_sys_tables WHERE schemaname = 'pg_catalog' "
        "AND relname IN (%s)" % names
    )
    # le plus récent des catalogues suivis : l'autovacuum est muet seulement
    # si aucun n'a été touché depuis le seuil ; un catalogue calme (pg_proc)
    # ne doit pas déclencher un faux signal.
    last_values = [float(row["last_autovacuum"]) for row in vac_rows
                   if row["last_autovacuum"] is not None]
    orphan_row = db.query(
        "SELECT count(*)::bigint AS n FROM pg_namespace WHERE left(nspname, 2) = %s",
        (SCHEMA_PREFIX,),
    )[0]
    return Reading(
        database=str(row["database"]),
        size_bytes=int(row["size_bytes"] or 0),
        catalogs=catalogs,
        orphan_schemas=int(orphan_row["n"] or 0),
        last_autovacuum=(max(last_values) if last_values else None),
        server_start=(float(row["server_start"]) if row["server_start"] is not None else None),
        now=now,
    )


@dataclass(frozen=True)
class Orphan:
    """Un schéma t_* inactif, avec la preuve de son âge."""

    schema: str
    created: float
    age_s: float


#: préfixe du commentaire de schéma qui prouve son propriétaire (suite de test)
OWNER_TAG = "ameesh-bench-owner"
_OWNER_RE = re.compile(r"^ameesh-bench-owner pid=(\d+)(?: start=(\d+(?:\.\d+)?))?$")


def process_start(pid: int) -> float | None:
    """Heure de démarrage du processus (epoch), via la couche plateforme.

    Sert à distinguer un propriétaire d'un processus qui a réutilisé son PID ;
    None si le processus n'existe pas ou si l'OS ne sait pas la donner.
    """
    try:
        return platform_mod.start_time(int(pid))
    except platform_mod.NotAvailable:
        return None


#: états d'une preuve de propriété : seul `dead` autorise une purge (#3735 B/P1)
OWNER_ALIVE = "alive"
OWNER_DEAD = "dead"
OWNER_UNKNOWN = "unknown"


def owner_marker(pid: int | None = None, start: float | None = None) -> str:
    """Commentaire de propriété d'un schéma (PID, starttime si connue).

    Sans starttime (processus disparu, ou OS qui ne sait pas la donner), le
    marqueur reste **valide** : « pid=N », sans le suffixe « start= » vide que
    la lecture refuserait (#3735 B/P1).
    """
    pid = os.getpid() if pid is None else int(pid)
    start = process_start(pid) if start is None else float(start)
    marker = "%s pid=%d" % (OWNER_TAG, pid)
    return marker if start is None else "%s start=%.6f" % (marker, start)


def parse_owner(comment: str | None) -> tuple[int, float | None] | None:
    """(pid, start) d'un commentaire de propriété, ou None si étranger."""
    if not comment:
        return None
    match = _OWNER_RE.match(comment.strip())
    if match is None:
        return None
    start = match.group(2)
    return int(match.group(1)), (float(start) if start else None)


def owner_state(comment: str | None) -> str:
    """La preuve de propriété est-elle vivante, morte, ou illisible ?

    Seul ``OWNER_DEAD`` autorise la purge. Un commentaire absent, étranger ou
    tronqué, et un OS qui ne sait pas lire la preuve, sont ``UNKNOWN`` : le
    doute ne supprime rien (#3735 B/P1).
    """
    parsed = parse_owner(comment)
    if parsed is None:
        return OWNER_UNKNOWN
    pid, start = parsed
    try:
        if not platform_mod.alive(pid):
            return OWNER_DEAD
        if start is None:
            return OWNER_ALIVE
        actual = platform_mod.start_time(pid)
        # un start connu et différent = PID réutilisé, donc l'ancien
        # propriétaire est bien parti ; None (disparu entre-temps) = parti.
        if actual is None:
            return OWNER_DEAD
        return OWNER_ALIVE if platform_mod.same_start(start, actual) else OWNER_DEAD
    except platform_mod.NotAvailable:
        return OWNER_UNKNOWN  # l'OS ne sait pas : ne pas juger


def owner_alive(comment: str | None) -> bool:
    """Le propriétaire du schéma (processus de la suite) vit-il, prouvé ?"""
    return owner_state(comment) == OWNER_ALIVE


def claim_owner(db, schema: str, *, pid: int | None = None,
                start: float | None = None) -> None:
    """Pose la preuve de propriété du schéma, tenue par toute la classe.

    ``pid``/``start`` sont ceux du processus de la suite par défaut ; les
    témoins peuvent désigner un processus mort. Le commentaire disparaît avec
    le schéma (``DROP SCHEMA``).
    """
    if not SCHEMA_RE.match(schema):
        raise ValueError("invalid bench schema name")
    # COMMENT ... IS n'accepte pas un paramètre lié (psycopg enverrait $1) :
    # le marqueur est construit ici (PID/start entiers), jamais une entrée
    # libre ; on l'inline comme littéral SQL.
    marker = owner_marker(pid=pid, start=start)
    literal = "'" + marker.replace("'", "''") + "'"
    db.execute('COMMENT ON SCHEMA "%s" IS %s' % (schema, literal))


def _owners(db) -> dict[str, str]:
    """Commentaire de chaque schéma t_* (vide si aucun)."""
    try:
        rows = db.query(
            "SELECT n.nspname AS schema, "
            "obj_description(n.oid, 'pg_namespace') AS comment "
            "FROM pg_namespace n WHERE left(n.nspname, 2) = %s",
            (SCHEMA_PREFIX,),
        )
    except db_mod.DbError as exc:
        raise BenchError("bench owners unreadable: %s" % exc) from exc
    return {str(row["schema"]): str(row["comment"])
            for row in rows if row["comment"]}


def _local_dsn(dsn: str) -> bool:
    """La preuve de propriété est un PID de l'hôte : le banc doit être local."""
    if not dsn:
        return True
    if not dsn.startswith(("postgresql://", "postgres://")):
        return False  # forme mot-clé/hôte inconnu : refus conservateur
    try:
        host = urlsplit(dsn).hostname
    except ValueError:
        return False
    if not host:
        return True  # socket local
    return host in ("127.0.0.1", "localhost", "::1",
                    socket.gethostname(), socket.getfqdn())


def _creation_rows(db) -> list[dict]:
    """Âge de chaque schéma t_* : mtime de sa première relation (création).

    La première relation par OID est celle que la migration a créée en premier ;
    son mtime est une borne basse de l'âge du schéma (un schéma plus jeune que
    le seuil n'est jamais purgé). Lire *toutes* les relations coûterait ~40 s
    sur un banc gonflé (152 k fichiers) ; une relation par schéma coûte ~8 s
    au pire, et seulement au démarrage du banc.

    ``pg_stat_file(path, true)`` (PostgreSQL 16+) rend NULL pour un fichier
    disparu au lieu d'échouer ; sur un serveur plus ancien la fonction à deux
    arguments manque et la lecture est refusée (aucune purge).
    """
    try:
        return db.query(
            "SELECT n.nspname AS schema, "
            "extract(epoch FROM s.modification)::double precision AS last_write "
            "FROM pg_namespace n "
            "CROSS JOIN LATERAL ("
            "  SELECT c.oid FROM pg_class c "
            "   WHERE c.relnamespace = n.oid AND c.relfilenode <> 0 "
            "   ORDER BY c.oid LIMIT 1) x "
            "CROSS JOIN LATERAL pg_stat_file(pg_relation_filepath(x.oid), true) s "
            "WHERE left(n.nspname, 2) = %s",
            (SCHEMA_PREFIX,),
        )
    except db_mod.DbError as exc:
        raise BenchError("bench activity unreadable: %s" % exc) from exc


def _own_schema(db) -> str:
    """Schéma de la connexion appelante : jamais purgé par cette connexion."""
    cfg = getattr(db, "cfg", None)
    name = getattr(cfg, "schema", "") or ""
    return name if SCHEMA_RE.match(name) else ""


def _in_use(db) -> set[str]:
    """Garde bon marché : schéma nommé, verrouillé ou dans un search_path.

    Ce n'est pas une preuve de propriété : entre deux transactions, une
    connexion au repos n'a ni verrou ni requête qui nomme son schéma, et un
    ``search_path`` posé par les options de connexion n'apparaît pas dans
    ``pg_db_role_setting``. La preuve durable est le commentaire de schéma
    (``claim_owner``) ; cette garde couvre les schémas antérieurs, sans
    commentaire.
    """
    used: set[str] = set()
    queries = (
        "SELECT DISTINCT n.nspname AS schema FROM pg_namespace n "
        "JOIN pg_stat_activity a ON a.pid <> pg_backend_pid() "
        "AND a.datname = current_database() "
        "WHERE left(n.nspname, 2) = %s "
        "AND a.query LIKE '%%' || n.nspname || '%%'",
        "SELECT DISTINCT n.nspname AS schema FROM pg_locks l "
        "JOIN pg_class c ON c.oid = l.relation "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE l.pid IS NOT NULL AND l.pid <> pg_backend_pid() "
        "AND left(n.nspname, 2) = %s",
        "SELECT DISTINCT n.nspname AS schema FROM pg_namespace n "
        "JOIN LATERAL (SELECT 1 FROM pg_db_role_setting s, "
        "unnest(s.setconfig) AS cfg "
        "WHERE cfg LIKE 'search_path=%%' AND position(n.nspname in cfg) > 0) x ON true "
        "WHERE left(n.nspname, 2) = %s",
    )
    try:
        for sql in queries:
            for row in db.query(sql, (SCHEMA_PREFIX,)):
                used.add(str(row["schema"]))
    except db_mod.DbError as exc:
        raise BenchError("bench sessions unreadable: %s" % exc) from exc
    return used


def orphan_schemas(db, *, min_age_s: float | None = None,
                   now: float | None = None,
                   only: frozenset[str] | set[str] | None = None,
                   admit_unmarked: bool = False) -> list[Orphan]:
    """Schémas ``t_*`` orphelins prouvés : assez vieux, aucune session.

    Un schéma sans relation n'a pas d'âge prouvable : il est gardé (le doute
    ne supprime rien). Une preuve de propriété (``claim_owner``) qui désigne
    un processus **mort** ne protège plus ; une preuve vivante, absente ou
    illisible garde le schéma (#3735 B/P1 : seule la mort prouvée autorise
    un DROP). Un schéma sans marqueur n'est admis que si ``admit_unmarked``
    est posé par l'opérateur (`--admit-unmarked`) : un schéma ancien sans
    marqueur peut appartenir à une suite legacy dont le processus Python
    survit au redémarrage du postmaster ; le code ne peut pas prouver son
    arrêt.
    ``only`` borne la passe à ces noms (témoins : le banc est partagé, un âge
    minimal abaissé n'y vise jamais les schémas des autres suites).
    """
    minimum = orphan_min_age_s() if min_age_s is None else float(min_age_s)
    if minimum < 0:
        raise ValueError("min_age_s must be >= 0")
    moment = time.time() if now is None else float(now)
    rows = _creation_rows(db)
    owners = _owners(db)
    used = _in_use(db)
    own = _own_schema(db)
    if own:
        used.add(own)
    unmarked_admitted = bool(admit_unmarked)
    out: list[Orphan] = []
    for row in rows:
        name = str(row["schema"])
        if not SCHEMA_RE.match(name):
            continue
        if only is not None and name not in only:
            continue
        comment = owners.get(name)
        if comment is not None:
            if owner_state(comment) != OWNER_DEAD:
                continue
        elif not unmarked_admitted:
            continue
        if name in used:
            continue
        last = row["last_write"]
        if last is None:
            continue
        last_write = float(last)
        age = moment - last_write
        if age < minimum:
            continue
        out.append(Orphan(schema=name, created=last_write, age_s=age))
    out.sort(key=lambda orphan: (orphan.created, orphan.schema))
    return out


@dataclass(frozen=True)
class Purge:
    """Résultat d'une passe : ce qui est parti, ce qui est gardé et pourquoi."""

    scanned: int
    dropped: tuple[str, ...]
    kept: tuple[tuple[str, str], ...]
    unreadable: str = ""
    freed_bytes: int = 0
    dry_run: bool = False

    def line(self) -> str:
        if self.unreadable:
            return "bench purge: %s — nothing purged" % self.unreadable
        if self.dry_run:
            return "bench purge (dry-run): %d orphan(s) would be dropped" % (
                len(self.kept))
        text = "bench purge: %d orphan(s) dropped (%s freed), %d kept (of %d candidate(s))" % (
            len(self.dropped), fmt_bytes(self.freed_bytes), len(self.kept), self.scanned)
        if self.kept:
            text += "; " + ", ".join("%s (%s)" % item for item in self.kept[:5])
            if len(self.kept) > 5:
                text += ", …"
        return text


def purge_orphans(db, *, min_age_s: float | None = None,
                  now: float | None = None, dry_run: bool = False,
                  budget_s: float = PURGE_BUDGET_S,
                  only: frozenset[str] | set[str] | None = None,
                  admit_unmarked: bool = False) -> Purge:
    """Purge les orphelins prouvés, un par un, sous verrou court et budget.

    Un `DROP SCHEMA ... CASCADE` qui n'obtient pas son verrou échoue et le
    schéma est gardé (aucune purge forcée). L'âge et l'absence de session sont
    revérifiés juste avant la passe ; une course reste couverte par le
    ``lock_timeout``. ``admit_unmarked`` n'est posé que par l'opérateur
    (`--admit-unmarked`) : les schémas sans marqueur restent gardés par
    défaut, une suite legacy pouvant les posséder sans qu'aucun verrou ni
    redémarrage ne le prouve.
    """
    if not _local_dsn(getattr(getattr(db, "cfg", None), "dsn", "") or ""):
        return Purge(scanned=0, dropped=(), kept=(),
                     unreadable="bench is not local: owner proof is host-local")
    try:
        orphans = orphan_schemas(db, min_age_s=min_age_s, now=now, only=only,
                                 admit_unmarked=admit_unmarked)
    except BenchError as exc:
        count = 0
        try:
            count = int(db.query(
                "SELECT count(*)::bigint AS n FROM pg_namespace "
                "WHERE left(nspname, 2) = %s", (SCHEMA_PREFIX,))[0]["n"] or 0)
        except db_mod.DbError:
            pass
        return Purge(scanned=count, dropped=(), kept=(), unreadable=str(exc))
    if dry_run:
        return Purge(scanned=len(orphans), dropped=(),
                     kept=tuple((orphan.schema, "dry-run") for orphan in orphans),
                     dry_run=True)
    # un schéma a pu être pris entre la lecture et la passe : on relit les
    # sessions juste avant de supprimer, et un doute laisse tout en place.
    try:
        used = _in_use(db)
        own = _own_schema(db)
        if own:
            used.add(own)
    except BenchError as exc:
        return Purge(scanned=len(orphans), dropped=(), kept=(),
                     unreadable=str(exc))
    orphans = [orphan for orphan in orphans if orphan.schema not in used]
    before = _database_size(db)
    started = time.monotonic()
    dropped: list[str] = []
    kept: list[tuple[str, str]] = []
    for orphan in orphans:
        name = orphan.schema
        if not SCHEMA_RE.match(name):  # defense in depth; never interpolate blindly
            kept.append((name, "name refused"))
            continue
        if time.monotonic() - started > budget_s:
            kept.append((name, "purge budget exceeded"))
            continue
        try:
            with db.transaction() as tx:
                tx.execute("SET LOCAL lock_timeout = '%dms'"
                           % int(LOCK_TIMEOUT_S * 1000))
                tx.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % name)
        except db_mod.DbError as exc:
            kept.append((name, _short_reason(exc)))
            continue
        dropped.append(name)
    after = _database_size(db)
    freed = (max(0, before - after)
             if before is not None and after is not None else 0)
    return Purge(scanned=len(orphans), dropped=tuple(dropped), kept=tuple(kept),
                 freed_bytes=freed)


def _database_size(db) -> int | None:
    try:
        row = db.query("SELECT pg_database_size(current_database())::bigint AS n")[0]
        return int(row["n"] or 0)
    except db_mod.DbError:
        return None


def _short_reason(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text[:80] if text else exc.__class__.__name__


@dataclass(frozen=True)
class InvalidPurge:
    """Bases invalides vues, supprimées et gardées par la passe (#4822)."""

    scanned: int
    dropped: tuple[str, ...]
    kept: tuple[tuple[str, str], ...]
    unreadable: str = ""


def invalid_databases(db) -> list[str]:
    """Bases invalides visibles par `db`, hors base courante (#4822).

    `datconnlimit = -2` est l'état que PostgreSQL laisse après un DROP
    DATABASE annulé pendant son checkpoint : la base n'accepte plus de
    connexion et seul un nouveau DROP DATABASE la retire.
    """
    rows = db.query(
        "SELECT datname FROM pg_database WHERE datconnlimit = %s "
        "AND datname <> current_database() ORDER BY datname",
        (INVALID_CONNLIMIT,))
    return [str(row["datname"]) for row in rows]


def purge_invalid_databases(db, *,
                            only: frozenset[str] | set[str] | None = None,
                            budget_s: float = PURGE_BUDGET_S) -> InvalidPurge:
    """Purge les bases de test invalides laissées par un DROP annulé (#4822).

    Le DROP qui a produit l'état invalide a été coupé par son
    `statement_timeout` pendant le checkpoint. La passe se connecte donc avec
    `statement_timeout_ms = 0` et retire chaque base par
    ``DROP DATABASE IF EXISTS … WITH (FORCE)``, comme la purge du banc de
    #1992 : le délai de la suite ne doit pas reproduire l'état qu'il répare.

    Seuls les noms des bases jetables de la suite et de ses scripts sont
    visés (`TEST_DATABASE_RE`) ; une base invalide étrangère est gardée.
    ``only`` borne la passe à ces noms (témoins).
    """
    cfg = getattr(db, "cfg", None)
    if cfg is None or getattr(cfg, "backend", "pg") == "mediated":
        return InvalidPurge(scanned=0, dropped=(), kept=(),
                            unreadable="no PostgreSQL bench connection")
    try:
        names = invalid_databases(db)
    except db_mod.DbError as exc:
        return InvalidPurge(scanned=0, dropped=(), kept=(),
                            unreadable=_short_reason(exc))
    kept = [(name, "not a test database") for name in names
            if not TEST_DATABASE_RE.match(name)]
    targets = [name for name in names if TEST_DATABASE_RE.match(name)]
    if only is not None:
        kept.extend((name, "outside only") for name in targets if name not in only)
        targets = [name for name in targets if name in only]
    if not targets:
        return InvalidPurge(scanned=len(names), dropped=(), kept=tuple(kept))
    try:
        admin = db_mod.connect(dataclasses.replace(cfg, statement_timeout_ms=0))
    except (db_mod.Unavailable, db_mod.DbError, OSError) as exc:
        return InvalidPurge(scanned=len(names), dropped=(), kept=tuple(kept),
                            unreadable=_short_reason(exc))
    dropped: list[str] = []
    started = time.monotonic()
    try:
        for name in targets:
            if time.monotonic() - started > budget_s:
                kept.append((name, "purge budget exceeded"))
                continue
            try:
                admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)"
                              % db_mod.quote_ident(name))
            except db_mod.DbError as exc:
                kept.append((name, _short_reason(exc)))
                continue
            dropped.append(name)
    finally:
        admin.close()
    return InvalidPurge(scanned=len(names), dropped=tuple(dropped),
                        kept=tuple(kept))


def doctor_lines(cfg=None, *, now: float | None = None) -> list[str]:
    """Lignes `doctor` du banc local ; jamais d'échec, jamais de verdict KO.

    Le banc interrogé est celui de la suite (``AMEESH_TEST_DSN``) ou le banc
    local par défaut — jamais la base du mesh. Un banc absent ou illisible est
    dit, pas bloquant.
    """
    from . import config as config_mod
    dsn = _with_connect_timeout(bench_dsn())
    base = cfg if cfg is not None else config_mod.Config()
    bench_cfg = dataclasses.replace(base, dsn=dsn, schema="public", backend="pg",
                                    statement_timeout_ms=120000.0)
    try:
        db = db_mod.connect(bench_cfg)
    except (db_mod.Unavailable, db_mod.DbError, OSError) as exc:
        return ["bench      : not checked (%s)" % _short_reason(exc)]
    try:
        try:
            reading = read(db, now=now)
        except db_mod.DbError as exc:
            return ["bench      : not checked (%s)" % _short_reason(exc)]
        return reading.doctor_lines()
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    """``python -m ameesh.bench status|purge [--dry-run] [--admit-unmarked]``."""
    import argparse
    from . import config as config_mod
    parser = argparse.ArgumentParser(description="banc Postgres local (schémas t_*)")
    parser.add_argument("action", choices=("status", "purge"))
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--admit-unmarked", action="store_true",
        help="opérateur : admettre les schémas sans marqueur (une suite legacy "
             "peut les posséder ; n'utiliser qu'après avoir arrêté tout "
             "processus de suite)")
    parser.add_argument("--min-age-hours", type=float, default=None)
    args = parser.parse_args(argv)
    dsn = _with_connect_timeout(args.dsn or bench_dsn())
    cfg = dataclasses.replace(config_mod.Config(), dsn=dsn, schema="public",
                              backend="pg", statement_timeout_ms=120000.0)
    try:
        db = db_mod.connect(cfg)
    except (db_mod.Unavailable, db_mod.DbError) as exc:
        print("bench: unavailable (%s)" % _short_reason(exc))
        return 1
    try:
        if args.action == "status":
            try:
                lines = read(db).doctor_lines()
            except db_mod.DbError as exc:
                print("bench: unreadable (%s)" % _short_reason(exc))
                return 2
            for line in lines:
                print(line)
            return 0
        minimum = (None if args.min_age_hours is None
                   else max(0.0, args.min_age_hours) * 3600.0)
        result = purge_orphans(db, min_age_s=minimum, dry_run=args.dry_run,
                               admit_unmarked=args.admit_unmarked)
        if args.admit_unmarked:
            print("bench: unmarked schemas admitted on the operator's assertion; "
                  "every suite process must be stopped", file=sys.stderr)
        print(result.line())
        return 0 if not result.unreadable else 2
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
