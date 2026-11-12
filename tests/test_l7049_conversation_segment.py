# SPDX-License-Identifier: AGPL-3.0-only
"""#7049: contract ameesh-conversation-segment/1 — whole turns, 24 KB budget,
Markdown rendering, digest, secret filter and fallback on the thread."""
from __future__ import annotations

import dataclasses
import json
import os
import unittest
import unittest.mock

from ameesh import conversation as C, conversation_segment as S, fil, storage, turn_context
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


def thread_entry(seq, author, text, *, agent="avatar"):
    return fil.Entry(author=author, recipient="agent:" + agent, text=text, ts=1000.0,
                     meta={"conversation": {"agent": agent, "seq": seq, "turn": "t"}})


class FakeStore:
    """Minimal `Conversations` page reader, for `_turn_head` without a database."""

    def __init__(self, rows):
        self.rows = rows

    def read(self, agent, *, after=0, limit=100):
        return {"entries": [r for r in self.rows if int(r["seq"]) > after][:limit]}


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
        sealed = S.seal(doc)
        self.assertEqual(size(sealed), size(doc) + S.DIGEST_BYTES)
        self.assertLessEqual(size(sealed), S.BUDGET_BYTES)

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

    def test_partial_head_turn_is_completed_from_the_thread_open_tail(self):
        # the window starts on seq 3 (the previous turn's final answer): the
        # question of the turn that ends at seq 3 is only in the thread.
        rows = turn(1, "q1", "r1")[2:] + turn(5, "q2", "r2")
        thread = [{"seq": 0, "kind": "human_message", "author": "human:a",
                   "ts": 0.0, "text": "fil", "meta": {}}]
        doc = S.select(rows, thread, agent="avatar", head_complete=False)
        self.assertEqual([e["seq"] for e in doc["entries"]], [0, 3, 5, 6, 7])
        self.assertEqual(doc["entries"][0]["source"], "thread")
        self.assertEqual(doc["cut"]["reduced_seq"], 3)

    def test_newest_turn_longer_than_the_read_window_is_kept(self):
        # review B (mty-amh-qa-01): 1 human + 498 text + 1 turn_end
        rows = [{"seq": 1, "kind": "human_message", "author": "human:alice", "ts": 1.0,
                 "text": "question", "meta": {}}]
        rows += [{"seq": n, "kind": "text", "author": "agent:x", "ts": 2.0,
                  "text": "pas %d" % n, "meta": {}} for n in range(2, 500)]
        rows.append({"seq": 500, "kind": "turn_end", "author": "agent:x", "ts": 3.0,
                     "text": "finale", "meta": {"status": "completed"}})
        for complete in (True, False):
            doc = S.select(rows, agent="x", head_complete=complete)
            self.assertEqual([(e["seq"], e["text"]) for e in doc["entries"]],
                             [(1, "question"), (500, "finale")])
            self.assertEqual(doc["cut"], {"truncated_before_seq": 1, "reason": "bytes",
                                          "reduced_seq": 500})


class ThreadRowsTest(unittest.TestCase):
    def test_thread_rows_dedupe_replayed_delivery(self):
        rows = S.thread_rows([
            thread_entry(4, "agent:avatar", "réponse"),
            thread_entry(4, "agent:avatar", "réponse"),
            thread_entry(1, "human:alice", "question"),
        ], "avatar")
        self.assertEqual([r["seq"] for r in rows], [4, 1])
        self.assertEqual(rows[0]["kind"], "turn_end")


class TurnHeadTest(unittest.TestCase):
    def test_first_entry_is_a_whole_turn(self):
        store = FakeStore([{"seq": 1, "kind": "human_message"}])
        self.assertEqual(S._turn_head(store, "avatar", 1), ([], True))

    def test_reads_past_ten_thousand_entries(self):
        rows = [{"seq": 1, "kind": "human_message", "text": "question",
                 "author": "human:a", "ts": 1.0, "meta": {}}]
        rows += [{"seq": n, "kind": "text", "text": "t", "author": "agent:a",
                  "ts": 1.0, "meta": {}} for n in range(2, 10_051)]
        messages, whole = S._turn_head(FakeStore(rows), "a", 10_050)
        self.assertEqual([m["seq"] for m in messages], [1])
        self.assertFalse(whole)


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

    def test_select_filters_environment_secrets_by_default(self):
        with unittest.mock.patch.dict(os.environ, {"OPENAI_API_KEY": SECRET}, clear=True):
            doc = S.select(turn(1, SECRET, "ok"), agent="avatar")
        self.assertNotIn(SECRET, S.canonical(doc))
        self.assertIn(S.REDACTED, S.canonical(doc))

    def test_seal_filters_environment_secrets_by_default(self):
        doc = S.select(turn(1, "q", "r"), agent="avatar", secrets=[])
        doc["entries"][0]["text"] = "x %s y" % SECRET
        with unittest.mock.patch.dict(os.environ, {"OPENAI_API_KEY": SECRET}, clear=True):
            with self.assertRaises(S.SegmentError):
                S.seal(doc)

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
        for missing in ({"schema": S.SCHEMA}, dict(doc, digest=None),
                        dict(doc, digest="0" * 63), dict(doc, digest="Z" * 64)):
            with self.assertRaises(S.SegmentError):
                S.verify(missing)


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
        self.cfg = dataclasses.replace(
            self.cfg, threads_dir=os.path.join(self.tmp, self._testMethodName),
            project="acme")
        storage.of(self.db).agents.upsert(
            "avatar", chantier="acme-team", harness="deepseek", host="test",
            cwd=self.tmp, session_id=None, status="idle", status_text="",
            model=None, budget_usd=None)
        # the real recorder writes assistant turns to the agent's project
        # thread (lot=None) and human messages to their lot's thread.
        self.log = C.Log(self.db, self.cfg, agent="avatar", project="acme-team")
        self.humans = C.Log(self.db, self.cfg, agent="avatar", project="acme", lot="7049")

    def exchange(self, question, answer, turn_id):
        self.humans.append("human_message", question, author="human:alice", turn=turn_id)
        self.log.append("tool", "Bash", meta={"tool": "Bash", "status": "completed"}, turn=turn_id)
        self.log.append("text", answer, turn=turn_id)
        self.log.append("turn_end", answer, turn=turn_id, meta={"status": "completed"},
                        thread_text=answer, event_id="final:" + turn_id)
        self.humans.drain_threads()
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

    def test_turn_longer_than_the_read_window_keeps_question_and_answer(self):
        self.exchange("ancienne question", "ancienne réponse", "t0")
        self.humans.append("human_message", "longue question", author="human:alice", turn="t1")
        for n in range(S.READ_LIMIT + 100):
            self.log.append("tool", "Bash", meta={"tool": "Bash", "status": "completed"},
                            turn="t1")
        self.log.append("turn_end", "longue réponse", turn="t1", meta={"status": "completed"},
                        thread_text="longue réponse", event_id="final:t1")
        self.humans.drain_threads()
        self.log.drain_threads()
        last = self.db.query("SELECT max(seq) AS m FROM conversation_log")[0]["m"]
        doc = S.build(self.db, self.cfg, "avatar", secrets=[])
        self.assertEqual([(e["kind"], e["text"]) for e in doc["entries"]],
                         [("human_message", "longue question"), ("turn_end", "longue réponse")])
        self.assertEqual(doc["cut"], {"truncated_before_seq": 5, "reason": "bytes",
                                      "reduced_seq": last})
        S.verify(doc, doc["digest"])

    def test_prune_in_the_middle_of_a_full_window_keeps_the_question(self):
        self.exchange("ancienne question", "ancienne réponse", "t0")
        self.humans.append("human_message", "longue question", author="human:alice",
                           turn="t1")
        for n in range(700):
            self.log.append("tool", "Bash", meta={"tool": "Bash", "status": "completed"},
                            turn="t1")
        self.log.append("turn_end", "longue réponse", turn="t1", meta={"status": "completed"},
                        thread_text="longue réponse", event_id="final:t1")
        self.humans.drain_threads()
        self.log.drain_threads()
        self.db.execute("UPDATE conversation_log SET created_at = now() - interval '91 days'"
                        " WHERE seq <= 200")
        self.log.prune()
        self.assertEqual(C.history(self.db, "avatar")["entries"][0]["seq"], 201)
        last = self.db.query("SELECT max(seq) AS m FROM conversation_log")[0]["m"]
        doc = S.build(self.db, self.cfg, "avatar", secrets=[])
        self.assertEqual([(e["kind"], e["text"]) for e in doc["entries"]],
                         [("human_message", "longue question"),
                          ("turn_end", "longue réponse")])
        self.assertEqual(doc["entries"][0]["source"], "thread")
        self.assertEqual(doc["cut"], {"truncated_before_seq": 5, "reason": "bytes",
                                      "reduced_seq": last})
        S.verify(doc, doc["digest"])

    def test_full_window_on_a_turn_boundary_keeps_whole_turns(self):
        for n in range(S.READ_LIMIT // 4 + 3):
            self.exchange("q%d" % n, "r%d" % n, "t%d" % n)
        doc = S.build(self.db, self.cfg, "avatar", secrets=[])
        closed, open_tail = S.turns(doc["entries"])
        self.assertEqual(open_tail, [])
        self.assertTrue(all(len(t) == 4 for t in closed))
        self.assertEqual(doc["entries"][-1]["text"], "r%d" % (S.READ_LIMIT // 4 + 2))

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

    def test_falls_back_on_the_thread_after_full_detail_retention(self):
        # review N: human messages live in the lot thread, assistant turns in
        # the project thread; after a full prune only the thread names them.
        self.exchange("question d'il y a longtemps", "réponse d'il y a longtemps", "t1")
        self.exchange("question récente", "réponse récente", "t2")
        self.db.execute("UPDATE conversation_log SET created_at = now() - interval '91 days'")
        self.log.prune()
        self.assertEqual(C.history(self.db, "avatar")["entries"], [])
        doc = S.build(self.db, self.cfg, "avatar", secrets=[])
        self.assertEqual(
            [(e["kind"], e.get("source"), e["text"]) for e in doc["entries"]],
            [("human_message", "thread", "question d'il y a longtemps"),
             ("turn_end", "thread", "réponse d'il y a longtemps"),
             ("human_message", "thread", "question récente"),
             ("turn_end", "thread", "réponse récente")])
        self.assertEqual((doc["from_seq"], doc["to_seq"]), (1, 8))
        self.assertIsNone(doc["cut"])
        S.verify(doc, doc["digest"])


if __name__ == "__main__":
    unittest.main()
