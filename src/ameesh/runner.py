#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""agent-runner — one runner per machine: leases, turns, wake-up on NOTIFY.

  agent-runner [--host H] [--runner-id ID] [--agents a,b] [--agents-file F]
               [--once [--wait S]]
               [--dry-run] [--max-turns N] [--migrate]
               [--take-idle S] [--take-max-per-hour N]
               [--take-lot-idle S] [--take-lot-max-per-hour N]
               [--take-review-max-per-hour N] [--take-interval S]
        claims this host's agents, renews their leases, starts a turn
        when a message arrives (LISTEN/NOTIFY) or after inactivity.
        Every --take-interval seconds, takes for its own idle agents the
        best-ranked lot that fits them (L119/L165, #804) — the same guarded
        `backlog.auto_take` that `ameesh notify` calls, so distribution no
        longer depends on a central notifier.
        --agents-file F: host-side admission list owned by the operator,
        re-read on every pass (#3925): an agent written into it is claimed
        without a restart, one taken out releases its lease after its turn;
        canon, placement, max_agents and account checks still apply.
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

from ameesh import work_lifecycle as lifecycle

import argparse
import dataclasses
import getpass
import hashlib
import io
import json
import math
import os
import queue
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import account_budget, account_turn, adapters, admission_lock, auth_failure, canon as canon_mod, canon_sync
from . import cost as cost_mod
from . import context_metrics, context_profile
from . import budget as budget_mod, db as db_mod, device_state, fil, harnesses, identity, mail, registry, sentinel, storage
from . import rate_limit as rate_limit_mod
from . import account_window
from . import provider_credits
from . import persona_memory as memory_mod
from . import train_events
from . import migrations
from . import turn_context
from . import events as events_mod, turn_stages
from . import venv_guard
from . import workspaces
from .config import CHANNEL_BUDGET, CHANNEL_LEASE, CHANNEL_MAIL, NAME_RE, Config
from . import accounts as accounts_mod, session_backup as session_backup_mod
from .config import fresh_radar, load as load_config


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


class SystemClock:
    """Horloge de bail de l'exécuteur (#2600).

    `time` est l'heure murale qui date l'échéance en base, `monotonic` la borne
    locale du bail. Les tests de course injectent une horloge simulée : le
    temps n'y avance que lorsqu'ils le décident, au lieu de dépendre de
    l'ordonnanceur d'une CI chargée. Les deux méthodes relisent le module
    `time` à chaque appel, donc un test qui le patche garde la main.
    """

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


#: Horloge par défaut : celle du système. Un `Runner` peut la remplacer
#: (`runner.clock = …`) avant de construire ses workers.
SYSTEM_CLOCK = SystemClock()


def _clock_of(worker) -> SystemClock:
    """L'horloge d'un worker (ou l'horloge système d'un double partiel)."""
    return getattr(worker, "clock", SYSTEM_CLOCK)


#: Marge de dérive tolérée entre l'horloge de la base (`session_reset_at`,
#: `clock_timestamp()`) et celle de l'hôte (mtime du fichier de session local).
#: Dans la fenêtre, on tranche pour l'oubli : une session oubliée ne ressuscite
#: jamais ; un fichier frais de moins que la marge est seulement écarté (il
#: n'est qu'un repli, le registre qui porte l'id est consulté en premier).
#: Même règle dans `reprise._local_session` (#1481, #1898 N4).
SESSION_RESET_SKEW_S = 5.0


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


def _modele_publie(document_model, ledger_model) -> str | None:
    """CTX-03b : le modèle demandé à publier dans le profil du tour.

    Le modèle du flux (``document_model``) prime ; à défaut, celui du grand
    livre (``ledger_model``), qui porte pour Codex le modèle lu dans le journal
    de session. Le sentinelle de tarification ``inconnu`` n'est jamais un nom
    de modèle : il rend ``None``, jamais une valeur supposée.
    """
    for value in (document_model, ledger_model):
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value and value.lower() != "inconnu":
            return value
    return None


def _sans_raison(observation: dict | None) -> dict | None:
    """Une observation de décision sans son texte libre, pour le journal.

    ``reason`` reste dans la mémoire du worker (journal local) ; l'événement
    partagé ne porte que des états : ``kind`` et les valeurs mesurées.
    """
    if not observation:
        return observation
    return {key: value for key, value in observation.items()
            if key != "reason"}


def _reasons_uniques(*groups) -> tuple[str, ...]:
    """Union ordonnée de raisons, sans doublon (état de session CTX-02b)."""
    out: list[str] = []
    for group in groups:
        for reason in group or ():
            if reason and reason not in out:
                out.append(reason)
    return tuple(out)


def _background_admission_error(process, name, exc):
    """Report admission state changes, keeping auxiliary threads retryable."""
    states = getattr(process, '_writer_admission_states', None)
    if states is None:
        states = process._writer_admission_states = {}
    state = type(exc) if exc is not None else None
    previous = states.get(name)
    if state != previous:
        states[name] = state
        if exc is None:
            log_async("%s: writer admission recovered" % name)
        else:
            log_async("%s: writer admission refused (%s): %s" %
                      (name, type(exc).__name__, " ".join(str(exc).split())[:240]))
    return False if name == 'canon_sync_once' else 0 if name == 'balance_once' else None


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
#: L76 : après un échec du processus résident (démarrage raté, mort), un tour
#: par événement pendant ce délai, doublé à chaque échec de suite, borné
RESIDENT_RETRY_MIN_S = 60.0
RESIDENT_RETRY_MAX_S = 3600.0
#: L76 : attente de la fin d'un tour résident après son flux (harnais tué)
RESIDENT_END_WAIT_S = 5.0
#: L77/B8 : borne de jonction du moniteur de préemption avant de solder les
#: steers en fin de tour. Un `driver.steer` accepté après le solde laisserait
#: une réservation orpheline ; la jonction ferme ce cycle.
PREEMPT_JOIN_S = 10.0
#: L77/B8 : barrière de steer. Le pilote borne une requête `turn/steer` à
#: `resident.CONTROL_TIMEOUT` (15 s), avec une attente préalable possible du
#: `turn/start` : la fin de tour attend le steer en cours au-delà de la simple
#: jonction du moniteur, puis vérifie le `TurnResult` effectif.
PREEMPT_STEER_WAIT_S = 35.0
#: #339: this host's rows whose group is gone are settled at start and then
#: every LIVE_REVIEW_EVERY_S (L147's rule, with a liveness check in place of
#: the boot time); a `running` row only once its turn (or grace period) is
#: LIVE_REVIEW_IDLE_S old, so a turn that is closing is never raced
LIVE_REVIEW_EVERY_S = 300.0
LIVE_REVIEW_IDLE_S = 600.0
#: L498 : attente maximale du READY du trampoline de cgroup (secondes) —
#: le placement précède l'exec du harnais, quelques millisecondes en pratique
TURN_CGROUP_READY_S = 30.0
#: L976 : au plus un balayage du courrier en souffrance par heure (le seuil de
#: l'alerte `mail_undeliverable` est de 15 min)
DEAD_LETTER_SWEEP_EVERY_S = 3600.0
#: #3007 : cadence du constat de vie du notificateur, porté par chaque hôte
#: (le ménage peut être coupé ; ce fil ne dépend pas de lui). La réclamation
#: en base dédoublonne les hôtes et espace les relances de six heures.
NOTIFY_HEALTH_EVERY_S = 300.0
#: #339 r2: a process of this user whose environment cannot be read keeps a
#: row if it started after the row, less this margin (the harness starts
#: just before its row is opened; the base's clock may drift from the host's)
LIVE_REVIEW_START_SLACK_S = 300.0
#: #1425 (N5) : `measurement_actor` relit les canons ; le balayage des arrêts
#: délibérés tourne à chaque passe — on espace cette lecture d'une fenêtre,
#: l'auditeur nommé (`auditeur`) restant résolu sans canon.
MEASURE_CANONS_TTL_S = 300.0
#: L85 : rétention du journal d'événements, au plus une fois par heure
EVENTS_PRUNE_EVERY_S = 3600.0
#: CTX-02b (#4870) : la qualité d'une session reprise par un worker neuf se
#: recharge du dernier `turn.context` publié. La fenêtre borne la lecture au
#: journal récent ; au-delà, ou sans preuve, la base reste inconnue.
CONTEXT_RESUME_DAYS = 30
CONTEXT_RESUME_EVENTS = 200
#: CTX-04a (#4970) : motif d'une clôture sur budget de relecture opt-in, publié
#: dans `turn.context` ; ni rotation ni compactage pour ce seul motif.
REREAD_BUDGET_CLOSE = "reread_budget"
#: #3070 : levée des portes d'appareil indisponible, au plus toutes les 5 min
DEVICE_GATES_EVERY_S = 300.0
#: #2096 : bornes de l'affichage gardé pour un tour de résumé (repli d'un flux
#: sans `final`). Le plafond en fragments ne borne pas la taille : un flux
#: bavard gardait des mégaoctets jusqu'au tour suivant. Seule la tête est lue
#: (`turn_summary` la borne à `adapters.SUMMARY_MAX_CHARS`), la queue est
#: perdue de toute façon.
SUMMARY_DISPLAY_MAX_FRAGMENTS = 10_000
SUMMARY_DISPLAY_MAX_CHARS = 64_000
#: #2096 : croissance de RSS (octets) à partir de laquelle un tour rend au
#: noyau les arènes qu'il a fait croître (pic de flux, de parsing ou de
#: journalisation). En deçà, l'allocateur garde sa marge : un tour ordinaire ne
#: paie pas le geste.
TURN_TRIM_RSS_GROWTH = 32 * 1024 * 1024


def _finite_ts(value) -> bool:
    """#1691: is `value` a usable finite instant?

    A date read back by the `psql` pilot may come as text (`'Infinity'` for an
    infinite `timestamptz`): `float()` accepts or rejects it, and a value that
    cannot be converted is an unknown date — never an exception raised into
    the runner loop, which promises not to raise."""
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False

#: #804 : cadence de la prise automatique déclenchée par l'exécuteur pour ses
#: propres agents au repos (secondes ; 0 coupe la prise de l'exécuteur). Le
#: débit horaire, lui, est compté en base pour tout le mesh (L119, 0037).
TAKE_INTERVAL_S = 60.0


def local_take(db, cfg, agents, settings, *, dry_run: bool = False, alerts=None,
               **kw) -> list:
    """#804 : prise automatique pour un ensemble d'agents (ceux de cet
    exécuteur), avec les garde-fous de `backlog.auto_take` (repos réel,
    budget, hôtes en pause, plafonds horaires comptés en base) ; l'appelant
    passe aussi l'alerte `balance_low` du moment (#754, owner 13/10).

    `settings` porte `idle_s`/`max_per_hour`/`lot_idle_s`/`lot_max_per_hour` ;
    `kw` surcharge les autres réglages d'`auto_take` (tests, diagnostic). Rend
    les prises faites ; l'appelant isole la panne éventuelle.
    """
    from . import backlog
    names = {str(name) for name in (agents or ()) if name}
    if not names:
        return []
    options = dict(settings)
    options.update(kw)
    return backlog.auto_take(cfg, db, agents=names, dry_run=dry_run, alerts=alerts,
                             **options)


def local_balance_alerts(db, thresholds, now: float) -> list:
    """#754 (owner, 13/10) : l'alerte `balance_low` du moment, avec les mêmes
    seuils qu'`ameesh alerts` et `ameesh notify` — un agent au token ne prend
    jamais sous cette alerte. `notify` ne publie pas d'état d'alerte : on
    relit le solde au moment de la passe, une seule implémentation
    (`sous_utilisation.balance_low`).

    #3923 : la garde locale ne lit plus le registre complet. `balance_low`
    n'en tire que le `responsible` de l'alerte routée ; ici elle n'est qu'un
    verrou de prise, sans destinataire (`notify` passe, lui, son listing).
    Cette lecture coûtait 1,2 s par passe et par exécuteur."""
    from . import sous_utilisation
    return sous_utilisation.balance_low(db, (), now, **(thresholds or {}))


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


def dump_stacks_on_signal(signum: int | None = None, stream=None) -> bool:
    """#4515 : `kill -USR1 <pid>` écrit la pile de chaque fil sur stderr.

    Un hôte sans `CAP_SYS_PTRACE` (conteneur sans capacités) ne peut pas
    échantillonner l'exécuteur avec py-spy ni gdb : le diagnostic d'un
    exécuteur qui brûle du CPU au repos doit venir de lui-même. Le vidage est
    fait par `faulthandler`, hors de l'interpréteur (aucun verrou Python),
    même GIL disputé. Faux si le flux n'a pas de descripteur."""
    import faulthandler
    signum = signal.SIGUSR1 if signum is None else signum
    try:
        faulthandler.register(signum, file=stream or sys.stderr, all_threads=True)
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return False
    return True


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


def turn_status_text(shown: str, duration: float, *, ok: bool = True,
                     cloture: str = "") -> str:
    """`status_text` du dernier tour — une durée passée, jamais une échéance.

    #2689 : « resume in 295s » était lu comme « le prochain tour reprend dans
    295 s » alors que 295 s est la **durée** du tour de reprise qui vient de
    finir. Une valeur qui ne bouge plus passait alors pour un compte à rebours
    figé. Le libellé dit maintenant explicitement qu'il s'agit du tour passé,
    et la durée reste celle du tour (pas le délai du prochain réveil, qui se
    lit dans l'état du bail et les alertes).
    """
    texte = "last turn: %s, %ds" % (shown, int(duration))
    if not ok:
        texte += " (failed)"
    if ok and cloture:
        texte += " (closed: %s)" % cloture.split(":")[0]
    return texte


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


#: 0018 : l'identité git d'un dossier de travail et son marqueur vivent dans
#: la façade (L101 T3) ; ces noms restent importables depuis `runner`.
worktree_marker = workspaces.worktree_marker
write_worktree_marker = workspaces.write_worktree_marker


def _preemption_generation(worker) -> int:
    """#2729 : génération des demandes de préemption, lue sous son verrou.

    Point unique des deux lectures de frontière (`pick()` et l'entrée d'un
    tour direct). Les doublures `SimpleNamespace` de L790 n'ont ni compteur ni
    verrou : elles partent de zéro.
    """
    verrou = getattr(worker, "_preempt_lock", None)
    if verrou is None:
        return getattr(worker, "preempt_gen", 0)
    with verrou:
        return worker.preempt_gen


def declared_adoption_binaries(harness: str, host: str) -> set[str]:
    """Les binaires installés déclarés sur CET hôte (#4488).

    La carte locale est relue à chaque tour : un binaire installé puis déclaré
    est vu sans redémarrer l'exécuteur. Une carte illisible est une erreur
    d'hôte (fail-closed), jamais un laissez-passer."""
    from . import harness_adoption
    try:
        return harness_adoption.declared_binaries(harness, host)
    except harness_adoption.VeilleError as exc:
        raise adapters.HarnessMissing("harness-adoption bins: %s" % exc) from exc


def agent_adapter(harness: str, harness_bin: str | None,
                  host: str) -> adapters.HarnessAdapter:
    """L'adaptateur d'un agent, avec la garde de lecture de #4488.

    Un `harness_bin` écrit en base n'est lancé que s'il est DÉCLARÉ sur cet
    hôte : carte des binaires installés (`harness_adoption`), `harness_bins`
    de la configuration, ou `AMEESH_<HARNAIS>_BIN`. Sinon c'est une erreur
    d'hôte (`HarnessMissing`) : le tour n'est pas lancé, et le binaire nommé
    par la base ne reçoit jamais les identifiants du compte."""
    if harness_bin:
        adapters.require_declared_harness_bin(
            harness, harness_bin, host=host,
            extra=declared_adoption_binaries(harness, host))
    return adapters.adapter_for(harness, harness_bin)


class AgentWorker(threading.Thread):
    #: nombre d'échecs de battement consécutifs avant d'arrêter le harnais
    MAX_RENEW_FAILURES = 3
    """Un agent = un bail + une boucle de tours sur cette machine.

    C'est un thread : en mode service plusieurs agents tournent en parallèle,
    chacun avec son battement de bail.
    """

    #: #1914 : marque et pas d'un tour différé — verrou de session tenu par un
    #: tiers (hors attach). Le statut, marqué (L35), n'est posé qu'au-delà du
    #: délai de grâce, jamais par-dessus un autre blocage ni sur un agent
    #: arrêté, et n'est levé que par la même marque.
    DEFERRED_STATUS = "session lock held"
    DEFERRED_RETRY = 0.5
    DEFERRED_RETRY_MAX = 30.0
    #: #1914 : état des tours différés — remis à zéro dès qu'un tour part
    #: (`_deferral_over`). Défauts de classe, comme `resident` : une
    #: construction partielle ne doit pas lever.
    deferred_since: float | None = None
    deferred_streak = 0
    deferred_alerted = False
    deferred_wait_s = DEFERRED_RETRY
    #: #1914 : motif du dernier report, posé par `run_turn` (« session lock
    #: held » ou « attach requested ») — le worker n'espace et ne rend visible
    #: qu'un verrou tenu par un tiers.
    deferred_reason = ""
    #: #2160 : motif de la série de reports en cours. Un changement de motif
    #: (attach ↔ verrou tenu) clôt la série : la grâce d'un verrou tenu ne
    #: compte que les reports de verrou, pas les passations d'attach qui l'ont
    #: précédé.
    deferred_series_reason = ""
    #: #2814 : début (horloge murale) du tour retenu par la pression, pour le
    #: texte « queued N s » que lit le client ; défaut de classe comme les
    #: reports, une construction partielle ne doit pas lever.
    queue_since = 0.0
    #: #2814 : instant du dernier texte de pression écrit, pour ne pas
    #: réécrire `status_text` à chaque sondage (l'âge change à chaque seconde)
    _pressure_text_at = 0.0
    #: #2810 : ids des messages que le tour choisi/en cours sert déjà. Un
    #: NOTIFY pour l'un d'eux ne doit pas arrêter ce tour (son propre message).
    #: Défaut de classe : une construction partielle ne doit pas lever.
    _turn_mail_ids: frozenset[int] = frozenset()

    #: L76 : le processus résident de l'agent entre ses tours, ou None.
    #: Défaut de classe : les constructions partielles (`__new__` sans
    #: `__init__`, chemin `--once` et tests de la porte d'hôte) doivent
    #: pouvoir passer par `release_lease` -> `_close_resident` sans
    #: AttributeError (CI #417).
    resident = None

    #: L165 (EP3) : brief de relève en attente. Il ouvre la session neuve, mais
    #: le brief MINIMAL d'un lot le COMPLÈTE sans l'écraser (`_open_lot_session`,
    #: `_switch_lot`) ; il survit donc au premier tour d'un lot. Défaut de
    #: classe, comme `resident`, pour les constructions partielles.
    _election_brief = ""

    def __init__(self, runner: "Runner", agent: dict, lease: dict):
        super().__init__(name=agent["name"], daemon=True)
        self.runner = runner
        self.db = runner.db
        self.cfg = runner.cfg
        #: #1466 : configuration relue, prise au début du tour suivant (jamais
        #: au milieu d'un tour, où schéma, dossier d'état et comptes doivent
        #: rester cohérents du début à la fin)
        self._cfg_pending: Config | None = None
        self._cfg_lock = threading.Lock()
        self.name = agent["name"]
        self.epoch = int(lease["lease_epoch"])
        self.agent = agent
        #: identité du tour courant, allouée au lancement du harnais (#2894) ;
        #: la clôture générique la reçoit pour fendre un tour sandbox par
        #: identité (#4881).
        self._turn_id: str | None = None
        self.wake = threading.Event()
        #: #4602 : un signal de travail (courrier) a réveillé ce worker ; sa
        #: prochaine passe d'admission base passe avant les sondages au repos.
        self._priority_wake = False
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
        #: L76 T2 : échecs du résident de suite (remis à zéro par un tour abouti)
        self._resident_failures = 0
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
        #: #2729 : série de tours interrompus par préemption, lue du marqueur
        #: posé dans `status_text` (suffixe `preemptions xN`) — elle survit au
        #: redémarrage de l'exécuteur. Un tour qui n'est pas préempté la remet
        #: à zéro ; l'alerte `preemption_streak` lit le marqueur.
        self.preemption_streak = exploitation_mod.preemption_streak(
            agent.get("status_text"))
        self.preemption_since = 0.0
        if self.preemption_streak:
            self.preemption_since = (
                exploitation_mod.preemption_since(agent.get("status_text"))
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
        #: #3733 : épisodes de débit passager clos de l'heure glissante, et
        #: début de leur série ; le cumul est écrit dans `status_text` (un
        #: succès ne l'efface pas) et lu par `rate_limited_chronic`. Relus de
        #: la ligne avec les bornes réelles des épisodes (#3743).
        self.rate_limit_window, self.rate_limit_window_since = rate_limit_mod.window_restore(
            rate_limit_mod.window_state(agent.get("status_text")), etat_debit)
        self.last_activity = time.monotonic()
        #: #408 : dernière lecture des lots actionnables (règle 1)
        self._lot_wake_checked = float("-inf")
        #: #2466 : dernière lecture des réveils programmés échus
        self._scheduled_wake_checked = float("-inf")
        #: #2614 : réveil programmé réclamé, pas encore consommé ({lot,
        #: wake_ts, done}) — un fil en renouvelle le bail jusqu'à son oubli
        self._wake_claim: dict | None = None
        self.state_dir = self.cfg.agent_dir(self.name)
        self.renew_failures = 0
        #: #2600 : horloge du bail, injectable par les tests de course. Elle
        #: date l'échéance en base (`time`) et la borne locale (`monotonic`).
        horloge = getattr(runner, "clock", None)
        self.clock = horloge if horloge is not None else SYSTEM_CLOCK
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
        #: L77/B10 : génération des demandes de préemption, pour ne pas effacer
        #: un signal arrivé pendant le traitement du précédent.
        self._preempt_seq = 0
        #: L77/B8 : remises steer réservées, rattachées à la génération et au
        #: `Turn` effectif : (jeton, ids, génération, Turn, input_id).
        self._steer_pending: list[tuple[str, list[int], int, object | None, str]] = []
        #: L77/B8 : barrière du steer en cours. Le tour ne se solde pas tant
        #: qu'une remise steer peut encore être acceptée par le pilote.
        self._steer_lock = threading.Lock()
        self._turn_generation = 0
        self._turn_live = False
        #: #2729 : génération des demandes de préemption. `pick()` fige la
        #: frontière du tour qu'il sélectionne ; `request_preempt` incrémente
        #: et pose le drapeau sous le même verrou. Au début du tour, seule une
        #: demande ANTÉRIEURE à la sélection est un résidu à consommer : une
        #: demande arrivée depuis est un arrêt pour CE tour, jamais un résidu
        #: (relecture B1 de #2729 : le clear() fixe effaçait la nouvelle
        #: préemption reçue pendant la préparation).
        self.preempt_gen = 0
        self._preempt_lock = threading.Lock()
        #: #2810 : messages que le tour choisi/en cours sert déjà, publiés sous
        #: `_preempt_lock` — `request_preempt` les voit dans la même section
        #: critique (le NOTIFY d'un message déjà servi n'arrête pas son tour).
        self._turn_mail_ids: frozenset[int] = frozenset()
        self._refus_vus: set[int] = set()
        #: contrat 1.5 absent (ancien serveur médié) : recherche profonde des
        #: humains indisponible, dite une fois sans tuer le fil.
        self._humains_profonds_indisponibles = False
        #: arrêt demandé avant la publication du Popen (point de passage unique)
        self.stop_requested = threading.Event()
        self._stop_reason = ""
        #: garde de budget (L13, 0019) : état de pause et cadence de contrôle
        self._budget_next_check = 0.0
        self._budget_paused = False
        self._budget_reason = ""
        #: #2934 : nature de la pause (compte, fenêtre, échéance) — le fil ne
        #: reçoit une note qu'à ce changement, pas à chaque point de jauge.
        self._budget_nature = ""
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
        #: #2619 : le brief `ameesh` continue un travail en cours (session
        #: inconnue du harnais après un résumé non produit), ce n'est pas une
        #: vraie session neuve : son `$TMPDIR` est gardé comme celui d'une
        #: suite de rotation (#2610).
        self.resume_continuation = False
        self.session_turns = 0
        self.session_tokens = 0.0
        self.last_turn_seconds = 0.0
        self.last_output = ""
        #: L218 : réponse finale du dernier tour (`final` du flux, None si le
        #: flux n'en donne pas), et, pour un tour de résumé, tout son affichage
        #: (repli d'un flux sans `final`, texte brut). #2096 : les deux sont
        #: bornés (caractères) — voir `SUMMARY_DISPLAY_MAX_*`.
        self.last_final: str | None = None
        self.summary_display: list[str] = []
        self._summary_chars = 0
        #: L60 : jetons d'entrée relus (cache compris) par le dernier tour
        #: inscrit au grand livre, et sa session — mesure de coût, pas du
        #: plafond (#2336) ni de l'alerte `session_too_big`, qui lit `S_hist`
        #: de la session (`session_total`, #4448)
        self.last_turn_reread = 0
        self.last_turn_reread_session: str | None = None
        #: L217 : relevé du flux du dernier tour, fil principal (somme des
        #: relectures, pour le grand livre et l'information) et sous-agents
        self.last_turn_main_reread = 0
        self.last_turn_subagent_reread = 0
        #: L217b/#2336 : la mesure de la rotation entre deux tours, celle du
        #: plafond — le contexte du dernier appel du fil principal (taille de
        #: session) —, et sa session ; Codex : lu dans le journal de session du
        #: fil (#2932) ; None = pas de relevé d'appel (journal illisible, tour
        #: de résumé, worker relancé) : repli sur le grand livre (somme des
        #: relectures du tour), jamais pour la rotation d'un tour Codex
        self.last_turn_context: int | None = None
        self.last_turn_context_session: str | None = None
        #: L81 (étude §6.3) : instant monotone du dernier message humain en
        #: attente — un entretien de session résidente ne le précède pas de
        #: moins de `resident_runner.QUIET_S`, sauf dépassement dur
        self._dernier_humain = 0.0
        #: L81 (étude §6.1) : relu cumulé par session — il appartient à la
        #: session, pas au processus : un résident endormi puis réveillé sur
        #: le même id reprend son cumul (sinon 240+240 élude le seuil)
        self._resident_reread: dict[str, int] = {}
        #: CTX-02b : observation `ameesh-context/1` du tour en cours, publiée
        #: avec les compteurs legacy après la ligne du grand livre ; None pour
        #: un tour réparé par un autre worker (la mesure est alors perdue, pas
        #: inventée).
        self._context_observation: dict | None = None
        #: CTX-03b : fenêtre observée du tour (Window non résident, ou profil
        #: du driver résident) et mode, publiés dans `turn.context.profile`
        #: avec la ligne du grand livre — jamais une décision.
        self._context_profile_observed: object | None = None
        self._context_profile_mode: str | None = None
        #: CTX-02b : base `R_session` (relu cumulé des tours précédents) et la
        #: session qu'elle décrit ; relue du grand livre une fois par session,
        #: puis tenue à jour par l'observation du tour.
        self._context_session_id: str | None = None
        self._context_session_reread: int | None = None
        #: Qualité et raisons de cette base : un tour partiel laisse la
        #: session partielle, un tour sans mesure l'invalide (#4870, B cx1).
        self._context_session_quality: str = context_metrics.UNKNOWN
        self._context_session_reasons: tuple[str, ...] = ()
        self._context_baseline: int | None = None
        self._context_baseline_quality: str = context_metrics.UNKNOWN
        self._context_baseline_reasons: tuple[str, ...] = ()
        #: Une collecte perdue avant qu'une session ne soit connue : la
        #: prochaine base reste inconnue jusqu'à une session neuve explicite.
        self._context_session_lost: str | None = None
        #: CTX-02b : motif de clôture observé (L105) et dernière rotation
        #: réussie, publiés avec le tour — jamais lus par une décision.
        self._turn_close_observation: dict | None = None
        self._last_rotation_observation: dict | None = None
        #: CTX-04b (#4971) : `C_last`/`O_last` du dernier tour mesuré, pour la
        #: projection de fenêtre ; None après une compaction (la prochaine
        #: mesure tranche, §4.2)
        self._window_measure: dict | None = None
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
        #: L80a : `resident.turn` d'un tour que le disque n'a pas accepté —
        #: gardés en mémoire jusqu'à leur écriture, pour que la comptabilité ne
        #: règle jamais un tour dont l'usage n'a pas de source durable.
        self._resident_usage_pending: list[dict] = []
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
        #: #4164 : `status_text` du blocage « provider_credits_exhausted » posé
        #: (ou hérité d'un exécuteur précédent), '' hors blocage
        self._credits_status = (agent.get("status_text") or "") if (
            agent.get("status") == "blocked"
            and provider_credits.is_held(agent.get("status_text"))
            and provider_credits.is_held(agent.get("last_error"))) else ""
        #: #4164 : (harnais, compte) de la sonde bornée que porte le tour en
        #: cours, None hors sonde
        self._credits_probe = None
        #: #4164 (B1 cx2) : (harnais, compte, raison) d'une sonde échue, à
        #: réserver seulement si `_pick` choisit vraiment un tour
        self._credits_candidate = None
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

    def apply_config(self, cfg: Config) -> None:
        """Le fichier de configuration a été relu (#1466) : ce worker prendra la
        nouvelle configuration au début de son prochain tour. Rien n'est changé
        au milieu d'un tour : le schéma, la base et le dossier d'état d'un tour
        en cours restent ceux avec lesquels il a démarré (R1 cx2)."""
        with self._cfg_lock:
            self._cfg_pending = cfg

    def _apply_pending_config(self) -> None:
        """Frontière de tour (#1466) : la configuration relue entre en vigueur
        ici, avant tout ce qui la lit (comptes, plafonds, rôles critiques)."""
        with self._cfg_lock:
            cfg = self._cfg_pending
            if cfg is None:
                return
            self._cfg_pending = None
            self.cfg = cfg

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
        (`session_reset_ts`, posé par `ameesh restart` appliqué sans bail, et
        par tout `clear_session`, #794) n'est jamais repris. Le retrait du
        fichier est un nettoyage au mieux : la décision ne dépend pas de sa
        réussite, sinon un `unlink` refusé (PermissionError) ferait
        ressusciter l'ancienne session — sur ce worker comme sur un worker
        recréé, qui relit `session_reset_ts` au registre.

        #1898 N4 : la comparaison porte sur deux horloges (mtime de l'hôte,
        `session_reset_at` de la base) ; `SESSION_RESET_SKEW_S` couvre leur
        dérive, et dans la fenêtre on tranche pour l'oubli (voir la constante).
        """
        session = (self.agent or {}).get("session_id")
        if session:
            return session
        fichier = self.read_session_file()
        reset = (self.agent or {}).get("session_reset_ts")
        if fichier and reset:
            try:
                perime = os.stat(self._path("session")).st_mtime \
                    <= float(reset) + SESSION_RESET_SKEW_S
            except OSError:
                return None
            if perime:
                try:
                    os.unlink(self._path("session"))
                except OSError:
                    pass
                return None
        return fichier

    def write_session_file(self, session_id: str) -> None:
        try:
            with open(self._path("session"), "w", encoding="utf-8") as fh:
                fh.write(session_id + "\n")
        except OSError:
            pass

    def _note_session(self, session_new: str, session: str | None, harness: str,
                      compte, cwd: str, resume: str, harnais_lance: str) -> None:
        """Enregistre la session ouverte par le tour : fichier local, registre
        et mémoire du worker. Le tour résident (L80a) comme le tour par
        événement passent par ici : le premier ne voit sa session que dans son
        `TurnResult`, jamais dans un événement du flux brut (Codex)."""
        self.write_session_file(session_new)
        if resume:
            # #389 r2 : le résumé a ouvert une session connue
            # (gardé jusque-là : un départ avorté le rejoue)
            self._resume_drop()
        # L242b (3) : le harnais de la session (rotation au changement de
        # harnais, `account_turn.continuity`)
        self._state_note("session_harness", harness)
        # L39 (0030) : le compte d'origine de la session, en registre avec son
        # id (le marqueur du flux reste la source des relevés du tour) ; #408 :
        # et le harnais et le dossier de son ouverture, ceux du lancement,
        # seulement si le tour l'OUVRE (une session reprise garde l'origine de
        # son ouverture)
        ouverte = session_new != session
        origine = (harnais_lance if ouverte else None, cwd if ouverte else None)
        try:
            registry.set_session(
                self.db, self.name, session_new,
                compte.name if compte is not None else None,
                harness=origine[0], cwd=origine[1])
        except db_mod.DbError as exc:
            # L72 : une panne de base ne tue pas le tour (le bail, lui, est
            # défendu par le battement) ; l'enregistrement est rejoué après le
            # tour.
            self._session_en_attente = (
                session_new, compte.name if compte is not None else None,
                *origine)
            log_async("[%s] session %s not recorded (%s): replayed "
                      "after the turn" % (self.name, session_new,
                                          db_mod.explain(exc)))
        if self.agent is not None:
            # #1898 N4 : le registre porte la session qui vient d'être
            # ouverte ; la mémoire du worker suit, sinon `current_session`
            # retombait sur le fichier local et sa comparaison d'horloges
            # (marge de dérive) pour chaque tour de la même session.
            self.agent["session_id"] = session_new
            self.agent["session_account"] = (
                compte.name if compte is not None else None)
        if ouverte:
            self._installe_hook_preuve(cwd)

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
        return (self.lease_deadline > 0
                and _clock_of(self).time() >= self.lease_deadline)

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
        return max(0.0, self.lease_deadline - _clock_of(self).time())

    def _renew_call(self) -> tuple[bool, float | None, Exception | None]:
        """`registry.renew` avec une borne dure, même si l'appel se bloque."""
        result: dict = {}

        def call() -> None:
            try:
                migrations.require_compatible_writer(self.db)
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
                reste = self.lease_deadline - _clock_of(self).time()
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
                              or _clock_of(self).time() >= self.lease_deadline)
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
        return _clock_of(self).monotonic() + float(self.runner.lease_ttl)

    def release_lease(self) -> bool:
        """Rend le bail ; False quand la base ne l'a pas pris (DbError) : le
        bail reste tenu jusqu'à son échéance (`lease_held_until`, plus
        renouvelé). Perdu ou déjà repris par un autre : True, il n'est plus
        à nous. Le retour est tenté même `lease_lost` posé (r1) : des pannes
        au renouvellement le posent sans que la base ait repris le bail ; le
        retour, gardé par l'epoch, ne touche pas celui d'un autre."""
        # L76 : rien ne survit au bail, pas même un processus résident inactif
        self._close_resident("lease released")
        AgentWorker._close_peek(self)  # aussi sur les doublures de test
        self._release_session_lock()
        self.watchdog_stop.set()  # le veilleur n'a plus de bail à défendre
        # L113 : le travail part au serveur sous le bail, puis le dossier est
        # effacé (même bail perdu : rien ne doit rester sur l'appareil)
        self._depot_push(final=True)
        AgentWorker._reserve_restart(self)  # aussi sur les doublures de test
        try:
            registry.release(self.db, self.name, self.runner.runner_id, self.epoch)
        except db_mod.DbError:
            return False
        return True

    def _reserve_restart(self) -> None:
        """#4086 : un rôle élu rendu parce que l'exécuteur s'arrête (drainage,
        `vm.sh redemarrer`, code périmé) reste réservé à cet hôte pendant
        `RESTART_RESERVE_S`, écrit AVANT le retour du bail : l'exécuteur
        relancé le reprend sans relève croisée et garde sa session. Jamais
        pour une bascule planifiée (le challenger doit pouvoir réclamer), ni
        bail perdu, ni une seconde fois. Une écriture impossible ne retient
        pas l'arrêt : la relève par rang reste la règle."""
        # doublure de test partielle : un attribut absent, aucune réservation
        runner_obj = getattr(self, "runner", None)
        lost = getattr(self, "lease_lost", None)
        if (runner_obj is None or lost is None or lost.is_set()
                or getattr(self, "_restart_reserved", False)
                or str((getattr(self, "agent", None) or {}).get("releve") or "")
                != "election"):
            return
        stopping = getattr(runner_obj, "restart_pending", None)
        if stopping is None or not stopping():
            return
        if self.name in (getattr(runner_obj, "_handover_released", None) or {}):
            return
        self._restart_reserved = True
        from . import election, role_handover_pg
        try:
            # B1 de la relecture : gardée en base par le bail vivant exact
            # (propriétaire + epoch) — un bail échu repris ailleurs sans que
            # ce worker l'ait vu n'écrit rien.
            written = role_handover_pg.reserve_restart(
                self.db, runner_obj.host, self.name, election.RESTART_RESERVE_S,
                owner=runner_obj.runner_id, epoch=self.epoch)
        except Exception as exc:  # noqa: BLE001 - confort, jamais une garde d'arrêt
            log_async("[%s] restart reservation not recorded (%s)"
                      % (self.name, db_mod.brief(exc)))
            return
        if not written:
            log_async("[%s] restart reservation skipped: lease (epoch %d) no longer ours"
                      % (self.name, self.epoch))
            return
        log_async("[%s] elected role reserved to %s for %ds (executor restart)"
                  % (self.name, runner_obj.host, int(election.RESTART_RESERVE_S)))

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
            sandbox_cancel = getattr(self, '_sandbox_cancel_requested', None)
        self._stop_reason = reason
        if sandbox_cancel is not None and not hard:
            # The sandbox supervisor owns a remote Job. Let the turn thread
            # fence, delete and observe it before terminating the local child.
            sandbox_cancel.set()
            return True
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
        # L77/B5 : une préemption seule ne tue jamais un résident vivant. Elle
        # a pu arriver entre la sélection du tour et la publication du
        # `Popen` (ou après que le moniteur a rendu la main) : le message est
        # remis au point sûr, sinon gardé pour le tour suivant. Les autres
        # déclencheurs (bail, échéance, arrêt, redémarrage, retrait d'hôte)
        # gardent leur arrêt dur, y compris pour un résident.
        if self.stop_requested.is_set():
            # Le latch vient d'un `stop_group_now` : sa raison nomme la cause.
            # Une préemption qui se serait ajoutée ne doit pas masquer un arrêt
            # d'une autre classe.
            preemption = self._stop_reason == "preemption"
        else:
            preemption = self.preempting.is_set()
        autre_cause = (self.lease_lost.is_set() or self.deadline_passed()
                       or self.stopping.is_set() or self.watchdog_stop.is_set()
                       or self.restarting.is_set()
                       or getattr(self, "host_yield", None) is not None
                       or (getattr(self, "runner", None) is not None
                           and self.runner.stop.is_set())
                       or bool(self._power_texte))
        if preemption and not autre_cause and self._preempt_resident():
            self.stop_requested.clear()
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
            return max(0.0, min(cap, self.lease_deadline - _clock_of(self).time()))
        return cap

    def renew_wait(self, cap: float) -> float:
        """Attente qui laisse un renouvellement **avant** l'échéance du bail.

        `wait_timeout` borne l'attente à l'échéance même : une reprise plus
        longue que le bail (#1270, jusqu'à 600 s pour un bail de 300 s)
        dormait jusqu'à la deadline, puis `renew()` était refusé
        (`deadline_passed`) — le worker rendait le bail au lieu de reprendre
        (R1 cx3 B1). On découpe donc au pas du battement de tour (`ttl/3`).
        Chaque tranche est suivie d'un `renew()` par l'appelant (boucle de
        reprise, boucle principale).

        #2139 : une tranche ne doit pas seulement **ne pas dépasser**
        l'échéance, elle doit laisser une tranche pleine pour renouveler. Se
        borner au temps restant planifiait un réveil À l'échéance dès qu'il
        restait moins d'une tranche : la moindre gigue d'ordonnancement
        faisait alors voir `deadline_passed` à `renew()`, ou n'accordait à
        l'aller-retour de base qu'un budget nul — le worker rendait un bail
        encore valide (marges de 0,16 s mesurées sous charge, #2139). Passé ce
        point la tranche vaut 0 : l'appelant renouvelle immédiatement, avec le
        temps restant comme budget d'appel.

        #2172 : dans une panne, sous `Reprise.MINIMUM` de marge, viser la
        moitié du temps restant : attendre toute la marge planifiait l'essai
        à l'échéance même, où `renew()` le refuse (`deadline_passed`) — le
        dernier essai utile était perdu. Le plancher de 50 ms évite le
        martèlement quand la marge s'épuise.
        """
        pas = (self.runner.lease_ttl / 3.0) if self.runner.lease_ttl > 0 else 3.0
        if getattr(self, "renew_failures", 0):
            # Un échec ne repousse pas le bail : la tranche nulle ne doit pas
            # marteler la base. Réessayer au délai court de L72, sans jamais
            # attendre au-delà de l'échéance connue.
            delai = max(0.0, min(float(cap), pas, Reprise.MINIMUM))
            if self.lease_deadline > 0:
                reste = max(0.0, self.lease_deadline - _clock_of(self).time())
                if 0.0 < reste < Reprise.MINIMUM:
                    # #2172 : viser la mi-marge restante, avec un plancher de
                    # 50 ms, pour que l'essai suivant ait un budget d'appel
                    # avant l'échéance au lieu de tomber dessus.
                    delai = min(delai, max(0.05, reste / 2.0))
            return self.wait_timeout(delai)
        if self.lease_deadline > 0:
            reste = self.lease_deadline - _clock_of(self).time() - pas
            pas = min(pas, max(0.0, reste))
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
        `lease_deadline` et l'horloge du worker (`clock.time`, simulable en
        test), n'attend que sur un Event (borné par `WATCHDOG_TICK`), puis
        pose `lease_lost` et envoie le SIGKILL.

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
            reste = deadline - _clock_of(self).time()
            if reste > 0:
                # Réveil au plus tard à l'échéance ; relecture régulière pour
                # voir une échéance repoussée (renouvellement) ou modifiée.
                self.watchdog_stop.wait(min(reste, self.WATCHDOG_TICK))
                continue
            if self.watchdog_stop.is_set():
                return
            if self.lease_deadline > _clock_of(self).time():
                continue  # bail renouvelé entre-temps : rien à faire
            self.lease_lost.set()
            self.stop_group_now("lease deadline reached", grace=0, hard=True)
            return

    def _heartbeat(self, stop_event: threading.Event) -> None:
        self.ensure_watchdog()
        # #2483 : le battement doit renouveler AVANT l'échéance. Un plancher de
        # 3 s faisait dormir un bail de 3 s jusqu'à l'échéance même : le
        # watchdog tuait alors un harnais pas encore publié sous charge CI
        # (run 39133881211). Le plancher ne sert plus qu'à éviter un pas nul.
        interval = max(0.05, self.runner.lease_ttl / 3.0)
        # L72 : après un échec, nouvel essai rapide (2 s, 4 s… jusqu'à
        # l'intervalle normal) — une base revenue doit prolonger le bail avant
        # son échéance, pas un tiers de bail plus tard. Pour un bail court, la
        # reprise ne peut pas attendre le minimum de 2 s : elle serait après
        # l'échéance (#2483).
        reprise = Reprise(maximum=interval, minimum=min(Reprise.MINIMUM, interval))
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

    def _idle_pilot(self) -> str:
        """#3273 : le pilote à qui l'agent au repos dit qu'il n'a rien à faire.

        Le premier pilote d'un projet de l'agent, projet primaire d'abord
        (`alert_briefs.project_pilots`, la règle des briefs), jamais l'agent
        lui-même. Agent sans projet,
        registre illisible ou aucun pilote : "" — la relance ne fait alors
        écrire personne (jamais un pilote d'une autre équipe par défaut)."""
        try:
            from . import alert_briefs, projects as projects_mod
            listing = registry.overview(self.db)
            row = next((r for r in listing if r.get("name") == self.name),
                       self.agent or {})
            by_name = {r["name"]: r for r in listing}
            for project in projects_mod.agent_projects(row or {}):
                for pilot in alert_briefs.project_pilots(project, listing,
                                                         cfg=self.cfg, db=self.db):
                    pilot_row = by_name.get(pilot) or {}
                    # project_pilots se replie sur tous les pilotes pour une
                    # base rouge ; une relance au repos reste dans le projet.
                    matches = (project in projects_mod.agent_projects(pilot_row)
                               or project == (pilot_row.get("workstream")
                                              or pilot_row.get("chantier") or ""))
                    if pilot and pilot != self.name and matches:
                        return pilot
        except Exception:  # noqa: BLE001 - une relance ne casse jamais le choix du tour
            return ""
        return ""

    def _moved_before_turn(self, spec: dict) -> bool:
        """L147b : `_placed_before_turn`, et #2614 : un tour qui ne part pas
        d'ici (agent placé ailleurs, ou base tombée pendant le contrôle —
        `placed_elsewhere` lit le registre) oublie le réveil programmé
        réclamé pour lui. Sinon le claim resterait tenu, renouvelé sans fin,
        et le réveil ne serait plus jamais délivré."""
        parti = True
        try:
            parti = self._placed_before_turn(spec)
            return parti
        finally:
            if parti:
                self._forget_wake_claim()

    def _placed_before_turn(self, spec: dict) -> bool:
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

    def admission_withdrawn(self) -> str:
        """#3925 : pourquoi cet agent n'est plus admis sur cet exécuteur ('' s'il
        l'est) : retiré de la liste d'admission de l'exécuteur, ou, gouverné
        par le canon, sa fiche ou son placement retirés (`placement_ok` faux
        après `canon sync`). Un canon momentanément invalide n'est pas un
        retrait (admission seule, comme `registry.wakeable`) ; un rôle élu a
        sa propre relève (EP2) ; une lecture impossible ne retire rien."""
        flt = getattr(self.runner, "agents_filter", None)
        if flt is not None and self.name not in flt:
            return "no longer in the runner's admission list"
        if getattr(self.runner, "mediated", False):
            return ""  # médié : le serveur juge l'admission à chaque bail
        try:
            row = storage.of(self.db).agents.wake_check(self.name)
        except NotImplementedError:
            return ""
        if not row:
            return ""
        if row.get("retired_ts"):
            return "retired from the registry"
        if (row.get("canon_governed") and not row.get("placement_admitted")
                and row.get("releve") != "election"):
            return "Agent card or Placement withdrawn from the canon (%s)" % (
                row.get("placement_diagnostic") or "placement not admitted")
        return ""

    def peek(self) -> bool:
        agent = registry.get(self.db, self.name)
        if agent is None or agent.get("status") == "stopped":
            return False
        if agent.get("pending_prompt"):
            return True
        # L125 : un courrier passif (accusé, copie, diffusion) n'ouvre pas de tour
        if mail.unread_active(self.db, self.name, limit=1):
            return True
        return self.retry_due() or self.idle_due()

    def _servir(self, ids) -> None:
        """#2810 : publie les messages que le tour choisi/en cours sert déjà.

        L'écriture et la lecture par `request_preempt` partagent
        `_preempt_lock` : un NOTIFY ne peut pas se glisser entre « ce tour
        sert ce message » et la décision de le préempter.
        """
        noyau = frozenset(int(i) for i in (ids or ()) if i is not None)
        verrou = getattr(self, "_preempt_lock", None)
        if verrou is None:  # construction partielle (`__new__` des tests)
            self._turn_mail_ids = noyau
            return
        with verrou:
            self._turn_mail_ids = noyau

    def serves_mail(self, message_id: int) -> bool:
        """#2810 : le tour choisi/en cours sert-il déjà ce message ?

        Lecture sous le verrou de préemption : la réponse ne peut pas être
        démentie par un `_servir` concurrent avant la décision de l'appelant.
        """
        verrou = getattr(self, "_preempt_lock", None)
        if verrou is None:
            return int(message_id) in getattr(self, "_turn_mail_ids", frozenset())
        with verrou:
            return int(message_id) in self._turn_mail_ids

    def request_preempt(self, message: int | dict | None = None) -> bool:
        """Un message prioritaire préempte le tour en cours (0018, R19).

        Le tour est arrêté (SIGTERM bref puis SIGKILL), sa consigne repart en
        attente, et `pick()` sert le message prioritaire en tête au tour suivant,
        sur la même session.

        L77 : pour un tour **résident**, la ligne (`message`, dict) est remise
        au point sûr (`steer`) si le pilote l'accepte ; sinon le tour résident
        est interrompu sans tuer le process. Le message non remis est servi au
        tour suivant, sur la même session. Un id seul (NOTIFY) suffit à
        interrompre, sans contenu à steerer.

        #2729 : la génération et le drapeau sont posés sous le même verrou que
        la consommation du début de tour — une demande ne peut donc pas tomber
        entre le test de génération et le `clear()`.

        #2810 : le NOTIFY d'un message que ce tour sert DÉJÀ (`message_id`
        dans `_turn_mail_ids`, dispatch traité après `_pick` ou après la
        remise mi-tour) n'est pas une demande : il n'arrête pas son propre
        tour. Rend True si la demande a été posée, False si elle était déjà
        servie.
        """
        row = message if isinstance(message, dict) else None
        try:
            message_id = int(row.get("id")) if row is not None else (
                int(message) if message is not None else None)
        except (TypeError, ValueError):
            message_id = None
        with self._preempt_lock:
            if message_id is not None and message_id in self._turn_mail_ids:
                return False
            self.preempt_gen += 1
            with self.lock:
                self._preempt_row = row
                self._preempt_seq += 1
            self.preempting.set()
        self.wake.set()
        return True

    def _interrupt_admitted(self, rp, driver, generation: int) -> bool:
        """Interrompt le résident si c'est encore le tour de `generation`.

        `_steer_lock` reste tenu de la revalidation à l'admission de
        l'interruption : la génération suivante ne peut pas être publiée entre
        les deux, donc un repli de steer tardif n'interrompt jamais le tour
        suivant. Un verrou non obtenu dans la borne rend False : le message
        reste en file plutôt que d'interrompre un tour inconnu.
        """
        if not self._steer_lock.acquire(timeout=PREEMPT_STEER_WAIT_S):
            log_async("[%s] interrupt admission barrier not reached within %.0fs"
                      % (self.name, PREEMPT_STEER_WAIT_S))
            return False
        try:
            with self.lock:
                if (not self._turn_live or self._turn_generation != generation
                        or self.resident is not rp):
                    return False
            try:
                return bool(driver.interrupt())
            except Exception as exc:
                log_async("[%s] resident interrupt failed (%s)"
                          % (self.name, " ".join(str(exc).split())[:120]))
                return False
        finally:
            self._steer_lock.release()

    def _preempt_resident(self, generation: int | None = None) -> str:
        """L77 : préempte un tour résident sans tuer le process.

        Rang 1 humain : `steer` au point sûr, la remise restant réservée jusqu'à
        la fin effective du tour (B8) ; rang 3 agent/système : `interrupt()`
        seulement, jamais `steer` (étude §2). Rend "" quand aucun résident ne
        porte le tour (chemin 0018 « un tour par événement »), sinon
        "steered", "interrupted" ou "deferred" — le moniteur continue après un
        steer et s'arrête après une interruption.
        """
        with self.lock:
            rp = self.resident
        if rp is None or not getattr(rp, "alive", False) \
                or getattr(rp, "closing", False):
            return ""
        driver = getattr(rp, "driver", None)
        if driver is None:
            # Un résident vivant sans pilote reste vivant : le message attend
            # le tour suivant, jamais un `stop_group_now` de préemption (B5).
            log_async("[%s] preemption: resident alive without driver; message "
                      "kept for the next turn" % self.name)
            return "deferred"
        if not getattr(driver, "busy", False):
            # Course avant `send`/après la fin du tour : le résident est vivant,
            # le message reste en file ; on ne le tue pas pour préempter.
            log_async("[%s] preemption: resident alive between turns; message "
                      "kept for the next turn" % self.name)
            return "deferred"
        with self.lock:
            if generation is None:
                generation = getattr(self, "_turn_generation", 0)
            row = self._preempt_row
            seq = self._preempt_seq
            self._preempt_row = None
        if row is not None and self._from_human(row):
            if self._steer_human(row, driver, generation):
                # B10 : ne retombe le latch que si aucune nouvelle demande n'est
                # arrivée pendant le steer ; sinon le moniteur doit la servir.
                with self.lock:
                    if self._preempt_seq == seq:
                        self.preempting.clear()
                return "steered"
        if self._interrupt_admitted(rp, driver, generation):
            log_async("[%s] resident turn interrupted (preemption)" % self.name)
            return "interrupted"
        # Aucune remise immédiate possible : le message reste en file, le
        # résident vivant est conservé (le tuer pour préempter est exclu).
        log_async("[%s] resident preemption deferred: message kept for the next "
                  "turn" % self.name)
        return "deferred"

    def _steer_human(self, row: dict, driver, generation: int) -> bool:
        """L77/B8 : réserve un message humain et le remet au point sûr.

        La réservation n'est **pas** soldée ici : elle est rattachée au `Turn`
        effectif et à la génération du tour, puis soldée par `_solde_steers` si
        ce `Turn` l'a réellement servi. Le steer ne démarre jamais un nouveau
        tour (`steer_running`) : si le tour s'achève pendant la requête, le
        message reste en file pour le tour suivant. La barrière `_steer_lock`
        couvre la réservation et la requête : `_solde_steers` attend qu'elles
        soient finies, ce qui ferme la course de l'ACK tardif.
        """
        try:
            ident = int(row.get("id"))
        except (KeyError, TypeError, ValueError):
            return False
        from . import resident as resident_mod
        with self._steer_lock:
            with self.lock:
                if not self._turn_live or self._turn_generation != generation:
                    return False
            jeton = mail.new_token()
            try:
                reserves = mail.reserve(
                    self.db, self.name, self.runner.runner_id, self.epoch, jeton,
                    ids=[ident], porteur="consigne",
                    ttl_seconds=mail.human_reservation_ttl(self.turn_max_seconds()))
            except db_mod.DbError as exc:
                log_async("[%s] steer reservation failed (%s)" % (self.name, exc))
                return False
            if not reserves:
                return False
            with self.lock:
                if not self._turn_live or self._turn_generation != generation:
                    # Le tour s'est clos pendant la réservation : ne pas
                    # steerer un tour qui n'existe plus.
                    try:
                        mail.release(self.db, self.name, jeton, [ident])
                    except db_mod.DbError as exc:
                        log_async("[%s] steer reservation not released (%s)"
                                  % (self.name, exc))
                    return False
            try:
                texte = self._urgent_steer_text(row)
                input_id = resident_mod.new_id()
                turn = (driver.steer_running(texte, input_id=input_id)
                        if texte and hasattr(driver, "steer_running") else None)
                if turn is not None:
                    with self.lock:
                        if self._turn_live and self._turn_generation == generation:
                            self._steer_pending.append(
                                (jeton, [ident], generation, turn, input_id))
                            log_async("[%s] priority message steered into the "
                                      "resident turn" % self.name)
                            return True
                    # L77/B8 : ACK accepté après la clôture de la génération :
                    # jamais attribué au tour suivant, la remise est libérée.
                    log_async("[%s] late steer acknowledged after the turn: "
                              "released" % self.name)
            except Exception as exc:  # InputRefused, ResidentBusy, protocole muet
                log_async("[%s] resident steer refused (%s): interrupting"
                          % (self.name, " ".join(str(exc).split())[:120]))
            try:
                mail.release(self.db, self.name, jeton, [ident])
            except db_mod.DbError as exc:
                log_async("[%s] steer reservation not released (%s)"
                          % (self.name, exc))
            return False

    def _begin_turn_generation(self) -> int:
        """L77/B8 : ouvre la génération du tour qui va être servi."""
        with self._steer_lock:
            with self.lock:
                self._turn_generation += 1
                self._turn_live = True
                return self._turn_generation

    def _solde_steers(self, result: bool, generation: int | None = None) -> None:
        """L77/B8 : solde les remises steer sur la fin effective du tour.

        La barrière `_steer_lock` attend le steer en cours (réservation et
        requête) ; la génération rattache chaque remise à son tour, pour qu'un
        ACK tardif ne soit jamais soldé par le tour suivant. Une remise n'est
        soldée que si le `TurnResult` effectif est achevé et contient son
        `input_id` ; sinon elle est libérée (au moins une fois).
        """
        with self.lock:
            if generation is None:
                generation = self._turn_generation
        acquired = self._steer_lock.acquire(timeout=PREEMPT_STEER_WAIT_S)
        try:
            with self.lock:
                pending = self._steer_pending
                self._steer_pending = []
                self._turn_live = False
                self._turn_generation += 1
        finally:
            if acquired:
                self._steer_lock.release()
        if not acquired:
            log_async("[%s] steer barrier not reached within %.0fs: a late "
                      "steer will release itself" % (self.name, PREEMPT_STEER_WAIT_S))
        from . import resident as resident_mod
        for jeton, ids, gen, turn, input_id in pending:
            fin = getattr(turn, "result", None) if turn is not None else None
            served = (gen == generation and fin is not None
                      and getattr(fin, "status", "") == resident_mod.COMPLETED
                      and input_id in (getattr(fin, "inputs", ()) or ()))
            if served:
                try:
                    livres = mail.deliver(self.db, self.name, self.runner.runner_id,
                                          self.epoch, jeton, ids)
                except db_mod.DbError as exc:
                    livres = []
                    log_async("[%s] steered message not settled (%s)"
                              % (self.name, exc))
                self._remise_incertaine(ids, livres)
            else:
                if turn is not None and gen != generation:
                    log_async("[%s] late steer from an earlier turn released"
                              % self.name)
                try:
                    mail.release(self.db, self.name, jeton, ids)
                except db_mod.DbError as exc:
                    log_async("[%s] steer reservation not released (%s)"
                              % (self.name, exc))

    def _urgent_steer_text(self, row: dict) -> str:
        """Le texte remis au point sûr : l'enveloppe ameesh, puis le corps
        neutralisé — jamais un en-tête fourni par l'expéditeur."""
        from . import resident_queue
        ident = resident_queue.identity_for(
            row.get("sender") or "", cfg=getattr(self, "cfg", None),
            canon_id=self._canon_id(),
            proof=mail.human_account(row), human=self._from_human(row))
        return "%s\n%s" % (resident_queue.envelope(ident, self._envelope_token()),
                           resident_queue.neutralise(row.get("body") or ""))

    def _preempt_monitor(self, done: threading.Event) -> None:
        """Arrête le harnais dès qu'un message prioritaire arrive (0018).

        Passe par le point de passage unique : signal d'abord, journal borné
        ensuite ; une préemption arrivée avant la publication du `Popen` est
        rattrapée par `kill_if_stop_requested()`. L77 : un tour résident est
        interrompu à son point sûr, sans tuer le process.
        """
        with self.lock:
            generation = self._turn_generation
        while not done.wait(0.1):
            if self.preempting.is_set():
                status = self._preempt_resident(generation)
                if status == "steered":
                    # Un steer accepté n'arrête pas le tour : le moniteur
                    # continue de veiller sur les urgents suivants et `restart`.
                    continue
                if status:
                    return
                self.stop_group_now("preemption", grace=1.0)
                return
            if self.restarting.is_set():
                # `ameesh restart` (L26) : même point de passage, même grâce
                self.stop_group_now("restart requested", grace=1.0)
                return

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
                                    status_text=self._fenetre_debit(texte, paused=True))
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
        self._context_session_forget()
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

    def session_max_tokens(self) -> float:
        """Seuil de rotation sur la taille (L11) du harnais de la session.

        #4448 : le canon compilé (`session.max_tokens` et
        `session.max_tokens_by_harness`) fait foi, surcharge du harnais puis
        valeur commune ; canon absent ou illisible, ou document sans bloc :
        le seuil commun de l'exécuteur (`AMEESH_SESSION_MAX_TOKENS`), jamais
        une valeur propre à un harnais écrite en dur.
        """
        from . import mechanics
        return mechanics.session_max_tokens(
            self.cfg, self._ledger_harness() or "", self.runner.session_max_tokens)

    def _restore_session_counters(self) -> None:
        """#4448 : reprendre les compteurs de la session depuis le grand livre.

        Un worker relancé (déploiement, redémarrage) repartait de zéro alors
        que la session, elle, continue : la décision de rotation et l'alerte
        `session_too_big` ne mesuraient plus la même chose. La somme et le
        nombre de tours de la session courante sont relus **une fois**, avant
        la première décision. Seul le nombre de tours dit si ce worker a déjà
        compté un tour (réussi ou non, la définition unifiée) : des jetons
        seuls — un tour en cours, une mesure reprise — ne bloquent plus la
        reprise. Une lecture en échec laisse les compteurs à zéro : la mesure
        repart du tour suivant, jamais une valeur supposée.
        """
        if self.session_turns:
            return
        session = self.current_session()
        if not session:
            return
        try:
            total = storage.of(self.db).turn_costs.session_total(self.name, session)
        except Exception:  # noqa: BLE001 — mesure seulement, jamais fatale au tour
            return
        if not total:
            return
        self.session_turns = int(total.get("turns") or 0)
        self.session_tokens = float(total.get("tokens") or 0)

    def rotation_due(self) -> bool:
        """Rotation due ? Jamais pendant un tour, jamais avant le minimum.

        Politique `jamais` : aucune rotation ; `taille` et `par-lot` : la
        rotation sur la taille (L11) reste le garde-fou."""
        if self.proc is not None:
            return False
        if self.session_policy() == "jamais":
            return False
        self._restore_session_counters()
        if self.session_turns < self.runner.session_min_turns:
            return False
        # #706 : la latence d'un tour autorisé ne suffit pas à oublier la
        # session. Le plancher suit aussi un plafond propre à l'agent ; le
        # facteur 2 laisse une marge pour atteindre le point sûr après la
        # borne du tour. Sans plafond, garder le seuil de latence configuré.
        latency_threshold = max(self.runner.session_max_turn_seconds,
                                2 * self.turn_max_seconds())
        return (self.session_tokens >= self.session_max_tokens()
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

    def turn_close_due(self, started: float, context: int,
                       now: float | None = None) -> str | None:
        """Le tour en cours doit-il se clore au prochain point sûr ? (L105)

        Deux plafonds : le contexte du dernier appel au modèle du fil
        principal — la taille de la session, mesure du plafond de contexte
        L60 (#2336) — et la durée du tour. La somme des relectures du tour
        n'est pas une taille de contexte : elle croît avec le nombre
        d'étapes, quelle que soit la session. Rend la raison, ou None."""
        plafond = self.context_max_tokens()
        if plafond > 0 and context >= plafond and self.session_policy() != "jamais":
            return "context cap: %d tokens in context (cap %d)" % (
                context, plafond)
        duree_max = self.turn_max_seconds()
        now = time.time() if now is None else now
        # L159f : le temps gelé par la garde de l'hôte ne compte pas
        ecoule = now - started - self.runner.gate_frozen_between(started, now)
        if duree_max > 0 and ecoule >= duree_max:
            return "maximum turn duration: %ds (cap %ds)" % (int(ecoule), duree_max)
        return None

    def reread_budget_due(self, live: dict) -> dict | None:
        """CTX-04a : un budget de relecture opt-in est-il dépassé ? (§5.1, 4)

        Rend l'observation de la clôture (raison, mesure, budget), ou None :
        budget éteint, ou mesure ``None`` — aucune décision sans mesure. Après
        la clôture, le tour suivant repart à ``R_turn`` nul ; ``R_session``,
        lui, reste acquis dans la même session : son budget se lit par tranche
        franchie pendant ce tour, sinon chaque tour suivant se clorait dès son
        premier point sûr. Jamais lu comme une taille de fenêtre ni comparé à
        ``context_max_tokens``.
        """
        budget_tour = int(self.cfg.turn_reread_budget)
        relu_tour = live.get("R_turn")
        if budget_tour > 0 and relu_tour is not None and relu_tour >= budget_tour:
            return {"kind": REREAD_BUDGET_CLOSE, "measure": "R_turn",
                    "value": relu_tour, "budget": budget_tour,
                    "reason": "reread budget: R_turn %d tokens (budget %d)"
                              % (relu_tour, budget_tour)}
        budget_session = int(self.cfg.session_reread_budget)
        relu_session, base = live.get("R_session"), live.get("base")
        if budget_session > 0 and relu_session is not None and base is not None \
                and relu_session // budget_session > int(base) // budget_session:
            return {"kind": REREAD_BUDGET_CLOSE, "measure": "R_session",
                    "value": relu_session, "budget": budget_session,
                    "reason": "reread budget: R_session %d tokens (budget %d)"
                              % (relu_session, budget_session)}
        return None

    def last_turn_context_reread(self) -> tuple[int, str | None]:
        """Contexte du dernier appel au dernier tour, et sa session (L217b) :
        le fil principal relevé dans le flux, sans les sous-agents, ni pour un
        tour clos avant son `result` — pour Codex, dans le journal de session
        du fil (#2932) ; sans relevé, les jetons relus par le dernier tour
        inscrit au grand livre (L60)."""
        if self.last_turn_context is not None:
            return self.last_turn_context, self.last_turn_context_session
        return self.last_turn_reread, self.last_turn_reread_session

    def _context_session_baseline(self, session: str | None) -> int | None:
        """CTX-02b : ``R_session`` des tours précédents, pour le normalisateur.

        Une session neuve part de zéro ; une session reprise relit le grand
        livre **une fois** (puis la base est tenue à jour par l'observation).
        La somme legacy ne prouve pas la complétude : elle n'est ``measured``
        que si le dernier ``turn.context`` publié porte la même valeur et
        qu'aucun tour n'a été inscrit depuis ; sinon la base reste inconnue,
        avec sa raison (#4870, B cx3). Une base partielle garde sa qualité et
        ses raisons ; une collecte perdue la rend inconnue jusqu'à une session
        neuve explicite (B cx1). Observation seule : aucune décision ne lit
        cette valeur.
        """
        if not session:
            if self._context_session_lost is not None:
                # La collecte perdue peut appartenir à la session qui s'ouvre :
                # aucune base mesurée n'est supposée.
                self._context_baseline = None
                self._context_baseline_quality = context_metrics.UNKNOWN
                self._context_baseline_reasons = (self._context_session_lost,)
                return None
            self._context_session_reread = 0
            self._context_session_quality = context_metrics.MEASURED
            self._context_session_reasons = ()
            self._context_baseline = 0
            self._context_baseline_quality = context_metrics.MEASURED
            self._context_baseline_reasons = ()
            return 0
        if self._context_session_lost is not None:
            if self._context_session_id in (None, session):
                self._context_session_id = session
                self._context_baseline = None
                self._context_baseline_quality = context_metrics.UNKNOWN
                self._context_baseline_reasons = (self._context_session_lost,)
                return None
            # La perte concernait une autre session : celle-ci repart propre.
            self._context_session_lost = None
        if self._context_session_id == session:
            self._context_baseline = self._context_session_reread
            self._context_baseline_quality = self._context_session_quality
            self._context_baseline_reasons = self._context_session_reasons
            return self._context_session_reread
        info = cost_mod.session_reread(self.db, self.name, session)
        if info is None:
            value, quality, reasons = (
                None, context_metrics.UNKNOWN,
                (context_metrics.REASON_SESSION_BASELINE_UNKNOWN,))
        else:
            value, quality, reasons = self._context_resume_state(session, info)
        self._context_session_id = session
        self._context_session_reread = value
        self._context_session_quality = quality
        self._context_session_reasons = reasons
        self._context_baseline = value
        self._context_baseline_quality = quality
        self._context_baseline_reasons = reasons
        return value

    def _context_resume_state(
            self, session: str, info: dict) -> tuple[int | None, str, tuple[str, ...]]:
        """CTX-02b : qualité durable d'une base ``R_session`` reprise.

        Le grand livre ne dit pas si les tours précédents étaient complets :
        un tour partiel, un ``R_turn`` absent ou une publication perdue y
        laissent les mêmes compteurs legacy. La preuve est le dernier
        ``turn.context`` publié pour cette session : il porte la valeur, la
        qualité et les raisons de la base, et son instant dit si un tour a été
        inscrit depuis. Sans événement, valeur divergente ou tour inscrit
        depuis, la base reste inconnue (#4870, B cx3) — jamais un total
        supposé.
        """
        try:
            rows = storage.of(self.db).events.recent_type(
                cost_mod.CONTEXT_EVENT, CONTEXT_RESUME_DAYS,
                CONTEXT_RESUME_EVENTS, object="agent:" + self.name)
        except Exception as exc:  # noqa: BLE001 — mesure seulement
            log_async("[%s] context resume proof not read (%s)"
                      % (self.name, " ".join(str(exc).split())[:160]))
            rows = ()
        for row in rows or ():
            data = row.get("data") or {}
            if data.get("session") != session:
                continue
            # Un tour inscrit depuis la publication est un trou : sa mesure
            # n'est pas dans l'événement, la somme legacy ne la remplace pas.
            if float(row.get("ts") or 0.0) < float(info.get("last_ts") or 0.0):
                break
            measure = (((data.get("context") or {}).get("measures") or {})
                       .get("R_session") or {})
            value = measure.get("value")
            if value is None or int(value) != int(info.get("tokens") or 0):
                break
            quality = measure.get("quality")
            if quality not in (context_metrics.MEASURED,
                               context_metrics.DERIVED,
                               context_metrics.PARTIAL):
                break
            return int(value), quality, tuple(measure.get("reasons") or ())
        return (None, context_metrics.UNKNOWN,
                (context_metrics.REASON_SESSION_BASELINE_UNKNOWN,))

    def _context_session_forget(self) -> None:
        """Oublie la base ``R_session`` : session neuve explicite (rotation,
        redémarrage, élection). Un trou d'une ancienne session ne contamine
        jamais la nouvelle."""
        self._context_session_id = None
        self._context_session_reread = None
        self._context_session_quality = context_metrics.UNKNOWN
        self._context_session_reasons = ()
        self._context_baseline = None
        self._context_baseline_quality = context_metrics.UNKNOWN
        self._context_baseline_reasons = ()
        self._context_session_lost = None

    def _invalidate_context_session(self, session: str | None,
                                    reasons) -> None:
        """Marque la base d'une session inconnue après un trou de collecte.

        La valeur d'avant n'est pas conservée comme si la session était
        complète : le cumul est inconnu, avec ses raisons. Sans session connue,
        la prochaine base reste inconnue jusqu'à une session neuve explicite.
        """
        self._context_session_id = session or None
        self._context_session_reread = None
        self._context_session_quality = context_metrics.UNKNOWN
        self._context_session_reasons = _reasons_uniques(reasons)
        if not session:
            self._context_session_lost = (self._context_session_reasons
                                          or (context_metrics.REASON_COLLECTION_LOST,))[0]
        else:
            self._context_session_lost = None

    def _remember_context_session(self, document: dict, session: str | None) -> None:
        """Reporte le ``R_turn`` publié sur la base ``R_session`` de la session.

        Le normalisateur a déjà ajouté ce tour à sa propre base ; on tient la
        même valeur pour le tour suivant **sans relire le grand livre**. La
        qualité et les raisons de la base sont conservées : une base partielle
        ne devient jamais ``measured`` parce que le tour courant est complet.
        Un ``R_turn`` absent (ou un relevé d'une autre session) **invalide** la
        base : un trou ne se comble pas par une somme supposée.
        """
        if not session:
            return
        reasons = tuple(document.get("reasons") or ())
        measures = document.get("measures")
        measure = measures.get("R_turn") if isinstance(measures, dict) else None
        if document.get("session") != session \
                or context_metrics.REASON_SESSION_MISMATCH in reasons \
                or not isinstance(measure, dict):
            self._invalidate_context_session(
                session, (context_metrics.REASON_SESSION_MISMATCH,))
            return
        value = measure.get("value")
        turn_reasons = tuple(measure.get("reasons") or ())
        if value is None:
            self._invalidate_context_session(
                session, _reasons_uniques(
                    (context_metrics.REASON_TURN_NOT_MEASURED,), turn_reasons))
            return
        if self._context_baseline is None:
            # Un trou antérieur : la somme de ce tour ne le reconstruit pas.
            self._context_session_id = session
            self._context_session_reread = None
            self._context_session_quality = context_metrics.UNKNOWN
            self._context_session_reasons = _reasons_uniques(
                self._context_baseline_reasons, turn_reasons)
            return
        quality = context_metrics.MEASURED if (
            self._context_baseline_quality in (context_metrics.MEASURED,
                                               context_metrics.DERIVED)
            and measure.get("quality") in (context_metrics.MEASURED,
                                           context_metrics.DERIVED)) \
            else context_metrics.PARTIAL
        self._context_session_id = session
        self._context_session_reread = int(self._context_baseline) + int(value)
        self._context_session_quality = quality
        self._context_session_reasons = _reasons_uniques(
            self._context_baseline_reasons, turn_reasons)

    def _codex_last_call_context(self, thread: str | None) -> int | None:
        """#2932 : le contexte du dernier appel d'un fil Codex, lu dans son
        journal de session (dossiers des comptes déclarés compris, L95), ou
        None s'il est illisible."""
        try:
            book = cost_mod.CostBook(
                state_dir=self.cfg.state_dir,
                codex_homes=accounts_mod.homes(self.cfg, "codex"))
            usage = book.codex_last_call_usage(thread)
        except Exception as exc:  # mesure seulement : jamais fatale au tour
            log_async("[%s] codex call context not read (%s)"
                      % (self.name, " ".join(str(exc).split())[:160]))
            return None
        return adapters.context_tokens("codex", usage) if usage is not None else None

    def _observer_journal_codex(self, normalizer, session: str | None,
                                started: float, ended: float) -> None:
        """#5148 : complète ``C_last``/``O_last`` du tour Codex au journal.

        ``codex exec --json`` ne publie pas d'usage d'appel ; le journal de
        session du fil porte le ``last_token_usage`` du dernier appel. On ne
        l'attribue qu'au tour dont le ``task_started`` tombe dans la fenêtre
        du tour : un journal plus ancien (lancement raté, reprise) laisse
        ``C_last`` inconnu avec son motif, jamais l'usage d'un tour précédent.
        Observation seule : ``R_turn`` et les caches cumulés restent au flux ;
        aucune erreur n'est fatale au tour.
        """
        reason: str | None = context_metrics.REASON_JOURNAL_UNREADABLE
        try:
            book = cost_mod.CostBook(
                state_dir=self.cfg.state_dir,
                codex_homes=accounts_mod.homes(self.cfg, "codex"))
            result = book.codex_turn_last_call_usage(session, started, ended)
            usage = result.get("usage") if isinstance(result, dict) else None
            if isinstance(usage, dict) and usage:
                normalizer.journal_call(usage)
                return
            reason = (result.get("reason") if isinstance(result, dict) else None) \
                or context_metrics.REASON_JOURNAL_MISSING
        except Exception as exc:  # mesure seulement : jamais fatale au tour
            log_async("[%s] codex journal observation not read (%s)"
                      % (self.name, " ".join(str(exc).split())[:160]))
        try:
            normalizer.journal_unavailable(reason)
        except Exception as exc:  # noqa: BLE001 — observation seule
            log_async("[%s] codex journal reason not published (%s)"
                      % (self.name, " ".join(str(exc).split())[:160]))

    def context_rotation_due(self) -> bool:
        """Le contexte de la session dépasse-t-il le plafond ? (L60)

        La mesure est celle du plafond du tour (L217b) : le contexte du
        dernier appel du fil principal, relevé dans le flux, ou pour Codex,
        qui ne publie son usage qu'en fin de tour, dans le journal de session
        du fil (#2932) ; à défaut, la somme des relectures du dernier tour
        inscrit au grand livre (`last_turn_reread`), sauf pour Codex : sans
        relevé d'appel (journal illisible, worker relancé), il ne tourne pas
        sur cette somme (#2932). Chaque étape d'un tour relit tout le
        contexte : une session qui grossit coûte de plus en plus cher à
        chaque tour, et l'alerte seule n'y changeait rien. La somme des
        appels d'un tour n'est pas la taille de la session (#2336) ; l'alerte
        `session_too_big` lit, elle, `S_hist` de la session (#4448).
        Ni minimum de tours, ni rotation pendant un tour ; la politique
        `jamais` s'en dispense ; il faut une session courante, et que le
        relevé soit bien celui de cette session.
        """
        if self.proc is not None or self.session_policy() == "jamais":
            return False
        if self.last_turn_context is None and self._ledger_harness() == "codex":
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

    def _resume_keep(self, resume: str, origin: str, *,
                     continuation: bool = False) -> bool:
        """Le résumé de reprise retenu pour la session neuve : en mémoire et
        dans l'état local, AVANT l'oubli de l'ancienne session — une panne
        entre l'oubli et le lancement ne le perd pas (#389 r1). Faux s'il
        n'a pas pu être écrit : l'appelant garde alors l'ancienne session
        (#389 r2).

        `continuation` (#2619) : le brief `ameesh` continue le travail de la
        session perdue (elle est inconnue du harnais, après un résumé non
        produit) ; il est gardé au `$TMPDIR` comme une suite de rotation."""
        path = self._path(self.RESUME_PENDING)
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "resume": resume, "origin": origin,
                           "continuation": bool(continuation)}, fh,
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
        self.resume_continuation = bool(continuation)
        return True

    def _resume_drop(self) -> None:
        """Le résumé de reprise a ouvert la session neuve, ou n'a plus lieu
        d'être : oublié, en mémoire et sur disque."""
        self.resume_summary = ""
        self.resume_continuation = False
        self._election_brief = ""
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
        self.resume_continuation = bool(pending.get("continuation"))
        log_async("[%s] pending resume summary recovered (%d characters)"
                  % (self.name, len(resume)))

    def adopt_election(self, lease: dict, brief: str | None = None) -> bool:
        """L165 (EP3) : ce worker vient de gagner une relève croisée.

        Aucune session ne traverse un hôte : la remise à zéro DURABLE a été
        écrite par `claim` ; ici on jette l'état local périmé de CET hôte
        (fichier de session et résumé en attente d'une tenure précédente),
        puis on garde le briefing persisté comme résumé de reprise de la
        session neuve. Le brief est relu de la ligne (`election_brief`) pour
        survivre à un exécuteur mort entre la réclamation et le premier tour ;
        il est oublié avec la session (`set_session`, `clear_session`).
        """
        lease = lease or {}
        brief = (brief if brief is not None else lease.get("election_brief")) or ""
        previous = lease.get("previous_host") or (self.agent or {}).get("previous_host")
        # L'état local de l'hôte est celui d'une tenure précédente, jamais
        # celui de l'ancien détenteur : il ne doit pas rouvrir une session.
        self.agent = dict(self.agent, session_id=None, session_account=None,
                          session_harness=None, session_cwd=None,
                          session_origin=None,
                          session_reset_ts=lease.get("session_reset_ts"),
                          lease_owner=self.runner.runner_id,
                          lease_epoch=lease.get("lease_epoch"),
                          status="queued")
        try:
            os.unlink(self._path("session"))
        except OSError:
            pass
        self._resume_drop()
        self.session_turns = 0
        self.session_tokens = 0.0
        self._context_session_forget()
        if not brief.strip():
            log_async("[%s] elected on %s: no persisted resume brief; new session "
                      "opens without one" % (self.name, self.runner.host))
            return False
        self._election_brief = brief
        if not self._resume_keep(brief, "ameesh"):
            # #389 r3 : au moins en mémoire, le brief ouvre le tour
            self.resume_summary, self.resume_origin = brief, "ameesh"
            self.resume_continuation = False
        log_async("[%s] elected on %s (epoch %s, previous %s/%s epoch %s): new "
                  "session on the resume brief (%d characters)"
                  % (self.name, self.runner.host, lease.get("lease_epoch"),
                     previous or "?", lease.get("previous_owner") or "?",
                     lease.get("previous_epoch"), len(brief)))
        self._fil_note(
            "Elected on %s (epoch %s): previous holder %s (%s, epoch %s). Its "
            "session %s stays there; this host opens a new session on a "
            "deterministic resume brief (%d characters)."
            % (self.runner.host, lease.get("lease_epoch"), previous or "?",
               lease.get("previous_owner") or "?",
               lease.get("previous_epoch"),
               lease.get("previous_session") or "none", len(brief)),
            meta={"action": "election-brief",
                  "previous_host": previous or "",
                  "previous_epoch": lease.get("previous_epoch"),
                  "session": lease.get("previous_session") or ""})
        return True

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
        # L81 (étude §6) : l'entretien d'une session résidente se décide entre
        # deux tours, sur la mesure enregistrée au dernier tour.
        entretien = self._resident_maintenance()
        if entretien and self._resident_maintain(entretien):
            return True
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
            return self._rotate("context cap: %d tokens in context "
                                "(cap %d)" % (self.last_turn_context_reread()[0],
                                              self.context_max_tokens()))
        if not self.rotation_due():
            return False
        return self._rotate("turns=%d, tokens=%.0f, last turn=%.0fs" % (
            self.session_turns, self.session_tokens, self.last_turn_seconds))

    def _resident_maintenance(self) -> str:
        """L81 : ce que la session résidente demande entre deux tours
        (`resident_runner.maintenance`), '' si elle peut continuer."""
        from . import resident_runner
        with self.lock:
            rp = self.resident
        if rp is None:
            return ""
        # L81 (étude §6.3) : l'entretien se décide AVANT le `pick` — un message
        # humain en attente, ou remis par le hook pendant le tour qui vient de
        # finir, doit tenir la fenêtre de calme dès cette décision.
        self._humain_observe()
        rotate = self.session_policy() != "jamais"
        # CTX-04b : en opt-in, la projection remplace le 50 % legacy ; le plafond
        # dur reste jugé avant elle (§5.1).
        fenetre = self.cfg.window_maintenance_share > 0
        entretien = rp.maintenance_due(
            cap=self.context_max_tokens() if rotate else 0,
            reread_max=resident_runner.reread_max(
                self._state_read(resident_runner.REREAD_MAX_SETTING)) if rotate else 0,
            **({"share": 0.0} if fenetre else {}))
        if not fenetre or entretien == resident_runner.ROTATE:
            return entretien
        projection = self._window_decision(rp)
        if projection["near"] is False:
            # une mesure retombée sous le seuil rouvre droit à une compaction
            rp.window_compact_tried = False
        return projection["decision"] or entretien

    def _window_decision(self, rp) -> dict:
        """CTX-04b : la projection de fenêtre de la session résidente `rp`
        (None : pas de résident, donc `W` inconnue), et sa décision. La politique
        `jamais` garde le compactage mais pas le renouvellement."""
        from . import resident_runner
        mesure = self._window_measure or {}
        if rp is not None and mesure.get("session") != rp.session:
            mesure = {}
        projection = resident_runner.window_maintenance(
            c_last=mesure.get("C_last"), o_last=mesure.get("O_last"),
            window=rp.profile_window if rp is not None else None,
            share=self.cfg.window_maintenance_share,
            input_reserve=self.cfg.window_input_reserve,
            output_reserve=self.cfg.window_output_reserve,
            compact_supported=(self.turn_harness()
                               in resident_runner.NATIVE_COMPACT_HARNESSES),
            compact_tried=rp.window_compact_tried if rp is not None else False)
        if projection["decision"] == resident_runner.WINDOW_RENEW \
                and self.session_policy() == "jamais":
            projection["decision"] = ""
        return projection

    def _window_measure_keep(self, document: dict, compacted: bool) -> None:
        """CTX-04b : garde les mesures du fil principal d'un tour pour la
        projection. Une mesure absente, de repli ou partielle (tour interrompu,
        borne d'appels, autre session, cache non qualifié) reste inconnue : la
        projection serait incomplète. Un tour qui a compacté n'en donne aucune :
        son dernier appel précède la frontière."""
        if compacted:
            self._window_measure = None
            return
        fiables = (context_metrics.MEASURED, context_metrics.DERIVED)

        def valeur(nom: str) -> int | None:
            mesure = (document.get("measures") or {}).get(nom) or {}
            return mesure.get("value") if mesure.get("quality") in fiables else None

        self._window_measure = {"session": document.get("session"),
                                "C_last": valeur("C_last"),
                                "O_last": valeur("O_last")}

    def _humain_observe(self) -> None:
        """L81 (étude §6.3) : rafraîchit la fenêtre de calme avant la décision
        d'entretien. Deux sources : le courrier humain NON ENCORE REMIS (le
        `_mail_spec` ne le voit qu'au `pick`, après l'entretien), et celui
        remis par le hook PENDANT le tour — déjà lu, mais daté dans l'état du
        tour (`cli.tour_lu`)."""
        try:
            rows = mail.unread_active(self.db, self.name)
        except db_mod.DbError:
            rows = []           # la décision d'entretien a ses propres gardes
        if any(self._from_human(m) for m in rows):
            self._dernier_humain = time.monotonic()
            return
        from . import cli as cli_mod
        ts = float(cli_mod.tour_lu(self.cfg, self.name).get("humain") or 0.0)
        if not ts:
            return
        marque = time.monotonic() - max(0.0, time.time() - ts)
        if marque > 0:
            self._dernier_humain = max(self._dernier_humain, marque)

    def _humain_recent(self, quiet: float | None = None) -> bool:
        """L81 (étude §6.3) : un message humain en attente depuis moins de
        `quiet` secondes interdit un entretien non urgent (le seuil de relu
        cumulé, la compaction) ; le dépassement dur, lui, passe outre."""
        from . import resident_runner
        delai = resident_runner.QUIET_S if quiet is None else quiet
        return bool(self._dernier_humain) and \
            time.monotonic() - self._dernier_humain < delai

    def _resident_reread_load(self, session: str | None) -> int:
        """L81 (étude §6.1) : relu cumulé déjà mesuré pour cette session."""
        return int(self._resident_reread.get(session or "", 0))

    def _resident_reread_keep(self, rp) -> None:
        """L81 (étude §6.1) : enregistre le cumul de la session du processus
        après chaque tour mesuré, pour celui qui la reprendra."""
        session = rp.session or ""
        if session:
            self._resident_reread[session] = rp.session_reread

    def _resident_maintain(self, decision: str) -> bool:
        """L81 (étude §6.2/§6.3) : exécute la décision d'entretien d'une
        session résidente — la rotation (quand calme pour le seuil de relu
        cumulé, sans attendre pour le plafond dur), la compaction native
        (quand calme). Rend True si l'entretien a eu lieu : pour une rotation
        le processus est alors fermé et le prochain tour en ouvre un neuf sur
        le résumé (étude §6.3)."""
        from . import resident_runner
        with self.lock:
            rp = self.resident
        if rp is None:
            return False
        if decision == resident_runner.ROTATE:
            # #2336 : le contexte du dernier appel (taille de session), ou le
            # relu du tour quand le harnais ne le donne pas.
            mesure = rp.last_context if rp.last_context is not None else rp.last_reread
            raison = ("context cap: %d tokens in context (cap %d)"
                      % (mesure, self.context_max_tokens()))
        elif decision == resident_runner.ALERT:
            if self._humain_recent():
                return False
            raison = ("resident session re-read: %d tokens (threshold %.0f)"
                      % (rp.session_reread, resident_runner.reread_max(
                          self._state_read(resident_runner.REREAD_MAX_SETTING))))
        elif decision == resident_runner.WINDOW_RENEW:
            if self._humain_recent():
                return False
            projection = self._window_decision(rp)
            raison = ("window maintenance: C_next %s tokens (W %s) after a native "
                      "compaction" % (projection["C_next"], projection["W"]))
        elif decision == resident_runner.COMPACT:
            if self._humain_recent():
                return False
            if not self._rotation_allowed():
                return False
            return self._native_compact(rp)
        else:
            return False
        if not self._rotation_allowed():
            return False
        if not self._rotate(raison):
            return False
        self._close_resident("rotated: %s" % raison)
        return True

    def _native_compact(self, rp) -> bool:
        """L81 (étude §6.2) : compaction native du résident — `/compact` servi
        comme un message entre deux tours, quand calme. Le harnais rapporte
        alors une frontière (`compact_boundary`) et la session continue ; sans
        frontière, la compaction n'a pas eu lieu et le délai de reprise
        (`COMPACT_RETRY_S`, porté par le processus) évite la boucle."""
        from . import resident_runner
        avant = rp.compactions
        mesure_avant = self._window_measure
        rp.compact_requested()
        ok = self.run_turn({"kind": "prompt",
                            "prompt": resident_runner.COMPACT_COMMAND, "ids": []})
        courant = self.resident or rp
        if courant.compactions <= avant:
            # CTX-04b : rien n'a changé dans la session, la mesure d'avant vaut
            self._window_measure = mesure_avant
        if not ok:
            log_async("[%s] native compaction cancelled: the turn did not complete"
                      % self.name)
            self._fil_note("Native compaction cancelled: the turn did not complete. "
                           "The session goes on.",
                           meta={"action": "compaction", "etat": "annulee"})
            return False
        if courant.compactions <= avant:
            log_async("[%s] native compaction: no compact_boundary reported "
                      "(context %s)" % (self.name, courant.last_context))
            self._conversation_state(
                "Native compaction asked (%s): the harness reported no "
                "compact_boundary; the session goes on."
                % resident_runner.COMPACT_COMMAND,
                meta={"action": "compaction", "etat": "sans-frontiere"})
            return False
        self._conversation_state(
            "Session compacted: context %s → %s tokens."
            % (courant.compact_pre, courant.compact_post),
            meta={"action": "compaction", "pre": courant.compact_pre,
                  "post": courant.compact_post})
        return True

    def _rotation_allowed(self) -> bool:
        """L226b : le tour de résumé d'une rotation est un appel de modèle. Il
        passe les gardes d'un tour (budget et plafonds du compte, santé de
        l'authentification) et tourne sous le compte de la session ; sinon la
        rotation attend : la bascule du prochain tour (`continuity`) résume
        sous l'ancien compte s'il le peut encore, ou ouvre un brief déterministe."""
        if (registry.get(self.db, self.name) or {}).get("operator_paused"):
            # L87b: the summary turn would be refused; wait without consuming a
            # forced rotation or noting a cancellation on every pass.
            return False
        if not self.budget_ok() or not self.auth_ready():
            return False
        if self._credits_exhausted():
            # #4164 (B mty-amh-docs-01) : aucun appel de modèle sur un compte
            # sans crédit, résumé compris ; la rotation attend son retour à ok
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

    def _keep_summary_display(self, text: str) -> bool:
        """#2096 : garde un fragment d'affichage pour le repli de
        `turn_summary`, dans les deux bornes (fragments et caractères). Seule
        la tête de l'affichage est lue ensuite : la queue perdue ne change
        rien au résumé, et le pic du tour reste borné.

        Les marqueurs de fin de tour ne sont pas gardés : `turn_summary` les
        ôte à la lecture, et un flux qui les répète ne doit pas consommer le
        budget qui revient au vrai affichage."""
        if text.startswith(adapters.END_OF_TURN_MARK):
            return False
        if len(self.summary_display) >= SUMMARY_DISPLAY_MAX_FRAGMENTS:
            return False
        separator = 1 if self.summary_display else 0
        remaining = SUMMARY_DISPLAY_MAX_CHARS - self._summary_chars - separator
        if remaining <= 0:
            return False
        text = text[:remaining]
        self.summary_display.append(text)
        self._summary_chars += len(text) + separator
        return True

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
        self._context_session_forget()
        self.last_turn_reread = 0
        self.last_turn_reread_session = None
        self.last_turn_context = self.last_turn_context_session = None
        # L81 (étude §6.1) : l'ancienne session ne sera plus reprise, son cumul
        # de relu n'a plus à être gardé.
        self._resident_reread.pop(ancienne or "", None)
        self._fil_note(
            "Session rotation (%s): the old id (%s) is kept in "
            "`session-history.jsonl`; the new session starts from the summary below.\n\n%s"
            % (raison, ancienne or "new", resume),
            meta={"action": "rotation", "session": ancienne or "", "raison": raison})
        # L81 (étude §6.3) : la rotation est annoncée dans la conversation.
        self._conversation_state(
            "Session rotated (%s): the old id (%s) is kept in "
            "`session-history.jsonl`; the new session starts from the summary."
            % (raison, ancienne or "new"),
            session=ancienne, meta={"action": "rotation", "raison": raison})
        # CTX-02b : la rotation n'est publiée que si elle a réussi ; le tour
        # suivant l'expose avec sa politique et ses valeurs. Aucune décision
        # ne lit ce champ.
        self._last_rotation_observation = {
            "kind": "context_cap" if raison.startswith("context cap")
                    else "window_maintenance" if raison.startswith("window maintenance")
                    else "session_size",
            "reason": raison,
            "old_session": ancienne or None,
            "at_ts": time.time(),
        }
        return True

    def _conversation_state(self, text: str, *, session: str | None = None,
                            meta: dict | None = None) -> None:
        """L78/L81 : une ligne d'état dans la conversation de l'agent (visible
        en `ameesh chat`), sans jamais casser le geste qu'elle raconte."""
        try:
            from . import conversation, fil as fil_mod
            log = conversation.Log(
                self.db, self.cfg, agent=self.name,
                project=fil_mod.project_for(self.cfg, fil_mod.agent_project(self.agent)))
            log.append("state", text, session=session, meta=meta)
        except Exception as exc:  # noqa: BLE001
            log_async("[%s] conversation state not recorded (%s)" % (self.name, exc))

    # -- rotation au changement de lot (0025, L26) ---------------------------
    def turn_lot(self, spec: dict) -> str | None:
        """Le lot (`work_item`) d'un tour, ou None s'il n'est pas déductible.

        1. Les messages du tour : s'ils portent UN seul lot (`--lot`), c'est
           lui ; plusieurs lots distincts = ambigu, None — sauf un lot tenu
           par un AUTRE agent, qui ne choisit pas le tour (#3325).
        2. Sinon, l'unique lot ouvert assigné à l'agent ; plusieurs = None.
        Un tour dont le lot est inconnu ne déclenche jamais de rotation.

        #2527 : un message seulement informatif (`_informative_message`) ne
        porte pas le lot du tour : un avis sur le lot d'un autre n'ouvre ni
        ne met de côté aucune session.

        #3325 (régression de #2410) : un courrier threadé sur le lot d'un
        AUTRE agent — le destinataire n'y est ni assigné ni relecteur désigné
        — ne choisit pas le lot du tour, donc n'ouvre ni ne met de côté
        aucune session. Le tour revient aux lots que l'agent porte (ou reste
        sans lot) ; le courrier reste livré dans la consigne. Un lot sans
        agent (non assigné, tenu par un humain, inconnu) garde le comportement
        d'avant : il n'appartient à aucun autre agent, et son dossier porte le
        courrier primaire (#811).
        """
        ops = storage.of(self.db).operations
        try:
            ids = [int(i) for i in spec.get("ids") or []]
            rows = spec.get("rows") or []
            if rows:
                utiles = [r for r in rows if not self._informative_message(r)]
                if not utiles:
                    return None   # que des avis : le tour reste où il est
                lots = sorted({str(r.get("work_item_id") or "").strip()
                               for r in utiles} - {""})
            elif ids:
                lots = ops.message_lots(ids)
            if rows or ids:
                # #3325 : le lot d'un AUTRE agent ne dédie pas la session ;
                # il est retiré avant l'arbitrage « un seul / ambigu ».
                lots = [lot for lot in lots if not self._turn_lot_foreign(lot)]
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
        actifs = [a for a in assignes if a.get("state") in lifecycle.ACTIVE]
        if len(actifs) == 1:
            return str(actifs[0]["id"])
        return None

    def _turn_lot_foreign(self, lot: str) -> bool:
        """#3325 : ce lot est-il tenu par un AUTRE agent (donc pas le tour) ?

        Seul un lot assigné à un autre agent est écarté. Un lot sans agent
        (non assigné, tenu par un humain, inconnu) n'appartient à personne
        d'autre : il garde son rôle de dossier du courrier primaire (#811), et
        une session qui lui serait dédiée ne prive aucun autre agent de son
        lot. Un relecteur désigné porte le lot : il n'est pas « d'un autre ».
        Une base illisible lève `DbError`, que `turn_lot` traite comme avant :
        aucun tour ne part sur une lecture incertaine."""
        from . import work as work_mod
        from .work_reviewers import designated

        lot = str(lot or "").strip()
        if not lot.isdigit():
            return False
        item = work_mod.get(self.db, int(lot))
        if item is None:
            return False
        assignee = str(item.get("assignee") or "").strip()
        if not assignee or assignee == self.name:
            return False
        if assignee.startswith(work_mod.HUMAN_PREFIX):
            return False
        return self.name not in designated(item)

    def _informative_message(self, row: dict) -> bool:
        """#2527 : ce message n'est qu'un avis, il ne choisit pas le lot du
        tour — message passif (L125 : accusé, copie, diffusion, radar…) ou
        avis d'une prise automatique (L165) d'un lot par un AUTRE agent. Les
        vrais échanges sur un lot (consigne, feu, CI, verdict) gardent la
        rotation."""
        if mail.passive_reason(row):
            return True
        payload = mail.message_payload(row)
        prise = payload.get("lot_take") if isinstance(payload, dict) else None
        return (mail.is_event(row) and isinstance(prise, dict)
                and str(prise.get("agent") or "") != self.name)

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

    def rotate_for_lot(self, spec: dict | None) -> dict | None:
        """Avant un tour : passe à la session du lot du tour s'il change
        (0025 ; #408, une session par lot).

        La session courante est mise de côté pour son lot, sans résumé ni
        oubli : un retour sur ce lot (relecture, décision, déblocage) la
        reprendra avec `--resume`. Celle du lot du tour est reprise si elle a
        été ouverte sous le même harnais, le même compte et le même dossier ;
        sinon une session neuve s'ouvre sur le brief MINIMAL du lot (#794 :
        canon, fiche, diff, fil du lot). Une session sans lot noté qui a déjà
        tourné n'est jamais adoptée par un lot : elle est oubliée avant
        d'ouvrir celle du lot (#794). Un lot FINI ferme sa session une fois,
        puis ses tours partagent la session courante ou une session sans lot :
        jamais de session dédiée rouverte ni de fermeture à chaque tour (#1898
        N1 ; c'est ici que vit l'ancien prédicat `lot_rotation_due`, #1898 N3).
        Si la session courante appartient à un AUTRE lot encore ouvert, elle
        est mise de côté avant ce tour (#2055) : la session d'un lot ne sert
        jamais un autre lot (#794), même fini. Bail perdu, base injoignable ou
        dossier absent : le tour ne part pas dans la session du lot précédent
        et sa consigne reste à livrer (#811).
        """
        if not spec:
            return spec
        lot = spec.get("lot")
        self._workdir_lot = lot or self.agent.get("session_work_item")
        from . import projects as projects_mod
        multi_switch = (lot and self.proc is None
                        and len(projects_mod.agent_projects(self.agent)) > 1
                        and str(lot) != str((self.agent or {}).get("session_work_item") or ""))
        if self.proc is not None or (self.session_policy() != "par-lot" and not multi_switch):
            return spec
        courant = str((self.agent or {}).get("session_work_item") or "").strip()
        fini = False
        if courant and (not lot or str(lot) == courant):
            if self._close_finished_lot_session(courant):
                # le lot de la session active est fini : elle est fermée ici,
                # avant le tour (même lot, ou tour sans lot) ; un tour sur un
                # AUTRE lot passe par la bascule, qui la ferme aussi.
                courant = ""
                fini = True
        if not lot:
            return spec
        if not fini and courant != str(lot):
            # #1898 N1 : un lot fini ne rouvre jamais de session dédiée. Le
            # tour partage la session courante (ou une session sans lot,
            # ouverte sur le brief déterministe posé par la fermeture) ; la
            # session n'est donc ni fermée ni rouverte à chaque tour.
            fini = self._lot_is_finished(lot)
        if fini:
            if courant and courant != str(lot):
                # #2055 : la session courante est celle d'un autre lot, encore
                # ouvert : elle est mise de côté pour lui, jamais partagée avec
                # le tour du lot fini (#794). Le tour part sans lot.
                if not self._park_lot_session(courant, str(lot)):
                    return dict(spec, lot_switch_failed=True)
            spec["lot_finished"] = True
            return spec
        if courant:
            if courant == str(lot):
                return spec
            if not self._switch_lot(courant, str(lot)):
                return dict(spec, lot_switch_failed=True)
            return spec
        # aucun lot noté : une session qui a déjà tourné ne sera jamais
        # partagée avec un lot ; celle qui va s'ouvrir est celle du lot.
        if self.current_session() and not self._forget_lotless_session():
            try:
                if storage.of(self.db).operations.set_session_work_item(
                        self.name, self.runner.runner_id, self.epoch, str(lot)):
                    self.agent["session_work_item"] = str(lot)
            except db_mod.DbError as exc:
                log_async("[%s] session lot not recorded (%s)" % (self.name, exc))
            return spec
        # #2055 : la session sans lot vient d'être écartée ; si une session
        # attend pour le lot du tour (mise de côté par une bascule, ou avant le
        # tour d'un lot fini), elle est reprise, comme toute bascule (#408) —
        # jamais une session neuve quand la sienne est de côté.
        parked = self._has_parked_session(str(lot))
        if parked is None:
            return dict(spec, lot_switch_failed=True)
        if parked:
            if not self._switch_lot("", str(lot)):
                return dict(spec, lot_switch_failed=True)
            return spec
        self._open_lot_session(lot)
        return spec

    #: états d'un lot FINI (#794) : sa session se ferme, comme la purge le
    #: faisait — fusionné, livré (`promoted`) ou fermé.
    LOT_FINISHED_STATES = lifecycle.COMPLETED

    def _lot_is_finished(self, lot: str | None) -> bool:
        """Le lot est-il FINI (fusionné, livré, fermé) ? (lecture du registre)

        #1898 N1 : un lot fini ne reçoit plus de session dédiée ; identifiant
        illisible ou base injoignable rend faux (le tour suit le chemin
        ordinaire), comme `_close_finished_lot_session`."""
        lot = str(lot or "").strip()
        if not lot.isdigit():
            return False
        try:
            from . import work as work_mod
            item = work_mod.get(self.db, int(lot))
        except db_mod.DbError as exc:
            log_async("[%s] lot #%s not checked (%s)" % (self.name, lot, exc))
            return False
        return bool(item and item.get("state") in self.LOT_FINISHED_STATES)

    def _close_finished_lot_session(self, lot: str) -> bool:
        """#794 B1 (revue Claude du 2026-10-14) : ferme la session active d'un
        lot FINI avant le tour suivant — « la session se ferme à la fusion ».

        Trois garde-fous : seulement un lot fini (un lot encore ouvert mais
        non assigné à l'agent — relecture, pilotage, audit — ne ferme jamais
        sa session) ; seulement en politique `par-lot` (les politiques `jamais`
        et `taille` gardent leur session longue) ; jamais pendant un tour, et
        l'oubli est fencé par le bail, comme `_forget_lotless_session`. Le
        fichier local qui portait la session est retiré dans la foulée ; s'il
        ne peut pas l'être (PermissionError), l'oubli tient quand même :
        `session_reset_at` est posé sous le bail, donc `current_session` ne le
        relit pas, worker recréé compris. Rend True si la session a été fermée.
        """
        if self.proc is not None or self.session_policy() != "par-lot":
            return False
        lot = str(lot or "").strip()
        if not lot.isdigit() or not self._lot_is_finished(lot):
            return False
        session = self.current_session()
        try:
            oubliee = registry.clear_session(self.db, self.name, self.runner.runner_id,
                                             self.epoch)
        except db_mod.DbError as exc:
            log_async("[%s] session %s of finished lot #%s not forgotten (%s)"
                      % (self.name, session or "?", lot, exc))
            return False
        if not oubliee:
            log_async("[%s] session of finished lot #%s not forgotten: lease lost"
                      % (self.name, lot))
            return False
        try:
            os.unlink(self._path("session"))
        except FileNotFoundError:
            pass
        except OSError as exc:
            # P2 bloquant du contrôle bureau (14/10) : le fichier reste, mais
            # l'oubli est durable — `session_reset_at` vient d'être posé dans
            # la même écriture fencée, donc `current_session` ne le relira pas
            # (worker recréé compris).
            log_async("[%s] local session file not removed (%s): already stale "
                      "after the reset" % (self.name, exc))
        self.agent = registry.get(self.db, self.name) or self.agent
        self._reset_session_counts()
        self._state_note("session_harness", "")
        if not self.resume_summary:
            # un tour sans lot (reprise, veille, courrier ambigu) ouvrira une
            # session neuve : le brief déterministe l'ouvre, comme
            # `_ecarte_session`. Un résumé déjà en attente (rotation) est
            # gardé : il décrit la session qu'on vient de fermer.
            try:
                from . import reprise
                brief = reprise.deterministic_brief(
                    self.cfg, self.db, self.agent, session=session,
                    reason="the session of lot #%s, which is finished" % lot,
                    consigne="This turn's instruction follows this brief: resume your "
                             "work from there.")
            except Exception as exc:  # un brief manquant n'empêche pas l'oubli
                log_async("[%s] deterministic brief unavailable (%s)" % (self.name, exc))
            else:
                if not self._resume_keep(brief, "ameesh"):
                    self.resume_summary, self.resume_origin = brief, "ameesh"
                    self.resume_continuation = False
        log_async("[%s] session %s closed: lot #%s is finished"
                  % (self.name, session or "?", lot))
        self._fil_note(
            "Session %s closed at the lot's end: lot #%s is finished; a new "
            "session opens on the next turn's brief." % (session or "?", lot),
            meta={"action": "session-lot-fin", "session": session or "", "lot": lot})
        return True

    def _park_lot_session(self, courant: str, fini: str) -> bool:
        """#2055 (P3 de la relecture de #1898) : met de côté la session du lot
        `courant` (encore ouvert) avant le tour d'un lot FINI `fini`.

        La session d'un lot n'est jamais partagée avec un autre (#794) ; le
        tour du lot fini part donc dans une session SANS lot, ouverte sur le
        brief déterministe (comme la fermeture d'un lot fini). La session mise
        de côté pour `courant` sera reprise par la bascule au prochain tour sur
        ce lot. Fencé par le bail, sans reprise : bail perdu ou base
        injoignable rend False, et le tour est différé, comme une bascule
        refusée.
        """
        compte = account_turn.for_turn(self)
        nom = None if compte is None or compte is account_turn.INVALID else compte.name
        session = self.current_session()
        # le harnais que le tour lancera (L242b), comme `_switch_lot` : une
        # session n'est reprise que sous le harnais qui l'a ouverte
        harnais = self.turn_harness()
        try:
            out = storage.of(self.db).lot_sessions.switch(
                self.name, self.runner.runner_id, self.epoch, to_lot=None,
                harness=harnais, account=nom, cwd=self.agent.get("cwd"),
                current=session)
        except db_mod.DbError as exc:
            log_async("[%s] session of lot %s not put aside (%s)"
                      % (self.name, courant, exc))
            return False
        if out is None:
            log_async("[%s] session of lot %s not put aside: lease lost"
                      % (self.name, courant))
            return False
        try:
            os.unlink(self._path("session"))  # sinon `current_session` le relirait
        except OSError:
            pass
        self.agent = registry.get(self.db, self.name) or self.agent
        self._reset_session_counts()
        self._state_note("session_harness", "")
        if not self.resume_summary:
            try:
                from . import reprise
                brief = reprise.deterministic_brief(
                    self.cfg, self.db, self.agent, session=session,
                    reason="the session of lot #%s, still open, is put aside for "
                           "its lot: this turn is on lot #%s, which is finished"
                           % (courant, fini),
                    consigne="This turn's instruction follows this brief: resume your "
                             "work from there.")
            except Exception as exc:  # un brief manquant n'empêche pas la mise de côté
                log_async("[%s] deterministic brief unavailable (%s)" % (self.name, exc))
            else:
                if not self._resume_keep(brief, "ameesh"):
                    self.resume_summary, self.resume_origin = brief, "ameesh"
                    self.resume_continuation = False
        log_async("[%s] session %s of lot %s put aside: lot %s is finished, no "
                  "session is dedicated to it" % (self.name, session or "?", courant, fini))
        self._fil_note(
            "Session %s of lot %s put aside before the turn of finished lot %s: "
            "a session is never shared between lots (#794)." % (
                session or "?", courant, fini),
            meta={"action": "session-lot-mise-de-cote", "session": session or "",
                  "lot": courant, "fini": fini})
        return True

    def _has_parked_session(self, lot: str) -> bool | None:
        """#2055 : une session est-elle mise de côté pour le lot `lot` ?

        Lecture seule : la bascule (`_switch_lot`) décide ensuite de la
        reprendre (même harnais, compte et dossier) ou de l'écarter.
        None si la lecture échoue : le tour doit attendre cette lecture."""
        lot = str(lot or "").strip()
        if not lot.isdigit():
            return False
        try:
            rows = storage.of(self.db).lot_sessions.listing(self.name)
        except db_mod.DbError as exc:
            log_async("[%s] parked sessions unreadable (%s)" % (self.name, exc))
            return None
        return any(str(r.get("work_item_id")) == lot and r.get("session_id")
                   for r in rows)

    def _forget_lotless_session(self) -> bool:
        """#794 : oublie la session active qui n'a aucun lot noté.

        Elle a été ouverte par un tour dont le lot était inconnu (reprise,
        veille, courrier ambigu) : l'adopter pour un lot ferait porter à ce
        lot le contexte d'un autre. Oubli fencé par le bail, comme une
        rotation ; le fichier local est retiré dans la foulée, et s'il ne peut
        pas l'être, le `session_reset_at` posé sous le bail le rend périmé
        (#794)."""
        session = self.current_session()
        try:
            oubliee = registry.clear_session(self.db, self.name, self.runner.runner_id,
                                             self.epoch)
        except db_mod.DbError as exc:
            log_async("[%s] lotless session %s not forgotten (%s)"
                      % (self.name, session or "?", exc))
            return False
        if not oubliee:
            log_async("[%s] lotless session not forgotten: lease lost" % self.name)
            return False
        try:
            os.unlink(self._path("session"))
        except FileNotFoundError:
            pass
        except OSError as exc:
            # #794 : même oubli durable que la session d'un lot fini — le
            # fichier non retiré est antérieur au reset posé par l'écriture
            # fencée, donc jamais repris.
            log_async("[%s] local session file not removed (%s): already stale "
                      "after the reset" % (self.name, exc))
        self.agent = registry.get(self.db, self.name) or self.agent
        self._reset_session_counts()
        self._state_note("session_harness", "")
        # #1898 N2 : le résumé de reprise en attente sur disque
        # (`RESUME_PENDING`) décrivait la session qu'on vient d'oublier ; sans
        # ce retrait, un worker recréé le relisait (`_resume_load`, aucune
        # session courante) et ressuscitait un résumé périmé au redémarrage.
        self._resume_drop()
        log_async("[%s] session %s had no lot: forgotten, never shared with a lot"
                  % (self.name, session or "?"))
        self._fil_note("Session %s had no lot (opened by a turn whose lot was "
                       "unknown): forgotten, it will never be shared between lots."
                       % (session or "?"), meta={"action": "session-sans-lot",
                                                 "session": session or ""})
        return True

    def _lot_opening_resume(self, brief: str) -> tuple[str, str]:
        """Le résumé qui ouvre une session de lot, sans perdre une relève.

        L165 (EP3) : un brief de relève en attente (origine `ameesh`) ouvre la
        session neuve AVANT le brief du lot, qui le complète ; il n'est jamais
        écrasé par lui (verdict cx2 B2). Un résumé de rotation (origine
        `agent`) décrit la même session de lot et ouvre aussi la session, la
        fiche du lot le complétant (#1898 N1).
        """
        election = getattr(self, "_election_brief", "")
        if election:
            return (election + "\n\n" + brief if brief else election, "ameesh")
        if self.resume_summary and self.resume_origin == "agent":
            return self.resume_summary + "\n\n" + brief, "agent"
        return brief, "ameesh"

    def _open_lot_session(self, lot: str) -> None:
        """#794 : la session qui va s'ouvrir est dédiée au lot `lot` ; son
        ouverture est le brief minimal du lot, jamais un brief de reprise
        général.

        N1 (revue Claude du 2026-10-14) : un résumé de rotation (origine
        `agent`, payé par un tour de résumé) décrit la MÊME session de lot ;
        il ouvre la session neuve, la fiche du lot le complète, et le tout est
        réécrit sur disque (`RESUME_PENDING`) pour qu'un redémarrage du worker
        ne ressuscite pas l'ancien résumé seul.

        L165 (EP3) : le brief de relève en attente survit à l'ouverture de la
        session de lot (`_lot_opening_resume`) ; sans cela, le premier tour
        élu perdait l'état du rôle (verdict cx2 B2).
        """
        brief = self._lot_brief(str(lot), "", {}) or ""
        resume, origin = self._lot_opening_resume(brief)
        if resume and not self._resume_keep(resume, origin):
            # disque indisponible : la mémoire au moins, le tour partira
            self.resume_summary, self.resume_origin = resume, origin
            self.resume_continuation = False
        elif not resume:
            self.resume_summary, self.resume_origin = "", "ameesh"
            self.resume_continuation = False
        self._reset_session_counts()
        log_async("[%s] new session for lot %s: minimal lot brief" % (self.name, lot))

    def _reset_session_counts(self) -> None:
        self.session_turns = 0
        self.session_tokens = 0.0
        self._context_session_forget()
        self.last_turn_reread = 0
        self.last_turn_reread_session = None
        self.last_turn_context = self.last_turn_context_session = None

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
            self.resume_continuation = False
        else:
            try:
                os.unlink(self._path("session"))  # sinon `current_session` le relirait
            except OSError:
                pass
        self.agent = registry.get(self.db, self.name) or self.agent
        self._reset_session_counts()
        if not repris:
            # L165 (EP3) : le brief de relève en attente n'est pas écrasé par
            # celui du lot ; les deux ouvrent la session neuve.
            resume, origin = self._lot_opening_resume(
                self._lot_brief(lot, courant, out))
            if resume and not self._resume_keep(resume, origin):
                self.resume_summary, self.resume_origin = resume, origin
                self.resume_continuation = False
            elif not resume:
                self.resume_summary, self.resume_origin = "", "ameesh"
                self.resume_continuation = False
        # #2055 : une reprise sans bascule (aucune session active) n'a pas de
        # lot courant à nommer.
        de = courant or "aucun"
        log_async("[%s] session of lot %s %s, lot %s: %s"
                  % (self.name, de, "put aside" if out.get("parked") else "none",
                     lot, "resumed (%s)" % repris if repris else "new session"))
        self._fil_note(
            "Lot change %s → %s: %s; %s.%s" % (
                de, lot,
                "session %s put aside for lot %s" % (out["parked"], de)
                if out.get("parked") else "no session to put aside",
                "session %s of lot %s resumed" % (repris, lot) if repris
                else "new session for lot %s, opened on the lot brief" % lot,
                " Session %s of lot %s not resumed (%s)." % (
                    out["discarded"], lot,
                    "lot finished or no longer assigned to the agent"
                    if out.get("discard_reason") == "lot"
                    else "other harness, account or directory, or unknown origin")
                if out.get("discarded") else ""),
            meta={"action": "session-lot", "de": courant, "vers": lot,
                  "session": out.get("parked") or "", "reprise": repris or ""})
        return True

    def _adopt_originless_session(self, session: str, cwd: str) -> bool:
        """#1900 N1 : adopter une session d'avant 0384 (origine inconnue).

        Un agent multi-projet dont la session courante n'a ni origine ni
        dossier connus (`session_origin`/`session_cwd` nuls) est basculé de
        force vers SON lot par `_run_turn` ; quand le lot est le projet
        primaire — donc le dossier de l'agent, seul dossier où une session
        d'avant 0384 a pu être ouverte — la bascule ne fait que perdre la
        session (`courant == to_lot`, rien à mettre de côté). On note son
        origine au registre : le tour la reprend et les bascules suivantes la
        mettent de côté normalement.

        Rend False quand l'écriture échoue : l'appelant garde la bascule
        forcée, comportement d'avant le correctif."""
        harnais = (self.agent.get("session_harness")
                   or self.agent.get("harness") or self.turn_harness())
        # #1900 B1 : écriture fencée (verrou de ligne, bail vivant, échéance
        # recontrôlée après l'attente, session ET lot inchangés). Une session
        # remplacée entre l'instantané et l'adoption — rotation, remplaçant,
        # autre lot — n'est jamais réécrite ; l'appelant garde alors la bascule
        # forcée, comportement d'avant le correctif.
        lot = str(self.agent.get("session_work_item") or "")
        try:
            adopte = registry.adopt_session_origin(
                self.db, self.name, session, lot,
                self.runner.runner_id, self.epoch,
                harness=harnais, cwd=cwd)
        except db_mod.DbError as exc:
            log_async("[%s] originless session %s not adopted (%s)"
                      % (self.name, session, exc))
            return False
        if not adopte:
            log_async("[%s] originless session %s not adopted: "
                      "lease or session changed" % (self.name, session))
            return False
        self.agent = registry.get(self.db, self.name) or self.agent
        log_async("[%s] originless session %s adopted for lot %s (origin %s)"
                  % (self.name, session, self.agent.get("session_work_item"), cwd))
        return True

    def _lot_brief(self, lot: str, courant: str, out: dict) -> str | None:
        """#794 : le brief MINIMAL d'une session neuve dédiée au lot `lot` :
        canon, fiche du lot, diff de sa branche, fil du lot réduit aux
        décisions, messages non lus et courriers déjà livrés pour ce lot
        (#2539 ; décision du bureau, 2026-10-13)."""
        try:
            from . import reprise, work as work_mod
            item = work_mod.get(self.db, int(lot)) if str(lot).isdigit() else None
            branche = (item or {}).get("branch")
            # #2055 : sans session active, aucun autre lot à citer ; #2242 :
            # ne dire « put aside » que si la bascule a réellement gardé une
            # session, et jamais avec un numéro de lot vide.
            if courant:
                aside = ("the one of lot #%s is put aside and will be resumed when "
                         "feedback arrives on it" % courant
                         if out.get("parked") else
                         "no session of lot #%s was put aside" % courant)
                autres = (" Your other lots keep their own sessions (%s): work only "
                          "on lot #%s here, in its own worktree and branch."
                          % (aside, lot))
                consigne = ("This session is dedicated to lot #%s%s.%s This turn's "
                            "instruction follows this brief."
                            % (lot, " (branch %s)" % branche if branche else "",
                               autres))
            else:
                constat = ("a session was put aside" if out.get("parked")
                           else "no session was put aside")
                consigne = ("This session is dedicated to lot #%s%s: %s. Work only "
                            "on lot #%s here, in its own worktree and branch. This "
                            "turn's instruction follows this brief."
                            % (lot, " (branch %s)" % branche if branche else "",
                               constat, lot))
            return reprise.lot_brief(
                self.cfg, self.db, self.agent, lot,
                reason="new session dedicated to lot #%s" % lot,
                consigne=consigne)
        except Exception as exc:  # un brief manquant n'empêche pas la bascule
            log_async("[%s] lot brief unavailable (%s)" % (self.name, exc))
            return None

    # -- dossier de travail déplacé (0018) ---------------------------------
    def adopt_moved_worktree(self) -> str | None:
        """Retrouve un dossier de travail déplacé, borné aux racines (0018).

        La recherche (marqueur git, candidat unique, dossier libre) et la mise
        à jour du registre vivent dans `workspaces` (L101 T3) ; ce point garde
        le journal de l'agent et la trace d'audit."""
        candidat = workspaces.adopt_moved_worktree(
            self.db, self.agent, marker_path=self._path("worktree.json"),
            roots=self.runner.worktree_roots,
            report=lambda texte: log("[%s] %s" % (self.name, texte)))
        if candidat:
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

    def _installe_hook_preuve(self, cwd: str) -> None:
        """#1465 : pose la paire de hooks `pre-commit`/`post-commit` du dépôt.

        Une seule fois par session OUVERTE, et jamais en mode relayé : la base
        n'est pas joignable depuis une VM médiée, le hook n'y servirait à rien.
        Le `pre-commit` atteste la création du commit dans le tour, le
        `post-commit` écrit la preuve ; aucun ne bloque un commit, et un échec
        ici ne touche pas le tour (l'origine des commits restera inconnue,
        jamais devinée)."""
        if getattr(self.runner, "mediated", False):
            return
        try:
            from . import review_provenance
            out = review_provenance.install_hook(cwd)
        except Exception as exc:  # noqa: BLE001 - la preuve ne casse pas le tour
            log_async("[%s] provenance hooks install failed: %s" % (self.name, exc))
            return
        if not out["installed"] and out.get("path"):
            log_async("[%s] provenance hooks not installed: %s"
                      % (self.name, out["reason"]))

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
                if not self._resume_keep(brief, "ameesh", continuation=True):
                    # #389 r3 : la session refusée ne se garde pas — le brief,
                    # au moins en mémoire, ouvre le tour
                    self.resume_summary, self.resume_origin = brief, "ameesh"
                    self.resume_continuation = True
            except Exception as exc:  # un brief manquant n'empêche pas l'oubli
                log_async("[%s] deterministic resume brief unavailable (%s)"
                          % (self.name, exc))
            self.agent = registry.get(self.db, self.name) or self.agent
            self.session_turns = 0
            self.session_tokens = 0.0
            self._context_session_forget()
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
        Une session présente seulement sous un autre compte repart avec un
        brief (#4546). Dans le doute (stockage introuvable, recherche
        incomplète), la session est gardée (verdict codex #183)."""
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
        store_present = os.path.isdir(reprise.session_root(harness, home))
        if not store_present:
            self._avertit_session("session store %s not found"
                                  % reprise.session_root(harness, home))

        def connue(session: str) -> bool:
            try:
                if reprise.find_session(harness, home, session, strict=True):
                    return True
                for autre in autres:
                    if reprise.find_session(harness, autre, session, strict=True):
                        self._avertit_session(
                            "session %s found under %s, not under %s (turn directory): "
                            "resume brief required" % (session, autre, home))
                        return False
            except reprise.SessionSearchIncomplete as exc:
                # stockage momentanément illisible : introuvable n'est pas établi
                self._avertit_session("search for session %s incomplete (%s): "
                                      "kept" % (session, exc))
                return True
            # No store and no positive evidence of an account mismatch:
            # preserve the existing conservative behavior.
            return not store_present
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

    def _resolve_workdir(self, adopt: bool = True, *, lot=None) -> str | None:
        """Le dossier de travail utilisable, ou None.

        La règle (primaire, lot secondaire déclaré, projet inconnu) vit dans
        `workspaces.resolve` (L101 T3) ; ce point garde l'appel et recopie le
        diagnostic lu par la garde L35 (`_workdir_missing`, #811 B1)."""
        lot = lot or getattr(self, "_workdir_lot", None) or self.agent.get("session_work_item")
        self._lot_workdir_secondary = False
        self._lot_workdir_path = None
        self._lot_workdir_error = ""
        issue = workspaces.resolve(
            self.cfg, self.db, self.agent, lot,
            mediated=getattr(self.runner, "mediated", False),
            mediated_workdir=self._mediated_workdir,
            adopt_worktree=(self.adopt_moved_worktree if adopt else None))
        self._lot_workdir_secondary = issue.secondary
        self._lot_workdir_path = issue.path
        self._lot_workdir_error = issue.error
        return issue.cwd

    def _primary_workdir(self, adopt: bool = True) -> str | None:
        """Le dossier primaire (cwd du registre, adoption bornée ; dépôt de
        l'exécuteur médié) — jamais un projet secondaire : la garde L35 ne se
        lève que sur le dossier qu'elle a vu manquer (#811 B1)."""
        return workspaces.primary_workdir(
            self.agent, mediated=getattr(self.runner, "mediated", False),
            mediated_workdir=self._mediated_workdir,
            adopt_worktree=(self.adopt_moved_worktree if adopt else None))

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
        sondage suivant, sans attendre l'échéance.

        Le délai (30 s, 60 s… borné) et son échéance sont calculés par
        `workspaces.lot_deferral` (L101 T3) ; l'état reste ici."""
        lot = str(lot or "")
        if not lot:
            return
        delay, until = workspaces.lot_deferral(self._lot_wd_delay.get(lot))
        self._lot_wd_delay[lot] = delay
        self._lot_wd_until[lot] = until
        if self._lot_workdir_path:
            self._lot_wd_path[lot] = self._lot_workdir_path
        log("[%s] lot #%s: %s — lot deferred for %ds, the agent keeps its other "
            "turns" % (self.name, lot, self._lot_workdir_error or "no directory",
                       int(delay)))

    def _lot_deferred(self, lot) -> bool:
        """#811 (B1) : ce lot secondaire attend-il encore son dossier ?

        Le dernier chemin connu est revérifié à chaque appel : un dossier
        recréé lève le différé tout de suite (pas d'attente de l'échéance).
        La règle vit dans `workspaces.lot_wait` (L101 T3) ; l'état reste ici."""
        lot = str(lot or "")
        if not lot or lot not in self._lot_wd_until:
            return False
        etat = workspaces.lot_wait(self._lot_wd_path.get(lot),
                                   self._lot_wd_until[lot])
        if etat.back:
            self._lot_wd_until.pop(lot, None)
            self._lot_wd_delay.pop(lot, None)
            self._lot_wd_path.pop(lot, None)
        return etat.waiting

    def _lot_workdir_blocked(self, lot) -> bool:
        """#811 (B1/B2) : ce lot ne peut pas ouvrir de tour faute de dossier.

        L'état de différé (mémoire de ce worker) d'abord ; sinon la règle
        partagée avec le hook (`workspaces.mail_hold`, L101 T3), pour ne pas
        livrer de courrier dans la session d'un autre lot ni consommer un
        réveil pour un lot dont le dossier manque. Une lecture en échec ne
        bloque rien."""
        if self._lot_deferred(lot):
            return True
        try:
            return workspaces.mail_hold(self.cfg, self.db, self.agent, lot)
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

        L'owner (`human:*`) interrompt toujours ; un événement du système
        (`ameesh`) seulement s'il est vérifiable en base (#795) : la fin de CI
        d'un lot de cet agent, pas un `--from ameesh` imité (garde L317c).
        Les agents passent par `AMEESH_INTERRUPT_SENDERS` ; la capacité
        « interrupt » d'une fiche canon s'ajoutera ici. Un urgent non habilité
        est remis comme un message normal (et l'abus est journalisé).
        """
        sender = (row.get("sender") or row.get("from") or "").strip()
        if not sender:
            return False
        from . import resident_queue, work
        # L77/B1 : toute entrée humaine est vérifiée AVANT la liste des agents.
        # Un nom nu déposé par un humain (forme d'`agent-mail send`) est une
        # entrée humaine dès que le message porte le marqueur `payload.human`
        # ou une preuve d'entrée ; `human:<id>` l'est toujours. Sans cette
        # normalisation, un nom nu déclaré dans `AMEESH_INTERRUPT_SENDERS`
        # préemptait sans preuve.
        humaine = (sender.startswith(work.HUMAN_PREFIX) or mail.is_human(row)
                   or bool(mail.human_account(row)))
        if humaine:
            # L77/B1 : un humain ne préempte que si la preuve d'entrée portée
            # par le message correspond à un local_account déclaré de sa fiche.
            # Le compte du runner/hook ne prouve jamais l'émetteur.
            ident = resident_queue.identity_for(
                sender, cfg=getattr(self, "cfg", None),
                canon_id=self._canon_id() if hasattr(self, "_canon_id") else None,
                proof=mail.human_account(row), human=True)
            if not ident.verified:
                log("[%s] urgent from %s without a verified local account: no "
                    "interruption" % (self.name, sender))
            return ident.verified
        if sender in self.cfg.interrupt_senders:
            return True
        return (self._system_event_interrupt(sender, row)
                or self._deep_analysis_interrupt(sender, row))

    def _system_event_interrupt(self, sender: str, row: dict) -> bool:
        """#795 : l'événement système qui perce le tour — la fin de CI d'un lot
        de cet agent. Vérifié en base : l'exécution est enregistrée, le lot
        qu'elle suit est bien assigné à l'agent, et ce courrier est celui que
        la CI a posé pour cette exécution (verdict ou première alerte). Un
        payload `ci` imité sous l'expéditeur `ameesh` ne préempte donc pas."""
        from . import work
        if sender != work.SYSTEM_SENDER:
            return False
        ci = mail.message_payload(row).get("ci")
        if not isinstance(ci, dict):
            return False
        try:
            run = storage.of(self.db).ci.run(str(ci.get("repo") or ""),
                                             int(ci.get("run_id") or 0))
            message_id = int(row.get("id") or 0)
        except (db_mod.DbError, TypeError, ValueError):
            return False
        if not run or not message_id:
            return False
        # le lot est celui de l'exécution (le courrier de CI le porte aussi,
        # mais la ligne de boîte ne rend pas `work_item_id`) : un lot relevé
        # par l'exécution et assigné à un autre agent ne perce pas ce tour.
        lot = (work.get(self.db, int(run["work_item_id"]))
               if run.get("work_item_id") else None)
        if not lot or (lot.get("assignee") or "") != self.name:
            return False
        posed = {int(run.get(cle) or 0)
                 for cle in ("urgent_message_id", "verdict_message_id")}
        return message_id in posed

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
        return raison or book.over(self.name, pace=False, hourly_share=part,
                                   account_checked=True)

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
            self._retenue_fin()  # #3218
            self._budget_reason = ""
            self._budget_nature = ""
            self._leve_pause_budget()
            return True
        self._retenue_debut("short_window" if account_window.is_short_window(raison)
                            else "budget")
        maintenant = time.monotonic()
        if maintenant >= self._budget_next_check:
            self._budget_next_check = maintenant + self.runner.budget_check_interval
            # `paused` n'est pas dans la contrainte de statut (L13 n'a pas de
            # migration) : la pause se lit `blocked` + raison dans `status_text`.
            # L242b (5) : la pause d'un agent critique est marquée — alerte
            # urgente `critical_budget_pause`
            # #3969 : un compte invalide est une configuration de l'hôte, pas
            # un budget — bloqué sous `CONFIG_BLOCK_PREFIX` (alerte
            # `account_config_invalid`), sans échéance de pause
            config = raison.startswith(accounts_mod.NO_VALID_PREFIX)
            marque = budget_mod.CRITICAL_MARK if account_turn.is_critical(self) \
                and not config else ""
            registry.set_status(self.db, self.name, "blocked",
                                status_text="%s%s%s" % (
                                    accounts_mod.CONFIG_BLOCK_PREFIX if config
                                    else budget_mod.PAUSE_PREFIX, marque, raison))
            # #2934 : une note par nature de pause (compte, fenêtre, échéance),
            # pas à chaque variation du pourcentage, qui reste dans `status_text`.
            # #4020 : la fenêtre courte (#3779 : cooldown, écart de départ,
            # plafond de tours) retient quelques secondes après chaque tour ;
            # elle reste dans `status_text`, sans note au fil.
            nature = accounts_mod.pause_nature(raison)
            if nature != self._budget_nature:
                self._budget_nature = nature
                if config:
                    self._fil_note("Account configuration error: %s. No turn until "
                                   "the host configuration is fixed (ameesh doctor)."
                                   % raison,
                                   meta={"action": "account_config", "raison": raison})
                elif not account_window.is_short_window(raison):
                    self._fil_note("Budget pause: %s. No turn until the gauge goes "
                                   "back down." % raison,
                                   meta={"action": "budget", "raison": raison})
            self._budget_reason = raison
        return False

    def _leve_pause_budget(self) -> None:
        """La garde laisse passer : une pause budget encore affichée est levée
        (L72). Sans cela, un agent mis en pause (par exemple quand l'état des
        comptes était illisible pendant une panne de base) restait `blocked`
        jusqu'à son prochain tour, même la jauge redescendue."""
        agent = self.agent or {}
        texte = agent.get("status_text") or ""
        if agent.get("status") != "blocked" or not texte.startswith(
                ("budget : ", accounts_mod.CONFIG_BLOCK_PREFIX)):
            return
        issue = registry.clear_marked_block(self.db, self.name, self.runner.runner_id,
                                            self.epoch, texte, "")
        if issue == "done":
            self.agent = dict(agent, status="idle", status_text="")
            log_async("[%s] pause lifted (%s)" % (self.name, texte))

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

    def _resident_usage_marker(self, usage: dict | None,
                               travail: dict | None) -> bool:
        """Pose (ou efface) la mesure résidente dans le marqueur (L80a).

        Le marqueur est l'état **durable** : un worker de remplacement y
        retrouve l'événement et peut réparer le tour avant de le régler.
        L'identité exacte du marqueur (`created_at`, `spend_key`, #2166) borne
        l'écriture à CE tour, jamais celui d'un remplaçant."""
        try:
            return registry.pending_spend_set_resident_usage(
                self.db, self.name, usage, travail)
        except db_mod.DbError as exc:
            log_async("[%s] resident usage marker not written (%s)"
                      % (self.name, db_mod.explain(exc)))
            return False

    def _resident_usage_durable(self, travail: dict | None) -> bool:
        """Met la mesure du tour résident à l'abri avant comptabilité (L80a).

        Elle vit d'abord dans le marqueur comptable (`resident_usage`), puis
        dans le flux (`resident.turn`) que lit le grand livre. Vrai seulement
        quand les deux sont faits : un worker de remplacement retrouve la
        mesure et ne règle jamais zéro. Un échec laisse le marqueur en place
        (fail-closed)."""
        from . import resident_runner
        path = self._path("events.jsonl")
        marker = travail
        usage = marker.get("resident_usage") if isinstance(marker, dict) else None
        if isinstance(usage, dict):
            if not resident_runner.write_usage_event(path, usage):
                return False
            # le flux porte la mesure : le marqueur n'a plus à la garder
            self._resident_usage_marker(None, marker)
            marker = dict(marker, resident_usage=None)
        while self._resident_usage_pending:
            event = self._resident_usage_pending[0]
            if not isinstance(marker, dict):
                return False       # pas de marqueur : rien de durable à réparer
            if not self._resident_usage_marker(event, marker):
                return False
            if not resident_runner.write_usage_event(path, event):
                return False
            self._resident_usage_marker(None, marker)
            self._resident_usage_pending.pop(0)
        return True

    def _rotation_du_flux(self, events_path: str) -> None:
        """#2820 : tourne `events.jsonl` au-delà de `events_quota`, au point sûr.

        Appelé avant le marqueur de compte du tour : les index de ligne
        (`spend_pending.start_index`) ne survivent pas à une rotation, donc
        aucun marqueur comptable ne doit attendre en base, ni aucune mesure
        résidente en mémoire. Base injoignable ou marqueur douteux : pas de
        rotation, le flux tournera à un tour suivant. Jamais bloquant."""
        from . import events_rotation
        try:
            quota = self.runner.housekeeping_policy().events_quota
            if os.path.getsize(events_path) <= quota:
                return
            if self._resident_usage_pending or self._compta_en_attente() is not None:
                return
            fait = events_rotation.rotate(events_path, quota)
        except FileNotFoundError:
            return
        except db_mod.Unavailable:
            return
        except Exception as exc:
            log("[%s] events.jsonl not rotated (%s)" % (self.name, exc))
            return
        if fait is not None:
            log("[%s] events.jsonl rotated: %.0f MiB, %d KiB carried over, previous in "
                "events.jsonl%s" % (self.name, fait.size / 1024 ** 2, fait.carried // 1024,
                                    events_rotation.PREVIOUS))

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
        if travail.get("ledger_ambiguous"):
            # #2166/B1 : plusieurs clés candidates pour ce marqueur (un ancien
            # worker a écrit une ligne que son identité ne distingue pas) :
            # écrire au hasard réglerait le mauvais tour. On suspend.
            self._compta_en_echec = True
            log_async("[%s] accounting marker ambiguous: turn accounting "
                      "suspended" % self.name)
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
            # pour sa réparation, qui ne double donc plus la ligne. Exacte
            # (#2166) quand le marqueur la porte ; les marqueurs d'avant 0471
            # gardent la clé historique, pour dédoublonner leur ligne.
            cle = self._spend_key(debut, travail)
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
            registry.pending_spend_clear(self.db, self.name, travail)
            self._compta_travail = None
            self._annonce_ram = ""
            self._compta_en_echec = False
            # CTX-02b : la mesure de contexte part APRÈS la ligne du grand
            # livre et son règlement ; un échec d'observation ne défait ni la
            # comptabilité ni la décision legacy (il est seulement journalisé).
            try:
                self._publier_contexte(usage, travail)
            except Exception as exc:  # noqa: BLE001 — observation seule
                log_async("[%s] context observation not published (%s)"
                          % (self.name, " ".join(str(exc).split())[:160]))
            return True
        except Exception as exc:  # panne non fatale au tour, mais fail-closed
            self._compta_en_echec = True
            log_async("[%s] turn accounting failed (%s): turns suspended"
                      % (self.name, " ".join(str(exc).split())[:160]))
            return False

    def _publier_contexte(self, usage: cost_mod.TurnUsage,
                          travail: dict) -> None:
        """CTX-02b : publie un événement ``turn.context`` pour ce tour.

        Le document ``ameesh-context/1`` collecté sur le flux part **à côté**
        des compteurs legacy qui ont servi au grand livre et aux décisions
        (entrée/cache/sortie, relecture, contexte du dernier appel, session),
        avec la politique en vigueur et les motifs de clôture/rotation
        observés. Observation seule : rien ici ne modifie une ligne
        ``turn_costs``, un seuil ou une décision ; une erreur est journalisée
        et le tour reste compté.
        """
        document = self._context_observation
        if document is None:
            # Tour réparé par un autre worker (la mesure du flux est perdue) :
            # on ne publie pas une observation inventée, et la session garde le
            # trou — `R_session` reste inconnue au lieu d'une somme supposée.
            self._context_profile_observed = None
            self._context_profile_mode = None
            self._invalidate_context_session(
                travail.get("session") or self._context_session_id,
                (context_metrics.REASON_COLLECTION_LOST,))
            return
        try:
            # CTX-03b : la clé de profil du contrat §3.1 dans `turn.context`.
            # Le modèle du grand livre fait foi pour Codex, que `exec --json`
            # n'annonce pas (lu dans son journal de session) ; le modèle
            # demandé n'est publié qu'avec la qualité `requested`, jamais une
            # fenêtre. Une version de harnais non enregistrée reste nulle avec
            # sa raison, sans retenir le tour.
            requested = _modele_publie(document.get("model"), usage.model)
            harness = usage.harness or document.get("harness")
            profile = context_profile.publish(
                self._context_profile_observed,
                requested_model=requested,
                harness=harness,
                harness_version=self.runner.harness_version(harness),
                mode=self._context_profile_mode or document.get("mode"),
                roles=(self.agent or {}).get("roles"))
            document["profile"] = profile
            document["model"] = profile["model_effective"] or document.get("model")
            document["mode"] = profile["mode"]
            document["role"] = profile["role"]
            legacy = {
                "input_tokens": usage.input_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "output_tokens": usage.output_tokens,
                "reread_tokens": usage.reread_tokens,
                "context_tokens": self.last_turn_context,
                "subagent_reread_tokens": self.last_turn_subagent_reread,
                "session_tokens": self.session_tokens,
                "session_turns": self.session_turns + 1,
                "context_max_tokens": self.context_max_tokens(),
                "session_max_tokens": self.session_max_tokens(),
            }
            decision = {
                "policy": {
                    "contract": context_metrics.SCHEMA,
                    "session": self.session_policy(),
                    "context_max_tokens": self.context_max_tokens(),
                    "session_max_tokens": self.session_max_tokens(),
                    # CTX-04a : présents seulement en opt-in.
                    **({"turn_reread_budget": int(self.cfg.turn_reread_budget)}
                       if self.cfg.turn_reread_budget > 0 else {}),
                    **({"session_reread_budget": int(self.cfg.session_reread_budget)}
                       if self.cfg.session_reread_budget > 0 else {}),
                    # CTX-04b : présent seulement en opt-in.
                    **({"window_maintenance_share": self.cfg.window_maintenance_share}
                       if self.cfg.window_maintenance_share > 0 else {}),
                },
                # Le journal partagé porte des états, pas du texte libre : le
                # motif est normalisé (`kind`), les valeurs qui l'ont déclenché
                # restent lisibles à côté.
                "close": _sans_raison(self._turn_close_observation),
                "rotation": _sans_raison(self._last_rotation_observation),
                # CTX-03b : `false` explicite quand le tour n'a pas compacté
                # (une demande n'est pas une réussite, contrat §4.2) ; le rejeu
                # peut alors juger la fenêtre au lieu de rester aveugle.
                "compacted": document.get("compaction") is not None,
            }
            if self.cfg.window_maintenance_share > 0:
                decision["window"] = self._window_decision(self.resident)
            payload = cost_mod.context_publication(
                document, legacy=legacy, decision=decision,
                agent=self.name, host=self.runner.host,
                label=travail.get("turn"), kind=None,
                work_item_id=travail.get("work_item_id"),
                started_ts=travail.get("created_ts"),
                ended_ts=time.time())
            project = ((self.agent or {}).get("team")
                       or (self.agent or {}).get("chantier") or None)
            events_mod.record(self.db, cost_mod.CONTEXT_EVENT,
                              "agent:" + self.name, project=project, **payload)
        except Exception as exc:  # noqa: BLE001 — observation jamais fatale
            log_async("[%s] context observation not published (%s)"
                      % (self.name, " ".join(str(exc).split())[:160]))
            # L'événement manque : le trou est dit, la base ne continue pas
            # comme si le tour avait été publié.
            self._invalidate_context_session(
                document.get("session") or self._context_session_id,
                (context_metrics.REASON_COLLECTION_LOST,))
        finally:
            self._context_observation = None
            self._context_profile_observed = None
            self._context_profile_mode = None
            self._turn_close_observation = None
            self._last_rotation_observation = None

    def _compta_repare(self) -> None:
        """Avant un tour : rejoue le travail en attente ; `{}` suspend."""
        travail = self._compta_en_attente()
        if travail is None and self._compta_doute:
            # L72 : la suspension venait d'une base injoignable à la création
            # du worker ; aucun marqueur n'attend, rien à réparer.
            self._compta_doute = False
            self._compta_en_echec = False
        if travail:
            if not self._resident_usage_durable(travail):
                self._compta_en_echec = True
                log_async("[%s] resident usage still not durable: "
                          "turn accounting stays suspended" % self.name)
                return
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

    def _spend_key(self, debut: int, marker) -> str | None:
        """La clé du tour (L60) : grand livre, marqueur et réservation #1469.

        Exacte quand le marqueur la porte (#2166 : `agent:index:created_at`,
        texte UTC à la microseconde), jamais dérivée d'un float8 relu — une
        connexion à précision réduite en perd les derniers chiffres. Un
        marqueur d'avant 0471 (`spend_key` NULL) porte `ledger_key`, la clé
        durable déjà écrite par son tour (ligne ou réservation, retrouvée par
        l'identité du marqueur côté stockage) : sa réparation dédouble la
        ligne existante et règle la réservation, quelle que soit la précision
        de la connexion. Un float reste accepté pour les appelants
        historiques (tests) et ne sert qu'à ce repli.
        """
        if isinstance(marker, dict):
            if marker.get("ledger_ambiguous"):
                return None       # B1 : ne jamais choisir une clé au hasard
            key = marker.get("ledger_key") or marker.get("spend_key")
            if key:
                return str(key)
            cree = marker.get("created_ts")
        else:
            cree = marker
        return ("%s:%d:%.6f" % (self.name, int(debut), float(cree))
                if cree is not None else None)

    @staticmethod
    def _same_marker(left: dict | None, right: dict | None) -> bool:
        """Le même marqueur comptable, par son identité exacte (#2166).

        Deux lectures du même marqueur rendent le même `created_at` texte et
        la même `spend_key` ; un remplaçant en diffère, même quand le float8
        relu à précision réduite les confondrait."""
        if not left or not right:
            return False
        if left.get("spend_key") or right.get("spend_key"):
            return (left.get("spend_key") == right.get("spend_key")
                    and left.get("created_at") == right.get("created_at"))
        if left.get("created_at") or right.get("created_at"):
            return left.get("created_at") == right.get("created_at")
        return left.get("created_ts") == right.get("created_ts")

    def _annule_tour_avant_lancement(self, marker: dict | None,
                                     spend_key: str | None) -> None:
        """Un tour refusé avant le `Popen` ne laisse rien de comptable ouvert.

        Le harnais n'a pas tourné : le marqueur est effacé par son identité
        exacte et la réservation du compte est rendue (`released`), pour que
        le refus ne compte pas comme dépense et ne bloque pas l'admission
        suivante. Au mieux : le tour est déjà refusé ; une panne de base est
        journalisée et la réparation reste fail-closed."""
        marker = marker or {}
        try:
            registry.pending_spend_clear(self.db, self.name, marker)
        except db_mod.DbError as exc:
            log_async("[%s] turn marker not cleared (%s)"
                      % (self.name, db_mod.explain(exc)))
        if spend_key:
            try:
                account_budget.settle(self.db, spend_key, state="released")
            except db_mod.DbError as exc:
                log_async("[%s] account reservation not released (%s)"
                          % (self.name, db_mod.explain(exc)))
        self._compta_travail = None

    def _purge_turn_context(self) -> None:
        """Retire le document du tour ; ne casse jamais un tour.

        Les workers partiels des tests (et l'arrêt d'un worker avant sa
        première réclamation) n'ont pas de dossier d'état : rien à retirer."""
        state_dir = getattr(self, "state_dir", "")
        if not state_dir:
            return
        try:
            turn_context.remove(state_dir)
        except OSError as exc:
            log("[%s] turn context not removed (%s)" % (self.name, exc))

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
        if propre and (not travail or not self._same_marker(travail, propre)):
            # #1469 : le marqueur en base n'est plus celui de ce tour (reprise
            # par un remplaçant, ou déjà réparé) : on règle CE tour par sa propre
            # clé (la ligne est dédoublonnée par `spend_key`), sans toucher au
            # marqueur d'un autre
            travail = dict(propre, model=self._annonce_ram or propre.get("model"))
        if travail:
            # L80a : la mesure résidente doit être durable (marqueur puis flux)
            # avant la ligne ; sinon on laisse le marqueur et le tour suspendu.
            if not self._resident_usage_durable(travail):
                self._compta_en_echec = True
                log_async("[%s] resident usage not durable: turn accounting "
                          "left pending" % self.name)
                return
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
        # L165 (EP1) : un rôle élu ne se déplace pas par handoff ; un autre
        # hôte candidat reprend le bail expiré, sans preuve de source.
        if self.agent.get("releve") == "election":
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
        except (db_mod.DbError, handoffs.HandoffError, ValueError, OSError, subprocess.SubprocessError):
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
        pause ; les autres attendent simplement que la pression redescende.

        #2814 : le texte dit la portée (`own cgroup`/`host`) et, pour un tour
        différé, son âge et la prochaine lecture — un client peut alors
        afficher « queued » au lieu de conclure à l'échec."""
        from . import resources as resources_mod
        if (not pressure.get("critical")
                or int(self.agent.get("priority") or 0) > 0):
            self._host_pressure_hold(pressure)
            return
        waiting = self._work_waiting()
        queued_s = self._queue_since(waiting)
        capacity = self._capacity_request(pressure, queued_s) if waiting else None
        # pas d'âge dans le texte critique : il resterait constant, et la
        # pause n'est posée qu'une fois par motif
        raisons = resources_mod.queue_text(
            self.runner.host, pressure,
            retry_s=pressure.get("retry_in_s", resources_mod.QUEUE_RETRY_S),
            capacity=capacity)
        if waiting:
            self.runner.pressure_delayed(self.name)
        texte = "critical " + raisons
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
        self._pressure_text_at = time.time()
        self._fil_note("Pause: %s. No new turn until the pressure goes back "
                       "down; the current turn is never interrupted." % texte,
                       meta={"action": "pression-hote", "hote": self.runner.host})

    def _work_waiting(self) -> bool:
        """Une consigne ou du courrier attend ce worker (L31b)."""
        if self.agent.get("pending_prompt"):
            return True
        try:
            # L125 : un courrier passif n'attend pas de tour
            return bool(self._live_mail(mail.unread_active(self.db, self.name, limit=1)))
        except db_mod.DbError:
            return False

    def _live_mail(self, rows) -> list[dict]:
        """#2814 : les messages qui peuvent encore ouvrir un tour.

        Une requête dont l'échéance EXPLICITE (`payload.request_expires_ts`,
        posée par `--request-expires`) est passée ne démarre JAMAIS : elle est
        soldée sans tour et l'EXPÉDITEUR reçoit un événement qui dit l'échéance
        (le message reste dans l'historique et le fil). La TTL de signature
        (`signature_expires_at`) n'est PAS une échéance de requête : un ordre
        signé non lu ne disparaît pas parce que sa signature a vieilli. Un
        échec de base laisse le message en place, mais il ne passe pas non
        plus : on ne démarre pas après l'échéance, même si le solde a échoué."""
        rows = list(rows or ())
        expired = [row for row in rows if mail.request_expired(row)]
        if not expired:
            return rows
        # #2825 N5: a settlement failure leaves the message queued, so the
        # next poll sees it again. The sender is told ONCE per message; the
        # settlement is retried without repeating the notice.
        notified = getattr(self, "_expired_notified", None)
        if not isinstance(notified, set):
            notified = self._expired_notified = set()
        for row in expired:
            mid = row.get("id")
            key = int(mid) if mid is not None else None
            if key is not None and key in notified:
                continue
            if self._notify_expired_request(row) and key is not None:
                notified.add(key)
        try:
            mail.drop_expired(self.db, expired)
        except db_mod.DbError as exc:
            log("[%s] expired request(s) not settled (%s); retried without a "
                "new notice" % (self.name, exc))
        else:
            for row in expired:
                mid = row.get("id")
                if mid is not None:
                    notified.discard(int(mid))
            log_async("[%s] %d request(s) expired before a turn: not started"
                      % (self.name, len(expired)))
        return [row for row in rows if not mail.request_expired(row)]

    def _notify_expired_request(self, row: dict) -> bool:
        """#2814 : dit à l'expéditeur qu'une requête à échéance explicite n'a
        pas été livrée, avant de la solder. Un message de service `ameesh`
        (événement) laisse une trace dans le fil ; jamais pour soi-même.

        Rend True quand l'avis est réglé (envoyé, ou inutile: expéditeur
        absent/soi-même), False quand l'envoi a échoué : l'appelant le
        retentera sans dupliquer un avis déjà parti (#2825 N5)."""
        sender = str(row.get("sender") or "")
        mid = row.get("id")
        if not sender or sender in ("ameesh", self.name):
            return True
        deadline = mail.request_deadline(row)
        when = time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(deadline)) if deadline else "?"
        body = ("request #%s expired at %s before a turn could start: it was not "
                "delivered. Resend it with a new --request-expires deadline if still "
                "needed." % (mid, when))
        try:
            mail.send(self.db, "ameesh", sender, body, kind="event",
                      payload={"request_expired": int(mid) if mid else None},
                      work_item_id=row.get("work_item_id"))
        except Exception as exc:  # a notice never blocks the settlement
            log("[%s] expired request #%s notice not sent (%s)"
                % (self.name, mid, type(exc).__name__))
            return False
        return True

    def _queue_since(self, waiting: bool) -> float:
        """#2814 : âge (s) du premier tour retenu, 0 sans travail en attente.

        L'âge se mesure à l'horloge murale et vit dans le worker : une
        pression qui tombe puis revient repart à zéro, la ligne de statut
        n'accumule pas les épisodes."""
        if not waiting:
            return 0.0
        since = float(getattr(self, "queue_since", 0.0) or 0.0)
        if not since:
            since = time.time()
            self.queue_since = since
        return max(0.0, time.time() - since)

    def _capacity_label(self, state: dict | None) -> str | None:
        """#2814 : le mot du fournisseur dans le statut, jamais un secret."""
        state = state or {}
        if state.get("state") == "releasing":
            return "releasing"
        if state.get("state") == "release_failed":
            return "release pending"
        action = state.get("action")
        return {"granted": "granted", "wait": "requested", "refused": "refused",
                "error": "unavailable"}.get(action)

    def _capacity_request(self, pressure: dict, queued_s: float) -> str | None:
        """Demande une capacité au fournisseur si le budget le permet.

        Sans budget disponible, rien n'est demandé (une extension ne doit pas
        contourner le plafond) ; le fournisseur est no-op par défaut."""
        capacity = getattr(self.runner, "capacity", None)
        if capacity is None or not getattr(capacity, "enabled", False):
            return None
        try:
            if not self.budget_ok():
                return None
        except Exception:
            return None
        try:
            state = capacity.need(
                host=self.runner.host, agent=self.name, verdict=pressure,
                budget_usd_hour=getattr(self.runner, "budget_usd_per_hour", None),
                max_agents=getattr(self.runner, "_host_max_agents", None),
                queued_s=queued_s)
        except Exception as exc:  # jamais fatal au tour suivant
            log_async("[%s] capacity request failed (%s)"
                      % (self.name, type(exc).__name__))
            return None
        return self._capacity_label(state)

    def _host_pressure_hold(self, pressure: dict) -> None:
        """L31b : une attente de pression se VOIT (`ameesh show`, `ameesh list`).

        Sans elle, un agent retenu restait `queued`, erreur vide, sans raison
        lisible. Le statut ne change pas ; seul le texte dit ce qui retient le
        tour, et seulement quand du travail attend. Fencé par le bail ; ne lève
        jamais. Le journal, lui, ne dit l'attente qu'une fois par épisode de
        pression, pour tous les agents (`Runner._pressure_episode_update`).
        Not on a mediated executor: `leases.hold_note` is not in contract 1.1
        (the wait stays silent there, as in 1.6.x).

        #2814 : `waiting: pressure on host X (own cgroup) — …; queued 42 s;
        capacity requested; retry in 10 s` — the `queued` object a client
        reads (`resources.parse_queue_status`)."""
        from . import resources as resources_mod
        if getattr(self.runner, "mediated", False):
            return
        if not self._work_waiting():
            return
        self.runner.pressure_delayed(self.name)
        queued_s = self._queue_since(True)
        capacity = self._capacity_request(pressure, queued_s)
        texte = "waiting: " + resources_mod.queue_text(
            self.runner.host, pressure, queued_s=queued_s,
            retry_s=pressure.get("retry_in_s", resources_mod.QUEUE_RETRY_S),
            capacity=capacity)
        now = time.time()
        if getattr(self, "_pressure_text", "") == texte:
            return
        # L'âge change à chaque sondage : réécrire `status_text` toutes les
        # secondes ferait du bruit en base pour rien. Un texte déjà posé est
        # rafraîchi au plus toutes les dix secondes.
        if getattr(self, "_pressure_text_at", 0.0) \
                and now - self._pressure_text_at < 10.0:
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
            self._pressure_text_at = now

    def _host_pressure_release(self) -> None:
        """L31b : la pression est retombée — l'attente ou la pause s'efface,
        si son texte est toujours en place (personne ne l'a remplacé) ; le texte
        d'avant (« reprise : même session »…) revient."""
        self._pause_refused = ""
        self.queue_since = 0.0
        self._pressure_text_at = 0.0
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

    # -- crédits du compte fournisseur (#4164) -------------------------------
    def credits_ready(self) -> bool:
        """Garde de `pick()` : le compte du prochain tour a-t-il du crédit ?

        Compte `exhausted` : aucun tour, blocage `provider_credits_exhausted`
        (fencé), consigne gardée, ni échec rapide ni arrêt — sauf la sonde
        bornée (un tour par compte et par `provider_credits.PROBE_S`). Ici la
        sonde échue est seulement **candidate** : `pick()` ne la réserve
        qu'une fois un tour choisi (B1 cx2 : un agent sans travail ne la
        consomme pas). Compte `ok` de nouveau : blocage levé."""
        self._credits_probe = None
        self._credits_candidate = None
        if not self._credits_tracked():
            return True
        compte = account_turn.for_turn(self)
        if compte is account_turn.INVALID:
            return True
        harness = self.turn_harness()
        account = auth_failure.account_name(compte)
        try:
            row = provider_credits.exhausted(self.db, harness, account)
        except db_mod.DbError as exc:
            # garde ouverte sur panne de lecture : l'échec du tour, lui,
            # reclassera le compte
            log_async("[%s] provider credits unreadable (%s): turn allowed"
                      % (self.name, db_mod.explain(exc)))
            return True
        if not row:
            return self._credits_release()
        echeance = row.get("next_probe_ts")
        if echeance is None or float(echeance) <= time.time():
            # la base reste seule juge de l'échéance (`claim_probe`, dans pick)
            self._credits_candidate = (harness, account, row.get("reason") or "")
            return True
        self._credits_block(harness, account, row.get("reason") or "")
        return False

    def _credits_tracked(self) -> bool:
        """#4164 : l'état partagé des crédits s'applique-t-il à ce worker ?
        Non en simulation, ni pour l'exécuteur médié (pas d'état partagé du
        compte), ni pour une construction partielle documentée (`__new__`
        sans `__init__`, doublures #811/#1270 : ni `cfg` ni exécuteur
        complet) — avis B ameesh-dev3."""
        runner = getattr(self, "runner", None)
        if getattr(self, "cfg", None) is None or runner is None:
            return False
        return not (getattr(runner, "dry_run", False) or getattr(runner, "mediated", False))

    def _credits_exhausted(self) -> bool:
        """#4164 : le compte du prochain tour est-il épuisé ? Lecture seule
        (ni sonde ni blocage), pour les appels de modèle hors `pick` (résumé
        de rotation). Ouvert sur panne de lecture, comme `credits_ready`."""
        if not self._credits_tracked():
            return False
        compte = account_turn.for_turn(self)
        if compte is account_turn.INVALID:
            return False
        try:
            return bool(provider_credits.exhausted(
                self.db, self.turn_harness(), auth_failure.account_name(compte)))
        except db_mod.DbError:
            return False

    def _credits_claim(self) -> bool:
        """#4164 (B1 cx2) : un tour est choisi alors que le compte est épuisé
        et sa sonde échue — la réserve atomiquement (un seul agent par
        échéance) et lève le blocage. Faux si un autre agent l'a prise, ou
        si le bail est perdu : le tour ne part pas."""
        candidat, self._credits_candidate = getattr(self, "_credits_candidate", None), None
        if not candidat:
            return True
        harness, account, reason = candidat
        try:
            pris = provider_credits.claim_probe(self.db, harness, account)
        except db_mod.DbError as exc:
            log_async("[%s] probe claim unreadable (%s): no turn"
                      % (self.name, db_mod.explain(exc)))
            pris = False
        if not pris:
            self._credits_block(harness, account, reason)
            return False
        self._credits_probe = (harness, account)
        log_async("[%s] %s: probe turn on %s (one every %ds)"
                  % (self.name, provider_credits.REASON,
                     provider_credits.who(harness, account),
                     int(provider_credits.PROBE_S)))
        # le tour choisi a souvent déjà remplacé la marque (consigne prise :
        # `running`) — « kept » n'est pas un refus ; seul le bail perdu l'est
        self._credits_release(probe=True)
        if self.lease_lost.is_set():
            self._credits_probe = None
            return False
        return True

    def _credits_block(self, harness: str, account: str, reason: str) -> None:
        """Pose (fencé) le blocage d'un agent dont le compte est épuisé ;
        journal et fil une fois par blocage."""
        texte = provider_credits.status_text(harness, account)
        agent = self.agent or {}
        if texte == self._credits_status and agent.get("status") == "blocked" \
                and agent.get("status_text") == texte:
            return
        if self._credits_status and self._credits_status != texte:
            registry.clear_marked_block(self.db, self.name, self.runner.runner_id,
                                        self.epoch, self._credits_status,
                                        provider_credits.STATUS_PREFIX)
            self._credits_status = ""
        issue = registry.set_marked_block(
            self.db, self.name, self.runner.runner_id, self.epoch, texte,
            "%s %s" % (provider_credits.STATUS_PREFIX, reason),
            provider_credits.STATUS_PREFIX)
        if issue == "lease":
            self.lease_lost.set()
            return
        if issue == "kept":
            return
        self._credits_note(harness, account, reason, texte)

    def _credits_note(self, harness: str, account: str, reason: str, texte: str,
                      turn: bool = False) -> None:
        self._credits_status = texte
        nom = provider_credits.who(harness, account)
        log_async("[%s] %s (%s) — no turn until the account has credits again "
                  "(probe every %ds)" % (self.name, texte, reason,
                                         int(provider_credits.PROBE_S)))
        self._fil_note(
            "%s: provider account %s has no credits left (%s). The instruction stays "
            "requested, with no retry loop and no fast failure; one probe turn per "
            "%d min checks the account, and the agent resumes on its own once it "
            "has credits again." % ("Turn failed" if turn else "Turn held", nom, reason,
                                    int(provider_credits.PROBE_S // 60)),
            meta={"action": provider_credits.REASON, "harnais": harness,
                  "compte": account, "raison": reason,
                  "source": "tour" if turn else "garde"})

    def _credits_release(self, probe: bool = False) -> bool:
        """Compte `ok` (ou sonde accordée) : le blocage encore posé est levé."""
        if not self._credits_status:
            return True
        texte = self._credits_status
        issue = registry.clear_marked_block(self.db, self.name, self.runner.runner_id,
                                            self.epoch, texte,
                                            provider_credits.STATUS_PREFIX)
        if issue == "lease":
            self.lease_lost.set()
            return False
        self._credits_status = ""
        if issue == "kept":
            row = registry.get(self.db, self.name)
            if row is not None:
                self.agent = row
            return False
        if not probe:
            log_async("[%s] %s lifted: the account has credits again — resuming"
                      % (self.name, texte))
            self._fil_note("Block %s lifted: the provider account has credits again, "
                           "the agent resumes." % texte,
                           meta={"action": "provider_credits_reprise"})
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
            cost_usd=cost, turn_id=self._turn_id)
        self.last_activity = time.monotonic()
        self._turn_end_mono = self.last_activity  # #3451
        return True

    def _pick_for_turn(self) -> dict | None:
        """Le tour choisi (`pick`), passé dans la session de son lot
        (`rotate_for_lot`).

        #2614 : un réveil programmé réclamé par `pick` mais qui ne part pas
        dans ce tour — écarté après le choix (dossier du lot), bascule de
        session abandonnée, base tombée pendant la bascule — est oublié ici :
        le claim n'est plus renouvelé, expire, et le réveil revient (#2466 B1)."""
        spec = None
        try:
            spec = self.rotate_for_lot(self.pick())
            return spec
        finally:
            if not (spec and spec.get("scheduled_wake")):
                self._forget_wake_claim()

    def pick(self) -> dict | None:
        """Le prochain tour à faire, ou None. Consomme la consigne en attente.

        Le tour porte son lot (`lot`, L26) quand il est déductible."""
        self._due_at = None  # #3218 : l'échéance se relit à chaque passe
        if self._attach_pending():
            self._servir(())  # #2810 : aucun tour ne part
            return None
        # #2810 : entre deux tours, rien n'est servi — un NOTIFY pour le
        # message du tour précédent reste réglé par la génération de #2729.
        self._servir(())
        # #2729 : frontière d'un tour ordinaire, prise AVANT la sélection.
        # Une demande qui entre pendant `_pick` — après la lecture des
        # urgents, donc hors de ce que le tour choisi sert — ne doit pas être
        # effacée au début du tour : elle l'arrête.
        frontiere = _preemption_generation(self)
        # #4164 (B2 cx2) : les reprises que `_pick` consomme en choisissant
        # (suite après plafond, reprise d'attach, relance), rendues si la
        # sonde du compte épuisé est perdue et que le tour ne part pas
        reprises = (getattr(self, "suite", None), getattr(self, "_reprise_attach", False),
                    getattr(self, "nudged", False))
        spec = self._pick()
        if spec is None:
            self._credits_candidate = None  # aucun tour : la sonde reste libre
        if spec is not None:
            # Le tour urgent, lui, sert la demande qui l'a fait choisir : sa
            # frontière est relue après la sélection, sinon il se tuerait
            # lui-même. Toute demande postérieure arrête le tour.
            if spec.get("kind") == "urgent":
                frontiere = _preemption_generation(self)
            # #408 : le réveil sur un lot actionnable porte déjà son lot
            spec["lot"] = spec.get("lot") or self.turn_lot(spec)
            if self._lot_workdir_blocked(spec.get("lot")):
                # #811 (B1) : ne pas relancer en boucle le tour d'un lot
                # secondaire sans dossier ; la consigne repart en attente.
                if spec.get("kind") == "prompt":
                    registry.restore_prompt(self.db, self.name, self.runner.runner_id,
                                            self.epoch)
                self._servir(())  # #2810 : ce tour ne partira pas
                self._credits_candidate = None
                return None
            # #4164 (B1 cx2) : la sonde d'un compte épuisé n'est réservée que
            # pour un tour réellement choisi ; perdue (autre agent), la
            # consigne repart en attente comme ci-dessus.
            if not self._credits_claim():
                if spec.get("kind") == "prompt":
                    registry.restore_prompt(self.db, self.name, self.runner.runner_id,
                                            self.epoch)
                self.suite, self._reprise_attach, self.nudged = reprises
                self._servir(())
                return None
            # transportée par le spec : `rotate_for_lot` et les copies de
            # `_run_guarded_turn` la gardent (dict(spec, …)).
            spec["_preempt_gen"] = frontiere
        return spec

    def _pick(self) -> dict | None:
        agent = registry.get(self.db, self.name)
        if agent is None or agent.get("status") == "stopped":
            if agent is not None:
                log("[%s] stopped in the registry" % self.name)
            self.stopping.set()
            return None
        self.agent = agent
        if agent.get("operator_paused"):
            # L87b : rien n'est consommé, aucun tour ne démarre. L232a n'est pas
            # un tour : un drainage d'hôte garde le droit de réserver puis de
            # clore le passage de ce worker, sinon le bail source reste tenu et
            # l'hôte ne se vide pas tant que la pause tient (N1 de la R2 #1462).
            self.maybe_relocate({})
            return None
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
        # #4164 : le compte du tour n'a plus de crédit chez le fournisseur —
        # ni consigne ni courrier consommés (le tour reste demandé), sauf la
        # sonde bornée ; reprise d'elle-même quand le compte repasse `ok`.
        if not self.credits_ready():
            return None
        # L30 : session ouverte sous un autre compte et non reprenable → résumé
        # sous l'ancien compte, avant de consommer quoi que ce soit (jamais en
        # plein tour).
        if not account_turn.continuity(self):
            return None  # #389 r2 : rotation requise mais annulée, rien consommé
        # #811 (B1/R3) : un urgent d'un lot retenu attend comme les autres, et
        # le préfixe retenu ne le masque plus ; les messages primaires passent.
        from . import lot_workdir
        # Une construction partielle documentée (`__new__` sans `__init__`,
        # doublures de test #1270) n'a pas de `cfg` : sans lui, la lecture du
        # courrier reste simple — aucun canon à charger, rien à retenir.
        cfg = getattr(self, "cfg", None)
        urgents = self._live_mail(lot_workdir.deliverable_unread(
            cfg, self.db, self.agent, besoin=50, mode="urgent",
            deferred=self._lot_deferred))
        autorises = [m for m in urgents if self.interrupt_allowed(m)]
        for refuse in (m for m in urgents if m not in autorises):
            mid = int(refuse.get("id") or 0)
            if mid and mid not in self._refus_vus:
                self._refus_vus.add(mid)
                log("[%s] urgent from %s without interruption: sender not allowed "
                    "to interrupt" % (self.name, refuse.get("sender") or "?"))
        if autorises:
            # L77 rang 1 : un message humain vérifié et urgent ne se mélange
            # jamais au courrier ; il passe avant tout le reste et porte
            # l'enveloppe. Les humains non vérifiés restent au rang 2.
            humans_urgent = [m for m in autorises if self._from_human(m)]
            if humans_urgent:
                self.nudged = False
                return self._human_spec("urgent", humans_urgent)
        # Rang 2 (#286/B4) : les messages d'humains ordinaires passent AVANT
        # le courrier urgent (rang 3), la suite et la consigne en attente
        # (rang 4). On lit tout le courrier ici pour les trouver, y compris
        # derrière un préfixe retenu ; les rangs suivants réutilisent la lecture.
        page, page_tronquee = lot_workdir.deliverable_unread(
            cfg, self.db, self.agent, besoin=mail.UNREAD_LIMIT,
            deferred=self._lot_deferred, troncature=True)
        # L77/B13 : le déclencheur porte sur la page LUE, avant `_live_mail` —
        # une requête expirée retirée de la page ne doit pas refermer la
        # recherche humaine (200 lus, 199 vivants : le préfixe était plein).
        # L77/B14 : une lecture tronquée par la borne du backend (médié :
        # 200 lignes sans décalage) laisse peut-être un humain au-delà du
        # préfixe, même si peu de lignes y sont remisables (lots retenus).
        page_pleine = page_tronquee or len(page) >= mail.UNREAD_LIMIT
        messages = self._live_mail(page)
        if page_pleine and all(mail.passive_reason(m) for m in messages):
            # L125 : boîte pleine de courrier passif — les messages qui
            # réveillent, plus récents, ne doivent pas y rester invisibles
            reveil_page = lot_workdir.deliverable_unread(
                cfg, self.db, self.agent, besoin=mail.UNREAD_LIMIT,
                mode="active", deferred=self._lot_deferred)
            page_pleine = page_pleine or len(reveil_page) >= mail.UNREAD_LIMIT
            reveil = self._live_mail(reveil_page)
            messages = reveil + messages
        humans = [m for m in messages
                  if self._from_human(m) and not mail.passive_reason(m)]
        if not humans and page_pleine:
            # L77/B13 : le préfixe de `UNREAD_LIMIT` peut être saturé par du
            # courrier ordinaire actif devant un humain. Recherche dédiée,
            # filtrée dans la requête et soumise aux mêmes lots retenus
            # (`deferred`), AVANT les rangs 3 à 5 : un humain ne se cache ni
            # derrière un urgent d'agent, ni derrière la suite ou la consigne
            # en attente.
            from .mediated_executor.contract import NotSupportedRemotely
            try:
                humains_profonds = self._live_mail(lot_workdir.deliverable_unread(
                    cfg, self.db, self.agent, besoin=mail.UNREAD_LIMIT,
                    mode="human", deferred=self._lot_deferred,
                    human_names=getattr(cfg, "human_names", None)))
            except NotSupportedRemotely as exc:
                # Contrat 1.5 absent (ancien serveur) : lire comme avant 1.5,
                # journalisé une fois, sans tuer le fil de l'agent à chaque
                # page pleine (déploiement serveur d'abord, documenté).
                if not getattr(self, "_humains_profonds_indisponibles", False):
                    self._humains_profonds_indisponibles = True
                    log("[%s] deep human search unavailable (%s): old executor "
                        "server, reading without it" % (self.name, exc))
                humains_profonds = []
            humans = [m for m in humains_profonds
                      if self._from_human(m) and not mail.passive_reason(m)]
        if humans:
            self.nudged = False
            self._reprise_attach = False
            return self._human_spec("mail", humans)
        if autorises:
            # Rang 3 : le courrier urgent d'un expéditeur habilité (agent,
            # système vérifié) passe avant la suite et la consigne en attente.
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
        # #2466 : un rendez-vous explicite passe avant la fenêtre de
        # regroupement du courrier ordinaire (le hook livrera le reste).
        reveil = self._scheduled_wake_spec()
        if reveil is not None:
            self.nudged = False
            self._reprise_attach = False
            return reveil
        # #2814 (fusionné) : l'unique lecture du lot (plus haut, rang 2) porte
        # déjà `_live_mail` ; ce bloc d'origine de `_pick` est supprimé par
        # #286, qui l'a déplacé avant les rangs 3/4 — aucun doublon.
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
        if self.retry_due():
            # #1270 : échéance d'une reprise cadencée sans consigne en attente
            return {"kind": "idle", "prompt": adapters.RETRY_PROMPT, "ids": []}
        if all(mail.passive_reason(m) for m in messages):
            # #408 : un courrier qui réveillera l'agent passe avant (fenêtre L125)
            reveil = self._lot_wake_spec()
            if reveil is not None:
                return reveil
        if self.idle_due():
            return {"kind": "idle", "prompt": self._idle_prompt(), "ids": []}
        return None

    def _idle_prompt(self) -> str:
        """#3033/#3288/#3273 : la relance d'inactivité porte les corrections et
        les avis que l'agent doit encore rendre, et nomme le pilote de son
        équipe (#3273). Sans elle, un relecteur dont la demande de désignation
        était un courrier déjà lu se déclarait « disponible » à
        l'orchestrateur pendant que son avis attendait (#3004, 1 j 14 h), et
        un auteur bloqué continuait sur d'autres lots (#3033 22 h, #3195 9 h).
        La correction due passe en premier : c'est elle qui débloque la revue.
        #3307 : corrections et avis dus précèdent la consigne de reprise, qui
        finit par « nothing to do … stop » : l'en-tête des avis les place
        « before declaring yourself available ».
        Une file illisible ne change pas la relance : le brief de reprise la
        dit, et un tour au repos ne part pas sur une lecture ratée."""
        from . import reprise
        parts: list[str] = []
        try:
            fixes = reprise.fixes_owed_lines(self.db, self.name)
        except Exception:  # noqa: BLE001 - la relance part quand même
            fixes = []
        if (fixes and fixes != [reprise.NO_FIX_OWED]
                and not fixes[0].startswith("(fixes owed unreadable")):
            parts += [adapters.IDLE_FIXES_HEADER] + fixes + [""]
        try:
            lines = reprise.pending_review_lines(self.db, self.name)
        except Exception:  # noqa: BLE001 - la relance part quand même
            lines = []
        if (lines and lines != [reprise.NO_PENDING_REVIEW]
                and not lines[0].startswith("(pending reviews unreadable")):
            parts += [adapters.IDLE_REVIEWS_HEADER] + lines + [""]
        parts.append(adapters.idle_prompt(self._idle_pilot()))
        return "\n".join(parts)

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

    #: #2466 : intervalle minimal entre deux lectures des réveils programmés
    SCHEDULED_WAKE_CHECK_S = 10.0
    #: #2466 : bail de livraison d'un réveil ; non consommé (drainage, arrêt,
    #: panne avant lancement), il expire et le réveil est délivré de nouveau.
    #: #2614 : renouvelé tant que le claim est tenu — un lancement plus long
    #: que le bail ne le laisse plus expirer.
    WAKE_CLAIM_TTL = 120.0
    #: #2614 : pas du renouvellement, un quart de bail
    WAKE_CLAIM_RENEW_S = WAKE_CLAIM_TTL / 4.0

    def _hold_wake_claim(self, taken: dict) -> None:
        """#2614 : tient le bail du réveil réclamé, jusqu'à son oubli.

        Le bail de 120 s couvrait le choix du tour, pas la suite : bascule de
        session du lot (dossier, brief), contrôle du placement, puis
        préparation du tour (READY, résident). Au-delà, le claim expirait et
        le réveil pouvait être délivré une seconde fois avant la consommation
        du premier. Un fil le renouvelle (`claim_wake`, `claim_ttl_s=0`) tous
        les quarts de bail, de la réclamation jusqu'à `_forget_wake_claim` :
        consommation, fin du tour qui le porte, ou abandon avant le tour
        (`_pick_for_turn`, `_moved_before_turn`). Un claim oublié sans
        consommation expire et le réveil revient (#2466 B1)."""
        self._forget_wake_claim()
        claim = {"lot": int(taken["id"]), "wake_ts": float(taken["wake_ts"]),
                 "done": threading.Event()}
        self._wake_claim = claim
        threading.Thread(target=self._keep_wake_claim, args=(claim,), daemon=True,
                         name="wake-claim-%s" % self.name).start()

    def _forget_wake_claim(self) -> None:
        """#2614 : le claim tenu n'est plus renouvelé (son fil s'arrête)."""
        claim, self._wake_claim = getattr(self, "_wake_claim", None), None
        if claim is not None:
            claim["done"].set()

    def _keep_wake_claim(self, claim: dict) -> None:
        """#2614 : fil de `_hold_wake_claim`. Il s'arrête à l'oubli du claim,
        à l'arrêt du worker ou à la perte de son bail (seul le détenteur
        renouvelle), et dès que l'échéance n'est plus la sienne."""
        while not claim["done"].wait(self.WAKE_CLAIM_RENEW_S):
            if (self.stopping.is_set() or self.lease_lost.is_set()
                    or not self._renew_wake_claim(claim)):
                break
        if self._wake_claim is claim:
            self._forget_wake_claim()

    def _renew_wake_claim(self, claim: dict) -> bool:
        """#2614 : un renouvellement du bail de `claim`. Faux quand il n'y a
        plus rien à tenir (échéance consommée, annulée ou reposée, lot
        réassigné ou fini) ; une base en panne laisse le claim tel quel, le
        quart de bail suivant réessaie."""
        try:
            taken = storage.of(self.db).lot_sessions.claim_wake(
                self.name, int(claim["lot"]), time.time(), 0.0)
        except db_mod.DbError as exc:
            log_async("[%s] scheduled wake claim for lot #%s not renewed (%s)"
                      % (self.name, claim["lot"], exc))
            return True
        return bool(taken) and abs(float(taken["wake_ts"]) - claim["wake_ts"]) < 0.001

    def _scheduled_wake_spec(self) -> dict | None:
        """#2466 : réveil programmé échu d'un lot de l'agent.

        Une échéance posée par `ameesh work wake` (ou la note `hold:` d'un
        rendez-vous, `--wake-at`) est un rendez-vous explicite : elle réveille
        l'assigné même si le lot est tenu par une note `hold:` ou n'est pas
        actionnable (`waiting_human`, `blocked`). La délivrance est un **bail**
        (`claim_wake`, `wake_claim_at`) et non une consommation : l'échéance
        reste armée jusqu'au lancement effectif du harnais, et un claim non
        consommé expire puis est délivré de nouveau — un drainage ou un arrêt
        entre le choix du tour et son lancement ne perd plus le rendez-vous
        (#2466 B1). Le claim est tenu jusqu'au lancement (`_hold_wake_claim`,
        #2614) ; le lot reçoit une ligne `wake:` au journal à la consommation,
        témoin d'un réveil réellement délivré. Le réglage `lot_wake=off` ne l'éteint pas : c'est un geste
        posé, pas une relance d'inactivité. Les gardes de `_pick` (arrêt,
        budget, porte et pression d'hôte, authentification) sont déjà passées ;
        un lot sans dossier reste armé (#811). Sur un exécuteur médié, les
        trois opérations passent par le contrat
        (`lot_sessions.due_wakes/claim_wake/consume_wake`, #2613)."""
        if time.monotonic() - getattr(self, "_scheduled_wake_checked",
                                      float("-inf")) < self.SCHEDULED_WAKE_CHECK_S:
            return None
        self._scheduled_wake_checked = time.monotonic()
        st = storage.of(self.db).lot_sessions
        now = time.time()
        try:
            for row in st.due_wakes(self.name, now, self.WAKE_CLAIM_TTL):
                if self._lot_workdir_blocked(row["id"]):
                    continue
                taken = st.claim_wake(self.name, int(row["id"]), now,
                                      self.WAKE_CLAIM_TTL)
                if not taken:
                    continue
                self._hold_wake_claim(taken)
                when = time.strftime("%Y-%m-%d %H:%M %Z",
                                     time.localtime(float(taken["wake_ts"])))
                log_async("[%s] scheduled wake for lot #%s claimed (due %s)"
                          % (self.name, taken["id"], when))
                return {"kind": "idle", "ids": [], "lot": str(taken["id"]),
                        "scheduled_wake": {"lot": str(taken["id"]),
                                           "wake_ts": float(taken["wake_ts"])},
                        "prompt": adapters.SCHEDULED_WAKE_PROMPT % {
                            "id": int(taken["id"]), "title": taken.get("title") or "",
                            "state": taken["state"], "when": when}}
        except db_mod.DbError as exc:
            log_async("[%s] scheduled wakes unreadable (%s)" % (self.name, exc))
        return None

    def _consume_scheduled_wake(self, spec: dict) -> None:
        """#2466 : le harnais du tour est parti — l'échéance est consommée.

        Appelé au lancement (`Popen` ou processus résident) : si le tour n'a
        pas été lancé (drainage, garde, dossier, session), le claim reste et
        expirera, puis le réveil sera délivré de nouveau — jamais perdu. Une
        consommation impossible laisse le claim expirer : un doublon signalé
        plutôt qu'une perte."""
        reveil = spec.get("scheduled_wake")
        if not reveil or not getattr(self, "_turn_launched", False):
            return
        self._forget_wake_claim()  # #2614 : plus de bail à renouveler
        lot = int(reveil["lot"])
        try:
            if storage.of(self.db).lot_sessions.consume_wake(
                    self.name, lot, float(reveil["wake_ts"])):
                log_async("[%s] scheduled wake for lot #%s consumed"
                          % (self.name, lot))
            else:
                # #5006 : réassigné pendant le lancement ; `consume_wake` laisse
                # l'échéance au nouvel assigné, le courrier `reassigned` atteint
                # ce tour à son prochain point sûr.
                log_async("[%s] scheduled wake for lot #%s not consumed: no longer "
                          "ours, the turn already started" % (self.name, lot))
        except db_mod.DbError as exc:
            log_async("[%s] scheduled wake for lot #%s not consumed (%s): it will "
                      "be redelivered when its claim expires"
                      % (self.name, lot, exc))

    def _wake_outdated(self, spec: dict) -> bool:
        """#5006 : le réveil porté par ce tour est-il devenu sans objet ?

        Assigné, état et échéance sont lus au choix du tour ; la bascule de
        session et la préparation peuvent durer, et une réassignation tombée
        entre-temps enverrait le tour à l'ancien assigné. Réveil programmé : le
        renouvellement du bail relit tout (assigné, état, échéance), par une
        opération déjà au contrat de l'exécuteur médié. Relance d'inactivité :
        le lot doit encore figurer dans les lots actionnables de l'agent. Une
        base en panne ne dit rien : le tour continue, et `consume_wake` reste
        filtré par l'assigné."""
        lot = spec.get("lot")
        reveil = spec.get("scheduled_wake")
        if reveil:
            perime = not self._renew_wake_claim(
                {"lot": int(reveil["lot"]), "wake_ts": float(reveil["wake_ts"])})
        elif spec.get("lot_wake"):
            import datetime as _dt
            try:
                lots = storage.of(self.db).lot_sessions.actionable(
                    self.name, today=_dt.date.today().isoformat())
            except db_mod.DbError as exc:
                log_async("[%s] actionable lots unreadable (%s)" % (self.name, exc))
                return False
            perime = str(lot) not in {str(row["id"]) for row in lots}
        else:
            return False
        if perime:
            log_async("[%s] wake for lot #%s outdated (reassigned, closed or moved): "
                      "no turn" % (self.name, lot))
        return perime

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
                return {"kind": "idle", "ids": [], "lot": str(row["id"]), "lot_wake": True,
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
        self._marque_vu(active)
        events = [m for m in active if mail.is_event(m)]
        others = [m for m in active if not mail.is_event(m)]
        mail_due = bool(others) and self._mail_due(others)
        if others and any(self._from_human(m) for m in others):
            # L81 (étude §6.3) : tant qu'un message humain attend, l'entretien
            # d'une session résidente lui laisse la place (fenêtre de calme).
            self._dernier_humain = time.monotonic()
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
        if (any(mail.is_urgent(m) and self.interrupt_allowed(m) for m in events)
                or fenetre <= 0 or (time.time() - dernier) >= fenetre):
            return True
        self.note_due_at(dernier + fenetre)  # #3218 : réveil à la fin de la fenêtre
        return False

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
        """L125 : message d'un humain — marqué à l'envoi, expéditeur qualifié
        `human:<id>`, ou nom déclaré humain sur cet hôte (`AMEESH_HUMANS`)."""
        return (mail.is_human(row)
                or (row.get("sender") or "") in getattr(self.cfg, "human_names", ()))

    def _mail_due(self, rows: list[dict]) -> bool:
        """L125 : le courrier ordinaire en attente ouvre-t-il un tour ?

        Oui si la fenêtre est nulle, si l'un des messages est urgent (réveil
        sans délai, quel que soit l'expéditeur ; seul un expéditeur habilité
        INTERROMPT un tour en cours, 0018) ou d'un humain, ou si le plus
        ancien attend depuis au moins la fenêtre. L'attente se mesure à
        l'horloge de la base (`created_ts`) et depuis que cet exécuteur a vu
        le message (horloge monotone) : un décalage d'horloge entre l'hôte et
        la base ne retarde jamais un message de plus d'une fenêtre.

        #3451 : la fenêtre court aussi depuis la fin du dernier tour de cet
        exécuteur — un message arrivé pendant un long tour n'en rouvre pas un
        2 à 6 s après le précédent, il attend que la fenêtre ait regroupé ce
        qui suit. Le retard ajouté est au plus d'une fenêtre ; l'urgent et
        l'humain n'attendent jamais.
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
        fin_tour = getattr(self, "_turn_end_mono", None)
        if fin_tour is not None:
            waited = min(waited, mono - fin_tour)
        if waited >= window:
            return True
        # #3218 : le worker se réveille à la fin de la fenêtre, pas au sondage
        # qui la suit (jusqu'à `poll` de plus)
        self.note_due_at(now + (window - waited))
        oldest = min(kept)
        if getattr(self, "_batch_logged", None) != oldest:
            self._batch_logged = oldest
            log_async("[%s] mail batched: %d message(s), turn in %ds at most "
                      "(window %gs)" % (self.name, len(rows), int(window - waited) + 1,
                                         window))
        return False

    # -- #3218 : échéance de réveil et reçu d'étapes ------------------------
    def note_due_at(self, ts: float) -> None:
        """Retient l'instant (epoch) où une attente de cette passe se lève
        d'elle-même (fin de fenêtre de regroupement, écart de début du
        compte) : l'attente au repos s'arrête là, pas au sondage suivant."""
        actuel = getattr(self, "_due_at", None)
        if actuel is None or ts < actuel:
            self._due_at = float(ts)

    def _until_due(self, cap: float) -> float:
        """`cap`, raccourci jusqu'à l'échéance retenue (consommée) ; une marge
        de 50 ms laisse l'échéance passée au moment de la relecture."""
        due, self._due_at = getattr(self, "_due_at", None), None
        if due is None:
            return cap
        return max(0.05, min(cap, due - time.time() + 0.05))

    def _marque_vu(self, messages) -> None:
        """L'instant (horloge de l'hôte) où cet exécuteur a lu chaque message
        actif pour la première fois ; les messages remis sont oubliés."""
        now = time.time()
        vus = getattr(self, "_mail_vu", {})
        self._mail_vu = {int(m["id"]): vus.get(int(m["id"]), now)
                         for m in messages if m.get("id") is not None}

    def _retenue_debut(self, nature: str) -> None:
        """Un tour dû est retenu avant l'admission (budget, fenêtre courte)."""
        if getattr(self, "_retenue", None) is None:
            self._retenue = (time.time(), nature)

    def _retenue_fin(self) -> None:
        retenue, self._retenue = getattr(self, "_retenue", None), None
        if retenue is not None:
            passees = getattr(self, "_retenues", [])[-7:]
            self._retenues = passees + [(retenue[0], time.time(), retenue[1])]

    def _etapes_initiales(self, rows) -> dict:
        """Les premiers jalons du reçu d'un tour de courrier : arrivée du plus
        ancien message servi (horloge de la base), première lecture par cet
        exécuteur, instant où le tour est dû (horloge de l'hôte)."""
        now = time.time()
        vus = getattr(self, "_mail_vu", {})
        crees, lus, ids = [], [], []
        for row in rows:
            if row.get("id") is None:
                continue
            ident = int(row["id"])
            ids.append(ident)
            lus.append(vus.setdefault(ident, now))
            if row.get("created_ts") is not None:
                crees.append(float(row["created_ts"]))
        self._mail_vu = vus
        fin = getattr(self, "_turn_end_mono", None)
        return {"ids": ids, "mail_ts": min(crees) if crees else None,
                "seen_ts": min(lus) if lus else now, "due_ts": now,
                "prev_turn_end_ts": (now - (time.monotonic() - fin))
                if fin is not None else None}

    def _recu_etapes(self, spec: dict) -> None:
        """#3218 : le reçu d'étapes d'un tour de courrier, une fois par tour, à
        la fin du flux du harnais (jamais pendant : le flux n'attend pas la
        base, L78) : événement `turn.stages` de l'agent (`ameesh events`,
        `GET /api/v1/events`). Jamais fatal : un reçu manqué ne retient aucun
        tour."""
        etapes = spec.get("_etapes")
        if etapes is None or etapes.get("sent"):
            return
        etapes["sent"] = True
        try:
            data = turn_stages.receipt(etapes, getattr(self, "_retenues", []))
            data.update(agent=self.name, host=self.cfg.host, kind=spec.get("kind"),
                        harness=self.turn_harness())
            events_mod.record(self.db, turn_stages.EVENT, "agent:" + self.name,
                              project=(self.agent or {}).get("team")
                              or (self.agent or {}).get("chantier") or None, **data)
        except Exception as exc:  # noqa: BLE001 - reçu facultatif
            log_async("[%s] turn stages receipt not written (%s)" % (self.name, exc))

    # -- un tour -----------------------------------------------------------
    def _adapter(self) -> adapters.HarnessAdapter:
        harness = self.turn_harness()
        # #4293 : binaire choisi pour CET agent (canari d'adoption) ; NULL =
        # résolution de l'hôte inchangée. Un tour déjà lancé garde son
        # adaptateur : la ligne est relue au début du tour suivant.
        agent = self.agent or {}
        choisi = agent.get("harness_bin") or None
        if choisi and agent.get("harness") and harness != agent.get("harness"):
            # #4488 : le binaire épinglé est celui du harnais de l'agent. Un
            # tour de repli (L242b) ou le résumé d'une rotation sous l'ancien
            # harnais ne doit ni lancer ce binaire sous un autre adaptateur,
            # ni être bloqué par sa garde : on reprend la résolution de l'hôte.
            choisi = None
        adapter = agent_adapter(harness, choisi, getattr(self.cfg, "host", ""))
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

    def _with_election_brief(self, text: str) -> str:
        """L165 (EP3) : ne jamais livrer une session neuve sans le brief de
        relève en attente, même quand un repli (`_ecarte_session`) a remplacé
        le résumé par un brief générique (verdict cx2 B2, session mise de côté
        inconnue du harnais)."""
        election = getattr(self, "_election_brief", "")
        if not election:
            return text
        if not text:
            return election
        if text == election or election in text:
            return text
        return election + "\n\n" + text

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
            self.cfg, self._with_election_brief(self.resume_summary), agent=self.name,
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
            # L165 (EP3) : un tour qui REPREND une session de lot ne doit pas
            # perdre le brief de relève en attente ; il l'ouvre comme un
            # résumé (le brief sera oublié en fin de tour, `_resume_drop`).
            if getattr(self, "_election_brief", ""):
                return self._election_prompt() + "\n\n"
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

    def _election_prompt(self) -> str:
        """L165 (EP3) : le brief de relève encadré comme un résumé de reprise
        (sans autorité), pour le premier tour d'une session de lot reprise."""
        from . import reprise
        brief = reprise.with_process_rules(
            self.cfg, self._election_brief, agent=self.name,
            canon_id=self._canon_id(),
            host=(self.agent or {}).get("host") or self.cfg.host)
        return adapters.resume_prompt(brief, origin="ameesh")

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
        ids = [int(m["id"]) for m in retenus if m.get("id") is not None]
        # #2810 : le tour choisi sert ces messages. Publié sous le verrou de
        # préemption : le NOTIFY de l'un d'eux, traité après `_pick`, ne
        # l'arrêtera pas ; une demande pour un AUTRE message, elle, reste un
        # arrêt (frontière de #2729, relue après `_pick` pour un urgent).
        self._servir(ids)
        return {
            "kind": kind,
            "prompt": prompt,
            "ids": ids,
            "rows": retenus,
            "candidats": list(messages),
            "remaining": reste,
            "_etapes": self._etapes_initiales(retenus),
        }

    def _envelope_token(self) -> str:
        """Le nonce public de session écrit dans l'enveloppe L77.

        Ce n'est **jamais** le bearer d'authentification (`session_token_for`) :
        l'enveloppe part dans la consigne, l'historique et les journaux. Nonce
        stable pour le bail (agent, runner, epoch), sans aucune autorité.
        """
        graine = "ameesh-l77-envelope|%s|%s|%s" % (
            self.name, self.runner.runner_id, self.epoch)
        return hashlib.sha256(graine.encode("utf-8")).hexdigest()[:32]

    def _human_spec(self, kind: str, messages: list[dict]) -> dict:
        """L77 : un tour dédié aux messages d'humains, avec l'enveloppe ameesh.

        Les humains ne se mélangent jamais au courrier dans la même entrée ;
        `envelopes` est conservé dans le spec pour que `run_turn` rende la
        consigne réservée avec la même enveloppe, dans le même budget.
        """
        from . import resident_queue

        token = self._envelope_token()
        canon_id = self._canon_id()
        envelopes: dict[int, str] = {}
        for row in messages:
            try:
                key = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            ident = resident_queue.identity_for(row.get("sender") or "",
                                                cfg=getattr(self, "cfg", None),
                                                canon_id=canon_id,
                                                proof=mail.human_account(row),
                                                human=True)
            envelopes[key] = resident_queue.envelope(ident, token)
        prompt, retenus, reste = mail.prompt_for(
            self.db, messages, kind=kind, budget=self._budget_courrier(),
            envelopes=envelopes)
        return {
            "kind": kind,
            "prompt": prompt,
            "ids": [int(m["id"]) for m in retenus if m.get("id") is not None],
            "rows": retenus,
            "candidats": list(messages),
            "remaining": reste,
            "envelopes": envelopes,
            "human": True,
            "_etapes": self._etapes_initiales(retenus),
        }

    def _release_session_lock(self) -> None:
        lock = getattr(self, "_session_lock", None)
        if lock is not None:
            self._session_lock = None
            lock.close()

    def _attach_pending(self) -> bool:
        from .platform import session_lock
        if not session_lock.requested(self.state_dir):
            return False
        if not getattr(self, "_session_turn_depth", 0):
            self._close_resident("attach requested")
            self._release_session_lock()
        return True

    def deferred_lock_grace(self) -> float:
        """#1914 : délai de grâce d'un tour différé par un verrou de session
        tenu par un tiers (hors attach). En deçà, c'est le post-tour normal
        d'un résident qui rend le verrou : quelques passations d'attach
        (`attach.HANDOFF_TIMEOUT`), pas de bruit."""
        from . import attach as attach_mod
        return 2.0 * attach_mod.HANDOFF_TIMEOUT

    def monotonic(self) -> float:
        """#1914 : horloge monotone des tours différés. Méthode à part pour que
        la sonde déterministe la remplace sans patcher le module `time` (des
        échéances de base y sont calculées)."""
        return time.monotonic()

    def _deferred_turn(self) -> float:
        """#1914 : un tour est reporté (#1686 N2) — compter les reports
        consécutifs. Au-delà du délai de grâce d'un verrou tenu par un tiers,
        le rendre visible **une fois** (journal + statut marqué) et espacer les
        reprises, au lieu de prendre et rendre la consigne deux fois par
        seconde sans fin. Un attach demandé, lui, garde le pas court : le
        résident doit reprendre la main dès la passation finie. Rend le pas de
        reprise."""
        maintenant = self.monotonic()
        motif = self.deferred_reason
        if self.deferred_series_reason != motif:
            # #2160 : le motif a changé (attach ↔ verrou tenu) — la série
            # précédente est close, sa marque éventuelle levée, et la grâce
            # d'un verrou tenu repart de zéro : un report d'attach (passation
            # normale) qui la précède ne la consomme pas.
            self._deferral_over()
            self.deferred_series_reason = motif
            # #2206 : `_deferral_over` remet `deferred_reason` à vide ; le
            # motif de la série qui commence est rétabli pour que la lecture
            # qui suit (grâce d'un verrou tenu) porte sur ce motif.
            self.deferred_reason = motif
        if self.deferred_since is None:
            self.deferred_since = maintenant
        self.deferred_streak += 1
        if self.deferred_reason != self.DEFERRED_STATUS:
            return self.DEFERRED_RETRY
        if maintenant - self.deferred_since < self.deferred_lock_grace():
            return self.DEFERRED_RETRY
        self.deferred_wait_s = min(self.DEFERRED_RETRY_MAX,
                                   max(2.0 * self.DEFERRED_RETRY,
                                       2.0 * self.deferred_wait_s))
        premier_report = not self.deferred_alerted
        self.deferred_alerted = True
        texte = ("%s on %s: another process holds session-use; turns deferred "
                 "without failure, retries spaced"
                 % (self.DEFERRED_STATUS, self.runner.host or "this host"))
        if premier_report:
            log("[%s] %s (streak %d, %.0fs)" % (self.name, texte, self.deferred_streak,
                                                maintenant - self.deferred_since))
        try:
            # pick() reprend la consigne et ecrit running a chaque essai :
            # reposer la marque apres chaque report, sans repeter le journal.
            # Blocage marqué, fencé par le bail : rien n'est posé sur un agent
            # arrêté ni par-dessus un autre blocage (budget, auth, hôte…).
            registry.set_marked_block(self.db, self.name, self.runner.runner_id,
                                      self.epoch, self.DEFERRED_STATUS, texte,
                                      self.DEFERRED_STATUS)
        except db_mod.DbError as exc:
            log_async("[%s] deferred lock status not written (%s)"
                      % (self.name, db_mod.explain(exc)))
        return self.deferred_wait_s

    def _deferral_over(self) -> None:
        """#1914 : la série de reports est finie (un tour a été lancé, a
        échoué, il n'y a plus rien à prendre, ou le worker s'arrête) — compteur
        remis à zéro et marque de verrou levée si elle est encore la nôtre."""
        if self.deferred_since is None and not self.deferred_alerted:
            return
        etait = self.deferred_alerted
        self.deferred_since = None
        self.deferred_streak = 0
        self.deferred_alerted = False
        self.deferred_wait_s = self.DEFERRED_RETRY
        self.deferred_reason = ""
        self.deferred_series_reason = ""
        if not etait:
            return
        try:
            # Ne lève que la marque posée par ce worker (texte et préfixe
            # exacts, bail contrôlé) : un autre blocage reste en place.
            registry.clear_marked_block(self.db, self.name, self.runner.runner_id,
                                        self.epoch, self.DEFERRED_STATUS,
                                        self.DEFERRED_STATUS)
        except db_mod.DbError as exc:
            log_async("[%s] deferred lock status not cleared (%s)"
                      % (self.name, db_mod.explain(exc)))

    @staticmethod
    def _with_turn_id(spec: dict) -> dict:
        """The one turn id of this turn (#2167 / L2894).

        It is allocated before the first side effect — mail reservation,
        accounting marker, account reservation — and travels in `spec` so
        `mail.reserve` (`meta.consigne.turn`), the marker, the resource row,
        the ledger row, `AMEESH_TURN_ID` and `turn-context.json` name the same
        id. Never allocate another one downstream.
        """
        if spec.get("turn_id"):
            return spec
        return dict(spec, turn_id=uuid.uuid4().hex)

    @migrations.writer_tick
    def run_turn(self, spec: dict) -> bool | None:
        """Un tour (voir `_run_turn_or_defer`).

        #2614 : le tour qui porte un réveil programmé oublie son claim en
        sortant, lancé ou non : non consommé, le bail n'est plus renouvelé,
        expire, et le réveil est délivré de nouveau.

        #2894 : l'identifiant du tour est alloué ici, avant la réservation du
        courrier et tout autre effet de bord, et un document périmé d'un
        processus mort est retiré avant que ce tour puisse écrire le sien."""
        self._purge_turn_context()
        spec = dict(spec, turn_id=uuid.uuid4().hex)
        try:
            return self._run_turn_or_defer(spec)
        finally:
            if spec.get("scheduled_wake"):
                self._forget_wake_claim()

    def _run_turn_or_defer(self, spec: dict) -> bool | None:
        """Exclude attach for the whole turn and life of its resident.

        Rend `True` (tour lancé), `False` (échec) ou `None` quand le tour est
        **différé** sans échec (#1686 N2) : attach demandé ou verrou de session
        tenu ; la consigne est restaurée. Le worker ne compte pas un report
        comme un échec rapide et ne lui applique pas l'attente d'échec.
        #1914 : `deferred_reason` porte le motif du report — le worker ne rend
        visible qu'un verrou tenu par un tiers."""
        self.fast_failure = False
        if (registry.get(self.db, self.name) or {}).get("operator_paused"):
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            self._servir(())  # #2810 : aucun tour ne part
            return False
        from .platform import process, session_lock
        if self._attach_pending():
            self.deferred_reason = "attach requested"
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            self._servir(())  # #2810 : le tour est reporté
            return None
        if getattr(self, "_session_lock", None) is None:
            self._session_lock = session_lock.try_lock(self.state_dir, "session-use")
            if self._session_lock is None:
                self.deferred_reason = "session lock held"
                registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
                self._servir(())  # #2810 : le tour est reporté
                return None
        if self._attach_pending():
            self.deferred_reason = "attach requested"
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            self._servir(())  # #2810 : le tour est reporté
            return None
        self._session_turn_depth = getattr(self, "_session_turn_depth", 0) + 1
        # #2096 : le RSS avant le tour, pour ne rendre les arènes au noyau que
        # si le tour a fait croître le processus (voir `_release_turn_memory`).
        rss_avant = process.rss_bytes()
        try:
            return self._run_guarded_turn(spec)
        finally:
            # #2810 : le tour est fini ; plus aucun message n'est servi. Un
            # NOTIFY tardif reste réglé par la génération de #2729 (une demande
            # antérieure à la sélection suivante est un résidu consommé).
            self._servir(())
            self._session_turn_depth -= 1
            if not self._session_turn_depth:
                try:
                    if session_lock.requested(self.state_dir):
                        self._close_resident("attach requested after turn")
                finally:
                    if self.resident is None:
                        self._release_session_lock()
            # Après le retour de `_run_guarded_turn`, ses gros tampons sont
            # morts (sa trame est détruite) : c'est le moment de rendre.
            self._release_turn_memory(rss_avant)

    def _release_turn_memory(self, rss_before: int | None) -> None:
        """#2096 : rend au noyau la mémoire libre de l'allocateur après un tour
        qui a fait croître le processus (pic de flux, de parsing ou de
        journalisation). L'allocateur garde le plafond du pic alors que ses
        objets sont morts : sans ce retour, le RSS d'un exécuteur ne redescend
        jamais et la somme des exécuteurs fait basculer l'hôte sous pression.

        Best-effort : ne lève jamais, et ne fait rien si la croissance du tour
        reste sous `TURN_TRIM_RSS_GROWTH` (un tour ordinaire ne paie pas le
        geste) ou si la plateforme ne sait pas mesurer/rendre.

        #2107/#2108 : le journal dit le motif exact (`platform.process.TRIM_*`)
        — « rien à rendre » n'est pas une plateforme sans `malloc_trim`."""
        try:
            from .platform import process
            apres = process.rss_bytes()
            if rss_before is None or apres is None or apres - rss_before < TURN_TRIM_RSS_GROWTH:
                return
            croissance = apres - rss_before
            motif = process.trim_free_memory()
            log_async("[%s] turn memory: RSS +%.1f MiB, arenas %s"
                      % (self.name, croissance / (1024.0 * 1024.0),
                         process.TRIM_LABELS.get(motif, motif)))
        except Exception as exc:  # une mesure ratée ne défait pas le tour
            log_async("[%s] turn memory not released (%s)" % (self.name, exc))

    def _reservation_ttl(self, spec: dict) -> float:
        """L77/B6 : durée de la réservation qui porte la consigne du tour.

        Un message humain n'est soldé qu'à la fin du tour : sa réservation
        couvre tout le tour, plus la marge de livraison. Les autres messages
        sont soldés au lancement et gardent le TTL ordinaire.
        """
        if not spec.get("human"):
            return mail.RESERVATION_TTL
        return mail.human_reservation_ttl(self.turn_max_seconds())

    def _run_turn_settled(self, spec: dict, *, close_mail=None) -> bool:
        """Exécute un tour et solde les remises steer sur sa fin effective.

        Le `finally` couvre les tours sans courrier (consigne, suite, relance)
        comme les tours de courrier : `close_mail(result)` solde la réservation
        du tour quand elle existe, puis `_solde_steers` clôt les steers.
        """
        self._solde_steers(False)  # restes d'un tour précédent, jamais réservés
        spec = self._with_turn_id(spec)
        generation = self._begin_turn_generation()
        result = False
        try:
            result = self._run_turn(spec)
            return result
        finally:
            if close_mail is not None:
                close_mail(result)
            self._solde_steers(result, generation)

    def _close_turn_mail(self, spec: dict, jeton: str, ids: list[int],
                         result: bool) -> None:
        """Solde ou libère la réservation de courrier d'un tour."""
        if spec.get("human") and self._courrier_lance and result:
            # L77/B9 : le message humain n'est soldé que sur une fin
            # EFFECTIVE du tour. Une mort du harnais après `send` laisse la
            # réservation repartir en livraison (au moins une fois).
            self._solde_humains(spec)
        elif not self._courrier_lance or not result:
            # Pas lancé, ou lancé puis mort/échec avant la fin : rien n'est
            # tenu pour servi ; la réservation est libérée et sera reprise.
            try:
                mail.release(self.db, self.name, jeton, ids)
            except db_mod.DbError as exc:
                # La réservation reste jusqu'à son échéance : le message sera
                # re-livré, signalé (aucune perte).
                log("[%s] could not cancel the reservation (%s)"
                    % (self.name, exc))

    def _run_guarded_turn(self, spec: dict) -> bool:
        """Un tour. Pour un tour de courrier, protocole de remise unique :
        réservation sous un jeton (registre verrouillé, bail contrôlé), consigne
        rendue sur les messages réservés, remise soldée juste après le lancement
        du harnais (bail toujours exigé), réservation annulée si le tour n'a pas
        pu démarrer.

        Panne ou bail perdu entre réservation et remise : la réservation n'est
        pas soldée ; elle expire ou change d'epoch, le message est remis plus
        tard et signalé « re-livré » — un doublon signalé, jamais une perte.
        """
        # #2729 : un appel direct (rotation, compaction, résumé de compte) n'a
        # pas la frontière figée par `pick()` ; on la fige ici, à l'entrée,
        # avant toute préparation. Une demande arrivée ensuite reste un arrêt
        # pour ce tour et ne sera pas effacée à son début.
        if "_preempt_gen" not in spec:
            spec = dict(spec, _preempt_gen=_preemption_generation(self))
        # #2894 : direct callers (rotation, compaction, tests) also get the
        # turn id before the mail reservation.
        spec = self._with_turn_id(spec)
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
            # L77/B8 : un steer humain reçu pendant un tour sans courrier
            # (consigne, suite, relance) est soldé à sa fin effective lui aussi.
            return self._run_turn_settled(spec)
        kind = spec["kind"]
        budget = self._budget_courrier()
        envelopes = spec.get("envelopes")
        _texte, retenus, reste = mail.prompt_for(self.db, candidats, kind=kind,
                                                 budget=budget, envelopes=envelopes)
        jeton = mail.new_token()
        try:
            reserves = mail.reserve(
                self.db, self.name, self.runner.runner_id, self.epoch, jeton,
                ids=[int(m["id"]) for m in retenus], porteur="consigne",
                ttl_seconds=self._reservation_ttl(spec),
                turn_id=spec.get("turn_id"))
        except db_mod.DbError as exc:
            log("[%s] mail reservation failed (%s): turn postponed"
                % (self.name, exc))
            return False
        if not reserves:
            self._servir(())  # #2810 : aucun tour ne sert ces messages
            log("[%s] turn mail already reserved or delivered elsewhere, or lease "
                "lost: turn cancelled" % self.name)
            return False
        # La consigne est rendue sur les messages RÉSERVÉS (leur état exact :
        # « re-livré » compris), dans le même budget.
        prompt, inclus, _ = mail.prompt_for(self.db, reserves, kind=kind, budget=budget,
                                            extra_remaining=reste, envelopes=envelopes)
        ids = [int(m["id"]) for m in inclus]
        # #2810 : ce tour sert exactement ces messages (la réservation peut
        # différer de la sélection).
        self._servir(ids)
        hors = [int(m["id"]) for m in reserves if int(m["id"]) not in ids]
        if hors:  # par construction vide ; jamais réservé sans être montré
            mail.release(self.db, self.name, jeton, hors)
        spec = dict(spec, prompt=prompt, rows=inclus, ids=ids, jeton=jeton)
        self._courrier_lance = False
        return self._run_turn_settled(
            spec,
            close_mail=lambda result: self._close_turn_mail(spec, jeton, ids, result))

    def _livre_courrier(self, spec: dict) -> None:
        """Le harnais est lancé avec la consigne : la réservation est soldée.

        Remise fencée (jeton ET bail courant). Si le bail est perdu, les
        messages restent non livrés et la remise est signalée incertaine : le
        bail suivant les re-livrera en le disant.
        """
        if not spec.get("jeton"):
            return
        self._courrier_lance = True
        etapes = spec.get("_etapes")
        if etapes is not None:
            etapes["spawned_ts"] = time.time()  # #3218 : harnais lancé
        if spec.get("human"):
            # L77 : un message humain n'est soldé qu'à la FIN du tour
            # (`_solde_humains`), jamais au lancement. Un plantage en cours de
            # tour laisse la réservation expirer : le message est re-livré,
            # signalé comme peut-être déjà remis (au moins une fois).
            return
        ids = list(spec["ids"])
        try:
            livres = mail.deliver(self.db, self.name, self.runner.runner_id, self.epoch,
                                  spec["jeton"], ids)
        except db_mod.DbError as exc:
            livres = []
            log("[%s] mail delivery not settled (%s)" % (self.name, exc))
        if etapes is not None and livres:
            etapes["delivered_ts"] = time.time()
        self._remise_incertaine(ids, livres)

    def _remise_incertaine(self, ids, livres) -> None:
        """Signale les messages montrés au harnais mais non soldés (bail perdu,
        base indisponible) : ils seront re-livrés en le disant."""
        incertains = sorted(set(ids) - set(livres))
        if not incertains:
            return
        log("[%s] uncertain delivery: message(s) %s shown to the harness but not "
            "settled (lease lost or database unavailable); they will be redelivered, "
            "flagged as possibly redelivered" % (self.name, ", ".join(map(str, incertains))))
        self._fil_note(
            "Uncertain delivery: %d message(s) put in the instruction of a started "
            "turn could not be marked delivered (lease lost or database "
            "unavailable). They will be delivered again, flagged as possibly redelivered."
            % len(incertains),
            meta={"action": "remise-incertaine", "messages": incertains})

    def _solde_humains(self, spec: dict) -> None:
        """L77 : solde la réservation humaine à la FIN du tour (servi).

        Appelé par `run_turn` dans son `finally`, que le tour réussisse ou
        échoue : le harnais a lu sa consigne. Si le bail est perdu ou la base
        indisponible, rien n'est soldé et le message sera re-livré. Le journal
        L78 n'est écrit qu'ici, avec la remise effective (idempotent).
        """
        jeton = spec.get("jeton")
        ids = list(spec.get("ids") or ())
        self._courrier_lance = False
        if not jeton or not ids:
            return
        try:
            livres = mail.deliver(self.db, self.name, self.runner.runner_id, self.epoch,
                                  jeton, ids)
        except db_mod.DbError as exc:
            log("[%s] human delivery not settled at the end of the turn (%s)"
                % (self.name, exc))
            return
        self._remise_incertaine(ids, livres)
        if livres:
            # L77/L78 : la trace lisible des messages humains servis. Idempotente
            # (`event_id = mail:<id>`) : une remise incertaine rejouée ne duplique
            # pas l'entrée ; le journal ne porte aucun état servi/ack.
            self._journal_humans(spec, livres)

    def _journal_humans(self, spec: dict, delivered) -> None:
        """Écrit une entrée `human_message` par message humain remis (L78)."""
        if not spec.get("human") or not delivered:
            return
        from . import conversation

        rows = {}
        for row in spec.get("rows") or ():
            try:
                rows[int(row["id"])] = row
            except (KeyError, TypeError, ValueError):
                continue
        cfg = getattr(self, "cfg", None)
        first = None
        for mid in delivered:
            row = rows.get(int(mid))
            if row is None:
                continue
            sender = str(row.get("sender") or "human:?")
            author = sender if sender.startswith(("human:", "chat-")) \
                else "human:" + sender
            try:
                entry = conversation.Log(self.db, cfg, agent=self.name,
                                         lot=row.get("lot_id"))
                entry.append("human_message", text=row.get("body") or "",
                             author=author, event_id="mail:%d" % int(mid),
                             meta={"messages": [int(mid)]})
                first = first or entry
            except Exception as exc:  # le tour part quand même, la trace est signalée
                log("[%s] conversation journal: human message #%s not recorded (%s)"
                    % (self.name, mid, exc))
        if first is not None:
            try:
                first.drain_threads()
            except Exception as exc:
                log("[%s] conversation journal: readable thread not delivered (%s)"
                    % (self.name, exc))

    def _github_env(self, env: dict, lot) -> None:
        """#1645 : journal des gestes gh et identité GitHub par rôle. Jamais
        bloquant : une erreur laisse l'identité héritée, et le dit."""
        from . import github_identity
        try:
            info = github_identity.apply(env, self.name, self.cfg.config_dir,
                                         self.cfg.state_dir, lot)
        except Exception as exc:  # noqa: BLE001 — le tour part quand même
            log("[%s] github identity not applied: %s" % (self.name, exc))
            return
        if info.get("problem"):
            log("[%s] github identity %s: %s"
                % (self.name, info.get("identity"), info["problem"]))

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

    @staticmethod
    def _read_turn_cgroup_ready(fd: int, timeout: float = TURN_CGROUP_READY_S) -> str:
        """L498 : lit le READY du trampoline sur le descripteur dédié, avec une
        borne indépendante (le harnais, lui, garde son stdout intact). Rend ""
        si le signal manque, si le fils est mort avant, ou à l'expiration."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        data = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ""
            try:
                readable, _, _ = select.select([fd], [], [], remaining)
            except OSError:
                return ""
            if not readable:
                return ""
            try:
                chunk = os.read(fd, 256)
            except OSError:
                return ""
            if not chunk:
                return ""
            data += chunk
            if b"\n" in data:
                return data.split(b"\n", 1)[0].decode("ascii", "replace")

    def _open_turn_resource(self, turn_id: str, pgid: int | None,
                            cgroup: str | None = None) -> None:
        """Rattache le groupe (L31, 0028) et le cgroup (L498) du tour ; ne
        casse jamais le tour."""
        self._turn_open = False
        self._turn_open = self._attach_resource(turn_id, pgid, cgroup=cgroup)

    def _attach_resource(self, turn_id: str, pgid: int | None,
                         cgroup: str | None = None) -> bool:
        """La ligne de ressource d'un tour, ou d'une vie de processus résident
        (L76) ; False si elle n'a pas pu être ouverte. Le cgroup délégué du
        tour (L498) est enregistré après la ligne, s'il y en a un."""
        try:
            from . import containers as containers_mod
            storage.of(self.db).turn_resources.open_turn(
                turn_id, self.name, self.runner.host, pgid=pgid,
                label=containers_mod.turn_labels(turn_id, self.name))
        except Exception as exc:  # ne casse jamais un tour
            log("[%s] could not attach the resources of turn %s (%s)"
                % (self.name, turn_id[:8], exc))
            return False
        if cgroup and not getattr(self.runner, "mediated", False):
            try:
                storage.of(self.db).turn_resources.attach_cgroup(turn_id, cgroup)
            except Exception as exc:  # la propriété locale du cgroup reste entière
                log("[%s] cgroup %s of turn %s not recorded (%s)"
                    % (self.name, cgroup, turn_id[:8], exc))
        return True

    def _close_turn_resource(self, turn_id: str, pgid: int | None, *,
                             survives: bool = False,
                             cgroup: str | None = None) -> None:
        """Ferme la ressource du tour ; `orphan` si elle survit (L31, 0028).

        Un groupe encore vivant après l'arrêt escaladé, un cgroup qui ne se
        vide pas, ou un conteneur qui porte encore l'étiquette du tour, est
        signalé orphelin : **jamais supprimé** ici. Appelée seulement quand le
        travail de fond n'est PAS confié au suivi (`background.start`) : le
        cgroup du tour est alors tué et enlevé, sa latence et son pic mémoire
        journalisés (L498, mesures)."""
        recorded = getattr(self, "_turn_open", False)
        if not recorded and not cgroup:
            return
        self._turn_open = False
        self._release_resource(turn_id, pgid, survives=survives, cgroup=cgroup,
                               recorded=recorded)

    def _release_resource(self, turn_id: str, pgid: int | None, *,
                          survives: bool = False, cgroup: str | None = None,
                          recorded: bool = True) -> None:
        from .turn_processes import group_running
        conteneurs: list[str] = []
        runtime = self.runner.container_runtime()
        if runtime is not None:
            try:
                conteneurs = runtime.running_for_turn(turn_id)
            except Exception:  # un moteur indisponible n'est pas un orphelin
                conteneurs = []
        emptied = None
        if cgroup:
            from .platform import turn_cgroup
            peak = turn_cgroup.memory_peak(cgroup)
            started = time.monotonic()
            try:
                emptied = turn_cgroup.kill(cgroup)
            except Exception as exc:
                log_async("[%s] turn %s cgroup %s not killed (%s)"
                          % (self.name, turn_id[:8], cgroup, exc))
                emptied = False
            log("[%s] turn %s cgroup cleanup: %s in %d ms (memory peak %s)"
                % (self.name, turn_id[:8],
                   "emptied and removed" if emptied else "still populated",
                   int((time.monotonic() - started) * 1000),
                   "%d bytes" % peak if peak is not None else "unknown"))
        orphelin = (survives or bool(conteneurs) or group_running(pgid)
                    or emptied is False)
        if recorded:
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
                   ", ".join(conteneurs) if conteneurs else
                   (cgroup if emptied is False else "surviving process group")))

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
            return "resident failed recently (%d in a row)" % self._resident_failures
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

    def _resident_probe(self, rp) -> bool:
        """T3 : la sonde de vivacité bornée, hors tour (aucun tour ne court).
        Jamais d'exception : un pilote qui se trompe vaut « muet », pas un
        tour cassé."""
        from . import resident_runner
        try:
            return bool(rp.driver.probe(resident_runner.PROBE_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 — la sonde ne casse pas le tour
            log("[%s] liveness probe failed (%s): treated as silent"
                % (self.name, exc))
            return False

    def _resident_cost_base(self, session: str | None) -> float | None:
        """Le dernier cumul USD **durable** de la session résidente (L80a).

        Un processus relancé sur la même session ACP (sommeil, réglages
        changés, redémarrage du worker) a perdu sa base en mémoire : sans ce
        repère, le premier prompt d'une session reprise compterait tout son
        passé, ou retomberait sur un barème de jetons. Lu dans le grand livre,
        jamais dans un fichier. Une lecture impossible laisse le pilote sans
        base (le grand livre la retrouvera au calcul du tour)."""
        if not session:
            return None
        try:
            book = cost_mod.CostBook(
                state_dir=self.cfg.state_dir, db=self.db,
                tools={self.name: self._ledger_harness()})
            base = book.baseline(self.name, self._ledger_harness(), session)
        except Exception as exc:  # une base de repère ne casse pas le tour
            log("[%s] resident cost baseline not read (%s)"
                % (self.name, " ".join(str(exc).split())[:120]))
            return None
        return base.get("cum_usd") if base.get("session") == session else None

    def _resident_for_turn(self, adapter, *, key: tuple, session: str | None, cwd: str,
                           env: dict, model, effort, tier, lot, stderr_path: str):
        """Le processus résident qui sert ce tour : celui qui tourne si sa clé
        de lancement et sa session sont celles du tour, sinon un neuf (repris
        sur la session). Un résident muet (il vit, son protocole ne répond
        plus) est arrêté et compté comme un échec : ce tour est servi un par
        événement. None si le démarrage échoue : ce tour, et les suivants
        pendant une attente croissante (`_resident_failed`), sont servis un
        par événement."""
        from . import containers as containers_mod, resident, resident_runner
        with self.lock:
            rp = self.resident
        if rp is not None:
            pourquoi = ("exited" if not rp.alive else
                        "being stopped" if rp.closing else
                        "launch settings changed" if rp.key != key else
                        "session changed" if (rp.session or None) != (session or None) else
                        "a turn still running" if rp.driver.busy else "")
            if not pourquoi and rp.probe_due():
                # T3 : sonde bornée, hors tour (le tour n'est pas lancé) — un
                # processus muet ne sert pas ce tour
                if self._resident_probe(rp):
                    rp.mark_alive()
                else:
                    raison = "silent: no answer to the liveness probe"
                    self._resident_failed(raison)
                    self._close_resident(raison)
                    return None
            if not pourquoi:
                return rp
            self._close_resident("restarted: %s" % pourquoi)
        life_id = resident_runner.ResidentProcess.new_life_id()
        from .platform import child_environment
        env = child_environment.grant(env, getattr(env, "credentials", ()))
        env.update({
            "AMEESH_TURN_ID": life_id,
            containers_mod.LABELS_ENV: containers_mod.turn_labels(
                life_id, self.name, str(lot) if lot else None),
            resident_runner.TURN_FILE_ENV: self._path(resident_runner.TURN_FILE),
        })
        stderr = open(stderr_path, "ab")
        # L80a : une session ACP reprise garde son cumul USD d'un processus à
        # l'autre ; la base durable est passée au pilote, qui calcule le delta
        # du premier prompt comme celui des suivants.
        protocol = (adapter.descriptor.resident or {}).get("protocol")
        base_cout = self._resident_cost_base(session) if protocol == "acp" else None
        driver = resident.driver_for(
            adapter, cwd=cwd, session=session, model=model or None, effort=effort or None,
            tier=tier or None, env=env, stderr=stderr, spawn=self._spawn_resident,
            cost_base=base_cout)
        rp = resident_runner.ResidentProcess(driver, key=key, life_id=life_id, stderr=stderr)
        # L81 (étude §6.1) : le cumul de relu appartient à la session — un
        # processus relancé sur le même id (réveil après sommeil, réglages
        # changés) reprend le cumul du précédent.
        rp.session_reread = self._resident_reread_load(driver.session_id or session)
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
            self._resident_failed("start failed (%s)" % exc)
            return None
        rp.resource_open = self._attach_resource(life_id, rp.pgid)
        with self.lock:
            self.resident = rp
        log("[%s] resident process started (pid %s, session %s)"
            % (self.name, rp.pgid, driver.session_id or "new"))
        return rp

    def _resident_failed(self, raison: str) -> None:
        """T2 : un échec du résident — un tour par événement pendant une
        attente qui double à chaque échec de suite (RESIDENT_RETRY_MIN_S à
        RESIDENT_RETRY_MAX_S). Les tours, eux, restent comptés par la série
        d'échecs rapides de L48, qui arrête l'agent si eux aussi échouent."""
        self._resident_failures += 1
        # l'exposant borné : 2 ** 1024 ne tient pas dans un flottant (codex r1, #422)
        attente = min(RESIDENT_RETRY_MIN_S * 2 ** min(self._resident_failures - 1, 32),
                      RESIDENT_RETRY_MAX_S)
        self._resident_refused_until = time.monotonic() + attente
        log("[%s] resident %s (failure %d in a row): one turn per event for %ds"
            % (self.name, raison, self._resident_failures, int(attente)))

    def _note_dormant(self, raison: str) -> None:
        """T2 : l'agent est `dormant` — bail gardé, processus arrêté, réveil
        au prochain tour sur sa session. Dit dans son statut s'il est `idle`
        (un `blocked`, un `stopped` restent tels quels), bail tenu."""
        try:
            # une seule écriture, conditionnelle (codex r1, #422) : un agent
            # arrêté ou mis en pause entre-temps n'est jamais remis `idle`
            storage.of(self.db).operations.set_dormant(
                self.name, self.runner.runner_id, self.epoch,
                "dormant: %s; woken by its next turn" % raison)
        except db_mod.DbError as exc:
            log_async("[%s] dormant status not written (%s)" % (self.name, db_mod.explain(exc)))

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
            if not getattr(self, "_session_turn_depth", 0):
                self._release_session_lock()
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

    def _served_message_ids(self, initial_ids=()) -> tuple[str, ...]:
        """L80b : les messages que ce tour résident a réellement servis.

        Les messages de la consigne (`initial_ids`, ceux du spec) et les
        remises steer acceptées — même condition que `_solde_steers` : tour
        achevé et `input_id` présent dans le `TurnResult`. Les uuids de
        corrélation ne sortent jamais : le grand livre reçoit des identifiants
        de messages (mail id / human_message id), pas des jetons internes."""
        from . import resident as resident_mod
        ids = {str(i) for i in (initial_ids or ()) if i is not None}
        with self.lock:
            generation = self._turn_generation
            pending = list(self._steer_pending)
        for _jeton, mail_ids, gen, turn, input_id in pending:
            if gen != generation or turn is None:
                continue
            result = getattr(turn, "result", None)
            if getattr(result, "status", "") != resident_mod.COMPLETED \
                    or input_id not in (getattr(result, "inputs", ()) or ()):
                continue
            ids.update(str(i) for i in (mail_ids or ()) if i is not None)
        return tuple(sorted(ids))

    def _resident_result(self, rp, tour, voulu: bool,
                         ) -> tuple[int, str | None, str | None, object | None]:
        """Code, erreur, session et `TurnResult` d'un tour résident fini
        (`voulu` : clos par l'exécuteur, une interruption n'est pas un échec).

        Le `TurnResult` est aussi la seule source d'usage du tour pour le grand
        livre quand le flux brut ne la porte pas (Codex app-server, ACP) : il
        est écrit une fois dans `events.jsonl` (L80a), après la barrière steer
        de `_resident_turn_end` et avant la comptabilité — par
        `_resident_usage_final`, qui rend le `TurnResult` pour que sa mesure et
        ses entrées soient lues au même point de clôture."""
        from . import resident
        if tour is None:
            return 1, None, rp.session, None  # pas lancé : l'erreur est déjà notée
        fin = tour.wait(RESIDENT_END_WAIT_S)
        if fin is None:
            return 1, "resident turn did not end", rp.session, None
        # L81 (étude §6.1) : la mesure continue d'un tour résident — contexte
        # courant, fenêtre, relu du tour et relu cumulé de la session.
        rp.record_turn(fin)
        self._resident_reread_keep(rp)
        session = fin.session or rp.session
        if fin.status == resident.COMPLETED:
            # N1 (#288) : la série est close par `_resident_usage_final`, une
            # fois la mesure écrite — pas ici, avant l'écriture.
            return 0, None, session, fin
        if fin.status == resident.INTERRUPTED:
            return ((0, None, session, fin) if voulu
                    else (1, "resident turn interrupted", session, fin))
        return 1, fin.error or "resident turn failed", session, fin

    def _resident_usage_final(self, rp, fin, served_ids=()) -> tuple[bool, str | None]:
        """Écrit la mesure et les entrées du tour résident (L80a/L80b).

        Appelé **après** `_resident_turn_end`, dont la barrière `_steer_lock`
        ferme la génération : un ACK de steer accepté pendant la clôture est
        alors dans `_steer_pending`, donc compté dans les messages servis
        (L80b B2). Les protocoles à usage natif (Claude) n'écrivent pas de
        `resident.turn` ; leurs entrées partent dans `resident.inputs`, dont
        l'échec ne fait jamais échouer le tour (le coût est déjà dans le flux).
        """
        from . import resident, resident_runner
        inputs = self._served_message_ids(served_ids)
        evenement = resident_runner.usage_event(
            rp.driver.protocol, fin, turn=self._turn_id or "", inputs=inputs)
        if evenement is not None:
            if not resident_runner.write_usage_event(
                    self._path("events.jsonl"), evenement):
                # L80a B2 : la mesure passe d'abord dans le marqueur comptable
                # (durable) ; à défaut elle reste en mémoire pour ce worker. Le
                # tour n'est pas comptablement complet et le marqueur ne sera
                # pas soldé tant que le flux ne porte pas la mesure.
                travail = getattr(self, "_compta_travail", None)
                if self._resident_usage_marker(evenement, travail):
                    if travail is not None:
                        self._compta_travail = dict(travail, resident_usage=evenement)
                else:
                    self._resident_usage_pending.append(evenement)
                    self._compta_en_echec = True
                return False, "resident usage event not written"
        else:
            attribution = resident_runner.inputs_event(
                rp.driver.protocol, fin, turn=self._turn_id or "", inputs=inputs)
            if attribution is not None and not resident_runner.write_usage_event(
                    self._path("events.jsonl"), attribution):
                log_async("[%s] resident inputs event not written: message "
                          "attribution lost for this turn" % self.name)
        # N1 (#288) : la série d'échecs n'est close qu'une fois la mesure
        # ÉCRITE ; un échec d'écriture garde la série (avant, `_resident_result`
        # la remettait à zéro avant même de tenter l'écriture).
        if getattr(fin, "status", "") == resident.COMPLETED:
            self._resident_failures = 0
        return True, None

    def _write_served_inputs(self, harness: str, ids, session, status: str) -> None:
        """#3452 : l'attribution des messages servis, pour un tour non résident.

        Les tours de harnais CLI n'écrivent ni `resident.turn` ni
        `resident.inputs` : la colonne `turn_costs.inputs` restait vide pour
        eux, et `cost turns --message` ne reliait aucun tour à son courrier.
        L'événement est le même que pour Claude natif (même type, même lecture
        par `cost.record`) ; une écriture ratée ne fait jamais échouer le tour,
        seule l'attribution est perdue."""
        from . import resident_runner
        event = resident_runner.inputs_attribution(
            harness, session=session or "", turn=self._turn_id or "",
            inputs=ids, status=status)
        if event is not None and not resident_runner.write_usage_event(
                self._path("events.jsonl"), event):
            log_async("[%s] turn inputs event not written: message attribution "
                      "lost for this turn" % self.name)

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
        rp.mark_alive()          # T3 : le tour qui vient de finir est une preuve de vie
        # L77/B8 : un steer est peut-être encore en vol (barrière `_steer_lock`) ;
        # attendre sa fin avant de juger le résident occupé, puis fermer la
        # génération pour qu'un ACK tardif ne soit pas attribué au tour suivant.
        lock = getattr(self, "_steer_lock", None)
        if lock is not None:
            acquired = lock.acquire(timeout=PREEMPT_STEER_WAIT_S)
            try:
                with self.lock:
                    self._turn_live = False
            finally:
                if acquired:
                    lock.release()
            if not acquired:
                log_async("[%s] steer barrier not reached at turn end within "
                          "%.0fs" % (self.name, PREEMPT_STEER_WAIT_S))
        urgence = self._urgent_end() or (
            "the host is withdrawing" if self.host_yield is not None else
            "stopping" if self.stopping.is_set() else "")
        if urgence == "preemption" and rp.alive and not rp.driver.busy:
            # L77/B5 : une préemption ne ferme pas un résident vivant. Les
            # autres causes d'arrêt gardent la priorité.
            if self.restarting.is_set():
                urgence = "restart requested"
            elif self.host_yield is not None and self.host_yield != self.HOST_SAFE_POINT:
                urgence = "the host is withdrawing"
            elif self.stopping.is_set():
                urgence = "stopping"
            elif self._power_texte:
                urgence = "critical battery"
            else:
                urgence = ""
        raison = ("exited" if not rp.alive else
                  "its turn did not end" if rp.driver.busy else urgence)
        if raison and not urgence:
            self._resident_failed("process %s during its turn" % raison)
        if raison:
            self._close_resident(raison)

    def _resident_between_turns(self) -> None:
        """Entre deux tours : un résident mort est fermé (un échec, T2) ; un
        résident inactif depuis `resident_sleep`, ou sous la pression de
        l'hôte, est endormi — `dormant` (bail gardé, réveil au prochain tour
        sur sa session) ; `resident=off` l'arrête ; un résident muet (T3) est
        sondé, borné, hors tour : sans réponse il est arrêté et compté comme
        un échec résident, le tour suivant repart sur un processus neuf."""
        from . import resident_runner
        with self.lock:
            rp = self.resident
        if rp is None:
            return
        if not rp.alive:
            self._resident_failed("process exited between turns")
            self._close_resident("exited")
        elif not resident_runner.enabled(self._state_read(resident_runner.SETTING)):
            self._close_resident("resident=off")
        elif self.runner.host_pressure().get("blocked"):
            # T2 : sous la pression de l'hôte (L31), un résident inactif dort
            # le premier — sa mémoire est rendue, son bail gardé
            self._close_resident("asleep: host under pressure")
            self._note_dormant("asleep, host under pressure")
        else:
            sommeil = resident_runner.sleep_seconds(
                self._state_read(resident_runner.SLEEP_SETTING))
            if sommeil and time.monotonic() - rp.last_used >= sommeil:
                self._close_resident("asleep after %ds without a turn" % int(sommeil))
                self._note_dormant("asleep after %ds without a turn" % int(sommeil))
            elif rp.probe_due():
                # T3 : il vit ; son protocole répond-il encore ? La sonde est
                # bornée et hors tour (aucun tour ne court ici)
                if self._resident_probe(rp):
                    rp.mark_alive()
                else:
                    raison = "silent between turns: no answer to the liveness probe"
                    self._resident_failed(raison)
                    self._close_resident(raison)

    def _menage_apres_tour(self, menage_tour, turn_id: str, spec: dict) -> None:
        """L73 : quota du dossier temporaire, worktrees apparus, /tmp signalé ;
        journalisé. Ne casse jamais la fin d'un tour."""
        if menage_tour is None:
            return
        try:
            lot = spec.get("lot") or self.agent.get("session_work_item") or None
            entries = workspaces.after_turn(
                menage_tour, lot=str(lot) if lot else None, turn_id=turn_id,
                db=self.db, host=self.runner.host,
                runtime=self.runner.container_runtime())
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
                    elif (entry.get("kind") == "tmp"
                          and entry.get("detail")
                          == "new session: temporary directory emptied"):
                        # #2610 : trace au journal du vidage, pour mesurer les
                        # rotations qui l'auraient encore déclenché.
                        log("[%s] new session: temporary directory emptied (%s, %s bytes)"
                            % (self.name, entry.get("path"), entry.get("bytes")))
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
        échéance n'est jamais repoussée par une tentative interrompue).
        #2729 : la série de préemptions est un suffixe, elle aussi conservée."""
        texte = base
        if self.rate_limit:
            texte = rate_limit_mod.status_text(
                texte, self.rate_limit, self.rate_limit_streak, self.rate_limit_since,
                rate_limit_mod.delay(self.rate_limit_streak),
                retry_at=self.rate_limit_retry_at or None)
        from . import exploitation as exploitation_mod
        if self.failure_streak:
            texte = exploitation_mod.turn_failure_status_text(
                texte, self.failure_streak, critical=account_turn.is_critical(self),
                since=self.failure_since)
        if self.preemption_streak:
            texte = exploitation_mod.preemption_status_text(
                texte, self.preemption_streak, since=self.preemption_since)
        return self._fenetre_debit(texte, paused=True)

    def _clos_episode_debit(self, fin: float) -> None:
        """#3733 : l'épisode de débit passager en cours, fini à `fin` (début
        du tour qui l'a terminé), entre dans la fenêtre glissante. Un quota
        n'y entre pas : L48 l'arrête déjà."""
        if self.rate_limit == rate_limit_mod.RATE_LIMITED and self.rate_limit_since:
            self.rate_limit_window.append(
                (self.rate_limit_since, max(self.rate_limit_since, float(fin))))

    def _fenetre_debit(self, texte: str, *, paused: bool = False) -> str:
        """#3733 : `texte` suffixé du cumul de débit passager de l'heure
        glissante (épisode en cours compris) ; tel quel sans cumul, et la
        série se referme quand la fenêtre est vide."""
        now = time.time()
        en_cours = (self.rate_limit_since
                    if self.rate_limit == rate_limit_mod.RATE_LIMITED else 0.0)
        self.rate_limit_window = rate_limit_mod.window_prune(self.rate_limit_window, now)
        debuts = [s for s, _e in self.rate_limit_window] + ([en_cours] if en_cours else [])
        if not debuts:
            self.rate_limit_window_since = 0.0
            return texte
        if not self.rate_limit_window_since:
            self.rate_limit_window_since = min(debuts)
        return rate_limit_mod.window_text(
            texte, rate_limit_mod.window_total(self.rate_limit_window, now, en_cours),
            len(debuts), self.rate_limit_window_since, now,
            closed=self.rate_limit_window, ongoing=en_cours, paused=paused)

    def _run_turn(self, spec: dict) -> bool:
        """Un tour, sous battement de bail **dès la préparation** (#2544).

        Le battement ne démarrait qu'une fois le harnais publié : sous charge
        CI, la préparation (dossier, harnais, canon, ménage, comptabilité)
        pouvait dépasser un bail court, le veilleur tuait le worker avant le
        `Popen` et le tour ne démarrait jamais (run 39158768160, psql 6/16 :
        « stop requested (lease deadline reached): harness not yet published »
        puis « lease lost … before the turn »). Le bail protège le tour entier
        — la préparation en fait partie — donc le battement démarre ici, avant
        elle, et la couvre jusqu'à la sortie du tour, y compris les retours
        anticipés.

        #2597 : il couvre aussi l'après-harnais (nettoyage, compta, fin de tour).
        Il s'arrêtait dès la sortie du harnais alors que le veilleur jugeait
        encore l'échéance : une compta lente sous charge laissait expirer le bail
        (run 39181194843, psql 6/16 : « harness not yet published » après
        « end of turn »). Seule cette sortie l'arrête.
        """
        #: #2466 : vrai dès que le harnais du tour est réellement lancé (Popen
        #: ou processus résident) ; l'échéance d'un réveil programmé n'est
        #: consommée qu'à ce moment.
        self._turn_launched = False
        heartbeat_done = threading.Event()
        self.heartbeat_thread = threading.Thread(target=self._heartbeat, args=(heartbeat_done,),
                                                 daemon=True)
        self.heartbeat_thread.start()
        try:
            return self._run_turn_sous_battement(spec)
        finally:
            heartbeat_done.set()
            # #2894 : le document de tour vit exactement le tour. Retiré sur
            # tout retour (succès, échec, lancement impossible, exception) ;
            # un document resté après un crash est refusé par le lecteur
            # (bail et marqueur) et remplacé au tour suivant.
            self._purge_turn_context()

    def _run_turn_sous_battement(self, spec: dict) -> bool:
        if spec.get("lot_switch_failed"):
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            return False
        from . import runner_sandbox
        sandbox_result = runner_sandbox.dispatch(self, spec)
        if sandbox_result is not None:
            return sandbox_result
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
            # #1900 N1 : origine inconnue (session d'avant 0384), sur SON lot
            # et dans le dossier de l'agent — le seul où elle a pu être
            # ouverte : adopter plutôt que perdre par une bascule vers le même
            # lot. Au dossier secondaire, la bascule reste (on ne peut pas
            # reprendre la session ailleurs).
            adopted = (not origin and lot
                       and str(lot) == str(self.agent.get("session_work_item") or "")
                       and self.agent.get("cwd")
                       and os.path.realpath(cwd) == os.path.realpath(self.agent["cwd"])
                       and self._adopt_originless_session(session, cwd))
            if not adopted and (not lot or not self._switch_lot(
                    str(self.agent.get("session_work_item") or ""), str(lot))):
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
        # #3779 : admission ATOMIQUE juste avant d'ouvrir le tour. La première
        # lecture (dans `account_turn.choose`) n'est qu'un pré-contrôle : deux
        # agents peuvent s'y présenter ensemble. Ici le cooldown, l'écart depuis
        # le dernier début et le plafond de tours actifs sont relus sous le
        # verrou du compte, et `begin_turn` (donc la place `running`) est posé
        # dans la MÊME transaction que le journal des débuts : la place est
        # visible des autres runners dès le commit, même avec un écart nul.
        # Un tour refusé ici n'a pas commencé : consigne remise, pas un échec.
        marqueurs = self._marqueurs_durables("%s turn (%s)" % (shown, harness))
        admission = account_window.claim_start(
            self.db, harness, getattr(compte, "name", None), self.cfg.host,
            self.name, profile=compte,
            begin=lambda tx: registry.begin_turn(
                tx, self.name, self.runner.runner_id, self.epoch, marqueurs))
        if not admission.ok:
            if admission.begin_failed:
                # L87b: pause may have won the row lock after pick/run_turn.
                # Return a prompt already taken, without rewriting concurrent status.
                registry.restore_prompt(self.db, self.name, self.runner.runner_id,
                                        self.epoch)
                log("[%s] lease lost, agent stopped or operator paused before the turn"
                    % self.name)
                return False
            log("[%s] turn postponed: %s" % (self.name, admission.reason))
            self._admission_reportee = True
            self._retenue_debut("short_window")
            if admission.retry_at is not None:
                self.note_due_at(admission.retry_at)
            registry.restore_prompt(self.db, self.name, self.runner.runner_id, self.epoch)
            registry.set_status(self.db, self.name, "blocked",
                                status_text=admission.reason)
            return False
        if not admission.begun:
            if not registry.begin_turn(self.db, self.name, self.runner.runner_id,
                                       self.epoch, marqueurs):
                # L87b: pause may have won the row lock after pick/run_turn.
                # Return a prompt already taken, without rewriting concurrent status.
                registry.restore_prompt(self.db, self.name, self.runner.runner_id,
                                        self.epoch)
                log("[%s] lease lost, agent stopped or operator paused before the turn"
                    % self.name)
                return False
        if spec.get("_etapes") is not None:
            spec["_etapes"]["admitted_ts"] = time.time()  # #3218
        lot = spec.get("lot")
        if lot and not spec.get("lot_finished") \
                and str(lot) != str(self.agent.get("session_work_item") or ""):
            # Le lot de la session courante (0025, L26) : noté au premier tour
            # d'une session neuve, ou après une rotation refusée ; fencé.
            # #1898 N1 : jamais pour un lot fini — la session qui sert ce tour
            # n'est pas la sienne, elle ne doit ni être close à chaque tour ni
            # lui être rattachée.
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
        # #2763 : ni VIRTUAL_ENV ni bin/ du venv de production dans le tour
        # (incident du 19/10 : `uv pip install -e` d'un worktree dans le venv
        # vivant) ; `ameesh` et `agent-mail` restent joignables par shims.
        venv_guard.scrub(env, venv_guard.shim_dir(self.cfg))
        # Decision 0029 disables native account memory for managed sessions.
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        # L31 (0028) : identifiant du tour et étiquettes à poser sur les
        # conteneurs lancés par ses outils, pour repérer les orphelins.
        # #2894 : celui alloué en tête de tour, jamais un second.
        from . import containers as containers_mod
        turn_id = spec.get("turn_id") or uuid.uuid4().hex
        self._turn_id = turn_id
        lot_du_tour = lot or self.agent.get("session_work_item") or None
        # #3452 : le lot imputé au grand livre ne se replie pas sur le lot de
        # session pour un tour de courrier sans lot (pilotage, courrier
        # ambigu) — il n'y a pas travaillé. Les étiquettes de conteneurs
        # gardent le lot de session (hygiène du dossier), pas la comptabilité.
        lot_comptable = cost_mod.turn_attribution_lot(
            spec, self.agent.get("session_work_item"))
        turn_label = containers_mod.turn_labels(
            turn_id, self.name, str(lot_du_tour) if lot_du_tour else None)
        identite = {
            "AGENT_MAIL_NAME": self.name,
            "AGENT_MAIL_STATE": self.cfg.v0_state,
            "AGENT_MAIL_CONFIG": self.cfg.config_dir,
            "AMEESH_TURN_ID": turn_id,
            # #2894 : le document du tour, écrit avant que le harnais puisse
            # agir (voir `turn_context`) ; chemin stable pour un résident.
            turn_context.ENV: turn_context.path(self.state_dir),
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
        # #1645 : chaque geste gh du tour est consigné (agent, tour, lot), et
        # le tour porte le jeton de son rôle GitHub quand il est configuré.
        self._github_env(env, lot_du_tour)
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
                # session neuve : dossier temporaire vidé — jamais sous le travail
                # de fond d'un tour précédent encore en délai de grâce
                fond = getattr(self.runner, "background", None)
                # #2610 : une rotation ouvre la session neuve sur le résumé d'un
                # travail EN COURS ; le dossier temporaire de l'agent est gardé
                # (le résumé peut désigner ses fichiers). Le vidage reste pour
                # une vraie session neuve (démarrage, fin de lot, redémarrage).
                # #2619 : un brief `ameesh` qui continue la session perdue
                # (inconnue du harnais après un résumé non produit) est aussi
                # une continuation, pas une vraie session neuve.
                suite_de_rotation = bool(self.resume_summary) \
                    and (self.resume_origin == "agent" or self.resume_continuation)
                menage_tour = workspaces.before_turn(
                    self.cfg, self.runner.housekeeping_policy(), self.name, cwd,
                    new_session=not session and not suite_de_rotation
                    and not (fond and fond.has_jobs(self.name))
                    and self.resident is None,
                    base_env=env)
                env.update(menage_tour.env)
            except Exception as exc:
                log("[%s] cleanup before the turn failed (%s)" % (self.name, exc))
                menage_tour = None
        events_path = self._path("events.jsonl")
        stderr_path = self._path("stderr.log")
        started = time.time()
        # #2820 : avant les marqueurs du tour, qui en ouvrent le flux neuf
        self._rotation_du_flux(events_path)
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
        #: CTX-03b : `modelUsage` et frontière de compaction du fil principal
        #: (Claude), collectés sur le même flux que les compteurs ; le profil
        #: résident, lui, vient du driver.
        model_usage = None
        compaction_event = None
        cost: float | None = None
        error: str | None = None
        result_code = 0
        self.last_output = ""
        self.host_yield = None
        self.last_final = None
        self.summary_display = []
        self._summary_chars = 0
        # L105 : plafonds pendant le tour — contexte du dernier appel (une
        # fois par appel) et durée ; jamais sur un tour de résumé (rotation,
        # bascule de compte)
        plafonne = spec.get("prompt") != adapters.SUMMARY_PROMPT
        #: L105 : somme des relectures du tour (grand livre, information)
        relus = 0
        #: L60 (#2336) : contexte du dernier appel du fil principal — la taille
        #: de la session, mesure du plafond de contexte
        contexte_appel = 0
        #: L217 : jetons relus par les sous-agents, à part et pour information
        #: (jamais comptés au plafond de contexte, qui mesure le fil principal)
        relus_sous_agents = 0
        relus_mesures = False  # L217b : le flux a relevé au moins un appel
        appels_vus: set = set()
        cloture: str | None = None
        cloture_budget = False
        budget_relecture_actif = (self.cfg.turn_reread_budget > 0
                                  or self.cfg.session_reread_budget > 0)
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
                                   work_item_id=(str(lot_comptable)
                                                 if lot_comptable else None),
                                   turn_id=turn_id):
            self.fail_turn("accounting unavailable",
                           "accounting marker not written")
            return False
        # #1469 : réservation atomique du tour sur l'enveloppe du compte de
        # facturation, sous la clé du marqueur (le règlement la retrouve par
        # elle, jamais par agent) ; refusée, le tour ne part pas et le
        # marqueur est retiré. Un résumé de bascule peut imposer l'ancien
        # compte : réserver celui appliqué au harnais (`compte`).
        from . import turn_budget
        marqueur = self._compta_travail or {}
        refus = turn_budget.reserve_local(
            self.cfg, self.db, self.name, compte,
            self._spend_key(events_start, marqueur), turn_id,
            canon_id=self._canon_id(),
            retry=spec.get("prompt") == adapters.RETRY_PROMPT,
            memory=self.runner._tranche_selectors)
        if refus:
            registry.pending_spend_clear(self.db, self.name,
                                         marqueur)  # rien n'est parti
            self._compta_travail = None
            self.fail_turn(budget_mod.PAUSE_PREFIX + refus, refus)
            return False

        # #2894 (TPK #2167 A1) : le document de tour, écrit après le marqueur
        # comptable et la réservation de compte (l'état ouvert au lancement),
        # et avant que le harnais puisse agir. Écriture atomique et privée ;
        # un échec ne lance pas le harnais et ne laisse ni marqueur ni
        # réservation ouverts derrière lui.
        marqueur = self._compta_travail or {}
        spend_key = self._spend_key(events_start, marqueur)
        try:
            reservation = account_budget.reservation(self.db, spend_key)
            document = turn_context.build(
                turn_id=turn_id, agent=self.name, host=self.runner.host,
                kind=spec.get("kind") or "prompt", started_ts=started,
                session=session, work_item_id=lot_du_tour,
                lease_owner=self.runner.runner_id, lease_epoch=self.epoch,
                lease_expires_ts=self.lease_deadline or None,
                requests=spec.get("rows") or (),
                harness=harness, model=model or None,
                budget_usd=self.agent.get("budget_usd"),
                spent_usd=self.agent.get("spent_usd"),
                spend_key=spend_key, marker=marqueur,
                reservation=reservation)
            turn_context.write(self.state_dir, document,
                               secrets=turn_context.environment_secrets())
        except (OSError, turn_context.ContextError, db_mod.DbError) as exc:
            self._annule_tour_avant_lancement(marqueur, spend_key)
            self.fail_turn("turn context unavailable",
                           "turn context not written: %s" % exc)
            return False

        # #5006 (B1 cx2, dev3) : dernier contrôle avant le lancement, préparation
        # faite. Un réveil devenu sans objet ne part pas ; rien de comptable ne
        # reste ouvert, `begin_turn` est défait et l'échéance attend le nouvel
        # assigné.
        if self._wake_outdated(spec):
            self._annule_tour_avant_lancement(marqueur, spend_key)
            registry.set_status(self.db, self.name, "idle",
                                status_text="wake outdated: no turn")
            return False

        preempt_done = threading.Event()
        preempt_thread = threading.Thread(
            target=self._preempt_monitor, args=(preempt_done,),
            daemon=True, name="preempt-monitor")
        # Un nouveau tour remet à zéro le latch de publication : il ne concerne
        # que la course du tour précédent, et les autres déclencheurs (bail,
        # arrêt, préemption) sont revérifiés à la publication (verdict B5).
        self.stop_requested.clear()
        self.host_yield = None
        # #2729 : le latch de préemption ne vaut que pour le tour qui l'a reçu.
        # Il n'était consommé qu'au tour prioritaire : après une préemption, un
        # tour ordinaire restait tué à la seconde et la boucle ne s'arrêtait
        # plus (185 tours sur mty-amh-impl-01, 19/10).
        # La consommation est bornée par la GÉNÉRATION figée à la sélection
        # (`pick()`, ou l'entrée pour un appel direct) : seule une demande
        # ANTÉRIEURE est un résidu. Une demande arrivée depuis la sélection —
        # dispatch pendant la préparation — est un arrêt pour CE tour : on ne
        # l'efface pas, le moniteur ci-dessous la voit (B1 de la relecture).
        # Le test et le clear sont sous le verrou de `request_preempt` : pas de
        # course entre « la génération a-t-elle bougé ? » et l'effacement.
        frontiere = spec.get("_preempt_gen")
        if frontiere is None:
            frontiere = getattr(self, "preempt_gen", 0)
        with self._preempt_lock:
            if self.preempt_gen <= frontiere:
                self.preempting.clear()
        preempt_thread.start()
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
        from .platform import child_environment
        env = child_environment.harness(env, harness, compte)
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
        #: L80b : le `TurnResult` du tour résident, écrit après la barrière steer
        fin_resident = None
        #: #210 : l'empreinte du jeton qui part avec le tour, lue au lancement —
        #: le propriétaire peut se reconnecter sur un autre jeton pendant le tour
        empreinte = sentinel.launch_fingerprint(harness, compte, env)
        #: #408 : le harnais du lancement, origine d'une session qu'il ouvre
        harnais_lance = harness
        #: L106 : le harnais n'a pas pu être exécuté (binaire disparu depuis
        #: la résolution, interpréteur absent) — erreur de l'hôte
        echec_lancement = ""
        lignes = 0
        #: CTX-02b : normalisateur du tour, créé après le lancement (mode
        #: résident ou non) ; None quand le tour n'a pas atteint son flux.
        normalizer = None
        conversation_recorder = None
        conversation_usage = None
        proc = None
        background_handles = {}
        self._background_handles = background_handles
        #: L226b : début de la sortie d'erreur de CE tour dans `stderr.log`
        #: (ouvert en ajout) : l'échec d'un tour précédent n'y est pas relu
        stderr_debut = None
        #: L498 : cgroup délégué du tour. None = repli PID/PGID (délégation
        #: absente) ou tour exempté (le scope de l'auditeur est un frère de
        #: l'unité, hors d'elle : décision de route). Le placement précède
        #: l'exec du harnais ; un placement refusé refuse le lancement.
        cgroup_path = None
        ready_r = ready_w = None
        # CTX-02b : l'observation du tour part d'un état neuf ; la base
        # `R_session` est relue du grand livre AVANT le lancement (la requête
        # ne retarde jamais le harnais ni son flux).
        self._context_observation = None
        self._context_profile_observed = None
        self._context_profile_mode = None
        self._turn_close_observation = None
        self._context_baseline = self._context_session_baseline(session)
        try:
            from .platform import turn_cgroup
            with open(stderr_path, "ab") as stderr:
                stderr_debut = stderr.tell()
                if cle_resident is not None:
                    # #2894 : le pointeur de tour du résident est écrit AVANT
                    # le démarrage du pilote (le document de tour l'est déjà).
                    # Sans lui, le hook lirait le tour précédent : ce tour
                    # part un par événement plutôt que d'être mal fencé.
                    try:
                        resident_runner.write_turn(
                            self._path(resident_runner.TURN_FILE), turn_id)
                    except OSError as exc:
                        log("[%s] resident turn file not written (%s): one turn "
                            "per event" % (self.name, exc))
                        cle_resident = None
                if cle_resident is not None:
                    # L76 : le processus résident (démarré, ou relancé sur la
                    # session) ; un démarrage raté : un tour par événement
                    rp = self._resident_for_turn(
                        adapter, key=cle_resident, session=session, cwd=cwd, env=env,
                        model=model, effort=effort, tier=tier, lot=lot_du_tour,
                        stderr_path=stderr_path)
                if rp is None:
                    # L498 : le cgroup délégué ne concerne que le lancement d'un
                    # harnais par événement ; le processus résident (L76) sert
                    # plusieurs tours et ne prend pas de cgroup par tour.
                    if not audit_exempt:
                        root = self.runner.turn_cgroup_root()
                        if root is not None:
                            # Publish creation and ownership together: reconciliation
                            # must never see a directory before its live turn id.
                            with self.runner._turn_cgroups_lock:
                                cgroup_path = turn_cgroup.create(root, turn_id)
                                if cgroup_path is None:
                                    log("[%s] turn %s refused: cannot create a turn cgroup "
                                        "under %s" % (self.name, turn_id[:8], root))
                                    raise _LancementImpossible(
                                        "turn cgroup refused: cannot create %s/turn-%s"
                                        % (root, turn_id))
                                self.runner._turn_cgroups_live.add(turn_id)
                            ready_r, ready_w = os.pipe()
                    try:
                        # Le moissonneur de l'exécuteur ne touche jamais un harnais,
                        # même pas pendant son lancement (voir `background`).
                        with self.runner.launch_lock:
                            if cgroup_path is not None:
                                commande = turn_cgroup.command(
                                    cgroup_path, ready_w,
                                    self.runner.audit_command(
                                        self.name, argv, self.agent, exempt=False,
                                        seconds=audit_seconds))
                            else:
                                commande = self.runner.audit_command(
                                    self.name, argv, self.agent, exempt=audit_exempt,
                                    seconds=audit_seconds)
                            proc = subprocess.Popen(
                                commande,
                                cwd=cwd, stdout=subprocess.PIPE, stderr=stderr,
                                text=True, bufsize=1, env=env,
                                start_new_session=True,  # son groupe : on peut le tuer en entier
                                pass_fds=(ready_w,) if ready_w is not None else (),
                            )
                            self.runner.harness_pids.add(proc.pid)
                    except RuntimeError as exc:
                        raise _LancementImpossible(str(exc)) from exc
                    except OSError as exc:
                        raise _LancementImpossible("could not launch %s: %s"
                                                   % (argv[0], exc.strerror or exc)) from exc
                    finally:
                        # Le parent ne garde pas l'écriture : sinon la lecture du
                        # READY attendrait son propre descripteur jusqu'à la borne.
                        if ready_w is not None:
                            os.close(ready_w)
                            ready_w = None
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
                self._open_turn_resource(turn_id, proc.pid, cgroup=cgroup_path)
                # L498 : le trampoline a validé le placement AVANT d'exec le
                # harnais ; sans READY, le lancement est refusé — aucun repli
                # silencieux, aucun courrier consommé.
                if ready_r is not None:
                    ready = self._read_turn_cgroup_ready(ready_r)
                    os.close(ready_r)
                    ready_r = None
                    if ready != turn_cgroup.READY:
                        log("[%s] turn %s refused: turn cgroup placement not validated "
                            "(missing READY, harness not run)" % (self.name, turn_id[:8]))
                        raise _LancementImpossible(
                            "turn cgroup not validated: placement refused or no READY")
            # Le scope du tour exempté (auditeur) valide son plafond mémoire
            # AVANT de lancer le harnais ; l'attente du READY est bornée par le
            # minuteur indépendant. Un scope refusé ne consomme aucun courrier.
            if audit_exempt and (proc.stdout is None
                                 or proc.stdout.readline().strip() != audit_guard.READY):
                raise _LancementImpossible("auditor scope not validated: memory cap "
                                           "missing or scope creation refused")
            # #2466 : le trampoline et le scope auditeur ont validé le
            # placement ; pour un harnais par événement il va être exec'é —
            # l'échéance du réveil programmé peut être consommée. Un refus
            # READY ci-dessus laisse le claim expirer et le réveil sera
            # délivré de nouveau. Le résident, lui, n'est consommé qu'après
            # un send accepté (plus bas).
            if rp is None:
                self._turn_launched = True
                self._consume_scheduled_wake(spec)
            # Lancé (et publié, pour que l'arrêt le trouve). La remise n'est
            # constatée qu'APRÈS l'acceptation de la consigne par le harnais
            # (L77/B2) : un `driver.send` en échec ne solde jamais un message
            # jamais présenté ; il repart en réservation/livraison.
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
                    # #2466 : le tour résident a démarré (send accepté) —
                    # seulement maintenant l'échéance est consommée. Un
                    # ResidentError laisse le claim expirer et le réveil
                    # sera délivré de nouveau.
                    self._turn_launched = True
                    self._consume_scheduled_wake(spec)
                    self._livre_courrier(spec)  # consigne acceptée : remise
                    flux = (json.dumps(raw, ensure_ascii=False)
                            for raw, _parsed in rp.stream(tour_resident))
                except resident_mod.ResidentError as exc:
                    error = "resident turn not started: %s" % exc
                    flux = ()
            else:
                self._livre_courrier(spec)
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
            # CTX-02b : l'observation se collecte sur le MÊME flux que les
            # compteurs legacy, sans le relire ; elle est close après le flux
            # et publiée avec la ligne du grand livre, jamais avant.
            normalizer = context_metrics.TurnNormalizer(
                harness, session=session, turn=turn_id, model=model or None,
                mode="resident" if rp is not None else "non-resident",
                session_reread=self._context_baseline,
                session_reread_quality=self._context_baseline_quality,
                session_reread_reasons=self._context_baseline_reasons)
            with open(events_path, "a", encoding="utf-8") as events:
                for raw in flux:
                    line = raw.rstrip("\n")
                    if not line:
                        continue
                    lignes += 1
                    if lignes == 1 and spec.get("_etapes") is not None:
                        # #3218 : un horodatage seulement — le flux n'attend
                        # jamais la base (L78), le reçu s'écrit après lui
                        spec["_etapes"]["first_output_ts"] = time.time()
                    # #2096 : la ligne brute est écrite telle quelle (`raw` finit
                    # par son saut de ligne) — `line + "\n"` en faisait une copie
                    # du même ordre que le pic du tour.
                    events.write(raw if raw.endswith("\n") else raw + "\n")
                    events.flush()
                    parsed = self._parse(adapter, line)
                    if isinstance(parsed.get("model_usage"), dict):
                        # CTX-03b : la dernière métadonnée runtime du fil
                        # principal donne le profil du tour (R1 : jamais la
                        # première entrée d'un `modelUsage` auxiliaire).
                        model_usage = parsed["model_usage"]
                    if isinstance(parsed.get("compaction"), dict):
                        # CTX-03b : frontière de compaction native du fil
                        # principal ; un sous-agent n'en publie pas.
                        compaction_event = parsed["compaction"]
                    try:
                        normalizer.observe(parsed)
                    except Exception as exc:  # noqa: BLE001 — observation seule
                        # Un flux inattendu ne prive jamais le tour de sa
                        # comptabilité ni de sa décision legacy (L13).
                        log_async("[%s] context observation skipped an event (%s)"
                                  % (self.name, " ".join(str(exc).split())[:160]))
                    if conversation_recorder is not None:
                        conversation_recorder.on_event(line, parsed)
                        if isinstance(parsed.get("usage"), dict):
                            conversation_usage = parsed["usage"]
                    if parsed.get("session") and not session_new:
                        session_new = parsed["session"]
                        self._note_session(session_new, session, harness, compte,
                                           cwd, resume, harnais_lance)
                    if parsed.get("model") and parsed["model"] != modele_annonce:
                        modele_annonce = parsed["model"]  # source du tour (L13 B4)
                        self._compta_annonce(modele_annonce)  # persisté tout de suite
                    for text in parsed.get("display") or []:
                        log("[%s] %s" % (self.name, text))
                        # #2096 : borner avant de concaténer — le texte affiché
                        # peut faire des mégaoctets et la copie entière ne sert
                        # qu'à en garder les 4 000 derniers caractères.
                        self.last_output = (self.last_output + "\n"
                                            + text[-4000:])[-4000:]
                        if not plafonne:
                            self._keep_summary_display(text)
                    if "final" in parsed:
                        self.last_final = adapters.bound_final(str(parsed["final"] or ""))
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
                                contexte_appel = adapters.context_tokens(harness, appel)
                                relus += adapters.reread_tokens(harness, appel)
                        appel = parsed.get("subagent_call_usage")
                        if isinstance(appel, dict):
                            ident = parsed.get("call_id")
                            if not ident or ident not in appels_vus:
                                if ident:
                                    appels_vus.add(ident)
                                relus_sous_agents += adapters.reread_tokens(harness, appel)
                        # CTX-04a : les plafonds passent avant le budget, même
                        # s'il a déjà demandé la clôture de ce tour : un plafond
                        # de contexte atteint avant le point sûr garde sa rotation.
                        if (cloture is None or cloture_budget) and not clos:
                            plafond_atteint = self.turn_close_due(started, contexte_appel)
                            if plafond_atteint:
                                cloture, cloture_budget = plafond_atteint, False
                                # CTX-02b : le motif et les valeurs qui ont
                                # fermé le tour, publiés avec l'observation ;
                                # aucune décision ne les relit.
                                self._turn_close_observation = {
                                    "kind": "context_cap"
                                            if cloture.startswith("context cap")
                                            else "duration",
                                    "reason": cloture,
                                    "context_tokens": contexte_appel,
                                    "context_max_tokens": self.context_max_tokens(),
                                    "elapsed_s": round(time.time() - started, 3),
                                    "turn_max_seconds": self.turn_max_seconds(),
                                }
                                log_async("[%s] %s: turn closed at the next safe point"
                                          % (self.name, cloture))
                        if cloture is None and not clos and budget_relecture_actif and any(
                                cle in parsed for cle in ("call_usage", "usage_total", "usage")):
                            budget_du = self.reread_budget_due(normalizer.live_reread())
                            if budget_du:
                                cloture, cloture_budget = budget_du["reason"], True
                                self._turn_close_observation = budget_du
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
                        # #4448 : `turn.completed` de Codex porte l'usage
                        # CUMULÉ du fil, pas le tour. L'additionner à chaque
                        # tour sommait des cumuls (un fil de 3 tours comptait
                        # ~6 fois son dernier relevé) et faisait tourner une
                        # session dès le minimum de tours, quel que soit le
                        # seuil. Le cumul du fil est la mesure de la session ;
                        # les autres harnais publient un usage par tour ou par
                        # étape, qui s'additionne.
                        if harness == "codex":
                            self.session_tokens = max(self.session_tokens,
                                                      float(entree + sortie))
                        else:
                            self.session_tokens += float(entree + sortie)
                    if parsed.get("cost") is not None:
                        cost = parsed["cost"]
                    if parsed.get("error"):
                        error = str(parsed["error"])
                    if self.lease_lost.is_set():
                        error = "lease lost during the turn"
                        self.stop_group_now("lease lost during the turn", grace=0, hard=True)
                        break
            self._recu_etapes(spec)  # #3218
            # #2096 : les tampons du dernier événement (ligne brute, dictionnaire
            # parsé) ne vivent pas plus que le flux — la suite du tour (attente du
            # harnais, compta, conversation) ne doit pas garder le pic.
            raw, line, parsed = "", "", {}
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
                result_code, erreur_resident, session_resident, fin_resident = \
                    self._resident_result(
                        rp, tour_resident, clos or self.host_yield is not None)
                error = error or erreur_resident
                # L80a : un tour résident Codex/ACP n'annonce sa session que
                # dans son `TurnResult` (le flux brut est du JSON-RPC) ; sans
                # cet enregistrement, le tour suivant la verrait changée et
                # relancerait le processus sur une session neuve.
                if session_resident and not session_new:
                    session_new = session_resident
                    self._note_session(session_new, session, harness, compte,
                                       cwd, resume, harnais_lance)
        except _LancementImpossible as exc:
            echec_lancement = str(exc)
            result_code = 127
        finally:
            preempt_done.set()
            # L77/B8 : jonction AVANT tout solde de steer — un ACK de
            # `driver.steer` arrivé après le solde laisserait le message réservé.
            preempt_thread.join(PREEMPT_JOIN_S)
            if preempt_thread.is_alive():
                log_async("[%s] preemption monitor still alive %.0fs after the turn"
                          % (self.name, PREEMPT_JOIN_S))
            pgid = self.pgid
            if rp is not None:
                self._resident_turn_end(rp)
                # L80b B2 : la mesure et les entrées se lisent APRÈS la
                # barrière steer de `_resident_turn_end` — un ACK accepté
                # pendant la clôture du tour compte dans ce tour.
                if fin_resident is not None:
                    ecrit, erreur_flux = self._resident_usage_final(
                        rp, fin_resident, spec.get("ids") or ())
                    if not ecrit:
                        error = error or erreur_flux
                        result_code = result_code or 1
            else:
                # #3452 : un tour non résident n'écrit aucune mesure résidente ;
                # ses messages servis vont quand même au grand livre, sinon
                # `cost turns --message` ne rend rien pour la majorité des tours.
                self._write_served_inputs(
                    harness, spec.get("ids") or (), session_new or session,
                    "completed" if result_code == 0 and not error else "failed")
            # L498 : les descripteurs du READY ne survivent pas au tour (le
            # descripteur d'écriture est déjà fermé après le Popen).
            if ready_r is not None:
                os.close(ready_r)
                ready_r = None
            if ready_w is not None:
                os.close(ready_w)
                ready_w = None
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
                        turn_id, pgid, handles=background_handles,
                        cgroup=cgroup_path)
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
            # #3944 : le groupe ne quitte `harness_pids` qu'une fois inscrit en
            # grâce — entre les deux, la passe des orphelins (`_reap_strays`)
            # consommerait le statut du travail de fond.
            try:
                if fond is not None:
                    self.runner.background.start(
                        agent=self.name, turn_id=turn_id, pgid=pgid, members=fond[0],
                        containers=fond[1], grace_s=self.turn_grace_seconds(),
                        handles=background_handles, cgroup=cgroup_path)
            finally:
                if rp is None:
                    with self.runner.launch_lock:
                        self.runner.harness_pids.discard(pgid)
            if fond is None and rp is None:
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
                self._close_turn_resource(turn_id, pgid, survives=cleanup_survives,
                                          cgroup=cgroup_path)
            # Le cgroup du tour a été soit confié au suivi (`background.start`,
            # qui l'a enregistré), soit tué et fermé ci-dessus : il n'est plus
            # « ouvert » pour la réconciliation.
            with self.runner._turn_cgroups_lock:
                self.runner._turn_cgroups_live.discard(turn_id)
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
        # CTX-02b : clôture de l'observation du tour. Elle est publiée avec la
        # ligne du grand livre (`_compta_ecrit`) ; une mesure perdue (worker
        # mort, tour réparé) n'est jamais inventée. Le caractère partiel suit
        # exactement les motifs de tour interrompu déjà connus.
        if normalizer is not None:
            # #5148 : le flux Codex ne publie pas d'usage d'appel ; le journal
            # du fil le porte. La lecture est bornée au tour observé et n'est
            # pas conditionnée par `plafonne` (les tours de résumé en ont un).
            if normalizer.harness == "codex":
                self._observer_journal_codex(
                    normalizer,
                    session_new or session or normalizer.session,
                    started, started + duration)
            interrupted = bool(
                error or result_code or clos or self.host_yield is not None
                or self.lease_lost.is_set() or self.preempting.is_set()
                or self.restarting.is_set()
                # Un tour résident clos par l'exécuteur (cap, retrait) n'est
                # pas un échec décisionnel mais sa mesure est partielle.
                or getattr(fin_resident, "status", "") not in ("", "completed"))
            try:
                document = normalizer.finish(interrupted=interrupted)
                # Le modèle et la session effectifs du flux priment sur ceux
                # du lancement, comme pour le grand livre (L13 B4).
                document["session"] = (session_new or session
                                       or document.get("session"))
                document["model"] = modele_annonce or model or None
                # CTX-03b : la fenêtre observée du tour. Le driver résident
                # publie déjà son profil (modèle effectif, W, compaction) ;
                # un tour non résident le lit dans `modelUsage` du flux, sans
                # jamais prendre une entrée auxiliaire pour le fil principal.
                observed = None
                resident_profile = getattr(fin_resident, "profile", None)
                if isinstance(resident_profile, dict) and resident_profile:
                    observed = resident_profile
                elif model_usage is not None:
                    observed = context_profile.from_model_usage(
                        model_usage, announced=modele_annonce or None)
                self._context_profile_observed = observed
                self._context_profile_mode = (
                    "resident" if rp is not None else "non-resident")
                # CTX-03b : la compaction native du tour, résidente (driver)
                # ou non résidente (`compact_boundary` du flux). Absente, le
                # document la garde à None et la décision dit `compacted=false`
                # explicitement, pour que le rejeu ne suppose rien.
                resident_compact = getattr(fin_resident, "compact", None)
                if isinstance(resident_compact, dict):
                    compaction = dict(resident_compact)
                elif compaction_event is not None:
                    compaction = context_profile.compaction(compaction_event).to_dict()
                else:
                    compaction = None
                document["compaction"] = compaction
                self._context_observation = document
                self._remember_context_session(document, session_new or session)
                self._window_measure_keep(
                    document, bool(getattr(fin_resident, "compact", None)))
            except Exception as exc:  # noqa: BLE001 — observation seule
                log_async("[%s] context observation not published (%s)"
                          % (self.name, " ".join(str(exc).split())[:160]))
                self._context_observation = None
                self._context_profile_observed = None
                self._context_profile_mode = None
                self._invalidate_context_session(
                    session_new or session or self._context_session_id,
                    (context_metrics.REASON_COLLECTION_LOST,))
        self.last_turn_seconds = duration
        # L217 : fil principal et sous-agents, mesurés à part dans le flux
        self.last_turn_main_reread = relus
        self.last_turn_subagent_reread = relus_sous_agents
        # L217b/#2336 : la rotation entre deux tours juge sur le contexte du
        # dernier appel du fil principal (la taille de la session), jamais sur
        # la somme des relectures du tour.
        self.last_turn_context = contexte_appel if relus_mesures else None
        self.last_turn_context_session = session_new or session or None
        if self.last_turn_context is None and plafonne and harness == "codex":
            # #2932 : `codex exec --json` ne publie que l'usage cumulé du tour ;
            # le contexte du dernier appel se lit dans le journal de session
            self.last_turn_context = self._codex_last_call_context(
                self.last_turn_context_session)
        if resume and not session_new and not result_code and not error:
            self._resume_drop()  # tour achevé sans id de session : livré
        if relus_sous_agents:
            log_async("[%s] tokens reread in the turn: %d main thread, %d subagents "
                      "(outside the cap)" % (self.name, relus, relus_sous_agents))
        lost = self.lease_lost.is_set()
        preempte = self.preempting.is_set()
        # #2729 : la série ne compte que des tours **consécutifs** interrompus
        # par préemption ; tout autre tour la remet à zéro. Le début d'épisode
        # est figé à la première préemption (clé d'alerte stable).
        if preempte:
            if self.preemption_streak < 1:
                self.preemption_since = time.time()
            self.preemption_streak += 1
        else:
            self.preemption_streak = 0
            self.preemption_since = 0.0
        # Comptabilité **avant** toute sortie anticipée : la dépense a eu lieu,
        # même si le bail a été perdu ou le tour préempté (L13 B1). Le modèle
        # est celui du lancement, déjà figé dans le marqueur (L13 B4).
        # #1469 : issue incertaine (erreur, bail perdu, préemption, arrêt
        # par plafond) : la réservation du compte reste comptée en entier
        self._compta_termine(modele_annonce, incertain=bool(
            error or result_code or lost or preempte or clos))
        # #4448 B cx3 : le nombre de tours de la session est celui des tours
        # COMPTÉS au grand livre — réussis ou non : la dépense a eu lieu même
        # quand le harnais échoue (L13 B1), et toutes les sorties anticipées
        # ci-dessous ont déjà leur ligne. L'alerte `session_too_big` relit ce
        # même compte (`turn_costs`) : les deux mesures ne peuvent plus
        # diverger, ni dépendre d'un redémarrage du worker.
        self.session_turns += 1
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
                               status_text=self._fenetre_debit(
                                   "tour interrompu : l'hôte se retire (%s)" % raison, paused=True))
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
                               status_text=self._fenetre_debit(
                                   "turn stopped: restart requested", paused=True))
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
                               status_text=self._fenetre_debit(
                                   "turn interrupted: the host is withdrawing (%s)" % raison, paused=True))
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
            from . import exploitation as exploitation_mod
            texte_fin = self._fenetre_debit(exploitation_mod.preemption_status_text(
                "turn interrupted: priority message", self.preemption_streak,
                since=self.preemption_since))
            if self.preemption_streak == exploitation_mod.PREEMPTIONS_ALERT:
                # #2729 : une alerte, pas seulement une ligne de journal — sans
                # elle, 185 tours préemptés d'affilée sont passés inaperçus.
                log_async("[%s] ALERT: %d consecutive turns interrupted by priority "
                          "preemption" % (self.name, self.preemption_streak))
            self._avec_reprise("instruction put back in the queue", registry.restore_prompt,
                               self.db, self.name, self.runner.runner_id, self.epoch)
            self._avec_reprise("end-of-turn status", registry.set_status,
                               self.db, self.name, "queued",
                               status_text=texte_fin)
            self._fil_note(
                "Turn interrupted by a priority message; the turn's instruction "
                "goes back in the queue and the priority message goes first.",
                meta={"action": "interruption", "tour_s": round(duration, 1),
                      "preemptions": self.preemption_streak})
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
                    error="%s : %s" % (self.HOST_ERROR, hote), cost_usd=cost,
                    turn_id=self._turn_id)
                self.last_activity = time.monotonic()
                self._turn_end_mono = self.last_activity  # #3451
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
                    error="%s %s" % (auth_failure.STATUS_PREFIX, raison_auth), cost_usd=cost,
                    turn_id=self._turn_id)
                self.last_activity = time.monotonic()
                self._turn_end_mono = self.last_activity  # #3451
                self._auth_note(nom_harnais, account, raison_auth, texte, turn=True)
                return False
            # #4164 : le compte fournisseur n'a plus de crédit — pas une panne
            # passagère ni un échec rapide : le compte passe `exhausted`, la
            # consigne reste demandée, aucun autre tour du compte n'est accordé
            # (hors sonde bornée) jusqu'à son retour à `ok`.
            stderr_credits = auth_failure.stderr_tail(stderr_path, start=stderr_debut)
            raison_credits = None if self.runner.mediated else provider_credits.classify(
                harness, error, stderr_credits)
            if raison_credits:
                self.fast_failure = False
                account = auth_failure.account_name(compte)
                sonde = self._credits_probe
                self._credits_probe = None
                transition = self._avec_reprise(
                    "provider credits", provider_credits.mark_exhausted, self.db,
                    harness, account, reason=raison_credits,
                    detail=provider_credits.detail_of(error, stderr_credits),
                    source="probe" if sonde else "turn", agent=self.name,
                    host=self.cfg.host)
                if self._session_en_attente is not None:
                    self._avec_reprise("turn session", self._enregistre_session)
                texte = provider_credits.status_text(harness, account)
                self._avec_reprise(
                    "end of turn", registry.end_turn,
                    self.db, self.name, self.runner.runner_id, self.epoch,
                    status="blocked", status_text=texte,
                    error="%s %s" % (provider_credits.STATUS_PREFIX, raison_credits),
                    cost_usd=cost, turn_id=self._turn_id)
                self.last_activity = time.monotonic()
                self._turn_end_mono = self.last_activity  # #3451
                if transition or not sonde:
                    self._credits_note(harness, account, raison_credits, texte, turn=True)
                else:
                    self._credits_status = texte  # sonde négative : rien de neuf à dire
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
            # #1270 : une panne passagère du fournisseur (429, 5xx, flux
            # DeepSeek) n'est pas un échec rapide — elle attend puis reprend
            # seule, à délais cadencés. Un quota épuisé suit la même cadence
            # mais reste compté par L48 : cinq refus arrêtent l'agent, un
            # plafond n'est pas repris sans fin ; la garde comptable (L226b)
            # reste seule juge du budget. Un échec d'une autre cause
            # efface l'épisode (hors refus de session fille, qui n'est pas une
            # panne fournisseur et n'efface ni n'étend l'épisode).
            stderr_debit = auth_failure.stderr_tail(stderr_path, start=stderr_debut)
            genre = rate_limit_mod.classify(
                error, self.last_final, stderr_debit)
            if genre:
                if genre == rate_limit_mod.RATE_LIMITED:
                    self.fast_failure = False
                if genre == self.rate_limit:
                    self.rate_limit_streak += 1
                else:
                    self._clos_episode_debit(started)
                    self.rate_limit = genre
                    self.rate_limit_streak = 1
                    self.rate_limit_since = time.time()
                # échéance absolue, écrite dans `status_text` : elle survit au
                # redémarrage et ne se recalcule pas à chaque réveil (#1270 B1)
                self.rate_limit_retry_at = (time.time() + rate_limit_mod.delay(
                    self.rate_limit_streak))
                # #3779 : un 429 du fournisseur est aussi un refus du BUCKET
                # COURT du compte, partagé par toutes ses sessions. On le
                # journalise en base pour que les autres agents du compte
                # s'arrêtent avec le fautif, au lieu de relancer chacun leur
                # tour et d'entretenir la rafale.
                detail_429 = "\n".join(
                    texte for texte in (str(error or ""), stderr_debit or "") if texte)
                if (genre == rate_limit_mod.RATE_LIMITED
                        and account_window.enabled(harness)
                        and ("429" in detail_429
                             or "Too Many Requests" in detail_429)):
                    nom_compte = getattr(compte, "name", None)
                    if nom_compte:
                        account_window.record_rate_limit(
                            self.db, harness, nom_compte, self.cfg.host,
                            self.name, detail_429)
            elif not session_fille_ecartee:
                self._clos_episode_debit(started)
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
        sonde_credits, self._credits_probe = getattr(self, "_credits_probe", None), None
        if ok and sonde_credits:
            # #4164 : la sonde bornée a abouti — le compte a de nouveau du
            # crédit ; les agents retenus reprennent à leur prochain sondage
            if self._avec_reprise("provider credits", provider_credits.mark_ok,
                                  self.db, sonde_credits[0], sonde_credits[1],
                                  source="probe", agent=self.name, host=self.cfg.host):
                log_async("[%s] provider account %s has credits again (probe turn "
                          "succeeded): held agents resume"
                          % (self.name, provider_credits.who(*sonde_credits)))
                self._fil_note(
                    "Probe turn succeeded: provider account %s has credits again, "
                    "its held agents resume in order." % provider_credits.who(*sonde_credits),
                    meta={"action": "provider_credits_reprise", "source": "sonde"})
        if ok:
            self.failure_streak = 0
            self.failure_since = 0.0
            self._clos_episode_debit(started)
            self.rate_limit, self.rate_limit_streak, self.rate_limit_since = None, 0, 0.0
            self.rate_limit_retry_at = 0.0
        elif not session_fille_ecartee:
            if self.failure_streak < 1:
                # début d'épisode : il ne bouge plus tant qu'un succès n'a pas
                # remis la série à zéro (clé d'alerte stable, #1268 B1)
                self.failure_since = time.time()
            self.failure_streak += 1
        texte_fin = turn_status_text(
            shown, duration, ok=ok,
            cloture=cloture if ok and clos and cloture else "")
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
        # #3733 : le cumul de débit de l'heure survit au succès
        texte_fin = self._fenetre_debit(texte_fin)
        self._avec_reprise(
            "end of turn", registry.end_turn,
            self.db, self.name, self.runner.runner_id, self.epoch,
            status=status, status_text=texte_fin,
            error=error, cost_usd=cost,
            turn_id=self._turn_id,
        )
        self.last_activity = time.monotonic()
        self._turn_end_mono = self.last_activity  # #3451
        self._backup_session(adapter, compte, session_new or session)
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

    def retry_due(self) -> bool:
        """#1270 : un tour de reprise est dû — l'échéance d'un épisode de
        débit passager est passée. Seul un tour de consigne remet sa consigne
        en attente ; un tour de courrier, d'événements ou de reprise en échec
        ne laisse rien (le courrier est soldé au lancement), et l'agent
        restait `blocked` jusqu'au courrier suivant ou à la relance
        d'inactivité — cas réels du 14/10 (Codex, tour « événements ») et du
        15/10 (DeepSeek, tour « messages »). Un quota épuisé n'est pas repris
        ainsi : il n'est pas repris sans fin."""
        return (self.rate_limit == rate_limit_mod.RATE_LIMITED
                and self.retry_remaining() <= 0)

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
        jusqu'à l'échéance durable (`retry at`) — jamais un délai repoussé par
        un réveil ; L48 n'arrête jamais un débit passager, un quota si."""
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
        self._peek_reader = getattr(adapter, "reader", None)
        try:
            return adapter.parse(line)
        except Exception as exc:  # un flux inattendu ne tue pas l'exécuteur
            return {"error": "unreadable stream: %s" % exc}

    # -- boucles -----------------------------------------------------------
    #: L165 (EP2) : fréquence de vérification d'une bascule planifiée
    HANDOVER_CHECK_S = 60.0
    # #4231: three observations, rather than one transient pressure sample.
    UNREADY_HANDOVER_S = 120.0

    def _election_handover(self) -> dict | None:
        """Une bascule planifiée est-elle due à cette fin de tour ? (EP2)

        Lit la ligne fraîche du registre (l'échéance du bail y est renouvelée)
        et laisse `election.handover_due` juger l'hystérésis : un challenger
        resté ≥ 5 points de disponibilité au-dessus du détenteur pendant une
        heure. Une indisponibilité continue de 120 s avec successeur prêt
        permet aussi une relève, uniquement entre les tours (#4231).
        """
        if (self.agent or {}).get("releve") != "election":
            return None
        now = _clock_of(self).monotonic()
        if now < getattr(self, "_handover_next", 0.0):
            return None
        self._handover_next = now + self.HANDOVER_CHECK_S
        from . import election
        try:
            row = registry.get(self.db, self.name)
            if row is None:
                self._unready_since = None
                return None
            skipped = self.runner.handover_failed(self.name)
            successor = election.unready_successor(self.db, row, skip=skipped)
            if successor is None:
                self._unready_since = None
            else:
                since = getattr(self, "_unready_since", None)
                if since is None:
                    since = self._unready_since = now
                elapsed = now - since
                if elapsed >= self.UNREADY_HANDOVER_S:
                    return dict(successor, seconds=elapsed, gap=0.0)
            return election.handover_due(
                self.db, row, self.runner.host, skip=skipped)
        except db_mod.DbError as exc:
            self._unready_since = None  # no evidence of continuous unavailability
            message = "elected-role handover not evaluated (%s)" % db_mod.explain(exc)
            if message != getattr(self, "_handover_error", None):
                log_async(message)
            self._handover_error = message
            return None

    def ensure_peek(self) -> None:
        if getattr(self, "_peek_publisher", None) is None:
            from .agent_peek import Publisher
            self._peek_publisher = Publisher(
                self.cfg, agent=self.name, owner=self.runner.runner_id, epoch=self.epoch,
                cwd=lambda: self.agent.get("cwd") or "",
                reader=lambda: getattr(self, "_peek_reader", None), warn=log_async)

    def _close_peek(self) -> None:
        publisher = getattr(self, "_peek_publisher", None)
        if publisher is not None:
            publisher.close()

    def process_once(self, wait_seconds: float) -> bool:
        """Mode banc : au plus un tour, en attendant `wait_seconds` du travail."""
        admission_lock.reset()  # #4602 : le fil appelant est ordinaire
        self.ensure_watchdog()
        self.ensure_peek()
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            migrations.require_compatible_writer(self.db)
            self._apply_pending_config()  # avant les gardes de rotation et de prise
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
            spec = self._pick_for_turn()
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

    def _idle_poll(self) -> float:
        """#4515 : le pas du sondage au repos de ce worker (voir
        `Runner.idle_poll`) ; une doublure d'exécuteur sans budget garde
        `poll`."""
        idle_poll = getattr(self.runner, "idle_poll", None)
        return idle_poll() if callable(idle_poll) else self.runner.poll

    def _admission_priority(self) -> None:
        """#4602 : applique au fil la classe de la passe qui commence.

        Un réveil de travail est consommé par cette passe : la passe suivante
        repart NORMAL, sauf nouvelle notification. Sans cela un worker réveillé
        une fois garderait la priorité pour toutes ses passes au repos."""
        prioritaire, self._priority_wake = self._priority_wake, False
        admission_lock.set_priority(
            admission_lock.CONTROL if prioritaire else admission_lock.NORMAL)

    def _admission_control(self) -> None:
        """#4602 : ne fait que MONTER en CONTROL pour un nouveau réveil.

        Le second appel, juste avant le choix du tour, ne doit jamais
        redescendre en NORMAL : sinon la passe réveillée perd sa priorité au
        moment précis où elle choisit et ouvre son tour (B2 de la relecture
        dev1). La classe est rendue au repos en fin de passe."""
        if self._priority_wake:
            self._priority_wake = False
            admission_lock.set_priority(admission_lock.CONTROL)

    def run(self) -> None:
        """Mode service : boucle jusqu'à l'arrêt, réveillée par NOTIFY."""
        self.ensure_watchdog()
        self.ensure_peek()
        try:
            while not self.stopping.is_set() and not self.runner.stop.is_set():
                try:
                    migrations.require_compatible_writer(self.db)
                except migrations.IncompatibleSchema as exc:
                    log_async("[%s] %s" % (self.name, exc))
                    break
                except db_mod.Unavailable as exc:
                    self._panne_de_base(exc, "schema admission")
                    continue
                # #4602 : la passe qui suit un réveil de travail passe avant
                # les passes de sondage des autres agents ; la classe est
                # consommée par cette passe (une nouvelle notification la
                # repose). Sans réveil, la passe reste NORMAL.
                self._admission_priority()
                self._apply_pending_config()  # une pause budget doit voir les comptes relus
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
                    retrait = self.admission_withdrawn()
                    if retrait:
                        # #3925 : plus admis (liste d'admission, fiche ou
                        # placement retirés) : entre deux tours, le bail est
                        # rendu et l'agent n'est plus réclamé
                        log("[%s] %s: lease released after the current turn"
                            % (self.name, retrait))
                        break
                    self.apply_restart_if_requested()
                    self.maybe_rotate()
                    # L165 (EP2) : bascule planifiée — le détenteur rend le bail
                    # en fin de tour, le challenger le réclame à son rang.
                    handover = self._election_handover()
                    if handover:
                        if handover.get("health_reason"):
                            message = ("Elected role handed over to %s: holder unready "
                                       "for %.0fs (%s); lease released at end of turn."
                                       % (handover["challenger"], handover["seconds"],
                                          handover["health_reason"]))
                        else:
                            message = ("Elected role handed over to %s: %.1f availability "
                                       "points ahead for %d min; lease released at end of turn."
                                       % (handover["challenger"], handover["gap"],
                                          max(1, int(handover["seconds"] // 60))))
                        log("[%s] %s" % (self.name, message))
                        self._fil_note(message, meta={"action": "election-handover",
                            "challenger": handover["challenger"],
                            "ahead_s": round(handover["seconds"], 1),
                            "health_reason": handover.get("health_reason")})
                        self.runner.note_handover_release(
                            self.name, handover["challenger"],
                            float(handover.get("delay_s") or 0.0))
                        break
                    # #1270 B1 : l'échéance de reprise durable garde le
                    # lancement du tour ; un courrier, un reset ou le budget
                    # ne la court-circuitent pas — l'attente consomme les
                    # réveils et renouvelle le bail. Aucune consigne n'est
                    # consommée avant l'échéance (`pick` est après).
                    attente_reprise = self.retry_remaining()
                    if attente_reprise > 0:
                        self._attendre_reprise(attente_reprise)
                        continue
                    # #4602 : une notification a pu arriver pendant les
                    # lectures de cette passe ; le choix du tour passe alors
                    # en CONTROL sans attendre la passe suivante — et sans
                    # jamais redescendre s'il était déjà CONTROL.
                    self._admission_control()
                    spec = self._pick_for_turn()
                    if spec is not None and self._moved_before_turn(spec):
                        break
                except db_mod.Unavailable as exc:
                    self._panne_de_base(exc, "choosing the turn")
                    continue
                self._base_retablie()
                if spec is None:
                    self._deferral_over()  # #1914 : plus rien à prendre
                    self._resident_between_turns()  # L76 : mort, sommeil
                    admission_lock.reset()  # #4602 : la passe prioritaire est finie
                    self.wake.wait(timeout=self.wait_timeout(self._until_due(
                        min(self._idle_poll(), max(1.0, self.runner.lease_ttl / 3.0)))))
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
                if reussi is None:
                    # #1686 N2 : tour différé (attach demandé ou verrou de
                    # session tenu) — la consigne est restaurée, le tour n'a pas
                    # échoué : ni compteur d'échecs rapides, ni attente d'échec
                    # croissante. On retente au pas court, sans boucle serrée et
                    # sans dépasser l'échéance du bail (le renouvellement du
                    # tour suivant doit rester possible) : le verrou est rendu à
                    # la fin du post-tour du résident. #1914 : un verrou tenu
                    # par un tiers au-delà du délai de grâce devient visible
                    # (journal + statut) et les reprises s'espacent.
                    self.wake.wait(timeout=self.wait_timeout(self._deferred_turn()))
                    self.wake.clear()
                    continue
                self._deferral_over()  # #1914 : la série de reports est finie
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
                attente = self.failure_wait()
                if getattr(self, "_admission_reportee", False):
                    # #3218 : un tour refusé à l'admission (fenêtre courte) n'a
                    # pas échoué — il repart à la levée datée, au plus au
                    # sondage suivant, jamais après l'attente d'échec.
                    self._admission_reportee = False
                    attente = self._until_due(min(attente, self.runner.poll))
                self._attendre_apres_echec(attente)
        finally:
            # #4602 : un worker qui sort ne laisse pas sa classe d'admission
            # derrière lui (le fil meurt, mais le nettoyage reste explicite).
            admission_lock.reset()
            # #1914 : le worker qui s'arrête ne laisse pas derrière lui un
            # blocage « session lock held » périmé (levé sous bail : si un
            # remplaçant a déjà repris la main, la marque n'est plus la nôtre
            # et rien n'est touché).
            self._deferral_over()
            self._mark_power_stop()
            self._close_peek()  # #1349 : l'aperçu ne survit pas au worker
            self.release_lease()


#: #1466 : les seuls champs qu'une relecture à chaud reprend du fichier. Liste
#: explicite, et non « tout sauf » : un champ nouveau reste figé tant qu'on ne
#: l'ajoute pas ici. Sont rechargeables ce qui décide une pause budget et la
#: lecture des comptes ; le reste (hôte, bail, dsn, schéma, dossier d'état,
#: cadences, porte d'hôte) a décidé des baux et des connexions en cours : le
#: changer sous un tour actif romprait ce qui tourne (R1 cx2 et dev2 sur #1466 :
#: schéma et `state_dir` appliqués aux workers sans reconnexion).
RELOADABLE_FIELDS = frozenset({
    # provenance de la lecture (journal de l'exécuteur, `ameesh doctor`)
    "config_path", "config_base", "config_shadowed",
    # comptes, plafonds et rôles : la décision de pause budget
    "accounts", "budget_usd_per_hour", "budget_usd_per_day", "budget_explicit",
    "critical_agents", "idle_after_s", "baseline_hosts",
    "noncritical_hourly_share", "critical_fallback_harnesses",
    # #2571 (suite de #313/#2580) : le partage des comptes est relu au tour
    # suivant et à la passe de revendications suivante ; un passage `report` →
    # `refuse`/`strict` (ou un registre déplacé) ne demande pas de redémarrage.
    "accounts_shared", "account_sharing", "account_registry",
    # binaires de harnais de l'hôte (L106), lus au lancement de chaque tour
    "harness_bins",
    # #3568 : la route de poussée (clé `notify`) décide le délégué de la passe
    # alerts que cet hôte publie avec son relevé de candidature ; l'ajouter
    # sans redémarrer évite de laisser un hôte fraîchement routé en `notify_ok`
    # faux jusqu'au prochain redémarrage de l'exécuteur.
    "notify",
})


def _admits_nobody(runner) -> bool:
    """#3925 : une liste d'admission vide (fichier vide, ou tout refusé)
    n'admet aucun agent, alors que `claimable` lit `[]` comme « tous ».
    Fonction et `getattr` : les doublures des tests passent à `Runner.sweep`
    un exécuteur sans cet attribut (sans filtre : tout est admis)."""
    flt = getattr(runner, "agents_filter", None)
    return flt is not None and not flt


class Runner:
    def __init__(
        self,
        cfg: Config,
        db: db_mod.Db,
        *,
        agents: list[str] | None = None,
        agents_file: str | None = None,
        once: bool = False,
        wait: float = 0.0,
        dry_run: bool = False,
        max_turns: int | None = None,
        take: dict | None = None,
        take_interval: float = TAKE_INTERVAL_S,
        take_balance: dict | None = None,
        train_passages=(),
        train_service_lease=None,
        train_role=None,
        train_refresh=None,
        cfg_overrides: dict | None = None,
    ):
        from .host_gate import gate_choice
        try:
            gate_choice(cfg, runner_id=cfg.runner)
        except ValueError as exc:
            raise SystemExit("agent-runner: %s" % exc) from exc
        self.cfg = cfg
        self._tranche_selectors = {}  # (canon_id, agent): last trusted tranche selector
        #: #1466 : options de ligne de commande (hôte, bail, sondage, réveil),
        #: décidées au démarrage. Elles ne se relisent pas — comme tous les
        #: champs hors `RELOADABLE_FIELDS` — et sont gardées ici pour la trace
        #: (l'hôte et le bail ont décidé les baux en cours).
        self._cfg_overrides = dict(cfg_overrides or {})
        #: #1466 : empreinte des fichiers lus au dernier chargement
        self._config_stamp = self.config_stamp()
        self.db = db
        #: #4602 : signale une attente longue sur la connexion partagée, pour
        #: rendre visible l'étage qui retarde le grant d'un tour réveillé.
        self._db_wait_seen: dict[str, float] = {}
        self._db_wait_lock = threading.Lock()
        admission_lock.set_wait_observer(self._db_wait_notice)
        self.host = cfg.host
        self.runner_id = cfg.runner
        from .agent_names import resolve
        self.agents_filter = (list(dict.fromkeys(
            resolve(n, db=self.db, operation="runner.filter") for n in agents))
            if agents is not None else None)
        #: #3925 : source d'admission côté hôte (`--agents-file`), relue à
        #: chaque passe. Le filtre devient alors `--agents` ∪ fichier, et la
        #: liste est modifiée EN PLACE (le canal de contrôle et les workers
        #: lisent le même objet). Une liste vide n'admet personne.
        self.agents_file = agents_file or None
        self._agents_static = list(self.agents_filter or ())
        self._admission_stamp: object = ()
        self._admission_file_names: list[str] = []
        self._admission_error: str | None = None
        self._control_channel = None
        if self.agents_file:
            self.agents_filter = list(self._agents_static)
            self.refresh_admission()
        self.once = once
        self.wait = wait
        self.dry_run = dry_run
        self.max_turns = max_turns
        #: #804 : réglages de la prise automatique que CET exécuteur déclenche
        #: pour ses propres agents (`None` : pas de prise) et sa cadence ; les
        #: seuils viennent des options et variables d'`ameesh notify`
        #: (`--take-*`, `AMEESH_TAKE_*`), lus dans `main`.
        self.take = take or None
        self.take_interval = (max(0.0, float(take_interval))
                              if self.take and take_interval else 0.0)
        #: #754 (owner, 13/10) : seuils de l'alerte `balance_low` lus des
        #: options `--balance-*` / `AMEESH_ALERT_BALANCE_*`, partagés avec
        #: `ameesh alerts` et `ameesh notify` (`exploitation.balance_thresholds`).
        self.take_balance = dict(take_balance or {})
        self._take_next = 0.0
        #: L165 (EP2) : publication périodique des scores de candidats de cet
        #: hôte (rôles `releve: election`), élagage de leur historique, et
        #: instant (monotone) du premier bail libre vu par rôle pour le délai
        #: par rang. Aucune écriture hors de `role_evaluation_once` (et de
        #: `_republish_reserved`, #4112).
        self._role_eval_next = 0.0
        #: #4112 : prochain instant (monotone) de republication, par rôle, du
        #: relevé d'un rôle réservé à cet hôte et lu « non prêt »
        self._reserved_republish_next: dict[str, float] = {}
        self._role_prune_next = 0.0
        self._role_error: str | None = None
        self._election_free_since: dict[str, float] = {}
        #: L165 (EP2) : `{rôle: (échéance monotone, challenger)}` — un détenteur
        #: qui a rendu le bail pour une bascule planifiée ne le reprend qu'au-delà
        #: du délai de rang du challenger plus une marge, sinon un challenger
        #: mort laisserait le rôle libre pour toujours.
        self._handover_released: dict[str, tuple[float, str]] = {}
        #: L165 (EP2) : `{rôle: {challenger: échéance monotone}}` — un
        #: challenger qui n'a pas réclamé dans sa fenêtre est écarté des
        #: bascules pendant `HYSTERESIS_S` : sans cela, le détenteur repris
        #: redonnerait le rôle au même challenger mort à chaque nouveau worker
        #: et ne prendrait jamais un tour (B1 de la relecture #3011).
        self._handover_failed: dict[str, dict[str, float]] = {}
        #: L165 (EP2) : les deux cartes ci-dessus sont relues dans
        #: `role_handover` au premier usage, pour survivre au redémarrage de
        #: l'exécuteur (N de la relecture #3011, panier #3099).
        self._handover_loaded = False
        self._handover_retry_at = 0.0
        self._handover_error: str | None = None
        # #1467: explicit qualified passages only; no live policy selected here.
        self.train_passages = tuple(train_passages)
        # #2446: the exclusive service lease assembled from `train.host`.
        self.train_service_lease = train_service_lease
        #: #3366: re-read the effective canon pin before each train passage.
        self.train_refresh = train_refresh
        # L165 (EP5): the elected role that gates the train passages, or None.
        self.train_role = train_role
        self.train_service = None
        self.lease_ttl = cfg.lease_ttl
        #: #2600 : horloge des workers de cet exécuteur (simulée en test).
        self.clock = SYSTEM_CLOCK
        self.idle_nudge = cfg.idle_nudge
        self.poll = max(1.0, cfg.poll)
        self._runner_seen_next = 0.0
        #: #2389 : dernier `no-land` journalisé, par lot et par feu (`fire_sha`).
        #: Un projet sans voie d'atterrissage déclarée reste `no-land` à chaque
        #: passage : le répéter à chaque tour noierait le journal, alors que
        #: l'abstention ne change pas tant que le feu est le même.
        self._fire_no_land: dict[int, str] = {}
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
        self._stop_signal_received = False
        self._drain_deadline = 0.0
        self._drain_since = 0.0
        self._drain_woken = False
        self._power_state = "unknown"
        self._power_thread: threading.Thread | None = None
        adapters.configure(cfg)
        self.stop = threading.Event()
        self.wake_all = threading.Event()
        self.workers: dict[str, AgentWorker] = {}
        #: L93 : canaux locaux « mon poste » (état en lecture seule, arrêt propre)
        self.device_channels: list = []
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
        #: #2814 : mesures du cgroup de CE processus (PSI, quota, throttling,
        #: mémoire), lues à chaque verdict frais ; le compteur de throttling
        #: a besoin de deux lectures (None à la première)
        self._cgroup = resources_mod.CgroupMeter()
        #: #2814 : fournisseur de capacité enfichable (no-op par défaut) ;
        #: il ne lance aucun tour, il signale « capacité nécessaire/libérée »
        from . import capacity as capacity_mod
        self.capacity = capacity_mod.Manager(log=log_async)
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
        self._housekeeping = workspaces.default_policy()
        #: L162 : jamais deux passages de ménage à la fois (le périodique, et
        #: celui qu'une pression du tmpfs déclenche tout de suite)
        self._housekeeping_lock = threading.Lock()
        #: travail de fond des tours finis, en délai de grâce (suivi par le
        #: fil `fond` en mode service) ; pids des harnais lancés, que le
        #: moissonneur des orphelins adoptés ne touche jamais (lancement
        #: compris : `launch_lock`)
        from . import background as background_mod
        self.background = background_mod.Tracker(self, log=log_async)
        #: L498 : racine cgroup déléguée de cette unité (None = repli
        #: PID/PGID), sondée une fois — le réglage d'unité ne change pas
        #: sans redémarrage de l'exécuteur
        self._turn_cgroup_root: str | None = None
        self._turn_cgroup_probed = False
        #: L498 : tours de CE processus dont le cgroup est ouvert (posé au
        #: lancement, retiré en fin de tour) — la réconciliation ne tue
        #: jamais un cgroup encore vivant
        self._turn_cgroups_live: set[str] = set()
        self._turn_cgroups_lock = threading.Lock()
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
        #: #3070 : dernière levée des portes d'appareil (monotone), son erreur
        self._device_gates_at: float | None = None
        self._device_gates_error: str | None = None
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
        #: H5 : la fabrique de handoff épinglée est liée AVANT tout appel à
        #: `readiness`/`advance_host` ; son absence reste un préalable nommé.
        self._lifecycle_factory = None
        self._lifecycle_adapter = None
        self._lifecycle_requirements = None
        self._lifecycle_gap = None
        self._lifecycle_db = None
        if not self.mediated:
            self._bind_lifecycle()

    def _bind_lifecycle(self) -> None:
        """H5: bind the pinned handoff factory on a dedicated connection.

        The runner, not an agent worker, owns this binding: the factory's
        companion reads durable state on the controller's own scoped
        connection, never on an agent's. Both ``readiness`` and
        ``advance_host`` therefore consume the same adapter and
        ``requirements_for``. A refused source or enrollment keeps the binding
        empty; it is recorded by the named prerequisites, never simulated.
        """
        from . import handoff_factory
        from .scoped_db import ScopedDb
        self._lifecycle_db = ScopedDb(self.cfg)
        try:
            factory = handoff_factory.load(self._lifecycle_db)
        except handoff_factory.FactoryUnavailable as exc:
            if getattr(self, "_lifecycle_gap", None) != exc.gap:
                log_async("host lifecycle: " + exc.gap)
            self._lifecycle_gap = exc.gap
            self._lifecycle_db = None
            return
        self._lifecycle_gap = None
        self._lifecycle_factory = factory
        self._lifecycle_adapter = factory.adapter
        self._lifecycle_requirements = factory.requirements_for

    def sandbox_credentials_for(self, agent, owner, epoch, turn_id, deadline_ts):
        """Issue only admitted sandbox credentials; no ordinary lease bearer."""
        from . import relay, runner_sandbox, sandbox_relay
        from .storage.postgres.sandbox import Refusal
        base = relay.relay_url({
            "AMEESH_RELAY_URL": os.environ.get("AMEESH_RELAY_URL", ""),
            "AMEESH_EXEC_URL": self.cfg.exec_url or os.environ.get("AMEESH_EXEC_URL", ""),
        })
        if not base or self.mediated or owner != self.runner_id:
            raise Refusal('profile-refused')
        token, identity = sandbox_relay.issue(
            self.db, agent=agent, owner=owner, epoch=epoch, turn_id=turn_id,
            host=self.host)
        return runner_sandbox.SandboxCredentials(base, token, identity)

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
        self._host_pinned_admissions = None
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

    def election_brief_for(self, agent: dict) -> str | None:
        """L165 (EP3) : briefing de reprise d'une relève CROISÉE, ou None.

        None : ce n'est pas une relève (agent `hote`, ou ligne déjà sur cet
        hôte — une reprise de session locale est normale). "" : une relève est
        due mais le brief n'a pas pu être construit ; l'appelant ne réclame
        pas — jamais de tour sans briefing. Construit APRÈS la porte de rang
        d'EP2, juste avant le CAS : le brief décrit l'état au plus près.
        """
        if agent.get("releve") != "election":
            return None
        previous = agent.get("previous_host")
        if not previous or previous == self.host:
            return None
        from . import reprise
        try:
            brief = reprise.election_brief(
                self.cfg, self.db, agent,
                previous_host=previous,
                previous_owner=agent.get("lease_owner"),
                previous_epoch=agent.get("lease_epoch"),
                previous_session=agent.get("session_id"))
        except Exception as exc:
            log_async("election brief for %s unavailable (%s): no claim this pass"
                      % (agent.get("name"), db_mod.brief(exc)))
            return ""
        if not brief.strip():
            log_async("election brief for %s is empty: no claim this pass"
                      % agent.get("name"))
            return ""
        return brief

    def _note_gate_wait(self, reason: str) -> None:
        if getattr(self, "mediated", False):
            return
        if _admits_nobody(self):
            return
        for agent in registry.claimable(self.db, self.host, self.agents_filter):
            self._note_unleased(agent, reason)

    # -- rôles élus (L165, EP2) -------------------------------------------
    #: fréquence de publication des scores de candidats de cet hôte
    ROLE_EVAL_INTERVAL_S = 60.0
    #: l'hystérésis lit une heure ; l'historique garde deux jours
    ROLE_PRUNE_EVERY_S = 6 * 3600.0

    @migrations.writer_tick
    def role_evaluation_once(self) -> int | None:
        """Publie le score de cet hôte pour chaque rôle élu dont il est candidat.

        Une ligne par rôle et par minute (`role_standings`), lue par les autres
        hôtes et par `ameesh role status`. Seuls les rôles que CET exécuteur
        peut réclamer sont publiés (`agents_filter`) : un relevé prêt qu'il ne
        réclamera jamais ferait basculer le rôle vers lui pour rien (#3099).
        Silencieux en exécuteur médié (les comptes et le budget sont sur le
        serveur) et en `--dry-run` (aucune écriture). Seule la partie LOCALE
        est publiée (harnais, compte, budget) : la porte, le canon, le
        placement et la fraîcheur sont recalculés par le lecteur depuis la
        base.
        """
        if self.dry_run or getattr(self, "mediated", False):
            return None
        now = self.clock.monotonic()
        if self._role_eval_next and now < self._role_eval_next:
            return None
        self._role_eval_next = now + self.ROLE_EVAL_INTERVAL_S
        from . import election, role_handover_pg as handover_pg
        from . import role_standings_pg as pg
        try:
            published = 0 if _admits_nobody(self) else election.publish_self(
                self.cfg, self.db, self.host, names=self.agents_filter,
                pressure=self.host_pressure())
            self._role_error = None
        except Exception as exc:  # un score de rôle ne fait jamais tomber l'exécuteur
            message = "role standings not published (%s)" % db_mod.brief(exc)
            if message != self._role_error:
                log_async(message)
            self._role_error = message
            return None
        if published and now >= self._role_prune_next:
            self._role_prune_next = now + self.ROLE_PRUNE_EVERY_S
            try:
                pg.prune(self.db)
            except db_mod.DbError as exc:
                log_async("role standings not pruned (%s)" % db_mod.explain(exc))
            try:
                handover_pg.prune(self.db)
            except db_mod.DbError as exc:
                log_async("role handovers not pruned (%s)" % db_mod.explain(exc))
        return published

    def _load_handover_state(self) -> None:
        """Relit les marques de bascule de cet hôte, une fois par processus.

        `role_handover` porte la marque écrite avant de rendre le bail : le
        détenteur repris ne réclame pas avant la marge, même après un
        redémarrage. Une marque échue est convertie en refroidissement du
        challenger (`released_until + HYSTERESIS_S`), comme en marche. Une
        base injoignable ou une table absente ne retient jamais un tour :
        l'état mémoire reste la référence et la trace n'est écrite qu'une fois.
        """
        if self._handover_loaded:
            return
        now = self.clock.monotonic()
        if now < self._handover_retry_at:
            return
        self._handover_retry_at = now + 60.0
        from . import election, role_handover_pg as pg
        try:
            rows = pg.load(self.db, self.host)
        except Exception as exc:  # état de confort, jamais une garde de tour
            message = "role handovers not loaded (%s)" % db_mod.brief(exc)
            if message != self._handover_error:
                log_async(message)
            self._handover_error = message
            return
        self._handover_loaded = True
        now_mono = self.clock.monotonic()
        for row in rows:
            name = row["name"]
            challenger = row["challenger"]
            now_ts = float(row["now_ts"])
            released = float(row["released_ts"])
            if released > now_ts:
                self._handover_released[name] = (
                    now_mono + (released - now_ts), challenger)
                continue
            until = released + election.HYSTERESIS_S
            if until > now_ts:
                self._handover_failed.setdefault(name, {})[challenger] = (
                    now_mono + (until - now_ts))

    def _clear_restart_reservation(self, name: str) -> None:
        """#4086 : le rôle est repris ; toute réservation de redémarrage est
        consommée (sinon elle retiendrait encore le challenger d'une bascule
        planifiée qui suivrait). Échec : elle expire d'elle-même."""
        from . import role_handover_pg as pg
        try:
            pg.clear_restart(self.db, name)
        except Exception as exc:  # noqa: BLE001 - la réservation est bornée
            log_async("[%s] restart reservation not cleared (%s)"
                      % (name, db_mod.brief(exc)))

    def note_handover_release(self, name: str, challenger: str,
                              delay_s: float = 0.0) -> None:
        """L165 (EP2) : ce détenteur vient de rendre le rôle pour une bascule.

        Le challenger est le mieux classé : il tente le CAS tout de suite. Si
        son exécuteur est mort, l'ancien détenteur ne reprend qu'après
        `délai de rang du challenger + HANDOVER_RECLAIM_MARGIN_S`. La marque
        est écrite dans `role_handover` pour survivre au redémarrage (#3099) ;
        une écriture impossible ne change pas la décision en cours.
        """
        from . import election, role_handover_pg as pg
        self._load_handover_state()
        margin_s = max(0.0, float(delay_s)) + election.HANDOVER_RECLAIM_MARGIN_S
        self._handover_released[name] = (
            self.clock.monotonic() + margin_s, challenger)
        try:
            pg.release(self.db, self.host, name, challenger, margin_s)
        except Exception as exc:  # la marque mémoire reste la référence
            message = "role handover not recorded (%s)" % db_mod.brief(exc)
            if message != self._handover_error:
                log_async(message)
            self._handover_error = message

    def handover_wait_s(self, name: str, now: float | None = None) -> float:
        """Temps restant avant que ce détenteur puisse reprendre un rôle rendu.

        À l'échéance de la marge, le challenger est enregistré comme ayant
        échoué : le détenteur peut reprendre, mais ne redonne plus le rôle à ce
        challenger pendant `HYSTERESIS_S` (`handover_failed`, lu par
        `_election_handover`). Le refroidissement part de l'échéance de la
        marge, pas de l'instant où on la constate : une marque relue après un
        redémarrage donne la même fenêtre.
        """
        self._load_handover_state()
        now = self.clock.monotonic() if now is None else float(now)
        mark = self._handover_released.get(name)
        if mark is None:
            return 0.0
        deadline, challenger = mark
        if now >= deadline:
            from . import election
            until = deadline + election.HYSTERESIS_S
            if until > now:
                self._handover_failed.setdefault(name, {})[challenger] = until
            del self._handover_released[name]
            return 0.0
        return deadline - now

    def handover_failed(self, name: str, now: float | None = None) -> tuple[str, ...]:
        """Challengers écartés des bascules de ce rôle (cooldown non expiré).

        Un challenger qui publie un relevé prêt mais ne réclame jamais (filtre
        d'agents de son exécuteur, porte d'hôte, refus du CAS non couvert par
        la préparation) ne doit pas faire tourner le rôle en boucle : le
        détenteur repris l'ignore pendant `HYSTERESIS_S`, puis la bascule
        redevient possible s'il est toujours ≥ 5 points devant. L'état est
        relu de la base au premier usage, donc un redémarrage le conserve.
        """
        self._load_handover_state()
        now = self.clock.monotonic() if now is None else float(now)
        current = self._handover_failed.get(name)
        if not current:
            return ()
        for host, until in list(current.items()):
            if until <= now:
                del current[host]
        if not current:
            self._handover_failed.pop(name, None)
            return ()
        return tuple(sorted(current))

    def restart_pending(self) -> bool:
        """#4086 : cet exécuteur s'arrête (signal, drainage, code périmé,
        retrait médié) : un rôle élu qu'il rend reste réservé à l'hôte.
        Jamais en passage unique ni médié (pas d'élection côté appareil)."""
        if self.once or self.dry_run or getattr(self, "mediated", False):
            return False
        return (self.stop.is_set() or self.draining.is_set()
                or bool(getattr(self, "restart_for_stale", False))
                or bool(getattr(self, "sigterm_requested", False)))

    def election_claim_allowed(self, agent: dict) -> tuple[bool, str, dict | None]:
        """L165 (EP2) : ce rôle élu peut-il être tenté maintenant, à son rang ?

        `free_since` est l'instant monotone où CETTE passe a vu le bail libre
        pour la première fois ; le délai vaut `rang × 20 s`. Un hôte sans
        relevé frais, non prêt, ou illisible ne tente pas : le CAS refuserait
        de toute façon, mais il ne doit pas prendre la place d'un candidat prêt.
        """
        if self.dry_run or getattr(self, "mediated", False):
            return True, "", None
        from . import election
        # B1 de la relecture #3099 : un exécuteur neuf a la carte vide ; le
        # premier claim doit relire la base avant de la consulter, sinon la
        # marque écrite avant de rendre le bail est ignorée au redémarrage.
        self._load_handover_state()
        name = agent["name"]
        now = self.clock.monotonic()
        released = self._handover_released.get(name)
        if released is not None:
            left = self.handover_wait_s(name, now)
            if left > 0:
                return (False,
                        "handover: waits %ds for %s" % (int(left) + 1, released[1]),
                        None)
        since = self._election_free_since.setdefault(name, now)
        try:
            verdict = election.claim_wait(self.db, agent, self.host, since, now=now)
            if (not verdict[0] and verdict[1].startswith("not ready")
                    and self._republish_reserved(name, now)):
                verdict = election.claim_wait(self.db, agent, self.host, since,
                                              now=now)
            return verdict
        except Exception as exc:  # idem : fail closed, jamais un tour sans rang
            message = "role standings unreadable (%s)" % db_mod.brief(exc)
            if message != self._role_error:
                log_async(message)
            self._role_error = message
            return False, "not ready: " + message, None

    #: #4112 : au plus une republication forcée par rôle réservé et par 5 s
    RESERVED_REPUBLISH_S = 5.0

    def _republish_reserved(self, name: str, now: float) -> bool:
        """#4112 : republie tout de suite le relevé de cet hôte pour un rôle
        qui lui est réservé (`@restart`, #4086) mais lu « non prêt ».

        Le relevé lu est souvent celui d'avant le redémarrage : pause du drain
        de `vm.sh` encore posée, ou publié par l'ancien processus il y a moins
        d'une minute (`publish_self` ne republie pas avant `interval_s`).
        Attendre le relevé suivant (60 s) mangeait la réservation (90 s) et
        le rang 0 d'un autre hôte reprenait le rôle. La readiness reste la
        garde : on ne fait que la relire à jour, bornée à `RESERVED_REPUBLISH_S`.
        Rend vrai quand un relevé neuf a été écrit."""
        # doublure de test partielle (sans l'état de l'exécuteur) : rien
        pending = getattr(self, "_reserved_republish_next", None)
        if pending is None or now < pending.get(name, 0.0):
            return False
        pending[name] = now + self.RESERVED_REPUBLISH_S
        from . import election, role_handover_pg as handover_pg
        reserved = handover_pg.restart_reservation(self.db, name)
        if reserved is None or reserved["host"] != self.host:
            return False
        return bool(election.publish_self(self.cfg, self.db, self.host,
                                          force=True, names=[name],
                                          pressure=self.host_pressure()))

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
    @migrations.writer_tick
    def expire_operator_pauses_once(self) -> list[dict]:
        """#3961 (issue #3014) : les pauses opérateur échues, à chaque passe.

        Comme les délégations : tout exécuteur natif vivant lève toutes les
        échéances (la pause d'un agent sans bail n'a pas de worker pour la
        relire) ; deux exécuteurs ne lèvent jamais deux fois (verrou de ligne,
        SKIP LOCKED). La levée a son événement d'audit (`operator_pause_events`)
        et une note dans le fil du projet de l'agent ; le déclencheur de 0441
        réveille l'exécuteur qui le tient. Une erreur de base est journalisée
        une fois, jamais fatale à la passe."""
        from . import agent_pause

        if getattr(self, "mediated", False) or self.dry_run:
            return []
        try:
            done = agent_pause.expire(self.db)
        except db_mod.DbError as exc:
            message = "expired operator pauses not processed (%s)" % exc
            if message != getattr(self, "_operator_pause_error", None):
                log_async(message)
            self._operator_pause_error = message
            return []
        self._operator_pause_error = None
        for row in done:
            text = ("Operator pause lifted at its deadline (%s, set by %s): %s"
                    % (agent_pause.until_label(row["until_ts"]), row.get("by_human") or "?",
                       row.get("reason") or "?"))
            log_async("[%s] %s" % (row["agent"], text))
            try:
                agent = registry.get(self.db, row["agent"]) or {}
                fil.record(self.cfg, self.db, sender=row["agent"], recipients=[], text=text,
                           ts=time.time(),
                           project=fil.project_for(self.cfg, agent.get("chantier") or ""),
                           meta={"action": "operator_pause_expired", "audit": "runner",
                                 "event_id": row.get("event_id")})
            except Exception as exc:  # une trace ne casse jamais la passe
                log_async("[%s] thread unavailable: %s" % (row["agent"], exc))
        return done

    @migrations.writer_tick
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

    @migrations.writer_tick
    def lift_device_gates_once(self, now: float | None = None) -> list[dict]:
        """#3070 (canon §12) : les lots `blocked` dont la seule attente est une
        séance sur un appareil déclaré indisponible en sortent, avec la trace
        « qualification physique reportée » et un lot de vérification.

        Au plus toutes les `DEVICE_GATES_EVERY_S` ; ni en dry-run (dit
        seulement), ni en exécuteur médié. Deux exécuteurs simultanés ne lèvent
        jamais deux fois un lot (transition gardée par l'état courant, lot de
        vérification annulé avec elle). Une erreur est dite une fois, jamais
        fatale à la passe."""
        from . import device_gates, work as work_mod

        now = time.monotonic() if now is None else now
        last = getattr(self, "_device_gates_at", None)
        if getattr(self, "mediated", False) or getattr(self, "db", None) is None or (
                last is not None and now - last < DEVICE_GATES_EVERY_S):
            return []
        self._device_gates_at = now
        try:
            done = device_gates.lift(
                self.db, canons=work_mod.configured_canons(getattr(self, "cfg", None)),
                actor="%s (runner %s)" % (device_gates.ACTOR, getattr(self, "runner_id", "?")),
                dry_run=getattr(self, "dry_run", False))
        except (db_mod.DbError, work_mod.WorkError) as exc:
            message = "device gates not lifted (%s)" % exc
            if message != getattr(self, "_device_gates_error", None):
                log_async(message)
            self._device_gates_error = message
            return []
        self._device_gates_error = None
        for row in done:
            if row.get("dry_run"):
                log_async("device gate: lot #%s would go back to %s [dry-run]"
                          % (row["lot"], row["state"]))
            else:
                log_async("device gate lifted: lot #%s → %s (%s, verification lot #%s)"
                          % (row["lot"], row["state"], device_gates.TRACE, row["verify"]))
        return done

    @migrations.writer_tick
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
    def report_liveness(self) -> None:
        """#508 : une passe vivante, même sans bail ni admission ; au plus
        une requête par intervalle, indépendamment des réveils de courrier."""
        # Un runner en drain reste vivant et continue cette preuve jusqu'à l'arrêt.
        if self.stop.is_set() or self.dry_run:
            return
        now = time.monotonic()
        if now < getattr(self, "_runner_seen_next", 0.0):
            return
        # Borne aussi les échecs : une panne ne multiplie pas les écritures.
        self._runner_seen_next = now + max(
            60.0, self.poll, self.cfg.runner_liveness_interval)
        registry.runner_seen(self.db, self.host, self.runner_id, self.agents_filter)

    def take_pass(self) -> list:
        """#804 : l'exécuteur déclenche la prise pour ses propres agents.

        `backlog.auto_take` n'est plus appelée seulement par `ameesh notify` :
        ici, sur l'hôte où ces agents tournent, avec les mêmes garde-fous
        (repos réel, budget, hôtes en pause, `balance_low`, plafonds horaires
        comptés en base). Seuls les agents servis par cet exécuteur — leurs
        workers vivants et encore admis — sont preneurs ; la liste du mesh
        reste entière pour les gardes et l'avertissement des orchestrateurs.
        Un hôte sous pression est écarté comme par `ameesh notify` (alerte
        `host_pressure` locale).

        #3972 : un worker retiré de l'admission reste vivant jusqu'à la fin
        de son tour ; il ne prend plus de lot entre-temps.
        """
        if self.once or self.mediated or not self.take:
            return []
        with self.lock:
            names = {name for name, worker in self.workers.items() if worker.is_alive()}
            flt = getattr(self, "agents_filter", None)
        if flt is not None:
            names &= set(flt)
        if not names:
            return []
        alerts = []
        try:
            if self.host_pressure().get("blocked"):
                alerts.append({"type": "host_pressure", "host": self.host})
        except Exception:
            pass  # relevé illisible : on ne devine pas la pression
        try:
            alerts += local_balance_alerts(self.db, self.take_balance, time.time())
        except Exception as exc:
            # Solde illisible : on ne devine pas. Un agent au token pourrait
            # être sous le seuil, on le retient ; un forfait, lui, ne dépense
            # pas ce solde et reste preneur.
            alerts.append({"type": "balance_low", "unreadable": True})
            log_async("auto-take: balance unreadable (%s); token agents held"
                      % type(exc).__name__)
        taken = local_take(self.db, self.cfg, names, self.take,
                           dry_run=self.dry_run, alerts=alerts)
        for take in taken:
            log_async("auto-take: %s takes lot #%s (%s)" % (
                take.get("agent"), (take.get("item") or {}).get("id"), take.get("kind")))
        return taken

    @migrations.writer_tick
    def release_stopped_lots_once(self) -> list[dict]:
        """#753 : un agent arrêté ne garde pas ses lots. À chaque passe qui
        atteint la reprise — après les gardes secteur/drainage/code périmé
        (#1425, N2) —, les lots ouverts d'un agent `stopped` retournent à la
        file d'équipe, et ses lots promus dont la mesure reste à refaire vont à
        l'auditeur.

        Seuls les arrêts DÉLIBÉRÉS (`stop_reason` dans
        `work.RELEASABLE_STOP_REASONS`) dessaisissent : un arrêt automatique et
        réversible (`erreur` de `stop_after_failures`, `retire_du_canon` que
        `canon.revive` annule, bail expiré) laisse l'agent reprendre ses lots —
        sinon une panne fournisseur viderait en rafale tous ses lots (R2
        Claude, B1). La transaction de `release_agent` revérifie elle-même
        l'état du registre (`stopped_with`, N3). Ne dépend d'aucun agent : tout
        exécuteur vivant le fait ; idempotent. Une erreur de base n'est pas
        fatale à la passe ; `--dry-run` dit ce qui serait fait sans écrire."""
        from . import work as work_mod

        if getattr(self, "mediated", False):
            return []  # L109 : le registre est tenu par le serveur
        # #3907 : le balayage tourne à chaque passe de chaque exécuteur, et une
        # passe suit chaque NOTIFY du mesh. `overview()` y coûtait 0,4 à 1,2 s
        # et saturait la base commune (27/10, charge 44 sur 4 vCPU) : le filtre
        # ne lit plus que `status` et `stop_reason` du registre. La liste
        # complète n'est lue (par `measurement_actor`) que s'il y a un lot à
        # rendre.
        try:
            names = storage.of(self.db).agents.stopped_with(
                work_mod.RELEASABLE_STOP_REASONS)
        except db_mod.DbError as exc:
            message = "stopped agents not swept (%s)" % exc
            if message != getattr(self, "_stopped_lots_error", None):
                log_async(message)
            self._stopped_lots_error = message
            return []
        self._stopped_lots_error = None
        # #1425 (N5) : ne payer la résolution de l'auditeur (lecture des canons)
        # que s'il y a un arrêt délibéré à dessaisir ; le balayage tourne à
        # chaque passe.
        stopped = [{"name": name} for name in names]
        if not stopped:
            return []
        # #2877 : ce que ces agents tiennent encore, en UNE lecture sans verrou.
        # `release_lots` verrouille la ligne du registre de l'agent le temps de
        # ses allers-retours ; refaite à chaque passe par chaque exécuteur pour
        # un agent qui n'a plus rien à rendre, elle formait un convoi (une
        # trentaine de sessions en file sur la même ligne, 20/10). Un lot
        # assigné après ce relevé est vu à la passe suivante ; la transaction
        # revérifie toujours l'agent et chaque lot.
        try:
            held = work_mod.held_lots(self.db, [a["name"] for a in stopped])
        except db_mod.DbError as exc:
            message = "stopped agents not swept (%s)" % exc
            if message != getattr(self, "_stopped_lots_error", None):
                log_async(message)
            self._stopped_lots_error = message
            return []
        stopped = [a for a in stopped if a["name"] in held]
        if not stopped:
            return []
        cache = getattr(self, "_measure_canons_cache", None)
        now = time.time()
        if cache is None or cache[0] <= now:
            cache = (now + MEASURE_CANONS_TTL_S,
                     work_mod.configured_canons(getattr(self, "cfg", None)))
            self._measure_canons_cache = cache
        measure_actor = work_mod.measurement_actor(
            self.db, self.cfg, canons=cache[1])
        measure_reason = ("promoted lot whose measurement is still to be redone: "
                          "handed to %s (#753)" % measure_actor) if measure_actor else ""
        out: list[dict] = []
        # B1 (R2 Claude) : un arrêt automatique et réversible — `erreur` de
        # `stop_after_failures`, `retire_du_canon` que `canon.revive` annule,
        # bail expiré — ne dessaisit pas : l'agent relancé retrouve ses lots.
        # Seul un arrêt délibéré (manuel) les rend.
        for agent in stopped:
            if not held[agent["name"]]["open"] and (
                    not measure_actor or measure_actor == agent["name"]):
                continue  # #2877 : des lots promus seulement, et personne pour les mesurer
            try:
                done = work_mod.release_lots(
                    self.db, agent["name"],
                    reason="agent stopped: returned to the team queue (#753)",
                    actor="runner %s" % self.runner_id,
                    measure_actor=measure_actor, measure_reason=measure_reason,
                    dry_run=self.dry_run,
                    stopped_with=work_mod.RELEASABLE_STOP_REASONS)
            except db_mod.DbError as exc:
                message = "stopped lots of %s not released (%s)" % (agent["name"], exc)
                if message != getattr(self, "_stopped_lots_error", None):
                    log_async(message)
                self._stopped_lots_error = message
                continue
            for row in done:
                to = row.get("assigned_to")
                if to:
                    log_async("lot #%s handed to %s (%s stopped, measurement to redo)%s"
                              % (row["id"], to, agent["name"],
                                 " [dry-run]" if row.get("dry_run") else ""))
                else:
                    log_async("lot #%s returned to the team queue (%s stopped)%s"
                              % (row["id"], agent["name"],
                                 " [dry-run]" if row.get("dry_run") else ""))
            out.extend(done)
        return out

    @migrations.writer_tick
    def sweep(self) -> None:
        self.report_liveness()
        self.role_evaluation_once()  # L165 (EP2) : score des candidats de l'hôte
        self.expire_delegations_once()
        self.expire_operator_pauses_once()  # #3961 : avant toute garde de passe
        self.lift_device_gates_once()
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
            if not getattr(self, "_gate_closed_woken", False):
                # #4515 : le sondage au repos s'est allongé (`idle_poll`) ; à la
                # fermeture, chaque worker au repos rend son bail sans attendre
                self._gate_closed_woken = True
                self._wake_workers()
            return  # host gate closed: registry diagnosis without taking a lease
        self._gate_closed_woken = False
        # #1425 (N2) : le dessaisissement des arrêts délibérés vient APRÈS les
        # gardes (secteur, drainage, code périmé, porte) : sous l'une d'elles
        # l'exécuteur n'agit pas sur la file, et un runner périmé ne dessaisit
        # pas avec une logique ancienne. Il précède la reprise des agents : la
        # file est à jour avant les nouvelles prises.
        self.release_stopped_lots_once()
        # #804 : la prise au repos est déclenchée ici, par l'exécuteur de
        # l'hôte, et non par un `ameesh notify` central (mêmes garde-fous).
        # Avant la réclamation : le lot pris et son courrier partent dans la
        # même passe ; une panne de prise ne fait pas tomber l'exécuteur.
        if self.take_interval > 0 and time.monotonic() >= self._take_next:
            self._take_next = time.monotonic() + self.take_interval
            try:
                self.take_pass()
            except Exception as exc:
                log_async("auto-take pass failed (%s): %s; runner continues"
                          % (type(exc).__name__, db_mod.brief(exc)))
        # #3925 : une liste d'admission vide n'admet personne (`claimable`
        # lit une liste vide comme « tous »)
        candidates = ([] if _admits_nobody(self)
                      else registry.claimable(self.db, self.host, self.agents_filter))
        for agent in candidates:
            with self.lock:
                if not self.once and agent["name"] in self.workers:
                    continue
            # L165 (EP2) : un rôle élu attend son rang avant de tenter le CAS ;
            # le bail (EP1) reste le seul arbitre, ce délai n'est que la
            # politesse qui laisse le meilleur candidat passer d'abord.
            if agent.get("releve") == "election":
                allowed, reason, _standing = self.election_claim_allowed(agent)
                if not allowed:
                    if reason.startswith("not ready"):
                        self._note_unleased(agent, "election: " + reason)
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
                # L165 (EP3) : le brief de relève est construit APRÈS la porte
                # de rang d'EP2, juste avant le CAS. Un brief impossible ne
                # réclame pas : jamais de tour sans briefing.
                election_brief = self.election_brief_for(agent)
                if election_brief == "":
                    self._release_slot(agent["name"])
                    continue
                lease = registry.claim(self.db, agent["name"], self.runner_id,
                                       self.lease_ttl, host=self.host,
                                       election_brief=election_brief or None)
                if not lease:
                    self._release_slot(agent["name"])
                    continue  # un autre exécuteur a gagné la course
                self._election_free_since.pop(agent["name"], None)
                if agent.get("releve") == "election":
                    self._clear_restart_reservation(agent["name"])
                # L165 (EP1) : un rôle élu peut être réclamé depuis cet hôte
                # alors que sa ligne était chez un autre candidat ; le worker
                # doit lire l'hôte et le dossier effectivement acquis.
                agent = dict(agent, host=lease.get("host") or self.host,
                             cwd=lease.get("cwd", agent.get("cwd")))
                # L165 (EP3) : relève croisée, ou brief d'une relève resté
                # d'un exécuteur mort avant son premier tour. La session de
                # l'ancien hôte n'est jamais reprise : le worker reçoit une
                # ligne remise à zéro et le brief comme résumé de la neuve.
                resumed_brief = lease.get("election_brief") or ""
                elected = bool(lease.get("elected"))
                # Un brief resté d'un exécuteur mort ne s'applique que si
                # aucune session n'est ouverte : une session ouverte a déjà
                # consommé le sien (et `set_session` l'a effacé).
                resume_election = elected or bool(resumed_brief
                                                  and not agent.get("session_id"))
                if resume_election:
                    agent.update(session_id=None, session_account=None,
                                 session_harness=None, session_cwd=None,
                                 session_origin=None,
                                 session_reset_ts=lease.get("session_reset_ts"),
                                 lease_owner=self.runner_id,
                                 lease_epoch=lease["lease_epoch"],
                                 status="queued")
                elif resumed_brief:
                    log_async("[%s] election brief left aside: a session is already open"
                              % agent["name"])
                log("lease acquired: %s (epoch %s, host %s)"
                    % (agent["name"], lease["lease_epoch"], self.host))
                worker = AgentWorker(self, agent, lease)
                if resume_election:
                    worker.adopt_election(lease, resumed_brief or election_brief)
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

        # L165 (EP2) : un rôle qui n'est plus libre (bail repris ailleurs)
        # repart de zéro au prochain bail libre : le délai par rang se compte
        # depuis la première observation de CETTE liberté.
        free = {agent["name"] for agent in candidates
                if agent.get("releve") == "election"
                and agent["name"] in self._election_free_since}
        for name in list(self._election_free_since):
            if name not in free:
                del self._election_free_since[name]
        # `_handover_released` n'est PAS purgé ici : le worker pose la marque
        # juste avant de rendre le bail (`finally`), donc un balayage
        # concurrent verrait encore le bail vivant, conclurait « pas libre »
        # et effacerait la marge de reprise. La marque s'efface d'elle-même
        # à l'échéance dans `handover_wait_s` ; une entrée périmée est inerte.

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

        # #2634 : la borne est prise sur l'horloge du worker (#2600) : elle
        # est relue sur la même, jamais sur `time.monotonic()` directement
        clock = _clock_of(worker)
        log_async("[%s] lease not returned (base): counted as held until it is, "
                  "or until it expires (%ds)"
                  % (worker.name, max(0, worker.lease_held_until - clock.monotonic())))
        while renewing() or clock.monotonic() < worker.lease_held_until:
            gate.tick()
            left = worker.lease_held_until - clock.monotonic()
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

    # -- relecture de la configuration (#1466) ------------------------------
    def config_stamp(self) -> tuple:
        """Empreinte des fichiers lus (chemin, mtime, taille) : on ne relit
        que sur changement."""
        out = []
        for path in (self.cfg.config_path, self.cfg.config_base):
            if not path:
                continue
            try:
                st = os.stat(path)
                out.append((path, st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((path, None, None))
        return tuple(out)

    @property
    def admits_nobody(self) -> bool:
        return _admits_nobody(self)

    def _admission_failed(self, path: str, why: str) -> None:
        """Panne passagère de la source d'admission : dernière liste gardée,
        empreinte non retenue (nouvel essai à la passe suivante), journal une
        fois par cause."""
        self._admission_stamp = ()
        if getattr(self, "_admission_error", None) != why:
            self._admission_error = why
            log_async("admission file %s %s: last list kept (%d agent(s)), retried "
                      "on the next pass" % (path, why, len(self.agents_filter or ())))

    def refresh_admission(self) -> bool:
        """Relit la source d'admission côté hôte si elle a changé (#3925).

        Un agent ajouté au fichier est réclamable à la passe suivante, sans
        redémarrage ; un agent retiré n'est plus réclamé, et son worker rend
        le bail à la fin de son tour (`AgentWorker.admission_withdrawn`). Une
        entrée invalide est écartée avec sa raison ; un fichier absent ou
        illisible garde la dernière liste lue (vide au démarrage). Une lecture
        refusée ou une résolution impossible (base injoignable) est une panne
        passagère, pas une entrée invalide : la dernière liste reste en
        vigueur et l'empreinte n'est pas retenue, la passe suivante réessaie
        sans édition du fichier (R1 cx2 sur #3925). Le fichier
        ne fait que restreindre : canon, placement, `max_agents`, porte et
        comptes s'appliquent toujours à la réclamation. Vrai si la liste a
        changé."""
        path = getattr(self, "agents_file", None)
        if not path:
            return False
        from . import admission
        stamp = admission.stamp(path)
        if stamp == self._admission_stamp:
            return False
        if stamp is None:
            self._admission_stamp = stamp  # sa création changera l'empreinte
            log_async("admission file %s missing: %d agent(s) of the last read kept"
                      % (path, len(self._admission_file_names)))
            return False
        try:
            parsed = admission.read(path)
        except (OSError, UnicodeDecodeError) as exc:
            self._admission_failed(path, "unreadable (%s)" % type(exc).__name__)
            return False
        from .agent_names import resolve
        names: list[str] = []
        for name in parsed.names:
            try:
                names.append(resolve(name, db=self.db, operation="runner.admission"))
            except Exception as exc:  # une entrée ne fait jamais tomber l'exécuteur
                self._admission_failed(path, "%r not resolved (%s)"
                                       % (name, type(exc).__name__))
                return False
        self._admission_stamp = stamp
        self._admission_error = None
        for number, entry, reason in parsed.problems:
            log_async("admission file %s%s: %r skipped, %s"
                      % (path, ":%d" % number if number else "", entry, reason))
        self._admission_file_names = names
        new = list(dict.fromkeys(self._agents_static + names))
        old = list(self.agents_filter or ())
        if new == old:
            return False
        added = [n for n in new if n not in old]
        removed = [n for n in old if n not in new]
        self.agents_filter[:] = new  # en place : canal de contrôle, workers
        control = getattr(self, "_control_channel", None)
        if control is not None:
            control.set_allowed(self.agents_filter)
        log_async("admission file %s: %d agent(s)%s%s"
                  % (path, len(new),
                     "; admitted " + ", ".join(added) if added else "",
                     "; withdrawn after their current turn " + ", ".join(removed)
                     if removed else ""))
        lock = getattr(self, "lock", None)
        if removed and lock is not None:
            with lock:
                leaving = [self.workers[n] for n in removed if n in self.workers]
            for worker in leaving:
                worker.wake.set()  # un worker au repos rend son bail sans attendre
        return True

    def reload_config(self, *, force: bool = False) -> bool:
        """Relit la configuration si un fichier lu a changé (#1466).

        Un exécuteur gardait la configuration de son démarrage : un compte
        retiré, un plafond relevé ou un profil corrigé restaient sans effet
        jusqu'au redémarrage, et des agents restaient en pause sur un plafond
        périmé. Seuls les champs de `RELOADABLE_FIELDS` — ceux qui décident une
        pause budget et la lecture des comptes (dont leur partage, #2571) —
        suivent le fichier. Tout le reste (identité de l'hôte,
        bail, dsn, schéma, dossier d'état, cadences, porte d'hôte) a décidé des
        baux et des connexions en cours : le changer sous un tour actif
        romprait ce qui tourne, il reste figé jusqu'au redémarrage (R1 cx2 et
        dev2 sur #1466 : schéma et `state_dir` appliqués aux workers sans
        reconnexion, `refresh_host_limits` relisant `cfg.lease_ttl`).

        Un fichier cassé ou disparu ne remplace rien : la dernière
        configuration valide reste en vigueur, et le refus est journalisé une
        fois par empreinte."""
        if not self.cfg.config_path:
            return False
        stamp = self.config_stamp()
        if not force and stamp == self._config_stamp:
            return False
        self._config_stamp = stamp
        # un fichier disparu ne remplace rien : sans AMEESH_CONFIG, `load`
        # rendrait sinon les valeurs par défaut (aucun compte)
        disparus = [path for path, mtime, _size in stamp if mtime is None]
        if disparus:
            log_async("config reload refused: %s missing" % ", ".join(disparus))
            return False
        try:
            lu = load_config()
        except SystemExit as exc:
            log_async("config reload refused: %s" % exc)
            return False
        if lu.config_path != self.cfg.config_path:
            log_async("config reload refused: now reads %s, started on %s"
                      % (lu.config_path or "defaults", self.cfg.config_path))
            return False
        # liste explicite : ce qui n'y est pas garde la valeur du démarrage, y
        # compris un champ nouveau (le défaut sûr est de ne pas le relire) ;
        # l'identité de l'exécuteur, son stockage (base, schéma, serveur médié,
        # dossier d'état) et les options de ligne de commande ont décidé les
        # baux et les tours en cours
        fresh = dataclasses.replace(
            self.cfg, **{nom: getattr(lu, nom) for nom in RELOADABLE_FIELDS})
        if fresh == self.cfg:
            return False
        avant = self.cfg
        self.cfg = fresh
        # le scalaire que `budget_limits` avait recopié au démarrage
        self.budget_usd_per_hour = max(0.0, fresh.budget_usd_per_hour)
        adapters.configure(fresh)  # L106 : `harness_bins` de l'hôte
        with self.lock:
            workers = list(self.workers.values())
        for worker in workers:
            worker.apply_config(fresh)
        note = "config reloaded: %s" % fresh.config_source
        if "accounts" in (fresh.config_shadowed or ()):
            note += (" — WARNING: this profile overrides the host accounts; a "
                     "stale profile diverges silently from other runners")
        log_async(note)
        if fresh.critical_agents != avant.critical_agents:
            log_async("critical agents: %s"
                      % (", ".join(fresh.critical_agents) or "none"))
        # la décision de pause est prise au début d'un tour : réveiller les
        # agents endormis, y compris ceux que la pause tenait à l'écart
        self.wake_all.set()
        for worker in workers:
            worker.wake.set()
        return True

    # -- écoute ------------------------------------------------------------
    #: #4515 : passes de sondage au repos par seconde, pour l'exécuteur
    #: entier. Une passe d'agent au repos coûte une trentaine de requêtes sur
    #: la connexion partagée ; à `poll` = 5 s, vingt agents en faisaient ~120/s
    #: et un runtime de soixante-dix agents saturait la connexion en
    #: permanence (fils en file, GIL disputé, ~1 cœur brûlé sans aucun tour).
    IDLE_PASSES_PER_S = 1.0
    #: #4515 : plafond du pas au repos. La relève élue d'un détenteur non
    #: prêt (#4231) est vue à une passe du worker : 60 s de publication, 60 s
    #: d'observation, 120 s de garde et au plus 45 s de pas restent sous les
    #: cinq minutes du canon (§11) ; au-delà de 45 agents, le budget de passes
    #: recroît donc doucement (70 agents : 1,6 passe/s, contre 14 avant).
    IDLE_POLL_MAX_S = 45.0

    def idle_poll(self) -> float:
        """#4515 : le pas du sondage d'un worker au repos.

        Le sondage n'est qu'un filet : courrier, redémarrage, budget, config
        et drainage réveillent le worker par son événement (NOTIFY, `dispatch`)
        et les échéances connues raccourcissent l'attente (`_until_due`).
        Tant que l'écoute est établie, le pas s'allonge donc avec le nombre
        d'agents pour que l'exécuteur entier fasse au plus
        `IDLE_PASSES_PER_S` passes de repos par seconde, dans la limite de
        `IDLE_POLL_MAX_S`. Le bail reste renouvelé au tiers de sa durée
        (borne de l'appelant). Sans écoute (pilote sans
        LISTEN, reconnexion en cours), le sondage redevient le seul réveil :
        `poll`, comme avant."""
        if getattr(self, "listener", None) is None:
            return self.poll
        with self.lock:
            agents = len(self.workers)
        return max(self.poll, min(self.IDLE_POLL_MAX_S, agents / self.IDLE_PASSES_PER_S))

    def _wake_workers(self) -> None:
        """#4515 : réveille chaque worker (écoute rétablie, porte fermée)."""
        lock = getattr(self, "lock", None)
        if lock is None:
            return  # doublure d'exécuteur sans workers
        with lock:
            for worker in self.workers.values():
                worker.wake.set()

    #: #4602 : au plus une ligne par agent et par fenêtre pour une attente
    #: d'admission base longue (le seuil du verrou est `admission_lock.WARN_S`).
    DB_WAIT_NOTICE_S = 30.0

    def _db_wait_notice(self, name: str, waited: float) -> None:
        """Journalise une attente longue sur la connexion partagée (#4594).

        Le verrou prioritaire appelle ceci hors de sa condition ; on ne écrit
        qu'une ligne par agent et par fenêtre, et jamais depuis un chemin qui
        pourrait bloquer l'admission (file de journal bornée)."""
        now = time.monotonic()
        with self._db_wait_lock:
            last = self._db_wait_seen.get(name, 0.0)
            if now - last < self.DB_WAIT_NOTICE_S:
                return
            self._db_wait_seen[name] = now
        log_async("[%s] deferred: shared database admission (queued %.1fs)"
                  % (name, waited))

    @migrations.writer_tick
    def dispatch(self, item: dict) -> None:
        """Réveille (ou préempte) le worker visé, **signal avant journal**.

        Un `log` synchrone avant `request_preempt`/`wake.set` laissait un pipe
        saturé retarder la préemption (verdict codex3 L11 B1) : tous les signaux
        partent d'abord, la journalisation passe par la file bornée.
        """
        if item.get("channel") == train_events.CHANNEL:
            service = getattr(self, "train_service", None)
            if service is not None:
                train_events.receive(service, item)
            return
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
            if worker is not None:
                # #4594 : le signal part AVANT toute lecture en base. Un
                # `mail.get` sur la connexion partagée peut attendre derrière
                # des dizaines de passes au repos ; le réveil ne doit pas
                # dépendre de cette file. Le contrôle d'urgence vient ensuite,
                # et #2810 couvre la course avec un tour qui a déjà commencé.
                # #4602 : cette passe d'admission du worker passe en tête.
                worker._priority_wake = True
                worker.wake.set()
            if payload.get("restart"):
                # `ameesh restart` (L26) : le NOTIFY n'est qu'un réveil ; la
                # demande est relue en base avant d'arrêter quoi que ce soit.
                if worker is not None:
                    with admission_lock.priority_for(admission_lock.CONTROL):
                        agent = registry.get(self.db, target) or {}
                    if agent.get("restart_requested_ts"):
                        worker.request_restart()  # le signal d'abord
                        log_async("restart requested for %s: stopping the turn" % target)
                self.wake_all.set()
                return
            prioritaire = False
            row = None
            if worker is not None and payload.get("id"):
                with admission_lock.priority_for(admission_lock.CONTROL):
                    row = mail.get(self.db, int(payload["id"]))
                if row and mail.is_urgent(row):
                    prioritaire = worker.interrupt_allowed(row)
                    if not prioritaire:
                        log_async("urgent from %s without interruption: sender not "
                                  "allowed to interrupt" % (row.get("sender") or "?"))
            if worker and prioritaire:
                if row.get("delivered_ts") is not None:
                    # #2810 : le message est déjà remis (hook mi-tour, ou tour
                    # précédent) — il n'y a plus de tour à interrompre pour lui.
                    worker.wake.set()
                    log_async("priority message for %s already delivered: no "
                              "interruption" % target)
                elif worker.request_preempt(row):  # le signal d'abord ; L77 : point sûr
                    log_async("priority message for %s: interrupting the turn" % target)
                else:
                    # #2810 : son propre NOTIFY, traité après `_pick` (ou après
                    # la remise) : le tour sert déjà ce message, l'arrêter ne
                    # ferait que le re-livrer au tour suivant.
                    log_async("priority message for %s already served by the turn: "
                              "no interruption" % target)
            elif worker:
                worker.wake.set()
                log_async("message for %s: waking the turn" % target)
            elif not target:
                # #4602 (N dev1) : pas de priorité pour un réveil sans cible —
                # soixante-dix CONTROL ne seraient plus une priorité ; ces
                # workers restent des sondeurs NORMAL.
                for autre in self.workers.values():
                    autre.wake.set()
            # #4515 : un courrier pour un agent que cet exécuteur ne sert pas
            # (les NOTIFY sont émis pour toute la base, donc tout le maillage)
            # ne concerne aucun de ses workers ; le réveiller tous lançait une
            # passe complète par agent à chaque courrier du maillage. Seule la
            # passe de l'exécuteur (`wake_all`, ci-dessous) peut le réclamer.
        self.wake_all.set()

    def _listen_loop(self) -> None:
        """Écoute LISTEN/NOTIFY, reconnectée après une coupure (L72) : attente
        croissante tant que la base ne répond pas (le sondage `poll` des
        workers et de la passe prend le relais), journal borné."""
        #: #4602 : le listener est un fil de contrôle ; ses lectures (`mail.get`
        #: du dispatch, `registry.get` d'un restart) passent avant les passes
        #: au repos pour que le réveil ne soit pas retenu par la file.
        admission_lock.set_priority(admission_lock.CONTROL)
        reprise = Reprise(self.db_retry_max, minimum=1.0)
        annonce = False
        while not self.stop.is_set():
            channels = [CHANNEL_MAIL, CHANNEL_LEASE, CHANNEL_BUDGET]
            service = getattr(self, "train_service", None)
            if service is not None:
                channels.append(train_events.CHANNEL)
            listener = storage.of(self.db).wakeups.subscribe(channels)
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
            # #4515 : un NOTIFY émis pendant la coupure est perdu, et le
            # sondage au repos s'est allongé (`idle_poll`) : chaque worker
            # relit une fois dès que l'écoute est (r)établie.
            self._wake_workers()
            if service is not None:
                # LISTEN is established first: recover hints lost before startup
                # or during disconnection once, never on a listener timeout.
                service.wake()
            if not annonce:
                log_async("LISTEN %s" % ", ".join(channels))
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
        self._stop_signal_received = True
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

    def start_device_channels(self) -> None:
        """L93 : sert l'état local et l'arrêt propre sur des sockets Unix.

        Un canal par exécuteur (les unités sont par agent) ; le client réunit
        les canaux de la machine. Sans base joignable, sans droits sur le
        dossier ou hors service (`--once`, médié), le canal est simplement
        absent — il ne fait jamais tomber l'exécuteur.
        """
        if self.once or self.dry_run or getattr(self, "mediated", False):
            return
        if (os.environ.get("AMEESH_DEVICE") or "").strip().lower() in (
                "0", "off", "false", "no"):
            return
        control_enabled = (os.environ.get("AMEESH_DEVICE_CONTROL") or "").strip().lower() \
            not in ("0", "off", "false", "no")
        state_path = control_path = ""
        channels: list = []
        try:
            state_path, control_path = device_state.paths(self.runner_id)

            def gate_state():
                gate = getattr(self, "host_gate", None)
                if gate is None:
                    return None
                try:
                    return gate.current.to_json()
                except Exception:
                    return None

            def build_state():
                return device_state.snapshot(
                    self.cfg, self.db, names=self.agents_filter,
                    runner_id=self.runner_id, socket_path=state_path,
                    control_socket=control_path, gate=gate_state())

            state = device_state.StateChannel(
                state_path, build_state,
                log=lambda msg: log_async("device state: %s" % msg))
            channels.append(state)
            if control_enabled:
                control = device_state.ControlChannel(
                    control_path,
                    lambda agent: device_state.stop_agent(self.cfg, self.db, agent, strict=True),
                    allowed=self.agents_filter,
                    log=lambda msg: log_async("device control: %s" % msg))
                if getattr(self, "agents_file", None):
                    control.set_allowed(self.agents_filter)
                self._control_channel = control  # #3925 : suit la liste relue
                channels.append(control)
            for channel in channels:
                channel.start()
        except Exception as exc:
            for channel in channels:
                try:
                    channel.stop()
                except Exception:
                    pass
            log_async("device channel unavailable (%s): continuing without it" % exc)
            return
        self.device_channels = channels
        log_async("device channel: %s (control %s)" % (
            state_path, control_path if control_enabled else "disabled"))

    def shutdown(self) -> None:
        self.stop.set()
        self.wake_all.set()
        # L93 : les canaux locaux s'arrêtent avant que la base ne se ferme
        for channel in getattr(self, "device_channels", ()):
            try:
                channel.stop()
            except Exception:
                pass
        self.device_channels = []
        train_service = getattr(self, "train_service", None)
        if train_service is not None:
            train_service.shutdown()
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
        if train_service is not None and train_service.thread is not None:
            self.stopped_threads.append(train_service.thread)
        if getattr(self, "host_gate", None) is not None:
            self.host_gate.tick()  # dernier acquittement : plus aucun bail
            self.host_gate.stop()
        if train_service is not None:
            # #3134 : un effet de train encore en vol (push, promotion) finit,
            # puis le bail train-service est rendu avant la sortie immédiate
            # de `_sortie_sure` ; les effets suivants sont déjà refusés.
            train_service.finish()
        # par la file : aucune écriture synchrone sur le chemin d'arrêt avant
        # le repli de `_sortie_sure` (relecture codex2 de 669a6fe, B1)
        log_async("shutdown: %d lease(s) returned, %d turn(s)" % (len(workers), self.turns))

    # -- canon sync (spec §4.4) --------------------------------------------
    @migrations.writer_tick(on_admission_error=_background_admission_error)
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
        self._designation_retry_ready = False
        entries = canon_mod.configured(self.cfg)
        self._canon_sync_counts = (0, len(entries))
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
            actually_synced = False
            for canon in loaded:
                try:
                    interval = getattr(self, "canon_sync_interval", 0.0)
                    report = (canon_sync.periodic_sync(
                        db, canon, self.host, interval,
                        on_incomplete=lambda why: log_async(
                            "host gate: canon sync incomplete: %s" % why))
                              if interval > 0 else canon_sync.sync(db, canon, self.host))
                    if report is None:
                        # Another executor is syncing, or this snapshot differs
                        # from its completed one: do not open the host gate.
                        continue
                    actually_synced |= not getattr(report, "shared", False)
                    synced.append(canon)
                    status = getattr(report, "status", None)
                    if status and status != canon_sync.CANON_OK:
                        log_async("canon sync%s: %s (%s)" % (
                            " %s" % canon.id if len(loaded) > 1 else "", status,
                            getattr(report, "diagnostic", None) or "no detail"))
                    # #1464 (N1 #793) : un repère d'envoi figé (canon réécrit)
                    # arrête les poussées de règles pour toujours ; on le dit
                    # une fois par incident, pas à chaque passe.
                    processes = getattr(report, "processes", None)
                    push_error = getattr(processes, "error", "") if processes else ""
                    errors = getattr(self, "_process_push_errors", {})
                    ident = getattr(canon, "id", "")
                    previous_error = errors.pop(ident, "")
                    if push_error:
                        errors[ident] = push_error
                    self._process_push_errors = errors
                    if push_error and push_error != previous_error:
                        log_async("canon process rules%s: %s" % (
                            " %s" % canon.id if len(loaded) > 1 else "",
                            push_error))
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
            self._canon_sync_counts = (len(synced), len(entries))
            self._canon_complete = bool(entries) and len(synced) == len(entries)
            # #2561: only a pass that actually synced a canon may replay the
            # pending designations (a mocked/test `canon_sync_once` must not).
            self._designation_retry_ready = actually_synced
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

    def retry_review_designations(self) -> None:
        """#2561: retry the freezes whose automatic designation was journaled
        unavailable. A canon publication/sync (or a worktree that became
        readable) can lift the cause after the freeze; the freeze is kept, so
        the same head is replayed, never a newer one. #3328: the same pass
        reattributes a seat whose designated reviewer became unreachable (host
        closed, stopped/dead, blocked/auth_blocked #4491). Bounded and never
        fatal: the canon sync must not fail because a designation retry did."""
        if getattr(self, "mediated", False):
            return
        if not getattr(self, "_designation_retry_ready", False):
            return  # only after a pass that really synced a canon
        try:
            from . import review_designation_activation as activation
            if not activation.enabled(self.cfg):
                return
            from .scoped_db import ScopedDb
            db = ScopedDb(self.cfg)
            try:
                report = activation.retry_pending(self.cfg, db)
                # #3328 : a seat whose reviewer's host vanished is reattributed
                # on the same pass, on the same freeze. Bounded and never fatal.
                moved = activation.reassign_unreachable(self.cfg, db)
            finally:
                db.close()
            if report.get("attempted") or report.get("errors"):
                detail = "" if not report.get("errors") else (
                    " (%d error(s): %s)" % (len(report["errors"]), report["errors"][0]))
                log_async("review designation retry: %s%s"
                          % (report.get("summary") or "", detail))
            if moved.get("examined") or moved.get("errors"):
                detail = "" if not moved.get("errors") else (
                    " (%d error(s): %s)" % (len(moved["errors"]), moved["errors"][0]))
                log_async("review designation reassign: %s%s"
                          % (moved.get("summary") or "", detail))
        except Exception as exc:  # noqa: BLE001 - never fatal for the canon sync
            log_async("review designation retry failed: %s"
                      % " ".join(str(exc).split())[:200])

    def _canon_sync_pass(self) -> bool:
        """Settle the host gate before optional, potentially slow review work."""
        initial = not self._gate_settled.is_set()
        started = time.monotonic()
        if initial:
            log_async("host gate: canon sync starting")
        ok = self.canon_sync_once()
        settled = self._settle_gate_after_sync()
        if initial:
            read, configured = getattr(self, "_canon_sync_counts", (0, 0))
            cause = "complete" if settled else ("incomplete" if ok else "failed")
            log_async("host gate: canon sync %.3fs, canons %d/%d, %s"
                      % (time.monotonic() - started, read, configured, cause))
        if ok and settled:
            # A retry may scan Git history: it must neither delay the first
            # claim nor postpone the next sync while the gate is incomplete.
            self.retry_review_designations()
        return settled

    def _canon_sync_loop(self) -> None:
        while not self.stop.is_set():
            settled = self._canon_sync_pass()
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
            self._canon_sync_pass()
            return
        if self.canon_thread is not None:
            return
        if self.canon_sync_interval <= 0:
            if self._canon_sync_pass():  # seulement au démarrage…
                return
            # … ou jusqu'à la première porte d'hôte fixée (fil ci-dessous)
        self.canon_thread = threading.Thread(
            target=self._canon_sync_loop, daemon=True, name="canon-sync")
        self.canon_thread.start()

    # -- solde des fournisseurs payés au token (L26) ------------------------
    @migrations.writer_tick(on_admission_error=_background_admission_error)
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
    @migrations.writer_tick(on_admission_error=_background_admission_error)
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

    @migrations.writer_tick
    def host_lifecycle_once(self, db) -> None:
        """Drive durable requests with the enrolled helper, or record its named gap."""
        from . import handoffs, host_lifecycle
        try:
            # An operator may finish enrollment after startup. Retry only an
            # empty binding; a live operation keeps its factory until restart.
            if (hasattr(self, "_lifecycle_factory")
                    and self._lifecycle_factory is None):
                self._bind_lifecycle()
            api = storage.of(db).host_lifecycle
            api.detect_losses(actor=self.runner_id,
                              older_than=int(host_lifecycle.staleness_s(
                                  self.cfg.resource_interval)),
                              critical=host_lifecycle.critical_names(self.cfg, db),
                              released_grace=float(self.cfg.host_release_boot_grace))
            api.expire_returns(open_claims=False)
            controls = api.listing()
            # #3035/#3180: a lost host that publishes fresh readings again is
            # reopened only on the database proof (origins home and unfenced, no
            # active handoff or recovery since the loss). A live lease of a home
            # agent is that host's own runner taking its lease back and does not
            # block the proof; any healthy runner may make that decision, never
            # the returning machine's own heartbeat.
            for control in controls:
                if control["mode"] != "lost":
                    continue
                result = host_lifecycle.reconcile(self.cfg, db, control["host"],
                                                  actor=self.runner_id)
                if result.get("reconciled"):
                    log_async("[%s] lost host %s reconciled (generation %s)"
                              % (self.runner_id, control["host"], result.get("generation")))
            for control in controls:
                if control["mode"] in ("draining", "returning", "lost"):
                    # #4148: a returning host whose `busy` hook has not
                    # reopened claims must not receive agents back yet.
                    if control["mode"] == "returning" and not control.get("claims_open", True):
                        continue
                    host_lifecycle.advance_host(
                        db, control["host"], limits_for=self.handoff_limits_for,
                        adapter=getattr(self, "_lifecycle_adapter", None),
                        requirements_for=getattr(self, "_lifecycle_requirements", None))
        except (db_mod.DbError, handoffs.HandoffError, ValueError):
            log_async("host lifecycle: request remains pending; helper or prerequisites unavailable")

    @migrations.writer_tick(on_admission_error=_background_admission_error)
    def host_release_once(self, *, scan_local=True, complete=False) -> None:
        """#4148: one release-scanner pass (adapter transitions, auto drain).

        #4435: `scan_local=False` is the bounded one-shot pass: it advances
        the durable requests but never starts an automatic withdrawal of the
        calling host. `complete=True` then closes the returns whose claims the
        pass just reopened, since no later resource pass may come.
        """
        from . import host_release
        db = None
        try:
            from .scoped_db import ScopedDb
            db = ScopedDb(self.cfg)
            if complete:
                from . import host_lifecycle
                # No resource pass will call `host_lifecycle_once` after this
                # one: expire the planned returns here, claims kept closed so
                # the `busy` hook still answers first.
                storage.of(db).host_lifecycle.expire_returns(open_claims=False)
            host_release.once(self.cfg, db,
                              local_host=self.host if scan_local else None,
                              actor=self.runner_id)
            if complete:
                host_lifecycle.complete_returns(db)
        except Exception as exc:  # a scanner pass never stops the runner
            log_async("host release: pass failed (%s: %s)"
                      % (type(exc).__name__, str(exc)[:160]))
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def start_host_release_poll(self) -> None:
        """#4148: transitions every minute; the idle scan is spaced in the DB.

        #4435: a one-shot executor is the only scanner of its host; it makes
        one explicit bounded pass (no automatic withdrawal) and closes the
        returns it reopened, then hands back.
        """
        if getattr(self, "mediated", False):
            return
        if self.once:
            self.host_release_once(scan_local=False, complete=True)
            return

        def boucle() -> None:
            while not self.stop.is_set():
                self.host_release_once()
                if self.stop.wait(60.0):
                    return

        threading.Thread(target=boucle, daemon=True, name="hotes-liberation").start()

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
    #: CTX-03b: a recorded harness version changes at the probe rhythm (6 h);
    #: a short cache keeps `turn.context` from reading SQL on every turn.
    HARNESS_VERSION_CACHE_S = 300.0

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
        return getattr(self, "_housekeeping", None) or workspaces.default_policy()

    def agents_in_turn(self) -> set:
        """Agents en tour sur cet exécuteur — et ceux dont le travail de fond
        d'un tour fini tourne encore (délai de grâce) : le ménage les traite
        de même (dossier temporaire, caches, worktrees)."""
        with self.lock:
            en_tour = {name for name, worker in self.workers.items()
                       if worker.is_alive() and getattr(worker, "proc", None) is not None}
        return en_tour | self.background.agents()

    @migrations.writer_tick(on_admission_error=_background_admission_error)
    def housekeeping_once(self, *, min_interval_s: float = 0.0) -> dict | None:
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
            return self._housekeeping_pass(min_interval_s=min_interval_s)
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
                          workspaces.tmpfs_pressure(mesure.get("system_tmp_top") or [],
                                                    self.housekeeping_policy())))

        threading.Thread(target=passage, daemon=True, name="menage-tmpfs").start()
        return True

    def _housekeeping_pass(self, *, min_interval_s: float = 0.0) -> dict | None:
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
            report = workspaces.periodic_pass(
                self.cfg, db, self.housekeeping_policy(), host=self.host,
                actor="runner:%s" % self.runner_id, busy=busy, agents_in_turn=en_tour,
                runtime=self.container_runtime(),
                min_interval_s=min_interval_s,
                # #3293 : au seuil de pause (disque presque plein), le ménage
                # libère les caches au lieu d'attendre le quota.
                free_target=int((self._host_limits or {}).get("min_disk_free") or 0),
                # L230: the worktrees are walked at most every few hours
                artifact_scan=build_artifacts.due(time.time()),
                # #4754 : les unités de veille de l'hôte, re-épinglées
                units_dir=workspaces.host_units_dir())
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
        fusionnés, livrés, fermés ou réassignés sont purgées. #794 : la session
        ACTIVE d'un lot FINI n'est PAS fermée ici — ce balayage n'a pas le bail
        de l'agent et peut tomber en plein tour (revue Claude B1 du
        2026-10-14) ; elle est fermée par son agent, à son tour suivant, sous
        son bail (`AgentWorker._close_finished_lot_session`). Les transcripts
        du harnais restent au ménage. Ne lève jamais."""
        try:
            purges = storage.of(db).lot_sessions.purge()
        except db_mod.DbError as exc:
            log_async("lot sessions not purged (%s)" % exc)
            return []
        for row in purges:
            if row.get("session_id"):
                log_async("[%s] session %s put aside for lot #%s dropped (lot finished "
                          "or reassigned)" % (row["agent"], row["session_id"],
                                              row["work_item_id"]))
        return purges

    @migrations.writer_tick
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

    @migrations.writer_tick
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

    @migrations.writer_tick(on_admission_error=_background_admission_error)
    def notify_health_once(self, db=None, now: float | None = None) -> dict | None:
        """#3007 : dit, depuis cet hôte, si un notificateur vivant couvre les
        humains qui ont des décisions en attente, et briefe les agents.

        Le constat vient des battements partagés (`notify_liveness`) : une VM
        sans route n'est plus une alerte, et le brief ne dépend pas du poste
        qui porte `ameesh-notify`. La réclamation `(destinataire, humain)` en
        base dédoublonne les hôtes et espace les relances. Sa propre connexion
        quand `db` est absent ; jamais fatal à l'exécuteur."""
        if getattr(self, "dry_run", False) or getattr(self, "mediated", False):
            return None
        own = db is None
        try:
            if own:
                from .scoped_db import ScopedDb
                db = ScopedDb(self.cfg)
            from . import notify_gap, validation_deadlines
            try:
                validation_deadlines.sweep(self.cfg, db, now=now)
            except Exception as exc:
                # Deadline failures never suppress the notifier safety brief.
                # Do not log private routing or audit data from an exception.
                log_async("validation deadlines: sweep failed (%s)" % type(exc).__name__)
            report = notify_gap.brief_once(self.cfg, db, now=now)
        except Exception as exc:      # jamais fatal pour l'exécuteur
            log_async("notify gap: pass failed (%s: %s)" % (type(exc).__name__, exc))
            return None
        finally:
            if own and db is not None:
                try:
                    db.close()
                except Exception:
                    pass
        for entry in report["sent"]:
            log_async("notify gap: brief %s to %s for %s (%d decision(s))"
                      % ("reminder" if entry.get("reminder") else "sent",
                         entry["recipient"], entry["human"],
                         len(entry.get("decisions") or ())))
        for entry in report["failed"]:
            log_async("notify gap: brief to %s failed (%s)"
                      % (entry.get("recipient") or "?", entry.get("error")))
        return report

    @migrations.writer_tick(on_admission_error=_background_admission_error)
    def branch_sweep_once(self) -> dict | None:
        """L118 : un relevé des branches des lots ouverts dont l'assigné est sur
        cet hôte (et, avec `--agents`, parmi eux) : une branche fusionnée dans
        sa cible, avec ou sans PR, fait passer son lot livré
        (`plan_git.sync_branches`). Le passage du feu (#1374, `fire.run_pass`)
        suit : il arme l'auto-fusion des lots qui portent un feu du pilote et
        réconcilie les PR fusionnées, avant l'atterrissage de la file
        (`radar_once`). Sa propre connexion ; ne lève jamais."""
        from . import plan_git
        db = None
        try:
            from .scoped_db import ScopedDb
            db = ScopedDb(self.cfg)
            # #3972 : un filtre d'admission vide ne sélectionne aucun agent
            # (un ensemble vide n'est jamais « tous les agents »)
            report = plan_git.sync_branches(
                db, host=self.host,
                agents=set(self.agents_filter) if self.agents_filter is not None else None,
                dry_run=self.dry_run, actor="runner %s" % self.runner_id)
            for entry in report["results"]:
                if entry["result"] in ("merged", "would-merge"):
                    log_async("work item #%s delivered: %s%s" % (
                        entry["work_item"], entry["detail"],
                        " [dry-run]" if entry["result"] == "would-merge" else ""))
            if not self.dry_run:
                self.footprint_once(db)
            self.carry_once(db)
            self.packet_sweep_once(db)
            self.fire_once(db)
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

    @migrations.writer_tick(on_admission_error=_background_admission_error)
    def footprint_once(self, db) -> None:
        """#4818 : l'empreinte suit le travail réel — le diff d'une branche
        poussée par un agent de cet hôte remplace l'estimation dès le premier
        push (et à chaque nouvelle tête), et une analyse d'impact restée
        inconnue est retentée (`work_footprints.run_pass`, borné). Ne lève
        jamais ; une même erreur n'est dite qu'une fois."""
        from . import registry, work_footprints
        try:
            names = {row["name"] for row in registry.overview(db)
                     if (row.get("host") or "") == self.host}
            if self.agents_filter is not None:
                names &= set(self.agents_filter)
            report = work_footprints.run_pass(
                self.cfg, db, actor="runner %s" % self.runner_id, agents=names,
                cursor=getattr(self, "_footprint_cursor", 0))
            self._footprint_cursor = int(report.get("cursor") or 0)
        except Exception as exc:  # jamais fatal pour l'exécuteur
            report = {"refreshed": [], "analyzed": [],
                      "errors": ["%s: %s" % (type(exc).__name__, exc)]}
        for entry in report["refreshed"]:
            if entry.get("changed"):
                log_async("footprint of #%s refreshed from its pushed head %s"
                          % (entry["lot"], entry["head"][:12]))
        for entry in report["analyzed"]:
            log_async("impact analysis of #%s retried: %s"
                      % (entry["lot"], entry["status"]))
        seen = getattr(self, "_footprint_errors", set())
        errors = set("footprint pass: %s" % error for error in report["errors"])
        for line in sorted(errors - seen):
            log_async(line)
        self._footprint_errors = errors

    @migrations.writer_tick
    def carry_once(self, db) -> None:
        """#2442 : reporte sur la tête gelée les verdicts d'une tête dont elle
        n'est qu'un réalignement mécanique sur la cible, avant le passage du
        feu qui lit la porte des avis. Ne lève jamais ; une même erreur n'est
        dite qu'une fois."""
        from . import verdict_carry
        try:
            report = verdict_carry.run_pass(fresh_radar(self.cfg), db, dry_run=self.dry_run)
        except Exception as exc:  # jamais fatal pour l'exécuteur
            report = [{"lot": "?", "result": "error",
                       "detail": "%s: %s" % (type(exc).__name__, exc)}]
        seen = getattr(self, "_carry_errors", set())
        errors = set()
        for entry in report:
            if entry.get("result") not in verdict_carry.LOUD:
                continue
            line = "verdict carry: lot #%s %s" % (entry["lot"], verdict_carry.describe(entry))
            if entry["result"] == "error":
                errors.add(line)
                if line in seen:
                    continue
            log_async(line)
        self._carry_errors = errors

    @migrations.writer_tick
    def packet_sweep_once(self, db) -> None:
        """#2426 : ferme les lots unitaires des paquets fusionnés.

        Les manifestes `reviews/**paquet**.md` sont lus sur la pointe fraîche
        de la cible de chaque projet `radar` (clone privé, `land.fresh_target`),
        sous le verrou de passage du projet : seuls les lots du manifeste sont
        examinés, chacun par son dernier gel. Un clone que personne ne fetch
        ne fermait jamais les unités d'un paquet fusionné (#2302). Ne lève
        jamais : le prochain passage réessaie."""
        from . import packet_close, radar
        try:
            cfg = fresh_radar(self.cfg)
            settings = radar.settings_of(cfg)
        except Exception as exc:            # configuration illisible : jamais fatal
            log_async("packet sweep failed (%s: %s)" % (type(exc).__name__, exc))
            return
        errors = getattr(self, "_packet_sweep_errors", None)
        if errors is None:
            errors = self._packet_sweep_errors = {}
        for project in settings.projects.values():
            lock = None
            try:
                lock = radar.pass_lock(cfg, project.name)
                if lock is None:
                    continue                # un autre exécutant de l'hôte le passe
                report = packet_close.sync_project(
                    cfg, db, project, dry_run=self.dry_run,
                    actor="runner %s" % self.runner_id)
            except Exception as exc:        # jamais fatal pour l'exécuteur
                message = "packet sweep %s failed (%s: %s)" % (
                    project.name, type(exc).__name__, exc)
                if errors.get(project.name) != message:
                    errors[project.name] = message
                    log_async(message)
                continue
            finally:
                if lock is not None:
                    lock.close()
            errors.pop(project.name, None)
            for entry in report["results"]:
                if entry["result"] in ("merged", "would-merge"):
                    log_async("packet %s: work item #%s delivered: %s%s" % (
                        project.name, entry["work_item"], entry["detail"],
                        " [dry-run]" if entry["result"] == "would-merge" else ""))

    @migrations.writer_tick
    def fire_once(self, db) -> None:
        """#1374 : le passage du feu du pilote — arme l'auto-fusion des lots
        couverts par le gel courant et dont les portes sont vertes, invalide un
        feu dont la tête a bougé, réconcilie une PR fusionnée. Ne lève jamais :
        un feu raté est retenté au passage suivant. #2212 : `radar` est relu à
        chaque passage, le mode du feu (file ou fusion directe) suit la
        configuration de l'hôte, pas celle du démarrage.

        #2389 : `no-land` n'est journalisé qu'une fois par lot et par feu.
        L'abstention d'un hôte qui ne connaît pas la voie d'atterrissage est un
        état stable, pas un événement : la redire à chaque passage ne dit rien
        de neuf. Un feu invalidé, retiré ou fusionné oublie la marque, pour
        qu'un nouveau feu parle de nouveau s'il retombe en `no-land`."""
        from . import fire
        try:
            seen = getattr(self, "_fire_no_land", None)
            if seen is None:
                seen = self._fire_no_land = {}
            report = fire.run_pass(fresh_radar(self.cfg), db, dry_run=self.dry_run)
            for entry in report:
                lot = entry.get("lot")
                result = entry.get("result")
                if result == "no-land":
                    sha = str(entry.get("fire_sha") or "")
                    if seen.get(lot) == sha:
                        continue
                    seen[lot] = sha
                elif result in ("invalidated", "withdrawn", "merged"):
                    seen.pop(lot, None)
                if result in fire.LOUD:
                    log_async("fire: lot #%s %s%s" % (
                        lot, result,
                        " — %s" % (entry.get("detail") or entry.get("error"))
                        if entry.get("detail") or entry.get("error") else ""))
        except Exception as exc:  # jamais fatal pour l'exécuteur
            log_async("fire pass failed (%s: %s)" % (type(exc).__name__, exc))

    @migrations.writer_tick
    def measure_once(self, db) -> None:
        """L157c: measures the lots delivered since the last sweep (phases,
        merged diff) with the repositories of this host. Never raises."""
        from . import lot_measures
        try:
            errors: list = []
            for row in lot_measures.measure_pending(
                    db, host=self.host,
                    agents=(set(self.agents_filter)
                            if self.agents_filter is not None else None),
                    errors=errors):
                log_async("work item #%s measured: %s" % (
                    row["work_item_id"], lot_measures.describe_phases(row)))
            for lot, exc in errors:
                log_async("work item #%s not measured (%s: %s)" % (lot, type(exc).__name__, exc))
        except Exception as exc:  # never fatal for the runner
            log_async("work item measures failed (%s: %s)" % (type(exc).__name__, exc))

    @migrations.writer_tick
    def radar_once(self, db) -> None:
        """L160b : un passage du radar de conflits, après le relevé des
        branches, sur l'hôte qui déclare `radar.projects` : journal, et
        courrier aux assignés sur conflit nouveau (L160c). Ne lève jamais.
        #2212 : `radar` relu à chaque passage, comme pour le feu."""
        from . import radar
        try:
            if getattr(self, "_radar_cache", None) is None:
                self._radar_cache = radar.Cache()
            owner = getattr(self, "runner_id", None) or None
            for line in radar.run_pass(fresh_radar(self.cfg), db, cache=self._radar_cache,
                                       owner=owner):
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

    @migrations.writer_tick
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

    def start_train_service(self) -> None:
        """#1467: separate from branch sweep, never inside a worker turn."""
        if (self.once or self.dry_run or getattr(self, "mediated", False)
                or self.stop.is_set() or getattr(self, "train_service", None) is not None):
            return
        passages = tuple(p for p in getattr(self, "train_passages", ()) if p.policy.enabled)
        if not passages:
            return
        # #2446: a shadow passage is read-only and needs no packet reservation;
        # the active passage keeps the #1467 requirement.
        if any(getattr(p, "reservation", None) is None
               for p in passages if not getattr(p, "shadow", False)):
            log_async("train service requires an explicit qualified packet reservation and deadline")
            return
        from .train_runner import Service
        self.train_service = Service(
            self.cfg, passages, stop=self.stop, report=log_async,
            lease=getattr(self, "train_service_lease", None),
            role=getattr(self, "train_role", None),
            refresh=getattr(self, "train_refresh", None))
        self.train_service.start()

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
        600 s ; 0 = aucun). Premier passage décalé entre exécuteurs ; cadence
        partagée par hôte. En mode `--once`, aucun passage périodique."""
        interval = max(0.0, float(getattr(self.cfg, "housekeeping_interval", 0) or 0))
        if getattr(self, "mediated", False) and not self.once:
            self._start_thread_expiry_notice()
        if interval <= 0 or self.once or getattr(self, "mediated", False):
            return  # exécuteur médié : pas de base, pas de ménage local (contrat 1.1)

        def boucle() -> None:
            import random
            period = max(30.0, interval)
            delay = period + random.uniform(0.0, period)
            while not self.stop.wait(delay):
                self.housekeeping_once(min_interval_s=period)
                delay = period

        threading.Thread(target=boucle, daemon=True, name="menage").start()

    def start_notify_health_poll(self) -> None:
        """#3007 : le constat de vie du notificateur, depuis chaque hôte.

        Une passe toutes les `NOTIFY_HEALTH_EVERY_S` (5 min), indépendante du
        ménage et du poste qui porte `ameesh-notify` : quand celui-ci tombe,
        les VM continuent de voir le silence et de briefer les agents. Le
        premier passage est décalé pour ne pas faire partir tous les hôtes à
        la même seconde (la réclamation en base dédoublonne de toute façon)."""
        if self.once or getattr(self, "mediated", False) or getattr(self, "dry_run", False):
            return

        def boucle() -> None:
            import random
            if self.stop.wait(random.uniform(30.0, 120.0)):
                return
            while not self.stop.is_set():
                self.notify_health_once()
                if self.stop.wait(NOTIFY_HEALTH_EVERY_S):
                    return

        threading.Thread(target=boucle, daemon=True, name="notify-health").start()

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
        # #2825 N4 : le défaut de CET hôte vient de sa propre détection (ou de
        # sa fiche) ; les autres hôtes gardent le défaut `host`, jamais celui
        # du processus qui lit leurs limites.
        local_scope = resources_mod.pressure_scope(None)
        mine = resources_mod.host_limits(canons, self.host,
                                         default_scope=local_scope)
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
                    effective = resources_mod.host_limits(
                        canons, h.title,
                        default_scope=local_scope if h.title == self.host else None)
                    par_hote[h.title] = effective["limits"]
                    if not hasattr(self, "_handoff_limits"):
                        self._handoff_limits = {}
                    self._handoff_limits[h.title] = dict(effective["limits"], max_agents=effective["max_agents"])
        self._host_max_agents = mine["max_agents"]
        self._host_limits_origin = mine["origin"]
        # #3101 : places des admissions épinglées sur cet hôte (tous les
        # canons) ; un rôle élu ne prend que celles qu'elles laissent libres
        from . import placement as placement_mod
        self._host_pinned_admissions = sum(
            placement_mod.host_load(one)[0].get(self.host, 0) for one in canons)
        # L159f : un hôte prêteur (offre `policy.lend`) a une porte fermée
        # par défaut
        sync_gate = getattr(self, "_sync_host_gate", None)
        if sync_gate is not None and complete:
            offer = limits.get("lend")
            sync_gate(bool(offer), offer)
        self._housekeeping = workspaces.policy(canons, self.host)
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

    def stop_on_signal(self) -> bool:
        """L2009 : un signal d'arrêt a-t-il déjà été traité ?

        `stop` ne dit rien ici : `shutdown` le pose dans tous les cas. Le
        marqueur de `request_stop` conserve aussi l'arrêt immédiat (drainage
        nul ou passage unique), qui ne pose pas `draining`. Le retrait médié
        pose `sigterm_requested`. Le remplacement d'image s'interdit alors
        de se remplacer."""
        return (bool(getattr(self, "_stop_signal_received", False))
                or self.draining.is_set() or bool(self.sigterm_requested))

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
        maximum déclaré : pas de borne.

        #3101 : un rôle élu (`releve: election`) ne prend que les places que
        les admissions épinglées de l'hôte laissent libres ; sinon il reste
        réclamable par ses autres candidats."""
        if getattr(self, "mediated", False) and not self._gate_admits(agent):
            return False  # L112 : `caps.max_concurrent` de la porte
        cap = getattr(self, "_host_max_agents", None)
        if cap is None or (agent or {}).get("ephemeral"):
            return True
        with self.lock:
            others = [(getattr(worker, "agent", None) or {})
                      for name, worker in self.workers.items()
                      if name != (agent or {}).get("name") and worker.is_alive()
                      and not (getattr(worker, "agent", None) or {}).get("ephemeral")]
        if len(others) >= cap:
            return False
        pinned = getattr(self, "_host_pinned_admissions", None)
        if (agent or {}).get("releve") != "election" or pinned is None:
            return True
        elected = sum(1 for other in others if other.get("releve") == "election")
        return elected < cap - pinned

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
        au milieu d'un tour. Sans relevé, aucune pression (on ne devine pas).

        #4433 : une brèche mémoire de l'hôte imputée à un cgroup de runner ne
        retient que ce runner (`own_path`, ou à défaut les agents servis) ; les
        autres exécuteurs de l'hôte restent ouverts."""
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
        # #2814 : les mesures du cgroup de CE processus sont lues sur place,
        # comme l'alimentation : le dernier relevé publié peut être celui du
        # nœud (conteneur) et ne dit rien de la capacité du conteneur
        meter = getattr(self, "_cgroup", None)
        cgroup: dict = {}
        if meter is not None:
            try:
                cgroup = meter.read()
            except Exception as exc:  # une mesure ne doit jamais arrêter la boucle
                log_async("cgroup pressure measure failed (%s)" % type(exc).__name__)
        measured = any(value is not None for key, value in cgroup.items()
                       if key != "path")
        if reading is None and not measured \
                and power.get("on_ac") is None and power.get("battery_percent") is None:
            verdict: dict = {"blocked": False, "critical": False, "breaches": [],
                             "attributed": [],
                             "limits": limits,
                             "scope": limits.get("pressure_scope", "host"),
                             "cgroup": {},
                             "power": resources_mod.power_state(None, limits)}
        else:
            merged = dict(reading or {}, **{k: v for k, v in power.items()
                                             if v is not None})
            # `CgroupMeter` returns unprefixed keys (published as
            # `host_resources.cgroup`); the pressure reader wants the flat
            # `cgroup_*` form for the live reading.
            merged.update({"cgroup_" + key: value for key, value in cgroup.items()})
            # #4433 : un franchissement mémoire imputé au cgroup d'un runner ne
            # retient que ce runner ; le chemin de SON cgroup fait foi.
            verdict = resources_mod.pressure(
                merged, limits=limits, own_path=cgroup.get("path") or None,
                own_agents=getattr(self, "agents_filter", None) or ())
            if reading is not None:
                verdict["since"] = reading.get("sampled_ts")
                verdict["reading"] = reading
        verdict["retry_in_s"] = resources_mod.QUEUE_RETRY_S
        capacity = getattr(self, "capacity", None)
        if capacity is not None:
            verdict["capacity"] = capacity.snapshot()
        with self._pressure_lock:
            self._pressure = verdict
            self._pressure_at = now
            self._pressure_episode_update(verdict)
        # #2814 : le fournisseur voit chaque verdict FRAIS (libération après
        # le calme requis) ; jamais appelé sous le verrou de pression
        if capacity is not None:
            try:
                capacity.observe(bool(verdict.get("blocked")))
            except Exception as exc:  # un fournisseur ne casse pas l'exécuteur
                log_async("capacity provider observe failed (%s)" % type(exc).__name__)
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
        de la marge. L242c : déclaré (`critical_agents`, `AMEESH_CRITICAL_AGENTS` :
        la surcharge explicite ; `AMEESH_ALERT_ORCHESTRATORS`), sinon par un
        rôle critique de sa fiche, dans son canon propriétaire seulement
        (`critical_roles` de l'organisation, défaut orchestrateur, conception,
        auditeur, relecture). Canon propriétaire illisible (#424 r1) : le
        dernier verdict lu, sinon critique (#424 r6). Canon propriétaire
        configuré sur aucun canon de l'hôte (#424 r7) : non critique, et
        jamais gravé au cache (#921 : rien ne doit déclasser un agent)."""
        if name in (getattr(self.cfg, "critical_agents", None) or ()):
            return True
        if name in getattr(self, "_orchestrators_declared", ()):
            return True
        from . import canon as canon_mod, sous_utilisation as su
        canons = getattr(self, "_audit_canons", ())
        if "_critical_memory" not in self.__dict__:
            # #424 r2 : les verdicts lus avant un redémarrage ; None si le
            # cache n'est pas digne de confiance (#424 r3 : tout y est inconnu)
            self._critical_memory = su.critical_memory(self.cfg)
            self._critical_known = {}
        known = self._critical_known
        last = known[name] if name in known else su.remembered(self._critical_memory, name)
        # le registre n'est lu que pour un nom candidat, comme `is_auditor`
        # (tous les canons configurés chargés : un absent ne dit rien)
        if (getattr(self, "_canon_complete", True) and last is not True
                and last != su.UNKNOWN
                and all(su.fully_read(c) for c in canons)
                and name not in su.critical_candidates(canons)):
            return False
        row = agent if agent is not None else registry.get(self.db, name)
        foreign = su.foreign_canon(dict(row or {}, name=name), canons,
                                   complete=getattr(self, "_canon_complete", False))
        if foreign is not None:
            # #424 r7 : état connu, pas une panne ; jamais retenu au cache
            if su.warn_foreign(name):
                log_async("[%s] owner canon %s is configured on no canon of this host:"
                          " not critical" % (name, foreign))
            return False
        configured = bool(canon_mod.configured(self.cfg))
        verdict, unknown = su.critical_verdict(
            dict(row or {}, name=name), canons, configured=configured, known=last)
        if unknown:
            if self.__dict__.setdefault("_critical_held", {}).get(name) != verdict:
                self._critical_held[name] = verdict
                log_async("[%s] canon of the agent unreadable: criticality held %s (%s)"
                          % (name, "critical" if verdict else "not critical",
                             "last known" if isinstance(last, bool) else
                             "unknown: fail-closed"))
        elif configured or canons:
            # #921 : ne rien graver du verdict de repli « hôte sans canon » —
            # ni sur le disque, ni dans la mémoire du processus (elle nourrit
            # `known` au passage suivant) : ce « non critique » serait tenu
            # quand un canon devient configuré mais illisible, et déclasserait
            # l'agent au lieu du repli critique (#424 r6). Un verdict n'est un
            # fait sur l'agent que si un canon est en jeu (`configured or
            # canons`, l'exact inverse du repli de `critical_verdict`) ; en
            # service, des canons chargés impliquent `configured`, comme la
            # garde `and configured` d'`auditor.is_critical`.
            # #424 r3 : la mémoire ne suit qu'une écriture réussie (sinon
            # nouvel essai au sondage suivant) ; #424 r4 : comparé au disque
            # à chaque verdict, jamais à la mémoire (`resume` a pu l'écrire)
            if (su.remember_critical(self.cfg, name, verdict)
                    or su._memory_path(self.cfg) is None):
                known[name] = verdict     # sans dossier d'état : la mémoire seule
        return verdict

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
                log_async("pressure on host %s (%s): start — %s; except auditors, no new turn%s"
                          % (self.host, resources_mod.scope_label(verdict.get("scope")),
                             resources_mod.describe(verdict.get("breaches"))
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
                and (self.agents_filter is None or r["name"] in self.agents_filter)]

    def harness_version(self, harness: str | None) -> str | None:
        """CTX-03b : la version du binaire de ``harness`` enregistrée pour cet
        hôte (L120, ``harness_versions.probe``), ou ``None``.

        Lecture seule, mise en cache courte : une version non enregistrée ou
        illisible reste ``None`` (le profil la dit ``version_unknown``), jamais
        une supposition. Une panne de base ne retient jamais un tour.
        """
        name = str(harness or "").strip()
        if not name:
            return None
        now = time.monotonic()
        cache = getattr(self, "_harness_version_cache", None)
        if cache is None:
            cache = self._harness_version_cache = {}
        known = cache.get(name)
        if known is not None and now < known[0]:
            return known[1]
        value = None
        try:
            for row in storage.of(self.db).hosts.harness_versions(self.host):
                if str(row.get("harness") or "") == name:
                    value = str(row.get("version") or "").strip() or None
                    break
        except Exception:  # noqa: BLE001 — observation seule, jamais fatale
            value = None
        cache[name] = (now + self.HARNESS_VERSION_CACHE_S, value)
        return value

    @migrations.writer_tick(on_admission_error=_background_admission_error)
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

    @migrations.writer_tick(on_admission_error=_background_admission_error)
    def account_claims_once(self) -> None:
        """#313/#2580 (décision 0033) : revendique, dans le registre de
        l'hôte, les identifiants des comptes de cette configuration, puis
        journalise ceux qu'un autre mesh de l'hôte déclare aussi — au premier
        passage, puis à chaque changement. Ne lève jamais."""
        from . import account_claims
        try:
            if not account_claims.refresh(self.cfg, db=self.db):
                log_async("mesh identity unreadable from the database: provider "
                          "accounts of this configuration are not claimed on the "
                          "host, a shared account would not be detected")
                return
            partages = account_claims.shared(self.cfg, db=self.db)
        except (OSError, ValueError) as exc:
            log_async("account registry of this host unusable (%s): a provider "
                      "account declared by another mesh is not detected" % exc)
            return
        vus = frozenset((p["harness"], p["account"], account_claims.describe(p))
                        for p in partages)
        if vus == getattr(self, "_accounts_shared", None):
            return
        self._accounts_shared = vus
        strict = account_claims.refuses(self.cfg)
        for harness, account, others in sorted(vus):
            log_async("%s account %s: credentials also declared by another mesh of "
                      "this host: %s — decision 0033, one organisation per account; "
                      "%s" % (harness, account, others,
                              "refused here (%s)" % account_claims.policy_name(self.cfg)
                              if strict else
                              "reported only (accounts_shared: report)"))

    def start_account_claims(self) -> threading.Thread | None:
        """#313 : revendications au démarrage, puis toutes les
        `account_claims.REFRESH_S` sur leur propre fil (une revendication vit
        une heure) ; en `--once`, un seul passage."""
        from . import account_claims
        if self.once:
            self.account_claims_once()
            return None

        def boucle() -> None:
            while not self.stop.is_set():
                self.account_claims_once()
                if self.stop.wait(account_claims.REFRESH_S):
                    return

        thread = threading.Thread(target=boucle, daemon=True, name="comptes-hote")
        thread.start()
        return thread

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

    def turn_cgroup_root(self) -> str | None:
        """L498 : la racine cgroup déléguée de cette unité, ou None (repli
        PID/PGID : hôte non Linux, délégation absente, racine non inscriptible,
        `DelegateSubgroup=runner` manquant). Sondée une fois par exécuteur et
        journalisée une fois — le code reste inerte là où la délégation n'est
        pas posée."""
        if not self._turn_cgroup_probed:
            from .platform import turn_cgroup
            root, reason = turn_cgroup.root_for_runner()
            self._turn_cgroup_root = root
            # Publish readiness only after the result, including to workers
            # probing concurrently: a pending probe is not a PID/PGID fallback.
            self._turn_cgroup_probed = True
            if root is None:
                log_async("per-turn cgroup unavailable (%s): keeping PID/PGID ownership"
                    % reason)
            else:
                log_async("per-turn cgroup: delegated root %s" % root)
        return self._turn_cgroup_root

    def _live_turn_ids(self) -> set | None:
        """Les tours de CE processus encore ouverts : cgroup posé au lancement
        et pas encore clos, et travail de fond en grâce. None si le suivi du
        fond est illisible : on ne tue alors rien à l'aveugle."""
        with self._turn_cgroups_lock:
            live = set(self._turn_cgroups_live)
        try:
            with self.background.lock:
                live.update(entry.turn_id for entry in self.background.entries
                            if entry.state == "running")
        except Exception as exc:  # jamais de nettoyage sur un état inconnu
            log_async("turn cgroups not reconciled: background state unreadable (%s)"
                      % exc)
            return None
        return live

    def reconcile_turn_cgroups(self, *, startup: bool = False) -> int:
        """L498 : enlève les cgroups `turn-*` de la racine déléguée qui n'ont
        plus de tour ouvert dans ce processus — au démarrage (`startup`, ce
        qu'un exécuteur mort a laissé : orphelins préexistants) et au passage
        périodique (nouveaux). Un cgroup dont le tour est en cours, ou dont le
        travail de fond est suivi, est laissé. `cgroup.kill` puis
        `populated 0` puis `rmdir` ; ce qui ne se vide pas est signalé avec
        son chemin, jamais enlevé de force. Ne lève jamais."""
        root = self.turn_cgroup_root()
        if root is None:
            return 0
        from .platform import turn_cgroup
        # Fail-closed : un état du fond illisible ne balaie rien, donc on le
        # sonde AVANT d'énumérer. L'énumération précède la prise de référence
        # qui décide : un tour créé pendant l'énumération est dans `live`, un
        # tour créé après est absent des chemins. Création et publication
        # partagent `_turn_cgroups_lock` avec la référence.
        if self._live_turn_ids() is None:
            return 0
        paths = turn_cgroup.orphans(root)
        live = self._live_turn_ids()
        if live is None:
            return 0
        killed = 0
        for path in paths:
            turn_id = turn_cgroup.turn_id_of(path.rsplit("/", 1)[-1])
            if not turn_id or turn_id in live:
                continue
            origine = "pre-existing" if startup else "new"
            try:
                emptied = turn_cgroup.kill(path)
            except Exception as exc:
                log_async("turn cgroup %s (%s) not reconciled (%s)"
                          % (path, origine, exc))
                continue
            if emptied:
                killed += 1
                log("turn cgroup %s (%s orphan): killed and removed" % (path, origine))
            else:
                log_async("turn cgroup %s (%s orphan) could not be emptied; "
                          "reported, nothing deleted" % (path, origine))
        return killed

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
            if any(not _finite_ts(stamp) for stamp in unread):
                log("turn resources not settled: a process with an unreadable "
                    "environment and no start time; next review in %ds"
                    % int(LIVE_REVIEW_EVERY_S))
                return []
            def unread_may_belong(row: dict) -> bool:
                if not unread:
                    return False
                start, end = row.get("started_ts"), row.get("ended_ts")
                if not _finite_ts(start):
                    return True  # no lower bound proves a process unrelated
                if end is not None and (not _finite_ts(end) or end < start):
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
        # #4602 : le fil principal de l'exécuteur est ordinaire ; une classe
        # posée par un test ou un appelant ne doit pas survivre à l'entrée.
        admission_lock.reset()
        migrations.require_compatible_writer(self.db)
        log("starting: host %s, runner %s, driver %s, schema %s, mail "
            "batch %ds (L125); config %s" % (self.host, self.runner_id, self.db.name,
                                             self.cfg.schema, int(self.mail_batch),
                                             self.cfg.config_source))
        self.start_canon_sync()
        self.start_account_claims()
        # L159f : suivi de la porte et acquittements — en `--once` aussi (r3) :
        # le tour de premier plan compte dans l'accusé partagé de l'hôte
        self._start_host_gate()
        self.start_balance_poll()
        self.start_resource_poll()
        self.start_host_release_poll()
        self.start_version_poll()
        self.start_housekeeping_poll()
        self.start_notify_health_poll()
        self.start_branch_sweep_poll()
        self.start_train_service()
        self.start_device_channels()
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
        self.listener_thread = threading.Thread(target=self._listen_loop,
                                                name="listen", daemon=True)
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
        self.reconcile_turn_cgroups(startup=True)  # L498 : cgroups d'un exécuteur mort
        reprise = Reprise(self.db_retry_max)
        code = 0
        try:
            while not self.stop.is_set():
                try:
                    migrations.require_compatible_writer(self.db)
                except migrations.IncompatibleSchema as exc:
                    log_async(str(exc))
                    code = 1
                    break
                except db_mod.Unavailable as exc:
                    log_async("schema admission unavailable: %s" % exc)
                    self.stop.wait(reprise.echec())
                    continue
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
                    self.reconcile_turn_cgroups()   # L498
                try:
                    self.reload_config()
                except Exception as exc:  # la relecture ne fait jamais tomber l'exécuteur
                    log_async("config reload failed (%s)" % type(exc).__name__)
                try:
                    if getattr(self, "agents_file", None):
                        self.refresh_admission()
                except Exception as exc:  # #3925 : jamais fatal non plus
                    log_async("admission file reload failed (%s)" % type(exc).__name__)
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
    parser.add_argument("--agents-file", default=None, metavar="FILE",
                        help="#3925: host-side admission list (one agent per line, "
                             "re-read while running; added agents are claimed without a "
                             "restart, withdrawn ones released after their turn)")
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
    # #804 : prise automatique déclenchée par l'exécuteur pour ses propres
    # agents — mêmes options et variables d'environnement qu'`ameesh notify`
    # (`AMEESH_TAKE_*`) ; les réglages sont lus par `notify.take_settings`,
    # une seule implémentation.
    from . import backlog, exploitation
    parser.add_argument("--take-idle", type=float,
                        default=exploitation._env_float("AMEESH_TAKE_IDLE",
                                                        backlog.DEFAULT_TAKE_IDLE_S),
                        help="take an item from the improvement backlog for an idle agent "
                             "after S seconds without a lot (default 1800; 0 turns it off)")
    parser.add_argument("--take-max-per-hour", type=int,
                        default=int(exploitation._env_float(
                            "AMEESH_TAKE_MAX_PER_HOUR", backlog.DEFAULT_TAKE_MAX_PER_HOUR)),
                        help="maximum backlog takes per rolling hour, for the whole mesh "
                             "(default 2)")
    parser.add_argument("--take-lot-idle", type=float,
                        default=exploitation._env_float("AMEESH_TAKE_LOT_IDLE",
                                                        backlog.DEFAULT_LOT_IDLE_S),
                        help="take the best unassigned lot of an idle agent's team after S "
                             "seconds (default 900; 0 turns it off)")
    parser.add_argument("--take-lot-max-per-hour", type=int,
                        default=int(exploitation._env_float(
                            "AMEESH_TAKE_LOT_MAX_PER_HOUR", backlog.DEFAULT_LOT_MAX_PER_HOUR)),
                        help="maximum team lots taken per rolling hour, for the whole mesh "
                             "(default 6)")
    # #1738 (N3) : l'interrupteur des propositions de relecture/correction,
    # le même qu'`ameesh notify` ; l'exécuteur est le preneur actif (#804).
    parser.add_argument("--take-review-max-per-hour", type=int,
                        default=int(exploitation._env_float(
                            "AMEESH_TAKE_REVIEW_MAX_PER_HOUR",
                            backlog.REVIEW_PROPOSAL_MAX_PER_HOUR)),
                        help="maximum review/correction proposals per rolling hour, for "
                             "the whole mesh (default 6; 0 turns them off)")
    parser.add_argument("--take-interval", type=float,
                        default=exploitation._env_float("AMEESH_TAKE_INTERVAL",
                                                        TAKE_INTERVAL_S),
                        help="seconds between two takes by this runner (default 60; 0 turns "
                             "the runner's take off)")
    # #754 (owner, 13/10) : la prise refuse tant qu'une alerte `balance_low`
    # est vive ; les seuils de cette alerte sont ceux d'`ameesh alerts` et
    # d'`ameesh notify` (une seule déclaration, dans `exploitation`).
    exploitation.add_balance_arguments(parser)
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
    from .agent_names import resolve
    parsed.name = resolve(parsed.name, db=db, operation="runner.register")
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
    # `stopped_with_mail` (l'humain sait qu'il l'a arrêté). L93 : le même
    # geste sert le canal local « mon poste » (`device_state.stop_agent`).
    try:
        returned = device_state.stop_agent(cfg, db, args[0])
    except ValueError as exc:
        print("agent-runner stop: %s" % exc, file=sys.stderr)
        return 2
    for row in returned:
        to = row.get("assigned_to")
        if to:
            log("lot #%s handed to %s (measurement to redo)" % (row["id"], to))
        else:
            log("lot #%s returned to the team queue" % row["id"])
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
    venv_guard.scrub(env, venv_guard.shim_dir(cfg))  # #2763 : comme un tour
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
    # #1645 : journal gh et identité GitHub du rôle, comme pour un tour
    from . import github_identity
    try:
        github_identity.apply(env, name, cfg.config_dir, cfg.state_dir)
    except Exception:  # noqa: BLE001 — la session part avec l'identité héritée
        pass
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
        return workspaces.attach_env(cfg, name, base_env, holds)
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
               ttl: float | None = None, max_seconds: float | None = None) -> int:
    from contextlib import ExitStack
    from . import attach as attach_mod
    from .platform import session_lock
    if not NAME_RE.match(name or ""):
        print("invalid agent name: %r" % name, file=sys.stderr)
        return 1
    max_seconds = attach_mod.DEFAULT_MAX if max_seconds is None else max_seconds
    attach_mod.Deadline(max_seconds)  # validate before taking a lock or lease
    with ExitStack() as locks:
        # The runner's requested() probe briefly holds this same lock. Give
        # that probe time to finish before declaring a duplicate session.
        for _ in range(10):
            request = session_lock.try_lock(cfg.agent_dir(name), "attach-request")
            if request is not None:
                break
            time.sleep(0.01)
        if request is None:
            print("agent %s already has an attach request/session: close it first" % name,
                  file=sys.stderr)
            return 3
        locks.enter_context(request)
        return _run_attach(cfg, db, name, wait=wait, ttl=ttl,
                           max_seconds=max_seconds, locks=locks)


def _run_attach(cfg: Config, db: db_mod.Db, name: str, *, wait: bool,
                ttl: float | None, max_seconds: float, locks) -> int:
    """`ameesh attach <agent>` : bail interactif sur la même session (C9).

    Le bail est pris même s'il est déjà détenu par un exécuteur **sans tour en
    cours** : c'est ce qui suspend la réclamation automatique (l'ancien
    exécuteur est fencé par l'epoch à son prochain renouvellement). Un tour en
    cours n'est jamais interrompu : refus, ou attente avec `--wait`. Le bail est
    renouvelé tant que la session vit, le harnais est arrêté si le bail est
    perdu, et le bail est rendu à la sortie.
    """
    from . import attach as attach_mod
    from .platform import session_lock
    from .agent_names import resolve
    name = resolve(name, db=db, operation="lease.attach")
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
        # #4293 : le binaire du canari suit l'agent en session interactive
        # aussi ; #4488 : seulement s'il est déclaré sur cet hôte.
        adapter = agent_adapter(agent.get("harness") or "other",
                                agent.get("harness_bin") or None, cfg.host)
        # #2706 : refus AVANT le bail. `dsh --profile agent` est headless et
        # exige une tâche : l'ancien code prenait le bail, lançait la commande
        # sans texte, puis le rendait en 4 s après « a task is required »,
        # ayant réveillé l'agent pour rien. Un harnais sans session interactive
        # doit refuser d'emblée, sans toucher au bail ni à la session.
        adapter.require_interactive()
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
    # #1686 N1 : le budget de passation court à partir du **premier conflit de
    # verrou**, pas depuis l'entrée dans l'attente du tour : un `--wait` plus
    # long que le budget ne fait plus refuser (3) un résident qui tient encore
    # son verrou le temps du post-tour (`_backup_session`, `_sync_memory`).
    handoff_end = None
    session_guard = None
    while True:
        current = registry.get(db, name) or {}
        if ((current.get("lease_owner") or "").startswith("attach:")
                and float(current.get("lease_expires_ts") or 0) > time.time()):
            print("agent %s already has an attached session: close it first" % name,
                  file=sys.stderr)
            return 3
        if registry.turn_in_progress(db, name):
            if not wait:
                print("a turn is in progress for %s (rerun with --wait to wait)"
                      % name, file=sys.stderr)
                return 3
            print("turn in progress for %s: waiting…" % name, file=sys.stderr, flush=True)
            time.sleep(2.0)
            continue
        if session_guard is None:
            session_guard = session_lock.try_lock(cfg.agent_dir(name), "session-use")
            if session_guard is None:
                if handoff_end is None:
                    handoff_end = time.monotonic() + attach_mod.HANDOFF_TIMEOUT
                if time.monotonic() >= handoff_end:
                    print("agent %s: resident did not hand back its session; attach refused"
                          % name, file=sys.stderr)
                    return 3
                time.sleep(0.1)
                continue
            locks.enter_context(session_guard)
        # No runner can start a turn while this guard is held. Refresh the
        # session after --wait, before taking a lease that needs cleanup.
        finished = registry.get(db, name) or {}
        if ((finished.get("harness") or "") == (agent.get("harness") or "")
                and agent_incarnation(finished) == agent_incarnation(agent)):
            agent = dict(agent, session_id=finished.get("session_id"),
                         pending_prompt=finished.get("pending_prompt"))
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
    limit = attach_mod.Deadline(max_seconds)
    expired = threading.Event()

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
            if limit.expired():
                expired.set()
                perdu.set()
                log_async("[%s] attach: maximum duration reached; returning to runner" % name)
                return
            remaining = limit.notice()
            if remaining is not None:
                log_async("[%s] attach ends in %.0fs; close the session to return to the runner"
                          % (name, remaining))
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
    def check_inputs() -> None:
        prompt_since = time.time() if agent.get("pending_prompt") else None
        alerted_since = None
        while not stop.is_set():
            try:
                queued = registry.get(db, name) or {}
                if not queued.get("pending_prompt"):
                    prompt_since = None
                elif prompt_since is None:
                    prompt_since = time.time()
                since = attach_mod.waiting_since(db, name, queued, prompt_since)
                if since is None:
                    alerted_since = None
                elif time.time() - since >= attach_mod.INPUT_DELAY and since != alerted_since:
                    log_async("[%s] attach: inputs waiting for more than 10 min; "
                              "close the session to return to the runner" % name)
                    alerted_since = since
            except db_mod.DbError as exc:
                log_async("[%s] attach: cannot check waiting inputs (%s)" % (name, exc))
            stop.wait(attach_mod.INPUT_CHECK)

    inputs = threading.Thread(target=check_inputs, daemon=True, name="%s-attach-inputs" % name)
    try:
        # codex verdict #182 (P2): everything after taking the lease is under the
        # `finally` — an error or a Ctrl-C while preparing (unreadable setting,
        # slow `claude --help`) still releases the lease
        thread.start()
        veille.start()
        inputs.start()
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
        if limit.expired():
            return 124
        if perdu.is_set():
            return 1
        log_async("[%s] interactive session: %s" % (name, " ".join(argv)))
        from .platform import child_environment
        proc = subprocess.Popen(argv, cwd=cwd, env=child_environment.harness(
                                    env, harness_revendique, compte),
                                start_new_session=True)
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
        if expired.is_set():
            code = 124
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
        for worker in (thread, veille, inputs):
            if worker.ident is not None:    # started (L223)
                worker.join(timeout=5)
        try:
            registry.release(db, name, owner, epoch)
        except db_mod.DbError:
            pass
        log_async("[%s] attach lease returned" % name)
    return code


def attach_main(argv: list[str] | None = None) -> int:
    from . import attach as attach_mod
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="ameesh attach",
        description="Put the resident to sleep for bounded native access, then return its lease (L82).")
    parser.add_argument("agent")
    parser.add_argument("--wait", action="store_true",
                        help="wait for a turn in progress to end before taking the lease")
    parser.add_argument("--ttl", type=float, default=None, help="lease duration in seconds")
    parser.add_argument("--max", type=attach_mod.duration, default=attach_mod.DEFAULT_MAX,
                        help="maximum interactive duration (default: 2h; e.g. 30m, 4h)")
    parsed = parser.parse_args(argv)
    if not _harden_executor_environment():
        return 1
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
        code = run_attach(cfg, db, parsed.agent, wait=parsed.wait, ttl=parsed.ttl,
                          max_seconds=parsed.max)
        return code
    finally:
        try:
            db.close()
        finally:
            # le battement et le veilleur journalisent par la file : la vider
            # avant la finalisation, même sur exception (SIGABRT de l'exécuteur)
            _sortie_sure(code)


#: signaux d'arrêt de l'exécuteur : SIGTERM de systemd, SIGINT du terminal
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class StopRequested(Exception):
    """L2009 : un signal d'arrêt a été reçu avant que l'exécuteur ne tourne.

    L'attente de la base au démarrage pose un gestionnaire temporaire ; quand
    la base finit par répondre, le signal ne doit pas être avalé (incident du
    16/10 : l'unité restait `deactivating/stop-sigterm` et l'exécuteur
    reprenait des tours). L'appelant sort proprement, sans tour ni bail.
    `signum` dit lequel."""
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = int(signum)


def _signal_label(signum: int) -> str:
    """Nom lisible d'un signal (`SIGTERM`), pour le journal."""
    try:
        return signal.Signals(signum).name
    except ValueError:  # pragma: no cover - signal inconnu du système
        return str(signum)


def _message_remplacement(message: str) -> None:
    """#2032 : diagnostic du chemin de remplacement, jamais bloquant.

    Le journal est fermé (`_sortie_sure`) et un tube plein ne doit pas retenir
    l'arrêt : écriture best-effort sur stderr, `EAGAIN` perdu (même doctrine
    que `_ecrire_sans_attendre`)."""
    _ecrire_sans_attendre(2, (message + "\n").encode("utf-8", "replace"))


class _CaptureArret:
    """#2032 : capte un signal d'arrêt pendant la remise au défaut.

    Fenêtre de quelques µs : un signal d'arrêt est livré, le gestionnaire C de
    CPython lève son drapeau, puis `signal.signal(sig, SIG_DFL)` remplace le
    gestionnaire avant que le gestionnaire Python n'ait tourné — le drapeau
    part avec le gestionnaire et l'arrêt est perdu : l'image neuve reprend des
    tours et l'unité reste `deactivating` (même incident que #2009). Le tube
    d'éveil (`signal.set_wakeup_fd`) reçoit le numéro du signal du gestionnaire
    C lui-même, **à la livraison** ; sa lecture après la remise au défaut
    prouve l'arrêt, que le gestionnaire Python ait tourné ou non. Le tube
    d'éveil précédent, s'il en existait un, est rendu à l'identique ; hors du
    fil principal (tests), l'armement est simplement sans effet.
    """

    def __init__(self) -> None:
        self.lu = -1
        self.ecrit = -1
        self.ancien = -1

    def armer(self) -> None:
        """Arme le tube d'éveil ; jamais d'exception (chemin d'arrêt)."""
        try:
            lu, ecrit = os.pipe()
        except OSError:
            return
        try:
            os.set_blocking(lu, False)
            os.set_blocking(ecrit, False)
            ancien = signal.set_wakeup_fd(ecrit, warn_on_full_buffer=False)
        except (OSError, ValueError, AttributeError):
            for fd in (lu, ecrit):
                try:
                    os.close(fd)
                except OSError:
                    pass
            return
        self.lu, self.ecrit, self.ancien = lu, ecrit, ancien

    def arret_vu(self) -> bool:
        """Vide le tube ; vrai si un signal d'arrêt y est tombé."""
        if self.lu < 0:
            return False
        vu = False
        while True:
            try:
                data = os.read(self.lu, 64)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            if not data:
                break
            vu = vu or any(octet in STOP_SIGNALS for octet in data)
        return vu

    def fermer(self) -> None:
        """Rend le tube d'éveil précédent et ferme les descripteurs."""
        if self.ecrit >= 0:
            try:
                signal.set_wakeup_fd(self.ancien)
            except (OSError, ValueError, AttributeError):
                pass
        for fd in (self.lu, self.ecrit):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.lu = self.ecrit = self.ancien = -1


def _replace_image_or_stop(runner) -> int:
    """L790/L2009/#2032 : remplacement d'image entre deux tours, ou sortie propre.

    Un signal d'arrêt reçu pendant que l'exécuteur se remplace ne doit jamais
    être perdu (incident #2009 : `systemctl restart` concurrent du redémarrage
    L790, unité `deactivating/stop-sigterm` 26 min, tours repris). S'il a déjà
    été traité, on ne se remplace pas : on sort en 0, ce que le service attend.
    Sinon les dispositions d'arrêt repassent au DÉFAUT avant l'`exec` : un
    signal reçu ensuite tue le processus — l'arrêt demandé est honoré — au lieu
    d'être consommé par le gestionnaire de l'image sortante. Le contrôle qui
    suit la remise au défaut rattrape un gestionnaire déjà déclenché dont
    l'effet (drainage, retrait médié) n'était pas encore visible. #2032 : le
    tube d'éveil armé avant la remise au défaut capte en plus le signal livré
    dans la fenêtre, même si le gestionnaire Python est remplacé avant d'avoir
    tourné — la course de quelques µs ne perd plus l'arrêt. Rend le code de
    sortie quand l'`exec` échoue (le service relance)."""
    if runner is not None and runner.stop_on_signal():
        _message_remplacement("agent-runner: image replacement cancelled: "
                              "stop already requested")
        return 0
    capture = _CaptureArret()
    capture.armer()
    anciens = {}
    for sig in STOP_SIGNALS:
        try:
            anciens[sig] = signal.signal(sig, signal.SIG_DFL)
        except ValueError:  # hors du fil principal (tests)
            pass
    try:
        if runner is not None and runner.stop_on_signal():
            _message_remplacement("agent-runner: image replacement cancelled: "
                                  "stop requested during the replacement")
            return 0
        if capture.arret_vu():
            _message_remplacement("agent-runner: image replacement cancelled: "
                                  "stop delivered during the replacement")
            return 0
        _restart_with_installed_code()
    finally:
        # Atteint seulement si l'`exec` n'a pas remplacé l'image (échec) ou si
        # un arrêt a été rattrapé : on rend le tube d'éveil et les dispositions
        # d'avant.
        capture.fermer()
        for sig, ancien in anciens.items():
            try:
                signal.signal(sig, ancien)
            except ValueError:  # hors du fil principal (tests)
                pass
    return EXIT_RESTART


def _connect_at_start(cfg: Config, *, wait: bool) -> "db_mod.Db | None":
    """Connexion de l'exécuteur au démarrage.

    L106 : en service (`wait`), une base injoignable au démarrage — l'hôte
    redémarre et le conteneur Postgres local, ou le tunnel vers la base, n'est
    pas encore là (2026-10-10 : un exécuteur du poste sorti en échec à 12:35:20,
    relancé à la main à 12:39) — n'est plus une sortie en échec : attente
    croissante (2 s… `db_retry_max`), jusqu'à la base ou un signal d'arrêt. En
    `--once`, l'échec reste immédiat.

    L2009 : un signal d'arrêt reçu pendant cette attente n'est jamais avalé,
    même quand la base finit par répondre. Le gestionnaire temporaire ne sert
    qu'à sortir de l'attente : il lève `StopRequested`, et l'appelant sort
    proprement (code 0, aucun tour, aucun bail)."""
    reprise = Reprise(float(getattr(cfg, "db_retry_max", 60.0)))
    arret = threading.Event()
    signaux: list[int] = []
    anciens = {}
    db = None
    if wait:
        def handler(signum, _frame):
            # gestionnaire de signal : des drapeaux seulement
            signaux.append(signum)
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
                if arret.is_set():
                    return None
                if not wait:
                    print("agent-runner: %s unreachable: %s"
                          % ("mesh server" if getattr(cfg, "backend", None) == "mediated"
                             else "database", exc), file=sys.stderr)
                    return None
                attente = reprise.echec()
                log("host not ready: database unreachable at startup (%s) — retrying "
                    "in %ds (failure %d)" % (db_mod.explain(exc), int(attente),
                                             reprise.echecs))
                if arret.wait(attente):
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
        if arret.is_set():
            # L2009 : le signal a été consommé pendant l'attente — ne pas
            # l'avaler. La base éventuellement ouverte est rendue ici, `main`
            # ne la verra jamais.
            closer = getattr(db, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # la sortie ne dépend pas de cette fermeture
                    pass
            raise StopRequested(signaux[0] if signaux else signal.SIGTERM)


def _take_settings(args) -> dict | None:
    """#804 : réglages de la prise de l'exécuteur, lus des options et
    variables d'environnement d'`ameesh notify` (une seule implémentation).
    `None` quand les deux prises (file d'amélioration, lots de l'équipe)
    sont coupées."""
    from . import notify
    settings = notify.take_settings(args)
    if settings is not None:
        # #754 : la prise par un agent au token est acquise ; l'ancien
        # `allow_paid` n'a plus d'objet.
        settings.pop("allow_paid", None)
    return settings


def _take_balance(args) -> dict:
    """#754 : seuils de l'alerte `balance_low` de la prise de l'exécuteur,
    lus des mêmes options que celles d'`ameesh alerts` et `ameesh notify`."""
    from . import exploitation
    return exploitation.balance_thresholds(args)


#: #5029: vrai quand ce processus a déjà posé son drapeau non-dumpable.
_ENV_HIDDEN = False


def _harden_executor_environment() -> bool:
    """#5029: rendre l'environnement de l'exécuteur illisible à ses pairs.

    Les clés de fournisseur (`DEEPSEEK_API_KEY`…) et le DSN vivent dans
    l'environnement initial de l'exécuteur ; un tour du même utilisateur les
    lisait dans `/proc/<pid>/environ` (incident cx2 #4944). `PR_SET_DUMPABLE`
    à 0 fait refuser cette lecture par le noyau.

    Appelé par l'exécuteur **lui-même**, jamais par un lanceur qui `exec` :
    `execve` remet le drapeau à 1. Un refus du noyau arrête l'exécuteur plutôt
    que de le laisser courir avec son environnement lisible.
    """
    global _ENV_HIDDEN
    from .platform import process as process_mod
    try:
        hidden = process_mod.become_undumpable()
    except OSError as exc:
        print("agent-runner: refusing to run: %s (the executor's environment would "
              "stay readable by other processes of the same user)" % exc,
              file=sys.stderr)
        return False
    from .platform import child_environment
    child_environment.install()
    if hidden and not _ENV_HIDDEN:
        _ENV_HIDDEN = True
        log("executor environment hidden from other processes of the same user "
            "(PR_SET_DUMPABLE=0)")
    if hidden:
        child_environment.attest_venv()
    return True


def main(argv: list[str] | None = None) -> int:
    if not _harden_executor_environment():
        return 1
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
    #: #1466 : les options de ligne de commande survivent à une relecture du
    #: fichier de configuration (elles ont décidé les baux en cours)
    overrides: dict = {}
    if parsed.host:
        overrides["host"] = parsed.host
    if parsed.runner_id:
        overrides["runner_id"] = parsed.runner_id
    if parsed.lease_ttl:
        overrides["lease_ttl"] = parsed.lease_ttl
    if parsed.poll:
        overrides["poll"] = parsed.poll
    if parsed.idle_nudge:
        overrides["idle_nudge"] = parsed.idle_nudge
    if overrides:
        cfg = dataclasses.replace(cfg, **overrides)

    from .host_gate import gate_choice
    try:
        gate_choice(cfg, runner_id=cfg.runner)
    except ValueError as exc:
        print("agent-runner: %s" % exc, file=sys.stderr)
        return 2
    try:
        db = _connect_at_start(cfg, wait=not parsed.once)
    except StopRequested as exc:
        # L2009 : un signal d'arrêt a été reçu pendant l'attente de la base —
        # sortie propre (code 0, le service ne relance pas), sans tour ni bail.
        print("agent-runner: %s received at startup: exiting without a turn"
              % _signal_label(exc.signum), file=sys.stderr)
        return 0
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
        # #2446 : assemble the train service from the host configuration
        # before the runner starts. A missing or malformed piece is a named
        # refusal, never a fallback passage; the agent service keeps working.
        train_passages, train_lease, train_role, train_refresh = (), None, None, None
        if not (parsed.once or parsed.dry_run or mediated):
            try:
                from . import train as train_mod, train_assembly
                assembly = train_assembly.assemble(cfg, notify=log_async)
                train_passages, train_lease = assembly.passages, assembly.lease
                train_role = assembly.role
                train_refresh = assembly.refresh
                log_async("train service: %s" % assembly.reason)
            except train_mod.TrainError as exc:
                log_async("train service disabled: %s" % exc)
        try:
            runner = Runner(
                cfg, db, agents=agents, agents_file=parsed.agents_file,
                once=parsed.once, wait=parsed.wait,
                dry_run=parsed.dry_run, max_turns=parsed.max_turns,
                take=_take_settings(parsed), take_interval=parsed.take_interval,
                take_balance=_take_balance(parsed),
                train_passages=train_passages,
                train_service_lease=train_lease,
                train_role=train_role,
                train_refresh=train_refresh,
                cfg_overrides=overrides,
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
        dump_stacks_on_signal()
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
    # L2009 : un signal d'arrêt reçu entre-temps fait sortir proprement (0) au
    # lieu de se remplacer — le remplacer avalerait le signal et l'unité
    # resterait `deactivating` pendant que l'image neuve reprend des tours.
    if code == EXIT_RESTART:
        code = _replace_image_or_stop(runner)
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
    service relance). #2032 : les diagnostics de ce chemin n'attendent jamais
    le puits (`_message_remplacement`)."""
    argv = _restart_argv()
    if not argv:
        _message_remplacement("agent-runner: outdated code — invocation mode not "
                              "replayable (python < 3.10): the service must restart it")
        return False
    _message_remplacement("agent-runner: outdated code — restarting with the "
                          "installed package")
    try:
        os.execv(argv[0], argv)
    except OSError as exc:
        _message_remplacement("agent-runner: restart failed: %s" % exc)
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
