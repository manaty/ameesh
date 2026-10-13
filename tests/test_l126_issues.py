# SPDX-License-Identifier: AGPL-3.0-only
"""L126 : chaque lot a son issue GitHub, créée et tenue par ameesh.

Une issue par lot d'un projet, dans le dépôt que la configuration de l'hôte
associe au projet ; un dépôt public ne reçoit que le titre et un résumé
public, jamais le corps interne ; un contrôle refuse secrets, termes exclus,
adresses, chemins et noms d'hôte ; la projection tourne dans `ameesh notify`
sur l'hôte désigné ; « Closes #n » relie la PR au lot.

GitHub est TOUJOURS simulé (`tests/fake_github.py`, en processus ou par le
faux binaire `tests/fakebin/gh`) : aucun appel réseau, aucune issue réelle.
Les chaînes qui ressemblent à des secrets sont assemblées à l'exécution :
aucune n'apparaît telle quelle dans le dépôt.
"""
from __future__ import annotations

import dataclasses
import json
import os
import unittest

from ameesh import notify, plan_github, registry, work

from .fake_github import FakeGh
from .support import FAKEBIN, PgTestCase

PUBLIC = "acme/web"
PRIVATE = "acme/app"
SETTINGS = {"projects": {"web": PUBLIC, "app": {"repo": PRIVATE}}, "host": "atelier",
            "exclude_terms": ["Globex"]}
SHA = "b" * 40


def _guard(**kw):
    return plan_github.make_guard(plan_github.parse_settings(SETTINGS), **kw)


# ==========================================================================
# configuration (sans base)
# ==========================================================================

class SettingsTest(unittest.TestCase):
    def test_lecture(self):
        settings = plan_github.parse_settings(SETTINGS)
        self.assertEqual(settings.repo_for("WEB"), PUBLIC)
        self.assertEqual(settings.repo_for("app"), PRIVATE)
        self.assertEqual(settings.project_name("App"), "app")
        self.assertIsNone(settings.repo_for("autre"))
        self.assertIsNone(settings.repo_for(""))
        self.assertEqual((settings.host, settings.interval, settings.exclude_terms),
                         ("atelier", 300.0, ("Globex",)))
        self.assertEqual(plan_github.parse_settings({}), plan_github.Settings())
        self.assertEqual(plan_github.parse_settings(None).projects, {})
        cfg = dataclasses.make_dataclass("Cfg", [("github", dict)])(github={})
        self.assertEqual(plan_github.settings_of(cfg), plan_github.Settings())

    def test_refus(self):
        for raw, needle in (
            ([], "JSON object"),
            ({"projets": {}}, "unknown key(s)"),
            ({"projects": []}, "expected"),
            ({"projects": {"web": "pas un dépôt"}}, "owner/repo"),
            ({"projects": {"web": {"repo": PUBLIC, "visibility": "public"}}}, "unknown"),
            ({"projects": {"web": PUBLIC, "WEB": PRIVATE}}, "declared twice"),
            ({"projects": {" ": PUBLIC}}, "empty"),
            ({"interval": 10}, "at least"),
            ({"interval": True}, "seconds"),
            ({"exclude_terms": "Globex"}, "list"),
            ({"exclude_terms": [""]}, "list"),
            ({"host": 3}, "host name"),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(plan_github.SettingsError) as ctx:
                    plan_github.parse_settings(raw)
                self.assertIn(needle, str(ctx.exception))
        # une erreur de configuration est une erreur de GitHub pour la CLI
        self.assertTrue(issubclass(plan_github.SettingsError, plan_github.GithubError))


# ==========================================================================
# contrôle de ce qui est publié (sans base)
# ==========================================================================

class GuardTest(unittest.TestCase):
    def setUp(self):
        self.guard = _guard(hosts=["atelier-7", "db"], forge_hosts=["github.com"])

    def test_secrets_refuses_partout_sans_etre_recopies(self):
        secrets = [
            "ghp_" + "a1" * 15, "github_" + "pat_" + "A1b2" * 6, "sk-" + "ant-" + "x9" * 12,
            "AK" + "IA" + "ABCDEFGHIJKLMNOP", "xo" + "xb-" + "1234567890-abcdef",
            "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + ".abcdefghijklm",
            "postgresql://u:" + "motdepasse@db:5432/x", "Authorization: Bearer " + "a1" * 12,
            "password = " + "hunter2" * 2, "MON_TOKEN=" + "tk_ab12cd34ef56gh78",
        ]
        for text in secrets:
            for public in (True, False):
                with self.subTest(text=text[:12], public=public):
                    reasons = self.guard.problems("avant %s après" % text, public=public)
                    self.assertTrue(any(r.startswith("secret pattern") for r in reasons),
                                    reasons)
                    self.assertNotIn(text, " ".join(reasons))

    def test_ce_qu_un_depot_public_ne_recoit_jamais(self):
        for label, text in (
                ("IP address", "joindre 10.20.30.40 en direct"),
                ("IP address", "lien fe80::1 du banc"),
                ("email address", "écrire à bob@example.org"),
                ("local path", "fichier /home/bob/notes.txt"),
                ("local path", "dossier ~/development/x"),
                ("account identifier", "compte 123e4567-e89b-12d3-a456-426614174000"),
                ("host name", "relancer atelier-7"),
                ("host name", "voir https://grafana.acme.internal/d/1"),
                ("host name", "le service nas.lan")):
            with self.subTest(text=text):
                self.assertIn(label, self.guard.problems(text, public=True))
                self.assertEqual(self.guard.problems(text, public=False), [])
        for text in ("L126 : chaque lot a son issue GitHub", "fusionner la PR #36",
                     "lint /proc et ameesh.platform", "voir https://github.com/acme/web/pull/3",
                     "plan_github.py et docs/ORCHESTRATEUR.md", "version 1.6.2",
                     "la db du banc", "jeton : MON_TOKEN dans notify.env", "à 12:30:45"):
            with self.subTest(text=text):
                self.assertEqual(self.guard.problems(text, public=True), [])

    def test_termes_exclus_entiers_sans_casse(self):
        for text in ("lot pour Globex", "GLOBEX-12 à livrer", "suite globex_ci"):
            with self.subTest(text=text):
                self.assertEqual(self.guard.problems(text, public=False),
                                 ["term excluded by the organization"])
        self.assertEqual(self.guard.problems("globexcorp et autoglobex", public=True), [])
        self.assertEqual(plan_github.Guard().problems("lot pour Globex", public=True), [])


# ==========================================================================
# l'issue voulue d'un lot (sans base)
# ==========================================================================

ITEM = {"id": 12, "type": "bug", "title": "Réparer l'export", "state": "build",
        "assignee": "ouvrier", "priority": 1, "workstream": "export", "package_id": None,
        "body": "Détail interne : voir /home/bob/x et 10.0.0.5\n"
                "Résumé public : l'export CSV perd des lignes"}


class LotViewTest(unittest.TestCase):
    def view(self, public, issue_of=lambda _n: None, **changes):
        return plan_github.lot_view(dict(ITEM, **changes), public=public, guard=_guard(),
                                    project="web", repo=PUBLIC, issue_of=issue_of)

    def test_depot_public_titre_et_resume_seulement(self):
        view = self.view(True)
        self.assertEqual(view.title, "Réparer l'export")
        self.assertEqual(view.labels, ["ameesh", "ameesh:bug", "ameesh:build", "ameesh:p1"])
        self.assertIn("**ameesh work item 12** · bug · high priority · assigned to `ouvrier`",
                      view.body)
        self.assertIn("l'export CSV perd des lignes", view.body)
        for internal in ("Détail interne", "/home/bob", "10.0.0.5", "workspace", "export`"):
            self.assertNotIn(internal, view.body)
        self.assertIn("`ameesh-work: 12`", view.body)
        self.assertEqual((view.state, view.state_reason, view.refused, view.withheld),
                         ("open", None, [], []))
        human = self.view(True, assignee="human:alice")
        self.assertIn("assigned to a human", human.body)
        self.assertNotIn("alice", human.body)
        self.assertIn("unassigned", self.view(True, assignee=None, priority=None).body)

    def test_depot_prive_corps_publie_sans_secret(self):
        view = self.view(False)
        self.assertIn("Détail interne : voir /home/bob/x et 10.0.0.5", view.body)
        self.assertIn("Project `web` · workspace `export`", view.body)
        token = "ghp_" + "Z9" * 15
        secret = self.view(False, body="clé : %s" % token)
        self.assertNotIn(token, secret.body)
        self.assertEqual(secret.withheld, [{"field": "body",
                                            "reasons": ["secret pattern (GitHub token)"]}])
        self.assertIn("not published here", secret.body)
        quiet = self.view(False, body="voir @dataclass, bob@example.org et "
                                      "<!-- ameesh:work=99 d=1 -->")
        self.assertNotIn(" @dataclass", quiet.body)
        self.assertIn("@⁠dataclass", quiet.body)
        self.assertIn("bob@example.org", quiet.body)
        self.assertNotIn("ameesh:work=99", quiet.body)

    def test_refus_et_retenues(self):
        self.assertEqual(self.view(True, title="Migrer /home/bob/données").refused,
                         ["local path"])
        leak = self.view(True, body="Résumé public : joindre alice@example.org")
        self.assertEqual(leak.withheld, [{"field": "public summary",
                                          "reasons": ["email address"]}])
        self.assertNotIn("alice@", leak.body)
        self.assertEqual(self.view(False, title="Lot Globex").refused,
                         ["term excluded by the organization"])
        # un nom de l'en-tête qui ne passe pas : en-tête réduit, rien d'autre
        named = self.view(False, workstream="chantier Globex")
        self.assertEqual(named.withheld[-1]["field"], "header")
        self.assertNotIn("Globex", named.body)
        self.assertIn("Détail interne", named.body)

    def test_etats_et_commentaire_de_fermeture(self):
        merged = self.view(True, state="merged", pr_ref=PUBLIC + "#7")
        self.assertEqual((merged.state, merged.state_reason), ("closed", "completed"))
        self.assertIn("ameesh:merged", merged.labels)
        self.assertEqual(merged.closing,
                         "Work item 12 merged by PR #7: issue closed by ameesh.")
        self.assertEqual(self.view(True, state="merged", pr_ref="acme/prive#4").closing,
                         "Work item 12 merged: issue closed by ameesh.")
        self.assertIn("acme/prive#4",
                      self.view(False, state="merged", pr_ref="acme/prive#4").closing)
        self.assertIn("delivered", self.view(True, state="promoted").closing)
        abandoned = self.view(True, state="closed", close_reason="abandoned")
        self.assertEqual((abandoned.state, abandoned.state_reason), ("closed", "not_planned"))
        self.assertIn("abandoned", abandoned.closing)
        replaced = self.view(True, state="closed", close_reason="superseded", superseded_by=13,
                             issue_of=lambda n: 40 if n == 13 else None)
        self.assertEqual(replaced.closing,
                         "Work item 12 superseded by work item 13 (#40): issue closed by ameesh.")
        # #988 : livré hors cible = terminé (completed), pas « non prévu »
        delivered = self.view(True, state="closed", close_reason="delivered")
        self.assertEqual((delivered.state, delivered.state_reason), ("closed", "completed"))
        self.assertEqual(delivered.closing,
                         "Work item 12 delivered outside the target: issue closed by ameesh.")
        self.assertIsNone(self.view(True).closing)

    def test_projet_d_un_lot(self):
        project = plan_github.lot_project
        self.assertEqual(project({"app": "web", "assignee_team": "app"}), "web")
        self.assertEqual(project({"app": "", "assignee": "o", "assignee_team": "app"}), "app")
        self.assertEqual(project({"assignee": "o", "assignee_chantier": "app"}), "app")
        self.assertEqual(project({"team": "web"}), "web")          # file d'amélioration
        self.assertIsNone(project({"title": "x"}))

    def test_pr_vers_issues(self):
        pr = {"body": "Closes #12, fixes acme/app#3 et resolves: #4 ; voir #5, encloses #6"}
        self.assertEqual(plan_github.issues_of_pr(pr, PUBLIC),
                         ["acme/app#3", "acme/web#4", "acme/web#12"])
        self.assertEqual(plan_github.issues_of_pr({}, PUBLIC), [])


# ==========================================================================
# projection sur un GitHub simulé (base réelle)
# ==========================================================================

class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.gh = FakeGh()
        self.gh.repo(PUBLIC)["visibility"] = "public"
        self.gh.repo(PRIVATE)["visibility"] = "private"
        self.settings = plan_github.parse_settings(SETTINGS)
        registry.upsert(self.db, "ouvrier", harness="claude", chantier="app", host="atelier-7")

    def project(self, project="web", repo=PUBLIC, **kw):
        kw.setdefault("settings", self.settings)
        return plan_github.project_lots(self.db, self.gh, project, repo, **kw)

    def issue(self, repo, title):
        found = self.gh.issue(repo, title)
        self.assertIsNotNone(found, title)
        return found

    def number(self, repo, title) -> int:
        return int(self.issue(repo, title)["number"])

    def comments(self, repo, number) -> list:
        return self.gh.repo(repo)["comments"].get(str(number), [])

    def assign(self, lot, agent) -> None:
        """L'assigné posé directement : la garde d'attribution (L37) n'est pas
        l'objet de ces tests, et dépend du canon de la machine."""
        self.db.execute("UPDATE work_items SET assignee = %s WHERE id = %s",
                        (agent, lot["id"]))


class LotIssuesTest(_Base):
    def test_une_issue_par_lot_ouvert_puis_rien(self):
        a = work.add(self.db, title="Export CSV", app="web", type="bug",
                     body="interne : /home/bob/x\nRésumé public : l'export perd des lignes")
        work.add(self.db, title="Autre projet", app="app", body="corps privé")
        old = work.add(self.db, title="Ancien", app="web")
        work.close(self.db, old["id"], abandoned=True)        # terminé : jamais d'issue neuve
        dry = self.project(dry_run=True)
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual([(r["work_item"], r["action"]) for r in dry["issues"]],
                         [(a["id"], "create")])
        self.assertTrue(dry["public"])
        self.assertIn("ameesh:bug", dry["labels_created"])
        self.assertIsNone(work.get(self.db, a["id"])["issue_ref"])
        report = self.project()
        issue = self.issue(PUBLIC, "Export CSV")
        self.assertEqual(sorted(issue["labels"]), ["ameesh", "ameesh:bug", "ameesh:intake"])
        self.assertIn("<!-- ameesh:work=%d d=" % a["id"], issue["body"])
        self.assertIn("l'export perd des lignes", issue["body"])
        self.assertNotIn("/home/bob", issue["body"])
        self.assertEqual(work.get(self.db, a["id"])["issue_ref"],
                         "%s#%d" % (PUBLIC, issue["number"]))
        self.assertEqual(report["counts"]["created"], 1)
        self.assertEqual(len(self.gh.repo(PUBLIC)["issues"]), 1)
        self.assertIn("ameesh:bug", self.gh.repo(PUBLIC)["labels"])
        # seconde passe : rien à écrire
        self.gh.reset_calls()
        again = self.project()
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual({r["action"] for r in again["issues"]}, {"unchanged"})
        # le lot de l'autre projet va dans son dépôt, privé : corps publié
        self.project("app", PRIVATE)
        self.assertIn("corps privé", self.issue(PRIVATE, "Autre projet")["body"])
        self.assertEqual(len(self.gh.repo(PUBLIC)["issues"]), 1)

    def test_projet_par_l_equipe_de_l_assigne_et_issue_ref_ailleurs(self):
        lot = work.add(self.db, title="Sans app")
        self.assign(lot, "ouvrier")                     # chantier « app » au registre
        self.project()
        self.assertEqual(self.gh.repo(PUBLIC)["issues"], [])
        self.project("app", PRIVATE)
        self.assertIn("assigned to `ouvrier`", self.issue(PRIVATE, "Sans app")["body"])
        # suivi dans le dépôt d'un autre projet configuré : jamais recopié ici
        self.db.execute("UPDATE work_items SET app = 'web' WHERE id = %s", (lot["id"],))
        self.gh.reset_calls()
        self.project()
        self.assertEqual(self.gh.repo(PUBLIC)["issues"], [])

    def test_titre_etat_assigne_priorite_suivis(self):
        lot = work.add(self.db, title="Export", app="web")
        self.project()
        number = self.number(PUBLIC, "Export")
        work.move(self.db, lot["id"], "build")
        self.assign(lot, "ouvrier")
        self.db.execute("UPDATE work_items SET title = 'Export v2', priority = 1 WHERE id = %s",
                        (lot["id"],))
        report = self.project()
        row = [r for r in report["issues"] if r["work_item"] == lot["id"]][0]
        self.assertEqual((row["action"], sorted(row["changes"])),
                         ("update", ["ameesh labels", "body", "title"]))
        issue = self.issue(PUBLIC, "Export v2")
        self.assertEqual(issue["number"], number)
        self.assertEqual(sorted(issue["labels"]), ["ameesh", "ameesh:build", "ameesh:evolution",
                                                   "ameesh:p1"])
        self.assertIn("high priority · assigned to `ouvrier`", issue["body"])
        self.gh.reset_calls()
        self.project()
        self.assertEqual(self.gh.writes(), [])

    def test_modification_humaine_gardee_tant_que_le_lot_ne_change_pas(self):
        lot = work.add(self.db, title="Export", app="web")
        self.project()
        issue = self.issue(PUBLIC, "Export")
        issue["title"] = "Titre retouché à la main"
        issue["body"] += "\n\nNote d'un humain."
        issue["labels"].append("priorité-client")              # étiquette humaine
        self.gh.reset_calls()
        report = self.project()
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(report["human_edits"][0]["kept"], ["title", "body"])
        self.assertEqual(work.get(self.db, lot["id"])["title"], "Export")   # jamais réimporté
        # le lot change de titre : seul le titre est réécrit, la note humaine reste
        self.db.execute("UPDATE work_items SET title = 'Export final' WHERE id = %s",
                        (lot["id"],))
        self.project()
        issue = self.issue(PUBLIC, "Export final")
        self.assertIn("Note d'un humain.", issue["body"])
        self.assertIn("priorité-client", issue["labels"])
        self.gh.reset_calls()
        self.project()
        self.assertEqual(self.gh.writes(), [])
        # une issue fermée à la main reste fermée tant que le lot est ouvert
        issue["state"] = "closed"
        self.project()
        self.assertEqual(self.issue(PUBLIC, "Export final")["state"], "closed")

    def test_fermeture_avec_un_commentaire_qui_dit_pourquoi(self):
        a = work.add(self.db, title="Fusionné", app="web")
        b = work.add(self.db, title="Remplacé", app="web")
        c = work.add(self.db, title="Promu", app="web")
        self.project()
        work.close_merged(self.db, a["id"], sha=SHA, pr_ref=PUBLIC + "#9")
        work.close(self.db, b["id"], superseded_by=a["id"])
        for state in ("build", "qa", "merged", "promoted"):
            work.move(self.db, c["id"], state)
        report = self.project()
        self.assertEqual(report["counts"]["closed"], 3)
        merged = self.issue(PUBLIC, "Fusionné")
        self.assertEqual((merged["state"], merged["state_reason"]), ("closed", "completed"))
        self.assertIn("ameesh:merged", merged["labels"])
        self.assertEqual(self.comments(PUBLIC, merged["number"]),
                         ["Work item %d merged by PR #9: issue closed by ameesh." % a["id"]])
        replaced = self.issue(PUBLIC, "Remplacé")
        self.assertEqual((replaced["state"], replaced["state_reason"]), ("closed", "not_planned"))
        self.assertEqual(self.comments(PUBLIC, replaced["number"]),
                         ["Work item %d superseded by work item %d (#%d): issue closed by ameesh."
                          % (b["id"], a["id"], merged["number"])])
        promoted = self.issue(PUBLIC, "Promu")
        self.assertEqual(promoted["state_reason"], "completed")
        self.assertIn("delivered", self.comments(PUBLIC, promoted["number"])[0])
        # une seule fois
        self.gh.reset_calls()
        self.project()
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(len(self.comments(PUBLIC, merged["number"])), 1)

    def test_titre_refuse_rien_n_est_publie(self):
        a = work.add(self.db, title="Purger /home/bob/cache", app="web")
        report = self.project()
        self.assertEqual(report["refused"][0]["work_item"], a["id"])
        self.assertEqual(report["refused"][0]["reasons"], ["local path"])
        self.assertEqual(report["counts"]["refused"], 1)
        self.assertEqual(self.gh.repo(PUBLIC)["issues"], [])
        self.assertNotIn("/home/bob", json.dumps(report, ensure_ascii=False))
        b = work.add(self.db, title="Lot pour Globex", app="app")
        private = self.project("app", PRIVATE)
        self.assertEqual([(r["work_item"], r["reasons"]) for r in private["refused"]],
                         [(b["id"], ["term excluded by the organization"])])
        self.assertEqual(self.gh.repo(PRIVATE)["issues"], [])
        # un chemin local passe dans un dépôt privé
        work.add(self.db, title="Purger /home/bob/cache privé", app="app")
        self.project("app", PRIVATE)
        self.issue(PRIVATE, "Purger /home/bob/cache privé")

    def test_visibilite_en_cache_et_depot_illisible_traite_en_public(self):
        work.add(self.db, title="Export", app="web", body="corps interne")
        cache: dict = {}
        self.project(cache=cache, dry_run=True)
        self.project(cache=cache, dry_run=True)
        reads = [c for c in self.gh.calls() if c[:2] == ["api", "repos/%s" % PUBLIC]]
        self.assertEqual(len(reads), 1)
        self.gh.state["missing_repos"] = [PRIVATE]
        report = self.project("web", PRIVATE)           # dépôt forcé, visibilité illisible
        self.assertEqual((report["visibility"], report["public"]), ("unknown", True))
        self.assertNotIn("corps interne", self.issue(PRIVATE, "Export")["body"])

    def test_erreur_de_github_notee_sans_lever(self):
        work.add(self.db, title="Un", app="web")
        work.add(self.db, title="Deux", app="web")
        self.gh.fail = lambda argv: argv[:3] == ["api", "-X", "POST"] \
            and argv[3] == "repos/%s/issues" % PUBLIC
        report = self.project()
        self.assertEqual(report["counts"]["errors"], 2)
        self.assertEqual(self.gh.repo(PUBLIC)["issues"], [])
        self.assertIn("502", report["errors"][0]["detail"])

    def test_issue_retrouvee_par_son_marqueur_et_issue_ref_etrangere(self):
        a = work.add(self.db, title="Export", app="web")
        b = work.add(self.db, title="Ticket ailleurs", app="web", issue_ref="JIRA-12")
        self.project()
        number = self.number(PUBLIC, "Export")
        # l'issue_ref perdue (écriture en base échouée) : retrouvée, jamais recréée
        self.db.execute("UPDATE work_items SET issue_ref = NULL WHERE id = %s", (a["id"],))
        report = self.project()
        self.assertEqual(report["counts"]["created"], 0)
        self.assertEqual(work.get(self.db, a["id"])["issue_ref"], "%s#%d" % (PUBLIC, number))
        # une issue_ref d'un autre outil est gardée ; l'issue se retrouve par son marqueur
        self.assertEqual(work.get(self.db, b["id"])["issue_ref"], "JIRA-12")
        self.assertEqual(len(self.gh.repo(PUBLIC)["issues"]), 2)

    def test_issue_designee_adoptee(self):
        self.gh.repo(PUBLIC)["issues"].append(
            {"id": 9500, "number": 50, "title": "Écrite à la main", "body": "texte humain",
             "labels": ["bug"], "state": "open", "state_reason": None})
        lot = work.add(self.db, title="Export", app="web", issue_ref=PUBLIC + "#50")
        self.project()
        issue = self.issue(PUBLIC, "Écrite à la main")
        self.assertEqual(len(self.gh.repo(PUBLIC)["issues"]), 1)
        self.assertTrue(issue["body"].startswith("<!-- ameesh:work=%d d=" % lot["id"]))
        self.assertIn("texte humain", issue["body"])
        self.assertEqual(sorted(issue["labels"]), ["ameesh", "ameesh:evolution",
                                                   "ameesh:intake", "bug"])
        # le lot change ensuite : ses champs suivent
        work.close(self.db, lot["id"], abandoned=True)
        self.project()
        self.assertEqual(self.issue(PUBLIC, "Écrite à la main")["state"], "closed")

    def test_doublon_d_un_projecteur_concurrent_ferme(self):
        lot = work.add(self.db, title="Export", app="web")

        def concurrent(argv):
            if argv[:3] == ["api", "-X", "POST"] and argv[3] == "repos/%s/issues" % PUBLIC:
                self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                                (PUBLIC + "#99", lot["id"]))
            return False

        self.gh.fail = concurrent
        report = self.project()
        self.assertEqual(report["duplicates"][0]["kept"], 99)
        mine = self.gh.repo(PUBLIC)["issues"][0]
        self.assertEqual((mine["state"], mine["state_reason"]), ("closed", "not_planned"))
        self.assertNotIn("ameesh:work=", mine["body"])
        self.assertIn("Duplicate of #99", self.comments(PUBLIC, mine["number"])[0])
        self.assertEqual(work.get(self.db, lot["id"])["issue_ref"], PUBLIC + "#99")

    def test_creations_bornees_par_passage(self):
        for n in range(3):
            work.add(self.db, title="Lot %d" % n, app="web")
        report = self.project(max_creates=2)
        self.assertEqual((report["counts"]["created"], report["counts"]["deferred"]), (2, 1))
        self.assertEqual(self.project()["counts"]["created"], 1)


class SyncGithubCloseTest(_Base):
    def test_closes_ferme_le_lot_de_l_issue(self):
        lot = work.add(self.db, title="Export", app="web")
        other = work.add(self.db, title="Autre", app="web")
        self.project()
        number = self.number(PUBLIC, "Export")
        self.gh.state["prs"]["%s#5" % PUBLIC] = {
            "state": "MERGED", "headRefName": "agent/ouvrier-export",
            "body": "Livre l'export.\n\nameesh-work: %d\nCloses #%d" % (lot["id"], number),
            "mergeCommit": {"oid": "c" * 40}, "mergedBy": {"login": "alice"},
            "mergedAt": "2999-01-01T00:00:00Z"}
        self.gh.state["prs"]["%s#6" % PUBLIC] = {
            "state": "MERGED", "headRefName": "agent/y", "body": "Fixes #%d" % (number + 1),
            "mergeCommit": {"oid": "d" * 40}, "mergedBy": {"login": "alice"},
            "mergedAt": "2999-01-01T00:00:00Z"}
        report = plan_github.sync_github(self.db, self.gh, PUBLIC)
        self.assertEqual(sorted((r["pr"], r["work_item"], r["result"]) for r in report["results"]),
                         [("%s#5" % PUBLIC, lot["id"], "merged"),
                          ("%s#6" % PUBLIC, other["id"], "merged")])
        self.assertEqual(work.get(self.db, lot["id"])["pr_ref"], "%s#5" % PUBLIC)
        # puis la projection ferme l'issue, avec la PR dans son commentaire
        self.project()
        self.assertEqual(self.issue(PUBLIC, "Export")["state"], "closed")
        self.assertEqual(self.comments(PUBLIC, number),
                         ["Work item %d merged by PR #5: issue closed by ameesh." % lot["id"]])


# ==========================================================================
# automatique : `ameesh notify`, sur l'hôte désigné
# ==========================================================================

class ProjectorTest(_Base):
    def configured(self, **github):
        return dataclasses.replace(self.cfg, github=dict(SETTINGS, **github), host="atelier")

    def test_hote_designe_seulement(self):
        work.add(self.db, title="Export", app="web")
        logs: list = []
        elsewhere = plan_github.Projector(self.configured(host="ailleurs"), log=logs.append,
                                          gh=self.gh)
        self.assertEqual(elsewhere.tick(self.db, now=1000.0), [])
        elsewhere.tick(self.db, now=2000.0)
        self.assertEqual(self.gh.calls(), [])
        self.assertEqual(logs, ["GitHub issues: the projection is run by host ailleurs, "
                                "not atelier"])
        logs.clear()
        nobody = dataclasses.replace(self.cfg, github={"projects": {"web": PUBLIC}})
        self.assertEqual(plan_github.Projector(nobody, log=logs.append, gh=self.gh)
                         .tick(self.db, now=1000.0), [])
        self.assertIn("no designated host", logs[0])
        broken = dataclasses.replace(self.cfg, github={"projets": {}})
        plan_github.Projector(broken, log=logs.append, gh=self.gh).tick(self.db, now=1000.0)
        self.assertIn("invalid configuration", logs[-1])
        self.assertEqual(self.gh.calls(), [])

    def test_cadence_et_lots_nouveaux_des_le_passage_suivant(self):
        work.add(self.db, title="Export", app="web")
        logs: list = []
        projector = plan_github.Projector(self.configured(), log=logs.append, gh=self.gh)
        first = projector.tick(self.db, now=1000.0)
        self.assertEqual(sorted(r["project"] for r in first), ["app", "web"])
        self.assertEqual(len(self.gh.repo(PUBLIC)["issues"]), 1)
        self.assertIn("GitHub issues acme/web (web): 1 created", logs)
        self.gh.reset_calls()
        self.assertEqual(projector.tick(self.db, now=1030.0), [])     # rien de nouveau
        self.assertEqual(self.gh.calls(), [])
        work.add(self.db, title="Import", app="web")
        again = projector.tick(self.db, now=1060.0)                   # lot nouveau : tout de suite
        self.assertEqual([r["project"] for r in again], ["web"])
        self.assertEqual(len(self.gh.repo(PUBLIC)["issues"]), 2)
        # tour complet de chaque projet à l'échéance
        self.assertEqual(sorted(r["project"] for r in projector.tick(self.db, now=1400.0)),
                         ["app", "web"])

    def test_refus_dit_une_fois_et_sans_passage_rapide(self):
        work.add(self.db, title="Purger /home/bob/cache", app="web")
        logs: list = []
        projector = plan_github.Projector(self.configured(), log=logs.append, gh=self.gh)
        projector.tick(self.db, now=1000.0)
        refusals = [line for line in logs if "title refused (local path)" in line]
        self.assertEqual(len(refusals), 1)
        self.assertIn("not published", refusals[0])
        self.assertNotIn("/home/bob", refusals[0])
        self.gh.reset_calls()
        self.assertEqual(projector.tick(self.db, now=1030.0), [])
        self.assertEqual(self.gh.calls(), [])
        projector.tick(self.db, now=1300.0)
        self.assertEqual(sum("title refused" in line for line in logs), 1)

    def test_panne_de_github_sans_effet_et_attente(self):
        work.add(self.db, title="Export", app="web")
        logs: list = []
        self.gh.fail = lambda argv: True
        projector = plan_github.Projector(self.configured(), log=logs.append, gh=self.gh)
        self.assertEqual(projector.tick(self.db, now=1000.0), [])
        self.assertEqual(sum("failed, retrying in 300 s" in line for line in logs), 2)
        self.gh.reset_calls()
        projector.tick(self.db, now=1030.0)
        self.assertEqual(self.gh.calls(), [])                  # le projet attend un intervalle
        self.gh.fail = None
        projector.tick(self.db, now=1300.0)
        self.assertEqual(len(self.gh.repo(PUBLIC)["issues"]), 1)

    def test_dans_le_passage_de_notify(self):
        work.add(self.db, title="Export", app="web")
        logs: list = []
        cfg = self.configured()
        notifier = notify.Notifier(cfg, notify.parse_config({}), log=logs.append,
                                   clock=lambda: 1000.0)
        notifier._projector = plan_github.Projector(cfg, log=logs.append, gh=self.gh,
                                                    clock=lambda: 1000.0)
        notifier.run_pass(self.db, current=[])
        self.assertEqual(len(self.gh.repo(PUBLIC)["issues"]), 1)

        class Boom:
            def tick(self, db, now):
                raise RuntimeError("boum")

        notifier._projector = Boom()
        notifier.run_pass(self.db, current=[])               # jamais fatal au passage
        self.assertIn("GitHub issues: pass failed (boum)", logs)
        plain = notify.Notifier(self.cfg, notify.parse_config({}), log=logs.append)
        plain.run_pass(self.db, current=[])
        self.assertIsNone(plain._projector)


# ==========================================================================
# CLI (sous-processus, faux gh)
# ==========================================================================

class CliTest(_Base):
    def cli_env(self, github=None) -> dict:
        state = os.path.join(self.tmp, "gh-state.json")
        if not os.path.exists(state):
            with open(state, "w", encoding="utf-8") as fh:
                json.dump({"prs": {}, "repos": {PUBLIC: {"visibility": "public"}}}, fh)
        config = os.path.join(self.tmp, "config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"github": SETTINGS if github is None else github}, fh)
        self.state_file = state
        return self.env(AMEESH_GH_BIN=os.path.join(FAKEBIN, "gh"),
                        AMEESH_FAKE_GH_STATE=state, AMEESH_CONFIG=config)

    def test_rattrapage_essai_puis_reel(self):
        lot = work.add(self.db, title="Export", app="web", type="bug")
        env = self.cli_env()
        out = self.mesh("work", "project-github", "--app", "web", "--dry-run", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("[dry run] work item issues of project web on acme/web (public repository",
                      out.stdout)
        self.assertIn("work item %-5s" % lot["id"], out.stdout)
        self.assertIn("to create", out.stdout)
        with open(self.state_file, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["repos"][PUBLIC]["issues"], [])
        out = self.mesh("work", "project-github", "--app", "WEB", "--json", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["counts"]["created"], 1)
        self.assertEqual(work.get(self.db, lot["id"])["issue_ref"], PUBLIC + "#1")
        out = self.mesh("work", "project-github", "--app", "web", env=env)
        self.assertIn("1 issue(s) unchanged", out.stdout)

    def test_erreurs(self):
        env = self.cli_env()
        out = self.mesh("work", "project-github", "--app", "inconnu", env=env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("missing from `github.projects`", out.stderr)
        out = self.mesh("work", "project-github", env=env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("--app <project>", out.stderr)
        out = self.mesh("work", "project-github", "--app", "web",
                        env=self.cli_env(github={"projets": {}}))
        self.assertEqual(out.returncode, 1)
        self.assertIn("unknown key(s)", out.stderr)
        # --repo avec --app : le dépôt configuré est remplacé
        work.add(self.db, title="Export", app="autre")
        out = self.mesh("work", "project-github", "--app", "autre", "--repo", PUBLIC,
                        "--dry-run", env=self.cli_env())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("on acme/web", out.stdout)


if __name__ == "__main__":
    unittest.main()
