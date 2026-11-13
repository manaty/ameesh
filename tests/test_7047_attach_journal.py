# SPDX-License-Identifier: AGPL-3.0-only
"""#7047 (avatar A0, design 5322 §2.2): an interactive attach writes the human
prompt and the final answer to `conversation_log` (and the R12 thread) through
its hooks; a driven turn keeps its Recorder and the hook abstains; the hook
never blocks on a database failure. Postgres réel."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from ameesh import attach_journal as A, registry
from ameesh.identity import Binding

from .support import PgTestCase

SID = "sess-attach-7047"
OWNER = "attach:alice@poste"


def transcript(lines) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl", dir=os.environ.get("TMPDIR"))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return path


def assistant(*texts, sidechain=False, tool=False):
    content = [{"type": "text", "text": t} for t in texts]
    if tool:
        content.append({"type": "tool_use", "name": "Bash", "input": {"command": "secret"}})
    return {"type": "assistant", "isSidechain": sidechain,
            "message": {"role": "assistant", "content": content}}


def user(text=None, result=None):
    content = text if text is not None else [{"type": "tool_result", "content": result}]
    return {"type": "user", "message": {"role": "user", "content": content}}


class AttachJournalTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM conversation_log; DELETE FROM conversation_heads")
        registry.upsert(self.db, "avatar", harness="claude", host=self.cfg.host,
                        cwd=self.tmp, session_id=SID)

    def _entries(self):
        return self.db.query("SELECT kind, author, text, event_id, turn, session,"
                             " thread_entry FROM conversation_log"
                             " WHERE agent = 'avatar' ORDER BY seq")

    def _hook(self, event, owner, epoch, *, attach="1", **payload):
        env = self.env(AGENT_MAIL_NAME="avatar", AMEESH_RUNNER_ID=owner,
                       AMEESH_LEASE_EPOCH=str(epoch), AMEESH_ATTACH=attach)
        proc = self.cli("hook", "claude", env=env, stdin=json.dumps(
            dict(payload, hook_event_name=event, session_id=SID, cwd=self.tmp)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_attach_exchange_in_journal_and_thread(self):
        epoch = registry.attach_claim(self.db, "avatar", OWNER, 300)["lease_epoch"]
        self._hook("UserPromptSubmit", OWNER, epoch, prompt="quelle couleur ?")
        path = transcript([user("quelle couleur ?"), assistant("je regarde", tool=True),
                           user(result="SORTIE-OUTIL"), assistant("Le vélo "),
                           assistant("est vert.")])
        self.addCleanup(os.unlink, path)
        self._hook("Stop", OWNER, epoch, transcript_path=path)
        rows = self._entries()
        self.assertEqual([(r["kind"], r["author"], r["text"]) for r in rows],
                         [("human_message", "human:alice@poste", "quelle couleur ?"),
                          ("turn_end", "agent:avatar", "Le vélo est vert.")])
        turn = rows[0]["event_id"].split(":")[2]
        self.assertEqual([r["event_id"] for r in rows],
                         ["attach:%s:%s:1" % (SID, turn), "attach:%s:%s:2" % (SID, turn)])
        self.assertEqual({r["session"] for r in rows}, {SID})
        # R12 : both are in the readable thread, tool output never is
        self.assertTrue(all(r["thread_entry"] for r in rows))
        self.assertNotIn("SORTIE-OUTIL", json.dumps(rows, default=str))
        # a Stop that delivered mail continues the turn: next seq, same turn
        self._hook("Stop", OWNER, epoch, last_assistant_message="Et la selle aussi.")
        last = self._entries()[-1]
        self.assertEqual((last["text"], last["event_id"]),
                         ("Et la selle aussi.", "attach:%s:%s:3" % (SID, turn)))

    def test_driven_turn_abstains_no_duplicate(self):
        epoch = registry.claim(self.db, "avatar", "runner-x", ttl_seconds=300,
                               host=self.cfg.host)["lease_epoch"]
        # inherited AMEESH_ATTACH=1 under a runner lease: the Recorder owns it
        self._hook("UserPromptSubmit", "runner-x", epoch, prompt="tour piloté")
        self._hook("Stop", "runner-x", epoch, last_assistant_message="réponse")
        # an attach identity whose lease was taken over by a runner: unbound
        self._hook("UserPromptSubmit", OWNER, epoch, prompt="fantôme")
        self.assertEqual(self._entries(), [])

    def test_without_attach_flag_nothing(self):
        epoch = registry.attach_claim(self.db, "avatar", OWNER, 300)["lease_epoch"]
        self._hook("UserPromptSubmit", OWNER, epoch, attach="", prompt="hors attach")
        self.assertEqual(self._entries(), [])

    def test_database_failure_never_blocks(self):
        binding = Binding(name="avatar", source="runner", runner_id=OWNER, epoch=1, ok=True)
        db = mock.Mock()
        db.transaction.side_effect = RuntimeError("base indisponible")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"AMEESH_ATTACH": "1"}), redirect_stderr(err):
            A.capture_quietly(self.cfg, db, binding, "UserPromptSubmit",
                              {"session_id": SID, "prompt": "bonjour"})
        self.assertIn("not recorded", err.getvalue())

    def test_lost_state_never_reuses_an_event_id(self):
        first = A.next_ids(self.cfg, "avatar", SID, "UserPromptSubmit", now=1000.0)
        os.unlink(os.path.join(self.cfg.agent_dir("avatar"), A.STATE_FILE))
        second = A.next_ids(self.cfg, "avatar", SID, "UserPromptSubmit", now=1001.0)
        self.assertNotEqual(first, second)
        self.assertEqual(A.next_ids(self.cfg, "avatar", "other", "Stop", now=1002.0),
                         ("1002000", 2))


class TranscriptTest(unittest.TestCase):
    def _final(self, lines):
        path = transcript(lines)
        self.addCleanup(os.unlink, path)
        return A.final_from_transcript(path)

    def test_last_assistant_text_skips_sidechains(self):
        self.assertEqual(self._final([user("q"), assistant("fin", tool=True),
                                      assistant("sous-agent", sidechain=True)]), "fin")

    def test_no_assistant_after_prompt(self):
        self.assertIsNone(self._final([assistant("ancien"), user("q")]))

    def test_refuses_non_transcript_path(self):
        self.assertIsNone(A.final_from_transcript("/etc/passwd"))
        self.assertIsNone(A.final_from_transcript(None))

    def test_payload_answer_wins(self):
        self.assertEqual(A.final_answer({"last_assistant_message": "direct",
                                         "transcript_path": "/nope.jsonl"}), "direct")


if __name__ == "__main__":
    unittest.main()
