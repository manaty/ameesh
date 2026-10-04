# SPDX-License-Identifier: AGPL-3.0-only
"""Reçus ameesh-receipt/1 : cas valides et chaque falsification (spec §8, C7).

Tout passe par des authentificateurs LOGICIELS (tests/webauthn_soft.py) et le
registre de confiance est rempli par `support.apply_authenticators` (verrou du
registre, écriture interne de L6), comme le ferait
`ameesh canon sync`. Aucune vraie clé, aucun service réseau.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

from ameesh import cose, jcs, p256, receipts, signing
from ameesh import db as db_mod

from .support import TEST_DSN, PgTestCase, apply_authenticators
from .webauthn_soft import (
    ORIGIN, RP_ID, SoftDevice, SoftEd25519, SoftES256Key, SoftWebAuthn, b64u, cbor,
    make_receipt, make_request, new_action_id, sha,
)

ALICE = "human:alice"
BOB = "human:bob"
CAROL = "human:carol"
COMMIT = "a" * 40
NEXT_COMMIT = "b" * 40
POLICY = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,))


def member(title: str, *entries: dict, commit: str = COMMIT) -> dict:
    return {"title": title, "canon_ref": "members/%s.md@%s" % (title, commit),
            "authenticators": list(entries)}


class ReceiptsBase(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute(
            "TRUNCATE authenticators, standing_approvals, standing_reservations, "
            "mesh_consumed_nonces RESTART IDENTITY CASCADE")
        self.alice = SoftWebAuthn("ES256")
        self.alice_ed = SoftWebAuthn("EdDSA")
        self.alice_device = SoftDevice()
        self.alice_tool = SoftEd25519()
        self.bob = SoftWebAuthn("ES256")
        self.carol_key = SoftWebAuthn("ES256")                       # clé matérielle : BE=0
        self.carol_synced = SoftWebAuthn("ES256", backup_eligible=True, backup_state=True)
        self.members = [
            member("alice", self.alice.entry(), self.alice_ed.entry(),
                   self.alice_device.entry(), self.alice_tool.entry()),
            member("bob", self.bob.entry()),
            member("carol", self.carol_key.entry(level="eleve"), self.carol_synced.entry()),
        ]
        result = apply_authenticators(self.db, self.members)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["added"]), 7)

    def verify(self, receipt, policy=POLICY, **kwargs):
        return receipts.verify_receipt(self.db, receipt, policy, **kwargs)

    def consume(self, receipt, by="porte", db=None, policy=POLICY):
        """Comme la porte : consommation liée à l'action qu'elle connaît."""
        request = receipt["request"]
        return receipts.verify_receipt(
            db or self.db, receipt, policy, consume_by=by,
            expected_digest=request["digest"], expected_action_id=request["action_id"])

    def assertRefused(self, verdict, code: str):
        self.assertFalse(verdict.ok, "accepté à tort : %s" % verdict.reason)
        self.assertEqual(verdict.code, code, verdict.reason)


# --------------------------------------------------------------------------
# empreintes et forme
# --------------------------------------------------------------------------

class DigestsTest(unittest.TestCase):
    def test_challenge_connu(self):
        """Le challenge est exactement SHA-256(domaine ‖ JCS) : interopérable."""
        request = {
            "v": 1, "approver": ALICE, "action_id": "act_0123456789ABCDEFGHJKMNPQRS",
            "digest": "sha256:" + "1" * 64, "decision": "approve",
            "summary_digest": "sha256:" + "2" * 64, "requested_by": "agent:deepseek7",
            "nonce": "AAAAAAAAAAAAAAAAAAAAAA", "iat": 1790000000, "exp": 1790000600,
        }
        canonical = (
            '{"action_id":"act_0123456789ABCDEFGHJKMNPQRS","approver":"human:alice",'
            '"decision":"approve","digest":"sha256:' + "1" * 64 + '","exp":1790000600,'
            '"iat":1790000000,"nonce":"AAAAAAAAAAAAAAAAAAAAAA","requested_by":"agent:deepseek7",'
            '"summary_digest":"sha256:' + "2" * 64 + '","v":1}')
        self.assertEqual(jcs.dumps(request), canonical)
        self.assertEqual(receipts.challenge(request),
                         hashlib.sha256(b"ameesh-approval/1\x00" + canonical.encode()).digest())
        self.assertEqual(receipts.check_request(request), "action")

    def test_empreinte_d_action(self):
        fields = {"action_id": new_action_id(), "project": "ameesh", "connector": "git-merge",
                  "operation": "merge", "target": "o/r#42", "args": {"pr": 42, "method": "merge"},
                  "amount": None, "currency": None, "policy_version": 3}
        digest = receipts.action_digest(fields)
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        canonical = jcs.canonicalize(fields)
        self.assertEqual(digest, "sha256:" + hashlib.sha256(
            b"ameesh-action/1\x00" + canonical).hexdigest())
        # l'ordre des clés est sans effet ; chaque champ compte
        self.assertEqual(receipts.action_digest(dict(reversed(list(fields.items())))), digest)
        for key, value in (("target", "o/r#43"), ("args", {"pr": 42}), ("amount", 1),
                           ("policy_version", 4), ("project", "autre")):
            with self.subTest(key=key):
                self.assertNotEqual(receipts.action_digest(fields, **{key: value}), digest)
        with self.assertRaises(receipts.ReceiptError):
            receipts.action_digest({k: v for k, v in fields.items() if k != "target"})
        with self.assertRaises(receipts.ReceiptError):
            receipts.action_digest(fields, extra=1)
        with self.assertRaises(receipts.ReceiptError):
            receipts.action_digest(fields, amount=12.5)
        with self.assertRaises(receipts.ReceiptError):
            receipts.action_digest(fields, amount=-1)

    def test_forme_stricte(self):
        good = make_request(ALICE)
        self.assertEqual(receipts.check_request(good), "action")
        cases = {
            "champ inconnu": dict(good, extra=1),
            "champ manquant": {k: v for k, v in good.items() if k != "summary_digest"},
            "v texte": dict(good, v="1"),
            "v booléen": dict(good, v=True),
            "agent approbateur": dict(good, approver="agent:deepseek7"),
            "approbateur nu": dict(good, approver="alice"),
            "décision": dict(good, decision="approved"),
            "nonce court": dict(good, nonce=b64u(os.urandom(8))),
            "nonce rembourré": dict(good, nonce=b64u(os.urandom(16)) + "=="),
            "nonce base64 standard": dict(good, nonce="+/" + b64u(os.urandom(16))[2:]),
            "iat flottant": dict(good, iat=float(good["iat"])),
            "exp avant iat": dict(good, exp=good["iat"]),
            "digest": dict(good, digest="sha256:XYZ"),
            "action_id": dict(good, action_id="act_court"),
            "action et standing": dict(good, standing={}),
            # `$` des regex Python accepte un saut de ligne final : refusé ici
            "approbateur + saut de ligne": dict(good, approver=ALICE + "\n"),
            "digest + saut de ligne": dict(good, digest=good["digest"] + "\n"),
            "action_id + saut de ligne": dict(good, action_id=good["action_id"] + "\n"),
            "nonce + saut de ligne": dict(good, nonce=good["nonce"] + "\n"),
            "décision liste": dict(good, decision=["approve"]),
        }
        for label, request in cases.items():
            with self.subTest(label):
                with self.assertRaises(receipts.ReceiptError):
                    receipts.check_request(request)


# --------------------------------------------------------------------------
# cas valides
# --------------------------------------------------------------------------

class ReceiptsValidTest(ReceiptsBase):
    def test_webauthn_es256_valide_puis_consomme(self):
        request = make_request(ALICE)
        receipt = self.alice.receipt(request)
        verdict = self.verify(receipt, expected_digest=request["digest"],
                              expected_action_id=request["action_id"])
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertTrue(verdict.authority)
        self.assertEqual((verdict.approver, verdict.facade, verdict.kind),
                         (ALICE, "webauthn", "action"))
        self.assertEqual(verdict.flags["up"], True)
        self.assertEqual(verdict.flags["uv"], True)
        self.assertEqual(verdict.flags["be"], False)
        self.assertFalse(verdict.consumed)
        # consommer sans dire quelle action : refusé, rien n'est consommé
        self.assertRefused(self.verify(receipt, consume_by="porte"), receipts.UNBOUND)
        self.assertRefused(self.verify(receipt, consume_by="porte",
                                       expected_digest=request["digest"]), receipts.UNBOUND)
        consumed = self.consume(receipt)
        self.assertTrue(consumed.ok, consumed.reason)
        self.assertTrue(consumed.consumed)
        row = self.db.query("SELECT consumed_by, challenge FROM mesh_consumed_nonces "
                            "WHERE approver = %s AND nonce = %s", (ALICE, request["nonce"]))[0]
        self.assertEqual(row["consumed_by"], "porte")
        self.assertEqual(row["challenge"], receipts.challenge(request).hex())

    def test_texte_json_du_reçu(self):
        receipt = self.alice.receipt(make_request(ALICE))
        self.assertTrue(self.verify(json.dumps(receipt)).ok)
        self.assertTrue(self.verify(json.dumps(receipt).encode("utf-8")).ok)

    def test_webauthn_eddsa_valide(self):
        verdict = self.verify(self.alice_ed.receipt(make_request(ALICE)))
        self.assertTrue(verdict.ok, verdict.reason)

    def test_device_es256_valide(self):
        verdict = self.verify(self.alice_device.receipt(make_request(ALICE)))
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertEqual(verdict.facade, "device-es256")

    def test_device_es256_cle_cose(self):
        device = SoftDevice(cose=True)
        self.members[0]["authenticators"].append(device.entry())
        self.assertEqual(apply_authenticators(self.db, self.members)["errors"], [])
        self.assertTrue(self.verify(device.receipt(make_request(ALICE))).ok)

    def test_decision_deny(self):
        receipt = self.alice.receipt(make_request(ALICE, decision="deny"))
        self.assertRefused(self.verify(receipt), receipts.DECISION)
        verdict = self.verify(receipt, expect_decision="deny")
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertFalse(verdict.authority)

    def test_iat_dans_la_tolerance(self):
        request = make_request(ALICE, iat=int(time.time()) + 60)
        self.assertTrue(self.verify(self.alice.receipt(request)).ok)

    def test_les_deux_backends_es256(self):
        receipt = self.alice.receipt(make_request(ALICE))
        tampered = json.loads(json.dumps(receipt))
        tampered["request"]["summary_digest"] = sha("autre résumé")
        for name in p256.available_backends():
            with self.subTest(backend=name):
                with mock.patch.dict(os.environ, {"AMEESH_ECDSA_BACKEND": name}):
                    self.assertEqual(p256.backend().name, name)
                    self.assertTrue(self.verify(receipt).ok)
                    self.assertFalse(self.verify(tampered).ok)


# --------------------------------------------------------------------------
# falsifications
# --------------------------------------------------------------------------

class ReceiptsForgeryTest(ReceiptsBase):
    def test_challenge_d_une_autre_demande(self):
        signed = make_request(ALICE)
        other = make_request(ALICE)
        receipt = make_receipt(other, "webauthn", self.alice.credential_id,
                               self.alice.proof(receipts.challenge(signed)))
        self.assertRefused(self.verify(receipt), receipts.CHALLENGE)

    def test_demande_modifiee_apres_signature(self):
        request = make_request(ALICE)
        for key, value in (("digest", sha("autre action")), ("exp", request["exp"] + 3600),
                           ("approver", BOB), ("decision", "deny")):
            with self.subTest(key=key):
                receipt = self.alice.receipt(request)
                receipt["request"] = dict(request, **{key: value})
                verdict = self.verify(receipt, expect_decision=None)
                self.assertFalse(verdict.ok)
                self.assertIn(verdict.code, (receipts.CHALLENGE, receipts.FOREIGN_AUTHENTICATOR))

    def test_empreinte_ou_action_attendue_differente(self):
        request = make_request(ALICE)
        receipt = self.alice.receipt(request)
        self.assertRefused(self.verify(receipt, expected_digest=sha("autre")),
                           receipts.DIGEST_MISMATCH)
        self.assertRefused(self.verify(receipt, expected_action_id=new_action_id()),
                           receipts.ACTION_MISMATCH)

    def test_autre_origine(self):
        receipt = self.alice.receipt(make_request(ALICE), origin="https://evil.example.test")
        self.assertRefused(self.verify(receipt), receipts.ORIGIN)
        # même origine que le RP mais en http
        receipt = self.alice.receipt(make_request(ALICE), origin="http://approve.example.test")
        self.assertRefused(self.verify(receipt), receipts.ORIGIN)

    def test_rp_id_different(self):
        receipt = self.alice.receipt(make_request(ALICE), rp_id="evil.example.test")
        self.assertRefused(self.verify(receipt), receipts.RP_ID)
        good = self.alice.receipt(make_request(ALICE))
        other_policy = receipts.Policy(rp_id="example.test", origins=(ORIGIN,))
        self.assertRefused(self.verify(good, other_policy), receipts.RP_ID)

    def test_up_0(self):
        receipt = self.alice.receipt(make_request(ALICE), up=False)
        self.assertRefused(self.verify(receipt), receipts.USER_PRESENCE)

    def test_uv_0(self):
        receipt = self.alice.receipt(make_request(ALICE), uv=False)
        self.assertRefused(self.verify(receipt), receipts.USER_VERIFICATION)

    def test_be_1_au_niveau_eleve(self):
        eleve = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,), level="eleve")
        # clé matérielle enrôlée « élevé », BE=0 : acceptée au niveau élevé
        self.assertTrue(self.verify(self.carol_key.receipt(make_request(CAROL)), eleve).ok)
        # la même clé qui annoncerait BE=1 : refusée
        receipt = self.carol_key.receipt(make_request(CAROL), be=True)
        self.assertRefused(self.verify(receipt, eleve), receipts.BACKUP)
        # enrôlée « élevé » mais BE=1 : refusée même sous la politique standard
        self.assertRefused(self.verify(receipt), receipts.BACKUP)
        # passkey synchronisée de niveau standard : refusée au niveau élevé…
        synced = self.carol_synced.receipt(make_request(CAROL))
        self.assertRefused(self.verify(synced, eleve), receipts.LEVEL)
        # … acceptée au niveau standard (BE et BS exposés)
        verdict = self.verify(synced)
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertEqual((verdict.flags["be"], verdict.flags["bs"]), (True, True))

    def test_bs_sans_be(self):
        receipt = self.alice.receipt(make_request(ALICE), be=False, bs=True)
        self.assertRefused(self.verify(receipt), receipts.AUTH_DATA)

    def test_authenticator_data_mal_forme(self):
        cases = {
            "AT dans une assertion": dict(extra_flags=receipts.FLAG_AT),
            "octets en trop": dict(auth_suffix=b"\x00"),
            "ED sans extensions": dict(extra_flags=receipts.FLAG_ED),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                receipt = self.alice.receipt(make_request(ALICE), **kwargs)
                self.assertRefused(self.verify(receipt), receipts.AUTH_DATA)
        # ED avec une table d'extensions bien formée : accepté
        receipt = self.alice.receipt(make_request(ALICE), extra_flags=receipts.FLAG_ED,
                                     auth_suffix=bytes([0xA1, 0x61, 0x78, 0xF5]))
        self.assertTrue(self.verify(receipt).ok)

    def test_client_data_refuse(self):
        cases = {
            receipts.CLIENT_TYPE: dict(type="webauthn.create"),
            receipts.CROSS_ORIGIN: dict(cross_origin=True),
        }
        for code, kwargs in cases.items():
            with self.subTest(code):
                receipt = self.alice.receipt(make_request(ALICE), **kwargs)
                self.assertRefused(self.verify(receipt), code)
        receipt = self.alice.receipt(make_request(ALICE), cross_origin=False)
        self.assertTrue(self.verify(receipt).ok)
        receipt = self.alice.receipt(make_request(ALICE), top_origin="https://evil.example.test")
        self.assertRefused(self.verify(receipt), receipts.CROSS_ORIGIN)
        receipt = self.alice.receipt(make_request(ALICE), cross_origin="false")
        self.assertRefused(self.verify(receipt), receipts.CROSS_ORIGIN)

    def test_client_data_cle_en_double(self):
        request = make_request(ALICE)
        receipt = self.alice.receipt(request)
        client = receipts.b64u_decode(receipt["proof"]["clientDataJSON"]).decode()
        doubled = client[:-1] + ',"type":"webauthn.get"}'
        receipt["proof"]["clientDataJSON"] = b64u(doubled.encode())
        self.assertRefused(self.verify(receipt), receipts.CLIENT_DATA)

    def test_signature_alteree(self):
        request = make_request(ALICE)
        receipt = self.alice.receipt(request)
        der = bytearray(receipts.b64u_decode(receipt["proof"]["signature"]))
        der[-1] ^= 0x01
        receipt["proof"]["signature"] = b64u(bytes(der))
        self.assertRefused(self.verify(receipt), receipts.SIGNATURE)

        # clientDataJSON modifié après signature (champ ajouté) : seule la
        # signature peut le voir
        receipt = self.alice.receipt(make_request(ALICE))
        client = json.loads(receipts.b64u_decode(receipt["proof"]["clientDataJSON"]))
        client["extra"] = "x"
        receipt["proof"]["clientDataJSON"] = b64u(json.dumps(client).encode())
        self.assertRefused(self.verify(receipt), receipts.SIGNATURE)

        # compteur modifié dans authenticatorData
        receipt = self.alice.receipt(make_request(ALICE))
        auth = bytearray(receipts.b64u_decode(receipt["proof"]["authenticatorData"]))
        auth[36] ^= 0x01
        receipt["proof"]["authenticatorData"] = b64u(bytes(auth))
        self.assertRefused(self.verify(receipt), receipts.SIGNATURE)

        for soft in (self.alice_ed, ):
            receipt = soft.receipt(make_request(ALICE))
            sig = bytearray(receipts.b64u_decode(receipt["proof"]["signature"]))
            sig[0] ^= 0x80
            receipt["proof"]["signature"] = b64u(bytes(sig))
            self.assertRefused(self.verify(receipt), receipts.SIGNATURE)

        receipt = self.alice_device.receipt(make_request(ALICE))
        receipt["request"] = dict(receipt["request"], nonce=b64u(os.urandom(16)))
        self.assertRefused(self.verify(receipt), receipts.SIGNATURE)

        tools = receipts.Policy(allow_facades={"ed25519"})
        receipt = self.alice_tool.receipt(make_request(ALICE))
        receipt["request"] = dict(receipt["request"], exp=receipt["request"]["exp"] - 1)
        self.assertRefused(self.verify(receipt, tools), receipts.SIGNATURE)

    def test_entrees_hostiles_donnent_un_verdict(self):
        """Jamais d'exception : toute entrée mal formée donne un refus FORMAT."""
        good = self.alice.receipt(make_request(ALICE))
        hostile = [
            [good], "pas du json", b"\xff\xfe", "null", '{"v": "ameesh-receipt/1"}',
            dict(good, credential_id=good["credential_id"] + "\n"),
            dict(good, credential_id=["x"]),
            dict(good, facade=["webauthn"]),
            dict(good, proof=dict(good["proof"], signature=good["proof"]["signature"] + "\n")),
            dict(good, proof=dict(good["proof"], signature="A")),
            dict(good, proof=dict(good["proof"], signature=None)),
            dict(good, proof=dict(good["proof"], extra="x")),
            dict(good, proof="x"),
            dict(good, request=dict(good["request"], standing={"class": {"x": 1}})),
            dict(good, extra=1),
            dict(good, v="ameesh-receipt/2"),
            dict(good, request=dict(good["request"], iat=2 ** 60)),
        ]
        for index, receipt in enumerate(hostile):
            with self.subTest(index=index):
                self.assertRefused(self.verify(receipt), receipts.FORMAT)
        duplicated = json.dumps(good)[:-1] + ', "facade": "webauthn"}'
        self.assertRefused(self.verify(duplicated), receipts.FORMAT)
        # une preuve dont le clientDataJSON n'est pas du JSON
        broken = dict(good, proof=dict(good["proof"], clientDataJSON=b64u(b"\xff{")))
        self.assertRefused(self.verify(broken), receipts.CLIENT_DATA)
        broken = dict(good, proof=dict(good["proof"], clientDataJSON=b64u(b"[1]")))
        self.assertRefused(self.verify(broken), receipts.CLIENT_DATA)
        broken = dict(good, proof=dict(good["proof"], authenticatorData=b64u(b"\x00" * 10)))
        self.assertRefused(self.verify(broken), receipts.AUTH_DATA)

    def test_signature_non_canonique_refusee_a_la_forme(self):
        receipt = self.alice.receipt(make_request(ALICE))
        receipt["proof"]["signature"] += "="
        self.assertRefused(self.verify(receipt), receipts.FORMAT)

    def test_cle_d_un_autre_approbateur(self):
        # le credential de bob présenté au nom d'alice
        receipt = self.bob.receipt(make_request(ALICE))
        self.assertRefused(self.verify(receipt), receipts.FOREIGN_AUTHENTICATOR)
        # le credential_id d'alice, mais signé par la clé de bob
        request = make_request(ALICE)
        receipt = make_receipt(request, "webauthn", self.alice.credential_id,
                               self.alice.proof(receipts.challenge(request), signer=self.bob.key))
        self.assertRefused(self.verify(receipt), receipts.SIGNATURE)
        # une clé d'appareil inconnue sous un identifiant d'alice
        request = make_request(ALICE)
        receipt = self.alice_device.receipt(request, signer=SoftES256Key())
        self.assertRefused(self.verify(receipt), receipts.SIGNATURE)

    def test_approbateur_ou_credential_inconnu(self):
        stranger = SoftWebAuthn()
        self.assertRefused(self.verify(stranger.receipt(make_request("human:mallory"))),
                           receipts.UNKNOWN_APPROVER)
        self.assertRefused(self.verify(stranger.receipt(make_request(ALICE))),
                           receipts.UNKNOWN_AUTHENTICATOR)
        # bon credential, mauvaise façade annoncée
        receipt = self.alice.receipt(make_request(ALICE))
        receipt["facade"] = "device-es256"
        self.assertRefused(self.verify(receipt), receipts.FORMAT)  # forme de preuve webauthn
        receipt = self.alice_device.receipt(make_request(ALICE))
        receipt["facade"] = "ed25519"
        tools = receipts.Policy(allow_facades={"ed25519"})
        self.assertRefused(self.verify(receipt, tools), receipts.UNKNOWN_AUTHENTICATOR)

    def test_agent_ne_peut_pas_approuver(self):
        receipt = self.alice.receipt(make_request("agent:deepseek7"))
        self.assertRefused(self.verify(receipt), receipts.FORMAT)

    def test_authentificateur_revoque(self):
        receipt = self.alice.receipt(make_request(ALICE))
        self.assertTrue(self.verify(receipt).ok)
        self.members[0]["authenticators"] = [
            e for e in self.members[0]["authenticators"]
            if e["credential_id"] != self.alice.credential_id]
        result = apply_authenticators(self.db, self.members)
        self.assertEqual(result["revoked"], ["%s/webauthn/%s" % (ALICE, self.alice.credential_id)])
        self.assertRefused(self.verify(receipt), receipts.REVOKED)
        self.assertRefused(self.verify(self.alice.receipt(make_request(ALICE))), receipts.REVOKED)

    def test_consommation_pendant_une_revocation(self):
        """B3 : la consommation d'un reçu attend une révocation en cours, puis refuse."""
        receipt = self.alice.receipt(make_request(ALICE))
        with revoking(self, self.alice.credential_id) as revocation:
            worker = Worker(self, lambda db: self.consume(receipt, db=db))
            waited = revocation.blocked(worker)
            revocation.commit()
        self.assertRefused(worker.result(), receipts.REVOKED)
        self.assertTrue(waited, "la consommation n'a pas attendu la révocation en cours")
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM mesh_consumed_nonces")[0]["n"], 0)

    def test_expire(self):
        request = make_request(ALICE, iat=int(time.time()) - 700, ttl=600)
        self.assertRefused(self.verify(self.alice.receipt(request)), receipts.EXPIRED)
        request = make_request(ALICE)
        receipt = self.alice.receipt(request)
        self.assertRefused(self.verify(receipt, now=request["exp"]), receipts.EXPIRED)
        self.assertTrue(self.verify(receipt, now=request["exp"] - 1).ok)

    def test_iat_futur(self):
        request = make_request(ALICE, iat=int(time.time()) + 600)
        self.assertRefused(self.verify(self.alice.receipt(request)), receipts.NOT_YET)

    def test_duree_de_vie_bornee(self):
        request = make_request(ALICE, ttl=receipts.DEFAULT_MAX_TTL + 60)
        self.assertRefused(self.verify(self.alice.receipt(request)), receipts.TTL)

    def test_nonce_rejoue(self):
        receipt = self.alice.receipt(make_request(ALICE))
        self.assertTrue(self.consume(receipt).ok)
        self.assertRefused(self.verify(receipt), receipts.REPLAY)
        self.assertRefused(self.consume(receipt), receipts.REPLAY)
        # nouvelle preuve WebAuthn pour la même demande : même nonce, refusée
        again = self.alice.receipt(receipt["request"])
        self.assertRefused(self.consume(again, by="autre"), receipts.REPLAY)
        # même nonce sous une autre façade (clé d'appareil d'alice) : refusé aussi
        device = self.alice_device.receipt(receipt["request"])
        self.assertRefused(self.consume(device, by="autre"), receipts.REPLAY)

    def test_nonce_rejoue_en_concurrence(self):
        receipt = self.alice.receipt(make_request(ALICE))
        workers = 8
        barrier = threading.Barrier(workers)
        verdicts: list = []
        lock = threading.Lock()

        def attempt(index: int) -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=30)
                verdict = self.consume(receipt, by="porte-%d" % index, db=db)
            finally:
                db.close()
            with lock:
                verdicts.append(verdict)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(len(verdicts), workers)
        self.assertEqual(sum(1 for v in verdicts if v.ok), 1, [v.reason for v in verdicts])
        self.assertTrue(all(v.code == receipts.REPLAY for v in verdicts if not v.ok))

    def test_facade_ed25519_refusee_pour_une_autorite_humaine(self):
        receipt = self.alice_tool.receipt(make_request(ALICE))
        self.assertRefused(self.verify(receipt), receipts.FACADE)
        tools = receipts.Policy(allow_facades={"ed25519"})
        self.assertTrue(self.verify(receipt, tools).ok)
        # et une politique qui n'admet que WebAuthn refuse la clé d'appareil
        webauthn_only = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,),
                                        allow_facades={"webauthn"})
        self.assertRefused(self.verify(self.alice_device.receipt(make_request(ALICE)),
                                       webauthn_only), receipts.FACADE)

    def test_politique_webauthn_incomplete(self):
        receipt = self.alice.receipt(make_request(ALICE))
        self.assertRefused(self.verify(receipt, receipts.Policy()), receipts.POLICY)

    def test_canon_attendu(self):
        receipt = self.alice.receipt(make_request(ALICE))
        policy = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,), canon_commit=COMMIT)
        self.assertTrue(self.verify(receipt, policy).ok)
        policy = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,), canon_commit=NEXT_COMMIT)
        self.assertRefused(self.verify(receipt, policy), receipts.CANON)

    def test_cle_injectee_en_base_hors_canon_est_signalee(self):
        """Une ligne ajoutée en base sans passer par le canon ressort."""
        intruder = SoftWebAuthn()
        raw = receipts.b64u_decode(intruder.public_key())
        self.db.query(
            "INSERT INTO authenticators (approver, facade, credential_id, public_key, "
            "key_fingerprint, level, canon_ref) VALUES (%s, 'webauthn', %s, %s, %s, "
            "'standard', %s) RETURNING id",
            (ALICE, intruder.credential_id, intruder.public_key(),
             hashlib.sha256(raw).hexdigest(), "members/alice.md@" + "f" * 40))
        flagged = receipts.mismatched_authenticators(self.db, COMMIT)
        self.assertEqual([row["credential_id"] for row in flagged], [intruder.credential_id])
        policy = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,), canon_commit=COMMIT)
        self.assertRefused(self.verify(intruder.receipt(make_request(ALICE)), policy),
                           receipts.CANON)
        # et la synchronisation suivante la révoque (absente du canon)
        result = apply_authenticators(self.db, self.members)
        self.assertEqual(result["revoked"], ["%s/webauthn/%s" % (ALICE, intruder.credential_id)])


# --------------------------------------------------------------------------
# Ed25519 : clés et signatures de petit ordre (falsification universelle)
# --------------------------------------------------------------------------

#: les 8 points de petit ordre d'Ed25519 (sous-groupe de torsion), encodages
#: canoniques — la liste standard (libsodium, « Taming the many EdDSAs »)
SMALL_ORDER = [bytes.fromhex(h) for h in (
    "0100000000000000000000000000000000000000000000000000000000000000",  # neutre
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",  # ordre 2
    "0000000000000000000000000000000000000000000000000000000000000000",  # ordre 4
    "0000000000000000000000000000000000000000000000000000000000000080",  # ordre 4
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",  # ordre 8
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",  # ordre 8
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",  # ordre 8
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",  # ordre 8
)]
IDENTITY = SMALL_ORDER[0]
#: R = neutre, S = 0 : avec la clé neutre, [S]B - [k]A = neutre pour TOUT message
FORGED = IDENTITY + bytes(32)
#: encodages non canoniques : y non réduit mod p, ou signe posé sur x = 0
NON_CANONICAL = [bytes.fromhex(h) for h in (
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",  # y = p
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",  # y = p + 1 (neutre)
    "0100000000000000000000000000000000000000000000000000000000000080",  # neutre, signe 1
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",  # (0, -1), signe 1
)]


def okp_cose(raw: bytes) -> bytes:
    return cbor({1: 1, 3: -8, -1: 6, -2: raw})


class ForgedSigner:
    """« Signe » n'importe quoi avec la signature constante (neutre, 0)."""

    @staticmethod
    def sign(_message: bytes) -> bytes:
        return FORGED


class SmallOrderTest(unittest.TestCase):
    def test_les_8_points_de_petit_ordre_refuses_a_l_entree(self):
        # la liste est exactement le sous-groupe de torsion : {k·T}, T d'ordre 8
        order8 = signing._point_decompress(SMALL_ORDER[4])
        self.assertEqual({signing._point_compress(signing._point_mul(k, order8))
                          for k in range(8)}, set(SMALL_ORDER))
        for point in SMALL_ORDER:
            with self.subTest(point=point.hex()):
                self.assertIsNotNone(signing._point_decompress(point))   # bien sur la courbe
                self.assertIn("petit ordre", signing.public_key_error(point))
                with self.assertRaises(ValueError):
                    signing.check_public_key(point)
                with self.assertRaises(ValueError):
                    signing.decode_public(signing.encode_public(point))
                with self.assertRaises(cose.CborError):
                    cose.ed25519_key(point)
                with self.assertRaises(cose.CborError):
                    cose.parse_cose_key(okp_cose(point))
                for facade, blob in (("ed25519", point), ("ed25519", okp_cose(point)),
                                     ("webauthn", okp_cose(point))):
                    with self.assertRaises(receipts.ReceiptError):
                        receipts.parse_public_key(facade, blob)

    def test_encodages_non_canoniques_refuses(self):
        for blob in NON_CANONICAL:
            with self.subTest(blob=blob.hex()):
                self.assertIsNotNone(signing.public_key_error(blob))
                with self.assertRaises(cose.CborError):
                    cose.ed25519_key(blob)
                with self.assertRaises(ValueError):
                    signing.check_public_key(blob)

    def test_signature_constante_refusee_par_tous_les_backends(self):
        messages = (b"", b"n'importe quel message", receipts.challenge(make_request(ALICE)))
        for name in signing.available_backends():
            for public in SMALL_ORDER:
                for message in messages:
                    with self.subTest(backend=name, public=public.hex()[:8], message=message[:8]):
                        self.assertFalse(signing.backend(name).verify(public, message, FORGED))
            with self.subTest(backend=name, via="PublicKey"), \
                    mock.patch.dict(os.environ, {"AMEESH_SIGNING_BACKEND": name}):
                # même une PublicKey construite sans passer par ed25519_key
                self.assertEqual(signing.backend().name, name)
                key = cose.PublicKey("EdDSA", raw=IDENTITY)
                self.assertEqual([key.verify(message, FORGED) for message in messages],
                                 [False] * len(messages))
        with self.subTest(via="signing.verify"):
            self.assertEqual([signing.verify(IDENTITY, message, FORGED) for message in messages],
                             [False] * len(messages))

    def test_r_de_petit_ordre_et_s_non_reduit(self):
        seed = os.urandom(32)
        public = signing.public_from_seed(seed)
        scalar, _prefix = signing.expand_seed(seed)
        message = b"message"
        good = signing.sign(seed, message)
        # R = neutre, S = k·a : l'équation sans cofacteur [S]B - [k]A = R tient
        k = int.from_bytes(hashlib.sha512(IDENTITY + public + message).digest(),
                           "little") % signing._L
        small_r = IDENTITY + (k * scalar % signing._L).to_bytes(32, "little")
        # S + L : même point, scalaire non réduit
        s = int.from_bytes(good[32:], "little")
        unreduced = good[:32] + (s + signing._L).to_bytes(32, "little")
        for name in signing.available_backends():
            with self.subTest(backend=name):
                backend = signing.backend(name)
                self.assertTrue(backend.verify(public, message, good))       # inchangé
                self.assertFalse(backend.verify(public, message, small_r))
                self.assertFalse(backend.verify(public, message, unreduced))
        self.assertEqual(signing.signature_error(public, small_r), "R de petit ordre")
        self.assertIn("S non réduit", signing.signature_error(public, unreduced))
        self.assertIsNone(signing.signature_error(public, good))

    def test_cles_honnetes_inchangees(self):
        for _ in range(16):
            public = signing.public_from_seed(os.urandom(32))
            self.assertIsNone(signing.public_key_error(public))
            self.assertEqual(signing.decode_public(signing.encode_public(public)), public)
            self.assertEqual(cose.ed25519_key(public).raw, public)


class SmallOrderEndToEndTest(ReceiptsBase):
    def inject(self, facade: str, credential_id: str, blob: bytes) -> None:
        """Ligne écrite en base sans passer par le canon (qui la refuserait)."""
        self.db.query(
            "INSERT INTO authenticators (approver, facade, credential_id, public_key, "
            "key_fingerprint, level, canon_ref) VALUES (%s, %s, %s, %s, %s, 'standard', %s) "
            "RETURNING id",
            (ALICE, facade, credential_id, b64u(blob), hashlib.sha256(blob).hexdigest(),
             "members/alice.md@" + COMMIT))

    def test_enrolement_canon_refuse(self):
        forged = SoftWebAuthn("EdDSA")
        members = self.members + [member(
            "mallory",
            {"facade": "ed25519", "credential_id": "ed25519:neutre",
             "public_key": b64u(IDENTITY)},
            {"facade": "ed25519", "credential_id": "ed25519:ordre8",
             "public_key": b64u(SMALL_ORDER[4])},
            dict(forged.entry(), public_key=b64u(okp_cose(IDENTITY))),
            {"facade": "ed25519", "credential_id": "ed25519:noncanonique",
             "public_key": b64u(NON_CANONICAL[1])},
        )]
        result = apply_authenticators(self.db, members)
        self.assertEqual(len(result["errors"]), 4, result["errors"])
        self.assertEqual(sum("petit ordre" in e for e in result["errors"]), 3, result["errors"])
        self.assertEqual(result["added"], [])
        self.assertEqual(receipts.list_authenticators(self.db, approver="human:mallory"), [])

    def test_recu_forge_refuse(self):
        tools = receipts.Policy(allow_facades={"ed25519"})
        self.inject("ed25519", "ed25519:neutre", IDENTITY)
        request = make_request(ALICE)
        receipt = make_receipt(request, "ed25519", "ed25519:neutre",
                               {"signature": b64u(FORGED)})
        self.assertRefused(self.verify(receipt, tools), receipts.KEY)
        self.assertRefused(receipts.verify_receipt(
            self.db, receipt, tools, consume_by="porte", expected_digest=request["digest"],
            expected_action_id=request["action_id"]), receipts.KEY)
        # passkey EdDSA dont la clé COSE est le neutre : assertion forgée
        forged = SoftWebAuthn("EdDSA")
        self.inject("webauthn", forged.credential_id, okp_cose(IDENTITY))
        receipt = forged.receipt(make_request(ALICE), signer=ForgedSigner())
        self.assertRefused(self.verify(receipt), receipts.KEY)
        # grant permanent forgé : rien n'est enregistré, aucun nonce consommé
        receipt = forged.receipt(make_request(ALICE, standing=standing()), signer=ForgedSigner())
        verdict, grant = receipts.register_standing(self.db, receipt, POLICY, registered_by="x")
        self.assertIsNone(grant)
        self.assertRefused(verdict, receipts.KEY)
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM mesh_consumed_nonces")[0]["n"], 0)


# --------------------------------------------------------------------------
# approbations permanentes bornées (§8.3)
# --------------------------------------------------------------------------

def standing(**overrides) -> dict:
    value = {"connector": "shell-noop", "operations": ["refund", "label"], "class": "costly",
             "max_amount": 100, "currency": "EUR", "until": int(time.time()) + 3600}
    value.update(overrides)
    return value


def action(**overrides) -> dict:
    value = {"action_id": new_action_id(), "connector": "shell-noop", "operation": "refund",
             "class": "costly", "amount": 30, "currency": "EUR"}
    value.update(overrides)
    return value


class HeldTransaction:
    """Transaction tenue OUVERTE sur une connexion à part, pour les courses réelles.

    Un `psql` lit ses instructions sur stdin (indépendant du pilote testé : la
    connexion qui attend, elle, passe par `db.py`). La dernière instruction
    renomme la session `<app>-pret` : quand pg_stat_activity la montre « idle
    in transaction » sous ce nom, tout ce qui précède est exécuté et les
    verrous sont pris.
    """

    def __init__(self, test: PgTestCase, statements: str):
        self.test = test
        self.app = "ameesh-test-%s" % os.urandom(4).hex()
        password, dsn = db_mod.split_password(TEST_DSN)
        env = dict(os.environ, PGAPPNAME=self.app)
        if password:
            env["PGPASSWORD"] = password
        self.proc = subprocess.Popen(
            [shutil.which("psql") or "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", dsn],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, env=env)
        self.proc.stdin.write("SET search_path TO %s;\nBEGIN;\n%s\nSET application_name = '%s-pret';\n"
                              % (db_mod.quote_ident(test.schema), statements, self.app))
        self.proc.stdin.flush()
        self.pid = test.wait_for(self._ready, timeout=30)

    def _ready(self):
        if self.proc.poll() is not None:
            self.test.fail("psql de la transaction tenue terminé : %s" % self.proc.stderr.read())
        rows = self.test.db.query(
            "SELECT pid FROM pg_stat_activity WHERE application_name = %s "
            "AND state = 'idle in transaction'", (self.app + "-pret",))
        return int(rows[0]["pid"]) if rows else None

    def blocked(self, worker: "Worker") -> bool:
        """Attend qu'une session soit bloquée derrière cette transaction.

        Faux si `worker` a terminé sans jamais l'attendre.
        """
        def check():
            if not worker.is_alive():
                return "terminé"
            rows = self.test.db.query(
                "SELECT count(*)::int AS n FROM pg_stat_activity "
                "WHERE %s::int = ANY(pg_blocking_pids(pid))", (self.pid,))
            return "bloqué" if rows[0]["n"] else None
        return self.test.wait_for(check, timeout=30) == "bloqué"

    def commit(self) -> None:
        self._end("COMMIT")

    def rollback(self) -> None:
        self._end("ROLLBACK")

    def _end(self, verb: str) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.write("%s;\n" % verb)
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.test.assertEqual(self.proc.returncode, 0, self.proc.stderr.read())

    def __enter__(self) -> "HeldTransaction":
        return self

    def __exit__(self, *exc) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.write("ROLLBACK;\n")
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stderr.close()


def revoking(test: PgTestCase, credential_id: str) -> HeldTransaction:
    """Une révocation d'authentificateur EN COURS : l'UPDATE, non encore validé."""
    row = test.db.query("SELECT id FROM authenticators WHERE credential_id = %s "
                        "AND revoked_at IS NULL", (credential_id,))[0]
    return HeldTransaction(
        test, "UPDATE authenticators SET revoked_at = now(), "
              "revoked_reason = 'revocation en cours (test)' WHERE id = %d;" % int(row["id"]))


class Worker(threading.Thread):
    """Un appel dans un fil, sur SA connexion (le pilote testé)."""

    def __init__(self, test: PgTestCase, call):
        super().__init__(daemon=True)
        self.test, self.call = test, call
        self.value = self.error = None
        self.start()

    def run(self) -> None:
        db = self.test.connect()
        try:
            self.value = self.call(db)
        except BaseException as exc:  # rendu au fil principal par result()
            self.error = exc
        finally:
            db.close()

    def result(self, timeout: float = 60.0):
        self.join(timeout)
        if self.is_alive():
            raise AssertionError("appel toujours bloqué après %ss" % timeout)
        if self.error is not None:
            raise self.error
        return self.value


class StandingBase(ReceiptsBase):
    """Utilitaires des grants (aucun test ici)."""

    def register(self, soft=None, approver=ALICE, **overrides):
        soft = soft or self.alice
        receipt = soft.receipt(make_request(approver, standing=standing(**overrides)))
        verdict, grant = receipts.register_standing(self.db, receipt, POLICY, registered_by="porte")
        self.assertTrue(verdict.ok, verdict.reason)
        return grant, receipt

    def reserve(self, grant, act):
        return receipts.standing_reserve(
            self.db, grant, act["action_id"], act["amount"], connector=act["connector"],
            operation=act["operation"], action_class=act["class"], currency=act["currency"])

    def consumed(self, grant) -> int:
        return int(receipts.get_standing(self.db, grant)["consumed_amount"])

    def reserving(self, grant, act) -> "Worker":
        """La réservation, lancée dans un fil sur sa propre connexion."""
        return Worker(self, lambda db: receipts.standing_reserve(
            db, grant, act["action_id"], act["amount"], connector=act["connector"],
            operation=act["operation"], action_class=act["class"], currency=act["currency"]))

    def lock_grant(self, grant) -> HeldTransaction:
        """Une autre transaction tient le verrou du grant (transaction ouverte)."""
        return HeldTransaction(
            self, "SELECT id FROM standing_approvals WHERE id = %d FOR UPDATE;" % int(grant))

    def revoking(self, credential_id: str) -> HeldTransaction:
        """Une révocation de l'authentificateur EN COURS (UPDATE non validé)."""
        return revoking(self, credential_id)

    def live_reservations(self) -> int:
        return self.db.query("SELECT count(*)::int AS n FROM standing_reservations "
                             "WHERE released_at IS NULL")[0]["n"]


class StandingTest(StandingBase):
    def test_cycle_reserver_reutiliser_liberer(self):
        grant, _receipt = self.register()
        first = action()
        reserved = self.reserve(grant, first)
        self.assertIsNotNone(reserved)
        self.assertEqual((reserved["reserved_amount"], reserved["grant_consumed"],
                          reserved["reused"]), (30, 30, False))
        # même action (nouvelle tentative après issue inconnue + déduplication
        # garantie) : même réservation, rien de plus n'est prélevé
        again = self.reserve(grant, first)
        self.assertEqual((again["reservation_id"], again["reused"]),
                         (reserved["reservation_id"], True))
        self.assertEqual(self.consumed(grant), 30)
        with self.assertRaises(receipts.ReceiptError):
            self.reserve(grant, dict(first, amount=31))
        # issue inconnue : jamais de libération
        with self.assertRaises(receipts.ReceiptError):
            receipts.standing_release(self.db, reserved["reservation_id"], outcome="unknown")
        self.assertEqual(self.consumed(grant), 30)
        # échec certain : libérée une fois
        released = receipts.standing_release(self.db, reserved["reservation_id"])
        self.assertEqual(released["released_amount"], 30)
        self.assertEqual(self.consumed(grant), 0)
        self.assertIsNone(receipts.standing_release(self.db, reserved["reservation_id"]))
        self.assertEqual(self.consumed(grant), 0)
        # une nouvelle tentative de la même action peut réserver de nouveau
        self.assertEqual(self.reserve(grant, first)["reused"], False)
        self.assertEqual(self.consumed(grant), 30)

    def test_idempotence_exige_la_meme_description(self):
        """B1 : réutiliser une réservation exige la MÊME action, sinon refus explicite."""
        grant, _ = self.register()          # EUR, opérations refund et label
        first = action(amount=30)
        reserved = self.reserve(grant, first)
        for label, changed, needle in (
            ("devise", dict(first, currency="USD"), "devise USD au lieu de EUR"),
            ("sans devise", dict(first, currency=None), "devise null au lieu de EUR"),
            ("montant", dict(first, amount=31), "montant 31 au lieu de 30"),
            ("connecteur", dict(first, connector="git-merge"), "connecteur git-merge"),
            # opération que le grant couvre AUSSI : seule la réservation la voit
            ("opération", dict(first, operation="label"), "opération label au lieu de refund"),
            ("classe", dict(first, **{"class": "irreversible"}), "classe irreversible"),
        ):
            with self.subTest(label):
                with self.assertRaises(receipts.ReceiptError) as caught:
                    self.reserve(grant, changed)
                self.assertIn("autre description", str(caught.exception))
                self.assertIn(needle, str(caught.exception))
                # standing_cover passe par la réservation vivante : refus aussi
                with self.assertRaises(receipts.ReceiptError):
                    receipts.standing_cover(self.db, changed)
        # rien n'a bougé ; la même description réutilise toujours la réservation
        self.assertEqual(self.consumed(grant), 30)
        again = self.reserve(grant, first)
        self.assertEqual((again["reservation_id"], again["reused"]),
                         (reserved["reservation_id"], True))
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM standing_reservations")[0]["n"], 1)
        # sans montant non plus, la devise annoncée doit être la même
        free = action(amount=0, currency=None)
        self.assertFalse(self.reserve(grant, free)["reused"])
        with self.assertRaises(receipts.ReceiptError):
            self.reserve(grant, dict(free, currency="EUR"))
        self.assertTrue(self.reserve(grant, free)["reused"])

    def test_plafond_cumule(self):
        grant, _ = self.register()
        self.assertIsNotNone(self.reserve(grant, action(amount=60)))
        self.assertIsNotNone(self.reserve(grant, action(amount=40)))
        self.assertIsNone(self.reserve(grant, action(amount=1)))
        self.assertIsNotNone(self.reserve(grant, action(amount=0)))
        self.assertEqual(self.consumed(grant), 100)

    def test_plafond_en_concurrence(self):
        grant, _ = self.register()
        workers = 10
        barrier = threading.Barrier(workers)
        results: list = []
        lock = threading.Lock()

        def attempt() -> None:
            db = self.connect()
            act = action(amount=30)
            try:
                barrier.wait(timeout=30)
                result = receipts.standing_reserve(
                    db, grant, act["action_id"], 30, connector="shell-noop",
                    operation="refund", action_class="costly", currency="EUR")
            finally:
                db.close()
            with lock:
                results.append(result)

        threads = [threading.Thread(target=attempt) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(len(results), workers)
        self.assertEqual(sum(1 for r in results if r), 3)
        self.assertEqual(self.consumed(grant), 90)
        count = self.db.query("SELECT count(*)::int AS n FROM standing_reservations "
                              "WHERE released_at IS NULL")[0]["n"]
        self.assertEqual(count, 3)

    def test_meme_action_en_concurrence(self):
        grant, _ = self.register()
        act = action(amount=30)
        workers = 6
        barrier = threading.Barrier(workers)
        results: list = []
        lock = threading.Lock()

        def attempt() -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=30)
                result = receipts.standing_reserve(
                    db, grant, act["action_id"], 30, connector="shell-noop",
                    operation="refund", action_class="costly", currency="EUR")
            finally:
                db.close()
            with lock:
                results.append(result)

        threads = [threading.Thread(target=attempt) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertTrue(all(results))
        self.assertEqual(len({r["reservation_id"] for r in results}), 1)
        self.assertEqual(sum(1 for r in results if not r["reused"]), 1)
        self.assertEqual(self.consumed(grant), 30)

    def test_enregistrement_rejoue(self):
        grant, receipt = self.register()
        verdict, again = receipts.register_standing(self.db, receipt, POLICY, registered_by="x")
        self.assertIsNone(again)
        self.assertRefused(verdict, receipts.REPLAY)

    def test_enregistrement_concurrent(self):
        receipt = self.alice.receipt(make_request(ALICE, standing=standing()))
        workers = 6
        barrier = threading.Barrier(workers)
        grants: list = []
        lock = threading.Lock()

        def attempt() -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=30)
                _verdict, grant = receipts.register_standing(db, receipt, POLICY,
                                                             registered_by="porte")
            finally:
                db.close()
            with lock:
                grants.append(grant)

        threads = [threading.Thread(target=attempt) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(sum(1 for g in grants if g), 1)
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM standing_approvals")[0]["n"], 1)

    def test_refus_apres_until(self):
        grant, _ = self.register()
        self.db.execute("UPDATE standing_approvals SET valid_until = now() - interval '1 second' "
                        "WHERE id = %s", (grant,))
        self.assertIsNone(self.reserve(grant, action()))
        # un reçu standing déjà échu ne s'enregistre pas
        until = int(time.time()) + 60
        receipt = self.alice.receipt(make_request(ALICE, standing=standing(until=until)))
        verdict, none = receipts.register_standing(self.db, receipt, POLICY, registered_by="x",
                                                   now=until + 1)
        self.assertIsNone(none)
        self.assertFalse(verdict.ok)

    def test_attente_du_verrou_puis_reservation(self):
        """Témoin des courses réelles : la réservation attend le verrou, puis passe."""
        grant, _ = self.register()
        with self.lock_grant(grant) as holder:
            worker = self.reserving(grant, action(amount=30))
            self.assertTrue(holder.blocked(worker), "la réservation n'a pas attendu le verrou")
            holder.commit()
        reserved = worker.result()
        self.assertIsNotNone(reserved)
        self.assertEqual(self.consumed(grant), 30)

    def test_echeance_pendant_l_attente_du_verrou(self):
        """B2 : un grant échu PENDANT l'attente du verrou ne couvre plus rien.

        now() est figé au début de la transaction, avant l'attente : seule
        l'heure réelle (clock_timestamp()) voit l'échéance passée.
        """
        grant, _ = self.register()
        self.db.query("UPDATE standing_approvals SET valid_until = clock_timestamp() "
                      "+ interval '3 seconds' WHERE id = %s RETURNING id", (grant,))
        with self.lock_grant(grant) as holder:
            worker = self.reserving(grant, action(amount=30))
            self.assertTrue(holder.blocked(worker), "la réservation n'a pas attendu le verrou")
            self.assertTrue(self.db.query(
                "SELECT clock_timestamp() < valid_until AS avant FROM standing_approvals "
                "WHERE id = %s", (grant,))[0]["avant"],
                "banc trop lent : l'échéance est passée avant le début de la réservation")
            # l'échéance passe pendant que la réservation attend
            self.wait_for(lambda: self.db.query(
                "SELECT clock_timestamp() > valid_until AS passee FROM standing_approvals "
                "WHERE id = %s", (grant,))[0]["passee"], timeout=10)
            holder.commit()
        self.assertIsNone(worker.result())
        self.assertEqual(self.consumed(grant), 0)
        self.assertEqual(self.live_reservations(), 0)

    def test_authentificateur_revoque_pendant_l_attente(self):
        """B3 : authentificateur révoqué (et validé) pendant l'attente du verrou."""
        grant, _ = self.register()
        with self.lock_grant(grant) as holder:
            worker = self.reserving(grant, action(amount=30))
            self.assertTrue(holder.blocked(worker), "la réservation n'a pas attendu le verrou")
            # le canon retire la passkey qui a signé le grant ; révocation validée
            self.members[0]["authenticators"] = [
                e for e in self.members[0]["authenticators"]
                if e["credential_id"] != self.alice.credential_id]
            result = apply_authenticators(self.db, self.members)
            self.assertEqual(result["revoked"],
                             ["%s/webauthn/%s" % (ALICE, self.alice.credential_id)])
            holder.commit()
        self.assertIsNone(worker.result())
        self.assertEqual(self.consumed(grant), 0)
        self.assertEqual(self.live_reservations(), 0)

    def test_grant_revoque_pendant_l_attente(self):
        grant, _ = self.register()
        with HeldTransaction(self, "UPDATE standing_approvals SET revoked_at = now(), "
                                   "revoked_by = 'human:alice' WHERE id = %d;" % grant) as holder:
            worker = self.reserving(grant, action(amount=30))
            self.assertTrue(holder.blocked(worker), "la réservation n'a pas attendu le verrou")
            holder.commit()
        self.assertIsNone(worker.result())
        self.assertEqual(self.consumed(grant), 0)

    def test_revocation_en_cours_serialisee_avec_la_reservation(self):
        """B3 : FOR SHARE sur l'authentificateur — la révocation en cours est attendue."""
        grant, _ = self.register()
        with self.revoking(self.alice.credential_id) as revocation:
            worker = self.reserving(grant, action(amount=30))
            waited = revocation.blocked(worker)
            revocation.commit()
        self.assertIsNone(worker.result())
        self.assertTrue(waited, "la réservation n'a pas attendu la révocation en cours")
        self.assertEqual(self.consumed(grant), 0)
        self.assertEqual(self.live_reservations(), 0)

    def test_enregistrement_pendant_une_revocation(self):
        """B3 : l'enregistrement d'un grant attend une révocation en cours, puis refuse."""
        receipt = self.alice.receipt(make_request(ALICE, standing=standing()))
        with self.revoking(self.alice.credential_id) as revocation:
            worker = Worker(self, lambda db: receipts.register_standing(
                db, receipt, POLICY, registered_by="porte"))
            waited = revocation.blocked(worker)
            revocation.commit()
        verdict, grant = worker.result()
        self.assertIsNone(grant)
        self.assertRefused(verdict, receipts.REVOKED)
        self.assertTrue(waited, "l'enregistrement n'a pas attendu la révocation en cours")
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM mesh_consumed_nonces")[0]["n"], 0)

    def test_grant_trop_long(self):
        receipt = self.alice.receipt(make_request(
            ALICE, standing=standing(until=int(time.time()) + receipts.DEFAULT_MAX_STANDING + 60)))
        verdict, grant = receipts.register_standing(self.db, receipt, POLICY, registered_by="x")
        self.assertIsNone(grant)
        self.assertRefused(verdict, receipts.TTL)

    def test_refus_apres_revocation(self):
        grant, _ = self.register()
        reserved_action = action()
        self.assertIsNotNone(self.reserve(grant, reserved_action))
        self.assertTrue(receipts.revoke_standing(self.db, grant, by="human:alice"))
        self.assertFalse(receipts.revoke_standing(self.db, grant, by="human:alice"))
        self.assertIsNone(self.reserve(grant, action()))
        # la réservation déjà faite n'autorise plus de nouvelle tentative non plus
        self.assertIsNone(self.reserve(grant, reserved_action))
        self.assertIsNone(receipts.standing_cover(self.db, reserved_action))

    def test_authentificateur_revoque_tue_le_grant(self):
        grant, _ = self.register()
        self.assertIsNotNone(self.reserve(grant, action()))
        self.members[0]["authenticators"] = [
            e for e in self.members[0]["authenticators"]
            if e["credential_id"] != self.alice.credential_id]
        apply_authenticators(self.db, self.members)
        self.assertIsNone(self.reserve(grant, action()))

    def test_couverture_exacte(self):
        grant, _ = self.register()
        for label, act in {
            "connecteur": action(connector="git-merge"),
            "opération": action(operation="ship"),
            "classe": action(**{"class": "irreversible"}),
            "devise": action(currency="USD"),
            "sans devise": action(currency=None),
        }.items():
            with self.subTest(label):
                self.assertIsNone(self.reserve(grant, act))
        self.assertEqual(self.consumed(grant), 0)
        # sans montant : couvert quelle que soit la devise
        self.assertIsNotNone(self.reserve(grant, action(amount=None, currency=None)))

    def test_standing_cover(self):
        small, _ = self.register(max_amount=40)
        large, _ = self.register(max_amount=500, until=int(time.time()) + 7200)
        act = action(amount=30)
        first = receipts.standing_cover(self.db, act)
        self.assertEqual(first["standing_id"], small)      # celui qui échoit le premier
        self.assertEqual(receipts.standing_cover(self.db, act)["reservation_id"],
                         first["reservation_id"])
        second = receipts.standing_cover(self.db, action(amount=30))
        self.assertEqual(second["standing_id"], large)     # le premier n'a plus la place
        self.assertIsNone(receipts.standing_cover(self.db, action(operation="ship")))

    def test_natures_non_interchangeables(self):
        standing_receipt = self.alice.receipt(make_request(ALICE, standing=standing()))
        self.assertRefused(self.verify(standing_receipt), receipts.KIND)
        self.assertRefused(self.verify(standing_receipt, kind=None, consume_by="porte"),
                           receipts.KIND)
        self.assertTrue(self.verify(standing_receipt, kind=None).ok)
        action_receipt = self.alice.receipt(make_request(ALICE))
        verdict, grant = receipts.register_standing(self.db, action_receipt, POLICY,
                                                    registered_by="x")
        self.assertIsNone(grant)
        self.assertRefused(verdict, receipts.KIND)
        deny = self.alice.receipt(make_request(ALICE, standing=standing(), decision="deny"))
        verdict, grant = receipts.register_standing(self.db, deny, POLICY, registered_by="x")
        self.assertIsNone(grant)
        self.assertRefused(verdict, receipts.DECISION)

    def test_forme_du_standing(self):
        for label, overrides in {
            "classe": {"class": "gratuit"},
            "plafond flottant": {"max_amount": 10.5},
            "plafond sans devise": {"currency": None},
            "devise": {"currency": "euro"},
            "opérations vides": {"operations": []},
            "opérations en double": {"operations": ["refund", "refund"]},
        }.items():
            with self.subTest(label):
                # la forme est contrôlée avant toute preuve : une preuve factice suffit
                receipt = make_receipt(make_request(ALICE, standing=standing(**overrides)),
                                       "webauthn", self.alice.credential_id,
                                       self.alice.proof(b"\x00" * 32))
                verdict, grant = receipts.register_standing(self.db, receipt, POLICY,
                                                            registered_by="x")
                self.assertIsNone(grant)
                self.assertRefused(verdict, receipts.FORMAT)


# --------------------------------------------------------------------------
# échéances recontrôlées après le dernier verrou (heure réelle de la base)
# --------------------------------------------------------------------------

class DeadlineRaceTest(StandingBase):
    """Toute échéance (exp, until, iat) est recontrôlée en SQL APRÈS le dernier
    verrou, à `clock_timestamp()`, immédiatement avant l'écriture.

    Courses réelles : une première connexion tient un verrou (authentificateur,
    grant ou nonce) — en MODIFIANT la ligne, ou par un simple `SELECT … FOR
    UPDATE / FOR SHARE` qui la laisse intacte —, la seconde l'attend (vu par
    pg_blocking_pids), et l'échéance passe pendant l'attente : la transaction
    entière est annulée. Témoins : la même attente, terminée avant l'échéance,
    réussit.
    """

    #: secondes entre la préparation de la course et l'échéance
    MARGIN = 4
    #: échéance des témoins : bien après la fin de l'attente
    LATER = 60

    def clock(self) -> float:
        """L'heure réelle de la BASE (celle des contrôles SQL)."""
        return float(self.db.query(
            "SELECT extract(epoch from clock_timestamp())::float8 AS epoch")[0]["epoch"])

    def count(self, table: str) -> int:
        return self.db.query("SELECT count(*)::int AS n FROM %s" % table)[0]["n"]

    # -- verrous tenus par la première connexion -------------------------------
    #
    # Chaque course est jouée derrière un verrou qui MODIFIE la ligne et derrière
    # un verrou qui la laisse intacte (`SELECT … FOR UPDATE / FOR SHARE`) :
    # PostgreSQL ne réévalue le WHERE d'une instruction après l'attente
    # (EvalPlanQual) que si la ligne a été modifiée — une échéance placée dans
    # ce WHERE passerait derrière un verrou sans modification. Chaque fabrique
    # reçoit la cible (requête du reçu, ou id du grant) et rend la transaction
    # tenue ; `finish` dit comment elle se termine.

    def authenticator_id(self, credential_id: str) -> int:
        return int(self.db.query("SELECT id FROM authenticators WHERE credential_id = %s "
                                 "AND revoked_at IS NULL", (credential_id,))[0]["id"])

    def authenticator_holders(self) -> list:
        """Verrous sur l'authentificateur signataire d'alice (jamais révoqué :
        la mise à jour est un nouveau commit du canon, comme canon sync).
        Un `FOR SHARE` concurrent n'est pas listé : compatible avec le nôtre, il
        ne fait rien attendre."""
        ident = self.authenticator_id(self.alice.credential_id)
        canon = db_mod.sql_literal("members/alice.md@" + NEXT_COMMIT)
        return [
            ("authentificateur : UPDATE (ligne modifiée)", lambda _target: HeldTransaction(
                self, "UPDATE authenticators SET canon_ref = %s, updated_at = now() "
                      "WHERE id = %d;" % (canon, ident)), "commit"),
            ("authentificateur : SELECT … FOR UPDATE (ligne intacte)",
             lambda _target: HeldTransaction(
                 self, "SELECT id FROM authenticators WHERE id = %d FOR UPDATE;" % ident),
             "commit"),
        ]

    def grant_holders(self) -> list:
        """Verrous sur le grant (la réservation le prend FOR UPDATE)."""
        return [
            ("grant : SELECT … FOR UPDATE (ligne intacte)", lambda grant: HeldTransaction(
                self, "SELECT id FROM standing_approvals WHERE id = %d FOR UPDATE;"
                      % int(grant)), "commit"),
            ("grant : SELECT … FOR SHARE (ligne intacte)", lambda grant: HeldTransaction(
                self, "SELECT id FROM standing_approvals WHERE id = %d FOR SHARE;"
                      % int(grant)), "commit"),
            ("grant : UPDATE (ligne modifiée)", lambda grant: HeldTransaction(
                self, "UPDATE standing_approvals SET registered_by = 'concurrent' "
                      "WHERE id = %d;" % int(grant)), "commit"),
        ]

    def nonce_holders(self) -> list:
        """Une autre transaction a inséré le MÊME nonce, puis s'annule : l'INSERT
        … ON CONFLICT de la seconde attend son issue (verrou de la clé unique)."""
        return [
            ("nonce : INSERT concurrent annulé", lambda request: HeldTransaction(
                self, "INSERT INTO mesh_consumed_nonces (approver, nonce, consumed_by) "
                      "VALUES (%s, %s, 'concurrent');" % (
                          db_mod.sql_literal(request["approver"]),
                          db_mod.sql_literal(request["nonce"]))), "rollback"),
        ]

    def race(self, holder: HeldTransaction, call, deadline: float, *, expires: bool,
             finish: str = "commit"):
        """`call` (sur sa connexion) attend le verrou de `holder`.

        `expires` : l'échéance passe PENDANT l'attente (heure de la base), puis
        `holder` se termine ; sinon (témoin) `holder` se termine aussitôt.
        """
        worker = Worker(self, call)
        self.assertTrue(holder.blocked(worker), "l'appel n'a pas attendu le verrou")
        self.assertLess(self.clock(), deadline,
                        "banc trop lent : l'échéance est passée avant l'attente")
        if expires:
            self.wait_for(lambda: self.clock() > deadline + 0.2, timeout=self.MARGIN + 10)
            self.assertTrue(worker.is_alive(), "l'appel n'attendait plus le verrou")
        getattr(holder, finish)()
        result = worker.result()
        if not expires:
            self.assertLess(self.clock(), deadline, "témoin : l'échéance est passée")
        return result

    def action_receipt(self, exp: int) -> dict:
        iat = int(self.clock()) - 10
        return self.alice.receipt(make_request(ALICE, iat=iat, ttl=exp - iat))

    def standing_receipt(self, *, exp: int | None = None, until: int | None = None) -> dict:
        iat = int(self.clock()) - 10
        ttl = (exp - iat) if exp is not None else 600
        return self.alice.receipt(make_request(
            ALICE, iat=iat, ttl=ttl, standing=standing(until=until or iat + 3600)))

    def registering(self, receipt):
        return lambda db: receipts.register_standing(db, receipt, POLICY, registered_by="porte")

    def consuming(self, receipt):
        return lambda db: self.consume(receipt, db=db)

    def assertNothingWritten(self) -> None:
        self.assertEqual(self.count("mesh_consumed_nonces"), 0)
        self.assertEqual(self.count("standing_approvals"), 0)

    def alice_live(self) -> bool:
        return bool(self.db.query(
            "SELECT 1 AS x FROM authenticators WHERE credential_id = %s "
            "AND revoked_at IS NULL", (self.alice.credential_id,)))

    # -- réservation -------------------------------------------------------
    def expiring_grant(self, margin: float) -> tuple[int, float]:
        """Un grant dont l'échéance tombe dans `margin` secondes (heure de la base)."""
        grant, _ = self.register()
        deadline = self.clock() + margin
        self.db.query("UPDATE standing_approvals SET valid_until = to_timestamp(%s) "
                      "WHERE id = %s RETURNING id", (deadline, grant))
        return grant, deadline

    def reserve_on(self, db, grant, act):
        return receipts.standing_reserve(
            db, grant, act["action_id"], act["amount"], connector=act["connector"],
            operation=act["operation"], action_class=act["class"], currency=act["currency"])

    def test_reservation_grant_echu_pendant_l_attente(self):
        """Course (1) : le grant échoit pendant l'attente d'un verrou — celui
        de l'authentificateur (non révoqué) ou celui du grant, ligne modifiée
        ou intacte — : aucune réservation, cumul inchangé."""
        for label, make, finish in self.authenticator_holders() + self.grant_holders():
            with self.subTest(label):
                grant, deadline = self.expiring_grant(self.MARGIN)
                with make(grant) as holder:
                    result = self.race(
                        holder, lambda db: self.reserve_on(db, grant, action(amount=30)),
                        deadline, expires=True, finish=finish)
                self.assertIsNone(result)
                self.assertEqual(self.consumed(grant), 0)
                self.assertEqual(int(receipts.get_standing(self.db, grant)["uses"]), 0)
                self.assertEqual(self.db.query(
                    "SELECT count(*)::int AS n FROM standing_reservations WHERE grant_id = %s",
                    (grant,))[0]["n"], 0)
                self.assertTrue(self.alice_live(), "l'authentificateur ne devait pas être révoqué")

    def test_reservation_temoin_attente_avant_l_echeance(self):
        grant, deadline = self.expiring_grant(self.LATER)
        holders = self.authenticator_holders() + self.grant_holders()
        for index, (label, make, finish) in enumerate(holders, start=1):
            with self.subTest(label):
                with make(grant) as holder:
                    result = self.race(
                        holder, lambda db: self.reserve_on(db, grant, action(amount=10)),
                        deadline, expires=False, finish=finish)
                self.assertIsNotNone(result)
                self.assertEqual(self.consumed(grant), 10 * index)

    # -- enregistrement d'un grant -------------------------------------------
    def test_enregistrement_echeance_pendant_l_attente(self):
        """exp du reçu, ou until du grant, passé pendant l'attente du verrou de
        l'authentificateur (ligne modifiée ou intacte), ou de l'INSERT du nonce
        (insertion concurrente annulée ; l'échéance est alors recontrôlée APRÈS
        l'écriture) : aucun grant enregistré, aucun nonce consommé."""
        cases = [(label + ", " + field, make, finish, field)
                 for label, make, finish in self.authenticator_holders()
                 for field in ("exp", "until")]
        cases += [(label + ", exp", make, finish, "exp")
                  for label, make, finish in self.nonce_holders()]
        for label, make, finish, field in cases:
            with self.subTest(label):
                deadline = int(self.clock()) + self.MARGIN
                receipt = self.standing_receipt(**{field: deadline})
                with make(receipt["request"]) as holder:
                    verdict, grant = self.race(holder, self.registering(receipt), deadline,
                                               expires=True, finish=finish)
                self.assertIsNone(grant)
                self.assertRefused(verdict, receipts.EXPIRED)
                self.assertFalse(verdict.consumed)
                self.assertNothingWritten()
                self.assertTrue(self.alice_live())

    def test_enregistrement_temoin_attente_avant_l_echeance(self):
        holders = self.authenticator_holders() + self.nonce_holders()
        for index, (label, make, finish) in enumerate(holders, start=1):
            with self.subTest(label):
                deadline = int(self.clock()) + self.LATER
                receipt = self.standing_receipt(exp=deadline, until=deadline)
                with make(receipt["request"]) as holder:
                    verdict, grant = self.race(holder, self.registering(receipt), deadline,
                                               expires=False, finish=finish)
                self.assertTrue(verdict.ok, verdict.reason)
                self.assertIsNotNone(grant)
                self.assertEqual((self.count("mesh_consumed_nonces"),
                                  self.count("standing_approvals")), (index, index))

    # -- consommation d'un nonce (reçu d'action) -------------------------------
    def test_consommation_recu_echu_pendant_l_attente(self):
        """Course (2) : le reçu d'action expire pendant l'attente — verdict
        EXPIRED, nonce NON consommé (derrière l'authentificateur, ligne modifiée
        ou intacte, ou derrière une insertion concurrente du même nonce)."""
        for label, make, finish in self.authenticator_holders() + self.nonce_holders():
            with self.subTest(label):
                deadline = int(self.clock()) + self.MARGIN
                receipt = self.action_receipt(deadline)
                with make(receipt["request"]) as holder:
                    verdict = self.race(holder, self.consuming(receipt), deadline,
                                        expires=True, finish=finish)
                self.assertRefused(verdict, receipts.EXPIRED)
                self.assertFalse(verdict.consumed)
                self.assertEqual(self.count("mesh_consumed_nonces"), 0)

    def test_consommation_temoin_attente_avant_l_echeance(self):
        holders = self.authenticator_holders() + self.nonce_holders()
        for index, (label, make, finish) in enumerate(holders, start=1):
            with self.subTest(label):
                receipt = self.action_receipt(int(self.clock()) + self.LATER)
                with make(receipt["request"]) as holder:
                    verdict = self.race(holder, self.consuming(receipt),
                                        receipt["request"]["exp"], expires=False,
                                        finish=finish)
                self.assertTrue(verdict.ok, verdict.reason)
                self.assertTrue(verdict.consumed)
                self.assertEqual(self.count("mesh_consumed_nonces"), index)

    # -- l'heure de la base fait foi pour toute écriture ----------------------
    def test_horloge_de_l_hote_en_retard(self):
        """L'hôte croit le reçu encore valable (`now` dans le passé) : la base,
        à l'heure réelle, refuse toute écriture."""
        request = make_request(ALICE, iat=int(time.time()) - 700, ttl=600)   # exp passé
        receipt = self.alice.receipt(request)
        late = request["exp"] - 1
        self.assertTrue(self.verify(receipt, now=late).ok)          # lecture seule
        verdict = receipts.verify_receipt(
            self.db, receipt, POLICY, consume_by="porte", expected_digest=request["digest"],
            expected_action_id=request["action_id"], now=late)
        self.assertRefused(verdict, receipts.EXPIRED)
        self.assertFalse(verdict.consumed)
        now = int(time.time())
        for label, request in (
                ("exp", make_request(ALICE, iat=now - 700, ttl=600, standing=standing())),
                ("until", make_request(ALICE, iat=now - 700, ttl=1200,
                                       standing=standing(until=now - 50)))):
            with self.subTest(label):
                late = min(request["exp"], request["standing"]["until"]) - 1
                verdict, grant = receipts.register_standing(
                    self.db, self.alice.receipt(request), POLICY, registered_by="porte",
                    now=late)
                self.assertIsNone(grant)
                self.assertRefused(verdict, receipts.EXPIRED)
        self.assertNothingWritten()

    def test_horloge_de_l_hote_en_avance(self):
        """iat dans le futur pour la base (hôte en avance) : aucune écriture."""
        ahead = time.time() + 600
        request = make_request(ALICE, iat=int(ahead))
        receipt = self.alice.receipt(request)
        self.assertTrue(self.verify(receipt, now=ahead).ok)          # lecture seule
        verdict = receipts.verify_receipt(
            self.db, receipt, POLICY, consume_by="porte", expected_digest=request["digest"],
            expected_action_id=request["action_id"], now=ahead)
        self.assertRefused(verdict, receipts.NOT_YET)
        request = make_request(ALICE, iat=int(ahead), standing=standing(until=int(ahead) + 3600))
        verdict, grant = receipts.register_standing(
            self.db, self.alice.receipt(request), POLICY, registered_by="porte", now=ahead)
        self.assertIsNone(grant)
        self.assertRefused(verdict, receipts.NOT_YET)
        self.assertNothingWritten()


# --------------------------------------------------------------------------
# synchronisation du registre
# --------------------------------------------------------------------------

class SyncTest(ReceiptsBase):
    def test_sync_idempotente_et_mise_a_jour(self):
        result = apply_authenticators(self.db, self.members)
        self.assertEqual((result["added"], result["revoked"], result["updated"]), ([], [], []))
        self.assertEqual(result["unchanged"], 7)
        # nouveau commit du canon : la provenance de chaque ligne suit
        moved = [member("alice", self.alice.entry(level="eleve"), self.alice_ed.entry(),
                        self.alice_device.entry(), self.alice_tool.entry(), commit=NEXT_COMMIT),
                 member("bob", self.bob.entry(), commit=NEXT_COMMIT),
                 member("carol", self.carol_key.entry(level="eleve"),
                        self.carol_synced.entry(), commit=NEXT_COMMIT)]
        result = apply_authenticators(self.db, moved)
        self.assertEqual(len(result["updated"]), 7)
        self.assertEqual(receipts.mismatched_authenticators(self.db, NEXT_COMMIT), [])
        self.assertEqual(len(receipts.mismatched_authenticators(self.db, COMMIT)), 7)
        rows = receipts.list_authenticators(self.db, approver=ALICE)
        level = {row["credential_id"]: row["level"] for row in rows}
        self.assertEqual(level[self.alice.credential_id], "eleve")

    def test_changement_de_cle(self):
        replaced = SoftWebAuthn()
        replaced.credential_id = self.alice.credential_id
        self.members[0]["authenticators"][0] = replaced.entry()
        result = apply_authenticators(self.db, self.members)
        label = "%s/webauthn/%s" % (ALICE, self.alice.credential_id)
        self.assertEqual((result["revoked"], result["added"]), ([label], [label]))
        self.assertRefused(self.verify(self.alice.receipt(make_request(ALICE))),
                           receipts.SIGNATURE)
        self.assertTrue(self.verify(replaced.receipt(make_request(ALICE))).ok)
        history = receipts.list_authenticators(self.db, approver=ALICE, include_revoked=True)
        self.assertEqual(sum(1 for row in history
                             if row["credential_id"] == self.alice.credential_id), 2)

    def test_membre_retire_et_credential_deplace(self):
        # bob disparaît du canon ; son credential est déclaré pour carol
        self.members[1]["authenticators"] = []
        self.members[2]["authenticators"].append(self.bob.entry())
        result = apply_authenticators(self.db, self.members)
        self.assertIn("%s/webauthn/%s" % (BOB, self.bob.credential_id), result["revoked"])
        self.assertIn("%s/webauthn/%s" % (CAROL, self.bob.credential_id), result["added"])
        self.assertRefused(self.verify(self.bob.receipt(make_request(BOB))),
                           receipts.REVOKED)
        self.assertTrue(self.verify(self.bob.receipt(make_request(CAROL))).ok)

    def test_entree_sans_reference_de_commit(self):
        """Constat d'erreur, aucune écriture pour le membre — même pas une révocation."""
        newcomer = SoftWebAuthn()
        members = [dict(m) for m in self.members]
        members[0] = dict(members[0], canon_ref=None, authenticators=[
            dict(self.alice.entry()),                                   # existante
            dict(newcomer.entry()),                                     # nouvelle
            dict(self.alice_ed.entry(), canon_ref="members/alice.md"),  # chemin sans SHA
            dict(self.alice_device.entry(), canon_ref="members/alice.md@" + COMMIT),
        ])
        before = receipts.list_authenticators(self.db, approver=ALICE)
        result = apply_authenticators(self.db, members)
        self.assertEqual(len(result["errors"]), 3, result["errors"])
        self.assertTrue(all("référence de commit" in e for e in result["errors"]))
        self.assertEqual(result["added"], [])
        # liste invalide : alice est gelée — ses lignes restent telles quelles,
        # l'outil ed25519 absent de CETTE liste n'est pas révoqué pour autant
        self.assertEqual((result["revoked"], result["updated"]), ([], []))
        self.assertEqual(result.get("frozen"), [ALICE])
        after = {row["credential_id"]: row for row in
                 receipts.list_authenticators(self.db, approver=ALICE)}
        self.assertEqual(sorted(after), sorted(row["credential_id"] for row in before))
        for row in before:
            self.assertEqual(after[row["credential_id"]]["canon_ref"], row["canon_ref"])
        self.assertNotIn(newcomer.credential_id, after)

    def test_membre_invalide_ne_change_rien(self):
        """B4 : une entrée de membre invalide ne change RIEN pour ce membre.

        Règle : `authenticators` qui n'est pas une liste (texte, objet, null,
        absent), ou une liste dont UN élément est invalide, gèle le membre — ni
        ajout, ni mise à jour, ni révocation. Seule une liste valide qui omet
        un credential le révoque.
        """
        newcomer = SoftWebAuthn()
        cases = {
            "texte": "webauthn:" + self.alice.credential_id,
            "objet": self.alice.entry(),
            "null": None,
            "absent": KeyError,
            "liste avec un élément non objet": [self.alice.entry(), newcomer.entry(), "x"],
            "liste avec une façade invalide": [self.alice.entry(), newcomer.entry(),
                                               dict(SoftWebAuthn().entry(), facade="sms")],
            "liste avec un objet incomplet": [self.alice.entry(), {"facade": "webauthn"}],
            "liste avec une clé illisible": [
                self.alice.entry(), dict(newcomer.entry(), public_key=b64u(b"pas une cle"))],
            "liste avec une clé de petit ordre": [
                self.alice.entry(), dict(newcomer.entry(), public_key=b64u(okp_cose(IDENTITY)))],
            "liste avec un niveau inconnu": [self.alice.entry(level="maximal")],
            "liste avec un credential en double": [self.alice.entry(), self.alice.entry()],
        }
        before = receipts.list_authenticators(self.db, include_revoked=True)
        for label, value in cases.items():
            with self.subTest(label):
                members = [dict(m) for m in self.members]
                if value is KeyError:
                    members[0] = {k: v for k, v in members[0].items() if k != "authenticators"}
                else:
                    members[0] = dict(members[0], authenticators=value)
                # un autre membre change en même temps : lui est appliqué
                members[1] = dict(members[1], authenticators=[self.bob.entry(level="eleve")])
                result = apply_authenticators(self.db, members)
                self.assertEqual((result["added"], result["revoked"]), ([], []))
                after = receipts.list_authenticators(self.db, include_revoked=True)
                alice_before = [r for r in before if r["approver"] == ALICE]
                self.assertEqual([r for r in after if r["approver"] == ALICE], alice_before)
                self.assertNotIn(newcomer.credential_id, [r["credential_id"] for r in after])
                self.assertNotEqual(result["errors"], [])
                self.assertEqual(result.get("frozen"), [ALICE])
                # la passkey d'alice fait toujours autorité
                self.assertTrue(self.verify(self.alice.receipt(make_request(ALICE))).ok)
        bob = receipts.list_authenticators(self.db, approver=BOB)
        self.assertEqual([r["level"] for r in bob], ["eleve"])
        # témoin : une liste VALIDE qui omet un credential le révoque
        members = [dict(m) for m in self.members]
        members[0] = dict(members[0], authenticators=[self.alice.entry()])
        result = apply_authenticators(self.db, members)
        self.assertEqual(result.get("frozen"), [])
        self.assertEqual(sorted(result["revoked"]), sorted([
            "%s/webauthn/%s" % (ALICE, self.alice_ed.credential_id),
            "%s/device-es256/%s" % (ALICE, self.alice_device.credential_id),
            "%s/ed25519/%s" % (ALICE, self.alice_tool.credential_id)]))

    def test_membres_illisibles_ne_revoquent_rien(self):
        """Une liste de membres invalide, ou un membre sans titre lisible : pas de révocation."""
        before = receipts.list_authenticators(self.db)
        for members in (None, "membres", {"alice": []}):
            with self.subTest(members=members):
                with self.assertRaises(receipts.ReceiptError):
                    apply_authenticators(self.db, members)
        # alice absente, mais une entrée illisible pourrait être la sienne
        members = self.members[1:] + [{"title": "Alice Martin", "authenticators": []}]
        result = apply_authenticators(self.db, members)
        self.assertEqual(result["revoked"], [])
        self.assertTrue(any("illisible" in e for e in result["errors"]), result["errors"])
        self.assertEqual(receipts.list_authenticators(self.db), before)
        # sans entrée illisible, le membre retiré du canon est révoqué
        result = apply_authenticators(self.db, self.members[1:])
        self.assertEqual(len(result["revoked"]), 4)

    def test_canon_lu_en_partie_ne_revoque_rien_pour_absence(self):
        """`revoke_absent=False` : un membre absent garde ses lignes ; une liste
        valide qui omet un credential le révoque toujours."""
        before = receipts.list_authenticators(self.db, approver=ALICE)
        members = [dict(self.members[1], authenticators=[]), self.members[2]]
        result = apply_authenticators(self.db, members, revoke_absent=False)
        self.assertEqual(result["revoked"], ["%s/webauthn/%s" % (BOB, self.bob.credential_id)])
        self.assertEqual(result["errors"], [])
        self.assertEqual(receipts.list_authenticators(self.db, approver=ALICE), before)

    def test_revocation_note_le_commit_qui_la_constate(self):
        """La ligne révoquée par la synchronisation porte le commit du canon qui
        l'a retirée : fiche du membre lue, ou ancien chemin au commit de sa source."""
        self.db.query("UPDATE authenticators SET canon_ref = %s WHERE approver = %s "
                      "RETURNING id", ("home:members/carol.md@" + COMMIT, CAROL))
        members = [dict(self.members[0]),
                   member("bob", commit=NEXT_COMMIT)]                    # bob : liste vide
        result = apply_authenticators(
            self.db, members, source_commits={"home": NEXT_COMMIT})
        self.assertEqual(len(result["revoked"]), 3)                     # bob, et carol ×2
        refs = {(row["approver"], row["credential_id"]): row["canon_ref"]
                for row in receipts.list_authenticators(self.db, include_revoked=True)
                if row.get("revoked_ts")}
        self.assertEqual(refs[(BOB, self.bob.credential_id)], "members/bob.md@" + NEXT_COMMIT)
        self.assertEqual({ref for (who, _c), ref in refs.items() if who == CAROL},
                         {"home:members/carol.md@" + NEXT_COMMIT})

    def test_entrees_invalides(self):
        broken_key = dict(SoftWebAuthn().entry(), public_key=b64u(b"pas une cle COSE"))
        private = SoftWebAuthn()
        x, y = (v.to_bytes(32, "big") for v in private.key.point)
        from .webauthn_soft import cbor
        with_private = dict(private.entry(), public_key=b64u(cbor(
            {1: 2, 3: -7, -1: 1, -2: x, -3: y, -4: private.key.private.to_bytes(32, "big")})))
        bad_level = dict(SoftWebAuthn().entry(), level="maximal")
        bad_facade = dict(SoftWebAuthn().entry(), facade="sms")
        dup = SoftWebAuthn()
        members = self.members + [
            member("dave", broken_key, with_private, bad_level, bad_facade, dup.entry()),
            member("erin", dup.entry()),
            {"title": "pas un nom valide !", "authenticators": []},
        ]
        result = apply_authenticators(self.db, members)
        joined = "\n".join(result["errors"])
        self.assertIn("COSE", joined)
        self.assertIn("PRIVÉE", joined)
        self.assertIn("niveau inconnu", joined)
        self.assertIn("façade ou credential_id invalide", joined)
        self.assertIn("déclaré 2 fois", joined)
        self.assertIn("titre invalide", joined)
        self.assertEqual(result["added"], [])
        self.assertEqual(receipts.list_authenticators(self.db, approver="human:dave"), [])

    def test_cles_en_base64_standard_acceptees_du_canon(self):
        device = SoftDevice()
        raw = receipts.b64u_decode(device.public_key())
        import base64
        entry = dict(device.entry(), public_key=base64.b64encode(raw).decode())
        self.members[0]["authenticators"].append(entry)
        result = apply_authenticators(self.db, self.members)
        self.assertEqual(result["errors"], [])
        self.assertTrue(self.verify(device.receipt(make_request(ALICE))).ok)

    def test_aaguid_normalise(self):
        entry = self.alice.entry(aaguid="ADCE0002-35BC-C60A-648B-0B25F1F05503")
        self.members[0]["authenticators"][0] = entry
        apply_authenticators(self.db, self.members)
        row = [r for r in receipts.list_authenticators(self.db, approver=ALICE)
               if r["credential_id"] == self.alice.credential_id][0]
        self.assertEqual(row["aaguid"], "adce0002-35bc-c60a-648b-0b25f1f05503")

    def test_mismatched_par_correspondance(self):
        rows = receipts.list_authenticators(self.db)
        expected = {(r["approver"], r["facade"], r["credential_id"]): r["canon_ref"] for r in rows}
        self.assertEqual(receipts.mismatched_authenticators(self.db, expected), [])
        first = rows[0]
        expected[(first["approver"], first["facade"], first["credential_id"])] = "x@" + "c" * 40
        self.assertEqual([r["id"] for r in receipts.mismatched_authenticators(self.db, expected)],
                         [first["id"]])
        with self.assertRaises(receipts.ReceiptError):
            receipts.mismatched_authenticators(self.db, "main")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class ReceiptsCliTest(ReceiptsBase):
    def write(self, receipt) -> str:
        path = os.path.join(self.tmp, "recu-%s.json" % os.urandom(4).hex())
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh)
        return path

    def test_receipt_verify(self):
        request = make_request(ALICE)
        path = self.write(self.alice.receipt(request))
        args = ["receipt", "verify", path, "--rp-id", RP_ID, "--origin", ORIGIN]
        proc = self.mesh(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reçu VALIDE", proc.stdout)
        self.assertIn("disponible", proc.stdout)
        proc = self.mesh(*args, "--json", "--digest", request["digest"])
        self.assertEqual(json.loads(proc.stdout)["code"], "ok")
        proc = self.mesh(*args, "--consume", "--by", "cli")
        self.assertEqual(proc.returncode, 2)                  # consommer sans lier : refusé
        bound = ["--digest", request["digest"], "--action-id", request["action_id"]]
        proc = self.mesh(*args, "--consume", "--by", "cli", *bound)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("consommé", proc.stdout)
        proc = self.mesh(*args, "--consume", "--by", "cli", *bound)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[replay]", proc.stderr)

    def test_receipt_verify_refus(self):
        path = self.write(self.alice.receipt(make_request(ALICE), origin="https://evil.test"))
        proc = self.mesh("receipt", "verify", path, "--rp-id", RP_ID, "--origin", ORIGIN)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[origin]", proc.stderr)
        path = self.write(self.alice_tool.receipt(make_request(ALICE)))
        proc = self.mesh("receipt", "verify", path)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[facade_refused]", proc.stderr)
        proc = self.mesh("receipt", "verify", path, "--allow-facade", "ed25519")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("receipt", "verify", path, "--consume")
        self.assertEqual(proc.returncode, 2)

    def test_receipt_verify_variables_d_environnement(self):
        path = self.write(self.alice.receipt(make_request(ALICE)))
        env = self.env(AMEESH_APPROVE_RP_ID=RP_ID, AMEESH_APPROVE_ORIGINS="https://x.test, " + ORIGIN)
        proc = self.mesh("receipt", "verify", path, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_authenticator_list(self):
        proc = self.mesh("authenticator", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(ALICE, proc.stdout)
        self.assertIn("members/bob.md@" + COMMIT, proc.stdout)
        proc = self.mesh("authenticator", "list", "--approver", BOB, "--json")
        rows = json.loads(proc.stdout)
        self.assertEqual([row["credential_id"] for row in rows], [self.bob.credential_id])
        proc = self.mesh("authenticator", "list", "--expect-commit", NEXT_COMMIT)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("HORS CANON ATTENDU", proc.stdout)


if __name__ == "__main__":
    unittest.main()
