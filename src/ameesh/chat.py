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
        interrupts it now (a short body is sent when no text is given), both
        through `AMEESH_INTERRUPT_SENDERS` (refused or delivered as a normal
        message, never forced); `!quit` leaves. The client writes nothing when
        it does not come from a human's terminal: it turns read-only there;
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

L480: between two messages of the human, the chat must not stay blind. When
the input and the output are a terminal, the launcher lends it to the harness
through a pty (`serve_pty`) and a bounded watch (`Veille`) reads the pending
decisions of the human and the mail that would open a turn; when something
new arrives, it serves one short `[veille ameesh] …` line into the session,
as if the human had typed it, and the harness presents it. The watch never
answers anything, never arms without a terminal (`--no-veille` refuses it),
and the process anchoring of `decisions.chat_session` is untouched: the
harness stays a child of the `ameesh chat` process.
"""
from __future__ import annotations

import argparse
import json
import os
import pty
import queue
import re
import signal
import string
import subprocess
import sys
import threading
import time

from . import config as config_mod
from . import db as db_mod
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
#: L480 : la veille du chat, la ligne qu'elle sert dans la session, ses bornes
VEILLE_PREFIX = "[veille ameesh]"
VEILLE_EVERY_S = 30.0
VEILLE_MIN_INTERVAL_S = 120.0
VEILLE_IDLE_S = 3.0
VEILLE_MAX_DEFER_S = 900.0
#: bornes de la période demandée par `--veille-every`
VEILLE_EVERY_MIN_S = 5.0
VEILLE_EVERY_MAX_S = 3600.0


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
    "  !stop [text]    interrupt the turn now; a short body is sent when text is "
    "empty (if authorised)\n"
    "  !state          refresh the agent's state\n"
    "  !quit           leave (also Ctrl-D)\n"
    "  <anything else> sent to the agent as mail"
)


# --------------------------------------------------------------------------
# L83 : l'écriture serveur d'un message humain dans la conversation
# --------------------------------------------------------------------------

#: #3889 : un courrier par (expéditeur, destinataire, Idempotency-Key)
IDEMPOTENT_MAIL_INDEX = "agent_mailbox_idempotency_key_idx"


def post_human_message(cfg: Config, db, *, human: str, sender: str, agent: str,
                       text: str, lot=None, event_id: str | None = None,
                       idempotency_key: str = "") -> dict:
    """Le message d'un humain dans la conversation partagée d'un agent (L83).

    Le serveur écrit : l'appelant a authentifié `human` (`human:<id>`, le
    rédacteur) et vérifié son droit de parler à `agent` (0033 §7/§9 ; L86
    vérifie le jeton OIDC et le lien au canon, jamais ici) ; `sender` est
    l'identité de boîte de l'humain, normalisée en `human:<id>` (L77/B1) —
    un nom nu est accepté et normalisé. Le message part
    d'abord dans la boîte (il réveille l'agent, `payload human`), puis il est
    reflété par une entrée `human_message` : `mail.send` écrit la copie
    lisible (R12), l'entrée ne porte donc pas de seconde sortie. Un échec
    après le courrier laisse le message délivré et l'erreur dite : le
    courrier est la délivrance, l'entrée son reflet. L'entrée est rangée dans
    le projet de la copie R12 : celui du lot quand un lot est posé
    (`fil.lot_project`, la règle de `mail.send`), sinon celui de l'agent.

    `event_id` est stable (`mail:<id>` par défaut) : un rejeu après la
    délivrance ne crée pas de seconde entrée.

    §16 (#3889) : `idempotency_key`, la clé de la requête de l'API, part
    dans le courrier (`payload.idempotency_key`). C'est l'accusé vérifiable
    d'une issue restée inconnue : `delivered_human_message` le retrouve et
    rend la même réponse, sans second envoi. Le dépôt est exclusif par clé
    (index 0497) : un second exécuteur sous la même clé rend le message déjà
    déposé, jamais un second courrier.
    """
    from . import mail, registry
    if not NAME_RE.match(agent or ""):
        raise ValueError("invalid agent name: %r" % (agent,))
    row = registry.get(db, agent)
    work_item_id = None if lot is None else str(lot)
    from . import resident_queue
    sender = resident_queue.human_ref(sender) or sender
    payload = {"human": True}
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    try:
        message_id = mail.send(db, sender, agent, text, host=cfg.host, kind="notify",
                               payload=payload, work_item_id=work_item_id)
    except db_mod.DbError as exc:
        # déjà déposé sous cette clé par un autre exécuteur (0497) : le sien
        if not (idempotency_key and db_mod.is_unique_violation(exc, IDEMPOTENT_MAIL_INDEX)):
            raise
        from . import storage
        mailbox = storage.of(db).mailbox
        message_id = mailbox.by_idempotency_key(sender, agent, idempotency_key)
        if message_id is None:
            raise
        # la même requête seulement : jamais un reflet d'un autre texte
        _same_message(mailbox.get(message_id), text, lot)
    return _reflect_human_message(cfg, db, row=row, human=human, agent=agent, text=text,
                                  lot=lot, message_id=message_id, event_id=event_id)


class KeyReused(ValueError):
    """L'Idempotency-Key d'un message déjà déposé, pour un autre texte ou lot."""


def _same_message(row: dict | None, text: str, lot) -> None:
    """Le courrier déposé sous une clé est-il ce message (texte et lot) ?"""
    row = row or {}
    if (row.get("body") != text
            or (row.get("work_item_id") or "") != ("" if lot is None else str(lot))):
        raise KeyReused("this Idempotency-Key was used for another message")


def delivered_human_message(cfg: Config, db, *, human: str, sender: str, agent: str,
                            text: str, lot=None, idempotency_key: str) -> dict | None:
    """§16 (#3889) : le message déjà délivré sous `idempotency_key` (son
    reflet dans la conversation complété, idempotent), ou None s'il n'est
    jamais parti. Jamais un second courrier. Lève `KeyReused` si le courrier
    de cette clé porte un autre texte ou un autre lot : ce n'est pas la même
    requête (la réservation de la clé a pu être purgée depuis)."""
    from . import registry, resident_queue, storage
    sender = resident_queue.human_ref(sender) or sender
    mailbox = storage.of(db).mailbox
    message_id = mailbox.by_idempotency_key(sender, agent, idempotency_key)
    if message_id is None:
        return None
    _same_message(mailbox.get(message_id), text, lot)
    return _reflect_human_message(cfg, db, row=registry.get(db, agent), human=human,
                                  agent=agent, text=text, lot=lot, message_id=message_id)


def _reflect_human_message(cfg: Config, db, *, row, human: str, agent: str, text: str,
                           lot, message_id: int, event_id: str | None = None) -> dict:
    from . import conversation, fil
    work_item_id = None if lot is None else str(lot)
    log = conversation.Log(db, cfg, agent=agent,
                           project=fil.project_for(cfg, fil.lot_project(db, work_item_id),
                                                   fil.agent_project(row)),
                           lot=work_item_id)
    entry = log.append("human_message", text, author=human,
                       event_id=event_id or "mail:%d" % message_id, thread=False)
    return {"agent": agent, "message_id": message_id,
            "lot": None if lot is None else int(lot),
            "entry": {key: entry.get(key)
                      for key in ("seq", "kind", "author", "ts", "event_id")}}


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
        self._text_key = None
        self._texts_seen = set()

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
            if turn is not None:
                # #887 : le journal porte parfois deux entrées `text` identiques
                # pour le même tour ; le client ne les affiche qu'une fois.
                if (author, turn) != self._text_key:
                    self._text_key = (author, turn)
                    self._texts_seen = set()
                if text in self._texts_seen:
                    self._last = (kind, author, turn)
                    return
                self._texts_seen.add(text)
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
        #: #3892 : lignes tapées pas encore déposées (base coupée), dans l'ordre
        self.outbox: list[dict] = []
        self._outbox_warned: set[str] = set()

    # -- identité ----------------------------------------------------------

    def _identity(self, work):
        """L'expéditeur des messages : l'humain local s'il est connu, sinon son identité de chat.

        L77/B1 : l'humain est déposé sous sa forme normalisée `human:<id>` (la
        même que les décisions) ; le nom nu reste celui du chat `chat-…`.
        """
        from . import decisions
        user = decisions.unix_user()
        human = work.known_human(self.db, user, self.cfg)
        if human:
            return human, True
        return default_name(user), False

    def _may_interrupt(self) -> bool:
        """L77/B1 : `AMEESH_INTERRUPT_SENDERS` se lit sur le nom nu de
        l'humain (`human:<id>` est sa forme de boîte)."""
        allowed = tuple(getattr(self.cfg, "interrupt_senders", ()) or ())
        if self.sender in allowed:
            return True
        if self.human and self.sender.startswith("human:"):
            return self.sender.split(":", 1)[1] in allowed
        return False

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
            if self.outbox:
                # #3892 : ne jamais quitter en silence sur une ligne gardée.
                self.view.line("  … %d message(s) not sent yet: kept and retried "
                               "(Ctrl-C to leave anyway)" % len(self.outbox))
                return True
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
        """Dépose un message pour l'agent ; rend False seulement sur `!quit`.

        #3892 (§16) : une panne de base au dépôt ne quitte plus le client et
        ne perd pas la ligne. Elle est gardée, avec une clé d'idempotence
        stable, et renvoyée tant que la base ne répond pas ; un renvoi après
        une réponse perdue est rejoué, jamais déposé deux fois.
        """
        if self.read_only:
            self.view.line("  ! read-only: %s" % self.read_only)
            return True
        text = (text or "").strip()
        if not text:
            return True
        payload: dict = {"human": True} if self.human else {}
        if urgent and self._may_interrupt():
            payload["urgent"] = True
        elif urgent:
            self.view.line("  ! %s may not interrupt a turn (AMEESH_INTERRUPT_SENDERS): "
                           "delivered as a normal message" % self.sender)
        self.outbox.append({
            "text": text, "urgent": bool(payload.get("urgent")),
            "payload": payload,
            # stable pour ce texte : un rejeu après une réponse perdue retombe
            # sur le message déjà déposé au lieu d'en déposer un second.
            "key": "chat:%s:%s" % (self.agent, os.urandom(8).hex()),
        })
        self.flush_outbox()
        return True

    def _send_payload(self, item: dict) -> dict:
        """Le corps du message, complété une seule fois (compte humain local)."""
        payload = dict(item["payload"])
        if payload and "human_account" not in payload:
            from . import identity, resident_queue
            binding = identity.resolve_binding(self.cfg, getattr(self, "db", None))
            if binding.source == "none":
                user, host = resident_queue.local_account(self.cfg)
                payload["human_account"] = "%s@%s" % (user or "?", host or "?")
                item["payload"] = payload
        return payload

    def flush_outbox(self) -> None:
        """Renvoie les lignes gardées, dans l'ordre ; ne lève jamais."""
        from . import db as db_mod, fil, mail, storage
        while self.outbox:
            item = self.outbox[0]
            try:
                payload = self._send_payload(item)
                message_id = mail.send(
                    self.db, self.sender, self.agent, item["text"], host=self.cfg.host,
                    kind="event" if item.get("urgent") else "notify",
                    payload=payload or None, idempotency_key=item["key"])
            except fil.UnreadableBody as exc:
                self.outbox.pop(0)
                self.view.line("  ! message not dropped: %s" % exc)
                continue
            except ValueError as exc:
                # #3892 : clé réutilisée pour un autre corps/lot (refus
                # déterministe) : la ligne ne partira jamais, on le dit.
                self.outbox.pop(0)
                self.view.line("  ! not sent: %s" % exc)
                continue
            except storage.MailboxRefused as exc:
                self.outbox.pop(0)
                self.view.line("  ! not sent: %s" % exc)
                continue
            except (db_mod.DbError, OSError) as exc:
                if item["key"] not in self._outbox_warned:
                    self._outbox_warned.add(item["key"])
                    self.view.line(
                        "  … sending: the line is kept and will be resent when the "
                        "database answers (%s)" % db_mod.explain(exc, 120))
                return
            self.outbox.pop(0)
            self._outbox_warned.discard(item["key"])
            self.view.line("  → sent to %s (mail #%d%s)" % (
                self.agent, message_id, ", urgent" if item.get("urgent") else ""))

    def interrupt(self, text: str) -> bool:
        """`!stop` : le même chemin que `ameesh interrupt`, refusé sans habilitation."""
        if self.read_only:
            self.view.line("  ! read-only: %s" % self.read_only)
            return True
        if not self._may_interrupt():
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
                           % ("human %s" % self.sender.split(":", 1)[-1]
                              if self.human else self.sender))
        try:
            return self.loop(cursor)
        except KeyboardInterrupt:
            if self.outbox:
                self.view.line("  ! %d message(s) not sent: the database did not answer; "
                               "they are lost with this session" % len(self.outbox))
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
            # #3892 : une ligne gardée est retentée à chaque tour, sans quitter
            # le client ; l'entrée terminée, on reste tant qu'elle n'est pas
            # déposée (Ctrl-C laisse sortir explicitement).
            self.flush_outbox()
            if self._eof.is_set() and self._queue.empty():
                if not self.outbox:
                    return 0
            wakeup = None
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
                    wakeup = item
            try:
                cursor = self.read_new(cursor)
            except db_mod.DbError as exc:
                self.view.line("  … history unavailable: %s" % db_mod.explain(exc, 160))
                self.sleep(self.poll)
            if wakeup is not None and wakeup.get("channel") == config_mod.CHANNEL_LEASE \
                    and self.row is not None:
                # #887 : une coupure de base sur l'état ne fait pas tomber le client.
                try:
                    lines = state_lines(self.cfg, self.db, self.agent, now=self.now)[1:2]
                except db_mod.DbError as exc:
                    self.view.line("  … state unavailable: %s" % db_mod.explain(exc, 160))
                else:
                    for line in lines:
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
# L480 : la veille — décisions et courrier servis entre deux messages
# --------------------------------------------------------------------------

def _count(n: int, singular: str, plural: str) -> str:
    return "1 %s" % singular if n == 1 else "%d %s" % (n, plural)


def _short_age(seconds: float) -> str:
    """Un âge court : « 45s », « 12m », « 3h »."""
    seconds = int(max(0.0, seconds))
    if seconds >= 3600:
        return "%dh" % (seconds // 3600)
    if seconds >= 60:
        return "%dm" % (seconds // 60)
    return "%ds" % seconds


def veille_line(pending: list[dict], seen_decisions: set, inbox: list[dict],
                seen_mail: set, *, human: str = "", now: float | None = None) -> str | None:
    """La ligne à servir dans la session, ou None : les décisions et le
    courrier que `seen_*` ne connaît pas encore, en une phrase courte.

    Elle demande de PRÉSENTER, jamais de répondre : `$human` seul tranche
    (§« Enregistrer une réponse » de la consigne)."""
    from . import mail as mail_mod
    now = time.time() if now is None else float(now)
    new_decisions = [d for d in pending if int(d["id"]) not in seen_decisions]
    new_mail = [m for m in inbox if int(m["id"]) not in seen_mail]
    if not new_decisions and not new_mail:
        return None
    bits = []
    if new_decisions:
        urgent = sum(1 for d in new_decisions
                     if d.get("urgent") or d.get("overdue")
                     or (d.get("due_ts") is not None
                         and float(d["due_ts"]) - now <= 3600.0))
        text = _count(len(new_decisions), "nouvelle décision", "nouvelles décisions")
        if urgent:
            text += " (dont %s)" % _count(
                urgent, "urgente ou à échéance proche", "urgentes ou à échéance proche")
        oldest = min(float(d.get("created_ts") or now) for d in new_decisions)
        if now - oldest >= 60.0:
            text += ", la plus ancienne il y a %s" % _short_age(now - oldest)
        bits.append(text)
    if new_mail:
        urgent = sum(1 for m in new_mail if mail_mod.is_urgent(m))
        text = _count(len(new_mail), "nouveau courrier", "nouveaux courriers")
        if urgent:
            text += " (dont %s)" % _count(urgent, "urgent", "urgents")
        bits.append(text)
    who = " à %s" % human if human else ""
    return "%s %s — lance `ameesh decisions`, présente-les%s, puis le courrier." % (
        VEILLE_PREFIX, " et ".join(bits), who)


#: L480 : l'état du harnais, écrit par le hook `agent-mail` et relu par la
#: veille juste avant d'écrire. `Stop` = le tour est fini, le prompt est rendu ;
#: tout le reste = un tour est en cours (dialogue de permission compris).
PHASE_STOP = "Stop"
PHASE_BUSY = "busy"


def phase_path(cfg: Config, name: str) -> str:
    return os.path.join(cfg.state_dir, "hooks", name + ".phase")


def hook_phase(event: str) -> str:
    """L'état à retenir après cet événement de hook.

    Seul `Stop` dit que le harnais a rendu son prompt ; `SessionStart`,
    `UserPromptSubmit` et `PostToolUse` disent qu'un tour est en cours."""
    return PHASE_STOP if event == "Stop" else PHASE_BUSY


def read_phase(cfg: Config, name: str) -> str | None:
    """Le marqueur d'état, ou None s'il manque ou est illisible : échec fermé,
    la veille n'écrit que sur un `Stop` explicite."""
    try:
        with open(phase_path(cfg, name), encoding="utf-8") as fh:
            text = fh.read(64).strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def write_phase(cfg: Config, name: str, phase: str) -> bool:
    """Écrit le marqueur atomiquement (temporaire puis renommage) ; ne lève
    jamais : un marqueur qu'on ne peut pas écrire laisse la veille fermée."""
    path = phase_path(cfg, name)
    tmp = None
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, phase.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return True
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def _draft_submitted(data: bytes) -> bool:
    """La dernière frappe soumet-elle le brouillon, ou l'abandonne-t-elle ?

    En mode brut, Entrée envoie un \r et jamais un \n ; Ctrl+J (\n), un
    antislash suivi d'Entrée et Alt+Entrée (\x1b\r) insèrent un saut de ligne
    dans Claude Code et laissent le brouillon ouvert. Seuls Ctrl-C (\x03) et
    un \r final non précédé d'un antislash ou d'ESC ferment le brouillon."""
    if not data:
        return False
    if data[-1:] == b"\x03":
        return True
    if data[-1:] != b"\r":
        return False
    return data[-2:-1] not in (b"\\", b"\x1b")


class Veille:
    """L480 : la veille d'une session de chat.

    Un fil lit la base — décisions en attente adressées à `human`, courrier
    non lu qui ouvrirait un tour pour `name` — et retient la ligne à servir ;
    le proxy du terminal (`serve_pty`) la sert quand `$human` ne tape pas. La
    première lecture est un repère : les décisions et le courrier déjà là sont
    ceux du démarrage, que la consigne fait présenter au premier tour.

    Bornes : une lecture par `every` (aucune sonde de CI), rien tant que rien
    n'est nouveau, deux services espacés d'au moins `min_interval`, et pas
    d'écriture dans les `idle` secondes qui suivent une frappe — au-delà de
    `max_defer`, on sert quand même : une session occupée ne doit pas affamer
    la veille. Garde d'état enfin : on n'écrit que si le hook `agent-mail` a
    laissé un marqueur `Stop` (prompt rendu) ; marqueur absent, illisible ou
    autre (tour en cours, dialogue de permission) = on attend le tour suivant.
    """

    def __init__(self, cfg: Config, human: str, name: str, *, every: float = VEILLE_EVERY_S,
                 min_interval: float = VEILLE_MIN_INTERVAL_S, idle: float = VEILLE_IDLE_S,
                 max_defer: float = VEILLE_MAX_DEFER_S, now=time.monotonic,
                 connect=None, log=None, phase=None):
        self.cfg, self.human, self.name = cfg, human, name
        self.every, self.min_interval = float(every), float(min_interval)
        self.idle, self.max_defer = float(idle), float(max_defer)
        self.now, self.connect = now, connect
        #: l'état du harnais (Stop = prompt rendu) ; absent/illisible = on n'écrit pas
        self.phase = phase or (lambda: read_phase(cfg, name))
        self.log = log or (lambda text: print("ameesh chat: %s" % text, file=sys.stderr,
                                              flush=True))
        self._lock = threading.Lock()
        self._seen_decisions: set[int] = set()
        self._seen_mail: set[int] = set()
        self._baseline = False
        self._text: str | None = None
        self._text_at: float | None = None
        self._draft_open = False
        self._tail = b""             # dernier octet de la lecture précédente
        self._last_human = 0.0
        self._last_served: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def observe(self, pending: list[dict], inbox: list[dict],
                *, wall: float | None = None) -> str | None:
        """Une lecture : met à jour les repères, retient la ligne s'il y a du
        nouveau (la première lecture ne sert rien)."""
        wall = time.time() if wall is None else float(wall)
        with self._lock:
            ids = {int(d["id"]) for d in pending}
            mids = {int(m["id"]) for m in inbox}
            if not self._baseline:
                self._baseline = True
                self._seen_decisions, self._seen_mail = ids, mids
                return None
            text = veille_line(pending, self._seen_decisions, inbox, self._seen_mail,
                               human=self.human, now=wall)
            self._seen_decisions, self._seen_mail = ids, mids
            if text:
                self._text, self._text_at = text, self.now()
            return text

    def note_human(self, now: float | None = None, data: bytes | None = None) -> None:
        """`$human` vient de taper : on ne lui écrit pas par-dessus.

        `data` sont les octets lus sur l'entrée du terminal. Un brouillon
        reste ouvert tant que la dernière frappe ne le soumet pas (Entrée, un
        \r final non précédé d'un antislash ou d'ESC — même si cet octet est
        arrivé dans la lecture précédente) ni ne l'abandonne (Ctrl-C, \x03) :
        la veille ne sert alors rien, même au-delà de `max_defer`, pour ne
        jamais coller sa ligne au début d'un message humain non validé."""
        self._last_human = self.now() if now is None else float(now)
        if data:
            self._draft_open = not _draft_submitted(self._tail + data)
            self._tail = data[-1:]

    def due(self, now: float | None = None) -> str | None:
        """La ligne à servir maintenant, ou None (rien de nouveau, frappe
        récente, deux services trop rapprochés, ou harnais pas au prompt).
        Passé `max_defer` depuis l'arrivée de la nouvelle, on sert quand même :
        une session occupée ne doit pas affamer la veille."""
        now = self.now() if now is None else float(now)
        with self._lock:
            text = self._text
            if not text:
                return None
            # brouillon humain ouvert : on ne l'écrase pas, même au-delà de
            # `max_defer` ; il se ferme sur Entrée ou Ctrl-C
            if self._draft_open:
                return None
            age = now - (self._text_at if self._text_at is not None else now)
            if age < self.max_defer:
                if now - self._last_human < self.idle:
                    return None
                if self._last_served is not None \
                        and now - self._last_served < self.min_interval:
                    return None
            # garde d'état, relue juste avant l'écriture : sans un `Stop`
            # explicite on n'écrit pas (dialogue de permission, tour en cours,
            # marqueur absent) ; la ligne reste en attente pour le tour suivant
            if self.phase() != PHASE_STOP:
                return None
            self._text, self._text_at = None, None
            self._last_served = now
            return text

    def pending_text(self) -> str | None:
        with self._lock:
            return self._text

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="chat-veille", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        from . import db as db_mod
        from . import decisions, mail
        try:
            db = (self.connect or db_mod.connect)(self.cfg)
        except Exception as exc:  # noqa: BLE001 — la veille ne casse jamais la session
            self.log("veille off: database unreachable (%s)" % exc)
            return
        try:
            while not self._stop.wait(self.every):
                try:
                    self.observe(decisions.pending(db, human=self.human),
                                 mail.unread_active(db, self.name))
                except Exception as exc:  # noqa: BLE001 — on réessaiera au tour suivant
                    self.log("veille: reading decisions and mail failed (%s)" % exc)
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# L480 : le terminal prêté au harnais, la veille servie entre deux frappes
# --------------------------------------------------------------------------

def _isatty(stream) -> bool:
    try:
        return bool(stream) and stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def veille_armed(args, *, popen=None) -> bool:
    """La veille s'arme-t-elle pour ce lancement ? Un terminal des deux côtés
    (entrée et sortie), pas d'injection de `popen` (tests), et `--no-veille`
    la refuse."""
    if popen is not None or getattr(args, "veille", None) is False:
        return False
    return _isatty(sys.stdin) and _isatty(sys.stdout)


def _winsize(fd) -> bytes:
    import struct
    size = os.get_terminal_size(fd)
    return struct.pack("HHHH", size.lines, size.columns, 0, 0)


def _copy_winsize(src_fd, dst_fd) -> None:
    import fcntl
    import termios
    try:
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, _winsize(src_fd))
    except (OSError, ValueError):
        pass


def _own_tty() -> None:
    """Dans l'enfant (`preexec_fn`) : le pty devient son terminal de contrôle,
    pour que Ctrl-C et le redimensionnement visent le harnais. Un échec ne
    casse pas le lancement : le harnais tourne alors sans terminal de
    contrôle."""
    import fcntl
    import termios
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


def _drain(master, stdout_fd) -> None:
    """Vide ce que le harnais a écrit avant de mourir : sa dernière sortie
    n'est pas perdue."""
    import select
    while True:
        try:
            ready, _, _ = select.select([master], [], [], 0.2)
        except (OSError, ValueError):
            return
        if not ready:
            return
        try:
            data = os.read(master, 65536)
        except OSError:
            return
        if not data:
            return
        try:
            os.write(stdout_fd, data)
        except OSError:
            return


def serve_pty(proc, master, *, veille=None, stdin_fd=None, stdout_fd=None,
              poll: float = 0.2) -> None:
    """Prête le terminal à `proc` (attaché au pty `master`) tout en laissant la
    veille écrire dans la session entre deux frappes.

    Les frappes vont au maître du pty, la sortie du harnais revient sur la
    sortie réelle ; `Ctrl-C` est un octet relayé, que le harnais traite comme
    dans son terminal. Le mode brut du terminal est rendu à la sortie, quoi
    qu'il arrive."""
    import select
    import termios
    import tty
    if stdin_fd is None:
        stdin_fd = sys.stdin.fileno()
    if stdout_fd is None:
        stdout_fd = sys.stdout.fileno()
    saved = None
    try:
        try:
            saved = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        except (termios.error, OSError, ValueError):
            saved = None  # pas un terminal (tests, tube) : on relaie quand même
        resized = threading.Event()
        stopped = threading.Event()

        def on_winch(_signum, _frame):
            resized.set()

        def on_stop(_signum, _frame):
            stopped.set()
        previous: dict = {}
        for sig, handler in ((signal.SIGWINCH, on_winch), (signal.SIGTERM, on_stop),
                             (signal.SIGHUP, on_stop)):
            try:
                previous[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # hors du fil principal (tests) : pas de signaux
        try:
            open_stdin = True
            while not stopped.is_set():
                watch = [master] + ([stdin_fd] if open_stdin else [])
                try:
                    ready, _, _ = select.select(watch, [], [], poll)
                except (OSError, ValueError):
                    break
                now = time.monotonic()
                if resized.is_set():
                    resized.clear()
                    _copy_winsize(stdin_fd, master)
                if stdin_fd in ready:
                    data = os.read(stdin_fd, 4096)
                    if not data:
                        open_stdin = False  # entrée fermée : la sortie continue
                    else:
                        try:
                            os.write(master, data)
                        except OSError:
                            break
                        if veille is not None:
                            veille.note_human(now, data)
                if master in ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    try:
                        os.write(stdout_fd, data)
                    except OSError:
                        break
                if veille is not None:
                    text = veille.due(now=now)
                    if text:
                        try:
                            # pas de \r en tête : il soumettrait le brouillon de
                            # l'humain (ou confirmerait un dialogue de permission)
                            # avant la ligne de veille
                            os.write(master, (text + "\r").encode("utf-8"))
                        except OSError:
                            break
                if proc.poll() is not None:
                    break
            _drain(master, stdout_fd)
        finally:
            for sig, ancien in previous.items():
                try:
                    signal.signal(sig, ancien)
                except (ValueError, OSError):
                    pass
    finally:
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)


def _kill_group(proc) -> None:
    """Le harnais d'une session pty vit dans sa propre session : on ne le
    laisse pas orphelin quand le lanceur s'arrête."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _finish_pty(proc, master):
    """Le proxy a rendu la main alors que le harnais vit encore (terminal
    fermé sur SIGHUP/SIGTERM, erreur du pty) : fermer le maître puis tuer son
    groupe avant d'attendre, sinon `proc.wait()` bloque pour toujours — le
    harnais est dans sa propre session, sur un pty dont le maître resterait
    ouvert. Rend le maître à fermer (None s'il a été fermé ici)."""
    if proc.poll() is not None:
        return master
    if master is not None:
        try:
            os.close(master)
        except OSError:
            pass
        master = None
    _kill_group(proc)
    return master


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
    # L480 : la veille de la session (décisions et courrier entre deux messages)
    parser.add_argument("--veille", dest="veille", action="store_true", default=None,
                        help="watch the pending decisions and the new mail while the "
                             "session is open and have the harness present them without "
                             "waiting for a message (default: on with a terminal)")
    parser.add_argument("--no-veille", dest="veille", action="store_false",
                        help="do not watch decisions and mail during the session")
    parser.add_argument("--veille-every", type=float, default=VEILLE_EVERY_S, metavar="S",
                        help="veille period in seconds (%g-%g, default %g)"
                             % (VEILLE_EVERY_MIN_S, VEILLE_EVERY_MAX_S, VEILLE_EVERY_S))
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
            if args.veille is False:
                print("veille: off (--no-veille)")
            elif veille_armed(args, popen=popen):
                print("veille: on with the terminal, decisions and mail every %gs"
                      % args.veille_every)
            else:
                print("veille: off (no terminal on input and output)")
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
    proc, master, veille = None, None, None
    if args.veille and not veille_armed(args, popen=popen):
        print("ameesh chat: --veille needs a terminal on input and output; the "
              "session runs without it", file=sys.stderr)
    from .platform.child_environment import harness
    env = harness(env, args.harness, compte)
    try:
        slave = None
        if veille_armed(args, popen=popen):
            try:
                master, slave = pty.openpty()
                _copy_winsize(sys.stdin.fileno(), slave)
                # un marqueur `Stop` d'une session précédente ne vaut pas pour
                # celle-ci : on part fermé, le hook dira quand le prompt est rendu
                write_phase(cfg, name, PHASE_BUSY)
                veille = Veille(cfg, human, name, every=args.veille_every)
            except OSError as exc:
                print("ameesh chat: veille off (%s)" % exc, file=sys.stderr)
                if slave is not None:
                    os.close(slave)
                if master is not None:
                    os.close(master)
                master, slave, veille = None, None, None
        if veille is not None:
            # le harnais vit dans sa propre session, sur le pty : ses
            # permissions restent interactives, Ctrl-C et la taille le visent
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=slave,
                                    stdout=slave, stderr=slave, start_new_session=True,
                                    preexec_fn=_own_tty)
            os.close(slave)
        else:
            proc = (popen or subprocess.Popen)(command, cwd=cwd, env=env)
        # Ctrl-C est pour le harnais au premier plan, pas pour ce lanceur
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            if veille is not None:
                veille.start()
                serve_pty(proc, master, veille=veille)
                # le proxy a rendu la main : si le harnais vit encore (terminal
                # fermé, erreur du pty), fermer le maître et tuer son groupe
                # avant d'attendre — sinon ce wait ne rend jamais
                master = _finish_pty(proc, master)
                code = proc.wait()
            else:
                code = proc.wait()
        finally:
            signal.signal(signal.SIGINT, previous)
    except (OSError, subprocess.SubprocessError) as exc:
        print("ameesh chat: cannot launch: %s" % exc, file=sys.stderr)
    finally:
        if veille is not None:
            veille.stop()
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        if veille is not None and proc is not None and proc.poll() is None:
            _kill_group(proc)
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
    if not VEILLE_EVERY_MIN_S <= args.veille_every <= VEILLE_EVERY_MAX_S:
        print("ameesh chat: --veille-every must be between %g and %g seconds"
              % (VEILLE_EVERY_MIN_S, VEILLE_EVERY_MAX_S), file=sys.stderr)
        return 2
    if getattr(args, "agent", None) and args.veille is not None:
        print("ameesh chat: --veille belongs to the chat session (`ameesh chat` without "
              "an agent); the terminal client follows the conversation", file=sys.stderr)
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
