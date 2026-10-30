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

from ameesh import cost, progress, registry, stagnation, storage, work

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

    def test_livre_hors_cible(self):
        """#988 : le jalon `delivered` date un lot livré hors cible ; `merged`
        reste nul (la livraison n'est pas une fusion)."""
        item = {"id": 2, "created_ts": 100.0, "state": "closed",
                "close_reason": "delivered"}
        j = progress._jalons_de_lot(item, [
            ev("intake", 100), ev("build", 110), ev("closed", 400)], [], [
            {"kind": "delivered", "at_ts": 400.0, "actor": "dev1"}])
        self.assertEqual(j["delivered"], 400.0)
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
        Le verdict retenu précède le DERNIER gel : en revue, pas approuvé. Un
        verdict périmé ne désigne personne (#1318) : sans désignation, la porte
        est « un relecteur (non désigné) », ni l'auteur de l'approbation caduque
        ni celui du rejet."""
        actions = [{"connector": "git-merge", "state": "approved", "approved_ts": 350,
                    "last_actor": "human:reviewer"}]
        events = [ev("intake", 100), ev("build", 110), ev("qa", 200),
                  ev("build", 300, "rev"), ev("qa", 400)]
        j = progress._jalons_de_lot(self.item, events, actions)
        self.assertEqual((j["frozen"], j["last_frozen"], j["verdict"]), (200, 400, 350))
        self.assertIsNone(j["reviewer"])
        self.assertFalse(j["verdict_current"])
        self.assertEqual(progress.lot_state("qa", j), "review")
        # le relecteur désigné (#705), lui, reste nommé après un regel
        j = progress._jalons_de_lot(dict(self.item, reviewer="rev2"), events, actions)
        self.assertEqual(j["reviewer"], "rev2")
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

    def test_1693_correction_annule_la_fusion_dans_toutes_les_sources(self):
        events = [ev("intake", 100), ev("qa", 200), ev("merged", 300), ev("build", 400)]
        actions = [{"connector": "git-merge", "state": "confirmed", "finished_ts": 300}]
        for declared in ([], [{"kind": "frozen", "at_ts": 200}],
                         [{"kind": "frozen", "at_ts": 200},
                          {"kind": "merged", "at_ts": 300}]):
            with self.subTest(declared=declared):
                j = progress._jalons_de_lot(self.item, events, actions, declared)
                self.assertIsNone(j["merged"])
                j = progress._jalons_de_lot(self.item, events + [ev("qa", 450)],
                                            actions, declared)
                self.assertIsNone(j["merged"])
                j = progress._jalons_de_lot(self.item, events + [ev("merged", 500)],
                                            actions, declared)
                self.assertEqual(j["merged"], 500)

    def test_1693_blocage_apres_fusion_conserve_sa_date(self):
        for suite in ([ev("blocked", 400)], [ev("promoted", 400), ev("merged", 500)]):
            with self.subTest(suite=suite):
                j = progress._jalons_de_lot(self.item,
                                            [ev("intake", 100), ev("merged", 300)] + suite, [])
                self.assertEqual(j["merged"], 300)

    def test_1693_le_dernier_jalon_merged_effectif_date(self):
        # #1693 (N4) : plusieurs jalons `merged` coexistent (l'historique
        # n'est pas réécrit) ; le dernier encore effectif date la livraison.
        events = [ev("intake", 100), ev("qa", 200), ev("merged", 300),
                  ev("build", 400), ev("qa", 450), ev("merged", 500)]
        declared = [{"kind": "merged", "at_ts": 300},
                    {"kind": "merged", "at_ts": 500}]
        j = progress._jalons_de_lot(self.item, events, [], declared)
        self.assertEqual(j["merged"], 500)
        # sans correction, le dernier jalon déclaré l'emporte aussi
        j = progress._jalons_de_lot(self.item, [ev("intake", 100)], [], declared)
        self.assertEqual(j["merged"], 500)

    def test_sans_journal(self):
        j = progress._jalons_de_lot(self.item, [], [])
        self.assertEqual(j["requested"], 100.0)
        self.assertIsNone(j["frozen"])
        self.assertEqual(j["blocked_verdicts"], 0)

    def test_gel_renouvele_meme_sha_preserve_le_verdict(self):
        """#1389 : un gel administratif renouvelé au MÊME SHA ne refait pas le
        verdict ; l'avis `ok` posté avant reste courant (lot approuvé)."""
        sha = "a" * 40
        declared = [
            {"kind": "frozen", "at_ts": 150.0, "sha": sha, "actor": "a1"},
            {"kind": "verdict", "at_ts": 170.0, "sha": sha, "verdict": "ok", "actor": "rev1"},
            {"kind": "frozen", "at_ts": 180.0, "sha": sha, "actor": "a1"},
        ]
        j = progress._jalons_de_lot(self.item, [ev("intake", 100)], [], declared)
        self.assertEqual(j["last_frozen"], 180.0)
        self.assertEqual(j["verdict"], 170.0)
        self.assertTrue(j["verdict_current"])
        self.assertEqual(progress.lot_state("qa", j), "approved")
        self.assertEqual(stagnation.waiting({"state": "qa", "assignee": "auteur"}, j)["what"],
                         "merge")

    def test_verdict_d_une_autre_tete_ne_compte_pas(self):
        """Un avis nommant une autre tête ne vaut pas pour le gel courant."""
        declared = [
            {"kind": "frozen", "at_ts": 150.0, "sha": "a" * 40, "actor": "a1"},
            {"kind": "verdict", "at_ts": 170.0, "sha": "a" * 40, "verdict": "ok",
             "actor": "rev1"},
            {"kind": "frozen", "at_ts": 180.0, "sha": "b" * 40, "actor": "a1"},
        ]
        j = progress._jalons_de_lot(self.item, [ev("intake", 100)], [], declared)
        self.assertFalse(j["verdict_current"])
        self.assertEqual(progress.lot_state("qa", j), "review")
        self.assertEqual(stagnation.waiting({"state": "qa", "assignee": "auteur"}, j)["what"],
                         "verdict")

    def test_gel_renouvele_meme_sha_ne_masque_pas_un_no_go(self):
        """Un verdict bloquant sur la tête gelée reste bloquant après un gel
        renouvelé au même SHA : le NO-GO n'est pas masqué (#1389)."""
        sha = "c" * 40
        declared = [
            {"kind": "frozen", "at_ts": 150.0, "sha": sha, "actor": "a1"},
            {"kind": "verdict", "at_ts": 170.0, "sha": sha, "verdict": "blocked",
             "actor": "rev1"},
            {"kind": "frozen", "at_ts": 180.0, "sha": sha, "actor": "a1"},
        ]
        j = progress._jalons_de_lot(self.item, [ev("intake", 100)], [], declared)
        self.assertEqual(j["verdict_kind"], "blocked")
        self.assertTrue(j["verdict_current"])
        self.assertEqual(stagnation.waiting({"state": "qa", "assignee": "auteur"}, j)["what"],
                         "fix")

    def test_plusieurs_lecteurs_le_dernier_avis_sur_la_tete_gagne(self):
        """Plusieurs lecteurs sur la même tête : l'avis le plus récent tranche ;
        un `ok` plus récent lève un `blocked` antérieur de la même tête."""
        sha = "d" * 40
        declared = [
            {"kind": "frozen", "at_ts": 150.0, "sha": sha, "actor": "a1"},
            {"kind": "verdict", "at_ts": 160.0, "sha": sha, "verdict": "blocked",
             "actor": "rev1"},
            {"kind": "verdict", "at_ts": 170.0, "sha": sha, "verdict": "ok", "actor": "rev2"},
            {"kind": "frozen", "at_ts": 180.0, "sha": sha, "actor": "a1"},
        ]
        j = progress._jalons_de_lot(self.item, [ev("intake", 100)], [], declared)
        self.assertEqual((j["verdict_kind"], j["reviewer"]), ("ok", "rev2"))
        self.assertTrue(j["verdict_current"])
        self.assertEqual(progress.lot_state("qa", j), "approved")

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
        # L95 : payé au token et valeur des forfaits séparés (`total_usd` gardé)
        self.assertEqual(b["spend"]["1h"], {"total_usd": 1.5, "paid_usd": 0.5,
                                            "plan_value_usd": 1.0})
        self.assertEqual(b["spend"]["window"], {"total_usd": 5.5, "paid_usd": 1.5,
                                                "plan_value_usd": 4.0})
        self.assertEqual([r["paid"] for r in b["by_agent"]], [True, False])
        plan = b["plans"][0]
        self.assertEqual(plan["key"], "five_hour")
        self.assertFalse(plan["exceeded"])        # 80 % utilisé pour un plafond de 90 %
        self.assertAlmostEqual(plan["pace_cap"], 0.9)

    def test_le_plan_d_un_compte_declare_suit_son_compte_actif(self):
        # #1028 P2 (R1 cx2) : le plan d'un harnais à comptes déclarés porte la
        # jauge ET le rythme du compte ACTIF. Le croisement « jauge du dossier
        # par défaut (80 %), multiplicateur du compte actif (m2) » publiait
        # 80 % / plafond 30 % / dépassé alors qu'aucun compte n'est dans cet
        # état : primaire 80 %/m1 (inactif), secondaire 20 %/m2 (actif).
        window = 7 * 86400.0
        primaire = cost.Gauge("codex", "week", 0.80, NOW + 0.9 * window, window)
        secondaire = cost.Gauge("codex", "week", 0.20, NOW + 0.9 * window, window)
        comptes = [
            {"harness": "codex", "account": "primaire", "active": False,
             "pace_multiplier": 1.0, "gauges": [], "_gauges": [primaire]},
            {"harness": "codex", "account": "secondaire", "active": True,
             "pace_multiplier": 2.0, "gauges": [], "_gauges": [secondaire]},
        ]
        b = progress.build_budget([], [primaire], NOW, accounts=comptes)
        self.assertEqual(len(b["plans"]), 1)
        plan = b["plans"][0]
        self.assertEqual(plan["used"], 0.20)          # la jauge du compte actif
        self.assertAlmostEqual(plan["pace_cap"], 0.30)  # 2 × 10 % + 10 %
        self.assertFalse(plan["exceeded"])            # le primaire l'est, pas lui
        # harnais sans compte déclaré : jauge et rythme nominaux, le
        # multiplicateur du compte actif ne fuit pas
        libre = cost.Gauge("claude", "five_hour", 0.05, NOW + 0.9 * 3600, 3600)
        b = progress.build_budget([], [libre], NOW, accounts=comptes)
        plans = {p["harness"]: p for p in b["plans"]}
        self.assertAlmostEqual(plans["claude"]["pace_cap"], 0.20)  # 10 % + 10 %
        self.assertEqual(plans["claude"]["used"], 0.05)
        self.assertEqual(plans["codex"]["used"], 0.20)  # compte actif, sans fuite
        # sans compte déclaré : rien ne change
        nomme = cost.Gauge("codex", "week", 0.25, NOW + 0.9 * window, window)
        b = progress.build_budget([], [nomme], NOW)
        self.assertAlmostEqual(b["plans"][0]["pace_cap"], 0.20)
        self.assertTrue(b["plans"][0]["exceeded"])


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
        self.assertIn("#7 in review", text)
        self.assertIn("blocks 1", text)
        self.assertIn("MILESTONES: none declared in the canon", text)
        self.assertIn("a1 — working", text)

    def test_html_autonome_et_echappe(self):
        html = progress.render_html(_snap())
        self.assertNotIn("</script><b>", html)
        self.assertNotIn("__AMEESH_PROGRESS_DATA__", html)
        # aucune ressource externe : ni src/href distants, ni import, ni CDN
        self.assertIsNone(re.search(r"(src|href)\s*=\s*[\"']?(https?:)?//", html, re.I))
        self.assertNotIn("@import", html)
        # L304 : le code de rafraîchissement existe, mais la page statique ne
        # peut rien demander au réseau (aucun connect-src)
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', html).group(1)
        self.assertTrue(csp.startswith("default-src 'none';"), csp)
        self.assertNotIn("connect-src", csp)
        self.assertIn('name="viewport"', html)
        # les données embarquées se relisent à l'identique
        blob = re.search(r'<script type="application/json" id="data">(.*?)</script>',
                         html, re.S).group(1)
        self.assertEqual(json.loads(blob)["lots"][0]["title"], _snap()["lots"][0]["title"])


class CiSuiteTest(unittest.TestCase):
    """#1476 : durée médiane de la suite complète par dépôt (veilleur CI) :
    médiane et p80, part couverte par le pool prioritaire de #1468, verdict
    face à la cible de 15 min portée par la vue."""

    @staticmethod
    def _run(repo, branch, base, duration, ts, run_id=1, event="push"):
        return {"repo": repo, "run_id": run_id, "branch": branch, "base": base,
                "event": event, "duration_s": duration, "concluded_ts": ts}

    def test_mediane_et_pool_prioritaire(self):
        rows = [
            self._run("manaty/ameesh", "integration/1204", "main", 720.0, 10, 1),
            self._run("manaty/ameesh", "lot/1476", "integration/1204", 1080.0, 20, 2,
                      event="pull_request"),
            self._run("manaty/ameesh", "main", "main", 1800.0, 30, 3),
            self._run("manaty/ameesh", "release/1.11.0", "main", 540.0, 40, 4),
            self._run("manaty/nexlink", "main", "main", 3600.0, 50, 5),
        ]
        out = progress.build_ci_suite(rows)
        self.assertEqual(out["schema"], progress.CI_SUITE_SCHEMA)
        self.assertEqual(out["target_s"], progress.CI_SUITE_TARGET_S)
        self.assertEqual(out["window_days"], progress.CI_SUITE_WINDOW_D)
        self.assertEqual([r["repo"] for r in out["repos"]],
                         ["manaty/ameesh", "manaty/nexlink"])
        ameesh = out["repos"][0]
        self.assertEqual(ameesh["runs"], 4)
        self.assertEqual(ameesh["median_s"], 900.0)      # (720 + 1080) / 2
        self.assertEqual(ameesh["p80_s"], 1368.0)        # interpolation sur 4 points
        self.assertEqual(ameesh["priority_runs"], 3)     # source, cible, release
        self.assertEqual(ameesh["priority_median_s"], 720.0)
        self.assertEqual(ameesh["last_s"], 540.0)
        self.assertEqual(ameesh["last_concluded_ts"], 40)
        nexlink = out["repos"][1]
        self.assertEqual(nexlink["priority_runs"], 0)
        self.assertIsNone(nexlink["priority_median_s"])

    def test_sans_duree_ni_ligne(self):
        self.assertEqual(progress.build_ci_suite([])["repos"], [])
        rows = [self._run("r", "main", "main", None, 10, 1),
                self._run("r", "main", "main", 600.0, 5, 2)]
        repo = progress.build_ci_suite(rows)["repos"][0]
        self.assertEqual(repo["runs"], 1)                 # une durée inconnue ne compte pas
        self.assertEqual(repo["median_s"], 600.0)
        self.assertIsNone(repo["last_s"])                 # la dernière conclue n'a pas de durée
        self.assertEqual(repo["last_concluded_ts"], 10)

    def test_lignes_et_verdict(self):
        rows = [
            self._run("r/prioritaire", "integration/1", "main", 600.0, 1, 1),
            self._run("r/prioritaire", "main", "main", 2400.0, 2, 2),
            self._run("r/cible", "integration/1", "main", 600.0, 3, 3),
            self._run("r/cible", "integration/1", "main", 720.0, 4, 4),
            self._run("r/lente", "main", "main", 2400.0, 5, 5),
            self._run("r/lente", "main", "main", 3000.0, 6, 6),
        ]
        lines = progress._ci_suite_lines(progress.build_ci_suite(rows), 60)
        text = "\n".join(lines)
        for line in text.splitlines():
            self.assertLessEqual(len(line), 60, line)
        plat = " ".join(text.split())          # le repli ne cache pas un verdict
        self.assertIn("target ≤ 15 min", plat)
        self.assertIn("r/cible: median 11 min", plat)
        self.assertIn("target met", plat)
        self.assertIn("r/prioritaire: median 25 min", plat)
        self.assertIn("target met on the priority pool only", plat)
        self.assertIn("r/lente: median 45 min", plat)
        self.assertIn("target not met", plat)
        vide = {"window_days": 7, "target_s": 900,
                "repos": [{"repo": "r/vide", "runs": 0}]}
        self.assertIn("r/vide: no concluded full suite",
                      " ".join(" ".join(progress._ci_suite_lines(vide, 60)).split()))

    def test_le_pool_prioritaire_suit_la_source_et_la_cible(self):
        # #1468 : integration/** et release/** en source comme en cible ; main
        # (poussée qui suit la fusion d'un paquet) ne l'est pas.
        for branch, base, attendu in (("integration/1", "main", True),
                                      ("lot/x", "integration/1", True),
                                      ("release/1.11.0", "main", True),
                                      ("lot/x", "release/1.11.0", True),
                                      ("main", "main", False),
                                      ("lot/x", "main", False)):
            self.assertEqual(progress._ci_priority({"branch": branch, "base": base}),
                             attendu, (branch, base))


class ProgressPgTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE action_events, action_attempts, actions, turn_costs, "
                        "spend_pending, ci_runs RESTART IDENTITY CASCADE")

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
        # L37 (0030) : l'assigné d'un lot doit exister (attribution gardée)
        registry.upsert(self.db, "a1", chantier="demo", harness="codex", host=self.cfg.host)
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
        # L102 T3 (#4393) : le vocabulaire du module actif (dev : « lot »)
        self.assertEqual(snap["vocabulary"]["singular"], "lot")
        lots = {l["title"]: l for l in snap["lots"]}
        self.assertNotIn("vieux lot", lots)
        self.assertEqual(lots["lot actif"]["state"], "active")
        self.assertEqual(lots["lot en revue"]["state"], "review")
        self.assertEqual(lots["lot en revue"]["blocked_verdicts"], 1)
        self.assertEqual(lots["lot en revue"]["verdict"], "blocked")
        # un verdict bloquant périmé (lot regelé) ne désigne plus son auteur
        # (#1318) : sans relecteur désigné, la porte n'est nommée par personne.
        self.assertIsNone(lots["lot en revue"]["reviewer"])
        self.assertEqual(lots["lot approuvé"]["state"], "approved")
        self.assertEqual(lots["lot approuvé"]["actions"], [act])
        self.assertEqual(lots["lot fusionné"]["state"], "merged")
        self.assertEqual(lots["lot bloqué"]["state"], "blocked")
        self.assertEqual(lots["lot bloqué"]["blocks"], 1)
        m = lots["lot fusionné"]["milestones"]
        self.assertTrue(m["requested"] <= m["frozen"] <= m["verdict"] <= m["merged"])
        self.assertEqual(set(m), {"requested", "frozen", "verdict", "merged", "delivered"})
        self.assertIsNone(m["delivered"])          # #988 : livré hors cible seulement
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
        self.assertIn("TRUNCATED: 3 of 501 lots shown", progress.format_text(small, 60))

    def test_timeline_dit_sa_troncature(self):
        """#3401 : la Chronologie dit la borne de rendu au lieu de faire croire
        à une liste complète, comme l'instantané `progress`."""
        self.db.execute(
            "INSERT INTO work_items (title, state, created_at, updated_at)"
            " SELECT 'vieux-' || n, 'merged', now() - interval '2 hours', now()"
            " FROM generate_series(1, 500) n")
        new = work.add(self.db, title="LOT COURANT", app="demo")
        cut = progress.timeline(self.db, self.cfg, max_lots=3)
        self.assertEqual(cut["truncated"],
                         {"lots": {"shown": 3, "total": 501, "limit": 3}})
        self.assertIn(new["id"], [x["id"] for x in cut["lots"]])   # l'ouvert d'abord
        self.assertEqual(len(cut["lots"]), 3)
        plain = progress.timeline(self.db, self.cfg, max_lots=1000)
        self.assertEqual(plain["truncated"], {})

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

    def test_gel_renouvele_meme_sha_garde_le_verdict(self):
        """#1389 : regeler au MÊME SHA après l'avis ne refait pas le verdict ;
        la frise reste `approved` et l'attente est la fusion, pas une relecture.
        Un gel sur une AUTRE tête, lui, rouvre la revue."""
        sha = "a" * 40
        a = work.add(self.db, title="gel renouvelé", app="demo")
        work.move(self.db, a["id"], "build")
        work.move(self.db, a["id"], "qa")
        work.milestone(self.db, a["id"], "frozen", sha=sha, actor="a1")
        work.milestone(self.db, a["id"], "verdict", sha=sha, actor="rev1", verdict="ok")
        work.milestone(self.db, a["id"], "frozen", sha=sha, actor="a1")
        snap = progress.snapshot(self.db, self.cfg, book=self._book())
        lot = {l["title"]: l for l in snap["lots"]}["gel renouvelé"]
        self.assertEqual(lot["state"], "approved")
        self.assertEqual(lot["waiting_for"]["what"], "merge")
        # un gel sur une autre tête rouvre bien la revue
        work.milestone(self.db, a["id"], "frozen", sha="b" * 40, actor="a1")
        snap = progress.snapshot(self.db, self.cfg, book=self._book())
        lot = {l["title"]: l for l in snap["lots"]}["gel renouvelé"]
        self.assertEqual(lot["state"], "review")
        self.assertEqual(lot["waiting_for"]["what"], "verdict")

    def test_ci_suite_du_veilleur(self):
        """#1476 : la vue ne compte que les suites complètes CONCLUES des
        contextes qui les garantissent — release/**, integration/** (source ou
        cible) et exécution manuelle ; une poussée sur main est écartée (voie
        documentation seule possible, L161a). Annulée, remplacée, en cours et
        hors fenêtre sont aussi écartées ; la borne de lecture est par
        dépôt."""
        def run(run_id, branch, base, status, *, repo="manaty/ameesh", minutes=10,
                age_s=0, event="push"):
            self.db.execute(
                "INSERT INTO ci_runs (repo, run_id, head_sha, branch, event, base,"
                " status, url, first_seen_at, concluded_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, 'u',"
                " to_timestamp(extract(epoch from now()) - %s),"
                " CASE WHEN %s = 'running' THEN NULL"
                "      ELSE to_timestamp(extract(epoch from now()) - %s) END)",
                (repo, run_id, "0" * 40, branch, event, base, status,
                 float(minutes * 60 + age_s), status, float(age_s)))
        run(1, "main", "main", "success", minutes=10)          # voie doc possible
        run(2, "release/1.11.0", "main", "failure", minutes=12)
        run(3, "integration/1204", "main", "success", minutes=14)
        run(4, "lot/1476", "integration/1204", "success", minutes=16, event="pull_request")
        run(5, "lot/999", "main", "success", minutes=18, event="pull_request")  # impactés
        run(6, "main", "main", "success", minutes=11, event="workflow_dispatch")
        run(7, "main", "main", "cancelled", minutes=5)
        run(8, "main", "main", "superseded", minutes=5)
        run(9, "release/1.11.0", "main", "running", minutes=5)
        run(10, "main", "main", "success", minutes=10, age_s=8 * 86400)
        run(11, "release/2.0", "main", "success", minutes=10, repo="manaty/nexlink")
        run(12, "integration/9", "main", "success", minutes=11, repo="manaty/nexlink")

        st = storage.of(self.db).progress
        # l'instant de référence vient de la base : aucune horloge locale
        depuis = self.db.query("SELECT extract(epoch from now())::float8 AS epoch")[0]["epoch"] \
            - 7 * 86400
        rows = st.ci_suite(since_ts=depuis, limit=100)
        self.assertEqual([r["run_id"] for r in rows], [12, 11, 6, 4, 3, 2])
        attendu = {2: 720.0, 3: 840.0, 4: 960.0, 6: 660.0, 11: 600.0, 12: 660.0}
        for row in rows:
            self.assertAlmostEqual(row["duration_s"], attendu[row["run_id"]], delta=2)
        # la borne est par dépôt : le dépôt actif ne chasse pas l'autre
        self.assertEqual([r["run_id"] for r in st.ci_suite(since_ts=depuis, limit=1)],
                         [12, 6])

        snap = progress.snapshot(self.db, self.cfg, book=self._book())
        self.assertEqual([r["repo"] for r in snap["ci_suite"]["repos"]],
                         ["manaty/ameesh", "manaty/nexlink"])
        ameesh = snap["ci_suite"]["repos"][0]
        self.assertEqual(ameesh["runs"], 4)
        self.assertEqual(ameesh["median_s"], 780.0)          # (720 + 840) / 2
        self.assertEqual(ameesh["priority_runs"], 3)         # release/**, integration/**, cible
        self.assertEqual(ameesh["priority_median_s"], 840.0)  # la main dispatchée n'en est pas
        self.assertEqual(snap["ci_suite"]["repos"][1]["runs"], 2)

    def test_vide(self):
        snap = progress.snapshot(self.db, self.cfg, book=self._book())
        self.assertEqual((snap["lots"], snap["agents"], snap["actions"]), ([], [], []))
        self.assertEqual(snap["budget"]["spend"]["24h"]["total_usd"], 0.0)
        self.assertEqual(snap["ci_suite"]["repos"], [])
        st = storage.of(self.db).progress
        self.assertEqual(st.lot_events([]), [])
        self.assertEqual(st.costs(since_ts=0, agents=[]), [])
        self.assertEqual(st.ci_suite(since_ts=0, limit=10), [])

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
        self.assertIn("unreadable --since", bad.stderr)


if __name__ == "__main__":
    unittest.main()
