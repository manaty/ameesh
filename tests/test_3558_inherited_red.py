# SPDX-License-Identifier: AGPL-3.0-only
"""#3558 — un rouge seulement hérité de la cible ne retient pas la fusion.

Le veilleur marque déjà chaque échec `inherited` quand son test était rouge au
dernier relevé conclu de la base (#1612, `ci.py` / `st.ci.failures`). La fusion
automatique d'un paquet exigeait pourtant tous les checks requis verts : h-18
(#3510, PR #2699) est resté `pending` sur son seul rouge `psql 1/16` — trois
témoins psycopg déjà rouges sur main — et impl-01 a forcé la fusion à la main ;
le pilote a dû écrire la même consigne pour h-19. Le canon (§3, §11 bis) dit
qu'un rouge que le paquet n'introduit pas ne le retient pas.

Témoins ici : rouge hérité seul → fusion + note au lot + incident de la cible ;
un échec propre en plus → `pending` ; une part rouge sans test lu → `pending` ;
une base sans relevé conclu → `pending`. Les autres cas (marqueur périmé, part
annulée, check en attente, flux sans part) sont les verrous de la même porte.
"""
from __future__ import annotations

import contextlib
import json
import types
import unittest
from unittest import mock

from ameesh import packet_automerge, storage, work

from .test_2813_packet_auto_merge import (HEAD, LOT, NUM, SLUG, FakeGh,  # noqa: E402
                                         Journal)

BASE_RUN = 300
BASE_SHA = "9" * 40
RUN = 200
FAIL_TESTS = ("tests.test_a", "tests.test_b", "tests.test_c")


def check(name, conclusion, *, run=RUN, workflow="tests", job=123):
    """Un check run de la tête, rattaché à l'exécution `run`."""
    return {"__typename": "CheckRun", "name": name, "workflowName": workflow,
            "status": "COMPLETED", "conclusion": conclusion,
            "detailsUrl": "https://github.com/%s/actions/runs/%d/job/%d"
                          % (SLUG, run, job)}


def pending(name, *, run=RUN, workflow="tests", job=123):
    return {"__typename": "CheckRun", "name": name, "workflowName": workflow,
            "status": "IN_PROGRESS", "conclusion": "",
            "detailsUrl": "https://github.com/%s/actions/runs/%d/job/%d"
                          % (SLUG, run, job)}


def flux(name, conclusion):
    """Le check d'une part publié par le portier (`/runs/<check-id>`, aucun
    run Actions : #3494)."""
    return {"__typename": "CheckRun", "name": name, "workflowName": "paquet",
            "status": "COMPLETED", "conclusion": conclusion,
            "detailsUrl": "https://github.com/%s/runs/121135624438" % SLUG}


class Ci:
    """La lecture de CI du lot : exécutions, échecs et référence de base."""

    def __init__(self, *, inherited=True, tests=FAIL_TESTS, base=None):
        self.runs = {RUN: {"run_id": RUN, "head_sha": HEAD, "workflow": "tests.yml",
                           "url": "https://github.com/%s/actions/runs/%d" % (SLUG, RUN)}}
        self.rows = {RUN: [{"part": "psql 1/16", "test": test, "inherited": inherited}
                           for test in tests]}
        self.reference = (base if base is not None else
                          {"run_id": BASE_RUN, "head_sha": BASE_SHA,
                           "tests": list(FAIL_TESTS)})

    def run(self, repo, run_id):
        return self.runs.get(int(run_id))

    def failures(self, repo, run_id):
        return self.rows.get(int(run_id), [])

    def base_failures(self, repo, branch, *, workflow=""):
        return self.reference


class InheritedGh(FakeGh):
    """Le faux `gh` avec le rollup exact de la tête (tous workflows)."""

    def __init__(self, entries, **kwargs):
        super().__init__(tests="failure", **kwargs)
        self.entries = list(entries)

    def answer(self, args):
        if list(args[:2]) == ["pr", "view"]:
            self.calls.append(list(args))
            return 0, json.dumps({
                "state": self.state, "headRefOid": self.head,
                "headRefName": self.branch, "baseRefName": self.base,
                "isDraft": False, "autoMergeRequest": None,
                "statusCheckRollup": self.entries, "title": "packet"}), ""
        return super().answer(args)


class InheritedRedTest(Journal):
    def setUp(self):
        super().setUp()
        self.armed()
        self.ci = Ci()
        self.created = []
        self.open_incident = None
        self.existing_incident = []
        self.add_error = False
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(
            storage, "of", lambda db: types.SimpleNamespace(
                ci=self.ci,
                work=types.SimpleNamespace(
                    by_issue_ref=lambda ref: self.existing_incident))))
        stack.enter_context(mock.patch.object(
            work, "open_with_source", lambda db, source: self.open_incident))
        stack.enter_context(mock.patch.object(work, "add", self._add))

    def _add(self, db, **fields):
        if self.add_error:
            raise RuntimeError("unique ref already taken")
        self.created.append(fields)
        return {"id": 9000 + len(self.created)}

    def rollup(self, *, part=True, extra=()):
        entries = [check("tests", "FAILURE")]
        if part:
            entries.append(check("psql 1/16", "FAILURE", job=124))
        entries += [flux("tests (flux) psql 1/16", "FAILURE"),
                    flux("tests (bilan des parts)", "FAILURE")]
        entries += list(extra)
        return entries

    def check(self, gh, **kwargs):
        return packet_automerge.check(None, None, LOT, gh=gh, **kwargs)

    def merged_notes(self):
        return [note for note in self.notes() if note.startswith("auto-merge merged")]

    def test_an_inherited_red_alone_merges_and_journals_the_base(self):
        gh = InheritedGh(self.rollup())
        out = self.check(gh)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", HEAD)])
        merged = self.merged_notes()
        self.assertEqual(len(merged), 1)
        self.assertIn("merged over inherited red: psql 1/16 %s base %s"
                      % (",".join(FAIL_TESTS), BASE_SHA), merged[0])
        self.assertEqual(out["inherited"]["parts"], ["psql 1/16"])
        self.assertEqual(out["inherited"]["base_shas"], [BASE_SHA])
        self.assertEqual(len(self.created), 1)
        incident_lot = self.created[0]
        self.assertEqual(incident_lot["source"],
                         "ci-incident:%s:%d" % (SLUG, BASE_RUN))
        self.assertEqual((incident_lot["type"], incident_lot["priority"]),
                         ("bug", 1))
        self.assertIn("INCIDENT CI main", incident_lot["title"])
        self.assertIn("merged over inherited red: psql 1/16", incident_lot["body"])
        self.assertTrue(incident_lot["body"].strip().endswith(
            "the target stays red until the correction lands"))

    def test_an_open_incident_receives_the_occurrence_instead_of_a_new_lot(self):
        self.open_incident = {"id": 4242}
        gh = InheritedGh(self.rollup())
        self.assertEqual(self.check(gh)["result"], "merged")
        self.assertEqual(self.created, [])
        notes = [entry["note"] for entry in self.events.get(4242, [])]
        self.assertTrue(any("merged over inherited red" in note for note in notes), notes)
        self.assertTrue(any("packet #%d" % LOT in note for note in notes), notes)

    def test_a_closed_incident_receives_the_occurrence_without_a_duplicate(self):
        # L'incident de l'exécution de base existe déjà, fermé : l'identité
        # `ci-incident:` est unique quel que soit l'état, on note l'occurrence
        # dessus au lieu d'ouvrir un second lot.
        self.existing_incident = [{"id": 777}]
        self.add_error = True
        gh = InheritedGh(self.rollup())
        self.assertEqual(self.check(gh)["result"], "merged")
        self.assertEqual(self.created, [])
        notes = [entry["note"] for entry in self.events.get(777, [])]
        self.assertTrue(any("merged over inherited red" in note for note in notes), notes)

    def test_an_own_failure_in_addition_blocks(self):
        self.ci.rows[RUN].append({"part": "psql 1/16", "test": "tests.test_new",
                                  "inherited": False})
        gh = InheritedGh(self.rollup())
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.landed, [])
        self.assertEqual(self.created, [])
        self.assertEqual(self.merged_notes(), [])

    def test_a_red_part_without_a_read_test_blocks(self):
        # `psycopg 1/8` rougit mais aucun événement de test n'a été lu : une
        # part rouge sans test identifié reste une preuve manquante.
        gh = InheritedGh(self.rollup(extra=[check("psycopg 1/8", "FAILURE",
                                                  job=125)]))
        self.assertEqual(self.check(gh)["result"], "pending")
        self.assertEqual(gh.landed, [])

    def test_an_aggregate_without_a_red_part_blocks(self):
        # L'agrégat requis rougit, les échecs hérités existent, mais aucune
        # part rouge n'est lue : ce n'est pas un échec de test identifié.
        entries = [entry for entry in self.rollup()
                   if entry["name"] != "psql 1/16"]
        gh = InheritedGh(entries)
        self.assertEqual(self.check(gh)["result"], "pending")
        self.assertEqual(gh.landed, [])

    def test_a_flux_part_without_a_read_test_blocks(self):
        entries = [entry for entry in self.rollup()
                   if entry["name"] != "tests (flux) psql 1/16"]
        entries.append(flux("tests (flux) psycopg 7/8", "FAILURE"))
        gh = InheritedGh(entries)
        self.assertEqual(self.check(gh)["result"], "pending")
        self.assertEqual(gh.landed, [])

    def test_a_base_without_a_finished_run_blocks(self):
        self.ci.reference = {}
        gh = InheritedGh(self.rollup())
        self.assertEqual(self.check(gh)["result"], "pending")
        self.assertEqual(gh.landed, [])
        self.assertEqual(self.created, [])

    def test_an_unreadable_base_blocks(self):
        def boom(*args, **kwargs):
            raise RuntimeError("base unreadable")
        self.ci.base_failures = boom
        gh = InheritedGh(self.rollup())
        self.assertEqual(self.check(gh)["result"], "pending")
        self.assertEqual(gh.landed, [])

    def test_a_stale_inherited_marker_blocks(self):
        # La base a depuis un relevé conclu qui ne porte plus un des tests :
        # le marqueur `inherited` enregistré plus tôt ne suffit plus.
        self.ci.reference = {"run_id": BASE_RUN, "head_sha": BASE_SHA,
                             "tests": list(FAIL_TESTS[:2])}
        gh = InheritedGh(self.rollup())
        self.assertEqual(self.check(gh)["result"], "pending")

    def test_a_cancelled_part_blocks(self):
        # #3449 : une part planifiée annulée ou expirée reste bloquante.
        gh = InheritedGh(self.rollup(extra=[check("psycopg 2/8", "CANCELLED",
                                                  job=126)]))
        self.assertEqual(self.check(gh)["result"], "pending")

    def test_a_skipped_part_does_not_block(self):
        gh = InheritedGh(self.rollup(extra=[check("lecteurs de la doc", "SKIPPED",
                                                  job=127)]))
        self.assertEqual(self.check(gh)["result"], "merged")

    def test_a_waiting_part_blocks(self):
        gh = InheritedGh(self.rollup(extra=[pending("psycopg 3/8", job=128)]))
        self.assertEqual(self.check(gh)["result"], "pending")

    def test_a_dry_run_merges_nothing_and_writes_nothing(self):
        gh = InheritedGh(self.rollup())
        out = self.check(gh, dry_run=True)
        self.assertEqual(out["result"], "would-merge", out)
        self.assertIn("inherited", out)
        self.assertEqual(gh.landed, [])
        self.assertEqual(self.created, [])
        self.assertEqual(self.merged_notes(), [])


if __name__ == "__main__":
    unittest.main()
