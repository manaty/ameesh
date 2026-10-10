# SPDX-License-Identifier: AGPL-3.0-only
"""L109 : le client de stockage distant de l'exécuteur médié.

Sans base. Le faux transport (`tests/exec_fake.py`) rejoue les jeux dorés de
L107 : chaque opération de la table, appelée par `storage.of(db)`, doit
produire EXACTEMENT la requête dorée et rendre la valeur dorée. Puis :
enveloppe de bail et carnet des baux, bail perdu, opérations refusées,
correspondance des erreurs, transport HTTP (jeton, idempotence, nouvelles
tentatives, SSE et attente longue) contre un vrai serveur HTTP local.
"""
from __future__ import annotations

import json
import time
import unittest

from ameesh import db as db_mod
from ameesh import storage
from ameesh.executeur_mediee import contrat as C
from ameesh.executeur_mediee import evenements as E
from ameesh.storage.remote import RemoteDb, RemoteStorage, RemoteSubscription
from ameesh.storage.remote import http as rhttp

from .exec_fake import (ACCESS, OWNER, SESSION, Fenced, GoldenServer, GoldenTransport,
                        Responder, error_cases, golden, make_db, op_cases)

CONTRAT = C.load()


def _call(db, name: str, args, kwargs):
    domain, method = name.split(".", 1)
    return getattr(getattr(storage.of(db), domain), method)(*args, **kwargs)


def _prime(db, case: dict) -> None:
    """Le carnet des baux tel qu'un exécuteur qui détient le bail du cas doré."""
    fence = case["requete"]["corps"].get("fence")
    if fence:
        db.book.hold(fence["agent"], fence["owner"], fence["epoch"])
        if case["op"] == "turn_resources.close_turn":
            db.book.open_turn(case["requete"]["corps"]["args"][0], fence["agent"])


class RejeuDoreTest(unittest.TestCase):
    """Chaque cas doré, rejoué par le client : requête et valeur identiques."""

    def test_chaque_operation_produit_la_requete_doree(self):
        vues = set()
        for case in op_cases():
            with self.subTest(case=case["nom"]):
                corps = case["requete"]["corps"]
                op = CONTRAT.get(case["op"])
                route_doree = case["requete"]["chemin"][len(C.PREFIX) + 1:]
                mode = "session" if route_doree == "session/op" else "executor"
                transport = GoldenTransport(Responder.only(case))
                db = make_db(transport, mode=mode)
                _prime(db, case)
                if case["nom"].endswith("/rejoue"):
                    continue  # même requête que `/ok` : servie par le serveur, pas le client
                value = _call(db, case["op"], corps["args"], corps["kwargs"])
                self.assertEqual(len(transport.calls), 1, transport.calls)
                route, body, key = transport.calls[0]
                self.assertEqual(route, route_doree)
                self.assertEqual(body, corps, "requête différente du jeu doré")
                self.assertEqual(key is not None, op.write,
                                 "Idempotency-Key : présente pour toute écriture, seulement")
                self.assertEqual(value, case["reponse"]["corps"]["value"])
                vues.add(case["op"])
        attendues = {n for n, o in CONTRAT.operations.items() if o.transport != "events"}
        self.assertEqual(attendues - vues, set(), "opérations de la table sans cas doré rejoué")

    def test_cles_d_idempotence_distinctes_par_appel_logique(self):
        transport = GoldenTransport()
        db = make_db(transport)
        db.book.hold("inge-front", OWNER, 42)
        for _ in range(2):
            storage.of(db).agents.mark_event_wake("inge-front")
        keys = [k for _r, _b, k in transport.calls]
        self.assertEqual(len(set(keys)), 2)

    def test_les_domaines_sont_ceux_de_l_interface(self):
        st = storage.of(make_db())
        self.assertIsInstance(st, RemoteStorage)
        self.assertEqual(st.driver, "mediated")
        from ameesh.storage import interface as SI
        self.assertIsInstance(st.leases, SI.Leases)
        self.assertIsInstance(st.mailbox, SI.Mailbox)


class EnveloppeDeBailTest(unittest.TestCase):
    def test_le_carnet_suit_claim_puis_release(self):
        transport = GoldenTransport()
        db = make_db(transport)
        lease = storage.of(db).leases.claim("inge-front", OWNER, 90.0,
                                             require_responsible=False)
        self.assertEqual(lease["lease_epoch"], 42)
        self.assertEqual(db.book.fence("inge-front"), C.Fence("inge-front", OWNER, 42))
        # une écriture sans owner/epoch reçoit l'enveloppe du carnet
        storage.of(db).agents.set_status("inge-front", "idle", "en attente", None)
        self.assertEqual(transport.calls[-1][1]["fence"],
                         {"agent": "inge-front", "owner": OWNER, "epoch": 42})
        self.assertTrue(storage.of(db).leases.release("inge-front", OWNER, 42))
        self.assertIsNone(db.book.fence("inge-front"))

    def test_sans_bail_connu_la_valeur_de_refus_sans_appel(self):
        transport = GoldenTransport()
        db = make_db(transport)
        self.assertIsNone(storage.of(db).agents.set_status("inge-front", "idle", None, None))
        self.assertFalse(storage.of(db).pending_spend.clear("inge-front"))
        self.assertEqual(storage.of(db).mailbox.release("inge-front", "tok", [1]), 0)
        self.assertEqual(transport.calls, [], "aucun appel réseau sans bail détenu")

    def test_bail_perdu_valeur_de_refus_et_carnet_vide(self):
        responder = Responder()
        transport = GoldenTransport(responder)
        db = make_db(transport)
        db.book.hold("inge-front", OWNER, 42)
        responder.script["leases.begin_turn"] = Fenced()
        self.assertFalse(storage.of(db).leases.begin_turn("inge-front", OWNER, 42, "tour"))
        self.assertIsNone(db.book.fence("inge-front"), "le bail perdu sort du carnet")
        avant = len(transport.calls)
        self.assertIsNone(storage.of(db).agents.set_status("inge-front", "idle", None, None))
        self.assertEqual(len(transport.calls), avant)

    def test_renew_refuse_oublie_le_bail(self):
        responder = Responder()
        db = make_db(GoldenTransport(responder))
        db.book.hold("inge-front", OWNER, 42)
        responder.script["leases.renew"] = None
        self.assertIsNone(storage.of(db).leases.renew("inge-front", OWNER, 42, 90.0))
        self.assertIsNone(db.book.fence("inge-front"))

    def test_bail_perdu_d_un_ancien_epoch_ne_vide_pas_le_nouveau(self):
        responder = Responder()
        db = make_db(GoldenTransport(responder))
        db.book.hold("inge-front", OWNER, 43)
        responder.script["leases.pause"] = Fenced()
        storage.of(db).leases.pause("inge-front", OWNER, 42, "x")
        self.assertEqual(db.book.fence("inge-front").epoch, 43)

    def test_auteur_de_fil_membre_qualifie(self):
        responder = Responder()
        transport = GoldenTransport(responder)
        db = make_db(transport)
        db.book.hold("inge-front", OWNER, 42)
        responder.script["threads.index"] = None
        storage.of(db).threads.index(
            project="site", lot="", transport="file", host="h", location="l", entry_id="e",
            mailbox_ids=[], author="agent:inge-front", excerpt="x", ts=1.0, trace={})
        self.assertEqual(transport.calls[-1][1]["fence"]["agent"], "inge-front")


class NonPrisEnChargeTest(unittest.TestCase):
    def test_toute_operation_refusee_leve_sans_appel(self):
        transport = GoldenTransport()
        db = make_db(transport)
        n = 0
        for domain, methods in CONTRAT.refused.items():
            for method in methods:
                with self.subTest(op="%s.%s" % (domain, method)):
                    fn = getattr(getattr(storage.of(db), domain), method)
                    with self.assertRaises(C.NotSupportedRemotely):
                        fn()
                    n += 1
        self.assertEqual(n + len(CONTRAT.operations), CONTRAT.interface_total)
        self.assertEqual(transport.calls, [])

    def test_routes_selon_le_jeton(self):
        session = make_db(mode="session")
        with self.assertRaises(C.NotSupportedRemotely):
            storage.of(session).agents.get("inge-front")  # route op seule
        executor = make_db()
        with self.assertRaises(C.NotSupportedRemotely):
            storage.of(executor).work.get(812)  # route session/op

    def test_pas_de_sql(self):
        db = make_db()
        for fn in (db.query, db.execute, db.transaction):
            with self.assertRaises(C.NotSupportedRemotely):
                fn("SELECT 1")
        db_mod.require_schema(db)  # le schéma est celui du serveur : rien à faire

    def test_appel_hors_signature(self):
        with self.assertRaises(db_mod.DbError):
            storage.of(make_db()).agents.get("a", "b")


class ErreursTest(unittest.TestCase):
    ATTENDU = {
        "forbidden_scope": C.Forbidden, "host_unavailable": C.Forbidden,
        "executor_revoked": C.ExecutorRevoked, "op_not_allowed": C.NotSupportedRemotely,
        "fence_required": db_mod.DbError, "idempotency_key_required": db_mod.DbError,
        "idempotency_mismatch": db_mod.DbError, "bad_args": db_mod.DbError,
        "token_expired": db_mod.DbError, "rate_limited": db_mod.Unavailable,
        "unavailable": db_mod.Unavailable,
    }

    def test_chaque_erreur_doree_devient_l_exception_du_contrat(self):
        for case in error_cases():
            corps = case["reponse"]["corps"]
            with self.subTest(case=case["nom"]):
                exc = C.client_exception(case["reponse"]["statut"], corps)
                self.assertIsInstance(exc, self.ATTENDU[corps["error"]])
                if corps["error"] == "executor_revoked":
                    self.assertEqual(exc.code, "executor_revoked")

    def test_erreur_du_serveur_remonte_par_le_stockage(self):
        responder = Responder()
        db = make_db(GoldenTransport(responder))
        responder.script["agents.get"] = (403, C.error_body("forbidden_scope", "hors portée"))
        with self.assertRaises(C.Forbidden):
            storage.of(db).agents.get("tresorier")
        responder.script["agents.get"] = (503, C.error_body("unavailable", "base"))
        with self.assertRaises(db_mod.Unavailable):
            storage.of(db).agents.get("inge-front")


# --------------------------------------------------------------------------
# transport HTTP contre un serveur local
# --------------------------------------------------------------------------

def _transport(server, **kw) -> rhttp.HttpTransport:
    kw.setdefault("sleep", lambda _s: None)
    return rhttp.HttpTransport(server.url, tokens=rhttp.StaticTokenSource(ACCESS),
                               session_tokens=rhttp.StaticTokenSource(SESSION), **kw)


class RenouvelleSource(rhttp.StaticTokenSource):
    def __init__(self, first: str, second: str):
        super().__init__(first)
        self.second = second
        self.refreshes = 0

    def access_token(self, *, refresh: bool = False) -> str:
        if refresh:
            self.refreshes += 1
            self._token = self.second
        return super().access_token()


class HttpTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = GoldenServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)

    def test_operation_en_tetes_et_valeur(self):
        db = RemoteDb(None, _transport(self.server), url=self.server.url)
        db.book.hold("inge-front", OWNER, 42)
        self.assertTrue(storage.of(db).leases.begin_turn("inge-front", OWNER, 42,
                                                         "tour en cours"))
        req = self.server.requests[-1]
        self.assertEqual(req["path"], "/api/exec/v1/op")
        self.assertEqual(req["headers"]["Authorization"], "Bearer " + ACCESS)
        self.assertEqual(req["headers"]["Content-Type"], "application/json")
        self.assertTrue(req["headers"].get(C.IDEMPOTENCY_HEADER))
        # lecture : pas de clé
        storage.of(db).agents.get("inge-front")
        self.assertNotIn(C.IDEMPOTENCY_HEADER, self.server.requests[-1]["headers"])

    def test_session_par_le_jeton_de_session(self):
        db = RemoteDb(None, _transport(self.server), mode="session", url=self.server.url)
        row = storage.of(db).work.get(812)
        self.assertEqual(row["id"], 812)
        req = self.server.requests[-1]
        self.assertEqual(req["path"], "/api/exec/v1/session/op")
        self.assertEqual(req["headers"]["Authorization"], "Bearer " + SESSION)

    def test_jeton_echu_renouvele_une_fois(self):
        source = RenouvelleSource("amx1.perime", ACCESS)
        transport = rhttp.HttpTransport(self.server.url, tokens=source, sleep=lambda s: None)
        db = RemoteDb(None, transport, url=self.server.url)
        self.assertEqual(storage.of(db).agents.harnesses(), {"inge-front": "dsh"})
        self.assertEqual(source.refreshes, 1)
        self.assertEqual([r["headers"]["Authorization"] for r in self.server.requests],
                         ["Bearer amx1.perime", "Bearer " + ACCESS])

    def test_jeton_refuse_apres_renouvellement(self):
        transport = rhttp.HttpTransport(
            self.server.url, tokens=rhttp.StaticTokenSource("amx1.faux"), sleep=lambda s: None)
        with self.assertRaises(db_mod.DbError) as ctx:
            storage.of(RemoteDb(None, transport)).agents.harnesses()
        self.assertNotIsInstance(ctx.exception, db_mod.Unavailable)
        self.assertEqual(len(self.server.requests), 2, "un seul nouvel essai")

    def test_panne_passagere_meme_cle_puis_succes(self):
        indispo = (503, {"Retry-After": "0"}, C.error_body("unavailable", "base"))
        self.server.queue("POST", "/op", indispo, indispo)
        db = RemoteDb(None, _transport(self.server), url=self.server.url)
        db.book.hold("inge-front", OWNER, 42)
        storage.of(db).agents.mark_event_wake("inge-front")
        cles = [r["headers"].get(C.IDEMPOTENCY_HEADER) for r in self.server.requests]
        self.assertEqual(len(cles), 3)
        self.assertEqual(len(set(cles)), 1, "la clé est tirée une fois par appel logique")

    def test_panne_durable_unavailable(self):
        attentes = []
        self.server.queue("POST", "/op", *[(429, {"Retry-After": "2"},
                                            C.error_body("rate_limited", "x"))] * 3)
        db = RemoteDb(None, _transport(self.server, sleep=attentes.append))
        with self.assertRaises(db_mod.Unavailable):
            storage.of(db).agents.harnesses()
        self.assertEqual(len(self.server.requests), 1 + rhttp.RETRIES)
        self.assertEqual(attentes, [2.0, 2.0], "Retry-After respecté (borné)")

    def test_serveur_injoignable(self):
        transport = rhttp.HttpTransport("http://127.0.0.1:9", tokens=rhttp.StaticTokenSource(
            ACCESS), retries=1, sleep=lambda s: None, timeout=2.0)
        with self.assertRaises(db_mod.Unavailable):
            storage.of(RemoteDb(None, transport)).agents.harnesses()

    def test_https_obligatoire_hors_boucle_locale(self):
        with self.assertRaises(ValueError):
            rhttp.HttpTransport("http://mesh.exemple")
        rhttp.HttpTransport("https://mesh.exemple")

    def test_flux_sse_dore(self):
        g = golden("evenements")["sse"]
        self.server.queue("GET", "/events", (200, {"Content-Type": "text/event-stream"},
                                             g["reponse"]["texte"]))
        events = []
        with self.assertRaises(db_mod.Unavailable):  # le serveur ferme : coupure
            for ev in _transport(self.server).stream_events("k3f9:16"):
                events.append(ev.to_json())
        self.assertEqual(events, g["evenements"])
        self.assertEqual(self.server.requests[-1]["headers"]["Last-Event-ID"], "k3f9:16")
        self.assertEqual(self.server.requests[-1]["headers"]["Accept"], "text/event-stream")

    def test_flux_non_servi_pas_de_sse(self):
        self.server.queue("GET", "/events", (200, {"Content-Type": "application/json"}, {}))
        with self.assertRaises(C.NotSupportedRemotely):
            list(_transport(self.server).stream_events(None))

    def test_attente_longue_doree(self):
        g = golden("evenements")["attente_longue"]
        self.server.queue("GET", "/events", (200, {}, g["reponse"]["corps"]))
        events, last = _transport(self.server).poll_events("k3f9:16", 25)
        self.assertEqual([e.to_json() for e in events], g["reponse"]["corps"]["events"])
        self.assertEqual(last, "k3f9:19")
        self.assertEqual(self.server.requests[-1]["path"],
                         "/api/exec/v1/events?wait=25&after=k3f9:16")

    def test_fiche_hote_et_jeton_de_session(self):
        ident = golden("identite")
        self.server.queue("GET", "/host", (200, {}, ident["host"]["reponse"]["corps"]))
        self.server.queue("POST", "/session-token",
                          (200, {}, ident["session_token"]["reponse"]["corps"]))
        transport = _transport(self.server)
        info = transport.host_info()
        self.assertEqual((info.host, info.executor_id, info.lease_ttl_s),
                         ("anna-portable", "7f3a9c2e4b1d6058", 90.0))
        issued = transport.session_token(C.Fence("inge-front", OWNER, 42))
        self.assertEqual(issued.token, SESSION)
        self.assertEqual(self.server.requests[-1]["body"],
                         ident["session_token"]["requete"]["corps"])


# --------------------------------------------------------------------------
# flux d'événements (abonnement)
# --------------------------------------------------------------------------

class AbonnementTest(unittest.TestCase):
    def _events(self):
        return [E.Event.from_json(e) for e in golden("evenements")["sse"]["evenements"]]

    def test_sse_rend_les_signaux_puis_down(self):
        transport = GoldenTransport()
        transport.events = self._events()
        db = make_db(transport)
        sub = storage.of(db).wakeups.subscribe(["agent_mail", "agent_lease", "ameesh_budget"])
        self.assertIsInstance(sub, RemoteSubscription)
        self.addCleanup(sub.close)
        got = [sub.wait(2.0) for _ in range(3)]
        self.assertEqual(got[0], {"channel": "agent_mail",
                                  "payload": json.dumps({"to": "inge-front", "id": 812})})
        self.assertEqual([g["channel"] for g in got],
                         ["agent_mail", "agent_lease", "ameesh_budget"])
        down = sub.wait(2.0)
        self.assertEqual(down["event"], "down")
        self.assertEqual(db.events_cursor, "k3f9:19", "curseur gardé pour la reprise")

    def test_reabonnement_reprend_au_curseur(self):
        transport = GoldenTransport()
        db = make_db(transport)
        db.events_cursor = "k3f9:19"
        sub = storage.of(db).wakeups.subscribe(["agent_mail"])
        self.addCleanup(sub.close)
        sub.wait(1.0)
        self.assertEqual(transport.stream_calls[0], "k3f9:19")

    def test_canaux_filtres_mais_reset_toujours(self):
        transport = GoldenTransport()
        transport.events = self._events() + [E.Event("k3f9:20", E.RESET, {"reason": "gap"})]
        sub = storage.of(make_db(transport)).wakeups.subscribe(["agent_lease"])
        self.addCleanup(sub.close)
        self.assertEqual(sub.wait(2.0)["channel"], "agent_lease")
        self.assertEqual(sub.wait(2.0)["channel"], "reset")

    def test_repli_attente_longue(self):
        transport = GoldenTransport()
        transport.stream_error = C.NotSupportedRemotely("pas de SSE")
        g = golden("evenements")["attente_longue"]["reponse"]["corps"]
        transport.polls = [E.parse_long_poll(g)]
        db = make_db(transport)
        sub = storage.of(db).wakeups.subscribe(list(E.CHANNELS))
        self.addCleanup(sub.close)
        self.assertEqual(sub.wait(2.0)["channel"], "agent_mail")
        self.assertTrue(db.events_long_poll)
        deadline = time.monotonic() + 2
        while not transport.poll_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(transport.poll_calls[0][1], E.MAX_WAIT_S)

    def test_close_rend_none(self):
        sub = storage.of(make_db()).wakeups.subscribe(["agent_mail"])
        sub.close()
        sub.close()
        self.assertIsNone(sub.wait(0.01))


if __name__ == "__main__":
    unittest.main()
