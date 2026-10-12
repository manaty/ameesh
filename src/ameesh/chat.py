# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh chat — the owner's conversation agent (work item L123).

  ameesh chat [--harness claude|codex|…] [--name chat-<human>] [--cwd DIR]
        opens an INTERACTIVE harness session (Claude by default) for the
        conversation agent of the human who launches it: decision requests
        from every project, their answers (`ameesh decide`), their other
        requests forwarded to the orchestrators;
  ameesh chat --instruction  prints the chat instructions (text versioned in the repository);
  ameesh chat --dry-run      says what would be launched, without launching or writing anything.

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
import re
import signal
import string
import subprocess
import sys
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
                        help="say what would be launched; launch nothing, write nothing")
    return parser


def run(cfg: Config, args, *, chain=None, environ_of=None, popen=None) -> int:
    """`ameesh chat` (sans `--consigne`) : contrôles, inscription, lancement."""
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
        # relue aussi DANS la session (contexte résumé) : le nom de ce chat
        from .decisions import CHAT_ENV, current_human, unix_user
        name = args.name or os.environ.get(CHAT_ENV) or default_name(unix_user())
        print(instruction(current_human(), name))
        return 0
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
