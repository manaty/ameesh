# SPDX-License-Identifier: AGPL-3.0-only
"""L514: during a dsh or codex turn the runner drives, the PostToolUse hook
(the safe point between two tool calls) delivers only verdicts,
arbitrations, human and urgent mail; ordinary mail waits for the end of the
turn (L125). Claude keeps getting all of it. A finished CI run wakes the
idle agent by itself (no polling), and `ameesh doctor --harness` reports a
hook whose command does not exist. Real Postgres.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import ci, cli, hostcheck, mail, registry, storage, work
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


class MidTurnTest(unittest.TestCase):
    def test_which_mail_does_not_wait(self):
        for payload, expected in (
                ({}, False), ({"ack": True}, False), ({"cc": "md"}, False),
                ({"verdict": True}, True), ({"arbitration": True}, True),
                ({"human": True, "decision_answer": {"decision": 1}}, True),
                ({"urgent": True}, True),
                # a finished CI run does not interrupt the turn: it wakes the
                # agent at the end of it (or the idle agent, by NOTIFY)
                ({"ci": {"kind": "verdict", "status": "success"}}, False)):
            with self.subTest(payload=payload):
                self.assertIs(mail.mid_turn({"payload": payload}), expected)


class _Base(PgTestCase):
    def _leased(self, name: str, harness: str) -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, harness, cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _hook(self, worker: AgentWorker, harness: str, event: str):
        env = self.env(AGENT_MAIL_NAME=worker.name,
                       AMEESH_RUNNER_ID=worker.runner.runner_id,
                       AMEESH_LEASE_EPOCH=str(worker.epoch), AMEESH_HARNESS=harness)
        proc = self.cli("hook", harness, env=env, stdin=json.dumps(
            {"hook_event_name": event, "session_id": "s-1", "cwd": self.tmp}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _pending(self, name: str) -> list[str]:
        return [row["body"] for row in self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = %s AND delivered_at IS NULL "
            "ORDER BY id", (name,))]


class MidTurnHookTest(_Base):
    def _mail(self, name: str) -> None:
        mail.send(self.db, "mty-amh-impl-02", name, "Ordinary progress note.")
        mail.send(self.db, "codex-review", name, "Verdict r2: two findings.",
                  payload={"verdict": True})
        mail.send(self.db, "mesh-design", name, "Arbitration: keep option b.",
                  payload={"arbitration": True})
        mail.send(self.db, "human:smichea", name, "Owner: answer before going on.",
                  payload={"human": True})

    def test_dsh_turn_gets_the_verdict_between_two_tool_calls(self):
        for harness in ("deepseek", "codex"):
            with self.subTest(harness=harness):
                name = "ds-" + harness
                worker = self._leased(name, harness)
                self._mail(name)
                out = self._hook(worker, harness, "PostToolUse")
                context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Verdict r2: two findings.", context)
                self.assertIn("Arbitration: keep option b.", context)
                self.assertIn("Owner: answer before going on.", context)
                self.assertNotIn("Ordinary progress note.", context)
                # released, not delivered: it waits for the end of the turn
                self.assertEqual(self._pending(name), ["Ordinary progress note."])
                self.assertEqual(self._hook(worker, harness, "PostToolUse"), "")
                stop = json.loads(self._hook(worker, harness, "Stop"))
                self.assertEqual(stop["decision"], "block")
                self.assertIn("Ordinary progress note.", stop["reason"])
                self.assertEqual(self._pending(name), [])

    def test_owner_mail_alone_interrupts_a_dsh_turn(self):
        for harness in ("deepseek", "codex"):
            with self.subTest(harness=harness):
                name = "own-" + harness
                worker = self._leased(name, harness)
                mail.send(self.db, "human:smichea", name, "Owner: decide now.",
                          payload={"human": True})
                context = json.loads(self._hook(worker, harness, "PostToolUse")
                                     )["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Owner: decide now.", context)
                self.assertEqual(self._pending(name), [])

    def test_claude_turn_still_gets_everything(self):
        worker = self._leased("cl", "claude")
        self._mail("cl")
        context = json.loads(self._hook(worker, "claude", "PostToolUse")
                             )["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Ordinary progress note.", context)
        self.assertEqual(self._pending("cl"), [])


class SendMarksTest(_Base):
    def test_verdict_and_arbitration_flags(self):
        self.register("rx", "deepseek")
        for flag in ("--verdict", "--arbitration"):
            with self.subTest(flag=flag):
                proc = self.cli("send", "rx", "Result of the review.", flag,
                                env=self.env(AGENT_MAIL_NAME="codex-review"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                row = self.db.query(
                    "SELECT payload FROM agent_mailbox WHERE recipient = 'rx' "
                    "ORDER BY id DESC LIMIT 1")[0]
                self.assertTrue(mail.mid_turn(row))
                self.assertNotIn("ack", mail.message_payload(row))

    def test_ack_contradicts_a_verdict(self):
        self.register("rx", "deepseek")
        proc = self.cli("send", "rx", "thanks", "--ack", "--verdict",
                        env=self.env(AGENT_MAIL_NAME="codex-review"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("contradict", proc.stderr)


class _FauxApi:
    """L'API GitHub vue par le relevé : exécutions en cours, état d'une
    exécution, check runs d'une tête (mêmes formes que test_l161c)."""

    def __init__(self):
        self.live, self.done, self.checks = [], {}, {}

    def __call__(self, path):
        if "/actions/runs?" in path:
            return {"workflow_runs": list(self.live)}
        if "/actions/runs/" in path:
            return self.done.get(int(path.rsplit("/", 1)[1]), {"status": "in_progress"})
        if "/check-runs" in path:
            return {"check_runs": []}
        raise AssertionError(path)


def _run_view(run_id, sha, branch):
    return {"id": run_id, "head_sha": sha, "head_branch": branch, "event": "pull_request",
            "path": ".github/workflows/tests.yml",
            "html_url": "https://github.com/manaty/ameesh/actions/runs/%d" % run_id,
            "pull_requests": [{"base": {"ref": "main"}}],
            "head_commit": {"message": ""}}


class CiWakeTest(_Base):
    """Fait quand #514 : une fin de CI réveille l'agent, sans sondage."""

    def setUp(self):
        super().setUp()
        for table in ("ci_test_failures", "ci_runs"):
            self.db.execute("DELETE FROM %s" % table)

    def test_ci_end_wakes_the_idle_agent_without_polling(self):
        name = "ci-dsh"
        worker = self._leased(name, "deepseek")
        # la fenêtre de regroupement des événements ne fait que retarder le
        # réveil : ici, zéro, pour que le test porte sur le réveil lui-même
        worker.runner.event_coalesce = 0.0
        lot = work.add(self.db, title="L514 : un lot", branch="lot/514-un", assignee=name)
        cfg = dataclasses.replace(
            self.cfg, github={"projects": {"ameesh": "manaty/ameesh"}, "host": self.cfg.host})
        api = _FauxApi()
        watcher = ci.Watcher(cfg, api=api)
        api.live = [_run_view(301, "a" * 40, "lot/514-un")]
        self.assertEqual(watcher.tick(self.db), [])  # l'exécution est suivie, rien à dire
        row = storage.of(self.db).ci.run("manaty/ameesh", 301)
        self.assertEqual(row["work_item_id"], lot["id"])
        # l'exécution se conclut : le verdict part vers l'assigné du lot
        api.live, api.done[301] = [], {"status": "completed", "conclusion": "success"}
        out = watcher.tick(self.db)
        self.assertEqual([(o["action"], o["to"], o["status"]) for o in out],
                         [("verdict", name, "success")])
        (message,) = mail.unread(self.db, name)
        self.assertFalse(mail.passive_reason(message), "un verdict de CI réveille")
        self.assertFalse(mail.is_urgent(message))
        self.assertIn(": green.", message["body"])
        # l'agent au repos est réveillé par ce courrier, sans avoir sondé
        spec = worker.pick()
        self.assertIsNotNone(spec, "la fin de CI doit ouvrir un tour")
        self.assertEqual(spec["ids"], [message["id"]])
        self.assertIn(": green.", spec["prompt"])


class DoctorHooksTest(unittest.TestCase):
    """L514 : `ameesh doctor --harness` voit un hook de courrier dont le
    binaire n'existe pas — l'incident du 2026-10-12 (`/usr/local/bin/agent-mail`
    absent, tous les hooks dsh en 127 : aucune remise, aucun Stop)."""

    def _home(self, *, binary=False) -> str:
        home = tempfile.mkdtemp(prefix="l514-hooks-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        if binary:
            path = os.path.join(home, "agent-mail")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            os.chmod(path, 0o755)
        return home

    @staticmethod
    def _write(home: str, document, filename: str = "hooks.json") -> None:
        with open(os.path.join(home, filename), "w", encoding="utf-8") as fh:
            fh.write(document if isinstance(document, str) else json.dumps(document))

    @staticmethod
    def _document(events: dict) -> dict:
        return {"hooks": {event: [{"hooks": [{"type": "command", "command": command}]}]
                          for event, command in events.items()}}

    def _lines(self, home: str, harness: str = "deepseek",
               manager_path: str = "") -> dict:
        """Les lignes du doctor ; `manager_path` remplace le PATH du
        gestionnaire systemd (vide : celui de la session du doctor, le
        comportement d'avant #514 N2)."""
        variable = {"deepseek": "DSH_HOME", "codex": "CODEX_HOME",
                    "claude": "CLAUDE_CONFIG_DIR"}[harness]
        with mock.patch.object(hostcheck.adapters, "systemd_manager_path",
                               return_value=manager_path):
            return hostcheck.hook_lines(None, [harness], environ={variable: home})

    def test_hook_commands_reads_the_nested_format(self):
        document = self._document({"PostToolUse": "bin/agent-mail hook deepseek"})
        self.assertEqual(hostcheck.hook_commands(document),
                         [("PostToolUse", "bin/agent-mail hook deepseek")])
        self.assertEqual(hostcheck.hook_commands({"hooks": "not an object"}), [])

    def test_missing_command_is_ko(self):
        home = self._home()
        # #514 R1 : jamais un chemin qui peut exister sur l'hôte — le binaire
        # est cherché sous un dossier temporaire neuf, donc toujours absent.
        missing = os.path.join(home, "missing-agent-mail")
        self._write(home, self._document(
            {"PostToolUse": "%s hook deepseek" % missing}))
        out = self._lines(home)
        self.assertFalse(out["ok"])
        texte = "\n".join(out["lines"])
        self.assertIn(missing, texte)
        self.assertIn("127", texte)

    def test_binary_missing_sous_le_path_du_gestionnaire_est_ko(self):
        """#514 N2 : le gestionnaire systemd lance les harnais avec SON PATH ;
        un binaire absent là sort en 127, même s'il est visible ici."""
        home = self._home(binary=True)
        self._write(home, self._document({"PostToolUse": "agent-mail hook deepseek",
                                          "Stop": "agent-mail hook deepseek"}))
        with mock.patch.dict(os.environ, {"PATH": home}):
            out = self._lines(home, manager_path="/nowhere-l514")
        self.assertFalse(out["ok"])
        texte = "\n".join(out["lines"])
        self.assertIn("systemd manager PATH", texte)
        self.assertIn("127", texte)

    def test_binary_visible_seulement_sous_le_gestionnaire_n_est_pas_ko(self):
        """L'inverse : sous le gestionnaire le hook tourne ; c'est la
        résolution de la session du doctor qui est trompeuse."""
        home = self._home(binary=True)
        self._write(home, self._document({"PostToolUse": "agent-mail hook deepseek",
                                          "Stop": "agent-mail hook deepseek"}))
        with mock.patch.dict(os.environ, {"PATH": "/nowhere-l514"}):
            out = self._lines(home, manager_path=home)
        self.assertTrue(out["ok"], out["lines"])
        self.assertIn("not in this session's PATH", "\n".join(out["lines"]))

    def test_binary_missing_resout_le_path_donne(self):
        """`_binary_missing` cherche un nom seul sous le PATH reçu ; vide ou
        absent, il retombe sur le PATH ambiant (comme avant #514 N2)."""
        home = self._home(binary=True)
        self.assertFalse(hostcheck._binary_missing("agent-mail", path=home))
        self.assertTrue(hostcheck._binary_missing("agent-mail", path="/nowhere-l514"))
        with mock.patch.dict(os.environ, {"PATH": home}):
            self.assertFalse(hostcheck._binary_missing("agent-mail"))
            self.assertFalse(hostcheck._binary_missing("agent-mail", path=""))

    def test_found_command_and_all_events(self):
        home = self._home(binary=True)
        command = os.path.join(home, "agent-mail") + " hook deepseek"
        self._write(home, self._document({event: command for event in (
            "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")}))
        out = self._lines(home)
        self.assertTrue(out["ok"], out["lines"])
        texte = "\n".join(out["lines"])
        self.assertIn("4 event(s)", texte)
        self.assertNotIn("warning", texte)

    def test_missing_stop_is_a_warning(self):
        home = self._home(binary=True)
        command = os.path.join(home, "agent-mail") + " hook deepseek"
        self._write(home, self._document({"PostToolUse": command}))
        out = self._lines(home)
        self.assertTrue(out["ok"], "un événement manquant n'empêche pas de travailler")
        texte = "\n".join(out["lines"])
        self.assertIn("Stop", texte)
        self.assertIn("no end-of-turn delivery", texte)

    def test_codex_reads_hooks_json_too(self):
        home = self._home(binary=True)
        command = os.path.join(home, "agent-mail") + " hook codex"
        self._write(home, self._document({"PostToolUse": command}))
        out = self._lines(home, "codex")
        self.assertTrue(out["ok"], out["lines"])
        self.assertIn("agent-mail", "\n".join(out["lines"]))

    def test_no_file_is_a_warning_not_a_ko(self):
        out = self._lines(self._home())
        self.assertTrue(out["ok"])
        self.assertIn("no hooks.json", "\n".join(out["lines"]))

    def test_unreadable_file_is_ko(self):
        home = self._home()
        self._write(home, "{ not json")
        out = self._lines(home)
        self.assertFalse(out["ok"])
        self.assertIn("unreadable", "\n".join(out["lines"]))


if __name__ == "__main__":
    unittest.main()
