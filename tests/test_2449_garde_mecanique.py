# SPDX-License-Identifier: AGPL-3.0-only
"""#2449 : garde mécanique — aucun paramètre du canon codé en dur.

Le canon §7 veut que chaque règle mécanique vienne de
`processus/mecanique.yaml`, compilé par `ameesh.mechanics` ; ce témoin vérifie
la garde `scripts/garde-mecanique.py` :

* chaque clé du schéma `ameesh-mechanics` a un détecteur, et la liste de la
  garde reste celle du lecteur (`src/ameesh/mechanics.py`) ;
* les motifs littéraux connus (coupures, cadence des paquets, plafond de lots,
  politique CI, familles et indépendance, fusion, prévalidation) sont refusés ;
* un code qui lit le canon, un littéral étranger ou un commentaire ne mordent
  pas ; les exemptions exigent un nom, une date ISO et une raison ;
* le dépôt livré est propre et toutes ses exemptions sont utilisées ;
* `ui.yml` ne part plus sur une PR de lot (canon §3/§11 bis) et son dispatch
  manuel est borné à une tête admise.

Aucune base : ce module lit des fichiers et des sources construites.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest

from ameesh import canon, mechanics

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "garde_mecanique", REPO / "scripts" / "garde-mecanique.py")
garde = importlib.util.module_from_spec(_spec)
# dataclasses résout le module par son nom pendant le décorateur
sys.modules[_spec.name] = garde
_spec.loader.exec_module(garde)

EXAMPLE = "src/ameesh/example.py"


def reader_keys() -> set[str]:
    """Les clés du schéma, dérivées des constantes du lecteur strict."""
    keys = {"schema", "timezone", "cutoffs", "projects"}
    for block, names in (
            ("packets", mechanics._PACKETS_KEYS),
            ("build", mechanics._BUILD_KEYS),
            ("ci", mechanics._CI_KEYS),
            ("reviews", mechanics._REVIEWS_KEYS),
            ("merge", mechanics._MERGE_KEYS),
            ("prevalidation", mechanics._PREVALIDATION_KEYS),
            ("backlog", mechanics._BACKLOG_KEYS),
            ("staging", mechanics._STAGING_KEYS),
            ("accounts", mechanics._ACCOUNTS_KEYS),
            ("paquet", mechanics._PAQUET_KEYS),
            ("packet_tests", mechanics._PACKET_TESTS_KEYS),
            ("file", mechanics._FILE_KEYS),
            ("session", mechanics._SESSION_KEYS),
            ("auto_take", mechanics._AUTO_TAKE_KEYS),
            # #3022 : `production.branch` est un bloc **par projet**, pas un
            # bloc du document global ; la garde doit le voir comme les autres.
            ("production", mechanics._PRODUCTION_KEYS)):
        keys |= {"%s.%s" % (block, name) for name in names}
    return keys


def findings(source: str) -> list[str]:
    return [item.parameter for item in garde.inspect_source(EXAMPLE, source)]


class SchemaCoverageTest(unittest.TestCase):
    def test_the_guard_keys_are_the_reader_keys(self):
        self.assertEqual(set(garde.SCHEMA_KEYS), reader_keys())
        self.assertEqual(len(garde.SCHEMA_KEYS), len(set(garde.SCHEMA_KEYS)))
        # le lecteur ne peut pas ajouter un bloc sans que la garde le voie
        self.assertEqual(set(mechanics._DOCUMENT_KEYS),
                         {"schema", "timezone", "cutoffs", "packets", "build",
                          "ci", "reviews", "merge", "prevalidation", "backlog",
                          "staging", "accounts", "paquet", "packet_tests",
                          "file", "session", "auto_take", "projects"})
        # #3022 : les surcharges par projet peuvent porter un bloc qui n'est
        # pas un bloc du document global (`production`) ; il reste couvert par
        # `production.branch` ci-dessus, jamais ajouté au schéma du document.
        self.assertLessEqual(set(mechanics._OVERRIDE_KEYS),
                             set(mechanics._DOCUMENT_KEYS) | {"production"})

    def test_every_key_has_a_working_detector(self):
        self.assertEqual(set(POSITIVE), set(garde.SCHEMA_KEYS))
        for key, source in POSITIVE.items():
            with self.subTest(key=key):
                self.assertIn(key, findings(source),
                              "%s n'a pas de détecteur pour %r" % (key, source))


#: une source positive minimale par clé du schéma. Le témoin ci-dessus vérifie
#: qu'elle produit bien un constat pour cette clé : aucune clé ne peut être
#: déclarée couverte sans que sa règle existe.
POSITIVE = {
    "auto_take.excluded_families": 'AUTO_TAKE_EXCLUDED_FAMILIES = ["deepseek"]\n',
    "auto_take.expire": 'AUTO_TAKE_EXPIRE = "2026-11-01"\n',
    "schema": 'MECHANICS_SCHEMA = "ameesh-mechanics/2"\n',
    "timezone": 'def cutoff(now):\n    return ZoneInfo("Europe/Paris")\n',
    "cutoffs": 'def cutoff(local):\n    return local.hour >= 23\n',
    "packets.cutoffs":
        'PACKETS = ["00:00", "01:00", "02:00", "03:00", "04:00", "05:00"]\n',
    "build.max_in_build": "BUILD_LIMIT = 2\n",
    "ci.packet": 'ci_packet = "full"\n',
    "ci.incident_label": 'incident_label = "ci-full"\n',
    "ci.environment_red": 'environment_red = "does_not_block"\n',
    "ci.faulty_lot": 'faulty_lot = "removed_without_new_review"\n',
    "reviews.families": 'FAMILIES = ("claude", "codex", "deepseek")\n',
    "reviews.exclude_author_family":
        "def f(*, exclude_author_family=True):\n    pass\n",
    "reviews.distinct_families":
        "def f(*, distinct_families=True):\n    pass\n",
    "reviews.light_reviews": "LIGHT_REVIEWS = 1\n",
    "merge.named_fire": "def f(*, named_fire=False):\n    pass\n",
    "merge.repeat_freeze": "def f(*, repeat_freeze=False):\n    pass\n",
    "merge.on_green_packet": "def f(*, on_green_packet=True):\n    pass\n",
    "merge.post_merge_red": 'post_merge_red = "forward_or_revert"\n',
    "prevalidation.classes": '# prevalidation\nclasses = ("P0", "P1")\n',
    "prevalidation.contracts":
        '# prevalidation\ncontracts = ("tests/parts/toujours.txt",)\n',
    "prevalidation.hosts":
        '# prevalidation\nhosts = ("ameesh-agents-1",)\n',
    "prevalidation.parts_script":
        '# prevalidation\nparts_script = "scripts/test-parts.py"\n',
    "prevalidation.timeout_seconds": "# prevalidation\ntimeout_seconds = 7200\n",
    "backlog.lot_min_value": "BACKLOG_LOT_MIN_VALUE = 30\n",
    "staging.waiting_lots_min": "WAITING_LOTS_MIN = 3\n",
    "staging.before_cutoff_seconds": "BEFORE_CUTOFF_SECONDS = 1200\n",
    "staging.before_packet_seconds": "BEFORE_PACKET_SECONDS = 600\n",
    "accounts.pace_multipliers": 'PACE_MULTIPLIERS = {"codex": {"primaire": 2}}\n',
    "accounts.underused_loss": "UNDERUSED_LOSS = 0.5\n",
    "paquet.attente_mediane_s": "ATTENTE_MEDIANE_S = 3600\n",
    "paquet.max_paquets": "MAX_PAQUETS = 2\n",
    "packet_tests.unit_timeout_seconds": "UNIT_TIMEOUT_SECONDS = 900\n",
    "packet_tests.parallel": "UNIT_TESTS_PARALLEL = 4\n",
    "packet_tests.total_timeout_seconds": "TOTAL_TIMEOUT_SECONDS = 1800\n",
    "file.repos_part": "REPOS_PART = 0.2\n",
    "file.repos_s": "REPOS_S = 1800\n",
    "file.wake_mail_s": "WAKE_MAIL_S = 1800\n",
    "session.max_tokens": "SESSION_MAX_TOKENS = 150000\n",
    "session.max_tokens_by_harness":
        'SESSION_MAX_TOKENS_BY_HARNESS = {"codex": 2000000}\n',
    "projects": 'PROJECT_OVERRIDES = {"ameesh": {"build": {"max_in_build": 3}}}\n',
    "production.branch": 'production_branch = "main"\n',
}


class RulesTest(unittest.TestCase):
    def test_a_mapping_literal_is_a_policy_copy(self):
        self.assertIn("merge.named_fire", findings(
            'MERGE = {"named_fire": False, "repeat_freeze": False}\n'))
        self.assertIn("prevalidation.timeout_seconds", findings(
            '# prevalidation\nBLOCK = {"timeout_seconds": 7200}\n'))
        self.assertIn("ci.packet", findings(
            '# ci packet\nCI = {"packet": "full"}\n'))
        self.assertIn("schema", findings(
            'CONFIG = {"schema": "ameesh-mechanics/1"}\n'))
        self.assertIn("staging.waiting_lots_min", findings(
            'STAGING = {"waiting_lots_min": 3}\n'))
        # #4448 : une table de seuils de rotation par harnais est une règle du
        # canon recopiée, pas une constante d'implémentation
        self.assertIn("session.max_tokens_by_harness", findings(
            'SESSION_MAX_TOKENS_BY_HARNESS = {"codex": 2_000_000}\n'))
        self.assertIn("session.max_tokens", findings(
            "def f(*, session_max_tokens=150_000):\n    pass\n"))

    def test_unrelated_literals_do_not_mordent(self):
        for source in (
                'FAMILIES = ("python", "node")\n',
                'timezone = "UTC"\n',
                "packet = None\n",
                "max_build = False\n",
                'hosts = ("web",)\n',
                '"""11:00 Europe/Paris"""\n',
                "# 11:00, Europe/Paris\n",
                "def f(hour=9):\n    pass\n",
                'SCHEMA = "ameesh-train/1"\n',
                'def f(*, families=("python",)):\n    pass\n',
                'WIDGET_FAMILIES = ("python", "node")\n',
                "MAX_BUILD = False\n",
                'DEFAULT_TZ = "UTC"  # cutoff\n',
                "def f():\n    return range(24)\n",
                'def f():\n    return mechanics.load(repo, commit, "ameesh")\n',
        ):
            with self.subTest(source=source):
                self.assertEqual(findings(source), [])

    def test_call_keywords_are_bindings(self):
        self.assertIn("build.max_in_build", findings("f(max_build=2)\n"))
        self.assertIn("reviews.families", findings(
            'f(families=("claude",))\n'))
        self.assertIn("prevalidation.timeout_seconds", findings(
            "# prevalidation\nf(timeout_seconds=7200)\n"))
        self.assertIn("cutoffs", findings(
            'cutoffs = ["11:00", "23:00"]\n'))

    def test_a_future_runner_hardcoded_cutoff_is_refused(self):
        # le point de relecture #2448 : 11:00/23:00 peuvent être recopiés
        # comme une paire d'heures, une constante `CUTOFF_*` ou un cron.
        self.assertIn("cutoffs", findings("CUTOFF_HOURS = (11, 23)\n"))
        self.assertIn("cutoffs", findings("CUTOFF_HOUR = 11\n"))
        self.assertIn("cutoffs", findings("HOURS = [11, 23]  # train\n"))
        self.assertIn("cutoffs", findings(
            'SCHEDULE = "0 11,23 * * *"  # train\n'))
        self.assertIn("packets.cutoffs", findings(
            'PACKET_CUTOFFS = ["04:00", "05:00"]\n'))
        # un runner de release écrit en français (#2448) : ni `cutoff`, ni
        # `packet`, ni `train`, et pourtant la règle recopiée
        release = ('"""Runner de release : coupures de 11:00 et 23:00."""\n'
                   "import datetime\n")
        for body in ("RELEASE_HOURS = (11, 23)\n",
                     "def due(now):\n    return now.hour in (11, 23)\n",
                     'CRON = "0 11,23 * * *"\n',
                     "AT = (datetime.time(11, 0), datetime.time(23, 0))\n",
                     'AT = ("11:00:00", "23:00:00")\n'):
            self.assertIn("cutoffs", findings(release + body), body)
        self.assertIn("timezone", findings(
            release + 'PARIS = ZoneInfo("Europe/Paris")\n'))
        # une paire d'heures étrangère ou une marge sans nom de coupure
        # ne mord pas
        self.assertEqual(findings("HOURS = [9, 17]\n"), [])
        self.assertEqual(findings("MARGIN = 5\n"), [])

    def test_upper_case_constants_are_caught_too(self):
        self.assertIn("reviews.families", findings(
            'ALLOWED_FAMILIES = ("claude", "codex")\n'))
        self.assertIn("build.max_in_build", findings("LOT_BUILD_LIMIT = 2\n"))
        self.assertIn("ci.incident_label", findings(
            'INCIDENT_LABEL = "ci-full"\n'))
        self.assertIn("schema", findings(
            'MECHANICS_SCHEMA = "ameesh-mechanics/1"\n'))
        self.assertIn("timezone", findings(
            'DEFAULT_TZ = "Europe/Paris"  # cutoff\n'))
        self.assertIn("build.max_in_build", findings("MAX_BUILD = 2\n"))

    def test_a_docstring_is_never_code(self):
        self.assertEqual(findings('def f():\n    """11:00: la coupure."""\n'), [])

    def test_an_unreadable_source_is_refused(self):
        found = garde.inspect_source(EXAMPLE, "def broken(:\n")
        self.assertEqual([(item.parameter, item.literal) for item in found],
                         [("source", "syntax-error")])


class ExemptionTest(unittest.TestCase):
    def exemption(self, **overrides):
        fields = dict(path=EXAMPLE, parameter="build.max_in_build",
                      literal="2", name="lot #2449 (test)",
                      date="2026-10-18", reason="littéral historique du témoin")
        fields.update(overrides)
        return garde.Exemption(**fields)

    def test_a_named_dated_exemption_covers_its_finding(self):
        found = garde.inspect_source(EXAMPLE, "BUILD_LIMIT = 2\n")
        remaining, used, unused = garde.apply_exemptions(
            found, (self.exemption(),))
        self.assertEqual((remaining, len(used), unused), ([], 1, ()))

    def test_wildcard_covers_every_occurrence_of_the_key_in_the_file(self):
        found = garde.inspect_source(
            EXAMPLE, "def f(local):\n    return local.hour >= 23 or local.hour >= 11\n")
        self.assertTrue(len(found) >= 2)
        remaining, used, unused = garde.apply_exemptions(
            found, (self.exemption(parameter="cutoffs", literal="*"),))
        self.assertEqual((remaining, len(used), unused), ([], 1, ()))

    def test_another_path_key_or_literal_never_covers(self):
        found = garde.inspect_source(EXAMPLE, "BUILD_LIMIT = 2\n")
        for exemption in (
                self.exemption(path="src/ameesh/other.py"),
                self.exemption(parameter="reviews.families"),
                self.exemption(literal="3"),
        ):
            with self.subTest(exemption=exemption):
                remaining, used, unused = garde.apply_exemptions(
                    found, (exemption,))
                self.assertEqual((len(remaining), used, len(unused)),
                                 (1, (), 1))

    def test_an_exemption_needs_a_name_a_date_and_a_reason(self):
        for change in (dict(name=""), dict(name=" ? "),
                       dict(date=""), dict(date="18/10/2026"),
                       dict(date="2026-02-30"), dict(reason=""),
                       dict(parameter="pas.une.cle"),
                       dict(path="/etc/passwd"), dict(path="../tests/x.py")):
            with self.subTest(change=change):
                with self.assertRaises(garde.GuardError):
                    self.exemption(**change)

    def test_an_unused_exemption_is_reported(self):
        found = garde.inspect_source(EXAMPLE, "BUILD_LIMIT = 2\n")
        remaining, used, unused = garde.apply_exemptions(
            found, (self.exemption(), self.exemption(parameter="cutoffs")))
        self.assertEqual((len(remaining), len(used), len(unused)), (0, 1, 1))
        self.assertEqual(unused[0].parameter, "cutoffs")


class RepositoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scan = garde.scan_repository(REPO)

    def test_the_repository_has_no_unexempted_literal(self):
        self.assertEqual(self.scan.findings, (),
                         [item.render() for item in self.scan.findings])
        self.assertTrue(self.scan.ok)

    def test_every_exemption_is_used(self):
        self.assertEqual(self.scan.unused, (),
                         [item.as_dict() for item in self.scan.unused])
        self.assertEqual(set(self.scan.used), set(garde.EXEMPTIONS))

    def test_the_guard_never_scans_the_reader_tests_or_itself(self):
        files = {rel for rel, _ in garde.python_files(REPO)}
        for excluded in ("src/ameesh/mechanics.py",
                         "scripts/garde-mecanique.py",
                         "tests/test_2449_garde_mecanique.py"):
            self.assertNotIn(excluded, files)
        self.assertFalse(any(rel.startswith("tests/") for rel in files))
        self.assertFalse(any(rel.startswith("reviews/") for rel in files))

    def test_the_cli_refuses_a_hardcode(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        (root / "src" / "ameesh").mkdir(parents=True)
        (root / "src" / "ameesh" / "x.py").write_text(
            "BUILD_LIMIT = 2\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = garde.main(["--root", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("refus", out.getvalue())
        self.assertIn("src/ameesh/x.py:1", out.getvalue())
        self.assertIn("build.max_in_build", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = garde.main(["--root", str(root), "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["findings"][0]["parameter"], "build.max_in_build")

    def test_the_cli_accepts_a_canon_reader(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        (root / "src" / "ameesh").mkdir(parents=True)
        (root / "src" / "ameesh" / "x.py").write_text(
            "def f(rules):\n"
            "    return rules.max_in_build\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = garde.main(["--root", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("aucun littéral", out.getvalue())


class UiWorkflowTest(unittest.TestCase):
    def setUp(self):
        text = (REPO / ".github" / "workflows" / "ui.yml").read_text(
            encoding="utf-8")
        self.doc = canon.load_yaml(text, use_pyyaml=False)
        self.triggers = self.doc["on"]

    def test_no_lot_pull_request_trigger(self):
        self.assertNotIn("pull_request", self.triggers)

    def test_push_covers_the_package_release_and_main(self):
        push = self.triggers["push"]
        for branch in ("main", "release/**", "integration/**"):
            self.assertIn(branch, push["branches"])
        for path in ("ui/**", "src/ameesh/ui/**", "docs/api/openapi-v1.json",
                     ".github/workflows/ui.yml"):
            self.assertIn(path, push["paths"])

    def test_a_manual_dispatch_is_bounded_to_an_admitted_head(self):
        self.assertIn("workflow_dispatch", self.triggers)
        steps = self.doc["jobs"]["ui"]["steps"]
        guards = [step for step in steps
                  if step.get("if") == "github.event_name == 'workflow_dispatch'"]
        self.assertEqual(len(guards), 1, "le dispatch doit être borné à une tête admise")
        run = " ".join(str(guards[0].get("run", "")).split())
        for ref in ("refs/heads/main", "refs/heads/release/*",
                    "refs/heads/integration/*", "refs/heads/validation/*"):
            self.assertIn(ref, run)
        self.assertIn("exit 1", run)


if __name__ == "__main__":
    unittest.main()
