# SPDX-License-Identifier: AGPL-3.0-only
"""#6085: read-only account connectors of an avatar, named tool by tool."""
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from ameesh import avatar_connectors, avatar_tools as policy, canon, harnesses, spawn
from . import support  # noqa: F401  (isolated environment, L142)
from .test_7630_avatar_tools import avatar, mesh

SLACK_READ = ['mcp__claude_ai_Slack__slack_read_thread',
              'mcp__claude_ai_Slack__slack_search_public_and_private']
GMAIL_READ = ['mcp__claude_ai_Gmail__search_threads', 'mcp__claude_ai_Gmail__get_thread']


class ConnectorPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent = dict(name='avatar', canon_governed=True, harness='claude')
        self.card = avatar(dict(tools=['Read', 'mcp:memoria', 'connector:claude_ai_Slack', *SLACK_READ,
                                       'connector:claude_ai_Gmail', *GMAIL_READ]))
        self.mesh = mesh(self.card, mode='enforce')
        self.write_mcp({'memoria': {'type': 'http', 'url': 'https://memoria.example/mcp',
                                    'headers': {'Authorization': 'Bearer ${MEMORIA_TOKEN}'}}})
        patch = mock.patch.object(canon, 'load_configured', side_effect=lambda cfg: [self.mesh])
        patch.start()
        self.addCleanup(patch.stop)

    def write_mcp(self, servers):
        with open(os.path.join(self.tmp.name, '.mcp.json'), 'w') as handle:
            json.dump({'mcpServers': servers}, handle)

    def prepare(self, **kwargs):
        return policy.prepare(None, self.agent, self.tmp.name, self.tmp.name, **kwargs)

    def settings(self):
        with open(os.path.join(self.tmp.name, avatar_connectors.SETTINGS_FILE)) as handle:
            return json.load(handle)

    def refused(self, code):
        with self.assertRaises(spawn.SpawnToolsError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, code)

    def test_connectors_replace_strict_flag_with_private_settings(self):
        applied = self.prepare()
        path = os.path.join(self.tmp.name, avatar_connectors.SETTINGS_FILE)
        self.assertEqual(applied.args[:2], ('--tools', 'Read'))
        self.assertNotIn('--strict-mcp-config', applied.args)
        self.assertEqual(applied.args[-2:], ('--settings', path))
        self.assertIn('--mcp-config', applied.args)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(self.settings()['allowedMcpServers'], [
            {'serverName': 'memoria'},
            {'serverUrl': 'https://mcp.slack.com/*'},
            {'serverUrl': 'https://gmailmcp.googleapis.com/*'}])
        self.assertIs(self.settings()['disableAllHooks'], False)

    def test_hook_refuses_every_mcp_tool_not_named_in_the_card(self):
        self.prepare()
        (rule,) = self.settings()['hooks']['PreToolUse']
        self.assertIn('exit 2', rule['hooks'][0]['command'])
        refused = re.compile(rule['matcher'])  # same lookahead syntax in JavaScript
        for allowed in [*SLACK_READ, *GMAIL_READ, 'mcp__memoria__search', 'mcp__memoria__read', 'Read', 'Bash']:
            self.assertIsNone(refused.search(allowed), allowed)
        for tool in ('mcp__claude_ai_Slack__slack_send_message',
                     'mcp__claude_ai_Slack__slack_read_thread_and_reply',  # an upgrade's new tool
                     'mcp__claude_ai_Gmail__send_message', 'mcp__claude_ai_Gmail__create_draft',
                     'mcp__claude_ai_Notion__search', 'mcp__memoria2__search', 'mcp__other__x'):
            self.assertIsNotNone(refused.search(tool), tool)

    def test_proof_journals_connector_tool_names_and_digest(self):
        applied = self.prepare()
        self.assertEqual(applied.proof['connectors'], {'claude_ai_Slack': SLACK_READ,
                                                       'claude_ai_Gmail': GMAIL_READ})
        self.assertEqual(applied.proof['tools'], ['Read'])
        self.assertEqual(applied.proof['mcp'], ['memoria'])
        self.card.fiche.data['tools'] = [t for t in self.card.fiche.data['tools']
                                         if t != GMAIL_READ[1]]
        self.assertNotEqual(applied.proof['digest'], self.prepare().proof['digest'])

    def test_card_without_connectors_is_unchanged_and_clears_stale_settings(self):
        self.prepare()
        self.card.fiche.data['tools'] = ['Read', 'mcp:memoria']
        applied = self.prepare()
        self.assertIn('--strict-mcp-config', applied.args)
        self.assertNotIn('--settings', applied.args)
        self.assertNotIn('connectors', applied.proof)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, avatar_connectors.SETTINGS_FILE)))

    def test_journal_mode_applies_the_same_card(self):
        self.mesh.federation['extensions']['ameesh']['action_gate']['mode'] = 'journal'
        self.assertIn('--settings', self.prepare().args)

    def test_connector_without_named_tools_is_refused(self):
        self.card.fiche.data['tools'] = ['Read', 'connector:claude_ai_Slack']
        self.refused('avatar-connector-without-tools')

    def test_tool_of_an_undeclared_connector_is_refused(self):
        self.card.fiche.data['tools'] = ['Read', *SLACK_READ]
        self.refused('avatar-connector-tool-orphan')
        self.card.fiche.data['tools'] = ['mcp:memoria', 'mcp__memoria__search']
        self.refused('avatar-connector-tool-orphan')

    def test_unknown_or_malformed_connector_is_refused(self):
        self.card.fiche.data['tools'] = ['connector:claude_ai_Notion', 'mcp__claude_ai_Notion__search']
        self.refused('avatar-connector-unknown')
        for value in ('connector:', 'connector:claude.ai Slack'):
            self.card.fiche.data['tools'] = [value]
            with self.subTest(value=value), self.assertRaises(spawn.SpawnToolsError):
                self.prepare()
        self.card.fiche.data['tools'] = ['connector:claude_ai_Slack', 'mcp__claude_ai_Slack__']
        self.refused('avatar-connector-tool-invalid')

    def test_harness_without_connector_support_is_refused(self):
        block = spawn.support('claude')
        stripped = spawn.Support(block.harness, block.tools_flag, block.mcp_flag, block.mcp_source,
                                 block.deny_flag)
        with mock.patch.object(spawn, 'support', return_value=stripped):
            self.refused('avatar-tools-unsupported')

    def test_canon_validation_reports_connector_errors(self):
        self.card.fiche.data['tools'] = ['connector:claude_ai_Slack']
        with self.assertRaises(spawn.SpawnToolsError) as error:
            policy.lists(self.card, self.mesh)
        self.assertEqual(error.exception.code, 'avatar-connector-without-tools')


class McpCredentialTest(unittest.TestCase):
    def copy(self, server):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, '.mcp.json'), 'w') as handle:
                json.dump({'mcpServers': {'memoria': server}}, handle)
            return spawn.mcp_servers(root, '.mcp.json', ['memoria'])['memoria']

    def test_environment_placeholders_are_copied_verbatim(self):
        for server in ({'type': 'http', 'url': 'https://m.example/mcp',
                        'headers': {'Authorization': 'Bearer ${MEMORIA_TOKEN}', 'X-Org': 'acme'}},
                       {'command': 'memoria-mcp', 'env': {'MEMORIA_TOKEN': '${MEMORIA_TOKEN}'}}):
            with self.subTest(server=server):
                self.assertEqual(self.copy(server), server)

    def test_literal_credentials_are_refused(self):
        for server in ({'type': 'http', 'url': 'https://m.example/mcp',
                        'headers': {'Authorization': 'Bearer 3f9a1c0d2b7e'}},
                       {'type': 'http', 'url': 'https://m.example/mcp',
                        'headers': {'X-Api-Key': '${MEMORIA_TOKEN}x'}},
                       {'command': 'memoria-mcp', 'env': {'MEMORIA_TOKEN': 'opaque'}},
                       {'command': 'memoria-mcp', 'env': {'MEMORIA_TOKEN': '${UNSET:-opaque}'}},
                       {'type': 'http', 'url': 'https://m.example/mcp?access_token=opaque'}):
            with self.subTest(server=server), self.assertRaises(spawn.SpawnToolsError) as error:
                self.copy(server)
            self.assertEqual(error.exception.code, 'spawn-context-secret')
            self.assertNotIn('opaque', str(error.exception))


class DescriptorTest(unittest.TestCase):
    def test_claude_descriptor_declares_known_connectors(self):
        block = spawn.support('claude')
        self.assertEqual(block.connectors_replaces, ('--strict-mcp-config',))
        self.assertIn(('claude_ai_Slack', 'https://mcp.slack.com/*'), block.connectors_known)
        self.assertIn(('claude_ai_Gmail', 'https://gmailmcp.googleapis.com/*'), block.connectors_known)
        raw = harnesses.get('claude').ameesh['spawn']
        self.assertEqual(harnesses._check_spawn(raw, 'claude.json'), [])

    def test_invalid_connector_block_is_reported(self):
        raw = dict(harnesses.get('claude').ameesh['spawn'])
        for block in ({'flag': ['--settings']}, {'flag': ['--settings', '{path}'], 'servers': {'x': 'http://x'}},
                      'x'):
            raw['connectors'] = block
            with self.subTest(block=block):
                codes = {f.code for f in harnesses._check_spawn(raw, 'claude.json')}
                self.assertIn('harness-spawn-invalid', codes)


if __name__ == '__main__':
    unittest.main()
