# SPDX-License-Identifier: AGPL-3.0-only
"""L111 : relais de modèle du serveur du mesh (étude de l'exécuteur médié §5).

Bout en bout contre un FAUX fournisseur local (protocole Messages, flux SSE) :
aucune clé réelle, aucun réseau. Le relais tourne dans le processus, sur la
base de test ; l'appareil est un client HTTP (ou le vrai `dsh` quand le banc
le fournit, `AMEESH_TEST_DSH_BIN`).
"""
from __future__ import annotations

import dataclasses
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ameesh import relay
from ameesh.storage import of as storage_of

from .support import PgTestCase

SERVER_KEY = "cle-serveur-factice-0000"   # jamais une vraie clé
TOKEN = "jeton-session-a1"


class FakeProvider:
    """Faux fournisseur compatible Messages : rejoue un flux, note les requêtes."""

    def __init__(self, *, usage_start=None, usage_end=None, cut_after: int | None = None):
        self.requests: list[dict] = []
        self.usage_start = usage_start or {"input_tokens": 1200, "cache_read_input_tokens": 800,
                                           "output_tokens": 0}
        self.usage_end = usage_end or {"output_tokens": 300}
        self.cut_after = cut_after
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                provider.requests.append({"path": self.path, "headers": dict(self.headers),
                                          "body": json.loads(body)})
                req = provider.requests[-1]["body"]
                events = [
                    ("message_start", {"type": "message_start", "message": {
                        "id": "msg_1", "type": "message", "role": "assistant",
                        "model": req.get("model"), "content": [],
                        "usage": provider.usage_start}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "text_delta", "text": "RELAIS-OK"}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta",
                                       "delta": {"stop_reason": "end_turn"},
                                       "usage": provider.usage_end}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                if not req.get("stream"):
                    payload = json.dumps({"id": "msg_1", "type": "message", "model": req["model"],
                                          "content": [{"type": "text", "text": "RELAIS-OK"}],
                                          "usage": dict(provider.usage_start,
                                                        **provider.usage_end)}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("request-id", "req_factice")
                self.send_header("x-secret-amont", "ne-pas-recopier")
                self.end_headers()
                for index, (name, data) in enumerate(events):
                    if provider.cut_after is not None and index >= provider.cut_after:
                        break
                    self.wfile.write(("event: %s\ndata: %s\n\n" % (name, json.dumps(data)))
                                     .encode())
                    self.wfile.flush()
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d/anthropic" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def identity(agent="a1", *, epoch=7, expires=None, turn=None) -> relay.Identity:
    return relay.Identity(executor="exec-7f3a", host="appareil-1", agent=agent,
                          lease_owner="exec:exec-7f3a:appareil-1:4121", lease_epoch=epoch,
                          turn=turn, expires_ts=expires)


class RelaisTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        for table in ("turn_costs", "budget_limits", "budget_events"):
            self.db.execute("DELETE FROM %s" % table)
        self.provider = FakeProvider()
        self.addCleanup(self.provider.close)
        self.start_relay()

    def start_relay(self, *, config=None, tokens=None, daily=None, cfg=None, provider=None):
        if getattr(self, "server", None) is not None:
            self.server.shutdown()
            self.server.server_close()
        provider = provider or self.provider
        tokens = tokens if tokens is not None else {
            TOKEN: identity(), "jeton-a2": identity("a2"),
            "jeton-echu": identity(expires=time.time() - 1)}
        config = config or relay.RelayConfig(upstreams={"deepseek": provider.url},
                                             max_tokens=4096, max_body=64 * 1024)
        self.relay = relay.Relay(
            verifier=relay.StaticTokens(tokens),
            policy=relay.StaticPolicy({"a1": ["deepseek-flash"], "a2": ["deepseek-*"]},
                                      daily=daily),
            db=self.db, cfg=cfg or dataclasses.replace(self.cfg, budget_usd_per_hour=10.0),
            secrets=relay.ServerSecrets({"DEEPSEEK_API_KEY": SERVER_KEY}),
            config=config, prices={"deepseek-flash": [0.27, 0.07, 1.10]})
        self.server = relay.make_server(self.relay, "127.0.0.1:0")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)
        self.port = self.server.server_address[1]

    def _stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def post(self, body, *, token=TOKEN, path="/api/exec/v1/llm/deepseek/v1/messages",
             headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = raw if raw is not None else json.dumps(body).encode()
        hdrs = {"content-type": "application/json", "anthropic-version": "2023-06-01",
                "x-deepseek-harness-session-id": "session-abc",
                "x-deepseek-harness-user-id": "identifiant-appareil"}
        if token is not None:
            hdrs["x-api-key"] = token
        hdrs.update(headers or {})
        conn.request("POST", path, body=payload, headers=hdrs)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def rows(self):
        return self.db.query("SELECT agent, harness, turn, model, session, usd, input_tokens, "
                             "cached_input_tokens, output_tokens, source, executor, lease_owner, "
                             "lease_epoch FROM turn_costs ORDER BY id")

    def body(self, **extra):
        out = {"model": "deepseek-flash", "max_tokens": 256000, "stream": True,
               "messages": [{"role": "user", "content": "bonjour"}]}
        out.update(extra)
        return out

    # -- flux, clé, mesure ---------------------------------------------------
    def test_flux_relaye_cle_du_serveur_et_usage_mesure(self):
        response, data = self.post(self.body(), headers={"x-ameesh-turn": "tour-42"})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("content-type").startswith("text/event-stream"))
        self.assertIsNone(response.getheader("x-secret-amont"))
        text = data.decode()
        self.assertIn("event: message_start", text)
        self.assertIn("RELAIS-OK", text)
        self.assertIn("event: message_stop", text)
        # le fournisseur voit la clé du serveur, jamais le jeton de l'appareil
        sent = self.provider.requests[-1]
        self.assertEqual(sent["path"], "/anthropic/v1/messages")
        self.assertEqual(sent["headers"].get("x-api-key"), SERVER_KEY)
        self.assertNotIn(TOKEN, json.dumps(sent))
        self.assertNotIn("x-deepseek-harness-user-id",
                         {k.lower() for k in sent["headers"]})
        self.assertEqual(sent["body"]["max_tokens"], 4096)      # plafonné
        # la clé du serveur ne revient jamais à l'appareil
        self.assertNotIn(SERVER_KEY, text)
        # usage mesuré : une ligne `relay`, rattachée à l'agent, au tour et au bail
        rows = self.wait_for(self.rows)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["agent"], row["harness"], row["source"]), ("a1", "deepseek", "relay"))
        self.assertEqual((row["turn"], row["model"], row["session"]),
                         ("tour-42", "deepseek-flash", "session-abc"))
        self.assertEqual((row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]),
                         (1200, 800, 300))
        self.assertEqual((row["executor"], row["lease_owner"], int(row["lease_epoch"])),
                         ("exec-7f3a", "exec:exec-7f3a:appareil-1:4121", 7))
        attendu = (1200 * 0.27 + 800 * 0.07 + 300 * 1.10) / 1e6
        self.assertAlmostEqual(float(row["usd"]), attendu, places=6)

    def test_reponse_sans_flux_mesuree(self):
        response, data = self.post(self.body(stream=False))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data)["content"][0]["text"], "RELAIS-OK")
        row = self.wait_for(self.rows)[0]
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (1200, 300))

    def test_flux_coupe_facture_par_exces(self):
        coupe = FakeProvider(cut_after=3)   # ni `message_delta`, ni fin
        self.addCleanup(coupe.close)
        self.start_relay(provider=coupe)
        response, _ = self.post(self.body())
        self.assertEqual(response.status, 200)
        row = self.wait_for(self.rows)[0]
        self.assertEqual(row["input_tokens"], 1200)
        # « RELAIS-OK » : 9 caractères → 5 jetons au moins (un pour deux)
        self.assertGreaterEqual(row["output_tokens"], 5)

    # -- refus ----------------------------------------------------------------
    def test_modele_non_admis_refuse_sans_appel_au_fournisseur(self):
        response, data = self.post(self.body(model="deepseek-v4-pro"))
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(data)["error"]["type"], "permission_error")
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.rows(), [])
        # un motif admet la famille pour a2
        response, _ = self.post(self.body(model="deepseek-v4-pro"), token="jeton-a2")
        self.assertEqual(response.status, 200)

    def test_jeton_invalide_ou_echu_refuse(self):
        for token in (None, "inconnu", "jeton-echu"):
            response, data = self.post(self.body(), token=token)
            self.assertEqual(response.status, 401, token)
            self.assertEqual(json.loads(data)["error"]["type"], "authentication_error")
        # `Authorization: Bearer` est aussi accepté
        response, _ = self.post(self.body(), token=None,
                                headers={"authorization": "Bearer " + TOKEN})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.provider.requests), 1)

    def test_plafonds_de_budget(self):
        # plafond de l'agent en base (L70)
        self.db.execute("INSERT INTO budget_limits (scope, window_s, usd, set_by) "
                        "VALUES ('a1', 3600, 1.0, 'human:test')")
        self.db.execute("INSERT INTO turn_costs (agent, harness, model, usd) "
                        "VALUES ('a1', 'deepseek', 'deepseek-flash', 1.5)")
        self.relay.budget_cache.invalidate()
        response, data = self.post(self.body())
        self.assertEqual(response.status, 402)
        self.assertIn("a1", json.loads(data)["error"]["message"])
        self.assertEqual(self.provider.requests, [])
        # a2 n'est pas visé par le plafond de a1
        response, _ = self.post(self.body(), token="jeton-a2")
        self.assertEqual(response.status, 200)
        # plafond horaire du mesh (configuration de l'hôte serveur)
        self.start_relay(cfg=dataclasses.replace(self.cfg, budget_usd_per_hour=1.0))
        response, _ = self.post(self.body(), token="jeton-a2")
        self.assertEqual(response.status, 402)

    def test_budget_par_jour_de_la_persona(self):
        self.db.execute("INSERT INTO turn_costs (agent, harness, model, usd, recorded_at) "
                        "VALUES ('a1', 'deepseek', 'deepseek-flash', 0.6, now() - interval '5 hours')")
        self.start_relay(daily={"a1": 0.5})
        response, _ = self.post(self.body())
        self.assertEqual(response.status, 402)

    def test_declaration_de_l_appareil_hors_plafond(self):
        # une ligne `device` (coût déclaré par l'appareil) ne compte pas
        storage_of(self.db).turn_costs.insert(
            agent="a1", harness="deepseek", turn="t", model="deepseek-flash", session=None,
            usd=50.0, input_tokens=0, cached_input_tokens=0, output_tokens=0, cum_usd=None,
            cum_input_tokens=None, cum_cached_input_tokens=None, cum_output_tokens=None,
            source="device")
        self.assertEqual(storage_of(self.db).turn_costs.spent(3600, agent="a1",
                                                              harnesses=("deepseek",)), 0.0)
        response, _ = self.post(self.body())
        self.assertEqual(response.status, 200)

    def test_taille_et_debit_bornes(self):
        response, _ = self.post(None, raw=b"{" + b" " * (70 * 1024) + b"}")
        self.assertEqual(response.status, 413)
        response, _ = self.post(None, raw=b"[1, 2]")
        self.assertEqual(response.status, 400)
        response, _ = self.post(self.body(), path="/api/exec/v1/llm/deepseek/v1/files")
        self.assertEqual(response.status, 404)
        self.start_relay(config=relay.RelayConfig(upstreams={"deepseek": self.provider.url},
                                                  rate_per_minute=2))
        statuts = [self.post(self.body())[0].status for _ in range(3)]
        self.assertEqual(statuts[:2], [200, 200])
        self.assertEqual(statuts[2], 429)

    def test_cle_absente_du_serveur(self):
        self.relay.secrets = relay.ServerSecrets({})
        response, _ = self.post(self.body())
        self.assertEqual(response.status, 503)
        self.assertEqual(self.provider.requests, [])

    def test_ecriture_en_echec_gardee_et_comptee(self):
        appels = []
        original = self.relay._write

        def en_panne(row):
            appels.append(row)
            return False
        self.relay._write = en_panne
        response, _ = self.post(self.body())
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.relay._unrecorded), 1)
        self.relay._write = original
        response, _ = self.post(self.body())
        self.assertEqual(response.status, 200)
        self.assertEqual(self.relay._unrecorded, [])
        self.assertEqual(len(self.wait_for(lambda: len(self.rows()) == 2 and self.rows())), 2)

    # -- vrai harnais (facultatif) ---------------------------------------------
    @unittest.skipUnless(os.environ.get("AMEESH_TEST_DSH_BIN"),
                         "AMEESH_TEST_DSH_BIN absent : essai avec le vrai dsh non lancé")
    def test_dsh_reel_par_le_relais(self):
        home = tempfile.mkdtemp(prefix="ameesh-test-dsh-")
        work = tempfile.mkdtemp(prefix="ameesh-test-dsh-ws-")
        self.addCleanup(shutil.rmtree, home, True)
        self.addCleanup(shutil.rmtree, work, True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("AMEESH_", "AGENT_MESH_", "DEEPSEEK_"))}
        env.update(relay.turn_env("deepseek", "http://127.0.0.1:%d/api/exec/v1/llm" % self.port,
                                  TOKEN))
        env["DSH_HOME"] = home
        proc = subprocess.run([os.environ["AMEESH_TEST_DSH_BIN"], "headless", "--json",
                               "Réponds OK"], cwd=work, env=env, capture_output=True,
                              text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("RELAIS-OK", proc.stdout)
        self.assertTrue(self.provider.requests)
        self.assertTrue(all(r["headers"].get("x-api-key") == SERVER_KEY
                            for r in self.provider.requests))
        rows = self.rows()
        self.assertTrue(rows and all(r["source"] == "relay" for r in rows))
        # dsh n'a écrit ni le jeton ni l'URL du relais sur disque
        for root, _dirs, files in os.walk(home):
            for name in files:
                with open(os.path.join(root, name), "rb") as fh:
                    self.assertNotIn(TOKEN.encode(), fh.read())


class RelaisSansBaseTest(unittest.TestCase):
    """Parties pures : environnement du tour côté appareil, secrets, mesure."""

    def tearDown(self):
        relay.set_session_token_source(None)

    def test_environnement_du_tour(self):
        env = {"DEEPSEEK_API_KEY": "cle-locale-heritee", "PATH": "/bin"}
        # exécuteur non médié : rien ne change
        self.assertFalse(relay.apply_turn_env(dict(env), "deepseek", agent="a1", owner="o",
                                              epoch=1, turn_id="t", environ={}))
        environ = {"AMEESH_EXEC_URL": "https://mesh.exemple.test/"}
        # médié sans source de jeton : refus, jamais de repli sur la clé locale
        with self.assertRaises(relay.RelayTurnError):
            relay.apply_turn_env(dict(env), "deepseek", agent="a1", owner="o", epoch=1,
                                 turn_id="t", environ=environ)
        demandes = []

        def source(agent, owner, epoch, turn_id):
            demandes.append((agent, owner, epoch, turn_id))
            return "jeton-court"
        relay.set_session_token_source(source)
        out = dict(env)
        self.assertTrue(relay.apply_turn_env(out, "deepseek", agent="a1", owner="o", epoch=3,
                                             turn_id="t9", environ=environ))
        self.assertEqual(out["DEEPSEEK_API_KEY"], "jeton-court")
        self.assertEqual(out["DEEPSEEK_BASE_URL"],
                         "https://mesh.exemple.test/api/exec/v1/llm/deepseek")
        self.assertEqual(demandes, [("a1", "o", 3, "t9")])
        # un harnais au forfait n'est pas relayé
        self.assertFalse(relay.apply_turn_env(dict(env), "claude", agent="a1", owner="o",
                                              epoch=1, turn_id="t", environ=environ))

        def panne(*_args):
            raise OSError("serveur injoignable")
        relay.set_session_token_source(panne)
        with self.assertRaises(relay.RelayTurnError):
            relay.apply_turn_env(dict(env), "deepseek", agent="a1", owner="o", epoch=1,
                                 turn_id="t", environ=environ)

    def test_politique_du_canon(self):
        from types import SimpleNamespace
        from ameesh.canon import HostPolicy

        def persona(title, **kw):
            base = dict(title=title, harness="deepseek", model=None, provider=None,
                        credential_mode=None, budget_usd_per_day=None)
            base.update(kw)
            return SimpleNamespace(**base)
        canon = SimpleNamespace(
            agents=[persona("a1", model="deepseek-flash", budget_usd_per_day=2.0),
                    persona("a2"), persona("a3", model="deepseek-v4-pro"),
                    persona("orch", harness="claude", model="claude-x")],
            hosts=[SimpleNamespace(title="appareil-1",
                                   policy=HostPolicy(models=["deepseek-flash"]))])
        policy = relay.CanonPolicy(lambda: canon)
        self.assertEqual(policy.refusal(identity("a1"), "deepseek-flash"), "")
        self.assertIn("non admis", policy.refusal(identity("a1"), "deepseek-v4-pro"))
        # sans modèle déclaré : le défaut du descripteur (deepseek-flash)
        self.assertEqual(policy.admitted(identity("a2")), ["deepseek-flash"])
        # modèle de la persona hors de la politique de l'hôte : rien d'admis
        self.assertTrue(policy.refusal(identity("a3"), "deepseek-v4-pro"))
        self.assertIn("harnais", policy.refusal(identity("orch"), "claude-x"))
        self.assertIn("absente", policy.refusal(identity("inconnu"), "deepseek-flash"))
        autre_hote = dataclasses.replace(identity("a1"), host="ailleurs")
        self.assertIn("absent", policy.refusal(autre_hote, "deepseek-flash"))
        self.assertEqual(policy.daily_usd(identity("a1")), 2.0)

    def test_secrets_fichier_prive(self):
        directory = tempfile.mkdtemp(prefix="ameesh-test-relay-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "deepseek.key")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("cle-fichier\n")
        os.chmod(path, 0o644)
        secrets = relay.ServerSecrets({"AMEESH_RELAY_DEEPSEEK_KEY_FILE": path,
                                       "DEEPSEEK_API_KEY": "cle-env"})
        with self.assertRaises(relay.SecretError):
            secrets.key("deepseek")
        os.chmod(path, 0o600)
        self.assertEqual(secrets.key("deepseek"), "cle-fichier")
        self.assertEqual(relay.ServerSecrets({"DEEPSEEK_API_KEY": "cle-env"}).key("deepseek"),
                         "cle-env")

    def test_amont_en_clair_refuse_hors_boucle_locale(self):
        relay.check_upstream("http://127.0.0.1:9/anthropic")
        relay.check_upstream("https://api.deepseek.com/anthropic")
        with self.assertRaises(ValueError):
            relay.check_upstream("http://api.exemple.test/anthropic")

    def test_mesure_garde_le_maximum(self):
        meter = relay.UsageMeter("deepseek-flash")
        meter.sse_line(b'data: {"type":"message_start","message":{"model":"deepseek-flash",'
                       b'"usage":{"input_tokens":10,"cache_read_input_tokens":4}}}\n')
        meter.sse_line(b'data: {"type":"message_delta","usage":{"input_tokens":10,'
                       b'"output_tokens":7}}\n')
        usage = relay.settle(meter.usage, 1000, 100)
        self.assertEqual((usage.input_tokens, usage.cache_read_tokens, usage.output_tokens),
                         (10, 4, 7))
        # sans aucun relevé : estimation par excès depuis la taille de la requête
        vide = relay.settle(relay.Usage(model="m"), 900, 100)
        self.assertEqual(vide.input_tokens, 300)


if __name__ == "__main__":
    unittest.main()
