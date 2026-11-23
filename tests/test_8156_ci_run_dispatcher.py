# SPDX-License-Identifier: AGPL-3.0-only
"""#8156 — `ameesh ci run` compose la pointe de la cible pour un dispatcher.

Pour un projet qui déclare `github.projects.<projet>.ci.dispatcher`, la suite
complète d'un porteur `integration/**` ne part plus par l'étiquette : la pointe
de la cible est lue sur le distant, composée par `merge-tree` sur la tête gelée,
poussée en avance rapide sur la branche du porteur, puis le script du dépôt
sélectionne les suites dues sur l'arbre composé. La décision porte
`sha` = composé (ce que le dispatcher teste) et `reviewed` = gel (ce que les
avis relisent).

Un dépôt git réel (origine nue + clone) tient les gardes : composition propre,
refus nommé d'un conflit sans poussée, refus d'un commit d'auteur après le gel,
acceptation d'une fusion propre de la cible déjà présente (le cas réel
`b6f18dcc` de #7678 : l'avance ne retire pas les avis et ne redemande pas de
gel). Le faux `api` lit les refs et les objets de l'origine nue comme GitHub ;
le faux dispatcher est un vrai script Node qui rend le plan `dry-run` attendu.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from unittest import mock

from ameesh import ci, plan_github, work
from tests import test_l161g_ci_gel as fixtures

REPO = "manaty/nexlink"
BRANCH = "integration/porteur-8156"
SCRIPT = "scripts/ci/dispatch-due-parts.mjs"
WORKFLOW = "tests.yml"
LOT = 8156

#: le script que le dépôt de test porte : il rend le plan `dry-run` d'une
#: seule suite, avec la tête reçue en argument (le contrat du vrai
#: `dispatch-due-parts.mjs` : schéma, ref, head, parts, argv).
DISPATCHER = """\
const [ref, head] = process.argv.slice(2);
console.log(JSON.stringify({
  schema: "ameesh-dispatch-due-parts/1",
  ref: ref,
  head: head,
  dryRun: true,
  failures: [],
  parts: [{ workflow: "tests.yml" }],
  argv: [{ argv: ["workflow", "run", "tests.yml", "--ref", ref,
                  "-f", "expected_sha=" + head] }],
}));
"""

WORKFLOW_DOC = """\
name: tests
on:
  pull_request:
    types: [labeled]
  workflow_dispatch:
    inputs:
      expected_sha:
        required: true
        type: string
# ci-(lot|full)-<sha>
# github.event.label.description
"""


def run(args, cwd=None, check=True, env=None):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          env=dict(os.environ, **(env or {})))
    if check and proc.returncode:
        raise AssertionError("%s: %s" % (" ".join(args), proc.stderr.strip()))
    return proc


class DispatcherRepo(fixtures.GelTestCase):
    """Un lot porteur, une origine nue, une cible qui a avancé depuis le gel."""

    conflict = False

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="ameesh-8156-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.seed = os.path.join(self.tmp, "seed")
        self.local = os.path.join(self.tmp, "work")
        run(["git", "init", "--bare", self.origin])
        run(["git", "init", "-b", "main", self.seed])
        run(["git", "-C", self.seed, "config", "user.name", "banc"])
        run(["git", "-C", self.seed, "config", "user.email", "banc@example.org"])
        self.write("README.md", "banc #8156\n")
        self.write("scripts/ci/dispatch-due-parts.mjs", DISPATCHER)
        self.write(".github/workflows/tests.yml", WORKFLOW_DOC)
        self.write("shared.txt", "base\n")
        self.commit("base")
        run(["git", "-C", self.seed, "remote", "add", "origin", self.origin])
        run(["git", "-C", self.seed, "push", "-u", "origin", "main"])
        # la branche du porteur, puis la cible qui avance : le gel reste la
        # tête du porteur, la PR nomme la pointe courante de `main`.
        run(["git", "-C", self.seed, "checkout", "-b", BRANCH])
        self.write("carrier.txt", "porteur\n")
        if self.conflict:
            self.write("shared.txt", "carrier\n")
        self.commit("porteur")
        run(["git", "-C", self.seed, "push", "origin", BRANCH])
        self.freeze = self.rev(self.seed, BRANCH)
        run(["git", "-C", self.seed, "checkout", "main"])
        self.write("target.txt", "cible\n")
        if self.conflict:
            self.write("shared.txt", "main\n")
        self.commit("cible")
        run(["git", "-C", self.seed, "push", "origin", "main"])
        self.target = self.rev(self.seed, "main")
        run(["git", "clone", self.origin, self.local])
        run(["git", "-C", self.local, "config", "user.name", "banc"])
        run(["git", "-C", self.local, "config", "user.email", "banc@example.org"])
        self.item.update(id=LOT, app="nexlink", branch=BRANCH,
                         branch_target="main", issue_ref=REPO + "#7678")
        self.cfg = self.settings(SCRIPT)
        self.jalons = [fixtures.jalon("frozen", sha=self.freeze, actor="auteur"),
                       fixtures.jalon("verdict", sha=self.freeze,
                                      actor="relecteur", verdict="ok")]

    @staticmethod
    def settings(script: str | None = SCRIPT):
        project = {"repo": REPO}
        if script is not None:
            project["ci"] = {"dispatcher": script}
        return types.SimpleNamespace(github={"projects": {"nexlink": project}})

    def write(self, path: str, text: str) -> None:
        full = Path(self.seed, path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)

    def commit(self, message: str) -> None:
        run(["git", "-C", self.seed, "add", "-A"])
        run(["git", "-C", self.seed, "commit", "-m", message])

    @staticmethod
    def rev(repo: str, ref: str) -> str:
        return run(["git", "-C", repo, "rev-parse", ref]).stdout.strip()

    def head(self, ref: str) -> str:
        return run(["git", "--git-dir", self.origin, "rev-parse",
                    "refs/heads/%s" % ref]).stdout.strip()

    def blob(self, sha: str, path: str) -> str:
        proc = run(["git", "--git-dir", self.origin, "show",
                    "%s:%s" % (sha, path)], check=False)
        if proc.returncode:
            raise plan_github.GithubError("HTTP 404 not found")
        return proc.stdout

    def message(self, sha: str) -> str:
        return run(["git", "--git-dir", self.origin, "show", "-s",
                    "--format=%B", sha]).stdout

    def plan(self, api, **kwargs):
        args = {"full": True, "reason": "packet full suite", "repo": REPO,
                "checkout": self.local}
        args.update(kwargs)
        return ci.gel_plan(self.db, self.cfg, LOT, api=api, **args)

    def api(self):
        return Api(self)


class Api:
    """Les lectures GitHub de `gel_plan`, servies depuis l'origine nue."""

    def __init__(self, case: DispatcherRepo):
        self.case = case
        self.paths: list[str] = []
        self.runs: list[dict] = []

    def __call__(self, path: str):
        self.paths.append(path)
        if "/branches/" in path:
            ref = unquote(path.split("/branches/", 1)[1])
            return {"commit": {"sha": self.case.head(ref)}}
        if "/check-runs?" in path:
            return {"check_runs": []}
        if "/commits/" in path:
            sha = unquote(path.split("/commits/", 1)[1])
            return {"commit": {"message": self.case.message(sha)}}
        if "/pulls?" in path:
            query = parse_qs(urlsplit(path).query)
            ref = unquote(query["head"][0].split(":", 1)[1])
            return [{"number": 7678,
                     "head": {"sha": self.case.head(ref), "ref": ref},
                     "base": {"sha": self.case.head("main"), "ref": "main"},
                     "labels": []}]
        if path.endswith("actions/workflows?per_page=100"):
            return {"workflows": [{"path": ".github/workflows/" + WORKFLOW,
                                   "state": "active"}]}
        if "/actions/workflows/" in path and "/runs?" in path:
            return {"workflow_runs": []}
        if "/contents/" in path:
            script = unquote(path.split("/contents/", 1)[1].split("?", 1)[0])
            sha = unquote(path.rsplit("ref=", 1)[1])
            content = self.case.blob(sha, script)
            return {"content": base64.b64encode(content.encode()).decode()}
        raise AssertionError(path)


class ComposeTest(DispatcherRepo):
    @unittest.skipUnless(shutil.which("node"),
                         "the dispatcher plan needs the node runtime")
    def test_compose_pushes_then_dispatches_the_composed_head(self):
        api = self.api()
        decision = self.plan(api)
        self.assertEqual(decision["action"], "compose")
        composed = decision["sha"]
        self.assertNotEqual(composed, self.freeze)
        self.assertEqual(decision["reviewed"], self.freeze)
        self.assertEqual(decision["base"], self.target)
        self.assertEqual(decision["composition"],
                         {"repo": self.local, "reviewed": self.freeze,
                          "head": self.freeze, "target": self.target,
                          "target_ref": "main", "composed": composed})
        parents = run(["git", "-C", self.local, "rev-list", "--parents", "-n", "1",
                       composed]).stdout.split()
        self.assertEqual(parents, [composed, self.freeze, self.target])
        message = run(["git", "-C", self.local, "show", "-s", "--format=%B",
                       composed]).stdout
        self.assertIn("Ameesh-Lot: #%d" % LOT, message)
        self.assertIn("Ameesh-Frozen: " + self.freeze, message)
        self.assertIn("Ameesh-Target: main@" + self.target, message)
        self.assertEqual(self.head(BRANCH), self.freeze, "le plan n'écrit jamais")
        # même entrée, même commit : la composition est déterministe.
        self.assertEqual(
            ci._compose_commit(self.local, self.freeze, self.target, "main", LOT,
                               self.freeze),
            composed)

        ci._push_composition(self.local, BRANCH, composed,
                             decision["composition"]["head"])
        self.assertEqual(self.head(BRANCH), composed)
        # idempotent : une composition déjà poussée n'est pas repoussée.
        ci._push_composition(self.local, BRANCH, composed,
                             decision["composition"]["head"])

        decision = self.plan(api)
        self.assertEqual(decision["action"], "request")
        self.assertEqual((decision["sha"], decision["reviewed"]),
                         (composed, self.freeze))
        self.assertEqual(decision["path"], "workflow-dispatch")
        self.assertEqual(decision["dispatch"], {WORKFLOW: {"expected_sha": composed}})
        self.assertEqual(decision["label"], ci.label_name("full", composed))
        self.assertFalse(any("/labels" in p for p in api.paths),
                         "aucune étiquette : le dispatcher est le chemin")
        self.assertEqual(decision["verdicts"], ["relecteur"])

    @unittest.skipUnless(shutil.which("node"),
                         "the dispatcher plan needs the node runtime")
    def test_a_clean_merge_after_the_freeze_keeps_the_reviews(self):
        # Le cas réel #7678 (b6f18dcc) : le porteur a avancé par une fusion
        # propre de la cible après le gel. L'avance ne retire pas les avis et
        # ne demande pas de regel : le plan accepte la chaîne, et la cible
        # déjà ancêtre ne recompose rien.
        run(["git", "-C", self.seed, "checkout", BRANCH])
        run(["git", "-C", self.seed, "merge", "--no-ff", "-m",
             "composition : #7678", "main"])
        run(["git", "-C", self.seed, "push", "origin", BRANCH])
        merged = self.rev(self.seed, BRANCH)
        run(["git", "-C", self.local, "fetch", "origin",
             "+refs/heads/%s:refs/remotes/origin/%s" % (BRANCH, BRANCH)])

        decision = self.plan(self.api())
        self.assertEqual(decision["action"], "request")
        self.assertEqual((decision["sha"], decision["reviewed"]), (merged, self.freeze))
        self.assertEqual(decision["verdicts"], ["relecteur"])

    @unittest.skipUnless(shutil.which("node"),
                         "the dispatcher plan needs the node runtime")
    def test_target_already_ancestor_dispatches_the_freeze_without_composition(self):
        # Après une première composition poussée, la pointe de la cible est
        # ancêtre de la tête : pas de nouvelle composition, dispatch sur elle.
        first = self.plan(self.api())
        ci._push_composition(self.local, BRANCH, first["sha"],
                             first["composition"]["head"])
        before = self.head(BRANCH)
        second = self.plan(self.api())
        self.assertEqual(second["action"], "request")
        self.assertEqual(second["sha"], before)
        self.assertEqual(second["composition"]["head"], before)
        self.assertEqual(second["composition"]["target"], self.target)
        self.assertEqual(self.head(BRANCH), before)

    def test_avis_zero_reads_the_carrier_manifest_on_the_reviewed_freeze(self):
        # Le manifeste du porteur est publié sur la tête relue (le gel), pas
        # sur la composition qui n'existe pas encore chez GitHub.
        with mock.patch.object(ci, "_carrier_manifest",
                               return_value="integration-porteur-8156.md") as fake:
            decision = self.plan(self.api(), avis=0)
        self.assertEqual(decision["action"], "compose")
        self.assertEqual(fake.call_args[0][3], self.freeze)

    def test_a_commit_after_the_freeze_is_refused_without_push(self):
        run(["git", "-C", self.seed, "checkout", BRANCH])
        self.write("carrier.txt", "modification d'auteur\n")
        self.commit("correctif non relu")
        run(["git", "-C", self.seed, "push", "origin", BRANCH])
        run(["git", "-C", self.local, "fetch", "origin",
             "+refs/heads/%s:refs/remotes/origin/%s" % (BRANCH, BRANCH)])
        with self.assertRaises(ci.GelError) as caught:
            self.plan(self.api())
        self.assertIn("not a composition merge", str(caught.exception))
        self.assertIn("refreeze", str(caught.exception))
        self.assertEqual(self.head(BRANCH), self.rev(self.seed, "HEAD"))

    def test_a_clean_merge_of_another_branch_is_refused(self):
        # Seule la cible avance un porteur après son gel : une fusion propre
        # d'une branche étrangère ajouterait du code non relu aux avis du gel.
        run(["git", "-C", self.seed, "checkout", "-b", "side", self.freeze])
        self.write("side.txt", "côté\n")
        self.commit("côté")
        run(["git", "-C", self.seed, "checkout", BRANCH])
        run(["git", "-C", self.seed, "merge", "--no-ff", "-m", "fusion côté", "side"])
        run(["git", "-C", self.seed, "push", "origin", BRANCH])
        run(["git", "-C", self.local, "fetch", "origin",
             "+refs/heads/%s:refs/remotes/origin/%s" % (BRANCH, BRANCH)])
        with self.assertRaises(ci.GelError) as caught:
            self.plan(self.api())
        self.assertIn("not in the target", str(caught.exception))

    def test_a_moved_remote_head_refuses_the_push_with_its_lease(self):
        decision = self.plan(self.api())
        run(["git", "-C", self.seed, "checkout", BRANCH])
        self.write("moved.txt", "tête bougée\n")
        self.commit("tête bougée")
        run(["git", "-C", self.seed, "push", "origin", BRANCH])
        with self.assertRaises(ci.GelError) as caught:
            ci._push_composition(self.local, BRANCH, decision["sha"],
                                 decision["composition"]["head"])
        self.assertIn("remote head is", str(caught.exception))
        self.assertNotEqual(self.head(BRANCH), decision["sha"])

    def test_a_declared_dispatcher_missing_before_the_push_is_refused(self):
        # Le chemin déclaré est absent de l'arbre composé : erreur nommée
        # avant toute poussée, jamais un repli silencieux sur l'étiquette.
        self.cfg = self.settings("scripts/ci/absent.mjs")
        with self.assertRaises(ci.GelError) as caught:
            self.plan(self.api())
        self.assertIn("does not carry", str(caught.exception))
        self.assertEqual(self.head(BRANCH), self.freeze)

    def test_a_dispatcher_project_refuses_a_lot_request(self):
        with self.assertRaises(ci.GelError) as caught:
            self.plan(self.api(), full=False, reason="")
        self.assertIn("tested by its packet", str(caught.exception))
        self.assertIn("--full", str(caught.exception))

    def test_a_project_without_dispatcher_keeps_the_label_path(self):
        self.cfg = self.settings(None)
        self.item.update(branch="lot/8156-sans-dispatcher")
        run(["git", "-C", self.seed, "checkout", "-B", "lot/8156-sans-dispatcher",
             self.freeze])
        run(["git", "-C", self.seed, "push", "origin", "lot/8156-sans-dispatcher"])
        self.write("scripts/ci/dispatch-due-parts.mjs", DISPATCHER)
        decision = self.plan(self.api(), full=False, reason="")
        self.assertEqual(decision["action"], "request")
        self.assertEqual(decision["path"], "pr-label")
        self.assertEqual((decision["sha"], decision["reviewed"]),
                         (self.freeze, self.freeze))
        self.assertNotIn("composition", decision)


class CommandTest(DispatcherRepo):
    """`cmd_ci_run` : la boucle compose, pousse, relit et demande le dispatch."""

    def args(self, **extra):
        row = {"id": LOT, "sha": "", "avis": 1, "exception": "", "reason":
               "packet full suite", "full": True, "force": False, "repo": REPO,
               "dry_run": False, "json": True, "prevalidation_repo": self.local,
               "prevalidation_map": ""}
        row.update(extra)
        return argparse.Namespace(**row)

    def run_cmd(self, api, args, request):
        output = io.StringIO()
        with mock.patch("ameesh.db.open_db", lambda cfg: self.db), \
                mock.patch.object(plan_github, "Gh", lambda *a, **k: object()), \
                mock.patch.object(plan_github, "_json",
                                  lambda gh, argv, *a, **k: api(argv[-1])), \
                mock.patch.object(work, "note", lambda *a, **k: True), \
                mock.patch.object(ci.packet_guard, "check_ci", lambda *a, **k: None), \
                mock.patch.object(ci, "_packet_prevalidation", lambda *a, **k: 0), \
                mock.patch.object(ci, "cancel_plan", lambda *a, **k: {"runs": []}), \
                mock.patch.object(ci, "_request_once", request), \
                mock.patch.object(ci, "_arm_packet", lambda *a, **k: None), \
                contextlib.redirect_stdout(output):
            code = ci.cmd_ci_run(self.cfg, args)
        return code, output.getvalue()

    @unittest.skipUnless(shutil.which("node"),
                         "the dispatcher plan needs the node runtime")
    def test_cmd_pushes_the_composition_then_requests_the_dispatch(self):
        api = self.api()
        seen = {}

        def request(db, item_id, decision, **kwargs):
            seen.update(decision)
            return {"requested": True, "action": "request", "run": {}}

        code, output = self.run_cmd(api, self.args(json=False), request)
        self.assertEqual(code, 0)
        composed = seen["sha"]
        self.assertNotEqual(composed, self.freeze)
        self.assertEqual(seen["reviewed"], self.freeze)
        self.assertEqual(seen["dispatch"], {WORKFLOW: {"expected_sha": composed}})
        self.assertEqual(self.head(BRANCH), composed)
        self.assertIn("dispatcher tests.yml", output)

    def test_dry_run_shows_the_composition_without_pushing(self):
        api = self.api()
        called = []
        code, output = self.run_cmd(
            api, self.args(dry_run=True),
            lambda *a, **k: called.append(1) or {"requested": False})
        self.assertEqual(code, 0)
        self.assertEqual(self.head(BRANCH), self.freeze)
        self.assertEqual(called, [])
        self.assertIn("compose", output)


class ConflictTest(DispatcherRepo):
    conflict = True

    def test_conflict_refuses_with_the_paths_and_pushes_nothing(self):
        with self.assertRaises(ci.GelError) as caught:
            self.plan(self.api())
        text = str(caught.exception)
        self.assertIn("conflicts", text)
        self.assertIn("shared.txt", text)
        self.assertEqual(self.head(BRANCH), self.freeze, "aucune poussée")


class RequestMarkTest(fixtures.GelTestCase):
    def test_the_mark_names_the_composed_head_and_the_reviewed_freeze(self):
        composed, frozen = "d" * 40, "e" * 40
        mark = ci._request_note("applied", 7678, ci.label_name("full", composed),
                                composed, "f" * 40, "full", "ab" * 8,
                                reviewed=frozen)
        self.assertIn("sha=" + composed, mark)
        self.assertIn("reviewed=" + frozen, mark)
        self.events = [{"id": 1, "note": mark, "created_ts": 1.0}]
        events = ci.request_events(self.db, LOT)
        self.assertEqual(events[0]["sha"], composed)
        self.assertEqual(events[0]["reviewed"], frozen)
