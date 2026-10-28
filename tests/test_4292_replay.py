# SPDX-License-Identifier: AGPL-3.0-only
"""#4292 — banc de rejeu comparatif : sélection, base, isolation, mesures,
relecture à l'aveugle et conclusion."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from ameesh import cost
from ameesh import replay

from . import support as _support  # noqa: F401 — isole l'environnement du poste


def _run(argv, *, cwd=None):
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("%s: %s" % (" ".join(argv), proc.stderr[-2000:]))
    return proc.stdout


def _init_repo(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=root)
    _run(["git", "-C", root, "config", "user.name", "test"], cwd=root)
    _run(["git", "-C", root, "config", "user.email", "test@invalid"], cwd=root)


def _commit(root: str, files: dict, message: str) -> str:
    for path, content in files.items():
        target = os.path.join(root, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
    _run(["git", "-C", root, "add", "-A"], cwd=root)
    _run(["git", "-C", root, "commit", "-q", "-m", message], cwd=root)
    return _run(["git", "-C", root, "rev-parse", "HEAD"], cwd=root).strip()


def _repo(tmp: str):
    root = os.path.join(tmp, "repo")
    _init_repo(root)
    base = _commit(root, {"README.md": "base\n", "tests/test_ref.py": "ref = 1\n"},
                   "base")
    return root, base


class BaseOfTest(unittest.TestCase):
    def test_merge_introduction_matches_the_reviewed_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "repo")
            _init_repo(root)
            base = _commit(root, {"a.txt": "a\n"}, "base")
            _run(["git", "-C", root, "checkout", "-q", "-b", "lot/42"], cwd=root)
            frozen = _commit(root, {"lot.txt": "lot\n"}, "#42: le lot")
            _run(["git", "-C", root, "checkout", "-q", "main"], cwd=root)
            _commit(root, {"main.txt": "m\n"}, "main after base")
            _run(["git", "-C", root, "merge", "-q", "--no-ff", "lot/42",
                  "-m", "merge #42"], cwd=root)
            self.assertEqual(replay.base_of(root, 42, frozen, []), (base, "merge-introduction"))
            # la fusion passe avant une plage de correction (le risque #4171)
            marks = [{"kind": "verdict", "at_ts": 10,
                      "note": "diff %s..%s lu" % (base[:12], frozen[:12])}]
            self.assertEqual(replay.base_of(root, 42, frozen, marks),
                             (base, "merge-introduction"))

    def test_subject_walk_without_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "repo")
            _init_repo(root)
            base = _commit(root, {"a.txt": "a\n"}, "base")
            frozen = _commit(root, {"lot.txt": "lot\n"}, "#7: le lot")
            _commit(root, {"after.txt": "after\n"}, "after")
            self.assertEqual(replay.base_of(root, 7, frozen, []),
                             (base, "subject-walk"))
            # sans fusion, une plage relue fait foi
            marks = [{"kind": "verdict", "at_ts": 10,
                      "note": "diff %s..%s lu" % (base[:12], frozen[:12])}]
            self.assertEqual(replay.base_of(root, 7, frozen, marks),
                             (base, "reviewed-range"))

    def test_unknown_head_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _base = _repo(tmp)
            with self.assertRaises(replay.ReplayError):
                replay.base_of(root, 1, "f" * 40, [])


class PickBenchTest(unittest.TestCase):
    def _lot(self, ident, klass):
        return replay.BenchLot(lot=ident, title="t", klass=klass, app="p",
                               repo="/tmp/r", target="main", base="a" * 40,
                               frozen="b" * 40, body="")

    def test_one_per_class_then_fill(self):
        got = replay.pick_bench([self._lot(1, "sensitive"), self._lot(2, "light"),
                                 self._lot(3, "light"), self._lot(4, "normal")])
        self.assertEqual([lot.lot for lot in got], [1, 2, 4])

    def test_missing_class_takes_the_next_lot(self):
        got = replay.pick_bench([self._lot(1, "sensitive"), self._lot(2, "light"),
                                 self._lot(3, "light")])
        self.assertEqual([lot.lot for lot in got], [1, 2, 3])

    def test_never_more_than_the_candidates(self):
        got = replay.pick_bench([self._lot(1, "light")])
        self.assertEqual([lot.lot for lot in got], [1])


class HelpersTest(unittest.TestCase):
    def test_test_paths_and_modules(self):
        self.assertTrue(replay.is_test_path("tests/test_x.py"))
        self.assertTrue(replay.is_test_path("web/src/a.test.ts"))
        self.assertTrue(replay.is_test_path("e2e/login.py"))
        self.assertFalse(replay.is_test_path("src/ameesh/replay.py"))
        self.assertEqual(replay.python_module("tests/test_x.py"), "test_x")
        self.assertEqual(replay.python_module("tests/sub/test_x.py"), "sub.test_x")
        self.assertIsNone(replay.python_module("docs/test_x.py"))

    def test_safe_relative_path_refuses_escapes(self):
        for value in ("", "/etc/passwd", "../x", "a/../b", "-rf"):
            with self.assertRaises(replay.ReplayError):
                replay._safe_rel(value)
        self.assertEqual(replay._safe_rel("tests/a.py"), "tests/a.py")

    def test_strip_hook_inserts(self):
        text = textwrap.dedent("""\
            - id: agent-default-model
              name: "@deepseek-ai/dsh-agent-default-model"
            - insert:
                - id: hooks-claude-code
                  name: "@deepseek-ai/dsh-hooks-claude-code"
                  config:
                    configPath: /home/x/.dsh/hooks.json
            """)
        cleaned = replay.strip_hook_inserts(text)
        self.assertIn("agent-default-model", cleaned)
        self.assertNotIn("hooks-claude-code", cleaned)
        self.assertNotIn("hooks.json", cleaned)


class IsolationTest(unittest.TestCase):
    def test_materialize_has_a_single_commit_and_no_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = _repo(tmp)
            _commit(root, {"later.txt": "later\n"}, "later")
            tree = os.path.join(tmp, "tree")
            info = replay.materialize(root, base, tree)
            self.assertIsNone(info["remote"])
            self.assertEqual(info["commits"], 1)
            self.assertEqual(_run(["git", "-C", tree, "remote"]).strip(), "")
            self.assertTrue(os.path.isfile(os.path.join(tree, "README.md")))
            self.assertFalse(os.path.exists(os.path.join(tree, "later.txt")))
            self.assertEqual(len(_run(["git", "-C", tree, "rev-list", "HEAD"]).split()), 1)
            with self.assertRaises(replay.ReplayError):
                replay.materialize(root, base, tree)

    def test_isolated_dsh_home_drops_hooks_and_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "dsh")
            profile = os.path.join(source, "profiles", "agent")
            os.makedirs(profile)
            with open(os.path.join(profile, "cordis.yml"), "w", encoding="utf-8") as fh:
                fh.write("- id: agent-default-model\n"
                         "- insert:\n    - id: hooks-claude-code\n"
                         "      name: \"@deepseek-ai/dsh-hooks-claude-code\"\n")
            with open(os.path.join(source, "hooks.json"), "w", encoding="utf-8") as fh:
                fh.write('{"hooks": {"Stop": []}}\n')
            os.makedirs(os.path.join(source, "sessions"))
            dest = os.path.join(tmp, "isolated")
            info = replay.isolated_dsh_home(source, dest)
            self.assertEqual(info["hooks"], "stripped")
            with open(os.path.join(dest, "profiles", "agent", "cordis.yml"),
                      encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("agent-default-model", text)
            self.assertNotIn("hooks", text.lower())
            self.assertFalse(os.path.exists(os.path.join(dest, "sessions")))
            with open(os.path.join(dest, "hooks.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"hooks": {}})

    def test_missing_dsh_profile_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dsh"))
            with self.assertRaises(replay.ReplayError):
                replay.isolated_dsh_home(os.path.join(tmp, "dsh"),
                                         os.path.join(tmp, "dest"))


class EnvironmentTest(unittest.TestCase):
    def test_state_is_stripped_and_key_comes_from_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = os.path.join(tmp, "env")
            with open(env_file, "w", encoding="utf-8") as fh:
                fh.write("DEEPSEEK_API_KEY=secret-key\n")
            env = replay.harness_env(
                "deepseek", env_file=env_file,
                source={"PATH": "/bin", "AMEESH_DSN": "pg://prod", "DEEPSEEK_API_KEY": ""})
            self.assertEqual(env["DEEPSEEK_API_KEY"], "secret-key")
            self.assertNotIn("AMEESH_DSN", env)
            self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file")

    def test_missing_key_is_an_actionable_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = os.path.join(tmp, "env")
            open(env_file, "w", encoding="utf-8").close()
            with self.assertRaises(replay.ReplayError) as caught:
                replay.harness_env("deepseek", env_file=env_file,
                                   source={"PATH": "/bin"})
            self.assertIn("DEEPSEEK_API_KEY", str(caught.exception))

    def test_isolate_env_points_the_mailbox_and_the_database_away(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = replay.isolate_env({"PATH": "/bin"}, os.path.join(tmp, "iso"))
            for name in ("AGENT_MAIL_CONFIG", "AGENT_MAIL_STATE", "AMEESH_CONFIG",
                         "AMEESH_STATE"):
                self.assertTrue(env[name].startswith(tmp), name)
            self.assertIn("127.0.0.1:1", env["AMEESH_DSN"])


class _FakeAdapter:
    """Un adaptateur minimal : la commande exécute un script, le flux est du
    JSON que `parse` réduit comme le vrai lecteur dsh."""

    def __init__(self, script: str):
        self.binary = script
        self.resolution = None

    def command(self, prompt, session_id=None, model=None, effort=None,
                patch=None, tier=None):
        self.patch = patch
        if patch:
            with open(patch, "w", encoding="utf-8") as fh:
                fh.write("model: %s\neffort: %s\n" % (model or "", effort or ""))
        return [sys.executable, self.binary, patch or "", prompt]

    def parse(self, line):
        event = json.loads(line)
        out = {}
        if event.get("type") == "status":
            out["usage"] = event["usage"]
        elif event.get("type") == "final":
            out["final"] = event.get("text") or ""
            if event.get("cost_usd") is not None:
                out["cost"] = event["cost_usd"]
        return out


def _harness_script(tmp: str, *, sleep: float = 0) -> str:
    path = os.path.join(tmp, "fake_harness.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent("""\
            import json, os, sys, time
            time.sleep(%s)
            print(json.dumps({"type": "status", "phase": "step_end",
                              "usage": {"inputTokens": 1000, "cacheReadTokens": 2000000,
                                        "outputTokens": 100}}), flush=True)
            print(json.dumps({"type": "final", "text": "done", "cost_usd": 0.5}),
                  flush=True)
            with open("touched.txt", "w", encoding="utf-8") as fh:
                fh.write("x")
            """) % sleep)
    return path


class RunHarnessTest(unittest.TestCase):
    def test_usage_duration_and_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "tree")
            os.makedirs(tree)
            adapter = _FakeAdapter(_harness_script(tmp))
            prices = cost.load_prices(None)
            prices["test-model"] = [1.0, 2.0, 3.0]
            result = replay.run_harness(
                adapter, "prompt", run_dir=os.path.join(tmp, "meta"), cwd=tree,
                env=dict(os.environ), harness="deepseek", model="test-model",
                effort="high", timeout=20, prices=prices)
            self.assertEqual(result["steps"], 1)
            self.assertEqual(result["input_tokens"], 1000)
            self.assertEqual(result["cache_tokens"], 2_000_000)
            self.assertEqual(result["output_tokens"], 100)
            self.assertAlmostEqual(result["cost_usd"], 4.0013, places=6)
            self.assertEqual(result["harness_cost_usd"], 0.5)
            self.assertEqual(result["final"], "done")
            self.assertFalse(result["timed_out"])
            self.assertEqual(result["exit"], 0)
            self.assertTrue(os.path.isfile(os.path.join(tree, "touched.txt")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "meta", "touched.txt")))

    def test_same_settings_produce_the_same_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "tree")
            os.makedirs(tree)
            adapter = _FakeAdapter(_harness_script(tmp))
            one = replay.run_harness(adapter, "p", run_dir=os.path.join(tmp, "a"),
                                     cwd=tree, env=dict(os.environ), harness="deepseek",
                                     model="deepseek-flash", effort="high", timeout=20,
                                     prices=cost.load_prices(None))
            two = replay.run_harness(adapter, "p", run_dir=os.path.join(tmp, "b"),
                                     cwd=tree, env=dict(os.environ), harness="deepseek",
                                     model="deepseek-flash", effort="high", timeout=20,
                                     prices=cost.load_prices(None))
            self.assertEqual(one["settings"], two["settings"])
            self.assertEqual(one["settings"]["model"], "deepseek-flash")
            self.assertEqual(one["settings"]["effort"], "high")

    def test_timeout_kills_the_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "tree")
            os.makedirs(tree)
            adapter = _FakeAdapter(_harness_script(tmp, sleep=5))
            result = replay.run_harness(
                adapter, "p", run_dir=os.path.join(tmp, "meta"), cwd=tree,
                env=dict(os.environ), harness="deepseek", model="deepseek-flash",
                effort="high", timeout=0.3, prices=cost.load_prices(None))
            self.assertTrue(result["timed_out"])
            self.assertLess(result["duration_s"], 4.0)


class RunTestsTest(unittest.TestCase):
    def _tree(self, tmp, body: str) -> str:
        tree = os.path.join(tmp, "tree")
        os.makedirs(os.path.join(tree, "tests"))
        with open(os.path.join(tree, "tests", "test_x.py"), "w", encoding="utf-8") as fh:
            fh.write(body)
        return tree

    def test_green_and_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._tree(tmp, textwrap.dedent("""\
                import unittest
                class T(unittest.TestCase):
                    def test_ok(self):
                        self.assertEqual(1, 1)
                """))
            green = replay.run_tests(tree, ["test_x"])
            self.assertTrue(green["passed"], green)
            self.assertEqual(green["counts"]["ran"], 1)
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._tree(tmp, textwrap.dedent("""\
                import unittest
                class T(unittest.TestCase):
                    def test_ko(self):
                        self.assertEqual(1, 2)
                """))
            red = replay.run_tests(tree, ["test_x"])
            self.assertFalse(red["passed"], red)
            self.assertEqual(red["counts"]["failures"], 1)

    def test_no_test_is_not_a_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "tree"))
            result = replay.run_tests(os.path.join(tmp, "tree"), ["absent"])
            self.assertEqual(result["status"], "no-tests")
            self.assertIsNone(result["passed"])


class ReviewTest(unittest.TestCase):
    def _lot(self):
        return replay.BenchLot(lot=5, title="corriger le cache", klass="normal",
                               app="p", repo="/tmp/r", target="main", base="a" * 40,
                               frozen="b" * 40,
                               body="Le cache rend une valeur périmée après une rotation.")

    def _run(self, diff="+x = 1\n"):
        return {"diff": {"text": diff}, "tests": {"command": "python -m unittest",
                                                  "passed": True,
                                                  "counts": {"ran": 3, "failures": 0,
                                                             "errors": 0}},
                "cost_usd": 1.0, "duration_s": 60.0}

    def test_blind_prompt_has_no_version_cost_or_duration(self):
        prompt_a = replay.review_prompt(self._lot(), "A", self._run())
        prompt_b = replay.review_prompt(self._lot(), "B", self._run())
        for forbidden in ("0.2.0", "0.2.1", "old", "new", "coût", "durée",
                          "duration", "cost"):
            self.assertNotIn(forbidden, prompt_a.lower())
        self.assertIn("soumission A", prompt_a)
        self.assertIn("soumission B", prompt_b)
        self.assertEqual(prompt_a.replace(" A", " B"), prompt_b)

    def test_parse_review_takes_the_last_json_and_clamps(self):
        text = ('brouillon\n```json\n{"score": 15, "defects": [], "summary": "ok"}\n```\n')
        parsed = replay.parse_review(text)
        self.assertEqual(parsed["score"], 10.0)
        self.assertEqual(parsed["summary"], "ok")
        broken = replay.parse_review("aucun json ici")
        self.assertIsNone(broken["score"])
        self.assertTrue(broken["error"])


class CompareTest(unittest.TestCase):
    def _run(self, *, cost=1.0, duration=60.0, passed=True, score=8.0):
        return {"cost_usd": cost, "duration_s": duration,
                "tests": {"passed": passed}, "review": {"score": score}}

    def test_noise_ten_percent_and_one_point(self):
        self.assertEqual(replay.compare_runs(self._run(), self._run(cost=1.1,
                                                                    duration=66.0,
                                                                    score=7.0))["verdict"],
                         "equal")
        self.assertEqual(replay.compare_runs(self._run(), self._run(cost=1.11,
                                                                    duration=60.0))["verdict"],
                         "regression")
        self.assertEqual(replay.compare_runs(self._run(), self._run(cost=0.89))["verdict"],
                         "better")
        self.assertEqual(replay.compare_runs(self._run(), self._run(score=6.0))["verdict"],
                         "regression")
        self.assertEqual(replay.compare_runs(self._run(), self._run(score=10.0))["verdict"],
                         "better")

    def test_tests_drive_quality_and_both_red_is_failed(self):
        self.assertEqual(replay.compare_runs(self._run(passed=True),
                                             self._run(passed=False))["verdict"],
                         "regression")
        self.assertEqual(replay.compare_runs(self._run(passed=False),
                                             self._run(passed=False))["verdict"],
                         "failed")

    def test_conclusion(self):
        self.assertEqual(replay.conclusion([{"verdict": "equal"},
                                            {"verdict": "equal"}]), "equal")
        self.assertEqual(replay.conclusion([{"verdict": "equal"},
                                            {"verdict": "better"}]), "better")
        self.assertEqual(replay.conclusion([{"verdict": "better"},
                                            {"verdict": "failed"}]), "regression")
        self.assertEqual(replay.conclusion([{"verdict": "incomplete"}]), "incomplete")


class InjectTest(unittest.TestCase):
    def test_reference_tests_replace_the_agent_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = _repo(tmp)
            tree = os.path.join(tmp, "tree")
            os.makedirs(os.path.join(tree, "tests"))
            with open(os.path.join(tree, "tests", "test_ref.py"), "w",
                      encoding="utf-8") as fh:
                fh.write("agent = 1\n")
            written = replay.inject_tests(root, base, tree, ["tests/test_ref.py",
                                                             "tests/deleted.py"])
            self.assertEqual(written, ["tests/test_ref.py"])
            with open(os.path.join(tree, "tests", "test_ref.py"),
                      encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "ref = 1\n")


# --------------------------------------------------------------------------
# exécution complète, avec des lanceurs injectés
# --------------------------------------------------------------------------

def _fake_agent(adapter, prompt, *, run_dir, cwd, env, harness, model, effort,
                timeout, prices):
    with open(os.path.join(cwd, "solution.py"), "w", encoding="utf-8") as fh:
        fh.write("solution = 1\n")
    return {"duration_s": 10.0, "turns": 1, "steps": 2, "input_tokens": 1000,
            "cache_tokens": 0, "output_tokens": 100, "cost_usd": 0.1,
            "exit": 0, "timed_out": False, "error": "", "final": "ok",
            "settings": {"model": model, "effort": effort}}


def _fake_tests(tree, modules, *, command="", timeout=0):
    return {"status": "ran", "passed": True, "exit": 0, "duration_s": 1.0,
            "modules": modules, "counts": {"ran": 1, "failures": 0, "errors": 0,
                                           "skipped": 0},
            "timed_out": False, "output_tail": "", "command": "fake"}


class _NullAdapter:
    """Un adaptateur sans commande : les lanceurs sont injectés dans ces tests."""

    def env(self):
        return {}


def _fake_review(adapter, prompt, *, run_dir, env, harness, model, effort,
                 timeout, prices):
    return {"score": 8.0, "defects": [], "summary": "ok", "error": "",
            "cost_usd": 0.01, "duration_s": 1.0}


def _plan(tmp: str, lot, **overrides):
    env_file = os.path.join(tmp, "env")
    with open(env_file, "w", encoding="utf-8") as fh:
        fh.write("ANTHROPIC_API_KEY=test\nOPENAI_API_KEY=test\n"
                 "DEEPSEEK_API_KEY=test\n")
    values = dict(
        project="p", harness="claude", model="claude-sonnet-5", effort="high",
        old_binary="/old", old_version="0.1", new_binary="/new", new_version="0.2",
        lots=[lot], work_root=os.path.join(tmp, "work"), eval_id="e1",
        report_path=os.path.join(tmp, "work", "report.json"),
        old_adapter=_NullAdapter(), new_adapter=_NullAdapter(),
        reviewer="codex", reviewer_adapter=_NullAdapter(), reviewer_model="gpt-6.1-sol",
        reviewer_effort="high", turn_cap_s=10.0, test_cap_s=10.0, review_cap_s=10.0,
        prices=cost.load_prices(None), env_file=env_file, budget_usd=10.0,
        keep=True)
    values.update(overrides)
    return replay.Plan(**values)


class ExecuteTest(unittest.TestCase):
    def _lot(self, root, base):
        return replay.BenchLot(
            lot=7, title="un lot", klass="light", app="p", repo=root,
            target="main", base=base, frozen=base, body="corps du lot",
            files=("tests/test_ref.py",), tests=("tests/test_ref.py",),
            base_source="test")

    def test_two_versions_then_two_blind_reviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = _repo(tmp)
            plan = _plan(tmp, self._lot(root, base))
            report = replay.execute(plan, run_agent=_fake_agent,
                                    test_runner=_fake_tests,
                                    review_agent=_fake_review)
            self.assertEqual(report["schema"], replay.REPORT_SCHEMA)
            entry = report["lots"][0]
            self.assertEqual(set(entry["runs"]), {"old", "new"})
            self.assertEqual(entry["runs"]["old"]["diff"]["files"], 1)
            self.assertTrue(entry["runs"]["old"]["tests"]["passed"])
            self.assertEqual(entry["reviews"]["old"]["score"], 8.0)
            self.assertEqual(entry["verdict"], "equal")
            self.assertEqual(report["conclusion"], "equal")
            self.assertEqual(report["budget"]["runs"], 2)
            self.assertEqual(report["budget"]["reviews"], 2)
            self.assertEqual(set(entry["blind"].values()), {"A", "B"})
            self.assertIsNone(entry["runs"]["old"]["isolation"]["remote"])
            self.assertEqual(report["evaluated"]["kind"], "harness")
            self.assertEqual(report["controls"]["held_constant"],
                             ["model", "effort", "turn_cap_s"])
            self.assertTrue(os.path.isfile(plan.report_path))
            with open(plan.report_path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["schema"], replay.REPORT_SCHEMA)

    def test_injected_tests_are_the_reference_before_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = _repo(tmp)
            plan = _plan(tmp, self._lot(root, base))
            seen = {}

            def tests(tree, modules, *, command="", timeout=0):
                with open(os.path.join(tree, "tests", "test_ref.py"),
                          encoding="utf-8") as fh:
                    seen["test"] = fh.read()
                seen["solution"] = os.path.exists(os.path.join(tree, "solution.py"))
                return _fake_tests(tree, modules, command=command, timeout=timeout)

            replay.execute(plan, run_agent=_fake_agent, test_runner=tests,
                           review_agent=_fake_review)
            self.assertEqual(seen["test"], "ref = 1\n")
            self.assertTrue(seen["solution"])

    def test_model_evaluation_changes_the_model_and_keeps_the_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = _repo(tmp)
            plan = _plan(tmp, self._lot(root, base), evaluated="model", model="",
                         old_model="model-old", new_model="model-new")
            seen = []

            def agent(adapter, prompt, *, run_dir, cwd, env, harness, model,
                      effort, timeout, prices):
                seen.append(model)
                return _fake_agent(adapter, prompt, run_dir=run_dir, cwd=cwd,
                                   env=env, harness=harness, model=model,
                                   effort=effort, timeout=timeout, prices=prices)

            report = replay.execute(plan, run_agent=agent,
                                    test_runner=_fake_tests,
                                    review_agent=_fake_review)
            self.assertEqual(seen, ["model-old", "model-new"])
            self.assertEqual(report["evaluated"]["kind"], "model")
            self.assertEqual(report["evaluated"]["old"]["label"], "model-old")
            self.assertEqual(report["evaluated"]["new"]["label"], "model-new")
            self.assertIsNone(report["constants"]["model"])
            self.assertEqual(report["constants"]["harness"], "claude")
            self.assertEqual(report["controls"]["held_constant"],
                             ["harness", "effort", "turn_cap_s"])
            md = replay.report_markdown(report)
            self.assertIn("modèles model-old → model-new", md)
            self.assertIn("harnais `claude` (constant)", md)

    def test_run_cap_stops_before_the_second_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = _repo(tmp)
            plan = _plan(tmp, self._lot(root, base), max_runs=1)
            report = replay.execute(plan, run_agent=_fake_agent,
                                    test_runner=_fake_tests,
                                    review_agent=_fake_review)
            entry = report["lots"][0]
            self.assertEqual(set(entry["runs"]), {"old"})
            self.assertEqual(report["conclusion"], "incomplete")
            self.assertIn("plafond", report["budget"]["stopped"])

    def test_budget_stops_before_the_second_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = _repo(tmp)
            plan = _plan(tmp, self._lot(root, base), budget_usd=0.05, keep=False)
            report = replay.execute(plan, run_agent=_fake_agent,
                                    test_runner=_fake_tests,
                                    review_agent=_fake_review)
            self.assertEqual(set(report["lots"][0]["runs"]), {"old"})
            self.assertIn("budget", report["budget"]["stopped"])
            self.assertFalse(os.path.isdir(os.path.join(
                plan.work_root, "lot-7-%s" % report["lots"][0]["blind"]["old"])))

    def test_cleanup_keeps_the_report_inside_the_work_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = _repo(tmp)
            plan = _plan(tmp, self._lot(root, base), keep=False)
            report = replay.execute(plan, run_agent=_fake_agent,
                                    test_runner=_fake_tests,
                                    review_agent=_fake_review)
            self.assertTrue(os.path.isfile(plan.report_path))
            self.assertFalse(os.path.exists(os.path.join(
                plan.work_root, "lot-7-%s" % report["lots"][0]["blind"]["old"])))


class ReportTest(unittest.TestCase):
    def _entry(self, lot):
        return {"lot": lot, "title": "t%d" % lot, "class": "normal",
                "base": "a" * 40, "frozen": "b" * 40, "verdict": "equal",
                "runs": {"old": {"cost_usd": 1.0, "duration_s": 60.0, "turns": 1,
                                 "steps": 2, "tests": {"passed": True},
                                 "review": {"score": 8.0, "defects": []}},
                         "new": {"cost_usd": 1.05, "duration_s": 62.0, "turns": 1,
                                 "steps": 2, "tests": {"passed": True},
                                 "review": {"score": 8.0, "defects": [{"summary": "n"}]}}},
                "deltas": {"cost_ratio": 0.05, "duration_ratio": 0.03}}

    def test_markdown_has_three_measures_per_lot(self):
        report = {"schema": replay.REPORT_SCHEMA, "project": "p", "harness": "deepseek",
                  "model": "m", "effort": "high", "turn_cap_s": 1800,
                  "versions": {"old": {"version": "0.1"}, "new": {"version": "0.2"}},
                  "lots": [self._entry(1), self._entry(2), self._entry(3)],
                  "conclusion": "equal", "budget": {"spent_usd": 0.2, "cap_usd": 10,
                                                    "runs": 6, "reviews": 6},
                  "notes": []}
        text = replay.report_markdown(report)
        for lot in (1, 2, 3):
            self.assertIn("## Lot #%d" % lot, text)
        self.assertIn("| Coût |", text)
        self.assertIn("| Durée |", text)
        self.assertIn("| Tests du lot |", text)
        self.assertIn("| Note aveugle |", text)
        self.assertIn("Conclusion", text)
        note = replay.note_text(report)
        self.assertEqual(len(note.splitlines()), 3)
        self.assertIn("équivalente", note)


if __name__ == "__main__":
    unittest.main()
