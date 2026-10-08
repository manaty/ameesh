# SPDX-License-Identifier: AGPL-3.0-only
"""Plan de travail (L29) : fiches WorkPackage, lots reliés, fermeture sur
fusion, projection GitHub, epics et attentes dans `ameesh progress`.

GitHub est TOUJOURS simulé (`tests/fake_github.py`, en processus ou par le
faux binaire `tests/fakebin/gh`) : aucun appel réseau, aucune issue réelle.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest

from ameesh import db as db_mod
from ameesh import (canon, canon_sync, plan, plan_git, plan_github, progress, registry,
                    stagnation, storage, work)

from .fake_github import FakeGh
from .support import FAKEBIN, PgTestCase
from .test_actions import ALICE, PR, ActionsBase
from .test_canon import _TmpMixin, codes, fiche, git, write

REPO = "acme/web"
SHA = "b" * 40


def package(ident: str, kind: str, parent: str | None = None, responsible: str = "human:alice",
            **extra) -> str:
    fields = {"type": "WorkPackage", "title": "\"%s\"" % ident, "kind": kind,
              "responsible": responsible}
    if parent:
        fields["parent"] = parent
    fields.update(extra)
    return fiche(**fields)


# ==========================================================================
# profil du canon : WorkPackage (sans base)
# ==========================================================================

class PackageValidationTest(_TmpMixin, unittest.TestCase):
    def load(self, root):
        return canon.load(root, untrusted=True)

    def test_exemple_valide_et_lu(self):
        root = self.example_copy()
        loaded = self.load(root)
        findings = [f for f in canon.validate(loaded) if f.code != "canon-untrusted"]
        self.assertEqual(findings, [])
        by_id = {p.id: p for p in loaded.packages}
        self.assertEqual(sorted(by_id), ["cat-export", "cat-recherche", "catalogue", "paiement",
                                         "pay-webhook", "v1"])
        self.assertEqual((by_id["catalogue"].kind, by_id["catalogue"].parent), ("epic", "v1"))
        self.assertEqual(by_id["cat-recherche"].scope,
                         ["src/catalogue/recherche/**", "tests/catalogue/test_recherche.py"])
        self.assertEqual(by_id["pay-webhook"].status, "draft")
        self.assertTrue(by_id["v1"].fiche.ref.startswith("canon:plan/v1.md@"))
        self.assertEqual(loaded.to_dict()["packages"][0]["id"], "cat-export")

    def test_erreurs_du_plan(self):
        root = self.example_copy()
        write(root, "plan/x-orphelin.md", package("x-orphelin", "lot"))
        write(root, "plan/x-parent.md", package("x-parent", "lot", "inconnu"))
        write(root, "plan/x-sous-lot.md", package("x-sous-lot", "lot", "cat-recherche"))
        write(root, "plan/x-epic.md", package("x-epic", "epic", "catalogue"))
        write(root, "plan/x-jalon.md", package("x-jalon", "milestone", "v1"))
        write(root, "plan/x-sorte.md", package("x-sorte", "chantier"))
        write(root, "plan/x-resp.md", package("x-resp", "epic", responsible="human:inconnu"))
        write(root, "plan/x-sans.md", fiche(type="WorkPackage", title="sans", kind="epic"))
        write(root, "plan/double.md", package("cat-export", "lot", "catalogue", id="cat-export"))
        findings = canon.validate(self.load(root))
        by_package = {}
        for f in findings:
            by_package.setdefault(f.package, set()).add(f.code)
        self.assertIn("package-lot-orphan", by_package["x-orphelin"])
        self.assertIn("package-parent-unknown", by_package["x-parent"])
        self.assertIn("package-kind-incoherent", by_package["x-sous-lot"])
        self.assertIn("package-kind-incoherent", by_package["x-epic"])
        self.assertIn("package-kind-incoherent", by_package["x-jalon"])
        self.assertIn("package-kind-invalid", by_package["x-sorte"])
        self.assertIn("package-responsible-unresolved", by_package["x-resp"])
        self.assertIn("package-responsible-missing", by_package["x-sans"])
        self.assertIn("package-duplicate", by_package["cat-export"])
        orphan = [f for f in findings if f.code == "package-lot-orphan"][0]
        self.assertEqual(orphan.severity, canon.WARNING)
        self.assertEqual(orphan.to_dict()["package"], "x-orphelin")

    def test_cycle(self):
        root = self.make_tmp()
        write(root, "membres/alice.md", fiche(type="Member", title="alice"))
        write(root, "a.md", package("a", "epic", "b"))
        write(root, "b.md", package("b", "epic", "a"))
        found = codes(canon.validate(self.load(root)), canon.ERROR)
        self.assertIn("package-cycle", found)

    def test_une_erreur_du_plan_ne_bloque_aucun_agent(self):
        root = self.example_copy()
        write(root, "plan/x.md", package("x", "lot", "inconnu"))
        write(root, "plan/casse.md", "---\ntype: WorkPackage\ntitle: [non fermé\n---\n")
        loaded = self.load(root)
        findings = canon.validate(loaded)
        errors = canon.errors(findings)
        self.assertTrue(errors)
        self.assertTrue(all(f.package for f in errors), errors)
        block = canon.blocking(findings)
        self.assertEqual(block.global_errors, [])
        self.assertEqual(block.reasons("orchestre", ["atelier"]), [])
        status, diagnostic = canon_sync.assess(loaded, "atelier", findings)
        self.assertEqual(status, canon_sync.CANON_OK)
        self.assertIn("plan", diagnostic)


# ==========================================================================
# dérivations du plan (sans base)
# ==========================================================================

PACKAGES = [
    {"id": "v1", "kind": "milestone", "title": "V1", "parent": None},
    {"id": "catalogue", "kind": "epic", "title": "Catalogue", "parent": "v1",
     "responsible": "human:alice"},
    {"id": "cat-a", "kind": "lot", "title": "A", "parent": "catalogue",
     "responsible": "human:alice"},
    {"id": "cat-b", "kind": "lot", "title": "B", "parent": "catalogue"},
    {"id": "cat-c", "kind": "lot", "title": "C", "parent": "catalogue"},
    {"id": "cat-d", "kind": "lot", "title": "D", "parent": "catalogue"},
    {"id": "solo", "kind": "lot", "title": "Solo", "parent": "v1"},
]


class PlanUnitTest(unittest.TestCase):
    def test_epic_et_progression(self):
        by_id = {p["id"]: p for p in PACKAGES}
        self.assertEqual(plan.epic_of("cat-a", by_id), "catalogue")
        self.assertEqual(plan.epic_of("catalogue", by_id), "catalogue")
        self.assertIsNone(plan.epic_of("solo", by_id))
        self.assertIsNone(plan.epic_of(None, by_id))
        items = [
            {"id": 1, "package_id": "cat-a", "state": "merged"},
            {"id": 2, "package_id": "cat-b", "state": "qa"},
            {"id": 3, "package_id": "cat-c", "state": "closed"},       # abandonné
            {"id": 4, "package_id": "catalogue", "state": "promoted"},  # direct sous l'epic
            {"id": 5, "package_id": "solo", "state": "merged"},
        ]
        summary = plan.summarize(PACKAGES, items)
        epic = summary["epics"][0]
        self.assertEqual((epic["id"], epic["milestone"]), ("catalogue", "v1"))
        # unités : cat-a fusionné, cat-b ouvert, cat-c abandonné, cat-d en attente, #4 fusionné
        self.assertEqual((epic["lots_total"], epic["lots_merged"], epic["lots_abandoned"],
                          epic["lots_open"], epic["lots_pending"]), (5, 2, 1, 1, 1))
        self.assertEqual(epic["progress"], 0.5)
        self.assertEqual(epic["work_items"], [4])
        self.assertEqual([lot["status"] for lot in epic["lots"]],
                         ["merged", "open", "abandoned", "pending"])
        ms = summary["milestones"][0]
        self.assertEqual((ms["id"], ms["epics"], ms["lots_total"], ms["lots_merged"]),
                         ("v1", ["catalogue"], 6, 3))
        self.assertIsNone(ms["at_ts"])

    def test_statut_d_une_fiche_lot(self):
        self.assertEqual(plan.package_status([]), "pending")
        self.assertEqual(plan.package_status([{"state": "closed"}]), "abandoned")
        self.assertEqual(plan.package_status([{"state": "closed"}, {"state": "merged"}]),
                         "merged")
        self.assertEqual(plan.package_status([{"state": "merged"}, {"state": "build"}]), "open")

    def test_stagnation(self):
        now = 100_000.0
        self.assertIsNone(stagnation.stale("build", now - 3600, now, 6 * 3600))
        stale = stagnation.stale("qa", now - 7 * 3600, now, 6 * 3600)
        self.assertEqual((stale["idle_s"], stale["threshold_s"]), (7 * 3600, 6 * 3600))
        self.assertIsNone(stagnation.stale("merged", now - 99 * 3600, now, 6 * 3600))
        self.assertIsNone(stagnation.stale("closed", now - 99 * 3600, now, 6 * 3600))
        self.assertEqual(stagnation.last_activity({"updated_ts": 5.0}, [{"created_ts": 9.0}],
                                            [{"at_ts": 12.0}], [{"updated_ts": 7.0}]), 12.0)
        self.assertEqual(stagnation.stale_after(None, {}), 6 * 3600)
        self.assertEqual(stagnation.stale_after("90m", {}), 5400)
        self.assertEqual(stagnation.stale_after(None, {"AMEESH_STALE_AFTER": "2h"}), 7200)
        with self.assertRaises(stagnation.StaleError):
            stagnation.stale_after("bientôt", {})

    def test_attentes(self):
        item = {"state": "qa", "assignee": "ouvrier"}
        none = {"verdict_kind": None, "verdict_current": False, "reviewer": None}
        self.assertEqual(stagnation.waiting(item, none)["label"],
                         "verdict d'un relecteur (non désigné)")
        prior = dict(none, reviewer="relecteur")
        self.assertEqual(stagnation.waiting(item, prior)["label"], "verdict de relecteur")
        blocked = {"verdict_kind": "blocked", "verdict_current": True}
        self.assertEqual(stagnation.waiting(item, blocked),
                         {"what": "fix", "who": "ouvrier", "label": "correction de ouvrier"})
        ok = {"verdict_kind": "ok", "verdict_current": True}
        self.assertEqual(stagnation.waiting(item, ok, responsible="human:alice")["label"],
                         "fusion par human:alice")
        merge = {"connector": "git-merge", "state": "proposed", "approvers": ["human:bob"]}
        self.assertEqual(stagnation.waiting(item, ok, [merge])["label"],
                         "approbation de la fusion par human:bob")
        approved = dict(merge, state="approved", proposed_by="agent:orchestre")
        self.assertEqual(stagnation.waiting(item, ok, [approved])["what"], "merge")
        self.assertEqual(stagnation.waiting(item, ok, [dict(merge, state="unknown")])["what"],
                         "merge-outcome")
        self.assertEqual(stagnation.waiting({"state": "waiting_human"}, none,
                                          responsible="human:alice")["label"],
                         "décision humaine de human:alice")
        self.assertEqual(stagnation.waiting({"state": "build", "assignee": "o"}, blocked)["what"],
                         "fix")
        self.assertEqual(stagnation.waiting({"state": "intake"}, none)["label"],
                         "attribution (lot non assigné)")
        self.assertIsNone(stagnation.waiting({"state": "merged"}, none))
        self.assertIsNone(stagnation.waiting({"state": "closed"}, none))

    def test_pr_vers_fiches(self):
        idents = ["L2", "L29", "cat-export"]
        self.assertEqual(plan_github.packages_of_pr({"headRefName": "lot/L29-plan"}, idents),
                         ["L29"])
        self.assertEqual(plan_github.packages_of_pr({"headRefName": "lot/l29"}, idents), ["L29"])
        self.assertEqual(plan_github.packages_of_pr({"headRefName": "lot/L2-x"}, idents), ["L2"])
        self.assertEqual(plan_github.packages_of_pr({"headRefName": "lot/L290"}, idents), [])
        self.assertEqual(plan_github.packages_of_pr({"headRefName": "feature/L29"}, idents), [])
        body = "Résumé\n\nameesh-lot: cat-export\n> ameesh-lot : L2\n"
        self.assertEqual(plan_github.packages_of_pr({"headRefName": "lot/L29-a", "body": body},
                                                    idents), ["L29", "cat-export", "L2"])
        self.assertEqual(plan_github.packages_of_pr({"body": "voir ameesh-lot: L29 plus loin"},
                                                    idents), [])
        self.assertEqual(plan_github.work_items_of_pr({"body": "ameesh-work: #12\nameesh-work: 3"}),
                         [3, 12])

    def test_lien_vers_la_fiche(self):
        ref = "canon:plan/v1.md@abc123"
        self.assertEqual(plan_github.fiche_link(ref, None), "`%s`" % ref)
        self.assertIn("(https://forge.example/acme/canon/blob/abc123/plan/v1.md)",
                      plan_github.fiche_link(ref, "https://forge.example/acme/canon/blob/{commit}"))


# ==========================================================================
# base : canon sync, lots reliés, fermetures
# ==========================================================================

class _PlanDb(_TmpMixin, PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.db.execute("TRUNCATE actions, action_attempts, action_events "
                        "RESTART IDENTITY CASCADE")
        self.root = self.example_copy()

    def sync(self, host: str = "atelier") -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, canon.load(self.root, untrusted=True), host)

    def packages(self) -> dict:
        return {p["id"]: p for p in storage.of(self.db).packages.all(include_absent=True)}


class PackageSyncTest(_PlanDb):
    def test_sync_recopie_les_fiches(self):
        report = self.sync()
        self.assertEqual(sorted(report.packages.created),
                         ["cat-export", "cat-recherche", "catalogue", "paiement",
                          "pay-webhook", "v1"])
        rows = self.packages()
        self.assertEqual(rows["cat-recherche"]["kind"], "lot")
        self.assertEqual(rows["cat-recherche"]["parent"], "catalogue")
        self.assertEqual(rows["cat-recherche"]["scope"],
                         ["src/catalogue/recherche/**", "tests/catalogue/test_recherche.py"])
        self.assertTrue(rows["v1"]["canon_ref"].startswith("canon:plan/v1.md@untrusted:"))
        self.assertTrue(all(r["present"] for r in rows.values()))
        # idempotent ; un autre hôte voit le même plan
        again = self.sync("banc")
        self.assertEqual(again.packages.created + again.packages.updated, [])
        self.assertEqual(len(again.packages.unchanged), 6)
        self.assertEqual(again.to_dict()["packages"]["unchanged"], again.packages.unchanged)

    def test_retrait_parent_change_et_fiche_en_erreur(self):
        self.sync()
        item = work.add(self.db, title="export", package="cat-export")
        self.assertEqual((item["package_id"], item["package_parent"]), ("cat-export", "catalogue"))
        # la fiche change de parent : le lot suit ; une fiche retirée reste (absente)
        write(self.root, "plan/cat-export.md",
              package("cat-export", "lot", "paiement", responsible="human:bruno"))
        write(self.root, "plan/pay-webhook.md", None)
        write(self.root, "plan/casse.md", package("casse", "lot", "inconnu"))
        report = self.sync()
        self.assertEqual(report.packages.updated, ["cat-export"])
        self.assertEqual(report.packages.retired, ["pay-webhook"])
        self.assertEqual(report.packages.skipped, ["casse"])
        self.assertEqual(report.packages.relinked, 1)
        self.assertEqual(report.status, canon_sync.CANON_OK)
        rows = self.packages()
        self.assertFalse(rows["pay-webhook"]["present"])
        self.assertNotIn("casse", rows)
        self.assertEqual(work.get(self.db, item["id"])["package_parent"], "paiement")
        # les agents restent réclamables malgré l'erreur du plan
        self.assertEqual(registry.get(self.db, "orchestre")["responsible"], "human:alice")
        with self.assertRaises(work.WorkError) as ctx:
            work.add(self.db, title="x", package="pay-webhook")
        self.assertIn("retirée", str(ctx.exception))
        with self.assertRaises(work.WorkError):
            work.add(self.db, title="x", package="nulle-part")
        with self.assertRaises(work.WorkError) as ctx:
            work.add(self.db, title="x", package="v1")          # un jalon
        self.assertIn("milestone", str(ctx.exception))

    def test_lier_et_delier(self):
        self.sync()
        item = work.add(self.db, title="sans plan")
        self.assertIsNone(item["package_id"])
        row = work.link(self.db, item["id"], "catalogue", actor="orchestre")
        self.assertEqual((row["package_id"], row["package_parent"]), ("catalogue", "v1"))
        row = work.link(self.db, item["id"], None)
        self.assertIsNone(row["package_id"])
        self.assertEqual(work.events(self.db, item["id"])[0]["note"], "plan : détaché")


class CloseTest(_PlanDb):
    def test_abandon_et_remplacement(self):
        a = work.add(self.db, title="A")
        b = work.add(self.db, title="B")
        work.move(self.db, a["id"], "build")
        with self.assertRaises(work.WorkError):
            work.move(self.db, a["id"], "closed")
        with self.assertRaises(work.WorkError):
            work.close(self.db, a["id"])                         # ni l'un ni l'autre
        with self.assertRaises(work.WorkError):
            work.close(self.db, a["id"], superseded_by=a["id"])
        with self.assertRaises(work.WorkError):
            work.close(self.db, a["id"], superseded_by=999)
        row = work.close(self.db, a["id"], superseded_by=b["id"], note="refait", actor="alice")
        self.assertEqual((row["state"], row["close_reason"], row["superseded_by"]),
                         ("closed", "superseded", b["id"]))
        self.assertIsNotNone(row["closed_ts"])
        ms = work.milestones(self.db, a["id"])
        self.assertEqual(ms[0]["kind"], "closed")
        self.assertEqual(ms[0]["note"], "remplacé par #%d — refait" % b["id"])
        with self.assertRaises(work.WorkError):
            work.close(self.db, a["id"], abandoned=True)         # déjà fermé
        with self.assertRaises(work.WorkError):
            work.move(self.db, a["id"], "build")                 # terminal
        row = work.close(self.db, b["id"], abandoned=True)
        self.assertEqual(row["close_reason"], "abandoned")

    def test_fusion_constatee_idempotente_sans_reouverture(self):
        a = work.add(self.db, title="A")
        work.move(self.db, a["id"], "build")
        done = work.close_merged(self.db, a["id"], sha=SHA, actor="github:alice",
                                 source="sync-github", pr_ref="acme/web#3")
        self.assertEqual((done["result"], done["item"]["state"]), ("merged", "merged"))
        self.assertEqual(done["item"]["pr_ref"], "acme/web#3")
        merged = [m for m in work.milestones(self.db, a["id"]) if m["kind"] == "merged"]
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]["sha"], merged[0]["actor"]), (SHA, "github:alice"))
        self.assertEqual(work.timeline(self.db, a["id"])["merged_ts"] is not None, True)
        again = work.close_merged(self.db, a["id"], sha=SHA)
        self.assertEqual(again["result"], "already")
        self.assertEqual(len(work.events(self.db, a["id"])), 3)   # création, build, fusion
        b = work.add(self.db, title="B")
        work.close(self.db, b["id"], abandoned=True)
        refused = work.close_merged(self.db, b["id"], sha=SHA, pr_ref="acme/web#4")
        self.assertEqual(refused["result"], "refused")
        self.assertIn("aucune réouverture", refused["detail"])
        self.assertEqual(work.get(self.db, b["id"])["state"], "closed")


# ==========================================================================
# fusion par la porte (connecteur git-merge, faux gh)
# ==========================================================================

class GateMergeCloseTest(ActionsBase):
    def test_fusion_confirmee_ferme_le_lot(self):
        item = work.add(self.db, title="lot fusionné par la porte")
        for state in ("build", "qa"):
            work.move(self.db, item["id"], state)
        gh = self.gh()
        action = self.propose("merge", connector=gh, target=PR, work_item=item["id"])
        self.approve(action)
        result = self.execute(action, gh)
        self.assertEqual(result["state"], "confirmed")
        row = work.get(self.db, item["id"])
        self.assertEqual((row["state"], row["pr_ref"]), ("merged", PR))
        merged = [m for m in work.milestones(self.db, item["id"]) if m["kind"] == "merged"][0]
        self.assertEqual(merged["sha"], self.pr()["mergeCommit"]["oid"])
        self.assertEqual(merged["actor"], ALICE)

    def test_fusion_apres_reconciliation_et_lot_ferme_jamais_rouvert(self):
        item = work.add(self.db, title="lot en file de fusion")
        work.move(self.db, item["id"], "build")
        queued = self.gh(mode="queue")
        action = self.propose("merge", connector=queued, target=PR, work_item=item["id"])
        self.approve(action)
        self.assertEqual(self.execute(action, queued)["state"], "unknown")
        self.assertEqual(work.get(self.db, item["id"])["state"], "build")
        from ameesh import actions
        result = actions.reconcile(self.db, action["action_id"], self.gh(view="land"), by=ALICE)
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(work.get(self.db, item["id"])["state"], "merged")
        # un lot abandonné dont la PR finit fusionnée reste fermé
        other = work.add(self.db, title="abandonné")
        work.close(self.db, other["id"], abandoned=True)
        self.set_pr("o/r#20")
        gh = self.gh()
        late = self.propose("merge", connector=gh, target="o/r#20", work_item=other["id"])
        self.approve(late)
        self.assertEqual(self.execute(late, gh)["state"], "confirmed")
        self.assertEqual(work.get(self.db, other["id"])["state"], "closed")


# ==========================================================================
# GitHub simulé : sync-github, project-github
# ==========================================================================

class GithubTest(_PlanDb):
    def setUp(self) -> None:
        super().setUp()
        self.sync()
        self.gh = FakeGh()

    def merged_pr(self, number: int, head: str, body: str = "", merged_at: str | None = None,
                  login: str = "alice") -> None:
        self.gh.state["prs"]["%s#%d" % (REPO, number)] = {
            "state": "MERGED", "headRefName": head, "body": body,
            "mergeCommit": {"oid": SHA}, "mergedBy": {"login": login},
            "mergedAt": merged_at or time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime(time.time() + 60)),
            "url": "https://example.invalid/%d" % number}

    def test_sync_github_ferme_les_lots_fusionnes(self):
        a = work.add(self.db, title="recherche", package="cat-recherche")
        work.move(self.db, a["id"], "build")
        b = work.add(self.db, title="export", package="cat-export")
        c = work.add(self.db, title="webhook", package="pay-webhook")
        d = work.add(self.db, title="hors plan")
        e = work.add(self.db, title="abandonné", package="cat-export")
        work.close(self.db, e["id"], abandoned=True)
        self.merged_pr(1, "lot/cat-recherche-index")
        self.merged_pr(2, "fix/export", body="ameesh-lot: cat-export")
        self.merged_pr(3, "x", body="ameesh-work: %d" % d["id"])
        self.merged_pr(4, "lot/pay-webhook", merged_at="2000-01-01T00:00:00Z")  # avant le lot
        self.gh.state["prs"]["%s#5" % REPO] = {"state": "OPEN", "headRefName": "lot/cat-export"}
        dry = plan_github.sync_github(self.db, self.gh, REPO, dry_run=True)
        self.assertEqual(work.get(self.db, a["id"])["state"], "build")
        self.assertIn("would-merge", [r["result"] for r in dry["results"]])
        report = plan_github.sync_github(self.db, self.gh, REPO)
        by_item = {r["work_item"]: r for r in report["results"]}
        self.assertEqual(by_item[a["id"]]["result"], "merged")
        self.assertEqual(by_item[b["id"]]["result"], "merged")
        self.assertEqual(by_item[d["id"]]["result"], "merged")
        self.assertEqual(by_item[c["id"]]["result"], "later")
        self.assertEqual(by_item[e["id"]]["result"], "refused")
        self.assertEqual(work.get(self.db, c["id"])["state"], "intake")
        self.assertEqual(work.get(self.db, e["id"])["state"], "closed")
        ms = [m for m in work.milestones(self.db, a["id"]) if m["kind"] == "merged"][0]
        self.assertEqual((ms["sha"], ms["actor"]), (SHA, "github:alice"))
        self.assertEqual(work.get(self.db, a["id"])["pr_ref"], "%s#1" % REPO)
        # idempotent ; et GitHub n'est que LU
        again = plan_github.sync_github(self.db, self.gh, REPO)
        self.assertEqual(again["merged"], 0)
        self.assertEqual({c[0] + " " + c[1] for c in self.gh.calls()}, {"pr list"})

    def issue(self, prefix: str) -> dict:
        found = self.gh.issue(REPO, prefix)
        self.assertIsNotNone(found, prefix)
        return found

    def test_projection_creee_puis_idempotente(self):
        a = work.add(self.db, title="recherche", package="cat-recherche")
        for state in ("build", "qa"):
            work.move(self.db, a["id"], state)
        b = work.add(self.db, title="export", package="cat-export")
        work.close_merged(self.db, b["id"], sha=SHA)
        # essai : lectures seulement
        dry = plan_github.project_github(self.db, self.gh, REPO, dry_run=True)
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual({i["action"] for i in dry["issues"]}, {"create"})
        self.assertEqual(len(dry["issues"]), 5)                   # 3 lots, 2 epics
        report = plan_github.project_github(self.db, self.gh, REPO,
                                            canon_url="https://forge.example/canon/blob/main")
        self.assertTrue(report["sub_issues_api"])
        self.assertIn("ameesh:qa", report["labels_created"])
        rech = self.issue("[cat-recherche]")
        self.assertEqual(sorted(rech["labels"]), ["ameesh", "ameesh:qa"])
        self.assertEqual(rech["state"], "open")
        self.assertIn("<!-- ameesh:package=cat-recherche d=", rech["body"])
        self.assertIn("(https://forge.example/canon/blob/main/plan/cat-recherche.md)", rech["body"])
        export = self.issue("[cat-export]")
        self.assertEqual((export["state"], export["state_reason"]), ("closed", "completed"))
        self.assertIn("ameesh:merged", export["labels"])
        hook = self.issue("[pay-webhook]")
        self.assertIn("ameesh:intake", hook["labels"])
        epic = self.issue("[catalogue]")
        children = self.gh.repo(REPO)["sub_issues"][str(epic["number"])]
        self.assertEqual(sorted(children), sorted([rech["id"], export["id"]]))
        self.assertIn("1/2 lots fusionnés", epic["body"])
        # seconde projection : rien à écrire
        self.gh.reset_calls()
        again = plan_github.project_github(self.db, self.gh, REPO,
                                           canon_url="https://forge.example/canon/blob/main")
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual({i["action"] for i in again["issues"]}, {"unchanged"})
        self.assertEqual(again["proposals"], [])
        # l'état du lot change : seule son issue (et l'epic) est mise à jour
        work.close_merged(self.db, a["id"], sha=SHA)
        third = plan_github.project_github(self.db, self.gh, REPO,
                                           canon_url="https://forge.example/canon/blob/main")
        changed = {i["package"] for i in third["issues"] if i["action"] == "update"}
        self.assertEqual(changed, {"cat-recherche", "catalogue"})
        self.assertEqual(self.issue("[cat-recherche]")["state"], "closed")
        self.assertEqual(self.issue("[catalogue]")["state"], "closed")   # epic complet
        self.assertEqual(third["proposals"], [])

    def test_modification_dans_github_signalee_jamais_reimportee(self):
        a = work.add(self.db, title="recherche", package="cat-recherche")
        work.move(self.db, a["id"], "build")
        plan_github.project_github(self.db, self.gh, REPO)
        issue = self.issue("[cat-recherche]")
        issue["title"] = "titre réécrit à la main"
        issue["state"] = "closed"
        issue["labels"].append("priorité-haute")            # label humain : jamais touché
        report = plan_github.project_github(self.db, self.gh, REPO)
        self.assertEqual(len(report["proposals"]), 1)
        proposal = report["proposals"][0]
        self.assertEqual((proposal["package"], sorted(proposal["fields"])),
                         ("cat-recherche", ["state", "title"]))
        # ameesh reste la source : le lot n'a pas bougé, la vue est réécrite
        self.assertEqual(work.get(self.db, a["id"])["state"], "build")
        issue = self.issue("[cat-recherche]")
        self.assertEqual(issue["state"], "open")
        self.assertIn("priorité-haute", issue["labels"])
        self.assertIn("ameesh:build", issue["labels"])
        again = plan_github.project_github(self.db, self.gh, REPO)
        self.assertEqual(again["proposals"], [])

    def test_label_de_base_retire_ni_doublon_ni_silence(self):
        """P2-3 : l'issue se retrouve par son marqueur, même sans le label
        `ameesh` ; le retrait est une proposition, la vue est réécrite."""
        plan_github.project_github(self.db, self.gh, REPO)
        issue = self.issue("[cat-recherche]")
        epic = self.issue("[catalogue]")
        before = len(self.gh.repo(REPO)["issues"])
        children = list(self.gh.repo(REPO)["sub_issues"][str(epic["number"])])
        issue["labels"].remove("ameesh")
        result = plan_github.project_github(self.db, self.gh, REPO)
        self.assertEqual(len(self.gh.repo(REPO)["issues"]), before, result)
        self.assertTrue(any(p["package"] == "cat-recherche" for p in result["proposals"]))
        proposal = [p for p in result["proposals"] if p["package"] == "cat-recherche"][0]
        self.assertEqual(proposal["fields"], ["labels"])
        self.assertIn("ameesh", self.issue("[cat-recherche]")["labels"])
        self.assertEqual(result["sub_issues"], [])
        self.assertEqual(self.gh.repo(REPO)["sub_issues"][str(epic["number"])], children)

    def test_sans_api_sub_issues_liste_de_taches(self):
        self.gh.repo(REPO)["sub_issues_api"] = False
        a = work.add(self.db, title="recherche", package="cat-recherche")
        work.close_merged(self.db, a["id"], sha=SHA)
        report = plan_github.project_github(self.db, self.gh, REPO)
        self.assertIs(report["sub_issues_api"], False)
        epic = self.issue("[catalogue]")
        rech = self.issue("[cat-recherche]")
        export = self.issue("[cat-export]")
        self.assertIn("- [x] #%d" % rech["number"], epic["body"])
        self.assertIn("- [ ] #%d" % export["number"], epic["body"])
        self.gh.reset_calls()
        again = plan_github.project_github(self.db, self.gh, REPO)
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(again["proposals"], [])

    def test_refus_de_gh_et_depot_invalide(self):
        with self.assertRaises(plan_github.GithubError):
            plan_github.project_github(self.db, self.gh, "pas un dépôt")
        with self.assertRaises(plan_github.GithubError):
            plan_github.sync_github(self.db, self.gh, "acme")


# ==========================================================================
# ameesh progress : epics, attentes, stagnation, fermeture
# ==========================================================================

class ProgressPlanTest(_PlanDb):
    def setUp(self) -> None:
        super().setUp()
        # L37 (0030) : un lot ne s'assigne qu'à un agent connu et réveillable
        registry.upsert(self.db, "ouvrier", harness="claude")

    def test_instantane_avec_le_plan(self):
        self.sync()
        a = work.add(self.db, title="recherche", package="cat-recherche", assignee="ouvrier")
        for state in ("build", "qa"):
            work.move(self.db, a["id"], state)
        b = work.add(self.db, title="export", package="cat-export")
        work.close_merged(self.db, b["id"], sha=SHA)
        c = work.add(self.db, title="vieux", package="pay-webhook", assignee="ouvrier")
        work.move(self.db, c["id"], "build")
        self.db.execute("UPDATE work_items SET updated_at = now() - interval '9 hours',"
                        " created_at = now() - interval '10 hours' WHERE id = %s", (c["id"],))
        self.db.execute("UPDATE work_item_events SET created_at = now() - interval '9 hours'"
                        " WHERE work_item_id = %s", (c["id"],))
        self.db.execute("UPDATE work_item_milestones SET at = now() - interval '10 hours'"
                        " WHERE work_item_id = %s", (c["id"],))
        d = work.add(self.db, title="abandonné")
        work.close(self.db, d["id"], abandoned=True)
        # l'assigné existe : l'attente ne porte pas « (agent inconnu) » (L36)
        registry.upsert(self.db, "ouvrier", harness="claude")
        snap = progress.snapshot(self.db, self.cfg, since="2d", book=_NoBook())
        self.assertEqual(snap["schema"], "ameesh-progress/1")
        lots = {lot["id"]: lot for lot in snap["lots"]}
        self.assertEqual((lots[a["id"]]["epic"], lots[a["id"]]["package"]),
                         ("catalogue", "cat-recherche"))
        self.assertEqual(lots[a["id"]]["waiting_for"]["what"], "verdict")
        self.assertIsNone(lots[a["id"]]["stale"])
        self.assertEqual(lots[c["id"]]["stale"]["threshold_s"], 6 * 3600)
        self.assertGreaterEqual(lots[c["id"]]["stale"]["idle_s"], 9 * 3600 - 60)
        self.assertEqual(lots[c["id"]]["waiting_for"]["label"], "travail de ouvrier")
        self.assertEqual(lots[d["id"]]["state"], "closed")
        self.assertEqual(lots[d["id"]]["closed"]["reason"], "abandoned")
        self.assertIsNotNone(lots[d["id"]]["closed"]["at_ts"])
        self.assertIsNone(lots[d["id"]]["waiting_for"])
        self.assertEqual(lots[b["id"]]["state"], "merged")
        epics = {e["id"]: e for e in snap["epics"]}
        self.assertEqual((epics["catalogue"]["lots_merged"], epics["catalogue"]["lots_total"]),
                         (1, 2))
        self.assertEqual(epics["paiement"]["lots_open"], 1)
        self.assertEqual([m["id"] for m in snap["milestones"]], ["v1"])
        self.assertEqual(snap["milestones"][0]["lots_merged"], 1)
        # seuil réglable
        loose = progress.snapshot(self.db, self.cfg, since="2d", book=_NoBook(),
                                  stale_after=12 * 3600)
        self.assertIsNone({l["id"]: l for l in loose["lots"]}[c["id"]]["stale"])
        text = progress.format_text(snap, width=100)
        self.assertIn("STAGNANT", text)
        self.assertIn("attend : verdict", text)
        self.assertIn("EPICS (2)", text)
        self.assertIn("catalogue — Catalogue des produits : 1/2 lots fusionnés", text)
        self.assertIn("abandonné", text)
        html = progress.render_html(snap)
        self.assertIn('"epics"', html)
        self.assertIn("stagnant", html)


class StagnationUnifiedTest(_PlanDb):
    def setUp(self) -> None:
        super().setUp()
        # L37 (0030) : un lot ne s'assigne qu'à un agent connu et réveillable
        registry.upsert(self.db, "ouvrier", harness="claude")

    """Module unique `stagnation` (L26 + L29) : messages comme activité, action
    en attente d'un humain d'abord, verdict bloquant seulement après le
    dernier gel, lot fermé jamais stagnant ; l'alerte garde `waiting_for`."""

    def _vieux(self, item_id: int, hours: int = 9) -> None:
        for sql in ("UPDATE work_items SET updated_at = now() - interval '%d hours',"
                    " created_at = now() - interval '%d hours' WHERE id = %%s" % (hours, hours + 1),
                    "UPDATE work_item_events SET created_at = now() - interval '%d hours'"
                    " WHERE work_item_id = %%s" % hours,
                    "UPDATE work_item_milestones SET at = now() - interval '%d hours'"
                    " WHERE work_item_id = %%s" % hours):
            self.db.execute(sql, (item_id,))

    def test_regles_reprises_de_l26(self):
        gel = {"kind": "frozen", "at_ts": 10.0}
        bloque = {"kind": "verdict", "at_ts": 20.0, "verdict": "blocked"}
        regel = {"kind": "frozen", "at_ts": 30.0}
        item = {"id": 1, "state": "qa", "assignee": "ouvrier", "created_ts": 1.0}
        # verdict bloquant après le dernier gel : correction
        self.assertEqual(stagnation.waiting_for(item, [], [gel, bloque], []), "correction")
        # regelé depuis : le verdict bloquant ne compte plus, on attend un verdict
        self.assertEqual(stagnation.waiting_for(item, [], [gel, bloque, regel], []), "verdict")
        build = dict(item, state="build")
        self.assertIsNone(stagnation.waiting_for(build, [], [gel, bloque, regel], []))
        # une action en attente d'un humain passe avant tout
        pending = {"state": "proposed", "connector": "shell-noop", "approvers": ["human:bob"]}
        detail = stagnation.waiting(build, {}, [pending])
        self.assertEqual((detail["what"], detail["who"]), ("decision", "human:bob"))
        self.assertEqual(stagnation.legacy(detail), "decision_humaine")
        self.assertIsNone(stagnation.waiting(dict(item, state="closed"), {}, [pending]))

    def test_message_lie_et_lot_ferme(self):
        from ameesh import exploitation, mail
        self.sync()
        a = work.add(self.db, title="oublié", package="cat-export", assignee="ouvrier")
        for state in ("build", "qa"):
            work.move(self.db, a["id"], state)
        b = work.add(self.db, title="abandonné", assignee="ouvrier")
        work.close(self.db, b["id"], abandoned=True)
        self._vieux(a["id"])
        self._vieux(b["id"])
        alerts = [x for x in exploitation.alerts(self.cfg, self.db) if x["type"] == "stale_lot"]
        self.assertEqual([x["lot"] for x in alerts], [a["id"]])       # fermé : jamais stagnant
        self.assertEqual(alerts[0]["waiting_for"], "verdict")          # ameesh-alert/1 intact
        self.assertEqual(alerts[0]["waiting"]["what"], "verdict")      # champ ajouté
        rows = plan.annotate(self.db, [work.get(self.db, a["id"])])
        self.assertIsNotNone(rows[0]["stale"])
        snap = progress.snapshot(self.db, self.cfg, since="2d", book=_NoBook())
        lot = {l["id"]: l for l in snap["lots"]}[a["id"]]
        self.assertIsNotNone(lot["stale"])
        self.assertEqual(lot["waiting_for"]["what"], "verdict")
        # un message lié au lot est une activité, pour les trois vues
        mail.send(self.db, "orchestre", "ouvrier", "relance", work_item_id=str(a["id"]))
        self.assertFalse([x for x in exploitation.alerts(self.cfg, self.db)
                          if x["type"] == "stale_lot"])
        self.assertIsNone(plan.annotate(self.db, [work.get(self.db, a["id"])])[0]["stale"])
        snap = progress.snapshot(self.db, self.cfg, since="2d", book=_NoBook())
        self.assertIsNone({l["id"]: l for l in snap["lots"]}[a["id"]]["stale"])


class _NoBook:
    def gauges(self):
        return []


# ==========================================================================
# CLI (sous-processus, faux gh)
# ==========================================================================

class PlanCliTest(_PlanDb):
    def gh_env(self) -> dict:
        self.gh_state = os.path.join(self.tmp, "gh-state.json")
        if not os.path.exists(self.gh_state):
            with open(self.gh_state, "w", encoding="utf-8") as fh:
                json.dump({"prs": {"%s#7" % REPO: {
                    "state": "MERGED", "headRefName": "lot/cat-export-csv", "body": "",
                    "mergeCommit": {"oid": SHA}, "mergedBy": {"login": "bruno"},
                    "mergedAt": "2999-01-01T00:00:00Z"}}}, fh)
        return self.env(AMEESH_GH_BIN=os.path.join(FAKEBIN, "gh"),
                        AMEESH_FAKE_GH_STATE=self.gh_state)

    def test_cli_du_plan(self):
        self.sync()
        out = self.mesh("work", "add", "--title", "export", "--package", "cat-export")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("(plan : cat-export)", out.stdout)
        out = self.mesh("work", "add", "--title", "x", "--package", "inconnue")
        self.assertEqual(out.returncode, 1)
        self.assertIn("fiche WorkPackage inconnue", out.stderr)
        out = self.mesh("work", "add", "--title", "autre")
        out = self.mesh("work", "list")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("EPIC", out.stdout)
        self.assertIn("catalogue", out.stdout)
        self.assertIn("attend : attribution (lot non assigné)", out.stdout)
        for table, column in (("work_items", "updated_at"), ("work_items", "created_at"),
                              ("work_item_events", "created_at"),
                              ("work_item_milestones", "at")):
            self.db.execute("UPDATE %s SET %s = %s - interval '2 hours'" % (table, column, column))
        out = self.mesh("work", "list", "--json", "--stale-after", "1h")
        rows = json.loads(out.stdout)
        self.assertTrue(all(r["stale"] for r in rows), rows)
        out = self.mesh("work", "show", "1")
        self.assertIn("plan     : fiche cat-export", out.stdout)
        self.assertIn("epic : catalogue", out.stdout)
        out = self.mesh("work", "close", "2", "--superseded-by", "1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("remplacé par #1", out.stdout)
        out = self.mesh("work", "close", "2", "--abandoned")
        self.assertEqual(out.returncode, 1)
        out = self.mesh("work", "close", "1")
        self.assertEqual(out.returncode, 2)                      # option requise
        env = self.gh_env()
        out = self.mesh("work", "project-github", "--repo", REPO, "--dry-run", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("[essai]", out.stdout)
        self.assertIn("à créer", out.stdout)
        with open(self.gh_state, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["repos"][REPO]["issues"], [])
        out = self.mesh("work", "sync-github", "--repo", REPO, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("1 lot(s) fermé(s)", out.stdout)
        self.assertEqual(work.get(self.db, 1)["state"], "merged")
        out = self.mesh("work", "project-github", "--repo", REPO, "--json", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout)
        self.assertEqual({i["action"] for i in report["issues"]}, {"create"})
        out = self.mesh("work", "sync-github", "--repo", REPO,
                        env=self.env(AMEESH_GH_BIN="/nulle/part/gh"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("gh", out.stderr)


# ==========================================================================
# fusion constatée par le contenu (dépôt git jetable)
# ==========================================================================

class ContentMergeTest(_PlanDb):
    """La fusion se prouve par le contenu, pas par le nom : commit gelé ancêtre
    de la cible, sinon patch-id (rebase, cherry-pick, squash), sinon la PR."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.join(self.make_tmp(), "depot")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.commit("README", "acme\n", "initial")

    def commit(self, path: str, text: str, message: str) -> str:
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a", encoding="utf-8") as fh:
            fh.write(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def frozen(self, title: str, sha: str, **kwargs) -> dict:
        item = work.add(self.db, title=title, **kwargs)
        for state in ("build", "qa"):
            work.move(self.db, item["id"], state)
        work.milestone(self.db, item["id"], "frozen", sha=sha, actor="ouvrier")
        return item

    def run_sync(self, **kwargs) -> dict:
        report = plan_git.sync_merges(self.db, self.repo, target="main", **kwargs)
        return {r["work_item"]: r for r in report["results"]}

    def test_nom_change_mais_gel_ancetre_de_la_cible(self):
        # gelé sur « revue-garde-acces », fusionné depuis « GARDE-ACCES » (commit de fusion)
        git(self.repo, "checkout", "-q", "-b", "revue-garde-acces")
        sha = self.commit("src/garde.sql", "guard\n", "garde d'accès")
        git(self.repo, "branch", "-q", "-m", "GARDE-ACCES")
        git(self.repo, "checkout", "-q", "main")
        self.commit("src/autre.py", "x = 1\n", "autre travail")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "fusion GARDE-ACCES", "GARDE-ACCES")
        item = self.frozen("revue-garde-acces", sha)
        dry = self.run_sync(dry_run=True)
        self.assertEqual(dry[item["id"]]["result"], "would-merge")
        self.assertEqual(work.get(self.db, item["id"])["state"], "qa")
        result = self.run_sync()[item["id"]]
        self.assertEqual((result["result"], result["how"]), ("merged", "ancestor"))
        self.assertEqual(work.get(self.db, item["id"])["state"], "merged")
        ms = [m for m in work.milestones(self.db, item["id"]) if m["kind"] == "merged"][0]
        self.assertEqual((ms["sha"], ms["actor"]), (sha, "git:main"))
        # idempotent
        self.assertEqual(self.run_sync(), {})

    def test_rebase_cherry_pick_et_squash_par_patch_id(self):
        git(self.repo, "checkout", "-q", "-b", "lot/a")
        self.commit("src/a.py", "a = 1\n", "a1")
        sha_a = self.commit("src/a.py", "a = 2\n", "a2")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "checkout", "-q", "-b", "lot/b")
        self.commit("src/b.py", "b = 1\n", "b1")
        sha_b = self.commit("src/b.py", "b = 2\n", "b2")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "checkout", "-q", "-b", "lot/jamais")
        sha_never = self.commit("src/n.py", "n = 1\n", "jamais fusionné")
        git(self.repo, "checkout", "-q", "main")
        self.commit("src/autre.py", "x = 1\n", "la cible avance")
        # a : rebase (cherry-pick des deux commits, nouveaux SHA)
        git(self.repo, "cherry-pick", "lot/a~1", "lot/a")
        # b : squash en un seul commit
        git(self.repo, "merge", "-q", "--squash", "lot/b")
        squashed = self.commit("NOTES", "b\n", "lot b (squash)")
        a = self.frozen("lot a", sha_a)
        b = self.frozen("lot b", sha_b)
        never = self.frozen("jamais", sha_never)
        results = self.run_sync()
        self.assertEqual((results[a["id"]]["result"], results[a["id"]]["how"]),
                         ("merged", "patch-id"))
        self.assertEqual(results[a["id"]]["ref"], git(self.repo, "rev-parse", "main~1"))
        self.assertEqual((results[b["id"]]["result"], results[b["id"]]["how"]),
                         ("open", None))   # squash + autre fichier : diff différent
        self.assertEqual(results[never["id"]]["result"], "open")
        self.assertIn("absents", results[never["id"]]["detail"])
        self.assertEqual(work.get(self.db, never["id"])["state"], "qa")
        # un squash exact (même diff) est reconnu
        git(self.repo, "reset", "-q", "--hard", "main~1")
        git(self.repo, "merge", "-q", "--squash", "lot/b")
        git(self.repo, "commit", "-q", "-m", "lot b (squash exact)")
        result = self.run_sync()[b["id"]]
        self.assertEqual((result["result"], result["how"]), ("merged", "squash"))
        self.assertEqual(result["ref"], git(self.repo, "rev-parse", "main"))
        self.assertNotEqual(squashed, result["ref"])

    def test_bloque_corrige_puis_fusionne_sous_un_autre_sha(self):
        git(self.repo, "checkout", "-q", "-b", "lot/c")
        first = self.commit("src/c.py", "c = 1\n", "c1")
        item = self.frozen("lot c", first)
        work.milestone(self.db, item["id"], "verdict", verdict="blocked", sha=first,
                       actor="relecteur")
        work.move(self.db, item["id"], "build")
        self.commit("src/c.py", "c = 2  # correction\n", "correction")   # non redéclarée
        git(self.repo, "checkout", "-q", "main")
        self.commit("src/autre.py", "x = 1\n", "la cible avance")
        git(self.repo, "rebase", "-q", "main", "lot/c")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--ff-only", "lot/c")
        result = self.run_sync()[item["id"]]
        self.assertEqual((result["result"], result["how"]), ("merged", "patch-id"))
        self.assertEqual(work.get(self.db, item["id"])["state"], "merged")
        # un lot absorbé par un autre se clôt explicitement, jamais rouvert
        other = self.frozen("absorbé", first)
        work.close(self.db, other["id"], superseded_by=item["id"])
        self.assertNotIn(other["id"], self.run_sync())
        self.assertEqual(work.get(self.db, other["id"])["state"], "closed")

    # -- revue codex1 (750c221), sondes reprises telles quelles -----------------
    def test_seul_le_dernier_gel_compte(self):
        """P2-1 : un gel antérieur déjà fusionné ne ferme pas un lot regelé
        sur un travail non fusionné."""
        git(self.repo, "checkout", "-q", "-b", "lot/x")
        first = self.commit("src/x.py", "x=1\n", "première version")
        item = self.frozen("x", first)
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--ff-only", "lot/x")
        git(self.repo, "checkout", "-q", "lot/x")
        latest = self.commit("src/x.py", "x=2\n", "nouveau travail gelé")
        work.milestone(self.db, item["id"], "frozen", sha=latest, actor="ouvrier")
        result = self.run_sync()[item["id"]]
        self.assertEqual(result["result"], "open", result)
        self.assertEqual(result["sha"], latest)
        self.assertEqual(work.get(self.db, item["id"])["state"], "qa")
        # le dernier gel fusionné à son tour : fermé
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--ff-only", "lot/x")
        self.assertEqual(self.run_sync()[item["id"]]["result"], "merged")

    def test_nouveau_gel_concurrent_empeche_la_fermeture(self):
        """La fermeture est conditionnée au gel examiné (P2-1, course)."""
        git(self.repo, "checkout", "-q", "-b", "lot/y")
        sha = self.commit("src/y.py", "y=1\n", "y")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--ff-only", "lot/y")
        item = self.frozen("y", sha)
        examined = [m for m in work.milestones(self.db, item["id"]) if m["kind"] == "frozen"][0]
        work.milestone(self.db, item["id"], "frozen", sha="d" * 40, actor="ouvrier")
        done = work.close_merged(self.db, item["id"], sha=sha, frozen_id=int(examined["id"]))
        self.assertEqual(done["result"], "refrozen")
        self.assertEqual(work.get(self.db, item["id"])["state"], "qa")

    def _wait_lock(self, needle: str) -> None:
        """Attend qu'une autre connexion soit RÉELLEMENT bloquée sur un verrou."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = self.db.query(
                "SELECT count(*) AS n FROM pg_stat_activity"
                " WHERE wait_event_type = 'Lock' AND query LIKE %s", ("%" + needle + "%",))
            if rows[0]["n"]:
                return
            time.sleep(0.05)
        self.fail("aucune attente de verrou Postgres sur %r" % needle)

    def test_gel_valide_pendant_que_la_fermeture_attend_le_verrou(self):
        """Sonde codex1 (afcfb21) : connexion A tient la ligne du lot ; la
        fermeture (connexion B) attend le verrou ; A valide un nouveau gel ;
        B ne doit PAS fermer (instantané relu après l'attente)."""
        git(self.repo, "checkout", "-q", "-b", "lot/course")
        sha = self.commit("course.py", "x=1\n", "ancien gel")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--ff-only", "lot/course")
        item = self.frozen("course", sha)
        examined = [m for m in work.milestones(self.db, item["id"]) if m["kind"] == "frozen"][0]
        blocker, closer = db_mod.connect(self.cfg), db_mod.connect(self.cfg)
        result, errors = [], []

        def close():
            try:
                result.append(work.close_merged(closer, item["id"], sha=sha,
                                                frozen_id=int(examined["id"])))
            except Exception as exc:  # noqa: BLE001 - rapporté par le test
                errors.append(repr(exc))

        thread = threading.Thread(target=close)
        try:
            with blocker.transaction() as tx:
                tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                         (item["id"],))
                thread.start()
                self._wait_lock("SET state = 'merged'")
                tx.query("INSERT INTO work_item_milestones (work_item_id, kind, sha)"
                         " VALUES (%s, 'frozen', %s) RETURNING id", (item["id"], "d" * 40))
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result[0]["result"], "refrozen", result)
            self.assertEqual(work.get(self.db, item["id"])["state"], "qa")
            self.assertEqual([m["kind"] for m in work.milestones(self.db, item["id"])
                              if m["kind"] == "merged"], [])
        finally:
            thread.join(10)
            blocker.close()
            closer.close()

    def test_un_gel_attend_la_fermeture_en_cours(self):
        """Ordre inverse : tant qu'une transaction tient la ligne du lot (comme
        la fermeture), `work milestone frozen` attend le verrou, puis s'écrit
        après elle — jamais entre la relecture du gel et la fermeture."""
        item = self.frozen("ordre", "e" * 40)
        writer = db_mod.connect(self.cfg)
        done, errors = [], []

        def freeze():
            try:
                done.append(work.milestone(writer, item["id"], "frozen", sha="f" * 40))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        thread = threading.Thread(target=freeze)
        blocker = db_mod.connect(self.cfg)
        try:
            with blocker.transaction() as tx:
                tx.query("UPDATE work_items SET state = 'merged' WHERE id = %s RETURNING id",
                         (item["id"],))
                thread.start()
                self._wait_lock("FOR NO KEY UPDATE")
                self.assertEqual(done, [])
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(done[0]["sha"], "f" * 40)
            kinds = [m["kind"] for m in reversed(work.milestones(self.db, item["id"]))]
            self.assertLess(kinds.index("merged"), len(kinds) - 1)
            self.assertEqual(kinds[-1], "frozen")
        finally:
            thread.join(10)
            blocker.close()
            writer.close()

    def test_patch_id_exact_sans_normaliser_les_espaces(self):
        """P2-2 : `"a b"` et `"ab"` ne sont pas le même contenu."""
        git(self.repo, "checkout", "-q", "-b", "lot/chaine")
        frozen = self.commit("src/value.py", 'value = "a b"\n', "chaîne requise")
        git(self.repo, "checkout", "-q", "main")
        self.commit("src/value.py", 'value = "ab"\n', "autre chaîne")
        result = plan_git.probe(self.repo, frozen, "main")
        self.assertFalse(result["merged"], result)
        # le même contenu, octet pour octet, est toujours reconnu (cherry-pick)
        git(self.repo, "reset", "-q", "--hard", "main~1")
        self.commit("src/autre.py", "z = 0\n", "la cible avance")
        git(self.repo, "cherry-pick", frozen)
        result = plan_git.probe(self.repo, frozen, "main")
        self.assertEqual((result["merged"], result["how"]), (True, "patch-id"), result)

    def test_sans_commit_ni_cible(self):
        bare = work.add(self.db, title="sans gel")
        self.assertEqual(self.run_sync()[bare["id"]]["result"], "no-commit")
        absent = self.frozen("commit inconnu", "c" * 40)
        self.assertIn("absent du dépôt", self.run_sync()[absent["id"]]["detail"])
        with self.assertRaises(plan_git.MergeProbeError):
            plan_git.sync_merges(self.db, self.repo, target="origin/nulle-part")

    def test_cli_contenu_puis_pr(self):
        self.sync()
        git(self.repo, "checkout", "-q", "-b", "lot/x")
        sha = self.commit("src/x.py", "x = 1\n", "x")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--ff-only", "lot/x")
        by_content = self.frozen("par le contenu", sha)
        by_pr = work.add(self.db, title="par la PR", package="cat-export")
        gh_state = os.path.join(self.tmp, "gh-state.json")
        with open(gh_state, "w", encoding="utf-8") as fh:
            json.dump({"prs": {"%s#9" % REPO: {
                "state": "MERGED", "headRefName": "lot/cat-export", "body": "",
                "mergeCommit": {"oid": SHA}, "mergedBy": {"login": "bruno"},
                "mergedAt": "2999-01-01T00:00:00Z"}}}, fh)
        env = self.env(AMEESH_GH_BIN=os.path.join(FAKEBIN, "gh"), AMEESH_FAKE_GH_STATE=gh_state)
        out = self.mesh("work", "sync-merges", "--git-dir", self.repo, "--target", "main",
                        "--repo", REPO, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("1 lot(s) fermé(s) par le contenu", out.stdout)
        self.assertIn("1 lot(s) fermé(s) par leur PR fusionnée", out.stdout)
        self.assertEqual(work.get(self.db, by_content["id"])["state"], "merged")
        self.assertEqual(work.get(self.db, by_pr["id"])["state"], "merged")
        out = self.mesh("work", "sync-merges", "--git-dir", self.repo, "--target", "absente")
        self.assertEqual(out.returncode, 1)
        self.assertIn("introuvable", out.stderr)


if __name__ == "__main__":
    unittest.main()
