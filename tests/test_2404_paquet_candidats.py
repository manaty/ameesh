# SPDX-License-Identifier: AGPL-3.0-only
"""#2404 — la liste des lots prêts, calculée et non demandée par courrier.

Le banc monte un dépôt jetable avec une cible et des branches de lots qui
couvrent chaque refus nommé : tête publiée différente du gel, verdicts requis
manquants, conflit avec la cible, lot léger sans verdict, véhicule de paquet,
branche absente, cible déclarée ailleurs, contenu déjà dans la cible. Aucune
base réelle : `storage.of` est simulé, comme dans les témoins de la file.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import lot_class, merge_queue, packet_candidates, resolutions, storage, work

from .support import patch_families

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, "scripts", filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


candidates_script = _load_script("test_paquet_candidats_script", "paquet-candidats.py")


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _write(repo: str, path: str, text: str) -> None:
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full) or full, exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)


def _commit(repo: str, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


class FakeWork:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def items(self, *, state, assignee, limit):
        assert (state, assignee, limit) == ("qa", None, None), \
            "the inventory must read every qa lot, without assignee filter or page limit"
        return [dict(row) for row in self.rows]

    def get(self, lot: int):
        return next((row for row in self.rows if row["id"] == lot), None)


class FakeProgress:
    def __init__(self, marks: dict[int, list[dict]]):
        self.marks = marks

    def lot_milestones(self, ids):
        out = []
        for lot in ids:
            out.extend(self.marks.get(int(lot), []))
        return out


class InventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2404-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "banc")
        _git(self.repo, "config", "user.email", "banc@example.invalid")
        _git(self.repo, "config", "commit.gpgsign", "false")
        _write(self.repo, "shared.py", "one\ntwo\n")
        _commit(self.repo, "base")
        self.marks: dict[int, list[dict]] = {}
        self.tips: dict[int, str] = {}
        self.branches: dict[int, str] = {}
        clock = [0.0]
        rows: list[dict] = []

        def row(lot: int, name: str, *, app="ameesh", branch_target=None) -> dict:
            return {"id": lot, "state": "qa", "app": app, "branch": name,
                    "branch_target": branch_target, "title": "lot %d" % lot,
                    "assignee": "author", "priority": 2, "created_ts": float(lot)}

        def branch(lot: int, name: str, edits: dict, *, app="ameesh",
                   branch_target=None, verdicts=(), extra=()) -> dict:
            _git(self.repo, "switch", "-q", "-c", name, "main")
            for path, text in edits.items():
                _write(self.repo, path, text)
            _commit(self.repo, "#%d" % lot)
            tip = _git(self.repo, "rev-parse", "HEAD")
            for path, text in extra:
                _write(self.repo, path, text)
                tip = _commit(self.repo, "#%d suite" % lot)
            _git(self.repo, "switch", "-q", "main")
            self.branches[lot] = name
            self.tips[lot] = tip
            clock[0] += 1
            marks = [{"id": int(clock[0]), "work_item_id": lot, "kind": "frozen",
                      "sha": tip, "actor": "author", "verdict": None,
                      "at_ts": clock[0]}]
            for actor in verdicts:
                clock[0] += 1
                marks.append({"id": int(clock[0]), "work_item_id": lot, "kind": "verdict",
                              "sha": tip, "actor": actor, "verdict": "ok",
                              "at_ts": clock[0]})
            self.marks[lot] = marks
            return row(lot, name, app=app, branch_target=branch_target)

        # 1. prêt : gel = tête publiée, un avis indépendant, fusion propre
        rows.append(branch(1, "lot/1-ready", {"ready.py": "x\n"},
                           verdicts=("reviewer",)))
        # 2. gel en retard sur la tête publiée
        rows.append(branch(2, "lot/2-freeze", {"freeze.py": "x\n"},
                           verdicts=("reviewer",), extra=[("freeze.py", "xy\n")]))
        # 3. sensible critique : deux avis requis, un seul rendu
        rows.append(branch(3, "lot/3-missing", {"migrations/0009_x.sql": "-- x\n"},
                           verdicts=("reviewer",)))
        # 4. conflit avec la cible (main touche la même ligne plus bas)
        rows.append(branch(4, "lot/4-conflict", {"shared.py": "FOUR\ntwo\n"},
                           verdicts=("reviewer",)))
        # 5. léger sans verdict : le gel déclaré suffit
        rows.append(branch(5, "lot/5-light", {"light.py": "x\n"}))
        # 6. véhicule de paquet : jamais une unité
        rows.append(branch(6, "integration/paquet-test", {"packet.py": "x\n"},
                           verdicts=("reviewer",)))
        # 7. branche jamais publiée
        rows.append(row(7, "lot/7-absent"))
        self.marks[7] = [{"id": 700, "work_item_id": 7, "kind": "frozen",
                          "sha": "0" * 40, "actor": "author", "verdict": None,
                          "at_ts": 700.0}]
        # 8. cible déclarée ailleurs
        rows.append(branch(8, "lot/8-target", {"target.py": "x\n"},
                           verdicts=("reviewer",), branch_target="release/1.0"))
        # 9. contenu déjà dans la cible (tête ancêtre de main)
        rows.append(branch(9, "lot/9-merged", {"merged.py": "x\n"},
                           verdicts=("reviewer",)))
        # 12. machinerie des paquets : train_reviews=2, un seul avis (#2696)
        rows.append(branch(12, "lot/12-train", {"src/ameesh/resolutions.py": "x\n"},
                           verdicts=("reviewer",)))
        # un lot d'un autre projet ne doit jamais apparaître
        rows.append(row(10, "lot/10-other", app="nexlink"))

        # le lot 2 : gel et avis sur la première tête, la branche a avancé
        first = _git(self.repo, "rev-parse", self.tips[2] + "^")
        for mark in self.marks[2]:
            mark["sha"] = first
        # la cible avance : conflit avec le lot 4, et contenu du lot 9 livré
        _git(self.repo, "switch", "-q", "main")
        _write(self.repo, "shared.py", "MAIN\ntwo\n")
        _write(self.repo, "main.py", "m\n")
        _commit(self.repo, "main avance")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "livre #9", "lot/9-merged")

        self.origin = os.path.join(self.tmp, "origin.git")
        _git(self.tmp, "init", "-q", "--bare", self.origin)
        _git(self.repo, "remote", "add", "origin", self.origin)
        _git(self.repo, "push", "-q", "origin", "--all")
        self.clone = resolutions.Replayer(self.repo, os.path.join(self.tmp, "clone"))
        self.rows = rows
        self.store = mock.Mock(work=FakeWork(rows),
                               progress=FakeProgress(self.marks))
        self.classes = {
            1: "normal", 2: "normal", 3: "sensitive", 4: "normal", 5: "light",
            6: "light", 7: "normal", 8: "normal", 9: "light", 12: "sensitive",
        }
        self.files = {
            3: [{"path": "migrations/0009_x.sql", "class": "sensitive", "rule": None}],
            12: [{"path": "src/ameesh/resolutions.py", "class": "sensitive",
                  "rule": None}],
        }
        # #2696 : le classement porte train_reviews ; la porte et la liste des
        # prêts doivent lire le même nombre.
        self.train_reviews = {12: 2}
        self.families = {"author": "deepseek", "reviewer": "claude"}
        self.class_calls = []
        self.head_classes: dict[str, tuple] = {}
        # #2664 : le banc n'a pas de canon Git ; les familles lues par la
        # porte sont épinglées, les familles d'acteurs restent simulées.
        patch_families(self)

    def inventory(self, *, family=None, **kwargs):
        def fake_class(db, lot, **rest):
            self.class_calls.append((lot, rest))
            # #2496 : la classe du contenu composé d'une remise, par SHA
            if rest.get("head") in self.head_classes:
                cls, files = self.head_classes[rest["head"]]
                return {"lot": lot, "class": cls, "doubt": False, "why": "test",
                        "files": files, "light_reviews": 1,
                        "train_reviews": self.train_reviews.get(lot, 0)}
            return {"lot": lot, "class": self.classes[lot], "doubt": False,
                    "why": "test", "files": self.files.get(
                        lot, [{"path": "src/ameesh/x.py", "class": "light", "rule": None}]),
                    "light_reviews": 1,
                    "train_reviews": self.train_reviews.get(lot, 0)}

        def fake_family(db, name, **kw):
            return self.families.get(name, "")

        with mock.patch.object(storage, "of", return_value=self.store), \
                mock.patch.object(work, "get",
                                  side_effect=lambda db, lot: next(
                                      (dict(row) for row in self.rows
                                       if row["id"] == int(lot)), None)), \
                mock.patch.object(lot_class, "of_lot", side_effect=fake_class), \
                mock.patch.object(merge_queue, "actor_family",
                                  side_effect=family or fake_family):
            return packet_candidates.inventory(
                object(), SimpleNamespace(name="ameesh", target="main"),
                repo=self.repo, clone=self.clone, target="main", cfg=None, **kwargs)

    def by_id(self, payload):
        return {entry["work_item"]: entry for entry in payload["candidates"]}

    def test_ready_lot_is_assigned_and_listed(self):
        payload = self.inventory()
        self.assertEqual(payload["schema"], "ameesh-packet-candidates/1")
        entry = self.by_id(payload)[1]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertEqual(entry["published_head"], self.tips[1])
        self.assertEqual(entry["frozen_sha"], self.tips[1])
        self.assertIn(1, payload["ready"])
        self.assertEqual(payload["errors"], [])

    def test_assigned_lots_are_read_not_filtered(self):
        # Régression #2404 : un filtre d'assigné (ou une page) cacherait les
        # lots prêts assignés, précisément ceux que le composeur a omis.
        payload = self.inventory()
        self.assertIn(1, payload["ready"])
        self.assertTrue(all(row["assignee"] == "author"
                            for row in self.rows if row["app"] == "ameesh"))

    def test_freeze_not_at_published_head_is_named(self):
        entry = self.by_id(self.inventory())[2]
        self.assertFalse(entry["ready"])
        self.assertIn("freeze-not-head", [cause["code"] for cause in entry["causes"]])

    def test_pilot_waiver_covers_the_frozen_head_after_an_older_verdict(self):
        """#2681 : le gel déclaré l'emporte sur un avis antérieur quand le
        pilote a posé `-none` ; la porte honore la même dispense."""
        self.marks[2].append({"id": 2000, "work_item_id": 2, "kind": "frozen",
                              "sha": self.tips[2], "actor": "author",
                              "verdict": None, "at_ts": 2000.0})
        entry = self.by_id(self.inventory())[2]
        self.assertIn("freeze-after-verdict",
                      [cause["code"] for cause in entry["causes"]])
        row = next(row for row in self.rows if row["id"] == 2)
        row["reviewer"] = work.REVIEWER_NONE
        entry = self.by_id(self.inventory())[2]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertEqual(entry["frozen_sha"], self.tips[2])
        self.assertIn("verdict-not-required", entry["freeze"]["notes"])

    def test_missing_required_verdicts_is_named(self):
        entry = self.by_id(self.inventory())[3]
        self.assertFalse(entry["ready"])
        self.assertIn("verdicts-missing", [cause["code"] for cause in entry["causes"]])
        self.assertGreaterEqual(entry["gate"]["required"], 2)

    def test_train_machinery_with_one_verdict_is_not_ready(self):
        # #2696 : train_reviews=2 pour la machinerie des paquets ; un seul avis
        # ne rend pas le lot prêt, même si sa classe est sensible.
        entry = self.by_id(self.inventory())[12]
        self.assertFalse(entry["ready"], entry["causes"])
        self.assertIn("verdicts-missing", [cause["code"] for cause in entry["causes"]])
        self.assertEqual(entry["gate"]["required"], 2)

    def test_unreadable_registry_is_an_error_not_a_verdicts_missing_wait(self):
        # #2521 : une lecture du registre impossible ne doit pas se lire
        # « verdicts-missing » (le lot serait écarté du paquet comme sans avis).
        def unreadable(db, name, **kw):
            raise merge_queue.RegistryReadError(
                'cannot read the agent registry for %s: column "aliases" does not exist'
                % name)

        payload = self.inventory(family=unreadable)
        entry = self.by_id(payload)[1]
        self.assertFalse(entry["ready"])
        self.assertIn("aliases", entry["error"])
        self.assertNotIn("verdicts-missing",
                         [cause["code"] for cause in entry["causes"]])
        self.assertIn(1, [e["work_item"] for e in payload["candidates"] if e["error"]])
        self.assertTrue(any("aliases" in error for error in payload["errors"]))
        self.assertNotIn(1, payload["waiting"])
        self.assertNotIn(1, payload["ready"])

    def test_conflict_with_target_is_named(self):
        entry = self.by_id(self.inventory())[4]
        self.assertFalse(entry["ready"])
        cause = next(cause for cause in entry["causes"] if cause["code"] == "conflict")
        self.assertEqual(cause["files"], ["shared.py"])

    def test_missing_freeze_blob_is_refetched_before_the_conflict_read(self):
        """#2738 : la tête du gel n'est qu'une ref sur des objets prêtés par le
        checkout des agents ; quand un blob de sa fusion a disparu (source
        élaguée), la fusion d'essai devient une erreur de lecture. Le fetch
        doit rapatrier les objets du gel avant de conclure."""
        tip = self.tips[4]
        self.clone.ensure()          # clone partagé, objets prêtés
        # l'objet que l'élagage de la source aurait retiré : le blob du gel
        # n'est plus lisible nulle part, le commit et l'arbre le restent
        blob = _git(self.repo, "rev-parse", tip + ":shared.py")
        path = os.path.join(self.repo, ".git", "objects", blob[:2], blob[2:])
        self.assertTrue(os.path.isfile(path), "le blob doit être loose pour ce témoin")
        os.unlink(path)
        payload = self.inventory()
        entry = self.by_id(payload)[4]
        self.assertEqual(entry["error"], "")
        self.assertIn("conflict", [cause["code"] for cause in entry["causes"]])
        self.assertEqual(payload["errors"], [])

    def test_light_lot_needs_the_declared_review(self):
        # #2642 : un lot léger n'entre plus au paquet sans l'avis d'une autre
        # famille ; le plancher est déclaré au canon (light_reviews: 1).
        entry = self.by_id(self.inventory())[5]
        self.assertFalse(entry["ready"], entry["causes"])
        self.assertIn("verdicts-missing",
                      [cause["code"] for cause in entry["causes"]])
        self.assertEqual(entry["gate"]["required"], 1)

    def test_packet_vehicle_is_not_a_unit(self):
        entry = self.by_id(self.inventory())[6]
        self.assertFalse(entry["ready"])
        self.assertEqual([cause["code"] for cause in entry["causes"]], ["packet-vehicle"])

    def test_unpublished_branch_and_target_mismatch_are_named(self):
        entries = self.by_id(self.inventory())
        self.assertIn("branch-not-published",
                      [cause["code"] for cause in entries[7]["causes"]])
        self.assertIn("target-mismatch", [cause["code"] for cause in entries[8]["causes"]])

    def test_content_already_in_target_is_named(self):
        entry = self.by_id(self.inventory())[9]
        self.assertFalse(entry["ready"])
        self.assertIn("already-in-target", [cause["code"] for cause in entry["causes"]])

    def test_other_project_is_out_of_the_list(self):
        payload = self.inventory()
        self.assertNotIn(10, [entry["work_item"] for entry in payload["candidates"]])

    def test_class_reads_the_package_target_not_origin_head(self):
        # #2464 : avec un origin/HEAD resté sur une release, la classe doit
        # recevoir la cible du paquet (main), pas relire ce HEAD.
        base = _git(self.repo, "rev-parse", self.tips[1] + "^")
        _git(self.repo, "update-ref", "refs/remotes/origin/release/1.11.0", base)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/release/1.11.0")
        payload = self.inventory()
        call = next(rest for lot, rest in self.class_calls if lot == 1)
        self.assertEqual(call["item"]["branch_target"], "main")
        self.assertEqual(call["base"], payload["base"])

    def test_origin_head_release_without_target_is_a_read_error(self):
        base = _git(self.repo, "rev-parse", self.tips[1] + "^")
        _git(self.repo, "update-ref", "refs/remotes/origin/release/1.11.0", base)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/release/1.11.0")
        with self.assertRaises(packet_candidates.CandidateError):
            packet_candidates.inventory(
                object(), SimpleNamespace(name="ameesh", target=""),
                repo=self.repo, clone=self.clone, target=None, cfg=None)

    def test_base_option_reproduces_a_manifest(self):
        base = _git(self.repo, "rev-parse", "main^")
        payload = self.inventory(base=base)
        self.assertEqual(payload["base"], base)
        self.assertEqual(payload["base_source"], "option")

    def test_json_is_serialisable(self):
        payload = self.inventory()
        self.assertEqual(json.dumps(payload, default=str)[:1], "{")
        self.assertIn("waiting", payload)
        self.assertIn("ready", payload)

    def test_script_render_shows_ready_reasons_and_errors(self):
        payload = self.inventory()
        payload["errors"] = ["#99 : lecture impossible"]
        text = candidates_script.render(payload)
        self.assertIn("PRÊTS", text)
        self.assertIn("#1", text)
        self.assertIn("EN ATTENTE", text)
        self.assertIn("freeze-not-head", text)
        self.assertIn("ERREURS", text)
        self.assertIn("#99 : lecture impossible", text)


class RemiseTest(InventoryTest):
    """#2496 : une remise propre sur la cible rend un lot en conflit prêt
    comme unité — parents (gel relu, commit de la cible), fusion propre avec
    la base, et un verdict sur la remise ou une union mécanique."""

    def _remise(self, lot: int, tail: str, content: dict, *, parent: str | None = None,
                verdict: str | None = None) -> str:
        """Fusionne main dans `parent` (le gel par défaut), résout avec
        `content`, publie la ref de remise et, si demandé, un verdict dessus."""
        _git(self.repo, "switch", "-q", "--detach", parent or self.tips[lot])
        subprocess.run(["git", "-C", self.repo, "merge", "-q", "--no-ff", "-m", "remise",
                        "main"], capture_output=True)
        for path, text in content.items():
            _write(self.repo, path, text)
        _git(self.repo, "add", "-A")
        if subprocess.run(["git", "-C", self.repo, "rev-parse", "-q", "--verify",
                           "MERGE_HEAD"], capture_output=True).returncode == 0:
            _git(self.repo, "commit", "-qm", "remise #%d sur main" % lot)
        else:  # fusion déjà propre : la résolution s'y ajoute, mêmes parents
            _git(self.repo, "commit", "-q", "--amend", "--no-edit", "--allow-empty")
        sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "switch", "-q", "main")
        ref = "refs/ameesh/resolutions/%d/%s" % (lot, tail)
        _git(self.repo, "update-ref", ref, sha)
        _git(self.repo, "push", "-q", "origin", "%s:%s" % (ref, ref))
        if verdict:
            self.marks[lot].append({"id": 9000 + len(self.marks[lot]), "work_item_id": lot,
                                    "kind": "verdict", "sha": sha, "actor": "reviewer",
                                    "verdict": verdict, "at_ts": 9000.0})
        return sha

    def _union_lot(self) -> None:
        """Lot 11 : ajout pur en fin de liste ; main ajoute une autre ligne au
        même endroit — un conflit que seule une union résout."""
        _write(self.repo, "list.txt", "a\nb\n")
        _commit(self.repo, "liste")
        _git(self.repo, "switch", "-q", "-c", "lot/11-union")
        _write(self.repo, "list.txt", "a\nb\nlot\n")
        tip = _commit(self.repo, "#11")
        _git(self.repo, "switch", "-q", "main")
        _write(self.repo, "list.txt", "a\nb\nmain\n")
        _commit(self.repo, "main ajoute")
        self.tips[11] = tip
        self.marks[11] = [{"id": 1100, "work_item_id": 11, "kind": "frozen", "sha": tip,
                           "actor": "author", "verdict": None, "at_ts": 1100.0},
                          {"id": 1101, "work_item_id": 11, "kind": "verdict", "sha": tip,
                           "actor": "reviewer", "verdict": "ok", "at_ts": 1101.0}]
        self.rows.append({"id": 11, "state": "qa", "app": "ameesh", "branch": "lot/11-union",
                          "branch_target": None, "title": "lot 11", "assignee": "author",
                          "priority": 2, "created_ts": 11.0})
        self.classes[11] = "normal"
        _git(self.repo, "push", "-q", "origin", "--all")

    def test_remise_with_verdict_makes_the_lot_ready(self):
        sha = self._remise(4, "main-abc", {"shared.py": "FOUR MAIN\ntwo\n"}, verdict="ok")
        payload = self.inventory()
        entry = self.by_id(payload)[4]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertEqual(entry["source"], sha)
        self.assertEqual(entry["remise"]["cover"], "verdict")
        self.assertIn(4, payload["ready"])
        self.assertIn("remise refs/ameesh/resolutions/4/main-abc",
                      candidates_script.render(payload))

    def test_remise_class_reads_the_package_target_not_origin_head(self):
        """#2574 : la remise d'un lot sans `branch_target` se classe avec la
        cible du paquet, comme sa tête gelée — jamais avec `origin/HEAD` du
        clone radar, resté sur une release."""
        base = _git(self.repo, "rev-parse", "main^")
        _git(self.repo, "update-ref", "refs/remotes/origin/release/1.11.0", base)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/release/1.11.0")
        sha = self._remise(4, "main-abc", {"shared.py": "FOUR MAIN\ntwo\n"}, verdict="ok")
        payload = self.inventory()
        call = next(rest for lot, rest in self.class_calls
                    if lot == 4 and rest.get("head") == sha)
        self.assertEqual(call["item"]["branch_target"], "main")
        self.assertEqual(call["base"], payload["base"])
        self.assertTrue(self.by_id(payload)[4]["ready"])

    def test_ready_lot_source_is_its_published_head(self):
        entry = self.by_id(self.inventory())[1]
        self.assertEqual(entry["source"], self.tips[1])
        self.assertIsNone(entry["remise"])

    def test_judged_remise_without_verdict_stays_conflict(self):
        self._remise(4, "main-abc", {"shared.py": "FOUR MAIN\ntwo\n"})
        entry = self.by_id(self.inventory())[4]
        self.assertFalse(entry["ready"])
        cause = next(c for c in entry["causes"] if c["code"] == "conflict")
        self.assertIn("main-abc", cause["detail"])
        self.assertIn("not a pure addition", cause["detail"])

    def test_mechanical_union_needs_no_verdict(self):
        self._union_lot()
        sha = self._remise(11, "union-main-1", {"list.txt": "a\nb\nlot\nmain\n"})
        entry = self.by_id(self.inventory())[11]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertEqual(entry["source"], sha)
        self.assertEqual(entry["remise"]["cover"], "mechanical")

    def test_union_with_extra_content_is_not_mechanical(self):
        self._union_lot()
        self._remise(11, "main-1", {"list.txt": "a\nb\nlot\nmain\n", "extra.py": "x\n"})
        entry = self.by_id(self.inventory())[11]
        self.assertFalse(entry["ready"])
        self.assertIn("beyond the union", entry["remises"][0]["why"])

    def test_blocked_verdict_on_a_union_refuses_it(self):
        self._union_lot()
        self._remise(11, "main-1", {"list.txt": "a\nb\nlot\nmain\n"}, verdict="blocked")
        entry = self.by_id(self.inventory())[11]
        self.assertFalse(entry["ready"])
        self.assertIn("blocked", entry["remises"][0]["why"])

    def test_remise_not_on_the_reviewed_freeze_is_refused(self):
        parent = _git(self.repo, "rev-parse", self.tips[4] + "^")
        self._remise(4, "main-old", {"shared.py": "MAIN\ntwo\n"}, parent=parent,
                     verdict="ok")
        entry = self.by_id(self.inventory())[4]
        self.assertFalse(entry["ready"])
        self.assertIn("reviewed freeze", entry["remises"][0]["why"])

    def test_resolution_with_another_lot_is_not_a_remise(self):
        self._remise(4, "12", {"shared.py": "FOUR MAIN\ntwo\n"}, verdict="ok")
        entry = self.by_id(self.inventory())[4]
        self.assertFalse(entry["ready"])
        self.assertEqual(entry["remises"], [])

    def test_remise_ref_on_the_freeze_never_hides_its_blocked_verdict(self):
        """B1 (cx1) : une ref de remise qui pointe sur le gel lui-même (un
        alias) ne fait pas disparaître le blocage rendu sur ce gel."""
        self.marks[5].append({"id": 5000, "work_item_id": 5, "kind": "verdict",
                              "sha": self.tips[5], "actor": "reviewer",
                              "verdict": "blocked", "at_ts": 5000.0})
        self.assertFalse(self.by_id(self.inventory())[5]["ready"])
        ref = "refs/ameesh/resolutions/5/main-alias"
        _git(self.repo, "update-ref", ref, self.tips[5])
        _git(self.repo, "push", "-q", "origin", "%s:%s" % (ref, ref))
        entry = self.by_id(self.inventory())[5]
        self.assertFalse(entry["ready"])
        self.assertIn("blocked-verdict", [c["code"] for c in entry["causes"]])

    def test_remise_is_classed_on_its_composed_content(self):
        """B2 (cx1, cx2) : une remise qui ajoute une migration passe la porte
        de son contenu (deux avis), pas celle du gel normal (un avis)."""
        sha = self._remise(4, "main-sql", {"shared.py": "FOUR MAIN\ntwo\n",
                                           "migrations/9999_review.sql": "-- x\n"},
                           verdict="ok")
        self.head_classes[sha] = ("sensitive", [
            {"path": "migrations/9999_review.sql", "class": "sensitive", "rule": None}])
        entry = self.by_id(self.inventory())[4]
        self.assertFalse(entry["ready"])
        remise = entry["remises"][0]
        self.assertFalse(remise["ok"])
        self.assertEqual(remise["review_class"]["class"], "sensitive")

    def test_remise_of_a_light_lot_needs_an_independent_verdict(self):
        """B2 (cx1) : une porte légère à zéro avis ne prouve rien sur une
        résolution jugée ; l'avis de l'auteur ne compte pas."""
        self.classes[4] = "light"
        sha = self._remise(4, "main-abc", {"shared.py": "FOUR MAIN\ntwo\n"})
        self.marks[4].append({"id": 9500, "work_item_id": 4, "kind": "verdict", "sha": sha,
                              "actor": "author", "verdict": "ok", "at_ts": 9500.0})
        entry = self.by_id(self.inventory())[4]
        self.assertFalse(entry["ready"])
        self.marks[4].append({"id": 9501, "work_item_id": 4, "kind": "verdict", "sha": sha,
                              "actor": "reviewer", "verdict": "ok", "at_ts": 9501.0})
        entry = self.by_id(self.inventory())[4]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertEqual(entry["remise"]["cover"], "verdict")

    def test_waiver_of_the_freeze_does_not_cover_a_remise(self):
        """B2 (cx2) : la dispense `-none` du gel ne couvre pas la résolution
        d'une remise jamais relue."""
        for row in self.rows:
            if row["id"] == 4:
                row["reviewer"] = work.REVIEWER_NONE
        self._remise(4, "main-abc", {"shared.py": "FOUR MAIN\ntwo\n", "extra.py": "x\n"})
        entry = self.by_id(self.inventory())[4]
        self.assertFalse(entry["ready"])
        self.assertNotEqual(entry["remises"][0]["cover"], "waiver")

    def test_is_remise_names(self):
        self.assertTrue(packet_candidates.is_remise(
            "refs/ameesh/resolutions/4/main-c85280ab", 4, "main"))
        self.assertTrue(packet_candidates.is_remise(
            "refs/ameesh/resolutions/4/union-main-1", 4, "main"))
        self.assertFalse(packet_candidates.is_remise(
            "refs/ameesh/resolutions/4/2479", 4, "main"))
        self.assertFalse(packet_candidates.is_remise(
            "refs/ameesh/resolutions/41/main-1", 4, "main"))
        self.assertFalse(packet_candidates.is_remise(
            "refs/ameesh/resolutions/4/develop-1", 4, "main"))


class ReadyOutTest(unittest.TestCase):
    """Relecture cx2 B3 : un inventaire incomplet ne laisse jamais d'export."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2404-ready-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = pathlib.Path(self.tmp) / "prets.txt"

    def test_ready_out_removed_when_inventory_has_errors(self):
        self.path.write_text("#100\n", encoding="utf-8")
        candidates_script.write_ready_out(self.path, {
            "target": "main", "base": "a" * 40, "ready": [100],
            "errors": ["#500 : lecture impossible"],
        })
        self.assertFalse(self.path.exists())

    def test_ready_out_written_when_inventory_is_complete(self):
        candidates_script.write_ready_out(self.path, {
            "target": "main", "base": "a" * 40, "ready": [100, 200],
            "errors": [],
        })
        content = self.path.read_text(encoding="utf-8")
        self.assertIn("#100", content)
        self.assertIn("#200", content)

    def test_previous_export_is_cleared_before_a_failed_run(self):
        self.path.write_text("#100\n", encoding="utf-8")
        with mock.patch.object(candidates_script.config, "load",
                               side_effect=RuntimeError("boom")):
            code = candidates_script.main(
                ["--repo", self.tmp, "--ready-out", str(self.path)]
            )
        self.assertEqual(code, 2)
        self.assertFalse(self.path.exists())

    def test_script_exits_1_and_clears_ready_out_on_a_read_error(self):
        # #2521 : une lecture du registre impossible rend la liste incomplète :
        # code 1 et aucun export, comme une porte illisible.
        self.path.write_text("#100\n", encoding="utf-8")
        payload = {
            "schema": "ameesh-packet-candidates/1", "project": "ameesh",
            "repo": self.tmp, "clone": self.tmp, "target": "main",
            "target_head": "a" * 40, "base": "a" * 40, "base_source": "target",
            "candidates": [], "ready": [], "waiting": [],
            "errors": ["#1 : cannot read the review gate: registry unreadable"],
        }
        project = SimpleNamespace(name="ameesh", target="main")
        conn = mock.Mock()
        with mock.patch.object(candidates_script.config, "load",
                               return_value=object()), \
                mock.patch.object(candidates_script, "resolve_project",
                                  return_value=(project, True)), \
                mock.patch.object(candidates_script.db, "connect", return_value=conn), \
                mock.patch.object(candidates_script.merge_queue, "working_clone",
                                  return_value=(self.tmp, None)), \
                mock.patch.object(candidates_script.packet_candidates, "inventory",
                                  return_value=payload):
            code = candidates_script.main(
                ["--repo", self.tmp, "--project", "ameesh", "--target", "main",
                 "--ready-out", str(self.path)]
            )
        self.assertEqual(code, 1)
        self.assertFalse(self.path.exists())
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
