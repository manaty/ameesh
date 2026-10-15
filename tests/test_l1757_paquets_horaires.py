# SPDX-License-Identifier: AGPL-3.0-only
"""#1757 : la cadence des paquets horaires, compilée depuis le canon.

`packets.cutoffs` est un bloc optionnel de `processus/mecanique.yaml`, distinct
des coupures de release : absent, le lecteur rend un tuple vide explicite et
`last_packet`/`next_packet` refusent — jamais un repli sur `cutoffs` ni une
durée locale ; présent, il est validé strictement et une surcharge de projet
remplace le bloc entier. Le calendrier suit Europe/Paris : heure absente sautée
(23 occurrences), heure répétée en deux occurrences distinguées par leur epoch
(25 occurrences), sans doublon d'epoch."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from ameesh import canon_sync, mechanics

HOURS = ["%02d:00" % hour for hour in range(24)]

MECHANICS = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
packets:
  cutoffs: [%s]
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
projects: {}
""" % ", ".join('"%s"' % hour for hour in HOURS)

PACKETS_BLOCK = 'packets:\n  cutoffs: [%s]\n' % ", ".join('"%s"' % hour for hour in HOURS)

FEDERATION = """federation: "0.1"
id: manaty
review_policies:
  risk_classes:
    default: normal
    projects:
      ameesh:
        default: light
        rules:
          - paths:
              - "src/ameesh/mechanics.py"
            class: sensitive
      utc:
        default: light
        rules: []
  train_reviews:
    schema: ameesh-train-reviews-policy/1
    projects:
      ameesh:
        rules:
          - paths:
              - "src/ameesh/mechanics.py"
            reviews: 2
      utc:
        rules:
          - paths:
              - "**"
            reviews: 1
"""


def _paris(year: int, month: int, day: int, hour: int, minute: int = 0,
           fold: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Paris"),
                    fold=fold).timestamp()


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute,
                    tzinfo=ZoneInfo("UTC")).timestamp()


class _Repo:
    """Un dépôt git minimal : `federation.yaml` + `processus/mecanique.yaml`."""

    def __init__(self, root: str, mechanics_text: str | None = MECHANICS,
                 federation_text: str = FEDERATION):
        self.root = root
        self.dry = dict(os.environ,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git("init", "-q")
        if mechanics_text is not None:
            self.write("processus/mecanique.yaml", mechanics_text)
        self.write("federation.yaml", federation_text)
        self.commit("initial")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", self.root, *args],
                              capture_output=True, text=True, env=self.dry)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, path: str, text: str) -> None:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def repo(self, **kwargs) -> _Repo:
        return _Repo(self.tmp.name, **kwargs)

    def loaded(self, project: str = "ameesh", **kwargs) -> mechanics.Mechanics:
        repo = self.repo(**kwargs)
        return mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), project)


class TestCalendar(_Base):
    def test_canon_declares_hourly_packets_distinct_from_release_cutoffs(self):
        rules = self.loaded()
        # les coupures de release ne changent pas : 11:00 et 23:00 restent
        self.assertEqual(rules.cutoffs, ("11:00", "23:00"))
        self.assertEqual(rules.packet_cutoffs, tuple(HOURS))
        self.assertEqual(rules.to_dict()["packets"]["cutoffs"], HOURS)
        day = rules.packets_on(date(2026, 10, 15))
        self.assertEqual([label for _stamp, label in day], HOURS)
        self.assertEqual([stamp for stamp, _ in day],
                         [_paris(2026, 10, 15, hour) for hour in range(24)])

    def test_spring_forward_skips_the_missing_hour(self):
        rules = self.loaded()
        day = rules.packets_on(date(2026, 3, 29))
        labels = [label for _stamp, label in day]
        self.assertEqual(len(day), 23)
        self.assertNotIn("02:00", labels)
        self.assertIn("03:00", labels)
        by_label = {label: stamp for stamp, label in day}
        self.assertEqual(by_label["03:00"], _paris(2026, 3, 29, 3))
        # 01:00 puis 03:00 : une seule heure d'écart, l'heure absente est sautée
        self.assertEqual(by_label["03:00"] - by_label["01:00"], 3600)
        self.assertEqual(len({stamp for stamp, _ in day}), 23)

    def test_fall_back_repeats_the_hour_with_two_distinct_epochs(self):
        rules = self.loaded()
        day = rules.packets_on(date(2026, 10, 25))
        self.assertEqual(len(day), 25)
        twice = [stamp for stamp, label in day if label == "02:00"]
        self.assertEqual(len(twice), 2)
        self.assertNotEqual(twice[0], twice[1])
        self.assertEqual(twice[0], _paris(2026, 10, 25, 2, fold=0))
        self.assertEqual(twice[1], _paris(2026, 10, 25, 2, fold=1))
        stamps = [stamp for stamp, _ in day]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), 25)

    def test_last_and_next_packet_cross_day_bounds(self):
        rules = self.loaded()
        self.assertEqual(rules.last_packet(_paris(2026, 10, 15, 0, 15)),
                         _paris(2026, 10, 15, 0))
        self.assertEqual(rules.next_packet(_paris(2026, 10, 15, 23, 30)),
                         _paris(2026, 10, 16, 0))
        # à l'instant exact d'un paquet, il est le dernier, pas le suivant
        self.assertEqual(rules.last_packet(_paris(2026, 10, 15, 12)),
                         _paris(2026, 10, 15, 12))
        self.assertEqual(rules.next_packet(_paris(2026, 10, 15, 12)),
                         _paris(2026, 10, 15, 13))

    def test_last_and_next_distinguish_the_repeated_hour(self):
        rules = self.loaded()
        between = _paris(2026, 10, 25, 2, 30, fold=0)
        self.assertEqual(rules.last_packet(between), _paris(2026, 10, 25, 2, fold=0))
        self.assertEqual(rules.next_packet(between), _paris(2026, 10, 25, 2, fold=1))
        after = _paris(2026, 10, 25, 2, 30, fold=1)
        self.assertEqual(rules.last_packet(after), _paris(2026, 10, 25, 2, fold=1))
        self.assertEqual(rules.next_packet(after), _paris(2026, 10, 25, 3))

    def test_absent_block_is_an_explicit_empty_cadence(self):
        rules = self.loaded(mechanics_text=MECHANICS.replace(PACKETS_BLOCK, ""))
        self.assertEqual(rules.packet_cutoffs, ())
        self.assertEqual(rules.to_dict()["packets"], {"cutoffs": []})
        self.assertEqual(rules.packets_on(date(2026, 10, 15)), ())
        for call in (rules.last_packet, rules.next_packet):
            with self.assertRaises(mechanics.MechanicsError):
                call(_paris(2026, 10, 15, 12))
        # les coupures de release restent lisibles : aucun repli croisé
        self.assertEqual(rules.cutoff(_paris(2026, 10, 15, 12)),
                         _paris(2026, 10, 15, 11))

    def test_changed_cadence_is_compiled_at_the_exact_commit(self):
        repo = self.repo()
        first = repo.git("rev-parse", "HEAD")
        repo.write("processus/mecanique.yaml", MECHANICS.replace(
            PACKETS_BLOCK, 'packets:\n  cutoffs: ["06:00", "18:00"]\n'))
        second = repo.commit("cadence de paquets")
        before = mechanics.load(repo.root, first, "ameesh")
        after = mechanics.load(repo.root, second, "ameesh")
        when = _paris(2026, 10, 15, 12)
        self.assertEqual(before.next_packet(when), _paris(2026, 10, 15, 13))
        self.assertEqual(after.next_packet(when), _paris(2026, 10, 15, 18))
        self.assertEqual(len(after.packet_cutoffs), 2)
        self.assertEqual(len(before.packet_cutoffs), 24)


class TestOverrides(_Base):
    def test_project_override_replaces_the_whole_packet_block(self):
        text = MECHANICS.replace("projects: {}", """projects:
  utc:
    timezone: UTC
    packets:
      cutoffs: ["06:00", "18:00"]
""")
        repo = self.repo(mechanics_text=text)
        head = repo.git("rev-parse", "HEAD")
        utc = mechanics.load(repo.root, head, "utc")
        ameesh = mechanics.load(repo.root, head, "ameesh")
        self.assertEqual(utc.packet_cutoffs, ("06:00", "18:00"))
        self.assertEqual(utc.next_packet(_paris(2026, 10, 15, 12)),
                         _utc(2026, 10, 15, 18))
        self.assertEqual(ameesh.packet_cutoffs, tuple(HOURS))

    def test_override_without_packets_inherits_the_global_block(self):
        text = MECHANICS.replace("projects: {}", """projects:
  utc:
    timezone: UTC
    cutoffs: ["06:00"]
""")
        repo = self.repo(mechanics_text=text)
        utc = mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "utc")
        self.assertEqual(utc.cutoffs, ("06:00",))
        self.assertEqual(utc.packet_cutoffs, tuple(HOURS))


class TestStrictness(_Base):
    def test_invalid_packet_blocks_are_refused(self):
        cases = {
            "missing cutoffs": MECHANICS.replace(PACKETS_BLOCK, "packets: {}\n"),
            "empty list": MECHANICS.replace(PACKETS_BLOCK, 'packets:\n  cutoffs: []\n'),
            "bad time": MECHANICS.replace(PACKETS_BLOCK, 'packets:\n  cutoffs: ["24:00"]\n'),
            "duplicate": MECHANICS.replace(
                PACKETS_BLOCK, 'packets:\n  cutoffs: ["06:00", "06:00"]\n'),
            "unknown key": MECHANICS.replace(
                PACKETS_BLOCK, 'packets:\n  cutoffs: ["06:00"]\n  duration: 1h\n'),
            "empty block": MECHANICS.replace(PACKETS_BLOCK, "packets:\n"),
            "not a mapping": MECHANICS.replace(PACKETS_BLOCK, 'packets: ["06:00"]\n'),
        }
        for label, text in cases.items():
            with self.subTest(label=label), self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(text)

    def test_invalid_project_packet_override_is_refused(self):
        cases = {
            "empty list": 'projects:\n  utc:\n    packets:\n      cutoffs: []\n',
            "unknown key": ('projects:\n  utc:\n    packets:\n'
                            '      cutoffs: ["06:00"]\n      surprise: 1\n'),
            "empty block": 'projects:\n  utc:\n    packets: {}\n',
        }
        for label, block in cases.items():
            with self.subTest(label=label), self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(MECHANICS.replace("projects: {}", block))


class TestSyncReport(_Base):
    def _canon(self, commit: str, repo: str, mode: str = "git"):
        return SimpleNamespace(sources=[SimpleNamespace(mode=mode, commit=commit,
                                                        repo=repo, prefix="")])

    def test_sync_reports_the_packet_cadence(self):
        repo = self.repo()
        done = canon_sync.sync_mechanics(
            self._canon(repo.git("rev-parse", "HEAD"), repo.root))
        self.assertEqual(done.status, "ok")
        self.assertEqual(done.to_dict()["packets"], HOURS)

    def test_sync_reports_an_absent_cadence_as_empty(self):
        repo = self.repo(mechanics_text=MECHANICS.replace(PACKETS_BLOCK, ""))
        done = canon_sync.sync_mechanics(
            self._canon(repo.git("rev-parse", "HEAD"), repo.root))
        self.assertEqual(done.status, "ok")
        self.assertEqual(done.to_dict()["packets"], [])

    def test_sync_marks_an_invalid_packet_block(self):
        repo = self.repo(mechanics_text=MECHANICS.replace(
            PACKETS_BLOCK, 'packets:\n  cutoffs: []\n'))
        done = canon_sync.sync_mechanics(
            self._canon(repo.git("rev-parse", "HEAD"), repo.root))
        self.assertEqual(done.status, "invalid")
        self.assertIn("packets", done.to_dict()["error"])


if __name__ == "__main__":
    unittest.main()
