#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""agent-runner — one runner per machine: leases, turns, wake-up on NOTIFY.

  agent-runner [--host H] [--runner-id ID] [--agents a,b] [--once [--wait S]]
               [--dry-run] [--max-turns N] [--migrate]
        claims this host's agents, renews their leases, starts a turn
        when a message arrives (LISTEN/NOTIFY) or after inactivity.
  agent-runner register <name> <claude|codex|deepseek> [--cwd DIR] [--prompt TEXT]
               [--session ID] [--chantier C] [--model M] [--budget USD]
        registers an agent (the v1 equivalent of "nexlink-agent start").
  agent-runner stop <name>
        marks the agent stopped: no new lease, the current turn finishes.

Guarantees (R3, R5):
* an agent has only one live lease: `claim` is an atomic conditional UPDATE;
* the lease is renewed by a heartbeat during turns; if it is lost, the
  harness process is terminated and the turn is not counted;
* if the runner dies (SIGKILL, power cut), the lease expires and another runner
  on the same machine takes the agent over;
* the harness JSONL stream is logged to
  ~/.local/state/agent-mesh/<name>/events.jsonl, the session is stored in the
  database and on disk: a turn can be resumed.

Tests never launch a real harness: AGENT_MESH_BIN_DIR or
AGENT_MESH_<HARNESS>_BIN point to fake binaries.
"""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import account_budget, account_turn, adapters, auth_failure, canon as canon_mod, canon_sync
from . import cost as cost_mod
from . import budget as budget_mod, db as db_mod, fil, harnesses, identity, mail, registry, sentinel, storage
from . import rate_limit as rate_limit_mod
from . import persona_memory as memory_mod
from .config import CHANNEL_BUDGET, CHANNEL_LEASE, CHANNEL_MAIL, Config
from . import accounts as accounts_mod, session_backup as session_backup_mod
from .config import load as load_config


def log(message: str) -> None:
    print("%s %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def _duree(secondes: float) -> str:
    """Une durée lisible : « 45 s », « 7 min 05 s », « 2 h 13 min »."""
    secondes = int(max(0.0, secondes))
    if secondes < 60:
        return "%d s" % secondes
    if secondes < 3600:
        return "%d min %02d s" % (secondes // 60, secondes % 60)
    return "%d h %02d min" % (secondes // 3600, secondes % 3600 // 60)


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
                log("[journal] %d message(s) dropped: sink saturated" % perdus)
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
EXIT_REVOKED = 6
#: L790 : le paquet installé a changé sous ce processus — sortie dédiée pour
#: que l'exécuteur se remplace entre deux tours (même invocation) ; si le
#: remplacement direct échoue, le code non nul fait relancer le service
#: (`Restart=on-failure`).
EXIT_RESTART = 75
#: L159f : tant que la porte d'hôte n'est pas fixée (canon pas encore lu en
#: entier), le sync du canon est réessayé à ce pas (secondes)
GATE_SETTLE_RETRY_S = 30

#: L147 : lignes orphelines d'avant le démarrage relues par page (pagination par id)
BOOT_ORPHAN_PAGE = 500
#: L147 (#220 r2) : un conteneur relancé par sa politique (unité du
#: gestionnaire utilisateur, podman-restart) peut démarrer après l'exécuteur —
#: aucune ligne n'est soldée moins de BOOT_SETTLE_S après le démarrage de
#: l'hôte, ni tant que le gestionnaire utilisateur démarre encore (au plus
#: BOOT_SETTLE_MAX_S) ; la revue est refaite toutes les BOOT_REVIEW_EVERY_S
BOOT_SETTLE_S = 600.0
BOOT_SETTLE_MAX_S = 1800.0
BOOT_REVIEW_EVERY_S = 60.0
#: #220 r3 : lignes revues au plus par passage (la suite au passage suivant)
BOOT_REVIEW_MAX_ROWS = 2000
#: L147b : le curseur de la revue — (phase `running` puis `orphan`, dernier id)
BOOT_REVIEW_START = ("running", 0)
#: L76 : après un démarrage résident raté, un tour par événement pendant ce délai
RESIDENT_RETRY_S = 600.0
#: L76 : attente de la fin d'un tour résident après son flux (harnais tué)
RESIDENT_END_WAIT_S = 5.0
#: #339: this host's rows whose group is gone are settled at start and then
#: every LIVE_REVIEW_EVERY_S (L147's rule, with a liveness check in place of
#: the boot time); a `running` row only once its turn (or grace period) is
#: LIVE_REVIEW_IDLE_S old, so a turn that is closing is never raced
LIVE_REVIEW_EVERY_S = 300.0
LIVE_REVIEW_IDLE_S = 600.0
#: L976 : au plus un balayage du courrier en souffrance par heure (le seuil de
#: l'alerte `mail_undeliverable` est de 15 min)
DEAD_LETTER_SWEEP_EVERY_S = 3600.0
#: #339 r2: a process of this user whose environment cannot be read keeps a
#: row if it started after the row, less this margin (the harness starts
#: just before its row is opened; the base's clock may drift from the host's)
LIVE_REVIEW_START_SLACK_S = 300.0
#: L85 : rétention du journal d'événements, au plus une fois par heure
EVENTS_PRUNE_EVERY_S = 3600.0


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
    _ecrire_sans_attendre(2, ("agent-runner: thread(s) still active at shutdown (%s): "
                              "exiting immediately\n" % noms).encode("utf-8", "replace"))
    os._exit(code)


class _LancementImpossible(Exception):
    """Le harnais n'a pas pu être exécuté (L106) : erreur de l'hôte."""


#: L156c : libellé lisible de la sorte d'un tour (journal, `status_text`) ;
#: la valeur stockée (marqueur comptable, grand livre) reste la clé française
TURN_LABELS_EN = {"consigne": "instruction", "messages": "mail", "événements": "events",
                  "prioritaire": "priority", "reprise": "resume", "suite": "continuation"}


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


def _expanduser_in(path: str, env: dict) -> str:
    """`~` résolu avec le HOME de `env` (l'environnement du tour), pas celui
    de l'exécuteur (L224, verdict codex #183) ; `~autre` reste à `expanduser`."""
    home = env.get("HOME")
    if home and (path == "~" or path.startswith("~/")):
        return home.rstrip("/") + path[1:] if home != "/" else "/" + path[2:]
    return os.path.expanduser(path)


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

    #: L76 : le processus résident de l'agent entre ses tours, ou None.
    #: Défaut de classe : les constructions partielles (`__new__` sans
    #: `__init__`, chemin `--once` et tests de la porte d'hôte) doivent
    #: pouvoir passer par `release_lease` -> `_close_resident` sans
    #: AttributeError (CI #417).
    resident = None

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
        #: L76 : le processus résident de l'agent, entre ses tours (publié
        #: dans `proc`/`pgid` le temps d'un tour seulement), ou None
        self.resident = None
        #: L76 : après un démarrage résident raté, un tour par événement
        #: jusqu'à cette échéance (monotone)
        self._resident_refused_until = 0.0
        self.nudged = False
        #: L48 : le dernier tour a échoué vite (harnais absent, introuvable,
        #: sorti en erreur en moins de FAST_FAILURE_S) ; et leur série
        self.fast_failure = False
        self.fast_failures = 0
        #: #1268 : série d'échecs de tour **réels** (hors hôte, auth, bail,
        #: session fille), remise à zéro par un succès ; marquée dans
        #: `status_text` avec le **début de l'épisode** (`since`), et lue par
        #: l'alerte `critical_turn_failures`. Relue de la ligne : elle survit
        #: au redémarrage de l'exécuteur, y compris pendant une tentative
        #: (`begin_turn` la préserve).
        from . import exploitation as exploitation_mod
        self.failure_streak = exploitation_mod.failure_streak(agent.get("status_text"))
        self.failure_since = 0.0
        if self.failure_streak:
            # marqueur d'avant le champ `since` : repli sur la date de la ligne
            self.failure_since = (exploitation_mod.failure_since(agent.get("status_text"))
                                  or float(agent.get("status_since_ts") or 0.0))
        #: #1270 : épisode de débit/quota fournisseur, relu de la ligne comme la
        #: série d'échecs — la reprise cadencée et son **échéance absolue**
        #: (`retry at`) survivent au redémarrage et ne se court-circuitent pas.
        etat_debit = rate_limit_mod.state(agent.get("status_text"))
        self.rate_limit = etat_debit["kind"] if etat_debit else None
        self.rate_limit_streak = etat_debit["streak"] if etat_debit else 0
        self.rate_limit_since = etat_debit["since"] if etat_debit else 0.0
        self.rate_limit_retry_at = 0.0
        if etat_debit:
            self.rate_limit_retry_at = etat_debit.get("retry_at") or 0.0
            if not self.rate_limit_retry_at:
                # marqueur d'avant `retry at` : repli sur la date de la ligne
                self.rate_limit_retry_at = (float(agent.get("status_since_ts") or 0.0)
                                            + etat_debit["wait"])
        self.last_activity = time.monotonic()
        #: #408 : dernière lecture des lots actionnables (règle 1)
        self._lot_wake_checked = float("-inf")
        self.state_dir = self.cfg.agent_dir(self.name)
        self.renew_failures = 0
        self.lease_deadline = float(lease.get("lease_expires_ts") or 0.0)
        #: L159f4 r1 : borne du bail sur l'horloge monotone de cet hôte, prise
        #: après la réponse de la base (prise ou renouvellement + ttl) : jamais
        #: avant l'échéance de la base, quel que soit l'écart des horloges
        self.lease_held_until = self._held_until()
        #: L159f4 r2 : le fil de renouvellement du tour (son renouvellement en
        #: cours peut encore prolonger `lease_held_until` après la fin du tour)
        self.heartbeat_thread: threading.Thread | None = None
        #: r3 : les appels de renouvellement détachés (sans réponse dans
        #: `call_budget()`) : l'un d'eux peut encore prolonger le bail en base
        self.renewals: list[threading.Thread] = []
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
        #: auteur du résumé de reprise : `agent` (son propre résumé) ou `ameesh`
        #: (brief déterministe d'une bascule de compte, L39) ; voir
        #: `adapters.resume_prompt`
        self.resume_origin = "agent"
        self.session_turns = 0
        self.session_tokens = 0.0
        self.last_turn_seconds = 0.0
        self.last_output = ""
        #: L218 : réponse finale du dernier tour (`final` du flux, None si le
        #: flux n'en donne pas), et, pour un tour de résumé, tout son affichage
        #: (repli d'un flux sans `final`, texte brut)
        self.last_final: str | None = None
        self.summary_display: list[str] = []
        #: plafond de contexte (L60) : jetons d'entrée relus (cache compris) par
        #: le dernier tour inscrit au grand livre, et sa session
        self.last_turn_reread = 0
        self.last_turn_reread_session: str | None = None
        #: L217 : relevé du flux du dernier tour, fil principal (la mesure du
        #: plafond pendant le tour) et sous-agents (pour information)
        self.last_turn_main_reread = 0
        self.last_turn_subagent_reread = 0
        #: L217b : la mesure de la rotation entre deux tours, celle du plafond
        #: (fil principal relevé dans le flux), et sa session ; None = pas de
        #: relevé d'appel dans le flux (harnais sans usage par appel, tour de
        #: résumé, worker relancé) : repli sur le grand livre
        self.last_turn_context: int | None = None
        self.last_turn_context_session: str | None = None
        #: plafonds du tour (L105) : le tour suivant reprend le travail d'un
        #: tour clos (raison), et la rotation forcée par le plafond de contexte
        #: atteint pendant un tour (raison) attend le prochain passage entre deux tours
        self.suite: str | None = None
        self.rotation_forcee: str | None = None
        #: raison de la clôture du dernier tour (L105), None s'il a fini seul
        self.last_turn_closed: str | None = None
        #: `ameesh restart` (L26) : arrêt du tour en cours, puis session neuve
        self.restarting = threading.Event()
        #: L159f : porte d'hôte — raison de l'arrêt du tour en cours parce que
        #: l'hôte se retire (`HOST_SAFE_POINT`, échéance, arrêt), None sinon
        self.host_yield: str | None = None
        #: comptes multiples (L30, 0027) : compte choisi pour le prochain tour, et
        #: compte imposé au tour de résumé d'une bascule (l'ancien compte)
        self._account = None
        #: #1469 : le marqueur comptable du tour lancé par CE worker
        self._compta_travail: dict | None = None
        self._account_override = None
        #: L242b (3) : harnais emprunté par un agent critique au seuil sur le
        #: sien, `(harnais, compte ou None)`, et harnais imposé au tour de
        #: résumé d'une rotation de harnais
        self._borrowed = None
        self._harness_override = None
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
        #: #811 (B1) : un lot secondaire déclaré dont le dossier manque ne
        #: bloque que LUI — échéance, délai croissant et dernier chemin connu
        #: par lot (`_lot_wd_path` : le dossier revenu débloque aussitôt), et
        #: `_lot_workdir_secondary` dit que l'échec courant ne relève pas du
        #: blocage global « dossier absent » (L35), réservé au cwd primaire
        self._lot_wd_until: dict = {}
        self._lot_wd_delay: dict = {}
        self._lot_wd_path: dict = {}
        self._lot_workdir_secondary = False
        self._lot_workdir_path: str | None = None
        self._lot_workdir_error = ""
        #: panne de base (L72) : attente croissante entre deux essais, consigne
        #: consommée par un tour qui n'a pas pu partir (à remettre en attente
        #: au retour de la base), statut « base injoignable » posé, et session
        #: ouverte par le harnais mais pas encore enregistrée au registre
        self._reprise = Reprise(getattr(runner, "db_retry_max", 60.0))
        self._consigne_a_restaurer = False
        self._base_marquee = False
        #: (session, compte, harnais, dossier de son ouverture)
        self._session_en_attente: tuple[str, str | None, str | None, str | None] | None = None
        #: hôte non prêt (L106) : binaire ou interpréteur du harnais
        #: introuvable — une erreur de l'HÔTE, jamais comptée comme un échec
        #: rapide de l'agent (L48). Blocage hérité d'un exécuteur précédent
        #: repris tel quel, levé dès que le harnais est de nouveau trouvé.
        self._host_blocked = (agent.get("status") == "blocked"
                              and agent.get("status_text") == self.HOST_STATUS
                              and str(agent.get("last_error") or "").startswith(
                                  self.HOST_ERROR))
        self._host_reason = ""
        self._host_delay = 0.0
        self._host_next = 0.0
        #: L226b : `status_text` du blocage « auth_blocked » posé (ou hérité
        #: d'un exécuteur précédent), '' hors blocage
        self._auth_status = (agent.get("status_text") or "") if (
            agent.get("status") == "blocked"
            and str(agent.get("status_text") or "").startswith(auth_failure.STATUS_PREFIX)
            and str(agent.get("last_error") or "").startswith(auth_failure.STATUS_PREFIX)) \
            else ""
        #: dernière résolution du binaire journalisée (L106)
        self.resolution_seen = ""
        #: L106 : arrêt propre demandé par la garde d'alimentation (texte de la
        #: raison) ; le tour en cours est arrêté au point sûr, la consigne
        #: remise en attente, le statut « hôte non prêt » posé avant de rendre
        #: le bail
        self._power_texte = ""
        #: L106 : le bail repris était celui d'une session `attach` morte (bail
        #: expiré sans être rendu) : un tour de reprise sur la même session
        self._reprise_attach = bool(str(agent.get("lease_owner") or "").startswith("attach:"))
        self._resume_load()

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
            log("[%s] thread unavailable: %s" % (self.name, exc))

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
            if result.get("expires") is not None or "error" in result:
                # L159f4 r3/r4 : même arrivée après `call_budget()` (appel
                # détaché), et même en erreur (la base a pu valider avant
                # que la réponse se perde), la borne locale du bail va
                # jusqu'à ce moment + ttl ; seul « bail perdu » ne prolonge rien
                self.lease_held_until = max(self.lease_held_until, self._held_until())

        thread = threading.Thread(target=call, daemon=True)
        self.renewals = [t for t in self.renewals if t.is_alive()] + [thread]
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
            self.stop_group_now("lease deadline reached", grace=0, hard=True)
            return False
        answered, expires, error = self._renew_call()
        if not answered:
            self.lease_lost.set()
            self.stop_group_now("renewal unanswered past the lease",
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
                echec = ("[%s] heartbeat: %s — failure %d, lease still valid for %ds: "
                         "the turn continues, retrying soon"
                         % (self.name, db_mod.explain(error), self.renew_failures,
                            max(0, int(reste))))
                definitive = reste <= 0
            else:
                echec = ("[%s] heartbeat: database unreachable (%s) — failure %d/%d"
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
            self.stop_group_now("database unreachable", grace=0, hard=True)
            log_async(echec)
            return False
        if expires is None:
            self.lease_lost.set()
            self.stop_group_now("lease lost (epoch %d)" % self.epoch, grace=0, hard=True)
            return False
        if self.renew_failures:
            log_async("[%s] heartbeat: database reachable again, lease renewed after "
                      "%d failure(s)" % (self.name, self.renew_failures))
        self.renew_failures = 0
        self.lease_deadline = float(expires)
        self.lease_held_until = self._held_until()
        return True

    def _held_until(self) -> float:
        return time.monotonic() + float(self.runner.lease_ttl)

    def release_lease(self) -> bool:
        """Rend le bail ; False quand la base ne l'a pas pris (DbError) : le
        bail reste tenu jusqu'à son échéance (`lease_held_until`, plus
        renouvelé). Perdu ou déjà repris par un autre : True, il n'est plus
        à nous. Le retour est tenté même `lease_lost` posé (r1) : des pannes
        au renouvellement le posent sans que la base ait repris le bail ; le
        retour, gardé par l'epoch, ne touche pas celui d'un autre."""
        # L76 : rien ne survit au bail, pas même un processus résident inactif
        self._close_resident("lease released")
        self.watchdog_stop.set()  # le veilleur n'a plus de bail à défendre
        # L113 : le travail part au serveur sous le bail, puis le dossier est
        # effacé (même bail perdu : rien ne doit rester sur l'appareil)
        self._depot_push(final=True)
        try:
            registry.release(self.db, self.name, self.runner.runner_id, self.epoch)
        except db_mod.DbError:
            return False
        return True

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
        signal_group = getattr(proc, "signal_group", None)
        if signal_group is not None:
            # L76 : le chef d'un résident — son groupe tant qu'il est à nous
            if pgid is None or not signal_group(sig):   # le harnais seul
                proc.send_signal(sig)
            return
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

    def _reap_group(self, pgid: int | None, skip: int | None) -> None:
        """Zombies adoptés du groupe (l'exécuteur est sous-moissonneur de ses
        tours) : moissonnés, sinon ils gardent le groupe « vivant » pour
        `killpg(…, 0)`. Jamais `skip`, le harnais, qu'attend son `Popen`."""
        tracker = getattr(self.runner, "background", None)
        if pgid is None or tracker is None or not tracker.subreaper:
            return
        from . import platform as os_layer
        try:
            table = os_layer.process_table()
        except os_layer.NotAvailable:
            return
        for p in table:
            if p["pgid"] == pgid and p["zombie"] and p["pid"] != skip:
                os_layer.reap(p["pid"])

    def terminate(self, grace: float | None = None, hard: bool = False,
                  group: bool = True) -> None:
        """Arrête le harnais et tout son groupe.

        `group=False` (tour clos à un point sûr, L105) : le harnais SEUL est
        arrêté (SIGTERM, grâce, puis SIGKILL) ; le travail lancé en fond
        pendant le tour garde son délai de grâce (fin de `_run_turn`).

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
        if (not group and not hasattr(proc, "reap")
                and getattr(self, "_background_handles", None) is not None):
            # Safe-point closure polls/reaps the harness here, before the main
            # stream reader reaches its wait. Capture while it still owns PGID.
            captured = self.runner.background.capture_before_wait(proc, wait=False)
            for pid, handle in captured.items():
                if pid in self._background_handles:
                    handle.close()
                else:
                    self._background_handles[pid] = handle
        pgid = self._group(proc) if group else None
        if proc.poll() is None and not hard:
            self._signal_group(pgid, proc, signal.SIGTERM)
            deadline = time.monotonic() + max(0.0, self.TERMINATE_GRACE if grace is None else grace)
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.05)
        if not group:
            if proc.poll() is None:
                self._signal_group(None, proc, signal.SIGKILL)
                log_async("[%s] harness still alive: SIGKILL (harness only)" % self.name)
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            return
        if hasattr(proc, "reap"):
            # L76 : le chef d'un résident, jamais moissonné ailleurs — son
            # groupe tué tant que son numéro est à nous, puis lui moissonné
            if proc.reap() is None:
                log_async("[%s] resident leader still alive after SIGKILL (kernel state?)"
                          % self.name)
            return
        if not self._group_alive(pgid) and proc.poll() is not None:
            # Groupe mort **et** enfant direct moissonné : rien à faire. Si le
            # pid n'est pas un chef de groupe (groupe inconnu) mais que l'enfant
            # vit encore, on ne doit pas sortir sans lui envoyer le SIGKILL.
            return
        # Le signal part avant le journal : un print+flush sur le chemin
        # critique laissait le temps à un remplaçant de réclamer sous charge.
        self._signal_group(pgid, proc, signal.SIGKILL)
        log_async("[%s] harness group still alive: SIGKILL" % self.name)
        # SIGKILL est asynchrone. On moissonne d'abord l'enfant direct (sinon
        # son zombie garde le groupe « vivant »), puis on attend la disparition
        # du groupe entier, dans le même budget de 2 s.
        fin = time.monotonic() + 2.0
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(0.0, fin - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        moisson = 0.0
        while time.monotonic() < fin and self._group_alive(pgid):
            if time.monotonic() >= moisson:
                # sous-moissonneur : les membres tués sont nos zombies
                self._reap_group(pgid, proc.pid)
                moisson = time.monotonic() + 0.05
            time.sleep(0.005)
        if self._group_alive(pgid):
            log_async("[%s] group still alive after SIGKILL (kernel state?)" % self.name)

    def stop_group_now(self, reason: str, *, grace: float | None = None,
                       hard: bool = False, group: bool = True) -> bool:
        """Point de passage **unique** de tout arrêt de harnais (0018, verdicts codex3).

        1. Le signal part d'abord (`terminate`, qui n'écrit rien avant) ;
        2. le journal vient ensuite, par la file bornée `log_async` ;
        3. si le `Popen` n'est pas encore publié, on pose `stop_requested` et
           `_stop_reason` : `kill_if_stop_requested()` recontrôlera juste après
           la publication (courses B5a-P et L11 B2).

        Tous les déclencheurs — échéance, bail perdu, arrêt de l'exécuteur,
        préemption, et demain la garde de budget (L13) — passent par ici : un
        nouveau chemin hérite des sondes paramétrées. `group=False` : le
        harnais seul (tour clos à un point sûr ; voir `terminate`).
        """
        with self.lock:
            proc = self.proc
            resident = self.resident if proc is None and hard else None
        self._stop_reason = reason
        if resident is not None:
            # L76 : un processus résident entre deux tours meurt avec le bail,
            # même inactif — le signal d'abord, sans E/S
            resident.kill_now()
        if proc is None:
            # Le latch ne sert qu'à la course de publication en cours : il est
            # consommé par le recontrôle, puis remis à zéro au tour suivant
            # (verdict codex3 L11 B5 : un latch permanent tuerait la reprise).
            self.stop_requested.set()
            log_async("[%s] stop requested (%s): harness not yet published"
                      % (self.name, reason))
            return False
        self.terminate(grace=grace, hard=hard, group=group)
        log_async("[%s] harness stopped (%s)" % (self.name, reason))
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
        self.stop_group_now(self._stop_reason or "stop", grace=0, hard=True)
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
        return self.stop_group_now("lease lost", grace=0, hard=True)

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
        return self.stop_group_now("preemption", grace=0, hard=True)

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

    def renew_wait(self, cap: float) -> float:
        """Attente qui laisse un renouvellement **avant** l'échéance du bail.

        `wait_timeout` borne l'attente à l'échéance même : une reprise plus
        longue que le bail (#1270, jusqu'à 600 s pour un bail de 300 s)
        dormait jusqu'à la deadline, puis `renew()` était refusé
        (`deadline_passed`) — le worker rendait le bail au lieu de reprendre
        (R1 cx3 B1). On découpe donc au pas du battement de tour (`ttl/3`),
        sans plancher : la tranche vaut `min(cap, ttl/3)` et ne dépasse jamais
        l'échéance du bail. Chaque tranche est suivie d'un `renew()` par
        l'appelant (boucle de reprise, boucle principale).
        """
        pas = (self.runner.lease_ttl / 3.0) if self.runner.lease_ttl > 0 else 3.0
        if self.lease_deadline > 0:
            pas = min(pas, max(0.0, self.lease_deadline - time.time()))
        return max(0.0, min(float(cap), pas))

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
            self.stop_group_now("lease deadline reached", grace=0, hard=True)
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

    def _moved_before_turn(self, spec: dict) -> bool:
        """L147b (codex r4 de #220, r1 de #283) : le placement est relu après
        `pick()`, juste avant le tour de travail — une rotation (taille, lot,
        bascule de compte dans `pick()`) a pu y faire un tour de résumé, pendant
        lequel le canon a placé l'agent ailleurs ; aucun tour de travail ne part
        alors d'ici. La consigne prise par `pick()` est remise en attente (le
        courrier n'est pas consommé par `pick()`), y compris si la base tombe
        pendant ce contrôle (`_consigne_a_restaurer`, repris au retour de la
        base). Vrai : le worker rend son bail."""
        self._consigne_a_restaurer = spec.get("kind") == "prompt"
        ailleurs = self.placed_elsewhere()
        if ailleurs and self._consigne_a_restaurer:
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
        self._consigne_a_restaurer = False
        if not ailleurs:
            return False
        log("[%s] placed on host %s, not %s, before the work turn: no turn here, "
            "lease released" % (self.name, ailleurs, self.runner.host))
        return True

    def placed_elsewhere(self) -> str:
        """L147: the host the registry places this agent on, when it is not
        this runner's host ('' otherwise, or for an agent without a host)."""
        agent = registry.get(self.db, self.name) or {}
        host = agent.get("host") or ""
        return host if host and host != self.runner.host else ""

    def peek(self) -> bool:
        agent = registry.get(self.db, self.name)
        if agent is None or agent.get("status") == "stopped":
            return False
        if agent.get("pending_prompt"):
            return True
        # L125 : un courrier passif (accusé, copie, diffusion) n'ouvre pas de tour
        if mail.unread_active(self.db, self.name, limit=1):
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
                self.stop_group_now("preemption", grace=1.0)
                return
            if self.restarting.is_set():
                # `ameesh restart` (L26) : même point de passage, même grâce
                self.stop_group_now("restart requested", grace=1.0)
                return

    def yield_to_host(self, reason: str) -> None:
        """Porte d'hôte (L112) : l'échéance du retrait est atteinte, ou l'hôte
        s'arrête — le tour en cours s'arrête (point de passage unique, grâce
        brève) ; sa consigne repartira en attente, la session est gardée."""
        self.host_yield = reason
        self.wake.set()
        threading.Thread(target=self.stop_group_now, args=("l'hôte se retire : %s" % reason,),
                         kwargs={"grace": 1.0}, daemon=True,
                         name="%s-retrait" % self.name).start()

    def _leave_for_host(self) -> None:
        """Porte d'hôte fermée (L112) : plus aucun tour ici. État de la
        session écrit, consigne éventuelle remise en attente, statut dit ;
        le bail est rendu par `run()` en sortant. Écritures au mieux : si le
        réseau est déjà coupé, le bail échoit seul et le courrier sera
        re-livré, signalé."""
        gate = getattr(self.runner, "host_gate", None)
        etat = gate.current if gate is not None else None
        texte = "hôte indisponible (%s, %s) : reprise au retour de l'hôte" % (
            etat.state if etat else "?", etat.reason if etat else "?")
        log_async("[%s] l'hôte se retire : bail rendu (%s)" % (self.name, texte))
        if self.lease_lost.is_set():
            return
        try:
            if self._session_en_attente is not None:
                self._enregistre_session()
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            row = registry.get(self.db, self.name) or {}
            if row.get("status") in ("running", "idle", "queued"):
                registry.set_status(self.db, self.name, "queued", status_text=texte)
        except db_mod.DbError as exc:
            log_async("[%s] état de retrait non écrit (%s)" % (self.name, db_mod.explain(exc)))
    def _audit_deadline_monitor(self, done: threading.Event, deadline: float) -> None:
        """L234 : échéance monotone indépendante du flux et de la base.

        Le scope a aussi RuntimeMaxSec : il arrête les descendants qui ont
        quitté le groupe POSIX, même si l'exécuteur meurt.
        """
        if done.wait(max(0.0, deadline - time.monotonic())) or done.is_set():
            return
        self._audit_guard_error = "hard cap of the auditor turn reached"
        self.stop_group_now(self._audit_guard_error, grace=0, hard=True)

    #: L159f : le tour s'arrête au point sûr (le harnais seul, son travail de
    #: fond garde le délai de grâce, borné par l'échéance du retrait)
    HOST_SAFE_POINT = "safe point"

    def yield_to_host(self, reason: str) -> None:
        """L159f : l'échéance du retrait est atteinte, ou l'hôte s'arrête —
        le tour en cours s'arrête, groupe compris (grâce brève) ; sa consigne
        repartira en attente, la session est gardée. Appelé par le fil de la
        porte : l'arrêt part dans son propre fil."""
        self.host_yield = reason
        self.wake.set()
        threading.Thread(target=self.stop_group_now, args=("the host is withdrawing: %s" % reason,),
                         kwargs={"grace": 1.0}, daemon=True,
                         name="%s-withdraw" % self.name).start()

    def _leave_for_host(self) -> None:
        """L159f : porte d'hôte fermée — plus aucun tour ici. Session écrite,
        consigne éventuelle remise en attente, statut dit ; le bail est rendu
        par `run()` en sortant, et l'agent sera réclamé de nouveau au retour
        de l'hôte (ici, ou ailleurs). Écritures au mieux : en échec, le bail
        échoit seul."""
        gate = getattr(self.runner, "host_gate", None)
        etat = gate.current if gate is not None else None
        texte = "host unavailable (%s, %s): resumes when the host is back" % (
            etat.state if etat else "?", etat.reason if etat else "?")
        log_async("[%s] the host is withdrawing: lease released (%s)" % (self.name, texte))
        if self.lease_lost.is_set():
            return
        try:
            if self._session_en_attente is not None:
                self._enregistre_session()
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            row = registry.get(self.db, self.name) or {}
            if row.get("status") in ("running", "idle", "queued"):
                registry.set_status(self.db, self.name,
                                    "queued" if row.get("pending_prompt") else "idle",
                                    status_text=texte)
        except db_mod.DbError as exc:
            log_async("[%s] withdrawal state not written (%s)" % (self.name, db_mod.explain(exc)))

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
        self._resume_drop()
        self.session_turns = 0
        self.session_tokens = 0.0
        self.last_turn_reread = 0
        self.last_turn_context = None
        self.nudged = False
        self.suite = self.rotation_forcee = None  # L105 : la session neuve part du brief
        self.agent = registry.get(self.db, self.name) or self.agent
        log_async("[%s] restart applied: session forgotten, brief first" % self.name)
        self._fil_note(
            "Restart applied: the old session (%s) is forgotten; the new "
            "session opens on the stored brief." % (row.get("forgotten_session") or "none"),
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
        # #706 : la latence d'un tour autorisé ne suffit pas à oublier la
        # session. Le plancher suit aussi un plafond propre à l'agent ; le
        # facteur 2 laisse une marge pour atteindre le point sûr après la
        # borne du tour. Sans plafond, garder le seuil de latence configuré.
        latency_threshold = max(self.runner.session_max_turn_seconds,
                                2 * self.turn_max_seconds())
        return (self.session_tokens >= self.runner.session_max_tokens
                or self.last_turn_seconds >= latency_threshold)

    def context_max_tokens(self) -> int:
        """Plafond de contexte effectif (L60) : réglage de l'agent
        (`ameesh set context_max_tokens=…`), sinon défaut de l'exécuteur
        (`AMEESH_CONTEXT_MAX_TOKENS`). 0 = désactivé."""
        from .exploitation import effective_context_max
        return effective_context_max(self.cfg, self.agent)

    def turn_max_seconds(self) -> int:
        """Durée maximale d'un tour (L105) : réglage de l'agent
        (`ameesh set turn_max_seconds=…`), sinon défaut de l'exécuteur
        (`AMEESH_TURN_MAX_SECONDS`). 0 = sans limite."""
        from .exploitation import effective_turn_max_seconds
        return effective_turn_max_seconds(self.cfg, self.agent)

    def turn_mail_max(self) -> int:
        """Messages remis par le hook pendant un tour (L105) : réglage de
        l'agent (`ameesh set turn_mail_max=…`), sinon défaut de l'exécuteur
        (`AMEESH_TURN_MAIL_MAX`). 0 = sans borne."""
        from .exploitation import effective_turn_mail_max
        return effective_turn_mail_max(self.cfg, self.agent)

    def turn_grace_seconds(self) -> int:
        """Délai de grâce du travail de fond d'un tour : réglage de l'agent
        (`ameesh set turn_grace_seconds=…`), sinon défaut de l'hôte
        (`AMEESH_TURN_GRACE_SECONDS`, 20 min). 0 = nettoyage immédiat."""
        from .exploitation import effective_turn_grace_seconds
        return effective_turn_grace_seconds(self.cfg, self.agent)

    def _urgent_end(self) -> str:
        """Raison d'un arrêt d'urgence du tour qui s'achève, ou '' : fin
        normale (le harnais a fini, ou il a été clos à un point sûr), dont le
        travail de fond a son délai de grâce."""
        if self.lease_lost.is_set() or self.deadline_passed():
            return "lease lost"
        if self.preempting.is_set():
            return "preemption"
        if self.restarting.is_set():
            return "restart requested"
        if self.host_yield is not None and self.host_yield != self.HOST_SAFE_POINT:
            return "the host is withdrawing (%s)" % self.host_yield
        if self._power_texte:
            return "critical battery"
        if self.runner.stop.is_set():
            return "runner shutdown"
        return ""

    def turn_close_due(self, started: float, reread: int,
                       now: float | None = None) -> str | None:
        """Le tour en cours doit-il se clore au prochain point sûr ? (L105)

        Deux plafonds : les jetons relus depuis le début du tour (somme des
        appels, la mesure du plafond de contexte L60) et la durée du tour.
        Rend la raison, ou None."""
        plafond = self.context_max_tokens()
        if plafond > 0 and reread >= plafond and self.session_policy() != "jamais":
            return "context cap: %d tokens reread in the turn (cap %d)" % (
                reread, plafond)
        duree_max = self.turn_max_seconds()
        now = time.time() if now is None else now
        # L159f : le temps gelé par la garde de l'hôte ne compte pas
        ecoule = now - started - self.runner.gate_frozen_between(started, now)
        if duree_max > 0 and ecoule >= duree_max:
            return "maximum turn duration: %ds (cap %ds)" % (int(ecoule), duree_max)
        return None

    def last_turn_context_reread(self) -> tuple[int, str | None]:
        """Jetons relus au dernier tour au sens du plafond de contexte, et leur
        session (L217b) : le fil principal relevé dans le flux, le compteur
        même du plafond pendant le tour (les sous-agents n'y sont pas, ni
        pour un tour clos avant son `result`) ; sans relevé dans le flux, le
        dernier tour inscrit au grand livre (L60)."""
        if self.last_turn_context is not None:
            return self.last_turn_context, self.last_turn_context_session
        return self.last_turn_reread, self.last_turn_reread_session

    def context_rotation_due(self) -> bool:
        """Le dernier tour a-t-il relu plus que le plafond de contexte ? (L60)

        La mesure est celle du plafond du tour (L217b) : les jetons relus par
        le fil principal, relevés dans le flux ; à défaut, celle de l'alerte
        `session_too_big` (entrée + entrée relue en cache du dernier tour
        inscrit au grand livre). Chaque étape
        d'un tour relit tout le contexte : une session qui grossit coûte de
        plus en plus cher à chaque tour, et l'alerte seule n'y changeait rien.
        Ni minimum de tours, ni rotation pendant un tour ; la politique
        `jamais` s'en dispense ; il faut une session courante, et que le
        relevé soit bien celui de cette session.
        """
        if self.proc is not None or self.session_policy() == "jamais":
            return False
        plafond = self.context_max_tokens()
        relus, relus_session = self.last_turn_context_reread()
        if plafond <= 0 or relus < plafond:
            return False
        session = self.current_session()
        if not session:
            return False
        return relus_session in (None, session)

    def _session_history(self, session_id: str | None, resume: str) -> None:
        """Garde l'ancien id de session et son résumé (audit, 0018) ; le
        résumé entier, déjà borné par `adapters.bound_summary` (L218)."""
        entry = {
            "ts": time.time(), "session": session_id, "turns": self.session_turns,
            "tokens": self.session_tokens,
            "last_turn_s": round(self.last_turn_seconds, 1), "resume": resume,
        }
        try:
            with open(self._path("session-history.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    #: le résumé de reprise d'une session oubliée, en attente de la session
    #: neuve qu'il ouvre (#389 r1 : il survit à une panne de l'exécuteur)
    RESUME_PENDING = "resume-pending.json"

    def _resume_keep(self, resume: str, origin: str) -> bool:
        """Le résumé de reprise retenu pour la session neuve : en mémoire et
        dans l'état local, AVANT l'oubli de l'ancienne session — une panne
        entre l'oubli et le lancement ne le perd pas (#389 r1). Faux s'il
        n'a pas pu être écrit : l'appelant garde alors l'ancienne session
        (#389 r2)."""
        path = self._path(self.RESUME_PENDING)
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "resume": resume, "origin": origin}, fh,
                          ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(path + ".tmp", path)
        except OSError as exc:
            log_async("[%s] resume summary not kept on disk (%s)" % (self.name, exc))
            try:
                os.unlink(path + ".tmp")
            except OSError:
                pass
            return False
        self.resume_summary, self.resume_origin = resume, origin
        return True

    def _resume_drop(self) -> None:
        """Le résumé de reprise a ouvert la session neuve, ou n'a plus lieu
        d'être : oublié, en mémoire et sur disque."""
        self.resume_summary = ""
        try:
            os.unlink(self._path(self.RESUME_PENDING))
        except OSError:
            pass

    def _resume_load(self) -> None:
        """Au démarrage du worker : le résumé de reprise resté en attente, si
        aucune session n'a été ouverte depuis (sinon il est périmé)."""
        try:
            with open(self._path(self.RESUME_PENDING), encoding="utf-8") as fh:
                pending = json.load(fh)
        except (OSError, ValueError):
            return
        resume = pending.get("resume") if isinstance(pending, dict) else None
        if not isinstance(resume, str) or not resume or self.current_session():
            self._resume_drop()
            return
        self.resume_summary = resume
        self.resume_origin = "ameesh" if pending.get("origin") == "ameesh" else "agent"
        log_async("[%s] pending resume summary recovered (%d characters)"
                  % (self.name, len(resume)))

    def maybe_rotate(self) -> bool:
        """Résume la session courante puis en ouvre une neuve (0018, R19).

        Le résumé est produit **dans** la session (un tour), écrit dans le fil
        (R12), l'ancien id est conservé dans l'historique de l'état, puis la
        session est oubliée : le prochain tour repart d'une session neuve,
        préfixé par le résumé. Ne se déclenche jamais pendant un tour.

        L105 : un tour clos par le plafond de contexte force la rotation au
        premier passage (le grand livre d'un tour interrompu peut ne rien dire).

        Sous la pression de l'hôte, une rotation due ATTEND la fin de la
        pression (le tour de résumé ne partirait pas) : elle n'est ni tentée
        ni annulée à chaque passage (2026-10-10 : 62 rotations annulées en une
        soirée), et la ligne de fin de l'épisode de pression la cite.
        """
        if self.proc is None and (self.rotation_forcee or self.context_rotation_due()
                                  or self.rotation_due()):
            if self._held_by_pressure(self.runner.host_pressure()):
                self.runner.pressure_delayed(self.name, rotation=True)
                return False
            if not self._rotation_allowed():
                return False
        if self.rotation_forcee and self.proc is None:
            raison, self.rotation_forcee = self.rotation_forcee, None
            if self.session_policy() != "jamais" and self.current_session():
                return self._rotate(raison)
        if self.context_rotation_due():
            return self._rotate("context cap: %d tokens reread in the last turn "
                                "(cap %d)" % (self.last_turn_context_reread()[0],
                                                  self.context_max_tokens()))
        if not self.rotation_due():
            return False
        return self._rotate("turns=%d, tokens=%.0f, last turn=%.0fs" % (
            self.session_turns, self.session_tokens, self.last_turn_seconds))

    def _rotation_allowed(self) -> bool:
        """L226b : le tour de résumé d'une rotation est un appel de modèle. Il
        passe les gardes d'un tour (budget et plafonds du compte, santé de
        l'authentification) et tourne sous le compte de la session ; sinon la
        rotation attend : la bascule du prochain tour (`continuity`) résume
        sous l'ancien compte s'il le peut encore, ou ouvre un brief déterministe."""
        if not self.budget_ok() or not self.auth_ready():
            return False
        if self.turn_harness() != account_turn.session_harness(self):
            # #389 r1 : le tour partirait sous un autre harnais que celui de
            # la session (repli L242b, ou retour) — c'est `continuity` qui
            # change de harnais, avec le résumé sous l'ancien
            return False
        compte = account_turn.for_turn(self)
        if compte is account_turn.INVALID:
            return False
        return account_turn.session_on(self, compte)

    def turn_summary(self) -> str:
        """Le résumé rendu par le dernier tour de résumé (L218) : sa réponse
        finale entière (à défaut, tout son affichage), bornée par
        `adapters.bound_summary` — jamais la fin de l'affichage, qui perdait
        la tête du résumé et finissait sur « == fin du tour : ».

        Une réponse finale vide n'a pas de résumé (verdict codex #178) : la
        rotation est annulée, la bascule de compte prend le brief L39. Le
        repli sur l'affichage ne vaut que pour un flux qui ne donne jamais de
        `final`, et les marqueurs de fin de tour de l'exécuteur en sont ôtés."""
        if self.last_final is not None:
            return adapters.bound_summary(self.last_final)
        texte = "\n".join(ligne for ligne in self.summary_display
                          if not ligne.startswith(adapters.END_OF_TURN_MARK))
        return adapters.bound_summary(texte)

    def _rotate(self, raison: str) -> bool:
        """Le mécanisme de rotation (L11) : résumé dans la session, puis oubli."""
        ancienne = self.current_session()
        log_async("[%s] session rotation (%s)" % (self.name, raison))
        self._refus_pression = False
        ok = self.run_turn({"kind": "prompt", "prompt": adapters.SUMMARY_PROMPT, "ids": []})
        if not ok and self._refus_pression:
            # la pression est venue entre le choix et le lancement : la
            # rotation attend sa fin (ligne de fin de l'épisode), sans trace
            # au fil à chaque passage
            self.runner.pressure_delayed(self.name, rotation=True)
            return False
        if not ok:
            # Résumé partiel, bail perdu pendant le tour, claim remplaçant : on ne
            # touche à rien. Effacer la session ici effacerait celle du remplaçant
            # (verdict codex3 L11 B3).
            log_async("[%s] rotation cancelled: the summary turn did not complete" % self.name)
            self._fil_note("Session rotation cancelled: the summary turn did not "
                           "complete. The old session is kept; will try "
                           "again later.",
                           meta={"action": "rotation", "etat": "annulee"})
            return False
        resume = self.turn_summary()
        if not resume:
            log_async("[%s] rotation cancelled: empty summary" % self.name)
            self._fil_note("Session rotation cancelled: empty summary. The old "
                           "session is kept.",
                           meta={"action": "rotation", "etat": "annulee"})
            return False
        self._session_history(ancienne, resume)
        # #538 : le résumé de l'agent est gardé BRUT ; les règles de travail du
        # canon sont injectées à la construction du prompt de la session neuve
        # (`_resume_prompt`), donc relues à ce moment-là : une fiche retirée ne
        # reste pas dans un résumé en attente.
        if not self._resume_keep(resume, "agent"):
            self._fil_note("Session rotation cancelled: the resume summary could not "
                           "be kept on disk. The old session is kept.",
                           meta={"action": "rotation", "etat": "annulee"})
            return False
        # L'effacement est fencé par le bail : si un remplaçant a réclamé l'agent
        # entre-temps, sa session n'est pas touchée.
        if not registry.clear_session(self.db, self.name, self.runner.runner_id, self.epoch):
            self._resume_drop()
            log_async("[%s] rotation cancelled: lease lost before the session was cleared"
                      % self.name)
            self._fil_note("Session rotation cancelled: the lease changed hands "
                           "before clearing; nothing was touched.",
                           meta={"action": "rotation", "etat": "annulee"})
            return False
        try:
            os.unlink(self._path("session"))
        except OSError:
            pass
        self.agent = registry.get(self.db, self.name) or self.agent
        self.session_turns = 0
        self.session_tokens = 0.0
        self.last_turn_reread = 0
        self.last_turn_reread_session = None
        self.last_turn_context = self.last_turn_context_session = None
        self._fil_note(
            "Session rotation (%s): the old id (%s) is kept in "
            "`session-history.jsonl`; the new session starts from the summary below.\n\n%s"
            % (raison, ancienne or "new", resume),
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
            log_async("[%s] turn lot unreadable (%s)" % (self.name, exc))
            return None
        if len(assignes) == 1:
            return str(assignes[0]["id"])
        # #408 : les lots en relecture ou en attente d'un humain ne comptent
        # pas ; l'unique lot sur lequel l'agent peut avancer est celui du tour
        actifs = [a for a in assignes if a.get("state") in ("intake", "build")]
        if len(actifs) == 1:
            return str(actifs[0]["id"])
        return None

    def _note_delegate_turn(self, spec: dict) -> None:
        """L40 (0030) : le tour qui commence compte pour les lots délégués à
        cet agent — le lot du tour et ceux de ses messages. Noté une fois par
        délégation (registre et journal du lot) : à l'échéance, le lot ne
        reviendra pas au délégant. L157 : le même tour date le début mesuré
        des lots assignés à cet agent qui n'en ont pas. Jamais bloquant pour
        le tour."""
        from . import estimates
        from . import work as work_mod

        lots = {str(spec["lot"])} if spec.get("lot") else set()
        try:
            ids = [int(i) for i in spec.get("ids") or []]
            if ids:
                lots.update(storage.of(self.db).operations.message_lots(ids))
            marques = work_mod.mark_delegate_turn(self.db, self.name, lots)
        except (db_mod.DbError, ValueError) as exc:
            log_async("[%s] turn on delegated lot not recorded (%s)" % (self.name, exc))
            return
        if marques:
            log_async("[%s] turn on delegated lot(s): %s"
                      % (self.name, ", ".join("#%d" % i for i in marques)))
        # L157 : le premier tour de l'assigné sur un lot date le début du
        # travail (durée réelle = fusion − début), une seule fois. Not on a
        # mediated executor: `work.mark_started` is not in contract 1.1.
        if getattr(self.runner, "mediated", False):
            return
        try:
            debuts = estimates.mark_started(self.db, self.name, lots)
        except (db_mod.DbError, ValueError) as exc:
            log_async("[%s] lot start not recorded (%s)" % (self.name, exc))
            return
        if debuts:
            log_async("[%s] start measured: %s"
                      % (self.name, ", ".join("#%d" % i for i in debuts)))

    def lot_rotation_due(self, lot: str | None) -> bool:
        """Changement de session au changement de lot dû ? (politique `par-lot`)

        Seulement si le lot du tour est connu, que la session courante a un
        lot noté et qu'il diffère. Jamais pendant un tour. #408 : même sans
        session courante, celle mise de côté pour le lot du tour est reprise.
        """
        if not lot or self.proc is not None or self.session_policy() != "par-lot":
            return False
        courant = (self.agent or {}).get("session_work_item")
        return bool(courant) and str(courant) != str(lot)

    def rotate_for_lot(self, spec: dict | None) -> dict | None:
        """Avant un tour : passe à la session du lot du tour s'il change
        (0025 ; #408, une session par lot).

        La session courante est mise de côté pour son lot, sans résumé ni
        oubli : un retour sur ce lot (relecture, décision, déblocage) la
        reprendra avec `--resume`. Celle du lot du tour est reprise si elle a
        été ouverte sous le même harnais, le même compte et le même dossier ;
        sinon une session neuve s'ouvre sur le brief déterministe (L39) du
        lot. Bail perdu, base injoignable ou dossier absent : le tour ne part
        pas dans la session du lot précédent et sa consigne reste à livrer.
        """
        if not spec:
            return spec
        lot = spec.get("lot")
        self._workdir_lot = lot or self.agent.get("session_work_item")
        from . import projects as projects_mod
        multi_switch = (lot and self.proc is None
                        and len(projects_mod.agent_projects(self.agent)) > 1
                        and str(lot) != str(self.agent.get("session_work_item") or ""))
        if not self.lot_rotation_due(lot) and not multi_switch:
            return spec
        courant = (self.agent or {}).get("session_work_item")
        if not self._switch_lot(str(courant), str(lot)):
            return dict(spec, lot_switch_failed=True)
        return spec

    def _switch_lot(self, courant: str, lot: str) -> bool:
        """#408 : met de côté la session du lot `courant`, rend active celle
        du lot `lot` (ou aucune : session neuve sur le brief du lot)."""
        compte = account_turn.for_turn(self)
        nom = None if compte is None or compte is account_turn.INVALID else compte.name
        session = self.current_session()
        # le harnais que le tour lancera (L242b : celui qu'un agent critique
        # emprunte), pas celui de la fiche : une session n'est reprise que
        # sous le harnais qui l'a ouverte
        harnais = self.turn_harness()
        cwd = self._resolve_workdir(lot=lot)
        if not cwd:
            if self._lot_workdir_secondary:
                # #811 (B1) : ce lot attend son dossier ; l'agent garde ses
                # tours primaires (pas de blocage global « dossier absent »).
                self._lot_workdir_deferred(lot)
            else:
                self._workdir_missing(force_status=True)
            return False
        try:
            out = storage.of(self.db).lot_sessions.switch(
                self.name, self.runner.runner_id, self.epoch, to_lot=lot,
                harness=harnais, account=nom, cwd=cwd,
                current=session)
        except db_mod.DbError as exc:
            log_async("[%s] session switch to lot %s failed (%s)" % (self.name, lot, exc))
            return False
        if out is None:
            log_async("[%s] session switch to lot %s cancelled: lease lost" % (self.name, lot))
            return False
        repris = out.get("resumed")
        # L242b (3) : le marqueur local du harnais de la session suit la
        # session active (celui de la session reprise ; aucun sans session,
        # le tour qui l'ouvrira le note)
        self._state_note("session_harness", harnais if repris else "")
        if repris:
            self.write_session_file(repris)
            self.resume_summary = None
        else:
            try:
                os.unlink(self._path("session"))  # sinon `current_session` le relirait
            except OSError:
                pass
        self.agent = registry.get(self.db, self.name) or self.agent
        self.session_turns = 0
        self.session_tokens = 0.0
        self.last_turn_reread = 0
        self.last_turn_reread_session = None
        self.last_turn_context = self.last_turn_context_session = None
        if not repris:
            self.resume_summary = self._lot_brief(lot, courant, out)
            self.resume_origin = "ameesh"
        log_async("[%s] session of lot %s %s, lot %s: %s"
                  % (self.name, courant, "put aside" if out.get("parked") else "none",
                     lot, "resumed (%s)" % repris if repris else "new session"))
        self._fil_note(
            "Lot change %s → %s: %s; %s.%s" % (
                courant, lot,
                "session %s put aside for lot %s" % (out["parked"], courant)
                if out.get("parked") else "no session to put aside",
                "session %s of lot %s resumed" % (repris, lot) if repris
                else "new session for lot %s, opened on the resume brief" % lot,
                " Session %s of lot %s not resumed (%s)." % (
                    out["discarded"], lot,
                    "lot finished or no longer assigned to the agent"
                    if out.get("discard_reason") == "lot"
                    else "other harness, account or directory, or unknown origin")
                if out.get("discarded") else ""),
            meta={"action": "session-lot", "de": courant, "vers": lot,
                  "session": out.get("parked") or "", "reprise": repris or ""})
        return True

    def _lot_brief(self, lot: str, courant: str, out: dict) -> str | None:
        """Le brief déterministe (L39) d'une session neuve dédiée au lot `lot`."""
        try:
            from . import reprise, work as work_mod
            item = work_mod.get(self.db, int(lot)) if str(lot).isdigit() else None
            branche = (item or {}).get("branch")
            return reprise.deterministic_brief(
                self.cfg, self.db, self.agent, session=None,
                reason="new session dedicated to lot #%s" % lot,
                consigne="This session is dedicated to lot #%s%s. Your other lots keep "
                         "their own sessions (the one of lot #%s is put aside and will "
                         "be resumed when feedback arrives on it): work only on lot #%s "
                         "here, in its own worktree and branch. This turn's instruction "
                         "follows this brief." % (
                             lot, " (branch %s)" % branche if branche else "",
                             courant, lot))
        except Exception as exc:  # un brief manquant n'empêche pas la bascule
            log_async("[%s] lot brief unavailable (%s)" % (self.name, exc))
            return None

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
                log("[%s] moved directory ambiguous: %s" % (self.name, ", ".join(candidats)))
            return None
        candidat = candidats[0]
        if registry.cwd_used(self.db, candidat, self.name):
            log("[%s] moved directory %s already used by another agent"
                % (self.name, candidat))
            return None
        registry.upsert(self.db, self.name, cwd=candidat)
        self.agent["cwd"] = candidat
        log("[%s] moved working directory adopted: %s" % (self.name, candidat))
        self._fil_note("Moved working directory adopted: %s" % candidat,
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
        log_async("[%s] %s during %s — turns suspended, retrying in %ds "
                  "(failure %d, down for %ds)"
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
            log_async("[%s] database reachable again after %ds (%d failure(s)): resuming"
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
                log_async("[%s] %s: %s — retrying in %ds (failure %d)"
                          % (self.name, quoi, db_mod.explain(exc), int(attente),
                             reprise.echecs))
                self.runner.stop.wait(self.wait_timeout(attente))
                if not self.renew() or self.lease_lost.is_set():
                    raise
                continue
            bilan = reprise.retablie()
            if bilan:
                log_async("[%s] %s: database reachable again after %d failure(s)"
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
                    self.DB_STATUS, "%s : turn not started" % self.DB_ERROR, self.DB_ERROR)
        except db_mod.Unavailable:
            raise
        except db_mod.DbError:
            pass

    def _enregistre_session(self) -> None:
        """Session ouverte par le harnais pendant une panne : enregistrée au
        registre dès que possible (sinon le tour suivant reprendrait l'ancienne,
        `current_session` préférant le registre au fichier local)."""
        session, compte, harnais, dossier = self._session_en_attente
        registry.set_session(self.db, self.name, session, compte, harness=harnais, cwd=dossier)
        self._session_en_attente = None
        if self.agent is not None:
            self.agent["session_id"] = session
            self.agent["session_account"] = compte
        log_async("[%s] session %s recorded in the registry (delayed by an outage)"
                  % (self.name, session))

    def _ecarte_session_fille(self, refusee: str) -> None:
        """L166 : le harnais refuse de reprendre `refusee`, session fille
        (sous-agent, fourche) retenue à tort comme session de l'agent."""
        self._ecarte_session(
            refusee, motif="child session %s discarded" % refusee,
            note="Session %s refused by the harness (child session: subagent or "
                 "fork); {suite}. Not counted as a fast failure." % refusee,
            action="session-fille")

    def _ecarte_session_inconnue(self, refusee: str, connue) -> None:
        """L224 : `refusee` n'existe pas dans le stockage de sessions du
        harnais pour le compte du tour (session d'un autre harnais capturée,
        session effacée) : jamais de reprise vouée à l'échec en boucle."""
        self._ecarte_session(
            refusee, motif="session %s unknown to the harness" % refusee,
            note="Session %s not found in the harness's session store "
                 "for the turn's account: not resumed; {suite}." % refusee,
            action="session-inconnue", connue=connue)

    def _ecarte_session(self, refusee: str, *, motif: str, note: str, action: str,
                        connue=None) -> None:
        """Le tour suivant repart de la dernière session principale connue (le
        fichier local, écrit seulement depuis le flux du harnais ; si `connue`
        est donné, encore faut-il qu'elle l'accepte), sinon d'une session neuve
        ouverte sur le brief de reprise déterministe (L39). Fencé par le bail."""
        principale = self.read_session_file()
        if principale == refusee or (principale and connue is not None
                                     and not connue(principale)):
            principale = None
        if principale:
            registry.set_session(self.db, self.name, principale, None)
            if self.agent is not None:
                self.agent["session_id"] = principale
                self.agent["session_account"] = None
            suite = "resuming the last known main session (%s)" % principale
        else:
            if not registry.clear_session(self.db, self.name, self.runner.runner_id,
                                          self.epoch):
                return  # bail perdu : rien n'est touché (L11 B3)
            try:
                os.unlink(self._path("session"))  # sinon `current_session` le relirait
            except OSError:
                pass
            try:
                from . import reprise
                brief = reprise.deterministic_brief(
                    self.cfg, self.db, registry.get(self.db, self.name) or self.agent,
                    session=refusee, reason=motif,
                    consigne="This turn's instruction follows this brief: resume your "
                             "work from there.")
                if not self._resume_keep(brief, "ameesh"):
                    # #389 r3 : la session refusée ne se garde pas — le brief,
                    # au moins en mémoire, ouvre le tour
                    self.resume_summary, self.resume_origin = brief, "ameesh"
            except Exception as exc:  # un brief manquant n'empêche pas l'oubli
                log_async("[%s] deterministic resume brief unavailable (%s)"
                          % (self.name, exc))
            self.agent = registry.get(self.db, self.name) or self.agent
            self.session_turns = 0
            self.session_tokens = 0.0
            suite = "new session opened on the resume brief"
        log_async("[%s] %s: %s" % (self.name, motif, suite))
        self._fil_note(note.replace("{suite}", suite),
                       meta={"action": action, "session": refusee,
                             "reprise": principale or ""})

    #: L224 : harnais dont le stockage de sessions est connu (`reprise.find_session`)
    SESSION_STORES = ("claude", "codex", "deepseek")

    def _session_connue(self, harness: str, compte, adapter):
        """L224 : prédicat « cette session existe dans le stockage de sessions
        du harnais, tel que le tour le verra » ; None quand on ne sait pas le
        vérifier (autre harnais, compte invalide, `AMEESH_SESSION_STORE_CHECK=0`
        du banc de test, stockage introuvable).

        Le dossier du harnais se résout comme au lancement : environnement de
        l'exécuteur, puis `adapter.env()` (un descripteur peut poser
        `CLAUDE_CONFIG_DIR` ou `CODEX_HOME`), puis le compte du tour
        (`accounts.launch_env`) ; `~` se lit avec le HOME de cet environnement.
        Le prédicat ne dit « inconnue » que si la session n'est ni là, ni sous
        un autre compte déclaré du harnais : dans le doute (stockage
        introuvable, recherche incomplète), la session est gardée (verdict
        codex #183) — une reprise ratée est rattrapée, un faux négatif efface
        une vraie session."""
        if (harness not in self.SESSION_STORES or compte is account_turn.INVALID
                or os.environ.get("AMEESH_SESSION_STORE_CHECK") == "0"):
            return None
        from . import accounts, reprise
        try:
            env = os.environ.copy()
            env.update(adapter.env())  # même ordre que le lancement du tour
            declared = accounts.parse(getattr(self.cfg, "accounts", None) or {})
            if declared:
                accounts.launch_env(env, declared, compte)
            variable = accounts.config_env_of(harness)
            home = os.path.abspath(_expanduser_in(
                (env.get(variable) if variable else "")
                or accounts.default_home_of(harness), env))
            autres = [h for _nom, h in reprise.search_homes(self.cfg, harness)
                      if os.path.abspath(h) != home]
        except Exception as exc:  # configuration illisible : on ne sait pas
            self._avertit_session("harness directory %s not resolved (%s)" % (harness, exc))
            return None
        if not os.path.isdir(reprise.session_root(harness, home)):
            self._avertit_session("session store %s not found"
                                  % reprise.session_root(harness, home))
            return None

        def connue(session: str) -> bool:
            try:
                if reprise.find_session(harness, home, session, strict=True):
                    return True
                for autre in autres:
                    if reprise.find_session(harness, autre, session, strict=True):
                        self._avertit_session(
                            "session %s found under %s, not under %s (turn directory): "
                            "kept" % (session, autre, home))
                        return True
            except reprise.SessionSearchIncomplete as exc:
                # stockage momentanément illisible : introuvable n'est pas établi
                self._avertit_session("search for session %s incomplete (%s): "
                                      "kept" % (session, exc))
                return True
            return False
        return connue

    def _avertit_session(self, texte: str) -> None:
        """L224 : un doute sur la session est dit une fois par texte (pas à
        chaque tour), et la session est gardée."""
        dits = self.__dict__.setdefault("_avertissements_session", set())
        if texte not in dits:
            dits.add(texte)
            log_async("[%s] session check impossible: %s" % (self.name, texte))

    # -- hôte non prêt (L106) ------------------------------------------------
    #: statut et préfixe d'erreur d'un blocage « hôte non prêt » (levé seulement
    #: s'il porte exactement ces deux marques, comme L35 et L72)
    HOST_STATUS = "hôte non prêt"
    HOST_ERROR = "hôte non prêt"
    #: attente croissante entre deux essais : 15 s, 30 s… bornée par
    #: `host_retry_max` (300 s par défaut)
    HOST_BACKOFF_MIN = 15.0

    def _host_missing(self, raison: str) -> None:
        """Un tour qui ne peut pas partir faute de harnais (L106).

        La consigne repart en attente ; le statut est posé par-dessus le tour
        ouvert (`begin_turn` fencé, puis le blocage marqué) — jamais par-dessus
        un arrêt, jamais sous un bail perdu. Pas d'échec rapide."""
        registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
        if not registry.begin_turn(self.db, self.name, self.runner.runner_id, self.epoch,
                                   "checking the host"):
            log("[%s] host not ready (%s) — lease lost or agent stopped" % (self.name, raison))
            return
        self._host_not_ready(raison, force_status=True)

    def _host_not_ready(self, raison: str, force_status: bool = False) -> bool:
        """Note « hôte non prêt » (L106) ; faux si le bail n'est plus le nôtre.

        Même forme que le dossier absent (L35) : au changement de raison (ou si
        `force_status`), blocage marqué fencé par le bail ; journal une fois
        par raison ; sinon l'attente double, bornée."""
        # même raison qu'au dernier essai (un 127 qui revient après un binaire
        # retrouvé) : ni nouveau journal, ni attente remise à zéro — elle double
        nouveau = raison != self._host_reason
        issue = "done"
        if nouveau or force_status or not self._host_blocked:
            issue = registry.set_marked_block(
                self.db, self.name, self.runner.runner_id, self.epoch,
                self.HOST_STATUS, "%s : %s" % (self.HOST_ERROR, raison), self.HOST_ERROR)
            if issue == "lease":
                log("[%s] host not ready (%s) — status not set: lease lost or replaced"
                    % (self.name, raison))
                self.lease_lost.set()
                return False
        maximum = max(self.HOST_BACKOFF_MIN, float(getattr(self.runner, "host_retry_max",
                                                            300.0)))
        if nouveau:
            self._host_delay = self.HOST_BACKOFF_MIN
            log("[%s] host not ready: %s — a host error, not the agent's: turns "
                "suspended, retrying in %ds (increasing backoff, at most %ds), "
                "automatic resume%s"
                % (self.name, raison, self.HOST_BACKOFF_MIN, maximum,
                   "" if issue == "done" else " (concurrent status kept)"))
        else:
            self._host_delay = min(maximum, max(self.HOST_BACKOFF_MIN, self._host_delay * 2))
        self._host_blocked = True
        self._host_reason = raison
        self._host_next = time.monotonic() + self._host_delay
        return True

    def host_ready(self) -> bool:
        """Garde de `pick()` pendant un blocage « hôte non prêt » (L106).

        Hors blocage : vrai (la détection a lieu au lancement d'un tour). En
        blocage : à échéance, le harnais est résolu de nouveau (PATH, PATH du
        gestionnaire systemd relu, emplacements connus) ; trouvé, le blocage
        est levé (fencé) et le tour part dans ce sondage. Rien n'est consommé
        tant que l'hôte n'est pas prêt."""
        if self.runner.dry_run or not self._host_blocked:
            return True
        if time.monotonic() < self._host_next:
            return False
        try:
            self._adapter()
        except adapters.HarnessMissing as exc:
            self._host_not_ready(str(exc))
            return False
        issue = registry.clear_marked_block(
            self.db, self.name, self.runner.runner_id, self.epoch,
            self.HOST_STATUS, self.HOST_ERROR)
        if issue == "lease":
            self.lease_lost.set()
            return False
        # la raison et l'attente restent connues jusqu'à un tour réussi (`run`)
        self._host_blocked = False
        if issue == "kept":
            # un arrêt ou un autre statut a été écrit entre-temps : conservé,
            # le sondage suivant repasse par toutes les gardes
            row = registry.get(self.db, self.name)
            if row is not None:
                self.agent = row
            return False
        log("[%s] host ready: harness %s — resuming"
            % (self.name, self.resolution_seen or "found"))
        return True

    @staticmethod
    def _echec_d_hote(code: int, lignes: int, session: str | None, duree: float,
                      stderr_path: str) -> str:
        """Un tour sorti en 126/127 sans rien écrire est un lancement manqué
        (`env: 'node': No such file or directory`, permission refusée) : une
        erreur de l'hôte (L106). Rend la raison, ou ""."""
        if code not in (126, 127) or lignes or session or duree > 30.0:
            return ""
        fin = ""
        try:
            with open(stderr_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 400))
                fin = fh.read().decode("utf-8", "replace").strip().splitlines()[-1:]
                fin = fin[0] if fin else ""
        except OSError:
            fin = ""
        return "the harness could not run (code %d%s)" % (
            code, ": %s" % fin.strip()[:200] if fin else "")

    def stop_for_power(self, texte: str) -> None:
        """Arrêt propre demandé par la garde d'alimentation (L106) : plus de
        tour ; le tour en cours, s'il dure, sera arrêté par l'exécuteur ; le
        statut « hôte non prêt » est posé avant de rendre le bail."""
        self._power_texte = texte
        self.stopping.set()
        self.wake.set()

    def _mark_power_stop(self) -> None:
        """Statut d'un worker arrêté pour l'alimentation, avant le bail rendu."""
        if not self._power_texte:
            return
        try:
            registry.set_marked_block(
                self.db, self.name, self.runner.runner_id, self.epoch,
                self.HOST_STATUS, "%s : %s" % (self.HOST_ERROR, self._power_texte),
                self.HOST_ERROR)
        except db_mod.DbError as exc:
            log_async("[%s] battery stop status not written (%s)"
                      % (self.name, db_mod.explain(exc)))

    # -- dossier de travail absent (L35) -------------------------------------
    #: statut et préfixe d'erreur d'un blocage « dossier absent » (le statut
    #: n'est levé que s'il porte exactement ces deux marques)
    WORKDIR_STATUS = "dossier absent"
    WORKDIR_ERROR = "dossier de travail absent"
    #: attente croissante entre deux essais complets (adoption d'un dossier
    #: déplacé comprise) : 5 s, 10 s, 20 s… bornée à 5 min, par agent
    WORKDIR_BACKOFF_MIN = 5.0
    WORKDIR_BACKOFF_MAX = 300.0
    #: #811 (B1) : dossier d'un lot secondaire absent — délai par lot (borné),
    #: l'agent gardant ses autres tours
    LOT_WORKDIR_BACKOFF_MIN = 30.0
    LOT_WORKDIR_BACKOFF_MAX = 600.0

    def _resolve_workdir(self, adopt: bool = True, *, lot=None) -> str | None:
        """Le dossier de travail utilisable, ou None.

        Le primaire garde le `cwd` du registre relu par `pick()` et l'adoption
        bornée d'un worktree déplacé (0018). Un lot SECONDaire DÉCLARÉ (projet
        de l'agent autre que le primaire) utilise le dossier de son projet au
        Host, sans modifier le registre ; quand ce dossier manque, `None` ne
        concerne que ce lot (`_lot_workdir_secondary`), jamais le blocage
        global « dossier absent » (L35) réservé au cwd primaire. Un lot d'un
        projet NON déclaré par l'agent, ou un lot inconnu, tourne dans le cwd
        primaire comme avant L243-4 (#811 B1)."""
        from . import canon as canon_mod, lot_workdir, projects as projects_mod
        lot = lot or getattr(self, "_workdir_lot", None) or self.agent.get("session_work_item")
        names = projects_mod.agent_projects(self.agent)
        self._lot_workdir_secondary = False
        self._lot_workdir_path = None
        project = ""
        if len(names) > 1:  # N3 : aucun accès base pour un agent mono-projet
            try:
                project = lot_workdir.project(self.db, lot)
            except (lot_workdir.WorkdirError, canon_mod.CanonError):
                project = ""  # lot inconnu : cwd primaire, comme avant L243-4
        if project and project != names[0] and project in names[1:]:
            self._lot_workdir_secondary = True
            if getattr(self.runner, "mediated", False):
                self._lot_workdir_error = "mediated executor has no per-project depot"
                return None
            try:
                cwd = lot_workdir.destination(self.cfg, self.agent, project)
            except (lot_workdir.WorkdirError, canon_mod.CanonError) as exc:
                self._lot_workdir_error = "project %s: %s" % (project, exc)
                return None
            self._lot_workdir_path = cwd
            self._lot_workdir_error = "project %s: %r" % (project, cwd)
            return cwd if cwd and os.path.isdir(cwd) else None
        self._lot_workdir_error = ""
        return self._primary_workdir(adopt=adopt)

    def _primary_workdir(self, adopt: bool = True) -> str | None:
        """Le dossier primaire (cwd du registre, adoption bornée ; dépôt de
        l'exécuteur médié) — jamais un projet secondaire : la garde L35 ne se
        lève que sur le dossier qu'elle a vu manquer (#811 B1)."""
        if getattr(self.runner, "mediated", False):
            return self._mediated_workdir()  # L113 : dépôt de travail
        cwd = self.agent.get("cwd")
        if cwd and os.path.isdir(cwd):
            return cwd
        return self.adopt_moved_worktree() if adopt else None

    # -- dépôt de travail de l'exécuteur médié (L113) -------------------------
    def _depot_dir(self) -> str:
        """`<AMEESH_EXEC_HOME>/work/<agent>` : un chemin STABLE d'un bail à
        l'autre (dsh lie sa session au dossier : un chemin par epoch rendait
        la reprise impossible, essai L115). Le contenu, lui, est refait à
        chaque prise de bail et effacé à la fin."""
        from .mediated_executor import device
        return os.path.join(device.home(), "work", self.name)

    def _depot_fence(self):
        from .mediated_executor import contract as exec_contract
        return exec_contract.Fence(self.name, self.runner.runner_id, int(self.epoch))

    def _mediated_workdir(self) -> str | None:
        """Le dossier de travail d'un exécuteur médié : l'archive du commit
        rendue par le serveur à la prise du bail (`work_repo.checkout`), sous
        `<AMEESH_EXEC_HOME>/work/<agent>`. Sans dépôt monté sur le
        serveur (404) : un dossier vide. None si l'archive est illisible."""
        from .mediated_executor import work_repo
        path = self._depot_dir()
        if getattr(self, "_depot_ready", None) == path and os.path.isdir(path):
            return path
        work_repo.wipe(path)  # reste d'un bail précédent (arrêt brutal)
        transport = getattr(self.db, "transport", None)
        if not hasattr(transport, "get_work"):
            os.makedirs(path, mode=0o700, exist_ok=True)
            self._depot_active = False
            self._depot_ready = path
            return path
        try:
            meta = work_repo.checkout(transport, self._depot_fence(), path)
            log_async("[%s] dépôt de travail : %s (%s) dans %s"
                      % (self.name, meta["branch"], meta["commit"][:12], path))
            self._depot_active = True
        except db_mod.DbError as exc:
            if getattr(exc, "code", None) != "op_not_allowed":
                log_async("[%s] dépôt de travail indisponible : %s" % (self.name, exc))
                work_repo.wipe(path)
                return None
            os.makedirs(path, mode=0o700, exist_ok=True)
            self._depot_active = False
            log_async("[%s] aucun dépôt de travail sur le serveur : dossier vide %s"
                      % (self.name, path))
        except (work_repo.DepotError, OSError, subprocess.SubprocessError) as exc:
            log_async("[%s] dépôt de travail illisible : %s" % (self.name, exc))
            work_repo.wipe(path)
            return None
        self._depot_ready = path
        return path

    def _depot_push(self, *, final: bool = False) -> None:
        """Après un tour, et à la fin du bail (`final` : travail non commité
        commité, puis dossier effacé) : les commits de l'appareil partent au
        serveur, qui pousse la branche de l'agent. Ne lève jamais."""
        if not getattr(self.runner, "mediated", False):
            return
        from .mediated_executor import work_repo
        path = getattr(self, "_depot_ready", None)
        try:
            if path and getattr(self, "_depot_active", False) and os.path.isdir(path) \
                    and not self.lease_lost.is_set():
                reply = work_repo.push(self.db.transport, self._depot_fence(), path,
                                   commit_pending=final)
                if reply:
                    log_async("[%s] dépôt de travail : %d commit(s) poussé(s) sur %s (%s)"
                              % (self.name, reply.get("commits", 0), reply.get("branch"),
                                 str(reply.get("commit") or "")[:12]))
        except (db_mod.DbError, work_repo.DepotError, OSError, subprocess.SubprocessError) as exc:
            log_async("[%s] dépôt de travail : envoi impossible (%s)" % (self.name, exc))
        finally:
            if final and path:
                work_repo.wipe(path)
                self._depot_ready = None

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
            message = "%s : %s" % (self.WORKDIR_ERROR,
                                     getattr(self, "_lot_workdir_error", "") or repr(cwd))
            issue = registry.set_marked_block(
                self.db, self.name, self.runner.runner_id, self.epoch,
                self.WORKDIR_STATUS, message, self.WORKDIR_ERROR)
            if issue == "lease":
                log("[%s] %s — block not set: lease lost or replaced"
                    % (self.name, message))
                self.lease_lost.set()
                return False
        if nouveau:
            self._wd_delay = self.WORKDIR_BACKOFF_MIN
            log("[%s] %s — turns suspended; retrying in %ds at the earliest (increasing "
                "backoff, at most %ds), immediately if the registry changes%s"
                % (self.name, message, self.WORKDIR_BACKOFF_MIN, self.WORKDIR_BACKOFF_MAX,
                   "" if issue == "done" else " (concurrent status kept)"))
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
                log("[%s] working directory found again (%s) but lease lost or replaced: "
                    "no resume here" % (self.name, cwd))
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
            log("[%s] working directory found again: %s — concurrent status kept "
                "(%s%s), no resume in this poll"
                % (self.name, cwd, (row or {}).get("status") or "?",
                   ": %s" % row["status_text"] if row and row.get("status_text") else ""))
            return False
        log("[%s] working directory found again: %s — resuming" % (self.name, cwd))
        return True

    def _lot_workdir_deferred(self, lot) -> None:
        """#811 (B1) : diffère CE lot secondaire (dossier absent), échéance
        croissante et dernier chemin connu — le dossier revenu le débloque au
        sondage suivant, sans attendre l'échéance."""
        lot = str(lot or "")
        if not lot:
            return
        delay = self._lot_wd_delay.get(lot)
        delay = (self.LOT_WORKDIR_BACKOFF_MIN if delay is None
                 else min(self.LOT_WORKDIR_BACKOFF_MAX, delay * 2))
        self._lot_wd_delay[lot] = delay
        self._lot_wd_until[lot] = time.monotonic() + delay
        if self._lot_workdir_path:
            self._lot_wd_path[lot] = self._lot_workdir_path
        log("[%s] lot #%s: %s — lot deferred for %ds, the agent keeps its other "
            "turns" % (self.name, lot, self._lot_workdir_error or "no directory",
                       int(delay)))

    def _lot_deferred(self, lot) -> bool:
        """#811 (B1) : ce lot secondaire attend-il encore son dossier ?

        Le dernier chemin connu est revérifié à chaque appel : un dossier
        recréé lève le différé tout de suite (pas d'attente de l'échéance)."""
        lot = str(lot or "")
        if not lot or lot not in self._lot_wd_until:
            return False
        path = self._lot_wd_path.get(lot)
        if path and os.path.isdir(path):
            self._lot_wd_until.pop(lot, None)
            self._lot_wd_delay.pop(lot, None)
            self._lot_wd_path.pop(lot, None)
            return False
        return self._lot_wd_until[lot] > time.monotonic()

    def _lot_workdir_blocked(self, lot) -> bool:
        """#811 (B1/B2) : ce lot ne peut pas ouvrir de tour faute de dossier.

        L'état de différé (mémoire de ce worker) d'abord ; sinon la règle
        partagée avec le hook (`lot_workdir.mail_hold`), pour ne pas livrer de
        courrier dans la session d'un autre lot ni consommer un réveil pour un
        lot dont le dossier manque. Une lecture en échec ne bloque rien."""
        if self._lot_deferred(lot):
            return True
        try:
            from . import lot_workdir
            return lot_workdir.mail_hold(self.cfg, self.db, self.agent, lot)
        except db_mod.DbError as exc:
            log_async("[%s] lot workdir unreadable (%s)" % (self.name, exc))
            return False

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
        # #811 (B1) : la garde L35 surveille le dossier PRIMAIRE ; un lot
        # secondaire différé ne la concerne pas (et ne la lève pas non plus).
        cwd = self._primary_workdir(adopt=du)
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
        if bool(sender) and sender in self.cfg.interrupt_senders:
            return True
        return self._deep_analysis_interrupt(sender, row)

    def _deep_analysis_interrupt(self, sender: str, row: dict) -> bool:
        """L317c (#315 r1) : la demande d'analyse profonde (`deep_analysis`)
        perce le tour en cours d'un auditeur — le premier rapport est dû dans
        les 30 min —, sans `AMEESH_INTERRUPT_SENDERS`. Seulement si le lot a
        bien une demande sans rapport : un courrier qui imite l'expéditeur
        `ameesh` n'interrompt rien."""
        from . import deep_analysis
        payload = mail.message_payload(row)
        if sender != deep_analysis.SENDER or not payload.get("deep_analysis"):
            return False
        try:
            if not self.runner.is_auditor(self.name, self.agent):
                return False
            found = deep_analysis.status(self.db, int(payload.get("lot") or 0),
                                         cfg=getattr(self, "cfg", None))
        except (db_mod.DbError, TypeError, ValueError):
            return False
        return bool(found and found["report_ts"] is None
                    and self.name in found["auditors"])

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
        critique = account_turn.is_critical(self)
        if not critique:
            return self._own_reason(book, margin=True)
        # L242b (3), #389 r1 : sous un harnais emprunté, le retour au sien
        # demande la marge d'un non-critique (pas d'aller-retour à chaque
        # franchissement du plafond) — sauf si aucun emprunt n'en a plus
        retour = account_turn.borrowing(self)
        raison = self._critical_fallback(self._own_reason(book, margin=retour), limites)
        if raison and retour:
            raison = self._own_reason(book, margin=False)
        return raison

    def _own_reason(self, book, *, margin: bool) -> str:
        """La raison de pause sur son propre harnais, ou ''. L30 (0027) : avec
        des comptes déclarés, le rythme se juge compte par compte et la garde
        bascule au lieu de mettre en pause. L242b : `margin` (un non-critique,
        ou un critique qui revient d'un emprunt) s'arrête plus tôt — sous le
        rythme (réserve, sans comptes déclarés ; avec, `accounts.choose` la
        prend) et à une part du plafond horaire payé au token du mesh."""
        from . import accounts as accounts_mod
        raison = account_turn.choose(self, book, margin=margin)
        part = getattr(self.cfg, "noncritical_hourly_share", 0.75) if margin else 1.0
        if raison is None:
            return book.over(self.name, reserve=accounts_mod.RESERVE if margin else 0.0,
                             hourly_share=part)
        return raison or book.over(self.name, pace=False, hourly_share=part)

    def _critical_fallback(self, raison: str, limites) -> str:
        """L242b (3) : la pause budget d'un agent critique cède à un autre
        harnais qui a de la marge (`account_turn.fallback`) ; jamais une
        configuration ou un état de comptes illisibles."""
        if not raison or raison.startswith(("invalid account configuration",
                                            "account state unavailable")):
            return raison
        return account_turn.fallback(self, raison, limites)

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
        self._borrowed = None
        if self.runner.budget_limits(self.db).per_hour <= 0:
            # L30 : bascule sans pause de rythme ; L226b : jamais un compte en
            # overage ou au-dessus d'un plafond fixe, quels que soient les
            # réglages de budget — sans autre compte, pas de tour
            raison = account_turn.choose(self, None, pause=False) or ""
            if raison and account_turn.is_critical(self):
                raison = self._critical_fallback(raison, self.runner.budget_limits(self.db))
        else:
            # Réparation d'abord : un travail comptable en attente (marqueur
            # persistant) doit être écrit avant d'autoriser le moindre tour ;
            # un marqueur corrompu suspend au lieu d'autoriser.
            self._compta_repare()
            raison = ("turn accounting unavailable" if self._compta_en_echec
                      else self.budget_reason())
        if not raison:
            # #1469 : l'enveloppe du compte de facturation retenu (y compris
            # le repli d'un critique) — aucune exception ne la dépasse
            raison = account_budget.reason(self.db, self._account)
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
            # L242b (5) : la pause d'un agent critique est marquée — alerte
            # urgente `critical_budget_pause`
            marque = budget_mod.CRITICAL_MARK if account_turn.is_critical(self) else ""
            registry.set_status(self.db, self.name, "blocked",
                                status_text="%s%s%s" % (budget_mod.PAUSE_PREFIX, marque,
                                                        raison))
            if raison != self._budget_reason:
                self._budget_reason = raison
                self._fil_note("Budget pause: %s. No turn until the gauge goes back "
                               "down." % raison,
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
            log_async("[%s] budget pause lifted (%s)" % (self.name, texte))

    def turn_harness(self) -> str:
        """L242b (3) : le harnais de ce tour (`account_turn._harness`) — le
        sien, ou celui qu'un agent critique emprunte, ou celui d'un résumé de
        rotation."""
        return account_turn._harness(self) or "other"

    def _ledger_harness(self) -> str:
        """Le harnais du dernier tour lancé (noté avant le lancement) : celui
        de sa ligne de grand livre, même rejouée après une panne."""
        return self._state_read("turn_harness") or (self.agent or {}).get("harness") or ""

    def _state_note(self, key: str, value: str) -> bool:
        """Écrit une valeur d'état locale, atomiquement ; jamais fatal. Rend
        vrai si elle est écrite (#389 r1 : le harnais du tour l'exige)."""
        path = os.path.join(self.state_dir, key)
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(value)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(path + ".tmp", path)
            return True
        except OSError as exc:
            log_async("[%s] state %s not written (%s)" % (self.name, key, exc))
            return False

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

    def _compta_marque(self, start: int, tour: str, model: str | None,
                       work_item_id: str | None = None,
                       turn_id: str | None = None) -> bool:
        """Pose le marqueur **avant** le tour, atomiquement en base (L13 B5).

        Si la pose échoue, le tour ne démarre pas : aucune dépense sans trace
        possible (fail-closed).

        `work_item_id` (#796) : le lot du tour part avec le marqueur, donc la
        ligne du grand livre le porte même si elle est écrite par la réparation
        d'un autre worker après une panne. `turn_id` (#796) : l'identifiant du
        tour, pour la même raison — il relie la ligne à `turn_resources`.
        """
        self._annonce_ram = ""  # un tour neuf n'a encore rien annoncé
        try:
            # Capture in the write itself: a replacement may write its marker
            # as soon as ours commits, before a separate read (#1469).
            self._compta_travail = registry.pending_spend_begin(
                self.db, self.name, int(start), tour,
                model if model is not None else "",
                work_item_id=work_item_id, turn_id=turn_id)
            return self._compta_travail is not None
        except db_mod.Unavailable:
            raise  # L72 : rien lancé ; la consigne est remise au retour de la base
        except db_mod.DbError as exc:
            self._compta_en_echec = True
            log_async("[%s] accounting marker not set (%s): turn refused"
                      % (self.name, " ".join(str(exc).split())[:160]))
            return False

    def _compta_ecrit(self, travail: dict, *, incertain: bool = True) -> bool:
        """Écrit une ligne du grand livre, puis efface le marqueur en base.

        Rejouable après panne : le marqueur n'est effacé qu'après une écriture
        réussie. Si l'effacement lui-même échoue, la réparation suivante peut
        écrire une ligne en double — jamais une dépense perdue en silence ; le
        contraire (autoriser un tour sans trace) est ce qu'on refuse.
        """
        try:
            debut = int(travail.get("start"))
            if debut < 0:
                raise ValueError("negative index")
        except (TypeError, ValueError):
            self._compta_en_echec = True
            return False
        try:
            from . import accounts as accounts_mod
            book = cost_mod.CostBook(
                state_dir=self.cfg.state_dir, db=self.db,
                tools={self.name: self._ledger_harness()},
                # L95 : le journal d'un fil Codex vit dans le dossier du compte
                codex_homes=accounts_mod.homes(self.cfg, "codex"))
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
            cle = self._spend_key(debut, cree)
            usage = book.record(self.name, start=debut, key=cle,
                                turn=travail.get("turn") or None,
                                session=travail.get("session") or None,
                                # `""` = modèle inconnu : tarif le plus cher
                                # (fail-closed), jamais un repli sur un état
                                # local mutable.
                                model=modele,
                                # #796 (0423) : le lot du tour et son instant de
                                # début viennent du marqueur, pas de la mémoire
                                # de ce worker : la réparation par un autre
                                # worker écrit la même attribution.
                                work_item_id=travail.get("work_item_id"),
                                started_at=travail.get("created_ts"),
                                # #796 (0423) : l'identifiant du tour, même
                                # provenance — il relie la ligne à ses
                                # ressources (`turn_resources`)
                                turn_id=travail.get("turn_id"))
            # Mesure du plafond de contexte (L60), lue sur la ligne écrite.
            self.last_turn_reread = usage.reread_tokens
            self.last_turn_reread_session = usage.session
            # #1469 : la ligne écrite prend la place de la réservation ; une
            # réparation (tour interrompu, `incertain` par défaut) la garde
            # Un échec lève : le marqueur reste, la réparation règle par la clé.
            # Le backend médié ne réserve aucun compte local : la dépense
            # fournisseur est comptée au relais, pas sur l'appareil (L111).
            if not self.runner.mediated:
                account_budget.settle(self.db, cle, state="kept" if incertain else "settled")
            registry.pending_spend_clear(self.db, self.name, cree)
            self._compta_travail = None
            self._annonce_ram = ""
            self._compta_en_echec = False
            return True
        except Exception as exc:  # panne non fatale au tour, mais fail-closed
            self._compta_en_echec = True
            log_async("[%s] turn accounting failed (%s): turns suspended"
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
            adapter = adapters.adapter_for(self._ledger_harness() or "other",
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
                raise db_mod.DbError("no accounting marker to update")
        except db_mod.DbError as exc:
            self._compta_en_echec = True
            log_async("[%s] announced model not persisted (%s): turns suspended"
                      % (self.name, " ".join(str(exc).split())[:120]))

    def _spend_key(self, debut: int, cree) -> str | None:
        """La clé du tour (L60) : grand livre, marqueur et réservation #1469."""
        return ("%s:%d:%.6f" % (self.name, int(debut), float(cree))
                if cree is not None else None)

    def _compta_termine(self, model_annonce: str | None = None, *,
                        incertain: bool = False) -> None:
        """Après un tour : écrit la ligne du grand livre.

        Le modèle **annoncé par le flux** du harnais prime sur celui figé au
        lancement : c'est la source du tour (mesh-design, 0019 §3).
        """
        # L72 : le tour a eu lieu ; sa ligne de grand livre attend la base
        # plutôt que d'être remise à une réparation ultérieure.
        travail = self._avec_reprise("turn accounting", self._compta_en_attente)
        propre = getattr(self, "_compta_travail", None)
        if propre and (not travail or travail.get("created_ts") != propre.get("created_ts")):
            # #1469 : le marqueur en base n'est plus celui de ce tour (reprise
            # par un remplaçant, ou déjà réparé) : on règle CE tour par sa propre
            # clé (la ligne est dédoublonnée par `spend_key`), sans toucher au
            # marqueur d'un autre
            travail = dict(propre, model=self._annonce_ram or propre.get("model"))
        if travail:
            if model_annonce:
                travail = dict(travail, model=model_annonce)
            self._compta_ecrit(travail, incertain=incertain)
        elif travail is not None:
            self._compta_en_echec = True

    def maybe_relocate(self, pressure: dict) -> bool:
        """L232a: reserve a complete handoff; never directly move a worktree.

        The separate L232b controller prepares hosts and drains at a lot
        boundary. Only a persisted claim fence stops this source worker.
        """
        if getattr(self.runner, "mediated", False):
            # Device relocation is controlled by the mesh server (L109).
            return False
        from . import handoffs, host_lifecycle, storage, work
        try:
            api = storage.of(self.db)
            demand = api.host_lifecycle.demand(self.name, self.agent["host"])
            if not getattr(self.runner, "relocate", False) and demand is None:
                return False
            active = api.handoffs.active(self.name)
            if active:
                if active["fenced"]:
                    self.stopping.set()
                    self.wake.set()
                    return True
                return False
            queued = bool(self.agent.get("pending_prompt") or work.open_lots(self.db, self.name))
            missing = []
            def ready(agent, target):
                result = host_lifecycle.readiness(
                    self.db, agent, target, adapter=getattr(self.runner, "_lifecycle_adapter", None),
                    requirements_for=getattr(self.runner, "_lifecycle_requirements", None))
                missing.extend(result["missing"])
                return result
            candidate = handoffs.plan(self.db, self.agent,
                                      limits_for=self.runner.handoff_limits_for, queued=queued,
                                      demand=demand, readiness_for=ready if demand else None)
            if candidate is None:
                if demand:
                    api.host_lifecycle.progress(demand["host"], demand["generation"], self.name,
                                                operation=None, missing=sorted(set(missing)) or ["fresh_admitted_capacity"])
                return False
            remote = subprocess.run(["git", "-C", self.agent.get("cwd") or "", "remote", "get-url", "origin"],
                                    check=True, text=True, capture_output=True, timeout=10).stdout.strip()
            operation = handoffs.request(self.db, candidate, repository=remote)
            log("[%s] host handoff %s reserved to %s; waits for host preparation and lot boundary"
                % (self.name, operation["id"], operation["target"]))
        except (db_mod.DbError, handoffs.HandoffError, OSError, subprocess.SubprocessError):
            log("[%s] host handoff unavailable; source and worktree retained" % self.name)
        return False

    def _held_by_pressure(self, pressure: dict) -> bool:
        """La pression de l'hôte retient-elle le prochain tour de cet agent ?

        Oui dès qu'un seuil est franchi (L31, 0028) — sauf pour un
        orchestrateur sous une pression NON critique et hors batterie basse :
        ses tours sont courts, et c'est lui qui distribue le travail des autres
        (2026-10-10 : charge jusqu'à 43 pour 12 CPU, plus aucun tour, pas même
        le sien, et personne pour répartir). L234 exempte les auditeurs,
        y compris sous pression critique ; leur harnais a une priorité réduite."""
        if not pressure.get("blocked"):
            return False
        # L234 : la surveillance reste active, même sous pression critique.
        if self.runner.is_auditor(self.name, self.agent):
            from .platform import audit_guard
            return not audit_guard.available()
        if pressure.get("critical") or not self.runner.is_orchestrator(self.name):
            return True
        return any(b.get("power") for b in pressure.get("breaches") or ())

    def _host_pressure_note(self, pressure: dict) -> None:
        """Trace la contre-pression (L31, 0028) ; en critique, met en pause.

        Ne touche JAMAIS un tour en cours : `_pick` s'exécute entre deux tours.
        Seuls les agents de priorité la plus basse (0, le défaut) sont mis en
        pause ; les autres attendent simplement que la pression redescende."""
        from . import resources as resources_mod
        raisons = resources_mod.describe(pressure.get("breaches")) or "threshold"
        if (not pressure.get("critical")
                or int(self.agent.get("priority") or 0) > 0):
            self._host_pressure_hold(raisons)
            return
        if self._work_waiting():
            self.runner.pressure_delayed(self.name)
        texte = "critical pressure on host %s: %s" % (self.runner.host, raisons)
        if getattr(self, "_pressure_reason", "") == texte:
            return
        # Pause fencée par le bail : un worker périmé (bail perdu, remplacé,
        # expiré) ou un tour en cours est refusé (L31, 0028).
        if not registry.pause(self.db, self.name, self.runner.runner_id, self.epoch,
                              "paused: " + texte):
            if getattr(self, "_pause_refused", "") != texte:  # dit une fois
                self._pause_refused = texte
                log_async("[%s] pause refused: lease lost, replaced, or turn in progress"
                          % self.name)
            return
        if not getattr(self, "_pressure_text", ""):
            self._pressure_before = self.agent.get("status_text") or ""
        self._pressure_reason = texte
        self._pressure_text = "paused: " + texte
        self._fil_note("Pause: %s. No new turn until the pressure goes back "
                       "down; the current turn is never interrupted." % texte,
                       meta={"action": "pression-hote", "hote": self.runner.host})

    def _work_waiting(self) -> bool:
        """Une consigne ou du courrier attend ce worker (L31b)."""
        if self.agent.get("pending_prompt"):
            return True
        try:
            # L125 : un courrier passif n'attend pas de tour
            return bool(mail.unread_active(self.db, self.name, limit=1))
        except db_mod.DbError:
            return False

    def _host_pressure_hold(self, raisons: str) -> None:
        """L31b : une attente de pression se VOIT (`ameesh show`, `ameesh list`).

        Sans elle, un agent retenu restait `queued`, erreur vide, sans raison
        lisible. Le statut ne change pas ; seul le texte dit ce qui retient le
        tour, et seulement quand du travail attend. Fencé par le bail ; ne lève
        jamais. Le journal, lui, ne dit l'attente qu'une fois par épisode de
        pression, pour tous les agents (`Runner._pressure_episode_update`).
        Not on a mediated executor: `leases.hold_note` is not in contract 1.1
        (the wait stays silent there, as in 1.6.x)."""
        if getattr(self.runner, "mediated", False):
            return
        if not self._work_waiting():
            return
        self.runner.pressure_delayed(self.name)
        texte = "waiting: pressure on host %s — %s" % (self.runner.host, raisons)
        if getattr(self, "_pressure_text", "") == texte:
            return
        try:
            posee = registry.hold_note(self.db, self.name, self.runner.runner_id,
                                       self.epoch, texte)
        except db_mod.DbError as exc:
            log("[%s] pressure wait not shown (%s)" % (self.name, exc))
            return
        if posee:
            if not getattr(self, "_pressure_text", ""):
                self._pressure_before = self.agent.get("status_text") or ""
            self._pressure_text = texte

    def _host_pressure_release(self) -> None:
        """L31b : la pression est retombée — l'attente ou la pause s'efface,
        si son texte est toujours en place (personne ne l'a remplacé) ; le texte
        d'avant (« reprise : même session »…) revient."""
        self._pause_refused = ""
        texte = getattr(self, "_pressure_text", "")
        if not texte:
            return
        if getattr(self.runner, "mediated", False):
            # `leases.release_hold` hors contrat 1.1 : sur un exécuteur médié,
            # une pause critique n'est pas levée d'elle-même (comme avant L31b)
            return
        try:
            levee = registry.release_hold(self.db, self.name, self.runner.runner_id,
                                          self.epoch, texte,
                                          getattr(self, "_pressure_before", ""))
        except db_mod.DbError as exc:
            log("[%s] could not lift the pressure wait (%s)" % (self.name, exc))
            return
        self._pressure_text = ""
        self._pressure_reason = ""
        if levee:
            # la fin de l'épisode est dite une fois, pour tous les agents
            agent = registry.get(self.db, self.name)
            if agent is not None:
                self.agent = agent

    # -- échec d'authentification (L226b) ------------------------------------
    def auth_ready(self) -> bool:
        """Garde de `pick()` : le compte du prochain tour est-il sain ?

        Le compte est celui que la garde de budget vient de choisir (L30 : un
        compte en échec d'authentification n'est choisi que s'il n'en reste
        aucun sain), ou le compte par défaut du harnais ('') sans comptes
        déclarés. En échec : blocage `auth_blocked` (fencé), pas de tour, pas
        d'échec rapide. Sain de nouveau : blocage levé, le tour part."""
        # Provider credentials and their health belong to the relay server.
        # The device has no account health authority or credential to inspect.
        if self.runner.dry_run:
            return True
        if self.runner.mediated:
            from . import relay
            harness = self.turn_harness()
            if self._relay_credential_ready():
                return self._auth_release()
            self._auth_block(harness, relay.CREDENTIAL_ACCOUNT, "relay server credential failing")
            return False
        compte = account_turn.for_turn(self)
        if compte is account_turn.INVALID:
            return True  # `account_turn.apply` refusera le tour, avec sa raison
        harness = self.turn_harness()
        account = auth_failure.account_name(compte)
        sick = auth_failure.failing(self.db, self.cfg.host, harness).get(account)
        if sick is None:
            return self._auth_release()
        self._auth_block(harness, account, sick.get("reason") or "authentication failing")
        return False

    def _auth_block(self, harness: str, account: str, reason: str) -> None:
        """Pose (fencé) le blocage `auth_blocked` d'un agent qui n'a pas de tour
        en cours ; journal et fil une fois par blocage."""
        texte = auth_failure.status_text(harness, account)
        agent = self.agent or {}
        if texte == self._auth_status and agent.get("status") == "blocked" \
                and agent.get("status_text") == texte:
            return
        if self._auth_status and self._auth_status != texte:
            # autre compte en échec (bascule vers un compte lui aussi en panne) :
            # l'ancienne marque est levée pour poser la nouvelle
            registry.clear_marked_block(self.db, self.name, self.runner.runner_id,
                                        self.epoch, self._auth_status,
                                        auth_failure.STATUS_PREFIX)
            self._auth_status = ""
        issue = registry.set_marked_block(
            self.db, self.name, self.runner.runner_id, self.epoch, texte,
            "%s %s" % (auth_failure.STATUS_PREFIX, reason), auth_failure.STATUS_PREFIX)
        if issue == "lease":
            self.lease_lost.set()
            return
        if issue == "kept":
            return  # un autre statut (arrêt, blocage) tient : réessai au sondage suivant
        self._auth_note(harness, account, reason, texte)

    def _auth_note(self, harness: str, account: str, reason: str, texte: str,
                   turn: bool = False) -> None:
        self._auth_status = texte
        if turn:
            log_async("[%s] turn failed authentication on %s/%s: %s — account "
                      "marked failing; switching to a healthy account if there is one, "
                      "otherwise auth_blocked" % (self.name, harness, account or "(default)",
                                                  reason))
            self._fil_note(
                "Turn failed authentication on %s/%s (%s): the instruction goes back to "
                "the queue, with no retry loop. The account is marked failing; the next "
                "turn runs on a healthy %s account if there is one, otherwise the agent "
                "stays auth_blocked until a healthy account is back."
                % (harness, account or "(default)", reason, harness),
                meta={"action": "auth_blocked", "harnais": harness, "compte": account,
                      "raison": reason, "source": "tour"})
            return
        log_async("[%s] %s: %s — no turn until a %s account is healthy "
                  "(sentinel, or `ameesh accounts healthy`)"
                  % (self.name, texte, reason, harness))
        self._fil_note(
            "Authentication failing on %s/%s (%s) and no other healthy %s account: "
            "the agent stays auth_blocked, with no retry; it resumes on its own as soon "
            "as an account is healthy again. Never a pay-per-token fallback."
            % (harness, account or "(default)", reason, harness),
            meta={"action": "auth_blocked", "harnais": harness, "compte": account,
                  "raison": reason})

    def _auth_release(self) -> bool:
        """Compte sain : le blocage `auth_blocked` encore posé est levé (fencé)."""
        if not self._auth_status:
            return True
        texte = self._auth_status
        issue = registry.clear_marked_block(self.db, self.name, self.runner.runner_id,
                                            self.epoch, texte, auth_failure.STATUS_PREFIX)
        if issue == "lease":
            self.lease_lost.set()
            return False
        self._auth_status = ""
        if issue == "kept":
            row = registry.get(self.db, self.name)
            if row is not None:
                self.agent = row
            return False
        log_async("[%s] %s lifted: healthy account — resuming" % (self.name, texte))
        self._fil_note("Block %s lifted: a healthy account is available again, "
                       "the agent resumes." % texte, meta={"action": "auth_reprise"})
        return True

    def _relay_credential_ready(self) -> bool:
        from . import relay
        harness = self.turn_harness()
        if harness not in relay.HARNESS_ENV:
            return True
        base = relay.relay_url({"AMEESH_RELAY_URL": os.environ.get("AMEESH_RELAY_URL", ""),
                                "AMEESH_EXEC_URL": self.db.url or self.cfg.exec_url})
        token = self.runner.session_token_for(self.name, self.runner.runner_id, self.epoch)
        return relay.credential_ready(base, token, harness)

    def _end_relay_auth_failure(self, cost) -> bool:
        """Server-authenticated relay failure: retain the prompt, no fast retry.

        The caller has already restored the prompt. A device never writes
        credential health; it only uses the relay's verdict to mark its agent.
        """
        if not self.runner.mediated or self._avec_reprise(
                "relay credential status", self._relay_credential_ready):
            return False
        from . import relay
        self._auth_block(self.turn_harness(), relay.CREDENTIAL_ACCOUNT, "relay server credential failing")
        self.fast_failure = False
        if self._session_en_attente is not None:
            self._avec_reprise("turn session", self._enregistre_session)
        self._avec_reprise(
            "end of turn", registry.end_turn,
            self.db, self.name, self.runner.runner_id, self.epoch,
            status="blocked", status_text=self._auth_status,
            error="%s relay server credential failing" % auth_failure.STATUS_PREFIX,
            cost_usd=cost)
        self.last_activity = time.monotonic()
        return True

    def pick(self) -> dict | None:
        """Le prochain tour à faire, ou None. Consomme la consigne en attente.

        Le tour porte son lot (`lot`, L26) quand il est déductible."""
        spec = self._pick()
        if spec is not None:
            # #408 : le réveil sur un lot actionnable porte déjà son lot
            spec["lot"] = spec.get("lot") or self.turn_lot(spec)
            if self._lot_workdir_blocked(spec.get("lot")):
                # #811 (B1) : ne pas relancer en boucle le tour d'un lot
                # secondaire sans dossier ; la consigne repart en attente.
                if spec.get("kind") == "prompt":
                    registry.restore_prompt(self.db, self.name, self.runner.runner_id,
                                            self.epoch)
                return None
        return spec

    def _pick(self) -> dict | None:
        agent = registry.get(self.db, self.name)
        if agent is None or agent.get("status") == "stopped":
            if agent is not None:
                log("[%s] stopped in the registry" % self.name)
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
                log_async("[%s] database reachable again: block \"%s\" lifted"
                          % (self.name, self.DB_STATUS))
            self._base_marquee = False
            self.agent = agent = registry.get(self.db, self.name) or agent
        if self.maybe_relocate({}):
            return None  # L232a persisted fence: return this worker's source lease
        if not self.budget_ok():
            return None  # garde de budget (L13) : aucun tour, état `paused` posé
        if self.runner.gate_holds():
            return None  # L159f : porte d'hôte fermée, rien n'est consommé
        # Contre-pression de l'hôte (L31, 0028) : au-dessus d'un seuil, aucun
        # NOUVEAU tour ne démarre ; la consigne en attente n'est pas consommée
        # (auditeur excepté ; orchestrateur sous pression non critique).
        pressure = self.runner.host_pressure()
        if self._held_by_pressure(pressure):
            self._host_pressure_note(pressure)
            return None
        self._host_pressure_release()
        # Hôte non prêt (L106) : harnais introuvable — rien n'est consommé,
        # nouvel essai à échéance, reprise d'elle-même.
        if not self.host_ready():
            return None
        # Dossier de travail absent (L35) : ni consigne ni courrier consommés,
        # pas de tour tenté à chaque sondage.
        if not self.workdir_ready():
            return None
        # L226b : le compte du tour est en échec d'authentification — ni
        # consigne ni courrier consommés, aucune relance ; reprise dès qu'il
        # (ou un autre, par la bascule de `budget_ok`) est sain.
        if not self.auth_ready():
            return None
        # L30 : session ouverte sous un autre compte et non reprenable → résumé
        # sous l'ancien compte, avant de consommer quoi que ce soit (jamais en
        # plein tour).
        if not account_turn.continuity(self):
            return None  # #389 r2 : rotation requise mais annulée, rien consommé
        # #811 (B1/R3) : un urgent d'un lot retenu attend comme les autres, et
        # le préfixe retenu ne le masque plus ; les messages primaires passent.
        from . import lot_workdir
        urgents = lot_workdir.deliverable_unread(self.cfg, self.db, self.agent,
                                                  besoin=50, mode="urgent",
                                                  deferred=self._lot_deferred)
        autorises = [m for m in urgents if self.interrupt_allowed(m)]
        for refuse in (m for m in urgents if m not in autorises):
            mid = int(refuse.get("id") or 0)
            if mid and mid not in self._refus_vus:
                self._refus_vus.add(mid)
                log("[%s] urgent from %s without interruption: sender not allowed "
                    "to interrupt" % (self.name, refuse.get("sender") or "?"))
        if autorises:
            # Le prioritaire passe devant la consigne en attente : c'est lui qui
            # ouvre le tour, la consigne interrompue suivra (0018).
            self.nudged = False
            return self._courrier_spec("urgent", autorises)
        if self.suite:
            lot = self.turn_lot({"kind": "suite", "prompt": "", "ids": []})
            if not self._lot_workdir_blocked(lot):
                # L105 : le tour précédent a été clos par un plafond ; le travail
                # reprend avant toute nouvelle consigne (le courrier suit, par le hook).
                raison, self.suite = self.suite, None
                self.nudged = False
                return {"kind": "suite", "prompt": adapters.SUITE_PROMPT % raison, "ids": [],
                        "lot": lot}
            # #811 (B1) : la suite attend le dossier de son lot, sans perdre sa place.
        prompt = registry.take_pending_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
        if prompt:
            lot = self.turn_lot({"kind": "prompt", "prompt": prompt, "ids": []})
            if self._lot_workdir_blocked(lot):
                # #811 (B1) : la consigne attend le dossier de SON lot ; elle
                # reste en attente et les tours primaires continuent.
                registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            else:
                self.nudged = False
                self._reprise_attach = False
                return {"kind": "prompt", "prompt": prompt, "ids": [], "lot": lot}
        # #811 (B1/R3) : le courrier d'un lot retenu attend, les autres
        # messages du tour passent — et la lecture franchit un préfixe retenu
        # qui masquerait un message remisable plus récent.
        messages = lot_workdir.deliverable_unread(self.cfg, self.db, self.agent,
                                                   besoin=mail.UNREAD_LIMIT,
                                                   deferred=self._lot_deferred)
        if len(messages) >= mail.UNREAD_LIMIT and all(mail.passive_reason(m) for m in messages):
            # L125 : boîte pleine de courrier passif — les messages qui
            # réveillent, plus récents, ne doivent pas y rester invisibles
            reveil = lot_workdir.deliverable_unread(self.cfg, self.db, self.agent,
                                                     besoin=mail.UNREAD_LIMIT,
                                                     mode="active",
                                                     deferred=self._lot_deferred)
            messages = reveil + messages
        if messages:
            spec = self._mail_spec(messages)
            if spec is not None:
                self.nudged = False
                self._reprise_attach = False  # le courrier ouvre la même session
                return spec
            # L125 : rien de dû (fenêtre de regroupement en cours, courrier
            # passif) — la reprise et la relance d'inactivité restent possibles
        if self._reprise_attach:
            # L106 : la session interactive est morte sans rendre son bail ;
            # l'exécuteur reprend la MÊME session (registre), une fois.
            self._reprise_attach = False
            return {"kind": "idle", "prompt": adapters.ATTACH_RESUME_PROMPT, "ids": []}
        if all(mail.passive_reason(m) for m in messages):
            # #408 : un courrier qui réveillera l'agent passe avant (fenêtre L125)
            reveil = self._lot_wake_spec()
            if reveil is not None:
                return reveil
        if self.idle_due():
            return {"kind": "idle", "prompt": adapters.IDLE_PROMPT, "ids": []}
        return None

    #: #408 : intervalle minimal entre deux lectures des lots actionnables
    LOT_WAKE_CHECK_S = 30.0

    def lot_wake_enabled(self) -> bool:
        """Règle 1 (#408) : réglage de l'agent (`ameesh set lot_wake=on|off`),
        sinon celui de l'hôte (`AMEESH_LOT_WAKE`, actif par défaut)."""
        reglage = ((self.agent or {}).get("lot_wake") or "").strip()
        if reglage:
            return reglage == "on"
        return bool(getattr(self.cfg, "lot_wake", True))

    def lot_wake_allowed(self, row: dict, now: float, base: float) -> bool:
        """Un réveil sur ce lot est-il dû ? Le premier, oui. Si le lot a vécu
        depuis le dernier réveil (lot, journal, branche, courrier), le compte
        repart : un nouveau réveil après `base`. Sinon le réveil précédent
        est resté sans effet : au plus `lot_wake_max` réveils, espacés d'un
        délai doublé à chaque fois (plafonné à `idle_nudge`). Tout vit en
        base : un redémarrage de l'exécuteur ne relance rien."""
        wakes = int(row.get("wakes") or 0)
        derniere = row.get("last_wake_ts")
        if not wakes or derniere is None:
            return True
        vecu = row.get("activity_ts") is not None and (
            row.get("wake_activity_ts") is None
            or float(row["activity_ts"]) > float(row["wake_activity_ts"]) + 0.001)
        ecoule = now - float(derniere)
        if vecu:
            return ecoule >= base
        if wakes >= int(getattr(self.cfg, "lot_wake_max", 3)):
            return False
        plafond = max(base, float(self.runner.idle_nudge))
        return ecoule >= min(base * (2 ** wakes), plafond)

    def _lot_wake_spec(self) -> dict | None:
        """Règle 1 (#408) : l'agent au repos depuis plus de `mail_batch` +
        `lot_wake_margin` qui détient un lot `intake`/`build` (ni tenu, ni en
        attente d'une décision ou d'un autre lot) est réveillé avec la
        consigne de le reprendre, le plus prioritaire puis le plus ancien
        d'abord ; le tour part dans la session de ce lot. Les gardes de
        `_pick` (arrêt, budget, porte et pression d'hôte, authentification)
        sont déjà passées."""
        if not self.lot_wake_enabled():
            return None
        base = self.mail_batch() + float(getattr(self.cfg, "lot_wake_margin", 60.0))
        repos = time.monotonic() - self.last_activity
        dernier_tour = (self.agent or {}).get("last_turn_ts")
        if dernier_tour:
            repos = min(repos, time.time() - float(dernier_tour))
        if repos < base:
            return None
        if time.monotonic() - self._lot_wake_checked < self.LOT_WAKE_CHECK_S:
            return None
        self._lot_wake_checked = time.monotonic()
        import datetime as _dt
        st = storage.of(self.db).lot_sessions
        now = time.time()
        try:
            lots = st.actionable(self.name, today=_dt.date.today().isoformat())
            for row in lots:
                if not self.lot_wake_allowed(row, now, base):
                    continue
                if self._lot_workdir_blocked(row["id"]):
                    # #811 (B2) : pas de réveil consommé pour un lot dont le
                    # dossier manque — un autre lot actionnable est élu.
                    continue
                note = st.note_wake(self.name, int(row["id"]), row.get("activity_ts"))
                log_async("[%s] idle with actionable lot #%s (%s): woken (wake %s)"
                          % (self.name, row["id"], row["state"], note.get("wakes")))
                return {"kind": "idle", "ids": [], "lot": str(row["id"]),
                        "prompt": adapters.LOT_WAKE_PROMPT % {
                            "id": int(row["id"]), "title": row.get("title") or "",
                            "state": row["state"]}}
        except db_mod.DbError as exc:
            log_async("[%s] actionable lots unreadable (%s)" % (self.name, exc))
        return None

    def _mail_spec(self, messages: list[dict]) -> dict | None:
        """Messages non remis -> un tour, ou None : rien n'est encore dû.

        L125 : un tour s'ouvre dès qu'un message NON PASSIF est dû, et il
        emporte tout le courrier non lu — les messages dus d'abord quand la
        consigne est pleine, les passifs ensuite :

        * courrier ordinaire : dû à la fin de sa fenêtre de regroupement
          (`mail_batch`, défaut 90 s, comptée depuis l'arrivée du plus
          ancien), tout de suite s'il est urgent ou d'un humain (`_mail_due`) ;
        * événements (C9) : un réveil par fenêtre `AMEESH_EVENT_COALESCE`
          (défaut 120 s), sauf un urgent d'un expéditeur habilité.
          L'instant du dernier réveil vit en base (`agent_registry.last_event_at`) :
          un redémarrage ne remet pas la fenêtre à zéro ;
        * passifs (accusé, copie, diffusion à tous : `mail.passive_reason`) :
          jamais dus ; lus au tour suivant, ou par le hook pendant un tour.
        """
        passive = [m for m in messages if mail.passive_reason(m)]
        active = [m for m in messages if not mail.passive_reason(m)]
        events = [m for m in active if mail.is_event(m)]
        others = [m for m in active if not mail.is_event(m)]
        mail_due = bool(others) and self._mail_due(others)
        if not mail_due and not (events and self._events_due(events)):
            return None  # regroupement en cours, ou rien que du courrier passif
        if events:
            registry.mark_event_wake(self.db, self.name)
        rows = active + passive
        return self._courrier_spec(
            "event" if all(mail.is_event(m) for m in rows) else "mail", rows)

    def _events_due(self, events: list[dict]) -> bool:
        """Les événements en attente ouvrent-ils un tour ? (C9)"""
        fenetre = self.runner.event_coalesce
        dernier = float(self.agent.get("last_event_ts") or 0.0)
        # Seul un urgent d'un expéditeur habilité perce la fenêtre : un urgent
        # non habilité reste soumis au regroupement (même règle que la
        # préemption, 0018).
        return (any(mail.is_urgent(m) and self.interrupt_allowed(m) for m in events)
                or fenetre <= 0 or (time.time() - dernier) >= fenetre)

    def mail_batch(self) -> float:
        """L125 : fenêtre de regroupement du courrier, en secondes — réglage de
        l'agent (`ameesh set <agent> mail_batch=…`, état local de son hôte),
        sinon défaut de l'exécuteur (`AMEESH_MAIL_BATCH`, 90 s). 0 = réveil
        immédiat (le comportement d'avant L125)."""
        local = self._state_read("mail_batch")
        if local:
            try:
                value = float(local)
            except ValueError:
                value = -1.0
            if 0 <= value < float("inf"):
                return value
        return float(getattr(self.runner, "mail_batch", self.cfg.mail_batch))

    def _from_human(self, row: dict) -> bool:
        """L125 : message d'un humain — marqué à l'envoi, ou expéditeur déclaré
        humain sur cet hôte (`AMEESH_HUMANS`)."""
        return mail.is_human(row) or (row.get("sender") or "") in self.cfg.human_names

    def _mail_due(self, rows: list[dict]) -> bool:
        """L125 : le courrier ordinaire en attente ouvre-t-il un tour ?

        Oui si la fenêtre est nulle, si l'un des messages est urgent (réveil
        sans délai, quel que soit l'expéditeur ; seul un expéditeur habilité
        INTERROMPT un tour en cours, 0018) ou d'un humain, ou si le plus
        ancien attend depuis au moins la fenêtre. L'attente se mesure à
        l'horloge de la base (`created_ts`) et depuis que cet exécuteur a vu
        le message (horloge monotone) : un décalage d'horloge entre l'hôte et
        la base ne retarde jamais un message de plus d'une fenêtre.
        """
        window = self.mail_batch()
        if window <= 0 or any(mail.is_urgent(m) or self._from_human(m) for m in rows):
            return True
        now, mono = time.time(), time.monotonic()
        seen = getattr(self, "_batch_seen", {})
        kept: dict[int, float] = {}
        waited = 0.0
        for row in rows:
            first = seen.get(int(row["id"]), mono)
            kept[int(row["id"])] = first
            waited = max(waited, now - float(row.get("created_ts") or now), mono - first)
        self._batch_seen = kept  # les messages remis entre-temps sont oubliés
        if waited >= window:
            return True
        oldest = min(kept)
        if getattr(self, "_batch_logged", None) != oldest:
            self._batch_logged = oldest
            log_async("[%s] mail batched: %d message(s), turn in %ds at most "
                      "(window %gs)" % (self.name, len(rows), int(window - waited) + 1,
                                         window))
        return False

    # -- un tour -----------------------------------------------------------
    def _adapter(self) -> adapters.HarnessAdapter:
        harness = self.turn_harness()
        adapter = adapters.adapter_for(harness)
        # Traçabilité de chaîne d'approvisionnement (décision 0021) : chaque
        # descripteur utilisé est journalisé avec sa provenance et son empreinte,
        # une fois par changement (pas à chaque tour).
        descriptor = adapter.descriptor
        signature = (descriptor.path, descriptor.sha256)
        if signature != self.descriptor_seen:
            self.descriptor_seen = signature
            log("[%s] descriptor %s: %s (%s, sha256 %s)"
                % (self.name, descriptor.id, descriptor.path, descriptor.source,
                   (descriptor.sha256 or "?")[:16]))
        # L106 : le chemin absolu du binaire et sa provenance, une fois par
        # changement — l'incident du 2026-10-10 ne laissait que « introuvable »
        resolution = adapter.resolution.describe() if adapter.resolution else ""
        if resolution and resolution != self.resolution_seen:
            self.resolution_seen = resolution
            log("[%s] harness %s: %s" % (self.name, descriptor.id, resolution))
        return adapter

    def _canon_id(self) -> str | None:
        """L'identifiant du canon qui possède la ligne du registre de l'agent
        (colonne `canon` ; NULL = canon par défaut) : les règles injectées sont
        les siennes, jamais celles d'un homonyme déclaré par un autre canon
        (L42/L43 ; verdict codex #510 r2, P1)."""
        return (self.agent or {}).get("canon")

    def _resume_prompt(self) -> str:
        """Le cadre et le résumé de reprise qui ouvriront la session neuve après
        une rotation (0018), ou "" sans résumé en attente. Le résumé est
        délimité et n'a aucune autorité (`adapters.resume_prompt`).

        #538 : les règles de travail du canon de l'agent sont relues MAINTENANT,
        quel que soit l'auteur du résumé : un brief préparé à l'avance (origine
        `ameesh`) ne fige pas des règles périmées (verdict codex #510 r2, P2)."""
        if not self.resume_summary:
            return ""
        from . import reprise
        resume = reprise.with_process_rules(
            self.cfg, self.resume_summary, agent=self.name,
            canon_id=self._canon_id(),
            host=(self.agent or {}).get("host") or self.cfg.host)
        return adapters.resume_prompt(resume, origin=self.resume_origin)

    def _ouverture(self) -> str:
        """Ce qui ouvre la consigne du prochain tour : le résumé de rotation
        (règles relues) quand il y en a un, sinon les règles de travail du canon
        pour une session neuve ordinaire (premier démarrage) ; "" quand la
        session reprend.

        #538 : `_budget_courrier` réserve ces octets — le bloc de première
        session ne se paie pas sur le courrier (verdict codex #510 r2, P2)."""
        if self.current_session():
            return ""
        if self.resume_summary:
            return self._resume_prompt()
        if self.session_turns:
            return ""
        from . import reprise
        bloc = reprise.process_rules_block(
            self.cfg, agent=self.name, canon_id=self._canon_id(),
            host=(self.agent or {}).get("host") or self.cfg.host)
        return bloc + "\n\n" if bloc else ""

    def _budget_courrier(self) -> int:
        """Octets UTF-8 disponibles pour la consigne d'un tour de courrier.

        Le plafond (`AMEESH_PROMPT_MAIL_MAX`) borne la consigne ENTIÈRE, résumé
        de reprise ou règles de première session compris quand ils ouvriront le
        tour ; jamais au-delà de la borne sûre d'un argument de processus. Un
        résumé qui ne laisserait pas `PROMPT_MIN_BYTES` au courrier les lui
        laisse quand même (la consigne dépasse alors le plafond, jamais la borne
        de l'argument).
        """
        resume = mail.octets(self._ouverture())
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
        if self.runner.draining.is_set():
            # Drainage commencé entre le choix du tour et son lancement : rien
            # ne part ; une consigne déjà prise repart en attente.
            if registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch):
                registry.set_status(self.db, self.name, "queued",
                                    status_text="runner shutting down: instruction put back "
                                                "in the queue")
            return False
        if getattr(self.runner, "restart_for_stale", False):
            # L790 : le paquet a changé entre le choix du tour et son lancement
            # — rien ne part sur le code périmé ; une consigne déjà prise
            # repart en attente, le tour est refusé sans compter comme échec.
            if registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch):
                registry.set_status(self.db, self.name, "queued",
                                    status_text="outdated runner: instruction put back "
                                                "in the queue")
            return False
        if self.runner.gate_holds():
            # L159f : l'hôte se retire entre le choix du tour et son lancement —
            # une consigne déjà prise repart en attente, rien n'est lancé.
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            return False
        # Dernier contrôle avant de consommer quoi que ce soit (L31, 0028) :
        # couvre les tours ouverts directement (résumé de rotation de compte).
        pression = self.runner.host_pressure()
        self._refus_pression = self._held_by_pressure(pression)
        if self._refus_pression:
            self._host_pressure_note(pression)
            return False
        if self.runner.gate_holds():
            # L112 : l'hôte se retire entre le choix du tour et son lancement —
            # une consigne déjà prise repart en attente, rien n'est lancé.
            try:
                registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            except db_mod.DbError:
                pass
            return False
        if pression.get("blocked"):
            self.runner.pressure_bypassed(self.name)  # orchestrateur ou auditeur
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
            log("[%s] mail reservation failed (%s): turn postponed"
                % (self.name, exc))
            return False
        if not reserves:
            log("[%s] turn mail already reserved or delivered elsewhere, or lease "
                "lost: turn cancelled" % self.name)
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
                    log("[%s] could not cancel the reservation (%s)"
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
            log("[%s] mail delivery not settled (%s)" % (self.name, exc))
        incertains = sorted(set(ids) - set(livres))
        if incertains:
            log("[%s] uncertain delivery: message(s) %s shown to the harness but not "
                "settled (lease lost or database unavailable); they will be redelivered, "
                "flagged as possibly redelivered" % (self.name, ", ".join(map(str, incertains))))
            self._fil_note(
                "Uncertain delivery: %d message(s) put in the instruction of a started "
                "turn could not be marked delivered (lease lost or database "
                "unavailable). They will be delivered again, flagged as possibly redelivered."
                % len(incertains),
                meta={"action": "remise-incertaine", "messages": incertains})

    def _mediated_env(self, env: dict) -> None:
        """L109 : la session du harnais d'un exécuteur médié parle au serveur
        avec un jeton de session lié au bail (jamais d'accès à la base) :
        `AMEESH_BACKEND=mediated`, `AMEESH_EXEC_URL`, `AMEESH_EXEC_TOKEN`.
        Sans jeton (serveur injoignable), le tour part quand même : seules
        les commandes `ameesh` de la session échoueront."""
        from .mediated_executor.interfaces import ENV_SERVER_URL, ENV_SESSION_TOKEN
        for name in ("AMEESH_DSN", "AGENT_MESH_DSN", "AMEESH_DATABASE_URL",
                     "AGENT_MESH_DATABASE_URL", ENV_SESSION_TOKEN):
            env.pop(name, None)
        env["AMEESH_BACKEND"] = env["AGENT_MESH_BACKEND"] = "mediated"
        env[ENV_SERVER_URL] = getattr(self.db, "url", "") or self.cfg.exec_url
        try:
            env[ENV_SESSION_TOKEN] = self.runner.session_token_for(
                self.name, self.runner.runner_id, self.epoch)
        except db_mod.DbError as exc:
            log_async("[%s] jeton de session indisponible (%s) : les commandes ameesh de la "
                      "session échoueront" % (self.name, db_mod.explain(exc)))

    def _open_turn_resource(self, turn_id: str, pgid: int | None) -> None:
        """Rattache le groupe du tour (L31, 0028) ; ne casse jamais le tour."""
        self._turn_open = False
        self._turn_open = self._attach_resource(turn_id, pgid)

    def _attach_resource(self, turn_id: str, pgid: int | None) -> bool:
        """La ligne de ressource d'un tour, ou d'une vie de processus résident
        (L76) ; False si elle n'a pas pu être ouverte."""
        try:
            from . import containers as containers_mod
            storage.of(self.db).turn_resources.open_turn(
                turn_id, self.name, self.runner.host, pgid=pgid,
                label=containers_mod.turn_labels(turn_id, self.name))
            return True
        except Exception as exc:  # ne casse jamais un tour
            log("[%s] could not attach the resources of turn %s (%s)"
                % (self.name, turn_id[:8], exc))
            return False

    def _close_turn_resource(self, turn_id: str, pgid: int | None, *,
                             survives: bool = False) -> None:
        """Ferme la ressource du tour ; `orphan` si elle survit (L31, 0028).

        Un groupe encore vivant après l'arrêt escaladé, ou un conteneur qui
        porte encore l'étiquette du tour, est signalé orphelin : **jamais
        supprimé** ici."""
        if not getattr(self, "_turn_open", False):
            return
        self._turn_open = False
        self._release_resource(turn_id, pgid, survives=survives)

    def _release_resource(self, turn_id: str, pgid: int | None, *,
                          survives: bool = False) -> None:
        from .turn_processes import group_running
        conteneurs: list[str] = []
        runtime = self.runner.container_runtime()
        if runtime is not None:
            try:
                conteneurs = runtime.running_for_turn(turn_id)
            except Exception:  # un moteur indisponible n'est pas un orphelin
                conteneurs = []
        orphelin = survives or bool(conteneurs) or group_running(pgid)
        try:
            storage.of(self.db).turn_resources.close_turn(
                turn_id, orphan=orphelin, containers=conteneurs or None)
        except Exception as exc:  # ne casse jamais la fin d'un tour
            log("[%s] could not close the resources of turn %s (%s)"
                % (self.name, turn_id[:8], exc))
            return
        if orphelin:
            log("[%s] orphan resource of turn %s: %s"
                % (self.name, turn_id[:8],
                   ", ".join(conteneurs) if conteneurs else "surviving process group"))

    # -- L76 : processus résident -----------------------------------------
    def _resident_refusal(self, adapter, audit_exempt: bool) -> str:
        """'' si ce tour peut être servi par le processus résident de
        l'agent, sinon pourquoi un tour par événement."""
        from . import resident_runner
        if not resident_runner.enabled(self._state_read(resident_runner.SETTING)):
            return "resident=off"
        if self.runner.once:
            return "one turn only (--once)"
        if getattr(self.runner, "mediated", False):
            # L109 : le jeton de session et l'environnement du bail d'un
            # exécuteur médié ne survivent jamais au tour (codex r8, #417)
            return "mediated executor (lease-bound session token)"
        if self.runner.is_auditor(self.name, self.agent):
            return "auditor (its turns keep their own bounds)"
        if time.monotonic() < self._resident_refused_until:
            return "resident start failed recently"
        return resident_runner.refusal(adapter)

    def _spawn_resident(self, argv, **kwargs) -> subprocess.Popen:
        """Lance le harnais résident comme un harnais de tour : sous le verrou
        de lancement, compté parmi les harnais, publié tout de suite (un arrêt
        pendant la poignée de main le trouve)."""
        from . import resident
        with self.runner.launch_lock:
            # jamais moissonné hors de l'arrêt de son groupe (codex r2, #417)
            proc = resident.spawn_leader(argv, **kwargs)
            self.runner.harness_pids.add(proc.pid)
        with self.lock:
            self.proc = proc
            self.pgid = proc.pid
        return proc

    def _resident_for_turn(self, adapter, *, key: tuple, session: str | None, cwd: str,
                           env: dict, model, effort, tier, lot, stderr_path: str):
        """Le processus résident qui sert ce tour : celui qui tourne si sa clé
        de lancement et sa session sont celles du tour, sinon un neuf (repris
        sur la session). None si le démarrage échoue : ce tour, et les
        suivants pendant RESIDENT_RETRY_S, sont servis un par événement."""
        from . import containers as containers_mod, resident, resident_runner
        with self.lock:
            rp = self.resident
        if rp is not None:
            pourquoi = ("exited" if not rp.alive else
                        "being stopped" if rp.closing else
                        "launch settings changed" if rp.key != key else
                        "session changed" if (rp.session or None) != (session or None) else
                        "a turn still running" if rp.driver.busy else "")
            if not pourquoi:
                return rp
            self._close_resident("restarted: %s" % pourquoi)
        life_id = resident_runner.ResidentProcess.new_life_id()
        env = dict(env)
        env.update({
            "AMEESH_TURN_ID": life_id,
            containers_mod.LABELS_ENV: containers_mod.turn_labels(
                life_id, self.name, str(lot) if lot else None),
            resident_runner.TURN_FILE_ENV: self._path(resident_runner.TURN_FILE),
        })
        stderr = open(stderr_path, "ab")
        driver = resident.driver_for(
            adapter, cwd=cwd, session=session, model=model or None, effort=effort or None,
            tier=tier or None, env=env, stderr=stderr, spawn=self._spawn_resident)
        rp = resident_runner.ResidentProcess(driver, key=key, life_id=life_id, stderr=stderr)
        driver.on_event = rp.on_event
        try:
            driver.start()
        except resident.ResidentError as exc:
            rp.close(0)
            with self.lock:
                self.proc = None
                self.pgid = None
            with self.runner.launch_lock:
                self.runner.harness_pids.discard(rp.pgid)
            self._resident_refused_until = time.monotonic() + RESIDENT_RETRY_S
            log("[%s] resident start failed (%s): one turn per event for %ds"
                % (self.name, exc, int(RESIDENT_RETRY_S)))
            return None
        rp.resource_open = self._attach_resource(life_id, rp.pgid)
        with self.lock:
            self.resident = rp
        log("[%s] resident process started (pid %s, session %s)"
            % (self.name, rp.pgid, driver.session_id or "new"))
        return rp

    def _close_resident(self, reason: str) -> None:
        """Arrête le processus résident (son groupe entier) et ferme sa ligne
        de ressource. Jamais pendant un tour : le tour l'a dépublié avant.
        Il reste dans `self.resident` jusqu'à la fin de l'arrêt (codex r1,
        #417) : un bail perdu pendant la grâce le tue encore (`stop_group_now`)."""
        with self.lock:
            rp = self.resident
            if rp is None or rp.closing:
                return
            rp.closing = True
        try:
            rp.close(rp.CLOSE_GRACE)
        except Exception as exc:  # le groupe est tué quoi qu'il arrive
            rp.kill_now()
            log_async("[%s] resident close failed (%s): group killed" % (self.name, exc))
        finally:
            with self.lock:
                if self.resident is rp:
                    self.resident = None
        with self.runner.launch_lock:
            self.runner.harness_pids.discard(rp.pgid)
        if rp.resource_open:
            rp.resource_open = False
            self._release_resource(rp.life_id, rp.pgid)
        log_async("[%s] resident process stopped (%s) after %d turn(s)"
                  % (self.name, reason, rp.turns))

    def _close_turn_now(self, rp, raison: str) -> None:
        """Clôt le tour à un point sûr (plafond L105, retrait de l'hôte) : le
        harnais seul pour un tour par événement ; pour le résident, son
        interruption native, sans le tuer — à défaut, comme un tour."""
        if rp is not None:
            tour = rp.driver.current
            # un tour fini entre-temps n'a plus rien à interrompre
            if tour is None or rp.driver.interrupt() or tour.done:
                log_async("[%s] %s: resident turn interrupted" % (self.name, raison))
                return
        self.stop_group_now(raison, group=False)

    def _resident_result(self, rp, tour, voulu: bool) -> tuple[int, str | None]:
        """Code et erreur d'un tour résident fini (`voulu` : clos par
        l'exécuteur, une interruption n'est pas un échec)."""
        from . import resident
        if tour is None:
            return 1, None              # pas lancé : l'erreur est déjà notée
        fin = tour.wait(RESIDENT_END_WAIT_S)
        if fin is None:
            return 1, "resident turn did not end"
        if fin.status == resident.COMPLETED:
            return 0, None
        if fin.status == resident.INTERRUPTED:
            return (0, None) if voulu else (1, "resident turn interrupted")
        return 1, fin.error or "resident turn failed"

    def _resident_turn_end(self, rp) -> None:
        """Fin d'un tour résident : dépublié ; le processus reste pour le tour
        suivant, sauf s'il est mort, si son tour n'a pas fini, ou si le tour
        s'arrête en urgence (bail, préemption, relance, retrait de l'hôte)."""
        from . import resident_runner
        with self.lock:
            self.proc = None
            self.pgid = None
        try:
            resident_runner.write_turn(self._path(resident_runner.TURN_FILE), "")
        except OSError:
            pass
        rp.turns += 1
        rp.last_used = time.monotonic()
        raison = ("exited" if not rp.alive else
                  "its turn did not end" if rp.driver.busy else
                  self._urgent_end() or (
                      "the host is withdrawing" if self.host_yield is not None else
                      "stopping" if self.stopping.is_set() else ""))
        if raison:
            self._close_resident(raison)

    def _resident_between_turns(self) -> None:
        """Entre deux tours : un résident mort est fermé ; un résident
        inactif depuis `resident_sleep` est endormi (bail gardé, réveil au
        prochain tour sur sa session) ; `resident=off` l'arrête."""
        from . import resident_runner
        with self.lock:
            rp = self.resident
        if rp is None:
            return
        if not rp.alive:
            self._close_resident("exited")
        elif not resident_runner.enabled(self._state_read(resident_runner.SETTING)):
            self._close_resident("resident=off")
        else:
            sommeil = resident_runner.sleep_seconds(
                self._state_read(resident_runner.SLEEP_SETTING))
            if sommeil and time.monotonic() - rp.last_used >= sommeil:
                self._close_resident("asleep after %ds without a turn" % int(sommeil))

    def _menage_apres_tour(self, menage_tour, turn_id: str, spec: dict) -> None:
        """L73 : quota du dossier temporaire, worktrees apparus, /tmp signalé ;
        journalisé. Ne casse jamais la fin d'un tour."""
        if menage_tour is None:
            return
        try:
            lot = spec.get("lot") or self.agent.get("session_work_item") or None
            entries = menage_tour.end(lot=str(lot) if lot else None, turn_id=turn_id,
                                      db=self.db, host=self.runner.host)
            # conteneurs étiquetés par CE tour, sans lot : supprimés tout de
            # suite (ceux d'un lot attendent sa fin, au passage périodique)
            if not menage_tour.off:
                from . import menage as menage_mod
                entries += menage_mod.reap_containers(
                    self.runner.container_runtime(), self.db, self.runner.host,
                    only_turn=turn_id)["entries"]
            if entries:
                storage.of(self.db).housekeeping.log(
                    self.runner.host, entries, actor="runner:%s" % self.runner.runner_id)
                for entry in entries:
                    if entry["kind"] == "orphan":
                        # L162 : le sort de l'entrée est dans le détail
                        # (supprimée une fois abandonnée, ou jamais)
                        log("[%s] /tmp: %s (%s) appeared during the turn — reported (%s): %s"
                            % (self.name, entry["path"], entry.get("bytes"),
                               entry.get("detail", "").rpartition(" : ")[2],
                               (entry.get("data") or {}).get("command")))
        except Exception as exc:
            log("[%s] cleanup after turn %s failed (%s)" % (self.name, turn_id[:8], exc))

    def _with_background_note(self, spec: dict) -> tuple[dict, list[str]]:
        """Le travail de fond des tours précédents (délai de grâce) : ce qui
        tourne encore, ce qui a fini (code de sortie), ce qui a été arrêté —
        dit à l'agent en tête de consigne, jamais dans un tour de résumé. Rend
        la consigne et les tours clos à noter « dits » une fois le harnais
        lancé. Une consigne déjà à la borne d'un argument garde sa place : la
        note attend alors le tour suivant."""
        tracker = getattr(self.runner, "background", None)
        if tracker is None or spec.get("prompt") == adapters.SUMMARY_PROMPT:
            return spec, []
        note, dits = tracker.note_for(self.name)
        if not note:
            return spec, []
        if mail.octets(self._ouverture() + note + spec["prompt"]) > mail.ARG_SAFE_BYTES:
            return spec, []
        return dict(spec, prompt=note + spec["prompt"]), dits

    def modele_du_tour(self, harness: str, adapter, *, emprunte: bool) -> str:
        """Le modèle passé au harnais du tour (0019) : vide si le harnais est
        emprunté (L242b), vide aussi quand le réglage appartient positivement à
        la famille d'un **autre** harnais (#1268 : ligne de registre héritée
        d'une bascule, #1037 — la passer ferait un 400 côté fournisseur) ;
        sinon le réglage de l'agent, ou le défaut livré par le descripteur
        (DeepSeek, L60). Un identifiant de famille inconnue n'est jamais
        refusé : on ne devine pas."""
        model = "" if emprunte else (self.agent.get("model") or self._state_read("model"))
        if model and cost_mod.foreign_harness_model(harness, model):
            vu = self.__dict__.get("_foreign_model_seen")
            if vu != model:
                self._foreign_model_seen = model
                log_async("[%s] model %s ignored for harness %s: it belongs to another "
                          "harness's family (stale registry setting); the harness default "
                          "is used" % (self.name, model, harness))
                self._fil_note(
                    "Model %s was ignored for harness %s: it belongs to another "
                    "harness's family (stale registry setting, #1268). The harness "
                    "default is used for this turn." % (model, harness),
                    meta={"action": "modele-etranger", "modele": model, "harnais": harness})
            model = ""
        if not model and adapter.spec.model_file and adapter.spec.defaults.get("model"):
            # Modèle livré par fichier (DeepSeek, L60) : sans réglage, le défaut
            # du descripteur est passé explicitement. Le modèle du tour est
            # alors connu (grand livre au bon tarif, jamais « inconnu ») au
            # lieu de dépendre du profil local du harnais.
            model = adapter.spec.defaults["model"]
        return model

    def _marqueurs_durables(self, base: str) -> str:
        """Le statut d'un tour **en cours** garde les marqueurs durables posés
        par les échecs précédents (#1268/#1270) : `begin_turn` ne les efface
        plus, donc un redémarrage pendant la tentative relit la série, son
        début d'épisode et l'échéance de reprise — sans les recalculer (une
        échéance n'est jamais repoussée par une tentative interrompue)."""
        texte = base
        if self.rate_limit:
            texte = rate_limit_mod.status_text(
                texte, self.rate_limit, self.rate_limit_streak, self.rate_limit_since,
                rate_limit_mod.delay(self.rate_limit_streak),
                retry_at=self.rate_limit_retry_at or None)
        if self.failure_streak:
            from . import exploitation as exploitation_mod
            texte = exploitation_mod.turn_failure_status_text(
                texte, self.failure_streak, critical=account_turn.is_critical(self),
                since=self.failure_since)
        return texte

    def _run_turn(self, spec: dict) -> bool:
        if spec.get("lot_switch_failed"):
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            return False
        self._workdir_lot = spec.get("lot") or self.agent.get("session_work_item")
        cwd = self._resolve_workdir()
        if not cwd:
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            if self._lot_workdir_secondary:
                # #811 (B1) : seul ce lot attend son dossier ; l'agent garde
                # ses tours primaires (pas de blocage global « dossier absent »).
                self._lot_workdir_deferred(self._workdir_lot)
                return False
            self._workdir_missing(force_status=True)
            return False
        # First assignment to another repository, or a changed Host root:
        # a session can only resume in the directory that opened it.
        session = self.current_session()
        origin = self.agent.get("session_cwd")
        from . import projects as projects_mod
        if (session and len(projects_mod.agent_projects(self.agent)) > 1
                and (self.agent.get("session_origin") != session or not origin
                     or os.path.realpath(origin) != os.path.realpath(cwd))):
            lot = self._workdir_lot
            if not lot or not self._switch_lot(str(self.agent.get("session_work_item") or ""),
                                               str(lot)):
                return False
        # Travail de fond des tours précédents : en tête de consigne.
        spec, fond_dits = self._with_background_note(spec)
        harness = self.turn_harness()
        # L242b (3) : un harnais emprunté ne prend ni le modèle, ni l'effort,
        # ni le tier de l'agent (ceux de son harnais) : les défauts du sien
        emprunte = harness != ((self.agent or {}).get("harness") or "other")
        try:
            adapter = self._adapter()
        except adapters.HarnessMissing as exc:
            # L106 : erreur de l'hôte, pas de l'agent — consigne remise,
            # « hôte non prêt », nouvel essai à échéance ; aucun échec rapide.
            self._host_missing(str(exc))
            return False

        compte = account_turn.for_turn(self)  # L30 : compte de ce tour (ou None)
        session = self.current_session()
        connue = (self._session_connue(harness, compte, adapter)
                  if session and not self.runner.dry_run else None)
        if connue is not None and not connue(session):
            # L224 : jamais de --resume sur un id inconnu du harnais et du
            # compte — le harnais échouerait à chaque tour (échecs rapides,
            # puis arrêt) ; dernière session principale connue, sinon brief.
            self._ecarte_session_inconnue(session, connue)
            session = self.current_session()
        # Session neuve : résumé de rotation (règles relues) ou, sans résumé,
        # règles de travail du canon (premier démarrage) — `_ouverture` calcule
        # les deux, règles bornées comprises.
        resume = self._ouverture()
        # Modèle et effort par agent (0019) : lus à chaque tour, donc un
        # `ameesh set` prend effet au tour suivant. DeepSeek reçoit un patch YAML.
        model = self.modele_du_tour(harness, adapter, emprunte=emprunte)
        effort = "" if emprunte else (self._state_read("effort")
                                      or self.agent.get("effort") or "")
        # Tier (L26) : passé par le descripteur du harnais (Codex :
        # `service_tier`) ; sans effet sur un harnais qui n'en déclare pas.
        tier = "" if emprunte else (self._state_read("tier") or self.agent.get("tier") or "")
        patch = self._path("model.patch.yml") if (model or effort) else None
        # Decision 0029: designate neutral files; the harness manages memory.
        memory_preface = ""
        if self.agent.get("memory_repository") and not self.runner.dry_run:
            try:
                memory_mod.require_isolation(self.cfg, self.db, self.name)
                activation = not self.__dict__.get("_memory_activated", False)
                if activation:
                    path = memory_mod.open_copy(self.agent["memory_repository"],
                                                self.cfg.state_dir, self.name,
                                                cfg_raw=self.cfg.persona_memory)
                    self._memory_activated = True
                else:
                    path = memory_mod.copy_dir(self.cfg.state_dir, self.name)
                if activation or not session:
                    journal_id = "session-" + uuid.uuid4().hex
                    memory_preface = memory_mod.preface(path, self.name, journal_id)
            except (memory_mod.MemoryError, db_mod.DbError, OSError, subprocess.SubprocessError) as exc:
                log("[%s] persona memory unavailable for this turn: %s" % (self.name, exc))
        resume += memory_preface
        argv = adapter.command(resume + spec["prompt"], session,
                               model=model or None, effort=effort or None, patch=patch,
                               tier=tier or None)
        label = {"prompt": "consigne", "mail": "messages", "event": "événements",
                 "urgent": "prioritaire", "idle": "reprise",
                 "suite": "suite"}[spec["kind"]]
        #: L156c : `label` reste la valeur stockée (marqueur comptable, grand
        #: livre) ; `shown` est son libellé pour les humains (journal, statut)
        shown = TURN_LABELS_EN.get(label, label)
        if spec.get("jeton") and mail.octets(resume + spec["prompt"]) > mail.ARG_SAFE_BYTES:
            # Garde-fou : le budget l'interdit ; jamais un E2BIG au lancement.
            self.fail_turn("instruction too long", "instruction of %d bytes"
                           % mail.octets(resume + spec["prompt"]))
            return False
        if self.runner.dry_run:
            log("[%s] %s turn (dry-run, session %s): %s"
                % (self.name, shown, session or "new", " ".join(argv)))
            return True
        if not registry.begin_turn(self.db, self.name, self.runner.runner_id, self.epoch,
                                   self._marqueurs_durables(
                                       "%s turn (%s)" % (shown, harness))):
            log("[%s] lease lost or agent stopped before the turn" % self.name)
            return False
        lot = spec.get("lot")
        if lot and str(lot) != str(self.agent.get("session_work_item") or ""):
            # Le lot de la session courante (0025, L26) : noté au premier tour
            # d'une session neuve, ou après une rotation refusée ; fencé.
            if storage.of(self.db).operations.set_session_work_item(
                    self.name, self.runner.runner_id, self.epoch, str(lot)):
                self.agent["session_work_item"] = str(lot)
        self._note_delegate_turn(spec)

        # Keep the directory used for the resume check above. Resolving a
        # changed policy again here could resume a session in another cwd.
        if not os.path.isdir(cwd):
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            self._workdir_missing(force_status=True)
            return False

        # #896 : l'identité du tour vient du bail (#834), jamais du dépôt. Un
        # `user.name` laissé dans le `.git/config` partagé signerait les
        # commits lancés hors harnais depuis ce dossier (incident du
        # 13/10/2026) : on le signale, sans bloquer le tour ni corriger la
        # configuration à la place de son auteur.
        partagee = identity.repo_config_identity(cwd)
        if partagee and partagee.get("user.name", "") not in ("", self.name):
            log("[%s] shared git identity in %s (%s): the turn signs %s <%s>; "
                "remove user.* from the shared .git/config"
                % (self.name, cwd,
                   ", ".join("%s=%s" % (cle, val)
                             for cle, val in sorted(partagee.items())),
                   self.name, "%s@%s" % (self.name, identity.GIT_EMAIL_DOMAIN)))

        env = os.environ.copy()
        env.update(adapter.env())
        # Decision 0029 disables native account memory for managed sessions.
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        # L31 (0028) : identifiant du tour et étiquettes à poser sur les
        # conteneurs lancés par ses outils, pour repérer les orphelins.
        from . import containers as containers_mod
        turn_id = uuid.uuid4().hex
        self._turn_id = turn_id
        lot_du_tour = lot or self.agent.get("session_work_item") or None
        turn_label = containers_mod.turn_labels(
            turn_id, self.name, str(lot_du_tour) if lot_du_tour else None)
        identite = {
            "AGENT_MAIL_NAME": self.name,
            "AGENT_MAIL_STATE": self.cfg.v0_state,
            "AGENT_MAIL_CONFIG": self.cfg.config_dir,
            "AMEESH_TURN_ID": turn_id,
            # L224 : le hook d'un autre harnais lancé pendant le tour (codex
            # exec sous claude) est refusé
            "AMEESH_HARNESS": harness,
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
            # L105 : borne du courrier remis par le hook pendant ce tour
            "AMEESH_TURN_MAIL_MAX": str(self.turn_mail_max()),
        }
        # #834 : l'identité git de la session — auteur ET committer — vient du
        # nom d'agent du bail, jamais de la configuration du dépôt (partagée
        # par tous les worktrees : un `user.name` écrit par un agent valait
        # pour les autres, incident du 13/10/2026).
        identite.update(identity.git_identity(self.name))
        env.update(identite)
        if getattr(self.runner, "mediated", False):
            self._mediated_env(env)
        if not account_turn.apply(self, env, compte):
            return False  # profil inutilisable : consigne remise, rien lancé
        # L111 : exécuteur médié (appareil prêté) — URL du relais de modèle et
        # jeton de session court, dans l'environnement du harnais seulement ;
        # sans jeton, pas de tour (jamais de repli sur une clé locale).
        from . import relay as relay_mod
        relaye = False
        try:
            relay_env = None
            if getattr(self.runner, "mediated", False):
                # le relais est sur le serveur du mesh de CET exécuteur
                relay_env = {"AMEESH_RELAY_URL": os.environ.get("AMEESH_RELAY_URL", ""),
                             "AMEESH_EXEC_URL": getattr(self.db, "url", "")
                             or self.cfg.exec_url}
            relaye = relay_mod.apply_turn_env(env, harness, agent=self.name,
                                              owner=self.runner.runner_id, epoch=self.epoch,
                                              turn_id=turn_id, environ=relay_env)
        except relay_mod.RelayTurnError as exc:
            log("[%s] %s : tour non lancé" % (self.name, exc))
            self.fail_turn("relais de modèle indisponible", str(exc))
            return False
        # L73 : dossier temporaire de l'agent et caches gérés ; worktrees et
        # /tmp relevés avant le tour. Jamais bloquant : un ménage impossible
        # laisse le tour partir avec l'environnement d'origine. Pas sur un
        # exécuteur médié : le journal du ménage (`housekeeping.*`) n'est pas
        # au contrat 1.1, et le dossier de travail est effacé en fin de bail.
        menage_tour = None
        if not getattr(self.runner, "mediated", False):
            try:
                from . import menage as menage_mod
                # session neuve : dossier temporaire vidé — jamais sous le travail
                # de fond d'un tour précédent encore en délai de grâce
                fond = getattr(self.runner, "background", None)
                menage_tour = menage_mod.Turn.begin(
                    self.cfg, self.runner.housekeeping_policy(), self.name, cwd,
                    new_session=not session and not (fond and fond.has_jobs(self.name))
                    and self.resident is None,
                    base_env=env)
                env.update(menage_tour.env)
            except Exception as exc:
                log("[%s] cleanup before the turn failed (%s)" % (self.name, exc))
                menage_tour = None
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
            log("[%s] turn marker not written (%s)" % (self.name, exc))
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
        self.host_yield = None
        self.last_final = None
        self.summary_display = []
        # L105 : plafonds pendant le tour — jetons relus (une fois par appel)
        # et durée ; jamais sur un tour de résumé (rotation, bascule de compte)
        plafonne = spec.get("prompt") != adapters.SUMMARY_PROMPT
        relus = 0
        #: L217 : jetons relus par les sous-agents, à part et pour information
        #: (jamais comptés au plafond de contexte, qui mesure le fil principal)
        relus_sous_agents = 0
        relus_mesures = False  # L217b : le flux a relevé au moins un appel
        appels_vus: set = set()
        cloture: str | None = None
        clos = False
        self.last_turn_closed = None

        # Marqueur comptable **avant** le lancement : si on ne peut pas garantir
        # une trace, on ne dépense pas (fail-closed, L13 B5). Le marqueur est
        # écrit atomiquement et lu au redémarrage ; la session sera résolue par
        # `record` au moment de l'écriture.
        # L242b (3) : le harnais de la ligne de grand livre de ce tour ; non
        # écrit, le tour ne part pas (#389 r1) — sa dépense serait comptée
        # sous un autre harnais, hors du plafond payé au token
        if not self._state_note("turn_harness", harness) \
                or self._state_read("turn_harness") != harness:
            self.fail_turn("accounting unavailable", "turn harness not written")
            return False
        if not self._compta_marque(events_start, label, model,
                                   work_item_id=str(lot_du_tour) if lot_du_tour else None,
                                   turn_id=turn_id):
            self.fail_turn("accounting unavailable",
                           "accounting marker not written")
            return False
        # #1469 : réservation atomique du tour sur l'enveloppe du compte de
        # facturation, sous la clé du marqueur (le règlement la retrouve par
        # elle, jamais par agent) ; refusée, le tour ne part pas et le
        # marqueur est retiré. Un résumé de bascule peut imposer l'ancien
        # compte : réserver celui appliqué au harnais (`compte`).
        if account_budget.billing_of(compte):
            marqueur = self._compta_travail or {}
            refus = account_budget.reserve(
                self.db, self.name, compte,
                self._spend_key(events_start, marqueur.get("created_ts")))
            if refus:
                registry.pending_spend_clear(self.db, self.name,
                                             marqueur.get("created_ts"))  # rien n'est parti
                self._compta_travail = None
                self.fail_turn(budget_mod.PAUSE_PREFIX + refus, refus)
                return False

        heartbeat_done = threading.Event()
        self.heartbeat_thread = threading.Thread(target=self._heartbeat, args=(heartbeat_done,),
                                                 daemon=True)
        self.heartbeat_thread.start()
        preempt_done = threading.Event()
        # Un nouveau tour remet à zéro le latch de publication : il ne concerne
        # que la course du tour précédent, et les autres déclencheurs (bail,
        # arrêt, préemption) sont revérifiés à la publication (verdict B5).
        self.stop_requested.clear()
        self.host_yield = None
        if spec["kind"] == "urgent":
            # Ce tour sert justement le message prioritaire : le signal est consommé.
            self.preempting.clear()
        threading.Thread(target=self._preempt_monitor, args=(preempt_done,),
                         daemon=True).start()
        from .platform import audit_guard
        audit_exempt = (self.runner.is_auditor(self.name, self.agent)
                        and self.runner.host_pressure().get("blocked"))
        self._audit_guard_error = ""
        audit_seconds = min(audit_guard.WALL_SECONDS,
                            self.turn_max_seconds() or audit_guard.WALL_SECONDS)
        if audit_exempt:
            threading.Thread(target=self._audit_deadline_monitor,
                             args=(preempt_done, time.monotonic() + audit_seconds),
                             daemon=True).start()
        # L76 : servi par le processus résident de l'agent, ou un par événement
        from . import resident as resident_mod, resident_runner
        # L111 : le jeton du relais vaut pour CE tour — jamais gardé par un résident
        refus_resident = ("model relay (per-turn session token)" if relaye
                          else self._resident_refusal(adapter, audit_exempt))
        if refus_resident and self.resident is not None:
            self._close_resident("one turn per event: %s" % refus_resident)
        cle_resident = None if refus_resident else resident_runner.launch_key(
            adapter, model=model, effort=effort, tier=tier,
            account=compte.name if compte is not None else None, cwd=cwd, env=env)
        rp = None
        tour_resident = None
        #: #210 : l'empreinte du jeton qui part avec le tour, lue au lancement —
        #: le propriétaire peut se reconnecter sur un autre jeton pendant le tour
        empreinte = sentinel.launch_fingerprint(harness, compte, env)
        #: #408 : le harnais du lancement, origine d'une session qu'il ouvre
        harnais_lance = harness
        #: L106 : le harnais n'a pas pu être exécuté (binaire disparu depuis
        #: la résolution, interpréteur absent) — erreur de l'hôte
        echec_lancement = ""
        lignes = 0
        conversation_recorder = None
        conversation_usage = None
        proc = None
        background_handles = {}
        self._background_handles = background_handles
        #: L226b : début de la sortie d'erreur de CE tour dans `stderr.log`
        #: (ouvert en ajout) : l'échec d'un tour précédent n'y est pas relu
        stderr_debut = None
        try:
            with open(stderr_path, "ab") as stderr:
                stderr_debut = stderr.tell()
                if cle_resident is not None:
                    # L76 : le processus résident (démarré, ou relancé sur la
                    # session) ; un démarrage raté : un tour par événement
                    rp = self._resident_for_turn(
                        adapter, key=cle_resident, session=session, cwd=cwd, env=env,
                        model=model, effort=effort, tier=tier, lot=lot_du_tour,
                        stderr_path=stderr_path)
                if rp is None:
                    try:
                        # Le moissonneur de l'exécuteur ne touche jamais un harnais,
                        # même pas pendant son lancement (voir `background`).
                        with self.runner.launch_lock:
                            proc = subprocess.Popen(
                                self.runner.audit_command(self.name, argv, self.agent,
                                                          exempt=audit_exempt,
                                                          seconds=audit_seconds),
                                cwd=cwd, stdout=subprocess.PIPE, stderr=stderr,
                                text=True, bufsize=1, env=env,
                                start_new_session=True,  # son groupe : on peut le tuer en entier
                            )
                            self.runner.harness_pids.add(proc.pid)
                    except RuntimeError as exc:
                        raise _LancementImpossible(str(exc)) from exc
                    except OSError as exc:
                        raise _LancementImpossible("could not launch %s: %s"
                                                   % (argv[0], exc.strerror or exc)) from exc
            if rp is not None:
                # publié le temps du tour : le veilleur, la perte du bail, la
                # préemption et l'arrêt le tuent comme un harnais de tour
                proc = rp.proc
                with self.lock:
                    self.proc = proc
                    self.pgid = rp.pgid
                try:
                    resident_runner.write_turn(self._path(resident_runner.TURN_FILE), turn_id)
                except OSError as exc:
                    log("[%s] resident turn file not written (%s)" % (self.name, exc))
            else:
                with self.lock:
                    self.proc = proc
                    self.pgid = proc.pid  # start_new_session : le groupe porte son pid
                # L31 (0028) : rattache le groupe (et l'étiquette des conteneurs) au
                # tour, pour repérer ce qui survivrait à sa fin.
                self._open_turn_resource(turn_id, proc.pid)
            # Le trampoline valide le cgroup AVANT de lancer le harnais.
            # L'attente de ce signal est bornée par le minuteur indépendant.
            # Un scope refusé ne consomme aucun courrier.
            if audit_exempt and (proc.stdout is None
                                 or proc.stdout.readline().strip() != audit_guard.READY):
                raise _LancementImpossible("auditor scope not validated: memory cap "
                                           "missing or scope creation refused")
            # Lancé (et publié, pour que l'arrêt le trouve) : les messages de la
            # consigne sont livrés, et seulement eux.
            self._livre_courrier(spec)
            if fond_dits:  # la note du travail de fond a été montrée
                self.runner.background.mark_reported(fond_dits)
            # Course de publication : un déclencheur d'arrêt (bail perdu,
            # préemption, arrêt de l'exécuteur, échéance) a pu conclure pendant
            # que ce Popen n'était pas encore publié (proc=None). Le recontrôle
            # unique couvre toutes les causes (B5a-P, L11 B2).
            self.kill_if_stop_requested()
            if rp is not None:
                log("[%s] %s turn, resident process %s (%d turn(s) served)"
                    % (self.name, shown, rp.pgid, rp.turns))
                rp.drain()  # rien d'un tour précédent
                try:
                    tour_resident = rp.driver.send(resume + spec["prompt"])
                    flux = (json.dumps(raw, ensure_ascii=False)
                            for raw, _parsed in rp.stream(tour_resident))
                except resident_mod.ResidentError as exc:
                    error = "resident turn not started: %s" % exc
                    flux = ()
            else:
                log("[%s] %s turn: %s" % (self.name, shown, " ".join(argv[:6]) + " …"))
                flux = proc.stdout or ()
            # L78: the writer owns its connection; harness output never waits
            # for SQL or the readable thread. ResidentWorker uses the same sink.
            if getattr(self.db, "driver", "") != "mediated":
                from .conversation import Recorder
                conversation_recorder = Recorder(
                    self.cfg, agent=self.name, turn=turn_id, session=session,
                    project=fil.project_for(self.cfg, fil.agent_project(self.agent)),
                    reader=adapter.reader, warn=log_async)
                conversation_recorder.append("input", spec.get("prompt") or "",
                                             author="ameesh:runner")
            with open(events_path, "a", encoding="utf-8") as events:
                for line in flux:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    lignes += 1
                    events.write(line + "\n")
                    events.flush()
                    parsed = self._parse(adapter, line)
                    if conversation_recorder is not None:
                        conversation_recorder.on_event(line, parsed)
                        if isinstance(parsed.get("usage"), dict):
                            conversation_usage = parsed["usage"]
                    if parsed.get("session") and not session_new:
                        session_new = parsed["session"]
                        self.write_session_file(session_new)
                        if resume:
                            # #389 r2 : le résumé a ouvert une session connue
                            # (gardé jusque-là : un départ avorté le rejoue)
                            self._resume_drop()
                        # L242b (3) : le harnais de la session (rotation au
                        # changement de harnais, `account_turn.continuity`)
                        self._state_note("session_harness", harness)
                        # L39 (0030) : le compte d'origine de la session, en
                        # registre avec son id (le marqueur du flux reste la
                        # source des relevés du tour) ; #408 : et le harnais
                        # et le dossier de son ouverture, ceux du lancement,
                        # seulement si le tour l'OUVRE (une session reprise
                        # garde l'origine de son ouverture)
                        ouverte = session_new != session
                        origine = (harnais_lance if ouverte else None,
                                   cwd if ouverte else None)
                        try:
                            registry.set_session(
                                self.db, self.name, session_new,
                                compte.name if compte is not None else None,
                                harness=origine[0], cwd=origine[1])
                        except db_mod.DbError as exc:
                            # L72 : une panne de base ne tue pas le tour (le
                            # bail, lui, est défendu par le battement) ;
                            # l'enregistrement est rejoué après le tour.
                            self._session_en_attente = (
                                session_new, compte.name if compte is not None else None,
                                *origine)
                            log_async("[%s] session %s not recorded (%s): replayed "
                                      "after the turn" % (self.name, session_new,
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
                        if not plafonne and len(self.summary_display) < 10_000:
                            self.summary_display.append(text)
                    if "final" in parsed:
                        self.last_final = str(parsed["final"] or "")
                    if parsed.get("safe_point") and not clos and self.host_yield is None \
                            and self.runner.gate_holds():
                        # L159f : l'hôte se retire (tout tour, résumé compris) —
                        # point sûr, l'appel d'outil en cours est fini et son
                        # résultat est dans la session. Le harnais SEUL, comme
                        # une clôture : le travail de fond garde sa grâce,
                        # bornée par l'échéance du retrait.
                        self.host_yield = self.HOST_SAFE_POINT
                        self._close_turn_now(
                            rp, "the host is withdrawing: turn stopped at a safe point")
                    if plafonne:
                        # L217b : le flux se lit jusqu'au bout, même après la
                        # clôture — un appel déjà en tampon compte encore.
                        relus_mesures = relus_mesures or isinstance(
                            parsed.get("call_usage") or parsed.get("subagent_call_usage"), dict)
                        appel = parsed.get("call_usage")
                        if isinstance(appel, dict):
                            ident = parsed.get("call_id")
                            if not ident or ident not in appels_vus:
                                if ident:
                                    appels_vus.add(ident)
                                relus += adapters.reread_tokens(harness, appel)
                        appel = parsed.get("subagent_call_usage")
                        if isinstance(appel, dict):
                            ident = parsed.get("call_id")
                            if not ident or ident not in appels_vus:
                                if ident:
                                    appels_vus.add(ident)
                                relus_sous_agents += adapters.reread_tokens(harness, appel)
                        if cloture is None and not clos:
                            cloture = self.turn_close_due(started, relus)
                            if cloture:
                                log_async("[%s] %s: turn closed at the next safe point"
                                          % (self.name, cloture))
                        if cloture and not clos and parsed.get("safe_point") \
                                and self.host_yield is None:
                            # Point sûr : l'appel d'outil en cours est fini et son
                            # résultat est dans la session. Aucun harnais mené
                            # n'offre d'interruption propre en mode non
                            # interactif : arrêt propre habituel (SIGTERM, grâce)
                            # du harnais SEUL — le travail lancé en fond pendant
                            # le tour garde son délai de grâce.
                            clos = True
                            self._close_turn_now(rp, "turn closed: %s" % cloture)
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
                        error = "lease lost during the turn"
                        self.stop_group_now("lease lost during the turn", grace=0, hard=True)
                        break
            if rp is None:
                if (not self.runner.once and self.turn_grace_seconds() > 0
                        and not self._urgent_end()):
                    captured = self.runner.background.capture_before_wait(proc)
                    for pid, handle in captured.items():
                        if pid in background_handles:
                            handle.close()
                        else:
                            background_handles[pid] = handle
                result_code = proc.wait()
            else:
                result_code, erreur_resident = self._resident_result(
                    rp, tour_resident, clos or self.host_yield is not None)
                error = error or erreur_resident
        except _LancementImpossible as exc:
            echec_lancement = str(exc)
            result_code = 127
        finally:
            heartbeat_done.set()
            preempt_done.set()
            pgid = self.pgid
            if rp is not None:
                self._resident_turn_end(rp)
            # Fin NORMALE (harnais fini et moissonné, aucun arrêt d'urgence) :
            # ce qui tourne encore — processus du groupe, conteneurs du tour
            # sans lot — garde un délai de grâce (`background`). Sinon, ou
            # sans délai, le nettoyage habituel, tout de suite.
            fond = None
            handoff_failed = False
            try:
                if (rp is None and proc is not None and pgid and proc.poll() is not None
                        and not self.runner.once and self.turn_grace_seconds() > 0
                        and not self._urgent_end()):
                    fond = self.runner.background.survivors(
                        turn_id, pgid, handles=background_handles)
            except Exception as exc:  # dans le doute, le nettoyage d'avant
                log_async("[%s] background work of turn %s not examined (%s)"
                          % (self.name, turn_id[:8], exc))
                handoff_failed = True
                fond = None
            cleanup_survives = handoff_failed
            if fond is None and rp is None:
                # Arrêt escaladé (SIGTERM puis SIGKILL) : un `Popen.terminate()`
                # sec laisserait vivre un harnais qui ignore SIGTERM, hors de portée
                # du veilleur une fois `self.proc` effacé (sonde codex3 B5a-S).
                self.terminate()
            with self.lock:
                self.proc = None
                self.pgid = None
            if rp is None:
                with self.runner.launch_lock:
                    self.runner.harness_pids.discard(pgid)
            if fond is not None:
                self.runner.background.start(
                    agent=self.name, turn_id=turn_id, pgid=pgid, members=fond[0],
                    containers=fond[1], grace_s=self.turn_grace_seconds(),
                    handles=background_handles)
            elif rp is None:
                if background_handles:
                    from .background import Entry
                    # A failed handoff must not discard detached live targets.
                    # Clean through their pidfds and retain uncertainty even
                    # when all known handles exit successfully.
                    cleanup = Entry(agent=self.name, turn_id=turn_id, pgid=pgid,
                                    ended=0, deadline=0, grace_s=0,
                                    handles=background_handles,
                                    unverified=handoff_failed or any(
                                        h.live() for h in background_handles.values()))
                    cleanup_survives |= self.runner.background._kill(cleanup)
                self._close_turn_resource(turn_id, pgid, survives=cleanup_survives)
            self._background_handles = None
            self._menage_apres_tour(menage_tour, turn_id, spec)
            if conversation_recorder is not None:
                interrupted = (self.lease_lost.is_set() or self.preempting.is_set()
                               or self.restarting.is_set() or clos)
                conversation_recorder.finish(
                    final=conversation_recorder.final_text or "",
                    status="interrupted" if interrupted else (
                        "failed" if result_code or error or sys.exc_info()[0] else "completed"),
                    cost=cost, usage=conversation_usage,
                    duration_s=max(0, time.time() - started))
                conversation_recorder.close()

        duration = time.time() - started
        self.last_turn_seconds = duration
        # L217 : fil principal et sous-agents, mesurés à part dans le flux
        self.last_turn_main_reread = relus
        self.last_turn_subagent_reread = relus_sous_agents
        # L217b : la rotation entre deux tours juge sur ce même compteur
        self.last_turn_context = relus if relus_mesures else None
        self.last_turn_context_session = session_new or session or None
        if resume and not session_new and not result_code and not error:
            self._resume_drop()  # tour achevé sans id de session : livré
        if relus_sous_agents:
            log_async("[%s] tokens reread in the turn: %d main thread, %d subagents "
                      "(outside the cap)" % (self.name, relus, relus_sous_agents))
        lost = self.lease_lost.is_set()
        preempte = self.preempting.is_set()
        # Comptabilité **avant** toute sortie anticipée : la dépense a eu lieu,
        # même si le bail a été perdu ou le tour préempté (L13 B1). Le modèle
        # est celui du lancement, déjà figé dans le marqueur (L13 B4).
        # #1469 : issue incertaine (erreur, bail perdu, préemption, arrêt
        # par plafond) : la réservation du compte reste comptée en entier
        self._compta_termine(modele_annonce, incertain=bool(
            error or result_code or lost or preempte or clos))
        if clos and not lost and not preempte and not self.restarting.is_set():
            # L105 : tour clos par un plafond — un tour abouti, pas un échec ;
            # le code de sortie est celui de l'arrêt demandé.
            result_code, error = 0, None
        if self._audit_guard_error:
            result_code, error = 124, self._audit_guard_error
        ok = result_code == 0 and not lost and not error
        if error is None and result_code != 0:
            error = "the harness returned code %d" % result_code
        status = "idle" if ok else "blocked"
        if lost:
            # Le bail n'est plus vivant : on ne clôt PAS le tour. `end_turn`
            # effacerait current_prompt alors que le tour n'a pas abouti (son
            # fence par epoch ne suffit pas : un bail expiré mais pas encore
            # repris porte toujours notre epoch), et `set_status` écraserait
            # l'état d'un agent qu'un remplaçant a pu reprendre.
            self._avec_reprise("instruction put back in the queue", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            return False
        if self.host_yield is not None:
            # L112 : l'hôte se retire — comme une préemption : la consigne
            # repart en attente, la session est gardée ; le worker rend son
            # bail au passage suivant (`_leave_for_host`).
            raison = self.host_yield
            self._avec_reprise("consigne remise en attente", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            self._avec_reprise("statut de fin de tour", registry.set_status,
                               self.db, self.name, "queued",
                               status_text="tour interrompu : l'hôte se retire (%s)" % raison)
            self._fil_note(
                "Tour interrompu après %ds : l'hôte se retire (%s). La consigne repart "
                "en attente ; le travail reprendra dans la même session au retour de "
                "l'hôte." % (int(duration), raison),
                meta={"action": "retrait-hote", "raison": raison,
                      "tour_s": round(duration, 1)})
            return False
        if self.restarting.is_set() and not preempte and not ok:
            # `ameesh restart` (L26) : le tour est arrêté ; sa consigne repart en
            # attente, DERRIÈRE le brief que la demande placera en tête.
            self._avec_reprise("instruction put back in the queue", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            self._avec_reprise("end-of-turn status", registry.set_status,
                               self.db, self.name, "queued",
                               status_text="turn stopped: restart requested")
            return False
        if self.host_yield is not None and not preempte:
            # L159f : l'hôte se retire — comme une préemption : la consigne
            # repart en attente, la session est gardée ; le worker rend son
            # bail au passage suivant (`_leave_for_host`).
            raison = self.host_yield
            if self._session_en_attente is not None:
                self._avec_reprise("turn session", self._enregistre_session)
            self._avec_reprise("instruction put back in the queue", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            self._avec_reprise("end-of-turn status", registry.set_status,
                               self.db, self.name, "queued",
                               status_text="turn interrupted: the host is withdrawing (%s)" % raison)
            self._fil_note(
                "Turn interrupted after %ds: the host is withdrawing (%s). The turn's "
                "instruction goes back in the queue; the work resumes in the same "
                "session when the host is back." % (int(duration), raison),
                meta={"action": "retrait-hote", "raison": raison,
                      "tour_s": round(duration, 1)})
            return False
        if preempte:
            # Message prioritaire reçu pendant le tour (0018) : la consigne
            # repart en attente, `pick()` servira le message en tête au tour
            # suivant, sur la même session. Le fil garde la trace.
            self._avec_reprise("instruction put back in the queue", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            self._avec_reprise("end-of-turn status", registry.set_status,
                               self.db, self.name, "queued",
                               status_text="turn interrupted: priority message")
            self._fil_note(
                "Turn interrupted by a priority message; the turn's instruction "
                "goes back in the queue and the priority message goes first.",
                meta={"action": "interruption", "tour_s": round(duration, 1)})
            return False
        session_fille_ecartee = False
        if not ok:
            # B6a : le tour n'a pas abouti, la consigne repart en attente au lieu
            # d'être effacée par end_turn. Au pire elle est rejouée.
            self._avec_reprise("instruction put back in the queue", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            self.fast_failure = duration < self.runner.fast_failure_s
            hote = echec_lancement or self._echec_d_hote(
                result_code, lignes, session_new, duration, stderr_path)
            if self._power_texte:
                hote = self._power_texte
            if hote:
                # L106 : l'hôte, pas l'agent — ni échec rapide (L48), ni arrêt ;
                # statut « hôte non prêt », nouvel essai à échéance (ou reprise
                # au retour du secteur pour un arrêt sur batterie).
                self.fast_failure = False
                if self._session_en_attente is not None:
                    self._avec_reprise("turn session", self._enregistre_session)
                self._avec_reprise(
                    "end of turn", registry.end_turn,
                    self.db, self.name, self.runner.runner_id, self.epoch,
                    status="blocked", status_text=self.HOST_STATUS,
                    error="%s : %s" % (self.HOST_ERROR, hote), cost_usd=cost)
                self.last_activity = time.monotonic()
                if not self._power_texte:
                    self._host_not_ready(hote)
                return False
            if self._end_relay_auth_failure(cost):
                return False
            # Only the server may report provider credential health.
            raison_auth = None if self.runner.mediated else auth_failure.classify(
                error, self.last_final, auth_failure.stderr_tail(stderr_path, start=stderr_debut))
            if raison_auth:
                # L226b : le compte n'est plus authentifié — ni échec rapide ni
                # relance en boucle : le compte est noté en échec, le tour
                # suivant bascule vers un compte sain, sinon l'agent reste
                # `auth_blocked` jusqu'au retour d'un compte sain.
                self.fast_failure = False
                nom_harnais = harness
                account = auth_failure.account_name(compte)
                # #210 : l'empreinte du jeton du tour (lue au lancement, pas
                # maintenant) — la sentinelle ne lève l'échec que pour un autre
                self._avec_reprise("account health", auth_failure.report, self.db,
                                   self.cfg.host, nom_harnais, account, "failing",
                                   reason=raison_auth, source="turn", fingerprint=empreinte)
                if self._session_en_attente is not None:
                    self._avec_reprise("turn session", self._enregistre_session)
                texte = auth_failure.status_text(nom_harnais, account)
                self._avec_reprise(
                    "end of turn", registry.end_turn,
                    self.db, self.name, self.runner.runner_id, self.epoch,
                    status="blocked", status_text=texte,
                    error="%s %s" % (auth_failure.STATUS_PREFIX, raison_auth), cost_usd=cost)
                self.last_activity = time.monotonic()
                self._auth_note(nom_harnais, account, raison_auth, texte, turn=True)
                return False
            session_fille_ecartee = bool(session and adapters.child_session_refused(error))
            if session_fille_ecartee:
                # L166 : la session reprise était une session fille ; la
                # reprendre encore échouerait de même. Pas un échec rapide, et
                # pas un échec « réel » de la série #1268 (B2) : le tour suivant
                # repart d'une session saine.
                self.fast_failure = False
                self._avec_reprise("child session discarded",
                                   self._ecarte_session_fille, session)
            # #1270 : un 429 de débit (ou un quota fournisseur) n'est pas un
            # échec rapide — il attend puis reprend seul, à délais cadencés ;
            # le quota est distingué du débit passager et la garde comptable
            # (L226b) reste seule juge du budget. Un échec d'une autre cause
            # efface l'épisode (hors refus de session fille, qui n'est pas une
            # panne fournisseur et n'efface ni n'étend l'épisode).
            genre = rate_limit_mod.classify(
                error, self.last_final,
                auth_failure.stderr_tail(stderr_path, start=stderr_debut))
            if genre:
                self.fast_failure = False
                if genre == self.rate_limit:
                    self.rate_limit_streak += 1
                else:
                    self.rate_limit = genre
                    self.rate_limit_streak = 1
                    self.rate_limit_since = time.time()
                # échéance absolue, écrite dans `status_text` : elle survit au
                # redémarrage et ne se recalcule pas à chaque réveil (#1270 B1)
                self.rate_limit_retry_at = (time.time() + rate_limit_mod.delay(
                    self.rate_limit_streak))
            elif not session_fille_ecartee:
                self.rate_limit = None
                self.rate_limit_streak = 0
                self.rate_limit_since = 0.0
                self.rate_limit_retry_at = 0.0
        if self._session_en_attente is not None:
            self._avec_reprise("turn session", self._enregistre_session)
        if ok and clos:
            self._turn_closed(cloture or "cap", duration)
        elif ok:
            self._note_courrier_borne(turn_id, duration)
        # #1268 : série d'échecs de tour **réels**. Les échecs classés (hôte,
        # authentification, bail) sont rendus plus haut ; le refus d'une
        # session fille est écarté ici — il ne compte pas dans la série (B2)
        # et ne l'efface pas non plus : seul un succès remet la série à zéro.
        if ok:
            self.failure_streak = 0
            self.failure_since = 0.0
            self.rate_limit, self.rate_limit_streak, self.rate_limit_since = None, 0, 0.0
            self.rate_limit_retry_at = 0.0
        elif not session_fille_ecartee:
            if self.failure_streak < 1:
                # début d'épisode : il ne bouge plus tant qu'un succès n'a pas
                # remis la série à zéro (clé d'alerte stable, #1268 B1)
                self.failure_since = time.time()
            self.failure_streak += 1
        texte_fin = ("%s in %ds%s" % (shown, int(duration), "" if ok else " (failed)")
                     + (" (closed: %s)" % cloture.split(":")[0]
                        if ok and clos and cloture else ""))
        if not ok and self.rate_limit:
            # #1270 : l'épisode de débit est écrit dans le statut — visible
            # dans `ameesh list`/`show`, relu après un redémarrage, et lu par
            # l'alerte #1268 (seuil de 15 min). `retry at` porte l'échéance
            # absolue, jamais recalculée à la relecture.
            texte_fin = rate_limit_mod.status_text(
                texte_fin, self.rate_limit, self.rate_limit_streak,
                self.rate_limit_since, rate_limit_mod.delay(self.rate_limit_streak),
                retry_at=self.rate_limit_retry_at or None)
        if not ok and self.failure_streak:
            from . import exploitation as exploitation_mod
            texte_fin = exploitation_mod.turn_failure_status_text(
                texte_fin, self.failure_streak,
                critical=account_turn.is_critical(self), since=self.failure_since)
        self._avec_reprise(
            "end of turn", registry.end_turn,
            self.db, self.name, self.runner.runner_id, self.epoch,
            status=status, status_text=texte_fin,
            error=error, cost_usd=cost,
        )
        self.last_activity = time.monotonic()
        self._backup_session(adapter, compte, session_new or session)
        if ok:
            self.session_turns += 1
        self._sync_memory()
        if spec["kind"] == "idle":
            self.nudged = True
        with self.runner.lock:
            self.runner.turns += 1
        return ok

    def _turn_closed(self, raison: str, duration: float) -> None:
        """Après un tour clos par un plafond (L105) : la suite au tour suivant,
        et, pour le plafond de contexte, la rotation avec résumé de reprise
        avant lui. Journalisé, et noté dans le fil."""
        self.last_turn_closed = raison
        self.suite = raison
        contexte = raison.startswith("context cap")
        if contexte:
            self.rotation_forcee = raison
        log_async("[%s] turn closed after %ds (%s): %s" % (
            self.name, int(duration), raison,
            "session rotation then continuation" if contexte
            else "continued in the next turn"))
        self._fil_note(
            "Turn closed by the runner at a safe point after %ds (%s). %s"
            % (int(duration), raison,
               "The session is rotated (resume summary) before the next turn, "
               "which resumes the work in progress." if contexte else
               "The next turn resumes the work in progress, in the same session."),
            meta={"action": "tour-clos", "raison": raison, "tour_s": round(duration, 1)})

    def _note_courrier_borne(self, turn_id: str, duration: float) -> None:
        """Le hook a-t-il borné le courrier de ce tour ? (L105) Le tour a été
        conclu sur son invitation : journal et fil, le reste du courrier ouvre
        le tour suivant, et la rotation existante a son passage entre les deux."""
        from . import cli as cli_mod
        etat = cli_mod.tour_lit(self.cfg, self.name, turn_id)
        if not etat.get("signale"):
            return
        self.last_turn_closed = "mail cap"
        log_async("[%s] turn concluded after %ds: mail cap (%d delivered, %d waiting)"
                  % (self.name, int(duration), etat["remis"], etat["attente"]))
        self._fil_note(
            "Turn concluded after %ds on the mail cap: %d message(s) delivered "
            "during the turn, %d waiting for the next turn."
            % (int(duration), etat["remis"], etat["attente"]),
            meta={"action": "courrier-borne", "remis": etat["remis"],
                  "attente": etat["attente"], "tour_s": round(duration, 1)})

    def _backup_session(self, adapter, account, session) -> None:
        """Back up the completed session in a nonblocking background worker."""
        if not session or self.runner.dry_run or not self.cfg.session_backup:
            return
        try:
            conf = session_backup_mod.parse(self.cfg.session_backup)
        except session_backup_mod.BackupError as exc:
            if not getattr(self, "_backup_config_reported", False):
                log("[%s] session backup disabled : %s" % (self.name, exc))
                self._backup_config_reported = True
            return
        descriptor = getattr(adapter, "descriptor", None)
        patterns = getattr(descriptor, "session_files", ()) if descriptor else ()
        if conf is None or not patterns:
            return
        harness = self.agent.get("harness") or ""
        home = (account.home() if account is not None else os.path.abspath(os.path.expanduser(
            os.environ.get(accounts_mod.config_env_of(harness))
            or accounts_mod.default_home_of(harness))))
        lock = self.__dict__.setdefault("_backup_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return          # A backup for this agent is already running.

        def copy_session() -> None:
            try:
                r = session_backup_mod.backup(
                    conf, host=self.cfg.host, agent=self.name, home=home, patterns=patterns,
                    session=session, state_dir=self.cfg.state_dir)
                if r["status"] == "copied":
                    log_async("[%s] session %s backed up off device (%d file(s), %d bytes)"
                        % (self.name, session, r["files"], r["bytes"]))
                elif r["status"] == "not_found":
                    log_async("[%s] backup: session %s files not found under %s"
                        % (self.name, session, home))
            except Exception as exc:  # noqa: BLE001 - backup failure must not fail a turn
                log_async("[%s] session %s backup failed : %s" % (self.name, session, exc))
            finally:
                lock.release()

        threading.Thread(target=copy_session, daemon=True,
                         name="%s-session-backup" % self.name).start()

    def retry_remaining(self) -> float:
        """#1270 : secondes restantes avant la reprise cadencée durable, 0 sinon.

        L'échéance (`retry at`) est celle écrite au tour en échec : elle est
        relue de la ligne après un redémarrage et ne se recalcule pas. C'est
        elle qui garde le lancement — un courrier, un reset ou le budget ne la
        court-circuitent pas (B1 cx3)."""
        if not self.rate_limit:
            return 0.0
        if self.rate_limit_retry_at:
            return max(0.0, self.rate_limit_retry_at - time.time())
        return rate_limit_mod.delay(self.rate_limit_streak)

    def _attendre_reprise(self, duree: float) -> None:
        """Attend l'échéance de reprise sans se laisser court-circuiter.

        Les réveils (courrier, reset, budget) sont consommés sans lancer de
        tour ; le bail est renouvelé **avant son échéance**, par tranches de
        `renew_wait` (B1 cx3 : dormir jusqu'à la deadline faisait refuser le
        renouvellement au réveil), et l'arrêt, le drainage ou la fermeture de
        la porte d'hôte interrompent l'attente."""
        fin = time.monotonic() + max(0.0, float(duree))
        while not self.stopping.is_set() and not self.runner.stop.is_set():
            if self.runner.draining.is_set() or self.runner.gate_holds():
                return
            reste = fin - time.monotonic()
            if reste <= 0:
                return
            self.wake.wait(timeout=self.renew_wait(reste))
            self.wake.clear()
            if not self.renew():
                return

    def _attendre_apres_echec(self, duree: float) -> None:
        """Attend `duree` après un échec, bail renouvelé, **sans troncature**.

        Quand la durée tient sous le pas de renouvellement (`renew_wait`),
        un seul `wake.wait` suffit — écourté par un réveil (courrier) comme
        avant. Au-delà (#1270 : une reprise cadencée peut dépasser le bail de
        300 s), l'attente se découpe et le bail est renouvelé entre les
        tranches : dormir jusqu'à l'échéance faisait refuser `renew()` au
        réveil et le worker rendait le bail (R1 cx3 B1)."""
        fin = time.monotonic() + max(0.0, float(duree))
        while not self.stopping.is_set() and not self.runner.stop.is_set():
            reste = fin - time.monotonic()
            if reste <= 0:
                return
            tranche = self.renew_wait(reste)
            reveil = self.wake.wait(timeout=tranche)
            self.wake.clear()
            if reveil or tranche >= reste:
                return
            if not self.renew():
                return

    def failure_wait(self) -> float:
        """Attente avant le tour suivant après un échec : doublée à chaque échec
        consécutif, bornée par `failure_backoff_max` (L48).

        #1268 : la série d'échecs **réels** compte aussi (pas seulement les
        échecs rapides) — un harnais qui échoue lentement ne doit pas être
        relancé toutes les 5 s sans fin.

        #1270 : pour un épisode de débit/quota, c'est le temps **restant**
        jusqu'à l'échéance durable (`retry at`) — jamais l'arrêt L48, jamais
        un délai repoussé par un réveil."""
        if self.rate_limit:
            return self.retry_remaining()
        base = self.runner.failure_backoff
        n = max(self.fast_failures, self.failure_streak)
        if n <= 1:
            return base
        return min(self.runner.failure_backoff_max,
                   base * (2 ** (n - 1)))

    def _sync_memory(self) -> None:
        """Transport the persona session's memory changes at turn end, in the background."""
        if not self.agent.get("memory_repository") or self.runner.dry_run:
            return
        if not self.__dict__.get("_memory_activated", False):
            return
        try:
            memory_mod.require_isolation(self.cfg, self.db, self.name)
        except (memory_mod.MemoryError, db_mod.DbError) as exc:
            log_async("[%s] persona memory disabled: %s" % (self.name, exc))
            return
        path = memory_mod.copy_dir(self.cfg.state_dir, self.name)
        if not os.path.isdir(os.path.join(path, ".git")):
            return
        lock = self.__dict__.setdefault("_memory_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return

        def push_memory() -> None:
            try:
                r = memory_mod.sync(path, "memory for %s: turn end" % self.name,
                                     cfg_raw=self.cfg.persona_memory,
                                     repository=self.agent["memory_repository"])
                if r["status"] == "pushed":
                    log_async("[%s] persona memory pushed (%s)" % (self.name, r["commit"]))
                elif r["status"] == "secret":
                    log_async("[%s] memory alert: likely secret in %s; commit refused"
                        % (self.name, ", ".join(r["files"])))
                elif r["status"] == "destructive":
                    log_async("[%s] memory alert: destructive deletions refused; copy kept" % self.name)
                elif r["status"] == "push_rejected":
                    log_async("[%s] memory alert: push rejected; copy kept (%s)"
                        % (self.name, r["error"]))
            except Exception as exc:  # noqa: BLE001 - memory failure must not fail a turn
                log_async("[%s] memory sync failed : %s" % (self.name, exc))
            finally:
                lock.release()

        threading.Thread(target=push_memory, daemon=True,
                         name="%s-memory" % self.name).start()

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
        texte = "stopped after %d consecutive fast failures" % n
        log("[%s] %s: %s" % (self.name, texte, derniere or "unknown cause"))
        try:
            registry.set_status(self.db, self.name, "stopped", status_text=texte,
                                error=derniere or None, stop_reason="erreur")
        except db_mod.DbError as exc:
            log("[%s] stop after failures not recorded: %s" % (self.name, exc))
        self._fil_note(
            "Agent stopped by the runner after %d consecutive fast failures "
            "(last error: %s). The instruction stays queued; fix the "
            "cause then `ameesh resume %s`." % (n, derniere or "unknown", self.name),
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
            return {"error": "unreadable stream: %s" % exc}

    # -- boucles -----------------------------------------------------------
    def process_once(self, wait_seconds: float) -> bool:
        """Mode banc : au plus un tour, en attendant `wait_seconds` du travail."""
        self.ensure_watchdog()
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            # #1270 B1 : pas de tentative avant l'échéance de reprise durable ;
            # un réveil déjà posé ne fait pas repartir le tour plus tôt.
            if self.retry_remaining() > 0:
                if time.monotonic() >= deadline:
                    return False
                self.wake.wait(timeout=min(0.25, max(0.02, deadline - time.monotonic())))
                self.wake.clear()
                continue
            self.apply_restart_if_requested()
            self.maybe_rotate()
            spec = self.rotate_for_lot(self.pick())
            if spec:
                # #283 r2 : la même garde que `run()` — une rotation a pu
                # déplacer l'agent ; la consigne reste alors en attente
                if self._moved_before_turn(spec):
                    return False
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
                if self.runner.draining.is_set():
                    break  # drainage : le tour d'avant est fini, aucun autre ne part
                if getattr(self.runner, "restart_for_stale", False):
                    # L790 : code périmé constaté — le tour d'avant est fini,
                    # aucun autre ne part ; le bail est rendu par le `finally`,
                    # la boucle principale se remplace ensuite.
                    log("[%s] outdated runner: no new turn, lease released" % self.name)
                    break
                if self.runner.gate_holds():
                    # L159f : porte d'hôte fermée — le bail est rendu (finally)
                    self._leave_for_host()
                    break
                if not self.renew():
                    break
                # L72 : une base injoignable n'arrête plus le worker (son fil
                # mourait sur l'exception) : tours suspendus, attente
                # croissante, reprise d'elle-même au retour de la base.
                try:
                    if self._consigne_a_restaurer:
                        self._restaure_consigne()
                    ailleurs = self.placed_elsewhere()
                    if ailleurs:
                        # L147 : the agent was placed on another host (canon)
                        # while this worker held its lease: between two turns,
                        # the lease is released so that host can claim it
                        log("[%s] placed on host %s, not %s: lease released after the "
                            "current turn" % (self.name, ailleurs, self.runner.host))
                        break
                    self.apply_restart_if_requested()
                    self.maybe_rotate()
                    # #1270 B1 : l'échéance de reprise durable garde le
                    # lancement du tour ; un courrier, un reset ou le budget
                    # ne la court-circuitent pas — l'attente consomme les
                    # réveils et renouvelle le bail. Aucune consigne n'est
                    # consommée avant l'échéance (`pick` est après).
                    attente_reprise = self.retry_remaining()
                    if attente_reprise > 0:
                        self._attendre_reprise(attente_reprise)
                        continue
                    spec = self.rotate_for_lot(self.pick())
                    if spec is not None and self._moved_before_turn(spec):
                        break
                except db_mod.Unavailable as exc:
                    self._panne_de_base(exc, "choosing the turn")
                    continue
                self._base_retablie()
                if spec is None:
                    self._resident_between_turns()  # L76 : mort, sommeil
                    self.wake.wait(timeout=self.wait_timeout(
                        min(self.runner.poll, max(1.0, self.runner.lease_ttl / 3.0))))
                    self.wake.clear()
                    continue
                try:
                    reussi = self.run_turn(spec)
                    self._depot_push()  # L113 : commits du tour au serveur
                except db_mod.Unavailable as exc:
                    # Panne avant le lancement du harnais (ou après-tour au-delà
                    # du bail) : la consigne éventuellement consommée repartira
                    # en attente au retour de la base. Pas un échec de tour :
                    # la série d'échecs rapides (L48) n'est pas touchée.
                    self._consigne_a_restaurer = True
                    self._panne_de_base(exc, "the turn")
                    continue
                if reussi:
                    self.fast_failures = 0
                    self._host_reason, self._host_delay = "", 0.0  # L106
                    continue
                if self.host_yield is not None or self.runner.gate_holds():
                    continue  # L112 : retrait de l'hôte, pas un échec de tour
                if self.fast_failure:
                    self.fast_failures += 1
                    if self.fast_failures >= self.runner.max_fast_failures:
                        self.stop_after_failures()
                        break
                if self.host_yield is not None or self.runner.gate_holds():
                    continue  # L159f : retrait de l'hôte, pas un échec de tour
                if self.stopping.is_set() or self.runner.draining.is_set():
                    break  # arrêt demandé (L106 : batterie ; drainage) — bail rendu sans attendre
                # Un message non remis ou un harnais en échec ne doit pas
                # produire une boucle serrée : on laisse retomber, de plus en
                # plus longtemps tant que les échecs rapides se suivent (L48).
                # L'attente est honorée en entier mais découpée au pas du
                # battement (`_attendre_apres_echec`) : le worker renouvelle son
                # bail au lieu de dormir jusqu'à l'échéance, sinon `renew()`
                # refusait au réveil (B1 cx3) — #1270 : la reprise cadencée peut
                # dépasser le bail.
                self._attendre_apres_echec(self.failure_wait())
        finally:
            self._mark_power_stop()
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
        from .host_gate import gate_choice
        try:
            gate_choice(cfg, runner_id=cfg.runner)
        except ValueError as exc:
            raise SystemExit("agent-runner: %s" % exc) from exc
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
        #: L125 : fenêtre de regroupement du courrier, défaut de l'hôte
        self.mail_batch = max(0.0, cfg.mail_batch)
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
        #: L106 : hôte non prêt (harnais introuvable) — attente maximale entre
        #: deux essais ; garde d'alimentation (arrêt propre sur batterie
        #: critique, reprise au retour du secteur)
        self.host_retry_max = float(getattr(cfg, "host_retry_max", 300.0))
        self.power_stop_grace = max(0.0, float(getattr(cfg, "power_stop_grace", 120.0)))
        #: arrêt sur batterie en cours : plus aucune réclamation de bail
        self.power_hold = threading.Event()
        #: arrêt demandé par un signal (SIGTERM de systemd) : drainage — plus
        #: de nouveau tour, le tour en cours et son travail de fond finissent,
        #: au plus `drain_seconds` ; un second signal arrête tout de suite
        self.drain_seconds = max(0.0, float(getattr(cfg, "drain_seconds", 1800.0)))
        self.draining = threading.Event()
        self._drain_deadline = 0.0
        self._drain_since = 0.0
        self._drain_woken = False
        self._power_state = "unknown"
        self._power_thread: threading.Thread | None = None
        adapters.configure(cfg)
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
        #: épisode de pression en cours (une ligne de journal à son début, une
        #: à sa fin : durée, tours retardés), ou None
        self._pressure_episode: dict | None = None
        #: orchestrateurs (rôle `orchestrateur` au canon, relu à chaque `canon
        #: sync` ; `AMEESH_ALERT_ORCHESTRATORS`) : sous une pression NON
        #: critique, leurs tours partent quand même
        from . import sous_utilisation as su
        self._orchestrators_declared = frozenset(su.declared_orchestrators())
        self._orchestrators_roles: frozenset = frozenset()
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
        #: ménage (L73) : politique de la fiche Host (dernier `canon sync`),
        #: valeurs par défaut tant qu'aucun canon n'est lu
        from . import menage as menage_mod
        self._housekeeping = menage_mod.Policy()
        #: L162 : jamais deux passages de ménage à la fois (le périodique, et
        #: celui qu'une pression du tmpfs déclenche tout de suite)
        self._housekeeping_lock = threading.Lock()
        #: travail de fond des tours finis, en délai de grâce (suivi par le
        #: fil `fond` en mode service) ; pids des harnais lancés, que le
        #: moissonneur des orphelins adoptés ne touche jamais (lancement
        #: compris : `launch_lock`)
        from . import background as background_mod
        self.background = background_mod.Tracker(self, log=log_async)
        #: #453 : une suite de tests lancée hors `ameesh heavy` a le plafond
        #: de durée d'une commande lourde
        from . import suite_cap
        self.suite_cap = suite_cap.SuiteCap(self, log=log_async)
        self.launch_lock = threading.Lock()
        self.harness_pids: set[int] = set()
        self.turns = 0
        self.did_turn = False
        #: L790 : le paquet a changé sous ce processus (voir
        #: `canon_sync_once`) — plus aucune réclamation (`sweep`), les workers
        #: sortent entre deux tours et la boucle principale se remplace
        #: (`run`).
        self.restart_for_stale = False
        #: les workers au repos ont été réveillés une fois pour sortir (L790)
        self._stale_woken = False
        #: L109 : exécuteur médié — pas de base, le serveur du mesh par
        #: `/api/exec/v1` (`storage.remote`). Synchronisation du canon, relevé
        #: des soldes, échéance des délégations et déplacement entre hôtes
        #: passent au serveur ; l'hôte, l'owner, le bail et les limites
        #: viennent de `GET /host`. Porte d'hôte (L112) : celle de la
        #: configuration, sinon `AlwaysAvailable`, relayée au serveur
        #: (`_mediated_gate`).
        self.mediated = getattr(db, "driver", None) == "mediated"
        from .mediated_executor import gate as gate_mod
        self.gate = gate_mod.AlwaysAvailable()
        self.host_info = None
        if self.mediated:
            self._mediated_setup()
        #: L147 (#220 r2) : prochaine revue des lignes d'avant le démarrage
        #: (horloge monotone), tant que leur solde attend les conteneurs
        self.boot_review_at: float | None = None
        #: et où reprend la revue suivante (phase, dernier id)
        self._boot_review_after = BOOT_REVIEW_START
        #: #339 : prochaine revue des lignes dont le groupe est mort (horloge
        #: monotone) et le dernier id revu
        self.live_review_at: float = 0.0
        self._live_review_after = 0
        #: dernière erreur de traitement des délégations échues (L40), dite une fois
        self._delegation_error: str | None = None
        self._delegation_dry_seen: set = set()
        #: L85 : dernière rétention du journal d'événements (monotone), son erreur
        self._events_pruned_at: float | None = None
        self._events_prune_error: str | None = None
        #: L159f : porte d'hôte — None sans porte (hôte classique : rien ne
        #: change). Celle de la configuration dès maintenant ; celle d'un hôte
        #: prêteur (fiche Host avec `policy.lend`) au premier `canon sync`,
        #: avant lequel le mode service ne réclame rien (`_gate_settled`).
        self.host_gate = None
        self._host_gate_key: tuple | None = None
        self._host_gate_lock = threading.Lock()
        self._gate_running = False
        self._gate_settled = threading.Event()
        #: L159f : places de `caps.max_concurrent` tenues (agent → fd verrouillé)
        self._gate_slots: dict[str, int] = {}
        self._gate_slots_lock = threading.RLock()
        #: L159f : le worker du tour `--once` (au premier plan, hors de
        #: `workers`) : l'accusé de la porte le compte tant qu'il tient son bail
        self.gate_foreground = None
        if once or not cfg.canon:
            self._gate_settled.set()
        if self.mediated:
            from .mediated_executor import host_gate
            self.host_gate = host_gate.controller_from_config(self, log=log_async)
            self._mediated_gate()
            self._gate_settled.set()
        else:
            self._sync_host_gate(False, None)

    # -- exécuteur médié (L109) ---------------------------------------------
    def _mediated_gate(self) -> None:
        """L112 dans L109 : la porte de l'hôte est relayée au serveur
        (`PUT /host/availability`) au démarrage, à chaque changement et
        jusqu'à ce que le serveur l'ait reçue ; sans porte configurée,
        `AlwaysAvailable` (le serveur doit quand même apprendre que l'hôte
        est disponible : sans rapport, il le tient pour indisponible)."""
        from .mediated_executor import gate as gate_mod
        from .mediated_executor import host_gate
        sink = host_gate.TransportSink(self.db.transport, log=log_async)
        if self.host_gate is None:
            self.host_gate = host_gate.GateController(
                self, gate_mod.AlwaysAvailable(),
                drain_s=getattr(self.cfg, "host_gate_drain", 0) or gate_mod.DEFAULT_DRAIN_S,
                sink=sink, log=log_async)
        else:
            self.host_gate.sink = sink
        self.gate = self.host_gate.gate
        # jetons de session : un par bail (agent, owner, epoch), il vit tant
        # que le bail vit ; partagé par la session du harnais et le relais
        self._session_tokens: dict = {}
        self._session_tokens_lock = threading.Lock()
        from . import relay as relay_mod
        relay_mod.set_session_token_source(
            lambda agent, owner, epoch, _turn: self.session_token_for(agent, owner, epoch))

    #: un jeton de session vit au plus 12 h (L110) ; renouvelé avant
    SESSION_TOKEN_REUSE_S = 11 * 3600.0

    def session_token_for(self, agent: str, owner: str, epoch: int) -> str:
        """Le jeton de session du bail (agent, owner, epoch) : demandé une
        fois (`POST /session-token`), réutilisé pour la session du harnais
        et le relais de modèle. Lève `DbError` si le serveur le refuse."""
        from .mediated_executor import contract as exec_contract
        key = (agent, owner, int(epoch))
        now = time.monotonic()
        with self._session_tokens_lock:
            for stale in [k for k in self._session_tokens if k[0] == agent and k != key]:
                del self._session_tokens[stale]
            hit = self._session_tokens.get(key)
            if hit is not None and now - hit[0] < self.SESSION_TOKEN_REUSE_S:
                return hit[1]
        issued = self.db.session_token(exec_contract.Fence(agent, owner, int(epoch)))
        with self._session_tokens_lock:
            self._session_tokens[key] = (now, issued.token)
        return issued.token

    def _mediated_setup(self) -> None:
        """La fiche de l'hôte rendue par le serveur (`GET /host`) : hôte fixé
        à l'enrôlement, owner `exec:<id>:<hôte>:<pid>`, bail imposé, limites
        (à la place de `resources.host_limits` sur le canon local)."""
        from . import resources as resources_mod
        from .mediated_executor import contract as exec_contract
        info = self.db.host_info()
        self.host_info = info
        owner = exec_contract.owner_for(info.executor_id, info.host, os.getpid())
        self.cfg = dataclasses.replace(self.cfg, host=info.host, runner_id=owner)
        self.host = info.host
        self.runner_id = owner
        if info.lease_ttl_s:
            self.lease_ttl = float(info.lease_ttl_s)
        # contrat 1.1 : `limits` = {"max_agents": int|null, "resources":
        # {seuils de resources.THRESHOLD_KEYS}} (interfaces.HostInfo)
        limits = dict(info.limits or {})
        self._host_limits = resources_mod.thresholds(
            {"resources": dict(limits.get("resources") or {})})
        self._pressure["limits"] = self._host_limits
        maximum = limits.get("max_agents")
        self._host_max_agents = int(maximum) if maximum is not None else None
        self._host_limits_origin = {"max_agents": "serveur"}
        self.relocate = False


    # -- porte d'hôte (L159f) ------------------------------------------------
    def _sync_host_gate(self, lending: bool, offer: dict | None) -> None:
        """Met la porte d'hôte en accord avec la configuration et la fiche
        Host (prêteur ou non, `drain_seconds` de l'offre) ; ne lève jamais."""
        from . import host_gate as host_gate_mod
        try:
            kind, reason = host_gate_mod.gate_choice(self.cfg, lending, runner_id=self.runner_id)
            if getattr(self, "_host_gate_rejected", ""):
                kind, reason = "", "ack permission denied; guest gate excluded"
            choice = (kind, reason)
            if getattr(self, "_host_gate_choice_logged", None) != choice:
                self._host_gate_choice_logged = choice
                log_async("host gate selected: %s (%s)" % (kind or "none", reason))
            key = (kind, bool(lending), host_gate_mod.drain_seconds(self.cfg, offer)) \
                if kind else None
        except ValueError as exc:
            log_async("host gate: %s" % exc)
            return
        with self._host_gate_lock:
            if key == self._host_gate_key:
                return
            old, self.host_gate = self.host_gate, None
            if key is not None:
                self.host_gate = host_gate_mod.controller_from_config(
                    self, lending=lending, offer=offer, log=log_async)
            self._host_gate_key = key
            new, running = self.host_gate, self._gate_running
        if old is not None:
            old.stop()
        if new is None:
            if old is not None:
                log_async("host gate removed (host %s)" % self.host)
            return
        log_async("host gate %s: %s (host %s, withdrawal within %ds, unreadable gate = %s): %s"
                  % (key[0], new.gate.path, "lending" if lending else "classic",
                     int(new.drain_s), new.gate.fallback, new.describe()))
        if lending and str(getattr(self.cfg, "host_gate", "") or "") == "none":
            log_async("host gate: `host_gate: none` on a lending host")
        if running:
            new.start()

    def reject_host_gate(self, controller, error: str) -> None:
        """An ack permission denial identifies another user's gate for natives.

        Guests never call this: they keep the gate closed on the same error.
        Remember rejection through canon sync so it cannot recreate the gate.
        """
        from . import host_gate as host_gate_mod
        if host_gate_mod.is_guest(self.cfg, self.runner_id):
            return
        with self._host_gate_lock:
            if self.host_gate is not controller:
                return
            self._host_gate_rejected = "host gate: not my gate (ack permission denied: %s)" % error
            self.host_gate, self._host_gate_key = None, None
        # May run inside the controller tick: signal shutdown without joining
        # a thread that could itself be waiting for that tick to finish.
        controller._stop.set()
        controller.gate.close()
        log_async(self._host_gate_rejected + "; native claims open")
        self.wake_all.set()

    def _note_unleased(self, agent: dict, reason: str) -> None:
        if getattr(self, "mediated", False):
            return  # L107: this native diagnostic operation is REFUSED
        error = "lease_not_taken: " + reason
        if agent.get("last_error") != error:
            storage.of(self.db).agents.note_unleased(agent["name"], self.host, error)

    def _note_gate_wait(self, reason: str) -> None:
        if getattr(self, "mediated", False):
            return
        for agent in registry.claimable(self.db, self.host, self.agents_filter):
            self._note_unleased(agent, reason)

    def _start_host_gate(self) -> None:
        with self._host_gate_lock:
            self._gate_running = True
            gate = self.host_gate
        if gate is not None:
            gate.start()

    def gate_holds(self) -> bool:
        """L159f : porte d'hôte fermée (`draining` ou `stopped`) — aucune
        réclamation, aucun nouveau tour. Toujours faux sans porte."""
        gate = self.host_gate
        return gate is not None and gate.holds_turns()

    def gate_frozen_between(self, start: float, end: float) -> float:
        """L159f : secondes de `[start, end]` gelées par la garde de l'hôte."""
        gate = self.host_gate
        return gate.frozen_between(start, end) if gate is not None and not getattr(self, "mediated", False) else 0.0

    def _gate_slot(self, agent: dict) -> tuple[bool, int | None]:
        """L159f : `caps.max_concurrent` de la porte — agents à la fois sur
        l'HÔTE, tous exécuteurs confondus, éphémères compris (la garde compte
        des processus). Rend (admis, place) : la place (`host_gate.SlotPool`)
        est tenue tant que le worker vit, rendue par `_release_slot`.

        Avec une porte, tout worker tient une place, plafond ou non : un
        plafond posé plus tard compte aussi les agents déjà là (ceux d'avant
        la porte en prennent une ici). Dossier des places inutilisable sous
        un plafond : aucune admission (jamais un comptage local, qui
        admettrait un agent par exécuteur)."""
        gate = self.host_gate
        if gate is None:
            return True, None
        cap = gate.max_concurrent()
        if getattr(self, "mediated", False):
            with self.lock:
                running = sum(w.is_alive() for w in self.workers.values())
            return cap is None or running < cap, None
        pool = self._slot_pool(gate)
        try:
            with self._gate_slots_lock:
                self._hold_slots_of(pool)
                slot = pool.acquire(cap)
        except OSError as exc:
            self._slot_error(pool, exc, cap)
            return cap is None, None
        return slot is not None, slot

    @staticmethod
    def _slot_pool(gate):
        from . import host_gate as host_gate_mod
        return host_gate_mod.SlotPool(
            os.path.join(os.path.dirname(gate.gate.ack_path) or ".", "slots"))

    def _slot_error(self, pool, exc: OSError, cap) -> None:
        message = "host gate: slots %s unusable (%s)%s" % (
            pool.directory, exc, ": no claim while caps.max_concurrent is set"
            if cap is not None else "")
        if getattr(self, "_slot_error_logged", None) != message:
            self._slot_error_logged = message
            log_async(message)

    def _hold_slots_of(self, pool) -> None:
        """Une place pour chaque worker vivant qui n'en tient pas (ceux d'avant
        la porte), sous `_gate_slots_lock` ; lève OSError."""
        with self.lock:
            missing = [name for name, worker in self.workers.items()
                       if worker.is_alive() and name not in self._gate_slots]
        for name in missing:
            self._gate_slots[name] = pool.acquire(None)

    def gate_hold_slots(self) -> None:
        """L159f (r3) : appelé par le contrôleur de la porte à son démarrage
        puis à chaque tick — les workers d'avant la porte prennent leur place
        tout de suite, pas à la prochaine réclamation de CET exécuteur (un
        autre exécuteur admettrait au-delà du plafond entre-temps)."""
        gate = self.host_gate
        if gate is None or getattr(self, "mediated", False):
            return
        pool = self._slot_pool(gate)
        try:
            with self._gate_slots_lock:
                self._hold_slots_of(pool)
        except OSError as exc:
            self._slot_error(pool, exc, gate.max_concurrent())

    def _release_slot(self, name: str) -> None:
        from . import host_gate as host_gate_mod
        with self._gate_slots_lock:
            host_gate_mod.SlotPool.release(self._gate_slots.pop(name, None))

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
                self.db, actor="runner %s" % self.runner_id, dry_run=self.dry_run)
        except db_mod.DbError as exc:
            message = "expired delegations not processed (%s)" % exc
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
                log_async("delegation expired: work item #%s returned to %s (%s took no turn on it)%s"
                          % (row["work_item_id"], row["delegated_by"], row["delegate"],
                             " [dry-run]" if row.get("dry_run") else ""))
            elif row["outcome"] == "soldee":
                log_async("delegation settled: work item #%s (work by %s)%s"
                          % (row["work_item_id"], row["delegate"],
                             " [dry-run]" if row.get("dry_run") else ""))
        return done

    def prune_events_once(self, now: float | None = None) -> int | None:
        """L85 : rétention du journal `ameesh_events` (0033 §9), au plus une
        fois par heure et par exécuteur, bornée (`PRUNE_MAX_BATCHES` lots,
        `PRUNE_MAX_S` secondes ; le reste à l'heure suivante) ; rien en dry-run
        ni en exécuteur médié (le journal est au serveur du mesh). Une erreur
        (base, réglage) est journalisée une fois, jamais fatale à la passe.

        #928/F6 : l'élagage passe par sa PROPRE connexion. Sa transaction
        (jusqu'à `PRUNE_MAX_S`, avec verrous et délais) tiendrait sinon le
        verrou de transaction de la connexion partagée de l'exécuteur, sur
        lequel les renouvellements de bail attendent."""
        from . import events as events_mod

        now = time.monotonic() if now is None else now
        if self.dry_run or getattr(self, "mediated", False) or (
                self._events_pruned_at is not None
                and now - self._events_pruned_at < EVENTS_PRUNE_EVERY_S):
            return None
        self._events_pruned_at = now
        db, own = self.db, None
        if getattr(self, "cfg", None) is not None:
            try:
                own = db_mod.open_db(self.cfg)
            except db_mod.DbError as exc:
                message = "event log retention not applied (%s)" % exc
                if message != self._events_prune_error:
                    log_async(message)
                self._events_prune_error = message
                return None
            db = own
        try:
            gone = events_mod.prune(db, max_batches=events_mod.PRUNE_MAX_BATCHES,
                                    max_s=events_mod.PRUNE_MAX_S)
            # rien supprimé alors qu'il reste des événements expirés : la
            # rétention est bloquée (jamais un 0 silencieux)
            left = 0 if gone else events_mod.expired(db)
            if left:
                raise ValueError("%d expired event(s) left, none deleted" % left)
        except (db_mod.DbError, ValueError) as exc:
            message = "event log retention not applied (%s)" % exc
            if message != self._events_prune_error:
                log_async(message)
            self._events_prune_error = message
            return None
        finally:
            if own is not None:
                own.close()
        self._events_prune_error = None
        if gone:
            log_async("event log: %d event(s) past retention deleted" % gone)
        return gone

    def sweep(self) -> None:
        self.expire_delegations_once()
        for row in registry.reap(self.db, self.host):
            log("lease expired: %s (owner %s)" % (row["name"], row.get("lease_owner")))
        self.prune_events_once()   # L85 : après la moisson des baux, bornée
        finished = []
        with self.lock:
            for name, worker in list(self.workers.items()):
                if not worker.is_alive():
                    log("worker %s finished" % name)
                    del self.workers[name]
                    finished.append(name)
        for name in finished:
            # L159f : place de la porte rendue, hors de `self.lock` (le
            # contrôleur prend `_gate_slots_lock` puis `self.lock`)
            self._release_slot(name)
        if self.power_hold.is_set():
            return  # L106 : arrêt sur batterie — aucun bail repris avant le secteur
        if self.draining.is_set():
            return  # drainage : aucun nouvel agent, aucun nouveau tour
        if getattr(self, "restart_for_stale", False):
            return  # L790 : code périmé — plus de réclamation, on redémarre
        if not self._gate_settled.is_set():
            return  # L159f : hôte prêteur ou non, su au premier `canon sync`
        if not getattr(self, "mediated", False):
            from .host_gate import GateController
            gate = self.host_gate
            if isinstance(gate, GateController):
                gate.refresh()  # confirm ack permission before any new lease
        gate = self.host_gate
        if gate is not None and gate.holds_turns():
            # The mediated gate has no local file acknowledgement.
            error = "" if getattr(self, "mediated", False) else \
                getattr(gate.gate, "ack_error", "")
            self._note_gate_wait("host gate: %s%s" % (
                gate.describe(), "; ack not written: " + error if error else ""))
            return  # host gate closed: registry diagnosis without taking a lease
        for agent in registry.claimable(self.db, self.host, self.agents_filter):
            with self.lock:
                if not self.once and agent["name"] in self.workers:
                    continue
            admitted, slot = self._gate_slot(agent) if self.host_gate is not None \
                else (True, None)
            if not admitted:
                self._note_unleased(agent, "host gate: no concurrent slot available")
                continue  # L159f : `caps.max_concurrent` de la porte, par hôte
            if slot is not None:
                with self._gate_slots_lock:
                    self._gate_slots[agent["name"]] = slot
            if not self.once and not self.admits_new_worker(agent):
                self._release_slot(agent["name"])
                # L43 (0031) : hôte plein (max_agents physique) — l'agent reste
                # réclamable, il le sera quand une place se libère
                if getattr(self, "_cap_logged", None) != self._host_max_agents:
                    self._cap_logged = self._host_max_agents
                    log_async("host %s full: max_agents = %s (strictest Host record, "
                              "%s) — %s waits for a slot"
                              % (self.host, self._host_max_agents,
                                 (getattr(self, "_host_limits_origin", None) or {}).get(
                                     "max_agents", "?"), agent["name"]))
                self._note_unleased(agent, "host capacity: max_agents=%s" % self._host_max_agents)
                continue
            # une exception (base, worker) rend la place de la porte ; un
            # worker inscrit la rend à sa fin (`sweep`)
            registered = False
            try:
                if getattr(self, "_host_gate_rejected", ""):
                    self._note_unleased(agent, self._host_gate_rejected)
                lease = registry.claim(self.db, agent["name"], self.runner_id, self.lease_ttl)
                if not lease:
                    self._release_slot(agent["name"])
                    continue  # un autre exécuteur a gagné la course
                log("lease acquired: %s (epoch %s, host %s)"
                    % (agent["name"], lease["lease_epoch"], self.host))
                worker = AgentWorker(self, agent, lease)
                if worker._reprise_attach:
                    # L106 : la session interactive est morte sans rendre son bail
                    log("[%s] attached session dead (lease of %s expired): the runner "
                        "resumes session %s" % (agent["name"], agent.get("lease_owner"),
                                                agent.get("session_id") or "new"))
                    worker._fil_note(
                        "The interactive session (%s) ended without returning its "
                        "lease; runner %s resumes the same session."
                        % (agent.get("lease_owner"), self.runner_id),
                        meta={"action": "reprise-attach",
                              "session": agent.get("session_id") or None})
                if self.once:
                    gate = self.host_gate
                    if gate is not None:
                        # L159f (r3) : la part d'accusé de cet exécuteur dit
                        # le bail tenu AVANT le tour, puis la porte est relue
                        # (le fichier, r4) : un autre exécuteur ne dit jamais
                        # l'hôte vidé pendant ce tour, et une fermeture
                        # d'entre-temps l'empêche
                        self.gate_foreground = worker
                    try:
                        if gate is not None and not gate.refresh().may_claim:
                            log_async("host gate closed meanwhile (%s): no turn for %s"
                                      % (gate.describe(), agent["name"]))
                            ran = False
                        else:
                            ran = worker.process_once(self.wait)
                    finally:
                        try:
                            released = worker.release_lease()
                            self._release_slot(agent["name"])
                            if not released and gate is not None:
                                # L159f4 : la part dit le bail tenu tant
                                # qu'il l'est (nouvel essai, ou échéance)
                                self._await_release(worker, gate)
                        finally:
                            # r4 : compté jusqu'à la libération faite, sinon
                            # le contrôleur dirait l'hôte vidé, bail tenu
                            self.gate_foreground = None
                            if gate is not None:
                                gate.tick()
                    self.did_turn = self.did_turn or ran
                else:
                    with self.lock:
                        self.workers[agent["name"]] = worker
                    registered = True
                    worker.start()
            except BaseException:
                if not registered:
                    self._release_slot(agent["name"])
                raise

    #: `--once` : intervalle des nouveaux essais d'un bail non rendu
    RELEASE_RETRY_S = 5.0

    def _await_release(self, worker, gate) -> None:
        """`--once`, la base n'a pas pris le retour du bail : le worker reste
        compté (`gate_foreground`) et la part le dit tenu, jusqu'à un retour
        réussi ou l'échéance du bail (il n'est plus renouvelé) ; jamais
        « vidé » bail tenu (L159f4). Un arrêt de l'exécuteur coupe l'attente :
        sa part disparaît avec lui."""
        # r1 : l'échéance lue en base n'est pas comparée à l'horloge murale
        # de cet hôte (en avance, elle dirait la part libre bail tenu).
        # r2-r4 : un renouvellement encore en cours (fil du battement, appel
        # détaché) peut toujours aboutir en base : tant qu'il court, le bail
        # est tenu, et sa fin (réponse ou erreur) repousse la borne ; elle
        # est donc relue à chaque tour. Les nouveaux essais du retour ne
        # l'attendent jamais : un retour réussi met fin à tout.
        heartbeat = worker.heartbeat_thread

        def renewing() -> bool:
            pending = [heartbeat] if heartbeat is not None else []
            return any(t.is_alive() for t in pending + list(worker.renewals))

        log_async("[%s] lease not returned (base): counted as held until it is, "
                  "or until it expires (%ds)"
                  % (worker.name, max(0, worker.lease_held_until - time.monotonic())))
        while renewing() or time.monotonic() < worker.lease_held_until:
            gate.tick()
            left = worker.lease_held_until - time.monotonic()
            wait = self.RELEASE_RETRY_S if renewing() else min(self.RELEASE_RETRY_S,
                                                                 max(0.0, left))
            if self.stop.wait(wait):
                return
            if worker.release_lease():
                return

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
            log_async("budget cap changed in the database: reloading")
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
                        log_async("restart requested for %s: stopping the turn" % target)
                self.wake_all.set()
                return
            prioritaire = False
            if worker is not None and payload.get("id"):
                row = mail.get(self.db, int(payload["id"]))
                if row and mail.is_urgent(row):
                    prioritaire = worker.interrupt_allowed(row)
                    if not prioritaire:
                        log_async("urgent from %s without interruption: sender not "
                                  "allowed to interrupt" % (row.get("sender") or "?"))
            if worker and prioritaire:
                worker.request_preempt()  # le signal d'abord
                log_async("priority message for %s: interrupting the turn" % target)
            elif worker:
                worker.wake.set()
                log_async("message for %s: waking the turn" % target)
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
                    log_async("cannot listen (%s): retrying in %ds"
                              % (db_mod.explain(exc), int(attente)))
                    self.stop.wait(attente)
                    continue
                log_async("LISTEN/NOTIFY unavailable with this driver: polling every %ss"
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
                            log_async("listening restored after %ds (%d failure(s))"
                                      % (int(bilan[1]), bilan[0]))
                        self.wake_all.set()  # battement de sondage
                        continue
                    if item.get("event") == "down":
                        attente = reprise.echec()
                        log_async("listening interrupted (%s): reconnecting in %ds"
                                  % (item.get("error") or "?", int(attente)))
                        break
                    try:
                        self.dispatch(item)
                    except db_mod.DbError as exc:
                        # réveil sans détail : chaque worker relira lui-même
                        log_async("wake-up without detail (%s)" % db_mod.explain(exc))
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
    def request_stop(self, signum: int) -> None:
        """Un signal d'arrêt (SIGTERM de systemd, SIGINT). Appelé depuis le
        gestionnaire de signal : aucun verrou, des drapeaux et la file du
        journal seulement.

        Premier signal : DRAINAGE — plus de nouveau tour ; le tour en cours
        et son travail de fond finissent, au plus `drain_seconds`, puis
        l'exécuteur rend ses baux et sort (`_drained`). Un redémarrage
        ordinaire (`systemctl restart`) ne tue donc plus le tour en cours.
        Second signal, borne nulle, ou passage unique : arrêt immédiat, comme
        avant (tour arrêté, consigne remise en attente)."""
        if self.once or self.drain_seconds <= 0 or self.draining.is_set():
            immediat = self.draining.is_set()
            self.stop.set()
            self.wake_all.set()
            log_async("signal %d received: %s" % (
                signum, "second signal, immediate stop (current turn and background "
                        "work stopped)" if immediat else "clean shutdown"))
            return
        self._drain_since = time.monotonic()
        self._drain_deadline = self._drain_since + self.drain_seconds
        self.draining.set()
        self.wake_all.set()
        log_async("signal %d received: draining — no new turn; the current turn and "
                  "its background work finish (at most %ds), then stop; a second "
                  "signal stops immediately" % (signum, int(self.drain_seconds)))

    def _stale_restart_ready(self) -> bool:
        """L790 : le redémarrage attend la fin du dernier tour et du fond.

        Dès que `canon_sync_once` a constaté un code périmé, `sweep` ne
        réclame plus : ici on réveille les workers au repos pour qu'ils
        sortent entre deux tours (même geste que le drainage, sans interrompre
        un tour en cours), puis on ne rend vrai que lorsque plus rien n'écrit —
        même critère que le drainage, sans sa borne."""
        if not getattr(self, "restart_for_stale", False):
            return False
        with self.lock:
            workers = list(self.workers.items())
        if not getattr(self, "_stale_woken", False):
            # hors du chemin de `shutdown` : les workers au repos attendent
            # leur propre événement, pas `wake_all` (celui de la boucle)
            self._stale_woken = True
            for _nom, worker in workers:
                worker.wake.set()
        vivants = [name for name, worker in workers if worker.is_alive()]
        return not vivants and not self.background.running_count()

    def _drained(self) -> bool:
        """Drainage en cours : vrai quand l'exécuteur peut s'arrêter — plus
        aucun worker vivant (chacun sort après son tour) ni travail de fond en
        délai de grâce, ou borne atteinte (l'arrêt arrête alors ce qui reste)."""
        with self.lock:
            workers = list(self.workers.items())
        if not self._drain_woken:
            # hors du gestionnaire de signal : les workers au repos sortent
            self._drain_woken = True
            for _nom, worker in workers:
                worker.wake.set()
        en_tour = sorted(nom for nom, worker in workers if worker.is_alive())
        fond = self.background.running_count()
        duree = int(time.monotonic() - self._drain_since)
        if not en_tour and not fond:
            log_async("draining finished in %ds: no turn or background work left, "
                      "stopping" % duree)
            return True
        if time.monotonic() >= self._drain_deadline:
            log_async("draining: %ds limit reached — stopping what remains (%s)"
                      % (int(self.drain_seconds), ", ".join(
                          (["turn of %s" % ", ".join(en_tour)] if en_tour else [])
                          + (["background work of %d turn(s)" % fond] if fond else []))))
            return True
        return False

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
            worker.stop_group_now("runner shutdown", grace=3.0)
        deadline = time.monotonic() + 8.0
        for worker in workers:
            worker.join(timeout=max(0.1, deadline - time.monotonic()))
            # Le tour a pu publier son harnais après le terminate initial
            # (course de publication) : rien ne doit survivre au relâchement
            # du bail (sonde codex3 B5a-P).
            worker.stop_group_now("runner shutdown (hard)", grace=0, hard=True)
            worker.release_lease()
        # Travail de fond encore en délai de grâce : le nettoyage habituel,
        # tout de suite (rien ne survit à l'exécuteur qui le suit).
        self.background.stop_all("runner shutdown")
        gate = getattr(self, "host_gate", None)
        if gate is not None:
            try:
                gate.tick()  # L159f : dernier acquittement, plus aucun bail
            finally:
                gate.stop()
        # L'écouteur (fil démon) ne doit plus écrire quand l'interpréteur se
        # finalise : sa connexion est fermée ci-dessus, on l'attend, borné.
        if self.listener_thread is not None:
            self.listener_thread.join(timeout=2.0)
        self.stopped_threads = list(workers)
        if getattr(self, "host_gate", None) is not None:
            self.host_gate.tick()  # dernier acquittement : plus aucun bail
            self.host_gate.stop()
        # par la file : aucune écriture synchrone sur le chemin d'arrêt avant
        # le repli de `_sortie_sure` (relecture codex2 de 669a6fe, B1)
        log_async("shutdown: %d lease(s) returned, %d turn(s)" % (len(workers), self.turns))

    # -- canon sync (spec §4.4) --------------------------------------------
    def canon_sync_once(self) -> bool:
        """Un `canon sync` de l'hôte ; ne lève jamais, ne tue jamais un tour.

        Le canon est relu à chaque passe (`from_config`) et l'état est écrit par
        `canon_sync.sync` ; un canon illisible enregistre l'état `unreadable`,
        ce qui ferme la réclamation (L2) sans toucher aux baux ni aux tours en
        cours. Un échec — canon, git, base — est journalisé et rend `False`.
        L780 : un exécuteur plus vieux que le paquet installé saute la passe
        sans rien écrire (canon_state, responsables et placements restent au
        dernier passage valide) et demande un redémarrage entre deux tours.
        """
        if getattr(self, "mediated", False):
            return True  # L109 : le canon est synchronisé par le serveur
        entries = canon_mod.configured(self.cfg)
        # L159f : aucun canon, aucune fiche Host prêteuse : la porte est fixée
        self._canon_complete = not entries
        if not entries:
            return True
        # L780 : livraison sous ce processus (paquet mis à jour, arbre source
        # tiré) — l'ancien code, validateur de descripteurs compris, ne juge
        # plus le canon. Rien n'est écrit ni fermé : l'état reste au dernier
        # passage valide, la porte d'hôte reste fermée, la passe est signalée
        # une fois, et le remède est un redémarrage entre deux tours.
        stale = harnesses.stale_reason()
        if stale:
            self._canon_complete = False
            # L790 : le remède est un redémarrage ; on le demande ici, sans
            # attendre un humain. `sweep` cesse de réclamer, la boucle
            # principale se remplace dès que le dernier tour (et le fond) est
            # fini — les tours en cours ne sont pas interrompus.
            self.restart_for_stale = True
            wake = getattr(self, "wake_all", None)
            if wake is not None:
                wake.set()
            if not getattr(self, "_stale_reported", False):
                self._stale_reported = True
                log_async("canon sync skipped: outdated runner (%s) — restarting "
                          "between turns" % stale)
                note = getattr(self, "_fil_note", None)
                if note is not None:
                    note("outdated runner: %s; canon_state unchanged and no new claim "
                         "until an automatic restart between turns" % stale,
                         meta={"kind": "runner-outdated"})
            return False
        db = None
        ok = True
        try:
            from .scoped_db import ScopedDb
            db = ScopedDb(self.cfg)
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
                            "configuration: duplicate canon id \"%s\" (%s): "
                            "this canon is not synced" % (ident, entry.path))
                    loaded.append(canon)
                except Exception as exc:  # jamais fatal : le canon ferme, il ne tue pas
                    ok = False
                    log_async("canon sync failed (%s): %s" % (
                        entry.path, " ".join(str(exc).split())[:200]))
            if len(entries) > 1 and loaded and all(hasattr(c, "is_default") for c in loaded):
                try:
                    canon_sync.adopt_default(db, self.host, loaded)
                except Exception as exc:
                    ok = False
                    log_async("default canon: %s — no canon synced"
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
                        log_async("canon sync%s: %s (%s)" % (
                            " %s" % canon.id if len(loaded) > 1 else "", status,
                            getattr(report, "diagnostic", None) or "no detail"))
                except Exception as exc:  # jamais fatal : le canon ferme, il ne tue pas
                    ok = False
                    log_async("canon sync failed%s: %s" % (
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
                    log_async("removed canons: %s" % " ".join(str(exc).split())[:200])
            # L31 : seuils de pression de l'hôte. `canon_sync_once` est aussi
            # exercé sur un objet factice (tests d'authentificateurs).
            # L159f : la porte d'hôte n'est fixée que sur TOUS les canons
            # configurés (un hôte prêteur l'est par sa fiche Host, `policy.lend`)
            self._canon_complete = bool(entries) and len(synced) == len(entries)
            refresh = getattr(self, "refresh_host_limits", None)
            if refresh is not None and synced:
                refresh(*synced, complete=self._canon_complete)
            orchestres = getattr(self, "refresh_orchestrators", None)
            if orchestres is not None and synced:
                orchestres(*synced)
            return ok
        except Exception as exc:  # jamais fatal : le canon ferme, il ne tue pas
            log_async("canon sync failed: %s" % " ".join(str(exc).split())[:200])
            return False
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def _settle_gate_after_sync(self) -> bool:
        """L159f : la porte d'hôte n'est fixée (réclamations permises) qu'après
        un sync qui a lu tous les canons configurés : un canon momentanément
        illisible ne fait pas d'un hôte prêteur un hôte classique, ouvert."""
        if getattr(self, "_canon_complete", False):
            self._gate_settled.set()
            return True
        if not self._gate_settled.is_set() and not getattr(self, "_settle_logged", False):
            self._settle_logged = True
            log_async("host gate: canon not read in full, no claim before a "
                      "complete sync (retry every %ds)"
                      % GATE_SETTLE_RETRY_S)
        return False

    def _canon_sync_loop(self) -> None:
        while not self.stop.is_set():
            self.canon_sync_once()
            settled = self._settle_gate_after_sync()
            # avant la première porte fixée, on réessaie vite
            wait = (self.canon_sync_interval if settled or self.canon_sync_interval <= 0
                    else min(self.canon_sync_interval, GATE_SETTLE_RETRY_S))
            if wait <= 0:
                # pas de sync périodique : seulement jusqu'à la première porte fixée
                if settled:
                    return
                wait = GATE_SETTLE_RETRY_S
            if self.stop.wait(wait):
                return

    def start_canon_sync(self) -> None:
        """Lance le sync du canon : immédiat, puis périodique (spec §4.4).

        Sans canon configuré, il n'y a rien à faire. Avec un intervalle nul,
        le sync n'est refait que jusqu'à la première porte d'hôte fixée (L159f).
        En mode `--once`, le sync est fait en ligne (le fil n'aurait pas le
        temps de tourner) ; sinon un fil dédié s'en charge.
        """
        if getattr(self, "mediated", False):
            self._gate_settled.set()
            return  # canon sync is server-side for mediated devices
        if not self.cfg.canon:
            self._gate_settled.set()
            return
        if self.once:
            self.canon_sync_once()
            return
        if self.canon_thread is not None:
            return
        if self.canon_sync_interval <= 0:
            self.canon_sync_once()  # seulement au démarrage…
            if self._settle_gate_after_sync():
                return
            # … ou jusqu'à la première porte d'hôte fixée (fil ci-dessous)
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
            from .scoped_db import ScopedDb
            db = ScopedDb(self.cfg)
            for source, compte in paires:
                try:
                    releves += len(balance_mod.record(db, source, compte,
                                                      min_interval_s=min_interval_s))
                except (balance_mod.BalanceError, db_mod.DbError) as exc:
                    log_async("balance %s%s: %s" % (
                        source.provider, "/%s" % compte if compte else "",
                        " ".join(str(exc).split())[:160]))
        except Exception as exc:  # jamais fatal
            log_async("balance: reading failed (%s)" % type(exc).__name__)
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
            from .scoped_db import ScopedDb
            db = ScopedDb(self.cfg)
            gate = self.host_gate
            reading = resources_mod.collect(self.cfg, db, policy=self.housekeeping_policy(),
                                            gate=gate.snapshot() if gate is not None else None)
            Runner.host_lifecycle_once(self, db)
            return reading
        except Exception as exc:  # jamais fatal pour l'exécuteur
            log_async("resources: reading failed (%s)" % type(exc).__name__)
            return None
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def host_lifecycle_once(self, db) -> None:
        """Drive durable requests with the enrolled helper, or record its named gap."""
        from . import handoffs, host_lifecycle
        try:
            api = storage.of(db).host_lifecycle
            api.detect_losses(actor=self.runner_id,
                              older_than=max(180, int(self.cfg.resource_interval * 3)),
                              critical=host_lifecycle.critical_names(self.cfg, db))
            api.expire_returns()
            for control in api.listing():
                if control["mode"] in ("draining", "returning", "lost"):
                    host_lifecycle.advance_host(
                        db, control["host"], limits_for=self.handoff_limits_for,
                        adapter=getattr(self, "_lifecycle_adapter", None),
                        requirements_for=getattr(self, "_lifecycle_requirements", None))
        except (db_mod.DbError, handoffs.HandoffError, ValueError):
            log_async("host lifecycle: request remains pending; helper or prerequisites unavailable")

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

    #: L120: how often the version thread wakes; the probe itself runs at
    #: most every `harness_versions.PROBE_INTERVAL_S`
    VERSIONS_WAKE_S = 600.0

    def start_version_poll(self) -> threading.Thread | None:
        """L120: harness versions read at startup, then every 6 h — on their
        own thread, whatever `AMEESH_RESOURCE_INTERVAL` says (0 disables the
        resource poll, not this). Not in `--once` mode."""
        if self.once or getattr(self, "mediated", False):
            return None

        def boucle() -> None:
            while not self.stop.is_set():
                self.harness_versions_once()
                if self.stop.wait(self.VERSIONS_WAKE_S):
                    return

        thread = threading.Thread(target=boucle, daemon=True, name="versions")
        thread.start()
        return thread

    # -- ménage (L73) -----------------------------------------------------
    def housekeeping_policy(self):
        """Politique de ménage de l'hôte (L73) ; défauts sans canon."""
        from . import menage as menage_mod
        return getattr(self, "_housekeeping", None) or menage_mod.Policy()

    def agents_in_turn(self) -> set:
        """Agents en tour sur cet exécuteur — et ceux dont le travail de fond
        d'un tour fini tourne encore (délai de grâce) : le ménage les traite
        de même (dossier temporaire, caches, worktrees)."""
        with self.lock:
            en_tour = {name for name, worker in self.workers.items()
                       if worker.is_alive() and getattr(worker, "proc", None) is not None}
        return en_tour | self.background.agents()

    def housekeeping_once(self) -> dict | None:
        """Un passage de ménage (L73) ; ne lève jamais.

        Les caches partagés ne sont évincés que si aucun tour de cet hôte ne
        tourne (d'après la base : les autres exécuteurs de l'hôte comptent).
        L162 : un passage déjà en cours rend None (jamais deux à la fois)."""
        lock = getattr(self, "_housekeeping_lock", None)
        if lock is None:
            lock = self._housekeeping_lock = threading.Lock()
        if not lock.acquire(blocking=False):
            return None
        try:
            return self._housekeeping_pass()
        finally:
            lock.release()

    def tmpfs_pressure_pass(self) -> bool:
        """L162 : une pression qui vient du tmpfs déclenche TOUT DE SUITE un
        passage de ménage (entrées de /tmp abandonnées par un tour), sans
        attendre le passage périodique ; le journal dit ensuite les plus
        grosses entrées de /tmp et l'action attendue. Rien sans passage
        périodique (`AMEESH_HOUSEKEEPING_INTERVAL=0`, `--once`). Rend vrai si
        le passage est lancé."""
        interval = float(getattr(self.cfg, "housekeeping_interval", 0) or 0)
        if interval <= 0 or self.once or getattr(self, "mediated", False):
            return False  # mediated executor: no local housekeeping (contract 1.1)

        def passage() -> None:
            from . import menage as menage_mod
            report = self.housekeeping_once()
            if not report:
                return
            mesure = report.get("measure") or {}
            supprimes = [e for e in report.get("entries") or ()
                         if e.get("kind") == "orphan" and e.get("action") == "deleted"]
            log_async("tmpfs pressure: immediate cleanup, %d abandoned /tmp entry(ies) "
                      "deleted%s; %s" % (
                          len(supprimes),
                          " (%s)" % ", ".join(e["path"] for e in supprimes[:5])
                          if supprimes else "",
                          menage_mod.tmpfs_hint(mesure.get("system_tmp_top") or [],
                                                self.housekeeping_policy())))

        threading.Thread(target=passage, daemon=True, name="menage-tmpfs").start()
        return True

    def _housekeeping_pass(self) -> dict | None:
        from . import menage as menage_mod
        from . import build_artifacts
        from . import resources as resources_mod
        db = None
        try:
            from .scoped_db import ScopedDb
            db = ScopedDb(self.cfg)
            en_tour = self.agents_in_turn()
            try:
                busy = bool(en_tour) or storage.of(db).hosts.turns_in_progress(self.host) > 0
            except Exception:
                busy = True
            report = menage_mod.run_pass(
                self.cfg, db, self.housekeeping_policy(), host=self.host,
                actor="runner:%s" % self.runner_id, busy=busy, agents_in_turn=en_tour,
                runtime=self.container_runtime(),
                # L230: the worktrees are walked at most every few hours
                artifact_scan=build_artifacts.due(time.time()))
            self.expire_threads_once(db)
            self.sweep_dead_letters_once(db)
            faits = [e for e in report["entries"] if e["kind"] != "mesure"]
            if faits:
                log_async("cleanup: %s" % "; ".join(
                    "%s %s %s%s" % (e["kind"], e["action"], e.get("path") or "",
                                    " (%s)" % resources_mod.fmt_value("bytes", e["bytes"])
                                    if e.get("bytes") else "")
                    for e in faits[:10]))
            return report
        except Exception as exc:  # jamais fatal pour l'exécuteur
            log_async("cleanup: pass failed (%s: %s)" % (type(exc).__name__, exc))
            return None
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def lot_sessions_purge(self, db) -> list[dict]:
        """#408 : les sessions mises de côté (et les réveils) des lots
        fusionnés, livrés, fermés ou réassignés sont oubliées ; les
        transcripts du harnais restent au ménage. Ne lève jamais."""
        try:
            purges = storage.of(db).lot_sessions.purge()
        except db_mod.DbError as exc:
            log_async("lot sessions not purged (%s)" % exc)
            return []
        for row in purges:
            if row.get("session_id"):
                log_async("[%s] session %s of lot #%s forgotten (lot finished or reassigned)"
                          % (row["agent"], row["session_id"], row["work_item_id"]))
        return purges

    def expire_threads_once(self, db, now: float | None = None,
                            clock: float | None = None) -> list | None:
        """L361: thread retention (0033 §9), at most once a day, from the
        housekeeping pass: entries older than `thread_retention_days` leave
        this host's thread files. Never raises.

        L361b : a mediated host has no transaction; the executor connection
        (`self.db`) carries the server operation `threads.recount`, which
        locks, counts again and expires in one shot. When it has to open one
        itself, it closes it; a connection it was given is left open."""
        now = time.time() if now is None else now
        # the day counted on the monotonic clock (r8), the wall clock kept
        # for the retention thresholds
        tick = time.monotonic() if clock is None else clock
        due = getattr(self, "_threads_expire_next", None)
        if due is not None and tick < due:
            return None
        self._threads_expire_next = tick + 86400.0
        created = None
        if db is None:
            db = getattr(self, "db", None)
        if db is None and not self.dry_run and fil.expiry_unavailable(self.cfg, None):
            # L361b: a mediated host has no local housekeeping; its executor
            # connection carries `threads.recount`. Without it, nothing expires.
            try:
                db = created = db_mod.connect(self.cfg)
            except db_mod.DbError as exc:
                log_async("thread expiry skipped: index unavailable (%s)"
                          % " ".join(str(exc).split())[:200])
                return None
        try:
            try:
                reports = fil.expire(self.cfg, db, now=now, dry_run=self.dry_run)
            except fil.ExpiryUnavailable as exc:  # said once per pass
                log_async("thread expiry skipped: %s" % exc)
                return None
            except Exception as exc:  # never fatal for the runner
                log_async("thread expiry failed (%s: %s)" % (type(exc).__name__, exc))
                return None
            for report in reports:
                if report.get("error") or report.get("held"):
                    log_async("thread expiry %s: %s" % (report["thread"],
                                                         report.get("error") or report["held"]))
                if report["dropped"]:
                    log_async("thread %s: %d entr%s before %s %s, %d kept" % (
                        report["thread"], report["dropped"],
                        "y" if report["dropped"] == 1 else "ies", report["before"],
                        "would expire [dry-run]" if self.dry_run else "expired", report["kept"]))
            return reports
        finally:
            if created is not None:      # only the connection opened here
                created.close()

    def sweep_dead_letters_once(self, db, now: float | None = None,
                                clock: float | None = None) -> dict | None:
        """L976 : le courrier en souffrance (destinataire arrêté ou inconnu)
        est re-livré au successeur déclaré, sinon expiré après
        `mail_dead_letter_days`, au plus une fois par heure depuis le ménage
        (le seuil d'alerte est de 15 min) ; l'alerte `mail_undeliverable` se
        vide donc sans geste humain. Ne lève jamais."""
        now = time.time() if now is None else now
        # l'intervalle se compte sur l'horloge monotone (r8), le seuil sur l'horloge murale
        tick = time.monotonic() if clock is None else clock
        due = getattr(self, "_dead_letters_next", None)
        if due is not None and tick < due:
            return None
        self._dead_letters_next = tick + DEAD_LETTER_SWEEP_EVERY_S
        if getattr(self, "mediated", False) or db is None:
            return None               # exécuteur médié : pas de base, pas de balayage
        try:
            from . import undeliverable
            report = undeliverable.sweep(self.cfg, db, now=now, dry_run=self.dry_run)
        except Exception as exc:      # jamais fatal pour l'exécuteur
            log_async("dead-letter sweep failed (%s: %s)" % (type(exc).__name__, exc))
            return None
        suffix = " [dry-run]" if self.dry_run else ""
        for action in report["forwarded"]:
            log_async("undeliverable mail: %d message(s) for %s re-delivered to %s%s"
                      % (action["count"], action["old"], action["new"], suffix))
        for action in report["expired"]:
            log_async("undeliverable mail: %d message(s) for %s expired after %d day(s)%s"
                      % (action["count"], action["recipient"], report["days"], suffix))
        for action in report["refused"]:
            log_async("undeliverable mail: %s not re-delivered to %s (%s)"
                      % (action["old"], action["new"], action["reason"].splitlines()[0]))
        return report

    def branch_sweep_once(self) -> dict | None:
        """L118 : un relevé des branches des lots ouverts dont l'assigné est sur
        cet hôte (et, avec `--agents`, parmi eux) : une branche fusionnée dans
        sa cible, avec ou sans PR, fait passer son lot livré
        (`plan_git.sync_branches`). Sa propre connexion ; ne lève jamais."""
        from . import plan_git
        db = None
        try:
            from .scoped_db import ScopedDb
            db = ScopedDb(self.cfg)
            report = plan_git.sync_branches(
                db, host=self.host, agents=set(self.agents_filter) if self.agents_filter
                else None, dry_run=self.dry_run, actor="runner %s" % self.runner_id)
            for entry in report["results"]:
                if entry["result"] in ("merged", "would-merge"):
                    log_async("work item #%s delivered: %s%s" % (
                        entry["work_item"], entry["detail"],
                        " [dry-run]" if entry["result"] == "would-merge" else ""))
            if not self.dry_run:
                self.measure_once(db)
                self.lot_sessions_purge(db)
            self.radar_once(db)
            self.incident_once(db)
            return report
        except Exception as exc:  # jamais fatal pour l'exécuteur
            message = "branch sweep failed (%s: %s)" % (type(exc).__name__, exc)
            if message != getattr(self, "_branch_sweep_error", None):
                log_async(message)
            self._branch_sweep_error = message
            return None
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def measure_once(self, db) -> None:
        """L157c: measures the lots delivered since the last sweep (phases,
        merged diff) with the repositories of this host. Never raises."""
        from . import lot_measures
        try:
            errors: list = []
            for row in lot_measures.measure_pending(
                    db, host=self.host,
                    agents=set(self.agents_filter) if self.agents_filter else None,
                    errors=errors):
                log_async("work item #%s measured: %s" % (
                    row["work_item_id"], lot_measures.describe_phases(row)))
            for lot, exc in errors:
                log_async("work item #%s not measured (%s: %s)" % (lot, type(exc).__name__, exc))
        except Exception as exc:  # never fatal for the runner
            log_async("work item measures failed (%s: %s)" % (type(exc).__name__, exc))

    def radar_once(self, db) -> None:
        """L160b : un passage du radar de conflits, après le relevé des
        branches, sur l'hôte qui déclare `radar.projects` : journal, et
        courrier aux assignés sur conflit nouveau (L160c). Ne lève jamais."""
        from . import radar
        try:
            if getattr(self, "_radar_cache", None) is None:
                self._radar_cache = radar.Cache()
            owner = getattr(self, "runner_id", None) or None
            for line in radar.run_pass(self.cfg, db, cache=self._radar_cache, owner=owner):
                if line.get("mail"):
                    log_async("radar %s: %d mail(s), including %d wake-up(s)" % (
                        line["project"], len(line["mail"]),
                        sum(1 for m in line["mail"] if m["level"] == "blocking")))
                for one in line.get("land") or ():
                    if one.get("result") != "busy":
                        log_async("fusion %s #%s : %s%s" % (
                            line["project"], one.get("lot") or "—", one["result"],
                            " (%s)" % one["error"] if one.get("error") else ""))
                if line["changes"] or line.get("error"):
                    log_async("radar %s: %s" % (line["project"], line.get("error") or (
                        "%d change(s), %d conflicting pair(s) (%.1f s)" % (
                            len(line["changes"]), line["summary"]["conflict"],
                            line["duration_s"]))))
        except Exception as exc:  # jamais fatal pour l'exécuteur
            message = "radar failed (%s: %s)" % (type(exc).__name__, exc)
            if message != getattr(self, "_radar_error", None):
                log_async(message)
            self._radar_error = message

    def incident_once(self, db) -> None:
        """#1681 : ouvre les incidents des exécutions terminales rouges de la
        branche d'intégration (develop Nexlink, main ameesh), à la source
        courante du veilleur (`ci.latest_concluded`) et à la même tête. Une
        exécution déjà portée par son incident n'est pas rouverte ; seules les
        ouvertures et les relevés illisibles sont journalisés. Ne lève jamais.

        #1467 : les passages du train (`self.train_passages`) apportent les
        faits exacts du paquet (atterrissage par membre, rapport, carte
        qualifiée par dépôt et tête) par `train_incidents.consume` ; sans
        module (pas encore atterri), sans passage, ou si ce helper échoue,
        `incident.consume(db)` fait foi — un incident n'est jamais perdu."""
        from . import incident
        try:
            lines = None
            try:
                from . import train_incidents
            except Exception:  # module du train #1467 pas encore atterri
                train_incidents = None
            if train_incidents is not None:
                try:
                    lines = train_incidents.consume(
                        db, getattr(self, "train_passages", ()), consumer=incident)
                except Exception as exc:  # le relevé direct reste la garde
                    message = "train facts unreadable (%s: %s)" % (type(exc).__name__, exc)
                    if message != getattr(self, "_incident_train_error", None):
                        log_async("CI incident: %s; direct reading" % message)
                    self._incident_train_error = message
            if lines is None:
                lines = incident.consume(db)
            for line in lines:
                where = "%s %s run %s" % (line.get("repo"), line.get("branch"),
                                          line.get("run_id"))
                if line.get("created"):
                    log_async("CI incident %s: lot #%s ouvert%s (%s)" % (
                        where, line.get("id"),
                        " assigné à %s" % line["author"] if line.get("assigned") else "",
                        line.get("status") or "?"))
                elif "illisible" in str(line.get("why") or ""):
                    log_async("CI incident %s: %s" % (where, line["why"]))
        except Exception as exc:  # jamais fatal pour l'exécuteur
            log_async("CI incident failed (%s: %s)" % (type(exc).__name__, exc))

    def start_branch_sweep_poll(self) -> None:
        """Relevé périodique des branches (`AMEESH_BRANCH_SWEEP_INTERVAL`,
        défaut 300 s ; 0 = aucun). L'exécuteur, et non `notify` : il tourne sur
        chaque hôte, là où sont les dossiers de travail des assignés."""
        interval = max(0.0, float(getattr(self.cfg, "branch_sweep_interval", 0) or 0))
        if interval <= 0 or self.once or getattr(self, "mediated", False):
            return  # exécuteur médié : `work.open_with_branch` hors contrat 1.1

        def boucle() -> None:
            while not self.stop.wait(max(30.0, interval)):
                self.branch_sweep_once()

        threading.Thread(target=boucle, daemon=True, name="branches").start()

    def _start_thread_expiry_notice(self) -> None:
        """#421 r7 : un exécuteur médié n'a pas de ménage local ; l'expiration
        des fils y a son propre réveil, une fois par jour (L361b : elle passe
        par l'opération serveur `threads.recount`), quel que soit l'intervalle
        du ménage. Une erreur de l'avis est journalisée, le suivant arrive."""
        def avis() -> None:
            try:
                self.expire_threads_once(None)
            except Exception as exc:  # the notice's thread never dies of it
                log_async("thread expiry notice failed (%s: %s)" % (type(exc).__name__, exc))

        def boucle() -> None:
            avis()
            while not self.stop.wait(3600.0):
                avis()

        threading.Thread(target=boucle, daemon=True, name="fils-expiration").start()

    def start_housekeeping_poll(self) -> None:
        """Passage périodique du ménage (`AMEESH_HOUSEKEEPING_INTERVAL`, défaut
        600 s ; 0 = aucun). En mode `--once`, aucun passage périodique."""
        interval = max(0.0, float(getattr(self.cfg, "housekeeping_interval", 0) or 0))
        if getattr(self, "mediated", False) and not self.once:
            self._start_thread_expiry_notice()
        if interval <= 0 or self.once or getattr(self, "mediated", False):
            return  # exécuteur médié : pas de base, pas de ménage local (contrat 1.1)

        def boucle() -> None:
            while not self.stop.wait(max(30.0, interval)):
                self.housekeeping_once()

        threading.Thread(target=boucle, daemon=True, name="menage").start()

    def refresh_host_limits(self, canon, *others, complete: bool = True) -> None:
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
        `host`.

        L159f : `complete` est faux quand un canon configuré n'a pas été lu ;
        la porte d'hôte garde alors son état (la fiche Host qui fait un hôte
        prêteur peut être dans le canon manquant : jamais de porte retirée,
        ni changée, sur une partie des canons)."""
        from . import resources as resources_mod
        canons = [c for c in (canon,) + tuple(others)
                  if c is not None and hasattr(c, "host") and hasattr(c, "hosts")]
        if not canons:
            return  # objet factice des tests de `canon_sync_once`
        mine = resources_mod.host_limits(canons, self.host)
        limits = mine["limits"]
        # L112 : bail court d'un hôte volatil (fiche Host), sinon la config
        from .mediated_executor import host_gate
        cfg = getattr(self, "cfg", None)
        ttl = host_gate.lease_ttl_for(canons, self.host, cfg.lease_ttl if cfg is not None
                                       else getattr(self, "lease_ttl", 300.0))
        if ttl != getattr(self, "lease_ttl", ttl):
            log_async("bail des agents de %s : %ds (fiche Host)" % (self.host, int(ttl)))
        self.lease_ttl = ttl
        # seuils de TOUS les hôtes des canons : le déplacement (L31) compare les
        # candidats avec les limites physiques de leur propre hôte.
        par_hote: dict = {}
        for one in canons:
            for h in one.hosts:
                if h.title not in par_hote:
                    effective = resources_mod.host_limits(canons, h.title)
                    par_hote[h.title] = effective["limits"]
                    if not hasattr(self, "_handoff_limits"):
                        self._handoff_limits = {}
                    self._handoff_limits[h.title] = dict(effective["limits"], max_agents=effective["max_agents"])
        self._host_max_agents = mine["max_agents"]
        self._host_limits_origin = mine["origin"]
        # L159f : un hôte prêteur (offre `policy.lend`) a une porte fermée
        # par défaut
        sync_gate = getattr(self, "_sync_host_gate", None)
        if sync_gate is not None and complete:
            offer = limits.get("lend")
            sync_gate(bool(offer), offer)
        from . import menage as menage_mod
        self._housekeeping = menage_mod.effective(canons, self.host)
        lock = getattr(self, "_pressure_lock", None)
        if lock is None:
            self._host_limits = limits
            return
        with lock:
            self._all_host_limits = par_hote
            if limits != self._host_limits:
                self._host_limits = limits
                self._pressure_at = 0.0

    #: L114b : SIGTERM reçu, retrait à engager par la boucle principale
    sigterm_requested = False

    def can_drain_on_sigterm(self) -> bool:
        """L114b : un exécuteur médié doté d'une porte se retire au point sûr
        sur SIGTERM, au lieu d'arrêter ses tours en 3 s."""
        return (bool(getattr(self, "mediated", False)) and not self.once
                and getattr(self, "host_gate", None) is not None)

    def _sigterm_drained(self) -> bool:
        """Retrait engagé par SIGTERM et terminé (aucun bail détenu). Engage
        le retrait au premier appel qui suit le signal."""
        if self.sigterm_requested and not getattr(self, "_draining_for_sigterm", False):
            self._draining_for_sigterm = True
            self.host_gate.terminate()
        if not getattr(self, "_draining_for_sigterm", False):
            return False
        with self.lock:
            alive = [w for w in self.workers.values() if w.is_alive()]
        return not alive

    def _gate_admits(self, agent: dict) -> bool:
        """`caps.max_concurrent` de la porte (L112) : agents à la fois sur
        l'appareil, éphémères compris (la porte compte des processus)."""
        gate = getattr(self, "host_gate", None)
        cap = gate.max_concurrent() if gate is not None else None
        if cap is None:
            return True
        with self.lock:
            running = sum(1 for name, worker in self.workers.items()
                          if name != (agent or {}).get("name") and worker.is_alive())
        return running < cap

    def admits_new_worker(self, agent: dict) -> bool:
        """L43 (0031) : `max_agents` PHYSIQUE de l'hôte (le plus petit des
        fiches Host de tous les canons) borne le nombre de personas que cet
        exécuteur fait tourner à la fois. Les éphémères n'y comptent pas (ils
        n'ont pas d'admission ; `max_agents` compte des admissions). Sans
        maximum déclaré : pas de borne."""
        if getattr(self, "mediated", False) and not self._gate_admits(agent):
            return False  # L112 : `caps.max_concurrent` de la porte
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

    def handoff_limits_for(self, host: str) -> dict | None:
        """Effective physical limits and max_agents, for reserved host moves."""
        return getattr(self, "_handoff_limits", {}).get(host)

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
        # L106 : l'alimentation est celle de CET hôte, lue sur place à chaque
        # verdict (pas seulement au dernier relevé publié, ni seulement quand
        # la base répond)
        power = self.power_reading()
        if reading is None and power.get("on_ac") is None \
                and power.get("battery_percent") is None:
            verdict: dict = {"blocked": False, "critical": False, "breaches": [],
                             "limits": limits,
                             "power": resources_mod.power_state(None, limits)}
        else:
            merged = dict(reading or {}, **{k: v for k, v in power.items()
                                             if v is not None})
            verdict = resources_mod.pressure(merged, limits=limits)
            if reading is not None:
                verdict["since"] = reading.get("sampled_ts")
                verdict["reading"] = reading
        with self._pressure_lock:
            self._pressure = verdict
            self._pressure_at = now
            self._pressure_episode_update(verdict)
        return dict(verdict)

    # -- pression : orchestrateurs et épisodes ------------------------------
    #: #453 : période du fil `plafond` (secondes ; le plafond lui-même n'est
    #: relu que toutes les `suite_cap.CAP_EVERY_S`, hors arrêt en cours)
    SUITE_CAP_TICK_S = 5.0

    def start_suite_cap(self) -> None:
        """#453 : le fil `plafond` arrête les suites de tests lancées hors
        `ameesh heavy` au-delà du plafond d'une commande lourde (`suite_cap`)."""
        def boucle() -> None:
            while not self.stop.wait(self.SUITE_CAP_TICK_S):
                try:
                    self.suite_cap.tick()
                except Exception as exc:  # jamais fatal à l'exécuteur
                    log_async("test suite cap failed (%s)" % type(exc).__name__)

        threading.Thread(target=boucle, daemon=True, name="plafond").start()

    def is_orchestrator(self, name: str) -> bool:
        """`name` est-il un orchestrateur ? Rôle au canon (`roles:
        [orchestrateur]` de sa fiche Agent) ou `AMEESH_ALERT_ORCHESTRATORS`."""
        return (name in getattr(self, "_orchestrators_declared", ())
                or name in getattr(self, "_orchestrators_roles", ()))

    def refresh_orchestrators(self, *canons) -> None:
        """Après chaque `canon sync` : les rôles orchestrateur et auditeur."""
        from . import sous_utilisation as su
        self._orchestrators_roles = frozenset(su.role_orchestrators(canons))
        self._audit_canons = tuple(canons)

    def is_auditor(self, name: str, agent=None) -> bool:
        """L234 : persona auditeur ou rôle auditeur/auditor au canon
        (`auditor.is_auditor`). Le worker passe sa ligne fraîche, ce qui évite
        une requête sur le chemin de lancement."""
        from . import auditor as auditor_mod
        return auditor_mod.is_auditor(self.db, name, getattr(self, "_audit_canons", ()),
                                      agent)

    def is_critical(self, name: str, agent=None) -> bool:
        """L242b : agent critique, jamais en pause budget tant qu'un compte a
        de la marge (`auditor.is_critical`, partagé avec L233b)."""
        from . import auditor as auditor_mod
        return auditor_mod.is_critical(self.cfg, self.db, name,
                                       getattr(self, "_audit_canons", ()),
                                       declared=getattr(self, "_orchestrators_declared", ()),
                                       agent=agent)

    def audit_command(self, name: str, argv: list, agent=None, *,
                      exempt: bool = False, seconds: float = 300) -> list:
        """L234 : faible priorité CPU et I/O, héritée par les sous-processus.

        Les wrappers sont facultatifs sur les plateformes qui ne les ont pas.
        Aucun preexec_fn dans cet exécuteur multithreadé.
        """
        if not self.is_auditor(name, agent):
            return argv
        prefix = []
        ionice = shutil.which("ionice")
        nice = shutil.which("nice")
        if ionice:
            prefix.extend([ionice, "-t", "-c", "3"])
        if nice:
            prefix.extend([nice, "-n", "10"])
        bounded = prefix + list(argv)
        if exempt:
            from .platform import audit_guard
            return audit_guard.command(bounded, seconds)
        return bounded

    def _pressure_episode_update(self, verdict: dict) -> None:
        """À chaque verdict FRAIS, sous `_pressure_lock` : une ligne de journal
        au début d'un épisode de pression, une à sa fin — début, fin, durée,
        tours retardés —, jamais une par tentative de tour."""
        from . import resources as resources_mod
        episode = getattr(self, "_pressure_episode", None)
        if verdict.get("blocked"):
            tmpfs = any(b.get("key") == "max_tmpfs_used" for b in verdict.get("breaches") or ())
            if episode is None:
                self._pressure_episode = episode = {
                    "since": time.time(), "critical": bool(verdict.get("critical")),
                    "delayed": set(), "rotations": set(), "orchestrator_turns": 0,
                    "auditor_turns": 0,
                    "tmpfs": False}
                batterie = any(b.get("power") for b in verdict.get("breaches") or ())
                log_async("pressure on host %s: start — %s; except auditors, no new turn%s"
                          % (self.host, resources_mod.describe(verdict.get("breaches"))
                             or "threshold",
                             " (critical pressure)" if verdict.get("critical") else
                             " (low battery)" if batterie else
                             " except an orchestrator's (non-critical pressure)"))
            elif verdict.get("critical"):
                episode["critical"] = True
            if tmpfs and not episode.get("tmpfs"):
                # L162 : la pression vient du tmpfs — ménage tout de suite (une
                # fois par épisode), puis les plus grosses entrées au journal
                episode["tmpfs"] = True
                self.tmpfs_pressure_pass()
            return
        if episode is None:
            return
        self._pressure_episode = None
        fin = time.time()
        retardes = sorted(episode["delayed"])
        log_async("pressure on host %s: end — from %s to %s (%s)%s; %d turn(s) delayed%s"
                  "%s%s" % (
                      self.host, time.strftime("%H:%M:%S", time.localtime(episode["since"])),
                      time.strftime("%H:%M:%S", time.localtime(fin)),
                      _duree(fin - episode["since"]),
                      ", critical at times" if episode["critical"] else "",
                      len(retardes),
                      " (%s%s)" % (", ".join(retardes[:12]), ", …" if len(retardes) > 12
                                   else "") if retardes else "",
                      "; session rotation postponed: %s"
                      % ", ".join(sorted(episode["rotations"])) if episode["rotations"] else "",
                      "; %d orchestrator turn(s) let through" % episode["orchestrator_turns"]
                      if episode["orchestrator_turns"] else "")
                  + ("; %d auditor turn(s) let through" % episode.get("auditor_turns", 0)
                     if episode.get("auditor_turns") else ""))

    def pressure_delayed(self, agent: str, *, rotation: bool = False) -> None:
        """Un tour (ou une rotation de session) de `agent` attend la fin de
        l'épisode de pression en cours : compté pour sa ligne de fin."""
        with self._pressure_lock:
            episode = getattr(self, "_pressure_episode", None)
            if episode is not None:
                episode["rotations" if rotation else "delayed"].add(agent)

    def pressure_bypassed(self, agent: str) -> None:
        """Un tour d'orchestrateur ou d'auditeur part malgré la pression."""
        with self._pressure_lock:
            episode = getattr(self, "_pressure_episode", None)
            if episode is not None:
                key = "auditor_turns" if self.is_auditor(agent) else "orchestrator_turns"
                episode[key] = episode.get(key, 0) + 1

    def power_reading(self) -> dict:
        """Alimentation de l'hôte (L106) ; surchargée par les tests."""
        from . import resources as resources_mod
        try:
            return resources_mod.power()
        except Exception:  # une mesure ne fait jamais tomber l'exécuteur
            return {"on_ac": None, "battery_percent": None}

    def power_guard(self) -> str:
        """Garde d'alimentation (L106), à chaque passe de l'exécuteur.

        * `low` (sur batterie, sous `min_battery_percent`) : `host_pressure`
          bloque déjà tout NOUVEAU tour ; journal au changement d'état ;
        * `stop` (sous `stop_battery_percent`) : arrêt propre — les tours en
          cours ont `power_stop_grace` pour finir, puis sont arrêtés (SIGTERM,
          consigne remise en attente) ; statut « hôte non prêt » ; baux rendus ;
          plus aucune réclamation ;
        * retour du secteur (ou mesure perdue) : réclamation reprise, les agents
          repartent d'eux-mêmes (le statut est levé au premier sondage).

        Rend l'état (`ok`, `low`, `stop`, `unknown`)."""
        verdict = self.host_pressure()
        power = verdict.get("power") or {}
        state = power.get("state") or "unknown"
        if state != self._power_state:
            precedent, self._power_state = self._power_state, state
            pct = power.get("battery_percent")
            if state == "low":
                log_async("power: on battery, %s %% (< %s %%) — no new "
                          "turn; clean shutdown at %s %%" % (pct, power.get("min"),
                                                            power.get("stop")))
            elif state == "stop":
                log_async("power: critical battery, %s %% (≤ %s %%) — clean "
                          "shutdown of this runner's agents" % (pct, power.get("stop")))
            elif precedent in ("low", "stop"):
                log_async("power: %s — resuming"
                          % ("mains power back" if power.get("on_ac") else
                             "reading lost" if state == "unknown" else
                             "battery at %s %%" % pct))
        if state == "stop" and not self.power_hold.is_set():
            self.power_hold.set()
            texte = ("battery %s %% on battery (stop threshold %s %%): clean shutdown, "
                     "resuming when mains power returns"
                     % (power.get("battery_percent"), power.get("stop")))
            self._power_thread = threading.Thread(
                target=self._power_stop, args=(texte,), daemon=True, name="alimentation")
            self._power_thread.start()
        elif state in ("ok", "unknown") and self.power_hold.is_set():
            if self._power_thread is None or not self._power_thread.is_alive():
                self.power_hold.clear()
                self.wake_all.set()
        return state

    def _power_stop(self, texte: str) -> None:
        """L'arrêt propre sur batterie critique (L106), hors du fil principal."""
        with self.lock:
            workers = list(self.workers.values())
        for worker in workers:
            worker.stop_for_power(texte)
        deadline = time.monotonic() + self.power_stop_grace
        for worker in workers:
            # le tour en cours finit de lui-même s'il le peut (point sûr) ;
            # au-delà du délai, SIGTERM puis SIGKILL du groupe — y compris un
            # tour sur le point de publier son harnais (`stop_requested`)
            while worker.is_alive() and time.monotonic() < deadline \
                    and not self.stop.is_set():
                time.sleep(0.2)
            if worker.is_alive():
                worker.stop_group_now("critical battery", grace=10.0)
        for worker in workers:
            worker.join(timeout=30.0)
        log_async("power: %d agent(s) stopped cleanly, leases returned; "
                  "resuming when mains power returns" % len(workers))

    def served_rows(self, db) -> list[dict]:
        """The agents this runner leads: this host, mode `execute`, not
        stopped, within `--agents`."""
        return [r for r in registry.overview(db)
                if (r.get("host") or "") == self.host
                and (r.get("mode") or "execute") == "execute"
                and r.get("status") != "stopped"
                and (not self.agents_filter or r["name"] in self.agents_filter)]

    def harness_versions_once(self, now: float | None = None) -> list[dict] | None:
        """L120: read and record the version of the harnesses this runner
        serves, at most every `PROBE_INTERVAL_S`; never raises. Read only:
        a harness is never updated here."""
        if getattr(self, "mediated", False):
            return None  # host maintenance is not admitted by contract 1.1
        from . import harness_versions as versions_mod
        now = time.monotonic() if now is None else now
        if now < getattr(self, "_versions_due", 0.0):
            return None
        self._versions_due = now + versions_mod.PROBE_INTERVAL_S
        db = None
        try:
            from .scoped_db import ScopedDb
            db = ScopedDb(self.cfg)
            served = {r.get("harness") for r in self.served_rows(db)} - {None, ""}
            return versions_mod.probe_and_record(db, self.host, served)
        except Exception as exc:  # never fatal for the runner
            log_async("harness versions: not read (%s)" % type(exc).__name__)
            return None
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def log_harnesses(self) -> None:
        """Au démarrage (L106) : le binaire de chaque harnais servi, résolu et
        journalisé avec son chemin absolu — ou « hôte non prêt » tout de suite,
        sans attendre le premier tour. Ne lève jamais."""
        try:
            rows = self.served_rows(self.db)
        except db_mod.DbError:
            return
        par_harnais: dict[str, list[str]] = {}
        for row in rows:
            par_harnais.setdefault(row.get("harness") or "other", []).append(row["name"])
        for harness, noms in sorted(par_harnais.items()):
            try:
                adapter = adapters.adapter_for(harness)
                texte = adapter.resolution.describe() if adapter.resolution else "?"
            except adapters.HarnessMissing as exc:
                texte = "host not ready: %s" % exc
            log("harness %s (%s): %s" % (harness, ", ".join(sorted(noms)), texte))

    def report_boot_orphans(self) -> list[dict]:
        """Après une coupure (L106) : les ressources de tour de CET hôte encore
        `running` (ou déjà `orphan`) ouvertes AVANT le démarrage de l'hôte n'ont
        plus de tour. L147 : aucun groupe de processus ne survit à un
        redémarrage — une ligne sans conteneur encore en cours est soldée
        (`done`) ; seule une ligne dont un conteneur tourne encore (relancé par
        sa politique de redémarrage), ou invérifiable faute de moteur, reste
        orpheline et signalée (alerte `orphan_resource`). Rien n'est supprimé.
        Toutes les orphelines d'avant le démarrage sont revues, page par page.
        #220 r2/r3 : juste après le démarrage, un conteneur que sa politique va
        relancer n'est peut-être pas encore là — tant que `_boot_settle_ready`
        est faux, rien n'est relevé ni écrit, la revue est refaite
        (`boot_review_at`) ; le moteur n'est interrogé qu'APRÈS ce feu vert.
        Seules les lignes qui changent sont écrites, par lots ; au plus
        BOOT_REVIEW_MAX_ROWS lignes par passage, la suite au passage suivant
        de la boucle. Rend les lignes restées orphelines. Ne lève jamais."""
        self.boot_review_at = None
        if getattr(self, "mediated", False):
            return []  # host orphan review belongs to the server/classic runner
        from . import resources as resources_mod
        boot = resources_mod.boot_time()
        if not boot:
            return []
        try:
            turns = storage.of(self.db).turn_resources
            # L147b : les lignes `running` puis les `orphan`, page par page,
            # toutes deux dans la même limite par passage
            phase, after = self._boot_review_after
            rows: list = []
            more = False
            while True:
                room = BOOT_REVIEW_MAX_ROWS - len(rows)
                if room <= 0:
                    more = True
                    break
                size = min(BOOT_ORPHAN_PAGE, room)
                lister = (turns.running_started_before if phase == "running"
                          else turns.orphans_started_before)
                page = lister(self.host, boot, after, size)
                rows += page
                if len(page) < size:
                    if phase == "running":
                        phase, after = "orphan", 0
                        continue
                    break
                after = int(page[-1]["id"])
            if not rows:
                self._boot_review_after = BOOT_REVIEW_START
                return []
            if not self._boot_settle_ready(time.time() - boot):
                # #220 r3 : ni relevé du moteur ni écriture avant le feu vert —
                # un relevé vide pris pendant le démarrage solderait à tort
                self._boot_review_after = BOOT_REVIEW_START
                self.boot_review_at = time.monotonic() + BOOT_REVIEW_EVERY_S
                log("%d turn(s) from before the reboot: review deferred, containers "
                    "may still be restarted (host booted %ds ago); next "
                    "review in %ds" % (len(rows), int(time.time() - boot),
                                        int(BOOT_REVIEW_EVERY_S)))
                return []
            runtime = self.container_runtime()
            running = self._running_containers(runtime)
            kept, dead, newly = [], [], []
            for row in rows:
                alive = self._containers_alive(running, runtime, row)
                if alive is not None and not alive:
                    dead.append(row)
                    continue
                kept.append(row)
                if row.get("status") != "orphan":
                    newly.append((row, alive))
            turns.settle_many([row["turn_id"] for row in dead])
            turns.mark_orphan_many([row["turn_id"] for row, _ in newly])
            for row in dead:
                log("turn %s of %s from before the host reboot: group %s "
                    "gone, no container running — settled"
                    % (row["turn_id"], row.get("agent"), row.get("pgid") or "?"))
            for row, alive in newly:
                log("orphan resource from before the host reboot: turn %s of %s "
                    "(group %s%s) — reported, nothing is deleted"
                    % (row["turn_id"], row.get("agent"), row.get("pgid") or "?",
                       ", containers %s" % ", ".join(alive or row["containers"])
                       if row.get("containers") else ""))
            if len(kept) > len(newly):
                log("%d resource(s) from before the reboot already reported, still there"
                    % (len(kept) - len(newly)))
            if more:
                # la suite au prochain passage de la boucle, sans attendre
                self._boot_review_after = (phase, after)
                self.boot_review_at = time.monotonic()
            else:
                self._boot_review_after = BOOT_REVIEW_START
            return kept
        except db_mod.DbError as exc:
            log_async("resources from before the reboot not checked (%s); next "
                      "review in %ds" % (db_mod.explain(exc), int(BOOT_REVIEW_EVERY_S)))
            self.boot_review_at = time.monotonic() + BOOT_REVIEW_EVERY_S
            return []

    def settle_dead_turns(self) -> list[dict]:
        """#339 : les lignes de CET hôte `orphan`, ou `running` depuis plus de
        LIVE_REVIEW_IDLE_S (laissées par un exécuteur mort ou relancé), dont
        plus rien ne survit sont soldées (`done`) — sans attendre un
        redémarrage de l'hôte (L147). Plus rien : ni le groupe de processus,
        ni un processus de l'utilisateur qui porte l'identifiant du tour
        (`AMEESH_TURN_ID`, hérité même après `setsid`), ni un conteneur
        étiqueté par le tour sur l'un des moteurs (`containers.all_runtimes`).
        Dans le doute, la ligne reste : sans groupe noté, groupe vivant (même
        réutilisé ; les zombies seuls ne comptent pas), processus à
        l'environnement illisible lancé pendant le tour (marges comprises ;
        sans fin connue, tout lancement depuis son début), moteur muet, table
        des processus ou démarrage de l'hôte inconnus. Pas avant le feu vert
        du démarrage (`_boot_settle_ready` :
        un conteneur relancé par sa politique peut manquer encore). Une ligne
        n'est soldée que si elle n'a pas changé depuis sa lecture
        (`settle_unchanged`). Rien n'est supprimé ; au plus
        BOOT_REVIEW_MAX_ROWS lignes par passage, la suite au passage suivant.
        Rend les lignes soldées. Ne lève jamais."""
        from . import containers as containers_mod, platform as os_layer
        from . import resources as resources_mod
        from .turn_processes import running_groups
        self.live_review_at = time.monotonic() + LIVE_REVIEW_EVERY_S
        boot = resources_mod.boot_time()
        if not boot or not self._boot_settle_ready(time.time() - boot):
            return []
        try:
            turns = storage.of(self.db).turn_resources
            rows: list = []
            after = self._live_review_after
            more = False
            while True:
                room = BOOT_REVIEW_MAX_ROWS - len(rows)
                if room <= 0:
                    more = True
                    break
                size = min(BOOT_ORPHAN_PAGE, room)
                page = turns.to_review(self.host, LIVE_REVIEW_IDLE_S, after, size)
                rows += page
                if len(page) < size:
                    break
                after = int(page[-1]["id"])
            self._live_review_after = after if more else 0
            if more:
                self.live_review_at = time.monotonic()  # la suite sans attendre
            if not rows:
                return []
            groups = running_groups(int(row["pgid"]) for row in rows if row.get("pgid"))
            if groups is None:
                return []  # unknown membership never proves a turn dead
            candidates = [row for row in rows
                          if row.get("pgid") and int(row["pgid"]) not in groups]
            if not candidates:
                return []
            try:
                scan = os_layer.env_values("AMEESH_TURN_ID")
            except os_layer.NotAvailable as exc:
                log("turn resources not settled: %s; next review in %ds"
                    % (exc, int(LIVE_REVIEW_EVERY_S)))
                return []
            unread = scan["unread"]
            if any(stamp is None or not math.isfinite(stamp) for stamp in unread):
                log("turn resources not settled: a process with an unreadable "
                    "environment and no start time; next review in %ds"
                    % int(LIVE_REVIEW_EVERY_S))
                return []
            def unread_may_belong(row: dict) -> bool:
                if not unread:
                    return False
                start, end = row.get("started_ts"), row.get("ended_ts")
                if start is None or not math.isfinite(start):
                    return True  # no lower bound proves a process unrelated
                if end is not None and (not math.isfinite(end) or end < start):
                    return True
                # #1266: inspect EVERY start, not just the latest: a process
                # after the end must not hide a possible survivor born inside.
                # An unfinished turn retains #339's unbounded upper interval.
                return any(stamp >= start - LIVE_REVIEW_START_SLACK_S
                           and (end is None or stamp <= end + LIVE_REVIEW_START_SLACK_S)
                           for stamp in unread)

            candidates = [row for row in candidates
                          if row["turn_id"] not in scan["values"]
                          and not unread_may_belong(row)]
            if not candidates:
                return []
            engines, silent = containers_mod.all_runtimes(self.cfg)
            running: dict = {}
            for engine in engines:
                part = self._running_containers(engine)
                if part is None:
                    silent.append(engine.binary + (" (%s)" % engine.context
                                                   if engine.context else ""))
                    continue
                for turn, ids in part.items():
                    running.setdefault(turn, []).extend(ids)
            if silent:
                log("turn resources not settled: container engine %s not "
                    "answering; next review in %ds"
                    % (", ".join(silent), int(LIVE_REVIEW_EVERY_S)))
                return []
            # sans moteur, une ligne qui note des conteneurs reste
            dead = [row for row in candidates
                    if self._containers_alive(running, engines or None, row) == []]
            settled = set(turns.settle_unchanged(dead))
            dead = [row for row in dead if row["turn_id"] in settled]
            for row in dead:
                log("turn %s of %s (%s): group %s gone, no process nor container "
                    "left — settled" % (row["turn_id"], row.get("agent"),
                                        row.get("status"), row["pgid"]))
            return dead
        except db_mod.DbError as exc:
            log_async("turn resources not reviewed (%s); next review in %ds"
                      % (db_mod.explain(exc), int(LIVE_REVIEW_EVERY_S)))
            return []

    def _boot_settle_ready(self, uptime: float) -> bool:
        """#220 r2 : les conteneurs relancés au démarrage ont-ils eu le temps
        de l'être ? Pas avant BOOT_SETTLE_S de fonctionnement, ni tant que le
        gestionnaire utilisateur démarre encore ses unités (celles qui
        relancent les conteneurs) ; au-delà de BOOT_SETTLE_MAX_S, oui."""
        if uptime < BOOT_SETTLE_S:
            return False
        if uptime >= BOOT_SETTLE_MAX_S:
            return True
        return self._user_manager_state() not in ("initializing", "starting")

    @staticmethod
    def _user_manager_state() -> str | None:
        """L'état du gestionnaire systemd de l'utilisateur
        (`systemctl --user is-system-running`), None sans systemd."""
        try:
            out = subprocess.run(["systemctl", "--user", "is-system-running"],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() or None

    @staticmethod
    def _running_containers(runtime) -> dict | None:
        """L147 : les conteneurs en cours par étiquette de tour (`ameesh.turn`),
        en un seul relevé ; {} sans moteur ; None si le moteur ne répond pas."""
        if runtime is None:
            return {}
        listed = runtime.list_all()
        if listed is None:
            return None
        out: dict = {}
        for row in listed:
            turn = (row.get("labels") or {}).get("ameesh.turn")
            if turn:
                out.setdefault(turn, []).append(row["id"])
        return out

    @staticmethod
    def _containers_alive(running, runtime, row) -> list | None:
        """L147 : les conteneurs du tour encore en cours ([] : aucun), lus par
        l'étiquette du tour même si la ligne n'en note aucun — un tour
        interrompu n'en note pas (relevé à la fin du tour). None si le moteur
        ne répond pas, ou si la ligne en note et que l'hôte n'a pas de moteur."""
        if running is None or (runtime is None and row.get("containers")):
            return None
        return list(running.get(row["turn_id"], []))

    def run(self) -> int:
        log("starting: host %s, runner %s, driver %s, schema %s, mail "
            "batch %ds (L125)" % (self.host, self.runner_id, self.db.name,
                                     self.cfg.schema, int(self.mail_batch)))
        self.start_canon_sync()
        # L159f : suivi de la porte et acquittements — en `--once` aussi (r3) :
        # le tour de premier plan compte dans l'accusé partagé de l'hôte
        self._start_host_gate()
        self.start_balance_poll()
        self.start_resource_poll()
        self.start_version_poll()
        self.start_housekeeping_poll()
        self.start_branch_sweep_poll()
        if self.once:
            try:
                self.sweep()
            finally:
                gate = self.host_gate
                if gate is not None:
                    gate.stop()  # sa part d'accusé quitte le dossier partagé
            if not self.did_turn:
                log_async("no work available")  # chemin de sortie : par la file
            return 0 if self.did_turn else 3
        self.listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.listener_thread.start()
        # Travail de fond des tours (délai de grâce) : l'exécuteur adopte les
        # orphelins de ses tours (codes de sortie), et les suit.
        self.background.start_monitor(self.stop)
        self.start_suite_cap()
        if not self.background.subreaper:
            log_async("background work of turns: no subreaper on this OS, "
                      "exit codes unknown")
        # L106 : après l'écoute (un réveil n'attend pas ces vérifications)
        self.log_harnesses()
        self.report_boot_orphans()
        self.settle_dead_turns()
        reprise = Reprise(self.db_retry_max)
        code = 0
        try:
            while not self.stop.is_set():
                if self._sigterm_drained():
                    log_async("retrait terminé après SIGTERM : arrêt")
                    break
                if self.draining.is_set() and self._drained():
                    break  # `shutdown` rend les baux et arrête ce qui resterait
                if self._stale_restart_ready():
                    # L790 : paquet changé sous ce processus — on se remplace
                    # entre deux tours (plus aucun tour ni fond en cours).
                    log_async("outdated runner: restarting between turns")
                    code = EXIT_RESTART
                    break
                try:
                    self.power_guard()
                except Exception as exc:  # la garde ne fait jamais tomber l'exécuteur
                    log_async("power guard failed (%s)" % type(exc).__name__)
                if self.boot_review_at is not None and time.monotonic() >= self.boot_review_at:
                    self.report_boot_orphans()      # #220 r2 : solde différé
                if time.monotonic() >= self.live_review_at:
                    self.settle_dead_turns()        # #339
                try:
                    self.sweep()
                except db_mod.DbError as exc:
                    if getattr(exc, "code", None) == "executor_revoked":
                        # L109 : exécuteur révoqué par le serveur — arrêt, sans
                        # nouvel essai (les baux sont déjà relâchés côté serveur)
                        log_async("exécuteur révoqué par le serveur : arrêt (%s)" % exc)
                        self.revoked = True
                        break
                    if self.stop.is_set():
                        # arrêt en cours : systemd a pu tuer le `psql` fils
                        # avec nous (« psql : code -15 ») — rien à réessayer
                        break
                    # L72 : une passe en échec n'arrête plus l'exécuteur (et
                    # avec lui tous les harnais de l'hôte) : on réessaie.
                    attente = reprise.echec()
                    log_async("runner pass failed (%s): retrying in %ds "
                              "(failure %d, outage for %ds); turns in progress continue"
                              % (db_mod.explain(exc), int(attente), reprise.echecs,
                                 int(reprise.duree())))
                    self.stop.wait(attente)
                    continue
                bilan = reprise.retablie()
                if bilan:
                    log_async("database reachable again after %ds (%d failure(s)): runner "
                              "pass resumed" % (int(bilan[1]), bilan[0]))
                if self.max_turns and self.turns >= self.max_turns:
                    log_async("limit of %d turn(s) reached" % self.max_turns)
                    break
                self.wake_all.wait(timeout=min(self.poll, 1.0)
                                   if getattr(self, "_draining_for_sigterm", False)
                                   else self.poll)
                self.wake_all.clear()
        finally:
            self.shutdown()
        if getattr(self, "revoked", False):
            # L114 (interface `ameesh-executor/1`, §7) : 6 = exécuteur révoqué,
            # le lanceur efface le volume et ne relance pas sans nouveau code
            return EXIT_REVOKED
        return code


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-runner", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=None, help="host whose agents this runner claims")
    parser.add_argument("--runner-id", default=None, help="lease identifier (default host:pid)")
    parser.add_argument("--agents", default=None, help="comma-separated list of agents")
    parser.add_argument("--once", action="store_true",
                        help="a single pass (exit 0 if a turn ran, 3 otherwise)")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="with --once: wait up to N seconds for work")
    parser.add_argument("--dry-run", action="store_true", help="print the commands without running them")
    parser.add_argument("--max-turns", type=int, default=None, help="stop after N turns")
    parser.add_argument("--lease-ttl", type=float, default=None,
                        help="lease duration in seconds (default 300)")
    parser.add_argument("--poll", type=float, default=None,
                        help="fallback polling interval in seconds (default 5)")
    parser.add_argument("--idle-nudge", type=float, default=None,
                        help="nudge after N seconds of inactivity (default 1200)")
    parser.add_argument("--migrate", action="store_true", help="apply migrations before starting")
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
            log("working directory %s: repository %s, branch %s"
                % (cwd, marker["git_common_dir"], marker.get("branch") or "?"))
    log("agent %s registered: %s on %s%s%s" % (
        parsed.name, parsed.harness, cfg.host,
        ", session %s" % parsed.session if parsed.session else "",
        ", instruction queued" if parsed.prompt else "",
    ))
    if not parsed.session and row and row.get("session_id"):
        # L39 (0030) : `register` sans --session GARDE la session enregistrée ;
        # l'oublier proprement est le rôle de `ameesh resume --fresh`.
        log("recorded session kept (%s): \"ameesh resume %s --fresh\" to "
            "start from a new session" % (row["session_id"], parsed.name))
    return 0 if row else 1


def cmd_stop(cfg: Config, db: db_mod.Db, args: list[str]) -> int:
    if not args:
        print("usage: agent-runner stop <name>", file=sys.stderr)
        return 2
    # L37 (0030) : raison structurée — un arrêt manuel ne lève pas
    # `stopped_with_mail` (l'humain sait qu'il l'a arrêté)
    registry.set_status(db, args[0], "stopped", status_text="stopped by hand",
                        stop_reason="manuel")
    log("agent %s marked stopped" % args[0])
    return 0


# --------------------------------------------------------------------------
# attach : une session interactive qui prend le bail (C9)
# --------------------------------------------------------------------------

def attach_owner(host: str) -> str:
    """Identité d'exécuteur d'une session attachée : distincte d'un runner."""
    return "attach:%s@%s" % (getpass.getuser(), host)


def _attach_env(cfg: Config, name: str, owner: str, epoch: int,
                harness: str = "") -> dict:
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
    # #834 : la session attachée porte la même identité git qu'un tour.
    env.update(identity.git_identity(name))
    if harness:
        env["AMEESH_HARNESS"] = harness  # L224 : comme pour un tour
    return env


def _attach_tmp_env(cfg: Config, name: str, base_env: dict, holds: list) -> dict:
    """L162 : le dossier temporaire de l'agent (sur disque) et les caches gérés
    pour une session `attach`, comme pour un tour de l'exécuteur — sans vider
    le dossier (la session est celle de l'agent). Politique de la fiche Host
    (canons lus sur place) ; jamais bloquant : sans ménage possible, la
    session part avec l'environnement d'origine. L219 : le dossier reste
    verrouillé jusqu'à la fin de la session (le verrou est ajouté à `holds`,
    que l'appelant relâche)."""
    try:
        from . import canon as canon_mod
        from . import menage as menage_mod
        try:
            canons = canon_mod.load_configured(cfg)
        except Exception:
            canons = []
        policy = menage_mod.effective(canons, cfg.host or "")
        if not policy.redirect or menage_mod.disabled(base_env):
            return {}
        hold = menage_mod.hold_tmp(cfg, policy, name, new_session=False)
        holds.append(hold)
        return menage_mod.turn_env(cfg, policy, name, base_env, hold=hold)
    except Exception as exc:
        log_async("[%s] attach: temporary directory not redirected (%s)" % (name, exc))
        return {}


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
    log_async("[%s] harness stopped (%s)" % (name or "?", reason))


#: L223/L685 : nom du fichier d'état local du mode de permission interactif.
#: Son contenu est un objet JSON `{"harness": …, "incarnation": …, "mode": …}` :
#: l'autorisation est liée au harnais **et** à l'enregistrement de l'agent qui
#: l'ont reçue (voir `_attach_permission_mode`).
PERMISSION_MODE_SETTING = "permission_mode"

#: L685 r2 : contenu inerte écrit à la place du fichier quand il ne peut pas
#: être supprimé (dossier non inscriptible) — le lecteur le refuse (harnais et
#: incarnation vides) et le traite comme une absence de réglage.
_PERMISSION_MODE_REVOKED = '{"harness": "", "incarnation": "", "mode": ""}\n'


class PermissionModeWriteError(RuntimeError):
    """L685 r2 (P2) : le réglage local `permission_mode` n'a pas pu être écrit
    ou révoqué — `ameesh set` doit le dire et échouer, jamais réussir en
    silence pendant qu'un mode reste actif."""


def agent_incarnation(agent: dict) -> str:
    """L685 r2 (P1) : identité de l'enregistrement de l'agent — l'instant de
    création de sa ligne `agent_registry`, qui change quand l'agent est supprimé
    puis recréé sous le même nom (le nom seul ne distingue donc pas deux
    incarnations qui partagent le dossier d'état local). '' si la ligne ne la
    porte pas : l'appelant refuse le réglage (fail-closed)."""
    value = agent.get("created_ts")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return "%.6f" % float(value)


def _incarnation_text(value: str) -> str:
    """Rend une incarnation lisible (date locale) dans les avertissements."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError, OverflowError):
        return value or "?"


def _neutralise_permission_mode(path: str, first: OSError) -> str:
    """Unlink refusé : neutralise le fichier sur place (contenu inerte ; tout
    état partiel — fichier tronqué — est déjà refusé par le lecteur, qui exige
    un harnais et une incarnation non vides). Renvoie '' si la neutralisation a
    réussi, sinon le message d'échec : dans les deux cas l'appelant échoue de
    façon visible, la révocation ne doit jamais être silencieuse."""
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_PERMISSION_MODE_REVOKED)
    except OSError as exc:
        return ("cannot remove %s (%s) and cannot neutralise it (%s): the previous "
                "mode may still be active" % (path, first, exc))
    return ""


def write_attach_permission_mode(cfg: Config, name: str, harness: str,
                                 incarnation: str, mode: str) -> None:
    """L685: enregistre le mode de permission avec le harnais et l'incarnation
    de l'agent qui l'autorisent (mode vide : révoque le réglage). Écriture
    atomique — `attach` ne lit jamais un fichier à moitié écrit.

    L685 r2 (P2) : une révocation qui ne peut pas retirer le fichier échoue de
    façon visible (`PermissionModeWriteError`) après l'avoir neutralisé sur
    place : un succès ne doit jamais laisser un mode actif derrière lui."""
    path = os.path.join(cfg.agent_dir(name), PERMISSION_MODE_SETTING)
    if not mode:
        first: OSError | None = None
        failure = ""
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            first = exc
            failure = _neutralise_permission_mode(path, first)
        if failure:
            raise PermissionModeWriteError(failure)
        raise PermissionModeWriteError(
            "cannot remove %s (%s); the setting was neutralised in place — attach no "
            "longer applies it, but the file remains (fix the permissions of %s)"
            % (path, first, cfg.agent_dir(name)))
    try:
        os.makedirs(cfg.agent_dir(name), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"harness": harness, "incarnation": incarnation, "mode": mode}, fh)
                fh.write("\n")
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise PermissionModeWriteError("cannot write %s (%s)" % (path, exc)) from exc


def _attach_permission_mode(cfg: Config, name: str, harness: str,
                            incarnation: str) -> str:
    """L223/L685: the permission mode set for the agent (`ameesh set <agent>
    permission_mode=…`, local state of its host), '' when unset; an unreadable
    setting (permissions, invalid bytes) is ignored with a warning.

    L685 (review r1 P1): the setting is bound to the harness that received it.
    A mode set under Claude (`bypassPermissions`) is never applied after the
    agent moves to Codex: a mismatch is ignored with a warning, so the codex
    bypass flag cannot appear without a fresh `ameesh set` under Codex.

    L685 (review r2 P1): it is also bound to the agent's *registration*
    (`created_ts`): deleting the agent and recreating it under the same name,
    with its state directory kept, does not inherit the previous authorization.
    A legacy plain-text file (written before L685) carries neither and is
    refused (fail-closed), with the same warning.

    L719: the harness and the incarnation are those of the registration
    **claimed** by `attach_claim` (read under its row lock), never of the read
    that preceded it; the caller passes an unmatchable incarnation when the two
    differ, so a registration replaced while the attach was starting inherits
    nothing."""
    path = os.path.join(cfg.agent_dir(name), PERMISSION_MODE_SETTING)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:  # ValueError: UnicodeDecodeError
        print("ameesh attach: unreadable permission_mode setting (%s: %s), ignored"
              % (path, exc), file=sys.stderr)
        return ""
    try:
        document = json.loads(raw)
    except ValueError:
        document = None
    if document is None:
        print("ameesh attach: permission_mode setting is not bound to a harness "
              "(legacy format): ignored; re-run `ameesh set %s permission_mode=…` "
              "to authorize a mode" % name, file=sys.stderr)
        return ""
    if not isinstance(document, dict):
        print("ameesh attach: malformed permission_mode setting (%s), ignored" % path,
              file=sys.stderr)
        return ""
    stored = document.get("harness")
    stored_inc = document.get("incarnation")
    mode = document.get("mode")
    if stored == "" and stored_inc == "" and mode == "":
        # neutralisé par une révocation qui n'a pas pu supprimer le fichier
        return ""
    if not (isinstance(stored, str) and stored
            and isinstance(stored_inc, str) and stored_inc
            and isinstance(mode, str)):
        print("ameesh attach: malformed permission_mode setting (%s), ignored" % path,
              file=sys.stderr)
        return ""
    if stored != harness:
        print("ameesh attach: permission_mode %r was set for harness %r, ignored under "
              "%r (re-run `ameesh set %s permission_mode=…` to authorize this harness)"
              % (mode, stored or "?", harness or "?", name), file=sys.stderr)
        return ""
    if stored_inc != incarnation or not incarnation:
        print("ameesh attach: permission_mode %r was set for another registration of %s "
              "(created %s), ignored; re-run `ameesh set %s permission_mode=…` to "
              "authorize this one"
              % (mode, name, _incarnation_text(stored_inc), name), file=sys.stderr)
        return ""
    return mode.strip()


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
        print("unknown agent: %s" % name, file=sys.stderr)
        return 1
    if agent.get("status") == "stopped":
        print("agent %s stopped: nothing to attach" % name, file=sys.stderr)
        return 1
    from . import budget as budget_pause
    texte = agent.get("status_text") or ""
    if agent.get("status") == "blocked" and texte.startswith(budget_pause.PAUSE_PREFIX):
        # L242b (4) : dire la pause budget, pas un arrêt ; la session
        # interactive reste possible (elle prend le compte actif du harnais)
        print("agent %s in budget %s" % (name, texte[len(budget_pause.PAUSE_PREFIX):]),
              file=sys.stderr)
    if (agent.get("host") or "") != cfg.host:
        print("agent %s pinned to %s, not to %s" % (name, agent.get("host") or "?", cfg.host),
              file=sys.stderr)
        return 1
    try:
        adapter = adapters.adapter_for(agent.get("harness") or "other")
    except adapters.HarnessMissing as exc:
        print("attach: %s" % exc, file=sys.stderr)
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
                print("a turn is in progress for %s (rerun with --wait to wait)"
                      % name, file=sys.stderr)
                return 3
            print("turn in progress for %s: waiting…" % name, file=sys.stderr, flush=True)
            time.sleep(2.0)
            continue
        lease = registry.attach_claim(db, name, owner, ttl)
        if lease:
            break
        from . import host_holds
        hold = host_holds.live(db).get(agent.get("host") or "")
        if hold:
            # #254 : un hôte en pause ne prend aucun bail, interactif compris
            print("agent %s not claimable: host %s on hold until %s (%s); lift it: "
                  "ameesh hosts %s --release" % (
                      name, hold["host"],
                      time.strftime("%Y-%m-%d %H:%M", time.localtime(float(hold["until_ts"]))),
                      hold["reason"], hold["host"]), file=sys.stderr)
            return 1
        print("agent %s not claimable (owner unresolved, ephemeral expired, or status)"
              % name, file=sys.stderr)
        return 1
    epoch = int(lease["lease_epoch"])
    log_async("[%s] attach lease taken (epoch %d)" % (name, epoch))
    # L719 : le mode de permission est vérifié contre la registration
    # **revendiquée** par `attach_claim` (harnais et created_ts lus sous son
    # verrou), jamais contre la lecture qui a précédé la prise du bail : un
    # enregistrement supprimé puis recréé entre les deux n'hérite pas de
    # l'autorisation précédente. L'adaptateur, lui, vient de la lecture
    # antérieure : si la registration revendiquée en diffère, aucun mode ne
    # peut lui être lié — incarnation vide, donc jamais de correspondance.
    incarnation_revendiquee = agent_incarnation(lease)
    if (incarnation_revendiquee
            and (lease.get("harness") or "") == (agent.get("harness") or "")
            and incarnation_revendiquee == agent_incarnation(agent)):
        harness_revendique = lease.get("harness") or ""
        mode_incarnation = incarnation_revendiquee
    else:
        harness_revendique = lease.get("harness") or (agent.get("harness") or "")
        mode_incarnation = ""
    stop = threading.Event()
    perdu = threading.Event()
    etat = {"deadline": float(lease.get("lease_expires_ts") or 0.0)}

    def battement() -> None:
        interval = max(3.0, ttl / 3.0)
        while not stop.wait(interval):
            try:
                expires = registry.renew(db, name, owner, epoch, ttl)
            except db_mod.DbError as exc:
                log_async("[%s] attach: renewal failed (%s)" % (name, exc))
                continue
            if expires is None:
                perdu.set()  # couper d'abord : le journal ne doit jamais retarder l'arrêt
                log_async("[%s] attach: lease lost" % name)
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
                log_async("[%s] attach: lease deadline reached, stopping the harness" % name)
                return
            stop.wait(max(0.05, min(0.5, reste)))

    thread = threading.Thread(target=battement, daemon=True, name="%s-attach" % name)
    veille = threading.Thread(target=veilleur, daemon=True, name="%s-attach-veilleur" % name)
    proc: subprocess.Popen | None = None
    code = 0
    holds: list = []
    try:
        # codex verdict #182 (P2): everything after taking the lease is under the
        # `finally` — an error or a Ctrl-C while preparing (unreadable setting,
        # slow `claude --help`) still releases the lease
        thread.start()
        veille.start()
        argv = adapter.interactive_command(
            agent.get("session_id"),
            permission_mode=_attach_permission_mode(
                cfg, name, harness_revendique, mode_incarnation))
        env = _attach_env(cfg, name, owner, epoch, agent.get("harness") or "")
        env.update(adapter.env())
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        env.update(_attach_tmp_env(cfg, name, env, holds))
        cwd = agent.get("cwd") or None
        if cwd and not os.path.isdir(cwd):
            cwd = None
        if not account_turn.apply_env_attach(env, compte, cfg):
            return 1  # le `finally` rend le bail
        log_async("[%s] interactive session: %s" % (name, " ".join(argv)))
        proc = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True)
        # Course de publication (même correctif que B5a-P) : le veilleur a pu
        # conclure pendant que ce Popen n'était pas encore publié.
        if etat["deadline"] > 0 and time.time() >= etat["deadline"]:
            perdu.set()
        while proc.poll() is None:
            if perdu.is_set():
                stop_group_now(proc, "lease lost (attach)", name=name)
                break
            time.sleep(0.2)
        code = proc.wait()
    except KeyboardInterrupt:
        if proc is not None:
            stop_group_now(proc, "interrupted (attach)", name=name)
            code = proc.wait()
        else:
            code = 130  # interrupted before the harness was launched
    finally:
        stop.set()
        for hold in holds:          # L219: the session is over
            hold.release()
        for worker in (thread, veille):
            if worker.ident is not None:    # started (L223)
                worker.join(timeout=5)
        try:
            registry.release(db, name, owner, epoch)
        except db_mod.DbError:
            pass
        log_async("[%s] attach lease returned" % name)
    return code


def attach_main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="ameesh attach",
        description="Open an interactive session on an agent's lease (C9).")
    parser.add_argument("agent")
    parser.add_argument("--wait", action="store_true",
                        help="wait for a turn in progress to end before taking the lease")
    parser.add_argument("--ttl", type=float, default=None, help="lease duration in seconds")
    parsed = parser.parse_args(argv)
    cfg = load_config()
    adapters.configure(cfg)  # L106 : `harness_bins` de l'hôte
    try:
        db = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        print("ameesh attach: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    code = 1  # reste 1 si une exception traverse : le repli sort alors en échec
    try:
        try:
            db_mod.require_schema(db)
        except db_mod.SchemaMissing as exc:
            print("ameesh attach: %s" % exc, file=sys.stderr)
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


def _connect_at_start(cfg: Config, *, wait: bool) -> "db_mod.Db | None":
    """Connexion de l'exécuteur au démarrage.

    L106 : en service (`wait`), une base injoignable au démarrage — l'hôte
    redémarre et le conteneur Postgres local, ou le tunnel vers la base, n'est
    pas encore là (2026-10-10 : un exécuteur du poste sorti en échec à 12:35:20,
    relancé à la main à 12:39) — n'est plus une sortie en échec : attente
    croissante (2 s… `db_retry_max`), jusqu'à la base ou un signal d'arrêt. En
    `--once`, l'échec reste immédiat."""
    reprise = Reprise(float(getattr(cfg, "db_retry_max", 60.0)))
    arret = threading.Event()
    anciens = {}
    if wait:
        def handler(signum, _frame):
            arret.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                anciens[sig] = signal.signal(sig, handler)
            except ValueError:  # hors du fil principal (tests)
                pass
    try:
        while True:
            try:
                db = db_mod.connect(cfg)
            except db_mod.Unavailable as exc:
                if not wait or arret.is_set():
                    print("agent-runner: %s unreachable: %s"
                          % ("mesh server" if getattr(cfg, "backend", None) == "mediated"
                             else "database", exc), file=sys.stderr)
                    return None
                attente = reprise.echec()
                log("host not ready: database unreachable at startup (%s) — retrying "
                    "in %ds (failure %d)" % (db_mod.explain(exc), int(attente),
                                             reprise.echecs))
                if arret.wait(attente):
                    print("agent-runner: stop requested before the database answered",
                          file=sys.stderr)
                    return None
                continue
            bilan = reprise.retablie()
            if bilan:
                log("database reachable after %ds (%d failure(s)): starting"
                    % (int(bilan[1]), bilan[0]))
            return db
    finally:
        for sig, ancien in anciens.items():
            signal.signal(sig, ancien)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # `register --help` / `stop --help` affichent l'aide, jamais d'écriture en base
    if argv and argv[0] in ("register", "stop") and any(a in ("-h", "--help") for a in argv[1:]):
        print("usage: agent-runner register <agent> <harness> [--session S] [--prompt P] [--cwd D] …\n"
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

    from .host_gate import gate_choice
    try:
        gate_choice(cfg, runner_id=cfg.runner)
    except ValueError as exc:
        print("agent-runner: %s" % exc, file=sys.stderr)
        return 2
    db = _connect_at_start(cfg, wait=not parsed.once)
    if db is None:
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
            print("agent-runner: %s" % exc, file=sys.stderr)
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
            if getattr(exc, "code", None) == "executor_revoked":
                # L114 (§7) : révoqué avant même la fiche de l'hôte
                print("agent-runner : exécuteur révoqué par le serveur : %s" % exc,
                      file=sys.stderr)
                return EXIT_REVOKED
            # L109 : la fiche de l'hôte (`GET /host`) est illisible au démarrage
            print("agent-runner : fiche de l'hôte indisponible : %s" % exc, file=sys.stderr)
            return 1

        def handler(signum, _frame):
            # Signal d'abord ; le journal part par la file : un `print` dans un
            # gestionnaire de signal peut tomber pendant un autre `print` du
            # fil principal (écriture réentrante sur le même flux).
            if (signum in (signal.SIGTERM, signal.SIGINT) and runner.can_drain_on_sigterm()
                    and not runner.stop.is_set()):
                if not runner.sigterm_requested:
                    # L114b : premier SIGTERM d'un exécuteur médié — retrait au
                    # point sûr (90 s au plus), puis baux rendus ; un second
                    # signal arrête tout de suite. Le retrait est engagé par la
                    # boucle principale (aucun verrou pris ici).
                    runner.sigterm_requested = True
                    runner.wake_all.set()
                    log_async("signal %d reçu : retrait de l'hôte (%ds au plus), puis arrêt"
                              % (signum, int(runner.host_gate.drain_s)))
                    return
                # Second signal during the host withdrawal: immediate stop.
                runner.stop.set()
                runner.wake_all.set()
                log_async("signal %d received: second signal, immediate stop" % signum)
                return
            # Classic executor: first signal drains, second stops immediately
            # (`request_stop`).
            runner.request_stop(signum)

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
        code = runner.run()  # son `finally` rend les baux (shutdown)
    finally:
        try:
            db.close()
        finally:
            # Incontournable, même sur exception, après la remise des baux : aucun
            # fil démon ne doit écrire pendant la finalisation (SIGABRT, 2026-10-05).
            _sortie_sure(code, runner.stopped_threads if runner is not None else ())
    # L790 : le paquet installé a changé sous ce processus. Baux rendus, base
    # fermée et journal vidé par le `finally` ci-dessus : on rejoue l'invocation
    # telle quelle (mêmes options, même identité) pour reprendre le code neuf,
    # sans attendre un humain. Si le remplacement direct échoue, le code 75
    # sort en échec et le service (`Restart=on-failure`) relance l'exécuteur.
    if code == EXIT_RESTART:
        _restart_with_installed_code()
    return code


def _restart_argv() -> list[str]:
    """L790 : l'invocation à rejouer, mode d'appel préservé.

    `sys.argv[0]` ne dit pas le mode : sous `python -m ameesh.runner`, il porte
    le chemin du module (`…/runner.py`), et rejouer `[sys.executable, argv[0]]`
    lance le fichier seul — ses imports relatifs le refusent. `sys.orig_argv`
    (3.10+) garde la ligne de commande réelle : `-m ameesh.runner`, `-m ameesh
    run`, options de l'exécuteur et options de l'interpréteur comprises. On ne
    reprend de l'interpréteur que `sys.executable` : chemin absolu, `execv` ne
    cherche pas dans `PATH`.

    Sans `orig_argv` (3.9), le mode est indéterminable sous `-m` : on ne devine
    pas — la liste vide fait sortir en `EXIT_RESTART`, et le service
    (`Restart=on-failure`) relance l'exécuteur."""
    orig = getattr(sys, "orig_argv", None)
    if orig:
        return [sys.executable] + list(orig[1:])
    if __package__:
        return []
    return [sys.executable] + list(sys.argv)


def _restart_with_installed_code() -> bool:
    """L790 : rejoue l'invocation de l'exécuteur, entre deux tours.

    `os.execv` remplace l'image du processus : le code neuf reprend la main
    avec les mêmes arguments et le même environnement, sans passer par un
    superviseur. Rend `False` si le remplacement échoue, ou si le mode d'appel
    n'est pas rejouable (l'appelant sort alors en `EXIT_RESTART`, que le
    service relance)."""
    argv = _restart_argv()
    if not argv:
        print("agent-runner: outdated code — invocation mode not replayable "
              "(python < 3.10): the service must restart it", file=sys.stderr, flush=True)
        return False
    print("agent-runner: outdated code — restarting with the installed package",
          file=sys.stderr, flush=True)
    try:
        os.execv(argv[0], argv)
    except OSError as exc:
        print("agent-runner: restart failed: %s" % exc, file=sys.stderr, flush=True)
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
