# SPDX-License-Identifier: AGPL-3.0-only
"""#7706 — contrat public courrier 1.2 : acteurs réservés et espace de noms.

Le constat de l'auditeur sur la tête de #7671 : la surface acceptait tout
acteur pour un processus sans identité déclarée (``ameesh`` compris) et
laissait la charge utile libre au premier niveau, donc une clé de contrôle
ajoutée plus tard au cœur aurait été ouverte aux modules par défaut.

Ces témoins sont négatifs avant d'être positifs : le même acteur non réservé,
la même charge utile sous ``payload["module"]`` et la même clé connue du cœur
imbriquée dans cet espace de noms doivent passer, pour prouver que le refus
tient au nom réservé ou au niveau de la clé, jamais à un autre contrôle.
Postgres réel.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import types
import unittest
from unittest import mock

from ameesh import contract_fragments, mail, registry
from ameesh.public import mail as public_mail
from ameesh.public.mail import (
    InvalidSubmission, ScopeRefused, Submission,
)

from .support import PgTestCase


def fake_canon(*cards, readable=True, findings=()) -> types.SimpleNamespace:
    """Un canon minimal : des fiches Agent/Persona (titre, rôles)."""
    return types.SimpleNamespace(
        agents=[types.SimpleNamespace(title=title, roles=list(roles))
                for title, roles in cards],
        readable=readable,
        root="/canon-test",
        load_findings=list(findings))


def canon_entry(path: str = "/canon-test", ref: str = "",
                untrusted: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(path=path, ref=ref, untrusted=untrusted)


@contextlib.contextmanager
def canon_configured(*cards, entries=None, canons=None, readable=True):
    """Un canon configuré pour la durée du bloc (les deux lectures patchées)."""
    if entries is None:
        entries = (canon_entry(),)
    if canons is None:
        canons = [fake_canon(*cards, readable=readable)]
    with mock.patch("ameesh.canon.configured",
                    return_value=tuple(entries)), \
         mock.patch("ameesh.canon.load_configured",
                    return_value=list(canons)):
        yield canons


class PublicMailBase(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        public_mail._reset_canon_cache()
        for name in ("alpha", "beta", "gamma"):
            registry.upsert(self.db, name, harness="codex", host=self.cfg.host)

    def submission(self, recipient: str = "beta", body: str = "hello",
                   **overrides) -> Submission:
        data = {"recipient": recipient, "body": body}
        data.update(overrides)
        return Submission(**data)

    def rows(self, recipient: str | None = None) -> list[dict]:
        if recipient is None:
            return self.db.query(
                "SELECT id, sender, recipient, body, kind, payload,"
                " work_item_id FROM agent_mailbox ORDER BY id")
        return self.db.query(
            "SELECT id, sender, recipient, body, kind, payload,"
            " work_item_id FROM agent_mailbox WHERE recipient = %s ORDER BY id",
            (recipient,))


class ReservedActorTest(PublicMailBase):
    def test_core_sender_identities_are_refused_before_deposit(self):
        before = self.rows()
        for actor in sorted(public_mail.RESERVED_ACTORS):
            with self.subTest(actor=actor):
                with self.assertRaises(ScopeRefused):
                    public_mail.submit(self.db, actor, self.submission())
        self.assertEqual(self.rows(), before)

    def test_the_guarded_deep_analysis_payload_never_reaches_the_core(self):
        """The negative witness of the auditor's scenario: actor ``ameesh``
        with the deep-analysis payload (``deep_analysis`` + lot) is refused by
        the actor guard, before the payload would be deposited."""
        before = self.rows()
        guarded = self.submission(
            kind="event", payload={"deep_analysis": "bug_p1", "lot": 7706})
        with self.assertRaises(ScopeRefused):
            public_mail.submit(self.db, "ameesh", guarded)
        self.assertEqual(self.rows(), before)
        # The same submission under a non-reserved service identity is admitted
        # once its payload is in the module namespace: the refusal is the actor.
        receipt = public_mail.submit(
            self.db, "ima-service",
            self.submission(kind="event",
                            payload={"module": {"deep_analysis": "bug_p1",
                                                "lot": 7706}}))
        self.assertGreater(receipt.id, 0)
        [row] = self.rows("beta")
        self.assertEqual(row["sender"], "ima-service")
        self.assertNotIn("deep_analysis", row["payload"])

    def test_case_variants_and_core_prefixes_are_refused(self):
        for actor in ("Ameesh", "AMEESH", "Veille", "Inconnu",
                      "Chat-smichea", "chat-smichea"):
            with self.subTest(actor=actor):
                with self.assertRaises(ScopeRefused):
                    public_mail.submit(self.db, actor, self.submission())
        self.assertEqual(self.rows(), [])

    def test_human_and_broadcast_stay_invalid(self):
        for actor in ("all", "human:smichea"):
            with self.subTest(actor=actor):
                with self.assertRaises(InvalidSubmission):
                    public_mail.submit(self.db, actor, self.submission())
        self.assertEqual(self.rows(), [])

    def test_reserved_actor_is_refused_even_when_the_process_declares_it(self):
        from ameesh import identity

        binding = identity.Binding(name="ameesh", source="runner", ok=True)
        with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "ameesh"}):
            with mock.patch("ameesh.identity.resolve_binding",
                            return_value=binding):
                with self.assertRaises(ScopeRefused):
                    public_mail.submit(self.db, "ameesh", self.submission())
        self.assertEqual(self.rows(), [])

    def test_reserved_actor_is_refused_through_find_history_and_batch(self):
        with canon_configured(("sonde", ["orchestrateur"])):
            for actor in ("ameesh", "veille", "sonde"):
                with self.subTest(actor=actor):
                    with self.assertRaises(ScopeRefused):
                        public_mail.find(self.db, actor, "beta", "key")
                    with self.assertRaises(ScopeRefused):
                        public_mail.history(self.db, actor)
                    with self.assertRaises(ScopeRefused):
                        public_mail.submit_many(
                            self.db, actor, [self.submission()])
        self.assertEqual(self.rows(), [])

    def test_an_alias_cannot_launder_a_reserved_primary(self):
        """The check covers the resolved primary, not only the raw spelling."""
        registry.upsert(self.db, "ameesh", harness="codex", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET aliases = ARRAY['zulu']"
            " WHERE name = 'ameesh'")
        with self.assertRaises(ScopeRefused):
            public_mail.submit(self.db, "zulu", self.submission())
        self.assertEqual(self.rows(), [])

    def test_a_service_identity_outside_the_reserved_names_still_submits(self):
        receipt = public_mail.submit(
            self.db, "ima-service", self.submission(body="from the module"))
        self.assertEqual(receipt.actor, "ima-service")
        [row] = self.rows("beta")
        self.assertEqual(row["sender"], "ima-service")
        self.assertEqual(row["body"], "from the module")

    def test_the_public_refusal_does_not_disable_the_core_system_sender(self):
        """The guard belongs to the public surface, not to the mailbox: the
        core keeps sending its own events under ``ameesh`` (mail.send)."""
        message_id = mail.send(
            self.db, "ameesh", "beta", "core event", kind="event")
        self.assertGreater(message_id, 0)
        [row] = self.rows("beta")
        self.assertEqual(row["sender"], "ameesh")
        self.assertEqual(row["body"], "core event")

    def test_a_canon_role_holder_is_refused(self):
        """A role declared only by the canon is reserved, with no core literal."""
        with canon_configured(("sonde-orchestrateur", ["orchestrateur"])):
            for actor in ("sonde-orchestrateur", "SONDE-ORCHESTRATEUR"):
                with self.subTest(actor=actor):
                    with self.assertRaises(ScopeRefused):
                        public_mail.submit(self.db, actor, self.submission())
        self.assertEqual(self.rows(), [])

    def test_a_canon_role_holder_cannot_be_laundered_by_an_alias(self):
        registry.upsert(self.db, "sonde", harness="codex", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET aliases = ARRAY['zulu']"
            " WHERE name = 'sonde'")
        with canon_configured(("sonde", ["auditeur"])):
            with self.assertRaises(ScopeRefused):
                public_mail.submit(self.db, "zulu", self.submission())
        self.assertEqual(self.rows(), [])

    def test_reserved_names_come_from_the_canon_not_the_core(self):
        from ameesh import chat, watch, work

        self.assertIn(work.SYSTEM_SENDER, public_mail.RESERVED_ACTORS)
        self.assertIn(watch.SENDER, public_mail.RESERVED_ACTORS)
        self.assertIn(work.UNKNOWN_ACTOR, public_mail.RESERVED_ACTORS)
        self.assertIn(chat.CHAT_PREFIX, public_mail.RESERVED_ACTOR_PREFIXES)
        self.assertIn(work.HUMAN_PREFIX, public_mail.RESERVED_ACTOR_PREFIXES)
        # No mesh persona name is written in the core (#7661): a governance
        # identity is recognized by the role its Agent/Persona card declares.
        for name in ("auditeur", "bureau"):
            self.assertNotIn(name, public_mail.RESERVED_ACTORS)
        with canon_configured(
            ("auditeur", ["auditeur", "auditor"]),
            ("bureau", ["bureau"]),
            ("pilote-nexlink", ["pilote"]),
            ("agent-sans-role", []),
        ):
            names = public_mail.reserved_actors(self.db)
        self.assertIn("auditeur", names)
        self.assertIn("bureau", names)
        # A role declared by another canon, unknown to the core, is covered.
        self.assertIn("pilote-nexlink", names)
        # A card without a role is not a governance identity.
        self.assertNotIn("agent-sans-role", names)
        self.assertNotIn("ima-service", names)

    def test_a_canon_card_without_roles_still_submits(self):
        with canon_configured(("agent-sans-role", [])):
            receipt = public_mail.submit(
                self.db, "agent-sans-role", self.submission())
        self.assertGreater(receipt.id, 0)

    def test_an_unreadable_canon_refuses_instead_of_an_empty_inventory(self):
        """Un canon configuré mais illisible ne vaut pas « aucun rôle ».

        Le nom d'un rôle de gouvernance omis ne doit pas passer comme identité
        de service : la surface refuse, et ne met pas l'ensemble vide en cache.
        """
        canon = fake_canon(("sonde-orchestrateur", ["orchestrateur"]),
                           readable=False)
        with canon_configured(canons=[canon]):
            with self.assertRaisesRegex(ScopeRefused, "canon"):
                public_mail.submit(self.db, "pilote", self.submission())
            with self.assertRaises(ScopeRefused):
                public_mail.reserved_actors(self.db)
        self.assertEqual(self.rows(), [])

    def test_a_canon_read_error_refuses_instead_of_an_empty_inventory(self):
        """Une exception de lecture ne devient pas « aucun rôle »."""
        from ameesh import canon as canon_mod

        with mock.patch("ameesh.canon.configured",
                        return_value=(canon_entry(),)), \
             mock.patch("ameesh.canon.load_configured",
                        side_effect=canon_mod.CanonError("boom")):
            with self.assertRaisesRegex(ScopeRefused, "cannot be read"):
                public_mail.submit(self.db, "pilote", self.submission())
        self.assertEqual(self.rows(), [])

    def test_a_partial_canon_load_refuses_instead_of_dropping_roles(self):
        """Un chargement partiel (un canon écarté) n'est pas un inventaire."""
        entries = (canon_entry("/canon-a"), canon_entry("/canon-b"))
        canons = [fake_canon(("sonde-orchestrateur", ["orchestrateur"]))]
        with canon_configured(entries=entries, canons=canons):
            with self.assertRaisesRegex(ScopeRefused, "incomplete"):
                public_mail.submit(self.db, "pilote", self.submission())
            with self.assertRaises(ScopeRefused):
                public_mail.reserved_actors(self.db)
        self.assertEqual(self.rows(), [])

    def test_an_invalid_canon_refuses_instead_of_dropping_roles(self):
        finding = types.SimpleNamespace(code="frontmatter-invalid",
                                        severity="error")
        canon = fake_canon(("sonde-orchestrateur", ["orchestrateur"]),
                           findings=[finding])
        with canon_configured(canons=[canon]):
            with self.assertRaisesRegex(ScopeRefused, "canon"):
                public_mail.submit(self.db, "pilote", self.submission())
        self.assertEqual(self.rows(), [])


class ModulePayloadNamespaceTest(PublicMailBase):
    def test_an_unknown_top_level_key_is_refused_by_default(self):
        """#2 of the constat: a marker the core does not read today is closed.

        The allow-list makes the future key refused even though it is absent
        from RESERVED_PAYLOAD_KEYS; the same key under payload["module"] is
        ordinary module data and is admitted."""
        future = "core_marker_added_later"
        self.assertNotIn(future, public_mail.RESERVED_PAYLOAD_KEYS)
        before = self.rows()
        with self.assertRaises(InvalidSubmission) as raised:
            public_mail.submit(
                self.db, "alpha", self.submission(payload={future: True}))
        self.assertIn("module", str(raised.exception))
        self.assertEqual(self.rows(), before)
        receipt = public_mail.submit(
            self.db, "alpha",
            self.submission(payload={"module": {future: True}}))
        self.assertGreater(receipt.id, 0)
        [row] = self.rows("beta")
        self.assertEqual(row["payload"]["module"], {future: True})
        self.assertNotIn(future, row["payload"])

    def test_a_bare_1_1_draft_key_is_refused_and_names_the_namespace(self):
        before = self.rows()
        with self.assertRaises(InvalidSubmission) as raised:
            public_mail.submit(
                self.db, "alpha", self.submission(payload={"document": {"id": 7}}))
        self.assertIn("payload['module']", str(raised.exception))
        self.assertEqual(self.rows(), before)

    def test_module_namespace_and_attribution_round_trip(self):
        receipt = public_mail.submit(
            self.db, "alpha",
            self.submission(
                kind="request", work_item_id=7706,
                payload={"module": {"document": {"id": 7},
                                    "memoria": [1, 2]},
                         "attribution": {"source": " ima ", "run_id": "r-1"}}))
        self.assertGreater(receipt.id, 0)
        [row] = self.rows("beta")
        self.assertEqual(row["payload"]["module"],
                         {"document": {"id": 7}, "memoria": [1, 2]})
        self.assertEqual(row["payload"]["attribution"],
                         {"source": "ima", "run_id": "r-1"})
        [entry] = public_mail.history(self.db, "alpha").entries
        self.assertEqual(entry.payload["module"], row["payload"]["module"])
        self.assertEqual(entry.payload["attribution"],
                         row["payload"]["attribution"])

    def test_the_module_namespace_must_be_a_json_object(self):
        before = self.rows()
        for value in (None, 5, "text", [1], True):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSubmission):
                    public_mail.submit(
                        self.db, "alpha",
                        self.submission(payload={"module": value}))
        self.assertEqual(self.rows(), before)

    def test_a_nested_core_marker_is_ordinary_data(self):
        """A known marker inside payload["module"] is data, never a core path.

        The mailbox rewrites a pending review request only when it sees
        ``payload.review_request`` at the top level; the nested key must leave
        the pending request untouched and the module deposit accepted."""
        lot = 7706
        self.db.execute(
            "INSERT INTO work_items (id, type, source, title, app, state)"
            " VALUES (%s, 'evolution', 'mesh-design', 'lot de relecture',"
            " 'ameesh', 'qa')", (lot,))
        pending = mail.send(
            self.db, "beta", "alpha", "relecture du lot",
            kind="request", work_item_id=str(lot),
            payload={"review_request": {"freeze": {"sha": "a" * 40}}})
        before = self.rows("alpha")
        receipt = public_mail.submit(
            self.db, "gamma", self.submission(
                recipient="alpha", body="module data, not a review",
                kind="request", work_item_id=lot,
                payload={"module": {"review_request": {"freeze":
                                                       {"sha": "b" * 40}}}}))
        self.assertGreater(receipt.id, 0)
        after = self.rows("alpha")
        self.assertEqual([row for row in after if row["id"] == pending],
                         [row for row in before if row["id"] == pending])
        new = [row for row in after if row["id"] == receipt.id][0]
        self.assertEqual(new["payload"]["module"]["review_request"],
                         {"freeze": {"sha": "b" * 40}})
        self.assertNotIn("review_request", new["payload"])
        self.assertEqual(self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE %s", (lot, "review-designation-replaced%")), [])

    def test_a_batch_reports_a_payload_outside_the_namespace_as_invalid(self):
        batch = public_mail.submit_many(self.db, "alpha", (
            self.submission(body="fine",
                            payload={"module": {"n": 1}}),
            self.submission(body="bare", payload={"n": 2}),
        ))
        self.assertEqual([result.ok for result in batch.results], [True, False])
        self.assertEqual(batch.results[1].error, "invalid")
        self.assertEqual([row["body"] for row in self.rows()], ["fine"])

    def test_a_1_1_draft_row_stays_readable_without_a_rewrite(self):
        """Compatibility migration: stored rows are read as-is, never rewritten."""
        sent = mail.send(self.db, "alpha", "beta", "legacy draft",
                         kind="notify", payload={"legacy_key": {"n": 1}})
        [entry] = public_mail.history(self.db, "alpha").entries
        self.assertEqual(entry.id, sent)
        self.assertEqual(entry.payload, {"legacy_key": {"n": 1}})
        self.assertEqual(self.rows("beta")[0]["payload"],
                         {"legacy_key": {"n": 1}})


class ContractVersionTest(unittest.TestCase):
    def test_the_package_and_mail_surface_keep_their_revisions(self):
        from ameesh import public

        # The package revision moves with the whole contract (#7701 adds the
        # fixture in 1.5, #8333 the production connection in 1.6, #8341 the
        # fixture's agent states in 1.7, #8342 the public action connectors in
        # 1.8, #8337 the public account profiles in 1.9, #8339 the migration
        # dry-run report in 1.10, #8343 the account reservations in 1.11,
        # #8340 the public canon-refusal CLI in 1.12, #8338 the public price
        # schedule in 1.13); a later revision is assigned by the packet composer
        # from a fragment (#8395), so the package revision is read from the
        # contract header, never hard-coded. The mail surface keeps the revision
        # that last changed it, so a caller's 1.4 gate stays true.
        root = Path(__file__).resolve().parents[1]
        header = contract_fragments.header_revision(
            (root / contract_fragments.CONTRACT_REL).read_text(encoding="utf-8"))
        self.assertEqual(public.CONTRACT_VERSION, header)
        self.assertEqual(public_mail.CONTRACT_VERSION, "1.4")
        self.assertEqual(public.fixture.CONTRACT_VERSION, "1.7")
        self.assertEqual(public.database.CONTRACT_VERSION, "1.6")
        self.assertEqual(public_mail.ADMITTED_PAYLOAD_KEYS,
                         frozenset(("module", "attribution")))
        self.assertEqual(public_mail.MODULE_PAYLOAD_KEY, "module")
        for name in ("MODULE_PAYLOAD_KEY", "ADMITTED_PAYLOAD_KEYS",
                     "RESERVED_ACTORS", "RESERVED_ACTOR_PREFIXES",
                     "reserved_actors"):
            self.assertIn(name, public_mail.__all__)


if __name__ == "__main__":
    unittest.main()
