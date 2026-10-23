# SPDX-License-Identifier: AGPL-3.0-only
"""#3166 : l'image ameesh-executor se construit et se publie sous des règles étroites.

Tests sans base, sans réseau et sans Docker réel : ils lisent le workflow, le
Containerfile, le script de publication et la politique écrite. Les tests de
comportement exécutent `deploy/executor-image/publish.sh` sur un faux registre
(faux `curl` et faux `docker` dans le PATH) et exercent les invariants qui
comptent pour la signature O4 du bureau :

  - une erreur autre qu'un 404 ne vaut jamais une absence : le tag immuable
    n'est jamais réécrit sur un lookup transitoirement en échec ;
  - le digest rendu est celui d'un manifeste simple `linux/amd64` : un index
    multi-architecture ou une autre architecture arrête le run avant de
    conclure, parce que c'est cette valeur `@sha256` que le bureau signe
    (docs/IMAGE-EXECUTEUR.md §9) ;
  - les jetons (GHCR, bearer) passent par la configuration lue sur stdin
    (`-K -`), jamais par argv : sur l'hôte ameesh-ci, plusieurs runners
    partagent l'utilisateur et /proc exposerait un secret en ligne de commande.

Ils verrouillent aussi la construction : image Node épinglée par digest,
arbre dsh figé par `package-lock.json`, `npm ci`, conteneur non-root sans
secret.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "image-executor.yml"
SCRIPT = REPO / "deploy" / "executor-image" / "publish.sh"
CONTAINERFILE = REPO / "deploy" / "executor-image" / "Containerfile"
PACKAGE = REPO / "deploy" / "executor-image" / "package.json"
LOCKFILE = REPO / "deploy" / "executor-image" / "package-lock.json"
POLICY = REPO / "docs" / "IMAGE-EXECUTEUR.md"

IMAGE = "ghcr.io/manaty/ameesh-executor"
SHA_TAG = "sha-0123456789ab"
VERSION = "9.9.9"
REVISION = "a708792e61d14a9cf15b69610bb006932ae19e15"
TOKEN = "canari-ghcr-3166"      # jeton GHCR injecté : jamais en argv
BEARER = "canari-bearer-3166"   # bearer rendu par le faux registre

# Faux curl : un mini-registre de manifestes et de blobs, sans réseau ni jeton.
FAKE_CURL = '''#!/usr/bin/env python3
import hashlib
import os
import pathlib
import sys
import urllib.parse

FAKE = pathlib.Path(os.environ["FAKE_DIR"])
REGISTRY = FAKE / "registry"
ERRORS = FAKE / "errors"
BLOBS = FAKE / "blobs"

method = "GET"
out = dump = wfmt = data = url = None
user = None
headers = {}
args = sys.argv[1:]

with (FAKE / "curl.log").open("a") as journal:
    journal.write(" ".join(args) + "\\n")

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
    elif arg == "-K":
        i += 1
        if args[i] != "-":
            sys.stderr.write("faux curl : config seulement sur stdin\\n")
            sys.exit(8)
        for ligne in sys.stdin.read().splitlines():
            cle, _, valeur = ligne.partition("=")
            valeur = valeur.strip()
            if len(valeur) >= 2 and valeur[0] == '"' and valeur[-1] == '"':
                valeur = valeur[1:-1]
            if cle.strip().lower() == "user":
                user = valeur
            elif cle.strip().lower() == "header":
                name, _, value = valeur.partition(":")
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
    elif arg == "--data-urlencode":
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
    if not user:
        sys.stderr.write("faux curl : echange de jeton sans identifiants\\n")
        sys.exit(5)
    respond(200, b'{"token": "canari-bearer-3166"}', "application/json")

if (FAKE / "network-down").exists():
    sys.exit(7)

path = urllib.parse.urlparse(url).path
blob_prefix = "/v2/manaty/ameesh-executor/blobs/"
manifest_prefix = "/v2/manaty/ameesh-executor/manifests/"
if not path.startswith(blob_prefix) and not path.startswith(manifest_prefix):
    sys.stderr.write("faux curl : URL inattendue " + url + "\\n")
    sys.exit(3)

if not headers.get("authorization"):
    sys.stderr.write("faux curl : requete registre sans bearer\\n")
    sys.exit(6)

if path.startswith(blob_prefix):
    ref = urllib.parse.unquote(path[len(blob_prefix):])
    if (ERRORS / ref).exists():
        respond(int((ERRORS / ref).read_text().strip()))
    blob = BLOBS / ref
    if blob.exists():
        respond(200, blob.read_bytes(), "application/octet-stream")
    respond(404, b'{"errors": [{"code": "BLOB_UNKNOWN"}]}', "application/json")

ref = urllib.parse.unquote(path[len(manifest_prefix):])

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

# Faux docker : journalise, répond à `image inspect`, et simule le push d'un
# manifeste simple dont le blob de configuration porte os/architecture.
FAKE_DOCKER = '''#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import sys

FAKE = pathlib.Path(os.environ["FAKE_DIR"])
with (FAKE / "docker.log").open("a") as journal:
    journal.write("docker " + " ".join(sys.argv[1:]) + "\\n")
with (FAKE / "docker-config.log").open("a") as journal:
    journal.write(os.environ.get("DOCKER_CONFIG", "") + "\\n")

if "--password-stdin" in sys.argv[1:]:
    sys.stdin.read()


def config_bytes():
    if (FAKE / "config-body").exists():
        return (FAKE / "config-body").read_bytes()
    revision = os.environ.get("FAKE_REVISION", "")
    return json.dumps({
        "os": "linux",
        "architecture": "amd64",
        "config": {"Labels": {"org.opencontainers.image.revision": revision}},
        "rootfs": {"type": "layers", "diff_ids": []},
    }).encode()


def config_digest():
    return "sha256:" + hashlib.sha256(config_bytes()).hexdigest()


args = sys.argv[1:]
if args[:2] == ["image", "inspect"]:
    if (FAKE / "no-local-image").exists():
        sys.exit(1)
    if "{{.Id}}" in args:
        if (FAKE / "config-id").exists():
            print((FAKE / "config-id").read_text().strip())
        else:
            print(config_digest())
        sys.exit(0)
    platform = "linux/amd64"
    if (FAKE / "local-platform").exists():
        platform = (FAKE / "local-platform").read_text().strip()
    print(platform)
    sys.exit(0)

if args[:1] == ["push"]:
    image = args[1]
    tag = image.split(":", 1)[1]
    config = config_bytes()
    digest = config_digest()
    blobs = FAKE / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    (blobs / digest).write_bytes(config)
    manifest = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                   "digest": digest, "size": len(config)},
        "layers": [],
    }).encode()
    registry = FAKE / "registry"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / tag).write_bytes(manifest)
    (registry / (tag + ".type")).write_text(
        "application/vnd.oci.image.manifest.v1+json")
    if (FAKE / "push-tamper").exists():
        # un autre manifeste prend le tag : le digest imprime reste celui du
        # vrai push, le lookup doit refuser l'ecart.
        tampered = json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                       "digest": "sha256:" + "1" * 64, "size": 1},
            "layers": [],
        }).encode()
        (registry / tag).write_bytes(tampered)
    print("%s: digest: sha256:%s size: %d" % (
        tag, hashlib.sha256(manifest).hexdigest(), len(manifest)))
sys.exit(0)
'''


class WorkflowTest(unittest.TestCase):
    """Déclencheurs étroits, droits minimaux, publication déléguée au script."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_aucun_declencheur_de_pr_ni_runner_heberge(self):
        self.assertNotIn("pull_request", self.text)
        for heberge in ("ubuntu-", "macos-", "windows-"):
            self.assertNotIn(heberge, self.text)

    def test_aucune_publication_avant_la_fusion(self):
        self.assertIn("on:\n  workflow_dispatch:\n", self.text)
        self.assertNotIn("\n  push:", self.text)
        self.assertNotIn("lot/3166-executor-image", self.text)
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
        self.assertIn("secrets.GITHUB_TOKEN", self.text)

    def test_chaque_job_declare_un_delai(self):
        self.assertEqual(self.text.count("runs-on:"),
                         self.text.count("timeout-minutes:"))

    def test_concurrence_par_image_pas_par_ref(self):
        groupe = re.search(r"^  group: (\S+)$", self.text, re.MULTILINE)
        self.assertIsNotNone(groupe)
        self.assertEqual(groupe.group(1), "image-executor-publish")
        self.assertNotIn("${{", groupe.group(1))
        self.assertIn("cancel-in-progress: false", self.text)

    def test_tag_immuable_sha12_et_digest(self):
        self.assertIn("cut -c1-12", self.text)
        self.assertIn("sha-${{ steps.meta.outputs.sha12 }}", self.text)
        self.assertIn("PLATFORM: linux/amd64", self.text)
        self.assertIn("docker build --platform linux/amd64", self.text)

    def test_publication_dans_le_script(self):
        self.assertIn("run: bash deploy/executor-image/publish.sh", self.text)
        self.assertNotIn("docker push", self.text)
        self.assertNotIn("docker manifest inspect", self.text)


class ScriptInvariantsTest(unittest.TestCase):
    """Le script de publication, verrouillé à la lecture."""

    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8")

    def test_un_seul_push_et_c_est_le_sha(self):
        self.assertEqual(self.text.count('docker push "$IMAGE:$SHA_TAG"'), 1)
        self.assertIn('docker push "$IMAGE:$SHA_TAG" | tee', self.text)

    def test_absence_prouvee_par_un_404(self):
        self.assertIn("404) LOOKUP_STATE=absent", self.text)
        self.assertIn("erreur autre qu'une absence prouvée", self.text)
        self.assertNotIn("docker manifest inspect", self.text)

    def test_digest_du_manifeste_linux_amd64(self):
        # c'est ce @sha256 que le bureau signe (O4) : il doit être un manifeste
        # simple de la bonne architecture, jamais un index multi-architecture.
        self.assertIn("image inspect --format '{{.Os}}/{{.Architecture}}'",
                      self.text)
        self.assertIn("application/vnd.oci.image.index.v1+json", self.text)
        self.assertIn("manifeste $PLATFORM", self.text)
        self.assertIn("verify_manifest", self.text)
        self.assertIn("blobs/$config", self.text)
        self.assertIn('image_ref="$IMAGE@$digest"', self.text)
        self.assertIn("org.opencontainers.image.revision", self.text)

    def test_le_digest_signe_est_lie_au_push_de_ce_run(self):
        # B2 (auditeur, 23/10/2026) : le digest rendu est celui du push, pas
        # celui qu'un acteur aurait pose sur le tag entre le push et la relecture.
        self.assertIn(r"digest: \(sha256:[0-9a-f]\{64\}\)", self.text)
        self.assertIn("$work/push.log", self.text)
        self.assertIn('"$LOOKUP_DIGEST" = "$pushed"', self.text)
        self.assertIn("docker image inspect --format '{{.Id}}'", self.text)
        self.assertIn('"$MANIFEST_CONFIG" = "$local_config"', self.text)

    def test_config_docker_privee(self):
        # B1 (auditeur, 23/10/2026) : jamais le ~/.docker partage des runners.
        self.assertIn('export DOCKER_CONFIG="$work/docker"', self.text)
        self.assertIn('mkdir -m 700 "$DOCKER_CONFIG"', self.text)

    def test_les_secrets_ne_passent_pas_en_argv(self):
        self.assertNotIn('-u "$GHCR_USER:$GHCR_TOKEN"', self.text)
        self.assertNotIn('-H "Authorization: Bearer $token"', self.text)
        # trois appels réseau (jeton, lookup, blob) lisent la config stdin
        self.assertGreaterEqual(len(re.findall(r"\|\s*curl\b[^\n]*-K -", self.text)), 3)
        self.assertIn("config_value", self.text)
        self.assertIn("--password-stdin", self.text)


class PublishBehaviourTest(unittest.TestCase):
    """Les invariants sont exercés sur un faux registre : aucune suite, aucun réseau."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = pathlib.Path(self.tmp.name) / "fake"
        (self.fake / "bin").mkdir(parents=True)
        (self.fake / "registry").mkdir()
        (self.fake / "errors").mkdir()
        (self.fake / "blobs").mkdir()
        (self.fake / "home").mkdir()
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
            "REVISION": REVISION,
            "FAKE_REVISION": REVISION,
            "HOME": str(self.fake / "home"),
            "GHCR_USER": "x-access-token",
            "GHCR_TOKEN": TOKEN,
        })

    def _bin(self, name, code):
        path = self.fake / "bin" / name
        path.write_text(code, encoding="utf-8")
        path.chmod(0o755)

    def _put(self, ref, body, ctype="application/vnd.oci.image.manifest.v1+json"):
        (self.fake / "registry" / ref).write_bytes(body)
        (self.fake / "registry" / (ref + ".type")).write_text(ctype,
                                                              encoding="utf-8")

    def _put_valid(self, ref, config=None):
        config = config or json.dumps({
            "os": "linux", "architecture": "amd64",
            "config": {"Labels": {"org.opencontainers.image.revision": REVISION}},
        }).encode()
        config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
        (self.fake / "blobs" / config_digest).write_bytes(config)
        manifest = json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                       "digest": config_digest, "size": len(config)},
            "layers": [],
        }).encode()
        self._put(ref, manifest)
        return manifest

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

    def test_une_erreur_5xx_ne_peut_pas_ecraser_un_tag_existant(self):
        d1 = b'{"image": "D1"}'
        self._put(SHA_TAG, d1)
        self._fail(SHA_TAG, 500)
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("autre qu'une absence prouvée", proc.stderr)
        self.assertEqual(self._pushed(), [])
        self.assertEqual(self._manifest(SHA_TAG), d1)

    def test_un_refus_d_acces_n_est_pas_une_absence(self):
        self._fail(SHA_TAG, 401)
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self._pushed(), [])
        self.assertFalse((self.fake / "registry" / SHA_TAG).exists())

    def test_un_registre_injoignable_n_ecrit_rien(self):
        d1 = b'{"image": "D1"}'
        self._put(SHA_TAG, d1)
        (self.fake / "network-down").write_text("", encoding="utf-8")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("injoignable", proc.stderr)
        self.assertEqual(self._pushed(), [])
        self.assertEqual(self._manifest(SHA_TAG), d1)

    def test_sha_absent_publie_le_sha_et_rend_le_digest_amd64(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pushes = self._pushed()
        self.assertEqual(len(pushes), 1)
        self.assertIn(":" + SHA_TAG, pushes[0])
        digest = self._digest(SHA_TAG)
        self.assertIn("digest=" + digest, proc.stdout)
        self.assertIn("image_ref=" + IMAGE + "@" + digest, proc.stdout)
        self.assertIn("platform=linux/amd64", proc.stdout)
        self.assertTrue((self.fake / "digest.txt").exists())
        ecrit = (self.fake / "digest.txt").read_text(encoding="utf-8")
        for ligne in ecrit.splitlines():
            self.assertIn(ligne, proc.stdout)

    def test_sha_deja_publie_jamais_repousse(self):
        manifest = self._put_valid(SHA_TAG)
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._pushed(), [])
        self.assertEqual(self._manifest(SHA_TAG), manifest)
        self.assertIn("déjà publié", proc.stdout)
        self.assertIn("digest=" + digest, proc.stdout)

    def test_le_tag_change_apres_le_push_est_refuse(self):
        # B2 (auditeur, 23/10/2026) : le digest du push et le manifeste relu
        # doivent etre le meme ; sinon rien ne doit etre propose a la signature.
        (self.fake / "push-tamper").write_text("", encoding="utf-8")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("un autre manifeste a pris le tag", proc.stderr)
        self.assertFalse((self.fake / "digest.txt").exists())

    def test_le_manifeste_servi_doit_etre_la_config_du_build(self):
        (self.fake / "config-id").write_text("sha256:" + "2" * 64, encoding="utf-8")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ce n'est pas le manifeste de ce build", proc.stderr)
        self.assertFalse((self.fake / "digest.txt").exists())

    def test_revision_servie_differente_refusee(self):
        config = json.dumps({
            "os": "linux", "architecture": "amd64",
            "config": {"Labels": {"org.opencontainers.image.revision": "deadbeef"}},
        }).encode()
        self._put_valid(SHA_TAG, config)
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("révision 'deadbeef'", proc.stderr)
        self.assertEqual(self._pushed(), [])

    def test_config_docker_privee_pas_dans_le_home(self):
        # B1 (auditeur, 23/10/2026) : le login ecrit dans $work/docker, jamais
        # dans le ~/.docker partage de l'utilisateur des runners.
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lignes = [ligne for ligne in
                  (self.fake / "docker-config.log").read_text(encoding="utf-8").splitlines()
                  if ligne]
        self.assertTrue(lignes)
        for ligne in lignes:
            self.assertTrue(ligne.endswith("/docker"), ligne)
            self.assertIn("ameesh-executor-publish", ligne)
            self.assertNotIn(str(self.fake / "home"), ligne)
        # le dossier $work (donc la configuration docker) est effacé à la sortie
        self.assertFalse(pathlib.Path(lignes[0]).exists())
        self.assertFalse((self.fake / "home" / ".docker").exists())

    def test_une_autre_architecture_locale_est_refusee_avant_reseau(self):
        (self.fake / "local-platform").write_text("linux/arm64", encoding="utf-8")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("linux/amd64 attendu", proc.stderr)
        self.assertFalse((self.fake / "requests.log").exists())
        self.assertEqual(self._pushed(), [])

    def test_un_index_multi_architecture_est_refuse(self):
        index = json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                           "digest": "sha256:" + "0" * 64,
                           "size": 1,
                           "platform": {"os": "linux", "architecture": "amd64"}}],
        }).encode()
        self._put(SHA_TAG, index, "application/vnd.oci.image.index.v1+json")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("index multi-architecture", proc.stderr)
        self.assertEqual(self._pushed(), [])

    def test_une_autre_architecture_servie_est_refusee(self):
        config = b'{"os":"linux","architecture":"arm64"}'
        config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
        (self.fake / "blobs" / config_digest).write_bytes(config)
        manifest = json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                       "digest": config_digest, "size": len(config)},
            "layers": [],
        }).encode()
        self._put(SHA_TAG, manifest)
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("linux/arm64", proc.stderr)
        self.assertEqual(self._pushed(), [])

    def test_tag_de_revision_inattendu_refuse_avant_reseau(self):
        self.env["SHA_TAG"] = "latest"
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self._pushed(), [])
        self.assertFalse((self.fake / "requests.log").exists())

    def test_aucun_secret_dans_argv_des_curl(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = (self.fake / "curl.log").read_text(encoding="utf-8")
        self.assertIn("-K -", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(BEARER, log)


class ContainerfileTest(unittest.TestCase):
    """Le conteneur : non-root, sans secret, dsh et base épinglés."""

    @classmethod
    def setUpClass(cls):
        cls.text = CONTAINERFILE.read_text(encoding="utf-8")

    def test_non_root_et_entree(self):
        self.assertIn("USER 10001:10001", self.text)
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/ameesh-executor"]',
                      self.text)
        self.assertIn('CMD ["run"]', self.text)
        self.assertIn('HEALTHCHECK', self.text)

    def test_aucun_secret_dans_l_image(self):
        self.assertIn("AUCUN SECRET", self.text)
        corps = "\n".join(ligne for ligne in self.text.splitlines()
                          if not ligne.lstrip().startswith("#"))
        for interdit in ("AMEESH_DSN", "AMEESH_EXEC_TOKEN",
                         "AMEESH_RELEASE_PUBLIC_KEY", "PASSWORD", "TOKEN="):
            self.assertNotIn(interdit, corps)

    def test_base_et_dsh_epingles(self):
        self.assertIn("node:22-bookworm-slim@sha256:", self.text)
        self.assertIn("npm ci --omit=dev", self.text)
        # N de la relecture auditeur : aucun script d'installation npm ne
        # s'exécute dans l'image ; dsh 0.2.0-rc.2 fonctionne sans (vérifié).
        self.assertIn("--ignore-scripts", self.text)
        self.assertIn("package-lock.json", self.text)
        self.assertIn("DSH_VERSION=0.2.0-rc.2", self.text)
        # plus de binaire dsh fourni au contexte : l'arbre npm est le verrou
        self.assertNotIn("COPY deploy/executor-image/dsh", self.text)

    def test_contrat_de_l_image(self):
        for attendu in ("AMEESH_EXEC_HOME=/var/lib/ameesh-exec",
                        "AMEESH_BACKEND=mediated",
                        "AMEESH_EXEC_SIGNER=cryptography",
                        "AMEESH_HOST_GATE_PATH=/run/ameesh-gate/state/state.json",
                        "AMEESH_HOST_GATE_ACK=/run/ameesh-gate/ack/ack.json",
                        "AMEESH_HOST_GATE_DRAIN=90",
                        "AMEESH_EXEC_ENROLL_DIR=/run/ameesh-enroll",
                        'VOLUME ["/var/lib/ameesh-exec"]'):
            self.assertIn(attendu, self.text)

    def test_construite_depuis_la_source(self):
        self.assertIn("COPY src ./src", self.text)
        self.assertIn("pip wheel", self.text)

    def test_lockfile_complet_et_integre(self):
        lock = json.loads(LOCKFILE.read_text(encoding="utf-8"))
        self.assertEqual(lock["lockfileVersion"], 3)
        self.assertIn("node_modules/@deepseek-ai/dsh", lock["packages"])
        self.assertEqual(lock["packages"]["node_modules/@deepseek-ai/dsh"]["version"],
                         "0.2.0-rc.2")
        sans = [nom for nom, paquet in lock["packages"].items()
                if nom and not paquet.get("integrity")]
        self.assertEqual(sans, [])
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        self.assertEqual(manifest["dependencies"]["@deepseek-ai/dsh"],
                         "0.2.0-rc.2")

    def test_dockerignore_laisse_passer_le_verrou(self):
        ignore = (REPO / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("!deploy/executor-image/package-lock.json", ignore)
        self.assertIn("!deploy/executor-image/package.json", ignore)


class PolicyTest(unittest.TestCase):
    """La politique est écrite : registre, tag, digest, O4, retour arrière."""

    def test_politique_ecrite(self):
        text = POLICY.read_text(encoding="utf-8")
        for attendu in ("ghcr.io/manaty/ameesh-executor",
                        "deploy/executor-image/publish.sh",
                        ".github/workflows/image-executor.yml",
                        "sha-<12>", "linux/amd64", "digest",
                        "404", "-K -", "stdin", "O4",
                        "après la fusion", "multi-architecture",
                        "retour arrière", "ré-épinglage"):
            self.assertIn(attendu, text)


if __name__ == "__main__":
    unittest.main()
