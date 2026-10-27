# SPDX-License-Identifier: AGPL-3.0-only
"""Canon §16 (lot #3889) : aucun client ameesh n'affiche une panne serveur.

- le journal des pannes par empreinte (`ameesh.server_faults`) : gabarit de
  route, classe d'exception, version ; aucune donnée personnelle ;
- ameesh-approve : le MÊME corps signé, renvoyé après une panne, rend le reçu
  déjà gardé (aucun double effet, jamais de re-signature) ; une panne répond
  `retry` et n'est pas un refus ; un refus voulu (autre signature sur un lien
  consommé) reste refusé, et n'est jamais rejoué.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from ameesh import receipts, server_faults
from ameesh.approve import server as approve_server
from ameesh.approve.sources import ActionSourceError
from ameesh.db import DbError

from .test_approve import ORIGIN, ApproveBase


class FaultJournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ameesh-faults-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = [1000.0]
        self.path = os.path.join(self.dir, "faults.json")
        self.journal = server_faults.Journal(self.path, "ameesh-test", revision="1.2.3",
                                             clock=lambda: self.now[0])

    def test_une_empreinte_par_route_exception_version(self):
        try:
            try:
                raise DbError("password authentication failed for user alice@example.org",
                              sqlstate="28P01")
            except DbError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as exc:
            first = self.journal.record("POST /a/<token>", exc, subject="tok-1")
            self.now[0] = 1060.0
            again = self.journal.record("POST /a/<token>", exc, subject="tok-2")
            self.journal.record("POST /a/<token>", exc, subject="tok-1")
        self.assertEqual(first, again)
        other = self.journal.record("GET /a/<token>", KeyError("x"))
        self.assertNotEqual(other, first)
        entries = self.journal.entries()
        self.assertEqual([e["empreinte"] for e in entries], [other, first])
        entry = entries[1]
        self.assertEqual(set(entry), set(server_faults.FIELDS))
        self.assertEqual((entry["route"], entry["exception"], entry["revision"]),
                         ("POST /a/<token>", "RuntimeError<-DbError:28P01", "1.2.3"))
        self.assertEqual((entry["first_seen"], entry["last_seen"], entry["occurrences"],
                          entry["users"]), (1000, 1060, 3, 2))
        # ni message d'exception, ni sujet en clair sur le disque
        with open(self.path, encoding="utf-8") as fh:
            raw = fh.read()
        for secret in ("alice", "password", "tok-1", "wrapped"):
            self.assertNotIn(secret, raw)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

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
        with open(os.path.join(self.acfg.state_dir, "faults.json"), encoding="utf-8") as fh:
            return json.load(fh)["faults"]

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
        # même le corps exact ne rejoue pas pour une AUTRE décision
        status, other = self.submit(path, dict(body, decision="deny"))
        self.assertEqual(status, 410)
        # la page rouverte dit que la réponse est gardée, pas un échec
        status, _headers, text = self.page(path)
        self.assertEqual(status, 410)
        self.assertIn("Already answered", text)

    def test_panne_du_registre_puis_rejeu_du_meme_corps(self):
        _action, data, path = self.create()
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
        [entry] = self.faults().values()
        self.assertEqual((entry["route"], entry["exception"], entry["occurrences"]),
                         ("POST /a/<token>", "ApproveError:registry<-DbError", 1))
        self.assertEqual(len(entry["subjects"]), 1)
        raw = json.dumps(self.faults())
        self.assertNotIn(path.rsplit("/", 1)[1], raw)

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
        [entry] = self.faults().values()
        self.assertEqual(entry["route"], "GET /a/<token>")

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
        [entry] = self.faults().values()
        self.assertEqual(entry["exception"], "PermissionError")


if __name__ == "__main__":
    unittest.main()
