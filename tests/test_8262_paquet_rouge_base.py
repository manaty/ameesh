# SPDX-License-Identifier: AGPL-3.0-only
"""#8262 — un rouge déjà présent sur la base n'est pas imputé au paquet.

h-09 et h-10 (24/11) ont été abandonnés pour test_7652, rouge sur main
2bde46fa : la porte n'avait que « unité suspecte » ou « non attribuable ».
Témoins, sans base ni réseau :

* `check_local_tests` rejoue les rouges propres et de contrat sur la base
  exacte ; même test et même classe d'échec = rouge de la base, consigné non
  concluant, sans retrait ; classe différente, rejeu non concluant ou noms
  sans classe = geste antérieur (retrait) ;
* `notify_base_red`/`base_red_lot` ouvrent ou relancent le lot de déblocage
  (clé durable `paquet-rouge-base:<test>`) et préviennent le pilote ;
* `packet_red.mark_base_red` classe le même rouge en CI tant que ce lot est
  ouvert, et `paquet-horaire.classify` ne retire aucune unité pour lui.
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

from ameesh import packet_red

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


auto = _load("test_paquet_auto_8262", ROOT / "scripts" / "paquet-auto.py")
hourly = _load("test_paquet_horaire_8262", ROOT / "scripts" / "paquet-horaire.py")

BASE = "4" * 40
OWN = "tests.test_propre_1.T.test_x"


def _red(label, modules, test, kind="FAIL"):
    return auto.TestRun(label, list(modules), 1,
                        "%s: test_x (%s)\nAssertionError\n" % (kind, test), 0.1)


def _green(label, modules):
    return auto.TestRun(label, list(modules), 0, "Ran 1 test in 0.1s\n\nOK", 0.1)


class OwnedBaseReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-8262-")
        self.addCleanup(self.tmp.cleanup)
        self.composition = auto.Composition(
            units=[auto.Unit(1, "a", "1" * 40), auto.Unit(2, "b", "2" * 40)],
            discarded=[], head="3" * 40)

    def check(self, results, at_base):
        self.replayed = []

        def owned_on_base(_worktree, base, modules, *, timeout):
            self.replayed.append((base, list(modules)))
            return at_base

        def fake_run(_worktree, label, modules, *, timeout):
            return results[label](modules)

        with mock.patch.object(auto, "unit_test_modules",
                               side_effect=lambda _w, unit:
                               ["tests.test_propre_%d" % unit.lot]), \
                mock.patch.object(auto, "contract_modules",
                                  return_value=["tests.test_contrat"]), \
                mock.patch.object(auto, "run_tests", side_effect=fake_run):
            return auto.check_local_tests(
                pathlib.Path(self.tmp.name), self.composition, timeout=1,
                log=lambda _line: None,
                attribute=lambda *_a: {"status": "ok", "tests": []},
                select=lambda *_a: ({}, "sans carte"), base_sha=BASE,
                on_base=lambda *_a, **_k: set(), owned_on_base=owned_on_base)

    def own_red(self, kind="FAIL"):
        return {"unité #1": lambda m: _red("unité #1", m, OWN, kind),
                "unité #2": lambda m: _green("unité #2", m),
                "contrats": lambda m: _green("contrats", m)}

    def test_rouge_propre_aussi_sur_la_base_ne_retire_rien(self) -> None:
        runs, faulty, removals = self.check(self.own_red(), {OWN: "FAIL"})
        self.assertEqual((faulty, removals), ([], []))
        self.assertEqual(self.replayed, [(BASE, ["tests.test_propre_1"])])
        self.assertEqual(runs[0].base_red, [OWN])
        self.assertTrue(runs[0].inconclusive)
        self.assertIn("rouge de la base", runs[0].motive())

    def test_classe_d_echec_differente_garde_le_retrait(self) -> None:
        _runs, faulty, _removals = self.check(self.own_red(), {OWN: "ERROR"})
        self.assertEqual(faulty, [1])

    def test_rejeu_non_concluant_garde_le_retrait(self) -> None:
        _runs, faulty, _removals = self.check(self.own_red(), None)
        self.assertEqual(faulty, [1])

    def test_noms_sans_classe_ne_prouvent_rien(self) -> None:
        _runs, faulty, _removals = self.check(self.own_red(), {OWN})
        self.assertEqual(faulty, [1])

    def test_vert_sur_la_base_retire_l_unite(self) -> None:
        _runs, faulty, _removals = self.check(self.own_red(), {})
        self.assertEqual(faulty, [1])

    def test_seul_le_rouge_de_la_base_est_ecarte(self) -> None:
        other = "tests.test_propre_2.T.test_x"
        _runs, faulty, _removals = self.check({
            "unité #1": lambda m: _red("unité #1", m, OWN),
            "unité #2": lambda m: _red("unité #2", m, other),
            "contrats": lambda m: _green("contrats", m),
        }, {OWN: "FAIL"})
        self.assertEqual(faulty, [2])
        self.assertEqual(self.replayed,
                         [(BASE, ["tests.test_propre_1", "tests.test_propre_2"])])

    def test_contrat_rouge_sur_la_base_n_arrete_pas(self) -> None:
        name = "tests.test_contrat.T.test_x"
        runs, faulty, _removals = self.check({
            "unité #1": lambda m: _green("unité #1", m),
            "unité #2": lambda m: _green("unité #2", m),
            "contrats": lambda m: _red("contrats", m, name),
        }, {name: "FAIL"})
        self.assertEqual(faulty, [])
        self.assertEqual(runs[-1].base_red, [name])

    def test_contrat_rouge_absent_de_la_base_arrete(self) -> None:
        with self.assertRaises(auto.CompositionRed):
            self.check({
                "unité #1": lambda m: _green("unité #1", m),
                "unité #2": lambda m: _green("unité #2", m),
                "contrats": lambda m: _red("contrats", m,
                                           "tests.test_contrat.T.test_x"),
            }, {})


class FailureKindsTest(unittest.TestCase):
    def test_classes_et_panne_d_environnement(self) -> None:
        output = ("FAIL: test_a (tests.test_m.T.test_a)\nAssertionError\n"
                  "======\n"
                  "ERROR: test_b (tests.test_m.T.test_b)\nTraceback:\n"
                  "ValueError: x\n======\n"
                  "ERROR: test_c (tests.test_m.T.test_c)\nTraceback:\n"
                  "ModuleNotFoundError: No module named 'psycopg'\n")
        self.assertEqual(auto.failure_kinds(output),
                         {"tests.test_m.T.test_a": "FAIL",
                          "tests.test_m.T.test_b": "ERROR"})


class BaseRedLotTest(unittest.TestCase):
    def call(self, outputs, sender=None):
        calls = []

        def fake(cmd, *, input_text=None, timeout=None, **_kw):
            calls.append((cmd, input_text))
            return subprocess.CompletedProcess(cmd, 0, outputs.pop(0).encode(), b"")

        with mock.patch.object(auto, "run_command", side_effect=fake):
            lot = auto.base_red_lot(OWN, name="h-test", carrier=9,
                                    base_sha=BASE, target="main",
                                    kind="FAIL", repo="manaty/ameesh",
                                    driver="psycopg", sender=sender,
                                    log=lambda _l: None)
        return lot, calls

    def test_ouvre_le_lot_avec_la_cle_durable(self) -> None:
        lot, calls = self.call(["lot #77 created in intake: Rouge de la base",
                                "noted"])
        self.assertEqual(lot, 77)
        self.assertEqual(len(calls), 2)
        cmd, body = calls[0]
        self.assertIn("paquet-rouge-base:" + OWN, cmd)
        self.assertNotIn("--app", cmd)
        self.assertEqual(cmd[cmd.index("--see-also") + 1], "9")
        self.assertIn(BASE, body)
        self.assertIn("psycopg", body)

    def test_estimation_presente_sous_un_agent(self) -> None:
        # B mty-amh-qa-01 : sous le tic, `--from mesh-design` est un agent
        # enregistré ; sans estimation, L157 refuse la création du lot.
        _lot, calls = self.call(["lot #77 created in intake: Rouge", "noted"],
                                sender="mesh-design")
        cmd = calls[0][0]
        self.assertEqual(cmd[cmd.index("--actor") + 1], "mesh-design")
        self.assertEqual(cmd[cmd.index("--estimate") + 1], "2h")
        self.assertIn("--estimate-source", cmd)

    def test_relance_un_lot_existant(self) -> None:
        lot, calls = self.call([
            "lot #77 already exists (unique-ref x, merged): Rouge", "noted"])
        self.assertEqual(lot, 77)
        self.assertEqual(calls[1][0][:4], ["ameesh", "work", "note", "77"])
        proof = calls[1][1]
        self.assertTrue(proof.startswith(auto.BASE_RED_PROOF))
        self.assertEqual(json.loads(proof[len(auto.BASE_RED_PROOF):]),
                         {"test": OWN, "kind": "FAIL", "base": BASE,
                          "repo": "manaty/ameesh", "driver": "psycopg",
                          "packet": "h-test", "carrier": 9})

    def test_preuve_meme_prefixe_que_la_lecture_ci(self) -> None:
        self.assertEqual(auto.BASE_RED_PROOF, packet_red.BASE_RED_PROOF)
        self.assertEqual(auto.BASE_RED_REF, packet_red.BASE_RED_REF)

    def test_depot_lu_a_l_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "-C", tmp, "remote", "add", "origin",
                            "git@github.com:Manaty/ameesh.git"], check=True)
            self.assertEqual(auto.repo_slug(pathlib.Path(tmp)), "manaty/ameesh")

    def test_courrier_au_pilote_nomme_les_lots(self) -> None:
        sent = []
        with mock.patch.object(auto, "send_mail",
                               side_effect=lambda *a, **_k: sent.append(a)):
            auto.notify_base_red("h-test", 9, [OWN], base_sha=BASE,
                                 target="main", log=lambda _l: None,
                                 open_lot=lambda *_a, **_k: 77)
        self.assertEqual(sent[0][0], "mesh-design")
        self.assertIn("%s → #77" % OWN, sent[0][2])


class CiReadingTest(unittest.TestCase):
    def entries(self):
        return [{"test": OWN, "status": "probable",
                 "units": [{"lot": 1, "best": 6, "evidence": []}], "more": 0},
                {"test": "tests.test_autre.T.test_y", "status": "probable",
                 "units": [{"lot": 2, "best": 6, "evidence": []}], "more": 0}]

    FAILURES = [{"test": OWN, "status": "echec"},
                {"test": "tests.test_autre.T.test_y", "status": "echec"}]

    def proof(self, **changes):
        row = {"test": OWN, "kind": "FAIL", "base": BASE,
               "repo": "manaty/ameesh", "carrier": 9}
        row.update(changes)
        return {"note": packet_red.BASE_RED_PROOF + json.dumps(row)}

    def mark(self, entries, rows, proofs=None, *, failures=None, base=BASE,
             repo="Manaty/ameesh"):
        if proofs is None:
            proofs = [self.proof()]
        with mock.patch.object(packet_red.work, "existing_by_unique_ref",
                               side_effect=lambda _db, key: rows.get(key)), \
                mock.patch.object(packet_red.work, "events_noted",
                                  return_value=proofs):
            return packet_red.mark_base_red(
                object(), entries,
                self.FAILURES if failures is None else failures,
                base=base, repo=repo)

    OPEN = {"paquet-rouge-base:" + OWN: {"id": 77, "state": "intake"}}

    def assert_not_marked(self, **kwargs) -> None:
        entries = self.entries()
        self.assertEqual(self.mark(entries, self.OPEN, **kwargs), 0)
        self.assertEqual(entries[0]["status"], "probable")

    def test_lot_ouvert_sans_preuve_ne_classe_rien(self) -> None:
        # B mty-amh-impl-11 : l'existence du suivi n'est pas une preuve
        self.assert_not_marked(proofs=[])

    def test_preuve_d_une_autre_base_ne_classe_rien(self) -> None:
        self.assert_not_marked(proofs=[self.proof(base="5" * 40)])

    def test_autre_classe_d_echec_ne_classe_rien(self) -> None:
        self.assert_not_marked(failures=[{"test": OWN, "status": "erreur"}])
        self.assert_not_marked(proofs=[self.proof(kind="ERROR")])

    def test_classe_absente_en_ci_ne_classe_rien(self) -> None:
        self.assert_not_marked(failures=[{"test": OWN}])

    def test_autre_depot_ou_base_inconnue_ne_classe_rien(self) -> None:
        self.assert_not_marked(proofs=[self.proof(repo="manaty/nexlink")])
        self.assert_not_marked(repo="")
        self.assert_not_marked(base="")
        self.assert_not_marked(base=BASE[:12])

    def test_lot_ouvert_classe_rouge_de_la_base(self) -> None:
        entries = self.entries()
        count = self.mark(entries, self.OPEN,
                          [self.proof(base="5" * 40), self.proof()])
        self.assertEqual(count, 1)
        self.assertEqual(entries[0]["base_proof"],
                         {"base": BASE, "kind": "FAIL", "carrier": 9})
        self.assertEqual((entries[0]["status"], entries[0]["units"],
                          entries[0]["base_lot"]), ("base", [], 77))
        self.assertEqual(entries[1]["status"], "probable")
        self.assertIn("rouge de la base (lot #77)",
                      packet_red.evidence_line(entries[0]))
        self.assertTrue(packet_red.note_of({"tests": entries})
                        .startswith("Rouge(s) de la base : %s (#77)" % OWN))

    def test_lot_livre_ne_classe_plus(self) -> None:
        entries = self.entries()
        merged = next(state for state in ("merged", "delivered", "closed")
                      if state not in packet_red.work.OPEN_STATES)
        self.assertEqual(self.mark(entries, {"paquet-rouge-base:" + OWN:
                                             {"id": 77, "state": merged}}), 0)
        self.assertEqual(entries[0]["status"], "probable")

    def test_classify_ne_retire_que_le_rouge_du_paquet(self) -> None:
        entries = self.entries()
        entries[0].update({"status": "base", "units": [], "base_lot": 77})
        run = {"status": "failure",
               "failures": [{"test": OWN, "part": "p1"},
                            {"test": "tests.test_autre.T.test_y", "part": "p2"}],
               "attribution": {"status": "ok", "tests": entries}}
        verdict = hourly.classify(run)
        self.assertTrue(verdict.deterministic)
        self.assertEqual(verdict.lots, [2])
        run["failures"] = run["failures"][:1]
        verdict = hourly.classify(run)
        self.assertFalse(verdict.deterministic)
        self.assertIn("rouge(s) de la base seulement", verdict.why)


if __name__ == "__main__":
    unittest.main()
