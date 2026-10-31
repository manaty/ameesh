# SPDX-License-Identifier: AGPL-3.0-only
"""#4291 / #3264 A : veille quotidienne des versions de harnais et de modèles.

Le témoin de la spec est rejoué sans réseau : dsh 0.2.0-rc.2 installée,
0.2.1-rc.4 publiée, 0.2.2-alpha.2 ignorée. Les autres cas couvrent l'alpha
seule, la deuxième version qui met à jour le même lot, l'écart entre hôtes, la
priorité 1 sur recommandation, le modèle servi jamais facturé et la
non-réouverture d'un objet déjà évalué.
"""
from __future__ import annotations

from .entrypoint import run as isolated_entrypoint

import dataclasses
import json
import types
import unittest
from unittest import mock

from ameesh import harness_adoption, hostcheck, storage, veille, veille_cli, work
from ameesh.discovery.base import ModelSeen, SourceResult

from .support import PgTestCase

CHANGELOG = "https://example.invalid/dsh/changelog"
NPM_URL = "https://registry.npmjs.org/@org/dsh"
DATES = {
    "0.2.0-rc.2": "2026-09-29T10:00:00Z",
    "0.2.0": "2026-10-01T10:00:00Z",
    "0.2.1-rc.1": "2026-10-20T10:00:00Z",
    "0.2.1-rc.4": "2026-10-27T10:00:00Z",
    "0.2.1-rc.5": "2026-10-30T10:00:00Z",
    "0.2.2-alpha.2": "2026-10-28T10:00:00Z",
}


def npm_doc(versions, tags=None):
    time = {"created": "2026-09-01T00:00:00Z", "modified": "2026-10-28T00:00:00Z"}
    for version in versions:
        time[version] = DATES.get(version, "2026-10-28T10:00:00Z")
    return json.dumps({"name": "@org/dsh", "dist-tags": tags or {},
                       "versions": {version: {} for version in versions},
                       "time": time})


def source(**extra):
    base = {"harness": "deepseek", "npm": "@org/dsh", "changelog": CHANGELOG}
    base.update(extra)
    return veille.Source.from_config(base, "test")


class VersionsTest(unittest.TestCase):
    def test_watched_versions(self):
        for version in ("0.2.0", "0.2.1-rc.4", "1.0", "v1.2.3"):
            with self.subTest(version=version):
                self.assertTrue(veille.watched(version))
        for version in ("0.2.2-alpha.2", "0.2.2-beta.1", "1.0.0-pre.1", "x"):
            with self.subTest(version=version):
                self.assertFalse(veille.watched(version))
        self.assertTrue(veille.is_rc("0.2.1-rc.4"))
        self.assertFalse(veille.is_rc("0.2.1"))

    def test_npm_keeps_stable_and_rc_and_ignores_alpha(self):
        releases, ignored = veille.parse_npm(json.loads(npm_doc(
            ["0.2.0-rc.2", "0.2.0", "0.2.1-rc.4", "0.2.2-alpha.2"],
            tags={"latest": "0.2.0", "rc": "0.2.1-rc.4"})))
        self.assertEqual([release.version for release in releases],
                         ["0.2.0-rc.2", "0.2.0", "0.2.1-rc.4"])
        self.assertEqual([release.kind for release in releases],
                         ["rc", "stable", "rc"])
        self.assertEqual(releases[-1].date, "2026-10-27")
        self.assertEqual(releases[-1].tag, "rc")
        self.assertEqual(ignored, ["0.2.2-alpha.2"])

    def test_releases_ignore_drafts_and_alpha(self):
        releases, ignored = veille.parse_releases([
            {"tag_name": "v0.2.1-rc.4", "published_at": "2026-10-27T00:00:00Z",
             "html_url": "https://example.invalid/r/rc4"},
            {"tag_name": "v0.2.2-alpha.2", "published_at": "2026-10-28T00:00:00Z"},
            {"tag_name": "v0.2.1", "published_at": "2026-10-29T00:00:00Z",
             "draft": True},
            {"tag_name": "v0.2.0", "published_at": "2026-10-01T00:00:00Z"},
        ])
        self.assertEqual([release.version for release in releases],
                         ["0.2.0", "0.2.1-rc.4"])
        self.assertEqual(ignored, ["0.2.2-alpha.2"])
        self.assertEqual(releases[-1].url, "https://example.invalid/r/rc4")

    def test_published_supports_github_releases(self):
        class Api:
            path = ""

            def get(self, path):
                self.path = path
                return [{"tag_name": "v1.2.3", "published_at": "2026-10-01T00:00:00Z",
                         "html_url": "https://example.invalid/r/v1.2.3"}]

        api = Api()
        out = veille.published(
            veille.Source.from_config({"harness": "codex",
                                       "releases": "openai/codex"}, "test"),
            github_api=api)
        self.assertEqual(api.path, "/repos/openai/codex/releases?per_page=100")
        self.assertEqual(out["source"], "releases")
        self.assertEqual([row["version"] for row in out["releases"]], ["1.2.3"])
        self.assertFalse(out["error"])

    def test_recommendation_or_later_and_model(self):
        found = veille.parse_recommendation(
            "Upgrade guide: DeepSeek Harness 0.2.1-rc.1 or later is required "
            "for DeepSeek-V4.1-Flash-1017.", models=["DeepSeek-V4.1-Flash-1017"])
        self.assertEqual(found["version"], "0.2.1-rc.1")
        self.assertEqual(found["model"], "DeepSeek-V4.1-Flash-1017")
        self.assertIsNone(veille.parse_recommendation("No recommendation here.")["version"])
        # an alpha is never a recommended release
        self.assertIsNone(veille.parse_recommendation(
            "version 0.2.2-alpha.2 or later")["version"])
        # a dependency requirement does not win over the harness recommendation
        found = veille.parse_recommendation(
            "Node.js 20.0.0 or later is required. DeepSeek Harness "
            "0.2.1-rc.1 or later is recommended.", names=("deepseek",))
        self.assertEqual(found["version"], "0.2.1-rc.1")

    def test_npm_document_cap_accepts_a_large_packument(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size):
                return self.body[:size]

        body = b"x" * 9_000_000
        with mock.patch("ameesh.veille.urllib.request.urlopen",
                        return_value=Response(body)):
            with self.assertRaises(veille.VeilleError):
                veille._http_get_text("https://example.invalid/npm")
            self.assertEqual(
                len(veille._http_get_text("https://example.invalid/npm",
                                          max_bytes=10_000_000)), 9_000_000)
        # the codex packument measured 18.3 MB on 28/10/2026 (B2 #4291)
        self.assertGreater(veille.NPM_MAX_BYTES, 18_300_000)

    def test_settings_are_strict_and_generic(self):
        good = types.SimpleNamespace(veille={
            "sources": [{"harness": "deepseek", "npm": "@org/dsh",
                         "changelog": CHANGELOG, "provider": "deepseek"}],
            "app": "ameesh", "estimate": "4h", "host": "pc", "model_days": 7})
        parsed = veille.settings(good)
        self.assertEqual(parsed.app, "ameesh")
        self.assertEqual(parsed.estimate, "4h")
        self.assertEqual(parsed.host, "pc")
        self.assertEqual(parsed.model_days, 7.0)
        self.assertEqual(parsed.sources[0].harness, "deepseek")
        self.assertEqual(parsed.sources[0].provider, "deepseek")
        for block in (
                {"sources": [{"harness": "deepseek"}]},
                {"sources": [{"harness": "deepseek", "npm": "@org/dsh",
                              "releases": "org/dsh"}]},
                {"sources": [{"harness": "deepseek", "npm": "@org/dsh",
                              "oops": 1}]},
                {"sources": [{"harness": "deepseek", "npm": "@org/dsh"},
                             {"harness": "deepseek", "npm": "@org/other"}]},
                {"sources": [{"harness": "deepseek", "npm": "@org/dsh",
                              "provider": "nope"}]},
                {"sources": [{"harness": "deepseek", "npm": "@org/dsh"}],
                 "estimate": "later"},
                {"sources": [{"harness": "deepseek", "npm": "@org/dsh"}],
                 "model_days": 0}):
            with self.subTest(block=block), self.assertRaises(veille.VeilleError):
                veille.settings(types.SimpleNamespace(veille=block))
        self.assertEqual(veille.settings(types.SimpleNamespace(veille={})).sources, ())

    def test_briefs_depend_on_the_evaluated_object(self):
        installed = {"hosts": {"vm": {"version": "1.0.0"}}, "newest": "1.0.0",
                     "differs": False, "missing": []}
        published = {"source": "npm", "error": "", "ignored": [],
                     "releases": [{"version": "1.1.0", "date": "2026-10-01",
                                   "source": "npm", "url": "", "tag": ""}]}
        decision = veille.evaluate(source(), installed, published, {}, {},
                                   ["ref-model"])
        body = decision["body"]
        self.assertIn("même modèle", body)
        self.assertIn("modèle de référence réellement facturé : `ref-model`", body)
        self.assertIn("version installée `1.0.0` contre version évaluée `1.1.0`",
                      body)
        self.assertNotIn("même harnais", body)
        # a model object keeps the harness constant and names the billed
        # reference; it never asks for the same model as the reference
        equal = {"source": "npm", "error": "", "ignored": [],
                 "releases": [{"version": "1.0.0", "date": "2026-09-01",
                               "source": "npm", "url": "", "tag": ""}]}
        models = {"served": ["old", "new"], "new": ["new"], "complete": True}
        decision = veille.evaluate(source(), installed, equal, {}, models, ["old"])
        self.assertEqual(decision["object"],
                         {"kind": "model", "name": "new", "version": "new"})
        body = decision["body"]
        self.assertIn("même harnais (`deepseek`)", body)
        self.assertIn("modèle de référence réellement facturé `old`", body)
        self.assertIn("modèle évalué `new`", body)
        self.assertNotIn("même modèle", body)
        decision = veille.evaluate(source(), installed, equal, {}, models, [])
        self.assertIn("aucun modèle facturé connu", decision["body"])

    def test_evaluate_witness_and_priority(self):
        installed = {"hosts": {"vm-a": {"version": "0.2.0-rc.2"}},
                     "newest": "0.2.0-rc.2", "differs": False, "missing": []}
        published_row = {
            "source": "npm", "error": "",
            "releases": [{"version": "0.2.1-rc.4", "date": "2026-10-27",
                          "source": "npm", "url": "", "tag": "rc"}],
            "ignored": ["0.2.2-alpha.2"]}
        decision = veille.evaluate(source(), installed, published_row, {}, {}, [])
        self.assertEqual(decision["object"],
                         {"kind": "harness", "name": "deepseek",
                          "version": "0.2.1-rc.4"})
        self.assertEqual(decision["priority"], 2)
        self.assertIn("0.2.2-alpha.2", decision["body"])
        self.assertIn("vm-a : 0.2.0-rc.2", decision["body"])
        self.assertIn(CHANGELOG, decision["body"])
        rec = {"version": "0.2.1-rc.1", "model": None}
        decision = veille.evaluate(source(), installed, published_row, rec, {}, [])
        self.assertEqual(decision["priority"], 1)
        self.assertIn("Recommandation du fournisseur", decision["body"])
        # a requirement on another tool (Node.js 20 or later) is not a
        # recommendation for this harness: no priority, no misleading body
        rec = {"version": "20.0.0", "model": None}
        decision = veille.evaluate(source(), installed, published_row, rec, {}, [])
        self.assertEqual(decision["priority"], 2)
        self.assertNotIn("Recommandation du fournisseur", decision["body"])
        # alpha only: no candidate
        published_row["releases"] = [
            {"version": "0.2.2-alpha.2", "date": "2026-10-28", "source": "npm",
             "url": "", "tag": ""}]
        decision = veille.evaluate(source(), installed, published_row, {}, {}, [])
        self.assertIsNone(decision["object"])


class VeilleDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM harness_versions")
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM model_catalog")
        self.db.execute("DELETE FROM model_harness")
        self.db.execute("DELETE FROM veille_readings")

    # -- helpers ---------------------------------------------------------
    def configure(self, *, provider="", changelog=CHANGELOG, host="vm-a",
                  designated="", **extra):
        entry = {"harness": "deepseek", "npm": "@org/dsh"}
        if changelog:
            entry["changelog"] = changelog
        if provider:
            entry["provider"] = provider
        block = {"sources": [entry], "app": "ameesh"}
        if designated:
            block["host"] = designated
        block.update(extra)
        return dataclasses.replace(self.cfg, host=host, veille=block)

    def record(self, host, version):
        storage.of(self.db).hosts.record_harness_version(
            host, "deepseek", path="/opt/dsh", version=version, raw=version)

    def catalog(self, *models):
        storage.of(self.db).catalog.upsert_models([
            {"provider": "deepseek", "model_id": name, "source": "deepseek"}
            for name in models])

    def readings(self, *models):
        storage.of(self.db).veille.record_readings(
            "deepseek", "deepseek", list(models))

    def fetch(self, versions, *, tags=None, changelog=""):
        doc = npm_doc(versions, tags)

        def get(url, headers=None):
            if url == CHANGELOG:
                if not changelog:
                    raise OSError("no changelog in this test")
                return changelog
            if url == NPM_URL:
                return doc
            raise AssertionError("unexpected URL %r" % url)
        return get

    def scan(self, versions, *, tags=None, changelog="", provider="", listing=None,
             dry_run=False):
        return veille.scan(
            self.configure(provider=provider), self.db, probe=False, dry_run=dry_run,
            http_get=self.fetch(versions, tags=tags, changelog=changelog),
            listing=listing)

    def lots(self, harness="deepseek"):
        return work.with_source(self.db, "veille:%s" % harness)

    # -- acceptance ------------------------------------------------------
    def test_witness_opens_one_lot_and_ignores_alpha(self):
        self.record("vm-a", "0.2.0-rc.2")
        report = self.scan(["0.2.0-rc.2", "0.2.1-rc.4", "0.2.2-alpha.2"],
                           tags={"latest": "0.2.0-rc.2", "rc": "0.2.1-rc.4"})
        self.assertTrue(report["enabled"])
        self.assertTrue(report["reconciled"])
        self.assertEqual(report["actions"][0]["action"], "created", report)
        lot = work.get(self.db, report["actions"][0]["lot"])
        self.assertEqual(lot["title"], "Évaluer deepseek 0.2.1-rc.4")
        self.assertEqual(lot["priority"], 2)
        self.assertIsNone(lot["assignee"])
        self.assertEqual(lot["source"], "veille:deepseek")
        self.assertIn("<!-- veille: harness=deepseek object=harness:deepseek "
                      "version=0.2.1-rc.4 -->", lot["body"])
        self.assertIn("vm-a : 0.2.0-rc.2", lot["body"])
        self.assertIn("2026-10-27", lot["body"])
        self.assertIn(CHANGELOG, lot["body"])
        self.assertIn("0.2.2-alpha.2", lot["body"])
        self.assertIn("Écart entre hôtes : aucun", lot["body"])

    def test_alpha_only_opens_nothing(self):
        self.record("vm-a", "0.2.0-rc.2")
        report = self.scan(["0.2.0-rc.2", "0.2.2-alpha.2"],
                           tags={"latest": "0.2.0-rc.2"})
        self.assertEqual(report["actions"][0]["action"], "none")
        self.assertEqual(self.lots(), [])

    def test_second_version_updates_the_same_lot(self):
        self.record("vm-a", "0.2.0-rc.2")
        first = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"],
                          tags={"rc": "0.2.1-rc.4"})
        lot_id = first["actions"][0]["lot"]
        # a human priority is never lowered by the automatic refresh
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (lot_id,))
        second = self.scan(["0.2.0-rc.2", "0.2.1-rc.4", "0.2.1-rc.5"],
                           tags={"rc": "0.2.1-rc.5"})
        self.assertEqual(second["actions"][0]["action"], "updated", second)
        self.assertEqual(second["actions"][0]["lot"], lot_id)
        self.assertEqual(len(self.lots()), 1)
        lot = work.get(self.db, lot_id)
        self.assertEqual(lot["title"], "Évaluer deepseek 0.2.1-rc.5")
        self.assertEqual(lot["priority"], 1)
        self.assertIn("version=0.2.1-rc.5", lot["body"])
        notes = [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (lot_id,))]
        self.assertTrue(any("0.2.1-rc.4 → 0.2.1-rc.5" in note for note in notes),
                        notes)

    def test_host_gap_is_signalled_in_the_lot(self):
        self.record("vm-a", "0.2.0-rc.2")
        self.record("vm-b", "0.2.0-rc.1")
        report = self.scan(["0.2.0-rc.1", "0.2.0-rc.2", "0.2.1-rc.4"],
                           tags={"rc": "0.2.1-rc.4"})
        lot = work.get(self.db, report["actions"][0]["lot"])
        self.assertIn("vm-b : 0.2.0-rc.1", lot["body"])
        self.assertIn("**Écart entre hôtes : oui**", lot["body"])

    def test_vendor_recommendation_gives_priority_one(self):
        self.record("vm-a", "0.2.0-rc.2")
        report = self.scan(
            ["0.2.0-rc.2", "0.2.1-rc.4"], tags={"rc": "0.2.1-rc.4"},
            changelog="DeepSeek Harness 0.2.1-rc.1 or later is recommended.")
        lot = work.get(self.db, report["actions"][0]["lot"])
        self.assertEqual(lot["priority"], 1)
        self.assertIn("Recommandation du fournisseur : version `0.2.1-rc.1`",
                      lot["body"])

    def test_no_installed_reading_opens_nothing(self):
        report = self.scan(["0.2.1-rc.4"], tags={"rc": "0.2.1-rc.4"})
        self.assertEqual(report["actions"][0]["action"], "none")
        self.assertIn("aucune version installée", report["actions"][0]["reason"])
        self.assertEqual(self.lots(), [])

    def test_evaluated_object_is_not_reopened(self):
        self.record("vm-a", "0.2.0-rc.2")
        first = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"], tags={"rc": "0.2.1-rc.4"})
        lot_id = first["actions"][0]["lot"]
        self.db.execute("UPDATE work_items SET state = 'closed',"
                        " close_reason = 'done' WHERE id = %s", (lot_id,))
        again = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"], tags={"rc": "0.2.1-rc.4"})
        # l'objet déjà évalué est écarté avant la sélection : plus d'objet,
        # donc aucun lot rouvert (B de cx2, #4291)
        self.assertEqual(again["actions"][0]["action"], "none")
        self.assertIn("déjà évaluée", again["actions"][0]["reason"])
        self.assertEqual(len(self.lots()), 1)
        # a NEWER version is compared, and opens a new lot
        newer = self.scan(["0.2.0-rc.2", "0.2.1-rc.4", "0.2.1-rc.5"],
                          tags={"rc": "0.2.1-rc.5"})
        self.assertEqual(newer["actions"][0]["action"], "created", newer)
        self.assertEqual(newer["actions"][0]["lot"] != lot_id, True)

    def test_model_served_and_never_billed_opens_a_model_lot(self):
        self.record("vm-a", "0.2.1-rc.4")
        self.readings("deepseek-old")
        storage.of(self.db).turn_costs.insert(
            agent="a", harness="deepseek", turn="t1", model="deepseek-old",
            session="s", usd=0.0, input_tokens=1, cached_input_tokens=0,
            output_tokens=1, cum_usd=None, cum_input_tokens=1,
            cum_cached_input_tokens=0, cum_output_tokens=1)
        listing = lambda: SourceResult(
            source="deepseek", complete=True,
            models=(ModelSeen(provider="deepseek", model_id="deepseek-old"),
                    ModelSeen(provider="deepseek", model_id="deepseek-new")))
        report = self.scan(["0.2.1-rc.4"], tags={"latest": "0.2.1-rc.4"},
                           provider="deepseek", listing=listing)
        self.assertEqual(report["actions"][0]["action"], "created", report)
        lot = work.get(self.db, report["actions"][0]["lot"])
        self.assertIn("Évaluer le modèle deepseek-new", lot["title"])
        self.assertIn("deepseek-old", lot["body"])
        self.assertIn("deepseek-new", lot["body"])
        self.assertIn("Nouveaux depuis le relevé précédent", lot["body"])
        self.assertEqual(lot["priority"], 2)

    def test_known_model_never_billed_does_not_open_a_lot(self):
        self.record("vm-a", "0.2.1-rc.4")
        self.readings("deepseek-old")
        listing = lambda: SourceResult(
            source="deepseek", complete=True,
            models=(ModelSeen(provider="deepseek", model_id="deepseek-old"),))
        report = self.scan(["0.2.1-rc.4"], tags={"latest": "0.2.1-rc.4"},
                           provider="deepseek", listing=listing)
        self.assertEqual(report["actions"][0]["action"], "none", report)
        self.assertEqual(self.lots(), [])

    def test_harness_lot_does_not_catalogue_an_unevaluated_model(self):
        self.record("vm-a", "0.2.0-rc.2")
        self.readings("deepseek-old")
        listing = lambda: SourceResult(
            source="deepseek", complete=True,
            models=(ModelSeen(provider="deepseek", model_id="deepseek-old"),
                    ModelSeen(provider="deepseek", model_id="deepseek-new")))
        first = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"],
                          tags={"rc": "0.2.1-rc.4"}, provider="deepseek",
                          listing=listing)
        self.assertEqual(first["actions"][0]["action"], "created", first)
        known = {row["model_id"] for row in storage.of(self.db).catalog.listing()}
        self.assertNotIn("deepseek-new", known)
        # once the harness lot is closed and the hosts are on the evaluated
        # version, the pending model opens its own lot
        self.db.execute("UPDATE work_items SET state = 'closed',"
                        " close_reason = 'done' WHERE id = %s",
                        (first["actions"][0]["lot"],))
        self.record("vm-a", "0.2.1-rc.4")
        second = self.scan(["0.2.1-rc.4"], tags={"latest": "0.2.1-rc.4"},
                           provider="deepseek", listing=listing)
        self.assertEqual(second["actions"][0]["action"], "created", second)
        lot = work.get(self.db, second["actions"][0]["lot"])
        self.assertIn("Évaluer le modèle deepseek-new", lot["title"])

    def test_two_new_models_keep_one_pending(self):
        self.record("vm-a", "1.0")
        self.readings("deepseek-old")
        listing = lambda: SourceResult(
            source="deepseek", complete=True,
            models=(ModelSeen(provider="deepseek", model_id="deepseek-old"),
                    ModelSeen(provider="deepseek", model_id="deepseek-new1"),
                    ModelSeen(provider="deepseek", model_id="deepseek-new2")))
        first = self.scan(["1.0"], tags={"latest": "1.0"},
                          provider="deepseek", listing=listing)
        self.assertEqual(first["actions"][0]["action"], "created", first)
        first_lot = work.get(self.db, first["actions"][0]["lot"])
        self.assertIn("deepseek-new2", first_lot["title"])
        known = {row["model_id"] for row in storage.of(self.db).catalog.listing()}
        self.assertEqual(known, {"deepseek-old", "deepseek-new2"})
        # the second new model is still pending and must not replace the open
        # model lot
        second = self.scan(["1.0"], tags={"latest": "1.0"},
                           provider="deepseek", listing=listing)
        self.assertEqual(second["actions"][0]["action"], "kept-object", second)
        known = {row["model_id"] for row in storage.of(self.db).catalog.listing()}
        self.assertNotIn("deepseek-new1", known)
        self.db.execute("UPDATE work_items SET state = 'closed',"
                        " close_reason = 'done' WHERE id = %s",
                        (first["actions"][0]["lot"],))
        third = self.scan(["1.0"], tags={"latest": "1.0"},
                          provider="deepseek", listing=listing)
        self.assertEqual(third["actions"][0]["action"], "created", third)
        lot = work.get(self.db, third["actions"][0]["lot"])
        self.assertIn("deepseek-new1", lot["title"])

    def test_open_model_lot_is_not_rewritten_by_a_new_rc(self):
        self.record("vm-a", "1.0")
        self.readings("deepseek-old")
        listing = lambda: SourceResult(
            source="deepseek", complete=True,
            models=(ModelSeen(provider="deepseek", model_id="deepseek-old"),
                    ModelSeen(provider="deepseek", model_id="deepseek-model")))
        first = self.scan(["1.0"], tags={"latest": "1.0"},
                          provider="deepseek", listing=listing)
        self.assertEqual(first["actions"][0]["action"], "created", first)
        lot_id = first["actions"][0]["lot"]
        # an rc is published while the model lot is open: the harness
        # candidate is recomputed every pass, the model object is not lost
        second = self.scan(["1.0", "1.1"], tags={"latest": "1.1"},
                           provider="deepseek", listing=listing)
        self.assertEqual(second["actions"][0]["action"], "kept-object", second)
        lot = work.get(self.db, lot_id)
        self.assertIn("Évaluer le modèle deepseek-model", lot["title"])
        self.assertIn("object=model:deepseek-model", lot["body"])
        known = {row["model_id"] for row in storage.of(self.db).catalog.listing()}
        self.assertIn("deepseek-model", known)

    def test_open_adoption_keeps_the_harness_lot(self):
        """#4312 (interaction A x C) : A ne réécrit pas le lot qui porte une
        adoption ouverte ; une fois l'adoption terminale, il reprend."""
        self.record("vm-a", "0.2.0-rc.2")
        first = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"],
                          tags={"rc": "0.2.1-rc.4"})
        self.assertEqual(first["actions"][0]["action"], "created", first)
        lot_id = first["actions"][0]["lot"]
        self.db.execute(
            "INSERT INTO harness_adoptions (lot_id, project, harness, object,"
            " from_version, to_version, stage, report, report_digest, state)"
            " VALUES (%s, 'ameesh', 'deepseek', 'harness', '0.2.0-rc.2',"
            " '0.2.1-rc.4', 'canary', '{}'::jsonb, 'x', %s::jsonb)",
            (lot_id, json.dumps({"schema": harness_adoption.STATE_SCHEMA,
                                 "stage": "canary"})))
        second = self.scan(["0.2.0-rc.2", "0.2.1-rc.4", "0.2.1-rc.5"],
                           tags={"rc": "0.2.1-rc.5"})
        self.assertEqual(second["actions"][0]["action"], "kept-object", second)
        self.assertEqual(work.get(self.db, lot_id)["title"],
                         "Évaluer deepseek 0.2.1-rc.4")

        self.db.execute(
            "UPDATE harness_adoptions SET stage = 'done', "
            "state = jsonb_set(state, '{stage}', '\"done\"') "
            "WHERE lot_id = %s", (lot_id,))
        third = self.scan(["0.2.0-rc.2", "0.2.1-rc.4", "0.2.1-rc.5"],
                          tags={"rc": "0.2.1-rc.5"})
        self.assertEqual(third["actions"][0]["action"], "updated", third)
        self.assertEqual(work.get(self.db, lot_id)["title"],
                         "Évaluer deepseek 0.2.1-rc.5")

    def test_discover_before_scan_does_not_hide_a_new_model(self):
        # `models discover` écrit au catalogue avant la veille : la présence au
        # catalogue n'est pas un relevé de la veille (B de l'auditeur, #4291)
        self.record("vm-a", "1.0")
        self.readings("deepseek-old")
        self.catalog("deepseek-old", "deepseek-new")
        listing = lambda: SourceResult(
            source="deepseek", complete=True,
            models=(ModelSeen(provider="deepseek", model_id="deepseek-old"),
                    ModelSeen(provider="deepseek", model_id="deepseek-new")))
        first = self.scan(["1.0"], tags={"latest": "1.0"},
                          provider="deepseek", listing=listing)
        self.assertEqual(first["actions"][0]["action"], "created", first)
        lot = work.get(self.db, first["actions"][0]["lot"])
        self.assertIn("Évaluer le modèle deepseek-new", lot["title"])
        # le relevé de la veille a avancé : pas de doublon au passage suivant
        second = self.scan(["1.0"], tags={"latest": "1.0"},
                           provider="deepseek", listing=listing)
        self.assertNotEqual(second["actions"][0]["action"], "created", second)

    def test_evaluated_rc_does_not_hide_a_new_model(self):
        # B de cx2 (#4291) : la rc 0.2.1-rc.4 a été évaluée puis close sans
        # adoption (le parc reste en 0.2.0-rc.2). Elle ne doit pas être
        # rechoisie au passage suivant et masquer le modèle nouveau : le lot
        # modèle s'ouvre, puis aucun doublon.
        self.record("vm-a", "0.2.0-rc.2")
        first = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"],
                          tags={"rc": "0.2.1-rc.4"})
        self.assertEqual(first["actions"][0]["action"], "created", first)
        self.db.execute("UPDATE work_items SET state = 'closed',"
                        " close_reason = 'abandoned' WHERE id = %s",
                        (first["actions"][0]["lot"],))
        self.readings("deepseek-old")
        listing = lambda: SourceResult(
            source="deepseek", complete=True,
            models=(ModelSeen(provider="deepseek", model_id="deepseek-old"),
                    ModelSeen(provider="deepseek", model_id="deepseek-new")))
        second = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"],
                           tags={"rc": "0.2.1-rc.4"}, provider="deepseek",
                           listing=listing)
        self.assertEqual(second["actions"][0]["action"], "created", second)
        lot = work.get(self.db, second["actions"][0]["lot"])
        self.assertIn("Évaluer le modèle deepseek-new", lot["title"])
        # la rc close n'est pas rouverte et le modèle n'est pas dupliqué
        third = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"],
                          tags={"rc": "0.2.1-rc.4"}, provider="deepseek",
                          listing=listing)
        self.assertEqual(third["actions"][0]["action"], "none", third)
        self.assertEqual(len(self.lots()), 2)

    def test_two_harnesses_same_provider_keep_their_own_reading(self):
        # deux harnais du même fournisseur : le relevé de l'un ne peut pas
        # absorber le modèle nouveau de l'autre (B de l'auditeur, #4291)
        self.record("vm-a", "1.0")
        storage.of(self.db).hosts.record_harness_version(
            "vm-a", "deepseek-b", path="/opt/dsh", version="1.0", raw="1.0")
        storage.of(self.db).veille.record_readings(
            "deepseek", "deepseek", ["deepseek-old"])
        storage.of(self.db).veille.record_readings(
            "deepseek-b", "deepseek", ["deepseek-old"])
        source = {"harness": "deepseek", "npm": "@org/dsh",
                  "changelog": CHANGELOG, "provider": "deepseek"}
        other = dict(source, harness="deepseek-b")
        cfg = dataclasses.replace(
            self.cfg, host="vm-a",
            veille={"sources": [source, other], "app": "ameesh"})
        listing = lambda: SourceResult(
            source="deepseek", complete=True,
            models=(ModelSeen(provider="deepseek", model_id="deepseek-old"),
                    ModelSeen(provider="deepseek", model_id="deepseek-new")))
        report = veille.scan(
            cfg, self.db, probe=False,
            http_get=self.fetch(["1.0"], tags={"latest": "1.0"}),
            listing=listing)
        actions = {action["harness"]: action for action in report["actions"]}
        self.assertEqual(actions["deepseek"]["action"], "created", report)
        self.assertEqual(actions["deepseek-b"]["action"], "created", report)
        self.assertNotEqual(actions["deepseek"]["lot"],
                            actions["deepseek-b"]["lot"])

    def test_canary_keeps_the_harness_lot_object(self):
        self.record("vm-a", "0.2.0-rc.2")
        first = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"],
                          tags={"rc": "0.2.1-rc.4"})
        lot_id = first["actions"][0]["lot"]
        # the canary moves the host to the evaluated version; candidate is then
        # None, and a new model must not overwrite the open harness lot (B1)
        self.record("vm-a", "0.2.1-rc.4")
        self.readings("deepseek-old")
        listing = lambda: SourceResult(
            source="deepseek", complete=True,
            models=(ModelSeen(provider="deepseek", model_id="deepseek-old"),
                    ModelSeen(provider="deepseek", model_id="deepseek-reasoner")))
        report = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"],
                           tags={"rc": "0.2.1-rc.4"}, provider="deepseek",
                           listing=listing)
        self.assertEqual(report["actions"][0]["action"], "kept-object", report)
        lot = work.get(self.db, lot_id)
        self.assertEqual(lot["title"], "Évaluer deepseek 0.2.1-rc.4")
        self.assertIn("version=0.2.1-rc.4", lot["body"])
        # the deferred model was not catalogued as a previous reading, so its
        # signal survives until the harness lot closes
        known = {row["model_id"] for row in storage.of(self.db).catalog.listing()}
        self.assertNotIn("deepseek-reasoner", known)

    def test_dry_run_writes_nothing(self):
        self.record("vm-a", "0.2.0-rc.2")
        report = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"],
                           tags={"rc": "0.2.1-rc.4"}, dry_run=True)
        self.assertEqual(report["actions"][0]["action"], "would-create")
        self.assertEqual(self.lots(), [])

    def test_dry_run_probes_without_recording(self):
        """L'appel normal de la veille (`probe=True`, sans `--no-probe`) ne
        persiste pas la sonde en dry-run : « write nothing » vaut aussi pour
        `harness_versions` (#4438). Le dry-run juge sur l'état frais."""
        cfg = self.configure()
        probe = {"harness": "deepseek", "path": "/opt/dsh",
                 "version": "0.2.0-rc.2", "raw": "dsh 0.2.0-rc.2",
                 "error": None}
        before = storage.of(self.db).hosts.harness_versions()
        with mock.patch("ameesh.harness_versions.probe", return_value=probe):
            report = veille.scan(
                cfg, self.db, probe=True, dry_run=True,
                http_get=self.fetch(["0.2.0-rc.2", "0.2.1-rc.4"],
                                    tags={"rc": "0.2.1-rc.4"}))
        self.assertEqual(report["probed"], [probe])
        self.assertEqual(
            report["sources"][0]["installed"]["hosts"],
            {"vm-a": {"version": "0.2.0-rc.2", "path": "/opt/dsh"}})
        self.assertEqual(report["actions"][0]["action"], "would-create")
        self.assertEqual(storage.of(self.db).hosts.harness_versions(), before)
        self.assertEqual(self.lots(), [])

    def test_scan_records_the_probe_outside_dry_run(self):
        """Le même passage hors dry-run enregistre bien la sonde."""
        cfg = self.configure()
        probe = {"harness": "deepseek", "path": "/opt/dsh",
                 "version": "0.2.0-rc.2", "raw": "dsh 0.2.0-rc.2",
                 "error": None}
        with mock.patch("ameesh.harness_versions.probe", return_value=probe):
            report = veille.scan(
                cfg, self.db, probe=True, dry_run=False,
                http_get=self.fetch(["0.2.0-rc.2", "0.2.1-rc.4"],
                                    tags={"rc": "0.2.1-rc.4"}))
        rows = storage.of(self.db).hosts.harness_versions()
        self.assertEqual(
            [(row["host"], row["harness"], row["version"]) for row in rows],
            [("vm-a", "deepseek", "0.2.0-rc.2")])
        self.assertEqual(report["actions"][0]["action"], "created")

    def test_designated_host_does_not_reconcile(self):
        self.record("vm-a", "0.2.0-rc.2")
        cfg = self.configure(host="vm-b", designated="vm-a")
        report = veille.scan(cfg, self.db, probe=False,
                             http_get=self.fetch(["0.2.0-rc.2", "0.2.1-rc.4"]))
        self.assertFalse(report["reconciled"])
        self.assertIn("vm-a", report["reason"])
        self.assertEqual(self.lots(), [])

    def test_cli_is_registered_and_off_without_config(self):
        proc = self.mesh("veille", "scan", "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(json.loads(proc.stdout)["enabled"])
        text = self.mesh("veille", "scan")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("veille: off", text.stdout)

    def test_cli_refuses_invalid_configuration(self):
        path = "%s/veille-bad.json" % self.tmp
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"veille": {"sources": [{"harness": "deepseek"}]}}, fh)
        proc = self.mesh("veille", "scan", env=self.env(AMEESH_CONFIG=path))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("npm", proc.stderr)


    def test_cli_returns_nonzero_on_a_persistent_error(self):
        import subprocess
        original_spawn = subprocess.Popen.__init__
        self.addCleanup(setattr, subprocess.Popen, "__init__", original_spawn)
        cfg = self.cfg
        args = types.SimpleNamespace(veille_command="scan", json=True,
                                     dry_run=False, no_probe=True, harness=None)

        class FakeDb:
            def close(self):
                pass

        report = {"enabled": True, "reconciled": True, "sources": [],
                  "actions": [], "errors": ["boom"]}
        with mock.patch("ameesh.mesh_cli._open", return_value=FakeDb()), \
                mock.patch("ameesh.veille.scan", return_value=report):
            self.assertEqual(isolated_entrypoint(veille_cli.cmd_veille, cfg, args), 1)
        self.assertIs(subprocess.Popen.__init__, original_spawn)


class HostcheckTest(unittest.TestCase):
    def cfg(self, block, host="vm-a"):
        return types.SimpleNamespace(host=host, veille=block)

    def test_timer_must_be_active(self):
        block = {"sources": [{"harness": "deepseek", "npm": "@org/dsh"}]}
        self.assertEqual(hostcheck.veille_lines(self.cfg(block),
                                                run=lambda *a: "active"), [])
        lines = hostcheck.veille_lines(self.cfg(block),
                                       run=lambda *a: "inactive")
        self.assertTrue(any("not active" in line for line in lines), lines)
        self.assertTrue(any("ameesh-veille.timer" in line for line in lines), lines)

    def test_other_designated_host_is_silent(self):
        block = {"host": "pc", "sources": [{"harness": "deepseek",
                                            "npm": "@org/dsh"}]}
        self.assertEqual(hostcheck.veille_lines(self.cfg(block),
                                                run=lambda *a: "inactive"), [])
        self.assertEqual(hostcheck.veille_lines(self.cfg({}),
                                                run=lambda *a: "inactive"), [])

    def test_unreadable_configuration_is_reported(self):
        lines = hostcheck.veille_lines(self.cfg({"oops": 1}),
                                       run=lambda *a: "active")
        self.assertTrue(any("unreadable" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
