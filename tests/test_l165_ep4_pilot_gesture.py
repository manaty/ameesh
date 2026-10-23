# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP4) : les gestes du pilote hors base sont fencés par l'époque du bail.

Étude `docs/design/etudes/election-pilote.md` §4. Un rôle élu
(`releve: election`) n'agit que sous son bail : `pilot_gesture.gate` vérifie
l'identité liée, refuse une session dont l'époque n'est plus celle du bail
vivant (en nommant le détenteur courant) et journalise le geste dans la même
transaction que le `renew` du bail. Les cinq gestes couverts sont la fusion
(`fire`, `merge-pr`, `work merged`), la file (`queue add/remove`, `land`),
l'attribution (`work assign`), le paquet (`packet record`, feu du train) et le
routage d'une décision (`decide ask`).

Ces témoins utilisent la même base migrée que les autres tests de l'interface
(`tests.support.PgTestCase`) : les noms d'agents sont propres à chaque test,
car `ameesh_events` est append-only.
"""
from __future__ import annotations

import contextlib
import os
import re
import time
import unittest
from unittest import mock

from ameesh import (
    decisions,
    fire,
    merge_pr,
    merge_queue,
    packet_automerge,
    packet_guard,
    packet_link,
    pilot_gesture,
    registry,
    work,
)

from .support import PgTestCase

IDENTITY_KEYS = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                 "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH",
                 "AMEESH_ROLE", "AMEESH_ROLE_HOST", "AMEESH_LEASE_OWNER")


class GestureFenceTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        # `ameesh_events` n'est pas tronquée (append-only) : un nom par test
        # évite qu'un événement d'un témoin précédent soit relu ici.
        slug = re.sub(r"[^a-z0-9]+", "-", self._testMethodName.lower()).strip("-")
        self.name = ("p-" + slug)[:32]
        registry.upsert(self.db, self.name, harness="claude", host="h1",
                        cwd=self.tmp, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET releve = 'election', candidats = ARRAY[%s, %s] "
            " WHERE name = %s", ("h1", "h2", self.name))
        self.db.execute("DELETE FROM host_holds")
        self.db.execute("DELETE FROM host_lifecycle")
        lease = registry.claim(self.db, self.name, "runner-h1", 600, host="h1")
        self.assertIsNotNone(lease)
        self.epoch = int(lease["lease_epoch"])

    # -- helpers ---------------------------------------------------------
    @contextlib.contextmanager
    def identity_env(self, *, name: str | None = None, runner: str | None = None,
                     epoch: int | None = None):
        """Pose l'identité d'une session, sans hériter celle du lanceur."""
        with mock.patch.dict(os.environ):
            for key in IDENTITY_KEYS:
                os.environ.pop(key, None)
            if name:
                os.environ["AGENT_MAIL_NAME"] = name
            if runner:
                os.environ["AMEESH_RUNNER_ID"] = runner
            if epoch is not None:
                os.environ["AMEESH_LEASE_EPOCH"] = str(epoch)
            yield

    def live(self):
        return self.identity_env(name=self.name, runner="runner-h1", epoch=self.epoch)

    @contextlib.contextmanager
    def role_env(self, *, role: str | None = None, epoch: int | None = None,
                 owner: str | None = None, name: str | None = None):
        """EP5 `ameesh role run` : le rôle et son époque, sans identité d'agent."""
        with self.identity_env(name=name):
            if role:
                os.environ["AMEESH_ROLE"] = role
            if epoch is not None:
                os.environ["AMEESH_LEASE_EPOCH"] = str(epoch)
            if owner:
                os.environ["AMEESH_LEASE_OWNER"] = owner
            yield

    def events(self, *, ref: str | None = None) -> list[dict]:
        sql = ("SELECT id, type, object, data FROM ameesh_events "
               " WHERE type = 'pilot.gesture' AND object = %s")
        params = ["agent:" + self.name]
        if ref is not None:
            sql += " AND data->>'ref' = %s"
            params.append(ref)
        return self.db.query(sql + " ORDER BY id", tuple(params))

    def refresh_epoch(self) -> None:
        row = self.db.query("SELECT lease_epoch FROM agent_registry WHERE name = %s",
                            (self.name,))[0]
        self.epoch = int(row["lease_epoch"])

    def steal_to_h2(self) -> dict | None:
        """Une relève croisée réelle (EP3) : sans brief, le CAS la refuse."""
        return registry.claim(self.db, self.name, "runner-h2", 600, host="h2",
                              election_brief="relève EP3")

    def new_lot(self, *, branch: str | None = None) -> int:
        return int(work.add(self.db, title="geste %s" % self.name, app="ameesh",
                            branch=branch)["id"])

    # -- the gate itself -------------------------------------------------
    def test_un_humain_n_est_pas_fence(self):
        with self.identity_env():
            self.assertIsNone(pilot_gesture.gate(self.cfg, self.db, "assign", "#1"))

    def test_un_agent_hote_n_est_pas_fence(self):
        registry.upsert(self.db, "hote-%s" % self.name, harness="claude", host="h1",
                        cwd=self.tmp, status="idle")
        lease = registry.claim(self.db, "hote-%s" % self.name, "runner-h1", 600)
        self.assertIsNotNone(lease)
        with self.identity_env(name="hote-%s" % self.name, runner="runner-h1",
                               epoch=int(lease["lease_epoch"])):
            self.assertIsNone(pilot_gesture.gate(self.cfg, self.db, "assign", "#1"))

    def test_un_geste_inconnu_est_refuse(self):
        with self.live():
            with self.assertRaises(ValueError):
                pilot_gesture.gate(self.cfg, self.db, "frobnicate", "#1")

    def test_sans_base_aucune_garde(self):
        with self.live():
            self.assertIsNone(pilot_gesture.gate(self.cfg, None, "assign", "#1"))

    def test_bail_vivant_autorise_et_journalise(self):
        with self.live():
            gate = pilot_gesture.gate(self.cfg, self.db, "assign", "#42")
            self.assertIsNotNone(gate)
            self.assertEqual((gate.agent, gate.epoch), (self.name, self.epoch))
            event = gate.hold(self.db)
        self.assertGreater(event, 0)
        rows = self.events(ref="#42")
        self.assertEqual(len(rows), 1, rows)
        data = rows[0]["data"]
        self.assertEqual(data["gesture"], "assign")
        self.assertEqual(data["epoch"], self.epoch)
        self.assertEqual(data["runner"], "runner-h1")
        self.assertEqual(data["host"], "h1")
        self.assertNotIn("token", data)
        self.assertNotIn("secret", data)
        # le bail a été renouvelé par la même transaction
        state = registry.get(self.db, self.name)
        self.assertGreater(float(state["lease_expires_ts"]), 0.0)

    def test_role_arrete_entre_la_garde_et_l_ecriture_refuse(self):
        with self.live():
            gate = pilot_gesture.gate(self.cfg, self.db, "merge", "stop #1")
            self.assertIsNotNone(gate)
            self.db.execute(
                "UPDATE agent_registry SET status = 'stopped' WHERE name = %s",
                (self.name,))
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                gate.hold(self.db)
        self.assertIn("stopped", str(caught.exception))
        self.assertEqual(self.events(ref="stop #1"), [])

    def test_ancienne_epoque_refusee_et_nomme_le_detenteur(self):
        old = self.epoch
        self.assertTrue(registry.release(self.db, self.name, "runner-h1", old))
        lease = self.steal_to_h2()
        self.assertIsNotNone(lease)
        new = int(lease["lease_epoch"])
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                pilot_gesture.guard(self.cfg, self.db, "merge", "merge-pr #7")
        message = str(caught.exception)
        self.assertIn("runner-h2", message)
        self.assertIn("h2", message)
        self.assertIn(str(new), message)
        self.assertEqual(self.events(ref="merge-pr #7"), [])

    def test_bail_expire_refuse(self):
        self.db.execute(
            "UPDATE agent_registry SET lease_expires_at = clock_timestamp() "
            "- interval '5 seconds' WHERE name = %s", (self.name,))
        with self.live():
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                pilot_gesture.guard(self.cfg, self.db, "queue", "land #3")
        self.assertIn("expired", str(caught.exception))
        self.assertEqual(self.events(ref="land #3"), [])

    def test_identite_explicite_sans_bail_refusee(self):
        with self.identity_env(name=self.name):
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                pilot_gesture.guard(self.cfg, self.db, "packet", "packet record #9")
        self.assertIn("lease", str(caught.exception).lower())
        self.assertEqual(self.events(ref="packet record #9"), [])

    # -- EP5 timers (`AMEESH_ROLE`) --------------------------------------
    def test_role_timer_live_journalise_le_role(self):
        with self.role_env(role=self.name, epoch=self.epoch):
            gate = pilot_gesture.gate(self.cfg, self.db, "merge", "role merge #1")
            self.assertIsNotNone(gate)
            self.assertEqual(gate.role, self.name)
            gate.hold(self.db)
        data = self.events(ref="role merge #1")[0]["data"]
        self.assertEqual(data["role"], self.name)
        self.assertEqual(data["epoch"], self.epoch)

    def test_role_timer_sans_epoque_refuse(self):
        with self.role_env(role=self.name):
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                pilot_gesture.gate(self.cfg, self.db, "merge", "role merge #2")
        self.assertIn("without AMEESH_LEASE_EPOCH", str(caught.exception))
        self.assertEqual(self.events(ref="role merge #2"), [])

    def test_role_timer_epoque_perimee_refusee(self):
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        lease = self.steal_to_h2()
        self.assertIsNotNone(lease)
        with self.role_env(role=self.name, epoch=old):
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                pilot_gesture.gate(self.cfg, self.db, "merge", "role merge #3")
        self.assertIn("runner-h2", str(caught.exception))
        self.assertEqual(self.events(ref="role merge #3"), [])

    def test_role_timer_proprietaire_divergent_refuse(self):
        with self.role_env(role=self.name, epoch=self.epoch, owner="runner-ailleurs"):
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                pilot_gesture.gate(self.cfg, self.db, "merge", "role merge #4")
        self.assertIn("runner-ailleurs", str(caught.exception))
        self.assertEqual(self.events(ref="role merge #4"), [])

    def test_role_timer_inconnu_refuse(self):
        with self.role_env(role="role-inconnu-3013", epoch=1):
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                pilot_gesture.gate(self.cfg, self.db, "merge", "role merge #5")
        self.assertIn("no such agent", str(caught.exception))

    def test_marge_refusee_si_le_bail_renouvele_est_trop_court(self):
        with self.live():
            gate = pilot_gesture.gate(self.cfg, self.db, "merge", "marge #1")
            self.assertIsNotNone(gate)
            with mock.patch.object(pilot_gesture.registry, "renew",
                                   return_value=time.time() + 5.0):
                with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                    gate.hold(self.db, margin_s=60.0)
        self.assertIn("external call", str(caught.exception))
        self.assertEqual(self.events(ref="marge #1"), [])

    # -- the five call sites --------------------------------------------
    def test_work_assign_live_ecrit_le_geste(self):
        worker = "worker-%s" % self.name
        registry.upsert(self.db, worker, harness="claude", host="h1",
                        cwd=self.tmp, status="idle")
        lot = self.new_lot()
        with self.live():
            out = work.assign(self.db, lot, worker, cfg=self.cfg)
        self.assertEqual(out["item"]["assignee"], worker)
        rows = self.events(ref="#%d" % lot)
        self.assertEqual([row["data"]["gesture"] for row in rows], ["assign"])

    def test_role_timer_live_attribue(self):
        worker = "worker-%s" % self.name
        registry.upsert(self.db, worker, harness="claude", host="h1",
                        cwd=self.tmp, status="idle")
        lot = self.new_lot()
        with self.role_env(role=self.name, epoch=self.epoch):
            out = work.assign(self.db, lot, worker, cfg=self.cfg)
        self.assertEqual(out["item"]["assignee"], worker)
        data = self.events(ref="#%d" % lot)[0]["data"]
        self.assertEqual(data["role"], self.name)

    def test_work_assign_stale_refuse_sans_changer_le_lot(self):
        worker = "worker-%s" % self.name
        registry.upsert(self.db, worker, harness="claude", host="h1",
                        cwd=self.tmp, status="idle")
        lot = self.new_lot()
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with self.assertRaises(pilot_gesture.GestureRefused):
                work.assign(self.db, lot, worker, cfg=self.cfg)
        self.assertIsNone(work.get(self.db, lot)["assignee"])
        self.assertEqual(self.events(ref="#%d" % lot), [])

    def test_work_add_assigne_stale_refuse_avant_creation(self):
        worker = "worker-%s" % self.name
        registry.upsert(self.db, worker, harness="claude", host="h1",
                        cwd=self.tmp, status="idle")
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with self.assertRaises(pilot_gesture.GestureRefused):
                work.add(self.db, title="attribué par un pilote", assignee=worker,
                         app="ameesh", cfg=self.cfg)
        rows = self.db.query("SELECT id FROM work_items WHERE title = %s",
                             ("attribué par un pilote",))
        self.assertEqual(rows, [])

    def test_work_add_race_apres_check_assignee_ne_cree_rien(self):
        worker = "worker-%s" % self.name
        registry.upsert(self.db, worker, harness="claude", host="h1",
                        cwd=self.tmp, status="idle")
        old = self.epoch
        original = work.check_assignee

        def steal(*args, **kwargs):
            check = original(*args, **kwargs)
            registry.release(self.db, self.name, "runner-h1", old)
            self.steal_to_h2()
            return check

        title = "attribué pendant la course 3013"
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(work, "check_assignee", side_effect=steal):
                with self.assertRaises(pilot_gesture.GestureRefused):
                    work.add(self.db, title=title, assignee=worker, app="ameesh",
                             cfg=self.cfg)
        self.assertEqual(self.db.query(
            "SELECT id FROM work_items WHERE title = %s", (title,)), [])

    def test_work_merged_race_apres_la_preuve_ne_declare_rien(self):
        lot = self.new_lot()
        old = self.epoch

        def steal(*args, **kwargs):
            registry.release(self.db, self.name, "runner-h1", old)
            self.steal_to_h2()
            return "a" * 40

        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(work, "_declared_merge_commit", side_effect=steal):
                with mock.patch.object(work, "close_merged",
                                       side_effect=AssertionError("déclaration atteinte")):
                    with self.assertRaises(pilot_gesture.GestureRefused):
                        work.merged(self.db, lot, sha="a" * 40, actor=self.name,
                                    cfg=self.cfg)
        self.assertEqual(work.get(self.db, lot)["state"], "intake")

    def test_work_merged_stale_refuse_avant_la_preuve(self):
        lot = self.new_lot()
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(work, "_declared_merge_commit",
                                   side_effect=AssertionError("preuve lue")):
                with self.assertRaises(pilot_gesture.GestureRefused):
                    work.merged(self.db, lot, sha="a" * 40, actor=self.name,
                                cfg=self.cfg)
        self.assertEqual(work.get(self.db, lot)["state"], "intake")

    def test_gate_ferme_sur_une_vraie_base_illisible(self):
        class BrokenDb:
            def query(self, *args, **kwargs):
                raise RuntimeError("boom")

            def execute(self, *args, **kwargs):
                raise RuntimeError("boom")

        with self.live():
            with self.assertRaises(pilot_gesture.GestureRefused) as caught:
                pilot_gesture.gate(self.cfg, BrokenDb(), "assign", "#1")
        self.assertIn("cannot read the registry", str(caught.exception))

    def test_merge_pr_exception_ne_survit_pas_a_une_bascule(self):
        lot = self.new_lot()
        head = "a" * 40
        old = self.epoch

        def steal(*args, **kwargs):
            registry.release(self.db, self.name, "runner-h1", old)
            self.steal_to_h2()
            return {"state": "MERGED", "baseRefName": "main",
                    "headRefName": "lot/3013-exception", "headRefOid": head,
                    "mergeCommit": {"oid": head}}

        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(merge_pr, "pr_view", side_effect=steal), \
                    mock.patch.object(merge_pr, "_bind_repo_identity"):
                with self.assertRaises(merge_pr.MergePrError) as caught:
                    merge_pr.direct_merge("manaty/ameesh", 7, head, lot=lot,
                                          repo=self.tmp, db=self.db, cfg=self.cfg,
                                          exception="incident",
                                          reason="test residu merge-pr")
        self.assertIn("fenced", str(caught.exception))
        self.assertIsNone(packet_guard.declared(self.db, lot, head))

    def test_merge_pr_stale_refuse_avant_github(self):
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(merge_pr, "pr_view",
                                   side_effect=AssertionError("lecture GitHub")):
                with self.assertRaises(pilot_gesture.GestureRefused):
                    merge_pr.direct_merge("manaty/ameesh", 7, "a" * 40, lot=1,
                                          repo=self.tmp, db=self.db, cfg=self.cfg)

    def test_file_stale_refuse_avant_la_preparation(self):
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(merge_queue, "readiness",
                                   side_effect=AssertionError("préparation lue")):
                with self.assertRaises(pilot_gesture.GestureRefused):
                    merge_queue.enter(self.db, 1, repo=self.tmp, cfg=self.cfg)

    def test_paquet_stale_refuse_avant_les_lectures_du_depot(self):
        lot = self.new_lot(branch="integration/paquet-test")
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(packet_link.work, "lot_repository",
                                   side_effect=AssertionError("dépôt lu")):
                with self.assertRaises(pilot_gesture.GestureRefused):
                    packet_link.record(self.db, lot, repo=self.tmp, cfg=self.cfg)

    def test_decision_stale_refuse_sans_demande(self):
        lot = self.new_lot()
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with self.assertRaises(decisions.DecisionError) as caught:
                decisions.ask(self.cfg, self.db, requester=self.name,
                              question="Quelle option ?", lot=str(lot))
        self.assertIn("fenced", str(caught.exception))
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_mailbox")[0]["n"], 0)
        self.assertEqual(work.get(self.db, lot)["state"], "intake")

    def test_decision_new_lot_stale_ne_cree_rien(self):
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with self.assertRaises(decisions.DecisionError):
                decisions.ask(self.cfg, self.db, requester=self.name,
                              question="Quelle option ?",
                              new_lot="orphelin de décision 3013")
        self.assertEqual(self.db.query(
            "SELECT id FROM work_items WHERE title = %s",
            ("orphelin de décision 3013",)), [])
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox")[0]["n"], 0)

    def test_decision_live_cree_la_demande_et_le_geste(self):
        lot = self.new_lot()
        with self.live():
            out = decisions.ask(self.cfg, self.db, requester=self.name,
                                question="Quelle option ?", lot=str(lot))
        self.assertEqual(out["lot_state"], "waiting_human")
        rows = self.events(ref="decision lot #%d" % lot)
        self.assertEqual([row["data"]["gesture"] for row in rows], ["decision"])

    def test_auto_merge_check_stale_refuse_avant_merge_into(self):
        lot = self.new_lot()
        work.note(self.db, lot,
                  "auto-merge armed repo=manaty/ameesh pr=1 sha=%s base=main "
                  "required=tests" % ("a" * 40), actor=self.name)
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        called = []
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(packet_automerge, "merge_into",
                                   side_effect=lambda *a, **k: called.append(1)):
                out = packet_automerge.check(self.cfg, self.db, lot, gh=lambda *a, **k: None)
        self.assertEqual(out["result"], "error")
        self.assertIn("fenced", out["error"])
        self.assertEqual(called, [])

    def test_auto_merge_arm_stale_refuse_avant_armement(self):
        lot = self.new_lot()
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(packet_automerge, "_arm_locked",
                                   side_effect=AssertionError("armement atteint")):
                out = packet_automerge.arm(
                    self.cfg, self.db, lot, slug="manaty/ameesh", number=1,
                    sha="a" * 40, target="main", gh=lambda *a, **k: None)
        self.assertEqual(out["state"], "error")
        self.assertIn("fenced", out["why"])

    def test_auto_merge_armed_marker_ne_survit_pas_a_une_bascule(self):
        lot = self.new_lot()
        old = self.epoch
        original = fire.required_checks

        def steal(*args, **kwargs):
            registry.release(self.db, self.name, "runner-h1", old)
            self.steal_to_h2()
            return [("tests", "app")]

        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(fire, "required_checks", side_effect=steal), \
                    mock.patch.object(packet_automerge, "pr_status",
                                      return_value={"state": "OPEN", "head": "a" * 40,
                                                    "base": "main", "auto": False,
                                                    "number": 1}):
                out = packet_automerge.arm(
                    self.cfg, self.db, lot, slug="manaty/ameesh", number=1,
                    sha="a" * 40, target="main", gh=lambda *a, **k: None)
        self.assertEqual(out["state"], "error")
        self.assertIn("lease", out["why"])
        self.assertIsNone(packet_automerge.active(packet_automerge.marks(
            self.db, lot)))

    def test_fire_exception_declaration_ne_survit_pas_a_une_bascule(self):
        lot = int(work.add(self.db, title="exception 3013", app="ameesh",
                           branch="lot/3013-exception",
                           branch_target="main")["id"])
        work.milestone(self.db, lot, "frozen", sha="a" * 40, actor=self.name)
        old = self.epoch

        def steal(*args, **kwargs):
            registry.release(self.db, self.name, "runner-h1", old)
            self.steal_to_h2()
            return True

        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with mock.patch.object(fire, "same_commit", side_effect=steal):
                with self.assertRaises(fire.FireError) as caught:
                    fire.give(self.db, lot, sha="a" * 40, actor=self.name,
                              exception="incident", reason="test residu B3")
        self.assertIn("fenced", str(caught.exception))
        self.assertIsNone(packet_guard.declared(self.db, lot, "a" * 40))

    def test_fire_stale_refuse_avant_le_jalon(self):
        lot = self.new_lot()
        old = self.epoch
        registry.release(self.db, self.name, "runner-h1", old)
        self.steal_to_h2()
        with self.identity_env(name=self.name, runner="runner-h1", epoch=old):
            with self.assertRaises(fire.FireError) as caught:
                fire.give(self.db, lot, sha="a" * 40, actor=self.name)
        self.assertIn("fenced", str(caught.exception))
        marks = work.milestones(self.db, lot)
        self.assertFalse([m for m in marks if m.get("kind") == fire.FIRE])


class NoElectedRoleTest(PgTestCase):
    """A host-pinned agent's expired session keeps today's behaviour."""

    def test_bail_expire_d_un_agent_hote_ne_bloque_pas_assign(self):
        registry.upsert(self.db, "ordinaire", harness="claude", host="h1",
                        cwd=self.tmp, status="idle")
        registry.upsert(self.db, "worker", harness="claude", host="h1",
                        cwd=self.tmp, status="idle")
        lease = registry.claim(self.db, "ordinaire", "runner-h1", 600)
        lot = int(work.add(self.db, title="lot hôte", app="ameesh")["id"])
        with mock.patch.dict(os.environ):
            for key in IDENTITY_KEYS:
                os.environ.pop(key, None)
            os.environ["AGENT_MAIL_NAME"] = "ordinaire"
            os.environ["AMEESH_RUNNER_ID"] = "runner-h1"
            os.environ["AMEESH_LEASE_EPOCH"] = str(int(lease["lease_epoch"]))
            self.db.execute(
                "UPDATE agent_registry SET lease_expires_at = clock_timestamp() "
                "- interval '5 seconds' WHERE name = 'ordinaire'")
            out = work.assign(self.db, lot, "worker", cfg=self.cfg)
        self.assertEqual(out["item"]["assignee"], "worker")


if __name__ == "__main__":
    unittest.main()
