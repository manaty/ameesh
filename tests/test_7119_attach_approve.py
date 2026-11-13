# SPDX-License-Identifier: AGPL-3.0-only
"""#7119 (avatar D1) : ameesh-approve émet le grant d'attach (WebAuthn).

Cycle réel par HTTP : `POST /attach-requests` (jeton de service), page
`/attach/<jeton>`, assertion WebAuthn, reçu par `/receipts/<id>`, puis
consommation par l'hôte de l'agent (attach_grant.redeem) avec la preuve de
l'appareil du poste. Un lien d'attach n'est jamais servi par `/a/`.
"""
from __future__ import annotations

import json
import unittest

from ameesh import attach_grant, receipts

from .support import apply_authenticators
from .test_7119_attach_grant import AGENT as AVATAR, HOST, POLICY, TRANSCRIPT, load_canon
from .test_approve import ALICE, ApproveBase, member
from .webauthn_soft import SoftDevice


class AttachApproveTest(ApproveBase):
    def setUp(self) -> None:
        super().setUp()
        self.station = SoftDevice()
        self.bob_station = SoftDevice()
        result = apply_authenticators(self.db, [
            member("alice", self.alice.entry(), self.station.entry()),
            member("bob", self.bob.entry(), self.bob_station.entry())])
        self.assertEqual(result["errors"], [])

    def ask(self, *, approver=ALICE, device=None, expect=201, **extra):
        body = {"agent": AVATAR, "host": HOST, "tenant": "", "approver": approver,
                "device_key": (device or self.station).credential_id,
                "requested_by": approver}
        body.update(extra)
        status, _headers, payload = self.api("POST", "/attach-requests", body)
        self.assertEqual(status, expect, payload)
        return json.loads(payload)

    def test_cycle_complet_jusqu_a_l_hote(self):
        data = self.ask()
        self.assertAlmostEqual(data["exp"] - self.clock(), attach_grant.GRANT_TTL, delta=2)
        path = "/" + data["link"].split("/", 3)[3]
        self.assertTrue(path.startswith("/attach/"), path)
        status, _headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        self.assertIn(AVATAR, text)
        self.assertIn(self.station.credential_id, text)
        page = self.page_data(text)
        self.assertEqual(page["kind"], "attach")
        # le résumé signé est celui que le service a rendu
        self.assertEqual(attach_grant.summary_digest(self.summary_of(text)),
                         data["summary_digest"])

        status, reply = self.submit(path, self.sign(self.alice, page))
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["decision"], "approve")
        status, receipt = self.fetch_receipt(data["request_id"])
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipts.check_request(receipt["request"]), "attach")

        # l'hôte de l'agent consomme, une fois, avec la preuve de l'appareil
        proof = self.station.key.sign(attach_grant.device_message(
            receipts.challenge(receipt["request"]), TRANSCRIPT))
        def redeem():
            return attach_grant.redeem(
                self.db, receipt, canon=load_canon(), agent=AVATAR, host=HOST, tenant="",
                device_key=self.station.credential_id, device_signature=proof,
                transcript_hash=TRANSCRIPT, policy=POLICY, checked_by="runner:" + HOST)
        grant = redeem()
        self.assertTrue(grant.ok, grant.reason)
        self.assertEqual(grant.attach_id, data["attach_id"])
        self.assertEqual(redeem().code, receipts.REPLAY)

    def test_lien_d_attach_refuse_sur_la_route_d_approbation(self):
        data = self.ask()
        token = data["link"].rsplit("/", 1)[1]
        status, _headers, _text = self.page("/a/" + token)
        self.assertEqual(status, 404)

    def test_refus_signe(self):
        data = self.ask()
        path = "/" + data["link"].split("/", 3)[3]
        page = self.page_data(self.page(path)[2])
        status, reply = self.submit(path, self.sign(self.alice, page, "deny"))
        self.assertEqual((status, reply["decision"]), (200, "deny"))
        _status, receipt = self.fetch_receipt(data["request_id"])
        self.assertEqual(receipt["request"]["decision"], "deny")

    def test_autre_humain_ne_signe_pas(self):
        data = self.ask()
        path = "/" + data["link"].split("/", 3)[3]
        page = self.page_data(self.page(path)[2])
        body = self.sign(self.bob, page)
        status, reply = self.submit(path, body)
        self.assertEqual(status, 403, reply)

    def test_appareil_non_enrole_pour_l_approbateur(self):
        # la clé d'appareil de bob, pour un grant d'alice : refusé dès la demande
        reply = self.ask(device=self.bob_station, expect=409)
        self.assertEqual(reply["error"], attach_grant.DEVICE)
        reply = self.ask(device=SoftDevice(), expect=409)
        self.assertEqual(reply["error"], attach_grant.DEVICE)

    def test_demande_mal_formee(self):
        self.assertEqual(self.ask(approver="agent:x", expect=400)["error"], "format")
        self.assertEqual(self.ask(agent="a b", expect=400)["error"], "format")
        status, _h, _p = self.api("POST", "/attach-requests", {"agent": AVATAR},
                                  auth="mauvais")
        self.assertEqual(status, 401)

    def test_lien_expire(self):
        data = self.ask()
        path = "/" + data["link"].split("/", 3)[3]
        self.clock.offset += attach_grant.GRANT_TTL
        status, _headers, _text = self.page(path)
        self.assertEqual(status, 410)
        status, pending = self.fetch_receipt(data["request_id"])
        self.assertEqual((status, pending["status"]), (410, "expired"))


if __name__ == "__main__":
    unittest.main()
