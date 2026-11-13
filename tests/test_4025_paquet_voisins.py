# SPDX-License-Identifier: AGPL-3.0-only
"""#4025 — les tests locaux d'une unité jouent aussi ses modules voisins.

Témoins : `scripts/paquet-attribution.py --select` sur un dépôt jetable
(un module qui importe un support de test modifié, une carte d'impact au
grain du bloc, un budget de durée, une carte absente ou antérieure à la
base), puis `check_local_tests` : un voisin rouge désigne l'unité qui seule
l'a sélectionné, et le manifeste nomme chaque voisin avec sa raison sans
citer d'id de lot. Aucune base, aucun réseau.
"""
from __future__ import annotations

import datetime
import gzip
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTO = ROOT / "scripts" / "paquet-auto.py"
HELPER = ROOT / "scripts" / "paquet-attribution.py"


def _load_script(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script(AUTO, "test_paquet_auto_4025")

SOURCE_V1 = '''\
def chaude():
    return 1


def froide():
    return 2
'''
SOURCE_V2 = SOURCE_V1.replace("return 1", "return 10")


class SelectionHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-4025-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("src/pkg/mod.py", SOURCE_V1)
        self.write("tests/__init__.py", "")
        self.write("tests/test_gabarit.py", "class _Base:\n    pass\n")
        self.write("tests/test_importe.py",
                   "from .test_gabarit import _Base\n")
        self.write("tests/test_importe_abs.py",
                   "import tests.test_gabarit\n")
        self.write("tests/test_sans_lien.py", "X = 1\n")
        for name in ("test_chaud_rapide", "test_chaud_lent", "test_froid",
                     "test_import_seul"):
            self.write("tests/%s.py" % name, "X = 1\n")
        # #4814 : les modules `support` sont pesés comme ceux de la carte
        self.write("tests/parts/durees.json", json.dumps(
            {"psycopg": {"test_importe": 2.0, "test_importe_abs": 3.0}}))
        self.base = self.commit("base")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def write(self, name: str, content: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def card(self, commit: str, blocs: dict | None = None) -> None:
        card = {
            "schema": "ameesh-carte/1", "commit": commit,
            "genere": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "blocs": blocs if blocs is not None else {
                "src/pkg/mod.py": {
                    "<module>": {"modules": ["test_chaud_lent", "test_chaud_rapide",
                                             "test_froid", "test_import_seul"]},
                    "chaude": {"modules": ["test_chaud_lent", "test_chaud_rapide"]},
                    "froide": {"modules": ["test_froid"]},
                }},
            "modules": {
                "test_chaud_rapide": {"durations": {"psycopg": 1.0, "psql": 50.0}},
                "test_chaud_lent": {"durations": {"psycopg": 40.0}},
                "test_import_seul": {"durations": {"psycopg": 0.5}},
            },
        }
        path = self.repo / "carte" / "carte.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump(card, stream)

    def select(self, unit: str, budget: float) -> dict:
        request = {"schema": "ameesh-paquet-selection-request/1",
                   "base": self.base, "budget": budget,
                   "units": [{"lot": 7, "unit_sha": unit}]}
        proc = subprocess.run(
            [sys.executable, str(HELPER), "--select", "--repo", str(self.repo),
             "--slug", "banc/banc", "--state-dir", self.tmp.name],
            input=json.dumps(request), capture_output=True, text=True,
            timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def unit(self) -> str:
        self.write("src/pkg/mod.py", SOURCE_V2)
        self.write("tests/test_gabarit.py", "class _Base:\n    X = 2\n")
        return self.commit("unité")

    def test_support_et_blocs_changes_sous_budget(self) -> None:
        self.card(self.base)
        report = self.select(self.unit(), budget=30)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["card"]["base"], "à jour")
        (unit,) = report["units"]
        added = [(row["module"], row["reason"]) for row in unit["added"]]
        # le support importé (relatif, absolu), puis le bloc changé `chaude`,
        # le moins cher d'abord ; `froide` et `<module>` ne changent pas, et
        # le module lent dépasse le budget
        self.assertEqual(added[:3], [
            ("tests.test_importe", "support"),
            ("tests.test_importe_abs", "support"),
            ("tests.test_chaud_rapide", "carte"),
        ])
        modules = [module for module, _reason in added]
        self.assertNotIn("tests.test_froid", modules)
        self.assertNotIn("tests.test_sans_lien", modules)
        self.assertNotIn("tests.test_gabarit", modules)
        self.assertNotIn("tests.test_chaud_lent", modules)
        self.assertEqual(unit["dropped"], 1)
        self.assertEqual(unit["added"][2]["files"], ["src/pkg/mod.py"])

    def test_7195_bloc_rare_mesure_garde_sa_place(self) -> None:
        # #7195 : 150 tests mesurés à 1,2 s couvrent le bloc `chaude` ; le bloc
        # rare `froide` a trois tests mesurés (14,4 ; 45,6 ; 60 s). L'ancien
        # ordre global (le moins cher d'abord) dépensait les 180 s dans
        # `chaude` et laissait tomber le test ciblé à 45,6 s ; le tour par
        # bloc, durées mesurées d'abord, le garde.
        large = ["test_large_%03d" % index for index in range(150)]
        for name in large:
            self.write("tests/%s.py" % name, "X = 1\n")
        cibles = ["test_cible_rapide", "test_cible_moyen", "test_cible_lent"]
        for name in cibles:
            self.write("tests/%s.py" % name, "X = 1\n")
        prices = {name: 1.2 for name in large}
        prices.update({"test_cible_rapide": 14.4, "test_cible_moyen": 45.6,
                       "test_cible_lent": 60.0})
        self.write("tests/parts/durees.json", json.dumps({"psycopg": prices}))
        self.base = self.commit("base large")
        self.card(self.base, {"src/pkg/mod.py": {
            "chaude": {"modules": large},
            "froide": {"modules": cibles},
        }})
        self.write("src/pkg/mod.py",
                   SOURCE_V1.replace("return 1", "return 10")
                   .replace("return 2", "return 20"))
        report = self.select(self.commit("unité deux blocs"), budget=180)
        (unit,) = report["units"]
        modules = [row["module"] for row in unit["added"]]
        self.assertIn("tests.test_cible_moyen", modules)
        self.assertLess(modules.index("tests.test_cible_moyen"), 5)
        self.assertLessEqual(unit["seconds"], 180)
        self.assertEqual(unit["coverage"],
                         {"unknown_paths": [], "unmapped_blocks": []})

    def test_7195_couverture_nomme_fichier_et_bloc_hors_carte(self) -> None:
        # #7195 : un fichier `src/` neuf et un bloc neuf absents de la carte
        # sont dits, pour qu'une carte antérieure ne fasse pas croire à une
        # sélection complète.
        self.card(self.base)
        self.write("src/pkg/mod.py", SOURCE_V1.replace(
            "return 1", "return 10") + "\n\ndef neuve():\n    return 3\n")
        self.write("src/pkg/neuf.py", "def neuf():\n    return 1\n")
        report = self.select(self.commit("unité carte incomplète"), budget=100)
        (unit,) = report["units"]
        self.assertEqual(unit["coverage"]["unknown_paths"], ["src/pkg/neuf.py"])
        self.assertEqual(unit["coverage"]["unmapped_blocks"],
                         ["src/pkg/mod.py:neuve"])
        self.assertIn("tests.test_chaud_rapide",
                      [row["module"] for row in unit["added"]])

    def test_7195_unite_deux_commits_lue_depuis_la_base(self) -> None:
        # B1 de l'auditeur (#7195) : une unité de deux commits, le premier
        # change le bloc `chaude` couvert par `test_chaud_rapide`, le second
        # un autre fichier. `sha^1..sha` ne voit que le second commit et perd
        # le test attendu ; les blocs et les fichiers se lisent depuis
        # `merge-base(base, unit)`.
        self.card(self.base, {
            "src/pkg/mod.py": {"chaude": {"modules": ["test_chaud_rapide"]}},
            "src/pkg/autre.py": {"<module>": {"modules": ["test_sans_lien"]}},
        })
        self.write("src/pkg/mod.py", SOURCE_V2)
        self.commit("unité, premier commit")
        self.write("src/pkg/autre.py", "def autre():\n    return 1\n")
        self.write("tests/test_autre.py", "X = 1\n")
        self.write("tests/parts/durees.json", json.dumps({"psycopg": {
            "test_chaud_rapide": 1.0, "test_sans_lien": 1.0}}))
        report = self.select(self.commit("unité, second commit"), budget=30)
        (unit,) = report["units"]
        modules = [row["module"] for row in unit["added"]]
        self.assertIn("tests.test_chaud_rapide", modules)
        self.assertIn("tests.test_sans_lien", modules)
        self.assertEqual(unit["coverage"]["unmapped_blocks"],
                         ["src/pkg/autre.py:autre"])

    def test_7211_tete_de_lot_fusionnee_avec_la_cible_lit_l_unite(self) -> None:
        # #7211 : une tête de lot qui a fusionné la cible est une fusion sans
        # trailer `ameesh-work:`. `sha^1..sha` ne voit que le delta de la
        # cible (bloc `chaude`) et perd le bloc de l'unité (`froide`) ; une
        # fusion ordinaire se lit depuis `merge-base(base, unit)`, ici la
        # cible, donc le delta de l'unité seule.
        self.card(self.base)
        self.git("checkout", "-q", "-b", "lot")
        self.write("src/pkg/mod.py", SOURCE_V1.replace("return 2", "return 20"))
        self.commit("unité : bloc froide")
        self.git("checkout", "-q", "main")
        self.write("src/pkg/mod.py", SOURCE_V1.replace("return 1", "return 10"))
        self.base = self.commit("cible : bloc chaude")
        self.git("checkout", "-q", "lot")
        self.git("merge", "--no-ff", "--no-edit", "main")
        report = self.select(self.git("rev-parse", "HEAD"), budget=200)
        (unit,) = report["units"]
        modules = [row["module"] for row in unit["added"]]
        self.assertIn("tests.test_froid", modules)
        self.assertNotIn("tests.test_chaud_rapide", modules)

    def test_7211_composition_reconnue_lit_le_premier_parent(self) -> None:
        # #7211 : une vraie composition (message avec `ameesh-work:`) garde
        # son premier parent, la composition précédente. Sans cette lecture,
        # l'unité 2 lirait aussi le delta de l'unité 1 depuis la base commune.
        self.card(self.base)
        self.git("checkout", "-q", "-b", "unite-1")
        self.write("src/pkg/mod.py", SOURCE_V1.replace("return 1", "return 10"))
        self.commit("unité 1 : bloc chaude")
        self.git("checkout", "-q", "main")
        self.write("README", "cible\n")
        self.base = self.commit("cible avancée")
        self.git("checkout", "-q", "-b", "paquet", "main")
        self.git("merge", "--no-ff", "-m",
                 "composition : #1\n\nameesh-work: 1", "unite-1")
        self.git("checkout", "-q", "-b", "unite-2", "main")
        self.write("src/pkg/mod.py", SOURCE_V1.replace("return 2", "return 20"))
        self.commit("unité 2 : bloc froide")
        self.git("checkout", "-q", "paquet")
        self.git("merge", "--no-ff", "-m",
                 "composition : #2\n\nameesh-work: 2", "unite-2")
        report = self.select(self.git("rev-parse", "HEAD"), budget=200)
        (unit,) = report["units"]
        modules = [row["module"] for row in unit["added"]]
        self.assertIn("tests.test_froid", modules)
        self.assertNotIn("tests.test_chaud_rapide", modules)

    def test_4814_support_partage_borne_par_le_budget(self) -> None:
        # #4814 : `tests/support.py` importé par ~545 modules faisait de la
        # série de l'unité la suite entière ; le support entre au budget, le
        # moins cher d'abord, et le reste est compté
        prices = {}
        for index in range(30):
            name = "test_partage_%02d" % index
            self.write("tests/%s.py" % name, "from .support import X\n")
            prices[name] = 10.0 + index
        self.write("tests/support.py", "X = 1\n")
        self.write("tests/parts/durees.json", json.dumps({"psycopg": prices}))
        self.base = self.commit("support partagé")
        self.write("tests/support.py", "X = 2\n")
        report = self.select(self.commit("unité"), budget=50)
        (unit,) = report["units"]
        self.assertEqual([row["module"] for row in unit["added"]],
                         ["tests.test_partage_00", "tests.test_partage_01",
                          "tests.test_partage_02", "tests.test_partage_03"])
        self.assertEqual({row["reason"] for row in unit["added"]}, {"support"})
        self.assertEqual(unit["dropped"], 26)
        self.assertEqual(unit["seconds"], 46.0)

    def test_carte_anterieure_a_la_base_dite(self) -> None:
        old = self.base
        self.write("README", "x\n")
        self.base = self.commit("base suivante")
        self.card(old)
        report = self.select(self.unit(), budget=100)
        self.assertEqual(report["card"]["base"], "antérieure")
        modules = [row["module"] for row in report["units"][0]["added"]]
        self.assertIn("tests.test_chaud_lent", modules)

    def test_carte_absente_seuls_les_supports(self) -> None:
        report = self.select(self.unit(), budget=100)
        self.assertEqual(report["status"], "ok")
        self.assertFalse(report["card"].get("commit"))
        self.assertEqual({row["reason"] for row in report["units"][0]["added"]},
                         {"support"})


def _red(label: str, modules, test: str) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(
        label, list(modules), 1,
        "FAIL: test_x (%s.T.test_x)\nAssertionError\n" % test, 0.1)


def _green(label: str, modules) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(label, list(modules), 0,
                               "Ran 1 test in 0.1s\n\nOK", 0.1)


class SummaryFinishedModulesTest(unittest.TestCase):
    def test_4814_serie_arretee_n_est_pas_jouee(self) -> None:
        # h-20261030-07 : « 496 modules, 6 tests exécutés » laissait croire
        # que les modules avaient tourné ; une série arrêtée par sa borne
        # (ou jamais partie) n'en joue aucun jusqu'au bout
        runs = [_green("unité #1", ["tests.test_a"]),
                paquet_auto.budget_spent("unité #2",
                                         ["tests.test_b", "tests.test_c"]),
                paquet_auto.TestRun("contrats", ["tests.test_d"], None,
                                    "délai", 1200.0, timed_out=True)]
        summary = paquet_auto._tests_summary(runs)
        self.assertIn("3 séries, 4 modules dont 1 joués jusqu'au bout, "
                      "1 tests exécutés, 0 échec(s)", summary)
        self.assertIn("2 non concluant(s)", summary)


class LocalNeighborsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-4025-")
        self.addCleanup(self.tmp.cleanup)
        self.worktree = pathlib.Path(self.tmp.name)
        self.composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a", "1" * 40),
                   paquet_auto.Unit(2, "b", "2" * 40)],
            discarded=[], head="3" * 40)

    def run_check(self, neighbors, results, at_base=frozenset(), prefix_map=None,
                  prefix=None):
        calls = []
        replayed = []

        def fake_base(_worktree, _base, modules, *, timeout):
            replayed.append(list(modules))
            return None if at_base is None else set(at_base)

        def fake_run(_worktree, label, modules, *, timeout):
            calls.append((label, list(modules)))
            return results[label](modules)

        def fake_prefix(_worktree, _composition, failures, *, timeout, log=None):
            # #4217 : le rejeu des préfixes est simulé par test → lot.
            found = {}
            for failure in failures:
                lot = (prefix_map or {}).get(failure["test"])
                if lot is not None:
                    found.setdefault(lot, []).append(failure)
            if not found:
                return {}, ("préfixe non simulé pour %d test(s)"
                            % len(failures)), []
            return found, "", []

        from unittest import mock
        with mock.patch.object(paquet_auto, "unit_test_modules",
                               side_effect=lambda _w, unit:
                               ["tests.test_propre_%d" % unit.lot]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contrat"]), \
                mock.patch.object(paquet_auto, "run_tests", side_effect=fake_run):
            out = paquet_auto.check_local_tests(
                self.worktree, self.composition, timeout=1,
                log=lambda _line: None,
                attribute=lambda *_a: {"status": "ok", "tests": []},
                select=lambda *_a: (neighbors, "carte d'impact `abc` (lue)"),
                base_sha="4" * 40, on_base=fake_base,
                prefix=prefix or fake_prefix)
        self.replayed = replayed
        return calls, out

    def test_voisin_support_rouge_retire_son_unite(self) -> None:
        neighbors = {1: [{"module": "tests.test_3512_slo_merged",
                          "reason": "support",
                          "files": ["tests/test_789_slo_alerts.py"]}]}
        calls, (runs, faulty, _removals) = self.run_check(
            neighbors, {
                "unité #1": lambda m: _red("unité #1", m,
                                           "tests.test_3512_slo_merged"),
                "unité #2": lambda m: _green("unité #2", m),
                "contrats": lambda m: _green("contrats", m),
            }, prefix_map={"tests.test_3512_slo_merged.T.test_x": 1})
        self.assertEqual(calls[0], ("unité #1", ["tests.test_propre_1",
                                                 "tests.test_3512_slo_merged"]))
        self.assertEqual(faulty, [1])
        self.assertIn("1 échec(s), 0 run(s) rouge(s) sans effet",
                      paquet_auto._tests_summary(runs))
        self.assertIn("4 modules dont 4 joués jusqu'au bout",
                      paquet_auto._tests_summary(runs))
        manifest = paquet_auto.build_manifest(
            name="banc", target="main", base_sha="4" * 40, carrier=99,
            composition=self.composition, table="## Composition\n",
            tests=runs, freeze_seconds=0.0)
        section = manifest.split("Modules voisins joués", 1)[1] \
            .split("- `scripts/paquet-inclusion.py`", 1)[0]
        self.assertIn("| unité 1 | `tests.test_3512_slo_merged` | support |",
                      section)
        self.assertIn("carte d'impact `abc`", section)
        self.assertNotIn("#", section)

    def test_voisin_de_carte_partage_est_situe_au_prefixe(self) -> None:
        # #4217 : un rouge partagé n'est plus laissé à la ci-full ; le rejeu
        # des préfixes situe la première unité où le test apparaît.
        shared = {"module": "tests.test_l165b_agents_libres", "reason": "carte",
                  "files": ["src/ameesh/stagnation.py"]}
        calls, (_runs, faulty, _removals) = self.run_check(
            {1: [shared], 2: [shared]}, {
                "unité #1": lambda m: _red("unité #1", m,
                                           "tests.test_l165b_agents_libres"),
                "unité #2": lambda m: _red("unité #2", m,
                                           "tests.test_l165b_agents_libres"),
                "contrats": lambda m: _green("contrats", m),
            }, prefix_map={"tests.test_l165b_agents_libres.T.test_x": 1})
        self.assertEqual(faulty, [1])

    def test_voisin_de_carte_propre_a_l_unite_la_designe(self) -> None:
        mine = {"module": "tests.test_l165b_agents_libres", "reason": "carte",
                "files": ["src/ameesh/stagnation.py"]}
        _calls, (_runs, faulty, _removals) = self.run_check(
            {2: [mine]}, {
                "unité #1": lambda m: _green("unité #1", m),
                "unité #2": lambda m: _red("unité #2", m,
                                           "tests.test_l165b_agents_libres"),
                "contrats": lambda m: _green("contrats", m),
            }, prefix_map={"tests.test_l165b_agents_libres.T.test_x": 2})
        self.assertEqual(faulty, [2])

    def test_voisin_rouge_aussi_sur_la_base_ou_non_rejoue(self) -> None:
        # h-42 : test_1651_ci_parts rouge par l'hôte (bwrap), vert en CI.
        # #4887 (h-10) : un rejeu de base non concluant (None) ne vaut plus
        # « sans effet » — le rouge de voisin est conservé et rejoué ; seul un
        # rouge aussi constaté sur la base est écarté.
        support = {"module": "tests.test_1651_ci_parts", "reason": "support",
                   "files": ["tests/test_1467_train.py"]}
        name = "tests.test_1651_ci_parts.T.test_x"
        results = {
            "unité #1": lambda m: _red("unité #1", m,
                                       "tests.test_1651_ci_parts"),
            "unité #2": lambda m: _green("unité #2", m),
            "contrats": lambda m: _green("contrats", m),
        }
        _calls, (runs, faulty, _removals) = self.run_check(
            {1: [support]}, dict(results), at_base={name})
        self.assertEqual(self.replayed, [["tests.test_1651_ci_parts"]])
        self.assertEqual(faulty, [])
        self.assertFalse(runs[0].green)
        self.assertTrue(runs[0].ignored_red)
        manifest = paquet_auto.build_manifest(
            name="banc", target="main", base_sha="4" * 40, carrier=99,
            composition=self.composition, table="## Composition\n",
            tests=runs, freeze_seconds=0.0)
        self.assertIn("0 échec(s), 1 run(s) rouge(s) sans effet", manifest)
        # base non concluante : le rouge n'est ni jeté ni attribué (le préfixe
        # 1 ne distingue pas un rouge préexistant, #4887) ; aucun retrait.
        _calls, (runs, faulty, _removals) = self.run_check(
            {1: [support]}, dict(results), at_base=None,
            prefix=lambda *_a, **_k: self.fail(
                "rejeu des préfixes sans base concluante"))
        self.assertEqual(faulty, [])
        self.assertFalse(runs[0].green)
        self.assertFalse(runs[0].ignored_red)
        self.assertTrue(runs[0].unattributed)
        manifest = paquet_auto.build_manifest(
            name="banc", target="main", base_sha="4" * 40, carrier=99,
            composition=self.composition, table="## Composition\n",
            tests=runs, freeze_seconds=0.0)
        self.assertIn("1 échec(s), 0 run(s) rouge(s) sans effet", manifest)

    def test_rouge_mixte_garde_l_echec_propre(self) -> None:
        support = {"module": "tests.test_voisin", "reason": "support",
                   "files": ["tests/test_support.py"]}
        def mixed(modules):
            run = _red("unité #1", modules, "tests.test_voisin")
            run.output += _red("unité #1", modules,
                               "tests.test_propre_1").output
            return run
        _calls, (runs, faulty, _removals) = self.run_check({1: [support]}, {
            "unité #1": mixed,
            "unité #2": lambda m: _green("unité #2", m),
            "contrats": lambda m: _green("contrats", m),
        }, at_base={"tests.test_voisin.T.test_x"})
        self.assertEqual(faulty, [1])
        self.assertFalse(runs[0].ignored_red)
        self.assertIn("1 échec(s), 0 run(s) rouge(s) sans effet",
                      paquet_auto._tests_summary(runs))

    def test_module_propre_rouge_n_est_pas_rejoue(self) -> None:
        _calls, (_runs, faulty, _removals) = self.run_check({}, {
            "unité #1": lambda m: _red("unité #1", m, "tests.test_propre_1"),
            "unité #2": lambda m: _green("unité #2", m),
            "contrats": lambda m: _green("contrats", m),
        })
        self.assertEqual(self.replayed, [])
        self.assertEqual(faulty, [1])

    def test_outil_absent_n_ajoute_rien(self) -> None:
        found, note = paquet_auto.neighbor_modules(
            self.worktree, self.composition, "4" * 40)
        self.assertEqual(found, {})
        self.assertIn("absent", note)

    def test_7195_note_dit_la_carte_incomplete(self) -> None:
        # #7195 : la note du manifeste nomme les fichiers/blocs que la carte
        # ne couvre pas, sans bloquer la sélection.
        (self.worktree / "scripts").mkdir()
        (self.worktree / "scripts/paquet-attribution.py").write_text("")
        (self.worktree / "tests").mkdir()
        (self.worktree / "tests/test_voisin.py").write_text("X = 1\n")
        report = {
            "status": "ok",
            "card": {"commit": "a" * 40, "status": "lue", "base": "antérieure"},
            "durations": {"table": "tests/parts/durees.json"},
            "units": [{
                "lot": 1,
                "added": [{"module": "tests.test_voisin", "reason": "carte",
                           "files": ["src/ameesh/x.py"], "seconds": 10.0}],
                "dropped": 0, "seconds": 10.0,
                "coverage": {"unknown_paths": ["src/ameesh/neuf.py"],
                             "unmapped_blocks": ["src/ameesh/x.py:neuve"]},
            }],
        }
        proc = type("Proc", (), {"returncode": 0,
                                 "stdout": json.dumps(report).encode(),
                                 "stderr": b""})()
        from unittest import mock
        with mock.patch.object(paquet_auto, "project_python",
                               return_value="python3"), \
                mock.patch.object(paquet_auto, "test_driver",
                                  return_value="psycopg"), \
                mock.patch.object(paquet_auto, "python_env", return_value={}), \
                mock.patch.object(paquet_auto, "run_command", return_value=proc):
            found, note = paquet_auto.neighbor_modules(
                self.worktree, self.composition, "4" * 40)
        self.assertEqual([row["module"] for row in found[1]],
                         ["tests.test_voisin"])
        self.assertIn("carte incomplète : 1 fichier(s) hors carte, 1 bloc(s) "
                      "non mesuré(s) (#7195)", note)


if __name__ == "__main__":
    unittest.main()
