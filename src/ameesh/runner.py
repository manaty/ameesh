#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
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
import getpass
import json
import os
import queue
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


class _AsyncLog:
    """Journal non bloquant pour le fil du battement (sondes codex3 B5a-N).

    Un `print`+flush sur un tube plein bloque indéfiniment : le fil du
    battement n'atteint plus l'échéance, le bail expire et un remplaçant peut
    réclamer l'agent pendant que l'ancien harnais vit encore. Les messages
    partent donc par une file **bornée**, écrite par un fil dédié ; si le puits
    sature, le message est abandonné et la perte comptée, jamais attendue.
    """

    def __init__(self, maxsize: int = 1024) -> None:
        self.queue: "queue.Queue[str]" = queue.Queue(maxsize)
        self.perdus = 0
        self.thread = threading.Thread(target=self._drain, name="journal", daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        while True:
            message = self.queue.get()
            if message is None:
                return
            if self.perdus:
                perdus, self.perdus = self.perdus, 0
                log("[journal] %d message(s) abandonné(s) : puits saturé" % perdus)
            log(message)

    def write(self, message: str) -> None:
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            self.perdus += 1


_journal = _AsyncLog()


def log_async(message: str) -> None:
    """Journal d'un chemin qui ne doit **jamais** attendre le puits (battement)."""
    _journal.write(message)


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
        self.pgid: int | None = None  # groupe du harnais en cours
        self.watchdog_lock = threading.Lock()
        self.watchdog_on = False
        self.watchdog_stop = threading.Event()

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
    def deadline_passed(self) -> bool:
        """L'échéance du bail est-elle déjà atteinte (ou inconnue) ?"""
        return self.lease_deadline > 0 and time.time() >= self.lease_deadline

    def call_budget(self) -> float:
        """Temps qu'on accepte d'attendre une réponse avant de conclure.

        Borné par l'échéance du bail, **sans marge ni plancher** : si l'échéance
        est passée, on ne donne pas 0,5 s de plus au harnais — un remplaçant peut
        déjà avoir réclamé l'agent (verdict codex3 B5a).
        """
        if self.lease_deadline <= 0:
            return max(3.0, self.runner.lease_ttl / 3.0)
        # Temps restant exact, sans plancher : un appel entamé 20 ms avant
        # l'échéance ne doit pas survivre 50 ms de plus (verdict codex3 B5a).
        return max(0.0, self.lease_deadline - time.time())

    def _renew_call(self) -> tuple[bool, float | None, Exception | None]:
        """`registry.renew` avec une borne dure, même si l'appel se bloque."""
        result: dict = {}

        def call() -> None:
            try:
                result["expires"] = registry.renew(
                    self.db, self.name, self.runner.runner_id, self.epoch,
                    self.runner.lease_ttl)
            except Exception as exc:  # DbError, mais aussi tout imprévu
                result["error"] = exc

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        thread.join(timeout=self.call_budget())
        if thread.is_alive():
            return False, None, None
        return True, result.get("expires"), result.get("error")

    def renew(self) -> bool:
        """Prolonge le bail. Une panne de base ne laisse pas le harnais tourner
        sans bail : après quelques échecs ou à l'échéance connue, on l'arrête.

        Sur toute perte de bail, l'ordre est : drapeau `lease_lost` (bon
        marché, sans E/S), **SIGKILL**, puis journal. Écrire le journal avant
        le signal a laissé, sous charge, un remplaçant réclamer le bail pendant
        que l'ancien harnais vivait encore (sonde codex3 b731f4f : +12 ms).
        """
        if self.lease_lost.is_set():
            return False
        if self.deadline_passed():
            # Échéance déjà atteinte (réveil tardif, horloge, bail raccourci) :
            # on ne tente même pas le renouvellement, on arrête tout de suite.
            self.lease_lost.set()
            self.terminate(grace=0, hard=True)
            log_async("[%s] échéance du bail atteinte : arrêt du harnais" % self.name)
            return False
        answered, expires, error = self._renew_call()
        if not answered:
            self.lease_lost.set()
            self.terminate(grace=0, hard=True)
            log_async("[%s] renouvellement sans réponse au-delà du bail : arrêt du harnais"
                      % self.name)
            return False
        if error is not None:
            self.renew_failures += 1
            echec = ("[%s] battement : base injoignable (%s) — échec %d/%d"
                     % (self.name, error, self.renew_failures, self.MAX_RENEW_FAILURES))
            definitive = (self.renew_failures >= self.MAX_RENEW_FAILURES
                          or time.time() >= self.lease_deadline)
            if not definitive:
                # Diagnostic par la file non bloquante : le fil du battement
                # doit continuer à surveiller l'échéance même si le puits est
                # plein (sonde codex3 B5a-N).
                log_async(echec)
                return True  # on laisse une chance au prochain battement
            # Perte définitive : arrêter avant d'écrire quoi que ce soit (un
            # journal bloqué ne doit pas retarder le SIGKILL, comme sur les
            # autres chemins de perte — revue codex3 de 852c8da).
            self.lease_lost.set()
            self.terminate(grace=0, hard=True)
            log_async(echec)
            log_async("[%s] bail considéré perdu (base injoignable) : arrêt du harnais"
                      % self.name)
            return False
        if expires is None:
            self.lease_lost.set()
            self.terminate(grace=0, hard=True)  # SIGKILL direct : plus de chevauchement
            log_async("[%s] bail perdu (epoch %d) : arrêt du tour" % (self.name, self.epoch))
            return False
        self.renew_failures = 0
        self.lease_deadline = float(expires)
        return True

    def release_lease(self) -> None:
        self.watchdog_stop.set()  # le veilleur n'a plus de bail à défendre
        if self.lease_lost.is_set():
            return
        try:
            registry.release(self.db, self.name, self.runner.runner_id, self.epoch)
        except db_mod.DbError:
            pass

    #: délai laissé au harnais entre SIGTERM et SIGKILL
    TERMINATE_GRACE = 3.0

    def _group(self, proc: subprocess.Popen) -> int | None:
        """Le groupe du harnais : gardé au lancement, sinon retrouvé par le pid."""
        if self.pgid:
            return self.pgid
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            return None
        return pgid if pgid != os.getpgid(0) else None

    @staticmethod
    def _signal_group(pgid: int | None, proc: subprocess.Popen, sig: int) -> None:
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except OSError:
                pass
        try:
            proc.send_signal(sig)
        except OSError:
            pass

    @staticmethod
    def _group_alive(pgid: int | None) -> bool:
        if pgid is None:
            return False
        try:
            os.killpg(pgid, 0)  # signal 0 : le groupe existe-t-il encore ?
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def terminate(self, grace: float | None = None, hard: bool = False) -> None:
        """Arrête le harnais et tout son groupe.

        Par défaut : SIGTERM, 3 secondes de grâce, puis SIGKILL. `hard=True`
        (perte du bail) envoie SIGKILL **directement, avant tout journal** : le
        remplaçant peut réclamer l'agent à l'échéance, chaque milliseconde de
        plus est un chevauchement (verdicts codex3 B5a et b731f4f).

        L'escalade ne dépend pas de la survie du **parent** : un harnais peut
        mourir sur SIGTERM en laissant un descendant qui l'ignore, et le groupe
        doit alors être tué (sonde codex3 B5b). Le groupe est mémorisé au
        lancement, donc il reste tuable même après la disparition du parent.

        L'enfant direct est moissonné après le SIGKILL : un zombie reste compté
        vivant par `killpg(pid, 0)` et faisait croire à un groupe survivant
        pendant les 2 s d'attente, en plus de fausser le constat « terminate est
        revenu donc le groupe est mort » (constat codex3 b731f4f).
        """
        with self.lock:
            proc = self.proc
        if proc is None:
            return
        pgid = self._group(proc)
        if proc.poll() is None and not hard:
            self._signal_group(pgid, proc, signal.SIGTERM)
            deadline = time.monotonic() + max(0.0, self.TERMINATE_GRACE if grace is None else grace)
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.05)
        if not self._group_alive(pgid):
            return
        # Le signal part avant le journal : un print+flush sur le chemin
        # critique laissait le temps à un remplaçant de réclamer sous charge.
        self._signal_group(pgid, proc, signal.SIGKILL)
        log_async("[%s] le groupe du harnais survit : SIGKILL" % self.name)
        # SIGKILL est asynchrone. On moissonne d'abord l'enfant direct (sinon
        # son zombie garde le groupe « vivant »), puis on attend la disparition
        # du groupe entier, dans le même budget de 2 s.
        fin = time.monotonic() + 2.0
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(0.0, fin - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        while time.monotonic() < fin and self._group_alive(pgid):
            time.sleep(0.005)
        if self._group_alive(pgid):
            log_async("[%s] groupe toujours vivant après SIGKILL (état noyau ?)" % self.name)

    def kill_if_lease_lost(self) -> bool:
        """Rattrape une publication tardive du harnais, après la perte du bail.

        Le veilleur (ou l'arrêt du worker) peut conclure entre `Popen` et la
        publication de `self.proc` : il voit alors `proc=None` et ne peut rien
        tuer. Sans ce recontrôle, le harnais publié survivrait, sans stdout,
        jusqu'à la fin du tour (sonde codex3 B5a-P) — y compris après un
        `shutdown()` qui a rendu le bail et arrêté le veilleur, alors même que
        l'ancienne échéance n'est pas encore atteinte (sonde codex3 B5a-Q).
        Renvoie True si le harnais vient d'être arrêté.
        """
        if not (self.lease_lost.is_set() or self.deadline_passed()
                or self.stopping.is_set() or self.watchdog_stop.is_set()):
            return False
        self.lease_lost.set()
        self.terminate(grace=0, hard=True)
        return True

    def wait_timeout(self, cap: float) -> float:
        """Attente bornée par l'échéance du bail, **sans plancher positif**.

        Un `max(0.05, …)` laissait le battement dormir jusqu'à 50 ms après une
        échéance atteinte : le remplaçant pouvait réclamer l'agent pendant que
        l'ancien harnais vivait encore (verdict codex3 B5a, bail à 20 ms).
        À l'échéance dépassée, l'attente vaut exactement 0 et `renew()` coupe
        immédiatement le harnais.
        """
        if self.lease_deadline > 0:
            return max(0.0, min(cap, self.lease_deadline - time.time()))
        return cap

    #: réveil maximal du veilleur : il relit l'échéance au moins à ce rythme.
    WATCHDOG_TICK = 0.25

    def ensure_watchdog(self) -> None:
        """Démarre le veilleur d'échéance, une seule fois par agent."""
        with self.watchdog_lock:
            if self.watchdog_on:
                return
            self.watchdog_on = True
        threading.Thread(target=self._deadline_watchdog, daemon=True,
                         name="%s-watchdog" % self.name).start()

    def _deadline_watchdog(self) -> None:
        """Seul juge de l'échéance (sondes codex3 B5a-N et B5a-S) : **aucune E/S**.

        Il ne dépend d'aucun autre fil : même si le battement est bloqué dans un
        journal, une requête ou une écriture de fichier, il arrête le harnais
        dès que l'échéance connue du bail est atteinte. Il ne lit que
        `lease_deadline` et `time.time`, n'attend que sur un Event (borné par
        `WATCHDOG_TICK`), puis pose `lease_lost` et envoie le SIGKILL.

        Il reste actif pendant un arrêt gracieux (`stopping`) : la grâce
        SIGTERM de 3 s ne doit pas laisser expirer le bail sans arrêt effectif
        (sonde codex3 B5a-S). Seuls le relâchement du bail (`watchdog_stop`,
        posé par `release_lease`) ou la perte déjà consommée l'arrêtent.
        """
        while not self.watchdog_stop.is_set() and not self.lease_lost.is_set():
            deadline = self.lease_deadline
            if deadline <= 0:
                self.watchdog_stop.wait(self.WATCHDOG_TICK)
                continue
            reste = deadline - time.time()
            if reste > 0:
                # Réveil au plus tard à l'échéance ; relecture régulière pour
                # voir une échéance repoussée (renouvellement) ou modifiée.
                self.watchdog_stop.wait(min(reste, self.WATCHDOG_TICK))
                continue
            if self.watchdog_stop.is_set():
                return
            if self.lease_deadline > time.time():
                continue  # bail renouvelé entre-temps : rien à faire
            self.lease_lost.set()
            log_async("[%s] échéance du bail atteinte : arrêt du harnais" % self.name)
            self.terminate(grace=0, hard=True)
            return

    def _heartbeat(self, stop_event: threading.Event) -> None:
        self.ensure_watchdog()
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
            return self._mail_spec(messages)
        if self.idle_due():
            return {"kind": "idle", "prompt": adapters.IDLE_PROMPT, "ids": []}
        return None

    def _mail_spec(self, messages: list[dict]) -> dict | None:
        """Messages non remis -> un tour. Les événements seuls sont regroupés (C9).

        Le courrier ordinaire réveille immédiatement ; un lot d'événements n'en
        réveille qu'un par fenêtre `AMEESH_EVENT_COALESCE` (défaut 120 s), sauf
        si l'un d'eux est `urgent`. L'instant du dernier réveil vit en base
        (`agent_registry.last_event_at`) : un redémarrage ne remet pas la
        fenêtre à zéro.
        """
        ids = [int(m["id"]) for m in messages if m.get("id") is not None]
        events = [m for m in messages if mail.is_event(m)]
        autres = [m for m in messages if not mail.is_event(m)]
        if autres:
            if events:
                registry.mark_event_wake(self.db, self.name)
            return {"kind": "mail", "prompt": adapters.MAIL_PROMPT, "ids": ids}
        if not events:
            return None
        fenetre = self.runner.event_coalesce
        dernier = float(self.agent.get("last_event_ts") or 0.0)
        if any(mail.is_urgent(m) for m in events) or fenetre <= 0 \
                or (time.time() - dernier) >= fenetre:
            registry.mark_event_wake(self.db, self.name)
            return {"kind": "event", "prompt": adapters.EVENT_PROMPT, "ids": ids}
        return None  # fenêtre de regroupement en cours : on attend la suivante

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
        label = {"prompt": "consigne", "mail": "messages", "event": "événements",
                 "idle": "reprise"}[spec["kind"]]
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
        identite = {
            "AGENT_MAIL_NAME": self.name,
            "AGENT_MAIL_STATE": self.cfg.v0_state,
            "AGENT_MAIL_CONFIG": self.cfg.config_dir,
            # Noms courants (ameesh) — et les anciens en alias, pour que
            # l'outillage agent-mesh d'hier continue de fonctionner.
            "AMEESH_DSN": self.cfg.dsn,
            "AGENT_MESH_DSN": self.cfg.dsn,
            "AMEESH_SCHEMA": self.cfg.schema,
            "AGENT_MESH_SCHEMA": self.cfg.schema,
            "AMEESH_STATE": self.cfg.state_dir,
            "AGENT_MESH_STATE": self.cfg.state_dir,
            "AMEESH_HOST": self.cfg.host,
            "AGENT_MESH_HOST": self.cfg.host,
            "AMEESH_RUNNER_ID": self.runner.runner_id,
            "AGENT_MESH_RUNNER_ID": self.runner.runner_id,
            # Lie l'identité agent-mail au bail : sans ce triplet, le hook ne
            # consomme rien (et le dossier ne donne jamais d'identité).
            "AMEESH_LEASE_EPOCH": str(self.epoch),
            "AGENT_MESH_LEASE_EPOCH": str(self.epoch),
        }
        env.update(identite)
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
                    start_new_session=True,  # son groupe : on peut le tuer en entier
                )
            with self.lock:
                self.proc = proc
                self.pgid = proc.pid  # start_new_session : le groupe porte son pid
            # Course de publication : le veilleur a pu conclure pendant que ce
            # Popen n'était pas encore publié (proc=None). Sans ce recontrôle,
            # le harnais survivrait au bail sans produire de stdout (B5a-P).
            if self.kill_if_lease_lost():
                log_async("[%s] bail perdu pendant le lancement : arrêt du harnais"
                          % self.name)
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
                        self.terminate(grace=0, hard=True)
                        break
            result_code = proc.wait()
        finally:
            heartbeat_done.set()
            # Arrêt escaladé (SIGTERM puis SIGKILL) : un `Popen.terminate()`
            # sec laisserait vivre un harnais qui ignore SIGTERM, hors de portée
            # du veilleur une fois `self.proc` effacé (sonde codex3 B5a-S).
            self.terminate()
            with self.lock:
                self.proc = None
                self.pgid = None

        duration = time.time() - started
        lost = self.lease_lost.is_set()
        ok = result_code == 0 and not lost and not error
        if error is None and result_code != 0:
            error = "le harnais a rendu le code %d" % result_code
        status = "idle" if ok else "blocked"
        if lost:
            # Le bail n'est plus vivant : on ne clôt PAS le tour. `end_turn`
            # effacerait current_prompt alors que le tour n'a pas abouti (son
            # fence par epoch ne suffit pas : un bail expiré mais pas encore
            # repris porte toujours notre epoch), et `set_status` écraserait
            # l'état d'un agent qu'un remplaçant a pu reprendre.
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
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
        if spec["kind"] in ("mail", "event") and ok and spec["ids"]:
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
        self.ensure_watchdog()
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
        self.ensure_watchdog()
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
        self.event_coalesce = max(0.0, cfg.event_coalesce)
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
            # Un tour en cours ne doit pas continuer pendant qu'on rend le bail.
            worker.terminate()
        deadline = time.monotonic() + 8.0
        for worker in workers:
            worker.join(timeout=max(0.1, deadline - time.monotonic()))
            # Le tour a pu publier son harnais après le terminate initial
            # (course de publication) : rien ne doit survivre au relâchement
            # du bail (sonde codex3 B5a-P).
            worker.terminate(grace=0, hard=True)
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


# --------------------------------------------------------------------------
# attach : une session interactive qui prend le bail (C9)
# --------------------------------------------------------------------------

def attach_owner(host: str) -> str:
    """Identité d'exécuteur d'une session attachée : distincte d'un runner."""
    return "attach:%s@%s" % (getpass.getuser(), host)


def _attach_env(cfg: Config, name: str, owner: str, epoch: int) -> dict:
    """L'identité que le harnais interactif transmet à ses hooks."""
    env = os.environ.copy()
    env.update({
        "AGENT_MAIL_NAME": name,
        "AGENT_MAIL_STATE": cfg.v0_state,
        "AGENT_MAIL_CONFIG": cfg.config_dir,
        "AMEESH_DSN": cfg.dsn, "AGENT_MESH_DSN": cfg.dsn,
        "AMEESH_SCHEMA": cfg.schema, "AGENT_MESH_SCHEMA": cfg.schema,
        "AMEESH_STATE": cfg.state_dir, "AGENT_MESH_STATE": cfg.state_dir,
        "AMEESH_HOST": cfg.host, "AGENT_MESH_HOST": cfg.host,
        "AMEESH_RUNNER_ID": owner, "AGENT_MESH_RUNNER_ID": owner,
        "AMEESH_LEASE_EPOCH": str(epoch), "AGENT_MESH_LEASE_EPOCH": str(epoch),
    })
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL du groupe du harnais (il est lancé dans sa propre session)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def run_attach(cfg: Config, db: db_mod.Db, name: str, *, wait: bool = False,
               ttl: float | None = None) -> int:
    """`ameesh attach <agent>` : bail interactif sur la même session (C9).

    Le bail est pris même s'il est déjà détenu par un exécuteur **sans tour en
    cours** : c'est ce qui suspend la réclamation automatique (l'ancien
    exécuteur est fencé par l'epoch à son prochain renouvellement). Un tour en
    cours n'est jamais interrompu : refus, ou attente avec `--wait`. Le bail est
    renouvelé tant que la session vit, le harnais est arrêté si le bail est
    perdu, et le bail est rendu à la sortie.
    """
    agent = registry.get(db, name)
    if agent is None:
        print("agent inconnu : %s" % name, file=sys.stderr)
        return 1
    if agent.get("status") == "stopped":
        print("agent %s arrêté : rien à attacher" % name, file=sys.stderr)
        return 1
    if (agent.get("host") or "") != cfg.host:
        print("agent %s épinglé à %s, pas à %s" % (name, agent.get("host") or "?", cfg.host),
              file=sys.stderr)
        return 1
    try:
        adapter = adapters.adapter_for(agent.get("harness") or "other")
    except adapters.HarnessMissing as exc:
        print("attach : %s" % exc, file=sys.stderr)
        return 1
    owner = attach_owner(cfg.host)
    ttl = max(5.0, ttl or cfg.lease_ttl)
    registry.reap(db, cfg.host)
    while True:
        if registry.turn_in_progress(db, name):
            if not wait:
                print("un tour est en cours pour %s (relancez avec --wait pour attendre)"
                      % name, file=sys.stderr)
                return 3
            print("tour en cours pour %s : attente…" % name, file=sys.stderr, flush=True)
            time.sleep(2.0)
            continue
        lease = registry.attach_claim(db, name, owner, ttl)
        if lease:
            break
        print("agent %s non réclamable (responsable non résolu, éphémère échu ou état)"
              % name, file=sys.stderr)
        return 1
    epoch = int(lease["lease_epoch"])
    log_async("[%s] bail attach pris (epoch %d)" % (name, epoch))
    stop = threading.Event()
    perdu = threading.Event()
    etat = {"deadline": float(lease.get("lease_expires_ts") or 0.0)}

    def battement() -> None:
        interval = max(3.0, ttl / 3.0)
        while not stop.wait(interval):
            try:
                expires = registry.renew(db, name, owner, epoch, ttl)
            except db_mod.DbError as exc:
                log_async("[%s] attach : renouvellement en échec (%s)" % (name, exc))
                continue
            if expires is None:
                perdu.set()  # couper d'abord : le journal ne doit jamais retarder l'arrêt
                log_async("[%s] attach : bail perdu" % name)
                return
            etat["deadline"] = float(expires)

    def veilleur() -> None:
        """Borne dure : le harnais interactif meurt à l'échéance connue du bail.

        Verdict L8 B2 : sans veilleur, une erreur SQL de renouvellement laissait
        la session vivante après l'expiration, pendant qu'un remplaçant
        réclamait l'agent. Le veilleur ne fait **aucune E/S** : il lit l'échéance
        et l'heure, pose `perdu` (donc la coupe), et ne journalise qu'ensuite,
        par le journal asynchrone borné — un `print` synchrone sur un tube plein
        bloquerait le chemin d'arrêt (sonde codex3 du 04:42). L'attente est
        bornée par l'échéance, comme le veilleur d'`AgentWorker`.
        """
        while not stop.is_set():
            if etat["deadline"] <= 0:
                stop.wait(0.5)
                continue
            reste = etat["deadline"] - time.time()
            if reste <= 0:
                perdu.set()
                log_async("[%s] attach : échéance du bail atteinte, arrêt du harnais" % name)
                return
            stop.wait(max(0.05, min(0.5, reste)))

    thread = threading.Thread(target=battement, daemon=True, name="%s-attach" % name)
    thread.start()
    veille = threading.Thread(target=veilleur, daemon=True, name="%s-attach-veilleur" % name)
    veille.start()
    argv = adapter.interactive_command(agent.get("session_id"))
    env = _attach_env(cfg, name, owner, epoch)
    env.update(adapter.env())
    cwd = agent.get("cwd") or None
    if cwd and not os.path.isdir(cwd):
        cwd = None
    proc: subprocess.Popen | None = None
    code = 0
    try:
        log_async("[%s] session interactive : %s" % (name, " ".join(argv)))
        proc = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True)
        # Course de publication (même correctif que B5a-P) : le veilleur a pu
        # conclure pendant que ce Popen n'était pas encore publié.
        if etat["deadline"] > 0 and time.time() >= etat["deadline"]:
            perdu.set()
        while proc.poll() is None:
            if perdu.is_set():
                _kill_group(proc)  # couper d'abord, journaliser ensuite
                log_async("[%s] attach : bail perdu, arrêt du harnais" % name)
                break
            time.sleep(0.2)
        code = proc.wait()
    except KeyboardInterrupt:
        if proc is not None:
            _kill_group(proc)
            code = proc.wait()
    finally:
        stop.set()
        thread.join(timeout=5)
        veille.join(timeout=5)
        try:
            registry.release(db, name, owner, epoch)
        except db_mod.DbError:
            pass
        log_async("[%s] bail attach rendu" % name)
    return code


def attach_main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="ameesh attach",
        description="Ouvrir une session interactive sur le bail d'un agent (C9).")
    parser.add_argument("agent")
    parser.add_argument("--wait", action="store_true",
                        help="attendre la fin d'un tour en cours avant de prendre le bail")
    parser.add_argument("--ttl", type=float, default=None, help="durée du bail en secondes")
    parsed = parser.parse_args(argv)
    cfg = load_config()
    try:
        db = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        print("ameesh attach : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    try:
        try:
            db_mod.require_schema(db)
        except db_mod.SchemaMissing as exc:
            print("ameesh attach : %s" % exc, file=sys.stderr)
            return 1
        return run_attach(cfg, db, parsed.agent, wait=parsed.wait, ttl=parsed.ttl)
    finally:
        db.close()


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
