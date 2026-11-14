# SPDX-License-Identifier: AGPL-3.0-only
"""#7051 (avatar A3, design 5322 §1.1, §2.3, §2.6): harness qualification.

An avatar is attached by its human and stays driven by the executor: its
harness must declare **both** an interactive command and a resident mode.
Claude and Codex qualify; DeepSeek (resident ACP, no interactive mode) and an
ACP harness without `ameesh.interactive` are refused by `canon check`
(`avatar-harness-unsupported`) and by the move's `prepare` probe.

The #5322 acceptance witnesses are exercised through a real move: the journal
stays one continuous conversation (an attach exchange and a mail exchange
before the move are in the target's segment), no native session or credential
file is copied nor named in the move row/segment/turn context, and a failed
move returns the source to its own native session (reprise).
"""
from __future__ import annotations

import dataclasses
import json
import os
import stat
import types
import unittest
from unittest import mock

from ameesh import (canon, conversation as C, conversation_move as M,
                    harnesses, registry, storage)

from . import test_canon
from .support import PgTestCase
from .test_canon import codes, write

SECRET = "sk-test-0123456789abcdef-SECRET"
TRAP_SESSION = "TRAP-NATIVE-SESSION-7051"
TRAP_CREDENTIAL = "TRAP-CREDENTIAL-7051"
READY = {check: True for check in M.CHECKS}


def ready(_move):
    return READY


# --------------------------------------------------------------------------
# canon: the avatar-harness-unsupported rule
# --------------------------------------------------------------------------

def avatar_card(title: str, harness: str, *, responsible: str = "human:alice",
                harnesses_list: list[str] | None = None) -> str:
    lines = ["---", "type: Persona", "title: " + title,
             "responsible: " + responsible, "team: acme-web",
             "capabilities: [read, propose]", "harness: " + harness]
    if harnesses_list:
        lines.append("harnesses: [%s]" % ", ".join(harnesses_list))
    lines += ["avatar:", "  conversation: portable", "  children: own-host",
              "---", "", "# avatar", ""]
    return "\n".join(lines)


def descriptor(ident: str, *, protocol: str = "cli", interactive=None,
               resident=True) -> dict:
    """A minimal valid harness descriptor for the host-descriptor directory."""
    document = {
        "id": ident, "name": ident, "version": "1.0.0", "schema_version": "1",
        "description": "test harness", "repository": "https://example.invalid/x",
        "license": "MIT",
        "ameesh": {
            "protocol": protocol, "binary": "faux-" + ident, "command": [],
            "interactive": interactive,
            "stream": "acp-json" if protocol == "acp" else "text",
        },
    }
    if protocol == "cli":
        document["ameesh"]["command"] = ["--json"]
        document["ameesh"]["session"] = {"flag": ["--resume"]}
    if resident:
        document["ameesh"]["resident"] = (
            {"protocol": "acp", "min_version": "1.0.0"} if protocol == "acp"
            else {"protocol": "claude-stream-json", "command": ["--resident"],
                  "min_version": "1.0.0"})
    return document


class AvatarHarnessCanonTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()
        self.host_dir = os.path.join(self.make_tmp(), "harnais")
        os.makedirs(self.host_dir, mode=0o700)
        self.env = mock.patch.dict(
            os.environ, {"AMEESH_HARNESSES_DIR": self.host_dir})
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_host(self, ident: str, **kwargs) -> None:
        path = os.path.join(self.host_dir, ident + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(descriptor(ident, **kwargs), fh)
        os.chmod(path, 0o600)

    def load(self):
        return canon.load(self.root, untrusted=True)

    def errors(self) -> set[str]:
        return codes(canon.validate(self.load()), canon.ERROR)

    def findings(self, code: str) -> list:
        return [finding for finding in canon.validate(self.load())
                if finding.code == code]

    def test_claude_and_codex_qualify(self):
        write(self.root, "agents/alice-avatar.md",
              avatar_card("alice-avatar", "claude"))
        write(self.root, "agents/bruno-avatar.md",
              avatar_card("bruno-avatar", "codex", responsible="human:bruno"))
        self.assertNotIn("avatar-harness-unsupported", self.errors())

    def test_deepseek_avatar_refused_without_interactive(self):
        write(self.root, "agents/alice-avatar.md",
              avatar_card("alice-avatar", "deepseek"))
        found = self.findings("avatar-harness-unsupported")
        self.assertTrue(found, self.errors())
        self.assertIn("interactive", found[0].message)
        self.assertIn("deepseek", found[0].message)
        # the descriptor itself is known: the refusal is about its modes
        self.assertNotIn("agent-harness-unknown", self.errors())

    def test_acp_avatar_without_interactive_refused(self):
        self.write_host("acp-no-interactive", protocol="acp", interactive=None)
        write(self.root, "agents/alice-avatar.md",
              avatar_card("alice-avatar", "acp-no-interactive"))
        found = self.findings("avatar-harness-unsupported")
        self.assertTrue(found, self.errors())
        self.assertIn("interactive", found[0].message)

    def test_interactive_harness_without_resident_refused(self):
        self.write_host("interactive-only", interactive=[], resident=False)
        write(self.root, "agents/alice-avatar.md",
              avatar_card("alice-avatar", "interactive-only"))
        found = self.findings("avatar-harness-unsupported")
        self.assertTrue(found, self.errors())
        self.assertIn("resident", found[0].message)

    def test_a_declared_list_is_qualified_harness_by_harness(self):
        write(self.root, "agents/alice-avatar.md",
              avatar_card("alice-avatar", "claude",
                          harnesses_list=["claude", "deepseek"]))
        found = self.findings("avatar-harness-unsupported")
        self.assertTrue(found, self.errors())
        self.assertIn("deepseek", found[0].message)
        self.assertNotIn("claude", found[0].message)

    def test_non_avatar_agent_is_not_judged(self):
        # the example canon carries `ouvrier` on deepseek with no `avatar`
        # block: the avatar rule must not turn it into an error.
        self.assertNotIn("avatar-harness-unsupported", self.errors())

    def test_package_descriptors_qualify_as_designed(self):
        found, _findings = harnesses.scan()
        for name in ("claude", "codex"):
            with self.subTest(harness=name):
                self.assertEqual(harnesses.avatar_problems(found[name]), [])
        problems = harnesses.avatar_problems(found["deepseek"])
        self.assertTrue(any("interactive" in problem for problem in problems), problems)
        self.assertFalse(any("resident" in problem for problem in problems), problems)


# --------------------------------------------------------------------------
# move prepare: the target harness must be interactive and resident
# --------------------------------------------------------------------------

class _Adapter:
    def __init__(self, binary: str, *, attach: bool, resident: bool):
        self.binary = binary
        self.spec = types.SimpleNamespace(attach=attach)
        self.descriptor = types.SimpleNamespace(
            resident={"protocol": "x"} if resident else {})


class MoveProbeTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = types.SimpleNamespace(accounts=None)
        self.binary = os.path.join(self.make_tmp(), "harness")
        with open(self.binary, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(self.binary, 0o755)

    def probes(self, *, attach: bool, resident: bool) -> dict:
        return M.host_probes(
            self.cfg, {"harness": "claude"},
            adapter_for=lambda _harness, _binary: _Adapter(
                self.binary, attach=attach, resident=resident))

    def test_all_checks_true_for_a_qualified_harness(self):
        verdicts = self.probes(attach=True, resident=True)
        self.assertEqual([check for check in M.CHECKS if verdicts[check] is not True], [])

    def test_interactive_only_is_not_resident(self):
        verdicts = self.probes(attach=True, resident=False)
        self.assertTrue(verdicts["interactive"])
        self.assertFalse(verdicts["resident"])

    def test_resident_only_is_not_interactive(self):
        verdicts = self.probes(attach=False, resident=True)
        self.assertFalse(verdicts["interactive"])
        self.assertTrue(verdicts["resident"])


# --------------------------------------------------------------------------
# a real move: non-copy, continuous conversation, reprise
# --------------------------------------------------------------------------

class MoveCase(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM conversation_moves; DELETE FROM conversation_log;"
                        " DELETE FROM conversation_heads")
        self.cfg = dataclasses.replace(
            self.cfg, threads_dir=os.path.join(self.tmp, self._testMethodName))
        self.state = os.path.join(self.tmp, self._testMethodName, "state")
        registry.upsert(self.db, "avatar", harness="claude", host="source",
                        cwd="/src/avatar", session_id="sess-source")
        self.lease = registry.claim(self.db, "avatar", "runner-src",
                                    ttl_seconds=300, host="source")
        self.epoch = self.lease["lease_epoch"]
        self.moves = storage.of(self.db).conversation_moves
        self.log = C.Log(self.db, self.cfg, agent="avatar", project="acme", lot="7051")

    def plan(self, **kw):
        return M.plan(self.db, "avatar", "target", requested_by="human:alice", **kw)

    def advance(self, probes=ready):
        return M.advance(self.db, "target", probes=probes,
                         cwd_for=lambda move: "/tgt/avatar")

    def target_claim(self, owner="runner-tgt"):
        return registry.claim(self.db, "avatar", owner, ttl_seconds=300,
                              host="target")

    def first_turn(self, lease, **kw):
        return M.open_first_turn(self.db, self.cfg, self.state, agent="avatar",
                                 host="target", owner="runner-tgt",
                                 epoch=lease["lease_epoch"], **kw)

    def row(self):
        return registry.get(self.db, "avatar")

    def worker(self, lease, agent=None):
        from ameesh.runner import AgentWorker
        os.makedirs(self.state, exist_ok=True)
        worker = types.SimpleNamespace(
            name="avatar", db=self.db, cfg=self.cfg, state_dir=self.state,
            epoch=lease["lease_epoch"], lease_lost=None,
            agent=dict(agent if agent is not None else self.row()),
            _move_turn=None, _path=lambda leaf: os.path.join(self.state, leaf),
            read_session_file=lambda: None)
        for name in ("current_session",):
            setattr(worker, name, getattr(AgentWorker, name).__get__(worker))
        return worker

    def exchange(self, question: str, answer: str, *, author="human:alice",
                 turn: str = "mail:1") -> None:
        self.log.append("human_message", question, author=author, turn=turn)
        self.log.append("turn_end", answer, author="agent:avatar", turn=turn,
                        meta={"status": "completed"})

    def source_files(self) -> tuple[str, str]:
        root = os.path.join(self.tmp, self._testMethodName, "source-state")
        os.makedirs(root, exist_ok=True)
        session = os.path.join(root, "native-session.jsonl")
        credential = os.path.join(root, ".credentials.json")
        with open(session, "w", encoding="utf-8") as fh:
            fh.write('{"type":"assistant","text":"' + TRAP_SESSION + '"}\n')
        with open(credential, "w", encoding="utf-8") as fh:
            fh.write('{"token":"' + TRAP_CREDENTIAL + '"}\n')
        return session, credential


class NonCopyTest(MoveCase):
    def test_no_native_session_or_credential_leaves_the_source(self):
        session, credential = self.source_files()
        before = {}
        for path in (session, credential):
            with open(path, "rb") as fh:
                before[path] = (fh.read(), os.stat(path).st_mtime_ns)
        origin = os.path.dirname(session)
        self.exchange("Avant le déplacement, quelle est la couleur du vélo ?",
                      "Le vélo est vert.", author="human:alice@poste",
                      turn="attach:1")
        self.exchange("Et la selle ?", "La selle est noire.", turn="mail:1")

        move = self.plan()
        with mock.patch("shutil.copyfile") as copyfile, \
                mock.patch("shutil.copy2") as copy2, \
                mock.patch("shutil.copytree") as copytree:
            self.assertEqual(self.advance(), [(move["move_id"], "prepared"),
                                              (move["move_id"], "switched")])
            lease = self.target_claim()
            first = self.first_turn(lease)
            self.assertIsNone(first.session)  # a NEW native session
            self.assertEqual(first.context["source_session"], "sess-source")
            first.launched("sess-target")
            # provisional until the first target turn is verified
            self.assertEqual(self.row()["session_id"], "sess-source")
            self.assertEqual(stat.S_IMODE(os.stat(first.path).st_mode), 0o600)
            with open(first.path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertEqual(len(os.listdir(self.state)), 1)
            self.assertEqual(os.listdir(self.state), [M.SEGMENT_FILENAME])
            done = first.finish(True, session="sess-target", account="acct",
                                harness="claude", cwd="/tgt/avatar")
        for patcher in (copyfile, copy2, copytree):
            patcher.assert_not_called()
        self.assertEqual((done["state"], done["source_cache"]),
                         ("verified", "stale-cache"))
        self.assertEqual(self.row()["session_id"], "sess-target")

        # the pre-move conversation is in the target's segment (attach and mail)
        for expected in ("quelle est la couleur du vélo", "Le vélo est vert.",
                         "Et la selle ?", "La selle est noire."):
            self.assertIn(expected, text)
        # ... and no native session/credential path or value is
        for absent in (TRAP_SESSION, TRAP_CREDENTIAL, "native-session",
                       ".credentials", origin):
            self.assertNotIn(absent, text)

        row = self.moves.get(move["move_id"])
        for absent in (TRAP_SESSION, TRAP_CREDENTIAL, "native-session",
                       ".credentials", origin):
            self.assertNotIn(absent, repr(row))
        # the context carries bounds, digest, the target file and the source
        # session ID -- never a credential or transcript path
        context = repr(first.context)
        for absent in (TRAP_SESSION, TRAP_CREDENTIAL, "native-session",
                       ".credentials", origin):
            self.assertNotIn(absent, context)
        self.assertIn(first.path, context)
        self.assertIn("sess-source", context)

        # the source files are intact and nothing was written beside them
        for path, (content, mtime) in before.items():
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), content)
            self.assertEqual(os.stat(path).st_mtime_ns, mtime)
        self.assertEqual(sorted(os.listdir(origin)), [".credentials.json",
                                                       "native-session.jsonl"])
        self.assertFalse(os.path.exists(os.path.join(self.state,
                                                     M.SEGMENT_FILENAME)))
        # one continuous conversation: no gap, no duplicate
        history = C.history(self.db, "avatar")
        self.assertFalse(history["gap"])
        seqs = [entry["seq"] for entry in history["entries"]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual(sum(entry["kind"] == "human_message"
                             for entry in history["entries"]), 2)


class RepriseTest(MoveCase):
    def test_prepare_failure_keeps_the_source_native_session(self):
        move = self.plan()
        self.assertEqual(self.advance(probes=lambda _move: dict(READY, resident=False)),
                         [(move["move_id"], "failed")])
        self.assertIn("resident", self.moves.get(move["move_id"])["error"])
        row = self.row()
        self.assertEqual((row["host"], row["lease_owner"], row["lease_epoch"],
                          row["session_id"]),
                         ("source", "runner-src", self.epoch, "sess-source"))
        # the source never released its lease: it serves the next turn with
        # its own native session (no re-claim, no provisional session)
        worker = self.worker({"lease_epoch": self.epoch})
        self.assertEqual(worker.current_session(), "sess-source")
        self.assertEqual(registry.begin_turn_diagnosed(
            self.db, "avatar", "runner-src", self.epoch, "turn"), "started")

    def test_failed_first_turn_returns_the_source_to_its_session(self):
        self.plan()
        self.advance()
        lease = self.target_claim()
        first = self.first_turn(lease)
        first.launched("sess-target")
        done = first.finish(False, session=None, account=None, harness="claude",
                            cwd="/tgt/avatar", error="harness exit 1")
        self.assertEqual(done["state"], "failed")
        row = self.row()
        self.assertEqual((row["host"], row["session_id"]),
                         ("source", "sess-source"))
        self.assertGreater(row["lease_epoch"], lease["lease_epoch"])
        self.assertIn("part of that turn may have run", row["pending_prompt"])
        back = registry.claim(self.db, "avatar", "runner-src", ttl_seconds=300,
                              host="source")
        self.assertIsNotNone(back)
        worker = self.worker({"lease_epoch": back["lease_epoch"]})
        self.assertEqual(worker.current_session(), "sess-source")

    def test_verified_target_session_is_what_a_later_worker_resumes(self):
        self.plan()
        self.advance()
        lease = self.target_claim()
        first = self.first_turn(lease)
        worker = self.worker(lease)
        worker._move_turn = first
        self.assertIsNone(worker.current_session())
        first.launched("sess-target")
        self.assertEqual(worker.current_session(), "sess-target")
        first.finish(True, session="sess-target", account="acct",
                     harness="claude", cwd="/tgt/avatar")
        later = self.worker({"lease_epoch": self.row()["lease_epoch"]})
        self.assertEqual(later.current_session(), "sess-target")
        self.assertEqual(self.row()["session_id"], "sess-target")


if __name__ == "__main__":
    unittest.main()
