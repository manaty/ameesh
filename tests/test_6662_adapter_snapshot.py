# SPDX-License-Identifier: AGPL-3.0-only
"""M4 (#6662) — le réglage réellement remis à l'adaptateur, dans ``turn.context``.

L'étude #6650 demande un snapshot du réglage **effectivement passé** à
l'adaptateur : effort, modèle, version du binaire et compte de facturation par
identifiant non secret, avec une qualité ``requested``/``effective``/``unknown``.

La publication est additive : un événement antérieur n'a pas de bloc
``adapter`` et ses trous se lisent ``unknown`` (aucun backfill). Une panne de
publication ne retient jamais le tour ; aucun secret, chemin de clé ni corps de
prompt n'entre dans l'événement.

Les tests d'intégration utilisent les faux harnais du banc (``tests/fakebin``)
et un vrai exécuteur ; ils n'appellent aucun fournisseur.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ameesh import context_corpus, context_metrics, context_profile, cost, registry

from .test_4870_context_publication import publication
from .test_l242b3_fallback import _Repli, _claude_au_seuil
from .test_l30_accounts import _Base
from .test_l81_resident_rotation import ResidentBench


class FakeAccount:
    """Un profil de compte qui porte aussi des champs à ne JAMAIS publier."""

    name = "cle-a"
    kind = "api_key_env"
    billing_account = "facturation-a"
    path = "/comptes/prives"
    env = "DEEPSEEK_API_KEY"
    key_env = "SUPER_SECRET_KEY_ENV"
    key_file = "/comptes/prives/cle.key"


class AdapterSnapshotTest(unittest.TestCase):
    """La forme publiée et ses qualités, sans base ni exécuteur."""

    def test_requested_model_different_from_effective(self):
        snapshot = context_profile.adapter_snapshot(
            harness="claude", own_harness="claude", borrowed=False,
            model_requested="claude-demande", model_effective="claude-execute",
            harness_version="2.1.309")
        self.assertEqual(snapshot["schema"], context_profile.ADAPTER_SCHEMA)
        self.assertEqual(snapshot["model"]["requested"], "claude-demande")
        self.assertEqual(snapshot["model"]["effective"], "claude-execute")
        self.assertEqual(snapshot["model"]["quality"], "effective")
        self.assertEqual(snapshot["version"], "2.1.309")
        self.assertEqual(snapshot["version_quality"], "effective")

    def test_a_requested_model_alone_is_never_effective(self):
        snapshot = context_profile.adapter_snapshot(
            harness="codex", model_requested="gpt-demande")
        self.assertEqual(snapshot["model"]["requested"], "gpt-demande")
        self.assertIsNone(snapshot["model"]["effective"])
        self.assertEqual(snapshot["model"]["quality"], "requested")
        self.assertIsNone(snapshot["version"])
        self.assertEqual(snapshot["version_quality"], "unknown")

    def test_effort_not_announced_stays_requested(self):
        snapshot = context_profile.adapter_snapshot(
            harness="claude", effort_requested="high")
        self.assertEqual(snapshot["effort"]["requested"], "high")
        self.assertIsNone(snapshot["effort"]["effective"])
        self.assertEqual(snapshot["effort"]["quality"], "requested")

    def test_effort_absent_is_unknown(self):
        snapshot = context_profile.adapter_snapshot(harness="codex")
        self.assertIsNone(snapshot["effort"]["requested"])
        self.assertEqual(snapshot["effort"]["quality"], "unknown")

    def test_no_setting_is_unknown(self):
        snapshot = context_profile.adapter_snapshot()
        self.assertEqual(snapshot["model"]["quality"], "unknown")
        self.assertEqual(snapshot["effort"]["quality"], "unknown")
        self.assertEqual(snapshot["account"]["quality"], "unknown")
        self.assertEqual(snapshot["version_quality"], "unknown")
        self.assertIsNone(snapshot["borrowed"])

    def test_account_identity_is_non_secret(self):
        identity = context_profile.account_identity(FakeAccount())
        self.assertEqual(identity["account_name"], "cle-a")
        self.assertEqual(identity["billing_account"], "facturation-a")
        self.assertEqual(identity["credential_mode"], "api-key")
        encoded = json.dumps(identity)
        for secret in ("/comptes", "SUPER_SECRET", "DEEPSEEK_API_KEY", "cle.key"):
            self.assertNotIn(secret, encoded)

    def test_borrowed_harness_and_account_are_flagged(self):
        snapshot = context_profile.adapter_snapshot(
            harness="codex", own_harness="claude", borrowed=True,
            account_name="primaire", billing_account="fact-codex",
            credential_mode="subscription")
        self.assertTrue(snapshot["borrowed"])
        self.assertEqual(snapshot["harness"], "codex")
        self.assertEqual(snapshot["own_harness"], "claude")
        self.assertEqual(snapshot["account"]["name"], "primaire")
        self.assertEqual(snapshot["account"]["quality"], "requested")


class AdapterViewTest(unittest.TestCase):
    """La lecture N-1 et les trous : ``unknown`` explicite, jamais deviné."""

    def test_an_event_without_adapter_block_has_unknowns(self):
        view = context_profile.adapter_view(
            {"schema": "ameesh-context/1", "model": "deepseek-flash"})
        self.assertEqual(view["model"]["quality"], "unknown")
        self.assertIsNone(view["model"]["requested"])
        self.assertIsNone(view["model"]["effective"])
        self.assertEqual(view["effort"]["quality"], "unknown")
        self.assertEqual(view["account"]["quality"], "unknown")
        self.assertEqual(view["version_quality"], "unknown")
        self.assertIsNone(view["borrowed"])

    def test_a_partial_adapter_block_exposes_the_gaps(self):
        view = context_profile.adapter_view({"adapter": {
            "schema": context_profile.ADAPTER_SCHEMA,
            "harness": "claude",
            "version": "2.1.309",
            "model": {"requested": "claude-demande",
                      "effective": "claude-execute",
                      "quality": "effective"},
            # effort et compte absents : trous, pas des valeurs inventées
        }})
        self.assertEqual(view["model"]["quality"], "effective")
        self.assertEqual(view["effort"]["quality"], "unknown")
        self.assertEqual(view["account"]["quality"], "unknown")
        self.assertEqual(view["version_quality"], "effective")

    def test_malformed_fields_fall_back_to_unknown(self):
        view = context_profile.adapter_view({"adapter": {
            "borrowed": "oui", "version": 42,
            "model": "pas-un-objet", "effort": {"requested": []},
            "account": {"name": "", "billing_account": 3,
                        "credential_mode": "autre"}}})
        self.assertIsNone(view["borrowed"])
        self.assertIsNone(view["version"])
        self.assertEqual(view["version_quality"], "unknown")
        self.assertEqual(view["model"]["quality"], "unknown")
        self.assertEqual(view["effort"]["quality"], "unknown")
        self.assertIsNone(view["account"]["credential_mode"])


class AdapterPublicationTest(unittest.TestCase):
    """Le bloc reste additif et le corpus antérieur l'ignore."""

    def _document(self, adapter=None):
        document = {
            "schema": context_metrics.SCHEMA,
            "harness": "deepseek",
            "model": "deepseek-flash",
            "mode": "non-resident",
            "role": "build",
            "session": "s",
            "turn": "t",
            "finished": True,
            "measures": {
                "C_last": {"value": 1, "quality": "measured", "reasons": [],
                           "granularity": "call", "source": "stream"},
                "R_turn": {"value": 2, "quality": "measured", "reasons": [],
                           "granularity": "call", "source": "stream"},
            },
        }
        if adapter is not None:
            document["adapter"] = adapter
        return document

    def test_corpus_still_reads_an_event_with_the_adapter_block(self):
        adapter = context_profile.adapter_snapshot(
            harness="deepseek", own_harness="deepseek", borrowed=False,
            model_requested="deepseek-flash", effort_requested="max",
            account_name="cle-a", credential_mode="api-key")
        payload = publication(self._document(adapter))
        line = context_corpus.corpus_line(payload)
        self.assertEqual(line["schema"], context_corpus.SCHEMA)
        self.assertEqual(line["model"], "deepseek-flash")
        self.assertEqual(line["model_quality"], "requested")

    def test_no_prompt_tool_or_secret_in_the_publication(self):
        adapter = context_profile.adapter_snapshot(
            harness="claude", model_requested="m", effort_requested="high",
            account_name="primaire", billing_account="fact-a",
            credential_mode="subscription")
        document = self._document(adapter)
        encoded = json.dumps(publication(document))
        self.assertIn("ameesh-adapter/1", encoded)
        self.assertNotIn("SUPER_SECRET", encoded)
        self.assertNotIn("/comptes", encoded)
        self.assertNotIn("PROMPT-SENTINEL-6662", encoded)


class AdapterTurnTest(_Base):
    """Un vrai tour du banc publie la section ``adapter`` avec ses trous."""

    def _event(self) -> dict:
        rows = self.db.query(
            "select data from ameesh_events where type = %s"
            " order by id desc limit 1", (cost.CONTEXT_EVENT,))
        self.assertTrue(rows, "aucun événement turn.context publié")
        return rows[0]["data"]

    def test_a_deepseek_turn_publishes_the_settings_it_really_passed(self):
        self.db.execute(
            "insert into harness_versions (host, harness, path, version, raw)"
            " values (%s, %s, %s, %s, %s)",
            (self.cfg.host, "deepseek", "/fake/bin/dsh", "0.2.1-rc.4",
             "0.2.1-rc.4"))
        _runner, worker = self._worker("ctx-m4-dsh", "deepseek", {})
        self.db.execute("UPDATE agent_registry SET effort = 'high'"
                        " WHERE name = 'ctx-m4-dsh'")
        worker.agent = registry.get(self.db, "ctx-m4-dsh")
        self.assertIsNotNone(self._tour(worker, "un tour M4"))
        adapter = self._event()["context"]["adapter"]
        self.assertEqual(adapter["schema"], context_profile.ADAPTER_SCHEMA)
        self.assertEqual(adapter["harness"], "deepseek")
        self.assertEqual(adapter["own_harness"], "deepseek")
        self.assertIs(adapter["borrowed"], False)
        # le modèle par défaut du descripteur est bien celui remis au harnais
        self.assertEqual(adapter["model"]["requested"], "deepseek-flash")
        self.assertIsNone(adapter["model"]["effective"])
        self.assertEqual(adapter["model"]["quality"], "requested")
        # l'effort passé (flag/gabarit) n'est pas annoncé par le harnais
        self.assertEqual(adapter["effort"]["requested"], "high")
        self.assertIsNone(adapter["effort"]["effective"])
        self.assertEqual(adapter["effort"]["quality"], "requested")
        # version du binaire enregistrée pour l'hôte
        self.assertEqual(adapter["version"], "0.2.1-rc.4")
        self.assertEqual(adapter["version_quality"], "effective")
        # aucun compte déclaré : le compte reste un trou, jamais inventé
        self.assertIsNone(adapter["account"]["name"])
        self.assertEqual(adapter["account"]["quality"], "unknown")

    def test_the_default_effort_written_by_the_patch_is_published(self):
        # DeepSeek écrit le modèle et l'effort dans le même patch YAML : sans
        # réglage d'agent, l'effort « max » du descripteur est bien remis.
        _runner, worker = self._worker("ctx-m4-effort", "deepseek", {})
        self.assertIsNotNone(self._tour(worker, "effort par defaut"))
        adapter = self._event()["context"]["adapter"]
        self.assertEqual(adapter["effort"]["requested"], "max")
        self.assertIsNone(adapter["effort"]["effective"])
        self.assertEqual(adapter["effort"]["quality"], "requested")

    def test_the_requested_model_is_not_the_executed_one(self):
        _runner, worker = self._worker("ctx-m4-claude", "claude", {})
        self.db.execute("UPDATE agent_registry SET model = 'claude-demande'"
                        " WHERE name = 'ctx-m4-claude'")
        worker.agent = registry.get(self.db, "ctx-m4-claude")
        usage = json.dumps({"claude-execute": {"contextWindow": 1_000_000}})
        self.assertIsNotNone(self._tour(
            worker, "modele demande",
            AMEESH_TEST_CLAUDE_MODEL="claude-execute",
            AMEESH_TEST_CLAUDE_MODEL_USAGE=usage))
        data = self._event()
        self.assertEqual(data["context"]["profile"]["model_effective"],
                         "claude-execute")
        adapter = data["context"]["adapter"]
        self.assertEqual(adapter["model"]["requested"], "claude-demande")
        self.assertEqual(adapter["model"]["effective"], "claude-execute")
        self.assertEqual(adapter["model"]["quality"], "effective")

    def test_an_account_is_published_without_its_secret(self):
        comptes = {"deepseek": [{
            "name": "cle-a", "type": "api_key_env",
            "key_env": "SUPER_SECRET_KEY_ENV",
            "billing_account": "facturation-a",
        }]}
        _runner, worker = self._worker("ctx-m4-compte", "deepseek", comptes)
        self.assertIsNotNone(self._tour(
            worker, "tour compte", SUPER_SECRET_KEY_ENV="s3cr3t-valeur-6662"))
        data = self._event()
        adapter = data["context"]["adapter"]
        self.assertEqual(adapter["account"]["name"], "cle-a")
        self.assertEqual(adapter["account"]["billing_account"],
                         "facturation-a")
        self.assertEqual(adapter["account"]["credential_mode"], "api-key")
        self.assertEqual(adapter["account"]["quality"], "requested")
        encoded = json.dumps(data)
        self.assertNotIn("s3cr3t-valeur-6662", encoded)
        self.assertNotIn("SUPER_SECRET_KEY_ENV", encoded)
        self.assertNotIn("tour compte", encoded)

    def test_a_publication_failure_does_not_block_the_turn(self):
        _runner, worker = self._worker("ctx-m4-panne", "deepseek", {})
        before = self.db.query(
            "select count(*) as n from ameesh_events where type = %s",
            (cost.CONTEXT_EVENT,))[0]["n"]
        with mock.patch("ameesh.runner.context_profile.adapter_snapshot",
                        side_effect=RuntimeError("panne de publication")):
            self.assertIsNotNone(self._tour(worker, "panne publication"))
        rows = self.db.query(
            "select input_tokens from turn_costs where agent = %s",
            ("ctx-m4-panne",))
        self.assertTrue(rows, "la ligne du grand livre doit rester écrite")
        after = self.db.query(
            "select count(*) as n from ameesh_events where type = %s",
            (cost.CONTEXT_EVENT,))[0]["n"]
        self.assertEqual(after, before, "l'observation en panne ne s'écrit pas")


class AdapterResidentTurnTest(ResidentBench):
    """Un tour résident publie la même section ``adapter`` que l'événement."""

    def test_a_resident_turn_publishes_adapter_settings(self):
        self.db.execute(
            "insert into harness_versions (host, harness, path, version, raw)"
            " values (%s, %s, %s, %s, %s)",
            (self.cfg.host, "claude", "/fake/bin/claude", "2.1.286",
             "2.1.286 (fake resident)"))
        _runner, worker = self._worker(name="ctx-m4-resident")
        self._set(worker, effort="high")
        with mock.patch.dict("os.environ", {"FAKE_CLAUDE_WINDOW": "1000000"}):
            self.assertTrue(self._turn(worker, "resident M4"))
        rows = self.db.query(
            "select data from ameesh_events where type = %s"
            " order by id desc limit 1", (cost.CONTEXT_EVENT,))
        self.assertTrue(rows, "aucun événement turn.context publié")
        adapter = rows[0]["data"]["context"]["adapter"]
        self.assertEqual(adapter["schema"], context_profile.ADAPTER_SCHEMA)
        self.assertEqual(adapter["harness"], "claude")
        self.assertEqual(adapter["own_harness"], "claude")
        self.assertIs(adapter["borrowed"], False)
        self.assertEqual(adapter["version"], "2.1.286")
        self.assertEqual(adapter["version_quality"], "effective")
        # un modèle exécuté sans réglage demandé est effectif, jamais inventé
        # comme « requested »
        if adapter["model"]["effective"] is not None:
            self.assertEqual(adapter["model"]["quality"], "effective")
        self.assertEqual(adapter["effort"]["requested"], "high")
        self.assertIsNone(adapter["effort"]["effective"])
        self.assertEqual(adapter["effort"]["quality"], "requested")


class AdapterFallbackTurnTest(_Repli):
    """Un repli de harnais publie le harnais et le compte empruntés."""

    def test_a_borrowed_harness_and_account_are_traced(self):
        worker = self.worker()
        try:
            self._tour(worker)
            with _claude_au_seuil():
                self.assertIsNotNone(self._tour(worker, "suite"))
            rows = self.db.query(
                "select data from ameesh_events where type = %s"
                " order by id desc limit 1", (cost.CONTEXT_EVENT,))
            self.assertTrue(rows, "aucun événement après le repli")
            adapter = rows[0]["data"]["context"]["adapter"]
            self.assertEqual(adapter["harness"], "codex")
            self.assertEqual(adapter["own_harness"], "claude")
            self.assertIs(adapter["borrowed"], True)
            self.assertEqual(adapter["account"]["name"], "primaire")
            self.assertEqual(adapter["account"]["credential_mode"],
                             "subscription")
            # le harnais emprunté n'hérite ni du modèle ni de l'effort de la
            # fiche ; Codex n'a pas de défaut d'effort : trou explicite.
            self.assertIsNone(adapter["effort"]["requested"])
            self.assertEqual(adapter["effort"]["quality"], "unknown")
        finally:
            worker.watchdog_stop.set()


if __name__ == "__main__":
    unittest.main()
