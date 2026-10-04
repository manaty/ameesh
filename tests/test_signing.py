# SPDX-License-Identifier: AGPL-3.0-only
"""Ed25519 : vecteurs RFC 8032, falsifications, backends, clés sur disque.

La clé de test est éphémère et locale au test. La clé réelle du propriétaire
n'est jamais générée par un agent : voir docs/BASCULE.md.
"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import signing

# RFC 8032 §7.1 : (germe, clé publique, message, signature)
RFC_VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ("833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
     "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
     "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
     "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
]


class SigningTest(unittest.TestCase):
    def test_backends_disponibles(self):
        available = signing.available_backends()
        self.assertIn("pure", available)
        for name in available:
            self.assertEqual(signing.backend(name).name, name)

    def test_backend_inconnu(self):
        with self.assertRaises(ValueError):
            signing.backend("maison")

    def test_vecteurs_rfc8032_sur_tous_les_backends(self):
        for name in signing.available_backends():
            backend = signing.backend(name)
            for seed_hex, public_hex, message_hex, signature_hex in RFC_VECTORS:
                with self.subTest(backend=name, public=public_hex[:12]):
                    seed = bytes.fromhex(seed_hex)
                    public = bytes.fromhex(public_hex)
                    message = bytes.fromhex(message_hex)
                    expected = bytes.fromhex(signature_hex)
                    self.assertEqual(backend.public_from_seed(seed), public)
                    self.assertEqual(backend.sign(seed, message), expected)
                    self.assertTrue(backend.verify(public, message, expected))

    def test_falsifications_refusees(self):
        backend = signing.backend("pure")
        seed = bytes.fromhex(RFC_VECTORS[0][0])
        public = backend.public_from_seed(seed)
        signature = backend.sign(seed, b"message")
        self.assertTrue(backend.verify(public, b"message", signature))
        self.assertFalse(backend.verify(public, b"message!", signature))
        self.assertFalse(backend.verify(public, b"message", signature[:-1] + bytes([signature[-1] ^ 1])))
        self.assertFalse(backend.verify(public, b"message", b"court"))
        self.assertFalse(backend.verify(b"\x00" * 32, b"message", signature))
        # S ≥ L : signature non canonique refusée
        s = int.from_bytes(signature[32:], "little") + signing._L
        if s < 2 ** 256:
            self.assertFalse(backend.verify(
                public, b"message", signature[:32] + s.to_bytes(32, "little")))

    def test_croisement_des_backends(self):
        """Une signature produite par un backend doit vérifier sur les autres."""
        names = signing.available_backends()
        if len(names) < 2:
            self.skipTest("un seul backend disponible (%s)" % names)
        seed = bytes(range(32))
        message = b"croisement agent-mesh"
        for signer in names:
            signature = signing.backend(signer).sign(seed, message)
            for verifier in names:
                with self.subTest(signer=signer, verifier=verifier):
                    self.assertTrue(signing.backend(verifier).verify(
                        signing.backend(signer).public_from_seed(seed), message, signature))

    def test_croisement_openssl(self):
        """OpenSSL est un vérificateur indépendant de notre implémentation."""
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl absent")
        seed = bytes(range(32))
        backend = signing.backend("pure")
        public = backend.public_from_seed(seed)
        message = b"agent-mesh/openssl"
        signature = backend.sign(seed, message)
        with tempfile.TemporaryDirectory() as directory:
            spki = bytes.fromhex("302a300506032b6570032100") + public
            pkcs8 = bytes.fromhex("302e020100300506032b657004220420") + seed
            paths = {name: os.path.join(directory, name) for name in
                     ("pub.der", "key.der", "msg.bin", "sig.bin", "sig2.bin")}
            for name, data in (("pub.der", spki), ("key.der", pkcs8),
                               ("msg.bin", message), ("sig.bin", signature)):
                with open(paths[name], "wb") as fh:
                    fh.write(data)
            verifie = subprocess.run(
                [openssl, "pkeyutl", "-verify", "-pubin", "-inkey", paths["pub.der"],
                 "-keyform", "DER", "-rawin", "-in", paths["msg.bin"],
                 "-sigfile", paths["sig.bin"]],
                capture_output=True, text=True)
            self.assertEqual(verifie.returncode, 0, verifie.stderr)
            signe = subprocess.run(
                [openssl, "pkeyutl", "-sign", "-inkey", paths["key.der"], "-keyform", "DER",
                 "-rawin", "-in", paths["msg.bin"], "-out", paths["sig2.bin"]],
                capture_output=True, text=True)
            self.assertEqual(signe.returncode, 0, signe.stderr)
            with open(paths["sig2.bin"], "rb") as fh:
                openssl_signature = fh.read()
            self.assertTrue(backend.verify(public, message, openssl_signature))

    def test_empreinte_stable(self):
        seed = bytes(range(32))
        public = signing.backend("pure").public_from_seed(seed)
        self.assertEqual(signing.fingerprint(public),
                         hashlib.sha256(public).hexdigest())
        self.assertEqual(len(signing.fingerprint(public)), 64)

    def test_cles_sur_disque(self):
        with tempfile.TemporaryDirectory() as directory:
            private_path, public_path, fingerprint = signing.write_keypair(directory, "test")
            self.assertEqual(os.stat(private_path).st_mode & 0o777, 0o600)
            seed = signing.read_private(private_path)
            public = signing.read_public(public_path)
            self.assertEqual(signing.fingerprint(public), fingerprint)
            # la clé publique ne se lit pas comme une privée, et réciproquement
            with open(private_path, encoding="utf-8") as fh:
                private_text = fh.read()
            with open(public_path, encoding="utf-8") as fh:
                public_text = fh.read()
            with self.assertRaises(ValueError):
                signing.decode_public(private_text)
            with self.assertRaises(ValueError):
                signing.decode_private(public_text)
            # refus d'écraser
            with self.assertRaises(FileExistsError):
                signing.write_keypair(directory, "test")

    def test_cle_privee_trop_ouverte_refusee(self):
        with tempfile.TemporaryDirectory() as directory:
            private_path, _public, _fp = signing.write_keypair(directory, "test")
            os.chmod(private_path, 0o644)
            with self.assertRaises(ValueError) as ctx:
                signing.read_private(private_path)
            self.assertIn("chmod 600", str(ctx.exception))

    def test_format_invalide(self):
        with self.assertRaises(ValueError):
            signing.decode_public("n'importe quoi")
        with self.assertRaises(ValueError):
            signing.decode_public(
                "-----BEGIN AGENT-MESH PUBLIC KEY-----\n%s\n"
                "-----END AGENT-MESH PUBLIC KEY-----\n" % base64.b64encode(b"court").decode())
        with self.assertRaises(ValueError):
            signing.expand_seed(b"trop court")


if __name__ == "__main__":
    unittest.main()
