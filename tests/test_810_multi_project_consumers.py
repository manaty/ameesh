# SPDX-License-Identifier: AGPL-3.0-only
"""L243-3 (#810): consumers of `projects`, from "equal to the team" to
"member of the agent's projects".

An Agent card's `projects:` list (L243-1, #435, migration 0388) is read by:

* the pick-up predicates (`backlog.fits`, `backlog.lot_fits`) and the
  orchestrators told about a take (`backlog._team_orchestrators`);
* the alert briefs (`alert_briefs._of_teams`, `sous_utilisation`);
* `progress` (`%s = any(r.projects)`) and `roadmap --project`;
* the display: `ameesh projects` lists the agent in every one of its project
  groups, its cost only in the primary one (the others marked `(+)`), and
  `ameesh list` shows `ameesh+1`.

Until L243-4 (#811) opens a work directory per lot project, the AUTOMATIC
pick-up stays on the agent's primary project; a lot of another project only
comes through an explicit assignment. The threads and the mail keep the
primary project (`projects.agent_project`, `fil.agent_project`,
`mailbox.PROJECT_SQL`) — not retested here.
"""
from __future__ import annotations

import unittest

from ameesh import alert_briefs, backlog, projects, registry, roadmap, storage, sous_utilisation, work

from .support import PgTestCase
from .test_l119_backlog import HEURE, _Base

NOW = 1_790_000_000.0


def agent(name, team=None, projects=None, **extra):
    row = {"name": name, "team": team, "projects": projects, "status": "idle",
           "lease_live": False, "harness": "claude", "mode": "execute"}
    row.update(extra)
    return row


class ProjetsDeLAgentTest(unittest.TestCase):
    """`projects.agent_projects` : la liste déclarée, sinon l'équipe, sinon le
    `workstream` ; `agent_project` reste le projet primaire (fils et courrier)."""

    def test_liste_declaree_ou_repli(self):
        self.assertEqual(projects.agent_projects(
            {"projects": ["ameesh", "nexlink"], "team": "ameesh"}), ["ameesh", "nexlink"])
        # une fiche qui ne déclare aucun projet n'en a aucun
        self.assertEqual(projects.agent_projects(
            {"projects": [], "team": "ameesh", "workstream": "web"}), [])
        # pas de `projects` : l'équipe, sinon le workstream, sinon rien
        self.assertEqual(projects.agent_projects(
            {"projects": None, "team": "ameesh", "workstream": "web"}), ["ameesh"])
        self.assertEqual(projects.agent_projects({"team": "", "workstream": "web"}), ["web"])
        self.assertEqual(projects.agent_projects({"chantier": "web"}), ["web"])
        self.assertEqual(projects.agent_projects({}), [])
        # un lecteur qui rend le tableau textuel
        self.assertEqual(projects.agent_projects({"projects": "{ameesh,nexlink}"}),
                         ["ameesh", "nexlink"])

    def test_etiquette_du_projet_primaire(self):
        self.assertEqual(projects.agent_project_label({"team": "ameesh"}), "ameesh")
        self.assertEqual(projects.agent_project_label(
            {"team": "ameesh", "projects": ["ameesh", "nexlink", "web"]}), "ameesh+2")
        self.assertIsNone(projects.agent_project_label({}))
        # le projet primaire lu par les fils et le courrier ne change pas
        self.assertEqual(projects.agent_project({"projects": ["a", "b"], "team": "a"}), "a")


class CorrespondanceTest(unittest.TestCase):
    """Les prédicats de prise et les orchestrateurs : appartenance, pas égalité."""

    def test_fits_par_appartenance(self):
        multi = {"team": "ameesh", "projects": ["ameesh", "nexlink"],
                 "capabilities": ["read"]}
        self.assertTrue(backlog.fits({"team": "nexlink", "required_capabilities": ["read"]},
                                     multi))
        self.assertTrue(backlog.fits({"team": "ameesh"}, multi))
        self.assertFalse(backlog.fits({"team": "web"}, multi))
        self.assertFalse(backlog.fits({"team": "nexlink", "required_capabilities": ["propose"]},
                                      multi))
        # sans `projects` déclaré, l'équipe reste le seul projet
        self.assertTrue(backlog.fits({"team": "ameesh"}, {"team": "ameesh"}))
        self.assertFalse(backlog.fits({"team": "nexlink"}, {"team": "ameesh"}))
        # un élément sans équipe est pris par tous
        self.assertTrue(backlog.fits({}, {}))

    def test_lot_fits_par_appartenance(self):
        multi = {"team": "ameesh", "projects": ["ameesh", "nexlink"], "harness": "deepseek"}
        self.assertTrue(backlog.lot_fits({"lot_team": "nexlink"}, multi))
        self.assertTrue(backlog.lot_fits(
            {"lot_team": "nexlink", "required_capabilities": ["harness:deepseek"]}, multi))
        self.assertFalse(backlog.lot_fits(
            {"lot_team": "nexlink", "required_capabilities": ["harness:claude"]}, multi))
        self.assertFalse(backlog.lot_fits({"lot_team": "web"}, multi))
        self.assertFalse(backlog.lot_fits({"lot_team": ""}, multi))

    def test_orchestrateurs_par_appartenance(self):
        listing = [{"name": "orch", "team": "ameesh", "projects": ["ameesh", "nexlink"]},
                   {"name": "web", "team": "web", "projects": None},
                   {"name": "sans", "team": None}]
        names = ["orch", "web", "sans"]
        self.assertEqual(backlog._team_orchestrators(None, None, listing, "nexlink",
                                                     orchestras=names), ["orch"])
        self.assertEqual(backlog._team_orchestrators(None, None, listing, "web",
                                                     orchestras=names), ["web"])
        # aucun orchestrateur du projet : tous, comme avant (repli, trié)
        self.assertEqual(backlog._team_orchestrators(None, None, listing, "zorg",
                                                     orchestras=names), sorted(names))

    def test_briefs_par_appartenance(self):
        by_name = {"orch": {"team": "ameesh", "projects": ["ameesh", "nexlink"]},
                   "web": {"team": "web", "projects": None},
                   "sans": {"team": None}}
        self.assertEqual(alert_briefs._of_teams(["orch", "web", "sans"], by_name, {"nexlink"}),
                         ["orch", "sans"])
        self.assertEqual(alert_briefs._of_teams(["orch", "web", "sans"], by_name, {"web"}),
                         ["web", "sans"])
        self.assertEqual(alert_briefs._of_teams(["orch", "web", "sans"], by_name, set()),
                         ["orch", "web", "sans"])

    def test_briefs_des_agents_au_repos(self):
        alert = {"type": "idle_capacity", "threshold": 1800,
                 "agents": ["a1", "a2"], "lots": [],
                 "orchestrators": ["orch"], "designers": []}
        listing = [{"name": "orch", "team": "ameesh", "projects": ["ameesh", "nexlink"]},
                   {"name": "a1", "team": "nexlink", "projects": ["nexlink"]},
                   {"name": "a2", "team": "web", "projects": ["web"]}]
        out = sous_utilisation.orchestrator_briefs(alert, listing)
        self.assertIn("a1", out["orch"])
        self.assertNotIn("a2", out["orch"])


class AffichageTest(unittest.TestCase):
    """`ameesh projects` : un agent dans chaque groupe, dépense au primaire."""

    def test_agent_dans_chaque_groupe(self):
        board = {"agents": [agent("m1", team="ameesh", projects=["ameesh", "nexlink"],
                                  credential_mode="api-key", usd_24h=2.0, unread=1),
                            agent("m2", team="nexlink", credential_mode="api-key",
                                  usd_24h=0.5)],
                 "lots": []}
        view = projects.build(board, now=NOW, paid_harnesses=())
        by = {p["name"]: p for p in view["projects"]}
        self.assertEqual(sorted(by), ["ameesh", "nexlink"])
        self.assertEqual([a["name"] for a in by["ameesh"]["agents"]], ["m1"])
        self.assertEqual([a["name"] for a in by["nexlink"]["agents"]], ["m1", "m2"])
        primary = by["ameesh"]["agents"][0]
        other = by["nexlink"]["agents"][0]
        self.assertTrue(primary["primary"])
        self.assertFalse(other["primary"])
        self.assertEqual(other["projects"], ["ameesh", "nexlink"])
        # la dépense n'est comptée que dans le projet primaire
        self.assertAlmostEqual(by["ameesh"]["usd_24h"], 2.0)
        self.assertAlmostEqual(by["nexlink"]["usd_24h"], 0.5)
        text = projects.format_text(view, 100)
        self.assertEqual(text.count("m1 (+)"), 1)
        # le projet primaire liste l'agent sans marque
        self.assertRegex(text, r"(?m)^\s+m1\s+idle\b")
        self.assertNotIn("m2 (+)", text)
        # le total d'agents ne compte pas deux fois le même agent
        self.assertIn("2 agent(s)", text)


class _Multi(PgTestCase):
    """Un schéma migré et des agents à plusieurs projets."""

    def agent(self, name, team=None, projects=None, harness="deepseek", *,
              status="idle", since_s=2 * HEURE):
        registry.upsert(self.db, name, chantier=team, harness=harness, host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = %s, lease_owner = %s,"
            " lease_expires_at = now() + interval '1 hour', responsible = 'human:test',"
            " team = %s, projects = %s::text[],"
            " status_since = now() - make_interval(secs => %s),"
            " last_turn_at = now() - make_interval(secs => %s) WHERE name = %s",
            (status, "runner-%s@h" % name, team,
             "{%s}" % ",".join(projects) if projects is not None else None,
             since_s, since_s, name))


class LectureBaseTest(_Multi):
    """La base rend `projects` : vue par projet et filtre par projet."""

    def test_vue_par_projet_groupe_et_depense(self):
        self.agent("m1", team="ameesh", projects=["ameesh", "nexlink"])
        self.agent("m2", team="nexlink", projects=["nexlink"])
        storage.of(self.db).turn_costs.insert(
            agent="m1", harness="deepseek", turn="t", model=None, session=None, usd=2.0,
            input_tokens=1, cached_input_tokens=0, output_tokens=1, cum_usd=None,
            cum_input_tokens=None, cum_cached_input_tokens=None, cum_output_tokens=None)
        view = projects.snapshot(self.db, paid_harnesses=())
        by = {p["name"]: p for p in view["projects"]}
        self.assertEqual([a["name"] for a in by["ameesh"]["agents"]], ["m1"])
        self.assertEqual([a["name"] for a in by["nexlink"]["agents"]], ["m1", "m2"])
        self.assertAlmostEqual(by["ameesh"]["usd_24h"], 2.0)
        self.assertAlmostEqual(by["nexlink"]["usd_24h"], 0.0)

    def test_progress_filtre_par_appartenance(self):
        self.agent("m1", team="ameesh", projects=["ameesh", "nexlink"])
        self.agent("web", team="web", projects=["web"])
        self.agent("legacy", team=None)
        self.db.execute("UPDATE agent_registry SET workstream = 'nexlink' WHERE name = 'legacy'")
        # `projects` déclaré fait foi : l'équipe ne compte plus à sa place
        self.agent("ecart", team="ameesh", projects=["web"])
        progress = storage.of(self.db).progress
        self.assertEqual({r["name"] for r in progress.agents("nexlink")}, {"m1", "legacy"})
        self.assertEqual({r["name"] for r in progress.agents("web")}, {"web", "ecart"})
        self.assertNotIn("ecart", {r["name"] for r in progress.agents("ameesh")})
        self.assertEqual({r["name"] for r in progress.agents(None)},
                         {"m1", "web", "legacy", "ecart"})

    def test_roadmap_who_suit_les_projets(self):
        self.agent("m1", team="ameesh", projects=["ameesh", "nexlink"])
        view = roadmap.who(self.db, project="nexlink")
        self.assertEqual([p["name"] for p in view["projects"]], ["nexlink"])
        self.assertEqual([a["name"] for a in view["projects"][0]["agents"]], ["m1"])


class PriseMultiProjetsTest(_Base):
    """L243-4 : la prise AUTOMATIQUE reste au projet primaire."""

    def lot(self, title, *, app="ameesh", age_s=HEURE, **kw):
        item = work.add(self.db, title=title, app=app, **kw)
        self.db.execute("UPDATE work_items SET created_at = created_at -"
                        " make_interval(secs => %s) WHERE id = %s", (age_s, item["id"]))
        return item

    def take(self, **kw):
        kw.setdefault("orchestras", [])
        return super().take(**kw)

    def multi(self, name="m1", projects=("ameesh", "nexlink")):
        self.agent(name, team=projects[0])
        self.db.execute("UPDATE agent_registry SET projects = %s::text[] WHERE name = %s",
                        ("{%s}" % ",".join(projects), name))

    def test_lot_d_un_autre_projet_attend_une_attribution(self):
        self.multi()
        primaire = self.lot("lot ameesh", app="ameesh")
        autre = self.lot("lot nexlink", app="nexlink", priority=1)
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("m1", primaire["id"])])
        self.assertIsNone(work.get(self.db, autre["id"])["assignee"])
        # le lot convient pourtant à l'agent : l'attribution explicite le donne
        self.assertTrue(backlog.lot_fits({"lot_team": "nexlink", "id": autre["id"]},
                                         {"team": "ameesh",
                                          "projects": ["ameesh", "nexlink"]}))
        work.assign(self.db, autre["id"], "m1")
        self.assertEqual(work.get(self.db, autre["id"])["assignee"], "m1")

    def test_amelioration_d_un_autre_projet_attend_aussi(self):
        self.multi()
        self.element("dette nexlink", 90, team="nexlink")
        self.assertEqual(self.take(), [])
        libre = self.element("dette sans équipe", 80)
        out = self.take()
        self.assertEqual([t["item"]["id"] for t in out], [libre["id"]])


class ListeTexteTest(_Multi):
    """`ameesh list` : la colonne projet dit `ameesh+1` (L243)."""

    def test_colonne_projet(self):
        self.agent("m1", team="ameesh", projects=["ameesh", "nexlink"])
        out = self.mesh("list")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ameesh+1", out.stdout)
        self.assertNotIn("nexlink+0", out.stdout)


if __name__ == "__main__":
    unittest.main()
