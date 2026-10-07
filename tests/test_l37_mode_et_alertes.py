# SPDX-License-Identifier: AGPL-3.0-only
"""L37 (décision 0030) : mode d'agent, raison d'arrêt, attribution gardée,
alertes de vivacité (`stopped_with_mail`, `orphan_lot`)."""
from __future__ import annotations

import dataclasses
import json
import unittest

from ameesh import exploitation, mail, registry, stagnation, storage, work

from .support import PgTestCase


class LabelExterneTest(unittest.TestCase):
    def test_session_externe_avec_ou_sans_responsable(self):
        detail = stagnation._waiting("build", "coord")
        out = stagnation.with_availability(detail, {"coord": "idle"},
                                           {"coord": "human:proprio"})
        self.assertEqual(out["label"], "travail de coord (session externe, human:proprio)")
        self.assertEqual((out["who_state"], out["responsible"]), ("externe", "human:proprio"))
        out = stagnation.with_availability(detail, {"coord": "idle"}, {"coord": None})
        self.assertEqual(out["label"], "travail de coord (session externe, sans responsable)")
        # sans table des externes : comportement de L36 inchangé
        self.assertIs(stagnation.with_availability(detail, {"coord": "idle"}), detail)


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("ouvrier", "claude", cwd=self.tmp)

    def _row(self, name):
        return registry.get(self.db, name)

    def _externe(self, name, responsible=None):
        registry.upsert(self.db, name, harness="codex", mode="externe")
        if responsible:
            self.db.execute("UPDATE agent_registry SET responsible = %s WHERE name = %s",
                            (responsible, name))


class MigrationEtRaisonTest(_Base):
    def test_colonnes_et_contraintes(self):
        row = self._row("ouvrier")
        self.assertEqual((row["mode"], row["stop_reason"]), ("execute", None))
        with self.assertRaises(Exception):
            self.db.execute("UPDATE agent_registry SET mode = 'autre' WHERE name = 'ouvrier'")
        with self.assertRaises(Exception):
            self.db.execute("UPDATE agent_registry SET status = 'stopped', "
                            "stop_reason = 'caprice' WHERE name = 'ouvrier'")
        with self.assertRaises(ValueError):
            registry.set_status(self.db, "ouvrier", "stopped", stop_reason="caprice")
        # la vue d'observabilité porte les nouvelles colonnes
        vue = {r["name"]: r for r in registry.overview(self.db)}
        self.assertEqual(vue["ouvrier"]["mode"], "execute")

    def test_arret_manuel_puis_reprise_efface_la_raison(self):
        proc = self.runner("stop", "ouvrier")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._row("ouvrier")["stop_reason"], "manuel")
        # repart : `register --prompt` passe en queued, la raison s'efface
        proc = self.register("ouvrier", "claude", prompt="reprends")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self._row("ouvrier")
        self.assertEqual((row["status"], row["stop_reason"]), ("queued", None))

    def test_bail_expire_puis_reclamation(self):
        registry.claim(self.db, "ouvrier", "r1", 3600)
        self.db.execute("UPDATE agent_registry SET status = 'running', "
                        "lease_expires_at = now() - interval '1 minute' WHERE name = 'ouvrier'")
        self.assertEqual([r["name"] for r in registry.reap(self.db)], ["ouvrier"])
        row = self._row("ouvrier")
        self.assertEqual((row["status"], row["stop_reason"]), ("dead", "bail_expire"))
        self.assertTrue(registry.claim(self.db, "ouvrier", "r2", 60))
        row = self._row("ouvrier")
        self.assertEqual((row["status"], row["stop_reason"]), ("idle", None))

    def test_retire_du_canon_et_arret_differe(self):
        self.db.execute("UPDATE agent_registry SET canon_ref = 'agents/ouvrier.md' "
                        "WHERE name = 'ouvrier'")
        canon = storage.of(self.db).canon
        stopped = canon.stop_removed("ouvrier", self.cfg.host,
                                     pending_text="arrêt demandé", stop_text="retiré du canon")
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(self._row("ouvrier")["stop_reason"], "retire_du_canon")
        self.assertTrue(canon.revive("ouvrier", "réintégré", "retiré du canon"))
        self.assertIsNone(self._row("ouvrier")["stop_reason"])
        # tour vivant : arrêt seulement demandé, aucune raison posée
        # (claim refusé : agent du canon sans canon synchronisé — bail posé à la main)
        self.db.execute("UPDATE agent_registry SET status = 'running', lease_owner = 'r1', "
                        "lease_expires_at = now() + interval '1 hour' WHERE name = 'ouvrier'")
        stopped = canon.stop_removed("ouvrier", self.cfg.host,
                                     pending_text="arrêt demandé", stop_text="retiré du canon")
        self.assertEqual(stopped["status"], "running")
        self.assertIsNone(self._row("ouvrier")["stop_reason"])


class ModeTest(_Base):
    def test_hook_sans_bail_cree_un_agent_externe(self):
        hook = self.cli("hook", "codex", env=self.env(AGENT_MAIL_NAME="coord"),
                        stdin=json.dumps({"hook_event_name": "SessionStart",
                                          "session_id": "s-1", "cwd": self.tmp}))
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(self._row("coord")["mode"], "externe")

    def test_hook_sans_bail_ne_bascule_pas_un_agent_existant(self):
        hook = self.cli("hook", "claude", env=self.env(AGENT_MAIL_NAME="ouvrier"),
                        stdin=json.dumps({"hook_event_name": "SessionStart",
                                          "session_id": "s-2", "cwd": self.tmp}))
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(self._row("ouvrier")["mode"], "execute")

    def test_set_mode_list_et_show(self):
        proc = self.mesh("set", "ouvrier", "mode=externe")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("mode=externe", proc.stdout)
        self.assertIn("sans responsable humain", proc.stderr)
        self.assertEqual(self._row("ouvrier")["mode"], "externe")
        proc = self.mesh("show", "ouvrier")
        self.assertIn("externe (session humaine, non réveillable)", proc.stdout)
        self.assertIn("SANS responsable humain", proc.stdout)
        proc = self.mesh("list")
        ligne = next(l for l in proc.stdout.splitlines() if l.startswith("ouvrier"))
        self.assertIn("ext/", ligne)
        proc = self.mesh("list", "--json")
        self.assertEqual(json.loads(proc.stdout)[0]["mode"], "externe")
        self.assertEqual(self.mesh("set", "ouvrier", "mode=bizarre").returncode, 2)
        proc = self.mesh("set", "ouvrier", "mode=")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._row("ouvrier")["mode"], "execute")
        self.assertIn("execute (mené par un exécuteur", self.mesh("show", "ouvrier").stdout)


class AttributionGardeeTest(_Base):
    def test_wakeable(self):
        self.assertEqual(registry.wakeable(self.db, self.cfg, "ouvrier"), (True, ""))
        ok, why = registry.wakeable(self.db, self.cfg, "fantome")
        self.assertFalse(ok)
        self.assertIn("inconnu", why)
        self._externe("coord", "human:proprio")
        ok, why = registry.wakeable(self.db, self.cfg, "coord")
        self.assertFalse(ok)
        self.assertIn("externe", why)
        # responsable requis (canon configuré) : sans responsable, refusé
        requis = dataclasses.replace(self.cfg, require_responsible=True)
        ok, why = registry.wakeable(self.db, requis, "ouvrier")
        self.assertFalse(ok)
        self.assertIn("responsable", why)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:proprio' "
                        "WHERE name = 'ouvrier'")
        self.assertTrue(registry.wakeable(self.db, requis, "ouvrier")[0])
        # agent du canon sans canon valide ni placement admis : non admis
        self.db.execute("UPDATE agent_registry SET canon_ref = 'agents/ouvrier.md' "
                        "WHERE name = 'ouvrier'")
        ok, why = registry.wakeable(self.db, requis, "ouvrier")
        self.assertFalse(ok)
        self.assertIn("pas admis", why)

    def test_work_add_refuse_accepte_et_externe(self):
        proc = self.mesh("work", "add", "--title", "a", "--assignee", "fantome")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("agent inconnu", proc.stderr)
        self._externe("coord", "human:proprio")
        self._externe("orphelin")
        proc = self.mesh("work", "add", "--title", "b", "--assignee", "coord")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--externe", proc.stderr)
        proc = self.mesh("work", "add", "--title", "c", "--assignee", "orphelin", "--externe")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("pas de responsable", proc.stderr)
        proc = self.mesh("work", "add", "--title", "d", "--assignee", "ouvrier", "--externe")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(work.list_items(self.db), [])
        proc = self.mesh("work", "add", "--title", "e", "--assignee", "ouvrier")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "add", "--title", "f", "--assignee", "coord", "--externe")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("session externe", proc.stdout)
        proc = self.mesh("work", "add", "--title", "g", "--assignee", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = {r["title"]: r for r in json.loads(self.mesh("work", "list", "--json").stdout)}
        self.assertEqual(set(rows), {"e", "f", "g"})
        self.assertEqual(rows["f"]["waiting_for"]["label"],
                         "démarrage par coord (session externe, human:proprio)")

    def test_work_assign(self):
        lot = work.add(self.db, title="à confier", assignee="ouvrier")
        proc = self.mesh("work", "assign", str(lot["id"]), "fantome")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("agent inconnu", proc.stderr)
        self.register("second", "codex", cwd=self.tmp)
        proc = self.mesh("work", "assign", str(lot["id"]), "second", "--actor", "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "second")
        events = work.events(self.db, lot["id"])
        self.assertTrue(any("assigné à second (avant : ouvrier)" in e["note"]
                            and e["actor"] == "orch" for e in events))
        self.assertEqual(self.mesh("work", "assign", str(lot["id"]), "second").returncode, 1)
        self._externe("coord", "human:proprio")
        self.assertEqual(self.mesh("work", "assign", str(lot["id"]), "coord").returncode, 1)
        proc = self.mesh("work", "assign", str(lot["id"]), "coord", "--externe")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(any("session externe, responsable human:proprio" in e["note"]
                            for e in work.events(self.db, lot["id"])))
        # lot fermé : plus de réassignation
        work.close(self.db, lot["id"], abandoned=True)
        proc = self.mesh("work", "assign", str(lot["id"]), "ouvrier")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("rien à réassigner", proc.stderr)


class AlertesVivaciteTest(_Base):
    def _courrier_ancien(self, dest):
        mail.send(self.db, "orch", dest, "à lire")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '10 minutes'")

    def _types(self, **kw):
        return [a for a in exploitation.alerts(self.cfg, self.db, **kw)
                if a["type"] in ("stopped_with_mail", "orphan_lot")]

    def test_stopped_with_mail_sauf_arret_manuel(self):
        self._courrier_ancien("ouvrier")
        registry.set_status(self.db, "ouvrier", "stopped", status_text="arrêté à la main",
                            stop_reason="manuel")
        self.assertEqual(self._types(), [])
        registry.set_status(self.db, "ouvrier", "dead", status_text="bail expiré",
                            stop_reason="bail_expire")
        alertes = self._types()
        self.assertEqual(len(alertes), 1)
        alerte = alertes[0]
        self.assertEqual((alerte["type"], alerte["agent"], alerte["value"]),
                         ("stopped_with_mail", "ouvrier", 1))
        self.assertEqual((alerte["stop_reason"], alerte["mode"]), ("bail_expire", "execute"))
        # seuil idle_mail respecté
        self.assertEqual(self._types(idle_mail_s=3600), [])
        # courrier lu : résolue
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now()")
        self.assertEqual(self._types(), [])

    def test_orphan_lot_raisons(self):
        lot_inconnu = work.add(self.db, title="inconnu", assignee="ouvrier")
        self.register("dort", "claude", cwd=self.tmp)
        lot_arrete = work.add(self.db, title="arrêté", assignee="dort")
        self._externe("coord")
        self.db.execute("INSERT INTO work_items (title, state, assignee) "
                        "VALUES ('externe', 'intake', 'coord')")
        attente = work.add(self.db, title="attend un humain", assignee="dort")
        work.move(self.db, attente["id"], "waiting_human")
        humain = work.add(self.db, title="humain", assignee="human:proprio")
        self.db.execute("DELETE FROM agent_registry WHERE name = 'ouvrier'")
        registry.set_status(self.db, "dort", "stopped", status_text="arrêté à la main",
                            stop_reason="manuel")
        alertes = {a["title"]: a for a in self._types()}
        self.assertEqual(set(alertes), {"inconnu", "arrêté", "externe"})
        self.assertEqual(alertes["inconnu"]["reason"], "inconnu")
        self.assertEqual(alertes["inconnu"]["lot"], lot_inconnu["id"])
        self.assertEqual(alertes["arrêté"]["reason"], "arrete")
        self.assertEqual(alertes["arrêté"]["lot"], lot_arrete["id"])
        self.assertEqual(alertes["externe"]["reason"], "externe_sans_responsable")
        self.assertNotIn(humain["id"], [a["lot"] for a in alertes.values()])
        for alerte in alertes.values():
            self.assertTrue({"lot", "title", "assignee", "reason", "responsible"} <= set(alerte))

    def test_orphan_lot_sans_tour_et_responsable(self):
        self.db.execute("UPDATE agent_registry SET responsible = 'human:proprio' "
                        "WHERE name = 'ouvrier'")
        lot = work.add(self.db, title="oublié", assignee="ouvrier")
        self.assertEqual(self._types(), [])
        for sql in ("UPDATE work_items SET created_at = now() - interval '2 hours',"
                    " updated_at = now() - interval '2 hours'",
                    "UPDATE work_item_events SET created_at = now() - interval '2 hours'",
                    "UPDATE work_item_milestones SET at = now() - interval '2 hours'"):
            self.db.execute(sql)
        alertes = self._types()
        self.assertEqual(len(alertes), 1)
        self.assertEqual((alertes[0]["reason"], alertes[0]["lot"], alertes[0]["responsible"]),
                         ("sans_tour", lot["id"], "human:proprio"))
        self.assertEqual(self._types(orphan_lot_s=3 * 3600), [])
        # un tour récent de l'assigné : plus orphelin
        self.db.execute("UPDATE agent_registry SET last_turn_at = now() WHERE name = 'ouvrier'")
        self.assertEqual(self._types(), [])
        # en qa, on n'attend pas un tour de l'assigné
        self.db.execute("UPDATE agent_registry SET last_turn_at = now() - interval '3 hours'")
        self.assertEqual(len(self._types()), 1)
        self.db.execute("UPDATE work_items SET state = 'qa', updated_at = now() - "
                        "interval '2 hours'")
        self.assertEqual(self._types(), [])

    def test_cli_orphan_lot_et_follow(self):
        lot = work.add(self.db, title="inconnu", assignee="ouvrier")
        self.db.execute("DELETE FROM agent_registry WHERE name = 'ouvrier'")
        proc = self.mesh("alerts", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lignes = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual([(l["type"], l["lot"], l["reason"]) for l in lignes],
                         [("orphan_lot", lot["id"], "inconnu")])
        avant = {exploitation.alert_key(a) for a in exploitation.alerts(self.cfg, self.db)}
        self.register("ouvrier", "claude", cwd=self.tmp)
        apres = {exploitation.alert_key(a) for a in exploitation.alerts(self.cfg, self.db)}
        self.assertEqual(apres, set())
        self.assertEqual({k[0] for k in avant}, {"orphan_lot"})
        proc = self.mesh("alerts", "--orphan-lot", "60")
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
