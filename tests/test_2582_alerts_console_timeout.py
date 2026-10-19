# SPDX-License-Identifier: AGPL-3.0-only
"""#2582 : une console PgBouncer muette ne bloque plus `ameesh alerts`.

La sonde de la console locale (#532/#1179) lisait `SHOW POOLS`, `SHOW LISTS`
et `SHOW CONFIG` sur la socket Unix sans délai total : `connect_timeout` est
ignoré par libpq sur une socket Unix, et la console n'a pas de
`statement_timeout`. Une console qui accepte la connexion puis ne répond plus
figeait donc `ameesh alerts` après toutes ses autres familles — et SIGINT avec
elle, puisque l'attente est dans libpq.

Le témoin : une sonde qui ne répond pas rend la main dans le délai, les autres
familles restent lisibles, et celle de la console est marquée illisible
(`pgbouncer_console_unreadable`, `unreadable`, #1179/#1901).
"""
from __future__ import annotations

import threading
import time
import types
import unittest
from unittest import mock

from ameesh import db as db_mod, exploitation, pgbouncer_local, work

from .support import PgTestCase


def _cfg():
    return types.SimpleNamespace(host="pc-smichea")


class BoundedProbe(unittest.TestCase):
    """`probe` ne dépend pas de la base : la console est lue depuis `cfg`."""

    def setUp(self):
        self.release = threading.Event()
        #: isole les témoins : une sonde laissée en vol par un cas ne doit pas
        #: être reprise par le suivant (le singleton est le comportement testé)
        pgbouncer_local._PROBE = None
        self.addCleanup(self.settle)

    def settle(self):
        self.release.set()
        probe = pgbouncer_local._PROBE
        if probe is not None:
            probe.thread.join(1.0)
        pgbouncer_local._PROBE = None

    def blocked(self, calls: list | None = None):
        def read(dsn):
            if calls is not None:
                calls.append(dsn)
            self.release.wait(5)
            return {}
        return read

    def test_a_console_that_never_answers_is_bounded(self):
        started = time.monotonic()
        with mock.patch.object(pgbouncer_local, "admin_dsn", return_value="admin"), \
                mock.patch.object(pgbouncer_local, "read", side_effect=self.blocked()), \
                mock.patch.object(pgbouncer_local, "PROBE_TIMEOUT_S", 0.05):
            with self.assertRaises(db_mod.Unavailable) as caught:
                pgbouncer_local.probe(_cfg())
            elapsed = time.monotonic() - started
        self.assertIn("no answer within 0.05s", str(caught.exception))
        self.assertLess(elapsed, 1.0, "the probe must not wait for the console")

    def test_the_timeout_marks_the_family_unreadable_and_keeps_the_line(self):
        unreadable: list = []
        with mock.patch.object(pgbouncer_local, "admin_dsn", return_value="admin"), \
                mock.patch.object(pgbouncer_local, "read", side_effect=self.blocked()), \
                mock.patch.object(pgbouncer_local, "PROBE_TIMEOUT_S", 0.05):
            lines = exploitation._local_pool_alerts(
                types.SimpleNamespace(cfg=_cfg()), unreadable=unreadable)
        self.assertEqual([a["type"] for a in lines], ["pgbouncer_console_unreadable"])
        self.assertIn("no answer within", lines[0]["detail"])
        self.assertEqual(unreadable, [pgbouncer_local.FAMILY])

    def test_a_stuck_probe_is_not_started_again_at_each_pass(self):
        calls: list = []
        with mock.patch.object(pgbouncer_local, "admin_dsn", return_value="admin"), \
                mock.patch.object(pgbouncer_local, "read",
                                  side_effect=self.blocked(calls)), \
                mock.patch.object(pgbouncer_local, "PROBE_TIMEOUT_S", 0.05):
            for _ in range(3):
                with self.assertRaises(db_mod.Unavailable):
                    pgbouncer_local.probe(_cfg())
        self.assertEqual(len(calls), 1,
                         "one stuck read, not one thread per pass")


class SnapshotStaysReadable(PgTestCase):
    """Une famille en panne ne doit pas emporter tout le relevé."""

    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        pgbouncer_local._PROBE = None
        self.addCleanup(self.settle)
        self.seuils = exploitation.thresholds(
            exploitation.build_parser().parse_args(["alerts"]))

    def settle(self):
        self.release.set()
        probe = pgbouncer_local._PROBE
        if probe is not None:
            probe.thread.join(1.0)
        pgbouncer_local._PROBE = None

    def stale_lot(self):
        self.register("ouvrier", "claude")
        lot = work.add(self.db, title="lot sans suite", assignee="ouvrier")
        for sql in ("UPDATE work_items SET created_at = now() - interval '8 hours',"
                    " updated_at = now() - interval '7 hours' WHERE id = %s",
                    "UPDATE work_item_events SET created_at = now() - interval '7 hours'"
                    " WHERE work_item_id = %s",
                    "UPDATE work_item_milestones SET at = now() - interval '7 hours'"
                    " WHERE work_item_id = %s"):
            self.db.execute(sql, (lot["id"],))
        return lot

    def test_a_hanging_console_does_not_blind_the_other_families(self):
        lot = self.stale_lot()

        def read(dsn):
            self.release.wait(5)
            return {}

        started = time.monotonic()
        with mock.patch.object(pgbouncer_local, "admin_dsn", return_value="admin"), \
                mock.patch.object(pgbouncer_local, "read", side_effect=read), \
                mock.patch.object(pgbouncer_local, "PROBE_TIMEOUT_S", 0.05):
            alerts = exploitation.alerts(self.cfg, self.db, local_pool=True,
                                         **self.seuils)
            elapsed = time.monotonic() - started
        self.assertFalse(self.release.is_set(), "the snapshot waited for the console")
        self.assertLess(elapsed, 2.0)
        types = {a["type"]: a for a in alerts}
        self.assertIn("stale_lot", types, "the other families stay readable")
        self.assertEqual(types["stale_lot"]["lot"], lot["id"])
        self.assertIn("pgbouncer_console_unreadable", types,
                      "the timed-out family is marked unreadable")
        self.assertIn("no answer within", types["pgbouncer_console_unreadable"]["detail"])

    def test_the_batched_snapshot_reads_the_console_once(self):
        """Les passes à blanc de `db.batched` ne sondent pas la console."""
        calls: list = []

        def read(dsn):
            calls.append(dsn)
            return dict(waiting=0, maxwait_s=0, clients_active=0, servers_active=0,
                        servers_idle=0, clients_used=None, clients_max=None)

        with mock.patch.object(pgbouncer_local, "admin_dsn", return_value="admin"), \
                mock.patch.object(pgbouncer_local, "read", side_effect=read):
            db_mod.batched(self.db, lambda db: exploitation.alerts(
                self.cfg, db, local_pool=True, **self.seuils))
        self.assertEqual(len(calls), 1, "one real read; dry passes send no I/O")


if __name__ == "__main__":
    unittest.main()
