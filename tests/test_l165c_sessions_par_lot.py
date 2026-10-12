# SPDX-License-Identifier: AGPL-3.0-only
"""L165b (#408) : un agent au repos n'attend jamais une relecture.

Une session par (agent, lot), mise de côté quand l'agent passe à un autre lot
et reprise (`--resume`) quand un retour arrive sur le premier ; réveil de
l'exécuteur sur un lot actionnable (règle 1), borné et gardé en base.
Postgres réel (les deux pilotes, AMEESH_DRIVER) ; faux harnais du banc."""
from __future__ import annotations

import os
import time
from unittest import mock

from ameesh import mail, registry, storage, work
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE agent_lot_sessions, work_items, agent_registry CASCADE")

    def _cwd(self, name: str) -> str:
        path = os.path.join(self.tmp, "work", name)
        os.makedirs(path, exist_ok=True)
        return path

    def bail(self, name: str):
        runner = Runner(self.cfg, self.db, once=True)
        self.register(name, "claude", cwd=self._cwd(name))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        return runner, lease

    @property
    def st(self):
        return storage.of(self.db).lot_sessions


class StockageTest(_Base):
    def test_bascule_met_de_cote_puis_reprend(self):
        runner, lease = self.bail("a1")
        a = work.add(self.db, title="A", assignee="a1")["id"]
        b = work.add(self.db, title="B", assignee="a1")["id"]
        epoch = lease["lease_epoch"]
        storage.of(self.db).operations.set_session_work_item("a1", runner.runner_id, epoch,
                                                             str(a))
        registry.set_session(self.db, "a1", "sess-a", "compte")
        cle = dict(harness="claude", account="compte", cwd="/w")
        out = self.st.switch("a1", runner.runner_id, epoch, to_lot=str(b),
                             current="sess-a", current_account="compte", **cle)
        self.assertEqual(out, {"resumed": None, "parked": "sess-a", "discarded": None})
        agent = registry.get(self.db, "a1")
        self.assertEqual((agent["session_id"], agent["session_work_item"]), (None, str(b)))
        registry.set_session(self.db, "a1", "sess-b", "compte")
        out = self.st.switch("a1", runner.runner_id, epoch, to_lot=str(a),
                             current="sess-b", current_account="compte", **cle)
        self.assertEqual(out, {"resumed": "sess-a", "parked": "sess-b", "discarded": None})
        agent = registry.get(self.db, "a1")
        self.assertEqual((agent["session_id"], agent["session_work_item"],
                          agent["session_account"]), ("sess-a", str(a), "compte"))
        lignes = {r["work_item_id"]: r["session_id"] for r in self.st.listing("a1")}
        self.assertEqual(lignes, {a: None, b: "sess-b"})

    def test_compte_harnais_ou_dossier_differents_ecartent_la_session(self):
        runner, lease = self.bail("a2")
        a = work.add(self.db, title="A", assignee="a2")["id"]
        b = work.add(self.db, title="B", assignee="a2")["id"]
        epoch = lease["lease_epoch"]
        for i, autre in enumerate(({"account": "autre"}, {"harness": "codex"},
                                   {"cwd": "/ailleurs"})):
            with self.subTest(autre=autre):
                cle = dict(harness="claude", account="compte", cwd="/w")
                storage.of(self.db).operations.set_session_work_item(
                    "a2", runner.runner_id, epoch, str(a))
                self.st.switch("a2", runner.runner_id, epoch, to_lot=str(b),
                               current="sess-%d" % i, current_account="compte",
                               **cle)
                cle.update(autre)
                out = self.st.switch("a2", runner.runner_id, epoch, to_lot=str(a),
                                     current=None, **cle)
                self.assertEqual((out["resumed"], out["discarded"]), (None, "sess-%d" % i))
                self.assertIsNone(registry.get(self.db, "a2")["session_id"])

    def test_bascule_fencee_par_le_bail(self):
        runner, lease = self.bail("a3")
        a = work.add(self.db, title="A", assignee="a3")["id"]
        self.assertIsNone(self.st.switch("a3", "autre", lease["lease_epoch"], to_lot=str(a),
                                         harness="claude", account=None, cwd="/w",
                                         current="s"))
        self.assertIsNone(self.st.switch("a3", runner.runner_id, lease["lease_epoch"] + 1,
                                         to_lot=str(a), harness="claude", account=None,
                                         cwd="/w", current="s"))
        self.assertEqual(self.st.listing("a3"), [])

    def test_purge_des_lots_finis_ou_reassignes(self):
        runner, lease = self.bail("a4")
        a = work.add(self.db, title="A", assignee="a4")["id"]
        b = work.add(self.db, title="B", assignee="a4")["id"]
        c = work.add(self.db, title="C", assignee="a4")["id"]
        for lot in (a, b, c):
            self.st.note_wake("a4", lot, time.time())
        work.close(self.db, a, abandoned=True, actor="test")
        work.assign(self.db, b, "human:quelqu-un")
        purges = {r["work_item_id"] for r in self.st.purge()}
        self.assertEqual(purges, {a, b})
        self.assertEqual([r["work_item_id"] for r in self.st.listing("a4")], [c])

    def test_reveils_comptes_jusqu_a_ce_que_le_lot_vive(self):
        self.bail("a5")
        a = work.add(self.db, title="A", assignee="a5")["id"]
        t = time.time()
        self.assertEqual(self.st.note_wake("a5", a, t)["wakes"], 1)
        self.assertEqual(self.st.note_wake("a5", a, t)["wakes"], 2)
        self.assertEqual(self.st.note_wake("a5", a, t + 0.0005)["wakes"], 3, "arrondi")
        self.assertEqual(self.st.note_wake("a5", a, t + 5)["wakes"], 1)

    def test_lots_actionnables(self):
        self.bail("a6")
        def lot(title, **kw):
            return work.add(self.db, title=title, assignee="a6", **kw)["id"]
        bas = lot("priorité basse", priority=3)
        haut = lot("priorité haute", priority=1)
        build = lot("en build")
        work.move(self.db, build, "build")
        qa = lot("en qa")
        work.move(self.db, qa, "build")
        work.move(self.db, qa, "qa")
        tenu = lot("tenu")
        work.note(self.db, tenu, "hold: attend la fin de #1", actor="a6")
        repris = lot("tenu puis repris")
        work.note(self.db, repris, "hold: un temps", actor="a6")
        work.note(self.db, repris, "reprise", actor="a6")
        bloque = lot("attend un autre lot")
        bloqueur = work.add(self.db, title="bloqueur")["id"]
        from ameesh import work_dependencies
        work_dependencies.add(self.db, bloque, bloqueur, actor="test")
        decision = lot("attend une décision")
        storage.of(self.db).decisions.create(
            sender="a6", recipient="human:*", body="faut-il ?",
            payload={"decision": {"v": 1, "state": "pending", "question": "faut-il ?",
                                  "options": [], "lot": decision,
                                  "previous_state": "intake"}},
            work_item_id=str(decision), host=None)
        demain = lot("pour plus tard")
        self.db.execute("UPDATE work_items SET planned_start = '2999-01-01' WHERE id = %s",
                        (demain,))
        ids = [r["id"] for r in self.st.actionable("a6")]
        self.assertEqual(ids[:2], [haut, bas])
        self.assertEqual(set(ids), {haut, bas, build, repris})
        self.assertNotIn(demain, ids)
        self.assertTrue(all(r["activity_ts"] for r in self.st.actionable("a6")))


class RoutageTest(_Base):
    def _worker(self, name):
        runner, lease = self.bail(name)
        return AgentWorker(runner, registry.get(self.db, name), lease)

    def _env(self, session):
        return mock.patch.dict(os.environ, {
            "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
            "AMEESH_SESSION_STORE_CHECK": "0", "AMEESH_TEST_NEW_SESSION": session})

    def tour(self, worker, lot, texte, session="claude-sess-x"):
        mail.send(self.db, "orch", worker.name, texte, work_item_id=str(lot))
        with self._env(session):
            spec = worker.rotate_for_lot(worker.pick())
            self.assertEqual(spec["lot"], str(lot))
            self.assertTrue(worker.run_turn(spec))
        return self.turns()[-1]["argv"]

    def test_relecture_puis_retour_reprend_la_session_du_lot(self):
        worker = self._worker("w1")
        try:
            a = work.add(self.db, title="A", assignee="w1")["id"]
            work.move(self.db, a, "build")
            argv = self.tour(worker, a, "commence A", session="sess-A")
            self.assertNotIn("--resume", argv)
            self.assertEqual(registry.get(self.db, "w1")["session_id"], "sess-A")
            work.move(self.db, a, "qa")                    # A part en relecture
            b = work.add(self.db, title="B", assignee="w1")["id"]
            argv = self.tour(worker, b, "prends B", session="sess-B")
            self.assertNotIn("--resume", argv)             # session distincte
            self.assertIn("dedicated to lot #%d" % b, " ".join(argv))
            agent = registry.get(self.db, "w1")
            self.assertEqual((agent["session_id"], agent["session_work_item"]),
                             ("sess-B", str(b)))
            argv = self.tour(worker, a, "retour de relecture sur A")
            self.assertEqual(argv[argv.index("--resume") + 1], "sess-A")
            self.assertNotIn("dedicated to lot", " ".join(argv))
            argv = self.tour(worker, b, "suite de B")
            self.assertEqual(argv[argv.index("--resume") + 1], "sess-B")
            lignes = {r["work_item_id"]: r["session_id"] for r in self.st.listing("w1")}
            self.assertEqual(lignes, {a: "sess-A", b: None})
        finally:
            worker.watchdog_stop.set()

    def test_le_lot_du_tour_est_l_unique_lot_actionnable(self):
        worker = self._worker("w2")
        try:
            a = work.add(self.db, title="A", assignee="w2")["id"]
            work.move(self.db, a, "build")
            work.move(self.db, a, "qa")
            b = work.add(self.db, title="B", assignee="w2")["id"]
            registry.set_pending_prompt(self.db, "w2", "continue")
            self.assertEqual(worker.pick()["lot"], str(b))
        finally:
            worker.watchdog_stop.set()


class ReveilTest(_Base):
    """Règle 1 : l'exécuteur réveille l'agent au repos qui détient un lot
    actionnable ; borné, gardé en base, désactivable."""

    def _worker(self, name, **cfg_extra):
        import dataclasses
        cfg = dataclasses.replace(self.cfg, **cfg_extra) if cfg_extra else self.cfg
        runner = Runner(cfg, self.db, once=True)
        self.register(name, "claude", cwd=self._cwd(name))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        worker.last_activity = time.monotonic() - 10_000   # au repos
        worker.nudged = True        # la relance d'inactivité unique (L11) est passée
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def pick(self, worker):
        worker._lot_wake_checked = float("-inf")
        return worker.pick()

    def recule(self, name, lot, secondes):
        self.db.execute("UPDATE agent_lot_sessions SET last_wake_at = last_wake_at"
                        " - make_interval(secs => %s) WHERE agent = %s AND work_item_id = %s",
                        (float(secondes), name, lot))

    def test_reveil_sur_le_lot_actionnable(self):
        worker = self._worker("r1")
        qa = work.add(self.db, title="en relecture", assignee="r1")["id"]
        work.move(self.db, qa, "build")
        work.move(self.db, qa, "qa")
        self.assertIsNone(self.pick(worker), "un lot en qa ne réveille pas")
        b = work.add(self.db, title="à faire", assignee="r1")["id"]
        spec = self.pick(worker)
        self.assertEqual((spec["kind"], spec["lot"]), ("idle", str(b)))
        self.assertIn("Resuming lot #%d" % b, spec["prompt"])
        self.assertIn('hold: <reason>', spec["prompt"])
        self.assertIsNone(self.pick(worker), "pas deux réveils de suite")

    def test_backoff_en_base_survit_au_redemarrage(self):
        worker = self._worker("r2")
        b = work.add(self.db, title="à faire", assignee="r2")["id"]
        self.assertIsNotNone(self.pick(worker))
        relance = self._worker("r2")    # nouvel exécuteur, même agent
        relance.runner.idle_nudge = 100_000.0    # plafond du délai doublé
        self.assertIsNone(self.pick(relance))
        base = relance.mail_batch() + relance.cfg.lot_wake_margin
        self.recule("r2", b, 2 * base - 30)
        self.assertIsNone(self.pick(relance), "délai doublé")
        self.recule("r2", b, 31)
        self.assertEqual(self.pick(relance)["lot"], str(b))
        for _ in range(3):
            self.recule("r2", b, 100_000)
            self.pick(relance)
        self.recule("r2", b, 100_000)
        self.assertEqual(self.st.listing("r2")[0]["wakes"], 3)
        self.assertIsNone(self.pick(relance), "au plus lot_wake_max réveils sans effet")
        work.note(self.db, b, "avancé", actor="r2")       # le lot vit : le compte repart
        self.assertEqual(self.pick(relance)["lot"], str(b))
        self.assertEqual(self.st.listing("r2")[0]["wakes"], 1)

    def test_desactivable_par_agent_et_par_hote(self):
        worker = self._worker("r3", lot_wake=False)
        work.add(self.db, title="à faire", assignee="r3")
        self.assertIsNone(self.pick(worker))
        storage.of(self.db).operations.set_settings("r3", {"lot_wake": "on"})
        self.assertIsNotNone(self.pick(worker), "l'agent l'emporte sur l'hôte")
        autre = self._worker("r4")
        work.add(self.db, title="à faire", assignee="r4")
        storage.of(self.db).operations.set_settings("r4", {"lot_wake": "off"})
        self.assertIsNone(self.pick(autre))

    def test_pas_de_reveil_sans_repos_ni_pendant_le_courrier(self):
        worker = self._worker("r5")
        work.add(self.db, title="à faire", assignee="r5")
        worker.last_activity = time.monotonic()
        self.assertIsNone(self.pick(worker), "pas encore au repos")
        worker.last_activity = time.monotonic() - 10_000
        worker.runner.mail_batch = 3600.0
        mail.send(self.db, "orch", "r5", "un message en fenêtre de regroupement")
        self.assertIsNone(self.pick(worker), "le courrier réveillera, pas la règle 1")

    def test_lot_tenu_ne_reveille_pas(self):
        worker = self._worker("r6")
        b = work.add(self.db, title="à faire", assignee="r6")["id"]
        work.note(self.db, b, "hold: attend l'hôte de test", actor="r6")
        self.assertIsNone(self.pick(worker))


class PurgeTest(_Base):
    def test_le_releve_des_branches_purge_les_lots_finis(self):
        runner, lease = self.bail("p1")
        a = work.add(self.db, title="A", assignee="p1")["id"]
        b = work.add(self.db, title="B", assignee="p1")["id"]
        for lot in (a, b):
            self.st.note_wake("p1", lot, time.time())
        work.close(self.db, a, abandoned=True, actor="test")
        purges = runner.lot_sessions_purge(self.db)
        self.assertEqual([r["work_item_id"] for r in purges], [a])
        self.assertEqual([r["work_item_id"] for r in self.st.listing("p1")], [b])


class AlerteTest(_Base):
    """Règle 3 : l'auditeur relance après au moins un réveil sans effet."""

    def alertes(self):
        from ameesh import exploitation
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "idle_actionable_lot"]

    def test_seulement_apres_un_reveil_sans_effet(self):
        from ameesh import alert_briefs
        self.bail("x1")
        a = work.add(self.db, title="A", assignee="x1")["id"]
        self.assertEqual(self.alertes(), [], "sans réveil, la règle 1 agit encore")
        activite = self.st.actionable("x1")[0]["activity_ts"]
        self.st.note_wake("x1", a, activite)
        self.assertEqual(self.alertes(), [], "réveil trop récent")
        self.db.execute("UPDATE agent_lot_sessions SET last_wake_at = now() - interval '1 hour'")
        (alerte,) = self.alertes()
        self.assertEqual((alerte["agent"], alerte["lot"], alerte["wakes"]), ("x1", a, 1))
        textes = alert_briefs.briefs(alerte, storage.of(self.db).operations.listing(),
                                     auditor_names=("x1",))
        self.assertIn("level 1", textes["x1"])
        self.assertIn("--lot %d" % a, textes["x1"])
        work.note(self.db, a, "avancé", actor="x1")         # le lot a vécu
        self.assertEqual(self.alertes(), [])
