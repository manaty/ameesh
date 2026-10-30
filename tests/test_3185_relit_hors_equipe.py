# SPDX-License-Identifier: AGPL-3.0-only
"""#3185 — relecteurs déclarés hors équipe (`relit:`).

Nexlink n'avait aucune seconde famille éligible : ses agents claude tournent en
DeepSeek et son pilote (claude) est exclu ; un lot nexlink écrit par deepseek à
deux avis attendait sans fin. Une fiche Agent déclare désormais `relit:
[<projet>, …]` (projets déclarés seulement, sinon `canon check` en erreur) ;
`canon sync` l'écrit au registre (0479) et la désignation admet un agent dont
l'équipe est le projet du lot OU que `relit` déclare. Famille réelle et
exclusions inchangées ; le pilote n'est jamais candidat.
"""
from __future__ import annotations

import unittest

from ameesh import canon, registry
from ameesh import review_designation as rd
from ameesh import review_designation_activation as rda

from .test_435_agent_projects import orchestre
from .test_canon import _CanonDbCase, _TmpMixin, codes, write

FAMILIES = ("claude", "codex", "deepseek")

#: #4803 : la déclaration de projet du canon, hors fiches et paquets.
FEDERATION = """\
id: acme
root: acme
members: []
review_policies:
  risk_classes:
    default: normal
    projects:
      acme-data:
        default: light
"""


def row(name, *, team="nexlink", harness="deepseek", session="", relit=None):
    return {"name": name, "team": team, "harness": harness, "session_harness": session,
            "status": "idle", "mode": "execute", "status_text": "", "relit": relit}


#: le registre nexlink du 23/10 : codex, claude* en DeepSeek, pilote claude
NEXLINK = [row("codex1", harness="codex", session="codex"),
           row("claude1", harness="claude", session="deepseek"),
           row("deepseek1", harness="deepseek", session="deepseek"),
           row("orchestrateur", harness="claude", session="claude")]
DEV4 = row("ameesh-dev4", team="ameesh", harness="claude", session="claude",
           relit=["nexlink"])
AUTHOR = [{"name": "deepseek2", "family": "deepseek"}]


def pick(listing, *, pilot="orchestrateur", project="nexlink"):
    candidates = rda._candidates(listing, loads={}, project=project,
                                 families=FAMILIES, pilot=pilot)
    return rd.select(families=FAMILIES, required=2, authors=AUTHOR,
                     candidates=candidates)


class DesignationTest(unittest.TestCase):

    def test_declared_reviewer_gives_nexlink_its_second_family(self):
        picked = pick(NEXLINK + [DEV4])
        self.assertEqual(picked["state"], rd.DESIGNATED, picked)
        self.assertEqual(sorted((r["name"], r["family"]) for r in picked["reviewers"]),
                         [("ameesh-dev4", "claude"), ("codex1", "codex")])

    def test_without_declaration_the_lot_still_waits(self):
        undeclared = dict(DEV4, relit=None)
        picked = pick(NEXLINK + [undeclared])
        self.assertNotEqual(picked["state"], rd.DESIGNATED, picked)
        self.assertEqual(picked["missing"], 1, picked)
        self.assertNotIn("ameesh-dev4",
                         [c.name for c in rd.candidates_from_registry(
                             NEXLINK + [undeclared], project="nexlink",
                             families=FAMILIES)])

    def test_relit_only_opens_the_declared_project(self):
        listing = NEXLINK + [DEV4]
        self.assertNotIn("ameesh-dev4", [c.name for c in rd.candidates_from_registry(
            listing, project="acme", families=FAMILIES)])
        # its own team is unchanged
        self.assertIn("ameesh-dev4", [c.name for c in rd.candidates_from_registry(
            listing, project="ameesh", families=FAMILIES)])

    def test_family_is_the_real_harness_not_the_declaration(self):
        borrowed = dict(DEV4, session_harness="deepseek")
        picked = pick(NEXLINK + [borrowed])
        self.assertNotEqual(picked["state"], rd.DESIGNATED, picked)

    def test_pilot_is_never_a_candidate(self):
        pilot = row("orchestrateur", harness="claude", session="claude",
                    relit=["nexlink"])
        listing = [r for r in NEXLINK if r["name"] != "orchestrateur"] + [pilot]
        self.assertNotIn("orchestrateur", [c.name for c in rda._candidates(
            listing, loads={}, project="nexlink", families=FAMILIES,
            pilot="orchestrateur")])
        self.assertNotEqual(pick(listing)["state"], rd.DESIGNATED)

    def test_postgres_array_text_is_read(self):
        self.assertEqual(rd.reviewed_projects({"relit": "{nexlink,acme}"}),
                         ["nexlink", "acme"])
        self.assertEqual(rd.reviewed_projects({"relit": None}), [])
        self.assertEqual(rd.reviewed_projects({}), [])


class RelitCardTest(_TmpMixin, unittest.TestCase):

    def check(self, root):
        loaded = canon.load(root, untrusted=True)
        return loaded, canon.validate(loaded)

    def test_declared_project_is_accepted_and_shown(self):
        root = self.example_copy()
        write(root, "agents/orchestre.md",
              orchestre(projects=["acme-api"], relit=["acme-web"]))
        loaded, findings = self.check(root)
        self.assertEqual([f for f in findings if f.severity == canon.ERROR], [])
        self.assertFalse({"agent-relit-unknown", "agent-relit-invalid"} & codes(findings))
        agent = next(a for a in loaded.agents if a.title == "orchestre")
        self.assertEqual(agent.relit, ["acme-web"])
        shown = {a["title"]: a for a in loaded.to_dict()["agents"]}
        self.assertEqual(shown["orchestre"]["relit"], ["acme-web"])
        self.assertIsNone(shown["relecteur"]["relit"])

    def test_unknown_project_is_an_error(self):
        root = self.example_copy()
        write(root, "agents/orchestre.md", orchestre(relit=["acme-web", "zorg"]))
        _loaded, findings = self.check(root)
        found = [f for f in findings if f.code == "agent-relit-unknown"]
        self.assertEqual(len(found), 1, findings)
        self.assertEqual((found[0].severity, found[0].agent), (canon.ERROR, "orchestre"))
        self.assertIn("zorg", found[0].message)
        self.assertNotIn("acme-web", found[0].message)

    def test_project_declared_by_federation_risk_classes_is_accepted(self):
        """#4803 : `risk_classes.projects` déclare le projet au canon, sans
        donner ce projet à un agent. `canon sync` le lit sans base."""
        root = self.example_copy()
        write(root, "federation.yaml", FEDERATION)
        write(root, "agents/orchestre.md",
              orchestre(projects=["acme-api"], relit=["acme-data"]))
        loaded, findings = self.check(root)
        self.assertFalse({"agent-relit-unknown", "agent-relit-invalid"} & codes(findings))
        # même verdict avec les projets connus de la base (`canon check`) :
        # le projet du canon est accepté, un projet inconnu du canon ne l'est pas
        found = [f for f in canon.validate(loaded, known_projects={"acme-data"})
                 if f.code == "agent-relit-unknown"]
        self.assertEqual(found, [])

    def test_a_database_known_project_does_not_declare_relit(self):
        """#4803 : un projet connu de la seule base (lot) ne suffit pas — le
        `canon sync` sans base le refuserait. `canon check` doit refuser aussi."""
        root = self.example_copy()
        write(root, "agents/orchestre.md", orchestre(relit=["acme-db"]))
        loaded = canon.load(root, untrusted=True)
        found = [f for f in canon.validate(loaded, known_projects={"acme-db"})
                 if f.code == "agent-relit-unknown"]
        self.assertEqual(len(found), 1, found)
        self.assertIn("acme-db", found[0].message)

    def test_a_string_is_not_a_list(self):
        root = self.example_copy()
        write(root, "agents/orchestre.md", orchestre(relit="acme-web, acme-api"))
        loaded, findings = self.check(root)
        self.assertIn("agent-relit-invalid", codes(findings))
        self.assertIsNone(next(a for a in loaded.agents if a.title == "orchestre").relit)


class RelitSyncTest(_CanonDbCase):

    def relit(self, name):
        return self.db.query("SELECT relit FROM agent_registry WHERE name = %s",
                             (name,))[0]["relit"]

    def test_sync_writes_relit_and_the_overview_exposes_it(self):
        write(self.root, "agents/orchestre.md",
              orchestre(team="acme-api", relit=["acme-web"]))
        self.sync()
        self.assertEqual(list(self.relit("orchestre")), ["acme-web"])
        self.assertEqual(self.actions(self.sync())["orchestre"], "inchangé")
        shown = {r["name"]: r for r in registry.overview(self.db)}
        self.assertEqual(list(shown["orchestre"]["relit"]), ["acme-web"])
        self.assertIn("orchestre", [c.name for c in rd.candidates_from_registry(
            registry.overview(self.db), project="acme-web", families=FAMILIES)])
        # the declaration withdrawn: the column is emptied, the row resynced
        write(self.root, "agents/orchestre.md", orchestre(team="acme-api"))
        self.assertNotEqual(self.actions(self.sync())["orchestre"], "inchangé")
        self.assertIsNone(self.relit("orchestre"))


if __name__ == "__main__":
    unittest.main()
