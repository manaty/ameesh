# SPDX-License-Identifier: AGPL-3.0-only
"""Stockage réel : une ancienne annotation ne remplace pas un relevé plus récent."""
from .support import PgTestCase
from ameesh import storage, ci_disk, exploitation


class DiskStorageTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('TRUNCATE ci_disk_state')
        self.ci = storage.of(self.db).ci

    def record(self, sample, free, host='ci-a'):
        self.ci.observe_disk('acme/widget', host=host, volume='/', run_id=42,
                             sampled_ts=sample, free_bytes=free, total_bytes=100 * 1024**3)

    def test_older_and_repeated_samples_do_not_erase_newer_low(self):
        self.record(200, 1024**3)
        self.record(100, 80 * 1024**3)
        self.record(200, 80 * 1024**3)
        row, = self.ci.disks()
        self.assertEqual(row['free_bytes'], 1024**3)
        self.assertEqual(row['sampled_ts'], 200)
        self.record(300, 80 * 1024**3)
        self.assertEqual(ci_disk.findings(self.ci.disks(), [], 300), [])

    def test_no_sample_is_unknown_not_zero_and_hosts_are_independent(self):
        self.ci.expect_disk('acme/widget', host='ci-b', volume='/')
        self.record(200, 1024**3)
        self.ci.expect_disk('acme/widget', host='ci-a', volume='/')
        alerts = ci_disk.findings(self.ci.disks(), [], 200)
        self.assertEqual({(a['host'], a['type']) for a in alerts},
                         {('ci-a', 'ci_disk_low'), ('ci-b', 'ci_disk_unreadable')})
        self.assertEqual(len({exploitation.alert_key(a) for a in alerts}), 2)

    def test_prune_removed_repository_but_not_empty_perimeter(self):
        self.record(200, 1024**3)
        self.ci.prune_disks([])
        self.assertEqual(len(self.ci.disks()), 1)
        self.ci.prune_disks(['other/project'])
        self.assertEqual(self.ci.disks(), [])

    def test_mesh_alert_pipeline_raises_and_resolves_on_new_measurement(self):
        self.record(200, 1024**3)
        alerts = exploitation._ci_disks(self.cfg, self.db, 200)
        self.assertEqual([a['type'] for a in alerts], ['ci_disk_low'])
        self.assertEqual(alerts[0]['schema'], exploitation.ALERT_SCHEMA)
        self.record(300, 80 * 1024**3)
        self.assertEqual(exploitation._ci_disks(self.cfg, self.db, 300), [])
