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

from ameesh import lot_class, merge_queue, packet_candidates, resolutions, storage

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
            6: "light", 7: "normal", 8: "normal", 9: "light",
        }
        self.files = {
            3: [{"path": "migrations/0009_x.sql", "class": "sensitive", "rule": None}],
        }
        self.families = {"author": "deepseek", "reviewer": "claude"}
        self.class_calls = []

    def inventory(self, **kwargs):
        def fake_class(db, lot, **rest):
            self.class_calls.append((lot, rest))
            return {"lot": lot, "class": self.classes[lot], "doubt": False,
                    "why": "test", "files": self.files.get(
                        lot, [{"path": "src/ameesh/x.py", "class": "light", "rule": None}])}

        def fake_family(db, name):
            return self.families.get(name, "")

        with mock.patch.object(storage, "of", return_value=self.store), \
                mock.patch.object(lot_class, "of_lot", side_effect=fake_class), \
                mock.patch.object(merge_queue, "actor_family", side_effect=fake_family):
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

    def test_missing_required_verdicts_is_named(self):
        entry = self.by_id(self.inventory())[3]
        self.assertFalse(entry["ready"])
        self.assertIn("verdicts-missing", [cause["code"] for cause in entry["causes"]])
        self.assertGreaterEqual(entry["gate"]["required"], 2)

    def test_conflict_with_target_is_named(self):
        entry = self.by_id(self.inventory())[4]
        self.assertFalse(entry["ready"])
        cause = next(cause for cause in entry["causes"] if cause["code"] == "conflict")
        self.assertEqual(cause["files"], ["shared.py"])

    def test_light_lot_without_verdict_is_ready(self):
        entry = self.by_id(self.inventory())[5]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertEqual(entry["gate"]["required"], 0)

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


if __name__ == "__main__":
    unittest.main()
