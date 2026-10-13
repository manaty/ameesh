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

    def origine(self, name: str) -> tuple:
        row = self.db.query("SELECT session_harness, session_cwd, session_origin"
                            "  FROM agent_registry WHERE name = %s", (name,))[0]
        return (row["session_harness"], row["session_cwd"], row["session_origin"])

    @property
    def st(self):
        return storage.of(self.db).lot_sessions


#: #540 : plusieurs lots non démarrés au même agent, forcés
_FORCE = "test: several lots for one agent (#540)"


class StockageTest(_Base):
    def test_bascule_met_de_cote_puis_reprend(self):
        runner, lease = self.bail("a1")
        a = work.add(self.db, title="A", assignee="a1", force=_FORCE)["id"]
        b = work.add(self.db, title="B", assignee="a1", force=_FORCE)["id"]
        epoch = lease["lease_epoch"]
        storage.of(self.db).operations.set_session_work_item("a1", runner.runner_id, epoch,
                                                             str(a))
        registry.set_session(self.db, "a1", "sess-a", "compte", harness="claude", cwd="/w")
        cle = dict(harness="claude", account="compte", cwd="/w")
        out = self.st.switch("a1", runner.runner_id, epoch, to_lot=str(b),
                             current="sess-a", **cle)
        self.assertEqual(out, {"resumed": None, "parked": "sess-a", "discarded": None,
                               "discard_reason": None})
        agent = registry.get(self.db, "a1")
        self.assertEqual((agent["session_id"], agent["session_work_item"]), (None, str(b)))
        registry.set_session(self.db, "a1", "sess-b", "compte", harness="claude", cwd="/w")
        out = self.st.switch("a1", runner.runner_id, epoch, to_lot=str(a),
                             current="sess-b", **cle)
        self.assertEqual(out, {"resumed": "sess-a", "parked": "sess-b", "discarded": None,
                               "discard_reason": None})
        agent = registry.get(self.db, "a1")
        self.assertEqual((agent["session_id"], agent["session_work_item"],
                          agent["session_account"]), ("sess-a", str(a), "compte"))
        self.assertEqual(self.origine("a1"), ("claude", "/w", "sess-a"))
        lignes = {r["work_item_id"]: r["session_id"] for r in self.st.listing("a1")}
        self.assertEqual(lignes, {a: None, b: "sess-b"})

    def test_compte_harnais_ou_dossier_differents_ecartent_la_session(self):
        runner, lease = self.bail("a2")
        a = work.add(self.db, title="A", assignee="a2", force=_FORCE)["id"]
        b = work.add(self.db, title="B", assignee="a2", force=_FORCE)["id"]
        epoch = lease["lease_epoch"]
        for i, autre in enumerate(({"account": "autre"}, {"harness": "codex"},
                                   {"cwd": "/ailleurs"})):
            with self.subTest(autre=autre):
                cle = dict(harness="claude", account="compte", cwd="/w")
                storage.of(self.db).operations.set_session_work_item(
                    "a2", runner.runner_id, epoch, str(a))
                registry.set_session(self.db, "a2", "sess-%d" % i, "compte",
                                     harness="claude", cwd="/w")
                out = self.st.switch("a2", runner.runner_id, epoch, to_lot=str(b),
                                     current="sess-%d" % i, **cle)
                self.assertEqual(out["parked"], "sess-%d" % i)
                cle.update(autre)
                out = self.st.switch("a2", runner.runner_id, epoch, to_lot=str(a),
                                     current=None, **cle)
                self.assertEqual((out["resumed"], out["discarded"], out["discard_reason"]),
                                 (None, "sess-%d" % i, "origin"))
                self.assertIsNone(registry.get(self.db, "a2")["session_id"])

    def test_origine_notee_a_l_ouverture_pas_a_la_mise_de_cote(self):
        """r1 #420 : A ouverte dans /old, le placement passe à /new, la bascule
        vers B met A de côté avec /old ; le retour sur A dans /new ne la
        reprend pas. Une session d'origine inconnue n'est jamais reprise."""
        runner, lease = self.bail("a6")
        a = work.add(self.db, title="A", assignee="a6", force=_FORCE)["id"]
        b = work.add(self.db, title="B", assignee="a6", force=_FORCE)["id"]
        epoch = lease["lease_epoch"]
        storage.of(self.db).operations.set_session_work_item("a6", runner.runner_id, epoch,
                                                             str(a))
        registry.set_session(self.db, "a6", "sess-a", None, harness="claude", cwd="/old")
        neuf = dict(harness="claude", account=None, cwd="/new")
        self.assertEqual(self.st.switch("a6", runner.runner_id, epoch, to_lot=str(b),
                                        current="sess-a", **neuf)["parked"], "sess-a")
        ligne = {r["work_item_id"]: r for r in self.st.listing("a6")}[a]
        self.assertEqual((ligne["harness"], ligne["cwd"]), ("claude", "/old"))
        out = self.st.switch("a6", runner.runner_id, epoch, to_lot=str(a),
                             current=None, **neuf)
        self.assertEqual((out["resumed"], out["discarded"]), (None, "sess-a"))
        # session sans origine connue (écrite sans harnais ni dossier) : pas reprise
        registry.set_session(self.db, "a6", "sess-x", None)
        self.st.switch("a6", runner.runner_id, epoch, to_lot=str(b), current="sess-x", **neuf)
        out = self.st.switch("a6", runner.runner_id, epoch, to_lot=str(a), current=None,
                             **neuf)
        self.assertEqual((out["resumed"], out["discarded"]), (None, "sess-x"))
        # l'origine décrit la session du registre seulement : une autre
        # session courante (fichier local) est mise de côté sans origine
        registry.set_session(self.db, "a6", "sess-y", None, harness="claude", cwd="/new")
        self.st.switch("a6", runner.runner_id, epoch, to_lot=str(b), current="sess-z", **neuf)
        out = self.st.switch("a6", runner.runner_id, epoch, to_lot=str(a), current=None,
                             **neuf)
        self.assertEqual((out["resumed"], out["discarded"]), (None, "sess-z"))

    def test_reprise_garde_l_origine_de_l_ouverture(self):
        """r2 #420 : une session reprise (même id annoncé) sans origine donnée
        garde celle de son ouverture ; un autre id la rend inconnue."""
        self.bail("a8")
        registry.set_session(self.db, "a8", "S", "X", harness="claude", cwd="/old")
        registry.set_session(self.db, "a8", "S", "Y")
        self.assertEqual(self.origine("a8"), ("claude", "/old", "S"))
        registry.set_session(self.db, "a8", "T", "Y")
        self.assertEqual(self.origine("a8"), (None, None, None))

    def test_tout_autre_writer_de_session_invalide_l_origine(self):
        """r2 #420 : S ouverte sous X ; `upsert(session_id=T)` (ou tout autre
        chemin qui écrit session_id) rend l'origine inconnue ; un exécuteur
        qui tient encore S la met de côté sans origine : jamais reprise."""
        runner, lease = self.bail("a9")
        epoch = lease["lease_epoch"]
        a = work.add(self.db, title="A", assignee="a9", force=_FORCE)["id"]
        b = work.add(self.db, title="B", assignee="a9", force=_FORCE)["id"]
        storage.of(self.db).operations.set_session_work_item("a9", runner.runner_id, epoch,
                                                             str(a))
        registry.set_session(self.db, "a9", "S", "X", harness="claude", cwd="/w")
        registry.upsert(self.db, "a9", session_id="T")
        self.assertEqual(self.origine("a9"), (None, None, None))
        compte = registry.get(self.db, "a9")["session_account"]
        cle = dict(harness="claude", account=compte, cwd="/w")
        self.assertEqual(self.st.switch("a9", runner.runner_id, epoch, to_lot=str(b),
                                        current="S", **cle)["parked"], "S")
        out = self.st.switch("a9", runner.runner_id, epoch, to_lot=str(a), current=None,
                             **cle)
        self.assertEqual((out["resumed"], out["discarded"]), (None, "S"))
        # toute écriture directe de session_id aussi (déclencheur de 0384)
        registry.set_session(self.db, "a9", "U", None, harness="claude", cwd="/w")
        self.db.execute("UPDATE agent_registry SET session_id = 'V' WHERE name = 'a9'")
        self.assertEqual(self.origine("a9"), (None, None, None))

    def test_lot_fini_ou_reassigne_jamais_repris_ni_mis_de_cote(self):
        """r1 #420 : un courrier `--lot A` après la fusion, la clôture ou la
        réassignation de A, avant le relevé des branches, ne reprend pas sa
        session ; un lot fini n'est pas mis de côté (pas de ligne recréée)."""
        runner, lease = self.bail("a7")
        epoch = lease["lease_epoch"]
        cle = dict(harness="claude", account=None, cwd="/w")
        b = work.add(self.db, title="B", assignee="a7", force=_FORCE)["id"]
        for fin in ("close", "assign"):
            with self.subTest(fin=fin):
                a = work.add(self.db, title="A", assignee="a7", force=_FORCE)["id"]
                storage.of(self.db).operations.set_session_work_item(
                    "a7", runner.runner_id, epoch, str(a))
                registry.set_session(self.db, "a7", "sess-" + fin, None, **{
                    k: cle[k] for k in ("harness", "cwd")})
                self.assertEqual(self.st.switch("a7", runner.runner_id, epoch, to_lot=str(b),
                                                current="sess-" + fin, **cle)["parked"],
                                 "sess-" + fin)
                if fin == "close":
                    work.close(self.db, a, abandoned=True, actor="test")
                else:
                    work.assign(self.db, a, "human:quelqu-un")
                out = self.st.switch("a7", runner.runner_id, epoch, to_lot=str(a),
                                     current=None, **cle)
                self.assertEqual((out["resumed"], out["discarded"], out["discard_reason"]),
                                 (None, "sess-" + fin, "lot"))
                self.assertIsNone(registry.get(self.db, "a7")["session_id"])
                # la session ouverte sur le lot fini n'est pas mise de côté
                registry.set_session(self.db, "a7", "sess-2-" + fin, None, **{
                    k: cle[k] for k in ("harness", "cwd")})
                out = self.st.switch("a7", runner.runner_id, epoch, to_lot=str(b),
                                     current="sess-2-" + fin, **cle)
                self.assertIsNone(out["parked"])
                lignes = {r["work_item_id"]: r["session_id"] for r in self.st.listing("a7")}
                self.assertIsNone(lignes.get(a))

    def test_bascule_fencee_par_le_bail(self):
        runner, lease = self.bail("a3")
        a = work.add(self.db, title="A", assignee="a3", force=_FORCE)["id"]
        self.assertIsNone(self.st.switch("a3", "autre", lease["lease_epoch"], to_lot=str(a),
                                         harness="claude", account=None, cwd="/w",
                                         current="s"))
        self.assertIsNone(self.st.switch("a3", runner.runner_id, lease["lease_epoch"] + 1,
                                         to_lot=str(a), harness="claude", account=None,
                                         cwd="/w", current="s"))
        self.assertEqual(self.st.listing("a3"), [])

    def test_purge_des_lots_finis_ou_reassignes(self):
        runner, lease = self.bail("a4")
        a = work.add(self.db, title="A", assignee="a4", force=_FORCE)["id"]
        b = work.add(self.db, title="B", assignee="a4", force=_FORCE)["id"]
        c = work.add(self.db, title="C", assignee="a4", force=_FORCE)["id"]
        for lot in (a, b, c):
            self.st.note_wake("a4", lot, time.time())
        work.close(self.db, a, abandoned=True, actor="test")
        work.assign(self.db, b, "human:quelqu-un")
        purges = {r["work_item_id"] for r in self.st.purge()}
        self.assertEqual(purges, {a, b})
        self.assertEqual([r["work_item_id"] for r in self.st.listing("a4")], [c])

    def test_reveils_comptes_jusqu_a_ce_que_le_lot_vive(self):
        self.bail("a5")
        a = work.add(self.db, title="A", assignee="a5", force=_FORCE)["id"]
        t = time.time()
        self.assertEqual(self.st.note_wake("a5", a, t)["wakes"], 1)
        self.assertEqual(self.st.note_wake("a5", a, t)["wakes"], 2)
        self.assertEqual(self.st.note_wake("a5", a, t + 0.0005)["wakes"], 3, "arrondi")
        self.assertEqual(self.st.note_wake("a5", a, t + 5)["wakes"], 1)

    def test_lots_actionnables(self):
        self.bail("a6")
        def lot(title, **kw):
            return work.add(self.db, title=title, assignee="a6", force=_FORCE, **kw)["id"]
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
            a = work.add(self.db, title="A", assignee="w1", force=_FORCE)["id"]
            work.move(self.db, a, "build")
            argv = self.tour(worker, a, "commence A", session="sess-A")
            self.assertNotIn("--resume", argv)
            self.assertEqual(registry.get(self.db, "w1")["session_id"], "sess-A")
            work.move(self.db, a, "qa")                    # A part en relecture
            b = work.add(self.db, title="B", assignee="w1", force=_FORCE)["id"]
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

    def test_placement_change_apres_ouverture_session_neuve(self):
        """r1 #420 : A ouverte dans un dossier, le placement change avant la
        bascule vers B : le retour sur A ne reprend pas sa session ailleurs."""
        worker = self._worker("w3")
        try:
            a = work.add(self.db, title="A", assignee="w3", force=_FORCE)["id"]
            work.move(self.db, a, "build")
            self.tour(worker, a, "commence A", session="sess-A")
            self.assertEqual(self.origine("w3"), ("claude", self._cwd("w3"), "sess-A"))
            self.db.execute("UPDATE agent_registry SET cwd = %s WHERE name = %s",
                            (self._cwd("w3-new"), "w3"))
            work.move(self.db, a, "qa")
            b = work.add(self.db, title="B", assignee="w3", force=_FORCE)["id"]
            self.tour(worker, b, "prends B", session="sess-B")
            argv = self.tour(worker, a, "retour de relecture sur A", session="sess-A2")
            self.assertNotIn("--resume", argv)
            self.assertIn("dedicated to lot #%d" % a, " ".join(argv))
        finally:
            worker.watchdog_stop.set()

    def test_tour_de_reprise_n_ecrase_pas_l_origine(self):
        """r2 #420 : A ouverte dans un dossier, le placement change, un autre
        tour sur A la reprend (même id annoncé) : l'origine reste celle de
        l'ouverture, et le retour A→B→A dans le nouveau dossier ne la reprend
        pas."""
        worker = self._worker("w4")
        try:
            a = work.add(self.db, title="A", assignee="w4", force=_FORCE)["id"]
            work.move(self.db, a, "build")
            self.tour(worker, a, "commence A", session="sess-A")
            self.db.execute("UPDATE agent_registry SET cwd = %s WHERE name = %s",
                            (self._cwd("w4-new"), "w4"))
            argv = self.tour(worker, a, "suite de A", session="jamais")
            self.assertEqual(argv[argv.index("--resume") + 1], "sess-A")
            self.assertEqual(self.origine("w4"), ("claude", self._cwd("w4"), "sess-A"))
            work.move(self.db, a, "qa")
            b = work.add(self.db, title="B", assignee="w4", force=_FORCE)["id"]
            self.tour(worker, b, "prends B", session="sess-B")
            argv = self.tour(worker, a, "retour sur A", session="sess-A2")
            self.assertNotIn("--resume", argv)
            self.assertIn("dedicated to lot #%d" % a, " ".join(argv))
        finally:
            worker.watchdog_stop.set()

    def _emprunt(self, name, parquee_sous):
        """Un agent claude qui emprunte codex (L242b) passe de B à A, dont la
        session a été mise de côté sous `parquee_sous`."""
        worker = self._worker(name)
        self.addCleanup(worker.watchdog_stop.set)
        a = work.add(self.db, title="A", assignee=name, force=_FORCE)["id"]
        b = work.add(self.db, title="B", assignee=name, force=_FORCE)["id"]
        epoch = worker.epoch
        ops = storage.of(self.db).operations
        cwd = registry.get(self.db, name)["cwd"]
        ops.set_session_work_item(name, worker.runner.runner_id, epoch, str(a))
        registry.set_session(self.db, name, "sess-A", None, harness=parquee_sous, cwd=cwd)
        self.st.switch(name, worker.runner.runner_id, epoch, to_lot=str(b),
                       harness=parquee_sous, account=None, cwd=cwd, current="sess-A")
        worker.agent = registry.get(self.db, name)
        worker._state_note("session_harness", "claude")
        with mock.patch.object(type(worker), "turn_harness", lambda self: "codex"), \
                mock.patch.object(type(worker), "current_session", lambda self: None):
            self.assertTrue(worker._switch_lot(str(b), str(a)))
        return worker

    def test_emprunt_de_harnais_ne_reprend_pas_une_session_claude(self):
        """r4 #420 : agent claude qui emprunte codex, session claude mise de
        côté pour A : jamais lancée sous codex (écartée, session neuve)."""
        worker = self._emprunt("w5", "claude")
        self.assertIsNone(registry.get(self.db, "w5")["session_id"])
        self.assertIsNone(worker.read_session_file())
        self.assertEqual(worker._state_read("session_harness"), "")

    def test_emprunt_de_harnais_reprend_la_session_codex(self):
        """r4 #420 : l'inverse : la session de A a été ouverte sous codex
        (emprunté) ; l'agent emprunte encore codex : elle est reprise, et le
        marqueur local du harnais de la session dit codex."""
        worker = self._emprunt("w6", "codex")
        self.assertEqual(registry.get(self.db, "w6")["session_id"], "sess-A")
        self.assertEqual(worker.read_session_file(), "sess-A")
        self.assertEqual(worker._state_read("session_harness"), "codex")
        from ameesh import account_turn
        self.assertEqual(account_turn.session_harness(worker), "codex")

    def test_le_lot_du_tour_est_l_unique_lot_actionnable(self):
        worker = self._worker("w2")
        try:
            a = work.add(self.db, title="A", assignee="w2", force=_FORCE)["id"]
            work.move(self.db, a, "build")
            work.move(self.db, a, "qa")
            b = work.add(self.db, title="B", assignee="w2", force=_FORCE)["id"]
            registry.set_pending_prompt(self.db, "w2", "continue")
            self.assertEqual(worker.pick()["lot"], str(b))
        finally:
            worker.watchdog_stop.set()

    def test_session_sans_lot_n_est_jamais_adoptee_par_un_lot(self):
        """#794 : une session ouverte par un tour dont le lot est inconnu (ici
        deux lots actifs, aucune consigne ne tranche) n'est jamais adoptée par
        le premier lot venu ; ce lot ouvre SA session, sur le brief minimal."""
        worker = self._worker("w7")
        try:
            a = work.add(self.db, title="A", assignee="w7")["id"]
            work.add(self.db, title="B", assignee="w7")
            registry.set_pending_prompt(self.db, "w7", "veille : rien de neuf")
            with self._env("sess-veille"):
                spec = worker.rotate_for_lot(worker.pick())
                self.assertIsNone(spec["lot"], "deux lots actifs : lot inconnu")
                self.assertTrue(worker.run_turn(spec))
            agent = registry.get(self.db, "w7")
            self.assertEqual((agent["session_id"], agent["session_work_item"]),
                             ("sess-veille", None))
            mail.send(self.db, "orch", "w7", "prends A", work_item_id=str(a))
            with self._env("sess-A"):
                spec = worker.rotate_for_lot(worker.pick())
                self.assertEqual(spec["lot"], str(a))
                self.assertTrue(worker.run_turn(spec))
            argv = self.turns()[-1]["argv"]
            self.assertNotIn("--resume", argv, "la session sans lot n'est pas reprise")
            self.assertIn("dedicated to lot #%d" % a, " ".join(argv))
            agent = registry.get(self.db, "w7")
            self.assertEqual((agent["session_id"], agent["session_work_item"]),
                             ("sess-A", str(a)))
        finally:
            worker.watchdog_stop.set()


class BriefDeLotTest(_Base):
    """#794 : le brief d'une session dédiée à un lot est MINIMAL — canon,
    fiche du lot, diff de sa branche, fil du lot (décisions et messages non
    lus) — jamais un transcript, jamais un autre lot, jamais un résumé."""

    def test_fiche_diff_et_fil_du_lot_seulement(self):
        import subprocess

        from ameesh import reprise
        self.bail("b1")
        autre = work.add(self.db, title="Un autre lot", assignee="b1")["id"]
        a = work.add(self.db, title="Le lot du brief", assignee="b1")["id"]
        work.note(self.db, a, "une note du lot", actor="b1")
        mail.send(self.db, "orch", "b1", "un message non lu sur le lot",
                  work_item_id=str(a))
        mail.send(self.db, "orch", "b1", "un message pour un autre lot",
                  work_item_id=str(autre))
        cwd = self._cwd("b1")

        def git(*args):
            subprocess.run(["git", "-C", cwd, *args], check=True,
                           capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "banc@test")
        git("config", "user.name", "banc")
        with open(os.path.join(cwd, "base.txt"), "w", encoding="utf-8") as fh:
            fh.write("base\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "main")
        git("checkout", "-q", "-b", "lot/b1")
        with open(os.path.join(cwd, "travail.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        git("add", "-A")
        git("commit", "-q", "-m", "travail du lot")
        work.set_branch(self.db, a, "lot/b1", "main")

        text = reprise.lot_brief(self.cfg, self.db, registry.get(self.db, "b1"), a,
                                 consigne="go")
        self.assertIn("Session dedicated to lot #%d" % a, text)
        self.assertIn("## Lot #%d — Le lot du brief" % a, text)
        self.assertIn("une note du lot", text)
        self.assertIn("## Diff of the lot's branch", text)
        self.assertIn("lot/b1", text)
        self.assertIn("travail.py", text)
        self.assertIn("1 file changed", text)
        self.assertIn("## Lot thread — decisions and unread messages", text)
        self.assertIn("un message non lu sur le lot", text)
        # jamais le contenu d'un autre lot, ni l'ancienne session, ni la liste
        # des lots assignés du brief de reprise
        self.assertNotIn("Un autre lot", text)
        self.assertNotIn("un message pour un autre lot", text)
        self.assertNotIn("Previous session", text)
        self.assertNotIn("Work items assigned to you", text)


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
        qa = work.add(self.db, title="en relecture", assignee="r1", force=_FORCE)["id"]
        work.move(self.db, qa, "build")
        work.move(self.db, qa, "qa")
        self.assertIsNone(self.pick(worker), "un lot en qa ne réveille pas")
        b = work.add(self.db, title="à faire", assignee="r1", force=_FORCE)["id"]
        spec = self.pick(worker)
        self.assertEqual((spec["kind"], spec["lot"]), ("idle", str(b)))
        self.assertIn("Resuming lot #%d" % b, spec["prompt"])
        self.assertIn('hold: <reason>', spec["prompt"])
        self.assertIsNone(self.pick(worker), "pas deux réveils de suite")

    def test_backoff_en_base_survit_au_redemarrage(self):
        worker = self._worker("r2")
        b = work.add(self.db, title="à faire", assignee="r2", force=_FORCE)["id"]
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
        work.add(self.db, title="à faire", assignee="r3", force=_FORCE)
        self.assertIsNone(self.pick(worker))
        storage.of(self.db).operations.set_settings("r3", {"lot_wake": "on"})
        self.assertIsNotNone(self.pick(worker), "l'agent l'emporte sur l'hôte")
        autre = self._worker("r4")
        work.add(self.db, title="à faire", assignee="r4", force=_FORCE)
        storage.of(self.db).operations.set_settings("r4", {"lot_wake": "off"})
        self.assertIsNone(self.pick(autre))

    def test_pas_de_reveil_sans_repos_ni_pendant_le_courrier(self):
        worker = self._worker("r5")
        work.add(self.db, title="à faire", assignee="r5", force=_FORCE)
        worker.last_activity = time.monotonic()
        self.assertIsNone(self.pick(worker), "pas encore au repos")
        worker.last_activity = time.monotonic() - 10_000
        worker.runner.mail_batch = 3600.0
        mail.send(self.db, "orch", "r5", "un message en fenêtre de regroupement")
        self.assertIsNone(self.pick(worker), "le courrier réveillera, pas la règle 1")

    def test_lot_tenu_ne_reveille_pas(self):
        worker = self._worker("r6")
        b = work.add(self.db, title="à faire", assignee="r6", force=_FORCE)["id"]
        work.note(self.db, b, "hold: attend l'hôte de test", actor="r6")
        self.assertIsNone(self.pick(worker))


class PurgeTest(_Base):
    def test_le_releve_des_branches_purge_les_lots_finis(self):
        runner, lease = self.bail("p1")
        a = work.add(self.db, title="A", assignee="p1", force=_FORCE)["id"]
        b = work.add(self.db, title="B", assignee="p1", force=_FORCE)["id"]
        for lot in (a, b):
            self.st.note_wake("p1", lot, time.time())
        work.close(self.db, a, abandoned=True, actor="test")
        purges = runner.lot_sessions_purge(self.db)
        self.assertEqual([r["work_item_id"] for r in purges], [a])
        self.assertEqual([r["work_item_id"] for r in self.st.listing("p1")], [b])

    def test_la_purge_oublie_la_session_active_du_lot_fini(self):
        """#794 : fermer = oublier la session harnais. La session ACTIVE d'un
        lot purgé est oubliée au registre et son fichier local retiré ; celle
        d'un autre lot, ou d'un agent passé à un autre lot, reste."""
        runner, lease = self.bail("p2")
        a = work.add(self.db, title="A", assignee="p2")["id"]
        storage.of(self.db).operations.set_session_work_item(
            "p2", runner.runner_id, lease["lease_epoch"], str(a))
        registry.set_session(self.db, "p2", "sess-A", None, harness="claude",
                             cwd=self._cwd("p2"))
        chemin = os.path.join(self.cfg.agent_dir("p2"), "session")
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write("sess-A\n")
        work.close(self.db, a, abandoned=True, actor="test")
        purges = runner.lot_sessions_purge(self.db)
        self.assertEqual([r["forgotten_session"] for r in purges], ["sess-A"])
        agent = registry.get(self.db, "p2")
        self.assertEqual((agent["session_id"], agent["session_work_item"]), (None, None))
        self.assertFalse(os.path.exists(chemin), "le fichier local ne ressuscite pas")

    def test_la_purge_ne_touche_pas_un_agent_passe_a_un_autre_lot(self):
        runner, lease = self.bail("p3")
        a = work.add(self.db, title="A", assignee="p3")["id"]
        b = work.add(self.db, title="B", assignee="p3")["id"]
        storage.of(self.db).operations.set_session_work_item(
            "p3", runner.runner_id, lease["lease_epoch"], str(b))
        registry.set_session(self.db, "p3", "sess-B", None, harness="claude",
                             cwd=self._cwd("p3"))
        self.st.note_wake("p3", a, time.time())
        work.close(self.db, a, abandoned=True, actor="test")
        purges = runner.lot_sessions_purge(self.db)
        self.assertEqual([r["work_item_id"] for r in purges], [a])
        self.assertEqual([r["forgotten_session"] for r in purges], [None])
        agent = registry.get(self.db, "p3")
        self.assertEqual((agent["session_id"], agent["session_work_item"]),
                         ("sess-B", str(b)))


class AlerteTest(_Base):
    """Règle 3 : l'auditeur relance après au moins un réveil sans effet."""

    def alertes(self):
        from ameesh import exploitation
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "idle_actionable_lot"]

    def test_seulement_apres_un_reveil_sans_effet(self):
        from ameesh import alert_briefs
        self.bail("x1")
        a = work.add(self.db, title="A", assignee="x1", force=_FORCE)["id"]
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
