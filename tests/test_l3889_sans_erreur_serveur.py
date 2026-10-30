# SPDX-License-Identifier: AGPL-3.0-only
"""Canon §16 (lot #3889) : aucun client ameesh n'affiche une panne serveur.

- le journal des pannes par empreinte (`ameesh.server_faults`), au contrat du
  lecteur de #3885 : gabarit de route, code d'exception, révision ; aucune
  donnée personnelle ; servi en page (`GET /fingerprints` d'ameesh-approve)
  ou rapporté à la primitive `fingerprints.open_lot` (page de progression) ;
- ameesh-approve : le MÊME corps signé, renvoyé après une panne ou en
  concurrence, rend le reçu déjà gardé (aucun double effet, jamais de
  re-signature) ; une panne répond `retry` et n'est pas un refus ; un refus
  voulu (autre corps sur un lien consommé) reste refusé, jamais rejoué ;
- l'enrôlement : un lien, un corps ; une panne après la consommation du
  lien se reprend avec le même corps, une réponse perdue se relit.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from ameesh import (chat, conversation, mail, receipts, registry, serve, serve_actions,
                    server_faults, storage)
from ameesh import db as db_mod
from ameesh.approve import enroll as enroll_mod
from ameesh.approve import server as approve_server
from ameesh.approve.sources import ActionSourceError
from ameesh.db import DbError

from . import test_approve as base
from .support import PgTestCase
from .test_approve import ORIGIN, ApproveBase, attestation
from .webauthn_soft import SoftWebAuthn

try:
    from ameesh import fingerprints
except ImportError:  # #3885 pas encore fusionné : témoins de bout en bout sautés
    fingerprints = None


class FaultJournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ameesh-faults-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = [1000.0]
        self.path = os.path.join(self.dir, "faults.json")
        self.journal = server_faults.Journal(self.path, "ameesh-test", revision="1.2.3",
                                             clock=lambda: self.now[0])

    def test_une_empreinte_par_route_et_code_au_contrat_3885(self):
        try:
            try:
                raise DbError("password authentication failed for user alice@example.org",
                              sqlstate="28P01")
            except DbError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as exc:
            first = self.journal.record("POST /a/<token>", exc, subject="human:alice")
            self.now[0] = 1060.0
            again = self.journal.record("POST /a/<token>", exc, subject="human:bob")
            self.journal.record("POST /a/<token>", exc, subject="human:alice")
        self.assertEqual(first, again)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        other = self.journal.record("GET /a/<token>", KeyError("x"))
        self.assertNotEqual(other, first)
        entries = self.journal.entries()
        self.assertEqual([e["fingerprint"] for e in entries], [other, first])
        entry = entries[1]
        self.assertEqual(set(entry), set(server_faults.FIELDS))
        self.assertEqual((entry["route"], entry["code"], entry["revision"]),
                         ("ameesh-test POST /a/<token>", "SQLSTATE:28P01", "1.2.3"))
        self.assertEqual((entry["first_seen"], entry["last_seen"], entry["occurrences"],
                          entry["users"]),
                         ("1970-01-01T00:16:40Z", "1970-01-01T00:17:40Z", 3, 2))
        self.assertEqual(entries[0]["code"], "EXCEPTION:KeyError")
        # ni message d'exception, ni sujet en clair sur le disque
        with open(self.path, encoding="utf-8") as fh:
            raw = fh.read()
        for secret in ("alice", "bob", "password", "wrapped"):
            self.assertNotIn(secret, raw)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_la_revision_reste_hors_de_l_empreinte(self):
        later = server_faults.Journal(self.path, "ameesh-test", revision="1.2.4",
                                      clock=lambda: self.now[0])
        a = self.journal.record("GET /", KeyError("k"))
        b = later.record("GET /", KeyError("k"))
        self.assertEqual(a, b)
        self.assertEqual(sorted(e["revision"] for e in self.journal.entries()),
                         ["1.2.3", "1.2.4"])

    def test_route_et_code_bornes_au_lecteur_3885(self):
        class Long(Exception):
            code = "c" * 40
        self.journal.record("GET /" + "x" * 400, Long())
        [entry] = self.journal.entries()
        self.assertLessEqual(len(entry["route"]), server_faults.MAX_ROUTE)
        self.assertLessEqual(len(entry["code"]), server_faults.MAX_CODE)

    def test_page_par_curseur(self):
        self.journal.record("GET /", KeyError("k"))
        page = self.journal.page()
        self.assertEqual((page["schema"], page["cursor"], len(page["fingerprints"])),
                         (server_faults.PAGE_SCHEMA, "1", 1))
        self.assertEqual(self.journal.page("1"),
                         {"schema": server_faults.PAGE_SCHEMA, "cursor": None,
                          "fingerprints": []})
        self.journal.record("GET /", KeyError("k"))
        page = self.journal.page("1")
        self.assertEqual((page["cursor"], page["fingerprints"][0]["occurrences"]), ("2", 2))

    @unittest.skipIf(fingerprints is None, "#3885 (ameesh.fingerprints) absent")
    def test_page_acceptee_par_le_lecteur_3885(self):
        self.journal.record("GET /a/<token>", KeyError("k"), subject="human:alice")
        entries, cursor = fingerprints.parse_page(self.journal.page())
        self.assertEqual((len(entries), cursor, entries[0].users), (1, "1", 1))

    def test_rapport_une_fois_par_occurrence_nouvelle(self):
        calls = []

        def open_lot(db, **fields):
            calls.append(fields)
            return {"lot": 1, "created": len(calls) == 1}
        self.journal.record("GET /", KeyError("k"), subject=server_faults.HUMAN)
        self.assertTrue(self.journal.pending)
        self.journal.report(None, open_lot)
        self.assertFalse(self.journal.pending)
        self.journal.report(None, open_lot)
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["app"], calls[0]["users"], calls[0]["count"]),
                         ("ameesh", 1, 1))
        self.assertEqual(calls[0]["key"], self.journal.entries()[0]["fingerprint"])
        self.journal.record("GET /", KeyError("k"))
        self.journal.report(None, open_lot)
        self.assertEqual([c["count"] for c in calls], [1, 2])

    def test_rapport_echoue_garde_l_entree(self):
        self.journal.record("GET /", KeyError("k"))

        def down(db, **fields):
            raise DbError("connection refused")
        with self.assertRaises(DbError):
            self.journal.report(None, down)
        self.assertTrue(self.journal.pending)

    def test_le_journal_ne_leve_jamais(self):
        blocked = os.path.join(self.dir, "file")
        with open(blocked, "w") as fh:
            fh.write("x")
        journal = server_faults.Journal(os.path.join(blocked, "faults.json"), "ameesh-test",
                                        revision="1")
        with self.assertLogs("ameesh.server_faults", "WARNING"):
            self.assertEqual(journal.record("GET /", KeyError("k")), "")

    def test_fichier_illisible_repart_de_zero(self):
        with open(self.path, "w") as fh:
            fh.write("{pas du json")
        with self.assertLogs("ameesh.server_faults", "WARNING"):
            self.assertTrue(self.journal.record("GET /", KeyError("k")))
        self.assertEqual(len(self.journal.entries()), 1)

    def test_passes_concurrentes_sans_perte(self):
        def burst():
            for _ in range(20):
                self.journal.record("GET /", KeyError("k"))
        threads = [threading.Thread(target=burst) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        [entry] = self.journal.entries()
        self.assertEqual(entry["occurrences"], 80)


class ApproveReplayTest(ApproveBase):
    def faults(self):
        return self.service.faults.entries()

    def test_meme_corps_renvoye_rend_le_recu_sans_second_effet(self):
        _action, data, path = self.create()
        _status, _headers, text = self.page(path)
        page = self.page_data(text)
        body = self.sign(self.alice, page)
        status, first = self.submit(path, body)
        self.assertEqual(status, 200, first)
        _status, receipt = self.fetch_receipt(data["request_id"])
        # la réponse a été perdue : la page renvoie le MÊME corps
        status, again = self.submit(path, body)
        self.assertEqual((status, again["decision"], again["replayed"]),
                         (200, "approve", True), again)
        self.assertEqual(self.fetch_receipt(data["request_id"])[1], receipt)
        # un AUTRE corps sur le lien consommé reste un refus
        status, other = self.submit(path, self.sign(self.alice, page, "deny"))
        self.assertEqual((status, other["error"]), (410, "used"))
        self.assertNotIn("retry", other)
        # même la preuve exacte ne rejoue pas pour une AUTRE décision
        status, other = self.submit(path, dict(body, decision="deny"))
        self.assertEqual(status, 410)
        # la page rouverte dit que la réponse est gardée, pas un échec
        status, _headers, text = self.page(path)
        self.assertEqual(status, 410)
        self.assertIn("Already answered", text)

    def test_deux_envois_concurrents_du_meme_corps_deux_accuses(self):
        _action, data, path = self.create()
        _status, _headers, text = self.page(path)
        body = self.sign(self.alice, self.page_data(text))
        barrier = threading.Barrier(2)
        real = self.service.store.save_receipt_once

        def together(*args):
            # les deux envois ont passé la relecture du reçu : ils écrivent ensemble
            barrier.wait(10)
            return real(*args)
        results = []
        with mock.patch.object(self.service.store, "save_receipt_once", together):
            threads = [threading.Thread(target=lambda: results.append(self.submit(path, body)))
                       for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(sorted(status for status, _r in results), [200, 200], results)
        self.assertEqual(sum(1 for _s, r in results if r.get("replayed")), 1)
        self.assertEqual(self.fetch_receipt(data["request_id"])[0], 200)

    def test_course_avant_la_lecture_live_deux_accuses(self):
        """B3 (cx2) : le second envoi lit le lien APRÈS l'écriture du premier
        (refus « used » de la lecture live) : il rend le reçu gardé."""
        _action, data, path = self.create()
        _status, _headers, text = self.page(path)
        body = self.sign(self.alice, self.page_data(text))
        first_done = threading.Event()
        real = self.service._live_record
        calls = []

        def ordered(token):
            calls.append(1)
            if len(calls) == 2:          # le second attend que le premier ait écrit
                first_done.wait(10)
            return real(token)
        results = {}
        with mock.patch.object(self.service, "_live_record", ordered):
            def one(name):
                results[name] = self.submit(path, body)
                if name == "a":
                    first_done.set()
            a = threading.Thread(target=one, args=("a",))
            a.start()
            while not calls:
                threading.Event().wait(0.01)
            b = threading.Thread(target=one, args=("b",))
            b.start()
            a.join(30)
            b.join(30)
        self.assertEqual((results["a"][0], results["b"][0]), (200, 200), results)
        self.assertTrue(results["b"][1]["replayed"])
        self.assertEqual(self.fetch_receipt(data["request_id"])[0], 200)

    def test_panne_du_registre_puis_rejeu_du_meme_corps(self):
        _action, _data, path = self.create()
        _status, _headers, text = self.page(path)
        body = self.sign(self.alice, self.page_data(text))
        real = receipts.verify_receipt
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise DbError("connection to 10.1.2.3 refused (alice)")
            return real(*args, **kwargs)
        with mock.patch.object(receipts, "verify_receipt", flaky):
            status, headers, payload = self.http("POST", path, body, origin=ORIGIN)
            result = json.loads(payload)
            self.assertEqual((status, result["retry"]), (503, True), result)
            self.assertEqual(headers.get("retry-after"), str(approve_server.RETRY_AFTER))
            self.assertNotIn("10.1.2.3", payload.decode())
            # rien d'écrit : le même corps, renvoyé, est accepté une fois
            status, result = self.submit(path, body)
            self.assertEqual((status, result["decision"]), (200, "approve"), result)
            self.assertNotIn("replayed", result)
        [entry] = self.faults()
        self.assertEqual((entry["route"], entry["code"], entry["occurrences"], entry["users"]),
                         ("ameesh-approve POST /a/<token>",
                          "EXCEPTION:ApproveError:registry<-DbError", 1, 1))
        raw = json.dumps(self.faults())
        self.assertNotIn(path.rsplit("/", 1)[1], raw)
        self.assertNotIn("alice", raw)

    def test_page_en_panne_se_recharge_seule(self):
        _action, _data, path = self.create()

        def down(_action_id):
            raise ActionSourceError("source db at 10.1.2.3 down")
        with mock.patch.object(self.source, "get_action", down):
            status, headers, text = self.page(path)
        self.assertEqual(status, 503)
        self.assertIn("Just a moment", text)
        self.assertIn('http-equiv="refresh"', text)
        self.assertNotIn("10.1.2.3", text)
        self.assertNotIn("rror", text.split("<body>", 1)[1])
        self.assertSecurityHeaders(headers)
        [entry] = self.faults()
        self.assertEqual(entry["route"], "ameesh-approve GET /a/<token>")

    def test_exception_imprevue_reste_a_renvoyer(self):
        _action, _data, path = self.create()
        _status, _headers, text = self.page(path)
        body = self.sign(self.alice, self.page_data(text))
        with mock.patch.object(self.service.store, "save_receipt_once",
                               side_effect=PermissionError("/srv/state alice")):
            status, result = self.submit(path, body)
        self.assertEqual((status, result["error"], result["retry"]), (500, "internal", True))
        self.assertNotIn("alice", json.dumps(result))
        status, result = self.submit(path, body)
        self.assertEqual(status, 200, result)
        [entry] = self.faults()
        self.assertEqual(entry["code"], "EXCEPTION:PermissionError")


class ApproveFingerprintsRouteTest(ApproveBase):
    """`GET /fingerprints` : la source de la passe `fingerprints` (#3885)."""

    TOKEN = "f" * 43

    def open_route(self):
        self.service._fingerprints_digest = hashlib.sha256(self.TOKEN.encode()).digest()

    def read(self, path="/fingerprints", token=TOKEN, **kwargs):
        kwargs.setdefault("host", self.local)
        status, _headers, payload = self.http("GET", path, auth=token, **kwargs)
        return status, json.loads(payload)

    def test_fermee_sans_jeton_dedie(self):
        self.assertEqual(self.read()[0], 401)
        self.assertEqual(self.read(token=self.service_token)[0], 401)

    def test_page_au_porteur_du_jeton(self):
        self.open_route()
        self.assertEqual(self.read(token=self.service_token)[0], 401)
        self.assertEqual(self.read(origin=ORIGIN)[0], 403)
        self.service.faults.record("POST /a/<token>", KeyError("k"), subject="human:alice")
        status, page = self.read()
        self.assertEqual((status, page["cursor"], len(page["fingerprints"])), (200, "1", 1),
                         page)
        self.assertEqual(self.read("/fingerprints?since=1")[1]["fingerprints"], [])
        self.assertEqual(self.read("/fingerprints?since=x")[0], 400)
        if fingerprints is not None:
            entries, _cursor = fingerprints.parse_page(page)
            result = fingerprints.open_lot(
                self.db, app="ameesh", key=entries[0].fingerprint,
                title=fingerprints.title_of(entries[0]), users=entries[0].users,
                count=entries[0].occurrences, route=entries[0].route, code=entries[0].code,
                revision=entries[0].revision, first_seen=entries[0].first_seen,
                last_seen=entries[0].last_seen)
            self.assertTrue(result["created"])
            self.assertEqual(result["priority"], 1)

    @unittest.skipIf(fingerprints is None, "#3885 (ameesh.fingerprints) absent")
    def test_rapport_direct_ouvre_puis_note_un_lot(self):
        self.service.faults.record("GET /a/<token>", KeyError("k"), subject="human:alice")
        [first] = self.service.faults.report(self.db)
        self.assertTrue(first["created"])
        self.service.faults.record("GET /a/<token>", KeyError("k"), subject="human:alice")
        [again] = self.service.faults.report(self.db)
        self.assertEqual((again["created"], again["lot"]), (False, first["lot"]))
        self.assertEqual(self.service.faults.report(self.db), [])


class EnrollReplayTest(ApproveBase):
    enroll_link = base.ApproveEnrollTest.enroll_link
    proposals = base.ApproveEnrollTest.proposals

    def body(self, path, *, credential_id=None):
        device = SoftWebAuthn("ES256")
        if credential_id is not None:
            device.credential_id = credential_id
        page = self.page_data(self.page(path)[2], "ameesh-enroll")
        return attestation(device, receipts.b64u_decode(page["challenge"]))

    def test_reponse_perdue_le_meme_corps_rend_la_proposition(self):
        path = self.enroll_link()
        body = self.body(path)
        status, first = self.submit(path, body)
        self.assertEqual(status, 200, first)
        status, again = self.submit(path, body)
        self.assertEqual((status, again["proposal"], again["replayed"]),
                         (200, first["proposal"], True), again)
        self.assertEqual(self.proposals(), [first["proposal"]])
        status, _headers, text = self.page(path)
        self.assertEqual(status, 410)
        self.assertIn("Passkey already sent", text)

    def test_panne_de_la_proposition_reprise_avec_le_meme_corps(self):
        path = self.enroll_link()
        body = self.body(path)
        with mock.patch.object(enroll_mod, "write_proposal",
                               side_effect=OSError("disk full")):
            status, result = self.submit(path, body)
        self.assertEqual((status, result.get("retry")), (500, True), result)
        self.assertEqual(self.proposals(), [])
        # la page qui l'a envoyé attend ; la page rouverte renvoie le corps
        # gardé — une attente voulue, pas une panne : non journalisée (B5 dev3)
        self.assertEqual(self.page(path)[0], 503)
        self.assertEqual([e["route"] for e in self.service.faults.entries()],
                         ["ameesh-approve POST /enroll/<token>"])
        status, result = self.submit(path, body)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.proposals(), [result["proposal"]])
        # un AUTRE corps sur le lien consommé reste un refus
        status, other = self.submit(path, self.body_for_other(path, body))
        self.assertEqual((status, other["error"]), (410, "used"))

    def test_autre_corps_meme_si_l_identifiant_finit_par_AA(self):
        """#4682 : l'identifiant b64u(16 octets) finit par « AA » une fois sur
        256 ; le corps « autre » doit alors encore différer, sinon le serveur
        le rejoue (200, sans clé « error ») au lieu de refuser le lien usé."""
        path = self.enroll_link()
        body = self.body(path, credential_id=receipts.b64u_encode(bytes(16)))
        self.assertTrue(body["credential_id"].endswith("AA"))
        self.assertEqual(self.submit(path, body)[0], 200)
        status, other = self.submit(path, self.body_for_other(path, body))
        self.assertEqual((status, other["error"]), (410, "used"))

    def test_proposition_ecrite_puis_reponse_non_gardee_aucun_doublon(self):
        """B2 (cx2) : la proposition est écrite, garder la réponse échoue ;
        une reprise plus tard (horloge avancée) rend la MÊME proposition."""
        path = self.enroll_link()
        body = self.body(path)
        real = self.service.store.settle_enroll
        with mock.patch.object(self.service.store, "settle_enroll",
                               side_effect=[OSError("disk full"), None]) as settle:
            status, result = self.submit(path, body)
            self.assertEqual((status, result.get("retry")), (500, True), result)
            [written] = self.proposals()
            # une écriture interrompue a laissé un fichier tronqué
            proposal = os.path.join(self.acfg.proposals_path, written)
            with open(proposal, "r+b") as fh:
                fh.truncate(20)
            self.clock.offset += 7
            settle.side_effect = real
            status, result = self.submit(path, body)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.proposals(), [written])
        self.assertEqual(result["proposal"], written)
        with open(proposal, encoding="utf-8") as fh:
            self.assertIn("authenticators:", fh.read())
        status, again = self.submit(path, body)
        self.assertEqual((status, again["proposal"], again["replayed"]), (200, written, True))

    def test_page_d_attente_porte_la_reprise(self):
        path = self.enroll_link()
        body = self.body(path)
        with mock.patch("ameesh.approve.enroll.write_proposal",
                        side_effect=OSError("disk full")):
            self.assertEqual(self.submit(path, body)[0], 500)
        status, _headers, text = self.page(path)
        self.assertEqual(status, 503)
        self.assertIn('id="ameesh-wait"', text)       # approve.js renvoie le corps gardé
        self.assertIn('<script src="../static/approve.js"', text)

    def body_for_other(self, path, body):
        # #4682 : b64u(16 octets) finit par A, Q, g ou w. Remplacer la fin par
        # « AA » peut donc ne rien changer (1/256) : le corps serait identique
        # et rejoué (200) au lieu d'être refusé. Choisir un suffixe différent.
        suffix = "AQ" if body["credential_id"].endswith("AA") else "AA"
        return dict(body, credential_id=body["credential_id"][:-2] + suffix)

    def test_deux_envois_concurrents_une_proposition(self):
        path = self.enroll_link()
        body = self.body(path)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.submit(path, body)))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(status for status, _r in results), [200, 200], results)
        self.assertEqual(len(self.proposals()), 1)


class LoginReplayTest(unittest.TestCase):
    """B3 (cx2) : done/cancel d'un défi, rejoués ou concurrents — sans base."""

    def setUp(self):
        from types import SimpleNamespace
        from urllib.parse import urlsplit
        from ameesh.approve.config import ApproveConfig
        from ameesh.approve.service import ApproveService
        from .test_approve import AGENT, ALICE
        directory = tempfile.TemporaryDirectory(prefix="l3889-login-")
        self.addCleanup(directory.cleanup)
        self.service = ApproveService(ApproveConfig(
            state_dir=directory.name, public_url="https://approve.example.test"),
            None, None, service_token="fake", clock=lambda: 1000)
        self.service._credentials = lambda _approver: ["fake-credential"]
        first = self.service.create_login_request(dict(
            host="h", provider="gh", account="a", approver=ALICE, requested_by=AGENT,
            url="https://github.com/login/device", code="CANARY-CODE-1234"))
        self.login_id = first["login_id"]
        self.token = urlsplit(first["link"]).path.rsplit("/", 1)[1]
        self.service.store.mark_login_viewed(self.login_id)
        from ameesh.approve import service as service_mod
        self.service._login_receipt = lambda record, body, decision: (
            service_mod._receipt_of(dict(record["request"], decision=decision), body),
            SimpleNamespace(ok=True, approver=ALICE))
        self.body = {"decision": "done", "credential_id": "Y3JlZA",
                     "authenticatorData": "YXV0aA", "clientDataJSON": "Y2xpZW50",
                     "signature": "c2ln"}

    def test_meme_corps_rejoue_autre_corps_refuse(self):
        from ameesh.approve.service import ApproveError
        self.assertNotIn("replayed", self.service.login_submit(self.token, self.body))
        again = self.service.login_submit(self.token, self.body)
        self.assertTrue(again["replayed"])
        with self.assertRaises(ApproveError) as caught:
            self.service.login_submit(self.token, dict(self.body, signature="b3RoZXI"))
        self.assertEqual((caught.exception.status, caught.exception.code), (410, "used"))

    def test_course_avant_le_verrou_deux_accuses(self):
        """Deux envois passent la première lecture, puis se suivent sous le
        verrou : le second voit le reçu (410 « used ») et rend le gardé."""
        store = self.service.store
        real = store.login_transition
        barrier = threading.Barrier(2)

        def together(login_id):
            barrier.wait(10)
            return real(login_id)
        results, errors = [], []

        def one():
            try:
                results.append(self.service.login_submit(self.token, self.body))
            except Exception as exc:  # noqa: BLE001 — le témoin le montre
                errors.append(exc)
        with mock.patch.object(store, "login_transition", together):
            threads = [threading.Thread(target=one) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(bool(r.get("replayed")) for r in results), [False, True])



class ChatReconcileTest(PgTestCase):
    """B1 (cx3), B (dev3) : l'issue inconnue d'un message de la conversation
    se lève par SA clé, côté serveur : le courrier porte l'Idempotency-Key ;
    une réservation restée sans réponse est réconciliée — courrier trouvé :
    sa réponse est rejouée (reflet complété, aucun second courrier) ; aucun :
    la clé est reprise et l'action tourne une fois sous elle."""

    KEY = "web-3889-reconcile-01"

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE api_idempotency, agent_mailbox, conversation_log,"
                        " conversation_heads CASCADE")
        registry.upsert(self.db, "a1", chantier="demo", harness="codex", host="h1")
        self.ctx = serve.Context(cfg=dataclasses.replace(self.cfg, read_only=True),
                                 human="human:owner")
        self.action, self.params = serve.match_action("/api/v1/agents/a1/chat")
        self.body = {"text": "bonjour"}
        self.digest = serve.fingerprint("/api/v1/agents/a1/chat", self.body)

    def act(self, key=KEY, body=None):
        return serve.act(self.ctx, self.db, self.action, self.params, body or self.body,
                         key, self.digest)

    def age(self, seconds):
        self.db.execute("update api_idempotency set created_at = now() - %s * interval '1 second'",
                        (seconds,))

    def mails(self):
        return self.db.query("select sender, payload::text as payload from agent_mailbox"
                             " order by id")

    def texts(self):
        return [e["text"] for e in conversation.history(self.db, "a1")["entries"]
                if e["kind"] == "human_message"]

    def test_le_courrier_porte_la_cle(self):
        status, data, replayed = self.act()
        self.assertEqual((status, replayed), (200, False))
        [mail_row] = self.mails()
        self.assertEqual(json.loads(mail_row["payload"])["idempotency_key"], self.KEY)

    def test_courrier_parti_reflet_perdu_rejoue_sans_second_courrier(self):
        real = conversation.Log.append
        with mock.patch.object(conversation.Log, "append",
                               side_effect=RuntimeError("connection lost")):
            with self.assertRaises(RuntimeError):
                self.act()
        self.assertEqual(len(self.mails()), 1)          # délivré, issue inconnue
        status, data, _r = self.act()
        self.assertEqual((status, data["error"]), (409, "idempotency_in_progress"))
        self.age(serve.RECONCILE_AFTER_S + 1)
        self.assertIs(conversation.Log.append, real)
        status, data, replayed = self.act()
        self.assertEqual((status, replayed), (200, True))
        self.assertEqual(data["result"]["message_id"],
                         self.db.query("select id from agent_mailbox")[0]["id"])
        self.assertEqual(len(self.mails()), 1)
        self.assertEqual(self.texts(), ["bonjour"])     # le reflet complété
        again, data2, replayed2 = self.act()
        self.assertEqual((again, data2, replayed2), (200, data, True))
        self.assertEqual(len(self.mails()), 1)

    def test_aucun_courrier_l_action_tourne_une_fois_sous_la_meme_cle(self):
        requests = storage.of(self.db).api_requests
        requests.reserve("human:owner", self.KEY, self.digest, retention_s=86400)
        self.age(serve.RECONCILE_AFTER_S + 1)
        status, data, replayed = self.act()
        self.assertEqual((status, replayed), (200, False))
        [mail_row] = self.mails()
        self.assertEqual(json.loads(mail_row["payload"])["idempotency_key"], self.KEY)
        self.assertEqual(self.act()[2], True)
        self.assertEqual(len(self.mails()), 1)

    def test_deux_reprises_synchronisees_un_seul_courrier(self):
        """B (cx2, gel #21556) : deux reprises simultanées d'une réservation
        abandonnée : une seule la reprend (retake atomique), un seul courrier."""
        storage.of(self.db).api_requests.reserve("human:owner", self.KEY, self.digest,
                                                 retention_s=86400)
        self.age(serve.RECONCILE_AFTER_S + 1)
        barrier, results = threading.Barrier(2), []

        def one():
            db = db_mod.connect(self.cfg)
            try:
                barrier.wait(10)
                results.append(serve.act(self.ctx, db, self.action, self.params,
                                         self.body, self.KEY, self.digest)[0])
            finally:
                db.close()
        threads = [threading.Thread(target=one) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(len(self.mails()), 1)

    def test_premiere_requete_vivante_au_dela_du_seuil_un_seul_courrier(self):
        """B (cx2) : aucune trace à la reprise, mais la première requête,
        encore vivante, dépose son courrier juste après : la reprise rend ce
        courrier (index 0497), jamais un second."""
        storage.of(self.db).api_requests.reserve("human:owner", self.KEY, self.digest,
                                                 retention_s=86400)
        self.age(serve.RECONCILE_AFTER_S + 1)
        first = {}
        real = self.action.outcome

        def outcome(ctx, db, params, body):
            found = real(ctx, db, params, body)
            # la première requête dépose maintenant, sous la même clé
            first["out"] = serve_actions._chat(ctx, db, params, body)
            return found
        action = dataclasses.replace(self.action, outcome=outcome)
        status, data, _r = serve.act(self.ctx, self.db, action, self.params, self.body,
                                     self.KEY, self.digest)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.mails()), 1)
        self.assertEqual(data["result"]["message_id"], first["out"]["message_id"])
        self.assertEqual(self.texts(), ["bonjour"])

    def test_rejeu_apres_la_purge_de_la_reservation_un_seul_courrier(self):
        """B1 (qa-01, gel #21556) : la page garde clé et corps 7 jours, la
        réservation est purgée après 24 h : le même envoi est rejoué depuis
        le courrier de sa clé, jamais refait."""
        status, first, _r = self.act()
        self.assertEqual(status, 200)
        self.db.execute("delete from api_idempotency")       # la purge de 24 h
        status, data, replayed = self.act()
        self.assertEqual((status, replayed), (200, True))
        self.assertEqual(data["result"]["message_id"], first["result"]["message_id"])
        self.assertEqual(len(self.mails()), 1)
        self.assertEqual(self.texts(), ["bonjour"])
        self.assertEqual(self.act()[:1], (200,))                # rejeu stocké
        self.assertEqual(len(self.mails()), 1)

    def test_meme_cle_autre_texte_apres_la_purge_refusee(self):
        self.assertEqual(self.act()[0], 200)
        self.db.execute("delete from api_idempotency")
        other = {"text": "autre chose"}
        status, data, _r = serve.act(self.ctx, self.db, self.action, self.params, other,
                                     self.KEY, serve.fingerprint("/api/v1/agents/a1/chat",
                                                                 other))
        self.assertEqual((status, data["error"]), (422, "idempotency_mismatch"))
        self.assertEqual(len(self.mails()), 1)

    def test_collision_sous_la_cle_avec_un_autre_texte_refusee_sans_reflet(self):
        """B (cx1, gel #21581) : le dépôt sous une clé déjà prise (0497) rend
        le courrier existant seulement s'il porte ce texte et ce lot ; sinon
        422, aucun reflet du nouveau texte."""
        self.assertEqual(self.act()[0], 200)
        ctx = dataclasses.replace(self.ctx, idempotency_key=self.KEY)
        with self.assertRaises(serve_actions.ActionError) as caught:
            serve_actions._chat(ctx, self.db, self.params, {"text": "autre chose"})
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (422, "idempotency_mismatch"))
        self.assertEqual(len(self.mails()), 1)
        self.assertEqual(self.texts(), ["bonjour"])
        same = serve_actions._chat(ctx, self.db, self.params, {"text": "bonjour"})
        self.assertEqual(same["message_id"], self.db.query("select id from agent_mailbox")[0]["id"])
        self.assertEqual(self.texts(), ["bonjour"])

    def test_un_autre_index_unique_n_est_pas_pris_pour_un_rejeu(self):
        error = DbError("duplicate", sqlstate="23505", constraint="autre_idx")
        with mock.patch.object(mail, "send", side_effect=error):
            with self.assertRaises(DbError):
                chat.post_human_message(self.cfg, self.db, human="human:owner",
                                        sender="owner", agent="a1", text="x",
                                        idempotency_key=self.KEY)

    def test_trop_recent_reste_409_rien_envoye(self):
        storage.of(self.db).api_requests.reserve("human:owner", self.KEY, self.digest,
                                                 retention_s=86400)
        status, data, _r = self.act()
        self.assertEqual((status, data["error"]), (409, "idempotency_in_progress"))
        self.assertEqual(self.mails(), [])

    def test_meme_texte_sous_une_autre_cle_ou_d_un_autre_n_acquitte_pas(self):
        # deux gestes identiques : chacun sa clé, chacun son courrier
        self.assertEqual(self.act(key="web-3889-reconcile-00")[0], 200)
        chat.post_human_message(self.cfg, self.db, human="human:other", sender="other",
                                agent="a1", text="bonjour", idempotency_key=self.KEY)
        storage.of(self.db).api_requests.reserve("human:owner", self.KEY, self.digest,
                                                 retention_s=86400)
        self.age(serve.RECONCILE_AFTER_S + 1)
        status, data, replayed = self.act()
        self.assertEqual((status, replayed), (200, False))
        self.assertEqual(len(self.mails()), 3)
        self.assertEqual([json.loads(r["payload"]).get("idempotency_key")
                          for r in self.mails() if r["sender"] == "human:owner"],
                         ["web-3889-reconcile-00", self.KEY])


if __name__ == "__main__":
    unittest.main()
