# SPDX-License-Identifier: AGPL-3.0-only
"""#5729 — réutiliser la qualification du paquet sur le push main à arbre égal.

Trois décisions, testées sans réseau, sans Actions et sans base :

* `scripts/ci/qualified-tree.py` : la preuve (commit poussé = merge commit
  d'une PR `integration/**` fusionnée sur `main`, arbre identique à la tête,
  check `tests` et run `tests.yml` verts sur cette tête). Toute lecture
  incomplète, ambiguë ou en échec rend `reuse=false` — fermé par défaut ;
* `scripts/test-plan.py` : `--reuse` ne vide les parts que sur un push de
  `main` en mode auto ; partout ailleurs le plan ordinaire (suite complète)
  s'applique ;
* `scripts/ci-agregat.py` : la voie `--qualified` ne verdit que si la preuve
  est complète et qu'aucune part n'a été planifiée ni lancée ; une part qui a
  tourné, un plan rouge ou un événement qui n'est pas le push de main
  rougissent l'agrégat.

Le workflow est aussi vérifié : job `qualification` borné (timeout, droits),
`plan` dépendant de lui et `tests` lisant la preuve.
"""
from __future__ import annotations

import datetime
import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qualified = _load("qualified_tree_5729", "scripts/ci/qualified-tree.py")
agregat = _load("ci_agregat_5729", "scripts/ci-agregat.py")
test_plan = _load("test_plan_5729", "scripts/test-plan.py")

SLUG = "manaty/ameesh"
SERVER = "https://github.com"
SHA = "a" * 40       # le merge commit poussé sur main
TREE = "b" * 40      # l'arbre, identique à celui de la tête du paquet
HEAD = "c" * 40      # la tête de la PR de paquet
PACKET = 4349
RUN_ID = 41779932222
BRANCH = "integration/paquet-h-20261104-05"


def packet_pr(**over) -> dict:
    row = {
        "number": PACKET,
        "state": "closed",
        "merged": True,
        "merged_at": "2026-11-04T05:05:00Z",
        "merge_commit_sha": SHA,
        "base": {"ref": "main", "repo": {"full_name": SLUG}},
        "head": {"ref": BRANCH, "sha": HEAD,
                 "repo": {"full_name": SLUG}},
    }
    row.update(over)
    return row


def tests_check(**over) -> dict:
    row = {
        "id": 991,
        "name": "tests",
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-11-04T04:58:00Z",
        "completed_at": "2026-11-04T05:01:00Z",
        "app": {"id": 15368, "slug": "github-actions"},
        "details_url": "%s/%s/actions/runs/%d/job/77" % (SERVER, SLUG, RUN_ID),
    }
    row.update(over)
    return row


def tests_run(**over) -> dict:
    row = {
        "id": RUN_ID,
        "head_sha": HEAD,
        "head_branch": BRANCH,
        "event": "pull_request",
        "path": ".github/workflows/tests.yml",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "repository": {"full_name": SLUG},
    }
    row.update(over)
    return row


def routes(**over) -> dict:
    table = {
        "/repos/%s/commits/%s/pulls" % (SLUG, SHA): [packet_pr()],
        "/repos/%s/pulls/%d" % (SLUG, PACKET): packet_pr(),
        "/repos/%s/git/commits/%s" % (SLUG, HEAD): {
            "sha": HEAD, "tree": {"sha": TREE}},
        "/repos/%s/commits/%s/check-runs" % (SLUG, HEAD): {
            "total_count": 1, "check_runs": [tests_check()]},
        "/repos/%s/actions/runs/%d" % (SLUG, RUN_ID): tests_run(),
    }
    table.update(over)
    return table


class FakeAPI:
    """Un lecteur `chemin -> corps JSON` ; toute route absente est un 404."""

    def __init__(self, table: dict | None = None):
        self.table = dict(routes() if table is None else table)
        self.calls = []

    def get(self, path: str):
        self.calls.append(path)
        key = path.split("?", 1)[0]
        if key not in self.table:
            raise qualified.ProofError("api_404")
        return self.table[key]


def qualify(**over) -> dict:
    values = dict(event="push", ref="refs/heads/main", repo=SLUG, sha=SHA,
                  tree=TREE, get=FakeAPI().get)
    values.update(over)
    return qualified.qualify(**values)


class ProofTest(unittest.TestCase):
    """La preuve d'arbre qualifié, cas heureux puis chaque refus."""

    def test_arbre_identique_et_run_vert_reutilise(self):
        result = qualify()
        self.assertTrue(result["reuse"], result)
        self.assertEqual(result["reason"], "qualified_packet_tree")
        self.assertEqual((result["packet"], result["head"], result["tree"],
                          result["run"]), (PACKET, HEAD, TREE, RUN_ID))

    def test_un_autre_evenement_ne_reutilise_jamais(self):
        self.assertEqual(qualify(event="pull_request")["reason"],
                         "ordinary_event")
        self.assertEqual(qualify(ref="refs/heads/release/1.2")["reason"],
                         "ordinary_event")
        self.assertEqual(qualify(event="workflow_dispatch")["reason"],
                         "ordinary_event")

    def test_identite_illisible(self):
        self.assertEqual(qualify(repo="pas-un-depot")["reason"],
                         "unknown_identity")
        self.assertEqual(qualify(sha="abc")["reason"], "unknown_identity")
        self.assertEqual(qualify(tree="")["reason"], "unknown_identity")

    def test_pas_de_pr_ou_plusieurs_ne_prouvent_rien(self):
        self.assertEqual(qualify(get=FakeAPI({
            "/repos/%s/commits/%s/pulls" % (SLUG, SHA): []}).get)["reason"],
            "no_unique_merged_packet")
        self.assertEqual(qualify(get=FakeAPI(routes(**{
            "/repos/%s/commits/%s/pulls" % (SLUG, SHA):
                [packet_pr(), packet_pr(number=4350)]})).get)["reason"],
            "no_unique_merged_packet")

    def test_pr_non_fusionnee_ou_autre_merge(self):
        cases = [
            {"state": "open"},
            {"merged_at": None},
            {"merge_commit_sha": "d" * 40},
            {"base": {"ref": "develop", "repo": {"full_name": SLUG}}},
            {"head": {"ref": "release/1.2", "sha": HEAD,
                      "repo": {"full_name": SLUG}}},
            {"head": {"ref": BRANCH, "sha": HEAD,
                      "repo": {"full_name": "fork/ameesh"}}},
        ]
        for case in cases:
            with self.subTest(case=case):
                table = routes(**{
                    "/repos/%s/commits/%s/pulls" % (SLUG, SHA):
                        [packet_pr(**case)]})
                self.assertFalse(qualify(get=FakeAPI(table).get)["reuse"])

    def test_l_endpoint_des_pr_associees_rend_merged_null(self):
        # Constaté à la forge sur #4349 : la liste rend `merged: null` même
        # pour une PR fusionnée ; la preuve lit `merged_at` et le merge commit.
        table = routes(**{
            "/repos/%s/commits/%s/pulls" % (SLUG, SHA):
                [packet_pr(merged=None)]})
        self.assertTrue(qualify(get=FakeAPI(table).get)["reuse"])

    def test_la_pr_complete_doit_etre_fusionnee(self):
        table = routes(**{
            "/repos/%s/pulls/%d" % (SLUG, PACKET): packet_pr(merged=False)})
        self.assertEqual(qualify(get=FakeAPI(table).get)["reason"],
                         "packet_not_merged")

    def test_arbre_different(self):
        table = routes(**{
            "/repos/%s/git/commits/%s" % (SLUG, HEAD): {
                "sha": HEAD, "tree": {"sha": "e" * 40}}})
        self.assertEqual(qualify(get=FakeAPI(table).get)["reason"],
                         "different_tree")

    def test_check_present_puis_chaque_refus(self):
        cases = [
            ({"status": "in_progress", "completed_at": None},
             "check_not_completed"),
            ({"conclusion": "failure"}, "tests_check_not_green"),
            ({"conclusion": "cancelled"}, "tests_check_not_green"),
            ({"app": {"id": 1, "slug": "autre-app"}},
             "tests_check_not_actions"),
            ({"details_url": None}, "check_run_url_missing"),
            ({"details_url": "https://example.invalid/actions/runs/1"},
             "status_run_mismatch"),
            ({"head_sha": "f" * 40}, "check_other_head"),
        ]
        for over, reason in cases:
            with self.subTest(reason=reason):
                table = routes(**{
                    "/repos/%s/commits/%s/check-runs" % (SLUG, HEAD): {
                        "total_count": 1, "check_runs": [tests_check(**over)]}})
                self.assertEqual(qualify(get=FakeAPI(table).get)["reason"],
                                 reason)

    def test_le_dernier_essai_fait_foi(self):
        # Un succès ancien ne rachète pas un essai plus récent en échec.
        table = routes(**{
            "/repos/%s/commits/%s/check-runs" % (SLUG, HEAD): {
                "total_count": 2,
                "check_runs": [
                    tests_check(),
                    tests_check(id=992, conclusion="failure",
                                started_at="2026-11-04T05:05:00Z",
                                completed_at="2026-11-04T05:06:00Z"),
                ]}})
        self.assertEqual(qualify(get=FakeAPI(table).get)["reason"],
                         "tests_check_not_green")

    def test_deux_essais_ambigus_au_meme_instant(self):
        table = routes(**{
            "/repos/%s/commits/%s/check-runs" % (SLUG, HEAD): {
                "total_count": 2,
                "check_runs": [
                    tests_check(),
                    tests_check(id=992, conclusion="failure"),
                ]}})
        self.assertEqual(qualify(get=FakeAPI(table).get)["reason"],
                         "ambiguous_check")

    def test_le_run_doit_etre_le_run_tests_de_la_meme_tete(self):
        cases = [
            ({"conclusion": "failure"}, "run_not_green"),
            ({"status": "in_progress"}, "run_not_completed"),
            ({"event": "push"}, "run_not_pull_request"),
            ({"path": ".github/workflows/paquet.yml"}, "run_not_tests"),
            ({"head_sha": "f" * 40}, "run_other_head"),
            ({"head_branch": "lot/autre"}, "run_other_branch"),
            ({"repository": {"full_name": "manaty/autre"}}, "run_other_repo"),
        ]
        for over, reason in cases:
            with self.subTest(reason=reason):
                table = routes(**{
                    "/repos/%s/actions/runs/%d" % (SLUG, RUN_ID):
                        tests_run(**over)})
                self.assertEqual(qualify(get=FakeAPI(table).get)["reason"],
                                 reason)

    def test_lecture_impossible_ferme_la_porte(self):
        self.assertEqual(qualify(get=FakeAPI({}).get)["reason"], "api_404")

        def unreachable(path):
            raise OSError("réseau")

        self.assertEqual(qualify(get=unreachable)["reason"],
                         "qualification_unavailable")

    def test_pagination_bornee(self):
        table = routes(**{
            "/repos/%s/commits/%s/check-runs" % (SLUG, HEAD): {
                "total_count": 501, "check_runs": []}})
        self.assertEqual(qualify(get=FakeAPI(table).get)["reason"],
                         "proof_too_large")

    def test_une_sortie_de_qualification_complete(self):
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
               "GITHUB_REPOSITORY": SLUG, "GITHUB_SHA": SHA,
               "GITHUB_SERVER_URL": SERVER}
        result = qualified.decide(env, get=FakeAPI().get,
                                  git=lambda *args: SHA if args[-1] == "HEAD"
                                  else TREE)
        self.assertTrue(result["reuse"])
        self.assertEqual(result["detail"],
                         "paquet #%d, tête %s, arbre %s, run tests %d"
                         % (PACKET, HEAD, TREE, RUN_ID))

    def test_une_sortie_de_qualification_fermee(self):
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
               "GITHUB_REPOSITORY": SLUG, "GITHUB_SHA": SHA}
        result = qualified.decide(env, get=FakeAPI().get,
                                  git=lambda *args: "0" * 40)
        self.assertEqual(result["reason"], "checkout_mismatch")
        self.assertFalse(result["reuse"])


class QualificationMainTest(unittest.TestCase):
    """`main` écrit les sorties attendues et ne lève jamais sur une preuve."""

    def test_sorties_et_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "out"
            summary = pathlib.Path(tmp) / "summary"
            with mock.patch.object(qualified, "decide", return_value={
                    "reuse": True, "reason": "qualified_packet_tree",
                    "packet": PACKET, "head": HEAD, "tree": TREE,
                    "run": RUN_ID}):
                code = qualified.main(["--github-output", str(out),
                                       "--summary", str(summary)], env={})
            self.assertEqual(code, 0)
            text = out.read_text(encoding="utf-8")
            self.assertIn("reuse=true", text)
            self.assertIn("packet=%d" % PACKET, text)
            self.assertIn("head=%s" % HEAD, text)
            self.assertIn("Arbre qualifié", summary.read_text(encoding="utf-8"))

    def test_preuve_absente_ecrit_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "out"
            with mock.patch.object(qualified, "decide",
                                   return_value={"reuse": False,
                                                 "reason": "different_tree"}):
                code = qualified.main(["--github-output", str(out)], env={})
            self.assertEqual(code, 0)
            self.assertIn("reuse=false", out.read_text(encoding="utf-8"))


class PlanReuseTest(unittest.TestCase):
    """Le plan ne vide les parts que sur un push de main en mode auto."""

    NOW = datetime.datetime(2026, 11, 4, 6, 0, tzinfo=datetime.timezone.utc)

    def plan(self, **over):
        values = dict(event="push", card=None, now=self.NOW, head="HEAD",
                      repo=REPO, base="HEAD^1", ref="refs/heads/main",
                      mode="auto", reuse=True,
                      reuse_detail="paquet #4349, tête %s, arbre %s"
                                   % (HEAD, TREE))
        values.update(over)
        return test_plan.make_plan(**values)

    def test_push_main_reutilise_et_vide_les_deux_parts(self):
        plan = self.plan()
        self.assertTrue(plan["reuse"])
        self.assertEqual(plan["parts"]["psycopg"], [])
        self.assertEqual(plan["parts"]["psql"], [])
        outputs = test_plan.outputs(plan)
        self.assertEqual((outputs["psycopg"], outputs["psycopg_parts"]),
                         ("[]", "0"))
        self.assertEqual((outputs["psql"], outputs["psql_parts"]), ("[]", "0"))
        self.assertEqual(outputs["reuse"], "true")
        self.assertEqual(outputs["code"], "true")
        self.assertEqual(outputs["version_only"], "false")
        self.assertIn("arbre qualifié", test_plan.summary(plan))

    def test_une_release_ou_une_pr_garde_la_suite(self):
        for over in ({"ref": "refs/heads/release/1.2"},
                     {"event": "pull_request", "ref": "refs/pull/1/merge",
                      "base": "HEAD^1", "head_ref": "lot/5729", "mode": "lot"}):
            with self.subTest(over=over):
                plan = self.plan(**over)
                self.assertFalse(plan.get("reuse"))
                self.assertTrue(plan["parts"]["psycopg"])

    def test_un_mode_explicite_ne_reutilise_pas(self):
        plan = self.plan(mode="full")
        self.assertFalse(plan.get("reuse"))
        self.assertTrue(plan["parts"]["psycopg"])


def classified(**over) -> dict:
    values = dict(
        event="push", ref="refs/heads/main", plan="success",
        version="skipped", version_only="false", docs_only="false",
        doc_readers="skipped", psycopg="skipped", psycopg_parts="0",
        psql="skipped", psql_parts="0", platform_macos="skipped",
        fs_class="skipped", qualified="true",
        qualified_packet=str(PACKET), qualified_head=HEAD,
        qualified_tree=TREE)
    values.update(over)
    return agregat.classify(**values)


class QualifiedAggregateTest(unittest.TestCase):
    """`tests` vert seulement si rien n'a tourné et si la preuve est complète."""

    def test_arbre_qualifie_verdit(self):
        result = classified()
        self.assertEqual(result["state"], agregat.GREEN, result)
        self.assertEqual(result["qualified"]["packet"], PACKET)

    def test_une_part_lancee_rougit(self):
        for over in ({"psycopg": "success"},
                     {"psql": "success"},
                     {"platform_macos": "success"},
                     {"doc_readers": "success"},
                     {"psycopg_parts": "8"}):
            with self.subTest(over=over):
                self.assertEqual(classified(**over)["state"], agregat.RED)

    def test_plan_rouge_ou_evenement_autre_rougit(self):
        for over in ({"plan": "failure"}, {"event": "pull_request"},
                     {"ref": "refs/heads/release/1.2"},
                     {"version_only": "true"}, {"docs_only": "true"}):
            with self.subTest(over=over):
                self.assertEqual(classified(**over)["state"], agregat.RED)

    def test_preuve_incomplete_rougit(self):
        for over in ({"qualified_packet": ""},
                     {"qualified_head": ""},
                     {"qualified_tree": "abc"}):
            with self.subTest(over=over):
                self.assertEqual(classified(**over)["state"], agregat.RED)

    def test_publication_du_detail_qualifie(self):
        created = []

        class Gh:
            def create(self, name, conclusion, title, summary):
                created.append((name, conclusion, title, summary))
                return 1

        code = agregat.main([
            "--event", "push", "--ref", "refs/heads/main",
            "--plan", "success", "--version", "skipped",
            "--version-only", "false", "--docs-only", "false",
            "--doc-readers", "skipped", "--psycopg", "skipped",
            "--psycopg-parts", "0", "--psql", "skipped", "--psql-parts", "0",
            "--plateforme-macos", "skipped", "--autre-fs", "skipped",
            "--qualified", "true", "--qualified-packet", str(PACKET),
            "--qualified-head", HEAD, "--qualified-tree", TREE,
            "--publish"], gh=Gh(), jobs=[])
        self.assertEqual(code, 0)
        [detail] = created
        self.assertEqual(detail[:2], (agregat.DETAIL_CHECK, "success"))
        self.assertIn("arbre qualifié", detail[2])
        self.assertIn("paquet #%d" % PACKET, detail[3])


class WorkflowTest(unittest.TestCase):
    """Le câblage : job borné, plan dépendant, agrégat qui lit la preuve."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = yaml.safe_load(cls.text)["jobs"]

    def test_qualification_tourne_sur_tous_les_evenements(self):
        # actions/runner#491 : un job sauté fait sauter **transitivement** les
        # jobs aval de `plan` sans fonction de statut. La qualification n'a
        # donc aucune porte d'événement : son script rend `ordinary_event`
        # hors push de main, sans lecture git ni API.
        job = self.jobs["qualification"]
        self.assertNotIn("if", job)
        self.assertEqual(job["timeout-minutes"], 5)
        self.assertEqual(job["permissions"]["checks"], "read")
        self.assertEqual(job["permissions"]["actions"], "read")
        steps = job["steps"]
        proof = next(step for step in steps if step.get("id") == "proof")
        self.assertIn("scripts/ci/qualified-tree.py", proof["run"])

    def test_plan_depend_de_la_qualification_et_lit_la_preuve(self):
        job = self.jobs["plan"]
        self.assertEqual(job["needs"], "qualification")
        self.assertIn("!cancelled()", job["if"])
        self.assertIn("needs.qualification.outputs.reuse",
                      job["env"]["AMEESH_CI_REUSE"])
        step = next(step for step in job["steps"]
                    if step.get("id") == "plan")
        self.assertIn("--reuse", step["run"])

    def test_les_jobs_facultatifs_ne_tournent_pas_sur_un_arbre_reutilise(self):
        for name in ("platform-macos", "bilan", "python314", "profil-nu"):
            with self.subTest(job=name):
                self.assertIn("needs.plan.outputs.reuse != 'true'",
                              self.jobs[name]["if"])

    def test_l_agregat_recoit_la_preuve(self):
        job = self.jobs["tests"]
        self.assertIn("qualification", job["needs"])
        step = next(step for step in job["steps"]
                    if step.get("name") == "bilan des parts")
        for option in ("--qualified ", "--qualified-packet ",
                       "--qualified-head ", "--qualified-tree ", "--ref "):
            self.assertIn(option, step["run"])
        # N P2 d'auditeur : la voie qualifiée suit la décision **appliquée**
        # par le plan, pas seulement la preuve du job de qualification.
        self.assertIn("needs.plan.outputs.reuse", step["run"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
