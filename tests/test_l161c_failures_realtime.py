# SPDX-License-Identifier: AGPL-3.0-only
"""L161c — chaque échec de test remonté en temps réel au courrier de l'agent du lot.

Côté CI, `scripts/ci-flow.py publish` tient le check run « tests (flux) »
pendant la part et `close` le termine ; côté ameesh, `ci.Watcher` (mené par
`ameesh notify` sur l'hôte `github.host`) relève les exécutions en cours,
envoie un urgent au premier échec d'une tête, des événements ordinaires
ensuite et le verdict à la fin, n'impute jamais un rouge hérité de main, et
retire les messages non lus d'une tête remplacée. `ci_main_red` signale main
rouge ; `ameesh ci status` montre les exécutions d'un lot.
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
import unittest

from ameesh import ci, exploitation, mail, notify, registry, storage, work

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
    (découverte, #1110), exécutions par état — `pending` compris (#1161) —,
    tête d'une branche, état d'une exécution, check runs d'une tête, et
    l'écriture d'annulation."""

    def __init__(self):
        self.live, self.queued, self.pending, self.done, self.checks = [], [], [], {}, {}
        #: exécutions terminées (rattrapage borné #1110)
        self.terminated = []
        #: fichiers de workflow que le dépôt publie (découverte #1110)
        self.workflows = ["tests.yml"]
        #: tête de branche rendue par l'API (#1161)
        self.branches = {}
        #: annulations demandées (#1161), et l'erreur que l'écriture rend
        self.cancels, self.cancel_error = [], None

    def cancel(self, repo, run_id):
        """L'écriture d'annulation (#1161) : l'exécution quitte les listes
        vivantes et devient terminée annulée, comme chez GitHub."""
        if self.cancel_error:
            raise self.cancel_error
        self.cancels.append((repo, int(run_id)))
        for bucket in (self.live, self.queued, self.pending):
            for run in list(bucket):
                if int(run["id"]) == int(run_id):
                    bucket.remove(run)
        self.done[int(run_id)] = {"status": "completed", "conclusion": "cancelled"}

    def _status_runs(self, path):
        for status, bucket in (("queued", self.queued), ("pending", self.pending),
                               ("in_progress", self.live)):
            if "status=%s" % status in path:
                return list(bucket)
        return []  # requested, waiting : aucun run dans ces états

    def __call__(self, path):
        if "/actions/runs?" in path:
            if "status=completed" in path:
                return {"workflow_runs": list(self.terminated)}
            return {"workflow_runs": self._status_runs(path)}
        if "/actions/workflows" in path:
            return {"workflows": [item if isinstance(item, dict) else
                                  {"path": ".github/workflows/%s" % item, "state": "active"}
                                  for item in self.workflows]}
        if "/branches/" in path:
            return {"commit": {"sha": self.branches.get(path.split("/branches/", 1)[1], "")}}
        if "/actions/runs/" in path:
            run_id = int(path.rsplit("/", 1)[1])
            if run_id in self.done:
                return self.done[run_id]
            for bucket in (self.live, self.queued, self.pending):
                for run in bucket:
                    if int(run["id"]) == run_id:
                        return run
            return {"status": "in_progress"}
        if "/check-runs" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            return {"check_runs": self.checks.get(sha, [])}
        raise AssertionError(path)


def run_view(run_id, sha, branch, *, base="main", message="", workflow="tests.yml",
             attempt=1, updated_at=None, status=None, created_at=None):
    view = {"id": run_id, "head_sha": sha, "head_branch": branch, "event": "pull_request"
            if branch != "main" else "push", "path": ".github/workflows/%s" % workflow,
            "run_attempt": attempt,
            "html_url": "https://github.com/%s/actions/runs/%d" % (DEPOT, run_id),
            "pull_requests": [{"base": {"ref": base}}] if branch != "main" else [],
            "head_commit": {"message": message}}
    if updated_at:
        view["updated_at"] = updated_at
    if created_at:
        view["created_at"] = created_at
    if status:
        view["status"] = status
    return view


def completed(path, conclusion, attempt=1):
    """L'état GitHub d'une exécution conclue (relevé par `_conclude`)."""
    return {"status": "completed", "conclusion": conclusion, "path": path,
            "run_attempt": attempt}


class WatcherTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ("ci_test_failures", "ci_runs"):
            self.db.execute("DELETE FROM %s" % table)
        registry.upsert(self.db, "dev-a", harness="claude", host=self.cfg.host)
        registry.upsert(self.db, "dev-b", harness="claude", host=self.cfg.host)
        self.lot = work.add(self.db, title="L1 : un lot", branch="lot/1-un", assignee="dev-a")
        self.lot_main = work.add(self.db, title="L0 : déjà fusionné", assignee="dev-b")
        self.cfg_ci = dataclasses.replace(
            self.cfg, github={"projects": {"ameesh": DEPOT}, "host": self.cfg.host})
        self.api = FauxApi()
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel)
        # une exécution verte de main déjà relevée : la référence du rouge hérité
        # (nommée `tests.yml` : la migration 0433 nomme les lignes antérieures)
        st = storage.of(self.db)
        st.ci.open_run(DEPOT, 100, head_sha="0" * 40, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.conclude(DEPOT, 100, "success")

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
        self.assertEqual(ci.Watcher(cfg, api=self.api, cancel=self.api.cancel).repos(), [])
        self.assertEqual(self.watcher.repos(), [DEPOT])

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

    def test_pending_lu_suivi_et_conclu(self):
        # #1161 : une exécution bloquée en `pending` (groupe de concurrence non
        # obtenu) est lue dès la poussée, garde une seule ligne quand elle
        # repasse `queued`, puis reçoit son verdict et son réveil
        sha = "e1" * 20
        self.api.pending = [run_view(601, sha, "lot/1-un", status="pending")]
        self.assertEqual(self.watcher.tick(self.db), [])
        row = storage.of(self.db).ci.run(DEPOT, 601)
        self.assertEqual((row["status"], row["work_item_id"]), ("running", self.lot["id"]))
        compteur = "SELECT count(*) AS n FROM ci_runs WHERE run_id = 601"
        self.assertEqual(self.db.query(compteur)[0]["n"], 1)
        # relue `pending` : rien de neuf, pas de doublon
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(self.db.query(compteur)[0]["n"], 1)
        # la même exécution passe `queued` : toujours une seule ligne
        self.api.pending = []
        self.api.queued = [run_view(601, sha, "lot/1-un", status="queued")]
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(self.db.query(compteur)[0]["n"], 1)
        # conclusion : verdict et réveil, une fois
        self.api.queued = []
        self.api.done[601] = completed(".github/workflows/tests.yml", "success")
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["status"]) for o in out],
                         [("verdict", "success")])
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 601)["status"], "success")
        self.assertEqual(len(self.mails("dev-a")), 1)

    def test_pending_annule_les_executions_supplantees(self):
        # #1161 : le groupe de concurrence est retenu par une exécution non
        # démarrée d'une tête supplantée ; le relevé l'annule, jamais la tête
        # courante, et l'écriture est idempotente
        vieille, neuve = "f1" * 20, "f2" * 20
        self.api.queued = [run_view(611, vieille, "lot/1-un", status="queued")]
        self.assertEqual(self.watcher.tick(self.db), [])
        self.api.pending = [run_view(612, neuve, "lot/1-un", status="pending")]
        self.api.branches["lot/1-un"] = neuve
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["run_id"]) for o in out],
                         [("superseded", 611), ("cancelled", 611)])
        self.assertIn("cancelled to free", "\n".join(ci.describe(o) for o in out))
        self.assertEqual(self.api.cancels, [(DEPOT, 611)])
        st = storage.of(self.db)
        self.assertEqual(st.ci.run(DEPOT, 611)["status"], "superseded")
        self.assertEqual(st.ci.run(DEPOT, 612)["status"], "running")
        # l'exécution annulée n'est plus vivante : rien à refaire, aucun doublon
        self.assertEqual([o for o in self.watcher.tick(self.db)
                          if o["action"] == "cancelled"], [])
        self.assertEqual(self.api.cancels, [(DEPOT, 611)])

    def test_annulation_reservee_aux_pr_supplantees(self):
        # #1161 : jamais `main`, jamais `release/…`, jamais la tête courante,
        # jamais une exécution plus récente que le successeur (rerun)
        self.api.queued = [run_view(621, "1a" * 20, "main", status="queued")]
        self.api.pending = [run_view(622, "1b" * 20, "main", status="pending")]
        self.api.branches["main"] = "1b" * 20
        self.watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        self.api.queued = [run_view(623, "1c" * 20, "release/v1.10.0", status="queued")]
        self.api.pending = [run_view(624, "1d" * 20, "release/v1.10.0", status="pending")]
        self.api.branches["release/v1.10.0"] = "1d" * 20
        self.watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        # la tête courante : deux exécutions de la même tête, dont une `pending`
        sha = "2a" * 20
        self.api.queued = [run_view(631, sha, "lot/1-un", status="queued")]
        self.api.pending = [run_view(632, sha, "lot/1-un", status="pending")]
        self.api.branches["lot/1-un"] = sha
        self.watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        # un rerun d'une tête ancienne, d'id plus récent que le successeur,
        # n'est pas une exécution supplantée : il n'est pas annulé (suivi des
        # reruns : lot #901)
        ancienne, courante = "2b" * 20, "2c" * 20
        self.api.queued = [run_view(641, courante, "lot/1-un", status="queued")]
        self.api.pending = [run_view(642, ancienne, "lot/1-un", status="pending")]
        self.api.branches["lot/1-un"] = courante
        self.watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])

    def test_erreur_d_annulation_sans_faux_verdict(self):
        # #1161 : une erreur de l'API d'annulation est journalisée ; elle ne
        # conclut rien, ne retire rien et laisse la nouvelle exécution suivie
        journal = []
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             log=journal.append)
        vieille, neuve = "3a" * 20, "3b" * 20
        self.api.queued = [run_view(651, vieille, "lot/1-un", status="queued")]
        watcher.tick(self.db)
        self.api.pending = [run_view(652, neuve, "lot/1-un", status="pending")]
        self.api.branches["lot/1-un"] = neuve
        self.api.cancel_error = RuntimeError("boom")
        out = watcher.tick(self.db)
        self.assertEqual([o["action"] for o in out], ["superseded"])
        self.assertTrue(any("cancel of run 651 failed" in line for line in journal))
        st = storage.of(self.db)
        self.assertEqual(st.ci.run(DEPOT, 651)["status"], "superseded")
        self.assertEqual(st.ci.run(DEPOT, 652)["status"], "running")
        self.assertEqual(self.mails("dev-a"), [])

    def test_pending_signale_apres_cinq_minutes(self):
        # #1161 : une exécution de la tête courante restée `pending` au-delà du
        # délai est signalée à l'assigné du lot, une seule fois par service
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: 1_000_000.0)
        sha = "4a" * 20
        self.api.pending = [run_view(661, sha, "lot/1-un", status="pending",
                                     created_at="1970-01-12T13:39:40+00:00")]
        self.api.branches["lot/1-un"] = sha
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o["to"], o["waited_s"], o["cancelled"]) for o in out],
                         [("pending", "dev-a", 420, [])])
        self.assertIn("pending 7 min", "\n".join(ci.describe(o) for o in out))
        (m,) = self.mails("dev-a")
        self.assertIn("pending for 7 min", m["body"])
        self.assertIn("held elsewhere", m["body"])
        self.assertFalse(mail.is_urgent(m))
        self.assertEqual(self.payload_of(m)["ci"]["kind"], "pending")
        # déjà signalée : le passage suivant ne double pas le message
        self.assertEqual(watcher.tick(self.db), [])
        self.assertEqual(len(self.mails("dev-a")), 1)
        # une exécution `queued` de la même tête (attente de runner, banale)
        # n'est jamais signalée : seul `pending` attend un groupe
        self.api.pending = []
        self.api.queued = [run_view(662, sha, "lot/1-un", status="queued",
                                    created_at="1970-01-12T13:39:40+00:00")]
        self.assertEqual(watcher.tick(self.db), [])
        self.assertEqual(len(self.mails("dev-a")), 1)

    def test_pending_signale_et_debloque(self):
        # #1161 : quand une exécution supplantée non démarrée retient le
        # groupe, elle est annulée et le signalement le dit
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: 1_000_000.0)
        vieille, neuve = "5a" * 20, "5b" * 20
        self.api.queued = [run_view(671, vieille, "lot/1-un", status="queued")]
        watcher.tick(self.db)
        self.api.pending = [run_view(672, neuve, "lot/1-un", status="pending",
                                     created_at="1970-01-12T13:39:40+00:00")]
        self.api.branches["lot/1-un"] = neuve
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o["run_id"]) for o in out],
                         [("superseded", 671), ("cancelled", 671), ("pending", 672)])
        (m,) = self.mails("dev-a")
        self.assertIn("671 cancelled", m["body"])
        self.assertEqual(self.payload_of(m)["ci"]["cancelled"], [671])

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
        watcher = ci.Watcher(cfg, api=api, cancel=api.cancel)
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
        watcher = ci.Watcher(cfg, api=api, cancel=api.cancel)
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
        watcher = ci.Watcher(cfg, api=api, cancel=api.cancel, log=journal.append)
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
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
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
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
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
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
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


if __name__ == "__main__":
    unittest.main()
