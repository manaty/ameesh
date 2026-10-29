# SPDX-License-Identifier: AGPL-3.0-only
"""#2130/D : projection opt-in des lots pinnés Process vers leur issue L126.

Le mapping est data-only (document compilé au chargement), l'état de l'issue
vient du cœur de l'artefact (`merged` reste ouvert), les faits manquants
refusent avec un code, sans écriture et sans retombée legacy. GitHub est simulé
(`tests/fake_github.py`), la base est réelle (`PgTestCase`) pour `issue_feed`.
"""
from __future__ import annotations

import dataclasses
import unittest

from ameesh import plan_github, process_projection, process_workflow, registry, storage, work

from .fake_github import FakeGh
from .support import PgTestCase

PUBLIC = "acme/web"
REF = "canon@%s:workflows/tpk.json" % ("a" * 40)
VERSION = 1

STAGES = (
    ("draft", "intake"), ("backlog", "intake"), ("sprint", "intake"),
    ("implementation", "build"), ("review", "qa"), ("tests", "qa"),
    ("staging", "merged"), ("staging_passed", "merged"),
    ("preprod", "merged"), ("preprod_passed", "merged"),
    ("staging_failed", "build"), ("preprod_failed", "build"),
    ("done", "promoted"), ("stopped", "closed"),
)
TRANSITIONS = (
    ("draft", "backlog", "planning"), ("draft", "sprint", "planning"),
    ("backlog", "sprint", "planning"), ("sprint", "implementation", "start_work"),
    ("implementation", "review", "review_requested"),
    ("review", "tests", "qa_started"), ("review", "implementation", "qa_repair"),
    ("tests", "staging", "merged_and_staging_deployed"),
    ("staging", "staging_passed", "staging_pass"),
    ("staging", "staging_failed", "staging_failure"),
    ("staging_passed", "preprod", "preprod_deployed"),
    ("preprod", "preprod_passed", "preprod_pass"),
    ("preprod", "preprod_failed", "preprod_failure"),
    ("preprod_passed", "done", "work_completed"),
    ("staging_failed", "implementation", "repair_started"),
    ("preprod_failed", "implementation", "repair_started"),
    ("draft", "stopped", "stop_work"),
)
LABELS = {status: "board:%s" % status for status, _core in STAGES}


def document(**changes):
    doc = {
        "schema": "process-workflow/1",
        "initial": "draft",
        "types": ["bug", "evolution", "docs"],
        "stages": [{"status": status, "core": core} for status, core in STAGES],
        "transitions": [{"from": source, "to": target, "guard": guard}
                        for source, target, guard in TRANSITIONS],
        "projection": {"project": "TPK", "status_field": "Stage",
                       "product_field": "Product",
                       "issue_open": ["intake", "build", "qa", "merged"],
                       "issue_completed": ["promoted"],
                       "issue_not_planned": ["closed"]},
    }
    doc.update(changes)
    return doc


SETTINGS = {"projects": {"web": PUBLIC}, "host": "atelier",
            "process": {"web": {"ref": REF, "version": VERSION,
                                "document": document(), "labels": LABELS}}}


def settings_of(raw=None):
    return plan_github.parse_settings(SETTINGS if raw is None else raw)


def mapping_of(raw=None, project="web"):
    return settings_of(raw).process_for(project)[0]


def guard_of(raw=None):
    return plan_github.make_guard(settings_of(raw))


def pinned_item(**changes):
    item = {"id": 12, "type": "bug", "title": "Réparer l'export", "state": "build",
            "assignee": "ouvrier", "priority": 1, "workstream": "export",
            "package_id": None, "body": "Détail interne",
            "workflow_ref": REF, "workflow_version": VERSION,
            "workflow_stage": "implementation", "close_reason": None}
    item.update(changes)
    return item


# ==========================================================================
# mapping : data-only, compilé au chargement (sans base)
# ==========================================================================

class MappingTest(unittest.TestCase):
    def test_mapping_compile(self):
        mapping = mapping_of()
        self.assertEqual((mapping.project, mapping.ref, mapping.version),
                         ("web", REF, VERSION))
        self.assertEqual(mapping.labels["staging"], "board:staging")
        self.assertEqual(mapping.spec.core_of("staging"), "merged")
        self.assertEqual(mapping.spec.workflow_version, VERSION)

    def test_liste_pour_un_transfert(self):
        """Un transfert admet deux révisions du même projet."""
        other = dict(SETTINGS)
        second = {"ref": REF + "@2", "version": 2,
                  "document": document(), "labels": LABELS}
        other["process"] = {"web": [SETTINGS["process"]["web"], second]}
        mappings = settings_of(other).process_for("web")
        self.assertEqual([m.version for m in mappings], [VERSION, 2])

    def test_sans_process_rien_ne_change(self):
        settings = settings_of({"projects": {"web": PUBLIC}})
        self.assertEqual(settings.process_for("web"), ())
        self.assertEqual(settings.process_for("autre"), ())
        self.assertEqual(plan_github.parse_settings({}), plan_github.Settings())

    def test_refus_de_configuration(self):
        base = SETTINGS["process"]["web"]
        cases = {
            "projet inconnu": {"autre": base},
            "cle inconnue": {"web": dict(base, extra=1)},
            "ref vide": {"web": dict(base, ref="  ")},
            "version negative": {"web": dict(base, version=-1)},
            "document absent": {"web": {k: v for k, v in base.items() if k != "document"}},
            "schema inconnu": {"web": dict(base, document=document(schema="process-workflow/2"))},
            "libelle manquant": {"web": dict(base, labels={k: v for k, v in LABELS.items()
                                                           if k != "stopped"})},
            "libelle en trop": {"web": dict(base, labels=dict(LABELS, autre="x"))},
            "libelle vide": {"web": dict(base, labels=dict(LABELS, done=" "))},
            "revision admise deux fois": {"web": [base, dict(base)]},
            "liste vide": {"web": []},
            "mapping nul": {"web": None},
        }
        for label, process in cases.items():
            with self.subTest(case=label):
                raw = {"projects": {"web": PUBLIC}, "process": process}
                with self.assertRaises(plan_github.SettingsError):
                    plan_github.parse_settings(raw)
        # `process` non-objet
        with self.assertRaises(plan_github.SettingsError):
            plan_github.parse_settings({"projects": {"web": PUBLIC}, "process": []})


# ==========================================================================
# décision pure : état et libellé d'un lot pinné (sans base)
# ==========================================================================

class ProjectIssueTest(unittest.TestCase):
    def project(self, stage, **changes):
        mapping = mapping_of()
        core = mapping.spec.core_of(stage)
        changes.setdefault("state", core)
        item = pinned_item(workflow_stage=stage, **changes)
        return process_projection.project_issue(item, mapping)

    def test_couverture_des_quatorze_etapes(self):
        for stage, core in STAGES:
            with self.subTest(stage=stage):
                project = self.project(stage)
                self.assertEqual(project.core, core)
                self.assertEqual(project.label, "board:%s" % stage)

    def test_merged_reste_ouvert(self):
        for stage in ("staging", "staging_passed", "preprod", "preprod_passed"):
            with self.subTest(stage=stage):
                self.assertEqual(self.project(stage).state, "open")
        for stage in ("draft", "implementation", "review", "staging_failed"):
            with self.subTest(stage=stage):
                self.assertEqual(self.project(stage).state, "open")

    def test_terminaux(self):
        done = self.project("done")
        self.assertEqual((done.state, done.state_reason), ("closed", "completed"))
        stopped = self.project("stopped")
        self.assertEqual((stopped.state, stopped.state_reason), ("closed", "not_planned"))
        delivered = self.project("stopped", close_reason="delivered")
        self.assertEqual((delivered.state, delivered.state_reason), ("closed", "completed"))

    def test_retenues_gardent_l_etape(self):
        """Le cœur d'une retenue n'est pas dans la ligne : il vient de l'artefact."""
        for hold in ("blocked", "waiting_human"):
            with self.subTest(hold=hold):
                project = self.project("review", state=hold)
                self.assertEqual((project.state, project.core, project.label),
                                 ("open", "qa", "board:review"))
        merged = self.project("preprod_passed", state="blocked")
        self.assertEqual((merged.state, merged.core), ("open", "merged"))

    def test_fermeture_native_hors_etape(self):
        """Un lot pinné fermé par `work close` garde son étape : le motif décide."""
        mapping = mapping_of()

        def native(reason):
            return process_projection.project_issue(
                pinned_item(workflow_stage="implementation", state="closed",
                            close_reason=reason), mapping)

        delivered = native("delivered")
        self.assertEqual((delivered.state, delivered.state_reason, delivered.core),
                         ("closed", "completed", "closed"))
        for reason in ("abandoned", "superseded"):
            with self.subTest(reason=reason):
                closed = native(reason)
                self.assertEqual((closed.state, closed.state_reason), ("closed", "not_planned"))
        for missing in (None, "", "inconnu"):
            with self.subTest(reason=missing):
                with self.assertRaises(process_projection.ProjectionRefusal) as caught:
                    native(missing)
                self.assertEqual(caught.exception.code, "projection-classification")

    def test_refus(self):
        mapping = mapping_of()
        cases = {
            "projection-unmapped": (pinned_item(), None),
            "projection-unmapped version": (pinned_item(workflow_version=2), mapping),
            "projection-unmapped ref": (pinned_item(workflow_ref=REF + "@2"), mapping),
            "projection-stage-missing": (pinned_item(workflow_stage=None), mapping),
            "projection-stage-undeclared": (pinned_item(workflow_stage="inconnue"), mapping),
            "projection-core-mismatch": (pinned_item(workflow_stage="review", state="build"),
                                         mapping),
        }
        for label, (item, admitted) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(process_projection.ProjectionRefusal) as caught:
                    process_projection.project_issue(item, admitted)
                self.assertEqual(caught.exception.code, label.split()[0])

    def test_refus_de_defense(self):
        mapping = mapping_of()
        bare = dataclasses.replace(mapping, labels={})
        with self.assertRaises(process_projection.ProjectionRefusal) as caught:
            process_projection.project_issue(pinned_item(), bare)
        self.assertEqual(caught.exception.code, "projection-label-missing")
        unclassified = dataclasses.replace(
            mapping, spec=dataclasses.replace(
                mapping.spec,
                projection=dataclasses.replace(mapping.spec.projection, issue_open=())))
        with self.assertRaises(process_projection.ProjectionRefusal) as caught:
            process_projection.project_issue(pinned_item(), unclassified)
        self.assertEqual(caught.exception.code, "projection-classification")


# ==========================================================================
# vue de l'issue : le pinné ne passe jamais par l'état legacy (sans base)
# ==========================================================================

class LotViewTest(unittest.TestCase):
    def view(self, mapping=True, **changes):
        item = dict(pinned_item(), **changes)
        return plan_github.lot_view(
            item, public=False, guard=guard_of(), project="web", repo=PUBLIC,
            mapping=mapping_of() if mapping else None)

    def test_libelle_d_etape_remplace_l_etat(self):
        view = self.view()
        self.assertEqual(view.labels, ["ameesh", "ameesh:bug", "ameesh:p1",
                                       "board:implementation"])
        self.assertNotIn("ameesh:build", view.labels)
        self.assertEqual((view.state, view.state_reason), ("open", None))
        self.assertEqual(view.projection_refused, [])

    def test_merged_reste_ouvert(self):
        view = self.view(workflow_stage="staging", state="merged")
        self.assertEqual((view.state, view.state_reason), ("open", None))
        self.assertIn("board:staging", view.labels)

    def test_fermetures(self):
        done = self.view(workflow_stage="done", state="promoted")
        self.assertEqual((done.state, done.state_reason), ("closed", "completed"))
        self.assertIn("delivered", done.closing)
        stopped = self.view(workflow_stage="stopped", state="closed",
                            close_reason="abandoned")
        self.assertEqual((stopped.state, stopped.state_reason), ("closed", "not_planned"))
        self.assertIn("abandoned", stopped.closing)

    def test_retenue(self):
        view = self.view(workflow_stage="review", state="blocked")
        self.assertEqual((view.state, view.labels[-1]), ("open", "board:review"))

    def test_sans_mapping_aucun_champ_desire(self):
        view = self.view(mapping=False, workflow_stage="staging", state="merged")
        self.assertEqual(view.projection_refused, ["projection-unmapped"])
        self.assertEqual((view.state, view.state_reason, view.labels, view.closing),
                         ("", None, [], None))

    def test_lot_non_pinne_inchange(self):
        item = dict(pinned_item(), workflow_ref="", workflow_stage=None,
                    workflow_version=0, state="build")
        view = plan_github.lot_view(item, public=False, guard=guard_of(), project="web",
                                    repo=PUBLIC, mapping=None)
        self.assertEqual(view.labels, ["ameesh", "ameesh:bug", "ameesh:build", "ameesh:p1"])
        self.assertEqual((view.state, view.projection_refused), ("open", []))


# ==========================================================================
# projection réelle sur un GitHub simulé (base réelle)
# ==========================================================================

class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.gh = FakeGh()
        self.gh.repo(PUBLIC)["visibility"] = "private"
        self.settings = settings_of()
        registry.upsert(self.db, "ouvrier", harness="claude", chantier="web", host="atelier")

    def project(self, **kw):
        kw.setdefault("settings", self.settings)
        kw.setdefault("mappings", self.settings.process_for("web"))
        return plan_github.project_lots(self.db, self.gh, "web", PUBLIC, **kw)

    def add(self, title="Export CSV", **changes):
        return work.add(self.db, title=title, app="web", **changes)

    def pin(self, lot, stage, *, state=None, ref=REF, version=VERSION, close_reason=None):
        mapping = self.settings.process_for("web")[0]
        core = mapping.spec.core_of(stage)
        self.db.execute(
            "UPDATE work_items SET workflow_ref = %s, workflow_stage = %s,"
            " workflow_version = %s, state = %s,"
            " close_reason = coalesce(%s, close_reason) WHERE id = %s",
            (ref, stage, version, state or core, close_reason, lot["id"]))

    def issue(self, title):
        found = self.gh.issue(PUBLIC, title)
        self.assertIsNotNone(found, title)
        return found

    def test_pinne_merged_cree_une_issue_ouverte(self):
        """Le cœur `merged` du flux : l'issue est créée OUVERTE, jamais fermée."""
        lot = self.add()
        self.pin(lot, "staging")
        row = work.get(self.db, lot["id"])
        self.assertEqual((row["state"], row["legacy_state"]), ("delivered", "merged"))
        report = self.project()
        self.assertEqual(report["counts"]["created"], 1)
        issue = self.issue("Export CSV")
        self.assertEqual(issue["state"], "open")
        self.assertIn("board:staging", issue["labels"])
        self.assertEqual(work.get(self.db, lot["id"])["issue_ref"],
                         "%s#%d" % (PUBLIC, issue["number"]))
        # seconde passe : rien à écrire
        self.gh.reset_calls()
        again = self.project()
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual({row["action"] for row in again["issues"]}, {"unchanged"})

    def test_pinne_done_ferme_completed(self):
        lot = self.add()
        self.pin(lot, "implementation")
        self.project()
        self.pin(lot, "done")
        row = work.get(self.db, lot["id"])
        self.assertEqual((row["state"], row["legacy_state"]), ("delivered", "promoted"))
        report = self.project()
        issue = self.issue("Export CSV")
        self.assertEqual((issue["state"], issue["state_reason"]), ("closed", "completed"))
        self.assertIn("board:done", issue["labels"])
        self.assertIn("completed", "\n".join(self.gh.repo(PUBLIC)["comments"][str(issue["number"])]).lower())
        self.assertEqual(report["counts"]["closed"], 1)

    def test_pinne_retenue_garde_l_etape(self):
        lot = self.add()
        self.pin(lot, "review", state="blocked")
        self.project()
        issue = self.issue("Export CSV")
        self.assertEqual((issue["state"], "board:review" in issue["labels"]), ("open", True))
        self.assertNotIn("ameesh:blocked", issue["labels"])

    def test_pinne_sans_mapping_refuse_sans_ecriture(self):
        """L'état legacy aurait fermé l'issue : rien n'est écrit, le refus est nommé."""
        lot = self.add()
        self.pin(lot, "staging", ref="canon@%s:autre.json" % ("b" * 40))
        report = self.project()
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual([row["codes"] for row in report["projection_refused"]],
                         [["projection-unmapped"]])
        self.assertEqual(report["counts"]["projection_refused"], 1)
        self.assertIsNone(work.get(self.db, lot["id"])["issue_ref"])

    def test_refus_ne_pose_pas_issue_ref(self):
        """Le refus n'adopte pas l'issue retrouvée par son marqueur (aucune
        écriture native, pas même `_remember_ref`)."""
        lot = self.add()
        self.pin(lot, "staging")
        self.project()
        issue = self.issue("Export CSV")
        self.db.execute("UPDATE work_items SET issue_ref = NULL, workflow_ref = %s"
                        " WHERE id = %s",
                        ("canon@%s:autre.json" % ("b" * 40), lot["id"]))
        self.gh.reset_calls()
        report = self.project()
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(report["counts"]["projection_refused"], 1)
        self.assertIsNone(work.get(self.db, lot["id"])["issue_ref"])
        self.assertEqual(issue["state"], "open")       # l'issue est restée intacte

    def test_fermeture_legacy_a_mauvaise_raison_corrigee(self):
        """Issue déjà fermée, mais au mauvais motif : la raison se corrige."""
        lot = self.add()
        self.pin(lot, "implementation")
        self.project()
        issue = self.issue("Export CSV")
        self.pin(lot, "stopped", state="closed", close_reason="abandoned")
        self.project()
        self.assertEqual((issue["state"], issue["state_reason"]), ("closed", "not_planned"))
        comments = list(self.gh.repo(PUBLIC)["comments"][str(issue["number"])])
        issue["state_reason"] = "completed"             # motif legacy erroné
        report = self.project()
        self.assertEqual((issue["state"], issue["state_reason"]), ("closed", "not_planned"))
        self.assertEqual(report["counts"]["updated"], 1)
        self.assertEqual(report["counts"]["closed"], 0)     # déjà fermée : pas de commentaire
        self.assertEqual(self.gh.repo(PUBLIC)["comments"][str(issue["number"])], comments)

    def test_fermeture_native_delivered_projette_completed(self):
        lot = self.add()
        self.pin(lot, "implementation")
        self.project()
        issue = self.issue("Export CSV")
        self.db.execute("UPDATE work_items SET state = 'closed', close_reason = 'delivered'"
                        " WHERE id = %s", (lot["id"],))
        report = self.project()
        self.assertEqual((issue["state"], issue["state_reason"]), ("closed", "completed"))
        self.assertIn("completed", "\n".join(self.gh.repo(PUBLIC)["comments"][str(issue["number"])]).lower())
        self.assertEqual(report["counts"]["closed"], 1)

    def test_transfert_retire_l_ancien_libelle(self):
        """Deux révisions admises : le libellé de l'ancienne est à nous, il cède."""
        lot = self.add()
        self.pin(lot, "staging")
        self.project()
        issue = self.issue("Export CSV")
        self.assertIn("board:staging", issue["labels"])
        second = {"ref": REF + "@2", "version": 2, "document": document(),
                  "labels": {k: v.replace("board:", "board2:") for k, v in LABELS.items()}}
        raw = dict(SETTINGS)
        raw["process"] = {"web": [SETTINGS["process"]["web"], second]}
        self.settings = plan_github.parse_settings(raw)
        self.pin(lot, "staging", ref=REF + "@2", version=2)
        self.project()
        self.assertIn("board2:staging", issue["labels"])
        self.assertNotIn("board:staging", issue["labels"])

    def test_work_get_porte_la_revision(self):
        lot = self.add()
        self.pin(lot, "staging")
        row = work.get(self.db, lot["id"])
        self.assertEqual((row["workflow_ref"], row["workflow_stage"],
                          row["workflow_version"]), (REF, "staging", VERSION))

    def test_pinne_done_sans_issue_n_en_cree_pas(self):
        lot = self.add()
        self.pin(lot, "done")
        report = self.project()
        self.assertEqual(report["counts"]["created"], 0)
        self.assertEqual(self.gh.repo(PUBLIC)["issues"], [])
        # le même lot remis dans un cœur ouvert reçoit son issue
        self.pin(lot, "implementation")
        self.assertEqual(self.project()["counts"]["created"], 1)

    def test_panne_github_puis_reprise(self):
        lot = self.add()
        self.pin(lot, "implementation")
        self.gh.fail = lambda argv: argv[:3] == ["api", "-X", "POST"] \
            and argv[3] == "repos/%s/issues" % PUBLIC
        report = self.project()
        self.assertEqual(report["counts"]["errors"], 1)
        self.gh.fail = None
        self.assertEqual(self.project()["counts"]["created"], 1)
        self.assertEqual(len(self.gh.repo(PUBLIC)["issues"]), 1)
        self.assertIsNotNone(work.get(self.db, lot["id"])["issue_ref"])

    def test_edition_humaine_gardee(self):
        lot = self.add()
        self.pin(lot, "implementation")
        self.project()
        issue = self.issue("Export CSV")
        self.gh.repo(PUBLIC)["issues"][0]["title"] = "Titre humain"
        self.gh.reset_calls()
        self.project()
        self.assertEqual(self.issue("Titre humain")["title"], "Titre humain")
        self.assertIn("title", [f for row in self.project()["human_edits"] for f in row["fields"]])

    def test_projector_transmet_le_mapping(self):
        lot = self.add()
        self.pin(lot, "staging")
        cfg = dataclasses.make_dataclass(
            "Cfg", [("github", dict), ("host", str)])(github=SETTINGS, host="atelier")
        projector = plan_github.Projector(cfg, gh=self.gh, log=lambda _t: None)
        projector.tick(self.db, now=1000.0)
        self.assertEqual(self.issue("Export CSV")["state"], "open")
        self.assertIn("board:staging", self.issue("Export CSV")["labels"])

    def test_issue_feed_porte_la_revision(self):
        pinned = self.add("Pinné")
        plain = self.add("Ordinaire")
        self.pin(pinned, "staging")
        feed = {row["id"]: row for row in
                __import__("ameesh.storage", fromlist=["storage"]).of(self.db)
                .work.issue_feed(100)}
        self.assertEqual(feed[pinned["id"]]["workflow_ref"], REF)
        self.assertEqual(feed[pinned["id"]]["workflow_stage"], "staging")
        self.assertEqual(feed[pinned["id"]]["workflow_version"], VERSION)
        self.assertEqual(feed[plain["id"]]["workflow_ref"], "")
        self.assertIsNone(feed[plain["id"]]["workflow_stage"])
        self.assertEqual(feed[plain["id"]]["workflow_version"], 0)


if __name__ == "__main__":
    unittest.main()
