# SPDX-License-Identifier: AGPL-3.0-only
"""#540 (TEAM-QUEUE) : au plus un lot non démarré en attente par agent, imposé à
l'attribution ; le reste va dans la file d'équipe (L165).

Un lot non démarré : ouvert, sans début mesuré (`started_at`). Attribuer un
lot non démarré à un agent qui en a déjà un autre le met dans la file
d'équipe (sans assigné, en `intake`), où un agent au repos le prendra (L165)."""
from __future__ import annotations

import threading
import time
from unittest import mock

from ameesh import db as db_mod, estimates, registry, storage, work
from ameesh.storage.postgres import work as pg_work

from .support import PgTestCase
from .test_l119_backlog import HEURE, _Base as _BacklogBase


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1", "dev2"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)

    def lot(self, title, assignee=None, **kw):
        return work.add(self.db, title=title, app="ameesh", assignee=assignee,
                        actor="orch", **kw)

    def assignee(self, item):
        return work.get(self.db, int(item["id"]))["assignee"]


class AttributionTest(_Base):
    def test_second_lot_non_demarre_en_file_d_equipe(self):
        first = self.lot("A", "dev1")
        self.assertEqual(self.assignee(first), "dev1")
        second = self.lot("B", "dev1")
        self.assertIsNone(self.assignee(second))
        self.assertEqual(second["state"], "intake")
        self.assertEqual(second["queued_behind"], int(first["id"]))
        self.assertIn("team queue", second["queue_warning"])
        self.assertIn("#%s" % first["id"], second["queue_warning"])
        # un autre agent n'est pas concerné
        self.assertEqual(self.assignee(self.lot("C", "dev2")), "dev2")

    def test_lot_demarre_libere_la_place(self):
        first = self.lot("A", "dev1")
        estimates.mark_started(self.db, "dev1", [first["id"]])
        second = self.lot("B", "dev1")
        self.assertEqual(self.assignee(second), "dev1")
        self.assertNotIn("queued_behind", second)
        # un lot passé en build compte aussi comme démarré
        work.move(self.db, int(second["id"]), "build", actor="dev1")
        self.assertEqual(self.assignee(self.lot("C", "dev1")), "dev1")

    def test_lot_ferme_libere_la_place(self):
        first = self.lot("A", "dev1")
        work.close(self.db, int(first["id"]), actor="orch", abandoned=True)
        self.assertEqual(self.assignee(self.lot("B", "dev1")), "dev1")

    def test_reassigner_un_lot_non_demarre(self):
        self.lot("A", "dev1")
        other = self.lot("B", "dev2")
        out = work.assign(self.db, int(other["id"]), "dev1", actor="orch")
        # retiré à dev2, mis en file : jamais deux lots non démarrés chez dev1
        self.assertIsNone(self.assignee(other))
        self.assertIsNone(out["item"]["assignee"])
        self.assertIn("team queue", out["queue_warning"])

    def test_reassigner_un_lot_demarre_reste_permis(self):
        self.lot("A", "dev1")
        other = self.lot("B", "dev2")
        estimates.mark_started(self.db, "dev2", [other["id"]])
        out = work.assign(self.db, int(other["id"]), "dev1", actor="orch")
        self.assertEqual(self.assignee(other), "dev1")
        self.assertNotIn("queue_warning", out)

    def test_lot_sans_equipe_attend_l_orchestrateur(self):
        self.lot("A", "dev1")
        second = work.add(self.db, title="B", assignee="dev1", actor="orch")
        self.assertIsNone(self.assignee(second))
        self.assertIn("no team", second["queue_warning"])

    def test_journal_du_lot(self):
        first = self.lot("A", "dev1")
        second = self.lot("B", "dev1")
        notes = [e["note"] for e in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(second["id"]),))]
        self.assertTrue(any("team queue" in n and "dev1" in n and "#%s" % first["id"] in n
                            for n in notes), notes)


class CliTest(_Base):
    def test_work_add_et_assign(self):
        self.lot("A", "dev1")
        proc = self.mesh("work", "add", "--title", "B", "--app", "ameesh",
                        "--assignee", "dev1", "--estimate", "1h")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("team queue", proc.stderr)
        lot = self.lot("C", "dev2")
        proc = self.mesh("work", "assign", str(lot["id"]), "dev1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("team queue", proc.stderr)
        self.assertIsNone(self.assignee(lot))

    def test_courrier_lot_en_file_message_non_depose(self):
        self.lot("A", "dev1")
        lot = self.lot("B")
        env = self.env(AMEESH_ALERT_ORCHESTRATORS="orch")
        proc = self.cli("send", "dev1", "Prends ce lot.", "--from", "orch",
                        "--lot", str(lot["id"]), env=env)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("team queue", proc.stderr)
        self.assertIsNone(self.assignee(lot))
        self.assertEqual(self.db.query("SELECT id FROM agent_mailbox WHERE recipient = 'dev1'"),
                         [])
        proc = self.cli("send", "dev1", "Et celui-ci.", "--from", "orch",
                        "--new-lot", "C", "--estimate", "1h", "--app", "ameesh", env=env)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("team queue", proc.stderr)
        created = self.db.query("SELECT assignee, state FROM work_items WHERE title = 'C'")
        self.assertEqual(created, [{"assignee": None, "state": "intake"}])
        self.assertEqual(self.db.query("SELECT id FROM agent_mailbox WHERE recipient = 'dev1'"),
                         [])


class ForcageTest(_Base):
    """Canon §4 : « sauf --force motivé » — pour tout appelant, raison non vide,
    écrite au journal du lot avec son auteur."""

    def notes(self, item):
        return [e["note"] for e in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(item["id"]),))]

    def test_force_motive(self):
        first = self.lot("A", "dev1")
        second = self.lot("B", "dev1", force="client en attente, priorité du jour")
        self.assertEqual(self.assignee(second), "dev1")
        self.assertEqual(second["forced_behind"], int(first["id"]))
        self.assertNotIn("queue_warning", second)
        self.assertTrue(any("forced by orch" in n and "client en attente" in n
                            for n in self.notes(second)), self.notes(second))
        other = self.lot("C", "dev2")
        out = work.assign(self.db, int(other["id"]), "dev1", actor="dev2",
                          force="relais {urgent}")
        self.assertEqual(self.assignee(other), "dev1")
        self.assertTrue(any("forced by dev2" in n and "relais {urgent}" in n
                            for n in self.notes(other)), self.notes(other))
        self.assertNotIn("queue_warning", out)

    def test_force_sans_raison_comme_le_cas_normal(self):
        self.lot("A", "dev1")
        for reason in ("", "   "):
            second = self.lot("B", "dev1", force=reason)
            self.assertIsNone(self.assignee(second))
            self.assertIn("team queue", second["queue_warning"])

    def test_force_en_cli_et_courrier(self):
        self.lot("A", "dev1")
        lot = self.lot("B", "dev2")
        proc = self.mesh("work", "assign", str(lot["id"]), "dev1", "--force", "revue urgente",
                         env=self.env(AGENT_MAIL_NAME="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.assignee(lot), "dev1")
        proc = self.mesh("work", "add", "--title", "C", "--app", "ameesh", "--assignee",
                         "dev1", "--estimate", "1h", "--force", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("team queue", proc.stderr)
        env = self.env(AGENT_MAIL_NAME="orch", AMEESH_ALERT_ORCHESTRATORS="orch")
        proc = self.cli("send", "dev1", "Et celui-ci.", "--from", "orch", "--new-lot", "D",
                        "--estimate", "1h", "--app", "ameesh", "--force", "demande du client",
                        env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        created = self.db.query("SELECT assignee FROM work_items WHERE title = 'D'")
        self.assertEqual(created, [{"assignee": "dev1"}])
        self.assertEqual(len(self.db.query(
            "SELECT id FROM agent_mailbox WHERE recipient = 'dev1'")), 1)


    def test_l_auteur_du_forcage_est_l_identite_de_la_session(self):
        """Revue r1 (1) : une session liée à dev2 ne force pas au nom d'un
        autre ; le journal nomme l'identité liée."""
        self.lot("A", "dev1")
        lot = self.lot("B", "dev2")
        as_dev2 = self.env(AGENT_MAIL_NAME="dev2")
        proc = self.mesh("work", "assign", str(lot["id"]), "dev1", "--force", "urgent",
                         "--actor", "human:owner", env=as_dev2)
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("on behalf of human:owner", proc.stderr)
        self.assertEqual(self.assignee(lot), "dev2")
        self.assertFalse(any("forced" in (n or "") for n in self.notes(lot)))
        proc = self.mesh("work", "assign", str(lot["id"]), "dev1", "--force", "urgent",
                         env=as_dev2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(any("forced by dev2" in (n or "") for n in self.notes(lot)),
                        self.notes(lot))
        # le courrier : --from différent de l'identité liée, rien de déposé
        proc = self.cli("send", "dev1", "Et celui-ci.", "--from", "orch", "--new-lot", "C",
                        "--estimate", "1h", "--app", "ameesh", "--force", "urgent",
                        env=self.env(AGENT_MAIL_NAME="dev2",
                                     AMEESH_ALERT_ORCHESTRATORS="orch"))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("on behalf of orch", proc.stderr)
        self.assertEqual(self.db.query("SELECT id FROM work_items WHERE title = 'C'"), [])
        # sans identité ni --actor : personne à écrire au journal, refus
        with self.assertRaises(work.WorkError):
            work.forcer(self.cfg, self.db, None)
        self.assertEqual(work.forcer(self.cfg, self.db, "human:owner"), "human:owner")

    def test_sans_liaison_un_nom_d_agent_est_refuse_par_les_deux_cli(self):
        """Revue r2 (B/P2) : une session sans identité liée ne force pas au nom
        d'un agent ; seul un humain explicite (`human:<id>`) force. Les deux
        CLI refusent avant d'écrire quoi que ce soit."""
        self.lot("A", "dev1")
        lot = self.lot("B", "dev2")
        events = len(work.events(self.db, lot["id"]))
        with self.assertRaises(work.WorkError):
            work.forcer(self.cfg, self.db, "ameesh-dev5")
        self.assertEqual(work.forcer(self.cfg, self.db, "human:owner"), "human:owner")
        # work assign/add --force --actor <agent> : refus, lot et journal intacts
        proc = self.mesh("work", "assign", str(lot["id"]), "dev1", "--force", "urgent",
                         "--actor", "ameesh-dev5")
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--force refused", proc.stderr)
        self.assertEqual(self.assignee(lot), "dev2")
        self.assertEqual(len(work.events(self.db, lot["id"])), events)
        proc = self.mesh("work", "add", "--title", "C", "--app", "ameesh", "--assignee",
                         "dev1", "--force", "urgent", "--actor", "ameesh-dev5")
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--force refused", proc.stderr)
        self.assertEqual(self.db.query("SELECT id FROM work_items WHERE title = 'C'"), [])
        # agent-mail send --force --from <agent> : refus, ni lot ni message
        proc = self.cli("send", "dev1", "Prends ce lot.", "--from", "ameesh-dev5",
                        "--new-lot", "D", "--estimate", "1h", "--app", "ameesh",
                        "--force", "urgent")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--force refused", proc.stderr)
        self.assertEqual(self.db.query("SELECT id FROM work_items WHERE title = 'D'"), [])
        self.assertEqual(self.db.query(
            "SELECT id FROM agent_mailbox WHERE recipient = 'dev1'"), [])


class ExemptionsTest(_Base):
    def test_assigne_humain_hors_regle(self):
        self.lot("A", "human:owner")
        second = self.lot("B", "human:owner")
        self.assertEqual(self.assignee(second), "human:owner")


    def test_demande_de_decision_garde_son_lot(self):
        from ameesh import decisions
        self.lot("A", "dev1")
        out = decisions.ask(self.cfg, self.db, requester="dev1", question="A ou B ?",
                            options=["a=A", "b=B"], new_lot="Choix")
        self.assertEqual(work.get(self.db, int(out["lot"]))["assignee"], "dev1")

    def test_l_exemption_de_decision_n_est_pas_un_parametre(self):
        """Revue r1 (2) : `work.add` n'offre aucun contournement ; seul
        `decisions.ask` garde le lot de sa question au demandeur."""
        self.lot("A", "dev1")
        with self.assertRaises(TypeError):
            work.add(self.db, title="B", app="ameesh", assignee="dev1", actor="orch",
                     team_queue=False)
        second = work.add(self.db, title="B", app="ameesh", assignee="dev1", actor="orch",
                          source="decision")
        self.assertIsNone(self.assignee(second))


class ConcurrenceTest(_Base):
    def test_controle_et_ecriture_sous_verrou_par_agent(self):
        """Une attribution concurrente qui tient le verrou de dev1 et pose un
        lot non démarré : l'attribution qui attend la voit au déblocage."""
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        done = {}
        with other.transaction() as tx:
            tx.query(pg_work.PENDING_LOCK, ("dev1",))
            thread = threading.Thread(target=lambda: done.setdefault(
                "item", self._add_in_thread()))
            thread.start()
            time.sleep(0.5)
            self.assertNotIn("item", done)            # bloquée sur le verrou
            tx.query("INSERT INTO work_items (type, source, app, title, assignee, state)"
                     " VALUES ('evolution', '', 'ameesh', 'X', 'dev1', 'intake') RETURNING id")
        thread.join(10)
        self.assertIsNone(done["item"]["assignee"])

    def _add_in_thread(self):
        conn = db_mod.connect(self.cfg)
        try:
            return work.add(conn, title="B", app="ameesh", assignee="dev1", actor="orch")
        finally:
            conn.close()


class PriseAutomatiqueTest(_BacklogBase):
    """Revue r1 (3) : la prise automatique (L119, L165) suit la même règle, sous
    le même verrou par agent."""

    def setUp(self):
        super().setUp()
        self.agent("claude1", team="nexlink")

    def queued(self, title):
        item = work.add(self.db, title=title, app="nexlink")
        self.db.execute("UPDATE work_items SET created_at = created_at - make_interval("
                        "secs => %s) WHERE id = %s", (HEURE, item["id"]))
        return item

    def test_course_avec_une_attribution(self):
        """claude1, choisi au repos, reçoit un lot non démarré entre sa
        sélection et l'écriture : la prise n'en ajoute pas un second."""
        lot = self.queued("B")
        check = work.check_assignee
        pending = {}

        def concurrent_add(db, name, **kw):
            if name == "claude1" and not pending:
                pending["lot"] = None
                other = db_mod.connect(self.cfg)
                try:
                    pending["lot"] = work.add(other, title="A", app="nexlink",
                                              assignee="claude1", actor="orch")
                finally:
                    other.close()
            return check(db, name, **kw)

        with mock.patch.object(work, "check_assignee", side_effect=concurrent_add):
            out = self.take(orchestras=[])
        self.assertEqual(pending["lot"]["assignee"], "claude1")
        self.assertEqual(out, [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM work_items WHERE assignee = 'claude1'"), [{"n": 1}])

    def test_ecriture_refusee_sous_le_verrou(self):
        st = storage.of(self.db)
        first = work.add(self.db, title="A", app="nexlink", assignee="claude1", actor="orch")
        lot = self.queued("B")
        events = len(work.events(self.db, lot["id"]))
        with self.assertRaises(storage.PendingLot) as raised:
            st.work.assign(int(lot["id"]), "claude1", current=None, note="prise", actor="x",
                           take=True)
        self.assertEqual(raised.exception.lot, int(first["id"]))
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        self.assertEqual(len(work.events(self.db, lot["id"])), events)
        estimates.mark_started(self.db, "claude1", [first["id"]])
        done = st.work.assign(int(lot["id"]), "claude1", current=None, note="prise",
                              actor="x", take=True)
        self.assertEqual(done["assignee"], "claude1")
