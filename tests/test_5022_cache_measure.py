# SPDX-License-Identifier: AGPL-3.0-only
"""Cache totals must expose incompleteness and retained entries survive a pass."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
from unittest import mock

from ameesh import deps, housekeeping as hk, storage
from tests.support import PgTestCase
from tests import test_l231_deps as fixtures
from tests.test_l73_housekeeping import _Cfg


class CacheMeasureTest(fixtures._Base):
    def test_shared_installs_are_counted_past_the_file_limit_and_show_over_quota(self):
        cfg = _Cfg(os.path.join(self.base, 'state'))
        policy = hk.Policy(cache_quota=8192)
        root = Path(hk.cache_root(cfg, policy)) / deps.NM_DIR
        root.mkdir(parents=True)
        for i in range(5):
            (root / str(i)).write_bytes(b'x' * 8192)
        with mock.patch.object(hk, 'WALK_LIMIT', 2):
            measured = hk.measure(cfg, policy)
        self.assertGreaterEqual(measured['cache_by_name']['nm'], 5 * 8192)
        self.assertEqual(measured['cache_partial'], [])
        self.assertTrue(measured['cache_quota_exceeded'])

    def test_budget_cutoff_is_a_lower_bound_and_never_claims_quota_compliance(self):
        cfg = _Cfg(os.path.join(self.base, 'state'))
        policy = hk.Policy()
        root = Path(hk.cache_root(cfg, policy)) / deps.NM_DIR
        root.mkdir(parents=True)
        (root / 'payload').write_bytes(b'x' * 8192)
        budget = mock.Mock()
        budget.expired.side_effect = [False, True, True]
        measured = hk.measure(cfg, policy, budget=budget)
        self.assertEqual(measured['cache_partial'], ['nm'])
        self.assertIsNone(measured['cache_quota_exceeded'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            hk.print_report({'host': 'test', 'dry_run': True, 'plan': [],
                             'skipped': [], 'measure': measured})
        self.assertIn('at least', output.getvalue())
        self.assertIn('quota compliance unknown', output.getvalue())

    def test_size_reports_truncation_without_retaining_every_filename(self):
        root = Path(self.base) / 'files'
        root.mkdir()
        for i in range(3):
            (root / str(i)).write_bytes(b'x' * 8192)
        state = {}
        measured = hk.size_of(str(root), limit=2, state=state)
        self.assertGreaterEqual(measured, 8192)
        self.assertTrue(state['truncated'])
        self.assertNotIn('links', state)


class CacheRetentionJournalTest(PgTestCase):
    def test_kept_deps_are_logged_and_included_in_the_measure(self):
        cfg = _Cfg(self.state)
        policy = hk.Policy(worktrees=False, artifacts=())
        hk._ensure_root(hk.cache_root(cfg, policy))
        kept = {'kind': 'deps', 'action': 'kept',
                'path': os.path.join(hk.cache_root(cfg, policy), 'nm', 'entry'),
                'bytes': 8192, 'detail': 'file holders unreadable: test process'}
        units = tempfile.mkdtemp(dir=self.tmp)
        with mock.patch.object(deps, 'gc_node_modules', return_value=[kept]), \
                mock.patch.dict(os.environ, AMEESH_SYSTEM_TMP=units):
            report = hk._run_pass(cfg, self.db, policy, host='test', actor='test',
                                  artifact_scan=False, units_dir=units)
        self.assertIn(kept, report['entries'])
        self.assertEqual(report['measure']['deps_kept'], [kept])
        rows = self.db.query("SELECT action, detail FROM housekeeping_log "
                             "WHERE host = 'test' AND kind = 'deps'")
        self.assertTrue(any(row['action'] == 'kept' and row['detail'] == kept['detail']
                            for row in rows))
        latest = storage.of(self.db).housekeeping.last_measure('test')
        self.assertEqual(latest[0]['data']['deps_kept'], [kept])
