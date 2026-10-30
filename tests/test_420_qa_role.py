# SPDX-License-Identifier: AGPL-3.0-only
"""L244 (#420) : le rôle QA et son verdict dans la porte des releases IT.

Le verdict QA est une évidence du module IT, liée à la livraison courante,
écrite par un porteur du rôle `qa` du projet, distinct de l'assigne, de
l'auteur de la livraison et de celui de la preuve. La règle opt-in
`require_qa` exige un verdict `ok` courant pour les états qu'elle garde ;
l'audit rapporte `missing_qa` et `stale_qa`.

Les contrôles de rôle et de séparation des devoirs vivent dans le garde SQL
(migration 0427) : ils valent pour les deux pilotes et pour tout appel direct
au stockage, pas seulement pour la CLI.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tests import support  # noqa: F401  (isole la config de l'hôte avant les modules)
from tests.support import PgTestCase
from ameesh import canon_sync, config as config_mod, db as db_mod, identity, modules, storage, work
from ameesh.modules import it
from ameesh.modules.storage import ModuleStore

from .test_canon import _CanonDbCase, fiche, write


def federation(project="acme", require_qa=None):
    spec = {"it": {"require_qa": require_qa}} if require_qa is not None else ["it"]
    return {"extensions": {"ameesh": {"projects": {project: {"modules": spec}}}}}


def card(**fields) -> str:
    declared = dict(type="Agent", title="orchestre", responsible="human:alice",
                    capabilities=["read", "report-drift", "propose"], harness="claude",
                    model="claude-opus", provider="anthropic",
                    credential_mode="subscription")
    declared.update(fields)
    return fiche(**{k: v for k, v in declared.items() if v is not None})


class QaDeclarationTest(unittest.TestCase):
    """Le descripteur et les déclarations, sans base."""

    def test_require_qa_adds_the_gate_rule_and_nothing_else(self):
        plain = modules.declarations(federation())[0]
        self.assertFalse(any(r.get("evidence_kind") == "qa" for r in plain["rules"]))
        gated = modules.declarations(federation(require_qa=["promoted"]))[0]
        self.assertEqual(gated["rules"][-1], {
            "types": ["release", "publication"], "states": ["promoted"],
            "subject_kind": "delivery", "evidence_kind": "qa", "evidence_verdict": "ok"})
        self.assertEqual(gated["types"], plain["types"])
        # R2 #420 (B3) : la porte QA n'a de sens que si la preuve est
        # attribuable à un agent lié à son exécuteur ; le reste du schéma est
        # inchangé.
        self.assertEqual(gated["schemas"]["proof"],
                         dict(plain["schemas"]["proof"], runner_bound_actor=True))
        self.assertEqual(gated["schemas"]["qa"], plain["schemas"]["qa"])
        self.assertEqual(gated["schemas"]["delivery"], plain["schemas"]["delivery"])

    def test_qa_schema_binds_the_delivery_and_declares_roles_and_duties(self):
        schema = modules.declarations(federation())[0]["schemas"]["qa"]
        self.assertEqual(schema["writer_role"], "ameesh_it_qa")
        self.assertEqual(schema["actor_role"], "qa")
        self.assertEqual(schema["binding_kind"], "delivery")
        self.assertEqual(schema["subject_field"], "sha256")
        self.assertEqual(schema["patterns"]["sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(schema["patterns"]["verdict"], "^(ok|blocked)$")
        self.assertEqual(schema["separation_of_duties"],
                         {"assignee": True, "declaration": True, "evidence": "proof"})

    def test_list_and_mapping_forms_are_equivalent(self):
        listed = modules.declarations(federation())[0]
        mapped = modules.declarations({"extensions": {"ameesh": {"projects": {
            "acme": {"modules": {"it": None}}}}}})[0]
        self.assertEqual(listed, mapped)

    def test_unknown_options_and_states_are_refused(self):
        for spec in ({"wat": 1}, {"require_qa": "promoted"}, {"require_qa": [3]},
                     {"require_qa": ["promoted", "promoted"]}, {"require_qa": ["intake"]},
                     {"require_qa": ["closed"]}, {"require_qa": ["qa"]}):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                modules.declarations({"extensions": {"ameesh": {"projects": {
                    "acme": {"modules": {"it": spec}}}}}})

    def test_both_qa_states_are_accepted(self):
        rule = modules.declarations(federation(require_qa=["merged", "promoted"]))[0]["rules"][-1]
        self.assertEqual(rule["states"], ["merged", "promoted"])


class RequireBoundTest(unittest.TestCase):
    """R2 #420 (N2) : une signature de droits exige une identité liée."""

    def test_unbound_and_explicit_identities_are_refused(self):
        cfg = config_mod.Config()
        explicit = identity.Binding(name="qa-1", source="explicit", ok=True)
        with mock.patch.object(identity, "resolve_binding", return_value=explicit):
            with self.assertRaisesRegex(ValueError, "not bound"):
                identity.require_bound(cfg, None, what="the QA verdict")
        unbound = identity.Binding(reason="AGENT_MAIL_NAME not set")
        with mock.patch.object(identity, "resolve_binding", return_value=unbound):
            with self.assertRaisesRegex(ValueError, "bound session identity.*AGENT_MAIL_NAME not set"):
                identity.require_bound(cfg, None, what="the QA verdict")

    def test_runner_and_session_bindings_are_accepted(self):
        cfg = config_mod.Config()
        runner = identity.Binding(name="qa-1", source="runner", runner_id="r", epoch=2, ok=True)
        with mock.patch.object(identity, "resolve_binding", return_value=runner):
            self.assertEqual(identity.require_bound(cfg, None).name, "qa-1")
        session = identity.Binding(name="qa-1", source="session", ok=True)
        with mock.patch.object(identity, "resolve_binding", return_value=session):
            self.assertEqual(identity.require_bound(cfg, None).name, "qa-1")
        with mock.patch.object(identity, "resolve_binding", return_value=runner):
            with self.assertRaisesRegex(ValueError, "not bound"):
                identity.require_bound(cfg, None, sources=("session",))


class RolesPersistenceTest(PgTestCase):
    """Les rôles d'une fiche sont écrits au registre et comparés comme une liste."""

    def declared(self, roles):
        return {
            "harness": "deepseek", "host": "h1", "cwd": "/work/dev", "model": "m",
            "budget_usd": None, "responsible": None, "team": "acme", "provider": "deepseek",
            "credential_mode": "api-key", "capabilities": ["read"], "canon_ref": "card",
            "priority": 0, "admitted_hosts": [], "admitted_tags": [], "memory_repository": None,
            "visibility_ok": True, "visibility_diagnostic": "", "placement_ok": True,
            "placement_diagnostic": "", "placement_ref": "placement", "placement_profile": None,
            "canon": None, "ephemeral": False, "projects": ["acme"], "roles": roles,
            # L165 (#3010) : relève et hôtes candidats, déclaratifs comme le
            # placement ; `write_declared` pose `hote` par défaut.
            "releve": "hote", "candidats": None,
            "_expected_lease_epoch": -1,
        }

    def test_roles_are_written_read_and_compared(self):
        self.assertTrue(storage.of(self.db).canon.write_declared("dev", self.declared(["qa"])))
        row = {r["name"]: r for r in storage.of(self.db).canon.host_rows("h1", ["dev"])}["dev"]
        self.assertEqual(row["roles"], ["qa"])
        self.assertTrue(canon_sync._same(row, self.declared(["qa"])))
        self.assertFalse(canon_sync._same(row, self.declared(["qa", "relecture"])))
        self.assertFalse(canon_sync._same(row, self.declared(None)))
        self.assertIn("roles", canon_sync.DECLARATIVE)


class RolesSyncTest(_CanonDbCase):
    """La fiche porte les rôles ; `canon sync` les écrit et les compare."""

    def roles(self) -> list[str] | None:
        rows = self.db.query("SELECT roles FROM agent_registry WHERE name = 'orchestre'")
        return rows[0]["roles"] if rows else None

    def test_sync_writes_the_declared_roles_and_their_order(self):
        write(self.root, "agents/orchestre.md", card(roles=["qa"]))
        self.sync()
        self.assertEqual(self.roles(), ["qa"])
        self.assertEqual(self.actions(self.sync())["orchestre"], "inchangé")
        write(self.root, "agents/orchestre.md", card(roles=["qa", "relecture"]))
        self.assertNotEqual(self.actions(self.sync())["orchestre"], "inchangé")
        self.assertEqual(self.roles(), ["qa", "relecture"])
        # Une fiche sans `roles` remet la colonne à NULL : aucun rôle hérité.
        write(self.root, "agents/orchestre.md", card())
        self.sync()
        self.assertIsNone(self.roles())

    def test_declared_roles_are_normalized_and_role_is_accepted(self):
        """N1 (#420 R2) : `roles` est normalisé, `role` au singulier est lu."""
        write(self.root, "agents/orchestre.md", card(roles=["QA", " qa ", "Relecture"]))
        self.sync()
        self.assertEqual(self.roles(), ["qa", "relecture"])
        write(self.root, "agents/orchestre.md", card(role="Qa"))
        self.sync()
        self.assertEqual(self.roles(), ["qa"])


class QaEvidenceTest(PgTestCase):
    """Rôle, projet, séparation des devoirs et porte, par le garde SQL."""

    def setUp(self):
        super().setUp()
        with self.db.transaction() as tx:
            ModuleStore(tx).replace("", modules.declarations(federation(require_qa=["promoted"])))
        self.artifact = Path(self.tmp, "artifact.py")
        self.artifact.write_text("print('qa smoke passed')\n")
        self.lot = work.add(self.db, title="Publication", type="release", app="acme")["id"]
        self.register("qa-1", roles=["qa"], projects=["acme"])
        self.register("qa-other", roles=["qa"], projects=["other"])
        self.register("dev-1", roles=["implementation"], projects=["acme"])
        self.register("reviewer-1", roles=["relecture"], projects=["acme"])

    def register(self, name, roles=None, projects=None, team="", runner_id=None, epoch=2):
        def param(values):
            return (None, None) if values is None else (json.dumps(values), json.dumps(values))
        roles_a, roles_b = param(roles)
        projects_a, projects_b = param(projects)
        self.db.execute(
            "INSERT INTO agent_registry(name, team, roles, projects, lease_owner, lease_epoch) VALUES "
            "(%s, %s, CASE WHEN %s::jsonb IS NULL THEN NULL "
            "ELSE ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)) END, "
            "CASE WHEN %s::jsonb IS NULL THEN NULL "
            "ELSE ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)) END, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET team = excluded.team, roles = excluded.roles, "
            "projects = excluded.projects, lease_owner = excluded.lease_owner, "
            "lease_epoch = excluded.lease_epoch",
            (name, team, roles_a, roles_b, projects_a, projects_b,
             "runner-%s" % name if runner_id is None else runner_id, epoch))

    def deliver(self, actor="dev-1"):
        return it.declare(self.db, self.lot, self.artifact, actor=actor)

    def prove(self, actor="dev-1", runner_id=None, epoch=2):
        """Une preuve attribuable (R2 #420, B3) : l'agent, lié à son exécuteur."""
        return it.prove(self.db, self.lot, self.artifact, actor=actor, runner=sys.executable,
                        identity={"agent": actor,
                                  "runner_id": runner_id or "runner-%s" % actor,
                                  "lease_epoch": epoch})

    def isolated_qa_role(self):
        """Un rôle QA isolé, avec les seuls droits de `role-module-writers.sql`."""
        role = "t_qa_writer_%s" % uuid.uuid4().hex[:8]
        q = db_mod.quote_ident
        self.db.execute("CREATE ROLE %s NOLOGIN" % q(role))
        self.addCleanup(self.drop_role, role)
        with self.db.transaction() as tx:
            tx.execute("GRANT USAGE ON SCHEMA %s TO %s" % (q(self.schema), q(role)))
            # #2481: the full work row projects its reserved migration slots.
            tx.execute("GRANT SELECT ON work_items, project_modules, work_module_milestones, "
                       "work_migration_reservations TO %s" % q(role))
            tx.execute("GRANT UPDATE (id) ON work_items TO %s" % q(role))
            tx.execute("GRANT INSERT ON work_module_milestones TO %s" % q(role))
            tx.execute("GRANT USAGE ON SEQUENCE work_module_milestones_id_seq TO %s" % q(role))
            tx.execute("GRANT EXECUTE ON FUNCTION ameesh_module_agent(text) TO %s" % q(role))
        return role

    def drop_role(self, role):
        q = db_mod.quote_ident
        if self.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)):
            self.db.execute("DROP OWNED BY %s" % q(role))
            self.db.execute("DROP ROLE %s" % q(role))

    def current_delivery(self):
        return ModuleStore(self.db).latest(self.lot, "it", "delivery")

    def qa(self, actor="qa-1", verdict="ok", digest=None):
        delivery = self.current_delivery()
        return it.record_qa(self.db, self.lot, sha256=digest or delivery["subject"],
                            verdict=verdict, scope=["install"], report="https://qa.test/run",
                            actor=actor)

    def go_and_merge(self):
        # Le verdict est posé au stockage : ce témoin teste la porte QA et la
        # fusion, pas l'écriture d'un verdict (#2962).
        storage.of(self.db).work.add_milestone(
            self.lot, "verdict", sha="a" * 40, actor="relecteur", verdict="ok",
            note="")
        return work.close_merged(self.db, self.lot, sha="a" * 40)

    # -- rôle et portée ---------------------------------------------------

    def test_qa_holder_of_the_project_records_a_bound_verdict(self):
        self.deliver()
        self.prove()
        delivery = self.current_delivery()
        row = self.qa()
        self.assertEqual((row["module"], row["kind"]), ("it", "qa"))
        self.assertEqual(row["actor"], "qa-1")
        self.assertEqual(row["binding_id"], delivery["id"])
        self.assertEqual(row["subject"], delivery["subject"])
        self.assertEqual(row["payload"]["scope"], ["install"])
        self.assertEqual(row["payload"]["verdict"], "ok")

    def test_actor_without_the_qa_role_is_refused(self):
        self.deliver()
        self.prove()
        for actor in ("dev-1", "reviewer-1", "not-registered"):
            with self.subTest(actor=actor), self.assertRaisesRegex(db_mod.DbError,
                                                                   "qa-role-required"):
                self.qa(actor=actor)

    def test_qa_role_outside_the_lot_project_is_refused(self):
        self.deliver()
        self.prove()
        with self.assertRaisesRegex(db_mod.DbError, "qa-role-scope"):
            self.qa(actor="qa-other")

    def test_global_qa_has_no_project_scope_and_a_team_fallback_applies(self):
        self.register("qa-global", roles=["qa"], projects=[])
        self.register("qa-team", roles=["qa"], projects=None, team="acme")
        self.deliver()
        self.prove()
        self.assertEqual(self.qa(actor="qa-global")["actor"], "qa-global")
        self.assertEqual(self.qa(actor="qa-team")["actor"], "qa-team")
        self.register("qa-elsewhere", roles=["qa"], projects=None, team="other")
        with self.assertRaisesRegex(db_mod.DbError, "qa-role-scope"):
            self.qa(actor="qa-elsewhere")

    # -- séparation des devoirs ------------------------------------------

    def test_the_lot_assignee_cannot_record_the_verdict(self):
        self.db.execute("UPDATE work_items SET assignee = 'qa-1' WHERE id = %s", (self.lot,))
        self.deliver()
        self.prove()
        with self.assertRaisesRegex(db_mod.DbError, "qa-same-actor"):
            self.qa(actor="qa-1")

    def test_the_delivery_author_cannot_record_the_verdict(self):
        self.deliver(actor="qa-1")
        self.prove()
        with self.assertRaisesRegex(db_mod.DbError, "qa-same-actor"):
            self.qa(actor="qa-1")

    def test_the_proof_author_cannot_record_the_verdict(self):
        self.deliver()
        self.prove(actor="qa-1")
        with self.assertRaisesRegex(db_mod.DbError, "qa-same-actor"):
            self.qa(actor="qa-1")

    def test_a_later_proof_by_another_actor_does_not_free_the_proof_author(self):
        self.deliver()
        self.prove(actor="qa-1")
        self.prove(actor="dev-1")  # second proof of the same delivery
        with self.assertRaisesRegex(db_mod.DbError, "qa-same-actor"):
            self.qa(actor="qa-1")

    def test_a_proof_after_the_verdict_by_its_author_is_refused(self):
        """R2 #420 (B3) : une preuve écrite APRÈS le verdict, par son auteur."""
        self.deliver()
        self.prove(actor="dev-1")
        self.qa(actor="qa-1")
        with self.assertRaisesRegex(db_mod.DbError, "qa-same-actor"):
            self.prove(actor="qa-1")

    def test_the_gate_rechecks_the_separation_of_duties(self):
        """R2 #420 (B3) : la porte revérifie la séparation, pas seulement l'écriture.

        La preuve est insérée hors du garde (comme une ligne héritée ou une
        course) : la promotion doit la refuser."""
        self.deliver()
        self.prove(actor="dev-1")
        self.qa(actor="qa-1")
        self.go_and_merge()
        self.db.execute("ALTER TABLE work_module_milestones "
                        "DISABLE TRIGGER ameesh_module_milestone_guard")
        try:
            self.prove(actor="qa-1")
        finally:
            self.db.execute("ALTER TABLE work_module_milestones "
                            "ENABLE TRIGGER ameesh_module_milestone_guard")
        with self.assertRaisesRegex(db_mod.DbError, "qa-same-actor"):
            work.move(self.db, self.lot, "promoted")

    def test_a_qa_gated_proof_must_be_runner_bound(self):
        """R2 #420 (B3) : sous `require_qa`, la preuve nomme l'agent lié."""
        self.deliver()
        with self.assertRaisesRegex(db_mod.DbError, "identity-required"):
            it.prove(self.db, self.lot, self.artifact, actor="dev-1", runner=sys.executable)
        with self.assertRaisesRegex(db_mod.DbError, "identity-unbound"):
            it.prove(self.db, self.lot, self.artifact, actor="dev-1", runner=sys.executable,
                     identity={"agent": "dev-1", "runner_id": "runner-other",
                               "lease_epoch": 2})
        proof = self.prove()
        self.assertEqual(proof["payload"]["agent"], "dev-1")
        self.assertEqual(proof["payload"]["runner_id"], "runner-dev-1")

    # -- porte et nouvelle livraison -------------------------------------

    def test_promotion_needs_a_current_ok_verdict(self):
        self.deliver()
        self.prove()
        self.go_and_merge()
        with self.assertRaises(db_mod.DbError) as caught:
            work.move(self.db, self.lot, "promoted")
        self.assertIn("qa", str(caught.exception))
        self.qa()
        self.assertEqual(work.move(self.db, self.lot, "promoted")["state"], "delivered")

    def test_blocked_verdict_keeps_the_gate_closed_until_a_later_ok(self):
        self.deliver()
        self.prove()
        self.go_and_merge()
        self.qa(verdict="blocked")
        with self.assertRaises(db_mod.DbError):
            work.move(self.db, self.lot, "promoted")
        self.qa()
        self.assertEqual(work.move(self.db, self.lot, "promoted")["state"], "delivered")

    def test_ok_then_blocked_keeps_the_gate_closed(self):
        """R2 #420 (B1) : c'est le DERNIER verdict qui décide, pas un ok ancien."""
        self.deliver()
        self.prove()
        self.go_and_merge()
        self.qa()  # ok
        self.qa(verdict="blocked")
        with self.assertRaises(db_mod.DbError):
            work.move(self.db, self.lot, "promoted")
        self.qa()  # un ok ultérieur rouvre la porte
        self.assertEqual(work.move(self.db, self.lot, "promoted")["state"], "delivered")

    def test_a_new_declaration_invalidates_the_verdict_even_for_same_bytes(self):
        self.deliver()
        self.prove()
        self.qa()
        self.go_and_merge()
        self.deliver()  # same bytes, new declaration: the binding moves
        self.prove()
        with self.assertRaises(db_mod.DbError):
            work.move(self.db, self.lot, "promoted")
        self.qa()
        self.assertEqual(work.move(self.db, self.lot, "promoted")["state"], "delivered")

    def test_a_return_to_the_old_digest_is_still_a_new_delivery(self):
        self.deliver()
        self.prove()
        self.qa()
        self.go_and_merge()
        self.artifact.write_text("print('second artifact')\n")
        self.deliver()
        self.prove()
        self.qa()
        self.artifact.write_text("print('qa smoke passed')\n")  # back to the first digest
        self.deliver()
        self.prove()
        with self.assertRaises(db_mod.DbError):
            work.move(self.db, self.lot, "promoted")
        self.qa()
        self.assertEqual(work.move(self.db, self.lot, "promoted")["state"], "delivered")

    def test_the_verdict_must_match_the_current_delivery(self):
        self.deliver()
        self.prove()
        with self.assertRaisesRegex(ValueError, "current delivery"):
            self.qa(digest="f" * 64)
        self.assertIsNone(ModuleStore(self.db).latest(self.lot, "it", "qa"))

    def test_a_project_without_require_qa_still_promotes_without_verdict(self):
        with self.db.transaction() as tx:
            ModuleStore(tx).replace("", modules.declarations(federation(require_qa=None)))
        self.deliver()
        self.prove()
        self.go_and_merge()
        self.assertEqual(work.move(self.db, self.lot, "promoted")["state"], "delivered")

    # -- CLI --------------------------------------------------------------

    def test_cli_records_the_verdict_and_its_scope(self):
        self.deliver()
        self.prove()
        binding = identity.Binding(name="qa-1", source="runner", runner_id="runner-qa-1",
                                   epoch=2, ok=True)
        out = io.StringIO()
        with mock.patch.object(identity, "require_bound", return_value=binding), \
                contextlib.redirect_stdout(out):
            code = it.main(self.db, ["qa", str(self.lot), "--sha256",
                                     self.current_delivery()["subject"], "--verdict", "ok",
                                     "--scope", "install, login", "--report", "https://qa.test/cli"],
                           actor="qa-1")
        self.assertEqual(code, 0)
        row = json.loads(out.getvalue())
        self.assertEqual(row["kind"], "qa")
        self.assertEqual(row["payload"]["scope"], ["install", "login"])
        self.assertEqual(row["payload"]["report"], "https://qa.test/cli")

    def test_cli_signs_with_the_bound_session_not_the_declared_name(self):
        """R2 #420 (N2) : le nom auto-déclaré ne signe pas à la place du bail."""
        self.deliver()
        self.prove()
        binding = identity.Binding(name="qa-1", source="runner", runner_id="runner-qa-1",
                                   epoch=2, ok=True)
        with mock.patch.object(identity, "require_bound", return_value=binding), \
                self.assertRaisesRegex(ValueError, "signed by the bound session"):
            it.main(self.db, ["qa", str(self.lot), "--sha256",
                              self.current_delivery()["subject"], "--verdict", "ok",
                              "--report", "https://qa.test/impostor"], actor="qa-other")

    # -- rôle QA isolé (R2 #420, B2) --------------------------------------

    def test_isolated_qa_writer_records_the_verdict(self):
        """Un rôle QA avec les seuls droits de `role-module-writers.sql`.

        Le garde lit le registre par `ameesh_module_agent` (SECURITY DEFINER,
        search_path figé) : une lecture directe `FOR SHARE` exigerait UPDATE
        sur `agent_registry`, que ce rôle n'a pas (R2 #420, B2)."""
        role = self.isolated_qa_role()
        entries = modules.declarations(federation(require_qa=["promoted"]))
        entries[0]["schemas"]["qa"]["writer_role"] = role
        with self.db.transaction() as tx:
            ModuleStore(tx).replace("", entries)
        self.deliver()
        self.prove()
        delivery = self.current_delivery()
        q = db_mod.quote_ident
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL ROLE %s" % q(role))
            row = it.record_qa(tx, self.lot, sha256=delivery["subject"], verdict="ok",
                               scope=["install"], report="https://qa.test/isolated",
                               actor="qa-1")
        self.assertEqual(row["actor"], "qa-1")
        self.assertEqual(row["payload"]["verdict"], "ok")

    # -- audit ------------------------------------------------------------

    def test_temp_registry_cannot_forge_qa_role_or_runner_binding(self):
        role = self.isolated_qa_role()
        entries = modules.declarations(federation(require_qa=["promoted"]))
        entries[0]["schemas"]["qa"]["writer_role"] = role
        ModuleStore(self.db).replace("", entries)
        self.deliver()
        delivery = self.current_delivery()
        with self.assertRaisesRegex(db_mod.DbError, "qa-role-required"), self.db.transaction() as tx:
            # Keep all statements on one connection, including with psql.
            # Model a caller with TEMP despite the deployment audit.
            tx.execute("CREATE TEMP TABLE agent_registry (name text, roles text[], "
                       "projects text[], team text, lease_owner text, lease_epoch bigint)")
            tx.execute("INSERT INTO pg_temp.agent_registry VALUES "
                       "('reviewer-1', ARRAY['qa'], ARRAY['acme'], '', 'forged-runner', 999)")
            tx.execute("GRANT ALL ON pg_temp.agent_registry TO %s" % db_mod.quote_ident(role))
            tx.execute("SET LOCAL ROLE %s" % db_mod.quote_ident(role))
            row = tx.query("SELECT * FROM ameesh_module_agent('reviewer-1')")[0]
            self.assertEqual(row["roles"], ["relecture"])
            self.assertEqual(row["lease_owner"], "runner-reviewer-1")
            self.assertEqual(row["lease_epoch"], 2)
            it.record_qa(tx, self.lot, sha256=delivery["subject"], verdict="ok",
                         scope=["install"], report="https://qa.test/forged",
                         actor="reviewer-1")

    def test_audit_reports_missing_then_stale_qa(self):
        # Le projet est promu avant l'activation de la règle : le constat suit
        # l'état courant, pas la date de la promotion.
        with self.db.transaction() as tx:
            ModuleStore(tx).replace("", modules.declarations(federation(require_qa=None)))
        self.deliver()
        self.prove()
        self.go_and_merge()
        work.move(self.db, self.lot, "promoted")
        self.assertEqual([f for f in modules.audit(self.db) if f["rule"].endswith("qa")], [])
        with self.db.transaction() as tx:
            ModuleStore(tx).replace("", modules.declarations(federation(require_qa=["promoted"])))
        findings = [f for f in modules.audit(self.db) if f["rule"] in ("missing_qa", "stale_qa")]
        self.assertEqual([(f["rule"], f["lot"], f["lot_state"]) for f in findings],
                         [("missing_qa", self.lot, "delivered")])
        self.qa()
        self.assertEqual([f for f in modules.audit(self.db) if f["rule"] in ("missing_qa", "stale_qa")],
                         [])
        self.deliver()  # new declaration: the verdict becomes stale
        self.prove()
        findings = [f for f in modules.audit(self.db) if f["rule"] in ("missing_qa", "stale_qa")]
        self.assertEqual([(f["rule"], f["lot"]) for f in findings], [("stale_qa", self.lot)])
        self.qa(verdict="blocked")
        findings = [f for f in modules.audit(self.db) if f["rule"] in ("missing_qa", "stale_qa")]
        self.assertEqual([(f["rule"], f["lot"]) for f in findings], [("missing_qa", self.lot)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
