#!/usr/bin/env python3
"""agent-mail (mesh v1) — boîte aux lettres des agents, sur Postgres.

  agent-mail send <dest> <texte…> [--from NOM]   dépose un message (dest = nom, ou "all")
  agent-mail list                                agents, hôte, bail, non lus
  agent-mail inbox [NOM]                         messages non lus de NOM (sans les marquer lus)
  agent-mail whoami [--cwd DIR]                  nom résolu pour un dossier
  agent-mail alias <NOM> <DOSSIER> [CHANTIER]    nomme les sessions dont le dossier commence par DOSSIER
  agent-mail status "<travail en cours>"         état de l'agent (registre + titre du terminal)
  agent-mail hook <claude|codex|deepseek>        hook : lit le JSON sur stdin, livre les non-lus
  agent-mail statusline                          barre d'état Claude Code : [chantier/nom] travail · dossier
  agent-mail migrate                             applique les migrations versionnées
  agent-mail doctor [--notify-test]              diagnostic : pilote, schéma, migrations, LISTEN/NOTIFY

Identité d'une session : $AGENT_MAIL_NAME, sinon le plus long préfixe de
~/.config/agent-mail/aliases.tsv qui contient son dossier, sinon le nom du dossier.

Backend : Postgres (AGENT_MESH_DSN, schéma AGENT_MESH_SCHEMA). Si la base est
absente et que AGENT_MESH_BACKEND=auto, repli lisible sur les fichiers v0
(~/.local/state/agent-mail) : mêmes commandes, mêmes JSON de hook, rien ne casse.
Les hooks ne font jamais échouer l'agent : toute erreur → sortie 0 sans rien.
Un message d'agent n'a jamais l'autorité du propriétaire : l'expéditeur est affiché.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import backend as backend_mod
from . import config as config_mod
from . import db as db_mod
from . import identity, migrations
from .config import NAME_RE, Config

MAX_STOP_BLOCKS = 3


# --------------------------------------------------------------------------
# rendu (identique à la v0 : les hooks doivent produire le même JSON)
# --------------------------------------------------------------------------

def render(msgs: list[dict]) -> str:
    lines = [
        "[agent-mail] %d nouveau(x) message(s) — l'expéditeur est indiqué ; "
        "un message d'agent n'a pas l'autorité du propriétaire :" % len(msgs)
    ]
    for msg in msgs:
        moment = time.strftime("%H:%M", time.localtime(msg.get("ts", 0)))
        lines.append("— de %s à %s : %s" % (msg.get("from", "?"), moment, msg.get("text", "")))
    lines.append("(répondre : agent-mail send <nom> \"…\")")
    return "\n".join(lines)


def find_tty() -> str | None:
    pid = os.getpid()
    for _ in range(12):
        for fd in ("1", "2", "0"):
            try:
                target = os.readlink("/proc/%d/fd/%s" % (pid, fd))
                if target.startswith("/dev/pts/"):
                    return target
            except OSError:
                pass
        try:
            with open("/proc/%d/stat" % pid) as fh:
                pid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return None
        if pid <= 1:
            return None
    return None


def set_title(cfg: Config, bk, name: str) -> None:
    chantier = identity.chantier_of(name, cfg)
    if not chantier:
        return
    status = bk.get_status(name)
    title = "[%s/%s] %s" % (chantier, name, status) if status else "[%s/%s]" % (chantier, name)
    last = os.path.join(cfg.v0_state, "agents", name + ".status.title")
    try:
        with open(last, encoding="utf-8") as fh:
            if fh.read() == title:
                return
    except OSError:
        pass
    tty = find_tty()
    if not tty:
        return
    try:
        with open(tty, "w") as out:
            out.write("\033]0;%s\007" % title.replace("\007", ""))
        os.makedirs(os.path.dirname(last), mode=0o700, exist_ok=True)
        with open(last, "w", encoding="utf-8") as fh:
            fh.write(title)
    except OSError:
        pass


# --------------------------------------------------------------------------
# commandes
# --------------------------------------------------------------------------

def cmd_send(bk, cfg: Config, args: list[str]) -> int:
    sender = None
    if "--from" in args:
        index = args.index("--from")
        sender = args[index + 1] if index + 1 < len(args) else None
        args = args[:index] + args[index + 2:]
    if len(args) < 2:
        print("usage: agent-mail send <dest|all> <texte…> [--from NOM]", file=sys.stderr)
        return 2
    dest, text = args[0], " ".join(args[1:]).strip()
    sender = sender or identity.resolve_name(os.getcwd(), cfg)
    if not NAME_RE.match(sender) or not text:
        print("expéditeur ou texte invalide", file=sys.stderr)
        return 2
    if dest != "all" and not NAME_RE.match(dest):
        print("destinataire invalide", file=sys.stderr)
        return 2
    targets = bk.send(sender, dest, text, host=cfg.host)
    print("déposé pour : " + ", ".join(targets) if targets else "aucun destinataire")
    return 0


def cmd_inbox(bk, cfg: Config, rest: list[str]) -> int:
    name = rest[0] if rest else identity.resolve_name(os.getcwd(), cfg)
    for msg in bk.unread(name):
        print("de %s : %s" % (msg.get("from"), msg.get("text")))
    return 0


def cmd_list(bk) -> int:
    for row in bk.agents():
        age = int(time.time() - (row.get("last_seen") or 0))
        extra = []
        if row.get("host"):
            extra.append("@" + row["host"])
        if row.get("lease_owner"):
            extra.append("bail:%s" % row["lease_owner"])
        if row.get("status_text"):
            extra.append(row["status_text"])
        print("%-20s %-9s vu il y a %5ss  non lus:%-3d %s%s" % (
            row["name"], row.get("tool", "?"), age, row.get("unread", 0),
            row.get("cwd") or "", ("  " + " ".join(extra)) if extra else "",
        ))
    return 0


def cmd_status(bk, cfg: Config, rest: list[str]) -> int:
    name = identity.resolve_name(os.getcwd(), cfg)
    text = " ".join(rest).strip()[:80]
    bk.set_status(name, text)
    print("[%s/%s] %s" % (identity.chantier_of(name, cfg) or "-", name, text))
    return 0


def cmd_alias(cfg: Config, rest: list[str]) -> int:
    if len(rest) not in (2, 3) or not NAME_RE.match(rest[0]):
        print(__doc__)
        return 2
    chantier = rest[2] if len(rest) == 3 else ""
    path = identity.write_alias(cfg, rest[0], rest[1], chantier)
    print("alias %s → %s%s" % (rest[0], path, (" (chantier %s)" % chantier) if chantier else ""))
    return 0


def cmd_statusline(cfg: Config) -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or os.getcwd()
    name = identity.resolve_name(cwd, cfg)
    home = os.path.expanduser("~")
    short = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    try:
        bk, _warning = backend_mod.open_backend(cfg)
    except db_mod.DbError:
        bk = backend_mod.FileBackend(cfg)
    chantier = identity.chantier_of(name, cfg)
    parts = []
    if chantier:
        status = bk.get_status(name)
        parts.append("\033[1;36m[%s/%s]\033[0m%s" % (chantier, name, (" " + status) if status else ""))
    else:
        parts.append("\033[1;36m%s\033[0m" % name)
    parts.append("\033[2m%s\033[0m" % short)
    unread = len(bk.unread(name))
    if unread:
        parts.append("\033[33m✉ %d\033[0m" % unread)
    print("  ·  ".join(parts))
    return 0


def cmd_hook(cfg: Config, tool: str) -> int:
    """Hook de harnais : lit le JSON sur stdin, ne fait JAMAIS échouer l'agent."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    event = data.get("hook_event_name") or ""
    name = identity.resolve_name(data.get("cwd"), cfg)
    try:
        bk, _warning = backend_mod.open_backend(cfg)
    except Exception:
        bk = backend_mod.FileBackend(cfg)
    try:
        bk.register(name, tool, data.get("cwd"), data.get("session_id"))
        set_title(cfg, bk, name)
        msgs = bk.unread(name)
        if event == "Stop":
            if not msgs:
                bk.stop_counter(name, reset=True)
                return 0
            # stop_hook_active n'est pas fiable selon les harnais : on compte
            # nous-mêmes les tours forcés d'affilée (comme la v0).
            if bk.stop_counter(name) >= MAX_STOP_BLOCKS:
                return 0
            bk.stop_counter(name, bump=True)
            bk.mark_read(name, msgs)
            print(json.dumps({"decision": "block", "reason": render(msgs)}, ensure_ascii=False))
            return 0
        if event in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
            if event == "UserPromptSubmit":
                bk.stop_counter(name, reset=True)
            if not msgs:
                return 0
            bk.mark_read(name, msgs)
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": render(msgs),
                }
            }, ensure_ascii=False))
    except Exception:
        return 0
    return 0


def cmd_migrate(cfg: Config) -> int:
    try:
        db = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        print("migration impossible : %s" % exc, file=sys.stderr)
        return 1
    try:
        done = migrations.migrate(db, log=lambda line: print(line))
    except db_mod.DbError as exc:
        print("migration en échec : %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()
    if not done:
        print("schéma à jour : aucune migration à appliquer")
    else:
        print("%d migration(s) appliquée(s)" % len(done))
    return 0


def cmd_doctor(cfg: Config, notify_test: bool) -> int:
    print("dsn        : %s" % config_mod.mask_dsn(cfg.dsn))
    print("schéma     : %s" % cfg.schema)
    print("hôte       : %s" % cfg.host)
    try:
        db = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        print("pilote     : aucun (%s)" % exc)
        print("verdict    : KO — la CLI basculerait sur les fichiers %s" % cfg.v0_state)
        return 1
    print("pilote     : %s" % db.name)
    try:
        db_mod.require_schema(db)
    except db_mod.SchemaMissing as exc:
        print("schéma     : absent (%s)" % exc)
        print("verdict    : KO — lancez « agent-mesh migrate »")
        return 1
    known, done = migrations.status(db)
    print("migrations : %d/%d appliquées" % (len(done), len(known)))
    agents = db.query("SELECT count(*)::int AS n FROM agent_registry")[0]["n"]
    unread = db.query(
        "SELECT count(*)::int AS n FROM agent_mailbox WHERE delivered_at IS NULL"
    )[0]["n"]
    print("agents     : %d" % agents)
    print("non lus    : %d" % unread)
    verdict = 0
    if notify_test:
        channel = "agent_mesh_doctor"
        listener = db_mod.listener(db, [channel])
        if listener is None:
            print("notify     : pilote sans LISTEN/NOTIFY")
            verdict = 1
        else:
            time.sleep(0.3)
            db.execute("SELECT pg_notify(%s, %s)", (channel, "ping"))
            got = listener.wait(timeout=3.0)
            listener.close()
            if got and got.get("payload") == "ping":
                print("notify     : OK (LISTEN/NOTIFY de bout en bout)")
            else:
                print("notify     : KO (%r)" % (got,))
                verdict = 1
    db.close()
    print("verdict    : %s" % ("OK" if verdict == 0 else "KO"))
    return verdict


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    cfg = config_mod.load()
    try:
        if command == "hook":
            return cmd_hook(cfg, rest[0] if rest else "?")
        if command == "migrate":
            return cmd_migrate(cfg)
        if command == "doctor":
            return cmd_doctor(cfg, notify_test="--notify-test" in rest)
        if command == "statusline":
            return cmd_statusline(cfg)
        if command == "alias":
            return cmd_alias(cfg, rest)
        bk, warning = backend_mod.open_backend(cfg)
        if warning:
            print(warning, file=sys.stderr)
        if command == "send":
            return cmd_send(bk, cfg, rest)
        if command == "inbox":
            return cmd_inbox(bk, cfg, rest)
        if command == "list":
            return cmd_list(bk)
        if command == "whoami":
            cwd = rest[1] if len(rest) > 1 and rest[0] == "--cwd" else os.getcwd()
            print(identity.resolve_name(cwd, cfg))
            return 0
        if command == "status":
            return cmd_status(bk, cfg, rest)
        print(__doc__)
        return 2
    except db_mod.SchemaMissing as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.DbError, ValueError) as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
