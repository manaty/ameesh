# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh progress --serve` (L304) : serveur local en lecture seule.

Sans base : liaison 127.0.0.1, jeton (URL → cookie lié au port, temps
constant, jamais journalisé), en-tête Host, 405 hors GET/HEAD, forme du
JSON, cache, `?project=`. Sur Postgres réel : l'instantané lu par une session
en lecture seule, les cartes `overview` et la CLI.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import threading
import unittest
from unittest import mock

from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh import progress, progress_serve, registry, work

from .support import PgTestCase
from .test_progress import NOW, _snap


def _projects_snap(project=None):
    snap = _snap(project=project,
                 projects=[{"name": "demo", "active": True}, {"name": "autre", "active": True}],
                 overview=[], decisions=[])
    return snap


class ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = []
        self.clock = [0.0]

        def build(project):
            self.calls.append(project)
            return _projects_snap(project)

        self.snapshots = progress_serve.Snapshots(build, clock=lambda: self.clock[0])
        self.server = progress_serve.make_server(self.snapshots, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.stderr = io.StringIO()
        patcher = mock.patch("sys.stderr", self.stderr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, *, host=None, cookie=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or "127.0.0.1:%d" % self.port}
        if cookie:
            headers["Cookie"] = cookie
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp, resp.read()
        finally:
            conn.close()

    def cookie(self):
        return "ameesh_progress_%d=%s" % (self.port, self.server.token)


class ServeTest(ServerCase):
    def test_lie_a_127_0_0_1_seulement(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(progress_serve.BIND, "127.0.0.1")
        self.assertNotIn("host", progress_serve.make_server.__code__.co_varnames)

    def test_jeton_de_l_url_vers_le_cookie(self):
        resp, _ = self.request("GET", "/")
        self.assertEqual(resp.status, 401)
        resp, _ = self.request("GET", "/?t=faux")
        self.assertEqual(resp.status, 401)
        resp, _ = self.request("GET", "/?t=%s&project=demo" % self.server.token)
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/?project=demo")
        cookie = resp.getheader("Set-Cookie")
        self.assertTrue(cookie.startswith(self.cookie() + ";"), cookie)
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)
        # le cookie d'un autre port (un autre serveur) ne vaut rien ici
        other = "ameesh_progress_%d=%s" % (self.port + 1, self.server.token)
        self.assertEqual(self.request("GET", "/", cookie=other)[0].status, 401)
        self.assertEqual(self.request("GET", "/", cookie=self.cookie() + "x")[0].status, 401)
        self.assertEqual(self.request("GET", "/", cookie=self.cookie())[0].status, 200)

    def test_redirection_vers_un_chemin_servi_seulement(self):
        resp, _ = self.request("GET", "//ailleurs.example/?t=%s" % self.server.token)
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/")

    def test_jeton_compare_en_temps_constant(self):
        with mock.patch("hmac.compare_digest", wraps=__import__("hmac").compare_digest) as cd:
            self.request("GET", "/", cookie=self.cookie())
        self.assertTrue(cd.called)

    def test_jeton_neuf_a_chaque_serveur(self):
        other = progress_serve.make_server(self.snapshots, port=0)
        try:
            self.assertNotEqual(other.token, self.server.token)
            self.assertGreaterEqual(len(other.token), 32)
        finally:
            other.server_close()

    def test_jeton_jamais_journalise(self):
        self.request("GET", "/?t=%s" % self.server.token)
        self.request("GET", "/?t=%s" % self.server.token[:-1])
        self.request("GET", "/", cookie=self.cookie())
        log = self.stderr.getvalue()
        self.assertIn("GET /", log)
        self.assertNotIn(self.server.token, log)
        self.assertNotIn(self.server.token[:-1], log)

    def test_hote_refuse(self):
        for host in ("ailleurs.example:%d" % self.port, "127.0.0.1:1", "127.0.0.1"):
            resp, _ = self.request("GET", "/", host=host, cookie=self.cookie())
            self.assertEqual(resp.status, 403, host)
        resp, _ = self.request("GET", progress_serve.API, host="localhost:%d" % self.port,
                               cookie=self.cookie())
        self.assertEqual(resp.status, 200)

    def test_lecture_seule(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            resp, _ = self.request(method, progress_serve.API, cookie=self.cookie(), body=b"{}")
            self.assertEqual(resp.status, 405, method)
            self.assertEqual(resp.getheader("Allow"), "GET, HEAD")
        self.assertEqual(self.calls, [])

    def test_page_servie(self):
        resp, body = self.request("GET", "/", cookie=self.cookie())
        self.assertEqual(resp.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        html = body.decode("utf-8")
        self.assertIn('var MODE = "served";', html)
        self.assertNotIn("</script><b>", html)
        head, _ = self.request("HEAD", "/", cookie=self.cookie())
        self.assertEqual(head.status, 200)

    def test_json_et_projet(self):
        resp, body = self.request("GET", progress_serve.API, cookie=self.cookie())
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json; charset=utf-8")
        data = json.loads(body)
        self.assertEqual(data["schema"], "ameesh-progress/1")
        for key in ("overview", "decisions", "lots", "agents", "generated_ts"):
            self.assertIn(key, data)
        resp, body = self.request("GET", progress_serve.API + "?project=demo",
                                  cookie=self.cookie())
        self.assertEqual(json.loads(body)["project"], "demo")
        for bad in ("inconnu", "../x", "a" * 80):
            resp, _ = self.request("GET", progress_serve.API + "?project=" + bad,
                                   cookie=self.cookie())
            self.assertEqual(resp.status, 400, bad)
        self.assertEqual(self.request("GET", "/ailleurs", cookie=self.cookie())[0].status, 404)

    def test_cache(self):
        self.request("GET", progress_serve.API, cookie=self.cookie())
        self.request("GET", "/", cookie=self.cookie())
        self.assertEqual(self.calls, [None])
        self.clock[0] += progress_serve.CACHE_S
        self.request("GET", progress_serve.API, cookie=self.cookie())
        self.assertEqual(self.calls, [None, None])


class PageModeTest(unittest.TestCase):
    def test_statique_bouton_desactive(self):
        html = progress.render_html(_snap())
        self.assertIn('var MODE = "static";', html)
        self.assertIn("open with --serve to refresh", html)
        self.assertIn("default-src 'none'", html)
        self.assertIn('var MODE = "served";', progress.render_html(_snap(), served=True))


class OverviewTest(unittest.TestCase):
    def test_cartes(self):
        packages = [{"id": "M1", "kind": "milestone", "parent": None},
                    {"id": "M2", "kind": "milestone", "parent": None},
                    {"id": "E1", "kind": "epic", "parent": "M1"},
                    {"id": "L1", "kind": "lot", "parent": "E1"}]
        items = [{"id": 1, "package_id": "L1", "app": "demo"},
                 {"id": 2, "package_id": "M2", "app": "demo"},
                 {"id": 3, "package_id": "M2", "app": "autre"},
                 {"id": 4, "package_id": None, "app": "demo"}]
        ms_projects = progress.milestone_projects(packages, items)
        self.assertEqual(ms_projects, {"M1": {"demo"}, "M2": {"demo", "autre"}})
        milestones = [
            {"id": "M1", "title": "un", "date": "2026-11-01", "progress": 0.5,
             "lots_merged": 1, "lots_total": 2},
            {"id": "M2", "title": "deux", "date": "2026-10-20", "progress": 1,
             "lots_merged": 2, "lots_total": 2}]
        projects = [{"name": "demo", "active": True, "lots_open": 3, "agents_active": 2,
                     "counts": {"working": 1}, "usd_24h": 1.234567, "warnings": ["w"]},
                    {"name": "autre", "active": False, "lots_open": 0, "agents_active": 0,
                     "counts": {}, "usd_24h": 0}]
        lots = [{"project": "demo", "state": "merged"}, {"project": "demo", "state": "active"},
                {"project": "autre", "state": "merged"}]
        decisions = [{"project": "demo"}, {"project": "demo"}, {"project": None}]
        demo, autre = progress.build_overview(projects, lots, milestones, ms_projects,
                                              decisions)
        self.assertEqual(demo, {
            "project": "demo", "active": True, "lots_open": 3, "lots_merged_in_window": 1,
            "agents_active": 2, "agents_working": 1, "spend_24h_usd": 1.23,
            "decisions_pending": 2, "warnings": ["w"],
            # M2, plus tôt, est atteint : M1 est le prochain
            "milestone": {"id": "M1", "title": "un", "date": "2026-11-01", "progress": 0.5,
                          "lots_merged": 1, "lots_total": 2}})
        self.assertIsNone(autre["milestone"])
        self.assertEqual((autre["lots_merged_in_window"], autre["decisions_pending"]), (1, 0))


class ServePgTest(PgTestCase):
    def test_instantane_en_lecture_seule(self):
        registry.upsert(self.db, "a1", chantier="demo", harness="codex", host=self.cfg.host)
        item = work.add(self.db, title="lot servi", app="demo", assignee="a1")
        work.move(self.db, item["id"], "build")
        build = progress_serve.db_builder(self.cfg, since="2d", stale_after=6 * 3600.0,
                                          gantt=True)
        snap = build(None)
        self.assertEqual([l["title"] for l in snap["lots"]], ["lot servi"])
        card = next(c for c in snap["overview"] if c["project"] == "demo")
        self.assertEqual(card["lots_open"], 1)
        self.assertEqual(snap["decisions"], [])
        self.assertEqual(build("demo")["project"], "demo")

        ro = db_mod.connect(__import__("dataclasses").replace(self.cfg, read_only=True))
        try:
            with self.assertRaises(db_mod.DbError):
                ro.execute("UPDATE work_items SET title = 'x' WHERE id = %s", (item["id"],))
        finally:
            ro.close()
        self.assertEqual(work.get(self.db, item["id"])["title"], "lot servi")

    def test_cli_options_incompatibles(self):
        env = self.env(HOME=self.tmp)
        for argv in (("--serve", "--json"), ("--serve", "--html", "x.html"),
                     ("--port", "9000"), ("--serve", "--port", "70000")):
            out = self.mesh("progress", *argv, env=env)
            self.assertEqual(out.returncode, 2, argv)


class ConfigTest(unittest.TestCase):
    def test_lecture_seule_au_demarrage(self):
        cfg = config_mod.Config(statement_timeout_ms=0)
        self.assertNotIn("read_only", db_mod.startup_options(cfg))
        ro = __import__("dataclasses").replace(cfg, read_only=True)
        self.assertEqual(db_mod.startup_options(ro), "-c default_transaction_read_only=on")


if __name__ == "__main__":
    unittest.main()
