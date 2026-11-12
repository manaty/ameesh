# SPDX-License-Identifier: AGPL-3.0-only
"""#7004 — une identité externe liée par session déclare plusieurs harnais.

Le bureau est UNE identité (décision du pilote, #211859 a) dont les sessions
sont liées une à une : codex sur le PC, claude sur BG. Le registre ne portait
qu'un harnais, et le reçu de provenance d'un commit de BG était refusé
(« registered under 'codex' but its session binding declares 'claude' »).

La fiche déclare désormais l'ensemble fermé `harnesses:` (L51), que
`canon sync` écrit au registre (0573). `_bound_session` admet le harnais de la
liaison s'il y appartient, et le reçu porte CE harnais (§11 : la famille est
le harnais exécuté), jamais celui du registre. Un ensemble vide, illisible ou
inconnu refuse ; un agent `execute` reste refusé ; sans ensemble, rien ne
change.
"""
from __future__ import annotations

import unittest

from ameesh import registry, review_authors, review_provenance as prov, work

from . import test_3985_bound_session_provenance as t3985
from .support import PgTestCase
from .test_435_agent_projects import orchestre
from .test_canon import _CanonDbCase, write


FAMILIES = t3985.FAMILIES


class SeveralHarnessesBoundSessionTest(PgTestCase):
    # les utilitaires de #3985, sans rejouer ses témoins ici
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")
        self.lot = work.add(self.db, title="un lot du bureau sur BG", app="ameesh",
                            actor="orchestrateur")

    bind = t3985.BoundSessionProvenanceTest.bind
    repo = t3985.BoundSessionProvenanceTest.repo
    stage = t3985.BoundSessionProvenanceTest.stage
    commit = t3985.BoundSessionProvenanceTest.commit
    bound_commit = t3985.BoundSessionProvenanceTest.bound_commit

    def declare(self, name="bureau", *, harness="codex", harnesses="{codex,claude}"):
        self.db.execute(
            "UPDATE agent_registry SET harness = %s, harnesses = %s::text[] "
            "WHERE name = %s", (harness, harnesses, name))

    def refused(self):
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture(environ={}, families=FAMILIES, db=self.db)
        return str(raised.exception)

    def test_a_declared_harness_is_accepted_and_the_receipt_carries_it(self):
        self.bind(session="s-bureau-bg", harness="claude")
        self.declare()
        repo, env = self.repo()
        head, ident = self.bound_commit(repo, env)
        self.assertEqual(ident["harness"], "claude")
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], "bureau")
        self.assertEqual(receipt["harness"], "claude")
        self.assertEqual(receipt["turn"], "session:s-bureau-bg")
        # L'indépendance des avis lit la famille réelle, pas celle du registre.
        authors, unknown, _ = review_authors.reading(
            self.db, {"id": int(self.lot["id"]), "assignee": "bureau",
                      "app": "ameesh"},
            None, [head], repo=repo, families=FAMILIES, strict=True)
        self.assertEqual(unknown, [])
        self.assertIn({"name": "bureau", "family": "claude"}, authors)

    def test_the_registry_harness_is_still_accepted_within_the_set(self):
        self.bind(harness="codex")
        self.declare()
        ident = prov.capture(environ={}, families=FAMILIES, db=self.db)
        self.assertEqual(ident["harness"], "codex")

    def test_a_harness_outside_the_set_is_refused(self):
        self.bind(harness="deepseek")
        self.declare()
        message = self.refused()
        self.assertIn("registered under 'codex, claude'", message)
        self.assertIn("declares 'deepseek'", message)

    def test_an_execute_agent_is_still_refused(self):
        registry.upsert(self.db, "executeur", harness="codex",
                        host=self.cfg.host, mode="execute")
        self.declare("executeur")
        self.bind(agent="executeur", session="s-exec", harness="claude", force=True)
        self.assertIn("execute agent", self.refused())

    def test_without_a_set_the_single_harness_still_rules(self):
        self.bind(harness="claude")
        self.declare(harnesses=None)
        self.assertIn("registered under 'codex' but its session binding "
                      "declares 'claude'", self.refused())

    def test_an_empty_set_is_refused(self):
        self.bind(harness="codex")
        self.declare(harnesses="{}")
        self.assertIn("empty harness set", self.refused())

    def test_an_unknown_harness_in_the_set_is_refused(self):
        self.bind(harness="claude")
        self.declare(harnesses="{claude,gemini}")
        self.assertIn("unknown harness(es) 'gemini'", self.refused())

    def test_an_unreadable_set_is_refused(self):
        for raw in ("codex,claude", {"codex": 1}, 7):
            with self.subTest(raw=raw), self.assertRaises(prov.ProvenanceError) as raised:
                prov._declared_harnesses({"harnesses": raw}, "bureau")
            self.assertIn("unreadable harness set", str(raised.exception))


class SeveralHarnessesSyncTest(_CanonDbCase):

    def harnesses(self, name):
        return self.db.query("SELECT harnesses FROM agent_registry WHERE name = %s",
                             (name,))[0]["harnesses"]

    def test_sync_writes_the_declared_set(self):
        write(self.root, "agents/orchestre.md",
              orchestre(harness="codex", harnesses=["codex", "claude"]))
        self.sync()
        self.assertEqual(list(self.harnesses("orchestre")), ["codex", "claude"])
        self.assertEqual(registry.get(self.db, "orchestre")["harnesses"],
                         ["codex", "claude"])
        self.assertEqual(self.actions(self.sync())["orchestre"], "inchangé")
        # la déclaration retirée : la colonne est vidée, la ligne resynchronisée
        write(self.root, "agents/orchestre.md", orchestre(harness="codex"))
        self.assertNotEqual(self.actions(self.sync())["orchestre"], "inchangé")
        self.assertIsNone(self.harnesses("orchestre"))


if __name__ == "__main__":
    unittest.main()
