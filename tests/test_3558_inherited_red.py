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
une base sans relevé conclu → `pending`. Les relectures B/P1 du 25/10 ajoutent
les fermetures du relevé : ligne encore `running`, attempt différent du flux,
dernier échec publié après la passe du veilleur, et provenance homonyme d'un
autre run (jamais masquée par le seul nom).
"""
from __future__ import annotations

import contextlib
import json
import types
import unittest
from unittest import mock

from ameesh import packet_automerge, storage, work

from .test_2813_packet_auto_merge import (APP, HEAD, LOT, NUM, SLUG,  # noqa: E402
                                         FakeGh, Journal)

BASE_RUN = 300
BASE_SHA = "9" * 40
RUN = 200
FAIL_TESTS = ("tests.test_a", "tests.test_b", "tests.test_c")


def event(test, *, part="psql 1/16", attempt=1, run=RUN, statut="echec"):
    """Une ligne `test` publiée dans la sortie d'un flux (`ci-flow.py`)."""
    return {"v": 1, "type": "test", "execution": run, "attempt": attempt,
            "part": part, "test": test, "statut": statut, "module": "",
            "extrait": "", "rejouer": "", "duree_s": 1.0,
            "ts": "2026-10-25T12:00:00Z"}


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
                           "status": "failure", "run_attempt": 1,
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


def _check_run(name, conclusion, *, details_url, suite, started):
    """Un check run REST de la tête, tel que `commits/<sha>/check-runs`."""
    return {"name": name, "status": "completed", "conclusion": conclusion,
            "head_sha": HEAD, "app": {"id": APP},
            "check_suite": {"id": suite}, "started_at": started,
            "details_url": details_url,
            "output": {"text": ""}}


class InheritedGh(FakeGh):
    """Le faux `gh` avec le rollup exact de la tête (tous workflows), l'état
    frais de l'exécution et les événements terminaux de ses flux."""

    def __init__(self, entries, **kwargs):
        super().__init__(tests="failure", **kwargs)
        self.entries = list(entries)
        #: l'état frais de `actions/runs/<RUN>` : conclu en échec, attempt 1
        self.run_state = {"id": RUN, "head_sha": HEAD, "status": "completed",
                          "conclusion": "failure", "run_attempt": 1}
        #: les événements terminaux relus ; None : ceux des lignes du faux CI
        self.events = None
        #: `total_count` de la page REST (un au-delà = lecture tronquée)
        self.check_runs_total = None
        #: des checks REST supplémentaires (provenance d'une autre suite/run)
        self.check_runs_extra = []

    def check_runs(self):
        runs = [_check_run(
            "tests", "failure",
            details_url="https://github.com/%s/actions/runs/%d/job/123" % (SLUG, RUN),
            suite=200, started="2026-10-25T12:00:00Z")]
        by_part = {}
        for ev in self.events or ():
            by_part.setdefault(str(ev.get("part") or ""), []).append(ev)
        for index, (part, events) in enumerate(sorted(by_part.items()), start=1):
            run = _check_run(
                "tests (flux) %s" % part, "failure",
                details_url="https://github.com/%s/runs/%d" % (SLUG, 121135624400 + index),
                suite=200 + index, started="2026-10-25T12:01:00Z")
            run["output"] = {"text": "\n".join(json.dumps(ev) for ev in events)}
            runs.append(run)
        runs += [dict(run) for run in self.check_runs_extra]
        return runs

    def answer(self, args):
        path = args[-1] if args else ""
        if list(args[:2]) == ["pr", "view"]:
            self.calls.append(list(args))
            return 0, json.dumps({
                "state": self.state, "headRefOid": self.head,
                "headRefName": self.branch, "baseRefName": self.base,
                "isDraft": False, "autoMergeRequest": None,
                "statusCheckRollup": self.entries, "title": "packet"}), ""
        if args[:1] == ["api"] and path.endswith("/actions/runs/%d" % RUN):
            self.calls.append(list(args))
            return 0, json.dumps(self.run_state), ""
        if args[:1] == ["api"] and "/commits/" in path and "/check-runs" in path:
            self.calls.append(list(args))
            runs = self.check_runs()
            total = len(runs) if self.check_runs_total is None else self.check_runs_total
            page = {"total_count": total, "check_runs": runs}
            # `_head_check_runs` lit `--paginate --slurp` : une liste de pages ;
            # la relecture des événements lit la page du veilleur, seule.
            return 0, json.dumps([page] if "--paginate" in args else page), ""
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

    def armed_for(self, *names):
        """Arme la même tête avec une liste de checks requis (le journal est
        la seule source de l'armement, #2813)."""
        self._note(None, LOT, "auto-merge armed repo=%s pr=%d sha=%s base=main "
                              "method=merge required=%s — test"
                  % (SLUG, NUM, HEAD, ",".join(names)))

    def lint_protection(self):
        return {"required_status_checks": {"strict": False,
                                           "contexts": ["tests", "lint"]}}

    def check(self, gh, **kwargs):
        if getattr(gh, "events", 0) is None:
            attempt = int(gh.run_state.get("run_attempt") or 1)
            gh.events = [event(str(row.get("test")),
                               part=str(row.get("part") or ""), attempt=attempt)
                         for row in self.ci.rows.get(RUN, ()) if row.get("test")]
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

    def test_a_line_still_running_is_not_a_complete_reading(self):
        # #3558 (ameesh-dev2 B/P1) : le veilleur CI et ce passage tournent
        # séparément. La ligne n'a pas encore conclu : un échec propre publié
        # après sa dernière passe n'est pas en base, et la part rouge du flux
        # le masquerait. La ligne doit être conclue `failure`.
        self.ci.runs[RUN]["status"] = "running"
        gh = InheritedGh(self.rollup())
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.landed, [])
        self.assertEqual(self.created, [])

    def test_a_flux_run_concluded_on_another_attempt_blocks(self):
        # #3558 (ameesh-cx3 B/P1) : la ligne est restée sur l'attempt 1 tandis
        # que la forge a conclu un attempt 2. Le flux lu est celui de l'attempt
        # 2 : son rouge ne se prouve pas avec les échecs de l'attempt 1.
        gh = InheritedGh(self.rollup())
        gh.run_state["run_attempt"] = 2
        gh.events = [event(test, attempt=1) for test in FAIL_TESTS]
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.landed, [])
        self.assertEqual(self.created, [])

    def test_a_last_failure_published_after_the_watcher_pass_blocks(self):
        # #3558 (ameesh-cx3 B/P1) : la ligne est conclue `failure` mais sa
        # dernière passe n'a lu que les échecs hérités ; le flux terminal porte
        # en plus un échec propre. Fusionner masquerait ce dernier échec.
        gh = InheritedGh(self.rollup())
        gh.events = [event(test) for test in FAIL_TESTS] + [event("tests.test_new")]
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.landed, [])
        self.assertEqual(self.created, [])

    def test_a_truncated_terminal_read_blocks(self):
        # La page de check runs du veilleur est tronquée (`total_count` plus
        # grand) : elle ne prouve pas que tous les flux ont été lus. La porte
        # ne suppose pas la complétude d'une lecture partielle.
        gh = InheritedGh(self.rollup())
        gh.check_runs_total = 999
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.landed, [])

    def test_a_same_app_failure_of_another_run_is_not_masked_by_name(self):
        # #3558 (ameesh-cx3 B/P1) : la provenance REST porte un second `tests`
        # FAILURE de la même application requise, mais d'une autre suite/run
        # (999) absente du rollup prouvé. L'exemption ne vaut que pour
        # l'identité exacte (nom, exécution) : ce rouge reste non prouvé.
        gh = InheritedGh(self.rollup())
        gh.check_runs_extra = [_check_run(
            "tests", "failure",
            details_url="https://github.com/%s/actions/runs/999/job/999" % SLUG,
            suite=999, started="2026-10-25T12:30:00Z")]
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.landed, [])
        self.assertEqual(self.created, [])

    def test_a_required_red_of_the_proven_run_is_masked_by_its_exact_identity(self):
        # La contrepartie : la provenance du run prouvé (200) est bien
        # masquée, y compris avec son application requise. Le second run plus
        # récent (999) de l'autre témoin n'est pas là.
        gh = InheritedGh(self.rollup())
        out = self.check(gh)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", HEAD)])

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

    def test_a_cancelled_flux_part_blocks(self):
        # #3558 (ameesh-dev4 B/P1) : le runner meurt après un échec hérité,
        # sans ligne `fin` ; le flux de la part conclut CANCELLED. Ce non
        # concluant ne prouve pas la fin du flux, et le job rouge serait masqué
        # par la part lue. Le flux doit être présent et FAILURE.
        entries = [entry for entry in self.rollup()
                   if entry["name"] != "tests (flux) psql 1/16"]
        entries.append(flux("tests (flux) psql 1/16", "CANCELLED"))
        gh = InheritedGh(entries)
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.landed, [])
        self.assertEqual(self.created, [])

    def test_a_red_required_check_without_its_own_test_blocks(self):
        # #3558 (ameesh-cx1 B/P1) : `tests` et `lint` sont requis, tous deux
        # rouges ; seul `psql 1/16` a un test lu et hérité. L'agrégat `tests`
        # s'exempte, pas `lint` : un check requis rouge sans sa propre preuve
        # ne se masque jamais en vert.
        self.armed_for("tests", "lint")
        gh = InheritedGh(self.rollup(extra=[check("lint", "FAILURE", job=125)]),
                         protection=self.lint_protection())
        out = self.check(gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.landed, [])
        self.assertEqual(self.created, [])

    def test_a_red_required_check_with_its_own_test_merges(self):
        # La contrepartie du témoin ci-dessus : un check requis non agrégat
        # rouge et prouvé par sa part (test lu, hérité, flux FAILURE) passe.
        # L'exemption est réservée à l'agrégat, pas un refus de tout autre.
        self.armed_for("tests", "lint")
        self.ci.rows[RUN].append({"part": "lint", "test": "tests.test_lint",
                                  "inherited": True})
        self.ci.reference["tests"].append("tests.test_lint")
        entries = self.rollup(extra=[check("lint", "FAILURE", job=125),
                                     flux("tests (flux) lint", "FAILURE")])
        gh = InheritedGh(entries, protection=self.lint_protection())
        out = self.check(gh)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", HEAD)])
        self.assertIn("lint", out["inherited"]["parts"])
        self.assertIn("merged over inherited red: lint, psql 1/16",
                      self.merged_notes()[0])

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
