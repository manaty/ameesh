# SPDX-License-Identifier: AGPL-3.0-only
"""#3963: concurrent executors publish each authenticator transition once."""
import threading
from types import SimpleNamespace
from unittest import mock

from ameesh import canon_sync, storage
from ameesh.modules import declarations
from ameesh.modules.storage import ModuleStore
from . import test_canon as canon_tests
from .support import PgTestCase


class AuthStateConcurrencyTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DROP TRIGGER IF EXISTS slow3963 ON canon_state")
        self.db.execute("DELETE FROM canon_state")
        storage.of(self.db).canon.record_state(
            "host3963", "ok", root="", source="", commit="", good=True,
            diagnostic="")
        storage.of(self.db).canon.record_auth_state("host3963", "ok", "")
        # Force overlap between the read and write of the old implementation.
        self.db.execute("CREATE OR REPLACE FUNCTION slow3963() RETURNS trigger LANGUAGE plpgsql "
                        "AS $$ BEGIN PERFORM pg_sleep(0.15); RETURN NEW; END $$")
        self.db.execute("CREATE TRIGGER slow3963 BEFORE UPDATE ON canon_state "
                        "FOR EACH ROW EXECUTE FUNCTION slow3963()")

    def race(self, done):
        barrier = threading.Barrier(4, timeout=10)
        posts, errors = [], []

        def run():
            conn = self.connect()
            try:
                barrier.wait()
                canon_sync.record_authenticators(conn, "host3963", done)
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        with mock.patch.object(canon_sync, "_post_authenticators",
                               side_effect=lambda *args: posts.append(args[3])):
            threads = [threading.Thread(target=run) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
            self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        return posts

    def test_error_and_recovery_are_each_announced_once(self):
        error = canon_sync.AuthenticatorSync(canon_sync.AUTH_REFUSED, "refusal3963")
        self.assertEqual(len(self.race(error)), 1)
        self.assertEqual(len(self.race(error)), 0)
        self.assertEqual(len(self.race(canon_sync.AuthenticatorSync(
            canon_sync.AUTH_SYNCED, result={"errors": []}))), 1)


    def test_repeated_transient_is_announced_once(self):
        error = canon_sync.AuthenticatorSync(
            canon_sync.AUTH_REFUSED, "timeout3963", transient=True)
        self.assertEqual(len(self.race(error)), 1)
        self.assertEqual(len(self.race(error)), 0)
        self.assertEqual(len(self.race(canon_sync.AuthenticatorSync(
            canon_sync.AUTH_SYNCED, result={"errors": []}))), 1)

class PeriodicSyncTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.canon = SimpleNamespace(
            id="canon3963", is_default=True, untrusted=False, readable=True,
            sources=[SimpleNamespace(member="root", repo="repo", commit="abc", mode="git")],
            root="root", load_findings=[])

    def test_one_winner_then_shared_state_and_separate_hosts(self):
        store = storage.of(self.db).canon
        self.assertTrue(store.claim_sync("host", "", "first", 300))
        self.assertFalse(store.claim_sync("host", "", "second", 300))
        self.assertIsNone(store.shared_sync("host", ""))
        store.complete_sync("host", "", "wrong-token", "wrong")
        self.assertIsNone(store.shared_sync("host", ""))
        store.complete_sync("host", "", "first", "sig")
        self.assertEqual(store.shared_sync("host", "")["sync_signature"], "sig")
        self.assertTrue(store.claim_sync("other-host", "", "other", 300))
        self.assertTrue(store.claim_sync("host", "other-canon", "other", 300))
        self.db.execute("UPDATE canon_state SET sync_claimed_at = now()-interval '301 seconds' "
                        "WHERE host='host' AND canon=''")
        self.assertTrue(store.claim_sync("host", "", "replacement", 300))
        store.complete_sync("host", "", "first", "late-completion")
        self.assertIsNone(store.shared_sync("host", ""))

    def test_concurrent_claims_have_exactly_one_winner(self):
        barrier = threading.Barrier(4, timeout=10)
        results, errors = [], []

        def run(index):
            conn = self.connect()
            try:
                barrier.wait()
                results.append(storage.of(conn).canon.claim_sync("host", "", str(index), 300))
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, False, False, True])

    def test_pending_is_not_complete_and_changed_snapshot_is_not_reused(self):
        started, finish = threading.Event(), threading.Event()
        results, errors = [], []

        def sync(db, canon, host):
            started.set()
            if not finish.wait(10):
                raise TimeoutError("test sync did not resume")
            storage.of(db).canon.record_state(
                host, "ok", root="", source="", commit="abc", good=True, diagnostic="")
            return canon_sync.SyncReport(host, "root", False, [], [])

        def run():
            conn = self.connect()
            try:
                results.append(canon_sync.periodic_sync(conn, self.canon, "host", 300))
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        with mock.patch.object(canon_sync, "state_key", return_value=""), \
                mock.patch.object(canon_sync, "configured_ref", return_value="origin/main"), \
                mock.patch.object(canon_sync, "sync", side_effect=sync) as sync_mock:
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(started.wait(10))
                self.assertIsNone(canon_sync.periodic_sync(self.db, self.canon, "host", 300))
            finally:
                finish.set()
                worker.join(20)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            self.assertEqual(canon_sync.periodic_sync(
                self.db, self.canon, "host", 300).status, "ok")
            self.canon.sources[0].repo = "another-clone-of-the-same-snapshot"
            self.assertTrue(canon_sync.periodic_sync(
                self.db, self.canon, "host", 300).shared)
            self.canon.sources[0].commit = "different"
            self.assertIsNone(canon_sync.periodic_sync(self.db, self.canon, "host", 300))
            self.assertEqual(sync_mock.call_count, 1)


class UnchangedModulesTest(PgTestCase):
    def test_identical_declarations_do_not_rewrite_or_fire_insert_trigger(self):
        entries = declarations({"extensions": {"ameesh": {"projects": {
            "project3963": {"modules": ["it"]}}}}})
        store = ModuleStore(self.db)
        store.replace("canon3963", entries)
        self.db.execute("CREATE FUNCTION reject3963() RETURNS trigger LANGUAGE plpgsql "
                        "AS $$ BEGIN RAISE EXCEPTION 'unexpected module write'; END $$")
        self.db.execute("CREATE TRIGGER reject3963 BEFORE INSERT OR UPDATE OR DELETE "
                        "ON project_modules FOR EACH ROW EXECUTE FUNCTION reject3963()")
        store.replace("canon3963", entries)
        self.assertTrue(store.enabled("project3963", "it"))


class UnchangedAuthenticatorsTest(canon_tests._AuthenticatorCase):
    def test_same_canon_and_registry_do_not_write(self):
        from ameesh import canon
        self.sync()
        self.db.execute("CREATE FUNCTION reject_auth3963() RETURNS trigger LANGUAGE plpgsql "
                        "AS $$ BEGIN RAISE EXCEPTION 'unexpected auth sync write'; END $$")
        for table in ("authenticators", "authenticator_syncs", "project_modules"):
            self.db.execute(
                "CREATE TRIGGER reject_auth3963 BEFORE INSERT OR UPDATE OR DELETE "
                "ON %s FOR EACH ROW EXECUTE FUNCTION reject_auth3963()" % table)
        done = canon_sync.sync_authenticators(self.db, canon.load(self.clone), host="atelier")
        self.assertEqual(done.status, canon_sync.AUTH_SYNCED, done)
