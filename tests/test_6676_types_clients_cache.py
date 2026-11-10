# SPDX-License-Identifier: AGPL-3.0-only
"""#6676 — le contrôle des types clients emprunte le cache partagé par lockfile.

h-06 du 10/11 : #6643 régénérait `docs/api/openapi-v1.json` sans
`ui/src/api/schema.d.ts` ; le clone dédié du composeur n'avait aucun
`ui/node_modules`, le contrôle (#4887) est resté « non concluant » et la CI du
paquet a rougi au job plan.

Témoins :

* OpenAPI changé, schéma inchangé, entrée du cache présente : le worktree
  jetable est lié par `ameesh deps link --no-fill`, le vrai `gen-api --check`
  refuse la composition avant la poussée, le lien est retiré ensuite ;
* cache sans entrée : non concluant **nommé** (cause `modules-absent`),
  jamais bloquant (§3) ;
* `ui/package-lock.json` modifié : non concluant comme avant, sans lien ;
* le manifeste porte la ligne des types clients, sans id relu par
  l'inclusion ; l'événement `packet.client_types` est écrit au mieux.

Aucune base, aucun réseau.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_script(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script(ROOT / "scripts" / "paquet-auto.py", "test_paquet_auto_6676")


class TypesClientsCacheTest(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="paquet-6676-")
        self.addCleanup(tmp.cleanup)
        self.top = pathlib.Path(tmp.name)
        self.repo = self.top / "clone"
        self.repo.mkdir()
        self.cache = self.top / "cache" / "nm" / "k" / "ui" / "node_modules"
        (self.cache / "openapi-typescript").mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write(paquet_auto.CLIENT_SPEC, '{"openapi": "3.1.0", "paths": {}}\n')
        self.write(paquet_auto.CLIENT_SCHEMA, "// schema\n")
        self.write(paquet_auto.CLIENT_GEN, "// générateur factice\n")
        self.write("ui/package-lock.json", '{"lockfileVersion": 3}\n')
        self.base = self.commit("base")
        self.calls: list[list[str]] = []

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def write(self, name: str, content: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def openapi_only(self) -> str:
        self.write(paquet_auto.CLIENT_SPEC, '{"openapi": "3.1.0", "paths": {"/x": {}}}\n')
        return self.commit("openapi seul")

    def fake_run(self, *, status: str = "linked", gen_rc: int = 1):
        def run(cmd, **kwargs):
            self.calls.append(list(cmd))
            if cmd[:3] == ["ameesh", "deps", "link"]:
                ui = pathlib.Path(cmd[-1])
                if status == "linked":
                    (ui / "node_modules").symlink_to(self.cache, target_is_directory=True)
                body = {"items": [{"package": str(ui), "status": status}]}
                return subprocess.CompletedProcess(cmd, 0, json.dumps(body).encode(), b"")
            self.assertTrue((self.repo / "ui" / "node_modules" / "openapi-typescript").is_dir())
            return subprocess.CompletedProcess(
                cmd, gen_rc, b"", b"ui/src/api/schema.d.ts is stale\n" if gen_rc else b"")
        return run

    def check(self, run, units=()):
        outcome: dict = {}
        with mock.patch.object(paquet_auto.shutil, "which", return_value="/usr/bin/node"), \
                mock.patch.object(paquet_auto, "run_command", side_effect=run):
            ok, note = paquet_auto.check_client_types(
                self.repo, self.base, repo=self.repo, units=units, outcome=outcome)
        return ok, note, outcome

    def test_cache_partage_lie_et_schema_perime_refuse_avant_la_poussee(self) -> None:
        self.openapi_only()
        ok, note, outcome = self.check(self.fake_run())
        self.assertFalse(ok)
        self.assertIn("gen-api --check", note)
        self.assertEqual(self.calls[0][:5],
                         ["ameesh", "deps", "link", "--no-fill", "--json"])
        self.assertEqual(outcome, {})
        # le lien du worktree jetable est retiré, l'entrée du cache intacte
        self.assertFalse((self.repo / "ui" / "node_modules").exists())
        self.assertTrue((self.cache / "openapi-typescript").is_dir())

    def test_cache_partage_lie_et_schema_a_jour(self) -> None:
        self.openapi_only()
        ok, note, outcome = self.check(self.fake_run(gen_rc=0))
        self.assertTrue(ok)
        self.assertIn("à jour", note)
        self.assertFalse(outcome)

    def test_cache_sans_entree_est_un_non_concluant_nomme(self) -> None:
        unit = self.openapi_only()
        ok, note, outcome = self.check(self.fake_run(status="not-cached"),
                                       units=[paquet_auto.Unit(6643, self.base, unit)])
        self.assertTrue(ok)
        self.assertIn("non concluant", note)
        self.assertIn("not-cached", note)
        self.assertIn("#6643", note)
        self.assertEqual(outcome["cause"], "modules-absent")
        self.assertIn("clone sans ui/node_modules", outcome["detail"])
        self.assertEqual(len(self.calls), 1)       # aucun node lancé

    def test_deps_link_en_panne_reste_non_concluant(self) -> None:
        self.openapi_only()

        def run(cmd, **kwargs):
            raise paquet_auto.PaquetAutoError("commande introuvable : ameesh")
        ok, note, outcome = self.check(run)
        self.assertTrue(ok)
        self.assertEqual(outcome["cause"], "modules-absent")
        self.assertIn("introuvable", outcome["detail"])

    def test_lockfile_ui_modifie_reste_non_concluant_sans_lien(self) -> None:
        self.write("ui/package-lock.json", '{"lockfileVersion": 3, "x": 1}\n')
        self.openapi_only()
        ok, note, outcome = self.check(self.fake_run())
        self.assertTrue(ok)
        self.assertIn("dépendances ui modifiées", note)
        self.assertEqual(outcome["cause"], "deps-changed")
        self.assertEqual(self.calls, [])

    def test_node_absent_est_compte(self) -> None:
        self.openapi_only()
        outcome: dict = {}
        with mock.patch.object(paquet_auto.shutil, "which", return_value=None), \
                mock.patch.object(paquet_auto, "run_command", side_effect=self.fake_run()):
            ok, _note = paquet_auto.check_client_types(
                self.repo, self.base, repo=self.repo, outcome=outcome)
        self.assertTrue(ok)
        self.assertEqual(outcome["cause"], "node-absent")
        self.assertEqual(self.calls, [])
        self.assertIn(outcome["cause"], paquet_auto.CLIENT_MISSING_CAUSES)


class ManifesteEtEvenementTest(unittest.TestCase):

    def test_manifeste_porte_la_ligne_sans_id_relu(self) -> None:
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "1" * 40)], discarded=[], head="e" * 40)
        text = paquet_auto.build_manifest(
            name="h-test", target="main", base_sha="b" * 40, carrier=9,
            composition=composition, table="## Composition\n", tests=None,
            freeze_seconds=1.0,
            client_types="types clients : non concluant (x) ; indice : unité(s) #6643")
        line = [l for l in text.splitlines() if l.startswith("- Types clients")]
        self.assertEqual(len(line), 1)
        self.assertIn("non concluant", line[0])
        self.assertNotIn("#", line[0])
        self.assertIn("lot 6643", line[0])

    def test_evenement_ecrit_au_mieux(self) -> None:
        seen = []

        def run(cmd, **kwargs):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, b"42\n", b"")
        with mock.patch.object(paquet_auto, "run_command", side_effect=run), \
                mock.patch.object(paquet_auto, "project_python", return_value="python3"), \
                mock.patch.object(paquet_auto, "python_env", return_value={}):
            line = paquet_auto.record_client_types_event(
                ROOT, name="h-test", project="ameesh",
                outcome={"cause": "modules-absent", "detail": "cache : not-cached"})
        self.assertIn("#42", line)
        cmd = seen[0]
        self.assertEqual(cmd[3], paquet_auto.CLIENT_TYPES_EVENT)
        self.assertEqual(cmd[4], paquet_auto.package_branch("h-test"))
        self.assertEqual(json.loads(cmd[5])["cause"], "modules-absent")
        self.assertEqual(cmd[6], "ameesh")

        def broken(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, b"", b"base absente\n")
        with mock.patch.object(paquet_auto, "run_command", side_effect=broken), \
                mock.patch.object(paquet_auto, "project_python", return_value="python3"), \
                mock.patch.object(paquet_auto, "python_env", return_value={}):
            line = paquet_auto.record_client_types_event(
                ROOT, name="h-test", project="", outcome={"cause": "node-absent"})
        self.assertIn("non écrit", line)


if __name__ == "__main__":
    unittest.main()
