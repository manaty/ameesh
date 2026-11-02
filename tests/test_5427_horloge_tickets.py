# SPDX-License-Identifier: AGPL-3.0-only
"""#5427 : les tickets d'admission de la passe horloge (`scripts/horloge-tickets.py`).

Sans réseau : `gh` est simulé. Le contrat vérifié est celui qui compte pour
la veille — un ticket par test, titre stable, corps qui nomme scénario, run et
tête ; un ticket déjà ouvert n'est pas dupliqué ; un `gh` en échec ne masque
pas le rouge de la passe.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "horloge_tickets", REPO / "scripts" / "horloge-tickets.py")
tickets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tickets)

REPORT = {
    "schema": "ameesh-horloge-passe/1",
    "scenarios": [
        {"name": "23h45", "tz": "UTC", "fake_at": "2026-11-02T23:45:00+00:00",
         "why": "le jour bascule dans 15 min",
         "failures": ["test_jour (tests.test_x.JourTest.test_jour)"],
         "output_tail": "KeyError: 'set_aside'"},
        {"name": "00h05", "tz": "UTC", "fake_at": "2026-11-03T00:05:00+00:00",
         "why": "le jour vient de basculer",
         "failures": ["test_jour (tests.test_x.JourTest.test_jour)",
                      "test_disque (tests.test_y.DisqueTest.test_disque)"],
         "output_tail": "AssertionError"},
    ],
    "verdict": "red",
}


class CollectTest(unittest.TestCase):
    def test_un_ticket_par_test_avec_tous_ses_scenarios(self):
        found = tickets.collect(REPORT)
        self.assertEqual(sorted(found), [
            "test_disque (tests.test_y.DisqueTest.test_disque)",
            "test_jour (tests.test_x.JourTest.test_jour)"])
        self.assertEqual([sc["name"] for sc in found[
            "test_jour (tests.test_x.JourTest.test_jour)"]], ["23h45", "00h05"])

    def test_empty_failures_are_ignored(self):
        self.assertEqual(tickets.collect({"scenarios": [
            {"failures": ["", None]}]}), {})

    def test_title_is_stable_and_bounded(self):
        self.assertEqual(tickets.title_for("x (m.C.test)"),
                         "test dépendant de l'environnement : x (m.C.test)")
        self.assertLessEqual(len(tickets.title_for("z" * 500)), tickets.MAX_TITLE)

    def test_body_names_scenarios_run_and_sha(self):
        body = tickets.body_for(
            "test_jour (tests.test_x.JourTest.test_jour)",
            tickets.collect(REPORT)[
                "test_jour (tests.test_x.JourTest.test_jour)"],
            run="https://github.com/o/r/actions/runs/1", sha="a" * 40)
        self.assertIn("test_jour", body)
        self.assertIn("23h45", body)
        self.assertIn("horloge simulée à 2026-11-02T23:45:00+00:00", body)
        self.assertIn("https://github.com/o/r/actions/runs/1", body)
        self.assertIn("a" * 40, body)
        self.assertIn("KeyError", body)


class OpenTicketsTest(unittest.TestCase):
    def _run(self, *, known=None, gh=None, dry_run=False):
        said = []
        with mock.patch.object(tickets, "existing", return_value=known or {}):
            actions = tickets.open_tickets(
                REPORT, repo="o/r", run="https://run/1", sha="b" * 40,
                dry_run=dry_run, say=lambda text, **_: said.append(text))
        return actions, said

    def test_opens_each_new_test(self):
        calls = []

        def gh(args, input_text=None):
            calls.append((args, input_text))
            return "https://github.com/o/r/issues/9\n"

        with mock.patch.object(tickets, "_gh", side_effect=gh):
            actions, said = self._run()
        self.assertEqual([a["action"] for a in actions], ["opened", "opened"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[0][:3] == ["issue", "create", "--repo"]
                            for call in calls))
        self.assertTrue(all(call[1].startswith("La passe horloge")
                            for call in calls))
        self.assertIn("ouvert : https://github.com/o/r/issues/9", said)

    def test_existing_ticket_is_not_duplicated(self):
        known = {tickets.title_for(test): 7 for test in tickets.collect(REPORT)}
        with mock.patch.object(tickets, "_gh") as gh:
            actions, said = self._run(known=known)
        gh.assert_not_called()
        self.assertEqual({a["action"] for a in actions}, {"existing"})
        self.assertEqual({a["issue"] for a in actions}, {7})
        self.assertIn("#7", said[0])

    def test_gh_failure_does_not_raise(self):
        with mock.patch.object(tickets, "_gh",
                               side_effect=tickets.TicketError("quota")):
            actions, said = self._run()
        self.assertEqual({a["action"] for a in actions}, {"failed"})
        self.assertIn("quota", actions[0]["why"])

    def test_dry_run_creates_nothing(self):
        with mock.patch.object(tickets, "_gh") as gh:
            actions, said = self._run(dry_run=True)
        gh.assert_not_called()
        self.assertEqual({a["action"] for a in actions}, {"would-open"})
        self.assertIn("DRY", said[0])


class MainTest(unittest.TestCase):
    def report_file(self, payload) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "rapport.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_dry_run(self):
        out = io.StringIO()
        with mock.patch.dict(tickets.os.environ,
                             {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "3"}), \
                mock.patch.object(tickets, "_gh") as gh, \
                contextlib.redirect_stdout(out):
            rc = tickets.main([self.report_file(REPORT), "--dry-run"])
        self.assertEqual(rc, 0)
        gh.assert_not_called()
        self.assertIn("DRY", out.getvalue())

    def test_missing_repo_is_a_usage_error(self):
        with mock.patch.dict(tickets.os.environ, {}, clear=True), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = tickets.main([self.report_file(REPORT), "--dry-run"])
        self.assertEqual(rc, 2)

    def test_bad_report_is_a_usage_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "bad.json"
        path.write_text("{", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = tickets.main([str(path), "--dry-run"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
