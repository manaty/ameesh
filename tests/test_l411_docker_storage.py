# SPDX-License-Identifier: AGPL-3.0-only
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ameesh import containers, docker_storage as D, exploitation, menage


class DockerStorageTest(unittest.TestCase):
    def runtime(self, volumes=(), cache=(), cache_rc=0, buildx_rc=0, buildx_error=""):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[1:3] == ['buildx', 'version']:
                return SimpleNamespace(returncode=buildx_rc, stdout='', stderr=buildx_error)
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
                                      cache_rc=1, buildx_rc=1,
                                      buildx_error="docker: unknown command: docker buildx")
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

    def test_buildx_probe_failure_does_not_claim_plugin_absent(self):
        for mode in ('timeout', 'oserror', 'generic', 'broken'):
            with self.subTest(mode=mode):
                runtime, _ = self.runtime(cache_rc=1, buildx_rc=1,
                                          buildx_error='plugin crashed')
                original = runtime.run

                def run(argv, **kwargs):
                    if argv[1:3] == ['buildx', 'version']:
                        if mode == 'timeout':
                            raise subprocess.TimeoutExpired(argv, 4)
                        if mode == 'oserror':
                            raise OSError('unavailable')
                        if mode == 'broken':
                            return SimpleNamespace(returncode=1, stdout='',
                                stderr='failed to fetch metadata: docker-buildx: permission denied')
                    return original(argv, **kwargs)

                runtime.run = run
                data = D.collect(runtime, now=1000)
                self.assertFalse(data['build_cache_unsupported'])
                self.assertIsNone(data['build_cache_bytes'])
                self.assertEqual(data['errors'],
                                 ['default builder cache inventory unavailable'])
                self.assertEqual([a['type'] for a in D.findings(
                    data, menage.Policy(), 'pc', 1000)], ['docker_storage_unavailable'])

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
            worktrees=lambda *args, **kwargs: [],
            latest_by_path=lambda *args, **kwargs: [])
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


class StalenessFromPassPeriodTest(unittest.TestCase):
    """#5861 : la fraîcheur se juge sur la période de la passe qui relève
    l'inventaire, pas sur 1800 s en dur (AG1 à 2091 s, alerte permanente)."""

    def test_one_and_a_half_periods_raise_nothing(self):
        period = D.default_pass_period()
        data = {'sampled_ts': 1000, 'pass_period_s': period}
        self.assertEqual(D.findings(data, menage.Policy(), 'pc', 1000 + 1.5 * period), [])
        # sans période relevée (avant #5861) : la cadence par défaut
        self.assertEqual(D.findings({'sampled_ts': 1000}, menage.Policy(), 'pc',
                                    1000 + 1.5 * period), [])
        # AG1 le 05/11 : 2091 s, ce n'est pas une passe perdue
        self.assertEqual(D.findings({'sampled_ts': 1000}, menage.Policy(), 'pc', 3091), [])

    def test_beyond_two_periods_is_stale(self):
        data = {'sampled_ts': 1000, 'pass_period_s': 600.0}
        found = D.findings(data, menage.Policy(), 'pc', 1000 + 1201)
        self.assertEqual([a['type'] for a in found], ['docker_storage_unavailable'])
        self.assertEqual(found[0]['threshold'], 1200)
        self.assertEqual(found[0]['value'], 1201)

    def test_period_is_interval_plus_budget_and_none_without_periodic_pass(self):
        self.assertEqual(D.pass_period(600, 900), 1500.0)
        self.assertIsNone(D.pass_period(0, 900))
        self.assertIsNone(D.pass_period(None, 900))
        data = {'sampled_ts': 1000, 'pass_period_s': None}
        self.assertEqual(D.findings(data, menage.Policy(), 'pc', 10 ** 9), [])

    def test_host_without_executor_raises_no_stale_inventory(self):
        data = {'sampled_ts': 1000}
        self.assertEqual(D.findings(data, menage.Policy(), 'pc', 10 ** 9, executor=False), [])
        hk = SimpleNamespace(
            last_measure=lambda _: [{'host': 'pc', 'data': {'docker_storage': data}},
                                    {'host': 'vm', 'data': {'docker_storage': data}}],
            worktrees=lambda *args, **kwargs: [],
            latest_by_path=lambda *args, **kwargs: [])
        readings = [{'host': 'pc', 'runner_id': 'publish@pc'},
                    {'host': 'vm', 'runner_id': 'r1'}]
        st = SimpleNamespace(housekeeping=hk, hosts=SimpleNamespace(current=lambda _: readings))
        with patch.object(exploitation.storage, 'of', return_value=st), \
             patch.object(exploitation.time, 'time', return_value=10 ** 9):
            found = exploitation._housekeeping_alerts([], object())
        self.assertEqual([(a['type'], a['host']) for a in found],
                         [('docker_storage_unavailable', 'vm')])
