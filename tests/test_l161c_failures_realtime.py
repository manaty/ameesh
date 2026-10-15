# SPDX-License-Identifier: AGPL-3.0-only
"""L161c — chaque échec de test remonté en temps réel au courrier de l'agent du lot.

Côté CI, `scripts/ci-flow.py publish` tient le check run « tests (flux) »
pendant la part et `close` le termine ; côté ameesh, `ci.Watcher` (mené par
`ameesh notify` sur l'hôte `github.host`) relève les exécutions en cours,
envoie un urgent au premier échec d'une tête, des événements ordinaires
ensuite et le verdict à la fin, n'impute jamais un rouge hérité de main, et
retire les messages non lus d'une tête remplacée. `ci_main_red` signale main
rouge dès le premier échec relevé, agrégateurs encore en file compris (#846) ;
`ameesh ci status` montre les exécutions d'un lot.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib.util
import io
import json
import pathlib
import re
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from ameesh import ci, db as db_mod, exploitation, mail, notify, registry, storage, work

from .support import PgTestCase

REPO = pathlib.Path(__file__).resolve().parent.parent


def _charger(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, REPO / "scripts" / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ciflux = _charger("ci_flux_l161c", "ci-flow.py")

DEPOT = "manaty/ameesh"


def ev_test(test, statut="echec", execution=101, part="psycopg 1/4", **extra):
    module = test.split(".")[1] if test.count(".") >= 2 else "test_x"
    return dict({"v": 1, "schema": ci.SCHEMA, "type": "test", "depot": DEPOT, "tete": "a" * 40,
                 "execution": execution, "part": part, "module": module, "test": test,
                 "statut": statut, "duree_s": 0.5, "ts": "2026-10-11T08:00:00.000Z",
                 "extrait": "Traceback (most recent call last):\nAssertionError: 1 != 2",
                 "rejouer": "python -m unittest %s" % test}, **extra)


def check_run(events, name="tests (flux) psycopg 1/4"):
    """Un check run tel que GitHub le rend, sortie produite par ci-flow.py."""
    return {"name": name, "output": ciflux.check_output(events)}


def check_job(name, run_id, conclusion="failure"):
    """Un check run natif de GitHub pour un job de l'exécution `run_id`."""
    return {"name": name, "conclusion": conclusion,
            "details_url": "https://github.com/%s/actions/runs/%d/job/%d"
                           % (DEPOT, run_id, run_id * 10)}


# --------------------------------------------------------------------------
# côté CI : scripts/ci-flow.py publish / close
# --------------------------------------------------------------------------

class FauxGitHub:
    def __init__(self, refus=False):
        self.appels, self.refus = [], refus

    def ready(self):
        return True

    def create(self, nom, output):
        if self.refus:
            raise ciflux.urllib.error.HTTPError("u", 403, "Resource not accessible", {}, None)
        self.appels.append(("create", nom, output, None))
        return 7

    def update(self, ident, output, conclusion=None):
        self.appels.append(("update", ident, output, conclusion))


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dossier))
        self.flux = self.dossier / "flux.jsonl"

    def ecrire(self, *events):
        with self.flux.open("a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev) + "\n")

    def args(self, **kw):
        return argparse.Namespace(**dict(dict(file=str(self.flux), name="tests (flux) p 1/1",
                                              interval=1.0, max_duration=100.0), **kw))

    def test_sortie_lue_par_ameesh(self):
        """La sortie du check run est exactement ce que `ci.events_of` relit."""
        events = [{"v": 1, "type": "debut", "part": "p 1/1", "modules": ["test_a"]},
                  ev_test("tests.test_a.T.test_ok", "succes", extrait=None),
                  ev_test("tests.test_a.T.test_ko"), ev_test("tests.test_a.T.test_err", "erreur")]
        sortie = ciflux.check_output(events)
        self.assertIn(ciflux.MARKER, sortie["text"])
        self.assertIn("2 échec(s)", sortie["title"])
        lus = ci.events_of([{"name": "tests (flux) p 1/1", "output": sortie}], 101)
        self.assertEqual([e["test"] for e in lus], ["tests.test_a.T.test_ko",
                                                    "tests.test_a.T.test_err"])
        self.assertEqual(lus[0]["rejouer"], "python -m unittest tests.test_a.T.test_ko")
        # une autre exécution sur la même tête (relance) n'est pas mélangée
        self.assertEqual(ci.events_of([{"name": "tests (flux) p", "output": sortie}], 102), [])
        # un check run d'un autre nom est ignoré
        self.assertEqual(ci.events_of([{"name": "tests", "output": sortie}], 101), [])

    def test_sortie_bornee(self):
        gros = "x" * 3000 + "\n"
        events = [ev_test("tests.test_a.T.test_%03d" % i, extrait=gros * 40) for i in range(200)]
        sortie = ciflux.check_output(events)
        self.assertLess(len(sortie["text"]), 65535)
        self.assertIn("hors de la limite", sortie["text"])
        self.assertTrue(ci.events_of([{"name": "tests (flux)", "output": sortie}], 101))

    def test_publier_suit_jusqu_a_la_fin(self):
        gh = FauxGitHub()
        tours = []

        def dormir(_s):
            tours.append(1)
            if len(tours) == 1:
                self.ecrire(ev_test("tests.test_a.T.test_ko"))
            elif len(tours) == 2:
                self.ecrire(ev_test("tests.test_a.T.test_ok", "succes"))
            else:
                self.ecrire({"v": 1, "type": "fin", "statut": "echec", "comptes": {}})

        horloge = iter(range(0, 10000, 1))
        self.assertEqual(ciflux.publish(self.args(), gh=gh, dormir=dormir,
                                        horloge=lambda: next(horloge)), 0)
        genres = [a[0] for a in gh.appels]
        self.assertEqual(genres[0], "create")
        # l'échec est publié aussitôt, avec son annotation, une seule fois
        avec_annot = [a for a in gh.appels if "annotations" in a[2]]
        self.assertEqual(len(avec_annot), 1)
        self.assertEqual(avec_annot[0][2]["annotations"][0]["path"], "tests/test_a.py")
        self.assertEqual(gh.appels[-1][3], "failure")
        # fermer ensuite ne refait rien : la conclusion est déjà posée
        avant = len(gh.appels)
        ciflux.close(self.args(), gh=gh)
        self.assertEqual(len(gh.appels), avant)

    def test_fermer_cree_si_absent(self):
        self.ecrire(ev_test("tests.test_a.T.test_ko"))
        gh = FauxGitHub()
        ciflux.close(self.args(), gh=gh)
        self.assertEqual([a[0] for a in gh.appels], ["create", "update"])
        self.assertEqual(gh.appels[-1][3], "cancelled")  # pas de ligne `fin`

    def test_refus_jamais_bloquant(self):
        gh = FauxGitHub(refus=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ciflux.publish(self.args(), gh=gh, dormir=lambda s: None), 0)
            self.assertEqual(ciflux.close(self.args(), gh=gh), 0)
        self.assertIn("non publié (HTTP Error 403", err.getvalue())

    def test_sans_jeton(self):
        gh = ciflux.GitHub(env={"GITHUB_REPOSITORY": DEPOT, "GITHUB_SHA": "a" * 40})
        self.assertFalse(gh.ready())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ciflux.publish(self.args(), gh=gh), 0)


class WorkflowTest(unittest.TestCase):
    def test_parts_publient(self):
        texte = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        for job in ("psycopg", "psql"):
            bloc = re.search(r"\n  %s:\n(.*?)(?=\n  [a-z-]+:\n)" % job, texte, re.S).group(1)
            self.assertIn("checks: write", bloc, job)
            self.assertIn("FLUX_NOM: tests (flux) %s" % job, bloc, job)
            self.assertRegex(bloc, r"\*publier|&publier", job)
            self.assertRegex(bloc, r"\*fermer|&fermer", job)
        self.assertIn("ci-flow.py publish", texte)
        self.assertIn("ci-flow.py close", texte)
        # la permission reste locale aux parts
        self.assertIn("permissions:\n  contents: read\n", texte)


# --------------------------------------------------------------------------
# côté ameesh : routage et textes, sans base
# --------------------------------------------------------------------------

class RoutageTest(unittest.TestCase):
    LOTS = [{"id": 5, "branch": "lot/5-a"}, {"id": 6, "branch": "Lot/6-B"}]

    def test_lot_par_branche_puis_ligne(self):
        self.assertEqual(ci.lot_of_run({"head_branch": "lot/6-b"}, self.LOTS), 6)
        self.assertEqual(ci.lot_of_run(
            {"head_branch": "main", "head_commit": {"message": "L1 (#9)\n\nameesh-work: 42\n"}},
            self.LOTS), 42)
        self.assertIsNone(ci.lot_of_run({"head_branch": "autre"}, self.LOTS))

    def test_base(self):
        self.assertEqual(ci.base_of({"pull_requests": [{"base": {"ref": "release/1"}}]}),
                         "release/1")
        self.assertEqual(ci.base_of({"pull_requests": []}), "main")

    def test_textes(self):
        run = {"run_id": 101, "head_sha": "b" * 40, "branch": "lot/5-a", "url": "https://x/101",
               "base": "main"}
        lot = {"id": 5, "title": "titre"}
        f = {"test": "tests.test_a.T.test_ko", "part": "psql 1/1", "status": "echec",
             "excerpt": "\n".join("l%d" % i for i in range(60)), "replay": "python -m unittest x"}
        texte = ci.failure_text(run, lot, [f], first=True)
        self.assertIn("first failure", texte)
        self.assertIn("replay: python -m unittest x", texte)
        self.assertIn("(20 more line(s))", texte)
        self.assertIn("gh pr checks --watch", texte)
        self.assertNotIn("l45", texte)
        self.assertIsNone(__import__("ameesh.fil", fromlist=["x"]).unreadable_reason(texte))
        self.assertIn("green.", ci.verdict_text(run, lot, "success", []))
        herite = ci.verdict_text(run, lot, "failure", [dict(f, inherited=True)])
        self.assertIn("nothing is charged to you", herite)
        rouge = ci.verdict_text(run, lot, "failure", [f, dict(f, test="t2", inherited=True)])
        self.assertIn("red, 1 failure(s), and 1 inherited from the base", rouge)


# --------------------------------------------------------------------------
# côté ameesh : le relevé sur une vraie base
# --------------------------------------------------------------------------

class FauxApi:
    """L'API GitHub vue par le relevé : workflows publiés par le dépôt
    (découverte, #1110), exécutions en cours, état d'une exécution, check runs
    d'une tête, et (#845) runners et jobs."""

    def __init__(self):
        self.live, self.queued, self.done, self.checks = [], [], {}, {}
        #: exécutions terminées (rattrapage borné #1110)
        self.terminated = []
        #: fichiers de workflow que le dépôt publie (découverte #1110)
        self.workflows = ["tests.yml"]
        #: #845 : runners du dépôt, jobs par exécution, liste d'exécutions
        #: tronquée, panne de l'API des runners
        self.runners, self.jobs, self.truncated, self.runners_fail = [], {}, False, False
        #: #845 : chemins appelés, pour compter les appels de la garde
        self.paths: list[str] = []

    def __call__(self, path):
        self.paths.append(path)
        if "/actions/runs/" in path and "/jobs" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            jobs = self.jobs.get(run_id, [])
            return {"total_count": len(jobs), "jobs": list(jobs)}
        if "/actions/runners" in path:
            if self.runners_fail:
                raise RuntimeError("403 runners: Resource not accessible")
            return {"total_count": len(self.runners), "runners": list(self.runners)}
        if "/actions/runs?" in path:
            if "status=completed" in path:
                return {"workflow_runs": list(self.terminated)}
            runs = list(self.queued if "status=queued" in path else self.live)
            return {"total_count": len(runs) + (1 if self.truncated else 0),
                    "workflow_runs": runs}
        if "/actions/workflows" in path:
            return {"workflows": [item if isinstance(item, dict) else
                                  {"path": ".github/workflows/%s" % item, "state": "active"}
                                  for item in self.workflows]}
        if "/actions/runs/" in path:
            return self.done.get(int(path.rsplit("/", 1)[1]), {"status": "in_progress"})
        if "/check-runs" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            return {"check_runs": self.checks.get(sha, [])}
        raise AssertionError(path)


class FauxHorloge:
    """#845 : l'horloge du relevé, pour espacer les relevés de la garde sans
    dormir (les bases de temps restent proches de l'horloge réelle, que
    PostgreSQL garde pour `stuck_since`/`seen_at`)."""

    def __init__(self, t: float | None = None):
        self.t = time.time() if t is None else float(t)

    def __call__(self) -> float:
        return self.t

    def avance(self, dt: float) -> None:
        self.t += float(dt)


def iso(t: float) -> str:
    return datetime.fromtimestamp(float(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runner_view(runner_id, name, *, busy=True, status="online"):
    return {"id": runner_id, "name": name, "status": status, "busy": busy}


def job_view(name, *, status="in_progress", runner="", job_id=None, created_at=None):
    job = {"name": name, "status": status, "runner_name": runner}
    if job_id is not None:
        job["id"] = job_id
    if created_at is not None:
        job["created_at"] = created_at
    return job


def run_view(run_id, sha, branch, *, base="main", message="", workflow="tests.yml",
             attempt=1, updated_at=None):
    view = {"id": run_id, "head_sha": sha, "head_branch": branch, "event": "pull_request"
            if branch != "main" else "push", "path": ".github/workflows/%s" % workflow,
            "run_attempt": attempt,
            "html_url": "https://github.com/%s/actions/runs/%d" % (DEPOT, run_id),
            "pull_requests": [{"base": {"ref": base}}] if branch != "main" else [],
            "head_commit": {"message": message}}
    if updated_at:
        view["updated_at"] = updated_at
    return view


def completed(path, conclusion, attempt=1):
    """L'état GitHub d'une exécution conclue (relevé par `_conclude`)."""
    return {"status": "completed", "conclusion": conclusion, "path": path,
            "run_attempt": attempt}


class WatcherTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ("ci_test_failures", "ci_runs", "ci_runner_state", "ci_queue_state",
                      "ci_guard_scan", "ci_guard_run"):
            self.db.execute("DELETE FROM %s" % table)
        registry.upsert(self.db, "dev-a", harness="claude", host=self.cfg.host)
        registry.upsert(self.db, "dev-b", harness="claude", host=self.cfg.host)
        self.lot = work.add(self.db, title="L1 : un lot", branch="lot/1-un", assignee="dev-a")
        self.lot_main = work.add(self.db, title="L0 : déjà fusionné", assignee="dev-b")
        self.cfg_ci = dataclasses.replace(
            self.cfg, github={"projects": {"ameesh": DEPOT}, "host": self.cfg.host})
        self.api = FauxApi()
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api)
        # une exécution verte de main déjà relevée : la référence du rouge hérité
        # (nommée `tests.yml` : la migration 0433 nomme les lignes antérieures)
        st = storage.of(self.db)
        st.ci.open_run(DEPOT, 100, head_sha="0" * 40, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.conclude(DEPOT, 100, "success")
        # #845 : les appels HTTP de la garde, comptés à la source (pendant
        # `_guard_state`). Depuis #1483, la liste du veilleur demande elle aussi
        # `per_page=100` : l'URL ne sépare plus les deux relevés.
        self._guard_paths: list[str] = []
        reel = ci.Watcher._guard_state

        def compte(watcher, db, repo):
            avant = len(self.api.paths)
            try:
                return reel(watcher, db, repo)
            finally:
                self._guard_paths.extend(self.api.paths[avant:])

        patcher = mock.patch.object(ci.Watcher, "_guard_state", compte)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mails(self, to):
        """Les messages d'ameesh à `to`, du plus ancien au plus récent."""
        return self.db.query(
            "SELECT id, kind, body, payload, work_item_id, delivered_at FROM agent_mailbox"
            " WHERE recipient = %s AND sender = %s ORDER BY id", (to, work.SYSTEM_SENDER))

    def payload_of(self, message):
        payload = message["payload"]
        return payload if isinstance(payload, dict) else json.loads(payload)

    def test_pas_l_hote_designe(self):
        cfg = dataclasses.replace(self.cfg, github={"projects": {"ameesh": DEPOT},
                                                    "host": "ailleurs"})
        self.assertEqual(ci.Watcher(cfg, api=self.api).repos(), [])
        self.assertEqual(self.watcher.repos(), [DEPOT])

    def test_run_vu_sans_branche_est_rattache_a_la_pose(self):
        """#1321 : un run vu avant la pose de la branche du lot (work_item_id
        NULL) est rattaché à la pose, et son verdict part à l'assigné."""
        # #540 : dev-a et dev-b portent déjà un lot non démarré, un second
        # irait dans la file d'équipe (assigné nul) ; on enregistre un agent
        # libre pour que l'attribution explicite soit conservée.
        registry.upsert(self.db, "dev-c", harness="claude", host=self.cfg.host)
        sha = "2" * 40
        self.api.live = [run_view(210, sha, "agent/dev-c-210")]
        self.watcher.tick(self.db)
        self.assertIsNone(storage.of(self.db).ci.run(DEPOT, 210)["work_item_id"])
        # toujours aucun lot sur cette branche : rien n'est inventé
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertIsNone(storage.of(self.db).ci.run(DEPOT, 210)["work_item_id"])
        # la branche du lot est posée après la première vue du run
        lot = work.add(self.db, title="L2 : branche posée tard",
                       branch="agent/dev-c-210", assignee="dev-c")
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 210)["work_item_id"], lot["id"])
        # la fin de l'exécution rend son verdict au lot rattaché
        self.api.live = []
        self.api.done[210] = {"status": "completed", "conclusion": "success"}
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["to"]) for o in out], [("verdict", "dev-c")])
        self.assertIn(": green.", self.mails("dev-c")[-1]["body"])

    def test_run_obsolete_non_rattache_quand_la_tete_a_avance(self):
        """#1321 : si le relevé des branches connaît une tête plus récente, un
        run plus ancien n'est pas rattaché — son verdict porterait sur une tête
        remplacée, et l'exécution de remplacement peut être `pending`
        (invisible au veilleur, #1161)."""
        sha, tete = "3" * 40, "4" * 40
        self.api.live = [run_view(220, sha, "agent/dev-a-220")]
        self.watcher.tick(self.db)
        self.assertIsNone(storage.of(self.db).ci.run(DEPOT, 220)["work_item_id"])
        lot = work.add(self.db, title="L3 : tête avancée", branch="agent/dev-a-220",
                       assignee="dev-a")
        st = storage.of(self.db)
        # la branche a avancé : la tête du run n'est plus celle de la branche
        self.assertTrue(st.work.set_branch_head(lot["id"], "agent/dev-a-220", tete))
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertIsNone(st.ci.run(DEPOT, 220)["work_item_id"])
        # la tête connue redevient celle du run : le rattachement se fait
        self.assertTrue(st.work.set_branch_head(lot["id"], "agent/dev-a-220", sha))
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(st.ci.run(DEPOT, 220)["work_item_id"], lot["id"])

    def test_urgent_puis_ordinaire_puis_verdict(self):
        sha = "c" * 40
        self.api.live = [run_view(201, sha, "lot/1-un")]
        self.assertEqual(self.watcher.tick(self.db), [])
        row = storage.of(self.db).ci.run(DEPOT, 201)
        self.assertEqual((row["status"], row["work_item_id"]), ("running", self.lot["id"]))
        # premier échec : urgent, rangé dans le fil du lot
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=201)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["to"], o["urgent"]) for o in out],
                         [("failures", "dev-a", True)])
        (m,) = self.mails("dev-a")
        self.assertTrue(mail.is_urgent(m))
        self.assertEqual(m["work_item_id"], str(self.lot["id"]))
        self.assertIn("tests.test_a.T.test_1", m["body"])
        self.assertEqual(mail.passive_reason(m), "")
        # rien de nouveau : rien n'est renvoyé
        self.assertEqual(self.watcher.tick(self.db), [])
        # un deuxième échec : un événement ordinaire
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=201),
                                           ev_test("tests.test_b.T.test_2", execution=201)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["tests"], o["urgent"]) for o in out],
                         [(["tests.test_b.T.test_2"], False)])
        # fin de l'exécution : verdict rouge, ordinaire
        self.api.live = []
        self.api.done[201] = {"status": "completed", "conclusion": "failure"}
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["status"]) for o in out], [("verdict", "failure")])
        mails = self.mails("dev-a")
        self.assertEqual(len(mails), 3)
        verdict = mails[-1]
        self.assertIn("red, 2 failure(s)", verdict["body"])
        self.assertFalse(mail.is_urgent(verdict))
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 201)["status"], "failure")
        # ameesh ci status
        report = ci.status_of(self.db, self.lot["id"])
        self.assertEqual(report["runs"][0]["status"], "failure")
        self.assertEqual(len(report["runs"][0]["failures"]), 2)
        texte = "\n".join(ci.render_status(report))
        self.assertIn("red  2 failure(s)", texte)

    def test_vert_sans_echec(self):
        self.api.live = [run_view(202, "d" * 40, "lot/1-un")]
        self.watcher.tick(self.db)
        self.api.live, self.api.done[202] = [], {"status": "completed", "conclusion": "success"}
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["status"], o["to"]) for o in out], [("success", "dev-a")])
        self.assertIn(": green.", self.mails("dev-a")[0]["body"])

    def test_annulee_sans_message(self):
        self.api.live = [run_view(203, "e" * 40, "lot/1-un")]
        self.watcher.tick(self.db)
        self.api.live, self.api.done[203] = [], {"status": "completed", "conclusion": "cancelled"}
        self.watcher.tick(self.db)
        self.assertEqual(self.mails("dev-a"), [])

    def test_rouge_herite_jamais_impute(self):
        main_sha, sha = "f" * 40, "1" * 40
        commun = "tests.test_c.T.test_casse"
        # main rouge sur `commun` : urgent à l'assigné du lot fusionné, alerte ci_main_red
        self.api.live = [run_view(300, main_sha, "main",
                                  message="L0 (#3)\n\nameesh-work: %d\n" % self.lot_main["id"])]
        self.api.checks[main_sha] = [check_run([ev_test(commun, execution=300)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["to"], o["urgent"]) for o in out], [("dev-b", True)])
        self.api.live, self.api.done[300] = [], {"status": "completed", "conclusion": "failure"}
        self.watcher.tick(self.db)
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "ci_main_red"]
        self.assertEqual(len(alertes), 1)
        self.assertEqual((alertes[0]["agent"], alertes[0]["lot"]), ("dev-b", self.lot_main["id"]))
        self.assertIn(commun, alertes[0]["detail"])
        self.assertIn("ci_main_red", notify.DEFAULT_TYPES)
        # la PR échoue sur le même test : hérité, ni urgent ni message
        self.api.live = [run_view(301, sha, "lot/1-un")]
        self.api.checks[sha] = [check_run([ev_test(commun, execution=301)])]
        out = self.watcher.tick(self.db)
        self.assertEqual(out, [])
        self.assertEqual(self.mails("dev-a"), [])
        failures = storage.of(self.db).ci.failures(DEPOT, 301)
        self.assertTrue(failures[0]["inherited"])
        # verdict : rouge hérité, rien d'imputé
        self.api.live, self.api.done[301] = [], {"status": "completed", "conclusion": "failure"}
        self.watcher.tick(self.db)
        (verdict,) = self.mails("dev-a")
        self.assertIn("nothing is charged to you", verdict["body"])
        # main repasse au vert : plus d'alerte
        self.api.live = [run_view(302, "2" * 40, "main")]
        self.watcher.tick(self.db)
        self.api.live, self.api.done[302] = [], {"status": "completed", "conclusion": "success"}
        self.watcher.tick(self.db)
        self.assertFalse([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "ci_main_red"])

    def test_poussee_suivante_retire_les_non_lus(self):
        old, new = "3" * 40, "4" * 40
        self.api.live = [run_view(401, old, "lot/1-un")]
        self.api.checks[old] = [check_run([ev_test("tests.test_a.T.test_1", execution=401)])]
        self.watcher.tick(self.db)
        self.watcher.tick(self.db)
        (urgent,) = self.mails("dev-a")
        self.assertIsNone(urgent["delivered_at"])
        # nouvelle tête : l'ancienne exécution est remplacée, son urgent retiré
        self.api.live = [run_view(402, new, "lot/1-un")]
        out = self.watcher.tick(self.db)
        sup = [o for o in out if o["action"] == "superseded"]
        self.assertEqual(sup[0]["withdrawn"], [urgent["id"]])
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 401)["status"], "superseded")
        self.assertEqual(mail.unread(self.db, "dev-a"), [])
        row = self.db.query("SELECT meta FROM agent_mailbox WHERE id = %s", (urgent["id"],))[0]
        meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"])
        self.assertEqual(meta["retire"]["remplace_par"], new)
        # l'ancienne exécution ne donne plus de verdict
        self.api.done[401] = {"status": "completed", "conclusion": "cancelled"}
        self.watcher.tick(self.db)
        self.assertEqual(len(self.mails("dev-a")), 1)

    def test_deja_remis_reste(self):
        old, new = "5" * 40, "6" * 40
        self.api.live = [run_view(501, old, "lot/1-un")]
        self.api.checks[old] = [check_run([ev_test("tests.test_a.T.test_1", execution=501)])]
        self.watcher.tick(self.db)
        self.watcher.tick(self.db)
        (urgent,) = self.mails("dev-a")
        mail.mark_delivered(self.db, [urgent["id"]])
        self.api.live = [run_view(502, new, "lot/1-un")]
        out = self.watcher.tick(self.db)
        self.assertEqual([o["withdrawn"] for o in out if o["action"] == "superseded"], [[]])

    def _urgent_non_lu(self, run_id, sha):
        self.api.live = [run_view(run_id, sha, "lot/1-un")]
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=run_id)])]
        self.watcher.tick(self.db)
        self.watcher.tick(self.db)
        (urgent,) = self.mails("dev-a")
        self.assertTrue(mail.is_urgent(urgent))
        return urgent

    def test_suivante_en_attente_quand_l_ancienne_est_annulee(self):
        # relecture (1) : la nouvelle exécution attend un runner quand
        # l'ancienne est annulée ; vue en attente, elle remplace l'ancienne
        urgent = self._urgent_non_lu(411, "a1" * 20)
        self.api.live, self.api.queued = [], [run_view(412, "a2" * 20, "lot/1-un")]
        self.api.done[411] = {"status": "completed", "conclusion": "cancelled"}
        out = self.watcher.tick(self.db)
        self.assertEqual([o["withdrawn"] for o in out if o["action"] == "superseded"],
                         [[urgent["id"]]])
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 411)["status"], "superseded")
        self.assertEqual(mail.unread(self.db, "dev-a"), [])
        self.assertEqual(len(self.mails("dev-a")), 1)

    def test_annulee_avant_que_la_suivante_soit_vue(self):
        # relecture (1), ordre défavorable : l'ancienne est conclue annulée
        # avant tout relevé de la nouvelle ; la nouvelle tête, vue plus tard,
        # retire quand même les non-lus de l'ancienne
        urgent = self._urgent_non_lu(421, "b1" * 20)
        self.api.live = []
        self.api.done[421] = {"status": "completed", "conclusion": "cancelled"}
        self.watcher.tick(self.db)
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 421)["status"], "cancelled")
        self.api.live = [run_view(422, "b2" * 20, "lot/1-un")]
        out = self.watcher.tick(self.db)
        self.assertEqual([o["withdrawn"] for o in out if o["action"] == "superseded"],
                         [[urgent["id"]]])
        self.assertEqual(mail.unread(self.db, "dev-a"), [])
        # une exécution plus ancienne vue après coup ne remplace pas la nouvelle
        self.api.live = [run_view(420, "b0" * 20, "lot/1-un")]
        self.watcher.tick(self.db)
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 422)["status"], "running")

    def test_sans_base_relevee_passif(self):
        # relecture (4) : aucune exécution conclue de main relevée, l'échec
        # est peut-être hérité : message passif, jamais urgent
        self.db.execute("DELETE FROM ci_runs WHERE run_id = 100")
        sha = "c1" * 20
        self.api.live = [run_view(431, sha, "lot/1-un")]
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=431)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["to"], o["urgent"]) for o in out], [("dev-a", False)])
        (m,) = self.mails("dev-a")
        self.assertFalse(mail.is_urgent(m))
        self.assertEqual(mail.passive_reason(m), "ci_passif")
        self.assertIn("may be inherited", m["body"])
        self.assertIn("first failure", m["body"])
        self.assertIsNone(storage.of(self.db).ci.run(DEPOT, 431)["urgent_message_id"])
        # le suivant n'est plus « premier », et toujours sans urgent
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=431),
                                           ev_test("tests.test_b.T.test_2", execution=431)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([o["urgent"] for o in out], [False])
        self.assertIn("next failure", self.mails("dev-a")[-1]["body"])

    def test_retrait_reserve_au_courrier_de_ci(self):
        # relecture (5) : withdraw ne touche qu'un message de CI d'ameesh
        autre = mail.send(self.db, "dev-b", "dev-a", "un message d'agent")
        systeme = mail.send(self.db, work.SYSTEM_SENDER, "dev-a", "autre événement",
                            kind="event")
        self.assertEqual(storage.of(self.db).mailbox.withdraw([autre, systeme], {"x": 1}), [])

    def test_main_rouge_une_seule_alerte(self):
        # relecture (2) : deux exécutions rouges de suite sur main, de deux
        # lots : la même alerte, qui ne se résout pas entre les deux
        for run_id, sha, lot in ((441, "d1" * 20, self.lot_main), (442, "d2" * 20, self.lot)):
            self.api.live = [run_view(run_id, sha, "main",
                                      message="L (#9)\n\nameesh-work: %d\n" % lot["id"])]
            self.api.checks[sha] = [check_run([ev_test("tests.test_x.T.test_y",
                                                       execution=run_id)])]
            self.watcher.tick(self.db)
            self.api.live = []
            self.api.done[run_id] = {"status": "completed", "conclusion": "failure"}
            self.watcher.tick(self.db)
            (alerte,) = [a for a in exploitation.alerts(self.cfg, self.db)
                         if a["type"] == "ci_main_red"]
            if run_id == 441:
                cle = exploitation.alert_key(alerte)
        self.assertEqual(alerte["lot"], self.lot["id"])
        self.assertEqual(exploitation.alert_key(alerte), cle)

    def test_main_rouge_sans_attendre_les_agregateurs(self):
        # #846 : un échec relevé sur une exécution de main encore en cours
        # (ses jobs agrégateurs attendent un runner) alerte déjà ; la
        # conclusion rouge ne change pas la clé, donc pas de doublon
        sha = "9" * 40
        self.api.live = [run_view(801, sha, "main",
                                  message="L0 (#3)\n\nameesh-work: %d\n" % self.lot_main["id"])]
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=801)])]
        self.watcher.tick(self.db)
        row = storage.of(self.db).ci.run(DEPOT, 801)
        self.assertEqual(row["status"], "running")   # agrégateurs encore en file
        (alerte,) = [a for a in exploitation.alerts(self.cfg, self.db)
                     if a["type"] == "ci_main_red"]
        self.assertEqual((alerte["agent"], alerte["lot"]), ("dev-b", self.lot_main["id"]))
        self.assertIn("tests.test_a.T.test_1", alerte["detail"])
        cle = exploitation.alert_key(alerte)
        # la conclusion rouge ne double pas l'alerte
        self.api.live = []
        self.api.done[801] = {"status": "completed", "conclusion": "failure"}
        self.watcher.tick(self.db)
        (alerte2,) = [a for a in exploitation.alerts(self.cfg, self.db)
                      if a["type"] == "ci_main_red"]
        self.assertEqual(exploitation.alert_key(alerte2), cle)

    def test_main_rouge_sur_un_job_sans_evenement(self):
        # #846 : un job en échec sans événement de test publié (installation,
        # délai dépassé) est un main rouge ; il est relevé, alerte, et ne
        # produit aucun courrier de test (les jobs d'une autre exécution du
        # même commit sont écartés)
        sha = "a9" * 20
        self.api.live = [run_view(802, sha, "main",
                                  message="L0 (#3)\n\nameesh-work: %d\n" % self.lot_main["id"])]
        self.api.checks[sha] = [check_job("psycopg 2/4", 802),
                                check_job("plan", 802, conclusion="success"),
                                check_job("psycopg 3/4", 999),
                                check_job("psql 1/1", 802, conclusion="cancelled")]
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(self.mails("dev-b"), [])
        (alerte,) = [a for a in exploitation.alerts(self.cfg, self.db)
                     if a["type"] == "ci_main_red"]
        self.assertEqual((alerte["agent"], alerte["lot"]), ("dev-b", self.lot_main["id"]))
        self.assertIn("psycopg 2/4", alerte["detail"])
        self.assertNotIn("psycopg 3/4", alerte["detail"])
        # le relevé suivant ne double ni le relevé ni l'alerte
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(len([a for a in exploitation.alerts(self.cfg, self.db)
                              if a["type"] == "ci_main_red"]), 1)

    def test_attente_d_un_runner_separee_sans_double_alerte(self):
        # #747 : 1 h 24 d'attente lue comme « psql bloqué 2 h 20 »
        import time as _time
        now = 1_800_000_000
        self.enterContext(mock.patch("time.time", return_value=now))
        cree = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 30 * 60))
        vue = dict(run_view(801, "9" * 40, "lot/1-un"), status="queued", created_at=cree)
        self.api.queued = [vue]
        self.watcher.tick(self.db)
        row = storage.of(self.db).ci.run(DEPOT, 801)
        self.assertIsNone(row["started_ts"])
        self.assertEqual(row["created_ts"], now - 30 * 60)
        self.assertEqual(ci.wait_text(row, now), "queued 30 min")
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "ci_queue_starved"], [])
        self.assertNotIn("ci_queue_starved", notify.DEFAULT_TYPES)
        # démarrée : l'attente est figée, la vue ne compte plus ce run
        self.api.queued, self.api.live = [], [dict(vue, status="in_progress")]
        self.watcher.tick(self.db)
        row = storage.of(self.db).ci.run(DEPOT, 801)
        self.assertIsNotNone(row["started_ts"])
        # PostgreSQL a sa propre horloge : fixer le démarrage de la fixture
        # après avoir vérifié que le relevé l'a bien enregistré.
        self.db.execute("UPDATE ci_runs SET started_at = to_timestamp(%s)"
                        " WHERE repo = %s AND run_id = %s", (now, DEPOT, 801))
        self.watcher.tick(self.db)
        row = storage.of(self.db).ci.run(DEPOT, 801)
        self.assertEqual(row["started_ts"], now)
        self.assertEqual(ci.wait_text(row, now + 60), "queued 30 min, ran 1 min")
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "ci_queue_starved"], [])
        report = ci.status_of(self.db, self.lot["id"])
        self.assertIn("(queued 30 min, ran 0 min)", "\n".join(ci.render_status(report)))

    def test_attente_courte_sans_alerte(self):
        # N747 : la clé `ci_queue_starved` est retirée — seule la garde #845
        # (`ci_queue_saturated`, en jobs) porte la capacité ; une attente
        # courte ne lève rien et la clé morte ne doit pas revenir.
        vue = dict(run_view(802, "a1" * 20, "lot/1-un"), status="queued")
        self.api.queued = [vue]
        self.watcher.tick(self.db)
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "ci_queue_starved"], [])
        self.assertNotIn("ci_queue_starved", exploitation.ALERT_TYPES)
        self.assertNotIn("ci_queue_starved", notify.DEFAULT_TYPES)

    def test_extrait_illisible_part_sans_extrait(self):
        sha = "7" * 40
        self.api.live = [run_view(601, sha, "lot/1-un")]
        blob = "QUJD" * 100
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=601,
                                                   extrait="valeur : " + blob)])]
        self.watcher.tick(self.db)
        self.watcher.tick(self.db)
        (m,) = self.mails("dev-a")
        self.assertNotIn(blob, m["body"])
        self.assertIn("tests.test_a.T.test_1", m["body"])

    def test_assigne_humain_ou_absent(self):
        humain = work.add(self.db, title="L2 : humain", branch="lot/2-h")
        self.api.live = [run_view(701, "8" * 40, "lot/2-h")]
        self.api.checks["8" * 40] = [check_run([ev_test("tests.test_a.T.test_1", execution=701)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([o["to"] for o in out], [None])
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 701)["work_item_id"], humain["id"])

    # -- #1110 : workflows suivis par projet/dépôt ---------------------------
    def test_workflows_decouverts_sans_tests_yml(self):
        """Un dépôt sans `tests.yml` (nexlink) voit ses suites découvertes,
        suivies, et l'auteur réveillé à la fin exacte du workflow."""
        repo = "manaty/nexlink"
        cfg = dataclasses.replace(self.cfg, github={"projects": {"nexlink": repo},
                                                    "host": self.cfg.host})
        api = FauxApi()
        api.workflows = ["backend-suite.yml", "sql-invariant.yml"]
        watcher = ci.Watcher(cfg, api=api)
        sha = "9" * 40
        api.live = [run_view(901, sha, "lot/1-un", workflow="backend-suite.yml")]
        self.assertEqual(watcher.tick(self.db), [])
        row = storage.of(self.db).ci.run(repo, 901)
        self.assertEqual((row["workflow"], row["work_item_id"]),
                         ("backend-suite.yml", self.lot["id"]))
        api.live = []
        api.done[901] = completed(".github/workflows/backend-suite.yml", "success")
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o["status"], o["to"]) for o in out],
                         [("verdict", "success", "dev-a")])
        (message,) = self.mails("dev-a")
        self.assertIn("(backend-suite.yml)", message["body"])
        self.assertIn(": green.", message["body"])
        payload = self.payload_of(message)["ci"]
        self.assertEqual((payload["workflow"], payload["attempt"]),
                         ("backend-suite.yml", 1))

    def test_defaut_ameesh_tests_yml_seul(self):
        """Un dépôt qui publie `tests.yml` garde le défaut L161 : ce seul
        workflow, même quand d'autres sont actifs."""
        self.api.workflows = ["tests.yml", "ui.yml"]
        self.api.live = [run_view(910, "a" * 40, "lot/1-un", workflow="ui.yml"),
                         run_view(911, "b" * 40, "lot/1-un", workflow="tests.yml")]
        self.watcher.tick(self.db)
        st = storage.of(self.db)
        self.assertIsNone(st.ci.run(DEPOT, 910))
        self.assertEqual(st.ci.run(DEPOT, 911)["workflow"], "tests.yml")

    def test_liste_declaree_prime_sur_la_decouverte(self):
        cfg = dataclasses.replace(self.cfg, github={
            "projects": {"ameesh": {"repo": DEPOT, "workflows": ["backend-suite.yml"]}},
            "host": self.cfg.host})
        api = FauxApi()
        api.workflows = ["backend-suite.yml", "website-suite.yml"]
        watcher = ci.Watcher(cfg, api=api)
        api.live = [run_view(920, "c" * 40, "lot/1-un", workflow="backend-suite.yml"),
                    run_view(921, "d" * 40, "lot/1-un", workflow="website-suite.yml")]
        watcher.tick(self.db)
        st = storage.of(self.db)
        self.assertEqual(st.ci.run(DEPOT, 920)["workflow"], "backend-suite.yml")
        self.assertIsNone(st.ci.run(DEPOT, 921))

    def test_workflow_declare_absent_et_desactive(self):
        """Un workflow déclaré mais absent du dépôt est signalé une fois ; un
        workflow désactivé n'est pas découvert."""
        journal = []
        cfg = dataclasses.replace(self.cfg, github={
            "projects": {"ameesh": {"repo": DEPOT, "workflows": ["disparu.yml"]}},
            "host": self.cfg.host})
        api = FauxApi()
        api.workflows = ["tests.yml", {"path": ".github/workflows/eteint.yml",
                                       "state": "disabled_manually"}]
        watcher = ci.Watcher(cfg, api=api, log=journal.append)
        api.live = [run_view(930, "e" * 40, "lot/1-un", workflow="eteint.yml")]
        watcher.tick(self.db)
        watcher.tick(self.db)
        self.assertEqual([line for line in journal if "disparu.yml" in line],
                         ["CI %s: workflow(s) declared in github.projects but absent "
                          "from the repository: disparu.yml" % DEPOT])
        self.assertIsNone(storage.of(self.db).ci.run(DEPOT, 930))

    def test_multi_workflows_meme_tete_statuts_et_verdicts(self):
        """Deux workflows d'une même tête gardent chacun leur ligne ; le
        verdict nomme la suite et les autres workflows encore en cours."""
        api = self.api
        api.workflows = ["backend-suite.yml", "website-suite.yml"]
        sha = "f" * 40
        api.live = [run_view(940, sha, "lot/1-un", workflow="backend-suite.yml"),
                    run_view(941, sha, "lot/1-un", workflow="website-suite.yml")]
        self.assertEqual(self.watcher.tick(self.db), [])
        st = storage.of(self.db)
        self.assertEqual((st.ci.run(DEPOT, 940)["status"], st.ci.run(DEPOT, 941)["status"]),
                         ("running", "running"))
        # backend-suite finit verte, website-suite tourne encore : pas de vert
        # global prématuré, le verdict nomme la suite et l'autre workflow
        api.live = [run_view(941, sha, "lot/1-un", workflow="website-suite.yml")]
        api.done[940] = completed(".github/workflows/backend-suite.yml", "success")
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["status"]) for o in out], [("verdict", "success")])
        (message,) = self.mails("dev-a")
        self.assertIn("(backend-suite.yml)", message["body"])
        self.assertIn("still running: website-suite.yml", message["body"])
        # website-suite finit rouge : son verdict, une seule fois
        api.live = []
        api.done[941] = completed(".github/workflows/website-suite.yml", "failure")
        api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=941)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("status")) for o in out],
                         [("failures", None), ("verdict", "failure")])
        mails = self.mails("dev-a")
        self.assertEqual(len(mails), 3)  # urgent, verdict backend, verdict website
        self.assertIn("(website-suite.yml)", mails[-1]["body"])
        self.assertNotIn("still running", mails[-1]["body"])
        # relire ne renvoie rien : ni doublon, ni rejeu
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(len(self.mails("dev-a")), 3)
        report = ci.status_of(self.db, self.lot["id"])
        self.assertEqual({run["workflow"] for run in report["runs"]},
                         {"backend-suite.yml", "website-suite.yml"})
        texte = "\n".join(ci.render_status(report))
        self.assertIn("backend-suite.yml", texte)
        self.assertIn("website-suite.yml", texte)

    def test_nouvelle_tete_remplace_tous_les_workflows(self):
        """Une poussée remplace les workflows de l'ancienne tête ; celui qui
        conclut après coup n'envoie rien (pas de rejeu périmé)."""
        api = self.api
        api.workflows = ["backend-suite.yml", "website-suite.yml"]
        old, new = "1" * 40, "2" * 40
        api.live = [run_view(950, old, "lot/1-un", workflow="backend-suite.yml"),
                    run_view(951, old, "lot/1-un", workflow="website-suite.yml")]
        self.watcher.tick(self.db)
        api.live = [run_view(952, new, "lot/1-un", workflow="backend-suite.yml")]
        self.watcher.tick(self.db)
        st = storage.of(self.db)
        self.assertEqual(st.ci.run(DEPOT, 950)["status"], "superseded")
        self.assertEqual(st.ci.run(DEPOT, 951)["status"], "superseded")
        api.done[951] = completed(".github/workflows/website-suite.yml", "success")
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(self.mails("dev-a"), [])

    def test_verdict_porte_l_attempt(self):
        """Le verdict situe la suite et l'attempt relancé (#901, #1110)."""
        self.api.workflows = ["backend-suite.yml"]
        sha = "3" * 40
        self.api.live = [run_view(960, sha, "lot/1-un", workflow="backend-suite.yml")]
        self.watcher.tick(self.db)
        self.api.live = []
        self.api.done[960] = completed(".github/workflows/backend-suite.yml", "success",
                                       attempt=2)
        self.watcher.tick(self.db)
        (message,) = self.mails("dev-a")
        self.assertIn("(backend-suite.yml)", message["body"])
        self.assertIn("attempt 2", message["body"])
        self.assertEqual(self.payload_of(message)["ci"]["attempt"], 2)

    # -- #1110 R1 auditeur : rattrapage et rouge par workflow ----------------
    #: horloge fixe des témoins de rattrapage (aucune dépendance à l'horloge
    #: réelle) : 1 000 000 s après l'epoch, soit 1970-01-12T13:46:40Z
    NOW = 1_000_000.0
    AT_NOW = "1970-01-12T13:46:40+00:00"

    def terminated(self, run_id, sha, branch, *, conclusion, workflow="tests.yml",
                   updated_at=None):
        """Une exécution terminée telle que la liste `status=completed` la rend."""
        return dict(run_view(run_id, sha, branch, workflow=workflow,
                             updated_at=updated_at or self.AT_NOW),
                    status="completed", conclusion=conclusion)

    def test_execution_courte_terminee_avant_le_premier_passage(self):
        """#1110 P2-1 : une exécution née et finie entre deux passages n'a
        jamais été vue « en cours » ; le rattrapage la découvre terminée, rend
        son verdict et réveille l'agent — une seule fois."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        sha = "a1" * 20
        self.api.terminated = [self.terminated(970, sha, "lot/1-un",
                                               conclusion="failure")]
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1",
                                                   execution=970)])]
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("status"), o.get("urgent")) for o in out],
                         [("failures", None, True), ("verdict", "failure", None)])
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 970)["status"], "failure")
        mails = self.mails("dev-a")
        self.assertEqual(len(mails), 2)  # urgent puis verdict
        self.assertIn("tests.test_a.T.test_1", mails[0]["body"])
        self.assertIn("red, 1 failure(s)", mails[-1]["body"])
        # le passage suivant ne double rien
        self.assertEqual(watcher.tick(self.db), [])
        self.assertEqual(len(self.mails("dev-a")), 2)

    def test_rattrapage_borne_et_sans_verdict_perime(self):
        """#1110 P2-1 : le rattrapage est borné (une exécution trop ancienne
        est ignorée) et une tête déjà remplacée ne rend pas de verdict."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        vieille, neuve = "b1" * 20, "b2" * 20
        self.api.live = [run_view(980, neuve, "lot/1-un")]
        watcher.tick(self.db)
        self.api.live = []
        self.api.terminated = [
            self.terminated(979, vieille, "lot/1-un", conclusion="failure"),
            self.terminated(978, "b3" * 20, "lot/1-un", conclusion="failure",
                            updated_at="1970-01-12T11:46:40+00:00")]  # hors fenêtre
        self.assertEqual(watcher.tick(self.db), [])
        st = storage.of(self.db)
        self.assertIsNone(st.ci.run(DEPOT, 979))
        self.assertIsNone(st.ci.run(DEPOT, 978))
        self.assertEqual(self.mails("dev-a"), [])

    def test_main_rouge_par_workflow(self):
        """#1110 P2-2 : sur la même tête de main, un succès d'une autre suite
        ne masque pas l'échec de `backend-suite.yml`."""
        st = storage.of(self.db)
        sha = "c1" * 20
        for run_id, workflow, conclusion in ((990, "backend-suite.yml", "failure"),
                                             (991, "scripts-suite.yml", "success")):
            st.ci.open_run(DEPOT, run_id, head_sha=sha, branch="main", event="push",
                           base="main", work_item_id=None, url=None, workflow=workflow)
            if conclusion == "failure":
                st.ci.add_failure(DEPOT, run_id, part="psql 1/1",
                                  test="tests.test_a.T.test_1", module="tests.test_a",
                                  status="echec", inherited=False, excerpt="", replay="",
                                  duration_s=None, failed_at=None)
            st.ci.conclude(DEPOT, run_id, conclusion)
        self.assertEqual([(r["run_id"], r["workflow"], r["tests"]) for r in
                          ci.main_red(self.db)],
                         [(990, "backend-suite.yml", ["tests.test_a.T.test_1"])])
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "ci_main_red"]
        self.assertEqual(len(alertes), 1)
        self.assertIn("backend-suite.yml", alertes[0]["detail"])

    def test_rouge_herite_par_workflow(self):
        """#1110 P2-2 : le rouge hérité se compare à la même suite ; l'échec
        d'une autre suite de la base ne rend pas le test « hérité »."""
        st = storage.of(self.db)
        base_sha = "d1" * 20
        for run_id, workflow, conclusion in ((994, "backend-suite.yml", "success"),
                                             (995, "scripts-suite.yml", "failure")):
            st.ci.open_run(DEPOT, run_id, head_sha=base_sha, branch="main", event="push",
                           base="main", work_item_id=None, url=None, workflow=workflow)
            if conclusion == "failure":
                st.ci.add_failure(DEPOT, run_id, part="psql 1/1",
                                  test="tests.test_a.T.test_1", module="tests.test_a",
                                  status="echec", inherited=False, excerpt="", replay="",
                                  duration_s=None, failed_at=None)
            st.ci.conclude(DEPOT, run_id, conclusion)
        # l'exécution du lot, même suite que le vert de la base : l'échec est
        # neuf (pas hérité du rouge d'une autre suite), donc urgent et nominatif
        self.api.workflows = ["backend-suite.yml"]
        sha = "d2" * 20
        self.api.live = [run_view(996, sha, "lot/1-un", workflow="backend-suite.yml")]
        self.watcher.tick(self.db)
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1",
                                                   execution=996)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["urgent"]) for o in out], [("failures", True)])
        (message,) = self.mails("dev-a")
        self.assertIn("tests.test_a.T.test_1", message["body"])
        self.assertNotIn("inherited", self.payload_of(message).get("ci_passif", ""))

    def test_pending_ne_cite_que_la_meme_tete(self):
        """#1110 P3 : le verdict ne cite comme « encore en cours » que les
        workflows de la même tête, pas ceux d'un autre lot."""
        self.api.workflows = ["backend-suite.yml", "website-suite.yml"]
        sha_a, sha_b = "e1" * 20, "e2" * 20
        work.add(self.db, title="L2 : autre", branch="lot/2-autre", assignee="dev-b")
        self.api.live = [run_view(1001, sha_a, "lot/1-un", workflow="backend-suite.yml"),
                         run_view(1002, sha_b, "lot/2-autre",
                                  workflow="website-suite.yml")]
        self.watcher.tick(self.db)
        self.api.live = [run_view(1002, sha_b, "lot/2-autre", workflow="website-suite.yml")]
        self.api.done[1001] = completed(".github/workflows/backend-suite.yml", "success")
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["status"]) for o in out], [("verdict", "success")])
        (message,) = self.mails("dev-a")
        self.assertIn("(backend-suite.yml)", message["body"])
        self.assertNotIn("still running", message["body"])

    def test_rattrapage_ne_cite_pas_un_workflow_conclu_du_meme_passage(self):
        """#1110 P3 : un workflow de la même tête, terminé dans le même
        passage, n'est pas annoncé « encore en cours » par le rattrapage."""
        self.api.workflows = ["backend-suite.yml", "website-suite.yml"]
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        sha = "f1" * 20
        # passage 1 : le backend est vu en cours (ligne `running` en base)
        self.api.live = [run_view(1010, sha, "lot/1-un", workflow="backend-suite.yml")]
        watcher.tick(self.db)
        # passage 2 : les deux suites de la même tête sont terminées ; le
        # website, plus court, n'a jamais été vu en cours (rattrapage)
        self.api.live = []
        self.api.done[1010] = completed(".github/workflows/backend-suite.yml", "success")
        self.api.terminated = [
            self.terminated(1011, sha, "lot/1-un", conclusion="success",
                            workflow="website-suite.yml"),
            self.terminated(1010, sha, "lot/1-un", conclusion="success",
                            workflow="backend-suite.yml")]
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o["run_id"], o["status"]) for o in out],
                         [("verdict", 1011, "success"), ("verdict", 1010, "success")])
        bodies = [m["body"] for m in self.mails("dev-a")]
        self.assertEqual(len(bodies), 2)
        self.assertFalse([b for b in bodies if "still running" in b])

    def test_reference_stricte_par_workflow(self):
        """#1110 P2-2 : une ligne sans workflow (écrite avant 0433 par un
        ancien processus) ne sert pas de référence à une autre suite : l'échec
        n'est pas classé hérité au nom d'une suite étrangère."""
        st = storage.of(self.db)
        sha_main = "0" * 40  # la ligne verte `tests.yml` du setUp
        st.ci.open_run(DEPOT, 993, head_sha=sha_main, branch="main", event="push",
                       base="main", work_item_id=None, url=None)  # sans workflow
        st.ci.add_failure(DEPOT, 993, part="psql 1/1", test="tests.test_a.T.test_1",
                          module="tests.test_a", status="echec", inherited=False,
                          excerpt="", replay="", duration_s=None, failed_at=None)
        st.ci.conclude(DEPOT, 993, "failure")
        # une exécution backend du lot échoue sur le même test : la ligne sans
        # workflow n'est pas une référence backend, l'échec est passif (base
        # inconnue), jamais « hérité » d'une autre suite
        self.api.workflows = ["backend-suite.yml"]
        sha = "0a" * 20
        self.api.live = [run_view(997, sha, "lot/1-un", workflow="backend-suite.yml")]
        self.watcher.tick(self.db)
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1",
                                                   execution=997)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["urgent"]) for o in out], [("failures", False)])
        (message,) = self.mails("dev-a")
        self.assertTrue(self.payload_of(message).get("ci_passif"))
        self.assertFalse(st.ci.failures(DEPOT, 997)[0]["inherited"])

    # -- #1483 : suites P3 de la R2 Claude de #1110 --------------------------
    def test_rattrapage_ne_perd_pas_un_verdict_de_main_remplace(self):
        """#1483 N1 : sur une branche de base, une exécution courte de la tête
        M1 garde son verdict — et le réveil du lot fusionné avec elle — même
        quand la tête M2 est déjà relevée : le relevé normal ne remplace jamais
        rien sur une base, le rattrapage ne doit pas non plus."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        m1, m2 = "a2" * 20, "a3" * 20
        st = storage.of(self.db)
        # la tête M2 est déjà relevée : `head_replaced` est vrai pour M1
        st.ci.open_run(DEPOT, 1111, head_sha=m2, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        self.assertTrue(st.ci.head_replaced(DEPOT, "main", m1, 1110))
        self.api.terminated = [dict(
            run_view(1110, m1, "main", updated_at=self.AT_NOW,
                     message="Merge pull request #9\n\nameesh-work: %d\n"
                             % self.lot_main["id"]),
            status="completed", conclusion="success")]
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o["run_id"], o["status"], o["to"]) for o in out],
                         [("verdict", 1110, "success", "dev-b")])
        self.assertEqual(st.ci.run(DEPOT, 1110)["status"], "success")
        self.assertIn(": green.", self.mails("dev-b")[-1]["body"])
        # le passage suivant ne double rien
        self.assertEqual(watcher.tick(self.db), [])
        self.assertEqual(len(self.mails("dev-b")), 1)

    def test_rattrapage_ecarte_la_tete_du_lot_remplacee(self):
        """#1483 N4 : dans le rattrapage, une exécution dont la tête n'est plus
        celle de la branche du lot (`branch_head`) est écartée — la même garde
        que `_follow` sur une ligne connue ; la tête courante rend son
        verdict."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        old, new = "e1" * 20, "e2" * 20
        st = storage.of(self.db)
        self.assertTrue(st.work.set_branch_head(self.lot["id"], "lot/1-un", new))
        self.api.terminated = [self.terminated(1500, old, "lot/1-un", conclusion="success")]
        self.assertEqual(watcher.tick(self.db), [])
        self.assertIsNone(st.ci.run(DEPOT, 1500))
        self.assertEqual(self.mails("dev-a"), [])
        # la tête courante de la branche, elle, rend son verdict
        self.api.terminated = [self.terminated(1501, new, "lot/1-un", conclusion="success")]
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o["run_id"], o["status"]) for o in out],
                         [("verdict", 1501, "success")])
        self.assertIn(": green.", self.mails("dev-a")[-1]["body"])

    def test_base_develop_declaree_pour_nexlink(self):
        """#1483 N2 : `github.projects.<projet>.base` déclare la base d'un
        dépôt — `develop` pour Nexlink : une exécution de develop n'est jamais
        remplacée, un push hors PR prend develop pour référence du rouge hérité,
        et l'alerte `ci_main_red` voit develop rouge."""
        repo = "manaty/nexlink"
        cfg = dataclasses.replace(self.cfg, github={
            "projects": {"nexlink": {"repo": repo, "base": "develop"}},
            "host": self.cfg.host})
        api = FauxApi()
        api.workflows = ["backend-suite.yml"]
        watcher = ci.Watcher(cfg, api=api)
        # main reste une base : jamais une branche de lot (R1 dev4)
        self.assertEqual(watcher.bases(repo), ("develop", "main"))
        st = storage.of(self.db)
        # deux têtes de develop : la première n'est pas remplacée (base)
        api.live = [run_view(1200, "b1" * 20, "develop", workflow="backend-suite.yml")]
        watcher.tick(self.db)
        api.live = [run_view(1201, "b2" * 20, "develop", workflow="backend-suite.yml")]
        watcher.tick(self.db)
        self.assertEqual((st.ci.run(repo, 1200)["status"], st.ci.run(repo, 1201)["status"]),
                         ("running", "running"))
        # ni deux têtes de main (release de Nexlink)
        api.live = [run_view(1204, "c1" * 20, "main", workflow="backend-suite.yml")]
        watcher.tick(self.db)
        api.live = [run_view(1205, "c2" * 20, "main", workflow="backend-suite.yml")]
        watcher.tick(self.db)
        self.assertEqual((st.ci.run(repo, 1204)["status"], st.ci.run(repo, 1205)["status"]),
                         ("running", "running"))
        # un push hors PR sur une branche de lot prend develop pour base
        run = run_view(1203, "b3" * 20, "lot/1-un", workflow="backend-suite.yml")
        run["pull_requests"], run["event"] = [], "push"
        api.live = [run]
        watcher.tick(self.db)
        self.assertEqual(st.ci.run(repo, 1203)["base"], "develop")
        # develop rouge : `ci_main_red` et l'alerte le voient
        st.ci.open_run(repo, 1202, head_sha="b2" * 20, branch="develop", event="push",
                       base="develop", work_item_id=None, url=None,
                       workflow="backend-suite.yml")
        st.ci.add_failure(repo, 1202, part="psql 1/1", test="tests.test_a.T.test_1",
                          module="tests.test_a", status="echec", inherited=False,
                          excerpt="", replay="", duration_s=None, failed_at=None)
        st.ci.conclude(repo, 1202, "failure")
        self.assertEqual([(r["repo"], r["branch"], r["run_id"]) for r in
                          ci.main_red(self.db, ("main", "develop"))],
                         [(repo, "develop", 1202)])
        self.assertEqual(
            [a["reason"] for a in exploitation.alerts(cfg, self.db)
             if a["type"] == "ci_main_red"],
            ["%s:develop:backend-suite.yml" % repo])

    def test_listes_de_100_et_troncature_journalisee(self):
        """#1483 N3 : les listes d'exécutions demandent 100 lignes (la file
        d'autres workflows ne cache plus une suite suivie) et une liste
        tronquée est journalisée une fois."""
        journal = []
        watcher = ci.Watcher(self.cfg_ci, api=self.api, log=journal.append)
        self.api.truncated = True
        self.api.live = [run_view(1300, "c1" * 20, "lot/1-un")]
        watcher.tick(self.db)
        for status in ("queued", "in_progress", "completed"):
            self.assertIn("repos/%s/actions/runs?status=%s&per_page=100" % (DEPOT, status),
                          self.api.paths)
        self.assertEqual([line for line in journal if "run list truncated" in line],
                         ["CI %s: in_progress run list truncated (1 of 2), a followed run "
                          "may be seen late" % DEPOT,
                          "CI %s: queued run list truncated (0 of 1), a followed run may "
                          "be seen late" % DEPOT])

    def test_decouverte_ne_suit_que_les_suites(self):
        """#1483 N3 : un workflow découvert qui n'a pas la forme d'une suite
        (déploiement, sonde) n'est pas suivi — pas de verdict de bruit ; il est
        journalisé, et une liste déclarée le suit si on le veut."""
        repo = "manaty/nexlink"
        cfg = dataclasses.replace(self.cfg, github={"projects": {"nexlink": repo},
                                                    "host": self.cfg.host})
        api = FauxApi()
        api.workflows = ["backend-suite.yml", "sql-invariant.yml", "deploy-api-prod.yml",
                         "compute-probe.yml"]
        journal = []
        watcher = ci.Watcher(cfg, api=api, log=journal.append)
        api.live = [run_view(1400, "d1" * 20, "lot/1-un", workflow="backend-suite.yml"),
                    run_view(1401, "d2" * 20, "lot/1-un", workflow="sql-invariant.yml"),
                    run_view(1402, "d3" * 20, "lot/1-un", workflow="deploy-api-prod.yml"),
                    run_view(1403, "d4" * 20, "lot/1-un", workflow="compute-probe.yml")]
        watcher.tick(self.db)
        st = storage.of(self.db)
        self.assertEqual([st.ci.run(repo, 1400)["workflow"],
                          st.ci.run(repo, 1401)["workflow"]],
                         ["backend-suite.yml", "sql-invariant.yml"])
        self.assertIsNone(st.ci.run(repo, 1402))
        self.assertIsNone(st.ci.run(repo, 1403))
        self.assertEqual([line for line in journal if "not followed" in line],
                         ["CI %s: discovered workflow(s) not followed (not a test suite): "
                          "compute-probe.yml, deploy-api-prod.yml; declare them in "
                          "github.projects to follow them" % repo])
        # une liste déclarée suit explicitement un workflow hors convention
        cfg = dataclasses.replace(self.cfg, github={
            "projects": {"nexlink": {"repo": repo, "workflows": ["deploy-api-prod.yml"]}},
            "host": self.cfg.host})
        api = FauxApi()
        api.workflows = ["backend-suite.yml", "deploy-api-prod.yml"]
        watcher = ci.Watcher(cfg, api=api)
        api.live = [run_view(1404, "d5" * 20, "lot/1-un", workflow="deploy-api-prod.yml")]
        watcher.tick(self.db)
        self.assertEqual(st.ci.run(repo, 1404)["workflow"], "deploy-api-prod.yml")

    # -- #845 : la garde de la CI (runners et file) --------------------------
    def test_garde_runner_busy_sans_job(self):
        """#845 : un runner busy sans job in_progress à son nom tient un job
        fantôme ; l'épisode est daté, l'alerte ne se lève qu'après 15 min, et
        se tait dès que le job revient ou que le runner disparaît."""
        horloge = FauxHorloge()
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge)
        self.api.runners = [runner_view(21, "ameesh-ci-1-1"),
                            runner_view(22, "ameesh-ci-1-2", busy=False)]
        self.api.live = [run_view(901, "9" * 40, "main")]
        self.api.jobs[901] = [job_view("psycopg 1/4", runner="ameesh-ci-1-1")]
        self.watcher.tick(self.db)
        st = storage.of(self.db)
        self.assertEqual(st.ci.stuck_runners(), [])
        # le job disparaît : l'épisode s'ouvre au relevé suivant (espacé)
        self.api.jobs[901] = []
        horloge.avance(ci.GUARD_SCAN_S)
        self.watcher.tick(self.db)
        (row,) = st.ci.stuck_runners()
        self.assertEqual((row["repo"], row["name"], row["job"]), (DEPOT, "ameesh-ci-1-1", None))
        # rien avant le seuil, l'alerte après (une par dépôt, non urgente)
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db, now=horloge.t)
                          if a["type"] == "ci_runner_stuck"], [])
        (alerte,) = [a for a in exploitation.alerts(
            self.cfg, self.db,
            now=float(row["since_ts"]) + exploitation.CI_RUNNER_STUCK_S + 1)
            if a["type"] == "ci_runner_stuck"]
        self.assertEqual(alerte["reason"], DEPOT)
        self.assertIn("ameesh-ci-1-1", alerte["detail"])
        self.assertIn("ci_runner_stuck", notify.DEFAULT_TYPES)
        # le job revient : l'épisode se ferme
        self.api.jobs[901] = [job_view("psycopg 1/4", runner="ameesh-ci-1-1")]
        horloge.avance(ci.GUARD_SCAN_S)
        self.watcher.tick(self.db)
        self.assertEqual(st.ci.stuck_runners(), [])
        # un runner retiré du dépôt ne laisse pas d'alerte derrière lui
        self.api.jobs[901] = []
        horloge.avance(ci.GUARD_SCAN_S)
        self.watcher.tick(self.db)
        self.assertEqual(len(st.ci.stuck_runners()), 1)
        self.api.runners = [runner_view(22, "ameesh-ci-1-2", busy=False)]
        horloge.avance(ci.GUARD_SCAN_S)
        self.watcher.tick(self.db)
        self.assertEqual(st.ci.stuck_runners(), [])

    def test_garde_file_saturee(self):
        """#845 : le plus vieux job `queued` (toutes exécutions non conclues)
        au-delà du SLO de 15 min lève `ci_queue_saturated` ; l'alerte dure tant
        que la file reste longue, même quand le plus vieux job change (une
        alerte par dépôt)."""
        horloge = FauxHorloge()
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge)
        self.api.queued = [run_view(902, "a1" * 20, "main"), run_view(903, "a2" * 20, "main")]
        self.api.queued[0].update({"status": "queued", "created_at": iso(horloge.t - 1000)})
        self.api.queued[1].update({"status": "queued", "created_at": iso(horloge.t - 10)})
        self.api.jobs[902] = [
            job_view("psql 1/1", status="queued", job_id=9021,
                     created_at=iso(horloge.t - 1000)),
            job_view("psycopg 1/1", status="queued", job_id=9022,
                     created_at=iso(horloge.t - 10))]
        self.api.jobs[903] = [job_view("psql 1/1", status="queued", job_id=9031,
                                       created_at=iso(horloge.t - 5))]
        self.watcher.tick(self.db)
        (row,) = storage.of(self.db).ci.queued_oldest()
        self.assertEqual((int(row["queued"]), int(row["oldest_id"])), (3, 9021))
        (alerte,) = [a for a in exploitation.alerts(self.cfg, self.db, now=horloge.t)
                     if a["type"] == "ci_queue_saturated"]
        self.assertEqual(alerte["reason"], DEPOT)
        self.assertIn("15 min queue SLO", alerte["detail"])
        cle = exploitation.alert_key(alerte)
        # un autre plus vieux job : même cause, même clé (pas de renvoi)
        self.api.queued = [run_view(904, "a3" * 20, "main")]
        self.api.queued[0].update({"status": "queued", "created_at": iso(horloge.t - 2000)})
        self.api.jobs = {904: [job_view("psql 1/1", status="queued", job_id=9041,
                                        created_at=iso(horloge.t - 2000))]}
        horloge.avance(ci.GUARD_SCAN_S)
        self.watcher.tick(self.db)
        (alerte,) = [a for a in exploitation.alerts(self.cfg, self.db, now=horloge.t)
                     if a["type"] == "ci_queue_saturated"]
        self.assertEqual(exploitation.alert_key(alerte), cle)
        # file redevenue courte : plus d'alerte, relevé effacé
        self.api.queued = []
        self.api.jobs = {}
        horloge.avance(ci.GUARD_SCAN_S)
        self.watcher.tick(self.db)
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db, now=horloge.t)
                          if a["type"] == "ci_queue_saturated"], [])
        (row,) = storage.of(self.db).ci.queued_oldest()
        self.assertEqual(int(row["queued"]), 0)
        self.assertIsNone(row["oldest_ts"])
        self.assertIn("ci_queue_saturated", notify.DEFAULT_TYPES)

    def test_garde_file_compte_les_jobs_des_runs_en_cours(self):
        """#845 : un run `in_progress` peut encore porter un job `queued` plus
        ancien que les runs de la file ; la mesure le compte, sinon la file
        paraîtrait vide alors qu'un job attend depuis plus du SLO."""
        horloge = FauxHorloge()
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge)
        self.api.runners = [runner_view(21, "ameesh-ci-1-1")]
        self.api.live = [run_view(904, "b1" * 20, "main")]
        self.api.live[0].update({"status": "in_progress", "created_at": iso(horloge.t - 60)})
        self.api.jobs[904] = [
            job_view("psql 1/1", runner="ameesh-ci-1-1", job_id=9041),
            job_view("psql 2/2", status="queued", job_id=9042,
                     created_at=iso(horloge.t - 1200))]
        self.watcher.tick(self.db)
        (row,) = storage.of(self.db).ci.queued_oldest()
        self.assertEqual((int(row["queued"]), int(row["oldest_id"])), (1, 9042))
        (alerte,) = [a for a in exploitation.alerts(self.cfg, self.db, now=horloge.t)
                     if a["type"] == "ci_queue_saturated"]
        self.assertIn("20 min", alerte["detail"])
        # le job en cours tient son runner : pas de panne signalée à tort
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db, now=horloge.t)
                          if a["type"] == "ci_runner_stuck"], [])

    def test_garde_file_ne_vieillit_pas_une_reprise(self):
        """#845 : une re-exécution garde le `created_at` de son run ; la date
        qui compte est celle du job en file, sinon une reprise récente d'un run
        ancien passerait pour une file de plusieurs heures."""
        horloge = FauxHorloge()
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge)
        run = run_view(905, "c1" * 20, "main")
        run.update({"status": "queued", "created_at": iso(horloge.t - 10800)})
        self.api.queued = [run]
        self.api.jobs[905] = [job_view("psql 1/1", status="queued", job_id=9051,
                                       created_at=iso(horloge.t - 30))]
        self.watcher.tick(self.db)
        (row,) = storage.of(self.db).ci.queued_oldest()
        self.assertEqual((int(row["queued"]), int(row["oldest_id"])), (1, 9051))
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db, now=horloge.t)
                          if a["type"] == "ci_queue_saturated"], [])

    def test_garde_file_compte_un_job_ne_apres_une_annulation_de_part(self):
        """#845 : une matrice `fail-fast` annule ses parts sans annuler
        l'exécution ; le job né après cette annulation (ici l'agrégat `tests`)
        attend réellement un runner et compte dans la file comme dans l'âge.
        La garde ne déduit pas l'annulation de l'exécution de celle d'un job :
        un orphelin d'exécution annulée a la même forme et se traite à la
        source (`if: always() && !cancelled()`), pas par une inférence qui
        masquerait une file réelle."""
        horloge = FauxHorloge()
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge)
        run = run_view(910, "f1" * 20, "lot/3-z")
        run.update({"status": "queued", "created_at": iso(horloge.t - 1900)})
        self.api.queued = [run]
        part = job_view("psycopg 1/4", status="completed",
                        created_at=iso(horloge.t - 1900))
        part.update({"conclusion": "cancelled", "completed_at": iso(horloge.t - 1200)})
        self.api.jobs[910] = [
            part,
            job_view("tests", status="queued", job_id=9101,
                     created_at=iso(horloge.t - 1200)),
        ]
        self.watcher.tick(self.db)
        (row,) = storage.of(self.db).ci.queued_oldest()
        self.assertEqual((int(row["queued"]), int(row["oldest_id"])), (1, 9101))
        (alerte,) = [a for a in exploitation.alerts(self.cfg, self.db, now=horloge.t)
                     if a["type"] == "ci_queue_saturated"]
        self.assertIn("20 min", alerte["detail"])

    def test_garde_sautee_si_releve_tronque_ou_trop_large(self):
        """#845 : un relevé incomplet ne conclut rien — l'épisode précédent
        reste, il n'est ni défait ni renouvelé (file ET runners gardent leur
        `seen_at`) — et l'alerte se tait quand le relevé n'est plus assez
        frais."""
        horloge = FauxHorloge()
        logs = []
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge, log=logs.append)
        self.api.runners = [runner_view(21, "ameesh-ci-1-1")]
        self.api.live = [run_view(901, "9" * 40, "main")]
        self.api.jobs[901] = []
        self.watcher.tick(self.db)
        (row,) = storage.of(self.db).ci.stuck_runners()
        (file_avant,) = storage.of(self.db).ci.queued_oldest()
        runner_avant = self.runner_seen_ts(21)
        self.api.truncated = True
        horloge.avance(ci.GUARD_SCAN_S)
        self.watcher.tick(self.db)
        self.assertTrue(any("incomplete (truncated)" in line for line in logs), logs)
        self.assertEqual(len(storage.of(self.db).ci.stuck_runners()), 1)
        self.assertEqual(self.scan_state(), (False, "truncated", 2))
        self.api.truncated = False
        self.api.live = [run_view(900 + i, "%040d" % i, "main")
                         for i in range(ci.GUARD_SCAN_MAX_RUNS + 1)]
        horloge.avance(ci.GUARD_SCAN_S)
        self.watcher.tick(self.db)
        self.assertTrue(any("incomplete (too_many_runs)" in line for line in logs), logs)
        self.assertEqual(self.scan_state()[1], "too_many_runs")
        (file_apres,) = storage.of(self.db).ci.queued_oldest()
        self.assertEqual(file_apres["seen_ts"], file_avant["seen_ts"],
                         "un relevé incomplet a publié la file")
        self.assertEqual(self.runner_seen_ts(21), runner_avant,
                         "un relevé incomplet a rafraîchi les runners")
        # le relevé est périmé : l'alerte ne se lève plus sur un état ancien
        now = float(row["since_ts"]) + ci.GUARD_SCAN_S + exploitation.CI_GUARD_FRESH_S + 1
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db, now=now)
                          if a["type"] == "ci_runner_stuck"], [])

    def scan_state(self, repo=DEPOT):
        """#845 : le dernier relevé de la garde — `(complete, reason, calls)`."""
        (row,) = self.db.query(
            "SELECT complete, reason, calls FROM ci_guard_scan WHERE repo = %s", (repo,))
        return (row["complete"], row["reason"], int(row["calls"]))

    def runner_seen_ts(self, runner_id, repo=DEPOT):
        """#845 : `seen_at` du dernier relevé pour un runner — témoin qu'un
        relevé incomplet ne rafraîchit ni ne publie l'état des runners."""
        (row,) = self.db.query(
            "SELECT extract(epoch from seen_at)::float8 AS seen_ts"
            "  FROM ci_runner_state WHERE repo = %s AND runner_id = %s",
            (repo, int(runner_id)))
        return row["seen_ts"]

    def guard_calls(self) -> list[str]:
        """#845 : les appels HTTP de la garde, relevés pendant `_guard_state`.
        Depuis #1483, la liste du veilleur demande elle aussi `per_page=100` :
        l'URL ne sépare plus les deux relevés, le compte se fait à la source."""
        return list(self._guard_paths)

    def test_garde_fenetre_persistee_avant_le_premier_appel(self):
        """#845 (revue N/P2 2328) : la fenêtre est écrite dans la base AVANT le
        premier appel HTTP de la garde, et elle est tenue entre deux relevés —
        un second `notify --once` sur le même dépôt n'appelle pas la garde et
        ne publie rien avant l'expiration."""
        horloge = FauxHorloge()
        logs = []
        vu: list[bool] = []
        reel = ci.Watcher._guard_state

        def guard_qui_verifie(watcher, db, repo):
            """Le premier appel HTTP de la garde voit la fenêtre déjà écrite."""
            reel_api = watcher.api

            def api_qui_verifie(path):
                if not vu:
                    vu.append(bool(self.db.query(
                        "SELECT 1 FROM ci_guard_scan WHERE repo = %s", (DEPOT,))))
                return reel_api(path)

            watcher.api = api_qui_verifie
            try:
                return reel(watcher, db, repo)
            finally:
                watcher.api = reel_api

        premier = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge, log=logs.append)
        self.api.runners = [runner_view(21, "ameesh-ci-1-1", busy=False)]
        self.api.live = [run_view(901, "9" * 40, "main")]
        self.api.jobs[901] = []
        with mock.patch.object(ci.Watcher, "_guard_state", guard_qui_verifie):
            premier.tick(self.db)
        self.assertEqual(vu, [True], "fenêtre absente avant le premier appel HTTP")
        self.assertEqual(self.scan_state(), (True, None, 4))
        second = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge, log=logs.append)
        self.api.paths.clear()
        self._guard_paths.clear()
        second.tick(self.db)
        self.assertEqual(self.guard_calls(), [], "le second relevé a appelé la garde")
        self.assertTrue(any("window held" in line for line in logs), logs)
        # la fenêtre expirée, le relevé reprend
        horloge.avance(ci.GUARD_SCAN_S)
        self.api.paths.clear()
        self._guard_paths.clear()
        second.tick(self.db)
        self.assertEqual(len(self.guard_calls()), 4)
        self.assertEqual(self.scan_state()[0], True)

    def test_garde_budget_ne_publie_pas_un_releve_partiel(self):
        """#845 (revue N/P2 2328 ; arbitrage A) : un relevé qui ne peut pas
        couvrir toutes les exécutions dans le budget d'appels s'arrête, ne
        publie RIEN (l'état précédent n'est ni défait ni rafraîchi : file ET
        runners gardent leur `seen_at`) et date son incomplétude. Avec 63
        appels pour 60 exécutions, le budget est un garde-fou de dernier
        recours : le témoin abaisse le plafond dans une portée `mock` (11
        exécutions, sous les 60 admises) pour l'exercer."""
        horloge = FauxHorloge()
        logs = []
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge, log=logs.append)
        self.api.runners = [runner_view(21, "ameesh-ci-1-1", busy=False)]
        self.api.live = [run_view(901, "9" * 40, "main")]
        self.api.live[0].update({"status": "in_progress", "created_at": iso(horloge.t - 2000)})
        self.api.jobs[901] = [job_view("psql 1/1", status="queued", job_id=9011,
                                       created_at=iso(horloge.t - 2000))]
        self.watcher.tick(self.db)
        (avant,) = storage.of(self.db).ci.queued_oldest()
        runner_avant = self.runner_seen_ts(21)
        self.assertTrue([a for a in exploitation.alerts(self.cfg, self.db, now=horloge.t)
                         if a["type"] == "ci_queue_saturated"],
                        "la file en attente doit alerter avant le relevé partiel")
        # 11 exécutions `in_progress` sous un plafond de 12 appels : la
        # onzième est refusée (2 listes + 10 jobs = 12 appels) ; un relevé
        # partiel publié effacerait l'alerte à tort.
        self.api.live = [run_view(910 + i, "%040d" % i, "main") for i in range(11)]
        for i in range(11):
            self.api.live[i].update({"status": "in_progress", "created_at": iso(horloge.t)})
            self.api.jobs[910 + i] = [job_view("psql 1/1", runner="ameesh-ci-1-1",
                                               job_id=9100 + i)]
        self.api.jobs.pop(901)
        horloge.avance(ci.GUARD_SCAN_S)
        with mock.patch.object(ci, "GUARD_SCAN_MAX_CALLS", 12):
            self.watcher.tick(self.db)
        self.assertEqual(self.scan_state(), (False, "budget", 12))
        self.assertTrue(any("incomplete (budget)" in line for line in logs), logs)
        (apres,) = storage.of(self.db).ci.queued_oldest()
        self.assertEqual(apres["seen_ts"], avant["seen_ts"],
                         "un relevé partiel a publié la file")
        self.assertEqual(self.runner_seen_ts(21), runner_avant,
                         "un relevé partiel a rafraîchi les runners")
        (alerte,) = [a for a in exploitation.alerts(self.cfg, self.db, now=horloge.t)
                     if a["type"] == "ci_queue_saturated"]
        self.assertEqual(int(alerte["value"]), 1)
        borne = float(avant["seen_ts"]) + exploitation.CI_GUARD_FRESH_S
        self.assertEqual(len([a for a in exploitation.alerts(self.cfg, self.db, now=borne)
                              if a["type"] == "ci_queue_saturated"]), 1,
                         "à la borne de fraîcheur, l'état complet précédent fonde encore")
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db, now=borne + 1)
                          if a["type"] == "ci_queue_saturated"], [])

    def test_garde_60_executions_releve_complet_en_63_appels(self):
        """#845 (arbitrage A) : le plafond de couverture (60 exécutions non
        conclues) coûte exactement le budget (63 appels = 2 listes + 60 jobs +
        1 runners) et publie un relevé complet."""
        horloge = FauxHorloge()
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge)
        self.api.runners = [runner_view(21, "ameesh-ci-1-1", busy=False)]
        self.api.live = [run_view(910 + i, "%040d" % i, "main")
                         for i in range(ci.GUARD_SCAN_MAX_RUNS)]
        for i in range(ci.GUARD_SCAN_MAX_RUNS):
            self.api.jobs[910 + i] = []
        self.watcher.tick(self.db)
        attendu = 2 + ci.GUARD_SCAN_MAX_RUNS + 1
        self.assertEqual(attendu, ci.GUARD_SCAN_MAX_CALLS,
                         "le budget ne couvre plus le plafond de couverture")
        self.assertEqual(len(self.guard_calls()), attendu, self.guard_calls())
        self.assertEqual(self.scan_state(), (True, None, attendu))

    def test_garde_61_executions_releve_incomplet_sans_publication(self):
        """#845 (arbitrage A) : une exécution de plus que le plafond (61 > 60)
        arrête le relevé avant tout appel de jobs (`too_many_runs`), ne publie
        RIEN (file ET runners gardent leur `seen_at`), et l'état complet
        précédent fonde encore ses alertes à la borne de fraîcheur (1800 s),
        plus au-delà."""
        horloge = FauxHorloge()
        logs = []
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge, log=logs.append)
        self.api.runners = [runner_view(21, "ameesh-ci-1-1", busy=False)]
        self.api.live = [run_view(901, "9" * 40, "main")]
        self.api.live[0].update({"status": "in_progress", "created_at": iso(horloge.t - 2000)})
        self.api.jobs[901] = [job_view("psql 1/1", status="queued", job_id=9011,
                                       created_at=iso(horloge.t - 2000))]
        self.watcher.tick(self.db)
        (file_avant,) = storage.of(self.db).ci.queued_oldest()
        runner_avant = self.runner_seen_ts(21)
        # 61 exécutions non conclues : une de plus que le plafond de couverture
        self.api.live = [run_view(910 + i, "%040d" % i, "main")
                         for i in range(ci.GUARD_SCAN_MAX_RUNS + 1)]
        self.api.jobs.pop(901)
        horloge.avance(ci.GUARD_SCAN_S)
        self.watcher.tick(self.db)
        self.assertTrue(any("incomplete (too_many_runs)" in line for line in logs), logs)
        self.assertEqual(self.scan_state(), (False, "too_many_runs", 2))
        (file_apres,) = storage.of(self.db).ci.queued_oldest()
        self.assertEqual(file_apres["seen_ts"], file_avant["seen_ts"],
                         "un relevé incomplet a publié la file")
        self.assertEqual(self.runner_seen_ts(21), runner_avant,
                         "un relevé incomplet a rafraîchi les runners")
        borne = float(file_avant["seen_ts"]) + exploitation.CI_GUARD_FRESH_S
        self.assertEqual(len([a for a in exploitation.alerts(self.cfg, self.db, now=borne)
                              if a["type"] == "ci_queue_saturated"]), 1,
                         "à 1800 s, l'état complet précédent fonde encore l'alerte")
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db, now=borne + 1)
                          if a["type"] == "ci_queue_saturated"], [])

    def test_garde_relit_une_execution_queued_dont_un_job_demarre(self):
        """#845 (R1 finale cx3, B1) : un run reste `queued` tant qu'un de ses
        jobs attend, même quand un autre job tourne (mesure fondatrice #828) —
        le relevé suivant ne peut donc PAS réutiliser l'instantané : le job
        passé `in_progress` et le job neuf resté en file sont relus, pas servis
        par le cache."""
        horloge = FauxHorloge()
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge)
        self.api.runners = [runner_view(21, "ameesh-ci-1-1", busy=False)]
        run = run_view(906, "a6" * 20, "main")
        run.update({"status": "queued", "created_at": iso(horloge.t - 1000)})
        self.api.queued = [run]
        self.api.jobs[906] = [job_view("psql 1/1", status="queued", job_id=9061,
                                       created_at=iso(horloge.t - 1000))]
        self.watcher.tick(self.db)
        self.assertEqual(len(self.guard_calls()), 4)
        # le job 9061 démarre alors que le run reste `queued` (9062 attend) :
        # l'instantané du premier relevé est périmé.
        horloge.avance(ci.GUARD_SCAN_S)
        self.api.paths.clear()
        self._guard_paths.clear()
        self.api.runners = [runner_view(21, "ameesh-ci-1-1")]
        self.api.jobs[906] = [
            job_view("psql 1/1", runner="ameesh-ci-1-1", job_id=9061),
            job_view("psycopg 1/1", status="queued", job_id=9062,
                     created_at=iso(horloge.t - 10)),
        ]
        self.watcher.tick(self.db)
        st = storage.of(self.db)
        (row,) = st.ci.queued_oldest()
        # un seul constat : le relevé doit relire (4 appels), la file ne doit
        # garder que 9062, et le runner qui tient 9061 ne doit pas être « stuck ».
        self.assertEqual(
            (len(self.guard_calls()), int(row["queued"]), int(row["oldest_id"]),
             [r["name"] for r in st.ci.stuck_runners()]),
            (4, 1, 9062, []), self.guard_calls())

    def test_garde_fenetre_atomique_entre_connexions(self):
        """#845 (revue N/P2 2328) : la revendication de fenêtre est atomique —
        quatre connexions qui la demandent ensemble n'en gagnent qu'une."""
        import threading
        now = time.time()
        dbs = [self.db] + [db_mod.connect(self.cfg) for _ in range(3)]
        for extra in dbs[1:]:
            self.addCleanup(extra.close)
        resultats: list[bool] = []
        barriere = threading.Barrier(len(dbs))
        verrou = threading.Lock()

        def revendique(db):
            st = storage.of(db)
            barriere.wait(timeout=30)
            gagne = bool(st.ci.claim_guard_scan(DEPOT, now=now, window_s=ci.GUARD_SCAN_S))
            with verrou:
                resultats.append(gagne)

        threads = [threading.Thread(target=revendique, args=(db,)) for db in dbs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(sorted(resultats), [False, False, False, True])

    def test_garde_ne_casse_pas_le_releve(self):
        """#845 : une erreur de la garde (jeton sans droit runners, GitHub) est
        journalisée et n'empêche ni les échecs, ni les verdicts."""
        logs = []
        self.api.runners_fail = True
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=FauxHorloge(),
                                  log=logs.append)
        sha = "c" * 40
        self.api.live = [run_view(201, sha, "lot/1-un")]
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=201)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["to"]) for o in out], [("failures", "dev-a")])
        self.assertTrue(any("guard failed" in line for line in logs), logs)
        self.assertEqual(storage.of(self.db).ci.stuck_runners(), [])

    def test_garde_erreur_ne_publie_rien(self):
        """#845 (arbitrage A) : une erreur du relevé (jeton sans droit runners,
        GitHub) ne publie RIEN — file ET runners gardent leur `seen_at` — et
        l'incomplétude est datée (`failed`)."""
        horloge = FauxHorloge()
        logs = []
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge, log=logs.append)
        self.api.runners = [runner_view(21, "ameesh-ci-1-1", busy=False)]
        self.api.live = [run_view(901, "9" * 40, "main")]
        self.api.live[0].update({"status": "in_progress", "created_at": iso(horloge.t - 2000)})
        self.api.jobs[901] = [job_view("psql 1/1", status="queued", job_id=9011,
                                       created_at=iso(horloge.t - 2000))]
        self.watcher.tick(self.db)
        (file_avant,) = storage.of(self.db).ci.queued_oldest()
        runner_avant = self.runner_seen_ts(21)
        self.api.runners_fail = True
        horloge.avance(ci.GUARD_SCAN_S)
        self.watcher.tick(self.db)
        self.assertTrue(any("guard failed" in line for line in logs), logs)
        # 2 listes + 1 jobs + l'appel runners qui échoue (compté, puis levé)
        self.assertEqual(self.scan_state(), (False, "failed", 4))
        (file_apres,) = storage.of(self.db).ci.queued_oldest()
        self.assertEqual(file_apres["seen_ts"], file_avant["seen_ts"],
                         "une erreur du relevé a publié la file")
        self.assertEqual(self.runner_seen_ts(21), runner_avant,
                         "une erreur du relevé a rafraîchi les runners")


if __name__ == "__main__":
    unittest.main()
