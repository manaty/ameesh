# SPDX-License-Identifier: AGPL-3.0-only
"""L114 : le point d'entrée de l'image ne laisse jamais voir le code d'enrôlement.

`deploy/image-executeur/ameesh-executor` est lancé avec un faux `ameesh` en
tête du PATH. Ce faux relève, à chaque appel, sa ligne de commande telle que
`/proc/self/cmdline` la montre et son environnement. Le code d'enrôlement ne
doit paraître ni dans une ligne de commande, ni dans un environnement, ni dans
les journaux (stdout et stderr) ; il ne passe que par `--code-file`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(REPO, "deploy", "image-executeur", "ameesh-executor")
CODE = "K7Q2-9XWD-4MZP-HT3V-R8NB-6YCF"
URL = "https://mesh.exemple.org"

FAKE = textwrap.dedent("""\
    #!%(python)s
    import json, os, sys
    with open("/proc/self/cmdline", "rb") as fh:
        cmdline = fh.read().replace(b"\\0", b" ").decode()
    with open(os.environ["FAKE_LOG"], "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "cmdline": cmdline,
                             "env": dict(os.environ)}) + "\\n")
    args = sys.argv[1:]
    def opt(name):
        return args[args.index(name) + 1] if name in args else None
    if args[:2] == ["device", "challenge"]:
        print("ameesh-device-attestation/1")
    elif args[:2] == ["device", "enroll"]:
        code = open(opt("--code-file")).read().strip()
        assert code, "code vide"
        with open(os.path.join(opt("--home"), "executor.json"), "w") as fh:
            json.dump({"executor_id": "0123456789abcdef", "mesh": "m1",
                       "host": "appareil-1", "server_url": opt("--server")}, fh)
    """)


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("sh"),
                     "Linux et sh requis (/proc)")
class EntreeSansCodeVisible(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l114-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        fake = os.path.join(bin_dir, "ameesh")
        with open(fake, "w") as fh:
            fh.write(FAKE % {"python": sys.executable})
        os.chmod(fake, 0o755)
        self.home = os.path.join(self.tmp, "volume")
        self.enroll = os.path.join(self.tmp, "enroll")
        self.ack = os.path.join(self.tmp, "gate", "ack")
        for d in (self.home, self.enroll, self.ack):
            os.makedirs(d)
        with open(os.path.join(self.enroll, "code"), "w") as fh:
            fh.write(CODE + "\n")
        self.log = os.path.join(self.tmp, "appels.jsonl")
        self.env = {"PATH": bin_dir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                    "FAKE_LOG": self.log, "AMEESH_EXEC_URL": URL,
                    "AMEESH_EXEC_HOME": self.home, "AMEESH_EXEC_ENROLL_DIR": self.enroll,
                    "AMEESH_HOST_GATE_ACK": os.path.join(self.ack, "ack.json")}

    def _run(self, **extra):
        env = dict(self.env, **extra)
        proc = subprocess.run(["sh", ENTRY, "run"], env=env, capture_output=True,
                              text=True, timeout=60)
        with open(self.log) as fh:
            calls = [json.loads(line) for line in fh]
        return proc, calls

    def _assert_never_visible(self, proc, calls):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        variants = {CODE, CODE.replace("-", "")}
        for call in calls:
            for v in variants:
                self.assertNotIn(v, call["cmdline"], call["argv"])
                self.assertNotIn(v, " ".join(call["argv"]))
                for name, value in call["env"].items():
                    self.assertNotIn(v, value, "variable %s" % name)
            self.assertNotIn("--code", call["argv"])
        for v in variants:
            self.assertNotIn(v, proc.stdout)
            self.assertNotIn(v, proc.stderr)

    def test_enrolement_par_fichier(self):
        proc, calls = self._run()
        self._assert_never_visible(proc, calls)
        kinds = [c["argv"][:2] for c in calls]
        self.assertEqual(kinds, [["device", "enroll"], ["run"]])
        enroll = calls[0]["argv"]
        self.assertEqual(enroll[enroll.index("--code-file") + 1],
                         os.path.join(self.enroll, "code"))

    def test_enrolement_avec_attestation(self):
        with open(os.path.join(self.enroll, "attestation.json"), "w") as fh:
            json.dump({"schema": "ameesh-device-attestation/1"}, fh)
        proc, calls = self._run(AMEESH_EXEC_ATTEST="1")
        self._assert_never_visible(proc, calls)
        kinds = [c["argv"][:2] for c in calls]
        self.assertEqual(kinds, [["device", "challenge"], ["device", "enroll"], ["run"]])
        self.assertIn("--attestation", calls[1]["argv"])

    def test_deja_enrole_ne_lit_pas_le_code(self):
        with open(os.path.join(self.home, "executor.json"), "w") as fh:
            json.dump({"executor_id": "0123456789abcdef", "mesh": "m1",
                       "host": "appareil-1", "server_url": URL}, fh)
        proc, calls = self._run()
        self._assert_never_visible(proc, calls)
        self.assertEqual([c["argv"][:1] for c in calls], [["run"]])


if __name__ == "__main__":
    unittest.main()
