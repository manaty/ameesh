# SPDX-License-Identifier: AGPL-3.0-only
"""Content-bound report persistence and packet reuse (#6185)."""
from contextlib import ExitStack
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import prevalidation, test_attestation as evidence
from tests.test_4304_paquet_poussee_precoce import paquet_auto as auto


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.scope = {
            'plan': {'modules': ['tests.test_a']}, 'selection_digest': '1' * 64,
            'tests': ['tests.test_a'], 'canon_digest': '2' * 64,
            'registry_digest': '3' * 64, 'runner_digest': '4' * 64,
            'profile': 'local-heavy', 'drivers': {'driver': 'psycopg'},
            'database_schema': 'v1', 'fixtures': {}, 'exclusions': {},
        }
        self.now = datetime(2026, 11, 8, tzinfo=timezone.utc)
        self.report = prevalidation.attest_report({
            'schema': prevalidation.REPORT_SCHEMA, 'lot': 6185, 'sha': 'a' * 40,
            'base': 'b' * 40, 'ok': True, 'outcome': 'green', 'modules': ['tests.test_a'],
        }, tree='c' * 40, scope=self.scope, lots=[{'id': '6185', 'sha': 'd' * 40}],
            results={'tests.test_a': 'passed'}, timestamp=self.now.isoformat(), links=[])

    def reuse(self, report=None, **kw):
        args = dict(head='f' * 40, tree='c' * 40, scope=self.scope, now=self.now)
        args.update(kw)
        return prevalidation.reuse_report(self.report if report is None else report, **args)

    def test_new_sha_same_tree_preserves_original_provenance_and_age(self):
        result, causes = self.reuse()
        self.assertEqual(causes, ())
        self.assertEqual(result['sha'], 'f' * 40)
        self.assertEqual(result['attestation'], self.report['attestation'])
        self.assertEqual(result['reuse'], {'source_sha': 'a' * 40, 'candidate_sha': 'f' * 40})
        self.assertEqual(self.report['sha'], 'a' * 40)
        self.assertEqual(result['schema'], prevalidation.REPORT_SCHEMA)

    def test_n_minus_one_report_and_corrupt_artifact_are_cache_misses(self):
        old = dict(self.report)
        del old['attestation']
        self.assertEqual(self.reuse(old), (None, ('attestation-absent',)))
        altered = copy.deepcopy(self.report)
        altered['attestation']['payload']['tree'] = 'f' * 40
        self.assertEqual(self.reuse(altered), (None, ('artifact-altered',)))
        for value in (None, [], {}, {'payload': []}):
            malformed = dict(self.report, attestation=value)
            self.assertIsNone(self.reuse(malformed)[0])

    def test_postgres_json_retains_the_envelope_and_n_minus_one_columns(self):
        from ameesh.storage.postgres.prevalidation import PrevalidationReports
        database = mock.Mock()
        store = PrevalidationReports(database)
        store.record(self.report)
        sql, parameters = database.execute.call_args.args
        self.assertIn('INSERT INTO prevalidation_reports', sql)
        self.assertEqual(parameters[0:2], (6185, 'a' * 40))
        self.assertEqual(parameters[4], prevalidation.REPORT_SCHEMA)
        self.assertEqual(json.loads(parameters[-1]), self.report)
        database.query.return_value = [{'report': json.loads(parameters[-1])}]
        recovered = store.latest(6185)['report']
        self.assertIsNotNone(self.reuse(recovered)[0])

    def test_recomposition_scope_change_non_green_and_expiration_are_named(self):
        self.assertEqual(self.reuse(tree='d' * 40), (None, ('tree-changed',)))
        changed = dict(self.scope, profile='other')
        self.assertEqual(self.reuse(scope=changed), (None, ('profile-changed',)))
        from datetime import timedelta
        self.assertEqual(self.reuse(now=self.now + timedelta(seconds=2), max_age_seconds=1),
                         (None, ('expired',)))
        for status in ('failed', 'skipped', 'inconclusive'):
            report = copy.deepcopy(self.report)
            report['attestation']['payload']['results']['tests.test_a'] = status
            report['attestation'] = evidence.seal(report['attestation']['payload'])
            self.assertIn('tests-not-green', self.reuse(report)[1])


class PacketTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='attestation-6185-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'manifest.md').write_text('final composition')
        self.git('add', '.')
        self.git('commit', '-qm', 'manifest')
        self.base = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        self.path = self.root / 'state' / 'report.json'
        self.scope = ReportTest('test_new_sha_same_tree_preserves_original_provenance_and_age')
        self.scope.setUp()
        self.context = (self.scope.scope, self.path, [{'id': '6185', 'sha': self.base}], 3600)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(auto, '_state_dir', return_value=str(self.root / 'state')))
        self.stack.enter_context(mock.patch.object(auto, 'contract_attestation_context', return_value=self.context))
        self.stack.enter_context(mock.patch.object(auto, 'project_python', return_value='/isolated/python'))
        self.stack.enter_context(mock.patch.object(auto, 'contract_modules', return_value=['tests.test_a']))
        self.stack.enter_context(mock.patch.object(auto.shutil, 'which', return_value='/isolated/ameesh'))
        self.execute = self.stack.enter_context(mock.patch.object(auto, 'run_contract_modules',
            side_effect=lambda *a, **k: auto.TestRun('contrats', ['tests.test_a'], 0,
                                                   'Ran 2 tests in 0.1s\nOK\n', .1)))
        # A test must never call the real mailbox, heavy wrapper or canon via a
        # subprocess. Real Git for the disposable repository is the only permit.
        original = subprocess.run
        def guarded(argv, *args, **kw):
            if argv[0] != 'git':
                raise AssertionError('unexpected real subprocess: ' + repr(argv))
            return original(argv, *args, **kw)
        self.stack.enter_context(mock.patch.object(subprocess, 'run', side_effect=guarded))

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def run_pass(self):
        return auto.prepublication_contracts(self.repo, base=self.base, budget=20)

    def test_final_manifest_then_regel_same_tree_do_not_reexecute(self):
        first = self.run_pass()
        self.assertEqual(first.evidence['attestation']['payload']['tree'], self.tree)
        self.assertTrue(self.path.is_file())
        self.assertFalse(self.path.is_relative_to(self.repo))
        self.git('commit', '--allow-empty', '-qm', 'regel')
        second = self.run_pass()
        self.assertEqual(self.execute.call_count, 1)
        self.assertEqual(second.tree, first.tree)
        self.assertNotEqual(second.head, first.head)
        self.assertEqual(second.evidence['reuse']['source_sha'], first.head)
        self.assertEqual(second.evidence['tree']['head'], second.head)
        self.assertEqual(second.evidence['tree']['snapshot'], first.head)
        self.assertEqual(second.run.tests, 2)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_n_minus_one_tree_is_an_object_with_string_fields(self):
        report = self.run_pass().evidence
        self.assertEqual(report['schema'], prevalidation.REPORT_SCHEMA)
        self.assertIsInstance(report['tree'], dict)
        for field in ('head', 'snapshot', 'plan'):
            self.assertIsInstance(report['tree'][field], str)
        self.assertEqual(report['tree']['head'], report['sha'])
        self.assertEqual(report['tree']['git_tree'], self.tree)
        self.assertEqual(report['attestation']['payload']['tree'], self.tree)
        incompatible = dict(report, tree=self.tree)
        self.assertEqual(prevalidation.reuse_report(incompatible, head=report['sha'], tree=self.tree,
            scope=self.scope.scope, now=datetime(2100, 1, 1, tzinfo=timezone.utc)),
            (None, ('report-tree-invalid',)))

    def test_no_age_policy_disables_reuse_and_expired_evidence_reexecutes(self):
        self.context = (*self.context[:3], None)
        with mock.patch.object(auto, 'contract_attestation_context', return_value=self.context):
            self.run_pass()
            self.run_pass()
        self.assertEqual(self.execute.call_count, 2)
        previous = auto.read_contract_attestation(self.path)
        previous['attestation']['payload']['timestamp'] = '2000-01-01T00:00:00+00:00'
        previous['attestation'] = evidence.seal(previous['attestation']['payload'])
        auto.save_contract_attestation(self.path, previous)
        self.run_pass()
        self.assertEqual(self.execute.call_count, 3)

    def test_changed_manifest_replays_and_wrong_tree_is_never_green(self):
        first = self.run_pass()
        (self.repo / 'manifest.md').write_text('removed a lot')
        self.git('commit', '-qam', 'recompose')
        second = self.run_pass()
        self.assertEqual(self.execute.call_count, 2)
        self.assertNotEqual(first.tree, second.tree)
        old, causes = prevalidation.reuse_report(first.evidence, head=second.head,
            tree=second.tree, scope=self.scope.scope, now=datetime(2100, 1, 1, tzinfo=timezone.utc))
        self.assertIsNone(old)
        self.assertIn('tree-changed', causes)

    def test_skipped_zero_tests_and_incomplete_runs_never_reuse(self):
        for output, code, timed in [('Ran 2 tests in 0.1s\nOK (skipped=1)\n', 0, False),
                                    ('Ran 0 tests in 0.1s\nOK\n', 0, False),
                                    ('timeout', None, True)]:
            self.path.unlink(missing_ok=True)
            self.execute.side_effect = lambda *a, **k: auto.TestRun(
                'contrats', ['tests.test_a'], code, output, .1, timed_out=timed)
            first = self.run_pass()
            self.assertEqual(first.evidence['outcome'], 'run-incomplete')
            before = self.execute.call_count
            self.run_pass()
            self.assertEqual(self.execute.call_count, before + 1)

    def test_unavailable_canon_or_heavy_wrapper_does_not_consume_real_services(self):
        with mock.patch.object(auto, 'contract_attestation_context', side_effect=ValueError('no canon')):
            report = self.run_pass()
        self.assertIsNone(report.evidence)
        with mock.patch.object(auto.shutil, 'which', return_value=None):
            report = self.run_pass()
        self.assertTrue(report.run.inconclusive)
        self.assertEqual(self.execute.call_count, 1)

    def test_existing_json_storage_receives_the_carrier_report(self):
        report = self.run_pass()
        connection = mock.Mock()
        with mock.patch('ameesh.db.connect', return_value=connection), \
                mock.patch('ameesh.config.load'), mock.patch('ameesh.prevalidation.record') as record, \
                mock.patch.object(auto, 'note_carrier', return_value='noted'):
            auto.record_contract_result(self.repo, 6185, report, base=self.base, target='main')
        stored = record.call_args.args[1]
        self.assertEqual(stored['lot'], 6185)
        self.assertEqual(stored['schema'], prevalidation.REPORT_SCHEMA)
        self.assertEqual(stored['attestation'], report.evidence['attestation'])
        connection.close.assert_called_once()
        # config.load uses SystemExit for a missing explicit config: optional
        # persistence must still leave the executed result and journal usable.
        with mock.patch('ameesh.config.load', side_effect=SystemExit('missing config')), \
                mock.patch.object(auto, 'note_carrier', return_value='noted') as note:
            auto.record_contract_result(self.repo, 6185, report, base=self.base, target='main')
        note.assert_called_once()

    def test_state_loss_reads_existing_report_without_a_schema_change(self):
        first = self.run_pass()
        self.path.unlink()
        with mock.patch.object(auto, 'run_command', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(first.evidence).encode(), b'')) as command:
            recovered = auto.previous_contract_attestation(
                self.path, 6185, worktree=self.repo, timeout=3)
        self.assertEqual(recovered, first.evidence)
        self.assertEqual(command.call_args.kwargs['timeout'], 3)
        self.assertEqual(command.call_args.kwargs['env']['PYTHONPATH'], str(self.repo / 'src'))
        with mock.patch.object(auto, 'run_command', side_effect=auto.PaquetAutoError('timeout')):
            self.assertIsNone(auto.previous_contract_attestation(
                self.path, 6185, worktree=self.repo, timeout=1))


class ContextTest(unittest.TestCase):
    def test_real_context_includes_final_manifest_lots_and_external_inputs(self):
        from types import SimpleNamespace
        import sys
        with tempfile.TemporaryDirectory(prefix='context-6185-') as root:
            repo = Path(root) / 'repo'
            repo.mkdir()
            def git(*args):
                return subprocess.run(['git', '-C', str(repo), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            (repo / 'base').write_text('base')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            reviews = repo / 'reviews'
            reviews.mkdir()
            (reviews / 'integration-paquet-example.md').write_text(
                '## Composition\n\n| Rang | Lots | Source | Commit |\n| --- | --- | --- | --- |\n'
                '| 1 | #6185 | `' + base + '` | `' + base + '` |\n')
            git('add', '.')
            git('commit', '-qm', 'manifest')
            launcher = Path(root) / 'ameesh'
            launcher.write_text('isolated launcher')
            source = SimpleNamespace(mode='git', repo='canon', commit='a' * 40, prefix='')
            probe = subprocess.CompletedProcess([], 0, json.dumps(
                ['Python test', sys.executable, 'test platform', [['psycopg', '3']]]).encode(), b'')
            from ameesh.config import Config
            from ameesh import mechanics
            document = SimpleNamespace(override=lambda project: SimpleNamespace(
                contract_selection=mechanics.ContractSelection('contracts.json',
                                                               attestation_max_age_seconds=3600)))
            # #6428 : le pilote initial est posé explicitement ; il ne dépend
            # pas de l'environnement du job. Les parts psql exportent
            # AMEESH_DRIVER=psql : sans cette base, le changement de pilote
            # vérifié plus bas ne changeait rien et le témoin rougissait au
            # nocturne psql alors que le contexte était correct.
            with mock.patch.dict('os.environ', {'AMEESH_DRIVER': 'psycopg'}), \
                    mock.patch('ameesh.config.load', return_value=Config()), \
                    mock.patch('ameesh.canon.load_configured', return_value=[SimpleNamespace(sources=[source])]), \
                    mock.patch('ameesh.mechanics.load_global', return_value=document), \
                    mock.patch.object(auto.shutil, 'which', return_value=str(launcher)), \
                    mock.patch.object(auto, 'run_command', return_value=probe), \
                    mock.patch.object(auto, 'project_python', return_value=sys.executable), \
                    mock.patch.object(auto, '_state_dir', return_value=str(Path(root) / 'state')):
                scope, path, lots, age = auto.contract_attestation_context(
                    repo, base=base, modules=['tests.test_a'], timeout=10, project='demo')
                self.assertEqual(lots, [{'id': '6185', 'sha': base}])
                self.assertEqual(age, 3600)
                self.assertEqual(scope['fixtures']['tree'], git('rev-parse', 'HEAD^{tree}'))
                self.assertNotIn(git('rev-parse', 'HEAD'), json.dumps(scope['plan']))
                self.assertFalse(path.is_relative_to(repo))
                launcher.write_text('changed heavy implementation')
                new_scope, _, _, _ = auto.contract_attestation_context(
                    repo, base=base, modules=['tests.test_a'], timeout=10, project='demo')
                self.assertNotEqual(scope['runner_digest'], new_scope['runner_digest'])
                with mock.patch.dict('os.environ', {'AMEESH_DRIVER': 'psql'}):
                    changed, _, _, _ = auto.contract_attestation_context(
                        repo, base=base, modules=['tests.test_a'], timeout=10, project='demo')
                self.assertNotEqual(scope['drivers'], changed['drivers'])
                from dataclasses import replace
                with mock.patch('ameesh.config.load', return_value=replace(Config(), dsn='changed-db')):
                    other, _, _, _ = auto.contract_attestation_context(
                        repo, base=base, modules=['tests.test_a'], timeout=10, project='demo')
                self.assertNotEqual(changed['drivers']['config_digest'], other['drivers']['config_digest'])
                with mock.patch.object(auto.socket, 'gethostname', return_value='another-physical-host'):
                    other_host, _, _, _ = auto.contract_attestation_context(
                        repo, base=base, modules=['tests.test_a'], timeout=10, project='demo')
                self.assertNotEqual(scope['drivers']['host'], other_host['drivers']['host'])
                with mock.patch('ameesh.canon.load_configured', return_value=[]):
                    with self.assertRaisesRegex(ValueError, 'no readable canon'):
                        auto.contract_attestation_context(repo, base=base, modules=['tests.test_a'], timeout=10, project='demo')


class ProcessReuseTest(unittest.TestCase):
    def test_real_context_reuses_between_processes_with_new_turn_and_lease(self):
        """Exercise the producer twice; only external services are doubled."""
        import os
        import sys
        from tests.test_l1616_mechanics import MECHANICS
        root_code = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='processes-6185-') as temporary:
            root = Path(temporary)
            repo = root / 'repo'
            repo.mkdir()
            def git(*args):
                return subprocess.run(['git', '-C', str(repo), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            (repo / 'manifest.md').write_text('final manifest')
            git('add', '.')
            git('commit', '-qm', 'final manifest')
            base = git('rev-parse', 'HEAD')
            policy = root / 'policy.yaml'
            policy.write_text(MECHANICS.replace('projects: {}',
                'projects:\n  demo:\n    contract_selection:\n'
                '      registry: contracts.json\n      attestation_max_age_seconds: 3600'))
            launcher = root / 'ameesh'
            launcher.write_text('stable heavy launcher')
            counter = root / 'executions'
            child = root / 'produce.py'
            child.write_text('''import importlib.util, json, os, sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from ameesh import config, mechanics
spec = importlib.util.spec_from_file_location('process_auto', sys.argv[1])
auto = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = auto
spec.loader.exec_module(auto)
repo, base, state, policy, launcher, counter = sys.argv[2:]
cfg = replace(config.Config(), state_dir=state, runner_id=os.environ['AMEESH_RUNNER_ID'],
              host=os.environ['AMEESH_HOST'])
document = mechanics.compile_document(Path(policy).read_text())
source = SimpleNamespace(mode='git', repo='pinned-canon', commit='a' * 40, prefix='')
probe = auto.subprocess.CompletedProcess([], 0,
    json.dumps([sys.version, sys.executable, 'test-platform', []]).encode(), b'')
def execute(*args, **kwargs):
    with Path(counter).open('a') as stream:
        stream.write('executed\\n')
    return auto.TestRun('contrats', ['tests.test_a'], 0, 'Ran 2 tests in 0.1s\\nOK\\n', .1)
with mock.patch('ameesh.config.load', return_value=cfg), \\
     mock.patch('ameesh.canon.load_configured', return_value=[SimpleNamespace(sources=[source])]), \\
     mock.patch('ameesh.mechanics.load_global', return_value=document), \\
     mock.patch.object(auto.shutil, 'which', return_value=launcher), \\
     mock.patch.object(auto, 'project_python', return_value=sys.executable), \\
     mock.patch.object(auto, 'run_command', return_value=probe), \\
     mock.patch.object(auto, 'test_driver', return_value='psycopg'), \\
     mock.patch.object(auto, '_state_dir', return_value=state), \\
     mock.patch.object(auto, 'contract_modules', return_value=['tests.test_a']), \\
     mock.patch.object(auto, 'run_contract_modules', side_effect=execute):
    result = auto.prepublication_contracts(Path(repo), base=base, budget=20, project='demo')
print(json.dumps({'reuse': result.evidence.get('reuse'),
                  'scope': result.evidence['attestation']['payload']['scope']}))
''')
            def produce(turn, lease_left):
                env = dict(os.environ, PYTHONPATH=str(root_code / 'src'),
                    AMEESH_TURN_ID=turn, AMEESH_LEASE_LEFT=lease_left,
                    AMEESH_LEASE_EPOCH=turn, AMEESH_RUNNER_ID='runner-' + turn,
                    AMEESH_ROLE='role-' + turn, AMEESH_LOT=turn,
                    AMEESH_CONTAINER_LABELS='labels-' + turn,
                    AMEESH_HOST='label-' + turn, AMEESH_TMPDIR=str(root / turn))
                run = subprocess.run([sys.executable, str(child),
                    str(root_code / 'scripts' / 'paquet-auto.py'), str(repo), base,
                    str(root / 'state'), str(policy), str(launcher), str(counter)],
                    env=env, cwd=repo, capture_output=True, text=True, check=True)
                return json.loads(run.stdout.splitlines()[-1])
            first = produce('first', '120')
            git('commit', '--allow-empty', '-qm', 'regel')
            second = produce('second', '93')
            self.assertIsNone(first['reuse'])
            self.assertEqual(counter.read_text().splitlines(), ['executed'])
            self.assertEqual(second['scope'], first['scope'])
            self.assertEqual(second['reuse']['source_sha'], base)
            self.assertEqual(second['reuse']['candidate_sha'], git('rev-parse', 'HEAD'))
            # The age policy is read through the real producer, not injected
            # into a mocked context. N-1 disables reuse; expiry triggers replay.
            policy.write_text(policy.read_text().replace(
                '\n      attestation_max_age_seconds: 3600', ''))
            absent = produce('third', '90')
            self.assertIsNone(absent['reuse'])
            self.assertEqual(len(counter.read_text().splitlines()), 2)
            saved = next((root / 'state' / 'contract-attestations').glob('*.json'))
            previous = json.loads(saved.read_text())
            previous['attestation']['payload']['timestamp'] = '2000-01-01T00:00:00+00:00'
            previous['attestation'] = evidence.seal(previous['attestation']['payload'])
            saved.write_text(json.dumps(previous))
            policy.write_text(policy.read_text() + '\n      attestation_max_age_seconds: 1')
            expired = produce('fourth', '87')
            self.assertIsNone(expired['reuse'])
            self.assertEqual(len(counter.read_text().splitlines()), 3)
