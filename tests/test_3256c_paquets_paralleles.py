# SPDX-License-Identifier: AGPL-3.0-only
"""#3256 C : second paquet en parallèle quand l'attente qa_merge dépasse 1 h.

Spec `docs/design/3256-debit-file.md` §4. Les témoins portent sur les calculs
déterministes (mesure journalière, partage en groupes disjoints) et sur le
composeur (`scripts/paquet-horaire.py`) : au-dessus du seuil de la veille,
deux porteurs, deux `--only` disjoints, aucun lot dans les deux ; sous le
seuil ou sans disjonction, un seul paquet.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
from zoneinfo import ZoneInfo

from ameesh import debit, mechanics, packet_candidates, packet_parallel

ROOT = Path(__file__).resolve().parents[1]
PARIS = ZoneInfo("Europe/Paris")
T = datetime(2026, 10, 28, 12, 0, tzinfo=PARIS).timestamp()
H = 3600.0


def at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=PARIS).timestamp()


class MechanicsBlockTest(unittest.TestCase):
    DOC = """schema: ameesh-mechanics/2
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
prevalidation:
  classes: [P0]
  contracts: [tests/parts/toujours.txt]
  hosts: [ameesh-agents-3]
  timeout_seconds: 60
%sprojects: {}
"""

    def test_block_absent_means_no_parallel_packet(self):
        document = mechanics.compile_document(self.DOC % "")
        self.assertIsNone(document.paquet)
        self.assertNotIn("paquet", document.to_dict())

    def test_block_is_strict_and_compiled(self):
        document = mechanics.compile_document(
            self.DOC % "paquet:\n  attente_mediane_s: 3600\n  max_paquets: 2\n")
        self.assertEqual(document.paquet.attente_mediane_s, 3600.0)
        self.assertEqual(document.paquet.max_paquets, 2)
        self.assertEqual(document.to_dict()["paquet"],
                         {"attente_mediane_s": 3600.0, "max_paquets": 2})
        for body in ("paquet:\n  attente_mediane_s: 3600\n  max_paquets: 3\n",
                     "paquet:\n  attente_mediane_s: 0\n  max_paquets: 2\n",
                     "paquet:\n  attente_mediane_s: 3600\n  max_paquets: 1\n",
                     "paquet:\n  attente_mediane_s: 3600\n",
                     "paquet:\n  attente_mediane_s: 3600\n  max_paquets: 2\n"
                     "  inconnu: 1\n"):
            with self.subTest(body=body), self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(self.DOC % body)


class DailyWaitsTest(unittest.TestCase):
    def row(self, lot, frozen_sha, composed):
        return {"work_item_id": lot, "frozen_sha": frozen_sha,
                "composed_ts": composed}

    def verdict(self, lot, at_ts, sha, verdict="ok"):
        return {"work_item_id": lot, "kind": "verdict", "verdict": verdict,
                "at_ts": at_ts, "sha": sha}

    def test_median_is_from_verdict_ok_to_packet_opening(self):
        sha = "a" * 40
        measure = packet_parallel.daily_waits(
            [self.row(1, sha, at(2026, 10, 27, 11, 0)),
             self.row(2, sha, at(2026, 10, 27, 12, 0)),
             self.row(3, sha, at(2026, 10, 27, 13, 0))],
            [self.verdict(1, at(2026, 10, 27, 9, 0), sha),
             self.verdict(2, at(2026, 10, 27, 10, 0), sha),
             self.verdict(3, at(2026, 10, 27, 11, 0), sha)],
            zone="Europe/Paris", now=T)
        yesterday = measure["yesterday"]
        self.assertEqual(yesterday["day"], "2026-10-27")
        self.assertEqual(yesterday["count"], 3)
        self.assertEqual(yesterday["median_s"], 2 * H)
        self.assertEqual(yesterday["p90_s"], 2 * H)

    def test_a_verdict_on_another_head_is_not_counted(self):
        sha, other = "a" * 40, "b" * 40
        measure = packet_parallel.daily_waits(
            [self.row(1, sha, at(2026, 10, 27, 11, 0))],
            [self.verdict(1, at(2026, 10, 27, 9, 0), other)],
            zone="Europe/Paris", now=T)
        self.assertEqual(measure["yesterday"]["count"], 0)
        self.assertEqual(measure["unmatched"], 1)

    def test_a_non_ok_or_blocked_verdict_is_not_counted(self):
        sha = "a" * 40
        measure = packet_parallel.daily_waits(
            [self.row(1, sha, at(2026, 10, 27, 11, 0))],
            [self.verdict(1, at(2026, 10, 27, 9, 0), sha, verdict="blocked")],
            zone="Europe/Paris", now=T)
        self.assertEqual(measure["yesterday"]["count"], 0)

    def test_the_first_packet_opening_wins_for_a_recomposed_lot(self):
        sha = "a" * 40
        measure = packet_parallel.daily_waits(
            [self.row(1, sha, at(2026, 10, 27, 11, 0)),
             self.row(1, sha, at(2026, 10, 27, 18, 0))],
            [self.verdict(1, at(2026, 10, 27, 9, 0), sha)],
            zone="Europe/Paris", now=T)
        self.assertEqual(measure["yesterday"]["count"], 1)
        self.assertEqual(measure["yesterday"]["median_s"], 2 * H)

    def test_day_boundary_is_the_canon_timezone(self):
        sha = "a" * 40
        # 23:30 UTC on 27/10 = 00:30 Paris on 28/10 → today, not yesterday
        measure = packet_parallel.daily_waits(
            [self.row(1, sha, datetime(2026, 10, 27, 23, 30,
                                       tzinfo=ZoneInfo("UTC")).timestamp())],
            [self.verdict(1, at(2026, 10, 27, 20, 0), sha)],
            zone="Europe/Paris", now=T)
        self.assertEqual(measure["yesterday"]["count"], 0)
        today = [row for row in measure["days"] if row["day"] == "2026-10-28"][0]
        self.assertEqual(today["count"], 1)

    def test_trigger_needs_yesterday_and_strictly_above_the_threshold(self):
        sha = "a" * 40
        measure = packet_parallel.daily_waits(
            [self.row(1, sha, at(2026, 10, 27, 11, 0))],
            [self.verdict(1, at(2026, 10, 27, 9, 0), sha)],
            zone="Europe/Paris", now=T)
        self.assertTrue(packet_parallel.trigger(measure, 3600.0, now=T, zone="Europe/Paris")["enabled"])
        # 2 h n'est pas strictement au-dessus de 2 h
        self.assertFalse(packet_parallel.trigger(measure, 2 * H, now=T, zone="Europe/Paris")["enabled"])
        self.assertFalse(packet_parallel.trigger(measure, 3 * H, now=T, zone="Europe/Paris")["enabled"])
        self.assertFalse(packet_parallel.trigger(
            {"yesterday": {"day": "2026-10-26", "count": 1, "median_s": 2 * H}},
            3600.0, now=T, zone="Europe/Paris")["enabled"])


class DebitSectionTest(unittest.TestCase):
    def test_the_qa_merge_section_is_in_the_report(self):
        sha = "a" * 40
        report = debit.build(
            items=[], events=[],
            milestones=[{"work_item_id": 1, "id": 2, "kind": "verdict",
                         "verdict": "ok", "sha": sha,
                         "at_ts": at(2026, 10, 27, 9, 0)}],
            packets=[{"work_item_id": 1, "frozen_sha": sha,
                      "composed_ts": at(2026, 10, 27, 11, 0)}],
            zone="Europe/Paris", now=T, installed="1.0")
        self.assertEqual(report["qa_merge"]["yesterday"]["count"], 1)
        self.assertEqual(report["qa_merge"]["yesterday"]["median_s"], 2 * H)


class PartitionTest(unittest.TestCase):
    def entry(self, lot, files=(), dependencies=()):
        return {"work_item": lot, "files": list(files),
                "dependencies": list(dependencies)}

    def test_two_disjoint_lots_make_two_packets(self):
        plan = packet_parallel.partition(
            [self.entry(1, ["a.py"]), self.entry(2, ["b.py"])], [1, 2])
        self.assertEqual(plan["first"], [1])
        self.assertEqual(plan["second"], [2])
        self.assertFalse(set(plan["first"]) & set(plan["second"]))

    def test_a_shared_file_keeps_the_lots_in_one_packet(self):
        plan = packet_parallel.partition(
            [self.entry(1, ["a.py"]), self.entry(2, ["a.py"])], [1, 2])
        self.assertEqual(plan["first"], [1, 2])
        self.assertEqual(plan["second"], [])

    def test_conflicts_and_distinct_migrations_stay_together(self):
        for metadata in ({"conflicts_with": [2]}, {"migration_files": ["schema/001.sql"]}):
            first = dict(self.entry(1, ["a.py"]), **metadata)
            second = self.entry(2, ["b.py"])
            if "migration_files" in metadata:
                second["migration_files"] = ["schema/002.sql"]
            with self.subTest(metadata=metadata):
                plan = packet_parallel.partition([first, second], [1, 2])
                self.assertEqual(plan["first"], [1, 2])
                self.assertEqual(plan["second"], [])

    def test_a_dependency_keeps_the_lots_in_one_packet(self):
        plan = packet_parallel.partition(
            [self.entry(1, ["a.py"]), self.entry(2, ["b.py"], dependencies=[1])],
            [1, 2])
        self.assertEqual(plan["first"], [1, 2])
        self.assertEqual(plan["second"], [])

    def test_two_components_are_balanced_and_keep_the_ready_order(self):
        plan = packet_parallel.partition(
            [self.entry(1, ["a.py"]), self.entry(2, ["b.py"]),
             self.entry(3, ["c.py"]), self.entry(4, ["d.py"])], [4, 3, 2, 1])
        self.assertEqual(len(plan["first"]), 2)
        self.assertEqual(len(plan["second"]), 2)
        self.assertEqual(plan["first"], sorted(plan["first"],
                                               key=[4, 3, 2, 1].index))
        self.assertFalse(set(plan["first"]) & set(plan["second"]))

    def test_a_lot_in_an_error_is_not_partitioned(self):
        # seuls les lots retenus (prêts) entrent : un lot absent de `order`
        # n'est pas composé.
        plan = packet_parallel.partition([self.entry(1, ["a.py"])], [1])
        self.assertEqual(plan["first"], [1])
        self.assertEqual(plan["second"], [])

    def test_changed_files_reads_the_lot_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = lambda *args: subprocess.run(
                ["git", "-C", tmp, *args], check=True, capture_output=True,
                text=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                                "GIT_COMMITTER_NAME": "t",
                                "GIT_COMMITTER_EMAIL": "t@x",
                                "PATH": "/usr/bin:/bin"})
            git("init", "-q")
            Path(tmp, "a.py").write_text("a = 1\n")
            git("add", "a.py")
            git("commit", "-qm", "base")
            base = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            git("checkout", "-q", "-b", "lot")
            Path(tmp, "b.py").write_text("b = 2\n")
            git("add", "b.py")
            git("commit", "-qm", "lot")
            head = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            self.assertEqual(packet_candidates.changed_files(tmp, base, head),
                             ["b.py"])


class ManifestEvidenceTest(unittest.TestCase):
    """#3256 C (B cx1) : la preuve de disjonction relit le manifeste figé."""

    TEXT = """# Paquet integration/paquet-h-1 — composition (§12), sur main `abc12345`

Base : `main` **`%s`** (tête exacte d'`origin/main`).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #10 | `%s` |
| #11 | `%s` |

## Écartés

| lot | motif |
|---|---|
| — | aucun |
""" % ("b" * 40, "c" * 40, "d" * 40)

    def test_base_and_admissions_are_read_from_the_frozen_manifest(self):
        self.assertEqual(packet_parallel.manifest_base(self.TEXT), "b" * 40)
        self.assertEqual(packet_parallel.manifest_units(self.TEXT),
                         [{"work_item": 10, "source": "c" * 40},
                          {"work_item": 11, "source": "d" * 40}])

    def test_a_manifest_without_admissions_proves_nothing(self):
        self.assertEqual(packet_parallel.manifest_units("## Écartés\n"),
                         [])
        self.assertEqual(packet_parallel.manifest_base("aucune base"), "")

    def test_an_illisible_admission_row_invalidates_the_table(self):
        broken = self.TEXT.replace("| #11 | `%s` |" % ("d" * 40), "| #11 | ? |")
        self.assertEqual(packet_parallel.manifest_units(broken), [])


# ---------------------------------------------------------------------------
# Composeur : deux porteurs, deux --only disjoints
# ---------------------------------------------------------------------------

SPEC = importlib.util.spec_from_file_location(
    "paquet_horaire_3256", ROOT / "scripts" / "paquet-horaire.py")
ph = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("paquet_horaire_3256", ph)
SPEC.loader.exec_module(ph)

from tests.test_3845_paquet_horaire import (  # noqa: E402
    FakeShell, MANIFEST, NOW, ZONE, carrier)

HEAD = "a" * 40


def candidate(lot, files):
    return {"work_item": lot, "files": list(files), "dependencies": [],
            "ready": True, "error": "", "work_item_id": lot}


class ParallelShell(FakeShell):
    def __init__(self):
        super().__init__()
        self.debit = {"qa_merge": {"yesterday": {"day": "2026-10-26",
                                                  "count": 3, "median_s": 5400.0}}}
        self.carriers = 0

    def run(self, cmd, *, cwd=None, input_text=None, timeout=0):
        if cmd[0] == "ameesh" and cmd[1:2] == ["debit"]:
            self.calls.append(list(cmd))
            return ph.Result(0, json.dumps(self.debit))
        if cmd[0] == "ameesh" and cmd[1:3] == ["work", "add"]:
            self.carriers += 1
            lot = 4000 + self.carriers
            args = list(cmd[2:])
            self.added.append(args)
            branch = args[args.index("--branch") + 1]
            title = args[args.index("--title") + 1]
            item = carrier(lot=lot, title=title, sha=HEAD)
            item["branch"] = branch
            item["events"] = []
            self.items[lot] = item
            return ph.Result(0, "lot #%d created in intake: x\n" % lot)
        return super().run(cmd, cwd=cwd, input_text=input_text, timeout=timeout)


class ComposerParallelTest(unittest.TestCase):
    def setUp(self):
        self.shell = ParallelShell()
        self.ctx = ph.Context(repo=ROOT, target="main", project="ameesh",
                              pilot="mesh-design", actor="mesh-design",
                              shell=self.shell, python="python3", now=NOW,
                              zone=ZONE,
                              parallel=SimpleNamespace(attente_mediane_s=3600.0,
                                                       max_paquets=2))
        self._saved = (ph.ready_lots, ph.candidates, ph.manifest_at,
                       ph.replay_locally, ph.next_name,
                       ph.auto.composition_holder)
        self.ready = [101, 102]
        self.payload = {"base": HEAD,
                        "candidates": [candidate(101, ["a.py"]),
                                       candidate(102, ["b.py"])],
                        "ready": [101, 102]}
        self.names = iter(["h-20261027-04", "h-20261027-05", "h-20261027-06"])
        ph.ready_lots = lambda ctx: list(self.ready)
        ph.candidates = lambda ctx: dict(self.payload)
        ph.manifest_at = lambda ctx, item, sha: MANIFEST
        ph.replay_locally = lambda ctx, sha, tests: ""
        ph.next_name = lambda ctx: next(self.names)
        # #4567 : le verrou de composition se lit dans le `.git` du checkout ;
        # le guest de prévalidation rejoue un snapshot sans dépôt. Comme le
        # Base de test_3845, les témoins remplacent cette lecture ; ceux du
        # verrou la patchent explicitement dans leur `with`.
        ph.auto.composition_holder = lambda repo, name: None

    def tearDown(self):
        (ph.ready_lots, ph.candidates, ph.manifest_at, ph.replay_locally,
         ph.next_name, ph.auto.composition_holder) = self._saved

    def autos(self):
        return [call for call in self.shell.calls
                if "paquet-auto.py" in " ".join(call)]

    def only(self, call):
        values = []
        for index, item in enumerate(call):
            if item == "--only":
                values.append(int(call[index + 1]))
        return values

    def test_above_the_threshold_two_disjoint_packets(self):
        ph.compose(self.ctx, [])
        self.assertEqual(len(self.autos()), 2)
        self.assertEqual([self.only(call) for call in self.autos()],
                         [[101], [102]])
        first, second = sorted(self.shell.items)
        note = [event["note"] for event in self.shell.items[second]["events"]
                if "second paquet parallèle" in event["note"]]
        self.assertTrue(note)
        self.assertIn("#%d" % first, note[0])
        # aucun lot dans les deux groupes
        self.assertFalse(set(self.only(self.autos()[0]))
                         & set(self.only(self.autos()[1])))

    def test_first_carrier_error_does_not_prevent_the_second(self):
        from unittest import mock
        original = ph.attempt
        def attempt(ctx, carrier, name, this):
            if this.only == [101]:
                raise ph.HoraireError("first failed")
            return original(ctx, carrier, name, this)
        with mock.patch.object(ph, "attempt", side_effect=attempt):
            result = ph.compose(self.ctx, [])
        self.assertIn("first failed", result)
        self.assertEqual(len(self.shell.items), 2)
        self.assertEqual(self.only(self.autos()[0]), [102])

    def test_missing_parallel_proof_recomposes_same_carrier_and_group(self):
        from unittest import mock
        item = carrier(lot=4002, sha=HEAD)
        item["events"] += [
            {"note": "paquet-horaire: second paquet parallèle de #4001 groupe=102"},
            {"note": "auto-merge parallel-recompose repo=manaty/ameesh pr=1 sha=%s missing clone" % HEAD}]
        with mock.patch.object(ph, "pending", return_value=None), \
                mock.patch.object(ph, "attempt", return_value="recomposed") as attempt:
            self.assertEqual(ph.follow(self.ctx, item), "recomposed")
        this = attempt.call_args.args[-1]
        self.assertEqual(this.only, [102])
        self.assertEqual(this.base, HEAD)
        self.assertEqual(this.step, ph.RECOMPOSITION)
        self.assertTrue(this.force)

    def test_below_the_threshold_one_packet(self):
        self.shell.debit["qa_merge"]["yesterday"]["median_s"] = 3599.0
        ph.compose(self.ctx, [])
        self.assertEqual(len(self.autos()), 1)
        self.assertEqual(self.only(self.autos()[0]), [])
        self.assertEqual(len(self.shell.items), 1)

    def test_a_shared_file_makes_one_packet(self):
        self.payload["candidates"] = [candidate(101, ["a.py"]),
                                      candidate(102, ["a.py"])]
        ph.compose(self.ctx, [])
        self.assertEqual(len(self.autos()), 1)
        self.assertEqual(self.only(self.autos()[0]), [])
        self.assertEqual(len(self.shell.items), 1)

    def test_without_the_canon_block_no_second_packet(self):
        self.ctx.parallel = None
        ph.compose(self.ctx, [])
        self.assertEqual(len(self.autos()), 1)
        self.assertFalse([call for call in self.shell.calls
                          if call[0] == "ameesh" and call[1:2] == ["debit"]])

    def test_publish_refused_keeps_the_group_on_recomposition(self):
        # #3256 C / B cx1 : le repli de publication refusée ne doit pas
        # recomposer toute la liste prête ; le porteur garde son groupe.
        self.shell.items[4002] = carrier(lot=4002, sha=HEAD)
        this = ph.Attempt(ph.COMPOSITION, 1, HEAD, only=[102],
                          publish="ref@" + "b" * 40)
        before = len(self.autos())
        ph.after_compose(self.ctx, 4002, "h-20261027-06",
                         ph.Result(ph.auto.PUBLISH_REFUSED, "refus\n", ""), this)
        calls = self.autos()[before:]
        self.assertTrue(calls)
        self.assertEqual(self.only(calls[-1]), [102])

    def test_the_pair_is_followed_together(self):
        ph.compose(self.ctx, [])
        self.shell.lists["qa"] = [self.shell.items[lot]
                                  for lot in sorted(self.shell.items)]
        text = ph.tick(self.ctx)
        self.assertIn("#4001", text)
        self.assertIn("#4002", text)


if __name__ == "__main__":
    unittest.main()


class ProjectPythonTest(unittest.TestCase):
    def test_explicit_interpreter_is_shared_with_freeze_checks(self):
        from unittest import mock
        from ameesh import project_python, packet_automerge
        import os
        with mock.patch.dict(os.environ, {"AMEESH_PYTHON": "/project/python"}), \
                mock.patch.object(project_python, "_PYTHON_INTERPRETER", None), \
                mock.patch.object(project_python.Path, "is_file", return_value=True), \
                mock.patch.object(project_python, "_supports_psycopg", return_value=True), \
                mock.patch.object(project_python.subprocess, "run", return_value=SimpleNamespace(stdout="")):
            self.assertEqual(project_python.project_python(Path("/repo")), "/project/python")
        with tempfile.TemporaryDirectory() as repo, \
                mock.patch.object(project_python, "project_python", return_value="/project/python"), \
                mock.patch.object(packet_automerge, "_git", return_value="ok"), \
                mock.patch.object(packet_automerge.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            state, _why = packet_automerge._parallel_freeze_checks(repo, "a" * 40, "b" * 40)
            self.assertEqual(state, "ok")
            self.assertEqual(run.call_args.args[0][0], "/project/python")
