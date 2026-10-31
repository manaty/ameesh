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

#795 N2 : le courrier d'un événement de CI et sa preuve
(`ci_runs.urgent_message_id` / `verdict_message_id`) sont publiés dans la
même transaction — un lecteur intercalé ne voit jamais le courrier engagé
sans la référence qui rend la préemption vérifiable.
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
import types
import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import unquote

from ameesh import ci, db as db_mod, exploitation, mail, notify, registry, runner, storage, work
from ameesh.storage.postgres.ci import CiResults

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


def check_run(events, name="tests (flux) psycopg 1/4", attempt=1):
    """Un check run tel que GitHub le rend, sortie produite par ci-flow.py.

    `attempt` est celui de la part qui publie (#901) : GitHub ne rend, pour un
    même nom, que le dernier check publié."""
    return {"name": name, "output": ciflux.check_output(events, attempt)}


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


class TapNodeTest(unittest.TestCase):
    """#3783 : le lecteur reconnaît les lignes `not ok` d'un log node réel."""

    def fixture(self) -> str:
        return (REPO / "tests" / "fixtures" / "l3783-node-tap.log").read_text(
            encoding="utf-8")

    def test_log_node_reel(self):
        """Le log du job `scripts` du run 40354337937 rend ses trois témoins."""
        failures = ci.tap_failures(self.fixture())
        self.assertEqual(
            [f["test"] for f in failures],
            ["CI-JOB-TIMEOUTS: every job carries a bounded timeout-minutes",
             "the fail-closed wiring bites: dropping the plan from the terminal is caught",
             "CI-TMPDIR (#686): the /tmp-heavy workflows keep their temp files on the runner"])
        self.assertEqual(failures[0]["failed_at"], "2026-10-26T15:58:08.902448Z")
        self.assertIn("timeout-minutes outside [5, 180] minutes", failures[0]["excerpt"])
        self.assertIn("ci-job-timeouts.test.mjs", failures[0]["module"])
        # ni le `# fail 3` du résumé ni les `ok` du log ne sont des tests
        self.assertEqual(len(failures), 3)

    def test_directives_et_faux_positifs(self):
        text = "\n".join([
            "TAP version 13",
            "ok 1 - un test vert",
            "not ok 2 - attendu # TODO",
            "not ok 3 - sauté # SKIP",
            "error: |-",
            "  not ok dans une ligne d'extrait, sans numéro",
            "not ok 4 - réellement rouge",
            "  ---",
            "  error: 'boom'",
            "  ...",
        ])
        failures = ci.tap_failures(text)
        self.assertEqual([f["test"] for f in failures], ["réellement rouge"])
        self.assertIn("boom", failures[0]["excerpt"])

    def test_ligne_numerotee_hors_ligne_ou_dans_extrait(self):
        """#3783 B/P2 de la relecture : une ligne TAP citée n'est pas un test.

        Seule une ligne qui *commence* par un résultat TAP après le préfixe
        GitHub compte ; un extrait d'assertion ou un bloc YAML numéroté reste
        du diagnostic, tandis qu'un vrai sous-test indenté est reconnu."""
        text = "\n".join([
            "error: expected not ok 1 - exemple",
            "console.log('not ok 2 - exemple')",
            "not ok 3 - vrai échec",
            "  ---",
            "  error: |-",
            "    not ok 4 - dans le bloc YAML",
            "  ...",
            "    not ok 5 - sous-test indenté",
            "      ---",
            "      error: 'boom'",
            "      ...",
        ])
        self.assertEqual([f["test"] for f in ci.tap_failures(text)],
                         ["vrai échec", "sous-test indenté"])

    def test_nom_tap_echappe_et_ansi(self):
        text = "\x1b[31m2026-10-26T15:58:08.9024484Z not ok 7 - \\#686 : TMPDIR\x1b[0m\n"
        failures = ci.tap_failures(text)
        self.assertEqual([f["test"] for f in failures], ["#686 : TMPDIR"])


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
        # #901 : la sortie publiée à l'attempt 2 n'est relue que pour l'attempt
        # 2 ; celle de l'attempt 1 (encore le dernier check publié tant que la
        # relance n'a pas republié) n'est jamais imputée à la relance
        relance = [{"name": "tests (flux) p 1/1", "output": ciflux.check_output(events, 2)}]
        self.assertEqual([e["test"] for e in ci.events_of(relance, 101, 2)],
                         ["tests.test_a.T.test_ko", "tests.test_a.T.test_err"])
        self.assertEqual([e["attempt"] for e in ci.events_of(relance, 101, 2)], [2, 2])
        self.assertEqual(ci.events_of(relance, 101, 1), [])
        # sans filtre, le relevé voit toutes les lignes avec leur provenance
        self.assertEqual([ci.event_attempt(e) for e in ci.events_of(relance, 101)], [2, 2])
        # l'ancien check et celui de la relance, du même nom, ne se masquent pas
        les_deux = [{"name": "tests (flux) p 1/1", "output": sortie},
                    {"name": "tests (flux) p 1/1", "output": ciflux.check_output(events, 2)}]
        self.assertEqual([ci.event_attempt(e) for e in ci.events_of(les_deux, 101)],
                         [1, 1, 2, 2])
        # une ligne publiée avant #901 (sans marque) vaut attempt 1 et n'est
        # jamais imputée à une relance
        sans_marque = {"name": "tests (flux) p 1/1",
                       "output": {"text": ciflux.MARKER + "\n```jsonl\n"
                                  + json.dumps(ev_test("tests.test_a.T.test_ko")) + "\n```"}}
        self.assertEqual([e["test"] for e in ci.events_of([sans_marque], 101, 1)],
                         ["tests.test_a.T.test_ko"])
        self.assertEqual(ci.events_of([sans_marque], 101, 2), [])
        self.assertEqual([ci.event_attempt(e) for e in ci.events_of([sans_marque], 101)], [1])

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

    def test_deux_ecrivains_de_l_etat_ne_se_volent_pas_le_temporaire(self):
        """#3336 : `publish` (arrière-plan) et `close` (`if: always()`) écrivent
        l'état de la même part. Avec un temporaire à nom fixe, le renommage de
        l'un consommait le fichier de l'autre : `close` échouait en
        FileNotFoundError (main, run 40084134277) et rougissait la part alors
        que ses 920 tests étaient verts. Chaque écrivain garde son propre
        temporaire jusqu'à son propre renommage."""
        a = ciflux.Publication(str(self.flux), "tests (flux) p 1/1", FauxGitHub())
        b = ciflux.Publication(str(self.flux), "tests (flux) p 1/1", FauxGitHub())
        vrai_replace = ciflux.os.replace
        passe = []

        def replace_avec_l_autre(src, dst):
            if not passe:
                passe.append(1)
                # l'autre écrivain écrit puis renomme *entre* notre écriture et
                # notre renommage : c'est l'entrelacement qui a rougi la CI.
                b._keep()
            return vrai_replace(src, dst)

        with mock.patch.object(ciflux.os, "replace", side_effect=replace_avec_l_autre):
            a._keep()
        self.assertEqual(json.loads((self.dossier / "flux.jsonl.check").read_text()),
                         a.etat)

    def test_etat_non_ecrit_ne_rougit_pas_l_etape(self):
        """#3336 : la publication d'un check run n'échoue jamais la part, même
        contrat que le refus de GitHub : un état non écrit est consigné."""
        self.ecrire(ev_test("tests.test_a.T.test_ko"))
        gh = FauxGitHub()
        err = io.StringIO()
        with mock.patch.object(ciflux.Publication, "_keep",
                               side_effect=OSError("disque plein")):
            with contextlib.redirect_stderr(err):
                self.assertEqual(ciflux.close(self.args(), gh=gh), 0)
        self.assertIn("non écrit", err.getvalue())

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
        # #901 : l'attempt paraît dans l'en-tête dès qu'il y en a plusieurs
        self.assertIn("attempt 2", ci.verdict_text(dict(run, run_attempt=2), lot, "success", []))
        self.assertNotIn("attempt", ci.verdict_text(run, lot, "success", []))
        self.assertEqual(ci.describe({"action": "rerun", "repo": DEPOT, "run_id": 801,
                                      "attempt": 2}),
                         "CI %s run 801 re-run attempt 2: tracking reopened" % DEPOT)


# --------------------------------------------------------------------------
# côté ameesh : le relevé sur une vraie base
# --------------------------------------------------------------------------

class FauxApi:
    """L'API GitHub vue par le relevé : workflows publiés par le dépôt
    (découverte, #1110), exécutions par état — `pending` compris (#1161) —,
    tête d'une branche, état d'une exécution, check runs d'une tête,
    l'écriture d'annulation, et (#845) runners et jobs."""

    def __init__(self):
        self.live, self.queued, self.pending, self.done, self.checks = [], [], [], {}, {}
        #: exécutions terminées (rattrapage borné #1110)
        self.terminated = []
        #: fichiers de workflow que le dépôt publie (découverte #1110)
        self.workflows = ["tests.yml"]
        #: #845 : runners du dépôt, jobs par exécution, liste d'exécutions
        #: tronquée, panne de l'API des runners
        self.runners, self.jobs, self.truncated, self.runners_fail = [], {}, False, False
        #: #846/#901 B1 : jobs d'une tentative — (run_id, attempt) -> jobs
        self.attempt_jobs: dict[tuple[int, int], list[dict]] = {}
        #: #1612 : état d'une tentative — (run_id, attempt) -> exécution
        self.attempts: dict[tuple[int, int], dict] = {}
        #: #845 : chemins appelés, pour compter les appels de la garde
        self.paths: list[str] = []
        #: #3783 : logs de job lus (`(dépôt, job_id) -> texte`) et appels vus
        self.log_text: dict[int, str] = {}
        self.log_calls: list[tuple[str, int]] = []
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

    def log(self, repo, job_id):
        """#3783 : le log d'un job, comme `ci.gh_job_log` le rendrait."""
        self.log_calls.append((repo, int(job_id)))
        return self.log_text.get(int(job_id), "")

    def _status_runs(self, path):
        for status, bucket in (("queued", self.queued), ("pending", self.pending),
                               ("in_progress", self.live)):
            if "status=%s" % status in path:
                return list(bucket)
        return []  # requested, waiting : aucun run dans ces états

    def __call__(self, path):
        self.paths.append(path)
        if "/actions/runs/" in path and "/jobs" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            jobs = self.jobs.get(run_id, [])
            if "/attempts/" in path:  # jobs d'une tentative (#846/#901 B1)
                attempt = int(path.split("/attempts/")[1].split("/")[0])
                jobs = self.attempt_jobs.get((run_id, attempt), [])
            return {"total_count": len(jobs), "jobs": list(jobs)}
        if "/actions/runners" in path:
            if self.runners_fail:
                raise RuntimeError("403 runners: Resource not accessible")
            return {"total_count": len(self.runners), "runners": list(self.runners)}
        if "/actions/runs?" in path:
            if "status=completed" in path:
                runs = list(self.terminated)
                # #2935 : le relevé de la tête d'une base filtre par branche
                if "branch=" in path:
                    branch = unquote(path.split("branch=")[1].split("&")[0])
                    runs = [r for r in runs
                            if (r.get("head_branch") or "") == branch]
                # #2938 : l'API pagine par 100 comme GitHub ; `page` 1 par défaut
                total = len(runs)
                page = 1
                if "&page=" in path:
                    page = int(path.split("&page=")[1].split("&")[0])
                runs = runs[(page - 1) * 100:page * 100]
                return {"total_count": total, "workflow_runs": runs}
            runs = self._status_runs(path)
            return {"total_count": len(runs) + (1 if self.truncated else 0),
                    "workflow_runs": runs}
        if "/actions/workflows" in path:
            return {"workflows": [item if isinstance(item, dict) else
                                  {"path": ".github/workflows/%s" % item, "state": "active"}
                                  for item in self.workflows]}
        if "/branches/" in path:
            return {"commit": {"sha": self.branches.get(path.split("/branches/", 1)[1], "")}}
        if "/actions/runs/" in path and "/attempts/" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            attempt = int(path.rsplit("/", 1)[1])
            return self.attempts.get((run_id, attempt), {"status": "in_progress",
                                                         "run_attempt": attempt})
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


def job_view(name, *, status="in_progress", runner="", job_id=None, created_at=None,
             conclusion=None):
    job = {"name": name, "status": status, "runner_name": runner}
    if conclusion is not None:
        job["conclusion"] = conclusion
    if job_id is not None:
        job["id"] = job_id
    if created_at is not None:
        job["created_at"] = created_at
    return job


def run_view(run_id, sha, branch, *, base="main", message="", workflow="tests.yml",
             attempt=1, updated_at=None, status=None, created_at=None,
             run_started_at=None, pr=1, source=DEPOT):
    pull_request = branch != "main" and pr is not None
    view = {"id": run_id, "head_sha": sha, "head_branch": branch,
            "event": "pull_request" if pull_request else "push",
            "path": ".github/workflows/%s" % workflow,
            "run_attempt": attempt,
            "head_repository": {"full_name": source},
            "html_url": "https://github.com/%s/actions/runs/%d" % (DEPOT, run_id),
            "run_attempt": attempt,
            "pull_requests": [{"number": pr, "base": {"ref": base}}] if pull_request else [],
            "head_commit": {"message": message}}
    if updated_at:
        view["updated_at"] = updated_at
    if created_at:
        view["created_at"] = created_at
    if run_started_at:
        view["run_started_at"] = run_started_at
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
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                                  logs=self.api.log)
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
        self.assertEqual(ci.Watcher(cfg, api=self.api, cancel=self.api.cancel).repos(), [])
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

    def test_run_conclu_sans_lot_recoit_son_verdict_a_la_pose(self):
        """#2021 : une ligne terminale restée sans lot (branche posée après la
        conclusion) est rattachée au rattrapage et son verdict part une seule
        fois à l'assigné — le cas des runs de paquet vus avant la pose."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        registry.upsert(self.db, "dev-d", harness="claude", host=self.cfg.host)
        sha = "d1" * 20
        self.api.live = [run_view(230, sha, "agent/dev-d-230")]
        watcher.tick(self.db)
        self.assertIsNone(storage.of(self.db).ci.run(DEPOT, 230)["work_item_id"])
        # l'exécution se conclut avant qu'aucun lot ne porte la branche : la
        # ligne reste terminale, sans lot et sans verdict
        self.api.live = []
        self.api.done[230] = completed(".github/workflows/tests.yml", "success")
        self.api.terminated = [self.terminated(230, sha, "agent/dev-d-230",
                                               conclusion="success")]
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o["to"]) for o in out], [("verdict", None)])
        self.assertEqual(self.mails("dev-d"), [])
        # la branche du lot est posée : le rattrapage rattache la ligne
        # terminale et délivre son verdict, une seule fois
        lot = work.add(self.db, title="L4 : branche posée après la conclusion",
                       branch="agent/dev-d-230", assignee="dev-d")
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o["to"]) for o in out], [("verdict", "dev-d")])
        row = storage.of(self.db).ci.run(DEPOT, 230)
        self.assertEqual(row["work_item_id"], lot["id"])
        self.assertIsNotNone(row["verdict_message_id"])
        self.assertIn(": green.", self.mails("dev-d")[-1]["body"])
        self.assertEqual(watcher.tick(self.db), [])
        self.assertEqual(len(self.mails("dev-d")), 1)

    def test_ligne_terminale_sans_lot_non_rattachee_si_tete_remplacee(self):
        """#2021 : une tête plus récente vue sur la branche laisse la ligne
        terminale sans lot — son verdict serait périmé (même garde que #1321)."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        registry.upsert(self.db, "dev-e", harness="claude", host=self.cfg.host)
        vieille, neuve = "e1" * 20, "e2" * 20
        self.api.live = [run_view(240, vieille, "agent/dev-e-240")]
        watcher.tick(self.db)
        self.api.live = []
        self.api.done[240] = completed(".github/workflows/tests.yml", "success")
        self.api.terminated = [self.terminated(240, vieille, "agent/dev-e-240",
                                               conclusion="success")]
        self.assertEqual([o["action"] for o in watcher.tick(self.db)], ["verdict"])
        # une tête plus récente de la même branche a été vue
        self.api.live = [run_view(241, neuve, "agent/dev-e-240")]
        self.api.terminated = []
        watcher.tick(self.db)
        self.api.live = []
        work.add(self.db, title="L5 : tête remplacée", branch="agent/dev-e-240",
                 assignee="dev-e")
        self.api.terminated = [self.terminated(240, vieille, "agent/dev-e-240",
                                               conclusion="success")]
        self.assertEqual(watcher.tick(self.db), [])
        self.assertIsNone(storage.of(self.db).ci.run(DEPOT, 240)["work_item_id"])
        self.assertEqual(self.mails("dev-e"), [])

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
        # fin de l'exécution : verdict rouge, ordinaire (#514 : la fin de CI
        # réveille l'agent, elle ne perce pas le tour en cours)
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

    def _deux_suites(self, sha):
        """#5044 : deux workflows sur la même tête du lot, tous deux en cours."""
        self.api.workflows = ["backend-suite.yml", "website-suite.yml"]
        self.api.live = [run_view(221, sha, "lot/1-un", workflow="backend-suite.yml"),
                         run_view(222, sha, "lot/1-un", workflow="website-suite.yml")]
        self.assertEqual(self.watcher.tick(self.db), [])

    def _termine(self, run_id, conclusion):
        path = next(r["path"] for r in self.api.live if r["id"] == run_id)
        self.api.live = [r for r in self.api.live if r["id"] != run_id]
        self.api.done[run_id] = completed(path, conclusion)
        return self.watcher.tick(self.db)

    def test_5044_seul_le_dernier_verdict_d_une_tete_reveille(self):
        self._deux_suites("e1" * 20)
        self._termine(221, "success")
        (vert,) = self.mails("dev-a")
        self.assertEqual(mail.passive_reason(vert), "info")
        self.assertIn("still running: website-suite.yml", vert["body"])
        self._termine(222, "success")
        self.assertEqual(mail.passive_reason(self.mails("dev-a")[-1]), "")

    def test_5044_rouge_non_annonce_reveille_meme_avant_la_fin(self):
        self._deux_suites("e2" * 20)
        self._termine(221, "failure")
        (rouge,) = self.mails("dev-a")
        self.assertEqual(mail.passive_reason(rouge), "")

    def test_5044_rouge_deja_annonce_passif_avant_la_fin(self):
        sha = "e3" * 20
        # la base de cette suite est connue : le premier échec est urgent
        st = storage.of(self.db)
        st.ci.open_run(DEPOT, 101, head_sha="1" * 40, branch="main", event="push",
                       base="main", work_item_id=None, url=None,
                       workflow="backend-suite.yml")
        st.ci.conclude(DEPOT, 101, "success")
        self._deux_suites(sha)
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=221)])]
        self.watcher.tick(self.db)
        premier = self.mails("dev-a")[0]
        self.assertTrue(mail.is_urgent(premier))
        self._termine(221, "failure")
        self.assertEqual(mail.passive_reason(self.mails("dev-a")[-1]), "info")
        self._termine(222, "success")
        self.assertEqual(mail.passive_reason(self.mails("dev-a")[-1]), "")

    def test_5044_echecs_suivants_passifs(self):
        sha = "e4" * 20
        self.api.live = [run_view(223, sha, "lot/1-un")]
        self.watcher.tick(self.db)
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=223)])]
        self.watcher.tick(self.db)
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=223),
                                           ev_test("tests.test_b.T.test_2", execution=223)])]
        self.watcher.tick(self.db)
        premier, suivant = self.mails("dev-a")
        self.assertEqual(mail.passive_reason(premier), "")
        self.assertTrue(mail.is_urgent(premier))
        self.assertEqual(mail.passive_reason(suivant), "info")
        self.assertIn("tests.test_b.T.test_2", suivant["body"])

    def test_5044_dernier_annule_reveille_a_la_place_du_verdict(self):
        """Le signal d'action : sans cet avis, un agent au repos ne lirait jamais
        le verdict passif de la suite déjà conclue."""
        self._deux_suites("e5" * 20)
        self._termine(221, "success")
        self._termine(222, "cancelled")
        passif, avis = self.mails("dev-a")
        self.assertEqual(mail.passive_reason(passif), "info")
        self.assertEqual(mail.passive_reason(avis), "")
        self.assertIn("backend-suite.yml success", avis["body"])

    def test_5044_annule_seul_reste_muet(self):
        self.api.live = [run_view(224, "e6" * 20, "lot/1-un")]
        self.watcher.tick(self.db)
        self._termine(224, "cancelled")
        self.assertEqual(self.mails("dev-a"), [])

    def test_rouge_sans_flux_lit_les_not_ok_tap(self):
        """#3783 : un job `node --test` rouge sans flux JSON donne ses tests.

        Le run 40354337937 (paquet Nexlink 191) avait trois `not ok` TAP et
        `ameesh ci status` rendait « red 0 failure(s) » : le verdict disait
        « no failed test seen » et le rouge passait pour non concluant."""
        sha = "a1" * 20
        self.api.live = [run_view(210, sha, "lot/1-un")]
        self.assertEqual(self.watcher.tick(self.db), [])
        self.api.attempt_jobs[(210, 1)] = [
            job_view("scripts", status="completed", conclusion="failure", job_id=991)]
        self.api.log_text[991] = (REPO / "tests" / "fixtures"
                                  / "l3783-node-tap.log").read_text(encoding="utf-8")
        # l'exécution tourne encore : le relevé ne paie pas les logs
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(self.api.log_calls, [])
        self.api.live = []
        self.api.done[210] = completed(".github/workflows/tests.yml", "failure")
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("urgent")) for o in out],
                         [("failures", True), ("verdict", None)])
        mails = self.mails("dev-a")
        self.assertEqual(len(mails), 2)
        self.assertTrue(mail.is_urgent(mails[0]))
        self.assertIn("CI-JOB-TIMEOUTS", mails[0]["body"])
        self.assertIn("red, 3 failure(s)", mails[1]["body"])
        self.assertNotIn("no failed test", mails[1]["body"])
        self.assertEqual(self.payload_of(mails[1])["ci"]["failures"], 3)
        report = ci.status_of(self.db, self.lot["id"])
        self.assertEqual(len(report["runs"][0]["failures"]), 3)
        texte = "\n".join(ci.render_status(report))
        self.assertIn("red  3 failure(s)", texte)
        self.assertIn("CI-TMPDIR (#686)", texte)
        self.assertEqual(self.api.log_calls, [(DEPOT, 991)])

    def test_rouge_sans_test_tap_ne_l_invente_pas(self):
        """#3783 : un job rouge sans `not ok` reste non concluant (canon §3) —
        une panne d'installation n'est pas un test négatif."""
        self.api.live = [run_view(211, "a2" * 20, "lot/1-un")]
        self.watcher.tick(self.db)
        self.api.live = []
        self.api.done[211] = completed(".github/workflows/tests.yml", "failure")
        self.api.attempt_jobs[(211, 1)] = [
            job_view("scripts", status="completed", conclusion="failure", job_id=992)]
        self.api.log_text[992] = "npm ERR! install failed\n# fail 0\n"
        self.watcher.tick(self.db)
        verdict = self.mails("dev-a")[-1]
        self.assertIn("no failed test seen", verdict["body"])
        self.assertEqual(storage.of(self.db).ci.failures(DEPOT, 211), [])

    def test_vert_sans_echec(self):
        self.api.live = [run_view(202, "d" * 40, "lot/1-un")]
        self.watcher.tick(self.db)
        self.api.live, self.api.done[202] = [], {"status": "completed", "conclusion": "success"}
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["status"], o["to"]) for o in out], [("success", "dev-a")])
        self.assertIn(": green.", self.mails("dev-a")[0]["body"])
        # #514 : le verdict vert réveille l'agent, il ne perce pas son tour
        self.assertFalse(mail.is_urgent(self.mails("dev-a")[0]))

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

    def test_echec_de_main_sans_lot_va_au_pilote_du_projet(self):
        """#2935 : une poussée sur une base n'appartient souvent à aucun lot
        (une fusion de paquet ne porte pas la ligne `ameesh-work:`) ; l'échec
        partait « (no recipient) ». Il va au pilote du projet, qui peut le
        traiter et suivre la fermeture de l'alerte."""
        canon = types.SimpleNamespace(agents=[
            types.SimpleNamespace(
                title="mesh-design",
                fiche=types.SimpleNamespace(data={"roles": ["orchestrateur"]}))])
        registry.upsert(self.db, "mesh-design", harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET team = %s WHERE name = %s",
                        ("ameesh", "mesh-design"))
        sha = "7" * 40
        self.api.live = [run_view(310, sha, "main")]
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1",
                                                   execution=310)])]
        with mock.patch("ameesh.canon.load_configured", return_value=[canon]):
            out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["to"], o["urgent"]) for o in out],
                         [("failures", "mesh-design", True)])
        (message,) = self.mails("mesh-design")
        self.assertIn("tests.test_a.T.test_1", message["body"])

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

    # -- relance du même run_id (#901) --------------------------------------

    def test_relance_rouvre_la_ligne_et_suit_l_attempt(self):
        """`gh run rerun` garde le run_id et passe à l'attempt suivant : la
        ligne conclue est rouverte, l'attempt suivi, le verdict envoyé. Tant
        que la relance est en file, le check run de l'attempt 1 reste le
        dernier publié : ses échecs ne sont pas imputés à l'attempt 2."""
        sha = "9" * 40
        # attempt 1 : un échec, puis annulation (le cas du seuil psql)
        self.api.live = [run_view(801, sha, "lot/1-un", attempt=1)]
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=801)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["urgent"]) for o in out], [("failures", True)])
        self.api.live = []
        self.api.done[801] = {"status": "completed", "conclusion": "cancelled"}
        self.watcher.tick(self.db)
        row = storage.of(self.db).ci.run(DEPOT, 801)
        self.assertEqual((row["status"], row["run_attempt"]), ("cancelled", 1))
        # relance : GitHub redit le même run `in_progress`, attempt 2 ; le
        # check run de l'attempt 1 est encore le dernier publié (le nouveau job
        # n'a pas encore publié le sien) : sa ligne n'est pas relue
        self.api.live = [run_view(801, sha, "lot/1-un", attempt=2)]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("attempt")) for o in out], [("rerun", 2)])
        row = storage.of(self.db).ci.run(DEPOT, 801)
        self.assertEqual((row["status"], row["run_attempt"]), ("running", 2))
        self.assertEqual(len(self.mails("dev-a")), 1)  # l'ancien échec n'est pas relu
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(len(self.mails("dev-a")), 1)
        # l'attempt 2 échoue à son tour sur le même test : signalé
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=801)],
                                          attempt=2)]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["urgent"]) for o in out], [("failures", True)])
        # l'attempt 2 est suivi : rien de neuf au relevé suivant
        self.assertEqual(self.watcher.tick(self.db), [])
        # fin de l'attempt 2 : verdict, la ligne ne reste pas figée
        self.api.live = []
        self.api.done[801] = completed(".github/workflows/tests.yml", "failure", attempt=2)
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["status"]) for o in out], [("verdict", "failure")])
        row = storage.of(self.db).ci.run(DEPOT, 801)
        self.assertEqual((row["status"], row["run_attempt"]), ("failure", 2))
        verdict = self.mails("dev-a")[-1]
        self.assertIn("attempt 2", verdict["body"])
        self.assertIn("red, 1 failure(s)", verdict["body"])
        payload = verdict["payload"]
        if not isinstance(payload, dict):
            payload = json.loads(payload)
        self.assertEqual(payload["ci"]["attempt"], 2)
        # ameesh ci status voit l'attempt
        report = ci.status_of(self.db, self.lot["id"])
        self.assertEqual(report["runs"][0]["run_attempt"], 2)
        self.assertIn("(attempt 2)", "\n".join(ci.render_status(report)))

    def test_liste_en_retard_ne_rouvre_pas_un_attempt_identique(self):
        """R1 (bureau) : une liste d'exécutions en retard (#828) peut redire
        `in_progress` une exécution conclue, au même attempt, sans qu'elle ait
        été relancée. Ni le relevé ni `reopen_run` ne la rouvrent : sinon son
        verdict repartirait à chaque passe. Un attempt supérieur, lui, rouvre."""
        sha = "99" * 20
        self.api.live = [run_view(851, sha, "lot/1-un", attempt=1)]
        self.watcher.tick(self.db)
        self.api.live, self.api.done[851] = [], {"status": "completed", "conclusion": "failure"}
        self.watcher.tick(self.db)
        row = storage.of(self.db).ci.run(DEPOT, 851)
        self.assertEqual((row["status"], row["run_attempt"]), ("failure", 1))
        self.assertEqual(len(self.mails("dev-a")), 1)  # le verdict, une fois
        # la ligne elle-même refuse un attempt qui n'augmente pas (WHERE)
        self.assertIsNone(storage.of(self.db).ci.reopen_run(DEPOT, 851, run_attempt=1))
        # la liste en retard redit le run en cours au même attempt : rien
        self.api.live = [run_view(851, sha, "lot/1-un", attempt=1)]
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(self.watcher.tick(self.db), [])
        row = storage.of(self.db).ci.run(DEPOT, 851)
        self.assertEqual((row["status"], row["run_attempt"]), ("failure", 1))
        self.assertEqual(len(self.mails("dev-a")), 1)
        # l'attempt suivant est bien une relance : la ligne est rouverte
        self.api.live = [run_view(851, sha, "lot/1-un", attempt=2)]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("attempt")) for o in out], [("rerun", 2)])
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 851)["run_attempt"], 2)

    def test_relance_impute_les_echecs_de_l_attempt_courant(self):
        """Un échec de l'attempt précédent qui n'est pas re-vu n'est plus
        imputé ; un échec qui revient compte comme nouveau (re-signalé)."""
        sha = "a9" * 20
        self.api.live = [run_view(811, sha, "lot/1-un")]
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1", execution=811)])]
        self.watcher.tick(self.db)
        self.api.live, self.api.done[811] = [], {"status": "completed", "conclusion": "cancelled"}
        self.watcher.tick(self.db)
        # relance : test_1 passe, test_2 échoue (le nouveau check, marqué de
        # l'attempt 2, remplace celui de l'attempt 1)
        self.api.live = [run_view(811, sha, "lot/1-un", attempt=2)]
        self.api.checks[sha] = [check_run([ev_test("tests.test_b.T.test_2", execution=811)],
                                          attempt=2)]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("tests"), o.get("urgent")) for o in out],
                         [("rerun", None, None), ("failures", ["tests.test_b.T.test_2"], True)])
        failures = storage.of(self.db).ci.failures(DEPOT, 811)
        self.assertEqual([(f["test"], f["run_attempt"]) for f in failures],
                         [("tests.test_b.T.test_2", 2)])
        # le verdict ne charge que l'attempt courant ; test_1 reste en base
        # (son message d'attempt 1 est toujours retirable)
        self.api.live = []
        self.api.done[811] = completed(".github/workflows/tests.yml", "failure", attempt=2)
        self.watcher.tick(self.db)
        verdict = self.mails("dev-a")[-1]["body"]
        self.assertIn("red, 1 failure(s)", verdict)
        self.assertNotIn("test_1", verdict)
        rows = self.db.query("SELECT test, run_attempt FROM ci_test_failures"
                             " WHERE repo = %s AND run_id = 811 ORDER BY test", (DEPOT,))
        self.assertEqual([(r["test"], r["run_attempt"]) for r in rows],
                         [("tests.test_a.T.test_1", 1), ("tests.test_b.T.test_2", 2)])

    def test_rerun_failed_ne_relit_pas_les_parts_non_rejouees(self):
        """`gh run rerun --failed` ne rejoue que les jobs en échec : les parts
        non rejouées gardent leur check run de l'attempt 1 (un échec toléré y
        figure). Il est enregistré sous son attempt, jamais imputé à la
        relance ni re-signalé."""
        sha = "a8" * 20
        self.api.live = [run_view(812, sha, "lot/1-un")]
        self.api.checks[sha] = [
            check_run([ev_test("tests.test_a.T.test_1", execution=812)]),
            check_run([ev_test("tests.test_b.T.test_2", execution=812)],
                      name="tests (flux) psycopg 2/4")]
        self.watcher.tick(self.db)
        self.assertEqual(len(self.mails("dev-a")), 1)  # les deux échecs, attempt 1
        self.api.live, self.api.done[812] = [], {"status": "completed", "conclusion": "failure"}
        self.watcher.tick(self.db)  # verdict
        self.assertEqual(len(self.mails("dev-a")), 2)
        # relance --failed : la part en échec republie (attempt 2), l'autre
        # garde son check de l'attempt 1
        self.api.live = [run_view(812, sha, "lot/1-un", attempt=2)]
        self.api.checks[sha] = [
            check_run([ev_test("tests.test_a.T.test_1", execution=812)], attempt=2),
            check_run([ev_test("tests.test_b.T.test_2", execution=812)],
                      name="tests (flux) psycopg 2/4")]
        avant = len([m for m in self.mails("dev-a") if "test_2" in m["body"]])
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("tests")) for o in out],
                         [("rerun", None), ("failures", ["tests.test_a.T.test_1"])])
        # l'échec de la part non rejouée n'est pas re-signalé...
        self.assertEqual(len([m for m in self.mails("dev-a") if "test_2" in m["body"]]), avant)
        self.assertNotIn("test_2", self.mails("dev-a")[-1]["body"])
        # ...et l'attempt courant ne charge que la part rejouée
        failures = storage.of(self.db).ci.failures(DEPOT, 812)
        self.assertEqual([(f["test"], f["run_attempt"]) for f in failures],
                         [("tests.test_a.T.test_1", 2)])
        rows = self.db.query("SELECT test, run_attempt FROM ci_test_failures"
                             " WHERE repo = %s AND run_id = 812 ORDER BY test", (DEPOT,))
        self.assertEqual([(r["test"], r["run_attempt"]) for r in rows],
                         [("tests.test_a.T.test_1", 2), ("tests.test_b.T.test_2", 1)])

    def test_relance_d_une_execution_remplacee_ne_la_ressuscite_pas(self):
        """La relance d'un run périmé (une tête plus récente est suivie) ne
        rouvre pas sa ligne."""
        old, new = "b9" * 20, "c9" * 20
        self.api.live = [run_view(821, old, "lot/1-un")]
        self.watcher.tick(self.db)
        self.api.live, self.api.done[821] = [], {"status": "completed", "conclusion": "failure"}
        self.watcher.tick(self.db)
        # une tête plus récente est suivie : 821 reste conclue
        self.api.live = [run_view(822, new, "lot/1-un")]
        self.watcher.tick(self.db)
        self.api.live = [run_view(821, old, "lot/1-un", attempt=2),
                         run_view(822, new, "lot/1-un", attempt=1)]
        out = self.watcher.tick(self.db)
        self.assertEqual([o for o in out if o["action"] == "rerun"], [])
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 821)["status"], "failure")
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 821)["run_attempt"], 1)

    def test_relance_d_une_execution_superseded_ignoree(self):
        """Une ligne remplacée (`superseded`) n'est jamais rouverte."""
        self.api.live = [run_view(831, "d9" * 20, "lot/1-un")]
        self.watcher.tick(self.db)
        self.api.live = [run_view(832, "e9" * 20, "lot/1-un")]
        out = self.watcher.tick(self.db)
        self.assertEqual([o["action"] for o in out if o["action"] == "superseded"],
                         ["superseded"])
        # l'agent relance l'ancien run : la ligne reste remplacée
        self.api.live = [run_view(831, "d9" * 20, "lot/1-un", attempt=2)]
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 831)["status"], "superseded")

    def test_attempt_suivi_sans_conclusion_vue(self):
        """Le relevé a manqué la fin de l'attempt 1 : le nouvel attempt est
        quand même noté et suivi."""
        sha = "f9" * 20
        self.api.live = [run_view(841, sha, "lot/1-un", attempt=1)]
        self.watcher.tick(self.db)
        self.api.live = [run_view(841, sha, "lot/1-un", attempt=2)]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("attempt")) for o in out], [("rerun", 2)])
        row = storage.of(self.db).ci.run(DEPOT, 841)
        self.assertEqual((row["status"], row["run_attempt"]), ("running", 2))

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
        # démarrée d'un essai antérieur ; le relevé l'annule, jamais la tête
        # courante, et l'écriture est idempotente
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: 1_000_000.0)
        vieille, neuve = "f1" * 20, "f2" * 20
        self.api.queued = [run_view(611, vieille, "lot/1-un", status="queued",
                                    created_at="2026-10-14T06:00:00Z")]
        self.assertEqual(watcher.tick(self.db), [])
        self.api.pending = [run_view(612, neuve, "lot/1-un", status="pending",
                                     created_at="2026-10-14T06:05:00Z")]
        self.api.branches["lot/1-un"] = neuve
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o["run_id"]) for o in out],
                         [("superseded", 611), ("cancelled", 611)])
        self.assertIn("cancelled to free", "\n".join(ci.describe(o) for o in out))
        self.assertEqual(self.api.cancels, [(DEPOT, 611)])
        st = storage.of(self.db)
        self.assertEqual(st.ci.run(DEPOT, 611)["status"], "superseded")
        self.assertEqual(st.ci.run(DEPOT, 612)["status"], "running")
        # l'exécution annulée n'est plus vivante : rien à refaire, aucun doublon
        self.assertEqual([o for o in watcher.tick(self.db)
                          if o["action"] == "cancelled"], [])
        self.assertEqual(self.api.cancels, [(DEPOT, 611)])

    def test_annulation_reservee_au_meme_groupe(self):
        # #1161 (R1 B1) : jamais `main`, jamais `release/…`, jamais la tête
        # courante, jamais une poussée simple, une autre PR, un autre dépôt
        # source ou un autre workflow — le groupe d'abord (même PR, même
        # dépôt source, même fichier de workflow)
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: 1_000_000.0)
        vieille, neuve = "1a" * 20, "1b" * 20
        ancienne, recente = "2026-10-14T06:00:00Z", "2026-10-14T06:05:00Z"
        # `main` et `release/…` : jamais touchées
        self.api.queued = [run_view(621, "1c" * 20, "main", status="queued",
                                    created_at=ancienne)]
        self.api.pending = [run_view(622, "1d" * 20, "main", status="pending",
                                     created_at=recente)]
        self.api.branches["main"] = "1d" * 20
        watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        self.api.queued = [run_view(623, "1e" * 20, "release/v1.10.0", status="queued",
                                    created_at=ancienne)]
        self.api.pending = [run_view(624, "1f" * 20, "release/v1.10.0", status="pending",
                                     created_at=recente)]
        self.api.branches["release/v1.10.0"] = "1f" * 20
        watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        # la tête courante : deux essais de la même tête, aucun n'est annulé
        self.api.queued = [run_view(631, neuve, "lot/1-un", status="queued",
                                    created_at=ancienne)]
        self.api.pending = [run_view(632, neuve, "lot/1-un", status="pending",
                                     created_at=recente)]
        self.api.branches["lot/1-un"] = neuve
        watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        # une poussée simple (sans PR) : son groupe n'est pas celui de la PR
        self.api.queued = [run_view(641, vieille, "lot/1-un", status="queued", pr=None,
                                    created_at=ancienne)]
        self.api.pending = [run_view(642, neuve, "lot/1-un", status="pending",
                                     created_at=recente)]
        watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        # même branche, une autre PR : deux groupes
        self.api.queued = [run_view(643, vieille, "lot/1-un", status="queued", pr=2,
                                    created_at=ancienne)]
        self.api.pending = [run_view(644, neuve, "lot/1-un", status="pending", pr=1,
                                     created_at=recente)]
        watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        # même PR, un autre dépôt source : deux groupes
        self.api.queued = [run_view(645, vieille, "lot/1-un", status="queued",
                                    source="fork/ameesh", created_at=ancienne)]
        self.api.pending = [run_view(646, neuve, "lot/1-un", status="pending",
                                     source=DEPOT, created_at=recente)]
        watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])

    def test_annulation_ne_traverse_pas_les_workflows(self):
        # #1161 (R1 B1) : le groupe s'évalue dans le fichier de workflow — un
        # ancien `website-suite.yml` sans successeur de son workflow n'est pas
        # annulé par la tête `tests.yml`, et l'est quand son workflow a son
        # propre successeur sur la tête courante
        cfg = dataclasses.replace(self.cfg, github={
            "projects": {"ameesh": {"repo": DEPOT,
                                    "workflows": ["tests.yml", "website-suite.yml"]}},
            "host": self.cfg.host})
        watcher = ci.Watcher(cfg, api=self.api, cancel=self.api.cancel,
                             clock=lambda: 1_000_000.0)
        vieille, neuve = "1a" * 20, "1b" * 20
        ancienne, recente = "2026-10-14T06:00:00Z", "2026-10-14T06:05:00Z"
        self.api.queued = [run_view(647, vieille, "lot/1-un", status="queued",
                                    workflow="website-suite.yml", created_at=ancienne)]
        self.api.pending = [run_view(648, neuve, "lot/1-un", status="pending",
                                     workflow="tests.yml", created_at=recente)]
        self.api.branches["lot/1-un"] = neuve
        watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        self.api.pending.append(run_view(649, neuve, "lot/1-un", status="pending",
                                         workflow="website-suite.yml",
                                         created_at=recente))
        out = watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [(DEPOT, 647)])
        self.assertIn(("cancelled", 647), [(o["action"], o["run_id"]) for o in out])

    def test_rerun_essai_2_date_et_non_id(self):
        # #1161 (R1 B2) : un rerun garde son `id` et augmente `run_attempt` ;
        # l'ordre des `id` ne prouve pas l'antériorité. L'essai 2 lancé après
        # l'essai 1 de la nouvelle tête est conservé, l'essai antérieur est
        # annulé, et une date d'essai incertaine conserve l'exécution
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: 1_000_000.0)
        ancienne, courante = "2a" * 20, "2b" * 20
        self.api.queued = [run_view(611, ancienne, "lot/1-un", status="queued",
                                    attempt=2, run_started_at="2026-10-14T08:00:00Z")]
        self.api.pending = [run_view(612, courante, "lot/1-un", status="pending",
                                     created_at="2026-10-14T07:00:00Z")]
        self.api.branches["lot/1-un"] = courante
        out = watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        self.assertNotIn("cancelled", [o["action"] for o in out])
        # le même essai, antérieur à la nouvelle tête, est annulé
        self.api.queued = [run_view(613, ancienne, "lot/1-un", status="queued",
                                    created_at="2026-10-14T06:00:00Z")]
        self.api.pending = [run_view(614, courante, "lot/1-un", status="pending",
                                     created_at="2026-10-14T06:05:00Z")]
        out = watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [(DEPOT, 613)])
        self.assertIn(("cancelled", 613), [(o["action"], o["run_id"]) for o in out])
        # une date d'essai incertaine (aucune date) : conservée
        self.api.queued = [run_view(615, ancienne, "lot/1-un", status="queued")]
        self.api.pending = [run_view(616, courante, "lot/1-un", status="pending",
                                     created_at="2026-10-14T06:05:00Z")]
        out = watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [(DEPOT, 613)])
        self.assertNotIn("cancelled", [o["action"] for o in out])
        # un rerun sans date d'essai (seul `created_at` du premier essai) :
        # date incertaine, conservé
        self.api.queued = [run_view(617, ancienne, "lot/1-un", status="queued",
                                    attempt=2, created_at="2026-10-14T05:00:00Z")]
        self.api.pending = [run_view(618, courante, "lot/1-un", status="pending",
                                     created_at="2026-10-14T06:05:00Z")]
        out = watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [(DEPOT, 613)])
        self.assertNotIn("cancelled", [o["action"] for o in out])

    def test_preuve_reverifiee_sur_la_relecture_fraiche(self):
        # #1161 (R1 delta) : la preuve d'antériorité est calculée sur la liste
        # du relevé ; la relecture fraîche de l'exécution doit la confirmer
        # avant le POST. Un rerun apparu entre les deux (essai 2 plus récent
        # que le successeur), une date d'essai devenue plus récente, une autre
        # tête ou un autre groupe conservent l'exécution : le POST ne vise
        # jamais un essai que la liste n'avait pas qualifié.
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: 1_000_000.0)
        vieille, courante = "3a" * 20, "3b" * 20
        self.api.pending = [run_view(612, courante, "lot/1-un", status="pending",
                                     created_at="2026-10-14T07:00:00Z")]
        self.api.branches["lot/1-un"] = courante
        # liste : essai 1 à 06:00, antérieur ; relecture fraîche : essai 2 à
        # 08:00, plus récent que le successeur -> conservée
        self.api.queued = [run_view(611, vieille, "lot/1-un", status="queued",
                                    created_at="2026-10-14T06:00:00Z")]
        self.api.done[611] = run_view(611, vieille, "lot/1-un", status="queued",
                                      attempt=2,
                                      run_started_at="2026-10-14T08:00:00Z")
        out = watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        self.assertNotIn("cancelled", [o["action"] for o in out])
        # essai inchangé mais date d'essai fraîche plus récente que le
        # successeur -> conservée
        self.api.queued = [run_view(613, vieille, "lot/1-un", status="queued",
                                    created_at="2026-10-14T06:00:00Z")]
        self.api.done[613] = run_view(613, vieille, "lot/1-un", status="queued",
                                      run_started_at="2026-10-14T08:00:00Z")
        out = watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        self.assertNotIn("cancelled", [o["action"] for o in out])
        # tête changée entre la liste et la relecture -> conservée
        self.api.queued = [run_view(614, vieille, "lot/1-un", status="queued",
                                    created_at="2026-10-14T06:00:00Z")]
        self.api.done[614] = run_view(614, "3c" * 20, "lot/1-un", status="queued",
                                      created_at="2026-10-14T06:00:00Z")
        out = watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        self.assertNotIn("cancelled", [o["action"] for o in out])
        # groupe changé (dépôt source) entre la liste et la relecture -> conservée
        self.api.queued = [run_view(615, vieille, "lot/1-un", status="queued",
                                    created_at="2026-10-14T06:00:00Z")]
        self.api.done[615] = run_view(615, vieille, "lot/1-un", status="queued",
                                      created_at="2026-10-14T06:00:00Z",
                                      source="quelqu-un/ameesh")
        out = watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [])
        self.assertNotIn("cancelled", [o["action"] for o in out])
        # relecture fraîche conforme à la liste (essai 1, 06:00, même tête,
        # même groupe) -> annulée, comme avant
        self.api.queued = [run_view(616, vieille, "lot/1-un", status="queued",
                                    created_at="2026-10-14T06:00:00Z")]
        self.api.done.pop(616, None)
        out = watcher.tick(self.db)
        self.assertEqual(self.api.cancels, [(DEPOT, 616)])
        self.assertIn(("cancelled", 616), [(o["action"], o["run_id"]) for o in out])

    def test_erreur_d_annulation_sans_faux_verdict(self):
        # #1161 : une erreur de l'API d'annulation est journalisée ; elle ne
        # conclut rien, ne retire rien et laisse la nouvelle exécution suivie
        journal = []
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             log=journal.append, clock=lambda: 1_000_000.0)
        vieille, neuve = "3a" * 20, "3b" * 20
        self.api.queued = [run_view(651, vieille, "lot/1-un", status="queued",
                                    created_at="2026-10-14T06:00:00Z")]
        watcher.tick(self.db)
        self.api.pending = [run_view(652, neuve, "lot/1-un", status="pending",
                                     created_at="2026-10-14T06:05:00Z")]
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
        self.api.queued = [run_view(671, vieille, "lot/1-un", status="queued",
                                    created_at="1970-01-12T13:39:00+00:00")]
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

    def job_failures(self, run_id, repo=DEPOT):
        """#846/#901 : les échecs natifs relevés, avec leur tentative."""
        return [(int(r["run_attempt"]), r["test"]) for r in self.db.query(
            "SELECT run_attempt, test FROM ci_test_failures"
            " WHERE repo = %s AND run_id = %s AND part = %s"
            " ORDER BY test, run_attempt", (repo, int(run_id), ci.JOB_PART))]

    def test_main_rouge_sur_un_job_sans_evenement(self):
        # #846 : un job en échec sans événement de test publié (installation,
        # délai dépassé) est un main rouge ; il est relevé, alerte, et ne
        # produit aucun courrier de test. #901 B1 : la provenance est la
        # tentative (`attempts/<n>/jobs`), donc les check runs de la tête
        # (ceux d'une autre exécution ou d'une autre tentative) sont écartés.
        sha = "a9" * 20
        self.api.live = [run_view(802, sha, "main",
                                  message="L0 (#3)\n\nameesh-work: %d\n" % self.lot_main["id"])]
        self.api.attempt_jobs[(802, 1)] = [
            job_view("psycopg 2/4", status="completed", conclusion="failure"),
            job_view("plan", status="completed", conclusion="success"),
            job_view("psql 1/1", status="completed", conclusion="cancelled")]
        # la tête porte encore le check run natif d'une autre exécution et
        # celui du job en échec : seul le job de la tentative compte
        self.api.checks[sha] = [check_job("psycopg 2/4", 802),
                                check_job("plan", 802, conclusion="success"),
                                check_job("psycopg 3/4", 999),
                                check_job("psql 1/1", 802, conclusion="cancelled")]
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(self.mails("dev-b"), [])
        self.assertEqual(self.job_failures(802), [(1, "psycopg 2/4")])
        (alerte,) = [a for a in exploitation.alerts(self.cfg, self.db)
                     if a["type"] == "ci_main_red"]
        self.assertEqual((alerte["agent"], alerte["lot"]), ("dev-b", self.lot_main["id"]))
        self.assertIn("psycopg 2/4", alerte["detail"])
        self.assertNotIn("psycopg 3/4", alerte["detail"])
        # le relevé suivant ne double ni le relevé ni l'alerte
        self.assertEqual(self.watcher.tick(self.db), [])
        self.assertEqual(self.job_failures(802), [(1, "psycopg 2/4")])
        self.assertEqual(len([a for a in exploitation.alerts(self.cfg, self.db)
                              if a["type"] == "ci_main_red"]), 1)

    def test_relance_ne_reporte_pas_un_echec_natif_de_l_attempt_precedent(self):
        # #901 B1 (R2 dev1/Claude, sonde auditeur) : un job en échec de
        # l'attempt 1, encore publié pendant la relance, garde sa provenance ;
        # le suivi de l'attempt 2 ne l'inscrit pas sous le sien. Le job réussit
        # à la relance : la ligne de l'attempt 1 n'est plus imputée, aucun faux
        # rouge ne subsiste.
        sha = "b9" * 20
        message = "L0 (#3)\n\nameesh-work: %d\n" % self.lot_main["id"]
        self.api.live = [run_view(803, sha, "main", message=message, attempt=1)]
        self.api.attempt_jobs[(803, 1)] = [
            job_view("psycopg 2/4", status="completed", conclusion="failure")]
        self.watcher.tick(self.db)
        self.assertEqual(self.job_failures(803), [(1, "psycopg 2/4")])
        # la relance démarre : le job de l'attempt 2 tourne, et le check run de
        # l'attempt 1 est encore le dernier publié pour ce nom
        self.api.live = [run_view(803, sha, "main", message=message, attempt=2)]
        self.api.checks[sha] = [check_job("psycopg 2/4", 803)]
        self.api.attempt_jobs[(803, 2)] = [job_view("psycopg 2/4", status="in_progress")]
        self.watcher.tick(self.db)
        self.assertEqual(self.job_failures(803), [(1, "psycopg 2/4")])
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "ci_main_red"], [])
        # il réussit à la relance : plus aucun échec imputé à l'exécution
        self.api.live = []
        self.api.done[803] = completed(".github/workflows/tests.yml", "success", attempt=2)
        self.watcher.tick(self.db)
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 803)["status"], "success")
        self.assertEqual(storage.of(self.db).ci.failures(DEPOT, 803), [])
        self.assertEqual(self.job_failures(803), [(1, "psycopg 2/4")])

    def test_echec_natif_de_l_attempt_courant_conserve(self):
        # #901 B1 : un vrai échec natif de l'attempt 2 est relevé sous 2 et
        # garde l'alerte #846 — la correction ne supprime pas le rouge d'une
        # relance.
        sha = "c9" * 20
        message = "L0 (#3)\n\nameesh-work: %d\n" % self.lot_main["id"]
        self.api.live = [run_view(804, sha, "main", message=message, attempt=1)]
        self.api.attempt_jobs[(804, 1)] = [
            job_view("psycopg 2/4", status="completed", conclusion="failure")]
        self.watcher.tick(self.db)
        self.assertEqual(self.job_failures(804), [(1, "psycopg 2/4")])
        # la relance échoue aussi : la ligne passe à l'attempt 2, l'alerte tient
        self.api.live = [run_view(804, sha, "main", message=message, attempt=2)]
        self.api.attempt_jobs[(804, 2)] = [
            job_view("psycopg 2/4", status="completed", conclusion="failure")]
        self.watcher.tick(self.db)
        self.assertEqual(self.job_failures(804), [(2, "psycopg 2/4")])
        (alerte,) = [a for a in exploitation.alerts(self.cfg, self.db)
                     if a["type"] == "ci_main_red"]
        self.assertEqual((alerte["agent"], alerte["lot"]), ("dev-b", self.lot_main["id"]))
        self.assertIn("psycopg 2/4", alerte["detail"])

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
        self.api.live = [run_view(960, sha, "lot/1-un", workflow="backend-suite.yml", attempt=2)]
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
                   updated_at=None, attempt=1):
        """Une exécution terminée telle que la liste `status=completed` la rend."""
        return dict(run_view(run_id, sha, branch, workflow=workflow,
                             updated_at=updated_at or self.AT_NOW, attempt=attempt),
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

    def test_vert_de_main_hors_fenetre_ferme_l_alerte(self):
        """#2935 : le vert de main conclu pendant une interruption du veilleur
        (hors de la fenêtre de rattrapage) est quand même enregistré : l'alerte
        `ci_main_red` se ferme au premier vert de la même base, sans courrier
        ni verdict ancien."""
        st = storage.of(self.db)
        st.ci.open_run(DEPOT, 4001, head_sha="8" * 20, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.add_failure(DEPOT, 4001, run_attempt=1, part="psql 1/1",
                          test="tests.test_a.T.test_1", module="tests.test_a",
                          status="echec", inherited=False, excerpt="", replay="",
                          duration_s=None, failed_at=None)
        st.ci.conclude(DEPOT, 4001, "failure")
        self.assertEqual([r["run_id"] for r in ci.main_red(self.db)], [4001])
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
        self.api.terminated = [self.terminated(
            4002, "9" * 20, "main", conclusion="success",
            updated_at="1970-01-12T11:46:40+00:00")]  # 2 h : hors fenêtre
        self.assertEqual(watcher.tick(self.db), [])
        row = st.ci.run(DEPOT, 4002)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "success")
        self.assertEqual(ci.main_red(self.db), [])
        self.assertFalse([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "ci_main_red"])
        self.assertEqual(self.mails("dev-a") + self.mails("dev-b"), [])

    def test_rouge_de_main_hors_fenetre_est_releve_pour_l_alerte(self):
        """#2935 : une base rouge conclue pendant l'interruption est relevée
        pour `ci_main_red` (son état et ses échecs), sans réveiller de lot."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
        sha = "a7" * 20
        self.api.terminated = [self.terminated(
            4003, sha, "main", conclusion="failure",
            updated_at="1970-01-12T11:46:40+00:00")]
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_2",
                                                   execution=4003)])]
        self.assertEqual(watcher.tick(self.db), [])
        row = storage.of(self.db).ci.run(DEPOT, 4003)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "failure")
        self.assertEqual([r["run_id"] for r in ci.main_red(self.db)], [4003])
        self.assertIn("tests.test_a.T.test_2",
                      " ".join(ci.main_red(self.db)[0]["tests"]))
        self.assertEqual(self.mails("dev-a") + self.mails("dev-b"), [])

    def test_relance_de_main_terminee_hors_fenetre_reprend_l_attempt(self):
        """#2935 (relecture cx3 B1) : une relance terminée pendant l'arrêt du
        veilleur (ligne déjà connue, `run_attempt` plus récent) remplace le
        résultat de l'attempt suivi — un rouge d'attempt 1 ne reste pas quand
        l'attempt 2 est vert."""
        st = storage.of(self.db)
        sha = "8" * 20
        st.ci.open_run(DEPOT, 4010, head_sha=sha, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.add_failure(DEPOT, 4010, run_attempt=1, part="psql 1/1",
                          test="tests.test_a.T.test_1", module="tests.test_a",
                          status="echec", inherited=False, excerpt="", replay="",
                          duration_s=None, failed_at=None)
        st.ci.conclude(DEPOT, 4010, "failure")
        self.assertEqual([r["run_id"] for r in ci.main_red(self.db)], [4010])
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
        self.api.terminated = [self.terminated(
            4010, sha, "main", conclusion="success", attempt=2,
            updated_at="1970-01-12T11:46:40+00:00")]  # 2 h : hors fenêtre
        self.assertEqual(watcher.tick(self.db), [])
        row = st.ci.run(DEPOT, 4010)
        self.assertEqual((row["status"], row["run_attempt"]), ("success", 2))
        self.assertEqual(ci.main_red(self.db), [])
        self.assertEqual(self.mails("dev-a") + self.mails("dev-b"), [])

    def test_vert_de_main_derriere_une_annulation_hors_fenetre(self):
        """#2935 (relecture cx3 B2) : le dernier résultat UTILE d'une base est
        le dernier success/failure, pas la dernière exécution ; un vert R2
        suivi d'une annulation R3 pendant l'arrêt ferme quand même le rouge
        R1 (`latest_concluded` ignore les annulations)."""
        st = storage.of(self.db)
        st.ci.open_run(DEPOT, 4020, head_sha="8" * 20, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.add_failure(DEPOT, 4020, run_attempt=1, part="psql 1/1",
                          test="tests.test_a.T.test_1", module="tests.test_a",
                          status="echec", inherited=False, excerpt="", replay="",
                          duration_s=None, failed_at=None)
        st.ci.conclude(DEPOT, 4020, "failure")
        self.assertEqual([r["run_id"] for r in ci.main_red(self.db)], [4020])
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
        self.api.terminated = [
            self.terminated(4023, "a3" * 20, "main", conclusion="cancelled",
                            updated_at="1970-01-12T11:46:40+00:00"),
            self.terminated(4022, "a2" * 20, "main", conclusion="success",
                            updated_at="1970-01-12T11:46:40+00:00"),
        ]  # 2 h : hors fenêtre
        self.assertEqual(watcher.tick(self.db), [])
        row = st.ci.run(DEPOT, 4022)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "success")
        self.assertEqual(ci.main_red(self.db), [])
        self.assertEqual(self.mails("dev-a") + self.mails("dev-b"), [])

    # -- #2938 : pagination de la tête de base et borne de fenêtre -----------
    OLD = "1970-01-12T11:46:40+00:00"  # 2 h avant NOW : hors fenêtre de rattrapage

    def _base_runs(self, count, *, start, conclusion, updated_at):
        return [self.terminated(start - i, "%040x" % (start - i), "main",
                                conclusion=conclusion, updated_at=updated_at)
                for i in range(count)]

    def test_tete_de_base_pagine_derriere_cent_annulations(self):
        """#2938 (N1/N2 cx3) : plus de cent annulations plus récentes ne
        cachent plus le dernier résultat utile ; la page suivante est lue, le
        vert est enregistré sans courrier et ferme `ci_main_red`."""
        st = storage.of(self.db)
        st.ci.open_run(DEPOT, 4001, head_sha="8" * 20, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.add_failure(DEPOT, 4001, run_attempt=1, part="psql 1/1",
                          test="tests.test_a.T.test_1", module="tests.test_a",
                          status="echec", inherited=False, excerpt="", replay="",
                          duration_s=None, failed_at=None)
        st.ci.conclude(DEPOT, 4001, "failure")
        self.assertEqual([r["run_id"] for r in ci.main_red(self.db)], [4001])
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
        self.api.terminated = self._base_runs(
            100, start=4200, conclusion="cancelled", updated_at=self.OLD) + [
            self.terminated(4100, "f" * 20, "main", conclusion="success",
                            updated_at=self.OLD)]
        watcher._refresh_bases(self.db, DEPOT, {"tests.yml"}, ("main",))
        row = st.ci.run(DEPOT, 4100)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "success")
        self.assertEqual(ci.main_red(self.db), [])
        self.assertEqual(self.mails("dev-a") + self.mails("dev-b"), [])
        self.assertTrue(any("&page=2" in p for p in self.api.paths))

    def test_tete_de_base_ne_relit_pas_une_execution_recente_sans_courrier(self):
        """#2938 (N auditeur) : une exécution récente que le rattrapage n'a pas
        vue n'est pas enregistrée en silence par le relevé de tête, même
        derrière plus de cent annulations récentes : elle reste à `_catch_up`,
        qui courrielle."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
        self.api.terminated = self._base_runs(
            100, start=4200, conclusion="cancelled", updated_at=self.AT_NOW) + [
            self.terminated(4100, "f" * 20, "main", conclusion="failure",
                            updated_at=self.AT_NOW)]
        watcher._refresh_bases(self.db, DEPOT, {"tests.yml"}, ("main",))
        self.assertIsNone(storage.of(self.db).ci.run(DEPOT, 4100))
        self.assertEqual(ci.main_red(self.db), [])
        self.assertEqual(self.mails("dev-a") + self.mails("dev-b"), [])
        self.assertTrue(any("&page=2" in p for p in self.api.paths))

    def test_tete_de_base_laisse_une_execution_recente_au_rattrapage(self):
        """#2938 (N auditeur) : la borne de fenêtre vaut aussi page 1 — le
        relevé de tête n'enregistre pas une exécution récente, son état et son
        courrier appartiennent au rattrapage."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
        self.api.terminated = [self.terminated(4110, "e" * 20, "main",
                                               conclusion="failure",
                                               updated_at=self.AT_NOW)]
        watcher._refresh_bases(self.db, DEPOT, {"tests.yml"}, ("main",))
        self.assertIsNone(storage.of(self.db).ci.run(DEPOT, 4110))
        self.assertEqual(self.mails("dev-a") + self.mails("dev-b"), [])

    def test_tete_de_base_s_arrete_des_que_chaque_workflow_a_son_resultat(self):
        """#2938 : la page 1 suffit quand elle porte un résultat utile de
        chaque workflow suivi ; aucune page 2 n'est demandée (coût borné)."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW)
        self.api.terminated = [self.terminated(4200, "d" * 20, "main",
                                               conclusion="success",
                                               updated_at=self.OLD)] + self._base_runs(
            99, start=4199, conclusion="cancelled", updated_at=self.OLD)
        watcher._refresh_bases(self.db, DEPOT, {"tests.yml"}, ("main",))
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 4200)["status"], "success")
        self.assertEqual(len([p for p in self.api.paths if "status=completed" in p
                              and "&page=" in p]), 1)

    def test_tete_de_base_borne_la_pagination_et_le_dit_une_fois(self):
        """#2938 : une branche sans résultat utile ne fait pas paginer sans
        fin ; le plafond est journalisé une seule fois."""
        journal = []
        watcher = ci.Watcher(self.cfg_ci, api=self.api, cancel=self.api.cancel,
                             clock=lambda: self.NOW, log=journal.append)
        self.api.terminated = self._base_runs(
            600, start=5000, conclusion="cancelled", updated_at=self.OLD)
        watcher._refresh_bases(self.db, DEPOT, {"tests.yml"}, ("main",))
        watcher._refresh_bases(self.db, DEPOT, {"tests.yml"}, ("main",))
        pages = [p for p in self.api.paths if "status=completed" in p and "&page=" in p]
        self.assertEqual(len(pages), 10)  # 5 pages, deux passages
        self.assertEqual(len([l for l in journal if "truncated at 5 pages" in l]), 1)
        self.assertIsNone(storage.of(self.db).ci.run(DEPOT, 5000))

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
                st.ci.add_failure(DEPOT, run_id, run_attempt=1, part="psql 1/1",
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

    def test_dispatch_d_une_suite_qui_ne_suit_pas_la_base(self):
        """#3686 : un `workflow_dispatch` refusé sur une base où la suite ne
        tourne jamais autrement (manaty/nexlink main, 26/10 05:46Z) ne lève pas
        `ci_main_red`, conclu ou en cours ; un dispatch rouge d'une suite qui
        tourne aussi en push sur la base alerte comme avant."""
        st = storage.of(self.db)
        sha = "f6" * 20
        # une ancienne PR dont la tête est main (synchronisation main →
        # develop) ne fait pas suivre la base à la suite
        st.ci.open_run(DEPOT, 2990, head_sha="f5" * 20, branch="main", event="pull_request",
                       base="main", work_item_id=None, url=None, workflow="backend-suite.yml")
        st.ci.conclude(DEPOT, 2990, "success")
        # backend-suite.yml : dispatch seul sur main, refusé par sa garde
        st.ci.open_run(DEPOT, 3001, head_sha=sha, branch="main", event="workflow_dispatch",
                       base="main", work_item_id=None, url=None, workflow="backend-suite.yml")
        st.ci.add_failure(DEPOT, 3001, run_attempt=1, part="job", test="backend",
                          module="", status="echec", inherited=False, excerpt="",
                          replay="", duration_s=None, failed_at=None)
        self.assertEqual(ci.main_red(self.db), [])  # en cours : rien
        st.ci.conclude(DEPOT, 3001, "failure")
        self.assertEqual(ci.main_red(self.db), [])
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "ci_main_red"], [])
        # tests.yml tourne en push sur main (setUp, run 100) : son dispatch rouge compte
        st.ci.open_run(DEPOT, 3002, head_sha=sha, branch="main", event="workflow_dispatch",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.conclude(DEPOT, 3002, "failure")
        self.assertEqual([(r["run_id"], r["workflow"]) for r in ci.main_red(self.db)],
                         [(3002, "tests.yml")])

    def test_main_rouge_execution_orpheline_ecartee(self):
        """#1453 N1 : une exécution de main jamais conclue (supprimée, l'API
        rend `{}`) qui porte un job rouge ne garde pas l'alerte quand une
        exécution plus récente du même workflow est conclue ; une exécution
        d'un autre workflow, ou plus récente que la conclue, alerte encore."""
        st = storage.of(self.db)
        for run_id, sha, workflow in ((1001, "e1" * 20, "tests.yml"),
                                      (1002, "e2" * 20, "tests.yml"),
                                      (1003, "e2" * 20, "native.yml"),
                                      (1005, "e3" * 20, "tests.yml")):
            st.ci.open_run(DEPOT, run_id, head_sha=sha, branch="main", event="push",
                           base="main", work_item_id=None, url=None, workflow=workflow)
        for run_id in (1001, 1003, 1005):
            st.ci.add_failure(DEPOT, run_id, run_attempt=1, part="job", test="psql 1/1",
                              module="", status="echec", inherited=False, excerpt="",
                              replay="", duration_s=None, failed_at=None)
        self.assertEqual(sorted(r["run_id"] for r in ci.main_red(self.db)),
                         [1001, 1003, 1005])
        st.ci.conclude(DEPOT, 1002, "success")
        # 1001 reste « running » à jamais : écartée par 1002, conclue verte ;
        # 1003 (autre workflow) et 1005 (plus récente) alertent toujours
        self.assertEqual(st.ci.run(DEPOT, 1001)["status"], "running")
        self.assertEqual(sorted(r["run_id"] for r in ci.main_red(self.db)), [1003, 1005])

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
                st.ci.add_failure(DEPOT, run_id, run_attempt=1, part="psql 1/1",
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

    def test_reference_du_rouge_herite_suit_la_tete_la_plus_recente(self):
        """#1683 N1 : la référence du rouge hérité est la tête la plus
        récemment poussée, pas l'exécution conclue le plus récemment — une
        exécution lente d'une tête plus ancienne ne redevient pas la base, et
        un réessai tardif de cette tête ne la rajeunit pas."""
        st = storage.of(self.db)
        repo = "manaty/hors-fixture"  # le setUp ne pose des exécutions que sur DEPOT
        ancienne, recente = "0a" * 20, "0b" * 20
        # M1 : tête ancienne, observée d'abord, conclue en dernier
        st.ci.open_run(repo, 2001, head_sha=ancienne, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.add_failure(repo, 2001, run_attempt=1, part="psql 1/1", test="tests.test_a.T.test_1",
                          module="tests.test_a", status="echec", inherited=False,
                          excerpt="", replay="", duration_s=None, failed_at=None)
        st.ci.conclude(repo, 2001, "failure")
        # M2 : tête plus récente, observée après M1, conclue avant
        st.ci.open_run(repo, 2002, head_sha=recente, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.conclude(repo, 2002, "success")
        self.db.execute(
            "UPDATE ci_runs SET first_seen_at = now() - interval '10 min',"
            " created_at = now() - interval '10 min', concluded_at = now()"
            " WHERE repo = %s AND run_id = 2001", (repo,))
        self.db.execute(
            "UPDATE ci_runs SET first_seen_at = now() - interval '5 min',"
            " created_at = now() - interval '5 min', concluded_at = now() - interval '1 min'"
            " WHERE repo = %s AND run_id = 2002", (repo,))
        reference = st.ci.base_failures(repo, "main", workflow="tests.yml")
        self.assertEqual((reference["run_id"], reference["head_sha"]), (2002, recente))
        self.assertEqual(reference["tests"], [])
        # un réessai de la tête ancienne, observé plus tard, ne la rajeunit pas :
        # la première observation de cette tête reste la référence d'ordre
        st.ci.open_run(repo, 2003, head_sha=ancienne, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.conclude(repo, 2003, "failure")
        self.assertEqual(st.ci.base_failures(repo, "main", workflow="tests.yml")["run_id"],
                         2002)

    def test_seconde_egale_garde_la_tete_poussee_en_dernier(self):
        """#2064 : `created_at` est à la seconde ; à observation égale, une
        tête plus ancienne conclue plus tard ne redevient pas la référence —
        c'est la tête poussée en dernier (run_id le plus grand) qui l'est."""
        st = storage.of(self.db)
        repo = "manaty/seconde-egale"
        ancienne, recente = "0f" * 20, "10" * 20
        for run_id, tete in ((2201, ancienne), (2202, recente)):
            st.ci.open_run(repo, run_id, head_sha=tete, branch="main", event="push",
                           base="main", work_item_id=None, url=None,
                           workflow="tests.yml",
                           created_at="2026-01-01T10:00:00Z")
        st.ci.add_failure(repo, 2201, run_attempt=1, part="psql 1/1",
                          test="tests.test_a.T.test_1", module="tests.test_a",
                          status="echec", inherited=False, excerpt="", replay="",
                          duration_s=None, failed_at=None)
        st.ci.conclude(repo, 2201, "failure")
        st.ci.conclude(repo, 2202, "success")
        # la tête ancienne est conclue plus tard, dans la même seconde
        self.db.execute(
            "UPDATE ci_runs SET concluded_at = CASE run_id"
            " WHEN 2201 THEN '2026-01-01T11:00:00Z'::timestamptz"
            " WHEN 2202 THEN '2026-01-01T10:30:00Z'::timestamptz END"
            " WHERE repo = %s AND run_id IN (2201, 2202)", (repo,))
        reference = st.ci.base_failures(repo, "main", workflow="tests.yml")
        self.assertEqual((reference["run_id"], reference["head_sha"]), (2202, recente))
        self.assertEqual(reference["tests"], [])
        # Une nouvelle exécution sur l'ancienne tête ne la rajeunit pas,
        # même quand les premières observations des deux têtes sont égales.
        st.ci.open_run(repo, 2203, head_sha=ancienne, branch="main", event="push",
                       base="main", work_item_id=None, url=None,
                       workflow="tests.yml", created_at="2026-01-01T10:01:00Z")
        st.ci.conclude(repo, 2203, "failure")
        reference = st.ci.base_failures(repo, "main", workflow="tests.yml")
        self.assertEqual((reference["run_id"], reference["head_sha"]), (2202, recente))
        self.assertEqual(reference["tests"], [])

    def test_reference_garde_le_dernier_resultat_de_la_tete_recente(self):
        """Une reprise actualise le résultat, jamais l'âge de sa tête."""
        st = storage.of(self.db)
        ancienne, recente = "0c" * 20, "0d" * 20
        for premier, dernier in (("failure", "success"), ("success", "failure")):
            repo = "manaty/reprise-" + premier
            runs = ((2001, ancienne, "cancelled"), (2002, recente, premier),
                    (2003, ancienne, "failure"), (2004, recente, dernier))
            for minute, (run_id, head, status) in enumerate(runs):
                st.ci.open_run(repo, run_id, head_sha=head, branch="main", event="push",
                               base="main", work_item_id=None, url=None, workflow="tests.yml",
                               created_at="2026-01-01T10:%02d:00Z" % minute)
                if status == "failure":
                    st.ci.add_failure(repo, run_id, run_attempt=1, part="psql 1/1",
                                      test="tests.test_a.T.test_1",
                                      module="tests.test_a", status="echec", inherited=False,
                                      excerpt="", replay="", duration_s=None, failed_at=None)
                st.ci.conclude(repo, run_id, status)
                self.db.execute("UPDATE ci_runs SET concluded_at = %s::timestamptz"
                                " WHERE repo = %s AND run_id = %s",
                                ("2026-01-01T11:%02d:00Z" % minute, repo, run_id))
            reference = st.ci.base_failures(repo, "main", workflow="tests.yml")
            self.assertEqual((reference["run_id"], reference["head_sha"]), (2004, recente))
            self.assertEqual(reference["tests"], ["tests.test_a.T.test_1"]
                             if dernier == "failure" else [])

    def test_raccord_1683_901_reference_recente_et_attempt_courant(self):
        """Raccord #1683 x #901 : la référence reste la tête la plus récemment
        poussée, et son rouge est celui de l'attempt courant — une relance
        rouverte ne ressuscite pas l'échec de l'attempt précédent, et un échec
        non re-vu à l'attempt 2 n'est plus imputé."""
        st = storage.of(self.db)
        repo = "manaty/raccord-attempt"
        tete = "0e" * 20
        st.ci.open_run(repo, 2101, head_sha=tete, branch="main", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        for test in ("tests.test_a.T.test_1", "tests.test_a.T.test_2"):
            st.ci.add_failure(repo, 2101, run_attempt=1, part="psql 1/1", test=test,
                              module="tests.test_a", status="echec", inherited=False,
                              excerpt="", replay="", duration_s=None, failed_at=None)
        st.ci.conclude(repo, 2101, "failure")
        self.assertEqual(st.ci.base_failures(repo, "main", workflow="tests.yml")["tests"],
                         ["tests.test_a.T.test_1", "tests.test_a.T.test_2"])
        # relance GitHub : la tête repasse en cours, sans conclusion — plus de
        # référence, et pas de retour à une tête plus ancienne
        self.assertTrue(st.ci.reopen_run(repo, 2101, run_attempt=2))
        self.assertEqual(st.ci.base_failures(repo, "main", workflow="tests.yml"), {})
        # l'attempt 2 ne re-voit qu'un test : l'autre échec reste à l'attempt 1
        # et n'est plus lu
        st.ci.add_failure(repo, 2101, run_attempt=2, part="psql 1/1",
                          test="tests.test_a.T.test_1", module="tests.test_a",
                          status="echec", inherited=False, excerpt="", replay="",
                          duration_s=None, failed_at=None)
        st.ci.conclude(repo, 2101, "failure")
        reference = st.ci.base_failures(repo, "main", workflow="tests.yml")
        self.assertEqual((reference["run_id"], reference["head_sha"]), (2101, tete))
        self.assertEqual(reference["tests"], ["tests.test_a.T.test_1"])

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

    def test_relance_courte_finie_entre_deux_passages_est_rouverte(self):
        """#1573 N1 : `gh run rerun` garde le run_id ; si l'attempt 2 naît et
        finit entre deux passages, le rattrapage ne voit qu'une ligne connue.
        Elle est rouverte (relance) puis conclue — sans quoi le verdict de la
        relance ne partirait jamais."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        sha = "71" * 20
        # passage 1 : attempt 1 suivi, puis conclu (échec)
        self.api.live = [run_view(1030, sha, "lot/1-un", attempt=1)]
        watcher.tick(self.db)
        self.api.live, self.api.done[1030] = [], {"status": "completed",
                                                  "conclusion": "failure"}
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("status")) for o in out],
                         [("verdict", "failure")])
        self.assertEqual(len(self.mails("dev-a")), 1)
        # passage 2 : la relance est née et finie entre les deux passages : elle
        # n'apparaît que dans la liste des exécutions terminées (attempt 2)
        self.api.terminated = [self.terminated(1030, sha, "lot/1-un",
                                               conclusion="success", attempt=2)]
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("attempt"), o.get("status")) for o in out],
                         [("rerun", 2, None), ("verdict", None, "success")])
        row = storage.of(self.db).ci.run(DEPOT, 1030)
        self.assertEqual((row["status"], row["run_attempt"]), ("success", 2))
        # un seul verdict par tentative : le passage suivant ne double rien
        self.assertEqual(watcher.tick(self.db), [])
        self.assertEqual(len(self.mails("dev-a")), 2)

    def test_verdict_d_un_autre_attempt_ne_conclut_pas_la_ligne(self):
        """#1612 : le dernier état GitHub ne conclut jamais un autre attempt,
        au rattrapage comme au relevé normal d'une ligne encore running ;
        l'attempt suivi est conclu par son propre état (`attempts/<n>`)."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        st = storage.of(self.db)
        for index, (catchup, tracked) in enumerate(((False, 1), (True, 1),
                                                   (False, 3), (True, 3))):
            with self.subTest(catchup=catchup, tracked=tracked):
                run_id = 1040 + index * 2
                sha = "%040x" % run_id
                # Sur main les anciennes têtes restent running. La tête plus
                # récente empêche reopen_run de rouvrir celle-ci à l'attempt 2.
                st.ci.open_run(DEPOT, run_id, head_sha=sha, branch="main",
                               event="push", base="main", work_item_id=self.lot_main["id"],
                               url=None, workflow="tests.yml", run_attempt=tracked)
                st.ci.open_run(DEPOT, run_id + 1, head_sha="%040x" % (run_id + 1),
                               branch="main", event="push", base="main",
                               work_item_id=None, url=None, workflow="tests.yml")
                self.api.done[run_id] = completed(".github/workflows/tests.yml", "success",
                                                  attempt=2)
                self.api.terminated = [self.terminated(run_id, sha, "main",
                                                       conclusion="success", attempt=2)] \
                    if catchup else []
                self.assertEqual(watcher.tick(self.db), [])
                row = st.ci.run(DEPOT, run_id)
                self.assertEqual((row["status"], row["run_attempt"]), ("running", tracked))
                self.assertEqual(len(self.mails("dev-b")), index)
                # l'attempt suivi se conclut par son propre état, pas par le
                # dernier : la ligne ne reste pas `running` sans fin
                self.api.attempts[(run_id, tracked)] = completed(
                    ".github/workflows/tests.yml", "failure", attempt=tracked)
                out = watcher.tick(self.db)
                self.assertEqual([(o["action"], o["run_id"], o["status"]) for o in out],
                                 [("verdict", run_id, "failure")])
                row = st.ci.run(DEPOT, run_id)
                self.assertEqual((row["status"], row["run_attempt"]), ("failure", tracked))
                self.assertEqual(len(self.mails("dev-b")), index + 1)
                self.assertIn("red", self.mails("dev-b")[-1]["body"])
                self.assertEqual(watcher.tick(self.db), [])

    def test_echec_de_l_attempt_suivant_inscrit_avant_la_reouverture(self):
        """#1573 N2 : une liste en retard (#828) peut publier un échec de
        l'attempt 2 pendant que la ligne est encore à l'attempt 1. La ligne
        l'enregistre sous son attempt ; à la réouverture, `add_failure` ne le
        réinsère pas (même attempt) — il doit pourtant être signalé."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        sha = "72" * 20
        # passage 1 : attempt 1 suivi
        self.api.live = [run_view(1031, sha, "lot/1-un", attempt=1)]
        watcher.tick(self.db)
        # passage 2 : la liste dit encore attempt 1, mais le check run de la
        # relance (attempt 2) est déjà publié et échoue
        self.api.checks[sha] = [check_run([ev_test("tests.test_b.T.test_2", execution=1031,
                                                   attempt=2)], attempt=2)]
        self.assertEqual(watcher.tick(self.db), [])
        self.assertEqual(self.mails("dev-a"), [])
        rows = self.db.query("SELECT test, run_attempt, message_id FROM ci_test_failures"
                             " WHERE repo = %s AND run_id = %s", (DEPOT, 1031))
        self.assertEqual([(r["test"], r["run_attempt"], r["message_id"]) for r in rows],
                         [("tests.test_b.T.test_2", 2, None)])
        # passage 3 : la liste rattrape l'attempt 2 ; la ligne est rouverte et
        # l'échec déjà inscrit est signalé, une seule fois
        self.api.live = [run_view(1031, sha, "lot/1-un", attempt=2)]
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("attempt"), o.get("tests"), o.get("urgent"))
                          for o in out],
                         [("rerun", 2, None, None),
                          ("failures", None, ["tests.test_b.T.test_2"], True)])
        mails = self.mails("dev-a")
        self.assertEqual(len(mails), 1)
        self.assertIn("tests.test_b.T.test_2", mails[0]["body"])
        self.assertEqual(watcher.tick(self.db), [])
        self.assertEqual(len(self.mails("dev-a")), 1)

    def test_echec_deja_inscrit_non_resignale_si_inherited_change(self):
        """#1786 N1 : la référence de base peut changer entre deux passes ; une
        ligne de l'attempt courant déjà inscrite sans message ne change pas de
        classe (`add_failure` ne met pas à jour le même attempt). Elle n'est
        donc pas resignalée comme échec propre : le message et la base
        resteraient divergents."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        sha = "74" * 20
        st = storage.of(self.db)
        st.ci.open_run(DEPOT, 1033, head_sha=sha, branch="lot/1-un",
                       event="pull_request", base="main",
                       work_item_id=self.lot["id"], url=None, workflow="tests.yml")
        # la ligne a d'abord été vue héritée (main rouge sur ce test), sans
        # message ; la référence de main est maintenant verte.
        st.ci.add_failure(DEPOT, 1033, run_attempt=1, part="psycopg 1/4",
                          test="tests.test_a.T.test_1", module="tests.test_a",
                          status="echec", inherited=True, excerpt="", replay="",
                          duration_s=None, failed_at=None)
        self.api.live = [run_view(1033, sha, "lot/1-un")]
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1",
                                                   execution=1033)])]
        self.assertEqual(watcher.tick(self.db), [])
        self.assertEqual(self.mails("dev-a"), [])
        failures = st.ci.failures(DEPOT, 1033)
        self.assertEqual([(f["test"], f["inherited"], f["message_id"])
                          for f in failures],
                         [("tests.test_a.T.test_1", True, None)])
        # le passage suivant ne double rien : la ligne reste sans message.
        self.assertEqual(watcher.tick(self.db), [])
        self.assertEqual(self.mails("dev-a"), [])

    def test_echec_sans_destinataire_journalise_une_seule_fois(self):
        """#1573 R2 : une exécution sans lot (main) n'envoie aucun message ;
        ses échecs déjà inscrits ne sont pas rejournalisés à chaque passe."""
        watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=lambda: self.NOW)
        sha = "73" * 20
        self.api.live = [run_view(1032, sha, "main")]
        self.api.checks[sha] = [check_run([ev_test("tests.test_c.T.test_3", execution=1032)])]
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o.get("tests"), o["to"]) for o in out],
                         [("failures", ["tests.test_c.T.test_3"], None)])
        self.assertEqual(watcher.tick(self.db), [])

    def test_reference_stricte_par_workflow(self):
        """#1110 P2-2 : une ligne sans workflow (écrite avant 0433 par un
        ancien processus) ne sert pas de référence à une autre suite : l'échec
        n'est pas classé hérité au nom d'une suite étrangère."""
        st = storage.of(self.db)
        sha_main = "0" * 40  # la ligne verte `tests.yml` du setUp
        st.ci.open_run(DEPOT, 993, head_sha=sha_main, branch="main", event="push",
                       base="main", work_item_id=None, url=None)  # sans workflow
        st.ci.add_failure(DEPOT, 993, run_attempt=1, part="psql 1/1", test="tests.test_a.T.test_1",
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
        st.ci.add_failure(repo, 1202, run_attempt=1, part="psql 1/1", test="tests.test_a.T.test_1",
                          module="tests.test_a", status="echec", inherited=False,
                          excerpt="", replay="", duration_s=None, failed_at=None)
        st.ci.conclude(repo, 1202, "failure")
        # #1683 N3 : une branche `develop` d'un dépôt qui ne la déclare pas
        # n'est pas une base — la lecture sans `bases_for` (appel historique)
        # la voit pourtant : c'est l'écart que l'alerte ne doit plus avoir
        st.ci.open_run(DEPOT, 1206, head_sha="d1" * 20, branch="develop", event="push",
                       base="main", work_item_id=None, url=None, workflow="tests.yml")
        st.ci.add_failure(DEPOT, 1206, run_attempt=1, part="psql 1/1", test="tests.test_b.T.test_1",
                          module="tests.test_b", status="echec", inherited=False,
                          excerpt="", replay="", duration_s=None, failed_at=None)
        st.ci.conclude(DEPOT, 1206, "failure")
        self.assertEqual(sorted((r["repo"], r["branch"], r["run_id"]) for r in
                                ci.main_red(self.db, ("main", "develop"))),
                         [(DEPOT, "develop", 1206), (repo, "develop", 1202)])
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
        # #1161 : les états vivants lus sont plus nombreux (`ci.LIVE_STATUSES`) ;
        # chaque liste tronquée est journalisée une fois, dans cet ordre.
        attendu = []
        for status in ci.LIVE_STATUSES:
            compte = "1 of 2" if status == "in_progress" else "0 of 1"
            attendu.append("CI %s: %s run list truncated (%s), a followed run may "
                           "be seen late" % (DEPOT, status, compte))
        self.assertEqual([line for line in journal if "run list truncated" in line], attendu)

    def test_decouverte_ne_suit_que_les_suites(self):
        """#1483 N3 : un workflow découvert qui n'a pas la forme d'une suite
        (déploiement, sonde) n'est pas suivi — pas de verdict de bruit ; il est
        journalisé, et une liste déclarée le suit si on le veut. #1683 N2 : un
        `ci.yml` générique est écarté comme les autres dès qu'une suite est
        reconnue — comportement assumé, journalisé, et déclarable."""
        repo = "manaty/nexlink"
        cfg = dataclasses.replace(self.cfg, github={"projects": {"nexlink": repo},
                                                    "host": self.cfg.host})
        api = FauxApi()
        api.workflows = ["backend-suite.yml", "sql-invariant.yml", "deploy-api-prod.yml",
                         "compute-probe.yml", "ci.yml"]
        journal = []
        watcher = ci.Watcher(cfg, api=api, log=journal.append)
        api.live = [run_view(1400, "d1" * 20, "lot/1-un", workflow="backend-suite.yml"),
                    run_view(1401, "d2" * 20, "lot/1-un", workflow="sql-invariant.yml"),
                    run_view(1402, "d3" * 20, "lot/1-un", workflow="deploy-api-prod.yml"),
                    run_view(1403, "d4" * 20, "lot/1-un", workflow="compute-probe.yml"),
                    run_view(1405, "d6" * 20, "lot/1-un", workflow="ci.yml")]
        watcher.tick(self.db)
        st = storage.of(self.db)
        self.assertEqual([st.ci.run(repo, 1400)["workflow"],
                          st.ci.run(repo, 1401)["workflow"]],
                         ["backend-suite.yml", "sql-invariant.yml"])
        self.assertIsNone(st.ci.run(repo, 1402))
        self.assertIsNone(st.ci.run(repo, 1403))
        self.assertIsNone(st.ci.run(repo, 1405))
        self.assertEqual([line for line in journal if "not followed" in line],
                         ["CI %s: discovered workflow(s) not followed (not a test suite): "
                          "ci.yml, compute-probe.yml, deploy-api-prod.yml; declare them in "
                          "github.projects to follow them" % repo])
        # une liste déclarée suit explicitement un workflow hors convention
        cfg = dataclasses.replace(self.cfg, github={
            "projects": {"nexlink": {"repo": repo,
                                     "workflows": ["deploy-api-prod.yml", "ci.yml"]}},
            "host": self.cfg.host})
        api = FauxApi()
        api.workflows = ["backend-suite.yml", "deploy-api-prod.yml", "ci.yml"]
        watcher = ci.Watcher(cfg, api=api)
        api.live = [run_view(1404, "d5" * 20, "lot/1-un", workflow="deploy-api-prod.yml"),
                    run_view(1406, "d7" * 20, "lot/1-un", workflow="ci.yml")]
        watcher.tick(self.db)
        self.assertEqual(st.ci.run(repo, 1404)["workflow"], "deploy-api-prod.yml")
        self.assertEqual(st.ci.run(repo, 1406)["workflow"], "ci.yml")

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
        self.assertEqual(self.scan_state(), (False, "truncated", len(ci.LIVE_STATUSES)))
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
        self.assertEqual(self.scan_state(), (True, None, len(ci.LIVE_STATUSES) + 2))
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
        self.assertEqual(len(self.guard_calls()), len(ci.LIVE_STATUSES) + 2)
        self.assertEqual(self.scan_state()[0], True)

    def test_garde_budget_ne_publie_pas_un_releve_partiel(self):
        """#845 (revue N/P2 2328 ; arbitrage A) : un relevé qui ne peut pas
        couvrir toutes les exécutions dans le budget d'appels s'arrête, ne
        publie RIEN (l'état précédent n'est ni défait ni rafraîchi : file ET
        runners gardent leur `seen_at`) et date son incomplétude. Avec
        len(LIVE_STATUSES) + 60 + 1 appels pour 60 exécutions, le budget est un
        garde-fou de dernier recours : le témoin abaisse le plafond dans une
        portée `mock` (11 exécutions, sous les 60 admises) pour l'exercer."""
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
        # limite est atteinte avant la fin (une liste par état vivant + les jobs
        # jusqu'au plafond) ; publier ce relevé effacerait l'alerte à tort.
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

    def test_garde_60_executions_releve_complet_au_budget(self):
        """#845 (arbitrage A) : le plafond de couverture (60 exécutions non
        conclues) coûte exactement le budget (une liste par état vivant +
        60 jobs + 1 runners) et publie un relevé complet."""
        horloge = FauxHorloge()
        self.watcher = ci.Watcher(self.cfg_ci, api=self.api, clock=horloge)
        self.api.runners = [runner_view(21, "ameesh-ci-1-1", busy=False)]
        self.api.live = [run_view(910 + i, "%040d" % i, "main")
                         for i in range(ci.GUARD_SCAN_MAX_RUNS)]
        for i in range(ci.GUARD_SCAN_MAX_RUNS):
            self.api.jobs[910 + i] = []
        self.watcher.tick(self.db)
        attendu = len(ci.LIVE_STATUSES) + ci.GUARD_SCAN_MAX_RUNS + 1
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
        self.assertEqual(self.scan_state(),
                         (False, "too_many_runs", len(ci.LIVE_STATUSES)))
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
        self.assertEqual(len(self.guard_calls()), len(ci.LIVE_STATUSES) + 2)
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
        # un seul constat : le relevé doit relire (une liste par état vivant,
        # puis un appel de jobs), la file ne doit garder que 9062, et le runner
        # qui tient 9061 ne doit pas être « stuck ».
        self.assertEqual(
            (len(self.guard_calls()), int(row["queued"]), int(row["oldest_id"]),
             [r["name"] for r in st.ci.stuck_runners()]),
            (len(ci.LIVE_STATUSES) + 2, 1, 9062, []), self.guard_calls())

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
        # une liste par état vivant + 1 jobs + l'appel runners qui échoue
        # (compté, puis levé)
        self.assertEqual(self.scan_state(), (False, "failed", len(ci.LIVE_STATUSES) + 2))
        (file_apres,) = storage.of(self.db).ci.queued_oldest()
        self.assertEqual(file_apres["seen_ts"], file_avant["seen_ts"],
                         "une erreur du relevé a publié la file")
        self.assertEqual(self.runner_seen_ts(21), runner_avant,
                         "une erreur du relevé a rafraîchi les runners")

    # -- #795 N2 : le courrier et sa preuve dans la même transaction ---------

    def _interrupt_allowed(self, row, name="dev-a"):
        """Ce que `runner.AgentWorker.interrupt_allowed` décide pour ce
        courrier (même montage que tests/test_l795_preemption_evenement.py)."""
        worker = types.SimpleNamespace(
            name=name, agent={"name": name}, db=self.db,
            cfg=types.SimpleNamespace(interrupt_senders=()),
            runner=types.SimpleNamespace(is_auditor=lambda n, a=None: False),
            _system_event_interrupt=lambda sender, r: runner.AgentWorker
            ._system_event_interrupt(worker, sender, r),
            _deep_analysis_interrupt=lambda sender, r: runner.AgentWorker
            ._deep_analysis_interrupt(worker, sender, r))
        return runner.AgentWorker.interrupt_allowed(worker, row)

    def _publication_probe(self, seen):
        """`set_message` instrumenté : au point de publication de la preuve,
        une AUTRE connexion lit la boîte et `ci_runs`, puis la preuve est
        écrite. Un lecteur intercalé (le dispatch de l'exécuteur) ne doit
        jamais voir le courrier engagé sans sa référence."""
        real = CiResults.set_message

        def set_message(st, repo, run_id, which, message_id):
            other = db_mod.connect(self.cfg)
            try:
                mails = other.query(
                    "SELECT count(*)::int AS n FROM agent_mailbox WHERE recipient = %s",
                    ("dev-a",))[0]["n"]
                run = other.query(
                    "SELECT urgent_message_id, verdict_message_id FROM ci_runs"
                    " WHERE repo = %s AND run_id = %s", (DEPOT, run_id))[0]
            finally:
                other.close()
            seen.append((which, mails, run["urgent_message_id"],
                         run["verdict_message_id"]))
            return real(st, repo, run_id, which, message_id)
        return set_message

    def test_alerte_et_preuve_publiees_ensemble(self):
        sha = "9" * 40
        self.api.live = [run_view(801, sha, "lot/1-un")]
        self.watcher.tick(self.db)
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1",
                                                   execution=801)])]
        seen = []
        with mock.patch.object(CiResults, "set_message",
                               self._publication_probe(seen)):
            out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["urgent"]) for o in out],
                         [("failures", True)])
        # au point de publication : ni courrier visible, ni référence
        self.assertEqual(seen, [("urgent", 0, None, None)])
        (alerte,) = self.mails("dev-a")
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 801)["urgent_message_id"],
                         alerte["id"])
        # après le commit, l'urgent est vérifiable : il préempte
        self.assertTrue(self._interrupt_allowed(
            mail.unread_urgent(self.db, "dev-a")[-1]))

    def test_verdict_et_preuve_publiees_ensemble(self):
        sha = "b" * 40
        self.api.live = [run_view(802, sha, "lot/1-un")]
        self.watcher.tick(self.db)
        self.api.live = []
        self.api.done[802] = {"status": "completed", "conclusion": "success"}
        seen = []
        with mock.patch.object(CiResults, "set_message",
                               self._publication_probe(seen)):
            out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["status"]) for o in out],
                         [("verdict", "success")])
        self.assertEqual(seen, [("verdict", 0, None, None)])
        (verdict,) = self.mails("dev-a")
        self.assertEqual(storage.of(self.db).ci.run(DEPOT, 802)["verdict_message_id"],
                         verdict["id"])
        # #514 : le verdict reste ordinaire, même publié avec sa preuve — il
        # réveille l'agent, il ne préempte pas son tour.
        self.assertFalse(mail.is_urgent(verdict))
        self.assertEqual(mail.unread_urgent(self.db, "dev-a"), [])

    def test_paquet_rouge_nomme_l_unite_probable(self):
        """#3801 : le courrier et `ci status` nomment l'unité du paquet.

        Le manifeste est lu à la tête du run (ici une composition d'une unité),
        la carte relie le module de test au fichier que l'unité modifie, et le
        retrait automatique n'existe pas : on nomme seulement.
        """
        from ameesh import packet_red
        from .test_3801_packet_red import GitRepo, card as red_card
        repo = GitRepo()
        self.addCleanup(repo.close)
        source, merge = repo.unit(101, {"src/ameesh/x.py": "x = 1\n"})
        repo.manifest([(101, source, merge)])
        repo.write("tests/test_a.py", "from .support import PgTestCase\n")
        sha = repo.commit("test")
        registry.upsert(self.db, "dev-packet", harness="claude", host=self.cfg.host)
        lot = work.add(self.db, title="Paquet h-test", assignee="dev-packet",
                       branch="integration/paquet-h-test")
        self.api.live = [run_view(810, sha, "integration/paquet-h-test")]
        self.api.branches["integration/paquet-h-test"] = sha
        data = red_card(blocs={"src/ameesh/x.py": {"f": {"modules": ["test_a"]}}})
        provenance = {"status": "lue", "commit": "c" * 40, "age_s": 3600}
        checkout = mock.patch.object(packet_red, "checkout_for",
                                     return_value=repo.path)
        carte = mock.patch.object(packet_red, "carte_for",
                                  return_value=(data, provenance))
        checkout.start()
        self.addCleanup(checkout.stop)
        carte.start()
        self.addCleanup(carte.stop)
        self.watcher.tick(self.db)
        self.api.checks[sha] = [check_run([ev_test("tests.test_a.T.test_1",
                                                   execution=810)])]
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["urgent"]) for o in out],
                         [("failures", True)])
        self.assertIn("unités probables : #101",
                      self.mails("dev-packet")[-1]["body"])
        # le verdict rouge porte la même unité
        self.api.live = []
        self.api.done[810] = {"status": "completed", "conclusion": "failure"}
        out = self.watcher.tick(self.db)
        self.assertEqual([(o["action"], o["status"]) for o in out],
                         [("verdict", "failure")])
        self.assertIn("unités probables : #101",
                      self.mails("dev-packet")[-1]["body"])
        self.assertIn("aucun retrait automatique",
                      self.mails("dev-packet")[-1]["body"])
        # `ameesh ci status` porte la même attribution
        report = ci.status_of(self.db, lot["id"], cfg=self.cfg)
        self.assertEqual(report["runs"][0]["attribution"]["manifest"]["units"], 1)
        texte = "\n".join(ci.render_status(report))
        self.assertIn("unités probables : #101", texte)


if __name__ == "__main__":
    unittest.main()
