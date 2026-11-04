# SPDX-License-Identifier: AGPL-3.0-only
"""#5742 : ENOSPC explicite est infra ; un négatif voisin reste bloquant."""
import contextlib
import importlib.util
import io
import json
import urllib.error
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agregat_disk_5742", ROOT / "scripts/ci-agregat.py")
agregat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agregat)


def job(**over):
    result = {"name": "psql 1/2", "status": "completed", "conclusion": "failure",
              "steps": [{"name": "part 1", "status": "completed", "conclusion": "failure"}]}
    result.update(over)
    return result


def traceback(message, name="test_disk"):
    return ("=" * 70 + "\nERROR: " + name + " (tests.fixture.Case)\n" + "-" * 70
            + "\nTraceback (most recent call last):\n" + message + "\n")


def classify(jobs):
    return agregat.classify(event="push", plan="success", version="skipped",
                            version_only="false", docs_only="false", doc_readers="skipped",
                            psycopg="success", psycopg_parts="[1]",
                            psql="failure", psql_parts="[1,2]", psql_jobs=jobs)


class EvidenceTest(unittest.TestCase):
    def test_tous_les_tracebacks_doivent_etre_disk(self):
        disk = traceback("OSError: [Errno 28] No space left on device")
        self.assertTrue(agregat.disk_infrastructure(job(), disk))
        self.assertTrue(agregat.disk_infrastructure(job(), disk + disk))
        self.assertFalse(agregat.disk_infrastructure(job(), disk + traceback("AssertionError: 2 != 3")))
        self.assertFalse(agregat.disk_infrastructure(job(), traceback("AssertionError: 2 != 3") + disk))
        self.assertFalse(agregat.disk_infrastructure(job(), disk + "ERROR: test_other (tests.X)\n"))
        self.assertFalse(agregat.disk_infrastructure(job(), disk + "test_other ... FAIL\n"))

    def test_avertissement_recupere_ne_suffit_pas(self):
        for text in ("No space left on device\n", "ENOSPC\n",
                     "No space left on device\nFAILED (errors=1)\n"):
            self.assertFalse(agregat.disk_infrastructure(job(), text))
        self.assertFalse(agregat.disk_infrastructure(job(), "No space left on device\nnot ok 1 - real regression"))

    def test_refus_et_worker_mort_explicitement_disk(self):
        self.assertTrue(agregat.disk_infrastructure(job(), "::warning::CI non concluante : marge disque insuffisante ou illisible ; / : 0.20 GiB libres"))
        self.assertTrue(agregat.disk_infrastructure(job(), "System.IO.IOException: No space left on device : Worker.log"))
        # Le timestamp Actions ne fait pas disparaître les frontières des erreurs.
        text = traceback("OSError: [Errno 28] No space left on device")
        timestamped = "\n".join("2026-11-04T10:12:35.001Z " + line for line in text.splitlines())
        self.assertTrue(agregat.disk_infrastructure(job(), timestamped))

    def test_un_autre_pas_en_echec_est_conserve(self):
        row = job()
        row["steps"].append({"name": "artifact", "conclusion": "failure"})
        self.assertFalse(agregat.disk_infrastructure(row, traceback("OSError: [Errno 28] No space left on device")))
        self.assertFalse(agregat.disk_infrastructure(job(conclusion="success"), "System.IO.IOException: No space left on device"))


class ClassifierTest(unittest.TestCase):
    def test_infra_seule_avec_part_verte_est_consignee_sans_negatif(self):
        rows = [job(infrastructure="disk-full"), job(name="psql 2/2", conclusion="success")]
        result = classify(rows)
        self.assertEqual(result["state"], agregat.GREEN)
        self.assertEqual(result["red"], [])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["consigned"][0]["result"], "infrastructure")

    def test_vrai_negatif_a_cote_garde_le_rouge(self):
        result = classify([job(infrastructure="disk-full"), job(name="psql 2/2")])
        self.assertEqual(result["state"], agregat.RED)
        self.assertTrue(result["red"])

    def test_aucune_preuve_ou_preuve_illisible_ne_fabrique_pas_de_vert(self):
        for rows in ([job(infrastructure="disk-full"), job(name="psql 2/2", infrastructure="disk-full")],
                     [job(infrastructure="disk-full")]):
            result = classify(rows)
            self.assertEqual(result["state"], agregat.RED)
            self.assertEqual(result["red"], [])
            self.assertTrue(result["missing"])
        result = classify([job(), job(name="psql 2/2", conclusion="success")])
        self.assertEqual(result["state"], agregat.RED)
        self.assertTrue(result["red"])

    def test_publier_neutral_est_obligatoire(self):
        class Gh:
            def create(self, name, conclusion, title, summary):
                self.conclusion = conclusion
                self.summary = summary
        gh = Gh()
        rows = [job(infrastructure="disk-full"), job(name="psql 2/2", conclusion="success")]
        with contextlib.redirect_stdout(io.StringIO()):
            code = agregat.main(["--event", "push", "--plan", "success", "--version", "skipped",
                                 "--psycopg", "success", "--psycopg-parts", "[1]", "--psql", "failure",
                                 "--psql-parts", "[1,2]", "--publish"], gh=gh, jobs=rows)
        self.assertEqual(code, 0)
        self.assertEqual(gh.conclusion, "neutral")
        self.assertIn("ENOSPC", gh.summary)


class TransportTest(unittest.TestCase):
    def test_preuve_log_est_raccordee_aux_jobs_et_lecture_ratee_ne_classe_pas_infra(self):
        row = job(id=123)
        payload = json.dumps({"total_count": 1, "jobs": [row]}).encode()
        api = agregat.GitHub({"GH_TOKEN": "faux", "GITHUB_REPOSITORY": "example/repo", "GITHUB_SHA": "a" * 40})
        for evidence, expected in ((traceback("OSError: [Errno 28] No space left on device"), True),
                                   (traceback("AssertionError: regression"), False),
                                   (agregat.AgregatError("log illisible"), False)):
            response = io.BytesIO(payload)
            with mock.patch.object(agregat.urllib.request, "urlopen", return_value=response), \
                    mock.patch.object(api, "job_log", **({"side_effect": evidence}
                                      if isinstance(evidence, Exception) else {"return_value": evidence})), \
                    contextlib.redirect_stderr(io.StringIO()):
                rows = api.jobs(1)
            self.assertEqual(rows[0].get("infrastructure") == "disk-full", expected)

    def test_url_signee_ne_recoit_pas_le_jeton_et_troncature_refusee(self):
        api = agregat.GitHub({"GH_TOKEN": "faux-secret", "GITHUB_REPOSITORY": "example/repo"})
        redirect = agregat.urllib.error.HTTPError("https://api.github.com/log", 302, "redirect",
                                                {"Location": "https://logs.example.invalid/signed"}, None)
        opener = mock.Mock()
        opener.open.side_effect = redirect
        with mock.patch.object(agregat.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(agregat.urllib.request, "urlopen", return_value=io.BytesIO(b"log")) as signed:
            self.assertEqual(api.job_log(123), "log")
        signed.assert_called_once_with("https://logs.example.invalid/signed", timeout=5)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer faux-secret")
        response = io.BytesIO(b"x" * (8 * 1024 * 1024 + 1))
        opener.open.side_effect = None
        opener.open.return_value = response
        with mock.patch.object(agregat.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(agregat.AgregatError, "truncated"):
                api.job_log(123)
