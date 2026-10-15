# SPDX-License-Identifier: AGPL-3.0-only
"""#1616 : paramètres mécaniques compilés depuis le canon, lecteur strict.

Le canon `processus/mecanique.yaml` déclare les coupures et le fuseau, le
plafond de lots en développement, la CI par paquet et son étiquette d'incident,
les familles et l'indépendance des avis ; les classes sensibles et les nombres
d'avis viennent de `federation.yaml` au même commit. Aucun repli : changer le
canon change la coupure, et une règle absente ou inconnue refuse."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from ameesh import canon_sync, mechanics

MECHANICS = """schema: ameesh-mechanics/1
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
projects: {}
"""

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
              - "src/ameesh/migrations/**"
              - "**/*.sql"
            class: sensitive
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
              - "src/ameesh/migrations/**"
              - "**/*.sql"
              - "src/ameesh/mechanics.py"
            reviews: 2
          - paths:
              - "src/ameesh/conversation.py"
            reviews: 1
      utc:
        rules:
          - paths:
              - "**"
            reviews: 1
"""


def _without_train_path(path: str) -> str:
    """Retire un motif des règles d'avis, sans toucher au classement."""
    head, sep, tail = FEDERATION.partition("  train_reviews:")
    return head + sep + tail.replace('              - "%s"\n' % path, "", 1)


def _paris(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute,
                    tzinfo=ZoneInfo("Europe/Paris")).timestamp()


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


class MechanicsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def repo(self, **kwargs) -> _Repo:
        return _Repo(self.tmp.name, **kwargs)


class TestCutoffs(MechanicsTest):
    def test_cutoffs_and_timezone_follow_the_canon(self):
        repo = self.repo()
        rules = mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "ameesh")
        self.assertEqual(rules.timezone, "Europe/Paris")
        self.assertEqual(rules.cutoffs, ("11:00", "23:00"))
        # 12:00 Paris : la coupure courante est 11:00, la suivante 23:00.
        self.assertEqual(rules.cutoff(_paris(2026, 10, 15, 12)), _paris(2026, 10, 15, 11))
        self.assertEqual(rules.next_cutoff(_paris(2026, 10, 15, 12)),
                         _paris(2026, 10, 15, 23))
        # Avant la première coupure locale : celle de la veille.
        self.assertEqual(rules.cutoff(_paris(2026, 10, 15, 8)), _paris(2026, 10, 14, 23))

    def test_cutoff_crosses_dst(self):
        repo = self.repo()
        rules = mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "ameesh")
        # Le passage à l'heure d'hiver (25/10/2026) change l'écart à UTC.
        self.assertEqual(rules.cutoff(_paris(2026, 10, 25, 23, 30)), _paris(2026, 10, 25, 23))
        self.assertEqual(rules.cutoff(_paris(2026, 10, 26, 0, 30)), _paris(2026, 10, 25, 23))
        self.assertEqual(rules.cutoff(_paris(2026, 12, 1, 12)), _paris(2026, 12, 1, 11))

    def test_changed_canon_changes_the_cutoff(self):
        repo = self.repo()
        first = repo.git("rev-parse", "HEAD")
        repo.write("processus/mecanique.yaml",
                   MECHANICS.replace('cutoffs: ["11:00", "23:00"]',
                                     'cutoffs: ["09:30", "21:30"]'))
        second = repo.commit("cutoffs")
        before = mechanics.load(repo.root, first, "ameesh")
        after = mechanics.load(repo.root, second, "ameesh")
        when = _paris(2026, 10, 15, 12)
        self.assertEqual(before.cutoff(when), _paris(2026, 10, 15, 11))
        self.assertEqual(after.cutoff(when), _paris(2026, 10, 15, 9, 30))
        self.assertEqual(after.next_cutoff(when), _paris(2026, 10, 15, 21, 30))
        self.assertNotEqual(before.commit, after.commit)

    def test_project_override_replaces_timezone_and_cutoffs(self):
        text = MECHANICS.replace("projects: {}", """projects:
  utc:
    timezone: UTC
    cutoffs: ["06:00"]
""")
        repo = self.repo(mechanics_text=text)
        rules = mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "utc")
        self.assertEqual(rules.timezone, "UTC")
        self.assertEqual(rules.cutoffs, ("06:00",))
        self.assertEqual(rules.cutoff(_paris(2026, 10, 15, 12)),
                         datetime(2026, 10, 15, 6, tzinfo=ZoneInfo("UTC")).timestamp())


class TestRules(MechanicsTest):
    def test_build_limit_and_independence_come_from_the_canon(self):
        repo = self.repo()
        rules = mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "ameesh")
        self.assertEqual(rules.max_in_build, 2)
        self.assertEqual(rules.ci_packet, "full")
        self.assertEqual(rules.ci_incident_label, "ci-full")
        self.assertEqual(rules.ci_environment_red, "does_not_block")
        self.assertEqual(rules.ci_faulty_lot, "removed_without_new_review")
        self.assertEqual(rules.families, ("claude", "codex", "deepseek"))
        self.assertTrue(rules.exclude_author_family)
        self.assertTrue(rules.distinct_families)
        # §11 bis, allègements 15/10/2026 14:45 : fusion sans feu nominatif.
        self.assertFalse(rules.merge_named_fire)
        self.assertFalse(rules.merge_repeat_freeze)
        self.assertTrue(rules.merge_on_green_packet)
        self.assertEqual(rules.merge_post_merge_red, "forward_or_revert")

    def test_changed_ci_and_merge_rules_are_compiled(self):
        repo = self.repo()
        text = (MECHANICS.replace("environment_red: does_not_block", "environment_red: blocks")
                .replace("named_fire: false", "named_fire: true")
                .replace("post_merge_red: forward_or_revert", "post_merge_red: freeze"))
        repo.write("processus/mecanique.yaml", text)
        head = repo.commit("regles de fusion")
        rules = mechanics.load(repo.root, head, "ameesh")
        self.assertEqual(rules.ci_environment_red, "blocks")
        self.assertTrue(rules.merge_named_fire)
        self.assertEqual(rules.merge_post_merge_red, "freeze")

    def test_changed_limit_and_families_are_compiled(self):
        repo = self.repo()
        first = repo.git("rev-parse", "HEAD")
        text = (MECHANICS.replace("max_in_build: 2", "max_in_build: 3")
                .replace("families: [claude, codex, deepseek]", "families: [claude, codex]")
                .replace("distinct_families: true", "distinct_families: false"))
        repo.write("processus/mecanique.yaml", text)
        second = repo.commit("limites")
        rules = mechanics.load(repo.root, second, "ameesh")
        self.assertEqual(rules.max_in_build, 3)
        self.assertEqual(rules.families, ("claude", "codex"))
        self.assertFalse(rules.distinct_families)
        # La tête d'avant garde ses valeurs : la compilation est liée au commit.
        self.assertEqual(mechanics.load(repo.root, first, "ameesh").max_in_build, 2)

    def test_review_counts_and_classes_come_from_federation(self):
        repo = self.repo()
        rules = mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "ameesh")
        self.assertEqual(rules.reviews_for(["src/ameesh/migrations/0001.sql"]),
                         mechanics.ReviewNeed("sensitive", 2))
        self.assertEqual(rules.reviews_for(["README.md"]),
                         mechanics.ReviewNeed("light", 0))
        self.assertEqual(rules.reviews_for(["src/ameesh/conversation.py"]),
                         mechanics.ReviewNeed("sensitive", 1))

    def test_non_light_path_without_rule_is_refused(self):
        # mechanics.py est classé sensible mais aucune règle d'avis ne le couvre.
        repo = self.repo(federation_text=_without_train_path("src/ameesh/mechanics.py"))
        rules = mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "ameesh")
        with self.assertRaises(mechanics.MechanicsError):
            rules.reviews_for(["src/ameesh/mechanics.py"])
        # Light sans règle : zéro avis, jamais un refus.
        self.assertEqual(rules.reviews_for(["docs/x.md"]),
                         mechanics.ReviewNeed("light", 0))
        # Un chemin non décodable ne se classe pas : le deviner ferait passer
        # un fichier sensible pour léger.
        with self.assertRaises(mechanics.MechanicsError):
            rules.reviews_for(["src/ameesh/\ufffd.sql"])

    def test_unclassifiable_paths_never_receive_zero_reviews(self):
        repo = self.repo()
        rules = mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "ameesh")
        for path in ("", "/src/ameesh/mechanics.py", "../src/ameesh/mechanics.py",
                     "C:/src/ameesh/mechanics.py", r"src\ameesh\mechanics.py",
                     "src/ameesh/\udcff.py", "src/ameesh/x\0.py", None):
            with self.subTest(path=path), self.assertRaises(mechanics.MechanicsError):
                rules.reviews_for(["README.md", path])
        self.assertEqual(rules.reviews_for(["src/ameesh/mechanics.py"]),
                         mechanics.ReviewNeed("sensitive", 2))
        # ni liste vide ni chaîne nue : zéro avis serait un repli
        for paths in ([], (), "src/ameesh/mechanics.py"):
            with self.subTest(paths=paths), self.assertRaises(mechanics.MechanicsError):
                rules.reviews_for(paths)

    def test_malformed_federation_blocks_refuse_with_mechanics_error(self):
        from ameesh import canon
        import copy
        original = canon.load_yaml(FEDERATION, use_pyyaml=False)
        changes = [
            (("review_policies", "risk_classes", "projects"), None),
            (("review_policies", "risk_classes", "projects"), "ameesh"),
            (("review_policies", "train_reviews", "projects"), None),
            (("review_policies", "train_reviews", "projects"), "ameesh"),
            (("review_policies", "train_reviews", "surprise"), True),
            (("review_policies", "risk_classes", "projects", "ameesh", "rules"),
             [{"paths": ["**"], "class": "light", "surprise": True}]),
        ]
        for keys, value in changes:
            declaration = copy.deepcopy(original)
            target = declaration
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = value
            with self.subTest(keys=keys), self.assertRaises(mechanics.MechanicsError):
                mechanics._review_policy(declaration, "ameesh", "0" * 40)
        # `default` seul est une politique complète (review.parse) ; sans
        # `default`, la classification est incomplète
        declaration = copy.deepcopy(original)
        del declaration["review_policies"]["risk_classes"]["projects"]["ameesh"]["rules"]
        policy, _rules = mechanics._review_policy(declaration, "ameesh", "0" * 40)
        self.assertEqual(policy.default, "light")
        declaration = copy.deepcopy(original)
        del declaration["review_policies"]["risk_classes"]["projects"]["ameesh"]["default"]
        with self.assertRaises(mechanics.MechanicsError):
            mechanics._review_policy(declaration, "ameesh", "0" * 40)

    def test_unknown_risk_class_key_is_refused(self):
        repo = self.repo(federation_text=FEDERATION.replace(
            "        default: light", "        default: light\n        rulez: []", 1))
        with self.assertRaises(mechanics.MechanicsError):
            mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "ameesh")


class TestStrictness(MechanicsTest):
    def test_absent_document_is_refused(self):
        repo = self.repo(mechanics_text=None)
        with self.assertRaises(mechanics.MechanicsAbsent):
            mechanics.load_global(repo.root, repo.git("rev-parse", "HEAD"))

    def test_invalid_documents_are_refused(self):
        cases = {
            "schema": MECHANICS.replace("schema: ameesh-mechanics/1", "schema: ameesh-mechanics/2"),
            "unknown key": MECHANICS + "surprise: 1\n",
            "missing cutoffs": MECHANICS.replace('cutoffs: ["11:00", "23:00"]\n', ""),
            "bad time": MECHANICS.replace('["11:00", "23:00"]', '["25:00"]'),
            "duplicate cutoff": MECHANICS.replace('["11:00", "23:00"]', '["11:00", "11:00"]'),
            "bad timezone": MECHANICS.replace("timezone: Europe/Paris", "timezone: Mars/Olympus"),
            "zero build": MECHANICS.replace("max_in_build: 2", "max_in_build: 0"),
            "text build": MECHANICS.replace("max_in_build: 2", 'max_in_build: "2"'),
            "bad ci": MECHANICS.replace("packet: full", "packet: parfois"),
            "bad environment red": MECHANICS.replace("environment_red: does_not_block",
                                                     "environment_red: parfois"),
            "bad faulty lot": MECHANICS.replace("faulty_lot: removed_without_new_review",
                                                "faulty_lot: parfois"),
            "missing merge": MECHANICS.replace("""merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
""", ""),
            "bad post merge red": MECHANICS.replace("post_merge_red: forward_or_revert",
                                                    "post_merge_red: parfois"),
            "text merge boolean": MECHANICS.replace("on_green_packet: true",
                                                    'on_green_packet: "true"'),
            "text boolean": MECHANICS.replace("exclude_author_family: true",
                                              'exclude_author_family: "true"'),
            "empty families": MECHANICS.replace("families: [claude, codex, deepseek]",
                                                "families: []"),
            "project unknown key": MECHANICS.replace("projects: {}", """projects:
  utc:
    cutoffs: ["06:00"]
    surprise: 1
"""),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(mechanics.MechanicsError):
                    mechanics.compile_document(text)

    def test_unknown_project_and_inexact_commit_are_refused(self):
        repo = self.repo()
        head = repo.git("rev-parse", "HEAD")
        with self.assertRaises(mechanics.MechanicsError):
            mechanics.load(repo.root, head, "nexlink")
        with self.assertRaises(mechanics.MechanicsError):
            mechanics.load(repo.root, "main", "ameesh")
        repo.git("tag", "v1")
        with self.assertRaises(mechanics.MechanicsError):
            mechanics.load(repo.root, "v1", "ameesh")
        with self.assertRaises(mechanics.MechanicsError):
            mechanics.load(repo.root, head, "")
        with self.assertRaises(mechanics.MechanicsError):
            mechanics.load(os.path.join(self.tmp.name, "absent"), head, "ameesh")

    def test_unknown_review_schema_or_count_is_refused(self):
        bad_schema = FEDERATION.replace("ameesh-train-reviews-policy/1", "autre/9")
        repo = self.repo(federation_text=bad_schema)
        with self.assertRaises(mechanics.MechanicsError):
            mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "ameesh")
        bad_count = FEDERATION.replace("reviews: 2", "reviews: 3")
        repo = self.repo(federation_text=bad_count)
        with self.assertRaises(mechanics.MechanicsError):
            mechanics.load(repo.root, repo.git("rev-parse", "HEAD"), "ameesh")


class TestSyncReport(MechanicsTest):
    def _canon(self, commit: str, repo: str, mode: str = "git", prefix: str = ""):
        return SimpleNamespace(sources=[SimpleNamespace(mode=mode, commit=commit,
                                                        repo=repo, prefix=prefix)])

    def test_compiled_configuration_is_reported(self):
        repo = self.repo()
        head = repo.git("rev-parse", "HEAD")
        done = canon_sync.sync_mechanics(self._canon(head, repo.root))
        self.assertEqual(done.status, "ok")
        self.assertEqual(done.to_dict()["cutoffs"], ["11:00", "23:00"])
        self.assertEqual(done.to_dict()["max_in_build"], 2)
        self.assertEqual(done.to_dict()["reviews"]["families"],
                         ["claude", "codex", "deepseek"])

    def test_absent_or_invalid_document_is_a_constat(self):
        absent_repo = self.repo(mechanics_text=None)
        absent = canon_sync.sync_mechanics(
            self._canon(absent_repo.git("rev-parse", "HEAD"), absent_repo.root))
        self.assertEqual(absent.status, "absent")
        self.assertIn("mecanique.yaml", absent.to_dict()["error"])
        # Sans commit exact (canon local) : rien n'est compilé, rien ne ferme.
        untrusted = canon_sync.sync_mechanics(self._canon("", self.tmp.name, mode="file"))
        self.assertEqual(untrusted.status, "untrusted")
        bad = self.repo(mechanics_text="schema: autre/1\n")
        done = canon_sync.sync_mechanics(self._canon(bad.git("rev-parse", "HEAD"), bad.root))
        self.assertEqual(done.status, "invalid")
        self.assertTrue(done.to_dict()["error"])

    def test_canon_under_a_sub_directory_is_refused(self):
        # le document à la racine Git n'est pas celui d'un canon en sous-dossier
        repo = self.repo()
        done = canon_sync.sync_mechanics(
            self._canon(repo.git("rev-parse", "HEAD"), repo.root, prefix="knowledge"))
        self.assertEqual(done.status, "invalid")
        self.assertIn("knowledge", done.to_dict()["error"])


if __name__ == "__main__":
    unittest.main()
