# SPDX-License-Identifier: AGPL-3.0-only
"""Shared cache accounting, safe collection and visible retention reasons."""
import os
import time
from unittest import mock

from ameesh import deps, housekeeping as hk
from . import test_l231_deps as fixtures


class SharedCacheTests(fixtures._Base):
    def test_measure_includes_nm_when_linking_disabled(self):
        cfg = fixtures._Cfg(os.path.join(self.base, 'state'))
        policy = hk.Policy(deps=False)
        root = hk.cache_root(cfg, policy)
        path = os.path.join(root, deps.NM_DIR, 'entry')
        os.makedirs(path)
        with open(os.path.join(path, 'payload'), 'w') as f:
            f.write('x' * 8192)
        report = hk.measure(cfg, policy)
        self.assertGreaterEqual(report['cache_by_name']['nm'], 8192)
        self.assertEqual(report['cache_bytes'], sum(report['cache_by_name'].values()))

    def test_stale_link_records_do_not_prevent_dry_run_or_apply(self):
        pkg = fixtures._package(os.path.join(self.base, 'wt'))
        done = deps.link_package(pkg, self.cache, installer=fixtures.FakeInstaller())
        entry = deps.entry_of(deps.nm_root(self.cache), done['key'])
        os.unlink(os.path.join(pkg, deps.NODE_MODULES))
        later = time.time() + 10 * 86400
        plan = deps.gc_node_modules(self.cache, now=later, dry_run=True,
                                   worktrees=[os.path.dirname(pkg)], holders=lambda _: [])
        self.assertEqual([x['action'] for x in plan], ['would-delete'])
        self.assertTrue(os.path.isdir(entry))
        result = deps.gc_node_modules(self.cache, now=later,
                                     worktrees=[os.path.dirname(pkg)], holders=lambda _: [])
        self.assertEqual([x['action'] for x in result], ['deleted'])
        self.assertFalse(os.path.exists(entry))

    def test_unreadable_holders_reported_without_deletion(self):
        pkg = fixtures._package(os.path.join(self.base, 'wt'))
        done = deps.link_package(pkg, self.cache, installer=fixtures.FakeInstaller())
        entry = deps.entry_of(deps.nm_root(self.cache), done['key'])
        os.unlink(os.path.join(pkg, deps.NODE_MODULES))
        for dry_run in (True, False):
            with mock.patch.object(deps, '_holders', side_effect=deps.platform.NotAvailable(
                    'file holders', 'test process unreadable')):
                lines = deps.gc_node_modules(self.cache, now=time.time() + 10 * 86400,
                                             dry_run=dry_run)
            self.assertEqual(lines[0]['action'], 'kept')
            self.assertIn('test process unreadable', lines[0]['detail'])
            self.assertTrue(os.path.isdir(entry))

    def test_broken_link_reported_without_cache_entries_or_lockfile(self):
        top = os.path.join(self.base, 'wt')
        os.makedirs(top)
        link = os.path.join(top, deps.NODE_MODULES)
        os.symlink(os.path.join(self.base, 'gone', deps.NODE_MODULES), link)
        for dry_run in (True, False):
            lines = deps.gc_node_modules(self.cache, worktrees=[top], dry_run=dry_run)
            self.assertEqual(lines[0]['path'], link)
            self.assertIn('broken node_modules link', lines[0]['detail'])
            self.assertTrue(os.path.islink(link))
