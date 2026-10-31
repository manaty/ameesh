# SPDX-License-Identifier: AGPL-3.0-only
"""#4687 — les transferts S3 nomment l'empreinte du corps, signée par SigV4.

curl 7.88.1 (Debian 12, l'hôte du mesh) ne pose jamais
``x-amz-content-sha256`` seul ; Scaleway refuse alors la requête (HTTP 400
``Required header missing``). Le helper doit donc le fournir pour le PUT (le
SHA-256 du fichier) comme pour le GET (le SHA-256 du corps vide).

Le témoin exécute le vrai curl de la machine contre un faux S3 TLS local qui
reproduit le refus du fournisseur : sans l'en-tête, 400 ; avec, 200 et
``SignedHeaders`` le contient. Il ne contacte aucun service réel.
"""
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy/mise-a-jour/sauvegarder-prive.py"
SPEC = importlib.util.spec_from_file_location("private_backup_4687", HELPER)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)

HOST = "acme.s3.fr-par.scw.cloud"
SECRET = "CANARY-SECRET-4687"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
HAVE_CURL = shutil.which("curl") is not None
HAVE_OPENSSL = shutil.which("openssl") is not None


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self, method):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        expected = hashlib.sha256(body).hexdigest() if method == "PUT" else EMPTY_SHA256
        supplied = self.headers.get("x-amz-content-sha256")
        authorization = self.headers.get("authorization") or ""
        signed = ""
        if "SignedHeaders=" in authorization:
            signed = authorization.split("SignedHeaders=", 1)[1].split(",", 1)[0]
        accepted = supplied == expected and "x-amz-content-sha256" in signed.split(";")
        self.server.requests.append({"method": method, "sha256": supplied,
                                     "signed": signed.split(";"), "accepted": accepted})
        if not accepted:
            payload = (b"<Error><Code>InvalidArgument</Code><Message>"
                       b"Required header missing: X-Amz-Content-Sha256</Message></Error>")
            self.send_response(400)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("x-amz-version-id", "v-4687")
        if method == "GET":
            self.send_header("Content-Length", str(len(self.server.payload)))
            self.end_headers()
            self.wfile.write(self.server.payload)
        else:
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):  # noqa: N802 - imposed by http.server
        self._handle("GET")

    def do_PUT(self):  # noqa: N802 - imposed by http.server
        self._handle("PUT")

    def log_message(self, *args):
        pass


def _certificate(directory):
    cert, key = directory / "cert.pem", directory / "key.pem"
    proc = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "2",
         "-subj", "/CN=" + HOST, "-addext", "subjectAltName=DNS:" + HOST],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise unittest.SkipTest("openssl cannot create the test certificate: %s"
                                % proc.stderr[-200:])
    return cert, key


@unittest.skipUnless(HAVE_CURL, "curl absent")
@unittest.skipUnless(HAVE_OPENSSL, "openssl absent")
class PayloadSha256ContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-4687-")
        self.addCleanup(self.temp.cleanup)
        # #5189 : the retrieved archive is a fake; its majors and the client's
        # are pinned, the version guard has its own contracts.
        for name, value in (("client_major", lambda name, env=None: 17),
                            ("archive_majors", lambda path: (17, 17))):
            patcher = mock.patch.object(backup, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = Path(self.temp.name)
        cert, key = _certificate(self.root)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.server.requests = []
        self.server.payload = b""
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.archive = self.root / "archive.dump"
        self.archive.write_bytes(b"archive-4687-payload")
        self.archive.chmod(0o600)
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def curl(self, args, *, env=None):
        """Run the helper's real curl argv against the local TLS endpoint."""
        if args[0] != "curl":
            return
        self.assertNotIn(SECRET, " ".join(args))
        args = list(args)
        self.assertTrue(args[-1].startswith("https://"))
        args[-1:-1] = ["--insecure", "--connect-to",
                       "%s:443:127.0.0.1:%d" % (HOST, self.port)]
        completed = subprocess.run(args, env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, timeout=60)
        if completed.returncode != 0:
            raise backup.BackupError("curl exited with status %d" % completed.returncode)

    def without_payload_header(self, args, *, env=None):
        """Reproduce curl 7.88's request when the caller omits the header."""
        args = list(args)
        index = args.index("-H")
        del args[index:index + 2]
        return self.curl(args, env=env)

    def test_upload_signs_the_file_digest_and_retrieve_signs_the_empty_body(self):
        credentials = self.root / "recovery.json"
        credentials.write_text(json.dumps({"access_key": "ACCESS", "secret_key": SECRET,
                                           "bucket": "acme", "region": "fr-par"}))
        credentials.chmod(0o600)
        receipt = self.archive.with_suffix(".upload.json")
        environment = {"SCW_ACCESS_KEY": "ACCESS", "SCW_SECRET_KEY": SECRET,
                       "SCW_BUCKET": "acme", "SCW_REGION": "fr-par",
                       "TMPDIR": str(self.root)}
        self.server.payload = self.archive.read_bytes()
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(backup, "run", side_effect=self.curl):
            proof = backup.upload(self.archive, "host-1", receipt=receipt)
            self.assertEqual(proof["sha256"], self.sha)
            with mock.patch.object(backup, "run", side_effect=self.curl):
                retrieved = backup.retrieve(credentials, "host-1", "remote.dump",
                                            self.sha, self.root / "restored.dump")
        self.assertTrue(retrieved["retrieved"])
        self.assertEqual((self.root / "restored.dump").read_bytes(),
                         self.archive.read_bytes())
        upload, download = self.server.requests
        self.assertEqual((upload["method"], upload["sha256"]), ("PUT", self.sha))
        self.assertIn("x-amz-content-sha256", upload["signed"])
        self.assertEqual((download["method"], download["sha256"]), ("GET", EMPTY_SHA256))
        self.assertIn("x-amz-content-sha256", download["signed"])

    def test_a_request_without_the_header_is_refused_like_scaleway(self):
        environment = {"SCW_ACCESS_KEY": "ACCESS", "SCW_SECRET_KEY": SECRET,
                       "SCW_BUCKET": "acme", "SCW_REGION": "fr-par",
                       "TMPDIR": str(self.root)}
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(backup, "run", side_effect=self.without_payload_header):
            with self.assertRaisesRegex(backup.BackupError, "curl exited with status 22"):
                backup.upload(self.archive, "host-1")
        request = self.server.requests[0]
        self.assertEqual(request["method"], "PUT")
        self.assertNotEqual(request["sha256"], self.sha)
        self.assertFalse(request["accepted"])


if __name__ == "__main__":
    unittest.main()
