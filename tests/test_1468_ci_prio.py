# SPDX-License-Identifier: AGPL-3.0-only
"""Pool de runners prioritaire de la CI (#1468).

Le `runs-on` des jobs Linux de `.github/workflows/tests.yml` choisit entre
trois destinations : `ameesh-ci` (runs ordinaires), `ameesh-ci-pool` (runs
prioritaires : paquet, release, lot marqué déblocage/incident, interrupteur
`vars.AMEESH_CI_PRIO` à `on`) et `ubuntu-latest` (PR d'une fourche, comme
avant). Ce module lit les expressions du workflow sans PyYAML (absent du venv
de la CI) et les évalue avec un petit interpréteur du sous-ensemble
d'expressions GitHub qu'elles emploient (`&&`/`||` qui rendent un opérande,
`==`/`!=`, `!`, `startsWith`, `contains`, `fromJSON`, chemins de contexte) :
le routage est vérifié sur l'expression réellement livrée, pas sur une copie.

Garanties : la même expression sur chaque job Linux ; interrupteur absent ou
éteint = comportement d'avant pour tous les runs ; aucune destination hébergée
autre que `ubuntu-latest` pour une fourche ; l'interrupteur n'apparaît dans
aucun `if:` (le routage ne décide ni d'un saut ni d'un check requis).
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"

ORDINARY = ["self-hosted", "linux", "ameesh-ci"]
POOL = ["self-hosted", "linux", "ameesh-ci-pool"]
HOSTED = "ubuntu-latest"

_JOB = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
_RUNS_ON = re.compile(r"^    runs-on:\s*(.*)$")


def runs_on_by_job(text: str) -> dict[str, str]:
    jobs: dict[str, str] = {}
    in_jobs, job = False, None
    for line in text.splitlines():
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line and not line.startswith(" ") and not line.startswith("#"):
            in_jobs = False
            continue
        m = _JOB.match(line)
        if m:
            job = m.group(1)
            continue
        m = _RUNS_ON.match(line)
        if m and job:
            jobs[job] = m.group(1).strip()
    return jobs


# --- interpréteur du sous-ensemble d'expressions GitHub ---------------------

_TOKEN = re.compile(r"""\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+)|(?P<op>&&|\|\||==|!=|!|\(|\)|,)"""
                    r"""|(?P<name>[A-Za-z_][A-Za-z0-9_.*-]*))""")


def _tokens(src: str) -> list[tuple[str, object]]:
    out, pos = [], 0
    src = src.strip()
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise ValueError("unsupported expression near %r" % src[pos:pos + 20])
        pos = m.end()
        if m.group("str") is not None:
            out.append(("lit", m.group("str")[1:-1].replace("''", "'")))
        elif m.group("num") is not None:
            out.append(("lit", int(m.group("num"))))
        elif m.group("op") is not None:
            out.append(("op", m.group("op")))
        else:
            name = m.group("name")
            if name in ("true", "false", "null"):
                out.append(("lit", {"true": True, "false": False, "null": None}[name]))
            else:
                out.append(("name", name))
    return out


def _truthy(v: object) -> bool:
    return v not in (None, False, 0, "")


def _str(v: object) -> str:
    return "" if v is None else str(v)


def _eq(a: object, b: object) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    return a == b


class _Eval:
    def __init__(self, tokens, ctx):
        self.t, self.i, self.ctx = tokens, 0, ctx

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, op=None):
        tok = self.peek()
        if op is not None and tok != ("op", op):
            raise ValueError("expected %r, got %r" % (op, tok))
        self.i += 1
        return tok

    def expr(self):
        v = self.and_()
        while self.peek() == ("op", "||"):
            self.take()
            rhs = self.and_()
            v = v if _truthy(v) else rhs
        return v

    def and_(self):
        v = self.eq()
        while self.peek() == ("op", "&&"):
            self.take()
            rhs = self.eq()
            v = rhs if _truthy(v) else v
        return v

    def eq(self):
        v = self.unary()
        if self.peek() in (("op", "=="), ("op", "!=")):
            op = self.take()[1]
            rhs = self.unary()
            v = _eq(v, rhs) if op == "==" else not _eq(v, rhs)
        return v

    def unary(self):
        if self.peek() == ("op", "!"):
            self.take()
            return not _truthy(self.unary())
        return self.primary()

    def primary(self):
        kind, val = self.take()
        if kind == "lit":
            return val
        if (kind, val) == ("op", "("):
            v = self.expr()
            self.take(")")
            return v
        if kind == "name" and self.peek() == ("op", "("):
            self.take()
            args = [self.expr()]
            while self.peek() == ("op", ","):
                self.take()
                args.append(self.expr())
            self.take(")")
            return self.call(val, args)
        if kind == "name":
            return self.lookup(val)
        raise ValueError("unexpected token %r" % ((kind, val),))

    def call(self, fn, args):
        if fn == "startsWith":
            return _str(args[0]).lower().startswith(_str(args[1]).lower())
        if fn == "contains":
            hay, needle = args
            if isinstance(hay, list):
                return any(_eq(x, needle) for x in hay)
            return _str(needle).lower() in _str(hay).lower()
        if fn == "fromJSON":
            return json.loads(_str(args[0]))
        raise ValueError("unsupported function %s" % fn)

    def lookup(self, path):
        v: object = self.ctx
        for part in path.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        return v


def evaluate(field: str, ctx: dict) -> object:
    m = re.fullmatch(r"\$\{\{(.*)\}\}", field.strip(), re.S)
    if not m:
        return field
    ev = _Eval(_tokens(m.group(1)), ctx)
    v = ev.expr()
    if ev.i != len(ev.t):
        raise ValueError("trailing tokens in %r" % field)
    return v


# --- contextes ---------------------------------------------------------------

REPO = "manaty/ameesh"


def push(ref, vars_=None):
    return {"github": {"event_name": "push", "ref": "refs/heads/" + ref,
                       "base_ref": "", "head_ref": "", "repository": REPO,
                       "event": {}}, "vars": vars_ or {}}


def dispatch(ref, vars_=None):
    ctx = push(ref, vars_)
    ctx["github"]["event_name"] = "workflow_dispatch"
    return ctx


def pr(head, base="main", number=1500, fork=False, vars_=None):
    return {"github": {
        "event_name": "pull_request", "ref": "refs/pull/%d/merge" % number,
        "base_ref": base, "head_ref": head, "repository": REPO,
        "event": {"pull_request": {
            "number": number,
            "head": {"repo": {"full_name": "someone/ameesh" if fork else REPO}}}}},
        "vars": vars_ or {}}


ON = {"AMEESH_CI_PRIO": "on"}


class RunsOnTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = runs_on_by_job(WORKFLOW.read_text(encoding="utf-8"))
        cls.linux = {j: e for j, e in cls.jobs.items()
                     if not j.startswith("platform-macos") and j != "python314"}
        cls.expr = next(iter(cls.linux.values()))

    def route(self, ctx):
        return evaluate(self.expr, ctx)

    def test_same_expression_on_every_linux_job(self):
        self.assertEqual(set(self.linux), {"plan", "psycopg", "psql", "doc-readers",
                                           "bilan", "version", "tests", "fs-class",
                                           "profil-nu"})
        # #1256 x #1468 : `bilan` (hors de la clôture du check requis,
        # continue-on-error) porte, après le pool prioritaire, le repli vers
        # l'hôte prêté quand un prêt est armé ; tous les autres jobs Linux
        # gardent l'expression unique du pool.
        others = {j: e for j, e in self.linux.items() if j != "bilan"}
        self.assertEqual(len(set(others.values())), 1, others)
        self.assertEqual(self.linux["bilan"], others["plan"].replace(
            """'["self-hosted", "linux", "ameesh-ci"]'""",
            """vars.AMEESH_LENT_RUNS_ON || '["self-hosted", "linux", "ameesh-ci"]'"""),
            self.linux["bilan"])
        # #987 x #1468 : les jobs requis tournent sur l'image x64 standard
        # (`macos-26-intel`) ; `macos-latest` (macOS 26 arm64) n'obtenait pas
        # de capacité hébergée et était annulé sans qu'une étape ne tourne.
        # Le littéral reste épinglé : toute dérive de l'image est vue.
        self.assertEqual(self.jobs["platform-macos"], "macos-26-intel")
        # #3380: the optional interpreter smoke runs on GitHub, outside the fleet.
        self.assertEqual(self.jobs["python314"], "ubuntu-latest")

    def test_switch_off_keeps_previous_routing(self):
        for vars_ in ({}, {"AMEESH_CI_PRIO": "off"}, {"AMEESH_CI_PRIO": ""},
                      {"AMEESH_CI_PRIO_PRS": "[1500]"}):
            for ctx in (push("integration/paquet-2", vars_), push("release/1.12", vars_),
                        push("main", vars_), dispatch("main", vars_),
                        pr("integration/paquet-2", vars_=vars_), pr("lot/1500-x", vars_=vars_)):
                with self.subTest(vars=vars_, ctx=ctx["github"]["ref"]):
                    self.assertEqual(self.route(ctx), ORDINARY)
            self.assertEqual(self.route(pr("lot/x", fork=True, vars_=vars_)), HOSTED)

    def test_package_and_release_runs_take_the_pool(self):
        for ctx in (push("integration/paquet-2", ON), push("release/1.12", ON),
                    dispatch("release/1.12", ON),
                    pr("integration/paquet-2", vars_=ON), pr("release/1.12", vars_=ON),
                    pr("lot/1500-x", base="integration/paquet-2", vars_=ON),
                    pr("hotfix", base="release/1.12", vars_=ON)):
            with self.subTest(ref=ctx["github"]["ref"], head=ctx["github"]["head_ref"]):
                self.assertEqual(self.route(ctx), POOL)

    def test_ordinary_runs_never_take_the_pool(self):
        for ctx in (push("main", ON), dispatch("main", ON), pr("lot/1500-x", vars_=ON),
                    pr("lot/integration-fix", vars_=ON),
                    pr("lot/1500-x", vars_=dict(ON, AMEESH_CI_PRIO_PRS="[1460, 1462]"))):
            with self.subTest(ref=ctx["github"]["ref"], head=ctx["github"]["head_ref"]):
                self.assertEqual(self.route(ctx), ORDINARY)

    def test_marked_unblock_or_incident_pr_takes_the_pool(self):
        vars_ = dict(ON, AMEESH_CI_PRIO_PRS="[1460, 1500]")
        self.assertEqual(self.route(pr("lot/1500-x", vars_=vars_)), POOL)
        # the mark is a PR number: a push to the same branch is not marked
        self.assertEqual(self.route(push("lot/1500-x", vars_)), ORDINARY)

    def test_fork_pr_stays_hosted_and_never_reaches_our_runners(self):
        vars_ = dict(ON, AMEESH_CI_PRIO_PRS="[1500]")
        for ctx in (pr("lot/1500-x", fork=True, vars_=vars_),
                    pr("release/1.12", fork=True, vars_=vars_),
                    pr("x", base="integration/p", fork=True, vars_=vars_)):
            self.assertEqual(self.route(ctx), HOSTED)

    def test_no_other_destination(self):
        """Only the three known destinations: no paid hosted fallback."""
        self.assertEqual(self.expr.count("ubuntu-latest"), 1)
        self.assertNotRegex(self.expr, r"macos|windows|ubuntu-\d|large|xl")

    def test_routing_never_gates_a_job(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "AMEESH_CI_PRIO" in line and not line.lstrip().startswith("#"):
                self.assertRegex(line, r"^    runs-on: ", line)


def _load_queue_wait():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ci_queue_wait", ROOT / "scripts" / "ci-queue-wait.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class QueueWaitTest(unittest.TestCase):
    """scripts/ci-queue-wait.py classifies runs like the routing does."""

    @classmethod
    def setUpClass(cls):
        cls.q = _load_queue_wait()

    def test_run_class(self):
        c = self.q.run_class
        self.assertEqual(c({"event": "push", "head_branch": "integration/p2"}), "prio")
        self.assertEqual(c({"event": "push", "head_branch": "release/1.12"}), "prio")
        self.assertEqual(c({"event": "push", "head_branch": "main"}), "ordinary")
        self.assertEqual(c({"event": "pull_request", "head_branch": "release/1.12"}), "prio")
        lot = {"event": "pull_request", "head_branch": "lot/1500-x",
               "pull_requests": [{"number": 1500, "base": {"ref": "main"}}]}
        self.assertEqual(c(lot), "ordinary")
        self.assertEqual(c(lot, frozenset({1500})), "prio")
        to_pkg = dict(lot, pull_requests=[{"number": 7, "base": {"ref": "integration/p2"}}])
        self.assertEqual(c(to_pkg), "prio")

    def test_waits_and_summary(self):
        runs = [{"id": 1, "event": "push", "head_branch": "integration/p2"},
                {"id": 2, "event": "push", "head_branch": "main"}]
        sh = ["self-hosted", "linux"]
        jobs = {
            1: [{"name": "a", "labels": sh + ["ameesh-ci-pool"], "runner_name": "ameesh-ci-3-1",
                 "created_at": "2026-10-15T01:00:00Z", "started_at": "2026-10-15T01:00:30Z"}],
            2: [{"name": "b", "labels": sh + ["ameesh-ci"], "runner_name": "ameesh-ci-1-1",
                 "created_at": "2026-10-15T01:00:00Z", "started_at": "2026-10-15T01:10:00Z"},
                {"name": "never", "labels": sh + ["ameesh-ci"], "runner_name": None,
                 "created_at": "2026-10-15T01:00:00Z", "started_at": None},
                {"name": "mac", "labels": ["macos-latest"], "runner_name": "GitHub Actions 1",
                 "created_at": "2026-10-15T01:00:00Z", "started_at": "2026-10-15T01:00:05Z"}]}
        rows = self.q.waits(runs, jobs)
        self.assertEqual([(r["job"], r["class"], r["wait"]) for r in rows],
                         [("a", "prio", 30.0), ("b", "ordinary", 600.0)])
        s = self.q.summary(rows)
        self.assertEqual(s["class=prio"], {"n": 1, "median": 30.0, "p80": 30.0, "max": 30.0})
        self.assertEqual(s["class=ordinary"]["median"], 600.0)
        self.assertEqual(s["group=reserved"]["n"], 1)
        self.assertEqual(s["group=general"]["n"], 1)


class EvaluatorTest(unittest.TestCase):
    """The interpreter itself, on the GitHub semantics the routing relies on."""

    def test_and_or_return_operands(self):
        self.assertEqual(evaluate("${{ true && 'a' || 'b' }}", {}), "a")
        self.assertEqual(evaluate("${{ false && 'a' || 'b' }}", {}), "b")
        self.assertEqual(evaluate("${{ '' || fromJSON('[1]') }}", {}), [1])

    def test_missing_context_is_null_and_case_insensitive(self):
        self.assertFalse(evaluate("${{ vars.X == 'on' }}", {"vars": {}}))
        self.assertTrue(evaluate("${{ startsWith(a.b, 'REL') }}", {"a": {"b": "release/1"}}))
        self.assertTrue(evaluate("${{ contains(fromJSON('[3, 4]'), n) }}", {"n": 4}))

    def test_unknown_syntax_is_refused(self):
        with self.assertRaises(ValueError):
            evaluate("${{ format('{0}', a) > 1 }}", {})


if __name__ == "__main__":
    unittest.main()
