# SPDX-License-Identifier: AGPL-3.0-only
"""L1651 (#1650) : la porte de file refuse un vert partiel.

La tête gelée d'un lot ancien (antérieure au job de plan #1368) peut ne
porter qu'une partie de ses parts : les statuts présents sont tous verts, la
part jamais dispatchée n'a aucun statut, et `radar.commit_ci` lisait vert
(#1470, cinq parts dues, une annoncée). Les témoins ci-dessous portent sur la
porte : l'ensemble exact des parts dues est recalculé par le sélecteur
versionné de la cible, et chaque part doit exister sur le SHA gelé et être
terminale-success.

Le sélecteur du dépôt de témoin est un faux volontairement minimal
(`.ci/parts.json` + `scripts/ci/lot-plan.py`) : la sélection d'un vrai dépôt
(Nexlink : `scripts/ci/lot-plan.mjs`) a ses propres témoins ; ici on éprouve
la plomberie d'ameesh — arbre épinglé de la cible, change set NUL, sortie
validée, décision de la porte."""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
import unittest
from unittest import mock

from ameesh import ci_parts, land, merge_queue, plan_git, radar, resolutions

from .test_l160b_radar import _git, _write
from .test_l160e2_land import _Land
from .test_l160g_nexlink import _Gh, _run

#: les cinq parts de code du contrat de témoin, et le Docs gate qui n'est
#: jamais planifié (B2 de #1345, #1368)
PARTS = [{"context": "lot/backend-suite", "paths": ["backend/**"]},
         {"context": "lot/rollback-checks", "paths": ["rollback/**"]},
         {"context": "lot/scripts-suite", "paths": ["scripts/**"]},
         {"context": "lot/sync-checks", "paths": ["sync/**"]},
         {"context": "lot/website-suite", "paths": ["website/**"]},
         {"context": "lot/docs-gate", "paths": ["docs/**"]}]

#: la dernière ligne du sélecteur de témoin, remplacée par les cas tordus
WRITE = 'json.dump({"schema": "test-lot-plan/1", "due": due}, handle)'
SELECTOR = '''\
import fnmatch
import json
import os
import sys

options = {}
args = sys.argv[1:]
for index, arg in enumerate(args):
    if arg.startswith("--"):
        options[arg[2:]] = args[index + 1]
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
with open(os.path.join(root, ".ci", "parts.json"), encoding="utf-8") as handle:
    parts = json.load(handle)
with open(options["files"], "rb") as handle:
    files = [name for name in handle.read().decode("utf-8").split("\\0") if name]
due = []
for part in parts:
    if part["context"] == "lot/docs-gate":
        continue
    if any(fnmatch.fnmatchcase(name, glob) for name in files for glob in part["paths"]):
        due.append(part["context"])
with open(options["out"], "w", encoding="utf-8") as handle:
    __WRITE__
'''


def selector(write=WRITE) -> str:
    return SELECTOR.replace("__WRITE__", write)


CI_PARTS = {"selector": "scripts/ci/lot-plan.py",
            "argv": [sys.executable, "{selector}", "--files", "{files}", "--out", "{out}"],
            "schema": "test-lot-plan/1"}
PARTS_CONTRACT = radar.parse_ci_parts(CI_PARTS, "`radar.projects.p`")


def contexts(**states) -> dict:
    """Le retour d'un lecteur de CI : `green` ne regarde que les statuts
    présents, comme `radar.commit_ci` — c'est le trou que la porte ferme.
    N1 de #1666 : les parts dues se lisent dans l'espace des **statuts de
    commit** (`statuses`) ; `contexts` (runs, checks et statuts mêlés) ne les
    prouve plus."""
    return {"runs": len(states) or 1, "green": all(v == "success" for v in states.values()),
            "pending": [k for k, v in states.items() if v == "pending"],
            "failed": [k for k, v in states.items() if v == "failed"],
            "contexts": dict(states), "statuses": dict(states)}


def labels_only(**states) -> dict:
    """Un lecteur dont les libellés d'états n'existent que dans `contexts`
    (un run ou un check homonyme d'une part) : aucune part n'est prouvée, le
    rollup peut pourtant être vert (N1 de #1666)."""
    return dict(contexts(**states), statuses={})


class SettingsTest(unittest.TestCase):
    def parse(self, parts):
        return radar.parse_settings({"projects": {
            "p": {"repo": "/r", "ci_required": True, "ci_parts": parts}}}).projects["p"]

    def test_the_versioned_contract_is_read(self):
        project = self.parse(dict(CI_PARTS, rev="develop"))
        self.assertEqual(project.ci_parts.selector, "scripts/ci/lot-plan.py")
        self.assertEqual(project.ci_parts.rev, "develop")
        self.assertEqual(project.ci_parts.due, "due")
        self.assertEqual(project.ci_parts.argv[0], sys.executable)

    def test_ambiguous_or_unsafe_declarations_are_refused(self):
        for parts, why in (
                ("scripts/ci/x.py", "object"),
                (dict(CI_PARTS, selector="/etc/passwd"), "inside the repository"),
                (dict(CI_PARTS, selector="a/../../x"), "inside the repository"),
                (dict(CI_PARTS, selector=""), "relative path"),
                (dict(CI_PARTS, argv=[]), "non-empty list"),
                (dict(CI_PARTS, argv=["node", "x", "--files", "{files}"]), "{out} is required"),
                (dict(CI_PARTS, argv=["node", "x", "--out", "{out}"]), "{files} is required"),
                (dict(CI_PARTS, argv=["node", "{unknown}", "--files", "{files}",
                                      "--out", "{out}"]), "unknown placeholder"),
                (dict(CI_PARTS, argv=["node", "{", "--files", "{files}", "--out", "{out}"]),
                 "stray brace"),
                (dict(CI_PARTS, rev="a..b"), "ci_parts.rev"),
                (dict(CI_PARTS, due=""), "non-empty text"),
                (dict(CI_PARTS, schema=3), "non-empty text"),
                (dict(CI_PARTS, extra="x"), "unknown key")):
            with self.subTest(why=why):
                with self.assertRaisesRegex(radar.SettingsError, why):
                    self.parse(parts)

    def test_an_inert_contract_is_refused(self):
        """`ci_parts` ne se lit que dans la CI du commit gelé : sans
        `ci_required`, la déclaration ne prouverait rien."""
        with self.assertRaisesRegex(radar.SettingsError, "ci_required must be true"):
            radar.parse_settings({"projects": {
                "p": {"repo": "/r", "ci_parts": dict(CI_PARTS)}}})


class _Contract(_Land):
    """Un dépôt de témoin dont la cible porte le contrat versionné."""

    def setUp(self):
        super().setUp()
        _write(self.repo, "scripts/ci/lot-plan.py", selector())
        _write(self.repo, ".ci/parts.json", json.dumps(PARTS))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "ci: the versioned selection source")
        _git(self.repo, "push", "-q", "origin", "main")
        self.project = dataclasses.replace(self.project, ci_required=True,
                                           ci_parts=PARTS_CONTRACT)

    def publish_main(self):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "contract change")
        _git(self.repo, "push", "-q", "-f", "origin", "main")

    def base(self):
        return _git(self.repo, "rev-parse", "main")

    def lot_touching(self, *dirs, marker=""):
        """Une tête gelée **sans** le sélecteur (branche d'avant le contrat),
        qui touche les répertoires donnés."""
        branch = self.fake.lots[4]["branch"]
        _git(self.repo, "checkout", "-q", branch)
        for name in dirs:
            _write(self.repo, "%s/file%s.txt" % (name, marker), "x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "delta")
        _git(self.repo, "push", "-q", "origin", branch)
        _git(self.repo, "checkout", "-q", "main")
        return branch

    def clone(self):
        return resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))


class ContractTest(_Contract):
    """Le contrat versionné : le sélecteur est lu dans l'arbre de la cible,
    jamais dans celui de la tête gelée."""

    def test_an_old_head_is_judged_by_the_target_contract(self):
        branch = self.lot_touching("backend", "scripts", "sync", "website", "rollback")
        tip = _git(self.repo, "rev-parse", branch)
        # la tête gelée ne porte pas le plan : c'est le trou #1470
        self.assertIsNone(plan_git._commit(self.repo, branch + ":scripts/ci/lot-plan.py"))
        found = ci_parts.due(self.project, tip, self.base(), self.repo)
        self.assertEqual(found["due"], ("lot/backend-suite", "lot/rollback-checks",
                                        "lot/scripts-suite", "lot/sync-checks",
                                        "lot/website-suite"))
        self.assertEqual(found["schema"], "test-lot-plan/1")
        self.assertEqual(found["selector"], "scripts/ci/lot-plan.py")
        self.assertEqual(found["merge_base"], _git(self.repo, "merge-base", "main", branch))

    def test_a_docs_only_change_set_makes_no_code_part_due(self):
        branch = self.lot_touching("docs")
        tip = _git(self.repo, "rev-parse", branch)
        self.assertEqual(ci_parts.due(self.project, tip, self.base(), self.repo)["due"], ())

    def test_the_head_cannot_suppress_a_part_with_its_own_selector(self):
        """Le sélecteur de la tête (saboté) ne compte pas : le contrat est
        celui de la cible. Le changement de `scripts/**` rend scripts-suite
        due, backend-suite l'est par le delta backend."""
        branch = self.fake.lots[4]["branch"]
        _git(self.repo, "checkout", "-q", branch)
        _write(self.repo, "scripts/ci/lot-plan.py", "raise SystemExit(0)\n")
        _write(self.repo, "backend/file.txt", "x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "delta + sabotaged selector")
        _git(self.repo, "push", "-q", "origin", branch)
        _git(self.repo, "checkout", "-q", "main")
        tip = _git(self.repo, "rev-parse", branch)
        self.assertEqual(ci_parts.due(self.project, tip, self.base(), self.repo)["due"],
                         ("lot/backend-suite", "lot/scripts-suite"))

    def test_a_selector_absent_from_the_pinned_tree_refuses(self):
        os.remove(os.path.join(self.repo, "scripts/ci/lot-plan.py"))
        self.publish_main()
        branch = self.lot_touching("backend")
        tip = _git(self.repo, "rev-parse", branch)
        with self.assertRaisesRegex(ci_parts.PartsError, "not in the tree"):
            ci_parts.due(self.project, tip, self.base(), self.repo)

    def test_an_unreadable_part_table_refuses(self):
        _write(self.repo, ".ci/parts.json", "{not json")
        self.publish_main()
        branch = self.lot_touching("backend")
        tip = _git(self.repo, "rev-parse", branch)
        with self.assertRaisesRegex(ci_parts.PartsError, "failed \\(exit 1\\)"):
            ci_parts.due(self.project, tip, self.base(), self.repo)

    def test_a_malformed_or_unknown_plan_refuses(self):
        branch = self.lot_touching("backend")
        tip = _git(self.repo, "rev-parse", branch)
        for write, why in (
                ('json.dump({"schema": "other/1", "due": []}, handle)',
                 "plan schema is 'other/1'"),
                ('json.dump({"due": []}, handle)', "declares no schema"),
                ('json.dump({"schema": "test-lot-plan/1", "due": "x"}, handle)',
                 "not a list of part names"),
                ('json.dump({"schema": "test-lot-plan/1", "due": ["a", "a"]}, handle)',
                 "repeats a part"),
                ("handle.write('{')", "no readable plan")):
            with self.subTest(why=why):
                _write(self.repo, "scripts/ci/lot-plan.py", selector(write))
                self.publish_main()
                with self.assertRaisesRegex(ci_parts.PartsError, why):
                    ci_parts.due(self.project, tip, self.base(), self.repo)
                _write(self.repo, "scripts/ci/lot-plan.py", selector())
                self.publish_main()

    def test_a_missing_frozen_commit_or_base_refuses(self):
        with self.assertRaisesRegex(ci_parts.PartsError, "not in"):
            ci_parts.due(self.project, "b" * 40, self.base(), self.repo)
        with self.assertRaisesRegex(ci_parts.PartsError, "not a commit"):
            ci_parts.due(self.project, self.base(), "nosuchref", self.repo)

    def test_a_hand_built_contract_cannot_read_outside_the_tree(self):
        project = dataclasses.replace(self.project, ci_parts=radar.CiParts(
            selector="../../etc/passwd",
            argv=(sys.executable, "{selector}", "--files", "{files}", "--out", "{out}")))
        with self.assertRaisesRegex(ci_parts.PartsError, "not a path inside the repository"):
            ci_parts.due(project, self.base(), self.base(), self.repo)

    def test_the_selector_runs_without_the_host_environment(self):
        """Le sélecteur vient du dépôt : il ne voit pas les jetons ni les DSN
        de l'hôte qui exécute la porte."""
        with mock.patch.dict(os.environ, {"AMEESH_SENTINEL": "secret", "GH_TOKEN": "t"}):
            env = ci_parts.selector_env("/root", "/home")
        self.assertNotIn("AMEESH_SENTINEL", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["GITHUB_WORKSPACE"], "/root")
        self.assertEqual(env["HOME"], "/home")
        self.assertIn("PATH", env)


class GateTest(_Contract):
    """La porte : 1/5 refuse, 5/5 autorise, absent/skipped/cancelled refusent,
    docs-only respecte la classification."""

    def setUp(self):
        super().setUp()
        self.parts = ("lot/backend-suite", "lot/rollback-checks", "lot/scripts-suite",
                      "lot/sync-checks", "lot/website-suite")

    def enter_with(self, *dirs, contexts=None, marker=""):
        self.lot_touching(*dirs, marker=marker)
        self.ready(4)
        return merge_queue.enter(None, 4, repo=self.repo, actor="orch", ci=None,
                                 clone=self.clone(), project=self.project,
                                 commit_ci=lambda sha: contexts)

    def test_five_of_five_enters(self):
        row = self.enter_with("backend", "scripts", "sync", "website", "rollback",
                              contexts=contexts(**{part: "success" for part in self.parts}))
        self.assertEqual(row["state"], "queued")

    def test_one_of_five_is_refused(self):
        with self.assertRaisesRegex(merge_queue.QueueError, "4 of 5 missing or not successful"):
            self.enter_with("backend", "scripts", "sync", "website", "rollback",
                            contexts=contexts(**{"lot/backend-suite": "success"}))
        self.assertNotIn(4, self.fake.queue_rows)

    def test_a_pending_or_red_part_is_refused_and_named(self):
        for index, (part, state) in enumerate((("lot/website-suite", "pending"),
                                               ("lot/sync-checks", "failed"))):
            with self.subTest(part=part):
                states = {name: "success" for name in self.parts}
                states[part] = state
                with self.assertRaisesRegex(merge_queue.QueueError, part.replace("/", "/")):
                    self.enter_with("backend", "scripts", "sync", "website", "rollback",
                                    contexts=contexts(**states), marker=str(index))

    def test_skipped_and_cancelled_runs_are_not_green_parts(self):
        sha = "a" * 40
        skipped = radar.commit_ci(self.project, sha, gh=_Gh([
            _run("lot/backend-suite", conclusion="skipped")]))
        self.assertTrue(skipped["green"])                    # ne bloque aucun rollup
        self.assertEqual(skipped["contexts"]["lot/backend-suite"], "failed")
        cancelled = radar.commit_ci(self.project, sha, gh=_Gh([
            _run("lot/backend-suite", conclusion="cancelled")]))
        self.assertEqual(cancelled["contexts"]["lot/backend-suite"], "failed")
        for runs in (skipped, cancelled):
            with self.subTest(contexts=runs["contexts"]):
                with self.assertRaises(merge_queue.QueueError):
                    merge_queue._required_ci(self.project, sha, commit_ci=lambda s: runs,
                                             parts=["lot/backend-suite"])

    def test_a_workflow_run_named_like_a_part_does_not_prove_it(self):
        """N1 de #1666 : un run Actions nommé `lot/backend-suite` qui réussit
        n'est pas la part — elle n'existe que dans les statuts de commit."""
        runs = labels_only(**{"lot/backend-suite": "success"})
        self.assertTrue(runs["green"])
        self.assertEqual(runs["statuses"], {})
        with self.assertRaisesRegex(merge_queue.QueueError, "lot/backend-suite"):
            merge_queue._required_ci(self.project, "a" * 40, commit_ci=lambda s: runs,
                                     parts=["lot/backend-suite"])

    def test_an_external_check_named_like_a_part_does_not_prove_it(self):
        """N1 de #1666 : un check d'une app tierce homonyme d'une part ne la
        prouve pas davantage — l'espace des parts est celui des statuts."""
        check = {"id": 7, "app": {"id": 9, "slug": "external-ci"},
                 "name": "lot/backend-suite", "head_sha": "a" * 40,
                 "status": "completed", "conclusion": "success",
                 "started_at": "2026-10-12T10:00:00Z",
                 "completed_at": "2026-10-12T11:00:00Z"}
        runs = radar.commit_ci(self.project, "a" * 40, gh=_Gh([_run("ci")], checks=[check]))
        self.assertEqual(runs["contexts"]["9/lot/backend-suite"], "success")
        self.assertEqual(runs["statuses"], {})
        with self.assertRaisesRegex(merge_queue.QueueError, "lot/backend-suite"):
            merge_queue._required_ci(self.project, "a" * 40, commit_ci=lambda s: runs,
                                     parts=["lot/backend-suite"])

    def test_a_real_commit_status_proves_the_part(self):
        """N1 de #1666 : le statut de commit, lui, prouve la part."""
        runs = radar.commit_ci(self.project, "a" * 40, gh=_Gh(
            [_run("ci")], statuses=[{"context": "lot/backend-suite", "state": "success"}]))
        self.assertEqual(runs["statuses"], {"lot/backend-suite": "success"})
        merge_queue._required_ci(self.project, "a" * 40, commit_ci=lambda s: runs,
                                 parts=["lot/backend-suite"])

    def test_a_reader_without_the_status_space_proves_nothing(self):
        """N1 de #1666 : un lecteur qui ne sépare pas les espaces (dict d'avant
        la clé `statuses`) ne prouve aucune part — jamais de repli sur
        `contexts`."""
        runs = {"runs": 1, "green": True, "pending": [], "failed": [],
                "contexts": {"lot/backend-suite": "success"}}
        with self.assertRaisesRegex(merge_queue.QueueError, "lot/backend-suite"):
            merge_queue._required_ci(self.project, "a" * 40, commit_ci=lambda s: runs,
                                     parts=["lot/backend-suite"])

    def test_an_old_green_attempt_does_not_cover_the_latest_cancelled_one(self):
        rows = [_run("lot/backend-suite"),
                _run("lot/backend-suite", conclusion="cancelled", at="2026-10-12T21:00:00Z")]
        runs = radar.commit_ci(self.project, "a" * 40, gh=_Gh(rows))
        self.assertEqual(runs["contexts"]["lot/backend-suite"], "failed")
        with self.assertRaises(merge_queue.QueueError):
            merge_queue._required_ci(self.project, "a" * 40, commit_ci=lambda s: runs,
                                     parts=["lot/backend-suite"])

    def test_docs_only_respects_the_classification(self):
        row = self.enter_with("docs", contexts=contexts(**{"lot/docs-gate": "success"}))
        self.assertEqual(row["state"], "queued")

    def test_the_queue_note_keeps_the_provenance_of_the_proof(self):
        self.enter_with("backend", "scripts", "sync", "website", "rollback",
                        contexts=contexts(**{part: "success" for part in self.parts}))
        note = [text for lot, text, actor in self.fake.notes if "queued at" in text][-1]
        self.assertIn("parts scripts/ci/lot-plan.py@%s (5 due)" % self.base()[:12], note)

    def test_a_divergent_versioned_rev_is_refused(self):
        self.project = dataclasses.replace(
            self.project, ci_parts=dataclasses.replace(self.project.ci_parts, rev="release"))
        self.lot_touching("backend")
        self.ready(4)
        with self.assertRaisesRegex(merge_queue.QueueError, "versioned at release"):
            merge_queue.enter(None, 4, repo=self.repo, actor="orch", ci=None,
                              clone=self.clone(), project=self.project,
                              commit_ci=lambda sha: contexts())

    def test_an_unknown_selector_on_the_target_refuses(self):
        os.remove(os.path.join(self.repo, "scripts/ci/lot-plan.py"))
        self.publish_main()
        with self.assertRaisesRegex(merge_queue.QueueError, "cannot be proven"):
            self.enter_with("backend", contexts=contexts(**{"lot/backend-suite": "success"}))

    def test_the_landing_replays_the_proof_on_the_same_target(self):
        self.lot_touching("backend", "scripts", "sync", "website", "rollback")
        self.ready(4)
        merge_queue.enter(None, 4, repo=self.repo, actor="orch", ci=None, clone=self.clone(),
                          project=self.project,
                          commit_ci=lambda sha: contexts(**{p: "success" for p in self.parts}))
        self.fake.queue_rows[4]["rank"] = 1
        before = _git(self.origin, "rev-parse", "main")
        with mock.patch.object(radar, "commit_ci",
                               return_value=contexts(**{"lot/backend-suite": "success"})):
            out = land.land_head(self.cfg, None, self.project, self.fake._row(4),
                                 owner="runner-1")
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("missing or not successful", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)


class CommitCiContextsTest(unittest.TestCase):
    """`commit_ci` rend, libellé par libellé, l'état des statuts du SHA gelé,
    et sépare l'espace des statuts de commit de celui des runs et checks
    (N1 de #1666)."""

    project = radar.Project("n", "/r", github="o/nexlink", ci_required=True)

    def test_statuses_and_skipped_checks_are_read_on_the_exact_sha(self):
        statuses = [{"context": "lot/backend-suite", "state": "success"},
                    {"context": "lot/scripts-suite", "state": "failure"}]
        runs = radar.commit_ci(self.project, "a" * 40,
                               gh=_Gh([_run("ci", conclusion="skipped")], statuses=statuses))
        self.assertEqual(runs["contexts"]["lot/backend-suite"], "success")
        self.assertEqual(runs["contexts"]["lot/scripts-suite"], "failed")
        self.assertEqual(runs["contexts"]["ci"], "failed")
        # N1 de #1666 : l'espace des statuts ne porte que les statuts — le run
        # Actions `ci`, même classé « failed » dans `contexts`, n'y est pas.
        self.assertEqual(runs["statuses"], {"lot/backend-suite": "success",
                                            "lot/scripts-suite": "failed"})

    def test_a_run_of_another_sha_is_never_read(self):
        from ameesh import plan_github
        with self.assertRaises(plan_github.GithubError):
            radar.commit_ci(self.project, "a" * 40,
                            gh=_Gh([dict(_run("ci"), headSha="b" * 40)]))


class ProofCacheTest(_Contract):
    """N2 de #1666 : la preuve est mise en cache par couple (commit de base,
    tête gelée) et par contrat ; une entrée revalidée évite l'extraction de
    l'arbre de la cible aux contrôles répétés (admission puis atterrissages)."""

    def setUp(self):
        super().setUp()
        self.cache = ci_parts.cache_dir(self.cfg)
        self.assertEqual(self.cache, os.path.join(self.cfg.state_dir, "radar", "ci-parts"))

    def counting(self):
        """Compte les extractions et les exécutions du sélecteur du test."""
        real_extract, real_plan = ci_parts.extract, ci_parts._plan_file
        calls = {"extract": 0, "selector": 0}

        def extract(*args, **kwargs):
            calls["extract"] += 1
            return real_extract(*args, **kwargs)

        def plan_file(*args, **kwargs):
            calls["selector"] += 1
            return real_plan(*args, **kwargs)

        patcher = mock.patch.multiple(ci_parts, extract=extract, _plan_file=plan_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def entry(self, proof):
        key = ci_parts._cache_key(self.project.ci_parts, proof["base"], proof["frozen"])
        return os.path.join(self.cache, key + ".json")

    def test_a_second_control_of_the_same_pair_reuses_the_cached_proof(self):
        branch = self.lot_touching("backend", "scripts")
        tip = _git(self.repo, "rev-parse", branch)
        calls = self.counting()
        first = ci_parts.due(self.project, tip, self.base(), self.repo, cache=self.cache)
        second = ci_parts.due(self.project, tip, self.base(), self.repo, cache=self.cache)
        self.assertEqual(first, second)
        self.assertEqual(first["due"], ("lot/backend-suite", "lot/scripts-suite"))
        self.assertEqual(calls, {"extract": 1, "selector": 1})

    def test_another_frozen_head_or_base_is_another_proof(self):
        branch = self.lot_touching("backend")
        first_tip = _git(self.repo, "rev-parse", branch)
        second_tip = _git(self.repo, "rev-parse", self.lot_touching("backend", marker="2"))
        calls = self.counting()
        ci_parts.due(self.project, first_tip, self.base(), self.repo, cache=self.cache)
        ci_parts.due(self.project, second_tip, self.base(), self.repo, cache=self.cache)
        self.assertEqual(calls["extract"], 2)
        _write(self.repo, "backend/marker-base.txt", "x\n")
        self.publish_main()
        ci_parts.due(self.project, second_tip, self.base(), self.repo, cache=self.cache)
        self.assertEqual(calls["extract"], 3)

    def test_another_contract_is_another_proof(self):
        branch = self.lot_touching("backend")
        tip = _git(self.repo, "rev-parse", branch)
        calls = self.counting()
        ci_parts.due(self.project, tip, self.base(), self.repo, cache=self.cache)
        other = dataclasses.replace(
            self.project,
            ci_parts=dataclasses.replace(
                self.project.ci_parts,
                argv=tuple(self.project.ci_parts.argv) + ("--marker", "2")))
        ci_parts.due(other, tip, self.base(), self.repo, cache=self.cache)
        self.assertEqual(calls["extract"], 2)

    def test_a_corrupt_cache_entry_is_ignored_and_recomputed(self):
        branch = self.lot_touching("backend")
        tip = _git(self.repo, "rev-parse", branch)
        calls = self.counting()
        first = ci_parts.due(self.project, tip, self.base(), self.repo, cache=self.cache)
        path = self.entry(first)
        self.assertTrue(os.path.isfile(path))
        junk = ("{not json",
                json.dumps({"version": ci_parts.PARTS_CACHE_VERSION,
                            "schema": "test-lot-plan/1", "due": ["a", "a"],
                            "base": "a" * 40, "merge_base": "a" * 40, "frozen": "a" * 40}))
        for index, text in enumerate(junk):
            with self.subTest(junk=index):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
                again = ci_parts.due(self.project, tip, self.base(), self.repo, cache=self.cache)
                self.assertEqual(again, first)
        self.assertEqual(calls["extract"], 3)          # 1 calcul + 2 recalculs

    def test_an_expired_entry_is_pruned_and_recomputed(self):
        branch = self.lot_touching("backend")
        tip = _git(self.repo, "rev-parse", branch)
        calls = self.counting()
        first = ci_parts.due(self.project, tip, self.base(), self.repo, cache=self.cache)
        old = time.time() - ci_parts.PARTS_CACHE_TTL - 60
        os.utime(self.entry(first), (old, old))
        self.assertEqual(
            ci_parts.due(self.project, tip, self.base(), self.repo, cache=self.cache), first)
        self.assertEqual(calls["extract"], 2)

    def test_an_expired_entry_is_ignored_when_pruning_cannot_remove_it(self):
        branch = self.lot_touching("backend")
        tip = _git(self.repo, "rev-parse", branch)
        calls = self.counting()
        first = ci_parts.due(self.project, tip, self.base(), self.repo, cache=self.cache)
        old = time.time() - ci_parts.PARTS_CACHE_TTL - 60
        path = self.entry(first)
        os.utime(path, (old, old))
        with mock.patch.object(ci_parts.os, "remove", side_effect=PermissionError):
            ci_parts._cache_prune(self.cache, time.time())
        self.assertTrue(os.path.isfile(path))
        # Le nettoyage est un effort : sa panne ne prolonge pas une preuve.
        with mock.patch.object(ci_parts, "_cache_prune"):
            again = ci_parts.due(self.project, tip, self.base(), self.repo, cache=self.cache)
        self.assertEqual(again, first)
        self.assertEqual(calls["extract"], 2)

    def test_without_a_cache_every_control_recomputes(self):
        branch = self.lot_touching("backend")
        tip = _git(self.repo, "rev-parse", branch)
        calls = self.counting()
        ci_parts.due(self.project, tip, self.base(), self.repo)
        ci_parts.due(self.project, tip, self.base(), self.repo)
        self.assertEqual(calls["extract"], 2)
        self.assertFalse(os.path.exists(self.cache))

    def test_a_host_without_state_has_no_cache(self):
        self.assertIsNone(ci_parts.cache_dir(object()))


#: un sélecteur qui lance un petit-fils puis dort : au délai dépassé, le
#: groupe entier doit mourir (N4 de #1666)
GRANDCHILD_SELECTOR = '''\
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c",
                  "import time; time.sleep(600)  # __MARKER__"])
time.sleep(600)
'''


@unittest.skipUnless(os.path.isdir("/proc"), "Linux: /proc is read to find the grandchild")
class TimeoutGroupTest(_Contract):
    """N4 de #1666 : au délai dépassé, le sélecteur **et ses petits-enfants**
    meurent — `subprocess.run(timeout=…)` ne tuait que l'enfant direct."""

    marker = "ameesh1666grandchild"

    def _grandchild(self):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % name, "rb") as handle:
                    if self.marker.encode() in handle.read():
                        return int(name)
            except OSError:
                continue
        return None

    def _reap(self):
        pid = self._grandchild()
        if pid:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def test_the_timeout_kills_the_selector_and_its_grandchild(self):
        _write(self.repo, "scripts/ci/lot-plan.py",
               GRANDCHILD_SELECTOR.replace("__MARKER__", self.marker))
        self.publish_main()
        branch = self.lot_touching("backend")
        tip = _git(self.repo, "rev-parse", branch)
        self.addCleanup(self._reap)
        with mock.patch.object(ci_parts, "PARTS_TIMEOUT", 3):
            with self.assertRaisesRegex(ci_parts.PartsError, "timed out"):
                ci_parts.due(self.project, tip, self.base(), self.repo)
        self.assertIsNone(self._grandchild())
