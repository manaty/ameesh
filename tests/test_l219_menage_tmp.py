# SPDX-License-Identifier: AGPL-3.0-only
"""L219: the housekeeping pass never deletes the temporary directory of an
agent in turn, nor one of another mesh sharing the state directory.

Incident of 2026-10-11 (06:01, 06:11, 06:22): two meshes of one host (same
user, same state directory) deleted each other's `.menage/tmp/<agent>`
every 10 min — the auditor's live session lost its command outputs —
because `run_pass` deleted every directory whose name was missing from ITS
registry, before looking at the agents in turn.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from ameesh import menage, registry

from .support import PgTestCase

HOUR = 3600.0


class _Cfg:
    def __init__(self, state_dir: str, dsn: str, schema: str):
        self.state_dir, self.dsn, self.schema, self.host = state_dir, dsn, schema, "pc"


def _write(path: str, age_s: float = 0.0) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("x" * 100)
    if age_s:
        at = time.time() - age_s
        os.utime(path, (at, at))


def _age(directory: str, age_s: float) -> None:
    at = time.time() - age_s
    for root, dirs, files in os.walk(directory, topdown=False):
        for name in files + dirs:
            os.utime(os.path.join(root, name), (at, at))
    os.utime(directory, (at, at))


class MeshKeyTest(unittest.TestCase):

    def test_same_database_same_key(self):
        def key(dsn, schema="public"):
            return menage.mesh_key(_Cfg("/s", dsn, schema))
        a = key("postgresql://ameesh:secret@10.77.0.1:5432/ameesh?sslmode=require")
        self.assertEqual(a, key("postgresql://other@10.77.0.1/ameesh"))
        self.assertNotEqual(a, key("postgresql://ameesh@10.77.0.1:5432/ameesh", "mesh2"))
        self.assertNotEqual(a, key("postgresql://ameesh@127.0.0.1:55432/ameesh"))
        self.assertEqual(key("host=h port=5432 dbname=d user=u password=p"),
                         key("host=h dbname=d user=v"))


class OrphanTmpTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM housekeeping_log")
        self.mine = _Cfg(self.state, self.cfg.dsn, self.cfg.schema)
        # the other mesh of the host: same state directory, other schema
        self.other = _Cfg(self.state, self.cfg.dsn, self.cfg.schema + "_autre")
        self.pol = menage.Policy()
        registry.upsert(self.db, "a1", harness="claude", host="pc")

    def _pass(self, *, later: float = 0.0, **kw) -> dict:
        return menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t",
                               now=time.time() + later, **kw)

    def _tmp(self, cfg, agent: str) -> str:
        return menage.turn_env(cfg, self.pol, agent, {})["TMPDIR"]

    def test_other_mesh_directory_is_never_deleted(self):
        # the auditor of the other mesh, idle for a day: not in our registry
        theirs = self._tmp(self.other, "auditeur")
        _write(os.path.join(theirs, "claude-1000", "out.txt"))
        _age(theirs, 24 * HOUR)
        report = self._pass()
        self.assertTrue(os.path.isdir(theirs))
        self.assertTrue(os.path.exists(os.path.join(theirs, "claude-1000", "out.txt")))
        self.assertEqual([e for e in report["entries"] if e["kind"] == "tmp"], [])
        self.assertEqual([p for p in report["plan"] if p["kind"] == "tmp"], [])

    def test_directory_without_owner_is_kept_unless_idle_for_a_day(self):
        # before L219, or created by another mesh's older version
        self._tmp(self.mine, "a1")                       # the managed root exists
        legacy = os.path.join(menage.tmp_root(self.mine, self.pol), "ancien")
        _write(os.path.join(legacy, "f"))
        _age(legacy, 23 * HOUR)
        report = self._pass()
        self.assertTrue(os.path.isdir(legacy))
        self.assertTrue(any("sans propriétaire" in s for s in report["skipped"]))
        self._pass(later=25 * HOUR)
        self.assertFalse(os.path.exists(legacy))

    def test_unowned_directory_of_a_registered_name_is_not_evicted(self):
        # review of #176, point 4: `a1` exists here, but the directory has
        # no owner file (another mesh's `a1`, older version): untouched
        self.pol = menage.Policy(tmp_quota=10)
        self._tmp(self.mine, "b")
        legacy = os.path.join(menage.tmp_root(self.mine, self.pol), "a1")
        _write(os.path.join(legacy, "f"), age_s=3 * HOUR)
        os.utime(legacy, (time.time() - 3 * HOUR,) * 2)
        self._pass()
        self.assertTrue(os.path.exists(os.path.join(legacy, "f")))

    def test_agent_in_turn_is_kept_even_unregistered(self):
        gone = self._tmp(self.mine, "parti")
        _write(os.path.join(gone, "f"))
        _age(gone, 24 * HOUR)
        report = self._pass(agents_in_turn={"parti"})
        self.assertTrue(os.path.isdir(gone))
        self.assertTrue(any("en tour" in s for s in report["skipped"]))

    def test_own_orphan_deleted_only_once_idle(self):
        gone = self._tmp(self.mine, "parti")
        _write(os.path.join(gone, "sortie.txt"))
        report = self._pass()
        self.assertTrue(os.path.isdir(gone))                 # written just now
        self.assertTrue(any("actif il y a" in s for s in report["skipped"]))
        report = self._pass(later=3 * HOUR)
        self.assertFalse(os.path.exists(gone))
        self.assertFalse(os.path.exists(menage.owner_file(
            menage.tmp_root(self.mine, self.pol), "parti")))
        self.assertEqual([(e["kind"], e["action"], e["agent"]) for e in report["entries"]
                          if e["kind"] == "tmp"], [("tmp", "deleted", "parti")])

    def test_registered_agent_directory_kept_and_owner_survives_session_start(self):
        mine = self._tmp(self.mine, "a1")
        _write(os.path.join(mine, "f"), age_s=48 * HOUR)
        menage.Turn.begin(self.mine, self.pol, "a1", None, new_session=True, base_env={})
        self.assertEqual(menage.read_owner(menage.tmp_root(self.mine, self.pol), "a1"),
                         menage.mesh_key(self.mine))
        self._pass(later=24 * HOUR)
        self.assertTrue(os.path.isdir(mine))

    # -- review of #176 -------------------------------------------------------

    def test_new_session_never_empties_a_homonym_of_another_mesh(self):
        # point 1: `a1` of the other mesh is active; a new session of our
        # `a1` neither empties its directory nor takes it over
        theirs = self._tmp(self.other, "a1")
        _write(os.path.join(theirs, "sortie.txt"))
        turn = menage.Turn.begin(self.mine, self.pol, "a1", None, new_session=True,
                                 base_env={})
        try:
            troot = menage.tmp_root(self.mine, self.pol)
            self.assertTrue(os.path.exists(os.path.join(theirs, "sortie.txt")))
            self.assertEqual(menage.read_owner(troot, "a1"), menage.mesh_key(self.other))
            ours = turn.env["TMPDIR"]
            self.assertNotEqual(ours, theirs)
            self.assertEqual(menage.read_owner(troot, os.path.basename(ours)),
                             menage.mesh_key(self.mine))
        finally:
            turn.end(lot=None, turn_id="t")
        _age(ours, 48 * HOUR)
        self._pass(later=48 * HOUR)                     # registered: kept
        self.assertTrue(os.path.isdir(ours))

    def test_turn_lock_protects_against_a_pass_that_does_not_see_it(self):
        # point 2: a turn has started (another runner: absent from
        # `agents_in_turn` and from the leases), the directory looks idle
        turn = menage.Turn.begin(self.mine, self.pol, "parti", None, new_session=False,
                                 base_env={})
        mine = turn.env["TMPDIR"]
        _write(os.path.join(mine, "f"))
        _age(mine, 3 * HOUR)
        report = self._pass(later=3 * HOUR)
        self.assertTrue(os.path.isdir(mine))
        self.assertTrue(any("verrou" in s for s in report["skipped"]))
        turn.end(lot=None, turn_id="t")
        _age(mine, 3 * HOUR)
        self._pass(later=3 * HOUR)
        self.assertFalse(os.path.exists(mine))

    def test_quota_spares_an_agent_with_a_live_lease(self):
        # point 3: `a1` is in turn under another runner (live lease)
        self.pol = menage.Policy(tmp_quota=10)
        mine = self._tmp(self.mine, "a1")
        _write(os.path.join(mine, "f"), age_s=3 * HOUR)
        self.db.execute(
            "UPDATE agent_registry SET status = 'running', lease_owner = 'autre',"
            " lease_expires_at = now() + interval '1 hour' WHERE name = 'a1'")
        self._pass()
        self.assertTrue(os.path.exists(os.path.join(mine, "f")))
        self.db.execute("UPDATE agent_registry SET lease_owner = NULL WHERE name = 'a1'")
        self._pass()
        self.assertFalse(os.path.exists(os.path.join(mine, "f")))

    def test_activity_is_read_at_any_depth(self):
        # point 5: old files, but a file just removed deep inside
        gone = self._tmp(self.mine, "parti")
        deep = os.path.join(gone, "a", "b")
        _write(os.path.join(deep, "vieux"))
        _write(os.path.join(deep, "efface"))
        _age(gone, 3 * HOUR)
        os.unlink(os.path.join(deep, "efface"))         # `b` changed now
        report = self._pass()
        self.assertTrue(os.path.isdir(gone))
        self.assertTrue(any("actif il y a" in s for s in report["skipped"]))

    def test_resumed_turn_refreshes_activity(self):
        # point 5: a resumed session writes nothing at first
        gone = self._tmp(self.mine, "parti")
        _write(os.path.join(gone, "f"))
        _age(gone, 3 * HOUR)
        turn = menage.Turn.begin(self.mine, self.pol, "parti", None, new_session=False,
                                 base_env={})
        turn.end(lot=None, turn_id="t")
        self._pass()
        self.assertTrue(os.path.isdir(gone))


    # -- second review of #176 ------------------------------------------------

    def _hold_exclusive(self, cfg, agent):
        troot = menage.tmp_root(cfg, self.pol)
        return menage.tmp_lock(troot, menage.tmp_name(troot, agent, menage.mesh_key(cfg)),
                               exclusive=True)

    def test_turn_without_its_lock_gets_a_fallback_directory(self):
        # point 1: a pass holds the lock beyond the wait
        own = self._tmp(self.mine, "a1")
        _write(os.path.join(own, "f"))
        held = self._hold_exclusive(self.mine, "a1")
        with mock.patch.object(menage, "LOCK_WAIT_S", 0.1), \
                mock.patch.object(menage, "CLEAR_WAIT_S", 0.1):
            turn = menage.Turn.begin(self.mine, self.pol, "a1", None, new_session=True,
                                     base_env={})
        held.close()
        try:
            other = turn.env["TMPDIR"]
            self.assertNotEqual(other, own)
            self.assertTrue(os.path.basename(other).startswith("a1" + menage.FALLBACK_SEP))
            self.assertTrue(os.path.exists(os.path.join(own, "f")))
            # the fallback directory is locked too
            report = self._pass(later=3 * HOUR)
            self.assertTrue(os.path.isdir(other))
            self.assertTrue(any("verrou" in s for s in report["skipped"]))
        finally:
            turn.end(lot=None, turn_id="t")
        self._pass(later=3 * HOUR)                       # used once: deleted once idle
        self.assertFalse(os.path.exists(other))
        self.assertTrue(os.path.isdir(own))

    def test_attach_session_holds_the_lock(self):
        # point 1: `ameesh attach` (runner._attach_tmp_env)
        from ameesh import runner
        holds: list = []
        env = runner._attach_tmp_env(self.mine, "parti", {}, holds)
        try:
            _write(os.path.join(env["TMPDIR"], "f"))
            report = self._pass(later=3 * HOUR)
            self.assertTrue(os.path.isdir(env["TMPDIR"]))
            self.assertTrue(any("verrou" in s for s in report["skipped"]))
        finally:
            for hold in holds:
                hold.release()
        self._pass(later=3 * HOUR)
        self.assertFalse(os.path.exists(env["TMPDIR"]))

    def test_turn_end_evicts_under_its_lock(self):
        # point 2: nobody may take the directory between release and eviction
        turn = menage.Turn.begin(self.mine, self.pol, "a1", None, new_session=False,
                                 base_env={})
        seen = []
        real = menage.evict_lru

        def evict(directories, root, *a, **kw):
            lock = self._hold_exclusive(self.mine, "a1")
            seen.append(lock is None)
            if lock is not None:
                lock.close()
            return real(directories, root, *a, **kw)
        with mock.patch.object(menage, "evict_lru", evict):
            turn.end(lot=None, turn_id="t")
        self.assertEqual(seen, [True])
        self.assertIsNotNone(self._hold_exclusive(self.mine, "a1"))   # released after

    def test_recent_extraction_with_old_mtimes_is_active(self):
        # point 3: mtimes kept by an extraction, ctime of now
        gone = self._tmp(self.mine, "parti")
        _write(os.path.join(gone, "archive", "f"))
        _age(gone, 3 * HOUR)
        report = self._pass()
        self.assertTrue(os.path.isdir(gone))
        self.assertTrue(any("actif il y a" in s for s in report["skipped"]))

    def test_adopted_directory_is_never_emptied(self):
        # point 5: found without owner file (another mesh's, older version)
        troot = menage.tmp_root(self.mine, self.pol)
        self._tmp(self.mine, "b")
        legacy = os.path.join(troot, "a1")
        _write(os.path.join(legacy, "theirs.txt"))
        for _session in range(2):
            turn = menage.Turn.begin(self.mine, self.pol, "a1", None, new_session=True,
                                     base_env={})
            turn.end(lot=None, turn_id="t")
            self.assertTrue(os.path.exists(os.path.join(legacy, "theirs.txt")))
        self.assertFalse(menage.created_by(troot, "a1", menage.mesh_key(self.mine)))
        # ours, created by us: emptied by a new session
        mine = self._tmp(self.mine, "c")
        _write(os.path.join(mine, "f"))
        turn = menage.Turn.begin(self.mine, self.pol, "c", None, new_session=True,
                                 base_env={})
        turn.end(lot=None, turn_id="t")
        self.assertFalse(os.path.exists(os.path.join(mine, "f")))


class MeshKeyParamsTest(unittest.TestCase):

    def test_host_given_as_a_parameter(self):
        # point 6
        def key(dsn):
            return menage.mesh_key(_Cfg("/s", dsn, "public"))
        self.assertNotEqual(key("postgresql:///d?host=a"), key("postgresql:///d?host=b"))
        self.assertEqual(key("postgresql:///d?host=a"), key("postgresql://a/d"))
        self.assertNotEqual(key("postgresql://h/d?port=5433"), key("postgresql://h/d"))
        # second review, point 4
        self.assertNotEqual(key("hostaddr=10.0.0.1 dbname=d"), key("hostaddr=10.0.0.2 dbname=d"))

    DSNS = (
        "postgresql://u:p@H.example:5433/base?sslmode=require",
        "postgres://u@[::1]:5432/d%20b",
        "postgresql://h1:5432,h2:5433/d",
        "postgresql:///d?host=%2Fvar%2Frun%2Fpostgresql&port=5434",
        "postgresql://h/d?hostaddr=10.0.0.1",
        "host=h port = 5433 dbname='ma base' user=u password='a\\'b c'",
        "hostaddr=10.0.0.1 dbname=d\\ x",
        "",
    )

    def test_without_psycopg(self):
        # the psql driver runs without psycopg: the key needs only Python
        with mock.patch.dict("sys.modules", {"psycopg": None, "psycopg.conninfo": None}):
            for dsn in self.DSNS:
                menage.mesh_key(_Cfg("/s", dsn, "public"))
        self.assertEqual(menage._conninfo_fields("host=h port = 5433 dbname='ma base' x='a\\'b'"),
                         {"host": "h", "port": "5433", "dbname": "ma base", "x": "a'b"})

    def test_same_fields_as_libpq(self):
        try:
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            self.skipTest("psycopg absent")
        for dsn in self.DSNS:
            want = {k: str(v) for k, v in conninfo_to_dict(dsn).items()
                    if k in ("host", "hostaddr", "port", "dbname")}
            got = {k: v for k, v in menage._conninfo_fields(dsn).items()
                   if k in ("host", "hostaddr", "port", "dbname")}
            self.assertEqual(got, want, dsn)


if __name__ == "__main__":
    unittest.main()
