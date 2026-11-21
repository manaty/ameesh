# SPDX-License-Identifier: AGPL-3.0-only
"""#7989 — un lot validé laissé en attente est nommé, au manifeste et sur le lot.

Le composeur ne donnait que le nombre de lots en attente. #7697, validé le
20/11 avec une migration 0601 non réservée, a manqué h-08 et h-09 sans motif
visible : son `migration-collision` restait dans le JSON des candidats. Le
calcul nomme désormais chaque validé en attente avec sa cause et le geste
attendu, et le premier calcul de composition écrit une note idempotente par
(lot, cause). Aucune base réelle : le journal est simulé.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

from ameesh import packet_candidates, work

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


candidates_script = _load_script("test_7989_paquet_candidats",
                                 "paquet-candidats.py")
composer_script = _load_script("test_7989_paquet_auto", "paquet-auto.py")


def payload() -> dict:
    """Un validé en attente (migration + avis), un qa (à ne pas noter)."""
    return {
        "waiting": [7, 8],
        "candidates": [
            {"work_item": 7, "state": "validated", "causes": [
                {"code": "migration-collision",
                 "detail": "migration 0601_veille.sql: number already on main "
                           "(0601_autre.sql): renumber, reserve, freeze again",
                 "number": 601, "case": "target"},
                {"code": "verdicts-missing", "detail": "1 missing"},
            ]},
            {"work_item": 8, "state": "qa", "causes": [
                {"code": "verdicts-missing", "detail": "review pending"},
            ]},
        ],
    }


class FakeJournal:
    """Le journal d'un lot : `conn.query` relit les marqueurs déjà écrits."""

    def __init__(self) -> None:
        self.notes: list[dict] = []
        self.markers: set[tuple[int, str]] = set()

    def query(self, _sql: str, params) -> list[dict]:
        lot, like = params
        marker = str(like)[:-1] if str(like).endswith("%") else str(like)
        return [{"present": 1}] if (int(lot), marker) in self.markers else []


def write_note(conn, lot, text, *, actor="", once=False) -> None:
    conn.notes.append({"lot": int(lot), "text": text, "actor": actor,
                       "once": once})
    code = text.split(":", 2)[1]
    conn.markers.add((int(lot), "paquet-attente:%s:" % code))


class WaitingGestureTest(unittest.TestCase):
    def test_migration_collision_names_the_reservation_command(self) -> None:
        gesture = packet_candidates.waiting_gesture(
            {"code": "migration-collision", "numbers": [601]}, 7697)
        self.assertIn("`ameesh work migration 7697 --number 601`", gesture)

    def test_target_collision_renumbers_instead_of_reserving_taken_number(self) -> None:
        gesture = packet_candidates.waiting_gesture(
            {"code": "migration-collision", "case": "target", "number": 601}, 7)
        self.assertIn("--number N`", gesture)
        self.assertNotIn("--number 601", gesture)
        self.assertIn("renommer", gesture)

    def test_behind_renumbers_above_target_head(self) -> None:
        gesture = packet_candidates.waiting_gesture(
            {"code": "migration-gap", "case": "behind", "number": 500,
             "target_head": 600}, 7)
        self.assertIn("--number N`", gesture)
        self.assertNotIn("--number 500", gesture)
        self.assertIn("au-dessus de la tête cible", gesture)
        self.assertIn("renommer", gesture)

    def test_gap_fills_missing_number_without_reusing_high_number(self) -> None:
        gesture = packet_candidates.waiting_gesture(
            {"code": "migration-gap", "case": "gap", "number": 603,
             "missing": 601, "target_head": 600}, 7)
        self.assertIn("--number N`", gesture)
        self.assertNotIn("--number 603", gesture)
        self.assertIn("comblant le trou", gesture)
        self.assertIn("premier numéro manquant : 601", gesture)
        self.assertIn("vérifier sa disponibilité", gesture)

    def test_all_uncovered_numbers_are_reserved(self) -> None:
        gesture = packet_candidates.waiting_gesture(
            {"code": "migration-collision", "numbers": [602, 601]}, 7)
        self.assertIn("`ameesh work migration 7 --number 601`", gesture)
        self.assertIn("`ameesh work migration 7 --number 602`", gesture)
        self.assertNotIn("renommer", gesture)

    def test_other_collisions_and_unknown_cases_do_not_reuse_faulty_number(self) -> None:
        for case in ("lot", "candidates", "", "unknown"):
            with self.subTest(case=case):
                gesture = packet_candidates.waiting_gesture(
                    {"code": "migration-collision", "case": case, "number": 601}, 7)
                self.assertIn("--number N`", gesture)
                self.assertNotIn("--number 601", gesture)

    def test_migration_gap_without_number_keeps_the_placeholder(self) -> None:
        gesture = packet_candidates.waiting_gesture(
            {"code": "migration-gap"}, 7697)
        self.assertIn("--number N", gesture)

    def test_unknown_cause_invents_no_gesture(self) -> None:
        self.assertEqual(packet_candidates.waiting_gesture({"code": "x"}, 1), "")

    def test_waiting_entries_keeps_only_validated_lots(self) -> None:
        self.assertEqual([entry["work_item"]
                          for entry in packet_candidates.waiting_entries(payload())],
                         [7])


class JournalWaitingTest(unittest.TestCase):
    def test_one_note_per_lot_and_cause_not_per_tick(self) -> None:
        conn = FakeJournal()
        with mock.patch.object(work, "note", side_effect=write_note):
            first = candidates_script.journal_waiting_notes(conn, payload())
            second = candidates_script.journal_waiting_notes(conn, payload())
        self.assertEqual([item["code"] for item in first],
                         ["migration-collision", "verdicts-missing"])
        self.assertEqual(second, [])
        # #7 seulement : un lot qa attend normalement sa relecture.
        self.assertEqual([note["lot"] for note in conn.notes], [7, 7])
        self.assertTrue(all(note["actor"] == "ameesh" for note in conn.notes))
        collision = next(note["text"] for note in conn.notes
                         if "migration-collision" in note["text"])
        self.assertTrue(collision.startswith("paquet-attente:migration-collision:"))
        self.assertIn("`ameesh work migration 7 --number N`", collision)
        self.assertIn("constat : migration 0601_veille.sql", collision)

    def test_an_existing_marker_blocks_a_second_note(self) -> None:
        conn = FakeJournal()
        conn.markers.add((7, "paquet-attente:verdicts-missing:"))
        with mock.patch.object(work, "note", side_effect=write_note):
            written = candidates_script.journal_waiting_notes(conn, payload())
        self.assertEqual([item["code"] for item in written], ["migration-collision"])

    def test_note_text_stays_bounded_and_single_line(self) -> None:
        text = candidates_script._waiting_note_text(
            7, {"code": "conflict", "detail": "a" * 900 + "\nb"})
        self.assertNotIn("\n", text)
        self.assertLess(len(text), 520)


class ManifestWaitingTest(unittest.TestCase):
    def test_manifest_names_each_validated_lot_with_its_cause_and_fix(self) -> None:
        composition = composer_script.Composition(units=[], discarded=[],
                                                  head="c" * 40)
        manifest = composer_script.build_manifest(
            name="banc", target="main", base_sha="a" * 40, carrier=99,
            composition=composition, table="", tests=[], freeze_seconds=0.0,
            waiting=2, waiting_entries=packet_candidates.waiting_entries(payload()))
        self.assertIn("## En attente (lots validés, motif calculé)", manifest)
        self.assertIn("| #7 | migration-collision: migration 0601_veille.sql", manifest)
        self.assertIn("`ameesh work migration 7 --number N`", manifest)
        self.assertIn("verdicts-missing", manifest)
        # Un lot qa n'est pas nommé : il attend sa relecture.
        self.assertNotIn("| #8 |", manifest)

    def test_manifest_without_waiting_keeps_the_old_shape(self) -> None:
        composition = composer_script.Composition(units=[], discarded=[],
                                                  head="c" * 40)
        manifest = composer_script.build_manifest(
            name="banc", target="main", base_sha="a" * 40, carrier=99,
            composition=composition, table="", tests=[], freeze_seconds=0.0)
        self.assertNotIn("## En attente", manifest)


if __name__ == "__main__":
    unittest.main()
