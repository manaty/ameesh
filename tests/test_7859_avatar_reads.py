# SPDX-License-Identifier: AGPL-3.0-only
"""#7859: the avatar read journal records identifiers, never content."""
import json
import unittest
from unittest import mock

from ameesh import avatar_reads
from . import support  # noqa: F401  (isolated environment, L142)

TOOL = 'mcp__claude_ai_Slack__slack_read_thread'
GMAIL = 'mcp__claude_ai_Gmail__get_thread'
ENV = {'AGENT_MAIL_NAME': 'avatar-7859', 'AMEESH_AVATAR_READ_TOOLS': TOOL + ',' + GMAIL}


def payload(tool=TOOL, event='PostToolUse', tool_input=None, session='s-1'):
    return json.dumps({'hook_event_name': event, 'tool_name': tool,
                       'tool_input': tool_input if tool_input is not None else {
                           'channel_id': 'C123', 'thread_ts': '1760000000.000100',
                           'text': 'a client secret', 'query': 'private search'},
                       'session_id': session})


class AvatarReadTest(unittest.TestCase):
    def test_target_keeps_identifier_fields_only(self):
        fields = avatar_reads.event(json.loads(payload()), env=ENV)
        self.assertEqual(fields['tool'], TOOL)
        self.assertEqual(fields['agent'], 'avatar-7859')
        self.assertEqual(fields['target'], {'channel_id': 'C123',
                                            'thread_ts': '1760000000.000100'})
        self.assertEqual(fields['outcome'], 'ok')
        self.assertNotIn('a client secret', json.dumps(fields))
        self.assertNotIn('private search', json.dumps(fields))

    def test_gmail_thread_and_failed_read_are_journaled_without_content(self):
        fields = avatar_reads.event(json.loads(payload(GMAIL, 'PostToolUseFailure',
                                                       {'thread_id': '18f2', 'subject': 'client'})),
                                    env=ENV)
        self.assertEqual(fields['target'], {'thread_id': '18f2'})
        self.assertEqual(fields['outcome'], 'error')
        self.assertNotIn('client', json.dumps(fields))

    def test_empty_identifier_target_still_journals_tool_and_date(self):
        fields = avatar_reads.event(json.loads(payload(tool_input={'query': 'secret'})), env=ENV)
        self.assertEqual(fields['target'], {})

    def test_tool_not_in_the_applied_policy_is_ignored(self):
        self.assertIsNone(avatar_reads.event(
            json.loads(payload('mcp__memoria__delete')), env=ENV))
        self.assertIsNone(avatar_reads.event(json.loads(payload()), env={}))
        self.assertIsNone(avatar_reads.event(json.loads(payload('Read')), env=ENV))

    def test_non_post_events_and_other_agents_are_ignored(self):
        self.assertIsNone(avatar_reads.event(json.loads(payload(event='PreToolUse')), env=ENV))
        without_name = dict(ENV)
        without_name.pop('AGENT_MAIL_NAME')
        self.assertIsNone(avatar_reads.event(json.loads(payload()), env=without_name))

    def test_journal_records_one_event_and_never_content(self):
        db = object()
        with mock.patch('ameesh.events.record') as record:
            self.assertTrue(avatar_reads.journal(None, payload(), env=ENV, db=db))
        record.assert_called_once()
        args, kwargs = record.call_args
        self.assertEqual(args, (db, 'agent.read', 'agent:avatar-7859'))
        self.assertEqual(kwargs['tool'], TOOL)
        self.assertEqual(kwargs['target'], {'channel_id': 'C123',
                                            'thread_ts': '1760000000.000100'})
        self.assertEqual(kwargs['outcome'], 'ok')
        self.assertNotIn('a client secret', json.dumps(kwargs))
        self.assertNotIn('private search', json.dumps(kwargs))

    def test_journal_is_silent_on_malformed_or_unexpected_input(self):
        with mock.patch('ameesh.events.record') as record:
            self.assertFalse(avatar_reads.journal(None, '{not json', env=ENV, db=object()))
            self.assertFalse(avatar_reads.journal(None, '{}', env=ENV, db=object()))
        record.assert_not_called()

    def test_journal_never_raises_when_the_database_fails(self):
        with mock.patch('ameesh.events.record', side_effect=RuntimeError('db down')):
            self.assertFalse(avatar_reads.journal(None, payload(), env=ENV, db=object()))


if __name__ == '__main__':
    unittest.main()
