# SPDX-License-Identifier: AGPL-3.0-only
"""The nm cache obeys the explicit host policy and records real deletions."""
import os
import tempfile
import time
from unittest import mock

from ameesh import deps, housekeeping as hk, storage
from ameesh.platform import dsh_scopes
from tests import test_l231_deps as fixtures
from tests.support import PgTestCase
from tests.test_l73_housekeeping import _Cfg


def _gpg_only(path, *, mapped=False, allow=()):
    if 'gpg-agent' not in allow:
        raise deps.platform.HoldersUnreadable(['123 gpg-agent'])
    return []


class CachePolicyTest(fixtures._Base):
    def orphan(self):
        pkg = fixtures._package(os.path.join(self.base, 'wt'))
        done = deps.link_package(pkg, self.cache, installer=fixtures.FakeInstaller())
        os.unlink(os.path.join(pkg, deps.NODE_MODULES))
        return deps.entry_of(deps.nm_root(self.cache), done['key'])

    def test_allowed_unreadable_daemon_no_longer_blocks_dry_run_or_deletion(self):
        entry = self.orphan()
        later = time.time() + 10 * 86400
        with mock.patch.object(deps.platform, 'holders', side_effect=_gpg_only):
            blocked = deps.gc_node_modules(self.cache, now=later, dry_run=True)
            self.assertEqual(blocked[0]['action'], 'kept')
            plan = deps.gc_node_modules(self.cache, now=later, dry_run=True,
                                       allow=('gpg-agent',))
            self.assertEqual(plan[0]['action'], 'would-delete')
            self.assertTrue(os.path.isdir(entry))
            result = deps.gc_node_modules(self.cache, now=later, allow=('gpg-agent',))
        self.assertEqual(result[0]['action'], 'deleted')
        self.assertFalse(os.path.exists(entry))

    def test_another_unreadable_process_still_blocks_with_an_allow_list(self):
        entry = self.orphan()
        for dry_run in (True, False):
            with mock.patch.object(deps.platform, 'holders', side_effect=
                                   deps.platform.HoldersUnreadable(['456 unknown-daemon'])):
                result = deps.gc_node_modules(self.cache, now=time.time() + 10 * 86400,
                                             dry_run=dry_run, allow=('gpg-agent',))
            self.assertEqual(result[0]['action'], 'kept')
            self.assertIn('unknown-daemon', result[0]['detail'])
            self.assertTrue(os.path.isdir(entry))

    def test_a_real_holder_is_not_excused_by_the_allow_list(self):
        entry = self.orphan()
        with mock.patch.object(deps.platform, 'holders', return_value=[
                {'pid': 123, 'name': 'gpg-agent', 'why': 'open file'}]):
            result = deps.gc_node_modules(self.cache, now=time.time() + 10 * 86400,
                                         allow=('gpg-agent',))
        self.assertEqual(result, [])
        self.assertTrue(os.path.isdir(entry))

    def test_the_policy_also_reaches_abandoned_temporary_fills(self):
        root = deps.nm_root(self.cache)
        entry = os.path.join(root, 'key' + deps.TMP_SEP + 'orphan')
        os.makedirs(entry)
        with mock.patch.object(deps.platform, 'holders', side_effect=_gpg_only):
            result = deps.gc_node_modules(self.cache, now=time.time() + 10 * 86400,
                                         allow=('gpg-agent',))
        self.assertEqual(result[0]['action'], 'deleted')
        self.assertFalse(os.path.exists(entry))


class CachePolicyJournalTest(PgTestCase):
    def test_host_policy_reaches_collection_and_the_deletion_is_journaled(self):
        cfg = _Cfg(self.state)
        policy = hk.Policy(worktrees=False, artifacts=(), docker_volumes=False,
                           docker_images=False, unreadable_ok=('gpg-agent',))
        cache = hk.cache_root(cfg, policy)
        hk._ensure_root(cache)
        self.addCleanup(deps._unseal, cache)
        pkg = fixtures._package(os.path.join(self.tmp, 'wt'))
        tool = {'node': 'v26', 'bindir': '', 'npm': None, 'npm_version': '11'}
        with mock.patch.object(deps, 'toolchain', return_value=tool), \
                mock.patch.object(deps, '_say'):
            done = deps.link_package(pkg, cache, installer=fixtures.FakeInstaller())
        entry = deps.entry_of(deps.nm_root(cache), done['key'])
        os.unlink(os.path.join(pkg, deps.NODE_MODULES))
        units = tempfile.mkdtemp(dir=self.tmp)
        with mock.patch.object(deps.platform, 'holders', side_effect=_gpg_only), \
                mock.patch.object(dsh_scopes, 'scan', return_value=[]), \
                mock.patch.dict(os.environ, AMEESH_SYSTEM_TMP=units):
            report = hk._run_pass(cfg, self.db, policy, host='policy-test', actor='test',
                                  artifact_scan=False, units_dir=units,
                                  now=time.time() + 10 * 86400)
        self.assertFalse(os.path.exists(entry))
        deleted = [line for line in report['entries'] if line['kind'] == 'deps']
        self.assertEqual([(line['path'], line['action']) for line in deleted],
                         [(entry, 'deleted')])
        rows = storage.of(self.db).housekeeping.latest_by_path('policy-test', 'deps')
        self.assertEqual([(row['path'], row['action']) for row in rows], [(entry, 'deleted')])
