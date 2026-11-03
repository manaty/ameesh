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

    def test_no_sample_has_a_grace_then_is_unknown_not_zero(self):
        import time
        now = time.time()
        self.ci.expect_disk('acme/widget', host='ci-b', volume='/')
        rows = self.ci.disks()
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0]['first_seen_ts'], now - 5)
        self.assertGreaterEqual(rows[0]['seen_ts'], now - 5)
        self.assertEqual(ci_disk.findings(rows, [], now), [])
        alerts = ci_disk.findings(rows, [], now + ci_disk.MAX_AGE + 1)
        self.assertEqual([(a['host'], a['type']) for a in alerts],
                         [('ci-b', 'ci_disk_unreadable')])
        self.assertEqual(len({exploitation.alert_key(a) for a in alerts}), 1)

    def test_repeated_collects_do_not_renew_the_first_measure_grace(self):
        """B1 #5633 : la grâce ne se renouvelle pas au rythme des collectes."""
        import time
        now = time.time()
        self.ci.expect_disk('acme/widget', host='ci-b', volume='/')
        self.db.execute("UPDATE ci_disk_state "
                        "SET first_seen_at = now() - interval '3 hours'")
        premier = self.ci.disks()[0]['first_seen_ts']
        # Deux collectes de plus, sans mesure : chaque job vu repousse seen_at,
        # jamais first_seen_at.
        self.ci.expect_disk('acme/widget', host='ci-b', volume='/')
        self.ci.expect_disk('acme/widget', host='ci-b', volume='/')
        row = self.ci.disks()[0]
        self.assertEqual(row['first_seen_ts'], premier)
        self.assertGreater(row['seen_ts'], premier + 2 * 3600)
        self.assertEqual([a['type'] for a in ci_disk.findings([row], [], now)],
                         ['ci_disk_unreadable'])
        # La VM est toujours présente : elle n'est pas oubliée.
        self.ci.forget_disks('acme/widget', ['ci-b'], older_than_s=0)
        self.assertEqual(len(self.ci.disks()), 1)

    def test_expect_refreshes_seen_at_without_touching_the_measure(self):
        self.record(200, 1024**3)
        self.db.execute("UPDATE ci_disk_state SET seen_at = now() - interval '3 days'")
        avant = self.ci.disks()[0]
        self.ci.expect_disk('acme/widget', host='ci-a', volume='/')
        apres = self.ci.disks()[0]
        self.assertGreater(apres['seen_ts'], avant['seen_ts'] + 86400)
        self.assertEqual(apres['first_seen_ts'], avant['first_seen_ts'])
        self.assertEqual(apres['sampled_ts'], 200)
        self.assertEqual(apres['free_bytes'], 1024**3)

    def test_forget_keeps_hosts_still_jobs_or_seen_recently(self):
        self.record(200, 1024**3, host='ci-a')
        self.record(200, 1024**3, host='ci-b')
        self.ci.expect_disk('acme/widget', host='ci-c', volume='/')
        self.ci.expect_disk('other/project', host='ci-a', volume='/')
        self.db.execute("UPDATE ci_disk_state SET seen_at = now() - interval '25 hours' "
                        "WHERE repo = 'acme/widget' AND host IN ('ci-a', 'ci-b')")
        self.ci.forget_disks('acme/widget', ['ci-b'], older_than_s=24 * 3600)
        self.assertEqual(sorted((r['repo'], r['host']) for r in self.ci.disks()),
                         [('acme/widget', 'ci-b'), ('acme/widget', 'ci-c'),
                          ('other/project', 'ci-a')])
        # Un relevé vide (lecture ratée) n'oublie rien, même à seuil nul.
        self.ci.forget_disks('acme/widget', [], older_than_s=0)
        self.assertEqual(len(self.ci.disks()), 3)

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
