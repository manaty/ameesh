# SPDX-License-Identifier: AGPL-3.0-only
"""#7357: local quota reads run once and transactions follow cached reads."""
from types import SimpleNamespace
import unittest
from unittest import mock

from ameesh import accounts, cost, db as db_mod, exploitation, storage
from tests.test_l61_allers_retours import _FausseBase
from tests.support import PgTestCase


class OveragePreloadTest(unittest.TestCase):
    def test_local_read_once_and_shared_read_still_preloaded(self):
        base = _FausseBase()
        profile = SimpleNamespace(kind="config_dir", name="local")
        cfg = SimpleNamespace(accounts={}, state_dir="unused", host="h1")
        book = mock.Mock()
        gauges = cost.Gauges()
        store = mock.Mock()
        store.operations.latest_gauges.side_effect = lambda **kw: view[0].query(
            "SELECT id FROM a") and []
        view = [None]

        def read(db):
            view[0] = db
            return exploitation._overage_alerts(cfg, db, 1000)

        with mock.patch.object(accounts, "parse", return_value={"claude": [profile]}), \
                mock.patch.object(cost, "CostBook", return_value=book) as create, \
                mock.patch.object(accounts, "gauges_of", return_value=gauges) as local, \
                mock.patch.object(storage, "of", return_value=store):
            self.assertEqual(db_mod.batched(base, read), [])
        create.assert_called_once()
        local.assert_called_once_with(book, profile, [profile])
        self.assertEqual(len(base.echanges), 1)
        self.assertGreater(store.operations.latest_gauges.call_count, 1)


class AlertOrderingTest(PgTestCase):
    def test_transaction_families_follow_preloaded_families(self):
        from ameesh import provider_credits, sous_utilisation, staging_watch
        reads = []
        original = self.db.query

        def query(sql, params=()):
            if sql == "SELECT 7357 AS probe":
                reads.append(sql)
            return original(sql, params)

        def late_read(db, **kw):
            db.query("SELECT 7357 AS probe")
            return []

        def transaction(cfg, db, *args, **kw):
            with db.transaction():
                pass
            return []

        with mock.patch.object(self.db, "query", side_effect=query), \
                mock.patch.object(staging_watch, "executor_alerts", side_effect=late_read), \
                mock.patch.object(provider_credits, "alerts", return_value=[]), \
                mock.patch.object(sous_utilisation, "alerts", side_effect=transaction):
            db_mod.batched(self.db, lambda db: exploitation.alerts(
                self.cfg, db, local_pool=False), levels=3)
        # The probe travels in a batch and is replayed from that answer. A
        # preceding transaction would abort the dry pass and force a query.
        self.assertEqual(reads, [])


class SchemaPreloadTest(unittest.TestCase):
    def test_schema_probe_does_not_speculate_about_missing_columns(self):
        from ameesh.storage.postgres import operations, work

        class Base(_FausseBase):
            def _lignes(self, sql, params):
                if 'information_schema.columns' in sql:
                    return [{"n": self.column_count}]
                return []

        for current in (True, False):
            with self.subTest(current=current):
                base = Base()
                base.column_count = len(work._OPTIONAL_ITEM_COLUMNS) - (not current)
                db_mod.batched(base, lambda db: operations.Operations(db).open_lots_activity(5))
                activity = [sql for trip in base.echanges for sql in trip
                            if 'last_activity_ts ASC' in sql]
                self.assertEqual(len(activity), 1)
                self.assertEqual('to_jsonb(w)' in activity[0], not current)
                # The schema count travels before the query it determines.
                self.assertEqual(len(base.echanges), 2)

    def test_known_empty_prerequisite_is_read_once(self):
        base = _FausseBase()
        # This base returns [] for an explicit absent-schema probe.
        with mock.patch.object(base, '_lignes', return_value=[]):
            result = db_mod.batched(base, lambda db: db_mod.prerequisite_query(
                db, 'SELECT absent FROM schema_probe'))
        self.assertEqual(result, [])
        self.assertEqual(len(base.echanges), 1)
