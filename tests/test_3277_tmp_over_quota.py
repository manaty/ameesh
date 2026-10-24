# SPDX-License-Identifier: AGPL-3.0-only
"""#3277: an agent's temporary directory owned by another mesh key, over
quota, is alerted; under a former key of THIS mesh, its entries of ended lots
are removed and the others kept.

Constat of 24/10 on pc-smichea: `.menage/tmp/codex2` (25 GiB, quota 4 GiB)
carried the key of the same database reached before pgbouncer (#532): the
pass skipped it as another mesh's, so nothing measured, evicted or alerted.

Witnesses: a stopped agent's overrun is alerted (size and biggest entries),
once per refresh, lifted under quota or once gone; an entry of an ended lot is
removed (former key only); an unattributed one is kept and named.
"""
from __future__ import annotations

import dataclasses
import os
import time
import unittest
from unittest import mock

from ameesh import (exploitation, housekeeping as hk, housekeeping_stale as stale,
                    menage, registry, storage, work)

from .support import PgTestCase

DAY = 24 * 3600.0
BIG = 64 * 1024
#: the ctime of a file cannot be set back: a pass that may remove runs later
LATER = DAY + 600


class _Cfg:
    def __init__(self, state_dir: str, dsn: str, schema: str):
        self.state_dir, self.dsn, self.schema, self.host = state_dir, dsn, schema, "pc"


def _write(path: str, size: int = BIG) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(os.urandom(size))


def _age(directory: str, age_s: float) -> None:
    at = time.time() - age_s
    for root, dirs, files in os.walk(directory, topdown=False):
        for name in files + dirs:
            os.utime(os.path.join(root, name), (at, at), follow_symlinks=False)
    os.utime(directory, (at, at))


class PolicyTest(unittest.TestCase):

    def test_former_mesh_keys_are_validated_and_united(self):
        parsed, problems, unknown = hk.parse_policy(
            {"former_mesh_keys": ["14c7cf1a946ed14b", "14c7cf1a946ed14b"]})
        self.assertEqual((problems, unknown), ([], []))
        self.assertEqual(parsed["former_mesh_keys"], ("14c7cf1a946ed14b",))
        for bad in ("14c7cf1a946ed14b", ["14C7"], [12]):
            self.assertTrue(hk.parse_policy({"former_mesh_keys": bad})[1], bad)

        class Card:
            def __init__(self, keys):
                self.policy = type("P", (), {"housekeeping": {"former_mesh_keys": keys},
                                             "work_root": None, "work_roots": None})()

        class Canon:
            def __init__(self, ident, keys):
                self.id, self.card = ident, Card(keys)

            def host(self, _host):
                return self.card

        pol = hk.effective([Canon("a", ("aaaaaaaaaaaaaaaa",)),
                            Canon("b", ("bbbbbbbbbbbbbbbb",))], "pc")
        self.assertEqual(pol.former_mesh_keys, ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"))


class OverQuotaTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        stale.clear_cache()
        self.db.execute("DELETE FROM housekeeping_log")
        self.mine = _Cfg(self.state, self.cfg.dsn, self.cfg.schema)
        # the same database by its former route: another key (#532)
        self.former = _Cfg(self.state, self.cfg.dsn, self.cfg.schema + "_avant")
        self.pol = menage.Policy(tmp_quota=4 * BIG)
        # codex2: stopped, retired — registered, never in turn
        registry.upsert(self.db, "codex2", harness="codex", host="pc")
        registry.upsert(self.db, "codex1", harness="codex", host="pc")
        # the former route created `codex2` first; this one then `codex2~<key>`
        self.old = menage.turn_env(self.former, self.pol, "codex2", {})["TMPDIR"]
        menage.turn_env(self.mine, self.pol, "codex2", {})
        self.assertEqual(os.path.basename(self.old), "codex2")
        self.addCleanup(stale.clear_cache)

    def ended_lot(self, *, assignee="codex2", age_s=2 * DAY) -> int:
        lot = work.add(self.db, title="ended lot", assignee=assignee)["id"]
        work.close(self.db, lot, abandoned=True)
        at = time.time() - age_s
        self.db.execute("UPDATE work_items SET updated_at = to_timestamp(%s),"
                        " closed_at = to_timestamp(%s) WHERE id = %s", (at, at, lot))
        return lot

    def _pass(self, pol=None, *, later: float = 0.0, **kw) -> dict:
        return menage.run_pass(self.mine, self.db, pol or self.pol, host="pc", actor="t",
                               now=time.time() + later, **kw)

    def _alerts(self) -> list[dict]:
        return [a for a in exploitation._housekeeping_alerts([], self.db)
                if a["type"] == "tmp_over_quota"]

    def _over(self, report) -> list[dict]:
        return [e for e in report["entries"]
                if e.get("path") == self.old and e.get("action") == "over_quota"]

    def test_stopped_agent_over_quota_under_another_key_is_alerted_never_deleted(self):
        lot = self.ended_lot()
        _write(os.path.join(self.old, "diag%d" % lot, "core"), 6 * BIG)
        _write(os.path.join(self.old, "scratch", "f"))
        _age(self.old, 2 * DAY)
        report = self._pass()
        # another mesh's (no former key declared): nothing deleted
        self.assertTrue(os.path.exists(os.path.join(self.old, "diag%d" % lot, "core")))
        first = self._over(report)
        self.assertEqual(len(first), 1)
        self.assertGreater(first[0]["bytes"], self.pol.tmp_quota)
        self.assertEqual(first[0]["agent"], "codex2")
        self.assertEqual(first[0]["data"]["top"][0]["name"], "diag%d" % lot)
        self.assertFalse(first[0]["data"]["former"])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["path"], self.old)
        self.assertIn("diag%d" % lot, alerts[0]["detail"])
        self.assertIn("former_mesh_keys", alerts[0]["detail"])
        # one constat per refresh window, same `since`
        self.assertEqual(self._over(self._pass(later=3600)), [])
        self.assertEqual(len(self._alerts()), 1)
        again = self._over(self._pass(later=hk.UNOWNED_REFRESH_S + 60))
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["data"]["first_seen"], first[0]["data"]["first_seen"])
        # back under quota: lifted
        os.unlink(os.path.join(self.old, "diag%d" % lot, "core"))
        os.utime(self.old)
        lifted = self._pass(later=hk.UNOWNED_REFRESH_S + 120)
        self.assertIn("resolved", [e["action"] for e in lifted["entries"]
                                   if e.get("path") == self.old])
        self.assertEqual(self._alerts(), [])

    def test_a_gone_directory_lifts_the_alert(self):
        _write(os.path.join(self.old, "scratch", "f"), 6 * BIG)
        self._pass()
        self.assertEqual(len(self._alerts()), 1)
        hk._safe_rmtree(self.old, hk.tmp_root(self.mine, self.pol))
        gone = self._pass(later=60)
        self.assertIn("gone", [e["action"] for e in gone["entries"]
                               if e.get("path") == self.old])
        self.assertEqual(self._alerts(), [])

    def test_former_key_removes_ended_lot_entries_and_keeps_the_others(self):
        lot = self.ended_lot()
        recent = self.ended_lot(age_s=-DAY)          # ended 10 min before the pass
        theirs = self.ended_lot(assignee="codex1")
        opened = work.add(self.db, title="open", assignee="codex2")["id"]
        for name in ("diag%d" % lot, "ci-run-%d" % recent, "review-%d" % theirs,
                     "probe-%d" % opened, "node-compile-cache"):
            _write(os.path.join(self.old, name, "f"), 2 * BIG)
        _write(os.path.join(self.old, "%d-targeted.log" % lot), BIG)
        _age(self.old, 2 * DAY)
        pol = dataclasses.replace(self.pol, former_mesh_keys=(menage.mesh_key(self.former),))
        # dry run: the plan says it, nothing goes, nothing is written
        dry = self._pass(pol, dry_run=True, later=LATER)
        planned = {os.path.basename(p["path"]) for p in dry["plan"]
                   if p["action"] == "suppression"}
        self.assertEqual(planned, {"diag%d" % lot, "%d-targeted.log" % lot})
        self.assertTrue(os.path.exists(os.path.join(self.old, "diag%d" % lot)))
        self.assertEqual(self._alerts(), [])
        report = self._pass(pol, later=LATER)
        self.assertFalse(os.path.exists(os.path.join(self.old, "diag%d" % lot)))
        self.assertFalse(os.path.exists(os.path.join(self.old, "%d-targeted.log" % lot)))
        deleted = [e for e in report["entries"] if e["action"] == "deleted"]
        self.assertEqual(sorted(e["lot"] for e in deleted), [str(lot)] * 2)
        for kept in ("ci-run-%d" % recent, "review-%d" % theirs, "probe-%d" % opened,
                     "node-compile-cache"):
            self.assertTrue(os.path.exists(os.path.join(self.old, kept, "f")), kept)
        # still over quota with what it keeps: alerted, with the reasons
        over = self._over(report)
        self.assertEqual(len(over), 1)
        self.assertTrue(over[0]["data"]["former"])
        reasons = {row["name"]: row.get("kept") for row in over[0]["data"]["top"]}
        self.assertIn("unattributed", reasons["node-compile-cache"])
        self.assertIn("not codex2's", reasons["review-%d" % theirs])
        self.assertIn("not ended", reasons["probe-%d" % opened])
        self.assertIn("waits", reasons["ci-run-%d" % recent])
        self.assertIn("former route of this mesh", self._alerts()[0]["detail"])

    def test_former_key_keeps_an_active_or_locked_entry(self):
        lot = self.ended_lot()
        _write(os.path.join(self.old, "diag%d" % lot, "f"), 6 * BIG)     # active now
        pol = dataclasses.replace(self.pol, former_mesh_keys=(menage.mesh_key(self.former),))
        self._pass(pol)
        self.assertTrue(os.path.exists(os.path.join(self.old, "diag%d" % lot, "f")))
        _age(self.old, 2 * DAY)
        stale.clear_cache()
        troot = hk.tmp_root(self.mine, self.pol)
        held = hk.tmp_lock(troot, "codex2", exclusive=False)          # a turn of the old route
        try:
            report = self._pass(pol, later=LATER)
        finally:
            held.close()
        self.assertTrue(os.path.exists(os.path.join(self.old, "diag%d" % lot, "f")))
        self.assertIn("tmp %s kept: in turn (lock held)" % self.old, report["skipped"])
        stale.clear_cache()
        self._pass(pol, later=LATER + 60)
        self.assertFalse(os.path.exists(os.path.join(self.old, "diag%d" % lot)))

    def _former(self):
        return dataclasses.replace(self.pol, former_mesh_keys=(menage.mesh_key(self.former),))

    def _diag(self) -> tuple[int, str]:
        lot = self.ended_lot()
        target = os.path.join(self.old, "diag%d" % lot, "nested", "result")
        _write(target, 6 * BIG)
        _age(self.old, 2 * DAY)
        return lot, target

    def test_a_deep_write_after_the_cached_measure_keeps_the_entry(self):
        # codex r1 B1: the first-level signature does not see a deep write;
        # the activity proof is a fresh walk right before the removal
        _lot, target = self._diag()
        at = time.time() + LATER
        stale.measure(self.old, now=at)                       # cached, idle
        with open(target, "ab") as fh:
            fh.write(b"fresh work")
        os.utime(target, (at - 60, at - 60))                  # a minute before the pass
        self._pass(self._former(), later=LATER)
        self.assertTrue(os.path.exists(target))
        over = self._over_any()
        self.assertIn("active", over["data"]["top"][0]["kept"])

    def _over_any(self) -> dict:
        rows = [r for r in storage.of(self.db).housekeeping.latest_by_path("pc", "tmp")
                if r.get("path") == self.old]
        self.assertEqual([r["action"] for r in rows], ["over_quota"])
        return rows[0]

    def test_a_bind_mount_or_an_unreadable_mount_table_keeps_the_entry(self):
        # codex r1 B2: a bind mount on the same file system keeps its st_dev
        _lot, target = self._diag()
        mounted = os.path.dirname(target)
        with mock.patch.object(stale.hk, "_mount_points", return_value={mounted}):
            self._pass(self._former(), later=LATER)
        self.assertTrue(os.path.exists(target))
        self.assertIn("mount point", self._over_any()["data"]["top"][0]["kept"])
        stale.clear_cache()
        with mock.patch.object(stale.hk, "_mount_points", return_value=None):
            self._pass(self._former(), later=LATER + 60)
        self.assertTrue(os.path.exists(target))

    def test_a_key_former_on_host_a_is_current_on_host_b(self):
        # mesh-design 24/10: 14c7cf1a is pc-smichea's former route and
        # agents-2's live key. The declaration is read per host only, and a
        # current key is never former, even if a card listed it.
        key = menage.mesh_key(self.former)

        class Canon:
            id = "c"

            def host(self, name):
                keys = [key] if name == "A" else []
                return type("H", (), {"policy": type("P", (), {
                    "housekeeping": {"former_mesh_keys": tuple(keys)} if keys else {},
                    "work_root": None, "work_roots": None})()})()

        self.assertEqual(hk.effective([Canon()], "A").former_mesh_keys, (key,))
        self.assertEqual(hk.effective([Canon()], "B").former_mesh_keys, ())
        _lot, target = self._diag()
        troot = hk.tmp_root(self.mine, self.pol)
        report = stale.check(self.db, troot=troot, name="codex2", agent="codex2",
                             owner=key, key=key, policy=self._former(),
                             now=time.time() + LATER, dry_run=False, previous=None,
                             agents=["codex2"], holder=lambda _p: "")
        self.assertTrue(os.path.exists(target))
        self.assertEqual([e for e in report["entries"] if e["action"] == "deleted"], [])
        self.assertEqual(report["plan"], [])

    @unittest.skipIf(os.getuid() == 0, "root reads every directory")
    def test_a_subtree_of_another_user_is_left_out_not_a_doubt(self):
        lot = self.ended_lot()
        diag = os.path.join(self.old, "diag%d" % lot)
        _write(os.path.join(diag, "core"), 6 * BIG)
        locked = os.path.join(diag, "xvfb-run.KIxBwC")
        os.makedirs(locked)
        _age(self.old, 2 * DAY)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)
        mine = os.getuid()
        found = stale.walk(diag)
        self.assertFalse(found["complete"])            # ours, unreadable: a doubt
        with mock.patch.object(stale.os, "getuid", return_value=mine + 1):
            found = stale.walk(diag)                   # another user's: left out
        self.assertTrue(found["complete"], found["why"])
        self.assertEqual(found["foreign"], [locked])
        copies, doubt = hk._git_copies_report(diag, skip=found["foreign"])
        self.assertEqual((copies, doubt), ([], ""))
        self.assertTrue(hk._git_copies_report(diag)[1])


if __name__ == "__main__":
    unittest.main()
