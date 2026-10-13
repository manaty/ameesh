# SPDX-License-Identifier: AGPL-3.0-only
"""#848 — le jeton GitHub partagé ne sature plus : cache partagé et quota.

Le 2026-10-13, le quota core du jeton partagé (5000/h) a été épuisé à 09:41Z :
chaque processus (notify, progress, versions) relisait GitHub sans partager ni
revalider, et les 403 se répétaient à chaque passage. Ces tests tiennent les
propriétés du correctif, sans réseau ni base :

* une lecture gardée est servie du disque, sans requête ;
* une lecture périmée est revalidée (`If-None-Match`) et un 304 ne consomme
  pas de quota ;
* le quota épuisé est écrit sur disque : plus aucune requête avant le reset,
  le cache est servi, sinon `GithubRateLimited` — jamais de boucle ;
* le relevé de CI passe par ce client, avec le cache de l'hôte ;
* l'économie est mesurable (compteurs, `ameesh ci status`).
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

from ameesh import ci, plan_github

from .fake_github import FakeGh

BASE = "https://api.test"


class Clock:
    """Une horloge réglable : aucun test ne dépend de l'heure réelle."""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def quota_headers(*, remaining="4999", limit="5000", reset=None, retry=None) -> dict:
    headers = {"X-RateLimit-Limit": limit, "X-RateLimit-Remaining": remaining}
    if reset is not None:
        headers["X-RateLimit-Reset"] = str(int(reset))
    if retry is not None:
        headers["Retry-After"] = str(int(retry))
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
    """Un GitHub simulé : réponses par chemin, ETags, quota, pannes.

    `fail_next` permet de faire répondre le prochain appel en 403/429, avec
    les en-têtes de quota voulus ; chaque appel est gardé dans `calls` et ses
    en-têtes dans `headers` (pour vérifier `If-None-Match`)."""

    def __init__(self, clock: Clock, *, remaining="4999", limit="5000"):
        self.clock = clock
        self.remaining = remaining
        self.limit = limit
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
                              limit=self.limit, reset=failure.get("reset", self.reset),
                              retry=failure.get("retry")),
                io.BytesIO(b'{"message": "API rate limit exceeded"}'))
        etag = self.etags.get(path)
        if etag and sent.get("if-none-match") == etag:
            raise urllib.error.HTTPError(
                request.full_url, 304, "Not Modified",
                quota_headers(remaining=self.remaining, limit=self.limit, reset=self.reset),
                None)
        if path not in self.body:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found",
                quota_headers(remaining=self.remaining, limit=self.limit, reset=self.reset),
                io.BytesIO(b'{"message": "Not Found"}'))
        headers = quota_headers(remaining=self.remaining, limit=self.limit, reset=self.reset)
        if etag:
            headers["ETag"] = etag
        return FakeResponse(json.dumps(self.body[path]).encode("utf-8"), headers)


class SharedReadsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()

    def reads(self, **kw):
        return plan_github.SharedReads(self.dir.name, clock=self.clock, **kw)

    def test_lecture_gardee_puis_perimee(self):
        reads = self.reads(ttl=60)
        self.assertIsNone(reads.cached("repos/o/r"))
        reads.store("repos/o/r", {"id": 1}, etag='"a"', read_ts=self.clock.now)
        entry = reads.cached("repos/o/r")
        self.assertEqual(entry.value, {"id": 1})
        self.assertEqual(entry.etag, '"a"')
        self.assertTrue(reads.fresh(entry))
        self.clock.advance(61)
        self.assertFalse(reads.fresh(entry))
        # le cache est borné : au-delà de max_entries, les plus anciens partent
        reads.store("repos/o/r2", {"id": 2})
        self.assertEqual(reads.prune(), 0)

    def test_elagage_borne(self):
        reads = self.reads(max_entries=2)
        for i in range(4):
            reads.store("repos/o/r%d" % i, {"id": i})
        self.assertEqual(reads.prune(), 2)
        self.assertEqual(len(os.listdir(os.path.join(reads.root, "reads"))), 2)

    def test_quota_epuise_ecrit_et_expire(self):
        reads = self.reads()
        self.assertIsNone(reads.blocked())
        until = reads.note(quota_headers(remaining="0", reset=self.clock.now + 300),
                           status=403)
        self.assertEqual(until, self.clock.now + 300)
        self.assertEqual(reads.blocked(), self.clock.now + 300)
        self.assertEqual(reads.counters()["requests"], 1)
        self.clock.advance(301)
        self.assertIsNone(reads.blocked())
        # un 403 sans en-tête de reset impose une attente bornée (secondaire)
        until = reads.note({"Retry-After": "20"}, status=403)
        self.assertEqual(until, self.clock.now + 20)

    def test_compteurs_partages(self):
        reads = self.reads()
        reads.note(quota_headers(remaining="4990"), status=200)
        reads.note(quota_headers(remaining="4990"), status=304)
        reads.count("hits", 2)
        counters = reads.counters()
        self.assertEqual((counters["requests"], counters["not_modified"],
                          counters["hits"]), (2, 1, 2))
        self.assertEqual(reads.quota()["remaining"], 4990)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.gh = FakeGitHub(self.clock)
        self.gh.body["repos/o/r"] = {"id": 1, "name": "r"}
        self.gh.etags["repos/o/r"] = '"abc"'

    def api(self, **kw):
        reads = plan_github.SharedReads(self.dir.name, clock=self.clock, ttl=60)
        return plan_github.Api(token="secret-token", base=BASE, reads=reads,
                               ttl=60, clock=self.clock, opener=self.gh, **kw)

    def test_cache_puis_revalidation_304(self):
        api = self.api()
        self.assertEqual(api.get("repos/o/r"), {"id": 1, "name": "r"})
        self.assertEqual(self.gh.calls, ["repos/o/r"])
        self.assertNotIn("if-none-match", self.gh.headers[0])
        # dans la durée de vie : servie du disque, aucune requête
        self.assertEqual(api.get("repos/o/r"), {"id": 1, "name": "r"})
        self.assertEqual(len(self.gh.calls), 1)
        # périmée : revalidée, le 304 rend la valeur sans consommer de quota
        self.clock.advance(61)
        self.assertEqual(api.get("repos/o/r"), {"id": 1, "name": "r"})
        self.assertEqual(len(self.gh.calls), 2)
        self.assertEqual(self.gh.headers[1]["if-none-match"], '"abc"')
        counters = api.reads.counters()
        self.assertEqual((counters["requests"], counters["not_modified"],
                          counters["hits"]), (2, 1, 1))
        self.assertEqual(api.reads.quota()["remaining"], 4999)

    def test_quota_epuise_sert_le_cache_puis_attend_le_reset(self):
        api = self.api()
        self.assertEqual(api.get("repos/o/r"), {"id": 1, "name": "r"})
        self.clock.advance(61)
        # 403 de quota : la valeur périmée est servie, rien n'est perdu
        self.gh.fail_next = {"code": 403, "remaining": "0",
                             "reset": self.clock.now + 600}
        self.assertEqual(api.get("repos/o/r"), {"id": 1, "name": "r"})
        self.assertEqual(len(self.gh.calls), 2)
        # bloqué : plus aucune requête, le cache répond encore
        self.assertEqual(api.get("repos/o/r"), {"id": 1, "name": "r"})
        self.assertEqual(len(self.gh.calls), 2)
        self.assertEqual(api.reads.counters()["stale"], 2)
        # le reset passé, la lecture repart (revalidation, pas de boucle)
        self.clock.advance(601)
        self.assertEqual(api.get("repos/o/r"), {"id": 1, "name": "r"})
        self.assertEqual(len(self.gh.calls), 3)

    def test_quota_epuise_sans_cache_leve_sans_requete(self):
        api = self.api()
        self.gh.fail_next = {"code": 403, "remaining": "0",
                             "reset": self.clock.now + 600}
        with self.assertRaises(plan_github.GithubRateLimited) as caught:
            api.get("repos/o/r")
        self.assertEqual(caught.exception.reset, self.clock.now + 600)
        self.assertEqual(len(self.gh.calls), 1)
        # un autre processus de l'hôte, même état : il n'appelle même pas
        other = plan_github.Api(token="secret-token", base=BASE,
                                cache_dir=self.dir.name, ttl=60, clock=self.clock,
                                opener=self.gh)
        with self.assertRaises(plan_github.GithubRateLimited):
            other.get("repos/o/r")
        self.assertEqual(len(self.gh.calls), 1)
        self.assertEqual(other.reads.counters()["refused"], 2)

    def test_sans_cache_comportement_inchange(self):
        api = plan_github.Api(token="secret-token", base=BASE, clock=self.clock,
                              opener=self.gh)
        self.assertEqual(api.get("repos/o/r"), {"id": 1, "name": "r"})
        self.assertEqual(api.get("repos/o/r"), {"id": 1, "name": "r"})
        self.assertEqual(len(self.gh.calls), 2)

    def test_client_partage_par_defaut(self):
        api = plan_github.client(env={"GH_TOKEN": "x"}, cache_dir=self.dir.name,
                                 clock=self.clock)
        self.assertIsInstance(api, plan_github.Api)
        self.assertEqual(api.reads.root, os.path.join(self.dir.name, "github"))
        self.assertIsNone(plan_github.client(env={}, allow_gh=False))
        # sans cache : aucun dossier créé
        plain = plan_github.client(env={"GH_TOKEN": "x"}, clock=self.clock)
        self.assertIsNone(plain.reads)

    def test_repli_gh_partage_aussi(self):
        gh = FakeGh()
        reads = plan_github.SharedReads(self.dir.name, clock=self.clock, ttl=60)
        api = plan_github.GhApi(gh, reads=reads, ttl=60, clock=self.clock)
        self.assertEqual(api.get("repos/o/r")["visibility"], "private")
        # dans la garde : servie du disque, aucune commande `gh`
        self.assertEqual(api.get("repos/o/r")["visibility"], "private")
        self.assertEqual(len(gh.calls()), 1)
        # périmée : revalidée par `If-None-Match`, un 304 rend la valeur
        self.clock.advance(61)
        self.assertEqual(api.get("repos/o/r")["visibility"], "private")
        self.assertEqual(len(gh.calls()), 2)
        self.assertIn("If-None-Match:", " ".join(gh.calls()[-1]))
        self.assertEqual(reads.counters()["not_modified"], 1)


class MesureTest(unittest.TestCase):
    """L'économie de requêtes, mesurée : 4 relevés, 2 requêtes, 2 cache."""

    def test_quatre_releves_deux_requetes(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            gh = FakeGitHub(clock)
            gh.body["repos/o/r/actions/runs?status=queued"] = {"workflow_runs": []}
            gh.etags["repos/o/r/actions/runs?status=queued"] = '"run1"'
            api = plan_github.Api(token="secret-token", base=BASE, reads=plan_github.SharedReads(
                directory, clock=clock, ttl=60), ttl=60, clock=clock, opener=gh)
            for _ in range(4):
                self.assertEqual(api.get("repos/o/r/actions/runs?status=queued"),
                                 {"workflow_runs": []})
                clock.advance(30)
            counters = api.reads.counters()
            self.assertEqual(counters["requests"], 2)
            self.assertEqual(counters["not_modified"], 1)
            self.assertEqual(counters["hits"], 2)
            self.assertEqual(api.reads.quota()["remaining"], 4999)


class SettingsTest(unittest.TestCase):
    def test_ci_ttl(self):
        self.assertEqual(plan_github.parse_settings({}).ci_ttl,
                         plan_github.DEFAULT_CI_TTL)
        config = plan_github.parse_settings({"projects": {"a": "o/a"}, "ci_ttl": 30})
        self.assertEqual(config.ci_ttl, 30.0)
        with self.assertRaises(plan_github.SettingsError):
            plan_github.parse_settings({"ci_ttl": 4})
        with self.assertRaises(plan_github.SettingsError):
            plan_github.parse_settings({"ci_ttl": True})

    def test_lecture_ci_du_releve(self):
        """Le relevé lit avec le client à jeton, dans le cache de l'hôte."""
        clock = Clock()
        cfg = SimpleNamespace(host="h", state_dir="/state",
                              github={"projects": {"a": "o/a", "b": "o/b"}, "host": "h"})
        seen: dict = {}

        def fake_gh_api(**kw):
            seen.update(kw)
            return lambda path: {}

        watcher = ci.Watcher(cfg, clock=clock)
        watcher.watch = lambda db, repo: []
        with mock.patch.object(ci, "gh_api", fake_gh_api):
            self.assertEqual(watcher.tick(None), [])
        self.assertEqual(seen["cache_dir"], "/state")
        self.assertEqual(seen["ttl"], plan_github.DEFAULT_CI_TTL)
        self.assertEqual(seen["clock"], clock)
        # `github.ci_ttl` règle la garde
        cfg.github["ci_ttl"] = 30
        seen.clear()
        with mock.patch.object(ci, "gh_api", fake_gh_api):
            ci.Watcher(cfg, clock=clock).tick(None)
        self.assertEqual(seen["ttl"], 30.0)

    def test_quota_epuise_arrete_le_passage(self):
        cfg = SimpleNamespace(host="h", state_dir="/state",
                              github={"projects": {"a": "o/a", "b": "o/b"}, "host": "h"})
        logs: list = []
        watcher = ci.Watcher(cfg, api=lambda path: {}, log=logs.append)
        visited: list = []

        def watch(db, repo):
            visited.append(repo)
            raise plan_github.GithubRateLimited(repo, 1000)

        watcher.watch = watch
        self.assertEqual(watcher.tick(None), [])
        self.assertEqual(visited, ["o/a"])          # le second dépôt n'appelle pas
        self.assertIn("quota exhausted", logs[0])


class StatusLineTest(unittest.TestCase):
    def test_ligne_de_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            self.assertIsNone(ci.quota_line(directory, now=clock.now))
            reads = plan_github.SharedReads(directory, clock=clock)
            reads.note(quota_headers(remaining="4998"), status=200)
            reads.count("hits", 3)
            line = ci.quota_line(directory, now=clock.now)
            self.assertIn("1 request(s) (0 not modified)", line)
            self.assertIn("3 served from the shared cache", line)
            self.assertIn("quota 4998/5000", line)
            self.assertNotIn("exhausted", line)
            reads.note(quota_headers(remaining="0", reset=clock.now + 120), status=403)
            self.assertIn("exhausted until", ci.quota_line(directory, now=clock.now))


class DataclassCompatTest(unittest.TestCase):
    def test_settings_reste_immutable(self):
        config = plan_github.Settings(projects={"a": "o/a"}, host="h")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.ci_ttl = 1


if __name__ == "__main__":
    unittest.main()
