# SPDX-License-Identifier: AGPL-3.0-only
"""Isolation des tests de contrats qui exposent des ACL à toute la base."""
from contextlib import ExitStack
from dataclasses import replace

from ameesh import db as db_mod
from ameesh import dsn as dsn_mod

#: Base d'administration commune à tous les workers d'une grappe. Un verrou
#: consultatif ne vaut que pour UNE base : le prendre sur la base jetable du
#: processus (#6803, `test-parts.py run --parallel`) ne sérialise pas les
#: workers entre eux, alors que l'audit du contrat (`pg_database`) et les
#: `GRANT ... TO PUBLIC` touchent toutes les bases de la grappe (#8481).
#: Chaque worker dérive donc la même base de son DSN : seuls le nom de base et
#: le schéma changent, l'hôte et le rôle restent ceux du banc.
#: `template1` est écartée : une session de garde y bloquerait les
#: `CREATE DATABASE` sans `TEMPLATE` (« source database is being accessed »).
ADMIN_DATABASES = ("postgres",)


class RoleAclGuard:
    """Session qui tient le verrou, pour les témoins d'isolation."""

    def __init__(self, transaction, pid, database):
        self.transaction = transaction
        self.pid = pid
        self.database = database


def _admin_database(cfg):
    """Connexion ouverte sur une base commune de la grappe, ou None.

    `postgres` est présente dans toute grappe et joignable par le rôle du banc.
    On vérifie la base réellement atteinte (`current_database`) plutôt que le
    DSN : un `dbname=` de la requête ou un profil de connexion peut rediriger.
    Si elle n'est pas joignable (profil limité à sa base), le fallback est la
    base du processus : la garde reste utile entre classes d'une même base,
    sans sérialiser les workers — c'est le mieux possible sans base commune.
    """
    if getattr(cfg, "backend", "") == "mediated":
        return None
    for name in ADMIN_DATABASES:
        try:
            dsn = dsn_mod.with_database(cfg.dsn, name)
        except dsn_mod.DsnError:
            return None
        try:
            db = db_mod.connect(replace(cfg, dsn=dsn, statement_timeout_ms=0),
                                writer=False)
        except db_mod.DbError:
            continue
        try:
            reached = db.query("SELECT current_database() AS name")[0]["name"]
        except db_mod.DbError:
            db.close()
            continue
        if reached == name:
            return db
        db.close()
    return None


def serialize_role_acl(test_class):
    """Garde installation, assertions et nettoyage sous le même verrou.

    Les schémas jetables ne cloisonnent pas PUBLIC ni pg_default_acl : un
    contrat peut voir une pollution volontaire d'une autre classe. Une
    transaction dédiée garde le verrou entre les appels psql des scripts,
    avec les deux pilotes. Le cleanup unittest passe aussi si setUpClass
    échoue ; la fermeture du processus libère le verrou si le worker meurt.

    #8481 : le verrou est pris sur `postgres` (base d'administration commune),
    # pas sur la base jetable du worker : deux instances de la même classe dans
    # deux processus de la part doivent s'attendre, puisque leurs
    # `GRANT ... TO PUBLIC` temporaires et l'audit `pg_database` du contrat se
    # croisent à l'échelle de la grappe.
    """
    stack = ExitStack()
    test_class.addClassCleanup(stack.close)
    db = _admin_database(test_class.cfg)
    if db is None:
        db = db_mod.connect(replace(test_class.cfg, statement_timeout_ms=0),
                            writer=False)
    stack.callback(db.close)
    tx = stack.enter_context(db.transaction())
    tx.execute("SET LOCAL statement_timeout = 0")
    tx.execute("SELECT pg_advisory_xact_lock("
               "hashtextextended('ameesh-test-role-public-acl', 0))")
    row = tx.query("SELECT pg_backend_pid() AS pid, "
                   "current_database() AS name")[0]
    return RoleAclGuard(tx, row["pid"], row["name"])
