# SPDX-License-Identifier: AGPL-3.0-only
"""#8059 : la veille GitHub tourne au repos sur BG, sans agent ni bail.

Témoin demandé par le lot : toutes les VM d'agents sont arrêtées (aucun bail
vivant, aucun `role run`), un ticket simulé est lu par la minuterie système de
BG, et le courrier actif du veilleur désigné est déposé. Le même courrier
compte dans `unread` et suffit à `host_release.return_targets` /
`vm_adapter.wake_due` pour demander le retour de l'hôte drainé du veilleur.

Le collecteur de repos s'efface pendant qu'un rôle élu tient le bail : sous
activité, c'est la minuterie du rôle qui collecte, et deux passages
concurrents ne déposent qu'un seul événement (verrou de curseur de `deliver`).
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from ameesh import db as db_mod
from ameesh import github_watch, host_release, mail, registry, role_run, storage, vm_adapter
from .support import PgTestCase
from .test_2845_github_watch import NOW, SUB, Api, issue

ROOT = pathlib.Path(__file__).resolve().parent.parent
BG = "bureau-goal"
OTHER_HOST = "ameesh-agents-2"


def section(path, start, end):
    """One documentation section, so unrelated user units are not read here."""
    text = (ROOT / path).read_text()
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


class StandbyUnitTest(unittest.TestCase):
    """The BG timer is a system unit, installed like the VM adapter worker."""

    def test_the_unit_runs_the_notify_pass_without_a_role_or_a_lease(self):
        service = (ROOT / "deploy/systemd/ameesh-github-watch.service").read_text()
        timer = (ROOT / "deploy/systemd/ameesh-github-watch.timer").read_text()
        exec_lines = [line for line in service.splitlines()
                      if line.startswith("ExecStart=")]
        self.assertEqual(len(exec_lines), 1)
        self.assertEqual(exec_lines[0],
                         "ExecStart=/opt/ameesh-vm-adapter/venv/bin/python -m ameesh "
                         "notify --once --only github-watch")
        self.assertNotIn("role run", exec_lines[0])
        self.assertNotIn("AMEESH_ROLE", service)
        # #230272: same pattern as deploy/vm-adapter (a SYSTEM unit, installed
        # by the bureau): the worker's operator account and database.env, the
        # worker's venv. No user unit, no linger, no new account.
        self.assertIn("User=REPLACE_WITH_EXISTING_OPERATOR_ACCOUNT", service)
        self.assertIn("EnvironmentFile=/etc/ameesh-vm-adapter/database.env", service)
        self.assertNotIn("loginctl", service)
        self.assertNotIn("systemctl --user", service)
        self.assertNotIn("%h", service)
        self.assertIn("Unit=ameesh-github-watch.service", timer)
        self.assertIn("OnUnitActiveSec=300s", timer)
        self.assertIn("WantedBy=timers.target", timer)
        # A timer failure is visible; the pass keeps its cursor on error.
        self.assertNotIn("Restart=", service)

    def test_the_token_is_read_by_systemd_before_dropping_privileges(self):
        service = (ROOT / "deploy/systemd/ameesh-github-watch.service").read_text()
        # notify reads GH_TOKEN/GITHUB_TOKEN, not $CREDENTIALS_DIRECTORY: the
        # root:root 0600 EnvironmentFile is the chosen form, not LoadCredential.
        self.assertIn("EnvironmentFile=/etc/ameesh-github-watch/notify.env", service)
        self.assertNotIn("\nLoadCredential=", service)
        self.assertNotIn("\nGH_TOKEN=", service)
        for path, text in (
                ("deploy/vm-adapter/README.md",
                 section("deploy/vm-adapter/README.md",
                         "### Veille GitHub au repos", "## Commandes par VM")),
                ("docs/EXPLOITATION.md",
                 section("docs/EXPLOITATION.md",
                         "### Veille GitHub au repos, sur BG",
                         "### Coupeur de release"))):
            self.assertIn("root:root 0600", text, path)
            self.assertIn("avant de passer au compte du service", text, path)
            self.assertIn("systemctl enable --now ameesh-github-watch.timer",
                          text, path)
            self.assertIn("systemctl disable --now ameesh-github-watch.timer",
                          text, path)
            # The installer is a root procedure like the worker's.
            self.assertNotIn("systemctl --user", text, path)
            self.assertNotIn("enable-linger", text, path)

    def test_bg_is_documented_and_the_hub_installer_is_untouched(self):
        text = (ROOT / "docs/EXPLOITATION.md").read_text()
        self.assertIn("Veille GitHub au repos, sur BG", text)
        self.assertIn("github.watch_role", text)
        self.assertIn("bureau-goal", text)
        # #8059's first cut installed the collector on the saturated hub; the
        # pilot moved it to BG, next to the VM adapter worker.
        for script in ("deploy/serveur/installer-serveur.sh",
                       "deploy/mise-a-jour/vm.sh"):
            self.assertNotIn("ameesh-github-watch",
                             (ROOT / script).read_text(), script)


class AtRestWitnessTest(PgTestCase):
    """A ticket read by BG while every agent VM is stopped."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE github_watch_cursors, github_watch_events,"
                        " github_watch_scans, github_watch_pending")
        registry.upsert(self.db, "watcher", harness="deepseek", chantier="app",
                        host=OTHER_HOST)
        self.db.execute("UPDATE agent_registry SET admitted_hosts = ARRAY[%s],"
                        " releve = 'hote', mode = 'execute', status = 'idle'"
                        " WHERE name = 'watcher'", (OTHER_HOST,))
        # BG names itself as the standby observer: this is the whole
        # authorization, no role lease and no AMEESH_ROLE.
        self.cfg = dataclasses.replace(self.cfg, host=BG,
                                       github={"host": BG})

    def _no_role(self):
        return mock.patch.dict(os.environ, {"AMEESH_ROLE": "",
                                            "AMEESH_ROLE_HOST": "",
                                            "AMEESH_ROLE_PASS": ""}, clear=False)

    def _mesh_design_lease(self, seconds):
        registry.upsert(self.db, "mesh-design", harness="claude", chantier="app",
                        host="ameesh-agents-1")
        self.db.execute(
            "UPDATE agent_registry SET releve = 'election', status = 'idle',"
            " lease_owner = %s, lease_epoch = 481,"
            " lease_expires_at = clock_timestamp() + make_interval(secs => %s)"
            " WHERE name = 'mesh-design'",
            ("runner-mesh-design@ameesh-agents-1", seconds))

    def test_a_simulated_ticket_produces_the_wake_mail_without_a_lease(self):
        api = Api([issue()])
        with self._no_role():
            # The designated host alone authorizes the pass: no role lease.
            self.assertTrue(role_run.host_authorized(self.cfg, BG))
            report = github_watch.Watcher(
                self.cfg, api=api, declarations=[SUB]).tick(self.db, NOW)
        self.assertEqual(report, [dict(project="app", repo="acme/app", events=1)])
        messages = mail.unread(self.db, "watcher")
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message["kind"], "event")
        self.assertEqual(mail.passive_reason(message), "")
        self.assertIn("acme/app#1", message["body"])
        self.assertIn("https://github.com/acme/app/issues/1", message["body"])
        self.assertEqual(message["payload"]["github_watch"]["count"], 1)
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                         NOW - github_watch.INDEX_LAG)

        # Every agent host is drained: the active mail is enough for the
        # adapter to request the watcher's host return (no role election here).
        listing = storage.of(self.db).operations.listing()
        controls = {host: {"host": host, "mode": "draining", "claims_open": False}
                    for host in (OTHER_HOST, "ameesh-agents-1")}
        control = controls[OTHER_HOST]
        self.assertEqual(host_release.return_targets(
            control, listing, controls,
            known_hosts={OTHER_HOST, "ameesh-agents-1"}, holds={}), ["watcher"])
        self.assertTrue(vm_adapter.wake_due(
            OTHER_HOST, control, listing, [], controls, {}, now=NOW,
            horizon=60, target={}))

    def test_a_live_role_lease_keeps_the_standby_collector_silent(self):
        self._mesh_design_lease(3600)
        with self._no_role():
            report = github_watch.Watcher(
                self.cfg, api=Api([issue()]), declarations=[SUB]).tick(self.db, NOW)
        self.assertEqual(report, [])
        self.assertEqual(mail.unread(self.db, "watcher"), [])
        # The role holder is awake again: at the next tick BG takes over.
        self.db.execute("UPDATE agent_registry SET lease_expires_at ="
                        " clock_timestamp() - interval '1 second'"
                        " WHERE name = 'mesh-design'")
        with self._no_role():
            report = github_watch.Watcher(
                self.cfg, api=Api([issue()]), declarations=[SUB]).tick(self.db, NOW)
        self.assertEqual(report, [dict(project="app", repo="acme/app", events=1)])
        self.assertEqual(len(mail.unread(self.db, "watcher")), 1)

    def test_a_dry_run_reads_github_even_under_a_live_lease(self):
        self._mesh_design_lease(3600)
        api = Api([issue()])
        with self._no_role():
            report = github_watch.Watcher(
                self.cfg, api=api, declarations=[SUB], dry_run=True).tick(self.db, NOW)
        self.assertEqual(report, [dict(project="app", repo="acme/app", events=1)])
        self.assertEqual(mail.unread(self.db, "watcher"), [])

    def test_a_pass_without_the_designated_host_or_a_role_stays_silent(self):
        # The timer is installed everywhere but inert until `github.host`
        # names this host: the historical gate is not widened for BG.
        cfg = dataclasses.replace(self.cfg, github={"host": OTHER_HOST})
        with self._no_role():
            self.assertFalse(role_run.host_authorized(cfg, OTHER_HOST))
            report = github_watch.Watcher(
                cfg, api=Api([issue()]), declarations=[SUB]).tick(self.db, NOW)
        self.assertEqual(report, [])
        self.assertEqual(mail.unread(self.db, "watcher"), [])

    def test_two_concurrent_standby_passes_deliver_one_event(self):
        # A handover can have two collectors read the same GitHub window; the
        # cursor lock and the committed-window compare keep one event.
        events = github_watch.collect(Api([issue()]), SUB, NOW)
        ready = threading.Barrier(2, timeout=10)

        def run():
            db = db_mod.connect(self.cfg)
            try:
                ready.wait()
                return github_watch.deliver(db, SUB, events, NOW, [], BG)
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            self.assertEqual(sorted(f.result(timeout=20) for f in futures), [0, 1])
        self.assertEqual(len(mail.unread(self.db, "watcher")), 1)


if __name__ == "__main__":
    unittest.main()
