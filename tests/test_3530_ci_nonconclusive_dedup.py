# SPDX-License-Identifier: AGPL-3.0-only
"""#3530 — lots de CI non concluante : une clé par check, une part par job.

Le run 40179297365 (main f571ac5d4) a ouvert quatre lots pour deux parts
(#3522 à #3525) :

- `radar.nonconclusive_source` ne retirait que `(ÉTAT)`, alors que
  `release_cutoff.ci_reading` écrit `(état; URL)` depuis #3244 : chaque
  occurrence du même check rouvrait un lot au lieu de le noter ;
- les check runs `tests (flux) …` (L161c), que l'API GitHub rend aussi dans
  la liste des jobs d'une exécution, étaient lus comme des jobs : chaque
  part était consignée deux fois ;
- une part encore `in_progress` ou `queued` était consignée comme non
  concluante alors qu'elle n'avait pas conclu : elle attend la fin du run ou
  le délai du job, et n'ouvre aucun lot.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import packet_automerge, radar, release_cutoff, work

from . import test_2448_release_cutoff as fixture
from .test_3209_skipped_by_construction import _Lots


def rollup(**over):
    """La lecture globale de `radar.commit_ci` (les flux y figurent aussi)."""
    out = {"green": False, "acceptable": False, "runs": 1, "negative": [],
           "pending": [], "failed": [], "inconclusive": [], "missing": []}
    out.update(over)
    return out


def suite(*jobs, run_id=600, workflow="tests", event="push"):
    return {"runs": [fixture._run(run_id, list(jobs), workflow=workflow,
                                  event=event)]}


class KeyTest(unittest.TestCase):
    """La clé de dédoublonnage du lot de correction (#3209, #3530)."""

    def key(self, entry):
        return radar.nonconclusive_source("ameesh", entry)

    def test_a_state_and_url_suffix_is_dropped(self):
        for state in ("cancelled", "in_progress", "queued", "timed_out"):
            with self.subTest(state=state):
                self.assertEqual(
                    self.key("psql 13/16 (%s; https://github.com/manaty/ameesh/"
                             "actions/runs/40179297365)" % state),
                    "nonconclusive:ameesh:psql 13/16")

    def test_the_same_check_across_runs_keeps_one_key(self):
        first = self.key("psql 13/16 (cancelled; https://github.com/manaty/"
                          "ameesh/actions/runs/1)")
        second = self.key("psql 13/16 (cancelled; https://github.com/manaty/"
                           "ameesh/actions/runs/2)")
        bare = self.key("psql 13/16")
        self.assertEqual(first, second)
        self.assertEqual(first, bare)

    def test_a_parenthesised_name_survives_a_state_and_url_suffix(self):
        self.assertEqual(self.key("tests (linux) (CANCELLED; https://example/1)"),
                         self.key("tests (linux)"))
        self.assertEqual(self.key("tests (linux) (in_progress; https://example/2)"),
                         self.key("tests (linux)"))
        self.assertNotEqual(self.key("tests (linux) (cancelled; https://example/1)"),
                            self.key("tests (windows) (cancelled; https://example/1)"))

    def test_a_parenthesis_that_is_not_a_state_is_never_dropped(self):
        self.assertEqual(self.key("tests (linux; https://example/1)"),
                         "nonconclusive:ameesh:tests (linux; https://example/1)")
        self.assertEqual(self.key("tests (linux)"), "nonconclusive:ameesh:tests (linux)")

    def test_several_states_and_an_absent_url_are_still_consigned(self):
        self.assertEqual(self.key("ci/docs* (ERROR, SUCCESS)"),
                         "nonconclusive:ameesh:ci/docs*")
        self.assertEqual(self.key("tests (cancelled; )"),
                         "nonconclusive:ameesh:tests")


class ReadingTest(unittest.TestCase):
    """`release_cutoff.ci_reading` : ni flux compté comme job, ni part en attente."""

    project = release_cutoff.ProjectRelease("ameesh", "/unused",
                                            github="acme/project")

    def read(self, data, reading=None):
        with mock.patch.object(release_cutoff, "required_checks", return_value=()), \
                mock.patch.object(release_cutoff.radar, "commit_ci",
                                  return_value=reading or rollup()), \
                mock.patch.object(release_cutoff, "suite_reading", return_value=data):
            return release_cutoff.ci_reading(self.project, "a" * 40)

    def test_a_flow_check_is_not_counted_as_a_second_job(self):
        # Le run réel : la part est annulée, son check « tests (flux) » est
        # vert (les tests de la part avaient fini), et le rollup porte les
        # flux dans ses listes non concluantes/en attente. Sans filtre :
        # deux consignations.
        data = suite(
            fixture._job("plan"),
            fixture._job("psql 13/16", "cancelled"),
            fixture._job("tests"),
            fixture._job("tests (flux) psql 13/16"),
            # Flux resté en cours ou annulé alors que sa part a fini : sa
            # conclusion ne prouve rien, elle n'est pas un second job.
            fixture._job("psql 5/16"),
            fixture._job("tests (flux) psql 5/16", "cancelled"),
            # Forme préfixée de l'appel du portier : elle double aussi son job.
            fixture._job("suite complète du paquet / tests (flux) psql 4/16"),
        )
        reading = self.read(data, rollup(
            pending=["tests", "tests (flux) psql 13/16"],
            inconclusive=["tests (flux) psql 2/16 (CANCELLED)"]))
        self.assertTrue(reading["green"], reading)
        self.assertEqual(reading["negative"], [])
        self.assertEqual(reading["pending"], [])
        self.assertEqual(reading["inconclusive"], [
            "psql 13/16 (cancelled; "
            "https://github.com/manaty/ameesh/actions/runs/600)"])

    def test_a_flow_failure_remains_a_negative(self):
        # Le publisher écrit `fin=echec` puis le job facultatif est annulé
        # pendant la clôture : le flux est le seul porteur du négatif
        # (relecture bloquante de #3530, ameesh-cx3). Il ne doit jamais
        # disparaître du verdict.
        data = suite(
            fixture._job("plan"),
            fixture._job("tests"),
            fixture._job("autre système de fichiers 1/16", "cancelled"),
            fixture._job("tests (flux) autre système de fichiers 1/16", "failure"),
        )
        reading = self.read(data, rollup(
            negative=["tests (flux) autre système de fichiers 1/16 (FAILURE)"]))
        self.assertFalse(reading["green"], reading)
        self.assertFalse(reading["acceptable"], reading)
        self.assertTrue(any("tests (flux) autre système de fichiers 1/16"
                            in text for text in reading["negative"]), reading)
        self.assertEqual(reading["inconclusive"], [
            "autre système de fichiers 1/16 (cancelled; "
            "https://github.com/manaty/ameesh/actions/runs/600)"])

    def test_a_historical_flow_failure_is_not_repaired_by_a_rerun(self):
        # Un flux négatif d'un essai antérieur que le nouvel essai n'a pas
        # réparé (sa part est encore annulée) reste un négatif.
        old = fixture._run(700, [
            fixture._job("plan"), fixture._job("tests"),
            fixture._job("autre système de fichiers 1/16", "cancelled"),
            fixture._job("tests (flux) autre système de fichiers 1/16", "failure"),
        ])
        new = fixture._run(800, [
            fixture._job("plan"), fixture._job("tests"),
            fixture._job("autre système de fichiers 1/16", "cancelled"),
            fixture._job("tests (flux) autre système de fichiers 1/16", "success"),
        ])
        reading = self.read({"runs": [old, new]})
        self.assertFalse(reading["green"], reading)
        self.assertTrue(any("(failure;" in text
                            and "tests (flux) autre système de fichiers 1/16" in text
                            for text in reading["negative"]), reading)
        self.assertEqual([text for text in reading["inconclusive"]
                          if "tests (flux)" in text], [])

    def test_an_in_progress_or_queued_part_waits_and_is_not_consigned(self):
        data = suite(
            fixture._job("plan"),
            fixture._job("tests"),
            fixture._job("psql 13/16", "", "in_progress"),
            fixture._job("psql 4/16", "", "queued"),
            fixture._job("tests (flux) psql 13/16", "", "in_progress"),
        )
        reading = self.read(data, rollup(
            pending=["tests (flux) psql 13/16"]))
        self.assertTrue(reading["green"], reading)
        self.assertEqual(reading["inconclusive"], [])
        self.assertEqual(reading["pending"], [])

    def test_an_unfinished_aggregate_stays_pending_and_opens_no_lot(self):
        data = suite(
            fixture._job("plan"),
            fixture._job("tests", "", "in_progress"),
            fixture._job("psql 13/16", "", "queued"),
        )
        reading = self.read(data, rollup(pending=["tests"]))
        self.assertFalse(reading["green"], reading)
        self.assertFalse(reading["acceptable"], reading)
        self.assertEqual(reading["inconclusive"], [])
        self.assertTrue(any("agrégat tests non validé" in name
                            for name in reading["pending"]), reading)

    def test_a_concluded_cancelled_part_is_still_consigned_once(self):
        data = suite(
            fixture._job("plan"),
            fixture._job("tests"),
            fixture._job("psql 13/16", "cancelled"),
            fixture._job("tests (flux) psql 13/16", "success"),
        )
        reading = self.read(data)
        self.assertTrue(reading["green"], reading)
        self.assertEqual(reading["inconclusive"], [
            "psql 13/16 (cancelled; "
            "https://github.com/manaty/ameesh/actions/runs/600)"])


class OneLotPerCheckTest(unittest.TestCase):
    """Le même check, d'un paquet à une release, note son lot unique."""

    def setUp(self):
        self.lots = _Lots(self)

    def test_state_and_url_do_not_reopen_the_lot(self):
        packet_automerge._open_correction(
            object(), {"app": "ameesh"}, slug="manaty/ameesh", number=2427,
            sha="b" * 40, base="main",
            consigned=["psql 13/16 (cancelled; https://github.com/manaty/"
                       "ameesh/actions/runs/40179297365)"], actor="ameesh")
        plan = types.SimpleNamespace(head="d" * 40, version="1.22.0", ci={
            "inconclusive": ["psql 13/16 (cancelled; https://github.com/manaty/"
                             "ameesh/actions/runs/40179297366)"]})
        release_cutoff._open_correction(object(), types.SimpleNamespace(name="ameesh"),
                                        plan)
        self.assertEqual(len(self.lots.rows), 1)
        self.assertIn("psql 13/16", self.lots.rows[0]["title"])
        self.assertEqual(len(self.lots.notes), 1)
        self.assertEqual(self.lots.notes[0][0], self.lots.rows[0]["id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
