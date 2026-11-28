# SPDX-License-Identifier: AGPL-3.0-only
"""#8338 (manaty/ameesh#6578) : lecture publique du barème de prix.

`ameesh prices --json` (`ameesh-prices/1`) rend le barème effectif : par
modèle, entrée / entrée en cache / sortie et origine (défaut ou fichier local),
par harnais le prix d'un modèle inconnu (majorant de la famille, sinon repli
prudent marqué), et la source lue (état, empreinte, date). Un fichier illisible
est signalé, jamais fatal ; aucune valeur hors triplet n'est recopiée.
"""
from tests import support
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import cost

#: valeur factice : ne doit jamais apparaître dans un document ou une erreur
SECRET = "sk-fake-8338-0123456789abcdef"


class PriceScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a8338-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = self.tmp / "prices.json"

    def by_model(self, document):
        return {row["model"]: row for row in document["models"]}

    def by_harness(self, document):
        return {row["harness"]: row for row in document["harnesses"]}

    def test_defaults_without_file(self):
        document = cost.price_schedule(str(self.file), now=1.0)
        self.assertEqual(document["schema"], "ameesh-prices/1")
        self.assertEqual(document["components"], ["input", "cached_input", "output"])
        self.assertEqual(document["observed_ts"], 1.0)
        source = document["source"]
        self.assertEqual((source["kind"], source["status"], source["path_origin"]),
                         ("defaults", "absent", "argument"))
        self.assertIsNone(source["sha256"])
        models = self.by_model(document)
        self.assertEqual(set(models), set(cost.DEFAULT_PRICES))
        opus = models["claude-opus-4-6"]
        self.assertEqual([opus["input"], opus["cached_input"], opus["output"]],
                         cost.DEFAULT_PRICES["claude-opus-4-6"])
        self.assertEqual((opus["origin"], opus["harness"]), ("default", "claude"))
        self.assertEqual(models["default-codex"]["harness"], "codex")
        self.assertEqual(models["claude-haiku-5-5"]["prompt_tier"]["max_prompt_tokens"],
                         100_000)
        self.assertEqual(models["deepseek-v4-flash"]["off_peak"]["price"]["output"], 0.60)
        prices = cost.load_prices(None)
        for harness, row in self.by_harness(document).items():
            ceiling = cost.max_price_of(prices, harness)
            self.assertEqual([row["max_price"][k] for k in document["components"]],
                             ceiling)
            self.assertEqual(row["unknown_model_price"], row["max_price"])
            self.assertEqual((row["unknown_model_basis"], row["fallback"]),
                             ("family_max", False))
        self.assertEqual(self.by_harness(document)["codex"]["default_entry"],
                         "default-codex")

    def test_local_file_overrides_and_is_dated(self):
        raw = json.dumps({"claude-opus-4-6": [6, 0.6, 30],
                          "claude-maison": [20, 2, 100]}).encode()
        self.file.write_bytes(raw)
        document = cost.price_schedule(str(self.file))
        source = document["source"]
        self.assertEqual((source["kind"], source["status"], source["entries"]),
                         ("file", "loaded", 2))
        self.assertEqual(source["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIsInstance(source["mtime"], float)
        models = self.by_model(document)
        self.assertEqual((models["claude-opus-4-6"]["origin"],
                          models["claude-opus-4-6"]["overrides_default"],
                          models["claude-opus-4-6"]["input"]), ("file", True, 6.0))
        self.assertEqual((models["claude-maison"]["origin"],
                          models["claude-maison"]["overrides_default"]), ("file", False))
        # le majorant suit le fichier, comme celui des réservations
        claude = self.by_harness(document)["claude"]
        self.assertEqual(claude["max_price"],
                         {"input": 20.0, "cached_input": 2.0, "output": 100.0})
        self.assertEqual(
            [claude["max_price"][k] for k in document["components"]],
            cost.max_price_of(cost.load_prices(str(self.file)), "claude"))

    def test_unreadable_or_malformed_file_is_signalled_not_fatal(self):
        for content, status in ((b"{not json", "malformed"), (b"[1, 2, 3]", "malformed"),
                                (b"\xff\xfe", "malformed")):
            with self.subTest(status=status, content=content):
                self.file.write_bytes(content)
                document = cost.price_schedule(str(self.file))
                self.assertEqual((document["source"]["status"],
                                  document["source"]["kind"]), (status, "defaults"))
                self.assertEqual(len(document["models"]), len(cost.DEFAULT_PRICES))
        self.file.unlink()
        self.file.mkdir()
        document = cost.price_schedule(str(self.file))
        self.assertEqual(document["source"]["status"], "unreadable")
        self.assertEqual(cost.load_prices(str(self.file)), cost.DEFAULT_PRICES)

    def test_unknown_family_never_prices_at_zero(self):
        document = cost.price_schedule(str(self.file))
        self.assertTrue(all(row["unknown_model_price"] for row in document["harnesses"]))
        # famille sans aucun tarif : repli prudent sur tout le barème, marqué
        prices = {"deepseek-x": [1, 0.1, 2]}
        with mock.patch.dict(cost.DEFAULT_PRICES, prices, clear=True):
            document = cost.price_schedule(str(self.file))
        claude = self.by_harness(document)["claude"]
        self.assertIsNone(claude["max_price"])
        self.assertEqual((claude["unknown_model_basis"], claude["fallback"]),
                         ("schedule_max", True))
        self.assertEqual(claude["unknown_model_price"],
                         {"input": 1.0, "cached_input": 0.1, "output": 2.0})

    def test_invalid_entries_are_counted_never_copied(self):
        self.file.write_text(json.dumps({
            "api_key": SECRET, "claude-x": [1, -1, 3], "gpt-y": [1, 2],
            "claude-ok": [1, 0.1, 5]}))
        document = cost.price_schedule(str(self.file))
        self.assertEqual((document["source"]["entries"],
                          document["source"]["rejected_entries"]), (1, 3))
        text = json.dumps(document)
        for forbidden in (SECRET, "api_key", "claude-x", "gpt-y"):
            self.assertNotIn(forbidden, text)

    def test_cli_json_needs_no_database_and_carries_no_secret(self):
        self.file.write_text(json.dumps({"api_key": SECRET, "claude-ok": [1, 0.1, 5]}))
        env = support.child_env(
            AMEESH_PRICES=str(self.file),
            AMEESH_DSN="postgresql://nobody:%s@127.0.0.1:9/none" % SECRET)
        for args in (["--json"], []):
            with self.subTest(args):
                done = subprocess.run(
                    [sys.executable, "-m", "ameesh", "prices", *args],
                    env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn(SECRET, done.stdout + done.stderr)
        document = json.loads(subprocess.run(
            [sys.executable, "-m", "ameesh", "prices", "--json"],
            env=env, capture_output=True, text=True, timeout=120).stdout)
        self.assertEqual(document["source"]["path_origin"], "AMEESH_PRICES")
        self.assertEqual(document["source"]["path"], str(self.file))
        self.assertEqual(self.by_model(document)["claude-ok"]["origin"], "file")


if __name__ == "__main__":
    unittest.main()
