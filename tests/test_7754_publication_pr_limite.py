# SPDX-License-Identifier: AGPL-3.0-only
"""#7754 — le corps final de la PR ne perd plus le paquet sur une limite.

Constat h-13 (17/11) : `paquet-auto` a créé la PR #5962 à 23:18:31Z, gelé la
tête 3b6a6cc1 et demandé la ci-full. À 23:25Z, la mise à jour finale du corps
(`ensure_pull_request`, `gh pr view` puis `gh pr edit`) est tombée sous la
limite GraphQL du compte partagé : le script a levé `PaquetAutoError`, sorti en
rc 1, envoyé le courrier d'échec et ouvert le lot d'empreinte §16, alors que la
poussée et le gel avaient abouti.

Attendu : (1) une lecture `gh pr view` refusée pour quota est relue par l'API
REST (`gh api`), bucket `core` distinct du bucket GraphQL, dans
`ensure_pull_request` et `pull_request_state` ; (2) une mise à jour finale qui
échoue encore est dite à la note du porteur et au courrier, sans arrêter le
paquet (rc 0) ; (3) la PR créée à la poussée n'est jamais perdue.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.test_4304_paquet_poussee_precoce import Flux, green, paquet_auto

BRANCH = "integration/paquet-h-20261117-13"
URL = "https://github.com/manaty/ameesh/pull/5962"
QUOTA = "GraphQL: API rate limit exceeded for user ID 16659140."


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""
              ) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["gh"], returncode, stdout.encode("utf-8"), stderr.encode("utf-8"))


def rest_pull(*, state: str = "open", merged_at: str | None = None,
              number: int = 5962) -> str:
    return json.dumps([{"number": number, "html_url": URL, "state": state,
                        "merged_at": merged_at}])


class GhCalls:
    """Un faux `gh` : rend les réponses dans l'ordre et consigne les appels."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, _cwd, *args, timeout=None):
        self.calls.append(args)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class EnsurePullRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-7754-")
        self.addCleanup(self.tmp.cleanup)
        self.worktree = pathlib.Path(self.tmp.name)

    def ensure(self, *answers):
        fake = GhCalls(*answers)
        with mock.patch.object(paquet_auto, "_gh", fake), \
                mock.patch.object(paquet_auto, "temporary_parent",
                                  return_value=self.worktree):
            result = paquet_auto.ensure_pull_request(
                self.worktree, BRANCH, "main", "Paquet", "corps")
        return result, fake

    def test_une_limite_graphql_est_relue_par_le_rest(self):
        (number, url), fake = self.ensure(
            completed(1, "", QUOTA), completed(0, rest_pull()), completed(0))
        self.assertEqual((number, url), (5962, URL))
        self.assertEqual(len(fake.calls), 3)
        view, api, edit = fake.calls
        self.assertEqual(view[:3], ("pr", "view", BRANCH))
        self.assertEqual(api[:2],
                         ("api",
                          "repos/{owner}/{repo}/pulls?head={owner}:%s&state=all"
                          % BRANCH))
        self.assertEqual(edit[:3], ("pr", "edit", "5962"))

    def test_une_ecriture_encore_refusee_dit_la_cause(self):
        with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
            self.ensure(completed(1, "", QUOTA), completed(0, rest_pull()),
                        completed(1, "", QUOTA))
        self.assertIn("gh pr edit #5962", str(caught.exception))
        self.assertIn("rate limit", str(caught.exception))

    def test_une_branche_sans_pr_ne_consomme_pas_le_rest(self):
        (number, url), fake = self.ensure(
            completed(1, "", "no pull requests found for branch %s" % BRANCH),
            completed(0, URL),
            completed(1, "", "no pull requests found for branch %s" % BRANCH))
        self.assertIsNone(number)
        self.assertEqual(url, URL)
        self.assertEqual([call[0] for call in fake.calls], ["pr", "pr", "pr"])


class PullRequestStateTest(unittest.TestCase):
    def state(self, *answers) -> tuple[str, GhCalls]:
        fake = GhCalls(*answers)
        with mock.patch.object(paquet_auto, "_gh", fake):
            return paquet_auto.pull_request_state(
                pathlib.Path("."), BRANCH), fake

    def test_etat_ouvert_lu_par_le_rest(self):
        state, fake = self.state(completed(1, "", QUOTA),
                                 completed(0, rest_pull()))
        self.assertEqual(state, "OPEN")
        self.assertEqual([call[0] for call in fake.calls], ["pr", "api"])

    def test_etat_fusionne_lu_par_le_rest(self):
        state, _fake = self.state(
            completed(1, "", QUOTA),
            completed(0, rest_pull(state="closed",
                                   merged_at="2026-11-18T02:01:57Z")))
        self.assertEqual(state, "MERGED")

    def test_les_deux_lectures_illisibles_rendent_vide(self):
        state, fake = self.state(completed(1, "", QUOTA),
                                 completed(1, "", "HTTP 403"))
        self.assertEqual(state, "")
        self.assertEqual(len(fake.calls), 2)


class DernierePublicationTest(unittest.TestCase):
    def test_refus_final_rc0_note_et_courrier_disent_la_cause(self):
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="c" * 40)

        class FluxLimite(Flux):
            """Le second geste PR (corps final) est refusé pour quota."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.pr_calls = 0

            def log(self, name, result=None):
                base = super().log(name, result)
                if name != "pr":
                    return base

                def side_effect(*args, **kwargs):
                    self.pr_calls += 1
                    if self.pr_calls == 2:
                        raise paquet_auto.PaquetAutoError(
                            "gh pr view %s a échoué (limite de débit) : %s ; "
                            "repli REST : HTTP 403" % (BRANCH, QUOTA))
                    return base(*args, **kwargs)

                return side_effect

        flux = FluxLimite(self, tests=[([green()], [], [])],
                          compositions=[composition])
        self.assertEqual(flux.run(), 0)
        self.assertEqual(flux.pr_calls, 2)
        self.assertTrue(flux.calls("note"))
        self.assertEqual(len(flux.calls("mail")), 1)
        note = flux.calls("note")[-1][1][2]
        mail = flux.calls("mail")[0][1][2]
        for text in (note, mail):
            self.assertIn("corps de PR non mis à jour", text)
            self.assertIn("limite de débit", text)
        self.assertIn("PR #3281", mail)


class UpdatePullRequestBodyTest(unittest.TestCase):
    def test_un_echec_est_rendu_avec_le_numero_de_la_poussee(self):
        with mock.patch.object(paquet_auto, "ensure_pull_request",
                               side_effect=paquet_auto.PaquetAutoError("refus")):
            number, url, failure = paquet_auto.update_pull_request_body(
                pathlib.Path("."), BRANCH, "main", "Paquet", "corps",
                3281, URL)
        self.assertEqual((number, url), (3281, URL))
        self.assertIn("corps de PR non mis à jour", failure)
        self.assertIn("refus", failure)


if __name__ == "__main__":
    unittest.main()
