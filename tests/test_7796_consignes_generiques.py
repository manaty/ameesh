# SPDX-License-Identifier: AGPL-3.0-only
"""#7796 (#7637): the watch instruction names the canon's staging hosts.

The bare profile declares none and the text names none; a mesh whose Host
cards carry a `staging:` block sees its own hosts, the workstation marked.
"""
from __future__ import annotations

import unittest

from ameesh import canon, chat, watch


class WatchInstructionTest(unittest.TestCase):
    def test_bare_profile_names_no_host(self):
        text = watch.instruction("w7796")
        self.assertNotIn("$staging_hosts", text)
        self.assertIn("none declared by the canon yet", text)
        self.assertEqual(text, watch.instruction("w7796", canon.StagingTopology()))

    def test_canon_hosts_are_named_in_order(self):
        topology = canon.StagingTopology(hosts=("vm-a", "vm-b", "srv", "desk"),
                                         agents=("vm-a", "vm-b"), server="srv",
                                         workstation="desk")
        text = watch.instruction("w7796", topology)
        self.assertIn("vm-a, vm-b, srv, desk (workstation);", text)
        self.assertIn("the workstation last", text)


class ChatInstructionTest(unittest.TestCase):
    def test_placeholders_are_filled(self):
        text = chat.instruction("human:alice", "chat-alice")
        self.assertNotIn("$human", text)
        self.assertIn("to human:alice or to their desk", text)
        self.assertIn("--assignee <agent-orchestration>", text)

    def test_personal_follow_ups_are_typed_and_self_assigned(self):
        # #8224 (manaty/ameesh#6403): the avatar's own follow-ups are typed
        # follow-up, assigned to itself with --externe (an external agent
        # is refused without it), woken then closed.
        text = chat.instruction("human:alice", "chat-alice")
        self.assertIn("work add --type follow-up --title", text)
        self.assertIn("--assignee chat-alice --externe`", text)
        self.assertIn("ameesh work wake <id> --at", text)
        self.assertIn("ameesh work close <id> --done", text)
        self.assertIn("ameesh work close <id> --abandoned", text)


if __name__ == "__main__":
    unittest.main()
