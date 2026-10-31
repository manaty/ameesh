# SPDX-License-Identifier: AGPL-3.0-only
"""#860 — les scripts de veille lisent GitHub par le cache partagé de l'hôte.

Décision owner #18841 (réponse b) : rester au jeton partagé et étendre le
cache de lectures partagées (#848) aux scripts de veille des agents, qui
appelaient `gh` directement. `ameesh github read` est leur porte d'entrée.

Ces tests tiennent la propriété, sans réseau ni base :

* la lecture passe par le client partagé de l'hôte (`state_dir`, `ci_ttl`) ;
* deux appels identiques ne paient qu'une requête (cache partagé entre
  processus), et une lecture périmée se revalide sans quota ;
* le quota épuisé sort en code 3 seulement si rien n'est en cache ; une
  valeur gardée est servie, marquée périmée, sans requête ;
* une erreur GitHub sort en 1, un chemin (blanc ou caractère de contrôle) ou
  un `--ttl` (non fini) refusés en 2, sans traceback ;
* le jeton n'apparaît ni sur la sortie, ni dans les erreurs ;
* la commande est routée par le point d'entrée public (`ameesh github read`).
"""
from __future__ import annotations

from .entrypoint import run as isolated_entrypoint

import contextlib
import dataclasses
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from . import support  # noqa: F401 - isoler l'environnement (L142) avant ameesh
from .fake_github import FakeGh
from ameesh import config as config_mod
from ameesh import github_cli, main as main_mod, plan_github

BASE = "https://api.test"
TOKEN = "ghp_0123456789abcdef0123456789abcdef0123"


class Clock:
    """Une horloge réglable : aucun test ne dépend de l'heure réelle."""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def quota_headers(*, remaining="4999", limit="5000", reset=None, resource=None) -> dict:
    headers = {"X-RateLimit-Limit": limit, "X-RateLimit-Remaining": remaining}
    if reset is not None:
        headers["X-RateLimit-Reset"] = str(int(reset))
    if resource is not None:
        headers["X-RateLimit-Resource"] = resource
    return headers


class FakeResponse:
    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeGitHub:
    """Un GitHub simulé : un corps par chemin, ETags, un échec programmable."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.reset = clock.now + 3600.0
        self.body: dict = {}
        self.etags: dict = {}
        self.calls: list = []
        self.headers: list = []
        self.fail_next: dict | None = None

    def __call__(self, request, timeout=None):
        path = request.full_url[len(BASE) + 1:]
        sent = {name.lower(): value for name, value in request.headers.items()}
        self.calls.append(path)
        self.headers.append(sent)
        if self.fail_next is not None:
            failure, self.fail_next = self.fail_next, None
            raise urllib.error.HTTPError(
                request.full_url, failure["code"], failure.get("message", "rate limited"),
                quota_headers(remaining=failure.get("remaining", "0"),
                              reset=failure.get("reset", self.reset)),
                io.BytesIO(b'{"message": "API rate limit exceeded"}'))
        etag = self.etags.get(path)
        if etag and sent.get("if-none-match") == etag:
            raise urllib.error.HTTPError(
                request.full_url, 304, "Not Modified",
                quota_headers(remaining=self.remaining, reset=self.reset), None)
        if path not in self.body:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found",
                quota_headers(remaining=self.remaining, reset=self.reset),
                io.BytesIO(b'{"message": "Not Found"}'))
        headers = quota_headers(remaining=self.remaining, reset=self.reset)
        if etag:
            headers["ETag"] = etag
        return FakeResponse(json.dumps(self.body[path]).encode("utf-8"), headers)

    @property
    def remaining(self) -> str:
        return "4999"


class CliCase(unittest.TestCase):
    """Le socle : un état d'hôte temporaire, une horloge, un faux GitHub."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.gh = FakeGitHub(self.clock)
        self.cfg = dataclasses.replace(config_mod.Config(), state_dir=self.dir.name,
                                       github={})
        self.seen: dict = {}
        self.addCleanup(mock.patch.stopall)

    def client(self, **kw):
        """Le client partagé que la commande construirait, sur le faux GitHub."""
        self.seen.update(kw)
        return plan_github.Api(token=TOKEN, base=BASE, cache_dir=kw.get("cache_dir"),
                               ttl=kw.get("ttl", plan_github.READS_TTL),
                               clock=self.clock, opener=self.gh)

    def run_cli(self, argv, client_factory=None):
        """`ameesh github …` par le point d'entrée public, sortie et code."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(config_mod, "load", return_value=self.cfg), \
                mock.patch.object(plan_github, "client", client_factory or self.client):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = isolated_entrypoint(main_mod.main, argv)
                except SystemExit as exit_:  # argparse : `--help`, choix refusé
                    code = int(exit_.code or 0)
        return code, out.getvalue(), err.getvalue()


class ReadPathTest(CliCase):
    def test_lecture_par_le_client_partage_de_l_hote(self):
        self.gh.body["repos/o/r/actions/runs?branch=lot%2F1&per_page=1"] = {
            "workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]}
        code, out, err = self.run_cli(
            ["github", "read", "/repos/o/r/actions/runs?branch=lot%2F1&per_page=1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["workflow_runs"][0]["conclusion"], "success")
        self.assertEqual(self.seen["cache_dir"], self.dir.name)
        self.assertEqual(self.seen["ttl"], plan_github.DEFAULT_CI_TTL)

    def test_ttl_du_reglage_puis_surcharge(self):
        self.gh.body["repos/o/r"] = {"id": 1}
        self.cfg = dataclasses.replace(self.cfg, github={"ci_ttl": 30})
        code, _out, _err = self.run_cli(["github", "read", "/repos/o/r"])
        self.assertEqual(code, 0)
        self.assertEqual(self.seen["ttl"], 30.0)
        code, _out, _err = self.run_cli(["github", "read", "/repos/o/r", "--ttl", "120"])
        self.assertEqual(code, 0)
        self.assertEqual(self.seen["ttl"], 120.0)

    def test_deux_appels_identiques_une_seule_requete(self):
        """Le cache est partagé entre les processus : le second ne paie rien."""
        self.gh.body["repos/o/r/pulls/42"] = {"number": 42, "mergeable": True}
        for _ in range(2):
            code, out, err = self.run_cli(["github", "read", "/repos/o/r/pulls/42"])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["number"], 42)
        self.assertEqual(len(self.gh.calls), 1)
        reads = plan_github.SharedReads(self.dir.name, clock=self.clock)
        self.assertEqual(reads.counters()["hits"], 1)

    def test_lecture_perimee_revalidee_sans_quota(self):
        """Au-delà de la garde, `If-None-Match` : un 304 ne consomme pas."""
        path = "repos/o/r/actions/runs?branch=lot%2F1&per_page=1"
        self.gh.body[path] = {"workflow_runs": []}
        self.gh.etags[path] = '"run-1"'
        self.cfg = dataclasses.replace(self.cfg, github={"ci_ttl": 5})
        code, _out, _err = self.run_cli(["github", "read", "/" + path])
        self.assertEqual(code, 0)
        self.clock.advance(10)
        code, out, err = self.run_cli(["github", "read", "/" + path])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"workflow_runs": []})
        self.assertEqual(self.gh.headers[1]["if-none-match"], '"run-1"')
        counters = plan_github.SharedReads(self.dir.name, clock=self.clock).counters()
        self.assertEqual(counters["requests"], 2)
        self.assertEqual(counters["not_modified"], 1)

    def test_quota_epuise_sans_cache_code_3(self):
        self.gh.fail_next = {"code": 403, "remaining": "0",
                             "reset": self.clock.now + 120}
        code, out, err = self.run_cli(["github", "read", "/repos/o/r/pulls/42"])
        self.assertEqual(code, github_cli.EXIT_RATE_LIMITED)
        self.assertEqual(out, "")
        self.assertIn("quota exhausted", err)
        self.assertIn("shared cache", err)

    def test_quota_epuise_sert_le_cache_marque_perime(self):
        """Une preuve déjà lue ne se perd pas : elle est servie du cache."""
        path = "repos/o/r/pulls/42"
        self.gh.body[path] = {"number": 42, "state": "open"}
        self.cfg = dataclasses.replace(self.cfg, github={"ci_ttl": 5})
        code, _out, _err = self.run_cli(["github", "read", "/" + path])
        self.assertEqual(code, 0)
        reads = plan_github.SharedReads(self.dir.name, clock=self.clock)
        reads.note(quota_headers(remaining="0", reset=self.clock.now + 120), status=403)
        self.clock.advance(10)
        code, out, err = self.run_cli(["github", "read", "/" + path])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["number"], 42)
        self.assertEqual(len(self.gh.calls), 1)  # aucune requête sous quota épuisé

    def test_erreur_github_code_1(self):
        code, out, err = self.run_cli(["github", "read", "/repos/o/r/pulls/404"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("HTTP 404", err)

    def test_chemin_refuse_code_2(self):
        """Blancs et caractères de contrôle (C0, DEL, C1) : refus 2, jamais un
        traceback d'`urllib` (N1 #894)."""
        for path in ("repos/o/r", "/repos/o/r pulls", "/repos/o/r\n",
                     "/repos/o/r\x01", "/repos/o/r\x1b", "/repos/o/r\x7f",
                     "/repos/o/r\x85"):
            with self.subTest(path=path):
                code, out, err = self.run_cli(["github", "read", path])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("github:", err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(self.gh.calls, [])

    def test_ttl_trop_court_code_2(self):
        self.gh.body["repos/o/r"] = {"id": 1}
        code, _out, err = self.run_cli(["github", "read", "/repos/o/r", "--ttl", "1"])
        self.assertEqual(code, 2)
        self.assertIn("at least", err)
        self.assertEqual(self.gh.calls, [])

    def test_ttl_non_fini_code_2(self):
        """`--ttl nan` ne désactive pas la garde, `inf` ne sert pas le cache
        indéfiniment : refus 2 (N2 #894)."""
        for ttl in ("nan", "inf", "-inf"):
            with self.subTest(ttl=ttl):
                code, out, err = self.run_cli(
                    ["github", "read", "/repos/o/r", "--ttl=%s" % ttl])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("finite", err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(self.gh.calls, [])

    def test_le_jeton_n_apparait_jamais(self):
        """Le jeton est envoyé, jamais rendu — même dans une erreur de quota."""
        self.gh.body["repos/o/r"] = {"id": 1}
        code, out, err = self.run_cli(["github", "read", "/repos/o/r"])
        self.assertEqual(code, 0)
        self.gh.fail_next = {"code": 403, "remaining": "0", "reset": self.clock.now + 60}
        code, out2, err2 = self.run_cli(["github", "read", "/repos/o/r/other"])
        self.assertEqual(code, github_cli.EXIT_RATE_LIMITED)
        for text in (out, err, out2, err2):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("ghp_", text)

    def test_aide_et_sous_commande_inconnue(self):
        code, out, _err = self.run_cli(["github", "read", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("REST path", out)
        code, _out, err = self.run_cli(["github", "write", "/repos/o/r"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_la_cle_du_cli_est_celle_des_lecteurs_internes(self):
        """Le chemin d'URL de la commande et `repos/…` des lecteurs internes
        (#848 : notify, ci, versions) désignent la même entrée de cache."""
        self.gh.body["repos/o/r"] = {"id": 1}
        code, _out, err = self.run_cli(["github", "read", "/repos/o/r"])
        self.assertEqual(code, 0, err)
        reader = plan_github.Api(token=TOKEN, base=BASE, cache_dir=self.dir.name,
                                 clock=self.clock, opener=self.gh)
        self.assertEqual(reader.get("repos/o/r"), {"id": 1})
        self.assertEqual(len(self.gh.calls), 1)  # servie par l'entrée du CLI

    def test_repli_gh_par_le_cli(self):
        """Sans jeton (hôte d'agents), la lecture passe par `gh api -i` et le
        cache partagé : le second appel ne relance aucune commande `gh`."""
        gh = FakeGh({"repos": {"o/r": {"visibility": "private"}}})

        def client(**kw):
            self.seen.update(kw)
            return plan_github.GhApi(
                gh, reads=plan_github.SharedReads(kw["cache_dir"], clock=self.clock,
                                                  ttl=kw["ttl"]),
                ttl=kw["ttl"], clock=self.clock)

        for _ in range(2):
            code, out, err = self.run_cli(["github", "read", "/repos/o/r"],
                                          client_factory=client)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["visibility"], "private")
        self.assertEqual(len(gh.calls()), 1)
        self.assertIn("api", gh.calls()[0])


class ReadPathUnitTest(unittest.TestCase):
    """Le cœur, sans CLI : résolution de la garde et refus du chemin."""

    def test_client_appele_avec_l_etat_de_l_hote(self):
        cfg = dataclasses.replace(config_mod.Config(), state_dir="/state",
                                  github={"ci_ttl": 45})
        seen: dict = {}

        class FakeClient:
            def get(self, path):
                seen["path"] = path
                return {"ok": True}

        def fake_client(**kw):
            seen.update(kw)
            return FakeClient()

        with mock.patch.object(plan_github, "client", fake_client):
            self.assertEqual(github_cli.read_path(cfg, "/repos/o/r"), {"ok": True})
        self.assertEqual(seen["cache_dir"], "/state")
        self.assertEqual(seen["ttl"], 45.0)
        self.assertEqual(seen["path"], "repos/o/r")  # clé du cache #848

    def test_ttl_explicite_gagne(self):
        cfg = dataclasses.replace(config_mod.Config(), state_dir="/state",
                                  github={"ci_ttl": 45})
        seen: dict = {}
        with mock.patch.object(plan_github, "client",
                               lambda **kw: seen.update(kw) or _EmptyClient()):
            github_cli.read_path(cfg, "/repos/o/r", ttl=90)
        self.assertEqual(seen["ttl"], 90.0)

    def test_check_path(self):
        self.assertEqual(github_cli.check_path("/repos/o/r"), "repos/o/r")
        self.assertEqual(github_cli.check_path("//repos/o/r"), "repos/o/r")
        for bad in ("", None, "repos/o/r", "https://api.github.com/repos/o/r",
                    "/repos/o/r\x01", "/repos/o/r\x7f", "/repos/o/r\x85"):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    github_cli.check_path(bad)

    def test_ttl_of(self):
        self.assertEqual(github_cli.ttl_of("90"), 90.0)
        for bad in ("4", "abc", None, True, "nan", "inf", "-inf",
                    float("nan"), float("inf")):
            with self.subTest(ttl=bad):
                with self.assertRaises(ValueError):
                    github_cli.ttl_of(bad)

    def test_read_path_valide_le_ttl_comme_le_cli(self):
        """`read_path` refuse aussi un `--ttl` non fini (N2 #894)."""
        cfg = dataclasses.replace(config_mod.Config(), state_dir="/state", github={})
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(ttl=bad):
                with self.assertRaises(ValueError):
                    github_cli.read_path(cfg, "/repos/o/r", ttl=bad)


class _EmptyClient:
    def get(self, path):  # pragma: no cover - jamais appelé ici
        return {}


class _StubGh:
    """Un `gh` de laboratoire : la CI n'installe pas `gh`, et le test ne doit
    pas dépendre du PATH de la machine (canon §3 : un test qui dépend de
    l'hôte est un défaut bloquant du test, corrigé à la source) — #894."""

    def __init__(self, *_args, **_kwargs):
        pass

    def run(self, *_args, **_kwargs):  # pragma: no cover - jamais appelé ici
        raise AssertionError("no gh call expected")


class FournisseurTest(unittest.TestCase):
    """Sans jeton, le repli `gh` reste servi — le cache s'y applique aussi."""

    def test_client_sans_jeton_rend_le_repli_gh(self):
        env = {name: "" for name in plan_github.TOKEN_VARS}
        env.update({name: value for name, value in os.environ.items()
                    if name not in plan_github.TOKEN_VARS})
        with tempfile.TemporaryDirectory() as state:  # jamais /state (N4 #894)
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(plan_github, "Gh", _StubGh):
                client = plan_github.client(cache_dir=state)
        self.assertIsInstance(client, plan_github.GhApi)
        self.assertIsInstance(client.gh, _StubGh)


if __name__ == "__main__":
    unittest.main()
