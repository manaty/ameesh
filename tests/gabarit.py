# SPDX-License-Identifier: AGPL-3.0-only
"""#3752 (étape 2 de #3749) : schéma de test migré une fois, puis cloné.

`migrations.migrate` rejouait l'historique entier (~150 migrations, ~1,5 s
de DDL côté serveur) à chaque classe de test : 36 % de test_3220 en psycopg.
Désormais la première classe d'une base migre son schéma comme avant, puis en
tire un **gabarit** : le `pg_dump` de ce schéma tout juste migré (avant tout
test), rangé dans la base elle-même (`ameesh_test_gabarits.gabarit`). Il sert
donc une fois par base — une fois par job de CI, par part de
`scripts/test-parallele.sh`, par banc local — à tous les processus. Les
classes suivantes rejouent ce DDL final sous leur propre nom de schéma
(~0,65 s) au lieu de l'historique.

- **Isolation inchangée** : chaque classe a toujours son propre schéma, vierge
  de toute donnée de test ; seul le chemin qui le construit change.
- **Clé** (`empreinte`) : version, nom et somme de chaque migration, version
  du serveur, rôle courant et rôles de contrat `ameesh_*` du cluster (une
  migration accorde des droits aux rôles qui existent, 0427). Une migration
  ajoutée ou modifiée invalide le gabarit.
- **Durée de vie bornée** (`TTL_S`) : des migrations écrivent `now()` (borne
  du rattrapage, 0438) ; un clone a au plus `TTL_S` de retard sur un schéma
  migré à l'instant, très en deçà des écarts que posent les tests (un jour).
- **Les deux pilotes** : le rejeu passe par le pilote de la classe
  (`transaction()` + `script()`), psql comme psycopg.
- **Repli** sur `migrate()`, comme avant : `AMEESH_TEST_GABARIT=0`, nom de
  schéma non simple, aucun `pg_dump` de version majeure ≥ celle du serveur,
  ou rejeu en échec (le gabarit fautif est alors retiré).

Seule différence connue avec un `migrate()` : une valeur tirée au hasard par
une migration (identité du journal d'événements, 0448) est celle du gabarit,
commune aux classes clonées d'un même gabarit — aucun test ne compare deux
classes entre elles.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import shutil
import subprocess
import time

from ameesh import db as db_mod
from ameesh import dsn as dsn_mod
from ameesh import migrations

#: à incrémenter si la normalisation du DDL change (invalide les gabarits)
FORMAT = 1
TTL_S = 20 * 60
SCHEMA = "ameesh_test_gabarits"
TABLE = SCHEMA + ".gabarit"
_LOCK = "ameesh_test_gabarit"
#: un nom que `pg_dump` écrit tel quel, sans guillemets : le remplacement
#: textuel du nom source par le nom cible est alors exact
_SIMPLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_RESTRICT = re.compile(r"(?m)^\\(?:un)?restrict .*\n?")
#: les réglages de session de l'en-tête (et de chaque objet) de pg_dump
_SET = re.compile(r"(?m)^SET ([a-z_]+ = [^;\n]*;)$")
_SEARCH_PATH = "SELECT pg_catalog.set_config('search_path', '', false);"

#: chemin pris par la dernière préparation (témoins) : "clone", "migrate"
#: ou "migrate+gabarit"
dernier: str | None = None
#: empreinte -> (schéma source, DDL, échéance en temps monotone)
_cache: dict[str, tuple[str, str, float]] = {}
#: version majeure du serveur -> pg_dump utilisable (ou None)
_pg_dumps: dict[int, str | None] = {}
#: gabarits dont le rejeu a échoué dans ce processus : pas de reconstruction
#: à chaque classe (migrate() seul, comme avant)
_echecs: set[str] = set()


def actif() -> bool:
    return os.environ.get("AMEESH_TEST_GABARIT", "1") != "0"


def empreinte(db, directory: str = migrations.MIGRATIONS_DIR) -> str:
    """Clé du gabarit : tout ce dont dépend le résultat de `migrate()`."""
    h = hashlib.sha256(b"ameesh-gabarit %d\n" % FORMAT)
    for migration in migrations.discover(directory):
        h.update(("%s %s\n" % (migration.label, migration.checksum)).encode())
    row = db.query(
        "SELECT current_setting('server_version_num') AS version, current_user AS moi, "
        "coalesce((SELECT string_agg(rolname, ',' ORDER BY rolname) FROM pg_roles "
        "WHERE left(rolname, 7) = 'ameesh_'), '') AS roles")[0]
    h.update(("%s\n%s\n%s\n" % (row["version"], row["moi"], row["roles"])).encode())
    return h.hexdigest()


def _major(binary: str) -> int | None:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\b(\d+)(?:\.\d+)?", out)
    return int(match.group(1)) if match else None


def pg_dump_pour(serveur: int) -> str | None:
    """Un `pg_dump` de version majeure ≥ `serveur` (il refuse un serveur plus
    récent que lui) : celui du PATH, sinon une version installée à côté
    (Debian/Ubuntu, Homebrew)."""
    if serveur not in _pg_dumps:
        candidats = [shutil.which("pg_dump")]
        candidats += sorted(glob.glob("/usr/lib/postgresql/*/bin/pg_dump"), reverse=True)
        for prefixe in ("/opt/homebrew", "/usr/local"):
            candidats += sorted(glob.glob(prefixe + "/opt/postgresql@*/bin/pg_dump"),
                                reverse=True)
        _pg_dumps[serveur] = next(
            (c for c in candidats if c and (_major(c) or 0) >= serveur), None)
    return _pg_dumps[serveur]


def _serveur(db) -> int:
    return int(db.query("SELECT current_setting('server_version_num') AS v")[0]["v"]) // 10000


def _dump(db, schema: str) -> str | None:
    """DDL et données du schéma `schema`, prêts à rejouer dans une transaction
    du pilote ; None sans `pg_dump` utilisable."""
    binary = pg_dump_pour(_serveur(db))
    if not binary:
        return None
    password, base = dsn_mod.split_password(db.cfg.dsn)
    env = dict(os.environ, LC_ALL="C")
    if password:
        env["PGPASSWORD"] = password
    try:
        out = subprocess.run(
            [binary, "--schema=" + schema, "--no-owner", "--inserts", "--dbname=" + base],
            capture_output=True, text=True, env=env, timeout=120, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if _SEARCH_PATH not in out or ("CREATE SCHEMA %s;" % schema) not in out:
        return None
    # Le rejeu vit dans une transaction de la connexion de la classe : les
    # réglages de session de l'en-tête y deviennent locaux (la connexion garde
    # son search_path), et les méta-commandes psql (\restrict) disparaissent.
    out = _RESTRICT.sub("", out)
    out = out.replace(_SEARCH_PATH, "SELECT pg_catalog.set_config('search_path', '', true);")
    return _SET.sub(r"SET LOCAL \1", out)


def _lire(db, cle: str) -> tuple[str, str] | None:
    entry = _cache.get(cle)
    if entry and entry[2] > time.monotonic():
        return entry[0], entry[1]
    if not db.query("SELECT to_regclass(%s) IS NOT NULL AS ok", (TABLE,))[0].get("ok"):
        return None
    rows = db.query(
        "SELECT source, ddl, extract(epoch FROM now() - built_at) AS age FROM " + TABLE +
        " WHERE cle = %s AND built_at > now() - make_interval(secs => %s)", (cle, TTL_S))
    if not rows:
        return None
    row = rows[0]
    _cache[cle] = (row["source"], row["ddl"],
                   time.monotonic() + TTL_S - float(row["age"]))
    return row["source"], row["ddl"]


def _ranger(db, cle: str, schema: str) -> bool:
    """Tire le gabarit du schéma `schema` (tout juste migré) et le range ;
    sans attente : si un autre processus le construit, on passe."""
    if cle in _echecs or pg_dump_pour(_serveur(db)) is None:
        return False
    with db.transaction() as tx:
        if not tx.query("SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok",
                        (_LOCK,))[0].get("ok"):
            return False
        tx.execute("CREATE SCHEMA IF NOT EXISTS " + SCHEMA)
        tx.execute("CREATE TABLE IF NOT EXISTS " + TABLE + " ("
                   "cle text PRIMARY KEY, source text NOT NULL, ddl text NOT NULL, "
                   "built_at timestamptz NOT NULL DEFAULT now())")
        if tx.query("SELECT 1 AS ok FROM " + TABLE + " WHERE cle = %s"
                    " AND built_at > now() - make_interval(secs => %s)", (cle, TTL_S)):
            return False
        ddl = _dump(tx, schema)
        if ddl is None:
            return False
        tx.execute("DELETE FROM " + TABLE + " WHERE cle = %s OR built_at < now() - interval '1 day'",
                   (cle,))
        tx.execute("INSERT INTO " + TABLE + " (cle, source, ddl) VALUES (%s, %s, %s)",
                   (cle, schema, ddl))
    _cache.pop(cle, None)
    return True


def _retirer(db, cle: str) -> None:
    _cache.pop(cle, None)
    try:
        db.execute("DELETE FROM " + TABLE + " WHERE cle = %s", (cle,))
    except db_mod.DbError:
        pass


def cloner(db, schema: str, source: str, ddl: str) -> None:
    """Rejoue le gabarit `ddl` (tiré du schéma `source`) sous le nom `schema`,
    en une transaction : tout ou rien."""
    with db.transaction() as tx:
        tx.script(ddl.replace(source, schema))


def preparer(db, schema: str) -> str:
    """Rend le schéma `schema` (celui de `db.cfg`) migré à jour, par clone du
    gabarit quand il existe, sinon par `migrate()` (qui fonde alors le
    gabarit). Rend le chemin pris, aussi noté dans `dernier`."""
    global dernier
    if not actif() or not _SIMPLE.match(schema) or schema == SCHEMA:
        migrations.migrate(db, log=None)
        dernier = "migrate"
        return dernier
    cle = empreinte(db)
    trouve = _lire(db, cle)
    if trouve is not None:
        source, ddl = trouve
        try:
            cloner(db, schema, source, ddl)
            if not migrations.pending(db):
                dernier = "clone"
                return dernier
        except db_mod.DbError:
            pass
        # un rôle de contrat disparu entre-temps, un gabarit incomplet… : on
        # le retire et on revient à l'historique, sur un schéma propre
        _echecs.add(cle)
        _retirer(db, cle)
        db.execute("DROP SCHEMA IF EXISTS %s CASCADE" % db_mod.quote_ident(schema))
    migrations.migrate(db, log=None)
    dernier = "migrate"
    try:
        if _ranger(db, cle, schema):
            dernier = "migrate+gabarit"
    except db_mod.DbError:
        pass
    return dernier
