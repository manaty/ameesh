# SPDX-License-Identifier: AGPL-3.0-only
"""L119 (décision 0037) : file d'amélioration continue, prise automatique par
les agents au repos, garde-fous, alerte `backlog_empty`."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from ameesh import backlog, exploitation, notify, registry, storage, sous_utilisation, work

from .support import PgTestCase

HEURE = 3600.0


def _seuils(**kw):
    args = exploitation.build_parser().parse_args(["alerts"])
    out = exploitation.thresholds(args)
    out.update(kw)
    return out


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()

    def agent(self, name, harness="claude", *, status="idle", since_s=2 * HEURE, team=None,
              capabilities=None, roles=None, host=None, responsible="human:proprio"):
        registry.upsert(self.db, name, harness=harness, host=host or self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = %s, lease_owner = %s,"
            " lease_expires_at = now() + interval '1 hour', responsible = %s, team = %s"
            " WHERE name = %s",
            (status, "runner-%s@h" % name, responsible, team, name))
        # #4779 : une relecture proposée désigne son relecteur ; la fiche de
        # test doit prouver `propose` et un rôle, comme `canon sync` en
        # production. Un témoin qui veut une autre fiche (canari read-only,
        # absence de rôle) passe capabilities/roles explicitement.
        if capabilities is None:
            capabilities = ("propose",)
        if roles is None:
            roles = ("implementation",)
        self.db.execute("UPDATE agent_registry SET capabilities = %s::text[],"
                        " roles = %s::text[] WHERE name = %s",
                        ("{" + ",".join(capabilities) + "}",
                         "{" + ",".join(roles) + "}", name))
        self.db.execute("UPDATE agent_registry SET status_since = now() - make_interval("
                        "secs => %s), last_turn_at = now() - make_interval(secs => %s)"
                        " WHERE name = %s", (since_s, since_s, name))

    def element(self, title, score, **kw):
        kw.setdefault("expected_value", "gain mesurable pour %s" % title)
        return backlog.add(self.db, title=title, value_score=score, **kw)

    def take(self, **kw):
        kw.setdefault("now", self.now)
        kw.setdefault("paid", ("deepseek",))
        kw.setdefault("budget_check", lambda agent: "")
        return backlog.auto_take(self.cfg, self.db, **kw)


class FileTest(_Base):
    def test_ajout_ordre_et_refus(self):
        bas = self.element("doc", 20)
        haut = self.element("dette", 80, source="constat de l'auditeur")
        urgent = self.element("alerte récurrente", 10, priority=1)
        self.assertEqual(bas["type"], "improvement")
        self.assertEqual(bas["state"], "intake")
        self.assertIsNone(bas["assignee"])
        self.assertEqual([i["id"] for i in backlog.items(self.db)],
                         [urgent["id"], haut["id"], bas["id"]])
        self.assertEqual(haut["source"], "constat de l'auditeur")
        # pas de travail pour occuper : la valeur attendue est obligatoire
        with self.assertRaises(backlog.BacklogError):
            backlog.add(self.db, title="occuper", expected_value="  ", value_score=50)
        with self.assertRaises(backlog.BacklogError):
            self.element("hors bornes", 0)
        with self.assertRaises(backlog.BacklogError):
            self.element("priorité", 50, priority=4)
        # la base refuse aussi un élément sans valeur
        with self.assertRaises(Exception):
            self.db.execute("INSERT INTO work_items (type, title) VALUES ('improvement', 'x')")
        # un élément pris sort de la file ouverte, pas de --all
        work.assign(self.db, bas["id"], "human:proprio")
        self.assertNotIn(bas["id"], [i["id"] for i in backlog.items(self.db)])
        self.assertIn(bas["id"], [i["id"] for i in backlog.items(self.db, open_only=False)])

    def test_en_file_n_est_pas_stagnant(self):
        item = self.element("dette", 50)
        self.db.execute("UPDATE work_items SET updated_at = now() - interval '2 days'")
        self.db.execute("UPDATE work_item_events SET created_at = now() - interval '2 days'")
        stale = [a for a in exploitation.alerts(self.cfg, self.db, **_seuils())
                 if a["type"] == "stale_lot"]
        self.assertNotIn(item["id"], [a.get("lot") for a in stale])

    def test_capacites_et_equipe(self):
        item = {"team": "web", "required_capabilities": ["propose"]}
        self.assertTrue(backlog.fits(item, {"team": "web", "capabilities": ["read", "propose"]}))
        self.assertFalse(backlog.fits(item, {"team": "web", "capabilities": ["read"]}))
        self.assertFalse(backlog.fits(item, {"team": "api", "capabilities": ["propose"]}))
        self.assertTrue(backlog.fits({}, {}))


class PriseTest(_Base):
    def test_lot_du_projet_bloque_laisse_prendre_l_amelioration(self):
        self.agent("claude1")
        amont = work.add(self.db, title="amont", assignee="human:proprio")
        lot = work.add(self.db, title="projet bloque")
        item = self.element("amelioration prete", 50)
        storage.of(self.db).work_dependencies.add(lot["id"], amont["id"], actor="test")

        self.assertEqual([t["item"]["id"] for t in self.take(dry_run=True)], [item["id"]])
        self.assertEqual([t["item"]["id"] for t in self.take()], [item["id"]])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

        # Un projet dont l'amont est satisfait reprend la priorite sur la file.
        work.close(self.db, amont["id"], abandoned=True)
        self.agent("claude2")
        self.element("autre amelioration", 50)
        self.assertEqual(self.take(), [])

    def _planifie(self, lot, jours=3):
        self.db.execute("UPDATE work_items SET planned_start = current_date + %s"
                        " WHERE id = %s", (int(jours), lot["id"]))

    def test_lot_planifie_plus_tard_laisse_prendre_l_amelioration(self):
        # #1625 : seul un lot dont le début prévu est à venir attend — il
        # attend sa date, pas un agent : l'amélioration prête est prise.
        self.agent("claude1")
        lot = work.add(self.db, title="projet planifie")
        self._planifie(lot)
        item = self.element("amelioration prete", 50)

        self.assertEqual([t["item"]["id"] for t in self.take()], [item["id"]])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_amelioration_planifiee_plus_tard_n_est_pas_prise(self):
        # #3296 : la lecture prête ignore planned_start — un élément à début
        # futur ne doit ni être proposé ni être pris ; le témoin positif (sans
        # date) reste prenable. Dates relatives à l'horloge (AGENTS.md j).
        from datetime import date, timedelta
        self.agent("claude1")
        futur = self.element("planifiee", 90)
        self.db.execute(
            "UPDATE work_items SET planned_start = %s WHERE id = %s",
            (date.today() + timedelta(days=1), futur["id"]))
        pret = self.element("prete", 50)

        self.assertEqual(
            [i["id"] for i in storage.of(self.db).work.backlog(
                open_only=True, limit=10, ready_only=True)], [pret["id"]])
        self.assertEqual([t["item"]["id"] for t in self.take()], [pret["id"]])
        self.assertIsNone(work.get(self.db, futur["id"])["assignee"])

    def test_amelioration_planifiee_date_passee_reste_prise(self):
        # #3296 : un début prévu passé ne retient pas la prise.
        from datetime import date, timedelta
        self.agent("claude1")
        passe = self.element("date passee", 70)
        self.db.execute(
            "UPDATE work_items SET planned_start = %s WHERE id = %s",
            (date.today() - timedelta(days=1), passe["id"]))

        self.assertEqual(
            [i["id"] for i in storage.of(self.db).work.backlog(
                open_only=True, limit=10, ready_only=True)], [passe["id"]])
        self.assertEqual([t["item"]["id"] for t in self.take()], [passe["id"]])

    def test_lot_pret_a_cote_d_un_lot_planifie_garde_la_priorite(self):
        # #1625 : mixte — un lot démarrable suffit à fermer la porte (0037 §3).
        self.agent("claude1")
        plus_tard = work.add(self.db, title="projet planifie")
        self._planifie(plus_tard)
        pret = work.add(self.db, title="demande humaine prete")
        item = self.element("amelioration", 50)

        self.assertEqual([t for t in self.take() if t["kind"] == "backlog"], [])
        self.assertIsNone(work.get(self.db, item["id"])["assignee"])
        work.close(self.db, pret["id"], abandoned=True)
        self.assertEqual([t["item"]["id"] for t in self.take()], [item["id"]])

    def test_attentes_illisibles_ferment_la_porte(self):
        # #1625 : une lecture des attentes en échec ne devine pas — le lot
        # reste compté et aucune amélioration n'est prise.
        self.agent("claude1")
        lot = work.add(self.db, title="projet planifie")
        self._planifie(lot)
        self.element("amelioration", 50)
        from ameesh import planned_waits
        with patch.object(planned_waits, "read", side_effect=RuntimeError("illisible")):
            self.assertEqual(self.take(), [])
            self.assertEqual(
                [l["id"] for l in sous_utilisation.waiting_project_lots(
                    storage.of(self.db).operations.open_lots_activity(500), db=self.db)],
                [lot["id"]])

    def test_capacite_idle_ne_propose_pas_un_lot_du_projet_bloque(self):
        self.agent("claude1")
        amont = work.add(self.db, title="amont", assignee="human:proprio")
        lot = work.add(self.db, title="projet bloque")
        storage.of(self.db).work_dependencies.add(lot["id"], amont["id"], actor="test")
        listing = storage.of(self.db).operations.listing()

        def alertes(**kw):
            return sous_utilisation.idle_capacity(
                self.db, listing, self.now, [], idle_s=30, paid=("deepseek",), **kw)

        # #2337 B1 : un lot qu'une dépendance retient n'est pas du travail à
        # confier — sans lot démarrable, l'alerte ne se lève plus (le détail
        # des bloqués reste couvert par test_l94_underuse). La prise
        # automatique (0037) garde, elle, le filtre `db=db` de #1021.
        self.assertEqual(alertes(), [])
        self.assertEqual(
            alertes(lots=storage.of(self.db).operations.open_lots_activity(500)), [])
        work.close(self.db, amont["id"], abandoned=True)
        self.assertEqual([a["type"] for a in alertes()], ["idle_capacity"])
        self.assertEqual(alertes()[0]["startable_lots"], [lot["id"]])
        self.assertIn("#%d" % lot["id"], alertes()[0]["detail"])

    def test_dependance_ouverte_laisse_prendre_l_amelioration_prete(self):
        self.agent("claude1")
        amont = work.add(self.db, title="amont", assignee="human:proprio")
        bloque = self.element("prioritaire mais bloque", 90)
        pret = self.element("pret", 50)
        storage.of(self.db).work_dependencies.add(bloque["id"], amont["id"], actor="test")

        self.assertEqual([i["id"] for i in storage.of(self.db).work.backlog(
            open_only=True, limit=1, ready_only=True)], [pret["id"]])
        self.assertEqual([t["item"]["id"] for t in self.take()], [pret["id"]])
        self.assertIsNone(work.get(self.db, bloque["id"])["assignee"])
        # La liste de la file continue d'afficher les éléments bloqués.
        self.assertIn(bloque["id"], [i["id"] for i in backlog.items(self.db)])

        work.close(self.db, amont["id"], abandoned=True)
        self.agent("claude2")
        self.assertEqual([t["item"]["id"] for t in self.take()], [bloque["id"]])

    def test_dependance_ajoutee_apres_selection_refuse_l_amelioration(self):
        self.agent("claude1")
        amont = work.add(self.db, title="amont", assignee="human:proprio")
        bloque = self.element("devient bloque", 90)
        pret = self.element("reste pret", 50)
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
        self.assertFalse(self.db.query("SELECT id FROM agent_mailbox WHERE work_item_id = %s",
                                       (str(bloque["id"]),)))
        self.assertFalse(any(e["note"].startswith(backlog.AUTO_TAKE_NOTE)
                             for e in work.events(self.db, bloque["id"])))

    def test_prise_de_plus_forte_valeur_et_courrier(self):
        self.agent("claude1")
        bas = self.element("doc", 20)
        haut = self.element("dette", 80)
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out], [("claude1", haut["id"])])
        lot = work.get(self.db, haut["id"])
        self.assertEqual(lot["assignee"], "claude1")
        self.assertIsNone(work.get(self.db, bas["id"])["assignee"])
        rows = self.db.query("SELECT sender, recipient, work_item_id, body FROM agent_mailbox")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["sender"], rows[0]["recipient"], rows[0]["work_item_id"]),
                         ("ameesh", "claude1", str(haut["id"])))
        self.assertIn("any irreversible or production action", rows[0]["body"])
        notes = [e["note"] for e in work.events(self.db, haut["id"])]
        self.assertTrue(any(n.startswith(backlog.AUTO_TAKE_NOTE) for n in notes))
        # l'agent a maintenant un lot (et du courrier) : il ne prend plus rien
        self.assertEqual(self.take(), [])

    def test_seuil_nul_desactive_la_prise(self):
        # #1677 (b) : le temps depuis le dernier tour ne borne plus la prise
        # (l'agent sans lot actionnable est au repos) ; zéro la désactive.
        self.agent("claude1", since_s=60)
        dette = self.element("dette", 50)
        self.assertEqual(self.take(idle_s=0), [])
        self.assertEqual([t["item"]["id"] for t in self.take()], [dette["id"]])

    def test_debit_par_heure(self):
        for name in ("a1", "a2", "a3"):
            self.agent(name)
        for score in (10, 20, 30):
            self.element("e%d" % score, score)
        out = self.take(max_per_hour=2)
        self.assertEqual(len(out), 2)
        self.agent("a4")
        self.assertEqual(self.take(max_per_hour=2), [])
        self.assertEqual(storage.of(self.db).work.auto_takes_since(3600, backlog.AUTO_TAKE_NOTE), 2)

    def test_forfait_avant_token(self):
        # #754 : le forfait passe d'abord ; ce qu'il laisse part aux agents au
        # token dans le même passage, sans `--take-paid` ni veto d'équipe.
        self.agent("deepseek1", "deepseek", since_s=5 * HEURE)
        self.agent("claude1", since_s=HEURE)
        haut = self.element("dette", 50)
        bas = self.element("doc", 40)
        out = self.take(max_per_hour=5)
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("claude1", haut["id"]), ("deepseek1", bas["id"])])

    def test_prise_au_token_jamais_sous_balance_low(self):
        # #754 : un agent au token reste écarté pendant une alerte
        # `balance_low`, même sans agent au forfait libre.
        self.agent("deepseek1", "deepseek")
        self.element("dette", 50)
        self.assertEqual(self.take(alerts=[{"type": "balance_low"}]), [])

    def test_garde_fous_budget_et_hote(self):
        self.agent("claude1")
        self.element("dette", 50)
        self.assertEqual(self.take(budget_check=lambda a: "plafond atteint"), [])
        self.assertEqual(self.take(alerts=[{"type": "host_pressure", "host": self.cfg.host}]),
                         [])
        # #754 : un agent du mesh en pause budget ne prend rien lui-même,
        # mais il n'empêche plus les autres agents libres de prendre (un
        # forfait au seuil ne doit pas arrêter ceux qui ont du budget).
        self.agent("claude2", status="blocked")
        self.db.execute("UPDATE agent_registry SET status_text = 'budget : plafond'"
                        " WHERE name = 'claude2'")
        self.assertEqual([t["agent"] for t in self.take()], ["claude1"])
        self.db.execute("UPDATE agent_registry SET status_text = '' WHERE name = 'claude2'")
        # claude1 a pris le seul élément : la pause levée n'ouvre pas de prise
        self.assertEqual(self.take(), [])

    def test_prise_au_token_quand_le_forfait_est_au_seuil(self):
        # #754 (owner, 13/10) : « quand les forfaits Claude sont au seuil, le
        # travail passe à DeepSeek » — à défaut d'agent au forfait, l'agent
        # au token de la même équipe prend l'élément.
        self.agent("claude1", status="blocked")
        self.db.execute("UPDATE agent_registry SET status_text = 'budget : plafond'"
                        " WHERE name = 'claude1'")
        self.agent("deepseek1", "deepseek")
        item = self.element("dette", 50)
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("deepseek1", item["id"])])
        # ... mais un forfait libre passe toujours devant
        self.db.execute("UPDATE work_items SET assignee = NULL, state = 'intake',"
                        " updated_at = now() WHERE id = %s", (item["id"],))
        self.agent("claude3")
        self.assertEqual([t["agent"] for t in self.take()], ["claude3"])

    def test_equipe_et_capacites(self):
        self.agent("web1", team="web", capabilities=["read"])
        autre = self.element("api", 90, team="api")
        cap = self.element("propose", 80, required_capabilities=["propose"])
        ok = self.element("web", 10, team="web", required_capabilities=["read"])
        out = self.take()
        self.assertEqual([t["item"]["id"] for t in out], [ok["id"]])
        self.assertIsNone(work.get(self.db, autre["id"])["assignee"])
        self.assertIsNone(work.get(self.db, cap["id"])["assignee"])

    def test_aucun_lot_ne_l_attend(self):
        self.agent("claude1")
        item = self.element("dette", 50)
        # un lot du projet attend un preneur : il passe avant l'amélioration
        lot = work.add(self.db, title="demande humaine")
        self.assertEqual(self.take(), [])
        work.close(self.db, lot["id"], abandoned=True)
        # courrier non lu, consigne en attente, lot de session encore ouvert
        self.db.execute("INSERT INTO agent_mailbox (sender, recipient, body)"
                        " VALUES ('human:proprio', 'claude1', 'à toi')")
        self.assertEqual(self.take(), [])
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now()")
        self.db.execute("UPDATE agent_registry SET pending_prompt = 'reprends'"
                        " WHERE name = 'claude1'")
        self.assertEqual(self.take(), [])
        self.db.execute("UPDATE agent_registry SET pending_prompt = NULL")
        autre = work.add(self.db, title="lot de session", assignee="human:proprio")
        self.db.execute("UPDATE agent_registry SET session_work_item = %s"
                        " WHERE name = 'claude1'", (str(autre["id"]),))
        self.assertEqual(self.take(), [])
        self.db.execute("UPDATE agent_registry SET session_work_item = NULL")
        self.assertEqual([t["item"]["id"] for t in self.take()], [item["id"]])

    def test_file_non_comptee_comme_travail_en_attente(self):
        # un élément de la file n'est pas un « lot sans assigné » d'idle_capacity
        self.agent("claude1", since_s=60)
        self.element("dette", 50)
        idle = [a for a in exploitation.alerts(self.cfg, self.db, now=self.now,
                                               **_seuils(idle_capacity_s=30))
                if a["type"] == "idle_capacity"]
        self.assertEqual(idle, [])

    def test_a_blanc(self):
        self.agent("claude1")
        item = self.element("dette", 50)
        out = self.take(dry_run=True)
        self.assertEqual([t["item"]["id"] for t in out], [item["id"]])
        self.assertIsNone(work.get(self.db, item["id"])["assignee"])

    def test_prise_ready_only_refuse_un_current_non_nul(self):
        """#1696 (N3) : `ready_only` exige `current=None` — erreur d'appel
        avant le verrou, jamais un None silencieux après coup."""
        self.agent("claude1")
        item = self.element("dette", 50)
        st = storage.of(self.db).work
        with self.assertRaises(ValueError):
            st.assign(item["id"], "claude1", current="human:proprio", note="prise",
                      actor="test", ready_only=True)
        self.assertIsNone(work.get(self.db, item["id"])["assignee"])
        st.assign(item["id"], "claude1", current=None, note="prise", actor="test",
                  ready_only=True)
        self.assertEqual(work.get(self.db, item["id"])["assignee"], "claude1")


class AlerteEtNotifyTest(_Base):
    def test_file_vide_avec_agents_au_repos(self):
        self.assertIn("backlog_empty", notify.DEFAULT_TYPES)
        self.assertIn("backlog_empty", exploitation.ALERT_TYPES)
        self.assertIn("backlog_empty", sous_utilisation.UNDERUSE_TYPES)
        self.agent("claude1")

        def alertes(**kw):
            return [a for a in exploitation.alerts(self.cfg, self.db, now=self.now,
                                                   **_seuils(**kw))
                    if a["type"] == "backlog_empty"]

        out = alertes()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["agents"], ["claude1"])
        self.assertEqual(out[0]["responsible"], "human:proprio")
        self.assertIn("ameesh work backlog add", out[0]["detail"])
        self.assertEqual(alertes(backlog_empty_s=0), [])
        # un lot du projet attend : idle_capacity parle, pas backlog_empty
        lot = work.add(self.db, title="demande humaine")
        self.assertEqual(alertes(), [])
        work.close(self.db, lot["id"], abandoned=True)
        # #1625 : un lot que sa date retient n'attend pas un agent — la file
        # vide se signale quand même
        plus_tard = work.add(self.db, title="demande planifiee")
        self.db.execute("UPDATE work_items SET planned_start = current_date + 3"
                        " WHERE id = %s", (plus_tard["id"],))
        self.assertEqual(len(alertes()), 1)
        work.close(self.db, plus_tard["id"], abandoned=True)
        # #1696 (N2) : un élément bloqué n'est pas prenable — l'alerte parle.
        amont = work.add(self.db, title="amont", assignee="human:proprio")
        bloque = self.element("dette bloquée", 50)
        storage.of(self.db).work_dependencies.add(bloque["id"], amont["id"], actor="test")
        self.assertEqual(len(alertes()), 1)
        # Amont satisfait : l'élément devient prenable, l'alerte se tait.
        work.close(self.db, amont["id"], abandoned=True)
        self.assertEqual(alertes(), [])
        self.element("dette", 50)
        self.assertEqual(alertes(), [])

    def test_passage_de_notify(self):
        self.agent("claude1")
        item = self.element("dette", 50)
        records = []
        ncfg = notify.parse_config({})
        notifier = notify.Notifier(self.cfg, ncfg, log=lambda t: None, emit=records.append,
                                   clock=lambda: self.now,
                                   take={"idle_s": 1800.0, "max_per_hour": 2})
        notifier.run_pass(self.db, thresholds=_seuils())
        prises = [r for r in records if r["event"] == "auto_take"]
        self.assertEqual([(r["agent"], r["lot"]) for r in prises], [("claude1", item["id"])])
        self.assertIn("auto-take", notify._format(prises[0]))
        self.assertEqual(work.get(self.db, item["id"])["assignee"], "claude1")
        args = notify.build_parser().parse_args(["--take-idle", "0", "--take-lot-idle", "0"])
        self.assertIsNone(notify.take_settings(args))
        # #1738 R1 (B2) : les propositions suivent une prise active ; leur
        # interrupteur à 0 les coupe en plus, la prise restant active.
        args = notify.build_parser().parse_args(["--take-review-max-per-hour", "0"])
        self.assertEqual(notify.take_settings(args)["review_max_per_hour"], 0)
        self.assertTrue(notify.take_settings(args)["max_per_hour"])


if __name__ == "__main__":
    unittest.main()
