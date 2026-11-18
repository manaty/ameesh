# SPDX-License-Identifier: AGPL-3.0-only
"""#4887 — la série locale tronquée ne laisse plus passer un rouge direct.

h-10 du 30/10 : dix séries, deux créneaux, 1800 s. La première série pouvait
prendre 1200 s et la deuxième le reste ; huit séries ne partaient jamais.
`test_2442` (#4834), `test_3805`/`test_3762` (#4791) et le schéma client
périmé (#4831) n'ont été vus qu'en CI, trois CI perdues.

Témoins :

* `series_share` découpe la borne totale en vagues : toutes les unités
  commencent, même quand chaque série consomme toute sa part ;
* un rouge dans le module direct du **dernier** lot est trouvé avant la CI,
  alors que la borne coupe ensuite ;
* le corps de la PR dit, lot par lot, si ses modules directs ont été joués ;
* le contrôle « types clients à jour » du job plan refuse une composition qui
  change l'OpenAPI sans le schéma généré, et lit le vrai `gen-api --check`
  quand Node et les dépendances sont là.

Aucune base, aucun réseau.
"""
from __future__ import annotations

import importlib.util
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


paquet_auto = _load_script(ROOT / "scripts" / "paquet-auto.py", "test_paquet_auto_4887")


def _composition(lots=(1, 2, 3)):
    return paquet_auto.Composition(
        units=[paquet_auto.Unit(lot, "a" * 40, ("%d" % lot) * 40) for lot in lots],
        discarded=[], head="e" * 40)


def _green(label, modules, seconds=0.1):
    return paquet_auto.TestRun(
        label, list(modules), 0, "Ran 1 test in 0.1s\n\nOK\n", seconds)


def _red(label, modules, module):
    output = ("FAIL: %s (tests.%s.T.%s)\n"
              "----------------------------------------------------------------------\n"
              "Traceback (most recent call last):\n"
              "  File \"tests/test_u.py\", line 1, in test\n"
              "AssertionError: boom\n\n"
              "Ran 1 test in 0.1s\n\n"
              "FAILED (failures=1)\n" % (module, module, module))
    return paquet_auto.TestRun(label, list(modules), 1, output, 0.1)


class PartDeBorneTest(unittest.TestCase):
    """#4887 : `series_share` divise la borne en vagues, jamais en une série."""

    def test_la_part_divise_par_vagues(self) -> None:
        import time
        now = time.monotonic()
        self.assertLessEqual(paquet_auto.series_share(1200, now + 1000, 10, 2), 200)
        self.assertGreaterEqual(paquet_auto.series_share(1200, now + 1000, 10, 2), 199)
        self.assertIn(paquet_auto.series_share(1200, now + 1000, 1, 2), (999, 1000))
        self.assertIn(paquet_auto.series_share(1200, now + 1000, 10, 2, 300),
                      (139, 140))
        self.assertIn(paquet_auto.series_share(300, now + 1000, 10, 2), (199, 200))
        self.assertEqual(paquet_auto.series_share(1200, None, 10, 2), 1200)

    def test_echeance_passee_rend_none(self) -> None:
        import time
        self.assertIsNone(paquet_auto.series_share(1200, time.monotonic() - 1, 4, 2))


class PartageDeBorneTest(unittest.TestCase):
    """#4887/#7740 : la série directe garde sa borne entière ; seule la borne
    totale (temps mural) peut la couper, et un lot tardif reste atteint."""

    def local(self, lots, run, **kwargs):
        composition = _composition(lots)
        lines: list[str] = []

        def fake(_worktree, label, modules, *, timeout, heavy=False, wall=None):
            self.calls.append((label, timeout, wall))
            return run(label, modules, timeout=timeout)

        with mock.patch.object(paquet_auto, "unit_test_modules",
                               side_effect=lambda _w, unit, **_k: [
                                   "tests.test_u%d" % unit.lot]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contrat"]), \
                mock.patch.object(paquet_auto.shutil, "which", return_value=None), \
                mock.patch.object(paquet_auto, "run_tests", side_effect=fake):
            return paquet_auto.check_local_tests(
                pathlib.Path("."), composition, timeout=900, parallel=1,
                select=lambda *_: ({}, ""), attribute=lambda *_a: {
                    "status": "no-helper", "tests": [], "why": "banc"},
                log=lines.append, **kwargs)

    def setUp(self) -> None:
        self.calls: list[tuple[str, int, int | None]] = []

    def test_chaque_serie_directe_garde_sa_borne(self) -> None:
        clock = [1000.0]

        def run(label, modules, *, timeout):
            # La première série consomme presque toute la borne totale : avec
            # la part #4887, les suivantes ne partaient plus.
            clock[0] += 800 if label == "unité #1" else 10
            return _green(label, modules)

        with mock.patch.object(paquet_auto.time, "monotonic",
                               side_effect=lambda: clock[0]):
            runs, faulty, _ = self.local((1, 2, 3), run,
                                         deadline=1000.0 + 900)
        units = [call for call in self.calls if call[0].startswith("unité")]
        self.assertEqual([label for label, _limit, _wall in units],
                         ["unité #1", "unité #2", "unité #3"])
        # #7740 : chaque série part avec `--tests-timeout`, jamais une part de
        # la borne totale ; le temps restant ne la coupe qu'en temps mural.
        self.assertEqual([limit for _label, limit, _wall in units],
                         [900, 900, 900])
        self.assertEqual([wall for _label, _limit, wall in units],
                         [900, 100, 90])
        self.assertEqual(faulty, [])
        unit_runs = [run for run in runs if run.label.startswith("unité #")]
        self.assertEqual([run.direct for run in unit_runs],
                         [["tests.test_u1"], ["tests.test_u2"], ["tests.test_u3"]])

    def test_rouge_direct_du_dernier_lot_trouve_avant_la_ci(self) -> None:
        clock = [1000.0]

        def run(label, modules, *, timeout):
            clock[0] += 5
            if label == "unité #3":
                return _red(label, modules, "test_u3")
            return _green(label, modules)

        with mock.patch.object(paquet_auto.time, "monotonic",
                               side_effect=lambda: clock[0]):
            runs, faulty, removals = self.local((1, 2, 3), run,
                                                deadline=1000.0 + 120)
        self.assertEqual(faulty, [3])
        self.assertEqual([removal.lot for removal in removals], [3])
        self.assertEqual([run.label for run in runs],
                         ["unité #1", "unité #2", "unité #3", "contrats"])

    def test_une_serie_coupee_ne_revendique_aucun_module(self) -> None:
        clock = [1000.0]

        def run(label, modules, *, timeout):
            clock[0] += 5
            if label == "unité #2":
                return paquet_auto.TestRun(
                    label, list(modules), None,
                    "borne totale atteinte : série arrêtée", 5.0,
                    timed_out=True)
            return _green(label, modules)

        with mock.patch.object(paquet_auto.time, "monotonic",
                               side_effect=lambda: clock[0]):
            runs, faulty, _ = self.local((1, 2), run, deadline=1000.0 + 60)
        cut = [run for run in runs if run.label == "unité #2"][0]
        self.assertTrue(cut.inconclusive)
        self.assertFalse(cut.finished)
        self.assertEqual(faulty, [])
        self.assertIn("0/1, série coupée", "\n".join(
            paquet_auto._direct_lines(_composition((1, 2)), runs)))


class VoisinNonRejoueTest(unittest.TestCase):
    """h-10 : un voisin rouge que la base ne permet pas de rejouer n'est pas
    « sans effet ». Le composer jetait `test_2442` (#4834) d'une série rouge
    parce que `base_failures` avait rendu None ; le compte disait « 1 run rouge
    sans effet »."""

    def check(self, on_base, prefix):
        composition = _composition((1,))
        neighbor = {1: [{"module": "tests.test_neighbor", "reason": "carte",
                         "files": ["src/pkg/x.py"]}]}

        def fake(_worktree, label, modules, *, timeout, heavy=False, wall=None):
            if label != "unité #1":
                return _green(label, modules)
            return _red(label, modules, "test_neighbor")

        calls = []
        with mock.patch.object(paquet_auto, "unit_test_modules",
                               return_value=["tests.test_own"]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contrat"]), \
                mock.patch.object(paquet_auto, "run_tests", side_effect=fake):
            runs, faulty, removals = paquet_auto.check_local_tests(
                pathlib.Path("."), composition, timeout=900, parallel=1,
                base_sha="b" * 40,
                select=lambda *_: (neighbor, "carte simulée"),
                on_base=on_base, prefix=prefix,
                attribute=lambda *_a: {"status": "no-helper", "tests": [],
                                       "why": "banc"},
                log=calls.append)
        return runs, faulty, removals, calls

    def test_base_non_concluante_garde_le_rouge_sans_attribuer(self) -> None:
        # Auditeur B : le préfixe 1 ne distingue pas un rouge préexistant d'un
        # rouge de u1 ; sans base concluante, le rouge est conservé mais aucun
        # retrait n'est fait (la ci-full tranche, §3).
        def prefix(*_args, **_kwargs):
            raise AssertionError("rejeu des préfixes sans base concluante")

        runs, faulty, removals, lines = self.check(
            lambda *_a, **_k: None, prefix)
        unit = [run for run in runs if run.label == "unité #1"][0]
        self.assertFalse(unit.ignored_red)
        self.assertEqual(unit.unattributed,
                         ["tests.test_neighbor.T.test_neighbor"])
        self.assertEqual(faulty, [])
        self.assertEqual(removals, [])
        self.assertFalse(any("sans effet" in line for line in lines))
        self.assertIn("NON ATTRIBUÉ", "\n".join(
            paquet_auto._unattributed_lines(runs)))
        self.assertIn("1 échec(s), 0 run(s) rouge(s) sans effet",
                      paquet_auto._tests_summary(runs))

    def test_rouge_aussi_sur_la_base_reste_sans_effet(self) -> None:
        replayed = []

        def prefix(*args, **kwargs):
            replayed.append(args[2])
            return {}, "", []

        runs, faulty, _removals, lines = self.check(
            lambda *_a, **_k: {"tests.test_neighbor.T.test_neighbor"}, prefix)
        unit = [run for run in runs if run.label == "unité #1"][0]
        self.assertTrue(unit.ignored_red)
        self.assertFalse(replayed)
        self.assertEqual(faulty, [])
        self.assertTrue(any("sans effet" in line for line in lines))


class ManifesteDirectTest(unittest.TestCase):
    def test_le_corps_dit_par_lot_joues_ou_non(self) -> None:
        composition = _composition((1, 2))
        runs = [
            _green("unité #1", ["tests.test_a"], seconds=12.0),
            paquet_auto.TestRun(
                "unité #2", ["tests.test_b", "tests.test_c"], None,
                "borne totale atteinte", 5.0, timed_out=True,
                direct=["tests.test_b", "tests.test_c"]),
        ]
        runs[0].direct = ["tests.test_a"]
        text = "\n".join(paquet_auto._direct_lines(composition, runs))
        self.assertIn("| #1 | 1 | oui (1/1) |", text)
        self.assertIn("| #2 | 2 | non (0/2, série coupée après 5 s)", text)
        self.assertEqual(paquet_auto._direct_note(composition, runs),
                         "modules directs non joués (non concluant) : #2")

    def test_le_corps_de_pr_porte_la_table(self) -> None:
        composition = _composition((1,))
        candidate = paquet_auto.Candidate(1, "a" * 40, title="lot")
        text = paquet_auto.pr_body(
            name="h-test", target="main", base_sha="b" * 40, carrier=9,
            composition=composition, candidates=[candidate],
            tests=[_green("unité #1", ["tests.test_a"])],
            inclusion_rc=0, freeze_seconds=1.0)
        self.assertIn("Modules directs par lot (#4887, #7740)", text)
        self.assertIn("| #1 | 1 | oui (1/1) |", text)

    def test_zero_module_direct_est_un_non_concluant_nomme(self) -> None:
        # #7740 : 0 module joué n'est jamais un « oui (0/0) » vert.
        composition = _composition((1,))
        runs = [_green("unité #1", [])]
        text = "\n".join(paquet_auto._direct_lines(composition, runs))
        self.assertIn("| #1 | 0 | aucun module direct (non concluant) |", text)
        self.assertEqual(paquet_auto._direct_note(composition, runs),
                         "modules directs non joués (non concluant) : #1")


class TypesClientsTest(unittest.TestCase):
    """#4887 : le contrôle rapide du plan avant la poussée et la CI."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="paquet-4887-")
        self.addCleanup(tmp.cleanup)
        self.repo = pathlib.Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write(paquet_auto.CLIENT_SPEC,
                   '{"openapi": "3.1.0", "paths": {}}\n')
        self.write(paquet_auto.CLIENT_SCHEMA, "// schema\n")
        self.write(paquet_auto.CLIENT_GEN, "// générateur factice\n")
        self.base = self.commit("base")

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

    def test_openapi_sans_schema_est_non_concluant_sans_node(self) -> None:
        # cx1 B1/P2 : un reformatage JSON ou un info.version peut laisser les
        # types identiques ; sans gen-api --check, on consigne, on ne bloque pas.
        self.write(paquet_auto.CLIENT_SPEC,
                   '{"openapi": "3.1.0", "paths": {"/x": {}}}\n')
        unit_sha = self.commit("openapi seul")
        with mock.patch.object(paquet_auto.shutil, "which", return_value=None):
            ok, note = paquet_auto.check_client_types(
                self.repo, self.base, repo=self.repo,
                units=[paquet_auto.Unit(7, self.base, unit_sha)])
        self.assertTrue(ok)
        self.assertIn("non concluant", note)
        self.assertIn("la CI tranche", note)
        self.assertIn("#7", note)

    def test_openapi_et_schema_acceptes_sans_node(self) -> None:
        self.write(paquet_auto.CLIENT_SPEC,
                   '{"openapi": "3.1.0", "paths": {"/x": {}}}\n')
        self.write(paquet_auto.CLIENT_SCHEMA, "// nouveau schéma\n")
        self.commit("openapi et schéma")
        with mock.patch.object(paquet_auto.shutil, "which", return_value=None):
            ok, note = paquet_auto.check_client_types(
                self.repo, self.base, repo=self.repo)
        self.assertTrue(ok)
        self.assertIn("non concluant", note)

    def test_dependances_ui_modifiees_ne_bloquent_pas(self) -> None:
        self.write("ui/package-lock.json", "{}\n")
        self.write(paquet_auto.CLIENT_SPEC,
                   '{"openapi": "3.1.0", "paths": {"/x": {}}}\n')
        self.commit("dépendances ui")
        ok, note = paquet_auto.check_client_types(
            self.repo, self.base, repo=self.repo)
        self.assertTrue(ok)
        self.assertIn("dépendances ui modifiées", note)

    def test_le_vrai_gen_api_est_lu_quand_node_est_la(self) -> None:
        self.write(paquet_auto.CLIENT_SPEC,
                   '{"openapi": "3.1.0", "paths": {"/x": {}}}\n')
        self.write(paquet_auto.CLIENT_SCHEMA, "// schéma\n")
        self.commit("openapi seul")
        (self.repo / "ui" / "node_modules" / "openapi-typescript").mkdir(parents=True)
        stale = subprocess.CompletedProcess(
            ["node"], 1, b"", b"ui/src/api/schema.d.ts is stale\n")
        with mock.patch.object(paquet_auto.shutil, "which",
                               return_value="/usr/bin/node"), \
                mock.patch.object(paquet_auto, "run_command", return_value=stale):
            ok, note = paquet_auto.check_client_types(
                self.repo, self.base, repo=self.repo)
        self.assertFalse(ok)
        self.assertIn("gen-api --check", note)
        self.assertIn("stale", note)

    def test_node_modules_prete_par_le_clone_est_un_lien_temporaire(self) -> None:
        worktree = self.repo / "worktree"
        (worktree / "ui").mkdir(parents=True)
        source = self.repo / "ui" / "node_modules" / "openapi-typescript"
        source.mkdir(parents=True)
        link, linked, _why = paquet_auto._client_node_modules(worktree, self.repo)
        self.assertTrue(linked)
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), source.parent.resolve())
        link.unlink()


class DepotTest(unittest.TestCase):
    """Un dépôt git réel, sans base ni réseau, pour les témoins #7740."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="paquet-7740-")
        self.addCleanup(tmp.cleanup)
        self.repo = pathlib.Path(tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("tests/__init__.py", "")
        self.write("src/__init__.py", "")

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


class TemoinsEmpreinteTest(DepotTest):
    """#7740 : migration, stockage et alertes de pause traînent leurs témoins."""

    def setUp(self) -> None:
        super().setUp()
        for name in (paquet_auto.MIGRATION_WITNESSES
                     + paquet_auto.STORAGE_WITNESSES
                     + paquet_auto.ALERT_BRIEF_WITNESSES):
            self.write(name.replace(".", "/") + ".py", "")
        self.base = self.commit("base")

    def unit(self, *paths: str) -> tuple[str, str]:
        for path in paths:
            self.write(path, "X = 1\n")
        return self.base, self.commit("unité")

    def test_une_migration_joue_les_temoins_de_roles_et_de_schema(self) -> None:
        base, commit = self.unit("src/ameesh/migrations/0001_x.py")
        rows = paquet_auto.footprint_witnesses(
            self.repo, paquet_auto.Unit(7, base, commit))
        self.assertEqual([row["module"] for row in rows],
                         ["tests.test_cluster", "tests.test_migrations",
                          "tests.test_migrations_additives"])
        self.assertTrue(all(row["reason"] == "empreinte" for row in rows))

    def test_un_stockage_joue_les_contrats_des_ecrivains(self) -> None:
        base, commit = self.unit("src/ameesh/storage/x.py")
        modules = [row["module"] for row in paquet_auto.footprint_witnesses(
            self.repo, paquet_auto.Unit(8, base, commit))]
        self.assertEqual(modules, ["tests.test_l107_executor_contract",
                                   "tests.test_contract_registry_ameesh",
                                   "tests.test_contract_selection"])

    def test_une_alerte_de_pause_joue_son_temoin(self) -> None:
        # Veille 17:58 du 17/11 : test_5424 est resté rouge en CI parce que
        # l'unité touchait alert_briefs.py sans le jouer.
        base, commit = self.unit("src/ameesh/alert_briefs.py")
        rows = paquet_auto.footprint_witnesses(
            self.repo, paquet_auto.Unit(10, base, commit))
        self.assertEqual([row["module"] for row in rows],
                         ["tests.test_5424_review_family_paused"])
        self.assertEqual(rows[0]["files"], ["src/ameesh/alert_briefs.py"])

    def test_sans_empreinte_aucun_temoin(self) -> None:
        base, commit = self.unit("docs/readme.md")
        self.assertEqual(paquet_auto.footprint_witnesses(
            self.repo, paquet_auto.Unit(9, base, commit)), [])


class PrefixeComposeTest(DepotTest):
    """#7740 : chaque série directe joue sur son préfixe composé.

    Cas de sens réel (#7719 vs #7725) : l'unité 1 ajoute un test qui attend la
    valeur 0, l'unité 2 passe la valeur à 2, l'unité 3 la ramène à 0. La tête
    composée est verte, mais le préfixe de l'unité 2 est rouge : le conflit
    est vu à l'unité qui le crée, et elle est retirée avant la poussée.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write("src/value.py", "VALUE = 0\n")
        self.base = self.commit("base")
        self.write("tests/test_value.py",
                   "import unittest\n\n"
                   "from src.value import VALUE\n\n\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_value(self):\n"
                   "        self.assertEqual(VALUE, 0)\n")
        self.a = self.commit("a : test attend 0")
        self.write("src/value.py", "VALUE = 2\n")
        self.b = self.commit("b : valeur 2")
        self.write("src/value.py", "VALUE = 0\n")
        self.c = self.commit("c : retour à 0")

    def composition(self) -> "paquet_auto.Composition":
        return paquet_auto.Composition(
            units=[paquet_auto.Unit(1, self.base, self.a),
                   paquet_auto.Unit(2, self.a, self.b),
                   paquet_auto.Unit(3, self.b, self.c)],
            discarded=[], head=self.c)

    def test_le_conflit_de_sens_est_vu_au_prefixe_de_l_unite(self) -> None:
        calls: list[tuple[str, tuple[str, ...], int]] = []
        lines: list[str] = []
        neighbors = {2: [{"module": "tests.test_value", "reason": "carte",
                          "files": ["src/value.py"]}]}

        def value_at(tree) -> int:
            text = (pathlib.Path(tree) / "src" / "value.py").read_text()
            return int(text.rsplit("=", 1)[1])

        def fake(tree, label, modules, *, timeout, heavy=False, wall=None):
            watched = [module for module in modules
                       if module == "tests.test_value"
                       or module.startswith("tests.test_value.")]
            calls.append((label, tuple(modules), value_at(tree)))
            if watched and value_at(tree) == 2:
                return _red(label, modules, "test_value")
            return _green(label, modules)

        with mock.patch.object(paquet_auto.shutil, "which", return_value=None), \
                mock.patch.object(paquet_auto, "contract_modules", return_value=[]), \
                mock.patch.object(paquet_auto, "run_tests", side_effect=fake):
            runs, faulty, removals = paquet_auto.check_local_tests(
                self.repo, self.composition(), timeout=900, parallel=1,
                base_sha=self.base,
                select=lambda *_: (neighbors, "carte simulée"),
                on_base=lambda *_a, **_k: set(),
                attribute=lambda *_a: {"status": "no-helper", "tests": [],
                                       "why": "banc"},
                log=lines.append)
        # La tête composée est verte : le rouge n'existe que sur le préfixe.
        self.assertEqual((self.repo / "src" / "value.py").read_text().strip(),
                         "VALUE = 0")
        series = {label: value for label, _modules, value in calls}
        self.assertEqual(series["unité #1"], 0)
        self.assertEqual(series["unité #2"], 2)   # a + b, pas la tête
        self.assertEqual(faulty, [2])
        self.assertEqual([(removal.lot, removal.proof) for removal in removals],
                         [(2, "prefixes")])
        by_label = {run.label: run for run in runs}
        self.assertEqual(by_label["unité #2"].direct, ["tests.test_value"])
        self.assertFalse(by_label["unité #2"].green)
        # 0 module joué pour l'unité 3 : non concluant nommé, jamais vert.
        self.assertTrue(by_label["unité #3"].inconclusive)
        self.assertIn("aucun module direct sélectionné", by_label["unité #3"].output)
        self.assertEqual(paquet_auto._direct_note(self.composition(), runs),
                         "modules directs non joués (non concluant) : #3")
        # les worktrees de préfixe ne restent pas derrière.
        listing = self.git("worktree", "list", "--porcelain")
        self.assertEqual(listing.count("worktree "), 1)

    def test_une_serie_sans_module_est_non_concluante_sans_retrait(self) -> None:
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(3, self.b, self.c)], discarded=[],
            head=self.c)
        lines: list[str] = []
        with mock.patch.object(paquet_auto.shutil, "which", return_value=None), \
                mock.patch.object(paquet_auto, "contract_modules", return_value=[]):
            runs, faulty, removals = paquet_auto.check_local_tests(
                self.repo, composition, timeout=900, parallel=1,
                base_sha=self.b, select=lambda *_: ({}, ""),
                log=lines.append)
        self.assertEqual(faulty, [])
        self.assertEqual(removals, [])
        self.assertTrue(runs[0].inconclusive)
        self.assertIn("non concluant", "\n".join(lines))

    def test_un_test_supprime_plus_tard_reste_joue_sur_son_prefixe(self) -> None:
        # #7740 : le test n'existe plus sur la tête, mais il existe sur le
        # préfixe de l'unité qui l'apporte ; l'unité suivante le supprime.
        self.write("tests/test_deleted.py",
                   "import unittest\n\n\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n")
        added = self.commit("ajoute un test")
        (self.repo / "tests" / "test_deleted.py").unlink()
        removed = self.commit("supprime le test")
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(5, self.base, added),
                   paquet_auto.Unit(6, added, removed)],
            discarded=[], head=removed)
        calls: list[tuple[str, tuple[str, ...]]] = []

        def fake(tree, label, modules, *, timeout, heavy=False, wall=None):
            calls.append((label, tuple(modules)))
            return _green(label, modules)

        with mock.patch.object(paquet_auto.shutil, "which", return_value=None), \
                mock.patch.object(paquet_auto, "contract_modules", return_value=[]), \
                mock.patch.object(paquet_auto, "run_tests", side_effect=fake):
            runs, faulty, removals = paquet_auto.check_local_tests(
                self.repo, composition, timeout=60, parallel=1,
                base_sha=self.base, select=lambda *_: ({}, ""),
                log=lambda _line: None)
        self.assertEqual(faulty, [])
        self.assertEqual(removals, [])
        self.assertIn(("unité #5", ("tests.test_deleted",)), calls)
        by_label = {run.label: run for run in runs}
        self.assertTrue(by_label["unité #5"].green)
        self.assertTrue(by_label["unité #6"].inconclusive)




if __name__ == "__main__":
    unittest.main()
