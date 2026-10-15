# SPDX-License-Identifier: AGPL-3.0-only
"""L165 : un agent au repos prend lui-même le lot non assigné le plus
prioritaire de son équipe (extension de la prise automatique de L119)."""
from __future__ import annotations

import json
import threading
import time
import unittest
from unittest.mock import patch

from ameesh import backlog, db as db_mod, notify, storage, work

from .support import hold_for_owner
from .test_l119_backlog import HEURE, _Base, _seuils


class _Lots(_Base):
    def lot(self, title, *, app="nexlink", age_s=HEURE, **kw):
        item = work.add(self.db, title=title, app=app, **kw)
        # created_at croissant avec l'ordre de création (l'ancienneté départage)
        self.db.execute("UPDATE work_items SET created_at = created_at - make_interval("
                        "secs => %s) WHERE id = %s", (age_s, item["id"]))
        return item

    def take(self, **kw):
        kw.setdefault("orchestras", ["orchestrateur"])
        return super().take(**kw)

    def courriers(self):
        return self.db.query("SELECT sender, recipient, kind, work_item_id, body, payload"
                             " FROM agent_mailbox ORDER BY id")


class PriseDeLotTest(_Lots):
    def test_dependance_ouverte_laisse_prendre_le_lot_pret_puis_se_libere(self):
        self.agent("claude1", team="nexlink")
        amont = self.lot("amont", assignee="human:proprio")
        bloque = self.lot("prioritaire mais bloque", priority=1)
        pret = self.lot("pret", priority=2)
        storage.of(self.db).work_dependencies.add(bloque["id"], amont["id"], actor="test")

        # Le filtre précède LIMIT : le premier lot prêt reste visible.
        self.assertEqual([i["id"] for i in storage.of(self.db).work.unassigned_lots(1)],
                         [pret["id"]])
        self.assertEqual([t["item"]["id"] for t in self.take()], [pret["id"]])
        self.assertIsNone(work.get(self.db, bloque["id"])["assignee"])

        work.close(self.db, amont["id"], abandoned=True)
        self.agent("claude2", team="nexlink")
        self.assertEqual([t["item"]["id"] for t in self.take()], [bloque["id"]])

    def test_toutes_les_dependances_doivent_etre_satisfaites(self):
        self.agent("claude1", team="nexlink")
        amonts = [self.lot("amont%d" % i, assignee="human:proprio") for i in range(2)]
        lot = self.lot("deux dependances")
        for amont in amonts:
            storage.of(self.db).work_dependencies.add(lot["id"], amont["id"], actor="test")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (amonts[0]["id"],))
        self.assertEqual(self.take(), [])
        self.db.execute("UPDATE work_items SET state = 'promoted' WHERE id = %s",
                        (amonts[1]["id"],))
        self.assertEqual([t["item"]["id"] for t in self.take()], [lot["id"]])

    def test_dependance_ajoutee_apres_selection_refuse_la_prise(self):
        self.agent("claude1", team="nexlink")
        amont = self.lot("amont", assignee="human:proprio")
        bloque = self.lot("devient bloque", priority=1)
        pret = self.lot("reste pret", priority=2)
        check = work.check_assignee
        added = False

        def add_dependency(*args, **kwargs):
            nonlocal added
            if not added:
                storage.of(self.db).work_dependencies.add(
                    bloque["id"], amont["id"], actor="test")
                added = True
            return check(*args, **kwargs)

        with patch.object(work, "check_assignee", side_effect=add_dependency):
            out = self.take()
        self.assertEqual([t["item"]["id"] for t in out], [pret["id"]])
        self.assertIsNone(work.get(self.db, bloque["id"])["assignee"])
        self.assertFalse(any(m["work_item_id"] == str(bloque["id"])
                             for m in self.courriers()))
        self.assertFalse(any(e["note"].startswith(backlog.LOT_TAKE_NOTE)
                             for e in work.events(self.db, bloque["id"])))
        # Une réservation manuelle reste possible, même avant l'amont.
        work.assign(self.db, bloque["id"], "human:proprio")
        self.assertEqual(work.get(self.db, bloque["id"])["assignee"], "human:proprio")

    def test_dependance_en_cours_de_commit_refuse_la_prise_concurrente(self):
        self.agent("claude1", team="nexlink")
        amont = self.lot("amont", assignee="human:proprio")
        bloque = self.lot("devient bloque", priority=1)
        pret = self.lot("reste pret", priority=2)
        selected = threading.Event()
        result, errors = [], []
        check = work.check_assignee

        def selected_candidate(*args, **kwargs):
            selected.set()
            return check(*args, **kwargs)

        def take_concurrently():
            connection = None
            try:
                connection = db_mod.connect(self.cfg)
                with patch.object(work, "check_assignee", side_effect=selected_candidate):
                    result.extend(backlog.auto_take(
                        self.cfg, connection, now=self.now, paid=("deepseek",),
                        budget_check=lambda _: "", orchestras=[]))
            except Exception as exc:
                errors.append(exc)
            finally:
                if connection is not None:
                    connection.close()

        worker = threading.Thread(target=take_concurrently, daemon=True)
        # La sélection voit encore le lot prêt (arête non commitée). La
        # transaction de prise doit attendre l'édition, puis lire son résultat.
        try:
            with self.db.transaction() as tx:
                storage.of(tx).work_dependencies.add(bloque["id"], amont["id"], actor="test")
                worker.start()
                self.assertTrue(selected.wait(15), "la sélection concurrente n'a pas abouti")
        finally:
            if worker.ident is not None:
                worker.join(15)
        self.assertFalse(worker.is_alive(), "la prise concurrente n'a pas terminé")
        self.assertEqual(errors, [])
        self.assertEqual([t["item"]["id"] for t in result], [pret["id"]])
        self.assertIsNone(work.get(self.db, bloque["id"])["assignee"])

    def test_auditeur_ne_prend_que_les_bugs_hors_instructions_protegees(self):
        self.agent("auditeur", team="ameesh")
        evolution = self.lot("Évolution de la surveillance", app="ameesh", type="evolution")
        protected = self.lot("Bug de docs/AUDITEUR.md", app="ameesh", type="bug")
        bug = self.lot("Crash du runner", app="ameesh", type="bug")
        out = self.take(orchestras=[])
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("auditeur", bug["id"])])
        self.assertIsNone(work.get(self.db, evolution["id"])["assignee"])
        self.assertIsNone(work.get(self.db, protected["id"])["assignee"])

    def test_lot_de_l_equipe_pris_et_orchestrateur_prevenu(self):
        self.agent("orchestrateur", team="nexlink", status="running")
        self.agent("deepseek7", "deepseek", team="nexlink")
        self.agent("claude1", team="nexlink")
        self.agent("dev1", team="ameesh")
        lot = self.lot("corriger l'export")
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"], t["kind"]) for t in out],
                         [("claude1", lot["id"], "lot")])
        self.assertEqual(out[0]["notified"], ["orchestrateur"])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "claude1")
        mails = self.courriers()
        self.assertEqual([(m["recipient"], m["kind"], m["work_item_id"]) for m in mails],
                         [("claude1", "event", str(lot["id"])),
                          ("orchestrateur", "event", str(lot["id"]))])
        self.assertIn("any irreversible or production action", mails[0]["body"])
        self.assertIn("ameesh work assign %d" % lot["id"], mails[1]["body"])
        payload = mails[1]["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        self.assertEqual(payload["lot_take"]["team"], "nexlink")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any(n.startswith(backlog.LOT_TAKE_NOTE) for n in notes))
        # la prise d'un lot ne mange pas le débit de la file d'amélioration
        st = storage.of(self.db).work
        self.assertEqual(st.auto_takes_since(3600, backlog.AUTO_TAKE_NOTE), 0)
        self.assertEqual(st.auto_takes_since(3600, backlog.LOT_TAKE_NOTE), 1)
        self.assertEqual(self.take(), [])

    def test_forfait_au_seuil_laisse_le_lot_a_l_agent_au_token(self):
        # #754 : quand le seul agent au forfait de l'équipe est en pause
        # budget, l'agent au token de la même équipe prend le lot (owner,
        # 13/10 : « quand les forfaits Claude sont au seuil, le travail passe
        # à DeepSeek et Codex »).
        self.agent("claude1", team="nexlink", status="blocked")
        self.db.execute("UPDATE agent_registry SET status_text = 'budget : plafond'"
                        " WHERE name = 'claude1'")
        self.agent("deepseek7", "deepseek", team="nexlink")
        self.agent("dev1", team="ameesh")
        lot = self.lot("corriger l'export")
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"], t["kind"]) for t in out],
                         [("deepseek7", lot["id"], "lot")])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "deepseek7")

    def test_forfait_libre_de_l_equipe_garde_le_lot(self):
        # #754 : un forfait libre de l'équipe passe toujours avant un agent
        # au token — le tri de `candidates` le garantit.
        self.agent("claude1", team="nexlink")
        self.agent("deepseek7", "deepseek", team="nexlink")
        lot = self.lot("corriger l'export")
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("claude1", lot["id"])])

    def test_forfait_qui_ne_peut_pas_prendre_ne_bloque_pas_le_token(self):
        # #754 r1 (mesh-design) : un forfait libre de l'équipe qui ne peut pas
        # prendre un lot (`lot_fits` le refuse, ou il vient d'en prendre un)
        # ne prive plus les agents au token de ce lot : les deux partent dans
        # le même passage. Sans le tri, `paid_allowed` rendait [] ici.
        self.agent("claude1", team="nexlink")
        self.agent("deepseek7", "deepseek", team="nexlink")
        pour_deepseek = self.lot("exige deepseek")
        self.db.execute("UPDATE work_items SET required_capabilities ="
                        " '{harness:deepseek}' WHERE id = %s", (pour_deepseek["id"],))
        ordinaire = self.lot("ordinaire")
        out = self.take()
        self.assertEqual(sorted((t["agent"], t["item"]["id"]) for t in out),
                         sorted([("claude1", ordinaire["id"]),
                                 ("deepseek7", pour_deepseek["id"])]))
        self.assertEqual(work.get(self.db, ordinaire["id"])["assignee"], "claude1")
        self.assertEqual(work.get(self.db, pour_deepseek["id"])["assignee"], "deepseek7")

    def test_seuil_de_repos_propre_aux_lots(self):
        self.agent("claude1", team="nexlink", since_s=20 * 60)
        lot = self.lot("lot")
        self.assertEqual(self.take(lot_idle_s=30 * 60), [])
        self.assertEqual(self.take(lot_idle_s=0), [])
        self.assertEqual(self.take(lot_max_per_hour=0), [])
        # un lot tout juste créé est laissé à qui l'a créé
        self.db.execute("UPDATE work_items SET created_at = now() WHERE id = %s",
                        (lot["id"],))
        self.assertEqual(self.take(), [])
        self.db.execute("UPDATE work_items SET created_at = now() - interval '10 minutes'"
                        " WHERE id = %s", (lot["id"],))
        # défaut : 15 min de repos suffisent, la file en demande 30
        self.assertEqual([t["item"]["id"] for t in self.take()], [lot["id"]])

    def test_ordre_priorite_puis_dates_prevues(self):
        self.agent("a1", team="nexlink", since_s=3 * HEURE)
        self.agent("a2", team="nexlink", since_s=2 * HEURE)
        self.agent("a3", team="nexlink", since_s=HEURE)
        tard = self.lot("prévu tard", priority=2)
        tot = self.lot("prévu tôt", priority=2)
        sans_date = self.lot("sans date", priority=2)
        urgent = self.lot("urgent", priority=1)
        self.lot("basse", priority=3)
        # dates passées : un début prévu après aujourd'hui attend (test suivant)
        self.db.execute("UPDATE work_items SET planned_start = '2020-01-20' WHERE id = %s",
                        (tard["id"],))
        self.db.execute("UPDATE work_items SET planned_start = '2020-01-12' WHERE id = %s",
                        (tot["id"],))
        self.assertEqual(
            [i["id"] for i in storage.of(self.db).work.unassigned_lots(10)][:4],
            [urgent["id"], tot["id"], tard["id"], sans_date["id"]])
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("a1", urgent["id"]), ("a2", tot["id"]), ("a3", tard["id"])])

    def test_debut_prevu_apres_aujourd_hui(self):
        self.agent("claude1", team="nexlink")
        demain = self.lot("prévu demain", priority=1)
        aujourd_hui = self.lot("prévu aujourd'hui", priority=2)
        self.db.execute("UPDATE work_items SET planned_start = %s WHERE id = %s",
                        (time.strftime("%Y-%m-%d", time.localtime(self.now + 86400)),
                         demain["id"]))
        self.db.execute("UPDATE work_items SET planned_start = %s WHERE id = %s",
                        (time.strftime("%Y-%m-%d", time.localtime(self.now)),
                         aujourd_hui["id"]))
        out = self.take()
        self.assertEqual([t["item"]["id"] for t in out], [aujourd_hui["id"]])
        self.assertIsNone(work.get(self.db, demain["id"])["assignee"])
        # le lendemain, il est pris
        self.agent("claude2", team="nexlink")
        out = self.take(now=self.now + 86400)
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("claude2", demain["id"])])
        self.assertFalse(backlog.lot_takeable(
            {"state": "intake", "lot_team": "web", "planned_start": "2026-10-12"},
            "2026-10-11"))
        self.assertTrue(backlog.lot_takeable(
            {"state": "intake", "lot_team": "web", "planned_start": "2026-10-11"},
            "2026-10-11"))

    def test_equipe_du_lot_fiche_puis_application(self):
        self.agent("web1", team="web")
        self.agent("api1", team="api")
        self.db.execute("INSERT INTO work_packages (id, kind, title, team, canon_ref)"
                        " VALUES ('epic-web', 'epic', 'Web', 'web', 'c:p@1'),"
                        " ('lot-web', 'lot', 'Lot', NULL, 'c:p@1')")
        par_fiche = self.lot("par la fiche parente", app="api")
        self.db.execute("UPDATE work_items SET package_id = 'lot-web',"
                        " package_parent = 'epic-web' WHERE id = %s", (par_fiche["id"],))
        sans_equipe = self.lot("sans équipe", app="")
        par_app = self.lot("par l'application", app="api")
        rows = {i["id"]: i for i in storage.of(self.db).work.unassigned_lots(10)}
        self.assertEqual(rows[par_fiche["id"]]["lot_team"], "web")
        self.assertEqual(rows[par_app["id"]]["lot_team"], "api")
        self.assertIsNone(rows[sans_equipe["id"]]["lot_team"])
        out = self.take()
        self.assertEqual(sorted((t["agent"], t["item"]["id"]) for t in out),
                         [("api1", par_app["id"]), ("web1", par_fiche["id"])])
        self.assertIsNone(work.get(self.db, sans_equipe["id"])["assignee"])

    def test_capacites_et_harnais(self):
        self.agent("claude1", team="nexlink", capabilities=["read"])
        self.agent("codex1", "codex", team="nexlink", capabilities=["read"],
                   since_s=HEURE)
        cap = self.lot("exige propose")
        harnais = self.lot("exige codex")
        self.db.execute("UPDATE work_items SET required_capabilities = '{propose}'"
                        " WHERE id = %s", (cap["id"],))
        self.db.execute("UPDATE work_items SET required_capabilities = '{harness:codex}'"
                        " WHERE id = %s", (harnais["id"],))
        out = self.take(paid=())
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("codex1", harnais["id"])])
        self.assertIsNone(work.get(self.db, cap["id"])["assignee"])

    def test_jamais_decision_production_ni_lot_confie(self):
        self.agent("claude1", team="nexlink")
        decision = self.lot("demande de décision en attente")
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, kind, payload, work_item_id)"
            " VALUES ('claude2', 'human:proprio', 'A ou B ?', 'request',"
            " '{\"decision\": {\"options\": [\"A\", \"B\"]}}'::jsonb, %s)",
            (str(decision["id"]),))
        jalon = self.lot("jalon de décision")
        self.db.execute("INSERT INTO commitments (what, kind, work_item_id)"
                        " VALUES ('trancher', 'decision', %s)", (jalon["id"],))
        for name in ("decision", "production"):
            reserve = self.lot("réservé %s" % name)
            self.db.execute("UPDATE work_items SET required_capabilities = %s::text[]"
                            " WHERE id = %s", ("{%s}" % name, reserve["id"]))
        confie = self.lot("déjà confié par courrier")
        work.move(self.db, confie["id"], "build")
        humain = self.lot("attend un humain")
        hold_for_owner(self.db, humain["id"])
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now()"
                        " WHERE recipient = 'claude1'")
        self.assertEqual(self.take(), [])
        self.assertEqual(self.db.query("SELECT id FROM work_items WHERE assignee IS NOT NULL"),
                         [])

    def test_orchestrateur_ne_se_sert_pas_et_forfait_d_abord(self):
        self.agent("orchestrateur", team="nexlink", since_s=5 * HEURE)
        self.agent("deepseek7", "deepseek", team="nexlink", since_s=5 * HEURE)
        self.agent("claude1", team="nexlink", since_s=HEURE)
        un = self.lot("un")
        deux = self.lot("deux")
        out = self.take()
        # #754 : le forfait prend le premier lot, le token le reste, dans le
        # même passage ; l'orchestrateur, lui, ne se sert jamais.
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("claude1", un["id"]), ("deepseek7", deux["id"])])

    def test_orchestrateur_sert_un_lot_qu_aucun_autre_ne_prend(self):
        """#1739 : sans autre preneur, l'orchestrateur prend le lot laissé, et
        la note d'attribution garde la trace du repli."""
        self.agent("orchestrateur", team="nexlink", since_s=5 * HEURE)
        lot = self.lot("demande")
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"], t.get("fallback")) for t in out],
                         [("orchestrateur", lot["id"], True)])
        note = self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id DESC LIMIT 1", (lot["id"],))[0]["note"]
        self.assertIn(backlog.LOT_TAKE_NOTE, note)
        self.assertIn("aucun autre preneur ; orchestrateur", note)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "orchestrateur")

    def test_lot_avant_la_file_et_garde_fous(self):
        self.agent("claude1", team="nexlink")
        element = self.element("dette", 90, team="nexlink")
        lot = self.lot("demande")
        self.assertEqual(self.take(budget_check=lambda a: "plafond"), [])
        self.assertEqual(self.take(alerts=[{"type": "host_pressure", "host": self.cfg.host}]),
                         [])
        out = self.take()
        self.assertEqual([(t["kind"], t["item"]["id"]) for t in out], [("lot", lot["id"])])
        self.assertIsNone(work.get(self.db, element["id"])["assignee"])

    def test_a_blanc(self):
        self.agent("claude1", team="nexlink")
        lot = self.lot("demande")
        out = self.take(dry_run=True)
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out], [("claude1", lot["id"])])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        self.assertEqual(self.courriers(), [])


class NotifyTest(_Lots):
    def test_passage_de_notify_et_reglages(self):
        self.agent("claude1", team="nexlink")
        lot = self.lot("demande")
        records = []
        notifier = notify.Notifier(self.cfg, notify.parse_config({}), log=lambda t: None,
                                   emit=records.append, clock=lambda: self.now,
                                   take={"idle_s": 0.0, "max_per_hour": 0,
                                         "lot_idle_s": 900.0, "lot_max_per_hour": 6,
                                         "orchestras": []})
        notifier.run_pass(self.db, thresholds=_seuils())
        prises = [r for r in records if r["event"] == "auto_take"]
        self.assertEqual([(r["agent"], r["lot"], r["source"]) for r in prises],
                         [("claude1", lot["id"], "lot de l'équipe")])
        parser = notify.build_parser()
        self.assertEqual(notify.take_settings(parser.parse_args(["--take-idle", "0"])),
                         {"idle_s": 0.0, "max_per_hour": 0,
                          "lot_idle_s": backlog.DEFAULT_LOT_IDLE_S,
                          "lot_max_per_hour": backlog.DEFAULT_LOT_MAX_PER_HOUR})
        self.assertEqual(notify.take_settings(parser.parse_args(["--take-lot-idle", "0"]))
                         ["lot_max_per_hour"], 0)
        # #754 : l'ancien `--take-paid` reste accepté, sans effet : la prise
        # par un agent au token est acquise après les forfaits.
        self.assertEqual(notify.take_settings(parser.parse_args(["--take-idle", "60"])),
                         notify.take_settings(parser.parse_args(["--take-idle", "60",
                                                                 "--take-paid"])))


if __name__ == "__main__":
    unittest.main()
