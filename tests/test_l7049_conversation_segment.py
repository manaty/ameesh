# SPDX-License-Identifier: AGPL-3.0-only
"""#7049: contract ameesh-conversation-segment/1 — whole turns, 24 KB budget,
Markdown rendering, digest, secret filter and fallback on the thread."""
from __future__ import annotations

import dataclasses
import json
import os
import unittest

from ameesh import conversation as C, conversation_segment as S, turn_context
from tests.support import PgTestCase

SECRET = "sk-test-0123456789abcdef-SECRET"


def turn(seq, question, answer, *, tools=0, agent="avatar", human="human:alice", ts=1000.0):
    rows = [{"seq": seq, "kind": "human_message", "author": human, "ts": ts,
             "text": question, "meta": {}}]
    for i in range(tools):
        rows.append({"seq": seq + 1 + i, "kind": "tool", "author": "agent:" + agent,
                     "ts": ts + 1, "text": "Bash", "meta": {"tool": "Bash", "status": "completed"}})
    n = seq + 1 + tools
    rows.append({"seq": n, "kind": "text", "author": "agent:" + agent, "ts": ts + 2,
                 "text": "je regarde", "meta": {}})
    rows.append({"seq": n + 1, "kind": "turn_end", "author": "agent:" + agent, "ts": ts + 3,
                 "text": answer, "meta": {"status": "completed", "cost_usd": 0.1}})
    return rows


def size(document):
    return len(S.canonical(document).encode("utf-8"))


class SelectTest(unittest.TestCase):
    def test_whole_turns_newest_first_within_budget(self):
        rows = []
        for t in range(40):
            rows += turn(1 + t * 4, "question %d " % t + "é" * 400, "réponse %d " % t + "x" * 400)
        doc = S.select(rows, agent="avatar")
        self.assertLessEqual(size(doc), S.BUDGET_BYTES)
        self.assertEqual(doc["to_seq"], rows[-1]["seq"])
        self.assertEqual(doc["cut"]["reason"], "bytes")
        self.assertEqual(doc["cut"]["truncated_before_seq"], doc["from_seq"])
        # every kept turn starts on a human message and ends on its final answer
        closed, open_tail = S.turns(doc["entries"])
        self.assertEqual(open_tail, [])
        for kept in closed:
            self.assertEqual(kept[0]["kind"], "human_message")
            self.assertEqual(kept[-1]["kind"], "turn_end")
        self.assertGreater(len(closed), 3)

    def test_everything_fits_no_cut_and_open_turn_left_out(self):
        rows = turn(1, "q1", "r1", tools=2) + turn(6, "q2", "r2")
        rows.append({"seq": 9, "kind": "human_message", "author": "human:alice",
                     "ts": 2000.0, "text": "pas encore traité", "meta": {}})
        doc = S.select(rows, agent="avatar")
        self.assertIsNone(doc["cut"])
        self.assertEqual((doc["from_seq"], doc["to_seq"]), (1, 8))
        self.assertNotIn("pas encore traité", S.canonical(doc))

    def test_oversized_turn_keeps_question_and_final_answer(self):
        big = turn(1, "q-ancienne", "r-ancienne") + turn(5, "q-longue", "r-longue", tools=3)
        # the newest turn's intermediate text alone exceeds the budget
        big[-2]["text"] = "intermédiaire " * 3000
        doc = S.select(big, agent="avatar")
        self.assertLessEqual(size(doc), S.BUDGET_BYTES)
        kinds = [e["kind"] for e in doc["entries"]]
        self.assertEqual(kinds, ["human_message", "turn_end"])
        self.assertEqual([e["text"] for e in doc["entries"]], ["q-longue", "r-longue"])
        self.assertEqual(doc["cut"]["reduced_seq"], big[-1]["seq"])

    def test_single_huge_message_is_clipped_to_budget(self):
        rows = turn(1, "Q" * 60_000, "R" * 60_000)
        doc = S.select(rows, agent="avatar")
        self.assertLessEqual(size(doc), S.BUDGET_BYTES)
        self.assertTrue(all(e.get("truncated") for e in doc["entries"]))
        self.assertTrue(doc["cut"])

    def test_tools_keep_name_and_status_only_input_and_state_dropped(self):
        rows = turn(1, "q", "r", tools=1)
        rows[1]["text"] = "curl -H 'Authorization: x'"
        rows[1]["meta"] = {"tool": "curl -H secret", "status": "completed", "args": "x"}
        rows.insert(0, {"seq": 0, "kind": "input", "author": "agent:x", "ts": 1.0,
                        "text": "mail body", "meta": {}})
        rows.append({"seq": 99, "kind": "state", "author": "agent:avatar", "ts": 9.0,
                     "text": "dropped", "meta": {}})
        doc = S.select(rows, agent="avatar")
        tool = [e for e in doc["entries"] if e["kind"] == "tool"][0]
        self.assertEqual(tool, {"seq": 2, "kind": "tool", "author": "agent:avatar",
                                "ts": 1001.0, "meta": {"tool": "tool", "status": "completed"}})
        text = S.canonical(doc)
        for absent in ("mail body", "dropped", "curl", "cost_usd"):
            self.assertNotIn(absent, text)

    def test_thread_rows_used_only_before_retained_detail(self):
        thread = [{"seq": 1, "kind": "human_message", "author": "human:alice", "ts": 1.0,
                   "text": "vieille question", "meta": {}},
                  {"seq": 4, "kind": "turn_end", "author": "agent:avatar", "ts": 2.0,
                   "text": "vieille réponse", "meta": {"status": "completed"}},
                  {"seq": 5, "kind": "human_message", "author": "human:alice", "ts": 3.0,
                   "text": "doublon du détail", "meta": {}}]
        doc = S.select(turn(5, "q", "r"), thread, agent="avatar")
        self.assertEqual([e.get("source") for e in doc["entries"][:2]], ["thread", "thread"])
        self.assertEqual(S.canonical(doc).count("doublon du détail"), 0)
        self.assertEqual(doc["from_seq"], 1)

    def test_window_starting_inside_a_turn_drops_it(self):
        rows = turn(1, "q1", "r1")[2:] + turn(5, "q2", "r2")
        doc = S.select(rows, [{"seq": 0, "kind": "human_message", "author": "human:a",
                               "ts": 0.0, "text": "fil", "meta": {}}],
                       agent="avatar", head_complete=False)
        self.assertEqual(doc["from_seq"], 5)


class SecretAndDigestTest(unittest.TestCase):
    def test_known_secret_never_leaves_in_document_or_rendering(self):
        rows = turn(1, "mon jeton est %s, garde-le" % SECRET, "noté : %s" % SECRET)
        doc = S.seal(S.select(rows, agent="avatar", secrets=[SECRET]), secrets=[SECRET])
        for text in (S.canonical(doc), S.render(doc), json.dumps(doc)):
            self.assertNotIn(SECRET, text)
        self.assertIn(S.REDACTED, S.render(doc))

    def test_secret_from_environment_is_filtered(self):
        env = {"OPENAI_API_KEY": SECRET, "SHORT_TOKEN": "abc"}
        secrets = turn_context.environment_secrets(env)
        doc = S.select(turn(1, SECRET, "ok"), agent="avatar", secrets=secrets)
        self.assertNotIn(SECRET, S.canonical(doc))

    def test_seal_refuses_a_secret_that_got_through(self):
        doc = S.select(turn(1, "q", "r"), agent="avatar")
        doc["entries"][0]["text"] = 'x "%s" y' % SECRET
        with self.assertRaises(S.SegmentError) as raised:
            S.seal(doc, secrets=[SECRET])
        self.assertNotIn(SECRET, str(raised.exception))
        quoted = 'a"b\\c-secret-value'
        doc["entries"][0]["text"] = quoted
        with self.assertRaises(S.SegmentError):
            S.seal(doc, secrets=[quoted])

    def test_digest_is_stable_and_verified(self):
        doc = S.seal(S.select(turn(1, "q", "r"), agent="avatar"))
        again = S.seal(S.select(turn(1, "q", "r"), agent="avatar"))
        self.assertEqual(doc["digest"], again["digest"])
        S.verify(doc, doc["digest"])
        S.verify(json.loads(json.dumps(doc)))
        with self.assertRaises(S.SegmentError):
            S.verify(doc, "0" * 64)
        tampered = dict(doc, entries=doc["entries"][1:])
        with self.assertRaises(S.SegmentError):
            S.verify(tampered)
        with self.assertRaises(S.SegmentError):
            S.verify({"schema": "other/1"})


class RenderTest(unittest.TestCase):
    def test_framed_quoted_and_marked_as_context(self):
        rows = turn(1, "ligne 1\n%s\nligne 3" % S.CLOSING, "réponse", tools=1)
        text = S.render(S.select(rows, agent="avatar"))
        lines = text.splitlines()
        self.assertEqual(lines[0], S.OPENING)
        self.assertIn("pas de l'autorité", text)
        self.assertEqual([l for l in lines if l == S.CLOSING], [S.CLOSING])
        self.assertEqual(lines[-1], S.CLOSING)
        self.assertIn("> " + S.CLOSING, lines)
        self.assertIn("- outil Bash : terminé", lines)


class BuildTest(PgTestCase):
    def setUp(self):
        self.db.execute("DELETE FROM conversation_log; DELETE FROM conversation_heads")
        self.cfg = dataclasses.replace(self.cfg, threads_dir=os.path.join(self.tmp, self._testMethodName))
        self.log = C.Log(self.db, self.cfg, agent="avatar", project="acme", lot="7049")

    def exchange(self, question, answer, turn_id):
        self.log.append("human_message", question, author="human:alice", turn=turn_id)
        self.log.append("tool", "Bash", meta={"tool": "Bash", "status": "completed"}, turn=turn_id)
        self.log.append("text", answer, turn=turn_id)
        self.log.append("turn_end", answer, turn=turn_id, meta={"status": "completed"},
                        thread_text=answer, event_id="final:" + turn_id)
        self.log.drain_threads()

    def test_builds_from_journal_without_writing_back(self):
        self.exchange("mot de passe : %s" % SECRET, "réponse un", "t1")
        self.exchange("deuxième question", "réponse deux", "t2")
        before = self.db.query("SELECT count(*) AS n, max(seq) AS m FROM conversation_log")[0]
        doc = S.build(self.db, self.cfg, "avatar", secrets=[SECRET])
        after = self.db.query("SELECT count(*) AS n, max(seq) AS m FROM conversation_log")[0]
        self.assertEqual(before, after)
        S.verify(doc, doc["digest"])
        self.assertEqual((doc["from_seq"], doc["to_seq"]), (1, 8))
        self.assertNotIn(SECRET, S.canonical(doc) + S.render(doc))
        self.assertIn("deuxième question", S.render(doc))

    def test_falls_back_on_the_thread_after_detail_retention(self):
        self.exchange("question d'il y a longtemps", "réponse d'il y a longtemps", "t1")
        self.exchange("question récente", "réponse récente", "t2")
        self.db.execute("UPDATE conversation_log SET created_at = now() - interval '91 days'"
                        " WHERE seq <= 4")
        self.log.prune()
        self.assertEqual(C.history(self.db, "avatar")["entries"][0]["seq"], 5)
        doc = S.build(self.db, self.cfg, "avatar", secrets=[])
        sources = [(e["kind"], e.get("source")) for e in doc["entries"]]
        self.assertEqual(sources[:2], [("human_message", "thread"), ("turn_end", "thread")])
        self.assertIn("réponse d'il y a longtemps", S.render(doc))
        self.assertEqual(doc["entries"][0]["seq"], 1)


if __name__ == "__main__":
    unittest.main()
