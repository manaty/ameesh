# SPDX-License-Identifier: AGPL-3.0-only
"""IT audit evidence and the generic human/agent reporting circuit."""
from pathlib import Path
import dataclasses
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import support
from tests.support import PgTestCase
from tests.test_l317_project_modules import federation
from ameesh import alert_briefs, db as db_mod, exploitation, fil, mail, modules, notify, registry, storage, work
from ameesh.modules import it
from ameesh.modules.storage import ModuleStore


def missing(**changes):
    return dict(id=42, title="Release", type="release", state="qa", assignee="dev",
                declaration_id=17, declaration_ts=100., created_ts=90., subject="a" * 64,
                proved=False, **changes)


def auditor_canon(*names):
    return SimpleNamespace(agents=[SimpleNamespace(
        title=name, fiche=SimpleNamespace(data={"roles": ["auditor"]})) for name in names])


def reporting_canon():
    c = auditor_canon("auditeur")
    c.agents.append(SimpleNamespace(title="orch", fiche=SimpleNamespace(data={"roles": ["orchestrator"]})))
    return c


class AuditReportingTest(unittest.TestCase):
    def finding(self):
        store = mock.Mock()
        store.audit_evidence.return_value = [missing()]
        # Pas de règle require_qa dans cette politique : le constat reste celui
        # de la preuve d'exécution (L244 ajoute les constats QA à part).
        store.policy.return_value = None
        return it.audit(store, "acme")[0]

    def test_no_active_modules_and_unknown_module_do_not_run_it(self):
        for entries in ([], [{"project": "acme", "module": "unknown"}]):
            with mock.patch.object(ModuleStore, "active", return_value=entries), \
                    mock.patch.object(it, "audit") as audit, \
                    mock.patch("ameesh.modules.dev.audit", return_value=[]) as dev_audit:
                self.assertEqual(modules.audit(mock.Mock()), [])
                audit.assert_not_called()
            # L101 T4 : le module toujours actif `dev` est audité une fois,
            # sans projet (portée hôte).
            dev_audit.assert_called_once_with(mock.ANY, None)

    def test_always_active_dev_runs_once_through_the_real_audit_path(self):
        """L101 T4: `dev` has no `project_modules` row (the canon refuses its
        selection), yet the common `modules.audit(db)` path returns its host
        findings once, alongside the project modules really activated."""
        from ameesh import canon
        finding = canon.Finding("host-work-dir-missing", canon.WARNING,
                                "agent a on host h: no working directory",
                                host="h", agent="a", member="m", path="p")
        active = [{"project": "acme", "module": "it"},     # real activations:
                  {"project": "other", "module": "it"}]    # no `dev` row exists
        with mock.patch.object(ModuleStore, "active", return_value=active), \
                mock.patch.object(it, "audit", return_value=[]) as it_audit, \
                mock.patch("ameesh.config.load", return_value=object()), \
                mock.patch("ameesh.canon.load_configured", return_value=[object()]), \
                mock.patch("ameesh.canon.validate", return_value=[finding]):
            result = modules.audit(mock.Mock())
        self.assertEqual(it_audit.call_count, 2)          # once per project
        # One host pass, not one per project, and no project owns it.
        self.assertEqual([(f["module"], f["rule"], f["project"], f["host"])
                          for f in result],
                         [("dev", "host-work-dir-missing", None, "h")])

    def test_always_active_dev_is_absent_in_the_bare_profile(self):
        with mock.patch("ameesh.modules._builtin", return_value={}), \
                mock.patch("ameesh.modules._entry_points", return_value=()), \
                mock.patch.object(ModuleStore, "active", return_value=[]), \
                mock.patch("ameesh.modules.dev.audit") as dev_audit:
            self.assertEqual(modules.audit(mock.Mock()), [])
        dev_audit.assert_not_called()

    def test_host_scoped_module_findings_keep_distinct_alert_keys(self):
        """L101 T4: two hosts with the same rule are two alerts."""
        base = {"type": "module_deviation", "module": "dev",
                "rule": "host-work-dir-missing", "project": None,
                "lot": None, "declaration_id": None}
        self.assertNotEqual(notify.key_of(dict(base, host="h1", agent="a1")),
                            notify.key_of(dict(base, host="h2", agent="a2")))
        project = dict(base, project="acme", lot=42, declaration_id=7)
        self.assertEqual(notify.key_of(project),
                         notify.key_of(dict(project, agent="reassigned")))

    def test_host_scoped_findings_get_no_project_orchestrator(self):
        """A host setting belongs to no project: never scope it by
        `chantier == None`, which would attach a project-less orchestrator to
        every host finding; a project finding keeps its project route."""
        host = {"type": "module_deviation", "module": "dev",
                "rule": "host-work-dir-missing", "project": None, "lot": None,
                "host": "h1", "agent": "a1", "since": None,
                "detail": "x", "action": "x"}
        project = dict(self.finding(), orchestrators=[])
        listing = [{"name": "global-orch"}, {"name": "orch-acme", "chantier": "acme"}]
        with mock.patch.object(modules, "audit", return_value=[host, project]), \
                mock.patch("ameesh.sous_utilisation.orchestrators",
                           return_value=["global-orch", "orch-acme"]):
            alerts = exploitation._module_deviations(None, mock.Mock(), listing)
        self.assertEqual(alerts[0]["orchestrators"], [])
        self.assertEqual(alerts[1]["orchestrators"], ["orch-acme"])

    def test_old_schema_is_supported_but_unavailability_propagates(self):
        db = mock.Mock()
        db.query.return_value = [{"relation": None}]
        self.assertEqual(ModuleStore(db).active(), [])
        db.query.side_effect = db_mod.Unavailable("offline")
        with self.assertRaises(db_mod.Unavailable):
            ModuleStore(db).active()

    def test_module_is_the_only_source_of_rule_and_findings(self):
        with mock.patch.object(ModuleStore, "active", return_value=[{"project": "acme", "module": "it"}]), \
                mock.patch.object(it, "audit", return_value=[self.finding()]) as audit, \
                mock.patch("ameesh.modules.dev.audit", return_value=[]):
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
        briefs = alert_briefs.briefs(findings[0], listing, auditor_names=["auditeur"],
                                    orchestrator_names=["orch", "other"])
        self.assertEqual(set(briefs), {"orch", "auditeur"})
        self.assertIn("execution proof", briefs["orch"])
        self.assertEqual(alert_briefs.briefs(findings[0], listing), {})

    def test_missing_project_orchestrator_never_falls_back(self):
        listing = [{"name": "orch", "chantier": "other"}, {"name": "global-orch"}]
        with mock.patch.object(modules, "audit", return_value=[self.finding()]), \
                mock.patch("ameesh.sous_utilisation.orchestrators", return_value=["orch", "global-orch"]):
            alerts = exploitation._module_deviations(None, mock.Mock(), listing)
        self.assertEqual(alerts[0]["orchestrators"], [])
        self.assertEqual(alert_briefs.briefs(alerts[0], listing), {})

    def test_briefs_recheck_scope_and_only_explicit_auditors_have_access(self):
        listing = [{"name": "orch", "chantier": "other"}, {"name": "auditeur"},
                   {"name": "local-audit", "chantier": "acme"},
                   {"name": "other-audit", "chantier": "other"}, {"name": "global-audit"}]
        names = alert_briefs.auditors(
            listing, [auditor_canon("local-audit", "other-audit", "global-audit")],
            explicit_only=True)
        self.assertNotIn("auditeur", names)
        alert = dict(self.finding(), orchestrators=["orch", "missing"])
        self.assertEqual(set(alert_briefs.briefs(alert, listing, auditor_names=names)),
                         {"local-audit", "global-audit"})
        self.assertEqual(alert_briefs.briefs(dict(alert, project=None), listing,
                                           auditor_names=names), {})

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
            self.assertEqual(self.findings()[0]["lot_state"], "delivered")
            self.assertEqual(work.get(self.db, self.lot)["legacy_state"], state)

    def test_notifier_delivers_human_channel_and_events_once_then_resolves(self):
        for name in ("orch", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        sender = notify.DrySender()
        ncfg = notify.parse_config({"default_human": "human:owner", "default": ["desktop"]})
        self.db.execute("UPDATE agent_registry SET chantier = 'acme' WHERE name = 'orch'")
        # #3036 : un brief ne part plus à un agent sans bail vivant ; ce banc
        # simule le runner qui tient les deux destinataires.
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-' || name,"
                        " lease_expires_at = now() + interval '1 hour'"
                        " WHERE name IN ('orch', 'auditeur')")
        cfg = dataclasses.replace(self.cfg, canon="test-canon")
        notifier = notify.Notifier(cfg, ncfg, sender=sender, log=lambda text: None, clock=lambda: 200.)
        alert = dict(self.findings()[0], orchestrators=["orch"])
        with mock.patch("ameesh.canon.load_configured", return_value=[reporting_canon()]):
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

    def scoped_notifier(self, sender=None):
        registry.upsert(self.db, "orch", chantier="acme", harness="claude", host=self.cfg.host)
        # #3036 : le brief n'est plus déposé à un agent sans bail vivant ; ce
        # banc simule un runner qui tient l'orchestrateur.
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-' || name,"
                        " lease_expires_at = now() + interval '1 hour' WHERE name = 'orch'")
        cfg = dataclasses.replace(self.cfg, canon="test-canon")
        ncfg = notify.parse_config({"default_human": "human:owner", "default": ["desktop"]})
        return notify.Notifier(cfg, ncfg, sender=sender or notify.DrySender(),
                               log=lambda text: None, clock=lambda: 200.)

    def test_reassignment_during_owner_notification_cannot_use_cached_recipient(self):
        sender = notify.DrySender()
        notifier = self.scoped_notifier(sender)
        stale = storage.of(self.db).operations.listing()
        alert = dict(self.findings()[0], orchestrators=["orch"])
        original = sender.send

        def send(channel, message):
            original(channel, message)
            registry.upsert(self.db, "orch", chantier="other")

        with mock.patch.object(sender, "send", side_effect=send), \
                mock.patch("ameesh.canon.load_configured", return_value=[reporting_canon()]), \
                mock.patch("ameesh.storage.postgres.operations.Operations.listing", return_value=stale):
            notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(self.db.query("SELECT recipient FROM agent_mailbox"), [])
        self.assertEqual(notifier.state["briefs"], {})

    def test_orchestrator_role_removed_after_preparation_prevents_deposit(self):
        notifier = self.scoped_notifier()
        alert = dict(self.findings()[0], orchestrators=["orch"])
        with mock.patch("ameesh.canon.load_configured", side_effect=[[reporting_canon()], []]):
            notifier.run_pass(self.db, current=[alert])
        self.assertEqual(self.db.query("SELECT recipient FROM agent_mailbox"), [])
        self.assertEqual(notifier.state["briefs"], {})

    def test_recipient_lock_blocks_reassignment_until_mailbox_commit(self):
        notifier = self.scoped_notifier()
        other = db_mod.connect(self.cfg)
        alert = dict(self.findings()[0], orchestrators=["orch"])
        send = mail.send

        def concurrent_send(*args, **kwargs):
            with self.assertRaisesRegex(db_mod.DbError, "lock timeout"):
                with other.transaction() as contender:
                    contender.execute("SET LOCAL lock_timeout = '100ms'")
                    contender.execute("UPDATE agent_registry SET chantier = 'other' WHERE name = 'orch'")
            return send(*args, **kwargs)

        try:
            with mock.patch("ameesh.canon.load_configured", return_value=[reporting_canon()]), \
                    mock.patch.object(mail, "send", side_effect=concurrent_send):
                self.assertTrue(notifier._send_module_brief(self.db, alert, "orch", alert["detail"], alert))
            self.assertEqual(self.db.query("SELECT recipient FROM agent_mailbox"), [{"recipient": "orch"}])
            other.execute("UPDATE agent_registry SET chantier = 'other' WHERE name = 'orch'")
        finally:
            other.close()

    def test_event_thread_uses_finding_project_despite_canon_team(self):
        notifier = self.scoped_notifier()
        self.db.execute("UPDATE agent_registry SET team = 'other', canon_ref = 'agents/orch.md' WHERE name = 'orch'")
        alert = dict(self.findings()[0], orchestrators=["orch"])
        with mock.patch("ameesh.canon.load_configured", return_value=[reporting_canon()]), \
                mock.patch.object(fil, "record", wraps=fil.record) as record:
            notifier.run_pass(self.db, current=[alert])
        self.assertEqual(record.call_count, 1)
        self.assertEqual(record.call_args.kwargs["project"], "acme")
        self.assertTrue(Path(self.cfg.state_dir, "fils", "acme", "_projet.md").exists())
        self.assertFalse(Path(self.cfg.state_dir, "fils", "other", "_projet.md").exists())

    def test_foreign_assignee_human_never_overrides_lot_owner_or_default(self):
        registry.upsert(self.db, "dev-b", chantier="other", harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:owner-b' WHERE name = 'dev-b'")
        storage.of(self.db).packages.upsert({"id": "owner-a", "kind": "lot", "title": "A",
                                           "responsible": "human:owner-a", "canon_ref": "lots/a.md"})
        self.db.execute("UPDATE work_items SET assignee = 'dev-b', package_id = 'owner-a' WHERE id = %s", (self.lot,))
        alert = self.findings()[0]
        self.assertIsNone(alert["agent"])
        self.assertEqual(alert["assignee"], "dev-b")
        ncfg = notify.parse_config({"default_human": "human:default"})
        router = notify.Router(self.cfg, self.db, ncfg)
        # Also protect generic module findings which still supply an agent.
        self.assertEqual(router.resolve(dict(alert, agent="dev-b")), ("human:owner-a", "lot"))
        self.db.execute("UPDATE work_packages SET responsible = NULL WHERE id = 'owner-a'")
        self.assertEqual(router.resolve(dict(alert, agent="dev-b")), ("human:default", "defaut"))

    def test_two_host_scoped_findings_reach_their_own_responsible_human(self):
        """L101 T4 / #4552: a host-scoped module finding has no lot; it must
        reach the responsible human of its host or agent, not fall back to the
        default human. Two hosts with the same rule stay two routes."""
        root = Path(self.tmp) / "canon-two-hosts"
        hosts = root / "hotes"
        hosts.mkdir(parents=True, exist_ok=True)
        for host in ("h1", "h2"):
            (hosts / ("%s.md" % host)).write_text(
                "---\ntype: Host\ntitle: %s\nresponsible: human:resp-%s\n"
                "policy:\n  harnesses: [claude]\n  max_agents: 2\n---\n\n# %s\n"
                % (host, host, host), encoding="utf-8")
        for agent, host in (("a1", "h1"), ("a2", "h2")):
            registry.upsert(self.db, agent, harness="claude", host=host)
        cfg = dataclasses.replace(self.cfg, canon=str(root), canon_untrusted=True)
        ncfg = notify.parse_config({"default_human": "human:default"})

        def finding(host, agent):
            return {"type": "module_deviation", "module": "dev",
                    "rule": "host-work-dir-missing", "project": None, "lot": None,
                    "host": host, "agent": agent, "since": None,
                    "detail": "workspace finding", "action": "fix the host"}

        router = notify.Router(cfg, self.db, ncfg)
        self.assertEqual(router.resolve(finding("h1", "a1")), ("human:resp-h1", "hote"))
        self.assertEqual(router.resolve(finding("h2", "a2")), ("human:resp-h2", "hote"))
        # The agent's responsible human is consulted before its host's.
        self.db.execute("UPDATE agent_registry SET responsible = 'human:resp-a2' WHERE name = 'a2'")
        self.assertEqual(notify.Router(cfg, self.db, ncfg).resolve(finding("h2", "a2")),
                         ("human:resp-a2", "agent"))

    def test_persisted_foreign_human_delivery_is_reconciled_before_retry_or_resolution(self):
        storage.of(self.db).packages.upsert({"id": "owner-a", "kind": "lot", "title": "A",
                                           "responsible": "human:owner-a", "canon_ref": "lots/a.md"})
        self.db.execute("UPDATE work_items SET package_id = 'owner-a' WHERE id = %s", (self.lot,))
        alert = dict(self.findings()[0], agent="dev-b")
        key = notify.key_of(alert)
        sender = notify.DrySender()
        ncfg = notify.NotifyConfig(routes={"human:owner-a": ("a",), "human:owner-b": ("b",)},
                                  channels={name: notify.Channel(name, "desktop") for name in ("a", "b")})
        notifier = notify.Notifier(self.cfg, ncfg, sender=sender, log=lambda text: None, clock=lambda: 200.)
        old = {"alert": alert, "human": "human:owner-b", "source": "agent", "raised_ts": 100.,
               "status": "envoi", "channels": {"b": {"ok": False, "attempts": 1}}}
        notifier.state["active"][key] = old
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual([channel for channel, message in sender.sent], ["a"])
        self.assertEqual(notifier.state["active"][key]["human"], "human:owner-a")
        # An old delivery which resolves must not produce one last foreign copy.
        notifier.state["active"][key] = dict(old, human="human:owner-b", source="agent",
                                             channels={"b": {"ok": True, "attempts": 1}})
        sender.sent.clear()
        notifier.run_pass(self.db, current=[])
        self.assertEqual(sender.sent, [])

    def test_no_project_recipient_reports_to_owner_without_cross_project_events(self):
        for name in ("orch-b", "audit-b", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET chantier = 'other' WHERE name != 'auditeur'")
        listing = registry.overview(self.db)
        with mock.patch("ameesh.sous_utilisation.orchestrators", return_value=["orch-b"]):
            alerts = exploitation._module_deviations(self.cfg, self.db, listing)
        sender = notify.DrySender()
        ncfg = notify.parse_config({"default_human": "human:owner", "default": ["desktop"]})
        notifier = notify.Notifier(dataclasses.replace(self.cfg, canon="test-canon"), ncfg,
                                   sender=sender, log=lambda text: None, clock=lambda: 200.)
        # Even stale or injected orchestrator candidates must be scope checked at send time.
        alerts[0]["orchestrators"] = ["orch-b"]
        with mock.patch("ameesh.canon.load_configured", return_value=[auditor_canon("audit-b")]):
            notifier.run_pass(self.db, current=alerts)
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("IT procedure deviation", sender.sent[0][1].body)
        self.assertEqual(self.db.query("SELECT recipient, payload FROM agent_mailbox"), [])
        self.assertEqual(notifier.state["briefs"], {})
