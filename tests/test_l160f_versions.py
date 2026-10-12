# SPDX-License-Identifier: AGPL-3.0-only
"""L160f (#497) : onglet « Versions » de `ameesh progress` (`--serve` et
`--html`) — dernière version publiée et prochaine release par produit.

Sans base : versions, tags, flux, notes, lecteur GitHub (jeton, cache,
fraîcheur, panne dite) et lots fusionnés avec leur PR lus en base.
Sur Postgres réel : la clé `versions` de l'instantané, son rendu texte.
"""
from __future__ import annotations

import base64
import json
import unittest
import urllib.error
from unittest import mock

from ameesh import plan_github, progress, versions, work

from .support import PgTestCase

NOW = 1_790_000_000.0
REPO = "acme/web"
NOTES = "docs/versions/0.2.11.md"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeApi:
    """Un GitHub en mémoire : `chemin -> JSON`, appels journalisés."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list = []

    def __call__(self, path: str):
        self.calls.append(path)
        if path not in self.routes:
            raise plan_github.GithubError("GitHub %s: HTTP 404" % path)
        return self.routes[path]


def routes(*, tags=(), releases=(), files=None, branch="main") -> dict:
    out = {
        versions.tags_path(REPO): list(tags),
        versions.releases_path(REPO): list(releases),
        versions.repo_path(REPO): {"default_branch": branch},
    }
    for ref, text in (files or {}).items():
        out[versions.file_path(REPO, NOTES, ref)] = {"encoding": "base64",
                                                     "content": b64(text)}
    return out


TAGS = [{"name": "v0.2.10", "commit": {"sha": "abc123"}}]
RELEASES = [{"tag_name": "v0.2.10", "draft": False, "prerelease": False,
             "published_at": "2026-09-01T00:00:00Z",
             "html_url": "https://github.com/acme/web/releases/tag/v0.2.10"}]
MERGED_TS = 1_788_300_000.0     # après la publication du tag, avant NOW


class FakeWork:
    def __init__(self, rows):
        self.rows = rows

    def items(self, state=None, assignee=None, limit=None):
        return [dict(row) for row in self.rows]


class FakeRoadmap:
    def __init__(self, plans=()):
        self.plans = list(plans)

    def item_plans(self, ids):
        wanted = {int(i) for i in ids}
        return [dict(p) for p in self.plans if int(p["id"]) in wanted]


class FakeStore:
    """Le peu de `storage.of(db)` que `versions.collect` lit."""

    def __init__(self, rows, packages=None, package_items=None, plans=()):
        self.work = FakeWork(rows)
        self.roadmap = FakeRoadmap(plans)
        self._packages = packages or {}
        self._package_items = package_items or []

    def packages(self):
        return list(self._packages.values())

    def package_items(self):
        return list(self._package_items)


def rows(*, merged=True, pr=True, branch=None):
    out = [{"id": 1, "title": "Release 0.2.11 : train", "state": "build",
            "app": "nexlink", "issue_ref": "acme/web#10", "updated_ts": NOW - 100,
            "branch": branch, "pr_ref": "acme/web#99" if pr else None}]
    if merged:
        out.append({"id": 2, "title": "Corrige le cache", "state": "merged",
                    "app": "nexlink", "issue_ref": "acme/web#11",
                    "updated_ts": MERGED_TS, "pr_ref": "acme/web#98" if pr else None})
    return out


def collect(api, *, items=None, packages=None, package_items=None, reader=None, now=NOW):
    store = FakeStore(items if items is not None else rows(),
                      packages if packages is not None else {7: {"id": 7, "title": "Cache"}},
                      package_items if package_items is not None
                      else [{"id": 2, "package_id": 7}])
    read = reader if reader is not None else versions.Reader(api, clock=lambda: 0.0,
                                                             wall=lambda: now)
    return versions.collect(store, None, None, reader=read, now=now)


class PureTest(unittest.TestCase):
    def test_tags_et_versions(self):
        self.assertEqual(versions.parse_tag("v1.10.0"),
                         {"tag": "v1.10.0", "prefix": "", "version": "1.10.0",
                          "key": (1, 10, 0, (1,))})
        self.assertEqual(versions.parse_tag("desktop-v0.2.8")["prefix"], "desktop")
        self.assertEqual(versions.parse_tag("desktop-0.2.8")["version"], "0.2.8")
        self.assertIsNone(versions.parse_tag("nightly"))
        self.assertEqual(versions.version_of("Release 0.2.11 : train serveur"), "0.2.11")
        self.assertEqual(versions.version_of("REL-1.9.1"), "1.9.1")
        self.assertIsNone(versions.version_of("Housekeeping"))
        self.assertTrue(versions.is_release_lot("QA-1.10.0"))
        self.assertFalse(versions.is_release_lot("Corrige la QA"))

    def test_pre_versions_semver(self):
        """P2 codex r1 : `0.2.11-rc.1` n'est ni rejetée ni affichée en 0.2.11."""
        rc = versions.parse_tag("v0.2.11-rc.1")
        self.assertEqual(rc["version"], "0.2.11-rc.1")
        self.assertEqual(rc["prefix"], "")
        final = versions.parse_tag("v0.2.11")
        self.assertLess(rc["key"], final["key"])
        self.assertLess(versions.version_key("0.2.11-rc.1"),
                        versions.version_key("0.2.11-rc.2"))
        self.assertLess(versions.version_key("0.2.11-rc.10"),
                        versions.version_key("0.2.11"))
        self.assertEqual(versions.parse_tag("desktop-v0.2.11-rc.1")["prefix"], "desktop")
        self.assertEqual(versions.version_of("Release 0.2.11-rc.1 : train"),
                         "0.2.11-rc.1")
        self.assertEqual(versions.version_of("Release 0.2.11 : train"), "0.2.11")
        self.assertEqual(versions.tag_for("", "0.2.11-rc.1", {"v"}), "v0.2.11-rc.1")
        self.assertEqual(versions.tag_for("desktop", "0.2.11-rc.1", {"v"}),
                         "desktop-v0.2.11-rc.1")

    def test_flux_et_indices(self):
        known = [versions.parse_tag("v1.9.1"), versions.parse_tag("desktop-v0.2.8"),
                 versions.parse_tag("desktop-v0.2.9")]
        for item in known:
            item["sha"] = None
        streams = versions.streams_of(known)
        self.assertEqual([s["prefix"] for s in streams], ["", "desktop"])
        prefixes = {s["prefix"] for s in streams}
        self.assertEqual(versions.stream_of("1.10.0", streams), "")
        self.assertEqual(versions.stream_of("0.2.10", streams), "desktop")
        self.assertEqual(versions.stream_of("1.10.0", streams, "desktop"), "desktop")
        # « serveur/prod » désigne le flux sans préfixe, jamais un produit
        self.assertEqual(versions.release_hint("Release 0.2.11 serveur", prefixes), "")
        self.assertEqual(versions.release_hint("Release desktop 0.2.11", prefixes),
                         "desktop")
        self.assertIsNone(versions.release_hint("Release 0.2.11", prefixes))
        self.assertEqual(versions.tag_for("desktop", "0.2.9", {"v"}), "desktop-v0.2.9")
        self.assertEqual(versions.tag_for("", "1.0.0", set()), "1.0.0")

    def test_libelles(self):
        self.assertEqual(versions._label("nexlink", "", True), "nexlink server/prod")
        self.assertEqual(versions._label("nexlink", "desktop", True), "nexlink desktop")
        self.assertEqual(versions._label("ameesh", "", False), "ameesh")

    def test_notes(self):
        parsed = versions.parse_notes(
            "Résumé de la 0.2.11.\n\n**Cache**\n- partagé par le serveur\n"
            "- et par --html\n**Migrations** : aucune\n")
        self.assertEqual(parsed["summary"], "Résumé de la 0.2.11.")
        self.assertEqual([t["theme"] for t in parsed["themes"]], ["Cache", "Migrations"])
        self.assertEqual(parsed["themes"][0]["items"],
                         ["partagé par le serveur", "et par --html"])
        self.assertEqual(parsed["themes"][1]["items"], ["aucune"])

    def test_pr_et_ages(self):
        self.assertEqual(versions._pr_parts("acme/web#12"), ("acme/web", "12"))
        self.assertIsNone(versions._pr_parts("pas-une-pr"))
        self.assertEqual(versions._pull_url("acme/web#12"),
                         "https://github.com/acme/web/pull/12")
        self.assertEqual(versions._ago(None), "not read")
        self.assertEqual(versions._ago(30), "30 s ago")
        self.assertEqual(versions._ago(180), "3 min ago")
        self.assertEqual(versions._ago(3900), "1 h 05 min ago")
        self.assertEqual(versions._ago(7200), "2 h ago")


class ReaderTest(unittest.TestCase):
    def test_cache_et_fraicheur(self):
        api = FakeApi({versions.tags_path(REPO): [{"name": "v1.0.0"}]})
        clock, wall = [0.0], [1000.0]
        reader = versions.Reader(api, ttl=300.0, clock=lambda: clock[0],
                                 wall=lambda: wall[0])
        self.assertEqual(reader.source, "github")
        self.assertEqual(reader.tags(REPO), [{"name": "v1.0.0"}])
        self.assertEqual(reader.read_ts(), 1000.0)
        self.assertEqual(reader.stamp(versions.tags_path(REPO)), 1000.0)
        clock[0], wall[0] = 200.0, 1200.0
        reader.tags(REPO)                       # servi par le cache
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(reader.read_ts(), 1000.0)   # la fraîcheur ne bouge pas
        clock[0] = 400.0                        # au-delà du cache : relecture
        reader.tags(REPO)
        self.assertEqual(len(api.calls), 2)
        self.assertEqual(reader.read_ts(), 1200.0)

    def test_panne_dite(self):
        api = FakeApi({})
        reader = versions.Reader(api)
        with self.assertRaises(plan_github.GithubError):
            reader.releases(REPO)

    def test_echec_en_cache_et_donnees_perimees(self):
        """P3 codex r1 : un échec est gardé 60 s ; les données précédentes
        restent servies, marquées périmées, jamais une page vide."""
        path = versions.tags_path(REPO)
        api = FakeApi({path: [{"name": "v1.0.0"}]})
        clock = [0.0]
        reader = versions.Reader(api, ttl=10.0, fail_ttl=60.0,
                                 clock=lambda: clock[0], wall=lambda: 1000.0)
        self.assertEqual(reader.tags(REPO), [{"name": "v1.0.0"}])
        self.assertFalse(reader.stale(path))
        clock[0] = 20.0                      # cache expiré, GitHub tombe
        api.routes.pop(path)
        self.assertEqual(reader.tags(REPO), [{"name": "v1.0.0"}])
        self.assertTrue(reader.stale(path))
        self.assertEqual(reader.stamp(path), 1000.0)     # la fraîcheur ne bouge pas
        calls = len(api.calls)
        clock[0] = 30.0
        self.assertEqual(reader.tags(REPO), [{"name": "v1.0.0"}])
        self.assertEqual(len(api.calls), calls)          # échec en cache 60 s
        clock[0] = 90.0
        api.routes[path] = [{"name": "v1.0.1"}]
        self.assertEqual(reader.tags(REPO), [{"name": "v1.0.1"}])
        self.assertFalse(reader.stale(path))

    def test_echec_jamais_lu_est_garde(self):
        api = FakeApi({})
        clock = [0.0]
        reader = versions.Reader(api, fail_ttl=60.0, clock=lambda: clock[0])
        with self.assertRaises(plan_github.GithubError):
            reader.releases(REPO)
        calls = len(api.calls)
        with self.assertRaises(plan_github.GithubError):
            reader.releases(REPO)            # même échec, aucun appel de plus
        self.assertEqual(len(api.calls), calls)

    def test_vue_perimee(self):
        api = FakeApi({versions.releases_path(REPO): RELEASES})
        reader = versions.Reader(api, ttl=10_000.0, clock=lambda: 0.0,
                                 wall=lambda: NOW - 600.0)
        reader.releases(REPO)
        view = versions.github_view(reader, NOW)
        self.assertEqual(view["age_s"], 600.0)
        self.assertTrue(view["stale"])
        self.assertEqual(view["source"], "github")

    def test_rendu_texte_montre_la_fraicheur(self):
        view = versions.empty()
        view["github"] = {"source": "api", "read_ts": NOW - 600.0, "age_s": 600.0,
                          "stale": True}
        lines = versions.format_lines(view)
        self.assertIn("  GitHub (api): read 10 min ago — stale cache", lines)


class ClientTest(unittest.TestCase):
    def test_jeton_ou_repli_local(self):
        self.assertIsInstance(plan_github.client(env={"GH_TOKEN": "ghp_x"}),
                              plan_github.Api)
        self.assertIsInstance(plan_github.client(env={"GITHUB_TOKEN": "ghp_y"}),
                              plan_github.Api)
        self.assertEqual(plan_github.client_name({"GH_TOKEN": "x"}), "api")
        self.assertEqual(plan_github.client_name({}), "gh")
        self.assertIsNone(plan_github.client(env={}, allow_gh=False))
        with mock.patch.object(plan_github, "Gh", lambda *a, **k: "faux-gh"):
            self.assertIsInstance(plan_github.client(env={}), plan_github.GhApi)

    def test_api_get_porte_le_jeton(self):
        seen = {}

        class Reponse:
            def read(self):
                return json.dumps({"ok": True}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def faux_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            return Reponse()

        with mock.patch.object(plan_github.urllib.request, "urlopen", faux_urlopen):
            api = plan_github.Api("ghp_secret", base="https://api.example/")
            self.assertEqual(api.get("repos/acme/web"), {"ok": True})
        self.assertEqual(seen["url"], "https://api.example/repos/acme/web")
        self.assertEqual(seen["auth"], "Bearer ghp_secret")

    def test_api_dit_la_panne_sans_le_jeton(self):
        def faux_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)

        with mock.patch.object(plan_github.urllib.request, "urlopen", faux_urlopen):
            with self.assertRaises(plan_github.GithubError) as ctx:
                plan_github.Api("ghp_secret").get("repos/acme/web")
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertNotIn("ghp_secret", str(ctx.exception))


class CollectTest(unittest.TestCase):
    def test_notes_lues_sur_la_branche_head_de_la_pr(self):
        """P2 codex r1 : les notes se lisent sur la branche head de la PR de
        release, jamais sur `release/vX`, le tag ou la branche par défaut."""
        head = "lot/0.2.11-notes"
        api = FakeApi(routes(
            tags=TAGS, releases=RELEASES,
            files={head: "Résumé de la 0.2.11.\n\n**Cache**\n- partagé par le serveur\n",
                   "release/v0.2.11": "Notes d'une autre référence\n"}))
        view = collect(api, items=rows(branch=head))
        self.assertEqual(view["schema"], versions.SCHEMA)
        self.assertEqual(view["github"]["source"], "github")
        self.assertEqual(view["github"]["age_s"], 0.0)
        self.assertFalse(view["github"]["stale"])
        product = view["products"][0]
        self.assertEqual(product["label"], "nexlink")
        self.assertEqual(product["last"]["tag"], "v0.2.10")
        self.assertEqual(product["last"]["date"], "2026-09-01")
        self.assertEqual(product["last"]["source"], "github")
        self.assertEqual(product["last"]["read_ts"], NOW)
        nxt = product["next"]
        self.assertEqual(nxt["version"], "0.2.11")
        self.assertEqual(nxt["state"], "build")
        self.assertEqual(nxt["pr"]["number"], 99)          # la PR vient de la base
        self.assertEqual(nxt["pr"]["source"], "ameesh work items (sync-github)")
        self.assertEqual(nxt["pr"]["head"], head)          # branche du lot, en base
        self.assertEqual(nxt["notes"]["source"], "PR #99 head %s" % head)
        self.assertEqual(nxt["notes"]["origin"], "github")
        self.assertEqual(nxt["notes"]["read_ts"], NOW)
        self.assertEqual(nxt["summary"], "Résumé de la 0.2.11.")
        self.assertEqual(nxt["themes"], [{"theme": "Cache",
                                          "items": ["partagé par le serveur"]}])
        self.assertEqual(nxt["merged_lots"], [])
        self.assertEqual(nxt["lots"][0]["pr"], "acme/web#99")
        self.assertEqual(nxt["lots"][0]["pr_url"],
                         "https://github.com/acme/web/pull/99")
        # la branche head seulement : ni release/vX, ni le tag, ni main
        self.assertIn(versions.file_path(REPO, NOTES, head), api.calls)
        self.assertNotIn(versions.file_path(REPO, NOTES, "release/v0.2.11"), api.calls)
        self.assertNotIn(versions.file_path(REPO, NOTES, "main"), api.calls)
        # aucun appel à l'API des PR : elles sont en base
        self.assertFalse([c for c in api.calls if "/pulls" in c])

    def test_notes_absentes_de_la_branche_head_sont_dites(self):
        """P2 codex r1 : sinon rien, avec la raison — pas de repli."""
        api = FakeApi(routes(
            tags=TAGS, releases=RELEASES,
            files={"release/v0.2.11": "Notes d'une autre référence\n"}))
        view = collect(api, items=rows(branch="lot/0.2.11-notes"))
        nxt = view["products"][0]["next"]
        self.assertIsNone(nxt["notes"])
        self.assertIn("head branch", nxt["notes_reason"])
        self.assertTrue(any("notes not read" in m for m in nxt["missing"]))
        self.assertEqual(nxt["merged_since"], "v0.2.10")   # repli : lots fusionnés
        self.assertEqual([l["id"] for l in nxt["merged_lots"]], [2])

    def test_repli_lots_fusionnes_et_leur_pr(self):
        api = FakeApi(routes(tags=TAGS, releases=RELEASES))
        view = collect(api)
        nxt = view["products"][0]["next"]
        self.assertIsNone(nxt["notes"])
        self.assertEqual(nxt["merged_since"], "v0.2.10")
        self.assertEqual(nxt["themes"], [{"theme": "Cache", "items": ["Corrige le cache"]}])
        self.assertEqual([l["id"] for l in nxt["merged_lots"]], [2])
        self.assertEqual(nxt["merged_lots"][0]["pr"], "acme/web#98")
        self.assertEqual(nxt["merged_lots"][0]["pr_url"],
                         "https://github.com/acme/web/pull/98")
        self.assertEqual(nxt["merged_lots"][0]["read_ts"], NOW)

    def test_github_illisible_est_dit(self):
        api = FakeApi({})
        view = collect(api)
        product = view["products"][0]
        self.assertIsNone(product["last"])
        self.assertTrue(product["missing"][0].startswith("GitHub unreadable"))
        self.assertIn("acme/web", product["missing"][0])

    def test_sans_lecteur_la_vue_dit_le_manque(self):
        view = versions.collect(FakeStore(rows()), None, None, reader=None, now=NOW)
        self.assertIsNone(view["github"])
        self.assertIsNone(view["read_ts"])
        self.assertEqual(len(view["products"]), 1)        # le produit est nommé
        product = view["products"][0]
        self.assertEqual(product["repo"], "acme/web")
        self.assertIsNone(product["last"])
        self.assertIsNone(product["next"])
        self.assertTrue(any("no GitHub reader" in m for m in product["missing"]))
        self.assertTrue(any("no GitHub reader" in m for m in view["missing"]))
        self.assertTrue(view["products"][0]["missing"])

    def test_deux_flux_et_train_sans_version(self):
        """Conventions mesh-design : (a) « serveur/prod » pour le flux sans
        préfixe ; (b) un lot de release sans version rejoint le seul flux qui
        a une suite (par ex. RELEASE-QA-HOLD avec 0.2.11 desktop)."""
        tags = [{"name": "v1.9.1", "commit": {"sha": "s1"}},
                {"name": "desktop-v0.2.10", "commit": {"sha": "s2"}}]
        releases = [{"tag_name": "v1.9.1", "draft": False, "prerelease": False,
                     "published_at": "2026-08-01T00:00:00Z"},
                    {"tag_name": "desktop-v0.2.10", "draft": False, "prerelease": False,
                     "published_at": "2026-09-01T00:00:00Z"}]
        items = [
            {"id": 1, "title": "REL-0.2.11 : version 0.2.11", "state": "build",
             "app": "nexlink", "issue_ref": "acme/web#1", "updated_ts": NOW},
            {"id": 2, "title": "RELEASE-QA-HOLD : pause d'approbation",
             "state": "intake", "app": "nexlink", "issue_ref": "acme/web#2",
             "updated_ts": NOW},
        ]
        api = FakeApi(routes(tags=tags, releases=releases))
        view = collect(api, items=items, packages={}, package_items=[])
        labels = {p["label"] for p in view["products"]}
        self.assertEqual(labels, {"nexlink server/prod", "nexlink desktop"})
        server = next(p for p in view["products"] if p["label"] == "nexlink server/prod")
        desktop = next(p for p in view["products"] if p["label"] == "nexlink desktop")
        self.assertIsNone(server["next"])                  # aucune suite annoncée
        self.assertEqual(server["last"]["tag"], "v1.9.1")
        self.assertEqual(desktop["next"]["version"], "0.2.11")
        self.assertEqual([lot["id"] for lot in desktop["next"]["lots"]], [1, 2])

    def test_depot_sans_etiquette_montre_ses_lots_de_release(self):
        """P3 codex r1 : sans tag ni release, les lots de release ouverts sont
        quand même montrés ; le manque d'étiquette est dit."""
        api = FakeApi(routes())
        view = collect(api)
        product = view["products"][0]
        self.assertTrue(any("no version tag or release known" in m
                            for m in product["missing"]))
        self.assertIsNone(product["last"])
        self.assertEqual(product["next"]["version"], "0.2.11")
        self.assertEqual(product["next"]["tag"], "v0.2.11")

    def test_versions_indexees_par_produit(self):
        """P2 codex r1 : `v1.0.0` et `desktop-v1.0.0` coexistent, indexés par
        (flux, version) — aucun produit ne disparaît."""
        tags = [{"name": "v1.0.0", "commit": {"sha": "s1"}},
                {"name": "desktop-v1.0.0", "commit": {"sha": "s2"}}]
        releases = [
            {"tag_name": "v1.0.0", "draft": False, "prerelease": False,
             "published_at": "2026-09-01T00:00:00Z"},
            {"tag_name": "desktop-v1.0.0", "draft": False, "prerelease": False,
             "published_at": "2026-09-02T00:00:00Z"}]
        api = FakeApi(routes(tags=tags, releases=releases))
        view = collect(api, items=rows(merged=False, pr=False), packages={},
                       package_items=[])
        labels = {p["label"] for p in view["products"]}
        self.assertEqual(labels, {"nexlink server/prod", "nexlink desktop"})
        by_label = {p["label"]: p for p in view["products"]}
        self.assertEqual(by_label["nexlink server/prod"]["last"]["tag"], "v1.0.0")
        self.assertEqual(by_label["nexlink desktop"]["last"]["tag"], "desktop-v1.0.0")

    def test_brouillon_ne_releve_pas_la_base(self):
        """P2 codex r1 : 0.2.10 publiée + 0.2.11 en brouillon → la prochaine
        release est 0.2.11, « préparée », pas « aucune release annoncée »."""
        tags = [{"name": "v0.2.10", "commit": {"sha": "s1"}},
                {"name": "v0.2.11", "commit": {"sha": "s2"}}]
        releases = [
            dict(RELEASES[0]),
            {"tag_name": "v0.2.11", "draft": True, "prerelease": False,
             "created_at": "2026-09-20T00:00:00Z",
             "html_url": "https://github.com/acme/web/releases/tag/v0.2.11"}]
        items = [{"id": 3, "title": "Ménage interne", "state": "build",
                  "app": "nexlink", "issue_ref": "acme/web#3", "updated_ts": NOW}]
        api = FakeApi(routes(tags=tags, releases=releases))
        view = collect(api, items=items, packages={}, package_items=[])
        product = view["products"][0]
        self.assertEqual(product["last"]["tag"], "v0.2.10")   # la base : publiée
        self.assertEqual(product["last_tag"]["tag"], "v0.2.11")
        self.assertTrue(product["last_tag"]["draft"])
        nxt = product["next"]
        self.assertEqual(nxt["version"], "0.2.11")            # le brouillon, préparé
        self.assertEqual(nxt["state"], "draft")
        self.assertTrue(nxt["release"]["draft"])
        self.assertNotIn("no next release announced", " ".join(product["missing"]))

    def test_pre_version_dans_le_train(self):
        """P2 codex r1 : une pré-version annoncée reste entière, étiquette
        attendue comprise."""
        items = [{"id": 1, "title": "Release 0.2.11-rc.1 : train", "state": "build",
                  "app": "nexlink", "issue_ref": "acme/web#10", "updated_ts": NOW,
                  "branch": "lot/497", "pr_ref": "acme/web#99"}]
        api = FakeApi(routes(tags=TAGS, releases=RELEASES))
        view = collect(api, items=items, packages={}, package_items=[])
        nxt = view["products"][0]["next"]
        self.assertEqual(nxt["version"], "0.2.11-rc.1")
        self.assertEqual(nxt["tag"], "v0.2.11-rc.1")

    def test_lot_fusionne_non_attribue(self):
        """P2 codex r1 : un lot fusionné dont le titre ne nomme aucun produit
        n'apparaît sous aucun des deux, mais dans la rubrique « produit non
        attribué »."""
        tags = [{"name": "v1.9.1", "commit": {"sha": "s1"}},
                {"name": "desktop-v0.2.10", "commit": {"sha": "s2"}}]
        releases = [
            {"tag_name": "v1.9.1", "draft": False, "prerelease": False,
             "published_at": "2026-08-01T00:00:00Z"},
            {"tag_name": "desktop-v0.2.10", "draft": False, "prerelease": False,
             "published_at": "2026-09-01T00:00:00Z"}]
        items = [
            {"id": 1, "title": "Release 1.10.0 serveur : train", "state": "build",
             "app": "nexlink", "issue_ref": "acme/web#1", "updated_ts": NOW},
            {"id": 2, "title": "Release desktop 0.2.11 : train", "state": "build",
             "app": "nexlink", "issue_ref": "acme/web#2", "updated_ts": NOW},
            {"id": 3, "title": "Correctif desktop : icônes", "state": "merged",
             "app": "nexlink", "issue_ref": "acme/web#3", "updated_ts": MERGED_TS},
            {"id": 4, "title": "Corrige le cache", "state": "merged",
             "app": "nexlink", "issue_ref": "acme/web#4", "updated_ts": MERGED_TS},
        ]
        api = FakeApi(routes(tags=tags, releases=releases))
        view = collect(api, items=items, packages={}, package_items=[])
        by_label = {p["label"]: p for p in view["products"]}
        self.assertEqual(set(by_label), {"nexlink server/prod", "nexlink desktop",
                                         "nexlink (product not assigned)"})
        server, desktop = by_label["nexlink server/prod"], by_label["nexlink desktop"]
        self.assertEqual(server["next"]["merged_lots"], [])     # pas de cache ici
        self.assertEqual([l["id"] for l in desktop["next"]["merged_lots"]], [3])
        rubric = by_label["nexlink (product not assigned)"]
        self.assertTrue(rubric["unassigned"])
        self.assertEqual([l["id"] for l in rubric["next"]["merged_lots"]], [4])
        self.assertEqual(rubric["next"]["themes"],
                         [{"theme": "nexlink (product not assigned)",
                           "items": ["Corrige le cache"]}])
        self.assertEqual(rubric["next"]["merged_since"], "v1.9.1")

    def test_date_de_train_non_ferme(self):
        """P2 codex r1 : un lot sans date suffit à ce que la date du train ne
        soit plus ferme — elle est un manque, la date connue une borne."""
        items = [
            {"id": 1, "title": "Release 0.2.11 : lot A", "state": "build",
             "app": "nexlink", "issue_ref": "acme/web#1", "updated_ts": NOW},
            {"id": 2, "title": "Release 0.2.11 : lot B", "state": "intake",
             "app": "nexlink", "issue_ref": "acme/web#2", "updated_ts": NOW},
        ]
        store = FakeStore(items, packages={}, package_items=[],
                          plans=[{"id": 1, "planned_delivery": "2026-10-20",
                                  "planned_source": "mesh-design"}])
        api = FakeApi(routes(tags=TAGS, releases=RELEASES))
        reader = versions.Reader(api, clock=lambda: 0.0, wall=lambda: NOW)
        view = versions.collect(store, None, None, reader=reader, now=NOW)
        nxt = view["products"][0]["next"]
        self.assertIsNone(nxt["date"])
        self.assertEqual(nxt["date_partial"], "2026-10-20")
        self.assertTrue(any("1 of 2 lot(s) have no planned date" in m
                            for m in nxt["missing"]))
        self.assertIn("not firm, latest 2026-10-20",
                      "\n".join(versions.format_lines(view)))

    def test_date_ferme_quand_tous_les_lots_sont_dates(self):
        items = [
            {"id": 1, "title": "Release 0.2.11 : lot A", "state": "build",
             "app": "nexlink", "issue_ref": "acme/web#1", "updated_ts": NOW},
            {"id": 2, "title": "Release 0.2.11 : lot B", "state": "intake",
             "app": "nexlink", "issue_ref": "acme/web#2", "updated_ts": NOW},
        ]
        store = FakeStore(items, packages={}, package_items=[],
                          plans=[{"id": 1, "planned_delivery": "2026-10-20"},
                                 {"id": 2, "planned_delivery": "2026-10-25"}])
        api = FakeApi(routes(tags=TAGS, releases=RELEASES))
        reader = versions.Reader(api, clock=lambda: 0.0, wall=lambda: NOW)
        view = versions.collect(store, None, None, reader=reader, now=NOW)
        nxt = view["products"][0]["next"]
        self.assertEqual(nxt["date"], "2026-10-25")
        self.assertIsNone(nxt["date_partial"])


class SnapshotTest(PgTestCase):
    def test_cle_versions_dans_instantane(self):
        item = work.add(self.db, title="Release 0.2.11 : train", app="demo")
        work.move(self.db, item["id"], "build")
        self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                        ("acme/web#10", item["id"]))
        api = FakeApi(routes(tags=TAGS, releases=RELEASES))
        reader = versions.Reader(api, clock=lambda: 0.0, wall=lambda: NOW)
        snap = progress.snapshot(self.db, self.cfg, github=reader, now=NOW)
        view = snap["versions"]
        self.assertEqual(view["schema"], versions.SCHEMA)
        self.assertEqual(view["github"]["source"], "github")
        product = view["products"][0]
        self.assertEqual(product["project"], "demo")
        self.assertEqual(product["last"]["tag"], "v0.2.10")
        self.assertEqual(product["next"]["version"], "0.2.11")
        text = progress.format_text(snap)
        self.assertIn("VERSIONS", text)
        self.assertIn("next 0.2.11", text)

    def test_sans_lecteur_la_cle_existe(self):
        snap = progress.snapshot(self.db, self.cfg, github=None, now=NOW)
        view = snap["versions"]
        self.assertIsNone(view["github"])
        self.assertTrue(any("no GitHub reader" in m for m in view["missing"]))
        self.assertIn("VERSIONS", progress.format_text(snap))
        self.assertTrue(progress.build_parser().parse_args(["--no-versions"]).no_versions)
