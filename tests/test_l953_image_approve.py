# SPDX-License-Identifier: AGPL-3.0-only
"""#953 (R5-IMAGE-APPROVE) : l'image approve se publie sous des regles etroites.

Tests sans base ni reseau : ils lisent le workflow, le Containerfile, le script
de publication et la politique ecrite. Les tests de comportement executent
`deploy/approve-image/publish.sh` sur un faux registre (faux `curl` et faux
`docker` dans le PATH) : ils exercent les deux bloquants B1/B2 du rapport de
relecture codex3 (13/10/2026) — une erreur autre qu'un 404 ne vaut jamais une
absence, et le tag de version recopie le manifeste distant au lieu de pousser
une reconstruction locale.

Ils verrouillent aussi ce que l'arbitrage du pilote du 13/10/2026 (corrige par
la consigne du bureau le meme jour) impose : aucune publication avant la
fusion, publication depuis main seulement, runner auto-heberge, permissions
minimales, delai maximal, tag immuable sha-<12>, conteneur non-root sans
secret.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "image-approve.yml"
SCRIPT = REPO / "deploy" / "approve-image" / "publish.sh"
CONTAINERFILE = REPO / "deploy" / "approve-image" / "Containerfile"
POLICY = REPO / "docs" / "IMAGE-APPROVE.md"

IMAGE = "ghcr.io/manaty/ameesh-approve"
SHA_TAG = "sha-0123456789ab"
VERSION = "9.9.9"

# Faux curl : un mini-registre de manifestes, sans reseau ni jeton.
FAKE_CURL = '''#!/usr/bin/env python3
import hashlib
import os
import pathlib
import sys
import urllib.parse

FAKE = pathlib.Path(os.environ["FAKE_DIR"])
REGISTRY = FAKE / "registry"
ERRORS = FAKE / "errors"

method = "GET"
out = dump = wfmt = data = url = None
headers = {}
args = sys.argv[1:]
i = 0
while i < len(args):
    arg = args[i]
    if arg == "-X":
        i += 1
        method = args[i]
    elif arg == "-H":
        i += 1
        name, _, value = args[i].partition(":")
        headers[name.strip().lower()] = value.strip()
    elif arg == "-o":
        i += 1
        out = args[i]
    elif arg == "-D":
        i += 1
        dump = args[i]
    elif arg == "-w":
        i += 1
        wfmt = args[i]
    elif arg == "--data-binary":
        i += 1
        data = args[i][1:] if args[i].startswith("@") else None
    elif arg in ("-u", "--data-urlencode"):
        i += 1
    if not arg.startswith("-"):
        url = arg
    i += 1

with (FAKE / "requests.log").open("a") as journal:
    journal.write(method + " " + url + "\\n")


def respond(code, body=b"", ctype=None, digest=None):
    if dump:
        lines = ["HTTP/1.1 %d" % code]
        if ctype:
            lines.append("Content-Type: " + ctype)
        if digest:
            lines.append("Docker-Content-Digest: " + digest)
        pathlib.Path(dump).write_text("\\r\\n".join(lines) + "\\r\\n\\r\\n")
    if out:
        pathlib.Path(out).write_bytes(body)
    elif body:
        sys.stdout.buffer.write(body)
    if wfmt == "%{http_code}":
        sys.stdout.write(str(code))
    sys.exit(0)


if "/token" in url:
    respond(200, b'{"token": "jeton-de-test"}', "application/json")

if (FAKE / "network-down").exists():
    sys.exit(7)

path = urllib.parse.urlparse(url).path
prefix = "/v2/manaty/ameesh-approve/manifests/"
if not path.startswith(prefix):
    sys.stderr.write("faux curl : URL inattendue " + url + "\\n")
    sys.exit(3)
ref = urllib.parse.unquote(path[len(prefix):])

if (ERRORS / ref).exists():
    respond(int((ERRORS / ref).read_text().strip()))

if ref.startswith("sha256:"):
    for candidate in sorted(REGISTRY.glob("*")):
        if candidate.name.endswith(".type"):
            continue
        body = candidate.read_bytes()
        if "sha256:" + hashlib.sha256(body).hexdigest() == ref:
            respond(200, body,
                    (REGISTRY / (candidate.name + ".type")).read_text(), ref)
    respond(404, b'{"errors": [{"code": "MANIFEST_UNKNOWN"}]}', "application/json")

manifest = REGISTRY / ref
if method == "PUT":
    if not data:
        sys.stderr.write("faux curl : PUT sans corps\\n")
        sys.exit(4)
    body = pathlib.Path(data).read_bytes()
    REGISTRY.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(body)
    (REGISTRY / (ref + ".type")).write_text(headers.get("content-type", ""))
    respond(201, b"", digest="sha256:" + hashlib.sha256(body).hexdigest())

if manifest.exists():
    body = manifest.read_bytes()
    respond(200, body, (REGISTRY / (ref + ".type")).read_text(),
            "sha256:" + hashlib.sha256(body).hexdigest())
respond(404, b'{"errors": [{"code": "MANIFEST_UNKNOWN"}]}', "application/json")
'''

# Faux docker : journalise, et simule le push du manifeste de l'image locale.
FAKE_DOCKER = '''#!/usr/bin/env python3
import os
import pathlib
import sys

FAKE = pathlib.Path(os.environ["FAKE_DIR"])
with (FAKE / "docker.log").open("a") as journal:
    journal.write("docker " + " ".join(sys.argv[1:]) + "\\n")

if "--password-stdin" in sys.argv[1:]:
    sys.stdin.read()

if len(sys.argv) > 2 and sys.argv[1] == "push":
    tag = sys.argv[2].split(":", 1)[1]
    local = FAKE / "local-manifest"
    body = local.read_bytes() if local.exists() else b'{"image": "locale"}'
    registry = FAKE / "registry"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / tag).write_bytes(body)
    (registry / (tag + ".type")).write_text(
        "application/vnd.oci.image.manifest.v1+json")
sys.exit(0)
'''


class WorkflowTest(unittest.TestCase):
    """Declencheurs etroits, droits minimaux, publication deleguee au script."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_aucun_declencheur_de_pr_ni_runner_heberge(self):
        self.assertNotIn("pull_request", self.text)
        # canon §3 : nos runners auto-heberges, aucun runner heberge payant
        for heberge in ("ubuntu-", "macos-", "windows-"):
            self.assertNotIn(heberge, self.text)

    def test_aucune_publication_avant_la_fusion(self):
        # le seul declencheur est le dispatch, et la tete refuse tout sauf main
        self.assertIn("on:\n  workflow_dispatch:\n", self.text)
        self.assertNotIn("\n  push:", self.text)
        self.assertNotIn("lot/953-image-approve", self.text)
        self.assertIn("refs/heads/main)", self.text)
        self.assertNotIn("refs/heads/lot", self.text)

    def test_fourche_ignoree(self):
        self.assertIn("github.repository == 'manaty/ameesh'", self.text)

    def test_runner_auto_heberge(self):
        self.assertIn("runs-on: [self-hosted, linux, ameesh-ci]", self.text)

    def test_permissions_minimales(self):
        self.assertIn("contents: read", self.text)
        self.assertIn("packages: write", self.text)
        self.assertNotIn("contents: write", self.text)
        # le jeton du depot, jamais un jeton personnel
        self.assertIn("secrets.GITHUB_TOKEN", self.text)

    def test_chaque_job_declare_un_delai(self):
        self.assertEqual(self.text.count("runs-on:"),
                         self.text.count("timeout-minutes:"))

    def test_concurrence_par_image_pas_par_ref(self):
        # B1 (codex3) : deux refs publiant la meme image se serialisent ; la
        # cle ne depend pas de la ref (sinon deux refs publieraient en parallele)
        groupe = re.search(r"^  group: (\S+)$", self.text, re.MULTILINE)
        self.assertIsNotNone(groupe)
        self.assertEqual(groupe.group(1), "image-approve-publish")
        self.assertNotIn("${{", groupe.group(1))
        self.assertIn("cancel-in-progress: false", self.text)

    def test_tag_immuable_sha12(self):
        self.assertIn("cut -c1-12", self.text)
        self.assertIn("sha-${{ steps.meta.outputs.sha12 }}", self.text)
        # B2 (codex3) : la version n'est jamais un tag local, donc jamais poussee
        self.assertNotIn('"$IMAGE:${{ steps.meta.outputs.version }}"', self.text)

    def test_publication_dans_le_script(self):
        self.assertIn("run: bash deploy/approve-image/publish.sh", self.text)
        self.assertNotIn("docker push", self.text)
        self.assertNotIn("docker manifest inspect", self.text)


class ScriptInvariantsTest(unittest.TestCase):
    """Le script de publication, verrouille a la lecture."""

    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8")

    def test_un_seul_push_et_c_est_le_sha(self):
        self.assertEqual(self.text.count("docker push "), 1)
        self.assertIn('docker push "$IMAGE:$SHA_TAG"', self.text)
        self.assertNotIn("docker push \"$IMAGE:$VERSION\"", self.text)

    def test_absence_prouvee_par_un_404(self):
        self.assertIn("404) LOOKUP_STATE=absent", self.text)
        self.assertIn("erreur autre qu'une absence prouvee", self.text)
        self.assertNotIn("docker manifest inspect", self.text)

    def test_promotion_du_manifeste_distant(self):
        # B2 : la version est un PUT des octets du manifeste sha, pas un push
        self.assertIn('"$api/manifests/$VERSION"', self.text)
        self.assertIn("--data-binary @", self.text)
        self.assertIn("promotion du manifeste distant", self.text)


class PublishBehaviourTest(unittest.TestCase):
    """B1/B2 exerces sur un faux registre : aucune suite, aucun reseau."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = pathlib.Path(self.tmp.name) / "fake"
        (self.fake / "bin").mkdir(parents=True)
        (self.fake / "registry").mkdir()
        (self.fake / "errors").mkdir()
        self._bin("curl", FAKE_CURL)
        self._bin("docker", FAKE_DOCKER)
        self.env = dict(os.environ)
        self.env.pop("GITHUB_STEP_SUMMARY", None)
        self.env.pop("RUNNER_TEMP", None)
        self.env.update({
            "PATH": str(self.fake / "bin") + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_DIR": str(self.fake),
            "IMAGE": IMAGE,
            "SHA_TAG": SHA_TAG,
            "VERSION": VERSION,
            "GHCR_USER": "x-access-token",
            "GHCR_TOKEN": "jeton-de-test",
        })

    def _bin(self, name, code):
        path = self.fake / "bin" / name
        path.write_text(code, encoding="utf-8")
        path.chmod(0o755)

    def _put(self, ref, body, ctype="application/vnd.oci.image.manifest.v1+json"):
        (self.fake / "registry" / ref).write_bytes(body)
        (self.fake / "registry" / (ref + ".type")).write_text(ctype,
                                                              encoding="utf-8")

    def _manifest(self, ref):
        return (self.fake / "registry" / ref).read_bytes()

    def _digest(self, ref):
        return "sha256:" + hashlib.sha256(self._manifest(ref)).hexdigest()

    def _fail(self, ref, code):
        (self.fake / "errors" / ref).write_text(str(code), encoding="utf-8")

    def _run(self):
        return subprocess.run(["bash", str(SCRIPT)], cwd=str(self.fake),
                              env=self.env, capture_output=True, text=True,
                              timeout=120)

    def _pushed(self):
        log = self.fake / "docker.log"
        if not log.exists():
            return []
        return [ligne for ligne in log.read_text(encoding="utf-8").splitlines()
                if " push " in ligne]

    def _puts(self):
        log = self.fake / "requests.log"
        if not log.exists():
            return []
        return [ligne for ligne in log.read_text(encoding="utf-8").splitlines()
                if ligne.startswith("PUT ")]

    def test_une_erreur_5xx_ne_peut_pas_ecraser_un_tag_existant(self):
        # temoin B1 : le tag existe, le lookup echoue transitoirement -> rien
        # n'est pousse, le digest existant ne bouge pas
        d1 = b'{"image": "D1"}'
        self._put(SHA_TAG, d1)
        self._fail(SHA_TAG, 500)
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("autre qu'une absence prouvee", proc.stderr)
        self.assertEqual(self._pushed(), [])
        self.assertEqual(self._puts(), [])
        self.assertEqual(self._manifest(SHA_TAG), d1)

    def test_un_refus_d_acces_n_est_pas_une_absence(self):
        self._fail(SHA_TAG, 401)
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self._pushed(), [])
        self.assertEqual(self._puts(), [])
        self.assertFalse((self.fake / "registry" / VERSION).exists())

    def test_un_registre_injoignable_n_ecrit_rien(self):
        d1 = b'{"image": "D1"}'
        self._put(SHA_TAG, d1)
        (self.fake / "network-down").write_text("", encoding="utf-8")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("injoignable", proc.stderr)
        self.assertEqual(self._pushed(), [])
        self.assertEqual(self._puts(), [])
        self.assertEqual(self._manifest(SHA_TAG), d1)

    def test_un_refus_sur_la_version_n_ecrit_rien(self):
        self._put(SHA_TAG, b'{"image": "D1"}')
        self._fail(VERSION, 403)
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self._puts(), [])
        self.assertEqual(self._pushed(), [])
        self.assertFalse((self.fake / "registry" / VERSION).exists())

    def test_sha_absent_publie_sha_puis_promeut_la_version(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pushes = self._pushed()
        self.assertEqual(len(pushes), 1)
        self.assertIn(":" + SHA_TAG, pushes[0])
        self.assertEqual(self._digest(VERSION), self._digest(SHA_TAG))
        self.assertIn("digest=" + self._digest(SHA_TAG), proc.stdout)
        self.assertTrue((self.fake / "digest.txt").exists())

    def test_sha_distant_d1_jamais_repousse_et_version_promue_d1(self):
        # temoin attendu par codex3 : SHA distant D1, build local D2, version
        # absente -> la version vaut D1, D2 n'est jamais publie
        d1 = b'{"image": "D1"}'
        self._put(SHA_TAG, d1)
        (self.fake / "local-manifest").write_bytes(b'{"image": "D2"}')
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._pushed(), [])
        self.assertEqual(self._manifest(SHA_TAG), d1)
        self.assertEqual(self._manifest(VERSION), d1)
        self.assertEqual(self._digest(VERSION), self._digest(SHA_TAG))

    def test_version_deja_publiee_sur_un_autre_digest_refusee_sans_ecriture(self):
        self._put(SHA_TAG, b'{"image": "D1"}')
        self._put(VERSION, b'{"image": "D0"}')
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("incrementer la version", proc.stderr)
        self.assertEqual(self._manifest(VERSION), b'{"image": "D0"}')
        self.assertEqual(self._puts(), [])
        self.assertEqual(self._pushed(), [])

    def test_version_deja_publiee_avec_le_meme_digest_est_idempotente(self):
        self._put(SHA_TAG, b'{"image": "D1"}')
        self._put(VERSION, b'{"image": "D1"}')
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._puts(), [])
        self.assertEqual(self._pushed(), [])

    def test_tag_de_revision_inattendu_refuse_avant_reseau(self):
        self.env["SHA_TAG"] = "latest"
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self._pushed(), [])
        self.assertFalse((self.fake / "requests.log").exists())


class ContainerfileTest(unittest.TestCase):
    """Le conteneur : non-root, sans secret, contrat du service explicite."""

    @classmethod
    def setUpClass(cls):
        cls.text = CONTAINERFILE.read_text(encoding="utf-8")

    def test_non_root_et_entree(self):
        self.assertIn("USER 10002:10002", self.text)
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--", "ameesh-approve"]',
                      self.text)
        self.assertIn('CMD ["serve"]', self.text)

    def test_aucun_secret_dans_l_image(self):
        self.assertIn("AUCUN SECRET", self.text)
        # seules les instructions comptent : un commentaire peut nommer le DSN
        corps = "\n".join(ligne for ligne in self.text.splitlines()
                          if not ligne.lstrip().startswith("#"))
        for interdit in ("AMEESH_DSN", "PASSWORD", "TOKEN="):
            self.assertNotIn(interdit, corps)

    def test_contrat_du_service(self):
        self.assertIn("AMEESH_APPROVE_CONFIG=/etc/ameesh-approve/config.json",
                      self.text)
        self.assertIn("AMEESH_APPROVE_STATE=/var/lib/ameesh-approve", self.text)
        self.assertIn("AMEESH_APPROVE_BIND=127.0.0.1", self.text)
        self.assertIn("EXPOSE 8765", self.text)
        self.assertIn(
            'org.opencontainers.image.source="https://github.com/manaty/ameesh"',
            self.text)

    def test_construite_depuis_la_source(self):
        self.assertIn("COPY src ./src", self.text)
        self.assertIn("pip wheel", self.text)


class PolicyTest(unittest.TestCase):
    """La politique est ecrite : registre, tags, digest, retour arriere."""

    def test_politique_ecrite(self):
        text = POLICY.read_text(encoding="utf-8")
        for attendu in ("ghcr.io/manaty/ameesh-approve", "sha-<12>", "digest",
                        "Retour arrière", "packages: write", "ghcr",
                        "deploy/approve-image/publish.sh", "404",
                        "promotion", "concurrence", "après la fusion"):
            self.assertIn(attendu, text)


if __name__ == "__main__":
    unittest.main()
