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
    """#4887 : la part de chaque série, puis le rouge direct d'un lot tardif."""

    def local(self, lots, run, **kwargs):
        composition = _composition(lots)
        lines: list[str] = []

        def fake(_worktree, label, modules, *, timeout, heavy=False, wall=None):
            self.calls.append((label, timeout, wall))
            return run(label, modules, timeout=timeout)

        with mock.patch.object(paquet_auto, "unit_test_modules",
                               side_effect=lambda _w, unit: ["tests.test_u%d" % unit.lot]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contrat"]), \
                mock.patch.object(paquet_auto.shutil, "which", return_value="/bin/ameesh"), \
                mock.patch.object(paquet_auto, "run_tests", side_effect=fake):
            return paquet_auto.check_local_tests(
                pathlib.Path("."), composition, timeout=900, parallel=2,
                select=lambda *_: ({}, ""), attribute=lambda *_a: {
                    "status": "no-helper", "tests": [], "why": "banc"},
                log=lines.append, **kwargs)

    def setUp(self) -> None:
        self.calls: list[tuple[str, int, int | None]] = []

    def test_part_de_borne_fait_partir_chaque_lot(self) -> None:
        clock = [1000.0]

        def run(label, modules, *, timeout):
            clock[0] += timeout       # la série consomme toute sa part
            return _green(label, modules)

        with mock.patch.object(paquet_auto.time, "monotonic",
                               side_effect=lambda: clock[0]):
            runs, faulty, _ = self.local((1, 2, 3), run,
                                         deadline=1000.0 + 120)
        # 4 séries, 2 à la fois : 60 s, puis 30 s, puis 30 s ; les contrats
        # tombent à l'échéance. Sans part, la première prenait les 120 s et les
        # deux autres ne partaient jamais.
        # 120 s, réserve de 30 s pour les rejeux : 90 s utiles sur 2 vagues
        # (45, puis 22 sur les 3 séries restantes, puis 23) ; les contrats
        # tombent sur la réserve seule et ne partent pas.
        self.assertEqual([(label, limit) for label, limit, _wall in self.calls],
                         [("unité #1", 45), ("unité #2", 22),
                          ("unité #3", 23)])
        self.assertTrue(all(wall is not None for _label, _limit, wall in self.calls))
        self.assertEqual(faulty, [])
        unit_runs = [run for run in runs if run.label.startswith("unité #")]
        self.assertEqual([run.direct for run in unit_runs],
                         [["tests.test_u1"], ["tests.test_u2"], ["tests.test_u3"]])

    def test_rouge_direct_du_dernier_lot_trouve_avant_la_ci(self) -> None:
        clock = [1000.0]

        def run(label, modules, *, timeout):
            clock[0] += timeout
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
            clock[0] += timeout
            if label == "unité #2":
                return paquet_auto.TestRun(
                    label, list(modules), None,
                    "borne totale atteinte : série arrêtée", float(timeout),
                    timed_out=True)
            return _green(label, modules)

        with mock.patch.object(paquet_auto.time, "monotonic",
                               side_effect=lambda: clock[0]):
            runs, faulty, _ = self.local((1, 2), run, deadline=1000.0 + 60)
        cut = [run for run in runs if run.label == "unité #2"][0]
        self.assertTrue(cut.inconclusive)
        self.assertFalse(cut.finished)
        self.assertEqual(faulty, [])
        self.assertIn("non (série coupée", "\n".join(
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
        self.assertIn("| #2 | 2 | non (série coupée après 5 s)", text)
        self.assertEqual(paquet_auto._direct_note(composition, runs),
                         "modules directs non joués : #2")

    def test_le_corps_de_pr_porte_la_table(self) -> None:
        composition = _composition((1,))
        candidate = paquet_auto.Candidate(1, "a" * 40, title="lot")
        text = paquet_auto.pr_body(
            name="h-test", target="main", base_sha="b" * 40, carrier=9,
            composition=composition, candidates=[candidate],
            tests=[_green("unité #1", ["tests.test_a"])],
            inclusion_rc=0, freeze_seconds=1.0)
        self.assertIn("Modules directs par lot (#4887)", text)
        self.assertIn("| #1 | 1 | oui (1/1) |", text)


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
        link, linked = paquet_auto._client_node_modules(worktree, self.repo)
        self.assertTrue(linked)
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), source.parent.resolve())
        link.unlink()


if __name__ == "__main__":
    unittest.main()
