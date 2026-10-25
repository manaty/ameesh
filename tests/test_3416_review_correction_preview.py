# SPDX-License-Identifier: AGPL-3.0-only
"""#3416 — l'aperçu qui prévient un relecteur avant un correctif trop serré.

Cas de référence : #3186, auteur claude, deux avis requis ; le relecteur codex
corrige au premier tour, sa famille devient auteur de la tête (#2888) et seule
deepseek reste éligible. La correction doit être renvoyée à l'auteur avant
d'être poussée (« §2 : retour à l'auteur »).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import types
import unittest
from unittest import mock

from ameesh import mesh_cli
from ameesh import review_correction as rc
from ameesh import review_designation
from ameesh import review_designation_activation

FAMILIES = ["codex", "deepseek", "claude"]
WRITER = [{"name": "redacteur", "family": "claude"}]


def reviewer_row(label: str, harness: str, *, live: bool = True):
    return review_designation.Candidate(name=label, family=harness, available=live)


BASE_CANDIDATES = [
    reviewer_row("lecteur-codex", "codex"),
    reviewer_row("lecteur-ds", "deepseek"),
    reviewer_row("lecteur-claude", "claude"),
]


class RemainingRoomTest(unittest.TestCase):
    def measure(self, **overrides):
        params = {
            "required": 2,
            "reviewer": {"name": "lecteur-codex", "family": "codex"},
            "authors": WRITER,
            "candidates": BASE_CANDIDATES,
            "families": FAMILIES,
        }
        params.update(overrides)
        return rc.outlook(**params)

    def test_the_3186_case_leaves_a_single_family(self):
        result = self.measure()
        self.assertEqual(result["state"], rc.STATE_TOO_TIGHT)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(result["families"], ["deepseek"])
        self.assertEqual(result["missing"], 1)
        self.assertTrue(result["why"].startswith(rc.BACK_TO_AUTHOR))
        self.assertIn("lecteur-codex", result["why"])

    def test_a_single_required_opinion_still_has_deepseek(self):
        result = self.measure(required=1)
        self.assertEqual(result["state"], rc.STATE_ENOUGH)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(result["families"], ["deepseek"])

    def test_a_fourth_family_keeps_two_opinions_possible(self):
        result = self.measure(
            families=FAMILIES + ["gemini"],
            candidates=BASE_CANDIDATES + [reviewer_row("lecteur-gm", "gemini")])
        self.assertEqual(result["state"], rc.STATE_ENOUGH)
        self.assertEqual(result["remaining"], 2)
        self.assertEqual(result["families"], ["deepseek", "gemini"])

    def test_an_earlier_corrector_already_consumed_its_family(self):
        result = self.measure(
            required=1,
            reviewer={"name": "lecteur-ds", "family": "deepseek"},
            earlier=[{"name": "lecteur-codex", "family": "codex"}])
        self.assertEqual(result["state"], rc.STATE_TOO_TIGHT)
        self.assertEqual(result["families"], [])
        self.assertEqual(result["remaining"], 0)

    def test_an_unavailable_agent_does_not_cover_an_opinion(self):
        result = self.measure(
            required=1,
            candidates=[reviewer_row("lecteur-codex", "codex"),
                        reviewer_row("lecteur-ds", "deepseek", live=False)])
        self.assertEqual(result["state"], rc.STATE_TOO_TIGHT)
        self.assertEqual(result["remaining"], 0)

    def test_an_unknown_reviewer_family_is_never_ok(self):
        result = self.measure(reviewer={"name": "inconnu", "family": ""})
        self.assertEqual(result["state"], rc.STATE_UNSURE)
        self.assertNotEqual(result["state"], rc.STATE_ENOUGH)

    def test_an_unknown_author_family_is_never_ok(self):
        result = self.measure(authors=[{"name": "redacteur", "family": ""}])
        self.assertEqual(result["state"], rc.STATE_UNSURE)

    def test_no_required_opinion_needs_no_warning(self):
        result = self.measure(required=0)
        self.assertEqual(result["state"], rc.STATE_ENOUGH)
        self.assertEqual(result["missing"], 0)

    def test_a_manual_seat_counts_in_the_remaining_room(self):
        # deepseek tient un siège manuel ; codex corrige et devient auteur : il
        # ne reste que le siège deepseek pour deux avis.
        result = self.measure(
            required=2,
            candidates=[reviewer_row("lecteur-codex", "codex")],
            seats=[{"name": "manuel-ds", "family": "deepseek"}])
        self.assertEqual(result["state"], rc.STATE_TOO_TIGHT)
        self.assertEqual(result["seats"], [{"name": "manuel-ds", "family": "deepseek"}])
        self.assertEqual(result["remaining"], 1)

    def test_manual_seats_can_cover_the_requirement_after_the_correction(self):
        result = self.measure(
            required=2,
            families=FAMILIES + ["gemini"],
            candidates=[reviewer_row("lecteur-codex", "codex")],
            seats=[{"name": "manuel-ds", "family": "deepseek"},
                   {"name": "manuel-gm", "family": "gemini"}])
        self.assertEqual(result["state"], rc.STATE_ENOUGH)
        self.assertEqual(result["remaining"], 2)


def _inputs(**overrides):
    data = {
        "item": {"id": 3186, "assignee": "redacteur", "app": "ameesh"},
        "freeze": {"id": 18170, "sha": "a" * 40},
        "policy": types.SimpleNamespace(required=2, families=FAMILIES),
        "authors": WRITER,
        "unknown_authors": [],
        "correctors": [],
        "candidates": BASE_CANDIDATES,
        "manual_seats": [],
    }
    data.update(overrides)
    return types.SimpleNamespace(**data)


class PreparationReadTest(unittest.TestCase):
    def test_the_room_comes_from_the_latest_freeze_inputs(self):
        with mock.patch.object(review_designation_activation, "prepare", return_value=_inputs()):
            result = rc.check(None, None, 3186, "lecteur-codex")
        self.assertEqual(result["state"], rc.STATE_TOO_TIGHT)
        self.assertEqual(result["lot"], 3186)

    def test_a_manual_reviewer_outside_the_candidates_is_read_at_the_registry(self):
        inputs = _inputs(candidates=[], manual_seats=[])
        with mock.patch.object(review_designation_activation, "prepare", return_value=inputs), \
                mock.patch.object(review_designation_activation.registry, "overview",
                                  return_value=[{"name": "manuel-ds",
                                                 "session_harness": "deepseek"}]):
            result = rc.check(None, None, 3186, "manuel-ds")
        self.assertEqual(result["family"], "deepseek")
        self.assertEqual(result["state"], rc.STATE_TOO_TIGHT)  # aucun candidat pour les avis

    def test_an_unavailable_freeze_is_named_not_guessed(self):
        with mock.patch.object(review_designation_activation, "prepare",
                               return_value=review_designation_activation.Unavailable("pas de gel")):
            result = rc.check(None, None, 3186, "lecteur-codex")
        self.assertEqual(result["state"], rc.STATE_UNAVAILABLE)
        self.assertIn("pas de gel", result["why"])

    def test_a_failed_preparation_is_named_not_guessed(self):
        with mock.patch.object(review_designation_activation, "prepare",
                               side_effect=RuntimeError("dépôt absent")):
            result = rc.check(None, None, 3186, "lecteur-codex")
        self.assertEqual(result["state"], rc.STATE_UNAVAILABLE)
        self.assertIn("dépôt absent", result["why"])

    def test_unknown_author_provenance_is_never_guessed(self):
        with mock.patch.object(review_designation_activation, "prepare",
                               return_value=_inputs(unknown_authors=["deadbeef"])):
            result = rc.check(None, None, 3186, "lecteur-codex")
        self.assertEqual(result["state"], rc.STATE_UNSURE)
        self.assertIn("deadbeef", result["why"])


class DesignatedReviewerWarningTest(unittest.TestCase):
    ITEM = {"id": 3186, "assignee": "redacteur", "app": "ameesh"}
    FREEZE = {"id": 18170, "sha": "b" * 40, "at_ts": 1.0}

    def warn(self, actor, *, holders=("lecteur-codex",), outcome=None):
        result = outcome or {"state": rc.STATE_TOO_TIGHT,
                             "why": rc.BACK_TO_AUTHOR + ": trop serré"}
        with mock.patch.object(rc.work, "get", return_value=dict(self.ITEM)), \
                mock.patch.object(review_designation_activation, "_latest_freeze",
                                  return_value=dict(self.FREEZE)), \
                mock.patch.object(review_designation_activation, "_designated_reviewers",
                                  return_value=set(holders)), \
                mock.patch.object(rc, "check", return_value=result) as checked:
            return rc.warning(None, None, 3186, actor), checked

    def test_a_designated_reviewer_is_warned_without_a_network_read(self):
        advice, checked = self.warn("lecteur-codex")
        self.assertIn(rc.BACK_TO_AUTHOR, advice)
        self.assertIn("--if-corrector lecteur-codex", advice)
        self.assertEqual(checked.call_args.kwargs.get("fetch"), False)

    def test_the_author_is_never_warned(self):
        advice, checked = self.warn("redacteur", holders=("lecteur-codex",))
        self.assertEqual(advice, "")
        checked.assert_not_called()

    def test_an_unknown_actor_is_silent(self):
        self.assertEqual(rc.warning(None, None, 3186, ""), "")

    def test_a_room_that_is_not_short_is_silent(self):
        advice, _ = self.warn("lecteur-codex",
                              outcome={"state": rc.STATE_ENOUGH, "why": "assez"})
        self.assertEqual(advice, "")

    def test_an_unreadable_journal_never_raises(self):
        with mock.patch.object(rc.work, "get", side_effect=RuntimeError("db")):
            self.assertEqual(rc.warning(None, None, 3186, "lecteur-codex"), "")


class CliWiringTest(unittest.TestCase):
    """La CLI porte les deux gestes : aperçu avant poussée et avis bloquant."""

    def test_the_preview_returns_one_when_the_correction_is_too_tight(self):
        args = argparse.Namespace(work_command="reviewers", id=3186,
                                  if_corrector="lecteur-codex", apply=False,
                                  json=False, actor="")
        with mock.patch.object(mesh_cli, "_open", return_value=mock.Mock()), \
                mock.patch.object(mesh_cli, "_work_actor"), \
                mock.patch.object(rc, "check",
                                  return_value={"state": rc.STATE_TOO_TIGHT,
                                                "why": rc.BACK_TO_AUTHOR}) as checked, \
                contextlib.redirect_stdout(io.StringIO()):
            code = mesh_cli.cmd_work(object(), args)
        self.assertEqual(code, 1)
        self.assertEqual(checked.call_args.args[2:], (3186, "lecteur-codex"))

    def test_the_preview_returns_zero_when_the_room_is_enough(self):
        args = argparse.Namespace(work_command="reviewers", id=3186,
                                  if_corrector="lecteur-codex", apply=False,
                                  json=False, actor="")
        with mock.patch.object(mesh_cli, "_open", return_value=mock.Mock()), \
                mock.patch.object(mesh_cli, "_work_actor"), \
                mock.patch.object(rc, "check",
                                  return_value={"state": rc.STATE_ENOUGH, "why": "assez"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mesh_cli.cmd_work(object(), args), 0)

    def test_a_blocked_verdict_prints_the_warning_on_stderr(self):
        args = argparse.Namespace(
            work_command="milestone", id=3186, kind="verdict", sha="",
            verdict="blocked", note="", actor="lecteur-codex", repo=None,
            supersedes=None, no_static_checks=None, prevalidation_map="", json=False)
        row = {"kind": "verdict", "verdict": "blocked",
               "actor": "lecteur-codex", "note": ""}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mesh_cli, "_open", return_value=mock.Mock()), \
                mock.patch.object(mesh_cli, "_work_actor"), \
                mock.patch.object(mesh_cli.work, "milestone", return_value=row), \
                mock.patch.object(rc, "warning",
                                  return_value=rc.BACK_TO_AUTHOR + " — trop serré"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mesh_cli.cmd_work(object(), args)
        self.assertEqual(code, 0)
        self.assertIn("warning:", err.getvalue())
        self.assertIn(rc.BACK_TO_AUTHOR, err.getvalue())

    def test_a_verdict_that_is_not_blocked_prints_no_warning(self):
        args = argparse.Namespace(
            work_command="milestone", id=3186, kind="verdict", sha="",
            verdict="ok", note="", actor="lecteur-codex", repo=None,
            supersedes=None, no_static_checks=None, prevalidation_map="", json=False)
        row = {"kind": "verdict", "verdict": "ok", "actor": "lecteur-codex", "note": ""}
        err = io.StringIO()
        with mock.patch.object(mesh_cli, "_open", return_value=mock.Mock()), \
                mock.patch.object(mesh_cli, "_work_actor"), \
                mock.patch.object(mesh_cli.work, "milestone", return_value=row), \
                mock.patch.object(rc, "warning") as warned, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            self.assertEqual(mesh_cli.cmd_work(object(), args), 0)
        warned.assert_not_called()
        self.assertNotIn("warning:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
