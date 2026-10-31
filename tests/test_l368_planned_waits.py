# SPDX-License-Identifier: AGPL-3.0-only
"""L368: planned waits suppress stale alerts consistently across all views."""
from __future__ import annotations

import datetime
import os
import re
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

from ameesh import (mesh_cli, plan, planned_waits as W, progress,
                    registry, roadmap, stagnation, storage, work, work_dependencies)
from ameesh import work_dependencies as D
from ameesh.storage import interface as SI
from ameesh.storage.postgres.progress import Progress
from tests.support import PgTestCase


class PacketTrailersTest(unittest.TestCase):
    """#2599 : l'appartenance d'un porteur se lit aussi dans les trailers.

    Le corps du porteur peut manquer la liste « #N <tête> » ; chaque commit
    de composition porte `ameesh-work: <lot>`. On ne lit que les commits
    au-dessus de la cible, jamais ceux hérités de `main`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.write("README", "base\n")
        self.git("add", "README")
        self.git("commit", "-q", "-m", "base")

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
             *args], cwd=self.repo, check=True, capture_output=True, text=True,
            env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1", HOME=self.repo)).stdout

    def write(self, name, text):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, message):
        self.write("note", message + "\n")
        self.git("add", "note")
        self.git("commit", "-q", "-m", message)

    def packet_branch(self):
        """Une cible, un trailer hérité, puis une branche de porteur."""
        self.commit("vehicule 2200 : #9999 abcdef0\n\nameesh-work: 9999")
        self.git("checkout", "-q", "-b", "integration/paquet-test")
        self.commit("vehicule 2300 : #1234 abcdef0\n\nameesh-work: 1234")
        self.commit("vehicule 2300 : #5678 abcdef0\n\nameesh-work: 5678")
        # un lot cité en prose, sans trailer, n'est pas une unité
        self.commit("paquet ; #2203 exclu, #1976 admis mais exclu")
        return self.git("rev-parse", "HEAD").strip()

    def test_reads_the_trailers_above_the_target(self):
        head = self.packet_branch()
        carrier = {"branch": "integration/paquet-test", "branch_head": head}
        self.assertEqual(W.packet_trailers(self.repo, carrier, "main"), {1234, 5678})
        # la tête observée reste lisible quand la branche locale a disparu
        self.git("checkout", "-q", "main")
        self.git("branch", "-q", "-D", "integration/paquet-test")
        self.assertEqual(W.packet_trailers(self.repo, carrier, "main"), {1234, 5678})

    def test_an_unreadable_branch_is_not_a_membership(self):
        self.assertIsNone(W.packet_trailers(
            self.repo, {"branch": "integration/absente"}, "main"))

    def test_published_target_excludes_history_when_local_main_is_stale(self):
        head = self.packet_branch()
        self.git("remote", "add", "origin", self.repo)
        self.git("update-ref", "refs/remotes/origin/main", "main")
        # Le clone partagé a fetché main sans déplacer sa branche locale.
        self.git("branch", "-f", "main", "main~1")
        carrier = {"branch": "integration/paquet-test", "branch_head": head}
        self.assertEqual(W.packet_trailers(self.repo, carrier, "main"), {1234, 5678})

    def test_published_carrier_excludes_units_dropped_since_local_branch(self):
        head = self.packet_branch()
        self.git("remote", "add", "origin", self.repo)
        self.git("checkout", "-q", "-b", "recomposed", "main")
        self.commit("vehicule : #5678 abcdef0\n\nameesh-work: 5678")
        self.git("update-ref", "refs/remotes/origin/integration/paquet-test", "HEAD")
        carrier = {"branch": "integration/paquet-test", "branch_head": head}
        self.assertEqual(W.packet_trailers(self.repo, carrier, "main"), {5678})

    def test_attachments_come_from_the_project_repo(self):
        head = self.packet_branch()
        cfg = types.SimpleNamespace(radar={"projects": {
            "ameesh": {"repo": self.repo, "target": "main"}}})
        carrier = {"id": 9001, "state": "qa", "app": "ameesh",
                   "branch": "integration/paquet-test", "branch_head": head}
        self.assertEqual(W.packet_attachments(cfg, [carrier]),
                         {9001: {1234, 5678}})

    def test_without_a_project_repo_nothing_is_attached(self):
        carrier = {"id": 9001, "state": "qa", "app": "ameesh",
                   "branch": "integration/paquet-test"}
        self.assertEqual(W.packet_attachments(None, [carrier]), {})
        self.assertEqual(W.packet_attachments(types.SimpleNamespace(radar={}), [carrier]), {})

    def test_a_registry_clone_replaces_a_missing_project_repo(self):
        # #2837 : l'hôte qui lit les alertes n'a pas forcément les réglages
        # radar ; un clone d'agent qui porte la tête du porteur suffit à lire
        # la composition, donc à taire le SLO des unités du paquet.
        head = self.packet_branch()
        carrier = {"id": 9001, "state": "qa", "app": "ameesh",
                   "branch": "integration/paquet-test", "branch_head": head}
        db = types.SimpleNamespace()
        with mock.patch.object(registry, "overview",
                               return_value=[{"cwd": self.repo}]):
            self.assertEqual(W.packet_attachments(None, [carrier], db=db),
                             {9001: {1234, 5678}})

    def test_registry_clone_reads_the_observed_head_not_a_stale_published_ref(self):
        # #2837 : un clone du registre peut garder une ref publiée en retard.
        # La tête observée (`branch_head`) fait foi : un lot retiré de la
        # recomposition ne reste pas attaché au véhicule.
        old = self.packet_branch()
        self.git("remote", "add", "origin", self.repo)
        self.git("update-ref", "refs/remotes/origin/main", "main")
        self.git("update-ref", "refs/remotes/origin/integration/paquet-test", old)
        self.git("checkout", "-q", "-b", "observed", "main")
        self.commit("vehicule : #5678 abcdef0\n\nameesh-work: 5678")
        head = self.git("rev-parse", "HEAD").strip()
        carrier = {"id": 9001, "state": "qa", "app": "ameesh",
                   "branch": "integration/paquet-test", "branch_head": head}
        with mock.patch.object(registry, "overview",
                               return_value=[{"cwd": self.repo}]):
            self.assertEqual(W.packet_attachments(None, [carrier],
                                                  db=types.SimpleNamespace()),
                             {9001: {5678}})

    def test_a_registry_clone_without_the_carrier_head_attaches_nothing(self):
        self.packet_branch()
        carrier = {"id": 9001, "state": "qa", "app": "ameesh",
                   "branch": "integration/absente", "branch_head": "0" * 40}
        with mock.patch.object(registry, "overview",
                               return_value=[{"cwd": self.repo}]):
            self.assertEqual(
                W.packet_attachments(None, [carrier], db=types.SimpleNamespace()), {})

    def test_the_dry_pass_is_not_read(self):
        # la passe à blanc de db.batched ne fait aucune I/O (même règle que
        # la sonde pgbouncer, #2582)
        from ameesh import db as db_mod

        self.assertTrue(W._dry_pass(mock.Mock(spec=db_mod._Recorder)))
        self.assertFalse(W._dry_pass(object()))

    def test_prose_only_is_not_an_attached_unit(self):
        rows = [{"id": 1234, "state": "qa"}, {"id": 2203, "state": "qa"}]
        carrier = {"id": 9001, "state": "qa", "branch": "integration/paquet-test",
                   "body": "paquet ; #2203 exclu (verdict manquant)"}
        waits = W.packet_waits(rows, rows + [carrier], attachments={9001: {1234}})
        self.assertEqual(list(waits), [1234])


class UnitTest(unittest.TestCase):
    now = plan.day_start_ts("2026-10-12") + 12 * 3600
    rows = [{"id": 1, "state": "qa", "package_id": "child"}]
    packages = {"child": {"id": "child", "parent": "epic", "present": True},
                "epic": {"id": "epic", "present": True},
                "prerequisite": {"id": "prerequisite", "present": True}}

    def resolve(self, *, dates=None, commitments=(), items=(), rows=None, now=None,
                landings=(), holds=None, decisions=()):
        return W.resolve(rows or self.rows, dates or {}, commitments, self.packages,
                         items, now=self.now if now is None else now,
                         landings=landings, holds=holds, decisions=decisions)

    def test_decision_request_waits_until_its_deadline(self):
        # #2378 : demande ouverte non échue → attente prévue jusqu'à l'échéance
        rows = [{"id": 1, "state": "waiting_human"}]
        open_ = {"id": 7, "lot": 1, "owner": "human:o", "due_ts": self.now + 3600}
        wait = self.resolve(rows=rows, decisions=[open_])[1]
        self.assertEqual((wait["what"], wait["decision"], wait["owner"]),
                         ("decision", 7, "human:o"))
        self.assertEqual(wait["due_ts"], round(self.now + 3600, 3))
        # échue, sans échéance, ou une des deux échue : le signal revient
        late = dict(open_, id=8, due_ts=self.now - 1)
        self.assertEqual(self.resolve(rows=rows, decisions=[late]), {})
        self.assertEqual(self.resolve(rows=rows, decisions=[dict(open_, due_ts=None)]), {})
        self.assertEqual(self.resolve(rows=rows, decisions=[open_, late]), {})
        # la plus proche échéance fait la fin de l'attente
        later = dict(open_, id=9, due_ts=self.now + 7200)
        self.assertEqual(self.resolve(rows=rows, decisions=[later, open_])[1]["decision"], 7)
        # seul un lot `waiting_human` attend son décideur ; un autre lot n'en dépend pas
        self.assertEqual(self.resolve(decisions=[open_]), {})
        self.assertEqual(self.resolve(rows=rows, decisions=[dict(open_, lot=2)]), {})
        # un hold du pilote reste prioritaire
        held = {1: {"what": "hold", "by": "orch", "since": 1.0}}
        self.assertEqual(self.resolve(rows=rows, decisions=[open_], holds=held)[1]["what"],
                         "hold")

    def test_future_wake_without_hold_expires_at_deadline(self):
        row = dict(id=1, state="build", wake_ts=self.now + 3600)
        wait = self.resolve(rows=[row])[1]
        self.assertEqual(wait["what"], "scheduled")
        self.assertEqual(wait["wake_ts"], row["wake_ts"])
        self.assertTrue(D.wait_label(wait).startswith("scheduled wake "))
        for wake in (self.now, self.now - 1, None):
            self.assertEqual(self.resolve(rows=[dict(row, wake_ts=wake)]), {})

    def test_future_start_ends_at_local_midnight_without_changing_activity(self):
        dates = {1: {"planned_start": "2026-10-13"}}
        self.assertEqual(self.resolve(dates=dates), {1: {"what": "scheduled", "start": "2026-10-13"}})
        self.assertEqual(self.resolve(dates=dates, now=plan.day_start_ts("2026-10-13")), {})
        for start in (None, "", "invalid", "2026-10-12", "2026-10-11"):
            self.assertEqual(self.resolve(dates={1: {"planned_start": start}}), {})

    def test_only_explicit_open_lots_block(self):
        for reference in ("#2", "2", "lot 2", "lot #2", "task #2", "TASK 2"):
            with self.subTest(reference=reference):
                commitment = {"status": "open", "work_item_id": 1, "depends_on": [reference]}
                self.assertEqual(self.resolve(commitments=[commitment], items=[{"id": 2, "state": "qa"}]),
                                 {1: {"what": "dependency", "lots": [2]}})
                for state in ("merged", "promoted", "closed"):
                    self.assertEqual(self.resolve(commitments=[commitment], items=[{"id": 2, "state": state}]), {})
        for reference in ("after #2 merges", "main carries abc123", "#999", "#1", "e2", None):
            self.assertEqual(self.resolve(commitments=[{"status": "open", "work_item_id": 1,
                                                       "depends_on": [reference]}],
                                          items=[{"id": 1, "state": "build"}, {"id": 2, "state": "qa"}]), {})

    def test_proposals_cancelled_and_unrelated_commitments_do_not_suppress(self):
        for status, lot in (("proposed", 1), ("cancelled", 1), ("done", 1), ("open", 3)):
            self.assertEqual(self.resolve(commitments=[{"status": status, "work_item_id": lot,
                                                       "depends_on": ["#2"]}],
                                          items=[{"id": 2, "state": "build"}]), {})

    def test_plan_dependency_inherits_through_ancestor_and_resolves_package(self):
        c = {"status": "open", "package_id": "epic", "depends_on": ["prerequisite"]}
        items = [{"id": 2, "state": "build", "package_id": "prerequisite"},
                 {"id": 3, "state": "merged", "package_id": "prerequisite"}]
        self.assertEqual(self.resolve(commitments=[c], items=items), {1: {"what": "dependency", "lots": [2]}})
        for reference in ("lot prerequisite", "package prerequisite", "WorkPackage prerequisite"):
            self.assertEqual(self.resolve(commitments=[dict(c, depends_on=[reference])], items=items),
                             {1: {"what": "dependency", "lots": [2]}})
        self.assertEqual(self.resolve(commitments=[c], items=[]), {})
        self.assertEqual(self.resolve(commitments=[c], items=items,
                                      rows=[{"id": 4, "state": "build", "package_id": "other"}]), {})
        packages = {"a": {"parent": "b"}, "b": {"parent": "a"}}
        self.assertEqual(list(W.ancestors("a", packages)), ["a", "b"])
        self.assertEqual(list(W.ancestors("retired", {"retired": {"present": False}})), [])

    def test_plan_start_inherits_and_explicit_lot_start_overrides_it(self):
        packages = {k: dict(v) for k, v in self.packages.items()}
        packages["epic"]["planned_start"] = "2026-10-13"
        self.assertEqual(W.resolve(self.rows, {}, [], packages, [], now=self.now),
            {1: {"what": "scheduled", "start": "2026-10-13", "package": "epic"}})
        self.assertEqual(W.resolve(self.rows, {1: {"planned_start": "2026-10-12"}}, [],
                                    packages, [], now=self.now), {})

    def test_external_gate_holds_until_the_end_of_the_due_day(self):
        commitment = {"id": 7, "status": "open", "work_item_id": 1, "due_on": "2026-10-13",
                      "depends_on": ["gate:liquichain-window"]}
        expected = {1: {"what": "hold", "gate": "liquichain-window", "until": "2026-10-13",
                        "commitment": 7}}
        self.assertEqual(self.resolve(commitments=[commitment]), expected)
        for alias in ("external:liquichain-window", "GATE:liquichain-window"):
            self.assertEqual(self.resolve(commitments=[dict(commitment, depends_on=[alias])]),
                             expected)
        end = plan.day_start_ts("2026-10-14")
        self.assertEqual(self.resolve(commitments=[commitment], now=end - 1), expected)
        self.assertEqual(self.resolve(commitments=[commitment], now=end), {})

    def test_external_gate_on_a_package_holds_its_descendants(self):
        commitment = {"id": 8, "status": "open", "package_id": "epic", "due_on": "2026-10-13",
                      "depends_on": ["gate:epic-window"]}
        self.assertEqual(self.resolve(commitments=[commitment]),
                         {1: {"what": "hold", "gate": "epic-window", "until": "2026-10-13",
                              "commitment": 8}})
        rows = [{"id": 4, "state": "build", "package_id": "other"}]
        self.assertEqual(self.resolve(commitments=[commitment], rows=rows), {})

    def test_unrepresentable_hold_end_does_not_break_other_waits(self):
        rows = [{"id": 1, "state": "build"}, {"id": 2, "state": "build"}]
        commitments = [
            {"status": "open", "work_item_id": 1, "due_on": "9999-12-31",
             "depends_on": ["gate:out-of-range"]},
            {"status": "open", "work_item_id": 2, "due_on": "2026-10-13",
             "depends_on": ["gate:valid"]},
        ]
        self.assertEqual(self.resolve(rows=rows, commitments=commitments),
                         {2: {"what": "hold", "gate": "valid", "until": "2026-10-13"}})

    def test_malformed_finished_or_undated_gate_does_not_suppress(self):
        base = {"id": 7, "status": "open", "work_item_id": 1, "due_on": "2026-10-13"}
        for reference in ("gate:", "gate", "external", "gate liquichain", "hold:liquichain",
                          "after the window", None):
            with self.subTest(reference=reference):
                self.assertEqual(self.resolve(commitments=[dict(base, depends_on=[reference])]), {})
        for status in ("proposed", "done", "cancelled"):
            self.assertEqual(self.resolve(commitments=[dict(base, status=status,
                                                            depends_on=["gate:x"])]), {})
        for due in (None, "", "someday"):
            self.assertEqual(self.resolve(commitments=[dict(base, due_on=due,
                                                            depends_on=["gate:x"])]), {})
        self.assertEqual(self.resolve(commitments=[dict(base, due_on="2026-10-11",
                                                        depends_on=["gate:x"])]), {})
        # a lot dependency stays a dependency, and wins over a gate on the same lot
        both = dict(base, depends_on=["gate:x", "#2"])
        self.assertEqual(self.resolve(commitments=[both], items=[{"id": 2, "state": "build"}]),
                         {1: {"what": "dependency", "lots": [2]}})

    # -- #1945 : atterrissage planifié sur un véhicule de paquet -------------

    def landing(self, **kw):
        row = {"work_item_id": 1, "vehicle_id": 2, "until_on": "2026-10-13",
               "open": True, "vehicle_state": "qa",
               "pinned_sha": "b" * 40, "head": "b" * 40}
        row.update(kw)
        return row

    def test_landing_waits_while_vehicle_open_until_the_end_of_its_day(self):
        expected = {1: {"what": "landing", "vehicle": 2, "step": "qa",
                        "head": "b" * 40, "until": "2026-10-13"}}
        self.assertEqual(self.resolve(landings=[self.landing()]), expected)
        end = plan.day_start_ts("2026-10-14")
        self.assertEqual(self.resolve(landings=[self.landing()], now=end - 1), expected)
        # la journée compte en entier : le lendemain, le signal stale revient
        self.assertEqual(self.resolve(landings=[self.landing()], now=end), {})
        for broken in ({"open": False}, {"until_on": None}, {"until_on": ""},
                       {"until_on": "someday"}, {"until_on": "2026-10-11"}, {"vehicle_id": 1}):
            with self.subTest(broken=broken):
                self.assertEqual(self.resolve(landings=[self.landing(**broken)]), {})
        # sans drapeau `open` (résolveur pur), le véhicule doit être un lot ouvert connu
        fallback = self.landing()
        fallback.pop("open")
        self.assertEqual(self.resolve(landings=[fallback]), {})
        self.assertEqual(self.resolve(landings=[fallback], items=[{"id": 2, "state": "qa"}]),
                         expected)

    def test_landing_is_pinned_to_the_freeze_and_gives_the_signal_back(self):
        """#1945 revue B : sans gel, sur un autre gel ou véhicule bloqué, rien
        n'est éteint ; le renouvellement explicite ré-épingle la nouvelle tête."""
        for broken in ({"pinned_sha": None}, {"pinned_sha": ""},
                       {"head": None}, {"head": ""},
                       {"head": "c" * 40},                      # nouveau gel B
                       {"vehicle_state": "blocked"}):
            with self.subTest(broken=broken):
                self.assertEqual(self.resolve(landings=[self.landing(**broken)]), {})
        # renouvellement sur la nouvelle tête : l'attente revient, épinglée à B
        renewed = self.landing(pinned_sha="c" * 40, head="c" * 40)
        self.assertEqual(self.resolve(landings=[renewed]),
                         {1: {"what": "landing", "vehicle": 2, "step": "qa",
                              "head": "c" * 40, "until": "2026-10-13"}})
        # sans drapeau `open` (résolveur pur), le véhicule doit être un lot ouvert connu
        fallback = self.landing()
        fallback.pop("open")
        self.assertEqual(self.resolve(landings=[fallback]), {})
        self.assertEqual(self.resolve(landings=[fallback], items=[{"id": 2, "state": "qa"}]),
                         {1: {"what": "landing", "vehicle": 2, "step": "qa",
                              "head": "b" * 40, "until": "2026-10-13"}})

    def test_explicit_waits_and_gates_stay_ahead_of_a_landing(self):
        landing = self.landing()
        self.assertEqual(self.resolve(dates={1: {"planned_start": "2026-10-13"}},
                                      landings=[landing]),
                         {1: {"what": "scheduled", "start": "2026-10-13"}})
        self.assertEqual(self.resolve(commitments=[{"status": "open", "work_item_id": 1,
                                                    "depends_on": ["#2"]}],
                                      items=[{"id": 2, "state": "build"}], landings=[landing]),
                         {1: {"what": "dependency", "lots": [2]}})
        self.assertEqual(self.resolve(commitments=[{"status": "open", "work_item_id": 1,
                                                    "due_on": "2026-10-13",
                                                    "depends_on": ["gate:x"]}],
                                      landings=[landing]),
                         {1: {"what": "hold", "gate": "x", "until": "2026-10-13"}})

    def test_landing_label_names_vehicle_step_head_and_deadline(self):
        wait = {"what": "landing", "vehicle": 42, "step": "qa",
                "head": "b" * 40, "until": "2026-10-18"}
        self.assertEqual(work_dependencies.wait_label(wait),
                         "landing on vehicle #42 (qa, head %s) until 2026-10-18" % ("b" * 12))

    def test_landing_only_parks_a_lot_still_in_review(self):
        """#2171 N3 : sorti de qa (build, blocked, humain), le signal revient."""
        expected = {1: {"what": "landing", "vehicle": 2, "step": "qa",
                        "head": "b" * 40, "until": "2026-10-13"}}
        self.assertEqual(self.resolve(rows=[{"id": 1, "state": "qa"}],
                                      landings=[self.landing()]), expected)
        self.assertEqual(self.resolve(rows=[{"id": 1, "state": "validated"}],
                                      landings=[self.landing()]), expected)
        for state in ("intake", "build", "blocked", "waiting_human"):
            self.assertEqual(self.resolve(rows=[{"id": 1, "state": state}],
                                          landings=[self.landing()]), {})

    def test_latest_freeze_breaks_ties_on_id_whatever_the_reading_order(self):
        """#2171 N2 : dernier gel = (at, id) maximal, comme le pin de set_landing."""
        rows = [
            {"id": 9, "work_item_id": 2, "kind": "frozen", "at_ts": 5.0, "sha": "b" * 40},
            {"id": 3, "work_item_id": 2, "kind": "frozen", "at_ts": 5.0, "sha": "a" * 40},
            {"id": 4, "work_item_id": 2, "kind": "frozen", "at_ts": 4.0, "sha": "c" * 40},
            {"id": 5, "work_item_id": 2, "kind": "verdict", "at_ts": 9.0, "sha": "d" * 40},
        ]
        self.assertEqual(W.latest_freeze(rows)["sha"], "b" * 40)
        self.assertIsNone(W.latest_freeze([r for r in rows if r["kind"] != "frozen"]))

        class _St:
            class progress:
                @staticmethod
                def lot_milestones(ids):
                    return rows

        landing = {"work_item_id": 1, "vehicle_id": 2, "pinned_sha": "b" * 40}
        self.assertEqual(W._with_heads(_St(), [landing])[0]["head"], "b" * 40)


    def test_explicit_hold_is_a_planned_wait_until_lifted_or_moved(self):
        """#1664 : `hold:` tient le lot (alerte stale éteinte) ; une note
        ordinaire (triage, relevé) ne le lève pas, seuls une levée explicite
        (`unhold:`…) ou un changement d'état le font."""
        def event(note, state="build", actor="mesh-design", ts=1.0, ident=1):
            return {"work_item_id": 1, "state": state, "note": note,
                    "actor": actor, "created_ts": ts, "id": ident}

        hold = W.explicit_holds([event("created", state="intake", ts=0.5, ident=1),
                                 event("hold: fenêtre opérateur", ts=1.0, ident=2)])
        self.assertEqual(hold, {1: {"what": "hold", "by": "mesh-design", "since": 1.0}})
        self.assertEqual(self.resolve(holds=hold),
                         {1: {"what": "hold", "by": "mesh-design", "since": 1.0}})
        # une note ordinaire ne lève pas la retenue
        self.assertEqual(W.explicit_holds([event("hold: op", ident=2),
                                           event("triage: relevé", ts=2.0, ident=3)]),
                         {1: {"what": "hold", "by": "mesh-design", "since": 1.0}})
        # une levée explicite la lève
        for lift in ("unhold: feu reçu", "hold lifted: feu reçu",
                     "hold released: feu reçu", "hold cleared: feu reçu"):
            with self.subTest(lift=lift):
                self.assertEqual(W.explicit_holds([event("hold: op", ident=2),
                                                   event(lift, ts=2.0, ident=3)]), {})
        # un changement d'état lève
        self.assertEqual(W.explicit_holds([event("hold: op", state="qa", ident=2),
                                           event("qa → build", state="build", ts=2.0,
                                                 ident=3)]), {})
        # une note vide ne compte pas ; un hold reposé après du bruit tient
        again = W.explicit_holds([event("hold: op", ident=2),
                                  event("triage: relevé", ts=2.0, ident=3),
                                  event("", ts=2.5, ident=4),
                                  event("hold: retour opérateur", ts=3.0, ident=5)])
        self.assertEqual(again[1]["since"], 3.0)
        # l'ordre est celui de l'écriture (created_ts, id), pas celui de la liste
        self.assertEqual(W.explicit_holds([event("hold: op", ts=2.0, ident=3),
                                           event("triage", ts=1.0, ident=2)]),
                         {1: {"what": "hold", "by": "mesh-design", "since": 2.0}})
        self.assertEqual(W.explicit_holds([event("triage", ts=2.0, ident=3),
                                           event("hold: op", ts=1.0, ident=2)]),
                         {1: {"what": "hold", "by": "mesh-design", "since": 1.0}})

    def test_only_the_allowed_author_holds_and_lifts(self):
        """#1664 : `allowed` restreint le hold à un pilote — le hold d'un autre
        acteur ne compte pas, et lui seul peut le lever."""
        def event(note, actor, ts, ident):
            return {"work_item_id": 1, "state": "build", "note": note,
                    "actor": actor, "created_ts": ts, "id": ident}

        self.assertEqual(W.explicit_holds([event("hold: op", "dev7", 1.0, 2)],
                                          allowed={"mesh-design"}), {})
        self.assertEqual(W.explicit_holds([event("hold: op", "dev7", 1.0, 2)],
                                          allowed={"dev7"})[1]["by"], "dev7")
        # la note et la levée d'un autre acteur ne comptent pas
        events = [event("hold: op", "mesh-design", 1.0, 2),
                  event("triage: relevé", "dev7", 2.0, 3),
                  event("unhold: à tort", "dev7", 3.0, 4)]
        self.assertEqual(W.explicit_holds(events, allowed={"mesh-design"})[1]["since"], 1.0)
        # la levée du pilote compte
        self.assertEqual(W.explicit_holds(
            events + [event("unhold: feu reçu", "mesh-design", 4.0, 5)],
            allowed={"mesh-design"}), {})
        # un prédicat d'autorisation est accepté
        self.assertEqual(W.explicit_holds([event("hold: op", "mesh-design", 1.0, 2)],
                                          allowed=lambda who: who == "mesh-design")[1]["by"],
                         "mesh-design")

    def test_pilot_hold_releases_keep_every_lift_for_the_intake_clock(self):
        """#2916/#1225 : les levées d'un hold de pilote sont rendues au calcul
        SLO (toutes, pour qu'il filtre par premier `build`, #2921) ; un hold
        d'un acteur ordinaire ne compte pas, même autorisation que
        `pilot_holds`."""
        def event(note, ts, ident, actor="human:pilote", state="intake"):
            return {"work_item_id": 1, "state": state, "note": note,
                    "actor": actor, "created_ts": ts, "id": ident}

        events = [event("created", 0.5, 1),
                  event("hold: attente owner", 1.0, 2),
                  event("triage: relevé", 2.0, 3),
                  event("unhold: gestes faits", 3.0, 4),
                  event("hold: seconde attente", 4.0, 5),
                  event("unhold: reprise", 5.0, 6)]
        self.assertEqual(W.pilot_hold_releases(None, events), {1: [3.0, 5.0]})
        # un hold d'un acteur ordinaire ne compte pas (il ne suspend pas la
        # stagnation non plus) ; la levée du pilote ne rend pas de date.
        self.assertEqual(W.pilot_hold_releases(
            None, [event("hold: op", 1.0, 2, actor="dev7"),
                   event("unhold: op", 2.0, 3)]), {})
        # une levée d'un acteur ordinaire ne lève pas le hold de pilote
        self.assertEqual(W.pilot_hold_releases(
            None, [event("hold: op", 1.0, 2),
                   event("unhold: à tort", 2.0, 3, actor="dev7")]), {})
        # ... mais la levée du pilote qui suit compte
        self.assertEqual(W.pilot_hold_releases(
            None, [event("hold: op", 1.0, 2),
                   event("unhold: à tort", 2.0, 3, actor="dev7"),
                   event("unhold: feu reçu", 4.0, 5)]), {1: [4.0]})
        # un changement d'état lève aussi le hold
        self.assertEqual(W.pilot_hold_releases(
            None, [event("hold: op", 1.0, 2),
                   event("prise", 2.0, 3, state="build")]), {1: [2.0]})
        # l'ordre est celui de l'écriture, pas celui de la liste
        self.assertEqual(W.pilot_hold_releases(
            None, [event("unhold: reprise", 3.0, 3),
                   event("hold: op", 1.0, 2)]), {1: [3.0]})

    def test_hold_outranks_a_declared_dependency_and_its_label(self):
        hold = {1: {"what": "hold", "by": "pilote", "since": 1700000000.0}}
        commitment = {"status": "open", "work_item_id": 1, "depends_on": ["#2"]}
        self.assertEqual(self.resolve(commitments=[commitment],
                                      items=[{"id": 2, "state": "qa"}], holds=hold), hold)
        label = D.wait_label(hold[1])
        self.assertTrue(label.startswith("hold by pilote since "), label)
        self.assertIsNone(D.wait_label(None))

    def test_hold_patterns_cover_the_exact_notes(self):
        """#1664 B2 : les motifs donnés au stockage couvrent toutes les notes
        que `is_hold_note`/`is_lift_note` acceptent, et aucune note ordinaire —
        le filtre SQL ne peut ni manquer une retenue ni transférer un texte
        ordinaire."""
        acceptees = ("hold: fenêtre", "HOLD: fenêtre",
                     "unhold: feu", "UNHOLD: feu", "unhold : feu",
                     "hold lifted: feu", "hold released: feu", "hold cleared: feu",
                     "hold   lifted : feu")
        for text in acceptees:
            with self.subTest(text=text):
                self.assertTrue(W.is_hold_note(text) or W.is_lift_note(text))
                self.assertTrue(any(re.match(p, text.strip(), re.I)
                                    for p in W.HOLD_NOTE_PATTERNS))
        for text in ("triage: relevé", "unholding the gate", "holding pattern",
                     "hold-up: staging", "threshold reached", ""):
            with self.subTest(text=text):
                self.assertFalse(W.is_hold_note(text) or W.is_lift_note(text))
                self.assertFalse(any(re.match(p, text.strip(), re.I)
                                     for p in W.HOLD_NOTE_PATTERNS))

    def test_targeted_stream_holds_like_the_full_journal(self):
        """#1664 B2 : le flux ciblé du stockage (transitions + notes hold/levée)
        donne le même hold que le journal complet, sans le texte des notes
        ordinaires ; une note portée par une transition est rendue avec elle."""
        class Journal:
            def __init__(self, events):
                self.events = list(events)

            def lot_events(self, ids):
                gardes = {int(i) for i in ids}
                return [e for e in self.events if int(e["work_item_id"]) in gardes]

        def row(ident, state, note="", actor="dev7"):
            return {"id": ident, "work_item_id": 1, "state": state, "note": note,
                    "actor": actor, "created_ts": float(ident)}

        bruit = "triage: " + "x" * 5000
        proche = "holding pattern: bruit ordinaire"
        journal = [row(1, "intake", "created"),
                   row(2, "build", "prise"),
                   row(3, "build", bruit),
                   row(4, "build", proche),
                   row(5, "build", "hold: fenêtre opérateur", actor="human:pilote"),
                   row(6, "build", bruit),
                   row(7, "qa", "hold: porte portée par la transition",
                       actor="human:pilote"),
                   row(8, "qa", bruit)]
        flux = SI.Progress.lot_hold_events(Journal(journal), [1], W.HOLD_NOTE_PATTERNS)
        notes = [str(e.get("note") or "") for e in flux]
        self.assertNotIn(bruit, notes)
        self.assertNotIn(proche, notes)     # même un mot proche ne sort pas
        self.assertIn("hold: fenêtre opérateur", notes)
        # la note portée par la transition est rendue avec la transition
        porte = next(e for e in flux if e["id"] == 7)
        self.assertEqual(porte["state"], "qa")
        self.assertEqual(porte["note"], "hold: porte portée par la transition")
        # le flux ciblé et le journal complet donnent le même hold
        autorises = {"human:pilote"}
        self.assertEqual(W.explicit_holds(journal, allowed=autorises),
                         W.explicit_holds(flux, allowed=autorises))
        self.assertEqual(W.explicit_holds(flux, allowed=autorises)[1]["since"], 7.0)
        # sans motif, le flux ne porte que les transitions, sans note
        transitions = SI.Progress.lot_hold_events(Journal(journal), [1], ())
        self.assertEqual([e["id"] for e in transitions], [1, 2, 7])
        self.assertTrue(all("note" not in e for e in transitions))
        # un motif vide est refusé : il ferait sortir toutes les notes
        with self.assertRaises(ValueError):
            SI.Progress.lot_hold_events(Journal(journal), [1],
                                        (W.HOLD_NOTE_PATTERNS[0], ""))


class PgTest(PgTestCase):
    #: #1664 r2 : l'identité du lanceur ne décide pas du test.
    IDENTITY_ENV = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                    "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH",
                    "AMEESH_ALERT_ORCHESTRATORS")

    def setUp(self):
        self.db.execute("TRUNCATE work_items, commitments, work_packages CASCADE")
        self.now = time.time()

    def unbound(self, **extra):
        """Environnement d'identité neutre pour ce test, restauré à la fin."""
        saved = {key: os.environ.get(key) for key in self.IDENTITY_ENV}
        for key in self.IDENTITY_ENV:
            os.environ.pop(key, None)
        os.environ.update(extra)

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def old(self, item):
        for sql in ("UPDATE work_items SET updated_at = to_timestamp(%s), created_at = to_timestamp(%s) WHERE id = %s",
                    "UPDATE work_item_events SET created_at = to_timestamp(%s) WHERE work_item_id = %s",
                    "UPDATE work_item_milestones SET at = to_timestamp(%s) WHERE work_item_id = %s"):
            args = (self.now - 10 * 3600, self.now - 11 * 3600, item["id"]) if "created_at = to_timestamp(%s) WHERE id" in sql else (self.now - 10 * 3600, item["id"])
            self.db.execute(sql, args)

    def item(self, title):
        item = work.add(self.db, title=title, app="acme")
        self.old(item)
        return item

    def test_planned_start_in_alert_work_and_progress_and_limit_after_filter(self):
        future = self.item("planned tomorrow")
        actionable = self.item("unexplained wait")
        tomorrow = (datetime.date.fromtimestamp(self.now) + datetime.timedelta(days=1)).isoformat()
        storage.of(self.db).roadmap.plan_item(future["id"], {"start": tomorrow}, source="test", actor="test")
        alerts = stagnation.stagnant_lots(self.db, now=self.now, limit=1)
        self.assertEqual([a["id"] for a in alerts], [actionable["id"]])
        annotated = plan.annotate(self.db, [work.get(self.db, future["id"])], now=self.now)[0]
        self.assertIsNone(annotated["stale"])
        self.assertEqual(annotated["planned_wait"]["start"], tomorrow)
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, gantt=False)
        lots = {r["id"]: r for r in snap["lots"]}
        self.assertIsNone(lots[future["id"]]["stale"])
        self.assertIsNotNone(lots[actionable["id"]]["stale"])
        after_start = stagnation.stagnant_lots(self.db, now=plan.day_start_ts(tomorrow))
        self.assertIn(future["id"], [r["id"] for r in after_start])

    def test_open_blocker_beyond_render_limit_and_release_when_closed(self):
        waiting = self.item("wait for a real lot")
        blocker = work.add(self.db, title="blocker", app="other-project")
        roadmap.add_commitment(self.db, "release", due="2999-01-01", lot=waiting["id"],
                               depends_on=["task #%d" % blocker["id"]])
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now, limit=1), [])
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertEqual(row["planned_wait"]["lots"], [blocker["id"]])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, max_lots=1, gantt=False)
        self.assertIsNone(snap["lots"][0]["stale"])
        work.close(self.db, blocker["id"], abandoned=True)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)], [waiting["id"]])

    def test_cancelled_commitment_or_prose_does_not_mask_stagnation(self):
        waiting = self.item("wait in prose only")
        blocker = work.add(self.db, title="blocker")
        commitment = roadmap.add_commitment(self.db, "release", due="2999-01-01", lot=waiting["id"],
                                             depends_on=["after #%d merges" % blocker["id"]])
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)], [waiting["id"]])
        storage.of(self.db).roadmap.set_commitment(commitment["id"],
            values={"depends_on": ["#%d" % blocker["id"]], "status": "cancelled"}, current=["open"])
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)], [waiting["id"]])

    def test_package_plan_dependency_is_read_from_storage(self):
        st = storage.of(self.db)
        for ident, kind, parent in (("epic", "epic", None), ("child", "lot", "epic"),
                                    ("prerequisite", "lot", None)):
            st.packages.upsert({"id": ident, "kind": kind, "title": ident, "parent": parent,
                                "canon_ref": "test", "present": True})
        waiting = work.add(self.db, title="waiting", app="acme", package="child")
        self.old(waiting)
        blocker = work.add(self.db, title="blocker", app="other", package="prerequisite")
        roadmap.add_commitment(self.db, "epic release", due="2999-01-01", package="epic",
                               depends_on=["prerequisite"])
        rows = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)
        self.assertEqual(rows[0]["planned_wait"]["lots"], [blocker["id"]])
        self.assertIsNone(rows[0]["stale"])

    def test_external_hold_defers_stagnation_and_ends_at_its_due_day(self):
        waiting = self.item("blocked by an external gate")
        due = (datetime.date.fromtimestamp(self.now)
               + datetime.timedelta(days=1)).isoformat()
        commitment = roadmap.add_commitment(
            self.db, "hold until the liquichain window", due=due, lot=waiting["id"],
            depends_on=["gate:liquichain-window"])
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertEqual(row["planned_wait"],
                         {"what": "hold", "gate": "liquichain-window", "until": due,
                          "commitment": commitment["id"]})
        self.assertIsNone(row["stale"])
        self.assertEqual(work_dependencies.wait_label(row["planned_wait"]),
                         "external hold until %s (liquichain-window)" % due)
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, gantt=False)
        self.assertIsNone(next(r for r in snap["lots"] if r["id"] == waiting["id"])["stale"])
        # the due day counts in full: the ordinary rule applies again the next day
        after = plan.day_start_ts(
            (datetime.date.fromtimestamp(self.now) + datetime.timedelta(days=2)).isoformat())
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=after)],
                         [waiting["id"]])
        # cancelling the commitment releases the lot immediately
        storage.of(self.db).roadmap.set_commitment(
            commitment["id"], values={"status": "cancelled"}, current=["open"])
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [waiting["id"]])

    def test_declared_landing_defers_stagnation_until_cleared_closed_or_expired(self):
        waiting = work.add(self.db, title="frozen, carried by a package vehicle", app="acme")
        work.move(self.db, waiting["id"], "build", actor="test")
        work.move(self.db, waiting["id"], "qa", actor="test")
        head = "c" * 40
        work.milestone(self.db, waiting["id"], "frozen", sha=head, actor="test")
        work.milestone(self.db, waiting["id"], "verdict", verdict="ok", sha=head, actor="reviewer")
        self.old(waiting)
        # sans déclaration, c'est bien le faux stale générique qui tire
        stale_row = next(r for r in stagnation.stagnant_lots(self.db, now=self.now)
                         if r["id"] == waiting["id"])
        self.assertEqual(stale_row["waiting_for"], "fusion")
        self.assertEqual(stale_row["waiting"]["label"], "merge (executor not designated)")
        vehicle = work.add(self.db, title="package vehicle", app="acme")
        vehicle_head = "e" * 40
        work.milestone(self.db, vehicle["id"], "frozen", sha=vehicle_head, actor="test")
        today = datetime.date.fromtimestamp(self.now)
        until = (today + datetime.timedelta(days=2)).isoformat()
        work_dependencies.land(self.db, waiting["id"], vehicle["id"], until_on=until,
                               actor="mesh-design")
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        label = "landing on vehicle #%d (qa, head %s) until %s" % (
            vehicle["id"], vehicle_head[:12], until)
        self.assertEqual(row["planned_wait"],
                         {"what": "landing", "vehicle": vehicle["id"], "step": "qa",
                          "head": vehicle_head, "until": until})
        self.assertIsNone(row["stale"])
        self.assertEqual(row["waiting_for"]["label"], label)
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, gantt=False)
        self.assertIsNone(next(r for r in snap["lots"] if r["id"] == waiting["id"])["stale"])
        # revue B : un nouveau gel rend le signal, sans renouvellement explicite
        newer = "f" * 40
        work.milestone(self.db, vehicle["id"], "frozen", sha=newer, actor="test")
        self.old(waiting)
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertIsNone(row["planned_wait"])
        self.assertEqual(row["waiting_for"]["label"], "merge (executor not designated)")
        self.assertIn(waiting["id"], [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        # le renouvellement explicite ré-épingle la tête courante
        work_dependencies.land(self.db, waiting["id"], vehicle["id"], until_on=until, actor="pilot")
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertEqual(row["planned_wait"]["head"], newer)
        self.assertIn(newer[:12], row["waiting_for"]["label"])
        # véhicule bloqué après coup : le signal revient aussi
        work.move(self.db, vehicle["id"], "blocked", actor="test")
        self.old(waiting)
        self.assertIn(waiting["id"], [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        with self.assertRaises(work.WorkError) as blocked:
            work_dependencies.land(self.db, waiting["id"], vehicle["id"], until_on=until, actor="pilot")
        self.assertIn("blocked", str(blocked.exception))
        # le retrait rend le signal stale immédiatement
        work.move(self.db, vehicle["id"], "qa", actor="test")
        work_dependencies.land(self.db, waiting["id"], vehicle["id"], until_on=until, actor="pilot")
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        self.assertTrue(work_dependencies.clear_landing(self.db, waiting["id"], actor="pilot"))
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertIsNone(row["planned_wait"])
        self.assertEqual(row["waiting_for"]["label"], "merge (executor not designated)")
        self.old(waiting)   # le geste lui-même est une activité ; on repart de l'âge d'avant
        self.assertIn(waiting["id"], [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        # véhicule fermé : le signal revient
        work_dependencies.land(self.db, waiting["id"], vehicle["id"], until_on=until, actor="pilot")
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        work.close(self.db, vehicle["id"], abandoned=True)
        self.old(waiting)
        self.assertIn(waiting["id"], [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        # véhicule sans gel : la déclaration est refusée avant toute écriture
        unfrozen = work.add(self.db, title="unfrozen vehicle", app="acme")
        with self.assertRaises(work.WorkError) as no_freeze:
            work_dependencies.land(self.db, waiting["id"], unfrozen["id"], until_on=until, actor="pilot")
        self.assertIn("frozen head", str(no_freeze.exception))
        self.assertIsNone(plan.annotate(self.db, [work.get(self.db, waiting["id"])],
                                        now=self.now)[0]["planned_wait"])
        # échéance dépassée : la journée compte en entier, puis le signal revient
        vehicle2 = work.add(self.db, title="second vehicle", app="acme")
        work.milestone(self.db, vehicle2["id"], "frozen", sha="a" * 40, actor="test")
        work_dependencies.land(self.db, waiting["id"], vehicle2["id"], until_on=until, actor="pilot")
        end_until = plan.day_start_ts(
            (datetime.date.fromisoformat(until) + datetime.timedelta(days=1)).isoformat())
        self.assertNotIn(waiting["id"],
                         [r["id"] for r in stagnation.stagnant_lots(self.db, now=end_until - 1)])
        self.assertIn(waiting["id"],
                      [r["id"] for r in stagnation.stagnant_lots(self.db, now=end_until)])

    def test_hold_note_suppresses_stagnation_until_lifted_or_moved(self):
        """#1664 : une note `hold:` d'un pilote éteint la relance 6 h et ses vues
        (work, progression) ; une note ordinaire ne la lève pas, seuls une levée
        explicite ou un changement d'état le font ; le hold d'un acteur ordinaire
        (règle 1) ne compte pas."""
        self.unbound()
        lot = self.item("parked on an operator window")
        work.note(self.db, lot["id"], "hold: fenêtre opérateur + accès",
                  actor="human:pilote")
        self.old(lot)
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        row = plan.annotate(self.db, [work.get(self.db, lot["id"])], now=self.now)[0]
        self.assertEqual(row["planned_wait"]["what"], "hold")
        self.assertEqual(row["planned_wait"]["by"], "human:pilote")
        self.assertIsNone(row["stale"])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, gantt=False)
        held = {r["id"]: r for r in snap["lots"]}[lot["id"]]
        self.assertEqual(held["planned_wait"]["what"], "hold")
        self.assertIsNone(held["stale"])
        # une note ordinaire ne lève pas la retenue
        work.note(self.db, lot["id"], "triage: relevé de passage", actor="dev7")
        self.old(lot)
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        # le hold d'un acteur ordinaire (règle 1) ne compte pas pour la stagnation
        other = self.item("hold sans autorité")
        work.note(self.db, other["id"], "hold: décision d'équipe", actor="dev7")
        self.old(other)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [other["id"]])
        # une levée explicite du pilote rallume la relance
        work.note(self.db, lot["id"], "unhold: fenêtre ouverte", actor="human:pilote")
        self.old(lot)
        self.assertIn(lot["id"],
                      [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        # un hold reposé tient, et un changement d'état le lève
        work.note(self.db, lot["id"], "hold: re-park", actor="human:pilote")
        self.old(lot)
        self.assertNotIn(lot["id"],
                         [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        work.move(self.db, lot["id"], "build", actor="human:pilote")
        self.old(lot)
        self.assertIn(lot["id"],
                      [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])

    def test_hold_note_authenticates_the_claimed_pilot(self):
        """#1664 r2 (B1) : une session ordinaire ne pose ni ne lève un hold de
        pilote en donnant `--actor human:…` ; sa propre note `hold:` (règle 1)
        reste permise et n'éteint pas la stagnation."""
        lot = self.item("porte pilote")
        other = self.item("faux hold de pilote")
        # un terminal humain sans liaison dit qui il est : le hold de pilote passe
        self.unbound()
        work.note(self.db, lot["id"], "hold: porte opérateur", actor="human:pilote")
        self.old(lot)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [other["id"]])
        # une session liée ordinaire ne se dit pas pilote…
        self.unbound(AGENT_MAIL_NAME="dev7")
        with self.assertRaisesRegex(work.WorkError, "belongs to dev7"):
            work.note(self.db, other["id"], "hold: faux pilote", actor="human:pilote")
        with self.assertRaisesRegex(work.WorkError, "belongs to dev7"):
            work.move(self.db, other["id"], "build", note="hold: faux pilote",
                      actor="human:pilote")
        # …ni lever le hold du pilote, même en son propre nom
        with self.assertRaisesRegex(work.WorkError, "neither a human"):
            work.note(self.db, lot["id"], "unhold: à tort", actor="dev7")
        # sa propre note `hold:` (règle 1) passe, mais la stagnation reste due
        work.note(self.db, other["id"], "hold: je ne peux pas avancer", actor="dev7")
        self.old(other)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [other["id"]])
        self.old(lot)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [other["id"]])

    def test_cli_refuses_a_pilot_hold_from_an_ordinary_session(self):
        """#1664 r2 (B1) : le chemin réel — `work.resolve_actor` rend `--actor`
        explicite avant tout contrôle, et la garde d'écriture refuse la session
        ordinaire ; un terminal humain sans liaison dit qui il est, et le pilote
        lié pose son hold en son nom."""
        lot = self.item("porte pilote")
        spoof = self.mesh("work", "note", str(lot["id"]), "hold: faux pilote",
                          "--actor", "human:pilote", env=self.env(AGENT_MAIL_NAME="dev7"))
        self.assertNotEqual(spoof.returncode, 0, spoof.stdout)
        self.assertIn("refused", spoof.stderr)
        # un humain à son terminal : aucune liaison, il dit qui il est
        ok = self.mesh("work", "note", str(lot["id"]), "hold: fenêtre opérateur",
                       "--actor", "human:pilote", env=self.env())
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.old(lot)
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        # le pilote lié (rôle déclaré) pose son hold en son nom
        other = self.item("porte pilote liée")
        self.unbound(AGENT_MAIL_NAME="mesh-design",
                     AMEESH_ALERT_ORCHESTRATORS="mesh-design")
        work.note(self.db, other["id"], "hold: fenêtre opérateur", actor="mesh-design")
        self.old(other)
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])

    def test_hold_carried_by_a_transition_suppresses_then_the_next_move_lifts(self):
        """#1664 B2 : un hold porté par une transition (`work move --note`) est
        lu du flux ciblé ; la transition suivante le lève, comme le journal
        complet."""
        self.unbound()
        lot = self.item("porte portée par une transition")
        work.move(self.db, lot["id"], "build", note="hold: fenêtre opérateur",
                  actor="human:pilote")
        self.old(lot)
        described = stagnation.describe(self.db, [work.get(self.db, lot["id"])],
                                        now=self.now)[lot["id"]]
        self.assertEqual(described["planned_wait"]["what"], "hold")
        self.assertIsNone(described["stale"])
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now,
                                 gantt=False)
        held = {r["id"]: r for r in snap["lots"]}[lot["id"]]
        self.assertEqual(held["planned_wait"]["what"], "hold")
        self.assertIsNone(held["stale"])
        # la transition suivante lève la retenue (build → blocked)
        work.move(self.db, lot["id"], "blocked", actor="human:pilote")
        self.old(lot)
        self.assertIn(lot["id"],
                      [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        # la note de transition est stockée telle qu'authentifiée, blancs ôtés :
        # un préfixe Unicode ne peut pas échapper au filtre littéral du pilote SQL
        espace = self.item("porte sur transition espacée")
        work.move(self.db, espace["id"], "build", note="\u00a0hold: fenêtre opérateur ",
                  actor="human:pilote")
        self.old(espace)
        stockee = next(e for e in work.events(self.db, espace["id"])
                       if e["state"] == "build")["note"]
        self.assertEqual(stockee, "hold: fenêtre opérateur")
        self.assertNotIn(espace["id"],
                         [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])

    def test_grouped_views_read_the_targeted_stream_never_the_journal(self):
        """#1664 B2 : avec un hold posé, les vues groupées (alerte, work list,
        progrès) et le repli `planned_waits.read` lisent le flux ciblé du
        stockage — jamais le journal complet (contrat #1647 conservé)."""
        self.unbound()
        lot = self.item("porte relue sans journal")
        work.note(self.db, lot["id"], "hold: fenêtre opérateur", actor="human:pilote")
        work.note(self.db, lot["id"], "triage: relevé de passage", actor="dev7")
        self.old(lot)

        def interdit(*_args, **_kwargs):
            raise AssertionError(
                "le journal complet est relu par une vue groupée (#1647/#1664)")

        with mock.patch.object(Progress, "lot_events", interdit):
            alertes = stagnation.stagnant_lots(self.db, now=self.now)
            rows = mesh_cli.work_rows(self.db, limit=50)
            snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now,
                                     gantt=False)
            waits = W.read(self.db, [work.get(self.db, lot["id"])], now=self.now,
                           open_items=[])
        self.assertEqual(alertes, [])
        ligne = next(r for r in rows if int(r["id"]) == lot["id"])
        self.assertEqual(ligne["planned_wait"]["what"], "hold")
        self.assertIsNone(ligne["stale"])
        vue = next(l for l in snap["lots"] if int(l["id"]) == lot["id"])
        self.assertEqual(vue["planned_wait"]["what"], "hold")
        self.assertIsNone(vue["stale"])
        self.assertEqual(waits[lot["id"]]["what"], "hold")

    def test_targeted_stream_keeps_unicode_holds_and_lifts(self):
        """Le filtre SQL garde les levées Unicode reconnues par le métier,
        y compris les blancs de bord des notes de transition historiques."""
        lot = self.item("porte Unicode")
        # Déclarés dans le témoin, sans reprendre la constante de production.
        spaces = "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680" + "".join(
            chr(c) for c in range(0x2000, 0x200b)) + "\u2028\u2029\u202f\u205f\u3000"
        notes = [text for s in spaces for text in
                 (s + "hold: porte" + s, "unhold" + s + ": feu",
                  "hold" + s + "lifted: feu")]
        notes += ["hold lİfted: feu", "hold lıfted: feu", "hold releaſed: feu"]
        self.assertTrue(all(W.is_hold_note(n) or W.is_lift_note(n) for n in notes))
        bruit = "triage: " + "x" * 5000
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " SELECT %s, 'intake', v.note, 'human:pilote' FROM (VALUES "
            + ",".join(["(%s)"] * (len(notes) + 1)) + ") v(note)",
            (lot["id"], *notes, bruit))
        st = storage.of(self.db).progress
        journal = st.lot_events([lot["id"]])
        flux = st.lot_hold_events([lot["id"]], W.HOLD_NOTE_PATTERNS)
        self.assertEqual([e["note"] for e in flux if "note" in e], notes)
        faux = mock.Mock(lot_events=mock.Mock(return_value=journal))
        self.assertEqual(flux, SI.Progress.lot_hold_events(
            faux, [lot["id"]], W.HOLD_NOTE_PATTERNS))
        self.assertEqual(W.explicit_holds(journal), W.explicit_holds(flux))

    def test_landing_lot_losing_its_verdict_or_freeze_gets_the_signal_back(self):
        """#2171 N3 (revue cx1) : la lecture groupée relit l'admission du lot.

        Un nouveau gel du lot admis, sans verdict, le laisse en `qa` mais défait
        son admission : `planned_waits.read` (donc stagnation et progress) rend
        le signal, comme `progress.verdict_current`."""
        waiting = work.add(self.db, title="carried while qa", app="acme")
        work.move(self.db, waiting["id"], "build", actor="test")
        work.move(self.db, waiting["id"], "qa", actor="test")
        head = "c" * 40
        work.milestone(self.db, waiting["id"], "frozen", sha=head, actor="test")
        work.milestone(self.db, waiting["id"], "verdict", verdict="ok", sha=head,
                       actor="reviewer")
        self.old(waiting)
        vehicle = work.add(self.db, title="package vehicle", app="acme")
        work.milestone(self.db, vehicle["id"], "frozen", sha="e" * 40, actor="test")
        until = (datetime.date.fromtimestamp(self.now)
                 + datetime.timedelta(days=2)).isoformat()
        work_dependencies.land(self.db, waiting["id"], vehicle["id"],
                               until_on=until, actor="pilot")
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        # nouveau gel du LOT admis, sans verdict : il reste qa, le signal revient
        work.milestone(self.db, waiting["id"], "frozen", sha="d" * 40, actor="test")
        self.old(waiting)
        item = work.get(self.db, waiting["id"])
        self.assertEqual(item["state"], "qa")
        self.assertIsNone(W.read(self.db, [item], now=self.now).get(waiting["id"]))
        self.assertIsNone(plan.annotate(self.db, [item], now=self.now)[0]["planned_wait"])
        self.assertIn(waiting["id"],
                      [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        # le verdict ok de l'ancienne tête ne vaut pas pour le nouveau gel
        self.assertFalse(progress._jalons_de_lot(
            item, [], [], storage.of(self.db).progress.lot_milestones([waiting["id"]])
        ).get("verdict_current"))
