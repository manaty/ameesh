# SPDX-License-Identifier: AGPL-3.0-only
"""#7119 (avatar D1) : grant d'attach — qui, preuve, appareil, usage unique, audit.

Décision Q3 #213938 (option a) : le `responsible` ou la liste explicite du
canon, un deputy sous sa propre identité ; reçu WebAuthn de 5 min, à usage
unique, lié à la clé d'appareil du poste. Chaque issue est journalisée.
Authentificateurs logiciels (tests/webauthn_soft.py), aucun réseau.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from ameesh import attach_grant, attach_grant_cli, canon as canon_mod, receipts

from .support import PgTestCase, apply_authenticators
from .webauthn_soft import ORIGIN, RP_ID, SoftDevice, SoftWebAuthn, make_receipt

ALICE, BOB, DAN = "human:alice", "human:bob", "human:dan"
AGENT, HOST, TENANT = "avatar-alice", "ameesh-agents-2", ""
POLICY = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,))
#: hash du transcript E2E que la preuve d'appareil lie (D2, #7120)
TRANSCRIPT = hashlib.sha256(b"ameesh-7119-transcript").digest()
COMMIT = "a" * 40


def write(root: str, rel: str, text: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def load_canon() -> canon_mod.Canon:
    with tempfile.TemporaryDirectory() as root:
        # dan est deputy d'alice : il n'est PAS dans la liste
        write(root, "members/alice.md",
              "---\ntype: Member\ntitle: alice\ndeputies: [human:dan]\n---\n")
        for human in ("bob", "dan"):
            write(root, "members/%s.md" % human, "---\ntype: Member\ntitle: %s\n---\n" % human)
        write(root, "agents/avatar.md",
              "---\ntype: Persona\ntitle: %s\nresponsible: human:alice\navatar:\n"
              "  attach_authorized: [human:bob, agent:mty-amh-impl-12, human:ghost]\n---\n"
              % AGENT)
        write(root, "agents/other.md",
              "---\ntype: Persona\ntitle: other\nresponsible: human:bob\n---\n")
        return canon_mod.load(root, untrusted=True)


class AuthorizedTest(unittest.TestCase):
    def test_responsible_et_liste_seulement(self):
        canon = load_canon()
        humans, problems = attach_grant.authorized(canon, AGENT)
        self.assertEqual(humans, [ALICE, BOB])
        # un agent ou un inconnu de la liste est écarté, et signalé
        self.assertEqual(len(problems), 2, problems)
        self.assertEqual(attach_grant.check_authorized(canon, AGENT, BOB), ("", ""))
        # le deputy d'alice n'hérite de rien
        self.assertEqual(attach_grant.check_authorized(canon, AGENT, DAN)[0],
                         attach_grant.NOT_AUTHORIZED)
        self.assertEqual(attach_grant.check_authorized(canon, "absent", ALICE)[0],
                         attach_grant.UNKNOWN_AGENT)

    def test_demande_bornee_a_cinq_minutes(self):
        request, summary = attach_grant.build_request(
            approver=ALICE, agent=AGENT, host=HOST, tenant=TENANT, device_key="device:x",
            requested_by=ALICE)
        self.assertEqual(request["exp"] - request["iat"], attach_grant.GRANT_TTL)
        self.assertEqual(receipts.check_request(dict(request, decision="approve")), "attach")
        self.assertEqual(request["summary_digest"], attach_grant.summary_digest(summary))
        with self.assertRaises(receipts.ReceiptError):
            attach_grant.build_request(
                approver=ALICE, agent=AGENT, host=HOST, tenant=TENANT, device_key="device:x",
                requested_by=ALICE, ttl=attach_grant.GRANT_TTL + 1)
        with self.assertRaises(receipts.ReceiptError):
            attach_grant.build_request(
                approver="agent:x", agent=AGENT, host=HOST, tenant=TENANT,
                device_key="device:x", requested_by=ALICE)


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(canon_mod, "load_configured", return_value=[load_canon()]), \
                mock.patch("ameesh.config.load"), mock.patch(
                    "ameesh.approve_client.create_attach_request") as create, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = attach_grant_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue(), create

    def test_who(self):
        code, out, _err, _create = self.run_cli("who", AGENT)
        self.assertEqual(code, 0)
        self.assertIn("human:alice, human:bob", out)
        self.assertIn("human:ghost", out)

    def test_request_refuse_avant_le_service(self):
        code, _out, err, create = self.run_cli(
            "request", AGENT, "--host", HOST, "--device-key", "device:x", "--approver", DAN)
        self.assertEqual(code, 1)
        self.assertIn("not_authorized", err)
        create.assert_not_called()
        # deux humains admis : l'approbateur doit être nommé
        code, _out, err, create = self.run_cli(
            "request", AGENT, "--host", HOST, "--device-key", "device:x")
        self.assertEqual(code, 2)
        create.assert_not_called()


class RedeemTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute(
            "TRUNCATE authenticators, mesh_consumed_nonces, attach_grant_events "
            "RESTART IDENTITY CASCADE")
        self.canon = load_canon()
        self.alice = SoftWebAuthn("ES256")
        self.alice_station = SoftDevice()
        self.bob = SoftWebAuthn("ES256")
        self.bob_station = SoftDevice()
        self.dan = SoftWebAuthn("ES256")
        self.dan_station = SoftDevice()
        members = [
            {"title": name, "canon_ref": "members/%s.md@%s" % (name, COMMIT),
             "authenticators": [key.entry(), station.entry()]}
            for name, key, station in (("alice", self.alice, self.alice_station),
                                       ("bob", self.bob, self.bob_station),
                                       ("dan", self.dan, self.dan_station))]
        self.assertEqual(apply_authenticators(self.db, members)["errors"], [])

    def grant(self, signer=None, station=None, *, approver=ALICE, ttl=attach_grant.GRANT_TTL,
              decision="approve", agent=AGENT, **request_fields):
        signer = signer or self.alice
        station = station or self.alice_station
        request, _summary = attach_grant.build_request(
            approver=approver, agent=agent, host=HOST, tenant=TENANT,
            device_key=station.credential_id, requested_by=approver)
        request.update(request_fields)
        if ttl != attach_grant.GRANT_TTL:
            request["exp"] = request["iat"] + ttl
        return signer.receipt(dict(request, decision=decision))

    def redeem(self, receipt, station=None, *, agent=AGENT, host=HOST, device_key=None,
               signature=None):
        station = station or self.alice_station
        if signature is None:
            chal = receipts.challenge(receipt["request"])
            signature = station.key.sign(attach_grant.device_message(chal, TRANSCRIPT))
        return attach_grant.redeem(
            self.db, receipt, canon=self.canon, agent=agent, host=host, tenant=TENANT,
            device_key=device_key or station.credential_id, device_signature=signature,
            transcript_hash=TRANSCRIPT, policy=POLICY,
            checked_by="runner:ameesh-agents-2")

    def events(self):
        return list(reversed(attach_grant.audit(self.db)))

    def assertRefused(self, grant, code):
        self.assertFalse(grant.ok, grant.reason)
        self.assertEqual(grant.code, code, grant.reason)
        last = attach_grant.audit(self.db, limit=1)[0]
        self.assertEqual((last["outcome"], last["code"]), ("refused", code))

    def test_grant_du_responsable_puis_rejeu_refuse(self):
        receipt = self.grant()
        grant = self.redeem(receipt)
        self.assertTrue(grant.ok, grant.reason)
        self.assertEqual((grant.human, grant.agent, grant.device_key),
                         (ALICE, AGENT, self.alice_station.credential_id))
        self.assertIsNotNone(grant.event_id)
        # usage unique : le même reçu, rejoué, est refusé
        self.assertRefused(self.redeem(receipt), receipts.REPLAY)
        outcomes = [(e["outcome"], e["code"]) for e in self.events()]
        self.assertEqual(outcomes, [("granted", "ok"), ("refused", "replay")])
        granted = self.events()[0]
        self.assertEqual((granted["human"], granted["agent"], granted["host"],
                          granted["checked_by"]),
                         (ALICE, AGENT, HOST, "runner:ameesh-agents-2"))

    def test_humain_de_la_liste(self):
        grant = self.redeem(self.grant(self.bob, self.bob_station, approver=BOB),
                            self.bob_station)
        self.assertTrue(grant.ok, grant.reason)

    def test_deputy_hors_liste_refuse(self):
        grant = self.redeem(self.grant(self.dan, self.dan_station, approver=DAN),
                            self.dan_station)
        self.assertRefused(grant, attach_grant.NOT_AUTHORIZED)

    def test_autre_agent_refuse(self):
        # grant signé pour avatar-alice, présenté sur l'hôte de `other`
        self.assertRefused(self.redeem(self.grant(), agent="other"),
                           attach_grant.NOT_AUTHORIZED)
        # grant signé pour `other` (alice n'y est pas autorisée)
        self.assertRefused(self.redeem(self.grant(agent="other"), agent="other"),
                           attach_grant.NOT_AUTHORIZED)

    def test_autre_hote_refuse(self):
        self.assertRefused(self.redeem(self.grant(), host="manaty-mesh-1"),
                           receipts.ATTACH_MISMATCH)

    def test_appareil_d_un_autre_humain_refuse(self):
        # le reçu d'alice désigne la clé d'appareil de bob
        receipt = self.grant(station=self.bob_station)
        self.assertRefused(self.redeem(receipt, self.bob_station), attach_grant.DEVICE)

    def test_autre_appareil_du_meme_humain_refuse(self):
        second = SoftDevice()
        members = [{"title": "alice", "canon_ref": "members/alice.md@%s" % COMMIT,
                    "authenticators": [self.alice.entry(), self.alice_station.entry(),
                                       second.entry()]}]
        apply_authenticators(self.db, members)
        receipt = self.grant()
        self.assertRefused(self.redeem(receipt, second), receipts.ATTACH_MISMATCH)

    def test_preuve_d_appareil_absente_ou_fausse(self):
        receipt = self.grant()
        self.assertRefused(self.redeem(receipt, signature=b"\x30\x00"),
                           attach_grant.DEVICE_PROOF)
        # la clé de l'appareil signe le challenge NU (forme d'un reçu
        # device-es256) : refusé, le domaine diffère
        bare = self.alice_station.key.sign(receipts.challenge(receipt["request"]))
        self.assertRefused(self.redeem(receipt, signature=bare), attach_grant.DEVICE_PROOF)
        # le grant n'a pas été consommé : la bonne preuve passe encore
        self.assertTrue(self.redeem(receipt).ok)

    def test_preuve_d_appareil_liee_au_transcript(self):
        receipt = self.grant()
        chal = receipts.challenge(receipt["request"])
        # preuve signée pour un AUTRE canal E2E : le relais qui terminerait
        # deux canaux distincts ne peut pas la transposer
        other = hashlib.sha256(b"un-autre-canal").digest()
        signature = self.alice_station.key.sign(
            attach_grant.device_message(chal, other))
        self.assertRefused(self.redeem(receipt, signature=signature),
                           attach_grant.DEVICE_PROOF)
        # le grant n'a pas été consommé : la preuve du bon transcript passe
        self.assertTrue(self.redeem(receipt).ok)

    def test_verification_de_lecture_du_relais(self):
        from ameesh import attach_relay_cli

        class Borrowed:
            """La base du banc, non fermée par le vérificateur du relais."""

            def __init__(self, db):
                self._db = db

            def __getattr__(self, name):
                return getattr(self._db, name)

            def close(self):
                pass

        expected = {"agent": AGENT, "host": HOST, "tenant": TENANT}
        # le vrai résolveur de canon (load_configured) est exercé ; seule la
        # connexion est empruntée pour ne pas fermer la base du banc
        with mock.patch.object(canon_mod, "load_configured",
                               return_value=[self.canon]):
            checker = attach_relay_cli._db_grant_checker(
                POLICY, self.cfg, db_opener=lambda cfg: Borrowed(self.db))
            receipt = self.grant()
            ok, code, reason = checker(receipt, expected)
            self.assertTrue(ok, reason)
            # le relais ne consomme pas le nonce : une seconde lecture passe
            self.assertTrue(checker(receipt, expected)[0])
            self.assertEqual(self.events(), [])
            # la façade est forcée à WebAuthn : la seule clé d'appareil est refusée
            device_receipt = self.alice_station.receipt(self.grant()["request"])
            ok, code, _reason = checker(device_receipt, expected)
            self.assertFalse(ok)
            self.assertEqual(code, receipts.FACADE)
            # la liaison agent/hôte/tenant est vérifiée par le relais
            ok, code, _reason = checker(receipt, dict(expected, host="autre-hote"))
            self.assertFalse(ok)
            self.assertEqual(code, receipts.ATTACH_MISMATCH)

    def test_lecture_du_relais_sans_canon(self):
        receipt = self.grant()
        ok, code, _reason = attach_grant.check_receipt(
            self.db, receipt, canon=None, policy=POLICY,
            expected={"agent": AGENT, "host": HOST, "tenant": TENANT})
        self.assertEqual((ok, code), (False, attach_grant.UNKNOWN_AGENT))

    def test_cle_d_appareil_ne_signe_pas_son_grant(self):
        request = self.grant()["request"]
        receipt = self.alice_station.receipt(request)
        self.assertRefused(self.redeem(receipt), receipts.FACADE)

    def test_expire_trop_long_refus_signe(self):
        now = int(time.time())
        expired = self.grant(iat=now - 400, exp=now - 100)
        self.assertRefused(self.redeem(expired), receipts.EXPIRED)
        self.assertRefused(self.redeem(self.grant(ttl=attach_grant.GRANT_TTL + 60)),
                           attach_grant.TTL)
        self.assertRefused(self.redeem(self.grant(decision="deny")), receipts.DECISION)

    def test_recu_illisible_ou_d_une_autre_nature(self):
        self.assertRefused(self.redeem({"v": "x"}, signature=b""), receipts.FORMAT)
        request = {"v": 1, "approver": ALICE, "decision": "approve",
                   "action_id": "act_0123456789ABCDEFGHJKMNPQRS",
                   "digest": "sha256:" + "1" * 64, "summary_digest": "sha256:" + "2" * 64,
                   "requested_by": "agent:x", "nonce": "AAAAAAAAAAAAAAAAAAAAAA",
                   "iat": int(time.time()), "exp": int(time.time()) + 60}
        self.assertRefused(self.redeem(self.alice.receipt(request), signature=b""),
                           receipts.KIND)

    def test_signature_falsifiee(self):
        receipt = self.grant()
        forged = make_receipt(dict(receipt["request"], device_key=receipt["request"][
            "device_key"]), "webauthn", self.alice.credential_id,
            self.bob.proof(receipts.challenge(receipt["request"])))
        self.assertRefused(self.redeem(forged), receipts.SIGNATURE)

    def test_appareil_revoque_pendant_la_verification(self):
        """B cx2 : la clé d'appareil révoquée entre sa lecture et la
        consommation du reçu n'ouvre rien (relue sous verrou à l'écriture)."""
        receipt = self.grant()
        real = receipts.verify_receipt

        def revoke_then_verify(*args, **kwargs):
            members = [{"title": "alice", "canon_ref": "members/alice.md@%s" % COMMIT,
                        "authenticators": [self.alice.entry()]}]
            other = self.connect()
            try:
                apply_authenticators(other, members)
            finally:
                other.close()
            return real(*args, **kwargs)

        with mock.patch.object(receipts, "verify_receipt", side_effect=revoke_then_verify):
            grant = self.redeem(receipt)
        self.assertRefused(grant, attach_grant.DEVICE)
        self.assertIn("revoked during verification", grant.reason)
        self.assertEqual([e["outcome"] for e in self.events()], ["refused"])

    def test_echeance_franchie_pendant_le_verrou(self):
        """B cx1 : la clé d'appareil tenue par une autre transaction jusqu'après
        `exp` ; le grant est recontrôlé à l'heure de la base après le verrou :
        aucun `granted`, refus `expired` journalisé."""
        receipt = self.grant(ttl=3)
        device_id = self.db.query(
            "SELECT id FROM authenticators WHERE credential_id = %s",
            (self.alice_station.credential_id,))[0]["id"]
        held, release = threading.Event(), threading.Event()

        def hold():
            other = self.connect()
            try:
                with other.transaction() as tx:
                    tx.query("SELECT id FROM authenticators WHERE id = %s FOR UPDATE",
                             (device_id,))
                    held.set()
                    release.wait(30)
            finally:
                other.close()

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(held.wait(30))
        timer = threading.Timer(max(0.0, receipt["request"]["exp"] - time.time() + 1.5),
                                release.set)
        timer.start()
        try:
            grant = self.redeem(receipt)
        finally:
            release.set()
            timer.cancel()
            holder.join(30)
        self.assertRefused(grant, receipts.EXPIRED)
        # refus de la base (après le verrou), pas du contrôle Python d'avant
        self.assertIn("heure de la base", grant.reason)
        self.assertEqual([e["outcome"] for e in self.events()], ["refused"])

    def test_un_seul_grant_par_attach_id(self):
        receipt = self.grant()
        self.assertTrue(self.redeem(receipt).ok)
        # même attach_id, autre nonce : l'index partiel garde l'usage unique
        again = self.grant(attach_id=receipt["request"]["attach_id"])
        self.assertRefused(self.redeem(again), receipts.REPLAY)


if __name__ == "__main__":
    unittest.main()
