# SPDX-License-Identifier: AGPL-3.0-only
"""L108 : le serveur de l'API d'exécuteur médiée (`/api/exec/v1`).

Sur une base temporaire (schéma jetable de `PgTestCase`) :

* jeux dorés de L107 rejoués contre le serveur réel (statut, code d'erreur,
  forme du résultat, rejeu d'idempotence, bail perdu) ;
* portée : chaque opération à agent refusée hors des agents admis, hôte
  forcé, rendus filtrés, opérations refusées (aucun reçu ni approbation) ;
* fencing : bail perdu, échu, d'un autre exécuteur ou d'un autre hôte →
  valeur de refus et rien d'écrit ; jeton de session mort → 401 ;
* idempotence : rejeu, 409, clé par exécuteur ;
* flux : filtrage par hôte, curseurs, trou de reprise, SSE, LISTEN réel ;
* porte d'hôte : réclamation refusée hors disponibilité ;
* bornes de taille et de débit, audit, serveur HTTP de bout en bout.
"""
from __future__ import annotations

import copy
import glob
import http.client
import json
import os
import threading
import time
import unittest

from ameesh import db as db_mod
from ameesh import storage
from ameesh.executeur_mediee import contrat as C
from ameesh.executeur_mediee import evenements as E
from ameesh.executeur_mediee.bouchon import StaticAuth
from ameesh.executeur_mediee.flux import EventHub
from ameesh.executeur_mediee.interfaces import AuthError, Principal
from ameesh.executeur_mediee.repartiteur import ConnectionPool, PgDispatcher
from ameesh.executeur_mediee.serveur import ExecApp, ExecHTTPServer

from tests.support import PgTestCase

DORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dore", "executeur_mediee")
HOST = "anna-portable"
EXEC = "7f3a"
OWNER = "exec:7f3a:anna-portable:4121"
TOKEN = "amx1." + "A" * 43
SESSION = "ams1." + "B" * 43
OTHER_TOKEN = "amx1." + "C" * 43          # autre exécuteur, autre hôte
SAME_HOST_OTHER_EXEC = "amx1." + "D" * 43  # autre exécuteur du même hôte
STALE_SESSION = "ams1." + "E" * 43         # jeton de session d'une epoch passée
FAR = 4e9


def _golden_cases() -> list:
    cases = []
    for path in sorted(glob.glob(os.path.join(DORE, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            cases.extend(json.load(fh).get("cas") or ())
    return cases


class FakeAuth(StaticAuth):
    """Faux fournisseur d'identité : jetons fixes, plus un mode forcé."""

    force: str | None = None

    def verify(self, token):
        if self.force:
            raise AuthError(self.force)
        return super().verify(token)


class BrokenPool:
    def connection(self, timeout=10.0):
        raise db_mod.Unavailable("base coupée (essai)")


class Deny:
    def allow(self, key):
        return False


class ServeurExecTest(PgTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pool = ConnectionPool(lambda: db_mod.connect(cls.cfg), size=4)
        cls.contract = C.load()

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.auth = FakeAuth()
        self.auth.add(TOKEN, Principal("executor", EXEC, "mesh-exemple", HOST, expires_ts=FAR))
        self.auth.add(SESSION, Principal("session", EXEC, "mesh-exemple", HOST,
                                         agent="inge-front", epoch=42, expires_ts=FAR))
        self.auth.add(STALE_SESSION, Principal("session", EXEC, "mesh-exemple", HOST,
                                               agent="inge-front", epoch=41, expires_ts=FAR))
        self.auth.add(OTHER_TOKEN, Principal("executor", "0000", "mesh-exemple", "autre-hote",
                                             expires_ts=FAR))
        self.auth.add(SAME_HOST_OTHER_EXEC, Principal("executor", "9b9b", "mesh-exemple", HOST,
                                                      expires_ts=FAR))
        self.dispatcher = PgDispatcher(self.pool)
        self.hub = EventHub(None, stream="k3f9")
        self.app = ExecApp(self.dispatcher, self.auth, self.hub, mesh="mesh-exemple")
        self.reset()

    def tearDown(self):
        self.hub.stop()

    # -- état de la base ----------------------------------------------------
    def reset(self):
        self.db.execute(
            "TRUNCATE agent_registry, agent_mailbox, work_items, work_item_events,"
            " exec_idempotency, exec_host_availability, exec_audit, canon_state,"
            " turn_resources, host_resources, quota_gauge_readings, budget_limits,"
            " turn_costs, thread_index, actions, spend_pending RESTART IDENTITY CASCADE")
        self.db.execute("INSERT INTO canon_state (host, status) VALUES (%s, 'ok'), ('autre-hote', 'ok')",
                        (HOST,))
        self.agent("inge-front", HOST, team="ingénieur")
        self.agent("coord", "serveur", team="coordinateur")
        self.agent("tresorier", "autre-hote")
        self.agent("manuel", HOST, governed=False)
        self.hold("inge-front", OWNER, 42)
        self.db.execute(
            "INSERT INTO agent_mailbox (id, sender, recipient, body, kind, work_item_id)"
            " VALUES (812, 'coord', 'inge-front', 'Lot 812 : relis la PR.', 'request', '812'),"
            "        (900, 'coord', 'tresorier', 'secret', 'request', NULL)")
        self.db.execute(
            "INSERT INTO work_items (id, type, title, state, assignee, app)"
            " VALUES (812, 'evolution', 'Page d''accueil', 'build', 'inge-front', 'site'),"
            "        (900, 'evolution', 'Compta', 'build', 'tresorier', 'compta')")
        self.availability("available", 1)

    def agent(self, name, host, *, team="", governed=True):
        self.db.execute(
            "INSERT INTO agent_registry (name, chantier, harness, host, cwd, status, mode,"
            " canon_ref, team, credential_mode, lease_epoch)"
            " VALUES (%s, 'site', 'dsh', %s, %s, 'idle', 'execute', %s, %s, 'relay', 41)",
            (name, host, "/var/lib/ameesh-exec/work/%s/41" % name,
             "canon@abc" if governed else None, team))
        self.db.execute(
            "UPDATE agent_registry SET placement_ok = true, placement_profile ="
            " ameesh_placement_profile(host, harness, provider, model, credential_mode)"
            " WHERE name = %s", (name,))

    def hold(self, name, owner, epoch, *, seconds=90):
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = %s, lease_epoch = %s,"
            " lease_expires_at = now() + make_interval(secs => %s) WHERE name = %s",
            (owner, epoch, seconds, name))

    def free(self, name, epoch=41):
        self.db.execute("UPDATE agent_registry SET lease_owner = NULL, lease_expires_at = NULL,"
                        " lease_epoch = %s WHERE name = %s", (epoch, name))

    def availability(self, state, seq, *, until_ts=None, token=TOKEN):
        resp = self.request("PUT", "/host/availability", {
            "schema": C.SCHEMA_AVAILABILITY, "available": state == "available",
            "state": state, "seq": seq, "until_ts": until_ts, "caps": {}, "reason": "idle"},
            token=token)
        self.assertEqual(resp.status, 204, resp.body)

    def row(self, name):
        return self.db.query("SELECT * FROM agent_registry WHERE name = %s", (name,))[0]

    # -- requêtes -------------------------------------------------------------
    def request(self, method, sub, body=None, *, token=TOKEN, key=None, headers=None):
        h = {"Authorization": "Bearer %s" % token} if token else {}
        if key:
            h[C.IDEMPOTENCY_HEADER] = key
        h.update(headers or {})
        raw = json.dumps(body).encode() if body is not None else b""
        return self.app.handle(method, C.PREFIX + sub, h, raw, client_ip="127.0.0.1")

    def op(self, name, args=(), kwargs=None, *, fence=None, token=TOKEN, key="auto",
           route=None):
        operation = self.contract.operations.get(name)
        if route is None:
            route = "/session/op" if operation and operation.transport == "session/op" else "/op"
        if key == "auto":
            key = C.new_idempotency_key() if operation and operation.write else None
        body = C.OpRequest(name, tuple(args), kwargs or {},
                           C.Fence(*fence) if fence else None).to_json()
        return self.request("POST", route, body, token=token, key=key)

    def golden(self, case, **overrides):
        req = case["requete"]
        sub = req["chemin"][len(C.PREFIX):]
        headers = {k: v for k, v in req["entetes"].items()}
        headers.update(overrides)
        raw = json.dumps(req["corps"]).encode()
        return self.app.handle(req["methode"], C.PREFIX + sub, headers, raw,
                               client_ip="127.0.0.1")


class DoreTest(ServeurExecTest):
    """Les jeux dorés de L107, rejoués un par un sur une base remise à zéro."""

    def prepare(self, case):
        name = case["nom"]
        corps = case["requete"]["corps"]
        if name.endswith("bail-perdu"):
            self.hold("inge-front", OWNER, 43)
        if name in ("leases.claim/ok", "leases.claim/rejoue", "idempotency_mismatch"):
            self.free("inge-front")
        if name in ("leases.claim/rejoue", "idempotency_mismatch"):
            first = [c for c in _golden_cases() if c["nom"] == "leases.claim/ok"][0]
            self.assertEqual(self.golden(first).status, 200)
        if name == "host_unavailable":
            self.availability("draining", 2)
        if name == "executor_revoked":
            self.auth.force = "executor_revoked"
        if name == "token_expired":
            self.auth.force = "token_expired"
        if name == "rate_limited":
            self.app.limiter = Deny()
        if name == "unavailable":
            self.app.pool = self.dispatcher.pool = BrokenPool()
        if name in ("leases.take_pending_prompt/ok", "leases.restore_prompt/ok"):
            self.db.execute("UPDATE agent_registry SET pending_prompt = 'Reprends le lot 812.',"
                            " current_prompt = 'Reprends le lot 812.' WHERE name = 'inge-front'")
        if name in ("pending_spend.set_model/ok", "pending_spend.clear/ok",
                    "pending_spend.get/ok"):
            storage.of(self.db).pending_spend.put("inge-front", 0, "turn-77", "deepseek-chat")
        if name == "turn_resources.close_turn/ok":
            storage.of(self.db).turn_resources.open_turn("turn-77", "inge-front", HOST,
                                                         pgid=1, label="dsh")
        if name == "leases.clear_marked_block/ok":
            self.db.execute("UPDATE agent_registry SET status = 'blocked', status_text ="
                            " 'dossier absent', last_error = 'cwd: /x' WHERE name = 'inge-front'")
        if name == "actions.get/ok":
            self.propose_action(corps["args"][0])
        if name.startswith("actions."):
            self.fix_action(corps)
        if corps.get("op") == "mailbox.send" and corps["kwargs"].get("kind") == "message":
            # `message` n'est pas un `kind` admis par 0012 (request, reply,
            # notify, event) : écart des jeux dorés, signalé
            corps["kwargs"]["kind"] = "request"
        if corps.get("op") in ("work.move", "work.note"):
            # `doing` et `review` ne sont pas des états de lot (0026) : écart
            # des jeux dorés, signalé ; `build` et `qa` à la place
            states = {"doing": "build", "review": "qa"}
            corps["args"] = [states.get(a, a) if isinstance(a, str) else a
                             for a in corps["args"]]
            if "current" in corps["kwargs"]:
                corps["kwargs"]["current"] = states.get(corps["kwargs"]["current"],
                                                        corps["kwargs"]["current"])

    def propose_action(self, action_id):
        storage.of(self.db).actions.propose(
            action_id=self.valid_action_id(action_id), project="site", work_item=812,
            proposed_by="agent:inge-front", connector="github", operation="merge_pr",
            target="owner/repo#812", args_json="{}", action_class="reversible", amount=None,
            currency=None, policy_version="1", digest="sha256:" + "0" * 64, dedupe="guaranteed",
            requires_receipt=True, approvers_json="[]", note="")

    @staticmethod
    def valid_action_id(action_id):
        return "act_" + (action_id.replace("-", "").replace("act", "") + "0" * 26)[:26]

    def fix_action(self, corps):
        """Les valeurs dorées d'action (`act-3b7e`, `sha256:…`, `merge-812`)
        ne passent pas les contraintes de la table `actions` (0010) : écart
        signalé dans le rapport de L108 ; on les rend valides ici."""
        if corps["op"] == "actions.get":
            corps["args"][0] = self.valid_action_id(corps["args"][0])
        else:
            kw = corps["kwargs"]
            kw["action_id"] = self.valid_action_id(kw["action_id"])
            kw["digest"] = "sha256:" + "0" * 64
            kw["dedupe"] = "guaranteed"

    def test_jeux_dores(self):
        cases = _golden_cases()
        self.assertGreater(len(cases), 70)
        for case in cases:
            with self.subTest(cas=case["nom"]):
                self.reset()
                self.auth.force = None
                case = copy.deepcopy(case)
                saved = (self.app.pool, self.dispatcher.pool, self.app.limiter)
                try:
                    self.prepare(case)
                    resp = self.golden(case)
                finally:
                    self.app.pool, self.dispatcher.pool, self.app.limiter = saved
                    self.auth.force = None
                expected = case["reponse"]
                self.assertEqual(resp.status, expected["statut"], resp.body)
                for header, value in expected.get("entetes", {}).items():
                    self.assertEqual(resp.headers.get(header), value)
                if expected["statut"] != 200:
                    self.assertEqual(resp.body["schema"], C.SCHEMA_ERROR)
                    self.assertEqual(resp.body["error"], expected["corps"]["error"])
                    continue
                op = self.contract.operations[case["op"]]
                self.assertEqual(resp.body["schema"], C.SCHEMA_RESULT)
                self.assertTrue(resp.body["ok"])
                self.assertTrue(C.conforms(resp.body["value"], op.result),
                                (case["nom"], resp.body["value"]))
                if case["nom"].endswith("bail-perdu"):
                    self.assertTrue(resp.body.get("fenced"))
                    self.assertEqual(resp.body["value"], op.refusal)
                    self.assertEqual(resp.body["value"], expected["corps"]["value"])
                else:
                    self.assertFalse(resp.body.get("fenced", False), case["nom"])

    def test_sante_et_hote(self):
        health = self.request("GET", "/health", token=None)
        self.assertEqual(health.status, 200)
        self.assertEqual(health.body["contract"], self.contract.version)
        host = self.request("GET", "/host")
        self.assertEqual(host.status, 200)
        self.assertEqual(host.body["schema"], C.SCHEMA_HOST)
        # seul l'agent du canon placé sur l'hôte est admis (pas l'agent inscrit à la main)
        self.assertEqual(host.body["agents"], ["inge-front"])
        self.assertTrue(host.body["available"])
        self.assertEqual(host.body["lease_ttl_s"], 90.0)


class PorteeTest(ServeurExecTest):

    def _swap(self, value, old, new):
        if isinstance(value, str):
            return value.replace(old, new)
        if isinstance(value, list):
            return [self._swap(v, old, new) for v in value]
        if isinstance(value, dict):
            return {k: self._swap(v, old, new) for k, v in value.items()}
        return value

    def test_chaque_operation_a_agent_refusee_hors_portee(self):
        refused = 0
        for case in _golden_cases():
            op = self.contract.operations.get(case["op"])
            if (op is None or not case["nom"].endswith("/ok") or op.transport != "op"
                    or not (("A" in op.scope and op.agent_param) or "B" in op.scope)):
                continue
            with self.subTest(op=op.name):
                corps = self._swap(case["requete"]["corps"], "inge-front", "tresorier")
                self.hold("tresorier", OWNER.replace(HOST, "autre-hote"), 42)
                resp = self.request("POST", "/op", corps,
                                    key=C.new_idempotency_key() if op.write else None)
                self.assertEqual(resp.status, 403, (op.name, resp.body))
                self.assertEqual(resp.body["error"], "forbidden_scope")
                refused += 1
        self.assertGreaterEqual(refused, 37)

    def test_agent_hors_canon_jamais_admis(self):
        resp = self.op("agents.get", ["manuel"])
        self.assertEqual(resp.body["error"], "forbidden_scope")

    def test_liste_blanche_de_l_invitation(self):
        self.auth.add("amx1." + "F" * 43, Principal("executor", EXEC, "m", HOST,
                                                    agents_allowlist=("coord",), expires_ts=FAR))
        resp = self.op("agents.get", ["inge-front"], token="amx1." + "F" * 43)
        self.assertEqual(resp.body["error"], "forbidden_scope")

    def test_autre_executeur_hors_portee(self):
        resp = self.op("agents.get", ["inge-front"], token=OTHER_TOKEN)
        self.assertEqual(resp.status, 403)

    def test_hote_force(self):
        storage.of(self.db).hosts.record({"host": "autre-hote", "load1": 9.0})
        resp = self.op("hosts.latest", ["autre-hote"])
        self.assertIsNone(resp.body["value"])
        resp = self.op("hosts.record", [{"host": "autre-hote", "load1": 0.5}])
        self.assertEqual(resp.body["value"]["host"], HOST)
        resp = self.op("hosts.current", ["autre-hote"])
        self.assertEqual({r["host"] for r in resp.body["value"]}, {HOST})

    def test_rendus_filtres(self):
        self.assertIsNone(self.op("mailbox.get", [900]).body["value"])
        self.assertEqual(self.op("mailbox.get", [812]).body["value"]["id"], 812)
        self.assertEqual(self.op("agents.harnesses").body["value"], {"inge-front": "dsh"})
        self.assertEqual(self.op("operations.message_lots", [[812, 900]]).body["value"], ["812"])
        claimable = self.op("agents.claimable", ["autre-hote", ["tresorier", "inge-front"]],
                            {"require_responsible": False}).body["value"]
        self.assertEqual([r["name"] for r in claimable], [])  # inge-front a un bail vivant
        self.free("inge-front")
        claimable = self.op("agents.claimable", ["autre-hote", ["tresorier"]],
                            {"require_responsible": False}).body["value"]
        self.assertEqual(claimable, [])
        claimable = self.op("agents.claimable", [HOST, []],
                            {"require_responsible": False}).body["value"]
        self.assertEqual([r["name"] for r in claimable], ["inge-front"])
        overview = self.op("agents.overview", token=SESSION).body["value"]
        self.assertTrue(all(set(r) == {"name", "role", "status"} for r in overview))
        budgets = storage.of(self.db).budgets
        budgets.put("", 3600, 5.0, actor="human:x")
        budgets.put("tresorier", 3600, 1.0, actor="human:x")
        self.assertEqual([r["scope"] for r in self.op("budgets.limits").body["value"]], [""])
        self.db.execute("UPDATE agent_registry SET cwd = '/x' WHERE name = 'tresorier'")
        self.assertFalse(self.op("agents.cwd_used", ["/x", "inge-front"]).body["value"])

    def test_regles_de_session(self):
        self.assertEqual(self.op("leases.state", ["coord"], token=SESSION).body["error"],
                         "forbidden_scope")
        self.assertIsNone(self.op("work.get", [900], token=SESSION).body["value"])
        self.assertEqual(self.op("work.get", [812], token=SESSION).body["value"]["id"], 812)
        moved = self.op("work.move", [900, "qa"], {"current": "build", "loops": 0,
                                                       "note": "x", "actor": "x"}, token=SESSION)
        self.assertEqual(moved.body["error"], "forbidden_scope")
        # le jeton d'accès n'ouvre pas /session/op, ni l'inverse
        self.assertEqual(self.op("leases.state", ["inge-front"]).status, 401)
        self.assertEqual(self.op("agents.get", ["inge-front"], token=SESSION).status, 401)
        # expéditeur forcé, destinataire existant seulement
        sent = self.op("mailbox.send", ["tresorier", "coord", "bonjour"],
                       {"host": "x", "kind": "request", "payload": None, "work_item_id": None,
                        "signature": None, "signature_key": None, "signed_payload": None,
                        "nonce": None, "created_us": None, "expires_us": None}, token=SESSION)
        self.assertEqual(sent.status, 200, sent.body)
        row = self.db.query("SELECT sender, host FROM agent_mailbox WHERE id = %s",
                            (sent.body["value"]["id"],))[0]
        self.assertEqual((row["sender"], row["host"]), ("inge-front", HOST))
        absent = self.op("mailbox.send", ["inge-front", "inconnu", "x"],
                         {"host": None, "kind": "request", "payload": None,
                          "work_item_id": None, "signature": None, "signature_key": None,
                          "signed_payload": None, "nonce": None, "created_us": None,
                          "expires_us": None}, token=SESSION)
        self.assertEqual(absent.body["error"], "bad_args")
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_registry"
                                       " WHERE name = 'inconnu'")[0]["n"], 0)
        # marquer remis : seulement les messages de l'agent du jeton
        self.assertEqual(self.op("mailbox.mark_delivered", [[900]], token=SESSION)
                         .body["value"], 0)

    def test_aucun_recu_ni_approbation(self):
        """Toute opération refusée par la table (approbations, nonces,
        reçus, canon…) : 404 sur les deux routes, rien d'exécuté."""
        names = [d + "." + m for d, ms in self.contract.refused.items() for m in ms]
        self.assertGreater(len(names), 150)
        for name in names:
            for token, route in ((TOKEN, "/op"), (SESSION, "/session/op")):
                resp = self.request("POST", route, {"schema": C.SCHEMA_OP, "op": name,
                                                    "args": [], "kwargs": {}},
                                    token=token, key=C.new_idempotency_key())
                self.assertEqual((name, resp.status, resp.body["error"]),
                                 (name, 404, "op_not_allowed"))
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM mesh_approvals")[0]["n"], 0)

    def test_upsert_ne_cree_rien(self):
        resp = self.op("agents.upsert", ["nouveau"], {
            "chantier": None, "harness": None, "host": None, "cwd": "/x", "session_id": None,
            "status": None, "status_text": None, "model": None, "budget_usd": None},
            fence=("nouveau", OWNER, 1))
        self.assertEqual(resp.body["error"], "forbidden_scope")
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_registry"
                                       " WHERE name = 'nouveau'")[0]["n"], 0)


class FencingTest(ServeurExecTest):

    FENCE = ("inge-front", OWNER, 42)

    def _writes(self):
        """(opération, args, kwargs) des écritures de portée B, tirées des
        jeux dorés."""
        out = []
        for case in _golden_cases():
            op = self.contract.operations.get(case["op"])
            if op and op.fence and case["nom"].endswith("/ok"):
                corps = case["requete"]["corps"]
                out.append((op, corps["args"], corps["kwargs"]))
        return out

    def test_bail_perdu_rend_le_refus_et_n_ecrit_rien(self):
        writes = self._writes()
        self.assertGreaterEqual(len(writes), 29)
        losses = {
            "epoch": lambda: self.hold("inge-front", OWNER, 43),
            "echu": lambda: self.hold("inge-front", OWNER, 42, seconds=-5),
            "autre-executeur": lambda: self.hold("inge-front", "exec:9b9b:anna-portable:1", 42),
            "autre-hote": lambda: self.db.execute(
                "UPDATE agent_registry SET host = 'ailleurs' WHERE name = 'inge-front'"),
        }
        for loss, apply in losses.items():
            for op, args, kwargs in writes:
                with self.subTest(perte=loss, op=op.name):
                    self.reset()
                    apply()
                    before = self.snapshot()
                    resp = self.op(op.name, args, kwargs, fence=self.FENCE)
                    if loss == "autre-hote":
                        # l'agent n'est plus admis sur l'hôte : refus de portée
                        self.assertEqual(resp.body.get("error"), "forbidden_scope")
                        continue
                    self.assertEqual(resp.status, 200, resp.body)
                    self.assertTrue(resp.body.get("fenced"), op.name)
                    self.assertEqual(resp.body["value"], op.refusal)
                    self.assertEqual(self.snapshot(), before, op.name)

    def test_owner_d_un_autre_executeur_avec_la_bonne_enveloppe(self):
        """Un exécuteur ne peut pas écrire sous le bail d'un autre, même en
        recopiant son (owner, epoch)."""
        resp = self.op("agents.set_status", ["inge-front", "idle", "x", None],
                       fence=self.FENCE, token=SAME_HOST_OTHER_EXEC)
        self.assertTrue(resp.body.get("fenced"))
        self.assertEqual(self.row("inge-front")["status"], "idle")

    def test_ecriture_non_fencee_par_l_interface(self):
        resp = self.op("agents.set_status", ["inge-front", "blocked", "x", None],
                       fence=self.FENCE)
        self.assertFalse(resp.body.get("fenced"))
        self.assertEqual(self.row("inge-front")["status"], "blocked")
        self.hold("inge-front", OWNER, 43)
        resp = self.op("agents.set_status", ["inge-front", "idle", "y", None], fence=self.FENCE)
        self.assertTrue(resp.body.get("fenced"))
        self.assertEqual(self.row("inge-front")["status"], "blocked")

    def test_enveloppe_incoherente(self):
        resp = self.op("leases.begin_turn", ["inge-front", OWNER, 41, "x"], fence=self.FENCE)
        self.assertEqual(resp.body["error"], "bad_args")

    def test_ecriture_de_session_sous_bail_mort(self):
        before = self.snapshot()
        for token in (STALE_SESSION,):
            resp = self.op("work.note", [812, "build", "texte", "x"], token=token)
            self.assertEqual((resp.status, resp.body["error"]), (401, "token_expired"))
        self.hold("inge-front", OWNER, 42, seconds=-1)
        resp = self.op("work.note", [812, "build", "texte", "x"], token=SESSION)
        self.assertEqual(resp.body["error"], "token_expired")
        self.assertEqual(self.snapshot(), before)
        self.hold("inge-front", OWNER, 42)
        resp = self.op("work.note", [812, "build", "texte", "x"], token=SESSION)
        self.assertEqual(resp.status, 200)
        actor = self.db.query("SELECT actor FROM work_item_events ORDER BY id DESC LIMIT 1")
        self.assertEqual(actor[0]["actor"], "inge-front")

    def snapshot(self):
        return json.dumps([
            self.db.query("SELECT name, status, status_text, last_error, session_id,"
                          " session_account, pending_prompt, current_prompt, cwd, lease_owner,"
                          " lease_epoch, session_work_item, last_event_at"
                          " FROM agent_registry ORDER BY name"),
            self.db.query("SELECT id, delivered_at, meta FROM agent_mailbox ORDER BY id"),
            self.db.query("SELECT agent, turn, model FROM spend_pending ORDER BY agent"),
            self.db.query("SELECT count(*) AS n FROM turn_costs"),
            self.db.query("SELECT turn_id, status FROM turn_resources ORDER BY turn_id"),
            self.db.query("SELECT count(*) AS n FROM thread_index"),
            self.db.query("SELECT count(*) AS n FROM work_item_events"),
        ], default=str, sort_keys=True)


class IdempotenceTest(ServeurExecTest):

    SEND = {"host": None, "kind": "request", "payload": None, "work_item_id": None,
            "signature": None, "signature_key": None, "signed_payload": None, "nonce": None,
            "created_us": None, "expires_us": None}

    def count(self):
        return self.db.query("SELECT count(*) AS n FROM agent_mailbox")[0]["n"]

    def test_rejeu_meme_reponse_une_seule_ecriture(self):
        key = C.new_idempotency_key()
        first = self.op("mailbox.send", ["inge-front", "coord", "un"], self.SEND,
                        token=SESSION, key=key)
        n = self.count()
        again = self.op("mailbox.send", ["inge-front", "coord", "un"], self.SEND,
                        token=SESSION, key=key)
        self.assertEqual(again.body, first.body)
        self.assertEqual(again.headers.get(C.IDEMPOTENCY_REPLAYED_HEADER), "true")
        self.assertEqual(self.count(), n)
        other = self.op("mailbox.send", ["inge-front", "coord", "deux"], self.SEND,
                        token=SESSION, key=key)
        self.assertEqual((other.status, other.body["error"]), (409, "idempotency_mismatch"))
        self.assertEqual(self.count(), n)

    def test_rejeu_d_un_bail_perdu(self):
        self.hold("inge-front", OWNER, 43)
        key = C.new_idempotency_key()
        first = self.op("leases.begin_turn", ["inge-front", OWNER, 42, "x"],
                        fence=("inge-front", OWNER, 42), key=key)
        self.hold("inge-front", OWNER, 42)
        again = self.op("leases.begin_turn", ["inge-front", OWNER, 42, "x"],
                        fence=("inge-front", OWNER, 42), key=key)
        self.assertEqual(again.body, first.body)
        self.assertTrue(again.body["fenced"])
        self.assertNotEqual(self.row("inge-front")["status"], "running")

    def test_cle_obligatoire_et_bien_formee(self):
        resp = self.op("leases.renew", ["inge-front", OWNER, 42, 90.0],
                       fence=("inge-front", OWNER, 42), key=None)
        self.assertEqual(resp.body["error"], "idempotency_key_required")
        resp = self.op("leases.renew", ["inge-front", OWNER, 42, 90.0],
                       fence=("inge-front", OWNER, 42), key="x y")
        self.assertEqual(resp.body["error"], "bad_request")

    def test_cle_par_executeur(self):
        key = C.new_idempotency_key()
        self.availability("available", 5, token=OTHER_TOKEN)
        a = self.op("hosts.record", [{"load1": 1.0}], key=key)
        b = self.op("hosts.record", [{"load1": 2.0}], key=key, token=OTHER_TOKEN)
        self.assertEqual((a.status, b.status), (200, 200))
        self.assertEqual(b.body["value"]["host"], "autre-hote")

    def test_echec_non_memorise(self):
        key = C.new_idempotency_key()
        self.availability("draining", 2)
        self.free("inge-front")
        refused = self.op("leases.claim", ["inge-front", OWNER, 90.0],
                          {"require_responsible": False}, key=key)
        self.assertEqual(refused.body["error"], "host_unavailable")
        self.availability("available", 3)
        ok = self.op("leases.claim", ["inge-front", OWNER, 90.0],
                     {"require_responsible": False}, key=key)
        self.assertEqual(ok.status, 200)
        self.assertEqual(ok.body["value"]["lease_epoch"], 42)


class PorteTest(ServeurExecTest):

    def claim(self, ttl=90.0):
        return self.op("leases.claim", ["inge-front", OWNER, ttl], {"require_responsible": False})

    def test_reclamation_refusee_hors_disponibilite(self):
        self.free("inge-front")
        for state in ("draining", "stopped"):
            self.availability(state, 10 if state == "draining" else 11)
            resp = self.claim()
            self.assertEqual((resp.status, resp.body["error"]), (403, "host_unavailable"))
            claimable = self.op("agents.claimable", [HOST, ["inge-front"]],
                                {"require_responsible": False})
            self.assertEqual(claimable.body["value"], [])
        self.assertIsNone(self.row("inge-front")["lease_owner"])
        self.availability("available", 12, until_ts=time.time() - 5)
        self.assertEqual(self.claim().body["error"], "host_unavailable")
        self.availability("available", 13)
        self.assertEqual(self.claim().status, 200)

    def test_sequence_qui_recule_ignoree(self):
        self.availability("draining", 20)
        self.availability("available", 19)
        self.assertFalse(self.request("GET", "/host").body["available"])

    def test_sans_rapport_indisponible(self):
        self.db.execute("TRUNCATE exec_host_availability")
        self.free("inge-front")
        self.assertEqual(self.claim().body["error"], "host_unavailable")

    def test_ttl_borne_et_owner_d_autrui(self):
        self.free("inge-front")
        resp = self.claim(ttl=3600.0)
        self.assertEqual(resp.status, 200)
        self.assertLess(resp.body["value"]["lease_expires_ts"] - time.time(), 120)
        self.free("inge-front")
        resp = self.op("leases.claim", ["inge-front", "exec:9b9b:anna-portable:1", 90.0],
                       {"require_responsible": False})
        self.assertEqual(resp.body["error"], "forbidden_scope")

    def test_corps_de_disponibilite_controle(self):
        resp = self.request("PUT", "/host/availability", {"schema": C.SCHEMA_AVAILABILITY,
                                                          "state": "sleeping", "seq": 1})
        self.assertEqual(resp.body["error"], "bad_request")


class FluxTest(ServeurExecTest):

    def poll(self, after=None, wait=0.0, token=TOKEN):
        q = "?wait=%s" % wait + ("&after=%s" % after if after else "")
        return self.request("GET", "/events" + q, token=token)

    def test_filtrage_par_hote(self):
        start = self.poll().body["last_id"]
        self.hub.publish("agent_mail", {"to": "inge-front", "id": 812, "from": "coord"})
        self.hub.publish("agent_mail", {"to": "tresorier", "id": 900})
        self.hub.publish("agent_lease", {"agent": "inge-front", "epoch": 42})
        self.hub.publish("agent_lease", {"name": "tresorier"})
        self.hub.publish("ameesh_budget", {"scope": ""})
        body = self.poll(start).body
        self.assertEqual(body["schema"], C.SCHEMA_EVENTS)
        self.assertEqual([e["channel"] for e in body["events"]],
                         ["agent_mail", "agent_lease", "ameesh_budget"])
        self.assertEqual(body["last_id"], "k3f9:5")
        other = self.poll(start, token=OTHER_TOKEN).body
        self.assertEqual([e["data"].get("to", e["data"].get("name")) for e in other["events"]],
                         ["tresorier", "tresorier", None])
        self.assertEqual(self.poll(body["last_id"], wait=0.2).body["events"], [])

    def test_attente_longue_reveillee(self):
        start = self.poll().body["last_id"]
        threading.Timer(0.2, self.hub.publish, ("agent_mail", {"to": "inge-front"})).start()
        t0 = time.monotonic()
        body = self.poll(start, wait=5).body
        self.assertLess(time.monotonic() - t0, 4)
        self.assertEqual(len(body["events"]), 1)

    def test_trou_de_reprise(self):
        for after in ("a001:5", "k3f9:99", "mal"):
            body = self.poll(after).body
            self.assertEqual([e["channel"] for e in body["events"]], [E.RESET], after)
        hub = EventHub(None, ring_size=3, stream="k3f9")
        for i in range(6):
            hub.publish("ameesh_budget", {"i": i})
        events, last = hub.poll("k3f9:1", lambda: (), 0)
        self.assertEqual([e.channel for e in events], [E.RESET])
        events, last = hub.poll("k3f9:3", lambda: (), 0)
        self.assertEqual(len(events), 3)

    def test_sse(self):
        resp = self.request("GET", "/events", headers={"Accept": "text/event-stream",
                                                       "Last-Event-ID": self.hub.current()})
        self.assertIsNotNone(resp.stream)
        self.assertEqual(next(resp.stream), E.format_retry())
        self.hub.publish("agent_mail", {"to": "inge-front", "id": 812})
        block = next(resp.stream)
        events = list(E.parse_sse(block.splitlines(True)))
        self.assertEqual(events[0].data, {"to": "inge-front", "id": 812})
        self.assertEqual(events[0].id, "k3f9:1")
        resp.stream.close()

    def test_listen_reel(self):
        hub = EventHub(lambda: storage.of(self.connect()).wakeups.subscribe(list(E.CHANNELS)),
                       stream="live")
        hub.start()
        try:
            self.wait_for(lambda: hub.listening, timeout=10)
            start = hub.current()
            storage.of(self.db).mailbox.send(
                "coord", "inge-front", "réveil", host=None, kind="request", payload=None,
                work_item_id=None, signature=None, signature_key=None, signed_payload=None,
                nonce=None, created_us=None, expires_us=None)
            deadline = time.monotonic() + 10
            mail = []
            while not mail and time.monotonic() < deadline:
                events, start = hub.poll(start, lambda: {"inge-front"}, 1.0)
                mail += [e for e in events if e.channel == "agent_mail"]
            self.assertEqual(mail[0].data["to"], "inge-front")
        finally:
            hub.stop()


class BornesEtAuditTest(ServeurExecTest):

    def test_corps_trop_gros(self):
        resp = self.app.handle("POST", C.PREFIX + "/op", {"Authorization": "Bearer " + TOKEN},
                               b"x" * (C.MAX_BODY_BYTES + 1))
        self.assertEqual((resp.status, resp.body["error"]), (413, "too_large"))

    def test_debit_borne(self):
        app = ExecApp(self.dispatcher, self.auth, self.hub, mesh="m", rate_per_minute=3)
        codes = [app.handle("POST", C.PREFIX + "/op", {"Authorization": "Bearer " + TOKEN},
                            json.dumps(C.OpRequest("agents.get", ("inge-front",)).to_json())
                            .encode()).status for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        resp = app.handle("POST", C.PREFIX + "/op", {"Authorization": "Bearer " + TOKEN}, b"{}")
        self.assertEqual(resp.headers.get("Retry-After"), "2")

    def test_bornes_d_arguments(self):
        resp = self.op("mailbox.unread", ["inge-front", 10 ** 6])
        self.assertEqual(resp.status, 200)
        resp = self.op("operations.message_lots", [list(range(2000))])
        self.assertEqual(resp.body["error"], "bad_args")

    def test_jeton(self):
        self.assertEqual(self.request("GET", "/host", token=None).body["error"], "token_invalid")
        self.assertEqual(self.request("GET", "/host", token="amx1.inconnu").status, 401)
        self.auth.revoke(EXEC)
        resp = self.request("GET", "/host")
        self.assertEqual((resp.status, resp.body["error"]), (403, "executor_revoked"))

    def test_routes_non_servies(self):
        for method, sub in (("POST", "/enroll"), ("POST", "/token"),
                            ("POST", "/session-token"), ("GET", "/work/inge-front/bundle"),
                            ("POST", "/llm/deepseek/v1/chat/completions")):
            resp = self.request(method, sub, {})
            self.assertEqual((sub, resp.status), (sub, 404))

    def test_journal_d_audit(self):
        self.op("agents.set_status", ["inge-front", "idle", "x", None],
                fence=("inge-front", OWNER, 42))
        self.op("agents.get", ["tresorier"])
        self.op("agents.get", ["inge-front"])
        rows = self.db.query("SELECT op, status, error, agent, fenced FROM exec_audit"
                             " WHERE op LIKE 'agents.%%' ORDER BY id")
        self.assertEqual([(r["op"], r["status"], r["error"]) for r in rows],
                         [("agents.set_status", 200, None),
                          ("agents.get", 403, "forbidden_scope")])


class HttpTest(ServeurExecTest):
    """Le serveur stdlib de bout en bout (une requête par route utile)."""

    def test_http(self):
        server = ExecHTTPServer(self.app, ("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", C.PREFIX + "/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read())["schema"], C.SCHEMA_HEALTH)
            body = json.dumps(C.OpRequest("leases.renew", ("inge-front", OWNER, 42, 90.0), {},
                                          C.Fence("inge-front", OWNER, 42)).to_json())
            headers = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
                       C.IDEMPOTENCY_HEADER: C.new_idempotency_key()}
            for replayed in (None, "true"):
                conn.request("POST", C.PREFIX + "/op", body, headers)
                resp = conn.getresponse()
                result = json.loads(resp.read())
                self.assertEqual(resp.status, 200, result)
                self.assertIsInstance(result["value"], float)
                self.assertEqual(resp.getheader(C.IDEMPOTENCY_REPLAYED_HEADER), replayed)
            conn.close()
            sse = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            sse.request("GET", C.PREFIX + "/events", headers={
                "Authorization": "Bearer " + TOKEN, "Accept": "text/event-stream"})
            resp = sse.getresponse()
            self.assertEqual(resp.getheader("Content-Type"), "text/event-stream; charset=utf-8")
            self.assertEqual(resp.readline(), b"retry: 3000\n")
            self.app.closing.set()
            sse.close()
        finally:
            self.app.closing.set()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
