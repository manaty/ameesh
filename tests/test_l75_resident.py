# SPDX-License-Identifier: AGPL-3.0-only
"""L75: resident protocols (decision 0035), contract tests.

No real harness, no network, no cost: `tests/fake_resident_harness.py` speaks
Claude Code stream-json and Codex app-server as observed on the real
binaries, and `tests/fake_acp_agent.py` speaks ACP.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

from ameesh import adapters, harnesses, resident

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_RESIDENT = os.path.join(HERE, "fake_resident_harness.py")
FAKE_ACP = os.path.join(HERE, "fake_acp_agent.py")

CLAUDE_COMMAND = ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                  "--verbose", "--replay-user-messages"]


def document(ident: str, **ameesh) -> dict:
    return {"id": ident, "name": ident, "version": "1.0.0", "schema_version": "1",
            "description": "L75 test harness.",
            "ameesh": {"binary": sys.executable, **ameesh}}


def claude_doc(**resident_block) -> dict:
    block = {"protocol": "claude-stream-json", "launcher": [FAKE_RESIDENT],
             "command": CLAUDE_COMMAND, "new_session": ["--session-id"],
             "min_version": "2.1.286"}
    block.update(resident_block)
    return document("fake-claude", protocol="cli", stream="claude-stream-json",
                    command=["-p"], session={"flag": ["--resume"]},
                    model={"flag": ["--model", "{model}"]}, resident=block)


def codex_doc(**resident_block) -> dict:
    block = {"protocol": "codex-app-server", "launcher": [FAKE_RESIDENT],
             "command": ["app-server"],
             "thread": {"approvalPolicy": "never", "sandbox": "danger-full-access"},
             "min_version": "0.159.3"}
    block.update(resident_block)
    return document("fake-codex", protocol="cli", stream="codex-json", command=["exec"],
                    resident=block)


def acp_doc() -> dict:
    return document("fake-acp", protocol="acp", command=[FAKE_ACP],
                    resident={"protocol": "acp", "min_version": "1.0.0"})


def findings_of(doc: dict) -> list[harnesses.Finding]:
    return harnesses.validate(doc, path="test.json")


def codes(findings, severity=harnesses.ERROR) -> list[str]:
    return [f.code for f in findings if f.severity == severity]


class DescriptorBlockTest(unittest.TestCase):
    def test_packaged_descriptors_are_resident_and_pinned(self):
        descriptors, findings = harnesses.scan(host=tempfile.mkdtemp())
        self.assertEqual([str(f) for f in findings], [])
        self.assertEqual(descriptors["claude"].resident["protocol"], "claude-stream-json")
        self.assertIn("--replay-user-messages", descriptors["claude"].resident["command"])
        self.assertEqual(descriptors["claude"].resident["new_session"], ("--session-id",))
        self.assertEqual(descriptors["codex"].resident["protocol"], "codex-app-server")
        self.assertEqual(descriptors["codex"].resident["thread"]["approvalPolicy"], "never")
        dsh = descriptors["deepseek"].resident
        self.assertEqual((dsh["protocol"], dsh["launcher"], dsh["command"]),
                         ("acp", ("--profile", "acp"), ()))
        for ident in ("claude", "codex", "deepseek"):
            self.assertTrue(descriptors[ident].resident["min_version"], ident)

    def test_valid_blocks(self):
        for doc in (claude_doc(), codex_doc(), acp_doc()):
            self.assertEqual(codes(findings_of(doc)), [], doc["id"])

    def test_acp_harness_defaults_to_its_own_arguments(self):
        doc = acp_doc()
        doc["ameesh"]["launcher"] = ["--x"]
        built = harnesses._build(doc, "p", "test")
        self.assertEqual(built.resident["launcher"], ("--x",))
        self.assertEqual(built.resident["command"], (FAKE_ACP,))

    def test_invalid_blocks(self):
        cases = {
            "protocol": claude_doc(protocol="telnet"),
            "command": claude_doc(command=[]),
            "launcher": claude_doc(launcher="x"),
            "version": claude_doc(min_version="latest"),
            "thread": codex_doc(thread={"approvalPolicy": 3}),
        }
        for name, doc in cases.items():
            self.assertIn("harness-resident-invalid", codes(findings_of(doc)), name)
            self.assertEqual(harnesses._build(doc, "p", "test").resident, {}, name)
        not_object = claude_doc()
        not_object["ameesh"]["resident"] = ["claude-stream-json"]
        self.assertIn("harness-resident-invalid", codes(findings_of(not_object)))
        acp = acp_doc()
        acp["ameesh"]["resident"] = {"protocol": "claude-stream-json", "command": ["-p"]}
        self.assertIn("harness-resident-invalid", codes(findings_of(acp)))
        # a command-line harness resident in ACP names its ACP arguments
        cli = codex_doc()
        cli["ameesh"]["resident"] = {"protocol": "acp", "min_version": "1.0"}
        self.assertIn("harness-resident-invalid", codes(findings_of(cli)))

    def test_warnings(self):
        unpinned = claude_doc()
        del unpinned["ameesh"]["resident"]["min_version"]
        self.assertIn("harness-resident-unpinned",
                      codes(findings_of(unpinned), harnesses.WARNING))
        self.assertTrue(harnesses._build(unpinned, "p", "t").resident)
        stray = claude_doc(thread={"a": "b"}, extra=1)
        warnings = [f.message for f in findings_of(stray) if f.severity == harnesses.WARNING]
        self.assertTrue(any("thread" in w for w in warnings), warnings)
        self.assertTrue(any("extra" in w for w in warnings), warnings)
        self.assertEqual(codes(findings_of(stray)), [])

    def test_availability(self):
        built = harnesses._build(claude_doc(min_version="0.2.0-rc.2"), "p", "t")
        self.assertEqual(resident.availability(built, "0.2.0-rc.2"), "")
        self.assertEqual(resident.availability(built, "0.2.0"), "")
        self.assertEqual(resident.availability(built, "0.2.0-rc.10"), "")
        self.assertIn("below", resident.availability(built, "0.2.0-rc.1"))
        self.assertIn("below", resident.availability(built, "0.1.9"))
        self.assertIn("unreadable", resident.availability(built, None))
        unpinned = claude_doc()
        del unpinned["ameesh"]["resident"]["min_version"]
        self.assertEqual(resident.availability(harnesses._build(unpinned, "p", "t"), None), "")
        plain = document("plain", protocol="cli", stream="text")
        self.assertIn("no resident protocol",
                      resident.availability(harnesses._build(plain, "p", "t"), "9"))


class DriverCase(unittest.TestCase):
    """A driver on a fake harness, its trace and its events."""

    doc = staticmethod(claude_doc)

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="l75-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "trace.jsonl")
        self.events: list[tuple[dict, dict]] = []
        self.drivers: list[resident.ResidentDriver] = []

    def driver(self, doc: dict | None = None, *, env: dict | None = None,
               **kwargs) -> resident.ResidentDriver:
        doc = doc or self.doc()
        path = os.path.join(self.tmp, "%s.json" % doc["id"])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        descriptor = harnesses.load(path, source="test")
        adapter = adapters.HarnessAdapter(descriptor, sys.executable)
        variables = {"FAKE_RESIDENT_LOG": self.log, "FAKE_ACP_LOG": self.log}
        variables.update(env or {})
        driver = resident.driver_for(adapter, cwd=self.tmp, env=variables,
                                     on_event=lambda raw, parsed: self.events.append(
                                         (raw, parsed)), **kwargs)
        self.drivers.append(driver)
        self.addCleanup(driver.close, 0.5)
        return driver

    def trace(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def wait(self, turn: resident.Turn, timeout: float = 10.0) -> resident.TurnResult:
        result = turn.wait(timeout)
        self.assertIsNotNone(result, "turn still running after %ss" % timeout)
        return result

    def until(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            time.sleep(0.02)


class ClaudeStreamJsonTest(DriverCase):
    def test_new_session_id_model_and_handshake(self):
        driver = self.driver(model="fake-model-x")
        session = driver.start()
        self.assertTrue(session)
        argv = self.trace()[0]["argv"]
        self.assertEqual(argv[argv.index("--session-id") + 1], session)
        self.assertEqual(argv[argv.index("--model") + 1], "fake-model-x")
        self.assertNotIn("--resume", argv)
        self.assertIn({"control": "initialize"}, self.trace())
        self.assertEqual(driver.pgid, driver.pid)
        self.assertEqual(os.getpgid(driver.pid), driver.pid)

    def test_turns_usage_and_cost_per_turn(self):
        driver = self.driver()
        driver.start()
        first = self.wait(driver.send("hello", input_id="in-1"))
        self.assertEqual(first.status, resident.COMPLETED)
        self.assertEqual(first.final, "ECHO:hello")
        self.assertEqual(first.inputs, ("in-1",))
        self.assertEqual(first.usage["input_tokens"], 10)
        self.assertEqual(first.usage["cache_read_input_tokens"], 200)
        self.assertEqual(first.usage["cache_creation_input_tokens"], 30)
        self.assertEqual(first.usage["context_tokens"], 240)
        self.assertAlmostEqual(first.cost, 0.01)
        second = self.wait(driver.send("again"))
        self.assertAlmostEqual(second.cost, 0.01)  # cumulative 0.02, this turn 0.01
        self.assertAlmostEqual(second.cost_total, 0.02)
        self.assertFalse(driver.busy)
        finals = [p.get("final") for _raw, p in self.events if "final" in p]
        self.assertEqual(finals, ["ECHO:hello", "ECHO:again"])

    def test_resume_session_and_cost_base(self):
        driver = self.driver(session="sess-old")
        self.assertEqual(driver.start(), "sess-old")
        argv = self.trace()[0]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-old")
        self.assertNotIn("--session-id", argv)
        result = self.wait(driver.send("hi"))
        self.assertIsNone(result.cost)  # base of the resumed session unknown
        self.assertAlmostEqual(result.cost_total, 0.51)
        self.assertAlmostEqual(self.wait(driver.send("hi")).cost, 0.01)
        known = self.driver(session="sess-old", cost_base=0.5)
        known.start()
        self.assertAlmostEqual(self.wait(known.send("hi")).cost, 0.01)

    def test_busy_then_steer_joins_the_turn(self):
        driver = self.driver()
        driver.start()
        turn = driver.send("SLOW first", input_id="a")
        with self.assertRaises(resident.ResidentBusy):
            driver.send("refused")
        self.assertIs(driver.steer("steered", input_id="b"), turn)
        result = self.wait(turn)
        self.assertEqual(result.status, resident.COMPLETED)
        self.assertEqual(result.inputs, ("a", "b"))
        self.assertEqual(result.final, "ECHO:SLOW first\nECHO:steered")
        self.assertIn({"user": "steered", "injected": True}, self.trace())

    def test_steer_read_after_the_result_stays_in_the_logical_turn(self):
        driver = self.driver(env={"FAKE_CLAUDE_QUEUE": "1"})
        driver.start()
        turn = driver.send("SLOW first", input_id="a")
        driver.steer("late", input_id="b")
        result = self.wait(turn)
        self.assertEqual(result.status, resident.COMPLETED)
        self.assertEqual(result.inputs, ("a", "b"))
        self.assertEqual(result.final, "ECHO:late")
        self.assertAlmostEqual(result.cost, 0.02)  # two harness turns
        self.assertEqual(result.usage["input_tokens"], 20)
        self.assertEqual(len([p for _r, p in self.events if "final" in p]), 2)

    def test_steer_outside_a_turn_is_a_send(self):
        driver = self.driver()
        driver.start()
        result = self.wait(driver.steer("alone", input_id="x"))
        self.assertEqual((result.final, result.inputs), ("ECHO:alone", ("x",)))

    def test_interrupt_without_killing(self):
        driver = self.driver()
        driver.start()
        self.assertFalse(driver.interrupt())  # no turn
        turn = driver.send("SLOW long")
        time.sleep(0.1)
        self.assertTrue(driver.interrupt())
        result = self.wait(turn)
        self.assertEqual(result.status, resident.INTERRUPTED)
        self.assertIsNone(result.error)
        self.assertTrue(driver.alive)
        self.assertEqual(self.wait(driver.send("next")).final, "ECHO:next")

    def test_failed_turn(self):
        driver = self.driver()
        driver.start()
        result = self.wait(driver.send("ERROR please"))
        self.assertEqual((result.status, result.error), (resident.FAILED, "boom"))
        self.assertTrue(driver.alive)

    def test_harness_requests_are_refused(self):
        driver = self.driver()
        driver.start()
        result = self.wait(driver.send("ASK something"))
        self.assertIn("asked:error", result.final)

    def test_harness_exit_fails_the_turn(self):
        driver = self.driver()
        driver.start()
        result = self.wait(driver.send("CRASH now"))
        self.assertEqual(result.status, resident.FAILED)
        self.assertIn("exited (code 3)", result.error)
        self.until(lambda: not driver.alive)
        with self.assertRaises(resident.ResidentError):
            driver.send("after")
        self.assertFalse(driver.probe())

    def test_close_stops_the_group(self):
        driver = self.driver()
        driver.start()
        turn = driver.send("SLOW long")
        pid = driver.pid
        driver.close(0.5)
        self.assertEqual(self.wait(turn).status, resident.FAILED)
        self.assertFalse(driver.alive)
        self.assertIsNotNone(driver.proc.poll())
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)

    def test_start_timeout(self):
        driver = self.driver(env={"FAKE_START_DELAY": "3"}, init_timeout=0.3)
        with self.assertRaises(resident.ResidentError):
            driver.start()

    def test_wrong_protocol_refused(self):
        with self.assertRaises(resident.ResidentError):
            resident.CodexAppServer(adapters.HarnessAdapter(
                harnesses._build(claude_doc(), "p", "t"), sys.executable), cwd=self.tmp)
        plain = harnesses._build(document("plain", protocol="cli", stream="text"), "p", "t")
        with self.assertRaises(resident.ResidentError):
            resident.driver_for(adapters.HarnessAdapter(plain, sys.executable), cwd=self.tmp)


class CodexAppServerTest(DriverCase):
    doc = staticmethod(codex_doc)

    def methods(self, name: str) -> list[dict]:
        return [t["params"] for t in self.trace() if t.get("method") == name]

    def test_start_thread_without_jsonrpc_member(self):
        driver = self.driver(model="gpt-x", tier="flex")
        self.assertEqual(driver.start(), "thr-new")
        params = self.methods("thread/start")[0]
        self.assertEqual(params["cwd"], self.tmp)
        self.assertEqual((params["approvalPolicy"], params["sandbox"]),
                         ("never", "danger-full-access"))
        self.assertEqual((params["model"], params["serviceTier"]), ("gpt-x", "flex"))
        self.assertTrue(any(t.get("method") == "initialized" for t in self.trace()))
        self.assertFalse([t for t in self.trace() if "error" in t and t.get("error")])
        self.assertEqual(self.events[0][1], {"session": "thr-new", "model": "gpt-x"})

    def test_resume_without_history(self):
        driver = self.driver(session="thr-old")
        self.assertEqual(driver.start(), "thr-old")
        params = self.methods("thread/resume")[0]
        self.assertEqual((params["threadId"], params["excludeTurns"]), ("thr-old", True))

    def test_turn_usage_and_effort(self):
        driver = self.driver(effort="low")
        driver.start()
        result = self.wait(driver.send("hello", input_id="i"))
        self.assertEqual(result.status, resident.COMPLETED)
        self.assertEqual(result.final, "ECHO:hello")
        self.assertEqual(result.usage["input_tokens"], 100)  # cache counted apart
        self.assertEqual(result.usage["cache_read_input_tokens"], 200)
        self.assertEqual(result.usage["output_tokens"], 9)
        self.assertEqual(result.usage["context_tokens"], 300)
        self.assertEqual(result.usage["context_window"], 258400)
        self.assertIsNone(result.cost)
        self.assertEqual(self.methods("turn/start")[0]["effort"], "low")
        self.assertTrue(any(p.get("safe_point") for _r, p in self.events))

    def test_steer_in_the_turn(self):
        driver = self.driver()
        driver.start()
        turn = driver.send("SLOW first", input_id="a")
        with self.assertRaises(resident.ResidentBusy):
            driver.send("refused")
        driver.steer("steered", input_id="b")
        result = self.wait(turn)
        self.assertEqual(result.inputs, ("a", "b"))
        self.assertEqual(result.final, "ECHO:SLOW first\nECHO:steered")
        self.assertEqual(self.methods("turn/steer")[0]["expectedTurnId"], "turn-1")
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_refused_steer_follows_in_the_same_logical_turn(self):
        driver = self.driver(env={"FAKE_CODEX_STEER_REFUSE": "1"})
        driver.start()
        turn = driver.send("SLOW first", input_id="a")
        driver.steer("follow", input_id="b")
        result = self.wait(turn)
        self.assertEqual(result.status, resident.COMPLETED)
        self.assertEqual(result.inputs, ("a", "b"))
        self.assertEqual(result.final, "ECHO:follow")
        self.assertEqual([p["input"][0]["text"] for p in self.methods("turn/start")],
                         ["SLOW first", "follow"])
        self.assertEqual(result.usage["output_tokens"], 18)

    def test_interrupt(self):
        driver = self.driver()
        driver.start()
        turn = driver.send("SLOW long")
        self.assertTrue(driver.interrupt())
        result = self.wait(turn)
        self.assertEqual(result.status, resident.INTERRUPTED)
        self.assertEqual(self.methods("turn/interrupt")[0]["turnId"], "turn-1")
        self.assertEqual(self.wait(driver.send("next")).final, "ECHO:next")

    def test_failed_turn(self):
        driver = self.driver()
        driver.start()
        result = self.wait(driver.send("ERROR"))
        self.assertEqual((result.status, result.error), (resident.FAILED, "boom"))

    def test_approval_declined(self):
        driver = self.driver()
        driver.start()
        result = self.wait(driver.send("ASK"))
        self.assertIn('asked:{"decision": "decline"}', result.final)
        self.assertTrue(any("[permission refused]" in (p.get("display") or [""])[0]
                            for _r, p in self.events))

    def test_probe_exit_and_close(self):
        driver = self.driver()
        driver.start()
        self.assertTrue(driver.probe())
        result = self.wait(driver.send("CRASH"))
        self.assertEqual(result.status, resident.FAILED)
        self.assertIn("exited", result.error)
        self.until(lambda: not driver.alive)
        self.assertFalse(driver.probe())


class AcpResidentTest(DriverCase):
    doc = staticmethod(acp_doc)

    def methods(self, name: str) -> list[dict]:
        return [t for t in self.trace() if t.get("method") == name]

    def test_several_prompts_on_one_session(self):
        driver = self.driver(env={"FAKE_ACP_MODE": "normal"})
        self.assertEqual(driver.start(), "sess-fake-1")
        first = self.wait(driver.send("one"))
        second = self.wait(driver.send("two"))
        for result in (first, second):
            self.assertEqual(result.status, resident.COMPLETED)
            self.assertEqual(result.final, "bonjour")
            self.assertEqual(result.usage["context_tokens"], 1234)
            self.assertEqual(result.usage["context_window"], 200000)
            self.assertAlmostEqual(result.cost, 0.02)
        self.assertEqual(len(self.methods("initialize")), 1)
        self.assertEqual(len(self.methods("session/new")), 1)
        self.assertEqual([t["text"] for t in self.methods("session/prompt")], ["one", "two"])
        self.assertTrue(driver.probe())  # session/list unknown: it answered

    def test_resume(self):
        driver = self.driver(env={"FAKE_ACP_MODE": "resume"}, session="sess-9")
        self.assertEqual(driver.start(), "sess-9")
        self.assertEqual(len(self.methods("session/resume")), 1)

    def test_steer_is_cancel_then_prompt(self):
        driver = self.driver(env={"FAKE_ACP_MODE": "hang_once"})
        driver.start()
        turn = driver.send("long", input_id="a")
        with self.assertRaises(resident.ResidentBusy):
            driver.send("refused")
        self.until(lambda: self.methods("session/prompt"))
        driver.steer("urgent", input_id="b")
        result = self.wait(turn)
        self.assertEqual(result.status, resident.COMPLETED)
        self.assertEqual(result.inputs, ("a", "b"))
        self.assertEqual(len(self.methods("session/cancel")), 1)
        self.assertEqual([t["text"] for t in self.methods("session/prompt")],
                         ["long", "urgent"])

    def test_interrupt(self):
        driver = self.driver(env={"FAKE_ACP_MODE": "hang"})
        driver.start()
        turn = driver.send("long")
        self.until(lambda: self.methods("session/prompt"))
        self.assertTrue(driver.interrupt())
        result = self.wait(turn)
        self.assertEqual(result.status, resident.INTERRUPTED)
        self.assertTrue(driver.alive)

    def test_exit_fails_the_turn(self):
        driver = self.driver(env={"FAKE_ACP_MODE": "exit"})
        driver.start()
        result = self.wait(driver.send("bye"))
        self.assertEqual(result.status, resident.FAILED)
        self.until(lambda: not driver.alive)


if __name__ == "__main__":
    unittest.main()
