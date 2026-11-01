# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh progress --serve` (L304) : serveur local en lecture seule.

Sans base : liaison 127.0.0.1, jeton (fragment de l'URL → en-tête
Authorization, aucun cookie, temps constant, jamais journalisé ni renvoyé
dans une page d'erreur), en-tête Host, 405 hors GET/HEAD, forme du
JSON, cache, `?project=`. Sur Postgres réel : l'instantané lu par une session
en lecture seule, les cartes `overview` et la CLI.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import re
import threading
import unittest
from unittest import mock

from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh import progress, progress_serve, registry, serve, work

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

    def request(self, method, path, *, host=None, auth=None, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = dict(headers or {}, Host=host or "127.0.0.1:%d" % self.port)
        if auth:
            headers["Authorization"] = auth
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp, resp.read()
        finally:
            conn.close()

    def auth(self):
        return "Bearer %s" % self.server.token

    def raw(self, line: bytes) -> bytes:
        """Une requête brute (mal formée au besoin) ; la réponse entière."""
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(line)
            out = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return out
                out += chunk


class StaleSnapshotTest(unittest.TestCase):
    """§16 (#3889) : une lecture en panne rend le dernier instantané avec son
    âge, jamais une erreur ; sans instantané, le serveur dit « en cours »."""

    def setUp(self):
        self.clock = [0.0]
        self.fail = None
        self.faults = []

        def build(project):
            if self.fail is not None:
                raise self.fail
            return _projects_snap(project)
        self.snapshots = progress_serve.Snapshots(build, clock=lambda: self.clock[0],
                                                  on_fault=self.faults.append)

    def test_dernier_instantane_marque_stale(self):
        first = self.snapshots.get(None)
        self.assertNotIn("stale", first)
        self.clock[0] = 60.0
        self.fail = db_mod.Unavailable("connection refused to 10.0.0.1")
        again = self.snapshots.get(None)
        self.assertTrue(again["stale"])
        self.assertEqual(again["generated_ts"], first["generated_ts"])
        self.assertEqual(len(self.faults), 1)
        # le retour de la base rend un instantané frais
        self.fail = None
        self.assertNotIn("stale", self.snapshots.get(None))

    def test_sans_instantane_le_serveur_dit_en_cours(self):
        self.fail = RuntimeError("secret-detail")
        server = progress_serve.make_server(self.snapshots, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        with mock.patch("sys.stderr"):
            conn.request("GET", progress_serve.API, headers={
                "Host": "127.0.0.1:%d" % server.server_address[1],
                "Authorization": "Bearer %s" % server.token})
            resp = conn.getresponse()
            body = resp.read().decode()
        conn.close()
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.getheader("Retry-After"), str(progress_serve.RETRY_AFTER))
        self.assertNotIn("secret-detail", body)
        self.assertNotIn("error", body)
        self.assertEqual([type(e) for e in self.faults], [RuntimeError])


class FaultReporterTest(unittest.TestCase):
    """§16 (#3889) : les pannes de la page deviennent des lots `ameesh` par la
    primitive de #3885, en arrière-plan, sans jamais lever."""

    def setUp(self):
        import shutil
        import tempfile
        from ameesh import server_faults
        directory = tempfile.mkdtemp(prefix="ameesh-progress-faults-")
        self.addCleanup(shutil.rmtree, directory, True)
        self.journal = server_faults.Journal(directory + "/faults.json", "ameesh-progress",
                                             revision="1")
        self.opened, self.down = [], [True]

        def write(fn):
            if self.down[0]:
                raise db_mod.Unavailable("connection refused")
            return fn("db")

        def open_lot(db, **fields):
            self.opened.append(fields)
            return {"lot": 7, "created": True}
        self.reporter = progress_serve.FaultReporter(self.journal, write, report=open_lot)

    def test_base_en_panne_puis_rapport_a_la_reprise(self):
        from ameesh import server_faults
        self.reporter.record("GET " + progress_serve.API, db_mod.Unavailable("x"),
                             subject=server_faults.HUMAN)
        with mock.patch("sys.stderr"):
            self.reporter.kick(wait=True)
        self.assertEqual(self.opened, [])
        self.assertTrue(self.journal.pending)
        self.down[0] = False
        self.reporter.kick(wait=True)
        [fields] = self.opened
        self.assertEqual((fields["app"], fields["users"], fields["route"]),
                         ("ameesh", 1, "ameesh-progress GET " + progress_serve.API))
        self.assertFalse(self.journal.pending)
        # au plus un rapport par intervalle hors panne
        self.reporter.record("GET " + progress_serve.API, db_mod.Unavailable("x"))
        self.reporter.kick(wait=True, every=3600)
        self.assertEqual(len(self.opened), 1)


class ServeTest(ServerCase):
    def test_lie_a_127_0_0_1_seulement(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(progress_serve.BIND, "127.0.0.1")
        self.assertNotIn("host", progress_serve.make_server.__code__.co_varnames)

    def test_jeton_en_en_tete_jamais_en_cookie(self):
        # la page vient sans jeton, mais sans données
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.getheader("Set-Cookie"))
        self.assertIn('<script type="application/json" id="data">null</script>', body.decode())
        self.assertEqual(self.calls, [])
        # l'API exige le jeton, en en-tête Authorization seulement
        for auth in (None, "Bearer faux", "Bearer " + self.server.token + "x",
                     "Basic " + self.server.token, self.server.token):
            resp, _ = self.request("GET", progress_serve.API, auth=auth)
            self.assertEqual(resp.status, 401, auth)
            self.assertEqual(resp.getheader("WWW-Authenticate"), "Bearer")
        # ni l'URL ni un cookie ne le remplacent (un autre serveur local, sur un
        # autre port, recevrait les cookies de 127.0.0.1)
        self.assertEqual(self.request("GET", progress_serve.API + "?t=" + self.server.token)[0].status, 401)
        resp, _ = self.request("GET", progress_serve.API, headers={
            "Cookie": "ameesh_progress_%d=%s" % (self.port, self.server.token)})
        self.assertEqual(resp.status, 401)
        resp, _ = self.request("GET", progress_serve.API, auth=self.auth())
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.getheader("Set-Cookie"))
        self.assertEqual(self.calls, [None])

    def test_url_au_jeton_dans_le_fragment(self):
        url = progress_serve.url_of(self.server)
        self.assertEqual(url, "http://127.0.0.1:%d/#t=%s" % (self.port, self.server.token))

    def test_requete_mal_formee_sans_echo(self):
        # le module standard recopie la ligne reçue dans la page 400 et le journal
        for line in (b"GET /api/progress.json?t=" + self.server.token.encode() + b" HTTP/1.1 x\r\n\r\n",
                     b"GET /?t=" + self.server.token.encode() + b" HTTP/9.9\r\n\r\n",
                     b"G\tET /" + self.server.token.encode() + b"\r\n\r\n"):
            out = self.raw(line)
            self.assertRegex(out, rb"\b(400|505)\b")
            self.assertNotIn(self.server.token.encode(), out)
        self.assertNotIn(self.server.token, self.stderr.getvalue())

    def test_jeton_compare_en_temps_constant(self):
        with mock.patch("hmac.compare_digest", wraps=__import__("hmac").compare_digest) as cd:
            self.request("GET", progress_serve.API, auth=self.auth())
        self.assertTrue(cd.called)

    def test_jeton_neuf_a_chaque_serveur(self):
        other = progress_serve.make_server(self.snapshots, port=0)
        try:
            self.assertNotEqual(other.token, self.server.token)
            self.assertGreaterEqual(len(other.token), 32)
        finally:
            other.server_close()

    def test_jeton_jamais_journalise(self):
        self.request("GET", progress_serve.API + "?t=%s" % self.server.token)
        self.request("GET", progress_serve.API, auth="Bearer " + self.server.token[:-1])
        self.request("GET", progress_serve.API, auth=self.auth())
        log = self.stderr.getvalue()
        self.assertIn("GET /api/progress.json", log)
        self.assertNotIn(self.server.token, log)
        self.assertNotIn(self.server.token[:-1], log)

    def test_hote_refuse(self):
        for host in ("ailleurs.example:%d" % self.port, "127.0.0.1:1", "127.0.0.1"):
            resp, _ = self.request("GET", "/", host=host, auth=self.auth())
            self.assertEqual(resp.status, 403, host)
        resp, _ = self.request("GET", progress_serve.API, host="localhost:%d" % self.port,
                               auth=self.auth())
        self.assertEqual(resp.status, 200)

    def test_lecture_seule(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            resp, _ = self.request(method, progress_serve.API, auth=self.auth(), body=b"{}")
            self.assertEqual(resp.status, 405, method)
            self.assertEqual(resp.getheader("Allow"), "GET, HEAD")
        self.assertEqual(self.calls, [])

    def test_page_servie(self):
        resp, body = self.request("GET", "/", auth=self.auth())
        self.assertEqual(resp.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        html = body.decode("utf-8")
        self.assertIn('var MODE = "served";', html)
        self.assertIn("connect-src 'self'", html)
        self.assertNotIn("</script><b>", html)
        head, _ = self.request("HEAD", "/", auth=self.auth())
        self.assertEqual(head.status, 200)

    def test_json_et_projet(self):
        resp, body = self.request("GET", progress_serve.API, auth=self.auth())
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json; charset=utf-8")
        data = json.loads(body)
        self.assertEqual(data["schema"], "ameesh-progress/1")
        for key in ("overview", "decisions", "lots", "agents", "generated_ts"):
            self.assertIn(key, data)
        resp, body = self.request("GET", progress_serve.API + "?project=demo",
                                  auth=self.auth())
        self.assertEqual(json.loads(body)["project"], "demo")
        for bad in ("inconnu", "../x", "a" * 80):
            resp, _ = self.request("GET", progress_serve.API + "?project=" + bad,
                                   auth=self.auth())
            self.assertEqual(resp.status, 400, bad)
        self.assertEqual(self.request("GET", "/ailleurs", auth=self.auth())[0].status, 404)

    def test_cache(self):
        self.request("GET", progress_serve.API, auth=self.auth())
        self.request("GET", progress_serve.API, auth=self.auth())
        self.assertEqual(self.calls, [None])
        self.clock[0] += progress_serve.CACHE_S
        self.request("GET", progress_serve.API, auth=self.auth())
        self.assertEqual(self.calls, [None, None])


def _csp(html):
    return re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', html).group(1)


class PageBuilderTest(unittest.TestCase):
    """Le gabarit (L304) : modes, politique de sécurité, onglets, fenêtre."""

    def test_statique_bouton_desactive(self):
        html = progress.render_html(_snap())
        self.assertIn('var MODE = "static";', html)
        self.assertIn("open with --serve to refresh", html)
        self.assertNotIn("connect-src", _csp(html))

    def test_servie_relit_l_api_seulement(self):
        html = progress.render_html(None, served=True)
        self.assertIn('var MODE = "served";', html)
        csp = _csp(html)
        self.assertEqual(csp, "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
                              "script-src 'unsafe-inline'; img-src data:")
        self.assertIn('var API = "%s"' % progress_serve.API, html)
        # servie, la page vient sans données
        blob = re.search(r'<script type="application/json" id="data">(.*?)</script>',
                         html, re.S).group(1)
        self.assertIsNone(json.loads(blob))
        # le jeton part en en-tête, jamais en cookie, et quitte l'URL
        self.assertIn('"Authorization": "Bearer " + TOKEN', html)
        self.assertIn('credentials: "omit"', html)
        self.assertIn("sessionStorage", html)
        self.assertNotIn("same-origin", html)

    def test_gantt_groupes_et_zoom(self):
        # L304 PR 3 : groupes repliables, zoom semaine/jour gardé dans l'URL
        html = progress.render_html(_snap())
        self.assertIn('el("details", "rm-gr")', html)
        self.assertIn('"z=day"', html)
        self.assertIn('[["week", "week"], ["day", "day"]]', html)
        self.assertIn("delivered and abandoned (", html)
        # replié sauf s'il porte une tâche en retard (« en cours » ne suffit pas)
        self.assertIn("group(g.key, title, count, sum, nLate > 0 ||", html)
        # r1 : une epic sans tâche garde son groupe ; fenêtre de jalon du min au
        # max de ses dates, en jours calendaires (changement d'heure)
        self.assertIn("if (hasTask[e.id]) return;", html)
        # r2 : une epic planifiée garde son groupe quand ses tâches finies sont
        # masquées ; pas de prototype (epic « constructor ») ; pas de « livré »
        # vert pour un groupe sans tâche
        self.assertIn("if (!shown.length && g.rows.length && !(g.epic && hasPlan(g.epic.planned))) return;", html)
        # lot #396 : « planned » est toujours là, toutes dates nulles l'epic
        # n'est pas planifiée ; un projet « __proto__ » reste dans le sélecteur
        self.assertIn("function hasPlan(p) { return !!(p && (p.start || p.end || p.delivery)); }", html)
        self.assertIn("var seen = Object.create(null);", html)
        self.assertNotIn("var seen = {};", html)
        # un harnais « constructor » sans comptes déclarés garde sa jauge
        self.assertIn('covered = Object.create(null);', html)
        self.assertNotIn('covered = {};', html)
        self.assertIn("var hasTask = Object.create(null);", html)
        self.assertIn('open.length || !g.rows.length ? "" : " done"', html)
        self.assertIn("f = addDays(midnight(Math.min.apply(null, mts)), -1);", html)
        self.assertIn("t = addDays(midnight(Math.max.apply(null, mts)), 2);", html)
        self.assertNotIn('x.state === "en_cours"; });', html)

    def test_onglets_et_fenetre(self):
        html = progress.render_html(_snap())
        for tab in ("overview", "gantt", "lots", "agents", "budget", "decisions"):
            self.assertIn('id="panel-%s"' % tab, html)
            self.assertIn('["%s", ' % tab, html)
        self.assertIn('<dialog id="modal"', html)
        # rien n'entre en HTML : le script ne passe que par textContent
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("insertAdjacentHTML", html)
        self.assertNotIn("document.write", html)

    def test_gabarit_sans_emplacement(self):
        snap = _snap()
        with mock.patch("builtins.open", mock.mock_open(read_data="<html></html>")):
            with self.assertRaises(progress.ProgressError):
                progress.render_html(snap)


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
                          "lots_merged": 1, "lots_total": 2, "progress_active": None,
                          "lots_active": None, "lots_waiting_activation": None,
                          "lots_unmeasured": None}})
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


class ConversationPgTest(PgTestCase):
    """L83 : la fiche 0024 lit la conversation et envoie un message — le
    parcours visible de la page servie, du GET au courrier déposé."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE api_idempotency, agent_mailbox, conversation_log,"
                        " conversation_heads CASCADE")
        registry.upsert(self.db, "a1", chantier="demo", harness="codex", host=self.cfg.host)
        snapshots = progress_serve.Snapshots(lambda project: _projects_snap(project))
        self.server = progress_serve.make_server(
            snapshots, port=0, chat=progress_serve.db_chat(self.cfg))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, *, auth=None, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        head = dict(headers or {}, Host="127.0.0.1:%d" % self.port)
        if auth:
            head["Authorization"] = auth
        try:
            conn.request(method, path, body=body, headers=head)
            resp = conn.getresponse()
            return resp, resp.read()
        finally:
            conn.close()

    def auth(self):
        return "Bearer %s" % self.server.token

    def conversation(self, agent="a1"):
        resp, body = self.request("GET", "/api/conversation.json?agent=" + agent, auth=self.auth())
        self.assertEqual(resp.status, 200, body)
        return json.loads(body)

    def post(self, payload, *, key="web-0000000001", auth=None, path="/api/conversation.json"):
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        resp, out = self.request("POST", path, auth=auth if auth is not None else self.auth(),
                                 body=body, headers=headers)
        try:
            data = json.loads(out) if out else {}
        except ValueError:
            data = {}
        return resp, data

    def test_la_fiche_lit_et_envoie(self):
        # la page servie porte le branchement : section, rendu et saisie
        resp, html = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        page = html.decode("utf-8")
        self.assertIn('section(box, "Conversation")', page)
        self.assertIn("agentConversation(name)", page)
        self.assertIn("/api/conversation.json", page)
        self.assertIn("Idempotency-Key", page)
        # lecture : vide au départ, au schéma de L78
        page = self.conversation()
        self.assertEqual((page["schema"], page["entries"]), ("ameesh-conversation/1", []))
        # envoi : une entrée, un courrier, la copie R12 une seule fois
        resp, out = self.post({"agent": "a1", "text": "bonjour depuis la fiche"})
        self.assertEqual(resp.status, 200, out)
        self.assertEqual((out["schema"], out["action"]), ("ameesh-action/1", "chat"))
        self.assertEqual(resp.getheader("Idempotent-Replayed"), None)
        rows = self.db.query("select sender, recipient, body, payload::text as payload"
                             " from agent_mailbox order by id")
        self.assertEqual(len(rows), 1)
        # #286 L77/B1 : la fiche écrit l'humain normalisé `human:<id>`, la
        # forme que `resident_queue.identity_for` vérifie.
        sender = serve.owner(self.cfg, None)
        self.assertEqual((rows[0]["sender"], rows[0]["recipient"]), (sender, "a1"))
        self.assertTrue(json.loads(rows[0]["payload"])["human"])
        entries = self.conversation()["entries"]
        self.assertEqual([e["text"] for e in entries], ["bonjour depuis la fiche"])
        self.assertEqual(entries[0]["kind"], "human_message")
        # rejeu de la même clé : la première réponse, rien de nouveau
        resp, again = self.post({"agent": "a1", "text": "bonjour depuis la fiche"})
        self.assertEqual((resp.status, again, resp.getheader("Idempotent-Replayed")),
                         (200, out, "true"))
        self.assertEqual(len(self.db.query("select 1 from agent_mailbox")), 1)
        self.assertEqual(len(self.conversation()["entries"]), 1)

    def test_les_gardes(self):
        # jeton requis, des deux côtés
        resp, _ = self.request("GET", "/api/conversation.json?agent=a1")
        self.assertEqual(resp.status, 401)
        resp, _ = self.post({"agent": "a1", "text": "x"}, auth="")
        self.assertEqual(resp.status, 401)
        # une seule écriture : le reste du serveur reste en lecture seule
        resp, _ = self.request("POST", "/api/progress.json", auth=self.auth(),
                               body=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual((resp.status, resp.getheader("Allow")), (405, "GET, HEAD"))
        # clé d'idempotence, corps et champs
        body = json.dumps({"agent": "a1", "text": "x"}).encode("utf-8")
        resp, _ = self.request("POST", "/api/conversation.json", auth=self.auth(), body=body,
                               headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)
        resp, _ = self.post({"agent": "a1", "text": "x", "surprise": 1})
        self.assertEqual(resp.status, 400)
        resp, _ = self.post({"agent": "a1", "text": "x", "author": "human:autre"})
        self.assertEqual(resp.status, 400)
        resp, out = self.post({"agent": "ghost", "text": "x"}, key="web-0000000009")
        self.assertEqual((resp.status, out["error"]), (404, "not_found"))
        self.assertEqual(self.db.query("select 1 from agent_mailbox"), [])


class ConversationGuardTest(unittest.TestCase):
    """#1673 : sans base, la saisie de la fiche suit le parseur de L87 et
    répond toujours, même sur une erreur imprévue de l'écriture."""

    def setUp(self):
        self.calls = []
        self.fail_with = None

        def read(query):
            if self.fail_with is not None:
                raise self.fail_with
            return {"entries": []}

        def write(key, body):
            self.calls.append(body)
            if self.fail_with is not None:
                raise self.fail_with
            return 200, {"ok": True}, False
        snapshots = progress_serve.Snapshots(lambda project: _projects_snap(project))
        self.server = progress_serve.make_server(snapshots, port=0, chat=(read, write))
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(5)
        self.addCleanup(stop)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": "127.0.0.1:%d" % self.port,
                   "Authorization": "Bearer %s" % self.server.token,
                   "Content-Type": "application/json", "Idempotency-Key": "web-0000000001"}
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp, resp.read().decode("utf-8")
        finally:
            conn.close()

    def test_cle_dupliquee_refusee_comme_l87(self):
        resp, _ = self.request("POST", progress_serve.CHAT,
                               b'{"agent": "a1", "text": "x", "text": "y"}')
        self.assertEqual(resp.status, 400)
        resp, _ = self.request("POST", progress_serve.CHAT,
                               b'{"agent": "a1", "text": NaN}')
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.calls, [])

    def test_erreur_imprevue_de_l_ecriture_repond_500(self):
        self.fail_with = KeyError("secret-detail")
        with mock.patch("sys.stderr"):
            resp, body = self.request("POST", progress_serve.CHAT,
                                      b'{"agent": "a1", "text": "x"}')
        self.assertEqual(resp.status, 500)
        self.assertIn("may have been delivered", body)
        self.assertNotIn("secret-detail", body)
        self.assertEqual(len(self.calls), 1)

    def test_erreur_imprevue_de_la_lecture_repond_503_a_reessayer(self):
        """§16 (#3889) : une lecture en panne n'est pas une erreur montrée."""
        self.fail_with = KeyError("secret-detail")
        with mock.patch("sys.stderr"):
            resp, body = self.request("GET", progress_serve.CHAT + "?agent=a1")
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.getheader("Retry-After"), str(progress_serve.RETRY_AFTER))
        self.assertNotIn("secret-detail", body)
        self.assertNotIn("error", body)

    def test_405_dit_ce_que_dit_allow(self):
        resp, body = self.request("PUT", progress_serve.CHAT, b"{}")
        self.assertEqual((resp.status, resp.getheader("Allow")), (405, "GET, HEAD, POST"))
        self.assertIn("GET, HEAD and POST", body)
        resp, body = self.request("POST", progress_serve.API, b"{}")
        self.assertEqual((resp.status, resp.getheader("Allow")), (405, "GET, HEAD"))
        self.assertIn("GET and HEAD only", body)
        self.assertNotIn("POST %s" % progress_serve.API, body)


class ConfigTest(unittest.TestCase):
    def test_lecture_seule_au_demarrage(self):
        cfg = config_mod.Config(statement_timeout_ms=0)
        self.assertNotIn("read_only", db_mod.startup_options(cfg))
        ro = __import__("dataclasses").replace(cfg, read_only=True)
        # #3503 : jit=off est toujours le dernier réglage du paquet de démarrage
        self.assertEqual(db_mod.startup_options(ro),
                         "-c default_transaction_read_only=on -c jit=off")


if __name__ == "__main__":
    unittest.main()
