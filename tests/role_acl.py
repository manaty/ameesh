# SPDX-License-Identifier: AGPL-3.0-only
"""Isolation des tests de contrats qui exposent des ACL à toute la base."""
from contextlib import ExitStack
from dataclasses import replace

from ameesh import db as db_mod


def serialize_role_acl(test_class):
    """Garde installation, assertions et nettoyage sous le même verrou.

    Les schémas jetables ne cloisonnent pas PUBLIC ni pg_default_acl : un
    contrat peut voir une pollution volontaire d'une autre classe. Une
    transaction dédiée garde le verrou entre les appels psql des scripts,
    avec les deux pilotes. Le cleanup unittest passe aussi si setUpClass
    échoue ; la fermeture du processus libère le verrou si le worker meurt.
    """
    stack = ExitStack()
    test_class.addClassCleanup(stack.close)
    db = db_mod.connect(replace(test_class.cfg, statement_timeout_ms=0))
    stack.callback(db.close)
    tx = stack.enter_context(db.transaction())
    tx.execute("SET LOCAL statement_timeout = 0")
    tx.execute("SELECT pg_advisory_xact_lock("
               "hashtextextended('ameesh-test-role-public-acl', 0))")
