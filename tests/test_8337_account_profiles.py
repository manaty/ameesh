# SPDX-License-Identifier: AGPL-3.0-only
"""#8337 (manaty/ameesh#6577) : lecture publique des profils de comptes.

`ameesh accounts profiles --json` (`ameesh-account-profiles/1`) liste tous
les profils déclarés : nom, type, harnais, noms de variables, réserve, compte
de facturation — jamais une valeur de clé, ni le contenu ou le chemin d'un
fichier. Un profil illisible est signalé par une raison fermée sans faire
tomber la liste.
"""
from tests import support
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import accounts

#: valeurs factices : aucune ne doit apparaître dans un document ou une erreur
SECRET = "sk-fake-8337-0123456789abcdef"
FILE_SECRET = "sk-file-8337-fedcba9876543210"


class PublicProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a8337-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.key_file = self.tmp / "deepseek-b.key"
        self.key_file.write_text(FILE_SECRET)
        self.key_file.chmod(0o600)
        self.home = self.tmp / "claude-2"
        self.home.mkdir(mode=0o700)
        self.raw = {
            "deepseek": [
                {"name": "cle-b", "type": "api_key_env", "key_file": str(self.key_file)},
                {"name": "cle-a", "type": "api_key_env", "key_env": "DEEPSEEK_API_KEY_A",
                 "reserve_usd": 3, "billing_account": "deepseek-manaty"},
            ],
            "claude": [
                {"name": "secondaire", "type": "config_dir", "path": str(self.home)},
            ],
        }

    def assertNoSecret(self, document):
        text = json.dumps(document)
        for forbidden in (SECRET, FILE_SECRET, str(self.key_file), str(self.home),
                          str(self.tmp)):
            self.assertNotIn(forbidden, text)

    def test_every_declared_profile_is_listed_sorted_and_redacted(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY_A": SECRET}):
            document = accounts.public_profiles(self.raw)
        self.assertEqual(document["schema"], "ameesh-account-profiles/1")
        self.assertTrue(document["valid"])
        self.assertEqual(document["refused"], [])
        self.assertEqual([(r["harness"], r["name"]) for r in document["profiles"]],
                         [("claude", "secondaire"), ("deepseek", "cle-a"),
                          ("deepseek", "cle-b")])
        rows = {r["name"]: r for r in document["profiles"]}
        self.assertEqual(rows["cle-a"], {
            "harness": "deepseek", "name": "cle-a", "kind": "api_key_env",
            "env": "DEEPSEEK_API_KEY", "key_env": "DEEPSEEK_API_KEY_A",
            "key_source": "env", "reserve_usd": 3.0,
            "billing_account": "deepseek-manaty"})
        self.assertEqual((rows["cle-b"]["key_env"], rows["cle-b"]["key_source"]),
                         (None, "file"))
        self.assertEqual(rows["secondaire"], {
            "harness": "claude", "name": "secondaire", "kind": "config_dir",
            "env": None, "key_env": None, "key_source": None,
            "reserve_usd": accounts.DEFAULT_RESERVE_USD, "billing_account": None})
        self.assertNoSecret(document)

    def test_an_unreadable_profile_is_reported_without_breaking_the_list(self):
        self.raw["deepseek"] += [
            # une clé collée à la place du nom de variable
            {"name": "collee", "type": "api_key_env", "key_env": SECRET},
            # erreur de forme dont le message cite le chemin du fichier
            {"name": "relative", "type": "api_key_env", "key_file": "relatif/" + FILE_SECRET},
            {"name": "cle-a", "type": "api_key_env", "key_env": "AUTRE"},
            "pas un objet",
        ]
        self.raw["inconnu"] = [{"name": "x"}]
        document = accounts.public_profiles(self.raw)
        self.assertFalse(document["valid"])
        self.assertEqual([r["name"] for r in document["profiles"]],
                         ["secondaire", "cle-a", "cle-b", "collee"])
        # #8457 : la clé collée est publiée à null, le profil reste listé
        self.assertIsNone(document["profiles"][-1]["key_env"])
        self.assertEqual(
            [(r["harness"], r["index"], r["name"], r["reason"]) for r in document["refused"]],
            [("deepseek", 3, "relative", "invalid_profile"),
             ("deepseek", 4, "cle-a", "duplicate_name"),
             ("deepseek", 5, None, "not_an_object"),
             (None, None, None, "unknown_harness")])
        self.assertLessEqual({r["reason"] for r in document["refused"]},
                             set(accounts.PUBLIC_REFUSALS))
        self.assertNoSecret(document)
        self.assertNotIn("relatif", json.dumps(document))

    def test_a_synthetic_profile_is_never_listed(self):
        document = accounts.public_profiles(
            {"claude": [{"name": "(default)"}, {"name": "primaire"}]})
        self.assertFalse(document["valid"])
        self.assertEqual([r["name"] for r in document["profiles"]], ["primaire"])
        self.assertEqual(document["refused"],
                         [{"harness": "claude", "index": 0, "name": None,
                           "reason": "invalid_name"}])

    def test_shared_directory_is_signalled_for_the_harness(self):
        document = accounts.public_profiles({"claude": [
            {"name": "a", "path": str(self.home)}, {"name": "b", "path": str(self.home)}]})
        self.assertFalse(document["valid"])
        self.assertEqual([r["name"] for r in document["profiles"]], ["a", "b"])
        self.assertEqual(document["refused"], [{"harness": "claude", "index": None,
                                                "name": None,
                                                "reason": "shared_configuration_dir"}])
        self.assertNoSecret(document)

    def test_no_accounts_and_malformed_accounts(self):
        self.assertEqual(accounts.public_profiles(None)["profiles"], [])
        self.assertTrue(accounts.public_profiles({})["valid"])
        document = accounts.public_profiles([SECRET])
        self.assertFalse(document["valid"])
        self.assertEqual(document["refused"], [{"harness": None, "index": None,
                                                "name": None,
                                                "reason": "invalid_accounts"}])
        self.assertNoSecret(document)
        for items in ([], "pas une liste"):
            with self.subTest(items=items):
                document = accounts.public_profiles({"claude": items})
                self.assertFalse(document["valid"])
                self.assertEqual(document["profiles"], [])
                self.assertEqual(document["refused"],
                                 [{"harness": "claude", "index": None, "name": None,
                                   "reason": "invalid_profile_list"}])

    def test_cli_json_needs_no_database_and_carries_no_secret(self):
        self.raw["deepseek"] += [
            {"name": "collee", "type": "api_key_env", "key_env": SECRET},
            {"name": "relative", "type": "api_key_env", "key_file": "relatif/cle"},
        ]
        config = self.tmp / "config.json"
        config.write_text(json.dumps({"accounts": self.raw}))
        env = support.child_env(AMEESH_CONFIG=str(config), DEEPSEEK_API_KEY_A=SECRET,
                                AMEESH_DSN="postgresql://nobody@127.0.0.1:9/none")
        for args in (["--json"], []):
            with self.subTest(args):
                done = subprocess.run(
                    [sys.executable, "-m", "ameesh", "accounts", "profiles", *args],
                    env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr)
                if not args:
                    self.assertIn("valid=false", done.stderr)
                for forbidden in (SECRET, FILE_SECRET, str(self.key_file)):
                    self.assertNotIn(forbidden, done.stdout + done.stderr)
        document = json.loads(subprocess.run(
            [sys.executable, "-m", "ameesh", "accounts", "profiles", "--json"],
            env=env, capture_output=True, text=True, timeout=120).stdout)
        self.assertEqual([r["name"] for r in document["profiles"]],
                         ["secondaire", "cle-a", "cle-b", "collee"])
        self.assertFalse(document["valid"])
        self.assertEqual([r["reason"] for r in document["refused"]], ["invalid_profile"])


class UntrustedNamesAreNeverPublishedTest(unittest.TestCase):
    """#8457 (manaty/ameesh#6666) : témoin de non-publication.

    Une valeur non de confiance dans `env`/`key_env` (une clé collée à la
    place du nom), ou à la place d'un nom de harnais, n'apparaît jamais dans
    le document public, JSON compris ; le profil reste listé, `valid` intact.
    """

    #: formes de clés, dont celles des relectures d'ameesh-cx2
    KEYS = ("a3f9c2e1d4b5a6978877665544332211",
            "DeadBeef0123456789abcdefCAFE",
            "A3F9C2E1D4B5A6978877665544332211",
            "AKIAIOSFODNN7EXAMPLE",
            "ABCDEFGH12345678_IJKLMNOP12345678",
            "ABCDEFGH_IJKLMNOP_QRSTUVWX_YZABCDEF",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEF",
            # préfixe de confiance sans le souligné, ou suffixe trop long
            "DEEPSEEK_API_KEYABCDEFGH12345678",
            "DEEPSEEK_API_KEY_" + "A" * 33,
            "sk-fake-8457-0123456789abcdef")

    def assertNotPublished(self, document, key):
        text = json.dumps(document)
        self.assertNotIn(key, text)
        self.assertNotIn(key.lower(), text.lower())

    def _one(self, item, harness="deepseek"):
        return accounts.public_profiles({harness: [item]})

    def test_untrusted_value_in_env_or_key_env_is_published_as_null(self):
        for key in self.KEYS:
            for field in ("key_env", "env"):
                item = {"name": "collee", "type": "api_key_env", "key_env": "DEEPSEEK_API_KEY"}
                item[field] = key
                with self.subTest(key=key, field=field):
                    document = self._one(item)
                    self.assertTrue(document["valid"])
                    self.assertEqual(document["refused"], [])
                    (row,) = document["profiles"]
                    self.assertEqual(row["name"], "collee")
                    self.assertIsNone(row[field])
                    self.assertEqual(row["key_source"], "env")
                    self.assertNotPublished(document, key)

    def test_attestation_still_requires_the_variable_shape(self):
        for key in ("a3f9c2e1d4b5a6978877665544332211", "sk-fake-8457-0123456789abcdef"):
            with self.subTest(key=key):
                document = self._one({"name": "a", "type": "api_key_env",
                                      "key_env": key, "public_names": True})
                self.assertIsNone(document["profiles"][0]["key_env"])
                self.assertNotPublished(document, key)

    def test_key_pasted_as_harness_is_refused_and_not_copied(self):
        for key in self.KEYS:
            with self.subTest(key=key):
                document = accounts.public_profiles({key: [{"name": "x"}]})
                self.assertEqual(document["refused"], [
                    {"harness": None, "index": None, "name": None,
                     "reason": "unknown_harness"}])
                self.assertNotPublished(document, key)

    def test_trusted_names_stay_published(self):
        cases = [
            ("deepseek", {"key_env": "DEEPSEEK_API_KEY"}),           # (a) défaut
            ("deepseek", {"key_env": "DEEPSEEK_API_KEY_A"}),         # (b) suffixe
            ("deepseek", {"key_env": "OPENAI_API_KEY_CLE_2"}),       # (b) autre défaut
            ("deepseek", {"key_env": "CODEX1_FAKE_SOURCE_A", "public_names": True}),  # (c)
            ("deepseek", {"env": "OPENAI_API_KEY", "key_env": "AUTRE",
                          "public_names": True}),
        ]
        for harness, fields in cases:
            item = {"name": "a", "type": "api_key_env", **fields}
            with self.subTest(fields=fields):
                document = self._one(item, harness)
                self.assertTrue(document["valid"])
                (row,) = document["profiles"]
                for field in ("env", "key_env"):
                    if field in fields:
                        self.assertEqual(row[field], fields[field])
        row = self._one({"name": "a", "type": "api_key_env",
                         "key_env": "CODEX1_FAKE_SOURCE_A"})["profiles"][0]
        self.assertIsNone(row["key_env"])
        self.assertEqual(row["env"], "DEEPSEEK_API_KEY")

    def test_public_names_must_be_a_strict_boolean(self):
        with self.assertRaises(accounts.AccountError):
            accounts.parse({"deepseek": [{"name": "a", "type": "api_key_env",
                                          "key_env": "DEEPSEEK_API_KEY",
                                          "public_names": "yes"}]})
        document = self._one({"name": "a", "type": "api_key_env",
                              "key_env": "DEEPSEEK_API_KEY", "public_names": 1})
        self.assertEqual([r["reason"] for r in document["refused"]], ["invalid_profile"])


if __name__ == "__main__":
    unittest.main()
