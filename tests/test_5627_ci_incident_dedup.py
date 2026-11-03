# SPDX-License-Identifier: AGPL-3.0-only
"""#5627 — générateur de lots d'incident CI : un lot par run, rien pour une
annulation volontaire ni pour un rouge déjà porté par un correctif.

Constat du 03/11 :

- la release 1.35.0 a ouvert 18 lots (#5608 à #5625), un par check annulé,
  pour un seul run (psql-nocturne 41609736877), annulé exprès par le bureau ;
- l'incident « CI main rouge » a ouvert #5600 puis #5626 alors que les
  correctifs #5599 et #5606 portaient déjà le test rouge.

Témoins : le regroupement par run (`ci_reading`, `correction_groups`,
`_open_correction`), l'annulation tracée (`deliberate_cancel`) avec ses
négatifs (autre run, note sans motif, demande échouée), et la couverture d'un
test rouge par un lot ouvert (`incident.covering_lots`, `consume`) avec les
siens (test non couvert, couverture partielle, autre projet, clé courte).
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import incident, release_cutoff

from . import test_2448_release_cutoff as fixture
from .test_3209_skipped_by_construction import _Lots

RUN = 41609736877
URL = "https://github.com/manaty/ameesh/actions/runs/%d" % RUN
JOBS = ["suite psql complète (nocturne) / psql %d/16" % n for n in range(1, 17)] \
    + ["suite psql complète (nocturne) / bilan des impactés"]


def rollup(**over):
    out = {"green": False, "acceptable": False, "runs": 1, "negative": [],
           "pending": [], "failed": [], "inconclusive": [], "missing": []}
    out.update(over)
    return out


def nightly_suite():
    """La tête de 1.35.0 : `tests` vert, le run nocturne annulé en entier."""
    tests = fixture._run(600, [fixture._job("plan"), fixture._job("tests")])
    nightly = fixture._run(RUN, [fixture._job(name, "cancelled") for name in JOBS],
                           workflow="psql-nocturne", event="schedule",
                           conclusion="cancelled")
    return {"runs": [tests, nightly]}


def plan_of(reading):
    return types.SimpleNamespace(head="6" * 40, version="1.35.0", ci=reading)


class _Events:
    """`storage.work.events_noted` : les notes de lot qui commencent par un préfixe."""

    def __init__(self, *notes):
        self.notes = [{"work_item_id": 5605, "actor": "bureau", "note": n} for n in notes]

    def events_noted(self, prefix, since_ts, ids=None):
        return [row for row in self.notes if row["note"].startswith(prefix)]


class OneLotPerRunTest(unittest.TestCase):
    """Un run annulé de 17 jobs ouvre un lot, pas dix-huit."""

    project = release_cutoff.ProjectRelease("ameesh", "/unused", github="acme/project")

    def setUp(self):
        self.lots = _Lots(self)

    def read(self):
        with mock.patch.object(release_cutoff, "required_checks", return_value=()), \
                mock.patch.object(release_cutoff.radar, "commit_ci",
                                  return_value=rollup(inconclusive=["psql-nocturne"])), \
                mock.patch.object(release_cutoff, "suite_reading",
                                  return_value=nightly_suite()):
            return release_cutoff.ci_reading(self.project, "6" * 40)

    def test_the_reading_maps_every_check_to_its_run(self):
        reading = self.read()
        self.assertEqual(len(reading["inconclusive"]), 18)
        runs = reading["inconclusive_runs"]
        self.assertEqual(set(runs), set(reading["inconclusive"]))
        self.assertEqual({row["id"] for row in runs.values()}, {RUN})
        self.assertEqual(runs["psql-nocturne"]["conclusion"], "cancelled")

    def test_one_lot_lists_the_checks_of_the_run(self):
        plan = plan_of(self.read())
        release_cutoff._open_correction(object(), self.project, plan)
        self.assertEqual(len(self.lots.rows), 1, [r["title"] for r in self.lots.rows])
        lot = self.lots.rows[0]
        self.assertIn("run %d" % RUN, lot["title"])
        self.assertIn("18 check(s)", lot["title"])
        self.assertEqual(lot["source"], "nonconclusive:ameesh:run:psql-nocturne")
        for name in JOBS:
            self.assertIn(name, lot["body"])

    def test_the_next_run_of_the_workflow_is_noted_on_the_open_lot(self):
        plan = plan_of(self.read())
        release_cutoff._open_correction(object(), self.project, plan)
        later = dict(plan.ci, inconclusive_runs={
            text: dict(run, id=RUN + 1) for text, run in plan.ci["inconclusive_runs"].items()})
        release_cutoff._open_correction(object(), self.project, plan_of(later))
        self.assertEqual(len(self.lots.rows), 1)
        self.assertEqual(len(self.lots.notes), 1)
        self.assertIn("run %d" % (RUN + 1), self.lots.notes[0][1])

    def test_a_check_without_a_known_run_keeps_its_own_lot(self):
        plan = plan_of({"inconclusive": ["ci/externe (ERROR)", "autre (NEUTRAL)"]})
        release_cutoff._open_correction(object(), self.project, plan)
        self.assertEqual(len(self.lots.rows), 2)

    def test_a_run_with_a_single_check_keeps_the_shared_check_key(self):
        text = "psql 13/16 (cancelled; %s)" % URL
        plan = plan_of({"inconclusive": [text], "inconclusive_runs": {
            text: {"id": RUN, "url": URL, "workflow": "tests", "conclusion": "success"}}})
        release_cutoff._open_correction(object(), self.project, plan)
        self.assertEqual(self.lots.rows[0]["source"],
                         release_cutoff.radar.nonconclusive_source("ameesh", text))


class DeliberateCancelTest(unittest.TestCase):
    """Une annulation volontaire et tracée n'ouvre aucun lot ; elle est rapportée."""

    def plan(self):
        names = ["psql-nocturne"] + ["%s (cancelled; %s)" % (name, URL) for name in JOBS]
        run = {"id": RUN, "url": URL, "workflow": "psql-nocturne", "conclusion": "cancelled"}
        return plan_of({"inconclusive": names, "inconclusive_runs": {n: run for n in names}})

    def groups(self, events):
        with mock.patch.object(release_cutoff.storage, "of",
                               return_value=types.SimpleNamespace(work=events)):
            return release_cutoff.correction_groups(self.plan(), object())

    def test_a_traced_manual_cancel_opens_nothing_and_is_reported(self):
        groups = self.groups(_Events(
            "ci-cancel: run %d — cible réservée au paquet #5605 (§12)" % RUN))
        self.assertEqual(len(groups), 1)
        self.assertIn("cible réservée", groups[0]["cancelled"])
        lots = _Lots(self)
        release_cutoff._open_correction(object(), types.SimpleNamespace(name="ameesh"),
                                        self.plan(), groups)
        self.assertEqual(lots.rows, [])
        lines = release_cutoff._cancel_deviation(self.plan(), groups)
        self.assertEqual(len(lines), 1)
        self.assertIn("annulé volontairement", lines[0])
        self.assertIn(str(RUN), lines[0])

    def test_the_packet_porter_trace_counts(self):
        groups = self.groups(_Events(
            "packet portier: cancel run %d (%s) requested" % (RUN, URL)))
        self.assertTrue(groups[0]["cancelled"])

    def test_negative_an_untraced_cancel_still_opens_one_lot(self):
        groups = self.groups(_Events())
        self.assertEqual(groups[0]["cancelled"], "")
        lots = _Lots(self)
        release_cutoff._open_correction(object(), types.SimpleNamespace(name="ameesh"),
                                        self.plan(), groups)
        self.assertEqual(len(lots.rows), 1)

    def test_negative_another_run_with_the_same_digits_does_not_count(self):
        groups = self.groups(_Events("ci-cancel: run %d0 — autre run" % RUN))
        self.assertEqual(groups[0]["cancelled"], "")

    def test_negative_a_trace_without_a_motive_does_not_count(self):
        groups = self.groups(_Events("ci-cancel: run %d —" % RUN))
        self.assertEqual(groups[0]["cancelled"], "")

    def test_negative_a_failed_porter_cancel_does_not_count(self):
        groups = self.groups(_Events("packet portier: cancel run %d: HTTP 409" % RUN))
        self.assertEqual(groups[0]["cancelled"], "")

    def test_negative_a_failed_run_is_never_a_cancel(self):
        plan = self.plan()
        for run in plan.ci["inconclusive_runs"].values():
            run["conclusion"] = "timed_out"
        events = _Events("ci-cancel: run %d — motif" % RUN)
        with mock.patch.object(release_cutoff.storage, "of",
                               return_value=types.SimpleNamespace(work=events)):
            groups = release_cutoff.correction_groups(plan, object())
        self.assertEqual(groups[0]["cancelled"], "")


class _Work:
    """`storage.work` : les lots par état, l'identité d'incident par source."""

    def __init__(self, rows):
        self.rows = list(rows)

    def items(self, *, state, assignee, limit):
        return [row for row in self.rows if row["state"] == state][:limit]

    def by_source(self, source):
        return [row for row in self.rows if row.get("source") == source]

    def by_issue_ref(self, ref):
        return [row for row in self.rows if row.get("issue_ref") == ref]


class _Ci:
    def __init__(self, run_id, modules):
        self.run_id, self.modules = run_id, list(modules)

    def latest_concluded(self, branches):
        return [{"repo": "manaty/ameesh", "branch": "main", "run_id": self.run_id,
                 "head_sha": "6" * 40, "workflow": "tests.yml", "status": "failure",
                 "url": "https://exemple/%d" % self.run_id}]

    def failures(self, repo, run_id):
        return [{"module": m, "test": "%s.Classe.test_x" % m} for m in self.modules]

    def head_replaced(self, *a):
        return False


def lot(lot_id, title, body="", state="intake", app="ameesh", **over):
    row = {"id": lot_id, "title": title, "body": body, "state": state, "app": app,
           "source": "", "issue_ref": ""}
    row.update(over)
    return row


FIX_5606 = lot(5606, "Test #5582 : seuil de 100 ms dépassé sous charge CI",
               "Constat : tests.test_5582_rattrapage_relecture.ReviewCatchUpPlanDbTest"
               ".test_le_plan échoue dans la CI de main 619ff3d4.")


class CoveredRedTest(unittest.TestCase):
    """Un test rouge déjà porté par un lot ouvert n'ouvre pas d'incident."""

    def consume(self, rows, modules, run_id=41609560517):
        store = types.SimpleNamespace(work=_Work(rows), ci=_Ci(run_id, modules))
        with mock.patch.object(incident.storage, "of", return_value=store), \
                mock.patch.object(incident.work, "add",
                                  return_value={"id": 9999}) as add, \
                mock.patch.object(incident.work, "note") as note, \
                mock.patch.object(incident.work, "assign"):
            out = incident.consume(object(), lands=lambda *a: [],
                                   integration={"manaty/ameesh": ("main", "ameesh")})
        return out, add, note

    def test_the_5626_case_notes_the_open_fix(self):
        out, add, note = self.consume([FIX_5606], ["tests/test_5582_rattrapage_relecture.py"])
        add.assert_not_called()
        self.assertEqual(out[0]["covered"], [5606])
        self.assertIn("#5606", out[0]["why"])
        note.assert_called_once()
        self.assertEqual(note.call_args.args[1], 5606)
        self.assertIn("41609560517", note.call_args.args[2])
        self.assertTrue(note.call_args.kwargs.get("once"))

    def test_a_dotted_module_and_a_footprint_cover_too(self):
        rows = [lot(5599, "Test #5587 : remplissage annulé")]
        store = types.SimpleNamespace(
            work=_Work(rows), ci=_Ci(1, ["tests.test_5587_tmp_path_filter"]),
            work_footprints=types.SimpleNamespace(for_items=lambda ids: [
                {"work_item_id": 5599, "path": "tests/test_5587_tmp_path_filter.py"}]))
        self.assertEqual(
            set(incident.covering_lots(store, app="ameesh",
                                       modules=["tests.test_5587_tmp_path_filter"])),
            {"test_5587_tmp_path_filter"})

    def test_a_closed_lot_superseded_by_an_open_one_covers(self):
        closed = lot(5600, "INCIDENT CI main rouge — run 41598021698",
                     "Fichiers de test en échec :\n- `test_5587_tmp_path_filter`",
                     state="closed", superseded_by=5599)
        rows = [closed, lot(5599, "Test #5587 : remplissage annulé (paquet #5596)")]
        out, add, _note = self.consume(rows, ["test_5587_tmp_path_filter"])
        add.assert_not_called()
        self.assertEqual(out[0]["covered"], [5599])

    def test_negative_an_uncovered_red_opens_the_incident(self):
        out, add, note = self.consume([FIX_5606], ["tests/test_9999_autre_chose.py"])
        add.assert_called_once()
        note.assert_not_called()
        self.assertTrue(out[0]["created"])

    def test_negative_a_partial_cover_still_opens_the_incident(self):
        out, add, _note = self.consume(
            [FIX_5606], ["tests/test_5582_rattrapage_relecture.py",
                         "tests/test_9999_autre_chose.py"])
        add.assert_called_once()

    def test_negative_a_lot_of_another_project_does_not_cover(self):
        other = dict(FIX_5606, app="nexlink")
        out, add, _note = self.consume([other], ["tests/test_5582_rattrapage_relecture.py"])
        add.assert_called_once()

    def test_negative_a_delivered_fix_does_not_cover(self):
        delivered = dict(FIX_5606, state="delivered")
        out, add, _note = self.consume([delivered],
                                       ["tests/test_5582_rattrapage_relecture.py"])
        add.assert_called_once()

    def test_negative_a_longer_name_is_not_a_mention(self):
        near = lot(5700, "Test test_5582_rattrapage_relecture_bis : autre chose")
        out, add, _note = self.consume([near], ["tests/test_5582_rattrapage_relecture.py"])
        add.assert_called_once()

    def test_negative_a_short_key_is_never_searched(self):
        self.assertEqual(incident.test_key("mobile/src/a.test.ts"), "a.test")
        store = types.SimpleNamespace(work=_Work([lot(1, "a.test cassé")]))
        self.assertEqual(incident.covering_lots(store, app="ameesh",
                                                modules=["mobile/src/a.test.ts"]), {})

    def test_negative_an_unreadable_store_covers_nothing(self):
        def broken(**_kw):
            raise RuntimeError("base indisponible")
        store = types.SimpleNamespace(work=types.SimpleNamespace(items=broken))
        self.assertEqual(incident.covering_lots(
            store, app="ameesh", modules=["tests/test_5582_rattrapage_relecture.py"]), {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
