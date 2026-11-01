# SPDX-License-Identifier: AGPL-3.0-only
"""#5192 — un atterrissage de coupure ne fait pas reposer l'étiquette d'une CI en vol.

Constat (paquet h-19, #5178, 31/10) : la ci-full de la tête part sur la base B ;
la coupure 1.31.0 pose sur la cible son atterrissage (version + notes) ; le
composeur redemande la CI sur la même tête avec la base B + atterrissage. La
demande ne reconnaissait que les runs de la même base : elle reposait le même
libellé, et le groupe de concurrence de tests.yml annulait le run à 21 min de
sa fin. Canon §12 : les seuls commits d'atterrissage d'une coupure ne font pas
une nouvelle composition. Un commit de code, lui, en fait toujours une.

Le dépôt temporaire porte de vrais commits : la règle jouée est celle de la
fusion (`packet_automerge._cutoff_landing_only`), pas une copie.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import itertools
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ameesh import ci, packet_automerge, plan_github, work

from .test_l161g_ci_gel import DEPOT, GEL, LABEL, PR, GelTestCase, Gh, Github, label_run, pull


class Cible:
    """La cible `main` d'un dépôt temporaire : une base, puis ce qui la suit."""

    def __init__(self, test: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory(prefix="ci-5192-")
        test.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.write("pyproject.toml", '[project]\nname = "ameesh"\nversion = "1.30.0"\n')
        self.write("src/ameesh/code.py", "x = 0\n")
        self.base = self.commit("base de composition")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=self.repo,
                              env=self.env, text=True, capture_output=True,
                              check=True).stdout.strip()

    def write(self, path: str, content: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def atterrissage(self, version: str = "1.31.0") -> str:
        self.write("pyproject.toml", '[project]\nname = "ameesh"\nversion = "%s"\n' % version)
        self.write("docs/versions/%s.md" % version, "notes %s\n" % version)
        return self.commit("release: %s (coupure de 23:00 du 31/10/2026) [skip ci]\n\n"
                           "ameesh-release: %s" % (version, version))

    def code(self) -> str:
        self.write("src/ameesh/code.py", "x = 1\n")
        return self.commit("un lot fusionné hors paquet")


class CutoffCoverageTest(GelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cible = Cible(self)
        self.clone = str(self.cible.repo)
        self.fetched: list[tuple] = []
        self.work.enter_context(mock.patch.object(
            packet_automerge, "_gate_repo", lambda cfg, db, row, slug, checkout="": self.clone))
        self.work.enter_context(mock.patch.object(
            packet_automerge, "_fetch_into",
            lambda repo, branches: self.fetched.append(tuple(branches)) or ""))
        self.notes: list[str] = []
        self.seq = itertools.count(1)
        self.work.enter_context(mock.patch.object(work, "note", self._note))

    def _note(self, db, item_id, text, **kw):
        self.notes.append(text)
        self.events.append({"id": next(self.seq), "note": text, "created_ts": time.time()})
        return True

    def api(self, base: str, *runs) -> Github:
        """La PR et la cible sur `base` ; les runs de la tête gelée."""
        return Github(base=base, pulls=[pull(base=base)], runs=list(runs))

    def vivant(self, base: str):
        return label_run(7, base=base, statut="in_progress", conclusion=None)

    def run_cmd(self, api, gh) -> int:
        """`ameesh ci run` (lot, exception `unblock` déclarée, comme RequestTest)."""
        self.events.append({"id": next(self.seq), "created_ts": time.time(),
                            "note": "packet-exception unblock sha=%s reason=test" % GEL})
        args = argparse.Namespace(id=1343, sha="", avis=1, exception="", reason="",
                                  full=False, force=False, repo=DEPOT, dry_run=False,
                                  json=True, prevalidation_repo="", prevalidation_map="")
        with mock.patch("ameesh.db.open_db", lambda cfg: self.db), \
                mock.patch.object(plan_github, "Gh", lambda *a, **k: gh), \
                mock.patch.object(plan_github, "_json",
                                  lambda g, argv, *a, **k: api(argv[-1])), \
                contextlib.redirect_stdout(io.StringIO()):
            return ci.cmd_ci_run(None, args)

    # -- le constat ------------------------------------------------------

    def test_un_run_vivant_d_avant_l_atterrissage_couvre_la_composition(self):
        self.gele()
        landed = self.cible.atterrissage()
        decision = self.gel(self.api(landed, self.vivant(self.cible.base)))
        self.assertEqual(decision["action"], "running")
        self.assertEqual(decision["base"], landed)
        self.assertEqual(decision["cutoff_bases"], [self.cible.base])
        self.assertIn("only by cutoff landings", decision["why"])
        self.assertEqual(self.fetched, [("main",)])

    def test_la_demande_ne_pose_ni_n_annule_rien(self):
        self.gele()
        landed = self.cible.atterrissage()
        gh = Gh()
        self.assertEqual(self.run_cmd(self.api(landed, self.vivant(self.cible.base)), gh), 0)
        self.assertEqual(gh.poses(), [], "l'étiquette reposée annulerait le run en vol")
        self.assertEqual(gh.annulations(), [])

    def test_un_vert_d_avant_deux_atterrissages_couvre_aussi(self):
        self.gele()
        self.cible.atterrissage("1.31.0")
        landed = self.cible.atterrissage("1.32.0")
        decision = self.gel(self.api(landed, label_run(7, base=self.cible.base)))
        self.assertEqual(decision["action"], "green")
        self.assertIn("only by cutoff landings", decision["why"])

    # -- ce qui reste une nouvelle composition ----------------------------

    def test_un_commit_de_code_fait_une_nouvelle_composition(self):
        self.gele()
        moved = self.cible.code()
        decision = self.gel(self.api(moved, self.vivant(self.cible.base)))
        self.assertEqual(decision["action"], "request")
        self.assertNotIn("cutoff_bases", decision)
        gh = Gh()
        self.assertEqual(self.run_cmd(self.api(moved, self.vivant(self.cible.base)), gh), 0)
        self.assertEqual(gh.poses(), [LABEL])

    def test_du_code_apres_l_atterrissage_fait_une_nouvelle_composition(self):
        self.gele()
        self.cible.atterrissage()
        moved = self.cible.code()
        self.assertEqual(self.gel(self.api(moved, self.vivant(self.cible.base)))["action"],
                         "request")

    def test_un_run_rouge_d_avant_l_atterrissage_ne_couvre_pas(self):
        # seuls un run vivant ou vert couvrent ; un rouge ou un annulé laisse
        # la demande partir sur la base actuelle, comme avant #5192
        self.gele()
        landed = self.cible.atterrissage()
        for conclusion in ("failure", "cancelled"):
            with self.subTest(conclusion=conclusion):
                decision = self.gel(self.api(
                    landed, label_run(7, base=self.cible.base, conclusion=conclusion)))
                self.assertEqual(decision["action"], "request")

    def test_sans_clone_lisible_rien_ne_couvre(self):
        self.gele()
        landed = self.cible.atterrissage()
        self.clone = ""
        decision = self.gel(self.api(landed, self.vivant(self.cible.base)))
        self.assertEqual(decision["action"], "request")

    def test_un_mode_lot_ne_couvre_pas_un_full(self):
        self.gele()
        landed = self.cible.atterrissage()
        decision = self.gel(self.api(landed, self.vivant(self.cible.base)),
                            full=True, reason="paquet §12")
        self.assertEqual((decision["action"], decision["mode"]), ("request", "full"))

    # -- un run apparu après le plan (B de cx3 sur 893d4aa5a3ce) -----------

    def tardif(self, landed: str) -> "Tardif":
        return Tardif(base=landed, pulls=[pull(base=landed)],
                      runs=[self.vivant(self.cible.base)])

    def test_la_phase_critique_relit_la_couverture_d_un_run_apparu_apres_le_plan(self):
        self.gele()
        landed = self.cible.atterrissage()
        api = self.tardif(landed)
        decision = self.gel(api)
        self.assertEqual(decision["action"], "request")
        self.assertNotIn("cutoff_bases", decision)
        api.visible = True
        gh = Gh()
        result = ci._request_once(self.db, 1343, decision, api=api, gh=gh, force=False)
        self.assertEqual((result["requested"], result["action"]), (False, "running"))
        self.assertEqual(gh.poses(), [])
        # sans clone lisible, la phase critique ne prouve rien : elle pose
        self.clone = ""
        result = ci._request_once(self.db, 1343, decision, api=api, gh=gh, force=False)
        self.assertEqual(result["action"], "request")
        self.assertEqual(gh.poses(), [LABEL])

    def test_la_demande_n_annule_pas_un_run_apparu_apres_le_plan(self):
        # `cmd_ci_run` annule les périmés avant la phase critique : un run de la
        # base d'avant l'atterrissage n'en est pas un
        self.gele()
        landed = self.cible.atterrissage()
        api = self.tardif(landed)
        real = ci.gel_plan

        def plan_then_appear(*a, **k):
            decision = real(*a, **k)
            api.visible = True
            return decision

        gh = Gh()
        with mock.patch.object(ci, "gel_plan", plan_then_appear):
            self.assertEqual(self.run_cmd(api, gh), 0)
        self.assertEqual(gh.annulations(), [], "le run en vol n'est pas périmé")
        self.assertEqual(gh.poses(), [], "l'étiquette reposée l'annulerait")

    def test_un_run_d_une_base_de_code_reste_perime_pour_l_annulation(self):
        self.gele()
        moved = self.cible.code()
        report = ci.cancel_plan(self.db, None, 1343, api=self.api(
            moved, self.vivant(self.cible.base)), repo=DEPOT, number=PR, base=moved)
        self.assertEqual([r["id"] for r in report["runs"]], [7])


class Tardif(Github):
    """La liste des runs de la tête, vide tant que `visible` est faux."""

    visible = False

    def rows(self) -> list[dict]:
        return list(self.runs) if self.visible else []


if __name__ == "__main__":
    unittest.main()
