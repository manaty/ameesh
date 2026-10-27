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

import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from ameesh import receipts, server_faults
from ameesh.approve import enroll as enroll_mod
from ameesh.approve import server as approve_server
from ameesh.approve.sources import ActionSourceError
from ameesh.db import DbError

from . import test_approve as base
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

    def body(self, path):
        page = self.page_data(self.page(path)[2], "ameesh-enroll")
        return attestation(SoftWebAuthn("ES256"), receipts.b64u_decode(page["challenge"]))

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
        # la page qui l'a envoyé attend ; la page rouverte se recharge seule
        self.assertEqual(self.page(path)[0], 503)
        status, result = self.submit(path, body)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.proposals(), [result["proposal"]])
        # un AUTRE corps sur le lien consommé reste un refus
        status, other = self.submit(path, self.body_for_other(path, body))
        self.assertEqual((status, other["error"]), (410, "used"))

    def body_for_other(self, path, body):
        return dict(body, credential_id=body["credential_id"][:-2] + "AA")

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


if __name__ == "__main__":
    unittest.main()
