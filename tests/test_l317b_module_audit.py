# SPDX-License-Identifier: AGPL-3.0-only
"""IT audit evidence and the generic human/agent reporting circuit."""
from pathlib import Path
import sys
import unittest
from unittest import mock

from tests import support
from tests.support import PgTestCase
from tests.test_l317_project_modules import federation
from ameesh import alert_briefs, db as db_mod, exploitation, modules, notify, registry, work
from ameesh.modules import it
from ameesh.modules.storage import ModuleStore


def missing(**changes):
    return dict(id=42, title="Release", type="release", state="qa", assignee="dev",
                declaration_id=17, declaration_ts=100., created_ts=90., subject="a" * 64,
                proved=False, **changes)


class AuditReportingTest(unittest.TestCase):
    def finding(self):
        store = mock.Mock()
        store.audit_evidence.return_value = [missing()]
        return it.audit(store, "acme")[0]

    def test_no_active_modules_and_unknown_module_do_not_run_it(self):
        for entries in ([], [{"project": "acme", "module": "unknown"}]):
            with mock.patch.object(ModuleStore, "active", return_value=entries), \
                    mock.patch.object(it, "audit") as audit:
                self.assertEqual(modules.audit(mock.Mock()), [])
                audit.assert_not_called()

    def test_old_schema_is_supported_but_unavailability_propagates(self):
        db = mock.Mock()
        db.query.return_value = [{"relation": None}]
        self.assertEqual(ModuleStore(db).active(), [])
        db.query.side_effect = db_mod.Unavailable("offline")
        with self.assertRaises(db_mod.Unavailable):
            ModuleStore(db).active()

    def test_module_is_the_only_source_of_rule_and_findings(self):
        with mock.patch.object(ModuleStore, "active", return_value=[{"project": "acme", "module": "it"}]), \
                mock.patch.object(it, "audit", return_value=[self.finding()]) as audit:
            result = modules.audit(mock.Mock())
        audit.assert_called_once_with(mock.ANY, "acme")
        self.assertEqual(result[0]["rule"], "execution-proof")

    def test_project_routing_does_not_follow_assignee_team(self):
        listing = [{"name": "orch", "chantier": "acme", "team": "different"},
                   {"name": "other", "chantier": "other", "team": "devteam"},
                   {"name": "dev", "chantier": "acme", "team": "devteam"},
                   {"name": "auditeur"}]
        with mock.patch.object(modules, "audit", return_value=[self.finding()]), \
                mock.patch("ameesh.sous_utilisation.orchestrators", return_value=["orch", "other"]):
            findings = exploitation._module_deviations(None, mock.Mock(), listing)
        self.assertEqual(findings[0]["orchestrators"], ["orch"])
        self.assertEqual(findings[0]["schema"], exploitation.ALERT_SCHEMA)
        briefs = alert_briefs.briefs(findings[0], listing, auditor_names=["auditeur"])
        self.assertEqual(set(briefs), {"orch", "auditeur"})
        self.assertIn("execution proof", briefs["orch"])

    def test_unknown_project_orchestrator_falls_back_to_known_orchestrators(self):
        with mock.patch.object(modules, "audit", return_value=[self.finding()]), \
                mock.patch("ameesh.sous_utilisation.orchestrators", return_value=["orch"]):
            alerts = exploitation._module_deviations(None, mock.Mock(), [{"name": "orch"}])
        self.assertEqual(alerts[0]["orchestrators"], ["orch"])

    def test_new_declaration_and_other_rule_are_new_episodes_but_assignment_is_not(self):
        alert = self.finding()
        key = notify.key_of(alert)
        self.assertNotEqual(key, notify.key_of(dict(alert, declaration_id=18)))
        self.assertNotEqual(key, notify.key_of(dict(alert, rule="other")))
        self.assertEqual(key, notify.key_of(dict(alert, agent="reassigned")))

    def test_auditing_never_executes_artifact(self):
        with mock.patch("ameesh.modules.it.execution.execute") as execute:
            self.finding()
        execute.assert_not_called()

    def test_generic_alert_defaults_to_human_channel_and_has_readable_details(self):
        self.assertIn("module_deviation", notify.parse_config({}).types)
        message = notify.render(self.finding(), "raised", 120)
        self.assertIn("lot #42", message.body)
        self.assertIn("execution proof", message.body)
        self.assertIn("acme", message.body)


class AuditEvidenceTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.store = ModuleStore(self.db)
        self.store.replace("", modules.declarations(federation()))
        self.lot = work.add(self.db, title="Release", type="release", app="acme")["id"]
        self.artifact = Path(self.tmp, "artifact.py")
        self.artifact.write_text("print('smoke passed')\n")

    def findings(self):
        return modules.audit(self.db)

    def prove(self):
        it.declare(self.db, self.lot, self.artifact, actor="test")
        it.prove(self.db, self.lot, self.artifact, actor="test", runner=sys.executable)

    def test_missing_delivery_missing_proof_resolved_then_identical_redeclaration(self):
        finding = self.findings()[0]
        self.assertIsNone(finding["declaration_id"])
        self.assertIn("delivery declaration missing", finding["detail"])
        it.declare(self.db, self.lot, self.artifact, actor="test")
        declared = self.findings()[0]
        self.assertNotEqual(notify.key_of(finding), notify.key_of(declared))
        self.prove()
        self.assertEqual(self.findings(), [])
        it.declare(self.db, self.lot, self.artifact, actor="test")
        stale = self.findings()[0]
        self.assertIn("stale", stale["detail"])
        self.assertNotEqual(notify.key_of(declared), notify.key_of(stale))

    def test_publication_workstream_scope_and_closed_lots(self):
        publication = work.add(self.db, title="Store", type="publication", workstream="acme")["id"]
        core = work.add(self.db, title="Core", app="other")["id"]
        self.assertEqual({a["lot"] for a in self.findings()}, {self.lot, publication})
        work.close(self.db, publication, abandoned=True)
        self.assertEqual({a["lot"] for a in self.findings()}, {self.lot})
        self.assertNotIn(core, {a["lot"] for a in self.findings()})

    def test_inactive_project_not_audited(self):
        self.store.replace("", [])
        self.assertEqual(self.findings(), [])

    def test_historical_merged_and_promoted_without_proof_are_audited(self):
        # Simulate rows imported before the lifecycle guard existed, without
        # weakening the runtime guard or introducing an import path.
        for state in ("merged", "promoted"):
            with self.db.transaction() as tx:
                tx.execute("ALTER TABLE work_items DISABLE TRIGGER ameesh_module_work_guard")
                tx.execute("UPDATE work_items SET state = %s WHERE id = %s", (state, self.lot))
                tx.execute("ALTER TABLE work_items ENABLE TRIGGER ameesh_module_work_guard")
            self.assertEqual(self.findings()[0]["lot_state"], state)

    def test_notifier_delivers_human_channel_and_events_once_then_resolves(self):
        for name in ("orch", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        sender = notify.DrySender()
        ncfg = notify.parse_config({"default_human": "human:owner", "default": ["desktop"]})
        notifier = notify.Notifier(self.cfg, ncfg, sender=sender, log=lambda text: None, clock=lambda: 200.)
        alert = dict(self.findings()[0], orchestrators=["orch"])
        notifier.run_pass(self.db, current=[alert])
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("IT procedure deviation", sender.sent[0][1].body)
        mails = self.db.query("SELECT recipient, kind, payload FROM agent_mailbox ORDER BY id")
        self.assertEqual({m["recipient"] for m in mails}, {"orch", "auditeur"})
        self.assertTrue(all(m["kind"] == "event" and m["payload"]["module"] == "it" for m in mails))
        self.prove()
        notifier.run_pass(self.db, current=self.findings())
        self.assertEqual(len(sender.sent), 2)
        self.assertIn("resolved", sender.sent[-1][1].body)
        self.assertEqual(notifier.state["briefs"], {})
