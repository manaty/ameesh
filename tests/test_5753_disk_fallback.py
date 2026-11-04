# SPDX-License-Identifier: AGPL-3.0-only
"""#5753 : secours disque sans faux rouge et sans appels répétés."""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import support  # isolate mesh environment
from ameesh import ci


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.watcher = ci.Watcher(SimpleNamespace(), clock=lambda: self.now)
        self.watcher._disk_refusal_messages = mock.Mock(return_value=[])
        self.row = dict(run_id=42, run_attempt=1)
        self.paths = []
        self.jobs = [dict(id=7, conclusion='success', check_run_url=
                         'https://api.github.com/repos/acme/widget/check-runs/70')]
        self.event = dict(schema='ameesh-ci-disk-refusal/1', host='ci-a',
                          run_id='42', attempt='1')
        self.watcher.api = self.api

    def api(self, path):
        self.paths.append(path)
        if path.endswith('/runs/42'):
            return {'head_repository': {'full_name': 'acme/widget'}}
        if '/jobs?' in path:
            return {'jobs': self.jobs}
        return [dict(title='ameesh-ci-disk-refusal/1', message=json.dumps(self.event))]

    def collect(self, **kwargs):
        with mock.patch('ameesh.ci.storage.of') as store, \
                mock.patch('ameesh.ci_disk.observe_refusal') as observe:
            store.return_value.ci.disks.return_value = [dict(repo='acme/widget', host='ci-a')]
            self.watcher._github_disk_refusals(object(), 'acme/widget', self.row, **kwargs)
            return observe, store

    def test_successful_job_still_collects_notice_without_test_failure(self):
        observe, store = self.collect(checks=[dict(id=70, output={'annotations_count': 1})])
        observe.assert_called_once()
        store.return_value.ci.add_failure.assert_not_called()
        self.assertEqual(len(self.paths), 2)

    def test_no_annotations_skips_annotation_api_and_amortizes_jobs(self):
        self.collect(checks=[dict(id=70, output={'annotations_count': 0})])
        self.collect(checks=[dict(id=70, output={'annotations_count': 0})])
        self.assertEqual(len(self.paths), 1)

    def test_unknown_check_output_keeps_enospc_fallback(self):
        observe, _ = self.collect(checks=[dict(id=70)])
        observe.assert_called_once()

    def test_running_scan_is_amortized_and_final_scan_not_delayed(self):
        self.collect()
        self.collect()
        self.assertEqual(len(self.paths), 2)
        self.collect(terminal='success')
        self.assertEqual(len(self.paths), 3)  # jobs refreshed; immutable job cached
        self.now += 301
        self.collect(terminal='success')
        self.assertEqual(len(self.paths), 5)

    def test_zero_annotation_hint_is_bound_to_check_id_not_job_id(self):
        self.collect(checks=[dict(id=70, output={'annotations_count': 0}), dict(id=80)])
        self.assertEqual(len(self.paths), 1)

    def test_failed_read_is_retried(self):
        self.watcher.api = mock.Mock(side_effect=OSError('quota'))
        self.collect()
        self.watcher.api = self.api
        observe, _ = self.collect()
        observe.assert_called_once()

    def test_unrelated_zero_checks_cannot_hide_notice_of_older_run(self):
        observe, _ = self.collect(checks=[dict(id=80, output={'annotations_count': 0})])
        observe.assert_called_once()

    def test_attempt_change_is_not_throttled_by_previous_attempt(self):
        self.collect()
        self.row['run_attempt'] = 2
        self.jobs[0]['id'] = 8
        self.jobs[0]['check_run_url'] = 'https://api.github.com/repos/acme/widget/check-runs/80'
        self.event['attempt'] = '2'
        observe, _ = self.collect()
        observe.assert_called_once()
        self.assertEqual(len(self.paths), 4)

    def test_map_success_uses_refusal_collector_even_outside_followed_suites(self):
        self.watcher.discovered_workflows = lambda _: ('tests.yml', 'carte.yml')
        self.watcher.api = mock.Mock(return_value={'workflow_runs': [dict(
            id=42, run_attempt=2, head_branch='main', head_sha='a' * 40)]})
        with mock.patch('ameesh.ci.storage.of') as store, \
                mock.patch.object(self.watcher, '_github_disk_refusals') as collect:
            store.return_value.ci.maps.return_value = []
            self.watcher._map(object(), 'acme/widget')
            self.assertEqual(collect.call_args.args[2], dict(run_id=42, run_attempt=2))
            self.assertEqual(collect.call_args.kwargs['terminal'], 'success')
            store.return_value.ci.add_failure.assert_not_called()

    def test_failed_annotations_cannot_grow_scan_cache_without_bound(self):
        self.watcher._disk_scans = {('repo', n, 1): (0, '') for n in range(520)}
        original = self.api
        self.watcher.api = lambda path: (_ for _ in ()).throw(OSError()) if '/jobs?' in path else original(path)
        self.collect()
        self.assertLessEqual(len(self.watcher._disk_scans), 512)

    def test_map_refusal_collection_has_a_five_run_budget(self):
        self.watcher.discovered_workflows = lambda _: ('carte.yml',)
        self.watcher.api = mock.Mock(return_value={'workflow_runs': [dict(
            id=n, head_branch='main') for n in range(1, 10)]})
        with mock.patch('ameesh.ci.storage.of') as store, \
                mock.patch.object(self.watcher, '_github_disk_refusals') as collect:
            store.return_value.ci.maps.return_value = []
            self.watcher._map(object(), 'acme/widget')
            self.assertEqual(collect.call_count, 5)
