# SPDX-License-Identifier: AGPL-3.0-only
"""CTX-03b (#5146) — le profil effectif publié dans ``turn.context``.

Le contrat ``ameesh-context/1`` §3.1 définit la clé de profil
``(modèle effectif, harnais, version, mode, rôle, fenêtre)`` ; CTX-03 (#4871)
calcule déjà la fenêtre du modèle principal, mais elle n'arrivait pas dans la
télémétrie du tour. Ce lot publie ce profil pour **tous** les tours, résidents
ou non : modèle effectif (jamais un modèle auxiliaire, R1), harnais et sa
version, mode, rôle de politique du canon, ``W`` avec source, qualité et raisons.

Observation seule : aucune décision de rotation/compactage ne change. Un
modèle seulement **demandé** au lancement est publié avec
``model_quality=requested``, jamais ``measured`` ; une fenêtre absente reste
``null`` avec ``window_unknown``, jamais une valeur supposée.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ameesh import adapters, context_profile, resident_runner
from tests.test_l81_resident_rotation import ResidentBench

MAIN = {"contextWindow": 1_000_000}
AUX = {"contextWindow": 200_000}


class RoleTest(unittest.TestCase):
    """Le rôle est la classe de politique du **premier** rôle du canon."""

    def test_first_declared_role_wins(self):
        self.assertEqual(context_profile.role_of(["implementation", "relecture"]),
                         "build")
        self.assertEqual(context_profile.role_of(["relecture", "implementation"]),
                         "review")

    def test_canon_names_and_contract_names(self):
        for canon, expected in (
                ("orchestrateur", "pilot"), ("orchestrator", "pilot"),
                ("relecture", "review"), ("review", "review"),
                ("auditeur", "audit"), ("auditor", "audit"),
                ("implementation", "build"), ("composition", "build"),
                ("documentation", "docs"), ("release-notes", "docs"),
                ("github-watch", "watch"), ("watch", "watch"),
                ("bureau", "other"), ("inconnu", "other")):
            with self.subTest(role=canon):
                self.assertEqual(context_profile.role_of([canon]), expected)

    def test_missing_or_unreadable_roles_are_other(self):
        for value in (None, [], [""], ["inconnu"], ["bureau", "implementation"],
                      42, {"role": "implementation"}):
            with self.subTest(value=value):
                self.assertEqual(context_profile.role_of(value), "other")

    def test_an_empty_first_entry_is_skipped(self):
        self.assertEqual(context_profile.role_of(["", "implementation"]), "build")


class PublishTest(unittest.TestCase):
    def test_measured_profile_carries_window_and_provenance(self):
        observed = context_profile.from_model_usage(
            {"claude-haiku-4-5": AUX, "claude-opus-5-5": MAIN},
            announced="claude-opus-5-5")
        profile = context_profile.publish(
            observed, harness="claude", harness_version="2.1.309",
            mode="non-resident", roles=["implementation"])
        self.assertEqual(profile["schema"], "ameesh-context/1")
        self.assertEqual(profile["model_effective"], "claude-opus-5-5")
        self.assertEqual(profile["model_quality"], "measured")
        self.assertEqual(profile["harness"], "claude")
        self.assertEqual(profile["harness_version"], "2.1.309")
        self.assertEqual(profile["harness_version_quality"], "measured")
        self.assertEqual(profile["mode"], "non-resident")
        self.assertEqual(profile["role"], "build")
        self.assertEqual(profile["window_tokens"], 1_000_000)
        self.assertEqual(profile["window_quality"], "measured")
        self.assertEqual(profile["window_source"],
                         context_profile.SOURCE_MODEL_USAGE)
        self.assertEqual(profile["auxiliary_models"], ["claude-haiku-4-5"])
        self.assertEqual(profile["reasons"], [])

    def test_requested_model_is_never_measured_and_gives_no_window(self):
        for harness in ("codex", "deepseek"):
            with self.subTest(harness=harness):
                profile = context_profile.publish(
                    None, requested_model="deepseek-flash", harness=harness,
                    harness_version=None, mode="non-resident",
                    roles=["implementation"])
                self.assertEqual(profile["model_effective"], "deepseek-flash")
                self.assertEqual(profile["model_quality"], "requested")
                self.assertEqual(profile["harness_version_quality"], "unknown")
                self.assertIsNone(profile["window_tokens"])
                self.assertEqual(profile["window_quality"], "unknown")
                self.assertIsNone(profile["window_source"])
                self.assertIn("window_unknown", profile["reasons"])
                self.assertIn("version_unknown", profile["version_reasons"])
                self.assertNotIn("version_unknown", profile["reasons"])
                self.assertEqual(profile["role"], "build")

    def test_auxiliary_model_never_becomes_the_main_window(self):
        # R1 : une entrée auxiliaire en première position, sans annonce du
        # modèle principal, ne donne ni modèle effectif ni fenêtre.
        observed = context_profile.from_model_usage(
            {"claude-haiku-4-5": AUX}, announced=None)
        profile = context_profile.publish(
            observed, requested_model="claude-opus-5-5", harness="claude",
            harness_version="2.1.309", mode="non-resident",
            roles=["implementation"])
        self.assertEqual(profile["model_effective"], "claude-opus-5-5")
        self.assertEqual(profile["model_quality"], "requested")
        self.assertIsNone(profile["window_tokens"])
        self.assertIn("window_unknown", profile["reasons"])
        self.assertEqual(profile["auxiliary_models"], ["claude-haiku-4-5"])

    def test_no_model_metadata_is_unknown_with_reasons(self):
        profile = context_profile.publish(
            None, harness="codex", mode="non-resident", roles=[])
        self.assertIsNone(profile["model_effective"])
        self.assertEqual(profile["model_quality"], "unknown")
        self.assertEqual(profile["role"], "other")
        self.assertIsNone(profile["window_tokens"])
        self.assertIn("model_unknown", profile["reasons"])
        self.assertIn("window_unknown", profile["reasons"])
        # L'absence de version est dite, pas inventée.
        self.assertIn("version_unknown", profile["version_reasons"])
        self.assertNotIn("version_unknown", profile["reasons"])

    def test_resident_driver_profile_is_accepted_as_is(self):
        # Forme déjà publiée par `TurnResult.profile` (CTX-03) : le runner y
        # ajoute seulement les faits du tour (harnais, version, mode, rôle).
        resident = {
            "schema": "ameesh-context/1",
            "model_effective": "gpt-x",
            "model_quality": "measured",
            "window_tokens": 258_400,
            "window_quality": "measured",
            "window_source": context_profile.SOURCE_CODEX,
            "reasons": [],
            "auxiliary_models": [],
        }
        profile = context_profile.publish(
            resident, harness="codex", harness_version="0.159.3",
            mode="resident", roles=["implementation"])
        self.assertEqual(profile["model_effective"], "gpt-x")
        self.assertEqual(profile["window_tokens"], 258_400)
        self.assertEqual(profile["mode"], "resident")
        self.assertEqual(profile["harness"], "codex")
        self.assertEqual(profile["harness_version"], "0.159.3")
        self.assertEqual(profile["role"], "build")

    def test_explicit_role_is_normalised(self):
        profile = context_profile.publish(
            None, requested_model="m", harness="claude", mode="non-resident",
            roles=["implementation"], role="relecture")
        self.assertEqual(profile["role"], "review")
        self.assertEqual(profile["model_quality"], "requested")


class ModelSelectionTest(unittest.TestCase):
    """Le modèle demandé publié : flux, puis grand livre, jamais ``inconnu``."""

    def test_stream_model_comes_first(self):
        from ameesh.runner import _modele_publie
        self.assertEqual(_modele_publie("claude-opus-5-5", "gpt-journal"),
                         "claude-opus-5-5")

    def test_ledger_model_fills_codex(self):
        from ameesh.runner import _modele_publie
        self.assertEqual(_modele_publie(None, "gpt-journal"), "gpt-journal")
        self.assertEqual(_modele_publie("", "gpt-journal"), "gpt-journal")

    def test_pricing_sentinel_is_not_a_model(self):
        from ameesh.runner import _modele_publie
        self.assertIsNone(_modele_publie(None, "inconnu"))
        self.assertIsNone(_modele_publie(None, ""))
        self.assertIsNone(_modele_publie(42, None))


class ClaudeModelUsageTest(unittest.TestCase):
    """Le `result` principal porte `modelUsage` ; un sous-agent ne l'annonce pas."""

    def setUp(self):
        self.reader = adapters.ClaudeStream()

    def test_main_result_exposes_model_usage(self):
        parsed = self.reader.parse_event({
            "type": "result", "result": "ok", "usage": {},
            "modelUsage": {"aux": AUX, "main": MAIN}})
        self.assertEqual(parsed["model_usage"], {"aux": AUX, "main": MAIN})

    def test_subagent_result_never_exposes_the_main_window(self):
        parsed = self.reader.parse_event({
            "type": "result", "result": "ok", "parent_tool_use_id": "tool-1",
            "modelUsage": {"aux": AUX}})
        self.assertNotIn("model_usage", parsed)

    def test_init_announces_the_effective_model(self):
        parsed = self.reader.parse_event({
            "type": "system", "subtype": "init", "session_id": "s",
            "model": "claude-opus-5-5"})
        self.assertEqual(parsed["model"], "claude-opus-5-5")
        observed = context_profile.from_model_usage(
            {"claude-opus-5-5": MAIN}, announced=parsed["model"])
        self.assertEqual(observed.window_tokens, 1_000_000)

    def test_main_thread_compact_boundary_is_exposed(self):
        parsed = self.reader.parse_event({
            "type": "system", "subtype": "compact_boundary", "session_id": "s",
            "compact_metadata": {"pre_tokens": 150_000, "post_tokens": 30_000}})
        self.assertEqual(parsed["compaction"]["compact_metadata"]["post_tokens"],
                         30_000)

    def test_subagent_compact_boundary_is_ignored(self):
        parsed = self.reader.parse_event({
            "type": "system", "subtype": "compact_boundary",
            "parent_tool_use_id": "tool-1",
            "compact_metadata": {"pre_tokens": 1, "post_tokens": 1}})
        self.assertNotIn("compaction", parsed)


class PublicationPayloadTest(unittest.TestCase):
    """Le profil reste dans le document `ameesh-context/1` publié."""

    def test_context_publication_keeps_the_profile(self):
        from ameesh import context_metrics, cost

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
            "profile": context_profile.publish(
                None, requested_model="deepseek-flash", harness="deepseek",
                harness_version=None, mode="non-resident",
                roles=["implementation"]),
        }
        legacy = {name: None for name in cost.CONTEXT_LEGACY_FIELDS}
        payload = cost.context_publication(
            document, legacy=legacy,
            decision={"policy": {"session": "par-lot"}},
            agent="a", host="h")
        self.assertIs(payload["context"]["profile"], document["profile"])
        self.assertEqual(json.dumps(payload["context"]["profile"]),
                         json.dumps(document["profile"]))


class ResidentProfileEventTest(ResidentBench):
    """Un tour résident publie le profil du driver et les faits du tour."""

    def _latest(self) -> dict:
        rows = self.db.query(
            "select data from ameesh_events where type = %s"
            " order by id desc limit 1", ("turn.context",))
        self.assertTrue(rows, "aucun événement turn.context publié")
        return rows[0]["data"]

    def test_resident_turn_publishes_mode_role_and_window(self):
        self.db.execute(
            "insert into harness_versions (host, harness, path, version, raw)"
            " values (%s, %s, %s, %s, %s)",
            (self.cfg.host, "claude", "/fake/bin/claude", "2.1.286",
             "2.1.286 (fake resident)"))
        _runner, worker = self._worker(name="ctx-profil-resident")
        with mock.patch.dict("os.environ", {"FAKE_CLAUDE_WINDOW": "1000000"}):
            self.assertTrue(self._turn(worker, "profil resident"))
        data = self._latest()
        profile = data["context"]["profile"]
        self.assertEqual(profile["model_effective"], "fake-model")
        self.assertEqual(profile["model_quality"], "measured")
        self.assertEqual(profile["window_tokens"], 1_000_000)
        self.assertEqual(profile["window_source"],
                         context_profile.SOURCE_MODEL_USAGE)
        self.assertEqual(profile["harness"], "claude")
        self.assertEqual(profile["harness_version"], "2.1.286")
        self.assertEqual(profile["mode"], "resident")
        self.assertEqual(profile["role"], "build")
        self.assertEqual(data["mode"], "resident")
        self.assertEqual(data["role"], "build")
        self.assertIs(data["decision"]["compacted"], False)
        self.assertIsNone(data["context"]["compaction"])

    def test_resident_compaction_is_true_then_reset_next_turn(self):
        with mock.patch.dict("os.environ", {"FAKE_CLAUDE_CONTEXT": "600",
                                            "FAKE_CLAUDE_WINDOW": "1000"}):
            _runner, worker = self._worker(name="ctx-compact-resident",
                                           resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            self.assertEqual(worker._resident_maintenance(),
                             resident_runner.COMPACT)
            self.assertTrue(worker.maybe_rotate())
            data = self._latest()
            self.assertIs(data["decision"]["compacted"], True)
            self.assertEqual(data["context"]["compaction"]["pre"], 600)
            self.assertEqual(data["context"]["compaction"]["post"], 120)
            # La compaction est un fait du tour : le suivant ne la rejoue pas.
            self.assertTrue(self._turn(worker, "deux"))
            # Témoin ResidentProcess : le second tour garde le contexte mesuré
            # (600), jamais l'ancien post de compaction (120), et ne compte
            # pas une seconde compaction.
            rp = worker.resident
            self.assertEqual(rp.compactions, 1)
            self.assertEqual(rp.last_context, 600)
            data = self._latest()
            self.assertIs(data["decision"]["compacted"], False)
            self.assertIsNone(data["context"]["compaction"])


if __name__ == "__main__":
    unittest.main()
