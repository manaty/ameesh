import contextlib
import tempfile
import types
import unittest
from unittest import mock

from ameesh import release_cutoff as rc
from ameesh.modules.nexlink import release as nx

OLD, NEW = 'a' * 40, 'b' * 40


class RetagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.tmp.name)
        self.project = rc.ProjectRelease('nexlink', '/unused', model='nexlink')
        self.pending = dict(version='11ck', sha=OLD, main='c' * 40,
                            integration='integration/release-11ck', handoff={'to': 'agent'})
        rc.save_state(self.cfg, {'schema': rc.SCHEMA, 'projects': {'nexlink': {'pending_nexlink': self.pending}}})
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tag = self.stack.enter_context(mock.patch.object(nx, 'release_tags', return_value={'11ck': NEW}))
        self.stack.enter_context(mock.patch.object(rc, 'commit_of', return_value=NEW))
        self.git = self.stack.enter_context(mock.patch.object(rc, '_git', side_effect=self.git_result))
        self.stack.enter_context(mock.patch.object(nx, 'ci_plan', return_value={'parts': [{'workflow': 'sql.yml'}]}))
        self.run = {'headSha': NEW, 'headBranch': self.pending['integration'], 'event': 'workflow_dispatch',
                    'status': 'completed', 'conclusion': 'success', 'createdAt': '2026-01-01', 'databaseId': 1}
        self.runs = self.stack.enter_context(mock.patch.object(nx, 'runs', return_value=[self.run]))
        self.migrations = self.stack.enter_context(mock.patch.object(nx, 'migration_requirements', return_value={'hub': []}))
        self.stack.enter_context(mock.patch.object(nx, 'handoff_preflight', return_value={'migrations': []}))
        self.stack.enter_context(mock.patch.object(nx, '_announce'))
        self.paths = '.ameesh/staging-migrations.json\0'

    def git_result(self, repo, *args, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout=self.paths if args[0] == 'diff' else '')

    def retag(self):
        return nx.retag(self.cfg, None, self.project, version='11ck', sha=NEW,
                        reason='migration declaration', actor='pilot', now=1)

    def saved(self):
        return rc.load_state(self.cfg)['projects']['nexlink']['pending_nexlink']

    def test_accept_and_reject_old_attestation(self):
        self.retag()
        pending = self.saved()
        self.assertEqual(pending['sha'], NEW)
        self.assertEqual(pending['retags'][0]['old_sha'], OLD)
        self.migrations.assert_called_with(self.project, NEW, self.pending['main'])
        self.assertEqual(nx.verify_attestation(self.project, pending, {'sha': OLD})[0], 'reject')

    def test_forbidden_delta(self):
        self.paths = 'src/app.py\0'
        with self.assertRaises(rc.ReleaseError): self.retag()
        self.assertEqual(self.saved()['sha'], OLD)

    def test_red_ci(self):
        self.run['conclusion'] = 'failure'
        with self.assertRaises(rc.ReleaseError): self.retag()
        self.assertEqual(self.saved()['sha'], OLD)

    def test_missing_tag(self):
        self.tag.return_value = {}
        with self.assertRaises(rc.ReleaseError): self.retag()
        self.assertEqual(self.saved()['sha'], OLD)
