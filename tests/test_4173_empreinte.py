# SPDX-License-Identifier: AGPL-3.0-only
"""#4173 (manaty/ameesh#3185, volet 3) : empreinte par lot et ordonnancement
sans chevauchement dans un dépôt.

Acceptation du ticket : l'ordonnanceur ne co-ordonnance jamais deux lots à
empreintes chevauchantes (test de propriété, `ProprieteTest`)."""
from __future__ import annotations

import itertools
import os
import random
import subprocess
import tempfile
import threading
import unittest

from ameesh import db as db_mod, storage, work, work_footprints as fp
from ameesh.storage.postgres.work import WorkItems

from .test_l165_prise_lots import _Lots


class RegleTest(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(fp.normalize("./src/ameesh/"), "src/ameesh")
        self.assertEqual(fp.normalize("src//a/../b.py"), "src/b.py")
        # #4186 : un chemin absolu n'est plus rabattu en relatif — il
        # désignerait un fichier hors du dépôt sous un nom qui existe peut-être.
        for absolute in ("/src//a/../b.py", "/", "~", "~/x", "C:/repo/x.py",
                         "c:x.py", "//server/share/x.py"):
            self.assertIsNone(fp.normalize(absolute), absolute)
        for bad in ("", "  ", "..", "../x", "a/../../x"):
            self.assertIsNone(fp.normalize(bad), bad)

    def test_chevauchement_fichier_dossier_et_prefixe_trompeur(self):
        self.assertTrue(fp.overlaps("src/a.py", "src/a.py"))
        self.assertTrue(fp.overlaps("src/a", "src/a/b.py"))
        self.assertTrue(fp.overlaps("src/a/b.py", "src"))
        self.assertFalse(fp.overlaps("src/a", "src/ab.py"))
        self.assertFalse(fp.overlaps("src/a.py", "src/a.pyc"))
        self.assertFalse(fp.overlaps("docs/x.md", "src/x.md"))

    def test_estimation_depuis_les_chemins_cites(self):
        text = ("Corriger `src/ameesh/backlog.py:932` et ./docs/WORK-FOOTPRINTS.md ;"
                " voir https://example.com/x.py")
        self.assertEqual(fp.estimate(text),
                         ["docs/WORK-FOOTPRINTS.md", "src/ameesh/backlog.py"])


class _Empreintes(_Lots):
    def footprint(self, item, *paths, source="manual"):
        storage.of(self.db).work_footprints.replace(
            int(item["id"]), source, [fp.normalize(p) for p in paths], actor="test")

    def give(self, item, agent="human:proprio"):
        self.db.execute("UPDATE work_items SET assignee = %s WHERE id = %s",
                        (agent, item["id"]))

    def merge(self, item):
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (item["id"],))

    def taken(self, **kw):
        return [t["item"]["id"] for t in self.take(**kw)]


class PriseTest(_Empreintes):
    def test_le_second_attend_la_fusion_du_premier(self):
        self.agent("claude1", team="nexlink")
        premier = self.lot("premier", priority=2)
        second = self.lot("second, prioritaire", priority=1)
        libre = self.lot("ailleurs", priority=2)
        self.footprint(premier, "src/ameesh/backlog.py")
        self.footprint(second, "src/ameesh")
        self.footprint(libre, "docs")
        self.give(premier)

        self.assertNotIn(second["id"],
                         [i["id"] for i in storage.of(self.db).work.unassigned_lots(10)])
        self.assertEqual(self.taken(), [libre["id"]])
        self.assertIsNone(work.get(self.db, second["id"])["assignee"])

        self.merge(premier)
        self.agent("claude2", team="nexlink")
        self.assertEqual(self.taken(), [second["id"]])

    def test_qa_et_build_rendu_occupent_encore(self):
        premier = self.lot("en relecture")
        second = self.lot("attend")
        self.footprint(premier, "a.py")
        self.footprint(second, "a.py")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (premier["id"],))
        self.assertEqual(storage.of(self.db).work_footprints.overlapping(second["id"])[0]["id"],
                         premier["id"])
        self.assertIsNone(WorkItems(self.db).assign(
            second["id"], "claude1", current=None, note="t", actor="t", ready_only=True))

    def test_autre_depot_ou_sans_empreinte_rien_n_attend(self):
        premier = self.lot("premier")
        ailleurs = self.lot("autre depot", app="ameesh")
        inconnu = self.lot("sans empreinte")
        self.footprint(premier, "src")
        self.footprint(ailleurs, "src")
        self.give(premier)
        self.assertEqual(
            sorted(i["id"] for i in storage.of(self.db).work.unassigned_lots(10)),
            sorted([ailleurs["id"], inconnu["id"]]))
        for item in (ailleurs, inconnu):
            self.assertIsNotNone(WorkItems(self.db).assign(
                item["id"], "claude1", current=None, note="t", actor="t", ready_only=True))

    def test_attribution_manuelle_refusee_reattribution_permise(self):
        premier = self.lot("premier")
        second = self.lot("second")
        self.footprint(premier, "src/a.py")
        self.footprint(second, "src/a.py", "src/b.py")
        work.assign(self.db, premier["id"], "human:proprio", actor="test")
        with self.assertRaises(work.WorkError) as ctx:
            work.assign(self.db, second["id"], "human:autre", actor="test")
        self.assertIn("#%d" % premier["id"], str(ctx.exception))
        self.assertIn("src/a.py", str(ctx.exception))
        self.assertIsNone(work.get(self.db, second["id"])["assignee"])
        # le lot déjà en cours se réattribue : il occupe déjà son empreinte
        work.assign(self.db, premier["id"], "human:autre", actor="test")
        # une empreinte corrigée lève l'attente, avec sa ligne au journal
        fp.set_paths(self.db, second["id"], ["src/b.py"], actor="pilote")
        work.assign(self.db, second["id"], "human:proprio", actor="test")
        notes = [e["note"] for e in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (second["id"],))]
        self.assertIn("Footprint (manual): src/b.py", notes)

    def test_prises_concurrentes_une_seule_passe(self):
        lots = [self.lot("concurrent%d" % i) for i in range(2)]
        for lot in lots:
            self.footprint(lot, "src/meme.py")
        barrier = threading.Barrier(2, timeout=15)
        results, errors = [], []

        def take(item, agent):
            connection = db_mod.connect(self.cfg)
            try:
                barrier.wait()
                results.append(WorkItems(connection).assign(
                    item["id"], agent, current=None, note="t", actor="t", ready_only=True))
            except Exception as exc:  # noqa: BLE001 - remonté par l'assertion
                errors.append(exc)
            finally:
                connection.close()

        workers = [threading.Thread(target=take, args=(lot, "agent%d" % i), daemon=True)
                   for i, lot in enumerate(lots)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for r in results if r is not None), 1, results)


class CreationAttribueeTest(_Empreintes):
    """B1 de cx2 : un lot créé attribué occupe aussitôt son empreinte — la
    création est gardée comme l'attribution, dans sa propre transaction."""

    def test_creation_attribuee_refusee_puis_libre_permise(self):
        premier = work.add(self.db, title="premier", app="nexlink",
                           assignee="human:proprio", footprint=["src/a.py"])
        self.assertEqual(fp.paths_of(self.db, premier["id"]), ["src/a.py"])
        before = self.db.query("SELECT count(*)::int AS n FROM work_items")[0]["n"]
        with self.assertRaises(storage.FootprintOverlap) as ctx:
            work.add(self.db, title="second", app="nexlink", assignee="human:autre",
                     footprint=["src"])
        self.assertEqual({r["id"] for r in ctx.exception.overlaps}, {premier["id"]})
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM work_items")[0]["n"],
                         before)
        libre = work.add(self.db, title="second", app="nexlink", footprint=["src"])
        self.assertIsNone(libre["assignee"])
        self.assertEqual(fp.paths_of(self.db, libre["id"]), ["src"])
        # autre dépôt : rien n'attend
        work.add(self.db, title="ailleurs", app="ameesh", assignee="human:autre",
                 footprint=["src"])

    def test_creations_attribuees_concurrentes_une_seule_passe(self):
        barrier = threading.Barrier(2, timeout=15)
        created, refused, errors = [], [], []

        def create(i):
            connection = db_mod.connect(self.cfg)
            try:
                barrier.wait()
                created.append(work.add(connection, title="c%d" % i, app="nexlink",
                                        assignee="human:a%d" % i, footprint=["src/a.py"]))
            except storage.FootprintOverlap as exc:
                refused.append(exc)
            except Exception as exc:  # noqa: BLE001 - remonté par l'assertion
                errors.append(exc)
            finally:
                connection.close()

        workers = [threading.Thread(target=create, args=(i,), daemon=True) for i in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        self.assertEqual(errors, [])
        self.assertEqual((len(created), len(refused)), (1, 1))

    def test_cli_cree_libre_avec_un_avertissement(self):
        work.add(self.db, title="premier", app="nexlink", assignee="human:proprio",
                 footprint=["src/ameesh/backlog.py"])
        proc = self.mesh("work", "add", "--title", "corriger src/ameesh/backlog.py",
                         "--app", "nexlink", "--assignee", "human:autre",
                         "--estimate", "1h")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("created unassigned", proc.stderr)
        self.assertIn("footprint: src/ameesh/backlog.py", proc.stdout)
        row = self.db.query("SELECT id, assignee FROM work_items WHERE title LIKE 'corriger%'")
        self.assertEqual(len(row), 1)
        self.assertIsNone(row[0]["assignee"])


class EcritureEmpreinteTest(_Empreintes):
    def test_b2_l_ecriture_attend_le_verrou_de_la_garde(self):
        """B2 de mty-amh-docs-01 : une empreinte posée pendant une attribution
        en cours (verrou du graphe tenu entre son contrôle et son écriture)
        attend la fin de celle-ci — jamais d'empreinte changée sous la garde."""
        from ameesh.storage.postgres.work_dependencies import _LOCK
        lot = self.lot("cible")
        holder = db_mod.connect(self.cfg)
        done = threading.Event()

        def write():
            connection = db_mod.connect(self.cfg)
            try:
                fp.set_paths(connection, lot["id"], ["src/a.py"], actor="pilote")
                done.set()
            finally:
                connection.close()

        try:
            with holder.transaction() as tx:
                tx.execute(_LOCK)
                worker = threading.Thread(target=write, daemon=True)
                worker.start()
                self.assertFalse(done.wait(1.0), "l'écriture n'a pas attendu le verrou")
            worker.join(30)
        finally:
            holder.close()
        self.assertTrue(done.is_set())
        self.assertEqual(fp.paths_of(self.db, lot["id"]), ["src/a.py"])

    def test_refresh_par_le_diff_garde_l_estimation(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True,
                               env=dict(os.environ, GIT_AUTHOR_NAME="t",
                                        GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                        GIT_COMMITTER_EMAIL="t@t"))
            git("init", "-q", "-b", "main")
            with open(os.path.join(tmp, "a.py"), "w") as handle:
                handle.write("a\n")
            git("add", ".")
            git("commit", "-qm", "base")
            git("checkout", "-qb", "lot/x")
            os.makedirs(os.path.join(tmp, "src"))
            with open(os.path.join(tmp, "src", "b.py"), "w") as handle:
                handle.write("b\n")
            git("add", ".")
            git("commit", "-qm", "lot")
            lot = self.lot("lot", branch="lot/x", branch_target="main")
            self.footprint(lot, "docs", source="intake")
            result = fp.refresh(self.db, self.cfg, lot["id"], actor="test", repo=tmp)
            self.assertEqual(result["paths"], ["docs", "src/b.py"])
            sources = {(r["source"], r["path"]) for r in
                       storage.of(self.db).work_footprints.for_items([lot["id"]])}
            self.assertEqual(sources, {("intake", "docs"), ("diff", "src/b.py")})

    def test_set_est_un_geste_de_pilote(self):
        lot = self.lot("lot")
        proc = self.mesh("work", "footprint", str(lot["id"]), "--set", "src",
                         "--actor", "ameesh-dev99")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("pilot gesture", proc.stderr)
        self.assertEqual(fp.paths_of(self.db, lot["id"]), [])
        proc = self.mesh("work", "footprint", str(lot["id"]), "--set", "src",
                         "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(fp.paths_of(self.db, lot["id"]), ["src"])


class ProprieteTest(_Empreintes):
    """Propriété : quelle que soit la suite de gestes de l'ordonnanceur (prises
    au repos, attributions manuelles, fusions, empreintes posées sur des lots
    en attente), jamais deux lots occupants d'un même dépôt n'ont d'empreintes
    chevauchantes."""

    UNIVERS = ("src", "src/a", "src/a/x.py", "src/a/y.py", "src/b.py", "docs",
               "docs/guide.md", "tests/test_a.py", "README.md")

    def invariant(self, step):
        rows = self.db.query(
            "SELECT w.id, w.app, f.path FROM work_items w"
            "  JOIN work_item_footprints f ON f.work_item_id = w.id"
            " WHERE w.state NOT IN ('merged', 'promoted', 'delivered', 'closed')"
            "   AND (w.assignee IS NOT NULL OR w.state <> 'intake')")
        for left, right in itertools.combinations(rows, 2):
            if left["id"] != right["id"] and left["app"] == right["app"]:
                self.assertFalse(fp.overlaps(left["path"], right["path"]),
                                 "%s : #%s %s ~ #%s %s" % (step, left["id"], left["path"],
                                                           right["id"], right["path"]))

    def test_jamais_deux_empreintes_chevauchantes_en_parallele(self):
        for seed in range(int(os.environ.get("AMEESH_FOOTPRINT_SEEDS", "6"))):
            with self.subTest(seed=seed):
                self.db.execute("DELETE FROM work_items")
                self.run_seed(random.Random(seed), seed)

    def run_seed(self, rng, seed):
        lots = []
        for i in range(12):
            lot = self.lot("lot %d/%d" % (seed, i), app=rng.choice(("nexlink", "nexlink", "ameesh")))
            self.footprint(lot, *rng.sample(self.UNIVERS, rng.randint(1, 3)))
            lots.append(lot)
        agents = ["agent%d" % i for i in range(4)]
        for step in range(60):
            lot = rng.choice(lots)
            current = work.get(self.db, lot["id"])
            gesture = rng.choice(("take", "take", "assign", "merge", "footprint", "create"))
            if gesture == "create":
                try:
                    lots.append(work.add(
                        self.db, title="cree %d/%d" % (seed, step),
                        app=rng.choice(("nexlink", "nexlink", "ameesh")),
                        assignee="human:%s" % rng.choice(agents),
                        footprint=rng.sample(self.UNIVERS, rng.randint(1, 3))))
                except storage.FootprintOverlap:
                    pass
            elif gesture == "take":
                WorkItems(self.db).assign(lot["id"], rng.choice(agents), current=None,
                                          note="t", actor="t", ready_only=True)
            elif gesture == "assign" and not current["assignee"]:
                try:
                    work.assign(self.db, lot["id"], "human:%s" % rng.choice(agents),
                                actor="test")
                except work.WorkError as exc:
                    self.assertIn("footprint overlaps", str(exc))
            elif gesture == "merge" and current["assignee"]:
                self.merge(lot)
            elif gesture == "footprint" and current["state"] == "intake" \
                    and not current["assignee"]:
                self.footprint(lot, *rng.sample(self.UNIVERS, rng.randint(1, 3)))
            self.invariant("seed %d, step %d (%s #%d)" % (seed, step, gesture, lot["id"]))


class DiffTest(unittest.TestCase):
    def git(self, *args):
        subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))

    def write(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_diff_depuis_la_base_de_la_branche(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo = tmp
            self.git("init", "-q", "-b", "main")
            self.write("src/a.py", "a\n")
            self.write("docs/x.md", "x\n")
            self.git("add", ".")
            self.git("commit", "-qm", "base")
            self.git("checkout", "-qb", "lot/1")
            self.write("src/a.py", "a2\n")
            self.write("src/new/b.py", "b\n")
            self.git("add", ".")
            self.git("commit", "-qm", "lot")
            self.git("checkout", "-q", "main")
            self.write("docs/x.md", "x2\n")   # la cible avance : hors du diff du lot
            self.git("commit", "-qam", "cible")
            self.assertEqual(fp.diff_paths(tmp, "main", "lot/1"),
                             ["src/a.py", "src/new/b.py"])
            self.assertIsNone(fp.diff_paths(tmp, "main", "lot/absent"))
            self.assertEqual(fp.estimate("voir a.py", tmp, "main"), ["src/a.py"])


if __name__ == "__main__":
    unittest.main()
