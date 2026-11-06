# SPDX-License-Identifier: AGPL-3.0-only
"""#6005 — attribution d'appelant des tours hors lot (manaty/ameesh#4592).

Un tour peut porter une petite attribution JSON fournie par l'appelant
(`{source, environment, run_type, run_id, repo, issue_number, pr_number,
run_kind}`), bornée et à clés allow-listées. Elle est écrite sur la ligne
`turn_costs`, à côté de `work_item_id`, et n'est **jamais** une admission, un
budget ou une autorisation. Le rapport par période regroupe le coût par clé et
montre toujours le reste non attribué.

Les tests de base tournent sur le schéma jetable de `PgTestCase` ; les tests de
validation et de rapport sont purs.
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from ameesh import cli, cost as cost_mod, cost_attribution as ca, db as db_mod, mail, mesh_cli, registry, storage
from ameesh.backend import PgBackend
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


class NormalizeTest(unittest.TestCase):
    def test_closed_keys_and_bounded_values(self):
        got = ca.normalize({"source": " ima ", "run_type": "builder",
                            "issue_number": "1679", "pr_number": 12})
        self.assertEqual(got, {"source": "ima", "run_type": "builder",
                               "issue_number": 1679, "pr_number": 12})
        self.assertIsNone(ca.normalize(None))
        self.assertIsNone(ca.normalize({}))
        self.assertIsNone(ca.normalize({"source": "  "}))

    def test_unknown_key_is_refused(self):
        with self.assertRaises(ca.AttributionError):
            ca.normalize({"source": "ima", "secret": "x"})
        with self.assertRaises(ca.AttributionError):
            ca.normalize({"source": 3})
        with self.assertRaises(ca.AttributionError):
            ca.normalize({"source": "a" * (ca.MAX_TEXT + 1)})
        with self.assertRaises(ca.AttributionError):
            ca.normalize({"source": "a\nb"})

    def test_numbers_are_positive_integers(self):
        for value in (0, -1, True, 1.5, "x"):
            with self.subTest(value=value), self.assertRaises(ca.AttributionError):
                ca.normalize({"issue_number": value})

    def test_size_is_bounded(self):
        # 128 caractères par clé texte, mais des caractères de plusieurs
        # octets : le bornage porte sur les octets sérialisés, pas seulement
        # sur le nombre de caractères.
        big = {key: "é" * ca.MAX_TEXT for key in ca.TEXT_KEYS}
        big["issue_number"] = 1
        with self.assertRaises(ca.AttributionError):
            ca.normalize(big)

    def test_parse_json(self):
        self.assertEqual(ca.parse('{"source":"ima"}'), {"source": "ima"})
        with self.assertRaises(ca.AttributionError):
            ca.parse("not json")


class FromMessagesTest(unittest.TestCase):
    @staticmethod
    def _row(payload):
        return {"id": 1, "payload": payload}

    def test_one_attribution(self):
        rows = [self._row({"attribution": {"source": "ima"}}),
                self._row({})]
        self.assertEqual(ca.from_messages(rows), {"source": "ima"})

    def test_several_identical(self):
        rows = [self._row({"attribution": {"source": "ima", "run_id": "r1"}}),
                self._row({"attribution": {"source": "ima", "run_id": "r1"}})]
        self.assertEqual(ca.from_messages(rows), {"source": "ima", "run_id": "r1"})

    def test_two_different_is_ambiguous_never_guessed(self):
        rows = [self._row({"attribution": {"source": "ima", "run_id": "r1"}}),
                self._row({"attribution": {"source": "ima", "run_id": "r2"}})]
        self.assertIsNone(ca.from_messages(rows))

    def test_invalid_is_ignored(self):
        rows = [self._row({"attribution": {"unknown": "x"}}),
                self._row({"attribution": {"source": "ima"}})]
        self.assertEqual(ca.from_messages(rows), {"source": "ima"})


class ReportTest(unittest.TestCase):
    def _rows(self):
        return [
            {"agent": "a", "usd": 2.0, "recorded_ts": 10.0,
             "attribution": {"source": "ima", "run_type": "builder",
                             "issue_number": 1}},
            {"agent": "a", "usd": 1.0, "recorded_ts": 11.0,
             "attribution": {"source": "ima", "run_type": "qa",
                             "issue_number": 1}},
            {"agent": "a", "usd": 4.0, "recorded_ts": 12.0, "attribution": None},
            {"agent": "a", "usd": 8.0, "recorded_ts": 13.0,
             "attribution": {"source": "ima"}, "void_reason": "duplicate"},
        ]

    def test_groups_and_unattributed_remainder(self):
        data = ca.report(self._rows(), keys=("source", "run_type"))
        self.assertEqual(data["total"], {"turns": 3, "usd": 7.0})
        self.assertEqual(data["unattributed"], {"turns": 1, "usd": 4.0})
        values = [group["values"] for group in data["groups"]]
        self.assertEqual(values, [{"source": "ima", "run_type": "builder"},
                                  {"source": "ima", "run_type": "qa"}])

    def test_until_bound_excludes_late_rows(self):
        data = ca.report(self._rows(), keys=("source",), until_ts=12.0)
        self.assertEqual(data["total"], {"turns": 2, "usd": 3.0})

    def test_partial_attributions_with_equal_cost_and_turns_are_ordered(self):
        """B1 : deux groupes partiels de même coût et même nombre de tours ne
        comparent jamais `str` avec `None` (TypeError, rapport indisponible)."""
        rows = [
            {"agent": "a", "usd": 1.0, "recorded_ts": 1.0,
             "attribution": {"source": "ima"}},
            {"agent": "a", "usd": 1.0, "recorded_ts": 2.0,
             "attribution": {"run_type": "builder"}},
        ]
        data = ca.report(rows, keys=("source", "run_type"))
        self.assertEqual(data["total"], {"turns": 2, "usd": 2.0})
        self.assertEqual([group["values"] for group in data["groups"]],
                         [{"source": None, "run_type": "builder"},
                          {"source": "ima", "run_type": None}])

    def test_mixed_number_and_text_values_are_ordered(self):
        """B1 : l'ordre reste total quand deux groupes partiels mêlent un
        texte et un entier, au lieu de lever une TypeError."""
        rows = [
            {"agent": "a", "usd": 1.0, "recorded_ts": 1.0,
             "attribution": {"source": "ima"}},
            {"agent": "a", "usd": 1.0, "recorded_ts": 2.0,
             "attribution": {"issue_number": 7}},
        ]
        data = ca.report(rows, keys=("source", "issue_number"))
        self.assertEqual([group["values"] for group in data["groups"]],
                         [{"source": None, "issue_number": 7},
                          {"source": "ima", "issue_number": None}])

    def test_unknown_group_key_is_refused(self):
        with self.assertRaises(ca.AttributionError):
            ca.report(self._rows(), keys=("nope",))

    def test_format_shows_unattributed(self):
        text = ca.format_report(ca.report(self._rows(), keys=("source",)))
        self.assertIn("unattributed", text)
        self.assertIn("total", text)


#: `pending_spend.put_get` de la version N-1 (main@782080628c28), avant
#: #6005 : l'ON CONFLICT remplace l'identité du marqueur mais ne connaît ni
#: `attribution` ni `attribution_at`.
_OLD_PENDING_SPEND_PUT_GET = """
INSERT INTO spend_pending (agent, start_index, turn, model, work_item_id,
                           turn_id, spend_key, created_at)
VALUES (%s, %s, %s, %s, %s, %s,
        %s || ':' || %s || ':' ||
        to_char(now() at time zone 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        now())
ON CONFLICT (agent) DO UPDATE SET
    start_index  = excluded.start_index,
    turn         = excluded.turn,
    model        = excluded.model,
    work_item_id = excluded.work_item_id,
    turn_id      = excluded.turn_id,
    spend_key    = excluded.spend_key,
    resident_usage = null,
    created_at   = now()
"""


class StorageTest(PgTestCase):
    def test_migration_adds_both_columns(self):
        rows = self.db.query(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema()"
            " AND table_name IN ('turn_costs', 'spend_pending')"
            " AND column_name = 'attribution' ORDER BY table_name")
        self.assertEqual([(r["table_name"]) for r in rows],
                         ["spend_pending", "turn_costs"])

    def test_migration_adds_the_marker_identity_for_attribution(self):
        rows = self.db.query(
            "SELECT table_name, data_type FROM information_schema.columns"
            " WHERE table_schema = current_schema()"
            " AND table_name = 'spend_pending' AND column_name = 'attribution_at'")
        self.assertEqual([(r["table_name"], r["data_type"]) for r in rows],
                         [("spend_pending", "timestamp with time zone")])

    def _insert(self, attribution, *, agent="attributed", usd=1.25):
        storage.of(self.db).turn_costs.insert(
            agent=agent, harness="deepseek", turn="t1", model="deepseek-flash",
            session="s1", usd=usd, input_tokens=10, cached_input_tokens=0,
            output_tokens=5, cum_usd=None, cum_input_tokens=None,
            cum_cached_input_tokens=None, cum_output_tokens=None,
            attribution=attribution)

    def test_insert_writes_the_attribution_beside_the_lot(self):
        self._insert({"source": "ima", "run_type": "builder"}, usd=2.0)
        row = storage.of(self.db).turn_costs.ledger()[-1]
        self.assertEqual(row["attribution"],
                         {"source": "ima", "run_type": "builder"})
        work_item = self.db.query(
            "SELECT work_item_id FROM turn_costs ORDER BY id DESC LIMIT 1")[0]
        self.assertIsNone(work_item["work_item_id"])

    def test_ledger_reads_an_old_writer_row(self):
        self.previous_insert(
            "turn_costs",
            {"agent": "old", "harness": "deepseek", "usd": 1.0},
            added=("attribution",))
        row = storage.of(self.db).turn_costs.ledger()[-1]
        self.assertIsNone(row["attribution"])

    def test_operations_turns_exposes_attribution(self):
        self._insert({"source": "ima"})
        rows = storage.of(self.db).operations.turns(
            agent="attributed", since_s=3600, limit=10)
        self.assertEqual(rows[0]["attribution"], {"source": "ima"})

    def test_marker_carries_and_returns_attribution(self):
        attribution = {"source": "ima", "run_id": "r1", "issue_number": 7}
        self.assertTrue(registry.pending_spend_put(
            self.db, "attributed", 0, "tour", "", attribution=attribution))
        marker = registry.pending_spend_get(self.db, "attributed")
        self.assertEqual(marker["attribution"], attribution)
        registry.pending_spend_clear(self.db, "attributed")

    def test_marker_reads_an_old_writer_row(self):
        self.previous_insert(
            "spend_pending",
            {"agent": "attributed", "start_index": 0, "turn": "tour", "model": ""},
            added=("attribution", "attribution_at"))
        marker = registry.pending_spend_get(self.db, "attributed")
        self.assertIsNotNone(marker)
        self.assertIsNone(marker["attribution"])
        registry.pending_spend_clear(self.db, "attributed")

    def test_new_writer_replacement_replaces_or_clears_attribution(self):
        self.assertTrue(registry.pending_spend_put(
            self.db, "rewritten", 0, "tour-a", "",
            attribution={"source": "ima"}))
        self.assertTrue(registry.pending_spend_put(
            self.db, "rewritten", 1, "tour-b", ""))
        self.assertIsNone(registry.pending_spend_get(
            self.db, "rewritten")["attribution"])
        self.assertTrue(registry.pending_spend_put(
            self.db, "rewritten", 2, "tour-c", "",
            attribution={"source": "builder"}))
        self.assertEqual(registry.pending_spend_get(
            self.db, "rewritten")["attribution"], {"source": "builder"})
        registry.pending_spend_clear(self.db, "rewritten")

    def test_old_writer_replacement_does_not_lend_its_attribution(self):
        """N-1 : l'`ON CONFLICT` de 782080628 remplace le marqueur sans
        `attribution` ; le nouveau lecteur ne doit pas hériter celle du
        marqueur précédent pour le tour suivant."""
        self.assertTrue(registry.pending_spend_put(
            self.db, "replaced", 0, "tour-a", "",
            attribution={"source": "ima", "run_id": "r-a"}))
        self.previous_write(
            _OLD_PENDING_SPEND_PUT_GET,
            ("replaced", 1, "tour-b", "", None, None, "replaced", 1),
            added=("attribution", "attribution_at"))
        marker = registry.pending_spend_get(self.db, "replaced")
        self.assertEqual(marker["start"], 1)
        self.assertEqual(marker["turn"], "tour-b")
        self.assertIsNone(marker["attribution"])
        registry.pending_spend_clear(self.db, "replaced")

    def test_cli_report_groups_and_shows_unattributed(self):
        self._insert({"source": "ima", "run_type": "builder"}, usd=2.0)
        self._insert(None, usd=1.0)
        args = argparse.Namespace(since="24h", until=None, by="source,run_type",
                                  agent=None, json=True)
        out = io.StringIO()
        with redirect_stdout(out):
            code = mesh_cli._cost_attribution(self.db, args)
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["schema"], ca.SCHEMA)
        self.assertEqual(data["total"], {"turns": 2, "usd": 3.0})
        self.assertEqual(data["unattributed"], {"turns": 1, "usd": 1.0})

    def test_cli_report_refuses_an_unknown_group_key(self):
        args = argparse.Namespace(since="24h", until=None, by="nope",
                                  agent=None, json=False)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = mesh_cli._cost_attribution(self.db, args)
        self.assertEqual(code, 2)
        self.assertIn("unknown group key", stderr.getvalue())

    def test_cli_report_bounds_the_period(self):
        """`ledger(since_ts=…)` veut un instant : une ligne de deux jours ne
        doit pas entrer dans la fenêtre de 24 h."""
        self._insert({"source": "ima"}, usd=2.0)
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, usd, recorded_at, attribution)"
            " VALUES ('attributed', 'deepseek', 5.0, now() - interval '2 days',"
            " '{\"source\": \"old\"}'::jsonb)")
        args = argparse.Namespace(since="24h", until=None, by="source",
                                  agent=None, json=True)
        out = io.StringIO()
        with redirect_stdout(out):
            code = mesh_cli._cost_attribution(self.db, args)
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["total"], {"turns": 1, "usd": 2.0})
        self.assertEqual([g["values"] for g in data["groups"]], [{"source": "ima"}])


class MailTest(PgTestCase):
    def test_send_refuses_an_invalid_attribution(self):
        registry.upsert(self.db, "sender", status="idle", harness="codex")
        registry.upsert(self.db, "pilot", status="idle", harness="claude")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.cmd_send(PgBackend(self.cfg, self.db), self.cfg,
                                ["pilot", "body", "--from", "sender",
                                 "--attribution", '{"unknown": 1}'])
        self.assertEqual(code, 2)
        self.assertIn("--attribution", stderr.getvalue())
        self.assertEqual(mail.unread(self.db, "pilot"), [])

    def test_send_stores_the_attribution_in_the_payload(self):
        registry.upsert(self.db, "sender", status="idle", harness="codex")
        registry.upsert(self.db, "pilot", status="idle", harness="claude")
        self.assertEqual(cli.cmd_send(
            PgBackend(self.cfg, self.db), self.cfg,
            ["pilot", "body", "--from", "sender", "--attribution",
             '{"source": "ima", "environment": "staging", "issue_number": 1679}']), 0)
        message = mail.unread(self.db, "pilot")[0]
        self.assertEqual(message["payload"]["attribution"],
                         {"source": "ima", "environment": "staging",
                          "issue_number": 1679})


class TurnTest(PgTestCase):
    """Le spec du tour porte l'attribution jusqu'au marqueur et au grand livre."""

    def _worker(self, name="attributed"):
        cfg = dataclasses.replace(self.cfg, budget_usd_per_hour=10.0,
                                  budget_check_interval=0.0)
        runner = Runner(cfg, self.db, once=True)
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM spend_pending WHERE agent = %s", (name,))
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "deepseek", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        return runner, AgentWorker(runner, registry.get(self.db, name), lease)

    def test_courrier_spec_carries_the_mail_attribution(self):
        runner, worker = self._worker()
        registry.upsert(self.db, "sender", status="idle", harness="codex")
        mail.send(self.db, "sender", "attributed", "build something",
                  payload={"attribution": {"source": "ima", "run_type": "builder"}})
        rows = mail.unread(self.db, "attributed")
        try:
            spec = worker._courrier_spec("mail", rows)
        finally:
            worker.watchdog_stop.set()
        self.assertEqual(spec["attribution"],
                         {"source": "ima", "run_type": "builder"})

    def test_turn_writes_the_attribution(self):
        runner, worker = self._worker()
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        attribution = {"source": "ima", "environment": "staging",
                       "run_type": "builder", "run_id": "r-1",
                       "repo": "thepackengers/issue-manager-agent",
                       "issue_number": 1679, "run_kind": "fix"}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "tour", "ids": [],
                     "attribution": attribution}))
        finally:
            worker.watchdog_stop.set()
        row = self.db.query(
            "SELECT attribution, work_item_id FROM turn_costs"
            " WHERE agent = 'attributed' ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(row["attribution"], attribution)
        self.assertIsNone(row["work_item_id"])

    def test_repair_writes_the_marker_attribution(self):
        runner, worker = self._worker()
        attribution = {"source": "ima", "run_id": "r-2"}
        self.assertTrue(worker._compta_marque(0, "tour", "",
                                              attribution=attribution))
        # un autre worker répare (même bail, autre session)
        worker2 = AgentWorker(runner, registry.get(self.db, "attributed"), {
            "lease_epoch": worker.epoch,
            "lease_expires_ts": time.time() + 3600,
        })
        worker2._compta_termine()
        row = self.db.query(
            "SELECT attribution FROM turn_costs WHERE agent = 'attributed'"
            " ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(row["attribution"], attribution)
        self.assertIsNone(registry.pending_spend_get(self.db, "attributed"))
        worker.watchdog_stop.set()

    def test_repair_after_an_old_writer_replacement_has_no_inherited_attribution(self):
        """N-1 : la réparation d'un marqueur remplacé par l'ancien writer ne
        doit pas écrire sur le grand livre l'attribution du tour précédent."""
        runner, worker = self._worker()
        self.assertTrue(worker._compta_marque(
            0, "tour-a", "", attribution={"source": "ima", "run_id": "r-a"}))
        self.previous_write(
            _OLD_PENDING_SPEND_PUT_GET,
            ("attributed", 1, "tour-b", "", None, None, "attributed", 1),
            added=("attribution", "attribution_at"))
        # un autre worker répare (même bail, autre session)
        worker2 = AgentWorker(runner, registry.get(self.db, "attributed"), {
            "lease_epoch": worker.epoch,
            "lease_expires_ts": time.time() + 3600,
        })
        worker2._compta_termine()
        row = self.db.query(
            "SELECT attribution, turn FROM turn_costs WHERE agent = 'attributed'"
            " ORDER BY id DESC LIMIT 1")[0]
        self.assertIsNone(row["attribution"])
        self.assertIsNone(registry.pending_spend_get(self.db, "attributed"))
        worker.watchdog_stop.set()


class MediatedCompatTest(unittest.TestCase):
    """Un serveur médié d'avant la 1.12 perd l'attribution, jamais le tour."""

    def _book(self):
        return cost_mod.CostBook(
            state_dir=tempfile.mkdtemp(prefix="ameesh-6005-"), db=object())

    def test_attribution_is_omitted_when_none(self):
        calls = []
        fake = SimpleNamespace(turn_costs=SimpleNamespace(
            insert=lambda **kw: calls.append(dict(kw))))
        with mock.patch.object(cost_mod.storage, "of", return_value=fake):
            self._book()._insert_turn_cost(agent="a1", spend_key="k")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("attribution", calls[0])

    def test_old_server_bad_args_retries_without_attribution(self):
        calls = []

        def insert(**kw):
            calls.append(dict(kw))
            if "attribution" in kw:
                raise db_mod.DbError(
                    "bad_args (400) : turn_costs.insert : paramètre inconnu attribution")

        fake = SimpleNamespace(turn_costs=SimpleNamespace(insert=insert))
        with mock.patch.object(cost_mod.storage, "of", return_value=fake):
            self._book()._insert_turn_cost(
                agent="a1", spend_key="k",
                attribution={"source": "ima"})
        self.assertEqual(len(calls), 2)
        self.assertNotIn("attribution", calls[1])

    def test_a_real_failure_is_not_swallowed(self):
        def insert(**kw):
            raise db_mod.DbError("connexion perdue")

        fake = SimpleNamespace(turn_costs=SimpleNamespace(insert=insert))
        with mock.patch.object(cost_mod.storage, "of", return_value=fake):
            with self.assertRaises(db_mod.DbError):
                self._book()._insert_turn_cost(
                    agent="a1", attribution={"source": "ima"})


class MarkerCompatTest(unittest.TestCase):
    """B2 : un serveur d'avant la 1.12 ne doit jamais empêcher le marqueur.

    Le client médié transmet le kwarg tel quel : `attribution=None` est un
    paramètre inconnu pour un serveur 1.11, et le marqueur d'un tour ordinaire
    ne serait jamais posé. Le paramètre absent n'est donc pas transmis ; s'il
    est refusé alors qu'une attribution existe, le marqueur est reposé sans
    elle (donnée seule, jamais une condition du tour).
    """

    def _row(self, name="a"):
        return {"agent": name, "start_index": 0, "turn": "tour", "model": "",
                "created_at": "2026-11-06T00:00:00.000000Z", "created_ts": 0.0,
                "spend_key": None, "ledger_key": None, "ledger_ambiguous": False,
                "work_item_id": None, "turn_id": None, "attribution": None,
                "resident_usage": None}

    def _old_api(self, error, *, put_get=False):
        calls = []

        def write(*args, **kwargs):
            calls.append(dict(kwargs))
            if "attribution" in kwargs:
                raise error
            return True

        def write_get(*args, **kwargs):
            write(*args, **kwargs)
            return self._row()

        api = SimpleNamespace(put=write, get=lambda name: self._row(name))
        if put_get:
            api.put_get = write_get
        return api, calls

    def _run(self, api, method, attribution):
        fake = SimpleNamespace(pending_spend=api)
        with mock.patch.object(registry.storage, "of", return_value=fake), \
                mock.patch("ameesh.agent_names.resolve",
                           side_effect=lambda name, **kw: name):
            if method == "put":
                return registry.pending_spend_put(
                    None, "a", 0, "tour", "", attribution=attribution)
            return registry.pending_spend_begin(
                None, "a", 0, "tour", "", attribution=attribution)

    def test_put_without_attribution_omits_the_parameter(self):
        api, calls = self._old_api(TypeError(
            "put() got an unexpected keyword argument 'attribution'"))
        self.assertTrue(self._run(api, "put", None))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("attribution", calls[0])

    def test_put_with_attribution_falls_back(self):
        api, calls = self._old_api(db_mod.DbError(
            "bad_args (400) : pending_spend.put : paramètre inconnu attribution"))
        self.assertTrue(self._run(api, "put", {"source": "ima"}))
        self.assertEqual(len(calls), 2)
        self.assertNotIn("attribution", calls[1])

    def test_begin_without_attribution_omits_the_parameter(self):
        api, calls = self._old_api(TypeError(
            "put() got an unexpected keyword argument 'attribution'"))
        marker = self._run(api, "begin", None)
        self.assertEqual(marker["agent"], "a")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("attribution", calls[0])

    def test_begin_with_attribution_falls_back(self):
        api, calls = self._old_api(db_mod.DbError(
            "bad_args (400) : pending_spend.put : paramètre inconnu attribution"))
        marker = self._run(api, "begin", {"source": "ima"})
        self.assertEqual(marker["agent"], "a")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("attribution", calls[1])

    def test_put_get_without_attribution_omits_the_parameter(self):
        api, calls = self._old_api(TypeError(
            "put_get() got an unexpected keyword argument 'attribution'"),
            put_get=True)
        marker = self._run(api, "begin", None)
        self.assertEqual(marker["agent"], "a")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("attribution", calls[0])

    def test_put_get_with_attribution_falls_back(self):
        api, calls = self._old_api(db_mod.DbError(
            "bad_args (400) : pending_spend.put_get : paramètre inconnu attribution"),
            put_get=True)
        marker = self._run(api, "begin", {"source": "ima"})
        self.assertEqual(marker["agent"], "a")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("attribution", calls[1])


if __name__ == "__main__":
    unittest.main()
