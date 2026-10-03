#!/usr/bin/env python3
"""agent-runner — un exécuteur par machine : baux, tours, réveil sur NOTIFY.

  agent-runner [--host H] [--runner-id ID] [--agents a,b] [--once [--wait S]]
               [--dry-run] [--max-turns N] [--migrate]
        réclame les agents de cet hôte, renouvelle leurs baux, lance un tour
        quand un message arrive (LISTEN/NOTIFY) ou après inactivité.
  agent-runner register <nom> <claude|codex|deepseek> [--cwd DIR] [--prompt TEXTE]
               [--session ID] [--chantier C] [--model M] [--budget USD]
        inscrit un agent dans le registre (l'équivalent v1 de « nexlink-agent start »).
  agent-runner stop <nom>
        marque l'agent arrêté : plus de nouveau bail, le tour en cours se termine.

Garanties (R3, R5) :
* un agent n'a qu'un bail vivant : `claim` est un UPDATE conditionnel atomique ;
* le bail est renouvelé par un battement pendant les tours ; s'il est perdu, le
  processus du harnais est terminé et le tour n'est pas compté ;
* si l'exécuteur meurt (SIGKILL, coupure), le bail expire et un autre exécuteur
  de la même machine reprend l'agent ;
* le flux JSONL du harnais est journalisé dans
  ~/.local/state/agent-mesh/<nom>/events.jsonl, la session est mémorisée en base
  et sur disque : un tour peut être repris.

Aucun harnais réel n'est lancé par les tests : AGENT_MESH_BIN_DIR ou
AGENT_MESH_<HARNAIS>_BIN pointent vers de faux binaires.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import signal
import subprocess
import sys
import threading
import time

from . import adapters, db as db_mod, mail, registry
from .config import CHANNEL_LEASE, CHANNEL_MAIL, Config
from .config import load as load_config


def log(message: str) -> None:
    print("%s %s" % (time.strftime("%H:%M:%S"), message), flush=True)


class AgentWorker(threading.Thread):
    #: nombre d'échecs de battement consécutifs avant d'arrêter le harnais
    MAX_RENEW_FAILURES = 3
    """Un agent = un bail + une boucle de tours sur cette machine.

    C'est un thread : en mode service plusieurs agents tournent en parallèle,
    chacun avec son battement de bail.
    """

    def __init__(self, runner: "Runner", agent: dict, lease: dict):
        super().__init__(name=agent["name"], daemon=True)
        self.runner = runner
        self.db = runner.db
        self.cfg = runner.cfg
        self.name = agent["name"]
        self.epoch = int(lease["lease_epoch"])
        self.agent = agent
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.lease_lost = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.nudged = False
        self.last_activity = time.monotonic()
        self.state_dir = self.cfg.agent_dir(self.name)
        self.renew_failures = 0
        self.lease_deadline = float(lease.get("lease_expires_ts") or 0.0)

    # -- état local --------------------------------------------------------
    def _path(self, name: str) -> str:
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        return os.path.join(self.state_dir, name)

    def read_session_file(self) -> str | None:
        try:
            with open(self._path("session"), encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    def write_session_file(self, session_id: str) -> None:
        try:
            with open(self._path("session"), "w", encoding="utf-8") as fh:
                fh.write(session_id + "\n")
        except OSError:
            pass

    # -- bail --------------------------------------------------------------
    def renew(self) -> bool:
        """Prolonge le bail. Une panne de base ne laisse pas le harnais tourner
        sans bail : après quelques échecs ou à l'échéance connue, on l'arrête."""
        if self.lease_lost.is_set():
            return False
        try:
            expires = registry.renew(self.db, self.name, self.runner.runner_id, self.epoch,
                                     self.runner.lease_ttl)
        except db_mod.DbError as exc:
            self.renew_failures += 1
            log("[%s] battement : base injoignable (%s) — échec %d/%d"
                % (self.name, exc, self.renew_failures, self.MAX_RENEW_FAILURES))
            if self.renew_failures >= self.MAX_RENEW_FAILURES or time.time() >= self.lease_deadline:
                log("[%s] bail considéré perdu (base injoignable) : arrêt du harnais" % self.name)
                self.lease_lost.set()
                self.terminate()
                return False
            return True  # on laisse une chance au prochain battement
        if expires is None:
            log("[%s] bail perdu (epoch %d) : arrêt du tour" % (self.name, self.epoch))
            self.lease_lost.set()
            self.terminate()
            return False
        self.renew_failures = 0
        self.lease_deadline = float(expires)
        return True

    def release_lease(self) -> None:
        if self.lease_lost.is_set():
            return
        try:
            registry.release(self.db, self.name, self.runner.runner_id, self.epoch)
        except db_mod.DbError:
            pass

    #: délai laissé au harnais entre SIGTERM et SIGKILL
    TERMINATE_GRACE = 3.0

    def terminate(self) -> None:
        """Arrête le harnais : SIGTERM, puis SIGKILL s'il l'ignore (B5b).

        Un processus qui traite SIGTERM sort en quelques dizaines de
        millisecondes ; un `sleep` qui l'ignore ne doit pas continuer à tourner
        alors que le bail est perdu.
        """
        with self.lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            return
        deadline = time.monotonic() + self.TERMINATE_GRACE
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)
        log("[%s] le harnais ignore SIGTERM : SIGKILL" % self.name)
        try:
            proc.kill()
        except OSError:
            pass

    def wait_timeout(self, cap: float) -> float:
        """Attente bornée par l'échéance du bail : on ne dort pas au-delà (B5a)."""
        if self.lease_deadline > 0:
            return max(0.2, min(cap, self.lease_deadline - time.time() + 0.2))
        return cap

    def _heartbeat(self, stop_event: threading.Event) -> None:
        interval = max(3.0, self.runner.lease_ttl / 3.0)
        while not stop_event.wait(self.wait_timeout(interval)):
            if not self.renew():
                return

    # -- choix du travail --------------------------------------------------
    def idle_due(self) -> bool:
        return (not self.nudged) and (time.monotonic() - self.last_activity >= self.runner.idle_nudge)

    def peek(self) -> bool:
        agent = registry.get(self.db, self.name)
        if agent is None or agent.get("status") == "stopped":
            return False
        if agent.get("pending_prompt"):
            return True
        if mail.unread(self.db, self.name, limit=1):
            return True
        return self.idle_due()

    def pick(self) -> dict | None:
        """Le prochain tour à faire, ou None. Consomme la consigne en attente."""
        agent = registry.get(self.db, self.name)
        if agent is None or agent.get("status") == "stopped":
            if agent is not None:
                log("[%s] arrêté dans le registre" % self.name)
            self.stopping.set()
            return None
        self.agent = agent
        prompt = registry.take_pending_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
        if prompt:
            self.nudged = False
            return {"kind": "prompt", "prompt": prompt, "ids": []}
        messages = mail.unread(self.db, self.name)
        if messages:
            self.nudged = False
            return {
                "kind": "mail",
                "prompt": adapters.MAIL_PROMPT,
                "ids": [int(m["id"]) for m in messages if m.get("id") is not None],
            }
        if self.idle_due():
            return {"kind": "idle", "prompt": adapters.IDLE_PROMPT, "ids": []}
        return None

    # -- un tour -----------------------------------------------------------
    def _adapter(self) -> adapters.HarnessAdapter:
        harness = self.agent.get("harness") or "other"
        return adapters.adapter_for(harness)

    def run_turn(self, spec: dict) -> bool:
        harness = self.agent.get("harness") or "other"
        try:
            adapter = self._adapter()
        except adapters.HarnessMissing as exc:
            log("[%s] %s" % (self.name, exc))
            self.fail_turn("harnais absent", str(exc))
            return False

        session = self.agent.get("session_id") or self.read_session_file()
        argv = adapter.command(spec["prompt"], session)
        label = {"prompt": "consigne", "mail": "messages", "idle": "reprise"}[spec["kind"]]
        if self.runner.dry_run:
            log("[%s] tour %s (dry-run, session %s) : %s"
                % (self.name, label, session or "neuve", " ".join(argv)))
            return True
        if not registry.begin_turn(self.db, self.name, self.runner.runner_id, self.epoch,
                                   "tour %s (%s)" % (label, harness)):
            log("[%s] bail perdu avant le tour" % self.name)
            return False

        cwd = self.agent.get("cwd")
        if not cwd or not os.path.isdir(cwd):
            message = "dossier de travail absent : %r" % (cwd,)
            log("[%s] %s" % (self.name, message))
            self.fail_turn("dossier absent", message)
            return False

        env = os.environ.copy()
        env.update(adapter.env())
        env.update({
            "AGENT_MAIL_NAME": self.name,
            "AGENT_MESH_DSN": self.cfg.dsn,
            "AGENT_MESH_SCHEMA": self.cfg.schema,
            "AGENT_MESH_STATE": self.cfg.state_dir,
            "AGENT_MAIL_STATE": self.cfg.v0_state,
            "AGENT_MAIL_CONFIG": self.cfg.config_dir,
            "AGENT_MESH_HOST": self.cfg.host,
            "AGENT_MESH_RUNNER_ID": self.runner.runner_id,
        })
        events_path = self._path("events.jsonl")
        stderr_path = self._path("stderr.log")
        started = time.time()
        session_new: str | None = None
        cost: float | None = None
        error: str | None = None
        result_code = 0

        heartbeat_done = threading.Event()
        threading.Thread(target=self._heartbeat, args=(heartbeat_done,), daemon=True).start()
        try:
            with open(stderr_path, "ab") as stderr:
                proc = subprocess.Popen(
                    argv, cwd=cwd, stdout=subprocess.PIPE, stderr=stderr,
                    text=True, bufsize=1, env=env,
                )
            with self.lock:
                self.proc = proc
            log("[%s] tour %s : %s" % (self.name, label, " ".join(argv[:6]) + " …"))
            with open(events_path, "a", encoding="utf-8") as events:
                for line in proc.stdout or ():
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    events.write(line + "\n")
                    events.flush()
                    parsed = self._parse(adapter, line)
                    if parsed.get("session") and not session_new:
                        session_new = parsed["session"]
                        self.write_session_file(session_new)
                        registry.set_session(self.db, self.name, session_new)
                    for text in parsed.get("display") or []:
                        log("[%s] %s" % (self.name, text))
                    if parsed.get("cost") is not None:
                        cost = parsed["cost"]
                    if parsed.get("error"):
                        error = str(parsed["error"])
                    if self.lease_lost.is_set():
                        error = "bail perdu en cours de tour"
                        self.terminate()
                        break
            result_code = proc.wait()
        finally:
            heartbeat_done.set()
            with self.lock:
                running = self.proc
                self.proc = None
            if running is not None and running.poll() is None:
                running.terminate()

        duration = time.time() - started
        lost = self.lease_lost.is_set()
        ok = result_code == 0 and not lost and not error
        if error is None and result_code != 0:
            error = "le harnais a rendu le code %d" % result_code
        status = "idle" if ok else "blocked"
        if lost:
            # Le bail ne nous appartient plus : end_turn est fencé, il ne fera rien.
            registry.end_turn(self.db, self.name, self.runner.runner_id, self.epoch,
                              status="dead", status_text="bail perdu", error=error)
            return False
        if not ok:
            # B6a : le tour n'a pas abouti, la consigne repart en attente au lieu
            # d'être effacée par end_turn. Au pire elle est rejouée.
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
        registry.end_turn(
            self.db, self.name, self.runner.runner_id, self.epoch,
            status=status,
            status_text="%s en %ds%s" % (label, int(duration), "" if ok else " (échec)"),
            error=error, cost_usd=cost,
        )
        self.last_activity = time.monotonic()
        if spec["kind"] == "idle":
            self.nudged = True
        with self.runner.lock:
            self.runner.turns += 1
        if spec["kind"] == "mail" and ok and spec["ids"]:
            # Si le hook du harnais les a déjà remis, c'est un no-op.
            mail.mark_delivered(self.db, spec["ids"])
        return ok

    def fail_turn(self, status_text: str, error: str) -> None:
        """Un tour qui n'a pas pu démarrer : la consigne repart en attente (R5)."""
        registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
        registry.set_status(self.db, self.name, "blocked",
                            status_text=status_text, error=error)

    def _parse(self, adapter: adapters.HarnessAdapter, line: str) -> dict:
        try:
            return adapter.parse(line)
        except Exception as exc:  # un flux inattendu ne tue pas l'exécuteur
            return {"error": "flux illisible : %s" % exc}

    # -- boucles -----------------------------------------------------------
    def process_once(self, wait_seconds: float) -> bool:
        """Mode banc : au plus un tour, en attendant `wait_seconds` du travail."""
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            spec = self.pick()
            if spec:
                self.run_turn(spec)
                return True
            if self.stopping.is_set() or time.monotonic() >= deadline:
                return False
            self.wake.wait(timeout=min(0.25, max(0.02, deadline - time.monotonic())))
            self.wake.clear()

    def run(self) -> None:
        """Mode service : boucle jusqu'à l'arrêt, réveillée par NOTIFY."""
        try:
            while not self.stopping.is_set() and not self.runner.stop.is_set():
                if not self.renew():
                    break
                spec = self.pick()
                if spec is None:
                    self.wake.wait(timeout=self.wait_timeout(
                        min(self.runner.poll, max(1.0, self.runner.lease_ttl / 3.0))))
                    self.wake.clear()
                    continue
                if not self.run_turn(spec):
                    # Un message non remis ou un harnais en échec ne doit pas
                    # produire une boucle serrée : on laisse retomber.
                    self.wake.wait(timeout=self.runner.failure_backoff)
                    self.wake.clear()
        finally:
            self.release_lease()


class Runner:
    def __init__(
        self,
        cfg: Config,
        db: db_mod.Db,
        *,
        agents: list[str] | None = None,
        once: bool = False,
        wait: float = 0.0,
        dry_run: bool = False,
        max_turns: int | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.host = cfg.host
        self.runner_id = cfg.runner
        self.agents_filter = agents
        self.once = once
        self.wait = wait
        self.dry_run = dry_run
        self.max_turns = max_turns
        self.lease_ttl = cfg.lease_ttl
        self.idle_nudge = cfg.idle_nudge
        self.poll = max(1.0, cfg.poll)
        self.failure_backoff = 5.0
        self.stop = threading.Event()
        self.wake_all = threading.Event()
        self.workers: dict[str, AgentWorker] = {}
        self.lock = threading.Lock()
        self.listener = None          # écouteur LISTEN/NOTIFY courant
        self.listener_thread = None
        self.turns = 0
        self.did_turn = False

    # -- réclamation -------------------------------------------------------
    def sweep(self) -> None:
        for row in registry.reap(self.db, self.host):
            log("bail expiré : %s (propriétaire %s)" % (row["name"], row.get("lease_owner")))
        with self.lock:
            for name, worker in list(self.workers.items()):
                if not worker.is_alive():
                    log("worker %s terminé" % name)
                    del self.workers[name]
        for agent in registry.claimable(self.db, self.host, self.agents_filter):
            with self.lock:
                if not self.once and agent["name"] in self.workers:
                    continue
            lease = registry.claim(self.db, agent["name"], self.runner_id, self.lease_ttl)
            if not lease:
                continue  # un autre exécuteur a gagné la course
            log("bail acquis : %s (epoch %s, hôte %s)"
                % (agent["name"], lease["lease_epoch"], self.host))
            worker = AgentWorker(self, agent, lease)
            if self.once:
                ran = worker.process_once(self.wait)
                self.did_turn = self.did_turn or ran
                worker.release_lease()
            else:
                with self.lock:
                    self.workers[agent["name"]] = worker
                worker.start()

    # -- écoute ------------------------------------------------------------
    def dispatch(self, item: dict) -> None:
        if item.get("channel") == CHANNEL_MAIL:
            try:
                payload = json.loads(item.get("payload") or "{}")
            except ValueError:
                payload = {}
            target = payload.get("to")
            with self.lock:
                worker = self.workers.get(target) if target else None
            if worker:
                log("message pour %s : réveil du tour" % target)
                worker.wake.set()
            else:
                for worker in self.workers.values():
                    worker.wake.set()
        self.wake_all.set()

    def _listen_loop(self) -> None:
        while not self.stop.is_set():
            listener = db_mod.listener(self.db, [CHANNEL_MAIL, CHANNEL_LEASE])
            if listener is None:
                log("LISTEN/NOTIFY indisponible avec ce pilote : sondage toutes les %ss" % self.poll)
                return
            self.listener = listener
            log("LISTEN %s, %s" % (CHANNEL_MAIL, CHANNEL_LEASE))
            try:
                while not self.stop.is_set():
                    item = listener.wait(timeout=self.poll)
                    if item is None:
                        self.wake_all.set()  # battement de sondage
                        continue
                    if item.get("event") == "down":
                        log("écoute interrompue (%s) : reconnexion" % (item.get("error") or "?"))
                        break
                    self.dispatch(item)
            finally:
                listener.close()
                self.listener = None
            if not self.stop.is_set():
                time.sleep(1.0)

    # -- arrêt -------------------------------------------------------------
    def shutdown(self) -> None:
        self.stop.set()
        self.wake_all.set()
        if self.listener:
            self.listener.close()
        with self.lock:
            workers = list(self.workers.values())
        for worker in workers:
            worker.stopping.set()
            worker.wake.set()
        deadline = time.monotonic() + 8.0
        for worker in workers:
            worker.join(timeout=max(0.1, deadline - time.monotonic()))
            worker.release_lease()
        log("arrêt : %d bail(aux) rendu(s), %d tour(s)" % (len(workers), self.turns))

    def run(self) -> int:
        log("démarrage : hôte %s, exécuteur %s, pilote %s, schéma %s"
            % (self.host, self.runner_id, self.db.name, self.cfg.schema))
        if self.once:
            self.sweep()
            if not self.did_turn:
                log("aucun travail disponible")
            return 0 if self.did_turn else 3
        self.listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.listener_thread.start()
        try:
            while not self.stop.is_set():
                self.sweep()
                if self.max_turns and self.turns >= self.max_turns:
                    log("limite de %d tour(s) atteinte" % self.max_turns)
                    break
                self.wake_all.wait(timeout=self.poll)
                self.wake_all.clear()
        finally:
            self.shutdown()
        return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-runner", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=None, help="hôte dont cet exécuteur réclame les agents")
    parser.add_argument("--runner-id", default=None, help="identifiant du bail (défaut hôte:pid)")
    parser.add_argument("--agents", default=None, help="liste d'agents séparés par des virgules")
    parser.add_argument("--once", action="store_true",
                        help="un seul passage (code 0 si un tour a eu lieu, 3 sinon)")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="avec --once : attendre le travail jusqu'à N secondes")
    parser.add_argument("--dry-run", action="store_true", help="imprimer les commandes sans les lancer")
    parser.add_argument("--max-turns", type=int, default=None, help="s'arrêter après N tours")
    parser.add_argument("--lease-ttl", type=float, default=None,
                        help="durée du bail en secondes (défaut 300)")
    parser.add_argument("--poll", type=float, default=None,
                        help="intervalle du sondage de secours en secondes (défaut 5)")
    parser.add_argument("--idle-nudge", type=float, default=None,
                        help="relance après N secondes d'inactivité (défaut 1200)")
    parser.add_argument("--migrate", action="store_true", help="appliquer les migrations avant de démarrer")
    return parser


def cmd_register(cfg: Config, db: db_mod.Db, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent-runner register")
    parser.add_argument("name")
    parser.add_argument("harness", choices=["claude", "codex", "deepseek"])
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--session", default=None)
    parser.add_argument("--chantier", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--budget", type=float, default=None)
    parsed = parser.parse_args(args)
    cwd = os.path.abspath(os.path.expanduser(parsed.cwd)) if parsed.cwd else None
    row = registry.upsert(
        db, parsed.name, chantier=parsed.chantier, harness=parsed.harness, host=cfg.host,
        cwd=cwd, session_id=parsed.session, model=parsed.model, budget_usd=parsed.budget,
        status="queued" if parsed.prompt else None,
    )
    if parsed.prompt:
        registry.set_pending_prompt(db, parsed.name, parsed.prompt)
    log("agent %s inscrit : %s sur %s%s%s" % (
        parsed.name, parsed.harness, cfg.host,
        ", session %s" % parsed.session if parsed.session else "",
        ", consigne en attente" if parsed.prompt else "",
    ))
    return 0 if row else 1


def cmd_stop(cfg: Config, db: db_mod.Db, args: list[str]) -> int:
    if not args:
        print("usage: agent-runner stop <nom>", file=sys.stderr)
        return 2
    registry.set_status(db, args[0], "stopped", status_text="arrêté à la main")
    log("agent %s marqué arrêté" % args[0])
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cfg = load_config()

    if argv and argv[0] == "register":
        db = db_mod.connect(cfg)
        try:
            db_mod.require_schema(db)
            return cmd_register(cfg, db, argv[1:])
        finally:
            db.close()
    if argv and argv[0] == "stop":
        db = db_mod.connect(cfg)
        try:
            db_mod.require_schema(db)
            return cmd_stop(cfg, db, argv[1:])
        finally:
            db.close()

    parsed = build_parser().parse_args(argv)
    if parsed.host:
        cfg = dataclasses.replace(cfg, host=parsed.host)
    if parsed.runner_id:
        cfg = dataclasses.replace(cfg, runner_id=parsed.runner_id)
    if parsed.lease_ttl:
        cfg = dataclasses.replace(cfg, lease_ttl=parsed.lease_ttl)
    if parsed.poll:
        cfg = dataclasses.replace(cfg, poll=parsed.poll)
    if parsed.idle_nudge:
        cfg = dataclasses.replace(cfg, idle_nudge=parsed.idle_nudge)

    try:
        db = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        print("agent-runner : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    try:
        if parsed.migrate:
            from . import migrations
            migrations.migrate(db, log=log)
        try:
            db_mod.require_schema(db)
        except db_mod.SchemaMissing as exc:
            print("agent-runner : %s" % exc, file=sys.stderr)
            return 1
        agents = [a for a in (parsed.agents or "").split(",") if a] or None
        runner = Runner(
            cfg, db, agents=agents, once=parsed.once, wait=parsed.wait,
            dry_run=parsed.dry_run, max_turns=parsed.max_turns,
        )

        def handler(signum, _frame):
            log("signal %d reçu : arrêt propre" % signum)
            runner.stop.set()
            runner.wake_all.set()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
        return runner.run()
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
