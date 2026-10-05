# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh progress` (L24) : jalons de lot, états, budget, rendus texte et HTML.

Tests unitaires (sans base) puis tests sur Postgres réel : lots déplacés par
`work.move`, action `git-merge` liée au lot, tour en cours sous bail, grand
livre `turn_costs`, filtre de projet, CLI `--json` et `--html`.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import unittest

from ameesh import cost, progress, registry, storage, work

from .support import PgTestCase

NOW = 1_790_000_000.0


def ev(state, at, actor=""):
    return {"state": state, "created_ts": at, "actor": actor}


class SinceTest(unittest.TestCase):
    def test_durees(self):
        self.assertEqual(progress.parse_since("16h", NOW), NOW - 16 * 3600)
        self.assertEqual(progress.parse_since("90m", NOW), NOW - 5400)
        self.assertEqual(progress.parse_since("2d", NOW), NOW - 2 * 86400)
        self.assertEqual(progress.parse_since("2j", NOW), NOW - 2 * 86400)
        self.assertEqual(progress.parse_since(None, NOW), NOW - 86400)

    def test_dates_iso(self):
        self.assertEqual(progress.parse_since("2026-10-05T08:00:00Z", NOW),
                         1791187200.0)
        self.assertEqual(progress.parse_since("2026-10-05T10:00:00+02:00", NOW),
                         1791187200.0)
        local = progress.parse_since("2026-10-05", NOW)
        self.assertEqual(time.localtime(local)[:6], (2026, 10, 5, 0, 0, 0))

    def test_illisible(self):
        for bad in ("hier", "12", "3 semaines", "2026-13-01"):
            with self.assertRaises(progress.ProgressError):
                progress.parse_since(bad, NOW)


class JalonsTest(unittest.TestCase):
    """`_jalons_de_lot` : le seul point de lecture des jalons d'un lot."""

    item = {"id": 1, "created_ts": 100.0, "state": "intake"}

    def test_cycle_complet(self):
        j = progress._jalons_de_lot(self.item, [
            ev("intake", 100), ev("build", 110), ev("qa", 200), ev("qa", 210),
            ev("merged", 300, "codex3")], [])
        self.assertEqual((j["requested"], j["frozen"], j["verdict"], j["merged"]),
                         (100, 200, 300, 300))
        self.assertEqual(j["verdict_kind"], "ok")
        self.assertEqual(j["reviewer"], "codex3")
        self.assertEqual((j["blocked_verdicts"], j["blocks"]), (0, 0))

    def test_verdict_bloquant_puis_regel(self):
        j = progress._jalons_de_lot(self.item, [
            ev("intake", 100), ev("build", 110), ev("qa", 200), ev("build", 250, "rev"),
            ev("qa", 280), ev("blocked", 290), ev("waiting_human", 295), ev("qa", 300)], [])
        self.assertEqual(j["frozen"], 200)            # premier gel
        self.assertEqual(j["verdict"], 250)
        self.assertEqual(j["verdict_kind"], "blocked")
        self.assertEqual(j["blocked_verdicts"], 1)
        self.assertEqual(j["blocks"], 1)              # blocked → waiting_human = une attente
        self.assertIsNone(j["merged"])

    def test_action_git_merge(self):
        actions = [{"connector": "git-merge", "state": "confirmed",
                    "approved_ts": 400.0, "finished_ts": 450.0},
                   {"connector": "shell-noop", "state": "confirmed",
                    "approved_ts": 999.0, "finished_ts": 999.0}]
        j = progress._jalons_de_lot(self.item, [ev("intake", 100), ev("qa", 200)], actions)
        self.assertEqual((j["verdict"], j["verdict_kind"], j["merged"]), (400.0, "ok", 450.0))

    def test_regel_apres_approbation_repasse_en_revue(self):
        """Sonde codex3 B2 : qa 200 → build 300 → qa 400, action approuvée à 350.
        Le verdict retenu précède le DERNIER gel : en revue, pas approuvé ; le
        relecteur est l'auteur du verdict retenu (l'approbation), pas du rejet."""
        actions = [{"connector": "git-merge", "state": "approved", "approved_ts": 350,
                    "last_actor": "human:reviewer"}]
        events = [ev("intake", 100), ev("build", 110), ev("qa", 200),
                  ev("build", 300, "rev"), ev("qa", 400)]
        j = progress._jalons_de_lot(self.item, events, actions)
        self.assertEqual((j["frozen"], j["last_frozen"], j["verdict"]), (200, 400, 350))
        self.assertEqual(j["reviewer"], "human:reviewer")
        self.assertFalse(j["verdict_current"])
        self.assertEqual(progress.lot_state("qa", j), "review")
        # une approbation postérieure au regel, elle, approuve
        actions[0]["approved_ts"] = 450
        j = progress._jalons_de_lot(self.item, events, actions)
        self.assertEqual(progress.lot_state("qa", j), "approved")
        self.assertTrue(j["verdict_current"])

    def test_jalons_declares_l10(self):
        """Table des jalons de lot : gels et verdicts déclarés priment sur la
        déduction ; requested/merged automatiques repris."""
        declared = [
            {"kind": "requested", "at_ts": 90.0, "actor": ""},
            {"kind": "frozen", "at_ts": 150.0, "actor": "a1"},
            {"kind": "verdict", "at_ts": 170.0, "verdict": "blocked", "actor": "rev1"},
            {"kind": "frozen", "at_ts": 180.0, "actor": "a1"},
            {"kind": "verdict", "at_ts": 190.0, "verdict": "ok", "actor": "rev2"},
        ]
        events = [ev("intake", 100), ev("build", 110), ev("qa", 200)]
        j = progress._jalons_de_lot(self.item, events, [], declared)
        self.assertEqual(j["source"], "milestones")
        self.assertEqual((j["requested"], j["frozen"], j["last_frozen"], j["verdict"]),
                         (90.0, 150.0, 180.0, 190.0))
        self.assertEqual((j["verdict_kind"], j["reviewer"], j["blocked_verdicts"]),
                         ("ok", "rev2", 1))
        self.assertEqual(progress.lot_state("qa", j), "approved")
        # seulement les jalons automatiques : repli sur la déduction
        j = progress._jalons_de_lot(self.item, events, [], declared[:1])
        self.assertEqual((j["source"], j["frozen"], j["requested"]), ("events", 200, 100))

    def test_sans_journal(self):
        j = progress._jalons_de_lot(self.item, [], [])
        self.assertEqual(j["requested"], 100.0)
        self.assertIsNone(j["frozen"])
        self.assertEqual(j["blocked_verdicts"], 0)

    def test_etats_du_lot(self):
        self.assertEqual(progress.lot_state("build", {}), "active")
        self.assertEqual(progress.lot_state("intake", {}), "active")
        self.assertEqual(progress.lot_state("qa", {"frozen": 1}), "review")
        self.assertEqual(progress.lot_state(
            "qa", {"frozen": 1, "verdict": 2, "verdict_kind": "blocked"}), "review")
        self.assertEqual(progress.lot_state(
            "qa", {"frozen": 1, "verdict": 2, "verdict_kind": "ok"}), "approved")
        self.assertEqual(progress.lot_state("waiting_human", {}), "blocked")
        self.assertEqual(progress.lot_state("blocked", {}), "blocked")
        self.assertEqual(progress.lot_state("merged", {}), "merged")
        self.assertEqual(progress.lot_state("promoted", {}), "merged")


class AgentStateTest(unittest.TestCase):
    def test_etats(self):
        self.assertEqual(progress.agent_state({"status": "running", "lease_live": True}), "working")
        self.assertEqual(progress.agent_state({"status": "running", "lease_live": False}), "stopped")
        self.assertEqual(progress.agent_state({"status": "blocked"}), "paused")
        self.assertEqual(progress.agent_state({"status": "stopped"}), "stopped")
        self.assertEqual(progress.agent_state({"status": "dead"}), "stopped")
        self.assertEqual(progress.agent_state({"status": "idle"}), "idle")
        self.assertEqual(progress.agent_state({"status": "queued"}), "idle")

    def test_tour_en_cours(self):
        row = {"name": "a1", "status": "running", "lease_live": True,
               "turn_started_ts": NOW - 125, "status_text": "tour consigne (codex)",
               "current_prompt": "Écris le test\n  du lot   L24 " + "x" * 300}
        agent = progress.build_agent(row, NOW, "high")
        self.assertEqual(agent["turn"]["duration_s"], 125)
        self.assertTrue(agent["turn"]["task"].startswith("Écris le test du lot L24 "))
        self.assertLessEqual(len(agent["turn"]["task"]), progress.TASK_CHARS)
        self.assertEqual(agent["effort"], "high")
        self.assertEqual(agent["since_ts"], NOW - 125)


class BudgetTest(unittest.TestCase):
    def test_paye_et_forfaits(self):
        rows = [
            {"agent": "d1", "harness": "deepseek", "model": "deepseek-flash",
             "usd_window": 1.5, "usd_1h": 0.5, "usd_24h": 2.0, "turns": 3,
             "input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5},
            {"agent": "c1", "harness": "claude", "model": "",
             "usd_window": 4.0, "usd_1h": 1.0, "usd_24h": 4.0, "turns": 2,
             "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
        ]
        gauges = [cost.Gauge("claude", "five_hour", 0.8, NOW + 3600, 5 * 3600)]
        b = progress.build_budget(rows, gauges, NOW)
        self.assertEqual(b["spend"]["1h"], {"total_usd": 1.5, "paid_usd": 0.5})
        self.assertEqual(b["spend"]["window"], {"total_usd": 5.5, "paid_usd": 1.5})
        self.assertEqual([r["paid"] for r in b["by_agent"]], [True, False])
        plan = b["plans"][0]
        self.assertEqual(plan["key"], "five_hour")
        self.assertFalse(plan["exceeded"])        # 80 % utilisé pour un plafond de 90 %
        self.assertAlmostEqual(plan["pace_cap"], 0.9)


def _snap(**over):
    snap = {
        "schema": progress.SCHEMA, "generated_ts": NOW, "generated_at": "x", "host": "h",
        "project": None, "window": {"from_ts": NOW - 86400, "to_ts": NOW},
        "lots": [{"id": 7, "title": "Un titre de lot assez long pour être coupé proprement "
                  "</script><b>gras</b> & co", "state": "review", "work_state": "qa",
                  "milestones": {"requested": NOW - 7200, "frozen": NOW - 3600,
                                 "verdict": None, "merged": None},
                  "verdict": None, "blocked_verdicts": 1, "blocks": 0, "assignee": "a1",
                  "reviewer": None, "actions": []}],
        "agents": [{"name": "a1", "harness": "codex", "model": None, "effort": None,
                    "state": "working", "since_ts": NOW - 60, "status_text": "x",
                    "turn": {"task": "une tâche", "label": "tour", "started_ts": NOW - 60,
                             "duration_s": 60}, "unread": 0}],
        "milestones": [], "actions": [],
        "budget": progress.build_budget([], [], NOW), "missing": list(progress.MISSING),
    }
    snap.update(over)
    return snap


class RenderTest(unittest.TestCase):
    def test_texte_etroit(self):
        text = progress.format_text(_snap(), width=40)
        for line in text.splitlines():
            self.assertLessEqual(len(line), 40, line)
        self.assertIn("#7 en revue", text)
        self.assertIn("blocages 1", text)
        self.assertIn("JALONS : aucun déclaré au canon", text)
        self.assertIn("a1 — travaille", text)

    def test_html_autonome_et_echappe(self):
        html = progress.render_html(_snap())
        self.assertNotIn("</script><b>", html)
        self.assertNotIn("__AMEESH_PROGRESS_DATA__", html)
        # aucune ressource externe : ni src/href distants, ni import, ni CDN
        self.assertIsNone(re.search(r"(src|href)\s*=\s*[\"']?(https?:)?//", html, re.I))
        self.assertNotIn("@import", html)
        self.assertNotIn("fetch(", html)
        self.assertIn("default-src 'none'", html)
        self.assertIn('name="viewport"', html)
        # les données embarquées se relisent à l'identique
        blob = re.search(r'<script type="application/json" id="data">(.*?)</script>',
                         html, re.S).group(1)
        self.assertEqual(json.loads(blob)["lots"][0]["title"], _snap()["lots"][0]["title"])


class ProgressPgTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE action_events, action_attempts, actions, turn_costs, "
                        "spend_pending RESTART IDENTITY CASCADE")

    def _book(self):
        return cost.CostBook(state_dir=self.state, codex_sessions=os.path.join(self.tmp, "codex"),
                             db=self.db)

    def _action(self, item_id, state, project="demo"):
        action_id = "act_" + ("%026d" % item_id)
        self.db.execute(
            "INSERT INTO actions (action_id, project, work_item, proposed_by, connector,"
            " operation, target, args, class, digest, dedupe, requires_receipt)"
            " VALUES (%s, %s, %s, 'agent:a1', 'git-merge', 'merge', 'repo#1', '{}',"
            " 'reversible', %s, 'none', false)",
            (action_id, project, item_id, "sha256:" + "0" * 64))
        if state != "proposed":
            self.db.execute("UPDATE actions SET state = 'approved', auth_kind = 'none',"
                            " approved_at = now() WHERE action_id = %s", (action_id,))
        return action_id

    def test_instantane(self):
        a = work.add(self.db, title="lot actif", app="demo", assignee="a1")
        work.move(self.db, a["id"], "build")
        b = work.add(self.db, title="lot en revue", app="demo")
        for s in ("build", "qa", "build", "qa"):
            work.move(self.db, b["id"], s, actor="rev")
        c = work.add(self.db, title="lot approuvé", app="demo")
        for s in ("build", "qa"):
            work.move(self.db, c["id"], s)
        act = self._action(c["id"], "approved")
        d = work.add(self.db, title="lot fusionné", app="demo")
        for s in ("build", "qa", "merged"):
            work.move(self.db, d["id"], s, actor="codex3")
        e = work.add(self.db, title="lot bloqué", app="autre")
        work.move(self.db, e["id"], "blocked")
        # un lot fusionné hors fenêtre n'apparaît pas
        old = work.add(self.db, title="vieux lot", app="demo")
        for s in ("build", "qa", "merged"):
            work.move(self.db, old["id"], s)
        self.db.execute("UPDATE work_items SET updated_at = now() - interval '3 days'"
                        " WHERE id = %s", (old["id"],))

        registry.upsert(self.db, "a1", chantier="demo", harness="codex", host=self.cfg.host,
                        model="gpt-x")
        registry.upsert(self.db, "a2", chantier="autre", harness="deepseek", host="ailleurs")
        registry.upsert(self.db, "a3", chantier="demo", harness="claude", host="ailleurs")
        registry.set_status(self.db, "a3", "blocked", status_text="budget : forfait")
        # R14 : un agent n'est réclamable qu'avec un responsable résolu
        self.db.execute("UPDATE agent_registry SET responsible = 'human:test'")
        lease = registry.claim(self.db, "a1", "runner-test", 60)
        self.assertIsNotNone(lease)
        epoch = int(lease["lease_epoch"])
        registry.set_pending_prompt(self.db, "a1", "Implémente la frise")
        self.assertEqual(registry.take_pending_prompt(self.db, "a1", "runner-test", epoch),
                         "Implémente la frise")
        registry.pending_spend_put(self.db, "a1", 0, "consigne", "gpt-x")
        os.makedirs(self.cfg.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(self.cfg.agent_dir("a1"), "effort"), "w") as fh:
            fh.write("high\n")

        tc = storage.of(self.db).turn_costs
        for agent, harness, usd in (("a2", "deepseek", 0.25), ("a2", "deepseek", 0.5),
                                    ("a1", "codex", 2.0)):
            tc.insert(agent=agent, harness=harness, turn="t", model=None, session=None,
                      usd=usd, input_tokens=100, cached_input_tokens=10, output_tokens=7,
                      cum_usd=None, cum_input_tokens=None, cum_cached_input_tokens=None,
                      cum_output_tokens=None)
        self.db.execute("UPDATE turn_costs SET recorded_at = now() - interval '3 hours'"
                        " WHERE usd = 0.5")

        snap = progress.snapshot(self.db, self.cfg, since="24h", book=self._book())
        self.assertEqual(snap["schema"], "ameesh-progress/1")
        lots = {l["title"]: l for l in snap["lots"]}
        self.assertNotIn("vieux lot", lots)
        self.assertEqual(lots["lot actif"]["state"], "active")
        self.assertEqual(lots["lot en revue"]["state"], "review")
        self.assertEqual(lots["lot en revue"]["blocked_verdicts"], 1)
        self.assertEqual(lots["lot en revue"]["verdict"], "blocked")
        self.assertEqual(lots["lot en revue"]["reviewer"], "rev")
        self.assertEqual(lots["lot approuvé"]["state"], "approved")
        self.assertEqual(lots["lot approuvé"]["actions"], [act])
        self.assertEqual(lots["lot fusionné"]["state"], "merged")
        self.assertEqual(lots["lot bloqué"]["state"], "blocked")
        self.assertEqual(lots["lot bloqué"]["blocks"], 1)
        m = lots["lot fusionné"]["milestones"]
        self.assertTrue(m["requested"] <= m["frozen"] <= m["verdict"] <= m["merged"])
        self.assertEqual(set(m), {"requested", "frozen", "verdict", "merged"})
        self.assertEqual([x["id"] for x in snap["actions"]], [act])

        agents = {x["name"]: x for x in snap["agents"]}
        self.assertEqual(agents["a1"]["state"], "working")
        self.assertEqual(agents["a1"]["effort"], "high")
        self.assertEqual(agents["a1"]["model"], "gpt-x")
        self.assertEqual(agents["a1"]["turn"]["task"], "Implémente la frise")
        self.assertIsNotNone(agents["a1"]["turn"]["started_ts"])
        self.assertEqual(agents["a2"]["state"], "idle")
        self.assertIsNone(agents["a2"]["effort"])           # autre hôte : inconnu
        self.assertEqual(agents["a3"]["state"], "paused")

        sp = snap["budget"]["spend"]
        self.assertAlmostEqual(sp["1h"]["paid_usd"], 0.25)
        self.assertAlmostEqual(sp["24h"]["paid_usd"], 0.75)
        self.assertAlmostEqual(sp["24h"]["total_usd"], 2.75)
        self.assertEqual(snap["budget"]["plans"], [])
        self.assertEqual(snap["milestones"], [])
        json.dumps(snap)                                     # sérialisable tel quel

        # filtre de projet : lots, agents et coûts du projet seulement
        demo = progress.snapshot(self.db, self.cfg, since="24h", project="demo",
                                 book=self._book())
        self.assertNotIn("lot bloqué", {l["title"] for l in demo["lots"]})
        self.assertEqual({x["name"] for x in demo["agents"]}, {"a1", "a3"})
        self.assertAlmostEqual(demo["budget"]["spend"]["24h"]["total_usd"], 2.0)
        self.assertEqual(demo["budget"]["spend"]["24h"]["paid_usd"], 0.0)

        # fenêtre courte : le lot fusionné il y a peu reste, les coûts de 3 h sortent
        court = progress.snapshot(self.db, self.cfg, since="1h", book=self._book())
        self.assertIn("lot fusionné", {l["title"] for l in court["lots"]})
        self.assertAlmostEqual(court["budget"]["spend"]["window"]["paid_usd"], 0.25)

    def test_lot_ouvert_jamais_masque_par_les_fusionnes(self):
        """Sonde codex3 B1 : 500 lots récemment fusionnés ne masquent pas un
        lot OUVERT ; la coupure est dite dans `truncated`."""
        self.db.execute(
            "INSERT INTO work_items (title, state, created_at, updated_at)"
            " SELECT 'vieux-' || n, 'merged', now() - interval '2 hours', now()"
            " FROM generate_series(1, 500) n")
        new = work.add(self.db, title="LOT COURANT", app="demo")
        snap = progress.snapshot(self.db, self.cfg, book=self._book())
        self.assertIn(new["id"], [x["id"] for x in snap["lots"]])
        self.assertEqual(snap["truncated"]["lots"],
                         {"shown": progress.MAX_LOTS, "total": 501, "limit": progress.MAX_LOTS})
        self.assertNotIn("actions", snap["truncated"])
        small = progress.snapshot(self.db, self.cfg, book=self._book(), max_lots=3)
        self.assertIn(new["id"], [x["id"] for x in small["lots"]])   # l'ouvert d'abord
        self.assertEqual(len(small["lots"]), 3)
        self.assertIn("TRONQUÉ : 3 lots affichés sur 501", progress.format_text(small, 60))

    def test_approbation_non_perdue_dans_la_borne_des_actions(self):
        """Sonde codex3 B1 : 500 actions sans lot ne masquent pas
        l'approbation de fusion d'un lot affiché."""
        item = work.add(self.db, title="lot en revue", app="demo")
        work.move(self.db, item["id"], "build")
        work.move(self.db, item["id"], "qa")
        self.db.execute(
            "INSERT INTO actions (action_id, project, proposed_by, connector, operation,"
            " target, args, class, digest, dedupe, requires_receipt, created_at)"
            " SELECT 'act_' || lpad((n + 10000)::text, 26, '0'), 'bruit', 'agent:a',"
            " 'shell-noop', 'run', 'noop', '{}', 'reversible', 'sha256:' || repeat('0', 64),"
            " 'none', false, now() - interval '2 hours' FROM generate_series(1, 500) n")
        action = self._action(item["id"], "approved")
        snap = progress.snapshot(self.db, self.cfg, book=self._book())
        lot = next(x for x in snap["lots"] if x["id"] == item["id"])
        self.assertEqual(lot["state"], "approved")
        self.assertEqual(lot["actions"], [action])
        self.assertEqual(snap["truncated"]["actions"]["total"], 501)
        # les non terminales passent en premier dans la borne de rendu
        self.assertIn(action, [a["id"] for a in snap["actions"]])

    def test_jalons_de_la_table_l10(self):
        """Gel et verdicts déclarés (`ameesh work milestone`, L10) : la frise
        les lit ; un lot sans déclaration garde la déduction."""
        a = work.add(self.db, title="déclaré", app="demo")
        work.move(self.db, a["id"], "build")
        work.move(self.db, a["id"], "qa")
        work.milestone(self.db, a["id"], "frozen", sha="a" * 40, actor="a1")
        work.milestone(self.db, a["id"], "verdict", sha="a" * 40, actor="rev1",
                       verdict="blocked")
        work.milestone(self.db, a["id"], "frozen", sha="b" * 40, actor="a1")
        work.milestone(self.db, a["id"], "verdict", sha="b" * 40, actor="rev2", verdict="ok")
        b = work.add(self.db, title="déduit", app="demo")
        work.move(self.db, b["id"], "build")
        work.move(self.db, b["id"], "qa")
        snap = progress.snapshot(self.db, self.cfg, book=self._book())
        lots = {l["title"]: l for l in snap["lots"]}
        lot = lots["déclaré"]
        self.assertEqual(lot["milestones_source"], "milestones")
        self.assertEqual((lot["state"], lot["verdict"], lot["reviewer"]),
                         ("approved", "ok", "rev2"))
        self.assertEqual(lot["blocked_verdicts"], 1)
        self.assertIsNotNone(lot["milestones"]["requested"])
        self.assertLess(lot["milestones"]["frozen"], lot["last_frozen_ts"])
        self.assertEqual(lots["déduit"]["milestones_source"], "events")
        self.assertEqual(lots["déduit"]["state"], "review")
        self.assertIsNotNone(lots["déduit"]["milestones"]["frozen"])

    def test_vide(self):
        snap = progress.snapshot(self.db, self.cfg, book=self._book())
        self.assertEqual((snap["lots"], snap["agents"], snap["actions"]), ([], [], []))
        self.assertEqual(snap["budget"]["spend"]["24h"]["total_usd"], 0.0)
        st = storage.of(self.db).progress
        self.assertEqual(st.lot_events([]), [])
        self.assertEqual(st.costs(since_ts=0, agents=[]), [])

    def test_cli(self):
        item = work.add(self.db, title="lot cli", app="demo")
        work.move(self.db, item["id"], "build")
        # HOME du bac à sable : les jauges ne lisent jamais ~/.codex réel ;
        # le mot de passe du banc reste celui de l'environnement ou du ~/.pgpass réel.
        extra = {"HOME": self.tmp}
        pgpass = os.path.expanduser("~/.pgpass")
        if "PGPASSFILE" not in os.environ and os.path.exists(pgpass):
            extra["PGPASSFILE"] = pgpass
        env = self.env(**extra)
        out = self.mesh("progress", "--json", "--since", "2d", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(data["schema"], "ameesh-progress/1")
        self.assertEqual([l["title"] for l in data["lots"]], ["lot cli"])

        text = self.mesh("progress", env=dict(env, COLUMNS="40"))
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("lot cli", text.stdout)
        self.assertTrue(all(len(l) <= 40 for l in text.stdout.splitlines()))

        page = os.path.join(self.tmp, "frise.html")
        html = self.mesh("progress", "--html", page, "--project", "demo", env=env)
        self.assertEqual(html.returncode, 0, html.stderr)
        with open(page, encoding="utf-8") as fh:
            self.assertIn("lot cli", fh.read())

        bad = self.mesh("progress", "--since", "hier", env=env)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("--since illisible", bad.stderr)


if __name__ == "__main__":
    unittest.main()
