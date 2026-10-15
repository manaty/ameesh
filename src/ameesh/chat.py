# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh chat — the owner's conversation agent (work item L123) and the
terminal client on an agent's shared conversation (L79).

  ameesh chat [--harness claude|codex|…] [--name chat-<human>] [--cwd DIR]
        opens an INTERACTIVE harness session (Claude by default) for the
        conversation agent of the human who launches it: decision requests
        from every project, their answers (`ameesh decide`), their other
        requests forwarded to the orchestrators;
  ameesh chat <agent> [--after SEQ] [--limit N] [--poll S] [--once]
        opens the TERMINAL CLIENT on that agent's shared conversation
        (L78/L79): the recent history, then the live stream (LISTEN on
        `ameesh_chat`, polling when the driver cannot listen), the agent's
        state and its open lots. A typed line is sent to the agent as mail;
        `!urgent <text>` preempts the turn in progress and `!stop [text]`
        interrupts it without a new message, both through
        `AMEESH_INTERRUPT_SENDERS` (refused or delivered as a normal message,
        never forced); `!quit` leaves. The client writes nothing when it does
        not come from a human's terminal: it turns read-only there;
  ameesh chat --instruction  prints the chat instructions (text versioned in the repository);
  ameesh chat --dry-run      says what would be launched (or shown), without
        launching, following or writing anything.

Unlike `ameesh attach`, the chat NEVER takes another agent's session or
lease: it has its own identity (`chat-<human>`), a fresh session on every
launch, and no background runner. It is registered in `externe` mode (no
runner claims it), with the human as owner and no work item: it appears
neither in under-use alerts, nor in "idle without a work item", nor in
`ameesh projects`.

Identity: the harness session carries `AGENT_MAIL_NAME=chat-<human>` — its
mail is delivered by the hook, it writes to the orchestrators under that
name — and `AMEESH_CHAT` / `AMEESH_CHAT_PID`. The state file
`<state>/.chat/<name>.<pid>.json` records the `ameesh chat` process (PID,
start time, human). `ameesh decide`, run inside the session, checks that this
process is indeed its ANCESTOR: the answer is then the human's, channel `chat`
(`decisions.chat_session`). The chat is launched from the human's terminal,
never from an agent's session (refused).

L233: the chat also shows decisions that agents wrote in prose in mail to
bureau or to the human (`decisions.prose_decisions`, listed by `ameesh
decisions`) and offers to have them filed as requests by their author
(`ameesh decide ask --from-mail <id>`); the chat itself never files one.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import string
import subprocess
import sys
import threading
import time

from . import config as config_mod
from . import platform
from .config import NAME_RE, Config
from .deprecated import DeprecatedAlias

#: préfixe du nom d'un agent de conversation (`chat-<humain>`)
CHAT_PREFIX = "chat-"
DEFAULT_HARNESS = "claude"
#: la consigne du chat, versionnée avec le code (données du paquet)
CONSIGNE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "consignes",
                             "chat.md")
STATE_SCHEMA = "ameesh-chat/1"
#: canal NOTIFY de la conversation (L78) et canal des changements de bail
CHANNEL_CHAT = "ameesh_chat"
CHANNEL_LEASE = config_mod.CHANNEL_LEASE


def is_chat_agent(row: dict | None) -> bool:
    """Une ligne du registre est-elle l'agent de conversation d'un humain ?
    Nom `chat-…` ET mode `externe` (`ameesh chat` ne l'inscrit pas autrement :
    un agent mené du même nom n'est jamais pris pour un chat)."""
    row = row or {}
    return str(row.get("name") or "").startswith(CHAT_PREFIX) \
        and (row.get("mode") or "execute") == "externe"


def default_name(user: str) -> str:
    """`chat-<utilisateur>`, ramené aux caractères d'un nom d'agent."""
    clean = re.sub(r"[^A-Za-z0-9._-]", "-", user or "").strip("-.") or "humain"
    return (CHAT_PREFIX + clean)[:64]


def instruction(human: str, name: str) -> str:
    """La consigne du chat, pour cet humain et ce nom d'agent."""
    with open(CONSIGNE_PATH, encoding="utf-8") as fh:
        text = fh.read()
    return string.Template(text).safe_substitute(human=human, agent=name)


#: L148 (stage 2c): old spelling kept for the callers not yet renamed.
consigne = instruction


# --------------------------------------------------------------------------
# état local : le processus `ameesh chat` d'une session
# --------------------------------------------------------------------------

def session_dir(cfg: Config) -> str:
    """`<état>/.chat` : un nom d'agent ne commence jamais par un point, ce
    dossier ne rencontre jamais celui d'un agent (`<état>/<agent>`)."""
    return os.path.join(cfg.state_dir, ".chat")


def session_path(cfg: Config, name: str, pid: int) -> str:
    return os.path.join(session_dir(cfg), "%s.%d.json" % (name, int(pid)))


def write_session(cfg: Config, record: dict) -> str:
    path = session_path(cfg, record["name"], record["pid"])
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)
    return path


def remove_session(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def verify_session(cfg: Config, name: str, pid_text, *, chain=None) -> dict:
    """L'état de la session de chat `name` dont le processus `ameesh chat`
    est `pid_text`, vérifié : état présent et cohérent, processus ANCÊTRE de
    celui-ci, même heure de démarrage (un PID recyclé ne vaut rien). Lève
    `decisions.DecisionError` sinon (fail closed : ascendance invérifiable =
    refus)."""
    from .decisions import DecisionError
    try:
        pid = int(str(pid_text or "").strip())
    except ValueError:
        raise DecisionError("refused: chat session without a process (AMEESH_CHAT_PID) — "
                            "relaunch `ameesh chat`") from None
    try:
        with open(session_path(cfg, name, pid), encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        raise DecisionError("refused: unknown chat session (%s, process %d) — relaunch "
                            "`ameesh chat` from your terminal" % (name, pid)) from None
    if not isinstance(record, dict) or record.get("name") != name \
            or int(record.get("pid") or 0) != pid:
        raise DecisionError("refused: inconsistent state for chat session %s" % name)
    try:
        pids = list(platform.ancestry() if chain is None else chain)
        started = platform.start_time(pid)
    except platform.NotAvailable as exc:
        raise DecisionError("refused: chat session cannot be verified on this host (%s)"
                            % exc) from None
    if pid not in pids:
        raise DecisionError("refused: this command does not descend from chat session %s "
                            "(process %d)" % (name, pid))
    if record.get("started_at") is None or not platform.same_start(
            float(record["started_at"]), started):
        raise DecisionError("refused: process %d is no longer chat session %s "
                            "(PID reused)" % (pid, name))
    return record


# --------------------------------------------------------------------------
# L79 : client terminal — `ameesh chat <agent>`
# --------------------------------------------------------------------------

#: commandes tapées par l'humain (une ligne = une entrée, sinon c'est un message)
CLIENT_QUIT = ("!quit", "!q", "!exit", "/quit", "/exit")
CLIENT_HELP = ("!help", "?")
CLIENT_STATE = ("!state",)
CLIENT_URGENT = "!urgent"
CLIENT_STOP = "!stop"
HISTORY_DEFAULT = 20
POLL_DEFAULT = 2.0
#: taille d'une page lue en suivi (la borne de `conversation.history` est 500)
STREAM_PAGE = 200
#: ce qu'un `!stop` sans texte laisse à l'agent (le courrier exige un corps lisible)
STOP_BODY = "(stop requested from ameesh chat)"
#: ligne d'aide du client
CLIENT_HELP_TEXT = (
    "  !urgent <text>  send now, preempting the turn in progress (if authorised)\n"
    "  !stop [text]    interrupt the turn without a new message (if authorised)\n"
    "  !state          refresh the agent's state\n"
    "  !quit           leave (also Ctrl-D)\n"
    "  <anything else> sent to the agent as mail"
)


def _stamp(ts, *, now=None):
    """Heure locale d'une entrée ; datée si elle n'est pas d'aujourd'hui."""
    if not ts:
        return "??:??:??"
    local = time.localtime(ts)
    today = time.localtime(time.time() if now is None else now)
    if (local.tm_year, local.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("%H:%M:%S", local)
    return time.strftime("%m-%d %H:%M", local)


class ClientView:
    """Rendu texte de la conversation, sans base ni E/S implicite (`out` est injecté).

    Les tranches de texte d'un même tour ne répètent pas l'en-tête, et la
    réponse finale d'un `turn_end` n'est pas réaffichée quand le flux l'a déjà
    montrée (même tour) : le client ne double jamais le texte de l'agent.
    """

    def __init__(self, out):
        self.out = out
        self._last = None
        self._text_turn = None

    def line(self, text=""):
        self.out.write("%s\n" % text)
        self.out.flush()

    def _block(self, text, *, truncated=False):
        lines = (text or "").splitlines() or [""]
        if truncated:
            lines = lines[:-1] + [lines[-1] + " […]"]
        for line in lines:
            self.line("  " + line)

    def entry(self, entry, *, now=None):
        kind = entry.get("kind")
        text = entry.get("text") or ""
        author = entry.get("author") or "?"
        turn = entry.get("turn")
        stamp = _stamp(entry.get("ts"), now=now)
        truncated = bool(entry.get("truncated"))
        if kind == "text":
            if self._last == ("text", author, turn):
                self._block(text, truncated=truncated)
            else:
                self.line("%s %s" % (stamp, author))
                self._block(text, truncated=truncated)
            if turn is not None:
                self._text_turn = turn
        elif kind == "tool":
            meta = entry.get("meta") or {}
            tool = meta.get("tool") or text or "tool"
            status = " (%s)" % meta["status"] if meta.get("status") else ""
            self.line("%s   · %s%s" % (stamp, tool, status))
        elif kind == "turn_end":
            meta = entry.get("meta") or {}
            if text.strip() and (turn is None or turn != self._text_turn):
                self._block(text, truncated=truncated)
            bits = [meta.get("status") or "end of turn"]
            if isinstance(meta.get("cost_usd"), (int, float)):
                bits.append("$%.4f" % meta["cost_usd"])
            if isinstance(meta.get("duration_s"), (int, float)):
                bits.append("%.1fs" % meta["duration_s"])
            usage = meta.get("usage") or {}
            if isinstance(usage.get("context_tokens"), int):
                bits.append("context %d" % usage["context_tokens"])
            self.line("%s   ── %s" % (stamp, " · ".join(bits)))
        elif kind == "human_message":
            self.line("%s %s [human]" % (stamp, author))
            self._block(text, truncated=truncated)
        elif kind == "input":
            self.line("%s   ← %s" % (stamp, author))
            self._block(text, truncated=truncated)
        elif kind == "state":
            self.line("%s   · %s" % (stamp, text))
        else:
            self.line("%s %s: %s" % (stamp, author, text))
        self._last = (kind, author, turn)


def _age(ts, *, now=None):
    if not ts:
        return None
    seconds = int(max(0, (time.time() if now is None else now) - ts))
    for limit, unit in ((60, "s"), (3600, "m"), (86400, "h")):
        if seconds < limit:
            return "%d%s" % (seconds, unit)
    return "%dd" % (seconds // 86400)


def state_lines(cfg: Config, db, agent: str, *, row: dict | None = None,
                now=None) -> list[str]:
    """L'état de l'agent, prêt à afficher : fiche, bail, équipe, lots ouverts."""
    from . import registry, storage
    row = registry.get(db, agent) if row is None else row
    if row is None:
        return []
    lease = storage.of(db).leases.state(agent) or {}
    head = "%s · %s · %s" % (row.get("name") or agent, row.get("mode") or "execute",
                             row.get("harness") or "?")
    if row.get("host"):
        head += " on %s" % row["host"]
    if row.get("model"):
        head += " · %s" % row["model"]
    status = row.get("status") or "?"
    if row.get("status_text"):
        status += " — %s" % row["status_text"]
    seen = _age(row.get("last_seen_ts"), now=now)
    if seen:
        status += " · seen %s ago" % seen
    if row.get("last_error"):
        status += " · last error: %s" % str(row["last_error"])[:120]
    if lease.get("live"):
        lease_line = "held by %s (epoch %s)" % (lease.get("lease_owner") or "?",
                                                lease.get("lease_epoch"))
    elif lease.get("lease_owner"):
        lease_line = "expired (last owner %s)" % lease["lease_owner"]
    else:
        lease_line = "free"
    lines = ["agent   : " + head, "state   : " + status, "lease   : " + lease_line]
    team = row.get("team")
    projects = row.get("projects") or []
    if team or projects:
        lines.append("team    : %s%s" % (
            team or "-",
            " (projects: %s)" % ", ".join(str(p) for p in projects) if projects else ""))
    try:
        lots = storage.of(db).work.open_for(agent)[:3]
    except Exception:
        lots = []
    for index, lot in enumerate(lots):
        lines.append("%s: #%s %s" % ("work    " if index == 0 else "          ",
                                     lot.get("id"), lot.get("title") or ""))
    return lines


class TerminalClient:
    """L79 : lit la conversation partagée d'un agent, la suit, et lui écrit.

    Aucune session d'agent n'est prise : les lignes tapées partent en courrier
    (`chat-…` ou l'humain local vérifié). Un `!stop`/`!urgent` passe par le
    même chemin que `ameesh interrupt`, donc par `AMEESH_INTERRUPT_SENDERS` :
    sans habilitation, il est refusé (ou remis comme un message ordinaire pour
    `!urgent`), jamais forcé.
    """

    def __init__(self, cfg: Config, db, agent: str, *, row: dict | None = None,
                 out=None, err=None, limit: int = HISTORY_DEFAULT, after=None,
                 poll: float = POLL_DEFAULT, lines=None, subscribe=None,
                 sleep=time.sleep, chain=None, environ_of=None, now=None):
        from . import decisions, registry, work
        if not NAME_RE.match(agent or ""):
            raise ValueError("invalid agent name: %r" % (agent,))
        self.cfg, self.db, self.agent = cfg, db, agent
        self.view = ClientView(out if out is not None else sys.stdout)
        self.err = err if err is not None else sys.stderr
        self.limit = limit
        self.after = after
        self.poll = poll
        self.now = now
        self.row = registry.get(db, agent) if row is None else row
        self.read_only = decisions.agent_session_reason(
            cfg, db, chain=chain, environ_of=environ_of)
        self.sender, self.human = self._identity(work)
        self.subscribe = subscribe
        self.sleep = sleep
        self._lines = lines
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._eof = threading.Event()
        self._thread: threading.Thread | None = None

    # -- identité ----------------------------------------------------------

    def _identity(self, work):
        """L'expéditeur des messages : l'humain local s'il est connu, sinon son identité de chat."""
        from . import decisions
        user = decisions.unix_user()
        if work.known_human(self.db, user, self.cfg):
            return user, True
        return default_name(user), False

    # -- lecture -----------------------------------------------------------

    def show_state(self) -> None:
        if self.row is None:
            return
        for line in state_lines(self.cfg, self.db, self.agent, row=self.row, now=self.now):
            self.view.line(line)

    def show_history(self, *, after=None, limit=None) -> int:
        """Affiche la page demandée et rend le curseur (dernier seq lu)."""
        from . import conversation
        limit = self.limit if limit is None else limit
        start = self.after if after is None else after
        if start is None:
            head = conversation.history(self.db, self.agent, after=0, limit=1)
            start = max(0, int(head["last_seq"]) - limit)
        page = conversation.history(self.db, self.agent, after=start, limit=limit)
        if page["gap"]:
            self._gap_note()
        for entry in page["entries"]:
            self.view.entry(entry, now=self.now)
        if not page["entries"]:
            self.view.line("  (no conversation entry)")
        return int(page["next_seq"])

    def _gap_note(self) -> None:
        self.view.line("  … missing entries: the history is incomplete "
                       "(retention, quarantine or an expired thread)")

    def read_new(self, cursor: int) -> int:
        from . import conversation, db as db_mod
        try:
            page = conversation.history(self.db, self.agent, after=cursor, limit=STREAM_PAGE)
        except db_mod.DbError as exc:
            self.view.line("  … history unavailable: %s" % db_mod.explain(exc, 160))
            return cursor
        if page["gap"]:
            self._gap_note()
        for entry in page["entries"]:
            self.view.entry(entry, now=self.now)
        return int(page["next_seq"])

    # -- entrée ------------------------------------------------------------

    def _pump(self, lines) -> None:
        try:
            source = sys.stdin if lines is None else lines
            for line in source:
                self._queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._eof.set()

    def _start_input(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._pump, args=(self._lines,), daemon=True,
                                        name="chat-client-input")
        self._thread.start()

    def _open_listener(self):
        from . import storage
        if self.subscribe is not None:
            return self.subscribe()
        try:
            return storage.of(self.db).wakeups.subscribe([CHANNEL_CHAT, CHANNEL_LEASE])
        except Exception:
            return None

    def _drain(self):
        while True:
            try:
                yield self._queue.get_nowait()
            except queue.Empty:
                return

    @staticmethod
    def _command(text, prefix):
        return text == prefix or text.startswith(prefix + " ")

    def handle(self, line) -> bool:
        """Traite une ligne tapée ; rend False pour quitter."""
        text = (line or "").strip()
        if not text:
            return True
        if text in CLIENT_QUIT:
            return False
        if text in CLIENT_HELP:
            self.view.line(CLIENT_HELP_TEXT)
            return True
        if text in CLIENT_STATE:
            self.show_state()
            return True
        if self._command(text, CLIENT_STOP):
            return self.interrupt(text[len(CLIENT_STOP):].strip())
        if self._command(text, CLIENT_URGENT):
            return self.send(text[len(CLIENT_URGENT):].strip(), urgent=True)
        return self.send(text)

    def send(self, text: str, *, urgent: bool = False) -> bool:
        """Dépose un message pour l'agent ; rend False seulement sur `!quit`."""
        from . import fil, mail
        if self.read_only:
            self.view.line("  ! read-only: %s" % self.read_only)
            return True
        text = (text or "").strip()
        if not text:
            return True
        payload: dict = {"human": True} if self.human else {}
        if urgent and self.sender in self.cfg.interrupt_senders:
            payload["urgent"] = True
        elif urgent:
            self.view.line("  ! %s may not interrupt a turn (AMEESH_INTERRUPT_SENDERS): "
                           "delivered as a normal message" % self.sender)
        try:
            message_id = mail.send(self.db, self.sender, self.agent, text, host=self.cfg.host,
                                   kind="event" if payload.get("urgent") else "notify",
                                   payload=payload or None)
        except fil.UnreadableBody as exc:
            self.view.line("  ! message not dropped: %s" % exc)
            return True
        self.view.line("  → sent to %s (mail #%d%s)" % (
            self.agent, message_id, ", urgent" if payload.get("urgent") else ""))
        return True

    def interrupt(self, text: str) -> bool:
        """`!stop` : le même chemin que `ameesh interrupt`, refusé sans habilitation."""
        if self.read_only:
            self.view.line("  ! read-only: %s" % self.read_only)
            return True
        if self.sender not in self.cfg.interrupt_senders:
            self.view.line("  ! !stop refused: %s is not allowed to interrupt a turn "
                           "(AMEESH_INTERRUPT_SENDERS)" % self.sender)
            return True
        return self.send(text or STOP_BODY, urgent=True)

    # -- boucle ------------------------------------------------------------

    def plan(self) -> int:
        """`--dry-run` : l'état et ce qui serait lu, sans rien suivre ni écrire."""
        from . import conversation, db as db_mod
        if self.row is None:
            print("ameesh chat: unknown agent %s" % self.agent, file=self.err)
            return 1
        self.show_state()
        try:
            head = conversation.history(self.db, self.agent, after=0, limit=1)
        except db_mod.DbError as exc:
            print("error: %s" % db_mod.explain(exc, 160), file=self.err)
            return 1
        last = int(head["last_seq"])
        start = self.after if self.after is not None else max(0, last - self.limit)
        self.view.line("dry run: %d entries, would show those after seq %d (up to %d) then follow; "
                       "input %s" % (last, start, self.limit,
                                     "read-only (%s)" % self.read_only if self.read_only
                                     else "sent as mail by %s" % self.sender))
        return 0

    def run(self, *, follow: bool = True) -> int:
        if self.row is None:
            print("ameesh chat: unknown agent %s" % self.agent, file=self.err)
            return 1
        self.show_state()
        cursor = self.show_history()
        if not follow:
            return 0
        self.view.line("  conversation shared by every participant; "
                       "the agent never sees owner approval here")
        if self.read_only:
            self.view.line("  read-only: %s" % self.read_only)
        else:
            self.view.line("  typing as %s; type !help for the commands"
                           % ("human %s" % self.sender if self.human else self.sender))
        try:
            return self.loop(cursor)
        except KeyboardInterrupt:
            self.view.line("")
            return 0

    def loop(self, cursor: int) -> int:
        from . import db as db_mod
        self._start_input()
        listener = self._open_listener()
        if listener is None:
            self.view.line("  (no LISTEN on this driver: polling every %gs)" % self.poll)
        while True:
            for line in self._drain():
                if not self.handle(line):
                    return 0
            if self._eof.is_set() and self._queue.empty():
                return 0
            signal = None
            if listener is None:
                self.sleep(self.poll)
            else:
                item = listener.wait(timeout=self.poll)
                if item is not None and item.get("event") == "down":
                    self.view.line("  … live connection lost (%s); falling back to polling"
                                   % (item.get("error") or "?"))
                    listener.close()
                    listener = None
                elif item is not None:
                    signal = item
            try:
                cursor = self.read_new(cursor)
            except db_mod.DbError as exc:
                self.view.line("  … history unavailable: %s" % db_mod.explain(exc, 160))
                self.sleep(self.poll)
            if signal is not None and signal.get("channel") == config_mod.CHANNEL_LEASE \
                    and self.row is not None:
                for line in state_lines(self.cfg, self.db, self.agent, now=self.now)[1:2]:
                    self.view.line("  · " + line)


def client_conflicts(args) -> list[str]:
    """Options de la session de chat incompatibles avec le mode client."""
    conflicts = []
    if getattr(args, "harness", DEFAULT_HARNESS) != DEFAULT_HARNESS:
        conflicts.append("--harness")
    if getattr(args, "name", None):
        conflicts.append("--name")
    if getattr(args, "cwd", None):
        conflicts.append("--cwd")
    if getattr(args, "consigne", False):
        conflicts.append("--instruction")
    return conflicts


def client_run(cfg: Config, args, *, db=None, out=None, err=None, chain=None,
               environ_of=None, **kwargs) -> int:
    """`ameesh chat <agent>` : le client terminal (L79)."""
    from . import db as db_mod
    conflicts = client_conflicts(args)
    if conflicts:
        print("ameesh chat %s: %s apply to the chat session, not to the terminal client"
              % (args.agent, ", ".join(conflicts)), file=err or sys.stderr)
        return 2
    own, close = db, False
    try:
        if own is None:
            own = db_mod.connect(cfg)
            close = True
        db_mod.require_schema(own)
        client = TerminalClient(cfg, own, args.agent, out=out, err=err,
                                limit=args.limit, after=args.after, poll=args.poll,
                                chain=chain, environ_of=environ_of, **kwargs)
        if args.dry_run:
            return client.plan()
        return client.run(follow=not args.once)
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=err or sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=err or sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("error: %s" % exc, file=err or sys.stderr)
        return 1
    finally:
        if close and own is not None:
            own.close()


# --------------------------------------------------------------------------
# lancement
# --------------------------------------------------------------------------

def chat_env(cfg: Config, name: str, pid: int, base: dict | None = None) -> dict:
    """L'environnement du harnais : l'identité du chat, la base du mesh, et
    AUCUNE variable d'exécuteur (pas de bail)."""
    from .decisions import CHAT_ENV, CHAT_PID_ENV, LEASE_ENV, RUNNER_ENV
    env = dict(os.environ if base is None else base)
    for key in RUNNER_ENV + LEASE_ENV:
        env.pop(key, None)
    env.update({
        "AGENT_MAIL_NAME": name,
        CHAT_ENV: name,
        CHAT_PID_ENV: str(int(pid)),
        "AGENT_MAIL_STATE": cfg.v0_state,
        "AGENT_MAIL_CONFIG": cfg.config_dir,
        "AMEESH_DSN": cfg.dsn, "AGENT_MESH_DSN": cfg.dsn,
        "AMEESH_SCHEMA": cfg.schema, "AGENT_MESH_SCHEMA": cfg.schema,
        "AMEESH_STATE": cfg.state_dir, "AGENT_MESH_STATE": cfg.state_dir,
        "AMEESH_HOST": cfg.host, "AGENT_MESH_HOST": cfg.host,
    })
    return env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ameesh chat", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("agent", nargs="?", default=None,
                        help="agent whose conversation to watch and talk to (L79, terminal "
                             "client); without it, opens the conversation agent's session")
    parser.add_argument("--harness", default=DEFAULT_HARNESS,
                        help="session harness (default %s)" % DEFAULT_HARNESS)
    parser.add_argument("--name", default=None,
                        help="conversation agent name (default chat-<user>)")
    parser.add_argument("--cwd", default=None,
                        help="session directory (default: a neutral directory under the state dir)")
    parser.add_argument("--instruction", dest="consigne", action="store_true",
                        help="print the chat instructions and exit")
    parser.add_argument("--consigne", dest="consigne", action=DeprecatedAlias, nargs=0,
                        canonical="--instruction", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be launched (or shown, with an agent); "
                             "launch nothing, write nothing")
    # L79 : options du client terminal (`ameesh chat <agent>`)
    parser.add_argument("--after", type=int, default=None, metavar="SEQ",
                        help="start at this conversation sequence")
    parser.add_argument("--limit", type=int, default=HISTORY_DEFAULT, metavar="N",
                        help="how many entries to show on entry (1-500, default %d)"
                             % HISTORY_DEFAULT)
    parser.add_argument("--poll", type=float, default=POLL_DEFAULT, metavar="S",
                        help="polling interval when LISTEN is unavailable "
                             "(default %gs)" % POLL_DEFAULT)
    parser.add_argument("--once", action="store_true",
                        help="show the history and exit, without following")
    return parser


def run(cfg: Config, args, *, chain=None, environ_of=None, popen=None) -> int:
    """`ameesh chat` : le client terminal d'un agent, sinon la session de chat."""
    if getattr(args, "agent", None):
        return client_run(cfg, args, chain=chain, environ_of=environ_of)
    from . import account_turn, adapters, decisions, storage
    from . import db as db_mod
    if os.environ.get(decisions.CHAT_ENV):
        print("ameesh chat: you are already in a chat session (%s)"
              % os.environ[decisions.CHAT_ENV], file=sys.stderr)
        return 1
    user = decisions.unix_user()
    name = args.name or default_name(user)
    if not NAME_RE.match(name) or not name.startswith(CHAT_PREFIX):
        print("ameesh chat: invalid name %r (chat-<name>, letters, digits, . _ -)" % name,
              file=sys.stderr)
        return 2
    adapters.configure(cfg)
    try:
        adapter = adapters.adapter_for(args.harness)
        argv = adapter.interactive_command(None)
    except adapters.HarnessMissing as exc:
        print("ameesh chat: %s" % exc, file=sys.stderr)
        return 1
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        reason = decisions.agent_session_reason(cfg, db, chain=chain, environ_of=environ_of)
        if reason:
            print("ameesh chat: refused — %s; the chat is launched from a human's terminal, "
                  "never from an agent's session (and never takes its session)"
                  % reason, file=sys.stderr)
            return 1
        human = decisions.current_human()
        cwd = os.path.abspath(os.path.expanduser(args.cwd)) if args.cwd \
            else os.path.join(session_dir(cfg), name)
        waiting = decisions.pending(db, human=human)
        # L233: decisions written in prose in mail, to offer converting them
        prose = [p for p in decisions.prose_decisions(db)
                 if p["recipient"] in decisions.PROSE_RECIPIENTS + (human, decisions.OWNER_ANY)]
        prompt = instruction(human, name)
        pid = os.getpid()
        env = chat_env(cfg, name, pid)
        env.update(adapter.env())
        ok, compte = account_turn.attach_profile(cfg, db, args.harness)
        if not ok:
            return 1
        if not account_turn.apply_env_attach(env, compte, cfg):
            return 1
        command = argv + [prompt]
        if args.dry_run:
            print("ameesh chat (dry run): %s for %s, harness %s, directory %s"
                  % (name, human, args.harness, cwd))
            print("command: %s <instructions, %d characters>" % (" ".join(argv), len(prompt)))
            print("identity: AGENT_MAIL_NAME=%s, %s=%s, %s=%d; no runner variable"
                  % (name, decisions.CHAT_ENV, name, decisions.CHAT_PID_ENV, pid))
            print("pending decisions for %s: %d" % (human, len(waiting)))
            print("decisions written in prose in mail (not requests): %d" % len(prose))
            return 0
        row = storage.of(db).decisions.register_chat(
            name, human=human, host=cfg.host, harness=args.harness, cwd=cwd,
            status_text="conversation of %s" % human)
        if row is None:
            print("ameesh chat: %s is an agent driven by a runner (or holds a lease): "
                  "choose another name (--name chat-…)" % name, file=sys.stderr)
            return 1
    finally:
        db.close()
    os.makedirs(cwd, mode=0o700, exist_ok=True)
    record = {"schema": STATE_SCHEMA, "name": name, "human": human, "pid": pid,
              "started_at": platform.start_time(pid), "harness": args.harness,
              "host": cfg.host, "cwd": cwd, "ts": round(time.time(), 3)}
    path = write_session(cfg, record)
    print("ameesh chat: %s, conversation of %s (%d pending decision(s)%s) — quitting the "
          "harness ends the conversation" % (
              name, human, len(waiting),
              "; %d decision(s) written in prose in mail, to convert" % len(prose)
              if prose else ""), flush=True)
    code = 1
    try:
        proc = (popen or subprocess.Popen)(command, cwd=cwd, env=env)
        # Ctrl-C est pour le harnais au premier plan, pas pour ce lanceur
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            code = proc.wait()
        finally:
            signal.signal(signal.SIGINT, previous)
    except OSError as exc:
        print("ameesh chat: cannot launch: %s" % exc, file=sys.stderr)
    finally:
        remove_session(path)
    return code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    cfg = config_mod.load()
    if args.consigne:
        if getattr(args, "agent", None):
            print("ameesh chat: --instruction does not take an agent (terminal client)",
                  file=sys.stderr)
            return 2
        # relue aussi DANS la session (contexte résumé) : le nom de ce chat
        from .decisions import CHAT_ENV, current_human, unix_user
        name = args.name or os.environ.get(CHAT_ENV) or default_name(unix_user())
        print(instruction(current_human(), name))
        return 0
    if not 1 <= args.limit <= 500:
        print("ameesh chat: --limit must be between 1 and 500", file=sys.stderr)
        return 2
    if args.after is not None and args.after < 0:
        print("ameesh chat: --after must be zero or positive", file=sys.stderr)
        return 2
    if args.poll <= 0:
        print("ameesh chat: --poll must be positive", file=sys.stderr)
        return 2
    from . import db as db_mod
    try:
        return run(cfg, args)
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
