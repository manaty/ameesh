# SPDX-License-Identifier: AGPL-3.0-only
"""L119 (décision 0037) : file d'amélioration continue, prise automatique par
les agents au repos, garde-fous, alerte `backlog_empty`."""
from __future__ import annotations

import time
import unittest

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
              capabilities=None, host=None, responsible="human:proprio"):
        registry.upsert(self.db, name, harness=harness, host=host or self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = %s, lease_owner = %s,"
            " lease_expires_at = now() + interval '1 hour', responsible = %s, team = %s"
            " WHERE name = %s",
            (status, "runner-%s@h" % name, responsible, team, name))
        if capabilities:
            self.db.execute("UPDATE agent_registry SET capabilities = %s::text[]"
                            " WHERE name = %s",
                            ("{" + ",".join(capabilities) + "}", name))
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
        self.assertIn("geste irréversible ou de production", rows[0]["body"])
        notes = [e["note"] for e in work.events(self.db, haut["id"])]
        self.assertTrue(any(n.startswith(backlog.AUTO_TAKE_NOTE) for n in notes))
        # l'agent a maintenant un lot (et du courrier) : il ne prend plus rien
        self.assertEqual(self.take(), [])

    def test_repos_trop_court_et_seuil_nul(self):
        self.agent("claude1", since_s=60)
        self.element("dette", 50)
        self.assertEqual(self.take(), [])
        self.assertEqual(self.take(idle_s=0), [])

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
        self.agent("deepseek1", "deepseek", since_s=5 * HEURE)
        self.agent("claude1", since_s=HEURE)
        self.element("dette", 50)
        self.element("doc", 40)
        out = self.take(max_per_hour=5)
        self.assertEqual([t["agent"] for t in out], ["claude1"])
        out = self.take(max_per_hour=5, allow_paid=True,
                        alerts=[{"type": "balance_low"}])
        self.assertEqual(out, [])
        out = self.take(max_per_hour=5, allow_paid=True)
        self.assertEqual([t["agent"] for t in out], ["deepseek1"])

    def test_garde_fous_budget_et_hote(self):
        self.agent("claude1")
        self.element("dette", 50)
        self.assertEqual(self.take(budget_check=lambda a: "plafond atteint"), [])
        self.assertEqual(self.take(alerts=[{"type": "host_pressure", "host": self.cfg.host}]),
                         [])
        # un agent du mesh en pause budget : aucune prise
        self.agent("claude2", status="blocked")
        self.db.execute("UPDATE agent_registry SET status_text = 'budget : plafond'"
                        " WHERE name = 'claude2'")
        self.assertEqual(self.take(), [])
        self.db.execute("UPDATE agent_registry SET status_text = '' WHERE name = 'claude2'")
        self.assertEqual(len(self.take()), 1)

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
        self.element("dette", 50)
        self.assertEqual(alertes(), [])

    def test_passage_de_notify(self):
        self.agent("claude1")
        item = self.element("dette", 50)
        records = []
        ncfg = notify.parse_config({})
        notifier = notify.Notifier(self.cfg, ncfg, log=lambda t: None, emit=records.append,
                                   clock=lambda: self.now,
                                   take={"idle_s": 1800.0, "max_per_hour": 2,
                                         "allow_paid": False})
        notifier.run_pass(self.db, thresholds=_seuils())
        prises = [r for r in records if r["event"] == "auto_take"]
        self.assertEqual([(r["agent"], r["lot"]) for r in prises], [("claude1", item["id"])])
        self.assertIn("prise automatique", notify._format(prises[0]))
        self.assertEqual(work.get(self.db, item["id"])["assignee"], "claude1")
        args = notify.build_parser().parse_args(["--take-idle", "0"])
        self.assertIsNone(notify.take_settings(args))


if __name__ == "__main__":
    unittest.main()
