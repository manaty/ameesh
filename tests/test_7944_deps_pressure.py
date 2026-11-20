# SPDX-License-Identifier: AGPL-3.0-only
"""#7944: under disk pressure or over the cache quota, an nm entry nobody
links to goes after PRESSURE_GRACE_S, not after a whole day."""
import os
import time

from ameesh import deps
from . import test_l231_deps as fixtures


class PressureGraceTests(fixtures._Base):
    def orphan(self):
        pkg = fixtures._package(os.path.join(self.base, 'wt'))
        done = deps.link_package(pkg, self.cache, installer=fixtures.FakeInstaller())
        os.unlink(os.path.join(pkg, deps.NODE_MODULES))
        return pkg, deps.entry_of(deps.nm_root(self.cache), done['key'])

    def gc(self, pkg, **kw):
        return deps.gc_node_modules(self.cache, now=time.time() + 2 * 3600,
                                    worktrees=[os.path.dirname(pkg)],
                                    holders=lambda _: [], **kw)

    def test_kept_a_day_without_pressure(self):
        pkg, entry = self.orphan()
        self.assertEqual(self.gc(pkg, quota=1 << 40), [])
        self.assertTrue(os.path.isdir(entry))

    def test_disk_pressure_shortens_the_grace(self):
        pkg, entry = self.orphan()
        result = self.gc(pkg, pressure=True)
        self.assertEqual([x['action'] for x in result], ['deleted'])
        self.assertIn('pressure', result[0]['detail'])
        self.assertFalse(os.path.exists(entry))

    def test_over_quota_shortens_the_grace(self):
        pkg, entry = self.orphan()
        self.assertEqual([x['action'] for x in self.gc(pkg, quota=0, dry_run=True)],
                         ['would-delete'])
        self.assertEqual([x['action'] for x in self.gc(pkg, quota=0)], ['deleted'])
        self.assertFalse(os.path.exists(entry))

    def test_pressure_still_waits_the_short_grace(self):
        pkg, entry = self.orphan()
        result = deps.gc_node_modules(self.cache, now=time.time() + 600, pressure=True,
                                      worktrees=[os.path.dirname(pkg)],
                                      holders=lambda _: [])
        self.assertEqual(result, [])
        self.assertTrue(os.path.isdir(entry))

    def test_pressure_never_deletes_a_linked_entry(self):
        pkg = fixtures._package(os.path.join(self.base, 'wt'))
        done = deps.link_package(pkg, self.cache, installer=fixtures.FakeInstaller())
        entry = deps.entry_of(deps.nm_root(self.cache), done['key'])
        self.assertEqual(self.gc(pkg, pressure=True, quota=0), [])
        self.assertTrue(os.path.isdir(entry))

    def test_pressure_never_deletes_a_held_entry(self):
        pkg, entry = self.orphan()
        result = deps.gc_node_modules(self.cache, now=time.time() + 2 * 3600,
                                      pressure=True, worktrees=[os.path.dirname(pkg)],
                                      holders=lambda _: ['42 node'])
        self.assertNotIn('deleted', [x['action'] for x in result])
        self.assertTrue(os.path.isdir(entry))
