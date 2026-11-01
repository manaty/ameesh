# SPDX-License-Identifier: AGPL-3.0-only
"""#3741 — délais de requête en rafale sur la base commune du mesh.

Constat (26/10, ~90 relevés de pg_stat_activity sur la base commune) : la vue
`agent_mesh_overview` occupait 40 % des requêtes actives (son compte des
non-lus passifs relisait le tas du courrier), `hosts.current` et
`menage.last_measure` parcouraient tout l'historique pour trouver leurs
hôtes, et `work.issue_feed` (synchro GitHub) calculait cinq fois `to_jsonb`
par lot. Sous cette charge, canon sync passait d'erreur à ok à chaque délai
dépassé, avec deux diffusions à « all » par oscillation.

Témoins : le compte passif lit l'index partiel 0489 (même prédicat que
`PASSIVE_SQL`), les lectures par hôte rendent le dernier relevé de chaque
hôte, le flux GitHub ne calcule plus `to_jsonb` de la ligne (#5404), et une
erreur passagère de la base n'est annoncée qu'à sa répétition.
"""
from __future__ import annotations

import json
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import canon_sync, receipts, storage
from ameesh import db as db_mod
from ameesh.storage.postgres import work as work_pg
from ameesh.storage.postgres.mailbox import PASSIVE_SQL

from .support import PgTestCase


# --------------------------------------------------------------------------
# canon sync : une erreur passagère n'oscille plus au fil
# --------------------------------------------------------------------------

class _FakeCanonStore:
    """`canon_state.auth_status` / `auth_diagnostic` d'un hôte, en mémoire."""

    def __init__(self) -> None:
        self.row: dict | None = {"auth_status": "ok", "auth_diagnostic": ""}

    def record_auth_state(self, host, status, diagnostic, canon="", *, quiet_prefix=""):
        previous = dict(self.row) if self.row is not None else None
        if quiet_prefix and (previous or {}).get("auth_status") != "error":
            diagnostic = (quiet_prefix + diagnostic)[:4000]
        self.row = {"auth_status": status, "auth_diagnostic": diagnostic}
        return previous


def _timeout() -> canon_sync.AuthenticatorSync:
    exc = db_mod.DbError("canceling statement due to statement timeout",
                         sqlstate=db_mod.QUERY_CANCELED)
    return canon_sync.AuthenticatorSync(
        canon_sync.AUTH_REFUSED, "database: %s — transaction rolled back" % exc,
        transient=canon_sync.transient_db_error(exc))


def _ok() -> canon_sync.AuthenticatorSync:
    return canon_sync.AuthenticatorSync("synced", result={"errors": []})


def _refused(reason="home @ abc does not descend") -> canon_sync.AuthenticatorSync:
    return canon_sync.AuthenticatorSync(canon_sync.AUTH_REFUSED, reason)


class TransientClassificationTest(unittest.TestCase):
    def test_sqlstate_and_type_decide_never_the_text(self):
        transient = canon_sync.transient_db_error
        self.assertTrue(transient(db_mod.DbError("x", sqlstate="57014")))
        self.assertTrue(transient(db_mod.DbError("x", sqlstate="55P03")))
        self.assertTrue(transient(db_mod.DbError("x", sqlstate="40P01")))
        self.assertTrue(transient(db_mod.DbError("x", sqlstate="08006")))
        self.assertTrue(transient(db_mod.Unavailable("x")))
        self.assertTrue(transient(receipts.RegistryLockError("x")))
        self.assertFalse(transient(db_mod.DbError("x", sqlstate="42P01")))
        self.assertFalse(transient(db_mod.DbError(
            "canceling statement due to statement timeout")))


class QuietTransientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _FakeCanonStore()
        self.posts: list[str] = []
        fake = SimpleNamespace(canon=self.store)
        patches = [
            mock.patch.object(canon_sync.storage, "of", lambda db: fake),
            mock.patch.object(canon_sync, "_post_authenticators",
                              lambda db, host, status, text, done:
                              self.posts.append(text)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def record(self, done) -> None:
        canon_sync.record_authenticators(object(), "agents-1", done)

    def test_one_timeout_then_ok_writes_nothing_to_the_thread(self):
        self.record(_timeout())
        self.assertEqual(self.posts, [])
        # l'état durable dit l'erreur, marquée non annoncée
        self.assertEqual(self.store.row["auth_status"], "error")
        self.assertTrue(self.store.row["auth_diagnostic"].startswith(
            canon_sync.QUIET_MARK))
        self.record(_ok())
        self.assertEqual(self.posts, [])
        self.assertEqual(self.store.row["auth_status"], "ok")

    def test_ten_oscillations_write_nothing(self):
        for _ in range(10):
            self.record(_timeout())
            self.record(_ok())
        self.assertEqual(self.posts, [])

    def test_a_repeated_timeout_is_announced_once_then_its_recovery(self):
        self.record(_timeout())
        self.record(_timeout())
        self.assertEqual(len(self.posts), 1)
        self.assertIn("IN ERROR", self.posts[0])
        self.assertFalse(self.store.row["auth_diagnostic"].startswith(
            canon_sync.QUIET_MARK))
        self.record(_timeout())
        self.assertEqual(len(self.posts), 1)
        self.record(_ok())
        self.assertEqual(len(self.posts), 2)
        self.assertIn("error-free again", self.posts[1])

    def test_a_real_refusal_is_announced_at_once(self):
        self.record(_refused())
        self.assertEqual(len(self.posts), 1)
        self.assertIn("does not descend", self.posts[0])
        self.record(_ok())
        self.assertEqual(len(self.posts), 2)

    def test_a_timeout_after_an_announced_error_is_announced(self):
        self.record(_refused())
        self.record(_timeout())
        self.assertEqual(len(self.posts), 2)
        self.assertIn("statement timeout", self.posts[1])

    def test_a_real_refusal_after_a_quiet_timeout_is_announced(self):
        self.record(_timeout())
        self.record(_refused())
        self.assertEqual(len(self.posts), 1)
        self.assertIn("does not descend", self.posts[0])


# --------------------------------------------------------------------------
# lectures de la base commune
# --------------------------------------------------------------------------

class IssueFeedSqlTest(unittest.TestCase):
    def test_to_jsonb_once_per_row(self):
        seen: list[str] = []

        class _Db:
            _has_reviewer = True
            _has_migration_slots = True

            def query(self, sql, params=()):
                seen.append(sql)
                return []

        work_pg.WorkItems(_Db()).issue_feed(10)
        sql = seen[-1]
        # #5404 : plus aucun `to_jsonb` de la ligne, seulement de `reviewers`
        self.assertNotIn("to_jsonb(work_items)", sql)
        self.assertEqual(len(re.findall(r"to_jsonb\(", sql)), 1, sql)

    def test_item_columns_direct(self):
        class _Db:
            _has_reviewer = True
            _has_migration_slots = True
        cols = work_pg.item_columns(_Db())
        self.assertIn("to_jsonb(reviewers) AS reviewers", cols)
        self.assertIn("external_executor AS external_executor", cols)
        self.assertEqual(len(re.findall(r"wake_ts", cols)), 1)


class SharedReadsDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("host_resources", "housekeeping_log"):
            self.db.execute("DELETE FROM %s" % table)

    def _reading(self, host, seconds_ago, load1):
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at, load1)"
            " VALUES (%s, now() - make_interval(secs => %s), %s)",
            (host, float(seconds_ago), float(load1)))

    def test_current_is_the_last_reading_of_each_host(self):
        for host in ("zeta", "alpha", "mid"):
            for age, load in ((300, 1.0), (60, 2.0), (120, 3.0)):
                self._reading(host, age, load)
        rows = storage.of(self.db).hosts.current(None)
        self.assertEqual([r["host"] for r in rows], ["alpha", "mid", "zeta"])
        self.assertEqual({r["load1"] for r in rows}, {2.0})
        only = storage.of(self.db).hosts.current("mid")
        self.assertEqual([(r["host"], r["load1"]) for r in only], [("mid", 2.0)])
        self.assertEqual(storage.of(self.db).hosts.current("absent"), [])

    def test_current_of_an_empty_history(self):
        self.assertEqual(storage.of(self.db).hosts.current(None), [])

    def _log(self, host, kind, seconds_ago, detail):
        self.db.execute(
            "INSERT INTO housekeeping_log (host, actor, kind, action, at, detail)"
            " VALUES (%s, 'test', %s, 'mesure', now() - make_interval(secs => %s), %s)",
            (host, kind, float(seconds_ago), detail))

    def test_last_measure_of_each_host(self):
        for host in ("b-host", "a-host"):
            self._log(host, "mesure", 300, "old")
            self._log(host, "mesure", 30, "new")
            self._log(host, "purge", 1, "not a measure")
        self._log("c-host", "purge", 1, "no measure at all")
        menage = storage.of(self.db).housekeeping
        rows = menage.last_measure(None)
        self.assertEqual([(r["host"], r["detail"]) for r in rows],
                         [("a-host", "new"), ("b-host", "new")])
        self.assertEqual([(r["host"], r["detail"]) for r in menage.last_measure("b-host")],
                         [("b-host", "new")])
        self.assertEqual(menage.last_measure("c-host"), [])


class PassiveUnreadIndexDbTest(PgTestCase):
    INDEX = "agent_mailbox_passive_unread_idx"

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM agent_mailbox")

    def _plan(self, sql) -> str:
        # le pilote psql enveloppe chaque lecture dans un agrégat JSON, où
        # `EXPLAIN` ne peut pas entrer : le plan se lit sous psycopg (pilote
        # par défaut de la CI), le prédicat est comparé sous les deux.
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest("EXPLAIN is wrapped by the psql driver")
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL enable_seqscan = off")
            tx.execute("SET LOCAL enable_bitmapscan = off")
            return json.dumps(tx.query("EXPLAIN (FORMAT JSON) " + sql))

    def test_the_index_predicate_is_passive_sql(self):
        # PostgreSQL décompile les deux prédicats de la même façon : un index
        # de contrôle bâti sur `PASSIVE_SQL` doit rendre le même texte.
        self.db.execute("DROP TABLE IF EXISTS mailbox_3741")
        self.db.execute("CREATE TABLE mailbox_3741 (LIKE agent_mailbox)")
        self.db.execute(
            "CREATE INDEX mailbox_3741_idx ON mailbox_3741 (recipient, id)"
            " WHERE delivered_at IS NULL AND "
            + PASSIVE_SQL.format(t="mailbox_3741"))
        rows = self.db.query(
            "SELECT c.relname, pg_get_expr(i.indpred, i.indrelid) AS pred"
            "  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE c.relname IN (%s, 'mailbox_3741_idx')"
            "   AND c.relnamespace = current_schema()::regnamespace",
            (self.INDEX,))
        preds = {r["relname"]: r["pred"] for r in rows}
        self.assertEqual(set(preds), {self.INDEX, "mailbox_3741_idx"})
        self.assertEqual(preds[self.INDEX], preds["mailbox_3741_idx"])

    def _mail(self, recipient, payload, delivered=False):
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, payload, delivered_at)"
            " VALUES ('test', %s, 'x', %s::jsonb, CASE WHEN %s THEN now() END)",
            (recipient, json.dumps(payload), delivered))

    def test_the_overview_count_reads_the_partial_index(self):
        # sans parcours séquentiel ni bitmap, seul un index dont le prédicat
        # est prouvé inclus par celui de la vue peut servir le compte : le
        # témoin casse si 0489 et la vue divergent.
        plan = self._plan("SELECT name, passive_unread FROM agent_mesh_overview")
        self.assertIn(self.INDEX, plan)

    def test_the_counts_are_unchanged(self):
        self.db.execute(
            "INSERT INTO agent_registry (name) VALUES ('t3741') ON CONFLICT DO NOTHING")
        for payload in ({"ack": True}, {"info": True}, {"cc": "orch"},
                        {"radar": {"lot": 1}}, {"broadcast": True, "urgent": True},
                        {}, {"ci_passif": True}):
            self._mail("t3741", payload)
        self._mail("t3741", {"ack": True}, delivered=True)
        row = self.db.query("SELECT unread, passive_unread FROM agent_mesh_overview"
                            " WHERE name = 't3741'")[0]
        self.assertEqual((row["unread"], row["passive_unread"]), (7, 5))


if __name__ == "__main__":
    unittest.main()
