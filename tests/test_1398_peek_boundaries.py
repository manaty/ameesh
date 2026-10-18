# SPDX-License-Identifier: AGPL-3.0-only
"""#1398: peek refusals, empty-reader non-disclosure and reader bounds."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ameesh import agent_peek as peek, decisions, registry, storage
from tests.support import PgTestCase
from tests import test_l108_executor_server as _exec_server
from tests.test_l108_executor_server import OWNER


class EmptyReaderTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "peek-worker", harness="codex", host="remote-host", mode="execute")
        self.db.execute("UPDATE agent_registry SET responsible='human:owner',"
                        " lease_owner='runner', lease_epoch=7,"
                        " lease_expires_at=now()+interval '5 minutes'"
                        " WHERE name='peek-worker'")
        self.store = storage.of(self.db).operations
        self.payload = {"workspace": {"branch": "work", "diff_stat": "", "available": True},
                        "events": [{"kind": "message", "summary": "owner only"}]}

    def test_empty_reader_or_responsible_selects_no_snapshot(self):
        self.assertTrue(self.store.publish_peek("peek-worker", "runner", 7, self.payload))
        self.assertIsNone(self.store.read_peek("peek-worker", ""))
        with self.assertRaises(PermissionError):
            peek.snapshot(self.db, "peek-worker", human="")
        # An empty declared responsible must not match an empty reader: the
        # two empty strings never authorise a snapshot (N4).
        registry.upsert(self.db, "orphan", harness="codex", host="remote-host", mode="execute")
        self.db.execute("UPDATE agent_registry SET responsible='', lease_owner='runner',"
                        " lease_epoch=1, lease_expires_at=now()+interval '5 minutes'"
                        " WHERE name='orphan'")
        self.assertTrue(self.store.publish_peek("orphan", "runner", 1, self.payload))
        self.assertIsNone(self.store.read_peek("orphan", ""))
        self.assertIsNone(self.store.read_peek("orphan", "human:owner"))
        with self.assertRaises(PermissionError):
            peek.snapshot(self.db, "orphan", human="")
        stored = self.db.query("SELECT count(*) AS n FROM agent_peek WHERE agent='orphan'")
        self.assertEqual(stored[0]["n"], 1)


class NameFenceTest(_exec_server.ServeurExecTest):
    FENCE = ("inge-front", OWNER, 42)

    def test_name_must_equal_the_fence_agent_before_any_write(self):
        payload = {"events": [{"kind": "message", "summary": "must not land"}]}
        response = self.op("operations.publish_peek",
                           ["coord", OWNER, 42, payload], fence=self.FENCE)
        self.assertEqual((response.status, response.body["error"]), (400, "bad_args"))
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_peek")[0]["n"], 0)


class CliRefusalTest(unittest.TestCase):
    def test_local_peek_refusal_is_about_peek_not_a_decision(self):
        with mock.patch("ameesh.config.load"), mock.patch("ameesh.db.connect") as connect, \
                mock.patch.object(decisions, "human_session",
                                  side_effect=decisions.DecisionError("agent context")), \
                mock.patch.object(peek, "snapshot") as snapshot, \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(peek.main(["worker", "--json"]), 2)
            self.assertEqual(out.getvalue(), "")
            snapshot.assert_not_called()
            self.assertIn("peek is for a human reader outside an agent session", err.getvalue())
            self.assertNotIn("decision", err.getvalue())
            connect.return_value.close.assert_called_once()


class ResponseBoundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="peek-bound-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp)
        self.token = Path(self.tmp) / "token"
        self.token.write_text("t" * 43)
        self.token.chmod(0o600)

    def _read(self, raw):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = lambda n=-1: raw[:n]
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch("urllib.request.build_opener", return_value=opener):
            return peek.read_api("https://mesh.example", str(self.token), "worker", 10)

    def test_reader_accepts_payload_plus_metadata_and_refuses_one_byte_more(self):
        self.assertGreater(peek.MAX_RESPONSE_BYTES, peek.MAX_PAYLOAD_BYTES)
        prefix = b'{"schema": "' + peek.SCHEMA.encode() + b'", "pad": "'
        suffix = b'"}'
        raw = prefix + b"x" * (peek.MAX_RESPONSE_BYTES - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(raw), peek.MAX_RESPONSE_BYTES)
        self.assertEqual(self._read(raw)["schema"], peek.SCHEMA)
        with self.assertRaises(ValueError):
            self._read(raw + b" ")
        # A maximal payload plus real snapshot metadata also fits.
        workspace = {"branch": "b", "diff_stat": "", "available": True,
                     "scope": "tracked changes against HEAD"}
        payload = {"workspace": workspace,
                   "events": [{"kind": "message",
                               "summary": "x" * (peek.MAX_PAYLOAD_BYTES - 200)}]}
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()),
                             peek.MAX_PAYLOAD_BYTES)
        data = {"schema": peek.SCHEMA, "agent": "worker", "host": "h1", "status": "idle",
                "status_text": "", "lot": {"id": None, "title": "", "state": ""},
                "captured_ts": 1.0, "stale": False, **payload}
        self.assertLessEqual(len(json.dumps(data, ensure_ascii=False).encode()),
                             peek.MAX_RESPONSE_BYTES)
        self.assertEqual(self._read(json.dumps(data).encode())["schema"], peek.SCHEMA)
