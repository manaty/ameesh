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
    for root, dirs, files in os.walk(directory):
        for name in files:
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

    def test_directory_without_owner_is_kept(self):
        # before L219, or created by another mesh's older version
        self._tmp(self.mine, "a1")                       # the managed root exists
        legacy = os.path.join(menage.tmp_root(self.mine, self.pol), "ancien")
        _write(os.path.join(legacy, "f"))
        _age(legacy, 24 * HOUR)
        report = self._pass()
        self.assertTrue(os.path.isdir(legacy))
        self.assertTrue(any("sans propriétaire" in s for s in report["skipped"]))

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
        _age(gone, 3 * HOUR)
        report = self._pass()
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


if __name__ == "__main__":
    unittest.main()
