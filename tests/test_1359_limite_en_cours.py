# SPDX-License-Identifier: AGPL-3.0-only
"""#1359 : limite d'en-cours (§4 du canon, owner 14/10/2026) — au plus
`work.BUILD_LIMIT` lots en `build` par agent, surplus rendu à la file d'équipe.

Couvre : la prise atomique sous plafond (`assign_capped`), le refus du
troisième lot dans la prise automatique, la restitution CAS (`work return`),
le balayage idempotent (`ameesh work wip`) avec son dry-run et son courrier,
l'exception pilote explicite, et les compteurs de `ameesh list` / progress.
"""
from __future__ import annotations

import time

from ameesh import backlog, notify, progress, registry, storage, work, wip

from .support import PgTestCase

HEURE = 3600.0


class _Base(PgTestCase):
    def agent(self, name, harness="deepseek", *, status="idle", team="ameesh",
              since_s=2 * HEURE):
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = %s, lease_owner = %s,"
            " lease_expires_at = now() + interval '1 hour', team = %s,"
            " status_since = now() - make_interval(secs => %s),"
            " last_turn_at = now() - make_interval(secs => %s)"
            " WHERE name = %s",
            (status, "runner-%s@h" % name, team, since_s, since_s, name))

    def lot(self, title, *, app="ameesh", assignee=None, branch=None, age_s=HEURE):
        item = work.add(self.db, title=title, app=app, assignee=assignee, branch=branch)
        # un lot tout juste créé n'est pas encore prenable (LOT_MIN_AGE_S) :
        # l'ancienneté est simulée, comme dans les tests de la prise L165
        self.db.execute("UPDATE work_items SET created_at = created_at - make_interval("
                        "secs => %s) WHERE id = %s", (age_s, item["id"]))
        return work.get(self.db, item["id"])

    def build(self, item):
        return work.move(self.db, item["id"], "build", note="en build", actor="human:proprio")

    def aged(self, item_id, hours):
        """Recule toute l'activité d'un lot (le balayage rend les moins récents)."""
        self.db.execute("UPDATE work_items SET updated_at = now() - make_interval("
                        "hours => %s) WHERE id = %s", (int(hours), item_id))
        self.db.execute("UPDATE work_item_events SET created_at = now() - make_interval("
                        "hours => %s) WHERE work_item_id = %s", (int(hours), item_id))

    def builds_of(self, assignee):
        return storage.of(self.db).work.count_builds(assignee)

    def courriers(self, recipient=None):
        sql = ("SELECT sender, recipient, kind, work_item_id, body FROM agent_mailbox")
        params: tuple = ()
        if recipient:
            sql += " WHERE recipient = %s"
            params = (recipient,)
        return self.db.query(sql + " ORDER BY id", params)

    def take(self, **kw):
        kw.setdefault("now", time.time())
        kw.setdefault("paid", ("deepseek",))
        kw.setdefault("budget_check", lambda agent: "")
        kw.setdefault("orchestras", [])
        return backlog.auto_take(self.cfg, self.db, **kw)


class PriseSousPlafondTest(_Base):
    def test_prise_atomique_sous_le_plafond(self):
        self.agent("dev1")
        self.agent("dev2")
        for i in range(2):
            self.build(self.lot("occupé %d" % i, assignee="dev1"))
        st = storage.of(self.db).work
        libre1 = self.lot("libre 1")
        self.assertIsNone(st.assign_capped(libre1["id"], "dev1", current=None,
                                           note="prise", actor="ameesh",
                                           max_build=work.BUILD_LIMIT))
        self.assertEqual(self.builds_of("dev1"), 2)
        self.assertIsNone(work.get(self.db, libre1["id"])["assignee"])
        # sous le plafond, la prise passe
        libre2 = self.lot("libre 2")
        self.assertIsNotNone(st.assign_capped(libre2["id"], "dev2", current=None,
                                              note="prise", actor="ameesh",
                                              max_build=work.BUILD_LIMIT))
        self.assertEqual(work.get(self.db, libre2["id"])["assignee"], "dev2")
        # CAS : un lot qui a bougé sous nos pieds n'est pas assigné
        libre3 = self.lot("libre 3")
        self.assertIsNone(st.assign_capped(libre3["id"], "dev2", current="quelqu_un",
                                           note="prise", actor="ameesh",
                                           max_build=work.BUILD_LIMIT))
        self.assertIsNone(work.get(self.db, libre3["id"])["assignee"])

    def test_la_prise_automatique_ignore_l_agent_au_plafond(self):
        self.agent("dev1")
        self.agent("dev2")
        for i in range(2):
            self.build(self.lot("occupé %d" % i, assignee="dev1"))
        libre = self.lot("à prendre")
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("dev2", libre["id"])])
        self.assertEqual(work.get(self.db, libre["id"])["assignee"], "dev2")


class RestitutionTest(_Base):
    def test_retour_cas_conserve_branche_et_journal(self):
        self.agent("dev1")
        lot = self.build(self.lot("avec branche", assignee="dev1",
                                  branch="lot/1359-limite-en-cours"))
        row = work.return_to_queue(self.db, lot["id"], actor="dev1", cfg=self.cfg)
        self.assertIsNone(row["assignee"])
        self.assertEqual(row["branch"], "lot/1359-limite-en-cours")
        self.assertEqual(row["state"], "build")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any(n.startswith(wip.RETURN_NOTE) for n in notes), notes)
        self.assertEqual(self.builds_of("dev1"), 0)
        # déjà rendu : rien à rendre
        with self.assertRaises(work.WorkError):
            work.return_to_queue(self.db, lot["id"], actor="dev1", cfg=self.cfg)

    def test_un_agent_ne_rend_pas_le_lot_d_un_autre(self):
        self.agent("dev1")
        self.agent("dev2")
        lot = self.build(self.lot("à dev1", assignee="dev1"))
        with self.assertRaises(work.WorkError):
            work.return_to_queue(self.db, lot["id"], actor="dev2", cfg=self.cfg)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")
        # un humain rend le lot
        row = work.return_to_queue(self.db, lot["id"], actor="human:proprio", cfg=self.cfg)
        self.assertIsNone(row["assignee"])

    def test_restitution_cas_refusee_si_l_assigne_a_bouge(self):
        self.agent("dev1")
        lot = self.build(self.lot("cas", assignee="dev1"))
        st = storage.of(self.db).work
        self.assertIsNone(st.unassign(lot["id"], current="dev2", note="n", actor="ameesh"))
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_le_lot_rendu_reste_prenable_avant_les_intake(self):
        self.agent("dev1")
        self.agent("dev2")
        lot = self.build(self.lot("rendu", assignee="dev1", branch="lot/rendu"))
        work.return_to_queue(self.db, lot["id"], actor="dev1", cfg=self.cfg)
        st = storage.of(self.db).work
        rows = st.unassigned_lots(50)
        self.assertIn(lot["id"], [int(i["id"]) for i in rows])
        intake = self.lot("intake neuf")
        ids = [int(i["id"]) for i in st.unassigned_lots(50)]
        self.assertLess(ids.index(lot["id"]), ids.index(intake["id"]))
        row = next(i for i in rows if int(i["id"]) == lot["id"])
        self.assertTrue(row["returned_to_queue"])
        self.assertTrue(backlog.lot_takeable(row))

    def test_un_build_jamais_rendu_n_est_pas_a_la_file(self):
        # L165/#1193 : un lot `build` sans assigné n'est pas forcément rendu
        # (prérequis, travail confié par courrier) — il ne passe pas à la file.
        self.agent("dev1")
        lot = self.lot("prérequis")
        work.move(self.db, lot["id"], "build", note="en cours", actor="human:proprio")
        st = storage.of(self.db).work
        self.assertNotIn(lot["id"], [int(i["id"]) for i in st.unassigned_lots(50)])
        rows = {int(r["id"]): r
                for r in storage.of(self.db).operations.open_lots_activity(50)}
        self.assertFalse(rows[lot["id"]]["returned_to_queue"])
        # témoin : avec une équipe connue, le lot reste refusé tant qu'il n'a
        # pas été rendu ; le même lot marqué rendu serait prenable
        row = dict(rows[lot["id"]], lot_team="ameesh", required_capabilities=[])
        self.assertFalse(backlog.lot_takeable(row))
        self.assertTrue(backlog.lot_takeable(dict(row, returned_to_queue=True)))
        self.assertEqual(self.take(), [])

    def test_un_element_de_la_file_en_build_doit_etre_rendu(self):
        self.agent("dev1")
        item = backlog.add(self.db, title="dette", value_score=50,
                           expected_value="gain mesurable")
        work.assign(self.db, item["id"], "dev1", actor="human:proprio", cfg=self.cfg)
        work.move(self.db, item["id"], "build", note="en build", actor="human:proprio")
        self.assertNotIn(item["id"], [int(i["id"]) for i in backlog.items(self.db)])
        work.return_to_queue(self.db, item["id"], actor="human:proprio", cfg=self.cfg)
        self.assertIn(item["id"], [int(i["id"]) for i in backlog.items(self.db)])

    def test_la_prise_automatique_reprend_un_lot_rendu(self):
        self.agent("dev1")
        self.agent("dev2")
        for i in range(2):      # dev1 reste au plafond après la restitution
            self.build(self.lot("occupé %d" % i, assignee="dev1"))
        lot = self.build(self.lot("rendu", assignee="dev1", branch="lot/rendu"))
        work.return_to_queue(self.db, lot["id"], actor="dev1", cfg=self.cfg)
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("dev2", lot["id"])])
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["assignee"], item["branch"], item["state"]),
                         ("dev2", "lot/rendu", "build"))


class BalayageTest(_Base):
    def test_dry_run_puis_apply_idempotent(self):
        self.agent("dev1")
        lots = []
        for i in range(4):
            item = self.build(self.lot("occupé %d" % i, assignee="dev1",
                                       branch="lot/1359-%d" % i))
            self.aged(item["id"], 10 - i)   # lots[0] le moins récemment actif
            lots.append(item)
        out = wip.sweep(self.cfg, self.db, dry_run=True)
        self.assertEqual([r["item"]["id"] for r in out],
                         [lots[0]["id"], lots[1]["id"]])
        self.assertTrue(all(r["dry_run"] for r in out))
        self.assertEqual(self.builds_of("dev1"), 4)
        self.assertEqual(self.courriers(), [])
        out = wip.sweep(self.cfg, self.db)
        self.assertEqual([r["item"]["id"] for r in out],
                         [lots[0]["id"], lots[1]["id"]])
        self.assertEqual(self.builds_of("dev1"), 2)
        for item in (lots[0], lots[1]):
            row = work.get(self.db, item["id"])
            self.assertIsNone(row["assignee"])
            self.assertEqual(row["state"], "build")
            self.assertEqual(row["branch"], item["branch"])
        mails = self.courriers("dev1")
        self.assertEqual([m["work_item_id"] for m in mails],
                         [str(lots[0]["id"]), str(lots[1]["id"])])
        self.assertIn("WIP limit", mails[0]["body"])
        # idempotent : plus rien à rendre, plus de courrier
        self.assertEqual(wip.sweep(self.cfg, self.db), [])
        self.assertEqual(len(self.courriers("dev1")), 2)

    def test_l_humain_et_l_exception_pilote_sont_laisses(self):
        self.agent("dev1")
        self.agent("dev2")
        human = self.build(self.lot("à un humain", assignee="human:proprio"))
        lots = []
        for i in range(4):
            item = self.build(self.lot("occupé %d" % i, assignee="dev1"))
            self.aged(item["id"], 10 - i)
            lots.append(item)
        # sans pilote : refus, et la raison est obligatoire
        with self.assertRaises(work.WorkError):
            work.assign(self.db, lots[0]["id"], "dev1", actor="dev1", cfg=self.cfg,
                        wip_exception="je garde")
        with self.assertRaises(work.WorkError):
            work.assign(self.db, lots[0]["id"], "dev1", actor="human:proprio",
                        cfg=self.cfg, wip_exception="   ")
        # le pilote pose l'exception sur le plus ancien : il reste
        work.assign(self.db, lots[0]["id"], "dev1", actor="human:proprio", cfg=self.cfg,
                    wip_exception="reprise urgente")
        out = wip.sweep(self.cfg, self.db)
        self.assertEqual([r["item"]["id"] for r in out], [lots[1]["id"]])
        self.assertEqual(work.get(self.db, lots[0]["id"])["assignee"], "dev1")
        self.assertEqual(work.get(self.db, human["id"])["assignee"], "human:proprio")
        # une réassignation postérieure sans marqueur efface l'exception
        work.assign(self.db, lots[0]["id"], "dev2", actor="human:proprio", cfg=self.cfg)
        self.assertEqual(
            storage.of(self.db).work.wip_exceptions([lots[0]["id"]],
                                                    work.WIP_EXCEPTION_MARK,
                                                    work.ASSIGN_NOTES),
            [])

    def test_cli_wip_et_return(self):
        self.agent("dev1")
        for i in range(3):
            lot = self.build(self.lot("cli %d" % i, assignee="dev1"))
        proc = self.mesh("work", "wip")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dry run: nothing written", proc.stdout)
        self.assertEqual(self.builds_of("dev1"), 3)
        proc = self.mesh("work", "return", str(lot["id"]), "--actor", "dev1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("returned to the team queue", proc.stdout)
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])


class CompteursTest(_Base):
    def test_list_montre_l_en_cours_et_le_depassement(self):
        self.agent("dev1")
        self.agent("dev2")
        for i in range(3):
            self.build(self.lot("occupé %d" % i, assignee="dev1"))
        self.build(self.lot("un seul", assignee="dev2"))
        proc = self.mesh("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        header = lines[0]
        self.assertIn("BUILD", header)
        col = header.index("BUILD")
        row = next(line for line in lines if line.startswith("dev1"))
        self.assertEqual(row[col:col + 5].strip(), "3!")
        row = next(line for line in lines if line.startswith("dev2"))
        self.assertEqual(row[col:col + 5].strip(), "1")
        self.assertIn("wip      : 1 agent(s) above the §4 cap (2 lots in build): dev1",
                      proc.stdout)

    def test_progress_porte_l_en_cours_tous_projets(self):
        self.agent("dev1", team="ameesh")
        self.agent("dev2", team="nexlink")
        for i in range(3):
            self.build(self.lot("ameesh %d" % i, app="ameesh", assignee="dev1"))
        self.build(self.lot("nexlink", app="nexlink", assignee="dev1", branch="lot/nx"))
        snap = progress.snapshot(self.db, self.cfg)
        agents = {a["name"]: a for a in snap["agents"]}
        # tous projets confondus : le plafond est global, pas par équipe
        self.assertEqual(agents["dev1"]["builds"], 4)
        self.assertEqual(agents["dev2"]["builds"], 0)
        text = progress.format_text(snap, width=100)
        self.assertIn("4 lots in build (above the §4 cap of 2)", text)


class BranchementNotifyTest(_Base):
    """§4 : le balayage est branché sur notify, coupé par défaut ; sans
    `--wip-apply` (ou sous le `--dry-run` global) il ne rend rien."""

    def test_le_balayage_notify_est_coupe_par_defaut(self):
        parser = notify.build_parser()
        self.assertIsNone(notify.wip_settings(parser.parse_args([])))
        self.assertEqual(notify.wip_settings(parser.parse_args(["--wip-max-returns", "3"])),
                         {"limit": 3, "apply": False})
        self.assertEqual(
            notify.wip_settings(parser.parse_args(["--wip-max-returns", "3", "--wip-apply"])),
            {"limit": 3, "apply": True})

    def _trois_lots(self):
        self.agent("dev1")
        lots = []
        for i in range(3):
            item = self.build(self.lot("occupé %d" % i, assignee="dev1"))
            self.aged(item["id"], 10 - i)   # lots[0] le moins récemment actif
            lots.append(item)
        return lots

    def test_notify_sans_apply_ne_rend_rien_et_releve(self):
        lots = self._trois_lots()
        records = []
        notifier = notify.Notifier(self.cfg, notify.parse_config({}), log=lambda t: None,
                                   emit=records.append, clock=time.time,
                                   wip={"limit": 10, "apply": False})
        notifier.run_pass(self.db, thresholds={})
        self.assertEqual(self.builds_of("dev1"), 3)
        self.assertEqual(self.courriers(), [])
        releves = [r for r in records if r["event"] == "wip_return"]
        self.assertEqual([r["lot"] for r in releves], [lots[0]["id"]])
        self.assertTrue(all(r["delivery"] == "a_blanc" for r in releves))
        self.assertIn("WIP return", notify._format(releves[0]))

    def test_notify_apply_rend_le_surplus_et_previ_ent_l_agent(self):
        lots = self._trois_lots()
        records = []
        notifier = notify.Notifier(self.cfg, notify.parse_config({}), log=lambda t: None,
                                   emit=records.append, clock=time.time,
                                   wip={"limit": 10, "apply": True})
        notifier.run_pass(self.db, thresholds={})
        self.assertEqual(self.builds_of("dev1"), 2)
        self.assertIsNone(work.get(self.db, lots[0]["id"])["assignee"])
        self.assertEqual(work.get(self.db, lots[1]["id"])["assignee"], "dev1")
        releves = [r for r in records if r["event"] == "wip_return"]
        self.assertEqual([r["lot"] for r in releves], [lots[0]["id"]])
        self.assertEqual(releves[0]["delivery"], "rendu")
        mails = self.courriers("dev1")
        self.assertEqual([m["work_item_id"] for m in mails], [str(lots[0]["id"])])

    def test_le_dry_run_global_force_le_dry_run(self):
        self._trois_lots()
        notifier = notify.Notifier(self.cfg, notify.parse_config({}), log=lambda t: None,
                                   emit=lambda r: None, clock=time.time, dry_run=True,
                                   wip={"limit": 10, "apply": True})
        notifier.run_pass(self.db, thresholds={})
        self.assertEqual(self.builds_of("dev1"), 3)
        self.assertEqual(self.courriers(), [])
