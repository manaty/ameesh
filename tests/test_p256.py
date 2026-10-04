# SPDX-License-Identifier: AGPL-3.0-only
"""ES256 (P-256) et COSE : vecteurs RFC 6979, DER strict, backends croisés."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import cose, p256

from .webauthn_soft import SoftEd25519Key, SoftES256Key, cbor, es256_sign

# RFC 6979 §A.2.5 (P-256, SHA-256)
RFC6979_D = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
RFC6979_Q = (0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6,
             0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299)
RFC6979_SIGS = [
    (b"sample", 0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
     0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8),
    (b"test", 0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
     0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083),
]


class P256Test(unittest.TestCase):
    def test_cle_publique_rfc6979(self):
        self.assertEqual(p256.scalar_mult(RFC6979_D, (p256.GX, p256.GY)), RFC6979_Q)
        self.assertTrue(p256.is_on_curve(*RFC6979_Q))

    def test_vecteurs_rfc6979_sur_tous_les_backends(self):
        for name in p256.available_backends():
            for message, r, s in RFC6979_SIGS:
                with self.subTest(backend=name, message=message):
                    der = p256.encode_der_signature(r, s)
                    self.assertEqual(p256.decode_der_signature(der), (r, s))
                    self.assertTrue(p256.verify(RFC6979_Q, message, der, name))
                    self.assertFalse(p256.verify(RFC6979_Q, message + b"!", der, name))
                    self.assertFalse(p256.verify(
                        RFC6979_Q, message, p256.encode_der_signature(r, s ^ 1), name))

    def test_signature_de_test_et_falsifications(self):
        key = SoftES256Key()
        message = os.urandom(69)
        der = key.sign(message)
        for name in p256.available_backends():
            with self.subTest(backend=name):
                self.assertTrue(p256.verify(key.point, message, der, name))
                other = SoftES256Key()
                self.assertFalse(p256.verify(other.point, message, der, name))
                r, s = p256.decode_der_signature(der)
                # s et n - s : les deux sont des signatures ECDSA valides (pas de
                # contrainte « low-s » en WebAuthn) ; r altéré ne l'est pas
                self.assertTrue(p256.verify(
                    key.point, message, p256.encode_der_signature(r, p256.N - s), name))
                self.assertFalse(p256.verify(
                    key.point, message, p256.encode_der_signature((r + 1) % p256.N, s), name))

    def test_bornes_de_r_et_s(self):
        message = b"x"
        for r, s in ((0, 1), (1, 0), (p256.N, 1), (1, p256.N), (p256.N + 5, 3)):
            with self.subTest(r=r, s=s):
                self.assertFalse(p256.verify(RFC6979_Q, message, p256.encode_der_signature(r, s)))

    def test_der_strict(self):
        message, r, s = RFC6979_SIGS[0]
        good = p256.encode_der_signature(r, s)
        self.assertTrue(p256.verify(RFC6979_Q, message, good))
        bad = [
            good + b"\x00",                                    # octet en trop
            good[:-1],                                         # tronquée
            b"\x31" + good[1:],                                # pas une SEQUENCE
            b"\x30\x81" + bytes([len(good) - 2]) + good[2:],   # longueur en forme longue (BER)
            # entier non minimal : 0x00 superflu devant r
            b"\x30" + bytes([len(good) - 1]) + b"\x02" + bytes([good[3] + 1]) + b"\x00"
            + good[4:],
        ]
        # r négatif (bit de poids fort sans 0x00 de tête)
        small = p256.encode_der_signature(0x7F, 0x7F)
        bad.append(small.replace(b"\x02\x01\x7f", b"\x02\x01\xff", 1))
        for blob in bad:
            with self.subTest(blob=blob.hex()):
                with self.assertRaises(p256.P256Error):
                    p256.decode_der_signature(blob)
                self.assertFalse(p256.verify(RFC6979_Q, message, blob))

    def test_points(self):
        x, y = RFC6979_Q
        self.assertEqual(p256.decode_point(p256.encode_point(x, y)), (x, y))
        compressed = bytes([2 | (y & 1)]) + x.to_bytes(32, "big")
        self.assertEqual(p256.decode_point(compressed), (x, y))
        with self.assertRaises(p256.P256Error):
            p256.decode_point(b"\x04" + x.to_bytes(32, "big") + (y ^ 1).to_bytes(32, "big"))
        with self.assertRaises(p256.P256Error):
            p256.decode_point(b"\x00")
        self.assertFalse(p256.verify((x, y ^ 1), b"sample", p256.encode_der_signature(
            RFC6979_SIGS[0][1], RFC6979_SIGS[0][2])))

    def test_backend_force_par_variable(self):
        with mock.patch.dict(os.environ, {"AMEESH_ECDSA_BACKEND": "pure"}):
            self.assertEqual(p256.backend().name, "pure")
        with mock.patch.dict(os.environ, {"AMEESH_ECDSA_BACKEND": "",
                                          "AMEESH_SIGNING_BACKEND": "pure"}):
            self.assertEqual(p256.backend().name, "pure")
        with self.assertRaises(ValueError):
            p256.backend("maison")

    @unittest.skipUnless("cryptography" in p256.available_backends(), "cryptography absent")
    def test_croise_avec_cryptography(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        for index in range(20):
            private = ec.generate_private_key(ec.SECP256R1())
            numbers = private.public_key().public_numbers()
            point = (numbers.x, numbers.y)
            message = os.urandom(index * 7)
            der = private.sign(message, ec.ECDSA(hashes.SHA256()))
            self.assertTrue(p256.verify(point, message, der, "pure"))
            self.assertEqual(
                p256.scalar_mult(private.private_numbers().private_value, (p256.GX, p256.GY)),
                point)
            # et dans l'autre sens : nos signatures de test, vérifiées par OpenSSL
            ours = es256_sign(private.private_numbers().private_value, message)
            self.assertTrue(p256.verify(point, message, ours, "cryptography"))


class CoseTest(unittest.TestCase):
    def test_cles_es256_et_eddsa(self):
        es = SoftES256Key()
        key = cose.parse_cose_key(es.cose())
        self.assertEqual((key.alg, key.point), ("ES256", es.point))
        message = b"message"
        self.assertTrue(key.verify(message, es.sign(message)))
        ed = SoftEd25519Key()
        key = cose.parse_cose_key(ed.cose())
        self.assertEqual((key.alg, key.raw), ("EdDSA", ed.public))
        self.assertTrue(key.verify(message, ed.sign(message)))
        self.assertFalse(key.verify(message + b"!", ed.sign(message)))

    def test_cles_refusees(self):
        es = SoftES256Key()
        x, y = (v.to_bytes(32, "big") for v in es.point)
        cases = {
            "partie privée": {1: 2, 3: -7, -1: 1, -2: x, -3: y, -4: b"\x01" * 32},
            "RS256": {1: 3, 3: -257, -1: b"n", -2: b"e"},
            "ES384": {1: 2, 3: -35, -1: 2, -2: x, -3: y},
            "courbe": {1: 2, 3: -7, -1: 2, -2: x, -3: y},
            "y compressé": {1: 2, 3: -7, -1: 1, -2: x, -3: True},
            "hors courbe": {1: 2, 3: -7, -1: 1, -2: x, -3: bytes(32)},
            "x court": {1: 2, 3: -7, -1: 1, -2: x[1:], -3: y},
            "OKP X25519": {1: 1, 3: -8, -1: 4, -2: bytes(32)},
        }
        for label, table in cases.items():
            with self.subTest(label):
                with self.assertRaises(cose.CborError):
                    cose.parse_cose_key(cbor(table))

    def test_cbor_strict(self):
        es = SoftES256Key()
        good = es.cose()
        self.assertEqual(cose.decode(cbor({1: [1, -2, b"x", "é", None, True]}))[0],
                         {1: [1, -2, b"x", "é", None, True]})
        bad = {
            "octets en trop": good + b"\x00",
            "tronqué": good[:-3],
            "clé en double": bytes([0xA2]) + cbor(1) + cbor(2) + cbor(1) + cbor(2),
            "longueur indéfinie": bytes([0x5F, 0x41, 0x00, 0xFF]),
            "étiquette": bytes([0xC2, 0x41, 0x01]),
            "flottant": bytes([0xF9, 0x3C, 0x00]),
            "longueur mensongère": bytes([0x5B]) + (2 ** 40).to_bytes(8, "big"),
            "texte non UTF-8": bytes([0x62, 0xFF, 0xFE]),
        }
        for label, blob in bad.items():
            with self.subTest(label):
                with self.assertRaises(cose.CborError):
                    cose.decode(blob)
        deep = b"\x81" * 40 + b"\x00"
        with self.assertRaises(cose.CborError):
            cose.decode(deep)


if __name__ == "__main__":
    unittest.main()
