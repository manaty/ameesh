# SPDX-License-Identifier: AGPL-3.0-only
"""L84: `ameesh serve`, the read API in local mode.

Defences (loopback, Host, Bearer token from a 0600 file, read-only, no echo
of the request), conformance of every route to its schema in the committed
OpenAPI document, and one serialisation shared with the CLI's `--json`.
"""
from __future__ import annotations

import dataclasses
import http.client
import io
import json
import logging
import os
import socket
import stat
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from ameesh import db as db_mod
from ameesh import fil, httpkit, mail, mesh_cli, registry, serve, server_faults, storage
from ameesh import turn_observation, work
from ameesh import resources as res
from ameesh import version as package_version

from .support import PgTestCase
from .test_l159a_capacity_guard import errors as schema_errors

ROOT = Path(__file__).resolve().parent.parent
OPENAPI_FILE = ROOT / "docs" / "api" / "openapi-v1.json"
UNIT = ROOT / "deploy" / "systemd" / "ameesh-serve.service"
TOKEN = "t" * 43


class ServerMixin:
    """A server on a free port, in a thread; `get()` answers (status, headers, body)."""

    def start(self, ctx, read, **kwargs):
        self.server = serve.make_server(ctx, token=TOKEN, read=read, port=0, **kwargs)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.logs = io.StringIO()
        handler = logging.StreamHandler(self.logs)
        serve.log.addHandler(handler)
        serve.log.setLevel(logging.INFO)
        self.addCleanup(serve.log.removeHandler, handler)
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def get(self, path, *, method="GET", host=None, auth=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", host or "127.0.0.1:%d" % self.port)
            if auth:
                conn.putheader("Authorization", "Bearer %s" % (auth if isinstance(auth, str)
                                                               else TOKEN))
            for name, value in (headers or {}).items():
                conn.putheader(name, value)
            conn.endheaders()
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, dict(resp.getheaders()), body
        finally:
            conn.close()

    def get_json(self, path, **kwargs):
        status, headers, body = self.get(path, **kwargs)
        return status, json.loads(body) if body else None


class DefencesTest(ServerMixin, unittest.TestCase):
    """Without a database: the routes that do not read it, and the guards."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l84-")
        cfg = type("Cfg", (), {"approve_url": "https://approve.example",
                               "threads_dir": os.path.join(self.tmp, "fils"),
                               "threads_root": os.path.join(self.tmp, "fils")})()
        self.reads = []

        def read(fn):
            self.reads.append(fn)
            raise db_mod.Unavailable("no database in this test")
        self.start(serve.Context(cfg=cfg, human="human:owner", organisation="Example"), read,
                   rate_per_minute=1000)

    def test_bound_to_the_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertNotIn("host", serve.make_server.__code__.co_varnames)

    def test_me_and_security_headers(self):
        status, headers, body = self.get("/api/v1/me")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual((data["schema"], data["human"], data["mode"]),
                         ("ameesh-me/1", "human:owner", "local"))
        for name, value in httpkit.BASE_HEADERS:
            self.assertEqual(headers[name], value)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Server"], "ameesh-serve")

    def test_token_required_and_compared(self):
        for auth in (False, "wrong", "t" * 42, "Basic " + TOKEN):
            status, headers, body = self.get("/api/v1/me", auth=auth)
            self.assertEqual(status, 401, auth)
            self.assertEqual(headers["WWW-Authenticate"], "Bearer")
            self.assertEqual(json.loads(body)["error"], "unauthorized")
        self.assertEqual(self.reads, [])

    def test_host_header_checked(self):
        for host in ("evil.example:%d" % 1, "127.0.0.1:1", "attacker.localhost"):
            status, _h, body = self.get("/api/v1/me", host=host)
            self.assertEqual(status, 403, host)
        self.assertEqual(self.get("/api/v1/me", host="localhost:%d" % self.port)[0], 200)

    def test_read_only_methods(self):
        # L87: POST acts, on its own routes only; a read route refuses it
        status, headers, _b = self.get("/api/v1/me", method="POST")
        self.assertEqual((status, headers.get("Allow")), (405, "GET, HEAD"))
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            status, headers, _b = self.get("/api/v1/me", method=method)
            self.assertEqual((status, headers.get("Allow")), (405, "GET, HEAD, POST"), method)
        status, headers, body = self.get("/api/v1/me", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_discovery_and_openapi_without_token(self):
        status, data = self.get_json("/.well-known/ameesh", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(schema_errors(serve.SCHEMAS["ameesh-discovery/1"], data), [])
        self.assertEqual((data["organisation"], data["api"], data["approve_url"]),
                         ("Example", "/api/v1", "https://approve.example"))
        # #3394: the discovery version is the package version, not the frozen
        # ACP protocol number of `ameesh.__version__`.
        self.assertEqual(data["version"], package_version())
        status, data = self.get_json("/api/v1/openapi.json", auth=False)
        self.assertEqual((status, data), (200, serve.openapi()))

    def test_unknown_route_parameter_and_path(self):
        self.assertEqual(self.get_json("/api/v1/nothing")[0], 404)
        self.assertEqual(self.get_json("/api/v2/me")[0], 404)
        status, data = self.get_json("/api/v1/me?x=1")
        self.assertEqual((status, data["error"]), (400, "bad_query"))
        status, data = self.get_json("/api/v1/agents/..%2Fetc")
        self.assertEqual((status, data["error"]), (400, "bad_path"))
        status, data = self.get_json("/api/v1/lots/abc")
        self.assertEqual((status, data["error"]), (400, "bad_path"))
        status, data = self.get_json("/api/v1/lots?limit=1&limit=2")
        self.assertEqual(status, 400)

    def test_database_unavailable_is_503_without_detail(self):
        status, data = self.get_json("/api/v1/budget")
        self.assertEqual((status, data["error"]), (503, "unavailable"))
        self.assertNotIn("no database", data["message"])

    def test_the_request_is_never_echoed(self):
        secret = "s3cr3t" * 8
        status, _h, body = self.get("/api/v1/nothing?t=%s" % secret)
        self.assertEqual(status, 404)
        self.assertNotIn(secret.encode(), body)
        # a malformed request line: the standard error page would copy it
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(b"GET /?t=" + secret.encode() + b" BOGUS/9\r\n\r\n")
            raw = sock.recv(65536)
        self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(secret, self.logs.getvalue())
        self.assertNotIn(TOKEN, self.logs.getvalue())

    def test_rate_limited(self):
        self.server.limiter = httpkit.RateLimiter(2)
        statuses = [self.get("/api/v1/me")[0] for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])

    def test_thread_read_from_files(self):
        transport = fil.FileTransport(self.server.RequestHandlerClass.ctx.cfg.threads_root)
        thread = fil.ThreadRef("demo", "7")
        first = transport.post(thread, fil.Entry(author="agent:a1", recipient="tous",
                                                 text="premier"))
        transport.post(thread, fil.Entry(author="human:owner", recipient="agent:a1",
                                         text="second"))
        status, data = self.get_json("/api/v1/threads/demo/7")
        self.assertEqual(status, 200)
        self.assertEqual(schema_errors(serve.SCHEMAS["ameesh-thread/1"], data), [])
        self.assertEqual([e["text"] for e in data["entries"]], ["premier", "second"])
        status, data = self.get_json("/api/v1/threads/demo/7?since=%s" % first.replace("#", "%23"))
        self.assertEqual([e["text"] for e in data["entries"]], ["second"])
        self.assertEqual(self.get_json("/api/v1/threads/demo/8")[0], 404)
        self.assertEqual(self.get_json("/api/v1/threads/demo")[0], 404)


class TokenFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l84-token-")

    def test_created_0600_then_read_never_overwritten(self):
        path = os.path.join(self.tmp, "conf", "serve-token")
        err = io.StringIO()
        token = serve.load_token(path, out=err)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertGreaterEqual(len(token), 43)
        self.assertNotIn(token, err.getvalue())
        self.assertEqual(serve.load_token(path), token)
        with self.assertRaises(ValueError):
            httpkit.write_token_file(path)

    def test_open_or_linked_file_refused(self):
        path = os.path.join(self.tmp, "token")
        with open(path, "w") as fh:
            fh.write(TOKEN)
        os.chmod(path, 0o644)
        with self.assertRaises(ValueError):
            serve.load_token(path)
        os.chmod(path, 0o600)
        link = os.path.join(self.tmp, "link")
        os.symlink(path, link)
        with self.assertRaises(ValueError):
            serve.load_token(link)

    def test_token_file_option_and_environment(self):
        self.assertEqual(serve.token_file("/x/y"), "/x/y")
        os.environ[serve.TOKEN_FILE_ENV] = "/a/b"
        try:
            self.assertEqual(serve.token_file(), "/a/b")
        finally:
            del os.environ[serve.TOKEN_FILE_ENV]
        self.assertTrue(serve.token_file().endswith("/.config/ameesh/serve-token"))


class OpenApiTest(unittest.TestCase):
    def test_committed_document_is_the_generated_one(self):
        self.assertEqual(OPENAPI_FILE.read_text(encoding="utf-8"), serve.dump_openapi(),
                         "regenerate: ameesh serve --openapi > docs/api/openapi-v1.json")

    def test_every_route_documented_with_its_schema(self):
        doc = serve.openapi()
        self.assertEqual(doc["openapi"], "3.1.0")
        for route in serve.ROUTES:
            operation = doc["paths"][route.path]["get"]
            if route.auth:
                self.assertNotIn("security", operation)  # the global Bearer
                content = operation["responses"]["200"]["content"]
                # L85: the event stream documents the `data` of its messages
                ref = content["text/event-stream"]["x-ameesh-data"] if route.stream \
                    else content["application/json"]["schema"]
                self.assertEqual(ref["$ref"].rsplit("/", 1)[1],
                                 route.schema.replace("/", "-"))
            else:
                self.assertEqual(operation["security"], [])
        self.assertEqual(doc["paths"]["/.well-known/ameesh"]["get"]["security"], [])

    def test_writes_are_the_actions_only(self):
        # L87: a POST exists exactly on the human actions, nothing else writes
        actions = {action.path for action in serve.ACTIONS}
        for path, item in serve.openapi()["paths"].items():
            self.assertEqual(set(item) - {"get"}, {"post"} if path in actions else set(), path)


class SystemdUnitTest(unittest.TestCase):
    def test_unit_runs_serve_on_the_loopback(self):
        text = UNIT.read_text(encoding="utf-8")
        exec_start = [line for line in text.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(len(exec_start), 1)
        self.assertIn("ameesh serve", exec_start[0])
        self.assertNotIn("--host", exec_start[0])
        for line in ("NoNewPrivileges=yes", "PrivateTmp=yes", "Restart=on-failure",
                     "UMask=0077"):
            self.assertIn(line, text.splitlines())


class ServePgTest(ServerMixin, PgTestCase):
    """On a migrated schema: each route, its schema, the CLI's serialisation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        registry.upsert(cls.db, "a1", chantier="demo", harness="codex", host="h1")
        cls.db.execute("UPDATE agent_registry SET responsible='human:owner' WHERE name='a1'")
        cls.lot = work.add(cls.db, title="lot servi", app="demo", assignee="a1")
        work.move(cls.db, cls.lot["id"], "build")
        storage.of(cls.db).hosts.record(res.sample("h1"))
        transport = fil.transport_for(cls.cfg)
        transport.post(fil.ThreadRef("demo"), fil.Entry(author="agent:a1", recipient="tous",
                                                        text="fil du projet"))
        transport.post(fil.ThreadRef("demo", str(cls.lot["id"])),
                       fil.Entry(author="agent:a1", recipient="tous", text="fil du lot"))
        # #2167 B1: one turn with its resource row, and one request delivered
        # in it (the turn recorded in `meta.consigne.turn` at reservation).
        cls.turn = uuid.uuid4().hex
        storage.of(cls.db).turn_resources.open_turn(
            cls.turn, "a1", "h1", pgid=None, label="tour observé")
        cls.request = mail.send(cls.db, "human:owner", "a1",
                                "corps secret jamais observé", kind="request",
                                payload={"secret": "charge utile jamais observée"})
        token = mail.new_token()
        mail.reserve(cls.db, "a1", None, None, token, ids=[cls.request],
                     porteur="test", turn_id=cls.turn)
        mail.deliver(cls.db, "a1", None, None, token, [cls.request])

    def setUp(self):
        ctx = serve.Context(cfg=dataclasses.replace(self.cfg, read_only=True),
                            human="human:owner")
        self.start(ctx, serve.db_reader(self.cfg))

    def paths(self):
        lot = str(self.lot["id"])
        return {
            "/api/v1/me": "/api/v1/me",
            "/api/v1/projects": "/api/v1/projects",
            "/api/v1/projects/{project}/progress": "/api/v1/projects/demo/progress?since=2d",
            "/api/v1/projects/{project}/timeline": "/api/v1/projects/demo/timeline?since=2d",
            "/api/v1/agents": "/api/v1/agents",
            "/api/v1/agents/{agent}": "/api/v1/agents/a1",
            "/api/v1/agents/{agent}/peek": "/api/v1/agents/a1/peek",
            "/api/v1/agents/{agent}/chat": "/api/v1/agents/a1/chat?limit=5",
            "/api/v1/lots": "/api/v1/lots?state=build&limit=10",
            "/api/v1/lots/{id}": "/api/v1/lots/" + lot,
            "/api/v1/turns/{turn}": "/api/v1/turns/" + self.turn,
            "/api/v1/requests/{request}": "/api/v1/requests/%d" % self.request,
            "/api/v1/decisions": "/api/v1/decisions?all=1",
            "/api/v1/alerts": "/api/v1/alerts",
            "/api/v1/hosts": "/api/v1/hosts",
            "/api/v1/hosts/{host}": "/api/v1/hosts/h1",
            "/api/v1/budget": "/api/v1/budget",
            "/api/v1/threads/{project}": "/api/v1/threads/demo",
            "/api/v1/threads/{project}/{lot}": "/api/v1/threads/demo/" + lot,
            "/api/v1/openapi.json": "/api/v1/openapi.json",
            "/api/v1/events": None,   # L85: a stream, tested in test_l85_events
        }

    def test_every_route_answers_in_line_with_its_schema(self):
        paths = self.paths()
        self.assertEqual(set(paths), {route.path for route in serve.ROUTES})
        components = json.loads(OPENAPI_FILE.read_text(encoding="utf-8"))["components"]
        for route in serve.ROUTES:
            if route.stream:
                continue
            status, data = self.get_json(paths[route.path])
            self.assertEqual(status, 200, (route.path, data))
            if route.schema == "openapi":
                continue
            schema = components["schemas"][route.schema.replace("/", "-")]
            self.assertEqual(schema_errors(schema, data), [], route.path)
            self.assertEqual(data["schema"], route.schema)

    def test_turn_and_request_observation(self):
        # #2167 B1: both routes answer the same `ameesh-turn-observation/1`
        # document; the request names its delivery turn, the turn names no
        # request. Local mode's token is the owner's, like `/api/v1/agents`.
        status, turn = self.get_json("/api/v1/turns/" + self.turn)
        self.assertEqual(status, 200, turn)
        self.assertEqual(turn["by"], {"turn": self.turn})
        self.assertIsNone(turn["request"])
        self.assertEqual(turn["turn"]["id"], self.turn)
        self.assertEqual(turn["turn"]["state"], "running")
        self.assertEqual(turn["agent"]["name"], "a1")
        self.assertFalse(turn["agent"]["turn_in_progress"])
        self.assertFalse(turn["lease"]["live"])
        self.assertEqual(turn["accounting"]["state"], "none")
        self.assertTrue(turn["quiescent"])
        status, request = self.get_json("/api/v1/requests/%d" % self.request)
        self.assertEqual(status, 200, request)
        self.assertEqual(request["by"], {"request": self.request})
        self.assertEqual(request["request"]["state"], "delivered")
        self.assertEqual(request["request"]["delivered_turn_id"], self.turn)
        self.assertEqual(request["turn"]["id"], self.turn)
        # metadata only: neither the body nor the payload crosses the route
        raw = json.dumps(request) + json.dumps(turn)
        for secret in ("corps secret", "charge utile"):
            self.assertNotIn(secret, raw)
        self.assertNotIn("payload", raw)

    def test_turn_and_request_bad_ids(self):
        for path in ("/api/v1/turns/not-a-turn", "/api/v1/requests/abcd",
                     "/api/v1/requests/" + "9" * 20):
            status, data = self.get_json(path)
            self.assertEqual((status, data["error"]), (400, "bad_path"), path)

    def test_timeline_is_the_light_progress_view(self):
        # #3395: the Chronologie endpoint says the same lots as the progress
        # snapshot without the sections it never reads (1.1 MB, 39-48 s).
        status, full = self.get_json("/api/v1/projects/demo/progress?since=2d")
        self.assertEqual(status, 200)
        status, light = self.get_json("/api/v1/projects/demo/timeline?since=2d")
        self.assertEqual(status, 200)
        self.assertEqual(set(light),
                         {"schema", "generated_ts", "project", "window", "lots",
                          "truncated"})
        self.assertEqual(light["truncated"].get("lots"),
                         full["truncated"].get("lots"))
        self.assertEqual([lot["id"] for lot in light["lots"]],
                         [lot["id"] for lot in full["lots"]])
        self.assertEqual([(lot["state"], (lot.get("waiting_for") or {}).get("what"))
                          for lot in light["lots"]],
                         [(lot["state"], (lot.get("waiting_for") or {}).get("what"))
                          for lot in full["lots"]])
        status, data = self.get_json("/api/v1/projects/demo/timeline?since=never")
        self.assertEqual((status, data["error"]), (400, "bad_query"))

    def test_same_serialisation_as_the_cli(self):
        def cli(*argv):
            out = self.mesh(*argv)
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout)

        _s, agents = self.get_json("/api/v1/agents")
        self.assertEqual([sorted(a) for a in agents["agents"]],
                         [sorted(a) for a in cli("list", "--json")])
        _s, agent = self.get_json("/api/v1/agents/a1")
        self.assertEqual(agent["agent"]["name"], "a1")
        self.assertEqual(sorted(agent["agent"]), sorted(cli("show", "a1", "--json")))
        _s, lot = self.get_json("/api/v1/lots/%d" % self.lot["id"])
        shown = cli("work", "show", str(self.lot["id"]), "--json")
        self.assertEqual(sorted(lot["lot"]), sorted(shown))
        self.assertEqual(lot["lot"]["events"], shown["events"])
        _s, lots = self.get_json("/api/v1/lots")
        self.assertEqual([l["id"] for l in lots["lots"]],
                         [l["id"] for l in cli("work", "list", "--json")])
        _s, host = self.get_json("/api/v1/hosts/h1")
        cli_host = self.mesh("hosts", "--json", "h1")
        self.assertEqual(sorted(host), sorted(json.loads(cli_host.stdout)))
        _s, decisions = self.get_json("/api/v1/decisions")
        expected = cli("decisions", "--for", "human:owner", "--json")
        self.assertEqual(dict(decisions, schema=None), dict(expected, schema=None))

    def test_agent_survives_unavailable_footprint(self):
        from ameesh import canon, host_idle, mesh_cli
        for module, method in ((canon, "load_configured"), (res, "current"),
                               (host_idle, "snapshot")):
            with self.subTest(source=method), mock.patch.object(
                    module, method, side_effect=OSError("unreadable")):
                row = mesh_cli.agent_detail(self.db, "a1", cfg=self.cfg, footprint=True)
                self.assertEqual(row["name"], "a1")
                self.assertIsNone(row["footprint"])
                self.assertEqual(row["footprint_unavailable"], "unreadable")
                status, data = self.get_json("/api/v1/agents/a1")
                self.assertEqual(status, 200, data)
                self.assertIsNone(data["agent"]["footprint"])

    def test_show_does_not_compute_the_host_footprint(self):
        # #4375 : host_idle.snapshot (~15 s sur un hôte réel) hors de `show`
        # et de l'API agent ; vm.sh lit `show --json` sous une borne de 30 s.
        from ameesh import host_idle, mesh_cli
        with mock.patch.object(host_idle, "snapshot",
                               side_effect=AssertionError("snapshot in show")):
            row = mesh_cli.agent_detail(self.db, "a1", cfg=self.cfg)
            self.assertEqual(row["name"], "a1")
            self.assertIsNone(row["footprint"])
            self.assertIsNone(row["footprint_unavailable"])
            status, data = self.get_json("/api/v1/agents/a1")
            self.assertEqual(status, 200, data)
            self.assertIsNone(data["agent"]["footprint"])

    def test_unknown_objects_are_404(self):
        for path in ("/api/v1/agents/nobody", "/api/v1/lots/999999", "/api/v1/hosts/nowhere",
                     "/api/v1/threads/nothing", "/api/v1/turns/" + "0" * 32,
                     "/api/v1/requests/999999"):
            status, data = self.get_json(path)
            self.assertEqual((status, data["error"]), (404, "not_found"), path)

    def test_the_database_session_is_read_only(self):
        def write(db):
            db.execute("UPDATE work_items SET title = 'x' WHERE id = %s", (self.lot["id"],))
        with self.assertRaises(db_mod.DbError):
            serve.db_reader(self.cfg)(write)
        self.assertEqual(work.get(self.db, self.lot["id"])["title"], "lot servi")

    def test_owner_of_local_mode(self):
        self.assertEqual(serve.owner(self.cfg, "human:alice"), "human:alice")
        with self.assertRaises(ValueError):
            serve.owner(self.cfg, "alice smith")
        cfg = dataclasses.replace(self.cfg, notify={"default_human": "human:bob"})
        self.assertEqual(serve.owner(cfg), "human:bob")


class TurnObservationTest(PgTestCase):
    """#2167 B1: the `ameesh-turn-observation/1` contract, from the facts.

    The routes are covered by `ServePgTest` (schema and serialisation); here
    the states are exercised at the builder both routes share, including the
    accounting precedence that made #2166 silent.
    """

    def setUp(self):
        super().setUp()
        for table in ("turn_resources", "turn_costs", "spend_pending",
                      "account_reservations"):
            self.db.execute("DELETE FROM " + table)

    def agent(self, name="a1", status="idle"):
        registry.upsert(self.db, name, chantier="demo", harness="deepseek", host="h1")
        if status != "idle":
            self.db.execute("UPDATE agent_registry SET status=%s, status_text=%s"
                            " WHERE name=%s", (status, status, name))
        return name

    def turn(self, state="running", agent="a1"):
        turn_id = uuid.uuid4().hex
        resources = storage.of(self.db).turn_resources
        resources.open_turn(turn_id, agent, "h1", pgid=None, label="tour")
        if state == "done":
            resources.close_turn(turn_id, orphan=False)
        elif state == "orphan":
            resources.mark_orphan(turn_id)
        return turn_id

    def marker(self, agent="a1", turn_id=None):
        return registry.pending_spend_begin(
            self.db, agent, 1, "tour", "deepseek-flash",
            turn_id=turn_id or uuid.uuid4().hex)

    def ledger(self, agent, marker):
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, turn, model, usd, turn_id,"
            " spend_key, started_at, recorded_at, source)"
            " VALUES (%s, 'deepseek', 'tour', %s, 0.5, %s, %s,"
            " now() - interval '1 hour', now(), 'harness')",
            (agent, marker.get("model") or "deepseek-flash", marker["turn_id"],
             marker["spend_key"]))

    def request(self, *, agent="a1", turn_id=None, delivered=False,
                body="corps secret jamais observé",
                payload=None):
        message = mail.send(self.db, "human:owner", agent, body, kind="request",
                            payload=payload or {"secret": "charge jamais observée"})
        if delivered:
            token = mail.new_token()
            mail.reserve(self.db, agent, None, None, token, ids=[message],
                         porteur="test", turn_id=turn_id)
            mail.deliver(self.db, agent, None, None, token, [message])
        return message

    def test_request_states(self):
        agent = self.agent()
        pending = self.request(agent=agent)
        document = turn_observation.observe(self.db, request=pending)
        self.assertEqual(document["by"], {"request": pending})
        self.assertEqual(document["request"]["state"], "pending")
        self.assertIsNone(document["request"]["delivered_ts"])
        self.assertIsNone(document["turn"])
        self.assertEqual(document["accounting"]["state"], "none")
        self.assertTrue(document["quiescent"])

        turn_id = self.turn("done", agent)
        delivered = self.request(agent=agent, delivered=True, turn_id=turn_id)
        document = turn_observation.observe(self.db, request=delivered)
        self.assertEqual(document["request"]["state"], "delivered")
        self.assertEqual(document["request"]["delivered_turn_id"], turn_id)
        self.assertEqual(document["turn"]["state"], "ended")
        self.assertIsNotNone(document["turn"]["ended_ts"])

        withdrawn = self.request(agent=agent)
        self.db.execute(
            "UPDATE agent_mailbox SET delivered_at = now(), status = 'delivered',"
            " meta = meta || '{\"retire\": {}}'::jsonb WHERE id = %s", (withdrawn,))
        document = turn_observation.observe(self.db, request=withdrawn)
        self.assertEqual(document["request"]["state"], "withdrawn")
        self.assertIsNone(document["request"]["delivered_ts"])
        self.assertIsNone(document["request"]["delivered_turn_id"])

    def test_turn_states(self):
        agent = self.agent()
        running = self.turn("running", agent)
        self.assertEqual(turn_observation.observe(
            self.db, turn=running)["turn"]["state"], "running")
        done = self.turn("done", agent)
        self.assertEqual(turn_observation.observe(
            self.db, turn=done)["turn"]["state"], "ended")
        orphan = self.turn("orphan", agent)
        self.assertEqual(turn_observation.observe(
            self.db, turn=orphan)["turn"]["state"], "orphan")

        # `starting`: the accounting marker exists before the resource row.
        starting = self.marker(agent)
        document = turn_observation.observe(self.db, turn=starting["turn_id"])
        self.assertEqual(document["turn"]["state"], "starting")
        self.assertEqual(document["turn"]["started_ts"], starting["created_ts"])
        self.assertIsNone(document["turn"]["ended_ts"])

        # `unknown`: the request names a delivery turn whose facts are gone.
        unknown = uuid.uuid4().hex
        message = self.request(agent=agent, delivered=True, turn_id=unknown)
        document = turn_observation.observe(self.db, request=message)
        self.assertEqual(document["turn"]["id"], unknown)
        self.assertEqual(document["turn"]["state"], "unknown")
        self.assertEqual(document["accounting"]["state"], "none")

    def test_accounting_precedence(self):
        # A ledger row settles the turn even when the marker is still there
        # (the #2166 symptom): the stale marker is shown, but the state is
        # `settled`, and the turn stays quiescent.
        settled = self.agent("settled")
        marker = self.marker(settled)
        self.ledger(settled, marker)
        document = turn_observation.observe(self.db, turn=marker["turn_id"])
        self.assertEqual(document["accounting"]["state"], "settled")
        self.assertEqual(document["accounting"]["ledger"]["spend_key"],
                         marker["spend_key"])
        self.assertIsNotNone(document["accounting"]["marker"])
        self.assertTrue(document["quiescent"])

        # A kept (or released) reservation is a terminal outcome and wins
        # over the ledger's `settled`.
        kept = self.agent("kept")
        marker = self.marker(kept)
        self.ledger(kept, marker)
        self.db.execute(
            "INSERT INTO account_reservations (billing_account, agent, usd,"
            " period, state, spend_key) VALUES ('acct', %s, 2.0, current_date,"
            " 'kept', %s)", (kept, marker["spend_key"]))
        document = turn_observation.observe(self.db, turn=marker["turn_id"])
        self.assertEqual(document["accounting"]["state"], "kept")
        self.assertEqual(document["accounting"]["reservation"]["state"], "kept")

        # A marker without a ledger is `pending`: the accounting is not done,
        # so the agent is never quiescent, even when it looks idle.
        pending = self.agent("pending")
        marker = self.marker(pending)
        document = turn_observation.observe(self.db, turn=marker["turn_id"])
        self.assertEqual(document["accounting"]["state"], "pending")
        self.assertIsNone(document["accounting"]["ledger"])
        self.assertFalse(document["quiescent"])

    def test_turn_in_progress_requires_a_live_lease_on_a_running_agent(self):
        agent = self.agent("busy", status="running")
        self.assertIsNotNone(registry.claim(self.db, agent, "runner-test", 3600))
        document = turn_observation.observe(self.db, turn=self.turn("running", agent))
        self.assertTrue(document["agent"]["turn_in_progress"])
        self.assertTrue(document["lease"]["live"])
        self.assertFalse(document["quiescent"])

    def test_document_carries_no_message_content(self):
        agent = self.agent()
        turn_id = self.turn("done", agent)
        message = self.request(agent=agent, delivered=True, turn_id=turn_id,
                               body="corps-interdit", payload={"secret": "charge-interdite"})
        for document in (turn_observation.observe(self.db, request=message),
                         turn_observation.observe(self.db, turn=turn_id)):
            text = json.dumps(document)
            self.assertNotIn("corps-interdit", text)
            self.assertNotIn("charge-interdite", text)
            self.assertNotIn("payload", text)

    def test_unknown_targets(self):
        self.agent()
        with self.assertRaises(turn_observation.Unknown):
            turn_observation.observe(self.db, turn=uuid.uuid4().hex)
        with self.assertRaises(turn_observation.Unknown):
            turn_observation.observe(self.db, request=999999)
        with self.assertRaises(ValueError):
            turn_observation.observe(self.db)


class BuildersTest(unittest.TestCase):
    def test_cli_builders_are_shared(self):
        # one serialisation: the API calls the functions the CLI prints
        source = (ROOT / "src" / "ameesh" / "serve.py").read_text(encoding="utf-8")
        for name in ("mesh_cli.agent_rows", "mesh_cli.agent_detail_json", "mesh_cli.work_rows",
                     "mesh_cli.work_detail", "mesh_cli.host_views",
                     "actions_cli.decisions_view", "projects.snapshot", "progress.snapshot",
                     "budget.status", "exploitation.alerts"):
            self.assertIn(name + "(", source)
        cli = (ROOT / "src" / "ameesh" / "mesh_cli.py").read_text(encoding="utf-8")
        for name in ("agent_detail(db, args.agent, cfg=cfg)", "work_rows(db,",
                     # #1905 : `work show` passe aussi repo= et target= (arguments nommés à défaut)
                     "work_detail(cfg, db, args.id", "_host_json(part)"):
            self.assertIn(name, cli)
        self.assertTrue(callable(mesh_cli.agent_rows))


class FaultJournalTest(ServerMixin, unittest.TestCase):
    """#3892 (§16) : une panne de `ameesh serve` est comptée par empreinte —
    gabarit de route, classe d'exception, version — jamais le message reçu."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l84-faults-")
        cfg = type("Cfg", (), {"approve_url": None,
                               "threads_dir": os.path.join(self.tmp, "fils"),
                               "threads_root": os.path.join(self.tmp, "fils"),
                               "state_dir": self.tmp})()
        self.failure = db_mod.Unavailable("database at 10.1.2.3 refused for alice")

        def read(fn):
            raise self.failure
        self.start(serve.Context(cfg=cfg, human="human:owner", organisation="Example"), read,
                   rate_per_minute=1000)

    def faults(self):
        with open(os.path.join(self.tmp, "faults-serve.json"), encoding="utf-8") as fh:
            return json.load(fh)["faults"]

    def test_a_database_fault_is_journaled_by_route_template(self):
        status, body = self.get_json("/api/v1/lots/123")
        self.assertEqual((status, body["error"]), (503, "unavailable"))
        [entry] = self.faults().values()
        self.assertEqual((entry["route"], entry["code"]),
                         ("ameesh-serve GET /api/v1/lots/{id}",
                          "EXCEPTION:Unavailable"))
        self.assertEqual(entry["occurrences"], 1)
        raw = json.dumps(self.faults())
        self.assertNotIn("10.1.2.3", raw)
        self.assertNotIn("alice", raw)
        self.assertNotIn("/lots/123", raw)

    def test_an_unexpected_fault_is_journaled_too(self):
        self.failure = RuntimeError("boom at 10.1.2.3")
        status, body = self.get_json("/api/v1/projects")
        self.assertEqual((status, body["error"]), (500, "internal"))
        [entry] = self.faults().values()
        self.assertEqual((entry["route"], entry["code"]),
                         ("ameesh-serve GET /api/v1/projects", "EXCEPTION:RuntimeError"))
        self.assertNotIn("10.1.2.3", json.dumps(self.faults()))


class FaultReporterTest(unittest.TestCase):
    """#3892 : le journal nourrit la primitive de #3885, une fois, sans double lot."""

    def test_the_reporter_feeds_the_primitive_once(self):
        tmp = tempfile.mkdtemp(prefix="l84-reporter-")
        journal = server_faults.Journal(os.path.join(tmp, "faults-serve.json"),
                                        "ameesh-serve")
        reported = []

        def report(db, **kwargs):
            reported.append(kwargs)
            return {"id": 7, "created": True}

        reporter = serve.FaultReporter(journal, lambda fn: fn(None), report=report)
        reporter.record("GET /api/v1/lots/{id}",
                        db_mod.Unavailable("database at 10.1.2.3 refused"))
        reporter.kick(wait=True)
        self.assertEqual(len(reported), 1)
        entry = reported[0]
        self.assertEqual((entry["app"], entry["route"], entry["code"]),
                         ("ameesh", "ameesh-serve GET /api/v1/lots/{id}",
                          "EXCEPTION:Unavailable"))
        self.assertEqual((entry["count"], entry["users"]), (1, 0))
        # entrée rapportée : un second rapport ne rouvre pas de lot
        reporter.kick(wait=True)
        self.assertEqual(len(reported), 1)


if __name__ == "__main__":
    unittest.main()
