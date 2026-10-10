# SPDX-License-Identifier: AGPL-3.0-only
"""L107 : le contrat de l'API d'exécuteur médiée est cohérent et figé.

Sans base : la table (`contract.json`) est confrontée à `storage.interface`,
les jeux dorés (`tests/dore/mediated_executor/`) au contrat, et les
interfaces figées entre L108, L109, L110 et L112 à leur liste de méthodes.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import typing
import unittest

from ameesh import db as db_mod
from ameesh.mediated_executor import contract as C
from ameesh.mediated_executor import events as E
from ameesh.mediated_executor import interfaces as I
from ameesh.mediated_executor import gate as P
from ameesh.storage import interface as SI

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DORE = os.path.join(REPO, "tests", "dore", "mediated_executor")
HINTS = typing.get_type_hints(SI.Storage)


def _domains() -> dict:
    return {d: cls for d, cls in HINTS.items()
            if isinstance(cls, type) and issubclass(cls, SI.Domain)}


def _interface_ops() -> set:
    out = set()
    for d, cls in _domains().items():
        for n, v in vars(cls).items():
            if not n.startswith("_") and getattr(v, "__isabstractmethod__", False):
                out.add("%s.%s" % (d, n))
    return out


def _schema(ann: str) -> dict:
    """Annotation de l'interface → schéma attendu dans la table."""
    ann = ann.strip()
    if ann.endswith("| None"):
        s = _schema(ann[: -len("| None")])
        t = s.get("type")
        return {**s, "type": [t, "null"]} if isinstance(t, str) else {"anyOf": [s, {"type": "null"}]}
    base = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
            "dict": "object", "None": "null"}
    if ann in base:
        return {"type": base[ann]}
    m = re.fullmatch(r"(?:Sequence|list)\[(.+)\]", ann)
    if m:
        return {"type": "array", "items": _schema(m.group(1))}
    m = re.fullmatch(r"dict\[str, (.+)\]", ann)
    if m:
        return {"type": "object", "additionalProperties": _schema(m.group(1))}
    if ann.startswith("Subscription"):
        return {"$ref": C.SCHEMA_EVENTS}
    raise AssertionError("annotation non prévue par le contrat : %s" % ann)


ARGS_THREADS = {"project": "site", "lot": "812", "transport": "github", "host": "h",
                "location": "owner/repo#812", "entry_id": "c-1", "mailbox_ids": [1],
                "author": "agent:a", "excerpt": "x", "ts": 1.5, "trace": {}}


def _golden(name: str) -> dict:
    with open(os.path.join(DORE, name), encoding="utf-8") as fh:
        return json.load(fh)


def _op_cases() -> list:
    out = []
    for fn in sorted(os.listdir(DORE)):
        g = _golden(fn)
        if g["famille"] in ("erreurs", "evenements", "porte", "identite"):
            continue
        out.extend(g["cas"])
    return out


class TableTest(unittest.TestCase):
    def setUp(self):
        self.c = C.load()

    def test_comptes(self):
        ops = self.c.operations.values()
        # contrat 1.1 : + operations.assigned_open_lots (lot du tour)
        self.assertEqual(len(self.c.operations), 62)
        self.assertEqual(sum(o.write for o in ops), 38)
        self.assertEqual(sum(not o.write for o in ops), 24)
        self.assertEqual(sum(o.transport == "session/op" for o in ops), 9)
        # contrat 1.1 : lignes de la route op servies aussi par session/op
        self.assertEqual(sorted(o.name for o in ops if o.session),
                         ["mailbox.deliver", "mailbox.release", "mailbox.reserve",
                          "mailbox.unread", "threads.index"])
        self.assertEqual(self.c.version, "1.1.0")
        self.assertEqual([o.name for o in ops if o.transport == "events"], ["wakeups.subscribe"])
        self.assertEqual(self.c.prefix, C.PREFIX)

    def test_chaque_operation_existe_avec_sa_signature(self):
        for name, op in self.c.operations.items():
            with self.subTest(op=name):
                cls = _domains().get(op.domain)
                self.assertIsNotNone(cls, "domaine inconnu")
                fn = vars(cls).get(op.method)
                self.assertTrue(getattr(fn, "__isabstractmethod__", False), "méthode absente")
                sig = inspect.signature(fn)
                params = list(sig.parameters.values())[1:]
                self.assertEqual([p.name for p in op.params], [p.name for p in params])
                for tp, p in zip(op.params, params):
                    kind = {p.POSITIONAL_OR_KEYWORD: "positionnel", p.KEYWORD_ONLY: "nomme"}[p.kind]
                    self.assertEqual(tp.kind, kind, p.name)
                    self.assertEqual(tp.required, p.default is p.empty, p.name)
                    if p.default is not p.empty:
                        self.assertEqual(tp.default, p.default, p.name)
                    if p.annotation is not p.empty:
                        self.assertEqual(tp.schema, _schema(p.annotation), p.name)
                self.assertEqual(op.result, _schema(sig.return_annotation))

    def test_l_interface_entiere_est_classee(self):
        """Toute opération de l'interface est admise OU refusée, jamais les
        deux : une opération ajoutée à l'interface doit être classée ici."""
        admitted = set(self.c.operations)
        refused = {"%s.%s" % (d, m) for d, ms in self.c.refused.items() for m in ms}
        self.assertFalse(admitted & refused)
        self.assertEqual(admitted | refused, _interface_ops())
        self.assertEqual(self.c.interface_total, len(_interface_ops()))
        self.assertEqual(set(self.c.refused_reasons), set(self.c.refused))

    def test_jamais_d_autorite(self):
        for d in ("approvals", "nonces", "grants", "authenticators", "canon", "packages",
                  "placements", "visibility", "ephemerals", "session_bindings", "action_source"):
            self.assertFalse([o for o in self.c.operations if o.startswith(d + ".")], d)
        self.assertEqual(sorted(o for o in self.c.operations if o.startswith("actions.")),
                         ["actions.get", "actions.propose"])
        for refused in ("agents.upsert_unleased", "keys.register", "keys.revoke",
                        "operations.listing", "leases.attach_claim"):
            with self.assertRaises(C.NotSupportedRemotely):
                self.c.get(refused)

    def test_regles_de_ligne(self):
        for name, op in self.c.operations.items():
            with self.subTest(op=name):
                self.assertEqual(op.fence, "B" in op.scope)
                self.assertEqual(op.requires_idempotency_key, op.write)
                self.assertEqual("S" in op.scope, op.transport == "session/op")
                pnames = {p.name for p in op.params}
                if op.agent_param is not None:
                    self.assertIn(op.agent_param, pnames)
                for forced in op.forced:
                    self.assertIn(forced, pnames)
                if op.fence:
                    self.assertTrue(op.write, "un fence ne protège qu'une écriture")
                    self.assertTrue(C.conforms(op.refusal, op.result) or op.refusal is None,
                                    "valeur de refus hors schéma")
                else:
                    self.assertIsNone(op.refusal)

    def test_paquet_livre_la_table(self):
        with open(os.path.join(REPO, "pyproject.toml"), encoding="utf-8") as fh:
            self.assertIn('"ameesh.mediated_executor" = ["contract.json"]', fh.read())


class EnveloppesTest(unittest.TestCase):
    def setUp(self):
        self.c = C.load()

    def test_dores_couvrent_chaque_operation(self):
        covered = {case["op"] for case in _op_cases()}
        expected = {n for n, o in self.c.operations.items() if o.transport != "events"}
        self.assertEqual(covered, expected)
        ev = _golden("events.json")
        self.assertTrue(ev["sse"]["evenements"])

    def test_dores_conformes(self):
        for case in _op_cases():
            with self.subTest(cas=case["nom"]):
                op = self.c.get(case["op"])
                req = case["requete"]
                route = req["chemin"][len(C.PREFIX) + 1:]
                self.assertIn(route, op.routes)
                self.assertEqual(case["nom"].endswith("/session"), route != op.transport)
                token = req["entetes"]["Authorization"].split()[1]
                prefix = I.SESSION_TOKEN_PREFIX if route == "session/op" else I.ACCESS_TOKEN_PREFIX
                self.assertTrue(token.startswith(prefix))
                key = req["entetes"].get(C.IDEMPOTENCY_HEADER)
                self.assertEqual(bool(key), op.write)
                parsed = C.OpRequest.from_json(req["corps"])
                self.assertEqual(parsed.to_json(), req["corps"])
                self.c.validate_request(parsed, route=route, idempotency_key=key)
                res = C.OpResult.from_json(case["reponse"]["corps"])
                self.assertEqual(case["reponse"]["statut"], 200)
                self.assertTrue(C.conforms(res.value, op.result), res.value)
                if res.fenced:
                    self.assertTrue(op.fence)
                    self.assertEqual(res.value, op.refusal)

    def test_validation_des_requetes(self):
        c = self.c
        f = C.Fence("a", "exec:x:h:1", 3)
        ok = C.OpRequest("leases.begin_turn", ("a", "exec:x:h:1", 3, "t"), {}, f)
        self.assertEqual(c.validate_request(ok, route="op", idempotency_key="k")["epoch"], 3)
        cases = [
            (C.OpRequest("leases.begin_turn", ("a", "exec:x:h:1", 3, "t")), "fence_required"),
            (ok, "idempotency_key_required"),
            (C.OpRequest("leases.begin_turn", ("a", "exec:x:h:1", 4, "t"), {}, f), "bad_args"),
            (C.OpRequest("leases.begin_turn", ("b", "exec:x:h:1", 3, "t"), {}, f), "bad_args"),
            (C.OpRequest("leases.begin_turn", ("a",), {}, f), "bad_args"),
            (C.OpRequest("agents.get", ("a",), {"bogus": 1}), "bad_args"),
            (C.OpRequest("agents.get", (12,)), "bad_args"),
        ]
        for req, code in cases:
            with self.subTest(code=code, req=req):
                key = None if code == "idempotency_key_required" else "k"
                with self.assertRaises(ValueError) as cm:
                    c.validate_request(req, route="op", idempotency_key=key)
                self.assertEqual(str(cm.exception), code)
        with self.assertRaises(C.NotSupportedRemotely):
            c.validate_request(C.OpRequest("mailbox.send", ("a", "b", "x")), route="op",
                               idempotency_key="k")
        with self.assertRaises(C.NotSupportedRemotely):
            c.validate_request(C.OpRequest("approvals.consume"), route="op", idempotency_key="k")

    def test_erreurs_dorees_et_exceptions_client(self):
        for case in _golden("errors.json")["cas"]:
            with self.subTest(cas=case["nom"]):
                body = case["reponse"]["corps"]
                self.assertEqual(body["schema"], C.SCHEMA_ERROR)
                spec = C.ERRORS[body["error"]]
                self.assertEqual(case["reponse"]["statut"], spec.status)
        expect = {
            "forbidden_scope": C.Forbidden, "host_unavailable": C.Forbidden,
            "executor_revoked": C.ExecutorRevoked, "op_not_allowed": C.NotSupportedRemotely,
            "rate_limited": db_mod.Unavailable, "unavailable": db_mod.Unavailable,
            "internal": db_mod.Unavailable,
        }
        for code, spec in C.ERRORS.items():
            with self.subTest(code=code):
                exc = C.client_exception(spec.status, C.error_body(code, "x"))
                self.assertIsInstance(exc, db_mod.DbError)
                if code in expect:
                    self.assertIsInstance(exc, expect[code])
                else:
                    self.assertNotIsInstance(exc, (db_mod.Unavailable, C.Forbidden,
                                                   C.NotSupportedRemotely))
        self.assertIsInstance(C.client_exception(502, None), db_mod.Unavailable)
        self.assertIsInstance(C.client_exception(0, None), db_mod.Unavailable)
        self.assertEqual(C.client_exception(403, C.error_body("host_unavailable", "x")).code,
                         "host_unavailable")
        with self.assertRaises(ValueError):
            C.error_body("inconnu", "x")

    def test_idempotence(self):
        a = C.OpRequest("leases.claim", ("a", "exec:x:h:1", 90.0), {"require_responsible": False}).to_json()
        b = dict(a, kwargs={"require_responsible": False})
        self.assertEqual(C.request_sha256(a), C.request_sha256(b))
        self.assertNotEqual(C.request_sha256(a), C.request_sha256(dict(a, args=["a", "exec:x:h:1", 60.0])))
        self.assertNotEqual(C.new_idempotency_key(), C.new_idempotency_key())
        self.assertEqual(C.owner_for("7f3a", "h", 12), "exec:7f3a:h:12")
        # contrat 1.1 : nombres décimaux écrits comme ECMAScript (RFC 8785)
        c = dict(a, args=["a", "exec:x:h:1", 90.5])
        self.assertEqual(C.request_sha256(c), hashlib.sha256(
            b'{"args":["a","exec:x:h:1",90.5],"kwargs":{"require_responsible":false},'
            b'"op":"leases.claim","schema":"ameesh-exec-op/1"}').hexdigest())
        self.assertEqual(C.request_sha256(dict(a, args=["a", "exec:x:h:1", 90])),
                         C.request_sha256(a))
        from ameesh import jcs
        for value, text in ((0.031, "0.031"), (1e21, "1e+21"), (1e-7, "1e-7"),
                            (1e-6, "0.000001"), (-0.0, "0"), (123.0, "123"),
                            (0.1 + 0.2, "0.30000000000000004")):
            self.assertEqual(jcs.dumps(value, doubles=True), text)
        with self.assertRaises(jcs.JcsError):
            jcs.dumps(0.5)  # reçus et actions : toujours refusé
        with self.assertRaises(jcs.JcsError):
            C.request_sha256(dict(a, args=[float("nan")]))

    def test_lignes_ouvertes_a_la_session(self):
        """Contrat 1.1 : le hook et `mail inbox` passent par session/op."""
        f = C.Fence("a", "exec:x:h:1", 3)
        req = C.OpRequest("mailbox.release", ("a", "tok", [1]), {}, f)
        self.c.validate_request(req, route="session/op", idempotency_key="k")
        self.c.validate_request(req, route="op", idempotency_key="k")
        self.c.validate_request(C.OpRequest("mailbox.unread", ("a", 5)), route="session/op")
        with self.assertRaises(C.NotSupportedRemotely):
            self.c.validate_request(C.OpRequest("agents.get", ("a",)), route="session/op")
        idx = dict(ARGS_THREADS, author="agent:a")
        self.c.validate_request(C.OpRequest("threads.index", (), idx, f), route="op",
                                idempotency_key="k")
        with self.assertRaises(ValueError):
            self.c.validate_request(C.OpRequest("threads.index", (), dict(idx, author="agent:b"),
                                                f), route="op", idempotency_key="k")


class EvenementsTest(unittest.TestCase):
    def test_sse_dore(self):
        g = _golden("events.json")
        parsed = list(E.parse_sse(g["sse"]["reponse"]["texte"].splitlines(True)))
        self.assertEqual([e.to_json() for e in parsed], g["sse"]["evenements"])
        for e in parsed:
            self.assertIn(e.channel, E.CHANNELS)
            sig = e.as_signal()
            self.assertEqual(json.loads(sig["payload"]), e.data)
            self.assertEqual(E.parse_cursor(e.id)[0], "k3f9")

    def test_attente_longue_doree(self):
        g = _golden("events.json")
        evs, last = E.parse_long_poll(g["attente_longue"]["reponse"]["corps"])
        self.assertEqual(last, evs[-1].id)
        evs, last = E.parse_long_poll(g["attente_longue_vide"]["reponse"]["corps"])
        self.assertEqual((evs, last), ([], "k3f9:19"))
        evs, _ = E.parse_long_poll(g["reprise_trou"]["reponse"]["corps"])
        self.assertEqual(evs[0].channel, E.RESET)

    def test_curseur(self):
        self.assertEqual(E.parse_cursor(E.cursor("ab:c", 5)), ("ab:c", 5))
        for bad in ("", "12", ":4"):
            with self.assertRaises(ValueError):
                E.parse_cursor(bad)


class PorteTest(unittest.TestCase):
    def test_etats_dores(self):
        g = _golden("gate.json")
        states = [P.GateState.from_json(s) for s in g["etats"]]
        self.assertEqual([s.state for s in states], list(P.STATES))
        self.assertEqual([s.to_json() for s in states], g["etats"])
        self.assertTrue(states[0].may_claim and not states[1].may_claim)
        for bad in g["illisibles"]:
            with self.assertRaises(ValueError):
                P.GateState.from_json(bad)
        # contrat 1.1 : porte illisible d'un hôte médié = arrêt
        self.assertEqual(P.GateState.unreadable().state, "stopped")
        self.assertEqual(states[2].max_concurrent, P.DEFAULT_MAX_CONCURRENT)
        self.assertEqual(states[0].max_concurrent, 1)
        for bad in ({"schema": C.SCHEMA_HOST_ACK}, {"schema": C.SCHEMA_HOST_ACK, "seq": "1",
                     "state": "available"},
                    {"schema": C.SCHEMA_HOST_ACK, "seq": 1, "state": "dormant"},
                    {"schema": C.SCHEMA_HOST_ACK, "seq": 1, "state": "draining",
                     "held": "a"}, None):
            with self.assertRaises(ValueError):
                P.GateAck.from_json(bad)
        for ack in g["acquittements"]:
            self.assertEqual(P.GateAck.from_json(ack).to_json(), ack)
        body = g["disponibilite"]["requete"]["corps"]
        self.assertEqual(body, P.availability_body(states[1]))
        self.assertFalse(body["available"])

    def test_interface_de_porte(self):
        self.assertEqual(P.HostGate.__abstractmethods__,
                         frozenset({"state", "wait_change", "acknowledge"}))
        with self.assertRaises(TypeError):
            P.HostGate()
        gate = P.AlwaysAvailable()
        self.assertTrue(gate.state().may_claim)
        gate.close()
        self.assertEqual(gate.wait_change(5).seq, 0)
        gate.acknowledge(P.GateAck(0, "available"))
        self.assertEqual(len(gate.acks), 1)


class InterfacesFigeesTest(unittest.TestCase):
    """Les signatures entre lots : changer l'une d'elles casse ce test."""

    def test_methodes_abstraites(self):
        frozen = {
            I.ExecTransport: {"call", "session_call", "poll_events", "stream_events",
                              "host_info", "put_availability", "session_token"},
            I.TokenSource: {"access_token"},
            I.ExecutorAuth: {"verify"},
            I.IdentityProvider: {"verify", "enroll", "issue_access_token",
                                 "issue_session_token", "revoke"},
            I.ScopeRules: {"admitted_agents", "apply", "filter_result"},
            I.ExecDispatcher: {"dispatch"},
        }
        for cls, names in frozen.items():
            with self.subTest(cls=cls.__name__):
                self.assertEqual(cls.__abstractmethods__, frozenset(names))
        self.assertEqual(list(inspect.signature(I.ExecutorAuth.verify).parameters),
                         ["self", "token"])
        self.assertEqual(list(inspect.signature(I.ExecTransport.call).parameters),
                         ["self", "request", "idempotency_key"])

    def test_identite_doree(self):
        g = _golden("identity.json")
        host = I.HostInfo.from_json(g["host"]["reponse"]["corps"])
        self.assertEqual(host.to_json(), g["host"]["reponse"]["corps"])
        self.assertEqual(host.contract_version, C.load().version)
        tok = g["token"]["reponse"]["corps"]
        self.assertEqual(tok["schema"], C.SCHEMA_TOKEN)
        self.assertTrue(tok["access_token"].startswith(I.ACCESS_TOKEN_PREFIX))
        ses = g["session_token"]["reponse"]["corps"]
        self.assertEqual(ses["schema"], C.SCHEMA_SESSION_TOKEN)
        self.assertTrue(ses["access_token"].startswith(I.SESSION_TOKEN_PREFIX))
        C.Fence.from_json(g["session_token"]["requete"]["corps"]["fence"])
        self.assertEqual(g["enroll"]["requete"]["corps"]["schema"], C.SCHEMA_ENROLL)


if __name__ == "__main__":
    unittest.main()
