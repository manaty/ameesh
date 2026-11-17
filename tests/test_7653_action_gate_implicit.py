# SPDX-License-Identifier: AGPL-3.0-only
"""#7653: `canon check` says when an avatar's mesh leaves the gate mode implicit.

The contract of `extensions.ameesh.action_gate.mode` (#7630) is `journal`
(default) or `enforce`; an absent key means `journal`. A canon that carries an
avatar without declaring the mode still works, but the implicit default is
made visible: a warning per avatar card, never an error, and nothing for a
canon without an avatar. A declared mode is enough — this check does not judge
the value (`journal` vs `enforce` is #7630's policy reader).
"""
from __future__ import annotations

import unittest

from ameesh import canon

from .test_canon import _TmpMixin, codes, write

FEDERATION = "id: acme\nroot: acme\nmembers: []\n"
GATE = ("extensions:\n"
        "  ameesh:\n"
        "    action_gate:\n"
        "      mode: %s\n")


def avatar_card(title: str = "alice-avatar", *, tools: bool = False) -> str:
    lines = ["---", "type: Persona", "title: " + title,
             "responsible: human:alice", "team: acme-web",
             "capabilities: [read, propose]", "harness: claude",
             "avatar:", "  conversation: portable", "  children: own-host"]
    if tools:
        # #7630 reads the list under `enforce`; journal keeps it too.
        lines.append("tools: [Read]")
    lines += ["---", "", "# avatar", ""]
    return "\n".join(lines)


class ActionGateImplicitTest(_TmpMixin, unittest.TestCase):

    def setUp(self) -> None:
        self.root = self.example_copy()

    def check(self) -> list[canon.Finding]:
        loaded = canon.load(self.root, untrusted=True)
        return canon.validate(loaded)

    def warnings(self, findings: list[canon.Finding]) -> list[canon.Finding]:
        return [f for f in findings if f.code == "action-gate-mode-implicit"]

    def test_absent_mode_warns_for_the_avatar(self):
        write(self.root, "federation.yaml", FEDERATION)
        write(self.root, "agents/alice-avatar.md", avatar_card())
        findings = self.check()
        found = self.warnings(findings)
        self.assertEqual(len(found), 1, findings)
        self.assertEqual(found[0].severity, canon.WARNING)
        self.assertEqual(found[0].agent, "alice-avatar")
        self.assertIn("journal", found[0].message)
        self.assertIn("enforce", found[0].message)
        self.assertEqual(canon.errors(findings), [])

    def test_no_avatar_keeps_the_default_silent(self):
        write(self.root, "federation.yaml", FEDERATION)
        findings = self.check()
        self.assertNotIn("action-gate-mode-implicit", codes(findings))

    def test_explicit_journal_is_silent(self):
        write(self.root, "federation.yaml", FEDERATION + GATE % "journal")
        write(self.root, "agents/alice-avatar.md", avatar_card(tools=True))
        findings = self.check()
        self.assertNotIn("action-gate-mode-implicit", codes(findings))

    def test_explicit_enforce_is_silent(self):
        write(self.root, "federation.yaml", FEDERATION + GATE % "enforce")
        write(self.root, "agents/alice-avatar.md", avatar_card(tools=True))
        findings = self.check()
        self.assertNotIn("action-gate-mode-implicit", codes(findings))

    def test_gate_block_without_mode_is_implicit(self):
        write(self.root, "federation.yaml",
              FEDERATION + "extensions:\n  ameesh:\n    action_gate: {}\n")
        write(self.root, "agents/alice-avatar.md", avatar_card())
        findings = self.check()
        self.assertEqual([f.agent for f in self.warnings(findings)], ["alice-avatar"])

    def test_each_avatar_gets_the_reminder_once(self):
        write(self.root, "federation.yaml", FEDERATION)
        write(self.root, "agents/alice-avatar.md", avatar_card("alice-avatar"))
        write(self.root, "agents/bruno-avatar.md", avatar_card("bruno-avatar"))
        findings = self.check()
        found = self.warnings(findings)
        self.assertEqual(sorted(f.agent for f in found),
                         ["alice-avatar", "bruno-avatar"])
        self.assertEqual(canon.errors(findings), [])
