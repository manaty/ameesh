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
* l'économie est mesurable (compteurs, `ameesh ci status`) ;
* un incrément de compteur arrivé pendant l'écriture n'est jamais perdu
  (#1927).
"""
from __future__ import annotations

import contextlib
import dataclasses
import gc
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

from ameesh import ci, plan_github, versions

from .fake_github import FakeGh
from .support import ThreadLeakMixin

BASE = "https://api.test"


class Clock:
    """Une horloge réglable : aucun test ne dépend de l'heure réelle."""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def quota_headers(*, remaining="4999", limit="5000", reset=None, retry=None,
                  resource=None) -> dict:
    headers = {"X-RateLimit-Limit": limit, "X-RateLimit-Remaining": remaining}
    if reset is not None:
        headers["X-RateLimit-Reset"] = str(int(reset))
    if retry is not None:
        headers["Retry-After"] = str(int(retry))
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
                              retry=failure.get("retry"), resource=failure.get("resource")),
                io.BytesIO(failure.get("body", b'{"message": "API rate limit exceeded"}')))
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

    def test_graphie_des_entetes_sans_casse(self):
        """R1 #848 B1 : `gh api -i` rend `X-Ratelimit-*`, urllib la camel-case ;
        les deux graphies (et les minuscules) disent le même quota."""
        real = {"X-Ratelimit-Limit": "5000", "X-Ratelimit-Remaining": "0",
                "X-Ratelimit-Used": "5000", "X-Ratelimit-Reset": "10000",
                "X-Ratelimit-Resource": "core"}
        for headers in (real, {name.lower(): value for name, value in real.items()}):
            reads = self.reads()
            until = reads.note(headers, status=403, now=self.clock.now)
            self.assertEqual(until, 10000.0, headers)
            self.assertEqual(reads.quota()["remaining"], 0, headers)
            self.assertEqual(reads.quota()["used"], 5000, headers)
            self.assertIsNotNone(reads.blocked(), headers)

    def test_quota_par_ressource(self):
        """R1 #848 : le bucket GraphQL ne bloque pas le bucket core."""
        reads = self.reads()
        until = reads.note(quota_headers(remaining="0", reset=5000, resource="graphql"),
                           status=403, now=self.clock.now)
        self.assertEqual(until, 5000.0)
        self.assertIsNone(reads.blocked())                       # core intact
        self.assertEqual(reads.blocked(resource="graphql"), 5000.0)
        reads.note(quota_headers(remaining="4990", resource="core"),
                   status=200, now=self.clock.now)
        self.assertEqual(reads.quota("core")["remaining"], 4990)
        self.assertEqual(reads.quota("graphql")["remaining"], 0)
        self.assertEqual(sorted(reads.quotas()), ["core", "graphql"])

    # -- N1, N3, N4 de la relecture Claude (#848) --------------------------

    def test_lecture_ne_cree_aucun_dossier(self):
        """N1 : une construction ou une lecture ne crée rien ; le cache naît
        à sa première écriture, et la vue ne fabrique aucun chemin."""
        github = os.path.join(self.dir.name, plan_github.READS_DIR)
        reads = self.reads()
        self.assertFalse(os.path.exists(github))
        self.assertIsNone(reads.cached("repos/o/r"))
        self.assertEqual(reads.quotas(), {})
        self.assertEqual(reads.counters(), {})
        self.assertEqual(plan_github.quota_stats(self.dir.name),
                         {"quota": {}, "counters": {}})
        self.assertFalse(os.path.exists(github), "une lecture a créé un dossier")
        reads.store("repos/o/r", {"id": 1})
        self.assertTrue(os.path.isdir(os.path.join(github, "reads")))

    def test_state_dir_vide_refuse(self):
        """N1 : un état vide est refusé, jamais transformé en `None/github`."""
        for empty in (None, "", "   "):
            with self.subTest(state_dir=empty):
                with self.assertRaises(ValueError):
                    plan_github.SharedReads(empty)
        self.assertEqual(plan_github.quota_stats(None),
                         {"quota": {}, "counters": {}})
        self.assertEqual(plan_github.quota_stats(""), {"quota": {}, "counters": {}})

    def test_compteurs_agreges_puis_ecrits_une_fois(self):
        """N3 : aucun verrou ni écriture par hit ; une écriture au passage."""
        reads = self.reads()
        writes: list = []
        real = plan_github._atomic_write

        def spy(path, data):
            writes.append(path)
            return real(path, data)

        with mock.patch.object(plan_github, "_atomic_write", spy):
            for _ in range(5):
                reads.count("hits")
            self.assertEqual(writes, [], "un hit de cache a écrit quota.json")
            self.assertEqual(reads.counters()["hits"], 5)     # agrégé en mémoire
            self.assertFalse(os.path.exists(reads._quota_path))
            reads.flush()
            self.assertEqual(writes, [reads._quota_path])     # une seule écriture
            self.assertEqual(self.reads().counters()["hits"], 5)
            reads.flush()                                     # rien de neuf
            self.assertEqual(len(writes), 1)

    def test_compteurs_du_passage_ecrits_a_la_fin(self):
        """N3 : le passage terminé (objet libéré), les compteurs sont sur disque."""
        reads = self.reads()
        reads.count("hits", 2)
        self.assertFalse(os.path.exists(reads._quota_path))
        del reads
        gc.collect()
        self.assertEqual(self.reads().counters()["hits"], 2)

    def test_flush_reads_ecrit_les_compteurs_du_passage(self):
        """#1813 : `flush_reads` (fin de passage du Watcher) écrit les compteurs
        des caches vivants, sans attendre la requête réelle suivante."""
        reads = self.reads()
        reads.count("hits", 3)
        self.assertFalse(os.path.exists(reads._quota_path))
        self.assertGreaterEqual(plan_github.flush_reads(self.dir.name), 1)
        self.assertEqual(self.reads().counters()["hits"], 3)
        # un second passage sans nouveau hit n'écrit rien
        before = os.path.getmtime(reads._quota_path)
        plan_github.flush_reads(self.dir.name)
        self.assertEqual(os.path.getmtime(reads._quota_path), before)

    def test_flush_reads_ignore_un_autre_etat(self):
        """#1813 : le passage d'un hôte ne vide pas les compteurs d'un autre."""
        reads = self.reads()
        reads.count("hits", 1)
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.assertEqual(plan_github.flush_reads(other.name), 0)
        self.assertEqual(plan_github.flush_reads(""), 0)
        self.assertFalse(os.path.exists(reads._quota_path))
        self.assertEqual(reads.counters()["hits"], 1)

    def test_count_atomique_entre_fils(self):
        """#1813 : un client partagé entre fils ne perd pas de compteur."""
        reads = self.reads()

        def bump():
            for _ in range(200):
                reads.count("hits")

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(reads.counters()["hits"], 800)

    # -- #1927 : un incrément concurrent ne se perd pas entre fusion et purge --

    def _during_write(self, quota_path, trigger):
        """Espionne `_atomic_write` et rejoue `trigger` pendant l'écriture.

        Le déclencheur ne capture que `quota_path` : le test du finaliseur ne
        doit garder aucune référence à l'objet qu'il fait collecter."""
        real = plan_github._atomic_write
        fired: list = []

        def spy(path, data):
            if path == quota_path and not fired:
                fired.append(path)
                trigger()
            return real(path, data)

        return spy

    def test_increment_concurrent_pendant_le_flush_est_conserve(self):
        """#1927 : l'incrément arrivé pendant l'écriture du flush reste visible.

        L'ancien code fusionnait l'instantané vivant puis vidait `pending`
        après coup : le `count` reçu pendant l'écriture était purgé sans avoir
        été fusionné (disque 1 + mémoire 0 au lieu de 2)."""
        reads = self.reads()
        reads.count("hits")
        spy = self._during_write(reads._quota_path, lambda: reads.count("hits"))
        with mock.patch.object(plan_github, "_atomic_write", spy):
            reads.flush()
        self.assertEqual(reads.counters()["hits"], 2)      # disque + mémoire
        self.assertEqual(self.reads().counters()["hits"], 2)

    def test_increment_concurrent_pendant_le_note_est_conserve(self):
        """#1927 : même garantie sur `note`, l'autre chemin de fusion."""
        reads = self.reads()
        reads.count("hits")
        spy = self._during_write(reads._quota_path, lambda: reads.count("hits"))
        with mock.patch.object(plan_github, "_atomic_write", spy):
            reads.note(quota_headers(remaining="4990"), status=200)
        self.assertEqual(reads.counters()["hits"], 2)
        self.assertEqual(self.reads().counters()["hits"], 2)

    def test_deux_flush_concurrents_somment_leurs_compteurs(self):
        """#1927 : deux écritures concurrentes du même cache s'additionnent,
        chacune fusionnant sous le verrou de fichier ; aucune ne bloque."""
        one = self.reads()
        two = self.reads()
        one.count("hits", 3)
        two.count("hits", 4)
        errors: list = []

        def flush(reads):
            try:
                reads.flush()
            except BaseException as exc:                  # jamais bloquant
                errors.append(exc)

        threads = [threading.Thread(target=flush, args=(reads,))
                   for reads in (one, two)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.reads().counters()["hits"], 7)

    def test_panne_du_flush_rend_les_compteurs_au_passage_suivant(self):
        """#1927 : l'écriture en panne ne perd rien — les compteurs retirés
        reviennent en mémoire et repartent au passage suivant."""
        reads = self.reads()
        reads.count("hits", 2)
        with mock.patch.object(plan_github, "_atomic_write",
                               side_effect=OSError("disque plein")):
            reads.flush()
        self.assertEqual(reads.counters()["hits"], 2)
        reads.flush()
        self.assertEqual(self.reads().counters()["hits"], 2)

    def test_panne_du_note_rend_les_compteurs_au_passage_suivant(self):
        """#1927 : même garantie de retour arrière sur `note`."""
        reads = self.reads()
        reads.count("hits", 2)
        with mock.patch.object(plan_github, "_atomic_write",
                               side_effect=OSError("disque plein")):
            self.assertIsNone(reads.note(quota_headers(remaining="4990"), status=200))
        self.assertEqual(reads.counters()["hits"], 2)
        reads.flush()
        self.assertEqual(self.reads().counters()["hits"], 2)

    def test_panne_inattendue_rend_aussi_les_compteurs(self):
        """#1927 : une panne hors OSError/ValueError (un `quota.json` aux
        valeurs d'un autre type lève TypeError dans la fusion) se propage
        comme avant, mais ne perd pas les compteurs déjà retirés."""
        for path in ("flush", "note"):
            with self.subTest(path=path):
                reads = self.reads()
                reads.count("hits", 2)
                with mock.patch.object(plan_github, "_atomic_write",
                                       side_effect=TypeError("valeur illisible")):
                    with self.assertRaises(TypeError):
                        if path == "flush":
                            reads.flush()
                        else:
                            reads.note(quota_headers(remaining="4990"), status=200)
                self.assertEqual(reads._pending.get("hits"), 2)
                reads._pending.clear()

    def test_finaliseur_ne_perd_pas_un_increment_concurrent(self):
        """#1927 : le finaliseur écrit par le même chemin ; un incrément reçu
        d'une autre instance vivante pendant son écriture reste visible."""
        dying = self.reads()
        live = self.reads()
        dying.count("hits")
        live.count("hits", 2)
        spy = self._during_write(dying._quota_path, lambda: live.count("hits"))
        with mock.patch.object(plan_github, "_atomic_write", spy):
            del dying
            gc.collect()
        self.assertEqual(self.reads().counters()["hits"], 4)

    def test_panne_du_finaliseur_laisse_le_disque_intact(self):
        """#1927 : un finaliseur dont l'écriture échoue ne corrompt rien et ne
        fait pas échouer le passage : le disque reste tel quel."""
        reads = self.reads()
        reads.count("hits", 2)
        with mock.patch.object(plan_github, "_atomic_write",
                               side_effect=OSError("disque plein")):
            del reads
            gc.collect()
            self.assertFalse(os.path.exists(os.path.join(
                self.dir.name, plan_github.READS_DIR, "quota.json")))
            self.assertEqual(self.reads().counters(), {})

    def test_cle_de_cache_par_base_et_jeton(self):
        """N4 : deux jetons (ou deux bases) ne partagent pas la même entrée,
        et le jeton n'apparaît nulle part dans le cache."""
        one = plan_github.SharedReads(
            self.dir.name, scope=plan_github.reads_scope(BASE, "jeton-a"))
        two = plan_github.SharedReads(
            self.dir.name, scope=plan_github.reads_scope(BASE, "jeton-b"))
        one.store("repos/o/r", {"id": 1})
        self.assertIsNotNone(one.cached("repos/o/r"))
        self.assertIsNone(two.cached("repos/o/r"), "un autre jeton partage l'entrée")
        other_base = plan_github.SharedReads(
            self.dir.name, scope=plan_github.reads_scope("https://ghe.test", "jeton-a"))
        self.assertIsNone(other_base.cached("repos/o/r"), "une autre base partage l'entrée")
        token = "jeton-secret-n4"
        scoped = plan_github.SharedReads(
            self.dir.name, scope=plan_github.reads_scope(BASE, token))
        scoped.store("repos/o/r2", {"id": 2})
        reads_dir = os.path.join(self.dir.name, plan_github.READS_DIR, "reads")
        for name in os.listdir(reads_dir):
            self.assertNotIn(token, name, "le jeton est dans un nom de fichier")
            with open(os.path.join(reads_dir, name), encoding="utf-8") as fh:
                self.assertNotIn(token, fh.read(), "le jeton est dans le cache")


class WatcherFlushTest(unittest.TestCase):
    """#1813 : le passage du Watcher écrit les compteurs du cache partagé."""

    def test_le_passage_ecrit_les_compteurs_meme_sans_depot(self):
        with tempfile.TemporaryDirectory() as state_dir:
            reads = plan_github.SharedReads(state_dir)
            reads.count("hits", 4)
            cfg = SimpleNamespace(state_dir=state_dir, github={}, host="h")
            watcher = ci.Watcher(cfg, api=lambda path: {})
            with mock.patch.object(watcher, "repos", return_value=[]):
                self.assertEqual(watcher.tick(None), [])
            self.assertEqual(plan_github.SharedReads(state_dir).counters()["hits"], 4)


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

    def test_deux_jetons_ne_partagent_pas_le_cache(self):
        """N4 : la clé du cache porte la base et une empreinte du jeton."""
        first = plan_github.Api(token="jeton-a", base=BASE, cache_dir=self.dir.name,
                                ttl=60, clock=self.clock, opener=self.gh)
        self.assertEqual(first.get("repos/o/r"), {"id": 1, "name": "r"})
        self.assertEqual(self.gh.calls, ["repos/o/r"])
        second = plan_github.Api(token="jeton-b", base=BASE, cache_dir=self.dir.name,
                                 ttl=60, clock=self.clock, opener=self.gh)
        self.assertEqual(second.get("repos/o/r"), {"id": 1, "name": "r"})
        self.assertEqual(len(self.gh.calls), 2,
                         "un autre jeton a été servi par le cache du premier")

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
        # la fausse sortie `gh` porte la graphie réelle (`X-Ratelimit-*`,
        # `Etag`) : ce témoin couvre B1 de bout en bout (If-None-Match, 304)
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


class ResourceAndSecondaryLimitTest(unittest.TestCase):
    """#1405 : le cache retient la ressource du chemin et la limite
    secondaire, et repaie une entrée illisible (N2-N3 de la R2 bureau de
    #848 ; la clé par client, N1, est celle de #1790 : `reads_scope`)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.gh = FakeGitHub(self.clock)
        self.gh.body["repos/o/r"] = {"id": 1}
        self.gh.body["search/issues?q=x"] = {"total_count": 0}

    def api(self, token="secret-token", base=BASE):
        reads = plan_github.SharedReads(self.dir.name, clock=self.clock, ttl=60)
        return plan_github.Api(token=token, base=base, reads=reads, ttl=60,
                               clock=self.clock, opener=self.gh)

    def test_entree_illisible_repayee(self):
        """Une date illisible dans une entrée ne fait pas échouer la lecture."""
        api = self.api()
        api.get("repos/o/r")
        path = api.reads._entry_path("repos/o/r")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["read_ts"] = "abc"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.assertIsNone(api.reads.cached("repos/o/r"))
        self.assertEqual(api.get("repos/o/r"), {"id": 1})
        self.assertEqual(len(self.gh.calls), 2)

    def test_ressource_du_chemin(self):
        self.assertEqual(plan_github.resource_of("repos/o/r"), "core")
        self.assertEqual(plan_github.resource_of("/search/issues?q=x"), "search")
        self.assertEqual(plan_github.resource_of("search/code?q=x"), "code_search")
        self.assertEqual(plan_github.resource_of("graphql"), "graphql")

    def test_recherche_epuisee_bloquee_davance(self):
        """N2 : une ressource non core épuisée ne repart pas vers GitHub."""
        api = self.api()
        self.gh.fail_next = {"code": 403, "remaining": "0", "resource": "search",
                             "reset": self.clock.now + 60}
        with self.assertRaises(plan_github.GithubRateLimited):
            api.get("search/issues?q=x")
        self.assertEqual(len(self.gh.calls), 1)
        with self.assertRaises(plan_github.GithubRateLimited):
            api.get("search/issues?q=x")
        self.assertEqual(len(self.gh.calls), 1)          # aucune requête de plus
        self.assertEqual(api.get("repos/o/r"), {"id": 1})  # core intact
        self.assertEqual(len(self.gh.calls), 2)
        self.clock.advance(61)
        self.assertEqual(api.get("search/issues?q=x"), {"total_count": 0})

    def test_refus_sans_entete_range_sous_la_ressource_du_chemin(self):
        """N2 : sans `X-RateLimit-Resource`, le refus va au bucket du chemin."""
        api = self.api()
        self.gh.fail_next = {"code": 429, "remaining": "0", "reset": self.clock.now + 60}
        with self.assertRaises(plan_github.GithubRateLimited):
            api.get("search/issues?q=x")
        self.assertIsNotNone(api.reads.blocked(resource="search"))
        self.assertIsNone(api.reads.blocked())

    def test_limite_secondaire_retenue(self):
        """N3 : 403 secondaire, quota restant, sans `Retry-After` : attente bornée."""
        api = self.api()
        api.get("repos/o/r")
        self.clock.advance(61)
        self.gh.fail_next = {"code": 403, "remaining": "4000",
                             "body": b'{"message": "You have exceeded a secondary rate limit"}'}
        self.assertEqual(api.get("repos/o/r"), {"id": 1})   # cache servi
        self.assertTrue(api.last_read.stale)
        self.assertEqual(api.reads.blocked(),
                         self.clock.now + plan_github.RATE_LIMIT_WAIT)
        self.assertEqual(api.get("repos/o/r"), {"id": 1})
        self.assertEqual(len(self.gh.calls), 2)              # pas de nouvel essai
        self.clock.advance(plan_github.RATE_LIMIT_WAIT + 1)
        api.get("repos/o/r")
        self.assertEqual(len(self.gh.calls), 3)

    def test_refus_ordinaire_ne_bloque_pas(self):
        """N3 : un 403 de droits (quota restant, sans mention secondaire) reste
        une erreur, sans attente imposée."""
        api = self.api()
        self.gh.fail_next = {"code": 403, "remaining": "4000",
                             "body": b'{"message": "Resource not accessible by integration"}'}
        with self.assertRaises(plan_github.GithubError) as caught:
            api.get("repos/o/r")
        self.assertNotIsInstance(caught.exception, plan_github.GithubRateLimited)
        self.assertIsNone(api.reads.blocked())

    def test_secondaire_quota_a_zero_attend_le_reset(self):
        reads = plan_github.SharedReads(self.dir.name, clock=self.clock)
        until = reads.note(quota_headers(remaining="0", reset=self.clock.now + 900),
                           status=403, body="secondary rate limit")
        self.assertEqual(until, self.clock.now + 900)


class TransportMetaTest(unittest.TestCase):
    """R1 #848 B2 : le transport dit ce qu'il a servi (date d'origine, péremption)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.gh = FakeGitHub(self.clock)
        self.gh.body["repos/o/r"] = {"id": 1}
        self.gh.etags["repos/o/r"] = '"abc"'

    def api(self, **kw):
        reads = plan_github.SharedReads(self.dir.name, clock=self.clock, ttl=60)
        return plan_github.Api(token="secret-token", base=BASE, reads=reads,
                               ttl=60, clock=self.clock, opener=self.gh, **kw)

    def test_hit_frais_garde_la_date_dorigine(self):
        api = self.api()
        self.assertEqual(api.get("repos/o/r"), {"id": 1})
        self.assertEqual(api.last_read, plan_github.ReadMeta(self.clock.now, False, "network"))
        first = self.clock.now
        self.clock.advance(10)
        self.assertEqual(api.get("repos/o/r"), {"id": 1})
        self.assertEqual(len(self.gh.calls), 1)          # servie du disque
        self.assertEqual(api.last_read.source, "cache")
        self.assertFalse(api.last_read.stale)
        self.assertEqual(api.last_read.read_ts, first)

    def test_cache_servi_sous_quota_epuise_est_perime(self):
        api = self.api()
        api.get("repos/o/r")
        first = self.clock.now
        self.clock.advance(61)
        self.gh.fail_next = {"code": 403, "remaining": "0",
                             "reset": self.clock.now + 600}
        self.assertEqual(api.get("repos/o/r"), {"id": 1})
        self.assertTrue(api.last_read.stale)
        self.assertEqual(api.last_read.source, "cache")
        self.assertEqual(api.last_read.read_ts, first)


class ReaderStaleTest(unittest.TestCase):
    """R1 #848 B2 : un Reader sur cache partagé ne dit pas frais ce qui est périmé."""

    @staticmethod
    def _no_network(*_args, **_kw):
        raise AssertionError("aucune requête attendue")

    def _reader(self, directory, wall, api):
        with mock.patch.object(plan_github, "client", lambda **kw: api):
            reader = versions.Reader(cache_dir=directory, wall=wall,
                                     clock=lambda: wall.now)
            value = reader.get("repos/o/r")
        return reader, value

    def test_reader_quota_epuise_garde_date_et_perime(self):
        with tempfile.TemporaryDirectory() as directory:
            wall = Clock()
            reads = plan_github.SharedReads(directory, clock=wall, ttl=60)
            api = plan_github.Api(token="t", base=BASE, reads=reads, ttl=60,
                                  clock=wall, opener=self._no_network)
            reads.store("repos/o/r", {"id": 1}, etag='"e"', read_ts=1.0)
            reads.note(quota_headers(remaining="0", reset=100000),
                       status=403, now=wall.now)
            reader, value = self._reader(directory, wall, api)
            self.assertEqual(value, {"id": 1})           # rien n'est perdu
            self.assertTrue(reader.stale("repos/o/r"))   # mais c'est périmé
            self.assertEqual(reader.stamp("repos/o/r"), 1.0)
            self.assertEqual(reader.read_ts(), 1.0)

    def test_reader_hit_frais_garde_la_date_du_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            wall = Clock()
            reads = plan_github.SharedReads(directory, clock=wall, ttl=60)
            api = plan_github.Api(token="t", base=BASE, reads=reads, ttl=60,
                                  clock=wall, opener=self._no_network)
            reads.store("repos/o/r", {"id": 1}, etag='"e"', read_ts=990.0)
            reader, value = self._reader(directory, wall, api)
            self.assertEqual(value, {"id": 1})
            self.assertEqual(reader.stamp("repos/o/r"), 990.0)
            self.assertFalse(reader.stale("repos/o/r"))


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
            # le bucket GraphQL est dit à part, sans masquer le core épuisé
            reads.note(quota_headers(remaining="4895", resource="graphql"), status=200)
            line = ci.quota_line(directory, now=clock.now)
            self.assertIn("quota 0/5000", line)
            self.assertIn("quota graphql 4895/5000", line)


class DataclassCompatTest(unittest.TestCase):
    def test_settings_reste_immutable(self):
        config = plan_github.Settings(projects={"a": "o/a"}, host="h")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.ci_ttl = 1


class JsonReportTest(ThreadLeakMixin, unittest.TestCase):
    """N2 #848 : la vue JSON de `ameesh ci status` dit les lectures partagées.

    #4740 : le témoin d'écriture concurrente lance un fil ; la classe vérifie
    qu'aucun fil ne survit à son test (sinon son « {} » tombe dans la capture
    d'un test voisin).
    """

    def _run(self, cfg):
        args = SimpleNamespace(json=True, id=7, limit=5)
        out = io.StringIO()
        with mock.patch.object(
                ci, "status_of",
                lambda db, item_id, limit=5: {"id": item_id, "runs": []}), \
                mock.patch("ameesh.db.open_db", lambda cfg: None):
            # #4736 : la vue va sur ce flux, jamais sur le `sys.stdout` global
            # qu'un fil d'un autre test du même processus peut écrire.
            code = ci.cmd_ci(cfg, args, out=out)
        return code, json.loads(out.getvalue())

    def test_json_porte_les_lectures_partagees(self):
        with tempfile.TemporaryDirectory() as directory:
            reads = plan_github.SharedReads(directory)
            reads.note(quota_headers(remaining="4997"), status=200)
            reads.count("hits", 2)
            reads.flush()
            code, report = self._run(SimpleNamespace(state_dir=directory))
            self.assertEqual(code, 0)
            self.assertEqual(report["github_reads"]["counters"]["requests"], 1)
            self.assertEqual(report["github_reads"]["counters"]["hits"], 2)
            self.assertEqual(report["github_reads"]["quota"]["core"]["remaining"], 4997)

    def test_json_sans_etat_ne_fabrique_pas_de_chemin(self):
        code, report = self._run(SimpleNamespace(state_dir=None))
        self.assertEqual(code, 0)
        self.assertEqual(report["github_reads"], {"quota": {}, "counters": {}})

    def test_un_fil_ecrivant_sur_le_flux_capture_ne_pollue_pas_la_vue(self):
        """#4736 : témoin du « Extra data: line 1 column 3 » de la CI.

        `redirect_stdout` est global au processus : un fil d'un autre test du
        même processus qui écrit un JSON vide pendant que la commande rend sa
        vue salit la capture. Ici l'écrivain écrit sur `bruit`, le flux capturé
        du test, et la vue, passée par `out`, reste un JSON lisible.

        #4740 : le fil est joint dans tous les cas — même si `status_of` n'est
        pas appelé (échec avant) — et n'écrit jamais sur le `sys.stdout`
        global : sinon il survit à son test et son « {} » atterrit plus tard
        dans la capture d'un test voisin."""
        args = SimpleNamespace(json=True, id=7, limit=5)
        out, bruit = io.StringIO(), io.StringIO()
        ecrit, fini = threading.Event(), threading.Event()

        def ecrivain():
            try:
                if ecrit.wait(10):
                    print("{}", file=bruit)
            finally:
                fini.set()

        def status_of(db, item_id, limit=5):
            ecrit.set()
            self.assertTrue(fini.wait(10), "l'écrivain concurrent n'a pas écrit")
            return {"id": item_id, "runs": []}

        fil = threading.Thread(target=ecrivain, name="ecrivain-4736", daemon=True)
        fil.start()
        try:
            with mock.patch.object(ci, "status_of", status_of), \
                    mock.patch("ameesh.db.open_db", lambda cfg: None), \
                    contextlib.redirect_stdout(bruit):
                code = ci.cmd_ci(SimpleNamespace(state_dir=None), args, out=out)
        finally:
            # #4740 : libère l'écrivain même si le corps a échoué avant
            # `status_of` ; il rend la main et ne parle qu'à `bruit`.
            ecrit.set()
            fil.join(10)
        self.assertFalse(fil.is_alive(), "l'écrivain a survécu à son test")
        self.assertEqual(code, 0)
        self.assertEqual(bruit.getvalue(), "{}\n")       # le bruit est bien passé
        self.assertEqual(json.loads(out.getvalue())["github_reads"],
                         {"quota": {}, "counters": {}})


class RateLimitedPropagationTest(unittest.TestCase):
    """N5 #848 : un quota épuisé n'est plus avalé par la découverte ni la garde."""

    def setUp(self):
        self.cfg = SimpleNamespace(host="h", state_dir="/state",
                                   github={"projects": {"a": "o/a"}, "host": "h"})

    def test_decouverte_ne_masque_pas_le_quota(self):
        def api(path):
            raise plan_github.GithubRateLimited(path, 1000)

        watcher = ci.Watcher(self.cfg, api=api)
        with self.assertRaises(plan_github.GithubRateLimited):
            watcher.discovered_workflows("o/a")
        self.assertEqual(watcher._workflows_cache, {},
                         "un quota épuisé est gardé comme « pas de workflow »")

    def test_garde_ne_masque_pas_le_quota(self):
        logs: list = []
        watcher = ci.Watcher(self.cfg, api=lambda path: {}, log=logs.append)

        def guard_state(db, repo):
            raise plan_github.GithubRateLimited(repo, 1000)

        watcher._guard_state = guard_state
        with self.assertRaises(plan_github.GithubRateLimited):
            watcher._guard(None, "o/a")
        self.assertEqual(logs, [], "le quota est journalisé comme échec de garde")


if __name__ == "__main__":
    unittest.main()
