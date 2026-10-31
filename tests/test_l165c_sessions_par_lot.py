# SPDX-License-Identifier: AGPL-3.0-only
"""L165b (#408) : un agent au repos n'attend jamais une relecture.

Une session par (agent, lot), mise de côté quand l'agent passe à un autre lot
et reprise (`--resume`) quand un retour arrive sur le premier ; réveil de
l'exécuteur sur un lot actionnable (règle 1), borné et gardé en base.
Postgres réel (les deux pilotes, AMEESH_DRIVER) ; faux harnais du banc."""
from __future__ import annotations

import os
import subprocess
import time
import unittest
from unittest import mock

from ameesh import db as db_mod, mail, registry, storage, work
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, REPO, PgTestCase, hold_for_owner


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

    def test_mise_de_cote_sans_lot_actif(self):
        """#2055 : `to_lot=None` met la session active de côté pour son lot et
        ne rend active aucune session (`session_work_item` NULL) — le tour
        suivant part dans une session sans lot."""
        runner, lease = self.bail("a10")
        a = work.add(self.db, title="A", assignee="a10", force=_FORCE)["id"]
        epoch = lease["lease_epoch"]
        storage.of(self.db).operations.set_session_work_item("a10", runner.runner_id, epoch,
                                                             str(a))
        registry.set_session(self.db, "a10", "sess-a", "compte", harness="claude", cwd="/w")
        out = self.st.switch("a10", runner.runner_id, epoch, to_lot=None,
                             harness="claude", account="compte", cwd="/w",
                             current="sess-a")
        self.assertEqual(out, {"resumed": None, "parked": "sess-a", "discarded": None,
                               "discard_reason": None})
        agent = registry.get(self.db, "a10")
        self.assertEqual((agent["session_id"], agent["session_work_item"]), (None, None))
        self.assertEqual(self.origine("a10"), (None, None, None))
        lignes = {r["work_item_id"]: r["session_id"] for r in self.st.listing("a10")}
        self.assertEqual(lignes, {a: "sess-a"})

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
        # #3986 : la relecture attend seulement après déclaration du gel.
        work.milestone(self.db, qa, "frozen", sha="a" * 40, actor="test")
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

    def test_note_de_l_outillage_ne_leve_pas_la_retenue(self):
        """#3992 : une note écrite par l'outillage après un `hold:` (composition
        de paquet, runner, git…) ne lève pas la retenue ; celle de l'assigné
        ou d'un humain la lève, comme avant. #4162 : la note ordinaire d'un
        autre agent ne la lève plus (voir le test suivant)."""
        self.bail("a7")
        def lot(title):
            return work.add(self.db, title=title, assignee="a7", force=_FORCE)["id"]
        def tenu(title):
            item = lot(title)
            work.note(self.db, item, "hold: composition par la minuterie", actor="a7")
            return item
        mecaniques = []
        for actor in ("inconnu", "", "ameesh", "runner runner-a7@hote",
                      "git:origin/main", "composition:42"):
            item = tenu("tenu, puis %r" % actor)
            work.note(self.db, item, "packet composition recorded: 5 unit(s)",
                      actor=actor)
            mecaniques.append(item)
        par_l_assigne = tenu("levé par l'assigné")
        work.note(self.db, par_l_assigne, "reprise", actor="a7")
        self.register("pilote7", "claude")
        par_un_agent = tenu("levé par un agent")
        with mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": "pilote7"}):
            work.note(self.db, par_un_agent, "unhold: à toi", actor="pilote7")
        par_l_owner = tenu("levé par l'owner")
        work.note(self.db, par_l_owner, "go", actor="human:owner")
        ids = {r["id"] for r in self.st.actionable("a7")}
        self.assertFalse(ids & set(mecaniques), "note mécanique : retenue gardée")
        self.assertEqual(ids, {par_l_assigne, par_un_agent, par_l_owner})

    def test_decoupage_epuise_ne_reveille_plus_le_pilote(self):
        """#4901 : épuisée ne réveille pas ; rouverte réarme la règle 1."""
        self.bail("pilote4901")
        lot = work.add(self.db, title="Découper la feuille de route",
                       source="file-decoupage:ameesh", app="ameesh",
                       assignee="pilote4901", force=_FORCE)["id"]
        work.move(self.db, lot, "build")
        work.note(self.db, lot, "hold: feuille de route vide", actor="pilote4901")
        work.note(self.db, lot, "roadmap: épuisée ameesh", actor="pilote4901")
        self.assertNotIn(lot, {r["id"] for r in self.st.actionable("pilote4901")})
        # Une note ordinaire et une réouverture d'un autre projet ne changent
        # pas la dernière déclaration qui nomme ameesh.
        work.note(self.db, lot, "état confirmé", actor="pilote4901")
        work.note(self.db, lot, "roadmap: rouverte autre", actor="pilote4901")
        self.assertNotIn(lot, {r["id"] for r in self.st.actionable("pilote4901")})
        work.note(self.db, lot, "roadmap: rouverte ameesh", actor="pilote4901")
        rows = self.st.actionable("pilote4901")
        self.assertIn(lot, {r["id"] for r in rows})
        self.assertNotIn("roadmap_source", rows[0])
        work.note(self.db, lot, "roadmap: épuisée ameesh", actor="pilote4901")
        self.assertNotIn(lot, {r["id"] for r in self.st.actionable("pilote4901")})

    def test_note_epuisee_ne_suspend_pas_un_lot_ordinaire(self):
        self.bail("agent4901")
        lot = work.add(self.db, title="Travail ordinaire", app="ameesh",
                       assignee="agent4901", force=_FORCE)["id"]
        work.note(self.db, lot, "roadmap: épuisée ameesh", actor="agent4901")
        self.assertIn(lot, {r["id"] for r in self.st.actionable("agent4901")})

    def test_note_d_un_autre_agent_ne_leve_pas_la_retenue(self):
        """#4162 : la note ordinaire d'un autre agent après un `hold:` ne
        réveille pas l'assigné (même lecture que `planned_waits.active_holds`) ;
        une levée explicite, un changement d'état (même aller-retour) ou une
        note de l'assigné lèvent la retenue."""
        self.bail("a8")
        self.register("pilote8", "claude")
        def tenu(title):
            item = work.add(self.db, title=title, assignee="a8", force=_FORCE)["id"]
            work.move(self.db, item, "build")
            work.note(self.db, item, "hold: attente owner", actor="a8")
            return item
        info = tenu("note d'information d'un tiers")
        work.note(self.db, info, "pour info : paquet composé", actor="agent:pilote8")
        reposé = tenu("hold d'un tiers après une levée")
        work.note(self.db, reposé, "reprise", actor="a8")
        work.note(self.db, reposé, "hold: attente bureau", actor="pilote8")
        work.note(self.db, reposé, "relevé", actor="pilote8")
        levé = tenu("levée explicite d'un tiers")
        work.note(self.db, levé, "pour info", actor="pilote8")
        with mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": "pilote8"}):
            work.note(self.db, levé, "hold lifted: go", actor="pilote8")
        aller_retour = tenu("aller-retour d'état")
        work.move(self.db, aller_retour, "qa")
        work.move(self.db, aller_retour, "build")
        repris = tenu("note de l'assigné après un tiers")
        work.note(self.db, repris, "pour info", actor="pilote8")
        work.note(self.db, repris, "je reprends", actor="agent:a8")
        ids = {r["id"] for r in self.st.actionable("a8")}
        self.assertEqual(ids, {levé, aller_retour, repris})

    def test_reveil_programme_tenu_est_du_et_consomme_une_fois(self):
        """#2466 : une note hold: portant une échéance réveille à l'échéance,
        une seule fois, et laisse un témoin `wake:` qui ne lève pas la retenue."""
        self.bail("aw")
        lot = work.add(self.db, title="séance owner", assignee="aw",
                       force=_FORCE)["id"]
        work.move(self.db, lot, "build")
        now = time.time()
        work.note(self.db, lot, "hold: séance owner 14:00", actor="aw",
                  wake_ts=now + 60)
        self.assertNotIn(lot, [r["id"] for r in self.st.actionable("aw")])
        self.assertEqual(self.st.due_wakes("aw", now), [], "pas avant l'échéance")
        rows = self.st.due_wakes("aw", now + 61)
        self.assertEqual([(r["id"], r["title"]) for r in rows],
                         [(lot, "séance owner")])
        taken = self.st.claim_wake("aw", lot, now + 61)
        self.assertEqual((taken["id"], taken["state"]), (lot, "build"))
        self.assertIsNone(self.st.claim_wake("aw", lot, now + 61),
                          "un claim en cours écarte une seconde délivrance")
        self.assertEqual(self.st.due_wakes("aw", now + 61), [])
        self.assertIsNotNone(work.get(self.db, lot)["wake_ts"],
                             "l'échéance n'est pas consommée par la délivrance")
        self.assertFalse(any(e["note"].startswith("wake:")
                             for e in work.events(self.db, lot)),
                         "#2614 : le claim seul n'écrit rien au journal")
        self.assertTrue(self.st.consume_wake("aw", lot, taken["wake_ts"]))
        self.assertIsNone(work.get(self.db, lot)["wake_ts"])
        event = work.events(self.db, lot)[0]
        self.assertEqual(event["actor"], "ameesh")
        self.assertTrue(event["note"].startswith("wake: scheduled wake delivered"))
        self.assertFalse(self.st.consume_wake("aw", lot, taken["wake_ts"]),
                         "une seule consommation")
        self.assertNotIn(lot, [r["id"] for r in self.st.actionable("aw")],
                         "le témoin wake: ne lève pas la retenue hold:")

    def test_wake_consumed_after_merge_keeps_event_phase(self):
        self.bail("aw-race")
        lot = work.add(self.db, title="fusion pendant réveil", assignee="aw-race",
                       force=_FORCE)["id"]
        now = time.time()
        work.schedule_wake(self.db, lot, now - 1, text="reprise", actor="aw-race")
        taken = self.st.claim_wake("aw-race", lot, now)
        self.assertIsNotNone(taken)
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (lot,))
        self.assertTrue(self.st.consume_wake("aw-race", lot, taken["wake_ts"]))
        event = work.events(self.db, lot)[0]
        self.assertTrue(event["note"].startswith("wake: scheduled wake delivered"))
        self.assertEqual(event["state"], "merged")

    def test_reveil_programme_waiting_human_est_du(self):
        """#640 : l'agent en hold sur un lot waiting_human est réveillé à
        l'échéance, sans dépendre de la règle 1 ni d'une décision lue."""
        self.bail("aw2")
        lot = work.add(self.db, title="séance owner", assignee="aw2",
                       force=_FORCE)["id"]
        work.move(self.db, lot, "build")
        hold_for_owner(self.db, lot)
        now = time.time()
        work.note(self.db, lot, "hold: reprise à la séance", actor="aw2",
                  wake_ts=now - 1)
        self.assertEqual([r["id"] for r in self.st.due_wakes("aw2", now)], [lot])
        taken = self.st.claim_wake("aw2", lot, now)
        self.assertEqual(taken["state"], "waiting_human")
        self.assertTrue(self.st.consume_wake("aw2", lot, taken["wake_ts"]))

    def test_claim_expire_redelivre_le_reveil(self):
        """#2466 B1 : un claim non consommé (drainage, arrêt, panne avant
        lancement) expire et le réveil est délivré de nouveau — jamais perdu."""
        self.bail("aw5")
        lot = work.add(self.db, title="séance owner", assignee="aw5",
                       force=_FORCE)["id"]
        now = time.time()
        work.schedule_wake(self.db, lot, now - 1, text="séance",
                           actor="orchestrateur")
        self.assertEqual([r["id"] for r in self.st.due_wakes("aw5", now)], [lot])
        self.assertIsNotNone(self.st.claim_wake("aw5", lot, now, claim_ttl_s=60))
        self.assertEqual(self.st.due_wakes("aw5", now, claim_ttl_s=60), [],
                         "claim en cours : pas de seconde délivrance")
        self.db.execute("UPDATE work_items SET wake_claim_at ="
                        " now() - interval '2 minutes' WHERE id = %s", (lot,))
        self.assertEqual([r["id"] for r in self.st.due_wakes("aw5", now,
                                                            claim_ttl_s=60)],
                         [lot], "claim expiré : délivré de nouveau")

    def test_claim_renouvele_par_le_detenteur_sans_temoin(self):
        """#2614 : le détenteur renouvelle son bail (`claim_ttl_s=0`) pendant
        un lancement plus long que le bail — pas de seconde délivrance — et
        ni le claim, ni son renouvellement, ni un claim expiré n'écrivent de
        ligne `wake:` ; seule la consommation en écrit une."""
        self.bail("aw7")
        lot = work.add(self.db, title="séance owner", assignee="aw7",
                       force=_FORCE)["id"]
        now = time.time()
        work.schedule_wake(self.db, lot, now - 1, text="", actor="pilot")
        taken = self.st.claim_wake("aw7", lot, now, claim_ttl_s=60)
        self.assertIsNotNone(taken)
        self.db.execute("UPDATE work_items SET wake_claim_at ="
                        " now() - interval '50 seconds' WHERE id = %s", (lot,))
        renewed = self.st.claim_wake("aw7", lot, now, claim_ttl_s=0)
        self.assertEqual(renewed["wake_ts"], taken["wake_ts"])
        self.db.execute("UPDATE work_items SET wake_claim_at = wake_claim_at"
                        " - interval '50 seconds' WHERE id = %s", (lot,))
        self.assertEqual(self.st.due_wakes("aw7", now, claim_ttl_s=60), [],
                         "100 s après le claim, le bail renouvelé tient encore")
        self.assertIsNone(self.st.claim_wake("aw7", lot, now, claim_ttl_s=60))
        self.db.execute("UPDATE work_items SET wake_claim_at ="
                        " now() - interval '2 minutes' WHERE id = %s", (lot,))
        self.assertIsNotNone(self.st.claim_wake("aw7", lot, now, claim_ttl_s=60),
                             "non renouvelé, le bail expire : redélivrance")
        def temoins():
            return [e for e in work.events(self.db, lot)
                    if e["note"].startswith("wake: scheduled wake delivered")]
        self.assertEqual(temoins(), [], "aucun témoin sans consommation")
        self.assertTrue(self.st.consume_wake("aw7", lot, taken["wake_ts"]))
        self.assertEqual(len(temoins()), 1)
        self.assertIsNone(self.st.claim_wake("aw7", lot, now, claim_ttl_s=0),
                          "consommé : plus rien à renouveler")

    def test_consume_ne_detruit_pas_une_echeance_reposee(self):
        """#2466 : une échéance reposée entre le claim et le lancement n'est
        pas effacée par la consommation du claim précédent."""
        self.bail("aw6")
        lot = work.add(self.db, title="séance owner", assignee="aw6",
                       force=_FORCE)["id"]
        now = time.time()
        work.schedule_wake(self.db, lot, now - 1, text="", actor="pilot")
        taken = self.st.claim_wake("aw6", lot, now)
        self.assertIsNotNone(taken)
        work.schedule_wake(self.db, lot, now + 3600, text="report", actor="pilot")
        self.assertFalse(self.st.consume_wake("aw6", lot, taken["wake_ts"]))
        self.assertAlmostEqual(work.get(self.db, lot)["wake_ts"], now + 3600,
                               delta=2)

    def test_reveil_programme_remplace_et_annule(self):
        self.bail("aw3")
        lot = work.add(self.db, title="séance owner", assignee="aw3",
                       force=_FORCE)["id"]
        now = time.time()
        work.schedule_wake(self.db, lot, now + 600, text="séance owner",
                           actor="orchestrateur")
        self.assertAlmostEqual(work.get(self.db, lot)["wake_ts"], now + 600, delta=2)
        self.assertIn("wake: séance owner", work.events(self.db, lot)[0]["note"])
        work.schedule_wake(self.db, lot, now + 120, text="", actor="orchestrateur")
        self.assertAlmostEqual(work.get(self.db, lot)["wake_ts"], now + 120, delta=2)
        work.schedule_wake(self.db, lot, None, text="", actor="orchestrateur")
        self.assertIsNone(work.get(self.db, lot)["wake_ts"])
        self.assertIn("cancelled", work.events(self.db, lot)[0]["note"])

    def test_note_porte_wake_at_dans_le_texte(self):
        """#2466 : `wake_at=<ISO>` dans la note est l'autre écriture de
        `--wake-at`, lue par le même chemin ; une valeur illisible est
        refusée au lieu d'être ignorée."""
        self.bail("aw4")
        lot = work.add(self.db, title="séance owner", assignee="aw4",
                       force=_FORCE)["id"]
        work.note(self.db, lot,
                  "hold: séance owner wake_at=2999-01-01T00:00:00+00:00",
                  actor="aw4")
        self.assertIsNotNone(work.get(self.db, lot)["wake_ts"])
        with self.assertRaises(work.WorkError):
            work.note(self.db, lot, "hold: séance wake_at=demain", actor="aw4")
        with self.assertRaises(work.WorkError):
            work.note(self.db, lot,
                      "hold: séance wake_at=2999-01-01T00:00:00+00:00",
                      actor="aw4", wake_ts=time.time() + 60)


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
            # #540 : le second lot non démarré irait à la file d'équipe sans
            # forçage motivé — le témoin « deux lots actifs » ne serait plus bâti.
            a = work.add(self.db, title="A", assignee="w7", force=_FORCE)["id"]
            work.add(self.db, title="B", assignee="w7", force=_FORCE)
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

    def test_l_oubli_d_une_session_sans_lot_retire_le_resume_en_attente(self):
        """N2 (#1898) : oublier une session sans lot retire aussi le
        `RESUME_PENDING` du disque ; sinon un worker recréé le relit (aucune
        session courante) et ressuscite un résumé périmé."""
        worker = self._worker("w13")
        try:
            registry.set_session(self.db, "w13", "sess-veille", None,
                                 harness="claude", cwd=self._cwd("w13"))
            worker.write_session_file("sess-veille")
            worker.agent = registry.get(self.db, "w13")
            self.assertTrue(worker._resume_keep("RESUME-PERIME", "agent"))
            self.assertTrue(os.path.exists(worker._path(worker.RESUME_PENDING)))
            self.assertTrue(worker._forget_lotless_session())
            self.assertFalse(os.path.exists(worker._path(worker.RESUME_PENDING)),
                             "le résumé périmé ne reste pas sur disque")
            # worker recréé : rien ne ressuscite le résumé périmé.
            recree = AgentWorker(Runner(self.cfg, self.db, once=True),
                                 registry.get(self.db, "w13"),
                                 {"lease_epoch": worker.epoch})
            recree._resume_load()
            self.assertEqual(recree.resume_summary, "")
        finally:
            worker.watchdog_stop.set()

    def test_le_lot_fini_ferme_la_session_au_tour_suivant(self):
        """B1 (revue Claude du 14/10) + N1 (#1898) : la session active d'un lot
        FINI est fermée par son agent, sous son bail, avant le tour suivant sur
        ce même lot ; ce tour et les suivants ne rouvrent PAS de session
        dédiée au lot fini — fini le cycle ferme/rouvre à chaque tour — ils
        partagent une session sans lot, que le prochain lot ouvert oubliera."""
        worker = self._worker("w8")
        try:
            a = work.add(self.db, title="A", assignee="w8")["id"]
            work.move(self.db, a, "build")
            self.tour(worker, a, "commence A", session="sess-A")
            self.assertEqual(registry.get(self.db, "w8")["session_id"], "sess-A")
            work.close(self.db, a, abandoned=True, actor="test")
            spec = worker.rotate_for_lot({"kind": "prompt", "prompt": "suite de A",
                                          "ids": [], "lot": str(a)})
            self.assertEqual(spec["lot"], str(a))
            self.assertTrue(spec.get("lot_finished"),
                            "le tour d'un lot fini est marqué : pas de session dédiée")
            agent = registry.get(self.db, "w8")
            self.assertEqual((agent["session_id"], agent["session_work_item"]),
                             (None, None), "la session du lot fini est fermée")
            self.assertIsNone(worker.read_session_file(),
                              "le fichier local ne ressuscite pas la session")
            self.assertIsNone(worker.current_session())
            self.assertNotIn("dedicated to lot", worker.resume_summary or "",
                             "aucune session dédiée au lot fini n'est rouverte")
            # le tour part sur le brief déterministe ; sa session n'est pas
            # rattachée au lot fini.
            with self._env("sess-A2"):
                self.assertTrue(worker.run_turn(spec))
            agent = registry.get(self.db, "w8")
            self.assertEqual(agent["session_id"], "sess-A2")
            self.assertIsNone(agent["session_work_item"],
                              "le lot fini n'est pas noté sur la session du tour")
            self.assertNotIn("--resume", self.turns()[-1]["argv"])
            # tour suivant sur le même lot fini : ni fermeture ni session neuve.
            spec2 = worker.rotate_for_lot({"kind": "prompt", "prompt": "encore A",
                                           "ids": [], "lot": str(a)})
            self.assertTrue(spec2.get("lot_finished"))
            self.assertEqual(registry.get(self.db, "w8")["session_id"], "sess-A2",
                             "la session n'est ni fermée ni rouverte")
            with self._env("sess-jamais"):
                self.assertTrue(worker.run_turn(spec2))
            self.assertEqual(registry.get(self.db, "w8")["session_id"], "sess-A2",
                             "la session du lot fini n'est pas rouverte")
            self.assertIn("--resume", self.turns()[-1]["argv"])
            self.assertIsNone(registry.get(self.db, "w8")["session_work_item"])
            # lot ouvert suivant : la session sans lot est oubliée, la sienne
            # s'ouvre normalement.
            b = work.add(self.db, title="B", assignee="w8")["id"]
            self.tour(worker, b, "prends B", session="sess-B")
            agent = registry.get(self.db, "w8")
            self.assertEqual((agent["session_id"], agent["session_work_item"]),
                             ("sess-B", str(b)))
        finally:
            worker.watchdog_stop.set()

    def test_la_mise_de_cote_refusee_differe_le_tour(self):
        worker = self._worker("w15")
        self.addCleanup(worker.watchdog_stop.set)
        b = work.add(self.db, title="B", assignee="w15", force=_FORCE)["id"]
        self.tour(worker, b, "commence B", session="sess-B")
        a = work.add(self.db, title="A", assignee="w15", force=_FORCE)["id"]
        work.close(self.db, a, abandoned=True, actor="test")
        for failure in (None, db_mod.DbError("base indisponible")):
            with self.subTest(failure=failure), mock.patch.object(
                    type(self.st), "switch", return_value=None, side_effect=failure):
                spec = worker.rotate_for_lot({"kind": "prompt", "prompt": "suite de A",
                                              "ids": [], "lot": str(a)})
                self.assertTrue(spec.get("lot_switch_failed"))
                self.assertFalse(worker._run_turn(spec))
                self.assertEqual(worker.current_session(), "sess-B")

    def test_le_tour_d_un_lot_fini_ne_partage_pas_la_session_d_un_autre_lot(self):
        """#2055 (P3 de la relecture de #1898) : quand le tour porte sur un lot
        FINI A alors que la session active est celle d'un AUTRE lot B encore
        ouvert, la session de B est mise de côté pour lui (reprise au prochain
        tour sur B) et le tour de A part dans une session sans lot : une session
        n'est jamais partagée entre lots (#794), même fini."""
        worker = self._worker("w14")
        try:
            b = work.add(self.db, title="B", assignee="w14", force=_FORCE)["id"]
            work.move(self.db, b, "build")
            self.tour(worker, b, "commence B", session="sess-B")
            a = work.add(self.db, title="A", assignee="w14", force=_FORCE)["id"]
            work.move(self.db, a, "build")
            self.tour(worker, a, "commence A", session="sess-A")
            argv = self.tour(worker, b, "retour sur B", session="sess-B2")
            self.assertEqual(argv[argv.index("--resume") + 1], "sess-B")
            self.assertEqual(registry.get(self.db, "w14")["session_id"], "sess-B")
            work.close(self.db, a, abandoned=True, actor="test")
            with mock.patch("ameesh.runner.os.unlink", side_effect=PermissionError("read-only")):
                spec = worker.rotate_for_lot({"kind": "prompt", "prompt": "suite de A",
                                              "ids": [], "lot": str(a)})
                self.assertIsNone(worker.current_session(), "le fichier resté est périmé")
                recree = AgentWorker(Runner(self.cfg, self.db, once=True),
                                     registry.get(self.db, "w14"),
                                     {"lease_epoch": worker.epoch})
                self.assertIsNone(recree.current_session(), "oubli durable après redémarrage")
            self.assertEqual(spec["lot"], str(a))
            self.assertTrue(spec.get("lot_finished"))
            agent = registry.get(self.db, "w14")
            self.assertEqual((agent["session_id"], agent["session_work_item"]), (None, None),
                             "la session de B est mise de côté, aucune n'est active")
            self.assertEqual(worker.read_session_file(), "sess-B",
                             "le refus de suppression a laissé le fichier de B")
            self.assertIsNone(worker.current_session())
            lignes = {r["work_item_id"]: r["session_id"] for r in self.st.listing("w14")}
            self.assertEqual(lignes.get(b), "sess-B", "la session de B est gardée pour B")
            self.assertNotIn("dedicated to lot", worker.resume_summary or "",
                             "aucune session dédiée au lot fini n'est rouverte")
            # le tour du lot fini part sans lot, jamais dans la session de B.
            with self._env("sess-A2"):
                self.assertTrue(worker.run_turn(spec))
            argv = self.turns()[-1]["argv"]
            self.assertNotIn("--resume", argv, "la session de B n'est pas partagée")
            agent = registry.get(self.db, "w14")
            self.assertEqual((agent["session_id"], agent["session_work_item"]),
                             ("sess-A2", None))
            with mock.patch.object(worker, "_switch_lot", return_value=False):
                spec = worker.rotate_for_lot({"kind": "prompt", "prompt": "suite de B",
                                              "ids": [], "lot": str(b)})
                self.assertTrue(spec.get("lot_switch_failed"))
            with mock.patch.object(type(self.st), "listing", side_effect=db_mod.DbError("base")):
                spec = worker.rotate_for_lot({"kind": "prompt", "prompt": "suite de B",
                                              "ids": [], "lot": str(b)})
                self.assertTrue(spec.get("lot_switch_failed"))
            # le tour suivant sur B reprend SA session, mise de côté.
            argv = self.tour(worker, b, "suite de B")
            self.assertEqual(argv[argv.index("--resume") + 1], "sess-B")
            agent = registry.get(self.db, "w14")
            self.assertEqual((agent["session_id"], agent["session_work_item"]),
                             ("sess-B", str(b)))
        finally:
            worker.watchdog_stop.set()

    def test_un_fichier_local_non_retire_ne_ressuscite_pas_la_session(self):
        """P2 bloquant (contrôle bureau + R1 Codex du 14/10) : si le fichier
        local ne peut pas être retiré (`unlink` refuse, PermissionError),
        l'oubli reste durable. `clear_session` pose `session_reset_at` dans la
        même écriture fencée par le bail ; le fichier, antérieur au reset, est
        périmé — sur ce worker comme sur un worker recréé qui relit le
        registre. Avant le correctif, la méthode rendait True et
        `current_session` relisait « sess-A »."""
        worker = self._worker("w12")
        try:
            a = work.add(self.db, title="A", assignee="w12")["id"]
            work.move(self.db, a, "build")
            self.tour(worker, a, "commence A", session="sess-A")
            chemin = worker._path("session")
            self.assertTrue(os.path.exists(chemin))
            work.close(self.db, a, abandoned=True, actor="test")
            with mock.patch("ameesh.runner.os.unlink",
                            side_effect=PermissionError("read-only")):
                self.assertTrue(worker._close_finished_lot_session(str(a)),
                                "l'oubli est durable, il ne dépend pas de l'unlink")
                self.assertTrue(os.path.exists(chemin),
                                "le fichier est resté : c'est le cas PermissionError")
                self.assertIsNone(worker.current_session(),
                                  "le fichier périmé ne ressuscite pas la session")
                agent = registry.get(self.db, "w12")
                self.assertIsNotNone(agent["session_reset_ts"],
                                     "l'oubli a posé un reset fencé par le bail")
                # worker recréé (redémarrage du service) : le registre suffit.
                recree = AgentWorker(Runner(self.cfg, self.db, once=True),
                                     registry.get(self.db, "w12"),
                                     {"lease_epoch": worker.epoch})
                self.assertIsNone(recree.current_session(),
                                  "même sur un worker neuf, le fichier reste périmé")
                self.assertTrue(os.path.exists(chemin),
                                "aucune reprise ne dépend de la disparition du fichier")
        finally:
            worker.watchdog_stop.set()

    def test_les_politiques_jamais_et_taille_gardent_la_session(self):
        """B1b : hors politique `par-lot`, un lot fini ne ferme pas la session
        longue (`jamais`, `taille`)."""
        for i, politique in enumerate(("jamais", "taille")):
            name = "w9%d" % i
            with self.subTest(politique=politique):
                worker = self._worker(name)
                try:
                    storage.of(self.db).operations.set_settings(
                        name, {"session_policy": politique})
                    worker.agent = registry.get(self.db, name)
                    a = work.add(self.db, title="A", assignee=name)["id"]
                    work.move(self.db, a, "build")
                    self.tour(worker, a, "commence A", session="sess-A")
                    work.close(self.db, a, abandoned=True, actor="test")
                    self.assertFalse(worker._close_finished_lot_session(str(a)))
                    worker.rotate_for_lot({"kind": "prompt", "prompt": "veille",
                                           "ids": []})
                    agent = registry.get(self.db, name)
                    self.assertEqual(agent["session_id"], "sess-A")
                    self.assertEqual(agent["session_work_item"], str(a))
                finally:
                    worker.watchdog_stop.set()

    def test_pas_de_fermeture_pendant_un_tour(self):
        """B1c : jamais pendant un tour — la garde `proc` laisse la session et
        son fichier local intacts, même pour un lot fini."""
        worker = self._worker("w10")
        proc = subprocess.Popen(["sleep", "60"])
        try:
            a = work.add(self.db, title="A", assignee="w10")["id"]
            work.move(self.db, a, "build")
            storage.of(self.db).operations.set_session_work_item(
                "w10", worker.runner.runner_id, worker.epoch, str(a))
            registry.set_session(self.db, "w10", "sess-A", None, harness="claude",
                                 cwd=self._cwd("w10"))
            worker.agent = registry.get(self.db, "w10")
            worker.proc = proc
            work.close(self.db, a, abandoned=True, actor="test")
            self.assertFalse(worker._close_finished_lot_session(str(a)))
            spec = worker.rotate_for_lot({"kind": "prompt", "prompt": "suite",
                                          "ids": [], "lot": str(a)})
            self.assertEqual(spec["lot"], str(a))
            agent = registry.get(self.db, "w10")
            self.assertEqual(agent["session_id"], "sess-A")
        finally:
            worker.proc = None
            proc.kill()
            proc.wait()
            worker.watchdog_stop.set()

    def test_rotation_de_taille_sur_le_meme_lot_garde_le_resume(self):
        """N1 (revue Claude du 14/10) : après une rotation de taille, le tour
        suivant sur le MÊME lot ne jette pas le résumé payé : il ouvre la
        session neuve (complété par la fiche du lot), en mémoire et sur disque
        (`RESUME_PENDING`), au lieu du brief de lot seul."""
        worker = self._worker("w11")
        try:
            a = work.add(self.db, title="A", assignee="w11")["id"]
            work.move(self.db, a, "build")
            mail.send(self.db, "orch", "w11", "commence A", work_item_id=str(a))
            with mock.patch.dict(os.environ, {
                    "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
                    "AMEESH_SESSION_STORE_CHECK": "0",
                    "AMEESH_TEST_NEW_SESSION": "sess-A",
                    "AMEESH_TEST_RESULT": "RESUME-PAYE"}):
                spec = worker.rotate_for_lot(worker.pick())
                self.assertTrue(worker.run_turn(spec))
                worker.rotation_forcee = "banc"        # rotation de taille due
                self.assertTrue(worker.maybe_rotate())
            self.assertIsNone(registry.get(self.db, "w11")["session_id"])
            self.assertEqual(worker.resume_origin, "agent")
            self.assertIn("RESUME-PAYE", worker.resume_summary)
            # le tour suivant porte le MÊME lot : le résumé ne doit pas être écrasé
            mail.send(self.db, "orch", "w11", "suite de A", work_item_id=str(a))
            spec = worker.rotate_for_lot(worker.pick())
            self.assertEqual(spec["lot"], str(a))
            self.assertEqual(worker.resume_origin, "agent")
            self.assertIn("RESUME-PAYE", worker.resume_summary)
            self.assertIn("dedicated to lot #%d" % a, worker.resume_summary,
                          "la fiche du lot complète le résumé")
            chemin = os.path.join(worker.state_dir, "resume-pending.json")
            with open(chemin, encoding="utf-8") as fh:
                self.assertIn("RESUME-PAYE", fh.read(),
                              "le disque garde le même résumé que la mémoire")
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
        self.assertIn("## Lot thread — decisions, unread and delivered messages", text)
        self.assertIn("un message non lu sur le lot", text)
        # jamais le contenu d'un autre lot, ni l'ancienne session, ni la liste
        # des lots assignés du brief de reprise
        self.assertNotIn("Un autre lot", text)
        self.assertNotIn("un message pour un autre lot", text)
        self.assertNotIn("Previous session", text)
        self.assertNotIn("Work items assigned to you", text)

    def test_bascule_reprend_un_verdict_livre_du_lot_suivant(self):
        """#2539 : un message livré dans la session d'un autre lot ne disparaît
        pas quand la session dédiée à son lot s'ouvre.

        Vraie bascule : la session du lot A est active, le verdict du lot B a
        été livré pendant celle-ci (et date de plus de 24 h), puis la session
        de B s'ouvre. Le verdict doit figurer dans son brief, sinon le nouveau
        tour ne sait pas qu'un blocked l'attend."""
        from ameesh import reprise
        runner, lease = self.bail("b4")
        worker = AgentWorker(runner, registry.get(self.db, "b4"), lease)
        try:
            a = work.add(self.db, title="Le lot de la session neuve", assignee="b4",
                         force=_FORCE)["id"]
            b = work.add(self.db, title="Le lot du verdict", assignee="b4",
                         force=_FORCE)["id"]
            cwd = self._cwd("b4")
            storage.of(self.db).operations.set_session_work_item(
                "b4", runner.runner_id, worker.epoch, str(a))
            registry.set_session(self.db, "b4", "sess-a", None, harness="claude", cwd=cwd)
            worker.agent = registry.get(self.db, "b4")
            mid = mail.send(self.db, "orch", "b4", "verdict B : blocked",
                            work_item_id=str(b))
            self.assertEqual(mail.mark_delivered(self.db, [mid]), 1)
            self.db.execute(
                "UPDATE agent_mailbox SET delivered_at = now() - interval '25 hours'"
                " WHERE id = %s", (mid,))

            self.assertTrue(worker._switch_lot(str(a), str(b)))
            text = worker.resume_summary or ""
            self.assertIn("verdict B : blocked", text)
            self.assertIn("- delivered #%d" % mid, text)
            self.assertNotIn("- unread #%d" % mid, text)
        finally:
            worker.watchdog_stop.set()

    def test_brief_ne_prend_que_les_courriers_du_lot_sous_le_bruit(self):
        """#2539 B2 : le filtre par lot est dans la requête, pas après une
        fenêtre de 200 messages : 201 courriers d'un autre lot ne masquent pas
        le verdict de CE lot."""
        from ameesh import reprise
        self.bail("b5")
        a = work.add(self.db, title="Le lot du verdict", assignee="b5")["id"]
        autre = work.add(self.db, title="Le lot bruyant", assignee="b5")["id"]
        mid = mail.send(self.db, "orch", "b5", "verdict utile", work_item_id=str(a))
        self.assertEqual(mail.mark_delivered(self.db, [mid]), 1)
        # 201 courriers plus récents d'un autre lot : un history(limit=200)
        # les lirait tous avant de filtrer et perdrait le verdict ci-dessus.
        self.db.execute(
            "INSERT INTO agent_mailbox"
            " (sender, recipient, body, work_item_id, status, delivered_at)"
            " SELECT 'orch', %s, 'bruit ' || g, %s, 'delivered', now()"
            "   FROM generate_series(1, 201) AS g",
            ("b5", str(autre)))

        text = reprise.lot_brief(self.cfg, self.db, registry.get(self.db, "b5"), a,
                                 consigne="go")
        self.assertIn("verdict utile", text)
        self.assertNotIn("bruit 200", text)

    def test_courriers_livres_du_lot_lus_sur_la_colonne_indexee(self):
        """#2752 : le lot est filtré sur `work_item_id`, sans fonction, pour
        que l'index partiel 0413 serve (comme #2563 pour les requests)."""
        self.bail("b7")
        a = work.add(self.db, title="Le lot indexé", assignee="b7")["id"]
        mid = mail.send(self.db, "orch", "b7", "livré", work_item_id=str(a))
        self.assertEqual(mail.mark_delivered(self.db, [mid]), 1)
        seen = []

        class Trace:
            """La connexion réelle, avec le SQL de la lecture du lot noté."""
            def __init__(self, db):
                self.db = db

            def query(self, sql, params=()):
                seen.append(" ".join(sql.split()))
                return self.db.query(sql, params)

            def __getattr__(self, name):
                return getattr(self.db, name)

        found = mail.delivered_for_lot(Trace(self.db), "b7", a, 10)
        self.assertEqual([r["id"] for r in found], [mid])
        queries = [sql for sql in seen if "FROM agent_mailbox" in sql]
        self.assertEqual(len(queries), 1)
        predicate = queries[0].split("WHERE", 1)[1]
        self.assertIn("work_item_id = %s", predicate)
        self.assertNotIn("btrim", predicate)

    def test_brief_borne_les_courriers_livres(self):
        """#2539 : la lecture par lot est bornée ; le brief annonce le reste."""
        from ameesh import reprise
        self.bail("b6")
        a = work.add(self.db, title="Le lot borné", assignee="b6")["id"]
        ids = []
        for i in range(reprise.LOT_MAIL_MAX + 2):
            ids.append(mail.send(self.db, "orch", "b6", "livré %d" % i,
                                 work_item_id=str(a)))
        self.assertEqual(mail.mark_delivered(self.db, ids), reprise.LOT_MAIL_MAX + 2)

        text = reprise.lot_brief(self.cfg, self.db, registry.get(self.db, "b6"), a,
                                 consigne="go")
        self.assertEqual(text.count("- delivered #"), reprise.LOT_MAIL_MAX)
        self.assertIn("more delivered messages for this lot", text)

    def test_brief_reste_lisible_si_la_lecture_mediate_refuse(self):
        """#2539 : en mode médié, `mailbox.delivered_for_lot` est refusé au
        contrat ; le brief garde les non-lus et n'échoue pas."""
        from ameesh import reprise
        from ameesh.mediated_executor.contract import NotSupportedRemotely
        self.bail("b7")
        a = work.add(self.db, title="Le lot médié", assignee="b7")["id"]
        mail.send(self.db, "orch", "b7", "un message non lu", work_item_id=str(a))
        with mock.patch.object(mail, "delivered_for_lot",
                               side_effect=NotSupportedRemotely("refusé")):
            text = reprise.lot_brief(self.cfg, self.db, registry.get(self.db, "b7"), a,
                                     consigne="go")
        self.assertIn("un message non lu", text)
        self.assertNotIn("- delivered #", text)

    def test_brief_ne_dit_pas_put_aside_quand_rien_n_est_garde(self):
        """#2242 : `_switch_lot` peut ne rien garder (lot courant fini ou
        réassigné) ; le brief d'une session neuve ne doit alors pas annoncer
        que sa session est de côté."""
        from ameesh import reprise
        runner, lease = self.bail("b2")
        worker = AgentWorker(runner, registry.get(self.db, "b2"), lease)
        a = work.add(self.db, title="Le lot du brief", assignee="b2")["id"]
        try:
            with mock.patch.object(reprise, "lot_brief", return_value="brief") as brief:
                worker._lot_brief(str(a), "999", {"parked": None})
            consigne = brief.call_args.kwargs["consigne"]
            self.assertNotIn("is put aside and will be resumed", consigne)
            self.assertIn("no session of lot #999 was put aside", consigne)
            self.assertNotIn("it is finished or no longer assigned", consigne)
            with mock.patch.object(reprise, "lot_brief", return_value="brief") as brief:
                worker._lot_brief(str(a), "999", {"parked": "sess-999"})
            consigne = brief.call_args.kwargs["consigne"]
            self.assertIn("the one of lot #999 is put aside", consigne)
            self.assertNotIn("no session of lot #999 was put aside", consigne)
            with mock.patch.object(reprise, "lot_brief", return_value="brief") as brief:
                worker._lot_brief(str(a), "", {})
            consigne = brief.call_args.kwargs["consigne"]
            self.assertIn("no session was put aside", consigne)
            self.assertNotIn("lot # was put aside", consigne)
        finally:
            worker.watchdog_stop.set()

    def test_branche_qui_commence_par_un_tiret_n_est_pas_une_option_git(self):
        """N2 (revue Claude du 14/10) : `branch`/`branch_target` viennent de la
        base (`work.set_branch` les valide, mais la colonne peut être écrite
        autrement) : jamais passés à git comme des options."""
        from ameesh import reprise
        lignes = reprise._lot_diff(
            {"branch": "--output=/tmp/prise", "branch_target": "--output=/tmp/autre"},
            self.tmp)
        self.assertIn("refused", "\n".join(lignes))


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
        self.addCleanup(worker._forget_wake_claim)
        return worker

    def pick(self, worker):
        worker._lot_wake_checked = float("-inf")
        worker._scheduled_wake_checked = float("-inf")
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
        # #3986 : la relecture attend seulement après déclaration du gel.
        work.milestone(self.db, qa, "frozen", sha="a" * 40, actor="test")
        self.assertIsNone(self.pick(worker), "un lot en qa gelé ne réveille pas")
        b = work.add(self.db, title="à faire", assignee="r1", force=_FORCE)["id"]
        spec = self.pick(worker)
        self.assertEqual((spec["kind"], spec["lot"]), ("idle", str(b)))
        self.assertIn("Resuming lot #%d" % b, spec["prompt"])
        self.assertIn('hold: <reason>', spec["prompt"])
        self.assertIsNone(self.pick(worker), "pas deux réveils de suite")

    def test_future_wake_without_hold_waits_in_intake_and_build(self):
        for state in ("intake", "build"):
            with self.subTest(state=state):
                name = "wake5028" + state
                worker = self._worker(name)
                lot = work.add(self.db, title="scheduled without hold", assignee=name)["id"]
                if state == "build":
                    work.move(self.db, lot, state)
                self.db.execute("UPDATE work_items SET wake_at = clock_timestamp()"
                                " + interval '1 hour' WHERE id = %s", (lot,))
                self.assertEqual(self.st.actionable(name), [])
                self.assertIsNone(self.pick(worker))
                self.assertEqual(self.st.listing(name), [])
                row = next(r for r in storage.of(self.db).operations.listing()
                           if r["name"] == name)
                self.assertEqual(row["actionable_lots"], 0)
                self.assertEqual(row["active_lots"], 0)
                from ameesh import planned_waits
                rows = [r for r in storage.of(self.db).operations.open_lots_activity(None)
                        if r["id"] == lot]
                self.assertGreater(rows[0]["wake_ts"], time.time())
                waits = planned_waits.read(self.db, rows, packets=False)
                self.assertEqual(waits[lot]["what"], "scheduled")
                self.db.execute("UPDATE work_items SET wake_at = clock_timestamp()"
                                " - interval '1 second' WHERE id = %s", (lot,))
                self.assertEqual([r["id"] for r in self.st.actionable(name)], [lot])
                row = next(r for r in storage.of(self.db).operations.listing()
                           if r["name"] == name)
                self.assertEqual(row["actionable_lots"], 1)
                self.assertEqual(self.pick(worker)["lot"], str(lot))

    def _qa_lot(self, name, *, freeze, verdict=None):
        lot = work.add(self.db, title="qa lot", assignee=name, force=_FORCE)["id"]
        work.move(self.db, lot, "build")
        work.move(self.db, lot, "qa")
        if freeze:
            head = "b" * 40
            work.milestone(self.db, lot, "frozen", sha=head, actor="test")
            if verdict:
                work.milestone(self.db, lot, "verdict", verdict=verdict, sha=head,
                               actor="reviewer")
        return lot

    def _future_wake(self, lot):
        self.db.execute("UPDATE work_items SET wake_at = clock_timestamp()"
                        " + interval '1 hour' WHERE id = %s", (lot,))

    def test_blocking_verdict_primes_over_a_future_wake(self):
        # #5037 (N2 de la revue de #5028) : la correction due n'attend pas
        # l'échéance du réveil, ni pour le réveil ni pour le décompte.
        name = "wake5037blocked"
        worker = self._worker(name)
        lot = self._qa_lot(name, freeze=True, verdict="blocked")
        self._future_wake(lot)
        self.assertEqual([r["id"] for r in self.st.actionable(name)], [lot])
        row = next(r for r in storage.of(self.db).operations.listing()
                   if r["name"] == name)
        self.assertEqual(row["actionable_lots"], 1)
        spec = self.pick(worker)
        self.assertEqual((spec["kind"], spec["lot"]), ("idle", str(lot)))
        self.assertIn("Resuming lot #%d" % lot, spec["prompt"])

    def test_missing_freeze_primes_over_a_future_wake(self):
        # #5037 (N2) : le gel dû est un geste de l'auteur, pas une attente.
        name = "wake5037freeze"
        worker = self._worker(name)
        lot = self._qa_lot(name, freeze=False)
        self._future_wake(lot)
        self.assertEqual([r["id"] for r in self.st.actionable(name)], [lot])
        row = next(r for r in storage.of(self.db).operations.listing()
                   if r["name"] == name)
        self.assertEqual(row["actionable_lots"], 1)
        self.assertEqual(self.pick(worker)["lot"], str(lot))

    def test_qa_without_due_work_still_waits_for_its_future_wake(self):
        # contrôle : tout lot en qa à réveil futur n'est pas rendu actionnable.
        name = "wake5037ok"
        worker = self._worker(name)
        lot = self._qa_lot(name, freeze=True, verdict="ok")
        self._future_wake(lot)
        self.assertEqual(self.st.actionable(name), [])
        row = next(r for r in storage.of(self.db).operations.listing()
                   if r["name"] == name)
        self.assertEqual(row["actionable_lots"], 0)
        self.assertIsNone(self.pick(worker))

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

    def test_reveil_programme_delivre_puis_consomme(self):
        """#2466 : un réveil programmé échu réveille l'agent même sous hold ;
        la délivrance (claim + témoin `wake:`) ne consomme l'échéance qu'au
        lancement du harnais."""
        worker = self._worker("r7")
        lot = work.add(self.db, title="séance owner", assignee="r7",
                       force=_FORCE)["id"]
        work.move(self.db, lot, "build")
        work.note(self.db, lot, "hold: séance owner 14:00", actor="r7",
                  wake_ts=time.time() - 1)
        spec = self.pick(worker)
        self.assertEqual((spec["kind"], spec["lot"]), ("idle", str(lot)))
        self.assertIn("Scheduled wake", spec["prompt"])
        self.assertIsNotNone(work.get(self.db, lot)["wake_ts"],
                             "la délivrance ne consomme pas l'échéance")
        worker._turn_launched = True
        worker._consume_scheduled_wake(spec)
        self.assertIsNone(work.get(self.db, lot)["wake_ts"])
        worker._scheduled_wake_checked = float("-inf")
        self.assertIsNone(worker._scheduled_wake_spec(), "l'échéance ne se rejoue pas")
        consumed = [e for e in work.events(self.db, lot)
                    if e["note"].startswith("wake: scheduled wake delivered")]
        self.assertEqual(len(consumed), 1)

    def test_reveil_programme_sur_executeur_medie(self):
        """#2613 : le runner médié ne refuse plus le réveil programmé ; les
        trois opérations de wake passent par le contrat (due_wakes,
        claim_wake, consume_wake), pas seulement par un exécuteur local."""
        worker = self._worker("r7m")
        lot = work.add(self.db, title="séance owner", assignee="r7m",
                       force=_FORCE)["id"]
        work.note(self.db, lot, "hold: séance owner", actor="r7m",
                  wake_ts=time.time() - 1)
        with mock.patch.object(storage, "is_remote", return_value=True):
            spec = self.pick(worker)
        self.assertIsNotNone(spec, "un exécuteur médié délivre le réveil (#2613)")
        self.assertEqual((spec["kind"], spec["lot"]), ("idle", str(lot)))
        self.assertIn("Scheduled wake", spec["prompt"])
        self.assertIsNotNone(work.get(self.db, lot)["wake_ts"],
                             "la délivrance médiée ne consomme pas l'échéance")

    def test_reveil_programme_perdu_au_drainage_est_redelivre(self):
        """#2466 B1 (cx2) : si le drainage refuse le tour entre le choix et le
        lancement, le claim expire et le réveil est délivré de nouveau."""
        worker = self._worker("r7b")
        lot = work.add(self.db, title="séance owner", assignee="r7b",
                       force=_FORCE)["id"]
        work.note(self.db, lot, "hold: séance owner", actor="r7b",
                  wake_ts=time.time() - 1)
        spec = self.pick(worker)
        self.assertEqual(spec["lot"], str(lot))
        worker.runner.draining.set()
        self.assertFalse(worker.run_turn(spec), "le drainage refuse le tour")
        self.assertIsNotNone(work.get(self.db, lot)["wake_ts"],
                             "l'échéance survit au tour refusé")
        self.db.execute("UPDATE work_items SET wake_claim_at ="
                        " now() - interval '10 minutes' WHERE id = %s", (lot,))
        worker._scheduled_wake_checked = float("-inf")
        again = worker._scheduled_wake_spec()
        self.assertIsNotNone(again, "claim expiré : le réveil revient")
        self.assertEqual(again["lot"], str(lot))
        self.assertFalse(any(e["note"].startswith("wake:")
                             for e in work.events(self.db, lot)),
                         "#2614 : pas de témoin pour un claim non consommé")

    def _reveil_reclame(self, name):
        """Un réveil programmé échu, réclamé par `pick` : (worker, lot, spec)."""
        worker = self._worker(name)
        lot = work.add(self.db, title="séance owner", assignee=name,
                       force=_FORCE)["id"]
        work.note(self.db, lot, "hold: séance owner", actor=name,
                  wake_ts=time.time() - 1)
        spec = self.pick(worker)
        self.assertEqual(spec["lot"], str(lot))
        self.assertEqual(worker._wake_claim["lot"], lot)
        return worker, lot, spec

    def vieillit_claim(self, lot):
        """Le claim a plus que le bail (120 s) : sans renouvellement, il a expiré."""
        self.db.execute("UPDATE work_items SET wake_claim_at = now() - interval"
                        " '150 seconds' WHERE id = %s", (lot,))

    def claim_age(self, lot):
        return self.db.query("SELECT extract(epoch from now() - wake_claim_at)::float8"
                             " AS age FROM work_items WHERE id = %s", (lot,))[0]["age"]

    def redelivre(self, worker):
        worker._scheduled_wake_checked = float("-inf")
        return worker._scheduled_wake_spec()

    def test_reveil_programme_bail_renouvele_pendant_une_bascule_longue(self):
        """#2614 : une bascule de session de lot plus longue que le bail (120 s)
        ne laisse plus le claim expirer — il est renouvelé tant qu'il est tenu,
        avant même le battement du tour ; aucun second choix ne délivre le
        réveil. Consommé, le claim n'est plus tenu ; un seul témoin `wake:`."""
        worker = self._worker("r7d")
        worker.WAKE_CLAIM_RENEW_S = 0.05
        lot = work.add(self.db, title="séance owner", assignee="r7d",
                       force=_FORCE)["id"]
        work.note(self.db, lot, "hold: séance owner", actor="r7d",
                  wake_ts=time.time() - 1)
        worker._scheduled_wake_checked = float("-inf")
        worker._lot_wake_checked = float("-inf")

        def bascule_longue(spec):
            self.vieillit_claim(lot)
            echeance = time.monotonic() + 10
            while self.claim_age(lot) > 60 and time.monotonic() < echeance:
                time.sleep(0.02)
            return spec

        with mock.patch.object(worker, "rotate_for_lot", side_effect=bascule_longue):
            spec = worker._pick_for_turn()
        self.assertEqual(spec["lot"], str(lot))
        self.assertLess(self.claim_age(lot), 60, "bail renouvelé pendant la bascule")
        self.assertIsNone(self.redelivre(worker), "bail renouvelé : pas de second réveil")
        claim = worker._wake_claim
        worker._turn_launched = True
        worker._consume_scheduled_wake(spec)
        self.assertIsNone(worker._wake_claim)
        self.assertTrue(claim["done"].is_set(), "plus de renouvellement")
        delivered = [e for e in work.events(self.db, lot)
                     if e["note"].startswith("wake: scheduled wake delivered")]
        self.assertEqual(len(delivered), 1, "un témoin, celui de la consommation")

    def test_reveil_programme_renouvellement_cesse_si_l_echeance_change(self):
        """#2614 : échéance annulée ou reposée pendant le claim — plus rien à
        tenir, le renouvellement le dit."""
        worker, lot, _spec = self._reveil_reclame("r7f")
        self.assertTrue(worker._renew_wake_claim(worker._wake_claim))
        work.note(self.db, lot, "hold: déplacé", actor="r7f",
                  wake_ts=time.time() - 30)
        self.assertFalse(worker._renew_wake_claim(worker._wake_claim))

    def assert_claim_oublie(self, worker, lot, claim):
        """Le claim oublié n'est plus renouvelé : il expire et le réveil revient
        (#2466 B1) — jamais tenu sans fin par un tour ultérieur."""
        self.assertIsNone(worker._wake_claim)
        self.assertTrue(claim["done"].is_set(), "le fil de renouvellement s'arrête")
        self.vieillit_claim(lot)
        again = self.redelivre(worker)
        self.assertIsNotNone(again, "claim expiré : le réveil revient")
        self.assertEqual(again["lot"], str(lot))
        self.assertFalse(any(e["note"].startswith("wake:")
                             for e in work.events(self.db, lot)),
                         "pas de témoin pour un claim non consommé")

    def test_reveil_programme_tour_refuse_oublie_son_claim(self):
        """#2614 : le tour qui porte le réveil oublie son claim en sortant sans
        lancement (drainage)."""
        worker, lot, spec = self._reveil_reclame("r7e")
        claim = worker._wake_claim
        worker.runner.draining.set()
        self.assertFalse(worker.run_turn(spec))
        self.assertIsNotNone(work.get(self.db, lot)["wake_ts"])
        self.assert_claim_oublie(worker, lot, claim)

    def test_reveil_programme_bascule_abandonnee_oublie_son_claim(self):
        """#2614 : le réveil réclamé par `pick` ne part pas dans ce tour (bascule
        de session abandonnée, ou base tombée pendant la bascule) : oublié."""
        worker = self._worker("r7g")
        lot = work.add(self.db, title="séance owner", assignee="r7g",
                       force=_FORCE)["id"]
        work.note(self.db, lot, "hold: séance owner", actor="r7g",
                  wake_ts=time.time() - 1)
        worker._scheduled_wake_checked = float("-inf")
        worker._lot_wake_checked = float("-inf")
        tenus = []

        def abandon(spec):
            tenus.append(worker._wake_claim)
            return None

        with mock.patch.object(worker, "rotate_for_lot", side_effect=abandon):
            self.assertIsNone(worker._pick_for_turn())
        self.assert_claim_oublie(worker, lot, tenus[0])

        def panne(spec):
            tenus.append(worker._wake_claim)
            raise db_mod.Unavailable("base injoignable (témoin)")

        self.vieillit_claim(lot)
        worker._scheduled_wake_checked = float("-inf")
        with mock.patch.object(worker, "rotate_for_lot", side_effect=panne):
            with self.assertRaises(db_mod.Unavailable):
                worker._pick_for_turn()
        self.assertEqual(tenus[1]["lot"], lot)
        self.assert_claim_oublie(worker, lot, tenus[1])

    def test_reveil_programme_panne_au_controle_du_placement_oublie_son_claim(self):
        """#2614 (complément bloquant de la relecture, 20/10 19:41-19:44) :
        après le claim, `_moved_before_turn` lit le registre
        (`placed_elsewhere`) ; une `db_mod.Unavailable` y est rattrapée par
        `run()` (« choosing the turn »), qui continue. Le claim ne doit pas
        rester posé : un tour ultérieur, sans réveil, ne le renouvelle pas."""
        worker, lot, spec = self._reveil_reclame("r7h")
        claim = worker._wake_claim
        with mock.patch.object(registry, "get",
                               side_effect=db_mod.Unavailable("base injoignable (témoin)")):
            with self.assertRaises(db_mod.Unavailable):
                worker._moved_before_turn(spec)
        self.assert_claim_oublie(worker, lot, claim)

    def test_reveil_programme_place_ailleurs_oublie_son_claim(self):
        """#2614 : agent placé sur un autre hôte avant le tour — le worker rend
        son bail, le réveil réclamé ici est oublié et revient."""
        worker, lot, spec = self._reveil_reclame("r7i")
        claim = worker._wake_claim
        with mock.patch.object(worker, "placed_elsewhere", return_value="autre-hote"):
            self.assertTrue(worker._moved_before_turn(spec))
        self.assert_claim_oublie(worker, lot, claim)

    def test_reveil_programme_place_ici_garde_son_claim(self):
        worker, lot, spec = self._reveil_reclame("r7j")
        claim = worker._wake_claim
        self.assertFalse(worker._moved_before_turn(spec))
        self.assertIs(worker._wake_claim, claim)
        self.assertFalse(claim["done"].is_set())

    def test_reveil_reclame_puis_reassigne_ne_part_pas_et_reste_au_nouvel_assigne(self):
        """#5006 (B1 cx2, dev3) : claim par A, réassignation vers B, tentative de
        lancement par A — aucun harnais, rien de comptable ouvert, et l'échéance
        revient à B à l'expiration du bail de A."""
        worker, lot, spec = self._reveil_reclame("r7k")
        wake_ts = work.get(self.db, lot)["wake_ts"]
        nouveau = self._worker("r7l")
        work.assign(self.db, lot, "r7l", actor="orch", force=_FORCE)
        avant = len(self.turns()) if os.path.exists(self.turns_log) else 0
        with mock.patch.dict(os.environ, {
                "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
                "AMEESH_SESSION_STORE_CHECK": "0",
                "AMEESH_TEST_NEW_SESSION": "sess-5006"}), \
                mock.patch.object(worker, "_wake_outdated",
                                  wraps=worker._wake_outdated) as garde:
            self.assertFalse(worker.run_turn(spec))
        garde.assert_called_once()
        self.assertEqual(len(self.turns()) if os.path.exists(self.turns_log) else 0,
                         avant, "aucun harnais pour l'ancien assigné")
        self.assertEqual(registry.get(self.db, "r7k")["status"], "idle",
                         "`begin_turn` défait : plus de place `running`")
        self.assertIsNone(registry.pending_spend_get(self.db, "r7k"),
                          "le marqueur comptable est retiré")
        self.assertIsNone(worker._wake_claim)
        self.assertEqual(work.get(self.db, lot)["wake_ts"], wake_ts, "échéance conservée")
        self.vieillit_claim(lot)
        again = self.redelivre(nouveau)
        self.assertEqual(again["lot"], str(lot), "le réveil revient au nouvel assigné")

    def test_consommation_apres_reassignation_laisse_l_echeance_au_nouvel_assigne(self):
        """#5006 : réassigné entre la dernière lecture et le lancement — le tour
        est parti, mais il ne consomme pas l'échéance du nouvel assigné."""
        worker, lot, spec = self._reveil_reclame("r7m")
        self.register("r7n", "claude", cwd=self._cwd("r7n"))
        work.assign(self.db, lot, "r7n", actor="orch", force=_FORCE)
        worker._turn_launched = True
        worker._consume_scheduled_wake(spec)
        self.assertIsNotNone(work.get(self.db, lot)["wake_ts"])
        self.assertFalse(any(e["note"].startswith("wake:")
                             for e in work.events(self.db, lot)))

    def test_relance_de_lot_reassigne_ne_part_pas(self):
        """#5006 : même fenêtre pour la relance d'inactivité (`_lot_wake_spec`)."""
        worker = self._worker("r7o")
        lot = work.add(self.db, title="lot actif", assignee="r7o", force=_FORCE)["id"]
        spec = {"kind": "idle", "ids": [], "lot": str(lot), "lot_wake": True,
                "prompt": "reprends"}
        self.assertFalse(worker._wake_outdated(spec))
        self.register("r7p", "claude", cwd=self._cwd("r7p"))
        work.assign(self.db, lot, "r7p", actor="orch", force=_FORCE)
        self.assertTrue(worker._wake_outdated(spec))

    def test_base_en_panne_ne_declare_pas_le_reveil_perime(self):
        worker, _lot, spec = self._reveil_reclame("r7q")
        with mock.patch.object(storage.of(self.db).lot_sessions, "claim_wake",
                               side_effect=db_mod.Unavailable("témoin")):
            self.assertFalse(worker._wake_outdated(spec))

    def test_reveil_programme_non_consomme_si_le_tour_n_est_pas_lance(self):
        """#2466 B1 (cx2, 2e tour) : tant que le harnais n'a pas démarré
        (`_turn_launched` faux : refus READY, send résident en échec), la
        consommation ne doit pas effacer l'échéance."""
        worker = self._worker("r7c")
        lot = work.add(self.db, title="séance owner", assignee="r7c",
                       force=_FORCE)["id"]
        work.note(self.db, lot, "hold: séance owner", actor="r7c",
                  wake_ts=time.time() - 1)
        spec = self.pick(worker)
        worker._turn_launched = False
        worker._consume_scheduled_wake(spec)
        self.assertIsNotNone(work.get(self.db, lot)["wake_ts"],
                             "aucune consommation avant le lancement effectif")
        self.assertFalse(any(e["note"].endswith("consumed")
                             for e in work.events(self.db, lot)))

    def test_reveil_programme_resiste_a_lot_wake_off(self):
        worker = self._worker("r8", lot_wake=False)
        lot = work.add(self.db, title="séance owner", assignee="r8",
                       force=_FORCE)["id"]
        work.note(self.db, lot, "hold: rendez-vous", actor="r8",
                  wake_ts=time.time() - 1)
        self.assertIsNotNone(self.pick(worker),
                             "un rendez-vous explicite ne dépend pas de lot_wake")

    def test_reveil_programme_passe_avant_le_courrier_en_fenetre(self):
        worker = self._worker("r9")
        lot = work.add(self.db, title="séance owner", assignee="r9",
                       force=_FORCE)["id"]
        work.note(self.db, lot, "hold: rendez-vous", actor="r9",
                  wake_ts=time.time() - 1)
        mail.send(self.db, "orch", "r9", "un message en fenêtre de regroupement")
        spec = self.pick(worker)
        self.assertEqual(spec["lot"], str(lot),
                         "le rendez-vous passe avant la fenêtre L125")


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

    def test_la_purge_ne_ferme_jamais_la_session_active(self):
        """B1 (revue Claude du 14/10) : le relevé des branches n'a pas le bail
        de l'agent et peut tomber en plein tour : il ne touche plus au registre.
        La session ACTIVE d'un lot fini est laissée à l'agent, qui la ferme à
        son tour suivant."""
        runner, lease = self.bail("p2")
        a = work.add(self.db, title="A", assignee="p2")["id"]
        storage.of(self.db).operations.set_session_work_item(
            "p2", runner.runner_id, lease["lease_epoch"], str(a))
        registry.set_session(self.db, "p2", "sess-A", None, harness="claude",
                             cwd=self._cwd("p2"))
        self.st.note_wake("p2", a, time.time())
        work.close(self.db, a, abandoned=True, actor="test")
        purges = runner.lot_sessions_purge(self.db)
        self.assertEqual([r["work_item_id"] for r in purges], [a])
        agent = registry.get(self.db, "p2")
        self.assertEqual((agent["session_id"], agent["session_work_item"]),
                         ("sess-A", str(a)), "le balayage ne ferme aucune session")

    def test_un_lot_non_assigne_ne_ferme_pas_la_session(self):
        """B1a : un relecteur, le pilote ou l'auditeur qui travaille sur un lot
        dont il n'est pas l'assigné garde sa session, même sans changer de lot
        (elle n'est fermée que par un lot FINI, jamais par une réassignation)."""
        runner, lease = self.bail("p3")
        worker = AgentWorker(runner, registry.get(self.db, "p3"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        a = work.add(self.db, title="A", assignee="p3")["id"]
        work.assign(self.db, a, "human:quelqu-un")        # relecture par un autre
        storage.of(self.db).operations.set_session_work_item(
            "p3", runner.runner_id, lease["lease_epoch"], str(a))
        registry.set_session(self.db, "p3", "sess-relecture", None, harness="claude",
                             cwd=self._cwd("p3"))
        worker.agent = registry.get(self.db, "p3")
        self.assertEqual(runner.lot_sessions_purge(self.db), [])
        self.assertFalse(worker._close_finished_lot_session(str(a)))
        spec = worker.rotate_for_lot({"kind": "prompt", "prompt": "relecture",
                                      "ids": [], "lot": str(a)})
        self.assertEqual(spec["lot"], str(a))
        agent = registry.get(self.db, "p3")
        self.assertEqual((agent["session_id"], agent["session_work_item"]),
                         ("sess-relecture", str(a)))

    def test_la_purge_ne_touche_pas_un_agent_passe_a_un_autre_lot(self):
        runner, lease = self.bail("p4")
        a = work.add(self.db, title="A", assignee="p4")["id"]
        b = work.add(self.db, title="B", assignee="p4")["id"]
        storage.of(self.db).operations.set_session_work_item(
            "p4", runner.runner_id, lease["lease_epoch"], str(b))
        registry.set_session(self.db, "p4", "sess-B", None, harness="claude",
                             cwd=self._cwd("p4"))
        self.st.note_wake("p4", a, time.time())
        work.close(self.db, a, abandoned=True, actor="test")
        purges = runner.lot_sessions_purge(self.db)
        self.assertEqual([r["work_item_id"] for r in purges], [a])
        agent = registry.get(self.db, "p4")
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


class ReveilProgrammeCliTest(_Base):
    """#2466 : `work wake` et `work note --wake-at` arment l'échéance."""

    def test_work_wake_arme_affiche_et_annule(self):
        self.register("c1", "claude")
        lot = work.add(self.db, title="séance owner", assignee="c1",
                       force=_FORCE)["id"]
        proc = self.mesh("work", "wake", str(lot), "--in", "15m",
                         "--why", "séance owner", "--actor", "tester")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("wake scheduled", proc.stdout)
        self.assertIsNotNone(work.get(self.db, lot)["wake_ts"])
        self.assertIn("wake: séance owner", work.events(self.db, lot)[0]["note"])
        proc = self.mesh("work", "show", str(lot))
        self.assertIn("wake     :", proc.stdout)
        proc = self.mesh("work", "wake", str(lot), "--cancel", "--actor", "tester")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cancelled", proc.stdout)
        self.assertIsNone(work.get(self.db, lot)["wake_ts"])

    def test_work_note_porte_l_echeance(self):
        self.register("c2", "claude")
        lot = work.add(self.db, title="séance owner", assignee="c2",
                       force=_FORCE)["id"]
        proc = self.mesh("work", "note", str(lot), "hold: séance owner",
                         "--wake-at", "2999-01-01T00:00:00+00:00",
                         "--actor", "tester")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNotNone(work.get(self.db, lot)["wake_ts"])
        self.assertIn("[réveil programmé:", work.events(self.db, lot)[0]["note"])

    def test_at_sans_fuseau_est_refuse(self):
        self.register("c3", "claude")
        lot = work.add(self.db, title="séance owner", assignee="c3",
                       force=_FORCE)["id"]
        proc = self.mesh("work", "wake", str(lot), "--at",
                         "2999-01-01T00:00:00", "--actor", "tester")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("timezone", proc.stderr)


class WakeDeadlineCliTest(unittest.TestCase):
    """The absolute CLI deadline reaches the shared deadline validator."""

    def test_absolute_deadline_is_parsed(self):
        from ameesh.mesh_cli import build_parser, _wake_deadline
        args = build_parser().parse_args([
            "work", "wake", "1", "--at", "2999-01-01T00:00:00Z"])
        self.assertIsNotNone(_wake_deadline(args))

    def test_absolute_and_relative_deadlines_are_rejected(self):
        from ameesh.mesh_cli import build_parser, _wake_deadline
        args = build_parser().parse_args([
            "work", "wake", "1", "--at", "2999-01-01T00:00:00Z",
            "--in", "15m"])
        with self.assertRaisesRegex(work.WorkError, "exactly one"):
            _wake_deadline(args)


class WakeGrantTest(unittest.TestCase):
    """#2466 : le GRANT du superviseur porte la colonne lue par `item_columns`."""

    def test_role_superviseur_lit_wake_at(self):
        path = os.path.join(REPO, "deploy/sql/role-superviseur.sql")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        block = text.split("('work_items', ARRAY[", 1)[1].split("]),", 1)[0]
        self.assertIn("'wake_at'", block)
        self.assertIn("'wake_claim_at'", block)
