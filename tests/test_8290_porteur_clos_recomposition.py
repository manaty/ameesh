# SPDX-License-Identifier: AGPL-3.0-only
"""#8290 : un porteur clos n'est jamais recomposé, poussé ni armé ; une unité
déjà portée par un autre paquet ouvert n'entre pas dans un second.

Constat h-20261126-01 (26/11) : la recomposition du pilote (`recompose
--carrier 8286 --exclude 8284 --wait 40m`) part à 02:12 sur #8286 encore
ouvert ; le pilote le clôt abandonné à 02:13 ; `paquet-auto` continue ses
tests puis, à 02:40, pousse #8285, ouvre la PR #6524, gèle et demande la
ci-full sur le porteur clos — pendant que le tic compose h-20261126-02 avec
le même #8285. Témoins :

- paquet-auto relit le porteur dans la garde de chaque geste : clos après
  les tests, rien n'est poussé, aucune PR, aucun gel, aucune ci-full (rc 7) ;
- paquet-auto écarte un lot déjà membre d'un autre paquet ouvert, avec son
  motif au manifeste ;
- paquet-horaire relit le porteur avant d'écrire une tentative : clos, ni
  marque ni lancement ; le rc 7 est un arrêt propre même sans relecture.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.test_3807_paquet_auto import paquet_auto as auto
from tests.test_6538_porteur_clos_pendant_tic import FakeShell, hourly

OPEN = {"state": "qa", "close_reason": "", "carried": {}}
CLOSED = {"state": "closed", "close_reason": "abandoned", "carried": {}}


def proc(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out.encode(), b"")


class ReadCarrierTest(unittest.TestCase):
    def test_porteur_clos_refuse_tout_geste(self):
        out = json.dumps({"state": "closed", "close_reason": "abandoned",
                          "carried": {}})
        with mock.patch.object(auto, "run_command", return_value=proc(0, out)):
            with self.assertRaisesRegex(auto.CarrierClosed, "abandoned"):
                auto.check_carrier_open(pathlib.Path("."), 8286)

    def test_porteur_ouvert_laisse_passer(self):
        out = json.dumps({"state": "qa", "close_reason": "", "carried": {}})
        with mock.patch.object(auto, "run_command", return_value=proc(0, out)):
            auto.check_carrier_open(pathlib.Path("."), 8286)

    def test_lecture_impossible_n_autorise_rien(self):
        with mock.patch.object(auto, "run_command",
                               return_value=proc(1, "base absente")):
            with self.assertRaisesRegex(auto.PaquetAutoError, "illisible"):
                auto.check_carrier_open(pathlib.Path("."), 8286)

    def test_main_rend_le_code_porteur_clos(self):
        with mock.patch.object(auto, "compose_packet",
                               side_effect=auto.CarrierClosed("porteur #8286 clos")):
            rc = auto.main(["--name", "h-20261126-01", "--carrier", "8286"])
        self.assertEqual(rc, auto.CARRIER_CLOSED_RC)

    def test_unite_d_un_autre_paquet_ouvert_ecartee(self):
        out = json.dumps({"state": "qa", "close_reason": "",
                          "carried": {"8285": 8288}})
        candidates = [auto.Candidate(8285, "a" * 40), auto.Candidate(8287, "b" * 40)]
        with mock.patch.object(auto, "run_command", return_value=proc(0, out)) as run:
            kept, gone = auto.carried_elsewhere(pathlib.Path("."), 8286, candidates)
        self.assertEqual(run.call_args.args[0][-3:], ["8286", "8285", "8287"])
        self.assertEqual([candidate.lot for candidate in kept], [8287])
        [item] = gone
        self.assertEqual((item.lot, item.reason), (8285, auto.CARRIED_ELSEWHERE_REASON))
        self.assertIn("#8288", item.detail)


class ClosedDuringTestsTest(unittest.TestCase):
    """Témoin : porteur clos pendant les tests → aucune poussée ni PR."""

    def test_clos_apres_les_tests_rien_n_est_publie(self):
        tmp = tempfile.TemporaryDirectory(prefix="paquet-8290-")
        self.addCleanup(tmp.cleanup)
        worktree = pathlib.Path(tmp.name) / "wt"
        (worktree / "scripts").mkdir(parents=True)
        (worktree / "scripts" / "paquet-compose.py").write_text("", encoding="utf-8")
        composition = auto.Composition(units=[auto.Unit(8285, "a" * 40, "b" * 40)],
                                       discarded=[], head="c" * 40)
        green = auto.TestRun("unité #8285", ["tests.test_x"], 0, "Ran 1 test\nOK", 0.1)
        args = argparse.Namespace(
            repo=tmp.name, name="h-20261126-01", target="main", carrier=8286,
            base=None, local=False, no_packet_record=False, keep_worktree=False,
            keep_failed_hours=24.0, force=True, tests_timeout=10, tests_parallel=1,
            pilot="mesh-design", sender="mesh-design", exclude=[8284],
            exclude_reason=[])
        tested: list[bool] = []

        def tests(*_args, **_kwargs):
            tested.append(True)
            return [green], [], []

        patches = dict(
            validate_target=None, validate_branch=None,
            fresh_base="e" * 40, remote_branch=None, ensure_carrier_branch=None,
            clear_local_branch=None, prune_stale_worktrees=[], make_worktree=worktree,
            reader_worktree=worktree / "reader",
            compute_candidates=([auto.Candidate(8285, "a" * 40)], {"ready": [8285]}),
            compose_candidates=composition, run_freeze_checks=(0, "ok", 0.1),
            contract_budget=10,
            prepublication_contracts=auto.ContractPass("d" * 40, "e" * 40, green),
            verify_contract_scope=None, check_epoch=None, check_pause=None,
            composition_table="## Composition\n", write_composition="d" * 40,
            record_packet="", run_inclusion=(0, "ok"), pull_request_state=None,
            ensure_pull_request=(7, "http://pr"), freeze_carrier="",
            request_full_ci="", note_carrier="", send_mail=None, push_branch=None,
            remove_worktree=None, PackageLock=mock.DEFAULT,
            CompositionClaim=mock.DEFAULT, keep_tested=None)
        with mock.patch.multiple(auto, **{k: mock.DEFAULT for k in patches}) as mocks, \
                mock.patch.object(auto, "check_local_tests", side_effect=tests), \
                mock.patch.object(auto, "read_carrier",
                                  side_effect=lambda *_a, **_k:
                                  dict(CLOSED if tested else OPEN)):
            for key, value in patches.items():
                mocks[key].return_value = value
            mocks["PackageLock"].return_value = mock.MagicMock()
            mocks["CompositionClaim"].return_value = mock.MagicMock()
            with self.assertRaises(auto.CarrierClosed):
                auto.compose_packet(args)
        self.assertTrue(tested)
        for gesture in ("push_branch", "ensure_pull_request", "freeze_carrier",
                        "request_full_ci", "keep_tested"):
            mocks[gesture].assert_not_called()


class HoraireClosedCarrierTest(unittest.TestCase):
    def context(self, lot: dict | None) -> hourly.Context:
        self.shell = FakeShell(lot)
        return hourly.Context(
            repo=pathlib.Path("."), target="main", project="ameesh",
            pilot="mesh-design", actor="mesh-design", shell=self.shell,
            python="python3")

    def test_tentative_reprise_sur_porteur_clos_sans_marque_ni_lancement(self):
        ctx = self.context({"id": 8286, "state": "closed", "close_reason": "abandoned"})
        with mock.patch.object(hourly, "composition_in_progress", return_value=""), \
                mock.patch.object(hourly, "carrier_composition", return_value=""), \
                mock.patch.object(hourly, "paquet_auto") as launched:
            got = hourly.attempt(ctx, 8286, "h-20261126-01", hourly.Attempt(
                hourly.RECOMPOSITION, 1, "", run_id=hourly.PILOT_RUN, exclude=[8284]))
        self.assertIn("porteur clos (abandoned)", got)
        launched.assert_not_called()
        self.assertFalse([call for call in self.shell.calls
                          if call[:3] == ["ameesh", "work", "note"]])

    def test_porteur_ouvert_lance_la_tentative(self):
        ctx = self.context({"id": 8286, "state": "qa"})
        with mock.patch.object(hourly, "composition_in_progress", return_value=""), \
                mock.patch.object(hourly, "carrier_composition", return_value=""), \
                mock.patch.object(hourly, "paquet_auto",
                                  return_value=hourly.Result(0)) as launched, \
                mock.patch.object(hourly, "after_compose", return_value="ok"):
            hourly.attempt(ctx, 8286, "h-20261126-01", hourly.Attempt(
                hourly.RECOMPOSITION, 1, "", run_id=hourly.PILOT_RUN, exclude=[8284]))
        launched.assert_called_once()

    def test_rc_porteur_clos_arret_propre_meme_illisible(self):
        ctx = self.context(None)
        got = hourly.after_compose(
            ctx, 8286, "h-20261126-01",
            hourly.Result(auto.CARRIER_CLOSED_RC, "", "porteur #8286 clos\n"),
            hourly.Attempt(hourly.RECOMPOSITION, 1, ""))
        self.assertIn("arrêt propre", got)
        self.assertFalse([call for call in self.shell.calls
                          if call[0] == "agent-mail"
                          or call[:3] == ["ameesh", "work", "note"]])


if __name__ == "__main__":
    unittest.main()
