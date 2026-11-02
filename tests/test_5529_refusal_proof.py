# SPDX-License-Identifier: AGPL-3.0-only
"""#5529 (issue #4065, mechanism 2) — the refusal proof contract.

A turn whose harness exits non-zero with no stream line and no usage keeps its
account reservation `kept`: a configurable wrapper may have called the provider
and then failed silently (B cx1 on #5503). This lot adds an opt-in positive
proof, `ameesh-refusal/1`: the harness refuses the turn **before** any request,
writes a private proof document for this turn and exits with the reserved
code. The reservation is released only on that proof; everything else stays
`kept`.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from ameesh import harnesses, refusal, registry, storage
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase, SRC
from .test_1469_account_budget import OPENAI, _profile

REFUSE = "policy: document harness accepts only mail turns"


def _descriptor(ident: str, stream: str, contract=None):
    return types.SimpleNamespace(id=ident, stream=stream, refusal=contract or {})


class ContractTest(unittest.TestCase):
    """The contract, its qualification and the proof document itself."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="refusal-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def _record(self, harness="claude", turn="turn-1"):
        return refusal.prepare(self.tmp, harness, turn)

    def _write(self, record, document, mode=0o600):
        with open(record["path"], "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.chmod(record["path"], mode)

    def _valid(self, record, **changes):
        document = {"schema": refusal.SCHEMA, "harness": record["harness"],
                    "token": record["token"], "turn_id": record["turn_id"],
                    "reason": "no mail turn"}
        document.update(changes)
        return document

    def test_les_trois_descripteurs_livres_declarent_le_contrat(self):
        for ident in ("claude", "codex", "deepseek"):
            descriptor = harnesses.get(ident)
            self.assertIsNotNone(descriptor, ident)
            self.assertEqual(dict(descriptor.refusal),
                             {"schema": refusal.SCHEMA, "exit_code": refusal.DEFAULT_EXIT})
            self.assertEqual(harnesses.errors(
                harnesses.validate(descriptor.document, path=descriptor.path)), [])
            self.assertIsNotNone(refusal.contract(descriptor), ident)

    def test_seules_les_familles_qualifiees_portent_une_preuve(self):
        contract = {"schema": refusal.SCHEMA, "exit_code": 86}
        self.assertIsNotNone(refusal.contract(_descriptor("claude", "claude-stream-json", contract)))
        self.assertIsNotNone(refusal.contract(_descriptor("codex", "codex-json", contract)))
        self.assertIsNotNone(refusal.contract(_descriptor("deepseek", "dsh-json", contract)))
        # commande configurée (stream=text) : jamais qualifiée, même déclarée
        self.assertIsNone(refusal.contract(_descriptor("claude", "text", contract)))
        # un harnais inconnu ne porte pas le contrat
        self.assertIsNone(refusal.contract(_descriptor("custom", "claude-stream-json", contract)))
        # flux d'une autre famille
        self.assertIsNone(refusal.contract(_descriptor("codex", "claude-stream-json", contract)))

    def test_contrat_refuse_les_codes_reserves_ou_hors_bornes(self):
        for code in (0, 1.5, True, "86", None, 126, 127, 200):
            contract = {"schema": refusal.SCHEMA, "exit_code": code}
            self.assertIsNone(refusal.contract(_descriptor("claude", "claude-stream-json", contract)),
                              code)
        for schema in (None, "ameesh-refusal/2", 1):
            contract = {"schema": schema, "exit_code": 86}
            self.assertIsNone(refusal.contract(_descriptor("claude", "claude-stream-json", contract)),
                              schema)

    def test_validation_du_descripteur_signale_un_contrat_invalide(self):
        base = harnesses.get("claude").document
        broken = copy.deepcopy(base)
        broken["ameesh"]["refusal"] = {"schema": "nope", "exit_code": 0}
        findings = harnesses.validate(broken)
        codes = [f.code for f in findings]
        self.assertIn("harness-refusal-invalid", codes)
        self.assertTrue(harnesses.errors(findings))
        unknown = copy.deepcopy(base)
        unknown["ameesh"]["refusal"] = {"schema": refusal.SCHEMA, "exit_code": 86,
                                        "channel": "file"}
        self.assertIn("harness-key-unknown",
                      [f.code for f in harnesses.validate(unknown)])

    def test_squelette_prive_et_jeton_neuf_a_chaque_tour(self):
        first = self._record(turn="turn-1")
        self.assertTrue(refusal.opened(first))
        self.assertEqual(os.stat(first["path"]).st_mode & 0o777, 0o600)
        second = self._record(turn="turn-2")
        self.assertNotEqual(first["token"], second["token"])
        # le squelette du tour précédent ne vaut plus rien
        self.assertFalse(refusal.opened(first))
        self.assertTrue(refusal.opened(second))

    def test_emit_ecrit_la_preuve_du_tour(self):
        record = self._record()
        with mock.patch.dict(os.environ, {refusal.ENV: record["path"]}):
            self.assertEqual(refusal.main(["emit", "--reason", REFUSE]), 0)
            self.assertFalse(refusal.opened(record))
            self.assertEqual(refusal.proof(record), REFUSE)
            self.assertFalse(os.path.exists(record["path"]))

    def test_emit_refuse_un_motif_vide_ou_multiligne(self):
        record = self._record()
        with mock.patch.dict(os.environ, {refusal.ENV: record["path"]}):
            for reason in ("", "   ", "a\nb", "a" * (refusal.MAX_REASON + 1)):
                self.assertEqual(refusal.main(["emit", "--reason", reason]), 1, reason)
            self.assertTrue(refusal.opened(record))

    def test_emit_sans_squelette_ou_hors_mode_prive(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(refusal.main(["emit", "--reason", REFUSE]), 1)
        record = self._record()
        with mock.patch.dict(os.environ, {refusal.ENV: record["path"]}):
            os.chmod(record["path"], 0o644)
            self.assertEqual(refusal.main(["emit", "--reason", REFUSE]), 1)
            junk = os.path.join(self.tmp, "junk.json")
            with open(junk, "w", encoding="utf-8") as handle:
                json.dump({"schema": "other"}, handle)
            os.chmod(junk, 0o600)
            self.assertEqual(refusal.main(["emit", "--reason", REFUSE, "--file", junk]), 1)

    def test_preuve_invalide_par_jeton_harnais_cle_ou_motif(self):
        cases = (
            {"token": "f" * 64},
            {"harness": "codex"},
            {"turn_id": "another-turn"},
            {"schema": "ameesh-refusal/2"},
            {"reason": "   "},
            {"reason": "a\nb"},
            {"reason": "a" * (refusal.MAX_REASON + 1)},
            {"extra": "field"},
        )
        for change in cases:
            record = self._record()
            document = self._valid(record)
            document.update(change)
            self._write(record, document)
            self.assertIsNone(refusal.proof(record), change)

    def test_preuve_refusee_si_lien_ou_mode_ou_taille(self):
        record = self._record()
        self._write(record, self._valid(record), mode=0o644)
        self.assertIsNone(refusal.proof(record))

        record = self._record()
        self._write(record, self._valid(record))
        with open(record["path"], "a", encoding="utf-8") as handle:
            handle.write(" " * refusal.MAX_BYTES)
        self.assertIsNone(refusal.proof(record))

        record = self._record()
        os.unlink(record["path"])
        os.symlink("/etc/passwd", record["path"])
        self.assertIsNone(refusal.proof(record))
        self.assertFalse(os.path.exists(record["path"]))

    def test_deux_objets_json_ne_font_pas_une_preuve(self):
        record = self._record()
        document = self._valid(record)
        with open(record["path"], "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document) + "\n" + json.dumps(document))
        os.chmod(record["path"], 0o600)
        self.assertIsNone(refusal.proof(record))


class RefusalRunnerTest(PgTestCase):
    """The runner releases the reservation only on the qualified proof."""

    def setUp(self) -> None:
        super().setUp()
        for table in ("turn_costs", "account_reservations", "account_budgets",
                      "budget_events", "spend_pending"):
            self.db.execute("DELETE FROM %s" % table)
        storage.of(self.db).budgets.account_put(OPENAI, 500.0, actor="human:t")

    def _worker(self, name: str, harness: str = "claude", **cfg_kw):
        cfg = dataclasses.replace(
            self.cfg, budget_check_interval=0.0,
            accounts={ident: [{"name": "k1", "type": "api_key_env", "key_env": "K",
                               "billing_account": OPENAI, "check_credentials": False}]
                      for ident in ("claude", "codex", "deepseek")},
            **cfg_kw)
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, harness, cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _turn(self, worker, **env):
        current = os.environ.get("PYTHONPATH", "")
        values = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
                  "K": "sk-test",
                  # le harnais factice appelle `python -m ameesh refusal emit`
                  # depuis le dossier de travail de l'agent : chemin absolu
                  "PYTHONPATH": SRC + (os.pathsep + current if current else "")}
        values.update(env)
        with mock.patch.dict(os.environ, values, clear=False):
            worker._account = _profile(OPENAI, harness=worker.turn_harness())
            return worker.run_turn({"kind": "prompt", "prompt": "t", "ids": []})

    def _states(self, agent):
        return [row["state"] for row in self.db.query(
            "select state from account_reservations where agent = %s order by id",
            (agent,))]

    def _usage(self):
        return storage.of(self.db).budgets.account_usage(OPENAI)[0]

    def _assert_released(self, agent):
        self.assertEqual(self._states(agent), ["released"])
        self.assertEqual(self._usage()["kept"], 0.0)
        row = self.db.query(
            "select usd::float8 as usd, billing_account from turn_costs"
            " where agent = %s", (agent,))[0]
        self.assertEqual(row["billing_account"], OPENAI)
        self.assertEqual(row["usd"], 0.0)

    def _assert_kept(self, agent):
        self.assertEqual(self._states(agent), ["kept"])
        self.assertAlmostEqual(self._usage()["kept"], 2.0)

    def test_preuve_qualifiee_libere_la_reservation(self):
        worker = self._worker("preuve")
        self.assertFalse(self._turn(worker, AMEESH_TEST_REFUSE=REFUSE,
                                    AMEESH_TEST_REFUSE_PROOF="1"))
        self._assert_released("preuve")
        self.assertFalse(os.path.exists(os.path.join(worker.state_dir, refusal.FILE)))

    def test_les_trois_familles_peuvent_prouver_le_refus(self):
        for ident in ("claude", "codex", "deepseek"):
            worker = self._worker("fam-" + ident, harness=ident)
            self.assertFalse(self._turn(worker, AMEESH_TEST_REFUSE=REFUSE,
                                        AMEESH_TEST_REFUSE_PROOF="1"))
            self._assert_released("fam-" + ident)

    def test_sortie_silencieuse_reste_kept(self):
        """B cx1 : un wrapper peut appeler puis sortir 1 sans rien écrire."""
        worker = self._worker("silencieux")
        self.assertFalse(self._turn(worker, AMEESH_TEST_REFUSE=REFUSE,
                                    AMEESH_TEST_REFUSE_EXIT="1"))
        self._assert_kept("silencieux")

    def test_code_reserve_sans_marqueur_reste_kept(self):
        worker = self._worker("sans-marqueur")
        self.assertFalse(self._turn(worker, AMEESH_TEST_REFUSE=REFUSE,
                                    AMEESH_TEST_REFUSE_EXIT="86"))
        self._assert_kept("sans-marqueur")

    def test_signal_reste_kept(self):
        worker = self._worker("signale")
        self.assertFalse(self._turn(worker, AMEESH_TEST_KILL_SELF="1"))
        self._assert_kept("signale")

    def test_preuve_avec_un_code_non_reserve_reste_kept(self):
        worker = self._worker("mauvais-code")
        self.assertFalse(self._turn(worker, AMEESH_TEST_REFUSE=REFUSE,
                                    AMEESH_TEST_REFUSE_PROOF="1",
                                    AMEESH_TEST_REFUSE_EXIT="1"))
        self._assert_kept("mauvais-code")

    def test_preuve_avec_une_ligne_de_flux_reste_kept(self):
        worker = self._worker("avec-flux")
        self.assertFalse(self._turn(
            worker, AMEESH_TEST_REFUSE=REFUSE, AMEESH_TEST_REFUSE_PROOF="1",
            AMEESH_TEST_REFUSE_LINE=json.dumps(
                {"type": "system", "subtype": "init", "session_id": "s"})))
        self._assert_kept("avec-flux")

    def test_preuve_au_mauvais_jeton_reste_kept(self):
        worker = self._worker("faux-jeton")
        target = worker._path(refusal.FILE)
        self.assertFalse(self._turn(worker, AMEESH_TEST_REFUSE=REFUSE,
                                    AMEESH_TEST_REFUSE_PROOF_AT=target,
                                    AMEESH_TEST_REFUSE_EXIT="86"))
        self._assert_kept("faux-jeton")

    def test_marqueur_sur_une_commande_configuree_non_qualifiee_reste_kept(self):
        """Le cas de l'issue : une commande configurée `stream=text` imprime un
        marqueur. Le descripteur n'est pas qualifié : le runner ne prépare même
        pas le canal de preuve et la réservation reste kept."""
        worker = self._worker("non-qualifie")
        target = worker._path(refusal.FILE)
        package = harnesses.get("claude")
        configured = dataclasses.replace(package, stream="text")
        with mock.patch("ameesh.harnesses.get", return_value=configured):
            self.assertFalse(self._turn(worker, AMEESH_TEST_REFUSE=REFUSE,
                                        AMEESH_TEST_REFUSE_PROOF_AT=target,
                                        AMEESH_TEST_REFUSE_EXIT="86"))
        self._assert_kept("non-qualifie")
        self.assertTrue(os.path.exists(target))
