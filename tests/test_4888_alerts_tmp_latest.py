# SPDX-License-Identifier: AGPL-3.0-only
"""#4888 — la passe d'alertes ne trie plus tout le journal `tmp`.

Constat du 30/10 sur agents-1 : `ameesh alerts` a dépassé 60 s puis 100 s
pour un bail de 60 s. Mesuré sur la base commune : deux lectures
`latest_by_path(None, "tmp")` de `_housekeeping_alerts` pesaient 12,5 s et
9,2 s. `housekeeping_log` (584 k lignes, 270 Mo, dont 508 k `tmp`) était
seq-scanné, puis merge-joint sur toute la table ; la fenêtre 24 h ne réduisait
rien (filtre non indexé) et la variante `unowned` (7 lignes) scannait aussi
tout.

Témoins :
* l'index `(kind, action, host, path, at desc)` est migré ;
* la lecture des chemins candidats passe par cet index, pas par un Seq Scan ;
* la dernière ligne de chaque chemin candidat fait foi (une suppression clôt
  l'épisode) et la fenêtre 24 h s'applique ;
* `_housekeeping_alerts` ne demande que les actions qui produisent une alerte.
"""
from __future__ import annotations

import json
from unittest import mock

from ameesh import db as db_mod, exploitation, storage
from ameesh.storage.postgres import menage as menage_pg

from .support import PgTestCase

INDEX = "housekeeping_log_kind_action_path_time_idx"


class TmpLatestIndexTest(PgTestCase):

    def test_the_index_is_migrated(self):
        rows = self.db.query(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s"
            " AND tablename = 'housekeeping_log' AND indexname = %s",
            (self.schema, INDEX))
        self.assertEqual(len(rows), 1, rows)
        definition = rows[0]["indexdef"].lower()
        self.assertIn("(kind, action, host, path, at desc)", definition)

    def _plan(self, sql, params=()):
        # Le pilote psql enveloppe les lectures dans un agrégat JSON où
        # `EXPLAIN` ne peut pas entrer : le plan se lit sous psycopg (pilote
        # par défaut de la CI), le prédicat est vérifié sous les deux.
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest("EXPLAIN is wrapped by the psql driver")
        return json.dumps(self.db.query("EXPLAIN (FORMAT JSON) " + sql, params))

    def _fill(self):
        """20 000 lignes `tmp` sans intérêt, trois chemins qui comptent."""
        self.db.execute("DELETE FROM housekeeping_log")
        self.db.execute(
            "INSERT INTO housekeeping_log (host, at, actor, kind, action, path, bytes)"
            " SELECT 'h1', now() - (g || ' seconds')::interval, 'test', 'tmp',"
            " 'signaled', '/tmp/p' || (g % 2000), g"
            " FROM generate_series(1, 20000) g")
        # Un chemin encore en défaut : l'alerte doit rester.
        self.db.execute(
            "INSERT INTO housekeeping_log (host, at, actor, kind, action, path,"
            " bytes, agent) VALUES ('h1', now() - interval '1 hour', 'test',"
            " 'tmp', 'unremovable', '/tmp/keep', 12, 'a1')")
        # Un chemin en défaut puis `deleted` : la dernière ligne clôt l'épisode.
        self.db.execute(
            "INSERT INTO housekeeping_log (host, at, actor, kind, action, path,"
            " bytes, agent) VALUES ('h1', now() - interval '3 hours', 'test',"
            " 'tmp', 'unremovable', '/tmp/gone', 34, 'a2')")
        self.db.execute(
            "INSERT INTO housekeeping_log (host, at, actor, kind, action, path,"
            " bytes, agent) VALUES ('h1', now() - interval '2 hours', 'test',"
            " 'tmp', 'deleted', '/tmp/gone', 0, 'a2')")
        # L'`unowned` a sa propre lecture, sans fenêtre (#2851).
        self.db.execute(
            "INSERT INTO housekeeping_log (host, at, actor, kind, action, path,"
            " bytes, agent) VALUES ('h1', now() - interval '1 hour', 'test',"
            " 'tmp', 'unowned', '/tmp/unowned', 7, 'a3')")
        # Une action d'alerte vieille de 25 h : hors de la fenêtre de 24 h.
        self.db.execute(
            "INSERT INTO housekeeping_log (host, at, actor, kind, action, path,"
            " bytes, agent) VALUES ('h1', now() - interval '25 hours', 'test',"
            " 'tmp', 'over_quota', '/tmp/old', 99, 'a4')")
        self.db.execute("ANALYZE housekeeping_log")

    def test_the_candidate_read_uses_the_index_and_not_a_scan(self):
        self._fill()
        sql, params = menage_pg._latest_by_path_sql(
            "tmp", since_s=86400.0, actions=list(exploitation.TMP_ALERT_ACTIONS))
        plan = self._plan(sql, params)
        self.assertIn(INDEX, plan)
        self.assertNotIn("Seq Scan", plan)

    def test_the_latest_line_of_each_candidate_path_wins(self):
        self._fill()
        rows = storage.of(self.db).housekeeping.latest_by_path(
            None, "tmp", since_s=86400.0,
            only_paths_with=exploitation.TMP_ALERT_ACTIONS)
        by_path = {row["path"]: row for row in rows}
        self.assertEqual(set(by_path), {"/tmp/keep", "/tmp/gone"})
        self.assertEqual(by_path["/tmp/keep"]["action"], "unremovable")
        self.assertEqual(by_path["/tmp/gone"]["action"], "deleted")
        # `unowned` n'est pas une action d'alerte de cette lecture : elle a sa
        # propre passe, sans fenêtre, et la dernière ligne du chemin y fait foi.
        unowned = storage.of(self.db).housekeeping.latest_by_path(
            None, "tmp", only_paths_with=("unowned",))
        self.assertEqual({row["path"] for row in unowned}, {"/tmp/unowned"})
        self.assertEqual(unowned[0]["action"], "unowned")

    def test_the_window_excludes_an_old_alert_action(self):
        self._fill()
        rows = storage.of(self.db).housekeeping.latest_by_path(
            None, "tmp", since_s=86400.0, only_paths_with=("over_quota",))
        self.assertEqual(rows, [])


class _RecordingHousekeeping:
    def __init__(self):
        self.calls = []

    def last_measure(self, host):
        return []

    def worktrees(self, host, statuses, limit=None):
        return []

    def latest_by_path(self, host, kind, **kwargs):
        self.calls.append((host, kind, kwargs))
        return []


class HousekeepingAlertReadTest(PgTestCase):
    """La famille d'alertes ne demande que les actions qui alertent (#4888)."""

    def test_the_tmp_read_is_bounded_to_the_alert_actions(self):
        fake = _RecordingHousekeeping()

        class _Storage:
            housekeeping = fake

        with mock.patch.object(exploitation.storage, "of", return_value=_Storage()):
            exploitation._housekeeping_alerts([], object(), readings=[])
        tmp = [call for call in fake.calls if call[1] == "tmp"]
        bounded = [call for call in tmp if call[2].get("since_s") is not None]
        self.assertEqual(len(bounded), 1, fake.calls)
        _host, _kind, kwargs = bounded[0]
        self.assertEqual(kwargs.get("since_s"), 86400.0)
        self.assertEqual(kwargs.get("only_paths_with"),
                         exploitation.TMP_ALERT_ACTIONS)
        # `unowned` garde sa lecture à part, sans fenêtre (#2851).
        unowned = [call for call in tmp
                   if call[2].get("only_paths_with") == ("unowned",)]
        self.assertEqual(len(unowned), 1, fake.calls)
