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
import uuid

from . import account_turn, adapters, canon as canon_mod, canon_sync, cost as cost_mod
from . import budget as budget_mod, db as db_mod, fil, mail, registry, storage
from .config import CHANNEL_BUDGET, CHANNEL_LEASE, CHANNEL_MAIL, Config
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
        self.queue: "queue.Queue[str | None]" = queue.Queue(maxsize)
        self.perdus = 0
        #: posé par `close` : plus rien n'entre dans la file (fin du processus)
        self.ferme = False
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
        if self.ferme:
            # Après `close` (fin du processus), un fil démon tardif (canon,
            # solde, veilleur) ne doit plus rien écrire : message abandonné.
            self.perdus += 1
            return
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            self.perdus += 1

    def close(self, timeout: float = 2.0) -> bool:
        """Vide la file puis arrête le fil, **dans un délai borné**.

        À appeler avant la fin de l'interpréteur : un fil démon encore dans un
        `print` au moment de la finalisation garde le verrou de `sys.stdout`,
        et le vidage final de CPython échoue alors en erreur fatale
        (« could not acquire lock for <stdout> at interpreter shutdown »,
        SIGABRT). Rend `True` si le fil s'est arrêté, `False` s'il est resté
        bloqué (puits plein) au-delà du délai : l'appelant ne doit alors pas
        laisser l'interpréteur se finaliser normalement.
        """
        fin = time.monotonic() + max(0.0, timeout)
        self.ferme = True
        thread = self.thread
        if not thread.is_alive():
            return True
        try:
            self.queue.put(None, timeout=max(0.0, fin - time.monotonic()))
        except queue.Full:
            return False
        thread.join(timeout=max(0.0, fin - time.monotonic()))
        return not thread.is_alive()


_journal = _AsyncLog()


def log_async(message: str) -> None:
    """Journal d'un chemin qui ne doit **jamais** attendre le puits (battement)."""
    _journal.write(message)


#: délai borné accordé au fil `journal` pour vider sa file à l'arrêt
JOURNAL_CLOSE_TIMEOUT = 2.0


def _ecrire_sans_attendre(fd: int, data: bytes) -> None:
    """Écrit `data` sur `fd` **sans jamais attendre** le puits (diagnostic).

    Le descripteur passe en O_NONBLOCK le temps de l'écriture : un tube plein
    rend EAGAIN, ignoré (message perdu). Toute erreur est ignorée.
    """
    try:
        import fcntl
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            os.write(fd, data)
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    except (OSError, ImportError, ValueError):
        pass


def _sortie_sure(code: int, fils: "list[threading.Thread] | tuple" = ()) -> int:
    """Prépare la fin du processus sans fil démon en train d'écrire.

    À appeler dans le `finally` du point d'entrée, **après** la remise des
    baux et la comptabilité (donc incontournable, même sur exception).
    Vide puis arrête le fil `journal` (délai borné). Si lui ou l'un des `fils`
    donnés (workers, qui écrivent par `log`) vit encore — bloqué sur un tube
    plein, typiquement —, la finalisation normale de CPython abandonnerait le
    processus (SIGABRT) en tentant de vider `sys.stdout` dont le verrou est
    tenu : on sort alors par `os._exit(code)`, sans vidage (chaque `log` vide
    déjà son flux), après un diagnostic qui n'attend jamais le puits. Aucune
    écriture synchrone sur stdout/stderr ici : un arrêt ne pend jamais.
    """
    journal_arrete = _journal.close(JOURNAL_CLOSE_TIMEOUT)
    vivants = [t for t in fils if t is not None and t.is_alive()]
    if journal_arrete and not vivants:
        return code
    noms = ", ".join(t.name for t in vivants) or "journal"
    _ecrire_sans_attendre(2, ("agent-runner : fil(s) encore actif(s) à l'arrêt (%s) : "
                              "sortie immédiate\n" % noms).encode("utf-8", "replace"))
    os._exit(code)


class Reprise:
    """Attente exponentielle bornée pendant une panne de base (L72).

    Un compteur par point d'appel (passe de l'exécuteur, écoute, worker,
    battement) : 2 s, 4 s, 8 s… jusqu'à `maximum`, remis à zéro au premier
    succès. Rien d'autre : le journal reste à l'appelant, qui sait où il est.
    """

    MINIMUM = 2.0

    def __init__(self, maximum: float = 60.0, minimum: float | None = None):
        self.minimum = float(self.MINIMUM if minimum is None else minimum)
        self.maximum = max(self.minimum, float(maximum))
        self.echecs = 0
        self.depuis = 0.0

    @property
    def en_panne(self) -> bool:
        return self.echecs > 0

    def echec(self) -> float:
        """Compte un échec ; rend l'attente avant le prochain essai."""
        if not self.echecs:
            self.depuis = time.time()
        self.echecs += 1
        return min(self.maximum, self.minimum * (2 ** min(self.echecs - 1, 30)))

    def duree(self) -> float:
        return max(0.0, time.time() - self.depuis) if self.echecs else 0.0

    def retablie(self) -> tuple[int, float] | None:
        """Succès : (échecs, durée de la panne) si on sortait d'une panne."""
        if not self.echecs:
            return None
        bilan = (self.echecs, self.duree())
        self.echecs = 0
        self.depuis = 0.0
        return bilan


def worktree_marker(cwd: str | None) -> dict:
    """Identité git d'un dossier de travail (0018 : suivre un renommage).

    `git_common_dir` est le dépôt commun (le même pour tous les worktrees) ;
    la branche distingue les worktrees d'un même dépôt. Best-effort : renvoie
    `{}` si le dossier n'est pas un dépôt ou si git manque.
    """
    if not cwd or not os.path.isdir(cwd):
        return {}
    try:
        common = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5)
        branche = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    if common.returncode != 0 or not common.stdout.strip():
        return {}
    return {
        "cwd": os.path.abspath(cwd),
        "git_common_dir": os.path.abspath(os.path.join(cwd, common.stdout.strip())),
        "branch": branche.stdout.strip() if branche.returncode == 0 else "",
    }


def write_worktree_marker(cfg: Config, name: str, cwd: str) -> dict:
    """Écrit le marqueur du dossier de travail dans l'état de l'agent."""
    marker = worktree_marker(cwd)
    if not marker:
        return {}
    directory = cfg.agent_dir(name)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with open(os.path.join(directory, "worktree.json"), "w", encoding="utf-8") as fh:
            json.dump(marker, fh, ensure_ascii=False)
    except OSError:
        return {}
    return marker


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
        #: L48 : le dernier tour a échoué vite (harnais absent, introuvable,
        #: sorti en erreur en moins de FAST_FAILURE_S) ; et leur série
        self.fast_failure = False
        self.fast_failures = 0
        self.last_activity = time.monotonic()
        self.state_dir = self.cfg.agent_dir(self.name)
        self.renew_failures = 0
        self.lease_deadline = float(lease.get("lease_expires_ts") or 0.0)
        self.pgid: int | None = None  # groupe du harnais en cours
        #: dernier descripteur de harnais journalisé (chemin, empreinte) — L16
        self.descriptor_seen: tuple[str, str] = ("", "")
        self.watchdog_lock = threading.Lock()
        self.watchdog_on = False
        self.watchdog_stop = threading.Event()
        #: message prioritaire : préempte le tour en cours (0018, R19)
        self.preempting = threading.Event()
        self._refus_vus: set[int] = set()
        #: arrêt demandé avant la publication du Popen (point de passage unique)
        self.stop_requested = threading.Event()
        self._stop_reason = ""
        #: garde de budget (L13, 0019) : état de pause et cadence de contrôle
        self._budget_next_check = 0.0
        self._budget_paused = False
        self._budget_reason = ""
        #: la comptabilité du dernier tour a échoué : plus de tour à l'aveugle.
        #: Un marqueur persistant rend la suspension durable au redémarrage (B5).
        #: L72 : base injoignable à la création — doute, donc suspension, mais
        #: levée dès qu'une lecture dit qu'aucun marqueur n'attend
        self._compta_doute = False
        try:
            self._compta_en_echec = self._compta_en_attente() is not None
        except db_mod.Unavailable:
            self._compta_en_echec = self._compta_doute = True
        #: modèle annoncé par le flux du tour en cours, gardé en mémoire **en plus** de
        #: la base : si l'écriture du marqueur échoue, ce worker-ci répare quand même
        #: au bon tarif (L13 B4). Effacé quand la ligne du grand livre est écrite.
        self._annonce_ram = ""
        #: le harnais du tour de courrier en cours a-t-il été lancé ? (livraison)
        self._courrier_lance = False
        #: rotation de session (0018) : compteurs de la session courante
        self.resume_summary = ""
        self.session_turns = 0
        self.session_tokens = 0.0
        self.last_turn_seconds = 0.0
        self.last_output = ""
        #: plafond de contexte (L60) : jetons d'entrée relus (cache compris) par
        #: le dernier tour inscrit au grand livre, et sa session
        self.last_turn_reread = 0
        self.last_turn_reread_session: str | None = None
        #: `ameesh restart` (L26) : arrêt du tour en cours, puis session neuve
        self.restarting = threading.Event()
        #: comptes multiples (L30, 0027) : compte choisi pour le prochain tour, et
        #: compte imposé au tour de résumé d'une bascule (l'ancien compte)
        self._account = None
        self._account_override = None
        #: dossier de travail absent (L35) : blocage en cours, `cwd` du registre
        #: au dernier échec, attente courante et échéance du prochain essai
        #: complet. Un blocage hérité d'un exécuteur précédent est repris tel
        #: quel, pour que le statut soit levé dès que le dossier revient.
        self._wd_blocked = (agent.get("status") == "blocked"
                            and agent.get("status_text") == self.WORKDIR_STATUS
                            and str(agent.get("last_error") or "").startswith(
                                self.WORKDIR_ERROR))
        self._wd_seen = agent.get("cwd")
        self._wd_delay = 0.0
        self._wd_next = 0.0
        #: panne de base (L72) : attente croissante entre deux essais, consigne
        #: consommée par un tour qui n'a pas pu partir (à remettre en attente
        #: au retour de la base), statut « base injoignable » posé, et session
        #: ouverte par le harnais mais pas encore enregistrée au registre
        self._reprise = Reprise(getattr(runner, "db_retry_max", 60.0))
        self._consigne_a_restaurer = False
        self._base_marquee = False
        self._session_en_attente: tuple[str, str | None] | None = None

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

    def current_session(self) -> str | None:
        """La session à reprendre : celle du registre, sinon le fichier local.

        Un fichier local **antérieur** au dernier oubli de session
        (`session_reset_ts`, posé par `ameesh restart` appliqué sans bail)
        n'est pas repris : il est effacé (L26).
        """
        session = (self.agent or {}).get("session_id")
        if session:
            return session
        fichier = self.read_session_file()
        reset = (self.agent or {}).get("session_reset_ts")
        if fichier and reset:
            try:
                if os.stat(self._path("session")).st_mtime <= float(reset):
                    os.unlink(self._path("session"))
                    return None
            except OSError:
                return None
        return fichier

    def write_session_file(self, session_id: str) -> None:
        try:
            with open(self._path("session"), "w", encoding="utf-8") as fh:
                fh.write(session_id + "\n")
        except OSError:
            pass

    def _fil_note(self, text: str, *, meta: dict | None = None) -> None:
        """Trace d'audit dans le fil du projet de l'agent (R12) ; ne lève jamais."""
        try:
            chantier = (self.agent or {}).get("chantier") or ""
            project = fil.project_for(self.cfg, chantier)
            fil.record(self.cfg, self.db, sender=self.name, recipients=[], text=text,
                       ts=time.time(), project=project,
                       meta=dict(meta or {}, audit="runner"))
        except Exception as exc:  # une trace ne casse jamais un tour
            log("[%s] fil indisponible : %s" % (self.name, exc))

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
            self.stop_group_now("échéance du bail atteinte", grace=0, hard=True)
            return False
        answered, expires, error = self._renew_call()
        if not answered:
            self.lease_lost.set()
            self.stop_group_now("renouvellement sans réponse au-delà du bail",
                                grace=0, hard=True)
            return False
        if error is not None:
            self.renew_failures += 1
            if isinstance(error, db_mod.Unavailable) and self.lease_deadline > 0:
                # L72 : base injoignable, échéance connue — le harnais continue
                # tant que le bail court encore ; seule l'échéance (ici, et le
                # veilleur qui n'attend personne) l'arrête. Le compte d'échecs
                # n'a plus de sens : le battement réessaie vite (2 s, 4 s…).
                reste = self.lease_deadline - time.time()
                echec = ("[%s] battement : %s — échec %d, bail valable encore %ds : "
                         "le tour continue, nouvel essai bientôt"
                         % (self.name, db_mod.explain(error), self.renew_failures,
                            max(0, int(reste))))
                definitive = reste <= 0
            else:
                echec = ("[%s] battement : base injoignable (%s) — échec %d/%d"
                         % (self.name, error, self.renew_failures,
                            self.MAX_RENEW_FAILURES))
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
            # autres chemins de perte — revue codex3 de 852c8da). Le diagnostic
            # part après le signal, par la file bornée.
            self.lease_lost.set()
            self.stop_group_now("base injoignable", grace=0, hard=True)
            log_async(echec)
            return False
        if expires is None:
            self.lease_lost.set()
            self.stop_group_now("bail perdu (epoch %d)" % self.epoch, grace=0, hard=True)
            return False
        if self.renew_failures:
            log_async("[%s] battement : base de nouveau joignable, bail renouvelé après "
                      "%d échec(s)" % (self.name, self.renew_failures))
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
        if not self._group_alive(pgid) and proc.poll() is not None:
            # Groupe mort **et** enfant direct moissonné : rien à faire. Si le
            # pid n'est pas un chef de groupe (groupe inconnu) mais que l'enfant
            # vit encore, on ne doit pas sortir sans lui envoyer le SIGKILL.
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

    def stop_group_now(self, reason: str, *, grace: float | None = None,
                       hard: bool = False) -> bool:
        """Point de passage **unique** de tout arrêt de harnais (0018, verdicts codex3).

        1. Le signal part d'abord (`terminate`, qui n'écrit rien avant) ;
        2. le journal vient ensuite, par la file bornée `log_async` ;
        3. si le `Popen` n'est pas encore publié, on pose `stop_requested` et
           `_stop_reason` : `kill_if_stop_requested()` recontrôlera juste après
           la publication (courses B5a-P et L11 B2).

        Tous les déclencheurs — échéance, bail perdu, arrêt de l'exécuteur,
        préemption, et demain la garde de budget (L13) — passent par ici : un
        nouveau chemin hérite des sondes paramétrées.
        """
        with self.lock:
            proc = self.proc
        self._stop_reason = reason
        if proc is None:
            # Le latch ne sert qu'à la course de publication en cours : il est
            # consommé par le recontrôle, puis remis à zéro au tour suivant
            # (verdict codex3 L11 B5 : un latch permanent tuerait la reprise).
            self.stop_requested.set()
            log_async("[%s] arrêt demandé (%s) : harnais non encore publié"
                      % (self.name, reason))
            return False
        self.terminate(grace=grace, hard=hard)
        log_async("[%s] harnais arrêté (%s)" % (self.name, reason))
        return True

    def kill_if_stop_requested(self) -> bool:
        """Recontrôle après publication du `Popen` : tout déclencheur d'arrêt.

        Remplace les recontrôles par cause (`kill_if_lease_lost`,
        `kill_if_preempted`) : un déclencheur nouveau, comme la préemption
        arrivée pendant le lancement, est rattrapé sans code dédié (verdict
        codex3 L11 B2).
        """
        en_cause = (self.lease_lost.is_set() or self.deadline_passed()
                    or self.stopping.is_set() or self.watchdog_stop.is_set()
                    or self.preempting.is_set() or self.stop_requested.is_set()
                    or self.restarting.is_set())
        if not en_cause:
            return False
        if (self.deadline_passed() or self.stopping.is_set()
                or self.watchdog_stop.is_set()):
            # Arrêt de classe « bail » : le tour ne doit pas être clos ni compté.
            self.lease_lost.set()
        with self.lock:
            proc = self.proc
        if proc is None:
            self.stop_requested.set()
            return False
        self.stop_group_now(self._stop_reason or "arrêt", grace=0, hard=True)
        # Le latch de publication est consommé : le prochain tour doit pouvoir
        # tourner (verdict codex3 L11 B5).
        self.stop_requested.clear()
        return True

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
        return self.stop_group_now("bail perdu", grace=0, hard=True)

    def kill_if_preempted(self) -> bool:
        """Rattrape une préemption arrivée avant la publication du `Popen` (0018).

        Le moniteur de tour peut déclencher entre `Popen` et l'affectation de
        `self.proc` : il voit `proc=None`, ne tue rien, et son fil se termine —
        le harnais survivrait alors jusqu'à la fin du tour (même classe que
        B5a-P, verdict codex3 L11 B2). Renvoie True si le harnais vient d'être
        arrêté.
        """
        if not self.preempting.is_set():
            return False
        return self.stop_group_now("préemption", grace=0, hard=True)

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
            self.stop_group_now("échéance du bail atteinte", grace=0, hard=True)
            return

    def _heartbeat(self, stop_event: threading.Event) -> None:
        self.ensure_watchdog()
        interval = max(3.0, self.runner.lease_ttl / 3.0)
        # L72 : après un échec, nouvel essai rapide (2 s, 4 s… jusqu'à
        # l'intervalle normal) — une base revenue doit prolonger le bail avant
        # son échéance, pas un tiers de bail plus tard.
        reprise = Reprise(maximum=interval)
        attente = interval
        while not stop_event.wait(self.wait_timeout(attente)):
            if not self.renew():
                return
            if self.renew_failures:
                attente = reprise.echec()
            else:
                reprise.retablie()
                attente = interval

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

    def request_preempt(self) -> None:
        """Un message prioritaire préempte le tour en cours (0018, R19).

        Le tour est arrêté (SIGTERM bref puis SIGKILL), sa consigne repart en
        attente, et `pick()` sert le message prioritaire en tête au tour suivant,
        sur la même session.
        """
        self.preempting.set()
        self.wake.set()

    def _preempt_monitor(self, done: threading.Event) -> None:
        """Arrête le harnais dès qu'un message prioritaire arrive (0018).

        Passe par le point de passage unique : signal d'abord, journal borné
        ensuite ; une préemption arrivée avant la publication du `Popen` est
        rattrapée par `kill_if_stop_requested()`.
        """
        while not done.wait(0.1):
            if self.preempting.is_set():
                self.stop_group_now("préemption", grace=1.0)
                return
            if self.restarting.is_set():
                # `ameesh restart` (L26) : même point de passage, même grâce
                self.stop_group_now("redémarrage demandé", grace=1.0)
                return

    def request_restart(self) -> None:
        """`ameesh restart` (L26) : arrêter le tour en cours, puis appliquer."""
        self.restarting.set()
        self.wake.set()

    def apply_restart_if_requested(self) -> bool:
        """Applique une demande `ameesh restart` en attente, fencée par le bail.

        En une écriture : session et lot de session oubliés, brief placé en
        tête de la consigne en attente. Ici : fichier de session local effacé,
        compteurs et résumé de reprise remis à zéro (la session neuve s'ouvre
        sur le brief, pas sur un résumé). Jamais pendant un tour.
        """
        if self.proc is not None:
            return False
        agent = registry.get(self.db, self.name)
        if agent is None or not agent.get("restart_requested_ts"):
            self.restarting.clear()
            return False
        row = storage.of(self.db).operations.apply_restart(
            self.name, self.runner.runner_id, self.epoch)
        if not row:
            return False
        self.restarting.clear()
        try:
            os.unlink(self._path("session"))
        except OSError:
            pass
        self.resume_summary = ""
        self.session_turns = 0
        self.session_tokens = 0.0
        self.last_turn_reread = 0
        self.nudged = False
        self.agent = registry.get(self.db, self.name) or self.agent
        log_async("[%s] redémarrage appliqué : session oubliée, brief en tête" % self.name)
        self._fil_note(
            "Redémarrage appliqué : l'ancienne session (%s) est oubliée ; la session "
            "neuve s'ouvre sur le brief déposé." % (row.get("forgotten_session") or "aucune"),
            meta={"action": "restart", "session": row.get("forgotten_session") or ""})
        return True

    # -- rotation de session (0018) ----------------------------------------
    def session_policy(self) -> str:
        """Politique de session effective (0025, L26) : réglage de l'agent
        (`ameesh set session_policy=…`), sinon défaut de l'exécuteur."""
        return ((self.agent or {}).get("session_policy") or "").strip() \
            or self.cfg.session_policy

    def rotation_due(self) -> bool:
        """Rotation due ? Jamais pendant un tour, jamais avant le minimum.

        Politique `jamais` : aucune rotation ; `taille` et `par-lot` : la
        rotation sur la taille (L11) reste le garde-fou."""
        if self.proc is not None:
            return False
        if self.session_policy() == "jamais":
            return False
        if self.session_turns < self.runner.session_min_turns:
            return False
        return (self.session_tokens >= self.runner.session_max_tokens
                or self.last_turn_seconds >= self.runner.session_max_turn_seconds)

    def context_max_tokens(self) -> int:
        """Plafond de contexte effectif (L60) : réglage de l'agent
        (`ameesh set context_max_tokens=…`), sinon défaut de l'exécuteur
        (`AMEESH_CONTEXT_MAX_TOKENS`). 0 = désactivé."""
        from .exploitation import effective_context_max
        return effective_context_max(self.cfg, self.agent)

    def context_rotation_due(self) -> bool:
        """Le dernier tour a-t-il relu plus que le plafond de contexte ? (L60)

        La mesure est celle de l'alerte `session_too_big` : entrée + entrée
        relue en cache du dernier tour inscrit au grand livre. Chaque étape
        d'un tour relit tout le contexte : une session qui grossit coûte de
        plus en plus cher à chaque tour, et l'alerte seule n'y changeait rien.
        Ni minimum de tours, ni rotation pendant un tour ; la politique
        `jamais` s'en dispense ; il faut une session courante, et que le
        relevé soit bien celui de cette session.
        """
        if self.proc is not None or self.session_policy() == "jamais":
            return False
        plafond = self.context_max_tokens()
        if plafond <= 0 or self.last_turn_reread < plafond:
            return False
        session = self.current_session()
        if not session:
            return False
        return self.last_turn_reread_session in (None, session)

    def _session_history(self, session_id: str | None, resume: str) -> None:
        """Garde l'ancien id de session et son résumé (audit, 0018)."""
        entry = {
            "ts": time.time(), "session": session_id, "turns": self.session_turns,
            "tokens": self.session_tokens,
            "last_turn_s": round(self.last_turn_seconds, 1), "resume": resume[:2000],
        }
        try:
            with open(self._path("session-history.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def maybe_rotate(self) -> bool:
        """Résume la session courante puis en ouvre une neuve (0018, R19).

        Le résumé est produit **dans** la session (un tour), écrit dans le fil
        (R12), l'ancien id est conservé dans l'historique de l'état, puis la
        session est oubliée : le prochain tour repart d'une session neuve,
        préfixé par le résumé. Ne se déclenche jamais pendant un tour.
        """
        if self.context_rotation_due():
            return self._rotate("plafond de contexte : %d jetons relus au dernier tour "
                                "(plafond %d)" % (self.last_turn_reread,
                                                  self.context_max_tokens()))
        if not self.rotation_due():
            return False
        return self._rotate("tours=%d, tokens=%.0f, dernier tour=%.0fs" % (
            self.session_turns, self.session_tokens, self.last_turn_seconds))

    def _rotate(self, raison: str) -> bool:
        """Le mécanisme de rotation (L11) : résumé dans la session, puis oubli."""
        ancienne = self.current_session()
        log_async("[%s] rotation de session (%s)" % (self.name, raison))
        ok = self.run_turn({"kind": "prompt", "prompt": adapters.SUMMARY_PROMPT, "ids": []})
        if not ok:
            # Résumé partiel, bail perdu pendant le tour, claim remplaçant : on ne
            # touche à rien. Effacer la session ici effacerait celle du remplaçant
            # (verdict codex3 L11 B3).
            log_async("[%s] rotation annulée : le tour de résumé n'a pas abouti" % self.name)
            self._fil_note("Rotation de session annulée : le tour de résumé n'a pas "
                           "abouti. L'ancienne session est conservée ; nouvelle "
                           "tentative plus tard.",
                           meta={"action": "rotation", "etat": "annulee"})
            return False
        resume = (self.last_output or "").strip()
        if not resume:
            log_async("[%s] rotation annulée : résumé vide" % self.name)
            self._fil_note("Rotation de session annulée : résumé vide. L'ancienne "
                           "session est conservée.",
                           meta={"action": "rotation", "etat": "annulee"})
            return False
        self._session_history(ancienne, resume)
        # L'effacement est fencé par le bail : si un remplaçant a réclamé l'agent
        # entre-temps, sa session n'est pas touchée.
        if not registry.clear_session(self.db, self.name, self.runner.runner_id, self.epoch):
            log_async("[%s] rotation annulée : bail perdu avant l'effacement de la session"
                      % self.name)
            self._fil_note("Rotation de session annulée : le bail a changé de main "
                           "avant l'effacement ; rien n'a été touché.",
                           meta={"action": "rotation", "etat": "annulee"})
            return False
        self.resume_summary = resume
        try:
            os.unlink(self._path("session"))
        except OSError:
            pass
        self.agent = registry.get(self.db, self.name) or self.agent
        self.session_turns = 0
        self.session_tokens = 0.0
        self.last_turn_reread = 0
        self.last_turn_reread_session = None
        self._fil_note(
            "Rotation de session (%s) : l'ancien id (%s) est conservé dans "
            "`session-history.jsonl` ; la session neuve repart du résumé ci-dessous.\n\n%s"
            % (raison, ancienne or "neuve", resume),
            meta={"action": "rotation", "session": ancienne or "", "raison": raison})
        return True

    # -- rotation au changement de lot (0025, L26) ---------------------------
    def turn_lot(self, spec: dict) -> str | None:
        """Le lot (`work_item`) d'un tour, ou None s'il n'est pas déductible.

        1. Les messages du tour : s'ils portent UN seul lot (`--lot`), c'est
           lui ; plusieurs lots distincts = ambigu, None.
        2. Sinon, l'unique lot ouvert assigné à l'agent ; plusieurs = None.
        Un tour dont le lot est inconnu ne déclenche jamais de rotation.
        """
        ops = storage.of(self.db).operations
        try:
            ids = [int(i) for i in spec.get("ids") or []]
            if ids:
                lots = ops.message_lots(ids)
                if len(lots) == 1:
                    return lots[0]
                if len(lots) > 1:
                    return None
            assignes = ops.assigned_open_lots(self.name)
        except db_mod.DbError as exc:
            log_async("[%s] lot du tour illisible (%s)" % (self.name, exc))
            return None
        if len(assignes) == 1:
            return str(assignes[0]["id"])
        return None

    def _note_delegate_turn(self, spec: dict) -> None:
        """L40 (0030) : le tour qui commence compte pour les lots délégués à
        cet agent — le lot du tour et ceux de ses messages. Noté une fois par
        délégation (registre et journal du lot) : à l'échéance, le lot ne
        reviendra pas au délégant. Jamais bloquant pour le tour."""
        from . import work as work_mod

        lots = {str(spec["lot"])} if spec.get("lot") else set()
        try:
            ids = [int(i) for i in spec.get("ids") or []]
            if ids:
                lots.update(storage.of(self.db).operations.message_lots(ids))
            marques = work_mod.mark_delegate_turn(self.db, self.name, lots)
        except (db_mod.DbError, ValueError) as exc:
            log_async("[%s] tour sur lot délégué non noté (%s)" % (self.name, exc))
            return
        if marques:
            log_async("[%s] tour sur lot(s) délégué(s) : %s"
                      % (self.name, ", ".join("#%d" % i for i in marques)))

    def lot_rotation_due(self, lot: str | None) -> bool:
        """Rotation au changement de lot due ? (politique `par-lot`)

        Seulement si le lot du tour est connu, que la session courante a un
        lot noté et qu'il diffère, et qu'il y a bien une session à tourner.
        Jamais pendant un tour.
        """
        if not lot or self.proc is not None or self.session_policy() != "par-lot":
            return False
        courant = (self.agent or {}).get("session_work_item")
        if not courant or str(courant) == str(lot):
            return False
        return bool(self.current_session())

    def rotate_for_lot(self, spec: dict | None) -> dict | None:
        """Avant un tour : ouvre une session neuve si le lot change (0025).

        Mécanisme L11 (résumé dans l'ancienne session, puis oubli fencé par le
        bail) : la session neuve s'ouvre sur le résumé de reprise. Une
        consigne déjà prise par `pick()` est remise en attente le temps du
        tour de résumé, puis reprise. Si le résumé échoue, le tour part dans
        l'ancienne session et le nouveau lot y est noté : pas de nouvelle
        tentative à chaque tour (l'alerte « session trop grosse » veille).
        """
        if not spec:
            return spec
        lot = spec.get("lot")
        if not self.lot_rotation_due(lot):
            return spec
        courant = (self.agent or {}).get("session_work_item")
        if spec.get("kind") == "prompt":
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
        ok = self._rotate("changement de lot %s → %s" % (courant, lot))
        if not ok:
            if storage.of(self.db).operations.set_session_work_item(
                    self.name, self.runner.runner_id, self.epoch, str(lot)):
                self.agent["session_work_item"] = str(lot)
        if spec.get("kind") == "prompt":
            return self.pick()
        return spec

    # -- dossier de travail déplacé (0018) ---------------------------------
    #: profondeur maximale de recherche d'un dossier de travail déplacé :
    #: `~/development/manaty/ameesh/.claude/worktrees/<nom>` est à cinq niveaux.
    WORKTREE_DEPTH = 6
    #: dossiers cachés qu'on traverse quand même (worktrees imbriqués)
    WORKTREE_HIDDEN = (".claude", ".worktrees")
    #: répertoires jamais traversés (dépendances, caches, sorties de build)
    WORKTREE_SKIP = ("node_modules", ".git", "venv", ".venv", "target", "build",
                     "dist", "__pycache__", ".cache", ".local")

    def _worktree_dirs(self, racine: str, ancien: str | None):
        """Dossiers candidats sous une racine, profondeur bornée (0018 B4).

        On ne descend pas dans les grands répertoires de dépendances ni dans
        `.git`, mais on traverse `.claude` / `.worktrees` : les worktrees
        imbriqués (ex. `~/development/manaty/ameesh/.claude/worktrees/<nom>`)
        doivent être trouvés, ou l'adoption refuse proprement.
        """
        base = racine.rstrip(os.sep).count(os.sep)
        for dossier, sous_dirs, _ in os.walk(racine):
            sous_dirs[:] = [d for d in sous_dirs
                            if d not in self.WORKTREE_SKIP
                            and (not d.startswith(".") or d in self.WORKTREE_HIDDEN)]
            if dossier.count(os.sep) - base >= self.WORKTREE_DEPTH:
                sous_dirs[:] = []
            if dossier != ancien and os.path.exists(os.path.join(dossier, ".git")):
                yield dossier

    def adopt_moved_worktree(self) -> str | None:
        """Retrouve un dossier de travail déplacé, borné aux racines (0018).

        Le marqueur git (common dir + branche) est écrit à l'inscription ; si le
        cwd a disparu, on cherche un candidat **unique** sous les racines
        configurées, en profondeur bornée, et on refuse un dossier déjà utilisé
        par un autre agent.
        """
        marqueur: dict = {}
        try:
            with open(self._path("worktree.json"), encoding="utf-8") as fh:
                marqueur = json.load(fh) or {}
        except (OSError, ValueError):
            return None
        common = marqueur.get("git_common_dir")
        if not common:
            return None
        branche = marqueur.get("branch") or ""
        ancien = self.agent.get("cwd")
        candidats = []
        for racine in self.runner.worktree_roots:
            if not os.path.isdir(racine):
                continue
            for chemin in self._worktree_dirs(racine, ancien):
                ident = worktree_marker(chemin)
                if not ident or ident.get("git_common_dir") != common:
                    continue
                if branche and ident.get("branch") != branche:
                    continue
                candidats.append(chemin)
        if len(candidats) != 1:
            if len(candidats) > 1:
                log("[%s] dossier déplacé ambigu : %s" % (self.name, ", ".join(candidats)))
            return None
        candidat = candidats[0]
        if registry.cwd_used(self.db, candidat, self.name):
            log("[%s] dossier déplacé %s déjà utilisé par un autre agent"
                % (self.name, candidat))
            return None
        registry.upsert(self.db, self.name, cwd=candidat)
        self.agent["cwd"] = candidat
        log("[%s] dossier de travail déplacé adopté : %s" % (self.name, candidat))
        self._fil_note("Dossier de travail déplacé adopté : %s" % candidat,
                       meta={"action": "deplacement", "cwd": candidat})
        return candidat

    # -- panne de base (L72) -------------------------------------------------
    #: statut et préfixe d'erreur du blocage « base injoignable » ; levé
    #: seulement s'il porte exactement ces deux marques (comme L35)
    DB_STATUS = "base injoignable"
    DB_ERROR = "base injoignable"

    def _marque_base(self, agent: dict | None) -> bool:
        agent = agent or {}
        return (agent.get("status") == "blocked"
                and agent.get("status_text") == self.DB_STATUS
                and str(agent.get("last_error") or "").startswith(self.DB_ERROR))

    def _panne_de_base(self, exc: BaseException, ou: str) -> None:
        """Base injoignable entre deux tours : journal, statut si possible,
        attente croissante. Aucun tour ne part ; le bail reste défendu par
        `renew()` à chaque passage de la boucle et par le veilleur."""
        attente = self._reprise.echec()
        log_async("[%s] %s pendant %s — tours suspendus, nouvel essai dans %ds "
                  "(échec %d, panne depuis %ds)"
                  % (self.name, db_mod.explain(exc), ou, int(attente),
                     self._reprise.echecs, int(self._reprise.duree())))
        if not self._base_marquee:
            # Une panne partielle (une requête sur deux) laisse ce statut
            # visible dans `ameesh show`/`list` ; une panne franche l'empêche
            # d'être écrit, et rien n'est perdu : il est retenté au prochain
            # échec, puis levé au retour de la base.
            try:
                issue = registry.set_marked_block(
                    self.db, self.name, self.runner.runner_id, self.epoch,
                    self.DB_STATUS, "%s : %s" % (self.DB_ERROR, db_mod.explain(exc)),
                    self.DB_ERROR)
                self._base_marquee = issue in ("done", "kept")
                if issue == "lease":
                    self.lease_lost.set()
            except db_mod.DbError:
                pass
        self.wake.wait(timeout=self.wait_timeout(attente))
        self.wake.clear()

    def _base_retablie(self) -> None:
        bilan = self._reprise.retablie()
        if bilan:
            log_async("[%s] base de nouveau joignable après %ds (%d échec(s)) : reprise"
                      % (self.name, int(bilan[1]), bilan[0]))

    def _avec_reprise(self, quoi: str, fn, *args, **kwargs):
        """Appel de base réessayé pendant une panne, tant que le bail court (L72).

        Pour l'après-tour : le harnais a fini, son issue est en mémoire et doit
        être écrite (fin de tour, consigne remise, grand livre) plutôt que
        perdue. Lève `Unavailable` si le bail est perdu, échu, ou l'exécuteur
        arrêté : la réclamation suivante remettra la consigne en attente (R5).
        """
        reprise = Reprise(self.runner.db_retry_max)
        while True:
            try:
                resultat = fn(*args, **kwargs)
            except db_mod.Unavailable as exc:
                attente = reprise.echec()
                if (self.lease_lost.is_set() or self.deadline_passed()
                        or self.runner.stop.is_set()):
                    raise
                log_async("[%s] %s : %s — nouvel essai dans %ds (échec %d)"
                          % (self.name, quoi, db_mod.explain(exc), int(attente),
                             reprise.echecs))
                self.runner.stop.wait(self.wait_timeout(attente))
                if not self.renew() or self.lease_lost.is_set():
                    raise
                continue
            bilan = reprise.retablie()
            if bilan:
                log_async("[%s] %s : base de nouveau joignable après %d échec(s)"
                          % (self.name, quoi, bilan[0]))
            return resultat

    def _restaure_consigne(self) -> None:
        """Un tour interrompu par une panne avant le lancement du harnais : sa
        consigne (déjà consommée) repart en attente au retour de la base, et un
        statut `running` resté en plan est remplacé par le blocage « base
        injoignable », que `_pick` lève aussitôt (idle ou queued)."""
        registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
        self._consigne_a_restaurer = False
        try:
            agent = registry.get(self.db, self.name) or {}
            if agent.get("status") == "running":
                registry.set_marked_block(
                    self.db, self.name, self.runner.runner_id, self.epoch,
                    self.DB_STATUS, "%s : tour non lancé" % self.DB_ERROR, self.DB_ERROR)
        except db_mod.Unavailable:
            raise
        except db_mod.DbError:
            pass

    def _enregistre_session(self) -> None:
        """Session ouverte par le harnais pendant une panne : enregistrée au
        registre dès que possible (sinon le tour suivant reprendrait l'ancienne,
        `current_session` préférant le registre au fichier local)."""
        session, compte = self._session_en_attente
        registry.set_session(self.db, self.name, session, compte)
        self._session_en_attente = None
        if self.agent is not None:
            self.agent["session_id"] = session
            self.agent["session_account"] = compte
        log_async("[%s] session %s enregistrée au registre (différée par une panne)"
                  % (self.name, session))

    # -- dossier de travail absent (L35) -------------------------------------
    #: statut et préfixe d'erreur d'un blocage « dossier absent » (le statut
    #: n'est levé que s'il porte exactement ces deux marques)
    WORKDIR_STATUS = "dossier absent"
    WORKDIR_ERROR = "dossier de travail absent"
    #: attente croissante entre deux essais complets (adoption d'un dossier
    #: déplacé comprise) : 5 s, 10 s, 20 s… bornée à 5 min, par agent
    WORKDIR_BACKOFF_MIN = 5.0
    WORKDIR_BACKOFF_MAX = 300.0

    def _resolve_workdir(self, adopt: bool = True) -> str | None:
        """Le dossier de travail utilisable, ou None.

        Le `cwd` est celui de la ligne du registre relue par `pick()` (jamais
        un cache de l'inscription) ; à défaut, adoption bornée d'un worktree
        déplacé (0018), seulement si `adopt`."""
        cwd = self.agent.get("cwd")
        if cwd and os.path.isdir(cwd):
            return cwd
        return self.adopt_moved_worktree() if adopt else None

    def _workdir_missing(self, force_status: bool = False) -> bool:
        """Note un dossier absent (L35) ; faux si le bail n'est plus le nôtre.

        Au CHANGEMENT d'état (nouveau blocage ou autre `cwd`), ou si
        `force_status` (après `begin_turn`), le blocage est posé par une
        transition atomique fencée par le bail : jamais par-dessus un arrêt ou
        un autre blocage, jamais sous un bail perdu. L'état local et le journal
        suivent l'issue de la transition ; hors changement, seule l'attente
        double, bornée."""
        cwd = self.agent.get("cwd")
        nouveau = not self._wd_blocked or cwd != self._wd_seen
        if nouveau or force_status:
            message = "%s : %r" % (self.WORKDIR_ERROR, cwd)
            issue = registry.set_marked_block(
                self.db, self.name, self.runner.runner_id, self.epoch,
                self.WORKDIR_STATUS, message, self.WORKDIR_ERROR)
            if issue == "lease":
                log("[%s] %s — blocage non posé : bail perdu ou remplacé"
                    % (self.name, message))
                self.lease_lost.set()
                return False
        if nouveau:
            self._wd_delay = self.WORKDIR_BACKOFF_MIN
            log("[%s] %s — tours suspendus ; nouvel essai dans %ds au plus tôt (attente "
                "croissante, %ds au plus), immédiat si le registre change%s"
                % (self.name, message, self.WORKDIR_BACKOFF_MIN, self.WORKDIR_BACKOFF_MAX,
                   "" if issue == "done" else " (statut concurrent préservé)"))
        else:
            self._wd_delay = min(self.WORKDIR_BACKOFF_MAX,
                                 max(self.WORKDIR_BACKOFF_MIN, self._wd_delay * 2))
        self._wd_blocked = True
        self._wd_seen = cwd
        self._wd_next = time.monotonic() + self._wd_delay
        return True

    def _workdir_back(self, cwd: str) -> bool:
        """Le dossier est revenu (L35) : levée atomique fencée par le bail,
        PUIS état local et journal. Vrai seulement si la levée a eu lieu
        (`done`) : sur `lease` (bail perdu) comme sur `kept` (arrêt ou autre
        statut écrit entre-temps, conservé), faux — rien n'est consommé dans
        ce sondage."""
        issue = registry.clear_marked_block(
            self.db, self.name, self.runner.runner_id, self.epoch,
            self.WORKDIR_STATUS, self.WORKDIR_ERROR)
        if issue == "lease":
            if not self.lease_lost.is_set():
                log("[%s] dossier de travail retrouvé (%s) mais bail perdu ou remplacé : "
                    "pas de reprise ici" % (self.name, cwd))
            self.lease_lost.set()
            return False
        self._wd_blocked = False
        self._wd_seen = cwd
        self._wd_delay = 0.0
        self._wd_next = 0.0
        if issue == "kept":
            # Bail vivant mais notre blocage n'est plus là : un arrêt ou un autre
            # statut a été écrit entre la lecture du registre et la levée. Il
            # est conservé, et PAS de reprise dans ce sondage (rien consommé) :
            # le sondage suivant relit la ligne et repasse par toutes les gardes
            # de `pick()` (arrêt, budget, pression). L'état local se cale sur
            # le statut conservé (le blocage « dossier absent » est fini).
            row = registry.get(self.db, self.name)
            if row is not None:
                self.agent = row
            log("[%s] dossier de travail retrouvé : %s — statut concurrent conservé "
                "(%s%s), pas de reprise dans ce sondage"
                % (self.name, cwd, (row or {}).get("status") or "?",
                   " : %s" % row["status_text"] if row and row.get("status_text") else ""))
            return False
        log("[%s] dossier de travail retrouvé : %s — reprise" % (self.name, cwd))
        return True

    def workdir_ready(self) -> bool:
        """Garde de `pick()` pendant un blocage « dossier absent » (L35).

        Hors blocage : vrai (la détection a lieu au lancement d'un tour). En
        blocage : contrôle gratuit du `cwd` du registre à chaque sondage — un
        `canon sync` qui le corrige, ou le dossier recréé, débloque au sondage
        suivant ; l'essai complet (adoption d'un worktree déplacé) attend son
        échéance, sauf si la ligne du registre a changé. Rien n'est consommé
        (consigne, courrier) tant que le dossier manque."""
        if self.runner.dry_run or not self._wd_blocked:
            return True
        change = self.agent.get("cwd") != self._wd_seen
        du = change or time.monotonic() >= self._wd_next
        cwd = self._resolve_workdir(adopt=du)
        if cwd:
            return self._workdir_back(cwd)
        if du:
            self._workdir_missing()
        return False

    def interrupt_allowed(self, row: dict) -> bool:
        """Un message ne préempte que s'il vient d'un expéditeur habilité (0018).

        Sans canon (L2), `AMEESH_INTERRUPT_SENDERS` est la seule autorisation ;
        la capacité « interrupt » d'une fiche canon s'ajoutera ici. Un urgent
        non habilité est remis comme un message normal (et l'abus est journalisé).
        """
        sender = (row.get("sender") or row.get("from") or "").strip()
        return bool(sender) and sender in self.cfg.interrupt_senders

    # -- garde de budget (L13, 0019, R20) ----------------------------------
    def budget_reason(self) -> str:
        """Raison de pause budget pour cet agent, ou '' (jauges + dépense).

        Le harnais est passé au `CostBook` (l'état local ne porte pas toujours
        `tool`) ; les plafonds sont ceux du mesh, relus en base à chaud (L70 :
        base > configuration de l'hôte > défaut), plus ceux de l'agent.
        """
        limites = self.runner.budget_limits(self.db)
        if limites.per_hour <= 0:
            return ""
        book = cost_mod.CostBook(
            state_dir=self.cfg.state_dir, db=self.db,
            tools={self.name: self.agent.get("harness") or ""},
            **limites.book_kwargs())
        # L30 (0027) : avec des comptes déclarés, le rythme se juge compte par
        # compte et la garde bascule au lieu de mettre en pause.
        raison = account_turn.choose(self, book)
        if raison is None:
            return book.over(self.name)
        return raison or book.over(self.name, pace=False)

    def budget_ok(self) -> bool:
        """La garde de budget autorise-t-elle un tour ? Pose l'état de pause.

        La **décision** est fraîche à chaque appel : un tour qui vient de
        franchir le plafond ne doit pas passer grâce à un cache (verdict codex3
        L13 B3). Seuls les effets de bord (statut, trace au fil) sont cadencés
        par `budget_check_interval`, pour ne pas écrire à chaque sondage.

        Si la comptabilité d'un tour a échoué (`_compta_en_echec`), les tours
        suivants sont refusés : sans grand livre, l'exécuteur dépenserait en
        aveugle (verdict L13 B1).
        """
        self._account = None
        if self.runner.budget_limits(self.db).per_hour <= 0:
            account_turn.choose(self, None, pause=False)  # L30 : bascule sans pause
            return True
        # Réparation d'abord : un travail comptable en attente (marqueur
        # persistant) doit être écrit avant d'autoriser le moindre tour ; un
        # marqueur corrompu suspend au lieu d'autoriser.
        self._compta_repare()
        raison = ("comptabilité des tours indisponible" if self._compta_en_echec
                  else self.budget_reason())
        self._budget_paused = bool(raison)
        if not raison:
            self._budget_reason = ""
            self._leve_pause_budget()
            return True
        maintenant = time.monotonic()
        if maintenant >= self._budget_next_check:
            self._budget_next_check = maintenant + self.runner.budget_check_interval
            # `paused` n'est pas dans la contrainte de statut (L13 n'a pas de
            # migration) : la pause se lit `blocked` + raison dans `status_text`.
            registry.set_status(self.db, self.name, "blocked",
                                status_text="budget : %s" % raison)
            if raison != self._budget_reason:
                self._budget_reason = raison
                self._fil_note("Pause budget : %s. Aucun tour tant que la jauge ne "
                               "redescend pas." % raison,
                               meta={"action": "budget", "raison": raison})
        return False

    def _leve_pause_budget(self) -> None:
        """La garde laisse passer : une pause budget encore affichée est levée
        (L72). Sans cela, un agent mis en pause (par exemple quand l'état des
        comptes était illisible pendant une panne de base) restait `blocked`
        jusqu'à son prochain tour, même la jauge redescendue."""
        agent = self.agent or {}
        texte = agent.get("status_text") or ""
        if agent.get("status") != "blocked" or not texte.startswith("budget : "):
            return
        issue = registry.clear_marked_block(self.db, self.name, self.runner.runner_id,
                                            self.epoch, texte, "")
        if issue == "done":
            self.agent = dict(agent, status="idle", status_text="")
            log_async("[%s] pause budget levée (%s)" % (self.name, texte))

    def _state_read(self, key: str) -> str:
        """Lit une valeur d'état locale (`model`, `effort`) ; '' si absente."""
        try:
            with open(os.path.join(self.state_dir, key), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def _compta_en_attente(self) -> dict | None:
        """Le marqueur comptable **en base** (L13 B5, arbitrage mesh-design).

        L'état de pause vit dans `spend_pending`, jamais dans un fichier ni en
        mémoire : une ligne présente, illisible ou incohérente suspend, elle
        n'autorise jamais. Une erreur de base renvoie `{}` (doute = suspension).
        """
        try:
            return registry.pending_spend_get(self.db, self.name)
        except db_mod.Unavailable:
            # L72 : base injoignable — ni marqueur ni doute à mémoriser : la
            # panne suspend déjà les tours, et un `{}` ici rendait la
            # suspension définitive jusqu'au redémarrage du worker.
            raise
        except db_mod.DbError:
            return {}

    def _compta_marque(self, start: int, tour: str, model: str | None) -> bool:
        """Pose le marqueur **avant** le tour, atomiquement en base (L13 B5).

        Si la pose échoue, le tour ne démarre pas : aucune dépense sans trace
        possible (fail-closed).
        """
        self._annonce_ram = ""  # un tour neuf n'a encore rien annoncé
        try:
            registry.pending_spend_put(self.db, self.name, int(start), tour,
                                       model if model is not None else "")
            return True
        except db_mod.Unavailable:
            raise  # L72 : rien lancé ; la consigne est remise au retour de la base
        except db_mod.DbError as exc:
            self._compta_en_echec = True
            log_async("[%s] marqueur de comptabilité non posé (%s) : tour refusé"
                      % (self.name, " ".join(str(exc).split())[:160]))
            return False

    def _compta_ecrit(self, travail: dict) -> bool:
        """Écrit une ligne du grand livre, puis efface le marqueur en base.

        Rejouable après panne : le marqueur n'est effacé qu'après une écriture
        réussie. Si l'effacement lui-même échoue, la réparation suivante peut
        écrire une ligne en double — jamais une dépense perdue en silence ; le
        contraire (autoriser un tour sans trace) est ce qu'on refuse.
        """
        try:
            debut = int(travail.get("start"))
            if debut < 0:
                raise ValueError("index négatif")
        except (TypeError, ValueError):
            self._compta_en_echec = True
            return False
        try:
            book = cost_mod.CostBook(
                state_dir=self.cfg.state_dir, db=self.db,
                tools={self.name: self.agent.get("harness") or ""})
            # Le modèle **du tour** se relit dans les événements du tour, qui sont
            # sur disque : ni le marqueur (modèle du lancement, ou annonce non
            # persistée), ni la mémoire d'un worker mort ne font foi quand le flux
            # a dit autre chose (L13 B4 : double panne puis reprise par un NOUVEAU
            # worker). Ordre : flux du tour > mémoire de ce worker > marqueur.
            modele = travail.get("model")
            annonce = self._annonce_des_evenements(debut)
            if annonce:
                modele = annonce
            # Clé du marqueur (L60) : la même pour l'écriture de fin de tour et
            # pour sa réparation, qui ne double donc plus la ligne.
            cree = travail.get("created_ts")
            cle = ("%s:%d:%.6f" % (self.name, debut, float(cree))
                   if cree is not None else None)
            usage = book.record(self.name, start=debut, key=cle,
                                turn=travail.get("turn") or None,
                                session=travail.get("session") or None,
                                # `""` = modèle inconnu : tarif le plus cher
                                # (fail-closed), jamais un repli sur un état
                                # local mutable.
                                model=modele)
            # Mesure du plafond de contexte (L60), lue sur la ligne écrite.
            self.last_turn_reread = usage.reread_tokens
            self.last_turn_reread_session = usage.session
            registry.pending_spend_clear(self.db, self.name)
            self._annonce_ram = ""
            self._compta_en_echec = False
            return True
        except Exception as exc:  # panne non fatale au tour, mais fail-closed
            self._compta_en_echec = True
            log_async("[%s] comptabilité du tour en échec (%s) : tours suspendus"
                      % (self.name, " ".join(str(exc).split())[:160]))
            return False

    def _compta_repare(self) -> None:
        """Avant un tour : rejoue le travail en attente ; `{}` suspend."""
        travail = self._compta_en_attente()
        if travail is None and self._compta_doute:
            # L72 : la suspension venait d'une base injoignable à la création
            # du worker ; aucun marqueur n'attend, rien à réparer.
            self._compta_doute = False
            self._compta_en_echec = False
        if travail:
            if self._annonce_ram:
                # Ce worker a vu l'annonce du flux ; si la base ne l'a pas gardée,
                # la mémoire fait foi (jamais le modèle du lancement).
                travail = dict(travail, model=self._annonce_ram)
            self._compta_ecrit(travail)
        elif travail is not None:
            self._compta_en_echec = True  # marqueur incohérent : jamais autoriser

    def _annonce_des_evenements(self, debut: int) -> str:
        """Le dernier modèle annoncé par le flux **à partir de l'index du tour**.

        Relu avec l'adaptateur du harnais, comme la boucle de lecture : les
        événements sont écrits ligne à ligne avant d'être analysés, donc ils
        survivent à un worker mort. Chaîne vide si le flux n'a rien annoncé ou
        s'il est illisible : l'appelant garde alors le modèle qu'il a. Lecteur
        seul : la comptabilité ne dépend pas de la présence du binaire.
        """
        modele = ""
        try:
            adapter = adapters.adapter_for(self.agent.get("harness") or "other",
                                           resolve=False)
            with open(self._path("events.jsonl"), encoding="utf-8") as flux:
                for numero, ligne in enumerate(flux):
                    if numero < debut:
                        continue
                    ligne = ligne.rstrip("\n")
                    if not ligne:
                        continue
                    annonce = self._parse(adapter, ligne).get("model")
                    if annonce:
                        modele = annonce
        except (OSError, ValueError, adapters.HarnessMissing):
            return ""
        return modele

    def _compta_annonce(self, modele: str) -> None:
        """Garde le modèle annoncé par le flux : mémoire d'abord, puis base (L13 B4).

        Persisté **dès sa réception** dans `spend_pending.model` : si la ligne du
        grand livre ne peut pas s'écrire, la réparation (ce worker, ou un autre
        après un redémarrage) facture le modèle réellement utilisé. Un échec de
        persistance — erreur de base, ou marqueur absent — suspend les tours
        (fail-closed) ; la mémoire garde l'annonce pour la réparation de ce worker.
        """
        self._annonce_ram = modele
        try:
            if not registry.pending_spend_set_model(self.db, self.name, modele):
                raise db_mod.DbError("aucun marqueur comptable à mettre à jour")
        except db_mod.DbError as exc:
            self._compta_en_echec = True
            log_async("[%s] modèle annoncé non persisté (%s) : tours suspendus"
                      % (self.name, " ".join(str(exc).split())[:120]))

    def _compta_termine(self, model_annonce: str | None = None) -> None:
        """Après un tour : écrit la ligne du grand livre.

        Le modèle **annoncé par le flux** du harnais prime sur celui figé au
        lancement : c'est la source du tour (mesh-design, 0019 §3).
        """
        # L72 : le tour a eu lieu ; sa ligne de grand livre attend la base
        # plutôt que d'être remise à une réparation ultérieure.
        travail = self._avec_reprise("comptabilité du tour", self._compta_en_attente)
        if travail:
            if model_annonce:
                travail = dict(travail, model=model_annonce)
            self._compta_ecrit(travail)
        elif travail is not None:
            self._compta_en_echec = True

    def maybe_relocate(self, pressure: dict) -> bool:
        """Déplace l'agent vers un hôte admis disponible (L31, 0028), si activé.

        Entre deux tours seulement : l'hôte d'exécution change (`relocate`) et
        le worker s'arrête, ce qui rend le bail ; l'exécuteur de l'hôte
        d'arrivée réclamera l'agent. Jamais au milieu d'un tour ; ne lève
        jamais."""
        if not self.runner.relocate or getattr(self.runner, "mediated", False):
            # L109 : le déplacement entre hôtes passe au serveur en mode médié
            return False
        from . import relocation as relocation_mod
        try:
            plan = relocation_mod.plan(
                self.agent.get("admitted_hosts") or [], self.runner.host, db=self.db,
                current_limits=(pressure or {}).get("limits"),
                limits_for=self.runner.host_limits_for,
                shared_sessions=self.runner.shared_sessions)
            if plan is None:
                return False
            result = relocation_mod.move(self.db, self.agent, plan,
                                         current_host=self.runner.host,
                                         owner=self.runner.runner_id, epoch=self.epoch)
        except db_mod.DbError as exc:
            log("[%s] déplacement impossible (%s)" % (self.name, exc))
            return False
        if result is None:
            return False
        log("[%s] déplacement vers %s : %s (session %s)" % (
            self.name, result["target"], result["reason"],
            "conservée" if result["keep_session"] else "tournée avec résumé"))
        self._fil_note(
            "Déplacement vers %s : %s. %s" % (
                result["target"], result["reason"],
                "La session est reprise (stockage partagé)." if result["keep_session"]
                else "La session n'est pas portable : reprise sur un résumé en tête de "
                     "consigne."),
            meta={"action": "deplacement", "vers": result["target"]})
        self.stopping.set()
        self.wake.set()
        return True

    def _host_pressure_note(self, pressure: dict) -> None:
        """Trace la contre-pression (L31, 0028) ; en critique, met en pause.

        Ne touche JAMAIS un tour en cours : `_pick` s'exécute entre deux tours.
        Seuls les agents de priorité la plus basse (0, le défaut) sont mis en
        pause ; les autres attendent simplement que la pression redescende."""
        if not pressure.get("critical"):
            return
        if int(self.agent.get("priority") or 0) > 0:
            return
        raisons = " ; ".join("%s %s (seuil %s)" % (b["label"], b["value"], b["limit"])
                              for b in pressure.get("breaches") or [])
        texte = "pression critique de l'hôte %s : %s" % (self.runner.host, raisons or "seuil")
        if getattr(self, "_pressure_reason", "") == texte:
            return
        # Pause fencée par le bail : un worker périmé (bail perdu, remplacé,
        # expiré) ou un tour en cours est refusé (L31, 0028).
        if not registry.pause(self.db, self.name, self.runner.runner_id, self.epoch,
                              "pression hôte"):
            log("[%s] pause refusée : bail perdu, remplacé, ou tour en cours" % self.name)
            return
        self._pressure_reason = texte
        self._fil_note("Pause : %s. Aucun nouveau tour tant que la pression ne "
                       "redescend pas ; le tour en cours n'est jamais interrompu." % texte,
                       meta={"action": "pression-hote", "hote": self.runner.host})

    def pick(self) -> dict | None:
        """Le prochain tour à faire, ou None. Consomme la consigne en attente.

        Le tour porte son lot (`lot`, L26) quand il est déductible."""
        spec = self._pick()
        if spec is not None:
            spec["lot"] = self.turn_lot(spec)
        return spec

    def _pick(self) -> dict | None:
        agent = registry.get(self.db, self.name)
        if agent is None or agent.get("status") == "stopped":
            if agent is not None:
                log("[%s] arrêté dans le registre" % self.name)
            self.stopping.set()
            return None
        self.agent = agent
        if self._session_en_attente is not None:
            self._enregistre_session()
        if self._marque_base(agent):
            # L72 : la base répond de nouveau — le blocage « base injoignable »
            # (posé par cet exécuteur ou hérité d'un précédent) est levé ici,
            # sans reprise manuelle.
            issue = registry.clear_marked_block(
                self.db, self.name, self.runner.runner_id, self.epoch,
                self.DB_STATUS, self.DB_ERROR)
            if issue == "lease":
                self.lease_lost.set()
                return None
            if issue == "done":
                log_async("[%s] base de nouveau joignable : blocage « %s » levé"
                          % (self.name, self.DB_STATUS))
            self._base_marquee = False
            self.agent = agent = registry.get(self.db, self.name) or agent
        if not self.budget_ok():
            return None  # garde de budget (L13) : aucun tour, état `paused` posé
        # Contre-pression de l'hôte (L31, 0028) : au-dessus d'un seuil, aucun
        # NOUVEAU tour ne démarre ; la consigne en attente n'est pas consommée.
        pressure = self.runner.host_pressure()
        if pressure.get("blocked"):
            if self.maybe_relocate(pressure):
                return None  # déplacé : le worker s'arrête, le bail sera rendu
            self._host_pressure_note(pressure)
            return None
        # Dossier de travail absent (L35) : ni consigne ni courrier consommés,
        # pas de tour tenté à chaque sondage.
        if not self.workdir_ready():
            return None
        # L30 : session ouverte sous un autre compte et non reprenable → résumé
        # sous l'ancien compte, avant de consommer quoi que ce soit (jamais en
        # plein tour).
        account_turn.continuity(self)
        urgents = mail.unread_urgent(self.db, self.name)
        autorises = [m for m in urgents if self.interrupt_allowed(m)]
        for refuse in (m for m in urgents if m not in autorises):
            mid = int(refuse.get("id") or 0)
            if mid and mid not in self._refus_vus:
                self._refus_vus.add(mid)
                log("[%s] urgent ignoré : expéditeur %s non habilité à interrompre"
                    % (self.name, refuse.get("sender") or "?"))
        if autorises:
            # Le prioritaire passe devant la consigne en attente : c'est lui qui
            # ouvre le tour, la consigne interrompue suivra (0018).
            self.nudged = False
            return self._courrier_spec("urgent", autorises)
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
        events = [m for m in messages if mail.is_event(m)]
        autres = [m for m in messages if not mail.is_event(m)]
        if autres:
            if events:
                registry.mark_event_wake(self.db, self.name)
            return self._courrier_spec("mail", messages)
        if not events:
            return None
        fenetre = self.runner.event_coalesce
        dernier = float(self.agent.get("last_event_ts") or 0.0)
        # Seul un urgent d'un expéditeur habilité perce la fenêtre : un urgent
        # non habilité reste soumis au regroupement (même règle que la
        # préemption, 0018).
        if any(mail.is_urgent(m) and self.interrupt_allowed(m) for m in events) \
                or fenetre <= 0 or (time.time() - dernier) >= fenetre:
            registry.mark_event_wake(self.db, self.name)
            return self._courrier_spec("event", events)
        return None  # fenêtre de regroupement en cours : on attend la suivante

    # -- un tour -----------------------------------------------------------
    def _adapter(self) -> adapters.HarnessAdapter:
        harness = self.agent.get("harness") or "other"
        adapter = adapters.adapter_for(harness)
        # Traçabilité de chaîne d'approvisionnement (décision 0021) : chaque
        # descripteur utilisé est journalisé avec sa provenance et son empreinte,
        # une fois par changement (pas à chaque tour).
        descriptor = adapter.descriptor
        signature = (descriptor.path, descriptor.sha256)
        if signature != self.descriptor_seen:
            self.descriptor_seen = signature
            log("[%s] descripteur %s : %s (%s, sha256 %s)"
                % (self.name, descriptor.id, descriptor.path, descriptor.source,
                   (descriptor.sha256 or "?")[:16]))
        return adapter

    #: préfixe du résumé de reprise (session neuve après rotation, 0018)
    RESUME_PREFIX = "Reprise de session après rotation — résumé :\n%s\n\n"
    def _budget_courrier(self) -> int:
        """Octets UTF-8 disponibles pour la consigne d'un tour de courrier.

        Le plafond (`AMEESH_PROMPT_MAIL_MAX`) borne la consigne ENTIÈRE, résumé
        de reprise compris quand il ouvrira le tour ; jamais au-delà de la
        borne sûre d'un argument de processus. Un résumé qui ne laisserait pas
        `PROMPT_MIN_BYTES` au courrier les lui laisse quand même (la consigne
        dépasse alors le plafond, jamais la borne de l'argument).
        """
        resume = mail.octets(self.RESUME_PREFIX % self.resume_summary) \
            if self.resume_summary else 0
        return min(self.runner.prompt_max_bytes, mail.ARG_SAFE_BYTES) - resume

    def _courrier_spec(self, kind: str, messages: list[dict]) -> dict:
        """Un tour de courrier : le CONTENU des messages est dans la consigne.

        Plafond sur le rendu réel (`_budget_courrier`) : les plus anciens
        d'abord ; les autres restent non livrés, mentionnés par leur nombre,
        pour le tour suivant. `candidats` garde tous les messages : `run_turn`
        refait le choix au moment de réserver.
        """
        prompt, retenus, reste = mail.prompt_for(self.db, messages, kind=kind,
                                                 budget=self._budget_courrier())
        return {
            "kind": kind,
            "prompt": prompt,
            "ids": [int(m["id"]) for m in retenus if m.get("id") is not None],
            "rows": retenus,
            "candidats": list(messages),
            "remaining": reste,
        }

    def run_turn(self, spec: dict) -> bool:
        """Un tour. Pour un tour de courrier, protocole de remise unique :
        réservation sous un jeton (registre verrouillé, bail contrôlé), consigne
        rendue sur les messages réservés, remise soldée juste après le lancement
        du harnais (bail toujours exigé), réservation annulée si le tour n'a pas
        pu démarrer.

        Panne ou bail perdu entre réservation et remise : la réservation n'est
        pas soldée ; elle expire ou change d'epoch, le message est remis plus
        tard et signalé « re-livré » — un doublon signalé, jamais une perte.
        """
        self.fast_failure = False
        candidats = spec.get("candidats")
        # Dernier contrôle avant de consommer quoi que ce soit (L31, 0028) :
        # couvre les tours ouverts directement (résumé de rotation de compte).
        pression = self.runner.host_pressure()
        if pression.get("blocked"):
            self._host_pressure_note(pression)
            return False
        if not candidats or self.runner.dry_run:
            return self._run_turn(spec)
        kind = spec["kind"]
        budget = self._budget_courrier()
        _texte, retenus, reste = mail.prompt_for(self.db, candidats, kind=kind,
                                                 budget=budget)
        jeton = mail.new_token()
        try:
            reserves = mail.reserve(
                self.db, self.name, self.runner.runner_id, self.epoch, jeton,
                ids=[int(m["id"]) for m in retenus], porteur="consigne",
                ttl_seconds=mail.RESERVATION_TTL)
        except db_mod.DbError as exc:
            log("[%s] réservation du courrier impossible (%s) : tour reporté"
                % (self.name, exc))
            return False
        if not reserves:
            log("[%s] courrier du tour déjà réservé ou livré ailleurs, ou bail "
                "perdu : tour annulé" % self.name)
            return False
        # La consigne est rendue sur les messages RÉSERVÉS (leur état exact :
        # « re-livré » compris), dans le même budget.
        prompt, inclus, _ = mail.prompt_for(self.db, reserves, kind=kind, budget=budget,
                                            extra_remaining=reste)
        ids = [int(m["id"]) for m in inclus]
        hors = [int(m["id"]) for m in reserves if int(m["id"]) not in ids]
        if hors:  # par construction vide ; jamais réservé sans être montré
            mail.release(self.db, self.name, jeton, hors)
        spec = dict(spec, prompt=prompt, rows=inclus, ids=ids, jeton=jeton)
        self._courrier_lance = False
        try:
            return self._run_turn(spec)
        finally:
            if not self._courrier_lance:
                # Le harnais n'a pas été lancé : rien n'a été vu, rien n'est livré.
                try:
                    mail.release(self.db, self.name, jeton, ids)
                except db_mod.DbError as exc:
                    # La réservation reste jusqu'à son échéance : le message sera
                    # re-livré, signalé (aucune perte).
                    log("[%s] annulation de la réservation impossible (%s)"
                        % (self.name, exc))

    def _livre_courrier(self, spec: dict) -> None:
        """Le harnais est lancé avec la consigne : la réservation est soldée.

        Remise fencée (jeton ET bail courant). Si le bail est perdu, les
        messages restent non livrés et la remise est signalée incertaine : le
        bail suivant les re-livrera en le disant.
        """
        if not spec.get("jeton"):
            return
        self._courrier_lance = True
        ids = list(spec["ids"])
        try:
            livres = mail.deliver(self.db, self.name, self.runner.runner_id, self.epoch,
                                  spec["jeton"], ids)
        except db_mod.DbError as exc:
            livres = []
            log("[%s] remise du courrier non soldée (%s)" % (self.name, exc))
        incertains = sorted(set(ids) - set(livres))
        if incertains:
            log("[%s] remise incertaine : message(s) %s montré(s) au harnais mais non "
                "soldé(s) (bail perdu ou base indisponible) ; ils seront re-livrés, "
                "signalés « re-livré »" % (self.name, ", ".join(map(str, incertains))))
            self._fil_note(
                "Remise incertaine : %d message(s) mis dans la consigne d'un tour "
                "lancé n'ont pas pu être marqués livrés (bail perdu ou base "
                "indisponible). Ils seront remis de nouveau, signalés « re-livré »."
                % len(incertains),
                meta={"action": "remise-incertaine", "messages": incertains})

    def _mediated_env(self, env: dict) -> None:
        """L109 : la session du harnais d'un exécuteur médié parle au serveur
        avec un jeton de session lié au bail (jamais d'accès à la base) :
        `AMEESH_BACKEND=mediated`, `AMEESH_EXEC_URL`, `AMEESH_EXEC_TOKEN`.
        Sans jeton (serveur injoignable), le tour part quand même : seules
        les commandes `ameesh` de la session échoueront."""
        from .executeur_mediee import contrat as exec_contrat
        from .executeur_mediee.interfaces import ENV_SERVER_URL, ENV_SESSION_TOKEN
        for name in ("AMEESH_DSN", "AGENT_MESH_DSN", "AMEESH_DATABASE_URL",
                     "AGENT_MESH_DATABASE_URL", ENV_SESSION_TOKEN):
            env.pop(name, None)
        env["AMEESH_BACKEND"] = env["AGENT_MESH_BACKEND"] = "mediated"
        env[ENV_SERVER_URL] = getattr(self.db, "url", "") or self.cfg.exec_url
        try:
            issued = self.db.session_token(
                exec_contrat.Fence(self.name, self.runner.runner_id, self.epoch))
            env[ENV_SESSION_TOKEN] = issued.token
        except db_mod.DbError as exc:
            log_async("[%s] jeton de session indisponible (%s) : les commandes ameesh de la "
                      "session échoueront" % (self.name, db_mod.explain(exc)))

    def _open_turn_resource(self, turn_id: str, pgid: int | None) -> None:
        """Rattache le groupe du tour (L31, 0028) ; ne casse jamais le tour."""
        self._turn_open = False
        try:
            from . import containers as containers_mod
            storage.of(self.db).turn_resources.open_turn(
                turn_id, self.name, self.runner.host, pgid=pgid,
                label=containers_mod.turn_labels(turn_id, self.name))
            self._turn_open = True
        except Exception as exc:  # ne casse jamais un tour
            log("[%s] rattachement du tour %s impossible (%s)"
                % (self.name, turn_id[:8], exc))

    def _close_turn_resource(self, turn_id: str, pgid: int | None) -> None:
        """Ferme la ressource du tour ; `orphan` si elle survit (L31, 0028).

        Un groupe encore vivant après l'arrêt escaladé, ou un conteneur qui
        porte encore l'étiquette du tour, est signalé orphelin : **jamais
        supprimé** ici."""
        if not getattr(self, "_turn_open", False):
            return
        self._turn_open = False
        conteneurs: list[str] = []
        runtime = self.runner.container_runtime()
        if runtime is not None:
            try:
                conteneurs = runtime.running_for_turn(turn_id)
            except Exception:  # un moteur indisponible n'est pas un orphelin
                conteneurs = []
        orphelin = bool(conteneurs) or bool(pgid and self._group_alive(pgid))
        try:
            storage.of(self.db).turn_resources.close_turn(
                turn_id, orphan=orphelin, containers=conteneurs or None)
        except Exception as exc:  # ne casse jamais la fin d'un tour
            log("[%s] fermeture du rattachement du tour %s impossible (%s)"
                % (self.name, turn_id[:8], exc))
            return
        if orphelin:
            log("[%s] ressource orpheline du tour %s : %s"
                % (self.name, turn_id[:8],
                   ", ".join(conteneurs) if conteneurs else "groupe de processus survivant"))

    def _run_turn(self, spec: dict) -> bool:
        harness = self.agent.get("harness") or "other"
        try:
            adapter = self._adapter()
        except adapters.HarnessMissing as exc:
            log("[%s] %s" % (self.name, exc))
            self.fail_turn("harnais absent", str(exc))
            return False

        compte = account_turn.for_turn(self)  # L30 : compte de ce tour (ou None)
        session = self.current_session()
        resume = ""
        if self.resume_summary and not session:
            # Session neuve après rotation (0018) : le résumé de reprise ouvre le tour.
            resume = self.RESUME_PREFIX % self.resume_summary
        # Modèle et effort par agent (0019) : lus à chaque tour, donc un
        # `ameesh set` prend effet au tour suivant. DeepSeek reçoit un patch YAML.
        model = self.agent.get("model") or self._state_read("model")
        if not model and adapter.spec.model_file and adapter.spec.defaults.get("model"):
            # Modèle livré par fichier (DeepSeek, L60) : sans réglage, le défaut
            # du descripteur est passé explicitement. Le modèle du tour est
            # alors connu (grand livre au bon tarif, jamais « inconnu ») au
            # lieu de dépendre du profil local du harnais.
            model = adapter.spec.defaults["model"]
        effort = self._state_read("effort") or self.agent.get("effort") or ""
        # Tier (L26) : passé par le descripteur du harnais (Codex :
        # `service_tier`) ; sans effet sur un harnais qui n'en déclare pas.
        tier = self._state_read("tier") or self.agent.get("tier") or ""
        patch = self._path("model.patch.yml") if (model or effort) else None
        argv = adapter.command(resume + spec["prompt"], session,
                               model=model or None, effort=effort or None, patch=patch,
                               tier=tier or None)
        label = {"prompt": "consigne", "mail": "messages", "event": "événements",
                 "urgent": "prioritaire", "idle": "reprise"}[spec["kind"]]
        if spec.get("jeton") and mail.octets(resume + spec["prompt"]) > mail.ARG_SAFE_BYTES:
            # Garde-fou : le budget l'interdit ; jamais un E2BIG au lancement.
            self.fail_turn("consigne trop longue", "consigne de %d octets"
                           % mail.octets(resume + spec["prompt"]))
            return False
        if self.runner.dry_run:
            log("[%s] tour %s (dry-run, session %s) : %s"
                % (self.name, label, session or "neuve", " ".join(argv)))
            return True
        if not registry.begin_turn(self.db, self.name, self.runner.runner_id, self.epoch,
                                   "tour %s (%s)" % (label, harness)):
            log("[%s] bail perdu ou agent arrêté avant le tour" % self.name)
            return False
        lot = spec.get("lot")
        if lot and str(lot) != str(self.agent.get("session_work_item") or ""):
            # Le lot de la session courante (0025, L26) : noté au premier tour
            # d'une session neuve, ou après une rotation refusée ; fencé.
            if storage.of(self.db).operations.set_session_work_item(
                    self.name, self.runner.runner_id, self.epoch, str(lot)):
                self.agent["session_work_item"] = str(lot)
        self._note_delegate_turn(spec)

        # Worktree renommé/déplacé (0018) : adoption bornée. Absent : la
        # consigne repart en attente et `pick()` garde l'agent bloqué, avec une
        # attente croissante et un journal au changement d'état (L35).
        cwd = self._resolve_workdir()
        if not cwd:
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            self._workdir_missing(force_status=True)
            return False

        env = os.environ.copy()
        env.update(adapter.env())
        # L31 (0028) : identifiant du tour et étiquettes à poser sur les
        # conteneurs lancés par ses outils, pour repérer les orphelins.
        from . import containers as containers_mod
        turn_id = uuid.uuid4().hex
        self._turn_id = turn_id
        turn_label = containers_mod.turn_labels(turn_id, self.name)
        identite = {
            "AGENT_MAIL_NAME": self.name,
            "AGENT_MAIL_STATE": self.cfg.v0_state,
            "AGENT_MAIL_CONFIG": self.cfg.config_dir,
            "AMEESH_TURN_ID": turn_id,
            containers_mod.LABELS_ENV: turn_label,
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
        if getattr(self.runner, "mediated", False):
            self._mediated_env(env)
        if not account_turn.apply(self, env, compte):
            return False  # profil inutilisable : consigne remise, rien lancé
        # L111 : exécuteur médié (appareil prêté) — URL du relais de modèle et
        # jeton de session court, dans l'environnement du harnais seulement ;
        # sans jeton, pas de tour (jamais de repli sur une clé locale).
        from . import relay as relay_mod
        try:
            relay_mod.apply_turn_env(env, harness, agent=self.name,
                                     owner=self.runner.runner_id, epoch=self.epoch,
                                     turn_id=turn_id)
        except relay_mod.RelayTurnError as exc:
            log("[%s] %s : tour non lancé" % (self.name, exc))
            self.fail_turn("relais de modèle indisponible", str(exc))
            return False
        events_path = self._path("events.jsonl")
        stderr_path = self._path("stderr.log")
        started = time.time()
        if compte is not None:
            # Marqueur de compte AVANT l'index de début : les relevés du tour (et
            # sa ligne de grand livre) sont attribués à ce compte (L30).
            account_turn.mark(self, events_path, compte)
        # L71 : marqueur de tour AVANT l'index de début — la session reprise
        # (vide = neuve). Sans total connu de cette session, `CostBook.record`
        # ne compte pas son cumul entier comme le coût du tour.
        try:
            with open(events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": cost_mod.TURN_MARKER, "resume": session or "",
                                     "ts": time.time(), "agent": self.name},
                                    ensure_ascii=False) + "\n")
        except OSError as exc:
            log("[%s] marqueur de tour non écrit (%s)" % (self.name, exc))
        # Index du flux avant le tour : `CostBook.record` ne compte que les
        # événements nouveaux (L13, 0019 §3).
        events_start = 0
        try:
            with open(events_path, "rb") as flux:
                events_start = sum(1 for _ in flux)
        except OSError:
            events_start = 0
        session_new: str | None = None
        modele_annonce = ""  # modèle annoncé par le flux du harnais (L13 B4)
        cost: float | None = None
        error: str | None = None
        result_code = 0
        self.last_output = ""

        # Marqueur comptable **avant** le lancement : si on ne peut pas garantir
        # une trace, on ne dépense pas (fail-closed, L13 B5). Le marqueur est
        # écrit atomiquement et lu au redémarrage ; la session sera résolue par
        # `record` au moment de l'écriture.
        if not self._compta_marque(events_start, label, model):
            self.fail_turn("comptabilité indisponible",
                           "marqueur de comptabilité non écrit")
            return False

        heartbeat_done = threading.Event()
        threading.Thread(target=self._heartbeat, args=(heartbeat_done,), daemon=True).start()
        preempt_done = threading.Event()
        # Un nouveau tour remet à zéro le latch de publication : il ne concerne
        # que la course du tour précédent, et les autres déclencheurs (bail,
        # arrêt, préemption) sont revérifiés à la publication (verdict B5).
        self.stop_requested.clear()
        if spec["kind"] == "urgent":
            # Ce tour sert justement le message prioritaire : le signal est consommé.
            self.preempting.clear()
        threading.Thread(target=self._preempt_monitor, args=(preempt_done,),
                         daemon=True).start()
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
            # L31 (0028) : rattache le groupe (et l'étiquette des conteneurs) au
            # tour, pour repérer ce qui survivrait à sa fin.
            self._open_turn_resource(turn_id, proc.pid)
            # Lancé (et publié, pour que l'arrêt le trouve) : les messages de la
            # consigne sont livrés, et seulement eux.
            self._livre_courrier(spec)
            # Course de publication : un déclencheur d'arrêt (bail perdu,
            # préemption, arrêt de l'exécuteur, échéance) a pu conclure pendant
            # que ce Popen n'était pas encore publié (proc=None). Le recontrôle
            # unique couvre toutes les causes (B5a-P, L11 B2).
            tue = self.kill_if_stop_requested()
            if resume and not tue:
                self.resume_summary = ""  # le résumé a ouvert la session neuve
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
                        # L39 (0030) : le compte d'origine de la session, en
                        # registre avec son id (le marqueur du flux reste la
                        # source des relevés du tour)
                        try:
                            registry.set_session(
                                self.db, self.name, session_new,
                                compte.name if compte is not None else None)
                        except db_mod.DbError as exc:
                            # L72 : une panne de base ne tue pas le tour (le
                            # bail, lui, est défendu par le battement) ;
                            # l'enregistrement est rejoué après le tour.
                            self._session_en_attente = (
                                session_new, compte.name if compte is not None else None)
                            log_async("[%s] session %s non enregistrée (%s) : rejouée "
                                      "après le tour" % (self.name, session_new,
                                                         db_mod.explain(exc)))
                        if self.agent is not None:
                            self.agent["session_account"] = (
                                compte.name if compte is not None else None)
                    if parsed.get("model") and parsed["model"] != modele_annonce:
                        modele_annonce = parsed["model"]  # source du tour (L13 B4)
                        self._compta_annonce(modele_annonce)  # persisté tout de suite
                    for text in parsed.get("display") or []:
                        log("[%s] %s" % (self.name, text))
                        self.last_output = (self.last_output + "\n" + text)[-4000:]
                    usage = parsed.get("usage")
                    if isinstance(usage, dict):
                        # Clés de chaque harnais normalisées (L60 : l'usage
                        # par étape de DeepSeek, `inputTokens`, comptait zéro).
                        entree, _cache, sortie = adapters.usage_tokens(usage)
                        self.session_tokens += float(entree + sortie)
                    if parsed.get("cost") is not None:
                        cost = parsed["cost"]
                    if parsed.get("error"):
                        error = str(parsed["error"])
                    if self.lease_lost.is_set():
                        error = "bail perdu en cours de tour"
                        self.stop_group_now("bail perdu en cours de tour", grace=0, hard=True)
                        break
            result_code = proc.wait()
        finally:
            heartbeat_done.set()
            preempt_done.set()
            pgid = self.pgid
            # Arrêt escaladé (SIGTERM puis SIGKILL) : un `Popen.terminate()`
            # sec laisserait vivre un harnais qui ignore SIGTERM, hors de portée
            # du veilleur une fois `self.proc` effacé (sonde codex3 B5a-S).
            self.terminate()
            with self.lock:
                self.proc = None
                self.pgid = None
            self._close_turn_resource(turn_id, pgid)

        duration = time.time() - started
        self.last_turn_seconds = duration
        lost = self.lease_lost.is_set()
        preempte = self.preempting.is_set()
        # Comptabilité **avant** toute sortie anticipée : la dépense a eu lieu,
        # même si le bail a été perdu ou le tour préempté (L13 B1). Le modèle
        # est celui du lancement, déjà figé dans le marqueur (L13 B4).
        self._compta_termine(modele_annonce)
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
            self._avec_reprise("consigne remise en attente", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            return False
        if self.restarting.is_set() and not preempte and not ok:
            # `ameesh restart` (L26) : le tour est arrêté ; sa consigne repart en
            # attente, DERRIÈRE le brief que la demande placera en tête.
            self._avec_reprise("consigne remise en attente", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            self._avec_reprise("statut de fin de tour", registry.set_status,
                               self.db, self.name, "queued",
                               status_text="tour arrêté : redémarrage demandé")
            return False
        if preempte:
            # Message prioritaire reçu pendant le tour (0018) : la consigne
            # repart en attente, `pick()` servira le message en tête au tour
            # suivant, sur la même session. Le fil garde la trace.
            self._avec_reprise("consigne remise en attente", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            self._avec_reprise("statut de fin de tour", registry.set_status,
                               self.db, self.name, "queued",
                               status_text="tour interrompu : message prioritaire")
            self._fil_note(
                "Tour interrompu par un message prioritaire ; la consigne du tour "
                "repart en attente et le message prioritaire passe en tête.",
                meta={"action": "interruption", "tour_s": round(duration, 1)})
            return False
        if not ok:
            # B6a : le tour n'a pas abouti, la consigne repart en attente au lieu
            # d'être effacée par end_turn. Au pire elle est rejouée.
            self._avec_reprise("consigne remise en attente", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            self.fast_failure = duration < self.runner.fast_failure_s
        if self._session_en_attente is not None:
            self._avec_reprise("session du tour", self._enregistre_session)
        self._avec_reprise(
            "fin de tour", registry.end_turn,
            self.db, self.name, self.runner.runner_id, self.epoch,
            status=status,
            status_text="%s en %ds%s" % (label, int(duration), "" if ok else " (échec)"),
            error=error, cost_usd=cost,
        )
        self.last_activity = time.monotonic()
        if ok:
            self.session_turns += 1
        if spec["kind"] == "idle":
            self.nudged = True
        with self.runner.lock:
            self.runner.turns += 1
        return ok

    def failure_wait(self) -> float:
        """Attente avant le tour suivant après un échec : doublée à chaque échec
        rapide consécutif, bornée par `failure_backoff_max` (L48)."""
        base = self.runner.failure_backoff
        if self.fast_failures <= 1:
            return base
        return min(self.runner.failure_backoff_max,
                   base * (2 ** (self.fast_failures - 1)))

    def stop_after_failures(self) -> None:
        """L48 : trop d'échecs rapides consécutifs (harnais introuvable, qui
        sort aussitôt…) : on arrête l'agent (`stop_reason` = erreur) au lieu de
        relancer sans fin. La consigne reste en attente ; l'alerte d'agent
        arrêté prévient le responsable ; `ameesh resume` le relance."""
        n = self.fast_failures
        derniere = (self.agent or {}).get("last_error") or ""
        try:
            fresh = registry.get(self.db, self.name) or {}
            derniere = fresh.get("last_error") or derniere
        except db_mod.DbError:
            pass
        texte = "arrêté après %d échecs rapides consécutifs" % n
        log("[%s] %s : %s" % (self.name, texte, derniere or "cause inconnue"))
        try:
            registry.set_status(self.db, self.name, "stopped", status_text=texte,
                                error=derniere or None, stop_reason="erreur")
        except db_mod.DbError as exc:
            log("[%s] arrêt après échecs non enregistré : %s" % (self.name, exc))
        self._fil_note(
            "Agent arrêté par l'exécuteur après %d échecs rapides consécutifs "
            "(dernière erreur : %s). La consigne reste en attente ; corriger la "
            "cause puis `ameesh resume %s`." % (n, derniere or "inconnue", self.name),
            meta={"action": "arret_apres_echecs", "echecs": n})

    def fail_turn(self, status_text: str, error: str) -> None:
        """Un tour qui n'a pas pu démarrer : la consigne repart en attente (R5)."""
        self.fast_failure = True
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
            self.apply_restart_if_requested()
            self.maybe_rotate()
            spec = self.rotate_for_lot(self.pick())
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
                # L72 : une base injoignable n'arrête plus le worker (son fil
                # mourait sur l'exception) : tours suspendus, attente
                # croissante, reprise d'elle-même au retour de la base.
                try:
                    if self._consigne_a_restaurer:
                        self._restaure_consigne()
                    self.apply_restart_if_requested()
                    self.maybe_rotate()
                    spec = self.rotate_for_lot(self.pick())
                except db_mod.Unavailable as exc:
                    self._panne_de_base(exc, "le choix du tour")
                    continue
                self._base_retablie()
                if spec is None:
                    self.wake.wait(timeout=self.wait_timeout(
                        min(self.runner.poll, max(1.0, self.runner.lease_ttl / 3.0))))
                    self.wake.clear()
                    continue
                try:
                    reussi = self.run_turn(spec)
                except db_mod.Unavailable as exc:
                    # Panne avant le lancement du harnais (ou après-tour au-delà
                    # du bail) : la consigne éventuellement consommée repartira
                    # en attente au retour de la base. Pas un échec de tour :
                    # la série d'échecs rapides (L48) n'est pas touchée.
                    self._consigne_a_restaurer = True
                    self._panne_de_base(exc, "le tour")
                    continue
                if reussi:
                    self.fast_failures = 0
                    continue
                if self.fast_failure:
                    self.fast_failures += 1
                    if self.fast_failures >= self.runner.max_fast_failures:
                        self.stop_after_failures()
                        break
                # Un message non remis ou un harnais en échec ne doit pas
                # produire une boucle serrée : on laisse retomber, de plus en
                # plus longtemps tant que les échecs rapides se suivent (L48).
                self.wake.wait(timeout=self.failure_wait())
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
        #: plafond (octets UTF-8) de la consigne d'un tour de courrier, résumé de
        #: reprise compris ; borné par l'argument de processus (128 Kio)
        self.prompt_max_bytes = int(min(max(float(mail.PROMPT_MIN_BYTES),
                                            cfg.prompt_mail_max),
                                        float(mail.ARG_SAFE_BYTES)))
        self.session_max_tokens = max(0.0, cfg.session_max_tokens)
        self.session_max_turn_seconds = max(0.0, cfg.session_max_turn_seconds)
        self.session_min_turns = max(0, int(cfg.session_min_turns))
        self.worktree_roots = tuple(cfg.worktree_roots)
        self.failure_backoff = 5.0
        #: L48 : attente maximale entre deux tours en échec, durée sous laquelle
        #: un échec compte comme rapide, et série d'échecs rapides qui arrête l'agent
        self.failure_backoff_max = max(self.failure_backoff, cfg.failure_backoff_max)
        self.fast_failure_s = max(0.0, cfg.fast_failure_s)
        self.max_fast_failures = max(1, int(cfg.max_fast_failures))
        #: L72 : attente maximale entre deux essais quand la base est injoignable
        self.db_retry_max = max(Reprise.MINIMUM, float(getattr(cfg, "db_retry_max", 60.0)))
        self.stop = threading.Event()
        self.wake_all = threading.Event()
        self.workers: dict[str, AgentWorker] = {}
        self.lock = threading.Lock()
        self.listener = None          # écouteur LISTEN/NOTIFY courant
        self.listener_thread = None
        #: workers attendus par `shutdown` ; un survivant force la sortie de `_sortie_sure`
        self.stopped_threads: list = []
        #: `canon sync` de l'hôte : au démarrage puis périodiquement (spec §4.4)
        self.canon_sync_interval = max(0.0, cfg.canon_sync_interval)
        self.canon_thread: threading.Thread | None = None
        #: garde de budget (L13, 0019) : plafond horaire et cadence de contrôle
        self.budget_usd_per_hour = max(0.0, cfg.budget_usd_per_hour)
        self.budget_check_interval = max(1.0, cfg.budget_check_interval)
        #: L70 : plafonds réglés en base pour tout le mesh, relus à chaud
        #: (cache court, invalidé par le réveil `ameesh_budget`) ; le plafond
        #: horaire de la configuration ci-dessus reste le défaut
        self.budget_cache = budget_mod.Cache()
        #: pression de l'hôte (L31, 0028) : seuils du canon et verdict caché
        from . import resources as resources_mod
        self._pressure_lock = threading.Lock()
        self._host_limits = resources_mod.thresholds(None)
        self._pressure: dict = {"blocked": False, "critical": False, "breaches": [],
                                "limits": self._host_limits}
        self._pressure_at = 0.0
        self._all_host_limits: dict = {}
        #: L43 (0031) : `max_agents` physique (le plus strict des canons) et
        #: provenance de chaque limite ; journal « hôte plein » une fois par valeur
        self._host_max_agents: int | None = None
        self._host_limits_origin: dict = {}
        self._cap_logged: int | None = None
        #: déplacement entre hôtes admis (L31, 0028), faux par défaut
        self.relocate = bool(cfg.relocate)
        self.shared_sessions = bool(cfg.shared_sessions)
        #: moteur de conteneurs (L31, 0028) : None sans binaire/démon
        from . import containers as containers_mod
        self._container_runtime = containers_mod.Runtime.from_config(cfg)
        self.turns = 0
        self.did_turn = False
        #: L109 : exécuteur médié — pas de base, le serveur du mesh par
        #: `/api/exec/v1` (`storage.remote`). Synchronisation du canon, relevé
        #: des soldes, échéance des délégations et déplacement entre hôtes
        #: passent au serveur ; l'hôte, l'owner, le bail et les limites
        #: viennent de `GET /host`. Porte d'hôte (L112) : `AlwaysAvailable`
        #: par défaut.
        self.mediated = getattr(db, "driver", None) == "mediated"
        from .executeur_mediee import porte as porte_mod
        self.gate = porte_mod.AlwaysAvailable()
        self.host_info = None
        if self.mediated:
            self._mediated_setup()
        #: dernière erreur de traitement des délégations échues (L40), dite une fois
        self._delegation_error: str | None = None
        self._delegation_dry_seen: set = set()

    # -- exécuteur médié (L109) ---------------------------------------------
    def _mediated_setup(self) -> None:
        """La fiche de l'hôte rendue par le serveur (`GET /host`) : hôte fixé
        à l'enrôlement, owner `exec:<id>:<hôte>:<pid>`, bail imposé, limites
        (à la place de `resources.host_limits` sur le canon local)."""
        from . import resources as resources_mod
        from .executeur_mediee import contrat as exec_contrat
        info = self.db.host_info()
        self.host_info = info
        owner = exec_contrat.owner_for(info.executor_id, info.host, os.getpid())
        self.cfg = dataclasses.replace(self.cfg, host=info.host, runner_id=owner)
        self.host = info.host
        self.runner_id = owner
        if info.lease_ttl_s:
            self.lease_ttl = float(info.lease_ttl_s)
        limits = dict(info.limits or {})
        resources = dict(limits.get("resources") or {})
        resources.update({k: v for k, v in limits.items()
                          if k in resources_mod.THRESHOLD_KEYS})
        self._host_limits = resources_mod.thresholds({"resources": resources})
        self._pressure["limits"] = self._host_limits
        maximum = limits.get("max_agents")
        self._host_max_agents = int(maximum) if maximum is not None else None
        self._host_limits_origin = {"max_agents": "serveur"}
        self.relocate = False

    # -- réclamation -------------------------------------------------------
    def expire_delegations_once(self) -> list[dict]:
        """L40 (0030, point 5) : les délégations échues, à chaque passe.

        Ne dépend d'aucun agent : tout exécuteur vivant traite toutes les
        échéances ; deux exécuteurs simultanés ne rendent jamais deux fois un
        lot (verrou de ligne et recontrôle, `work.expire_delegations`). En
        dry-run, ce qui serait fait est seulement journalisé. Une erreur de
        base est journalisée une fois, jamais fatale à la passe."""
        from . import work as work_mod

        if getattr(self, "mediated", False):
            return []  # L109 : l'échéance des délégations passe au serveur
        try:
            done = work_mod.expire_delegations(
                self.db, actor="exécuteur %s" % self.runner_id, dry_run=self.dry_run)
        except db_mod.DbError as exc:
            message = "délégations échues non traitées (%s)" % exc
            if message != self._delegation_error:
                log_async(message)
            self._delegation_error = message
            return []
        self._delegation_error = None
        for row in done:
            if row.get("dry_run"):
                # rien n'est écrit en dry-run : dit une fois, pas à chaque passe
                if row["delegation_id"] in self._delegation_dry_seen:
                    continue
                self._delegation_dry_seen.add(row["delegation_id"])
            if row["outcome"] == "rendue":
                log_async("délégation échue : lot #%s rendu à %s (%s n'y a fait aucun tour)%s"
                          % (row["work_item_id"], row["delegated_by"], row["delegate"],
                             " [dry-run]" if row.get("dry_run") else ""))
            elif row["outcome"] == "soldee":
                log_async("délégation soldée : lot #%s (travail de %s)%s"
                          % (row["work_item_id"], row["delegate"],
                             " [dry-run]" if row.get("dry_run") else ""))
        return done

    def sweep(self) -> None:
        self.expire_delegations_once()
        for row in registry.reap(self.db, self.host):
            log("bail expiré : %s (propriétaire %s)" % (row["name"], row.get("lease_owner")))
        with self.lock:
            for name, worker in list(self.workers.items()):
                if not worker.is_alive():
                    log("worker %s terminé" % name)
                    del self.workers[name]
        if getattr(self, "mediated", False) and not self.gate.state().may_claim:
            return  # L109/L112 : porte d'hôte fermée, aucune nouvelle réclamation
        for agent in registry.claimable(self.db, self.host, self.agents_filter):
            with self.lock:
                if not self.once and agent["name"] in self.workers:
                    continue
            if not self.once and not self.admits_new_worker(agent):
                # L43 (0031) : hôte plein (max_agents physique) — l'agent reste
                # réclamable, il le sera quand une place se libère
                if getattr(self, "_cap_logged", None) != self._host_max_agents:
                    self._cap_logged = self._host_max_agents
                    log_async("hôte %s plein : max_agents = %s (fiche Host la plus stricte, "
                              "%s) — %s attend une place"
                              % (self.host, self._host_max_agents,
                                 (getattr(self, "_host_limits_origin", None) or {}).get(
                                     "max_agents", "?"), agent["name"]))
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

    def budget_limits(self, db) -> "budget_mod.Limits":
        """Les plafonds en vigueur (L70) : base (cache court) > config > défaut."""
        return self.budget_cache.limits(self.cfg, db,
                                        hourly_default=self.budget_usd_per_hour)

    # -- écoute ------------------------------------------------------------
    def dispatch(self, item: dict) -> None:
        """Réveille (ou préempte) le worker visé, **signal avant journal**.

        Un `log` synchrone avant `request_preempt`/`wake.set` laissait un pipe
        saturé retarder la préemption (verdict codex3 L11 B1) : tous les signaux
        partent d'abord, la journalisation passe par la file bornée.
        """
        if item.get("channel") == CHANNEL_BUDGET:
            # L70 : un plafond a changé en base ; le réveil n'est qu'un signal,
            # la garde relit la table au prochain sondage de chaque worker.
            self.budget_cache.invalidate()
            with self.lock:
                for worker in self.workers.values():
                    worker.wake.set()
            self.wake_all.set()
            log_async("plafond de budget changé en base : relecture")
            return
        if item.get("channel") == "reset":
            # L109 : trou de reprise du flux d'événements du serveur — des
            # réveils ont pu se perdre : chaque worker relit en base.
            with self.lock:
                for worker in self.workers.values():
                    worker.wake.set()
            self.wake_all.set()
            return
        if item.get("channel") == CHANNEL_MAIL:
            try:
                payload = json.loads(item.get("payload") or "{}")
            except ValueError:
                payload = {}
            target = payload.get("to")
            with self.lock:
                worker = self.workers.get(target) if target else None
            if payload.get("restart"):
                # `ameesh restart` (L26) : le NOTIFY n'est qu'un réveil ; la
                # demande est relue en base avant d'arrêter quoi que ce soit.
                if worker is not None:
                    agent = registry.get(self.db, target) or {}
                    if agent.get("restart_requested_ts"):
                        worker.request_restart()  # le signal d'abord
                        log_async("redémarrage demandé pour %s : arrêt du tour" % target)
                self.wake_all.set()
                return
            prioritaire = False
            if worker is not None and payload.get("id"):
                row = mail.get(self.db, int(payload["id"]))
                if row and mail.is_urgent(row):
                    prioritaire = worker.interrupt_allowed(row)
                    if not prioritaire:
                        log_async("urgent de %s ignoré : expéditeur non habilité à interrompre"
                                  % (row.get("sender") or "?"))
            if worker and prioritaire:
                worker.request_preempt()  # le signal d'abord
                log_async("message prioritaire pour %s : interruption du tour" % target)
            elif worker:
                worker.wake.set()
                log_async("message pour %s : réveil du tour" % target)
            else:
                for autre in self.workers.values():
                    autre.wake.set()
        self.wake_all.set()

    def _listen_loop(self) -> None:
        """Écoute LISTEN/NOTIFY, reconnectée après une coupure (L72) : attente
        croissante tant que la base ne répond pas (le sondage `poll` des
        workers et de la passe prend le relais), journal borné."""
        reprise = Reprise(self.db_retry_max, minimum=1.0)
        annonce = False
        while not self.stop.is_set():
            listener = storage.of(self.db).wakeups.subscribe(
                [CHANNEL_MAIL, CHANNEL_LEASE, CHANNEL_BUDGET])
            if listener is None:
                try:
                    self.db.ping()
                except db_mod.DbError as exc:
                    # psycopg : la connexion d'écoute n'a pas pu s'ouvrir
                    attente = reprise.echec()
                    log_async("écoute impossible (%s) : nouvel essai dans %ds"
                              % (db_mod.explain(exc), int(attente)))
                    self.stop.wait(attente)
                    continue
                log_async("LISTEN/NOTIFY indisponible avec ce pilote : sondage toutes les %ss"
                          % self.poll)
                return
            self.listener = listener
            if not annonce:
                log_async("LISTEN %s, %s, %s" % (CHANNEL_MAIL, CHANNEL_LEASE, CHANNEL_BUDGET))
                annonce = True
            debut = time.monotonic()
            # une écoute qui tient plus longtemps qu'une tentative de connexion
            # est vivante : la panne est finie pour elle
            vivante_apres = max(1.0, float(self.cfg.connect_timeout)) + self.poll
            try:
                while not self.stop.is_set():
                    item = listener.wait(timeout=self.poll)
                    if item is None:
                        if (reprise.en_panne
                                and time.monotonic() - debut >= vivante_apres):
                            bilan = reprise.retablie()
                            log_async("écoute rétablie après %ds (%d échec(s))"
                                      % (int(bilan[1]), bilan[0]))
                        self.wake_all.set()  # battement de sondage
                        continue
                    if item.get("event") == "down":
                        attente = reprise.echec()
                        log_async("écoute interrompue (%s) : reconnexion dans %ds"
                                  % (item.get("error") or "?", int(attente)))
                        break
                    try:
                        self.dispatch(item)
                    except db_mod.DbError as exc:
                        # réveil sans détail : chaque worker relira lui-même
                        log_async("réveil sans détail (%s)" % db_mod.explain(exc))
                        with self.lock:
                            for worker in self.workers.values():
                                worker.wake.set()
                        self.wake_all.set()
            finally:
                listener.close()
                self.listener = None
            if not self.stop.is_set():
                self.stop.wait(reprise.minimum if not reprise.en_panne
                               else min(reprise.maximum,
                                        reprise.minimum * 2 ** (reprise.echecs - 1)))

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
            worker.stop_group_now("arrêt de l'exécuteur", grace=3.0)
        deadline = time.monotonic() + 8.0
        for worker in workers:
            worker.join(timeout=max(0.1, deadline - time.monotonic()))
            # Le tour a pu publier son harnais après le terminate initial
            # (course de publication) : rien ne doit survivre au relâchement
            # du bail (sonde codex3 B5a-P).
            worker.stop_group_now("arrêt de l'exécuteur (dur)", grace=0, hard=True)
            worker.release_lease()
        # L'écouteur (fil démon) ne doit plus écrire quand l'interpréteur se
        # finalise : sa connexion est fermée ci-dessus, on l'attend, borné.
        if self.listener_thread is not None:
            self.listener_thread.join(timeout=2.0)
        self.stopped_threads = list(workers)
        # par la file : aucune écriture synchrone sur le chemin d'arrêt avant
        # le repli de `_sortie_sure` (relecture codex2 de 669a6fe, B1)
        log_async("arrêt : %d bail(aux) rendu(s), %d tour(s)" % (len(workers), self.turns))

    # -- canon sync (spec §4.4) --------------------------------------------
    def canon_sync_once(self) -> bool:
        """Un `canon sync` de l'hôte ; ne lève jamais, ne tue jamais un tour.

        Le canon est relu à chaque passe (`from_config`) et l'état est écrit par
        `canon_sync.sync` ; un canon illisible enregistre l'état `unreadable`,
        ce qui ferme la réclamation (L2) sans toucher aux baux ni aux tours en
        cours. Un échec — canon, git, base — est journalisé et rend `False`.
        """
        if getattr(self, "mediated", False):
            return True  # L109 : le canon est synchronisé par le serveur
        entries = canon_mod.configured(self.cfg)
        if not entries:
            return True
        db = None
        ok = True
        try:
            db = db_mod.connect(self.cfg)
            db_mod.require_schema(db)
            # L42 (0031) : chaque canon configuré est relu et synchronisé à chaque
            # passe, le canon par défaut d'abord ; une erreur dans l'un n'empêche
            # pas les autres. Les identifiants en double sont refusés (le second).
            loaded: list = []
            for index, entry in enumerate(entries):
                try:
                    canon = (canon_mod.from_config(self.cfg) if index == 0
                             else canon_mod.from_config(self.cfg, entry=entry))
                    ident = getattr(canon, "id", None)
                    if ident and any(getattr(c, "id", None) == ident for c in loaded):
                        raise canon_mod.CanonError(
                            "configuration : identifiant de canon « %s » en double (%s) : "
                            "ce canon n'est pas synchronisé" % (ident, entry.path))
                    loaded.append(canon)
                except Exception as exc:  # jamais fatal : le canon ferme, il ne tue pas
                    ok = False
                    log_async("canon sync en échec (%s) : %s" % (
                        entry.path, " ".join(str(exc).split())[:200]))
            if len(entries) > 1 and loaded and all(hasattr(c, "is_default") for c in loaded):
                try:
                    canon_sync.adopt_default(db, self.host, loaded)
                except Exception as exc:
                    ok = False
                    log_async("canon par défaut : %s — aucun canon synchronisé"
                              % " ".join(str(exc).split())[:300])
                    # L46 : changement de canon par défaut refusé ou en échec
                    # (transaction annulée) : AUCUN canon n'est synchronisé, le
                    # nouveau canon par défaut prendrait sinon les lignes NULL
                    # de l'ancien pour les siennes. Rien n'est fermé non plus.
                    loaded = []
            synced: list = []
            for canon in loaded:
                try:
                    report = canon_sync.sync(db, canon, self.host)
                    synced.append(canon)
                    status = getattr(report, "status", None)
                    if status and status != canon_sync.CANON_OK:
                        log_async("canon sync%s : %s (%s)" % (
                            " %s" % canon.id if len(loaded) > 1 else "", status,
                            getattr(report, "diagnostic", None) or "sans détail"))
                except Exception as exc:  # jamais fatal : le canon ferme, il ne tue pas
                    ok = False
                    log_async("canon sync en échec%s : %s" % (
                        " (%s)" % getattr(canon, "id", "?") if len(loaded) > 1 else "",
                        " ".join(str(exc).split())[:200]))
            # L42 : un canon retiré de la configuration ne garde pas ses agents
            # réclamables (seulement si tous les canons configurés sont chargés :
            # jamais de fermeture sur une configuration lue en partie)
            if len(loaded) == len(entries) and all(hasattr(c, "is_default") for c in loaded):
                try:
                    canon_sync.close_unconfigured(db, self.host, loaded)
                except Exception as exc:
                    ok = False
                    log_async("canons retirés : %s" % " ".join(str(exc).split())[:200])
            # L31 : seuils de pression de l'hôte. `canon_sync_once` est aussi
            # exercé sur un objet factice (tests d'authentificateurs).
            refresh = getattr(self, "refresh_host_limits", None)
            if refresh is not None and synced:
                refresh(*synced)
            return ok
        except Exception as exc:  # jamais fatal : le canon ferme, il ne tue pas
            log_async("canon sync en échec : %s" % " ".join(str(exc).split())[:200])
            return False
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def _canon_sync_loop(self) -> None:
        while not self.stop.is_set():
            self.canon_sync_once()
            if self.stop.wait(self.canon_sync_interval):
                return

    def start_canon_sync(self) -> None:
        """Lance le sync du canon : immédiat, puis périodique (spec §4.4).

        Sans canon configuré ou avec un intervalle nul, il n'y a rien à faire.
        En mode `--once`, le sync est fait en ligne (le fil n'aurait pas le
        temps de tourner) ; sinon un fil dédié s'en charge.
        """
        if getattr(self, "mediated", False):
            return  # L109 : le canon est synchronisé par le serveur
        if not self.cfg.canon or self.canon_sync_interval <= 0:
            if self.cfg.canon:
                self.canon_sync_once()  # seulement au démarrage
            return
        if self.once:
            self.canon_sync_once()
            return
        if self.canon_thread is not None:
            return
        self.canon_thread = threading.Thread(
            target=self._canon_sync_loop, daemon=True, name="canon-sync")
        self.canon_thread.start()

    # -- solde des fournisseurs payés au token (L26) ------------------------
    def balance_once(self, sources=None, min_interval_s: float = 0.0) -> int:
        """Relève le solde de chaque source configurée ; ne lève jamais.

        La clé n'est lue que par la source, dans l'environnement, et ne
        figure dans aucun message : seuls le fournisseur et la raison courte
        de l'échec sont journalisés.
        """
        if getattr(self, "mediated", False):
            return 0  # L109 : les soldes sont relevés par le serveur (sans clé ici)
        from . import balance as balance_mod
        # L30 : une source par compte de clé d'API déclaré (sinon l'historique)
        paires = (balance_mod.account_sources(self.cfg) if sources is None
                  else [(s, None) for s in sources])
        paires = [(s, compte) for s, compte in paires if s.configured()]
        if not paires:
            return 0
        db = None
        releves = 0
        try:
            db = db_mod.connect(self.cfg)
            for source, compte in paires:
                try:
                    releves += len(balance_mod.record(db, source, compte,
                                                      min_interval_s=min_interval_s))
                except (balance_mod.BalanceError, db_mod.DbError) as exc:
                    log_async("solde %s%s : %s" % (
                        source.provider, "/%s" % compte if compte else "",
                        " ".join(str(exc).split())[:160]))
        except Exception as exc:  # jamais fatal
            log_async("solde : relevé impossible (%s)" % type(exc).__name__)
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
        return releves

    def start_balance_poll(self) -> None:
        """Relevé périodique du solde (`AMEESH_BALANCE_INTERVAL`, défaut 900 s).

        Rien en mode `--once`, avec un intervalle nul, ou sans clé de
        fournisseur dans l'environnement."""
        from . import balance as balance_mod
        interval = max(0.0, float(self.cfg.balance_interval))
        if self.once or interval <= 0 or getattr(self, "mediated", False):
            return
        if not any(s.configured() for s, _ in balance_mod.account_sources(self.cfg)):
            return

        def boucle() -> None:
            while not self.stop.is_set():
                # L71 : un relevé par période pour tous les exécuteurs de la
                # base, pas un par exécuteur (moitié de période de marge)
                self.balance_once(min_interval_s=interval / 2.0)
                if self.stop.wait(max(60.0, interval)):
                    return

        threading.Thread(target=boucle, daemon=True, name="solde").start()

    # -- ressources de l'hôte (L31, 0028) -----------------------------------
    def resource_once(self) -> dict | None:
        """Relève et publie les ressources de l'hôte ; ne lève jamais.

        Un relevé ne doit jamais empêcher un tour : toute erreur (poste sans
        `/proc`, base indisponible) est journalisée et rend None."""
        from . import resources as resources_mod
        if getattr(self, "mediated", False):
            # L109 : relevé auto-déclaré par la connexion distante (`hosts.record`)
            try:
                return resources_mod.collect(self.cfg, self.db)
            except Exception as exc:  # jamais fatal pour l'exécuteur
                log_async("ressources : relevé impossible (%s)" % type(exc).__name__)
                return None
        db = None
        try:
            db = db_mod.connect(self.cfg)
            return resources_mod.collect(self.cfg, db)
        except Exception as exc:  # jamais fatal pour l'exécuteur
            log_async("ressources : relevé impossible (%s)" % type(exc).__name__)
            return None
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def start_resource_poll(self) -> None:
        """Relevé périodique des ressources (`AMEESH_RESOURCE_INTERVAL`,
        défaut 60 s). En mode `--once`, un seul relevé en ligne."""
        interval = max(0.0, float(self.cfg.resource_interval))
        if interval <= 0:
            return
        if self.once:
            self.resource_once()
            return

        def boucle() -> None:
            while not self.stop.is_set():
                self.resource_once()
                if self.stop.wait(max(10.0, interval)):
                    return

        threading.Thread(target=boucle, daemon=True, name="ressources").start()

    def refresh_host_limits(self, canon, *others) -> None:
        """Met à jour les limites physiques de CET hôte depuis les canons.

        Appelé après chaque `canon sync` (le canon est la source déclarative
        des seuils) ; sans fiche Host, les valeurs par défaut prudentes
        s'appliquent. Force le recalcul du verdict de pression.

        L43 (0031) : `others` sont les autres canons configurés. Les limites
        PHYSIQUES (seuils de ressources, `max_agents` de l'hôte) sont les plus
        strictes de toutes les fiches Host de cet hôte dans ces canons
        (`resources.host_limits`) ; l'admission de chaque agent reste jugée
        par la fiche de son canon (`canon sync`).

        Défensif sur les attributs : `canon_sync_once` est aussi exercé sur un
        objet factice par les tests d'authentificateurs, qui n'a que `cfg` et
        `host`."""
        from . import resources as resources_mod
        canons = [c for c in (canon,) + tuple(others)
                  if c is not None and hasattr(c, "host") and hasattr(c, "hosts")]
        if not canons:
            return  # objet factice des tests de `canon_sync_once`
        mine = resources_mod.host_limits(canons, self.host)
        limits = mine["limits"]
        # seuils de TOUS les hôtes des canons : le déplacement (L31) compare les
        # candidats avec les limites physiques de leur propre hôte.
        par_hote: dict = {}
        for one in canons:
            for h in one.hosts:
                if h.title not in par_hote:
                    par_hote[h.title] = resources_mod.host_limits(canons, h.title)["limits"]
        self._host_max_agents = mine["max_agents"]
        self._host_limits_origin = mine["origin"]
        lock = getattr(self, "_pressure_lock", None)
        if lock is None:
            self._host_limits = limits
            return
        with lock:
            self._all_host_limits = par_hote
            if limits != self._host_limits:
                self._host_limits = limits
                self._pressure_at = 0.0

    def admits_new_worker(self, agent: dict) -> bool:
        """L43 (0031) : `max_agents` PHYSIQUE de l'hôte (le plus petit des
        fiches Host de tous les canons) borne le nombre de personas que cet
        exécuteur fait tourner à la fois. Les éphémères n'y comptent pas (ils
        n'ont pas d'admission ; `max_agents` compte des admissions). Sans
        maximum déclaré : pas de borne."""
        cap = getattr(self, "_host_max_agents", None)
        if cap is None or (agent or {}).get("ephemeral"):
            return True
        with self.lock:
            running = sum(1 for name, worker in self.workers.items()
                          if name != (agent or {}).get("name") and worker.is_alive()
                          and not (getattr(worker, "agent", None) or {}).get("ephemeral"))
        return running < cap

    def host_limits_for(self, host: str) -> dict | None:
        """Seuils connus d'un hôte (dernier `canon sync`), ou None."""
        return getattr(self, "_all_host_limits", {}).get(host)

    def container_runtime(self):
        """Le moteur de conteneurs de l'hôte, ou None (L31, 0028)."""
        return getattr(self, "_container_runtime", None)

    def host_pressure(self, now: float | None = None) -> dict:
        """Verdict de pression de l'hôte (L31, 0028), cache court.

        `blocked` : au moins un seuil est franchi — aucun nouveau tour ne
        démarre, les tours en cours finissent. `critical` : un franchissement
        est grave — les agents de plus faible priorité sont mis en pause, jamais
        au milieu d'un tour. Sans relevé, aucune pression (on ne devine pas)."""
        from . import resources as resources_mod
        now = time.monotonic() if now is None else float(now)
        with self._pressure_lock:
            if now - self._pressure_at < 10.0:
                return dict(self._pressure)
            limits = self._host_limits
        reading = None
        try:
            reading = storage.of(self.db).hosts.latest(self.host)
        except db_mod.DbError:
            reading = None
        if reading is None:
            verdict: dict = {"blocked": False, "critical": False, "breaches": [],
                             "limits": limits}
        else:
            verdict = resources_mod.pressure(reading, limits=limits)
            verdict["since"] = reading.get("sampled_ts")
            verdict["reading"] = reading
        with self._pressure_lock:
            self._pressure = verdict
            self._pressure_at = now
        return dict(verdict)

    def run(self) -> int:
        log("démarrage : hôte %s, exécuteur %s, pilote %s, schéma %s"
            % (self.host, self.runner_id, self.db.name, self.cfg.schema))
        self.start_canon_sync()
        self.start_balance_poll()
        self.start_resource_poll()
        if self.once:
            self.sweep()
            if not self.did_turn:
                log_async("aucun travail disponible")  # chemin de sortie : par la file
            return 0 if self.did_turn else 3
        self.listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.listener_thread.start()
        reprise = Reprise(self.db_retry_max)
        try:
            while not self.stop.is_set():
                try:
                    self.sweep()
                except db_mod.DbError as exc:
                    if getattr(exc, "code", None) == "executor_revoked":
                        # L109 : exécuteur révoqué par le serveur — arrêt, sans
                        # nouvel essai (les baux sont déjà relâchés côté serveur)
                        log_async("exécuteur révoqué par le serveur : arrêt (%s)" % exc)
                        break
                    if self.stop.is_set():
                        # arrêt en cours : systemd a pu tuer le `psql` fils
                        # avec nous (« psql : code -15 ») — rien à réessayer
                        break
                    # L72 : une passe en échec n'arrête plus l'exécuteur (et
                    # avec lui tous les harnais de l'hôte) : on réessaie.
                    attente = reprise.echec()
                    log_async("passe de l'exécuteur en échec (%s) : nouvel essai dans %ds "
                              "(échec %d, panne depuis %ds) ; les tours en cours continuent"
                              % (db_mod.explain(exc), int(attente), reprise.echecs,
                                 int(reprise.duree())))
                    self.stop.wait(attente)
                    continue
                bilan = reprise.retablie()
                if bilan:
                    log_async("base de nouveau joignable après %ds (%d échec(s)) : passe "
                              "de l'exécuteur reprise" % (int(bilan[1]), bilan[0]))
                if self.max_turns and self.turns >= self.max_turns:
                    log_async("limite de %d tour(s) atteinte" % self.max_turns)
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
    if cwd:
        # Identité git du dossier : elle permettra de le retrouver s'il est
        # déplacé (0018), sans confondre deux worktrees du même dépôt.
        marker = write_worktree_marker(cfg, parsed.name, cwd)
        if marker:
            log("dossier de travail %s : dépôt %s, branche %s"
                % (cwd, marker["git_common_dir"], marker.get("branch") or "?"))
    log("agent %s inscrit : %s sur %s%s%s" % (
        parsed.name, parsed.harness, cfg.host,
        ", session %s" % parsed.session if parsed.session else "",
        ", consigne en attente" if parsed.prompt else "",
    ))
    if not parsed.session and row and row.get("session_id"):
        # L39 (0030) : `register` sans --session GARDE la session enregistrée ;
        # l'oublier proprement est le rôle de `ameesh resume --fresh`.
        log("session enregistrée conservée (%s) : « ameesh resume %s --fresh » pour "
            "repartir d'une session neuve" % (row["session_id"], parsed.name))
    return 0 if row else 1


def cmd_stop(cfg: Config, db: db_mod.Db, args: list[str]) -> int:
    if not args:
        print("usage: agent-runner stop <nom>", file=sys.stderr)
        return 2
    # L37 (0030) : raison structurée — un arrêt manuel ne lève pas
    # `stopped_with_mail` (l'humain sait qu'il l'a arrêté)
    registry.set_status(db, args[0], "stopped", status_text="arrêté à la main",
                        stop_reason="manuel")
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


def stop_group_now(proc: subprocess.Popen, reason: str, *, name: str = "",
                   grace: float | None = None, hard: bool = True) -> None:
    """Point de passage d'arrêt d'un groupe de harnais isolé (`attach`).

    Même règle que `AgentWorker.stop_group_now` : le signal part **d'abord**
    (SIGTERM bref puis SIGKILL si `hard` est faux), le journal borné vient
    ensuite. Aucun `print` ne précède la coupe.
    """
    if hard or grace is None:
        _kill_group(proc)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        if proc.poll() is None:
            _kill_group(proc)
    log_async("[%s] harnais arrêté (%s)" % (name or "?", reason))


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
    # L30 : la session interactive prend le compte actif du harnais ; un profil
    # inutilisable est refusé AVANT de prendre le bail.
    ok, compte = account_turn.attach_profile(cfg, db, agent.get("harness") or "")
    if not ok:
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
        if not account_turn.apply_env_attach(env, compte, cfg):
            return 1  # le `finally` rend le bail
        log_async("[%s] session interactive : %s" % (name, " ".join(argv)))
        proc = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True)
        # Course de publication (même correctif que B5a-P) : le veilleur a pu
        # conclure pendant que ce Popen n'était pas encore publié.
        if etat["deadline"] > 0 and time.time() >= etat["deadline"]:
            perdu.set()
        while proc.poll() is None:
            if perdu.is_set():
                stop_group_now(proc, "bail perdu (attach)", name=name)
                break
            time.sleep(0.2)
        code = proc.wait()
    except KeyboardInterrupt:
        if proc is not None:
            stop_group_now(proc, "interruption (attach)", name=name)
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
    code = 1  # reste 1 si une exception traverse : le repli sort alors en échec
    try:
        try:
            db_mod.require_schema(db)
        except db_mod.SchemaMissing as exc:
            print("ameesh attach : %s" % exc, file=sys.stderr)
            return 1
        code = run_attach(cfg, db, parsed.agent, wait=parsed.wait, ttl=parsed.ttl)
        return code
    finally:
        try:
            db.close()
        finally:
            # le battement et le veilleur journalisent par la file : la vider
            # avant la finalisation, même sur exception (SIGABRT de l'exécuteur)
            _sortie_sure(code)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # `register --help` / `stop --help` affichent l'aide, jamais d'écriture en base
    if argv and argv[0] in ("register", "stop") and any(a in ("-h", "--help") for a in argv[1:]):
        print("usage: agent-runner register <agent> <harnais> [--session S] [--prompt P] [--cwd D] …\n"
              "       agent-runner stop <agent>\n"
              "       agent-runner [--once] [--agents a,b] [--poll S] …  (agent-runner --help)")
        return 0
    cfg = load_config()
    mediated = cfg.backend == "mediated"
    if mediated and argv and argv[0] in ("register", "stop"):
        # L109 : un exécuteur médié ne déclare ni n'arrête d'agent (le
        # serveur et le canon le font) ; il ne réclame que ce qu'on lui admet
        print("agent-runner %s : non admis pour un exécuteur médié (backend mediated)"
              % argv[0], file=sys.stderr)
        return 2

    if argv and argv[0] == "register":
        db = db_mod.connect(cfg)
        code = 1
        try:
            db_mod.require_schema(db)
            code = cmd_register(cfg, db, argv[1:])
            return code
        finally:
            try:
                db.close()
            finally:
                _sortie_sure(code)
    if argv and argv[0] == "stop":
        db = db_mod.connect(cfg)
        code = 1
        try:
            db_mod.require_schema(db)
            code = cmd_stop(cfg, db, argv[1:])
            return code
        finally:
            try:
                db.close()
            finally:
                _sortie_sure(code)

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
        print("agent-runner : %s injoignable : %s"
              % ("serveur du mesh" if mediated else "base", exc), file=sys.stderr)
        return 1
    #: 1 tant que `run` n'a pas rendu : une exception sort en échec, y compris
    #: par le repli `os._exit` de `_sortie_sure`
    code = 1
    runner: Runner | None = None
    try:
        if parsed.migrate and mediated:
            print("agent-runner : --migrate sans objet pour un exécuteur médié",
                  file=sys.stderr)
            return 2
        if parsed.migrate:
            from . import migrations
            migrations.migrate(db, log=log)
        try:
            db_mod.require_schema(db)
        except db_mod.SchemaMissing as exc:
            print("agent-runner : %s" % exc, file=sys.stderr)
            return 1
        agents = [a for a in (parsed.agents or "").split(",") if a] or None
        try:
            runner = Runner(
                cfg, db, agents=agents, once=parsed.once, wait=parsed.wait,
                dry_run=parsed.dry_run, max_turns=parsed.max_turns,
            )
        except db_mod.DbError as exc:
            if not mediated:
                raise
            # L109 : la fiche de l'hôte (`GET /host`) est illisible au démarrage
            print("agent-runner : fiche de l'hôte indisponible : %s" % exc, file=sys.stderr)
            return 1

        def handler(signum, _frame):
            # Signal d'abord ; le journal part par la file : un `print` dans un
            # gestionnaire de signal peut tomber pendant un autre `print` du
            # fil principal (écriture réentrante sur le même flux).
            runner.stop.set()
            runner.wake_all.set()
            log_async("signal %d reçu : arrêt propre" % signum)

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
        code = runner.run()  # son `finally` rend les baux (shutdown)
        return code
    finally:
        try:
            db.close()
        finally:
            # Incontournable, même sur exception, après la remise des baux : aucun
            # fil démon ne doit écrire pendant la finalisation (SIGABRT, 2026-10-05).
            _sortie_sure(code, runner.stopped_threads if runner is not None else ())


if __name__ == "__main__":
    sys.exit(main())
