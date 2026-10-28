# SPDX-License-Identifier: AGPL-3.0-only
"""#2404 — la liste des lots prêts, calculée et non demandée par courrier.

Le banc monte un dépôt jetable avec une cible et des branches de lots qui
couvrent chaque refus nommé : tête publiée différente du gel, verdicts requis
manquants, conflit avec la cible, lot léger sans verdict, véhicule de paquet,
branche absente, cible déclarée ailleurs, contenu déjà dans la cible. Aucune
base réelle : `storage.of` est simulé, comme dans les témoins de la file.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import (assignments, canon, lot_class, merge_queue, packet_candidates,
                    planned_waits, resolutions, sous_utilisation, storage, work)

from .support import patch_families

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, "scripts", filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


candidates_script = _load_script("test_paquet_candidats_script", "paquet-candidats.py")
inclusion_script = _load_script("test_paquet_candidats_inclusion", "paquet-inclusion.py")


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
        assert state in ("qa", "validated") and (assignee, limit) == (None, None), \
            "the inventory must read every qa lot, without assignee filter or page limit"
        return [dict(row) for row in self.rows if row["state"] == state]

    def get(self, lot: int):
        return next((row for row in self.rows if row["id"] == lot), None)


class FakeProgress:
    def __init__(self, marks: dict[int, list[dict]], holds: dict[int, list[dict]] | None = None):
        self.marks = marks
        self.holds = holds if holds is not None else {}

    def lot_milestones(self, ids):
        out = []
        for lot in ids:
            out.extend(self.marks.get(int(lot), []))
        return out

    def lot_hold_events(self, ids, patterns):
        """#3375 : le flux ciblé du stockage (transitions + notes hold/levée)."""
        compiled = tuple(re.compile(pattern, re.I) for pattern in patterns)
        out = []
        for lot in ids:
            for event in self.holds.get(int(lot), ()):
                note = str(event.get("note") or "").strip()
                if note and any(rx.match(note) for rx in compiled):
                    out.append(dict(event))
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
        # #3375 : le journal ciblé des holds, alimenté par les témoins
        self.holds: dict[int, list[dict]] = {}
        self.hold_clock = 10000
        # #3375 : l'autorité de pilote est simulée ; une panne doit lever.
        self.role_error: Exception | None = None
        self.role_verdicts: dict[str, bool] = {}
        self.store = mock.Mock(work=FakeWork(rows),
                               progress=FakeProgress(self.marks, self.holds))
        self.store.work_dependencies.for_items.return_value = []
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

        def fake_orchestrator(cfg, db, name, **kw):
            if self.role_error is not None:
                raise self.role_error
            return bool(self.role_verdicts.get(name, False))

        with mock.patch.object(storage, "of", return_value=self.store), \
                mock.patch.object(work, "get",
                                  side_effect=lambda db, lot: next(
                                      (dict(row) for row in self.rows
                                       if row["id"] == int(lot)), None)), \
                mock.patch.object(lot_class, "of_lot", side_effect=fake_class), \
                mock.patch.object(assignments, "is_orchestrator",
                                  side_effect=fake_orchestrator), \
                mock.patch.object(merge_queue, "actor_family",
                                  side_effect=family or fake_family):
            return packet_candidates.inventory(
                object(), SimpleNamespace(name="ameesh", target="main"),
                repo=self.repo, clone=self.clone, target="main", cfg=None, **kwargs)

    def by_id(self, payload):
        return {entry["work_item"]: entry for entry in payload["candidates"]}

    def test_validated_lot_is_a_candidate_with_the_same_review_gate(self):
        row = next(row for row in self.rows if row["id"] == 1)
        row["state"] = "validated"
        self.assertIn(1, self.inventory()["ready"])
        self.marks[1] = [m for m in self.marks[1] if m["kind"] != "verdict"]
        self.assertNotIn(1, self.inventory()["ready"])

    def test_ready_lot_is_assigned_and_listed(self):
        payload = self.inventory()
        self.assertEqual(payload["schema"], "ameesh-packet-candidates/1")
        entry = self.by_id(payload)[1]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertEqual(entry["published_head"], self.tips[1])
        self.assertEqual(entry["frozen_sha"], self.tips[1])
        self.assertIn(1, payload["ready"])
        self.assertEqual(payload["errors"], [])

    # ------------------------------------------------------------------
    # #3375 : un hold de pilote actif exclut le lot de la liste calculée
    # ------------------------------------------------------------------

    def test_blocking_dependency_outside_qa_excludes_reviewed_lot(self):
        # #4139/#4134 : le parent waiting_human n'est pas candidat lui-même.
        self.store.work_dependencies.for_items.return_value = [
            {"work_item_id": 1, "blocker_id": 4134,
             "blocker_state": "waiting_human", "blocking": True}]
        payload = self.inventory()
        entry = self.by_id(payload)[1]
        self.assertFalse(entry["ready"])
        self.assertNotIn(1, payload["ready"])
        self.assertIn(1, payload["waiting"])
        self.assertEqual(entry["causes"][0]["code"], "dependency-blocking")
        self.assertIn("#4134 waiting_human", candidates_script.render(payload))

    def test_merged_dependency_leaves_reviewed_lot_ready(self):
        self.store.work_dependencies.for_items.return_value = [
            {"work_item_id": 1, "blocker_id": 4134,
             "blocker_state": "merged", "blocking": False}]
        self.assertIn(1, self.inventory()["ready"])

    def test_hold_override_cannot_lift_a_blocking_dependency(self):
        self.hold(1, "hold: attente")
        self.store.work_dependencies.for_items.return_value = [
            {"work_item_id": 1, "blocker_id": 4134,
             "blocker_state": "waiting_human", "blocking": True}]
        entry = self.by_id(self.inventory(hold_overrides={1: "urgence"}))[1]
        self.assertFalse(entry["ready"])
        self.assertEqual([c["code"] for c in entry["causes"]], ["dependency-blocking"])

    def test_unreadable_dependencies_refuse_the_inventory(self):
        self.store.work_dependencies.for_items.side_effect = RuntimeError("offline")
        with self.assertRaisesRegex(packet_candidates.CandidateError,
                                    "cannot read blocking dependencies: offline"):
            self.inventory()

    def hold(self, lot: int, note: str, *, actor: str = "human:smichea") -> dict:
        """Ajoute un événement hold/levée au journal ciblé simulé."""
        self.hold_clock += 1
        event = {"id": self.hold_clock, "work_item_id": int(lot), "state": "qa",
                 "actor": actor, "note": note,
                 "created_ts": float(self.hold_clock)}
        self.holds.setdefault(int(lot), []).append(event)
        return event

    def test_pilot_hold_excludes_a_frozen_and_reviewed_lot(self):
        """Le témoin #3375 : gelé, avisé, sans conflit — et pourtant pas candidat.

        #3231 était exactement dans cet état sous le hold du pilote du 24/10
        03:18Z ; le calcul le proposait quand même, et le composeur l'a mis
        dans h-09. Le motif doit être « hold pilote » pour la table
        « Écartés » du manifeste.
        """
        self.role_verdicts["mesh-design"] = True
        self.hold(1, "hold: témoin EP7, premier paquet après T0", actor="mesh-design")
        payload = self.inventory()
        entry = self.by_id(payload)[1]
        self.assertTrue(entry["freeze"]["sha"], entry["freeze"])
        self.assertFalse(entry["ready"])
        self.assertEqual([cause["code"] for cause in entry["causes"]], ["pilot-hold"])
        self.assertIn("hold pilote", entry["causes"][0]["detail"])
        self.assertIn("mesh-design", entry["causes"][0]["detail"])
        self.assertEqual(entry["pilot_hold"]["by"], "mesh-design")
        self.assertNotIn(1, payload["ready"])
        self.assertIn(1, payload["waiting"])
        self.assertIn("pilot-hold", candidates_script.render(payload))
        self.assertIn("hold pilote", candidates_script.render(payload))

    def test_unreadable_pilot_authority_is_a_read_error_not_a_free_lot(self):
        """B1 (#3375) : une autorité de pilote illisible ne vaut pas « non ».

        Tant que `_pilot_allowed` avalait l'erreur, un hold de mesh-design
        devenait invisible et le lot repassait prêt : la porte du paquet doit
        refuser de conclure, et l'appelant retirer sa liste prête.
        """
        self.role_verdicts["mesh-design"] = True
        self.hold(1, "hold: témoin EP7, premier paquet après T0", actor="mesh-design")
        self.role_error = RuntimeError("registry unavailable")
        with self.assertRaises(packet_candidates.CandidateError) as caught:
            self.inventory()
        self.assertIn("pilot holds", str(caught.exception))
        self.assertIn("registry unavailable", str(caught.exception))

    def test_unreadable_authority_does_not_hide_a_human_hold(self):
        """L'humain est pilote sans lire le registre : la panne ne l'efface pas."""
        self.role_error = RuntimeError("registry unavailable")
        self.hold(1, "hold: décision humaine", actor="human:smichea")
        entry = self.by_id(self.inventory())[1]
        self.assertFalse(entry["ready"])
        self.assertEqual([cause["code"] for cause in entry["causes"]], ["pilot-hold"])
        self.assertEqual(entry["pilot_hold"]["by"], "human:smichea")

    def test_unhold_gives_the_lot_back_to_the_candidates(self):
        """Le même lot, après le `unhold:` du pilote, redevient candidat."""
        self.role_verdicts["mesh-design"] = True
        self.hold(1, "hold: témoin EP7, premier paquet après T0", actor="mesh-design")
        self.assertFalse(self.by_id(self.inventory())[1]["ready"])
        self.hold(1, "unhold: décision du pilote (premier paquet post-T0 atteint)",
                  actor="mesh-design")
        entry = self.by_id(self.inventory())[1]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertNotIn("pilot-hold", [cause["code"] for cause in entry["causes"]])

    def test_an_assignee_hold_is_not_a_pilot_hold(self):
        """Un `hold:` ordinaire (règle 1 du réveil) ne réserve pas le paquet.

        L'autorité est lue (le registre répond « non pilote ») : c'est un
        « non » certain, pas une panne.
        """
        self.hold(1, "hold: je reprends demain", actor="ameesh-dev5")
        entry = self.by_id(self.inventory())[1]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertIsNone(entry["pilot_hold"])

    def test_hold_override_lifts_only_the_hold_and_is_labelled(self):
        """Le geste explicite du composeur : le hold tombe, les autres refus restent."""
        self.role_verdicts["mesh-design"] = True
        self.hold(1, "hold: témoin EP7, premier paquet après T0", actor="mesh-design")
        payload = self.inventory(hold_overrides={1: "premier paquet post-T0 atteint"})
        entry = self.by_id(payload)[1]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertEqual(entry["hold_override"], "premier paquet post-T0 atteint")
        self.assertNotIn("pilot-hold", [cause["code"] for cause in entry["causes"]])
        self.assertIn(1, payload["ready"])
        self.assertIn("hold pilote surchargé", candidates_script.render(payload))
        # le hold n'est pas levé : la liste sans surcharge l'écarte encore
        self.assertFalse(self.by_id(self.inventory())[1]["ready"])

    def test_hold_override_does_not_bypass_another_refusal(self):
        """Une surcharge ne lève que le hold : #2 garde son gel en retard."""
        self.role_verdicts["mesh-design"] = True
        self.hold(2, "hold: témoin", actor="mesh-design")
        entry = self.by_id(self.inventory(hold_overrides={2: "raison"}))[2]
        self.assertFalse(entry["ready"])
        self.assertEqual(entry["hold_override"], "raison")
        self.assertIn("freeze-not-head", [cause["code"] for cause in entry["causes"]])

    def test_hold_override_needs_an_active_hold_and_a_reason(self):
        with self.assertRaises(packet_candidates.HoldOverrideError):
            self.inventory(hold_overrides={1: "sans hold actif"})
        self.role_verdicts["mesh-design"] = True
        self.hold(1, "hold: témoin", actor="mesh-design")
        with self.assertRaises(packet_candidates.HoldOverrideError):
            self.inventory(hold_overrides={1: "   "})
        with self.assertRaises(packet_candidates.HoldOverrideError):
            self.inventory(hold_overrides={99: "lot inconnu"})
        with self.assertRaises(packet_candidates.HoldOverrideError):
            self.inventory(hold_overrides={1: "raison", "x": "raison"})

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
        # #3813 : la même erreur, attribuée au lot, pour un export utilisable.
        self.assertIn(1, [item["work_item"] for item in payload["errors_by_lot"]])
        self.assertIn("aliases", next(
            item["error"] for item in payload["errors_by_lot"]
            if item["work_item"] == 1))
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

    def test_reviewed_remise_cannot_lift_a_blocking_dependency(self):
        self._remise(4, "main-abc", {"shared.py": "FOUR MAIN\ntwo\n"}, verdict="ok")
        self.store.work_dependencies.for_items.return_value = [
            {"work_item_id": 4, "blocker_id": 4134,
             "blocker_state": "waiting_human", "blocking": True}]
        entry = self.by_id(self.inventory())[4]
        self.assertTrue(entry["remise"])
        self.assertFalse(entry["ready"])
        self.assertEqual([c["code"] for c in entry["causes"]], ["dependency-blocking"])

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


class StrictPilotAuthorityTest(unittest.TestCase):
    """#3375 (B1) : autorité de pilote lue en strict.

    Le contrat partagé garde son défaut tolérant (alertes, stagnation) ; la
    porte du paquet demande `strict=True` pour distinguer un acteur « non
    pilote » d'une autorité **indéterminée** (registre, canon, alias).
    """

    cfg = SimpleNamespace(canon_entries=(), canon="", canon_ref="",
                          canon_untrusted=False)

    def _resolve(self):
        return mock.patch("ameesh.agent_names.resolve",
                          side_effect=lambda name, **kw: name)

    def test_strict_orchestrator_propagates_a_registry_failure(self):
        with mock.patch.dict(os.environ, {assignments.ORCHESTRATORS_ENV: ""}), \
                mock.patch("ameesh.agent_names.resolve",
                           side_effect=RuntimeError("registry unavailable")):
            with self.assertRaises(RuntimeError) as caught:
                assignments.is_orchestrator(self.cfg, object(), "mesh-design",
                                            history=False, strict=True)
            self.assertIn("registry unavailable", str(caught.exception))
            # défaut inchangé : une panne ne fait pas taire les alertes
            self.assertFalse(assignments.is_orchestrator(
                self.cfg, object(), "mesh-design", history=False))

    def test_strict_orchestrator_propagates_an_unloaded_canon(self):
        with mock.patch.dict(os.environ, {assignments.ORCHESTRATORS_ENV: ""}), \
                self._resolve(), \
                mock.patch.object(canon, "configured", return_value=(object(),)), \
                mock.patch.object(canon, "load_configured", return_value=[]):
            with self.assertRaises(RuntimeError) as caught:
                assignments.is_orchestrator(self.cfg, object(), "mesh-design",
                                            history=False, strict=True)
        self.assertIn("canon unreadable", str(caught.exception))

    def test_strict_orchestrator_propagates_a_partially_read_canon(self):
        with mock.patch.dict(os.environ, {assignments.ORCHESTRATORS_ENV: ""}), \
                self._resolve(), \
                mock.patch.object(canon, "configured", return_value=(object(),)), \
                mock.patch.object(canon, "load_configured", return_value=[object()]), \
                mock.patch.object(sous_utilisation, "fully_read", return_value=False):
            with self.assertRaises(RuntimeError) as caught:
                assignments.is_orchestrator(self.cfg, object(), "mesh-design",
                                            history=False, strict=True)
        self.assertIn("canon partially read", str(caught.exception))

    def test_pilot_predicate_propagates_only_in_strict_mode(self):
        with mock.patch.object(assignments, "is_orchestrator",
                               side_effect=RuntimeError("registry unavailable")):
            with self.assertRaises(RuntimeError):
                planned_waits._pilot_allowed(object(), None, strict=True)("mesh-design")
            self.assertFalse(planned_waits._pilot_allowed(object(), None)("mesh-design"))

    def test_active_holds_strict_refuses_an_unreadable_pilot_authority(self):
        events = [{"id": 1, "work_item_id": 7, "state": "qa",
                   "actor": "mesh-design", "note": "hold: témoin",
                   "created_ts": 1.0}]
        with mock.patch.object(assignments, "is_orchestrator",
                               side_effect=RuntimeError("registry unavailable")):
            with self.assertRaises(RuntimeError):
                planned_waits.active_holds(object(), events, strict=True)
            held = planned_waits.active_holds(object(), events)
        self.assertFalse(held[7]["mesh-design"]["pilot"])


class CodeProvenanceTest(unittest.TestCase):
    """#3782 : le code exécuté (le `src` importé) est la tête de la cible.

    Le refus doit couvrir les deux sens du décalage (code en avance ou en
    retard), un dépôt sans la tête de la cible, un arbre non commité et un
    HEAD illisible. Un calcul fait depuis un vieux checkout ne doit plus
    pouvoir passer pour un calcul sur main.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3782-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "banc")
        _git(self.repo, "config", "user.email", "banc@example.invalid")
        _git(self.repo, "config", "commit.gpgsign", "false")
        _write(self.repo, "src/ameesh/__init__.py", "one\n")
        _commit(self.repo, "base")
        self.target = _git(self.repo, "rev-parse", "HEAD")

    def test_the_code_of_the_target_is_accepted(self):
        prov = candidates_script.code_provenance(self.repo, self.target)
        self.assertEqual(prov["code_sha"], self.target)
        self.assertEqual(prov["code_relation"], "same")
        self.assertTrue(prov["code_contains_ref"])
        self.assertFalse(prov["code_dirty"])
        self.assertEqual(candidates_script.code_refusal(prov, self.target), "")

    def test_a_code_ahead_of_the_target_is_refused(self):
        _write(self.repo, "src/ameesh/next.py", "two\n")
        ahead = _commit(self.repo, "next")
        prov = candidates_script.code_provenance(self.repo, self.target)
        self.assertEqual(prov["code_sha"], ahead)
        self.assertEqual(prov["code_relation"], "ahead")
        self.assertTrue(prov["code_contains_ref"])
        refusal = candidates_script.code_refusal(prov, self.target)
        self.assertIn("en avance", refusal)
        self.assertIn(self.target[:12], refusal)
        self.assertIn("--allow-code-mismatch", refusal)

    def test_a_code_behind_the_target_is_refused(self):
        _write(self.repo, "src/ameesh/next.py", "two\n")
        ahead = _commit(self.repo, "next")
        _git(self.repo, "switch", "-q", "--detach", self.target)
        prov = candidates_script.code_provenance(self.repo, ahead)
        self.assertEqual(prov["code_relation"], "behind")
        self.assertFalse(prov["code_contains_ref"])
        self.assertIn("antérieur", candidates_script.code_refusal(prov, ahead))

    def test_an_unrelated_code_is_refused(self):
        _git(self.repo, "switch", "-q", "--orphan", "autre")
        _write(self.repo, "src/ameesh/autre.py", "x\n")
        _commit(self.repo, "autre")
        prov = candidates_script.code_provenance(self.repo, self.target)
        self.assertNotEqual(prov["code_sha"], self.target)
        self.assertEqual(prov["code_relation"], "diverged")
        self.assertFalse(prov["code_contains_ref"])
        self.assertIn("sans rapport",
                      candidates_script.code_refusal(prov, self.target))

    def test_an_absent_target_is_refused(self):
        missing = "f" * 40
        prov = candidates_script.code_provenance(self.repo, missing)
        self.assertEqual(prov["code_relation"], "absent")
        self.assertIn("absente", candidates_script.code_refusal(prov, missing))

    def test_a_dirty_code_tree_is_refused(self):
        _write(self.repo, "src/ameesh/__init__.py", "modified\n")
        prov = candidates_script.code_provenance(self.repo, self.target)
        self.assertTrue(prov["code_dirty"])
        self.assertEqual(prov["code_relation"], "dirty")
        self.assertIn("non commit", candidates_script.code_refusal(prov, self.target))

    def test_an_untracked_module_shadow_is_refused(self):
        # Relecture cx2 B/P2 : un paquet non suivi sous src masque un module.
        _write(self.repo, "src/ameesh/packet_candidates/__init__.py", "shadow\n")
        prov = candidates_script.code_provenance(self.repo, self.target)
        self.assertTrue(prov["code_dirty"])
        self.assertEqual(prov["code_relation"], "dirty")
        self.assertIn("non commit",
                      candidates_script.code_refusal(prov, self.target))

    def test_an_unreadable_head_is_refused(self):
        prov = candidates_script.code_provenance(
            os.path.join(self.tmp, "absent"), self.target)
        self.assertEqual(prov["code_relation"], "unknown")
        self.assertIn("inconnu", candidates_script.code_refusal(prov, self.target))


class CodeReferenceTest(unittest.TestCase):
    """#3782 : la référence de l'outillage, distincte de la cible du projet.

    Pour le projet ameesh (même origine que le dépôt du code), la référence
    est la tête de la cible. Pour un autre projet (Nexlink), la cible est un
    commit du projet : la référence est `origin/main` du dépôt du code.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3782-ref-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = os.path.join(self.tmp, "origin.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", self.remote)
        self.repo = os.path.join(self.tmp, "code")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "banc")
        _git(self.repo, "config", "user.email", "banc@example.invalid")
        _git(self.repo, "config", "commit.gpgsign", "false")
        _write(self.repo, "src/ameesh/__init__.py", "one\n")
        _commit(self.repo, "base")
        _git(self.repo, "remote", "add", "origin", self.remote)
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        self.ref = _git(self.repo, "rev-parse", "HEAD")

    def project(self, name, remote=None):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        _git(path, "init", "-q", "-b", "develop")
        _git(path, "config", "user.name", "banc")
        _git(path, "config", "user.email", "banc@example.invalid")
        _write(path, "README", "x\n")
        _commit(path, "project")
        if remote:
            _git(path, "remote", "add", "origin", remote)
        return path

    def test_the_same_origin_uses_the_project_target(self):
        project = self.project("same", self.remote)
        ref, source = candidates_script.code_reference(
            self.repo, project, "a" * 40)
        self.assertEqual((ref, source), ("a" * 40, "project-target"))

    def test_another_project_uses_the_tooling_origin_main(self):
        other_remote = os.path.join(self.tmp, "other.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "develop", other_remote)
        other = self.project("other", other_remote)
        ref, source = candidates_script.code_reference(
            self.repo, other, "b" * 40)
        self.assertEqual(ref, self.ref)
        self.assertEqual(source, "tooling-origin-main")

    def test_an_unreadable_origin_main_is_unknown(self):
        local = os.path.join(self.tmp, "local")
        os.makedirs(local)
        _git(local, "init", "-q", "-b", "main")
        ref, source = candidates_script.code_reference(local, local, "a" * 40)
        self.assertEqual((ref, source), ("", "unknown"))


class ReadyOutTest(unittest.TestCase):
    """Relecture cx2 B3 : un inventaire incomplet ne laisse jamais d'export."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2404-ready-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = pathlib.Path(self.tmp) / "prets.txt"

    def provenance(self, relation="same", sha="a" * 40):
        return {"code_sha": sha, "code_repo": self.tmp,
                "code_relation": relation,
                "code_contains_ref": relation in ("same", "ahead", "dirty"),
                "code_dirty": relation == "dirty"}

    def payload(self, *, errors=None, errors_by_lot=None):
        return {
            "schema": "ameesh-packet-candidates/1", "project": "ameesh",
            "repo": self.tmp, "clone": self.tmp, "target": "main",
            "target_head": "a" * 40, "base": "a" * 40, "base_source": "target",
            "candidates": [], "ready": [], "waiting": [],
            "errors": list(errors or []),
            "errors_by_lot": list(errors_by_lot or []),
        }

    def test_ready_out_removed_when_inventory_has_errors(self):
        # #2404 B3 : une erreur non attribuée à un lot ne laisse aucun export.
        self.path.write_text("#100\n", encoding="utf-8")
        candidates_script.write_ready_out(self.path, {
            "target": "main", "base": "a" * 40, "ready": [100],
            "errors": ["#500 : lecture impossible"],
        })
        self.assertFalse(self.path.exists())

    def test_ready_out_lists_the_ready_lots_and_names_the_error_lots(self):
        # #3813 : l'erreur d'un lot n'invalide pas la liste des autres ; le
        # fichier dit explicitement lequel écarter, et paquet-inclusion le lit.
        self.path.write_text("#100\n", encoding="utf-8")
        listed = candidates_script.write_ready_out(self.path, {
            "target": "main", "base": "a" * 40, "ready": [100, 200],
            "errors": ["#500 : cannot read the review gate: provenance unknown"],
            "errors_by_lot": [{"work_item": 500, "error":
                               "cannot read the review gate: provenance unknown"}],
        })
        self.assertEqual(listed, {
            500: "cannot read the review gate: provenance unknown"})
        content = self.path.read_text(encoding="utf-8")
        self.assertIn("# prêts", content)
        self.assertIn("#100", content)
        self.assertIn("#200", content)
        self.assertIn("# erreurs", content)
        self.assertIn("!500 : cannot read the review gate: provenance unknown",
                      content)
        ids, errors = inclusion_script.read_ready_file(self.path)
        self.assertEqual(ids, [100, 200])
        self.assertEqual(errors, {
            500: "cannot read the review gate: provenance unknown"})

    def test_ready_out_refuses_an_error_that_is_also_ready(self):
        self.assertIsNone(candidates_script.write_ready_out(self.path, {
            "target": "main", "base": "a" * 40, "ready": [500],
            "errors": ["#500 : lecture impossible"],
            "errors_by_lot": [{"work_item": 500, "error": "lecture impossible"}],
        }))
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
        # #2404 B3 / #3813 : une erreur que l'inventaire ne rattache à aucun lot
        # reste globale : code 1 et aucun export. Une porte illisible, elle,
        # est attribuée au lot et laisse la liste utilisable (rc 3).
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
                mock.patch.object(candidates_script, "code_reference",
                                  return_value=("a" * 40, "project-target")), \
                mock.patch.object(candidates_script, "code_provenance",
                                  return_value=self.provenance()), \
                mock.patch.object(candidates_script.packet_candidates, "inventory",
                                  return_value=payload):
            code = candidates_script.main(
                ["--repo", self.tmp, "--project", "ameesh", "--target", "main",
                 "--ready-out", str(self.path)]
            )
        self.assertEqual(code, 1)
        self.assertFalse(self.path.exists())
        conn.close.assert_called_once()

    def test_script_exits_3_and_keeps_a_usable_export_on_a_per_lot_error(self):
        # #3813 : une porte illisible sur un lot n'invalide pas les autres.
        payload = self.payload(
            errors=["#1 : cannot read the review gate: registry unreadable"],
            errors_by_lot=[{"work_item": 1, "error":
                            "cannot read the review gate: registry unreadable"}])
        payload["ready"] = [7]
        payload["candidates"] = [{
            "work_item": 7, "published_head": "b" * 40,
            "branch": "lot/7-temoin", "review_class": {"class": "léger"},
            "source": "b" * 40, "assignee": "agent",
        }]
        project = SimpleNamespace(name="ameesh", target="main")
        conn = mock.Mock()
        stdout = io.StringIO()
        with mock.patch.object(candidates_script.config, "load",
                               return_value=object()), \
                mock.patch.object(candidates_script, "resolve_project",
                                  return_value=(project, True)), \
                mock.patch.object(candidates_script.db, "connect", return_value=conn), \
                mock.patch.object(candidates_script.merge_queue, "working_clone",
                                  return_value=(self.tmp, None)), \
                mock.patch.object(candidates_script, "code_reference",
                                  return_value=("a" * 40, "project-target")), \
                mock.patch.object(candidates_script, "code_provenance",
                                  return_value=self.provenance()), \
                mock.patch.object(candidates_script.packet_candidates, "inventory",
                                  return_value=payload), \
                contextlib.redirect_stdout(stdout):
            code = candidates_script.main(
                ["--repo", self.tmp, "--project", "ameesh", "--target", "main",
                 "--ready-out", str(self.path)])
        self.assertEqual(code, 3)
        content = self.path.read_text(encoding="utf-8")
        self.assertIn("#7", content)
        self.assertIn("!1 : cannot read the review gate: registry unreadable",
                      content)
        self.assertIn("liste reste utilisable", stdout.getvalue())
        conn.close.assert_called_once()

    def test_script_exits_1_and_clears_ready_out_on_a_pilot_authority_error(self):
        """#3375 B1 : une autorité illisible ne peut pas laisser une liste prête."""
        self.path.write_text("#100\n", encoding="utf-8")
        project = SimpleNamespace(name="ameesh", target="main")
        conn = mock.Mock()
        error = packet_candidates.CandidateError(
            "cannot read the active pilot holds: registry unavailable")
        with mock.patch.object(candidates_script.config, "load",
                               return_value=object()), \
                mock.patch.object(candidates_script, "resolve_project",
                                  return_value=(project, True)), \
                mock.patch.object(candidates_script.db, "connect", return_value=conn), \
                mock.patch.object(candidates_script.merge_queue, "working_clone",
                                  return_value=(self.tmp, None)), \
                mock.patch.object(candidates_script, "code_reference",
                                  return_value=("a" * 40, "project-target")), \
                mock.patch.object(candidates_script, "code_provenance",
                                  return_value=self.provenance()), \
                mock.patch.object(candidates_script.packet_candidates, "inventory",
                                  side_effect=error):
            code = candidates_script.main(
                ["--repo", self.tmp, "--project", "ameesh", "--target", "main",
                 "--ready-out", str(self.path)])
        self.assertEqual(code, 1)
        self.assertFalse(self.path.exists())
        conn.close.assert_called_once()

    def test_script_refuses_a_code_that_is_not_the_target_head(self):
        # #3782 : le calcul d'un vieux checkout ne rend plus une liste.
        self.path.write_text("#100\n", encoding="utf-8")
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
                                  return_value=self.payload()), \
                mock.patch.object(candidates_script, "code_reference",
                                  return_value=("a" * 40, "project-target")), \
                mock.patch.object(candidates_script, "code_provenance",
                                  return_value=self.provenance("behind", "b" * 40)):
            code = candidates_script.main(
                ["--repo", self.tmp, "--project", "ameesh", "--target", "main",
                 "--ready-out", str(self.path)])
        self.assertEqual(code, 2)
        self.assertFalse(self.path.exists())

    def test_script_runs_an_assumed_mismatch_and_says_it(self):
        project = SimpleNamespace(name="ameesh", target="main")
        conn = mock.Mock()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(candidates_script.config, "load",
                               return_value=object()), \
                mock.patch.object(candidates_script, "resolve_project",
                                  return_value=(project, True)), \
                mock.patch.object(candidates_script.db, "connect", return_value=conn), \
                mock.patch.object(candidates_script.merge_queue, "working_clone",
                                  return_value=(self.tmp, None)), \
                mock.patch.object(candidates_script.packet_candidates, "inventory",
                                  return_value=self.payload()), \
                mock.patch.object(candidates_script, "code_reference",
                                  return_value=("a" * 40, "project-target")), \
                mock.patch.object(candidates_script, "code_provenance",
                                  return_value=self.provenance("ahead", "b" * 40)), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = candidates_script.main(
                ["--repo", self.tmp, "--project", "ameesh", "--target", "main",
                 "--ready-out", str(self.path), "--allow-code-mismatch"])
        self.assertEqual(code, 0)
        self.assertIn("en avance sur la référence", stdout.getvalue())
        self.assertIn("DÉCALAGE ASSUMÉ", stdout.getvalue())
        self.assertIn("AVERTISSEMENT", stderr.getvalue())
        self.assertIn("code bbbbbbbbbbbb", self.path.read_text(encoding="utf-8"))

    def test_script_render_shows_the_code_sha_next_to_the_target(self):
        payload = self.payload()
        payload.update(self.provenance())
        payload["code_ref"] = "a" * 40
        payload["code_ref_source"] = "project-target"
        text = candidates_script.render(payload)
        self.assertIn("code aaaaaaaaaaaa", text)
        self.assertIn("réf aaaaaaaaaaaa : project-target", text)
        self.assertIn("la référence exacte", text)


class HoldOverrideJournalTest(unittest.TestCase):
    """#3375 : le drapeau et sa raison sont journalisés sur le lot.

    La sélection reste en lecture seule ; seule la surcharge explicite écrit,
    et uniquement quand elle a réellement rendu le lot prêt.
    """

    def test_reason_is_required_with_the_flag(self):
        with self.assertRaises(candidates_script.HoldOverrideError):
            candidates_script.parse_hold_overrides([7], None)
        with self.assertRaises(candidates_script.HoldOverrideError):
            candidates_script.parse_hold_overrides([7], "   ")
        with self.assertRaises(candidates_script.HoldOverrideError):
            candidates_script.parse_hold_overrides(None, "raison sans surcharge")
        self.assertEqual(
            candidates_script.parse_hold_overrides([7, 7], " premier  paquet "),
            {7: "premier paquet"})

    def payload(self, *, ready=True, hold_override="premier paquet post-T0"):
        entry = {"work_item": 7, "ready": ready, "hold_override": hold_override,
                 "pilot_hold": {"by": "mesh-design", "since": 1.0,
                                "note": "hold: témoin"},
                 "source": "b" * 40, "published_head": "b" * 40,
                 "branch": "lot/7", "assignee": "author", "review_class": None,
                 "causes": [] if ready else [{"code": "freeze-not-head",
                                              "detail": "gel en retard"}]}
        return {"project": "ameesh", "target": "main", "base": "a" * 40,
                "base_source": "target", "target_head": "a" * 40,
                "candidates": [entry], "ready": [7] if ready else [],
                "waiting": [] if ready else [7], "errors": []}

    def test_the_reason_is_written_on_the_lot(self):
        conn = mock.Mock()
        with mock.patch.object(candidates_script.work, "note") as note:
            written = candidates_script.journal_hold_overrides(
                conn, self.payload(), {7: "premier paquet post-T0"},
                actor="mty-amh-impl-01")
        note.assert_called_once()
        called = note.call_args
        self.assertEqual(called.args[1], 7)
        self.assertIn("hold-override", called.args[2])
        self.assertIn("hold pilote", called.args[2])
        self.assertIn("premier paquet post-T0", called.args[2])
        self.assertIn("mesh-design", called.args[2])
        self.assertEqual(called.kwargs["actor"], "mty-amh-impl-01")
        # la note de surcharge ne doit ni poser ni lever un hold : sinon le
        # journal du geste effacerait la retenue qu'il prétend surcharger.
        self.assertFalse(planned_waits.is_hold_note(called.args[2]))
        self.assertFalse(planned_waits.is_lift_note(called.args[2]))
        self.assertEqual(written, [{"lot": 7, "note": called.args[2]}])

    def test_a_surcharge_without_effect_writes_nothing(self):
        conn = mock.Mock()
        with mock.patch.object(candidates_script.work, "note") as note:
            with self.assertRaises(candidates_script.HoldOverrideError):
                candidates_script.journal_hold_overrides(
                    conn, self.payload(ready=False), {7: "raison"})
        note.assert_not_called()
        with mock.patch.object(candidates_script.work, "note") as note:
            with self.assertRaises(candidates_script.HoldOverrideError):
                candidates_script.journal_hold_overrides(
                    conn, self.payload(hold_override=""), {7: "raison"})
        note.assert_not_called()

    def test_main_refuses_without_reason_and_clears_the_export(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-3375-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = pathlib.Path(tmp) / "prets.txt"
        path.write_text("#1\n", encoding="utf-8")
        code = candidates_script.main(["--repo", tmp, "--hold-override", "7",
                                       "--ready-out", str(path)])
        self.assertEqual(code, 2)
        self.assertFalse(path.exists())

    def test_main_journals_the_override_before_exporting_the_lot(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-3375-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = pathlib.Path(tmp) / "prets.txt"
        project = SimpleNamespace(name="ameesh", target="main")
        conn = mock.Mock()
        with mock.patch.object(candidates_script.config, "load",
                               return_value=object()), \
                mock.patch.object(candidates_script, "resolve_project",
                                  return_value=(project, True)), \
                mock.patch.object(candidates_script.db, "connect", return_value=conn), \
                mock.patch.object(candidates_script.merge_queue, "working_clone",
                                  return_value=(tmp, None)), \
                mock.patch.object(candidates_script.packet_candidates, "inventory",
                                  return_value=self.payload()), \
                mock.patch.object(candidates_script, "code_reference",
                                  return_value=("a" * 40, "project-target")), \
                mock.patch.object(candidates_script, "code_provenance",
                                  return_value={"code_sha": "a" * 40,
                                                "code_repo": tmp,
                                                "code_relation": "same",
                                                "code_contains_ref": True,
                                                "code_dirty": False}), \
                mock.patch.object(candidates_script, "hold_actor",
                                  return_value="mty-amh-impl-01"), \
                mock.patch.object(candidates_script.work, "note") as note:
            code = candidates_script.main(
                ["--repo", tmp, "--project", "ameesh", "--target", "main",
                 "--hold-override", "7",
                 "--hold-reason", "premier paquet post-T0",
                 "--ready-out", str(path)])
        self.assertEqual(code, 0)
        note.assert_called_once()
        self.assertIn("hold-override", note.call_args.args[2])
        self.assertIn("#7", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
