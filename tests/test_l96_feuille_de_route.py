# SPDX-License-Identifier: AGPL-3.0-only
"""L96 : feuille de route — dates prévues, engagements datés, propositions
tirées des décisions, « qui avance sur quoi », Gantt (texte, JSON, page) et
alerte `engagement_overdue` poussée par notify.

Tests sans base (jours, propositions, rendu du Gantt, règle « cœur
générique »), puis sur Postgres réel.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
import unittest

from ameesh import (exploitation, mail, notify, progress, projects, registry, roadmap,
                    roadmap_dev, roadmap_propose, storage, work)

from .support import PgTestCase

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src",
                   "ameesh")


def ts(day: str, hour: int = 12) -> float:
    """Un instant local du jour ISO (midi par défaut)."""
    d = _dt.date.fromisoformat(day)
    return time.mktime(_dt.datetime(d.year, d.month, d.day, hour).timetuple())


#: samedi 2026-10-10, midi (le jour des demandes du propriétaire)
NOW = ts("2026-10-10")

DECISION = """---
type: Decision
title: "Comptes séparés par organisation"
status: proposed
decision_date: 2026-10-10
---

# Décision

Chaque organisation a ses comptes. On fera ça lundi, avec la bascule des clés.

# Conséquences

- Lots L97–L99 ; même vague que L95.
- Mise en production prévue pour le 2026-10-20.
- Décidé après le 2026-10-01 : ancienne date, ignorée.
"""


class JoursTest(unittest.TestCase):

    def test_parse_day(self):
        self.assertEqual(roadmap.parse_day("2026-10-12"), "2026-10-12")
        self.assertEqual(roadmap.parse_day("lundi", now=NOW), "2026-10-12")
        self.assertEqual(roadmap.parse_day("samedi", now=NOW), "2026-10-17")  # jamais aujourd'hui
        self.assertEqual(roadmap.parse_day("demain", now=NOW), "2026-10-11")
        self.assertEqual(roadmap.parse_day("aujourd'hui", now=NOW), "2026-10-10")
        self.assertIsNone(roadmap.parse_day("-", allow_clear=True))
        for bad in ("12/10", "2026-13-01", "bientôt", "-"):
            with self.assertRaises(roadmap.RoadmapError):
                roadmap.parse_day(bad, now=NOW)


class ProposeTest(unittest.TestCase):
    """Lecture des décisions : jalon de décision, « lundi », dates, lots,
    dépendances ; et le corps d'une tâche."""

    def test_decision(self):
        props = roadmap_propose.from_decision(
            {"type": "Decision", "title": "Comptes séparés par organisation",
             "status": "proposed", "decision_date": "2026-10-10"},
            DECISION.split("---", 2)[2], ref="canon:decisions/0099.md@abc",
            origin="canon:decisions/0099.md", base=_dt.date(2026, 10, 10))
        kinds = [(p["kind"], p["due"]) for p in props]
        self.assertIn(("decision", None), kinds)
        self.assertIn(("commitment", "2026-10-12"), kinds)       # « on fera ça lundi »
        self.assertIn(("commitment", "2026-10-20"), kinds)
        self.assertNotIn(("commitment", "2026-10-01"), kinds)    # passée : ignorée
        lots = next(p for p in props if p["lots"])
        self.assertEqual(lots["lots"], ["L97", "L98", "L99"])
        self.assertEqual(lots["depends_on"], ["L95"])
        self.assertIsNone(lots["due"])
        for p in props:
            self.assertEqual(p["source_kind"], "decision")
            self.assertEqual(p["source_ref"], "canon:decisions/0099.md@abc")
            self.assertTrue(p["command"].startswith("ameesh plan add "), p["command"])
        monday = next(p for p in props if p["due"] == "2026-10-12")
        self.assertIn("--pour 2026-10-12", monday["command"])
        # clés stables : une relecture rend les mêmes
        again = roadmap_propose.from_decision(
            {"title": "Comptes séparés par organisation", "status": "proposed",
             "decision_date": "2026-10-10"}, DECISION.split("---", 2)[2],
            ref="autre@def", origin="canon:decisions/0099.md", base=_dt.date(2026, 10, 11))
        self.assertEqual({p["key"] for p in props}, {p["key"] for p in again})

    def test_decision_stable_sans_question(self):
        props = roadmap_propose.from_decision(
            {"title": "x", "status": "stable"}, "Rien de daté ici.", ref="r", origin="o",
            base=_dt.date(2026, 10, 10))
        self.assertEqual(props, [])

    def test_corps_d_une_tache(self):
        item = {"id": 65, "title": "Comptes séparés par organisation", "app": "ameesh",
                "assignee": "w1", "created_ts": ts("2026-10-09"),
                "body": "Contexte long. Prévu le lundi 2026-10-12, après la revue."}
        props = roadmap_propose.from_task(item, base=_dt.date(2026, 10, 10), has_due=set())
        self.assertEqual(len(props), 1)
        p = props[0]
        self.assertEqual((p["due"], p["lot"], p["source_kind"], p["source_ref"]),
                         ("2026-10-12", 65, "task", "#65"))
        self.assertIn("ameesh work plan 65 --livraison 2026-10-12", p["command"])
        # déjà porté par un engagement : rien à proposer
        self.assertEqual(roadmap_propose.from_task(
            item, base=_dt.date(2026, 10, 10), has_due={(65, "2026-10-12")}), [])

    def test_dossier_de_decisions(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "0099-comptes.md"), "w", encoding="utf-8") as fh:
                fh.write(DECISION)
            with open(os.path.join(tmp, "notes.md"), "w", encoding="utf-8") as fh:
                fh.write("---\ntype: Study\ntitle: t\n---\nOn fera ça lundi.\n")
            found = roadmap_propose._read_dir(tmp)
        self.assertEqual(len(found), 1)
        self.assertIn("On fera ça lundi", found[0][1])


class CoeurGeneriqueTest(unittest.TestCase):
    """Règle « cœur générique » : le code de L96 ne nomme aucun état ni jalon
    de métier ; les correspondances tiennent dans UNE table."""

    FILES = ("roadmap.py", "roadmap_propose.py", os.path.join("storage", "postgres",
                                                              "roadmap.py"))
    FORBIDDEN = re.compile(r"""["'](intake|build|qa|merged|promoted|frozen|closed|"""
                           r"""waiting_human|review|approved|merge)["']|\bgel\b|\bfusion|"""
                           r"""\bPR\b|git-merge|pr_ref""")

    def test_aucun_litteral_de_metier(self):
        for name in self.FILES:
            with open(os.path.join(SRC, name), encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    self.assertIsNone(self.FORBIDDEN.search(line),
                                      "%s:%d : %s" % (name, number, line.strip()))

    def test_une_seule_table(self):
        table = roadmap_dev.CORRESPONDANCES
        self.assertEqual(set(table["jalons"]), {"demandee", "soumise", "verdict", "livree"})
        self.assertTrue(roadmap_dev.livree(work.MERGED_STATES[0]))
        self.assertEqual(roadmap_dev.etat_generique("approved"), "validee")


class GanttTextTest(unittest.TestCase):

    def view(self):
        return {
            "schema": roadmap.SCHEMA, "generated_ts": NOW, "today": "2026-10-10",
            "project": None,
            "window": {"from": "2026-10-03", "to": "2026-10-24"},
            "milestones": [{"kind": "milestone", "id": "v1", "title": "Mise en service",
                            "planned": {"start": None, "end": None, "delivery": "2026-10-20",
                                        "source": "canon"},
                            "real": {"debut": None, "livree": None}, "progress": 0.5,
                            "units_total": 2, "units_delivered": 1, "late": None,
                            "source": {"label": "canon c:plan/v1.md@1", "url": None}}],
            "epics": [],
            "tasks": [{"kind": "task", "id": 65, "title": "Comptes séparés par organisation",
                       "state": "en_cours", "assignee": "w1", "epic": None,
                       "planned": {"start": "2026-10-05", "end": "2026-10-08",
                                   "delivery": "2026-10-09", "source": "conversation",
                                   "by": "human:o"},
                       "real": {"demandee": ts("2026-10-04"), "debut": ts("2026-10-05"),
                                "soumise": None, "verdict": None, "verdict_kind": None,
                                "livree": None},
                       "late": {"what": "livraison", "due": "2026-10-09", "days": 1,
                                "open": True},
                       "source": {"label": "plan conversation", "url": None},
                       "commitments": [3]}],
            "commitments": [{"kind": "commitment", "id": 3, "what": "Comptes séparés",
                             "due": "2026-10-12", "status": "open", "owner": "w1",
                             "project": "ameesh", "lot": 65, "depends_on": ["L95"],
                             "fulfilled": False, "late": None,
                             "source": {"label": "conversation 2026-10-10", "url": None}}],
            "decisions": [{"kind": "decision", "id": 4, "what": "Mot par défaut",
                           "due": None, "status": "open", "owner": "human:o",
                           "source": {"label": "décision 0035", "url": None}}],
            "late": [{"kind": "task", "id": 65, "title": "Comptes séparés", "what": "livraison",
                      "due": "2026-10-09", "days": 1, "open": True}],
            "truncated": {},
        }

    def test_rendu(self):
        text = roadmap.format_gantt(self.view(), 100)
        lines = text.splitlines()
        self.assertTrue(all(len(l) <= 100 for l in lines), text)
        bar = next(l for l in lines if l.startswith("#65 "))
        self.assertIn(roadmap.GLYPHS["real"], bar)
        self.assertIn(roadmap.GLYPHS["late"], bar)            # après la livraison prévue
        self.assertIn(roadmap.GLYPHS["delivery"], bar)
        self.assertIn(roadmap.GLYPHS["today"], next(l for l in lines if l.startswith("v1 ")))
        self.assertIn("RETARD 1 j (livraison 09/10)", text)
        self.assertIn("source : plan conversation", text)
        self.assertIn("engagement(s) e3", text)
        self.assertIn("DÉCISIONS ATTENDUES (1)", text)
        self.assertIn("dépend de L95", text)
        self.assertIn("EN RETARD (1)", text)
        e3 = next(l for l in lines if l.startswith("e3 "))
        self.assertIn(roadmap.GLYPHS["due"], e3)
        for width in (60, 40):
            narrow = roadmap.format_gantt(self.view(), width)
            self.assertTrue(all(len(l) <= width for l in narrow.splitlines()), narrow)


def write_canon(root: str) -> None:
    files = {
        "plan/v1.md": "---\ntype: WorkPackage\ntitle: Mise en service\nkind: milestone\n"
                      "responsible: human:alice\ndate: 2026-10-20\n---\n",
        "plan/comptes.md": "---\ntype: WorkPackage\ntitle: Comptes\nkind: epic\n"
                           "parent: v1\nresponsible: human:alice\nstart: 2026-10-12\n"
                           "end: 12/10\n---\n",
        "membres/alice.md": "---\ntype: Member\ntitle: alice\nroles: [owner]\n---\n",
        "decisions/0099-comptes.md": DECISION,
    }
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


class CanonDatesTest(unittest.TestCase):

    def test_dates_et_decisions_du_canon(self):
        import tempfile
        from ameesh import canon
        with tempfile.TemporaryDirectory() as tmp:
            write_canon(tmp)
            c = canon.load(tmp, untrusted=True)
            findings = canon.validate(c) if hasattr(canon, "validate") else []
        by_id = {p.id: p for p in c.packages}
        self.assertEqual(by_id["v1"].delivery, "2026-10-20")       # `date` d'un jalon
        self.assertEqual((by_id["comptes"].start, by_id["comptes"].end),
                         ("2026-10-12", None))                     # date illisible ignorée
        codes = [f.code for f in list(c.load_findings) + list(findings or [])]
        self.assertIn("package-date-invalid", codes)
        self.assertEqual(len(c.decisions), 1)
        note = c.decisions[0]
        self.assertEqual(note.fiche.data["status"], "proposed")
        self.assertIn("On fera ça lundi", note.body)
        self.assertNotIn("type: Decision", note.body)


class RoadmapPgTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE commitments RESTART IDENTITY CASCADE")

    def _lot(self, title="Comptes séparés par organisation", **kw):
        kw.setdefault("app", "ameesh")
        return work.add(self.db, title=title, **kw)

    def test_plan_d_une_tache_et_d_une_fiche(self):
        lot = self._lot(body="Prévu le lundi 2026-10-12.")
        out = self.mesh("work", "plan", str(lot["id"]), "--debut", "2026-10-10", "--fin",
                        "2026-10-11", "--livraison", "2026-10-12", "--source",
                        "conversation du 2026-10-10")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("livraison 2026-10-12", out.stdout)
        row = storage.of(self.db).roadmap.item_plans([lot["id"]])[0]
        self.assertEqual((row["planned_start"], row["planned_end"], row["planned_delivery"],
                          row["planned_source"]),
                         ("2026-10-10", "2026-10-11", "2026-10-12",
                          "conversation du 2026-10-10"))
        # la replanification n'est pas une activité de la tâche
        before = work.get(self.db, lot["id"])["updated_ts"]
        roadmap.plan(self.db, lot["id"], end="-", actor="human:o")
        self.assertIsNone(storage.of(self.db).roadmap.item_plans([lot["id"]])[0]["planned_end"])
        self.assertEqual(work.get(self.db, lot["id"])["updated_ts"], before)
        out = self.mesh("work", "show", str(lot["id"]))
        self.assertIn("prévu    : début 2026-10-10 · fin — · livraison 2026-10-12", out.stdout)
        # ordre : début après fin refusé ; date illisible refusée
        with self.assertRaises(roadmap.RoadmapError):
            roadmap.plan(self.db, lot["id"], start="2026-10-20", end="2026-10-11")
        out = self.mesh("work", "plan", str(lot["id"]), "--fin", "12/10")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("date illisible", out.stderr)
        # une fiche : dates posées dans ameesh, à côté de celles du canon
        st = storage.of(self.db)
        st.packages.upsert({"id": "v1", "kind": "milestone", "title": "Mise en service",
                            "responsible": "human:o", "canon_ref": "c:plan/v1.md@1",
                            "delivery_on": "2026-10-20"})
        self.assertEqual(st.packages.get("v1")["delivery_on"], "2026-10-20")
        out = self.mesh("work", "plan", "v1", "--debut", "2026-10-13")
        self.assertEqual(out.returncode, 0, out.stderr)
        pkg = st.packages.get("v1")
        self.assertEqual((pkg["planned_start"], pkg["delivery_on"]), ("2026-10-13", "2026-10-20"))
        out = self.mesh("work", "plan", "inconnue", "--fin", "2026-10-13")
        self.assertEqual(out.returncode, 1)
        self.assertIn("ni tâche ni fiche", out.stderr)

    def test_canon_sync_recopie_les_dates(self):
        import tempfile
        from ameesh import canon, canon_sync
        with tempfile.TemporaryDirectory() as tmp:
            write_canon(tmp)
            c = canon.load(tmp, untrusted=True)
            canon_sync.sync_packages(self.db, c, [])
            props = roadmap_propose.propose(self.db, canons=[c], tasks=False, now=NOW)
        st = storage.of(self.db)
        self.assertEqual(st.packages.get("v1")["delivery_on"], "2026-10-20")
        self.assertEqual(st.packages.get("comptes")["start_on"], "2026-10-12")
        snap = progress.snapshot(self.db, None, gantt=False)
        self.assertIsNone(snap["roadmap"])
        self.assertEqual([(m["id"], m["date"]) for m in snap["milestones"]],
                         [("v1", "2026-10-20")])
        self.assertIn("v1 Mise en service", roadmap.format_gantt(roadmap.build(self.db), 100))
        kinds = sorted((p["kind"], p["due"] or "") for p in props["proposals"])
        self.assertIn(("decision", ""), kinds)
        self.assertIn(("commitment", "2026-10-12"), kinds)
        self.assertTrue(all(p["source_ref"].startswith("canon:") or ":" in p["source_ref"]
                            for p in props["proposals"]))

    def test_engagements_cli(self):
        lot = self._lot(assignee=None)
        out = self.mesh("plan", "add", "Comptes séparés par organisation", "--pour",
                        "2026-10-12", "--lot", str(lot["id"]), "--source",
                        "conversation 2026-10-10", "--depend-de", "L95")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("engagement e1 enregistré", out.stdout)
        c = storage.of(self.db).roadmap.commitments()[0]
        self.assertEqual((c["due_on"], c["work_item_id"], c["project"], c["source_kind"],
                          c["depends_on"]),
                         ("2026-10-12", lot["id"], "ameesh", "conversation", ["L95"]))
        out = self.mesh("plan", "add", "sans date")
        self.assertEqual(out.returncode, 1)
        self.assertIn("un engagement a une date", out.stderr)
        out = self.mesh("plan", "add", "Mot par défaut du cœur", "--decision", "--source-type",
                        "decision", "--source", "étude cœur générique, question 1")
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.mesh("plan", "list")
        self.assertIn("e1    2026-10-12 open      engagement Comptes séparés", out.stdout)
        self.assertIn("sans date  open      décision   Mot par défaut", out.stdout)
        self.assertEqual(self.mesh("plan", "done", "1").returncode, 0)
        out = self.mesh("plan", "done", "1")
        self.assertIn("déjà done", out.stderr)
        self.assertNotIn("e1 ", self.mesh("plan", "list").stdout)
        self.assertIn("e1 ", self.mesh("plan", "list", "--all").stdout)

    def test_propositions_puis_validation(self):
        """Le lot #65 réel : une date écrite seulement dans le corps."""
        lot = self._lot(body="Prévu le lundi 2026-10-12 (dit en conversation).",
                        assignee=None)
        view = roadmap_propose.propose(self.db, canon=False, now=NOW)
        props = [p for p in view["proposals"] if p["lot"] == lot["id"]]
        self.assertEqual([p["due"] for p in props], ["2026-10-12"])
        self.assertFalse(storage.of(self.db).roadmap.commitments())   # rien sans --record
        created = roadmap_propose.record(self.db, view["proposals"])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["status"], "proposed")
        again = roadmap_propose.propose(self.db, canon=False, now=NOW)
        self.assertEqual(again["proposals"], [])     # la date est portée : plus rien à proposer
        # une proposition n'est pas un engagement : pas d'alerte avant validation
        late = NOW + 5 * 86400
        self.assertEqual(roadmap.overdue(self.db, now=late), [])
        roadmap.accept(self.db, created[0]["id"], actor="human:o")
        rows = roadmap.overdue(self.db, now=late)
        self.assertEqual([(r["reason"], r["commitment"], r["lot"], r["days"]) for r in rows],
                         [("engagement", created[0]["id"], lot["id"], 3)])
        # via la CLI, avec un dossier de décisions
        folder = os.path.join(self.tmp, "decisions")
        os.makedirs(folder)
        with open(os.path.join(folder, "0099-comptes.md"), "w", encoding="utf-8") as fh:
            fh.write(DECISION)
        out = self.mesh("plan", "propose", "--no-canon", "--from", folder)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("jalon de décision sans date — Décider : Comptes séparés", out.stdout)
        self.assertIn("pour valider : ameesh plan add", out.stdout)
        self.assertIn("rien n'est enregistré", out.stdout)
        out = self.mesh("plan", "propose", "--no-canon", "--from", folder, "--record", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        recorded = json.loads(out.stdout)["recorded"]
        self.assertGreaterEqual(len(recorded), 4)
        lots = next(c for c in storage.of(self.db).roadmap.commitments(ids=recorded)
                    if c["due_on"] is None and c["kind"] == "commitment")
        out = self.mesh("plan", "accept", str(lots["id"]))
        self.assertEqual(out.returncode, 1)
        self.assertIn("sans date", out.stderr)
        out = self.mesh("plan", "accept", str(lots["id"]), "--pour", "2026-10-30")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_gantt_prevu_face_au_reel(self):
        st = storage.of(self.db)
        st.packages.upsert({"id": "v1", "kind": "milestone", "title": "Mise en service",
                            "responsible": "human:o", "canon_ref": "c:plan/v1.md@1",
                            "delivery_on": "2026-10-20"})
        st.packages.upsert({"id": "comptes", "kind": "epic", "title": "Comptes",
                            "parent": "v1", "responsible": "human:o",
                            "canon_ref": "c:plan/comptes.md@1", "start_on": "2026-10-01",
                            "end_on": "2026-10-09"})
        done = self._lot("Lot livré", package="comptes")
        for state in ("build", "qa"):
            work.move(self.db, done["id"], state)
        work.milestone(self.db, done["id"], roadmap_dev.jalon("soumise"), actor="auteur")
        work.milestone(self.db, done["id"], "verdict", verdict="ok", actor="relecteur")
        work.close_merged(self.db, done["id"], actor="porte")
        late = self._lot("Lot en retard", package="comptes")
        work.move(self.db, late["id"], "build")
        roadmap.plan(self.db, late["id"], start="2026-10-01", delivery="2026-10-05")
        waiting = self._lot("Choisir le mot par défaut")
        work.move(self.db, waiting["id"], "waiting_human")
        roadmap.add_commitment(self.db, "Comptes séparés", due="2026-10-12", lot=late["id"],
                               source_ref="conversation 2026-10-10", now=NOW)
        now = time.time()
        view = roadmap.build(self.db, now=now)
        self.assertEqual(view["schema"], "ameesh-roadmap/1")
        tasks = {t["id"]: t for t in view["tasks"]}
        d = tasks[done["id"]]
        self.assertEqual(d["state"], "livree")
        for key in ("demandee", "debut", "soumise", "verdict", "livree"):
            self.assertIsNotNone(d["real"][key], key)
        self.assertEqual(d["real"]["verdict_kind"], "ok")
        self.assertEqual(d["epic"], "comptes")
        lt = tasks[late["id"]]
        self.assertEqual(lt["planned"]["delivery"], "2026-10-05")
        self.assertTrue(lt["late"]["open"])
        self.assertEqual(lt["late"]["what"], "livraison")
        self.assertEqual(len(lt["commitments"]), 1)
        self.assertTrue(tasks[waiting["id"]]["waiting_decision"])
        self.assertIn(waiting["id"], [x.get("lot") for x in view["decisions"]])
        epic = next(e for e in view["epics"] if e["id"] == "comptes")
        self.assertEqual(epic["planned"]["source"], "canon")
        self.assertEqual(epic["source"]["ref"], "c:plan/comptes.md@1")
        self.assertTrue(epic["late"]["open"])                 # fin 09/10 passée, 1 lot ouvert
        self.assertEqual((epic["units_total"], epic["units_delivered"]), (2, 1))
        ms = next(m for m in view["milestones"] if m["id"] == "v1")
        self.assertEqual(ms["planned"]["delivery"], "2026-10-20")
        self.assertIn({"kind": "task", "id": late["id"]},
                      [{"kind": l["kind"], "id": l["id"]} for l in view["late"]])
        json.dumps(view)

        out = self.mesh("plan", "show", "--width", "110")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("FEUILLE DE ROUTE", out.stdout)
        self.assertIn("QUI AVANCE SUR QUOI", out.stdout)
        self.assertIn("#%d Lot en retard" % late["id"], out.stdout)
        self.assertIn("RETARD", out.stdout)
        shown = json.loads(self.mesh("plan", "show", "--json").stdout)
        self.assertEqual(shown["schema"], "ameesh-roadmap/1")
        self.assertIn("who", shown)

        # ameesh progress : la frise par défaut, --no-gantt la retire
        out = self.mesh("progress", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        snap = json.loads(out.stdout)
        self.assertEqual(snap["roadmap"]["schema"], "ameesh-roadmap/1")
        self.assertEqual(snap["milestones"][0]["date"], "2026-10-20")
        self.assertIsNone(json.loads(self.mesh("progress", "--json", "--no-gantt").stdout)
                          ["roadmap"])
        self.assertIn("FEUILLE DE ROUTE", self.mesh("progress").stdout)
        self.assertNotIn("FEUILLE DE ROUTE", self.mesh("progress", "--no-gantt").stdout)
        html = progress.render_html(snap)
        self.assertIn('id="roadmap-section"', html)
        self.assertIn('"ameesh-roadmap/1"', html)
        self.assertNotIn("<script src", html)

    def test_alerte_engagement_overdue(self):
        registry.upsert(self.db, "w1", chantier="ameesh", harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:resp' WHERE name = 'w1'")
        lot = self._lot(assignee="w1")
        roadmap.plan(self.db, lot["id"], delivery="2026-10-12")
        roadmap.add_commitment(self.db, "Comptes séparés", due="2026-10-12", owner="w1",
                               source_ref="conversation", now=NOW)
        roadmap.add_commitment(self.db, "Question au propriétaire", decision=True,
                               owner="human:o")
        # le jour même : rien ; le lendemain : deux alertes
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db,
                                                         now=ts("2026-10-12", 23))
                          if a["type"] == "engagement_overdue"], [])
        alerts = [a for a in exploitation.alerts(self.cfg, self.db, now=ts("2026-10-14"))
                  if a["type"] == "engagement_overdue"]
        self.assertEqual(sorted(a["reason"] for a in alerts), ["date_prevue", "engagement"])
        for a in alerts:
            self.assertEqual((a["agent"], a["responsible"], a["value"]), ("w1", "human:resp", 2))
        self.assertEqual(len({exploitation.alert_key(a) for a in alerts}), 2)
        self.assertIn("engagement_overdue", notify.DEFAULT_TYPES)
        self.assertIn("engagement_overdue", notify.TYPE_LABELS)
        # tâche livrée : l'engagement rattaché est tenu, la date prévue aussi
        for state in ("build", "qa"):
            work.move(self.db, lot["id"], state)
        work.close_merged(self.db, lot["id"])
        reasons = [a["reason"] for a in exploitation.alerts(self.cfg, self.db,
                                                            now=ts("2026-10-14"))
                   if a["type"] == "engagement_overdue"]
        self.assertEqual(reasons, ["engagement"])   # non rattaché à la tâche : reste dû
        roadmap.settle(self.db, 1, "done")
        self.assertFalse([a for a in exploitation.alerts(self.cfg, self.db,
                                                         now=ts("2026-10-14"))
                          if a["type"] == "engagement_overdue"])

    def test_qui_avance_sur_quoi(self):
        registry.upsert(self.db, "w1", chantier="ameesh", harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = 'w1'")
        lot = self._lot(assignee="w1")
        work.move(self.db, lot["id"], "build", actor="w1")
        mail.send(self.db, "w1", "human:o", "Feuille de route : migration écrite.\nDétails…",
                  work_item_id=str(lot["id"]))
        view = projects.snapshot(self.db, paid_harnesses=())
        a = next(x for p in view["projects"] for x in p["agents"] if x["name"] == "w1")
        self.assertEqual(a["project"], "ameesh")
        self.assertEqual(a["lot"]["id"], lot["id"])
        self.assertEqual(a["last_update"]["text"], "Feuille de route : migration écrite.")
        self.assertEqual(a["last_update"]["lot"], lot["id"])
        self.assertIsNotNone(a["on_task_since_ts"])
        text = projects.format_text(view, 120)
        self.assertIn("avancée il y a", text)
        self.assertIn("migration écrite", text)
        text = roadmap.format_gantt(roadmap.build(self.db), 100,
                                    who=roadmap.who(self.db))
        self.assertIn("w1 [ameesh] au repos — #%d" % lot["id"], text)
        self.assertIn("dernière avancée (il y a", text)


if __name__ == "__main__":
    unittest.main()
