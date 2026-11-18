# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 (étude #7616 §4.6) : résolution du schéma physique du module.

Le code de stockage du module n'écrit jamais ``public.ci_runs`` ni
``public__dev.ci_runs`` en dur : il passe par :func:`relation`, qui lit le nom
physique dans ``module_schemas`` (registre de P1a, #7649) et rend le nom
courant.

* avant P2 (#7651), aucune ligne ``dev`` n'existe : le nom est le schéma noyau,
  ``<noyau>.<table>`` ;
* après P2, la migration ``dev/0001`` a inscrit ``<noyau>__dev`` : le même code
  rend ``<noyau>__dev.<table>``, sans second changement.

:class:`ModuleDb` applique ce résolveur à chaque instruction du stockage du
module : c'est la seule couche qui a le droit de nommer les tables ``dev``
(le test de frontière le vérifie). La réécriture se fait hors des littéraux
SQL (une valeur ``'ci_runs'`` ou un commentaire reste intact) et sans doubler
une qualification déjà présente (``public.ci_runs``).
"""
from __future__ import annotations

import contextlib
import re

from .... import config
from ....storage import interface

#: Une ligne de `module_schemas` (écrite par le runner de migrations).
MODULE = "dev"

#: Forme d'un nom de schéma physique (contrainte de `module_schemas`, P1a).
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote(name: str) -> str:
    """Un identifiant SQL, toujours cité (jamais deviné)."""
    return '"%s"' % name.replace('"', '""')


def _raw(db):
    """La connexion derrière un :class:`ModuleDb` (sans empiler les enveloppes)."""
    while isinstance(db, ModuleDb):
        db = object.__getattribute__(db, "_db")
    return db


def _connection(db):
    """L'objet qui porte le cache : la connexion, pas la transaction éphémère.

    Le pilote psql rend une transaction (``db.driver`` est la connexion
    persistante) ; psycopg rend le pilote lui-même. Une migration P2 arrête les
    exécuteurs : le schéma ne change pas sous une connexion vivante.
    """
    db = _raw(db)
    return getattr(db, "driver", db)


def _core_schema(db) -> str:
    """Le schéma noyau configuré, dans la forme admise par ``config``.

    B2 (#7650) : ``SCHEMA_NAME`` est la forme, plus étroite, des schémas
    physiques de module (minuscules) ; un schéma noyau valide peut contenir
    une majuscule ou ``$`` (``config.SCHEMA_RE``). Le repli silencieux sur
    ``public`` écrivait alors dans le mauvais schéma : on refuse fermé.
    """
    cfg = getattr(_raw(db), "cfg", None)
    schema = str(getattr(cfg, "schema", "") or "")
    if not config.SCHEMA_RE.fullmatch(schema):
        raise ValueError("invalid core schema: %r" % (schema,))
    return schema


def _registry_present(db, core: str) -> bool:
    """Le registre ``module_schemas`` existe-t-il dans le schéma noyau ?

    ``to_regclass`` rend NULL pour une relation absente au lieu de lever :
    sur une base d'avant P1a, la transaction de l'appelant reste utilisable
    (relecture cx2 #226204). Le registre présent mais illisible, lui, lève :
    on ne simule pas un repli que PostgreSQL a déjà invalidé.
    """
    rows = _raw(db).query(
        "SELECT to_regclass(%s) IS NOT NULL AS present",
        ("%s.module_schemas" % _quote(core),))
    return bool(rows and (rows[0] or {}).get("present"))


def schema_name(db) -> str:
    """Le schéma physique des tables du module, lu dans ``module_schemas``.

    Lecture unique par connexion (cache positif ET négatif : le registre ne
    change qu'avec les exécuteurs arrêtés). Une base d'avant P1a et un nom
    hors forme retombent sur le schéma noyau ; un registre présent mais
    illisible lève, sans repli silencieux.
    """
    connection = _connection(db)
    cached = getattr(connection, "_ameesh_dev_schema", None)
    if cached is not None:
        return cached
    core = _core_schema(db)
    found = core
    if _registry_present(db, core):
        # la connexion brute : passer par `ModuleDb` rappellerait `qualify`,
        # qui rappellerait `schema_name` (récursion).
        rows = _raw(db).query(
            "SELECT schema_name FROM %s.module_schemas WHERE module = %%s"
            % _quote(core), (MODULE,))
        if rows:
            candidate = str((rows[0] or {}).get("schema_name") or "")
            if SCHEMA_NAME.fullmatch(candidate):
                found = candidate
    try:
        connection._ameesh_dev_schema = found
    except Exception:  # noqa: BLE001 - proxy sans attributs : on relira
        pass
    return found


def relation(db, table: str) -> str:
    """Le nom qualifié courant de ``table``, lu au registre (jamais deviné)."""
    if not _IDENT.fullmatch(str(table)):
        raise ValueError("invalid module table name: %r" % (table,))
    return "%s.%s" % (_quote(schema_name(db)), _quote(str(table)))


#: Les tables du module, dans l'ordre de déclaration (import paresseux : ce
#: module est importé par le stockage, `tables` ne dépend de rien).
def tables():
    from ..tables import TABLES
    return TABLES


def _names_re():
    from ..tables import TABLES
    return re.compile(r"(?<![A-Za-z0-9_.\"])(%s)(?![A-Za-z0-9_\"])"
                      % "|".join(sorted((re.escape(name) for name in TABLES),
                                        key=len, reverse=True)))


_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")

#: Le contexte qui fait d'un nom une **table** : les colonnes homonymes
#: (`work_item_measures.ci_runs`) ne sont jamais qualifiées.
_TABLE_PREFIX = re.compile(
    r"(?is)\b(?:FROM|JOIN|UPDATE|INSERT\s+INTO|DELETE\s+FROM"
    r"|TRUNCATE(?:\s+TABLE)?|ALTER\s+TABLE)\s+(?:ONLY\s+)?$")


def _in_table_position(text: str, start: int) -> bool:
    """Le nom qui commence à ``start`` suit-il un mot-clé de table ?"""
    return bool(_TABLE_PREFIX.search(text[max(0, start - 160):start]))


def qualify(db, sql: str) -> str:
    """Qualifie les tables du module dans ``sql``, hors littéraux et commentaires.

    Une instruction du stockage du module peut donc nommer ``ci_runs`` comme
    avant : la couche de stockage la rend telle quelle au pilote avec le nom
    physique courant. Seules les positions de table (``FROM``, ``JOIN``,
    ``UPDATE``, ``INSERT INTO``, ``DELETE FROM``, ``TRUNCATE``, ``ALTER
    TABLE``) sont réécrites : une colonne homonyme garde son nom. Les
    occurrences déjà qualifiées (``public.ci_runs``) et les valeurs écrites
    entre quotes (``table_name = 'ci_runs'``) sont laissées intactes.
    """
    text = str(sql)
    pattern = _names_re()
    if not pattern.search(text):
        return text
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "'":                       # littéral SQL, '' échappé
            start = index
            index += 1
            while index < length:
                if text[index] == "'":
                    if index + 1 < length and text[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            out.append(text[start:index])
            continue
        if char == '"':                       # identifiant cité, "" échappé
            start = index
            index += 1
            while index < length:
                if text[index] == '"':
                    if index + 1 < length and text[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            out.append(text[start:index])
            continue
        if char == "$":
            match = _DOLLAR.match(text, index)
            if match:
                tag = match.group(0)
                end = text.find(tag, match.end())
                if end != -1:
                    end += len(tag)
                    out.append(text[index:end])
                    index = end
                    continue
        if char == "-" and text.startswith("--", index):
            end = text.find("\n", index)
            end = length if end == -1 else end
            out.append(text[index:end])
            index = end
            continue
        if char == "/" and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end == -1 else end + 2
            out.append(text[index:end])
            index = end
            continue
        match = pattern.match(text, index)
        if match:
            if _in_table_position(text, index):
                out.append(relation(db, match.group(1)))
            else:
                out.append(match.group(1))
            index = match.end()
            continue
        out.append(char)
        index += 1
    return "".join(out)


class ModuleDb:
    """La connexion vue par le stockage du module : chaque SQL est qualifié.

    Les transactions sont enveloppées aussi : ``with self.db.transaction() as
    tx`` rend un ``ModuleDb`` sur ``tx``, donc le SQL écrit dans la transaction
    est qualifié de la même façon. Les attributs (``cfg``, caches du pilote)
    sont délégués à la connexion réelle.
    """

    __slots__ = ("_db",)

    def __init__(self, db):
        object.__setattr__(self, "_db", _raw(db))

    def __getattr__(self, name):
        if name == "_db":
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_db"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_db"), name, value)

    def query(self, sql, *args, **kwargs):
        db = object.__getattribute__(self, "_db")
        return db.query(qualify(db, sql), *args, **kwargs)

    def query_batch(self, queries):
        """Qualifie chaque SQL du lot, puis délègue au pilote (un aller-retour).

        Les paires déjà qualifiées par le domaine sont laissées telles quelles
        (`qualify` ne redouble pas une qualification) : le rapport peut donc
        mêler ses lectures noyau et celles du module dans le même lot.
        """
        db = object.__getattribute__(self, "_db")
        items = []
        for query in queries:
            if isinstance(query, str):
                items.append(qualify(db, query))
            else:
                sql, params = query
                items.append((qualify(db, sql), params))
        return db.query_batch(items)

    def execute(self, sql, *args, **kwargs):
        db = object.__getattribute__(self, "_db")
        return db.execute(qualify(db, sql), *args, **kwargs)

    def script(self, sql, *args, **kwargs):
        db = object.__getattribute__(self, "_db")
        return db.script(qualify(db, sql), *args, **kwargs)

    @contextlib.contextmanager
    def transaction(self, **kwargs):
        db = object.__getattribute__(self, "_db")
        with db.transaction(**kwargs) as tx:
            yield ModuleDb(tx)


class DevDomain(interface.Domain):
    """Un domaine de stockage du module `dev` : SQL qualifié à chaque appel."""

    __slots__ = ()

    def __init__(self, db):
        super().__init__(ModuleDb(db))
