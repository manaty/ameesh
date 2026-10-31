# SPDX-License-Identifier: AGPL-3.0-only
"""#4186 (manaty/ameesh#3207, suite de #4173) : l'empreinte d'un lot couvre
toutes les créations de lots et reste exacte après les gestes manuels.

Le N de la relecture de #4173 : l'estimation d'intake ne vivait que dans
`ameesh work add` — `agent-mail --new-lot`, incident, empreinte §16, lot de
relecture, porteur de paquet et file d'amélioration créaient des lots sans
empreinte ; et `--estimate` après un `--set` ré-ajoutait des lignes `intake`
sous la correction du pilote."""
from __future__ import annotations

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import assignments, backlog, storage, work, work_footprints as fp

from .test_l165_prise_lots import _Lots


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          env=env)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _analysis(paths):
    """#4818 : une analyse d'impact connue (ou inconnue, sans chemin), le
    point d'entrée des chemins de création (`analyze_for_creation`)."""
    return {"status": "known" if paths else "unknown",
            "reason": None if paths else "no cited path",
            "paths": [{"path": p, "confidence": "high", "why": "cited"} for p in paths],
            "weak": [], "footprint": list(paths), "policy": "open",
            "note": "Impact analysis: test", "elapsed_ms": 0, "git_calls": 0}


class EstimationTest(unittest.TestCase):
    def test_un_texte_sans_chemin_ne_resout_rien(self):
        self.assertEqual(fp.estimate_for_creation(types.SimpleNamespace(), app="p",
                                                  title="rien à citer"), [])

    def test_sans_cfg_aucune_estimation(self):
        self.assertEqual(fp.estimate_for_creation(None, app="p", title="voir src/a.py"), [])

    def test_l_estimation_resout_dans_le_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            _git(tmp, "init", "-q")
            _git(tmp, "checkout", "-q", "-b", "main")
            os.makedirs(os.path.join(tmp, "src"))
            for path in ("src/a.py", "docs/x.md"):
                full = os.path.join(tmp, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as handle:
                    handle.write("x\n")
            _git(tmp, "add", ".")
            _git(tmp, "commit", "-q", "-m", "base")
            paths = fp.estimate_for_creation(
                types.SimpleNamespace(), app="p",
                title="revoir src/a.py et a.py", body="voir docs/x.md",
                branch_target="main", repo=tmp)
            # `a.py` seul devient son chemin unique dans le clone.
            self.assertEqual(paths, ["docs/x.md", "src/a.py"])


class CreationTest(_Lots):
    def notes(self, item):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(item["id"]),))]

    def test_work_add_ecrit_l_empreinte_estimee(self):
        with mock.patch.object(fp, "analyze_for_creation",
                               return_value=_analysis(["src/a.py"])) as estimate:
            item = work.add(self.db, title="corriger src/a.py", app="ameesh",
                            cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["src/a.py"])
        self.assertIn("Footprint (intake): src/a.py", self.notes(item))
        self.assertEqual(estimate.call_args.kwargs["app"], "ameesh")

    def test_une_empreinte_explicite_prime_sur_l_estimation(self):
        with mock.patch.object(fp, "analyze_for_creation",
                               return_value=_analysis(["src/a.py"])) as estimate:
            item = work.add(self.db, title="corriger src/a.py", app="ameesh",
                            cfg=self.cfg, footprint=["docs/x.md"])
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["docs/x.md"])
        estimate.assert_not_called()

    def test_creation_attribuee_est_gardee_par_l_estimation(self):
        occupe = work.add(self.db, title="occupe", app="ameesh",
                          assignee="human:proprio", footprint=["src"])
        self.assertEqual(occupe["assignee"], "human:proprio")
        with mock.patch.object(fp, "analyze_for_creation", return_value=_analysis(["src/a.py"])):
            with self.assertRaises(storage.FootprintOverlap):
                work.add(self.db, title="cite src/a.py", app="ameesh",
                         assignee="human:autre", cfg=self.cfg)
        # la même création libre passe et porte l'empreinte estimée
        with mock.patch.object(fp, "analyze_for_creation", return_value=_analysis(["src/a.py"])):
            libre = work.add(self.db, title="cite src/a.py", app="ameesh", cfg=self.cfg)
        self.assertIsNone(libre["assignee"])
        self.assertEqual(fp.paths_of(self.db, libre["id"]), ["src/a.py"])

    def test_le_repli_ne_perd_pas_un_lot_externe(self):
        """N de la relecture de #4173 : le repli sur chevauchement naît libre,
        donc sans `--externe` (qui force l'attribution) — sinon la seconde
        création lève « --externe without assignee » et le lot n'existe pas."""
        self.agent("externe", responsible="human:proprio")
        self.db.execute("UPDATE agent_registry SET mode = 'externe'"
                        " WHERE name = 'externe'")
        work.add(self.db, title="premier", app="nexlink",
                 assignee="human:proprio", footprint=["src/ameesh/backlog.py"])
        proc = self.mesh("work", "add", "--title", "corriger src/ameesh/backlog.py",
                         "--app", "nexlink", "--assignee", "externe",
                         "--externe", "--estimate", "1h")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("created unassigned", proc.stderr)
        row = self.db.query(
            "SELECT id, assignee FROM work_items WHERE title LIKE 'corriger%'")
        self.assertEqual(len(row), 1)
        self.assertIsNone(row[0]["assignee"])


class BacklogTest(_Lots):
    RULE = backlog.Rules(lot_min_value=30, timezone="Europe/Paris", commit="c" * 40)

    def setUp(self):
        super().setUp()
        # le projet doit être connu : un lot le déclare (comme dans la vie réelle)
        self.projet = work.add(self.db, title="projet ameesh", app="ameesh")

    def element(self, title, score, **kw):
        with mock.patch.object(backlog, "rules", return_value=self.RULE):
            return backlog.add(self.db, title=title, expected_value="gain",
                               value_score=score, app="ameesh", actor="test", **kw)

    def test_un_lot_direct_porte_l_empreinte(self):
        with mock.patch.object(fp, "analyze_for_creation",
                               return_value=_analysis(["src/a.py"])):
            lot = self.element("corriger src/a.py", 40)
        self.assertEqual(fp.paths_of(self.db, lot["id"]), ["src/a.py"])

    def test_le_panier_unionne_les_chemins_de_ses_lignes(self):
        with mock.patch.object(fp, "analyze_for_creation", return_value=_analysis(["src/a.py"])):
            premier = self.element("ligne a — src/a.py", 10)
        with mock.patch.object(fp, "analyze_for_creation", return_value=_analysis(["docs/x.md"])):
            panier = self.element("ligne b — docs/x.md", 10)
        self.assertEqual(panier["id"], premier["id"])
        self.assertEqual(fp.paths_of(self.db, panier["id"]),
                         ["docs/x.md", "src/a.py"])
        # une ligne qui ne cite rien ne retire pas l'empreinte acquise
        with mock.patch.object(fp, "analyze_for_creation", return_value=_analysis([])):
            encore = self.element("ligne c", 5)
        self.assertEqual(encore["id"], premier["id"])
        self.assertEqual(fp.paths_of(self.db, panier["id"]),
                         ["docs/x.md", "src/a.py"])


class MailTest(_Lots):
    def test_new_lot_qui_chevauche_nait_libre_et_avertit(self):
        self.agent("orch")
        self.agent("dev1")
        occupe = work.add(self.db, title="occupe", app="p",
                          assignee="human:proprio", footprint=["src"])
        with mock.patch.object(fp, "analyze_for_creation", return_value=_analysis(["src/a.py"])):
            out = assignments.link_message(
                self.cfg, self.db, sender="orch", recipient="dev1",
                new_lot="corriger src/a.py", estimate="1h", app="p", new_project=True)
        self.assertTrue(out["created"])
        self.assertFalse(out["assigned"])
        lot = work.get(self.db, int(out["work_item_id"]))
        self.assertIsNone(lot["assignee"])
        self.assertEqual(fp.paths_of(self.db, lot["id"]), ["src/a.py"])
        self.assertTrue(any("footprint overlaps" in warning for warning in out["warnings"]),
                        out["warnings"])
        self.assertIn("#%d" % occupe["id"], " ".join(out["warnings"]))


class EstimateManuelTest(_Lots):
    def test_set_refuse_un_chemin_absolu(self):
        item = self.lot("absolu")
        with self.assertRaises(work.WorkError):
            fp.set_paths(self.db, item["id"], ["/src/a.py"], actor="pilote")
        with self.assertRaises(work.WorkError):
            fp.set_paths(self.db, item["id"], ["src/a.py", "/hors/depot"], actor="pilote")
        self.assertEqual(fp.paths_of(self.db, item["id"]), [])

    def test_estimate_apres_set_ne_rajoute_pas_d_intake(self):
        item = self.lot("empreinte corrigée")
        fp.set_paths(self.db, item["id"], ["src/a.py"], actor="pilote")
        with mock.patch.object(fp, "analyze_lot", return_value=_analysis(["docs/x.md"])):
            self.assertIsNone(fp.record_estimate(
                self.db, self.cfg, work.get(self.db, item["id"]), actor="test"))
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["src/a.py"])

    def test_estimate_reprend_apres_clear(self):
        item = self.lot("empreinte effacée")
        fp.set_paths(self.db, item["id"], ["src/a.py"], actor="pilote")
        storage.of(self.db).work_footprints.replace(
            item["id"], "manual", [], actor="pilote", replaces=fp.ESTIMATE_SOURCES)
        with mock.patch.object(fp, "analyze_lot", return_value=_analysis(["docs/x.md"])):
            self.assertIsNotNone(fp.record_estimate(
                self.db, self.cfg, work.get(self.db, item["id"]), actor="test"))
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["docs/x.md"])
