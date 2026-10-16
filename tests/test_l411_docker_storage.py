# SPDX-License-Identifier: AGPL-3.0-only
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ameesh import containers, docker_storage as D, exploitation, menage


class DockerStorageTest(unittest.TestCase):
    def runtime(self, volumes=(), cache=(), cache_rc=0, buildx_rc=0):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[1:3] == ['buildx', 'version']:
                return SimpleNamespace(returncode=buildx_rc, stdout='', stderr='')
            rows = volumes if argv[1] == 'volume' else cache
            return SimpleNamespace(returncode=cache_rc if argv[1] == 'buildx' else 0,
                                   stdout='\n'.join(json.dumps(r) for r in rows))
        return containers.Runtime('docker', timeout=4, run=run), calls

    def test_read_only_counts_and_private_reclaimable_bytes(self):
        runtime, calls = self.runtime(
            [{'Name': 'anonymous', 'Labels': 'com.docker.volume.anonymous='},
             {'Name': 'project-data', 'Labels': 'com.docker.compose.project=prod'}],
            [{'Size': '2GB', 'Reclaimable': True, 'Shared': False},
             {'Size': '3000000000', 'Reclaimable': True, 'Shared': True},
             {'Size': '4GB', 'Reclaimable': False, 'Shared': False},
             {'Size': '0B', 'Reclaimable': True, 'Shared': False}])
        data = D.collect(runtime, now=1000)
        self.assertEqual(data['orphan_volumes'], 2)
        self.assertEqual(data['anonymous_orphan_volumes'], 1)
        self.assertEqual(data['named_orphan_volumes'], 1)
        self.assertEqual(data['build_cache_reclaimable_bytes'], 2_000_000_000)
        self.assertEqual(data['build_cache_bytes'], 9_000_000_000)
        self.assertEqual(data['errors'], [])
        self.assertEqual([c[0][1:3] for c in calls], [['volume', 'ls'], ['buildx', 'du']])
        self.assertIn('dangling=true', calls[0][0])
        self.assertIn('default', calls[1][0])
        self.assertTrue(all(c[1]['timeout'] == 4 for c in calls))
        self.assertNotIn('project-data', json.dumps(data))

    def test_failure_is_unknown_and_independent_of_other_measurement(self):
        runtime, _ = self.runtime(cache=[])
        original = runtime.run
        runtime.run = lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 4)) \
            if argv[1] == 'volume' else original(argv, **kw)
        data = D.collect(runtime, now=1000)
        self.assertIsNone(data['orphan_volumes'])
        self.assertEqual(data['build_cache_bytes'], 0)
        self.assertTrue(data['errors'])
        alerts = D.findings(data, menage.Policy(), 'pc', 1000)
        self.assertEqual([a['type'] for a in alerts], ['docker_storage_unavailable'])

    def test_missing_buildx_is_unsupported_not_a_permanent_error(self):
        runtime, calls = self.runtime([{'Name': 'v', 'Labels': ''}],
                                      cache_rc=1, buildx_rc=1)
        data = D.collect(runtime, now=1000)
        self.assertIsNone(data['build_cache_bytes'])
        self.assertEqual(data['errors'], [])
        self.assertTrue(data['build_cache_unsupported'])
        self.assertEqual(D.findings(data, menage.Policy(), 'pc', 1000), [])
        self.assertEqual([c[0][1:3] for c in calls],
                         [['volume', 'ls'], ['buildx', 'du'], ['buildx', 'version']])

    def test_cache_failure_with_buildx_present_stays_an_error(self):
        runtime, _ = self.runtime(cache_rc=1, buildx_rc=0)
        data = D.collect(runtime, now=1000)
        self.assertFalse(data['build_cache_unsupported'])
        self.assertEqual(data['errors'], ['default builder cache inventory unavailable'])
        self.assertEqual([a['type'] for a in D.findings(data, menage.Policy(), 'pc', 1000)],
                         ['docker_storage_unavailable'])

    def test_malformed_cache_never_becomes_zero(self):
        for row in ({'Size': '-1', 'Reclaimable': True, 'Shared': False},
                    {'Size': '10GB', 'Reclaimable': 'true', 'Shared': False},
                    {'Size': True, 'Reclaimable': True, 'Shared': False}, {}):
            runtime, _ = self.runtime(cache=[row])
            data = D.collect(runtime)
            self.assertIsNone(data['build_cache_bytes'])
            self.assertEqual(data['orphan_volumes'], 0)

    def test_alert_thresholds_and_stale_sample(self):
        data = {'sampled_ts': 1000, 'orphan_volumes': 100,
                'build_cache_reclaimable_bytes': 20 * 1024**3}
        found = D.findings(data, menage.Policy(), 'pc', 1000)
        self.assertEqual([a['type'] for a in found], ['docker_orphan_volumes', 'docker_build_cache'])
        self.assertEqual(D.findings(dict(data, orphan_volumes=99,
                                        build_cache_reclaimable_bytes=1), menage.Policy(), 'pc', 1000), [])
        self.assertEqual([a['type'] for a in D.findings(data, menage.Policy(), 'pc', 5000)],
                         ['docker_storage_unavailable'])

    def test_alerts_read_persisted_sample_without_docker(self):
        hk = SimpleNamespace(
            last_measure=lambda _: [{'host': 'pc', 'data': {'docker_storage': {
                'sampled_ts': 1000, 'orphan_volumes': 101}}}],
            worktrees=lambda *args: [], latest_by_path=lambda *args: [])
        st = SimpleNamespace(housekeeping=hk, hosts=SimpleNamespace(current=lambda _: []))
        with patch.object(exploitation.storage, 'of', return_value=st), \
             patch.object(exploitation.time, 'time', return_value=1000):
            found = exploitation._housekeeping_alerts([], object())
        self.assertEqual(found[0]['type'], 'docker_orphan_volumes')
        self.assertEqual(found[0]['host'], 'pc')
        self.assertEqual(found[0]['value'], 101)
        self.assertIn(found[0]['type'], exploitation.ALERT_TYPES)

    def test_policy_values_and_validation(self):
        parsed, problems, unknown = menage.parse_policy({
            'docker_orphan_volumes_alert': 50, 'docker_build_cache_alert': '10GiB'})
        self.assertEqual(parsed['docker_build_cache_alert'], 10 * 1024**3)
        self.assertEqual(problems + unknown, [])
        for bad in (False, 0, -1, '100'):
            self.assertTrue(menage.parse_policy({'docker_orphan_volumes_alert': bad})[1])

    def test_unsupported_runtime_never_invokes_docker(self):
        runtime, calls = self.runtime()
        runtime.binary = 'podman'
        self.assertIsNone(D.collect(runtime))
        self.assertEqual(calls, [])
        self.assertIsNone(D.collect(None))


if __name__ == '__main__':
    unittest.main()
