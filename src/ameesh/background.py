# SPDX-License-Identifier: AGPL-3.0-only
"""Travail de fond d'un tour : délai de grâce à la fin du tour, puis nettoyage.

Constat du 2026-10-10 (onze agents sur un poste de 12 CPU) : la fin d'un tour
tuait le travail lancé en fond pendant le tour. L'exécuteur tuait le groupe de
processus du tour et le ménage supprimait les conteneurs étiquetés du tour :
une suite de tests SQL (conteneurs) coupée deux fois, 217 faux rouges et une
relecture rendue sans la suite complète ; un autre job tué, 14 min de
relance. Or les tours sont souvent clos avant la fin d'un tel travail : borne
du courrier, plafonds du tour, rotations.

Désormais, à la fin d'un tour NORMAL (le harnais a fini, ou il a été clos à
un point sûr), ce qui tourne encore — processus du groupe du tour, conteneurs
étiquetés du tour sans lot — reçoit un délai de grâce (`turn_grace_seconds` :
20 min par défaut, réglable par hôte et par agent). L'exécuteur le suit (sous
Linux il est le sous-moissonneur de ses tours, et lit donc le code de sortie
des processus orphelins), et l'agent en est informé en tête de son tour
suivant (`Tracker.note_for`). À l'échéance, le nettoyage habituel
s'applique : groupe arrêté (SIGTERM, grâce, SIGKILL), ressource du tour
fermée, conteneurs du tour sans lot supprimés par le ménage. Un arrêt
d'urgence (bail perdu, préemption, redémarrage demandé, batterie critique,
arrêt forcé de l'exécuteur) nettoie tout de suite, comme avant.

Pendant le tour, le hook de courrier ne demande pas de conclure sur la borne
du courrier tant qu'un travail du tour tourne (`hook_jobs`).
"""
from __future__ import annotations

import os
import signal
import threading
import time
from dataclasses import dataclass, field

from . import containers as containers_mod
from . import platform, storage

#: noms de processus tenus pour des shells (tâche de fond d'un harnais)
SHELLS = ("bash", "sh", "dash", "zsh", "ksh", "mksh", "fish")
#: longueur maximale d'une ligne de commande citée à l'agent
COMMAND_MAX = 80
#: taille maximale de la note donnée à l'agent (octets UTF-8)
NOTE_MAX_BYTES = 1500
#: au plus ce nombre de processus et de conteneurs cités par tour
CITED_MAX = 3


def _short(text: str, limit: int = COMMAND_MAX) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _shell(name: str) -> bool:
    return (name or "").lstrip("-") in SHELLS


def _minutes(seconds: float) -> str:
    minutes = max(0, int(round(seconds / 60.0)))
    return "moins d'une minute" if minutes == 0 else "%d min" % minutes


# --------------------------------------------------------------------------
# pendant le tour : vu du hook de courrier
# --------------------------------------------------------------------------

def jobs_in_turn(table: list[dict], pgid: int, *, exclude=()) -> list[dict]:
    """Les processus du groupe `pgid` (un tour EN COURS) qui sont un travail
    de fond : détachés du harnais (lancés avec `&` ou `nohup` par un shell
    déjà sorti), ou shell du harnais qui fait tourner une commande (tâche de
    fond d'un harnais, comme `run_in_background` de Claude Code). Jamais le
    harnais (chef du groupe), ni le hook et ses ascendants (`exclude`), ni un
    enfant direct du harnais qui n'est pas un shell (serveur MCP). Un seul
    processus par arbre : le plus haut. Vide si le harnais a disparu."""
    members = {p["pid"]: p for p in table if p["pgid"] == pgid and not p["zombie"]}
    if pgid not in members:
        return []
    exclude = set(exclude)
    enfants: dict[int, list[int]] = {}
    for p in members.values():
        enfants.setdefault(p["ppid"], []).append(p["pid"])

    def lignee(pid: int) -> list[int]:
        """Les ascendants de `pid` dans le groupe, du parent au harnais."""
        out, vus = [], {pid}
        pid = members[pid]["ppid"]
        while pid in members and pid not in vus:
            out.append(pid)
            vus.add(pid)
            pid = members[pid]["ppid"]
        return out

    candidats = []
    for pid, p in members.items():
        if pid == pgid or pid in exclude:
            continue
        haut = lignee(pid)
        if pgid not in haut:
            if p["ppid"] not in members:  # racine d'un arbre détaché
                candidats.append(pid)
        elif _shell(p["name"]) and any(c not in exclude for c in enfants.get(pid, ())):
            candidats.append(pid)
    retenus = set(candidats)
    return [members[pid] for pid in sorted(candidats)
            if not any(a in retenus for a in lignee(pid))]


def describe_process(p: dict) -> str:
    return "pid %d « %s »" % (p["pid"], _short(platform.command_line(p["pid"])
                                               or p.get("name") or "?"))


def hook_jobs(cfg, db, turn_id: str, *, table: list[dict] | None = None,
              runtime=None) -> list[str]:
    """Le travail du tour `turn_id` qui tourne encore, vu du hook de courrier
    (lancé par le harnais pendant ce tour) : descriptions courtes, vide si
    rien ou si le tour n'est pas un tour en cours de cet hôte. Le groupe du
    tour vient de sa ressource (`turn_resources.pgid`). Ne lève jamais."""
    if not turn_id or db is None:
        return []
    try:
        row = storage.of(db).turn_resources.get(turn_id)
    except Exception:
        return []
    if not row or row.get("status") != "running" or row.get("ended_ts") \
            or (row.get("host") or "") != (getattr(cfg, "host", "") or ""):
        return []
    out: list[str] = []
    if row.get("pgid"):
        try:
            table = platform.process_table() if table is None else table
            exclure = set(platform.ancestry())
        except platform.NotAvailable:
            table, exclure = [], set()
        out += [describe_process(p) for p in jobs_in_turn(table, int(row["pgid"]),
                                                          exclude=exclure)]
    try:
        runtime = containers_mod.Runtime.from_config(cfg) if runtime is None else runtime
        found = runtime.turn_containers(turn_id) if runtime is not None else None
    except Exception:
        found = None
    out += ["conteneur %s" % (c["name"] or c["id"][:12]) for c in found or ()
            if not c.get("lot")]
    return out


# --------------------------------------------------------------------------
# après le tour : le délai de grâce, suivi par l'exécuteur
# --------------------------------------------------------------------------

@dataclass
class Job:
    """Un processus racine du travail de fond (son parent a fini)."""
    pid: int
    cmd: str
    #: code de sortie lu par l'exécuteur (sous-moissonneur), sinon None
    code: int | None = None
    #: fin constatée (secondes epoch), None tant qu'il tourne
    ended: float | None = None


@dataclass
class Entry:
    """Le travail de fond d'un tour fini, en délai de grâce."""
    agent: str
    turn_id: str
    pgid: int | None
    #: fin du tour et échéance de la grâce (secondes epoch)
    ended: float
    deadline: float
    grace_s: int
    jobs: dict = field(default_factory=dict)
    #: tous les processus vus dans le groupe : un groupe dont aucun membre
    #: n'en descend a été réattribué (numéro recyclé) — jamais tué
    lineage: set = field(default_factory=set)
    #: autres processus vivants du groupe (descendants des racines)
    others: int = 0
    #: conteneurs du tour sans lot : en cours, et tous ceux vus
    containers: list = field(default_factory=list)
    containers_seen: dict = field(default_factory=dict)
    containers_at: float = 0.0
    #: running | done | expired | stopped
    state: str = "running"
    closed: float | None = None
    reported: bool = False


class Tracker:
    """Le travail de fond des tours d'un exécuteur (un par processus).

    `survivors` (fin d'un tour normal) dit ce qui tourne encore ; `start`
    l'inscrit en délai de grâce ; `tick` (fil `fond`, toutes les
    `TICK_S`) suit les processus et les conteneurs, lit les codes de sortie
    et applique le nettoyage à l'échéance ; `note_for` rédige ce que l'agent
    apprend en tête de son tour suivant ; `stop_all` nettoie tout (arrêt de
    l'exécuteur). Ne lève jamais vers l'exécuteur."""

    TICK_S = 5.0
    #: un relevé des conteneurs coûte un appel au moteur : espacé
    CONTAINERS_EVERY_S = 30.0
    #: zombies adoptés hors des tours suivis (démons détachés) : moissonnés
    STRAYS_EVERY_S = 60.0
    #: fin de tour : délai laissé aux processus qui se ferment avec le
    #: harnais (serveurs MCP) avant de parler de travail de fond
    SETTLE_S = 2.0
    #: SIGTERM puis ce délai, puis SIGKILL (comme `AgentWorker.terminate`)
    TERM_GRACE_S = 3.0
    #: une entrée close et dite à l'agent part ; close et jamais dite (agent
    #: sans tour suivant), elle part après ce délai
    KEEP_S = 24 * 3600.0

    def __init__(self, runner, log=print) -> None:
        self.runner = runner
        self.log = log
        self.lock = threading.Lock()
        self.entries: list[Entry] = []
        #: l'exécuteur adopte-t-il les orphelins de ses tours (codes de sortie) ?
        self.subreaper = False
        self._strays_at = 0.0
        self.thread: threading.Thread | None = None

    # -- fil de suivi -------------------------------------------------------
    def start_monitor(self, stop: threading.Event) -> None:
        """Mode service : sous-moissonneur (Linux), puis le fil `fond`."""
        self.subreaper = platform.become_subreaper()
        if self.thread is not None:
            return

        def boucle() -> None:
            while not stop.wait(self.TICK_S):
                self.tick()

        self.thread = threading.Thread(target=boucle, daemon=True, name="fond")
        self.thread.start()

    # -- fin de tour --------------------------------------------------------
    def survivors(self, turn_id: str, pgid: int | None) -> tuple[list, list] | None:
        """À la fin d'un tour normal (harnais fini et moissonné) : ce qui
        tourne encore, `(processus vivants du groupe, conteneurs du tour sans
        lot)`, ou None si rien. Attend `SETTLE_S` au plus que le groupe se
        vide de lui-même. Si l'OS ne permet pas de lister le groupe : None —
        le nettoyage immédiat d'avant s'applique."""
        vivants: list[dict] = []
        if pgid:
            fin = time.monotonic() + self.SETTLE_S
            while True:
                try:
                    vivants = self._members(pgid, platform.process_table())
                except platform.NotAvailable:
                    return None
                if not vivants or time.monotonic() >= fin:
                    break
                time.sleep(0.1)
        conteneurs: list[dict] = []
        runtime = self.runner.container_runtime()
        if runtime is not None:
            try:
                conteneurs = [c for c in runtime.turn_containers(turn_id) or ()
                              if not c.get("lot")]
            except Exception:  # un moteur indisponible n'est pas un travail
                conteneurs = []
        if not vivants and not conteneurs:
            return None
        return vivants, conteneurs

    def _members(self, pgid: int, table: list[dict], entry: Entry | None = None) -> list:
        """Membres VIVANTS du groupe ; les zombies du groupe qui sont nos
        enfants (adoptés) sont moissonnés au passage, leur code noté."""
        vivants = []
        for p in table:
            if p["pgid"] != pgid:
                continue
            if not p["zombie"]:
                vivants.append(p)
                continue
            code = platform.reap(p["pid"])
            if entry is not None and code is not None and p["pid"] in entry.jobs:
                job = entry.jobs[p["pid"]]
                if job.ended is None:
                    job.code, job.ended = code, time.time()
        return vivants

    def start(self, *, agent: str, turn_id: str, pgid: int | None, members: list,
              containers: list, grace_s: int, now: float | None = None) -> Entry:
        """Inscrit le travail de fond d'un tour fini en délai de grâce."""
        now = time.time() if now is None else float(now)
        pids = {p["pid"] for p in members}
        entry = Entry(agent=agent, turn_id=turn_id, pgid=pgid, ended=now,
                      deadline=now + max(0, int(grace_s)), grace_s=max(0, int(grace_s)),
                      lineage=set(pids) | ({pgid} if pgid else set()),
                      containers=list(containers),
                      containers_seen={c["id"]: c for c in containers},
                      containers_at=now)
        for p in members:
            if p["ppid"] not in pids:  # racine : son parent (le harnais) a fini
                entry.jobs[p["pid"]] = Job(p["pid"], describe_process(p))
        entry.others = len(members) - len(entry.jobs)
        with self.lock:
            self.entries.append(entry)
        try:
            storage.of(self.runner.db).turn_resources.begin_grace(
                turn_id, [c["id"] for c in containers] or None)
        except Exception as exc:  # la grâce ne casse jamais la fin d'un tour
            self.log("[%s] ressource du tour %s non marquée en grâce (%s)"
                     % (agent, turn_id[:8], exc))
        self.log("[%s] travail de fond du tour %s : %s — délai de grâce de %s, puis "
                 "nettoyage s'il tourne encore"
                 % (agent, turn_id[:8], self._what(entry, running_only=True),
                    _minutes(entry.grace_s)))
        return entry

    # -- suivi ---------------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        """Un passage du suivi ; ne lève jamais."""
        now = time.time() if now is None else float(now)
        with self.lock:
            suivis = [e for e in self.entries if e.state == "running"]
            self.entries = [e for e in self.entries
                            if not (e.state != "running" and (e.reported or (
                                e.closed is not None and now - e.closed > self.KEEP_S)))]
        strays = self.subreaper and now - self._strays_at >= self.STRAYS_EVERY_S
        if not suivis and not strays:
            return
        try:
            table = platform.process_table()
        except platform.NotAvailable:
            table = None
        except Exception:
            return
        for entry in suivis:
            try:
                self._refresh(entry, table, now)
            except Exception as exc:  # jamais fatal au fil de suivi
                self.log("[%s] suivi du travail de fond du tour %s en échec (%s)"
                         % (entry.agent, entry.turn_id[:8], exc))
        if strays and table is not None:
            self._strays_at = now
            self._reap_strays(table, suivis)

    def _refresh(self, entry: Entry, table: list[dict] | None, now: float) -> None:
        vivants: list[dict] = []
        if entry.pgid and table is not None:
            membres = [p for p in table if p["pgid"] == entry.pgid]
            parents = entry.lineage | {os.getpid()}  # adoptés : ppid = l'exécuteur
            if membres and not any(p["pid"] in entry.lineage or p["ppid"] in parents
                                   for p in membres):
                # Le numéro du groupe a été réattribué (tous les processus du
                # tour avaient fini) : on ne tue jamais ce nouveau groupe.
                entry.pgid = None
                membres = []
            entry.lineage.update(p["pid"] for p in membres)
            vivants = self._members(entry.pgid, membres, entry) if entry.pgid else []
            pids = {p["pid"] for p in vivants}
            for job in entry.jobs.values():
                if job.ended is None and job.pid not in pids:
                    job.code, job.ended = platform.reap(job.pid), now
            for p in vivants:  # un descendant dont le parent a fini : racine
                if p["pid"] not in entry.jobs and p["ppid"] not in pids:
                    entry.jobs[p["pid"]] = Job(p["pid"], describe_process(p))
            entry.others = sum(1 for p in vivants if p["pid"] not in entry.jobs)
        elif entry.pgid and table is None and _group_alive(entry.pgid):
            vivants = [{"pid": entry.pgid}]  # détail indisponible : le groupe vit
        if not vivants or now - entry.containers_at >= self.CONTAINERS_EVERY_S:
            self._containers(entry, now)
        if not vivants and not entry.containers:
            self._close(entry, "done", now)
        elif now >= entry.deadline:
            self._expire(entry, now)

    def _containers(self, entry: Entry, now: float) -> None:
        runtime = self.runner.container_runtime()
        if runtime is None:
            entry.containers = []
            return
        try:
            found = runtime.turn_containers(entry.turn_id)
        except Exception:
            found = None
        entry.containers_at = now
        if found is None:
            return  # moteur muet : on garde le dernier relevé
        entry.containers = [c for c in found if not c.get("lot")]
        for c in entry.containers:
            entry.containers_seen[c["id"]] = c

    def _expire(self, entry: Entry, now: float) -> None:
        """Échéance de la grâce : le nettoyage habituel."""
        self._kill(entry)
        self._close(entry, "expired", now)

    def _kill(self, entry: Entry) -> None:
        """SIGTERM au groupe, `TERM_GRACE_S`, puis SIGKILL ; zombies adoptés
        moissonnés (sinon ils gardent le groupe « vivant »)."""
        if not entry.pgid:
            return
        for sig, delai in ((signal.SIGTERM, self.TERM_GRACE_S), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(entry.pgid, sig)
            except OSError:
                return  # groupe disparu
            fin = time.monotonic() + delai
            while time.monotonic() < fin:
                try:
                    if not self._members(entry.pgid, platform.process_table(), entry):
                        return
                except platform.NotAvailable:
                    if not _group_alive(entry.pgid):
                        return
                time.sleep(0.1)

    def _close(self, entry: Entry, state: str, now: float) -> None:
        """Fin du suivi : ressource du tour fermée (orpheline si quelque chose
        survit), conteneurs du tour sans lot supprimés par le ménage, issue
        journalisée — et SEULEMENT ENSUITE l'entrée close : le drainage de
        l'exécuteur, qui attend `running_count() == 0`, ne sort pas au milieu."""
        entry.closed = now
        for job in entry.jobs.values():
            if job.ended is None:
                job.code, job.ended = platform.reap(job.pid), now
        self._release_resource(entry)
        self.log("[%s] travail de fond du tour %s : %s" % (
            entry.agent, entry.turn_id[:8], self._outcome(entry, now, state)))
        with self.lock:
            entry.state = state

    def _release_resource(self, entry: Entry) -> None:
        from . import menage as menage_mod
        db, runtime = self.runner.db, self.runner.container_runtime()
        restants: list[str] = []
        if runtime is not None:
            try:
                restants = [c["id"] for c in runtime.turn_containers(entry.turn_id) or ()]
            except Exception:
                restants = []
        orphelin = bool(restants) or bool(entry.pgid and _group_alive(entry.pgid))
        try:
            storage.of(db).turn_resources.close_turn(entry.turn_id, orphan=orphelin,
                                                     containers=restants or None)
            if runtime is not None and not menage_mod.disabled():
                lignes = menage_mod.reap_containers(runtime, db, self.runner.host,
                                                    only_turn=entry.turn_id)["entries"]
                if lignes:
                    storage.of(db).housekeeping.log(
                        self.runner.host, lignes, actor="runner:%s" % self.runner.runner_id)
        except Exception as exc:  # jamais fatal au fil de suivi
            self.log("[%s] fermeture de la ressource du tour %s impossible (%s)"
                     % (entry.agent, entry.turn_id[:8], exc))

    def _reap_strays(self, table: list[dict], suivis: list[Entry]) -> None:
        """Zombies adoptés hors des tours (démons détachés de leur groupe) :
        moissonnés, sans quoi ils s'accumulent sous l'exécuteur. Jamais un
        harnais (ni en cours de lancement), ni un enfant du groupe de
        l'exécuteur (git, psql, docker attendus par leur `subprocess`), ni un
        membre d'un tour en cours ou suivi."""
        moi = os.getpid()
        try:
            mon_groupe = os.getpgid(0)
        except OSError:
            return
        suivis_pgid = {e.pgid for e in suivis if e.pgid}
        with self.runner.launch_lock:
            proteges = set(self.runner.harness_pids)
            for p in table:
                if p["ppid"] != moi or not p["zombie"] or p["pgid"] == mon_groupe:
                    continue
                if p["pid"] in proteges or p["pgid"] in proteges \
                        or p["pgid"] in suivis_pgid:
                    continue
                platform.reap(p["pid"])

    # -- l'agent, au tour suivant --------------------------------------------
    def has_jobs(self, agent: str) -> bool:
        with self.lock:
            return any(e.agent == agent and e.state == "running" for e in self.entries)

    def running_count(self) -> int:
        with self.lock:
            return sum(1 for e in self.entries if e.state == "running")

    def agents(self) -> set:
        """Les agents dont un travail de fond est en délai de grâce."""
        with self.lock:
            return {e.agent for e in self.entries if e.state == "running"}

    def note_for(self, agent: str, now: float | None = None) -> tuple[str, list[str]]:
        """Ce que l'agent apprend en tête de son tour suivant : le travail de
        fond de ses tours précédents encore en cours, ou fini depuis (code de
        sortie), ou arrêté. Rend `(texte, tours clos à marquer dits)` ; texte
        vide si rien. Borné à `NOTE_MAX_BYTES`."""
        now = time.time() if now is None else float(now)
        with self.lock:
            entries = sorted((e for e in self.entries if e.agent == agent
                              and (e.state == "running" or not e.reported)),
                             key=lambda e: e.ended)
        if not entries:
            return "", []
        tete = ("[ameesh] Travail de fond de tes tours précédents (laissé à finir après "
                "la fin du tour, puis arrêté à l'échéance) :")
        lignes, dites = [], []
        for entry in entries:
            ligne = "- tour %s : %s." % (entry.turn_id[:8], self._outcome(entry, now))
            if len((tete + "\n".join(lignes + [ligne])).encode("utf-8")) > NOTE_MAX_BYTES:
                lignes.append("- …")  # la suite au tour d'après
                break
            lignes.append(ligne)
            if entry.state != "running":
                dites.append(entry.turn_id)
        return tete + "\n" + "\n".join(lignes) + "\n\n", dites

    def mark_reported(self, turn_ids) -> None:
        cles = set(turn_ids or ())
        with self.lock:
            for entry in self.entries:
                if entry.turn_id in cles and entry.state != "running":
                    entry.reported = True

    # -- arrêt de l'exécuteur ------------------------------------------------
    def stop_all(self, reason: str) -> int:
        """Nettoyage immédiat de tout le travail de fond suivi (arrêt de
        l'exécuteur au-delà du drainage, passage unique fini). Le fil `fond`
        (arrêté avec l'exécuteur) finit d'abord son passage : une entrée n'est
        jamais close deux fois à la fois."""
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=30.0)
        with self.lock:
            suivis = [e for e in self.entries if e.state == "running"]
        for entry in suivis:
            try:
                self._kill(entry)
                self._close(entry, "stopped", time.time())
            except Exception as exc:
                self.log("[%s] arrêt du travail de fond du tour %s impossible (%s)"
                         % (entry.agent, entry.turn_id[:8], exc))
        if suivis:
            self.log("%s : travail de fond de %d tour(s) arrêté" % (reason, len(suivis)))
        return len(suivis)

    # -- textes ---------------------------------------------------------------
    def _what(self, entry: Entry, running_only: bool = False,
              state: str | None = None) -> str:
        parts = []
        jobs = [j for j in entry.jobs.values() if not running_only or j.ended is None]
        for job in jobs[:CITED_MAX]:
            if job.ended is None:
                etat = "en cours"
            elif job.code is None:
                etat = "fini (code de sortie inconnu)"
            elif job.code < 0:
                etat = "tué par le signal %d" % -job.code
            else:
                etat = "code de sortie %d" % job.code
            parts.append("%s : %s" % (job.cmd, etat))
        if len(jobs) > CITED_MAX:
            parts.append("%d autre(s) processus" % (len(jobs) - CITED_MAX))
        if entry.others and (running_only or (state or entry.state) == "running"):
            parts.append("%d processus descendant(s)" % entry.others)
        en_cours = {c["id"] for c in entry.containers}
        vus = list(entry.containers_seen.values())
        for c in vus[:CITED_MAX]:
            if running_only and c["id"] not in en_cours:
                continue
            parts.append("conteneur %s (%s) %s" % (
                c.get("name") or c["id"][:12], c.get("image") or "?",
                "en cours" if c["id"] in en_cours else "arrêté"))
        return " ; ".join(parts) or "processus du groupe du tour"

    def _outcome(self, entry: Entry, now: float, state: str | None = None) -> str:
        state = state or entry.state
        fini = "fini il y a %s" % _minutes(now - entry.ended)
        if state == "running":
            return "%s, en cours — %s ; arrêté dans %s s'il tourne encore" % (
                fini, self._what(entry, state=state), _minutes(entry.deadline - now))
        if state == "done":
            return "terminé %s après la fin du tour — %s" % (
                _minutes((entry.closed or now) - entry.ended), self._what(entry, state=state))
        if state == "expired":
            return "arrêté à l'échéance du délai de grâce (%s) — %s" % (
                _minutes(entry.grace_s), self._what(entry, state=state))
        return "arrêté avec l'exécuteur — %s" % self._what(entry, state=state)


def _group_alive(pgid: int | None) -> bool:
    """Le groupe existe-t-il encore ? (signal 0, comme `AgentWorker`)"""
    if not pgid:
        return False
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
