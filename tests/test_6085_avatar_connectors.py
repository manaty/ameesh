# SPDX-License-Identifier: AGPL-3.0-only
"""#6085/#7859: read-only account connectors and MCP tools of an avatar."""
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from ameesh import avatar_connectors, avatar_tools as policy, canon, harnesses, spawn
from . import support  # noqa: F401  (isolated environment, L142)
from .test_7630_avatar_tools import avatar, mesh

MEMORIA_READ = ['mcp__memoria__inbox', 'mcp__memoria__search', 'mcp__memoria__ask',
                'mcp__memoria__read']
SLACK_READ = ['mcp__claude_ai_Slack__slack_read_thread',
              'mcp__claude_ai_Slack__slack_search_public_and_private']
GMAIL_READ = ['mcp__claude_ai_Gmail__search_threads', 'mcp__claude_ai_Gmail__get_thread']


class ConnectorPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent = dict(name='avatar', canon_governed=True, harness='claude')
        self.card = avatar(dict(tools=['Read', 'mcp:memoria', *MEMORIA_READ,
                                       'connector:claude_ai_Slack', *SLACK_READ,
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
        self.assertIn('PostToolUse', self.settings()['hooks'])

    def test_exact_mcp_read_tools_keep_strict_and_add_private_settings(self):
        self.card.fiche.data['tools'] = ['Read', 'mcp:memoria', *MEMORIA_READ]
        applied = self.prepare()
        path = os.path.join(self.tmp.name, avatar_connectors.SETTINGS_FILE)
        self.assertIn('--strict-mcp-config', applied.args)
        self.assertEqual(applied.args[-2:], ('--settings', path))
        self.assertEqual(self.settings()['allowedMcpServers'], [{'serverName': 'memoria'}])
        self.assertEqual(applied.proof['mcp_tools'], {'memoria': MEMORIA_READ})
        self.assertNotIn('connectors', applied.proof)

    def test_hook_refuses_every_mcp_tool_not_named_in_the_card(self):
        self.prepare()
        (rule,) = self.settings()['hooks']['PreToolUse']
        self.assertIn('exit 2', rule['hooks'][0]['command'])
        refused = re.compile(rule['matcher'])  # same lookahead syntax in JavaScript
        for allowed in [*MEMORIA_READ, *SLACK_READ, *GMAIL_READ, 'Read', 'Bash']:
            self.assertIsNone(refused.search(allowed), allowed)
        for tool in ('mcp__memoria__delete', 'mcp__memoria__write',
                     'mcp__claude_ai_Slack__slack_send_message',
                     'mcp__claude_ai_Slack__slack_read_thread_and_reply',  # an upgrade's new tool
                     'mcp__claude_ai_Gmail__send_message', 'mcp__claude_ai_Gmail__create_draft',
                     'mcp__claude_ai_Gmail__get_attachment',  # not named by this card
                     'mcp__claude_ai_Notion__search', 'mcp__memoria2__search', 'mcp__other__x'):
            self.assertIsNotNone(refused.search(tool), tool)

    def test_journal_hook_is_limited_to_the_exact_read_tools(self):
        self.prepare()
        (rule,) = self.settings()['hooks']['PostToolUse']
        journaled = re.compile(rule['matcher'])
        for tool in [*MEMORIA_READ, *SLACK_READ, *GMAIL_READ]:
            self.assertIsNotNone(journaled.search(tool), tool)
        for tool in ('mcp__memoria__delete', 'mcp__claude_ai_Gmail__send_message', 'Read'):
            self.assertIsNone(journaled.search(tool), tool)
        self.assertEqual(rule['hooks'][0]['command'], avatar_connectors.JOURNAL)

    def test_proof_journals_connector_and_mcp_tool_names_and_digest(self):
        applied = self.prepare()
        self.assertEqual(applied.proof['connectors'], {'claude_ai_Slack': SLACK_READ,
                                                       'claude_ai_Gmail': GMAIL_READ})
        self.assertEqual(applied.proof['mcp_tools'], {'memoria': MEMORIA_READ})
        self.assertEqual(applied.proof['tools'], ['Read'])
        self.assertEqual(applied.proof['mcp'], ['memoria'])
        self.card.fiche.data['tools'] = [t for t in self.card.fiche.data['tools']
                                         if t != GMAIL_READ[1]]
        self.assertNotEqual(applied.proof['digest'], self.prepare().proof['digest'])

    def test_attach_wake_hooks_merge_into_the_same_settings_and_rehash(self):
        applied = self.prepare()
        settings = policy.settings_path(applied)
        self.assertTrue(settings)
        avatar_connectors.merge(settings, {'hooks': {'SessionStart': [
            {'hooks': [{'type': 'command', 'command': 'agent-mail wait',
                        'asyncRewake': True}]}]}})
        rehashed = policy.rehash(applied, self.tmp.name)
        self.assertNotEqual(applied.proof['digest'], rehashed.proof['digest'])
        self.assertEqual(rehashed.args, applied.args)
        document = self.settings()
        self.assertIn('PreToolUse', document['hooks'])
        self.assertIn('PostToolUse', document['hooks'])
        self.assertIn('SessionStart', document['hooks'])

    def test_card_without_read_policy_is_unchanged_and_clears_stale_settings(self):
        self.prepare()
        self.card.fiche.data['tools'] = ['Read']
        applied = self.prepare()
        self.assertIn('--strict-mcp-config', applied.args)
        self.assertNotIn('--settings', applied.args)
        self.assertNotIn('connectors', applied.proof)
        self.assertNotIn('mcp_tools', applied.proof)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, avatar_connectors.SETTINGS_FILE)))

    def test_enforce_requires_an_exact_list_for_each_mcp_server(self):
        self.card.fiche.data['tools'] = ['Read', 'mcp:memoria']
        self.refused('avatar-mcp-without-tools')

    def test_journal_mode_keeps_the_whole_server_prefix(self):
        self.mesh.federation['extensions']['ameesh']['action_gate']['mode'] = 'journal'
        self.card.fiche.data['tools'] = ['Read', 'mcp:memoria']
        applied = self.prepare()
        # A journal-mode server without an exact list keeps the historical
        # strict MCP configuration (all its tools); no private settings needed.
        self.assertIn('--strict-mcp-config', applied.args)
        self.assertNotIn('--settings', applied.args)
        self.assertNotIn('mcp_tools', applied.proof)
        block = spawn.support('claude')
        document = avatar_connectors.settings(block, {}, ['memoria'], {})
        refused = re.compile(document['hooks']['PreToolUse'][0]['matcher'])
        self.assertIsNone(refused.search('mcp__memoria__delete'))
        self.assertNotIn('PostToolUse', document['hooks'])

    def test_journal_mode_applies_the_same_card(self):
        self.mesh.federation['extensions']['ameesh']['action_gate']['mode'] = 'journal'
        self.assertIn('--settings', self.prepare().args)

    def test_connector_without_named_tools_is_refused(self):
        self.card.fiche.data['tools'] = ['Read', 'connector:claude_ai_Slack']
        self.refused('avatar-connector-without-tools')

    def test_tool_of_an_undeclared_server_is_refused(self):
        self.card.fiche.data['tools'] = ['Read', *SLACK_READ]
        self.refused('avatar-connector-tool-orphan')
        self.card.fiche.data['tools'] = ['mcp:memoria', 'mcp__other__search']
        self.refused('avatar-connector-tool-orphan')

    def test_server_declared_twice_is_refused(self):
        self.card.fiche.data['tools'] = ['mcp:claude_ai_Slack',
                                         'connector:claude_ai_Slack', *SLACK_READ]
        self.refused('avatar-connector-conflict')

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
                       {'type': 'http', 'url': 'https://m.example/mcp?access_token=opaque'},
                       {'type': 'http', 'url': 'https://user:opaque@m.example/mcp'}):
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
