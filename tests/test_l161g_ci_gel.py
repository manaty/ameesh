# SPDX-License-Identifier: AGPL-3.0-only
"""#1343 — la décision de `ameesh ci run` / `ci cancel` : gel, avis, idempotence.

Le canon §3 (owner 14/10 21:45) met la relecture avant la CI : une poussée de
lot ne lance plus rien, et la CI du lot (impactés + contrat) part au gel, une
fois, sur la tête gelée. Ces témoins tiennent la décision seule
(`ci.gel_plan`, `ci.cancel_plan`), sans réseau ni base : le faux `api` rend
les quatre lectures GitHub (tête de branche, exécutions, merge-base, contenu
du workflow) et les jalons du lot sont posés à la main.

Chaque refus est vérifié par son message *et* par l'absence de lecture de la
base ou du workflow — donc aucun dispatch possible ; chaque acceptation prouve
que c'est le SHA gelé qui part, avec sa base. Le faux `head_sha=`/`branch=`
applique le filtre de la requête comme GitHub le ferait : un témoin ne passe
pas parce que le faux aurait rendu des exécutions que la vraie requête
n'aurait pas rendues.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import re
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ameesh import ci, mesh_cli, plan_github, work  # noqa: E402

DEPOT = "manaty/ameesh"
GEL = "a" * 40
TETE = "b" * 40
BASE = "c" * 40
AUTRE = "d" * 40
BRANCHE = "lot/1343-ci-au-gel"


def lot(**extra) -> dict:
    """Le lot relu : une branche de lot, une cible main, un assigné."""
    row = {"id": 1343, "app": "ameesh", "assignee": "mty-amh-impl-04",
           "branch": BRANCHE, "branch_target": "main",
           "issue_ref": "manaty/ameesh#1040"}
    row.update(extra)
    return row


def jalon(kind: str, *, sha: str = "", actor: str = "",
          verdict: str | None = None) -> dict:
    """Un jalon tel que `work.milestones` le rend (les clés lues suffisent)."""
    return {"kind": kind, "sha": sha, "actor": actor, "verdict": verdict}


def execution(ident: int = 1, *, statut: str = "completed",
              conclusion: str | None = "success", sha: str = GEL,
              branche: str = BRANCHE) -> dict:
    """Une exécution de `tests.yml` telle que GitHub la rend."""
    return {"id": ident, "status": statut, "conclusion": conclusion, "head_sha": sha,
            "head_branch": branche, "event": "workflow_dispatch",
            "html_url": "https://github.com/%s/actions/runs/%d" % (DEPOT, ident)}


class Github:
    """Les lectures GitHub de `gel_plan`/`cancel_plan`, sans réseau."""

    def __init__(self, *, head: str = GEL, runs=(), merge_base: str = BASE,
                 workflow: str | None = "#1343 : declencheur du gel\n"):
        self.head = head
        self.runs = list(runs)
        self.merge_base = merge_base
        self.workflow = workflow
        self.paths: list[str] = []

    def __call__(self, path: str) -> dict:
        self.paths.append(path)
        if path.startswith("repos/%s/branches/" % DEPOT):
            return {"commit": {"sha": self.head}}
        if "/actions/workflows/tests.yml/runs?" in path:
            runs = self.runs
            found = re.search(r"[?&]head_sha=([0-9a-f]{40})", path)
            if found:
                runs = [r for r in runs if (r.get("head_sha") or "") == found.group(1)]
            found = re.search(r"[?&]branch=([^&]+)", path)
            if found:
                branche = unquote(found.group(1))
                runs = [r for r in runs if (r.get("head_branch") or "") == branche]
            return {"workflow_runs": runs}
        if path.startswith("repos/%s/compare/" % DEPOT):
            return {"merge_base_commit": {"sha": self.merge_base}}
        if "/contents/.github/workflows/tests.yml" in path:
            if self.workflow is None:
                return {}
            return {"content": base64.b64encode(self.workflow.encode()).decode()}
        raise AssertionError("chemin GitHub inattendu : %s" % path)

    def ecrit(self) -> list[str]:
        """Les lectures faites seulement avant un dispatch (base, workflow)."""
        return [p for p in self.paths if "/compare/" in p or "/contents/" in p]


class Gh:
    """Le `gh` des écritures : enregistre les argv, réussit (sauf refus)."""

    def __init__(self, refuse_dispatch: bool = False):
        self.calls: list[list[str]] = []
        self.refuse_dispatch = refuse_dispatch

    def run(self, argv: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if self.refuse_dispatch and argv and argv[0] == "workflow":
            return 1, "", "Unexpected inputs provided: ['exception']"
        return 0, "", ""

    def annulations(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["api", "-X"]]

    def dispatch(self) -> list[str]:
        return next((c for c in self.calls if c and c[0] == "workflow"), [])


class GelTestCase(unittest.TestCase):
    """Le faux `work` : un lot et ses jalons, sans base ni courrier."""

    def setUp(self) -> None:
        self.item = lot()
        self.jalons: list[dict] = []
        self.db = object()
        self.work = contextlib.ExitStack()
        self.addCleanup(self.work.close)
        self.work.enter_context(mock.patch.object(work, "get", self._get))
        self.work.enter_context(mock.patch.object(work, "milestones", self._milestones))

    def _get(self, db, item_id):
        return self.item if int(item_id) == self.item["id"] else None

    def _milestones(self, db, item_id, limit=100):
        return list(self.jalons)

    def gele(self, sha: str = GEL, acteur: str = "relecteur") -> None:
        """Le lot gelé sur `sha`, avec un verdict `ok` indépendant dessus."""
        self.jalons += [jalon("frozen", sha=sha, actor="auteur"),
                        jalon("verdict", sha=sha, actor=acteur, verdict="ok")]

    def gel(self, api, **kw) -> dict:
        args = {"sha": "", "avis": 1, "exception": "", "reason": "", "full": False,
                "force": False, "repo": DEPOT}
        args.update(kw)
        return ci.gel_plan(self.db, None, self.item["id"], api=api, **args)

    def annule(self, api, **kw) -> dict:
        args = {"all_runs": False, "repo": DEPOT}
        args.update(kw)
        return ci.cancel_plan(self.db, None, self.item["id"], api=api, **args)


class GelPlanTest(GelTestCase):
    """`ci.gel_plan` : ce qui refuse, ce qui ne redispatch pas, ce qui part."""

    def test_un_lot_sans_gel_est_refuse_avant_toute_lecture(self):
        api = Github()
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("is not frozen", str(caught.exception))
        self.assertEqual(api.paths, [], "rien ne doit être lu sans gel")

    def test_une_tete_bougee_est_refusee_jamais_un_vert_ailleurs(self):
        self.gele()
        api = Github(head=TETE)
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("the ref moved", str(caught.exception))
        self.assertEqual(api.ecrit(), [], "aucune base, aucun workflow lu")

    def test_un_avis_independant_dispatch_sur_le_sha_gele(self):
        self.gele()
        api = Github()
        decision = self.gel(api)
        self.assertEqual(decision["action"], "dispatch")
        self.assertEqual(decision["sha"], GEL)
        self.assertEqual(decision["base"], BASE)
        self.assertEqual(decision["mode"], "lot")
        self.assertEqual(decision["verdicts"], ["relecteur"])
        self.assertIn("compare/main...%s" % GEL, " ".join(api.paths))

    def test_l_avis_de_l_auteur_ne_compte_pas(self):
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=GEL, actor=self.item["assignee"], verdict="ok")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github())
        self.assertIn("0 independent verdict(s)", str(caught.exception))

    def test_deux_avis_du_meme_acteur_ne_font_qu_un(self):
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=GEL, actor="relecteur", verdict="ok"),
                        jalon("verdict", sha=GEL, actor="relecteur", verdict="ok")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(), avis=2)
        self.assertIn("1 independent verdict(s)", str(caught.exception))
        self.jalons.append(jalon("verdict", sha=GEL, actor="second", verdict="ok"))
        self.assertEqual(self.gel(Github(), avis=2)["action"], "dispatch")

    def test_un_avis_sur_une_autre_tete_ne_compte_pas(self):
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=TETE, actor="relecteur", verdict="ok")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github())
        self.assertIn("0 independent verdict(s)", str(caught.exception))

    def test_un_verdict_bloque_ne_compte_pas(self):
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=GEL, actor="relecteur", verdict="blocked")]
        with self.assertRaises(ci.GelError):
            self.gel(Github())

    def test_un_lot_leger_dispatch_sans_avis(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        self.assertEqual(self.gel(Github(), avis=0)["action"], "dispatch")

    def test_un_vert_deja_obtenu_ne_redispatch_pas(self):
        self.gele()
        api = Github(runs=[execution(7, conclusion="success")])
        decision = self.gel(api)
        self.assertEqual((decision["action"], decision["run"]["id"]), ("green", 7))
        self.assertEqual(api.ecrit(), [], "un vert ne relit ni base ni workflow")

    def test_une_execution_en_cours_ne_redispatch_pas(self):
        self.gele()
        api = Github(runs=[execution(8, statut="in_progress", conclusion=None)])
        self.assertEqual(self.gel(api)["action"], "running")

    def test_un_rouge_demande_force(self):
        self.gele()
        rouge = [execution(9, conclusion="failure")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(runs=rouge))
        self.assertIn("--force", str(caught.exception))
        self.assertEqual(self.gel(Github(runs=rouge), force=True)["action"], "dispatch")

    def test_un_sha_demande_hors_gel_est_refuse(self):
        self.gele()
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(), sha=AUTRE)
        self.assertIn("is not the freeze", str(caught.exception))

    def test_un_sha_seul_ne_remplace_pas_le_gel(self):
        # le gel est un jalon : nommer un SHA ne dispense pas de le déclarer,
        # sinon la CI d'un lot jamais gelé partirait sans trace de gel.
        api = Github()
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api, sha=GEL)
        self.assertIn("is not frozen", str(caught.exception))
        self.assertEqual(api.paths, [], "rien ne doit être lu sans gel")

    def test_une_exception_apres_gel_test_la_tete_du_correctif(self):
        # un correctif d'incident arrive après le gel : l'exception teste la
        # tête, pas le gel périmé, et le dispatch est marqué.
        self.gele()
        api = Github(head=TETE)
        decision = self.gel(api, exception="incident", reason="incident prod")
        self.assertEqual((decision["action"], decision["sha"]), ("dispatch", TETE))
        self.assertEqual(decision["base"], BASE)

    def test_un_workflow_sans_declencheur_est_refuse(self):
        self.gele()
        api = Github(workflow="name: tests\n")
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("does not carry the freeze trigger", str(caught.exception))

    def test_une_base_introuvable_est_refusee(self):
        self.gele()
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(merge_base=""))
        self.assertIn("merge-base", str(caught.exception))

    def test_une_exception_dispatch_sans_gel_ni_avis_mais_marquee(self):
        api = Github(head=TETE)
        decision = self.gel(api, exception="incident", reason="CI cassée sur main")
        self.assertEqual(decision["action"], "dispatch")
        self.assertEqual((decision["sha"], decision["exception"]), (TETE, "incident"))
        self.assertIn("CI cassée sur main", decision["reason"])

    def test_une_exception_sans_reason_est_refusee(self):
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(), exception="unblock")
        self.assertIn("requires --reason", str(caught.exception))
        with self.assertRaises(ci.GelError):
            self.gel(Github(), exception="autre", reason="x")

    def test_full_dispatch_sans_avis_en_mode_full(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        decision = self.gel(Github(), full=True, reason="paquet §12")
        self.assertEqual((decision["action"], decision["mode"]), ("dispatch", "full"))

    def test_avis_hors_bornes_refuse(self):
        for avis in (-1, 3):
            with self.assertRaises(ci.GelError):
                self.gel(Github(), avis=avis)

    def test_lot_inconnu_refuse(self):
        with self.assertRaises(ci.GelError) as caught:
            ci.gel_plan(self.db, None, 999, api=Github(), repo=DEPOT)
        self.assertIn("unknown", str(caught.exception))

    def test_lot_sans_branche_refuse(self):
        self.item = lot(branch="")
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github())
        self.assertIn("has no branch", str(caught.exception))


class CancelPlanTest(GelTestCase):
    """`ci.cancel_plan` : les exécutions non conclues, le SHA gelé gardé."""

    def test_seuls_les_runs_ouverts_hors_sha_gele_sont_annules(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github(runs=[execution(1, statut="in_progress", conclusion=None, sha=GEL),
                           execution(2, statut="queued", conclusion=None, sha=TETE),
                           execution(3, conclusion="success", sha=TETE)])
        report = self.annule(api)
        self.assertEqual([r["id"] for r in report["runs"]], [2])
        self.assertIn(GEL[:12], report["why"])

    def test_le_sha_gele_s_annule_avec_all(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github(runs=[execution(1, statut="in_progress", conclusion=None, sha=GEL),
                           execution(2, statut="queued", conclusion=None, sha=TETE)])
        report = self.annule(api, all_runs=True)
        self.assertEqual(sorted(r["id"] for r in report["runs"]), [1, 2])
        self.assertTrue(report["all"])

    def test_un_lot_sans_gel_annule_tous_ses_runs_ouverts(self):
        api = Github(runs=[execution(1, statut="queued", conclusion=None, sha=GEL),
                           execution(2, conclusion="failure", sha=TETE)])
        self.assertEqual([r["id"] for r in self.annule(api)["runs"]], [1])

    def test_un_lot_sans_branche_refuse(self):
        self.item = lot(branch="")
        with self.assertRaises(ci.GelError) as caught:
            self.annule(Github())
        self.assertIn("nothing to cancel", str(caught.exception))


class DispatchTest(GelTestCase):
    """`ci.cmd_ci_run` : annule les périmés, dispatche une fois, note le lot."""

    def setUp(self) -> None:
        super().setUp()
        self.args = argparse.Namespace(id=1343, sha="", avis=1, exception="", reason="",
                                       full=False, force=False, repo=DEPOT, dry_run=False,
                                       json=True)

    def dispatch(self, api, gh, *, args=None, note=None):
        """`cmd_ci_run` avec le `gh`, la base et la note remplacés."""
        notes = [] if note is None else note
        with mock.patch("ameesh.db.open_db", lambda cfg: self.db), \
                mock.patch.object(plan_github, "Gh", lambda *a, **k: gh), \
                mock.patch.object(plan_github, "_json",
                                  lambda g, argv, *a, **k: api(argv[-1])), \
                mock.patch.object(work, "note", lambda db, i, text, **kw: notes.append(text)), \
                contextlib.redirect_stdout(io.StringIO()):
            code = ci.cmd_ci_run(None, args or self.args)
        return code, notes

    def test_dispatch_annule_les_perimes_puis_porte_sha_base_lot(self):
        self.gele()
        api = Github(runs=[execution(4, statut="queued", conclusion=None, sha=TETE)])
        gh = Gh()
        code, notes = self.dispatch(api, gh)
        self.assertEqual(code, 0)
        self.assertEqual([c[-1] for c in gh.annulations()],
                         ["repos/%s/actions/runs/4/cancel" % DEPOT])
        self.assertEqual(gh.dispatch(),
                         ["workflow", "run", "tests.yml", "--repo", DEPOT,
                          "--ref", BRANCHE, "-f", "mode=lot", "-f", "sha=%s" % GEL,
                          "-f", "base=%s" % BASE, "-f", "lot=1343"])
        self.assertEqual(len(notes), 1)
        self.assertIn("dispatched on %s" % GEL, notes[0])

    def test_dry_run_n_annule_ni_ne_dispatche(self):
        self.gele()
        api = Github(runs=[execution(4, statut="queued", conclusion=None, sha=TETE)])
        gh = Gh()
        code, notes = self.dispatch(api, gh, args=argparse.Namespace(
            **{**vars(self.args), "dry_run": True}))
        self.assertEqual((code, gh.calls, notes), (0, [], []))

    def test_un_dispatch_refuse_est_dit_et_note(self):
        self.gele()
        api = Github()
        gh = Gh(refuse_dispatch=True)
        code, notes = self.dispatch(api, gh)
        self.assertEqual(code, 1)
        self.assertEqual(len(notes), 1)
        self.assertIn("dispatch refused", notes[0])
        self.assertIn("Unexpected inputs", notes[0])

    def test_une_exception_nomme_le_drapeau_dans_le_dispatch(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github()
        gh = Gh()
        code, notes = self.dispatch(api, gh, args=argparse.Namespace(
            **{**vars(self.args), "exception": "unblock", "reason": "main rouge"}))
        self.assertEqual(code, 0)
        self.assertIn("exception=unblock", gh.dispatch())
        self.assertNotIn("main rouge", " ".join(gh.dispatch()),
                         "le motif reste au lot, pas dans l'argv du dispatch")
        self.assertIn("exception unblock: main rouge", notes[0])


class ParserTest(unittest.TestCase):
    """Le parseur : `ci run`/`ci cancel` et leurs défauts, sans base."""

    def test_ci_run_et_cancel_sont_du_parseur(self):
        parser = mesh_cli.build_parser()
        args = parser.parse_args(["ci", "run", "1343"])
        self.assertEqual(args.func.__name__, "cmd_ci")
        self.assertEqual((args.id, args.avis), (1343, 1))
        self.assertFalse(args.full or args.force or args.dry_run)
        args = parser.parse_args(["ci", "cancel", "1343", "--all", "--dry-run"])
        self.assertEqual(args.func.__name__, "cmd_ci")
        self.assertTrue(args.all and args.dry_run)


if __name__ == "__main__":
    unittest.main()
