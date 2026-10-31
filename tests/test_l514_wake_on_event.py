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
from types import SimpleNamespace
from unittest import mock

from ameesh import ci, cli, hostcheck, mail, registry, storage, work
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


class MidTurnTest(unittest.TestCase):
    def test_which_mail_does_not_wait(self):
        for payload, expected in (
                ({}, False), ({"ack": True}, False), ({"cc": "md"}, False),
                ({"verdict": True}, True), ({"arbitration": True}, True),
                ({"reassigned": {"lot": 1}}, True),
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

    def _hook(self, worker: AgentWorker, harness: str, event: str, *,
              turn: str | None = None):
        env = self.env(AGENT_MAIL_NAME=worker.name,
                       AMEESH_RUNNER_ID=worker.runner.runner_id,
                       AMEESH_LEASE_EPOCH=str(worker.epoch), AMEESH_HARNESS=harness)
        if turn is not None:
            # L77/B11 : le fichier de tour nomme un tour résident pour le hook.
            path = os.path.join(self.tmp, "resident-turn-%s" % worker.name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(turn)
            env["AMEESH_TURN_ID_FILE"] = path
        proc = self.cli("hook", harness, env=env, stdin=json.dumps(
            {"hook_event_name": event, "session_id": "s-1", "cwd": self.tmp}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _pending(self, name: str) -> list[str]:
        return [row["body"] for row in self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = %s AND delivered_at IS NULL "
            "ORDER BY id", (name,))]

    def _touched(self, name: str) -> int:
        """#885 : les non-lus qu'une réservation a touchés (posée puis rendue,
        ou soldée) — le témoin du churn : `consigne` ou `deja_consigne`."""
        return self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox"
            " WHERE recipient = %s AND delivered_at IS NULL"
            "   AND (meta ? 'consigne' OR meta ? 'deja_consigne')", (name,))[0]["n"]

    def _backlog(self, name: str, n: int) -> None:
        """#885 : `n` non-lus ordinaires d'un coup (sans passer par `mail.send`)."""
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, kind, payload)"
            " SELECT 'bulk', %s, 'Ordinary backlog ' || i, 'notify', '{}'::jsonb"
            "   FROM generate_series(1, %s) i", (name, n))


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

    def test_verdict_behind_two_hundred_unread_is_seen_mid_turn(self):
        """#885 : le verdict est derrière plus de `UNREAD_LIMIT` non-lus ; il
        est vu au point sûr, et le retard ordinaire n'est pas touché (aucune
        réservation, aucune libération : pas de churn)."""
        for harness in ("deepseek", "codex"):
            with self.subTest(harness=harness):
                name = "deep-" + harness
                worker = self._leased(name, harness)
                self._backlog(name, 250)
                mail.send(self.db, "codex-review", name, "Verdict beyond 200.",
                          payload={"verdict": True})
                context = json.loads(self._hook(worker, harness, "PostToolUse")
                                     )["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Verdict beyond 200.", context)
                self.assertNotIn("Ordinary backlog", context)
                self.assertEqual(len(self._pending(name)), 250)
                self.assertEqual(self._touched(name), 0)

    def test_ordinary_mail_does_not_churn_the_reservation(self):
        """#885 : deux points sûrs sans rien d'urgent n'écrivent RIEN — l'ancien
        chemin réservait jusqu'à 200 non-lus puis les relâchait (le churn)."""
        for harness in ("deepseek", "codex"):
            with self.subTest(harness=harness):
                name = "churn-" + harness
                worker = self._leased(name, harness)
                self._backlog(name, 3)
                self.assertEqual(self._hook(worker, harness, "PostToolUse"), "")
                self.assertEqual(self._hook(worker, harness, "PostToolUse"), "")
                self.assertEqual(self._touched(name), 0)
                self.assertEqual(len(self._pending(name)), 3)

    def test_active_reservations_do_not_hide_the_next_eligible_message(self):
        worker = self._leased("reserved-codex", "codex")
        ids = [int(row["id"]) for row in self.db.query(
            "INSERT INTO agent_mailbox (sender, recipient, body, payload)"
            " SELECT 'review', %s, 'Reserved verdict ' || i, '{\"verdict\":true}'::jsonb"
            " FROM generate_series(1, 200) i RETURNING id", (worker.name,))]
        mail.reserve(self.db, worker.name, worker.runner.runner_id, worker.epoch,
                     "other-token", ids=ids, porteur="consigne", ttl_seconds=3600)
        last = mail.send(self.db, "review", worker.name, "Verdict after reservations",
                         payload={"verdict": True})
        self.assertEqual([int(m["id"]) for m in mail.unread_mid_turn(
            self.db, worker.name, limit=1)], [last])
        out = self._hook(worker, "codex", "PostToolUse")
        self.assertIn("Verdict after reservations", out)
        self.assertNotIn("Reserved verdict", out)
        self.assertEqual(len(self._pending(worker.name)), 200)
        # A reservation from an old lease or an expired one is reclaimable.
        self.db.execute("UPDATE agent_mailbox SET meta = jsonb_set(meta,"
                        " '{consigne,epoch}', to_jsonb(%s::bigint)) WHERE id = %s",
                        (worker.epoch - 1, ids[0]))
        self.db.execute("UPDATE agent_mailbox SET meta = jsonb_set(meta,"
                        " '{consigne,expire}', '0'::jsonb) WHERE id = %s", (ids[1],))
        self.assertEqual([int(m["id"]) for m in mail.unread_mid_turn(
            self.db, worker.name, limit=2)], ids[:2])

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

    def test_resident_turn_leaves_human_mail_to_the_runner(self):
        """B11 : le hook d'un tour résident ne remet pas les humains ; ils
        restent non lus pour la file du tour (rang 1/2, au moins une fois)."""
        for harness in ("deepseek", "codex"):
            with self.subTest(harness=harness):
                name = "resident-" + harness
                worker = self._leased(name, harness)
                mail.send(self.db, "human:smichea", name, "Owner: ordinary.",
                          payload={"human": True})
                mail.send(self.db, "human:smichea", name, "Owner: urgent.",
                          payload={"human": True, "urgent": True})
                # Ancien message humain : sender human:* sans payload.human
                # (mail.is_human le reconnaît aussi) ; il ne doit pas passer.
                mail.send(self.db, "human:smichea", name, "Owner: legacy urgent.",
                          kind="event", payload={"urgent": True})
                mail.send(self.db, "review", name, "Verdict for the turn.",
                          payload={"verdict": True})
                out = self._hook(worker, harness, "PostToolUse",
                                 turn="turn-resident-1")
                context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Verdict for the turn.", context)
                self.assertNotIn("Owner: ordinary.", context)
                self.assertNotIn("Owner: urgent.", context)
                self.assertNotIn("Owner: legacy urgent.", context)
                pending = self._pending(name)
                self.assertIn("Owner: ordinary.", pending)
                self.assertIn("Owner: urgent.", pending)
                self.assertIn("Owner: legacy urgent.", pending)
                self.assertIn("Owner: legacy urgent.", pending)
                self.assertNotIn("Verdict for the turn.", pending)
                # Stop ne les remet pas non plus : la file du tour les soldera.
                self.assertEqual(self._hook(worker, harness, "Stop",
                                            turn="turn-resident-1"), "")
                pending = self._pending(name)
                self.assertIn("Owner: ordinary.", pending)
                self.assertIn("Owner: urgent.", pending)
                self.assertIn("Owner: legacy urgent.", pending)


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

    def test_reassigned_is_a_payload_key_not_a_cli_option(self):
        # #5025 N6 : `reassigned` est une clé de charge utile ; le CLI ne la
        # connaît pas comme option et n'avale donc plus un corps qui la cite.
        self.register("rx", "deepseek")
        proc = self.cli("send", "rx", "--reassigned", "to the queue",
                        env=self.env(AGENT_MAIL_NAME="ameesh-dev3"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown option --reassigned", proc.stderr)
        # un corps littéral commence par `--` : la marque `--` le protège.
        proc = self.cli("send", "rx", "--", "--reassigned", "to the queue",
                        env=self.env(AGENT_MAIL_NAME="ameesh-dev3"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self.db.query(
            "SELECT body, payload FROM agent_mailbox WHERE recipient = 'rx' "
            "ORDER BY id DESC LIMIT 1")[0]
        self.assertIn("--reassigned", row["body"])
        self.assertFalse(mail.message_payload(row).get("reassigned"))


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

    def _home(self, *, binary=False, rel="agent-mail") -> str:
        home = tempfile.mkdtemp(prefix="l514-hooks-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        if binary:
            path = os.path.join(home, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
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
               manager_path: str = "", environ: dict | None = None) -> dict:
        """Les lignes du doctor ; `manager_path` remplace le PATH du
        gestionnaire systemd (vide : celui de la session du doctor, le
        comportement d'avant #514 N2) ; `environ` s'ajoute à l'environnement
        du harnais (HOME, pour les chemins `$HOME` des hooks)."""
        variable = {"deepseek": "DSH_HOME", "codex": "CODEX_HOME",
                    "claude": "CLAUDE_CONFIG_DIR"}[harness]
        env = {variable: home, **(environ or {})}
        with mock.patch.object(hostcheck.adapters, "systemd_manager_path",
                               return_value=manager_path):
            return hostcheck.hook_lines(None, [harness], environ=env)

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
        self.assertIn("exits 127", texte)

    def test_hook_binaire_sous_home_variable_est_trouve(self):
        """Incident staging b9a2 (16/10) : un hook écrit `$HOME/.local/bin/
        agent-mail` ; `expanduser` seul ne développe pas la variable, le
        binaire passait pour absent (faux KO). Le chemin est résolu sans
        exécuter de shell."""
        home = self._home(binary=True, rel=os.path.join(".local", "bin", "agent-mail"))
        for command in ("$HOME/.local/bin/agent-mail hook deepseek",
                        "${HOME}/.local/bin/agent-mail hook deepseek"):
            with self.subTest(command=command):
                self._write(home, self._document({"PostToolUse": command,
                                                  "Stop": command}))
                out = self._lines(home, environ={"HOME": home})
                self.assertTrue(out["ok"], out["lines"])
                # Le chemin du fichier de hooks (suffixe temporaire aléatoire)
                # peut lui-même contenir « 127 » : le témoin porte sur le
                # message du doctor, pas sur un nombre libre dans la sortie
                # (#2891, CI paquet 39490886347).
                self.assertNotIn("exits 127", "\n".join(out["lines"]))

    def test_hook_binaire_sous_home_variable_absent_reste_ko(self):
        """La variable développée vers un chemin absent reste un KO : la
        résolution ne masque jamais un vrai binaire manquant."""
        home = self._home(binary=True, rel=os.path.join(".local", "bin", "agent-mail"))
        self._write(home, self._document(
            {"PostToolUse": "$HOME/.local/bin/missing-agent-mail hook deepseek"}))
        out = self._lines(home, environ={"HOME": home})
        self.assertFalse(out["ok"])
        self.assertIn("exits 127", "\n".join(out["lines"]))

    def test_dollar_litteral_absent_ne_devient_pas_un_hook_present(self):
        home = self._home(binary=True, rel=".local/bin/agent-mail")
        for word in ("'$HOME/.local/bin/agent-mail'",
                     r"\$HOME/.local/bin/agent-mail",
                     r'"\$HOME/.local/bin/agent-mail"',
                     "'~/.local/bin/agent-mail'",
                     '"~/.local/bin/agent-mail"'):
            with self.subTest(word=word):
                self._write(home, self._document({"PostToolUse": word + " hook deepseek"}))
                out = self._lines(home, environ={"HOME": home})
                self.assertFalse(out["ok"], out["lines"])
                self.assertIn("exits 127", "\n".join(out["lines"]))

    def test_variable_dont_le_nom_commence_par_un_chiffre(self):
        """#1981/N2 : `$1abc` vaut `$1` puis `abc` (le shell), pas la variable
        `1abc` ; `$1` seul reste développé et un nom normal aussi ; `${1abc}`
        reste littéral (substitution invalide)."""
        self.assertEqual(hostcheck._hook_path("$1abc", {"1abc": "/mauvais"}),
                         "$1abc")
        self.assertEqual(hostcheck._hook_path("${1abc}", {"1abc": "/mauvais"}),
                         "${1abc}")
        self.assertEqual(hostcheck._hook_path("$1", {"1": "/bon"}), "/bon")
        self.assertEqual(hostcheck._hook_path("$a1", {"a1": "/bon"}), "/bon")

    def test_home_entre_guillemets_doubles_reste_developpe(self):
        home = self._home(binary=True, rel=".local/bin/agent-mail")
        for word in ('"$HOME/.local/bin/agent-mail"',
                     '"${HOME}"/.local/bin/agent-mail'):
            with self.subTest(word=word):
                self._write(home, self._document({"PostToolUse": word + " hook deepseek"}))
                self.assertTrue(self._lines(home, environ={"HOME": home})["ok"])

    def test_chemin_existant_avec_dollar_litteral_reste_trouve(self):
        home = self._home(binary=True, rel="$HOME/agent-mail")
        path = os.path.join(home, "$HOME", "agent-mail")
        for word in ("'%s'" % path, path.replace("$", r"\$")):
            with self.subTest(word=word):
                self._write(home, self._document({"PostToolUse": word + " hook deepseek"}))
                self.assertTrue(self._lines(home, environ={"HOME": home})["ok"])

    def test_hook_variable_non_definie_reste_litterale_et_ko(self):
        """Une variable non définie n'est pas remplacée en silence : le chemin
        littéral `$VAR/...` reste contrôlé et signalé absent."""
        home = self._home()
        self._write(home, self._document(
            {"PostToolUse": "$AMEESH_L514_ABSENT/agent-mail hook deepseek"}))
        out = self._lines(home, environ={"HOME": home})
        self.assertFalse(out["ok"])
        self.assertIn("AMEESH_L514_ABSENT", "\n".join(out["lines"]))

    def test_binary_missing_developpe_les_variables_du_hook(self):
        """Le contrôle unitaire résout `$HOME` et `${HOME}` avec
        l'environnement reçu ; absent ou non définie reste manquant."""
        home = self._home(binary=True, rel=os.path.join(".local", "bin", "agent-mail"))
        environ = {"HOME": home}
        self.assertFalse(hostcheck._binary_missing(
            "$HOME/.local/bin/agent-mail", environ=environ))
        self.assertFalse(hostcheck._binary_missing(
            "${HOME}/.local/bin/agent-mail", environ=environ))
        self.assertTrue(hostcheck._binary_missing(
            "$HOME/.local/bin/absent-agent-mail", environ=environ))
        self.assertTrue(hostcheck._binary_missing(
            "$AMEESH_L514_ABSENT/agent-mail", environ=environ))

    def test_chemins_litteraux_existants_restent_ok(self):
        """Un chemin littéral existant n'est pas touché par la résolution."""
        home = self._home(binary=True)
        command = os.path.join(home, "agent-mail") + " hook deepseek"
        self._write(home, self._document({"PostToolUse": command}))
        out = self._lines(home)
        self.assertTrue(out["ok"], out["lines"])

    def test_binary_missing_sous_le_path_du_gestionnaire_est_ko(self):
        """#514 N2 : le gestionnaire systemd peut lancer les harnais avec SON
        PATH ; un binaire absent là sort en 127, même s'il est visible ici."""
        home = self._home(binary=True)
        self._write(home, self._document({"PostToolUse": "agent-mail hook deepseek",
                                          "Stop": "agent-mail hook deepseek"}))
        with mock.patch.dict(os.environ, {"PATH": home}):
            out = self._lines(home, manager_path="/nowhere-l514")
        self.assertFalse(out["ok"])
        texte = "\n".join(out["lines"])
        self.assertIn("systemd manager PATH", texte)
        self.assertIn("exits 127", texte)

    def test_binary_visible_seulement_sous_le_gestionnaire_est_ko(self):
        """#1395 (avis bloquant cx2) : la présence sous le gestionnaire ne
        prouve pas que le hook tournera — le PATH réel du service est celui de
        son unité ; absent de la session, le binaire reste un défaut."""
        home = self._home(binary=True)
        self._write(home, self._document({"PostToolUse": "agent-mail hook deepseek",
                                          "Stop": "agent-mail hook deepseek"}))
        with mock.patch.dict(os.environ, {"PATH": "/nowhere-l514"}):
            out = self._lines(home, manager_path=home)
        self.assertFalse(out["ok"], out["lines"])
        texte = "\n".join(out["lines"])
        self.assertIn("this session's PATH", texte)
        self.assertIn("exits 127", texte)

    def test_binary_visible_dans_les_deux_contextes_est_ok(self):
        home = self._home(binary=True)
        self._write(home, self._document({"PostToolUse": "agent-mail hook deepseek",
                                          "Stop": "agent-mail hook deepseek"}))
        with mock.patch.dict(os.environ, {"PATH": home}):
            out = self._lines(home, manager_path=home)
        self.assertTrue(out["ok"], out["lines"])
        self.assertIn("2 event(s)", "\n".join(out["lines"]))

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


class SenderVeilleTest(unittest.TestCase):
    """Incident staging b9a2 : le registre portait une ligne `veille` —
    l'identité d'ENVOI de la consigne de veille (`watch.SENDER`), mode
    `execute`, jamais de session ni de tour — que le doctor servait comme un
    agent, d'où un harnais `other` inconnu en KO. La ligne est écartée
    nommément ; un vrai agent à harnais inconnu garde son KO."""

    @staticmethod
    def _row(name: str, harness: str) -> dict:
        return {"name": name, "harness": harness, "host": "vm", "mode": "execute",
                "status": "idle"}

    def test_le_sender_de_veille_n_est_pas_un_agent_servi(self):
        rows = [self._row("veille", "other"), self._row("impl-x", "deepseek")]
        with mock.patch.object(hostcheck.registry, "overview", return_value=rows):
            served = hostcheck.served_agents(SimpleNamespace(host="vm"), None)
        self.assertEqual([row["name"] for row in served], ["impl-x"])

    def test_un_harnais_inconnu_reel_reste_ko(self):
        rows = [self._row("veille", "other"), self._row("impl-inconnu", "other")]
        with mock.patch.object(hostcheck.registry, "overview", return_value=rows):
            served = hostcheck.served_agents(SimpleNamespace(host="vm"), None)
            items = hostcheck.harness_report(served)
        self.assertEqual([item["harness"] for item in items], ["other"])
        self.assertEqual(items[0]["agents"], ["impl-inconnu"])
        self.assertFalse(items[0]["ok"])


if __name__ == "__main__":
    unittest.main()
