# SPDX-License-Identifier: AGPL-3.0-only
"""L62 : vue par projet (`ameesh projects`), colonne projet de `ameesh list`,
et la même vue en tête de `ameesh progress` (texte, JSON, page HTML).

Tests unitaires de `projects.build` (sans base), puis sur Postgres réel :
une seule requête agrégée, lot en cours, non-lus, dépense 24 h, forfait ou
token, lots sans agent et projet sans agent actif.
"""
from __future__ import annotations

import json
import os
import unittest

from ameesh import cost, mail, progress, projects, registry, storage, work

from .support import PgTestCase

NOW = 1_790_000_000.0


def agent(name, team=None, chantier=None, status="idle", lease_live=False, **extra):
    row = {"name": name, "team": team, "chantier": chantier, "status": status,
           "lease_live": lease_live, "harness": "claude", "mode": "execute"}
    row.update(extra)
    return row


class BuildTest(unittest.TestCase):
    """`projects.build` : regroupement, états, lots sans agent, signalements."""

    def test_regroupement_et_signalements(self):
        board = {
            "agents": [
                agent("a1", team="alpha", status="running", lease_live=True,
                      turn_started_ts=NOW - 300, credential_mode="subscription",
                      assigned_lot_id=7, assigned_lot_title="Lot sept", assigned_lot_state="build",
                      open_lots=2, unread=3, usd_24h=1.5),
                agent("a2", chantier="alpha", status="blocked", status_text="budget horaire",
                      credential_mode="api-key", usd_24h=2.25),
                agent("b1", team="beta", status="stopped", stop_reason="manuel",
                      status_text="arrêté à la main"),
                agent("c1", team="gamma", status="stopped", stop_reason="erreur"),
            ],
            "lots": [
                {"id": 7, "title": "Lot sept", "state": "build", "app": "alpha",
                 "assignee": "a1", "updated_ts": NOW, "total": 4},
                {"id": 8, "title": "Lot huit", "state": "intake", "app": "beta",
                 "assignee": "b1", "updated_ts": NOW, "total": 4},
                {"id": 9, "title": "Lot neuf", "state": "intake", "workstream": "beta",
                 "updated_ts": NOW, "total": 4},
                {"id": 10, "title": "Lot dix", "state": "qa", "assignee": "fantome",
                 "package_team": "delta", "updated_ts": NOW, "total": 4},
            ],
        }
        view = projects.build(board, now=NOW, paid_harnesses=("deepseek",))
        self.assertEqual(view["schema"], "ameesh-projects/1")
        by = {p["name"]: p for p in view["projects"]}
        self.assertEqual([p["name"] for p in view["projects"]][:1], ["alpha"])

        alpha = by["alpha"]
        self.assertTrue(alpha["active"])
        self.assertEqual([a["name"] for a in alpha["agents"]], ["a1", "a2"])  # au travail d'abord
        a1, a2 = alpha["agents"]
        self.assertEqual((a1["state"], a1["payment"]), ("working", "plan"))
        self.assertEqual(a1["lot"], {"id": 7, "title": "Lot sept", "state": "build",
                                     "source": "assigned"})
        self.assertEqual(a1["since_ts"], NOW - 300)
        self.assertEqual((a2["state"], a2["reason"], a2["payment"]),
                         ("paused", "budget horaire", "token"))
        self.assertEqual((alpha["unread"], alpha["usd_24h"], alpha["usd_24h_token"]),
                         (3, 3.75, 2.25))
        self.assertEqual(alpha["lots_without_agent"], [])
        self.assertEqual(alpha["warnings"], [])

        beta = by["beta"]
        self.assertTrue(beta["active"])                  # du travail ouvert…
        self.assertEqual(beta["agents_active"], 0)       # … et personne pour le faire
        self.assertEqual(beta["agents"][0]["reason"], "manuel — arrêté à la main")
        self.assertEqual({(l["id"], l["why"]) for l in beta["lots_without_agent"]},
                         {(8, "stopped"), (9, "unassigned")})
        self.assertIn("aucun agent actif pour 2 lot(s) ouvert(s)", beta["warnings"])

        delta = by["delta"]                              # projet de la fiche du plan
        self.assertEqual(delta["lots_without_agent"][0]["why"], "unknown")

        self.assertFalse(by["gamma"]["active"])          # arrêté, rien d'ouvert
        self.assertEqual(view["projects"][-1]["name"], "gamma")

        text = projects.format_text(view, 100)
        self.assertIn("alpha — 2 agent(s) : 1 au travail, 1 en pause", text)
        self.assertIn("#7 Lot sept (+1)", text)
        self.assertIn("raison : budget horaire", text)
        self.assertIn("sans agent : #9 intake — Lot neuf (non assigné)", text)
        self.assertIn("assigné à b1, arrêté", text)
        self.assertIn("! aucun agent actif pour 2 lot(s) ouvert(s)", text)
        self.assertIn("à l'arrêt (sans agent actif ni lot ouvert) : gamma (c1)", text)
        self.assertIn("gamma — 1 agent(s)", projects.format_text(view, 100, show_inactive=True))

        # terminal étroit : deux lignes par agent, rien ne dépasse
        narrow = projects.format_text(view, 40, show_inactive=True)
        self.assertTrue(all(len(l) <= 40 for l in narrow.splitlines()), narrow)
        self.assertIn("a1 — au travail 5m · forfait ·", narrow)
        self.assertIn("    #7 Lot sept (+1)", narrow)

        only = projects.build(board, now=NOW, project="beta", paid_harnesses=())
        self.assertEqual([p["name"] for p in only["projects"]], ["beta"])

    def test_paiement(self):
        self.assertEqual(projects.payment({"credential_mode": "subscription",
                                           "harness": "deepseek"}, ("deepseek",)), "plan")
        self.assertEqual(projects.payment({"credential_mode": "api-key"}, ()), "token")
        self.assertEqual(projects.payment({"harness": "deepseek"}, ("deepseek",)), "token")
        self.assertEqual(projects.payment({"harness": "codex"}, ("deepseek",)), "plan")
        self.assertIsNone(projects.payment({"harness": "codex"}, None))   # descripteurs illisibles
        self.assertIsNone(projects.payment({}, ("deepseek",)))

    def test_projet_d_un_lot_et_d_un_agent(self):
        self.assertEqual(projects.agent_project({"team": " ", "chantier": "c"}), "c")
        self.assertIsNone(projects.agent_project({"team": "", "chantier": None}))
        self.assertEqual(projects.lot_project({"assignee": "a"}, {"a": "p"}), "p")
        self.assertEqual(projects.lot_project({"app": "x", "workstream": "y"}), "x")

    def test_sans_projet_et_tronque(self):
        board = {"agents": [agent("solo", status="running", lease_live=True,
                                  status_text="tour messages")],
                 "lots": [{"id": 1, "title": "t", "state": "intake", "updated_ts": NOW,
                           "total": 9}]}
        view = projects.build(board, now=NOW, max_lots=1)
        self.assertEqual(view["projects"][0]["name"], None)
        self.assertEqual(view["truncated"], {"lots": {"shown": 1, "total": 9, "limit": 1}})
        text = projects.format_text(view, 100)
        self.assertIn("(sans projet) — 1 agent(s)", text)
        self.assertIn("tour : tour messages", text)
        self.assertIn("TRONQUÉ : 1 lots ouverts lus sur 9", text)


class ProjectsPgTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending RESTART IDENTITY CASCADE")

    def _seed(self):
        for name, team, harness, mode in (("w1", "alpha", "codex", "subscription"),
                                          ("w2", "alpha", "deepseek", "api-key"),
                                          ("s1", "beta", "claude", "subscription")):
            registry.upsert(self.db, name, chantier=team, harness=harness, host=self.cfg.host)
            self.db.execute("UPDATE agent_registry SET team = %s, credential_mode = %s,"
                            " responsible = 'human:test' WHERE name = %s", (team, mode, name))
        lot = work.add(self.db, title="Écrire la vue par projet", app="alpha", assignee="w1")
        work.move(self.db, lot["id"], "build")
        other = work.add(self.db, title="Deuxième lot", app="alpha", assignee="w1")
        orphan = work.add(self.db, title="Lot oublié", app="beta", assignee="s1")
        loose = work.add(self.db, title="Lot libre", app="beta")
        done = work.add(self.db, title="Lot fini", app="alpha", assignee="w2")
        work.close(self.db, done["id"], abandoned=True)
        lease = registry.claim(self.db, "w1", "runner-test", 60)
        self.assertIsNotNone(lease)
        registry.begin_turn(self.db, "w1", "runner-test", int(lease["lease_epoch"]), "tour")
        registry.set_status(self.db, "s1", "stopped", status_text="arrêté à la main",
                            stop_reason="manuel")
        mail.send(self.db, "w2", "w1", "bonjour")
        mail.send(self.db, "w2", "w1", "encore")
        tc = storage.of(self.db).turn_costs
        for name, harness, usd in (("w2", "deepseek", 0.5), ("w2", "deepseek", 0.25),
                                   ("w1", "codex", 3.0)):
            tc.insert(agent=name, harness=harness, turn="t", model=None, session=None,
                      usd=usd, input_tokens=1, cached_input_tokens=0, output_tokens=1,
                      cum_usd=None, cum_input_tokens=None, cum_cached_input_tokens=None,
                      cum_output_tokens=None)
        self.db.execute("UPDATE turn_costs SET recorded_at = now() - interval '30 hours'"
                        " WHERE usd = 0.25")
        return lot, other, orphan, loose

    def test_vue_par_projet(self):
        lot, other, orphan, loose = self._seed()
        calls = []
        real = self.db.query

        def counting(sql, params=()):
            calls.append(sql)
            return real(sql, params)

        self.db.query = counting
        try:
            view = projects.snapshot(self.db, paid_harnesses=("deepseek",))
        finally:
            del self.db.query
        self.assertEqual(len(calls), 1)                    # un seul aller-retour
        by = {p["name"]: p for p in view["projects"]}
        self.assertEqual([p["name"] for p in view["projects"]], ["alpha", "beta"])
        agents = {a["name"]: a for a in by["alpha"]["agents"]}
        w1, w2 = agents["w1"], agents["w2"]
        self.assertEqual((w1["state"], w1["payment"], w1["unread"]), ("working", "plan", 2))
        self.assertEqual(w1["lot"]["source"], "assigned")
        self.assertEqual(w1["lot"]["id"], other["id"])     # le plus récemment modifié
        self.assertEqual(w1["open_lots"], 2)
        self.assertAlmostEqual(w1["usd_24h"], 3.0)
        self.assertEqual((w2["state"], w2["payment"], w2["open_lots"]), ("idle", "token", 0))
        self.assertAlmostEqual(w2["usd_24h"], 0.5)          # la dépense de 30 h sort
        self.assertEqual(by["alpha"]["lots_open"], 2)       # le lot fermé ne compte pas
        self.assertAlmostEqual(by["alpha"]["usd_24h_token"], 0.5)
        beta = by["beta"]
        self.assertEqual(beta["agents"][0]["reason"], "manuel — arrêté à la main")
        self.assertEqual({(l["id"], l["why"]) for l in beta["lots_without_agent"]},
                         {(orphan["id"], "stopped"), (loose["id"], "unassigned")})
        self.assertEqual(beta["agents_active"], 0)
        self.assertTrue(beta["warnings"])
        json.dumps(view)

        # lot de la session : prioritaire sur le lot assigné
        self.db.execute("UPDATE agent_registry SET session_work_item = %s WHERE name = 'w1'",
                        (str(lot["id"]),))
        view = projects.snapshot(self.db, paid_harnesses=())
        w1 = next(a for p in view["projects"] for a in p["agents"] if a["name"] == "w1")
        self.assertEqual((w1["lot"]["id"], w1["lot"]["source"]), (lot["id"], "session"))
        self.assertEqual(w1["lot"]["title"], "Écrire la vue par projet")

    def test_cli_projects_list_et_progress(self):
        self._seed()
        out = self.mesh("projects", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        view = json.loads(out.stdout)
        self.assertEqual(view["schema"], "ameesh-projects/1")
        self.assertEqual({p["name"] for p in view["projects"]}, {"alpha", "beta"})

        out = self.mesh("projects")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("alpha — 2 agent(s) : 1 au travail, 1 au repos", out.stdout)
        self.assertIn("Lot libre", out.stdout)
        self.assertIn("non assigné", out.stdout)

        out = self.mesh("projects", "--project", "beta")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("alpha", out.stdout)

        # `ameesh list` : colonne PROJET, regroupé par projet
        out = self.mesh("list")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertTrue(lines[0].startswith("NOM"))
        self.assertIn("PROJET", lines[0])
        names = [l.split()[0] for l in lines[1:4]]
        projs = [l.split()[1] for l in lines[1:4]]
        self.assertEqual(projs, ["alpha", "alpha", "beta"])
        self.assertEqual(names[2], "s1")
        out = self.mesh("list", "--json")
        self.assertEqual({r["name"]: r["project"] for r in json.loads(out.stdout)},
                         {"w1": "alpha", "w2": "alpha", "s1": "beta"})

        # `ameesh progress` : la vue en tête (texte, JSON, page)
        out = self.mesh("progress")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertLess(out.stdout.index("PROJETS (2 en cours)"), out.stdout.index("LOTS ("))
        snap = json.loads(self.mesh("progress", "--json").stdout)
        self.assertEqual([p["name"] for p in snap["projects"]], ["alpha", "beta"])
        snap = json.loads(self.mesh("progress", "--json", "--project", "beta").stdout)
        self.assertEqual([p["name"] for p in snap["projects"]], ["beta"])
        page = os.path.join(self.tmp, "p.html")
        out = self.mesh("progress", "--html", page)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(page, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn('id="projects"', html)
        self.assertIn('"projects"', progress.render_html(snap))

    def test_progress_snapshot_porte_les_projets(self):
        self._seed()
        book = cost.CostBook(state_dir=self.state, codex_sessions=os.path.join(self.tmp, "codex"),
                             db=self.db)
        snap = progress.snapshot(self.db, self.cfg, book=book)
        self.assertEqual({p["name"] for p in snap["projects"]}, {"alpha", "beta"})
        text = progress.format_text(snap, 100)
        self.assertIn("PROJETS (2 en cours)", text)
        self.assertIn("sans agent : #", text)


if __name__ == "__main__":
    unittest.main()
