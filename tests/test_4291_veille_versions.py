# SPDX-License-Identifier: AGPL-3.0-only
"""#4291 / #3264 A : veille quotidienne des versions de harnais et de modèles.

Le témoin de la spec est rejoué sans réseau : dsh 0.2.0-rc.2 installée,
0.2.1-rc.4 publiée, 0.2.2-alpha.2 ignorée. Les autres cas couvrent l'alpha
seule, la deuxième version qui met à jour le même lot, l'écart entre hôtes, la
priorité 1 sur recommandation, le modèle servi jamais facturé et la
non-réouverture d'un objet déjà évalué.
"""
from __future__ import annotations

import dataclasses
import json
import types
import unittest

from ameesh import hostcheck, storage, veille, work
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
        self.assertEqual(again["actions"][0]["action"], "already-evaluated")
        self.assertEqual(len(self.lots()), 1)
        # a NEWER version is compared, and opens a new lot
        newer = self.scan(["0.2.0-rc.2", "0.2.1-rc.4", "0.2.1-rc.5"],
                          tags={"rc": "0.2.1-rc.5"})
        self.assertEqual(newer["actions"][0]["action"], "created", newer)
        self.assertEqual(newer["actions"][0]["lot"] != lot_id, True)

    def test_model_served_and_never_billed_opens_a_model_lot(self):
        self.record("vm-a", "0.2.1-rc.4")
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
        self.assertEqual(lot["priority"], 2)

    def test_dry_run_writes_nothing(self):
        self.record("vm-a", "0.2.0-rc.2")
        report = self.scan(["0.2.0-rc.2", "0.2.1-rc.4"],
                           tags={"rc": "0.2.1-rc.4"}, dry_run=True)
        self.assertEqual(report["actions"][0]["action"], "would-create")
        self.assertEqual(self.lots(), [])

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
