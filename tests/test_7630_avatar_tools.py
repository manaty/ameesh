# SPDX-License-Identifier: AGPL-3.0-only
"""Avatar policy follows the harness into resident, turn and attach launches."""
import json
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

from ameesh import avatar_tools as policy, canon, mail, registry, resident_runner, runner, spawn
from .support import PgTestCase


def avatar(raw):
    return types.SimpleNamespace(title='avatar', fiche=types.SimpleNamespace(
        data={'avatar': {}, **raw}, ref='home:agents/avatar.md@' + 'a' * 40))


def mesh(card, mode='journal'):
    return types.SimpleNamespace(readable=True, federation={'action_gate': {'mode': mode}},
                                 owns=lambda value: value in (None, ''), agent=lambda name: card)


class AvatarPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent = dict(name='avatar', canon_governed=True, harness='claude')
        self.card = avatar(dict(tools=['Read', 'mcp:readonly'], deny_tools=['Bash(curl *)']))
        self.mesh = mesh(self.card)
        with open(os.path.join(self.tmp.name, '.mcp.json'), 'w') as handle:
            json.dump({'mcpServers': {'readonly': {'command': 'true'},
                                      'slack': {'command': 'false'}}}, handle)
        patch = mock.patch.object(canon, 'load_configured', side_effect=lambda cfg: [self.mesh])
        patch.start()
        self.addCleanup(patch.stop)

    def prepare(self, **kwargs):
        return policy.prepare(None, self.agent, self.tmp.name, self.tmp.name, **kwargs)

    def test_allowed_mcp_only_and_deny_flags_in_private_profile(self):
        applied = self.prepare()
        self.assertEqual(applied.args[:2], ('--tools', 'Read'))
        self.assertIn('--strict-mcp-config', applied.args)
        self.assertEqual(applied.args[-2:], ('--disallowedTools', 'Bash(curl *)'))
        with open(os.path.join(self.tmp.name, spawn.MCP_FILE)) as handle:
            self.assertEqual(json.load(handle)['mcpServers'], {'readonly': {'command': 'true'}})
        for file in (spawn.PROFILE_FILE, spawn.MCP_FILE):
            self.assertEqual(os.stat(os.path.join(self.tmp.name, file)).st_mode & 0o777, 0o600)

    def test_empty_list_disables_tools_and_inherited_mcp(self):
        self.card.fiche.data['tools'] = []
        applied = self.prepare()
        self.assertEqual(applied.args[:2], ('--tools', ''))
        with open(os.path.join(self.tmp.name, spawn.MCP_FILE)) as handle:
            self.assertEqual(json.load(handle), {'mcpServers': {}})

    def test_unqualified_harness_is_refused_even_for_empty_allowlist(self):
        self.card.fiche.data = {'avatar': {}, 'tools': []}
        with self.assertRaises(spawn.SpawnToolsError):
            self.prepare(harness='codex')
        with mock.patch.object(spawn, 'support', return_value=spawn.Support('x', (), (), None)):
            with self.assertRaises(spawn.SpawnToolsError):
                self.prepare(harness='x')

    def test_legacy_mesh_accepts_missing_list_but_enforced_mesh_refuses(self):
        self.card.fiche.data = {'avatar': {}}
        self.assertIsNone(self.prepare())
        self.mesh.federation['action_gate']['mode'] = 'enforce'
        with self.assertRaises(spawn.SpawnToolsError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, 'avatar-tools-missing')

    def test_malformed_list_never_becomes_unrestricted(self):
        for value in (None, 'Read', [None], ['--tools']):
            self.card.fiche.data['tools'] = value
            with self.subTest(value=value), self.assertRaises(spawn.SpawnToolsError):
                self.prepare()

    def test_policy_digest_restarts_resident_after_tool_or_transport_change(self):
        old = self.prepare()
        self.card.fiche.data['tools'] = ['Read']
        changed = self.prepare()
        self.assertNotEqual(old.proof['digest'], changed.proof['digest'])
        self.assertNotEqual(resident_runner._env_digest({'AMEESH_AVATAR_TOOLS_DIGEST': old.proof['digest']}),
                            resident_runner._env_digest({'AMEESH_AVATAR_TOOLS_DIGEST': changed.proof['digest']}))

    def test_changed_mcp_transport_changes_the_proof(self):
        old = self.prepare()
        with open(os.path.join(self.tmp.name, '.mcp.json'), 'w') as handle:
            json.dump({'mcpServers': {'readonly': {'command': 'another-reader'}}}, handle)
        self.assertNotEqual(old.proof['digest'], self.prepare().proof['digest'])

    def test_canon_missing_and_cross_canon_cards_refuse(self):
        self.mesh.agent = lambda name: None
        with self.assertRaises(spawn.SpawnToolsError):
            self.prepare()
        self.mesh.owns = lambda value: False
        with self.assertRaises(spawn.SpawnToolsError):
            self.prepare()

    def test_resident_spawn_receives_restrictions_and_records_proof(self):
        applied = self.prepare()
        fake = types.SimpleNamespace(_avatar_tools=applied, db=object(), name='avatar',
            lock=threading.Lock(), runner=types.SimpleNamespace(launch_lock=threading.Lock(), harness_pids=set()))
        proc = types.SimpleNamespace(pid=7630)
        with mock.patch('ameesh.resident.spawn_leader', return_value=proc) as start, \
                mock.patch.object(policy.events, 'record') as record:
            runner.AgentWorker._spawn_resident(fake, ['claude', '-p'], env={})
        self.assertEqual(start.call_args.args[0], ['claude', *applied.args, '-p'])
        self.assertEqual(record.call_args.kwargs['mode'], 'resident')
        self.assertEqual(record.call_args.kwargs['digest'], applied.proof['digest'])


class AvatarAttachTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register('avatar7630', 'claude', cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET canon_ref = 'home:agents/avatar.md', responsible = 'human:alice', placement_ok = true, placement_profile = ameesh_placement_profile(host, harness, provider, model, credential_mode) WHERE name = 'avatar7630'")
        self.db.execute("INSERT INTO canon_state (host, status) VALUES (%s, 'ok') ON CONFLICT (host, canon) DO UPDATE SET status = 'ok'", (self.cfg.host,))
        self.card = avatar(dict(tools=['Read']))
        self.env_patch = mock.patch.dict(os.environ, {'AMEESH_BIN_DIR': os.path.join(os.path.dirname(__file__), 'fakebin'),
                                                'AMEESH_TEST_LOG': self.turns_log,
                                                'FAKE_RESIDENT_LOG': os.path.join(self.tmp, 'resident-tools.jsonl')})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        patch = mock.patch.object(canon, 'load_configured', return_value=[mesh(self.card)])
        patch.start()
        self.addCleanup(patch.stop)

    def test_attach_has_same_policy_and_durable_proof(self):
        self.assertEqual(runner.run_attach(self.cfg, self.db, 'avatar7630'), 0)
        args = self.turns()[0]['argv']
        self.assertEqual(args[args.index('--tools') + 1], 'Read')
        self.assertIn('--strict-mcp-config', args)
        rows = self.db.query("SELECT data FROM ameesh_events WHERE type = 'agent.tools'")
        self.assertTrue(rows)
        self.assertEqual(rows[-1]['data']['mode'], 'attach')
        self.assertEqual(rows[-1]['data']['mcp'], [])

    def test_remote_attach_uses_the_same_restrictions(self):
        self.assertEqual(runner.run_attach(self.cfg, self.db, 'avatar7630',
            owner='attach:t1:cred-7630', human='human:alice', attach_id='att7630',
            tenant='t1', device='cred-7630', remote=True), 0)
        args = self.turns()[0]['argv']
        self.assertEqual(args[args.index('--tools') + 1], 'Read')
        self.assertIn('--strict-mcp-config', args)

    def test_runner_turn_and_resident_apply_the_card(self):
        for resident in (False, True):
            with self.subTest(resident=resident):
                name = 'avatar7630'
                owner = runner.Runner(self.cfg, self.db, once=not resident)
                lease = registry.claim(self.db, name, owner.runner_id, 3600)
                self.assertIsNotNone(lease)
                worker = runner.AgentWorker(owner, registry.get(self.db, name), lease)
                self.addCleanup(worker.watchdog_stop.set)
                os.makedirs(worker.state_dir, exist_ok=True)
                if resident:
                    import sys
                    from ameesh import adapters, harnesses
                    from .test_l75_resident import claude_doc
                    path = os.path.join(self.tmp, 'fake-avatar-resident.json')
                    with open(path, 'w') as handle:
                        json.dump(claude_doc(), handle)
                    adapter = adapters.HarnessAdapter(harnesses.load(path, source='test'), sys.executable)
                    worker._adapter = lambda: adapter
                    with open(os.path.join(worker.state_dir, 'resident'), 'w') as handle:
                        handle.write('on')
                self.register('sender7630', 'claude', cwd=self.tmp)
                mail.send(self.db, 'sender7630', name, 'hello', kind='request')
                try:
                    with mock.patch.object(resident_runner, 'binary_version', return_value='99.0.0'):
                        self.assertTrue(worker.run_turn(worker.pick()))
                    rows = self.db.query("SELECT data FROM ameesh_events WHERE type = 'agent.tools' ORDER BY id")
                    self.assertEqual(rows[-1]['data']['mode'], 'resident' if resident else 'turn')
                    self.assertEqual(rows[-1]['data']['tools'], ['Read'])
                    self.assertEqual(rows[-1]['data']['mcp'], [])
                    if resident:
                        with open(os.path.join(self.tmp, 'resident-tools.jsonl')) as handle:
                            starts = [json.loads(line)['argv'] for line in handle if '"argv"' in line]
                        self.assertEqual(starts[-1][starts[-1].index('--tools') + 1], 'Read')
                        self.assertIn('--strict-mcp-config', starts[-1])
                finally:
                    worker._close_resident('test end')
                    registry.release(self.db, name, owner.runner_id, int(lease['lease_epoch']))

    def test_unsupported_avatar_refused_before_attach_lease(self):
        with mock.patch.object(spawn, 'support', return_value=None), \
                mock.patch.object(registry, 'attach_claim') as claim:
            self.assertEqual(runner.run_attach(self.cfg, self.db, 'avatar7630'), 1)
        claim.assert_not_called()
        self.assertEqual(self.turns(), [])


class AvatarCanonCheckTest(unittest.TestCase):
    def check(self, raw, mode):
        from . import test_canon
        from .test_7051_avatar_a3 import avatar_card
        with tempfile.TemporaryDirectory() as root:
            import shutil
            shutil.copytree(test_canon.EXAMPLE, root, dirs_exist_ok=True)
            test_canon.write(root, 'agents/alice-avatar.md', avatar_card('alice-avatar', 'claude'))
            loaded = canon.load(root, untrusted=True)
            loaded.federation = {**(loaded.federation or {}), 'action_gate': {'mode': mode}}
            loaded.agent('alice-avatar').fiche.data.update(raw)
            return {f.code for f in canon.validate(loaded) if f.severity == canon.ERROR}

    def test_missing_list_refused_only_when_gate_enforces(self):
        self.assertIn('avatar-tools-missing', self.check({}, 'enforce'))
        self.assertNotIn('avatar-tools-missing', self.check({}, 'journal'))
        self.assertNotIn('avatar-tools-missing', self.check({'tools': []}, 'enforce'))

    def test_bad_list_and_unknown_mode_refused(self):
        self.assertIn('avatar-tools-invalid', self.check({'tools': None}, 'enforce'))
        self.assertIn('avatar-tools-policy-invalid', self.check({}, 'unknown'))
